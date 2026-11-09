# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""What the loop owes callers that show model output as it arrives.

Every landed response is announced to the run's response-ready observer before
its tools run or the run returns it, and a streaming call that already yielded
readable text is never sent again by any wire retry lane — or, when the
policy says when streamed answer text shows, once it did.
"""

from __future__ import annotations

from typing import Any

import pytest

from chrys.foundation.retry import StreamStall
from chrys.kernel import (
    RESPONSE_READY_KWARG,
    ChatResponse,
    ChatResponseUpdate,
    Content,
    Message,
    is_retry_boundary_update,
)
from chrys.service.agent_middleware.response_validation import TerminalResponseValidationError
from tests.kernel._fakes import (
    _call_response,
    _call_update,
    _final_response,
    _make_tool,
    _OverflowSink,
    _stack,
    _text_response,
    _text_update,
    _user,
    _WireRetryPolicy,
)
from tests.support.provider_errors import openai_context_overflow


def _blank_verdict() -> TerminalResponseValidationError:
    """Validation's rejection of a response whose final message showed nothing, such as one ending in reasoning."""
    error = TerminalResponseValidationError("Response contained only reasoning")
    error.blank_response = True
    return error


def _shown(response: ChatResponse) -> str:
    calls = [content.call_id for message in response.messages for content in message.contents if content.call_id]
    return response.text or ",".join(str(call) for call in calls)


@pytest.mark.parametrize("stream", [False, True], ids=["blocking", "streaming"])
async def test_each_landed_response_is_ready_before_its_tools_run_and_the_run_ends(stream: bool) -> None:
    events: list[str] = []

    async def ready(response: ChatResponse, final: bool) -> None:
        events.append(f"ready:{_shown(response)}:{'final' if final else 'more'}")

    turns: list[Any] = (
        [[_call_update("c1", "echo", {"text": "a"})], [_text_update("done")]]
        if stream
        else [_call_response(("c1", "echo", {"text": "a"})), _text_response("done")]
    )
    layer, _wire = _stack(turns)

    response = await _final_response(
        layer,
        [_user()],
        stream=stream,
        options={"tools": [_make_tool(events)]},
        client_kwargs={RESPONSE_READY_KWARG: ready},
    )

    assert events == ["ready:c1:more", "tool:echo:a", "ready:done:final"]
    assert response.text == "done"


async def test_a_streamed_response_is_ready_after_all_of_its_updates() -> None:
    events: list[str] = []

    async def ready(response: ChatResponse, final: bool) -> None:
        events.append(f"ready:{response.text}:{final}")

    layer, _wire = _stack([[_text_update("one "), _text_update("two")]])
    stream = layer.get_response([_user()], stream=True, client_kwargs={RESPONSE_READY_KWARG: ready})
    async for update in stream:
        events.append(f"update:{update.text}")
    await stream.get_final_response()

    assert events == ["update:one ", "update:two", "ready:one two:True"]


@pytest.mark.parametrize("stream", [False, True], ids=["blocking", "streaming"])
async def test_the_final_response_without_tools_is_ready_as_the_run_returns_it(stream: bool) -> None:
    ready_texts: list[tuple[str, bool]] = []

    async def ready(response: ChatResponse, final: bool) -> None:
        ready_texts.append((response.text, final))

    call = ("c1", "echo", {"text": "a"})
    turns: list[Any] = (
        [[_call_update(*call)], [_call_update("c2", "echo", {"text": "b"})]]
        if stream
        else [_call_response(call), _call_response(("c2", "echo", {"text": "b"}))]
    )
    layer, _wire = _stack(turns, max_iterations=1)

    response = await _final_response(
        layer,
        [_user()],
        stream=stream,
        options={"tools": [_make_tool()]},
        client_kwargs={RESPONSE_READY_KWARG: ready},
    )

    # The tail's ignored call is stripped and its fallback text added before it is announced.
    assert ready_texts[0] == ("", False)
    assert ready_texts[1] == (response.messages[-1].text, True)
    assert response.messages[-1].text != ""


async def test_the_blocking_fallback_of_a_stalled_stream_is_ready_before_its_tools_run() -> None:
    events: list[str] = []

    async def ready(response: ChatResponse, final: bool) -> None:
        events.append(f"ready:{_shown(response)}:{'final' if final else 'more'}")

    layer, _wire = _stack(
        [[StreamStall("stalled")], _call_response(("c1", "echo", {"text": "a"})), [_text_update("done")]]
    )
    await _final_response(
        layer,
        [_user()],
        stream=True,
        options={"tools": [_make_tool(events)]},
        client_kwargs={RESPONSE_READY_KWARG: ready, "wire_retry_policy": _WireRetryPolicy()},
    )

    assert events == ["ready:c1:more", "tool:echo:a", "ready:done:final"]


