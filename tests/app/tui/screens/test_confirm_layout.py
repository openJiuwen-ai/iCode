# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Responsive confirmations keep tasks readable and actions reachable."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import create_autospec

import pytest
from rich.cells import cell_len
from textual.app import App
from textual.containers import VerticalScroll
from textual.widgets import Button, Static, Tooltip

from chrys.app.tui import i18n as tui_i18n
from chrys.app.tui.i18n import LocaleController
from chrys.app.tui.screens.dialogs.background_task import confirm_stopping
from chrys.app.tui.screens.dialogs.confirm import ConfirmDialog, NoticeDialog, StartupSessionChoiceDialog
from chrys.app.tui.screens.dialogs.editor import EditorDialog
from chrys.app.tui.widgets.chat.panel import ChatPanel
from chrys.app.tui.widgets.chat.tool_call import ToolCall
from chrys.app.tui.widgets.chrome.footer import ChrysFooter
from chrys.app.tui.widgets.editor import EditorBufferSnapshot
from chrys.app.tui.widgets.task_summary import (
    TaskSummary,
    TaskSummaryData,
    display_command,
    preview_text,
)
from chrys.foundation.config.settings import Settings
from chrys.foundation.models.background_tasks import BackgroundTaskView
from tests.support.pilot_barrier import screen_is_settled
from tests.support.tui_app_harness import make_chrys_app
from tests.support.tui_helpers import click_when_settled, resize_when_settled
from tests.support.waiting import wait_for

if TYPE_CHECKING:
    from rich.console import RenderableType
    from textual.geometry import Region
    from textual.screen import Screen


class _ConfirmationFrameApp(App):
    def __init__(self, locale: str = "en") -> None:
        self.locale_controller = LocaleController(Settings(locale=locale))
        self.painted_sizes: list[tuple[int, int]] = []
        self.painted_button_regions: list[tuple[Region, ...]] = []
        super().__init__()

    def _display(self, screen: Screen, renderable: RenderableType | None) -> None:
        super()._display(screen, renderable)
        if renderable is not None and isinstance(screen, ConfirmDialog):
            region = screen.query_one("#confirm-container").region
            if region.width:
                self.painted_sizes.append((region.width, region.height))
                self.painted_button_regions.append(tuple(button.region for button in screen.query(Button)))


@pytest.mark.parametrize(("keys", "start_new"), [("enter", False), ("left enter", True), ("escape", False)])
async def test_startup_session_choice_retries_until_new_is_selected(keys: str, start_new: bool) -> None:
    app = _ConfirmationFrameApp()
    dialog = StartupSessionChoiceDialog(confirm_label="Start new session", cancel_label="Retry")
    choices: list[bool | None] = []
    async with app.run_test() as pilot:
        await app.push_screen(dialog, choices.append)
        await wait_for(lambda: screen_is_settled(app, dialog), pilot=pilot)
        assert dialog.focused is dialog.query_one("#confirm-no", Button)
        await pilot.press(*keys.split())
        await wait_for(lambda: bool(choices), pilot=pilot)

    assert choices == [start_new]


@pytest.mark.parametrize("locale", ["en", "zh-Hans"])
@pytest.mark.parametrize("size", [(120, 36), (60, 30), (40, 30), (40, 14)])
async def test_editor_discard_confirmation_has_its_final_size_on_every_frame(
    locale: str, size: tuple[int, int]
) -> None:
    app = _ConfirmationFrameApp(locale)
    async with app.run_test(size=size) as pilot:
        await app.push_screen(EditorDialog(EditorBufferSnapshot("draft", (0, 0))))
        await click_when_settled(pilot, "#editor-cancel")
        dialog = app.screen
        assert isinstance(dialog, ConfirmDialog)
        await wait_for(
            lambda: dialog.is_mounted and screen_is_settled(app, dialog) and bool(app.painted_sizes),
            pilot=pilot,
        )
        final = dialog.query_one("#confirm-container").region
        expected_width = 43 if locale == "zh-Hans" else 80
        assert final.width == min(expected_width, int(size[0] * 0.9))
        assert set(app.painted_sizes) == {(final.width, final.height)}
        buttons = tuple(button.region for button in dialog.query(Button))
        assert set(app.painted_button_regions) == {buttons}
        assert all(button.bottom <= final.bottom for button in buttons)


