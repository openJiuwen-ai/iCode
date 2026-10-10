# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for hook config loading in agent lifecycle startup."""

from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from chrys.foundation.config.settings import Settings
from chrys.foundation.config.settings_store import LoadedSettings, SettingsHandle
from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import AGENT_LOAD_STATUS_DONE, AgentLoadProgress, Warning
from chrys.foundation.i18n import DisplayBlock, DisplayPath, Localizer
from chrys.foundation.models.workspace import Workspace
from chrys.orchestration import session_hooks
from chrys.orchestration.engine.assembly import assemble_agent_engine
from chrys.orchestration.engine.build import builder
from chrys.orchestration.engine.build.construction import StagedBuild
from chrys.orchestration.engine.build.loaded import CompletedBuild
from chrys.orchestration.engine.engine import AgentEngine
from chrys.orchestration.engine.loader import AgentLoader
from chrys.orchestration.engine.trajectory import TrajectoryRecorder
from chrys.orchestration.invoker.runtime import restore_phase4_state
from chrys.service.agent_middleware.reminders.archive_pointer import CATALOG_POINTER_RECORD_COUNT_STATE_KEY
from chrys.service.context.compaction.last_words_state import DropRoundBreakerState, ManifestEntry
from chrys.service.hooks.events import HookEvent
from chrys.service.hooks.loader import merge_hooks_files
from chrys.service.hooks.manager import HookManager
from chrys.service.hooks.schema import HookConfig, HookExecution, HookRun, HookSettings, HooksFile
from chrys.service.profiles.agents.schema import AgentProfile
from chrys.service.state.store import JsonFileStateStore
from chrys.service.trajectory.session import SessionTrajectory
from tests.support.loaded_agents import install_loaded_agent, make_loaded_agent, make_manifest
from tests.support.reminder_stack import reminder_pair
from tests.support.waiting import wait_until


def test_default_hook_discovery_uses_platform_isolation_without_a_loader_patch(tmp_path: Path) -> None:
    from chrys.foundation.platform import get_platform

    assert session_hooks.get_platform is get_platform
    assert session_hooks.get_platform().config_dir.is_relative_to(tmp_path)
    get_platform.cache_clear()
    assert session_hooks.get_platform().config_dir.is_relative_to(tmp_path)


class _StopStartup(Exception):
    """Sentinel used to stop startup once the observed call has run."""


class _FakeExecutor:
    def __init__(self) -> None:
        self.history_state: dict = {}
        self.service_session_id = ""
        self.service_session_storage_enabled = False

    @property
    def backend(self):
        return self

    @property
    def inputs(self):
        return self

    @property
    def state(self):
        return self

    @property
    def approval(self):
        return self

    @property
    def tool_events(self):
        return self


class _FakeHookManager:
    def __init__(self, label: str) -> None:
        self.label = label
        self.closed = False
        self.recovered = False

    async def drain_session(self, *, close: bool = True) -> None:
        self.closed = True

    async def recover_outbox(self) -> int:
        self.recovered = True
        return 0


class _Permits:
    def begin_agent_load(self) -> None:
        return None

    def finish_agent_load(self) -> None:
        return None


class _HookLoader:
    build_hook_manager = AgentLoader.build_hook_manager
    publish_load_progress = AgentLoader.publish_load_progress

    def __init__(self, bus, session) -> None:
        self._bus = bus
        self._session = session


class _Engine:
    def __init__(self, workspace_root: Path) -> None:
        self.current = SimpleNamespace(loaded=SimpleNamespace(), manifest=make_manifest())
        self._current = self.current
        self.permits = _Permits()
        self.session = SimpleNamespace()
        self.session.agent_profile = None
        self.session.workspace = Workspace(primary_cwd=str(workspace_root))
        self.session.session_id = "session-1"
        self.event_bus = EventBus()
        self.loader = _HookLoader(self.event_bus, self.session)
        self.session.hook_manager = None
        self.session.outbox_recovery_task = None
        self.session.guard = SimpleNamespace(ensure=lambda _sid: True)
        self._persistence = SimpleNamespace(state_store=None)
        self.session.mutation_coordinator = None
        install_loaded_agent(self, agent_profile_fingerprint="")
        install_loaded_agent(self, model_profile_fingerprint="")
        self._trajectory_recorder = TrajectoryRecorder()
        self._settings_handle = SettingsHandle(LoadedSettings(settings=Settings(), provenance={}))

    @property
    def loaded_settings(self) -> LoadedSettings:
        return self._settings_handle.loaded

    async def _subscribe_event_handlers(self) -> None:
        return None

    async def _publish_load_failed(self, **_kwargs: object) -> None:
        return None


