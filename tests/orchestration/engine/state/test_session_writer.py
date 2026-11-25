# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Session writer ordering, identity capture, and primary-save admission."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import patch

import pytest

from chrys.foundation.models.session_surface import SessionSurface
from chrys.foundation.models.workspace import Workspace
from chrys.foundation.recovery import RecoveryPersistOutcome
from chrys.service.profiles.models.schema import ModelProfile
from chrys.service.state.store import JsonFileStateStore
from tests.orchestration.engine._recovery_helpers import CheckpointComponents, _profile, make_checkpoint_components
from tests.support.components import plan_workspace
from tests.support.loaded_agents import install_loaded_agent
from tests.support.waiting import wait_until


def _seed_identity_components(store: JsonFileStateStore, tmp_path: Path) -> tuple[CheckpointComponents, dict]:
    """Supply each identity field independently of the writer's projection."""
    workspace = Workspace(primary_cwd=str(tmp_path))
    model = ModelProfile(id="model-id", name="Original model", provider="mock", model_id="original-model")
    expected = {
        "session_id": "original",
        "agent_profile_name": "Original",
        "agent_display_name": "Original display",
        "agent_profile_id": "agent-id",
        "agent_profile_fingerprint": "original-agent-fingerprint",
        "model_profile_fingerprint": "original-model-fingerprint",
        "workspace": workspace,
        "model_profile": model,
        "last_surface": SessionSurface.CLI,
    }
    components = make_checkpoint_components(store, "original")
    profile = _profile("Original", "Original display")
    profile.id = "agent-id"
    components.session.agent_profile = profile
    components.session.install_workspace(plan_workspace(workspace))
    components.session.surface = SessionSurface.CLI
    components.session.mark_surface()
    install_loaded_agent(
        components,
        agent_profile_fingerprint="original-agent-fingerprint",
        model_profile_fingerprint="original-model-fingerprint",
        active_profile=model,
    )
    return components, expected


@pytest.mark.parametrize("operation", ["checkpoint", "barrier", "strict", "primary"])
async def test_session_writes_preserve_all_nine_identity_fields(tmp_path: Path, operation: str) -> None:
    components, expected = _seed_identity_components(JsonFileStateStore(tmp_path), tmp_path)
    if operation == "checkpoint":
        method = "save_recovery_session"
    elif operation == "primary":
        method = "save_session"
    else:
        method = "save_recovery_session_strict"
    with patch.object(components.persistence, method, autospec=True, return_value=True) as write:
        if operation == "checkpoint":
            await components.writer.save_checkpoint()
        elif operation == "barrier":
            assert await components.writer.persist_barrier() is RecoveryPersistOutcome.PERSISTED
        elif operation == "strict":
            assert await components.writer.persist_now() is True
        else:
            assert await components.writer.save_current_session() is True
        await components.writer.flush()
    write.assert_awaited_once()
    actual = {"session_id": write.call_args.args[0], **write.call_args.kwargs}
    for name, value in expected.items():
        assert actual[name] == value, name


@pytest.mark.parametrize("operation", ["checkpoint", "barrier", "strict"])
async def test_checkpoint_operations_write_and_flush_the_captured_history(tmp_path: Path, operation: str) -> None:
    store = JsonFileStateStore(tmp_path)
    components = make_checkpoint_components(store, operation)
    writer = components.writer
    if operation == "checkpoint":
        await writer.save_checkpoint()
        assert writer.write_task is not None
    elif operation == "barrier":
        assert await writer.persist_barrier() is RecoveryPersistOutcome.PERSISTED
    else:
        assert await writer.persist_now() is True
    await writer.flush()
    assert writer.pending is None
    assert writer.strict_write_tasks == set()
    assert writer.snapshot_seq == writer.persisted_seq == 1
    restored = await store.load_recovery_session(operation)
    assert restored is not None
    assert any(content.result == "done" for message in restored["messages"] for content in message.contents)


async def test_queued_checkpoint_keeps_its_session_identity_after_session_change(tmp_path: Path) -> None:
    store = JsonFileStateStore(tmp_path)
    components = make_checkpoint_components(store, "old-session")
    components.session.agent_profile = _profile("Original", "Original display")
    await components.writer.save_checkpoint()
    captured = components.writer.pending
    assert captured is not None
    components.session.session_id = "new-session"
    components.session.agent_profile = _profile("Replacement", "Replacement display")
    await components.writer.flush()
    assert await store.load_recovery_session("old-session") is not None
    assert await store.load_recovery_session("new-session") is None
    assert captured[1].session_id == "old-session"
    assert captured[1].metadata["agent_profile_name"] == "Original"
    assert captured[1].metadata["agent_display_name"] == "Original display"


async def test_barrier_waiting_for_background_write_keeps_all_captured_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = JsonFileStateStore(tmp_path)
    components, expected = _seed_identity_components(store, tmp_path)
    entered = asyncio.Event()
    release = asyncio.Event()
    original_write = components.persistence.save_recovery_session
    calls = []

    async def blocked_write(session_id, state, **metadata):
        entered.set()
        await release.wait()
        await original_write(session_id, state, **metadata)

    original_strict = components.persistence.save_recovery_session_strict

    async def record_strict(session_id, state, **metadata):
        calls.append({"session_id": session_id, **metadata})
        return await original_strict(session_id, state, **metadata)

    monkeypatch.setattr(components.persistence, "save_recovery_session", blocked_write)
    monkeypatch.setattr(components.persistence, "save_recovery_session_strict", record_strict)
    await components.writer.save_checkpoint()
    await asyncio.wait_for(entered.wait(), 5)
    barrier = asyncio.create_task(components.writer.persist_barrier())
    try:
        assert await wait_until(lambda: components.writer.snapshot_seq == 2)
        components.session.session_id = "replacement"
        components.session.agent_profile = _profile("Replacement")
        components.session.install_workspace(plan_workspace(Workspace(primary_cwd=str(tmp_path / "replacement"))))
        install_loaded_agent(
            components,
            agent_profile_fingerprint="replacement-agent",
            model_profile_fingerprint="replacement-model",
            active_profile=ModelProfile(id="replacement-model", name="Replacement model", provider="mock"),
        )
    finally:
        release.set()
    assert await barrier is RecoveryPersistOutcome.PERSISTED
    await components.writer.flush()
    assert calls == [expected, expected]
    assert await store.load_recovery_session("replacement") is None


@pytest.mark.parametrize("suppress_save", [False, True])
async def test_primary_save_without_build_or_with_suppression_returns_false(
    tmp_path: Path, suppress_save: bool
) -> None:
    store = JsonFileStateStore(tmp_path)
    if suppress_save:
        components = make_checkpoint_components(store, "suppressed")
        components.session.suppress_save = True
        await components.writer.save_checkpoint()
    else:
        components = make_checkpoint_components(store, None)
    assert await components.writer.save_current_session() is False
    assert components.writer.pending is None
    if suppress_save:
        assert components.writer.persisted_seq == 1
        assert await store.load_recovery_session("suppressed") is not None
