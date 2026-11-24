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
from chrys.foundation.models.ask_user import ASK_USER_RESPONSE_DIGEST_KEY, ask_user_response_digest
from chrys.foundation.models.history_markers import HistoryMarkerKind, LastWordsRecord
from chrys.foundation.platform.files import secure_open_owner_only_binary
from chrys.foundation.tool_result_metadata import TOOL_RESULT_METADATA_KEY
from chrys.kernel import ChatResponse, Content, Message
from chrys.kernel.compaction import GROUP_ANNOTATION_KEY, SUMMARY_OF_GROUP_IDS_KEY
from chrys.kernel.middleware import active_tool_batch_messages
from chrys.service.approval import formal as formal_module
from chrys.service.approval import judge as judge_module
from chrys.service.approval import review_types as review_types_module
from chrys.service.approval.judge import _SYSTEM_PROMPT, FormalEvaluationCancelled
from chrys.service.approval.predicate import (
    PredicateAssetError,
    evaluate_structured_response,
    load_default_asset,
    parse_asset,
)
from tests.support.formal_approval import evaluate_judge as _evaluate
from tests.support.formal_approval import judge_client as _judge
from tests.support.formal_approval import predicate_values as _values
from tests.support.formal_approval import structured_response as _structured
from tests.support.waiting import wait_for


@pytest.mark.parametrize("triggered", list(_values()))
async def test_any_uncleared_match_requires_human(monkeypatch, triggered):
    values = _values() | {triggered: True}
    async with _judge(monkeypatch, [_structured({triggered: True})]) as (judge, calls):
        verdict = await _evaluate(judge)
        assert not verdict.approved
        assert verdict.triggered == (triggered,)
        assert verdict.audit["stages"] == ["predicate"]
        assert verdict.audit["usage"]["total_token_count"] == 5
        assert {entry["id"]: entry["value"] for entry in verdict.audit["predicate_results"]} == values
        assert len(calls) == 1
        body = json.loads(calls[0].content)
        assert "<response_schema>" in body["messages"][0]["content"]
        assert "main.py" in body["messages"][1]["content"]
        assert calls[0].url.path == "/v1/chat/completions"
        assert body["model"] == "test"
        assert calls[0].headers["authorization"] == "Bearer synthetic-key"
        assert "synthetic-key" not in calls[0].content.decode()


@pytest.mark.parametrize(
    ("matches", "expected"),
    [
        ({"file_folder_deletion": True, "user_boundary": True, "remote_repoint": True}, ("file_folder_deletion",)),
        ({"user_boundary": True, "remote_repoint": True}, ("user_boundary",)),
        ({"remote_repoint": True}, ("remote_repoint",)),
        ({}, ()),
    ],
)
async def test_verdict_reports_only_blockers_at_the_winning_stage(monkeypatch, matches, expected):
    response = _structured(matches | {"security_boundary_change": True}, authorized={"security_boundary_change"})
    async with _judge(monkeypatch, [response]) as (judge, _calls):
        verdict = await _evaluate(judge)
    assert verdict.approved is (not expected)
    assert verdict.triggered == expected
    assert next(entry for entry in verdict.audit["predicate_results"] if entry["id"] == "security_boundary_change") == {
        "id": "security_boundary_change",
        "value": True,
        "consent_bar_met": True,
    }


async def test_all_false_approves_and_writes_only_standard_log(monkeypatch, tmp_path):
    async with _judge(monkeypatch, [_structured()]) as (judge, calls):
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
        with secure_open_owner_only_binary(tmp_path / "safe.log") as log:
            assert b"APPROVED:" in log.read()


async def test_repair_logs_both_formal_attempts(monkeypatch, tmp_path):
    async with _judge(monkeypatch, ["invalid", _structured()]) as (judge, calls):
        verdict = await judge.evaluate("Inspect", "read_file", "", {}, [], request_id="repair", log_dir=tmp_path)
        assert verdict.approved
        assert len(calls) == 2
        log = (tmp_path / "repair.log").read_text(encoding="utf-8")
        assert log.count("ATTEMPT 0 (predicate)") == 1
        assert log.count("ATTEMPT 1 (predicate)") == 1
        repaired = json.loads(calls[-1].content)["messages"]
        assert repaired[2] == {"role": "assistant", "content": "invalid"}
        assert "<response_schema>" in repaired[3]["content"]


