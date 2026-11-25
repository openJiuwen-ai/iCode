# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for ACP session mutations: agent/model switch, inject, rollback, approval mode, workspace, reload."""

from __future__ import annotations

import asyncio

import pytest

from chrys.app.acp.session_manager import AcpSessionError, ManagedSession
from chrys.foundation.config.settings import Settings
from chrys.foundation.config.settings_store import LoadedSettings
from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import (
    AgentLoadFailed,
    AgentProfileSwitch,
    ApprovalModeUpdated,
    Error,
    ModelProfileSwitched,
    ProfileSwitched,
    RollbackResult,
    SetApprovalMode,
    SetModelProfile,
    SettingsReload,
    SettingsReloaded,
    UserInject,
    UserMessage,
    UserRollback,
    Warning,
    WorkspaceChange,
    WorkspaceChangeFinished,
)
from chrys.service.profiles.models.schema import ModelProfile
from tests.app.acp._session_manager_fakes import (
    _CloseHost,
    _InjectHost,
    _manager,
    _StaticListStore,
)
from tests.support.async_tasks import wait_for_completion


@pytest.mark.asyncio
async def test_set_workspace_rejects_nonexistent_dir(tmp_path) -> None:
    manager = _manager(str(tmp_path), _StaticListStore([]))
    bus = EventBus()
    published: list[WorkspaceChange] = []

    async def _capture(event: WorkspaceChange) -> None:
        published.append(event)

    await bus.subscribe(WorkspaceChange, _capture)
    manager._sessions["s1"] = ManagedSession(  # type: ignore[assignment]
        session_id="s1",
        cwd=str(tmp_path),
        profile_name="Code",
        host=_InjectHost(bus, is_turn_active=True),  # type: ignore[arg-type]
    )

    async with manager._sessions["s1"].mutation_lock:
        with pytest.raises((AcpSessionError, FileNotFoundError)):
            await asyncio.wait_for(manager.set_workspace("s1", str(tmp_path / "does-not-exist")), timeout=1)

    # Validation happens before publishing, so no broken workspace is rebuilt/persisted.
    assert published == []


@pytest.mark.asyncio
async def test_switch_agent_rejects_unknown_profile(tmp_path) -> None:
    manager = _manager(str(tmp_path), _StaticListStore([]))
    manager._sessions["s1"] = ManagedSession(  # type: ignore[assignment]
        session_id="s1",
        cwd=str(tmp_path),
        profile_name="Code",
        host=_CloseHost(),
    )

    with pytest.raises(AcpSessionError, match="Agent profile not found: Ghost"):
        await manager.switch_agent("s1", "Ghost")


@pytest.mark.asyncio
async def test_switch_agent_same_profile_is_noop(tmp_path) -> None:
    manager = _manager(str(tmp_path), _StaticListStore([]))
    host = _CloseHost()
    host.session_id = "s1"  # type: ignore[attr-defined]
    host.event_bus = EventBus()  # type: ignore[attr-defined]
    # The live engine snapshot carries the current runtime so the no-op response
    # does not blank out the client's model/tool/skill state.
    host.engine.snapshot = ProfileSwitched(
        from_profile="Code",
        to_profile="Code",
        from_display_name="Code",
        to_display_name="Code",
        message_count=4,
        model_profile_id="gpt-5",
        max_context_tokens=200000,
        session_id="s1",
        tool_names=["shell", "read_file"],
        skill_names=["search"],
        sub_agent_tool_names=["explore"],
        memory_files=["AGENTS.md"],
    )
    seen: list[AgentProfileSwitch] = []

    async def _unexpected_backend_switch(event: AgentProfileSwitch) -> None:
        seen.append(event)

    await host.event_bus.subscribe(AgentProfileSwitch, _unexpected_backend_switch)  # type: ignore[attr-defined]
    manager._sessions["s1"] = ManagedSession(  # type: ignore[assignment]
        session_id="s1",
        cwd=str(tmp_path),
        profile_name="Code",
        host=host,
    )

    result = await wait_for_completion(manager.switch_agent("s1", "Code"))

    assert result.from_profile == "Code"
    assert result.to_profile == "Code"
    assert result.session_id == "s1"
    # Runtime fields must reflect the live engine, not dataclass blanks.
    assert result.model_profile_id == "gpt-5"
    assert result.max_context_tokens == 200000
    assert result.tool_names == ["shell", "read_file"]
    assert result.skill_names == ["search"]
    assert result.sub_agent_tool_names == ["explore"]
    assert result.memory_files == ["AGENTS.md"]
    assert seen == []
    assert result.message_count == 4


