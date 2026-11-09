# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""What an ACP client learns when something fails: the cause in the message, the verdict in the data.

The provider cases run the real server, session manager, engine and Chat
Completions client; only the network answers through ``httpx.MockTransport``,
so the route stamp, the SDK's exception and the classifier are all real.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import httpx
import pytest
from acp import RequestError
from acp import schema as acp_schema
from acp.helpers import text_block

import chrys.service.llm.clients as clients_module
from chrys.app.acp.errors import internal_error, invalid_params, summary
from chrys.app.acp.server import ChrysAcpServer
from chrys.app.acp.session_manager import AcpSessionManager
from chrys.foundation.config.runtime_pointer import MODEL_POINTER_ENV, MODEL_POINTER_KEY
from chrys.foundation.config.settings_store import persist
from chrys.orchestration.engine.run.bindings import TurnBindings
from chrys.orchestration.startup import bootstrap_runtime
from chrys.service.approval.policy import ApprovalMode
from chrys.service.profiles.agents.registry import AgentProfileRegistry
from chrys.service.profiles.agents.schema import (
    AgentProfile,
    ApprovalConfig,
    CompactionConfig,
    SkillsConfig,
    SubAgentRef,
    SubAgentsConfig,
    ToolsConfig,
)
from chrys.service.profiles.models.registry import ModelProfileRegistry
from chrys.service.profiles.models.schema import ModelProfile
from tests.app.acp._server_fakes import _FakeClient
from tests.support.acp_wire import acp_error_json
from tests.support.waiting import ENGINE_TURN_TIMEOUT, wait_for

_AGENT = "Probe"
_HELPER = "Helper"
_MODEL = "deepseek-probe"


class _Answers(Protocol):
    def respond(self, request: httpx.Request) -> httpx.Response: ...