@pytest.mark.parametrize("response", ["invalid", _structured({"data_exfiltration": "unknown"})])
async def test_exhausted_repair_writes_terminal_failure(monkeypatch, tmp_path, response):
    async with _judge(monkeypatch, [response] * 3) as (judge, calls):
        verdict = await judge.evaluate("Inspect", "read_file", "", {}, [], request_id="invalid", log_dir=tmp_path)
        assert not verdict.approved
        assert verdict.failure_reason == "invalid_shape"
        assert verdict.audit["stages"] == ["predicate"]
        assert len(calls) == 3
        assert all(json.loads(call.content)["messages"][0]["content"] != _SYSTEM_PROMPT for call in calls)
        log = (tmp_path / "invalid.log").read_text(encoding="utf-8")
        assert "ATTEMPT 3 (predicate)" in log
        assert "FLAGGED: Predicate response is invalid" in log


@pytest.mark.parametrize("strategy", ["direct", "unrecognized"])
async def test_direct_strategy_uses_original_prompt_without_formal_audit(monkeypatch, strategy):
    history = (Message("user", ["Earlier hidden-to-Direct message"]),)
    monkeypatch.setattr(review_types_module, "current_time_context", lambda: "Current time: fixed")
    async with _judge(monkeypatch, ['{"approved":true,"reason":"direct"}'], auto_review=strategy) as (judge, calls):
        with active_tool_batch_messages.set((history, ())), formal_module.pending_formal_call_id.set("active"):
            verdict = await _evaluate(judge)
        assert verdict.approved is True
        assert verdict.audit is None
        messages = json.loads(calls[0].content)["messages"]
        assert messages[0]["content"] == _SYSTEM_PROMPT
        assert messages[1]["content"] == (
            "Current time: fixed\n\nWorkspace directories: /workspace\n\n"
            "Current-turn user prompts:\n1. Inspect source\n\nLatest user prompt:\nInspect source\n\n"
            'Proposed action:\nTool: read_file (kind: filesystem.read)\nArguments:\n{\n  "path": "main.py"\n}'
        )


@pytest.mark.parametrize("tool", ["", " "])
async def test_invalid_tool_never_calls_model(monkeypatch, tool):
    async with _judge(monkeypatch, []) as (judge, calls):
        verdict = await judge.evaluate("Inspect", tool, "", {}, [], user_messages=["Inspect"])
        assert verdict.approved is False
        assert verdict.failure_reason == "invalid_input"
        assert calls == []


@pytest.mark.parametrize("risk", [False, True])
async def test_empty_context_is_reviewed_once(monkeypatch, risk):
    async with _judge(monkeypatch, [_structured({"data_exfiltration": risk})]) as (judge, calls):
        verdict = await judge.evaluate("", "read_file", "", {"path": "main.py"}, [], user_messages=[" ", "\t"])
        assert verdict.approved is not risk
        assert verdict.audit["stages"] == ["predicate"]
        assert len(calls) == 1
        assert "<context>\n\n</context>" in json.loads(calls[0].content)["messages"][1]["content"]


async def test_empty_latest_message_uses_normalized_user_context(monkeypatch):
    async with _judge(monkeypatch, [_structured()]) as (judge, calls):
        verdict = await judge.evaluate(" ", "read_file", "", {}, [], user_messages=[" ", " Inspect source ", "\t"])
        assert verdict.approved is True
        prompt = json.loads(calls[0].content)["messages"][1]["content"]
        assert '{"user": "Inspect source"}' in prompt


async def test_asset_failure_never_calls_model(monkeypatch):
    monkeypatch.setattr(
        formal_module, "load_default_asset", create_autospec(load_default_asset, side_effect=PredicateAssetError)
    )
    async with _judge(monkeypatch, []) as (judge, calls):
        verdict = await _evaluate(judge)
        assert verdict.failure_reason == "asset_unavailable"
        assert calls == []


