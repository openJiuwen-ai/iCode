# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Usage across sessions: what the scan reads, what it skips, and how the totals add up."""

from __future__ import annotations

import sys
import threading
import time
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from chrys.foundation.trajectory.envelope import LINE_BUDGET_BYTES, Actor
from chrys.foundation.trajectory.event_types import EventType, ExchangeOutcome
from chrys.foundation.trajectory.fingerprint import DOMAIN_WORKSPACE, fingerprint_text
from chrys.service.analytics.reader import TrajectoryScanCancelled, scan_event_types
from chrys.service.analytics.usage_report import (
    UsageCall,
    UsageReport,
    UsageTotals,
    load_usage_report,
    percentile,
    scan_usage,
    week_start,
)
from chrys.service.state.store import JsonFileStateStore
from chrys.service.trajectory.tombstone import INTENT_SUFFIX, tombstones_dir
from tests.service.analytics._events import TURN_ID, EventLog

if TYPE_CHECKING:
    from collections.abc import Iterator

    from chrys.foundation.trajectory.envelope import TrajectoryEvent

_KEY = b"k" * 32
_MAIN = Actor(kind="agent", role="main")
_SUB_AGENT = Actor(kind="agent", role="sub_agent")
_TITLE = Actor(kind="side_call", role="title_gen")
_NOON = "2026-10-15T12:00:00+00:00"
_OTHER_PASS = "5" * 32
_WANTED = frozenset({EventType.MODEL_EXCHANGE_FINISHED})


def _exchange(
    log: EventLog,
    *,
    occurred_at: str = _NOON,
    actor: Actor = _MAIN,
    turn_id: str | None = TURN_ID,
    model: str = "model-a",
    input_total: int | None = 100,
    cache_read: int | None = 40,
    output: int | None = 10,
    **payload: Any,
) -> None:
    normalized = {
        name: value
        for name, value in (("input_total", input_total), ("cache_read", cache_read), ("output_total", output))
        if value is not None
    }
    log.add(
        EventType.MODEL_EXCHANGE_FINISHED,
        log.next_sequence,
        turn_id=turn_id,
        actor=actor,
        occurred_at=occurred_at,
        payload={
            "outcome": ExchangeOutcome.SUCCESS,
            "response_model": model,
            "usage": {"normalized": normalized},
            **payload,
        },
    )


def _write_session(root: Path, name: str, log: EventLog) -> Path:
    path = root / name / "trajectory" / "events.jsonl"
    path.parent.mkdir(parents=True)
    log.write(path)
    return path


def _scan(root: Path, **kwargs: Any) -> UsageReport:
    kwargs.setdefault("session_cwds", {})
    kwargs.setdefault("fingerprint_key", _KEY)
    return scan_usage(root, **kwargs)


def _events_of(path: Path, event_types: frozenset[str] = _WANTED) -> list[TrajectoryEvent]:
    events: list[TrajectoryEvent] = []
    scan_event_types(path, event_types, events.append)
    return events


def _call(**overrides: Any) -> UsageCall:
    values: dict[str, Any] = {
        "day": date(2026, 10, 15),
        "hour": 12,
        "model": "model-a",
        "source": "main",
        "project": "/w/a",
        "new_input": 0,
        "cache_read": 0,
        "output": 0,
        "duration_ms": 0,
        "first_chunk_ms": None,
        "reported": True,
        "failed": False,
    }
    return UsageCall(**{**values, **overrides})


# ---------------------------------------------------------------------- scan_event_types


def test_scan_hands_over_only_the_asked_event_types(tmp_path: Path) -> None:
    log = EventLog()
    log.turn(1, 2)
    _exchange(log)
    path = tmp_path / "events.jsonl"
    log.write(path)

    assert [event.event_type for event in _events_of(path)] == [EventType.MODEL_EXCHANGE_FINISHED]


