# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Messages and file changes share the Chat tab as two independent framed sections."""

from __future__ import annotations

from dataclasses import replace

import pytest
from rich.cells import cell_len
from textual.app import App, ComposeResult
from textual.containers import Vertical
from textual.geometry import Region
from textual.widgets import Static, Tree

from chrys.app.tui import i18n as tui_i18n
from chrys.app.tui.i18n import LocaleController
from chrys.app.tui.theme import CHRYS_ANSI_THEME, CHRYS_THEME
from chrys.app.tui.util.diff_entries import DiffFileEntry
from chrys.app.tui.widgets.sidebar.diff_preview import DiffPreview, SidebarChatPanel
from chrys.app.tui.widgets.sidebar.panel import SidebarPanel
from chrys.app.tui.widgets.sidebar.toc import ConversationToc, TocItem
from chrys.foundation.config.settings import Settings
from chrys.service.mutations.types import MutationOp
from tests.support.pilot_barrier import screen_is_settled
from tests.support.tui_helpers import WidgetApp, click_when_settled, resize_when_settled
from tests.support.waiting import wait_for


def changed_file(name: str) -> DiffFileEntry:
    return DiffFileEntry(
        path=f"/repo/{name}",
        rel_path=name,
        operation=MutationOp.MODIFY,
        old_path=None,
        before_text="before",
        after_text="after",
        is_binary=False,
        encoding="utf-8",
        line_count_result=(1, 1),
    )


class ChatNavigationApp(App):
    def __init__(self) -> None:
        super().__init__()
        self.selected: list[str] = []

    def compose(self) -> ComposeResult:
        yield SidebarChatPanel()

    def on_conversation_toc_turn_selected(self, event: ConversationToc.TurnSelected) -> None:
        self.selected.append(event.turn_id)


async def test_empty_messages_frame_contains_a_centered_nonselectable_prompt() -> None:
    async with WidgetApp(SidebarChatPanel).run_test(size=(60, 24)) as pilot:
        chat = pilot.app.query_one(SidebarChatPanel)
        toc = chat.query_one(ConversationToc)
        box = toc.query_one("#toc-box", Vertical)
        empty = box.query_one(".toc-empty", Static)
        await wait_for(
            lambda: box.region.height == 24 and screen_is_settled(pilot.app, pilot.app.screen),
            pilot=pilot,
            description="empty Messages frame laid out",
        )
        assert box.border_title == "Messages"
        assert box.styles.border_top[0] == "round"
        assert empty.parent is box
        assert box.query_one("#toc-tree", Tree).parent is box
        assert empty.display and not box.query_one(Tree).display
        assert not empty.allow_select
        assert empty.region == box.content_region
        assert not chat.query_one(DiffPreview).display

        message = "Your conversation will appear here"
        rendered = empty.render_lines(Region(0, 0, empty.size.width, empty.size.height))
        occupied = [(index, row.text) for index, row in enumerate(rendered) if message in row.text]
        assert len(occupied) == 1
        y, line = occupied[0]
        x = line.index(message)
        assert abs(2 * y + 1 - empty.content_size.height) <= 1
        assert abs(2 * x + cell_len(message) - empty.content_size.width) <= 1


async def test_messages_frame_preserves_mouse_selection_and_keyboard_navigation() -> None:
    async with ChatNavigationApp().run_test(size=(60, 24)) as pilot:
        app = pilot.app
        toc = app.query_one(ConversationToc)
        toc.update_items([TocItem("turn-1", "First prompt"), TocItem("turn-2", "Second prompt")])
        box = toc.query_one("#toc-box")
        tree = box.query_one(Tree)
        await wait_for(
            lambda: tree.display and len(tree.root.children) == 2 and screen_is_settled(app, app.screen),
            pilot=pilot,
            description="message rows shown inside the frame",
        )
        assert box.border_title == "Messages"
        assert not box.query_one(".toc-empty", Static).display
        assert [node.label.plain for node in tree.root.children] == ["1. First prompt", "2. Second prompt"]
        await click_when_settled(pilot, tree, offset=(3, 0))
        assert app.selected == ["turn-1"]
        await pilot.press("down", "enter")
        assert app.selected == ["turn-1", "turn-2"]


@pytest.mark.parametrize(("height", "preview_height"), [(16, 8), (22, 11), (28, 13), (37, 13)])
async def test_file_changes_shrink_only_the_messages_section_and_removal_restores_its_height(
    height: int, preview_height: int
) -> None:
    async with WidgetApp(SidebarChatPanel).run_test(size=(60, height)) as pilot:
        app = pilot.app
        chat = app.query_one(SidebarChatPanel)
        toc = chat.query_one(ConversationToc)
        toc.update_items([TocItem("turn-1", "A conversation")])
        box = toc.query_one("#toc-box")
        preview = chat.query_one(DiffPreview)
        await wait_for(lambda: box.region.height == height, pilot=pilot, description="Messages fills Chat")
        full_height, full_region = box.region.height, box.region
        chat_height = chat.content_size.height
        chat.set_entries(tuple(changed_file(f"file{i}.py") for i in range(11)))
        changes_box = preview.query_one("#diff-preview-box")
        await wait_for(
            lambda: preview.display and box.region.height == height - preview_height,
            pilot=pilot,
            description="file changes share at most half of Chat",
        )
        assert chat.content_size.height == chat_height
        assert preview.size.height == preview_height
        assert box.region.height + preview.region.height == chat_height
        assert changes_box.region.y == box.region.bottom
        assert box.region.x == changes_box.region.x == full_region.x
        assert box.region.right == changes_box.region.right == full_region.right
        assert box.region.y == full_region.y
        assert preview.size.height <= chat_height // 2
        assert 2 * box.region.height >= chat_height

        chat.set_entries(())
        await wait_for(
            lambda: not preview.display and box.region.height == full_height,
            pilot=pilot,
            description="Messages reclaims the full Chat height",
        )
        assert box.region == full_region
        assert box.border_title == "Messages"


