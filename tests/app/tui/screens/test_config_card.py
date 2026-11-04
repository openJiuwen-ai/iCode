# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for shared agent-config card chrome."""

from __future__ import annotations

import pytest
from textual import on
from textual.app import App, ComposeResult
from textual.widgets import Button, Input, Label
from textual.widgets.collapsible import CollapsibleTitle

from chrys.app.tui.screens.agents.panels.mcp import MCPConnectionCard
from chrys.app.tui.screens.agents.panels.subagents import SubAgentCard
from chrys.app.tui.widgets.config_card import CollapsibleConfigCard, ConfigCard
from tests.support.tui_helpers import click_when_settled
from tests.support.waiting import wait_for


class _ProbeCard(ConfigCard):
    _delete_button_prefix = "probe-delete"

    def compose(self) -> ComposeResult:
        yield from self.compose_header("Probe", row_class="probe-header", title_class="probe-title")


class _ConfigCardApp(App):
    def __init__(self) -> None:
        super().__init__()
        self.removed_indexes: list[int] = []
        self.pressed_ids: list[str] = []

    def compose(self) -> ComposeResult:
        yield _ProbeCard(index=7)

    @on(ConfigCard.RemoveRequested)
    def _on_removed(self, event: ConfigCard.RemoveRequested) -> None:
        self.removed_indexes.append(event.index)

    @on(Button.Pressed)
    def _on_button_pressed(self, event: Button.Pressed) -> None:
        self.pressed_ids.append(event.button.id or "")


class _MultiConfigCardApp(App):
    def compose(self) -> ComposeResult:
        yield _ProbeCard(index=0)
        yield _ProbeCard(index=1)


def test_large_config_cards_use_shared_base() -> None:
    assert issubclass(MCPConnectionCard, ConfigCard)
    assert issubclass(SubAgentCard, ConfigCard)


@pytest.mark.asyncio
async def test_config_card_delete_button_posts_removed_message() -> None:
    async with _ConfigCardApp().run_test() as pilot:
        pilot.app.query_one("#probe-delete-7", Button).press()
        await pilot.pause()

        assert pilot.app.removed_indexes == [7]
        assert pilot.app.pressed_ids == ["probe-delete-7"]


@pytest.mark.asyncio
async def test_config_card_delete_button_ids_include_card_index() -> None:
    async with _MultiConfigCardApp().run_test() as pilot:
        buttons = list(pilot.app.query(Button))

        assert [button.id for button in buttons] == ["probe-delete-0", "probe-delete-1"]


@pytest.mark.asyncio
async def test_config_card_default_css_applies_to_card_node_and_delete_glyph() -> None:
    async with _ConfigCardApp().run_test() as pilot:
        await pilot.pause()

        card = pilot.app.query_one(_ProbeCard)
        title = card.query_one(".config-card-title", Label)
        delete_button = card.query_one("#probe-delete-7", Button)

        assert card.styles.padding.left == 2
        assert card.styles.padding.top == 1
        assert card.styles.background.a > 0
        assert str(title.styles.width) == "1fr"
        assert str(title.styles.text_style) == "bold"
        assert str(delete_button.styles.min_width) == "3"
        assert not delete_button.styles.border
        assert delete_button.styles.background.a == 0


class _ProbeCollapsibleCard(CollapsibleConfigCard):
    _delete_button_prefix = "fold-delete"

    def compose(self) -> ComposeResult:
        yield from self.compose_header("[b]not markup[/b]", row_class="fold-header", title_class="fold-title")
        with self.card_body():
            yield Input(id="fold-input")


class _CollapsibleCardApp(App):
    def __init__(self, *, collapsed: bool, read_only: bool = False) -> None:
        super().__init__()
        self._collapsed = collapsed
        self._read_only = read_only
        self.removed_indexes: list[int] = []

    def compose(self) -> ComposeResult:
        yield _ProbeCollapsibleCard(index=3, collapsed=self._collapsed, read_only=self._read_only)

    @on(ConfigCard.RemoveRequested)
    def _on_removed(self, event: ConfigCard.RemoveRequested) -> None:
        self.removed_indexes.append(event.index)


def _title_text(card: CollapsibleConfigCard) -> str:
    return str(card.query_one(CollapsibleTitle).content)


def _is_folded(card: CollapsibleConfigCard) -> bool:
    body = card.query_one(".config-card-body")
    return card.collapsed and card.has_class("-collapsed") and not body.display and card.region.height == 3


def _is_unfolded(card: CollapsibleConfigCard) -> bool:
    body = card.query_one(".config-card-body")
    return (
        not card.collapsed
        and not card.has_class("-collapsed")
        and body.display
        and card.query_one("#fold-input").region.height > 0
    )


@pytest.mark.asyncio
async def test_collapsible_config_card_folds_to_its_header_row_with_literal_title() -> None:
    async with _CollapsibleCardApp(collapsed=True).run_test() as pilot:
        card = pilot.app.query_one(_ProbeCollapsibleCard)
        await wait_for(lambda: _is_folded(card), pilot=pilot, description="card folded to its header row")

        assert _title_text(card) == "▶ [b]not markup[/b]"
        assert card.query_one("#fold-delete-3", Button).region.height == 1
        # The folded body stays mounted, so readers of its fields keep working.
        assert card.query_one("#fold-input", Input).value == ""

        card.set_title("[/x] renamed")
        assert _title_text(card) == "▶ [/x] renamed"
        card.set_title("tab\x1bname")
        assert "\x1b" not in _title_text(card)


@pytest.mark.asyncio
async def test_collapsible_config_card_click_and_enter_toggle_the_fold() -> None:
    async with _CollapsibleCardApp(collapsed=True).run_test() as pilot:
        card = pilot.app.query_one(_ProbeCollapsibleCard)
        title = card.query_one(CollapsibleTitle)

        await click_when_settled(pilot, title)
        await wait_for(lambda: _is_unfolded(card), pilot=pilot, description="click unfolds the card")
        assert _title_text(card) == "▼ [b]not markup[/b]"
        assert title.has_focus

        await pilot.press("enter")
        await wait_for(lambda: _is_folded(card), pilot=pilot, description="Enter folds the card again")
        assert _title_text(card) == "▶ [b]not markup[/b]"

        # Folding is not a delete: the delete message stays reserved for the ✕ button.
        assert pilot.app.removed_indexes == []
        card.query_one("#fold-delete-3", Button).press()
        await wait_for(lambda: pilot.app.removed_indexes == [3], pilot=pilot, description="delete posts Removed")


@pytest.mark.asyncio
async def test_collapsible_config_card_set_collapsed_is_idempotent_and_read_only_still_unfolds() -> None:
    async with _CollapsibleCardApp(collapsed=False, read_only=True).run_test() as pilot:
        card = pilot.app.query_one(_ProbeCollapsibleCard)
        await wait_for(lambda: _is_unfolded(card), pilot=pilot, description="card starts unfolded")
        assert card.query_one("#fold-delete-3", Button).display is False

        card.set_collapsed(False)
        assert _is_unfolded(card)
        assert _title_text(card) == "▼ [b]not markup[/b]"

        # Read-only cards hide editing controls but keep folding, which only navigates.
        await click_when_settled(pilot, card.query_one(CollapsibleTitle))
        await wait_for(lambda: _is_folded(card), pilot=pilot, description="read-only card folds on click")