@pytest.mark.asyncio
async def test_switch_agent_surfaces_rebuild_failure(tmp_path) -> None:
    manager = _manager(str(tmp_path), _StaticListStore([]))
    bus = EventBus()

    class _BusHost:
        session_id = "s1"
        event_bus = bus

    async def _fail_rebuild(event: AgentProfileSwitch) -> None:
        # Mirror soft_restart: a valid profile whose rebuild fails publishes
        # AgentLoadFailed (then raises inside the handler, which the bus swallows).
        await bus.publish(AgentLoadFailed(session_id="s1", agent_profile=event.profile_name, message="rebuild boom"))

    await bus.subscribe(AgentProfileSwitch, _fail_rebuild)
    manager._sessions["s1"] = ManagedSession(  # type: ignore[assignment]
        session_id="s1",
        cwd=str(tmp_path),
        profile_name="Bootstrap",
        host=_BusHost(),  # type: ignore[arg-type]
    )

    with pytest.raises(AcpSessionError, match="rebuild boom"):
        await manager.switch_agent("s1", "Code")


@pytest.mark.asyncio
async def test_inject_rejects_when_no_active_turn(tmp_path) -> None:
    manager = _manager(str(tmp_path), _StaticListStore([]))
    manager._sessions["s1"] = ManagedSession(  # type: ignore[assignment]
        session_id="s1",
        cwd=str(tmp_path),
        profile_name="Code",
        host=_InjectHost(EventBus(), is_turn_active=False),  # type: ignore[arg-type]
    )

    with pytest.raises(AcpSessionError, match="No active turn"):
        await manager.inject("s1", "more context")


@pytest.mark.asyncio
async def test_inject_publishes_user_inject_when_active(tmp_path) -> None:
    manager = _manager(str(tmp_path), _StaticListStore([]))
    bus = EventBus()
    injects: list[UserInject] = []
    messages: list[UserMessage] = []

    async def _capture_inject(event: UserInject) -> None:
        injects.append(event)

    async def _capture_message(event: UserMessage) -> None:
        messages.append(event)

    await bus.subscribe(UserInject, _capture_inject)
    await bus.subscribe(UserMessage, _capture_message)
    manager._sessions["s1"] = ManagedSession(  # type: ignore[assignment]
        session_id="s1",
        cwd=str(tmp_path),
        profile_name="Code",
        host=_InjectHost(bus, is_turn_active=True),  # type: ignore[arg-type]
    )

    await manager.inject("s1", "more context")

    # UserInject (never starts a turn), not UserMessage (would start a stray turn).
    assert [e.text for e in injects] == ["more context"]
    assert injects[0].session_id == "s1"
    assert messages == []


@pytest.mark.asyncio
async def test_rollback_ignores_non_fatal_restore_warnings(tmp_path) -> None:
    manager = _manager(str(tmp_path), _StaticListStore([]))
    bus = EventBus()

    async def _on_rollback(event: UserRollback) -> None:
        # A successful rollback restores the session before publishing its result,
        # and restore can emit unrelated non-fatal warnings. These must not be
        # mistaken for a rollback refusal.
        await bus.publish(Warning(code="service_session_incompatible", message="local only", session_id="s1"))
        await bus.publish(Warning(code="sub_agents_reload_discarded", message="discarded", session_id="s1"))
        await bus.publish(RollbackResult(session_id="s1", target_turn=event.target_turn))

    await bus.subscribe(UserRollback, _on_rollback)
    manager._sessions["s1"] = ManagedSession(  # type: ignore[assignment]
        session_id="s1",
        cwd=str(tmp_path),
        profile_name="Code",
        host=_InjectHost(bus, is_turn_active=False),  # type: ignore[arg-type]
    )

    result = await manager.rollback("s1", target_turn=2, revert_changes=False, selected_paths=None)

    assert result.target_turn == 2