@dataclass
class _Provider:
    """The one HTTP answer every model request gets, and the requests that got it."""

    status: int
    body: dict[str, Any]
    requests: list[httpx.Request]

    def respond(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(self.status, json=self.body)


@dataclass
class _Acp:
    server: ChrysAcpServer
    manager: AcpSessionManager
    client: _FakeClient
    home: Path

    async def initialize(self, capabilities: acp_schema.ClientCapabilities | None = None) -> None:
        await self.server.initialize(protocol_version=1, client_capabilities=capabilities)

    async def open(self) -> str:
        response = await self.server.new_session(cwd=str(self.home), mcp_servers=[])
        return response.session_id

    async def prompt(self, session_id: str) -> acp_schema.PromptResponse:
        return await asyncio.wait_for(
            self.server.prompt([text_block("hello")], session_id=session_id), ENGINE_TURN_TIMEOUT
        )

    def notifications(self, method: str) -> list[dict[str, Any]]:
        return [params for name, params in self.client.ext_notifications if name == method]


class AcpFactory(Protocol):
    def __call__(self, provider: _Answers, *, delegates: bool = False) -> _Acp: ...


@pytest.fixture
async def acp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[AcpFactory]:
    """Build the real server for one provider answer; every manager it built is shut down after the test."""
    # A direct route: with no proxy settings at all, httpx would read the system proxy.
    monkeypatch.setenv("NO_PROXY", "*")
    # Registered with monkeypatch even when absent, so bootstrap's pointer does not outlive the test.
    monkeypatch.setenv(MODEL_POINTER_ENV, "")
    monkeypatch.delenv(MODEL_POINTER_ENV)
    # Each ACP session reads its settings from the files: the model to use, and no
    # transient retries, so a retryable failure ends the turn at once instead of after its backoff.
    assert persist({MODEL_POINTER_KEY: _MODEL, "llm.retry.max_transient": 0}).ok
    loaded = bootstrap_runtime(apply_patches=False, setup_telemetry=False).loaded
    managers: list[AcpSessionManager] = []

    def build(provider: _Answers, *, delegates: bool = False) -> _Acp:
        build_http_client = clients_module._build_profile_http_client

        def answering_http_client(
            profile: ModelProfile,
            timeout: Any,
            *,
            raw_http_log_path: Path | None = None,
            session_id: str | None = None,
            auth: httpx.Auth | None = None,
            transport: httpx.AsyncBaseTransport | None = None,
        ) -> httpx.AsyncClient:
            # The real client, route hooks included; only the network is replaced.
            _ = transport
            return build_http_client(
                profile,
                timeout,
                raw_http_log_path=raw_http_log_path,
                session_id=session_id,
                auth=auth,
                transport=httpx.MockTransport(provider.respond),
            )

        monkeypatch.setattr(clients_module, "_build_profile_http_client", answering_http_client)
        models = ModelProfileRegistry()
        models.register(
            ModelProfile(
                id=_MODEL,
                name=_MODEL,
                provider="deepseek-openai",
                api_style="chat_completions",
                model_id="deepseek-chat",
                base_url="https://api.deepseek.test/v1",
                api_key="test-key",
                http_max_retries=0,
                stream=True,
            )
        )
        agents = AgentProfileRegistry()
        for name, sub_agents in (
            (_AGENT, [SubAgentRef(profile=_HELPER, tool_description="Looks things up.")] if delegates else []),
            (_HELPER, []),
        ):
            agents.register(
                AgentProfile(
                    name=name,
                    instructions="Reply briefly.",
                    tools=ToolsConfig(builtins=[]),
                    skills=SkillsConfig(auto_load_user_agents_skills=False, auto_load_cwd_agents_skills=False),
                    approval=ApprovalConfig(default="auto"),
                    compaction=CompactionConfig(enabled=False),
                    sub_agents=SubAgentsConfig(agents=sub_agents),
                    sub_agent_only=name == _HELPER,
                )
            )
        manager = AcpSessionManager(
            loaded_settings=loaded,
            profile_name=_AGENT,
            approval_mode=ApprovalMode.BYPASS,
            process_cwd=str(tmp_path),
            agent_registry=agents,
            model_registry=models,
        )
        managers.append(manager)
        server = ChrysAcpServer(manager, initial_vision=False)
        client = _FakeClient()
        server.on_connect(client)
        return _Acp(server, manager, client, tmp_path)

    try:
        yield build
    finally:
        for manager in managers:
            await manager.shutdown()


_DEEPSEEK_402 = {
    "error": {
        "message": "Insufficient Balance",
        "type": "unknown_error",
        "param": None,
        "code": "invalid_request_error",
    }
}


@pytest.mark.parametrize(
    ("status", "body", "message", "verdict"),
    [
        pytest.param(
            402,
            _DEEPSEEK_402,
            "Error code: 402 - Insufficient Balance",
            {
                "kind": "quota_exhausted",
                "retryable": False,
                "statusCode": 402,
                "hint": "The model service account is out of quota or credit. Check the account and try again.",
            },
            id="out-of-balance",
        ),
        pytest.param(
            401,
            # DeepSeek's own answer to a bad key: its code is the broad `invalid_request_error`.
            {
                "error": {
                    "message": "Authentication Fails, Your api key: ****abcd is invalid",
                    "type": "authentication_error",
                    "param": None,
                    "code": "invalid_request_error",
                }
            },
            "Error code: 401 - Authentication Fails, Your api key: ****abcd is invalid",
            {
                "kind": "auth_failed",
                "retryable": False,
                "statusCode": 401,
                "hint": "The API key is invalid or lacks access. Check the API key in the model profile.",
            },
            id="bad-key",
        ),
        pytest.param(
            429,
            {"error": {"message": "Rate limit reached", "type": "rate_limit_error"}},
            "Error code: 429 - Rate limit reached",
            {
                "kind": "rate_limited",
                "retryable": True,
                "statusCode": 429,
                "hint": "The model service is rate limiting requests.",
            },
            id="rate-limited",
        ),
    ],
)
async def test_a_failed_prompt_tells_the_client_the_providers_cause_and_the_verdict(
    acp: AcpFactory, status: int, body: dict[str, Any], message: str, verdict: dict[str, Any]
) -> None:
    provider = _Provider(status, body, [])
    server = acp(provider)
    await server.initialize()
    session_id = await server.open()

    with pytest.raises(RequestError) as failed:
        await server.prompt(session_id)

    # The message a client shows by itself is the provider's own words, not "Internal error".
    assert failed.value.code == -32603
    assert str(failed.value) == message
    data = failed.value.data
    assert isinstance(data, dict)
    assert {key: data[key] for key in ("code", "message", *verdict)} == {
        "code": "executor_error",
        "message": message,
        **verdict,
    }
    assert data["turnId"]
    # The failure is told as it happens too, before the turn is saved and the request answers.
    assert server.notifications("chrys/error") == [
        {"sessionId": session_id, "code": "executor_error", "message": message, "recoverable": True, **verdict}
    ]
    # No retry: a quota or key failure would fail again, and this host allows no transient retries.
    assert len(provider.requests) == 1


async def test_a_retry_notice_tells_the_client_what_is_being_retried(
    acp: AcpFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rate limit iCode retries is told as it happens, with the verdict a client can show at once."""
    monkeypatch.setattr(TurnBindings, "_BACKOFF_SCHEDULE", (0,))
    # Read by the session opened below; the fixture's own setting allows no retry.
    assert persist({"llm.retry.max_transient": 1}).ok
    provider = _Provider(429, {"error": {"message": "Rate limit reached", "type": "rate_limit_error"}}, [])
    server = acp(provider)
    await server.initialize()

    with pytest.raises(RequestError):
        await server.prompt(await server.open())

    notices = server.notifications("chrys/retry_attempt")
    assert [(n["kind"], n["statusCode"], n["hint"], "retryable" in n) for n in notices] == [
        ("rate_limited", 429, "The model service is rate limiting requests.", False)
    ]
    assert len(provider.requests) == 2


async def test_a_retry_that_fails_again_reports_the_same_verdict(acp: AcpFactory) -> None:
    server = acp(_Provider(402, _DEEPSEEK_402, []))
    await server.initialize()
    session_id = await server.open()
    with pytest.raises(RequestError):
        await server.prompt(session_id)

    with pytest.raises(RequestError) as failed:
        await asyncio.wait_for(server.server.retry(session_id), ENGINE_TURN_TIMEOUT)

    assert str(failed.value) == "Error code: 402 - Insufficient Balance"
    assert isinstance(failed.value.data, dict)
    assert (failed.value.data["kind"], failed.value.data["retryable"], failed.value.data["statusCode"]) == (
        "quota_exhausted",
        False,
        402,
    )


@pytest.mark.parametrize(
    ("capabilities", "pauses"),
    [
        pytest.param(None, False, id="no-capabilities"),
        pytest.param(acp_schema.ClientCapabilities(), False, id="standard-client"),
        pytest.param(
            acp_schema.ClientCapabilities(field_meta={"chrys": {"subAgentPause": "yes"}}), False, id="not-true"
        ),
        pytest.param(acp_schema.ClientCapabilities(field_meta={"chrys": {"subAgentPause": True}}), True, id="declared"),
    ],
)
async def test_only_a_client_declaring_it_answers_a_paused_sub_agent(
    acp: AcpFactory, capabilities: acp_schema.ClientCapabilities | None, pauses: bool
) -> None:
    server = acp(_Provider(402, _DEEPSEEK_402, []))
    await server.initialize(capabilities)

    session = server.manager.get(await server.open())

    assert session.host.engine.session.pause_failed_sub_agents is pauses


def _streamed(delta: dict[str, Any], finish_reason: str) -> httpx.Response:
    chunks = [
        {"index": 0, "delta": delta, "finish_reason": None},
        {"index": 0, "delta": {}, "finish_reason": finish_reason},
    ]
    events = "".join(
        "data: "
        + json.dumps({"id": "c1", "object": "chat.completion.chunk", "created": 1, "model": "m", "choices": [choice]})
        + "\n\n"
        for choice in chunks
    )
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=f"{events}data: [DONE]\n\n")


_HELPER_TASK = "Look it up."


@dataclass
class _Delegating:
    """The parent asks the helper once, then answers; every request the helper makes is refused with a 402.

    Requests are told apart by what they carry, never by their order: once the
    turn has succeeded, the session's title refresh asks the model too, in the
    background, and it may come before or after the test reads the requests.
    """

    parent: list[dict[str, Any]]
    helper: list[dict[str, Any]]

    def respond(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if _HELPER in {tool["function"]["name"] for tool in body.get("tools") or []}:
            self.parent.append(body)
            if any(message.get("role") == "tool" for message in body["messages"]):
                return _streamed({"role": "assistant", "content": "Done."}, "stop")
            arguments = json.dumps({"prompt": _HELPER_TASK})
            call = {
                "index": 0,
                "id": "call_1",
                "type": "function",
                "function": {"name": _HELPER, "arguments": arguments},
            }
            return _streamed({"role": "assistant", "content": None, "tool_calls": [call]}, "tool_calls")
        if any(
            message.get("role") == "user" and str(message.get("content")).startswith(_HELPER_TASK)
            for message in body["messages"]
        ):
            self.helper.append(body)
            return httpx.Response(402, json=_DEEPSEEK_402)
        # The title refresh.
        return _streamed({"role": "assistant", "content": "Looking things up"}, "stop")


@pytest.mark.parametrize(
    "declares", [pytest.param(False, id="plain-client"), pytest.param(True, id="declaring-client")]
)
async def test_a_failed_sub_agent_pauses_only_for_a_client_that_declares_it_answers_one(
    acp: AcpFactory, declares: bool
) -> None:
    provider = _Delegating([], [])
    server = acp(provider, delegates=True)
    await server.initialize(
        acp_schema.ClientCapabilities(field_meta={"chrys": {"subAgentPause": True}}) if declares else None
    )
    session_id = await server.open()

    turn = asyncio.create_task(server.prompt(session_id))
    if declares:
        await wait_for(
            lambda: server.notifications("chrys/sub_agent_paused") or turn.done(),
            timeout=ENGINE_TURN_TIMEOUT,
            description="the failed sub-agent paused",
        )
        # Paused, it waits for the client's answer: the turn is still running.
        assert not turn.done()
        (paused,) = server.notifications("chrys/sub_agent_paused")
        assert (paused["kind"], paused["statusCode"]) == ("quota_exhausted", 402)
        await server.server.ext_method(
            "sub_agent/abort", {"sessionId": session_id, "invocationId": paused["invocationId"]}
        )
    response = await asyncio.wait_for(turn, ENGINE_TURN_TIMEOUT)

    assert response.stop_reason == "end_turn"
    assert len(server.notifications("chrys/sub_agent_paused")) == int(declares)
    aborted = server.notifications("chrys/sub_agent_aborted")
    assert [(event["kind"], event["retryable"], event["statusCode"]) for event in aborted] == [
        ("quota_exhausted", False, 402)
    ]
    # Not retried: a spent balance fails again, and this host allows no transient retries.
    assert len(provider.helper) == 1
    # The parent model was told: its second request carries the sub-agent's error as the tool result.
    _, answering = provider.parent
    (result,) = [message for message in answering["messages"] if message.get("role") == "tool"]
    assert "Insufficient Balance" in str(result["content"])


@pytest.mark.parametrize("method", ["chrys/session_runtime", "mcp/list", "skills/list"])
async def test_an_extension_request_that_fails_says_why(acp: AcpFactory, method: str) -> None:
    """A failure no branch handled itself still answers with its cause, not the SDK's "Internal error"."""
    server = acp(_Provider(200, {}, []))
    await server.initialize()

    with pytest.raises(RequestError) as failed:
        await server.server.ext_method(method, {"sessionId": "gone"})

    assert (failed.value.code, str(failed.value)) == (-32602, "ACP session is not active: gone")
    assert failed.value.data == {"details": "ACP session is not active: gone"}


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        pytest.param("Error code: 402 - Insufficient Balance", "Error code: 402 - Insufficient Balance", id="one-line"),
        pytest.param("\n  \nfirst line\nsecond line", "first line", id="first-non-blank-line"),
        pytest.param("x" * 600, "x" * 499 + "…", id="cut"),
        pytest.param("x" * 500, "x" * 500, id="at-the-limit"),
        pytest.param("  \n ", "", id="blank"),
        # A pair split by the cut would leave a lone surrogate, which strict JSON parsers refuse.
        pytest.param("x" * 498 + "\ud83d\ude00" + "x" * 100, "x" * 498 + "\U0001f600…", id="pair-at-the-cut"),
        pytest.param("no such folder: /w/\udcff", "no such folder: /w/\\udcff", id="undecodable-byte"),
    ],
)
def test_a_json_rpc_message_is_one_bounded_line(text: str, expected: str) -> None:
    assert summary(text) == expected


@pytest.mark.parametrize("build", [internal_error, invalid_params], ids=["internal-error", "invalid-params"])
def test_an_error_with_an_undecodable_path_still_reaches_a_strict_client(
    build: Callable[[dict[str, Any]], RequestError],
) -> None:
    """A POSIX path byte that isn't UTF-8 rides as a lone surrogate; the error spells it as its escape."""
    error = build({"details": "working directory no longer exists: /w/\udcff"})

    wire = acp_error_json(error)

    assert wire["message"] == "working directory no longer exists: /w/\\udcff"
    assert wire["data"] == {"details": "working directory no longer exists: /w/\\udcff"}
