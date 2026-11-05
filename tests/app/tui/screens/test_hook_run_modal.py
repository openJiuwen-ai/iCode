# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The hook run details dialog: its Input and Output tabs, polling, log tail and click-to-copy sections (plan section 4)."""

from __future__ import annotations

import dataclasses
import inspect
import json
import time
from dataclasses import replace
from pathlib import Path

import pytest
from rich.text import Text
from textual.app import App
from textual.content import Content
from textual.widgets import Button, Log, Static, TabbedContent

from chrys.app.tui.screens.dialogs.hook_run import HookRunModal, _CopySection
from chrys.app.tui.widgets.chat.tool_call import fmt_duration
from chrys.foundation.models.hook_runs import HookRunApplied, HookRunDetail, HookRunIO, HookRunRecord
from chrys.service.trajectory.hooks import HookOutcome
from tests.support.tui_helpers import click_when_settled, rich_plain
from tests.support.waiting import wait_for, wait_until

_PAYLOAD = '{"event":"before_tool_call","tool":{"name":"zsh","args":{"command":"rm -rf build"}}}'


def _record(**fields: object) -> HookRunRecord:
    now = time.monotonic()
    base: dict[str, object] = {
        "run_id": "run-1",
        "session_id": "session-abc",
        "hook_id": "guard",
        "hook_event": "before_tool_call",
        "execution_mode": "blocking",
        "detach": False,
        "tool_name": "zsh",
        "sub_agent": "",
        "retry": False,
        "registered_at": now - 3.0,
        "started_at": now - 3.0,
        "ended_at": now - 2.5,
        "registered_wall": time.time() - 3.0,
        "state": "ended",
        "outcome": HookOutcome.SUCCESS,
        "exit_code": 0,
        "effect": None,
        "number": 7,
    }
    base.update(fields)
    return HookRunRecord(**base)  # type: ignore[arg-type]


def _io(**fields: object) -> HookRunIO:
    base: dict[str, object] = {
        "payload": _PAYLOAD,
        "payload_size": len(_PAYLOAD),
        "result_file": "missing",
        "decision": "",
        "stdout": "",
        "stderr": "",
        "stdout_dropped": 0,
        "stderr_dropped": 0,
    }
    base.update(fields)
    return HookRunIO(**base)  # type: ignore[arg-type]


def _detail(record: HookRunRecord | None = None, **fields: object) -> HookRunDetail:
    record = record or _record()
    base: dict[str, object] = {
        "record": record,
        "tool_call_id": "call-42",
        "timeout_seconds": 30.0,
        "on_error": "warn",
        "delivery": "best_effort",
        "source": "global",
        "source_path": "/config/hooks/hooks.yaml",
        "run_type": "command",
        "run_argv": ("python3", "guard.py", "--strict"),
        "run_cwd": "/repo",
        "run_env_names": ("API_TOKEN",),
        "log_path": "",
        "shared_log": False,
        "io": _io(),
        "applied": HookRunApplied() if record.state == "ended" else None,
    }
    base.update(fields)
    return HookRunDetail(**base)  # type: ignore[arg-type]


class _CountedModal(HookRunModal):
    """Counts the ticks of its poll timer: they stop only once the timer is parked."""

    # A relative CSS_PATH resolves beside the class that names it: this module's directory.
    CSS_PATH = Path(inspect.getfile(HookRunModal)).with_name(str(HookRunModal.CSS_PATH))
    polls = 0

    async def _poll(self) -> None:
        self.polls += 1
        await super()._poll()


class _ModalApp(App):
    def __init__(self, detail: HookRunDetail) -> None:
        super().__init__()
        self.current: HookRunDetail | None = detail
        self.asked: list[str] = []
        self.modal = _CountedModal(detail, latest=self._latest)

    def _latest(self, run_id: str) -> HookRunDetail | None:
        self.asked.append(run_id)
        return self.current

    def on_mount(self) -> None:
        self.push_screen(self.modal)


def _plain(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, Content):
        return content.plain
    if isinstance(content, Text):
        return content.plain
    return rich_plain(content)  # type: ignore[arg-type]


def _pane_text(modal: HookRunModal, pane: str) -> str:
    parts: list[str] = []
    for widget in modal.query_one(f"#{pane}").query("*"):
        if isinstance(widget, _CopySection):
            parts.append(_plain(widget.border_title))
        elif isinstance(widget, Static):
            parts.append(_plain(widget.content))
        elif isinstance(widget, Log):
            parts.append("\n".join(widget.lines))
    return "\n".join(parts)


def _active(modal: HookRunModal) -> str:
    return modal.query_one(TabbedContent).active


def _subtitle(modal: HookRunModal) -> str:
    return str(modal.query_one("#hook-run-container").border_subtitle or "")


def _title(modal: HookRunModal) -> str:
    return str(modal.query_one("#hook-run-container").border_title or "")


async def _open(pilot, app: _ModalApp) -> HookRunModal:
    await wait_for(lambda: app.screen is app.modal and app.modal.is_mounted, pilot=pilot, description="modal shown")
    return app.modal


# --- Title, tab choice, Input ---------------------------------------------------


