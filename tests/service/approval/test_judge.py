# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for ApprovalJudge — verdict parsing, retry loop, and log writing."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, create_autospec, patch

import pytest

from chrys.kernel import ChatResponse, Content, Message
from chrys.service.approval.judge import ApprovalJudge, JudgeVerdict, _assistant_retry_messages, _parse_verdict
from chrys.service.profiles.models.resolver import default_profile
from chrys.service.profiles.models.schema import ModelProfile

# ─── _parse_verdict ─────────────────────────────────────────────────


def test_parse_verdict_approved() -> None:
    verdict = _parse_verdict('{"approved": true, "reason": "read-only"}')
    assert verdict is not None
    assert verdict.approved is True
    assert verdict.reason == "read-only"


def test_parse_verdict_flagged() -> None:
    verdict = _parse_verdict('{"approved": false, "reason": "rm -rf unrelated dir"}')
    assert verdict is not None
    assert verdict.approved is False
    assert verdict.reason == "rm -rf unrelated dir"


def test_parse_verdict_strips_markdown_fence() -> None:
    fenced = '```json\n{"approved": true, "reason": "ok"}\n```'
    verdict = _parse_verdict(fenced)
    assert verdict is not None
    assert verdict.approved is True


def test_parse_verdict_strips_plain_fence() -> None:
    fenced = '```\n{"approved": false, "reason": "bad"}\n```'
    verdict = _parse_verdict(fenced)
    assert verdict is not None
    assert verdict.approved is False


def test_parse_verdict_extracts_json_surrounded_by_text() -> None:
    text = 'I would approve this.\n{"approved": true, "reason": "safe read"}\nDone.'
    verdict = _parse_verdict(text)
    assert verdict is not None
    assert verdict.approved is True
    assert verdict.reason == "safe read"


def test_parse_verdict_conflicting_verdict_objects_retry() -> None:
    text = (
        'Example: {"approved": true, "reason": "example only"}\n'
        'Actual verdict: {"approved": false, "reason": "reads credentials"}'
    )
    assert _parse_verdict(text) is None


def test_parse_verdict_multiple_same_direction_objects_uses_last() -> None:
    text = (
        'Example: {"approved": true, "reason": "example only"}\n'
        'Actual verdict: {"approved": true, "reason": "safe read"}'
    )
    verdict = _parse_verdict(text)
    assert verdict is not None
    assert verdict.approved is True
    assert verdict.reason == "safe read"


def test_parse_verdict_duplicate_approved_conflict_retries() -> None:
    assert _parse_verdict('{"approved": true, "Approved": false, "reason": "conflicting"}') is None


def test_parse_verdict_repairs_missing_closing_brace() -> None:
    verdict = _parse_verdict('{"approved": false, "reason": "deletion requires approval"')
    assert verdict is not None
    assert verdict.approved is False
    assert verdict.reason == "deletion requires approval"


def test_parse_verdict_repairs_missing_string_quote_and_brace() -> None:
    verdict = _parse_verdict('{"approved": true, "reason": "read-only grep')
    assert verdict is not None
    assert verdict.approved is True
    assert verdict.reason == "read-only grep"


def test_parse_verdict_repairs_truncated_positive_verdict() -> None:
    """An explicit approved=true can pass even when the optional reason is truncated."""
    verdict = _parse_verdict('{"approved": true, "reason": "')
    assert verdict is not None
    assert verdict.approved is True
    assert verdict.reason == ""


def test_parse_verdict_accepts_case_drifted_field_names() -> None:
    verdict = _parse_verdict('{"Approved": true, "Reason": "safe"}')
    assert verdict is not None
    assert verdict.approved is True
    assert verdict.reason == "safe"


def test_parse_verdict_invalid_json() -> None:
    assert _parse_verdict("not json") is None
    assert _parse_verdict("") is None


def test_parse_verdict_not_a_dict() -> None:
    assert _parse_verdict("[1, 2, 3]") is None
    assert _parse_verdict('"just a string"') is None


def test_parse_verdict_missing_fields() -> None:
    assert _parse_verdict('{"approved": true}') is None
    assert _parse_verdict('{"reason": "no bool"}') is None


def test_parse_verdict_wrong_types() -> None:
    assert _parse_verdict('{"approved": "yes", "reason": "x"}') is None
    assert _parse_verdict('{"approved": true, "reason": 42}') is None


# ─── Fake chat client for retry-loop tests ─────────────────────────


