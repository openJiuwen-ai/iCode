# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""A turn whose request overflows the context window compacts and resends once instead of failing.

The provider rejection is a real OpenAI SDK error; the engine runs the real
compaction strategy, so the resent request is the compacted one.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from chrys.foundation.events.types import Error, InvocationRetryAttempt, ToolCompacted
from chrys.service.llm.mock import MockResponse
from chrys.service.profiles.agents.schema import CompactionConfig
from tests.support.event_capture import capture_events
from tests.support.pipeline_helpers import create_test_engine, error_on_nth, extract_final_messages
from tests.support.provider_errors import openai_context_overflow

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.parametrize("stream", [False, True])
async def test_an_overflowing_request_is_compacted_and_resent_within_the_turn(tmp_path: Path, stream: bool) -> None:
    ctx = await create_test_engine(
        [
            MockResponse(tool_calls=[("echo", "call-1", {"message": "x" * 4000})]),
            MockResponse(text="first answer"),
            MockResponse(text="second answer"),
        ],
        tmp_path,
        stream=stream,
        compaction=CompactionConfig(enabled=True),
    )
    # The second turn's first request is rejected; the failed call consumes no scripted response.
    restore = error_on_nth(ctx, 3, error=await openai_context_overflow())
    errors = await capture_events(ctx.bus, Error)
    retries = await capture_events(ctx.bus, InvocationRetryAttempt)
    compacted = await capture_events(ctx.bus, ToolCompacted)

    try:
        await ctx.send_message("first")
        await ctx.send_message("second")
    finally:
        restore()
        await ctx.cleanup()

    assert errors == []
    assert extract_final_messages(ctx.events) == ["first answer", "second answer"]
    [retry] = retries
    assert (retry.scope, retry.attempt, retry.max_attempts, retry.delay_seconds) == ("wire", 1, 1, 0)
    assert retry.display_message is not None
    assert retry.display_message.definition.key == "retry.context_overflow"
    # An overflow pass has no escalation gate: the summary alone frees too little, so the group goes too.
    assert [event.phase for event in compacted] == ["phase1", "phase2"]
    resent, _options = ctx.mock_client.call_history[-1]
    assert not any(
        content.type in {"function_call", "function_result"} for message in resent for content in message.contents
    )
