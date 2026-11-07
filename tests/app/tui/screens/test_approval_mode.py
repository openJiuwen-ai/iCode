# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for the approval mode picker."""

from __future__ import annotations

import pytest
from textual.app import App, ComposeResult
from textual.widgets import OptionList, Static
from textual.widgets.option_list import Option

from chrys.app.tui.screens.dialogs.approval.mode import ApprovalModeScreen
from chrys.app.tui.widgets.option_menu import MenuOptionList
from chrys.service.approval.policy import ApprovalMode


def test_approval_mode_selection_dismisses_once(monkeypatch: pytest.MonkeyPatch) -> None:
    screen = ApprovalModeScreen(ApprovalMode.MANUAL)
    results: list[ApprovalMode | None] = []

    def _dismiss(result: ApprovalMode | None = None) -> None:
        results.append(result)

    monkeypatch.setattr(screen, "dismiss", _dismiss)
    event = OptionList.OptionSelected(
        OptionList(),
        Option("BYPASS", id=ApprovalMode.BYPASS.value),
        0,
    )

    screen._on_selected(event)
    screen._on_selected(event)
    screen.action_cancel()
    screen._dismiss_clicked_outside()

    assert results == [ApprovalMode.BYPASS]


@pytest.mark.asyncio
async def test_bypass_option_says_relay_enhanced_review_is_inactive_for_relay_models() -> None:
    """With a relay model active, the bypass option must not imply protection."""

    class _Harness(App):
        def __init__(self, relay_active: bool) -> None:
            super().__init__()
            self._relay_active = relay_active

        def compose(self) -> ComposeResult:
            yield ApprovalModeScreen(ApprovalMode.AUTO, relay_active=self._relay_active)

    # Undo the menu's soft wrapping: compare with all whitespace removed.
    def _flat(text: str) -> str:
        return "".join(text.split())

    async with _Harness(True).run_test() as pilot:
        await pilot.pause()
        rendered = pilot.app.query_one(ApprovalModeScreen).query_one(MenuOptionList).options
        by_id = {option.id: str(option.prompt) for option in rendered}
    assert "relay-enhancedreviewisNOTactive" in _flat(by_id["bypass"])

    async with _Harness(False).run_test() as pilot:
        await pilot.pause()
        rendered = pilot.app.query_one(ApprovalModeScreen).query_one(MenuOptionList).options
        by_id = {option.id: str(option.prompt) for option in rendered}
    assert "relay-enhancedreview" not in _flat(by_id["bypass"])


@pytest.mark.asyncio
async def test_approval_mode_duplicate_queued_selection_events_complete_once() -> None:
    class _Harness(App):
        def __init__(self) -> None:
            super().__init__()
            self.approval_screen: ApprovalModeScreen | None = None
            self.results: list[ApprovalMode | None] = []

        def compose(self) -> ComposeResult:
            yield Static("base")

        def on_mount(self) -> None:
            self.approval_screen = ApprovalModeScreen(ApprovalMode.MANUAL)
            self.push_screen(self.approval_screen, self.results.append)

    async with _Harness().run_test() as pilot:
        await pilot.pause()
        screen = pilot.app.approval_screen
        assert screen is not None
        option_list = screen.query_one(OptionList)
        option_id = ApprovalMode.BYPASS.value
        option = option_list.get_option(option_id)
        option_index = option_list.get_option_index(option_id)

        option_list.post_message(OptionList.OptionSelected(option_list, option, option_index))
        option_list.post_message(OptionList.OptionSelected(option_list, option, option_index))
        await pilot.pause()

    assert pilot.app.results == [ApprovalMode.BYPASS]


@pytest.mark.asyncio
async def test_approval_mode_scrollbar_is_one_cell_and_flush_right() -> None:
    class _Harness(App):
        def compose(self) -> ComposeResult:
            yield Static("base")

        def on_mount(self) -> None:
            self.push_screen(ApprovalModeScreen(ApprovalMode.MANUAL))

    async with _Harness().run_test(size=(80, 8)) as pilot:
        await pilot.pause()
        option_list = pilot.app.screen.query_one(OptionList)
        container = pilot.app.screen.query_one("#container")

        assert option_list.show_vertical_scrollbar is True
        assert option_list.styles.scrollbar_size_vertical == 1
        assert option_list.vertical_scrollbar.region.right == container.content_region.right