async def test_an_ended_run_opens_on_input_with_its_title_and_state() -> None:
    record = _record(outcome=HookOutcome.FAILED, exit_code=3)
    app = _ModalApp(_detail(record, io=_io(stderr="boom"), applied=HookRunApplied(not_applied="failed")))
    async with app.run_test(size=(120, 40)) as pilot:
        modal = await _open(pilot, app)
        assert _title(modal) == "Hook #7 · guard"
        # The first tab from the first paint on: no switch to Output after it.
        assert not await wait_until(lambda: _active(modal) != "hook-run-input", pilot=pilot, timeout=0.5)
        subtitle = _subtitle(modal)
        assert "exit 3" in subtitle
        assert "500ms" in subtitle or "0.5s" in subtitle, subtitle


@pytest.mark.parametrize("state", ["running", "queued"])
async def test_a_live_run_opens_on_input_and_says_its_output_comes_later(state: str) -> None:
    now = time.monotonic()
    started = now - 1.0 if state == "running" else None
    record = _record(state=state, started_at=started, ended_at=None, outcome=None, exit_code=None)
    app = _ModalApp(_detail(record, applied=None))
    async with app.run_test(size=(120, 40)) as pilot:
        modal = await _open(pilot, app)
        assert _active(modal) == "hook-run-input"
        assert "Running… output shows when the hook ends" in _pane_text(modal, "hook-run-output")
        assert state in _subtitle(modal)


async def test_the_input_tab_shows_the_configured_run_and_the_payload() -> None:
    record = _record(registered_at=time.monotonic() - 10.0, started_at=time.monotonic() - 4.0)
    record = dataclasses.replace(record, registered_wall=time.mktime((2026, 10, 21, 9, 30, 0, 0, 0, -1)))
    app = _ModalApp(_detail(record, delivery="durable", on_error="block", timeout_seconds=12.0))
    async with app.run_test(size=(140, 50)) as pilot:
        modal = await _open(pilot, app)
        text = _pane_text(modal, "hook-run-input")
        for expected in (
            "Event",
            "before_tool_call",
            "Mode",
            "blocking",
            "12",
            "block",
            "durable",
            "Session",
            "session-abc",
            "Triggered by",
            "Main agent",
            "zsh",
            "call-42",
            "Started",
            "09:30:06",
            "Queued",
            "Source",
            "global",
            "/config/hooks/hooks.yaml",
            "Run",
            "guard.py",
            "--strict",
            "/repo",
            "API_TOKEN",
            "Payload",
        ):
            assert expected in text, (expected, text)
        assert "Retry of an earlier run" not in text
        # Pretty-printed: one key per line.
        assert '"command": "rm -rf build"' in text
        assert "\n" in text.split("Payload", 1)[1]


async def test_the_input_tab_names_a_sub_agent_and_a_retry_and_hides_start_and_queue_when_unknown() -> None:
    now = time.monotonic()
    record = _record(sub_agent="Explore", retry=True, registered_at=now - 2.0, started_at=None, state="queued")
    record = dataclasses.replace(record, ended_at=None, outcome=None, exit_code=None)
    app = _ModalApp(_detail(record, applied=None))
    async with app.run_test(size=(140, 50)) as pilot:
        modal = await _open(pilot, app)
        text = _pane_text(modal, "hook-run-input")
        assert "Explore" in text
        assert "Main agent" not in text
        assert "Retry of an earlier run" in text
        assert "Started" not in text, "still queued: no start time"


async def test_the_queue_time_of_a_run_still_queued_counts_up() -> None:
    record = _record(registered_at=time.monotonic() - 2.0, started_at=None, state="queued")
    record = dataclasses.replace(record, ended_at=None, outcome=None, exit_code=None)
    app = _ModalApp(_detail(record, applied=None))
    async with app.run_test(size=(140, 50)) as pilot:
        modal = await _open(pilot, app)

        def queued() -> list[list[str]]:
            lines = _pane_text(modal, "hook-run-input").splitlines()
            return [line.split() for line in lines if line.startswith("Queued")]

        opened = queued()
        assert len(opened) == 1, opened
        await wait_for(lambda: queued() != opened, pilot=pilot, description="the queue time counted up")
        assert queued()[0][0] == "Queued"


async def test_a_run_that_never_queued_shows_no_queue_time() -> None:
    record = _record()
    app = _ModalApp(_detail(record))
    async with app.run_test(size=(140, 50)) as pilot:
        modal = await _open(pilot, app)
        text = _pane_text(modal, "hook-run-input")
        assert "Started" in text
        assert "Queued" not in text


async def test_a_detached_run_shows_when_it_was_handed_off_and_no_queue_time() -> None:
    record = _record(detach=True, outcome=HookOutcome.DETACHED, exit_code=None, started_at=None)
    app = _ModalApp(_detail(record, io=_io()))
    async with app.run_test(size=(140, 50)) as pilot:
        modal = await _open(pilot, app)
        text = _pane_text(modal, "hook-run-input")
        assert "Started" in text
        assert "Queued" not in text


async def test_a_run_that_ended_before_it_took_a_slot_shows_how_long_it_queued() -> None:
    registered = time.monotonic() - 5.0
    record = _record(
        registered_at=registered,
        started_at=None,
        ended_at=registered + 2.5,
        outcome=HookOutcome.CANCELLED,
        exit_code=None,
    )
    app = _ModalApp(_detail(record))
    async with app.run_test(size=(140, 50)) as pilot:
        modal = await _open(pilot, app)
        lines = _pane_text(modal, "hook-run-input").splitlines()
        # Until it ended, not until now, and not nothing.
        assert [line.split() for line in lines if line.startswith("Queued")] == [["Queued", fmt_duration(2500)]], lines
        assert not any(line.startswith("Started") for line in lines), "it never started"


