# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Workflow nodes commit recoverable compactions and attribute their summary usage."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import create_autospec

import pytest

import chrys.orchestration.workflows.agent_node_build as agent_node_module
from chrys.foundation.config.settings import Settings
from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import (
    Event,
    InvocationCompactionCommitted,
    InvocationCompactionFinished,
    InvocationCompactionStarted,
    InvocationContextPressure,
    InvocationProgress,
    InvocationRetryAttempt,
    UsageUpdate,
)
from chrys.foundation.models.workspace import Workspace
from chrys.kernel import Content, Message, included_token_count
from chrys.orchestration.engine.state.active_session import ActiveSession
from chrys.orchestration.engine.state.current_agent import CurrentAgent
from chrys.orchestration.engine.usage import UsagePublisher
from chrys.orchestration.workflows.agent_archive import AgentNodeArchive
from chrys.orchestration.workflows.agent_node import WorkflowAgentShell
from chrys.orchestration.workflows.agent_node_build import AgentNodeResources, KernelNodeParts
from chrys.service.approval.policy import ApprovalMode
from chrys.service.context.compaction.last_words import CompactionStatus, LastWordsGenerator
from chrys.service.context.compaction.spill import SpillQuota, catalog_live_records, reconcile_spill_storage
from chrys.service.hooks.events import HookEvent
from chrys.service.hooks.manager import HookManager
from chrys.service.llm.mock import MockChatClient
from chrys.service.profiles.agents.schema import AgentProfile, ToolsConfig
from chrys.service.profiles.models.schema import ModelProfile
from chrys.service.session.persistence import SessionPersistence
from chrys.service.workflows.admission import AgentBinding
from chrys.service.workflows.store import RunSpec, WorkflowRunStore
from chrys.service.workflows.transcript import read_node_usage
from tests.service.workflows.test_store import header


