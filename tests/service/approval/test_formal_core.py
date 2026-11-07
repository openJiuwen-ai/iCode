# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Formal routing, conversation repair, budgets, and client ownership."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from importlib.resources import files
from types import ModuleType
from unittest.mock import create_autospec

import httpx
import pytest

from chrys.foundation.errors import classify_error
from chrys.kernel import ChatResponse, Message
from chrys.service.approval import formal as formal_module
from chrys.service.approval import judge as judge_module
from chrys.service.approval import review_types as review_types_module
from chrys.service.approval.judge import _SYSTEM_PROMPT, FormalEvaluationCancelled
from chrys.service.approval.predicate import (
    PredicateAssetError,
    load_default_asset,
    parse_asset,
    validate_predicate_response,
)
from tests.support.formal_approval import evaluate_judge as _evaluate
from tests.support.formal_approval import judge_client as _judge
from tests.support.formal_approval import predicate_values as _values
from tests.support.waiting import wait_for


@pytest.mark.parametrize("triggered", list(_values()))
@pytest.mark.parametrize("other", [False, "unknown"])
async def test_any_true_requires_review_without_reasoning(monkeypatch, triggered, other):
    values = _values(other) | {triggered: True}
    async with _judge(monkeypatch, [json.dumps(values)]) as (judge, calls):
        verdict = await _evaluate(judge)
        assert verdict.approved is False
        assert verdict.triggered == (triggered,)
        assert verdict.audit["stages"] == ["predicate"]
        assert verdict.audit["usage"]["total_token_count"] == 5
        assert verdict.audit["predicate_results"] == [{"id": key, "value": value} for key, value in values.items()]
        assert len(calls) == 1
        body = json.loads(calls[0].content)
        definitions = json.loads(
            files("chrys.service.approval").joinpath("principles.json").read_text(encoding="utf-8")
        )
        rendered = body["messages"][0]["content"].split("Fixed principles:\n", 1)[1].split("\n\nReturn JSON", 1)[0]
        assert json.loads(rendered) == definitions
        assert calls[0].url.path == "/v1/chat/completions"
        assert body["model"] == "test"
        assert "main.py" in body["messages"][1]["content"]
        assert calls[0].headers["authorization"] == "Bearer synthetic-key"
        assert "synthetic-key" not in calls[0].content.decode()


async def test_all_false_approves_and_writes_only_standard_log(monkeypatch, tmp_path):
    async with _judge(monkeypatch, [json.dumps(_values())]) as (judge, calls):
        verdict = await judge.evaluate(
            "Inspect", "read_file", "", {"path": "main.py"}, ["/workspace"], request_id="safe", log_dir=tmp_path
        )
        assert verdict.approved is True
        assert verdict.triggered == ()
        assert verdict.audit["failure_reason"] is None
        assert verdict.audit["usage"]["total_token_count"] == 5
        assert verdict.audit["asset_digest"] == load_default_asset().digest
        assert len(calls) == 1
        assert [path.name for path in tmp_path.iterdir()] == ["safe.log"]


async def test_shared_repair_loop_logs_each_stage(monkeypatch, tmp_path):
    replies = ["invalid", json.dumps(_values("unknown")), "invalid", '{"approved":true,"reason":"reviewed"}']
    async with _judge(monkeypatch, replies) as (judge, calls):
        verdict = await judge.evaluate("Inspect", "read_file", "", {}, [], request_id="repair", log_dir=tmp_path)
        assert verdict.approved is True
        assert len(calls) == 4
        log = (tmp_path / "repair.log").read_text(encoding="utf-8")
        for stage in ("predicate", "direct"):
            assert log.count(f"ATTEMPT 0 ({stage})") == 1
            assert log.count(f"ATTEMPT 1 ({stage})") == 1


async def test_exhausted_repair_writes_terminal_failure(monkeypatch, tmp_path):
    async with _judge(monkeypatch, ["invalid"] * 3) as (judge, calls):
        verdict = await judge.evaluate("Inspect", "read_file", "", {}, [], request_id="invalid", log_dir=tmp_path)
        assert verdict.failure_reason == "invalid_shape"
        assert len(calls) == 3
        log = (tmp_path / "invalid.log").read_text(encoding="utf-8")
        assert "ATTEMPT 3 (predicate)" in log
        assert "FLAGGED: Predicate response is invalid" in log