@pytest.mark.parametrize("stream", [False, True], ids=["blocking", "streaming"])
async def test_a_response_whose_only_call_is_informational_is_ready_as_the_final_one(stream: bool) -> None:
    """The loop runs no call it only reports: the response ends the run, and says so as it lands."""
    events: list[str] = []

    async def ready(response: ChatResponse, final: bool) -> None:
        events.append(f"ready:{response.text}:{'final' if final else 'more'}")

    contents = [
        Content.from_function_call("c1", "echo", arguments={"text": "a"}, informational_only=True),
        Content.from_text("done"),
    ]
    turns: list[Any] = (
        [[ChatResponseUpdate(contents=contents, role="assistant")]]
        if stream
        else [ChatResponse(messages=[Message(role="assistant", contents=contents)])]
    )
    layer, _wire = _stack(turns)

    response = await _final_response(
        layer,
        [_user()],
        stream=stream,
        options={"tools": [_make_tool(events)]},
        client_kwargs={RESPONSE_READY_KWARG: ready},
    )

    assert events == ["ready:done:final"]
    assert response.text == "done"


@pytest.mark.parametrize("lane", ["transient", "stall_retry", "stall_fallback", "overflow_resend", "blank_resend"])
async def test_a_stream_that_yielded_answer_text_is_never_sent_again(lane: str) -> None:
    shown = Content.from_text("partial answer")
    failure: BaseException = (
        await openai_context_overflow()
        if lane == "overflow_resend"
        else _blank_verdict()
        if lane == "blank_resend"
        else StreamStall("stalled")
        if lane.startswith("stall")
        else ConnectionError("dropped")
    )
    policy = _WireRetryPolicy(stall_max_retries=1 if lane == "stall_retry" else 0)
    sink = _OverflowSink()
    layer, wire = _stack([[ChatResponseUpdate(contents=[shown], role="assistant"), failure], [_text_update("again")]])
    stream = layer.get_response(
        [_user()],
        stream=True,
        compaction_strategy=sink,
        client_kwargs={"wire_retry_policy": policy},
    )
    updates: list[ChatResponseUpdate] = []

    with pytest.raises(type(failure)):
        async for update in stream:
            updates.append(update)

    assert len(wire.calls) == 1
    assert policy.retries == []
    assert policy.before_retry_calls == 0
    assert not any(is_retry_boundary_update(update) for update in updates)
    # The window is still noted full, so the next pass compacts first.
    assert sink.notes == ([failure] if lane == "overflow_resend" else [])


@pytest.mark.parametrize(
    "shown",
    [
        Content.from_text_reasoning(text=None, protected_data="signature"),
        Content.from_text_reasoning(text="thinking"),
        Content.from_text(" \n"),
    ],
    ids=["opaque_reasoning", "readable_reasoning", "blank_answer"],
)
async def test_a_stream_that_yielded_no_answer_text_is_retried(shown: Content) -> None:
    """Reasoning may stream again: a retry shows a fresh run of thought, never a repeated answer."""
    policy = _WireRetryPolicy()
    layer, wire = _stack(
        [[ChatResponseUpdate(contents=[shown], role="assistant"), ConnectionError("dropped")], [_text_update("again")]]
    )

    response = await _final_response(layer, [_user()], stream=True, client_kwargs={"wire_retry_policy": policy})

    assert len(wire.calls) == 2
    assert len(policy.retries) == 1
    assert response.text == "again"


@pytest.mark.parametrize("shown", [False, True], ids=["not_yet_shown", "shown"])
async def test_a_policy_that_says_when_streamed_answer_text_shows_decides_the_replay(shown: bool) -> None:
    """A caller that shows each answer segment whole once it completes may retry a segment cut off before then."""
    probes: list[str] = []

    def answer_shown() -> bool:
        probes.append("probe")
        return shown

    policy = _WireRetryPolicy(answer_shown=answer_shown)
    layer, wire = _stack([[_text_update("partial answer"), ConnectionError("dropped")], [_text_update("again")]])
    stream = layer.get_response([_user()], stream=True, client_kwargs={"wire_retry_policy": policy})

    if shown:
        with pytest.raises(ConnectionError):
            async for _update in stream:
                pass
        assert len(wire.calls) == 1
        assert policy.retries == []
    else:
        async for _update in stream:
            pass
        response = await stream.get_final_response()
        assert len(wire.calls) == 2
        assert len(policy.retries) == 1
        assert response.text == "again"
    assert probes == ["probe"]