def test_scan_ignores_an_event_that_only_mentions_the_type_in_its_text(tmp_path: Path) -> None:
    log = EventLog()
    log.add(EventType.TURN_FINISHED, 1, payload={"end_reason": EventType.MODEL_EXCHANGE_FINISHED})
    path = tmp_path / "events.jsonl"
    log.write(path)

    assert f'"{EventType.MODEL_EXCHANGE_FINISHED}"'.encode() in path.read_bytes()
    assert _events_of(path) == []


def test_scan_skips_a_torn_tail_and_a_line_that_does_not_decode(tmp_path: Path) -> None:
    log = EventLog()
    _exchange(log, model="kept")
    path = tmp_path / "events.jsonl"
    log.write(path)
    whole = path.read_bytes()
    broken = f'{{"event_type":"{EventType.MODEL_EXCHANGE_FINISHED}"}}\n'.encode()
    path.write_bytes(broken + whole + whole[:-20])

    assert [event.payload["response_model"] for event in _events_of(path)] == ["kept"]


def test_scan_skips_a_line_over_the_write_budget_and_reads_the_next(tmp_path: Path) -> None:
    log = EventLog()
    _exchange(log, model="kept")
    path = tmp_path / "events.jsonl"
    log.write(path)
    whole = path.read_bytes()
    oversized = whole[:-1] + b" " * (LINE_BUDGET_BYTES * 2 + 7) + b"\n"
    path.write_bytes(oversized + whole)

    assert [event.payload["response_model"] for event in _events_of(path)] == ["kept"]


def test_scan_skips_a_future_schema(tmp_path: Path) -> None:
    log = EventLog()
    _exchange(log)
    path = tmp_path / "events.jsonl"
    log.write(path)
    path.write_bytes(path.read_bytes().replace(b'"schema_version":1', b'"schema_version":99'))

    assert b'"schema_version":99' in path.read_bytes()
    assert _events_of(path) == []


def test_scan_stops_when_cancelled(tmp_path: Path) -> None:
    log = EventLog()
    _exchange(log)
    path = tmp_path / "events.jsonl"
    log.write(path)
    cancel = threading.Event()
    cancel.set()

    with pytest.raises(TrajectoryScanCancelled):
        scan_event_types(path, _WANTED, lambda _event: None, cancel_event=cancel)


# ---------------------------------------------------------------------- scan_usage


def test_scan_usage_counts_each_session_and_skips_what_is_no_session(tmp_path: Path) -> None:
    for name in ("alive", "no-session-json", ".hidden", "deleted"):
        log = EventLog()
        _exchange(log, model=name)
        _write_session(tmp_path, name, log)
    (tmp_path / "without-log").mkdir()
    (tmp_path / "a-file").write_text("x", encoding="utf-8")
    graveyard = tombstones_dir(tmp_path)
    graveyard.mkdir(parents=True)
    (graveyard / f"deleted{INTENT_SUFFIX}").write_bytes(b"deleted")

    report = _scan(tmp_path, today=date(2026, 10, 31))

    assert sorted(call.model for call in report.calls) == ["alive", "no-session-json"]
    assert report.unreadable_logs == 0
    assert report.today == date(2026, 10, 31)


def test_scan_usage_counts_a_log_it_cannot_read(tmp_path: Path) -> None:
    (tmp_path / "broken" / "trajectory" / "events.jsonl").mkdir(parents=True)

    report = _scan(tmp_path)

    assert report.calls == ()
    assert report.unreadable_logs == 1


def test_scan_usage_fails_when_it_cannot_list_the_sessions(tmp_path: Path) -> None:
    """An unlistable folder is not an empty history: the dialog must say the read failed."""
    with pytest.raises(FileNotFoundError):
        _scan(tmp_path / "missing")


def test_scan_usage_lets_a_cancel_through(tmp_path: Path) -> None:
    log = EventLog()
    _exchange(log)
    _write_session(tmp_path, "one", log)
    cancel = threading.Event()
    cancel.set()

    with pytest.raises(TrajectoryScanCancelled):
        _scan(tmp_path, cancel_event=cancel)