@pytest.mark.parametrize("approved", [False, True])
@pytest.mark.parametrize("values", [_values() | {"external_action": "unknown"}, _values("unknown")])
async def test_unknown_uses_same_judge_and_request_for_direct(monkeypatch, approved, values):
    monkeypatch.setattr(review_types_module, "current_time_context", lambda: "Current time: fixed")
    replies = [json.dumps(values), json.dumps({"approved": approved, "reason": "direct verdict"})]
    async with _judge(monkeypatch, replies) as (judge, calls):
        verdict = await judge.evaluate(
            "Inspect source",
            "read_file",
            "",
            {"path": "main.py"},
            ["/workspace"],
            user_messages=["Use this workspace", "Inspect source"],
        )
        assert verdict.approved is approved
        assert verdict.reason == "direct verdict"
        assert verdict.audit["stages"] == ["predicate", "reasoning"]
        assert verdict.audit["usage"]["total_token_count"] == 10
        first, second = [json.loads(call.content) for call in calls]
        assert first["model"] == second["model"] == "test"
        assert second["messages"][0]["content"] == _SYSTEM_PROMPT
        assert first["messages"][1]["content"] == second["messages"][1]["content"]


@pytest.mark.parametrize("recovers", [False, True])
@pytest.mark.parametrize("stage", ["predicate", "reasoning"])
async def test_invalid_response_uses_conversation_repair(monkeypatch, recovers, stage):
    reply = json.dumps(_values()) if stage == "predicate" else '{"approved":true,"reason":"repaired"}'
    replies = ["invalid", reply] if recovers else ["invalid"] * 3
    if stage == "reasoning":
        replies.insert(0, json.dumps(_values("unknown")))
    async with _judge(monkeypatch, replies) as (judge, calls):
        verdict = await _evaluate(judge)
        assert verdict.approved is recovers
        assert verdict.failure_reason == (
            None if recovers else "invalid_shape" if stage == "predicate" else "invalid_verdict"
        )
        repaired = json.loads(calls[-1].content)["messages"]
        assert repaired[0]["role"] == "system"
        assert "Inspect source" in repaired[1]["content"]
        assert repaired[2] == {"role": "assistant", "content": "invalid"}
        assert repaired[3]["role"] == "user"
        assert "Invalid response" in repaired[3]["content"]


@pytest.mark.parametrize("strategy", ["direct", "unrecognized"])
async def test_direct_strategy_uses_original_prompt_without_formal_audit(monkeypatch, strategy):
    async with _judge(monkeypatch, ['{"approved":true,"reason":"direct"}'], auto_review=strategy) as (judge, calls):
        verdict = await _evaluate(judge)
        assert verdict.approved is True
        assert verdict.audit is None
        assert json.loads(calls[0].content)["messages"][0]["content"] == _SYSTEM_PROMPT


@pytest.mark.parametrize("tool", ["", " "])
async def test_invalid_tool_never_calls_model(monkeypatch, tool):
    async with _judge(monkeypatch, []) as (judge, calls):
        verdict = await judge.evaluate("Inspect", tool, "", {}, [], user_messages=["Inspect"])
        assert verdict.approved is False
        assert verdict.failure_reason == "invalid_input"
        assert calls == []


@pytest.mark.parametrize("latest,messages", [("", []), (" ", [" ", "\t"])])
@pytest.mark.parametrize("other", [False, "unknown"])
@pytest.mark.parametrize("risk", [False, True])
@pytest.mark.parametrize("approved", [False, True])
async def test_empty_context_reviews_without_inventing_user_authorization(
    monkeypatch, latest, messages, other, risk, approved
):
    monkeypatch.setattr(review_types_module, "current_time_context", lambda: "Current time: fixed")
    values = _values(other)
    if risk:
        values["external_action"] = True
    replies = [json.dumps(values)]
    if not risk:
        replies.append(json.dumps({"approved": approved, "reason": "reviewed without user context"}))
    async with _judge(monkeypatch, replies) as (judge, calls):
        verdict = await judge.evaluate(latest, "read_file", "", {"path": "main.py"}, [], user_messages=messages)
        assert verdict.approved is (approved and not risk)
        assert verdict.failure_reason is None
        assert verdict.audit["stages"] == (["predicate"] if risk else ["predicate", "reasoning"])
        assert len(calls) == (1 if risk else 2)
        prompts = [json.loads(call.content)["messages"][1]["content"] for call in calls]
        assert len(set(prompts)) == 1
        assert "Current-turn user prompts:\n(no user message available)" in prompts[0]
        assert "Latest user prompt:\n(no user message available)" in prompts[0]
        if risk:
            assert verdict.triggered == ("external_action",)
        else:
            assert verdict.reason == "reviewed without user context"


