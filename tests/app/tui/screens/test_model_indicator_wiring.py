# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for main-screen model-indicator state and event wiring."""

from __future__ import annotations

from typing import Literal

import pytest
from textual.app import App, ComposeResult
from textual.widgets import OptionList, Static

from chrys.app.tui import i18n as tui_i18n
from chrys.app.tui.i18n import LocaleController, LocaleSwitchStatus
from chrys.app.tui.screens.dialogs.confirm import ConfirmDialog
from chrys.app.tui.screens.main.model_indicator import ModelIndicatorState
from chrys.app.tui.screens.main.screen import MainScreen
from chrys.app.tui.screens.models.picker import ModelsScreen
from chrys.app.tui.widgets.chat.messages import ConversationStatusAction
from chrys.app.tui.widgets.chrome.input_bar import InputBar, _ChatTextArea
from chrys.app.tui.widgets.chrome.status_bar import StatusBar
from chrys.app.tui.widgets.chrome.suggestion_list import SuggestionList
from chrys.foundation.config.settings import Settings
from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import (
    AgentLoadFailed,
    AgentLoadStarted,
    AgentRuntimeDetails,
    AgentRuntimeUpdated,
    ProfileSwitched,
    RuntimeModelDetails,
    SessionReady,
)
from chrys.service.profiles.models.registry import ModelProfileRegistry
from chrys.service.profiles.models.schema import UNCONFIGURED_MODEL_ID, ModelProfile
from tests.support.tui_helpers import click_when_settled
from tests.support.waiting import wait_for


class _ModelIndicatorApp(App[None]):
    def __init__(
        self,
        registry: ModelProfileRegistry,
        *,
        locale_controller: LocaleController | None = None,
    ) -> None:
        super().__init__()
        self.main_screen = MainScreen(
            EventBus(),
            model_registry=registry,
            engine_provider=None,
            locale_controller=locale_controller,
        )

    def compose(self) -> ComposeResult:
        yield from ()

    async def on_mount(self) -> None:
        await self.push_screen(self.main_screen)


def _valid_model(profile_id: str = "model-profile", name: str = "Configured Model") -> ModelProfile:
    return ModelProfile(
        id=profile_id,
        name=name,
        provider="openai",
        api_style="responses",
        model_id=f"{profile_id}-wire",
        max_context_tokens=200_000,
        max_output_tokens=32_000,
    )


def _blank_seeded_profile() -> ModelProfile:
    """The auto-seeded placeholder: registered but not selectable (no model id)."""
    return ModelProfile(id="profile-1", name="Profile 1", model_id="")


def _registry(*profiles: ModelProfile) -> ModelProfileRegistry:
    registry = ModelProfileRegistry()
    for profile in profiles:
        registry.register(profile)
    return registry


