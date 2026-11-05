# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Engine runtime controls: profile switch, workspace change, approval mode, rebuild-permit gating, and the live snapshot."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, create_autospec

import pytest

import chrys.orchestration.engine.engine as engine_module
from chrys.foundation.config.settings import (
    Settings,
)
from chrys.foundation.config.settings_store import LoadedSettings, load_settings
from chrys.foundation.config.spec import Source
from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import (
    AgentProfileSwitch,
    ApprovalModeUpdated,
    Error,
    ModelProfileSwitched,
    ProfileSwitched,
    SetApprovalMode,
    SetModelProfile,
    SettingsReload,
    SettingsReloaded,
    WorkspaceChange,
    WorkspaceUpdated,
)
from chrys.foundation.i18n import DisplayPath
from chrys.foundation.models.workspace import Workspace
from chrys.orchestration.engine.assembly import assemble_agent_engine
from chrys.orchestration.engine.state import lifecycle_permits
from chrys.orchestration.sub_agents.tools import SubAgentTools
from chrys.service.agent_middleware.control.approval import ApprovalMiddleware
from chrys.service.approval.policy import ApprovalMode, ApprovalPolicy
from chrys.service.profiles.agents.schema import (
    AgentProfile,
    ApprovalConfig,
)
from chrys.service.profiles.models.schema import ModelProfile
from tests.orchestration.engine._recovery_helpers import (
    _ApprovalTarget,
    _profile,
    _registry,
    stub_engine_start,
)
from tests.support.event_capture import assert_display_message, collect_events
from tests.support.loaded_agents import install_loaded_agent


async def test_profile_switch_reports_missing_registry() -> None:
    events: list[Error] = []
    bus = EventBus()
    await bus.subscribe(Error, lambda event: collect_events(events, event))
    engine = assemble_agent_engine(bus, settings=Settings())
    engine.session.session_id = "sid"

    await engine._on_profile_switch(AgentProfileSwitch(profile_name="Explore"))

    assert [event.code for event in events] == ["no_registry"]
    assert events[0].message == "No profile registry configured — cannot switch profiles"
    assert_display_message(events[0], "controls.no_registry")
    assert events[0].session_id == "sid"


async def test_profile_switch_reports_missing_profile() -> None:
    events: list[Error] = []
    bus = EventBus()
    await bus.subscribe(Error, lambda event: collect_events(events, event))
    engine = assemble_agent_engine(bus, settings=Settings(), agent_registry=_registry(_profile()))
    engine.session.session_id = "sid"

    await engine._on_profile_switch(AgentProfileSwitch(profile_name="Explore"))

    assert [event.code for event in events] == ["profile_not_found"]
    assert events[0].message == "Profile 'Explore' not found"
    assert_display_message(events[0], "controls.profile_not_found", {"profile_name": "Explore"})


async def test_profile_switch_retries_start_when_no_executor(monkeypatch: pytest.MonkeyPatch) -> None:
    explore = _profile("Explore", "Explore Agent")
    events: list[ProfileSwitched] = []
    bus = EventBus()
    await bus.subscribe(ProfileSwitched, lambda event: collect_events(events, event))
    engine = assemble_agent_engine(bus, settings=Settings(), agent_registry=_registry(explore))
    engine.session.session_id = "sid"
    calls: list[tuple[AgentProfile, str]] = []

    async def fake_start(
        profile: AgentProfile,
        *,
        operation: str = "startup",
        staged_loaded: LoadedSettings | None = None,
        session: Mapping[str, Any] | None = None,
        workspace: Workspace | None = None,
    ) -> None:
        calls.append((profile, operation))
        engine.session.agent_profile = profile
        executor = MagicMock()
        executor.backend.history_state = {"messages": []}
        install_loaded_agent(engine, bindings=executor)

    monkeypatch.setattr(engine.lifecycle, "start", fake_start)

    await engine._on_profile_switch(AgentProfileSwitch(profile_name="Explore"))

    assert calls == [(explore, "switch")]
    assert [(event.from_profile, event.to_profile, event.session_id) for event in events] == [
        ("Explore", "Explore", "sid")
    ]