@pytest.mark.parametrize("screen_width", [120, 60, 40])
@pytest.mark.parametrize(
    ("dialog_type", "title", "message", "tasks", "expected_width"),
    [
        (ConfirmDialog, "Confirm", "Short message", (), 43),
        (ConfirmDialog, "Confirm", "Long message " * 4, (), 80),
        (ConfirmDialog, "Long title " * 8, "Short message", (), 100),
        (NoticeDialog, "Notice", "Short message", (), 64),
        (NoticeDialog, "Notice", "Long notice line\n" * 70, (), 64),
        (ConfirmDialog, "Stop tasks", "Stop?", (TaskSummaryData("command " * 30),), 100),
    ],
)
async def test_confirmation_layout_is_final_from_first_painted_frame(
    screen_width: int,
    dialog_type: type[ConfirmDialog],
    title: str,
    message: str,
    tasks: tuple[TaskSummaryData, ...],
    expected_width: int,
) -> None:
    app = _ConfirmationFrameApp()
    dialog = dialog_type(title=title, message=message, tasks=tasks)
    async with app.run_test(size=(screen_width, 36)) as pilot:
        await app.push_screen(dialog)
        await wait_for(
            lambda: dialog.is_mounted and screen_is_settled(app, dialog) and bool(app.painted_sizes),
            pilot=pilot,
        )
        assert {width for width, _height in app.painted_sizes} == {min(expected_width, int(screen_width * 0.9))}
        final = dialog.query_one("#confirm-container").region
        assert set(app.painted_sizes) == {(final.width, final.height)}
        assert set(app.painted_button_regions) == {tuple(button.region for button in dialog.query(Button))}


@pytest.mark.parametrize("text", ["abc " * 200, "中文测试 " * 100, "👩‍💻é " * 100, "你\t好\t" * 100])
def test_preview_keeps_both_ends_within_two_cell_bounded_lines(text: str) -> None:
    original = "START " + text + " END"
    preview, truncated = preview_text(original, 23)
    assert truncated
    assert preview.startswith("START ") and preview.endswith(" END")
    assert len(preview.splitlines()) == 2
    assert all(cell_len(line) <= 23 for line in preview.splitlines())
    assert "…" in preview and "\t" not in preview


def test_preview_preserves_short_commands_and_uses_cell_tab_stops() -> None:
    assert display_command("你\t好") == "你  好"
    assert preview_text("python -m http.server 8080 --bind 127.0.0.1", 25) == (
        "python -m http.server \n8080 --bind 127.0.0.1",
        False,
    )
    assert preview_text("first\nsecond\nthird", 20)[1]
    assert preview_text("🇨🇳" * 10, 3) == ("🇨🇳…\n🇨🇳", True)
    assert preview_text("👩🏽‍💻" * 10, 3) == ("👩🏽‍💻…\n👩🏽‍💻", True)


@pytest.mark.parametrize("dialog_type", [ConfirmDialog, NoticeDialog])
@pytest.mark.parametrize("size", [(120, 40), (80, 24), (40, 24)])
async def test_task_dialog_keeps_all_rows_and_fixed_actions(
    dialog_type: type[ConfirmDialog], size: tuple[int, int]
) -> None:
    tasks = tuple(
        TaskSummaryData("uv run pytest " + "tests/long_path.py " * 100 + "--last", "32s", "a reason " * 40)
        for _ in range(12)
    )
    dialog = dialog_type(
        message="Stop these tasks?", tasks=tasks, impact=("This stops tasks and their child processes.",)
    )
    async with App().run_test(size=size) as pilot:
        await pilot.app.push_screen(dialog)
        await wait_for(lambda: dialog.is_mounted and screen_is_settled(pilot.app, dialog), pilot=pilot)
        container = dialog.query_one("#confirm-container")
        scroll = dialog.query_one("#notice-scroll", VerticalScroll)
        assert len(dialog.query(TaskSummary)) == 12
        assert container.region.width <= int(size[0] * 0.9)
        assert container.region.height <= int(size[1] * 0.9)
        assert scroll.max_scroll_y > 0
        for row in dialog.query(TaskSummary):
            assert row.query_one(".task-command").region.height <= 2
            assert row.query_one(".task-reason").region.height == 1
            assert not row.query_one(".task-meta").display
        impact = dialog.query_one("#confirm-impact")
        for selector in ("#confirm-yes", "#confirm-no"):
            button = dialog.query_one(selector, Button)
            assert button.region.y >= impact.region.bottom
            assert button.region.bottom <= container.region.bottom
            assert pilot.app.get_widget_at(button.region.x, button.region.y)[0] is button
        if size[0] == 40:
            assert dialog.query_one("#confirm-no").region.y > dialog.query_one("#confirm-yes").region.y


async def test_truncated_command_has_full_tooltip_and_no_click_action() -> None:
    original = "echo '中文\t[literal]'\n" + "--path long/ " * 100 + " END"
    dialog = NoticeDialog(tasks=tuple(TaskSummaryData(original) for _ in range(10)))
    async with App().run_test(size=(100, 30), tooltips=True) as pilot:
        await pilot.app.push_screen(dialog)
        await wait_for(lambda: dialog.is_mounted and screen_is_settled(pilot.app, dialog), pilot=pilot)
        scroll = dialog.query_one("#notice-scroll", VerticalScroll)
        scroll.scroll_end(animate=False)
        await wait_for(lambda: scroll.scroll_y == scroll.max_scroll_y, pilot=pilot)
        position = scroll.scroll_y
        button = list(dialog.query(".task-command"))[-1]
        assert str(button.tooltip) == display_command(original)
        await click_when_settled(pilot, button)
        assert pilot.app.screen is dialog
        assert len(dialog.query(Button)) == 2
        assert dialog._tasks[0].command == original
        assert scroll.scroll_y == position
        await pilot.hover(button, offset=(1, 0))
        await wait_for(lambda: dialog.query_one(Tooltip).display, pilot=pilot)
        assert str(dialog.query_one(Tooltip).content) == display_command(original)
        await pilot.hover(dialog, offset=(0, 0))
        await wait_for(lambda: not dialog.query_one(Tooltip).display, pilot=pilot)
        await resize_when_settled(pilot, 50, 24)
        await wait_for(lambda: dialog.query_one("#confirm-container").region.width <= 45, pilot=pilot)
        assert len(dialog.query(TaskSummary)) == 10