async def test_a_cut_payload_says_so() -> None:
    io = _io(payload=_PAYLOAD[:40], payload_size=200_000)
    app = _ModalApp(_detail(io=io))
    async with app.run_test(size=(140, 50)) as pilot:
        modal = await _open(pilot, app)
        assert "Payload cut at 64 KB of" in _pane_text(modal, "hook-run-input")


# --- Output ---------------------------------------------------------------------


async def test_the_output_tab_shows_the_decision_what_was_applied_and_both_streams() -> None:
    decision = {"action": "block", "reason": "rm denied"}
    io = _io(
        result_file="ok",
        decision=json.dumps(decision),
        stdout="checked 3 paths",
        stderr="warning: slow disk",
        stdout_dropped=1234,
    )
    record = _record(effect="blocked")
    app = _ModalApp(_detail(record, io=io, applied=HookRunApplied(block_reason="hook 'guard': rm denied")))
    async with app.run_test(size=(140, 60)) as pilot:
        modal = await _open(pilot, app)
        text = _pane_text(modal, "hook-run-output")
        for expected in (
            "Result",
            "blocked",
            "Decision",
            '"reason": "rm denied"',
            "Applied",
            "rm denied",
            "stdout",
            "checked 3 paths",
            "1234 bytes from the middle were not kept",
            "stderr",
            "warning: slow disk",
        ):
            assert expected in text, (expected, text)
        assert "No result file" not in text


@pytest.mark.parametrize(
    ("status", "expected"),
    [("missing", "No result file"), ("invalid", "The result file was invalid and was ignored")],
)
async def test_no_or_an_invalid_result_file_is_named(status: str, expected: str) -> None:
    app = _ModalApp(_detail(io=_io(result_file=status)))
    async with app.run_test(size=(140, 50)) as pilot:
        modal = await _open(pilot, app)
        assert expected in _pane_text(modal, "hook-run-output")


@pytest.mark.parametrize(
    ("applied", "expected"),
    [
        (HookRunApplied(args='{"path": "/safe", "mode": "r"}'), '"path": "/safe"'),
        (HookRunApplied(reminder="remember the style guide"), "remember the style guide"),
        (HookRunApplied(extra_context="ruff found 2 issues"), "ruff found 2 issues"),
        (HookRunApplied(not_applied="nonblocking"), "Async and fire_and_forget hooks can't change the action"),
        (HookRunApplied(not_applied="unsupported"), "before_tool_call doesn't support this action"),
        (HookRunApplied(not_applied="failed"), "The hook failed, so its result file was ignored"),
        (
            HookRunApplied(not_applied="failed_blocked"),
            "The hook failed and on_error is block, so the action was blocked",
        ),
    ],
    ids=["args", "reminder", "context", "nonblocking", "unsupported", "failed", "failed-blocked"],
)
async def test_applied_names_what_was_taken_or_why_not(applied: HookRunApplied, expected: str) -> None:
    effect = (
        "modified_args"
        if applied.args
        else "added_reminder"
        if applied.reminder
        else "added_context"
        if applied.extra_context
        else "blocked"
        if applied.not_applied == "failed_blocked"
        else None
    )
    outcome = HookOutcome.FAILED if applied.not_applied in ("failed", "failed_blocked") else HookOutcome.SUCCESS
    record = _record(effect=effect, outcome=outcome, exit_code=1 if outcome == HookOutcome.FAILED else 0)
    app = _ModalApp(_detail(record, applied=applied, io=_io(result_file="ok", decision="{}")))
    async with app.run_test(size=(140, 50)) as pilot:
        modal = await _open(pilot, app)
        assert expected in _pane_text(modal, "hook-run-output")


async def test_a_change_that_got_the_call_rejected_says_so_after_the_change() -> None:
    record = replace(_record(effect="modified_args"), call_rejected=True)
    app = _ModalApp(
        _detail(record, applied=HookRunApplied(args='{"path": "/x"}'), io=_io(result_file="ok", decision="{}"))
    )
    async with app.run_test(size=(140, 50)) as pilot:
        modal = await _open(pilot, app)
        text = _pane_text(modal, "hook-run-output")
        assert '"path": "/x"' in text
        assert "Hooks kept changing the arguments after the user continued, so the tool call was rejected." in text


def _section_texts(modal: HookRunModal, pane: str, title: str) -> list[str]:
    """What each widget of the section titled *title* shows, in order."""
    (section,) = [section for section in _sections(modal, pane) if _plain(section.border_title) == title]
    return [_plain(widget.content) for widget in section.query(Static)]


_GONE = "Details are no longer kept for this run"


@pytest.mark.parametrize(
    ("effect", "label"),
    [
        ("modified_args", "Changed the tool arguments to:"),
        ("added_reminder", "Added a reminder for the model:"),
        ("added_context", "Added to the tool result:"),
    ],
    ids=["args", "reminder", "context"],
)
async def test_applied_text_whose_details_are_gone_says_so_instead_of_showing_an_empty_block(
    effect: str, label: str
) -> None:
    # As the run log trims a run: its texts go with the rest of its details.
    record = _record(effect=effect)
    app = _ModalApp(_detail(record, io=None, applied=HookRunApplied()))
    async with app.run_test(size=(140, 50)) as pilot:
        modal = await _open(pilot, app)
        assert _section_texts(modal, "hook-run-output", "Applied") == [label, _GONE]