def _completed_build(engine: AgentEngine, staged: StagedBuild, *, engine_services) -> CompletedBuild:
    """Supply candidate values for tests that isolate load orchestration."""
    return CompletedBuild(
        staged=staged,
        settings=engine.settings_handle.prepare(staged.loaded),
        workspace_retarget=engine_services(engine).workspace_change_tracker.resolve_retarget(
            staged.workspace,
            resolve_scope=engine.settings_handle.prepare(staged.loaded).effective.settings.workspace_change_notice,
        ),
        loaded=engine.current.loaded if engine.current.loaded is not None else make_loaded_agent(),
        manifest=engine.current.manifest,
        mutation_tracker=None,
        todo_tracker=None,
        compaction_strategy=None,
    )


def test_runtime_hook_snapshot_preserves_sources_and_omits_executable_configuration(tmp_path: Path) -> None:
    project_path = tmp_path / "project" / ".chrys" / "hooks" / "hooks.yaml"
    global_path = tmp_path / "config" / "hooks" / "hooks.yaml"
    project_hook = HookConfig(
        id="shared",
        event=HookEvent.BEFORE_TOOL_CALL,
        run=HookRun(type="command", argv=["project-secret"], env={"TOKEN": "project-token"}),
        execution=HookExecution(mode="blocking"),
        description="Project guard",
    )
    global_hook = HookConfig(
        id="shared",
        event=HookEvent.AFTER_TURN,
        run=HookRun(type="shell", shell="global-secret"),
        enabled=False,
        description="Global observer",
    )
    manager = HookManager(
        file=merge_hooks_files(
            project=HooksFile(hooks=[project_hook], source=str(project_path)),
            global_=HooksFile(hooks=[global_hook], source=str(global_path)),
        ),
        hooks_dir=tmp_path / "config" / "hooks",
    )

    sources = builder._runtime_hook_sources(manager)

    assert [source.scope for source in sources] == ["project", "global"]
    assert [source.source_path for source in sources] == [str(project_path), str(global_path)]
    assert [
        (hook.id, hook.event, hook.execution_mode, hook.enabled, hook.description) for hook in sources[0].hooks
    ] == [("shared", "before_tool_call", "blocking", True, "Project guard")]
    assert [
        (hook.id, hook.event, hook.execution_mode, hook.enabled, hook.description) for hook in sources[1].hooks
    ] == [("shared", "after_turn", "fire_and_forget", False, "Global observer")]
    assert set(vars(sources[0].hooks[0])) == {"id", "event", "execution_mode", "enabled", "description"}
    assert builder._runtime_hook_sources(None) == []


@pytest.mark.asyncio
async def test_publish_load_progress_keeps_message_and_threads_semantic_fields(tmp_path: Path) -> None:
    engine = _Engine(tmp_path)
    progress_events: list[AgentLoadProgress] = []

    async def _capture(event: AgentLoadProgress) -> None:
        progress_events.append(event)

    await engine.event_bus.subscribe(AgentLoadProgress, _capture)

    await engine.loader.publish_load_progress(
        phase="mcp",
        message="Connected MCP server fs",
        server_name="fs",
        current=1,
        total=1,
        status=AGENT_LOAD_STATUS_DONE,
        subject="fs",
        detail="diagnostic",
    )

    assert len(progress_events) == 1
    progress = progress_events[0]
    assert (
        progress.phase,
        progress.message,
        progress.server_name,
        progress.current,
        progress.total,
        progress.failed,
    ) == (
        "mcp",
        "Connected MCP server fs",
        "fs",
        1,
        1,
        0,
    )
    assert (progress.status, progress.subject, progress.detail, progress.session_id) == (
        AGENT_LOAD_STATUS_DONE,
        "fs",
        "diagnostic",
        "session-1",
    )


