# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Workflow agents retain the user's approval context and account for their spend in the hosting session."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import create_autospec

import pytest

import chrys.orchestration.workflows.agent_node_build as agent_node_build_module
from chrys.foundation.config.settings import Settings
from chrys.foundation.events.types import (
    ApprovalRequest,
    ApprovalResponse,
    SetApprovalMode,
    UsageUpdate,
    WorkflowNodeStateChanged,
    WorkflowRunAccepted,
)
from chrys.kernel import UsageDetails
from chrys.service.approval.judge import ApprovalJudge, JudgeVerdict
from chrys.service.approval.policy import ApprovalMode
from chrys.service.llm.mock import MockChatClient, MockResponse
from tests.orchestration.workflows._hosting import (
    PROFILE,
    confirm,
    make_host,
    make_profile,
    make_workdir,
    of_type,
    patch_runtime,
    run,
    write_workflow,
)

pytestmark = pytest.mark.asyncio


def _workflow(*, second: bool = False) -> bytes:
    lines = [
        "from chrys.workflows import Retry, WorkflowBuilder",
        "def prepare(text):\n    return 'Generated node input, not the user request.'",
        "wf = WorkflowBuilder('session context')",
        "prepare = wf.python('prepare', prepare)",
        f"review = wf.agent('review', profile={PROFILE!r}, retry=Retry(max_attempts=2, backoff=0))",
        "wf.start(prepare)",
        "wf.chain(prepare, review)",
    ]
    if second:
        lines += [f"judge = wf.agent('judge', profile={PROFILE!r})", "wf.chain(review, judge)", "wf.output(judge)"]
    else:
        lines.append("wf.output(review)")
    lines.append("workflow = wf.build()")
    return ("\n".join(lines) + "\n").encode()


@pytest.mark.parametrize("mode", ["manual", "auto"])
async def test_approvals_keep_the_original_request_across_retries_and_replace_it_for_the_next_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("reference", encoding="utf-8")
    first_call = MockResponse(tool_calls=[("read_file", "call_1", {"path": str(outside)})])
    retry_call = MockResponse(tool_calls=[("read_file", "call_2", {"path": str(outside)})])
    client = MockChatClient(responses=[])
    monkeypatch.setattr(
        client,
        "_next_response",
        create_autospec(
            client._next_response,
            side_effect=[first_call, TimeoutError("retry this pass"), retry_call, MockResponse(text="reviewed")],
        ),
    )
    patch_runtime(
        monkeypatch,
        [
            MockChatClient(responses=[]),
            client,
            MockChatClient(responses=[first_call, MockResponse(text="reviewed again")]),
        ],
        builtin_tools=True,
    )
    evaluate = create_autospec(ApprovalJudge.evaluate, return_value=JudgeVerdict(approved=True, reason="in scope"))
    monkeypatch.setattr(ApprovalJudge, "evaluate", evaluate)
    project = make_workdir(tmp_path)
    write_workflow(project, "review", _workflow())
    # No in-place request retry: the timeout fails the pass, and the node's next attempt resumes it.
    host = make_host(
        tmp_path,
        project=project,
        profiles=[make_profile(builtins=["filesystem.read"])],
        settings=Settings(model_profile="mock-profile", max_transient_retries=0),
    )
    requests: list[ApprovalRequest] = []

    async def approve(event: ApprovalRequest) -> None:
        requests.append(event)
        if not event.judging:
            await host.event_bus.publish(
                ApprovalResponse(request_id=event.request_id, approved=True, session_id=host.workflow_session_id)
            )

    async def select_mode(event: WorkflowRunAccepted) -> None:
        await host.event_bus.publish(SetApprovalMode(mode=mode, persist=False))

    await host.event_bus.subscribe(WorkflowRunAccepted, select_mode)
    await host.event_bus.subscribe(ApprovalRequest, approve)
    try:
        await confirm(host, "review")
        await host.start()
        assert host.engine.approval_mode is ApprovalMode.BYPASS
        first, events = await run(host, "review", input_text="Read the reference for this review.")
        assert first.outcome.value == "completed"
        assert [
            event.attempt
            for event in of_type(events, WorkflowNodeStateChanged)
            if event.node_id == "review" and event.state == "running"
        ] == [1, 2]
        second, _events = await run(host, "review", input_text="Read the reference for a different task.")
        assert second.outcome.value == "completed"
        expected = [
            "Read the reference for this review.",
            "Read the reference for this review.",
            "Read the reference for a different task.",
        ]
        assert [event.user_message for event in requests] == expected
        assert [event.judging for event in requests] == [mode == "auto"] * 3
        if mode == "auto":
            assert [call.kwargs["user_message"] for call in evaluate.await_args_list] == expected
            assert [call.kwargs["user_messages"] for call in evaluate.await_args_list] == [[text] for text in expected]
        else:
            evaluate.assert_not_awaited()
    finally:
        await host.shutdown()


