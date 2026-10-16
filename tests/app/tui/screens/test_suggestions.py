# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for the suggestion handler core: popup presentation, prompt history, and # / $ triggers."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import pytest
from textual.css.query import NoMatches

from chrys.app.tui.i18n import LocaleController
from chrys.app.tui.screens.main.view_adapter import MainScreenViewAdapter
from chrys.app.tui.widgets.chrome.suggestion_list import SuggestionItem
from chrys.foundation.config.settings import Settings
from chrys.service.profiles.models.registry import ModelProfileRegistry
from chrys.service.profiles.models.schema import ModelProfile
from tests.support.tui_helpers import (
    AgentProfileStub,
    AgentRegistryStub,
    SuggestionScreen,
    make_suggestion_handler,
    make_suggestion_screen,
    suggestion_values,
)


def test_view_adapter_suggestions_ignore_teardown_nomatches() -> None:
    screen = make_suggestion_screen()

    def query_one(cls):
        if cls.__name__ == "SuggestionList":
            raise NoMatches("SuggestionList")
        return screen.input_bar

    screen.query_one = query_one
    view = MainScreenViewAdapter(screen, state=screen.state)  # type: ignore[arg-type]

    view.show_suggestions("files", [SuggestionItem(value="a.py", label="a.py", kind="file")])
    view.show_suggestions_loading("files", title="Files")
    view.update_suggestions([SuggestionItem(value="b.py", label="b.py", kind="file")])
    view.hide_suggestions()

    assert screen.input_bar.replacements == []


def test_show_suggestions_titles_popup_per_mode() -> None:
    """The popup border names what is being suggested; files mode names the
    same root the file scanner is scoped to."""
    screen = make_suggestion_screen()
    handler = make_suggestion_handler(screen)
    screen.state.workspace_marker.current_cwd = "/tmp/title-root"

    handler._show_suggestions("commands", [])
    assert screen.suggestion_list.last_title == "Commands"

    handler._show_suggestions("agents", [])
    assert screen.suggestion_list.last_title == "Agents"

    handler._show_suggestions("models", [])
    assert screen.suggestion_list.last_title == "Models"

    handler._show_suggestions("files", [])
    assert screen.suggestion_list.last_title == "Files under /tmp/title-root"

    handler._show_suggestions("history", [])
    assert screen.suggestion_list.last_title == "Prompt History"


def test_prompt_history_suggestions_are_global_recent_first_and_single_line() -> None:
    screen = make_suggestion_screen()
    screen.input_bar.prompt_history = ["old prompt", "middle\nline", "latest\r\nprompt"]
    handler = make_suggestion_handler(screen)

    asyncio.run(handler.show_prompt_history_async())

    assert screen.input_bar.prompt_history_limits == [100]
    assert screen.suggestion_list.last_mode == "history"
    assert screen.suggestion_list.last_title == "Prompt History"
    items = screen.suggestion_list.last_items
    assert [item.value for item in items if isinstance(item, SuggestionItem)] == [
        "latest\r\nprompt",
        "middle\nline",
        "old prompt",
    ]
    assert [item.label for item in items if isinstance(item, SuggestionItem)] == [
        "latest ↵ prompt",
        "middle ↵ line",
        "old prompt",
    ]
    assert all(item.kind == "history" for item in items if isinstance(item, SuggestionItem))
    assert all(item.marquee_start == 0 for item in items if isinstance(item, SuggestionItem))


async def test_prompt_history_opens_loading_popup_before_history_is_ready() -> None:
    screen = make_suggestion_screen()
    started = asyncio.Event()
    release = asyncio.Event()

    async def load_prompt_history(*, max_entries: int) -> list[str]:
        assert max_entries == 100
        started.set()
        await release.wait()
        return ["ready prompt"]

    screen.input_bar.load_prompt_history = load_prompt_history  # type: ignore[method-assign]
    handler = make_suggestion_handler(screen)
    revision = handler.start_prompt_history()
    assert screen.suggestion_list.last_mode == "history"
    assert screen.suggestion_list.last_title == "Prompt History"
    assert screen.suggestion_list.is_loading is True
    assert screen.suggestion_list.last_items == []

    task = asyncio.create_task(handler.show_prompt_history_async(revision=revision))
    await started.wait()
    release.set()
    await task

    assert screen.suggestion_list.is_loading is False
    assert suggestion_values(screen.suggestion_list.last_items) == ["ready prompt"]


async def test_mode_switch_during_prompt_history_load_does_not_replace_new_suggestions() -> None:
    screen = make_suggestion_screen()
    started = asyncio.Event()
    release = asyncio.Event()

    async def load_prompt_history(*, max_entries: int) -> list[str]:
        assert max_entries == 100
        started.set()
        await release.wait()
        return ["stale prompt"]

    screen.input_bar.load_prompt_history = load_prompt_history  # type: ignore[method-assign]
    handler = make_suggestion_handler(screen)
    revision = handler.start_prompt_history()
    task = asyncio.create_task(handler.show_prompt_history_async(revision=revision))
    await started.wait()

    handler._show_suggestions("commands", [SuggestionItem(value="new", label="new")])
    release.set()
    await task

    assert screen.suggestion_list.last_mode == "commands"
    assert screen.suggestion_list.is_loading is False
    assert suggestion_values(screen.suggestion_list.last_items) == ["new"]