@pytest.mark.asyncio
async def test_hook_manager_build_normalizes_missing_global_hooks_to_none(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_dir = tmp_path / "config"
    project_root = tmp_path / "project"
    config_dir.mkdir()
    project_root.mkdir()
    engine = _Engine(project_root)
    seen: dict[str, HooksFile | None] = {}

    def _fake_get_platform() -> SimpleNamespace:
        return SimpleNamespace(config_dir=config_dir)

    def _fake_load_hooks_dir(_config_dir: Path) -> HooksFile:
        return HooksFile()

    def _fake_load_hooks_project(_project_root: Path) -> None:
        return None

    def _fake_merge_hooks_files(*, project: HooksFile | None, global_: HooksFile | None) -> None:
        seen["project"] = project
        seen["global_"] = global_
        raise _StopStartup

    monkeypatch.setattr(session_hooks, "get_platform", _fake_get_platform)
    monkeypatch.setattr("chrys.service.hooks.loader.load_hooks_dir", _fake_load_hooks_dir)
    monkeypatch.setattr("chrys.service.hooks.loader.load_hooks_project", _fake_load_hooks_project)
    monkeypatch.setattr("chrys.service.hooks.loader.merge_hooks_files", _fake_merge_hooks_files)

    with pytest.raises(_StopStartup):
        await engine.loader.build_hook_manager(project_root=str(project_root), project_hooks_enabled=True)

    assert seen["project"] is None
    assert seen["global_"] is None


@pytest.mark.asyncio
async def test_hook_manager_build_for_settings_only_global_hooks_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_dir = tmp_path / "config"
    project_root = tmp_path / "project"
    config_dir.mkdir()
    project_root.mkdir()
    engine = _Engine(project_root)

    def _fake_get_platform() -> SimpleNamespace:
        return SimpleNamespace(config_dir=config_dir)

    def _fake_load_hooks_dir(_config_dir: Path) -> HooksFile:
        return HooksFile(
            settings=HookSettings(shutdown_grace_seconds=9.0),
            settings_overrides={"shutdown_grace_seconds"},
            source=str(config_dir / "hooks" / "hooks.yaml"),
        )

    def _fake_load_hooks_project(_project_root: Path) -> None:
        return None

    monkeypatch.setattr(session_hooks, "get_platform", _fake_get_platform)
    monkeypatch.setattr("chrys.service.hooks.loader.load_hooks_dir", _fake_load_hooks_dir)
    monkeypatch.setattr("chrys.service.hooks.loader.load_hooks_project", _fake_load_hooks_project)

    manager = await engine.loader.build_hook_manager(project_root=str(project_root), project_hooks_enabled=True)

    assert manager is not None
    assert manager.file.settings.shutdown_grace_seconds == 9.0


@pytest.mark.asyncio
async def test_hook_manager_build_uses_isolated_global_config_dir(
    tmp_path: Path,
    _isolate_hook_config_dir: Path,
) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    engine = _Engine(project_root)

    assert await engine.loader.build_hook_manager(project_root=str(project_root), project_hooks_enabled=True) is None

    hooks_dir = _isolate_hook_config_dir / "hooks"
    hooks_dir.mkdir(parents=True)
    (hooks_dir / "hooks.yaml").write_text(
        """\
version: 1
hooks:
  - id: isolated-global-hook
    event: session_start
    run:
      type: command
      argv: ["test-hook-command"]
""",
        encoding="utf-8",
    )

    manager = await engine.loader.build_hook_manager(project_root=str(project_root), project_hooks_enabled=True)

    assert manager is not None
    assert [hook.id for hook in manager.file.hooks] == ["isolated-global-hook"]
    assert manager.file.sources == [str(hooks_dir / "hooks.yaml")]


@pytest.mark.asyncio
async def test_hook_manager_build_skips_project_hooks_when_the_setting_is_off(
    tmp_path: Path,
    _isolate_hook_config_dir: Path,
) -> None:
    project_root = tmp_path / "project"
    project_hooks_dir = project_root / ".chrys" / "hooks"
    project_hooks_dir.mkdir(parents=True)
    (project_hooks_dir / "hooks.yaml").write_text(
        """\
version: 1
hooks:
  - id: project-hook
    event: session_start
    run:
      type: command
      argv: ["test-hook-command"]
""",
        encoding="utf-8",
    )
    engine = _Engine(project_root)

    with_project = await engine.loader.build_hook_manager(project_root=str(project_root), project_hooks_enabled=True)
    assert with_project is not None
    assert [hook.id for hook in with_project.file.hooks] == ["project-hook"]

    # Off: the project file is not even opened, so no manager without global hooks.
    assert await engine.loader.build_hook_manager(project_root=str(project_root), project_hooks_enabled=False) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["global", "project"])
