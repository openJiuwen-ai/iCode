# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Retiring an empty turn supersedes it, while the prompt that replaced it keeps an exact submission wait."""

from __future__ import annotations

import asyncio
import sys
from dataclasses import replace
from pathlib import Path
from unittest.mock import create_autospec

import pytest

import chrys.orchestration.engine.build.builder as builder_module
from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import UserInterrupt, UserMessage, UserRollback
from chrys.foundation.models.workspace import Workspace
from chrys.foundation.trajectory.event_types import EventType
from chrys.service.analytics import Precision, analyze_trajectory
from chrys.service.hooks.events import HookEvent
from chrys.service.hooks.manager import HookManager
from chrys.service.hooks.schema import HookConfig, HookExecution, HookRun, HooksFile
from chrys.service.state.store import JsonFileStateStore
from chrys.service.trajectory.session import trajectory_events_path
from tests.orchestration.engine.run._engine_run_helpers import _PROFILE, _make_registry
from tests.orchestration.engine.run.test_fresh_turn_alignment import _ask, _EndingClient
from tests.support.pipeline_helpers import make_mock_settings_and_registry
from tests.support.trajectory_invariants import assert_trajectory_file_accounted
from tests.support.waiting import ENGINE_TURN_TIMEOUT, await_run_task_chain, wait_for

# An after_turn hook that holds a turn's finalization until the test releases it.
_HELD_AFTER_TURN = """
import pathlib, sys, time
pathlib.Path(sys.argv[1]).touch()
release = pathlib.Path(sys.argv[2])
deadline = time.monotonic() + 30
while not release.exists() and time.monotonic() < deadline:
    time.sleep(0.01)
"""


def _hook(hook_id: str, event: HookEvent, argv: list[str]) -> HookConfig:
    return HookConfig(
        id=hook_id,
        event=event,
        execution=HookExecution(mode="blocking"),
        run=HookRun(type="command", argv=argv),
    )


@pytest.mark.parametrize("later_rollback", [False, True], ids=["replacement_kept", "replacement_rolled_back"])
@pytest.mark.parametrize("interleaved", [False, True], ids=["after_settle", "during_finalization"])
async def test_retirement_keeps_the_replacement_submission_exact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, agent_engine, interleaved: bool, later_rollback: bool
) -> None:
    settings, models = make_mock_settings_and_registry(stream=True)
    settings = replace(settings, workspace_change_notice=False)
    store = JsonFileStateStore(tmp_path)
    client = _EndingClient("empty", tmp_path / "unused")
    monkeypatch.setattr(
        builder_module, "create_client", create_autospec(builder_module.create_client, return_value=client)
    )
    engine = agent_engine(
        EventBus(),
        settings=settings,
        agent_registry=_make_registry(),
        model_registry=models,
        state_store=store,
        initial_workspace=Workspace.from_cwd(str(tmp_path)),
    )
    await engine.start(_PROFILE)
    held, release = tmp_path / "after_turn_held", tmp_path / "after_turn_release"
    release.touch()
    engine.session.hook_manager = HookManager(
        file=HooksFile(
            hooks=[
                _hook("submission", HookEvent.USER_PROMPT_SUBMIT, [sys.executable, "-c", "pass"]),
                _hook(
                    "finalization",
                    HookEvent.AFTER_TURN,
                    [sys.executable, "-c", _HELD_AFTER_TURN, str(held), str(release)],
                ),
            ]
        ),
        hooks_dir=tmp_path,
    )
    await _ask(engine, "Baseline")
    baseline_id = engine.turn_records()[-1].turn_id
    if interleaved:
        release.unlink()
        held.unlink()
    await engine.event_bus.publish(UserMessage(text="Empty turn"))
    await wait_for(client.held.is_set, timeout=ENGINE_TURN_TIMEOUT, description="empty turn in flight")
    retired_id = engine.turn_records()[-1].turn_id
    await engine.event_bus.publish(UserInterrupt())
    if interleaved:
        # The next prompt arrives while the empty turn is still finishing.
        await wait_for(held.exists, timeout=ENGINE_TURN_TIMEOUT, description="empty turn finalizing")
        sender = asyncio.create_task(engine.event_bus.publish(UserMessage(text="Replacement")))
        lease = engine.turns.turn_state.lease
        await wait_for(
            lambda: bool(lease.pre_admission_preparations),
            timeout=ENGINE_TURN_TIMEOUT,
            description="replacement preparation started",
        )
        release.touch()
        await sender
        await await_run_task_chain(engine, turn_state=engine.turns.turn_state)
    else:
        await await_run_task_chain(engine, turn_state=engine.turns.turn_state)
        await _ask(engine, "Replacement")
    replacement_id = engine.turn_records()[-1].turn_id
    if later_rollback:
        await engine.event_bus.publish(UserRollback(target_turn=1))
        await await_run_task_chain(engine, turn_state=engine.turns.turn_state)
    session_id = engine.session_id
    assert session_id is not None
    path = trajectory_events_path(store.session_dir(session_id))
    await engine.shutdown()

    events = assert_trajectory_file_accounted(path).events
    start = next(
        event for event in events if event.event_type == EventType.TURN_STARTED and event.turn_id == replacement_id
    )
    preparation = next(
        event
        for event in events
        if event.event_type == EventType.PREPARATION_STARTED
        and event.operation_id == start.payload["preparation_scope_operation_id"]
    )
    retirement = next(
        event
        for event in events
        if event.event_type == EventType.SESSION_ROLLBACK and event.payload["reason_code"] == "fresh_turn_retired"
    )
    # The recorded range still runs up to the retirement, over the replacement's preparation.
    assert retirement.payload["superseded_to_sequence"] == retirement.sequence - 1
    assert preparation.sequence < retirement.sequence < start.sequence
    if interleaved:
        retired_after_preparation = [
            event
            for event in events
            if preparation.sequence < event.sequence < retirement.sequence and event.turn_id == retired_id
        ]
        assert retired_after_preparation

    analysis = analyze_trajectory(path)
    assert analysis.diagnostics.rollback_projection_unresolved is False
    assert analysis.diagnostics.timeline_operations == ()
    kept_turns = [baseline_id] if later_rollback else [baseline_id, replacement_id]
    assert [turn.turn_id for turn in analysis.turns] == kept_turns
    latency = analysis.submission_latency
    assert latency is not None
    samples = [sample for bucket in latency.buckets for sample in bucket.samples]
    assert sum(bucket.unresolved_count for bucket in latency.buckets) == 0
    replacement_samples = [sample for sample in samples if sample.turn_id == replacement_id]
    if later_rollback:
        assert replacement_samples == []
    else:
        assert [sample.duration_ns.precision for sample in replacement_samples] == [Precision.EXACT]
