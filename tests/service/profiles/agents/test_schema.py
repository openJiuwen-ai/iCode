# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for the agent profile data model."""

from __future__ import annotations

import pytest

from chrys.service.profiles.agents.schema import MCPServerConfig


@pytest.mark.parametrize("command", ["  /opt/mcp/server ", " ", "\t\n", ""], ids=["padded", "space", "blank", "empty"])
def test_mcp_stdio_command_is_stored_verbatim(command: str) -> None:
    config = MCPServerConfig(name="srv", transport="stdio", command=command)

    assert config.command == command
