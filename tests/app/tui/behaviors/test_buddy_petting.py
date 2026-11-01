# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Petting while an answer is already on its way, from the slash command and from the sidebar."""

from __future__ import annotations

from random import Random

import pytest
from textual.app import App, ComposeResult
from textual.css.query import NoMatches

from chrys.app.features.buddy import actions
from chrys.app.tui.screens.main.commands import SlashCommandActions
from chrys.app.tui.screens.main.state import MainScreenServices, MainScreenState
from chrys.app.tui.screens.main.suggestions import SuggestionCallbacks, SuggestionHandler
from chrys.app.tui.screens.main.view_adapter import MainScreenViewAdapter
from chrys.app.tui.widgets.sidebar.buddy import BuddyPanel
from chrys.foundation.events.bus import EventBus
from tests.support.buddies import HeldPetReply, HeldSaveFile, assert_reply_gate_open
from tests.support.tui_helpers import discard_worker
from tests.support.waiting import wait_for, wait_until

pytestmark = pytest.mark.usefixtures("buddy_reply_gate_left_open")


def _pets() -> int:
    buddy = actions.current_buddy()
    assert buddy is not None
    return buddy.record.pets


class _SuggestionScreen:
    def __init__(self) -> None:
        self.app = type("_App", (), {"available_themes": ["textual-dark"], "theme": "textual-dark"})()
        self.state = MainScreenState()
        self.services = MainScreenServices(bus=EventBus(), state_store=object())
        self.notifications: list[str] = []

    def _debug(self, *_args, **_kwargs) -> None:
        return

    def action_pick_theme(self) -> None:
        return

    def _resume_last_session(self) -> None:
        return

    def _create_new_session(self) -> None:
        return

    def action_quit(self) -> None:
        return

    def action_sessions(self) -> None:
        return

    def start_chdir(self, _arg: str) -> None:
        return

    def _copy_agent_responses(self, _arg: str) -> None:
        return

    def _toggle_fold(self) -> None:
        return

    def action_show_diff(self) -> None:
        return

    def action_show_rollback(self, _arg: str = "") -> None:
        return

    def start_approval_mode_change(self, _arg: str) -> None:
        return

    def _open_model_config(self) -> None:
        return

    def _open_agent_config(self) -> None:
        return

    def _open_agent_config_tab(self, _tab: str) -> None:
        return

    def notify(self, message: str, **_kwargs) -> None:
        self.notifications.append(message)

    def query_one(self, _cls):
        raise NoMatches("no sidebar in this test")

    def call_after_refresh(self, callback) -> None:
        callback()


def _make_handler(screen: _SuggestionScreen) -> SuggestionHandler:
    view = MainScreenViewAdapter(screen, state=screen.state)  # type: ignore[arg-type]
    return SuggestionHandler(
        state=screen.state,
        services=screen.services,
        view=view,
        command_actions=SlashCommandActions(
            list_themes=lambda: sorted(screen.app.available_themes),
            get_theme=lambda: screen.app.theme,
            apply_theme=lambda name: setattr(screen.app, "theme", name),
            pick_theme=screen.action_pick_theme,
            list_languages=list,
            get_language=lambda: "system",
            apply_language=lambda _requested_locale: None,
            pick_language=lambda: None,
            render_unknown_language_warning=lambda requested_locale: f"Unknown /language locale: {requested_locale}",
            debug_event=screen._debug,
            new_session=screen._create_new_session,
            clear_session=lambda: None,
            quit_app=screen.action_quit,
            resume_session=screen._resume_last_session,
            fork_session=lambda: None,
            workflow_selection=lambda: None,
            open_guide=lambda: None,
            browse_session_list=screen.action_sessions,
            edit_session_title=lambda: None,
            apply_session_title=lambda _title: None,
            change_directory=screen.start_chdir,
            copy_conversation=screen._copy_agent_responses,
            fold_tools=screen._toggle_fold,
            open_diff=screen.action_show_diff,
            open_rollback=screen.action_show_rollback,
            get_approval_mode=lambda: "manual",
            change_approval_mode=screen.start_approval_mode_change,
            configure_model=screen._open_model_config,
            configure_agent=screen._open_agent_config,
            configure_agent_tab=screen._open_agent_config_tab,
            show_runtime_details=lambda: None,
            show_usage=lambda: None,
            configure_settings=lambda _tab: None,
            show_manual_pages=lambda _pages, _start_index: None,
            warn=lambda message, title, timeout: screen.notify(message, title=title, timeout=timeout),
        ),
        callbacks=SuggestionCallbacks(
            notify_warning=lambda message, title, timeout: screen.notify(message, title=title, timeout=timeout),
            start_worker=discard_worker,
            submit_user_text=lambda _text: None,
            start_agent_profile_switch=lambda _profile: None,
            start_model_profile_switch=lambda _profile: None,
        ),
        buddy_view=view,
    )


