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
from chrys.foundation.i18n import DisplaySequence, MessageRef
from chrys.foundation.models.history_markers import HistoryMarkerKind
from chrys.foundation.models.workspace import Workspace
from chrys.foundation.platform import get_platform
from chrys.foundation.util.session_ids import session_short_id
from chrys.orchestration.session_host import (
    AgentProfileNotFoundError,
    AmbiguousSessionIdError,
    ChrysSessionHost,
    SessionNotFoundError,
)
from chrys.service.llm.mock import MockChatClient, MockResponse
from chrys.service.profiles.agents.schema import (
    AgentProfile,
    ApprovalConfig,
    CompactionConfig,
    ToolsConfig,
)
from chrys.service.state.store import JsonFileStateStore
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
    from chrys.foundation.models.workspace import WorkingDir

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
    from chrys.foundation.models.workspace import WorkingDir

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
    from chrys.foundation.models.workspace import WorkingDir

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

    missing = ChrysSessionHost(
        profile_name="Headless",
        session_id="000000000000",
        settings=settings,
        agent_registry=agent_registry,
        model_registry=model_registry,
        state_store=store,
    )
    try:
        with pytest.raises(
            SessionNotFoundError,
            match=f"Session not found: 000000000000.*{session_short_id(first_result.session_id)}",
        ) as exc_info:
            await missing.start()
    finally:
        await missing.shutdown()

    recent_id = session_short_id(first_result.session_id)
    error = exc_info.value
    assert str(error) == f"'Session not found: 000000000000. Recent sessions: {recent_id}'"
    assert isinstance(error.display_message, MessageRef)
    assert error.display_message.definition is session_host_module._SESSION_NOT_FOUND_WITH_RECENT
    assert dict(error.display_message.args) == {
        "session_id": "000000000000",
        "recent": DisplaySequence((recent_id,)),
    }


@pytest.mark.asyncio
async def test_session_host_reports_missing_restore_session_without_recent_sessions(tmp_path) -> None:
    settings, model_registry = _make_settings_and_models()
    host = ChrysSessionHost(
        profile_name="Headless",
        session_id="000000000000",
        settings=settings,
        agent_registry=_make_agent_registry(_make_profile(approval_default="skip")),
        model_registry=model_registry,
        state_store=JsonFileStateStore(tmp_path / "sessions"),
    )

    try:
        with pytest.raises(SessionNotFoundError, match="Session not found: 000000000000") as exc_info:
            await host.start()
    finally:
        await host.shutdown()

    error = exc_info.value
    assert str(error) == "'Session not found: 000000000000'"
    assert isinstance(error.display_message, MessageRef)
    assert error.display_message.definition is session_host_module._SESSION_NOT_FOUND
    assert dict(error.display_message.args) == {"session_id": "000000000000"}


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


@pytest.mark.asyncio
async def test_session_host_reports_ambiguous_restore_session(tmp_path) -> None:
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
    assert error.display_message.definition is session_host_module._SESSION_ID_AMBIGUOUS
    assert dict(error.display_message.args) == {"session_id": short_id}
