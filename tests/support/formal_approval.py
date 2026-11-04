# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Approval judge requests through the real client factory and mocked HTTP."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from unittest.mock import create_autospec

import httpx
import pytest

from chrys.service.approval.judge import ApprovalJudge, JudgeVerdict
from chrys.service.approval.predicate import TruthValue, load_default_asset
from chrys.service.llm import clients
from chrys.service.profiles.models.schema import ModelProfile
from tests.support.gc_isolation import earlier_tests_heap_frozen


def predicate_values(value: TruthValue = False) -> dict[str, TruthValue]:
    return dict.fromkeys((principle.id for principle in load_default_asset().principles), value)


@asynccontextmanager
async def judge_client(
    monkeypatch: pytest.MonkeyPatch,
    replies: list[str | httpx.Response],
    auto_review: str = "formal",
    timeout: float = 5,
) -> AsyncIterator[tuple[ApprovalJudge, list[httpx.Request]]]:
    """Build a warmed OpenAI Chat Completions judge over mocked HTTP."""
    calls: list[httpx.Request] = []
    http_clients: list[httpx.AsyncClient] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        reply = replies.pop(0)
        if isinstance(reply, httpx.Response):
            return reply
        return httpx.Response(
            200,
            json={
                "id": "test",
                "object": "chat.completion",
                "created": 1,
                "model": "test",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": reply}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5},
            },
        )

    def build_http(profile, timeout, *, raw_http_log_path=None, session_id=None, auth=None):
        http = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        http_clients.append(http)
        return http

    monkeypatch.setattr(
        clients,
        "_build_profile_http_client",
        create_autospec(clients._build_profile_http_client, side_effect=build_http),
    )
    judge = ApprovalJudge(
        ModelProfile(
            id="core-test",
            name="Core test",
            provider="openai",
            api_style="chat_completions",
            model_id="test",
            base_url="https://provider.test/v1",
            api_key="synthetic-key",
            stream=False,
            http_max_retries=0,
            http_read_timeout=timeout,
        ),
        auto_review=auto_review,
    )
    try:
        # These scenarios budget review work, not SDK cold-start imports. The
        # resource tree is lazy even after the client factory has returned;
        # materialize it without sending a request or consuming a scripted reply.
        client = await judge._get_client()
        sdk = client.inner.inner.sdk_client
        await asyncio.to_thread(lambda: sdk.chat.completions.with_raw_response)
        # Nor a full collection walking what earlier tests in this worker left alive.
        with earlier_tests_heap_frozen():
            yield judge, calls
    finally:
        await judge.aclose()
        assert all(client.is_closed for client in http_clients)


async def evaluate_judge(judge: ApprovalJudge) -> JudgeVerdict:
    return await judge.evaluate("Inspect source", "read_file", "filesystem.read", {"path": "main.py"}, ["/workspace"])
