# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The hook runs list in the sidebar's Hooks tab (plan section 3, 6)."""

from __future__ import annotations

import dataclasses
import re
import time
from collections.abc import Callable

import pytest
from textual.app import App, ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.geometry import Region
from textual.widgets import Static

from chrys.app.tui.widgets.sidebar import hooks as hooks_module
from chrys.app.tui.widgets.sidebar.hooks import HookRunList, run_duration, run_label, run_outcome
from chrys.foundation.i18n.formatting import format_message
from chrys.foundation.models.hook_runs import HookRunRecord
from chrys.service.hooks.run_log import HOOK_RUN_LOG_MAX_ENDED
from chrys.service.trajectory.hooks import HookOutcome
from tests.support.frozen_clock import freeze_monotonic
from tests.support.tui_helpers import click_when_settled, resize_when_settled
from tests.support.waiting import wait_for, wait_until


def _record(
    run_id: str,
    *,
    hook_id: str | None = None,
    state: str = "ended",
    outcome: str | None = HookOutcome.SUCCESS,
    effect: str | None = None,
    exit_code: int | None = 0,
    tool_name: str = "zsh",
    event: str = "before_tool_call",
    sub_agent: str = "",
    session_id: str = "s1",
    detach: bool = False,
    queued_for: float = 0.0,
    ran_for: float | None = 0.0405,
    ended_ago: float = 1.0,
    number: int = 1,
    call_rejected: bool = False,
    confirmation: str | None = None,
    now: float | None = None,
) -> HookRunRecord:
    now = time.monotonic() if now is None else now
    if state == "queued":
        registered, started, ended = now - queued_for, None, None
        outcome, exit_code = None, None
    elif state == "running":
        started = now - (ran_for or 0.0)
        registered, ended = started - queued_for, None
        outcome, exit_code = None, None
    else:
        ended = now - ended_ago
        started = None if ran_for is None else ended - ran_for
        registered = (started if started is not None else ended) - queued_for
    return HookRunRecord(
        run_id=run_id,
        session_id=session_id,
        hook_id=hook_id or run_id,
        hook_event=event,
        execution_mode="blocking",
        detach=detach,
        tool_name=tool_name,
        sub_agent=sub_agent,
        retry=False,
        registered_at=registered,
        started_at=started,
        ended_at=ended,
        registered_wall=time.time() - (now - registered),
        state=state,  # type: ignore[arg-type]
        outcome=outcome,
        exit_code=exit_code,
        effect=effect,  # type: ignore[arg-type]
        number=number,
        call_rejected=call_rejected,
        confirmation=confirmation,  # type: ignore[arg-type]
    )


class _Source:
    def __init__(self, *records: HookRunRecord, configured: int = 1, dropped: int = 0) -> None:
        self.records = tuple(records)
        self.configured = configured
        self.dropped = dropped
        """Ended runs the log dropped past its cap."""
        self.reads = 0

    def read(self) -> tuple[HookRunRecord, ...]:
        self.reads += 1
        return self.records


class _ListApp(App):
    def __init__(self, source: _Source) -> None:
        super().__init__()
        self.source = source
        self.opened: list[str] = []

    def compose(self) -> ComposeResult:
        with Vertical(id="holder"):
            yield HookRunList()

    def on_mount(self) -> None:
        hooks = self.query_one(HookRunList)
        hooks.bind_source(self.source.read, lambda: self.source.configured, lambda: self.source.dropped)
        hooks.runs_changed()

    def on_hook_run_list_opened(self, event: HookRunList.Opened) -> None:
        self.opened.append(event.run_id)


_NUMBER = re.compile(r"^\s*\d+ ")


def _numbered_rows(app: App) -> list[str]:
    rows = app.query_one("#hook-runs-rows", Static)
    width, height = rows.size
    return [strip.text.rstrip() for strip in rows.render_lines(Region(0, 0, width, height)) if strip.text.strip()]


def _rows(app: App) -> list[str]:
    """The rows from their icon on: the run number before it has a test of its own."""
    return [_NUMBER.sub("", row) for row in _numbered_rows(app)]


def _title(app: App) -> str:
    return str(app.query_one("#hook-runs-title", Static).render())


