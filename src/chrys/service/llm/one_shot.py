# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Helpers for one-shot chat-client calls."""

from __future__ import annotations

import asyncio
import logging
from inspect import isawaitable
from typing import TYPE_CHECKING, Any

from chrys.foundation.util.once_close import finish_close
from chrys.kernel import report_wire_progress, wire_progress_scope

if TYPE_CHECKING:
    from collections.abc import Awaitable, Mapping, Sequence

    from chrys.kernel import ChatResponse, Message

_log = logging.getLogger(__name__)


async def _await_with_timeout(awaitable: Awaitable[Any], timeout: float | None, label: str) -> Any:
    """Await *awaitable*, failing once it goes *timeout* seconds without progress.

    Progress it reports (``report_wire_progress``) restarts this timer as it
    does the enclosing watchdog's: a stream that reported how it ends and
    reads on only for its usage is not cut off as if it went quiet.
    """
    if timeout is None:
        return await awaitable
    try:
        if timeout <= 0:
            # wait_for times out a non-positive timeout even when the call
            # would finish without suspending; asyncio.timeout does not.
            return await asyncio.wait_for(awaitable, timeout=timeout)
        event_loop = asyncio.get_running_loop()
        watching = True
        async with asyncio.timeout(timeout) as deadline:

            def _on_progress() -> None:
                # A task the call spawned can report after it settled, or
                # while the timeout is already cancelling it.
                if watching and not deadline.expired():
                    deadline.reschedule(event_loop.time() + timeout)

            try:
                with wire_progress_scope(_on_progress):
                    return await awaitable
            finally:
                watching = False
    except TimeoutError as exc:
        # Both timers raise a no-arg TimeoutError for their own timeout.
        # If the inner awaitable raised a meaningful TimeoutError
        # itself, preserve that provider/tool message verbatim.
        if str(exc):
            raise
        raise TimeoutError(f"{label} timed out after {timeout:g}s") from None


async def cleanup_response_stream(stream: Any) -> None:
    """Close a response stream through its public cancellation API."""
    close = getattr(stream, "aclose", None)
    if close is None:
        _log.warning("ResponseStream close API is unavailable")
        return
    result = close()
    if isawaitable(result):
        await result


async def get_final_response(
    client: Any,
    messages: Sequence[Message],
    *,
    stream: bool,
    options: Mapping[str, Any] | None = None,
    timeout: float | None = None,
    **kwargs: Any,
) -> ChatResponse[Any]:
    """Return the final chat response, draining a stream when requested."""
    if not stream:
        return await client.get_response(messages, stream=False, options=options, **kwargs)

    result = await _await_with_timeout(
        client.get_response(messages, stream=True, options=options, **kwargs),
        timeout,
        "LLM stream",
    )

    try:
        aiter = result.__aiter__()
        while True:
            try:
                await _await_with_timeout(aiter.__anext__(), timeout, "LLM stream update")
            except StopAsyncIteration:
                break
            # A call made while a wire pull waits on it (a compaction side
            # call) keeps that pull's stall watchdog alive chunk by chunk.
            report_wire_progress()
        return await _await_with_timeout(result.get_final_response(), timeout, "LLM final response")
    except asyncio.CancelledError:
        # An async update transform runs outside ResponseStream's protected wire pull.
        # Cancellation there still needs to close the stream and drain its cleanup.
        close_task = asyncio.create_task(cleanup_response_stream(result))
        try:
            await finish_close(close_task)
        except Exception:
            _log.debug("Failed to run ResponseStream cleanup hook after cancellation", exc_info=True)
        raise
    except TimeoutError:
        try:
            await cleanup_response_stream(result)
        except Exception:
            _log.debug("Failed to run ResponseStream cleanup hook after timeout", exc_info=True)
        raise
