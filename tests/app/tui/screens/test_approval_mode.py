# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for the approval mode picker."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from textual.app import App, ComposeResult
from textual.widgets import OptionList, Static
from textual.widgets.option_list import Option

from chrys.app.tui.screens.dialogs.approval.mode import ApprovalModeScreen
from chrys.app.tui.screens.main.config_actions import _session_relay_risk
from chrys.app.tui.screens.main.state import MainScreenServices, MainScreenState, RuntimeState
from chrys.app.tui.widgets.option_menu import MenuOptionList
from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import AgentRuntimeDetails, RuntimeModelDetails
from chrys.service.approval.policy import ApprovalMode
from chrys.service.profiles.models.registry import ModelProfileRegistry
from chrys.service.profiles.models.schema import ModelProfile
from chrys.service.session.runtime_metadata import SessionRuntimeMetadata


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


class _EngineView:
    """An engine that is not the chat engine: no session latch is read from it."""

    def __init__(self) -> None:
        # A latch sitting here must be ignored, whatever the provider returns.
        self.session = SimpleNamespace(runtime_meta=SessionRuntimeMetadata(relay_risk_seen=True))


def _registry() -> ModelProfileRegistry:
    registry = ModelProfileRegistry()
    registry.register(
        ModelProfile(
            id="relay-a",
            name="Relay A",
            provider="openai",
            model_id="gpt-x",
            base_url="https://relay.example.com/v1",
            api_key="sk-relay",
            is_relay=True,
        )
    )
    registry.register(
        ModelProfile(
            id="direct-b",
            name="Direct B",
            provider="openai",
            model_id="gpt-y",
            base_url="https://direct.example.com/v1",
            api_key="sk-direct",
        )
    )
    return registry


def _relay_risk_view(
    *,
    running_profile_id: str,
    confirmed: bool,
    active_profile_id: str = "direct-b",
    engine_provider=None,
) -> bool:
    state = MainScreenState(
        runtime=RuntimeState(
            details=AgentRuntimeDetails(model=RuntimeModelDetails(profile_id=running_profile_id)),
            details_confirmed=confirmed,
        )
    )
    services = MainScreenServices(
        bus=EventBus(),
        model_registry=_registry(),
        active_model_profile_id=active_profile_id,
        engine_provider=engine_provider,
    )
    return _session_relay_risk(state, services)


def test_relay_risk_keys_on_the_running_model_not_the_global_setting() -> None:
    # The build runs the agent's bound relay model while the globally active
    # profile is direct: the warning must show.
    assert _relay_risk_view(running_profile_id="relay-a", confirmed=True) is True
    # The globally active profile is a relay, but the build runs a direct
    # model and nothing latched: the warning must not show.
    assert _relay_risk_view(running_profile_id="direct-b", confirmed=True, active_profile_id="relay-a") is False


def test_relay_risk_stays_on_through_the_chat_session_latch(agent_engine) -> None:
    # A non-chat engine provider contributes no latch, whatever sits behind
    # its ``session``.
    non_chat = _relay_risk_view(running_profile_id="direct-b", confirmed=True, engine_provider=_EngineView)
    assert non_chat is False

    engine = agent_engine(EventBus())

    def view() -> bool:
        return _relay_risk_view(running_profile_id="direct-b", confirmed=True, engine_provider=lambda: engine)

    assert view() is False
    # After a relay → direct switch the latch keeps the risk on.
    engine.session.runtime_meta.relay_risk_seen = True
    assert view() is True


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
