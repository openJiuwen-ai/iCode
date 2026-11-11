# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The working directory a stdio MCP server starts in: resolution, cache key and real child processes."""

from __future__ import annotations

import asyncio
import builtins
import logging
import ntpath
import os
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest.mock import patch

import pytest

from chrys.service.mcp._stdio_transport import (
    StdioWorkingDirectoryError,
    _anchor_relative_command,
    _check_working_directory,
    tolerant_stdio_client,
)
from chrys.service.mcp.adapter import MCPAdapter
from chrys.service.mcp.cache import (
    MCPConnectionCache,
    mcp_config_cache_key,
    resolve_mcp_effective_config,
    resolve_stdio_cwd,
)
from chrys.service.mcp.errors import MCPConnectionError, mcp_failure_diagnostic
from chrys.service.profiles.agents.schema import MCPServerConfig
from tests.service.mcp._helpers import block_import
from tests.support.symlinks import symlink_or_skip
from tests.support.waiting import ENGINE_TURN_TIMEOUT

CWD_SERVER = Path(__file__).with_name("mcp_cwd_server.py")

# Keys computed on the commit before ``cwd`` existed, for the fixed config below.
_KEY_WITH_WORKSPACE = "39da96613917d61cae4ac45a33e7bae9c9066b02e3086083c16263e53a47156c"
_KEY_WITH_PROCESS_CWD = "bd617c2cea95fa4ae4847e8bf4975cb9839c443c0c0e2ccb27fc27a5d240ea13"


def _config(**fields: object) -> MCPServerConfig:
    return MCPServerConfig(
        name="srv",
        transport="stdio",
        command="python",
        args=["-m", "srv"],
        env={"A": "1"},
        encoding="utf-8",
        **fields,  # type: ignore[arg-type]
    )


def _pin_child_environment():
    return patch(
        "chrys.service.mcp.cache._inherited_stdio_environment", side_effect=lambda _extra: {"PATH": "/bin", "A": "1"}
    )


def _cwd_server_config(**fields: object) -> MCPServerConfig:
    return MCPServerConfig(
        name="cwd",
        transport="stdio",
        command=sys.executable,
        args=[str(CWD_SERVER)],
        **fields,  # type: ignore[arg-type]
    )


def _write_launcher(folder: Path) -> Path:
    """A script in *folder* that runs this interpreter; a link to it would leave the virtual environment."""
    folder.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        launcher = folder / "launch.cmd"
        launcher.write_text(f'@echo off\r\n"{sys.executable}" %*\r\n', encoding="utf-8")
    else:
        launcher = folder / "launch.sh"
        launcher.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n', encoding="utf-8")
        launcher.chmod(0o755)
    return launcher


async def _reported_cwd(config: MCPServerConfig, workspace: Path) -> str:
    cache = MCPConnectionCache()
    adapter = MCPAdapter(cache=cache, stdio_cwd=str(workspace))
    try:
        tools = await asyncio.wait_for(adapter.connect(config), timeout=ENGINE_TURN_TIMEOUT)
        tool = next(tool for tool in tools if tool.name == "get_cwd")
        result = await asyncio.wait_for(tool.invoke(arguments={}), timeout=ENGINE_TURN_TIMEOUT)
        return "\n".join(content.text for content in result)
    finally:
        await adapter.disconnect_all()
        await cache.close_all()


# ── Cache key ────────────────────────────────────────────────────────


def test_unset_cwd_keeps_the_cache_key_of_a_server_without_the_field() -> None:
    with _pin_child_environment():
        assert mcp_config_cache_key(_config(), stdio_cwd="/work/space") == _KEY_WITH_WORKSPACE
        with patch("chrys.service.mcp.cache.os.getcwd", return_value="/proc/cwd"):
            assert mcp_config_cache_key(_config()) == _KEY_WITH_PROCESS_CWD


