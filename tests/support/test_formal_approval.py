# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Cold client setup must not consume the shared approval scenario budget."""

from __future__ import annotations

import asyncio
import gc
from contextlib import asynccontextmanager
from functools import cached_property
from types import ModuleType
from typing import Any
from unittest.mock import create_autospec

import pytest
from openai import AsyncOpenAI

from chrys.service.approval import formal as formal_module
from chrys.service.approval import judge as judge_module
from chrys.service.approval import review_types as review_types_module
from chrys.service.llm import clients
from tests.support.formal_approval import evaluate_judge, judge_client, structured_response


@pytest.mark.parametrize("cold_stage", ["client-factory", "sdk-resources"])
async def test_cold_setup_leaves_the_full_budget_for_real_review(monkeypatch, cold_stage):
    clock = [100.0]
    deadlines = []

    @asynccontextmanager
    async def bounded(deadline):
        deadlines.append(deadline)
        yield

    fake_time = ModuleType("time")
    fake_time.monotonic = lambda: clock[0]
    fake_asyncio = ModuleType("asyncio")
    fake_asyncio.__dict__.update(vars(asyncio), timeout_at=bounded)
    monkeypatch.setattr(formal_module, "time", fake_time)
    monkeypatch.setattr(review_types_module, "time", fake_time)
    monkeypatch.setattr(formal_module, "asyncio", fake_asyncio)

    # Inject six seconds at the real lazy boundary, without sleeping or relying
    # on whether an earlier test in this worker already imported the SDK.
    if cold_stage == "client-factory":
        wire_client_class = clients._wire_client_class

        def cold_factory(provider, api_style):
            clock[0] += 6.0
            return wire_client_class(provider, api_style)

        monkeypatch.setattr(clients, "_wire_client_class", create_autospec(wire_client_class, side_effect=cold_factory))
    else:
        chat = AsyncOpenAI.chat.func

        def cold_chat(self):
            clock[0] += 6.0
            return chat(self)

        resource = cached_property(cold_chat)
        resource.__set_name__(AsyncOpenAI, "chat")
        monkeypatch.setattr(AsyncOpenAI, "chat", resource)

    response = create_autospec(judge_module.get_final_response, side_effect=judge_module.get_final_response)
    monkeypatch.setattr(judge_module, "get_final_response", response)
    replies = [structured_response()]
    async with judge_client(monkeypatch, replies) as (judge, calls):
        assert calls == []
        assert len(replies) == 1
        verdict = await evaluate_judge(judge)
        assert verdict.approved is True, verdict.failure_reason
        assert verdict.failure_reason is None
        assert deadlines == [111.0]
        assert clock[0] == 106.0
        assert response.await_count == 1
        assert response.call_args.kwargs["timeout"] == 5.0
        assert len(calls) == 1
        assert replies == []
        assert verdict.audit["usage"]["total_token_count"] == 5


async def test_a_full_collection_during_review_skips_what_earlier_tests_left_alive(monkeypatch):
    earlier = [[] for _ in range(20_000)]
    walked: list[int] = []

    def on_gc(phase: str, info: dict[str, Any]) -> None:
        if phase == "start" and info["generation"] == 2:
            walked.append(len(gc.get_objects()))

    async with judge_client(monkeypatch, [structured_response()]) as (judge, _calls):
        gc.callbacks.append(on_gc)
        try:
            gc.collect()
        finally:
            gc.callbacks.remove(on_gc)
        verdict = await evaluate_judge(judge)

    assert verdict.approved is True, verdict.failure_reason
    assert len(walked) == 1
    assert walked[0] < len(earlier)
