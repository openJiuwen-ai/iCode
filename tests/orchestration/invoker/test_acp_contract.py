# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The minimal ACP contract uses the managed client and a real stub process."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import create_autospec

import pytest

from chrys.foundation.events.bus import EventBus
from chrys.foundation.tool_result_metadata import TOOL_FAILED_METADATA_KEY
from chrys.kernel import Content, Message
from chrys.orchestration.invoker.contracts import (
    ContinuationCapability,
    Failed,
    FailureCategory,
    FailureDisposition,
    Ok,
    RunIntent,
    RunRequest,
    StaleContinuation,
    StopCause,
    SubAgentStatus,
    UnsupportedRequest,
    UsageDelta,
)
from chrys.orchestration.invoker.evidence import UNKNOWN_COUNT
from chrys.service.acp_client.errors import AcpTransportError
from chrys.service.tools.result_metadata import tool_result_metadata
from tests.orchestration.sub_agents._acp_fakes import make_controller
from tests.service.acp_client.helpers import make_spec
from tests.support.waiting import ENGINE_TEST_WAIT_TIMEOUT, with_wait_deadline


@pytest.mark.parametrize("scenario", ["happy", "refusal", "empty", "prompt_truncated"])
async def test_real_acp_initialize_new_prompt_close_and_terminal_disposition(tmp_path: Path, scenario: str) -> None:
    controller = make_controller(
        EventBus(),
        tmp_path,
        spec_factory=lambda ordinal: make_spec(tmp_path, scenario=scenario),
    )
    try:
        outcome = await controller.policy.backend.run(
            RunRequest([Message("user", ["prompt"])], RunIntent.FRESH, controller.origin)
        )
        assert isinstance(outcome, Ok if scenario == "happy" else Failed)
        if isinstance(outcome, Failed):
            assert outcome.disposition is FailureDisposition.TERMINAL
            assert outcome.exception is None
            assert outcome.continuation is None
            assert outcome.stop is StopCause.FAILED
            assert (
                outcome.error
                == {
                    "refusal": "Error: The ACP agent refused the request.",
                    "empty": "Error: sub-agent 'external' returned no output",
                    "prompt_truncated": "Error: The ACP agent stopped with max_tokens. Partial output: stub response",
                }[scenario]
            )
        assert controller.policy.backend.stop_reason == {"refusal": "refusal", "prompt_truncated": "max_tokens"}.get(
            scenario, "end_turn"
        )
        assert outcome.effects.external_stateful is True
        assert outcome.effects.local_dispatched == UNKNOWN_COUNT
        assert outcome.effects.local_answered == UNKNOWN_COUNT
        assert outcome.effects.hosted_observed == UNKNOWN_COUNT
        assert controller.policy.backend._active_client is None
        assert controller.policy.backend.active_handle is None
        assert controller.policy.backend.transport_ordinal == 1
        assert "chrys_history" not in vars(controller)
    finally:
        await controller.policy.backend.aclose()


async def test_real_acp_remote_cancel_requires_caller_decision(tmp_path: Path) -> None:
    controller = make_controller(
        EventBus(), tmp_path, spec_factory=lambda ordinal: make_spec(tmp_path, scenario="prompt_cancelled")
    )
    try:
        outcome = await controller.policy.backend.run(
            RunRequest([Message("user", ["prompt"])], RunIntent.FRESH, controller.origin)
        )
        assert controller.policy.backend.stop_reason == "cancelled"
        assert isinstance(outcome, Failed)
        assert outcome.disposition is FailureDisposition.CALLER_DECISION
        assert outcome.stop is StopCause.FAILED
        assert isinstance(outcome.exception, AcpTransportError)
        assert outcome.category is FailureCategory.TRANSPORT
        assert outcome.error == "The ACP agent cancelled the prompt unexpectedly."
        assert outcome.continuation is not None
        assert outcome.continuation.capability is ContinuationCapability.FRESH_SESSION
        assert outcome.usage == UsageDelta(3, 5, 8, True, 0)
        assert outcome.effects.external_stateful is True
        assert controller.policy.backend.transport_ordinal == 1
        assert controller.policy.backend.active_handle is None
        assert controller.policy.backend._active_client is None
    finally:
        await controller.policy.backend.aclose()


@pytest.mark.parametrize("result_mode", ["last_segment", "transcript"])
@pytest.mark.parametrize("entry", ["pass", "controller"])
async def test_successful_acp_error_prefix_preserves_success(tmp_path: Path, result_mode: str, entry: str) -> None:
    expected = "Error: is the literal prefix requested in this example."
    controller = make_controller(
        EventBus(),
        tmp_path,
        spec_factory=lambda ordinal: make_spec(tmp_path, scenario="error_prefix"),
        result_mode=result_mode,
    )
    metadata: dict[str, object] = {}
    token = tool_result_metadata.set(metadata)
    try:
        if entry == "pass":
            outcome = await controller.policy.backend.run(
                RunRequest([Message("user", ["prompt"])], RunIntent.FRESH, controller.origin)
            )
        else:
            assert await controller.run() == expected
            outcome = controller.outcome
        assert controller.policy.backend.stop_reason == "end_turn"
        assert controller.status is SubAgentStatus.COMPLETED
        assert metadata[TOOL_FAILED_METADATA_KEY] is False
        assert isinstance(outcome, Ok)
        assert "".join(content.text or "" for content in outcome.segments) == expected
        assert outcome.continuation is None
        assert outcome.usage.total_tokens == 8
        assert outcome.usage.complete is True
        assert controller.policy.backend.active_handle is None
        assert controller.policy.backend._active_client is None
    finally:
        tool_result_metadata.reset(token)
        await controller.policy.backend.aclose()


