# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The first MCP connect of a process imports the SDK off the event loop."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from chrys.foundation.platform.process import windows_hidden_subprocess_kwargs
from tests.support.waiting import ENGINE_TURN_TIMEOUT, wait_for

PROBE_SERVER = Path(__file__).parents[2] / "support" / "mcp_reload_probe_server.py"

_CONNECT = textwrap.dedent(
    """
    import asyncio, sys, threading
    from chrys.service.mcp.cache import MCPConnectionCache
    from chrys.service.profiles.agents.schema import MCPServerConfig

    class ImportThreads:
        # Notes the thread of the SDK's first import, and finds nothing.
        on_main_thread = {}

        def find_spec(self, name, path=None, target=None):
            if name == "mcp":
                self.on_main_thread.setdefault(name, threading.current_thread() is threading.main_thread())
            return None

    async def connect(config):
        cache = MCPConnectionCache()
        try:
            await cache.release(await cache.acquire(config))
        finally:
            await cache.close_all()

    transport, target, state = sys.argv[1:]
    if transport == "stdio":
        config = MCPServerConfig(
            name="probe", transport="stdio", command=sys.executable, args=[target, "--state-dir", state]
        )
    else:
        config = MCPServerConfig(name="probe", transport="http", url=target, bypass_proxy=True)
    sys.meta_path.insert(0, ImportThreads())
    asyncio.run(connect(config))
    print(ImportThreads.on_main_thread)
    """
)


def _first_connect(tmp_path: Path, timeout: float, *args: str) -> subprocess.CompletedProcess[str]:
    home = str(tmp_path)
    return subprocess.run(
        [sys.executable, "-c", _CONNECT, *args],
        cwd=tmp_path,
        env={**os.environ, "HOME": home, "USERPROFILE": home, "APPDATA": home, "NO_PROXY": "*"},
        stdin=subprocess.DEVNULL,
        capture_output=True,
        encoding="utf-8",
        timeout=timeout,
        check=False,
        **windows_hidden_subprocess_kwargs(),
    )


@pytest.fixture
def http_server(tmp_path):
    """A Streamable HTTP probe server, and the file it writes its port to once it listens."""
    state = tmp_path / "http-state"
    state.mkdir()
    port_file = state / "port"
    process = subprocess.Popen(
        [sys.executable, str(PROBE_SERVER), "--state-dir", str(state), "--port-file", str(port_file)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        **windows_hidden_subprocess_kwargs(),
    )
    try:
        yield state, port_file
    finally:
        process.kill()
        process.wait()


def test_a_first_stdio_connect_imports_the_sdk_off_the_event_loop(tmp_path) -> None:
    # Hundreds of modules: imported on the event loop, they froze the TUI while the agent built.
    state = tmp_path / "state"
    state.mkdir()
    # Two cold starts: this interpreter, then the server's.
    result = _first_connect(tmp_path, 2 * ENGINE_TURN_TIMEOUT, "stdio", str(PROBE_SERVER), str(state))

    assert result.returncode == 0, result.stdout + result.stderr
    assert (state / "starts").read_text(encoding="utf-8").split(), "the server never started"
    assert result.stdout.strip() == "{'mcp': False}"


async def test_a_first_http_connect_imports_the_sdk_off_the_event_loop(tmp_path, http_server) -> None:
    # The HTTP transport builds its client, which imports the SDK, before it connects.
    _state, port_file = http_server
    await wait_for(
        lambda: port_file.exists() and port_file.read_text(encoding="utf-8"),
        timeout=ENGINE_TURN_TIMEOUT,
        description="the server is listening",
    )
    url = f"http://127.0.0.1:{port_file.read_text(encoding='utf-8')}/mcp"
    result = await asyncio.to_thread(_first_connect, tmp_path, ENGINE_TURN_TIMEOUT, "http", url, "")

    # A failed connect raises from acquire, so a clean exit means the server answered.
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == "{'mcp': False}"
