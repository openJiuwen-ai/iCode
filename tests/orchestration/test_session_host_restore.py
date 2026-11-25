# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Session restore through the headless Chrys session host.

What a restore has to bring back — the conversation, the workspace it was
pinned to, the profile and the calibration record — and how it reports the
requests it cannot honour.
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

import chrys.orchestration.engine.build.builder as builder_module
import chrys.orchestration.session_host as session_host_module
from chrys.foundation.config.settings_store import DormantWorkdirConfig, LoadedSettings, load_settings
from chrys.foundation.events.types import (
    AgentLoadFailed,
    AgentLoadFinished,
    AgentLoadStarted,
    SessionRestore,
    SessionRestored,
    Warning,
)
from chrys.foundation.i18n import MessageRef
from chrys.foundation.models.history_markers import HistoryMarkerKind
from chrys.foundation.models.workspace import WorkingDir, Workspace
from chrys.foundation.platform import get_platform
from chrys.foundation.util.session_ids import session_short_id
from chrys.kernel import Message
from chrys.orchestration.session_host import (
    AgentProfileNotFoundError,
    AmbiguousSessionIdError,
    ChrysSessionHost,
    HeadlessRunError,
    SessionNotFoundError,
)
from chrys.service.llm.mock import MockChatClient, MockResponse
from chrys.service.profiles.agents.schema import (
    AgentProfile,
    ApprovalConfig,
    CompactionConfig,
    ToolsConfig,
)
from chrys.service.state.store import (
    SESSION_ID_AMBIGUOUS,
    ConflictingSessionCheckpointsError,
    FileLock,
    JsonFileStateStore,
    StartupSessionInUseError,
)
from tests.orchestration._session_host_helpers import (
    _make_agent_registry,
    _make_agent_registry_many,
    _make_profile,
    _make_settings_and_models,
    _patch_runtime,
)

# Restore-path tests can run two serial authority-bound phases, each with a
# 45s async timeout. The repository's 60s default could kill the worker during
# phase two before its own TimeoutError surfaces; 120s leaves 30s beyond the
# declared 90s path budget for diagnostics and teardown.
pytestmark = pytest.mark.timeout(120)