async def test_real_acp_retry_opens_fresh_session_and_consumes_ticket_once(tmp_path: Path) -> None:
    wire_path = tmp_path / "wire.jsonl"
    controller = make_controller(
        EventBus(),
        tmp_path,
        spec_factory=lambda ordinal: make_spec(
            tmp_path,
            scenario="retry_reused_tool_usage",
            extra_env={"CHRYS_ACP_STUB_FLAG_FILE": str(wire_path)},
        ),
    )
    request = RunRequest([Message("user", ["prompt"])], RunIntent.FRESH, controller.origin)
    try:
        first = await controller.policy.backend.run(request)
        assert isinstance(first, Failed)
        assert first.disposition is FailureDisposition.CALLER_DECISION
        assert first.continuation is not None
        assert first.continuation.capability is ContinuationCapability.FRESH_SESSION
        assert first.usage.total_tokens == 21
        assert first.effects.external_stateful is True
        retry = RunRequest(request.messages, RunIntent.RETRY, request.origin, first.continuation)
        second = await controller.policy.backend.run(retry)
        assert isinstance(second, Ok)
        assert second.usage.total_tokens == 8
        assert second.handle.invocation_id == first.handle.invocation_id
        assert second.handle.pass_id != first.handle.pass_id
        assert controller.policy.backend.transport_ordinal == 2
        with pytest.raises(StaleContinuation):
            await controller.policy.backend.run(retry)
        assert controller.policy.backend.transport_ordinal == 2
        assert controller.policy.backend.total_usage_tokens == 29
    finally:
        await controller.policy.backend.aclose()


@pytest.mark.parametrize("case", ["continue", "image", "missing_ticket"])
async def test_acp_rejects_unsupported_input_before_transport(tmp_path: Path, case: str) -> None:
    factory = create_autospec(lambda ordinal: make_spec(tmp_path))
    controller = make_controller(EventBus(), tmp_path, spec_factory=factory)
    messages = [Message("user", ["text"])]
    if case == "image":
        messages = [Message("user", [Content.from_uri("https://example.invalid/image", media_type="image/png")])]
    intent = (
        RunIntent.CONTINUE if case == "continue" else RunIntent.RETRY if case == "missing_ticket" else RunIntent.FRESH
    )
    try:
        with pytest.raises((UnsupportedRequest, StaleContinuation)):
            await controller.policy.backend.run(RunRequest(messages, intent, controller.origin))
        factory.assert_not_called()
        assert controller.policy.backend.transport_ordinal == 0
    finally:
        await controller.policy.backend.aclose()


async def test_acp_closed_retry_ticket_is_stale_before_transport(tmp_path) -> None:
    from chrys.orchestration.invoker.contracts import AbortCause, ContinuationCapability, ContinuationTicket

    controller = make_controller(
        EventBus(), tmp_path, spec_factory=lambda ordinal: make_spec(tmp_path, scenario="happy")
    )
    controller.policy.backend._owner_close_cause = AbortCause.OWNER_CLOSE
    ticket = ContinuationTicket("closed", "pass", 1, ContinuationCapability.FRESH_SESSION)
    with pytest.raises(StaleContinuation):
        await controller.policy.backend.run(
            RunRequest([Message("user", ["work"])], RunIntent.RETRY, controller.origin, ticket)
        )
    assert controller.policy.backend.transport_ordinal == 0
    await controller.policy.backend.aclose()


@with_wait_deadline(ENGINE_TEST_WAIT_TIMEOUT)
async def test_real_acp_overlap_rejected_before_factory_prompt_ordinal_and_disk(
    tmp_path, monkeypatch, wait_for_child_window
):
    import asyncio

    from chrys.orchestration.invoker.contracts import OverlappingRun
    from chrys.service.acp_client import AcpAgentClient

    entered, release = asyncio.Event(), asyncio.Event()
    prompts = []
    original_prompt = AcpAgentClient.prompt

    async def prompt(client, text):
        prompts.append(text)
        if len(prompts) == 1:
            entered.set()
            await release.wait()
        return await original_prompt(client, text)

    monkeypatch.setattr(AcpAgentClient, "prompt", create_autospec(original_prompt, side_effect=prompt))
    factory = create_autospec(lambda ordinal: make_spec(tmp_path), side_effect=lambda ordinal: make_spec(tmp_path))
    controller = make_controller(EventBus(), tmp_path, spec_factory=factory)
    first = asyncio.create_task(
        controller.policy.backend.run(RunRequest([Message("user", ["first"])], RunIntent.FRESH, controller.origin))
    )
    try:
        await wait_for_child_window(first, entered, "first prompt sent to the ACP stub")
        ordinal = controller.policy.backend.transport_ordinal
        active = controller.policy.backend.active_handle
        factory_calls = factory.call_count
        files = {str(p.relative_to(tmp_path)): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
        with pytest.raises(OverlappingRun) as caught:
            await asyncio.wait_for(
                controller.policy.backend.run(
                    RunRequest([Message("user", ["second"])], RunIntent.FRESH, controller.origin)
                ),
                5,
            )
        assert type(caught.value) is OverlappingRun
        assert factory.call_count == factory_calls == 1
        assert controller.policy.backend.transport_ordinal == ordinal == 1
        assert controller.policy.backend.active_handle is active
        assert controller.policy.backend._prompt == "first"
        assert prompts == ["first"]
        assert files == {str(p.relative_to(tmp_path)): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
        release.set()
        assert isinstance(await first, Ok)
    finally:
        release.set()
        await controller.policy.backend.aclose()
        await asyncio.gather(first, return_exceptions=True)
