# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""One table, every layer: a shortcut's agent and model, sent in a real app on a real engine.

Each row is one combination of the agent the session runs, the agent and model
the shortcut names, and what each agent binds. One row asserts what the card
says (a model row or not), what the engine builds (the agent and model that
run), what the session keeps as its own model, and whether the user is told
the model was left out. The card measures a model against what would run on
the agent the send runs on, and the engine decides whether that agent runs it
(``model_for_agent``): a row that disagrees anywhere fails here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from chrys.foundation.config.settings import Settings
from chrys.foundation.events import types as ev
from tests.app.tui.screens.test_prompt_shortcut_send import (
    ALPHA,
    BETA,
    FAST_MODEL,
    MAIN_MODEL,
    MODEL_NAMES,
    card_state,
    expand_shortcut,
    make_agent,
    mock_models,
    model_tag,
    notices,
    running_session,
    turn_finished,
    write_shortcut,
)
from tests.support.waiting import ENGINE_TEST_WAIT_TIMEOUT, ENGINE_TURN_TIMEOUT, wait_for, with_wait_deadline

if TYPE_CHECKING:
    from pathlib import Path

    from tests.support.engines import AgentEngineFactory

pytestmark = pytest.mark.usefixtures("clear_model_profile_env")

BOUND = "Bound"
"""A main agent that binds the Fast model."""
THIRD_MODEL = "mock-third"
_NAMES = {**MODEL_NAMES, THIRD_MODEL: "Third"}
_LEFT_OUT = "fixes its own model"
"""From the notice that the card's model was left out."""


@pytest.mark.parametrize(
    ("startup", "agent", "model", "row", "runs_agent", "runs_model", "session_model", "told"),
    [
        # No agent switch.
        pytest.param(ALPHA, "", FAST_MODEL, True, ALPHA, FAST_MODEL, FAST_MODEL, False, id="model-only"),
        pytest.param(BOUND, "", THIRD_MODEL, True, BOUND, FAST_MODEL, "", True, id="model-only-on-bound-agent"),
        pytest.param(BOUND, "", FAST_MODEL, False, BOUND, FAST_MODEL, "", False, id="bound-agents-own-model"),
        # To an agent binding no model: measured against the session's model, not the one running now.
        pytest.param(
            BOUND, BETA, FAST_MODEL, True, BETA, FAST_MODEL, FAST_MODEL, False, id="running-now-not-on-target"
        ),
        pytest.param(ALPHA, BETA, MAIN_MODEL, False, BETA, MAIN_MODEL, "", False, id="target-runs-the-sessions"),
        # To an agent binding its own model: that one runs, and nothing is kept for a later agent.
        pytest.param(ALPHA, BOUND, THIRD_MODEL, True, BOUND, FAST_MODEL, "", True, id="target-binds-another"),
        pytest.param(
            ALPHA, BOUND, MAIN_MODEL, True, BOUND, FAST_MODEL, "", True, id="running-now-target-binds-another"
        ),
        pytest.param(ALPHA, BOUND, FAST_MODEL, False, BOUND, FAST_MODEL, "", False, id="target-binds-the-one-named"),
    ],
)
@with_wait_deadline(ENGINE_TEST_WAIT_TIMEOUT)
async def test_a_shortcuts_agent_and_model_agree_across_the_card_the_engine_and_the_session(
    agent_engine: AgentEngineFactory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    startup: str,
    agent: str,
    model: str,
    row: bool,
    runs_agent: str,
    runs_model: str,
    session_model: str,
    told: bool,
) -> None:
    agents = (make_agent(ALPHA), make_agent(BETA), make_agent(BOUND, model=FAST_MODEL))
    frontmatter = "".join(f"{key}: {value}\n" for key, value in (("agent", agent), ("model", model)) if value)
    write_shortcut("zzpair", f"---\n{frontmatter}---\nReview it")
    async with running_session(
        agent_engine,
        tmp_path,
        monkeypatch,
        agents=agents,
        models=mock_models(_NAMES),
        settings=Settings(model_profile=MAIN_MODEL),
        startup_agent=startup,
    ) as session:
        main, pilot, engine = session.main, session.pilot, session.engine

        await expand_shortcut(session, "zzpair")
        await pilot.pause()
        state = card_state(main)
        assert (state is not None and state.model is not None) is row

        await pilot.press("enter")
        await wait_for(
            lambda: bool(session.seen.of(ev.UserMessage)) and session.seen.of(ev.UserMessage)[-1].started_turn,
            pilot=pilot,
            timeout=ENGINE_TURN_TIMEOUT,
            description="the shortcut's message sent",
        )
        await turn_finished(session)
        await wait_for(lambda: session.app.screen is main, pilot=pilot, description="back on the main screen")

        profile = engine.session.agent_profile
        assert profile is not None
        assert profile.name == runs_agent
        active = engine.current.manifest.active_profile
        assert active is not None
        assert active.id == runs_model
        assert engine.settings_handle.session_model == session_model
        await wait_for(lambda: model_tag(main) == _NAMES[runs_model], pilot=pilot, description="the model shown")
        assert any(_LEFT_OUT in message for _severity, message in notices(session.app)) is told
