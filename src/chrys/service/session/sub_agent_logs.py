# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Sub-agent audit log storage."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from chrys.foundation.platform import get_platform
from chrys.foundation.platform.files import (
    SecureFileError,
    atomic_write_owner_only_bytes,
    atomic_write_owner_only_text,
    fsync_directory,
    read_owner_verified_bounded,
    secure_open_owner_only,
    secure_unlink_owner_verified,
    surrogate_safe_text,
)
from chrys.foundation.platform.key_files import publish_owner_only_key, read_owner_only_key
from chrys.foundation.trajectory.metadata import read_analytics_item_id
from chrys.foundation.util.filenames import bounded_safe_stem, safe_fs_name
from chrys.foundation.util.lock import FileLock
from chrys.service.session.message_metadata import TOOL_RESULT_METADATA_KEY
from chrys.service.state.serializers import serialize_state

logger = logging.getLogger(__name__)

SUB_AGENT_LOG_PREVIEW_MAX_CHARS = 2000
SUB_AGENT_TRANSCRIPT_OCCURRENCE_IDENTITY = "analytics_item_id_v1"
SUB_AGENT_LOG_TMP_PREFIX = "."
SUB_AGENT_LOG_TMP_SUFFIX = ".tmp"
SUB_AGENT_PENDING_SOURCE_PATH_KEY = "_chrys_source_path"
SUB_AGENT_RESTORE_CONSUMED_KEY = "_chrys_restore_consumed"
ACP_ANSWER_WITHDRAWN_UPDATE = "answer_withdrawn"
"""The ``sessionUpdate`` of a local ACP audit entry: a retry withdrew the answer its attempt showed after its last card."""
_INVOCATION_ID_RE = re.compile(r"^[0-9a-f]{12}$")
_ACP_HMAC_KEY_FILE = "acp-audit-hmac.key"
_ACP_HMAC_KEY_BYTES = 32
_ACP_RING_ITEMS = 500
_ACP_RING_BYTES = 256 * 1024
_ACP_ATTEMPT_ITEMS = 50
# Read ceilings for owner-VERIFIED artifacts. Owner verification proves the
# file is same-uid, but a same-uid ACP child is UNTRUSTED (it may plant a
# huge/sparse record), so restore/fork reads must be bounded to stop one
# small-on-disk file from forcing an unbounded allocation. Pending records
# are tiny metadata; audit envelopes may embed a kernel sub-agent's full
# serialized history (including images), so they get far more headroom.
_MAX_PENDING_RECORD_BYTES = 4 * 1024 * 1024
MAX_SUB_AGENT_AUDIT_BYTES = 128 * 1024 * 1024
# A same-uid ACP child is untrusted: individual pending records are bounded to
# _MAX_PENDING_RECORD_BYTES, but the child can plant hundreds of them, so drain
# must also cap the AGGREGATE it materializes in memory per restore. Past the
# cap the remainder is deferred, not dropped — finalize deletes the records
# consumed this restore, so the leftovers drain over subsequent restores.
MAX_PENDING_RECORDS = 256
MAX_PENDING_RECORDS_TOTAL_BYTES = 32 * 1024 * 1024
# Even before the per-record caps apply, the mere ENUMERATION of a session
# artifact directory is attacker-influenced: a same-uid child can plant millions
# of tiny files. `Path.glob`/`Path.iterdir` are EAGER on 3.14 (they list scandir
# before yielding), so `scan_bounded_json_files` drives `os.scandir` directly and
# breaks after MAX_SCANNED_* examined entries. A truncated scan defers the
# remainder; corrupt/consumed files leave the scan over restores so it converges.
MAX_SCANNED_PENDING_FILES = 4096
MAX_SCANNED_AUDIT_FILES = 4096
# Enumeration count is not enough: a bounded set of files can still be huge/
# sparse, so reading them all (`read_owner_verified_bounded`, up to the per-file
# ceilings) would itself be an unbounded I/O DoS (e.g. 4096 x 128 MiB audit
# reads). Bound the CUMULATIVE on-disk size a single scan will accept for
# reading, using scandir's free st_size to refuse a bomb without reading it.
MAX_PENDING_SCAN_TOTAL_BYTES = 64 * 1024 * 1024
MAX_AUDIT_SCAN_TOTAL_BYTES = 512 * 1024 * 1024
# Marks a paused record whose still-"running" audit could NOT be re-flipped to
# "orphaned" this restore (transient audit-write failure). finalize keeps such
# records so the next restore retries the repair; deleting them would strand
# the audit "running" and backlinked, hence permanently exempt from repair.
SUB_AGENT_REPAIR_FAILED_KEY = "_chrys_repair_failed"


@dataclass(slots=True)
class SubAgentLogStats:
    """In-memory counters snapshotted into sub-agent audit logs."""

    tool_call_count: int = 0
    total_tokens: int = 0
    total_usage_tokens: int = 0
    usage_unreported_attempts: int = 0

    def record_tool_call(self) -> None:
        self.tool_call_count += 1

    def record_usage(self, total_tokens: int, total_usage_tokens: int) -> None:
        self.total_tokens = total_tokens
        self.total_usage_tokens = total_usage_tokens


@dataclass(frozen=True, slots=True)
class SubAgentModelProvenance:
    """Non-sensitive model provenance stored in sub-agent audit metadata."""

    provider: str = ""
    api_style: str = ""
    model_id: str = ""
    base_url_origin: str = ""


