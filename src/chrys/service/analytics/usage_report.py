# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Token usage across every saved session, behind ``/usage``.

Each session's ``trajectory/events.jsonl`` holds what that session itself spent:
a fork's log starts empty, so an inherited turn counts once, and a rolled-back
attempt counts because it was spent all the same. A deleted session's usage goes
with its log, and an external ACP agent's model calls never reach it.

"New" tokens are what a request added on top of the provider's cache: its input
less cache reads, plus its output (reasoning included).
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from math import ceil
from typing import TYPE_CHECKING, Final, Protocol

from chrys.foundation.platform import get_platform
from chrys.foundation.trajectory.envelope import TrajectoryEvent
from chrys.foundation.trajectory.event_types import EventType, ExchangeOutcome
from chrys.foundation.trajectory.fingerprint import DOMAIN_WORKSPACE, fingerprint_text
from chrys.foundation.trajectory.keys import load_or_create_fingerprint_key
from chrys.service.analytics.reader import plain_int_value, scan_event_types
from chrys.service.trajectory.tombstone import pending_delete_intents

if TYPE_CHECKING:
    from pathlib import Path
    from threading import Event

logger = logging.getLogger(__name__)

_TRAJECTORY_LOG: Final = ("trajectory", "events.jsonl")
_USAGE_EVENT_TYPES: Final = frozenset(
    {EventType.MODEL_EXCHANGE_FINISHED, EventType.SESSION_STARTED, EventType.TURN_STARTED}
)
_FAILED_OUTCOMES: Final = frozenset({ExchangeOutcome.ERROR, ExchangeOutcome.STALLED})


@dataclass(frozen=True, slots=True)
class UsageCall:
    """One model request, reduced to what the usage view shows."""

    day: date
    """The local calendar day the request ended on."""
    hour: int
    """The local hour of that day, 0 to 23."""
    model: str
    """The model that answered; empty when none did and the request does not say."""
    source: str
    """``actor.role``: the main agent, a sub-agent, a workflow node or a side call."""
    project: str
    """The working folder the request ran in; empty when no saved session still names it."""
    new_input: int
    cache_read: int
    output: int
    duration_ms: int
    first_chunk_ms: int | None
    reported: bool
    """Whether the provider reported usage for it; a request cut off first did not."""
    failed: bool


@dataclass(slots=True)
class UsageTotals:
    """Sums over any set of calls."""

    calls: int = 0
    failed: int = 0
    unreported: int = 0
    new_input: int = 0
    cache_read: int = 0
    output: int = 0
    duration_ms: int = 0
    speed_output: int = 0
    speed_ms: int = 0

    def add(self, call: UsageCall) -> None:
        self.calls += 1
        self.failed += call.failed
        self.unreported += not call.reported
        self.new_input += call.new_input
        self.cache_read += call.cache_read
        self.output += call.output
        self.duration_ms += call.duration_ms
        if call.first_chunk_ms is not None:
            streaming_ms = call.duration_ms - call.first_chunk_ms
            # A failed call's output is what got counted before it broke off, often a placeholder.
            if call.output and streaming_ms > 0 and not call.failed:
                self.speed_output += call.output
                self.speed_ms += streaming_ms

    @property
    def new_tokens(self) -> int:
        return self.new_input + self.output

    @property
    def cache_hit(self) -> float | None:
        """The share of input the provider served from its cache; ``None`` with no input."""
        total = self.new_input + self.cache_read
        return self.cache_read / total if total else None

    @property
    def speed(self) -> float | None:
        """Output tokens per second from each stream's first chunk to its end; ``None`` with no stream to time.

        Measured over all of a model's streams together: the output includes
        reasoning, and so does the time, whereas counting from the first answer
        text would credit the thinking to a time that excludes it.
        """
        return self.speed_output / (self.speed_ms / 1000) if self.speed_ms else None


@dataclass(slots=True)
class UsageSummary:
    """One date range's totals, overall and by each grouping."""

    total: UsageTotals = field(default_factory=UsageTotals)
    models: defaultdict[str, UsageTotals] = field(default_factory=lambda: defaultdict(UsageTotals))
    sources: defaultdict[str, UsageTotals] = field(default_factory=lambda: defaultdict(UsageTotals))
    projects: defaultdict[str, UsageTotals] = field(default_factory=lambda: defaultdict(UsageTotals))


@dataclass(frozen=True, slots=True)
class UsageReport:
    """Every recorded call at one moment."""

    calls: tuple[UsageCall, ...]
    today: date
    unreadable_logs: int = 0
    new_tokens_by_day: Mapping[date, int] = field(default_factory=dict)
    new_tokens_by_hour: Mapping[int, int] = field(default_factory=dict)
    """Today's new tokens by local hour."""

    @property
    def first_day(self) -> date | None:
        return min(self.new_tokens_by_day, default=None)

    def summarize(self, start: date, end: date) -> UsageSummary:
        """The calls from *start* to *end*, both included."""
        summary = UsageSummary()
        for call in self.calls:
            if start <= call.day <= end:
                summary.total.add(call)
                summary.models[call.model].add(call)
                summary.sources[call.source].add(call)
                summary.projects[call.project].add(call)
        return summary


def week_start(day: date) -> date:
    """The Monday of *day*'s week."""
    return day - timedelta(days=day.weekday())


def percentile(values: Iterable[int], fraction: float) -> int | None:
    """The nearest-rank percentile of *values*; ``None`` with no value."""
    ordered = sorted(values)
    return ordered[max(0, ceil(len(ordered) * fraction) - 1)] if ordered else None


class UsageSessionStore(Protocol):
    """What the report needs from the session store."""

    @property
    def sessions_root(self) -> Path: ...
    def session_folder_cwds(self) -> dict[str, str]: ...


