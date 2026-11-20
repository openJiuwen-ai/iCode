# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Durable outbox for ``delivery: durable`` hooks.

Job lifecycle::

    pending/{job_id}.json   ← written before subprocess spawn
       │  (subprocess runs)
       ├─→  done/{job_id}.json    on exit_code == 0
       └─→  failed/{job_id}.json  on non-zero / timeout

Each job file carries the hook id, event payload, retry count, and
timestamps. On startup a terminal record takes precedence over any leftover
pending file for the same job. The manager re-spawns unfinished entries older
than ``HookSettings.outbox_retry_age_seconds`` that haven't exceeded
``outbox_max_retries``. Entries that exceed the cap move directly to
``failed/`` with no retry — the user is expected
to inspect them, fix the hook, and either delete the file or move it
back to ``pending/`` so no terminal copy remains.

Semantics are at-least-once.  Hooks marked ``delivery: durable`` must
be idempotent — if chrys crashes after the subprocess exits 0 but
before the move-to-done renames, the next startup will re-run the
hook.  This matches every "outbox pattern" I've seen and is the
trade-off for not requiring a daemon.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final
from uuid import uuid4

from chrys.foundation.models.workdir_migration import source_edit
from chrys.foundation.platform.files import replace_with_retry, secure_open_regular_binary

logger = logging.getLogger(__name__)


PENDING = "pending"
DONE = "done"
FAILED = "failed"
MAX_TERMINAL_RECORD_BYTES: Final = 64 * 1024 * 1024


def has_terminal_record(root: Path, job_id: str) -> bool:
    """Check for a valid terminal outcome without touching the outbox."""
    terminal, _ = _terminal_status(root, f"{job_id}.json", job_id)
    return terminal


def _terminal_status(root: Path, name: str, job_id: str) -> tuple[bool, str | None]:
    diagnostic = None
    for folder in (DONE, FAILED):
        terminal = root / folder / name
        try:
            _read_terminal_record(terminal, job_id)
        except FileNotFoundError:
            continue
        except OSError as error:
            diagnostic = diagnostic or f"Could not read terminal record {terminal}: {error}"
            continue
        except (ValueError, RecursionError) as error:
            diagnostic = diagnostic or f"Ignoring invalid terminal record {terminal}: {error}"
            continue
        return True, diagnostic
    return False, diagnostic


def _read_terminal_bytes(path: Path) -> bytes:
    with secure_open_regular_binary(path) as stream:
        raw = stream.read(MAX_TERMINAL_RECORD_BYTES + 1)
    if len(raw) > MAX_TERMINAL_RECORD_BYTES:
        raise ValueError("Terminal record exceeds its size limit")
    return raw


def _read_terminal_record(path: Path, job_id: str) -> tuple[bytes, dict[str, Any]]:
    raw = _read_terminal_bytes(path)
    data = json.loads(raw)
    if not isinstance(data, dict) or data.get("job_id") != job_id or data.get("state") != path.parent.name:
        raise ValueError("Terminal record has an invalid job ID or state")
    if not isinstance(data.get("payload"), dict) or not all(
        isinstance(data.get(key), str) for key in ("hook_id", "event", "created_at")
    ):
        raise ValueError("Terminal record is missing valid job fields")
    return raw, data


@dataclass
class OutboxJob:
    """On-disk shape of an outbox job.

    Field names are part of the persisted contract — renaming one is a
    breaking change to existing pending entries.  ``state`` is implicit
    in the parent directory; we still write it into the JSON for
    forensic clarity (you can ``cat`` a file in ``failed/`` and see what
    chrys thought of it).
    """

    job_id: str
    hook_id: str
    event: str
    payload: dict[str, Any]
    created_at: str
    hook_source: str = ""
    """Hook configuration layer that created the job, currently
    ``"workdir"`` or ``"global"``. Empty for legacy jobs written before
    working directory/global layering existed.
    """
    hook_source_path: str = ""
    """Absolute hooks config file path that created the job.

    This disambiguates working directory hooks sharing the global outbox across
    workspaces. Empty for legacy jobs written before source paths were
    persisted.
    """
    state: str = PENDING
    retries: int = 0
    last_started_at: str = ""
    last_finished_at: str = ""
    exit_code: int | None = None
    error: str = ""
    stderr_tail: str = ""
    """Last ~1KB of the hook's stderr, captured when the job moves to
    ``failed/``.  Useful for debugging from the file alone.
    """

    extra: dict[str, Any] = field(default_factory=dict)
    """Forward-compat slot — unknown fields read off disk land here so
    older chrys builds don't drop newer keys on round-trip.
    """