@pytest.mark.asyncio
async def test_rollback_refusal_warning_fails_request(tmp_path) -> None:
    manager = _manager(str(tmp_path), _StaticListStore([]))
    bus = EventBus()

    async def _on_rollback(_event: UserRollback) -> None:
        # A genuine refusal (rollback_* code) emits no RollbackResult and must
        # surface as an error rather than hanging until the timeout.
        await bus.publish(Warning(code="rollback_refused", message="cannot roll back", session_id="s1"))

    await bus.subscribe(UserRollback, _on_rollback)
    manager._sessions["s1"] = ManagedSession(  # type: ignore[assignment]
        session_id="s1",
        cwd=str(tmp_path),
        profile_name="Code",
        host=_InjectHost(bus, is_turn_active=False),  # type: ignore[arg-type]
    )

    with pytest.raises(AcpSessionError, match="cannot roll back"):
        await manager.rollback("s1", target_turn=1, revert_changes=False, selected_paths=None)


@pytest.mark.asyncio
async def test_set_approval_mode_publishes_session_scoped_event(tmp_path) -> None:
    manager = _manager(str(tmp_path), _StaticListStore([]))
    bus = EventBus()
    seen: list[SetApprovalMode] = []

    async def _echo(event: SetApprovalMode) -> None:
        seen.append(event)
        await bus.publish(ApprovalModeUpdated(mode=event.mode, session_id="s1"))

    await bus.subscribe(SetApprovalMode, _echo)
    manager._sessions["s1"] = ManagedSession(  # type: ignore[assignment]
        session_id="s1",
        cwd=str(tmp_path),
        profile_name="Code",
        host=_InjectHost(bus, is_turn_active=True),  # type: ignore[arg-type]
    )

    result = await manager.set_approval_mode("s1", "auto")

    assert result.mode == "auto"
    assert len(seen) == 1
    assert seen[0].mode == "auto"
    assert seen[0].persist is False
    assert seen[0].session_id == "s1"


@pytest.mark.asyncio
async def test_concurrent_approval_mode_updates_resolve_independently(tmp_path) -> None:
    manager = _manager(str(tmp_path), _StaticListStore([]))
    bus = EventBus()
    seen: list[str] = []

    async def _echo(event: SetApprovalMode) -> None:
        await asyncio.sleep(0)
        seen.append(event.mode)
        await bus.publish(ApprovalModeUpdated(mode=event.mode, session_id="s1"))

    await bus.subscribe(SetApprovalMode, _echo)
    manager._sessions["s1"] = ManagedSession(  # type: ignore[assignment]
        session_id="s1",
        cwd=str(tmp_path),
        profile_name="Code",
        host=_InjectHost(bus, is_turn_active=True),  # type: ignore[arg-type]
    )

    results = await asyncio.gather(
        manager.set_approval_mode("s1", "auto"),
        manager.set_approval_mode("s1", "bypass"),
    )

    assert [result.mode for result in results] == ["auto", "bypass"]
    assert seen == ["auto", "bypass"]


@pytest.mark.asyncio
async def test_set_model_profile_surfaces_rebuild_failure(tmp_path) -> None:
    manager = _manager(str(tmp_path), _StaticListStore([]))
    bus = EventBus()

    async def _fail(event: SetModelProfile) -> None:
        await bus.publish(AgentLoadFailed(session_id="s1", message="model boom"))

    await bus.subscribe(SetModelProfile, _fail)
    manager._sessions["s1"] = ManagedSession(  # type: ignore[assignment]
        session_id="s1",
        cwd=str(tmp_path),
        profile_name="Code",
        host=_InjectHost(bus, is_turn_active=True),  # type: ignore[arg-type]
    )

    with pytest.raises(AcpSessionError, match="model boom"):
        await manager.set_model_profile("s1", "model")


@pytest.mark.asyncio
async def test_set_workspace_surfaces_rebuild_failure(tmp_path) -> None:
    manager = _manager(str(tmp_path), _StaticListStore([]))
    bus = EventBus()

    async def _fail(event: WorkspaceChange) -> None:
        await bus.publish(AgentLoadFailed(session_id="s1", message="unrelated workspace load failure"))
        await bus.publish(
            WorkspaceChangeFinished(session_id="s1", request_id=event.event_id, error=Error(message="workspace boom"))
        )

    await bus.subscribe(WorkspaceChange, _fail)
    manager._sessions["s1"] = ManagedSession(  # type: ignore[assignment]
        session_id="s1",
        cwd=str(tmp_path),
        profile_name="Code",
        host=_InjectHost(bus, is_turn_active=True),  # type: ignore[arg-type]
    )

    with pytest.raises(AcpSessionError, match="unrelated workspace load failure"):
        await manager.set_workspace("s1", str(tmp_path))