@pytest.mark.parametrize("status,kind", [(401, "auth_failed"), (429, "rate_limited"), (500, "server_error")])
async def test_provider_errors_use_standard_classification(monkeypatch, status, kind):
    replies = [httpx.Response(status, json={"error": {"message": "provider error", "type": "error"}})]
    async with _judge(monkeypatch, replies) as (judge, calls):
        verdict = await _evaluate(judge)
        assert verdict.approved is False
        assert verdict.failure_reason == kind
        assert len(calls) == 1


async def test_sdk_read_timeout_is_distinct_from_budget_timeout(monkeypatch):
    error = httpx.ReadTimeout("read failed")
    monkeypatch.setattr(
        judge_module, "get_final_response", create_autospec(judge_module.get_final_response, side_effect=error)
    )
    async with _judge(monkeypatch, []) as (judge, _):
        verdict = await _evaluate(judge)
        assert verdict.failure_reason == classify_error(error).kind.value == "read_timeout"


async def test_cancellation_retains_formal_audit(monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()

    async def response(client, messages, *, stream, options, timeout):
        entered.set()
        await release.wait()
        return ChatResponse(messages=[Message("assistant", [_structured()])])

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
            assert cancelled.value.verdict.audit["stages"] == ["predicate"]
            assert cancelled.value.verdict.failure_reason == "cancelled"
        finally:
            release.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("end", [104.5, 106.0])
@pytest.mark.parametrize("repair", [False, True])
async def test_formal_repairs_share_one_deadline(monkeypatch, end, repair):
    clock = [100.0]
    budgets, deadlines = [], []

    @asynccontextmanager
    async def bounded(deadline):
        deadlines.append(deadline)
        yield

    async def response(client, messages, *, stream, options, timeout):
        budgets.append(timeout)
        if repair and len(budgets) == 1:
            clock[0] = 102.0
            return ChatResponse(messages=[Message("assistant", ["invalid"])])
        clock[0] = end
        return ChatResponse(messages=[Message("assistant", [_structured()])])

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
        assert budgets == ([5.0, 3.0] if repair else [5.0])
        assert verdict.approved is (end < 105.0)
        assert verdict.failure_reason == (None if end < 105.0 else "timeout")


async def test_postprocessing_cannot_approve_after_formal_deadline(monkeypatch, tmp_path):
    clock = [0.0]
    fake_time = ModuleType("time")
    fake_time.monotonic = lambda: clock[0]
    monkeypatch.setattr(formal_module, "time", fake_time)
    monkeypatch.setattr(review_types_module, "time", fake_time)
    original_log = judge_module.ApprovalJudge._write_log

    def write_log(log_path, attempt, messages, text, verdict, stage="direct"):
        original_log(log_path, attempt, messages, text, verdict, stage)
        clock[0] += 6

    monkeypatch.setattr(judge_module.ApprovalJudge, "_write_log", staticmethod(write_log))
    async with _judge(monkeypatch, [_structured()]) as (judge, calls):
        # Align the fake review clock with timeout_at only after cold setup.
        clock[0] = asyncio.get_running_loop().time()
        verdict = await judge.evaluate("Inspect", "read_file", "", {}, [], request_id="deadline", log_dir=tmp_path)
        assert not verdict.approved
        assert verdict.failure_reason == "timeout"
        assert len(calls) == 1
        assert "APPROVED:" in (tmp_path / "deadline.log").read_text(encoding="utf-8")
        assert verdict.audit["usage"]["total_token_count"] == 5


async def test_predicate_asset_is_loaded_once_per_judge(monkeypatch):
    load = create_autospec(load_default_asset, return_value=load_default_asset())
    monkeypatch.setattr(formal_module, "load_default_asset", load)
    async with _judge(monkeypatch, [_structured(), _structured()]) as (judge, calls):
        assert (await _evaluate(judge)).approved is True
        assert (await _evaluate(judge)).approved is True
        assert len(calls) == 2
        load.assert_called_once_with()


async def test_concurrent_requests_keep_snapshot_and_audit_separate(monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()

    async def response(client, messages, *, stream, options, timeout):
        first = "first.py" in messages[1].text
        if first:
            assert "second.py" not in messages[1].text
            entered.set()
            await release.wait()
        result = _structured({"data_exfiltration": not first})
        return ChatResponse(messages=[Message("assistant", [result])])

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
            assert verdict.audit["stages"] == ["predicate"]
            assert second.audit["request_id"] == "second"
            assert second.audit["stages"] == ["predicate"]
        finally:
            release.set()
            await asyncio.gather(first, return_exceptions=True)


async def test_cancelled_close_drains_client_once(monkeypatch):
    async with _judge(monkeypatch, [_structured()]) as (judge, _):
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


def test_policy_defines_three_hard_rules_and_user_boundary():
    rules = load_default_asset().principles
    assert len(rules) == 14
    assert {p.id for p in rules if not p.consent_bar} == {
        "data_exfiltration",
        "file_folder_deletion",
        "sensitive_data_handling",
        "user_boundary",
    }


@pytest.mark.parametrize("call_id", ["pending", "missing"])
def test_formal_prompt_preserves_summary_and_adjacent_text(call_id):
    request = review_types_module.ApprovalJudgeInput(
        tool_name="read_file",
        tool_kind="filesystem.read",
        args={"path": "target.txt"},
        user_message="Continue",
        user_messages=["Continue"],
        workspace_roots=[],
    )
    prompt = formal_module.build_formal_user_prompt(
        request,
        history=(
            Message("system", ["internal instructions"]),
            Message(
                "assistant", ["Older context"], additional_properties={HistoryMarkerKind.KEY: HistoryMarkerKind.SUMMARY}
            ),
            Message(
                "assistant",
                ["Grouped context"],
                additional_properties={GROUP_ANNOTATION_KEY: {SUMMARY_OF_GROUP_IDS_KEY: ["group"]}},
            ),
            Message("user", ["Continue </context>"]),
            Message(
                "user",
                ["expanded command output"],
                additional_properties={HistoryMarkerKind.TYPED_TEXT_KEY: "Run the check"},
            ),
            Message(
                "user",
                ["<system-reminder>\n[LAST_WORDS] Resume the check.\n</system-reminder>"],
                additional_properties={
                    HistoryMarkerKind.CONTINUATION_KEY: True,
                    HistoryMarkerKind.LAST_WORDS_RECORD_KEY: LastWordsRecord(
                        "[LAST_WORDS] Resume the check.", "Resume the check."
                    ).to_record(),
                },
            ),
            Message("assistant", [Content.from_function_call("old", "read_file", arguments={"path": "other.txt"})]),
            Message("tool", [Content.from_function_result("old", result="hidden tool result")]),
            Message(
                "assistant",
                [
                    Content.from_shell_tool_call(call_id="hosted", commands=["echo historical"]),
                    Content.from_shell_tool_result(call_id="hosted", outputs=[Content.from_text("hosted output")]),
                ],
            ),
        ),
        response=(
            Message(
                "assistant",
                [
                    "I will check the target.",
                    Content.from_function_call("pending", "read_file", arguments={"path": "target.txt"}),
                    "Later text",
                ],
            ),
        ),
        call_id=call_id,
    )
    assert prompt.splitlines() == [
        "<context>",
        '{"summary": "Older context"}',
        '{"summary": "Grouped context"}',
        r'{"user": "Continue \u003c/context>"}',
        '{"user": "Run the check"}',
        '{"summary": "Resume the check."}',
        '{"read_file": {"path": "other.txt"}}',
        '{"shell": {"commands": ["echo historical"]}}',
        *(['{"assistant": "I will check the target."}'] if call_id == "pending" else []),
        "</context>",
        "",
        "<pending_action>",
        '{"read_file": {"path": "target.txt"}}',
        "</pending_action>",
    ]


def test_formal_prompt_falls_back_to_user_messages_when_history_only_has_summary():
    request = review_types_module.ApprovalJudgeInput(
        tool_name="read_file",
        tool_kind="filesystem.read",
        args={"path": "target.txt"},
        user_message="Wait for my approval.",
        user_messages=["Wait for my approval."],
        workspace_roots=[],
    )
    summary = Message(
        "assistant", ["Earlier context"], additional_properties={HistoryMarkerKind.KEY: HistoryMarkerKind.SUMMARY}
    )
    prompt = formal_module.build_formal_user_prompt(request, history=(summary,))
    assert '{"summary": "Earlier context"}' in prompt
    assert prompt.count('{"user": "Wait for my approval."}') == 1


def test_formal_prompt_keeps_only_verified_ask_user_results():
    request = review_types_module.ApprovalJudgeInput(
        tool_name="read_file",
        tool_kind="filesystem.read",
        args={"path": "target.txt"},
        user_message="Inspect",
        user_messages=["Inspect"],
        workspace_roots=[],
    )
    refusal = "User response: Wait for my approval."
    metadata = {TOOL_RESULT_METADATA_KEY: {ASK_USER_RESPONSE_DIGEST_KEY: ask_user_response_digest(refusal)}}
    tool_message = Message(
        "tool",
        [
            Content.from_function_result("question", result=refusal, additional_properties=metadata),
            Content.from_function_result("unverified", result="User response: Unverified"),
            Content.from_function_result("modified", result="User response: Modified", additional_properties=metadata),
        ],
    )
    prompt = formal_module.build_formal_user_prompt(request, history=(tool_message,))
    assert json.dumps({"tool": "ask_user", "result": refusal}) in prompt
    assert json.dumps({"user": "Inspect"}) in prompt
    assert "Unverified" not in prompt
    assert "Modified" not in prompt


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
        *[_structured({"data_exfiltration": value}) for value in (None, 0, 1, "true", "UNKNOWN", [], {})],
        _structured({"data_exfiltration": "false"}),
        _structured().replace('"predicates": {', '"predicates": {"extra": false,'),
        _structured().replace('"matches": false', '"matches": true, "matches": false'),
    ],
)
def test_predicates_reject_missing_duplicate_extra_or_illegal_values(response):
    with pytest.raises(PredicateAssetError):
        evaluate_structured_response(response, load_default_asset())