class SubAgentSessionLogWriter:
    """Cancellation-safe per-invocation writer for one sub-agent log file."""

    def __init__(
        self,
        *,
        parent_session_dir: Path | None,
        parent_session_id: str | None,
        invocation_id: str,
        tool_name: str,
        agent_profile: str,
        agent_display_name: str,
        prompt: str,
        parent_provider_call_id: str,
        parent_event_call_id: str,
        model: SubAgentModelProvenance | None = None,
        primary_cwd: str = "",
        working_dirs: list[str] | None = None,
        stats: SubAgentLogStats | None = None,
        runner: str = "kernel",
    ) -> None:
        self._parent_session_dir = parent_session_dir
        self._parent_session_id = parent_session_id or ""
        self._invocation_id = invocation_id
        self._tool_name = tool_name
        self._agent_profile = agent_profile
        self._agent_display_name = agent_display_name
        self._prompt_preview = preview_text(prompt)
        self._parent_provider_call_id = parent_provider_call_id
        self._parent_event_call_id = parent_event_call_id
        self._model = model or SubAgentModelProvenance()
        self._primary_cwd = primary_cwd
        self._working_dirs = list(working_dirs or ([primary_cwd] if primary_cwd else []))
        self._stats = stats or SubAgentLogStats()
        self._runner = runner
        self._acp_command = ""
        self._acp_args: list[str] = []
        self._created_at = _utc_now()
        self._lock = asyncio.Lock()
        self._active = False
        self._log_file_name = make_sub_agent_log_file_name(tool_name, invocation_id)

    @property
    def log_file_name(self) -> str:
        return self._log_file_name

    @property
    def path(self) -> Path | None:
        if self._parent_session_dir is None:
            return None
        return sessions_dir(self._parent_session_dir) / self._log_file_name

    @property
    def active(self) -> bool:
        return self._active

    def set_acp_launch(self, command: str, args: list[str]) -> None:
        """Update the resolved launch argv used by the next ACP envelope write."""
        if self._runner != "acp":
            raise RuntimeError("ACP launch provenance is available only for ACP audit writers")
        self._acp_command = command
        self._acp_args = list(args)

    async def write(
        self,
        *,
        status: str,
        state: dict[str, Any] | None = None,
        result: str = "",
        failure_reason: str = "",
        last_error: str = "",
        ended: bool = False,
        acp_state: dict[str, Any] | None = None,
    ) -> bool:
        """Best-effort write of the current invocation envelope."""
        path = self.path
        if path is None:
            return False
        async with self._lock:
            write_task: asyncio.Task[None] | None = None
            cancelled = False
            try:
                envelope = self._envelope(
                    status=status,
                    state=state,
                    result=result,
                    failure_reason=failure_reason,
                    last_error=last_error,
                    ended=ended,
                    acp_state=acp_state,
                )
                write_task = asyncio.create_task(asyncio.to_thread(_atomic_write_json, path, envelope))
                try:
                    await asyncio.shield(write_task)
                except asyncio.CancelledError:
                    cancelled = True
                    while not write_task.done():
                        try:
                            await asyncio.shield(write_task)
                        except asyncio.CancelledError:
                            cancelled = True
                    write_task.result()
            except Exception as exc:
                logger.warning("Failed to write sub-agent audit log %s: %s", path.name, exc)
                if cancelled:
                    raise asyncio.CancelledError from exc
                if self._runner == "acp":
                    raise
                return False
            self._active = True
            if cancelled:
                raise asyncio.CancelledError
            return True

    def _envelope(
        self,
        *,
        status: str,
        state: dict[str, Any] | None,
        result: str,
        failure_reason: str,
        last_error: str,
        ended: bool,
        acp_state: dict[str, Any] | None,
    ) -> dict[str, Any]:
        serialized_state = serialize_state(state or {}) if self._runner == "kernel" else {}
        now = _utc_now()
        meta = {
            "schema_version": 1,
            "runner": self._runner,
            "record_type": "sub_agent_session",
            "parent_session_id": self._parent_session_id,
            "parent_event_call_id": self._parent_event_call_id,
            "parent_provider_call_id": self._parent_provider_call_id,
            "invocation_id": self._invocation_id,
            "tool_name": self._tool_name,
            "agent_profile": self._agent_profile,
            "agent_display_name": self._agent_display_name,
            "status": status,
            "failure_reason": failure_reason,
            "last_error": last_error,
            "created_at": self._created_at,
            "updated_at": now,
            "ended_at": now if ended else "",
            "prompt_preview": self._prompt_preview,
            "result_preview": preview_text(result),
            "message_count": len(serialized_state.get("messages", [])),
            "tool_call_count": self._stats.tool_call_count,
            "total_tokens": self._stats.total_tokens,
            "total_usage_tokens": self._stats.total_usage_tokens,
            "usage_unreported_attempts": self._stats.usage_unreported_attempts,
            "primary_cwd": self._primary_cwd,
            "working_dirs": list(self._working_dirs),
            "model_provider": self._model.provider,
            "model_api_style": self._model.api_style,
            "model_id": self._model.model_id,
            "model_base_url_origin": self._model.base_url_origin,
        }
        if self._runner == "kernel" and serialized_history_has_occurrence_ids(serialized_state):
            meta["transcript_occurrence_identity"] = SUB_AGENT_TRANSCRIPT_OCCURRENCE_IDENTITY
        if self._runner == "acp":
            normalized = normalize_acp_state(acp_state or {}, parent_session_dir=self._parent_session_dir)
            normalized["usage_unreported_attempts"] = self._stats.usage_unreported_attempts
            normalized["launch"] = acp_launch_provenance(self._acp_command, self._acp_args)
            return {"meta": meta, "acp_state": normalized}
        return {"meta": meta, "state": serialized_state}


