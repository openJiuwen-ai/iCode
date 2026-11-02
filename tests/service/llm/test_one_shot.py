# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for the one-shot chat-call helpers."""

from __future__ import annotations

import asyncio

import pytest

from chrys.kernel import ChatResponse, ChatResponseUpdate, Content, Message, ResponseStream, report_wire_progress
from chrys.kernel.client import start_with_wire_progress
from chrys.service.llm.one_shot import get_final_response
from tests.support.waiting import wait_for


class _Response:
    text = "done"


class _HangingStream:
    def __init__(self) -> None:
        self.cleanup_calls = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        await asyncio.sleep(10)
        raise AssertionError("unreachable after timeout")

    async def get_final_response(self):
        raise AssertionError("final response should not be reached")

    async def aclose(self) -> None:
        self.cleanup_calls += 1


class _StreamingClient:
    def __init__(self) -> None:
        self.stream = _HangingStream()

    async def get_response(self, _messages, *, stream=False, **_kwargs):
        assert stream is True
        return self.stream


class _BlockingClient:
    def __init__(self) -> None:
        self.streams: list[bool] = []

    async def get_response(self, _messages, *, stream=False, **_kwargs):
        self.streams.append(stream)
        assert stream is False
        return _Response()


@pytest.mark.asyncio
async def test_get_final_response_returns_blocking_response_without_timeout() -> None:
    client = _BlockingClient()
    response = await get_final_response(
        client,
        [Message("user", ["hello"])],
        stream=False,
        timeout=0.01,
    )

    assert response.text == "done"
    assert client.streams == [False]


@pytest.mark.asyncio
async def test_get_final_response_times_out_hung_stream_and_runs_cleanup() -> None:
    client = _StreamingClient()

    with pytest.raises(TimeoutError, match=r"LLM stream update timed out after 0\.01s"):
        await get_final_response(
            client,
            [Message("user", ["hello"])],
            stream=True,
            timeout=0.01,
        )

    assert client.stream.cleanup_calls == 1


@pytest.mark.parametrize("in_transform", [False, True])
async def test_cancelling_one_shot_closes_its_stream_without_finalizing(in_transform: bool) -> None:
    entered = asyncio.Event()
    closed = asyncio.Event()
    release = asyncio.Event()

    async def updates():
        try:
            if not in_transform:
                entered.set()
                await release.wait()
            yield ChatResponseUpdate(contents=[Content.from_text("must not finish")], role="assistant")
        finally:
            closed.set()

    async def transform(_update: ChatResponseUpdate) -> None:
        entered.set()
        await release.wait()

    stream = ResponseStream(
        updates(), finalizer=ChatResponse.from_updates, transform_hooks=[transform] if in_transform else []
    )

    class Client:
        async def get_response(self, messages, *, stream: bool, options=None):
            return response_stream

    response_stream = stream
    task = asyncio.create_task(get_final_response(Client(), [Message("user", ["question"])], stream=True))
    try:
        await wait_for(entered.is_set, description="side stream is being read")
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert closed.is_set()
        assert stream._closed
        assert not stream._finalized
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_streamed_final_response_reports_each_chunk_to_the_enclosing_wire_watchdog() -> None:
    """A compaction side call drained here keeps the waiting pull's stall
    watchdog alive chunk by chunk."""
    reports = 0

    def _on_progress() -> None:
        nonlocal reports
        reports += 1

    at_chunk: list[int] = []

    async def _updates():
        for text in ("a", "b", "c"):
            at_chunk.append(reports)
            yield ChatResponseUpdate(contents=[Content.from_text(text)], role="assistant")

    class _Client:
        async def get_response(self, _messages, *, stream=False, **_kwargs):
            assert stream is True
            return ResponseStream(_updates(), finalizer=ChatResponse.from_updates)

    response = await start_with_wire_progress(
        get_final_response(_Client(), [Message("user", ["hi"])], stream=True, timeout=5), _on_progress
    )

    assert response.text == "abc"
    assert at_chunk == [0, 1, 2]
    assert reports == 3


@pytest.mark.asyncio
async def test_a_report_that_outlives_its_read_is_ignored() -> None:
    late = asyncio.Event()
    strays: list[asyncio.Task[None]] = []

    async def _late_report() -> None:
        await late.wait()
        report_wire_progress()

    async def _updates():
        # Copies the read's context, its timer included, and outlives it.
        strays.append(asyncio.create_task(_late_report()))
        yield ChatResponseUpdate(contents=[Content.from_text("done")], role="assistant")

    class _Client:
        async def get_response(self, _messages, *, stream=False, **_kwargs):
            assert stream is True
            return ResponseStream(_updates(), finalizer=ChatResponse.from_updates)

    try:
        response = await get_final_response(_Client(), [Message("user", ["hi"])], stream=True, timeout=5)
        late.set()
        await strays[0]
    finally:
        late.set()
        await asyncio.gather(*strays, return_exceptions=True)

    assert response.text == "done"


@pytest.mark.asyncio
async def test_a_report_made_while_the_read_times_out_keeps_it_a_timeout() -> None:
    async def _updates():
        try:
            await asyncio.Event().wait()
        finally:
            report_wire_progress()
        yield ChatResponseUpdate(contents=[Content.from_text("unreachable")], role="assistant")

    class _Client:
        async def get_response(self, _messages, *, stream=False, **_kwargs):
            assert stream is True
            return ResponseStream(_updates(), finalizer=ChatResponse.from_updates)

    with pytest.raises(TimeoutError, match=r"LLM stream update timed out after 0\.01s"):
        await get_final_response(_Client(), [Message("user", ["hi"])], stream=True, timeout=0.01)
