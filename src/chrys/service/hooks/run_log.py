# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The hook runs since the process started, across sessions, for the TUI's Hooks panel.

One log lives as long as the engine that owns it; every main-session hook
manager the engine builds writes its runs here, so session switches and
manager replacements keep them. Writes are synchronous; the manager tells its
observer after each one. Details (payload, result file, output) stay in this
process's memory only.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from chrys.foundation.models.hook_runs import (
    HookResultFileStatus,
    HookRunApplied,
    HookRunDetail,
    HookRunEffect,
    HookRunIO,
    HookRunRecord,
)

if TYPE_CHECKING:
    from chrys.foundation.models.hook_confirmations import HookConfirmationOutcome
    from chrys.service.hooks.schema import HookConfig

HOOK_RUN_LOG_MAX_ENDED = 100
"""Ended runs kept; queued and running ones are always kept, and not counted."""

HOOK_RUN_LOG_MAX_DETAIL_CHARS = 16 * 1024 * 1024
"""What the kept details may hold together; the oldest ended runs' details go first."""

HOOK_RUN_TEXT_MAX_BYTES = 64 * 1024
"""What a run keeps of its payload, of the tool arguments it changed and of why it blocked, in UTF-8 bytes.

The block reason outlives the other details (``_without_details``), so the
ended runs' reasons always fit the detail budget. Every other text a run keeps
is bounded where it is made: the output by its capture, the result file and
what iCode took from it by the result file's limit, its JSON by
``indented_json``.
"""

_INDENT_SLACK_CHARS = 4096
UNWRITABLE_JSON = "(nested too deeply to write as JSON)"
"""What ``indented_json`` gives for a value nested deeper than the encoder writes."""


@dataclass(slots=True)
class _Entry:
    record: HookRunRecord
    detail: HookRunDetail


def _detail_size(detail: HookRunDetail) -> int:
    """Every text *detail* keeps, in characters: what the detail budget counts."""
    size = 0
    io = detail.io
    if io is not None:
        size += len(io.payload) + len(io.decision) + len(io.stdout) + len(io.stderr)
    applied = detail.applied
    if applied is not None:
        size += (
            len(applied.block_reason)
            + len(applied.args)
            + len(applied.reminder)
            + len(applied.extra_context)
            + len(applied.result_override)
        )
    return size


def _without_details(detail: HookRunDetail) -> HookRunDetail:
    """*detail* without what it read and wrote, and without the texts iCode took from it.

    The block reason stays: a run that blocked an action still says why.
    """
    applied = detail.applied
    if applied is not None:
        applied = replace(applied, args="", args_size=0, reminder="", extra_context="", result_override="")
    return replace(detail, io=None, applied=applied)


def indented_json(value: object) -> str:
    """*value* as JSON, indented unless that would swell it past twice its compact size, then compact.

    Indenting grows with the nesting depth on every line, so a small deeply
    nested value would indent into a huge text. Never raises: parsed JSON can
    nest deeper than the encoder writes, and reads ``UNWRITABLE_JSON`` then.
    """
    try:
        compact = json.dumps(value, ensure_ascii=False, default=str)
    except RecursionError:
        return UNWRITABLE_JSON
    limit = 2 * len(compact) + _INDENT_SLACK_CHARS
    parts: list[str] = []
    size = 0
    try:
        # The pure-Python encoder streams, so a swelling text is given up before it is built.
        for part in json.JSONEncoder(ensure_ascii=False, default=str, indent=2).iterencode(value):
            size += len(part)
            if size > limit:
                return compact
            parts.append(part)
    except RecursionError:
        return compact
    return "".join(parts)


def _clip_text(text: str, limit: int = HOOK_RUN_TEXT_MAX_BYTES) -> tuple[str, int]:
    """*text* cut to at most *limit* UTF-8 bytes, and its full size in them.

    A cut drops the character it splits; lone surrogates, which a path that is not
    UTF-8 holds, are kept as they are.
    """
    if text.isascii():
        return text[:limit], len(text)
    data = text.encode("utf-8", "surrogatepass")
    if len(data) <= limit:
        return text, len(data)
    cut = limit
    # Back to the first byte of the character the limit splits: its others are 10xxxxxx.
    while cut and data[cut] & 0xC0 == 0x80:
        cut -= 1
    return data[:cut].decode("utf-8", "surrogatepass"), len(data)


