# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Failure events from a real engine carry what the error means to the user, beside the raw text.

The main agent and its sub-agent run real OpenAI clients against a loopback
provider; DNS answers are scripted per lookup and the route probe sees a
routing table with no way out, so every surface also carries the offline
hint.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket
import sys
from collections.abc import AsyncIterator
from http import HTTPStatus
from types import ModuleType
from typing import TYPE_CHECKING, Any

import pytest

from chrys.foundation.errors import ErrorKind
from chrys.foundation.errors.network import codes_for
from chrys.foundation.events.types import (
    Error,
    InvocationAbortRequested,
    InvocationMessage,
    InvocationPaused,
    InvocationRetryAttempt,
    UserMessage,
)
from chrys.foundation.i18n import MessageRef
from chrys.foundation.net import route_probe
from chrys.orchestration.engine.run.bindings import TurnBindings
from tests.support.event_capture import capture_events
from tests.support.llm_client_engines import SUB_AGENT, ClientEngine, start_client_engine
from tests.support.network_faults import NetworkFaults, gaierror, network_faults
from tests.support.provider_errors import API_HOST, OPENAI_CONTEXT_OVERFLOW_BODY
from tests.support.waiting import ENGINE_TURN_TIMEOUT, await_run_task_chain, wait_for

if TYPE_CHECKING:
    from pathlib import Path

    from tests.support.engines import AgentEngineFactory

_USAGE = {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}


def _completion(message: dict[str, Any], finish_reason: str) -> bytes:
    return json.dumps(
        {
            "id": "chatcmpl-stub",
            "object": "chat.completion",
            "created": 0,
            "model": "stub",
            "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
            "usage": _USAGE,
        }
    ).encode()


def _answer(text: str) -> bytes:
    return _completion({"role": "assistant", "content": text}, "stop")


def _delegate(prompt: str) -> bytes:
    call = {
        "id": "call-1",
        "type": "function",
        "function": {"name": SUB_AGENT, "arguments": json.dumps({"prompt": prompt})},
    }
    return _completion({"role": "assistant", "content": None, "tool_calls": [call]}, "tool_calls")


# The server's window (131072) is below the loopback profiles' default of 200000.
_OVERFLOW = (HTTPStatus.BAD_REQUEST, json.dumps(OPENAI_CONTEXT_OVERFLOW_BODY).encode())


@contextlib.asynccontextmanager
async def _provider(*replies: bytes | tuple[HTTPStatus, bytes]) -> AsyncIterator[int]:
    """Answer one scripted reply (200 unless a status is given) per connection, then close.

    Every request resolves the host again.
    """
    queue = list(replies)

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            length = next(
                (
                    int(line.split(b":", 1)[1])
                    for line in head.split(b"\r\n")
                    if line.lower().startswith(b"content-length:")
                ),
                0,
            )
            await reader.readexactly(length)
            reply = queue.pop(0)
            status, body = reply if isinstance(reply, tuple) else (HTTPStatus.OK, reply)
            writer.write(
                f"HTTP/1.1 {status.value} {status.phrase}\r\n".encode()
                + b"Content-Type: application/json\r\nConnection: close\r\n"
                + f"Content-Length: {len(body)}\r\n\r\n".encode()
                + body
            )
            await writer.drain()
        except asyncio.IncompleteReadError, ConnectionError:
            pass
        finally:
            writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    try:
        yield server.sockets[0].getsockname()[1]
    finally:
        server.close()
        server.close_clients()
        await server.wait_closed()


def _script_lookups(faults: NetworkFaults, *failures: int | None) -> None:
    """Answer ``API_HOST`` lookups in order: a code fails that lookup, None resolves to loopback; then loopback."""
    faults.resolve_to(API_HOST, "127.0.0.1")
    loopback = faults.resolve_rules[API_HOST]
    pending = list(failures)

    def answer() -> list[tuple[Any, ...]] | BaseException:
        code = pending.pop(0) if pending else None
        return loopback() if code is None else gaierror(code)

    faults.resolve_rules[API_HOST] = answer


