# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""/buddy and the sidebar's Buddy tab end to end: the real controller, the real adapter, the real panel."""

from __future__ import annotations

from random import Random

import pytest
from textual.app import App, ComposeResult
from textual.widgets import Static, TabbedContent

from chrys.app.features.buddy import actions, lifecycle
from chrys.app.tui.screens.main.buddy_command import BuddyCommandController
from chrys.app.tui.screens.main.state import MainScreenState
from chrys.app.tui.screens.main.view_adapter import MainScreenViewAdapter
from chrys.app.tui.widgets.sidebar import buddy as buddy_module
from chrys.app.tui.widgets.sidebar.buddy import BuddyPanel
from chrys.app.tui.widgets.sidebar.panel import SidebarPanel
from tests.support.buddies import HeldPetReply, HeldSaveFile, wedge_the_save_file
from tests.support.toasts import shown_toasts
from tests.support.waiting import wait_for

_NEVER = 3600.0


pytestmark = pytest.mark.usefixtures("buddy_reply_gate_left_open")


def _turns() -> int:
    buddy = actions.current_buddy()
    assert buddy is not None
    return buddy.record.turns


class _SidebarApp(App[None]):
    def compose(self) -> ComposeResult:
        yield SidebarPanel()

    @property
    def sidebar(self) -> SidebarPanel:
        return self.query_one(SidebarPanel)

    @property
    def panel(self) -> BuddyPanel:
        return self.sidebar.buddy_panel

    @property
    def active_tab(self) -> str:
        return self.sidebar.query_one(TabbedContent).active

    def controller(self) -> BuddyCommandController:
        return BuddyCommandController(MainScreenViewAdapter(self.screen, state=MainScreenState()))  # type: ignore[arg-type]

    def toasts(self) -> list[tuple[str, str]]:
        """Every toast shown, as (severity, text), oldest first."""
        return [(notification.severity, notification.message) for notification in shown_toasts(self)]

    def label(self, selector: str) -> str:
        return str(self.panel.query_one(selector, Static).render())


@pytest.mark.asyncio
async def test_hatching_from_the_command_opens_the_buddy_tab_on_the_newborn() -> None:
    app = _SidebarApp()
    async with app.run_test(size=(60, 40)) as pilot:
        assert app.active_tab == "tab-toc"
        assert app.panel.query_one(".buddy-empty", Static).display

        app.controller().handle("hatch")
        await wait_for(lambda: app.active_tab == "tab-buddy", pilot=pilot, description="the Buddy tab opens")

        buddy = actions.current_buddy()
        assert buddy is not None
        assert app.panel.buddy == buddy
        assert not app.panel.query_one(".buddy-empty", Static).display
        assert app.label("#buddy-level") == "Level 1\n0/100 XP"
        assert app.label("#buddy-status") == "Click to pet!"
        assert [severity for severity, _text in app.toasts()] == ["information"]
        assert "Out of the egg:" in app.toasts()[0][1]
        await wait_for(
            lambda: buddy.display_name in app.label("#buddy-sprite"),
            pilot=pilot,
            description="the newborn's portrait is drawn",
        )