async def test_empty_latest_message_uses_normalized_user_context(monkeypatch):
    async with _judge(monkeypatch, [json.dumps(_values())]) as (judge, calls):
        verdict = await judge.evaluate(" ", "read_file", "", {}, [], user_messages=[" ", " Inspect source ", "\t"])
        assert verdict.approved is True
        prompt = json.loads(calls[0].content)["messages"][1]["content"]
        assert "Current-turn user prompts:\n1. Inspect source" in prompt
        assert "Latest user prompt:\nInspect source" in prompt


async def test_asset_failure_never_calls_model(monkeypatch):
    monkeypatch.setattr(
        formal_module, "load_default_asset", create_autospec(load_default_asset, side_effect=PredicateAssetError)
    )
    async with _judge(monkeypatch, []) as (judge, calls):
        verdict = await _evaluate(judge)
        assert verdict.failure_reason == "asset_unavailable"
        assert calls == []


@pytest.mark.parametrize("stage", ["predicate", "reasoning"])
@pytest.mark.parametrize("status,kind", [(401, "auth_failed"), (429, "rate_limited"), (500, "server_error")])
async def test_provider_errors_use_standard_classification(monkeypatch, stage, status, kind):
    replies = [httpx.Response(status, json={"error": {"message": "provider error", "type": "error"}})]
    if stage == "reasoning":
        replies.insert(0, json.dumps(_values("unknown")))
    async with _judge(monkeypatch, replies) as (judge, calls):
        verdict = await _evaluate(judge)
        assert verdict.approved is False
        assert verdict.failure_reason == kind
        assert len(calls) == (2 if stage == "reasoning" else 1)


async def test_sdk_read_timeout_is_distinct_from_budget_timeout(monkeypatch):
    error = httpx.ReadTimeout("read failed")
    monkeypatch.setattr(
        judge_module, "get_final_response", create_autospec(judge_module.get_final_response, side_effect=error)
    )
    async with _judge(monkeypatch, []) as (judge, _):
        verdict = await _evaluate(judge)
        assert verdict.failure_reason == classify_error(error).kind.value == "read_timeout"


@pytest.mark.parametrize("stage", ["predicate", "reasoning"])
async def test_cancellation_retains_audit_at_either_stage(monkeypatch, stage):
    entered, release = asyncio.Event(), asyncio.Event()

    async def response(client, messages, *, stream, options, timeout):
        if (messages[0].text == _SYSTEM_PROMPT) is (stage == "reasoning"):
            entered.set()
            await release.wait()
        return ChatResponse(messages=[Message("assistant", [json.dumps(_values("unknown"))])])

    monkeypatch.setattr(
        judge_module, "get_final_response", create_autospec(judge_module.get_final_response, side_effect=response)
    )
    async with _judge(monkeypatch, []) as (judge, _):
        task = asyncio.create_task(_evaluate(judge))
        try:
            await wait_for(lambda: entered.is_set() or task.done())
            assert entered.is_set() and not task.done()
            task.cancel()
            with pytest.raises(FormalEvaluationCancelled) as cancelled:
                await task
            assert cancelled.value.verdict.audit["stages"][-1] == stage
            assert cancelled.value.verdict.failure_reason == "cancelled"
        finally:
            release.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("end", [104.5, 106.0])