class _FakeResponse:
    def __init__(self, text: str, messages: list[Message] | None = None) -> None:
        self.text = text
        self.messages = messages or []


class _FakeStream:
    def __init__(self, response: _FakeResponse) -> None:
        self._response = response
        self._done = False

    def __aiter__(self) -> _FakeStream:
        return self

    async def __anext__(self) -> Any:
        if self._done:
            raise StopAsyncIteration
        self._done = True
        return object()

    async def get_final_response(self) -> _FakeResponse:
        return self._response


class _FakeClient:
    """Minimal async chat client returning scripted responses in order."""

    def __init__(self, responses: list[str | _FakeResponse | Exception]) -> None:
        self._responses = [
            response if isinstance(response, _FakeResponse | Exception) else _FakeResponse(response)
            for response in responses
        ]
        self.calls: list[list[Any]] = []
        self.streams: list[bool] = []

    async def get_response(self, messages: list[Any], stream: bool = False, options: Any = None) -> Any:
        self.calls.append(list(messages))
        self.streams.append(stream)
        if not self._responses:
            raise RuntimeError("no more scripted responses")
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        if stream:
            return _FakeStream(response)
        return response


def _make_judge(client: _FakeClient) -> ApprovalJudge:
    judge = ApprovalJudge(default_profile())
    # Bypass lazy client creation by injecting directly
    judge._client = client
    judge._chat_options = None
    return judge


async def test_approval_judge_passes_session_id_to_client() -> None:
    judge = ApprovalJudge(default_profile(), session_id="sess-judge", parent_session_id="parent-judge")

    with patch("chrys.service.llm.clients.create_client", return_value=MagicMock(aclose=AsyncMock())) as create_client:
        await judge._get_client()

    create_client.assert_called_once()
    assert create_client.call_args.kwargs["session_id"] == "sess-judge"
    assert create_client.call_args.kwargs["parent_session_id"] == "parent-judge"


async def test_closing_the_judge_closes_its_client_and_refuses_a_new_one() -> None:
    """The judge's owner closes it; an evaluation racing that close must not reopen a client."""
    from chrys.service.llm import clients as clients_mod

    class _Client:
        closes = 0

        async def aclose(self) -> None:
            self.closes += 1

    client = _Client()
    judge = ApprovalJudge(default_profile())
    with patch.object(clients_mod, "create_client", create_autospec(clients_mod.create_client, return_value=client)):
        assert await judge._get_client() is client
        await judge.aclose()
        await judge.aclose()
        assert client.closes == 1
        with pytest.raises(RuntimeError, match="closed"):
            await judge._get_client()


# ─── evaluate() retry behaviour ────────────────────────────────────


@pytest.mark.asyncio
async def test_evaluate_propagates_client_exception() -> None:
    error = RuntimeError("judge client unavailable")
    judge = _make_judge(_FakeClient([error]))

    with pytest.raises(RuntimeError, match="judge client unavailable") as exc_info:
        await judge.evaluate(
            user_message="read the file",
            tool_name="read_file",
            tool_kind="filesystem.read",
            args={"path": "foo.py"},
            workspace_roots=["/ws"],
        )

    assert exc_info.value is error


@pytest.mark.asyncio
async def test_evaluate_returns_verdict_when_log_write_fails(tmp_path: Path) -> None:
    judge = _make_judge(_FakeClient(['{"approved": true, "reason": "safe read"}']))

    with patch("builtins.open", side_effect=OSError("disk full")):
        verdict = await judge.evaluate(
            user_message="read the file",
            tool_name="read_file",
            tool_kind="filesystem.read",
            args={"path": "foo.py"},
            workspace_roots=["/ws"],
            request_id="req-log-failure",
            log_dir=tmp_path / "approvals",
        )

    assert verdict == JudgeVerdict(approved=True, reason="safe read")


@pytest.mark.asyncio
async def test_evaluate_returns_valid_verdict_on_first_try() -> None:
    client = _FakeClient(['{"approved": true, "reason": "safe read"}'])
    judge = _make_judge(client)

    verdict = await judge.evaluate(
        user_message="read the file",
        tool_name="read_file",
        tool_kind="filesystem.read",
        args={"path": "foo.py"},
        workspace_roots=["/ws"],
    )

    assert verdict.approved is True
    assert verdict.reason == "safe read"
    assert len(client.calls) == 1
    user_prompt = client.calls[0][1].text
    assert "Current time (local): " in user_prompt
    assert "Current time (UTC): " in user_prompt
    assert "Current-turn user prompts:\n1. read the file" in user_prompt
    assert "Latest user prompt:\nread the file" in user_prompt


