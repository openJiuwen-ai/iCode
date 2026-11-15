# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Mounted prompt viewers reject edits and route customization through cloning."""

from __future__ import annotations

import asyncio
import threading

import pytest
from textual import events
from textual.widgets import Button, Input, TextArea

from chrys.app.tui.screens.dialogs.confirm import ConfirmDialog
from tests.app.tui.screens._agent_config_support import (
    _registry,
    _wait_for_hydrated,
    open_agent_config,
)
from tests.support.pilot_barrier import screen_is_settled
from tests.support.tui_helpers import click_when_settled
from tests.support.waiting import ENGINE_TURN_TIMEOUT, wait_for

pytestmark = pytest.mark.usefixtures("isolated_chrys_config_dir")


@pytest.mark.parametrize("name", ["Code", "QA", "Explore", "General"])
@pytest.mark.parametrize(
    "tab,selector", [("instructions", "#ic-instructions"), ("compaction", "#cc-last-words-template")]
)
async def test_builtin_prompt_rejects_first_key_and_clone_is_editable(name, tab, selector):
    registry = _registry()
    original = registry.get(name)
    async with open_agent_config(registry, current_profile=name, initial_tab=tab) as (screen, pilot):
        assert not screen.query("#ac-reset")
        area = screen.query_one(selector, TextArea)
        assert area.read_only and not area.disabled
        before = area.text
        area.focus()
        if name == "Code" and tab == "instructions":
            await click_when_settled(pilot, area)
        else:
            await pilot.press("x")
        await wait_for(
            lambda: isinstance(pilot.app.screen, ConfirmDialog) and pilot.app.screen.is_mounted,
            pilot=pilot,
            description="builtin prompt clone dialog",
        )
        assert area.text == before
        pilot.app.screen.query_one("#confirm-yes", Button).press()
        await wait_for(
            lambda: pilot.app.screen is screen and screen._selected_profile_name == f"{name}-copy",
            pilot=pilot,
            description="editable clone selected",
        )
        await _wait_for_hydrated(screen, pilot)
        area = screen.query_one(selector, TextArea)
        assert not area.read_only
        assert area.text == before
        area.focus()
        await pilot.press("x")
        assert area.text != before
        draft = screen._drafts[screen._selected_draft_key]
        assert not draft.is_builtin and draft.profile.id != original.id
        assert registry.get(f"{name}-copy") is None
    assert registry.get(name) == original


async def test_builtin_viewer_allows_selection_copy_and_compaction_budget_edit(monkeypatch):
    from chrys.app.tui.widgets import text_area

    copied = []
    monkeypatch.setattr(text_area, "copy_text_to_clipboards", lambda app, text: copied.append(text))
    registry = _registry()
    async with open_agent_config(registry, current_profile="Code", initial_tab="compaction") as (screen, pilot):
        area = screen.query_one("#cc-last-words-template", TextArea)
        area.focus()
        await pilot.press("ctrl+a", "ctrl+c")
        assert copied == [area.text]
        assert pilot.app.screen is screen
        budget = screen.query_one("#cc-last-words-max-tokens", Input)
        assert not budget.disabled
        budget.value = "7000"
        screen._sync_selected_draft_from_panels()
        draft = screen._drafts[screen._selected_draft_key]
        assert draft.profile.compaction.last_words_max_output_tokens == 7000
        area.load_text("programmatic edit must not replace builtin")
        harvested = screen._build_profile_from_mounted_panels(draft)
        assert (
            harvested.compaction.last_words_template
            == registry.get_builtin_template("Code").compaction.last_words_template
        )


@pytest.mark.parametrize(
    "tab,selector", [("instructions", "#ic-instructions"), ("compaction", "#cc-last-words-template")]
)
@pytest.mark.parametrize("edit_intent", ["click", "type", "paste"])
async def test_prompt_drag_copies_without_notice_but_later_edit_still_prompts(tab, selector, edit_intent, monkeypatch):
    from chrys.app.tui.widgets import text_area

    copied = []
    monkeypatch.setattr(text_area, "copy_text_to_clipboards", lambda app, text: copied.append(text))
    async with open_agent_config(_registry(), current_profile="Code", initial_tab=tab) as (screen, pilot):
        area = screen.query_one(selector, TextArea)
        before = area.text
        await wait_for(lambda: screen_is_settled(pilot.app, screen), pilot=pilot, description="prompt layout settled")
        # Use the driver's App path: Pilot.mouse_up bypasses the Click that
        # App generates when a drag ends inside the same widget.
        for event_type, column in ((events.MouseDown, 1), (events.MouseMove, 9), (events.MouseUp, 9)):
            x, y = area.region.x + column, area.region.y
            await pilot.app.on_event(
                event_type(
                    area,
                    x=x,
                    y=y,
                    delta_x=0,
                    delta_y=0,
                    button=1,
                    shift=False,
                    meta=False,
                    ctrl=False,
                    screen_x=x,
                    screen_y=y,
                )
            )
            await pilot.pause()
            assert pilot.app.screen is screen
        selected = area.selected_text
        assert selected
        await pilot.press("ctrl+c")
        assert copied == [selected]
        assert area.text == before

        if edit_intent == "click":
            await click_when_settled(pilot, area, offset=(2, 0))
        elif edit_intent == "type":
            await pilot.press("x")
        else:
            area.post_message(events.Paste("replacement"))
        await wait_for(
            lambda: isinstance(pilot.app.screen, ConfirmDialog) and pilot.app.screen.is_mounted,
            pilot=pilot,
            description="clone notice after selection",
        )
        assert area.text == before