class Outbox:
    """Filesystem-backed durable job store.

    Operations are blocking I/O but each one is small (a few KB) so we
    don't bother with an executor.  The manager calls these from a
    single event loop; serialization is fine.
    """

    def __init__(self, root: Path) -> None:
        self._root = root
        for sub in (PENDING, DONE, FAILED):
            (root / sub).mkdir(parents=True, exist_ok=True)

    @property
    def root(self) -> Path:
        return self._root

    # ── pending writes ───────────────────────────────────────────────

    def write_pending(
        self,
        *,
        hook_id: str,
        event: str,
        payload: dict[str, Any],
        hook_source: str = "",
        hook_source_path: str = "",
    ) -> OutboxJob:
        """Create a fresh pending job for *hook_id* / *event* / *payload*.

        Called immediately before subprocess spawn so a crash between
        this point and ``mark_done`` / ``mark_failed`` leaves the entry
        recoverable on next startup.
        """
        job_id = uuid4().hex[:16]
        job = OutboxJob(
            job_id=job_id,
            hook_id=hook_id,
            event=event,
            payload=payload,
            created_at=_now_iso(),
            hook_source=hook_source,
            hook_source_path=hook_source_path,
        )
        self._write(job)
        return job

    def mark_started(self, job: OutboxJob) -> bool:
        """Stamp the pending job, returning whether it is still eligible to run."""
        if self.skip_pending(job.job_id):
            return False
        job.last_started_at = _now_iso()
        job.retries += 1
        self._write(job)
        # Completion may have landed while the pending write was in progress.
        return not self.skip_pending(job.job_id)

    def mark_done(self, job: OutboxJob, *, exit_code: int) -> None:
        """Move the job to ``done/``."""
        job.state = DONE
        job.last_finished_at = _now_iso()
        job.exit_code = exit_code
        self._move(job, src=PENDING, dst=DONE)

    def mark_failed(
        self,
        job: OutboxJob,
        *,
        exit_code: int | None,
        error: str = "",
        stderr_tail: str = "",
    ) -> None:
        """Move the job to ``failed/``."""
        job.state = FAILED
        job.last_finished_at = _now_iso()
        job.exit_code = exit_code
        job.error = error
        job.stderr_tail = stderr_tail[-1024:]
        self._move(job, src=PENDING, dst=FAILED)

    # ── recovery (called once at startup) ────────────────────────────

    def skip_pending(self, job_id: str) -> bool:
        """Skip a terminal job and discard its leftover pending file when possible."""
        skip, _ = self._skip_pending_path(self._root / PENDING / f"{job_id}.json")
        return skip

    def _skip_pending_path(self, pending: Path) -> tuple[bool, str | None]:
        """Return the skip verdict and a diagnostic for the recovery scan."""
        skip, diagnostic = _terminal_status(self._root, pending.name, pending.stem)
        if skip:
            with contextlib.suppress(OSError):
                pending.unlink()
                logger.warning("Removed pending hook record %s because a terminal record already exists", pending)
        return skip, diagnostic

    def list_stale_pending(self, *, retry_age_seconds: float) -> list[OutboxJob]:
        """Return pending jobs whose ``last_started_at`` is older than
        *retry_age_seconds* (or that were never started).

        Younger entries are treated as "another chrys is working on it"
        and skipped; if that other chrys later disappears they'll show
        up as stale on a subsequent startup.
        """
        out: list[OutboxJob] = []
        now = time.time()
        for path in sorted((self._root / PENDING).glob("*.json")):
            skip, diagnostic = self._skip_pending_path(path)
            if diagnostic is not None:
                logger.warning("%s", diagnostic)
            if skip:
                continue
            job = self._read(path)
            if job is not None and job.hook_source not in {"", "global", "workdir"}:
                continue
            if job is None:
                continue
            anchor = job.last_started_at or job.created_at
            if not anchor:
                out.append(job)
                continue
            # ISO-8601 strings written by ``_now_iso`` end in ``Z``; strip
            # it before parsing because ``datetime.fromisoformat`` accepts
            # ``+00:00`` but not the ``Z`` shorthand on every Python build.
            iso = anchor.removesuffix("Z")
            try:
                anchor_ts = datetime.fromisoformat(iso).replace(tzinfo=UTC).timestamp()
            except ValueError:
                out.append(job)
                continue
            if now - anchor_ts >= retry_age_seconds:
                out.append(job)
        return out

    def force_fail(self, job: OutboxJob, *, reason: str) -> None:
        """Move a pending job to ``failed/`` without spawning anything.

        Used for entries that exceeded ``outbox_max_retries`` — there's
        nothing to wait for, just record the failure and move on.
        """
        if self.skip_pending(job.job_id):
            return
        job.state = FAILED
        job.last_finished_at = _now_iso()
        job.error = reason
        self._move(job, src=PENDING, dst=FAILED)

    # ── internals ────────────────────────────────────────────────────

    def _write(self, job: OutboxJob) -> None:
        data = asdict(job)
        path = self._root / PENDING / f"{job.job_id}.json"
        _write_json_atomic_owner_only(path, data)

    def _move(self, job: OutboxJob, *, src: str, dst: str) -> None:
        src_path = self._root / src / f"{job.job_id}.json"
        dst_path = self._root / dst / f"{job.job_id}.json"
        data = asdict(job)
        source_edit(data, "hook_source")
        # Publish the outcome before removing the pending entry.
        _write_json_atomic_owner_only(dst_path, data)
        with contextlib.suppress(FileNotFoundError):
            src_path.unlink()

    def _read(self, path: Path) -> OutboxJob | None:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Failed to read outbox job %s: %s", path, exc)
            return None
        if not isinstance(data, dict) or "job_id" not in data:
            logger.warning("Outbox job %s is malformed; ignoring", path)
            return None
        source_edit(data, "hook_source")
        all_fields = set(OutboxJob.__dataclass_fields__)
        valid_keys = all_fields - {"extra"}
        extra = {k: v for k, v in data.items() if k not in all_fields}
        # Honour any prior ``extra`` dict round-tripped through asdict()
        # so multiple loads in a row don't lose unknown keys.
        prior_extra = data.get("extra")
        if isinstance(prior_extra, dict):
            for k, v in prior_extra.items():
                extra.setdefault(k, v)
        known = {k: v for k, v in data.items() if k in valid_keys}
        # Guard against missing required fields (file written by an
        # older or future chrys); fall back to safe defaults.
        known.setdefault("hook_id", "")
        known.setdefault("event", "")
        known.setdefault("payload", {})
        known.setdefault("created_at", "")
        try:
            return OutboxJob(**known, extra=extra)
        except TypeError as exc:
            logger.warning("Outbox job %s could not be loaded: %s", path, exc)
            return None


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _write_json_atomic_owner_only(path: Path, data: dict[str, Any]) -> None:
    """Atomically write a JSON file readable only by the current user where supported."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_tmp = tempfile.mkstemp(prefix=f"{path.name}.", suffix=".tmp", dir=path.parent)
    tmp = Path(raw_tmp)
    try:
        _set_fd_owner_only(fd)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        replace_with_retry(tmp, path)
        _chmod_owner_only(path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.close(fd)
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)
        raise


def _set_fd_owner_only(fd: int) -> None:
    with contextlib.suppress(AttributeError, OSError):
        os.fchmod(fd, 0o600)


def _chmod_owner_only(path: Path) -> None:
    with contextlib.suppress(OSError):
        os.chmod(path, 0o600)