class _NoRouteSocket:
    def __init__(self, family: int, _kind: int) -> None:
        self._family = family

    def __enter__(self) -> _NoRouteSocket:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def connect(self, _address: Any) -> None:
        codes = codes_for(sys.platform)
        if codes is None:
            raise OSError("no code table")
        raise OSError(codes.enetunreach, "Network is unreachable")


@pytest.fixture
def offline(monkeypatch: pytest.MonkeyPatch, direct_route: None) -> None:
    """A direct route and a routing table with no way out."""
    if codes_for(sys.platform) is None:
        pytest.skip("no code table for this OS")
    monkeypatch.setattr(TurnBindings, "_BACKOFF_SCHEDULE", (0,))
    shadow = ModuleType("socket")
    shadow.__dict__.update(vars(socket), socket=_NoRouteSocket)
    monkeypatch.setattr(route_probe, "socket", shadow)


def _key(ref: MessageRef | None) -> str | None:
    return None if ref is None else ref.definition.key


def _final_answers(messages: list[InvocationMessage]) -> int:
    return sum(1 for message in messages if message.is_final and message.origin.kind == "turn")


async def _settle(started: ClientEngine) -> None:
    await await_run_task_chain(started.engine, turn_state=started.engine.turns.turn_state)


@pytest.mark.usefixtures("offline")
async def test_a_failed_turn_says_what_went_wrong_beside_the_raw_text(
    agent_engine: AgentEngineFactory, tmp_path: Path
) -> None:
    async with _provider() as port:
        with network_faults() as faults:
            # Never reached before: a name that does not exist fails at once.
            _script_lookups(faults, socket.EAI_NONAME)
            started = await start_client_engine(
                agent_engine, tmp_path, sub_agent=False, base_url=f"http://{API_HOST}:{port}/v1"
            )
            errors = await capture_events(started.bus, Error)
            retries = await capture_events(started.bus, InvocationRetryAttempt)

            await started.bus.publish(UserMessage(text="hello"))
            await wait_for(lambda: bool(errors), timeout=ENGINE_TURN_TIMEOUT, description="turn error")
            await _settle(started)

    [error] = errors
    assert error.code == "executor_error"
    assert error.facts is not None and error.facts.kind is ErrorKind.DNS_FAILED
    assert str(gaierror(socket.EAI_NONAME)) in error.message
    assert error.display_message is not None
    assert (_key(error.display_message), dict(error.display_message.args)) == (
        "error.kind.dns_failed",
        {"host": f"{API_HOST}:{port}"},
    )
    assert _key(error.display_hint) == "error.hint.maybe_offline"
    assert retries == []


@pytest.mark.usefixtures("offline")
async def test_a_retry_notice_says_what_went_wrong(agent_engine: AgentEngineFactory, tmp_path: Path) -> None:
    async with _provider(_answer("answer")) as port:
        with network_faults() as faults:
            _script_lookups(faults, socket.EAI_AGAIN)
            started = await start_client_engine(
                agent_engine, tmp_path, sub_agent=False, base_url=f"http://{API_HOST}:{port}/v1"
            )
            messages = await capture_events(started.bus, InvocationMessage)
            retries = await capture_events(started.bus, InvocationRetryAttempt)

            await started.bus.publish(UserMessage(text="hello"))
            await wait_for(lambda: _final_answers(messages) == 1, timeout=ENGINE_TURN_TIMEOUT, description="answer")
            await _settle(started)

    [retry] = retries
    assert (retry.origin.kind, retry.scope) == ("turn", "wire")
    assert str(gaierror(socket.EAI_AGAIN)) in retry.message
    assert _key(retry.display_message) == "error.kind.dns_failed"
    assert _key(retry.display_hint) == "error.hint.maybe_offline"