async def test_prompt_click_clones_and_saved_prompt_survives(tmp_path):
    registry = _registry()
    async with open_agent_config(registry, current_profile="Code", initial_tab="instructions") as (screen, pilot):
        await click_when_settled(pilot, screen.query_one("#ic-instructions", TextArea))
        await wait_for(
            lambda: isinstance(pilot.app.screen, ConfirmDialog) and pilot.app.screen.is_mounted,
            pilot=pilot,
            description="clone prompt dialog",
        )
        pilot.app.screen.query_one("#confirm-yes", Button).press()
        await wait_for(lambda: screen._selected_profile_name == "Code-copy", pilot=pilot, description="clone selected")
        await _wait_for_hydrated(screen, pilot)
        screen.query_one("#ic-instructions", TextArea).load_text("My custom instructions")
        screen._sync_selected_draft_from_panels()
        screen.query_one("#ac-save", Button).press()
        await wait_for(lambda: registry.get("Code-copy") is not None, pilot=pilot, description="custom prompt saved")
        assert registry.get("Code-copy").instructions == "My custom instructions"
        assert "My custom instructions" in (tmp_path / "agents" / "Code-copy.yaml").read_text()


async def test_running_agent_does_not_offer_prompt_customization():
    registry = _registry()
    async with open_agent_config(registry, current_profile="Code", initial_tab="instructions", read_only=True) as (
        screen,
        pilot,
    ):
        area = screen.query_one("#ic-instructions", TextArea)
        area.focus()
        await pilot.press("x")
        assert pilot.app.screen is screen
        assert screen._selected_profile_name == "Code"


async def test_cancelled_prompt_dialog_can_reopen():
    registry = _registry()
    async with open_agent_config(registry, current_profile="Code", initial_tab="instructions") as (screen, pilot):
        area = screen.query_one("#ic-instructions", TextArea)
        before = area.text
        area.focus()
        await pilot.press("x")
        await wait_for(
            lambda: isinstance(pilot.app.screen, ConfirmDialog) and pilot.app.screen.is_mounted,
            pilot=pilot,
            description="first clone dialog",
        )
        first_dialog = pilot.app.screen
        stack_size = len(pilot.app.screen_stack)
        first_dialog.query_one("#confirm-no", Button).press()
        await wait_for(
            lambda: pilot.app.screen is screen and not screen._prompt_dialog_open,
            pilot=pilot,
            description="cancelled clone dialog",
        )
        area.focus()
        await pilot.press("x")
        await wait_for(
            lambda: isinstance(pilot.app.screen, ConfirmDialog) and pilot.app.screen.is_mounted,
            pilot=pilot,
            description="clone dialog reopened",
        )
        assert pilot.app.screen is not first_dialog
        assert len(pilot.app.screen_stack) == stack_size
        assert area.text == before
        pilot.app.screen.query_one("#confirm-yes", Button).press()
        await wait_for(
            lambda: pilot.app.screen is screen and screen._selected_profile_name == "Code-copy",
            pilot=pilot,
            description="clone selected after retry",
        )


async def test_tui_clone_preserves_self_reference_and_unsaved_draft_identities():
    from chrys.service.profiles.agents.schema import SubAgentRef

    registry = _registry()
    source = registry.get("Code")
    source.sub_agents.agents = [SubAgentRef("Code")]
    async with open_agent_config(registry, current_profile="Code") as (screen, pilot):
        screen.query_one("#ac-clone", Button).press()
        await wait_for(lambda: screen._selected_profile_name == "Code-copy", pilot=pilot, description="first clone")
        await _wait_for_hydrated(screen, pilot)
        first = screen._drafts[screen._selected_draft_key].profile
        screen.query_one("#ac-clone", Button).press()
        await wait_for(lambda: screen._selected_profile_name == "Code-copy-2", pilot=pilot, description="second clone")
        second = screen._drafts[screen._selected_draft_key].profile
        assert len({source.id, first.id, second.id}) == 3
        assert first.sub_agents.agents[0].profile == first.name
        assert second.sub_agents.agents[0].profile == second.name
        assert second.sub_agents.agents[0].tool_name == "Code"
        assert second.sub_agents.agents[0].tool_description == source.description
        assert registry.get(first.name) is None and registry.get(second.name) is None


async def test_save_waits_without_blocking_and_harvests_latest_editor_content():
    from chrys.service.profiles.agents.serializer import profile_directory_lock

    registry = _registry()
    ready, release = threading.Event(), threading.Event()

    def competing_writer():
        with profile_directory_lock(registry.directory):
            ready.set()
            if not release.wait(ENGINE_TURN_TIMEOUT):
                raise TimeoutError("test did not release competing writer")

    async with open_agent_config(registry, current_profile="Code", initial_tab="instructions") as (screen, pilot):
        screen.query_one("#ac-clone", Button).press()
        await wait_for(lambda: screen._selected_profile_name == "Code-copy", pilot=pilot, description="editable clone")
        await _wait_for_hydrated(screen, pilot)
        area = screen.query_one("#ic-instructions", TextArea)
        area.load_text("before waiting")
        writer = asyncio.create_task(asyncio.to_thread(competing_writer))
        try:
            await wait_for(lambda: ready.is_set() or writer.done(), description="competing writer acquired lock")
            if writer.done():
                await writer
            assert ready.is_set()
            button = screen.query_one("#ac-save", Button)
            button.press()
            # Do not drain Pilot's message barrier: Save is deliberately in flight.
            await wait_for(lambda: button.disabled, description="save waiting for writer")
            assert registry.get("Code-copy") is None
            assert not writer.done()
            area.load_text("edited while waiting")
            release.set()
            await writer
            await wait_for(lambda: registry.get("Code-copy") is not None, pilot=pilot, description="save completed")
            assert registry.get("Code-copy").instructions == "edited while waiting"
        finally:
            release.set()
            await writer