async def test_short_confirmation_starts_at_43_columns() -> None:
    async with App().run_test(size=(120, 40)) as pilot:
        dialog = ConfirmDialog()
        await pilot.app.push_screen(dialog)
        await wait_for(lambda: dialog.is_mounted and screen_is_settled(pilot.app, dialog), pilot=pilot)
        assert dialog.query_one("#confirm-container").region.width == 43


async def test_locale_switch_refreshes_tasks_impact_and_button_layout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tui_i18n, "persist_locale", lambda locale: None)

    class LocalizedApp(App):
        locale_controller = LocaleController(Settings(locale="en"))

    task = BackgroundTaskView(
        id="test",
        command="uv run pytest " + "tests/long.py " * 80,
        reason="tests",
        cwd="/repo",
        trigger="timeout",
        turn=1,
        pids=(),
        log_path="",
        started_at=0,
    )
    async with LocalizedApp().run_test(size=(50, 30)) as pilot:
        dialog = confirm_stopping([task], pilot.app.screen)
        await pilot.app.push_screen(dialog)
        await wait_for(lambda: dialog.is_mounted and screen_is_settled(pilot.app, dialog), pilot=pilot)
        assert "Running for" in str(dialog.query_one(".task-meta", Static).content)
        pilot.app.locale_controller.switch_locale("zh-Hans")
        await wait_for(lambda: screen_is_settled(pilot.app, dialog), pilot=pilot)
        assert "已运行" in str(dialog.query_one(".task-meta", Static).content)
        assert dialog.query_one(".task-command").tooltip is not None
        assert "子进程" in str(dialog.query_one("#confirm-impact", Static).content)
        assert str(dialog.query_one("#confirm-yes", Button).label) == "停止并继续"
        assert dialog.query_one("#confirm-no").region.bottom <= dialog.query_one("#confirm-container").region.bottom


async def test_reason_and_runtime_share_a_line_and_reason_wins_when_narrow() -> None:
    dialog = ConfirmDialog(tasks=(TaskSummaryData("python server.py", "32s", "Start the static server"),))
    async with App().run_test(size=(100, 30)) as pilot:
        await pilot.app.push_screen(dialog)
        await wait_for(lambda: dialog.is_mounted and screen_is_settled(pilot.app, dialog), pilot=pilot)
        row = dialog.query_one(TaskSummary)
        reason = row.query_one(".task-reason", Static)
        runtime = row.query_one(".task-meta", Static)
        assert reason.region.y == runtime.region.y
        assert reason.region.x == row.content_region.x
        assert runtime.region.right == row.content_region.right
        assert runtime.display
        assert row.query_one(".task-command").tooltip is None
        await resize_when_settled(pilot, 35, 24)
        await wait_for(lambda: not runtime.display and reason.region.width == row.content_region.width, pilot=pilot)
        assert "Start the static server" in str(reason.content)


async def test_task_confirmation_does_not_refresh_populated_chat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = make_chrys_app(tmp_path / "sessions")
    async with app.run_test(size=(100, 32)) as pilot:
        main = app.screen
        chat = main.query_one(ChatPanel)
        cards = [
            ToolCall(f"task-preview-{index}", "read_file", args={"path": f"file{index}.py"}) for index in range(30)
        ]
        await chat.mount(*cards)
        for card in cards:
            card.set_complete("File contents\n" * 20)
        footer = main.query_one(ChrysFooter)
        await wait_for(
            lambda: (
                screen_is_settled(app, main)
                and not footer._binding_recompose_in_progress
                and not footer._binding_recompose_dirty
            ),
            pilot=pilot,
        )
        spies = []
        for target, method, original in (
            (main, "_refresh_layout", main._refresh_layout),
            (main, "update_node_styles", main.update_node_styles),
            (footer, "recompose", footer.recompose),
        ):
            spy = create_autospec(original, side_effect=original)
            monkeypatch.setattr(target, method, spy)
            spies.append(spy)
        dialog = NoticeDialog(tasks=(TaskSummaryData("pytest " + "tests/long.py " * 80, "32s", "tests"),))
        await app.push_screen(dialog)
        await wait_for(lambda: dialog.is_mounted and screen_is_settled(app, dialog), pilot=pilot)
        await click_when_settled(pilot, "#confirm-no")
        await wait_for(lambda: app.screen is main and screen_is_settled(app, main), pilot=pilot)
        assert all(spy.call_count == 0 for spy in spies)