async def test_damaged_hook_file_warns_without_aborting_agent_build(
    tmp_path: Path,
    _isolate_hook_config_dir: Path,
    source: str,
) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    hooks_dir = _isolate_hook_config_dir / "hooks" if source == "global" else project_root / ".chrys" / "hooks"
    hooks_dir.mkdir(parents=True)
    (hooks_dir / "hooks.yaml").write_bytes(("# 中文配置说明\n" * 20).encode() + b"version: 1\n# damaged \xff\n")
    engine = _Engine(project_root)
    warnings: list[Warning] = []

    async def _capture(event: Warning) -> None:
        warnings.append(event)

    await engine.event_bus.subscribe(Warning, _capture)
    assert await engine.loader.build_hook_manager(project_root=str(project_root), project_hooks_enabled=True) is None
    assert len(warnings) == 1
    assert warnings[0].code == ("hooks_config_invalid" if source == "global" else "project_hooks_config_invalid")
    assert "Cannot decode hooks config file" in warnings[0].message


@pytest.mark.asyncio
async def test_settings_reload_flipping_project_hooks_rebuilds_the_hook_manager(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, engine_services
) -> None:
    profile = AgentProfile(name="Code")
    workspace = Workspace(primary_cwd=str(tmp_path / "ws"))
    old_manager = _FakeHookManager("old")
    new_manager = _FakeHookManager("new")
    engine = assemble_agent_engine(EventBus(), settings=Settings())
    engine.session.agent_profile = profile
    engine.session.workspace = workspace
    install_loaded_agent(engine, bindings=_FakeExecutor())  # type: ignore[assignment]
    engine.session.hook_manager = old_manager  # type: ignore[assignment]
    builds: list[tuple[str, object]] = []

    async def _fake_build_hook_manager(*, project_root: str, **kwargs: object) -> _FakeHookManager:
        builds.append((project_root, kwargs.get("project_hooks_enabled")))
        return new_manager

    async def _fake_build_agent(_profile: AgentProfile, staged: StagedBuild, **_kwargs: object) -> CompletedBuild:
        candidate = _completed_build(engine, staged, engine_services=engine_services)
        candidate = replace(candidate, loaded=replace(candidate.loaded, bindings=_FakeExecutor()))
        return candidate

    monkeypatch.setattr(engine.loader, "build_hook_manager", _fake_build_hook_manager)
    monkeypatch.setattr(engine.loader, "build", _fake_build_agent)

    # Same value: a plain settings reload leaves the manager alone.
    await engine.loader.reload(
        profile,
        operation="settings_reload",
        staged_loaded=LoadedSettings(settings=Settings(project_hooks_enabled=False), provenance={}),
    )
    assert builds == []
    assert engine.session.hook_manager is old_manager

    await engine.loader.reload(
        profile,
        operation="settings_reload",
        staged_loaded=LoadedSettings(settings=Settings(project_hooks_enabled=True), provenance={}),
    )
    if engine.session.outbox_recovery_task is not None:
        await engine.session.outbox_recovery_task

    assert builds == [(workspace.primary_cwd, True)]
    assert old_manager.closed is True
    assert engine.session.hook_manager is new_manager


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_source", ["global", "project"])
async def test_hook_manager_config_warning_keeps_legacy_text_and_semantics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    invalid_source: str,
) -> None:
    from chrys.service.hooks.loader import HooksConfigError

    config_dir = tmp_path / "config"
    project_root = tmp_path / "project"
    config_dir.mkdir()
    project_root.mkdir()
    engine = _Engine(project_root)
    warnings: list[Warning] = []

    async def _capture(event: Warning) -> None:
        warnings.append(event)

    def _fake_get_platform() -> SimpleNamespace:
        return SimpleNamespace(config_dir=config_dir)

    def _load_global(_config_dir: Path) -> HooksFile:
        if invalid_source == "global":
            raise HooksConfigError("invalid global hooks")
        return HooksFile()

    def _load_project(_project_root: Path) -> None:
        if invalid_source == "project":
            raise HooksConfigError("invalid project hooks")

    await engine.event_bus.subscribe(Warning, _capture)
    monkeypatch.setattr(session_hooks, "get_platform", _fake_get_platform)
    monkeypatch.setattr("chrys.service.hooks.loader.load_hooks_dir", _load_global)
    monkeypatch.setattr("chrys.service.hooks.loader.load_hooks_project", _load_project)

    assert await engine.loader.build_hook_manager(project_root=str(project_root), project_hooks_enabled=True) is None

    assert len(warnings) == 1
    if invalid_source == "global":
        expected_message = "Global hooks config could not be loaded: invalid global hooks.  Global hooks disabled."
        expected_key = "construction.global_hooks_invalid"
        expected_detail = "invalid global hooks"
        expected_code = "hooks_config_invalid"
    else:
        expected_message = (
            "Project hooks config could not be loaded: invalid project hooks. "
            "Project hooks disabled; global hooks unaffected."
        )
        expected_key = "construction.project_hooks_invalid"
        expected_detail = "invalid project hooks"
        expected_code = "project_hooks_config_invalid"
    assert (warnings[0].code, warnings[0].message, warnings[0].session_id) == (
        expected_code,
        expected_message,
        "session-1",
    )
    reference = warnings[0].display_message
    assert reference is not None
    assert reference.definition.key == expected_key
    assert dict(reference.args) == {"detail": DisplayBlock(expected_detail)}


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["global", "project"])
async def test_hook_with_an_invalid_regex_is_skipped_with_a_warning_naming_it_and_its_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source: str,
) -> None:
    config_dir = tmp_path / "config"
    project_root = tmp_path / "project"
    hooks_dir = config_dir / "hooks" if source == "global" else project_root / ".chrys" / "hooks"
    hooks_dir.mkdir(parents=True)
    project_root.mkdir(exist_ok=True)
    hooks_path = hooks_dir / "hooks.yaml"
    hooks_path.write_text(
        "version: 1\n"
        "hooks:\n"
        "  - id: guard\n"
        "    event: before_tool_call\n"
        "    match: {args: {command: {regex: 'rm (-rf'}}}\n"
        "    run: {type: command, argv: [/bin/true]}\n"
        "  - id: guard-sudo\n"
        "    event: before_tool_call\n"
        "    match: {args: {command: {regex: '^sudo '}}}\n"
        "    run: {type: command, argv: [/bin/true]}\n",
        encoding="utf-8",
    )
    with pytest.raises(re.error) as regex_error:
        re.compile("rm (-rf")
    reason = f"match.args.command.regex is not a valid regular expression: {regex_error.value}"
    engine = _Engine(project_root)
    warnings: list[Warning] = []

    async def _capture(event: Warning) -> None:
        warnings.append(event)

    def _fake_get_platform() -> SimpleNamespace:
        return SimpleNamespace(config_dir=config_dir)

    await engine.event_bus.subscribe(Warning, _capture)
    monkeypatch.setattr(session_hooks, "get_platform", _fake_get_platform)

    manager = await engine.loader.build_hook_manager(project_root=str(project_root), project_hooks_enabled=True)

    assert manager is not None
    assert [hook.id for hook in manager.file.hooks] == ["guard-sudo"]
    assert [(warning.code, warning.message, warning.session_id) for warning in warnings] == [
        (
            "hook_skipped",
            f"Hook 'guard' in {hooks_path} was skipped: {reason}. The other hooks in this file still run.",
            "session-1",
        )
    ]
    reference = warnings[0].display_message
    assert reference is not None
    assert reference.definition.key == "construction.hook_skipped"
    assert dict(reference.args) == {
        "hook_id": "guard",
        "path": DisplayPath(str(hooks_path)),
        "detail": DisplayBlock(reason),
    }