@pytest.mark.asyncio
async def test_evaluate_includes_all_current_turn_user_messages() -> None:
    client = _FakeClient(['{"approved": true, "reason": "安全读取"}'])
    judge = _make_judge(client)

    verdict = await judge.evaluate(
        user_message="当前代码仓有多少行代码?",
        user_messages=["这个代码仓是做什么的?", "当前代码仓有多少行代码?"],
        tool_name="zsh",
        tool_kind="shell",
        args={"command": "rg --files | xargs wc -l"},
        workspace_roots=["/ws"],
    )

    assert verdict.approved is True
    user_prompt = client.calls[0][1].text
    assert "Current-turn user prompts:\n1. 这个代码仓是做什么的?\n2. 当前代码仓有多少行代码?" in user_prompt
    assert "Latest user prompt:\n当前代码仓有多少行代码?" in user_prompt


@pytest.mark.asyncio
async def test_evaluate_shows_non_ascii_tool_arguments_as_written() -> None:
    client = _FakeClient(['{"approved": true, "reason": "只读"}'])
    judge = _make_judge(client)

    await judge.evaluate(
        user_message="统计报告行数",
        tool_name="zsh",
        tool_kind="shell",
        args={"command": "wc -l 报告.txt"},
        workspace_roots=["/ws"],
    )

    assert '"command": "wc -l 报告.txt"' in client.calls[0][1].text


@pytest.mark.asyncio
async def test_evaluate_uses_model_profile_stream_setting() -> None:
    client = _FakeClient(['{"approved": true, "reason": "safe read"}'])
    profile = default_profile()
    profile.stream = True
    judge = ApprovalJudge(profile)
    judge._client = client
    judge._chat_options = None

    verdict = await judge.evaluate(
        user_message="read the file",
        tool_name="read_file",
        tool_kind="filesystem.read",
        args={"path": "foo.py"},
        workspace_roots=["/ws"],
    )

    assert verdict.approved is True
    assert client.streams == [True]


@pytest.mark.asyncio
async def test_evaluate_retries_on_invalid_response() -> None:
    # First: invalid; second: valid
    client = _FakeClient(
        [
            "not json at all",
            '{"approved": false, "reason": "looks destructive"}',
        ]
    )
    judge = _make_judge(client)

    verdict = await judge.evaluate(
        user_message="do something",
        tool_name="shell",
        tool_kind="shell",
        args={"command": "rm -rf /"},
        workspace_roots=[],
    )

    assert verdict.approved is False
    assert verdict.reason == "looks destructive"
    assert len(client.calls) == 2
    # Second call should include the correction turn (assistant + user)
    # original 2 + assistant + correction-user = 4 messages
    assert len(client.calls[1]) == 4


@pytest.mark.asyncio
async def test_evaluate_retry_preserves_full_assistant_messages() -> None:
    first_response = _FakeResponse(
        "```json\nnot actually json\n```",
        messages=[
            Message(
                "assistant",
                [
                    Content.from_text("Let me think."),
                    Content.from_text_reasoning(
                        protected_data='"judge hidden chain of thought"',
                        additional_properties={"openai_reasoning_format": "reasoning_content"},
                    ),
                ],
            )
        ],
    )
    client = _FakeClient(
        [
            first_response,
            '{"approved": true, "reason": "safe read"}',
        ]
    )
    judge = _make_judge(client)

    verdict = await judge.evaluate(
        user_message="read the file",
        tool_name="read_file",
        tool_kind="filesystem.read",
        args={"path": "foo.py"},
        workspace_roots=["/ws"],
    )

    assert verdict.approved is True
    retry_assistant = client.calls[1][2]
    assert retry_assistant.role == "assistant"
    assert [c.type for c in retry_assistant.contents] == ["text", "text_reasoning"]


# ─── _assistant_retry_messages direct unit tests ───────────────────


def test_assistant_retry_messages_falls_back_to_text_when_no_messages() -> None:
    """When the response object has no structured messages, fall back to plain text.

    Mock clients and any provider that doesn't surface a ``messages`` list
    must still produce a usable retry turn so the judge keeps converging."""

    result = _assistant_retry_messages(ChatResponse(messages=[]), "raw text response")

    assert len(result) == 1
    assert result[0].role == "assistant"
    assert result[0].text == "raw text response"