@pytest.mark.asyncio
async def test_renaming_and_muting_from_the_command_show_on_the_panel(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(buddy_module, "_RELOAD_SECONDS", _NEVER)
    actions.hatch(Random(1))
    app = _SidebarApp()
    async with app.run_test(size=(60, 40)) as pilot:
        controller = app.controller()
        # Open the tab first, and see that opening it has been dealt with: from here on the panel
        # is told about a change by the command or not at all.
        actions.record_turn()
        app.sidebar.focus_tab("tab-buddy")
        await wait_for(
            lambda: app.label("#buddy-level") == "Level 1\n10/100 XP", pilot=pilot, description="the tab is open"
        )

        controller.handle("name Nori")
        await wait_for(
            lambda: "Nori" in app.label("#buddy-sprite"), pilot=pilot, description="the nameplate follows the rename"
        )
        assert app.active_tab == "tab-buddy"

        controller.handle("mute")
        await wait_for(
            lambda: app.label("#buddy-status") == "Click to pet!\n\n🔇 Notifications muted",
            pilot=pilot,
            description="the panel says the buddy is muted",
        )
        assert [severity for severity, _text in app.toasts()] == ["information", "information"]


@pytest.mark.asyncio
async def test_a_command_pet_updates_the_panel_without_taking_the_tab(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(buddy_module, "_RELOAD_SECONDS", _NEVER)
    model = HeldPetReply(monkeypatch)
    actions.hatch(Random(1))
    app = _SidebarApp()
    async with app.run_test(size=(60, 40)) as pilot:
        controller = app.controller()
        assert app.panel.buddy is not None
        assert app.panel.buddy.record.pets == 0

        controller.handle("pet")
        await wait_for(lambda: model.calls == 1, pilot=pilot, description="the model is asked")
        await wait_for(
            lambda: app.panel.buddy is not None and app.panel.buddy.record.pets == 1,
            pilot=pilot,
            description="the panel shows the pet as soon as it is counted",
        )
        model.go.set()
        await wait_for(lambda: app.toasts()[-1:] == [("information", "💛 done")], pilot=pilot, description="answered")

        assert app.active_tab == "tab-toc"
        await controller.shutdown()


@pytest.mark.asyncio
async def test_a_command_pet_counted_after_it_was_answered_still_shows_on_the_panel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(buddy_module, "_RELOAD_SECONDS", _NEVER)
    model = HeldPetReply(monkeypatch)
    actions.hatch(Random(1))
    app = _SidebarApp()
    async with app.run_test(size=(60, 40)) as pilot:
        controller = app.controller()
        hold = HeldSaveFile(monkeypatch)

        controller.handle("pet")
        await wait_for(hold.entered.is_set, pilot=pilot, description="the count has reached the save file")
        model.go.set()
        await wait_for(lambda: app.toasts()[-1:] == [("information", "💛 done")], pilot=pilot, description="answered")
        assert app.panel.buddy is not None
        assert app.panel.buddy.record.pets == 0

        hold.release()
        await wait_for(
            lambda: app.panel.buddy is not None and app.panel.buddy.record.pets == 1,
            pilot=pilot,
            description="the panel shows the pet once it is counted",
        )
        assert app.active_tab == "tab-toc"
        await controller.shutdown()


class _WatchedPanel(BuddyPanel):
    """Counts the times it has come into view. Textual runs the panel's own handler right after this one."""

    shows_handled = 0

    def on_show(self) -> None:
        self.shows_handled += 1


class _PanelApp(App[None]):
    def compose(self) -> ComposeResult:
        yield _WatchedPanel()


@pytest.mark.asyncio
async def test_a_shown_panel_picks_up_a_finished_turn_by_itself(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(buddy_module, "_RELOAD_SECONDS", 0.05)
    actions.hatch(Random(1))
    app = _PanelApp()
    async with app.run_test(size=(60, 40)) as pilot:
        panel = app.query_one(_WatchedPanel)
        # Coming into view reads the save file too. Once that is over, only the clock is left to notice a turn.
        await wait_for(lambda: panel.shows_handled == 1, pilot=pilot, description="coming into view is dealt with")
        assert str(panel.query_one("#buddy-level", Static).render()) == "Level 1\n0/100 XP"

        lifecycle.on_successful_turn()

        await wait_for(
            lambda: str(panel.query_one("#buddy-level", Static).render()) == "Level 1\n10/100 XP",
            pilot=pilot,
            description="the turn's XP shows",
        )
        assert panel.shows_handled == 1


@pytest.mark.asyncio
async def test_coming_back_to_the_tab_shows_the_turns_finished_meanwhile(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(buddy_module, "_RELOAD_SECONDS", _NEVER)
    actions.hatch(Random(1))
    app = _SidebarApp()
    async with app.run_test(size=(60, 40)) as pilot:
        assert app.active_tab == "tab-toc"
        assert app.label("#buddy-level") == "Level 1\n0/100 XP"

        lifecycle.on_successful_turn()
        lifecycle.on_successful_turn()
        await wait_for(lambda: _turns() == 2, pilot=pilot, description="both turns are credited")
        app.sidebar.focus_tab("tab-buddy")

        await wait_for(
            lambda: app.label("#buddy-level") == "Level 1\n20/100 XP", pilot=pilot, description="both turns show"
        )


@pytest.mark.asyncio
async def test_a_wedged_save_file_costs_the_change_and_never_the_screen(monkeypatch: pytest.MonkeyPatch) -> None:
    model = HeldPetReply(monkeypatch)
    actions.hatch(Random(1))
    actions.rename("Nori")
    app = _SidebarApp()
    async with app.run_test(size=(60, 40)) as pilot:
        controller = app.controller()
        wedge_the_save_file(monkeypatch)

        controller.handle("name Mochi")
        await wait_for(lambda: len(app.toasts()) == 1, pilot=pilot, description="the failed rename is reported")
        severity, text = app.toasts()[0]
        assert severity == "warning"
        assert text.startswith("The buddy save file could not be updated")
        assert app.panel.buddy is not None
        assert app.panel.buddy.name == "Nori"

        app.panel.pet()
        assert app.panel.is_petting
        assert app.label("#buddy-status") == "Petting..."
        await wait_for(lambda: model.calls == 1, pilot=pilot, description="the sidebar pet is still answered")
        model.go.set()
        await wait_for(
            lambda: app.toasts()[-1:] == [("information", "💛 done")], pilot=pilot, description="answer toasted"
        )

        controller.handle("pet")
        await wait_for(lambda: model.calls == 2, pilot=pilot, description="the command pet is still answered")
        await wait_for(lambda: controller.pet_task is None, pilot=pilot, description="the second answer lands")
        assert app.panel.buddy.record.pets == 0
