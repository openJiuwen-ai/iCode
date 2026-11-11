# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The background command details dialog, its log tail and the stop confirmation."""

from __future__ import annotations

import dataclasses
import os
import time
from pathlib import Path

import pytest
from rich.table import Table
from textual.app import App
from textual.widgets import Button, Log, Static

from chrys.app.tui.screens.dialogs import background_task
from chrys.app.tui.screens.dialogs.background_task import (
    ROLLBACK_NOTE,
    BackgroundTaskModal,
    LogTail,
    confirm_stopping,
)
from chrys.app.tui.screens.dialogs.confirm import ConfirmDialog
from chrys.app.tui.widgets.task_summary import TaskSummary
from chrys.foundation.models.background_tasks import BackgroundTaskView
from tests.support.pilot_barrier import screen_is_settled
from tests.support.tui_helpers import click_when_settled, rich_plain
from tests.support.waiting import wait_for


def _task(log_path: Path, **fields: object) -> BackgroundTaskView:
    base: dict[str, object] = {
        "id": "t1",
        "command": "npm run dev",
        "reason": "dev server",
        "cwd": "/repo",
        "trigger": "timeout",
        "turn": 1,
        "pids": (4242,),
        "log_path": str(log_path),
        "started_at": time.time() - 65,
    }
    base.update(fields)
    return BackgroundTaskView(**base)  # type: ignore[arg-type]


def test_log_tail_reads_only_what_was_added(tmp_path: Path) -> None:
    log = tmp_path / "x.log"
    log.write_bytes(b"one\ntwo\n")
    tail = LogTail(str(log))
    assert tail.read() == ("one\ntwo\n", False)
    assert tail.read() == ("", False)
    with log.open("ab") as file:
        file.write(b"three\n")
    assert tail.read() == ("three\n", False)


def test_log_tail_starts_over_when_the_log_shrinks(tmp_path: Path) -> None:
    log = tmp_path / "x.log"
    log.write_bytes(b"one\ntwo\n")
    tail = LogTail(str(log))
    tail.read()
    log.write_bytes(b"new\n")
    assert tail.read() == ("new\n", True)


def test_log_tail_starts_over_when_the_log_rotated_to_the_same_size(tmp_path: Path) -> None:
    log = tmp_path / "x.log"
    log.write_bytes(b"old\n")
    tail = LogTail(str(log))
    tail.read()
    # Between two reads the log rotated and its new file grew as large as the old one.
    rotated = tmp_path / "new.log"
    rotated.write_bytes(b"new\n")
    os.replace(rotated, log)
    assert tail.read() == ("new\n", True)