def test_assistant_retry_messages_returns_empty_when_no_messages_and_no_text() -> None:
    """No structured messages and no text → return empty.

    Better to append nothing than to inject an empty assistant turn the
    model would have to interpret."""

    assert _assistant_retry_messages(ChatResponse(messages=[]), "") == []


def test_assistant_retry_messages_filters_non_assistant_messages() -> None:
    """Only assistant-role messages are echoed; tool/user/system are dropped.

    Replaying a tool/user message back to the model would corrupt the
    conversation shape."""
    response = _FakeResponse(
        "irrelevant",
        messages=[
            Message("user", ["leftover user turn"]),
            Message("assistant", [Content.from_text("real assistant reply")]),
            Message("tool", [Content.from_function_result(call_id="x", result="leak")]),
            Message("system", ["leftover system turn"]),
        ],
    )

    result = _assistant_retry_messages(response, "fallback")

    assert len(result) == 1
    assert result[0].role == "assistant"
    assert result[0].text == "real assistant reply"


def test_assistant_retry_messages_preserves_reasoning_metadata_round_trip() -> None:
    """``additional_properties`` survive ``to_dict()``/``from_dict()`` cloning.

    DeepSeek's serializer keys off ``openai_reasoning_format`` /
    ``reasoning_content`` on both the message and the content block to
    decide whether to replay reasoning on the wire — losing that on
    retry would silently break the strict tool-call replay contract."""
    original = Message(
        "assistant",
        [
            Content.from_text("partial answer"),
            Content.from_text_reasoning(
                protected_data='"hidden chain of thought"',
                additional_properties={"openai_reasoning_format": "reasoning_content"},
            ),
        ],
        additional_properties={
            "reasoning_content": "hidden chain of thought",
            "openai_reasoning_format": "reasoning_content",
        },
    )
    response = _FakeResponse("partial answer", messages=[original])

    result = _assistant_retry_messages(response, "partial answer")

    assert len(result) == 1
    cloned = result[0]
    # Message-level metadata survives the round-trip
    assert cloned.additional_properties["reasoning_content"] == "hidden chain of thought"
    assert cloned.additional_properties["openai_reasoning_format"] == "reasoning_content"
    # Content-level metadata on the text_reasoning block also survives
    reasoning_block = next(c for c in cloned.contents if c.type == "text_reasoning")
    assert reasoning_block.protected_data == '"hidden chain of thought"'
    assert reasoning_block.additional_properties["openai_reasoning_format"] == "reasoning_content"
    # Cloned, not the same object — caller can mutate without affecting source
    assert cloned is not original


def test_assistant_retry_messages_preserves_multiple_assistant_messages() -> None:
    """All assistant messages are echoed in order — providers may emit
    several assistant turns (e.g. when tool calls are interleaved)."""
    response = _FakeResponse(
        "joined text",
        messages=[
            Message("assistant", [Content.from_text("first")]),
            Message("assistant", [Content.from_text("second")]),
        ],
    )

    result = _assistant_retry_messages(response, "joined text")

    assert [m.text for m in result] == ["first", "second"]


@pytest.mark.asyncio
async def test_evaluate_failsafe_after_exhausted_retries() -> None:
    # Three invalid responses (MAX_RETRIES=2 → 3 attempts total)
    client = _FakeClient(["bad1", "bad2", "bad3"])
    judge = _make_judge(client)

    verdict = await judge.evaluate(
        user_message="x",
        tool_name="t",
        tool_kind="k",
        args={},
        workspace_roots=[],
    )

    assert verdict.approved is False
    assert "invalid response" in verdict.reason.lower()
    assert len(client.calls) == 3


@pytest.mark.asyncio
async def test_evaluate_writes_log_when_requested(tmp_path: Path) -> None:
    client = _FakeClient(['{"approved": true, "reason": "ok"}'])
    judge = _make_judge(client)

    log_dir = tmp_path / "approvals"
    await judge.evaluate(
        user_message="u",
        tool_name="read_file",
        tool_kind="filesystem.read",
        args={"path": "a.py"},
        workspace_roots=["/w"],
        request_id="req123",
        log_dir=log_dir,
    )

    log_file = log_dir / "req123.log"
    assert log_file.exists()
    text = log_file.read_text()
    assert "ATTEMPT 0" in text
    assert "--- SYSTEM ---" not in text
    assert "--- USER ---" in text
    assert "--- RESPONSE ---" in text
    assert "APPROVED: ok" in text