@pytest.mark.parametrize(
    ("args", "args_size", "shown"),
    [
        (
            '{"path": "/safe", "content": "aaaa',
            200_000,
            ["Arguments cut at 64 KB of 195.3 KB", '{"path": "/safe", "content": "aaaa'],
        ),
        ('{"path": "/safe"}', 0, ['{"path": "/safe"}']),
    ],
    ids=["cut", "whole"],
)
async def test_cut_arguments_say_how_much_was_kept_above_what_was(args: str, args_size: int, shown: list[str]) -> None:
    record = _record(effect="modified_args")
    applied = HookRunApplied(args=args, args_size=args_size)
    app = _ModalApp(_detail(record, applied=applied, io=_io(result_file="ok", decision="{}")))
    async with app.run_test(size=(140, 50)) as pilot:
        modal = await _open(pilot, app)
        assert _section_texts(modal, "hook-run-output", "Applied") == ["Changed the tool arguments to:", *shown]


async def test_a_timed_out_run_shows_its_partial_output() -> None:
    record = _record(outcome=HookOutcome.TIMED_OUT, exit_code=None)
    app = _ModalApp(_detail(record, io=_io(stdout="half done"), applied=HookRunApplied(not_applied="failed")))
    async with app.run_test(size=(140, 50)) as pilot:
        modal = await _open(pilot, app)
        text = _pane_text(modal, "hook-run-output")
        assert "timed out" in text
        assert "half done" in text


async def test_a_run_whose_details_are_gone_says_so_and_keeps_the_configured_fields() -> None:
    app = _ModalApp(_detail(io=None))
    async with app.run_test(size=(140, 50)) as pilot:
        modal = await _open(pilot, app)
        gone = "Details are no longer kept for this run"
        assert gone in _pane_text(modal, "hook-run-output")
        text = _pane_text(modal, "hook-run-input")
        assert gone in text
        assert "guard.py" in text and "before_tool_call" in text
        assert "rm -rf build" not in text


# --- Polling --------------------------------------------------------------------


async def test_a_running_run_fills_its_output_once_it_ends_without_switching_tab() -> None:
    now = time.monotonic()
    record = _record(state="running", started_at=now - 1.0, ended_at=None, outcome=None, exit_code=None)
    app = _ModalApp(_detail(record, applied=None))
    async with app.run_test(size=(140, 50)) as pilot:
        modal = await _open(pilot, app)
        await wait_for(lambda: app.asked, timeout=3.0, pilot=pilot, description="latest() polled")
        assert app.asked[0] == "run-1"
        ended = dataclasses.replace(record, state="ended", ended_at=time.monotonic(), outcome=HookOutcome.SUCCESS)
        app.current = _detail(ended, io=_io(stdout="all clean"), applied=HookRunApplied())
        await wait_for(
            lambda: "all clean" in _pane_text(modal, "hook-run-output"),
            timeout=4.0,
            pilot=pilot,
            description="output filled",
        )
        assert "Running…" not in _pane_text(modal, "hook-run-output")
        assert _active(modal) == "hook-run-input", "the user may be reading the payload"
        # Not only the reads: a timer left running would wake the dialog every second.
        polled = app.modal.polls
        assert not await wait_until(lambda: app.modal.polls > polled, timeout=2.5, pilot=pilot), "polling stops"


async def test_a_run_the_log_drops_while_its_dialog_polls_says_its_details_are_gone_and_stops_polling() -> None:
    now = time.monotonic()
    record = _record(state="running", started_at=now - 1.0, ended_at=None, outcome=None, exit_code=None)
    app = _ModalApp(_detail(record, applied=None))
    async with app.run_test(size=(140, 50)) as pilot:
        modal = await _open(pilot, app)
        await wait_for(lambda: app.asked, timeout=3.0, pilot=pilot, description="latest() polled")
        assert "running" in _subtitle(modal)
        # It ended and was dropped past the log's cap between two reads.
        app.current = None
        await wait_for(
            lambda: _GONE in _pane_text(modal, "hook-run-output"),
            timeout=4.0,
            pilot=pilot,
            description="the output says the details are gone",
        )
        assert "Running…" not in _pane_text(modal, "hook-run-output")
        assert _subtitle(modal) == "", "how it ended is not known"
        assert _active(modal) == "hook-run-input", "the tab stays where the user is"
        # Not only the reads: a timer left running would wake the dialog every second.
        polled = app.modal.polls
        assert not await wait_until(lambda: app.modal.polls > polled, timeout=2.5, pilot=pilot), "polling stops"