def test_only_the_cwd_differing_gives_another_key(tmp_path: Path) -> None:
    workspace = str(tmp_path / "workspace")
    with _pin_child_environment():
        base = mcp_config_cache_key(_config(), stdio_cwd=workspace)
        other = mcp_config_cache_key(_config(cwd=str(tmp_path / "elsewhere")), stdio_cwd=workspace)
        relative = mcp_config_cache_key(_config(cwd="sub"), stdio_cwd=workspace)
        assert len({base, other, relative}) == 3


def test_the_key_holds_the_folder_the_server_starts_in(tmp_path: Path) -> None:
    workspace = str(tmp_path / "workspace")
    configured = os.path.join("link", "..")
    effective = resolve_mcp_effective_config(_config(cwd=configured), stdio_cwd=workspace)

    assert effective.resolved_stdio_cwd == resolve_stdio_cwd(configured, workspace)
    with _pin_child_environment():
        # `link/..` is not the workspace on POSIX when `link` is a symlink, so it is another connection.
        folded = mcp_config_cache_key(_config(), stdio_cwd=workspace)
        unfolded = mcp_config_cache_key(_config(cwd=configured), stdio_cwd=workspace)
        assert (unfolded == folded) is (sys.platform == "win32")


def test_effective_config_holds_the_resolved_folder() -> None:
    effective = resolve_mcp_effective_config(_config(cwd="sub"), stdio_cwd=os.path.join(os.sep, "work", "space"))

    assert effective.resolved_stdio_cwd == os.path.join(os.sep, "work", "space", "sub")


# ── Resolution ───────────────────────────────────────────────────────


def test_empty_cwd_is_the_workspace_folder_untouched() -> None:
    assert resolve_stdio_cwd("", "/work/sp ace/../x") == "/work/sp ace/../x"
    with patch("chrys.service.mcp.cache.os.getcwd", return_value="/proc/cwd"):
        assert resolve_stdio_cwd("", None) == "/proc/cwd"
        assert resolve_stdio_cwd("", "") == "/proc/cwd"