@pytest.mark.asyncio
async def test_evaluate_log_records_retries_and_fallback(tmp_path: Path) -> None:
    client = _FakeClient(["bad1", "bad2", "bad3"])
    judge = _make_judge(client)

    log_dir = tmp_path / "approvals"
    await judge.evaluate(
        user_message="",
        tool_name="t",
        tool_kind="k",
        args={},
        workspace_roots=[],
        request_id="r1",
        log_dir=log_dir,
    )

    text = (log_dir / "r1.log").read_text()
    # 3 attempts + final fallback entry
    assert text.count("ATTEMPT 0") == 1
    assert text.count("ATTEMPT 1") == 1
    assert text.count("ATTEMPT 2") == 1
    assert text.count("ATTEMPT 3") == 1  # fallback
    assert "(unparsed)" in text
    assert "FLAGGED: Judge returned invalid response" in text


@pytest.mark.asyncio
async def test_evaluate_no_log_when_request_id_missing(tmp_path: Path) -> None:
    client = _FakeClient(['{"approved": true, "reason": "ok"}'])
    judge = _make_judge(client)

    log_dir = tmp_path / "approvals"
    await judge.evaluate(
        user_message="u",
        tool_name="t",
        tool_kind="k",
        args={},
        workspace_roots=[],
        request_id="",  # no request_id → no log
        log_dir=log_dir,
    )

    assert not log_dir.exists()


# ─── JudgeVerdict dataclass ────────────────────────────────────────


def test_judge_verdict_construction() -> None:
    v = JudgeVerdict(approved=True, reason="ok")
    assert v.approved is True
    assert v.reason == "ok"


# ─── Profile resolution (resolve_judge_profile) ────────────────────


def test_resolve_judge_profile_uses_fallback_without_registry() -> None:
    from chrys.foundation.config.settings import Settings
    from chrys.service.profiles.models.resolver import resolve_judge_profile
    from chrys.service.profiles.models.schema import ModelProfile

    fallback = ModelProfile(id="agent-1", name="Agent", provider="openai", model_id="gpt-5.4")
    resolved = resolve_judge_profile(None, Settings.from_env(), fallback)
    assert resolved is fallback


def test_resolve_judge_profile_prefers_judge_profile_over_fallback() -> None:
    from chrys.foundation.config.settings import Settings
    from chrys.service.profiles.models.registry import ModelProfileRegistry
    from chrys.service.profiles.models.resolver import resolve_judge_profile
    from chrys.service.profiles.models.schema import ModelProfile

    registry = ModelProfileRegistry()
    agent_profile = ModelProfile(id="agent-1", name="Agent", provider="openai", model_id="gpt-5.4")
    judge_profile = ModelProfile(id="judge-1", name="Judge", provider="anthropic", model_id="claude-haiku-4-5")
    registry.register(agent_profile)
    registry.register(judge_profile)

    settings = Settings.from_env()
    settings = replace(settings, approval_judge_model_profile="judge-1")

    resolved = resolve_judge_profile(registry, settings, agent_profile)
    assert resolved is judge_profile


def test_resolve_judge_profile_falls_back_when_unset() -> None:
    from chrys.foundation.config.settings import Settings
    from chrys.service.profiles.models.registry import ModelProfileRegistry
    from chrys.service.profiles.models.resolver import resolve_judge_profile
    from chrys.service.profiles.models.schema import ModelProfile

    registry = ModelProfileRegistry()
    agent_profile = ModelProfile(id="agent-1", name="Agent", provider="openai", model_id="gpt-5.4")
    registry.register(agent_profile)

    settings = Settings.from_env()
    settings = replace(settings, approval_judge_model_profile="")  # not set → fall back

    resolved = resolve_judge_profile(registry, settings, agent_profile)
    assert resolved is agent_profile


def test_resolve_judge_profile_missing_id_falls_back() -> None:
    from chrys.foundation.config.settings import Settings
    from chrys.service.profiles.models.registry import ModelProfileRegistry
    from chrys.service.profiles.models.resolver import resolve_judge_profile
    from chrys.service.profiles.models.schema import ModelProfile

    registry = ModelProfileRegistry()  # empty
    fallback = ModelProfile(id="agent-1", name="Agent", provider="openai", model_id="gpt-5.4")

    settings = Settings.from_env()
    settings = replace(settings, approval_judge_model_profile="does-not-exist")

    resolved = resolve_judge_profile(registry, settings, fallback)
    assert resolved is fallback