def test_log_tail_skips_ahead_from_a_whole_line(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(background_task, "_TAIL_BYTES", 12)
    log = tmp_path / "x.log"
    log.write_bytes(b"first line\nsecond\nthird\n")
    tail = LogTail(str(log))
    # The last 12 bytes start inside "second": that partial line is dropped.
    assert tail.read() == ("third\n", True)
    with log.open("ab") as file:
        file.write(b"fourth\n")
    assert tail.read() == ("fourth\n", False)


def test_log_tail_skips_ahead_from_a_line_a_carriage_return_ended(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(background_task, "_TAIL_BYTES", 10)
    log = tmp_path / "x.log"
    # Progress a hook rewrites in place, as a detached hook's log keeps it.
    log.write_bytes(b"10%\r20%\r30%\r40%\r50%\r")
    tail = LogTail(str(log))
    # The last 10 bytes start inside "30%": the rest is kept as written, but for the CR
    # it ends on, which waits for the LF that may follow it.
    assert tail.read_raw() == ("40%\r50%", True)


def test_log_tail_joins_a_crlf_split_between_reads(tmp_path: Path) -> None:
    log = tmp_path / "x.log"
    log.write_bytes(b"first\r")
    tail = LogTail(str(log))
    assert tail.read_raw() == ("first", False)
    with log.open("ab") as file:
        file.write(b"\nsecond\r\n")
    assert tail.read_raw() == ("\r\nsecond\r\n", False)
    # A lone CR still ends its line once more is read.
    with log.open("ab") as file:
        file.write(b"50%\r")
    assert tail.read() == ("50%", False)
    with log.open("ab") as file:
        file.write(b"60%")
    assert tail.read() == ("\n60%", False)


def test_log_tail_joins_a_character_split_between_reads(tmp_path: Path) -> None:
    log = tmp_path / "x.log"
    tail = LogTail(str(log))
    chunks = [b"done ", "中文".encode()[:4], "中文".encode()[4:] + b"\n"]
    log.write_bytes(b"")
    read = ""
    for chunk in chunks:
        with log.open("ab") as file:
            file.write(chunk)
        read += tail.read()[0]
    assert read == "done 中文\n"


def test_confirm_stopping_names_each_task_and_the_note() -> None:
    task = _task(Path("/logs/x.log"), command="npm run dev\n  --port 3000", reason="x" * 100)
    dialog = confirm_stopping([task], Static(), note=ROLLBACK_NOTE)
    message = dialog._message
    assert message == "This stops the background task:"
    assert dialog._tasks[0].command == task.command
    assert dialog._tasks[0].reason == task.reason
    assert dialog._tasks[0].duration.startswith("1m ")
    assert dialog._impact_text().endswith("Discarding turns may not restore what they changed while in the background.")

    two = confirm_stopping([task, dataclasses.replace(task, id="t2", reason="")], Static())
    assert two._message == "This stops 2 background tasks:"
    assert len(two._tasks) == 2
    assert "Discard" not in two._impact_text()


@pytest.mark.asyncio
async def test_confirm_stopping_scrolls_a_long_list_right_above_its_buttons() -> None:
    task = _task(Path("/logs/x.log"))
    tasks = [dataclasses.replace(task, id=f"t{n}") for n in range(7)]
    async with App().run_test(size=(100, 30)) as pilot:
        await pilot.app.push_screen(confirm_stopping(tasks, Static()))
        dialog = pilot.app.screen
        buttons = dialog.query_one("#confirm-buttons")
        await wait_for(
            lambda: dialog.is_mounted and screen_is_settled(pilot.app, dialog),
            pilot=pilot,
            description="confirmation sizing and layout completed",
        )

        scroll = pilot.app.screen.query_one("#notice-scroll")
        await wait_for(lambda: scroll.max_scroll_y > 0, pilot=pilot, description="task list scrolls")
        assert scroll.max_scroll_y > 0
        assert buttons.region.y >= scroll.region.bottom
        assert len(pilot.app.screen.query(TaskSummary)) == len(tasks)
        assert scroll.styles.scrollbar_size_vertical == 1


class _ModalApp(App):
    def __init__(self, task: BackgroundTaskView) -> None:
        super().__init__()
        self.current = task
        self.stopped: list[str] = []
        self.modal = BackgroundTaskModal(task, latest=self._latest, stop=self.stopped.append)

    def _latest(self, task_id: str) -> BackgroundTaskView | None:
        return self.current if self.current.id == task_id else None

    def on_mount(self) -> None:
        self.push_screen(self.modal)


def _plain(fields: Static) -> str:
    table = fields.content
    assert isinstance(table, Table)
    return rich_plain(table)


def _log_text(modal: BackgroundTaskModal) -> str:
    return "\n".join(modal.query_one(Log).lines)


@pytest.mark.asyncio
async def test_modal_follows_the_log_and_stops_after_a_confirmation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hold_refresh_callbacks = True
    # Hold post-refresh callbacks to prove previews and actions are ready
    # without a second layout pass.
    invoke_callbacks = ConfirmDialog._invoke_and_clear_callbacks

    async def invoke_when_released(dialog: ConfirmDialog) -> None:
        if not hold_refresh_callbacks:
            await invoke_callbacks(dialog)

    monkeypatch.setattr(ConfirmDialog, "_invoke_and_clear_callbacks", invoke_when_released)
    log = tmp_path / "x.log"
    log.write_bytes(b"listening on :3000\n")
    app = _ModalApp(_task(log))
    async with app.run_test(size=(100, 30)) as pilot:
        modal = app.modal
        await wait_for(lambda: "listening on :3000" in _log_text(modal), pilot=pilot, description="the log shown")
        with log.open("ab") as file:
            file.write(b"GET / 200\n")
        await wait_for(lambda: "GET / 200" in _log_text(modal), pilot=pilot, description="the new line shown")

        await click_when_settled(pilot, "#background-task-stop")
        await wait_for(
            lambda: isinstance(app.screen, ConfirmDialog) and app.screen.is_mounted,
            pilot=pilot,
            description="the confirmation mounted before post-refresh callbacks",
        )
        dialog = app.screen
        try:
            message = dialog.query_one(".task-command", Static)
            assert "npm run dev" in str(message.content)
            await wait_for(lambda: dialog.query_one("#confirm-yes").region.width > 0, pilot=pilot)
            initial_buttons = tuple(button.region for button in dialog.query(Button))
            assert not screen_is_settled(app, dialog)
        finally:
            hold_refresh_callbacks = False
            dialog.check_idle()
        await wait_for(
            lambda: screen_is_settled(app, dialog), pilot=pilot, description="the confirmation previews laid out"
        )
        assert "npm run dev" in str(message.content)
        assert tuple(button.region for button in dialog.query(Button)) == initial_buttons
        await click_when_settled(pilot, "#confirm-yes")
        await wait_for(lambda: app.stopped == ["t1"], pilot=pilot, description="the stop requested")
        assert app.screen is modal


@pytest.mark.asyncio
async def test_modal_disables_stop_once_the_task_ended(tmp_path: Path) -> None:
    log = tmp_path / "x.log"
    log.write_bytes(b"")
    app = _ModalApp(_task(log))
    async with app.run_test(size=(100, 30)) as pilot:
        await wait_for(lambda: app.screen is app.modal and app.modal.is_mounted, pilot=pilot, description="modal shown")
        stop = app.modal.query_one("#background-task-stop", Button)
        assert not stop.disabled

        app.current = dataclasses.replace(app.current, ended_at=time.time(), exit_code=0)
        await wait_for(lambda: stop.disabled, pilot=pilot, description="stop disabled")
        subtitle = app.modal.query_one("#background-task-container").border_subtitle
        assert subtitle is not None
        assert "exit 0" in str(subtitle)


@pytest.mark.asyncio
async def test_modal_follows_the_processes_and_ends_a_task_that_left_the_list(tmp_path: Path) -> None:
    log = tmp_path / "x.log"
    log.write_bytes(b"")
    app = _ModalApp(_task(log))
    async with app.run_test(size=(100, 30)) as pilot:
        await wait_for(lambda: app.screen is app.modal and app.modal.is_mounted, pilot=pilot, description="modal shown")
        fields = app.modal.query_one("#background-task-fields", Static)
        assert "4242" in _plain(fields)

        # Its shell exited first: the processes it left run on.
        app.current = dataclasses.replace(app.current, pids=(7, 8), shell_exit_code=0)
        await wait_for(lambda: "7, 8" in _plain(fields), pilot=pilot, description="the new processes")

        # More ended at once than the list keeps: this one left it without being seen to end.
        app.current = dataclasses.replace(app.current, id="t2")
        stop = app.modal.query_one("#background-task-stop", Button)
        await wait_for(lambda: stop.disabled, pilot=pilot, description="stop disabled")
        assert "ended" in str(app.modal.query_one("#background-task-container").border_subtitle)