@pytest.mark.asyncio
async def test_reload_settings_surfaces_rebuild_failure(tmp_path) -> None:
    manager = _manager(str(tmp_path), _StaticListStore([]))
    bus = EventBus()

    async def _fail(_event: SettingsReload) -> None:
        # A reload whose soft-restart fails publishes AgentLoadFailed and then
        # raises inside the bus handler (swallowed) — the await must surface it
        # rather than report a silent success.
        await bus.publish(AgentLoadFailed(session_id="s1", message="reload boom"))

    await bus.subscribe(SettingsReload, _fail)
    manager._sessions["s1"] = ManagedSession(  # type: ignore[assignment]
        session_id="s1",
        cwd=str(tmp_path),
        profile_name="Code",
        host=_InjectHost(bus, is_turn_active=True),  # type: ignore[arg-type]
    )

    with pytest.raises(AcpSessionError, match="reload boom"):
        await manager.reload_settings("s1")


@pytest.mark.asyncio
async def test_reload_settings_surfaces_handler_error(tmp_path) -> None:
    manager = _manager(str(tmp_path), _StaticListStore([]))
    bus = EventBus()

    async def _fail(_event: SettingsReload) -> None:
        # Mirror a bad-env reload: the handler reports an Error then raises before
        # publishing any completion event. The bus swallows the raise, so the await
        # must resolve on the Error rather than hanging until the timeout.
        await bus.publish(Error(code="settings_reload_failed", message="bad env", session_id="s1"))
        raise ValueError("bad env")

    await bus.subscribe(SettingsReload, _fail)
    manager._sessions["s1"] = ManagedSession(  # type: ignore[assignment]
        session_id="s1",
        cwd=str(tmp_path),
        profile_name="Code",
        host=_InjectHost(bus, is_turn_active=True),  # type: ignore[arg-type]
    )

    with pytest.raises(AcpSessionError, match="bad env"):
        await manager.reload_settings("s1")


@pytest.mark.asyncio
async def test_reload_settings_awaits_completion_event(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    manager = _manager(str(tmp_path), _StaticListStore([]))
    manager._loaded_settings = LoadedSettings(
        settings=Settings(model_profile="old-model", ask_user_timeout_seconds=None),
        provenance={},
    )
    monkeypatch.setenv("CHRYS_MODEL_PROFILE", "new-model")
    bus = EventBus()

    async def _succeed(_event: SettingsReload) -> None:
        await bus.publish(SettingsReloaded(session_id="s1"))

    await bus.subscribe(SettingsReload, _succeed)
    manager._sessions["s1"] = ManagedSession(  # type: ignore[assignment]
        session_id="s1",
        cwd=str(tmp_path),
        profile_name="Code",
        host=_InjectHost(bus, is_turn_active=True),  # type: ignore[arg-type]
    )

    result = await manager.reload_settings("s1")

    assert isinstance(result, SettingsReloaded)
    assert result.session_id == "s1"
    assert manager._loaded_settings.settings.model_profile == "new-model"
    assert manager._loaded_settings.settings.ask_user_timeout_seconds is None


@pytest.mark.asyncio
async def test_concurrent_model_switches_resolve_independently(tmp_path) -> None:
    # Two overlapping mutations of the same result type must not cross-resolve:
    # the per-session lock serializes them so each awaits its own completion.

    manager = _manager(str(tmp_path), _StaticListStore([]))
    bus = EventBus()
    seen: list[str] = []

    async def _echo(event: SetModelProfile) -> None:
        # Yield so a second request can interleave if the lock were absent, then
        # echo a result tagged with the profile that drove this rebuild.
        await asyncio.sleep(0)
        seen.append(event.profile_id)
        await bus.publish(ModelProfileSwitched(session_id="s1", model_profile_id=event.profile_id))

    await bus.subscribe(SetModelProfile, _echo)
    manager._sessions["s1"] = ManagedSession(  # type: ignore[assignment]
        session_id="s1",
        cwd=str(tmp_path),
        profile_name="Code",
        host=_InjectHost(bus, is_turn_active=True),  # type: ignore[arg-type]
    )
    manager._model_registry.register(ModelProfile(id="a", name="A"))
    manager._model_registry.register(ModelProfile(id="b", name="B"))

    results = await asyncio.gather(
        manager.set_model_profile("s1", "a"),
        manager.set_model_profile("s1", "b"),
    )

    # Each call resolved from its own echo, not whichever fired first.
    assert {r.model_profile_id for r in results} == {"a", "b"}
    assert sorted(seen) == ["a", "b"]