def test_a_call_keeps_what_its_exchange_reported(tmp_path: Path) -> None:
    log = EventLog()
    _exchange(
        log,
        actor=_SUB_AGENT,
        input_total=100,
        cache_read=40,
        output=10,
        duration_ms=5000,
        ttfc_ms=1000,
        ttfv_ms=2000,
    )
    _write_session(tmp_path, "one", log)

    [call] = _scan(tmp_path).calls

    assert (call.model, call.source) == ("model-a", "sub_agent")
    assert (call.new_input, call.cache_read, call.output) == (60, 40, 10)
    assert (call.duration_ms, call.first_chunk_ms) == (5000, 1000)
    assert call.reported is True
    assert call.failed is False


def test_a_call_without_usage_counts_as_unreported_and_a_failure_as_failed(tmp_path: Path) -> None:
    log = EventLog()
    _exchange(log, input_total=None, cache_read=None, output=None, outcome=ExchangeOutcome.ERROR)
    _exchange(log, input_total=None, cache_read=None, output=None, outcome=ExchangeOutcome.STALLED)
    _exchange(log, outcome=ExchangeOutcome.ERROR)
    _write_session(tmp_path, "one", log)

    calls = _scan(tmp_path).calls

    assert [(call.reported, call.failed, call.new_input) for call in calls] == [
        (False, True, 0),
        (False, True, 0),
        (True, True, 60),
    ]


def test_a_call_reporting_either_total_counts_as_reported(tmp_path: Path) -> None:
    log = EventLog()
    _exchange(log, input_total=10, cache_read=None, output=None)
    _exchange(log, input_total=None, cache_read=None, output=5)
    _write_session(tmp_path, "one", log)

    assert [call.reported for call in _scan(tmp_path).calls] == [True, True]


def test_new_input_never_goes_below_zero_when_cache_reads_exceed_the_input(tmp_path: Path) -> None:
    log = EventLog()
    _exchange(log, input_total=10, cache_read=50)
    _write_session(tmp_path, "one", log)

    assert _scan(tmp_path).calls[0].new_input == 0


def test_the_request_model_names_a_call_with_no_answer(tmp_path: Path) -> None:
    log = EventLog()
    log.add(
        EventType.MODEL_EXCHANGE_FINISHED,
        1,
        actor=_MAIN,
        occurred_at=_NOON,
        payload={"outcome": ExchangeOutcome.ERROR, "request_model": "asked"},
    )
    log.add(
        EventType.MODEL_EXCHANGE_FINISHED,
        2,
        actor=_TITLE,
        occurred_at=_NOON,
        payload={"outcome": ExchangeOutcome.ERROR},
    )
    _write_session(tmp_path, "one", log)

    assert [call.model for call in _scan(tmp_path).calls] == ["asked", ""]


@pytest.fixture
def _utc_plus_eight() -> Iterator[None]:
    if sys.platform == "win32":
        pytest.skip("time.tzset is POSIX-only")
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("TZ", "CST-8")
        time.tzset()
        yield
    time.tzset()


@pytest.mark.usefixtures("_utc_plus_eight")
def test_a_call_falls_on_the_local_day_it_ended(tmp_path: Path) -> None:
    log = EventLog()
    _exchange(log, occurred_at="2026-10-31T17:00:00+00:00")
    _exchange(log, occurred_at="2026-10-31T15:59:00+00:00")
    _write_session(tmp_path, "one", log)

    report = _scan(tmp_path)

    assert [call.day for call in report.calls] == [date(2026, 11, 1), date(2026, 10, 31)]
    assert set(report.new_tokens_by_day) == {date(2026, 11, 1), date(2026, 10, 31)}


