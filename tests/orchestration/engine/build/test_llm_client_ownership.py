# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Every LLM client a main-agent build opens closes with that build."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from tests.support.llm_client_engines import (
    ALT_MODEL,
    MAIN_MODEL,
    SUB_MODEL,
    open_judge_client,
    start_client_engine,
    switch_model,
)
from tests.support.tool_packs import install_tools
from tests.support.waiting import ENGINE_TURN_TIMEOUT, wait_for

if TYPE_CHECKING:
    from pathlib import Path

    from tests.support.engines import AgentEngineFactory
    from tests.support.llm_http_clients import HttpClientLedger


async def test_model_switch_rebuild_closes_previous_clients(
    agent_engine: AgentEngineFactory, tmp_path: Path, http_client_ledger: HttpClientLedger
) -> None:
    started = await start_client_engine(agent_engine, tmp_path)
    await open_judge_client(started)
    old_main, old_judge = http_client_ledger.for_profile(MAIN_MODEL)
    [old_sub] = http_client_ledger.for_profile(SUB_MODEL)
    assert http_client_ledger.open() == [old_main, old_sub, old_judge]

    await switch_model(started, ALT_MODEL)

    await wait_for(
        lambda: old_main.is_closed and old_sub.is_closed and old_judge.is_closed,
        timeout=ENGINE_TURN_TIMEOUT,
        description="previous build's clients closed",
    )
    new_main = http_client_ledger.for_profile(ALT_MODEL)
    [_, new_sub] = http_client_ledger.for_profile(SUB_MODEL)
    assert len(new_main) == 1
    assert http_client_ledger.open() == [*new_main, new_sub]


async def test_engine_shutdown_closes_every_llm_client(
    agent_engine: AgentEngineFactory, tmp_path: Path, http_client_ledger: HttpClientLedger
) -> None:
    started = await start_client_engine(agent_engine, tmp_path)
    await open_judge_client(started)
    assert len(http_client_ledger.open()) == 3

    await started.engine.shutdown()

    assert len(http_client_ledger.clients) == 3
    assert http_client_ledger.open() == []


async def test_main_build_failure_after_client_creation_closes_it(
    agent_engine: AgentEngineFactory,
    tmp_path: Path,
    http_client_ledger: HttpClientLedger,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    error = RuntimeError("builtin tools failed")
    install_tools(monkeypatch, side_effect=error)

    with pytest.raises(RuntimeError) as info:
        await start_client_engine(agent_engine, tmp_path)

    assert info.value is error
    assert http_client_ledger.for_profile(MAIN_MODEL) == http_client_ledger.clients
    assert len(http_client_ledger.clients) == 1
    assert http_client_ledger.open() == []


async def test_last_words_client_closes_with_its_runtime(
    agent_engine: AgentEngineFactory, tmp_path: Path, http_client_ledger: HttpClientLedger
) -> None:
    started = await start_client_engine(agent_engine, tmp_path, sub_agent=False)
    strategy = started.engine.current.require_loaded().bindings.backend.compaction_strategy
    assert strategy is not None
    await strategy._last_words_generator._get_client()
    main, last_words = http_client_ledger.for_profile(MAIN_MODEL)
    assert not last_words.is_closed

    await switch_model(started, ALT_MODEL)

    await wait_for(
        lambda: main.is_closed and last_words.is_closed,
        timeout=ENGINE_TURN_TIMEOUT,
        description="previous runtime's last-words client closed",
    )