def test_relative_cwd_starts_at_the_workspace_not_the_process_folder(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"

    assert resolve_stdio_cwd("tools/srv", str(workspace)) == os.path.normpath(workspace / "tools" / "srv")
    assert os.path.normpath(resolve_stdio_cwd(os.path.join("..", "other"), str(workspace))) == str(tmp_path / "other")


@pytest.mark.skipif(sys.platform == "win32", reason="Windows resolves `..` lexically, before any link")
def test_a_relative_cwd_steps_back_through_a_linked_folder(tmp_path: Path) -> None:
    external = tmp_path / "external"
    (external / "bin").mkdir(parents=True)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    symlink_or_skip(workspace / "link", external / "bin", target_is_directory=True)

    resolved = resolve_stdio_cwd("link/..", str(workspace))

    assert resolved == str(workspace / "link" / "..")
    assert os.path.samefile(resolved, external)


def test_home_prefix_expands_on_every_platform(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    workspace = str(tmp_path / "workspace")

    assert resolve_stdio_cwd("~", workspace) == str(home)
    assert resolve_stdio_cwd("~/srv", workspace) == str(home / "srv")
    if sys.platform == "win32":
        assert resolve_stdio_cwd("~\\srv", workspace) == str(home / "srv")


def test_a_full_path_needs_no_base_folder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # No workspace given and the process folder deleted: only a path that needs a base fails.
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    target = str(tmp_path / "srv")
    with patch("chrys.service.mcp.cache.os.getcwd", side_effect=FileNotFoundError):
        assert resolve_stdio_cwd(target, None) == target
        assert resolve_stdio_cwd("~/srv", None) == str(home / "srv")
        for needs_base in ("", "srv"):
            with pytest.raises(FileNotFoundError):
                resolve_stdio_cwd(needs_base, None)


def test_absolute_cwd_is_used_as_written(tmp_path: Path) -> None:
    target = tmp_path / "abs" / "dir"

    assert resolve_stdio_cwd(str(target), str(tmp_path / "workspace")) == str(target)


def test_variable_syntaxes_are_not_expanded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SRV_HOME", "/expanded")
    workspace = str(tmp_path)

    for literal in ("{{SRV_HOME}}", "$SRV_HOME", "%SRV_HOME%"):
        assert resolve_stdio_cwd(literal, workspace) == os.path.normpath(os.path.join(workspace, literal))


def test_the_written_case_is_kept(tmp_path: Path) -> None:
    assert resolve_stdio_cwd("Tools/SRV", str(tmp_path)) == os.path.normpath(tmp_path / "Tools" / "SRV")


@pytest.mark.skipif(sys.platform == "win32", reason="lone surrogates in paths are POSIX-only")
def test_a_surrogateescaped_workspace_stays_raw(tmp_path: Path) -> None:
    workspace = "/work/pro\udcffject"

    assert resolve_stdio_cwd("sub", workspace) == "/work/pro\udcffject/sub"
    assert resolve_stdio_cwd("", workspace) == workspace


@pytest.mark.skipif(sys.platform != "win32", reason="Windows path syntax")
def test_windows_separators_and_drive_paths() -> None:
    assert resolve_stdio_cwd("sub/dir", "C:\\work") == "C:\\work\\sub\\dir"
    assert resolve_stdio_cwd("sub\\dir", "C:\\work") == "C:\\work\\sub\\dir"
    assert resolve_stdio_cwd("D:/data", "C:\\work") == "D:\\data"
    assert resolve_stdio_cwd("sub\\..\\other", "C:\\work") == "C:\\work\\other"
    assert resolve_stdio_cwd("D:server", "C:\\work") == "D:server"


def test_a_drive_relative_windows_path_is_never_joined_onto_the_workspace() -> None:
    # Windows path rules on every host. `D:server` counts from drive D's own current folder,
    # so it stays as written (and is refused at spawn), on the workspace's drive as well.
    windows_os = ModuleType("os")
    windows_os.path = ntpath  # type: ignore[attr-defined]
    windows = ModuleType("platform")
    windows.is_windows = True  # type: ignore[attr-defined]
    with (
        patch("chrys.service.mcp.cache.os", windows_os),
        patch("chrys.service.mcp.cache.get_platform", lambda: windows),
    ):
        for workspace in ("C:\\workspace", "D:\\workspace"):
            assert resolve_stdio_cwd("D:server", workspace) == "D:server"
            assert resolve_stdio_cwd("d:server/sub", workspace) == "d:server\\sub"
        # A full path, a relative path and a path from the workspace drive's root still resolve.
        assert resolve_stdio_cwd("D:\\server", "D:\\workspace") == "D:\\server"
        assert resolve_stdio_cwd("server", "D:\\workspace") == "D:\\workspace\\server"
        assert resolve_stdio_cwd("\\server", "D:\\workspace") == "D:\\server"
        # Neither reads a base folder (this `os` has no getcwd).
        assert resolve_stdio_cwd("D:server", None) == "D:server"
        assert resolve_stdio_cwd("D:\\server", None) == "D:\\server"


# ── Directory check and relative commands ────────────────────────────


def test_check_working_directory_names_what_is_wrong(tmp_path: Path) -> None:
    folder = tmp_path / "folder"
    folder.mkdir()
    file = tmp_path / "file"
    file.write_text("x", encoding="utf-8")

    _check_working_directory(folder)
    for path, reason in ((tmp_path / "missing", "missing"), (file, "not_directory"), (file / "below", "not_directory")):
        with pytest.raises(StdioWorkingDirectoryError) as caught:
            _check_working_directory(path)
        assert caught.value.reason == reason


def test_check_working_directory_refuses_a_path_relative_to_another_drive(tmp_path: Path) -> None:
    # Windows path rules on every host: `D:server` is not absolute there, and never reaches the disk.
    windows_os = ModuleType("os")
    windows_os.path = ntpath  # type: ignore[attr-defined]
    with (
        patch("chrys.service.mcp._stdio_transport.os", windows_os),
        pytest.raises(StdioWorkingDirectoryError) as caught,
    ):
        _check_working_directory("D:server")

    assert caught.value.reason == "not_absolute"
    assert mcp_failure_diagnostic(MCPConnectionError("cwd", "stdio", cause=caught.value)) == (
        "the server's working directory starts with a drive letter and must be a full path, such as D:\\server"
    )


@pytest.mark.skipif(sys.platform == "win32" or os.geteuid() == 0, reason="POSIX permission bits, not as root")
def test_check_working_directory_refuses_a_folder_it_cannot_enter(tmp_path: Path) -> None:
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0)
    try:
        with pytest.raises(StdioWorkingDirectoryError) as caught:
            _check_working_directory(locked)
    finally:
        locked.chmod(0o700)

    assert caught.value.reason == "inaccessible"


def test_a_relative_command_with_a_folder_part_is_read_from_the_cwd(tmp_path: Path) -> None:
    assert _anchor_relative_command(os.path.join("bin", "srv"), str(tmp_path)) == str(tmp_path / "bin" / "srv")
    assert _anchor_relative_command(os.path.join(".", "srv"), str(tmp_path)) == os.path.join(tmp_path, ".", "srv")
    # A bare name is searched on PATH, an absolute path stays, and no cwd leaves the command alone.
    assert _anchor_relative_command("srv", str(tmp_path)) == "srv"
    assert _anchor_relative_command(str(tmp_path / "srv"), str(tmp_path / "other")) == str(tmp_path / "srv")
    assert _anchor_relative_command(os.path.join("bin", "srv"), None) == os.path.join("bin", "srv")


@pytest.mark.skipif(sys.platform == "win32", reason="Windows resolves `..` lexically, before any link")
def test_an_anchored_command_still_steps_back_through_a_linked_folder(tmp_path: Path) -> None:
    package = tmp_path / "package"
    (package / "bin").mkdir(parents=True)
    (package / "server").write_text("", encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    symlink_or_skip(workspace / "bin", package / "bin", target_is_directory=True)

    anchored = _anchor_relative_command("bin/../server", str(workspace))

    # The child would have read `bin/..` from the workspace through the link, landing in the package.
    assert os.path.samefile(anchored, package / "server")


# ── Real child processes ─────────────────────────────────────────────


async def test_a_server_without_cwd_starts_in_the_workspace_folder(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    reported = await _reported_cwd(_cwd_server_config(), workspace)

    assert os.path.samefile(reported, workspace)


async def test_a_relative_cwd_starts_at_the_workspace_folder(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    (workspace / "tools" / "srv").mkdir(parents=True)

    reported = await _reported_cwd(_cwd_server_config(cwd=os.path.join("tools", "srv")), workspace)

    assert os.path.samefile(reported, workspace / "tools" / "srv")


async def test_a_home_cwd_starts_in_the_home_folder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    (home / "srv").mkdir(parents=True)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))

    reported = await _reported_cwd(_cwd_server_config(cwd="~/srv"), workspace)

    assert os.path.samefile(reported, home / "srv")


async def test_an_absolute_cwd_is_used_as_written(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = tmp_path / "elsewhere"
    target.mkdir()

    reported = await _reported_cwd(_cwd_server_config(cwd=str(target)), workspace)

    assert os.path.samefile(reported, target)


@pytest.mark.skipif(sys.platform == "win32", reason="Windows resolves `..` lexically, before any link")
async def test_a_cwd_through_a_linked_folder_starts_where_the_link_leads(tmp_path: Path) -> None:
    external = tmp_path / "external"
    (external / "bin").mkdir(parents=True)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    symlink_or_skip(workspace / "link", external / "bin", target_is_directory=True)

    reported = await _reported_cwd(_cwd_server_config(cwd="link/.."), workspace)

    assert os.path.samefile(reported, external)


async def test_servers_differing_only_in_cwd_do_not_share_a_connection(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    (workspace / "a").mkdir(parents=True)
    (workspace / "b").mkdir()
    cache = MCPConnectionCache()
    first = MCPAdapter(cache=cache, stdio_cwd=str(workspace))
    second = MCPAdapter(cache=cache, stdio_cwd=str(workspace))
    try:
        await asyncio.wait_for(first.connect(_cwd_server_config(cwd="a")), timeout=ENGINE_TURN_TIMEOUT)
        await asyncio.wait_for(second.connect(_cwd_server_config(cwd="b")), timeout=ENGINE_TURN_TIMEOUT)

        assert first._leases["cwd"].key != second._leases["cwd"].key
        assert first._leases["cwd"].mcp_tool is not second._leases["cwd"].mcp_tool
    finally:
        await first.disconnect_all()
        await second.disconnect_all()
        await cache.close_all()


async def test_a_relative_command_is_found_from_the_cwd(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    server_dir = workspace / "srv"
    launcher = _write_launcher(server_dir)
    config = MCPServerConfig(
        name="cwd",
        transport="stdio",
        command=os.path.join(".", launcher.name),
        args=[str(CWD_SERVER)],
        cwd="srv",
    )

    reported = await _reported_cwd(config, workspace)

    assert os.path.samefile(reported, server_dir)


async def test_a_relative_command_without_cwd_is_found_from_the_workspace(tmp_path: Path) -> None:
    # The test runs in its own empty folder, so the process folder holds no ``srv``: on
    # Windows the command used to be looked up there, now it is anchored like on POSIX.
    workspace = tmp_path / "workspace"
    launcher = _write_launcher(workspace / "srv")
    config = MCPServerConfig(
        name="cwd",
        transport="stdio",
        command=os.path.join("srv", launcher.name),
        args=[str(CWD_SERVER)],
    )

    reported = await _reported_cwd(config, workspace)

    assert os.path.samefile(reported, workspace)


# ── The connection test (Test button) ───────────────────────────────


async def test_the_connection_test_starts_where_the_agent_would(tmp_path: Path) -> None:
    # A relative cwd and a command relative to it only both resolve from the server's own folder.
    workspace = tmp_path / "workspace"
    launcher = _write_launcher(workspace / "srv")
    config = MCPServerConfig(
        name="cwd",
        transport="stdio",
        command=os.path.join(".", launcher.name),
        args=[str(CWD_SERVER)],
        cwd="srv",
    )
    adapter = MCPAdapter(stdio_cwd=str(workspace))

    report = await asyncio.wait_for(adapter.test_connection(config), timeout=ENGINE_TURN_TIMEOUT)

    assert "get_cwd" in [name for name, _description in report.tools]


async def test_the_connection_test_reports_a_missing_working_directory(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    adapter = MCPAdapter(stdio_cwd=str(workspace))

    with pytest.raises(MCPConnectionError) as caught:
        await asyncio.wait_for(
            adapter.test_connection(_cwd_server_config(cwd="no-such-folder")), timeout=ENGINE_TURN_TIMEOUT
        )

    assert mcp_failure_diagnostic(caught.value) == "the server's working directory does not exist"


# ── Stock stdio client fallback ──────────────────────────────────────


@asynccontextmanager
async def _stock_client_fallback() -> AsyncIterator[list[Any]]:
    """Force the SDK-internals fallback; yields the server params the stock client received."""
    import mcp.client.stdio as stdio

    received: list[Any] = []

    @asynccontextmanager
    async def fake_stock(server: Any, errlog: Any = sys.stderr) -> AsyncIterator[tuple[str, str]]:
        received.append(server)
        yield ("stock-read", "stock-write")

    with (
        patch.object(
            builtins, "__import__", side_effect=block_import("mcp.client.stdio", "_create_platform_compatible_process")
        ),
        patch.object(stdio, "stdio_client", fake_stock),
    ):
        yield received


async def test_the_stock_fallback_reads_a_relative_command_from_the_cwd(tmp_path: Path) -> None:
    from mcp.client.stdio import StdioServerParameters

    server = StdioServerParameters(command=os.path.join(".", "launch"), cwd=str(tmp_path))

    async with _stock_client_fallback() as received, tolerant_stdio_client(server, inherit_env=False) as streams:
        assert streams == ("stock-read", "stock-write")

    (forwarded,) = received
    assert forwarded.command == os.path.join(tmp_path, ".", "launch")
    assert forwarded.cwd == str(tmp_path)


async def test_the_stock_fallback_names_a_missing_cwd_before_spawning(tmp_path: Path) -> None:
    from mcp.client.stdio import StdioServerParameters

    server = StdioServerParameters(command="python", cwd=str(tmp_path / "missing"))

    async with _stock_client_fallback() as received:
        with pytest.raises(StdioWorkingDirectoryError) as caught:
            async with tolerant_stdio_client(server, inherit_env=False):
                pass

    assert caught.value.reason == "missing"
    assert received == []


# ── Missing folder ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("problem", "detail"),
    [
        ("missing", "the server's working directory does not exist"),
        ("not_directory", "the server's working directory is not a folder"),
        pytest.param(
            "inaccessible",
            "the server's working directory cannot be entered",
            marks=pytest.mark.skipif(
                sys.platform == "win32" or os.geteuid() == 0, reason="POSIX permission bits, not as root"
            ),
        ),
    ],
)
async def test_loading_logs_the_unusable_cwd_and_keeps_the_traceback(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, problem: str, detail: str
) -> None:
    target = tmp_path / "server-folder"

    def prepare() -> None:
        if problem == "not_directory":
            target.write_text("x", encoding="utf-8")
        elif problem == "inaccessible":
            target.mkdir()
            target.chmod(0)

    await asyncio.to_thread(prepare)
    cache = MCPConnectionCache()
    adapter = MCPAdapter(cache=cache, stdio_cwd=str(tmp_path))
    try:
        with caplog.at_level(logging.ERROR, logger="chrys.service.mcp.adapter"):
            await asyncio.wait_for(
                adapter.connect_all([_cwd_server_config(cwd=target.name)]), timeout=ENGINE_TURN_TIMEOUT
            )
        failure = adapter.failures["cwd"]
        records = [record for record in caplog.records if record.name == "chrys.service.mcp.adapter"]
        (record,) = records
        assert f"MCP server 'cwd' (stdio) failed to connect: {detail}" in record.getMessage()
        assert f"Working directory: {target}" in record.getMessage()
        assert record.exc_info is not None
        assert record.exc_info[1] is failure.cause
        assert record.exc_info[2] is not None
    finally:
        if problem == "inaccessible":
            await asyncio.to_thread(target.chmod, 0o700)
        await adapter.disconnect_all()
        await cache.close_all()


async def test_a_missing_cwd_fails_that_server_only_and_says_why(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    cache = MCPConnectionCache()
    adapter = MCPAdapter(cache=cache, stdio_cwd=str(workspace))
    good = _cwd_server_config()
    good.name = "good"
    bad = _cwd_server_config(cwd="no-such-folder")
    bad.name = "bad"
    try:
        tools = await asyncio.wait_for(adapter.connect_all([bad, good]), timeout=ENGINE_TURN_TIMEOUT)
        failure = adapter.failures["bad"]

        assert set(adapter.failures) == {"bad"}
        assert any(tool.name == "get_cwd" for tool in tools)
        assert isinstance(failure, MCPConnectionError)
        assert mcp_failure_diagnostic(failure) == "the server's working directory does not exist"
        assert "no-such-folder" in str(failure)
    finally:
        await adapter.disconnect_all()
        await cache.close_all()


async def test_a_cwd_that_is_a_file_is_named_as_such(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "file").write_text("x", encoding="utf-8")
    cache = MCPConnectionCache()
    adapter = MCPAdapter(cache=cache, stdio_cwd=str(workspace))
    try:
        await asyncio.wait_for(adapter.connect_all([_cwd_server_config(cwd="file")]), timeout=ENGINE_TURN_TIMEOUT)
        failure = adapter.failures["cwd"]

        assert mcp_failure_diagnostic(failure) == "the server's working directory is not a folder"
    finally:
        await adapter.disconnect_all()
        await cache.close_all()