# ─── Relay-aware resolution: a relay main model must not judge itself ───


def test_relay_enhanced_user_prompt_carries_the_security_context() -> None:
    from chrys.service.approval.review_types import (
        RELAY_REVIEW_POLICY_VERSION,
        ApprovalJudgeInput,
        build_user_prompt,
    )

    def _request(**overrides: object) -> ApprovalJudgeInput:
        fields: dict[str, object] = {
            "tool_name": "zsh",
            "tool_kind": "shell",
            "args": {"command": "make build"},
            "user_message": "ship it",
            "user_messages": ["ship it"],
            "workspace_roots": ["/workspace"],
        }
        fields.update(overrides)
        return ApprovalJudgeInput(**fields)  # type: ignore[arg-type]

    enhanced = build_user_prompt(_request(relay_enhanced=True, reviewed_model="gpt-relay-x"))
    assert "RELAY RISK" in enhanced
    assert "untrusted evidence" in enhanced
    assert "gpt-relay-x" in enhanced
    assert RELAY_REVIEW_POLICY_VERSION in enhanced

    assert "RELAY RISK" not in build_user_prompt(_request())


def test_relay_enhanced_prompt_with_latched_history_names_no_relayed_working_model() -> None:
    """History-latched risk describes the conversation, not a relayed current model."""
    from chrys.service.approval.review_types import ApprovalJudgeInput, build_user_prompt

    request = ApprovalJudgeInput(
        tool_name="zsh",
        tool_kind="shell",
        args={"command": "make build"},
        user_message="ship it",
        user_messages=["ship it"],
        workspace_roots=["/workspace"],
        relay_enhanced=True,
        reviewed_model="",
    )

    prompt = build_user_prompt(request)

    assert "RELAY RISK" in prompt
    assert "already carries content produced through a third-party relay" in prompt
    assert "untrusted evidence" in prompt
    # No claim that the working model's traffic is relayed, and no model named.
    assert "working model's traffic is routed" not in prompt
    assert "Working model under review" not in prompt


def _relay_profile(**overrides: object) -> ModelProfile:
    fields: dict[str, object] = {
        "id": "relay-1",
        "name": "Relay",
        "provider": "openai",
        "model_id": "gpt-x",
        "base_url": "https://relay.example.com/v1",
        "api_key": "sk-relay",
        "is_relay": True,
    }
    fields.update(overrides)
    return ModelProfile(**fields)  # type: ignore[arg-type]


def _direct_profile(**overrides: object) -> ModelProfile:
    fields: dict[str, object] = {
        "id": "judge-1",
        "name": "Judge",
        "provider": "openai",
        "model_id": "gpt-y",
        "base_url": "https://direct.example.com/v1",
        "api_key": "sk-judge",
    }
    fields.update(overrides)
    return ModelProfile(**fields)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("selector", "registry_profiles"),
    [
        pytest.param("", [], id="unset-selector"),
        pytest.param("does-not-exist", [], id="unresolved-selector"),
        pytest.param("relay-2", ["relay-2"], id="judge-is-another-relay"),
        pytest.param("same-host", ["same-host"], id="judge-shares-the-relay-host"),
    ],
)
def test_resolve_judge_profile_relay_fallback_without_independent_judge_defers_to_user(
    selector: str, registry_profiles: list[str]
) -> None:
    from chrys.foundation.config.settings import Settings
    from chrys.service.profiles.models.registry import ModelProfileRegistry
    from chrys.service.profiles.models.resolver import resolve_judge_profile

    registry = ModelProfileRegistry()
    registry.register(_relay_profile(id="relay-2", name="Relay 2", base_url="https://other.example.com/v1"))
    registry.register(_direct_profile(id="same-host", name="Same Host", base_url="https://relay.example.com/other"))
    fallback = _relay_profile()

    settings = replace(Settings.from_env(), approval_judge_model_profile=selector)

    assert resolve_judge_profile(registry, settings, fallback) is None