def _empty(app: App) -> str:
    empty = app.query_one("#hook-runs-empty", Static)
    return str(empty.render()) if empty.display else ""


async def _shown(pilot, count: int) -> list[str]:
    await wait_for(lambda: len(_rows(pilot.app)) == count, pilot=pilot, description=f"{count} rows")
    return _rows(pilot.app)


# --- Each row: icon, label, duration; the outcome is in the run's details ------


@pytest.mark.parametrize(
    ("fields", "icon", "outcome"),
    [
        ({"state": "queued", "queued_for": 2.0}, "◌", "queued"),
        ({}, "✓", ""),
        ({"effect": "blocked"}, "⊘", "blocked"),
        ({"effect": "modified_args"}, "✓", "modified args"),
        ({"effect": "modified_args", "call_rejected": True}, "⊘", "call rejected"),
        ({"effect": "asked", "confirmation": "pending"}, "?", "waiting for user"),
        ({"effect": "asked", "confirmation": "approved"}, "✓", "user continued"),
        ({"effect": "asked", "confirmation": "declined"}, "⊘", "user cancelled"),
        ({"effect": "asked", "confirmation": "timed_out"}, "⊘", "no answer in time"),
        ({"effect": "asked", "confirmation": "unavailable"}, "⊘", "couldn't ask user"),
        ({"effect": "asked", "confirmation": "interrupted"}, "■", "interrupted"),
        ({"effect": "added_reminder"}, "✓", "added reminder"),
        ({"effect": "added_context"}, "✓", "added context"),
        ({"outcome": HookOutcome.FAILED, "exit_code": 1}, "✗", "exit 1"),
        ({"outcome": HookOutcome.FAILED, "exit_code": 2, "effect": "blocked"}, "✗", "exit 2 · blocked"),
        ({"outcome": HookOutcome.TIMED_OUT, "exit_code": None}, "✗", "timed out"),
        ({"outcome": HookOutcome.TIMED_OUT, "exit_code": None, "effect": "blocked"}, "✗", "timed out · blocked"),
        ({"outcome": HookOutcome.LAUNCH_ERROR, "exit_code": None}, "✗", "couldn't start"),
        ({"outcome": HookOutcome.CANCELLED, "exit_code": None}, "■", "cancelled"),
        ({"outcome": HookOutcome.DETACHED, "exit_code": None, "detach": True, "ran_for": None}, "→", "detached"),
        (
            {"outcome": HookOutcome.SPAWN_FAILED, "exit_code": None, "detach": True, "ran_for": None},
            "✗",
            "couldn't start",
        ),
    ],
    ids=[
        "queued",
        "success",
        "blocked",
        "modified",
        "modified-call-rejected",
        "asked-pending",
        "asked-continued",
        "asked-cancelled",
        "asked-timed-out",
        "asked-unavailable",
        "asked-interrupted",
        "reminder",
        "context",
        "exit",
        "exit-blocked",
        "timed-out",
        "timed-out-blocked",
        "launch-error",
        "cancelled",
        "handed-off",
        "spawn-failed",
    ],
)
async def test_a_rows_icon_follows_how_it_ended_and_its_outcome_is_left_to_the_details(
    fields: dict[str, object], icon: str, outcome: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A queued row counts its wait up from now: the list and the expected text share one frozen clock.
    clock = freeze_monotonic(monkeypatch, hooks_module, now=time.monotonic())
    record = _record("r1", hook_id="check", now=clock.now, **fields)  # type: ignore[arg-type]
    # What the run's details say of it.
    assert " · ".join(format_message(part) for part in run_outcome(record)) == outcome
    app = _ListApp(_Source(record))
    async with app.run_test(size=(60, 20)) as pilot:
        (row,) = await _shown(pilot, 1)
        # Only its duration follows the hook's name; handed off, it has none.
        expected = [] if fields.get("ran_for", 0.0) is None else [run_duration(record, clock.now)]
        assert row.split() == [icon, "check", *expected], row


async def test_a_running_row_shows_a_still_dot_and_its_duration_alone() -> None:
    app = _ListApp(_Source(_record("r1", hook_id="slow", state="running", ran_for=6.0)))
    async with app.run_test(size=(60, 20)) as pilot:
        (row,) = await _shown(pilot, 1)
        # As a running background task.
        assert row.startswith("● slow "), row
        # Its duration ticks in the running color, which says it runs.
        assert re.fullmatch(r"\d+s", row.split("slow", 1)[1].strip()), row


async def test_every_rows_duration_ends_at_the_right_edge_whatever_the_others_show() -> None:
    records = (
        _record("a", hook_id="quick", number=1),
        _record("b", hook_id="gate", outcome=HookOutcome.FAILED, exit_code=1, effect="blocked", number=2),
        _record("c", hook_id="slow", state="running", ran_for=6.0, number=3),
    )
    app = _ListApp(_Source(*records))
    async with app.run_test(size=(60, 20)) as pilot:
        await _shown(pilot, 3)
        rows = pilot.app.query_one("#hook-runs-rows", Static)
        width = rows.size.width
        lines = [strip.text for strip in rows.render_lines(Region(0, 0, width, 3))]
        assert [len(line.rstrip()) for line in lines] == [width] * 3, lines
        assert re.search(r"quick +40ms$", lines[0]), lines
        assert re.search(r"gate +40ms$", lines[1]), lines
        assert re.search(r" \d+s$", lines[2]), lines


async def test_the_scrollbar_sits_on_the_panels_edge_and_the_rows_end_with_the_title() -> None:
    app = _ListApp(_Source(*_ended(40)))
    async with app.run_test(size=(60, 20)) as pilot:
        await wait_for(lambda: len(_rows(pilot.app)) > 5, pilot=pilot, description="rows")
        hooks = pilot.app.query_one(HookRunList)
        box = pilot.app.query_one("#hook-runs-box", VerticalScroll)
        assert box.show_vertical_scrollbar, "40 rows overflow the box"
        assert box.region.right == hooks.region.right, "no padding outside the bar"
        rows = pilot.app.query_one("#hook-runs-rows", Static).region
        # The bar's column, reserved with or without the bar, keeps the rows off the edge.
        assert rows.right == hooks.region.right - 1
        clear = pilot.app.query_one("#hook-runs-clear", Static).region
        assert clear.right == rows.right, "the title's link ends where the rows do"


def test_the_label_is_the_hook_alone_whatever_fired_it() -> None:
    # The event, tool and sub-agent are in the run's details.
    assert run_label(_record("a", hook_id="guard", tool_name="write_file")) == "guard"
    assert run_label(_record("b", hook_id="lint", tool_name="", event="after_turn")) == "lint"
    assert run_label(_record("c", hook_id="protect", tool_name="edit_file", sub_agent="Explore")) == "protect"


async def test_external_text_is_shown_literally_and_sanitized() -> None:
    record = _record("r1", hook_id="[bold]gate[/]\x1b[31m\x07")
    app = _ListApp(_Source(record))
    async with app.run_test(size=(80, 20)) as pilot:
        (row,) = await _shown(pilot, 1)
        assert "[bold]gate[/]" in row
        assert "\x1b" not in row and "\x07" not in row


def test_durations() -> None:
    now = time.monotonic()
    queued = _record("q", state="queued", queued_for=2.0)
    assert run_duration(queued, queued.registered_at + 2.0) == "2s"
    running = _record("r", state="running", ran_for=6.0, queued_for=10.0)
    assert running.started_at is not None
    assert run_duration(running, running.started_at + 6.0) == "6s", "from the slot, not the registration"
    # As everywhere in iCode: whole seconds from a second on.
    assert run_duration(_record("e", ran_for=1.2, queued_for=5.0), now) == "1s"
    assert run_duration(_record("g", ran_for=6.04), now) == "6s"
    assert run_duration(_record("h", ran_for=14.2), now) == "14s"
    assert run_duration(_record("f", ran_for=0.0405), now) == "40ms"
    never_started = _record("c", outcome=HookOutcome.CANCELLED, exit_code=None, ran_for=None, queued_for=3.0)
    assert run_duration(never_started, now) == ""
    detached = _record("d", outcome=HookOutcome.DETACHED, exit_code=None, detach=True, ran_for=0.01)
    assert run_duration(detached, now) == ""


async def test_the_running_duration_grows_while_shown() -> None:
    app = _ListApp(_Source(_record("r1", hook_id="slow", state="running", ran_for=3.0)))
    async with app.run_test(size=(60, 20)) as pilot:
        (first,) = await _shown(pilot, 1)
        await wait_for(
            lambda: _rows(pilot.app) and _rows(pilot.app)[0] != first,
            timeout=4.0,
            pilot=pilot,
            description="the duration ticked",
        )


# --- The list as a whole --------------------------------------------------------


async def test_rows_keep_the_order_active_first_then_ended_and_mix_sessions() -> None:
    records = (
        _record("q", hook_id="queued-check", state="queued", queued_for=2.0, session_id="s2"),
        _record("r", hook_id="slow-shell", state="running", ran_for=6.0, session_id="s1"),
        _record("e1", hook_id="ruff", effect="added_context", ended_ago=1.0, session_id="s2"),
        _record("e2", hook_id="protect", effect="modified_args", ended_ago=5.0, session_id="s1"),
    )
    app = _ListApp(_Source(*records))
    async with app.run_test(size=(70, 20)) as pilot:
        rows = await _shown(pilot, 4)
        assert [row.split()[1] for row in rows] == ["queued-check", "slow-shell", "ruff", "protect"]
        assert "s1" not in " ".join(rows) and "s2" not in " ".join(rows), "rows do not name the session"


async def test_each_row_starts_with_its_run_number_aligned_as_the_messages_tab_numbers_turns() -> None:
    records = (
        _record("r", hook_id="slow", state="running", number=104),
        _record("e", hook_id="lint", number=9),
    )
    app = _ListApp(_Source(*records))
    async with app.run_test(size=(70, 20)) as pilot:
        await _shown(pilot, 2)
        first, second = _numbered_rows(pilot.app)
        assert first.startswith("104 ") and second.startswith("  9 "), (first, second)
        # Right-aligned: the icons line up in one column.
        assert first.index("slow") == second.index("lint")


async def test_a_long_name_takes_the_row_and_gives_way_only_to_the_duration() -> None:
    record = _record(
        "e", hook_id="all-modify-timeout-and-more", outcome=HookOutcome.TIMED_OUT, exit_code=None, effect="blocked"
    )
    app = _ListApp(_Source(record))
    async with app.run_test(size=(30, 10)) as pilot:
        (row,) = await _shown(pilot, 1)
        assert row.startswith("✗ all-modify") and "…" in row, row
        assert re.search(r"… +40ms$", row), row
        # Wider, the whole name shows.
        await resize_when_settled(pilot, 90, 10)
        await wait_for(
            lambda: re.fullmatch(r"✗ all-modify-timeout-and-more +40ms", _rows(pilot.app)[0]) is not None,
            pilot=pilot,
            description="whole name",
        )


def _ended(count: int, *, start: int = 1) -> tuple[HookRunRecord, ...]:
    return tuple(_record(f"e{n}", hook_id=f"hook-{n}", number=n) for n in range(start, start + count))


async def test_the_list_opens_on_the_newest_run_and_follows_new_ones_while_at_the_bottom() -> None:
    source = _Source(*_ended(30))
    app = _ListApp(source)
    async with app.run_test(size=(60, 12)) as pilot:
        await _shown(pilot, 30)
        box = pilot.app.query_one("#hook-runs-box", VerticalScroll)
        await wait_for(lambda: box.max_scroll_y > 0, pilot=pilot, description="more rows than fit")
        await wait_for(lambda: box.scroll_y == box.max_scroll_y, pilot=pilot, description="opened at the newest")

        source.records = _ended(32)
        pilot.app.query_one(HookRunList).runs_changed()
        await _shown(pilot, 32)
        await wait_for(
            lambda: box.scroll_y == box.max_scroll_y and box.max_scroll_y > 0,
            pilot=pilot,
            description="followed the new runs to the bottom",
        )


async def test_a_list_at_the_bottom_stays_there_when_the_notice_shows_above_its_rows() -> None:
    source = _Source(*_ended(30))
    app = _ListApp(source)
    async with app.run_test(size=(60, 12)) as pilot:
        await _shown(pilot, 30)
        box = pilot.app.query_one("#hook-runs-box", VerticalScroll)
        await wait_for(lambda: box.scroll_y == box.max_scroll_y > 0, pilot=pilot, description="at the newest")

        # A run that ends at once (a detached one) drops the oldest: as many rows, and the notice.
        source.records = _ended(30, start=2)
        source.dropped = 1
        pilot.app.query_one(HookRunList).runs_changed()
        await wait_for(lambda: _unlisted(pilot.app) == _ONLY_LATEST, pilot=pilot, description="notice")
        await wait_for(lambda: box.scroll_y == box.max_scroll_y, pilot=pilot, description="still at the newest")

        source.records = _ended(32, start=2)
        pilot.app.query_one(HookRunList).runs_changed()
        await _shown(pilot, 32)
        await wait_for(
            lambda: box.scroll_y == box.max_scroll_y and _numbered_rows(pilot.app)[-1].lstrip().startswith("33 "),
            pilot=pilot,
            description="still following the new runs",
        )


async def test_a_list_scrolled_up_to_an_older_run_stays_there_as_new_runs_join() -> None:
    source = _Source(*_ended(30))
    app = _ListApp(source)
    async with app.run_test(size=(60, 12)) as pilot:
        await _shown(pilot, 30)
        box = pilot.app.query_one("#hook-runs-box", VerticalScroll)
        await wait_for(lambda: box.scroll_y == box.max_scroll_y > 0, pilot=pilot, description="at the newest")
        box.scroll_to(y=2, animate=False)
        await wait_for(lambda: box.scroll_y == 2, pilot=pilot, description="scrolled up")

        source.records = _ended(32)
        pilot.app.query_one(HookRunList).runs_changed()
        await _shown(pilot, 32)
        assert not await wait_until(lambda: box.scroll_y != 2, timeout=0.5, pilot=pilot)


async def test_the_title_counts_running_and_queued_runs() -> None:
    source = _Source(_record("e"))
    app = _ListApp(source)
    async with app.run_test(size=(60, 20)) as pilot:
        await _shown(pilot, 1)
        assert _title(pilot.app) == "Hooks"
        source.records = (_record("q", state="queued"), _record("r", state="running"), _record("e"))
        pilot.app.query_one(HookRunList).runs_changed()
        await wait_for(lambda: _title(pilot.app) == "Hooks (2 active)", pilot=pilot, description="2 active")
        source.records = (_record("q", state="queued"), _record("e"))
        pilot.app.query_one(HookRunList).runs_changed()
        await wait_for(lambda: _title(pilot.app) == "Hooks (1 active)", pilot=pilot, description="1 active")


@pytest.mark.parametrize(
    ("configured", "expected"), [(0, "No hooks configured"), (2, "No hook has run since iCode started")]
)
async def test_the_empty_states_before_any_run(configured: int, expected: str) -> None:
    app = _ListApp(_Source(configured=configured))
    async with app.run_test(size=(60, 20)) as pilot:
        await wait_for(lambda: _empty(pilot.app) == expected, pilot=pilot, description=expected)
        assert _rows(pilot.app) == [] or not pilot.app.query_one("#hook-runs-box").display


async def test_a_list_whose_runs_were_all_cleared_says_so_and_not_that_nothing_ran() -> None:
    source = _Source(_record("e1", hook_id="done-1", number=1), _record("e2", hook_id="done-2", number=2))
    app = _ListApp(source)
    async with app.run_test(size=(60, 20)) as pilot:
        await _shown(pilot, 2)
        assert _empty(pilot.app) == ""
        await click_when_settled(pilot, "#hook-runs-clear")
        await wait_for(
            lambda: _empty(pilot.app) == "Ended runs cleared", pilot=pilot, description="the cleared empty state"
        )
        assert _rows(pilot.app) == [] or not pilot.app.query_one("#hook-runs-box").display
        # A new run shows again, and the empty state goes.
        source.records = (*source.records, _record("e3", hook_id="done-3", number=3))
        pilot.app.query_one(HookRunList).runs_changed()
        (row,) = await _shown(pilot, 1)
        assert "done-3" in row
        assert _empty(pilot.app) == ""


async def test_clear_ended_hides_ended_rows_until_new_runs_end() -> None:
    source = _Source(_record("r", hook_id="slow", state="running"), _record("e1", hook_id="done-1"))
    app = _ListApp(source)
    async with app.run_test(size=(60, 20)) as pilot:
        await _shown(pilot, 2)
        await click_when_settled(pilot, "#hook-runs-clear")
        rows = await _shown(pilot, 1)
        assert "slow" in rows[0]
        hooks = pilot.app.query_one(HookRunList)
        # The backend still returns it: hidden on the TUI side only.
        source.records = (_record("e2", hook_id="done-2"), *source.records)
        hooks.runs_changed()
        rows = await _shown(pilot, 2)
        assert any("done-2" in row for row in rows)
        assert not any("done-1" in row for row in rows)
        assert "e1" in hooks._cleared
        # Once the log no longer keeps it, the cleared set lets it go.
        source.records = tuple(record for record in source.records if record.run_id != "e1")
        hooks.runs_changed()
        await wait_for(lambda: "e1" not in hooks._cleared, pilot=pilot, description="pruned")


def _unlisted(app: App) -> str:
    notice = app.query_one("#hook-runs-unlisted", Static)
    return str(notice.render()) if notice.display else ""


# The number is the log's own cap.
_ONLY_LATEST = f"Only the latest {HOOK_RUN_LOG_MAX_ENDED} runs are kept"


async def test_the_notice_shows_only_once_the_log_dropped_runs_past_its_cap() -> None:
    # Numbers past 1 alone do not show it: only the log's own count of dropped runs.
    source = _Source(*_ended(3, start=40))
    app = _ListApp(source)
    async with app.run_test(size=(60, 20)) as pilot:
        await _shown(pilot, 3)
        assert not await wait_until(lambda: _unlisted(pilot.app) != "", timeout=0.3, pilot=pilot)
        source.records = _ended(3, start=41)
        source.dropped = 1
        pilot.app.query_one(HookRunList).runs_changed()
        await wait_for(lambda: _unlisted(pilot.app) == _ONLY_LATEST, pilot=pilot, description="notice")
        # Above the oldest row, scrolled with them.
        notice = pilot.app.query_one("#hook-runs-unlisted", Static)
        rows = pilot.app.query_one("#hook-runs-rows", Static)
        assert notice.parent is rows.parent and notice.region.y < rows.region.y
        # Not selectable: it does not look like a row to click.
        assert notice.allow_select is False
        assert notice.text_selection is None


async def test_clearing_ended_runs_never_shows_the_notice_and_numbers_carry_on() -> None:
    source = _Source(
        _record("e1", hook_id="done-1", number=1),
        _record("r", hook_id="slow", state="running", number=2),
        _record("e3", hook_id="done-3", number=3),
    )
    app = _ListApp(source)
    async with app.run_test(size=(60, 20)) as pilot:
        await _shown(pilot, 3)
        await click_when_settled(pilot, "#hook-runs-clear")
        (left,) = await _shown(pilot, 1)
        assert "slow" in left
        assert not await wait_until(lambda: _unlisted(pilot.app) != "", timeout=0.3, pilot=pilot)
        # A new run after the clear keeps counting: #4, not a renumbered #2.
        source.records = (*source.records, _record("e4", hook_id="done-4", number=4))
        pilot.app.query_one(HookRunList).runs_changed()
        await _shown(pilot, 2)
        numbered = _numbered_rows(pilot.app)
        assert [row.split()[0] for row in numbered] == ["2", "4"], numbered
        assert _unlisted(pilot.app) == ""


async def _scroll_when_selecting_the_20th_row(start: int, *, dropped: int = 0, width: int = 60) -> tuple[float, int]:
    """Where the list scrolls to once the 20th row is selected, and how many lines the notice takes above the rows."""
    app = _ListApp(_Source(*_ended(60, start=start), dropped=dropped))
    async with app.run_test(size=(width, 12)) as pilot:
        await _shown(pilot, 60)
        box = pilot.app.query_one("#hook-runs-box", VerticalScroll)
        await wait_for(lambda: box.scroll_y == box.max_scroll_y > 0, pilot=pilot, description="at the end")
        pilot.app.query_one(HookRunList).select(f"e{start + 19}")
        await wait_for(lambda: box.scroll_y < box.max_scroll_y, pilot=pilot, description="scrolled to it")
        notice = pilot.app.query_one("#hook-runs-unlisted", Static)
        above = notice.outer_size.height if notice.display else 0
        # In view: the rows start right under the notice, one line each.
        assert box.scroll_y <= above + 19 < box.scroll_y + box.scrollable_content_region.height, (box.scroll_y, above)
        return box.scroll_y, above


async def test_a_selected_row_scrolls_into_view_below_the_notice() -> None:
    plain, _ = await _scroll_when_selecting_the_20th_row(start=1)
    # Once the log dropped runs, the notice takes the first line: the same row sits one line lower.
    assert await _scroll_when_selecting_the_20th_row(start=41, dropped=40) == (plain + 1, 1)


async def test_a_selected_row_scrolls_into_view_below_a_notice_that_wraps() -> None:
    # A narrow sidebar: the notice wraps.
    plain, _ = await _scroll_when_selecting_the_20th_row(start=1, width=24)
    scrolled, above = await _scroll_when_selecting_the_20th_row(start=41, dropped=40, width=24)
    assert above >= 2
    # The row sits as far below where it sits without the notice as the notice is tall: as centered.
    assert scrolled == plain + above


async def test_clicking_a_row_opens_its_run() -> None:
    app = _ListApp(_Source(_record("run-a", hook_id="first"), _record("run-b", hook_id="second", ended_ago=3)))
    async with app.run_test(size=(60, 20)) as pilot:
        await _shown(pilot, 2)
        rows = pilot.app.query_one("#hook-runs-rows", Static)
        await click_when_settled(pilot, rows, offset=(4, 1))
        await wait_for(lambda: pilot.app.opened == ["run-b"], pilot=pilot, description="opened")


# --- Hidden: no reads, no paints, no timer (section 6) --------------------------


async def test_a_hidden_list_only_marks_a_reread_and_reads_once_when_shown() -> None:
    source = _Source(_record("e"))
    app = _ListApp(source)
    async with app.run_test(size=(60, 20)) as pilot:
        await _shown(pilot, 1)
        holder = pilot.app.query_one("#holder")
        holder.display = False
        await wait_for(lambda: not pilot.app.query_one(HookRunList).is_on_screen, pilot=pilot, description="hidden")
        reads = source.reads
        hooks = pilot.app.query_one(HookRunList)
        source.records = (_record("e2"), _record("e"))
        for _ in range(3):
            hooks.runs_changed()
        assert not await wait_until(lambda: source.reads > reads, timeout=0.5, pilot=pilot)
        holder.display = True
        await _shown(pilot, 2)
        assert source.reads == reads + 1


async def test_several_changes_in_one_cycle_are_read_once() -> None:
    source = _Source(_record("e"))
    app = _ListApp(source)
    async with app.run_test(size=(60, 20)) as pilot:
        await _shown(pilot, 1)
        reads = source.reads
        hooks = pilot.app.query_one(HookRunList)
        source.records = (_record("e2"), _record("e"))
        for _ in range(5):
            hooks.runs_changed()
        await _shown(pilot, 2)
        assert source.reads == reads + 1


async def test_the_timer_stops_while_hidden_and_when_nothing_is_active(monkeypatch: pytest.MonkeyPatch) -> None:
    source = _Source(_record("r", state="running", ran_for=2.0))
    app = _ListApp(source)
    async with app.run_test(size=(60, 20)) as pilot:
        await _shown(pilot, 1)
        rows = pilot.app.query_one("#hook-runs-rows", Static)
        updates: list[object] = []
        original: Callable[..., None] = rows.update

        def counting(*args: object, **kwargs: object) -> None:
            updates.append(args)
            original(*args, **kwargs)

        monkeypatch.setattr(rows, "update", counting)
        await wait_for(lambda: len(updates) >= 1, timeout=3.0, pilot=pilot, description="a tick while shown")
        holder = pilot.app.query_one("#holder")
        holder.display = False
        await wait_for(lambda: not pilot.app.query_one(HookRunList).is_on_screen, pilot=pilot, description="hidden")
        seen = len(updates)
        assert not await wait_until(lambda: len(updates) > seen, timeout=1.5, pilot=pilot), "ticked while hidden"
        holder.display = True
        source.records = (dataclasses.replace(_record("r"), run_id="r"),)
        pilot.app.query_one(HookRunList).runs_changed()
        await wait_for(
            lambda: bool(_rows(pilot.app)) and _rows(pilot.app)[0].startswith("✓"), pilot=pilot, description="ended"
        )
        seen = len(updates)
        assert not await wait_until(lambda: len(updates) > seen, timeout=1.5, pilot=pilot), "ticked with none active"


def test_the_module_exposes_its_row_helpers() -> None:
    assert hooks_module.run_state(_record("q", state="queued")) == "queued"
    assert hooks_module.run_state(_record("r", state="running")) == "running"


def _row_backgrounds(app: App, index: int) -> set[object]:
    rows = app.query_one("#hook-runs-rows", Static)
    width, height = rows.size
    lines = [strip for strip in rows.render_lines(Region(0, 0, width, height)) if strip.text.strip()]
    return {segment.style.bgcolor for segment in lines[index] if segment.style is not None and segment.text.strip()}


async def test_a_selected_row_follows_a_theme_change_with_nothing_over_the_list() -> None:
    app = _ListApp(_Source(_record("a", number=1), _record("b", number=2)))
    async with app.run_test(size=(60, 20)) as pilot:
        await _shown(pilot, 2)
        hooks = pilot.app.query_one(HookRunList)

        def selected() -> object:
            return hooks.get_component_rich_style("hook-runs--selected").bgcolor

        hooks.select("a")
        await wait_for(lambda: _row_backgrounds(pilot.app, 0) == {selected()}, pilot=pilot, description="marked")
        before = selected()
        # Every run ended: no tick repaints the rows, and no overlay closes over them.
        pilot.app.theme = "textual-dark" if pilot.app.theme == "textual-light" else "textual-light"
        await wait_for(lambda: selected() != before, pilot=pilot, description="the theme's color resolved")
        await wait_for(
            lambda: _row_backgrounds(pilot.app, 0) == {selected()}, pilot=pilot, description="the row follows the theme"
        )


async def test_a_theme_change_while_the_list_is_hidden_paints_it_only_once_it_shows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _ListApp(_Source(_record("a", number=1), _record("b", number=2)))
    async with app.run_test(size=(60, 20)) as pilot:
        await _shown(pilot, 2)
        hooks = pilot.app.query_one(HookRunList)

        def selected() -> object:
            return hooks.get_component_rich_style("hook-runs--selected").bgcolor

        hooks.select("a")
        await wait_for(lambda: _row_backgrounds(pilot.app, 0) == {selected()}, pilot=pilot, description="marked")
        rows = pilot.app.query_one("#hook-runs-rows", Static)
        updates: list[object] = []
        original: Callable[..., None] = rows.update

        def counting(*args: object, **kwargs: object) -> None:
            updates.append(args)
            original(*args, **kwargs)

        monkeypatch.setattr(rows, "update", counting)
        holder = pilot.app.query_one("#holder")
        holder.display = False
        await wait_for(lambda: not hooks.is_on_screen, pilot=pilot, description="hidden")
        before = selected()
        pilot.app.theme = "textual-dark" if pilot.app.theme == "textual-light" else "textual-light"
        await wait_for(lambda: selected() != before, pilot=pilot, description="the theme's color resolved")
        # A widget the screen cannot show, repainted, makes it lay the whole screen out again.
        assert not await wait_until(lambda: bool(updates), timeout=0.5, pilot=pilot), "painted while hidden"
        holder.display = True
        await _shown(pilot, 2)
        await wait_for(
            lambda: _row_backgrounds(pilot.app, 0) == {selected()}, pilot=pilot, description="painted once shown"
        )
