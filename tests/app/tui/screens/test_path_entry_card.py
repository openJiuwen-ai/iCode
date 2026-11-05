# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for shared agent-config path entry cards."""

from __future__ import annotations

import asyncio

import pytest
from textual.app import App, ComposeResult
from textual.widget import Widget
from textual.widgets import Button, Label

from chrys.app.tui.screens.agents.panels.memory import MemoryFileCard, MemoryFolderCard
from chrys.app.tui.screens.agents.panels.skills import SkillDirCard
from chrys.app.tui.widgets.config_card import ConfigCard
from chrys.app.tui.widgets.path_entry import PathEntryCard
from tests.support.waiting import wait_for


class _PathEntryCardApp(App):
    def compose(self) -> ComposeResult:
        yield SkillDirCard("skills", index=0, workspace_cwd=None)


def test_config_path_cards_use_shared_base() -> None:
    assert issubclass(MemoryFileCard, PathEntryCard)
    assert issubclass(MemoryFolderCard, PathEntryCard)
    assert issubclass(SkillDirCard, PathEntryCard)
    assert issubclass(PathEntryCard, ConfigCard)


def test_path_entry_card_returns_seed_before_children_mount() -> None:
    skill = SkillDirCard("skills", index=0, workspace_cwd=None)
    memory_file = MemoryFileCard("notes.md", index=0, workspace_cwd=None)
    memory_folder = MemoryFolderCard("docs", index=0)

    assert skill.get_path() == "skills"
    assert skill.is_relative_checked() is True
    assert memory_file.get_path() == "notes.md"
    assert memory_file.is_relative_checked() is True
    assert memory_folder.get_path() == "docs"
    assert memory_folder.is_relative_checked() is True


@pytest.mark.asyncio
async def test_path_entry_card_uses_shared_config_card_chrome() -> None:
    async with _PathEntryCardApp().run_test() as pilot:
        await pilot.pause()

        card = pilot.app.query_one(SkillDirCard)
        title = card.query_one(".config-card-title", Label)
        delete_button = card.query_one("#sk-delete-btn-0", Button)

        assert card.styles.padding.left == 2
        assert card.styles.background.a > 0
        assert str(title.styles.width) == "1fr"
        assert str(title.styles.text_style) == "bold"
        assert str(delete_button.styles.min_width) == "3"
        assert not delete_button.styles.border
        assert delete_button.styles.background.a == 0


@pytest.mark.parametrize("card_type", [SkillDirCard, MemoryFileCard])
async def test_path_card_removed_during_compose_does_not_refresh_missing_children(
    card_type: type[PathEntryCard],
) -> None:
    composing = asyncio.Event()
    release = asyncio.Event()
    previews: list[PathEntryCard] = []

    class HeldCard(card_type):
        async def mount_composed_widgets(self, widgets: list[Widget]) -> None:
            composing.set()
            await release.wait()
            await super().mount_composed_widgets(widgets)

        def _refresh_preview(self) -> None:
            previews.append(self)
            super()._refresh_preview()

    async with App().run_test() as pilot:
        card = HeldCard("relative/path", index=0)
        mounted = pilot.app.mount(card)
        try:
            await wait_for(composing.is_set, description="path card paused before child mounting")
            removed = card.remove()
            assert card._pruning
        finally:
            release.set()
        await mounted
        await removed
        assert not previews

        # A live replacement still runs the inherited mount handler and renders its preview.
        replacement = HeldCard("relative/path", index=0)
        await pilot.app.mount(replacement)
        assert replacement in previews
        assert replacement.query_one(f"#{replacement._preview_id}", Label).display
