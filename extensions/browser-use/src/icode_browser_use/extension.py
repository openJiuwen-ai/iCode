# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""``chrys.extensions`` adapter: the browser core as an iCode agent's tools.

One extension id (``browser``) owns a registry of ``browser_id -> BrowserSession``.
Every id gets its own state directory (one slot under the owner's profile folder),
so it also gets its own Chrome profile and instance lock: two sub-agents or
workflow nodes in one iCode session never share a page. ``ToolContext.agent_id``
keeps builds apart even when they share the session and profile name; the main
agent has none and keeps the session/profile key. The adapter never imports
iCode beyond ``chrys.extensions``; the core itself does not import iCode at all.

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
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit

from chrys.extensions import Extension, ToolCall, ToolContext, ToolError, TurnContext

from . import registry
from .actions import build_tool_defs
from .broker_client import BrokerCallUnknown, BrokerSession
from .config import BrowserConfig, BrowserEnv, ConfigError
from .errors import BrowserError, ConnectionClosed, Unavailable
from .locking import acquire_instance_lock, probe_instance_lock
from .logging import set_log_sink
from .prompts import BROWSER_PLAYBOOK
from .runtime import BrowserRuntime, call_tool
from .session import BrowserSession

logger = logging.getLogger(__name__)

extension = Extension()


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
    owner_build: str | None = None
    slot: int = 0
    url: str = ""
    title: str = ""
    backend: str = ""
    record: dict[str, object] = field(default_factory=dict)
    manifest: Path | None = None


_browsers: dict[str, _Browser] = {}
_MAX_BROWSERS = 2
"""Runaway guard: one agent build may keep at most this many browsers open."""


def _log_sink(line: str) -> None:
    logger.debug("%s", line)


set_log_sink(_log_sink)


def _agent_id(ctx: ToolContext) -> str | None:
    """The host's per-build id, when it distinguishes builds that share a session and profile."""
    return getattr(ctx, "agent_id", None)


def _owner_matches(browser: _Browser, ctx: ToolContext) -> bool:
    """Match the explicit browser handle to its owning session, agent profile and build."""
    return (
        browser.owner_session == ctx.session_id
        and browser.owner_agent == ctx.agent_name
        and browser.owner_build == _agent_id(ctx)
    )


def _owner_key(ctx: ToolContext) -> str:
    """A stable, filesystem-safe key for one agent build's profile and journal.

    The host's ``agent_id`` keeps a sub-agent tool or a workflow activation apart
    from another build that shares its session and profile name; the main agent
    has none, so its key is unchanged.
    """
    identity = f"{ctx.session_id or '-'}\x00{ctx.agent_name}"
    agent_id = _agent_id(ctx)
    if agent_id:
        identity += f"\x00{agent_id}"
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
    """Scheme and host only: context must not leak query tokens into a sibling's prompt."""
    if not url:
        return "(not navigated yet)"
    parts = urlsplit(url)
    if parts.scheme and parts.netloc:
        return f"{parts.scheme}://{parts.netloc}"
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


def _save_record(path: Path | None, record: dict[str, object]) -> None:
    """Persist one record, superseding older generations of its slot; never breaks a call."""
    if path is None:
        return
    try:
        records, problem = registry.load(path)
        if problem == "unreadable":
            logger.warning("browser manifest unreadable; skipping write to avoid wiping records")
            return
        registry.save(path, registry.supersede(records, record))
    except OSError as exc:
        logger.warning("browser manifest write failed: %s", exc)


def _browser_origin(browser: _Browser) -> str:
    """The redacted page origin, falling back to the live session snapshot."""
    return registry.redacted_origin(browser.url or browser.session.snapshot.url)


def _persist(browser: _Browser) -> None:
    """Refresh the redacted origin, writing only when it changed."""
    if browser.manifest is None or not browser.record:
        return
    origin = _browser_origin(browser)
    if browser.record.get("origin") == origin:
        return
    browser.record["origin"] = origin
    browser.record["updated_at"] = time.time()
    _save_record(browser.manifest, browser.record)


def _recoverable(ctx: ToolContext) -> list[dict[str, object]]:
    """Manifest records this session/agent may reopen, excluding live handles."""
    path = _manifest_path(ctx)
    if path is None:
        return []
    records, _problem = registry.load(path)
    live = {browser.record.get("resume_id") for browser in _mine(ctx) if browser.record}
    owner_key = _owner_key(ctx)
    return [
        record
        for record in records
        if record.get("owner_key") == owner_key
        and record.get("session_id") == ctx.session_id
        and record.get("agent_name") == ctx.agent_name
        and record.get("resume_id") not in live
    ]


def _open(ctx: ToolContext) -> _Browser:
    """Allocate a browser in the first free profile slot for this agent build.

    The registry is mutated only in this synchronous block, so no lock is needed:
    there is no ``await`` between reading the used slots and writing the entry.
    """
    base = _config()
    _prune_closed()
    used = {browser.slot for browser in _mine(ctx)}
    slot = next((index for index in range(_MAX_BROWSERS) if index not in used), None)
    if slot is None:
        raise ToolError(
            f"This agent already has {_MAX_BROWSERS} browsers open; reuse a browser_id or call browser_close.",
            code="browser_limit",
        )
    return _allocate(ctx, base, slot)


def _allocate(ctx: ToolContext, base: BrowserConfig, slot: int) -> _Browser:
    """Create the handle and core session for *slot*; nothing launches yet."""
    browser_id = "b" + secrets.token_hex(4)
    generation = secrets.token_hex(4)
    state_dir = base.state_dir / "agents" / _owner_key(ctx) / f"slot-{slot}"
    # Bridge mode must use the same discovery file the native host reads; per-agent
    # state dirs are for owned profiles only. One bridge per state dir is enforced by
    # BridgeServer's instance lock, so a second agent gets a clear "resource busy".
    config = base if base.bridge or base.broker else dataclasses.replace(base, state_dir=state_dir)
    if config.backend == "owned" and config.download_dir is not None:
        # Controlled downloads are session-scoped (the handle owns the browser).
        config = dataclasses.replace(config, download_dir=(ctx.session_data_dir or ctx.data_dir) / "downloads")
    session = (
        BrokerSession(
            base,
            owner=_owner_key(ctx),
            session_id=f"{_owner_key(ctx)}-slot-{slot}",
        )
        if base.broker
        else BrowserSession(
            config,
            journal_owner=_owner_key(ctx),
            journal_session=f"{_owner_key(ctx)}-slot-{slot}",
        )
    )
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
    browser = _Browser(
        browser_id,
        session,
        ctx.session_id,
        ctx.agent_name,
        owner_build=_agent_id(ctx),
        slot=slot,
        backend=backend,
        record=record,
        manifest=manifest,
    )
    _browsers[browser_id] = browser
    _save_record(manifest, record)
    return browser


def _get(ctx: ToolContext, browser_id: str) -> _Browser:
    _prune_closed()
    browser = _browsers.get(browser_id)
    if browser is None:
        raise ToolError(
            f"Unknown browser_id {browser_id!r}; call browser_list to see this session's browsers, "
            "or browser_open to allocate a new browser handle.",
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
        raise ToolError(exc.render(), code=exc.code) from exc
    except BrokerCallUnknown as exc:
        return [{"type": "text", "text": str(exc)}]
    except BrowserError as exc:
        raise ToolError(exc.render(), code=exc.code) from exc


def _text(blocks: list[dict[str, object]]) -> str:
    return "\n".join(
        str(block.get("text", "")) for block in blocks if block.get("type") == "text" and block.get("text")
    )


async def _run(browser: _Browser, name: str, arguments: dict[str, object]) -> str:
    """Run one core tool and return its text blocks joined for the model."""
    try:
        blocks = await _call(browser, name, arguments)
    finally:
        snapshot = browser.session.snapshot
        browser.url = snapshot.url or browser.url
        browser.title = snapshot.title or browser.title
        _persist(browser)
    return _text(blocks)


@extension.tool(requires_approval=True)
async def browser_open(ctx: ToolContext) -> str:
    """Allocate a browser handle and return its browser_id; launch/connect is deferred until a page tool needs it."""
    browser = _open(ctx)
    return f"browser_id: {browser.id}"


def _describe(browser: _Browser) -> tuple[str, str, str]:
    """Map the transport state to ``(state, reason, next step)`` for the model."""
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


@extension.tool(requires_approval=False)
async def browser_list(ctx: ToolContext) -> str:
    """List this session's browsers plus recorded handles that can be reopened; never launches Chrome."""
    mine = _mine(ctx)
    path = _manifest_path(ctx)
    records: list[dict[str, object]] = []
    problem: str | None = None
    if path is not None:
        records, problem = registry.load(path)
    owner_key = _owner_key(ctx)
    own = [
        record
        for record in records
        if record.get("owner_key") == owner_key
        and record.get("session_id") == ctx.session_id
        and record.get("agent_name") == ctx.agent_name
    ]
    live = {browser.record.get("resume_id") for browser in mine if browser.record}
    recoverable = [record for record in own if record.get("resume_id") not in live]
    unclaimable = sum(1 for record in records if record.get("session_id") != ctx.session_id)

    lines = [f"open={len(mine)} recoverable={len(recoverable)} manifest={problem or 'ok'}"]
    for browser in mine:
        state, reason, next_step = _describe(browser)
        line = (
            f"- browser_id={browser.id} state={state} backend={browser.backend or 'unknown'} "
            f"slot={browser.slot} origin={_browser_origin(browser)}"
        )
        resume_id = browser.record.get("resume_id")
        if resume_id:
            line += f" resume_id={resume_id}"
        lines.append(line)
        if reason:
            lines.append(f"  reason: {reason}")
        lines.append(f"  next: {next_step}")
    for record in recoverable:
        lines.append(
            f"- resume_id={record.get('resume_id')} state=recorded backend={record.get('backend')} "
            f"slot={record.get('slot')} origin={record.get('origin')}"
        )
        lines.append("  next: call browser_resume to reopen this handle (it does not navigate)")
    if unclaimable:
        lines.append(f"unclaimable={unclaimable} (records copied from another session; history only)")
    if not mine and not recoverable:
        lines.append("next: call browser_open to allocate a browser handle")
    return "\n".join(lines)


@extension.tool(requires_approval=False)
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


@extension.tool(requires_approval=True)
async def browser_resume(
    ctx: ToolContext,
    resume_id: Annotated[str, "resume_id from browser_list for a recorded, not-live browser"],
) -> str:
    """Reopen a recorded browser by resume_id; never navigates or replays an action."""
    base = _config()
    path = _manifest_path(ctx)
    records, _problem = registry.load(path) if path is not None else ([], None)
    record = next((item for item in records if item.get("resume_id") == resume_id), None)
    if record is None:
        raise ToolError(
            f"Unknown resume_id {resume_id!r}; call browser_list to see this session's recorded browsers.",
            code="unknown_resume",
        )
    if (
        record.get("owner_key") != _owner_key(ctx)
        or record.get("session_id") != ctx.session_id
        or record.get("agent_name") != ctx.agent_name
    ):
        raise ToolError(
            "That record belongs to another session or agent; a copied/forked session cannot claim it. "
            "Call browser_open to start a browser for this session.",
            code="browser_resume_denied",
        )
    for browser in _mine(ctx):
        if browser.record.get("resume_id") == resume_id:
            return f"browser_id: {browser.id}\nresume_id {resume_id} is already live; use its browser_id."
    backend = base.backend
    if record.get("backend") != backend:
        raise ToolError(
            f"That browser was opened in {record.get('backend')} mode but this plugin is configured for {backend}; "
            "restore the original configuration or call browser_open instead.",
            code="browser_resume_backend_mismatch",
        )
    slot = int(record["slot"])  # type: ignore[arg-type]
    if any(browser.slot == slot for browser in _mine(ctx)):
        raise ToolError(
            f"Slot {slot} already has a live browser; use its browser_id, close it first, or call browser_open.",
            code="browser_resume_conflict",
        )
    if backend == "owned":
        state_dir = base.state_dir / "agents" / _owner_key(ctx) / f"slot-{slot}"
        try:
            probe = await acquire_instance_lock(state_dir, wait=0)
        except Unavailable as exc:
            raise ToolError(
                f"That profile is in use by another plugin instance ({exc}); close it before resuming.",
                code="browser_resume_blocked",
            ) from exc
        probe.release()
    browser = _allocate(ctx, base, slot)
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


@extension.tool(requires_approval=True)
async def browser_navigate(
    ctx: ToolContext,
    url: Annotated[
        str,
        "Absolute HTTP(S) URL or relative path against the current HTTP(S) page. "
        "Public sites work by default. ICODE_BROWSER_ALLOWED_DOMAINS and ICODE_BROWSER_ALLOWED_ORIGINS "
        "restrict destinations when configured. Local origins require explicit access.",
    ],
    browser_id: Annotated[str, "Browser id returned by browser_open"],
) -> str:
    """Navigate an explicit browser and return its fresh element snapshot."""
    browser = _get(ctx, browser_id)
    text = await _run(browser, "browser_navigate", {"url": url})
    return f"browser_id: {browser.id}\n{text}"


@extension.tool(requires_approval=False)
async def browser_tabs(
    ctx: ToolContext,
    browser_id: Annotated[str, "Browser id returned by browser_open"],
) -> str:
    """List the tabs this handle owns (root tab plus popups) with tab_id and current marker."""
    browser = _get(ctx, browser_id)
    return await _run(browser, "browser_tabs", {})


@extension.tool(requires_approval=False)
async def browser_tab_switch(
    ctx: ToolContext,
    browser_id: Annotated[str, "Browser id returned by browser_open"],
    tab_id: Annotated[str, "A tab_id listed by browser_tabs"],
) -> str:
    """Make another owned tab current; refs from the old tab become stale and a snapshot returns."""
    browser = _get(ctx, browser_id)
    return await _run(browser, "browser_tab_switch", {"tab_id": tab_id})


@extension.tool(requires_approval=True)
async def browser_tab_close(
    ctx: ToolContext,
    browser_id: Annotated[str, "Browser id returned by browser_open"],
    tab_id: Annotated[str, "A tab_id listed by browser_tabs"],
) -> str:
    """Close one owned tab; closing the current tab makes its opener current. The last tab stays open."""
    browser = _get(ctx, browser_id)
    return await _run(browser, "browser_tab_close", {"tab_id": tab_id})


@extension.tool(requires_approval=False)
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


@extension.tool(requires_approval=False)
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


@extension.tool(requires_approval=True)
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
) -> str:
    """Click an element by ref or text; a stale ref fails and asks for a new snapshot.

    The mouse click targets the element's centre and, when that is covered, points near its
    corners (result shows hit_point=corner); when every point is covered nothing is sent
    (status=blocked), and when a hit test cannot be answered nothing is sent either
    (status=uncertain). The result reports effect=navigation|dom|dom_elsewhere|focus|none|unknown;
    verified=control_state means the same control changed its accessibility state or label.
    There is never an automatic JS second click. On effect=none or status=uncertain do not retry
    the action: verify the expected state with browser_find(fresh=true) or a scoped snapshot, and for a text
    field use browser_fill with submit=true. dispatch=dom is enforced to one try per ref in a
    document: once any record for that ref carries the dom label it is refused, and it is otherwise
    refused unless the newest mouse click on that ref was all-occluded, or effect=none on a
    non-submit target.
    """
    browser = _get(ctx, browser_id)
    return await _run(
        browser,
        "browser_click",
        {"target": target, "button": button, "click_count": click_count, "dispatch": dispatch},
    )


@extension.tool(requires_approval=False)
async def browser_hover(
    ctx: ToolContext,
    browser_id: Annotated[str, "Browser id returned by browser_open"],
    target: Annotated[str, "An @eN#epoch ref from the latest snapshot, or free text to resolve"],
) -> str:
    """Move the mouse over an element without clicking; then snapshot to see hover controls."""
    browser = _get(ctx, browser_id)
    return await _run(browser, "browser_hover", {"target": target})


@extension.tool(requires_approval=True)
async def browser_fill(
    ctx: ToolContext,
    browser_id: Annotated[str, "Browser id returned by browser_open"],
    target: Annotated[str, "An @eN#epoch ref from the latest snapshot, or free text to resolve"],
    text: Annotated[str, "Text to type into the field"],
    clear: Annotated[bool, "Clear the field first"] = True,
    submit: Annotated[bool, "Press Enter after typing"] = False,
    keypress_fallback: Annotated[bool, "Type character by character if insertText fails"] = False,
) -> str:
    """Type into a field with real input events; returns what the page reports.

    submit=true sends a real Enter and reports submitted only when the editor cleared or the
    page navigated; a page that merely updated is reported as unconfirmed. For chat-style SPAs
    (composer + send button) prefer submit=true first, over a separate browser_press_key or a
    click on the send button.
    """
    browser = _get(ctx, browser_id)
    return await _run(
        browser,
        "browser_fill",
        {
            "target": target,
            "text": text,
            "clear": clear,
            "submit": submit,
            "keypress_fallback": keypress_fallback,
        },
    )


@extension.tool(requires_approval=True)
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


@extension.tool(requires_approval=True)
async def browser_select_option(
    ctx: ToolContext,
    browser_id: Annotated[str, "Browser id returned by browser_open"],
    target: Annotated[str, "An @eN#epoch ref from the latest snapshot, or free text to resolve"],
    values: Annotated[list[str], "Option values (or visible labels) to select; one for a single <select>"],
) -> str:
    """Select options on a native <select> and report the values read back from the page."""
    browser = _get(ctx, browser_id)
    return await _run(browser, "browser_select_option", {"target": target, "values": values})


@extension.tool(requires_approval=False)
async def browser_wait_for(
    ctx: ToolContext,
    browser_id: Annotated[str, "Browser id returned by browser_open"],
    condition: Annotated[
        Literal["visible", "hidden", "editable", "text", "url", "text_changed", "stable", "count"],
        "What to wait for",
    ],
    target: Annotated[
        str,
        "An @eN#epoch ref or free text; required for visible/hidden/editable, optional for text/text_changed/stable "
        "(page-wide when omitted); for count it is the browser_find query",
    ] = "",
    value: Annotated[
        str,
        "Text to find or the URL substring (text/url); quiet window in ms for stable (default 1500, max 5000); "
        "a comparison such as '>3' or '=2' for count",
    ] = "",
    timeout_ms: Annotated[int, "Wait budget in milliseconds (100-180000)"] = 5000,
) -> str:
    """Wait for a page condition with a hard deadline; on timeout it reports what it last saw.

    text_changed succeeds on the first difference from the text seen when the wait started and
    returns the new text; stable succeeds once no DOM mutation happened for value ms (not network
    idle); count compares the number of browser_find matches. After sending a chat message use
    wait_for(hidden, 'Stop generating') or wait_for(stable, target=<last message ref>) instead
    of sleeping.
    """
    browser = _get(ctx, browser_id)
    return await _run(
        browser,
        "browser_wait_for",
        {"condition": condition, "target": target, "value": value, "timeout_ms": timeout_ms},
    )


@extension.tool(requires_approval=True)
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


@extension.tool(requires_approval=False)
async def browser_downloads(
    ctx: ToolContext,
    browser_id: Annotated[str, "Browser id returned by browser_open"],
) -> str:
    """List this handle's downloads with state, bytes, safe name and absolute path."""
    browser = _get(ctx, browser_id)
    return await _run(browser, "browser_downloads", {})


@extension.tool(requires_approval=True)
async def browser_download_cancel(
    ctx: ToolContext,
    browser_id: Annotated[str, "Browser id returned by browser_open"],
    guid: Annotated[str, "guid from browser_downloads"],
) -> str:
    """Cancel one in-progress download; partial files are removed, nothing is retried."""
    browser = _get(ctx, browser_id)
    return await _run(browser, "browser_download_cancel", {"guid": guid})


@extension.tool(requires_approval=False)
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


@extension.tool(requires_approval=False)
async def browser_extract(
    ctx: ToolContext,
    browser_id: Annotated[str, "Browser id returned by browser_open"],
    query: Annotated[str | None, "Optional element to extract; omit for the whole page"] = None,
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


@extension.tool(requires_approval=False)
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


@extension.tool(requires_approval=True)
async def browser_back(
    ctx: ToolContext,
    browser_id: Annotated[str, "Browser id returned by browser_open"],
) -> str:
    """Go back one entry in the page's navigation history and return a new snapshot."""
    browser = _get(ctx, browser_id)
    return await _run(browser, "browser_back", {})


@extension.tool(requires_approval=True)
async def browser_forward(
    ctx: ToolContext,
    browser_id: Annotated[str, "Browser id returned by browser_open"],
) -> str:
    """Go forward one entry in the page's navigation history and return a new snapshot."""
    browser = _get(ctx, browser_id)
    return await _run(browser, "browser_forward", {})


@extension.tool(requires_approval=True)
async def browser_reload(
    ctx: ToolContext,
    browser_id: Annotated[str, "Browser id returned by browser_open"],
    ignore_cache: Annotated[bool, "Bypass the HTTP cache"] = False,
) -> str:
    """Reload the current page and return a new snapshot; a POST result page may resubmit."""
    browser = _get(ctx, browser_id)
    return await _run(browser, "browser_reload", {"ignore_cache": ignore_cache})


@extension.tool(requires_approval=False)
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
    blocks = await _call(browser, "browser_screenshot", arguments)
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
    return (
        f"Screenshot saved to {path} ({mime}, {pixels}{len(data)} bytes)\n"
        f"Session file: {relative}\n"
        "Use view_image with the absolute path to inspect it."
    )


async def _close_and_forget(browser: _Browser) -> None:
    """Close the core session, then drop the handle and its recorded identity.

    This runs as its own task so a cancelled turn cannot stop the bookkeeping
    halfway: the record disappears with the browser, not with the caller.
    """
    await browser.session.aclose()
    _browsers.pop(browser.id, None)
    _drop_record(browser)


def _drop_record(browser: _Browser) -> None:
    """Remove the durable record that would otherwise offer a resume for a closed browser."""
    resume_id = browser.record.get("resume_id")
    if not resume_id or browser.manifest is None:
        return
    try:
        records, _problem = registry.load(browser.manifest)
        registry.save(browser.manifest, registry.drop(records, str(resume_id)))
    except OSError as exc:
        logger.warning("browser manifest update failed: %s", exc)


def _log_late_close_failure(task: asyncio.Task[None]) -> None:
    """Report a shielded close that failed after its caller was cancelled; there is nobody to raise to."""
    if task.cancelled():
        return
    error = task.exception()
    if error is not None:
        logger.warning("browser close finished after cancellation with an error: %s", error)


@extension.tool(requires_approval=True)
async def browser_close(
    ctx: ToolContext,
    browser_id: Annotated[str, "Browser id to close"],
) -> str:
    """Close a browser and release its Chrome process."""
    browser = _get(ctx, browser_id)
    task = asyncio.create_task(_close_and_forget(browser))
    try:
        # Shield the teardown: a cancelled turn must neither stop Chrome teardown
        # nor skip removing the handle and its recoverable record.
        await asyncio.shield(task)
    except asyncio.CancelledError:
        task.add_done_callback(_log_late_close_failure)
        raise
    except Exception as exc:
        raise ToolError(f"browser close failed: {exc}", code="browser_close_failed") from exc
    return f"browser_id {browser.id} closed"


@extension.tool(requires_approval=True)
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
        if configured_roots:
            if not any(candidate == root or root in candidate.parents for root in configured_roots):
                raise ToolError(
                    "Upload path is outside ICODE_BROWSER_UPLOAD_ROOTS.",
                    code="browser_upload_denied",
                )
        else:
            browser.session.cfg = dataclasses.replace(browser.session.cfg, upload_roots=workspaces)
    elif not BrowserEnv().get("UPLOAD_ROOTS"):
        raise ToolError(
            "Broker upload requires ICODE_BROWSER_UPLOAD_ROOTS configured before the Broker starts.",
            code="browser_upload_denied",
        )
    return await _run(browser, "browser_upload", {"target": target, "path": str(candidate)})


@extension.tool(requires_approval=False)
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
        return (
            f"Open browsers: {len(mine)}. Use the browser_id returned by browser_open for browser_* tools. "
            + BROWSER_PLAYBOOK
        )
    recorded = _recoverable(ctx)
    if recorded:
        return (
            f"Open browsers: 0, but {len(recorded)} browser(s) recorded for this session can be reopened. "
            "Call browser_list to see them, then browser_resume."
        )
    return None


@extension.before_tool
async def browser_guard(call: ToolCall) -> None:
    """In strict mode, refuse shell and file writes while this agent has a browser open."""
    if (BrowserEnv().get("GUARD") or "").strip().lower() != "strict":
        return
    if call.kind not in {"shell", "filesystem.write"}:
        return
    if _mine(call.context):
        raise ToolError(
            "A browser is open; shell and file writes are blocked while ICODE_BROWSER_GUARD=strict.",
            code="browser_guard",
        )
