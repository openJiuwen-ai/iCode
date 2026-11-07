# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The middleware's positional usage callback keeps its meaning at the workflow-shell boundary."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import create_autospec

from chrys.foundation.config.settings import Settings
from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import UsageUpdate
from chrys.foundation.models.workspace import Workspace
from chrys.orchestration.engine.state.active_session import ActiveSession
from chrys.orchestration.engine.state.current_agent import CurrentAgent
from chrys.orchestration.engine.usage import UsagePublisher
from chrys.orchestration.workflows.agent_archive import AgentNodeArchive
from chrys.orchestration.workflows.agent_node import WorkflowAgentShell
from chrys.orchestration.workflows.agent_node_build import AgentNodeResources
from chrys.service.approval.policy import ApprovalMode
from chrys.service.context.compaction import UnifiedContextStrategy
from chrys.service.context.middleware.usage import UsageTrackingMiddleware
from chrys.service.profiles.agents.schema import AgentProfile
from chrys.service.profiles.models.schema import ModelProfile
from chrys.service.session.persistence import SessionPersistence
from chrys.service.workflows.admission import AgentBinding


async def test_middleware_usage_fields_reach_session_events_and_extra_args_do_not_rebind_them(tmp_path: Path) -> None:
    bus = EventBus()
    session = ActiveSession(
        persistence=SessionPersistence(None, bus), workspace=None, approval_mode=ApprovalMode.BYPASS
    )
    session.session_id = "session"
    publisher = UsagePublisher(bus=bus, session=session, current=CurrentAgent())
    model = ModelProfile(id="model", name="model", provider="mock", model_id="mock", max_context_tokens=4096)
    shell = WorkflowAgentShell(
        binding=AgentBinding("review", AgentProfile(name="Review"), model, ""),
        node_id="review",
        invocation_id="node-run",
        archive=create_autospec(AgentNodeArchive, instance=True),
        resources=AgentNodeResources(
            bus=bus,
            session_id="session",
            session_dir=tmp_path,
            approval_anchor="Review this.",
            usage_publisher=publisher,
            workspace=Workspace.from_cwd(str(tmp_path)),
            settings=Settings(),
            approval_mode=lambda: ApprovalMode.BYPASS,
            approval_judge_for=lambda _model: None,
            approval_relay_context=lambda _model: (False, ""),
            hook_manager=None,
            mutation_tracker=None,
            mutation_coordinator=None,
            spill_quota=None,
            background_tasks=None,
            allow_user_interaction=False,
            mcp_cache=None,
            agent_registry=None,
            model_registry=None,
        ),
    )
    strategy = create_autospec(UnifiedContextStrategy, instance=True)
    strategy.calibration_ratio = 1.25
    strategy.system_overhead_tokens = 7
    strategy.calibration_initialized = True
    callback = create_autospec(shell._on_usage, side_effect=shell._on_usage)
    middleware = UsageTrackingMiddleware(on_usage=callback, compaction_strategy=strategy)
    events: list[UsageUpdate] = []

    async def record(event: UsageUpdate) -> None:
        events.append(event)

    await bus.subscribe(UsageUpdate, record)
    try:
        middleware._fire_callback(
            {"total_token_count": 42, "input_token_count": 31, "output_token_count": 11, "prompt/cached_tokens": 5},
            use_local_context_estimate=True,
            context_input_tokens=91,
        )
        # Pin the producer's complete call shape. A new field must update the adapter, not disappear in *_rest.
        expected = (42, 31, 11, 91, 1.25, 7, 5, True, True)
        callback.assert_called_once_with(*expected)
        shell._on_usage(*expected[:-1], False, "future positional field")
        await publisher.drain()
        assert len(events) == 2
        for event, (total, input_tokens) in zip(events, [(102, 91), (42, 31)], strict=True):
            assert (event.total_tokens, event.input_tokens, event.output_tokens, event.cache_hit_tokens) == (
                total,
                input_tokens,
                11,
                5,
            )
            assert (event.local_tokens, event.calibration_ratio, event.system_overhead_tokens) == (91, 1.25, 7)
            assert (event.max_context_tokens, event.agent_profile, event.usage_source_id) == (
                4096,
                "Review",
                "node-run",
            )
        assert session.runtime_meta.total_session_tokens == 84
        assert session.runtime_meta.total_session_input_tokens == 62
        assert session.runtime_meta.total_session_output_tokens == 22
        assert session.runtime_meta.total_session_cache_hit_tokens == 10
        assert session.runtime_meta.last_usage_details == {}
    finally:
        await shell.close()
        await publisher.settle()
