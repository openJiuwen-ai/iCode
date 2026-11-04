# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Spec tests: deleting a model profile in the Models dialog of a real app on a real engine.

Whatever profile is deleted — the session's own model, the model the session's
agent binds, the default model or one nothing uses — the dialog closes with
``"deleted"``, the screen asks for exactly one settings reload, and the engine's
rebuild drops what no longer resolves: the session runs on what is left.

Written from ``.plan/prompt-shortcuts-engine.md`` (3.3 and item 23), not from
the implementation; the dialog no longer needs to know the session's models.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from textual.widgets import Input, OptionList

from chrys.app.tui.screens.dialogs.confirm import ConfirmDialog
from chrys.app.tui.screens.models.screen import ModelConfigScreen
from chrys.foundation.config.settings import Settings
from chrys.foundation.events import types as ev
from chrys.service.profiles.models.env_bridge import set_global_default_profile_id
from chrys.service.profiles.models.registry import ModelProfileRegistry
from chrys.service.profiles.models.schema import ModelProfile
from chrys.service.profiles.models.serializer import save_profile
from tests.app.tui.screens.test_prompt_shortcut_send import (
    ALPHA,
    BETA,
    FAST_MODEL,
    MAIN_MODEL,
    Session,
    expand_shortcut,
    make_agent,
    model_tag,
    running_session,
    turn_finished,
    write_shortcut,
)
from tests.support.tui_helpers import click_when_settled
from tests.support.waiting import ENGINE_TEST_WAIT_TIMEOUT, ENGINE_TURN_TIMEOUT, wait_for, with_wait_deadline

if TYPE_CHECKING:
    from pathlib import Path

    from tests.support.engines import AgentEngineFactory

pytestmark = pytest.mark.usefixtures("clear_model_profile_env")

SPARE_MODEL = "mock-spare"
BOUND_MODEL = "mock-bound"
BOUND = "Bound"
"""An agent that binds its own model."""

_NAMES = {MAIN_MODEL: "Main", FAST_MODEL: "Fast", SPARE_MODEL: "Spare", BOUND_MODEL: "Bound model"}


def _models_on_disk() -> ModelProfileRegistry:
    """The user's model profiles, saved where the dialog deletes them from; Main is the default model.

    A provider the dialog can show (it has no form for ``mock``); every build gets the scripted model anyway.
    """
    for profile_id, name in _NAMES.items():
        save_profile(ModelProfile(id=profile_id, name=name, provider="openai", model_id="gpt-test", stream=False))
    set_global_default_profile_id(MAIN_MODEL)
    registry = ModelProfileRegistry()
    registry.load_all()
    assert set(registry.list_ids()) == set(_NAMES)
    return registry


async def _session_model(session: Session, model: str) -> None:
    """Give the session its own model the way a user does: send a shortcut that switches to it."""
    write_shortcut("zzownmodel", f"---\nmodel: {model}\n---\nUse your own model")
    await expand_shortcut(session, "zzownmodel")
    await session.pilot.press("enter")
    await wait_for(
        lambda: bool(session.seen.of(ev.UserMessage)) and session.seen.of(ev.UserMessage)[-1].started_turn,
        pilot=session.pilot,
        timeout=ENGINE_TURN_TIMEOUT,
        description="the shortcut's message sent",
    )
    await turn_finished(session)
    assert session.seen.of(ev.SessionOnlySwitch)[-1].applied is True
    # The switch's load dialog closes a moment after the load: keys go to the main screen after that.
    await wait_for(lambda: session.app.screen is session.main, pilot=session.pilot, description="load dialog closed")
    await wait_for(
        lambda: model_tag(session.main) == _NAMES[model], pilot=session.pilot, description="the session's own model"
    )


async def _delete_in_the_models_dialog(session: Session, profile_id: str) -> None:
    """Open the Models dialog, select *profile_id*, delete it, confirm, and close the dialog."""
    app, pilot = session.app, session.pilot
    await pilot.press("f4")
    await wait_for(
        lambda: isinstance(app.screen, ModelConfigScreen) and app.screen.is_mounted,
        pilot=pilot,
        description="Models dialog open",
    )
    dialog = app.screen
    assert isinstance(dialog, ModelConfigScreen)
    profiles = dialog.query_one("#mc-list", OptionList)
    await wait_for(lambda: profiles.option_count > 0, pilot=pilot, description="profiles listed")
    profiles.focus()
    profiles.highlighted = profiles.get_option_index(profile_id)
    await pilot.press("enter")
    await wait_for(
        lambda: dialog.query_one("#mc-name", Input).value == _NAMES[profile_id],
        pilot=pilot,
        description=f"{_NAMES[profile_id]} selected",
    )
    await click_when_settled(pilot, "#mc-delete")
    await wait_for(lambda: isinstance(app.screen, ConfirmDialog), pilot=pilot, description="delete confirmation")
    await click_when_settled(pilot, "#confirm-yes")
    await wait_for(
        lambda: (
            app.screen is dialog
            and profile_id not in {profiles.get_option_at_index(i).id for i in range(profiles.option_count)}
        ),
        pilot=pilot,
        description=f"{_NAMES[profile_id]} deleted",
    )
    await click_when_settled(pilot, "#mc-cancel")
    await wait_for(lambda: app.screen is session.main, pilot=pilot, description="Models dialog closed")