@pytest.mark.asyncio
async def test_start_workspace_change_stages_hook_manager_reload_for_the_retry_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    agent_engine,
) -> None:
    """A no-executor workspace-change retry rebuilds project hooks for the new
    root — but as a staged candidate: a build that fails keeps the old manager
    live and drains the candidate instead of installing it.
    """
    new_workspace = tmp_path / "new"
    new_workspace.mkdir()
    engine = agent_engine(EventBus(), settings=Settings(), initial_workspace=Workspace(primary_cwd=str(new_workspace)))
    old_manager = _FakeHookManager("old")
    new_manager = _FakeHookManager("new")
    engine.session.hook_manager = old_manager
    loaded_from: list[str] = []

    async def _fake_build_hook_manager(*, project_root: str, **_kwargs: object) -> _FakeHookManager:
        loaded_from.append(project_root)
        return new_manager

    async def _fail_build(_profile: AgentProfile, _staged: StagedBuild, **_kwargs: object) -> None:
        raise _StopStartup

    monkeypatch.setattr(engine.loader, "build_hook_manager", _fake_build_hook_manager)
    engine.loader.build = _fail_build  # type: ignore[attr-defined]

    try:
        with pytest.raises(_StopStartup):
            await engine.lifecycle.start(
                AgentProfile(name="Code"),
                operation="workspace_change",
            )

        assert loaded_from == [str(new_workspace)]
        assert engine.session.hook_manager is old_manager
        assert old_manager.closed is False
        assert new_manager.closed is True
    finally:
        engine.session.hook_manager = None