async def test_a_payload_shown_before_the_log_dropped_the_runs_details_still_copies(copied: list[str]) -> None:
    now = time.monotonic()
    record = _record(state="running", started_at=now - 1.0, ended_at=None, outcome=None, exit_code=None)
    app = _ModalApp(_detail(record, applied=None))
    async with app.run_test(size=(140, 50)) as pilot:
        modal = await _open(pilot, app)
        await wait_for(lambda: app.asked, timeout=3.0, pilot=pilot, description="latest() polled")
        # It ended, and the log dropped its details for newer runs' between two reads.
        ended = dataclasses.replace(record, state="ended", ended_at=time.monotonic(), outcome=HookOutcome.SUCCESS)
        app.current = _detail(ended, io=None, applied=None)
        await wait_for(
            lambda: _GONE in _pane_text(modal, "hook-run-output"),
            timeout=4.0,
            pilot=pilot,
            description="the output says the details are gone",
        )
        assert _active(modal) == "hook-run-input"
        assert '"rm -rf build"' in _pane_text(modal, "hook-run-input")
        await pilot.press("c")
        await wait_for(lambda: len(copied) == 1, pilot=pilot, description="input copied")
        assert copied[0].endswith("\n\n## Payload\n" + json.dumps(json.loads(_PAYLOAD), indent=2)), copied[0][-300:]


# --- Detached runs --------------------------------------------------------------


async def test_a_detached_run_tails_its_log_and_notes_a_shared_one(tmp_path: Path) -> None:
    log = tmp_path / "handed-off.20261021T093000Z.log"
    log.write_text("worker started\n", encoding="utf-8")
    record = _record(detach=True, outcome=HookOutcome.DETACHED, exit_code=None, started_at=None)
    app = _ModalApp(_detail(record, log_path=str(log), shared_log=True, io=_io()))
    async with app.run_test(size=(140, 50)) as pilot:
        modal = await _open(pilot, app)
        await wait_for(
            lambda: "worker started" in _pane_text(modal, "hook-run-output"),
            timeout=4.0,
            pilot=pilot,
            description="log tail",
        )
        text = _pane_text(modal, "hook-run-output")
        assert "Log" in text and str(log) in text
        assert "Another run of this hook started in the same second and writes to this log too" in text


# --- Click to copy --------------------------------------------------------------