def serialized_history_has_occurrence_ids(state: dict[str, Any]) -> bool:
    """Return whether every persisted kernel message has stable occurrence identity."""
    live_messages = state.get("messages")
    compressed = state.get("compressed_msgs")
    if not isinstance(live_messages, list) or not isinstance(compressed, list):
        return False
    messages: list[Any] = list(live_messages)
    for block in compressed:
        if not isinstance(block, dict):
            return False
        block_messages = block.get("messages")
        if not isinstance(block_messages, list):
            return False
        messages.extend(block_messages)
    return all(
        isinstance(message, dict) and read_analytics_item_id(message.get("additional_properties")) is not None
        for message in messages
    )


def pending_dir(parent_session_dir: Path) -> Path:
    return parent_session_dir / "sub_agents" / "pending"


def sessions_dir(parent_session_dir: Path) -> Path:
    return parent_session_dir / "sub_agents" / "sessions"


def make_sub_agent_log_file_name(tool_name: str, invocation_id: str) -> str:
    # The invocation id is appended as the filename suffix. Sanitize it as well
    # as the tool-name stem: a chrys-generated 12-hex id passes through
    # unchanged (``safe_fs_name`` is a length-preserving 1:1 substitution), but
    # an attacker-plantable pause record could carry a traversal payload
    # (``/../../victim``) that would otherwise escape the sessions directory
    # when this name is path-joined. Stripping the separators keeps the result
    # a single in-directory basename regardless of caller trust.
    suffix = f"_{safe_fs_name(invocation_id)}.json"
    stem = bounded_safe_stem(
        tool_name,
        suffix=suffix,
        temp_prefix=SUB_AGENT_LOG_TMP_PREFIX,
        temp_suffix=SUB_AGENT_LOG_TMP_SUFFIX,
    )
    return f"{stem}{suffix}"


def validate_invocation_id(invocation_id: str) -> bool:
    return bool(_INVOCATION_ID_RE.fullmatch(invocation_id))


def preview_text(text: str, *, max_chars: int = SUB_AGENT_LOG_PREVIEW_MAX_CHARS) -> str:
    """Return a bounded preview, appending ``...`` only within the cap."""
    if len(text) <= max_chars:
        return text
    if max_chars <= 3:
        return "." * max(0, max_chars)
    return f"{text[: max_chars - 3]}..."


def sanitize_base_url_origin(raw_url: str) -> str:
    """Return ``scheme://host[:port]`` for normal absolute URLs, else ``""``."""
    if not raw_url:
        return ""
    try:
        parsed = urlsplit(raw_url)
    except ValueError:
        return ""
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return ""
    try:
        port = parsed.port
    except ValueError:
        return ""
    host = parsed.hostname
    if port is None:
        return f"{parsed.scheme}://{host}"
    return f"{parsed.scheme}://{host}:{port}"


def redact_acp_argv(args: list[str]) -> list[str]:
    """Conservatively retain option prefixes while redacting every value."""
    redacted: list[str] = []
    positional_only = False
    for arg in args:
        if positional_only:
            # Everything after ``--`` is positional (doc §10: every attached
            # AND positional value is replaced), even if it starts with ``-``.
            redacted.append("<redacted>")
            continue
        if arg == "--":
            # The end-of-options marker is a fixed token, not a secret; keep
            # it and redact everything that follows.
            positional_only = True
            redacted.append(arg)
        elif arg.startswith("--") and "=" in arg:
            option, _value = arg.split("=", 1)
            redacted.append(f"{option}=<redacted>")
        elif arg.startswith("--") and len(arg) > 2:
            redacted.append(arg)
        elif arg.startswith("-") and len(arg) > 2:
            redacted.append(f"{arg[:2]}<redacted>")
        elif arg.startswith("-") and len(arg) == 2:
            redacted.append(arg)
        else:
            redacted.append("<redacted>")
    return redacted


def _audit_safe_text(value: str) -> str:
    """Escape any unpaired surrogate so this string is safe to audit.

    POSIX filesystem paths (and env-expanded launch argv derived from them) can
    carry surrogateescaped bytes — legal str values that are NOT strict-UTF-8
    encodable. Left raw they crash the audit twice: the provenance digest below
    and the ``ensure_ascii=False`` file write, and even a surrogatepass-written
    file would fail the audit's strict-UTF-8 JSON read-back in reconcile/fork.
    ``backslashreplace`` turns a lone surrogate into its ``\\udcXX`` escape (a
    no-op on valid text), keeping the stored value total and round-trippable
    while the digest below still binds the exact original bytes.

    This is per-field defense-in-depth for the provenance digest inputs; the
    whole-envelope boundary sanitize in ``_atomic_write_json`` (and the sibling
    ACP writers) is what guarantees EVERY field — meta paths included — is
    surrogate-free before the strict file write.
    """
    return surrogate_safe_text(value)