@pytest.mark.parametrize(
    "response",
    [
        pytest.param(f"```json\n{_structured()}\n```", id="json"),
        pytest.param(f"```\n{_structured()}\n```", id="plain"),
        pytest.param(f" \n```JSON\n{_structured()}\n```\n ", id="uppercase-with-whitespace"),
    ],
)
def test_predicates_accept_complete_fence(response):
    values = evaluate_structured_response(response, load_default_asset()).values
    assert {value.id: value.value for value in values} == _values()


@pytest.mark.parametrize(
    "response",
    [
        pytest.param(f"Here you go:\n```json\n{_structured()}\n```", id="leading-prose"),
        pytest.param(f"```json\n{_structured()}\n```\n```json\n{{}}\n```", id="two-blocks"),
        pytest.param(
            "```json\n" + _structured().replace(", ", ",\n```\n```json\n", 1) + "\n```",
            id="object-split-across-blocks",
        ),
        pytest.param(f"```json\n{_structured()}", id="unterminated"),
        pytest.param(
            "```json\n" + _structured().replace('"matches": false', '"matches": true, "matches": false') + "\n```",
            id="duplicate-key",
        ),
    ],
)
def test_predicates_reject_invalid_fenced_response(response):
    with pytest.raises(PredicateAssetError):
        evaluate_structured_response(response, load_default_asset())


@pytest.mark.parametrize(
    "mutation", ["wrapped-array", "duplicate", "missing", "bad-instructions", "bad-criterion", "bad-label"]
)
def test_invalid_assets_are_rejected(mutation):
    definitions = json.loads(files("chrys.service.approval").joinpath("principles.json").read_text(encoding="utf-8"))
    if mutation == "wrapped-array":
        definitions = {"principles": definitions}
    elif mutation == "duplicate":
        definitions.append(definitions[0])
    elif mutation == "missing":
        del definitions[0]["criterion"]
    elif mutation == "bad-instructions":
        definitions[0]["instructions"] = []
    elif mutation == "bad-criterion":
        del definitions[0]["criterion"]["true"]
    else:
        definitions[0]["criterion"]["true"] = True
    with pytest.raises(PredicateAssetError):
        parse_asset(json.dumps(definitions))
