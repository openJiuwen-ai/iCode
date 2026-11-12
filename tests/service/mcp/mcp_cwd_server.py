# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Minimal MCP server that reports the folder it was started in."""

from __future__ import annotations

import os

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("cwd-server")


@mcp.tool()
def get_cwd() -> str:
    """Return the working directory of the MCP server process."""
    return os.getcwd()


if __name__ == "__main__":
    mcp.run(transport="stdio")