@pytest.mark.parametrize(
    ("deleted", "startup_agent", "own_model", "runs_after"),
    [
        # The session's own model goes: the rebuild drops it, and the session runs on the default.
        pytest.param(FAST_MODEL, ALPHA, FAST_MODEL, MAIN_MODEL, id="the-sessions-own-model"),
        # The model the session's agent binds goes: the agent falls back to the session's model.
        pytest.param(BOUND_MODEL, BOUND, None, MAIN_MODEL, id="the-running-agent-bound-model"),
        # The default model goes: the session keeps its own model, which still resolves.
        pytest.param(MAIN_MODEL, ALPHA, FAST_MODEL, FAST_MODEL, id="the-pointers-model"),
        # A model nothing uses goes: the session runs on what it ran on.
        pytest.param(SPARE_MODEL, ALPHA, None, MAIN_MODEL, id="an-unused-model"),
    ],
)
@with_wait_deadline(ENGINE_TEST_WAIT_TIMEOUT)
async def test_deleting_a_model_closes_with_deleted_reloads_once_and_drops_what_no_longer_resolves(
    agent_engine: AgentEngineFactory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    deleted: str,
    startup_agent: str,
    own_model: str | None,
    runs_after: str,
) -> None:
    """Ambiguity: "the pointer's" model is read as the default model — the settings' model, which is
    also the file default here, since no process pointer is set; deleting it promotes another default,
    which the session, running on a model of its own, does not take (R8, 3.3)."""
    # The reload rereads settings from files and the environment, as in production: keep the
    # workspace scans off there too.
    monkeypatch.setenv("CHRYS_WORKSPACE_CHANGE_NOTICE", "0")
    models = _models_on_disk()
    results: list[object] = []
    dismiss = ModelConfigScreen.dismiss

    def record_dismiss(screen: ModelConfigScreen, result: object = None) -> object:
        results.append(result)
        return dismiss(screen, result)

    monkeypatch.setattr(ModelConfigScreen, "dismiss", record_dismiss)
    agents = (make_agent(ALPHA), make_agent(BETA), make_agent(BOUND, model=BOUND_MODEL))
    async with running_session(
        agent_engine,
        tmp_path,
        monkeypatch,
        agents=agents,
        models=models,
        settings=Settings(model_profile=MAIN_MODEL, workspace_change_notice=False),
        startup_agent=startup_agent,
    ) as session:
        main, pilot, seen = session.main, session.pilot, session.seen
        if own_model is not None:
            await _session_model(session, own_model)
        before_model = BOUND_MODEL if startup_agent == BOUND else (own_model or MAIN_MODEL)
        await wait_for(
            lambda: model_tag(main) == _NAMES[before_model], pilot=pilot, description="the model the session runs"
        )
        reloads_before = len(seen.of(ev.SettingsReload))
        reloaded_before = len(seen.of(ev.SettingsReloaded))
        builds_before = len(session.builds.profiles)

        await _delete_in_the_models_dialog(session, deleted)

        assert results == ["deleted"]
        await wait_for(
            lambda: len(seen.of(ev.SettingsReloaded)) > reloaded_before,
            pilot=pilot,
            timeout=ENGINE_TURN_TIMEOUT,
            description="the reload done",
        )
        reloads = seen.of(ev.SettingsReload)[reloads_before:]
        assert [reload.model_chosen for reload in reloads] == [False]
        # Any deletion rebuilds the agent, once, on what still resolves.
        assert session.builds.profiles[builds_before:] == [runs_after]
        assert seen.of(ev.SettingsReloaded)[-1].runtime_details.model.profile_id == runs_after
        await wait_for(
            lambda: model_tag(main) == _NAMES[runs_after], pilot=pilot, description="the model the session runs now"
        )
        assert models.get(deleted) is None