@pytest.mark.asyncio
async def test_start_does_not_materialize_session_dir_until_first_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, engine_services
) -> None:
    store = JsonFileStateStore(tmp_path)
    engine = assemble_agent_engine(EventBus(), settings=Settings(), state_store=store)
    profile = AgentProfile(name="Code")

    async def _fake_build_hook_manager(*, project_root: str, **_kwargs: object) -> None:
        return None

    async def _fake_build_agent(_profile: AgentProfile, staged: StagedBuild, **_kwargs: object) -> CompletedBuild:
        candidate = _completed_build(engine, staged, engine_services=engine_services)
        candidate = replace(candidate, loaded=replace(candidate.loaded, bindings=_FakeExecutor()))
        return candidate

    monkeypatch.setattr(engine.loader, "build_hook_manager", _fake_build_hook_manager)
    monkeypatch.setattr(engine.loader, "build", _fake_build_agent)

    try:
        await engine.lifecycle.start(profile)
        assert engine.session_id is not None
        assert not store.session_dir(engine.session_id).exists()
    finally:
        engine.session.guard.release()


@pytest.mark.asyncio
async def test_trajectory_activation_warning_keeps_the_bound_session_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, engine_services
) -> None:
    store = JsonFileStateStore(tmp_path)
    engine = assemble_agent_engine(EventBus(), settings=Settings(), state_store=store)
    profile = AgentProfile(name="Code")
    warnings: list[Warning] = []

    async def _capture(event: Warning) -> None:
        warnings.append(event)

    async def _fake_build_hook_manager(*, project_root: str, **_kwargs: object) -> None:
        return None

    async def _fake_build_agent(_profile: AgentProfile, staged: StagedBuild, **_kwargs: object) -> CompletedBuild:
        candidate = _completed_build(engine, staged, engine_services=engine_services)
        candidate = replace(candidate, loaded=replace(candidate.loaded, bindings=_FakeExecutor()))
        return candidate

    def _fail_activation(_trajectory: SessionTrajectory) -> None:
        raise RuntimeError("activation unavailable")

    await engine.event_bus.subscribe(Warning, _capture)
    monkeypatch.setattr(engine.loader, "build_hook_manager", _fake_build_hook_manager)
    monkeypatch.setattr(engine.loader, "build", _fake_build_agent)
    monkeypatch.setattr(SessionTrajectory, "_activate_locked", _fail_activation)
    bound_session_id: str | None = None
    try:
        await engine.lifecycle.start(profile)
        bound_session_id = engine.session_id
        trajectory = engine_services(engine).trajectory_recorder.trajectory
        assert trajectory is not None
        engine.session.session_id = "replacement-session"
        assert await trajectory.ensure_active() is False
        assert await wait_until(lambda: bool(warnings))
    finally:
        engine.session.session_id = bound_session_id
        engine.session.guard.release()

    assert len(warnings) == 1
    assert warnings[0].session_id == bound_session_id