def test_prompt_history_selection_restores_original_multiline_prompt() -> None:
    screen = make_suggestion_screen()
    screen.suggestion_list.is_visible = True
    handler = make_suggestion_handler(screen)
    handler._suggestion_mode = "history"

    handler.on_suggestion_selected("history", "first line\nsecond line", execute=False, kind="history")

    assert screen.input_bar.value == "first line\nsecond line"
    assert screen.suggestion_list.is_visible is False
    assert screen.submitted == []


def test_typing_filters_prompt_history_and_clearing_restores_all() -> None:
    screen = make_suggestion_screen()
    screen.input_bar.prompt_history = ["Fix the session bug", "write docs", "Session storage\nnotes"]
    handler = make_suggestion_handler(screen)
    asyncio.run(handler.show_prompt_history_async())

    handler.on_text_changed("session")
    assert suggestion_values(screen.suggestion_list.last_items) == ["Session storage\nnotes", "Fix the session bug"]

    handler.on_text_changed("SESSION bug")
    assert suggestion_values(screen.suggestion_list.last_items) == ["Fix the session bug"]

    handler.on_text_changed("nothing matches")
    assert screen.suggestion_list.last_items == []
    assert screen.suggestion_list.last_empty_text == "No matching prompts. Try changing or clearing the input."
    assert screen.suggestion_list.is_visible is True
    assert handler.suggestion_mode == "history"

    handler.on_text_changed("")
    assert suggestion_values(screen.suggestion_list.last_items) == [
        "Session storage\nnotes",
        "write docs",
        "Fix the session bug",
    ]


def test_empty_prompt_history_says_there_is_no_history_yet() -> None:
    screen = make_suggestion_screen()
    screen.input_bar.value = "anything"
    screen.input_bar.prompt_history = []
    handler = make_suggestion_handler(screen)

    asyncio.run(handler.show_prompt_history_async())

    assert screen.suggestion_list.last_items == []
    assert screen.suggestion_list.last_empty_text == "No prompt history yet."


def test_prompt_history_opens_filtered_by_the_draft() -> None:
    screen = make_suggestion_screen()
    screen.input_bar.value = "docs"
    screen.input_bar.prompt_history = ["write docs", "fix bug", "review docs"]
    handler = make_suggestion_handler(screen)

    asyncio.run(handler.show_prompt_history_async())
    assert suggestion_values(screen.suggestion_list.last_items) == ["review docs", "write docs"]

    handler.on_text_changed("docs review")
    assert suggestion_values(screen.suggestion_list.last_items) == ["review docs"]


async def test_typing_while_prompt_history_loads_filters_loaded_entries() -> None:
    screen = make_suggestion_screen()
    started = asyncio.Event()
    release = asyncio.Event()

    async def load_prompt_history(*, max_entries: int) -> list[str]:
        assert max_entries == 100
        started.set()
        await release.wait()
        return ["alpha", "beta"]

    screen.input_bar.load_prompt_history = load_prompt_history  # type: ignore[method-assign]
    handler = make_suggestion_handler(screen)
    revision = handler.start_prompt_history()
    task = asyncio.create_task(handler.show_prompt_history_async(revision=revision))
    await started.wait()

    screen.input_bar.value = "bet"
    handler.on_text_changed("bet")
    assert screen.suggestion_list.is_loading is True
    release.set()
    await task

    assert suggestion_values(screen.suggestion_list.last_items) == ["beta"]


def test_dismiss_drops_loaded_prompt_history() -> None:
    screen = make_suggestion_screen()
    screen.input_bar.prompt_history = ["alpha"]
    handler = make_suggestion_handler(screen)
    asyncio.run(handler.show_prompt_history_async())

    handler.dismiss_suggestions()
    handler.on_text_changed("alpha")

    assert screen.suggestion_list.is_visible is False
    assert handler.suggestion_mode is None


def test_update_suggestions_rerenders_border_title_in_active_locale() -> None:
    """Per-keystroke rebuilds re-supply the title so a popup opened before a
    locale switch does not keep its stale-language border."""
    screen = make_suggestion_screen()
    controller = LocaleController(Settings(locale="zh-Hans"))
    handler = make_suggestion_handler(screen, locale_controller=controller)
    handler.build_slash_commands()
    handler._suggestion_mode = "commands"

    handler.on_text_changed("/r")

    assert screen.suggestion_list.last_title == "命令"