def load_usage_report(store: UsageSessionStore | None, cancel_event: Event | None = None) -> UsageReport:
    """Read every session's usage; blocking, so it runs off the event loop."""
    if store is None:
        return UsageReport(calls=(), today=datetime.now().astimezone().date())
    return scan_usage(
        store.sessions_root,
        session_cwds=store.session_folder_cwds(),
        fingerprint_key=_fingerprint_key(),
        cancel_event=cancel_event,
    )


def _fingerprint_key() -> bytes | None:
    try:
        return load_or_create_fingerprint_key(get_platform().config_dir)
    except OSError:
        logger.warning("Usage report: the trajectory fingerprint key is unavailable", exc_info=True)
        return None


def scan_usage(
    sessions_root: Path,
    *,
    session_cwds: Mapping[str, str],
    fingerprint_key: bytes | None,
    today: date | None = None,
    cancel_event: Event | None = None,
) -> UsageReport:
    """Read the usage of every session folder under *sessions_root*.

    *session_cwds* names each saved session's working folder. With
    *fingerprint_key*, a request belongs to the folder its turn ran in, as the
    log fingerprints it, rather than the one its session has moved on to.
    """
    cwd_by_fingerprint = (
        {fingerprint_text(fingerprint_key, DOMAIN_WORKSPACE, cwd): cwd for cwd in set(session_cwds.values()) if cwd}
        if fingerprint_key is not None
        else {}
    )
    calls: list[UsageCall] = []
    unreadable = 0
    deleted = pending_delete_intents(sessions_root)
    for folder in sorted(sessions_root.iterdir()):
        # Dot-prefixed entries (locks, caches, deleted sessions) are never sessions.
        if folder.name.startswith(".") or folder.name in deleted:
            continue
        log = folder.joinpath(*_TRAJECTORY_LOG)
        collector = _SessionUsage(session_cwds.get(folder.name, ""), cwd_by_fingerprint, calls.append)
        try:
            scan_event_types(log, _USAGE_EVENT_TYPES, collector.take, cancel_event=cancel_event)
        except FileNotFoundError, NotADirectoryError:
            continue
        except OSError:
            logger.warning("Usage report: cannot read %s", log, exc_info=True)
            unreadable += 1
    today = today or datetime.now().astimezone().date()
    new_tokens_by_day: defaultdict[date, int] = defaultdict(int)
    new_tokens_by_hour: defaultdict[int, int] = defaultdict(int)
    for call in calls:
        new_tokens_by_day[call.day] += call.new_input + call.output
        if call.day == today:
            new_tokens_by_hour[call.hour] += call.new_input + call.output
    return UsageReport(
        calls=tuple(calls),
        today=today,
        unreadable_logs=unreadable,
        new_tokens_by_day=dict(new_tokens_by_day),
        new_tokens_by_hour=dict(new_tokens_by_hour),
    )


class _SessionUsage:
    """Turns one session's log into calls, tracking the working folder each pass ran in."""

    def __init__(
        self, session_cwd: str, cwd_by_fingerprint: Mapping[str, str], add_call: Callable[[UsageCall], None]
    ) -> None:
        self._session_cwd = session_cwd
        self._cwd_by_fingerprint = cwd_by_fingerprint
        self._add_call = add_call
        self._fingerprint_by_pass: dict[str, str] = {}
        self._latest_fingerprint = ""

    def take(self, event: TrajectoryEvent) -> None:
        if event.event_type == EventType.MODEL_EXCHANGE_FINISHED:
            call = self._call(event)
            if call is not None:
                self._add_call(call)
        elif event.event_type == EventType.TURN_STARTED:
            self._note_fingerprint(event.payload.get("workspace_revision"), event.turn_id)
        else:
            self._note_fingerprint(event.payload.get("workspace_fingerprint"), None)

    def _note_fingerprint(self, value: object, pass_id: str | None) -> None:
        if isinstance(value, str) and value:
            self._latest_fingerprint = value
            if pass_id is not None:
                self._fingerprint_by_pass[pass_id] = value

    def _project(self, pass_id: str | None) -> str:
        """The folder a request ran in; one outside a pass (a title, a workflow node) takes the latest seen."""
        fingerprint = self._fingerprint_by_pass.get(pass_id or "", self._latest_fingerprint)
        if not fingerprint or not self._cwd_by_fingerprint:
            return self._session_cwd
        return self._cwd_by_fingerprint.get(fingerprint, "")

    def _call(self, event: TrajectoryEvent) -> UsageCall | None:
        try:
            ended = datetime.fromisoformat(event.occurred_at).astimezone()
        except ValueError, OverflowError, OSError:
            return None
        payload = event.payload
        usage = payload.get("usage")
        normalized = usage.get("normalized") if isinstance(usage, Mapping) else None
        if not isinstance(normalized, Mapping):
            normalized = {}
        input_total = plain_int_value(normalized.get("input_total"))
        output = plain_int_value(normalized.get("output_total"))
        cache_read = plain_int_value(normalized.get("cache_read")) or 0
        model = payload.get("response_model") or payload.get("request_model")
        return UsageCall(
            day=ended.date(),
            hour=ended.hour,
            model=model if isinstance(model, str) else "",
            source=event.actor.role,
            project=self._project(event.turn_id),
            new_input=max(0, (input_total or 0) - cache_read),
            cache_read=cache_read,
            output=output or 0,
            duration_ms=plain_int_value(payload.get("duration_ms")) or 0,
            first_chunk_ms=plain_int_value(payload.get("ttfc_ms")),
            reported=input_total is not None or output is not None,
            failed=payload.get("outcome") in _FAILED_OUTCOMES,
        )