async def test_profile_switch_waits_for_active_run_before_soft_restart(monkeypatch: pytest.MonkeyPatch) -> None:
    code = _profile("Code", "Code Agent")
    explore = _profile("Explore", "Explore Agent")
    engine = assemble_agent_engine(EventBus(), settings=Settings(), agent_registry=_registry(code, explore))
    engine.session.agent_profile = code
    install_loaded_agent(engine, bindings=object())  # type: ignore[assignment]
    release_run = asyncio.Event()
    calls: list[tuple[AgentProfile, str]] = []

    async def active_run() -> None:
        await release_run.wait()

    async def fake_soft_restart(profile: AgentProfile, **kwargs: Any) -> None:
        calls.append((profile, kwargs["operation"]))

    engine.turns.turn_state.lease.run_task = asyncio.create_task(active_run())
    monkeypatch.setattr(engine.lifecycle, "reload", fake_soft_restart)

    switch_task = asyncio.create_task(engine._on_profile_switch(AgentProfileSwitch(profile_name="Explore")))
    try:
        await asyncio.sleep(0)
        assert calls == []
    finally:
        release_run.set()
        await asyncio.gather(switch_task, engine.turns.turn_state.lease.run_task, return_exceptions=True)

    assert calls == [(explore, "switch")]


@pytest.mark.parametrize("mutation", ["model_switch", "settings_reload", "workspace_change"])
async def test_runtime_mutation_waits_for_startup_load(
    mutation: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A mutation parks behind an in-flight startup load: nothing is rebuilt or mutated until it ends."""
    active = _profile("Code", "Code Agent")
    engine = assemble_agent_engine(
        EventBus(),
        settings=Settings(model_profile="old-model", default_approval_mode="manual"),
        agent_registry=_registry(active),
    )
    engine.session.agent_profile = active
    engine.permits.begin_agent_load()
    old_settings = engine.settings
    monkeypatch.setattr(
        "chrys.orchestration.engine.state.controls.load_settings",
        lambda **kwargs: LoadedSettings(settings=Settings(default_approval_mode="auto"), provenance={}),
    )
    start_calls = stub_engine_start(monkeypatch, engine)
    if mutation == "model_switch":
        pending = engine._on_set_model_profile(SetModelProfile(profile_id="new-model"))
    elif mutation == "settings_reload":
        pending = engine._on_settings_reload(SettingsReload())
    else:
        pending = engine._on_workspace_change(WorkspaceChange(primary_cwd=str(tmp_path)))

    task = asyncio.create_task(pending)
    try:
        await asyncio.sleep(0)
        assert start_calls == []
        assert engine.settings is old_settings
        assert engine.workspace is None
    finally:
        engine.permits.finish_agent_load()
        await asyncio.gather(task, return_exceptions=True)


async def test_workspace_change_starts_from_failed_build_without_executor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = _profile()
    events: list[WorkspaceUpdated] = []
    bus = EventBus()
    await bus.subscribe(WorkspaceUpdated, lambda event: collect_events(events, event))
    engine = assemble_agent_engine(bus, settings=Settings())
    engine.session.session_id = "sid"
    engine.session.agent_profile = profile
    calls: list[tuple[AgentProfile, str]] = []

    async def fake_start(
        start_profile: AgentProfile,
        *,
        operation: str = "startup",
        staged_loaded: LoadedSettings | None = None,
        session: Mapping[str, Any] | None = None,
        workspace: Workspace | None = None,
    ) -> None:
        calls.append((start_profile, operation))
        executor = MagicMock()
        executor.backend.history_state = {"messages": []}
        install_loaded_agent(engine, bindings=executor)
        # A successful start commits the staged workspace; the double honors
        # the same contract now that nothing pre-assigns it.
        if workspace is not None:
            engine.session.workspace = workspace

    monkeypatch.setattr(engine.lifecycle, "start", fake_start)

    await engine._on_workspace_change(WorkspaceChange(primary_cwd=str(tmp_path)))

    assert engine.workspace is not None
    assert engine.workspace.primary_cwd == str(tmp_path)
    assert calls == [(profile, "workspace_change")]
    assert [(event.primary_cwd, event.session_id) for event in events] == [(str(tmp_path), "sid")]


async def test_failed_workspace_change_without_executor_keeps_the_old_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The new root rides the build as staged input: when the no-executor
    rebuild fails, both the live workspace and the live settings must still
    describe the old root."""
    profile = _profile()
    old_root = tmp_path / "old"
    old_root.mkdir()
    new_root = tmp_path / "new"
    new_root.mkdir()
    replacement_settings = Settings(default_approval_mode="auto")
    engine = assemble_agent_engine(EventBus(), settings=Settings())
    engine.session.session_id = "sid"
    engine.session.agent_profile = profile
    engine.session.workspace = Workspace.from_cwd(str(old_root))
    old_settings = engine.settings

    async def failing_build(_profile: AgentProfile, _staged: Any, **_kwargs: object) -> None:
        raise RuntimeError("build failed")

    monkeypatch.setattr(
        "chrys.orchestration.engine.state.controls.load_settings",
        lambda **kwargs: LoadedSettings(settings=replacement_settings, provenance={}),
    )
    monkeypatch.setattr(engine.loader, "build", failing_build)

    with pytest.raises(RuntimeError, match="build failed"):
        await engine._on_workspace_change(WorkspaceChange(primary_cwd=str(new_root)))

    assert engine.workspace is not None
    assert engine.workspace.primary_cwd == Workspace.from_cwd(str(old_root)).primary_cwd
    assert engine.settings is old_settings


async def test_failed_workspace_change_load_publishes_a_terminal_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A load failure aborts before the rebuild publishes anything, and the
    bus swallows handler exceptions: without an explicit Error a caller
    awaiting the change (ACP's 60s wait, the TUI's loading state) hangs."""
    profile = _profile()
    new_root = tmp_path / "new"
    new_root.mkdir()
    errors: list[Error] = []
    bus = EventBus()
    await bus.subscribe(Error, lambda event: collect_events(errors, event))
    engine = assemble_agent_engine(bus, settings=Settings())
    engine.session.session_id = "sid"
    engine.session.agent_profile = profile
    old_settings = engine.settings

    def failing_load(**kwargs: Any) -> LoadedSettings:
        raise RuntimeError("unreadable config")

    monkeypatch.setattr("chrys.orchestration.engine.state.controls.load_settings", failing_load)

    with pytest.raises(RuntimeError, match="unreadable config"):
        await engine._on_workspace_change(WorkspaceChange(primary_cwd=str(new_root)))

    assert engine.settings is old_settings
    assert [error.code for error in errors] == ["workspace_change_failed"]
    assert errors[0].session_id == "sid"


async def test_workspace_change_soft_restarts_live_executor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = _profile()
    engine = assemble_agent_engine(EventBus(), settings=Settings())
    engine.session.agent_profile = profile
    install_loaded_agent(engine, bindings=object())  # type: ignore[assignment]
    calls: list[tuple[AgentProfile, str, str]] = []

    async def fake_soft_restart(start_profile: AgentProfile, **kwargs: Any) -> None:
        workspace = kwargs["workspace"]
        calls.append((start_profile, workspace.primary_cwd, kwargs["operation"]))

    monkeypatch.setattr(engine.lifecycle, "reload", fake_soft_restart)

    await engine._on_workspace_change(WorkspaceChange(primary_cwd=str(tmp_path)))

    assert calls == [(profile, str(tmp_path), "workspace_change")]


async def test_workspace_change_derives_settings_from_the_new_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A workspace change is a settings reload in disguise: the new root's
    project layer rides the same rebuild that installs the new workspace."""
    profile = _profile()
    old_root = tmp_path / "old"
    old_root.mkdir()
    new_root = tmp_path / "new"
    new_root.mkdir()
    replacement_settings = Settings(default_approval_mode="auto")
    engine = assemble_agent_engine(EventBus(), settings=Settings())
    engine.session.agent_profile = profile
    engine.session.workspace = Workspace.from_cwd(str(old_root))
    load_kwargs: dict[str, Any] = {}

    def fake_load(**kwargs: Any) -> LoadedSettings:
        load_kwargs.update(kwargs)
        return LoadedSettings(settings=replacement_settings, provenance={})

    monkeypatch.setattr("chrys.orchestration.engine.state.controls.load_settings", fake_load)
    stub_engine_start(monkeypatch, engine)

    await engine._on_workspace_change(WorkspaceChange(primary_cwd=str(new_root)))

    assert load_kwargs["project_root"] == Path(Workspace.from_cwd(str(new_root)).primary_cwd)
    assert engine.settings is replacement_settings


async def test_set_approval_mode_updates_runtime_targets_and_persists_bypass_as_auto(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[ApprovalModeUpdated] = []
    bus = EventBus()
    await bus.subscribe(ApprovalModeUpdated, lambda event: collect_events(events, event))
    settings = Settings(default_approval_mode="manual")
    engine = assemble_agent_engine(bus, settings=settings)
    engine.session.session_id = "sid"
    executor = _ApprovalTarget()
    sub_agents = SubAgentTools(event_bus=bus, approval_mode=ApprovalMode.MANUAL)
    live_approval = ApprovalMiddleware(
        approval_policy=ApprovalPolicy(ApprovalConfig(default="require")),
        event_bus=bus,
        approval_mode=ApprovalMode.MANUAL,
    )
    sub_agents._live_approvals.append(live_approval)
    install_loaded_agent(engine, bindings=executor)  # type: ignore[assignment]
    install_loaded_agent(engine, sub_agent_tools=sub_agents)
    # Live workflow nodes are the fourth runtime target; the coordinator forwards to them.
    workflow_nodes = create_autospec(engine.workflows.set_approval_mode)
    monkeypatch.setattr(engine.workflows, "set_approval_mode", workflow_nodes)
    persisted: list[str] = []

    def persist_mode(mode: str) -> None:
        persisted.append(mode)

    monkeypatch.setattr(engine_module, "persist_approval_mode", persist_mode)

    await engine._on_set_approval_mode(SetApprovalMode(mode="bypass"))

    assert engine.session.approval_mode is ApprovalMode.BYPASS
    assert executor.modes == [ApprovalMode.BYPASS]
    assert sub_agents._approval_mode is ApprovalMode.BYPASS
    assert live_approval.approval_mode is ApprovalMode.BYPASS
    workflow_nodes.assert_called_once_with(ApprovalMode.BYPASS)
    assert persisted == ["bypass"]
    assert engine.settings.default_approval_mode == "auto"
    assert [(event.mode, event.session_id) for event in events] == [("bypass", "sid")]


async def test_setting_the_approval_mode_moves_its_provenance_with_the_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A live write is still a write: the layer that won has to change too."""
    bus = EventBus()
    loaded = LoadedSettings(settings=Settings(default_approval_mode="manual"), provenance={})
    engine = assemble_agent_engine(bus, loaded_settings=loaded)
    engine.session.session_id = "sid"
    monkeypatch.setattr(engine_module, "persist_approval_mode", lambda _mode: None)

    await engine._on_set_approval_mode(SetApprovalMode(mode="auto"))

    assert engine.settings.default_approval_mode == "auto"
    assert engine.loaded_settings.settings is engine.settings
    assert engine.loaded_settings.source_for("approval.default_mode").layer is Source.RUNTIME


async def test_setting_the_approval_mode_breaks_the_seal_it_no_longer_describes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``approval.default_mode`` is DANGEROUS, so a bad env value seals it.

    The seal means "we refused every layer and fell back to the built-in
    default". Once the user picks a mode at runtime that sentence is false,
    and leaving the seal on reproduces the contradiction the freeze fix
    removed: sealed, yet holding a value nobody defaulted to.
    """
    monkeypatch.setenv("CHRYS_DEFAULT_APPROVAL_MODE", "garbage")
    loaded = load_settings()
    assert loaded.settings.default_approval_mode == "manual"
    assert "approval.default_mode" in loaded.sealed_keys

    bus = EventBus()
    engine = assemble_agent_engine(bus, loaded_settings=loaded)
    engine.session.session_id = "sid"
    monkeypatch.setattr(engine_module, "persist_approval_mode", lambda _mode: None)

    await engine._on_set_approval_mode(SetApprovalMode(mode="auto"))

    assert engine.settings.default_approval_mode == "auto"
    assert "approval.default_mode" not in engine.loaded_settings.sealed_keys


async def test_set_approval_mode_can_skip_global_persistence(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[ApprovalModeUpdated] = []
    bus = EventBus()
    await bus.subscribe(ApprovalModeUpdated, lambda event: collect_events(events, event))
    settings = Settings(default_approval_mode="manual")
    engine = assemble_agent_engine(bus, settings=settings)
    engine.session.session_id = "sid"
    executor = _ApprovalTarget()
    sub_agents = _ApprovalTarget()
    install_loaded_agent(engine, bindings=executor)  # type: ignore[assignment]
    install_loaded_agent(engine, sub_agent_tools=sub_agents)  # type: ignore[assignment]
    persisted: list[str] = []

    def persist_mode(mode: str) -> None:
        persisted.append(mode)

    monkeypatch.setattr(engine_module, "persist_approval_mode", persist_mode)

    await engine._on_set_approval_mode(SetApprovalMode(mode="bypass", persist=False))

    assert engine.session.approval_mode is ApprovalMode.BYPASS
    assert executor.modes == [ApprovalMode.BYPASS]
    assert sub_agents.modes == [ApprovalMode.BYPASS]
    assert persisted == []
    assert settings.default_approval_mode == "manual"
    assert [(event.mode, event.session_id) for event in events] == [("bypass", "sid")]


async def test_set_approval_mode_ignores_invalid_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[ApprovalModeUpdated] = []
    bus = EventBus()
    await bus.subscribe(ApprovalModeUpdated, lambda event: collect_events(events, event))
    engine = assemble_agent_engine(bus, settings=Settings())
    persisted: list[str] = []

    def persist_mode(mode: str) -> None:
        persisted.append(mode)

    monkeypatch.setattr(engine_module, "persist_approval_mode", persist_mode)

    await engine._on_set_approval_mode(SetApprovalMode(mode="unknown"))

    assert engine.session.approval_mode is ApprovalMode.MANUAL
    assert persisted == []
    assert events == []


def test_current_profile_snapshot_reflects_live_runtime() -> None:
    engine = assemble_agent_engine(EventBus(), settings=Settings())
    engine.session.session_id = "sid"
    engine.session.agent_profile = _profile(name="Code", display_name="Coder")
    install_loaded_agent(
        engine,
        active_profile=ModelProfile(
            id="model-1", name="GPT", provider="openai", model_id="gpt-5", max_context_tokens=200000
        ),
    )
    install_loaded_agent(engine, tool_names=["shell", "read_file"])
    install_loaded_agent(engine, skill_names=["search"])
    install_loaded_agent(engine, memory_files=["AGENTS.md"])
    install_loaded_agent(engine, sub_agent_tools=MagicMock())
    engine.current.loaded.sub_agent_tools.tool_names.return_value = ["explore"]
    executor = MagicMock()
    executor.backend.history_state = {"messages": [1, 2, 3]}
    install_loaded_agent(engine, bindings=executor)

    snapshot = engine.current_profile_snapshot()

    # No-op switch: from and to are identical and reflect the live agent.
    assert snapshot.from_profile == "Code"
    assert snapshot.to_profile == "Code"
    assert snapshot.from_display_name == "Coder"
    assert snapshot.to_display_name == "Coder"
    assert snapshot.session_id == "sid"
    assert snapshot.message_count == 3
    assert snapshot.model_profile_id == "model-1"
    assert snapshot.max_context_tokens == 200000
    assert snapshot.tool_names == ["shell", "read_file"]
    assert snapshot.skill_names == ["search"]
    assert snapshot.sub_agent_tool_names == ["explore"]
    assert snapshot.memory_files == ["AGENTS.md"]
    # Mutating the snapshot's lists must not bleed back into engine state.
    snapshot.tool_names.append("rm")
    assert list(engine.current.manifest.tool_names) == ["shell", "read_file"]


async def test_runtime_control_denials_publish_captured_session_id(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    bus = EventBus()
    errors: list[Error] = []
    await bus.subscribe(Error, lambda event: collect_events(errors, event))
    engine = assemble_agent_engine(bus, settings=Settings(), agent_registry=_registry(_profile(), _profile("Explore")))
    engine.session.session_id = "old-session"
    engine.session.agent_profile = _profile()
    install_loaded_agent(engine, bindings=object())  # type: ignore[assignment]
    denied = lifecycle_permits.RebuildPermitDenied(
        reason="session_changed",
        code="runtime_mutation_session_changed",
        message="session moved",
    )

    async def deny_with_new_live_session(
        _token: lifecycle_permits.RebuildControlToken,
    ) -> lifecycle_permits.RebuildPermitDenied:
        engine.session.session_id = "new-session"
        return denied

    monkeypatch.setattr(engine.permits, "acquire_rebuild_permit", deny_with_new_live_session)

    engine.session.session_id = "old-session"
    await engine._on_profile_switch(AgentProfileSwitch(profile_name="Explore"))
    engine.session.session_id = "old-session"
    await engine._on_set_model_profile(SetModelProfile(profile_id="new-model"))
    engine.session.session_id = "old-session"
    await engine._on_settings_reload(SettingsReload())
    engine.session.session_id = "old-session"
    await engine._on_workspace_change(WorkspaceChange(primary_cwd=str(tmp_path)))

    assert [event.code for event in errors] == [
        "runtime_mutation_session_changed",
        "runtime_mutation_session_changed",
        "runtime_mutation_session_changed",
        "runtime_mutation_session_changed",
    ]
    assert {event.session_id for event in errors} == {"old-session"}


async def test_workspace_change_to_a_missing_directory_is_refused_before_any_rebuild(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    bus = EventBus()
    errors: list[Error] = []
    await bus.subscribe(Error, lambda event: collect_events(errors, event))
    engine = assemble_agent_engine(bus, settings=Settings(), agent_registry=_registry(_profile()))
    engine.session.session_id = "sid"
    engine.session.agent_profile = _profile()
    engine.session.workspace = Workspace.from_cwd(str(tmp_path))
    install_loaded_agent(engine, bindings=object())  # type: ignore[assignment]
    acquire_calls: list[lifecycle_permits.RebuildControlToken] = []

    async def record_acquire(
        token: lifecycle_permits.RebuildControlToken,
    ) -> lifecycle_permits.RebuildPermitDenied:
        acquire_calls.append(token)
        return lifecycle_permits.RebuildPermitDenied(reason="superseded", code="unexpected", message="unexpected")

    monkeypatch.setattr(engine.permits, "acquire_rebuild_permit", record_acquire)
    missing = tmp_path / "missing"

    await engine._on_workspace_change(WorkspaceChange(primary_cwd=str(missing)))

    assert [(event.code, event.session_id) for event in errors] == [("workspace_change_failed", "sid")]
    assert errors[0].message == f"Working directory does not exist: {missing}"
    assert_display_message(errors[0], "controls.workspace_missing", {"path": DisplayPath(str(missing))})
    assert acquire_calls == []
    assert engine.session.workspace.primary_cwd == str(tmp_path)


async def test_already_satisfied_denials_publish_typed_success(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    current = str(tmp_path)
    bus = EventBus()
    profile_events: list[ProfileSwitched] = []
    model_events: list[ModelProfileSwitched] = []
    workspace_events: list[WorkspaceUpdated] = []
    await bus.subscribe(ProfileSwitched, lambda event: collect_events(profile_events, event))
    await bus.subscribe(ModelProfileSwitched, lambda event: collect_events(model_events, event))
    await bus.subscribe(WorkspaceUpdated, lambda event: collect_events(workspace_events, event))

    profile = _profile()
    engine = assemble_agent_engine(bus, settings=Settings(), agent_registry=_registry(profile))
    engine.session.session_id = "sid"
    engine.session.agent_profile = profile
    executor = MagicMock()
    executor.backend.history_state = {"messages": []}
    install_loaded_agent(engine, bindings=executor)
    install_loaded_agent(
        engine, active_profile=ModelProfile(id="model-1", name="Model", provider="openai", model_id="gpt-5")
    )
    engine.session.workspace = Workspace.from_cwd(current)
    denied = lifecycle_permits.RebuildPermitDenied(
        reason="superseded",
        code="runtime_mutation_superseded",
        message="newer runtime",
    )
    acquire_calls = 0

    async def deny_after_boundary(
        _token: lifecycle_permits.RebuildControlToken,
    ) -> lifecycle_permits.RebuildPermitDenied:
        nonlocal acquire_calls
        acquire_calls += 1
        return denied

    monkeypatch.setattr(engine.permits, "acquire_rebuild_permit", deny_after_boundary)

    await engine._on_profile_switch(AgentProfileSwitch(profile_name="Code"))
    await engine._on_set_model_profile(SetModelProfile(profile_id="model-1"))
    await engine._on_workspace_change(WorkspaceChange(primary_cwd=current))

    assert acquire_calls == 3
    assert [(event.from_profile, event.to_profile, event.session_id) for event in profile_events] == [
        ("Code", "Code", "sid")
    ]
    assert [(event.model_profile_id, event.session_id) for event in model_events] == [("model-1", "sid")]
    assert engine.session.model_profile_pinned is True
    assert engine.settings.model_profile == "model-1"
    assert engine.settings.model_profile_override == "model-1"
    assert engine.settings.model_profile_override_sub_agents is False
    assert [(event.primary_cwd, event.session_id) for event in workspace_events] == [(current, "sid")]


@pytest.mark.parametrize(
    ("reason", "code"),
    [
        ("session_changed", "runtime_mutation_session_changed"),
        ("shutdown", "runtime_mutation_shutdown"),
    ],
)
async def test_terminal_denials_are_not_masked_by_live_satisfied_state(
    monkeypatch: pytest.MonkeyPatch,
    reason: lifecycle_permits.RebuildPermitDeniedReason,
    code: str,
    tmp_path: Path,
) -> None:
    current = str(tmp_path)
    bus = EventBus()
    errors: list[Error] = []
    profile_events: list[ProfileSwitched] = []
    model_events: list[ModelProfileSwitched] = []
    workspace_events: list[WorkspaceUpdated] = []
    await bus.subscribe(Error, lambda event: collect_events(errors, event))
    await bus.subscribe(ProfileSwitched, lambda event: collect_events(profile_events, event))
    await bus.subscribe(ModelProfileSwitched, lambda event: collect_events(model_events, event))
    await bus.subscribe(WorkspaceUpdated, lambda event: collect_events(workspace_events, event))

    profile = _profile()
    engine = assemble_agent_engine(bus, settings=Settings(), agent_registry=_registry(profile))
    engine.session.session_id = "old-session"
    engine.session.agent_profile = profile
    executor = MagicMock()
    executor.backend.history_state = {"messages": []}
    install_loaded_agent(engine, bindings=executor)
    install_loaded_agent(
        engine, active_profile=ModelProfile(id="model-1", name="Model", provider="openai", model_id="gpt-5")
    )
    engine.session.workspace = Workspace.from_cwd(current)
    denied = lifecycle_permits.RebuildPermitDenied(reason=reason, code=code, message=reason)

    async def deny_after_boundary(
        _token: lifecycle_permits.RebuildControlToken,
    ) -> lifecycle_permits.RebuildPermitDenied:
        if reason == "session_changed":
            engine.session.session_id = "new-session"
        return denied

    monkeypatch.setattr(engine.permits, "acquire_rebuild_permit", deny_after_boundary)

    engine.session.session_id = "old-session"
    await engine._on_profile_switch(AgentProfileSwitch(profile_name="Code"))
    engine.session.session_id = "old-session"
    await engine._on_set_model_profile(SetModelProfile(profile_id="model-1"))
    engine.session.session_id = "old-session"
    await engine._on_workspace_change(WorkspaceChange(primary_cwd=current))

    assert [(event.code, event.session_id) for event in errors] == [
        (code, "old-session"),
        (code, "old-session"),
        (code, "old-session"),
    ]
    assert profile_events == []
    assert model_events == []
    assert workspace_events == []


async def test_concurrent_profile_switch_waiters_supersede_after_first_rebuild(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    code = _profile("Code")
    explore = _profile("Explore")
    docs = _profile("docs")
    bus = EventBus()
    errors: list[Error] = []
    await bus.subscribe(Error, lambda event: collect_events(errors, event))
    engine = assemble_agent_engine(bus, settings=Settings(), agent_registry=_registry(code, explore, docs))
    engine.session.session_id = "sid"
    engine.session.agent_profile = code
    install_loaded_agent(engine, bindings=object())  # type: ignore[assignment]
    restart_entered = asyncio.Event()
    release_restart = asyncio.Event()
    calls: list[str] = []

    async def fake_soft_restart_with_permit(
        _permit: lifecycle_permits.RebuildPermit,
        profile: AgentProfile,
        workspace: Workspace | None = None,
        *,
        operation: str = "switch",
        staged_loaded: LoadedSettings | None = None,
        session: Mapping[str, Any] | None = None,
    ) -> None:
        _ = workspace, operation
        calls.append(profile.name)
        restart_entered.set()
        await release_restart.wait()
        engine.session.agent_profile = profile
        engine.permits.advance_build_generation()

    monkeypatch.setattr(engine.lifecycle, "reload_with_rebuild_permit", fake_soft_restart_with_permit)

    first = asyncio.create_task(engine._on_profile_switch(AgentProfileSwitch(profile_name="Explore")))
    await asyncio.wait_for(restart_entered.wait(), timeout=5.0)
    second = asyncio.create_task(engine._on_profile_switch(AgentProfileSwitch(profile_name="docs")))
    await asyncio.sleep(0)
    release_restart.set()
    await asyncio.wait_for(asyncio.gather(first, second), timeout=5.0)

    assert calls == ["Explore"]
    assert [(event.code, event.session_id) for event in errors] == [("runtime_mutation_superseded", "sid")]


@pytest.mark.parametrize("mutation", ["workspace_change", "model_switch"])
async def test_runtime_mutation_without_agent_publishes_not_ready_error(mutation: str, tmp_path: Path) -> None:
    bus = EventBus()
    errors: list[Error] = []
    await bus.subscribe(Error, lambda event: collect_events(errors, event))
    engine = assemble_agent_engine(bus, settings=Settings())
    engine.session.session_id = "sid"

    if mutation == "workspace_change":
        await engine._on_workspace_change(WorkspaceChange(primary_cwd=str(tmp_path)))
        message, key = "No active agent — cannot change workspace", "controls.workspace_switch_not_ready"
    else:
        await engine._on_set_model_profile(SetModelProfile(profile_id="model-1"))
        message, key = "No active agent — cannot switch model", "controls.model_switch_not_ready"

    assert [(event.code, event.message, event.session_id) for event in errors] == [
        ("runtime_mutation_not_ready", message, "sid")
    ]
    assert_display_message(errors[0], key)


@pytest.mark.parametrize("mutation", ["model_switch", "settings_reload"])
async def test_runtime_mutation_success_publishes_before_rebuild_permit_release(
    mutation: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bus = EventBus()
    release_seen = False
    event_release_states: list[bool] = []
    published = ModelProfileSwitched if mutation == "model_switch" else SettingsReloaded
    await bus.subscribe(published, lambda _event: collect_events(event_release_states, release_seen))
    profile = _profile()
    engine = assemble_agent_engine(bus, settings=Settings(), agent_registry=_registry(profile))
    engine.session.session_id = "sid"
    engine.session.agent_profile = profile
    install_loaded_agent(engine, bindings=MagicMock())
    install_loaded_agent(
        engine, active_profile=ModelProfile(id="old-model", name="Old", provider="openai", model_id="gpt-4")
    )

    async def fake_soft_restart(
        _profile: AgentProfile,
        workspace: Workspace | None = None,
        *,
        operation: str = "switch",
        staged_loaded: LoadedSettings | None = None,
        session: Mapping[str, Any] | None = None,
    ) -> None:
        _ = workspace, operation
        install_loaded_agent(
            engine, active_profile=ModelProfile(id="new-model", name="New", provider="openai", model_id="gpt-5")
        )

    original_release = engine.permits.release_rebuild_permit

    def release_with_marker(permit: lifecycle_permits.RebuildPermit) -> None:
        nonlocal release_seen
        release_seen = True
        original_release(permit)

    monkeypatch.setattr(
        "chrys.orchestration.engine.state.controls.load_settings",
        lambda **kwargs: LoadedSettings(settings=Settings(default_approval_mode="auto"), provenance={}),
    )
    monkeypatch.setattr(engine.lifecycle, "reload", fake_soft_restart)
    monkeypatch.setattr(engine.permits, "release_rebuild_permit", release_with_marker)

    if mutation == "model_switch":
        await engine._on_set_model_profile(SetModelProfile(profile_id="new-model"))
    else:
        await engine._on_settings_reload(SettingsReload())

    assert event_release_states == [False]