@pytest.mark.usefixtures("offline")
async def test_a_paused_sub_agent_says_what_went_wrong(agent_engine: AgentEngineFactory, tmp_path: Path) -> None:
    async with _provider(_delegate("look around"), _answer("done without it")) as port:
        with network_faults() as faults:
            # The parent's request resolves; the child's resolver then fails for good, which no retry fixes.
            _script_lookups(faults, None, socket.EAI_FAIL)
            started = await start_client_engine(agent_engine, tmp_path, base_url=f"http://{API_HOST}:{port}/v1")
            messages = await capture_events(started.bus, InvocationMessage)
            paused = await capture_events(started.bus, InvocationPaused)

            await started.bus.publish(UserMessage(text="delegate"))
            await wait_for(lambda: bool(paused), timeout=ENGINE_TURN_TIMEOUT, description="sub-agent pause")
            [pause] = paused
            await started.bus.publish(InvocationAbortRequested(invocation_id=pause.origin.invocation_id))
            await wait_for(lambda: _final_answers(messages) == 1, timeout=ENGINE_TURN_TIMEOUT, description="answer")
            await _settle(started)

    assert pause.origin.kind == "sub_agent"
    assert str(gaierror(socket.EAI_FAIL)) in pause.last_error
    assert pause.last_error_display is not None
    assert (_key(pause.last_error_display), dict(pause.last_error_display.args)) == (
        "error.kind.dns_failed",
        {"host": f"{API_HOST}:{port}"},
    )
    assert _key(pause.last_error_hint) == "error.hint.maybe_offline"


_MISMATCH_ARGS = {"configured_max_context_tokens": 200_000, "server_max_context_tokens": 131_072}


@pytest.mark.usefixtures("direct_route")
async def test_a_turn_over_a_smaller_server_window_says_which_window_to_set(
    agent_engine: AgentEngineFactory, tmp_path: Path
) -> None:
    async with _provider(_OVERFLOW) as port:
        with network_faults() as faults:
            _script_lookups(faults)
            started = await start_client_engine(
                agent_engine, tmp_path, sub_agent=False, base_url=f"http://{API_HOST}:{port}/v1", compaction=True
            )
            errors = await capture_events(started.bus, Error)
            retries = await capture_events(started.bus, InvocationRetryAttempt)

            await started.bus.publish(UserMessage(text="hello"))
            await wait_for(lambda: bool(errors), timeout=ENGINE_TURN_TIMEOUT, description="turn error")
            await _settle(started)

    [error] = errors
    assert error.facts is not None and error.facts.kind is ErrorKind.CONTEXT_OVERFLOW
    assert "maximum context length is 131072 tokens" in error.message
    assert error.display_message is not None
    assert (_key(error.display_message), dict(error.display_message.args)) == (
        "error.kind.context_overflow_config_mismatch",
        _MISMATCH_ARGS,
    )
    # Only the user can fix the window: no resend.
    assert retries == []


@pytest.mark.usefixtures("direct_route")
async def test_a_paused_sub_agent_over_a_smaller_server_window_says_which_window_to_set(
    agent_engine: AgentEngineFactory, tmp_path: Path
) -> None:
    async with _provider(_delegate("look around"), _OVERFLOW, _answer("done without it")) as port:
        with network_faults() as faults:
            _script_lookups(faults)
            started = await start_client_engine(agent_engine, tmp_path, base_url=f"http://{API_HOST}:{port}/v1")
            messages = await capture_events(started.bus, InvocationMessage)
            paused = await capture_events(started.bus, InvocationPaused)

            await started.bus.publish(UserMessage(text="delegate"))
            await wait_for(lambda: bool(paused), timeout=ENGINE_TURN_TIMEOUT, description="sub-agent pause")
            [pause] = paused
            await started.bus.publish(InvocationAbortRequested(invocation_id=pause.origin.invocation_id))
            await wait_for(lambda: _final_answers(messages) == 1, timeout=ENGINE_TURN_TIMEOUT, description="answer")
            await _settle(started)

    assert pause.origin.kind == "sub_agent"
    assert pause.last_error_display is not None
    assert (_key(pause.last_error_display), dict(pause.last_error_display.args)) == (
        "error.kind.context_overflow_config_mismatch",
        _MISMATCH_ARGS,
    )