@pytest.mark.usefixtures("_utc_plus_eight")
def test_todays_new_tokens_fall_on_the_local_hour_they_ended(tmp_path: Path) -> None:
    log = EventLog()
    _exchange(log, occurred_at="2026-10-31T17:30:00+00:00")
    _exchange(log, occurred_at="2026-10-31T17:45:00+00:00")
    _exchange(log, occurred_at="2026-10-31T15:59:00+00:00")
    _write_session(tmp_path, "one", log)

    report = _scan(tmp_path, today=date(2026, 11, 1))

    assert [call.hour for call in report.calls] == [1, 1, 23]
    assert report.new_tokens_by_hour == {1: report.new_tokens_by_day[date(2026, 11, 1)]}
    assert report.new_tokens_by_hour[1] > 0


def _fingerprint(cwd: str) -> str:
    return fingerprint_text(_KEY, DOMAIN_WORKSPACE, cwd)


def _pass_started(log: EventLog, turn_id: str, cwd: str) -> None:
    log.add(
        EventType.TURN_STARTED,
        log.next_sequence,
        turn_id=turn_id,
        payload={"turn_number": 1, "workspace_revision": _fingerprint(cwd)},
    )


def test_a_call_belongs_to_the_folder_its_pass_ran_in(tmp_path: Path) -> None:
    log = EventLog()
    _pass_started(log, TURN_ID, "/w/first")
    _exchange(log, turn_id=TURN_ID, model="in-first")
    _pass_started(log, _OTHER_PASS, "/w/second")
    _exchange(log, turn_id=_OTHER_PASS, model="in-second")
    # The earlier pass's sub-agent can still report after a later pass began.
    _exchange(log, turn_id=TURN_ID, model="late-in-first")
    _write_session(tmp_path, "moved", log)

    calls = _scan(tmp_path, session_cwds={"moved": "/w/second", "other": "/w/first"}).calls

    assert {call.model: call.project for call in calls} == {
        "in-first": "/w/first",
        "in-second": "/w/second",
        "late-in-first": "/w/first",
    }


def test_a_call_outside_any_pass_takes_the_latest_folder_seen(tmp_path: Path) -> None:
    log = EventLog()
    log.add(
        EventType.SESSION_STARTED,
        log.next_sequence,
        turn_id=None,
        payload={"workspace_fingerprint": _fingerprint("/w/first")},
    )
    _exchange(log, turn_id=None, actor=_TITLE, model="after-start")
    _pass_started(log, TURN_ID, "/w/second")
    _exchange(log, turn_id=None, actor=_TITLE, model="after-pass")
    _exchange(log, turn_id="9" * 32, actor=_SUB_AGENT, model="unknown-pass")
    _write_session(tmp_path, "one", log)

    calls = _scan(tmp_path, session_cwds={"one": "/w/first", "other": "/w/second"}).calls

    assert {call.model: call.project for call in calls} == {
        "after-start": "/w/first",
        "after-pass": "/w/second",
        "unknown-pass": "/w/second",
    }


def test_a_fingerprint_no_saved_session_names_has_no_project(tmp_path: Path) -> None:
    log = EventLog()
    _pass_started(log, TURN_ID, "/w/gone")
    _exchange(log)
    _write_session(tmp_path, "one", log)

    assert _scan(tmp_path, session_cwds={"one": "/w/here"}).calls[0].project == ""


@pytest.mark.parametrize("fingerprint_key", [_KEY, None], ids=["key", "no-key"])
def test_a_log_without_a_fingerprint_or_a_key_uses_the_sessions_folder(
    tmp_path: Path, fingerprint_key: bytes | None
) -> None:
    log = EventLog()
    _exchange(log)
    _write_session(tmp_path, "one", log)

    report = _scan(tmp_path, session_cwds={"one": "/w/here"}, fingerprint_key=fingerprint_key)

    assert report.calls[0].project == "/w/here"


def test_no_key_uses_the_sessions_folder_even_with_a_fingerprint(tmp_path: Path) -> None:
    log = EventLog()
    _pass_started(log, TURN_ID, "/w/elsewhere")
    _exchange(log)
    _write_session(tmp_path, "one", log)

    report = _scan(tmp_path, session_cwds={"one": "/w/here"}, fingerprint_key=None)

    assert report.calls[0].project == "/w/here"