@pytest.mark.asyncio
async def test_workspace_soft_restart_reloads_project_hook_manager_before_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, engine_services
) -> None:
    profile = AgentProfile(name="Code")
    old_workspace = Workspace(primary_cwd=str(tmp_path / "old"))
    new_workspace = Workspace(primary_cwd=str(tmp_path / "new"))
    old_manager = _FakeHookManager("old")
    new_manager = _FakeHookManager("new")
    engine = assemble_agent_engine(EventBus(), settings=Settings())
    engine.session.agent_profile = profile
    engine.session.workspace = old_workspace
    install_loaded_agent(engine, bindings=_FakeExecutor())  # type: ignore[assignment]
    engine.session.hook_manager = old_manager  # type: ignore[assignment]
    loaded_from: list[str] = []
    build_managers: list[_FakeHookManager | None] = []

    async def _fake_build_hook_manager(*, project_root: str, **_kwargs: object) -> _FakeHookManager:
        loaded_from.append(project_root)
        return new_manager

    async def _fake_build_agent(_profile: AgentProfile, staged: StagedBuild, **_kwargs: object) -> CompletedBuild:
        build_managers.append(staged.hook_manager)  # type: ignore[arg-type]
        candidate = _completed_build(engine, staged, engine_services=engine_services)
        candidate = replace(candidate, loaded=replace(candidate.loaded, bindings=_FakeExecutor()))
        return candidate

    monkeypatch.setattr(engine.loader, "build_hook_manager", _fake_build_hook_manager)
    monkeypatch.setattr(engine.loader, "build", _fake_build_agent)

    await engine.loader.reload(
        profile,
        workspace=new_workspace,
        operation="workspace_change",
    )
    if engine.session.outbox_recovery_task is not None:
        await engine.session.outbox_recovery_task

    assert loaded_from == [new_workspace.primary_cwd]
    assert build_managers == [new_manager]
    assert old_manager.closed is True
    assert new_manager.closed is False
    assert new_manager.recovered is True
    assert engine.session.hook_manager is new_manager


@pytest.mark.asyncio
async def test_soft_restart_rearms_preserved_phase4_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, engine_services
) -> None:
    profile = AgentProfile(name="Code")
    engine = assemble_agent_engine(EventBus(), settings=Settings())
    engine.session.agent_profile = profile
    engine.session.workspace = Workspace(primary_cwd=str(tmp_path))
    entry = ManifestEntry(
        record_id="r1",
        group_id="g1",
        record_dir="compactions/dropped/turn001",
        relative_path="compactions/dropped/turn001/001_tool_r1.md",
        turn=1,
        round=1,
        sequence=1,
        tool="read_file",
        display_argument='path="safe.txt"',
        outcome="success",
        size_chars=12,
    )
    breaker = DropRoundBreakerState(attempts=2, consecutive_no_progress=1, tail_override=True, side_call_tokens=123)
    install_loaded_agent(engine, bindings=_FakeExecutor())  # type: ignore[assignment]
    engine.current.loaded.bindings.backend.history_state = {
        "messages": [],
        "compressed_msgs": [],
        "last_words": "[LAST_WORDS] resume from step 3",
        "last_words_manifest": [entry.to_state()],
        "last_words_breaker": breaker.to_state(),
    }
    reminder, last_words = reminder_pair()
    install_loaded_agent(engine, reminder_middleware=reminder, last_words=last_words)
    engine.current.loaded.reminder_middleware.sources.archive_pointer.restore_record_count(7)

    async def _fake_build_agent(
        _profile: AgentProfile,
        staged: StagedBuild,
        *,
        preserved_history: dict | None = None,
        **_kwargs: object,
    ) -> CompletedBuild:
        candidate = _completed_build(engine, staged, engine_services=engine_services)
        candidate = replace(candidate, loaded=replace(candidate.loaded, bindings=_FakeExecutor()))
        displaced_reminder, displaced_last_words = reminder_pair()
        install_loaded_agent(engine, reminder_middleware=displaced_reminder, last_words=displaced_last_words)
        # Mirror the real commit tail: the preserved conversation and phase-4
        # state go live with the executor, and the manager binds that dict.
        if preserved_history is not None:
            candidate.loaded.bindings.backend.history_state = preserved_history
            restore_phase4_state(candidate.loaded.reminder_middleware, candidate.loaded.last_words, preserved_history)
        return candidate

    monkeypatch.setattr(engine.loader, "build", _fake_build_agent)

    await engine.loader.reload(profile, operation="model_switch")

    middleware = engine.current.loaded.reminder_middleware
    state = engine.current.loaded.last_words
    assert middleware is not None
    assert state.get_last_words() == "[LAST_WORDS] resume from step 3"
    assert state.get_last_words_manifest()[0]["record_id"] == "r1"
    assert state.get_drop_round_breaker() == breaker
    assert middleware.sources.archive_pointer.record_count_state() == 7
    assert engine.current.loaded.bindings.backend.history_state[CATALOG_POINTER_RECORD_COUNT_STATE_KEY] == 7

    middleware.prepare_turn(usage={}, preserve_last_words=True)

    assert state.get_last_words() == "[LAST_WORDS] resume from step 3"
    assert state.get_last_words_manifest()[0]["record_id"] == "r1"
    assert state.get_drop_round_breaker() == breaker
    assert middleware.sources.archive_pointer.record_count_state() == 7