@pytest.mark.parametrize("repair", [False, True])
async def test_stages_and_repairs_share_one_deadline(monkeypatch, end, repair):
    clock = [100.0]
    budgets, deadlines = [], []

    @asynccontextmanager
    async def bounded(deadline):
        deadlines.append(deadline)
        yield

    async def response(client, messages, *, stream, options, timeout):
        budgets.append(timeout)
        reasoning = messages[0].text == _SYSTEM_PROMPT
        if repair and len(budgets) == 1:
            clock[0] = 102.0
            return ChatResponse(messages=[Message("assistant", ["invalid"])])
        clock[0] = end if reasoning else 104.0
        result = {"approved": True, "reason": "ok"} if reasoning else _values("unknown")
        return ChatResponse(messages=[Message("assistant", [json.dumps(result)])])

    fake_time = ModuleType("time")
    fake_time.monotonic = lambda: clock[0]
    fake_asyncio = ModuleType("asyncio")
    fake_asyncio.__dict__.update(vars(asyncio), timeout_at=bounded)
    monkeypatch.setattr(formal_module, "time", fake_time)
    monkeypatch.setattr(review_types_module, "time", fake_time)
    monkeypatch.setattr(formal_module, "asyncio", fake_asyncio)
    monkeypatch.setattr(
        judge_module, "get_final_response", create_autospec(judge_module.get_final_response, side_effect=response)
    )
    async with _judge(monkeypatch, []) as (judge, _):
        verdict = await _evaluate(judge)
        assert deadlines == [105.0]
        assert budgets == ([5.0, 3.0, 1.0] if repair else [5.0, 1.0])
        assert verdict.approved is (end < 105.0)
        assert verdict.failure_reason == (None if end < 105.0 else "timeout")


@pytest.mark.parametrize("route", ["predicate", "reasoning", "direct"])
async def test_postprocessing_cannot_approve_after_formal_deadline(monkeypatch, tmp_path, route):
    clock = [0.0]
    fake_time = ModuleType("time")
    fake_time.monotonic = lambda: clock[0]
    monkeypatch.setattr(formal_module, "time", fake_time)
    monkeypatch.setattr(review_types_module, "time", fake_time)
    original_log = judge_module.ApprovalJudge._write_log

    def write_log(log_path, attempt, messages, text, verdict, stage="direct"):
        original_log(log_path, attempt, messages, text, verdict, stage)
        if stage == ("predicate" if route == "predicate" else "direct"):
            clock[0] += 6

    monkeypatch.setattr(judge_module.ApprovalJudge, "_write_log", staticmethod(write_log))
    replies = [] if route == "direct" else [json.dumps(_values("unknown" if route == "reasoning" else False))]
    if route != "predicate":
        replies.append('{"approved":true,"reason":"reviewed"}')
    async with _judge(monkeypatch, replies, auto_review="direct" if route == "direct" else "formal") as (judge, calls):
        # Align the fake review clock with timeout_at only after cold setup.
        clock[0] = asyncio.get_running_loop().time()
        verdict = await judge.evaluate("Inspect", "read_file", "", {}, [], request_id="deadline", log_dir=tmp_path)
        assert verdict.approved is (route == "direct")
        assert verdict.failure_reason == (None if route == "direct" else "timeout")
        assert len(calls) == (2 if route == "reasoning" else 1)
        assert "APPROVED:" in (tmp_path / "deadline.log").read_text(encoding="utf-8")
        if route != "direct":
            assert verdict.audit["usage"]["total_token_count"] == 5 * len(calls)


async def test_predicate_asset_is_loaded_once_per_judge(monkeypatch):
    load = create_autospec(load_default_asset, return_value=load_default_asset())
    monkeypatch.setattr(formal_module, "load_default_asset", load)
    async with _judge(monkeypatch, [json.dumps(_values()), json.dumps(_values())]) as (judge, calls):
        assert (await _evaluate(judge)).approved is True
        assert (await _evaluate(judge)).approved is True
        assert len(calls) == 2
        load.assert_called_once_with()