def test_load_usage_report_reads_the_stores_sessions_and_their_folders(tmp_path: Path) -> None:
    store = JsonFileStateStore(tmp_path)
    log = EventLog()
    _exchange(log)
    _write_session(tmp_path, "one", log)

    before = datetime.now().astimezone().date()
    report = load_usage_report(store)
    after = datetime.now().astimezone().date()

    assert [call.model for call in report.calls] == ["model-a"]
    assert before <= report.today <= after
    assert store.sessions_root == tmp_path


async def test_session_folder_cwds_names_each_saved_session(tmp_path: Path) -> None:
    store = JsonFileStateStore(tmp_path)
    await store.save_session("11111111-aaaa", {}, primary_cwd="/w/one")

    assert store.session_folder_cwds() == {store.session_dir("11111111-aaaa").name: "/w/one"}


def test_load_usage_report_without_a_store_is_empty() -> None:
    assert load_usage_report(None).calls == ()


# ---------------------------------------------------------------------- totals


def test_totals_add_up_new_tokens_and_the_cache_hit() -> None:
    totals = UsageTotals()
    totals.add(_call(new_input=60, cache_read=40, output=10))
    totals.add(_call(new_input=40, cache_read=60, output=20, reported=False, failed=True))

    assert totals.new_tokens == 130
    assert totals.cache_hit == 0.5
    assert (totals.calls, totals.failed, totals.unreported) == (2, 1, 1)
    assert UsageTotals().cache_hit is None


def test_speed_is_output_over_streaming_time_across_the_timed_calls() -> None:
    totals = UsageTotals()
    totals.add(_call(output=100, duration_ms=3000, first_chunk_ms=1000))
    totals.add(_call(output=500, duration_ms=5000, first_chunk_ms=1000))
    # None of these can be timed: not streamed, no output, or no time after the first chunk.
    totals.add(_call(output=9999, duration_ms=1000))
    totals.add(_call(output=0, duration_ms=9000, first_chunk_ms=1000))
    totals.add(_call(output=9999, duration_ms=1000, first_chunk_ms=1000))

    assert totals.speed == 100.0
    assert UsageTotals().speed is None


def test_speed_leaves_out_a_failed_calls_partial_output() -> None:
    """A cut-off stream counts about one output token over all the time it ran."""
    totals = UsageTotals()
    totals.add(_call(output=100, duration_ms=3000, first_chunk_ms=1000))
    totals.add(_call(output=1, duration_ms=60000, first_chunk_ms=1000, failed=True))

    assert totals.speed == 50.0
    assert totals.output == 101


def test_a_report_summarizes_only_the_days_in_range() -> None:
    report = UsageReport(
        calls=(
            _call(day=date(2026, 10, 1), new_input=1),
            _call(day=date(2026, 10, 2), new_input=2, model="model-b", source="sub_agent", project="/w/b"),
            _call(day=date(2026, 10, 3), new_input=4),
        ),
        today=date(2026, 10, 3),
    )

    summary = report.summarize(date(2026, 10, 2), date(2026, 10, 3))

    assert summary.total.new_input == 6
    assert {key: totals.new_input for key, totals in summary.models.items()} == {"model-a": 4, "model-b": 2}
    assert {key: totals.new_input for key, totals in summary.sources.items()} == {"main": 4, "sub_agent": 2}
    assert {key: totals.new_input for key, totals in summary.projects.items()} == {"/w/a": 4, "/w/b": 2}


def test_week_start_is_monday() -> None:
    assert week_start(date(2026, 11, 1)) == date(2026, 10, 26)
    assert week_start(date(2026, 10, 26)) == date(2026, 10, 26)


def test_percentile_is_nearest_rank() -> None:
    assert percentile([], 0.5) is None
    assert percentile([5], 0.95) == 5
    assert percentile([1, 2, 3, 4], 0.5) == 2
    assert percentile(range(1, 101), 0.95) == 95
    assert percentile([*range(1, 20), 1000], 0.95) == 19
