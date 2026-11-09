# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for non-selectable sidebar empty-state prompts."""

from __future__ import annotations

import pytest
from textual.app import App, ComposeResult
from textual.content import Content
from textual.widgets import Static

from chrys.app.tui.i18n import LocaleController
from chrys.app.tui.widgets.sidebar import background_tasks as background_module
from chrys.app.tui.widgets.sidebar import buddy as buddy_module
from chrys.app.tui.widgets.sidebar import context as context_module
from chrys.app.tui.widgets.sidebar import tasks as tasks_module
from chrys.app.tui.widgets.sidebar import toc as toc_module
from chrys.app.tui.widgets.sidebar.buddy import BuddyPanel
from chrys.app.tui.widgets.sidebar.context import ContextPanel
from chrys.app.tui.widgets.sidebar.tasks import TasksPanel
from chrys.app.tui.widgets.sidebar.toc import ConversationToc
from chrys.foundation.config.settings import Settings
from chrys.foundation.i18n import MessageRef
from tests.support.buddies import a_buddy


class SidebarEmptyStatesApp(App):
    def compose(self) -> ComposeResult:
        yield ConversationToc()
        yield TasksPanel()
        yield BuddyPanel()


@pytest.mark.asyncio
async def test_sidebar_empty_state_prompts_are_not_selectable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("chrys.app.tui.widgets.sidebar.buddy.current_buddy", lambda: None)

    async with SidebarEmptyStatesApp().run_test(size=(80, 40)) as pilot:
        await pilot.pause()

        prompts = [
            pilot.app.query_one(".toc-empty", Static),
            pilot.app.query_one("#tasks-empty", Static),
            pilot.app.query_one("#background-tasks-empty", Static),
            pilot.app.query_one(".buddy-empty", Static),
        ]
        assert all(not prompt.allow_select for prompt in prompts)
        assert pilot.app.query_one("#tasks-checklist", Static).allow_select


@pytest.mark.asyncio
async def test_buddy_localized_status_treats_translation_markup_as_literal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class MarkupLocalizer:
        effective_locale = "zh-Hans"

        def render(self, reference: MessageRef) -> str:
            if reference.definition is buddy_module._BUDDY_CLICK_TO_PET:
                return "[red]x[/red]"
            return reference.definition.fallback

    controller = LocaleController(
        Settings(locale="zh-Hans"),
        localizer=MarkupLocalizer(),  # type: ignore[arg-type]
    )
    monkeypatch.setattr("chrys.app.tui.widgets.sidebar.buddy.current_buddy", lambda: None)

    class MarkupBuddyApp(App):
        def compose(self) -> ComposeResult:
            yield BuddyPanel(locale_controller=controller)

    async with MarkupBuddyApp().run_test() as pilot:
        panel = pilot.app.query_one(BuddyPanel)
        panel._update_status(buddy_module._BUDDY_CLICK_TO_PET.bind(), style="green")

        assert str(panel.query_one("#buddy-status", Static).render()) == "[red]x[/red]"


@pytest.mark.asyncio
async def test_sidebar_static_chrome_treats_translation_markup_as_literal() -> None:
    markup_definitions = (
        tasks_module._TASKS_EMPTY,
        background_module._EMPTY,
        toc_module._TOC_EMPTY,
        toc_module._TOC_TITLE,
        context_module._CONTEXT_USAGE,
    )

    class MarkupLocalizer:
        effective_locale = "zh-Hans"

        def render(self, reference: MessageRef) -> str:
            if reference.definition in markup_definitions:
                return "[red]x[/red]"
            return reference.definition.fallback

    controller = LocaleController(
        Settings(locale="zh-Hans"),
        localizer=MarkupLocalizer(),  # type: ignore[arg-type]
    )

    class MarkupSidebarApp(App):
        def compose(self) -> ComposeResult:
            yield ConversationToc(locale_controller=controller)
            yield TasksPanel(locale_controller=controller)
            yield ContextPanel(locale_controller=controller)

    async with MarkupSidebarApp().run_test(size=(80, 40)) as pilot:
        await pilot.pause()
        for selector in (".toc-empty", "#tasks-empty", "#background-tasks-empty", "#ctx-usage-label"):
            rendered = pilot.app.query_one(selector, Static).render()
            assert str(rendered) == "[red]x[/red]", selector
            assert not getattr(rendered, "spans", []), selector
        title = pilot.app.query_one("#toc-box").border_title
        assert Content.from_markup(title).plain == "[red]x[/red]"


@pytest.mark.asyncio
async def test_buddy_level_shows_translation_markup_literally_and_styles_only_the_xp_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class MarkupLocalizer:
        effective_locale = "zh-Hans"

        def render(self, reference: MessageRef) -> str:
            if reference.definition is buddy_module._BUDDY_LEVEL:
                return "[red]3 级[/red]"
            return reference.definition.fallback

    controller = LocaleController(
        Settings(locale="zh-Hans"),
        localizer=MarkupLocalizer(),  # type: ignore[arg-type]
    )
    monkeypatch.setattr("chrys.app.tui.widgets.sidebar.buddy.current_buddy", lambda: a_buddy(turns=20))

    class LevelBuddyApp(App):
        def compose(self) -> ComposeResult:
            yield BuddyPanel(locale_controller=controller)

    async with LevelBuddyApp().run_test(size=(42, 40)) as pilot:
        panel = pilot.app.query_one(BuddyPanel)
        assert panel.buddy is not None
        assert panel.buddy.level == 3

        rendered = panel.query_one("#buddy-level", Static).render()
        assert isinstance(rendered, Content)
        level_line, xp_line = rendered.plain.split("\n")
        # The translation's markup-looking text is shown as it is, never parsed.
        assert level_line == "[red]3 级[/red]"
        # The level line takes its bold accent from the stylesheet. The one span is the XP line opting out.
        assert [(span.start, span.end, str(span.style)) for span in rendered.spans] == [
            (len(level_line), len(rendered.plain), "not bold dim")
        ]
        assert xp_line


@pytest.mark.asyncio
async def test_buddy_info_shows_the_persona_in_the_users_language(monkeypatch: pytest.MonkeyPatch) -> None:
    class KeyLocalizer:
        effective_locale = "zh-Hans"

        def render(self, reference: MessageRef) -> str:
            return f"<{reference.definition.key}>"

    controller = LocaleController(
        Settings(locale="zh-Hans"),
        localizer=KeyLocalizer(),  # type: ignore[arg-type]
    )
    monkeypatch.setattr("chrys.app.tui.widgets.sidebar.buddy.current_buddy", a_buddy)

    class PersonaBuddyApp(App):
        def compose(self) -> ComposeResult:
            yield BuddyPanel(locale_controller=controller)

    async with PersonaBuddyApp().run_test(size=(42, 40)) as pilot:
        panel = pilot.app.query_one(BuddyPanel)
        assert panel.buddy is not None

        rendered = panel.query_one("#buddy-info", Static).render()
        assert isinstance(rendered, Content)
        # The persona is the panel's to translate, like every other line of the card.
        assert rendered.plain.split("\n") == [
            "<tui.sidebar.buddy.species>",
            "<tui.sidebar.buddy.rarity>",
            "“<tui.buddy.persona.focus>”",
        ]