def acp_launch_provenance(command: str, args: list[str]) -> dict[str, Any]:
    """Return non-secret launch metadata plus a keyed full-argv digest."""
    key = load_or_create_acp_audit_key()
    # surrogatepass keeps the digest total over the EXACT argv bytes even when a
    # value carries a surrogateescaped filesystem byte; the stored command/args
    # are surrogate-escaped so the audit envelope stays strict-UTF-8 round-trippable.
    payload = json.dumps(
        [command, *args],
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8", "surrogatepass")
    return {
        "command": _audit_safe_text(command),
        "args": [_audit_safe_text(arg) for arg in redact_acp_argv(args)],
        "arg_count": len(args),
        "argv_hmac_sha256": hmac.new(key, payload, hashlib.sha256).hexdigest(),
    }


def load_or_create_acp_audit_key(*, config_dir: Path | None = None, timeout: float = 5.0) -> bytes:
    """Securely validate or no-replace-create the installation ACP HMAC key."""
    root = config_dir or get_platform().config_dir
    root.mkdir(parents=True, exist_ok=True)
    key_path = root / _ACP_HMAC_KEY_FILE
    lock_path = root / f"{_ACP_HMAC_KEY_FILE}.lock"
    with FileLock(lock_path, timeout=timeout):
        if key_path.exists() or key_path.is_symlink():
            return _read_acp_audit_key(key_path)

        temp_path = root / f".{_ACP_HMAC_KEY_FILE}.{secrets.token_hex(8)}.tmp"
        fd = secure_open_owner_only(temp_path, write=True, create=True)
        try:
            key = secrets.token_bytes(_ACP_HMAC_KEY_BYTES)
            with os.fdopen(fd, "wb") as file:
                fd = -1
                file.write(key)
                file.flush()
                os.fsync(file.fileno())
            try:
                publish_owner_only_key(temp_path, key_path)
            except FileExistsError:
                # A creator not using this lock is still never allowed to be
                # overwritten; validate what won the no-replace publish race.
                return _read_acp_audit_key(key_path)
            fsync_directory(root)
            return _read_acp_audit_key(key_path)
        finally:
            if fd >= 0:
                os.close(fd)
            secure_unlink_owner_verified(temp_path)


def _read_acp_audit_key(path: Path) -> bytes:
    try:
        key = read_owner_only_key(path, max_bytes=_ACP_HMAC_KEY_BYTES + 1)
    except OSError as exc:
        raise SecureFileError("The ACP audit HMAC key failed secure verification.") from exc
    if len(key) != _ACP_HMAC_KEY_BYTES:
        raise SecureFileError("The ACP audit HMAC key has an invalid length.")
    return key


def normalize_acp_state(state: dict[str, Any], *, parent_session_dir: Path | None) -> dict[str, Any]:
    """Bound the ACP codec and normalize referenced files session-relatively."""
    normalized = json.loads(json.dumps(state, ensure_ascii=False, default=str, allow_nan=False))
    updates = normalized.get("translated_updates")
    if isinstance(updates, list):
        ring: list[Any] = []
        # Count list delimiters and commas as well as entry payloads.
        size = 2
        for update in reversed(updates[-_ACP_RING_ITEMS:]):
            item_size = len(
                json.dumps(
                    update,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    allow_nan=False,
                ).encode()
            )
            added = item_size + (1 if ring else 0)
            if size + added > _ACP_RING_BYTES:
                break
            ring.append(update)
            size += added
        normalized["translated_updates"] = list(reversed(ring))
    attempts = normalized.get("attempts")
    if isinstance(attempts, list) and len(attempts) > _ACP_ATTEMPT_ITEMS:
        # Manual retries are unlimited, so the attempt history needs a ring
        # like translated_updates. Newest-wins; per-entry fields are already
        # client-capped (ACP session ids are validated at 4096 chars).
        normalized["attempts"] = attempts[-_ACP_ATTEMPT_ITEMS:]
    for key in ("diagnostic_path", "stderr_log_path"):
        raw = normalized.get(key)
        if not raw:
            continue
        if parent_session_dir is None:
            normalized.pop(key, None)
            continue
        root = parent_session_dir.resolve()
        path = Path(str(raw))
        if not path.is_absolute():
            path = root / path
        try:
            resolved = path.resolve()
            normalized[key] = str(resolved.relative_to(root))
        except (OSError, RuntimeError, ValueError) as exc:
            raise ValueError(f"ACP state file reference escapes the session directory: {key}") from exc
    return normalized


def validate_acp_state_file_references(state: dict[str, Any], *, parent_session_dir: Path) -> None:
    """Reject absolute, parent-traversing, and symlink-escaping ACP references."""
    root = parent_session_dir.resolve()
    for key in ("diagnostic_path", "stderr_log_path"):
        raw = state.get(key)
        if not raw:
            continue
        path = Path(str(raw))
        if path.is_absolute() or ".." in path.parts:
            raise ValueError(f"Invalid ACP state file reference: {key}")
        try:
            (root / path).resolve().relative_to(root)
        except (OSError, RuntimeError, ValueError) as exc:
            raise ValueError(f"ACP state file reference escapes the session directory: {key}") from exc


_PAUSED_RECORD_REQUIRED_STR_FIELDS = ("invocation_id", "tool_name")
_PAUSED_RECORD_OPTIONAL_STR_FIELDS = (
    "parent_call_id",
    "parent_provider_call_id",
    "sub_agent_log_file",
    "created_at",
    "paused_at",
    "status",
    "failure_reason",
    "last_error",
)


def _paused_record_is_well_formed(data: dict[str, Any]) -> bool:
    """Reject records whose field types would crash restore consumers.

    Required identity fields must be non-empty strings; optional fields are
    string-or-absent (``inject_error_results_for_sub_agents`` uses parent
    call ids as dict keys, and the restore Warning joins tool names).
    """
    for field in _PAUSED_RECORD_REQUIRED_STR_FIELDS:
        value = data.get(field)
        if not isinstance(value, str) or not value:
            return False
    for field in _PAUSED_RECORD_OPTIONAL_STR_FIELDS:
        value = data.get(field)
        if value is not None and not isinstance(value, str):
            return False
    return True


def scan_bounded_json_files(
    directory: Path,
    *,
    max_files: int,
    max_total_bytes: int | None = None,
    suffixes: tuple[str, ...] = (".json",),
) -> tuple[list[Path], bool]:
    """Lazily enumerate at most *max_files* matching entries via ``os.scandir``.

    ``Path.glob``/``Path.iterdir`` are EAGER on CPython 3.14 — they run
    ``list(scandir)`` internally before yielding the first result, so wrapping
    them in ``islice`` does NOT bound enumeration and a same-uid flood of
    planted files still OOMs restore/fork during listing. ``os.scandir`` yields
    lazily; we break after *max_files* EXAMINED entries (not just matched ones,
    so a flood of non-matching names cannot spin the loop unbounded) and, when
    *max_total_bytes* is given, once the cumulative on-disk size of accepted
    files would exceed it — using the free ``st_size`` from ``scandir`` so a
    sparse/huge file is refused WITHOUT being read (bounding the bytes a later
    ``read_owner_verified_bounded`` will materialize). Returns the bounded
    subset sorted by path plus a truncated flag; the remainder (the
    filesystem-order tail beyond the caps) is deferred to a later pass.
    """
    paths: list[Path] = []
    truncated = False
    total = 0
    try:
        with os.scandir(directory) as iterator:
            examined = 0
            for entry in iterator:
                examined += 1
                if examined > max_files or (max_total_bytes is not None and total >= max_total_bytes):
                    truncated = True
                    break
                if not entry.name.endswith(suffixes):
                    continue
                if max_total_bytes is not None:
                    try:
                        total += min(entry.stat(follow_symlinks=False).st_size, MAX_SUB_AGENT_AUDIT_BYTES + 1)
                    except OSError:
                        continue
                paths.append(Path(entry.path))
    except FileNotFoundError, NotADirectoryError:
        return [], False
    except OSError:
        logger.warning("Failed to scan sub-agent directory %s", directory.name, exc_info=True)
        return [], False
    return sorted(paths), truncated


class SubAgentSessionArtifactService:
    """Profile-independent restore and orphan repair for one session directory."""

    def __init__(self, session_dir: Path) -> None:
        self._session_dir = session_dir

    def drain_paused_records(self) -> list[dict[str, Any]]:
        logs = self._log_files_by_invocation()
        records: list[dict[str, Any]] = []
        candidates: list[Path] = []
        for directory in (self._session_dir / "sub_agents", pending_dir(self._session_dir)):
            # Bound enumeration AND cumulative read BEFORE the per-record caps: a
            # same-uid flood of tiny (even corrupt) files must not materialize an
            # unbounded Path list, and a pile of huge/sparse records must not
            # force an unbounded read. Corrupt/consumed files leave the scan over
            # restores.
            paths, truncated = scan_bounded_json_files(
                directory,
                max_files=MAX_SCANNED_PENDING_FILES,
                max_total_bytes=MAX_PENDING_SCAN_TOTAL_BYTES,
            )
            if truncated:
                logger.warning(
                    "Paused sub-agent scan of %s truncated; a later restore drains the rest",
                    directory.name,
                )
            candidates.extend(paths)
        total_bytes = 0
        for index, path in enumerate(candidates):
            # A same-uid ACP child can plant hundreds of individually-valid
            # (<4 MiB) records; without an aggregate ceiling this loop would
            # hold them all in memory at once and OOM the restore. Stop past the
            # cap and DEFER the rest — candidate sizes are unknown until read, so
            # breaking is safer than scanning on. finalize removes the records
            # consumed this restore, letting the remainder drain later.
            if len(records) >= MAX_PENDING_RECORDS or total_bytes >= MAX_PENDING_RECORDS_TOTAL_BYTES:
                logger.warning(
                    "Deferring %d paused sub-agent record(s) past the per-restore cap (%d drained, %d bytes)",
                    len(candidates) - index,
                    len(records),
                    total_bytes,
                )
                break
            try:
                raw = _secure_read_bytes(path)
            except OSError, ValueError:
                # ValueError == over the per-record read ceiling (an over-cap
                # planted record); OSError == I/O failure. _archive drops the
                # over-cap file via its own ceiling and quarantines the rest.
                logger.warning("Failed to read paused sub-agent %s", path.name, exc_info=True)
                self._archive(path, "corrupt")
                continue
            try:
                data = json.loads(raw.decode("utf-8"))
            except UnicodeError, json.JSONDecodeError, ValueError, RecursionError:
                # RecursionError (not a ValueError) covers a deeply nested but
                # byte-bounded record whose json.loads blows the stack; quarantine
                # it like any other unreadable record rather than aborting restore.
                logger.warning("Failed to load paused sub-agent %s", path.name, exc_info=True)
                self._archive(path, "corrupt")
                continue
            if not isinstance(data, dict) or not _paused_record_is_well_formed(data):
                logger.warning("Quarantining malformed paused sub-agent record %s", path.name)
                self._archive(path, "corrupt")
                continue
            total_bytes += len(raw)
            data[SUB_AGENT_PENDING_SOURCE_PATH_KEY] = str(path)
            invocation_id = data.get("invocation_id")
            if not data.get("sub_agent_log_file") and isinstance(invocation_id, str) and invocation_id in logs:
                data["sub_agent_log_file"] = logs[invocation_id]
            records.append(data)
        records.sort(key=lambda record: str(record.get("created_at") or record.get("paused_at") or ""))
        return records

    def reconcile_orphaned_running_logs(
        self,
        parent_messages: list[Any],
        paused_records: list[dict[str, Any]],
    ) -> int:
        pending_by_id: dict[str, dict[str, Any]] = {}
        for record in paused_records:
            value = record.get("invocation_id")
            if isinstance(value, str) and value:
                pending_by_id.setdefault(value, record)
        pending_ids = set(pending_by_id)
        backlink_ids, backlink_files = _sub_agent_backlinks(parent_messages)
        repaired = 0
        directory = sessions_dir(self._session_dir)
        seen: set[Path] = set()
        # Reconcile each pending record's OWN audit by its deterministic path
        # FIRST, independent of the bounded scan below. The critical case — a
        # pending record beside a still-"running" audit (the process died in the
        # window between writing the pause record and stamping the audit
        # "paused") — MUST be repaired THIS restore: injection then deletes the
        # record and backlinks the log, after which the backlink-exemption would
        # shield the stale "running" status from every future repair. A flood
        # (or a legitimately huge session) can push that audit past the scan
        # cap, so scanning alone is not enough — target it directly.
        for invocation_id, record in pending_by_id.items():
            tool_name = record.get("tool_name")
            if not isinstance(tool_name, str) or not tool_name:
                continue
            # Only a chrys-generated 12-hex id can name a real audit; a pending
            # record is same-uid attacker-plantable, so reject anything else
            # before it reaches the path join below (defense in depth with the
            # sanitization inside make_sub_agent_log_file_name).
            if not validate_invocation_id(invocation_id):
                continue
            target = directory / make_sub_agent_log_file_name(tool_name, invocation_id)
            if target in seen:
                continue
            seen.add(target)
            if self._reconcile_running_audit(
                target,
                pending_by_id=pending_by_id,
                pending_ids=pending_ids,
                backlink_ids=backlink_ids,
                backlink_files=backlink_files,
            ):
                repaired += 1
        # Bound the audit scan by BOTH count and cumulative on-disk size: a flood
        # of planted audit files (each up to the 128 MiB ceiling) would otherwise
        # force reading+parsing every one before any repair. This best-effort
        # pass catches genuinely-orphaned running audits (no pending record).
        paths, truncated = scan_bounded_json_files(
            directory,
            max_files=MAX_SCANNED_AUDIT_FILES,
            max_total_bytes=MAX_AUDIT_SCAN_TOTAL_BYTES,
        )
        if truncated:
            logger.warning("Sub-agent audit reconcile scan truncated; a later restore repairs the rest")
        for path in paths:
            if path in seen:
                continue
            seen.add(path)
            if self._reconcile_running_audit(
                path,
                pending_by_id=pending_by_id,
                pending_ids=pending_ids,
                backlink_ids=backlink_ids,
                backlink_files=backlink_files,
            ):
                repaired += 1
        return repaired

    def _reconcile_running_audit(
        self,
        path: Path,
        *,
        pending_by_id: dict[str, dict[str, Any]],
        pending_ids: set[str],
        backlink_ids: set[str],
        backlink_files: set[str],
    ) -> bool:
        """Flip one still-"running" audit to "orphaned"; return True if repaired.

        Shared by the targeted (per pending record) and bounded-scan passes of
        reconcile_orphaned_running_logs.
        """
        envelope = self._read_audit_envelope_or_drop(path)
        if envelope is None:
            return False
        try:
            meta = envelope.get("meta")
            if isinstance(meta, dict) and meta.get("runner") == "acp":
                acp_state = envelope.get("acp_state")
                if not isinstance(acp_state, dict):
                    raise ValueError("ACP sub-agent log is missing ACP state")
                validate_acp_state_file_references(
                    acp_state,
                    parent_session_dir=self._session_dir,
                )
            if not isinstance(meta, dict) or meta.get("status") != "running":
                return False
            invocation_id = meta.get("invocation_id")
            if not isinstance(invocation_id, str):
                return False
            # A pending record beside a still-"running" audit means the
            # process died between the pause-record and audit-"paused"
            # writes — repair it NOW: the record is discarded during this
            # restore and its injected error result backlinks the log, so
            # both skips below would otherwise shield the stale status
            # forever. Backlinked audits without a pending record keep
            # their accounted-for exemption.
            if invocation_id not in pending_ids and (invocation_id in backlink_ids or path.name in backlink_files):
                return False
            now = _utc_now()
            meta.update(
                {
                    "status": "orphaned",
                    "failure_reason": "process_terminated",
                    "last_error": "process terminated before sub-agent wrote a terminal audit status",
                    "updated_at": now,
                    "ended_at": now,
                }
            )
            serialized = json.dumps(envelope, indent=2, ensure_ascii=False, allow_nan=False)
            try:
                atomic_write_owner_only_text(path, serialized)
            except OSError, ValueError:
                # The audit could not be flipped to "orphaned" this restore
                # (transient write failure — NOT an encode failure, which
                # stays with the outer handler). Flag its pending record so
                # finalize keeps it: deleting it would leave the still-
                # "running" audit backlinked and thus permanently exempt
                # (the skip above) from repair on every future restore.
                record = pending_by_id.get(invocation_id)
                if record is not None:
                    record[SUB_AGENT_REPAIR_FAILED_KEY] = True
                logger.warning(
                    "Failed to write reconciled sub-agent audit log %s; keeping its pending record for retry",
                    path.name,
                    exc_info=True,
                )
                return False
            return True
        except OSError, ValueError:
            logger.warning("Failed to reconcile sub-agent audit log %s", path.name, exc_info=True)
            return False

    def finalize_restored_paused_records(self, records: list[dict[str, Any]]) -> None:
        for record in records:
            if record.get(SUB_AGENT_REPAIR_FAILED_KEY):
                # reconcile could not repair this record's still-"running"
                # audit; keep the pending record in place so the next restore
                # retries (it stays in pending_ids, bypassing the backlink
                # exemption). Deleting it now would strand the audit forever.
                continue
            source = record.get(SUB_AGENT_PENDING_SOURCE_PATH_KEY)
            if not isinstance(source, str) or not source:
                continue
            path = Path(source)
            if record.get(SUB_AGENT_RESTORE_CONSUMED_KEY):
                secure_unlink_owner_verified(path)
            else:
                self._archive(path, "unmatched")

    def archive_unconsumed_restored_paused_records(self, records: list[dict[str, Any]]) -> None:
        for record in records:
            if record.get(SUB_AGENT_RESTORE_CONSUMED_KEY) or record.get(SUB_AGENT_REPAIR_FAILED_KEY):
                # Skip repair-failed records for the same reason as finalize:
                # archiving them out of pending would strand the "running" audit.
                continue
            source = record.get(SUB_AGENT_PENDING_SOURCE_PATH_KEY)
            if isinstance(source, str) and source:
                self._archive(Path(source), "unmatched")

    def _read_audit_envelope_or_drop(self, path: Path) -> dict[str, Any] | None:
        """Read one audit envelope, dropping it if it exceeds the read ceiling.

        An over-cap audit envelope can never be parsed, repaired, or forked;
        leaving it in place re-reads/re-allocates the whole file on every
        restore and would strand a stuck-"running" status forever. Over-cap
        files are dropped (mirroring the over-cap pending-record policy);
        malformed-but-bounded files are skipped in place for later inspection.
        """
        # Keep the bounded READ and the PARSE in separate try blocks: the
        # over-cap ValueError (from read_owner_verified_bounded) means DROP,
        # but a parse-time ValueError must NOT — json.loads raises a *plain*
        # ValueError for a valid-but-oversized integer (>4300 digits) and a
        # RecursionError (not a ValueError at all) for deeply nested input,
        # both of which can occur well under the byte ceiling. Fusing the two
        # would misclassify those as over-cap and delete bounded evidence, or
        # let RecursionError escape and abort hydration entirely.
        try:
            raw = _secure_read_bytes(path, max_bytes=MAX_SUB_AGENT_AUDIT_BYTES)
        except ValueError:
            # read_owner_verified_bounded raises a plain ValueError before any
            # decode/parse when the file is over the ceiling — drop it.
            logger.warning("Dropping over-sized sub-agent audit log %s", path.name)
            secure_unlink_owner_verified(path)
            return None
        except OSError:
            logger.warning("Failed to read sub-agent audit log %s", path.name, exc_info=True)
            return None
        try:
            envelope = json.loads(raw.decode("utf-8"))
        except UnicodeError, ValueError, RecursionError:
            # Malformed / oversized-int / deeply nested but byte-bounded:
            # preserve in place for later inspection (never delete).
            logger.warning("Skipping malformed sub-agent audit log %s", path.name, exc_info=True)
            return None
        return envelope if isinstance(envelope, dict) else None

    def _log_files_by_invocation(self) -> dict[str, str]:
        directory = sessions_dir(self._session_dir)
        found: dict[str, str] = {}
        duplicate: set[str] = set()
        # Bound the audit scan by BOTH count and cumulative on-disk size: this
        # builds a map by reading+parsing every audit file, so an unbounded
        # listing (or unbounded reads) would OOM/stall restore before drain's
        # per-record caps ever apply.
        paths, truncated = scan_bounded_json_files(
            directory,
            max_files=MAX_SCANNED_AUDIT_FILES,
            max_total_bytes=MAX_AUDIT_SCAN_TOTAL_BYTES,
        )
        if truncated:
            logger.warning("Sub-agent audit log-file scan truncated; some records may lack a log backlink this restore")
        for path in paths:
            envelope = self._read_audit_envelope_or_drop(path)
            if envelope is None:
                continue
            meta = envelope.get("meta")
            if isinstance(meta, dict) and meta.get("runner") == "acp":
                acp_state = envelope.get("acp_state")
                try:
                    if not isinstance(acp_state, dict):
                        raise ValueError("ACP sub-agent log is missing ACP state")
                    validate_acp_state_file_references(
                        acp_state,
                        parent_session_dir=self._session_dir,
                    )
                except ValueError:
                    continue
            invocation_id = meta.get("invocation_id") if isinstance(meta, dict) else None
            if not isinstance(invocation_id, str) or not invocation_id:
                continue
            if invocation_id in found:
                duplicate.add(invocation_id)
            else:
                found[invocation_id] = path.name
        for invocation_id in duplicate:
            found.pop(invocation_id, None)
        return found

    def _archive(self, path: Path, category: str) -> None:
        try:
            payload = _secure_read_bytes(path)
        except FileNotFoundError:
            return
        except ValueError:
            # Over the read ceiling: an over-cap file is exactly why we are
            # quarantining it — never copy it forward, just drop the source.
            logger.warning("Dropping over-sized sub-agent pending record %s", path.name)
            secure_unlink_owner_verified(path)
            return
        except OSError:
            logger.warning("Failed to read sub-agent pending record %s", path, exc_info=True)
            return
        try:
            destination_dir = pending_dir(self._session_dir) / category
            destination = destination_dir / path.name
            for _attempt in range(100):
                if not destination.exists():
                    break
                destination = destination_dir / f"{path.stem}-{secrets.token_hex(4)}{path.suffix}"
            atomic_write_owner_only_bytes(destination, payload)
            secure_unlink_owner_verified(path)
        except FileNotFoundError:
            return
        except OSError:
            logger.warning("Failed to archive sub-agent pending record %s", path, exc_info=True)


def _secure_read_bytes(path: Path, *, max_bytes: int = _MAX_PENDING_RECORD_BYTES) -> bytes:
    # Owner-verified, not owner-only: pre-existing session artifacts predate
    # the owner-only writers and must stay readable after upgrade.
    return read_owner_verified_bounded(path, max_bytes=max_bytes)


def _sub_agent_backlinks(parent_messages: list[Any]) -> tuple[set[str], set[str]]:
    invocation_ids: set[str] = set()
    log_files: set[str] = set()
    for message in parent_messages:
        for content in getattr(message, "contents", ()):
            properties = getattr(content, "additional_properties", {})
            metadata = properties.get(TOOL_RESULT_METADATA_KEY) if isinstance(properties, dict) else None
            if not isinstance(metadata, dict):
                continue
            invocation_id = metadata.get("sub_agent_invocation_id")
            if isinstance(invocation_id, str) and invocation_id:
                invocation_ids.add(invocation_id)
            log_file = metadata.get("sub_agent_log_file")
            if isinstance(log_file, str) and log_file:
                log_files.add(log_file)
    return invocation_ids, log_files


_MAX_REFERENCE_WALK_DEPTH = 200


def collect_sub_agent_log_file_references(root: Any) -> set[str]:
    """Collect every ``sub_agent_log_file`` value anywhere in a parsed envelope.

    Used by the fork copier to guarantee every audit the forked history
    references is carried forward, so a bounded artifact scan can never strand
    a dangling reference in a legitimately large session. The walk is
    schema-agnostic (it keys on the field name, not the message/content
    nesting, so serialization changes cannot silently drop references),
    iterative, and depth-bounded so a deeply nested — but ``json``-parseable —
    envelope cannot exhaust the interpreter stack.
    """
    references: set[str] = set()
    stack: list[tuple[int, Any]] = [(0, root)]
    while stack:
        depth, node = stack.pop()
        if depth > _MAX_REFERENCE_WALK_DEPTH:
            continue
        if isinstance(node, dict):
            value = node.get("sub_agent_log_file")
            if isinstance(value, str) and value:
                references.add(value)
            stack.extend((depth + 1, child) for child in node.values() if isinstance(child, (dict, list)))
        elif isinstance(node, list):
            stack.extend((depth + 1, child) for child in node if isinstance(child, (dict, list)))
    return references


def is_safe_sub_agent_log_basename(name: str) -> bool:
    """True iff *name* is a bare ``.json`` audit basename safe to join to a dir.

    A ``sub_agent_log_file`` reference is chrys-authored, but the fork joins it
    to the parent's owner-writable sessions dir, so reject anything that is not
    a plain in-directory filename (no separators, no ``.``/``..``, no leading
    dot) before using it as a copy source.
    """
    if not name or not name.endswith(".json"):
        return False
    if name.startswith("."):
        return False
    if name in (".", ".."):
        return False
    return name == os.path.basename(name) and "/" not in name and "\\" not in name


def _atomic_write_json(path: Path, envelope: dict[str, Any]) -> None:
    # A surrogateescaped workspace path (meta primary_cwd/working_dirs) or any
    # other field is made write-safe at the sink: atomic_write_owner_only_text
    # encodes with backslashreplace, so this cannot crash on a lone surrogate.
    atomic_write_owner_only_text(
        path,
        json.dumps(envelope, indent=2, ensure_ascii=False, allow_nan=False),
    )


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()