async def test_node_usage_across_calls_retries_and_nodes_is_persisted_without_changing_the_main_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The timeout is retried in place; its production backoff would only add seconds.
    monkeypatch.setattr(agent_node_build_module, "RETRY_BACKOFF_SCHEDULE", (0,))
    project = make_workdir(tmp_path)
    reference = project / "reference.txt"
    reference.write_text("reference", encoding="utf-8")
    write_workflow(project, "review", _workflow(second=True))
    client = MockChatClient(responses=[])
    monkeypatch.setattr(
        client,
        "_next_response",
        create_autospec(
            client._next_response,
            side_effect=[
                MockResponse(
                    tool_calls=[("read_file", "call_1", {"path": str(reference)})],
                    usage_details=UsageDetails(
                        {"input_token_count": 10, "output_token_count": 5, "prompt/cached_tokens": 4}
                    ),
                ),
                TimeoutError("retry after a billed call"),
                MockResponse(
                    text="reviewed",
                    usage_details=UsageDetails(
                        {"input_token_count": 20, "output_token_count": 6, "prompt/cached_tokens": 7}
                    ),
                ),
            ],
        ),
    )
    patch_runtime(
        monkeypatch,
        [
            MockChatClient(
                responses=[
                    MockResponse(text="main", usage_details=UsageDetails(input_token_count=7, output_token_count=3))
                ]
            ),
            client,
            MockChatClient(
                responses=[
                    MockResponse(
                        text="verdict",
                        usage_details=UsageDetails(
                            {"input_token_count": 30, "output_token_count": 9, "prompt/cached_tokens": 8}
                        ),
                    )
                ]
            ),
            MockChatClient(responses=[]),  # restoring the saved main agent
        ],
        builtin_tools=True,
    )
    profiles = [make_profile(builtins=["filesystem.read"])]
    host = make_host(tmp_path, project=project, profiles=profiles)
    try:
        await host.run_until_final("A previous main-agent turn.")
        parent_context = dict(host.engine.session.runtime_meta.last_usage_details)
        await confirm(host, "review")
        result, events = await run(host, "review", input_text="Review the reference.")
        assert result.outcome.value == "completed"
        states = of_type(events, WorkflowNodeStateChanged)
        identities = {event.node_id: event.invocation_id for event in states if event.invocation_id}
        usage = [event for event in of_type(events, UsageUpdate) if event.usage_source_id in identities.values()]
        assert [event.usage_source_id for event in usage] == [
            identities["review"],
            identities["review"],
            identities["judge"],
        ]
        assert [(event.input_tokens, event.output_tokens, event.cache_hit_tokens) for event in usage] == [
            (10, 5, 4),
            (20, 6, 7),
            (30, 9, 8),
        ]
        assert [event.total_session_tokens for event in usage] == [15, 41, 80]
        assert all(event.agent_profile == PROFILE for event in usage)
        assert host.engine.session.runtime_meta.total_session_tokens == 10
        assert host.engine.session.runtime_meta.last_usage_details == parent_context
        session_id = host.workflow_session_id
        from chrys.service.state.store import JsonFileStateStore

        state_store = JsonFileStateStore(tmp_path / "sessions")
        saved = (await state_store.load_workflow_session(session_id)).encode()
        assert saved is not None
        assert saved["total_session_tokens"] == 80
        assert saved["total_session_input_tokens"] == 60
        assert saved["total_session_output_tokens"] == 20
        assert saved["total_session_cache_hit_tokens"] == 19
    finally:
        await host.shutdown()

    restored = make_host(tmp_path, project=project, profiles=profiles)
    try:
        await restored.load_workflow_session(session_id)
        assert restored.session_id is None
        assert (await state_store.load_workflow_session(session_id)).encode() == saved
    finally:
        await restored.shutdown()