class HookRunLog:
    """The hook runs of every session since the process started; the latest ended ones only."""

    def __init__(
        self,
        *,
        max_ended: int = HOOK_RUN_LOG_MAX_ENDED,
        max_detail_chars: int = HOOK_RUN_LOG_MAX_DETAIL_CHARS,
    ) -> None:
        self._max_ended = max_ended
        self._max_detail_chars = max_detail_chars
        self._entries: dict[str, _Entry] = {}
        self._ended: list[str] = []
        """Ended run ids, oldest end first."""
        self._detail_chars = 0
        """``_detail_size`` of every kept detail; only ``_set_detail`` and ``_trim_ended`` change it."""
        self._registered = 0
        """Runs registered so far: the next run's number is one more."""
        self._dropped = 0
        """Ended runs dropped past the cap."""

    # ── reads ────────────────────────────────────────────────────────

    def records(self) -> tuple[HookRunRecord, ...]:
        """The runs kept, oldest first, by number: a run keeps its place as it starts and ends."""
        # Registered in number order, and a dict keeps that order.
        return tuple(entry.record for entry in self._entries.values())

    @property
    def dropped(self) -> int:
        """Ended runs no longer kept because more than the cap ended since."""
        return self._dropped

    def record(self, run_id: str) -> HookRunRecord | None:
        entry = self._entries.get(run_id)
        return entry.record if entry is not None else None

    def detail(self, run_id: str) -> HookRunDetail | None:
        entry = self._entries.get(run_id)
        return entry.detail if entry is not None else None

    # ── writes ───────────────────────────────────────────────────────

    def register(
        self,
        *,
        run_id: str,
        session_id: str,
        hook: HookConfig,
        source: str,
        source_path: str,
        payload_text: str,
        tool_name: str,
        tool_call_id: str,
        sub_agent: str,
        retry: bool,
    ) -> None:
        """Add a run queued for a slot."""
        now = time.monotonic()
        self._registered += 1
        record = HookRunRecord(
            run_id=run_id,
            session_id=session_id,
            hook_id=hook.id,
            hook_event=str(hook.event),
            execution_mode=hook.execution.mode,
            detach=hook.execution.detach,
            tool_name=tool_name,
            sub_agent=sub_agent,
            retry=retry,
            registered_at=now,
            started_at=None,
            ended_at=None,
            registered_wall=time.time(),
            state="queued",
            outcome=None,
            exit_code=None,
            effect=None,
            number=self._registered,
        )
        run = hook.run
        if run.type == "script":
            argv: tuple[str, ...] = (run.path, *run.args)
        elif run.type == "command":
            argv = (*run.argv, *run.args)
        else:
            argv = (run.shell,)
        payload, payload_size = _clip_text(payload_text)
        io = HookRunIO(
            payload=payload,
            payload_size=payload_size,
            result_file=None,
            decision="",
            stdout="",
            stderr="",
            stdout_dropped=0,
            stderr_dropped=0,
        )
        detail = HookRunDetail(
            record=record,
            tool_call_id=tool_call_id,
            timeout_seconds=hook.execution.timeout_seconds,
            on_error=hook.execution.on_error,
            delivery=hook.execution.delivery,
            source=source,
            source_path=source_path,
            run_type=run.type,
            run_argv=argv,
            run_cwd=run.cwd,
            run_env_names=tuple(run.env),
            log_path="",
            shared_log=False,
            io=io,
            applied=None,
        )
        entry = _Entry(record, detail)
        self._entries[run_id] = entry
        self._detail_chars += _detail_size(detail)
        self._trim_details()

    def mark_running(self, run_id: str) -> bool:
        """Mark a queued run as holding its slot; False when it is not queued here."""
        entry = self._entries.get(run_id)
        if entry is None or entry.record.state != "queued":
            return False
        self._set_record(entry, replace(entry.record, state="running", started_at=time.monotonic()))
        return True

    def finish(
        self,
        run_id: str,
        *,
        outcome: str,
        exit_code: int | None = None,
        effect: HookRunEffect | None = None,
        applied: HookRunApplied | None = None,
        result_file: HookResultFileStatus | None = None,
        decision: str = "",
        stdout: str = "",
        stderr: str = "",
        stdout_dropped: int = 0,
        stderr_dropped: int = 0,
        log_path: str = "",
    ) -> bool:
        """End a queued or running run; False when it is not open here.

        Changed arguments and a block reason past ``HOOK_RUN_TEXT_MAX_BYTES`` are cut, as the payload is.
        """
        entry = self._entries.get(run_id)
        if entry is None or entry.record.state == "ended":
            return False
        record = replace(
            entry.record,
            state="ended",
            ended_at=time.monotonic(),
            outcome=outcome,
            exit_code=exit_code,
            effect=effect,
            confirmation="pending" if effect == "asked" else None,
        )
        io = entry.detail.io
        if io is not None:
            io = replace(
                io,
                result_file=result_file,
                decision=decision,
                stdout=stdout,
                stderr=stderr,
                stdout_dropped=stdout_dropped,
                stderr_dropped=stderr_dropped,
            )
        applied = applied if applied is not None else HookRunApplied()
        if applied.args:
            args, size = _clip_text(applied.args)
            applied = replace(applied, args=args, args_size=size if args != applied.args else 0)
        if applied.block_reason:
            applied = replace(applied, block_reason=_clip_text(applied.block_reason)[0])
        shared = False
        if log_path:
            for key, other in self._entries.items():
                if key != run_id and other.detail.log_path == log_path:
                    shared = True
                    if not other.detail.shared_log:
                        self._set_detail(other, replace(other.detail, shared_log=True))
        entry.record = record
        self._set_detail(
            entry, replace(entry.detail, record=record, io=io, applied=applied, log_path=log_path, shared_log=shared)
        )
        self._ended.append(run_id)
        self._trim_ended()
        self._trim_details()
        return True

    def confirm(self, run_id: str, outcome: HookConfirmationOutcome) -> bool:
        """Set what came of an ``asked`` run's question; False when the run is not kept or did not ask."""
        entry = self._entries.get(run_id)
        if entry is None or entry.record.confirmation != "pending":
            return False
        self._set_record(entry, replace(entry.record, confirmation=outcome))
        return True

    def reject_call(self, run_id: str) -> bool:
        """Mark that the call was rejected for *run_id*'s argument change; False when the run is not kept or not ended."""
        entry = self._entries.get(run_id)
        if entry is None or entry.record.state != "ended" or entry.record.call_rejected:
            return False
        self._set_record(entry, replace(entry.record, call_rejected=True))
        return True

    def _set_record(self, entry: _Entry, record: HookRunRecord) -> None:
        entry.record = record
        self._set_detail(entry, replace(entry.detail, record=record))

    def _set_detail(self, entry: _Entry, detail: HookRunDetail) -> None:
        """Every change to a kept detail goes through here, which keeps the budget's count exact."""
        self._detail_chars += _detail_size(detail) - _detail_size(entry.detail)
        entry.detail = detail

    def _trim_ended(self) -> None:
        while len(self._ended) > self._max_ended:
            entry = self._entries.pop(self._ended.pop(0))
            self._detail_chars -= _detail_size(entry.detail)
            self._dropped += 1

    def _trim_details(self) -> None:
        """Drop the oldest ended runs' details until the rest fit; queued and running runs keep theirs."""
        for run_id in self._ended:
            if self._detail_chars <= self._max_detail_chars:
                return
            entry = self._entries[run_id]
            if entry.detail.io is not None:
                self._set_detail(entry, _without_details(entry.detail))