def test_model_trigger_filters_selectable_profiles_and_switches_by_profile_id() -> None:
    screen = make_suggestion_screen()
    registry = ModelProfileRegistry()
    registry.register(ModelProfile(id="current-id", name="Current", model_id="vendor/current"))
    registry.register(ModelProfile(id="fast-id", name="Fast Model", model_id="vendor/fast"))
    registry.register(ModelProfile(id="incomplete-id", name="Incomplete"))
    screen.services.model_registry = registry
    screen.services.active_model_profile_id = "current-id"
    handler = make_suggestion_handler(screen)

    handler.on_model_triggered()

    assert screen.suggestion_list.last_mode == "models"
    assert suggestion_values(screen.suggestion_list.last_items) == ["current-id", "fast-id"]
    items, disabled = handler._get_model_items()
    assert disabled == {"current-id"}
    assert items[0].label.plain == "◦ Current  vendor/current"

    handler.on_text_changed("$fast")
    assert suggestion_values(screen.suggestion_list.last_items) == ["fast-id"]

    handler.on_suggestion_selected("models", "fast-id", execute=True)
    assert screen.picked_models == ["fast-id"]
    assert screen.input_bar.value == ""
    assert screen.suggestion_list.is_visible is False


def test_pickers_stay_shut_while_the_agent_loads() -> None:
    """A switch that lands mid-load is dropped after the draft is already gone.

    The status-bar selectors block loading as well as running; the inline
    triggers have to agree, or the popup opens onto a choice that cannot be
    committed.
    """
    screen = make_suggestion_screen()
    registry = ModelProfileRegistry()
    registry.register(ModelProfile(id="model-id", name="Model", model_id="vendor/model"))
    screen.services.model_registry = registry
    screen.state.run.agent_loading = True
    handler = make_suggestion_handler(screen)

    handler.on_model_triggered()
    assert screen.suggestion_list.is_visible is False

    handler.on_agent_triggered()
    assert screen.suggestion_list.is_visible is False


@pytest.mark.parametrize("selection_source", ["agent", "override", "inherited"])
def test_model_trigger_does_not_bypass_locked_runtime_selection(selection_source: str) -> None:
    screen = make_suggestion_screen()
    registry = ModelProfileRegistry()
    registry.register(ModelProfile(id="model-id", name="Model", model_id="vendor/model"))
    screen.services.model_registry = registry
    screen.state.runtime.details_confirmed = True
    screen.state.runtime.details.model.profile_id = "model-id"
    screen.state.runtime.details.model.selection_source = selection_source  # type: ignore[assignment]
    handler = make_suggestion_handler(screen)

    handler.on_model_triggered()

    assert screen.suggestion_list.is_visible is False


def _register_agent_profiles(screen: SuggestionScreen) -> None:
    screen.services.agent_registry = AgentRegistryStub(
        [
            AgentProfileStub(name="Code", description="Code agent"),
            AgentProfileStub(name="QA", description="QA agent"),
        ]
    )


def _register_model_profiles(screen: SuggestionScreen) -> None:
    registry = ModelProfileRegistry()
    registry.register(ModelProfile(id="current-id", name="Current", model_id="vendor/current"))
    registry.register(ModelProfile(id="fast-id", name="Fast Model", model_id="vendor/fast"))
    screen.services.model_registry = registry
    screen.services.active_model_profile_id = "current-id"


@pytest.mark.parametrize(
    ("mode", "register_profiles", "typed", "submits"),
    [
        pytest.param("agents", _register_agent_profiles, "#123", True, id="agent-trigger-unmatched-text"),
        pytest.param("agents", _register_agent_profiles, "#Code", False, id="agent-trigger-active-profile"),
        pytest.param("models", _register_model_profiles, "$not-a-model", True, id="model-trigger-unmatched-text"),
        pytest.param("models", _register_model_profiles, "$Current", False, id="model-trigger-active-profile"),
    ],
)
def test_trigger_enter_submits_unmatched_text_but_keeps_a_matched_draft(
    mode: str,
    register_profiles: Callable[[SuggestionScreen], None],
    typed: str,
    submits: bool,
) -> None:
    """Enter with nothing highlighted submits the draft only when the typed
    trigger matches no profile; text that names the active profile stays put."""
    screen = make_suggestion_screen()
    register_profiles(screen)
    screen.input_bar.value = typed
    screen.suggestion_list.is_visible = True
    handler = make_suggestion_handler(screen)
    handler._suggestion_mode = mode

    assert handler.on_suggestion_select(execute=True) is True

    if submits:
        assert screen.input_bar.value == ""
        assert screen.submitted == [typed]
        assert screen.suggestion_list.is_visible is False
    else:
        assert screen.input_bar.value == typed
        assert screen.submitted == []
        assert screen.suggestion_list.is_visible is True


def test_command_suggestion_enter_without_selection_keeps_submit_fallback() -> None:
    screen = make_suggestion_screen()
    screen.input_bar.value = "/mcp"
    handler = make_suggestion_handler(screen)
    handler._suggestion_mode = "commands"
    handler.build_slash_commands()

    assert handler.on_suggestion_select(execute=True) is True

    assert screen.input_bar.value == ""
    assert screen.submitted == ["/mcp"]
