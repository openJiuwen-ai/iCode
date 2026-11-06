# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The Discard dialog labels the first removed turn and explains file restoration."""


# ruff: noqa: RUF001

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from rich.text import Text
from textual.color import Color
from textual.widgets import Button, Checkbox, Select, Static
from textual.widgets._select import SelectCurrent

from chrys.app.tui.i18n import LocaleController
from chrys.app.tui.screens.diff.rollback_modal import RollbackModal
from chrys.app.tui.screens.diff.screen import CheckableTree, DiffPeriodPane
from chrys.app.tui.util.diff_entries import DiffFileEntry
from chrys.foundation.config.settings import Settings
from chrys.service.mutations.store import SnapshotStore
from chrys.service.mutations.tracker import MutationTracker
from chrys.service.mutations.types import MutationOp, MutationSource
from tests.app.tui.screens.test_rollback_modal import _ModalHostApp
from tests.support.tui_helpers import resize_when_settled, rich_plain
from tests.support.waiting import wait_for


def _tracker(root: Path, file_turn: int) -> MutationTracker:
    tracker = MutationTracker(SnapshotStore(root / "store"))
    paths = [root / f"file-{number}.py" for number in range(3)]
    for path in paths:
        path.write_text("old content\n", encoding="utf-8")
    for turn in range(1, 21):
        tracker.start_turn(turn)
        if turn == file_turn:
            for path in paths:
                mutation = tracker.record(str(path), MutationOp.MODIFY, MutationSource.EDIT_FILE, path.name)
                path.write_text("new content\n", encoding="utf-8")
                tracker.record_after(mutation)
    return tracker


@pytest.mark.parametrize("locale", ["en", "zh-Hans"])
@pytest.mark.parametrize(("target", "file_turn"), [(16, 17), (19, 20), (19, 17), (0, 17), (0, 0)])
async def test_dialog_turn_axis_notes_column_titles_and_actions(
    tmp_path: Path,
    locale: str,
    target: int,
    file_turn: int,
) -> None:
    controller = LocaleController(Settings(locale=locale))
    modal = RollbackModal(
        tracker=_tracker(tmp_path, file_turn),
        available_turns=[0, 16, 17, 18, 19],
        current_turn=20,
        initial_selection=target,
        cwd=str(tmp_path),
        turn_prompts={turn: f"Question {turn}" for turn in [1, 17, 18, 19, 20]},
        locale_controller=controller,
    )
    host = _ModalHostApp(modal)
    host.locale_controller = controller
    async with host.run_test(size=(120, 40)) as pilot:
        await wait_for(lambda: modal._content_ready, pilot=pilot, description="undo dialog ready")
        english = locale == "en"
        assert rich_plain(modal.query_one("#rollback-container").border_title) == ("Discard" if english else "撤回")
        select = modal.query_one("#rollback-turn-select", Select)
        assert select.value == target
        labels = [(rich_plain(label), value) for label, value in select._options]
        assert labels == [
            (
                f"Turn {value + 1} — Question {value + 1}" if english else f"第 {value + 1} 轮 — Question {value + 1}",
                value,
            )
            for value in [19, 18, 17, 16, 0]
        ]
        assert select.prompt == ("Discard starting from" if english else "从这一轮开始撤回")
        first = target + 1
        span = str(first) if first == 20 else f"{first}–20"
        range_text = (
            (f"Discard turn {span}" if first == 20 else f"Discard turns {span}") if english else f"撤回第 {span} 轮"
        )
        button = modal.query_one("#btn-rollback", Button)
        assert str(button.label) == (("Discard all turns" if english else "撤回全部") if target == 0 else range_text)
        assert str(modal.query_one("#btn-close", Button).label) == ("Cancel" if english else "取消")
        has_files = file_turn > 0 and file_turn > target
        note = modal.query_one("#rollback-range-note", Static)
        await pilot.pause()
        assert note.region.y == select.region.bottom + 1
        # The note hangs from the picker: its "└─" starts one column before the selected turn's text.
        glyph = modal.query_one("#rollback-range-glyph", Static)
        assert rich_plain(glyph.render()) == "└─"
        assert glyph.region.y == note.region.y
        assert glyph.content_region.x == select.query_one(SelectCurrent).query_one("#label").content_region.x - 1
        assert note.content_region.x == glyph.content_region.x + 3
        assert note.styles.color == Color.parse(host.get_css_variables()["text-muted"])
        assert note.styles.text_style.dim
        if not has_files:
            assert rich_plain(note.render()) == range_text
            assert not modal.query_one("#revert-checkbox", Checkbox).display
            assert rich_plain(modal.query_one("#rollback-diff-placeholder", Static).render()) == (
                "No files to restore." if english else "没有要恢复的文件。"
            )
        else:
            suffix = (
                f"; checked files go back to before turn {first}"
                if english
                else f"，勾选的文件改回第 {first} 轮之前的内容"
            )
            assert rich_plain(note.render()) == range_text + suffix
            columns = [rich_plain(item.render()) for item in modal.query(".diff-column-title")]
            assert columns == (["Now", "After discard"] if english else ["现在", "撤回后"])
            checkbox = modal.query_one("#revert-checkbox", Checkbox)
            assert str(checkbox.label) == ("Also restore 3 checked files" if english else "同时恢复勾选的 3 个文件")
            pane = modal.query_one(DiffPeriodPane)
            tree = modal.query_one(".workspace-tree", CheckableTree)
            leaves = [node for node in tree.root.children if isinstance(node.data, DiffFileEntry)]
            pane.toggle_check_node(leaves[0])
            await pilot.pause()
            assert str(checkbox.label) == ("Also restore 2 checked files" if english else "同时恢复勾选的 2 个文件")
            checkbox.value = False
            await pilot.pause()
            assert rich_plain(note.render()) == range_text + (
                "; files stay as they are" if english else "，文件保持不变"
            )
            assert pane.selected_paths() == []
        button.press()
        await wait_for(lambda: host.dismiss_result != "<unset>", pilot=pilot, description="undo confirmation delivered")
        assert host.dismiss_result == (target, False, None)