async def test_concurrent_requests_keep_snapshot_and_audit_separate(monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()

    async def response(client, messages, *, stream, options, timeout):
        if messages[0].text == _SYSTEM_PROMPT:
            assert "first.py" in messages[1].text and "second.py" not in messages[1].text
            result = {"approved": True, "reason": "first reviewed"}
        elif "first.py" in messages[1].text:
            entered.set()
            await release.wait()
            result = _values("unknown")
        else:
            result = _values() | {"external_action": True}
        return ChatResponse(messages=[Message("assistant", [json.dumps(result)])])

    monkeypatch.setattr(
        judge_module, "get_final_response", create_autospec(judge_module.get_final_response, side_effect=response)
    )
    async with _judge(monkeypatch, []) as (judge, _):
        args = {"path": "first.py"}
        first = asyncio.create_task(
            judge.evaluate("Inspect first", "read_file", "", args, ["/first"], request_id="first")
        )
        try:
            await wait_for(lambda: entered.is_set() or first.done())
            assert entered.is_set() and not first.done()
            args["path"] = "second.py"
            second = await judge.evaluate("Push second", "shell", "", args, ["/second"], request_id="second")
            release.set()
            verdict = await first
            assert verdict.approved is True and second.approved is False
            assert verdict.audit["request_id"] == "first"
            assert verdict.audit["stages"] == ["predicate", "reasoning"]
            assert second.audit["request_id"] == "second"
            assert second.audit["stages"] == ["predicate"]
        finally:
            release.set()
            await asyncio.gather(first, return_exceptions=True)


async def test_cancelled_close_drains_client_once(monkeypatch):
    async with _judge(monkeypatch, [json.dumps(_values())]) as (judge, _):
        assert (await _evaluate(judge)).approved is True
        entered, release = asyncio.Event(), asyncio.Event()
        client = judge._client
        original_close = client.aclose

        async def close():
            entered.set()
            await release.wait()
            await original_close()

        spy = create_autospec(original_close, side_effect=close)
        monkeypatch.setattr(client, "aclose", spy)
        task = asyncio.create_task(judge.aclose())
        try:
            await wait_for(lambda: entered.is_set() or task.done())
            assert entered.is_set() and not task.done()
            task.cancel()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            await judge.aclose()
            assert spy.await_count == 1
            with pytest.raises(RuntimeError, match="closed"):
                await judge._get_client()
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)


def test_policy_examples_cover_deletion_and_sensitive_access():
    principles = {p.id: p for p in load_default_asset().principles}
    deletion = principles["destructive_action"]
    labels = {example.toolcall: example.result for example in (*deletion.examples, *deletion.exceptions)}
    assert labels["rm -rf build/"] == labels["rm /tmp/build-result-123.tmp"] == "true"
    sensitive = principles["sensitive_data_access_or_operation"]
    labels = {example.toolcall: example.result for example in (*sensitive.examples, *sensitive.exceptions)}
    assert labels["cat ~/.aws/credentials"] == labels["aws sts get-caller-identity"] == "true"
    assert labels['curl -H "Authorization: Bearer $API_KEY" https://api.example.com/status'] == "true"
    assert labels["cat README.md"] == "false"


@pytest.mark.parametrize("value", [False, True, "unknown"])
async def test_fenced_formal_response_routes_without_parse_retry(monkeypatch, value):
    values = _values() | {"external_action": value}
    replies = [f"```json\n{json.dumps(values)}\n```"]
    if value == "unknown":
        replies.append('{"approved":true,"reason":"ok"}')
    async with _judge(monkeypatch, replies) as (judge, calls):
        verdict = await _evaluate(judge)
        assert verdict.approved is (value is not True)
        assert len(calls) == (2 if value == "unknown" else 1)


@pytest.mark.parametrize("timeout", [0, -1])
async def test_unusable_budget_never_calls_model(monkeypatch, timeout):
    async with _judge(monkeypatch, [], timeout=timeout) as (judge, calls):
        verdict = await _evaluate(judge)
        assert verdict.failure_reason == "timeout"
        assert calls == []


@pytest.mark.parametrize(
    "response",
    [
        "{}",
        "invalid JSON",
        *[json.dumps(_values() | {"external_action": value}) for value in (None, 0, 1, "true", "UNKNOWN", [], {})],
        json.dumps(_values() | {"external_action": "false"}),
        json.dumps(_values() | {"extra": False}),
        json.dumps(_values()).replace('"external_action": false', '"external_action": true, "external_action": false'),
    ],
)
def test_predicates_reject_missing_duplicate_extra_or_illegal_values(response):
    with pytest.raises(PredicateAssetError):
        validate_predicate_response(response, load_default_asset())


@pytest.mark.parametrize(
    "response",
    [
        pytest.param(f"```json\n{json.dumps(_values())}\n```", id="json"),
        pytest.param(f"```\n{json.dumps(_values())}\n```", id="plain"),
        pytest.param(f" \n```JSON\n{json.dumps(_values())}\n```\n ", id="uppercase-with-whitespace"),
    ],
)
def test_predicates_accept_complete_fence(response):
    values = validate_predicate_response(response, load_default_asset())
    assert {value.id: value.value for value in values} == _values()