@pytest.mark.asyncio
async def test_session_host_restores_existing_session(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    first_client = MockChatClient(responses=[MockResponse(text="first response")])
    second_client = MockChatClient(responses=[MockResponse(text="second response")])
    clients = [first_client, second_client]
    _patch_runtime(monkeypatch, clients, [])
    settings, model_registry = _make_settings_and_models()
    store = JsonFileStateStore(tmp_path / "sessions")
    agent_registry = _make_agent_registry(_make_profile(approval_default="skip"))

    first = ChrysSessionHost(
        profile_name="Headless",
        settings=settings,
        agent_registry=agent_registry,
        model_registry=model_registry,
        state_store=store,
    )
    try:
        first_result = await first.run_until_final("first prompt", timeout=45)
        session_id = first_result.session_id
    finally:
        await first.shutdown()

    second = ChrysSessionHost(
        profile_name="Headless",
        session_id=session_id,
        settings=settings,
        agent_registry=agent_registry,
        model_registry=model_registry,
        state_store=store,
    )
    restore_requests: list[SessionRestore] = []

    async def collect_restore_request(event: SessionRestore) -> None:
        restore_requests.append(event)

    await second._bus.subscribe(SessionRestore, collect_restore_request)
    try:
        second_result = await second.run_until_final("second prompt", timeout=45, keep_events=True)
    finally:
        await second.shutdown()

    raw = await store.load_session_raw(session_id)
    user_texts = [
        content["text"]
        for message in raw or []
        if message["role"] == "user"
        for content in message["contents"]
        if content.get("type") == "text"
    ]
    assert second_result.session_id == session_id
    assert any(isinstance(event, SessionRestored) and event.session_id == session_id for event in second_result.events)
    assert len(restore_requests) == 1
    assert restore_requests[0].apply_saved_model is False
    assert user_texts == ["first prompt", "second prompt"]
    assert [event.operation for event in second_result.events if isinstance(event, AgentLoadStarted)] == ["restore"]
    assert [event.operation for event in second_result.events if isinstance(event, AgentLoadFinished)] == ["restore"]
    assert clients == []
    session_dirs = [
        path.name for path in (tmp_path / "sessions").iterdir() if path.is_dir() and not path.name.startswith(".")
    ]
    assert session_dirs == [session_short_id(session_id)]


@pytest.mark.asyncio
async def test_session_host_restore_publishes_workdir_settings_warnings(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """The restore re-derives settings for the target root and must report
    that root's verdicts on the bus under the restored session's id — the
    same report a reload or workspace change makes."""
    first_client = MockChatClient(responses=[MockResponse(text="first response")])
    second_client = MockChatClient(responses=[MockResponse(text="second response")])
    _patch_runtime(monkeypatch, [first_client, second_client], [])
    settings, model_registry = _make_settings_and_models()
    store = JsonFileStateStore(tmp_path / "sessions")
    agent_registry = _make_agent_registry(_make_profile(approval_default="skip"))

    first = ChrysSessionHost(
        profile_name="Headless",
        settings=settings,
        agent_registry=agent_registry,
        model_registry=model_registry,
        state_store=store,
    )
    try:
        session_id = (await first.run_until_final("first prompt", timeout=45)).session_id
    finally:
        await first.shutdown()

    real_load_settings = load_settings

    def load_with_dormant_workdir(**kwargs: Any) -> LoadedSettings:
        # Tests never freeze the process env, so the real load reads no files;
        # graft the verdict a workdir document at the target root would earn.
        loaded = real_load_settings(**kwargs)
        dormant_path = Path(kwargs["workdir"]) / ".chrys" / "settings.yaml"
        return replace(
            loaded,
            dormant_workdir=(DormantWorkdirConfig(path=dormant_path, keys=("tools.result.ceiling_tokens",)),),
        )

    monkeypatch.setattr("chrys.orchestration.engine.session_lifecycle.load_settings", load_with_dormant_workdir)
    second = ChrysSessionHost(
        profile_name="Headless",
        session_id=session_id,
        settings=settings,
        agent_registry=agent_registry,
        model_registry=model_registry,
        state_store=store,
    )
    warnings: list[Warning] = []

    async def collect_warning(event: Warning) -> None:
        warnings.append(event)

    await second._bus.subscribe(Warning, collect_warning)
    try:
        second_result = await second.run_until_final("second prompt", timeout=45)
    finally:
        await second.shutdown()

    assert second_result.session_id == session_id
    dormant = [event for event in warnings if event.code == "workdir_config_dormant"]
    assert [event.session_id for event in dormant] == [session_id]


@pytest.mark.asyncio
async def test_session_host_restore_preserves_surrogateescaped_workspace_paths(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    if not get_platform().is_linux:
        pytest.skip("invalid-UTF-8 filesystem paths are Linux-specific")

    clients = [MockChatClient(responses=[MockResponse(text="first response")]), MockChatClient()]
    _patch_runtime(monkeypatch, clients, [])
    settings, model_registry = _make_settings_and_models()
    store = JsonFileStateStore(tmp_path / "sessions")
    agent_registry = _make_agent_registry(_make_profile(approval_default="skip"))

    first = ChrysSessionHost(
        profile_name="Headless",
        settings=settings,
        agent_registry=agent_registry,
        model_registry=model_registry,
        state_store=store,
    )
    try:
        session_id = (await first.run_until_final("first prompt", timeout=45)).session_id
    finally:
        await first.shutdown()

    raw_cwd_bytes = os.path.join(os.fsencode(tmp_path), b"restored-primary-\xff")
    raw_extra_bytes = os.path.join(os.fsencode(tmp_path), b"restored-extra-\xfe")
    os.mkdir(raw_cwd_bytes)
    os.mkdir(raw_extra_bytes)
    raw_cwd = os.fsdecode(raw_cwd_bytes)
    raw_extra = os.fsdecode(raw_extra_bytes)
    state = await store.load_session(session_id)
    assert state is not None
    await store.save_session(
        session_id,
        state,
        agent_profile="Headless",
        primary_cwd=raw_cwd,
        working_dirs=[raw_cwd, raw_extra],
    )
    second = ChrysSessionHost(
        profile_name="Headless",
        session_id=session_id,
        settings=settings,
        agent_registry=agent_registry,
        model_registry=model_registry,
        state_store=store,
    )
    try:
        await second.start()
        restored_workspace = second.engine.workspace
        restored_paths = [working_dir.path for working_dir in restored_workspace.working_dirs]
    finally:
        await second.shutdown()

    assert restored_workspace.primary_cwd == raw_cwd
    assert restored_paths == [raw_cwd, raw_extra]
    assert os.fsencode(restored_workspace.primary_cwd) == raw_cwd_bytes
    assert [os.fsencode(path) for path in restored_paths] == [raw_cwd_bytes, raw_extra_bytes]
    assert all(os.path.isdir(path) for path in [restored_workspace.primary_cwd, *restored_paths])


@pytest.mark.asyncio
async def test_session_host_restore_hydrates_calibration_only_on_fingerprint_match(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """The lifecycle restore path must hydrate strategy calibration through the
    provenance gate — the record survives save/restore and a fingerprint
    mismatch leaves the fresh strategy untouched."""
    clients = [MockChatClient(responses=[MockResponse(text=f"response {n}")]) for n in range(3)]
    _patch_runtime(monkeypatch, clients, [])
    settings, model_registry = _make_settings_and_models()
    store = JsonFileStateStore(tmp_path / "sessions")
    agent_registry = _make_agent_registry(_make_profile(approval_default="skip"))
    # Restore re-derives settings from the environment, so the model choice
    # must live in a derivable layer — the constructor ``settings=`` has no
    # provenance to survive the re-load. The env pointer is how the active
    # model rides across restores in production.
    monkeypatch.setenv("CHRYS_MODEL_PROFILE", "mock-profile")

    first = ChrysSessionHost(
        profile_name="Headless",
        settings=settings,
        agent_registry=agent_registry,
        model_registry=model_registry,
        state_store=store,
    )
    try:
        first_result = await first.run_until_final("first prompt", timeout=45)
        session_id = first_result.session_id
        # Record after one real turn, then persist through the same save path
        # used at turn end (production records this mid-run via publish_usage).
        first.engine.session.runtime_meta.record_context_calibration(
            system_overhead_tokens=17,
            calibration_ratio=1.25,
            model_profile_fingerprint=first.engine.current.manifest.model_profile_fingerprint,
            agent_profile_fingerprint=first.engine.current.manifest.agent_profile_fingerprint,
        )
        assert await first.engine.writer.save_current_session(raise_on_error=True)
    finally:
        await first.shutdown()

    second = ChrysSessionHost(
        profile_name="Headless",
        session_id=session_id,
        settings=settings,
        agent_registry=agent_registry,
        model_registry=model_registry,
        state_store=store,
    )
    try:
        await second.start()
        strategy = second.engine.current.loaded.bindings.backend.compaction_strategy
        assert strategy is not None
        assert strategy.calibration_initialized
        assert strategy.system_overhead_tokens == 17
        assert strategy.calibration_ratio == 1.25
        # Tamper the stored provenance (and persist it) so the next restore
        # must skip hydration.
        second.engine.session.runtime_meta.context_calibration["model_profile_fingerprint"] = "stale"
        assert await second.engine.writer.save_current_session(raise_on_error=True)
    finally:
        await second.shutdown()

    third = ChrysSessionHost(
        profile_name="Headless",
        session_id=session_id,
        settings=settings,
        agent_registry=agent_registry,
        model_registry=model_registry,
        state_store=store,
    )
    try:
        await third.start()
        strategy = third.engine.current.loaded.bindings.backend.compaction_strategy
        assert strategy is not None
        assert not strategy.calibration_initialized
    finally:
        await third.shutdown()


@pytest.mark.asyncio
async def test_session_host_restore_startup_failure_raises_without_hanging(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    first_client = MockChatClient(responses=[MockResponse(text="first response")])
    _patch_runtime(monkeypatch, [first_client], [])
    settings, model_registry = _make_settings_and_models()
    store = JsonFileStateStore(tmp_path / "sessions")
    agent_registry = _make_agent_registry(_make_profile(approval_default="skip"))

    first = ChrysSessionHost(
        profile_name="Headless",
        settings=settings,
        agent_registry=agent_registry,
        model_registry=model_registry,
        state_store=store,
    )
    try:
        first_result = await first.run_until_final("first prompt", timeout=45)
        session_id = first_result.session_id
    finally:
        await first.shutdown()

    async def _raise_create_client(_settings: Any = None, **_kwargs: Any) -> MockChatClient:
        msg = "restore client boom"
        raise RuntimeError(msg)

    monkeypatch.setattr(builder_module, "create_client", _raise_create_client)
    second = ChrysSessionHost(
        profile_name="Headless",
        session_id=session_id,
        settings=settings,
        agent_registry=agent_registry,
        model_registry=model_registry,
        state_store=store,
    )
    events: list[Any] = []

    async def _collect() -> None:
        async for event in second.iter_run_events("second prompt"):
            events.append(event)

    try:
        with pytest.raises(RuntimeError, match="restore client boom"):
            await asyncio.wait_for(_collect(), timeout=45)
    finally:
        await second.shutdown()

    assert any(isinstance(event, AgentLoadFailed) and "restore client boom" in event.message for event in events)


@pytest.mark.asyncio
async def test_session_host_restores_existing_session_from_short_id(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    first_client = MockChatClient(responses=[MockResponse(text="first response")])
    second_client = MockChatClient(responses=[MockResponse(text="second response")])
    _patch_runtime(monkeypatch, [first_client, second_client], [])
    settings, model_registry = _make_settings_and_models()
    store = JsonFileStateStore(tmp_path / "sessions")
    agent_registry = _make_agent_registry(_make_profile(approval_default="skip"))

    first = ChrysSessionHost(
        profile_name="Headless",
        settings=settings,
        agent_registry=agent_registry,
        model_registry=model_registry,
        state_store=store,
    )
    try:
        first_result = await first.run_until_final("first prompt", timeout=45)
        session_id = first_result.session_id
    finally:
        await first.shutdown()

    second = ChrysSessionHost(
        profile_name="Headless",
        session_id=session_short_id(session_id),
        settings=settings,
        agent_registry=agent_registry,
        model_registry=model_registry,
        state_store=store,
    )
    try:
        second_result = await second.run_until_final("second prompt", timeout=45)
    finally:
        await second.shutdown()

    assert second_result.session_id == session_id


@pytest.mark.asyncio
async def test_session_host_restore_respects_explicit_cwd(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    original_cwd = tmp_path / "original"
    override_cwd = tmp_path / "override"
    original_cwd.mkdir()
    override_cwd.mkdir()
    monkeypatch.chdir(original_cwd)

    first_client = MockChatClient(responses=[MockResponse(text="first response")])
    second_client = MockChatClient(responses=[MockResponse(text="second response")])
    _patch_runtime(monkeypatch, [first_client, second_client], [])
    settings, model_registry = _make_settings_and_models()
    store = JsonFileStateStore(tmp_path / "sessions")
    agent_registry = _make_agent_registry(_make_profile(approval_default="skip"))

    first = ChrysSessionHost(
        profile_name="Headless",
        settings=settings,
        agent_registry=agent_registry,
        model_registry=model_registry,
        state_store=store,
    )
    try:
        first_result = await first.run_until_final("first prompt", timeout=45)
        session_id = first_result.session_id
    finally:
        await first.shutdown()

    legacy_workdir = tmp_path / "legacy-workdir"
    legacy_workdir.mkdir()
    state = await store.load_session(session_id)
    assert state is not None
    await store.save_session(
        session_id,
        state,
        agent_profile="Headless",
        primary_cwd=str(original_cwd),
        working_dirs=[str(legacy_workdir)],
    )

    monkeypatch.chdir(tmp_path)
    second = ChrysSessionHost(
        profile_name="Headless",
        session_id=session_id,
        cwd=str(override_cwd),
        settings=settings,
        agent_registry=agent_registry,
        model_registry=model_registry,
        state_store=store,
    )
    try:
        second_result = await second.run_until_final("second prompt", timeout=45)
    finally:
        await second.shutdown()

    metas = await store.list_sessions()
    restored_meta = next(meta for meta in metas if meta.session_id == session_id)
    assert second_result.session_id == session_id
    assert Path.cwd() == tmp_path
    assert restored_meta.primary_cwd == str(override_cwd)
    assert restored_meta.working_dirs == []


@pytest.mark.asyncio
async def test_session_host_restore_preserves_working_dirs_when_cwd_matches(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    original_cwd = tmp_path / "original"
    original_cwd.mkdir()
    monkeypatch.chdir(original_cwd)

    first_client = MockChatClient(responses=[MockResponse(text="first response")])
    second_client = MockChatClient(responses=[MockResponse(text="second response")])
    _patch_runtime(monkeypatch, [first_client, second_client], [])
    settings, model_registry = _make_settings_and_models()
    store = JsonFileStateStore(tmp_path / "sessions")
    agent_registry = _make_agent_registry(_make_profile(approval_default="skip"))

    first = ChrysSessionHost(
        profile_name="Headless",
        settings=settings,
        agent_registry=agent_registry,
        model_registry=model_registry,
        state_store=store,
    )
    try:
        first_result = await first.run_until_final("first prompt", timeout=45)
        session_id = first_result.session_id
    finally:
        await first.shutdown()

    legacy_workdir = tmp_path / "legacy-workdir"
    legacy_workdir.mkdir()
    state = await store.load_session(session_id)
    assert state is not None
    await store.save_session(
        session_id,
        state,
        agent_profile="Headless",
        primary_cwd=str(original_cwd),
        working_dirs=[str(legacy_workdir)],
    )

    monkeypatch.chdir(tmp_path)
    second = ChrysSessionHost(
        profile_name="Headless",
        session_id=session_id,
        cwd=str(original_cwd),
        settings=settings,
        agent_registry=agent_registry,
        model_registry=model_registry,
        state_store=store,
    )
    try:
        second_result = await second.run_until_final("second prompt", timeout=45)
    finally:
        await second.shutdown()

    metas = await store.list_sessions()
    restored_meta = next(meta for meta in metas if meta.session_id == session_id)
    assert second_result.session_id == session_id
    assert Path.cwd() == tmp_path
    assert restored_meta.primary_cwd == str(original_cwd)
    assert restored_meta.working_dirs == [str(legacy_workdir)]


@pytest.mark.asyncio
async def test_session_host_restore_honors_request_time_additional_dirs(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:

    original_cwd = tmp_path / "original"
    original_cwd.mkdir()
    monkeypatch.chdir(original_cwd)

    first_client = MockChatClient(responses=[MockResponse(text="first response")])
    second_client = MockChatClient(responses=[MockResponse(text="second response")])
    _patch_runtime(monkeypatch, [first_client, second_client], [])
    settings, model_registry = _make_settings_and_models()
    store = JsonFileStateStore(tmp_path / "sessions")
    agent_registry = _make_agent_registry(_make_profile(approval_default="skip"))

    first = ChrysSessionHost(
        profile_name="Headless",
        settings=settings,
        agent_registry=agent_registry,
        model_registry=model_registry,
        state_store=store,
    )
    try:
        session_id = (await first.run_until_final("first prompt", timeout=45)).session_id
    finally:
        await first.shutdown()

    saved_workdir = tmp_path / "saved"
    saved_workdir.mkdir()
    extra_workdir = tmp_path / "extra"
    extra_workdir.mkdir()
    state = await store.load_session(session_id)
    assert state is not None
    await store.save_session(
        session_id,
        state,
        agent_profile="Headless",
        primary_cwd=str(original_cwd),
        working_dirs=[str(saved_workdir)],
    )

    # Client-supplied additionalDirectories are authoritative on load (ACP): they
    # REPLACE the saved roots, so the request-time extra dir is active and the saved
    # dir is dropped (the client narrowed/swapped scope).
    workspace = Workspace(
        primary_cwd=str(original_cwd),
        working_dirs=[
            WorkingDir(path=str(original_cwd), is_primary=True),
            WorkingDir(path=str(extra_workdir)),
        ],
    )
    second = ChrysSessionHost(
        profile_name="Headless",
        session_id=session_id,
        cwd=str(original_cwd),
        settings=settings,
        agent_registry=agent_registry,
        model_registry=model_registry,
        state_store=store,
        workspace=workspace,
    )
    try:
        await second.run_until_final("second prompt", timeout=45)
        restored_dirs = second.engine.workspace.working_dirs
        restored_paths = [d.path for d in restored_dirs]
    finally:
        await second.shutdown()

    assert str(extra_workdir) in restored_paths
    assert str(saved_workdir) not in restored_paths
    # Primary stays first so an unchanged scope still compares equal for service reuse.
    assert restored_paths[0] == str(original_cwd)
    assert restored_dirs[0].is_primary is True


@pytest.mark.asyncio
async def test_session_host_restore_preserves_saved_primary_in_working_dirs(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    # ACP's _workspace_from_dirs stores the primary cwd inside working_dirs, so
    # meta.working_dirs is [primary, extra]. Restore must keep that list verbatim
    # (when reloading with the same dirs) or _can_restore_service_session's exact
    # path comparison breaks and an OpenAI Responses session is wrongly discarded.

    original_cwd = tmp_path / "original"
    original_cwd.mkdir()
    monkeypatch.chdir(original_cwd)

    first_client = MockChatClient(responses=[MockResponse(text="first response")])
    second_client = MockChatClient(responses=[MockResponse(text="second response")])
    _patch_runtime(monkeypatch, [first_client, second_client], [])
    settings, model_registry = _make_settings_and_models()
    store = JsonFileStateStore(tmp_path / "sessions")
    agent_registry = _make_agent_registry(_make_profile(approval_default="skip"))

    first = ChrysSessionHost(
        profile_name="Headless",
        settings=settings,
        agent_registry=agent_registry,
        model_registry=model_registry,
        state_store=store,
    )
    try:
        session_id = (await first.run_until_final("first prompt", timeout=45)).session_id
    finally:
        await first.shutdown()

    extra_workdir = tmp_path / "extra"
    extra_workdir.mkdir()
    saved_dirs = [str(original_cwd), str(extra_workdir)]
    state = await store.load_session(session_id)
    assert state is not None
    await store.save_session(
        session_id,
        state,
        agent_profile="Headless",
        primary_cwd=str(original_cwd),
        working_dirs=saved_dirs,
    )

    workspace = Workspace(
        primary_cwd=str(original_cwd),
        working_dirs=[
            WorkingDir(path=str(original_cwd), is_primary=True),
            WorkingDir(path=str(extra_workdir)),
        ],
    )
    second = ChrysSessionHost(
        profile_name="Headless",
        session_id=session_id,
        cwd=str(original_cwd),
        settings=settings,
        agent_registry=agent_registry,
        model_registry=model_registry,
        state_store=store,
        workspace=workspace,
    )
    try:
        await second.run_until_final("second prompt", timeout=45)
        restored_dirs = second.engine.workspace.working_dirs
        restored_paths = [d.path for d in restored_dirs]
    finally:
        await second.shutdown()

    assert restored_paths == saved_dirs
    assert restored_dirs[0].is_primary is True


@pytest.mark.asyncio
async def test_session_host_restore_clears_saved_dirs_with_explicit_empty(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    # An explicit empty additionalDirectories (a workspace with only the primary) is
    # authoritative: it clears the saved additional roots, distinct from omitting the
    # field (which keeps them).

    original_cwd = tmp_path / "original"
    original_cwd.mkdir()
    monkeypatch.chdir(original_cwd)

    first_client = MockChatClient(responses=[MockResponse(text="first response")])
    second_client = MockChatClient(responses=[MockResponse(text="second response")])
    _patch_runtime(monkeypatch, [first_client, second_client], [])
    settings, model_registry = _make_settings_and_models()
    store = JsonFileStateStore(tmp_path / "sessions")
    agent_registry = _make_agent_registry(_make_profile(approval_default="skip"))

    first = ChrysSessionHost(
        profile_name="Headless",
        settings=settings,
        agent_registry=agent_registry,
        model_registry=model_registry,
        state_store=store,
    )
    try:
        session_id = (await first.run_until_final("first prompt", timeout=45)).session_id
    finally:
        await first.shutdown()

    saved_extra = tmp_path / "extra"
    saved_extra.mkdir()
    state = await store.load_session(session_id)
    assert state is not None
    await store.save_session(
        session_id,
        state,
        agent_profile="Headless",
        primary_cwd=str(original_cwd),
        working_dirs=[str(original_cwd), str(saved_extra)],
    )

    # Workspace with only the primary == client sent additionalDirectories: [].
    cleared_workspace = Workspace(
        primary_cwd=str(original_cwd),
        working_dirs=[WorkingDir(path=str(original_cwd), is_primary=True)],
    )
    second = ChrysSessionHost(
        profile_name="Headless",
        session_id=session_id,
        cwd=str(original_cwd),
        settings=settings,
        agent_registry=agent_registry,
        model_registry=model_registry,
        state_store=store,
        workspace=cleared_workspace,
    )
    try:
        await second.run_until_final("second prompt", timeout=45)
        restored_dirs = second.engine.workspace.working_dirs
        restored_paths = [d.path for d in restored_dirs]
    finally:
        await second.shutdown()

    assert str(saved_extra) not in restored_paths
    assert restored_paths == [str(original_cwd)]
    assert restored_dirs[0].is_primary is True


@pytest.mark.asyncio
async def test_session_host_restore_respects_explicit_profile(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    first_client = MockChatClient(responses=[MockResponse(text="first response")])
    second_client = MockChatClient(responses=[MockResponse(text="second response")])
    _patch_runtime(monkeypatch, [first_client, second_client], [])
    settings, model_registry = _make_settings_and_models()
    store = JsonFileStateStore(tmp_path / "sessions")
    saved_profile = _make_profile(approval_default="skip")
    override_profile = AgentProfile(
        name="Override",
        instructions="You are the override test assistant.",
        tools=ToolsConfig(builtins=[]),
        approval=ApprovalConfig(default="skip", overrides={}),
        compaction=CompactionConfig(enabled=False),
    )
    agent_registry = _make_agent_registry_many(saved_profile, override_profile)

    first = ChrysSessionHost(
        profile_name="Headless",
        settings=settings,
        agent_registry=agent_registry,
        model_registry=model_registry,
        state_store=store,
    )
    try:
        first_result = await first.run_until_final("first prompt", timeout=45)
        session_id = first_result.session_id
    finally:
        await first.shutdown()

    second = ChrysSessionHost(
        profile_name="Override",
        session_id=session_id,
        settings=settings,
        agent_registry=agent_registry,
        model_registry=model_registry,
        state_store=store,
    )
    try:
        second_result = await second.run_until_final("second prompt", timeout=45)
    finally:
        await second.shutdown()

    metas = await store.list_sessions()
    restored_meta = next(meta for meta in metas if meta.session_id == session_id)
    state = await store.load_session(session_id)
    switches = state.get("agent_profile_switches", []) if state else []
    user_messages = [message for message in (state or {}).get("messages", []) if message.role == "user"]
    assert second_result.session_id == session_id
    assert restored_meta.agent_profile == "Override"
    assert restored_meta.agent_profile_history[-1] == "Override"
    assert second.engine.agent_profile is not None
    assert second.engine.agent_profile.name == "Override"
    assert switches[-1]["from"] == "Headless"
    assert switches[-1]["to"] == "Override"
    assert user_messages[-1].additional_properties.get(HistoryMarkerKind.PROFILE_SWITCH_TO_KEY) == "Override"


@pytest.mark.asyncio
async def test_session_host_reports_missing_restore_session(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    first_client = MockChatClient(responses=[MockResponse(text="first response")])
    _patch_runtime(monkeypatch, [first_client], [])
    settings, model_registry = _make_settings_and_models()
    store = JsonFileStateStore(tmp_path / "sessions")
    agent_registry = _make_agent_registry(_make_profile(approval_default="skip"))

    first = ChrysSessionHost(
        profile_name="Headless",
        settings=settings,
        agent_registry=agent_registry,
        model_registry=model_registry,
        state_store=store,
    )
    try:
        first_result = await first.run_until_final("first prompt", timeout=45)
    finally:
        await first.shutdown()

    session_id = first_result.session_id
    missing_id = session_id[:-1] + ("0" if session_id[-1] != "0" else "1")
    primary = store.session_dir(session_id) / "session.json"
    primary.unlink()
    before = {path: path.read_bytes() for path in store.sessions_root.rglob("*.json")}
    missing = ChrysSessionHost(
        profile_name="Headless",
        session_id=missing_id,
        settings=settings,
        agent_registry=agent_registry,
        model_registry=model_registry,
        state_store=store,
    )
    try:
        with pytest.raises(SessionNotFoundError) as exc_info:
            await missing.start()
    finally:
        await missing.shutdown()

    assert {path: path.read_bytes() for path in store.sessions_root.rglob("*.json")} == before
    error = exc_info.value
    assert str(error) == f"'Session not found: {missing_id}'"
    assert isinstance(error.display_message, MessageRef)
    assert error.display_message.definition is session_host_module._SESSION_NOT_FOUND
    assert dict(error.display_message.args) == {"session_id": missing_id}


@pytest.mark.asyncio
async def test_session_host_restore_requires_existing_explicit_profile(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    first_client = MockChatClient(responses=[MockResponse(text="first response")])
    clients = [first_client]
    _patch_runtime(monkeypatch, clients, [])
    settings, model_registry = _make_settings_and_models()
    store = JsonFileStateStore(tmp_path / "sessions")
    agent_registry = _make_agent_registry(_make_profile(approval_default="skip"))

    first = ChrysSessionHost(
        profile_name="Headless",
        settings=settings,
        agent_registry=agent_registry,
        model_registry=model_registry,
        state_store=store,
    )
    try:
        first_result = await first.run_until_final("first prompt", timeout=45)
    finally:
        await first.shutdown()
    assert clients == []

    missing_profile = ChrysSessionHost(
        profile_name="Missing",
        session_id=first_result.session_id,
        settings=settings,
        agent_registry=agent_registry,
        model_registry=model_registry,
        state_store=store,
    )
    try:
        with pytest.raises(AgentProfileNotFoundError, match="Agent profile not found: Missing"):
            await missing_profile.run_until_final("should not run", timeout=45)
        assert clients == []
    finally:
        await missing_profile.shutdown()


@pytest.mark.parametrize(
    "session_id", ["12345678-abcd-4111-8222-123456789abc", "12345678abcd", "ABCDEFAB-ABCD-4111-8222-123456789ABC"]
)
async def test_explicit_startup_matches_the_stored_identity(tmp_path, session_id: str) -> None:
    store = JsonFileStateStore(tmp_path / "sessions")
    await store.save_session(session_id, {"messages": []})
    compact = session_id.replace("-", "")
    for selector in (session_id, compact.upper(), compact[:12].upper()):
        meta = await store.resolve_startup_session(selector)
        assert meta is not None and meta.session_id == session_id
    assert await store.resolve_startup_session(compact[:12] + "0" * 20) is None
    assert await store.resolve_startup_session(compact[:8]) is None


@pytest.mark.parametrize("source", ["primary", "backup", "legacy"])
async def test_host_rejects_checkpoint_replaced_after_resolution(tmp_path, monkeypatch, source: str) -> None:
    settings, model_registry = _make_settings_and_models()
    store = JsonFileStateStore(tmp_path / "sessions")
    session_id = "12345678-abcd-4111-8222-123456789abc"
    await store.save_session(session_id, {"messages": []}, agent_profile="Headless")
    primary = store.session_dir(session_id) / "session.json"
    backup = primary.with_suffix(".json.bak")
    legacy = store.sessions_root / "12345678abcd.json"
    replacement = json.loads(primary.read_text(encoding="utf-8"))
    replacement["meta"].update(session_id=session_id[:-1] + "0", updated_at="invalid")
    payload = json.dumps(replacement)
    replacement_path = {"primary": primary, "backup": backup, "legacy": legacy}[source]
    resolve = store.resolve_startup_session

    async def replace_after_resolution(selector: str, *, require_inactive: bool = False):
        meta = await resolve(selector, require_inactive=require_inactive)
        primary.unlink()
        backup.unlink()
        replacement_path.write_text(payload, encoding="utf-8")
        return meta

    monkeypatch.setattr(store, "resolve_startup_session", replace_after_resolution)
    host = ChrysSessionHost(
        profile_name="Headless",
        session_id=session_id,
        settings=settings,
        agent_registry=_make_agent_registry(_make_profile(approval_default="skip")),
        model_registry=model_registry,
        state_store=store,
    )
    try:
        with pytest.raises(HeadlessRunError) as rejected:
            await host.start()
        assert rejected.value.event.code == "session_not_found"
        assert host.engine.session.session_id is None
    finally:
        await host.shutdown()

    existing = {path: path.read_text(encoding="utf-8") for path in (primary, backup, legacy) if path.exists()}
    assert existing == {replacement_path: payload}


@pytest.mark.parametrize("flat_target", [False, True])
@pytest.mark.parametrize("require_inactive", [False, True])
async def test_exact_startup_skips_unrelated_flat_archives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, flat_target: bool, require_inactive: bool
) -> None:
    store = JsonFileStateStore(tmp_path)
    identity = "12345678-abcd-4111-8222-123456789abc"
    await store.save_session(identity, {}, title="Target")
    primary = store.session_dir(identity) / "session.json"
    unrelated = [tmp_path / "987654321abc.json", tmp_path / "imported.json"]
    for path in unrelated:
        path.write_text(primary.read_text().replace(identity, "987654321abc"))
    if flat_target:
        primary.replace(tmp_path / "12345678abcd.json")
        primary.with_suffix(".json.bak").unlink()
    original = Path.open

    def open_related(path: Path, mode="r", buffering=-1, encoding=None, errors=None, newline=None):
        assert path not in unrelated, f"Opened unrelated flat archive {path}"
        return original(path, mode, buffering, encoding, errors, newline)

    monkeypatch.setattr(Path, "open", open_related)
    resolved = await store.resolve_startup_session(identity.upper(), require_inactive=require_inactive)
    assert resolved is not None and resolved.session_id == identity


@pytest.mark.parametrize("legacy_name", ["12345678abcd.json", "imported.json", "noncanonical-imported-archive.json"])
async def test_ambiguous_startup_preserves_backup_and_legacy(tmp_path, legacy_name: str) -> None:
    store = JsonFileStateStore(tmp_path)
    identity = "12345678-abcd-4111-8222-123456789abc"
    other = identity[:-1] + "0"
    await store.save_session(identity, {"messages": []})
    primary = store.session_dir(identity) / "session.json"
    backup = primary.with_suffix(".json.bak")
    primary.replace(backup)
    legacy = tmp_path / legacy_name
    legacy.write_text(backup.read_text().replace(identity, other))
    with pytest.raises(AmbiguousSessionIdError):
        await store.resolve_startup_session("12345678ABCD")
    assert legacy.is_file() and backup.is_file() and not primary.exists()
    with pytest.raises(ValueError, match="stored location"):
        await store.resolve_startup_session(other)
    assert (await store.resolve_startup_session(identity)).session_id == identity
    assert await store.load_session(identity) is not None
    assert json.loads(primary.read_text())["meta"]["session_id"] == identity
    assert legacy.is_file()


@pytest.mark.parametrize("alias_style", ["compact", "uppercase"])
async def test_startup_deduplicates_uuid_spellings_in_same_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, alias_style: str
) -> None:
    store = JsonFileStateStore(tmp_path)
    identity = "12345678-abcd-4111-8222-123456789abc"
    alias = identity.replace("-", "") if alias_style == "compact" else identity.upper()
    short = session_short_id(identity)
    await store.save_session(identity, {"messages": []}, title="Primary")
    primary = store.session_dir(identity) / "session.json"
    envelope = json.loads(primary.read_bytes())
    envelope["meta"].update(session_id=alias, title="Legacy copy")
    legacy = tmp_path / f"{short}.json"
    legacy.write_text(json.dumps(envelope), encoding="utf-8")

    inspected: list[Path] = []
    scan = store._startup_folder_meta

    def track_folder(name: str, selector: str):
        inspected.append(tmp_path / name)
        return scan(name, selector)

    monkeypatch.setattr(store, "_startup_folder_meta", track_folder)
    for require_inactive in (False, True):
        for selector in (short.upper(), alias.upper()):
            inspected.clear()
            meta = await store.resolve_startup_session(selector, require_inactive=require_inactive)
            assert meta is not None
            assert (meta.session_id, meta.title) == (identity, "Primary")
            [directory] = inspected
            assert directory.samefile(primary.parent)
            assert legacy.is_file()


@pytest.mark.parametrize("backup", ["{", "[]", "{}", '{"meta":{"session_id":[]},"state":{}}'])
async def test_startup_loads_canonical_legacy_with_duplicate_and_damaged_backup(tmp_path, backup: str) -> None:
    store = JsonFileStateStore(tmp_path)
    identity = "abcdef123456"
    await store.save_session(identity, {"messages": []})
    primary = store.session_dir(identity) / "session.json"
    legacy = tmp_path / f"{identity}.json"
    primary.replace(legacy)
    primary.with_suffix(".json.bak").write_text(backup)
    (tmp_path / "aaa.json").write_bytes(legacy.read_bytes())
    assert (await store.resolve_startup_session(identity.upper())).session_id == identity
    assert await store.load_session(identity) is not None
    assert primary.is_file() and not legacy.exists()


@pytest.mark.parametrize("recovery", [None, "{", "{}", "stale"])
async def test_explicit_startup_prefers_newer_legacy_checkpoint_of_same_session(tmp_path, recovery) -> None:
    store = JsonFileStateStore(tmp_path)
    identity = "12345678-abcd-4111-8222-123456789abc"
    await store.save_session(identity, {"messages": []}, title="Current legacy title")
    primary = store.session_dir(identity) / "session.json"
    checkpoint = json.loads(primary.read_text(encoding="utf-8"))
    checkpoint["meta"].update(
        session_id=identity.replace("-", ""), title="Old backup title", updated_at="2025-01-01T00:00:00Z"
    )
    primary.with_suffix(".json.bak").write_text(json.dumps(checkpoint), encoding="utf-8")
    legacy = tmp_path / "12345678abcd.json"
    primary.replace(legacy)
    legacy_data = json.loads(legacy.read_bytes())
    legacy_data["meta"]["updated_at"] = "2025-03-01T00:00:00Z"
    legacy.write_text(json.dumps(legacy_data), encoding="utf-8")
    if recovery is not None:
        sidecar = json.dumps(checkpoint) if recovery == "stale" else recovery
        primary.with_name("session.recovery.json").write_text(sidecar, encoding="utf-8")

    for require_inactive in (False, True):
        assert (await store.resolve_startup_session(identity, require_inactive=require_inactive)).title == (
            "Current legacy title"
        )
    with FileLock(store.active_lock_path(identity), timeout=0):
        if recovery in {None, "stale"}:
            assert (await store.list_sessions())[0].title == "Old backup title"
            assert legacy.is_file() and not primary.exists()
        assert await store.load_session(identity, prefer_recovery=True, owns_active_lock=True) is not None
    assert json.loads(primary.read_text(encoding="utf-8"))["meta"]["title"] == "Current legacy title"


async def test_conflicting_primary_and_recovery_are_never_opened_by_explicit_startup(tmp_path) -> None:
    store = JsonFileStateStore(tmp_path)
    primary_id = "12345678-abcd-4111-8222-123456789abc"
    recovery_id = primary_id[:-1] + "0"
    await store.save_session(primary_id, {"messages": []})
    store.save_recovery_session(recovery_id, {"messages": []})
    directory = store.session_dir(primary_id)
    primary = directory / "session.json"
    recovery = directory / "session.recovery.json"
    before = (primary.read_bytes(), recovery.read_bytes())

    with pytest.raises(AmbiguousSessionIdError):
        await store.resolve_startup_session("12345678ABCD")
    for identity in (primary_id, recovery_id):
        with pytest.raises(ConflictingSessionCheckpointsError):
            await store.resolve_startup_session(identity)
        with FileLock(store.active_lock_path(identity), timeout=0), pytest.raises(ConflictingSessionCheckpointsError):
            await store.load_session(identity, prefer_recovery=True)
    assert await store.resolve_startup_session(primary_id[:-1] + "1") is None
    assert (primary.read_bytes(), recovery.read_bytes()) == before


async def test_latest_chat_uses_canonical_id_for_existing_mru_alias(tmp_path) -> None:
    store = JsonFileStateStore(tmp_path)
    canonical = "12345678abcd41118222123456789abc"
    await store.save_session(canonical, {"messages": []})
    assert await store.load_latest_session_id() == canonical
    meta = await store.load_session_meta(canonical)
    store._mru.record("12345678abcd", meta.updated_at)

    assert await store.load_latest_session_id(chat_only=True) == canonical


@pytest.mark.parametrize("identity", ["12345678abcd", "12345678-abcd-4111-8222-123456789abc"])
async def test_startup_find_legacy_archive_migrated_during_lookup(tmp_path, monkeypatch, identity: str) -> None:
    store = JsonFileStateStore(tmp_path)
    await store.save_session(identity, {"messages": []})
    primary = store.session_dir(identity) / "session.json"
    primary.replace(tmp_path / f"{session_short_id(identity)}.json")
    primary.with_suffix(".json.bak").unlink(missing_ok=True)
    primary.parent.rmdir()
    scan_legacy = store._legacy_session_files

    def migrate_before_scan():
        paths = scan_legacy()
        store.resolve_session_file(identity)
        return paths

    monkeypatch.setattr(store, "_legacy_session_files", migrate_before_scan)
    assert (await store.resolve_startup_session(identity)).session_id == identity


async def test_legacy_migration_leaves_busy_session_with_damaged_recovery_alone(tmp_path) -> None:
    store = JsonFileStateStore(tmp_path)
    active = "12345678-abcd-4111-8222-123456789abc"
    legacy_id = "12345678-abcd-4111-8222-123456789ab0"
    await store.save_session(legacy_id, {"messages": []})
    directory = store.session_dir(active)
    primary = directory / "session.json"
    legacy = tmp_path / "12345678abcd.json"
    primary.replace(legacy)
    primary.with_suffix(".json.bak").unlink(missing_ok=True)
    (directory / "session.recovery.json").write_text("{", encoding="utf-8")

    with FileLock(store.active_lock_path(active), timeout=0):
        assert await store.load_session_meta(active) is None
        assert legacy.is_file() and not primary.exists()


async def test_explicit_startup_excludes_logically_deleted_session(tmp_path) -> None:
    from chrys.service.trajectory.tombstone import INTENT_SUFFIX, tombstones_dir

    store = JsonFileStateStore(tmp_path)
    identity = "ABCDEF12-3456-4111-8222-123456789ABC"
    await store.save_session(identity, {"messages": []})
    directory = store.session_dir(identity)
    graveyard = tombstones_dir(tmp_path)
    graveyard.mkdir()
    (graveyard / f"{directory.name}{INTENT_SUFFIX}").write_text(directory.name, encoding="utf-8")

    assert (directory / "session.json").is_file()
    assert await store.resolve_startup_session("abcdef123456") is None
    assert await store.resolve_startup_session(identity.lower()) is None
    # Deletion between selecting the identity and acquiring its lock is final too.
    assert await store.load_session(identity) is None


@pytest.mark.parametrize("invalid_id", [[], "12345678abcd/invalid"], ids=["non-string", "invalid-suffix"])
async def test_explicit_startup_ignores_invalid_legacy_identity(tmp_path, invalid_id: object) -> None:
    store = JsonFileStateStore(tmp_path)
    identity = "12345678-abcd-4111-8222-123456789abc"
    await store.save_session(identity, {"messages": []})
    raw = json.loads((store.session_dir(identity) / "session.json").read_text(encoding="utf-8"))
    raw["meta"]["session_id"] = invalid_id
    (tmp_path / "unrelated.json").write_text(json.dumps(raw), encoding="utf-8")

    assert (await store.resolve_startup_session("12345678abcd")).session_id == identity


async def test_unrelated_json_array_does_not_block_startup_restore(tmp_path) -> None:
    store = JsonFileStateStore(tmp_path)
    identity = "12345678-abcd-4111-8222-123456789abc"
    await store.save_session(identity, {"messages": []})
    (tmp_path / "unrelated.json").write_text("[]", encoding="utf-8")

    assert (await store.resolve_startup_session(identity)).session_id == identity


async def test_startup_preflight_detects_busy_primary_checkpoint(tmp_path) -> None:
    store = JsonFileStateStore(tmp_path)
    identity = "12345678-abcd-4111-8222-123456789abc"
    await store.save_session(identity, {"messages": []}, primary_cwd=str(tmp_path / "missing"))
    with FileLock(store.active_lock_path(identity), timeout=0), pytest.raises(StartupSessionInUseError):
        await store.resolve_startup_session("12345678ABCD", require_inactive=True)
    assert (await store.resolve_startup_session(identity, require_inactive=True)).session_id == identity


@pytest.mark.parametrize("recovery_spelling", ["same", "compact", "uppercase"])
async def test_startup_preflight_reads_workspace_after_active_lock_handoff(
    tmp_path, monkeypatch, recovery_spelling
) -> None:
    store = JsonFileStateStore(tmp_path)
    identity = "12345678-abcd-4111-8222-123456789abc"
    await store.save_session(identity, {"messages": []}, primary_cwd=str(tmp_path))
    recovery_cwd = tmp_path / "recovery-workspace"
    recovery_id = {"same": identity, "compact": identity.replace("-", ""), "uppercase": identity.upper()}[
        recovery_spelling
    ]
    store.save_recovery_session(identity, {"messages": []}, primary_cwd=str(recovery_cwd))
    directory = store.session_dir(identity)
    primary = directory / "session.json"
    recovery = directory / "session.recovery.json"
    envelope = json.loads(recovery.read_bytes())
    envelope["meta"]["session_id"] = recovery_id
    envelope["meta"]["updated_at"] = "2099-01-01T00:00:00Z"
    recovery.write_text(json.dumps(envelope), encoding="utf-8")
    before = (primary.read_bytes(), recovery.read_bytes())
    active_path = store.active_lock_path(identity)
    acquire = FileLock.acquire

    with FileLock(active_path, timeout=0) as owner:

        def handoff_before_wait(lock: FileLock) -> None:
            if lock._path == active_path and lock._timeout:
                owner.release()
            acquire(lock)

        monkeypatch.setattr(FileLock, "acquire", handoff_before_wait)
        meta = await store.resolve_startup_session("12345678ABCD", require_inactive=True)

    recovery_dir = store.session_dir(recovery_id)
    if recovery_dir.exists() and directory.samefile(recovery_dir):
        assert meta is not None
        assert meta.session_id == recovery_id
        assert meta.primary_cwd == str(recovery_cwd)
    else:
        assert meta is None
    with FileLock(active_path, timeout=0):
        assert (primary.read_bytes(), recovery.read_bytes()) == before


@pytest.mark.parametrize(
    ("identity", "other"),
    [
        ("12345678-abcd-4111-8222-123456789abc", "12345678-abcd-4111-8222-123456789abd"),
        ("opaque-archive", "opaquearchive"),
    ],
)
async def test_uuid_aliases_do_not_relabel_distinct_archives(tmp_path: Path, identity: str, other: str) -> None:
    store = JsonFileStateStore(tmp_path)
    await store.save_session(identity, {"messages": []}, title="Original")
    primary = store.session_dir(identity) / "session.json"
    before = primary.read_bytes()
    assert await store.resolve_startup_session(other) is None
    assert await store.update_session_titles(other, generated_title="Wrong archive") is None
    assert primary.read_bytes() == before


async def test_save_after_alias_recovery_keeps_title_and_fork_metadata(tmp_path: Path) -> None:
    store = JsonFileStateStore(tmp_path)
    identity = "12345678-abcd-4111-8222-123456789abc"
    alias = identity.replace("-", "")
    parent = "abcdef12-3456-4111-8222-123456789abc"
    await store.save_session(identity, {"messages": []}, parent_session_id=parent)
    directory = store.session_dir(identity)
    primary = directory / "session.json"
    recovery = directory / "session.recovery.json"
    created = json.loads(primary.read_bytes())["meta"]["created_at"]
    store.save_recovery_session(alias, {"messages": [Message("user", ["continued"])]})
    meta = await store.resolve_startup_session(session_short_id(identity), require_inactive=True)
    assert meta is not None and meta.session_id == alias
    with FileLock(store.active_lock_path(alias), timeout=0):
        restored = await store.load_chat_for_restore(alias, prefer_recovery=True)
        assert restored is not None and restored.recovered_from_sidecar
        await store.update_session_titles(alias, custom_title="Renamed")
        for checkpoint in (primary, recovery):
            assert json.loads(checkpoint.read_bytes())["meta"]["custom_title"] == "Renamed"
        await store.save_session(alias, restored.state)
    saved = json.loads(primary.read_bytes())["meta"]
    assert (saved["session_id"], saved["parent_session_id"], saved["created_at"], saved["custom_title"]) == (
        alias,
        parent,
        created,
        "Renamed",
    )


async def test_pending_title_survives_recovery_under_another_uuid_spelling(tmp_path: Path) -> None:
    store = JsonFileStateStore(tmp_path)
    identity = "12345678-abcd-4111-8222-123456789abc"
    alias = identity.replace("-", "")
    await store.save_session(identity, {})
    primary = store.session_dir(identity) / "session.json"
    store.save_recovery_session(alias, {"messages": [Message("user", ["continued"])]})
    damaged = json.loads(primary.read_bytes())
    damaged["meta"]["updated_at"] = "unreadable"
    primary.write_text(json.dumps(damaged), encoding="utf-8")

    assert await store.update_session_titles(identity, custom_title="Pending") is None
    meta = await store.resolve_startup_session(session_short_id(identity), require_inactive=True)
    assert meta is not None and meta.session_id == alias
    with FileLock(store.active_lock_path(alias), timeout=0):
        restored = await store.load_chat_for_restore(alias, prefer_recovery=True)
        assert restored is not None and restored.recovered_from_sidecar
        await store.save_session(alias, restored.state)
        assert json.loads(primary.read_bytes())["meta"]["custom_title"] == "Pending"
        await store.update_session_titles(alias, custom_title="")
        await store.save_session(identity, restored.state)
        assert json.loads(primary.read_bytes())["meta"]["custom_title"] == ""


async def test_busy_recovery_owner_with_an_opaque_id_requires_exact_spelling(tmp_path: Path) -> None:
    store = JsonFileStateStore(tmp_path)
    identity = "opaque-1"
    store.save_recovery_session(identity, {})
    with FileLock(store.active_lock_path(identity), timeout=0):
        store.active_owner_path(identity).write_text(json.dumps({"session_id": identity}), encoding="utf-8")
        assert await store.resolve_startup_session("opaque1") is None
        with pytest.raises(StartupSessionInUseError):
            await store.resolve_startup_session(identity)


async def test_startup_identifies_busy_recovery_only_session(tmp_path) -> None:
    store = JsonFileStateStore(tmp_path)
    identity = "12345678-abcd-4111-8222-123456789abc"
    store.save_recovery_session(identity, {"messages": []})
    with FileLock(store.active_lock_path(identity), timeout=0), pytest.raises(StartupSessionInUseError):
        await store.resolve_startup_session("12345678ABCD")
    assert (await store.resolve_startup_session(identity)).session_id == identity


async def test_busy_recovery_owner_still_counts_toward_short_id_ambiguity(tmp_path) -> None:
    store = JsonFileStateStore(tmp_path)
    first = "12345678-abcd-4111-8222-123456789abc"
    second = first[:-1] + "0"
    await store.save_session(second, {"messages": []})
    (store.session_dir(second) / "session.json").replace(tmp_path / "imported.json")
    (store.session_dir(second) / "session.json.bak").unlink(missing_ok=True)
    store.save_recovery_session(first, {"messages": []})
    with FileLock(store.active_lock_path(first), timeout=0):
        store.active_owner_path(first).write_text(json.dumps({"session_id": first}))
        with pytest.raises(AmbiguousSessionIdError):
            await store.resolve_startup_session("12345678abcd")


@pytest.mark.parametrize("workspace_id", [None, "team"])
async def test_session_host_reports_ambiguous_restore_session(tmp_path, workspace_id) -> None:
    settings, model_registry = _make_settings_and_models()
    store = JsonFileStateStore(tmp_path / "sessions")
    short_id = "abc123abc123"
    first_session_id = f"{short_id}-0000-0000-0000-000000000001"
    second_session_id = f"{short_id}-0000-0000-0000-000000000002"
    now = datetime.now(UTC).isoformat()
    await store.save_session(first_session_id, {"messages": []}, agent_profile="Headless")
    legacy_path = tmp_path / "sessions" / f"{short_id}.json"
    legacy_path.write_text(
        json.dumps(
            {
                "meta": {
                    "session_id": second_session_id,
                    "agent_profile": "Headless",
                    "created_at": now,
                    "updated_at": now,
                    "message_count": 0,
                },
                "state": {"messages": []},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    host = ChrysSessionHost(
        profile_name="Headless",
        session_id=short_id,
        workspace_id=workspace_id,
        settings=settings,
        agent_registry=_make_agent_registry(_make_profile(approval_default="skip")),
        model_registry=model_registry,
        state_store=store,
    )

    try:
        with pytest.raises(AmbiguousSessionIdError, match=f"Session id '{short_id}' is ambiguous.") as exc_info:
            await host.start()
    finally:
        await host.shutdown()

    error = exc_info.value
    assert str(error) == f"Session id '{short_id}' is ambiguous."
    assert isinstance(error.display_message, MessageRef)
    assert error.display_message.definition is SESSION_ID_AMBIGUOUS
    assert dict(error.display_message.args) == {"session_id": short_id}