@pytest.mark.parametrize("locale", ["en", "zh-Hans"])
@pytest.mark.parametrize("deferred", [False, True])
async def test_pruned_picker_has_only_retained_turns_and_keyboard_can_choose(
    tmp_path: Path, locale: str, deferred: bool
) -> None:
    controller = LocaleController(Settings(locale=locale))
    tracker = _tracker(tmp_path, 17)

    async def load_state() -> SimpleNamespace:
        return SimpleNamespace(
            tracker=tracker,
            available_turns=[16, 17, 18, 19],
            current_turn=20,
            workspace_cwd=str(tmp_path),
            turn_prompts={17: "Earlier", 18: "Next"},
            plan_augment=None,
            attribution_refresh=None,
            projection_acquire=None,
            projection_release=None,
        )

    modal = RollbackModal(
        tracker=None if deferred else tracker,
        available_turns=[] if deferred else [16, 17, 18, 19],
        current_turn=20,
        cwd=str(tmp_path),
        turn_prompts={17: "Earlier", 18: "Next"},
        load_state=load_state if deferred else None,
        locale_controller=controller,
    )
    host = _ModalHostApp(modal)
    host.locale_controller = controller
    async with host.run_test(size=(100, 35)) as pilot:
        await wait_for(lambda: modal._content_ready, pilot=pilot, description="pruned dialog ready")
        select = modal.query_one("#rollback-turn-select", Select)
        assert select.value == 19
        assert [value for _, value in select._options] == [19, 18, 17, 16]
        assert all("Session start" not in rich_plain(label) for label, _ in select._options)
        select.focus()
        await pilot.press("enter", "down", "enter")
        await wait_for(
            lambda: modal._content_ready and not modal._preview_refreshing and select.value == 18,
            pilot=pilot,
            description="keyboard chose next retained turn",
        )
        assert modal._selected_turn == 18


@pytest.mark.parametrize("locale", ["en", "zh-Hans"])
async def test_range_note_too_long_for_its_line_shows_only_the_discarded_turns(tmp_path: Path, locale: str) -> None:
    controller = LocaleController(Settings(locale=locale))
    modal = RollbackModal(
        tracker=_tracker(tmp_path, 17),
        available_turns=[0, 16, 17, 18, 19],
        current_turn=20,
        initial_selection=16,
        cwd=str(tmp_path),
        turn_prompts={},
        locale_controller=controller,
    )
    host = _ModalHostApp(modal)
    host.locale_controller = controller
    english = locale == "en"
    short = "Discard turns 17–20" if english else "撤回第 17–20 轮"
    full = short + ("; checked files go back to before turn 17" if english else "，勾选的文件改回第 17 轮之前的内容")
    async with host.run_test(size=(60, 30)) as pilot:
        await wait_for(lambda: modal._content_ready, pilot=pilot, description="undo dialog ready")
        note = modal.query_one("#rollback-range-note", Static)
        assert Text(full).cell_len > note.content_region.width
        assert rich_plain(note.render()) == short
        assert note.region.height == 1
        await resize_when_settled(pilot, 120, 30)
        await wait_for(lambda: rich_plain(note.render()) == full, pilot=pilot, description="whole note once it fits")
        # The glyph keeps its column either way.
        glyph = modal.query_one("#rollback-range-glyph", Static)
        assert note.content_region.x == glyph.content_region.x + 3