def _runtime_details(
    name: str,
    *,
    profile_id: str = "model-profile",
    source: Literal["override", "agent", "inherited", "active", "default"] = "active",
) -> AgentRuntimeDetails:
    return AgentRuntimeDetails(
        model=RuntimeModelDetails(
            profile_id=profile_id,
            name=name,
            provider="openai",
            api_style="responses",
            model_id=f"{profile_id}-wire",
            max_context_tokens=200_000,
            selection_source=source,
        )
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("registry", [_registry(), _registry(_blank_seeded_profile())])
async def test_unconfirmed_configure_tag_routes_to_model_config(
    registry: ModelProfileRegistry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _ModelIndicatorApp(registry)

    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        tag = app.main_screen.query_one("#model-tag", Static)

        assert app.main_screen._state.runtime.details_confirmed is False
        assert tag in app.main_screen.query_one(StatusBar).query("#model-tag")
        assert list(app.main_screen.query_one(InputBar).query("#model-tag")) == []
        assert tag.display is False
        assert app.main_screen.query_one("#model-label", Static).display is False

        app.main_screen._set_profile_display("Code Agent")
        app.main_screen._state.runtime.details_confirmed = True
        app.main_screen._refresh_model_indicator()
        await pilot.pause()
        opened: list[None] = []
        picked: list[str] = []

        async def record_pick(profile_id: str) -> None:
            picked.append(profile_id)

        monkeypatch.setattr(app.main_screen._config_actions, "open_model_config", lambda: opened.append(None))
        monkeypatch.setattr(app.main_screen._config_actions, "on_model_picked", record_pick)

        await click_when_settled(pilot, "#model-tag")
        await pilot.pause()

        assert opened == [None]
        assert picked == []


@pytest.mark.asyncio
async def test_unconfirmed_select_tag_opens_picker_and_routes_selected_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _ModelIndicatorApp(_registry(_valid_model()))

    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        tag = app.main_screen.query_one("#model-tag", Static)

        assert app.main_screen._state.runtime.details_confirmed is False
        assert tag in app.main_screen.query_one(StatusBar).query("#model-tag")
        assert list(app.main_screen.query_one(InputBar).query("#model-tag")) == []
        assert tag.display is False
        assert app.main_screen.query_one("#model-label", Static).display is False

        app.main_screen._set_profile_display("Code Agent")
        app.main_screen._state.runtime.details_confirmed = True
        app.main_screen._refresh_model_indicator()
        await pilot.pause()
        picked: list[str] = []

        async def record_pick(profile_id: str) -> None:
            picked.append(profile_id)

        monkeypatch.setattr(app.main_screen._config_actions, "on_model_picked", record_pick)

        await click_when_settled(pilot, "#model-tag")
        await pilot.pause()

        assert isinstance(app.screen, ModelsScreen)
        option_list = app.screen.query_one(OptionList)
        option_id = "profile:model-profile"
        option = option_list.get_option(option_id)
        option_index = option_list.get_option_index(option_id)
        option_list.post_message(OptionList.OptionSelected(option_list, option, option_index))
        await pilot.pause()

        assert picked == ["model-profile"]


@pytest.mark.asyncio
async def test_dollar_trigger_opens_inline_model_picker_and_switches_selected_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _ModelIndicatorApp(
        _registry(
            _valid_model("current-model", "Current Model"),
            _valid_model("next-model", "Next Model"),
        )
    )

    async with app.run_test(size=(100, 24)) as pilot:
        await pilot.pause()
        app.main_screen._state.runtime.details = AgentRuntimeDetails(
            model=RuntimeModelDetails(
                profile_id="current-model",
                name="Current Model",
                model_id="current-model-wire",
                selection_source="active",
            )
        )
        app.main_screen._state.runtime.details_confirmed = True
        picked: list[str] = []

        async def record_pick(profile_id: str) -> None:
            picked.append(profile_id)

        monkeypatch.setattr(app.main_screen._config_actions, "on_model_picked", record_pick)
        input_bar = app.main_screen.query_one(InputBar)
        input_bar.focus_input()
        await wait_for(
            lambda: input_bar.query_one("#chat-input").has_focus,
            pilot=pilot,
            description="composer focus before interaction",
        )

        await pilot.press("$")
        await pilot.pause()

        suggestions = app.main_screen.query_one(SuggestionList)
        assert suggestions.mode == "models"
        assert str(suggestions.border_title) == "Models"

        await pilot.press("enter")
        await pilot.pause()

        assert picked == ["next-model"]
        assert input_bar.value == ""


async def _open_model_picker(app: _ModelIndicatorApp, pilot) -> InputBar:
    """Confirm runtime details, then open the $ popup over the live registry."""
    app.main_screen._state.runtime.details = AgentRuntimeDetails(
        model=RuntimeModelDetails(
            profile_id="current-model",
            name="Current Model",
            model_id="current-model-wire",
            selection_source="active",
        )
    )
    app.main_screen._state.runtime.details_confirmed = True
    input_bar = app.main_screen.query_one(InputBar)
    input_bar.focus_input()
    await wait_for(
        lambda: input_bar.query_one("#chat-input").has_focus,
        pilot=pilot,
        description="composer focus before interaction",
    )
    await pilot.press("$")
    await pilot.pause()
    assert app.main_screen.query_one(SuggestionList).mode == "models"
    return input_bar


@pytest.mark.asyncio
async def test_selecting_a_row_retired_while_the_popup_stood_open_persists_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F4 is a priority binding, so the model config screen can open over the popup.

    Deleting the displayed profile there leaves a row on screen that no longer
    resolves; ``activate_model_profile`` would persist the dangling id.
    """
    registry = _registry(
        _valid_model("current-model", "Current Model"),
        _valid_model("next-model", "Next Model"),
    )
    app = _ModelIndicatorApp(registry)

    async with app.run_test(size=(100, 24)) as pilot:
        await pilot.pause()
        picked: list[str] = []

        async def record_pick(profile_id: str) -> None:
            picked.append(profile_id)

        monkeypatch.setattr(app.main_screen._config_actions, "on_model_picked", record_pick)
        input_bar = await _open_model_picker(app, pilot)

        assert registry.remove("next-model") is True

        await pilot.press("enter")
        await pilot.pause()

        assert picked == []
        assert input_bar.value == ""


@pytest.mark.asyncio
async def test_selecting_a_row_after_an_agent_claims_the_model_persists_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F2 can hand model ownership to an agent while the rows stay on screen."""
    app = _ModelIndicatorApp(
        _registry(
            _valid_model("current-model", "Current Model"),
            _valid_model("next-model", "Next Model"),
        )
    )

    async with app.run_test(size=(100, 24)) as pilot:
        await pilot.pause()
        picked: list[str] = []

        async def record_pick(profile_id: str) -> None:
            picked.append(profile_id)

        monkeypatch.setattr(app.main_screen._config_actions, "on_model_picked", record_pick)
        await _open_model_picker(app, pilot)

        app.main_screen._state.runtime.details.model.selection_source = "agent"

        await pilot.press("enter")
        await pilot.pause()

        assert picked == []


@pytest.mark.asyncio
async def test_commit_guard_refuses_unknown_and_unselectable_profiles() -> None:
    app = _ModelIndicatorApp(_registry(_valid_model("known", "Known"), _blank_seeded_profile()))

    async with app.run_test(size=(100, 24)) as pilot:
        await pilot.pause()
        screen = app.main_screen

        assert screen._model_selection_is_committable("known") is True
        assert screen._model_selection_is_committable("never-registered") is False
        # Registered but structurally incomplete — the picker never offers it,
        # and a stale row for it must not persist either.
        assert screen._model_selection_is_committable("profile-1") is False

        screen._state.run.agent_loading = True
        assert screen._model_selection_is_committable("known") is False


@pytest.mark.asyncio
async def test_model_action_label_relocalizes_in_place(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(tui_i18n, "persist_locale", lambda _locale: None)
    controller = LocaleController(Settings(locale="en"))
    app = _ModelIndicatorApp(_registry(), locale_controller=controller)

    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        app.main_screen._set_profile_display("Code Agent")
        app.main_screen._state.runtime.details_confirmed = True
        app.main_screen._refresh_model_indicator()
        await pilot.pause()
        tag = app.main_screen.query_one("#model-tag", Static)
        assert tag.render().plain == "Select Model"

        result = controller.switch_locale("zh-Hans")
        await pilot.pause()

        assert result.status is LocaleSwitchStatus.EFFECTIVE_CHANGED
        assert tag.render().plain == "选择模型"


@pytest.mark.asyncio
async def test_same_and_changed_profile_switches_refresh_model_tag() -> None:
    # AIxCoding catalog: registry is the live name source — a catalog sync
    # renames the profile under a running session and the tag must follow it;
    # a profile id the registry does not know falls back to the runtime snapshot.
    app = _ModelIndicatorApp(_registry(_valid_model(name="Reloaded Model")))

    async with app.run_test(size=(120, 24)) as pilot:
        screen = app.main_screen
        screen._set_profile_display("Code Agent")

        await screen._sessions.on_profile_switched(
            ProfileSwitched(
                from_profile="Code",
                to_profile="Code",
                from_display_name="Code Agent",
                to_display_name="Code Agent",
                runtime_details=_runtime_details("Reloaded Model"),
            )
        )
        await pilot.pause()

        tag = screen.query_one("#model-tag", Static)
        assert screen._state.runtime.details_confirmed is True
        assert tag.render().plain == "Reloaded Model"

        await screen._sessions.on_profile_switched(
            ProfileSwitched(
                from_profile="Code",
                to_profile="QA",
                from_display_name="Code Agent",
                to_display_name="QA Agent",
                runtime_details=_runtime_details("Switched Model", profile_id="qa-model"),
            )
        )
        await pilot.pause()

        assert screen._state.runtime.profile == "QA Agent"
        assert tag.render().plain == "Switched Model"


@pytest.mark.asyncio
async def test_session_ready_and_runtime_update_confirm_and_refresh_model_tag() -> None:
    app = _ModelIndicatorApp(_registry(_valid_model(name="Ready Model")))

    async with app.run_test(size=(120, 24)) as pilot:
        screen = app.main_screen
        await screen._events.on_session_ready(
            SessionReady(
                agent_profile="Code",
                display_name="Code Agent",
                primary_cwd="/workspace",
                runtime_details=_runtime_details("Ready Model"),
            )
        )
        await pilot.pause()

        tag = screen.query_one("#model-tag", Static)
        assert screen._state.runtime.details_confirmed is True
        assert tag.render().plain == "Ready Model"

        screen._state.runtime.details_confirmed = False
        screen._refresh_model_indicator()
        await screen._events.on_agent_runtime_updated(
            AgentRuntimeUpdated(runtime_details=_runtime_details("Updated Model", profile_id="updated-model"))
        )
        await pilot.pause()

        assert screen._state.runtime.details_confirmed is True
        assert tag.render().plain == "Updated Model"


@pytest.mark.asyncio
async def test_load_operation_state_machine_resets_only_destructive_confirmation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _ModelIndicatorApp(_registry(_valid_model(name="Confirmed Old Model")))

    async with app.run_test(size=(80, 24)) as pilot:
        screen = app.main_screen
        screen._set_profile_display("Code Agent")
        tag = screen.query_one("#model-tag", Static)

        async def ignore_started(_event: AgentLoadStarted) -> None:
            return

        monkeypatch.setattr(screen._events._agent_load_controller, "on_started", ignore_started)

        async def run_started(operation: str) -> None:
            screen._state.runtime.details = _runtime_details("Confirmed Old Model")
            screen._state.runtime.details_confirmed = True
            screen._refresh_model_indicator()
            await screen._events.on_agent_load_started(AgentLoadStarted(operation=operation))
            await pilot.pause()

        for operation in ("startup", "new_session", "restore", "reset", "reset_failed", "unexpected"):
            await run_started(operation)
            assert screen._state.runtime.details_confirmed is False, operation
            # The stale label stays painted mid-load — repainting here would
            # flash "Select Model" between the old and new model names. Only
            # the terminal confirmation or failure event repaints the tag.
            assert tag.render().plain == "Confirmed Old Model", operation

        for operation in ("switch", "settings_reload", "workspace_change", "model_switch"):
            await run_started(operation)
            assert screen._state.runtime.details_confirmed is True, operation
            assert tag.render().plain == "Confirmed Old Model", operation


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "confirmed_after", "visible_after", "label_after"),
    [
        ("new_session", False, False, ""),
        ("switch", True, True, "Confirmed Old Model"),
        ("settings_reload", True, True, "Confirmed Old Model"),
    ],
)
async def test_started_then_failed_sequence_settles_per_operation_kind(
    operation: str,
    confirmed_after: bool,
    visible_after: bool,
    label_after: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _ModelIndicatorApp(_registry(_valid_model(name="Confirmed Old Model")))

    async with app.run_test(size=(80, 24)) as pilot:
        screen = app.main_screen
        screen._set_profile_display("Code Agent")
        tag = screen.query_one("#model-tag", Static)

        async def ignore_started(_event: AgentLoadStarted) -> None:
            return

        monkeypatch.setattr(screen._events._agent_load_controller, "on_started", ignore_started)
        monkeypatch.setattr(screen._events._agent_load_controller, "on_failed", lambda _event, *, display=None: None)

        screen._state.runtime.details = _runtime_details("Confirmed Old Model")
        screen._state.runtime.details_confirmed = True
        screen._refresh_model_indicator()
        await pilot.pause()

        await screen._events.on_agent_load_started(AgentLoadStarted(operation=operation))
        await screen._events.on_agent_load_failed(AgentLoadFailed(operation=operation, agent_profile="Code"))
        await pilot.pause()

        assert screen._state.runtime.details_confirmed is confirmed_after, operation
        assert tag.display is visible_after, operation
        assert tag.render().plain == label_after, operation


@pytest.mark.asyncio
async def test_startup_load_failure_keeps_unconfirmed_action_tag_hidden(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _ModelIndicatorApp(_registry(_valid_model()))

    async with app.run_test(size=(80, 24)) as pilot:
        screen = app.main_screen
        tag = screen.query_one("#model-tag", Static)
        screen._state.runtime.details_confirmed = False
        screen.query_one(StatusBar).set_model(
            ModelIndicatorState(
                label="Phantom Model",
                tooltip="Phantom Model",
                mode="select",
                profile_id="phantom",
                visible=True,
            )
        )
        monkeypatch.setattr(screen._events._agent_load_controller, "on_failed", lambda _event, *, display=None: None)

        await screen._events.on_agent_load_failed(AgentLoadFailed(operation="startup", agent_profile="Code"))
        await pilot.pause()

        assert tag.display is False
        assert screen.query_one("#model-label", Static).display is False


@pytest.mark.asyncio
async def test_input_lock_message_updates_status_selector_guards() -> None:
    app = _ModelIndicatorApp(_registry(_valid_model()))

    async with app.run_test(size=(120, 24)) as pilot:
        screen = app.main_screen
        screen._set_profile_display("Code Agent")
        screen._state.runtime.details = _runtime_details("Configured Model")
        screen._state.runtime.details_confirmed = True
        screen._refresh_model_indicator()
        screen.query_one(StatusBar).set_profile("Code Agent")
        await pilot.pause()

        input_bar = screen.query_one(InputBar)
        status_bar = screen.query_one(StatusBar)
        profile_tag = status_bar.query_one("#profile-tag", Static)
        model_tag = status_bar.query_one("#model-tag", Static)
        assert profile_tag.styles.pointer == "pointer"
        assert model_tag.styles.pointer == "pointer"

        input_bar.lock_with_text()
        await pilot.pause()
        assert status_bar.input_locked is True
        assert profile_tag.styles.pointer == "default"
        assert model_tag.styles.pointer == "default"

        input_bar.unlock_and_keep()
        await pilot.pause()
        assert status_bar.input_locked is False
        assert profile_tag.styles.pointer == "pointer"
        assert model_tag.styles.pointer == "pointer"


@pytest.mark.asyncio
async def test_blocked_selector_clicks_explain_why_with_literal_names(monkeypatch: pytest.MonkeyPatch) -> None:
    app = _ModelIndicatorApp(_registry(_valid_model()))

    async with app.run_test(size=(120, 24)) as pilot:
        screen = app.main_screen
        screen._set_profile_display("Code [Agent]")
        screen._state.runtime.details = _runtime_details("Bound [Model]", source="agent")
        screen._state.runtime.details_confirmed = True
        screen._refresh_model_indicator()
        screen.query_one(StatusBar).set_profile("Code [Agent]")
        await pilot.pause()
        notices: list[tuple[str, str, bool]] = []

        def notify(
            message: str,
            *,
            title: str = "",
            severity: str = "information",
            timeout: float | None = None,
            markup: bool = True,
        ) -> None:
            notices.append((message, title, markup))

        monkeypatch.setattr(screen, "notify", notify)

        await click_when_settled(pilot, "#model-tag")
        await wait_for(lambda: bool(notices), pilot=pilot, description="model lock notice")
        assert notices == [
            (
                (
                    "Code [Agent] is bound to the model Bound [Model], so the model cannot be switched here. "
                    "To change it, press F2 and edit the agent's model on its Basic tab."
                ),
                "Model Locked",
                False,
            )
        ]
        assert app.screen is screen

        notices.clear()
        screen._set_agent_running(True)
        await pilot.pause()
        await click_when_settled(pilot, "#profile-tag")
        await wait_for(lambda: bool(notices), pilot=pilot, description="busy agent selector notice")
        assert notices == [("Cannot switch agents while the agent is busy", "Busy", False)]
        assert app.screen is screen


@pytest.mark.asyncio
@pytest.mark.parametrize("registry", [_registry(), _registry(_blank_seeded_profile())])
async def test_submit_without_configured_model_blocks_and_restores_draft(
    registry: ModelProfileRegistry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _ModelIndicatorApp(registry)

    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        screen = app.main_screen
        submitted: list[str] = []
        monkeypatch.setattr(screen._input_flow, "submit_user_text", lambda text, _draft=None: submitted.append(text))

        screen.post_message(InputBar.UserSubmitted("hello world"))
        await pilot.pause()

        assert submitted == []
        assert isinstance(app.screen, ConfirmDialog)
        assert screen.query_one(InputBar).snapshot_draft().text == "hello world"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("button", "expect_opened"),
    [("#confirm-yes", True), ("#confirm-no", False)],
)
async def test_model_guard_dialog_routes_confirm_to_model_config(
    button: str,
    expect_opened: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _ModelIndicatorApp(_registry())

    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        screen = app.main_screen
        opened: list[None] = []
        monkeypatch.setattr(screen._config_actions, "open_model_config", lambda: opened.append(None))

        screen.post_message(InputBar.UserSubmitted("hello"))
        await pilot.pause()
        assert isinstance(app.screen, ConfirmDialog)

        await click_when_settled(pilot, button)
        await pilot.pause()

        assert not isinstance(app.screen, ConfirmDialog)
        assert opened == ([None] if expect_opened else [])
        if not expect_opened:
            assert screen.query_one("#chat-input").has_focus


@pytest.mark.asyncio
async def test_submit_with_selectable_model_proceeds_without_dialog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _ModelIndicatorApp(_registry(_valid_model()))

    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        screen = app.main_screen
        submitted: list[str] = []
        monkeypatch.setattr(screen._input_flow, "submit_user_text", lambda text, _draft=None: submitted.append(text))

        screen.post_message(InputBar.UserSubmitted("hello"))
        await pilot.pause()

        assert submitted == ["hello"]
        assert not isinstance(app.screen, ConfirmDialog)


def _unconfigured_runtime_details(model_id: str) -> AgentRuntimeDetails:
    """Details as confirmed by SessionReady when the resolver fell back to Default."""
    return AgentRuntimeDetails(
        model=RuntimeModelDetails(
            profile_id="",
            name="Default",
            provider="openai",
            api_style="responses",
            model_id=model_id,
            max_context_tokens=200_000,
            selection_source="default",
        )
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("model_id", [UNCONFIGURED_MODEL_ID, ""])
async def test_submit_with_confirmed_unconfigured_fallback_still_blocks(
    model_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The built-in fallback loads and confirms details, but requests are doomed."""
    app = _ModelIndicatorApp(_registry(_blank_seeded_profile()))

    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        screen = app.main_screen
        submitted: list[str] = []
        monkeypatch.setattr(screen._input_flow, "submit_user_text", lambda text, _draft=None: submitted.append(text))
        screen._state.runtime.details = _unconfigured_runtime_details(model_id)
        screen._state.runtime.details_confirmed = True

        screen.post_message(InputBar.UserSubmitted("hello"))
        await pilot.pause()

        assert submitted == []
        assert isinstance(app.screen, ConfirmDialog)


@pytest.mark.asyncio
async def test_confirmed_blank_details_defer_to_selectable_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Event sources may confirm without model details — a blank id is not proof of misconfiguration."""
    app = _ModelIndicatorApp(_registry(_valid_model()))

    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        screen = app.main_screen
        submitted: list[str] = []
        monkeypatch.setattr(screen._input_flow, "submit_user_text", lambda text, _draft=None: submitted.append(text))
        screen._state.runtime.details = _unconfigured_runtime_details("")
        screen._state.runtime.details_confirmed = True

        screen.post_message(InputBar.UserSubmitted("hello"))
        await pilot.pause()

        assert submitted == ["hello"]
        assert not isinstance(app.screen, ConfirmDialog)


@pytest.mark.asyncio
async def test_guard_confirm_opens_picker_when_profiles_are_selectable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _ModelIndicatorApp(_registry(_valid_model()))

    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        screen = app.main_screen
        submitted: list[str] = []
        monkeypatch.setattr(screen._input_flow, "submit_user_text", lambda text, _draft=None: submitted.append(text))
        screen._state.runtime.details = _unconfigured_runtime_details(UNCONFIGURED_MODEL_ID)
        screen._state.runtime.details_confirmed = True

        screen.post_message(InputBar.UserSubmitted("hello"))
        await pilot.pause()
        assert submitted == []
        assert isinstance(app.screen, ConfirmDialog)

        await click_when_settled(pilot, "#confirm-yes")
        await pilot.pause()

        assert isinstance(app.screen, ModelsScreen)

        await pilot.press("escape")
        await pilot.pause()

        assert app.screen is screen
        assert screen.query_one("#chat-input").has_focus


@pytest.mark.asyncio
async def test_guard_confirm_opens_config_and_close_restores_input_focus(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from textual.screen import Screen

    class _FakeConfigScreen(Screen[str]):
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            super().__init__()

    monkeypatch.setattr("chrys.app.tui.screens.models.screen.ModelConfigScreen", _FakeConfigScreen)
    monkeypatch.setattr("chrys.service.profiles.models.env_bridge.get_global_default_profile_id", lambda: "")

    app = _ModelIndicatorApp(_registry())

    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        screen = app.main_screen

        screen.post_message(InputBar.UserSubmitted("hello"))
        await pilot.pause()
        await click_when_settled(pilot, "#confirm-yes")
        await pilot.pause()
        assert isinstance(app.screen, _FakeConfigScreen)

        app.screen.dismiss("")
        await pilot.pause()

        assert app.screen is screen
        assert screen.query_one("#chat-input").has_focus


@pytest.mark.asyncio
async def test_submit_with_confirmed_runtime_model_bypasses_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _ModelIndicatorApp(_registry())

    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        screen = app.main_screen
        submitted: list[str] = []
        monkeypatch.setattr(screen._input_flow, "submit_user_text", lambda text, _draft=None: submitted.append(text))
        screen._state.runtime.details = _runtime_details("Runtime Model")
        screen._state.runtime.details_confirmed = True

        screen.post_message(InputBar.UserSubmitted("hello"))
        await pilot.pause()

        assert submitted == ["hello"]
        assert not isinstance(app.screen, ConfirmDialog)


@pytest.mark.asyncio
async def test_slash_command_dispatch_bypasses_model_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _ModelIndicatorApp(_registry())

    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        screen = app.main_screen
        submitted: list[str] = []
        dispatched: list[str] = []

        def record_dispatch(text: str) -> bool:
            dispatched.append(text)
            return True

        monkeypatch.setattr(screen._input_flow, "submit_user_text", lambda text, _draft=None: submitted.append(text))
        monkeypatch.setattr(screen._suggestions, "dispatch_slash_command", record_dispatch)

        screen.post_message(InputBar.UserSubmitted("/help"))
        await pilot.pause()

        assert dispatched == ["/help"]
        assert submitted == []
        assert not isinstance(app.screen, ConfirmDialog)


@pytest.mark.asyncio
async def test_runtime_skill_selection_blocks_when_model_unconfigured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Suggestion-originated submissions must hit the same guard as composer submits."""
    app = _ModelIndicatorApp(_registry())

    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        screen = app.main_screen
        submitted: list[str] = []
        monkeypatch.setattr(screen._input_flow, "submit_user_text", lambda text, _draft=None: submitted.append(text))

        screen._suggestions.on_suggestion_selected("commands", "review", execute=True, kind="skill")
        await pilot.pause()

        assert submitted == []
        assert isinstance(app.screen, ConfirmDialog)
        assert screen.query_one(InputBar).snapshot_draft().text == "/review"


@pytest.mark.asyncio
async def test_runtime_skill_selection_submits_with_confirmed_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _ModelIndicatorApp(_registry())

    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        screen = app.main_screen
        submitted: list[str] = []
        monkeypatch.setattr(screen._input_flow, "submit_user_text", lambda text, _draft=None: submitted.append(text))
        screen._state.runtime.details = _runtime_details("Runtime Model")
        screen._state.runtime.details_confirmed = True

        screen._suggestions.on_suggestion_selected("commands", "review", execute=True, kind="skill")
        await pilot.pause()

        assert submitted == ["/review"]
        assert not isinstance(app.screen, ConfirmDialog)


@pytest.mark.asyncio
async def test_retry_blocks_when_model_unconfigured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A retry re-runs the turn against the model, so it must hit the same
    guard as a fresh submit — and hand back the consumed continuation note."""
    app = _ModelIndicatorApp(_registry())

    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        screen = app.main_screen
        retried: list[str] = []
        monkeypatch.setattr(screen._input_flow, "request_retry", lambda text, _draft=None: retried.append(text))

        screen.post_message(InputBar.RetryRequested(text="carry on"))
        await pilot.pause()

        assert retried == []
        assert isinstance(app.screen, ConfirmDialog)
        assert screen.query_one(InputBar).snapshot_draft().text == "carry on"


@pytest.mark.asyncio
async def test_blocked_submit_hand_back_never_overwrites_a_draft_typed_in_the_gap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """InputBar clears the composer when it posts; the hand-back arrives a hop later."""
    app = _ModelIndicatorApp(_registry())

    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        screen = app.main_screen
        submitted: list[str] = []
        monkeypatch.setattr(screen._input_flow, "submit_user_text", lambda text, _draft=None: submitted.append(text))
        input_bar = screen.query_one(InputBar)

        screen.post_message(InputBar.UserSubmitted(text="the blocked message"))
        # The user starts the next message before MainScreen handles the submit.
        input_bar.query_one("#chat-input", _ChatTextArea).load_text("the next thing I want to say")
        await pilot.pause()

        assert submitted == []
        assert isinstance(app.screen, ConfirmDialog)
        assert input_bar.snapshot_draft().text == "the next thing I want to say"


@pytest.mark.asyncio
async def test_blocked_retry_hand_back_never_overwrites_a_draft_typed_in_the_gap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The consumed continuation note loses to a draft that landed before the hand-back."""
    app = _ModelIndicatorApp(_registry())

    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        screen = app.main_screen
        retried: list[str] = []
        monkeypatch.setattr(screen._input_flow, "request_retry", lambda text, _draft=None: retried.append(text))
        input_bar = screen.query_one(InputBar)

        screen.post_message(InputBar.RetryRequested(text="carry on"))
        input_bar.query_one("#chat-input", _ChatTextArea).load_text("a new note")
        await pilot.pause()

        assert retried == []
        assert isinstance(app.screen, ConfirmDialog)
        assert input_bar.snapshot_draft().text == "a new note"


@pytest.mark.asyncio
async def test_status_action_retry_blocks_when_model_unconfigured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _ModelIndicatorApp(_registry())

    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        screen = app.main_screen
        retried: list[str] = []
        monkeypatch.setattr(screen._input_flow, "request_retry", lambda text, _draft=None: retried.append(text))
        screen.query_one(InputBar).retry_mode = True

        screen.post_message(ConversationStatusAction.Pressed())
        await pilot.pause()

        assert retried == []
        assert isinstance(app.screen, ConfirmDialog)


@pytest.mark.asyncio
async def test_retry_proceeds_with_confirmed_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _ModelIndicatorApp(_registry())

    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        screen = app.main_screen
        retried: list[str] = []
        monkeypatch.setattr(screen._input_flow, "request_retry", lambda text, _draft=None: retried.append(text))
        screen._state.runtime.details = _runtime_details("Runtime Model")
        screen._state.runtime.details_confirmed = True

        screen.post_message(InputBar.RetryRequested(text="carry on"))
        await pilot.pause()

        assert retried == ["carry on"]
        assert not isinstance(app.screen, ConfirmDialog)