@pytest.fixture
def copied(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    seen: list[str] = []
    monkeypatch.setattr("chrys.app.tui.clipboard.clipboard_copy", seen.append)
    return seen


def _sections(modal: HookRunModal, pane: str) -> list[_CopySection]:
    return list(modal.query_one(f"#{pane}").query(_CopySection))


def _notices(app: App) -> list[str]:
    return [str(notification.message) for notification in app._notifications]


async def _ended_with_output(pilot) -> HookRunModal:
    """Open the dialog and click its Output tab, as a user does: it opens on Input."""
    modal = await _open(pilot, pilot.app)
    tabs = modal.query_one(TabbedContent)
    await click_when_settled(pilot, modal.query_one("#--content-tab-hook-run-output"))
    await wait_for(lambda: tabs.active == "hook-run-output", pilot=pilot, description="on output")
    return modal


_OUTPUT_IO = {"result_file": "ok", "decision": '{"action":"allow"}', "stdout": "out-text\nline2", "stderr": "err-text"}


async def test_the_dialog_has_no_copy_buttons() -> None:
    app = _ModalApp(_detail(io=_io(**_OUTPUT_IO)))
    async with app.run_test(size=(140, 50)) as pilot:
        modal = await _ended_with_output(pilot)
        assert not modal.query(Button)


async def test_a_click_anywhere_in_a_section_copies_that_section_and_says_which(copied: list[str]) -> None:
    app = _ModalApp(_detail(io=_io(**_OUTPUT_IO)))
    async with app.run_test(size=(140, 50)) as pilot:
        modal = await _ended_with_output(pilot)
        result, decision, _applied, stdout, _stderr = _sections(modal, "hook-run-output")

        # On the body, not the heading: the whole section is the target.
        await click_when_settled(pilot, stdout.children[-1])
        await wait_for(lambda: copied == ["out-text\nline2"], pilot=pilot, description="stdout copied")
        assert "Copied stdout" in _notices(app)
        await click_when_settled(pilot, decision.children[0])
        await wait_for(lambda: copied[1:] == ['{"action":"allow"}'], pilot=pilot, description="decision copied")
        await click_when_settled(pilot, result.children[0])
        await wait_for(lambda: len(copied) == 3, pilot=pilot, description="result copied")
        assert copied[2].startswith("✓ succeeded")

        modal.query_one(TabbedContent).active = "hook-run-input"
        overview, payload = _sections(modal, "hook-run-input")
        await click_when_settled(pilot, payload.children[-1])
        await wait_for(lambda: len(copied) == 4, pilot=pilot, description="payload copied")
        assert json.loads(copied[3]) == json.loads(_PAYLOAD)
        assert "Copied Payload" in _notices(app)
        await click_when_settled(pilot, overview.children[0])
        await wait_for(lambda: len(copied) == 5, pilot=pilot, description="overview copied")
        assert copied[4].splitlines()[0] == "Event: before_tool_call"
        assert "Payload" not in copied[4]


async def test_a_click_that_ends_a_text_selection_copies_nothing_more(
    copied: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _ModalApp(_detail(io=_io(**_OUTPUT_IO)))
    async with app.run_test(size=(140, 50)) as pilot:
        modal = await _ended_with_output(pilot)
        stdout = _sections(modal, "hook-run-output")[3]
        with monkeypatch.context() as selecting:
            selecting.setattr(type(modal), "get_selected_text", lambda self: "out")
            await click_when_settled(pilot, stdout.children[-1])
            assert not await wait_until(lambda: bool(copied), timeout=0.5, pilot=pilot)
        await click_when_settled(pilot, stdout.children[-1])
        await wait_for(lambda: copied == ["out-text\nline2"], pilot=pilot, description="copied once not selecting")


async def test_the_mouse_over_any_part_of_a_section_lights_the_whole_section() -> None:
    app = _ModalApp(_detail(io=_io(**_OUTPUT_IO)))
    async with app.run_test(size=(140, 50)) as pilot:
        modal = await _ended_with_output(pilot)
        _, decision, applied, *_ = _sections(modal, "hook-run-output")
        await pilot.hover(decision.children[-1])
        await wait_for(lambda: decision.has_class("-hover"), pilot=pilot, description="lit from its body")
        await pilot.hover(decision.children[0])
        assert decision.has_class("-hover")
        await pilot.hover(applied.children[0])
        await wait_for(
            lambda: applied.has_class("-hover") and not decision.has_class("-hover"),
            pilot=pilot,
            description="the light moves to the next section",
        )


async def test_a_detached_log_another_run_shares_after_the_dialog_opened_says_so_and_keeps_its_lines(
    tmp_path: Path,
) -> None:
    log = tmp_path / "handed-off.20261021T093000Z.log"
    log.write_bytes(b"first run\n")
    record = _record(detach=True, outcome=HookOutcome.DETACHED, exit_code=None, started_at=None)
    detail = _detail(record, log_path=str(log), io=_io())
    app = _ModalApp(detail)
    async with app.run_test(size=(140, 50)) as pilot:
        modal = await _ended_with_output(pilot)
        view = modal.query_one("#hook-run-log", Log)
        await wait_for(lambda: "first run" in view.lines, timeout=4.0, pilot=pilot, description="log tail")
        assert "writes to this log too" not in _pane_text(modal, "hook-run-output")
        # The same hook ran again in the same second: the run log marks this run's log shared.
        app.current = dataclasses.replace(detail, shared_log=True)
        await wait_for(
            lambda: "writes to this log too" in _pane_text(modal, "hook-run-output"),
            timeout=4.0,
            pilot=pilot,
            description="the shared log noted",
        )
        assert modal.query_one("#hook-run-log", Log) is view
        assert "first run" in view.lines


async def test_a_detached_run_copies_its_log_path_and_its_log_apart(copied: list[str], tmp_path: Path) -> None:
    log = tmp_path / "x.log"
    log.write_text("worker started\n", encoding="utf-8")
    record = _record(detach=True, outcome=HookOutcome.DETACHED, exit_code=None, started_at=None)
    app = _ModalApp(_detail(record, log_path=str(log), shared_log=True, io=_io()))
    async with app.run_test(size=(140, 50)) as pilot:
        modal = await _ended_with_output(pilot)
        await wait_for(
            lambda: "worker started" in _pane_text(modal, "hook-run-output"), pilot=pilot, description="log tail"
        )
        *_, path, tail = _sections(modal, "hook-run-output")
        await click_when_settled(pilot, path.children[0])
        await wait_for(lambda: copied == [str(log)], pilot=pilot, description="log path copied")
        assert "Copied log path" in _notices(app)
        await click_when_settled(pilot, tail.children[0])
        await wait_for(lambda: len(copied) == 2, pilot=pilot, description="log copied")
        assert copied[1] == "worker started", copied
        await pilot.press("c")
        await wait_for(lambda: len(copied) == 3, pilot=pilot, description="whole tab copied")
        assert f"## Log file\n{log}\n\n## Log\nworker started" in copied[2], copied[2]


async def test_c_copies_the_whole_open_tab(copied: list[str]) -> None:
    app = _ModalApp(_detail(io=_io(**_OUTPUT_IO)))
    async with app.run_test(size=(140, 50)) as pilot:
        modal = await _ended_with_output(pilot)
        await pilot.press("c")
        await wait_for(lambda: len(copied) == 1, pilot=pilot, description="output copied")
        output = copied[0]
        assert output.startswith("## Result\n✓ succeeded")
        sections = ["## Decision\n", "## Applied\n", "## stdout\nout-text", "## stderr\nerr-text"]
        positions = [output.find(section) for section in sections]
        assert -1 not in positions and positions == sorted(positions), output
        assert "## Payload" not in output

        modal.query_one(TabbedContent).active = "hook-run-input"
        await wait_for(lambda: _active(modal) == "hook-run-input", pilot=pilot, description="input tab")
        await pilot.press("c")
        await wait_for(lambda: len(copied) == 2, pilot=pilot, description="input copied")
        fields, payload = copied[1].split("\n\n## Payload\n")
        assert fields.splitlines()[:2] == ["## Overview", "Event: before_tool_call"]
        assert json.loads(payload) == json.loads(_PAYLOAD)


# --- The run's own text: shown sanitized, copied as it is -----------------------


async def test_control_characters_in_the_runs_paths_show_neutralized_and_copy_as_they_are(copied: list[str]) -> None:
    detail = _detail(
        source_path="/config/hooks\x1b[31m/hooks.yaml",
        run_cwd="/repo\x1b]0;owned\x07",
        log_path="/logs/guard\x1b[2J.log",
    )
    app = _ModalApp(detail)
    async with app.run_test(size=(140, 50)) as pilot:
        modal = await _ended_with_output(pilot)
        output = _pane_text(modal, "hook-run-output")
        assert "/logs/guard" in output and "[2J.log" in output, output
        assert "\x1b" not in output, output
        modal.query_one(TabbedContent).active = "hook-run-input"
        await wait_for(lambda: _active(modal) == "hook-run-input", pilot=pilot, description="input tab")
        shown = _pane_text(modal, "hook-run-input")
        assert "[31m/hooks.yaml" in shown and "]0;owned" in shown, shown
        assert "\x1b" not in shown and "\x07" not in shown, shown
        await pilot.press("c")
        await wait_for(lambda: len(copied) == 1, pilot=pilot, description="input copied")
        assert "/config/hooks\x1b[31m/hooks.yaml" in copied[0] and "cwd: /repo\x1b]0;owned\x07" in copied[0]


async def test_a_multi_line_shell_hook_shows_and_copies_its_script_line_by_line(copied: list[str]) -> None:
    script = "set -e\n\techo one\necho two"
    app = _ModalApp(_detail(run_type="shell", run_argv=(script,)))
    async with app.run_test(size=(140, 50)) as pilot:
        modal = await _ended_with_output(pilot)
        modal.query_one(TabbedContent).active = "hook-run-input"
        await wait_for(lambda: _active(modal) == "hook-run-input", pilot=pilot, description="input tab")
        fields = rich_plain(modal.query_one("#hook-run-input-fields", Static).content)
        assert "�" not in fields, fields
        lines = fields.splitlines()
        (run,) = [index for index, line in enumerate(lines) if line.endswith("shell: set -e")]
        column = lines[run].index("shell:")
        # Each line of the script under the first, its tab an indent.
        assert lines[run + 1 : run + 3] == [" " * column + "    echo one", " " * column + "echo two"], lines
        await pilot.press("c")
        await wait_for(lambda: len(copied) == 1, pilot=pilot, description="input copied")
        # The script as it is, each line under its first as the copied fields put them.
        indent = " " * len("Run: ")
        assert f"Run: shell: set -e\n{indent}\techo one\n{indent}echo two\n" in copied[0], copied[0]


async def test_a_multi_line_block_reason_shows_line_by_line_and_copies_as_it_is(copied: list[str]) -> None:
    reason = "hook 'guard' failed (exit 1): denied\n\x1b[31mrm -rf\x1b[0m is not allowed"
    record = _record(effect="blocked")
    app = _ModalApp(
        _detail(record, io=_io(result_file="ok", decision="{}"), applied=HookRunApplied(block_reason=reason))
    )
    async with app.run_test(size=(140, 50)) as pilot:
        modal = await _ended_with_output(pilot)
        label, shown = _section_texts(modal, "hook-run-output", "Applied")
        assert label == "Blocked the action:"
        first, second = shown.splitlines()
        assert first == "hook 'guard' failed (exit 1): denied"
        assert "\x1b" not in second and "rm -rf" in second and second.endswith(" is not allowed"), second
        await pilot.press("c")
        await wait_for(lambda: len(copied) == 1, pilot=pilot, description="output copied")
        assert f"Blocked the action:\n{reason}" in copied[0], copied[0]


async def test_a_payload_path_that_is_not_utf8_shows_without_a_lone_surrogate() -> None:
    # A POSIX path with a byte that is not UTF-8, as JSON writes it: no terminal can be sent it unescaped.
    payload = json.dumps({"tool": {"name": "read_file", "args": {"path": "/tmp/caf\udce9"}}})
    app = _ModalApp(_detail(io=_io(payload=payload, payload_size=len(payload))))
    async with app.run_test(size=(140, 50)) as pilot:
        modal = await _ended_with_output(pilot)
        modal.query_one(TabbedContent).active = "hook-run-input"
        await wait_for(lambda: _active(modal) == "hook-run-input", pilot=pilot, description="input tab")
        shown = _pane_text(modal, "hook-run-input")
        assert "/tmp/caf" in shown, shown
        assert not any(0xD800 <= ord(char) <= 0xDFFF for char in shown), shown


async def test_a_payload_that_indenting_would_swell_shows_compact(copied: list[str]) -> None:
    # Indenting adds the depth to every line: 200 levels would grow from some 400 characters to 80,000.
    payload = json.dumps({"tool": {"name": "write", "args": {"x": json.loads("[" * 200 + "]" * 200)}}})
    app = _ModalApp(_detail(io=_io(payload=payload, payload_size=len(payload))))
    async with app.run_test(size=(140, 50)) as pilot:
        modal = await _ended_with_output(pilot)
        modal.query_one(TabbedContent).active = "hook-run-input"
        await wait_for(lambda: _active(modal) == "hook-run-input", pilot=pilot, description="input tab")
        await pilot.press("c")
        await wait_for(lambda: len(copied) == 1, pilot=pilot, description="input copied")
        assert copied[0].endswith("\n\n## Payload\n" + payload), copied[0][-200:]


@pytest.mark.parametrize(
    ("stdout", "first", "last"),
    [
        ("".join(f"line {number}\n" for number in range(3_000)), "line 0\n", "line 2999\n"),
        ("a" * 40_000 + "z" * 40_000, "a", "z"),
    ],
    ids=["many-lines", "one-long-line"],
)
async def test_a_long_block_shows_its_ends_and_copies_whole(
    copied: list[str], stdout: str, first: str, last: str
) -> None:
    """Laid out whole, 256 KB of short lines held the dialog for seconds before it showed."""
    app = _ModalApp(_detail(io=_io(stdout=stdout)))
    async with app.run_test(size=(140, 50)) as pilot:
        modal = await _ended_with_output(pilot)
        head, note, tail = _section_texts(modal, "hook-run-output", "stdout")
        assert head.startswith(first) and tail.endswith(last)
        assert stdout.startswith(head) and stdout.endswith(tail)
        assert max(len(head), len(tail)) <= 32 * 1024
        assert max(len(head.splitlines()), len(tail.splitlines())) <= 500
        left_out = len(stdout) - len(head) - len(tail)
        assert left_out > 0
        assert note == f"{left_out} characters from the middle are not shown; a copy includes them"
        await pilot.press("c")
        await wait_for(lambda: len(copied) == 1, pilot=pilot, description="output copied")
        assert f"## stdout\n{stdout}\n\n## stderr" in copied[0]


async def test_a_detached_log_shows_sanitized_and_copies_as_it_was_written(tmp_path: Path, copied: list[str]) -> None:
    log = tmp_path / "handed-off.20261021T093000Z.log"
    written = "col1\tcol2\n\x1b[31mred\x1b[0m\n"
    # Bytes, as a hook writes them: text mode would write CRLF on Windows.
    log.write_bytes(written.encode())
    record = _record(detach=True, outcome=HookOutcome.DETACHED, exit_code=None, started_at=None)
    app = _ModalApp(_detail(record, log_path=str(log), io=_io()))
    async with app.run_test(size=(140, 50)) as pilot:
        modal = await _ended_with_output(pilot)
        await wait_for(
            lambda: "red" in "\n".join(modal.query_one("#hook-run-log", Log).lines),
            timeout=4.0,
            pilot=pilot,
            description="log tail",
        )
        shown = "\n".join(modal.query_one("#hook-run-log", Log).lines)
        assert "\t" not in shown and "\x1b" not in shown, shown
        (section,) = [
            section for section in _sections(modal, "hook-run-output") if _plain(section.border_title) == "Log"
        ]
        await click_when_settled(pilot, section.children[-1])
        await wait_for(lambda: len(copied) == 1, pilot=pilot, description="log copied")
        assert copied[0] == written.rstrip("\n")


async def test_a_detached_log_line_too_long_to_show_ends_in_a_mark_and_copies_whole(
    tmp_path: Path, copied: list[str]
) -> None:
    """Measured again on every write, a line growing into megabytes held the dialog longer at each read."""
    log = tmp_path / "handed-off.20261021T093000Z.log"
    written = "x" * 100_000 + "\nnext\n"
    log.write_bytes(written.encode())
    record = _record(detach=True, outcome=HookOutcome.DETACHED, exit_code=None, started_at=None)
    app = _ModalApp(_detail(record, log_path=str(log), io=_io()))
    async with app.run_test(size=(140, 50)) as pilot:
        modal = await _ended_with_output(pilot)
        widget = modal.query_one("#hook-run-log", Log)
        await wait_for(lambda: "next" in widget.lines, timeout=4.0, pilot=pilot, description="log tail")
        assert widget.lines[:2] == ["x" * 64 * 1024 + "…", "next"]
        await pilot.press("c")
        await wait_for(lambda: len(copied) == 1, pilot=pilot, description="output copied")
        assert "\n## Log\n" + written.rstrip("\n") in copied[0]


# Five lines kept: the copy holds the lines the widget shows, however the log ends them.
@pytest.mark.parametrize(
    ("written", "kept"),
    [
        ("".join(f"{percent}%\r" for percent in range(12)) + "done\n", "9%\r10%\r11%\rdone"),
        ("a\nb\nc\nd\ne\nf\n", "c\nd\ne\nf"),
        ("a\nb\n\n\n", "a\nb\n\n"),
    ],
    ids=["carriage-returns", "newlines", "blank-lines"],
)
async def test_a_detached_logs_copy_keeps_the_lines_its_view_keeps(
    tmp_path: Path, copied: list[str], monkeypatch: pytest.MonkeyPatch, written: str, kept: str
) -> None:
    monkeypatch.setattr("chrys.app.tui.screens.dialogs.hook_run._MAX_LOG_LINES", 5)
    log = tmp_path / "progress.20261021T093000Z.log"
    log.write_bytes(written.encode())
    record = _record(detach=True, outcome=HookOutcome.DETACHED, exit_code=None, started_at=None)
    app = _ModalApp(_detail(record, log_path=str(log), io=_io()))
    async with app.run_test(size=(140, 50)) as pilot:
        modal = await _ended_with_output(pilot)
        view = modal.query_one("#hook-run-log", Log)
        await wait_for(lambda: len(view.lines) == 5, timeout=4.0, pilot=pilot, description="log tail")
        (section,) = [
            section for section in _sections(modal, "hook-run-output") if _plain(section.border_title) == "Log"
        ]
        await click_when_settled(pilot, section.children[-1])
        await wait_for(lambda: len(copied) == 1, pilot=pilot, description="log copied")
        assert copied[0] == kept
        assert [*kept.replace("\r", "\n").split("\n"), ""] == list(view.lines)
