# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Engine-driven: every session runs on its own agent and model; a new session starts on the default ones.

Written from ``.plan/prompt-shortcuts-engine.md`` (rules R8-R10, §3, and §7 items 11-15, 27-29 and 31), not from
the implementation. A shortcut's switch (``SessionOnlySwitch``) sets the session's own agent and model; that
choice ends only when the session closes, when the user picks the same kind themselves (``#`` / ``$``), when the
profile it names is deleted, or when a restore gives the session its own saved choice. The defaults: the agent in
use at launch or picked with ``#`` (``ActiveSession.new_session_agent``) and the settings' model
(``SettingsHandle.base``), which the process pointer stands for. Every test drives a real engine build over mock
model clients.
"""

from __future__ import annotations

import asyncio
import dataclasses
from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import create_autospec

import pytest

import chrys.orchestration.engine.build.builder as builder_module
from chrys.foundation.config.runtime_pointer import MODEL_POINTER_KEY, get_model_pointer
from chrys.foundation.config.settings_store import LoadedSettings, load_settings
from chrys.foundation.config.spec import Source
from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import (
    AgentLoadFailed,
    AgentLoadStarted,
    AgentProfileSwitch,
    Event,
    ModelProfileSwitched,
    RollbackResult,
    SessionClear,
    SessionNew,
    SessionOnlySwitch,
    SessionRestore,
    SessionRestored,
    SetModelProfile,
    SettingsReload,
    UserMessage,
    UserRollback,
    WorkflowRunStarted,
    WorkspaceChange,
)
from chrys.foundation.models.workspace import Workspace
from chrys.service.llm.mock import MockChatClient, MockResponse
from chrys.service.profiles.agents.registry import AgentProfileRegistry
from chrys.service.profiles.agents.schema import ModelConfig
from chrys.service.profiles.models.env_bridge import activate_model_profile
from chrys.service.profiles.models.registry import ModelProfileRegistry
from chrys.service.profiles.models.resolver import loaded_with_active_model_profile
from chrys.service.profiles.models.schema import ModelProfile
from chrys.service.state.store import JsonFileStateStore
from tests.orchestration.engine._recovery_helpers import (
    _model_registry,
    _profile,
    _registry,
    _seed_restorable_session,
)
from tests.orchestration.workflows._hosting import (
    confirm,
    make_host,
    make_project,
    of_type,
    patch_runtime,
    run,
    write_workflow,
)
from tests.support.event_capture import capture_event_sequence, collect_events
from tests.support.waiting import (
    ENGINE_TEST_WAIT_TIMEOUT,
    ENGINE_TURN_TIMEOUT,
    await_run_task_chain,
    wait_for,
    with_wait_deadline,
)

if TYPE_CHECKING:
    from chrys.orchestration.engine.engine import AgentEngine
    from chrys.service.profiles.agents.schema import AgentProfile
    from tests.support.engines import AgentEngineFactory

DEFAULT_MODEL = "model-a"
"""The settings' model: what the process pointer names at launch."""
OTHER_MODEL = "model-b"
SAVED_MODEL = "model-c"

_CODE = _profile("Code", "Code Agent")
_DOCS = _profile("Docs", "Docs Agent")
_QA = _profile("QA", "QA Agent")
# Registration order matters where the launch chain ends at "the first main profile": Docs comes before QA.
_AGENTS = (_CODE, _DOCS, _QA)


def _mock_model(profile_id: str) -> ModelProfile:
    return ModelProfile(
        id=profile_id, name=profile_id, provider="mock", model_id=profile_id, max_context_tokens=100_000, stream=False
    )


@pytest.fixture(autouse=True)
def _default_model_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """The default model is the process pointer; teardown gives back whatever a test or the engine wrote to it."""
    monkeypatch.setenv("CHRYS_MODEL_PROFILE", DEFAULT_MODEL)


class _Clients:
    """Hand every build a fresh mock client; ``fail_builds`` fails the next builds, ``hold`` parks the next one."""

    def __init__(self) -> None:
        self.fail_builds = 0
        self.created = 0
        self.hold: asyncio.Event | None = None
        self.held = asyncio.Event()

    async def create(self, *_args: Any, **_kwargs: Any) -> MockChatClient:
        if self.hold is not None:
            hold, self.hold = self.hold, None
            self.held.set()
            await hold.wait()
        if self.fail_builds:
            self.fail_builds -= 1
            raise RuntimeError("model client unavailable")
        self.created += 1
        return MockChatClient(responses=[MockResponse(text=f"reply {self.created}.{n}") for n in range(6)])


@dataclass
class _Started:
    engine: AgentEngine
    clients: _Clients
    store: JsonFileStateStore
    agents: AgentProfileRegistry
    models: ModelProfileRegistry
    workspace: Path


async def _start(
    agent_engine: AgentEngineFactory,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    loaded: LoadedSettings | None = None,
    agent: AgentProfile = _CODE,
    agents: Sequence[AgentProfile] = _AGENTS,
    launch: bool = True,
) -> _Started:
    """Build the engine and, with *launch*, start it on *agent* as a host names its launch agent."""
    clients = _Clients()
    monkeypatch.setattr(
        builder_module, "create_client", create_autospec(builder_module.create_client, side_effect=clients.create)
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    store = JsonFileStateStore(tmp_path / "sessions")
    agent_registry = _registry(*agents)
    model_registry = _model_registry(_mock_model(DEFAULT_MODEL), _mock_model(OTHER_MODEL), _mock_model(SAVED_MODEL))
    engine: AgentEngine = agent_engine(
        EventBus(),
        loaded_settings=loaded if loaded is not None else load_settings(),
        agent_registry=agent_registry,
        model_registry=model_registry,
        state_store=store,
        initial_workspace=Workspace.from_cwd(str(workspace)),
    )
    if launch:
        await engine.start(agent)
        assert engine.current.loaded is not None
    return _Started(engine, clients, store, agent_registry, model_registry, workspace)


async def _publish(engine: AgentEngine, event: Event) -> None:
    await engine.event_bus.publish(event, raise_handler_errors=True)


async def _switch(engine: AgentEngine, *, agent: str = "", model: str = "", strict: bool = True) -> SessionOnlySwitch:
    """Publish a shortcut's switch as the TUI does; *strict* False lets the bus swallow a failed build, as in the TUI."""
    event = SessionOnlySwitch(agent=agent, model_profile=model)
    await engine.event_bus.publish(event, raise_handler_errors=strict)
    return event


def _agent(engine: AgentEngine) -> str:
    profile = engine.session.agent_profile
    assert profile is not None
    return profile.name


def _model(engine: AgentEngine) -> str:
    """The model the running build was made on."""
    active = engine.current.manifest.active_profile
    assert active is not None
    return active.id


def _session_model(engine: AgentEngine) -> str:
    return engine.settings_handle.session_model


def _default_model(engine: AgentEngine) -> str:
    return engine.settings_handle.base.settings.model_profile


def _assert_defaults(engine: AgentEngine, *, agent: str = "Code", model: str = DEFAULT_MODEL) -> None:
    """The session runs on the default agent and model, with no choice of its own."""
    assert _agent(engine) == agent
    assert _model(engine) == model
    assert engine.settings.model_profile == model
    assert _session_model(engine) == ""


async def _complete_turn(engine: AgentEngine, text: str = "hello") -> None:
    message = UserMessage(text=text)
    await engine.event_bus.publish(message)
    assert message.started_turn is True
    await await_run_task_chain(engine, turn_state=engine.turns.turn_state, timeout=ENGINE_TURN_TIMEOUT)


async def _restore(engine: AgentEngine, session_id: str, **kwargs: Any) -> None:
    async with capture_event_sequence(engine.event_bus, SessionRestored) as restored:
        await _publish(engine, SessionRestore(session_id=session_id, **kwargs))
    assert [e.session_id for e in restored if isinstance(e, SessionRestored)] == [session_id]


def _command_line_model(monkeypatch: pytest.MonkeyPatch, profile_id: str) -> LoadedSettings:
    """``icode -m <profile_id>``: the flag parks its value in the pointer and credits the command line."""
    monkeypatch.setenv("CHRYS_MODEL_PROFILE", profile_id)
    return loaded_with_active_model_profile(load_settings(), _mock_model(profile_id), Source.CLI)


# ---------------------------------------------------------------------------
# Item 11: the switch is one rebuild, both halves or neither (R9)
# ---------------------------------------------------------------------------


async def test_one_switch_of_agent_and_model_is_one_rebuild_on_the_session_s_own_model(
    agent_engine: AgentEngineFactory, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    started = await _start(agent_engine, monkeypatch, tmp_path)
    engine = started.engine

    async with capture_event_sequence(engine.event_bus, AgentLoadStarted, ModelProfileSwitched) as seen:
        switch = await _switch(engine, agent="QA", model=OTHER_MODEL)

    assert switch.applied is True
    assert len([e for e in seen if isinstance(e, AgentLoadStarted)]) == 1
    assert [e.model_profile_id for e in seen if isinstance(e, ModelProfileSwitched)] == [OTHER_MODEL]
    assert _agent(engine) == "QA"
    assert _model(engine) == OTHER_MODEL
    assert _session_model(engine) == OTHER_MODEL
    # The defaults stay where they were (R8): the switch is the session's own.
    assert _default_model(engine) == DEFAULT_MODEL
    assert engine.session.new_session_agent == "Code"
    assert get_model_pointer()[0] == DEFAULT_MODEL


async def test_a_model_the_target_agent_overrides_is_left_out_of_the_switch(
    agent_engine: AgentEngineFactory, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    bound = dataclasses.replace(_profile("Bound"), model=ModelConfig(profile_id=SAVED_MODEL))
    started = await _start(agent_engine, monkeypatch, tmp_path, agents=(*_AGENTS, bound))
    engine = started.engine

    switch = await _switch(engine, agent="Bound", model=OTHER_MODEL)

    # The agent's own model runs; the model asked for is not kept to surface on a later agent, and that is said.
    assert switch.applied is True
    assert switch.model_left_out is True
    assert _agent(engine) == "Bound"
    assert _model(engine) == SAVED_MODEL
    assert _session_model(engine) == ""
    await _switch(engine, agent="Code")
    assert _model(engine) == DEFAULT_MODEL


async def test_the_model_the_target_agent_runs_anyway_is_not_made_the_session_s(
    agent_engine: AgentEngineFactory, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    bound = dataclasses.replace(_profile("Bound"), model=ModelConfig(profile_id=SAVED_MODEL))
    started = await _start(agent_engine, monkeypatch, tmp_path, agents=(*_AGENTS, bound))
    engine = started.engine

    switch = await _switch(engine, agent="Bound", model=SAVED_MODEL)

    # Its own model runs either way: nothing to ask for, nothing left out, nothing kept for a later agent.
    assert switch.applied is True
    assert switch.model_left_out is False
    assert _model(engine) == SAVED_MODEL
    assert _session_model(engine) == ""
    await _switch(engine, agent="Code")
    assert _model(engine) == DEFAULT_MODEL


async def test_a_switch_that_asks_for_what_is_in_force_rebuilds_nothing(
    agent_engine: AgentEngineFactory, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    started = await _start(agent_engine, monkeypatch, tmp_path)
    engine = started.engine

    async with capture_event_sequence(engine.event_bus, AgentLoadStarted) as seen:
        switch = await _switch(engine, agent="Code", model=DEFAULT_MODEL)

    assert switch.applied is True
    assert seen == []
    assert started.clients.created == 1


async def test_a_switch_whose_build_fails_changes_neither_the_agent_nor_the_model(
    agent_engine: AgentEngineFactory, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    started = await _start(agent_engine, monkeypatch, tmp_path)
    engine = started.engine
    failures: list[AgentLoadFailed] = []
    await engine.event_bus.subscribe(AgentLoadFailed, lambda e: collect_events(failures, e))

    started.clients.fail_builds = 1
    switch = await _switch(engine, agent="QA", model=OTHER_MODEL, strict=False)

    assert failures, "the switch's build was expected to fail"
    assert switch.applied is False
    _assert_defaults(engine)
    # Nothing half-applied is left for the next rebuild to pick up.
    await _publish(engine, SettingsReload())
    _assert_defaults(engine)


async def test_a_switch_with_nothing_loaded_reports_its_failed_build_and_a_later_one_starts_the_session(
    agent_engine: AgentEngineFactory, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    started = await _start(agent_engine, monkeypatch, tmp_path)
    engine = started.engine
    # "Nothing loaded" is reached the way a user reaches it: a new session whose build failed.
    started.clients.fail_builds = 1
    await engine.event_bus.publish(SessionNew())
    assert engine.current.loaded is None, "the new session's start was expected to fail"
    before = _agent(engine)

    started.clients.fail_builds = 1
    failed = await _switch(engine, agent="QA", model=OTHER_MODEL, strict=False)

    assert failed.applied is False
    assert engine.current.loaded is None
    # Neither half stays: the next start is not on the switch's agent or model.
    assert _agent(engine) == before
    assert _session_model(engine) == ""
    # Positive control: the same switch with a working build starts the session on both halves.
    applied = await _switch(engine, agent="QA", model=OTHER_MODEL)
    assert applied.applied is True
    assert engine.current.loaded is not None
    assert _agent(engine) == "QA"
    assert _model(engine) == OTHER_MODEL


async def test_a_switch_that_starts_a_host_s_first_session_leaves_the_new_session_agent_unnamed(
    agent_engine: AgentEngineFactory, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A host that named no launch agent (one that starts by restoring) has nothing loaded: the switch's agent
    starts the session as its own and never becomes the agent new sessions start on. A host's own start names it
    (``test_one_switch_of_agent_and_model_is_one_rebuild_on_the_session_s_own_model``)."""
    started = await _start(agent_engine, monkeypatch, tmp_path, launch=False)
    engine = started.engine
    # What a host that restores at launch does first (``ChrysSessionHost.restore_session``).
    await engine.prepare()
    assert engine.current.loaded is None
    assert engine.session.new_session_agent == ""

    applied = await _switch(engine, agent="QA", model=OTHER_MODEL)

    assert applied.applied is True
    assert engine.current.loaded is not None
    assert _agent(engine) == "QA"
    assert engine.session.new_session_agent == ""


# ---------------------------------------------------------------------------
# Item 12: every rebuild builds on the session's own model (§3.1)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("rebuild", ["settings_reload", "workspace_change", "rollback_restore"])
@with_wait_deadline(ENGINE_TEST_WAIT_TIMEOUT)
async def test_rebuilds_keep_the_session_s_own_model(
    agent_engine: AgentEngineFactory, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, rebuild: str
) -> None:
    started = await _start(agent_engine, monkeypatch, tmp_path)
    engine = started.engine
    await _switch(engine, model=OTHER_MODEL)
    assert engine.current.manifest.active_profile is not None
    assert engine.current.manifest.active_profile.id == OTHER_MODEL
    builds = started.clients.created

    if rebuild == "settings_reload":
        await _publish(engine, SettingsReload())
    elif rebuild == "workspace_change":
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        await _publish(engine, WorkspaceChange(primary_cwd=str(elsewhere)))
        assert engine.workspace_primary_cwd == str(elsewhere)
    else:
        await _complete_turn(engine, "one")
        await _complete_turn(engine, "two")
        results: list[RollbackResult] = []
        await engine.event_bus.subscribe(RollbackResult, lambda e: collect_events(results, e))
        # §3.4 says a rollback stages "no session model (unchanged)": read as the session's own layer staying
        # as it is (a rollback restores the current session in place, R10a), not as dropping it.
        await _publish(engine, UserRollback(target_turn=1, revert_changes=False))
        assert [result.target_turn for result in results] == [1]

    assert started.clients.created > builds, "the step under test was expected to rebuild the agent"
    assert engine.current.manifest.active_profile is not None
    assert engine.current.manifest.active_profile.id == OTHER_MODEL
    assert _session_model(engine) == OTHER_MODEL
    assert _default_model(engine) == DEFAULT_MODEL


# ---------------------------------------------------------------------------
# Item 13: closing the session ends its own choice; closing it in place keeps it (R10a)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("close", ["new", "clear"])
@with_wait_deadline(ENGINE_TEST_WAIT_TIMEOUT)
async def test_new_and_cleared_sessions_start_on_the_default_agent_and_model(
    agent_engine: AgentEngineFactory, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, close: str
) -> None:
    started = await _start(agent_engine, monkeypatch, tmp_path)
    engine = started.engine
    await _complete_turn(engine)
    old_session = engine.session_id
    await _switch(engine, agent="QA", model=OTHER_MODEL)

    await _publish(engine, SessionNew() if close == "new" else SessionClear(session_id=old_session))

    assert engine.session_id != old_session
    _assert_defaults(engine)


async def test_a_session_model_then_a_hash_pick_then_a_new_session_runs_the_picked_agent_on_the_default_model(
    agent_engine: AgentEngineFactory, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    started = await _start(agent_engine, monkeypatch, tmp_path)
    engine = started.engine
    await _switch(engine, model=OTHER_MODEL)

    await _publish(engine, AgentProfileSwitch(profile_name="Docs"))

    # ``#`` rebuilds on the session's own model; it picks only the agent.
    assert _agent(engine) == "Docs"
    assert _model(engine) == OTHER_MODEL
    assert engine.session.new_session_agent == "Docs"
    await _publish(engine, SessionNew())
    # The default model never absorbed the session's: the next session has the default model and ``#``'s agent.
    _assert_defaults(engine, agent="Docs")


@pytest.mark.parametrize("close", ["restore_current", "reset_to_welcome"])
@with_wait_deadline(ENGINE_TEST_WAIT_TIMEOUT)
async def test_closing_the_session_in_place_keeps_its_own_agent_and_model(
    agent_engine: AgentEngineFactory, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, close: str
) -> None:
    started = await _start(agent_engine, monkeypatch, tmp_path)
    engine = started.engine
    await _switch(engine, agent="QA", model=OTHER_MODEL)
    await _complete_turn(engine)
    current = engine.session_id
    assert current is not None

    if close == "restore_current":
        await _restore(engine, current)
    else:
        results: list[RollbackResult] = []
        await engine.event_bus.subscribe(RollbackResult, lambda e: collect_events(results, e))
        await _publish(engine, UserRollback(target_turn=0, revert_changes=False))
        assert [result.target_turn for result in results] == [0]

    assert engine.session_id == current
    assert _agent(engine) == "QA"
    assert _model(engine) == OTHER_MODEL
    assert _session_model(engine) == OTHER_MODEL
    # Still the session's own: a new session leaves both behind.
    await _publish(engine, SessionNew())
    _assert_defaults(engine)


async def test_a_restore_that_fails_after_the_close_leaves_the_defaults_for_the_next_start(
    agent_engine: AgentEngineFactory, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    started = await _start(agent_engine, monkeypatch, tmp_path)
    engine = started.engine
    await _seed_restorable_session(started.store, agent_profile="Docs", model_profile_id=SAVED_MODEL)
    await _switch(engine, agent="QA", model=OTHER_MODEL)
    lifecycle = engine.lifecycle
    real_start = lifecycle.start
    failed: list[str] = []

    async def start(profile: AgentProfile, **kwargs: Any) -> None:
        if kwargs.get("operation") == "restore" and not failed:
            failed.append(profile.name)
            raise RuntimeError("the restored session's build failed")
        await real_start(profile, **kwargs)

    monkeypatch.setattr(lifecycle, "start", create_autospec(real_start, side_effect=start))

    # Default delivery: the bus logs and swallows the handler's failure, as in the TUI.
    await engine.event_bus.publish(SessionRestore(session_id="restore_me", apply_saved_model=True))

    assert failed == ["Docs"], "the restore was expected to fail once the session had closed"
    assert engine.current.loaded is None
    # The restore's own model went with its failed build; the close had already dropped the session's.
    # ``session.agent_profile`` is not asserted here: the restore names its agent before its start fails, and
    # §3.1 promises the defaults to the next start, which is what the rest checks.
    assert _session_model(engine) == ""
    await _publish(engine, SessionNew())
    _assert_defaults(engine)


# ---------------------------------------------------------------------------
# Item 14: the user's own pick, deletions, and reloads (R10b, R10c)
# ---------------------------------------------------------------------------


async def test_a_hash_pick_makes_the_agent_the_session_s_and_the_default(
    agent_engine: AgentEngineFactory, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    started = await _start(agent_engine, monkeypatch, tmp_path)
    engine = started.engine
    await _switch(engine, agent="Docs")
    assert engine.session.new_session_agent == "Code"

    await _publish(engine, AgentProfileSwitch(profile_name="QA"))

    assert _agent(engine) == "QA"
    assert engine.session.new_session_agent == "QA"
    await _publish(engine, SessionNew())
    _assert_defaults(engine, agent="QA")


@pytest.mark.parametrize("deleted", ["session_agent", "default_agent"])
async def test_a_fallback_switch_keeps_the_default_agent_unless_the_default_was_deleted(
    agent_engine: AgentEngineFactory, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, deleted: str
) -> None:
    # §3.3: a fallback sets the new-session agent "only when the current one no longer resolves", read as the
    # current new-session agent (a deletion or rename of the default), never the session's own agent.
    started = await _start(agent_engine, monkeypatch, tmp_path)
    engine = started.engine
    if deleted == "session_agent":
        # The session runs on its own QA; the agents dialog deletes QA and falls back on Docs.
        await _switch(engine, agent="QA")
        assert started.agents.remove("QA")
        await _publish(engine, AgentProfileSwitch(profile_name="Docs", source="fallback"))
        assert _agent(engine) == "Docs"
        assert engine.session.new_session_agent == "Code"
        await _publish(engine, SessionNew())
        _assert_defaults(engine)
    else:
        # The session runs on the default, Code, which the dialog deletes, falling back on QA. Without Code the
        # launch chain would land on the first main profile, Docs: QA here can only come from the fallback.
        assert started.agents.remove("Code")
        await _publish(engine, AgentProfileSwitch(profile_name="QA", source="fallback"))
        assert _agent(engine) == "QA"
        assert engine.session.new_session_agent == "QA"
        await _publish(engine, SessionNew())
        _assert_defaults(engine, agent="QA")


async def test_a_dollar_pick_drops_the_session_model_and_becomes_the_default(
    agent_engine: AgentEngineFactory, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    started = await _start(agent_engine, monkeypatch, tmp_path)
    engine = started.engine
    await _switch(engine, model=OTHER_MODEL)

    # What the ``$`` list does: activate the picked profile, then ask for a reload saying a model was chosen.
    await asyncio.to_thread(activate_model_profile, SAVED_MODEL)
    await _publish(engine, SettingsReload(model_chosen=True))

    assert _model(engine) == SAVED_MODEL
    assert _session_model(engine) == ""
    assert _default_model(engine) == SAVED_MODEL
    await _publish(engine, SessionNew())
    _assert_defaults(engine, model=SAVED_MODEL)


async def test_a_reload_keeps_the_session_model_until_its_profile_is_deleted(
    agent_engine: AgentEngineFactory, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    started = await _start(agent_engine, monkeypatch, tmp_path)
    engine = started.engine
    await _switch(engine, model=OTHER_MODEL)

    await _publish(engine, SettingsReload())
    assert _model(engine) == OTHER_MODEL
    assert _session_model(engine) == OTHER_MODEL

    assert started.models.remove(OTHER_MODEL)
    await _publish(engine, SettingsReload())
    _assert_defaults(engine)


async def test_a_command_line_model_keeps_its_attribution_across_a_reload_with_a_session_model(
    agent_engine: AgentEngineFactory, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    started = await _start(agent_engine, monkeypatch, tmp_path, loaded=_command_line_model(monkeypatch, SAVED_MODEL))
    engine = started.engine
    assert engine.loaded_settings.source_for(MODEL_POINTER_KEY).layer is Source.CLI
    await _switch(engine, model=OTHER_MODEL)

    await _publish(engine, SettingsReload())

    assert _model(engine) == OTHER_MODEL
    base = engine.settings_handle.base
    assert base.settings.model_profile == SAVED_MODEL
    assert base.source_for(MODEL_POINTER_KEY).layer is Source.CLI
    await _publish(engine, SessionNew())
    _assert_defaults(engine, model=SAVED_MODEL)
    assert engine.loaded_settings.source_for(MODEL_POINTER_KEY).layer is Source.CLI


# ---------------------------------------------------------------------------
# Item 15 (M6): a restore gives the session its own saved choice and leaves the defaults (R10d)
# ---------------------------------------------------------------------------


async def test_restoring_another_session_runs_it_on_its_own_agent_and_model_and_leaves_the_defaults(
    agent_engine: AgentEngineFactory, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    started = await _start(agent_engine, monkeypatch, tmp_path)
    engine = started.engine
    await _seed_restorable_session(started.store, agent_profile="QA", model_profile_id=SAVED_MODEL)

    await _restore(engine, "restore_me", apply_saved_model=True)

    assert _agent(engine) == "QA"
    assert _model(engine) == SAVED_MODEL
    assert _session_model(engine) == SAVED_MODEL
    assert _default_model(engine) == DEFAULT_MODEL
    assert engine.session.new_session_agent == "Code"
    # The process pointer is the default model: a restore no longer writes it.
    assert get_model_pointer()[0] == DEFAULT_MODEL
    await _publish(engine, SessionNew())
    _assert_defaults(engine)


async def test_restoring_another_session_with_a_command_line_model_takes_no_session_model(
    agent_engine: AgentEngineFactory, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    started = await _start(agent_engine, monkeypatch, tmp_path, loaded=_command_line_model(monkeypatch, OTHER_MODEL))
    engine = started.engine
    await _seed_restorable_session(started.store, agent_profile="QA", model_profile_id=SAVED_MODEL)

    # The TUI with ``-m`` restores without applying the saved model.
    await _restore(engine, "restore_me", apply_saved_model=False)

    assert _agent(engine) == "QA"
    assert _model(engine) == OTHER_MODEL
    assert _session_model(engine) == ""
    assert get_model_pointer()[0] == OTHER_MODEL


# ---------------------------------------------------------------------------
# Item 27: live choices and the session layer (§3.1)
# ---------------------------------------------------------------------------


@with_wait_deadline(ENGINE_TEST_WAIT_TIMEOUT)
async def test_a_live_choice_made_during_a_switch_s_rebuild_survives_its_commit(
    agent_engine: AgentEngineFactory, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    started = await _start(agent_engine, monkeypatch, tmp_path)
    engine = started.engine
    assert engine.settings.chat_thinking_expanded is False
    release = asyncio.Event()
    started.clients.hold = release
    switch = SessionOnlySwitch(model_profile=OTHER_MODEL)
    publish = asyncio.create_task(engine.event_bus.publish(switch, raise_handler_errors=True))
    try:
        await wait_for(
            lambda: started.clients.held.is_set() or publish.done(), description="the switch's build in progress"
        )
        assert not publish.done()

        engine.settings_handle.override(chat_thinking_expanded=True)
    finally:
        release.set()
    await publish

    assert switch.applied is True
    assert _model(engine) == OTHER_MODEL
    assert engine.settings.model_profile == OTHER_MODEL
    assert engine.settings.chat_thinking_expanded is True


async def test_a_live_choice_keeps_the_session_model(
    agent_engine: AgentEngineFactory, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    started = await _start(agent_engine, monkeypatch, tmp_path)
    engine = started.engine
    await _switch(engine, model=OTHER_MODEL)

    engine.settings_handle.override(chat_thinking_expanded=True)

    assert engine.settings.model_profile == OTHER_MODEL
    assert engine.settings.chat_thinking_expanded is True
    assert _session_model(engine) == OTHER_MODEL
    # And the next rebuild is still made on it.
    await _publish(engine, SettingsReload())
    assert _model(engine) == OTHER_MODEL


# ---------------------------------------------------------------------------
# Item 28: what a close resolves the next start's agent to (§3.1 End)
# ---------------------------------------------------------------------------


async def test_a_model_pin_outranks_the_session_s_own_model(
    agent_engine: AgentEngineFactory, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A caller that owns the model (``SetModelProfile``: ACP, ``--model``) pins it; a shortcut's model gives way."""
    started = await _start(agent_engine, monkeypatch, tmp_path)
    engine = started.engine
    assert (await _switch(engine, model=OTHER_MODEL)).applied is True

    await _publish(engine, SetModelProfile(profile_id=SAVED_MODEL))

    assert _model(engine) == SAVED_MODEL
    assert engine.settings.model_profile == SAVED_MODEL
    assert _session_model(engine) == ""


async def test_a_teardown_that_fails_before_releasing_the_runtime_leaves_the_session_agent_and_model(
    agent_engine: AgentEngineFactory, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    started = await _start(agent_engine, monkeypatch, tmp_path)
    engine = started.engine
    await _switch(engine, agent="QA", model=OTHER_MODEL)
    writer = engine.writer
    real_save = writer.save_current_session
    failed: list[bool] = []

    async def save_fails_once(*args: Any, **kwargs: Any) -> Any:
        if not failed:
            failed.append(True)
            raise RuntimeError("the closing save failed")
        return await real_save(*args, **kwargs)

    # "Teardown raising before release" is made with the session's closing save, which a close runs before it
    # releases the runtime.
    monkeypatch.setattr(writer, "save_current_session", create_autospec(real_save, side_effect=save_fails_once))

    # Default delivery: the bus logs and swallows the handler's failure, as in the TUI.
    await engine.event_bus.publish(SessionNew())

    # The closing save is part of the teardown, before the runtime is released; it raised, so the build is still
    # held, and the session's agent is still the one that build runs.
    assert failed == [True]
    assert engine.current.loaded is not None
    assert _agent(engine) == "QA"
    # The model too: what the settings report is what the build still runs.
    assert _model(engine) == OTHER_MODEL
    assert _session_model(engine) == OTHER_MODEL
    assert engine.settings.model_profile == OTHER_MODEL


async def test_a_failed_startup_restore_drops_the_session_model_even_when_its_teardown_fails(
    agent_engine: AgentEngineFactory, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    started = await _start(agent_engine, monkeypatch, tmp_path)
    engine = started.engine
    await _switch(engine, model=OTHER_MODEL)
    writer = engine.writer
    real_save = writer.save_current_session
    failed: list[bool] = []

    async def save_fails_once(*args: Any, **kwargs: Any) -> Any:
        if not failed:
            failed.append(True)
            raise RuntimeError("the closing save failed")
        return await real_save(*args, **kwargs)

    monkeypatch.setattr(writer, "save_current_session", create_autospec(real_save, side_effect=save_fails_once))

    with pytest.raises(RuntimeError, match="the closing save failed"):
        await engine.reset_after_failed_startup_restore()

    # The fallback start that follows is a new session, so it starts on the default model.
    assert failed == [True]
    assert _session_model(engine) == ""
    assert engine.settings.model_profile == DEFAULT_MODEL


async def test_a_default_agent_made_sub_agent_only_is_skipped_at_a_new_session(
    agent_engine: AgentEngineFactory, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    started = await _start(agent_engine, monkeypatch, tmp_path, agent=_DOCS)
    engine = started.engine
    assert engine.session.new_session_agent == "Docs"

    started.agents.register(replace(_DOCS, sub_agent_only=True))
    await _publish(engine, SessionNew())

    # Docs is no longer main-eligible: the launch chain's next step, ``Settings.default_agent`` (Code), starts it.
    _assert_defaults(engine, agent="Code")


# ---------------------------------------------------------------------------
# Item 29: ``#`` on the agent in force, and a restored agent that no longer resolves
# ---------------------------------------------------------------------------


async def test_a_hash_pick_of_the_agent_already_in_force_makes_it_the_default(
    agent_engine: AgentEngineFactory, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    started = await _start(agent_engine, monkeypatch, tmp_path)
    engine = started.engine
    await _switch(engine, agent="QA")
    assert engine.session.new_session_agent == "Code"

    await _publish(engine, AgentProfileSwitch(profile_name="QA"))

    assert _agent(engine) == "QA"
    assert engine.session.new_session_agent == "QA"
    await _publish(engine, SessionNew())
    _assert_defaults(engine, agent="QA")


async def test_restoring_a_session_whose_agent_no_longer_resolves_starts_it_on_the_default_agent(
    agent_engine: AgentEngineFactory, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    started = await _start(agent_engine, monkeypatch, tmp_path)
    engine = started.engine
    await _seed_restorable_session(started.store, agent_profile="Retired")
    # The agent in use before the close is the session's own QA; the default is Code.
    await _switch(engine, agent="QA")

    await _restore(engine, "restore_me")

    assert engine.session_id == "restore_me"
    assert _agent(engine) == "Code"


# ---------------------------------------------------------------------------
# Item 31: workflows run on the default model, never on a session's own
# ---------------------------------------------------------------------------

_AGENT_WORKFLOW = (
    b"from chrys.workflows import WorkflowBuilder\n"
    b"wf = WorkflowBuilder('review')\n"
    b"a = wf.agent('default', profile='Headless')\n"
    b"wf.start(a)\nwf.output(a)\nworkflow = wf.build()\n"
)


@with_wait_deadline(ENGINE_TEST_WAIT_TIMEOUT)
async def test_a_workflow_started_after_a_shortcut_model_switch_runs_on_the_default_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A headless host is the harness that runs workflows; the engine it holds handles the switch as the TUI's does.
    # Its default model is the one it started with (§5: workflows read their startup snapshot and the files).
    patch_runtime(monkeypatch, [MockChatClient(responses=[]), MockChatClient(responses=[MockResponse(text="done")])])
    project = make_project(tmp_path)
    write_workflow(project, "review", _AGENT_WORKFLOW)
    host = make_host(tmp_path, project=project)
    registry = host.engine.model_registry
    assert registry is not None
    registry.register(ModelProfile(id="second-model", name="second-model", provider="mock", model_id="second-model"))
    try:
        await host.start()
        switch = SessionOnlySwitch(model_profile="second-model")
        await host.event_bus.publish(switch, raise_handler_errors=True)
        assert switch.applied is True
        assert host.engine.current.manifest.active_profile is not None
        assert host.engine.current.manifest.active_profile.id == "second-model"

        await confirm(host, "review")
        _result, events = await run(host, "review")

        (run_started,) = of_type(events, WorkflowRunStarted)
        assert run_started.model is not None
        assert run_started.model.profile_id == "mock-profile"
        assert {node["node_id"]: node["model_profile_id"] for node in run_started.resolved_nodes} == {
            "default": "mock-profile"
        }
        # The chat session keeps its own model meanwhile.
        assert host.engine.current.manifest.active_profile is not None
        assert host.engine.current.manifest.active_profile.id == "second-model"
    finally:
        await host.shutdown()