async def test_narrow_and_short_chat_keep_the_messages_frame_when_the_preview_has_no_room() -> None:
    async with WidgetApp(SidebarChatPanel).run_test(size=(42, 24)) as pilot:
        app = pilot.app
        chat = app.query_one(SidebarChatPanel)
        chat.set_entries(tuple(changed_file(f"file{i}.py") for i in range(7)))
        box = chat.query_one("#toc-box")
        preview = chat.query_one(DiffPreview)
        await wait_for(
            lambda: preview.display and preview.rows == 7 and screen_is_settled(app, app.screen),
            pilot=pilot,
            description="narrow Chat keeps both aligned frames",
        )
        changes_box = preview.query_one("#diff-preview-box")
        assert box.styles.border_top[0] == "round"
        assert box.region.x == changes_box.region.x
        assert box.region.right == changes_box.region.right

        await resize_when_settled(pilot, 24, 7)
        await wait_for(
            lambda: not preview.display and box.region.height == 7,
            pilot=pilot,
            description="short narrow Chat hides preview but retains Messages",
        )
        assert box.border_title == "Messages"
        assert box.styles.border_top[0] == "round"
        assert box.region.width == chat.content_size.width
        assert box.query_one(".toc-empty", Static).display


async def test_shell_mode_and_live_theme_changes_style_both_chat_frames_together() -> None:
    app = WidgetApp(SidebarPanel)
    app.register_theme(CHRYS_THEME)
    app.register_theme(CHRYS_ANSI_THEME)
    app.theme = "textual-dark"
    async with app.run_test(size=(140, 40)) as pilot:
        sidebar = app.query_one(SidebarPanel)
        chat = sidebar.query_one(SidebarChatPanel)
        chat.set_entries((changed_file("file.py"),))
        messages = chat.query_one("#toc-box")
        changes = chat.query_one("#diff-preview-box")
        await wait_for(
            lambda: chat.query_one(DiffPreview).display and screen_is_settled(app, app.screen),
            pilot=pilot,
            description="both Chat frames settled",
        )
        normal = messages.styles.border_top
        assert normal == changes.styles.border_top
        assert messages.styles.border_title_color == changes.styles.border_title_color
        assert messages.styles.border_title_style == changes.styles.border_title_style
        sidebar.add_class("-shell-active")
        await wait_for(
            lambda: messages.styles.border_top != normal,
            pilot=pilot,
            description="Messages uses the Shell warning border",
        )
        assert messages.styles.border_top == changes.styles.border_top
        sidebar.remove_class("-shell-active")
        await wait_for(
            lambda: messages.styles.border_top == changes.styles.border_top == normal,
            pilot=pilot,
            description="normal Chat borders restored",
        )
        for theme in ("textual-light", "chrys", "chrys-ansi"):
            app.theme = theme
            await wait_for(
                lambda theme=theme: app.current_theme.name == theme and screen_is_settled(app, app.screen),
                pilot=pilot,
                description=f"Chat frames restyled for {theme}",
            )
            assert messages.styles.border_top == changes.styles.border_top
            assert messages.styles.border_title_color == changes.styles.border_title_color
            assert messages.styles.border_title_style == changes.styles.border_title_style
            assert messages.border_title == "Messages"


async def test_chinese_messages_title_switches_live_without_rebuilding_the_tree(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(tui_i18n, "persist_locale", lambda _locale: None)
    controller = LocaleController(Settings(locale="en"))
    async with WidgetApp(lambda: SidebarPanel(locale_controller=controller)).run_test(size=(140, 40)) as pilot:
        sidebar = pilot.app.query_one(SidebarPanel)
        toc = sidebar.query_one(ConversationToc)
        toc.update_items([TocItem("turn-1", "A conversation")])
        box = toc.query_one("#toc-box")
        tree = box.query_one(Tree)
        await wait_for(lambda: tree.display and tree.size.height > 0, pilot=pilot, description="English Messages shown")
        assert box.border_title == "Messages"
        nodes = tuple(tree.root.children)
        sidebar.query_one(SidebarChatPanel).set_entries((replace(changed_file("file.py"), line_count_result=(2, 1)),))
        controller.switch_locale("zh-Hans")
        assert box.border_title == "消息"
        assert box.query_one(Tree) is tree
        assert tuple(tree.root.children) == nodes
        assert [node.data for node in tree.root.children] == ["turn-1"]
        assert box.query_one(".toc-empty", Static).content.plain == "你的对话将显示在这里"