async def test_parallel_node_compactions_archive_reduce_context_and_publish_node_side_call_usage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bus = EventBus()
    session = ActiveSession(
        persistence=SessionPersistence(None, bus), workspace=None, approval_mode=ApprovalMode.BYPASS
    )
    session.session_id = "session"
    session.agent_profile = AgentProfile(name="Code")
    publisher = UsagePublisher(bus=bus, session=session, current=CurrentAgent())
    # An existing main context must remain untouched by either node's summary.
    session.runtime_meta.record_usage(30, input_tokens=20, output_tokens=10)
    parent_usage = dict(session.runtime_meta.last_usage_details)
    quota = SpillQuota()
    events: list[UsageUpdate] = []
    progress: list[InvocationProgress] = []
    reporting: list[Event] = []
    hooks = create_autospec(HookManager, instance=True)
    hooks.has_hooks_for.return_value = True

    async def report(event: Event) -> None:
        reporting.append(event)

    for event_type in (
        InvocationCompactionStarted,
        InvocationCompactionFinished,
        InvocationCompactionCommitted,
        InvocationContextPressure,
        InvocationRetryAttempt,
    ):
        await bus.subscribe(event_type, report)

    async def record(event: UsageUpdate) -> None:
        events.append(event)

    async def record_progress(event: InvocationProgress) -> None:
        progress.append(event)

    await bus.subscribe(UsageUpdate, record)
    await bus.subscribe(InvocationProgress, record_progress)
    monkeypatch.setattr(
        agent_node_module,
        "create_client",
        create_autospec(agent_node_module.create_client, side_effect=lambda *_args, **_kwargs: MockChatClient()),
    )

    async def generate(self: LastWordsGenerator, *_args, **_kwargs) -> str:
        # Replace only the external LLM call, retaining the shell's callback,
        # real compaction strategy, spill writer, catalog and quota machinery.
        assert self._log_dir is not None and self._log_dir.name == "debug"
        await self._publish_status_cancellable(CompactionStatus(compaction_id="compact", stage="started"))
        await self._publish_retry_attempt("transient", 1, 2, 0)
        await self._publish_status_cancellable(
            CompactionStatus(compaction_id="compact", stage="finished", outcome="ok", duration_ms=12)
        )
        await self._publish_status_cancellable(CompactionStatus(compaction_id="compact", stage="committed"))
        self._report_side_call_usage(
            {
                "input_token_count": 300,
                "output_token_count": 100,
                "total_token_count": 400,
                "cache_read_input_token_count": 200,
            }
        )
        return "The review found no blocking issues. Continue checking the remaining files."

    generate_call = create_autospec(LastWordsGenerator.generate, side_effect=generate)
    monkeypatch.setattr(LastWordsGenerator, "generate", generate_call)
    resources = AgentNodeResources(
        bus=bus,
        session_id="session",
        session_dir=tmp_path,
        approval_anchor="Review the change.",
        usage_publisher=publisher,
        workspace=Workspace.from_cwd(str(tmp_path)),
        settings=Settings(),
        approval_mode=lambda: ApprovalMode.BYPASS,
        approval_judge_for=lambda _model: None,
        approval_relay_context=lambda _model: (False, ""),
        hook_manager=hooks,
        mutation_tracker=None,
        mutation_coordinator=None,
        spill_quota=quota,
        background_tasks=None,
        allow_user_interaction=False,
        mcp_cache=None,
        agent_registry=None,
        model_registry=None,
    )
    model = ModelProfile(
        id="model", name="model", provider="mock", model_id="mock", max_context_tokens=30_000, max_output_tokens=3_000
    )
    binding = AgentBinding("review", AgentProfile(name="QA", tools=ToolsConfig(builtins=[])), model, "")
    store = WorkflowRunStore.open(
        spec=RunSpec(manifest={}, environment={}, resolved_nodes=()),
        input_text="go",
        run_dir=tmp_path / "run",
        header=header(),
        source=b"",
    )
    shells = [
        WorkflowAgentShell(
            binding=binding,
            node_id="review",
            invocation_id=invocation,
            resources=resources,
            archive=AgentNodeArchive(store, activation_id=invocation, invocation_id=invocation, profile_name="QA"),
        )
        for invocation in ("first", "second")
    ]

    async def compact(shell: WorkflowAgentShell) -> None:
        await shell.open(resources.approval_anchor)
        shell._on_usage(50, input_tokens=40, output_tokens=10)
        original = f"{shell.invocation_id} review evidence " * 12_000
        messages = [
            Message("user", [Content.from_text("Review the change.")]),
            Message("assistant", [Content.from_function_call("read", "read_file", arguments={"path": "change.py"})]),
            Message("tool", [Content.from_function_result("read", result=original)]),
        ]
        assert isinstance(shell._parts, KernelNodeParts)
        strategy = shell._parts.compaction
        assert strategy._debug_log_dir == shell._archive.log_dir
        assert strategy._on_context_pressure is not None
        await strategy._on_context_pressure("round_limit", strategy._last_words_state.get_drop_round_breaker(), 123)
        before = strategy._annotate_and_count(messages)
        assert before > model.max_context_tokens
        assert await strategy(messages)
        assert included_token_count(messages) < before / 2
        assert not await strategy(messages)  # the next exchange is below the trigger
        assert not strategy._last_words_state.get_drop_round_breaker().disabled
        manifest = strategy._last_words_state.get_last_words_manifest()
        assert len(manifest) == 1 and manifest[0]["available"]
        archive = tmp_path / manifest[0]["relative_path"]
        assert original in archive.read_text()
        await shell._parts.events.flush_progress()
        latest = [event for event in progress if event.origin.invocation_id == shell.invocation_id][-1]
        assert (latest.total_tokens, latest.total_usage_tokens) == (50, 450)
        # A later model response must retain the summary spend in its cumulative total.
        shell._on_usage(25, input_tokens=20, output_tokens=5)
        await shell._save_transcript(status="completed")
        usage = read_node_usage(store.run_dir, shell.invocation_id, 1)
        assert usage is not None and usage.usage_tokens == 475

    try:
        await asyncio.gather(*(compact(shell) for shell in shells))
        await publisher.drain()
        assert generate_call.call_count == 2
        assert hooks.fire.await_count == 2
        for call in hooks.fire.await_args_list:
            event, payload = call.args
            assert event is HookEvent.PRE_COMPACT
            assert payload["session_id"] == "session" and payload["profile"] == "QA"
            assert payload["sub_agent"] == {"name": "QA", "tool_name": "review"}
            assert payload["tokens_before"] > model.max_context_tokens
        for shell in shells:
            node_events = [event for event in reporting if event.origin == shell.origin]
            assert [type(event) for event in node_events] == [
                InvocationContextPressure,
                InvocationCompactionStarted,
                InvocationRetryAttempt,
                InvocationCompactionFinished,
                InvocationCompactionCommitted,
            ]
            pressure, _started, retry, finished, _committed = node_events
            assert pressure.source == "workflow_node" and pressure.side_call_token_budget == 123
            assert retry.scope == "compaction" and retry.message == "LAST_WORDS compaction: transient"
            assert finished.outcome == "ok" and finished.duration_ms == 12
        assert len(events) == 6
        side_calls = [event for event in events if event.usage_source_id.endswith(":last_words")]
        assert {event.usage_source_id for event in side_calls} == {"first:last_words", "second:last_words"}
        for event in side_calls:
            assert event.agent_profile == "QA"
            assert (event.input_tokens, event.output_tokens, event.total_tokens, event.cache_hit_tokens) == (
                300,
                100,
                400,
                200,
            )
            assert event.max_context_tokens == 30_000
        assert session.runtime_meta.last_usage_details == parent_usage
        assert session.runtime_meta.total_session_tokens == 980
        assert session.runtime_meta.total_session_input_tokens == 740
        assert session.runtime_meta.total_session_output_tokens == 240
        assert session.runtime_meta.total_session_cache_hit_tokens == 400
        records = catalog_live_records(tmp_path)
        assert len(records) == 2
        assert len({Path(record.relative_path).parent for record in records}) == 2
        restored = SpillQuota()
        reconcile_spill_storage(tmp_path, restored)
        assert all(restored.is_record_available(record.relative_path) for record in records)
        assert restored.spent_bytes == quota.spent_bytes > 0
    finally:
        await asyncio.gather(*(shell.close() for shell in shells))
        await publisher.settle()
        await store.close()
