# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""A sub-agent registration's LLM client closes with the registration."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from chrys.service.tools import packs as tool_packs
from tests.support.llm_client_engines import (
    ALT_MODEL,
    MAIN_MODEL,
    SUB_AGENT,
    SUB_MODEL,
    start_client_engine,
    switch_model,
)
from tests.support.tool_packs import install_tools
from tests.support.waiting import ENGINE_TURN_TIMEOUT, wait_for

if TYPE_CHECKING:
    from pathlib import Path

    from chrys.kernel import FunctionTool
    from tests.support.engines import AgentEngineFactory
    from tests.support.llm_http_clients import HttpClientLedger


async def test_sub_agent_rebuild_closes_its_registration_client(
    agent_engine: AgentEngineFactory, tmp_path: Path, http_client_ledger: HttpClientLedger
) -> None:
    started = await start_client_engine(agent_engine, tmp_path)
    [old_sub] = http_client_ledger.for_profile(SUB_MODEL)
    assert not old_sub.is_closed

    await switch_model(started, ALT_MODEL)

    await wait_for(lambda: old_sub.is_closed, timeout=ENGINE_TURN_TIMEOUT, description="old sub-agent client closed")
    [_, new_sub] = http_client_ledger.for_profile(SUB_MODEL)
    assert not new_sub.is_closed


async def test_sub_agent_acquire_failure_closes_its_client(
    agent_engine: AgentEngineFactory,
    tmp_path: Path,
    http_client_ledger: HttpClientLedger,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    error = RuntimeError("sub-agent tools failed")
    create_packs = tool_packs.create_packs

    def fail_the_sub_agent(build: tool_packs.ToolBuild, pack_ids: list[str]) -> list[tuple[str, list[FunctionTool]]]:
        if build.agent_name == SUB_AGENT:
            raise error
        return create_packs(build, pack_ids)

    install_tools(monkeypatch, side_effect=fail_the_sub_agent)

    with pytest.raises(RuntimeError) as info:
        await start_client_engine(agent_engine, tmp_path)

    assert info.value is error
    assert len(http_client_ledger.for_profile(SUB_MODEL)) == 1
    assert len(http_client_ledger.for_profile(MAIN_MODEL)) == 1
    assert http_client_ledger.open() == []