@pytest.mark.asyncio
async def test_buddy_slash_pet_noops_while_response_is_in_flight(monkeypatch: pytest.MonkeyPatch) -> None:
    model = HeldPetReply(monkeypatch)
    actions.hatch(Random(1))
    handler = _make_handler(_SuggestionScreen())
    handler.build_slash_commands()

    assert handler.dispatch_slash_command("/buddy pet")
    assert handler.dispatch_slash_command("/buddy pet")
    await wait_for(lambda: model.calls == 1, interval=0, description="first pet response started")
    count = handler.buddy_command.count_task
    task = handler.buddy_command.pet_task
    assert count is not None
    assert task is not None
    model.go.set()
    await task
    # The pet stays in hand until its count task is over, not until the save file shows the count:
    # the thread that wrote it may still be on its way back. A pet given in between is dropped whole.
    await count

    assert (model.calls, _pets(), handler.buddy_command.pet_task) == (1, 1, None)
    assert handler.dispatch_slash_command("/buddy pet")
    second_count = handler.buddy_command.count_task
    assert second_count is not None
    assert second_count is not count
    await wait_for(lambda: model.calls == 2, interval=0, description="second pet response started")
    await second_count
    assert _pets() == 2
    await handler.buddy_command.shutdown()


class _PanelApp(App[None]):
    def compose(self) -> ComposeResult:
        yield BuddyPanel()

    def toasts(self) -> list[str]:
        """Live toast texts, oldest first."""
        return [notification.message for notification in self._notifications]


@pytest.mark.asyncio
async def test_buddy_panel_pet_noops_while_response_is_in_flight(monkeypatch: pytest.MonkeyPatch) -> None:
    model = HeldPetReply(monkeypatch)
    actions.hatch(Random(1))
    actions.rename("Nori")

    app = _PanelApp()
    async with app.run_test(size=(42, 40)) as pilot:
        panel = app.query_one(BuddyPanel)

        panel.pet()
        panel.pet()
        await wait_for(lambda: model.calls == 1, pilot=pilot, description="first pet response started")

        assert panel.is_petting
        await wait_for(
            lambda: panel.buddy is not None and panel.buddy.record.pets == _pets() == 1,
            pilot=pilot,
            description="the pet is counted once and the panel shows it",
        )
        assert ["Nori" in toast for toast in app.toasts()] == [True]

        model.go.set()
        await wait_for(lambda: app.toasts()[-1:] == ["💛 done"], pilot=pilot, description="the answer is toasted")
        assert model.calls == 1

        panel.pet()
        await wait_for(lambda: model.calls == 2, pilot=pilot, description="second pet response started")
        await wait_for(lambda: _pets() == 2, pilot=pilot, description="the second pet is counted")


@pytest.mark.asyncio
async def test_a_muted_buddy_is_still_petted_and_counted_but_asks_nobody(monkeypatch: pytest.MonkeyPatch) -> None:
    model = HeldPetReply(monkeypatch)
    actions.hatch(Random(1))
    actions.set_muted(True)

    app = _PanelApp()
    async with app.run_test(size=(42, 40)) as pilot:
        panel = app.query_one(BuddyPanel)

        panel.pet()
        panel.pet()

        assert panel.is_petting
        assert panel._replies.task is None
        await wait_for(lambda: _pets() == 2, pilot=pilot, description="both pets are counted")
        # Asking and toasting both happen a step later, so give them the chance they must not take.
        assert not await wait_until(lambda: model.calls or app.toasts(), timeout=0.5, pilot=pilot)


@pytest.mark.asyncio
async def test_closing_the_panel_stops_waiting_for_the_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    model = HeldPetReply(monkeypatch)
    actions.hatch(Random(1))

    async with _PanelApp().run_test(size=(42, 40)) as pilot:
        panel = pilot.app.query_one(BuddyPanel)
        panel.pet()
        await wait_for(lambda: model.calls == 1, pilot=pilot, description="pet response started")
        task = panel._replies.task
        assert task is not None

        await panel.remove()

        # The panel's own teardown did this. The app, which would have cancelled the orphan too, is still running.
        assert task.cancelled()
        assert panel._replies.task is None
        assert_reply_gate_open()


@pytest.mark.asyncio
async def test_a_pet_is_counted_on_a_thread_while_the_panel_goes_on(monkeypatch: pytest.MonkeyPatch) -> None:
    actions.hatch(Random(1))
    actions.set_muted(True)  # nobody to ask: the count is all that is left of a pet
    app = _PanelApp()
    async with app.run_test(size=(42, 40)) as pilot:
        panel = app.query_one(BuddyPanel)
        hold = HeldSaveFile(monkeypatch)

        panel.pet()

        assert panel.is_petting
        # Seen from the loop while the count sits on the save file: the loop is not the one sitting there.
        await wait_for(hold.entered.is_set, pilot=pilot, description="the count has reached the save file")
        assert _pets() == 0
        hold.release()
        await wait_for(
            lambda: panel.buddy is not None and panel.buddy.record.pets == _pets() == 1,
            pilot=pilot,
            description="the pet is counted and the panel shows it",
        )


@pytest.mark.asyncio
async def test_closing_the_panel_stops_waiting_for_the_count_and_the_count_still_lands(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actions.hatch(Random(1))
    actions.set_muted(True)
    app = _PanelApp()
    async with app.run_test(size=(42, 40)) as pilot:
        panel = app.query_one(BuddyPanel)
        hold = HeldSaveFile(monkeypatch)
        panel.pet()
        await wait_for(hold.entered.is_set, pilot=pilot, description="the count has reached the save file")
        (worker,) = [worker for worker in app.workers if worker.node is panel]

        await panel.remove()

        await wait_for(lambda: worker.is_cancelled, pilot=pilot, description="the panel's own teardown gave up waiting")
        hold.release()
        await wait_for(lambda: _pets() == 1, pilot=pilot, description="the count lands on its thread")
