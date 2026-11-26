# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""``chrys.extensions`` adapter: the browser core as an iCode agent's tools.

One extension id (``browser``) owns a registry of ``browser_id -> BrowserSession``.
Every id gets its own state directory (one slot under the owner's profile folder),
so it also gets its own Chrome profile and instance lock. Ownership is keyed by
``(ToolContext.session_id, ToolContext.agent_name)`` plus caller/invocation
metadata (``ToolContext.invocation_type``, ``ToolContext.caller_name``,
``ToolContext.invocation_id``): main-agent chat turns keep one stable owner key
across turns, while sub-agent calls, workflow node activations and side
questions are isolated from each other (workflow retries keep the same
``invocation_id`` and retain access). On older hosts without those fields,
concurrent builds sharing the same session and profile name share that owner
scope and must pass explicit ``browser_id`` values (or use distinct profiles /
``max_concurrency: 1``).
The adapter never imports iCode beyond ``chrys.extensions``; the core itself does
not import iCode at all.

Configuration uses ``ICODE_BROWSER_*`` environment variables:

- ``ICODE_BROWSER_ALLOWED_DOMAINS`` (optional comma-separated domain allowlist)
- ``ICODE_BROWSER_HEADLESS`` (default off: a visible window)
- ``ICODE_BROWSER_STATE_DIR`` (default: ``~/.cache/icode-browser-use``)
- ``ICODE_BROWSER_GUARD=strict`` blocks ``shell``/``filesystem.write`` while a
  browser is open for the same agent.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import dataclasses
import hashlib
import logging
import os
import secrets
import shutil
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any, Final, Literal
from urllib.parse import urljoin, urlsplit

from chrys.extensions import Extension, ToolCall, ToolContext, ToolError, TurnContext

from . import cli, registry
from ._proc import atomic_write_private
from .actions import build_tool_defs
from .broker_client import BrokerCallUnknown, BrokerSession
from .config import BrowserConfig, BrowserEnv, ConfigError
from .errors import PRE_DISPATCH_ERROR_CODES, Blocked, BrowserError, ConnectionClosed, Unavailable
from .locking import (
    InstanceLock,
    acquire_file_lock,
    acquire_instance_lock,
    probe_instance_lock,
    try_acquire_file_lock,
    try_acquire_instance_lock,
)
from .policy import canonical_origin
from .logging import set_log_sink
from .prompts import BROWSER_PLAYBOOK, COMPACT_BROWSER_PLAYBOOK
from .runtime import BrowserRuntime, call_tool
from .session import BrowserSession

logger = logging.getLogger(__name__)

extension = Extension()

#: Stable plugin CLI entry point (ready for host ``icode plugin browser-use`` forwarding).
cli_main = cli.main

#: 15 high-cohesion core tools exposed when ``ICODE_BROWSER_TOOLSET=compact`` is set.
#: Includes ``browser_list`` for post-compaction handle recovery, ``browser_scroll`` for
#: long/lazy pages, and ``browser_select_option`` for native form dropdowns.
COMPACT_TOOL_NAMES: Final[frozenset[str]] = frozenset(
    {
        "browser_open",
        "browser_list",
        "browser_navigate",
        "browser_snapshot",
        "browser_find",
        "browser_click",
        "browser_fill",
        "browser_press_key",
        "browser_select_option",
        "browser_wait_for",
        "browser_scroll",
        "browser_extract",
        "browser_screenshot",
        "browser_dialog",
        "browser_close",
    }
)

_WRITE_TOOL_NAMES: Final[frozenset[str]] = frozenset(
    {
        "browser_click",
        "browser_drag",
        "browser_fill",
        "browser_press_key",
        "browser_upload",
        "browser_select_option",
        "browser_dialog",
        "browser_evaluate",
    }
)

_PRE_DISPATCH_ERROR_CODES: Final[frozenset[str]] = PRE_DISPATCH_ERROR_CODES


def _sync_write_actions(browser: _Browser) -> bool:
    """Sync ``browser.write_actions`` from authoritative session/journal counters.

    Returns True when an authoritative counter exists (race-free under concurrent
    calls on the same handle), or False when the caller must fall back to
    per-call tracking (e.g. ``BrokerSession`` or minimal test doubles).
    """
    session = browser.session
    if isinstance(session, BrokerSession):
        return False
    count_fn = getattr(session, "dispatched_write_count", None)
    if callable(count_fn):
        browser.write_actions = int(count_fn(_WRITE_TOOL_NAMES))
        return True
    journal = getattr(session, "journal", None)
    dispatched_for = getattr(journal, "dispatched_for", None)
    if callable(dispatched_for):
        browser.write_actions = int(dispatched_for(_WRITE_TOOL_NAMES))
        return True
    return False


def _was_dispatched_write(name: str, text: str) -> bool:
    """Fallback write-dispatch check when no local structured journal is available.

    Inspects only the action's own header lines before any ``[EXPECT]`` section
    (which carries untrusted page text), and never inspects ``browser_evaluate``
    output.
    """
    if name == "browser_evaluate":
        return True
    header = text.split("\n[EXPECT]\n", 1)[0]
    lines = [line.strip() for line in header.splitlines()]
    if "dispatch=sent" in lines:
        return True
    if "dispatch=not_sent" in lines:
        return False
    has_dialog = any(line.startswith("dialog=") for line in lines)
    has_blocked = any(
        line.startswith("status=blocked") or line.endswith("；status=blocked retryable=false") for line in lines
    )
    if has_blocked and not has_dialog:
        return False
    return True


def _active_toolset() -> str:
    raw = (BrowserEnv().get("TOOLSET") or "full").strip().lower()
    if raw not in {"full", "compact"}:
        logger.warning("invalid ICODE_BROWSER_TOOLSET=%r; falling back to 'full' during tool registration", raw)
        return "full"
    return raw


def _tool(*, requires_approval: bool) -> Any:
    """Register on *extension* unless ``ICODE_BROWSER_TOOLSET=compact`` excludes this tool."""

    def decorator(fn: Any) -> Any:
        if _active_toolset() == "full" or fn.__name__ in COMPACT_TOOL_NAMES:
            return extension.tool(requires_approval=requires_approval)(fn)
        return fn

    return decorator


#: The core's tool definitions; the adapter reuses their descriptions and
#: parameter specs so the two front ends cannot drift.
def _env_enabled(name: str) -> bool:
    return BrowserEnv().enabled(name)


_TOOLS = {
    tool.name: tool
    for tool in build_tool_defs(
        evaluate_enabled=_env_enabled("ICODE_BROWSER_EVALUATE"),
        save_state_enabled=_env_enabled("ICODE_BROWSER_SAVE_STATE"),
    )
}

#: ``browser_evaluate`` and ``browser_save_state`` are opt-in high-risk tools.
#: They are absent from the extension tool list unless the matching env flag is set.


@dataclass
class _Browser:
    """One open browser, owned by the agent build that opened it."""

    id: str
    session: BrowserSession | BrokerSession
    owner_session: str | None
    owner_agent: str
    owner_scope: str | None = None
    slot: int = 0
    url: str = ""
    title: str = ""
    backend: str = ""
    record: registry.ManifestRecord | None = None
    manifest: Path | None = None
    slot_state_dir: Path | None = None
    ephemeral_state_dir: Path | None = None
    #: True while an implicit browser_navigate has not started successfully yet:
    #: its record is not written, so a failed start keeps the previous generation's.
    record_pending: bool = False
    #: True when this handle is creating a brand-new Owned slot dir (no prior dir or manifest record).
    created_fresh_state_dir: bool = False
    reclaim_on_idle: bool = False
    manifest_committed: bool = False
    superseded: bool = False
    close_task: asyncio.Task[None] | None = None
    idle_reclaim_task: asyncio.Task[None] | None = None
    slot_generation_stamped: bool = False
    visited_origins: list[str] = field(default_factory=list)
    blocked_origins: list[str] = field(default_factory=list)
    screenshots: list[str] = field(default_factory=list)
    write_actions: int = 0


_browsers: dict[str, _Browser] = {}
_reclaiming_slots: set[tuple[str | None, str, str | None, int]] = set()
_allocating_slots: set[tuple[str | None, str, str | None, int]] = set()
_MAX_BROWSERS = 2
"""Runaway guard: one agent build may keep at most this many browsers open."""


def _log_sink(line: str) -> None:
    logger.debug("%s", line)


set_log_sink(_log_sink)


def _owner_scope(ctx: ToolContext) -> str | None:
    """Per-caller / per-invocation isolation key beyond ``(session_id, agent_name)``.

    - Main-agent chat turns (``invocation_type == "turn"`` with ``caller_name``
      matching ``agent_name``) return ``None`` so the main agent keeps one
      stable profile/journal key and reuses open browsers across turns.
    - Non-turn invocations (``sub_agent``, ``workflow_node``, ``side_question``)
      scope by ``(invocation_type, caller_name, invocation_id)`` so distinct
      sub-agent tools, concurrent calls to the same sub-agent, and separate
      workflow node activations never share handles, slots, profiles or
      journals, while workflow retries (same ``invocation_id``) keep access.
    - Outside an invocation (``invocation_type is None``), a distinct
      ``caller_name`` (different from ``agent_name``) still isolates builds
      sharing a profile.
    """
    invocation_type = getattr(ctx, "invocation_type", None)
    caller_name = getattr(ctx, "caller_name", None)
    invocation_id = getattr(ctx, "invocation_id", None)
    if invocation_type is not None and invocation_type != "turn":
        caller = caller_name or ctx.agent_name
        if invocation_id:
            return f"{invocation_type}\x00{caller}\x00{invocation_id}"
        return f"{invocation_type}\x00{caller}"
    if caller_name and caller_name != ctx.agent_name:
        return f"caller\x00{caller_name}"
    return None


def _is_ephemeral_invocation(ctx: ToolContext) -> bool:
    """Whether *ctx* belongs to a one-shot invocation whose Owned profile dir cannot be reused after close."""
    invocation_type = getattr(ctx, "invocation_type", None)
    invocation_id = getattr(ctx, "invocation_id", None)
    return invocation_type is not None and invocation_type != "turn" and bool(invocation_id)


def _is_reclaimable_on_idle(ctx: ToolContext) -> bool:
    """Whether an idle timeout should fully close and reclaim this one-shot invocation.

    Sub-agents and side questions get a fresh ``invocation_id`` on every call,
    so an abandoned handle is unreachable once its caller finishes. Workflow
    nodes keep their ``invocation_id`` across retries and must retain their
    recorded slot/profile on idle.
    """
    invocation_type = getattr(ctx, "invocation_type", None)
    invocation_id = getattr(ctx, "invocation_id", None)
    return invocation_type in {"sub_agent", "side_question"} and bool(invocation_id)


def _ctx_slot_key(ctx: ToolContext, slot: int) -> tuple[str | None, str, str | None, int]:
    return (ctx.session_id, ctx.agent_name, _owner_scope(ctx), slot)


def _slot_key(browser: _Browser) -> tuple[str | None, str, str | None, int]:
    return (browser.owner_session, browser.owner_agent, browser.owner_scope, browser.slot)


def _owner_matches(browser: _Browser, ctx: ToolContext) -> bool:
    """Match the explicit browser handle to its owning session, agent profile and scope."""
    return (
        browser.owner_session == ctx.session_id
        and browser.owner_agent == ctx.agent_name
        and browser.owner_scope == _owner_scope(ctx)
    )


def _owner_key(ctx: ToolContext) -> str:
    """A stable, filesystem-safe key for one owner scope's profile and journal.

    Main-agent turns have ``_owner_scope(ctx) is None`` so their legacy key
    ``sha256(f"{session_id or '-'}\\x00{agent_name}")[:16]`` stays unchanged.
    """
    identity = f"{ctx.session_id or '-'}\x00{ctx.agent_name}"
    scope = _owner_scope(ctx)
    if scope:
        identity += f"\x00{scope}"
    return hashlib.sha256(identity.encode()).hexdigest()[:16]


def _prune_closed() -> None:
    """Drop entries whose core session closed itself (crash, explicit close)."""
    for browser_id, browser in list(_browsers.items()):
        if browser.session.state == "closed":
            _browsers.pop(browser_id, None)


def _mine(ctx: ToolContext) -> list[_Browser]:
    _prune_closed()
    return [b for b in _browsers.values() if _owner_matches(b, ctx)]


def _image_size(data: bytes) -> tuple[int, int] | None:
    """Read PNG/JPEG pixel dimensions without a decoding dependency."""
    if len(data) >= 24 and data[:8] == b"\x89PNG\r\n\x1a\n":
        return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    if len(data) < 4 or data[:2] != b"\xff\xd8":
        return None
    index = 2
    while index + 9 < len(data):
        if data[index] != 0xFF:
            index += 1
            continue
        marker = data[index + 1]
        length = int.from_bytes(data[index + 2 : index + 4], "big")
        if marker in {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}:
            return (
                int.from_bytes(data[index + 7 : index + 9], "big"),
                int.from_bytes(data[index + 5 : index + 7], "big"),
            )
        index += 2 + length
    return None


def _screenshot_directory(ctx: ToolContext) -> Path:
    """The plugin's own screenshots folder inside the session (or per-user) extension data folder."""
    return (ctx.session_data_dir or ctx.data_dir) / "screenshots"


def _write_screenshot(directory: Path, data: bytes, suffix: str) -> Path:
    """Write one screenshot under a unique name, atomically: readers never see a partial file."""
    directory.mkdir(parents=True, exist_ok=True)
    for _ in range(5):
        name = f"screenshot-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{secrets.token_hex(4)}{suffix}"
        path = directory / name
        if path.exists():
            continue  # never replace an existing capture
        temporary = directory / f".{name}.{secrets.token_hex(4)}.tmp"
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            continue
        try:
            stream = os.fdopen(descriptor, "wb")
        except BaseException:
            with contextlib.suppress(OSError):
                os.close(descriptor)
            raise
        try:
            with stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            return path
        finally:
            with contextlib.suppress(OSError):
                temporary.unlink()
    raise OSError("could not find a free screenshot name")


def _prune_screenshots(directory: Path, keep: int, current: Path) -> None:
    """Remove only this plugin's older screenshots; the one just written and other files stay."""
    try:
        shots = sorted(
            (
                path
                for path in directory.iterdir()
                if path.name.startswith("screenshot-")
                and path.suffix.lower() in {".png", ".jpg", ".jpeg"}
                and path.is_file()
                and not path.is_symlink()
            ),
            key=lambda path: (path.stat().st_mtime, path.name),
            reverse=True,
        )
        orphans = [
            path for path in directory.iterdir() if path.name.startswith(".screenshot-") and path.name.endswith(".tmp")
        ]
    except OSError as exc:
        logger.warning("screenshot retention skipped: %s", exc)
        return
    survivors = {current}
    for path in shots:
        if len(survivors) >= max(keep, 1):
            break
        survivors.add(path)
    for stale in shots:
        if stale in survivors:
            continue
        try:
            stale.unlink()
        except OSError as exc:
            logger.warning("screenshot retention could not remove %s: %s", stale, exc)
    for orphan in orphans:
        try:
            # A concurrent capture's temporary file lives for milliseconds; only a
            # crashed writer leaves one, so an age bound keeps live writes intact.
            if time.time() - orphan.stat().st_mtime > 600:
                orphan.unlink()
        except OSError as exc:
            logger.warning("screenshot cleanup skipped %s: %s", orphan, exc)


def _origin(url: str) -> str:
    """Scheme and host only: context must not leak credentials or query tokens into prompts."""
    if not url:
        return "(not navigated yet)"
    origin = canonical_origin(url)
    if origin:
        return origin
    parts = urlsplit(url)
    if parts.scheme and parts.hostname:
        host = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
        try:
            port = parts.port
        except ValueError:
            port = None
        return f"{parts.scheme}://{host}:{port}" if port is not None else f"{parts.scheme}://{host}"
    return url[:80]


def _config() -> BrowserConfig:
    try:
        return BrowserConfig.from_env()
    except ConfigError as exc:
        raise ToolError(str(exc), code="browser_not_configured") from exc


def _manifest_path(ctx: ToolContext) -> Path | None:
    """The session's manifest file, or None when the host gave the plugin no folder."""
    try:
        return registry.manifest_path(ctx)
    except RuntimeError:  # a test context without a data folder
        return None


def _save_record(path: Path | None, record: registry.ManifestRecord) -> bool:
    """Persist one record, superseding older generations of its slot; never breaks a call."""
    if path is None:
        return False
    try:
        with _manifest_lock(path):
            records, problem = registry.load(path)
            if problem in {"unreadable", "unexpected shape"}:
                logger.warning("browser manifest %s; skipping write to avoid wiping records", problem)
                return False
            registry.save(path, registry.supersede(records, record))
            return True
    except OSError as exc:
        logger.warning("browser manifest write failed: %s", exc)
        return False


def _slot_has_manifest_record(path: Path | None, owner_key: str, slot: int) -> bool:
    """Whether *path* already holds any record for ``(owner_key, slot)`` (fails closed on read error)."""
    if path is None:
        return False
    try:
        with _manifest_lock(path):
            records, problem = registry.load(path)
    except OSError:
        return True
    if problem in {"unreadable", "unexpected shape"}:
        return True
    return any(item.owner_key == owner_key and item.slot == slot for item in records)


def _update_record_if_owned(
    path: Path | None,
    record: registry.ManifestRecord,
    *,
    slot_state_dir: Path | None = None,
) -> bool:
    """Persist an updated record only while *record.resume_id* still owns its slot."""
    if path is None:
        return False
    if slot_state_dir is not None:
        stamped = _read_slot_generation(slot_state_dir)
        if stamped and stamped != record.resume_id:
            logger.warning(
                "browser manifest update skipped: slot %d belongs to %s, not %s",
                record.slot,
                stamped,
                record.resume_id,
            )
            return False
    try:
        with _manifest_lock(path):
            records, problem = registry.load(path)
            if problem in {"unreadable", "unexpected shape"}:
                logger.warning("browser manifest %s; skipping write to avoid wiping records", problem)
                return False
            same_slot = [item for item in records if item.owner_key == record.owner_key and item.slot == record.slot]
            if not same_slot or any(item.resume_id != record.resume_id for item in same_slot):
                logger.warning(
                    "browser manifest update skipped: slot %d is no longer owned by %s",
                    record.slot,
                    record.resume_id,
                )
                return False
            registry.save(path, registry.upsert(records, record))
            return True
    except OSError as exc:
        logger.warning("browser manifest write failed: %s", exc)
        return False


_MANIFEST_LOCKS: dict[str, threading.Lock] = {}
_MANIFEST_LOCKS_GUARD = threading.Lock()


def _manifest_lock(path: Path) -> threading.Lock:
    """One lock per manifest file: its load-modify-save runs in worker threads, and an
    atomic replace alone lets the later writer silently discard the earlier change."""
    key = os.path.normcase(os.path.abspath(path))
    with _MANIFEST_LOCKS_GUARD:
        return _MANIFEST_LOCKS.setdefault(key, threading.Lock())


async def _asave_record(path: Path | None, record: registry.ManifestRecord) -> bool:
    """Off-loop manifest persistence so Windows ``icacls`` never stalls the event loop."""
    if path is None:
        return False
    return await asyncio.to_thread(_save_record, path, record)


def _browser_origin(browser: _Browser) -> str:
    """The redacted page origin, falling back to the live session snapshot."""
    return registry.redacted_origin(browser.url or browser.session.snapshot.url)


async def _apersist(browser: _Browser) -> None:
    """Async off-loop origin persistence; never overwrites a slot taken over by another handle."""
    if browser.superseded or browser.manifest is None or browser.record is None or browser.record_pending:
        return
    origin = _browser_origin(browser)
    if browser.record.origin == origin:
        return
    updated = dataclasses.replace(browser.record, origin=origin, updated_at=time.time())
    saved = await asyncio.to_thread(
        _update_record_if_owned,
        browser.manifest,
        updated,
        slot_state_dir=browser.slot_state_dir,
    )
    if saved:
        browser.record = updated
    elif _check_slot_ownership(browser) is not None:
        browser.superseded = True


def _recoverable(ctx: ToolContext) -> list[registry.ManifestRecord]:
    """Manifest records this session/agent may reopen, excluding live handles."""
    path = _manifest_path(ctx)
    if path is None:
        return []
    records, _problem = registry.load(path)
    live = {browser.record.resume_id for browser in _mine(ctx) if browser.record is not None}
    owner_key = _owner_key(ctx)
    return [
        record
        for record in records
        if record.owner_key == owner_key
        and record.session_id == ctx.session_id
        and record.agent_name == ctx.agent_name
        and record.resume_id not in live
    ]


_DEFER_MANIFEST_PERSIST = False


def _next_free_slot(ctx: ToolContext) -> int:
    """Return the first free profile slot for *ctx*, or raise ``browser_limit``."""
    _prune_closed()
    busy_slots = _reclaiming_slots | _allocating_slots
    used = {browser.slot for browser in _mine(ctx)} | {
        slot for slot in range(_MAX_BROWSERS) if _ctx_slot_key(ctx, slot) in busy_slots
    }
    slot = next((index for index in range(_MAX_BROWSERS) if index not in used), None)
    if slot is None:
        raise ToolError(
            f"This agent already has {_MAX_BROWSERS} browsers open; reuse a browser_id or call browser_close.",
            code="browser_limit",
        )
    return slot


def _open(ctx: ToolContext) -> _Browser:
    """Allocate a browser in the first free profile slot for this agent build.

    The registry is mutated only in this synchronous block, so no lock is needed:
    there is no ``await`` between reading the used slots and writing the entry.
    """
    base = _config()
    slot = _next_free_slot(ctx)
    return _allocate(ctx, base, slot, persist=not _DEFER_MANIFEST_PERSIST)


_DEFAULT_OPEN = _open


def _allocate_via_open(ctx: ToolContext, base: BrowserConfig, slot: int) -> _Browser:
    if _open is not _DEFAULT_OPEN:
        return _open(ctx)
    return _allocate(ctx, base, slot, persist=False)


def _allocate(ctx: ToolContext, base: BrowserConfig, slot: int, *, persist: bool = True) -> _Browser:
    """Create the handle and core session for *slot*; nothing launches yet."""
    browser_id = "b" + secrets.token_hex(4)
    generation = secrets.token_hex(4)
    state_dir = base.state_dir / "agents" / _owner_key(ctx) / f"slot-{slot}"
    # Bridge mode must use the same discovery file the native host reads; per-agent
    # state dirs are for owned profiles only. One bridge per state dir is enforced by
    # BridgeServer's instance lock, so a second agent gets a clear "resource busy".
    # Explicit browser_save_state archives always land under base.state_archive_dir,
    # isolated by owner and slot (outside the per-slot profile dir) so ephemeral close
    # cleanup never deletes them and concurrent sessions/slots never overwrite each other.
    archive_dir = base.state_archive_dir / "agents" / _owner_key(ctx) / f"slot-{slot}"
    config = (
        base
        if base.broker
        else dataclasses.replace(base, state_archive_root=archive_dir)
        if base.bridge
        else dataclasses.replace(base, state_dir=state_dir, state_archive_root=archive_dir)
    )
    if config.backend == "owned" and config.download_dir is not None:
        # Controlled downloads are session-scoped (the handle owns the browser).
        config = dataclasses.replace(config, download_dir=(ctx.session_data_dir or ctx.data_dir) / "downloads")
    reclaim_on_idle = _is_reclaimable_on_idle(ctx)

    async def _reclaim_abandoned_idle() -> None:
        target = _browsers.get(browser_id)
        if target is None or target.session.state != "idle":
            return
        await _ensure_close_task(target)

    if base.broker:
        session: BrowserSession | BrokerSession = BrokerSession(
            base,
            owner=_owner_key(ctx),
            session_id=f"{_owner_key(ctx)}-slot-{slot}",
        )
    else:
        session_kwargs: dict[str, Any] = {
            "journal_owner": _owner_key(ctx),
            "journal_session": f"{_owner_key(ctx)}-slot-{slot}",
        }
        if reclaim_on_idle:
            session_kwargs["on_idle"] = _reclaim_abandoned_idle
        session = BrowserSession(config, **session_kwargs)
    backend = base.backend
    record = registry.new_record(
        browser_id=browser_id,
        resume_id=f"s{slot}-{generation}",
        session_id=ctx.session_id,
        agent_name=ctx.agent_name,
        owner_key=_owner_key(ctx),
        backend=backend,
        generation=generation,
        slot=slot,
        origin_url=session.snapshot.url,
        updated_at=time.time(),
    )
    manifest = _manifest_path(ctx)
    owner_key = _owner_key(ctx)
    slot_state_dir = state_dir if backend == "owned" else None
    created_fresh_state_dir = (
        slot_state_dir is not None
        and not slot_state_dir.exists()
        and not _slot_has_manifest_record(manifest, owner_key, slot)
    )
    browser = _Browser(
        browser_id,
        session,
        ctx.session_id,
        ctx.agent_name,
        owner_scope=_owner_scope(ctx),
        slot=slot,
        backend=backend,
        record=record,
        manifest=manifest,
        slot_state_dir=slot_state_dir,
        ephemeral_state_dir=state_dir if backend == "owned" and _is_ephemeral_invocation(ctx) else None,
        created_fresh_state_dir=created_fresh_state_dir,
        reclaim_on_idle=reclaim_on_idle,
    )
    set_guard = getattr(session, "set_slot_acquire_guard", None)
    if slot_state_dir is not None and callable(set_guard):

        async def _verify_slot_on_acquire() -> None:
            target = _browsers.get(browser_id)
            if target is None or target.slot_state_dir is None:
                return
            slot_dir = target.slot_state_dir
            slot_lock = await acquire_file_lock(_slot_lifecycle_lock_path(slot_dir), wait=5.0)
            if slot_lock is None:
                raise Unavailable(
                    "That profile slot is busy with another plugin lifecycle operation; retry after it finishes.",
                    pre_dispatch=True,
                )
            try:
                if target.record_pending:
                    if target.created_fresh_state_dir:
                        preexisting = any(
                            child.name not in {"instance.lock", "instance.owner.json"} for child in slot_dir.iterdir()
                        )
                        has_record = target.record is not None and _slot_has_manifest_record(
                            target.manifest, target.record.owner_key, target.slot
                        )
                        if preexisting or has_record:
                            target.created_fresh_state_dir = False
                    return
                reason = _check_slot_ownership(target)
                if reason is not None:
                    target.superseded = True
                    err = BrowserError(
                        f"Browser handle {target.id} is no longer the active owner of its slot ({reason}); "
                        "call browser_close on this stale handle and use browser_list / browser_resume or browser_open.",
                        pre_dispatch=True,
                    )
                    err.code = "browser_superseded"
                    raise err
                if target.record is not None and target.record.resume_id:
                    _write_slot_generation(slot_dir, target.record.resume_id)
                    target.slot_generation_stamped = True
            finally:
                slot_lock.release()

        set_guard(_verify_slot_on_acquire)
    _browsers[browser_id] = browser
    if persist:
        if _save_record(manifest, record):
            browser.manifest_committed = True
        if reclaim_on_idle:
            _arm_unstarted_idle_reclaim(browser, base.idle_close_seconds)
    return browser


def _is_unstarted_session(session: BrowserSession | BrokerSession) -> bool:
    """Whether *session* has not launched/connected yet (so its own idle watchdog is not active)."""
    if isinstance(session, BrokerSession):
        return not session.connected
    return not getattr(session, "started_once", False) and getattr(session, "state", "") in {"idle", "failed"}


def _arm_unstarted_idle_reclaim(browser: _Browser, idle_close_seconds: float) -> None:
    """Reclaim an abandoned one-shot sub-agent/side-question handle that never launched Chrome."""
    if idle_close_seconds <= 0 or browser.idle_reclaim_task is not None:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    browser_id = browser.id
    delay = max(0.5, float(idle_close_seconds))

    async def _reclaim_unstarted_idle() -> None:
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        target = _browsers.get(browser_id)
        if target is None or target.close_task is not None:
            return
        if not _is_unstarted_session(target.session):
            return
        target.idle_reclaim_task = None
        await _ensure_close_task(target)

    browser.idle_reclaim_task = loop.create_task(_reclaim_unstarted_idle())


def _cancel_unstarted_idle_reclaim(browser: _Browser) -> None:
    task = browser.idle_reclaim_task
    browser.idle_reclaim_task = None
    if task is not None and task is not asyncio.current_task() and not task.done():
        task.cancel()


def _maybe_rearm_unstarted_idle_reclaim(browser: _Browser) -> None:
    if (
        browser.reclaim_on_idle
        and not browser.record_pending
        and browser.close_task is None
        and browser.id in _browsers
        and _is_unstarted_session(browser.session)
    ):
        _arm_unstarted_idle_reclaim(browser, browser.session.cfg.idle_close_seconds)


def _check_slot_ownership(browser: _Browser) -> str | None:
    """Return a conflict description when *browser* has been superseded on its slot, else ``None``."""
    if browser.superseded:
        return "this browser handle was superseded by a newer session on the same slot"
    if browser.record is None or not browser.record.resume_id or browser.record_pending:
        return None
    expected_id = browser.record.resume_id
    if browser.slot_state_dir is not None:
        stamped = _read_slot_generation(browser.slot_state_dir)
        if stamped and stamped != expected_id:
            return f"slot {browser.slot} is now owned by newer generation {stamped} (this handle is {expected_id})"
    if browser.manifest is not None:
        try:
            with _manifest_lock(browser.manifest):
                records, problem = registry.load(browser.manifest)
        except OSError:
            return None
        if problem not in {"unreadable", "unexpected shape"}:
            owner_key = browser.record.owner_key
            slot = browser.record.slot
            same_slot = [item for item in records if item.owner_key == owner_key and item.slot == slot]
            if same_slot:
                other = next((item.resume_id for item in same_slot if item.resume_id != expected_id), None)
                if other is not None:
                    return f"slot {slot} is now owned by newer generation {other} (this handle is {expected_id})"
            elif browser.manifest_committed and browser.manifest.exists():
                return f"slot {slot} record {expected_id} is no longer active in the manifest"
    return None


async def _ensure_handle_active(browser: _Browser) -> None:
    """Reject operations on handles whose slot was already resumed/superseded by another instance."""
    reason = await asyncio.to_thread(_check_slot_ownership, browser)
    if reason is not None:
        browser.superseded = True
        raise ToolError(
            f"Browser handle {browser.id} is no longer the active owner of its slot ({reason}); "
            "call browser_close on this stale handle and use browser_list / browser_resume or browser_open.",
            code="browser_superseded",
        )


def _get(ctx: ToolContext, browser_id: str) -> _Browser:
    _prune_closed()
    browser = _browsers.get(browser_id)
    if browser is None:
        hint = "call browser_list to see this session's browsers, or browser_open to allocate a new browser handle."
        raise ToolError(
            f"Unknown browser_id {browser_id!r}; {hint}",
            code="unknown_browser",
        )
    if not _owner_matches(browser, ctx):
        raise ToolError(
            "That browser belongs to another agent; allocate your own handle with browser_open.",
            code="browser_owner_mismatch",
        )
    return browser


def _take(ctx: ToolContext, browser_id: str) -> _Browser:
    browser = _get(ctx, browser_id)
    _browsers.pop(browser.id, None)
    return browser


async def _call(browser: _Browser, name: str, arguments: dict[str, object]) -> list[dict[str, object]]:
    try:
        if isinstance(browser.session, BrokerSession):
            return await browser.session.call_tool(name, arguments)
        return await BrowserRuntime(browser.session).execute(_TOOLS[name], arguments, runner=call_tool)
    except ConnectionClosed as exc:
        # The next call restarts Chrome from the same profile; without this the
        # session stays "ready" on a dead connection forever.
        browser.session.mark_disconnected()
        blocked_origin = getattr(browser.session, "consume_site_blocked_origin", lambda: "")()
        if blocked_origin:
            _record_origin(browser.blocked_origins, blocked_origin)
            err = Blocked(
                f"site_permission_required: {blocked_origin} "
                "(open the iCode Browser Bridge extension and allow this site)",
                blocked_origin=blocked_origin,
            )
            raise ToolError(err.render(), code=err.code) from err
        raise ToolError(exc.render(), code=exc.code) from exc
    except BrokerCallUnknown as exc:
        return [{"type": "text", "text": str(exc)}]
    except BrowserError as exc:
        blocked_origin = getattr(browser.session, "consume_site_blocked_origin", lambda: "")()
        if blocked_origin and "site_permission_required" not in str(exc) and "site_blocked" not in str(exc):
            _record_origin(browser.blocked_origins, blocked_origin)
            err = Blocked(
                f"site_permission_required: {blocked_origin} "
                "(open the iCode Browser Bridge extension and allow this site)",
                blocked_origin=blocked_origin,
            )
            raise ToolError(err.render(), code=err.code) from err
        raise ToolError(exc.render(), code=exc.code) from exc


def _text(blocks: list[dict[str, object]]) -> str:
    return "\n".join(
        str(block.get("text", "")) for block in blocks if block.get("type") == "text" and block.get("text")
    )


def _record_origin(bucket: list[str], url: str) -> None:
    """Append a normalized ``scheme://host:port`` origin uniquely (never credentials/query/path/tokens)."""
    if not url:
        return
    origin = canonical_origin(url)
    if not origin:
        return
    if origin not in bucket and len(bucket) < 32:
        bucket.append(origin)


def _format_session_facts(browser: _Browser) -> str:
    """Render runtime-observed session facts on close for parent-agent cross-checking."""
    _sync_write_actions(browser)
    visited = ", ".join(browser.visited_origins) if browser.visited_origins else "none"
    blocked = ", ".join(browser.blocked_origins) if browser.blocked_origins else "none"
    shots = ", ".join(browser.screenshots[-10:]) if browser.screenshots else "none"
    return (
        "[BROWSER_SESSION_FACTS]\n"
        f"visited_origins: {visited}\n"
        f"blocked_origins: {blocked}\n"
        f"write_actions: {browser.write_actions}\n"
        f"screenshots: {shots}"
    )


async def _run(browser: _Browser, name: str, arguments: dict[str, object]) -> str:
    """Run one core tool and return its text blocks joined for the model."""
    _cancel_unstarted_idle_reclaim(browser)
    is_write = name in _WRITE_TOOL_NAMES
    try:
        await _ensure_handle_active(browser)
        blocks = await _call(browser, name, arguments)
    except ToolError as exc:
        cause = exc.__cause__
        pre_dispatch = exc.code in _PRE_DISPATCH_ERROR_CODES or (isinstance(cause, BrowserError) and cause.pre_dispatch)
        if is_write and not _sync_write_actions(browser) and not pre_dispatch:
            browser.write_actions += 1
        if (
            exc.code in {"browser_blocked", "browser_policy_denied"}
            or "site_blocked" in exc.message
            or "site_permission_required" in exc.message
        ):
            blocked_origin = cause.blocked_origin if isinstance(cause, BrowserError) else ""
            if blocked_origin:
                _record_origin(browser.blocked_origins, blocked_origin)
            else:
                raw_url = str(arguments.get("url") or "")
                target_url = (
                    urljoin(browser.url, raw_url) if raw_url and browser.url else (raw_url or browser.url or "")
                )
                _record_origin(browser.blocked_origins, target_url)
        raise
    else:
        rendered = _text(blocks)
        if is_write and not _sync_write_actions(browser) and _was_dispatched_write(name, rendered):
            browser.write_actions += 1
    finally:
        for origin in getattr(browser.session, "drain_blocked_origins", lambda: ())():
            _record_origin(browser.blocked_origins, origin)
        # A block that arrived during this call is attributed (above) or already
        # drained; it must not relabel a later, unrelated failure.
        getattr(browser.session, "consume_site_blocked_origin", lambda: "")()
        if (
            not browser.superseded
            and browser.slot_state_dir is not None
            and not browser.slot_generation_stamped
            and not browser.record_pending
            and browser.record is not None
            and bool(getattr(browser.session, "owns_state_dir", False))
            and browser.slot_state_dir.is_dir()
            and _check_slot_ownership(browser) is None
        ):
            _write_slot_generation(browser.slot_state_dir, browser.record.resume_id)
            browser.slot_generation_stamped = True
        if not browser.superseded:
            snapshot = browser.session.snapshot
            browser.url = snapshot.url or browser.url
            browser.title = snapshot.title or browser.title
            _record_origin(browser.visited_origins, browser.url)
            await _apersist(browser)
        _maybe_rearm_unstarted_idle_reclaim(browser)
    return rendered


@_tool(requires_approval=True)
async def browser_open(ctx: ToolContext) -> str:
    """Allocate a browser handle and return its browser_id; launch/connect is deferred until a page tool needs it."""
    base = _config()
    slot = _next_free_slot(ctx)
    slot_key = _ctx_slot_key(ctx, slot)
    _allocating_slots.add(slot_key)
    try:
        if base.backend == "owned":
            state_dir = base.state_dir / "agents" / _owner_key(ctx) / f"slot-{slot}"
            slot_lock = await acquire_file_lock(_slot_lifecycle_lock_path(state_dir), wait=5.0)
            if slot_lock is None:
                raise ToolError(
                    "That profile is busy with another plugin instance; retry after it finishes.",
                    code="browser_open_blocked",
                )
            try:
                if state_dir.exists():
                    try:
                        probe = await acquire_instance_lock(state_dir, wait=0)
                    except Unavailable as exc:
                        raise ToolError(
                            f"That profile is in use by another plugin instance ({exc}); close it before opening.",
                            code="browser_open_blocked",
                        ) from exc
                    try:
                        browser = _allocate_via_open(ctx, base, slot)
                        if browser.record is not None:
                            if await _asave_record(browser.manifest, browser.record):
                                browser.manifest_committed = True
                            _write_slot_generation(state_dir, browser.record.resume_id)
                            browser.slot_generation_stamped = True
                    finally:
                        probe.release()
                else:
                    browser = _allocate_via_open(ctx, base, slot)
                    if browser.record is not None and await _asave_record(browser.manifest, browser.record):
                        browser.manifest_committed = True
            finally:
                slot_lock.release()
        else:
            browser = _allocate_via_open(ctx, base, slot)
            if browser.record is not None and await _asave_record(browser.manifest, browser.record):
                browser.manifest_committed = True
    finally:
        _allocating_slots.discard(slot_key)
    if _is_reclaimable_on_idle(ctx):
        _arm_unstarted_idle_reclaim(browser, browser.session.cfg.idle_close_seconds)
    return f"browser_id: {browser.id}"


def _describe(browser: _Browser) -> tuple[str, str, str]:
    """Map the transport state to ``(state, reason, next step)`` for the model."""
    if browser.superseded:
        return (
            "superseded",
            "another instance resumed or took over this slot",
            "call browser_close on this stale handle, then call browser_list",
        )
    session = browser.session
    if isinstance(session, BrokerSession):
        if session.connected:
            return (
                "ready",
                "",
                "call browser_snapshot to confirm the Broker page, or browser_navigate to load a new one",
            )
        return (
            "disconnected",
            "the Broker connection is not open",
            "call any page tool to reacquire the Broker lease, or browser_resume to reopen this slot",
        )
    state = session.state
    if state == "idle":
        if not session.started_once:
            return "allocated", "nothing has launched yet", "call browser_navigate to start Chrome from this profile"
        return (
            "idle",
            "closed after the idle timeout",
            "browser_navigate restarts Chrome from the same profile; the old page is gone (a new page)",
        )
    if state == "starting":
        return "connecting", "Chrome is starting", "wait, then call browser_status again"
    if state == "ready":
        if session.login_pending:
            return (
                "ready",
                "login pending",
                "ask the user to sign in in the visible window, then call browser_snapshot at least 120s apart "
                "to keep the wait alive",
            )
        return "ready", "", "call browser_snapshot or browser_navigate"
    if state == "hung":
        return "hung", "the page stopped responding", "call browser_navigate to recycle the tab"
    if state == "failed":
        return (
            "disconnected",
            "the CDP connection dropped",
            "browser_navigate restarts Chrome from the same profile; the previous page is gone (a new page)",
        )
    if state == "closing":
        return "closing", "the browser is shutting down", "wait, then call browser_list"
    return "closed", "the handle was closed", "call browser_list or browser_open"


@_tool(requires_approval=False)
async def browser_list(ctx: ToolContext) -> str:
    """List this session's browsers plus recorded handles that can be reopened; never launches Chrome."""
    mine = _mine(ctx)
    path = _manifest_path(ctx)
    records: list[registry.ManifestRecord] = []
    problem: str | None = None
    if path is not None:
        records, problem = registry.load(path)
    owner_key = _owner_key(ctx)
    own = [
        record
        for record in records
        if record.owner_key == owner_key and record.session_id == ctx.session_id and record.agent_name == ctx.agent_name
    ]
    live = {browser.record.resume_id for browser in mine if browser.record is not None}
    recoverable = [record for record in own if record.resume_id not in live]
    unclaimable = sum(1 for record in records if record.session_id != ctx.session_id)

    lines = [f"open={len(mine)} recoverable={len(recoverable)} manifest={problem or 'ok'}"]
    for browser in mine:
        state, reason, next_step = _describe(browser)
        line = (
            f"- browser_id={browser.id} state={state} backend={browser.backend or 'unknown'} "
            f"slot={browser.slot} origin={_browser_origin(browser)}"
        )
        if browser.record is not None and browser.record.resume_id:
            line += f" resume_id={browser.record.resume_id}"
        lines.append(line)
        owned_pages: list[Any] = []
        if isinstance(browser.session, BrokerSession):
            if browser.session.connected:
                with contextlib.suppress(Exception):
                    listed = await browser.session.list_pages()
                    if isinstance(listed, list):
                        owned_pages = listed
        elif browser.session.state == "ready":
            pages_fn = getattr(browser.session, "pages", None)
            owned_pages = list(pages_fn()) if callable(pages_fn) else []
            action_lock = getattr(browser.session, "_action_lock", None)
            busy = bool(callable(getattr(action_lock, "locked", None)) and action_lock.locked())
            if len(owned_pages) > 1 and not busy:
                list_pages_fn = getattr(browser.session, "list_pages", None)
                if callable(list_pages_fn):
                    with contextlib.suppress(Exception):
                        refreshed = await asyncio.wait_for(list_pages_fn(), timeout=1.0)
                        if isinstance(refreshed, list) and refreshed:
                            owned_pages = refreshed
        if len(owned_pages) > 1:
            tab_parts = []
            for info in owned_pages:
                mark = "*" if getattr(info, "current", False) else ""
                tid = str(getattr(info, "target_id", "") or "")
                url = str(getattr(info, "url", "") or "about:blank")
                tab_parts.append(f"#{getattr(info, 'index', '?')}{mark} tab_id={tid} url={url}")
            lines.append("  tabs: " + " | ".join(tab_parts))
        if reason:
            lines.append(f"  reason: {reason}")
        lines.append(f"  next: {next_step}")
    for record in recoverable:
        lines.append(
            f"- resume_id={record.resume_id} state=recorded backend={record.backend} "
            f"slot={record.slot} origin={record.origin}"
        )
        if _active_toolset() == "compact":
            lines.append(
                "  next: call browser_navigate(url=...) or browser_open to start a new handle "
                "(browser_resume is unavailable in compact toolset)"
            )
        else:
            lines.append("  next: call browser_resume to reopen this handle (it does not navigate)")
    if unclaimable:
        lines.append(f"unclaimable={unclaimable} (records copied from another session; history only)")
    if not mine and not recoverable:
        lines.append("next: call browser_open to allocate a browser handle")
    return "\n".join(lines)


@_tool(requires_approval=False)
async def browser_status(
    ctx: ToolContext,
    browser_id: Annotated[str, "Browser id returned by browser_open"],
) -> str:
    """Report one browser's connection state and next step; never starts or reconnects."""
    browser = _get(ctx, browser_id)
    state, reason, next_step = _describe(browser)
    parts = [
        f"browser_id={browser.id}",
        f"state={state}",
        f"backend={browser.backend or 'unknown'}",
        f"slot={browser.slot}",
        f"origin={_browser_origin(browser)}",
    ]
    session = browser.session
    if isinstance(session, BrokerSession):
        parts.append("page_epoch=n/a")
        parts.append("login_pending=n/a")
    elif session.started_once:
        parts.append(f"page_epoch={session.page_epoch}")
        parts.append(f"login_pending={'yes' if session.login_pending else 'no'}")
        current_page = next((p for p in session.pages() if p.current), None)
        if current_page is not None and current_page.active is not None:
            parts.append(f"foreground={'yes' if current_page.active else 'no'}")
    elif getattr(session, "_instance_lock", None) is None:
        cfg = getattr(session, "cfg", None)
        state_dir = getattr(cfg, "state_dir", None)
        if state_dir is not None:
            lock_probe = probe_instance_lock(state_dir)
            if lock_probe.locked:
                parts.append(f"lock={lock_probe.state}")
                if lock_probe.holder_pid is not None:
                    parts.append(f"holder_pid={lock_probe.holder_pid}")
                reason = f"state directory is held by another instance ({lock_probe.state})"
                next_step = (
                    "do not loop browser_navigate/sleep or inspect bridge.json/cookies; "
                    "report the holder PID to the user or use a separate ICODE_BROWSER_STATE_DIR / Broker"
                )
    lines = [" ".join(parts)]
    if reason:
        lines.append(f"reason: {reason}")
    lines.append(f"next: {next_step}")
    return "\n".join(lines)


@_tool(requires_approval=True)
async def browser_resume(
    ctx: ToolContext,
    resume_id: Annotated[str, "resume_id from browser_list for a recorded, not-live browser"],
) -> str:
    """Reopen a recorded browser by resume_id; never navigates or replays an action."""
    base = _config()
    path = _manifest_path(ctx)
    records, _problem = registry.load(path) if path is not None else ([], None)
    record = next((item for item in records if item.resume_id == resume_id), None)
    if record is None:
        raise ToolError(
            f"Unknown resume_id {resume_id!r}; call browser_list to see this session's recorded browsers.",
            code="unknown_resume",
        )
    if (
        record.owner_key != _owner_key(ctx)
        or record.session_id != ctx.session_id
        or record.agent_name != ctx.agent_name
    ):
        raise ToolError(
            "That record belongs to another session or agent; a copied/forked session cannot claim it. "
            "Call browser_open to start a browser for this session.",
            code="browser_resume_denied",
        )
    for browser in _mine(ctx):
        if browser.record is not None and browser.record.resume_id == resume_id:
            return f"browser_id: {browser.id}\nresume_id {resume_id} is already live; use its browser_id."
    backend = base.backend
    if record.backend != backend:
        raise ToolError(
            f"That browser was opened in {record.backend} mode but this plugin is configured for {backend}; "
            "restore the original configuration or call browser_open instead.",
            code="browser_resume_backend_mismatch",
        )
    slot = record.slot
    if (
        any(browser.slot == slot for browser in _mine(ctx))
        or _ctx_slot_key(ctx, slot) in _reclaiming_slots
        or _ctx_slot_key(ctx, slot) in _allocating_slots
    ):
        raise ToolError(
            f"Slot {slot} already has a live browser; use its browser_id, close it first, or call browser_open.",
            code="browser_resume_conflict",
        )
    if backend == "owned":
        state_dir = base.state_dir / "agents" / _owner_key(ctx) / f"slot-{slot}"
        slot_lock = await acquire_file_lock(_slot_lifecycle_lock_path(state_dir), wait=5.0)
        if slot_lock is None:
            raise ToolError(
                "That profile is busy with another plugin instance; retry after it finishes.",
                code="browser_resume_blocked",
            )
        try:
            if path is not None:
                fresh_records, _ = registry.load(path)
                if not any(item.resume_id == resume_id for item in fresh_records):
                    raise ToolError(
                        f"Unknown resume_id {resume_id!r}; call browser_list to see this session's recorded browsers.",
                        code="unknown_resume",
                    )
            try:
                probe = await acquire_instance_lock(state_dir, wait=0)
            except Unavailable as exc:
                raise ToolError(
                    f"That profile is in use by another plugin instance ({exc}); close it before resuming.",
                    code="browser_resume_blocked",
                ) from exc
            try:
                browser = _allocate(ctx, base, slot, persist=False)
                if browser.record is not None:
                    if await _asave_record(browser.manifest, browser.record):
                        browser.manifest_committed = True
                    _write_slot_generation(state_dir, browser.record.resume_id)
                    browser.slot_generation_stamped = True
            finally:
                probe.release()
        finally:
            slot_lock.release()
    else:
        browser = _allocate(ctx, base, slot, persist=False)
        if browser.record is not None and await _asave_record(browser.manifest, browser.record):
            browser.manifest_committed = True
    if backend == "broker":
        note = (
            "reacquired the Broker session for this slot; the Broker may still hold the original page. "
            "Call browser_snapshot to check its state (no navigation was performed)."
        )
    elif backend == "owned":
        note = (
            "reopened this slot from the same Chrome profile; this is a new page — the previous DOM and "
            "history are not restored and no URL was loaded again."
        )
    else:
        note = (
            f"allocated a new {backend}-mode handle for this slot; no page was opened or navigated. "
            "Call browser_navigate to load a URL."
        )
    return f"browser_id: {browser.id}\nresume_id {resume_id} {note}"


@_tool(requires_approval=True)
async def browser_navigate(
    ctx: ToolContext,
    url: Annotated[
        str,
        "Absolute HTTP(S) URL or relative path against the current HTTP(S) page. "
        "Public sites work by default. ICODE_BROWSER_ALLOWED_DOMAINS and ICODE_BROWSER_ALLOWED_ORIGINS "
        "restrict destinations when configured. Local origins require explicit access.",
    ],
    browser_id: Annotated[
        str,
        "Existing browser id from browser_open; omit to open a new browser and navigate in one step",
    ] = "",
) -> str:
    """Navigate an existing browser, or allocate a new one and navigate in a single step, returning its fresh snapshot."""
    allocated = False
    if browser_id.strip():
        browser = _get(ctx, browser_id)
    else:
        mine = _mine(ctx)
        if mine:
            raise ToolError(
                "A browser is already open for this session/agent; call browser_list to see open browser_id(s), "
                "pass browser_id to reuse it, or call browser_open to allocate a separate handle.",
                code="browser_id_required",
            )
        global _DEFER_MANIFEST_PERSIST
        _DEFER_MANIFEST_PERSIST = True
        try:
            browser = _open(ctx)
        finally:
            _DEFER_MANIFEST_PERSIST = False
        # Commit the record only once the browser has started: writing it now would
        # supersede this slot's previous generation even if the start then fails.
        browser.record_pending = browser.record is not None
        allocated = True
    try:
        text = await _run(browser, "browser_navigate", {"url": url})
    except ToolError as exc:
        if exc.code == "browser_login_required":
            await _commit_pending_record(browser)
            if not browser_id.strip():
                raise ToolError(f"browser_id: {browser.id}\n{exc.message}", code=exc.code) from exc
            raise
        if allocated:
            with contextlib.suppress(Exception):
                await _ensure_close_task(browser)
        raise
    except BaseException:
        if allocated:
            with contextlib.suppress(Exception):
                await _ensure_close_task(browser)
        raise
    await _commit_pending_record(browser)
    return f"browser_id: {browser.id}\n{text}"


async def _commit_pending_record(browser: _Browser) -> None:
    """Write an implicit navigate's record once its browser is up (see ``record_pending``)."""
    if not browser.record_pending:
        return
    browser.record_pending = False
    if browser.superseded or browser.record is None:
        return
    browser.record = dataclasses.replace(browser.record, origin=_browser_origin(browser), updated_at=time.time())
    slot_dir = browser.slot_state_dir
    if slot_dir is not None and bool(getattr(browser.session, "owns_state_dir", False)):
        slot_lock = await acquire_file_lock(_slot_lifecycle_lock_path(slot_dir), wait=5.0)
        try:
            if await _asave_record(browser.manifest, browser.record):
                browser.manifest_committed = True
            if slot_dir.is_dir():
                _write_slot_generation(slot_dir, browser.record.resume_id)
                browser.slot_generation_stamped = True
        finally:
            if slot_lock is not None:
                slot_lock.release()
    else:
        if await _asave_record(browser.manifest, browser.record):
            browser.manifest_committed = True


@_tool(requires_approval=False)
async def browser_tabs(
    ctx: ToolContext,
    browser_id: Annotated[str, "Browser id returned by browser_open"],
) -> str:
    """List the tabs this handle owns (root tab plus popups) with tab_id and current marker."""
    browser = _get(ctx, browser_id)
    return await _run(browser, "browser_tabs", {})


@_tool(requires_approval=False)
async def browser_tab_switch(
    ctx: ToolContext,
    browser_id: Annotated[str, "Browser id returned by browser_open"],
    tab_id: Annotated[str, "A tab_id listed by browser_tabs"],
) -> str:
    """Make another owned tab current; refs from the old tab become stale and a snapshot returns."""
    browser = _get(ctx, browser_id)
    return await _run(browser, "browser_tab_switch", {"tab_id": tab_id})


@_tool(requires_approval=True)
async def browser_tab_close(
    ctx: ToolContext,
    browser_id: Annotated[str, "Browser id returned by browser_open"],
    tab_id: Annotated[str, "A tab_id listed by browser_tabs"],
) -> str:
    """Close one owned tab; closing the current tab makes its opener current. The last tab stays open."""
    browser = _get(ctx, browser_id)
    return await _run(browser, "browser_tab_close", {"tab_id": tab_id})


@_tool(requires_approval=False)
async def browser_find(
    ctx: ToolContext,
    browser_id: Annotated[str, "Browser id returned by browser_open"],
    query: Annotated[str, "Text, name, tag, role or href substring to find in the current snapshot"],
    role: Annotated[str, "Optional exact ARIA role"] = "",
    limit: Annotated[int, "Maximum matches"] = 20,
    fresh: Annotated[bool, "Refresh first to verify asynchronous changes against the live page"] = False,
) -> str:
    """Find elements by text or href and return full @eN#epoch refs.

    Use role to narrow a broad query (e.g. link, article, button). The first find
    after a page action refreshes automatically; use fresh=true to check a later update.
    """
    browser = _get(ctx, browser_id)
    return await _run(browser, "browser_find", {"query": query, "role": role, "limit": limit, "fresh": fresh})


@_tool(requires_approval=False)
async def browser_snapshot(
    ctx: ToolContext,
    browser_id: Annotated[str, "Browser id returned by browser_open"],
    scope: Annotated[Literal["viewport", "full"], "viewport or full"] = "viewport",
    interactive_only: Annotated[bool, "Keep only interactive elements"] = False,
    target: Annotated[str, "Serialize only this element's subtree (an @eN ref or free text)"] = "",
    since: Annotated[int, "Return only changes since this snapshot version; 0 returns full"] = 0,
) -> str:
    """Return the current page as a compact tree of interactive elements with @eN refs.

    Unnamed non-interactive nodes are not printed (their refs still exist); consecutive
    same-name siblings under one parent collapse to one line with "×N". target=<ref|text>
    serializes only that element's subtree, which is how you read a dense region.
    """
    browser = _get(ctx, browser_id)
    return await _run(
        browser,
        "browser_snapshot",
        {"scope": scope, "interactive_only": interactive_only, "target": target, "since": since},
    )


@_tool(requires_approval=True)
async def browser_click(
    ctx: ToolContext,
    browser_id: Annotated[str, "Browser id returned by browser_open"],
    target: Annotated[str, "An @eN#epoch ref from the latest snapshot, or free text to resolve"],
    button: Annotated[Literal["left", "middle", "right"], "Mouse button"] = "left",
    click_count: Annotated[int, "Click count from 1 to 3"] = 1,
    dispatch: Annotated[
        Literal["mouse", "dom"],
        "mouse (default) sends trusted mouse events; dom calls the element's own click() and is "
        "allowed once per ref, only for a button/link/input (or role=button) whose newest mouse click on this same "
        "ref was reported all-occluded (status=blocked, nothing sent) or, for a non-submit target, "
        "effect=none",
    ] = "mouse",
    offset_x: Annotated[
        float | None,
        "Optional X offset in CSS pixels from target's top-left corner (must be paired with offset_y; e.g. for <canvas>/<svg>)",
    ] = None,
    offset_y: Annotated[
        float | None,
        "Optional Y offset in CSS pixels from target's top-left corner (must be paired with offset_x; e.g. for <canvas>/<svg>)",
    ] = None,
    expect_condition: Annotated[
        Literal["", "visible", "hidden", "editable", "text", "url", "text_changed", "stable", "count"],
        "Optional bounded read-only condition to verify immediately after this single click (never retries the click). "
        "visible/hidden/editable/count require expect_target; text/url/count require expect_value; url forbids expect_target",
    ] = "",
    expect_target: Annotated[
        str,
        "Target (@eN#epoch ref or free text) for expect_condition: REQUIRED when expect_condition is visible, hidden, "
        "editable, or count (free-text query only for count); optional for text/text_changed/stable; MUST be empty for url",
    ] = "",
    expect_value: Annotated[
        str,
        "Value for expect_condition: REQUIRED when expect_condition is text, url, or count (e.g. '>3', '=2'); "
        "optional quiet window ms (100-5000, default 1500) for stable",
    ] = "",
    expect_timeout_ms: Annotated[int, "Read-only expect wait budget in ms (100-60000)"] = 5000,
    expect_return: Annotated[
        Literal["none", "ref", "snapshot", "text"],
        "Optional payload to return when expect_condition succeeds",
    ] = "none",
) -> str:
    """Click an element by ref or text; a stale ref fails and asks for a new snapshot.

    The mouse click targets the element's centre and, when that is covered, points near its
    corners (result shows hit_point=corner); pass offset_x and offset_y (CSS pixels from the
    target's top-left corner) to click a specific sub-region such as inside a <canvas> or <svg>.
    When every point is covered nothing is sent (status=blocked), and when a hit test cannot be
    answered nothing is sent either (status=uncertain). The result reports
    effect=navigation|new_tab|dom|dom_elsewhere|focus|none|unknown;
    verified=control_state means the same control changed its accessibility state or label.
    There is never an automatic JS second click. Pass expect_condition (and optionally
    expect_return='ref'|'snapshot'|'text') to run a single read-only verification right after
    the click (taking text_changed baseline before dispatch) instead of extra find/snapshot calls.
    On effect=none or status=uncertain do not retry the action: verify the expected state with
    browser_find(fresh=true) or a scoped snapshot, and for a text field use browser_fill with
    submit=true. dispatch=dom is enforced to one try per ref in a document.
    """
    browser = _get(ctx, browser_id)
    arguments: dict[str, object] = {
        "target": target,
        "button": button,
        "click_count": click_count,
        "dispatch": dispatch,
    }
    if offset_x is not None:
        arguments["offset_x"] = offset_x
    if offset_y is not None:
        arguments["offset_y"] = offset_y
    if expect_condition:
        arguments.update(
            {
                "expect_condition": expect_condition,
                "expect_target": expect_target,
                "expect_value": expect_value,
                "expect_timeout_ms": expect_timeout_ms,
                "expect_return": expect_return,
            }
        )
    return await _run(browser, "browser_click", arguments)


@_tool(requires_approval=True)
async def browser_drag(
    ctx: ToolContext,
    browser_id: Annotated[str, "Browser id returned by browser_open"],
    source: Annotated[str, "Source element @eN#epoch ref or free text to drag"],
    target: Annotated[str, "Destination element @eN#epoch ref or free text to drop onto"],
    steps: Annotated[int, "Intermediate pointer movement steps between source and target (1-30)"] = 8,
    expect_condition: Annotated[
        Literal["", "visible", "hidden", "editable", "text", "url", "text_changed", "stable", "count"],
        "Optional bounded read-only condition to verify immediately after this single drag (never retries)",
    ] = "",
    expect_target: Annotated[str, "Target (@eN#epoch ref or free text) for expect_condition"] = "",
    expect_value: Annotated[str, "Value for expect_condition"] = "",
    expect_timeout_ms: Annotated[int, "Read-only expect wait budget in ms (100-60000)"] = 5000,
    expect_return: Annotated[
        Literal["none", "ref", "snapshot", "text"],
        "Optional payload to return when expect_condition succeeds",
    ] = "none",
) -> str:
    """Drag source element onto target element via trusted CDP mouse and HTML5 drag events.

    Supports both native HTML5 draggable="true" boards and pointer-event sortable lists/kanban cards.
    Recorded in the action journal and never auto-retried.
    """
    browser = _get(ctx, browser_id)
    arguments: dict[str, object] = {"source": source, "target": target, "steps": steps}
    if expect_condition:
        arguments.update(
            {
                "expect_condition": expect_condition,
                "expect_target": expect_target,
                "expect_value": expect_value,
                "expect_timeout_ms": expect_timeout_ms,
                "expect_return": expect_return,
            }
        )
    return await _run(browser, "browser_drag", arguments)


@_tool(requires_approval=False)
async def browser_hover(
    ctx: ToolContext,
    browser_id: Annotated[str, "Browser id returned by browser_open"],
    target: Annotated[str, "An @eN#epoch ref from the latest snapshot, or free text to resolve"],
) -> str:
    """Move the mouse over an element without clicking; then snapshot to see hover controls."""
    browser = _get(ctx, browser_id)
    return await _run(browser, "browser_hover", {"target": target})


@_tool(requires_approval=True)
async def browser_fill(
    ctx: ToolContext,
    browser_id: Annotated[str, "Browser id returned by browser_open"],
    target: Annotated[str, "An @eN#epoch ref from the latest snapshot, or free text to resolve"],
    text: Annotated[str, "Text to type into the field"],
    clear: Annotated[bool, "Clear the field first"] = True,
    submit: Annotated[bool, "Press Enter after typing"] = False,
    keypress_fallback: Annotated[bool, "Type character by character if insertText fails"] = False,
    expect_condition: Annotated[
        Literal["", "visible", "hidden", "editable", "text", "url", "text_changed", "stable", "count"],
        "Optional bounded read-only condition to verify immediately after this single fill/submit (never retries). "
        "visible/hidden/editable/count require expect_target; text/url/count require expect_value; url forbids expect_target",
    ] = "",
    expect_target: Annotated[
        str,
        "Target (@eN#epoch ref or free text) for expect_condition: REQUIRED when expect_condition is visible, hidden, "
        "editable, or count (free-text query only for count); optional for text/text_changed/stable; MUST be empty for url",
    ] = "",
    expect_value: Annotated[
        str,
        "Value for expect_condition: REQUIRED when expect_condition is text, url, or count (e.g. '>3', '=2'); "
        "optional quiet window ms (100-5000, default 1500) for stable",
    ] = "",
    expect_timeout_ms: Annotated[int, "Read-only expect wait budget in ms (100-60000)"] = 5000,
    expect_return: Annotated[
        Literal["none", "ref", "snapshot", "text"],
        "Optional payload to return when expect_condition succeeds",
    ] = "none",
) -> str:
    """Type into a field with real input events; returns what the page reports.

    submit=true sends a real Enter and reports submitted only when the editor cleared or the
    page navigated; a page that merely updated is reported as unconfirmed. For chat-style SPAs
    (composer + send button) prefer submit=true first, over a separate browser_press_key or a
    click on the send button. Pass expect_condition (and optionally expect_return='text'|'ref'|'snapshot')
    to verify the post-submit result in the same call with a pre-write baseline.
    """
    browser = _get(ctx, browser_id)
    arguments: dict[str, object] = {
        "target": target,
        "text": text,
        "clear": clear,
        "submit": submit,
        "keypress_fallback": keypress_fallback,
    }
    if expect_condition:
        arguments.update(
            {
                "expect_condition": expect_condition,
                "expect_target": expect_target,
                "expect_value": expect_value,
                "expect_timeout_ms": expect_timeout_ms,
                "expect_return": expect_return,
            }
        )
    return await _run(browser, "browser_fill", arguments)


@_tool(requires_approval=True)
async def browser_press_key(
    ctx: ToolContext,
    browser_id: Annotated[str, "Browser id returned by browser_open"],
    key: Annotated[str, "Enter, Tab, Escape, ArrowDown, Ctrl+A, Shift+Tab, or a single character"],
    target: Annotated[str, "Optional @eN#epoch ref, or free text, to focus before pressing"] = "",
) -> str:
    """Focus target (when given), press one key through trusted input events, and report the effect.

    A plain Enter with target set to a single-line text field (contenteditable or a text-like input, not a
    textarea) is judged like browser_fill(submit=true): status=succeeded only when the field emptied or the
    page navigated. Shift+Enter only inserts a line break and a textarea Enter is a newline. Never press Enter
    again on effect=none or status=uncertain; snapshot and look for the submit button instead.
    """
    browser = _get(ctx, browser_id)
    arguments: dict[str, object] = {"key": key, "target": target}
    return await _run(browser, "browser_press_key", arguments)


@_tool(requires_approval=True)
async def browser_select_option(
    ctx: ToolContext,
    browser_id: Annotated[str, "Browser id returned by browser_open"],
    target: Annotated[str, "An @eN#epoch ref from the latest snapshot, or free text to resolve"],
    values: Annotated[list[str], "Option values (or visible labels) to select; one for a single <select>"],
) -> str:
    """Select options on a native <select> and report the values read back from the page."""
    browser = _get(ctx, browser_id)
    return await _run(browser, "browser_select_option", {"target": target, "values": values})


@_tool(requires_approval=False)
async def browser_wait_for(
    ctx: ToolContext,
    browser_id: Annotated[str, "Browser id returned by browser_open"],
    condition: Annotated[
        Literal["visible", "hidden", "editable", "text", "url", "text_changed", "stable", "count"],
        "What to wait for",
    ],
    target: Annotated[
        str,
        "An @eN#epoch ref or free text; REQUIRED for visible/hidden/editable/count (free-text browser_find query for count), "
        "optional for text/text_changed/stable (page-wide when omitted); MUST be empty for url",
    ] = "",
    value: Annotated[
        str,
        "Text to find or the URL substring (text/url); quiet window in ms for stable (default 1500, max 5000); "
        "a comparison such as '>3' or '=2' for count",
    ] = "",
    timeout_ms: Annotated[int, "Wait budget in milliseconds (100-180000)"] = 5000,
    return_mode: Annotated[
        Literal["none", "ref", "snapshot", "text"],
        "Optional content to return immediately when the wait settles (ref, scoped snapshot, or bounded text)",
    ] = "none",
    max_chars: Annotated[int, "Maximum characters for return_mode='text' or 'snapshot' (200-7000)"] = 2000,
) -> str:
    """Wait for a page condition with a hard deadline; optionally return ref, snapshot, or bounded text in one call.

    text_changed succeeds on the first difference from the text seen when the wait started and
    returns the new text; stable succeeds once no DOM mutation happened for value ms (not network
    idle; marks completion=inferred_stable); count compares the number of browser_find matches.
    Set return_mode='text'|'ref'|'snapshot' to avoid follow-up find/extract loops; on timeout with
    return_mode='text', partial observed text and character progress are included.
    """
    browser = _get(ctx, browser_id)
    arguments: dict[str, object] = {
        "condition": condition,
        "target": target,
        "value": value,
        "timeout_ms": timeout_ms,
    }
    if return_mode != "none":
        arguments["return_mode"] = return_mode
    if max_chars != 2000:
        arguments["max_chars"] = max_chars
    return await _run(browser, "browser_wait_for", arguments)


@_tool(requires_approval=True)
async def browser_dialog(
    ctx: ToolContext,
    browser_id: Annotated[str, "Browser id returned by browser_open"],
    action: Annotated[Literal["accept", "dismiss"], "accept or dismiss the open dialog"],
    text: Annotated[str, "Answer text for a prompt (required to accept one); ignored by alert/confirm"] = "",
) -> str:
    """Accept or dismiss the open JavaScript dialog; reports its type, message and why it closed."""
    browser = _get(ctx, browser_id)
    arguments: dict[str, object] = {"action": action}
    if text:
        arguments["text"] = text
    return await _run(browser, "browser_dialog", arguments)


@_tool(requires_approval=False)
async def browser_downloads(
    ctx: ToolContext,
    browser_id: Annotated[str, "Browser id returned by browser_open"],
) -> str:
    """List this handle's downloads with state, bytes, safe name and absolute path."""
    browser = _get(ctx, browser_id)
    return await _run(browser, "browser_downloads", {})


@_tool(requires_approval=True)
async def browser_download_cancel(
    ctx: ToolContext,
    browser_id: Annotated[str, "Browser id returned by browser_open"],
    guid: Annotated[str, "guid from browser_downloads"],
) -> str:
    """Cancel one in-progress download; partial files are removed, nothing is retried."""
    browser = _get(ctx, browser_id)
    return await _run(browser, "browser_download_cancel", {"guid": guid})


@_tool(requires_approval=False)
async def browser_scroll(
    ctx: ToolContext,
    browser_id: Annotated[str, "Browser id returned by browser_open"],
    direction: Annotated[Literal["up", "down", "left", "right"], "Scroll direction"] = "down",
    amount: Annotated[int, "Number of viewports to move (1-10)"] = 3,
    target: Annotated[str | None, "Scroll this element's own container"] = None,
    to_text: Annotated[str | None, "Bring this text into view"] = None,
) -> str:
    """Scroll the window or an element's container; returns the new position."""
    browser = _get(ctx, browser_id)
    arguments: dict[str, object] = {"direction": direction, "amount": amount}
    if target:
        arguments["target"] = target
    if to_text:
        arguments["to_text"] = to_text
    return await _run(browser, "browser_scroll", arguments)


@_tool(requires_approval=False)
async def browser_extract(
    ctx: ToolContext,
    browser_id: Annotated[str, "Browser id returned by browser_open"],
    query: Annotated[
        str | None,
        "Optional @eN#epoch ref, #element-id, or visible text to extract; omit for the whole page (complex CSS selectors not supported)",
    ] = None,
    format: Annotated[Literal["text", "markdown", "html"], "Output format"] = "text",
    max_chars: Annotated[int, "Maximum characters (200-7000)"] = 7000,
    offset: Annotated[int, "Start at this character; use the 'next offset' of a cut result"] = 0,
) -> str:
    """Extract text, markdown or HTML from the live DOM; long content pages by offset."""
    browser = _get(ctx, browser_id)
    arguments: dict[str, object] = {"format": format, "max_chars": max_chars}
    if offset:
        # Only paged reads carry it: a Broker started by an older plugin still serves
        # the first page (its validator rejects keys it does not know).
        arguments["offset"] = offset
    if query:
        arguments["query"] = query
    return await _run(browser, "browser_extract", arguments)


@_tool(requires_approval=False)
async def browser_logs(
    ctx: ToolContext,
    browser_id: Annotated[str, "Browser id returned by browser_open"],
    kind: Annotated[Literal["all", "console", "network"], "Which log to read"] = "all",
    errors_only: Annotated[bool, "Drop info lines; keep errors and warnings"] = False,
    since: Annotated[int, "Only entries after this #seq (the cursor of a previous result)"] = 0,
    limit: Annotated[int, "Newest entries to show (1-100)"] = 30,
) -> str:
    """Read the current tab's console messages and network outcomes, oldest first.

    Network lines cover XHR/fetch responses, HTTP >= 400 and failed or blocked requests.
    Pass since=<cursor> from the previous result to see only new entries.
    """
    browser = _get(ctx, browser_id)
    return await _run(
        browser,
        "browser_logs",
        {"kind": kind, "errors_only": errors_only, "since": since, "limit": limit},
    )


@_tool(requires_approval=False)
async def browser_network_body(
    ctx: ToolContext,
    browser_id: Annotated[str, "Browser id returned by browser_open"],
    seq: Annotated[int | None, "Optional #seq of a network entry from browser_logs"] = None,
    url_contains: Annotated[
        str, "Optional case-insensitive URL substring when seq is omitted (picks newest match)"
    ] = "",
    include_headers: Annotated[bool, "Include scrubbed request and response headers"] = True,
    include_request_body: Annotated[bool, "Include scrubbed request POST payload when present"] = True,
    max_chars: Annotated[int, "Maximum response body characters (200-7000)"] = 4000,
    offset: Annotated[int, "Start offset for paginating long response bodies"] = 0,
) -> str:
    """Inspect a recorded XHR/fetch or HTTP >= 400 request's headers, POST payload and response body.

    Fetches the response body via CDP Network.getResponseBody on demand. Sensitive headers
    (Authorization, Cookie, Set-Cookie, API keys) and tokens/JWTs are redacted; binary bodies
    are summarized by MIME type and byte length.
    """
    browser = _get(ctx, browser_id)
    arguments: dict[str, object] = {
        "url_contains": url_contains,
        "include_headers": include_headers,
        "include_request_body": include_request_body,
        "max_chars": max_chars,
        "offset": offset,
    }
    if seq is not None:
        arguments["seq"] = seq
    return await _run(browser, "browser_network_body", arguments)


@_tool(requires_approval=True)
async def browser_back(
    ctx: ToolContext,
    browser_id: Annotated[str, "Browser id returned by browser_open"],
) -> str:
    """Go back one entry in the page's navigation history and return a new snapshot."""
    browser = _get(ctx, browser_id)
    return await _run(browser, "browser_back", {})


@_tool(requires_approval=True)
async def browser_forward(
    ctx: ToolContext,
    browser_id: Annotated[str, "Browser id returned by browser_open"],
) -> str:
    """Go forward one entry in the page's navigation history and return a new snapshot."""
    browser = _get(ctx, browser_id)
    return await _run(browser, "browser_forward", {})


@_tool(requires_approval=True)
async def browser_reload(
    ctx: ToolContext,
    browser_id: Annotated[str, "Browser id returned by browser_open"],
    ignore_cache: Annotated[bool, "Bypass the HTTP cache"] = False,
) -> str:
    """Reload the current page and return a new snapshot; a POST result page may resubmit."""
    browser = _get(ctx, browser_id)
    return await _run(browser, "browser_reload", {"ignore_cache": ignore_cache})


@_tool(requires_approval=False)
async def browser_screenshot(
    ctx: ToolContext,
    browser_id: Annotated[str, "Browser id returned by browser_open"],
    full_page: Annotated[bool, "Capture beyond the viewport"] = False,
    target: Annotated[str | None, "Optional @eN ref or text to clip"] = None,
) -> str:
    """Save a screenshot file under the session folder; use view_image to inspect it.

    The file gets a unique name and is written atomically. The result reports the
    format, pixel size, byte size and both the absolute path (for view_image in
    this session) and the session-relative path a copied or forked session can
    resolve. Images above 2.5 MB are re-captured as JPEG (quality 80); a still
    oversized image fails, which full_page captures are the likeliest to hit.
    Older screenshots beyond ICODE_BROWSER_SCREENSHOT_KEEP are removed from the
    plugin's own screenshots folder only.
    """
    browser = _get(ctx, browser_id)
    arguments: dict[str, object] = {"full_page": full_page}
    if target:
        arguments["target"] = target
    _cancel_unstarted_idle_reclaim(browser)
    try:
        await _ensure_handle_active(browser)
        blocks = await _call(browser, "browser_screenshot", arguments)
    finally:
        _maybe_rearm_unstarted_idle_reclaim(browser)
    image = next((block for block in blocks if block.get("type") == "image"), None)
    if image is None:
        return _text(blocks) or "screenshot returned no image"
    data = base64.b64decode(str(image.get("data", "")))
    mime = str(image.get("mimeType") or "image/png")
    try:
        directory = _screenshot_directory(ctx)
        path = _write_screenshot(directory, data, ".jpg" if "jpeg" in mime else ".png")
    except OSError as exc:
        raise ToolError(f"could not save the screenshot: {exc}", code="browser_screenshot_failed") from exc
    _prune_screenshots(directory, browser.session.cfg.screenshot_keep, path)
    size = _image_size(data)
    pixels = f"{size[0]}x{size[1]}, " if size is not None else ""
    relative = path.relative_to(directory.parent).as_posix()
    browser.screenshots.append(relative)
    return (
        f"Screenshot saved to {path} ({mime}, {pixels}{len(data)} bytes)\n"
        f"Session file: {relative}\n"
        "Use view_image with the absolute path to inspect it."
    )


_SLOT_GENERATION_FILE = "slot.generation"


def _slot_lifecycle_lock_path(state_dir: Path) -> Path:
    """Cross-process lock file outside *state_dir* and its owner folder.

    Keeping the lock outside *state_dir* lets Windows rename *state_dir* while
    holding the lock, eliminating any takeover window between generation check
    and directory removal; keeping it outside ``agents/<owner_key>`` also lets
    empty owner folders be ``rmdir()``'d without ever unlinking an active lock.
    """
    return state_dir.parent.parent / ".slot-locks" / f"{state_dir.parent.name}-{state_dir.name}.lock"


def _read_slot_generation(state_dir: Path) -> str:
    try:
        return (state_dir / _SLOT_GENERATION_FILE).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _write_slot_generation(state_dir: Path, resume_id: str) -> None:
    if not resume_id or not state_dir.is_dir():
        return
    with contextlib.suppress(OSError):
        atomic_write_private(state_dir / _SLOT_GENERATION_FILE, f"{resume_id}\n", strict=False)


def _manifest_allows_reclaim(browser: _Browser) -> bool:
    """True only when *browser* owns its slot (active generation, or a freshly created pending dir)."""
    if browser.superseded or browser.record is None or not browser.record.resume_id:
        return False
    if browser.record_pending:
        if not browser.created_fresh_state_dir:
            return False
        if browser.manifest is None:
            return True
        try:
            with _manifest_lock(browser.manifest):
                records, problem = registry.load(browser.manifest)
        except OSError:
            return False
        if problem in {"unreadable", "unexpected shape"}:
            return False
        owner_key = browser.record.owner_key
        slot = browser.record.slot
        same_slot = [item for item in records if item.owner_key == owner_key and item.slot == slot]
        return not same_slot
    if browser.manifest is None:
        return True
    try:
        with _manifest_lock(browser.manifest):
            records, problem = registry.load(browser.manifest)
    except OSError:
        return False
    if problem in {"unreadable", "unexpected shape"}:
        return False
    owner_key = browser.record.owner_key
    slot = browser.record.slot
    same_slot = [item for item in records if item.owner_key == owner_key and item.slot == slot]
    if not same_slot:
        # Fail closed when a manifest exists and no longer names this generation.
        return not browser.manifest.exists()
    return all(item.resume_id == browser.record.resume_id for item in same_slot)


def _stage_ephemeral_dir(
    state_dir: Path, *, browser: _Browser | None = None
) -> tuple[Path | None, bool, InstanceLock | None, bool]:
    """Move *state_dir* aside before releasing its slot, only while owning both lock and generation.

    Returns ``(cleanup_target, slot_can_be_reused, held_lock, record_dropped)``.
    A cross-process lifecycle lock outside *state_dir* serializes generation
    checks and directory staging against concurrent ``browser_resume`` takeovers
    (including the brief Windows ``instance.lock`` release needed for rename).
    """
    if not state_dir.exists():
        return None, True, None, False
    lifecycle_lock = try_acquire_file_lock(_slot_lifecycle_lock_path(state_dir))
    if lifecycle_lock is None:
        logger.warning("ephemeral state dir %s is busy with another slot operation; leaving it in place", state_dir)
        return None, True, None, False
    try:
        if not state_dir.exists():
            return None, True, None, False
        lock = try_acquire_instance_lock(state_dir)
        if lock is None:
            logger.warning("ephemeral state dir %s is held by another instance; leaving it in place", state_dir)
            return None, True, None, False
        if browser is not None:
            expected_id = browser.record.resume_id if browser.record is not None else ""
            stamped = _read_slot_generation(state_dir)
            if (stamped and stamped != expected_id) or not _manifest_allows_reclaim(browser):
                lock.release()
                logger.info(
                    "ephemeral state dir %s belongs to a newer generation (%s != %s); leaving it in place",
                    state_dir,
                    stamped or "manifest",
                    expected_id,
                )
                return None, True, None, False
        staged = state_dir.parent / f".reclaim-{state_dir.name}-{secrets.token_hex(4)}"
        attempts = 2 if os.name == "nt" else 1
        for attempt in range(attempts):
            if attempt:
                # Windows cannot rename a folder that has an open file (our lock) in
                # it: release instance.lock while lifecycle_lock stays held so no
                # cross-host browser_resume can take over between release and rename.
                lock.release()
            try:
                state_dir.rename(staged)
            except FileNotFoundError:
                lock.release()
                return None, True, None, False
            except OSError:
                continue
            lock.release()
            dropped = False
            if browser is not None:
                _drop_record(browser)
                dropped = True
            return staged, True, None, dropped
        if attempts > 1:
            lock = try_acquire_instance_lock(state_dir)
            if lock is None:
                logger.warning("ephemeral state dir %s is held by another instance; leaving it in place", state_dir)
                return None, True, None, False
        return state_dir, False, lock, False
    finally:
        lifecycle_lock.release()


def _cleanup_ephemeral_dir(state_dir: Path) -> None:
    """Remove an ephemeral Owned slot directory and its parent owner folder if empty."""
    shutil.rmtree(state_dir, ignore_errors=True)
    with contextlib.suppress(OSError):
        state_dir.parent.rmdir()


def _ensure_close_task(browser: _Browser) -> asyncio.Task[None]:
    """Return the single teardown task for *browser* so concurrent closes share one cleanup."""
    if browser.close_task is None:
        browser.close_task = asyncio.create_task(_close_and_forget(browser))
    return browser.close_task


async def _close_and_forget(browser: _Browser) -> None:
    """Close the core session, then drop the handle and its recorded identity.

    This runs as its own task so a cancelled turn cannot stop the bookkeeping
    halfway: the record disappears with the browser, not with the caller.
    """
    cleanup_target: Path | None = None
    cleanup_lock: InstanceLock | None = None
    reserved_slot: tuple[str | None, str, str | None, int] | None = None
    record_dropped = False
    idle_task = browser.idle_reclaim_task
    browser.idle_reclaim_task = None
    if idle_task is not None and idle_task is not asyncio.current_task() and not idle_task.done():
        idle_task.cancel()
    try:
        await browser.session.aclose()
    finally:
        if (
            not browser.superseded
            and browser.ephemeral_state_dir is not None
            and bool(getattr(browser.session, "owns_state_dir", False))
        ):
            cleanup_target, slot_free, cleanup_lock, record_dropped = _stage_ephemeral_dir(
                browser.ephemeral_state_dir, browser=browser
            )
            if not slot_free:
                reserved_slot = _slot_key(browser)
                _reclaiming_slots.add(reserved_slot)
        _browsers.pop(browser.id, None)
        try:
            # Off the loop: on Windows the rewrite runs icacls.
            if not record_dropped:
                await asyncio.to_thread(_drop_record, browser)
            if cleanup_target is not None:
                await asyncio.to_thread(_cleanup_ephemeral_dir, cleanup_target)
        finally:
            if cleanup_lock is not None:
                cleanup_lock.release()
            if reserved_slot is not None:
                _reclaiming_slots.discard(reserved_slot)


def _drop_record(browser: _Browser) -> None:
    """Remove the durable record that would otherwise offer a resume for a closed browser."""
    if browser.record is None or not browser.record.resume_id or browser.manifest is None:
        return
    if browser.record_pending:
        # Never written: dropping would only race with the previous generation's record.
        return
    try:
        with _manifest_lock(browser.manifest):
            records, problem = registry.load(browser.manifest)
            if problem in {"unreadable", "unexpected shape"}:
                logger.warning("browser manifest skipped drop (%s): %s", problem, browser.manifest)
                return
            remaining = registry.drop(records, browser.record.resume_id)
            if len(remaining) == len(records):
                return
            registry.save(browser.manifest, remaining)
    except OSError as exc:
        logger.warning("browser manifest update failed: %s", exc)


def _log_late_close_failure(task: asyncio.Task[None]) -> None:
    """Report a shielded close that failed after its caller was cancelled; there is nobody to raise to."""
    if task.cancelled():
        return
    error = task.exception()
    if error is not None:
        logger.warning("browser close finished after cancellation with an error: %s", error)


@_tool(requires_approval=True)
async def browser_close(
    ctx: ToolContext,
    browser_id: Annotated[str, "Browser id to close"],
) -> str:
    """Close a browser and release its Chrome process."""
    browser = _get(ctx, browser_id)
    task = _ensure_close_task(browser)
    try:
        # Shield the teardown: a cancelled turn must neither stop Chrome teardown
        # nor skip removing the handle and its recoverable record.
        await asyncio.shield(task)
    except asyncio.CancelledError:
        task.add_done_callback(_log_late_close_failure)
        raise
    except Exception as exc:
        raise ToolError(f"browser close failed: {exc}", code="browser_close_failed") from exc
    unclosed = getattr(browser.session, "unclosed_targets", ())
    header = f"browser_id {browser.id} closed"
    if unclosed:
        header += f"\nwarning: cleanup incomplete — could not reconnect to close detached tab(s): {', '.join(unclosed)}"
    return f"{header}\n{_format_session_facts(browser)}"


@_tool(requires_approval=True)
async def browser_upload(
    ctx: ToolContext,
    browser_id: Annotated[str, "Browser id returned by browser_open"],
    target: Annotated[str, "File input @eN ref or free text"],
    path: Annotated[str, "Existing file under the workspace"],
) -> str:
    """Attach a local file to a file input; high-risk write action."""
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = Path(ctx.cwd) / candidate
    candidate = candidate.resolve()
    workspaces = tuple(Path(root).resolve() for root in (ctx.workspace_dirs or (ctx.cwd,)))
    if not any(candidate == root or root in candidate.parents for root in workspaces):
        raise ToolError("Upload path is outside the workspace.", code="browser_upload_denied")
    if not candidate.is_file():
        raise ToolError("Upload path is not a regular file.", code="browser_bad_request")
    browser = _get(ctx, browser_id)
    configured_roots = tuple(Path(root).resolve() for root in getattr(browser.session.cfg, "upload_roots", ()))
    if not isinstance(browser.session, BrokerSession):
        if not configured_roots:
            raise ToolError(
                "Uploads are disabled until ICODE_BROWSER_UPLOAD_ROOTS is configured.",
                code="browser_upload_denied",
            )
        if not any(candidate == root or root in candidate.parents for root in configured_roots):
            raise ToolError(
                "Upload path is outside ICODE_BROWSER_UPLOAD_ROOTS.",
                code="browser_upload_denied",
            )
    elif not BrowserEnv().get("UPLOAD_ROOTS"):
        raise ToolError(
            "Broker upload requires ICODE_BROWSER_UPLOAD_ROOTS configured before the Broker starts.",
            code="browser_upload_denied",
        )
    return await _run(browser, "browser_upload", {"target": target, "path": str(candidate)})


@_tool(requires_approval=False)
async def browser_action_status(
    ctx: ToolContext,
    action_id: Annotated[str, "Action id from a previous action result"],
    browser_id: Annotated[str | None, "Browser handle whose journal recorded the action"] = None,
) -> str:
    """Look up a recorded action result in one browser's journal; never retries."""
    if browser_id is None:
        mine = _mine(ctx)
        if not mine:
            raise ToolError(
                "No browser is open, so no journal can be queried; pass the browser_id that recorded the action "
                "or open one with browser_open.",
                code="browser_id_required",
            )
        if len(mine) > 1:
            raise ToolError(
                "Several browsers are open; pass browser_id to choose the action journal.",
                code="browser_id_required",
            )
        browser_id = mine[0].id
    return await _run(_get(ctx, browser_id), "browser_action_status", {"action_id": action_id})


if _env_enabled("ICODE_BROWSER_EVALUATE"):

    @extension.tool(requires_approval=True)
    async def browser_evaluate(
        ctx: ToolContext,
        browser_id: Annotated[str, "Browser id returned by browser_open"],
        expression: Annotated[str, "JavaScript expression evaluated in the page"],
        await_promise: Annotated[bool, "Await a returned Promise"] = False,
    ) -> str:
        """Last resort: run a high-risk page-context script (enabled by ICODE_BROWSER_EVALUATE=allow).

        Prefer browser_find/browser_snapshot to locate, browser_extract to read text,
        browser_fill(submit=true) to send, and browser_wait_for to wait.
        """
        browser = _get(ctx, browser_id)
        text = await _run(
            browser,
            "browser_evaluate",
            {"expression": expression, "await_promise": await_promise},
        )
        return text


if _env_enabled("ICODE_BROWSER_SAVE_STATE"):

    @extension.tool(requires_approval=True)
    async def browser_save_state(
        ctx: ToolContext,
        browser_id: Annotated[str, "Browser id returned by browser_open"],
        name: Annotated[str, "Archive name, ^[a-z0-9_-]{1,32}$"],
    ) -> str:
        """Export cookies/localStorage; enabled by ICODE_BROWSER_SAVE_STATE=allow."""
        browser = _get(ctx, browser_id)
        return await _run(browser, "browser_save_state", {"name": name})


@extension.context
async def browser_context(ctx: TurnContext) -> str | None:
    """Tell the agent which browsers it has open, and the id to pass to browser_* tools."""
    mine = _mine(ctx)
    if mine:
        playbook = COMPACT_BROWSER_PLAYBOOK if _active_toolset() == "compact" else BROWSER_PLAYBOOK
        return (
            f"Open browsers: {len(mine)}. Call browser_list to see open browser_id(s), "
            "and pass browser_id to browser_* tools. " + playbook
        )
    recorded = _recoverable(ctx)
    if recorded and _active_toolset() == "full":
        return (
            f"Open browsers: 0, but {len(recorded)} browser(s) recorded for this session can be reopened. "
            "Call browser_list to see them, then browser_resume."
        )
    return None


@extension.before_tool
async def browser_guard(call: ToolCall) -> None:
    """In strict mode, refuse shell, file writes, and skill scripts while this session has a browser open."""
    if (BrowserEnv().get("GUARD") or "").strip().lower() != "strict":
        return
    is_blocked_tool = call.kind in {"shell", "filesystem.write"} or (
        call.kind == "skill" and call.name == "run_skill_script"
    )
    if not is_blocked_tool:
        return
    session_id = call.context.session_id
    if session_id is not None:
        has_open = any(
            browser.owner_session == session_id for browser in _browsers.values() if browser.close_task is None
        )
    else:
        has_open = bool(_mine(call.context))
    if has_open:
        raise ToolError(
            "A browser is open; shell, file writes, and skill scripts are blocked while ICODE_BROWSER_GUARD=strict.",
            code="browser_guard",
        )