@pytest.mark.asyncio
async def test_soft_restart_incompatible_service_session_warning_keeps_legacy_text_and_semantics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, engine_services
) -> None:
    monkeypatch.setenv("CHRYS_LOCALE", "en")
    profile = AgentProfile(name="Code")
    engine = assemble_agent_engine(EventBus(), settings=Settings())
    engine.session.agent_profile = profile
    engine.session.workspace = Workspace(primary_cwd=str(tmp_path))
    install_loaded_agent(engine, bindings=_FakeExecutor())  # type: ignore[assignment]
    engine.current.loaded.bindings.backend.service_session_id = "response-session"
    engine.current.loaded.bindings.backend.service_session_storage_enabled = True
    warnings: list[Warning] = []

    async def _capture(event: Warning) -> None:
        warnings.append(event)

    async def _fake_build_agent(_profile: AgentProfile, staged: StagedBuild, **_kwargs: object) -> CompletedBuild:
        candidate = _completed_build(engine, staged, engine_services=engine_services)
        candidate = replace(candidate, loaded=replace(candidate.loaded, bindings=_FakeExecutor()))
        return candidate

    await engine.event_bus.subscribe(Warning, _capture)
    monkeypatch.setattr(engine.loader, "build", _fake_build_agent)

    await engine.loader.reload(profile, operation="model_switch")

    assert engine.current.loaded.bindings.backend.service_session_id == ""
    assert len(warnings) == 1
    warning = warnings[0]
    expected_message = (
        "The previous OpenAI Responses service session is not compatible with the active agent profile, workspace, "
        "model profile, service endpoint, or storage is disabled. iCode will continue from local history only."
    )
    assert (warning.code, warning.message, warning.session_id) == (
        "service_session_incompatible",
        expected_message,
        None,
    )
    reference = warning.display_message
    assert reference is not None
    assert reference.definition.key == "construction.service_session_incompatible"
    assert dict(reference.args) == {"app_name": "iCode"}
    assert Localizer(engine.settings.locale).render(reference) == expected_message


@pytest.mark.asyncio
async def test_workspace_soft_restart_keeps_reloaded_hook_manager_after_post_build_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, engine_services
) -> None:
    profile = AgentProfile(name="Code")
    old_workspace = Workspace(primary_cwd=str(tmp_path / "old"))
    new_workspace = Workspace(primary_cwd=str(tmp_path / "new"))
    old_manager = _FakeHookManager("old")
    new_manager = _FakeHookManager("new")
    engine = assemble_agent_engine(EventBus(), settings=Settings())
    engine.session.agent_profile = profile
    engine.session.workspace = old_workspace
    install_loaded_agent(engine, bindings=_FakeExecutor())  # type: ignore[assignment]
    engine.session.hook_manager = old_manager  # type: ignore[assignment]

    async def _fake_build_hook_manager(*, project_root: str, **_kwargs: object) -> _FakeHookManager:
        return new_manager

    async def _fake_build_agent(_profile: AgentProfile, staged: StagedBuild, **_kwargs: object) -> CompletedBuild:
        candidate = _completed_build(engine, staged, engine_services=engine_services)
        candidate = replace(candidate, loaded=replace(candidate.loaded, bindings=_FakeExecutor()))
        # The history bind rides the commit now, so the injected failure
        # below fires inside the build — after the staged hook manager went
        # live — instead of in a post-build continuation.
        return candidate

    def _fail_bind(_state: dict) -> None:
        raise RuntimeError("bind failed")

    monkeypatch.setattr(engine.loader, "build_hook_manager", _fake_build_hook_manager)
    monkeypatch.setattr(engine.loader, "build", _fake_build_agent)
    monkeypatch.setattr(engine_services(engine).history, "bind", _fail_bind)

    with pytest.raises(RuntimeError, match="bind failed"):
        await engine.loader.reload(
            profile,
            workspace=new_workspace,
            operation="workspace_change",
        )
    if engine.session.outbox_recovery_task is not None:
        await engine.session.outbox_recovery_task

    assert old_manager.closed is True
    assert new_manager.closed is False
    assert new_manager.recovered is True
    assert engine.session.hook_manager is new_manager
    assert engine.workspace is new_workspace