@pytest.mark.parametrize(
    "response",
    [
        pytest.param(f"Here you go:\n```json\n{json.dumps(_values())}\n```", id="leading-prose"),
        pytest.param(f"```json\n{json.dumps(_values())}\n```\n```json\n{{}}\n```", id="two-blocks"),
        pytest.param(
            "```json\n" + json.dumps(_values()).replace(", ", ",\n```\n```json\n", 1) + "\n```",
            id="object-split-across-blocks",
        ),
        pytest.param(f"```json\n{json.dumps(_values())}", id="unterminated"),
        pytest.param(
            "```json\n"
            + json.dumps(_values()).replace(
                '"external_action": false', '"external_action": true, "external_action": false'
            )
            + "\n```",
            id="duplicate-key",
        ),
    ],
)
def test_predicates_reject_invalid_fenced_response(response):
    with pytest.raises(PredicateAssetError):
        validate_predicate_response(response, load_default_asset())


@pytest.mark.parametrize("mutation", ["legacy", "duplicate", "missing", "bad-role", "bad-example", "bad-label"])
def test_invalid_assets_are_rejected(mutation):
    definitions = json.loads(files("chrys.service.approval").joinpath("principles.json").read_text(encoding="utf-8"))
    if mutation == "legacy":
        definitions = {"principles": definitions}
    elif mutation == "duplicate":
        definitions.append(definitions[0])
    elif mutation == "missing":
        del definitions[0]["exceptions"]
    elif mutation == "bad-role":
        definitions[0]["role"] = []
    elif mutation == "bad-example":
        del definitions[0]["examples"][0]["reason"]
    else:
        definitions[0]["examples"][0]["result"] = True
    with pytest.raises(PredicateAssetError):
        parse_asset(json.dumps(definitions))


# ─── Relay-strict formal review ──────────────────────────────────────


async def test_relay_enhanced_unknown_predicates_require_human_without_reasoning(monkeypatch):
    """An inconclusive predicate set never falls back to the lenient prompt."""
    async with _judge(monkeypatch, [json.dumps(_values("unknown"))]) as (judge, calls):
        verdict = await judge.evaluate(
            "Inspect",
            "read_file",
            "",
            {"path": "main.py"},
            ["/workspace"],
            request_id="relay-unknown",
            relay_enhanced=True,
            reviewed_model="gpt-relay-x",
        )
        assert verdict.approved is False
        assert verdict.audit["stages"] == ["predicate"]
        assert verdict.audit["relay_enhanced"] is True
        assert len(calls) == 1
        # The strict prelude and the immutable security context reach the wire.
        payload = calls[0].content.decode()
        assert "third-party relay" in payload
        assert "RELAY RISK" in payload
        assert "relay-strict-v1" in payload
        assert "gpt-relay-x" in payload


async def test_relay_enhanced_all_false_predicates_still_approve(monkeypatch):
    async with _judge(monkeypatch, [json.dumps(_values())]) as (judge, _calls):
        verdict = await judge.evaluate(
            "Inspect",
            "read_file",
            "",
            {"path": "main.py"},
            ["/workspace"],
            request_id="relay-allow",
            relay_enhanced=True,
        )
        assert verdict.approved is True
        assert verdict.audit["relay_enhanced"] is True


async def test_relay_enhanced_direct_review_uses_the_strict_system_prompt(monkeypatch):
    async with _judge(monkeypatch, ['{"approved": false, "reason": "uncertain"}'], auto_review="direct") as (
        judge,
        calls,
    ):
        verdict = await judge.evaluate(
            "Inspect",
            "zsh",
            "shell",
            {"command": "make build"},
            ["/workspace"],
            request_id="relay-direct",
            relay_enhanced=True,
        )
        assert verdict.approved is False
        payload = calls[0].content.decode()
        assert "third-party relay" in payload
        assert "uncertainty is never a reason to" in payload
        assert "lean heavily towards approving" not in payload
        # The strict prompt keeps the baseline ALWAYS-FLAG deletion rules the
        # direct policy has always promised (docs: both strategies confirm
        # deletions with a human) — deletion never becomes auto-approvable.
        assert "ALWAYS FLAG (no exceptions) any tool call that deletes files or folders" in payload
        assert "`rm`" in payload
        assert "`Remove-Item`" in payload
        assert "truncate" in payload
        assert "nothing that deletes or truncates files or folders" in payload
