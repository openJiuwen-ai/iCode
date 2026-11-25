# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Dependency preparation through the real root and ordinary child builders."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from chrys.service.mcp.cache import MCPConnectionCache
from chrys.service.profiles.agents.schema import MCPServerConfig
from chrys.service.skills import loader
from tests.support.initialization_build import InitializationBuild, create_skills

PROBE = Path(__file__).parents[3] / "support" / "mcp_reload_probe_server.py"


@pytest.mark.parametrize("children", [0, 3])
async def test_build_shares_discovery_and_recovers_failed_connection_next_build(tmp_path, monkeypatch, children):
    state = tmp_path / "server"
    state.mkdir()
    (state / "refuse").touch()
    create_skills(tmp_path, 3)
    config = MCPServerConfig(
        name="probe", transport="stdio", command=sys.executable, args=[str(PROBE), "--state-dir", str(state)]
    )
    fixture = InitializationBuild(tmp_path, children, [config])
    fixture.install(monkeypatch)
    cache = MCPConnectionCache()
    builds = []
    try:
        # Observe real directory discovery, including the call made inside the worker thread.
        with patch.object(loader, "discover_skill_directories", wraps=loader.discover_skill_directories) as discover:
            failed = await fixture.build(cache)
            builds.append(failed)
            assert discover.call_count == 1
            assert failed.mcp_adapter is not None
            assert "probe" in failed.mcp_adapter.failures
            assert len((state / "starts").read_text(encoding="utf-8").splitlines()) == 1
            assert sorted(failed.skill_names) == [f"skill-{i}" for i in range(3)]
            (state / "refuse").unlink()
            recovered = await fixture.build(cache)
            builds.append(recovered)
            assert discover.call_count == 2
            assert recovered.mcp_adapter is not None
            assert recovered.mcp_adapter.failures == {}
            assert len((state / "starts").read_text(encoding="utf-8").splitlines()) == 2
            lease = recovered.mcp_adapter._leases["probe"]
            (answer,) = await lease.functions[0].invoke(label="recovered")
            assert "answered by" in answer.text
            if children:
                assert recovered.sub_agent_tools is not None
                for adapter in recovered.sub_agent_tools._mcp_adapters.values():
                    child_lease = adapter._leases["probe"]
                    assert child_lease.mcp_tool is lease.mcp_tool
                    assert child_lease.functions[0] is not lease.functions[0]
                    (answer,) = await child_lease.functions[0].invoke(label="child")
                    assert "answered by" in answer.text
    finally:
        for result in builds:
            await result.prepared.aclose()
        await cache.close_all()