def test_resolve_judge_profile_relay_fallback_with_independent_judge_uses_it() -> None:
    from chrys.foundation.config.settings import Settings
    from chrys.service.profiles.models.registry import ModelProfileRegistry
    from chrys.service.profiles.models.resolver import resolve_judge_profile

    registry = ModelProfileRegistry()
    judge = _direct_profile()
    registry.register(judge)
    fallback = _relay_profile()

    settings = replace(Settings.from_env(), approval_judge_model_profile="judge-1")

    assert resolve_judge_profile(registry, settings, fallback) is judge


def test_resolve_judge_profile_direct_fallback_keeps_the_explicit_choice() -> None:
    # A direct main model may explicitly choose any judge, relay included —
    # the relay restriction exists to protect traffic the relay carries.
    from chrys.foundation.config.settings import Settings
    from chrys.service.profiles.models.registry import ModelProfileRegistry
    from chrys.service.profiles.models.resolver import resolve_judge_profile

    registry = ModelProfileRegistry()
    chosen = _relay_profile()
    registry.register(chosen)
    fallback = _direct_profile()

    settings = replace(Settings.from_env(), approval_judge_model_profile="relay-1")

    assert resolve_judge_profile(registry, settings, fallback) is chosen


def test_resolve_judge_profile_latched_history_rejects_a_relay_judge() -> None:
    # A restored session with relay_risk_seen=True keeps relay-era content
    # untrusted: even with a direct current model, a judge that is itself a
    # relay must never review that traffic.
    from chrys.foundation.config.settings import Settings
    from chrys.service.profiles.models.registry import ModelProfileRegistry
    from chrys.service.profiles.models.resolver import resolve_judge_profile

    registry = ModelProfileRegistry()
    registry.register(_relay_profile(id="judge-relay", name="Judge Relay"))
    fallback = _direct_profile()

    settings = replace(Settings.from_env(), approval_judge_model_profile="judge-relay")

    assert resolve_judge_profile(registry, settings, fallback, relay_history_risk=True) is None


def test_resolve_judge_profile_latched_history_rejects_a_mock_judge() -> None:
    from chrys.foundation.config.settings import Settings
    from chrys.service.profiles.models.registry import ModelProfileRegistry
    from chrys.service.profiles.models.resolver import resolve_judge_profile

    registry = ModelProfileRegistry()
    registry.register(_direct_profile(id="mock-judge", name="Mock Judge", provider="mock"))
    fallback = _direct_profile()

    settings = replace(Settings.from_env(), approval_judge_model_profile="mock-judge")

    assert resolve_judge_profile(registry, settings, fallback, relay_history_risk=True) is None


def test_resolve_judge_profile_latched_history_keeps_follow_active_on_a_direct_model() -> None:
    # Unset selector with a direct current model: "follow the active model"
    # lands on the direct active profile — a sane direct service, kept.
    from chrys.foundation.config.settings import Settings
    from chrys.service.profiles.models.resolver import resolve_judge_profile

    fallback = _direct_profile()
    settings = replace(Settings.from_env(), approval_judge_model_profile="")

    assert resolve_judge_profile(None, settings, fallback, relay_history_risk=True) is fallback


def test_resolve_judge_profile_latched_history_allows_same_host_direct_judge() -> None:
    # The historical relay profile is unknown, so with a direct current model
    # there is no host to compare against: a judge sharing the current DIRECT
    # model's host stays usable — only the judge's own directness is enforced.
    from chrys.foundation.config.settings import Settings
    from chrys.service.profiles.models.registry import ModelProfileRegistry
    from chrys.service.profiles.models.resolver import resolve_judge_profile

    registry = ModelProfileRegistry()
    judge = _direct_profile(id="same-host", name="Same Host", base_url="https://direct.example.com/other")
    registry.register(judge)
    fallback = _direct_profile()

    settings = replace(Settings.from_env(), approval_judge_model_profile="same-host")

    assert resolve_judge_profile(registry, settings, fallback, relay_history_risk=True) is judge


def test_resolve_judge_profile_relay_fallback_with_latched_history_uses_independent_judge() -> None:
    # Current relay plus latched history: the full qualification applies and
    # an independent judge still wins.
    from chrys.foundation.config.settings import Settings
    from chrys.service.profiles.models.registry import ModelProfileRegistry
    from chrys.service.profiles.models.resolver import resolve_judge_profile

    registry = ModelProfileRegistry()
    judge = _direct_profile()
    registry.register(judge)
    fallback = _relay_profile()

    settings = replace(Settings.from_env(), approval_judge_model_profile="judge-1")

    assert resolve_judge_profile(registry, settings, fallback, relay_history_risk=True) is judge
