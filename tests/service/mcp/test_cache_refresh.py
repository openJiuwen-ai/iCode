# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""One manual reload's refresh round, against real stdio MCP server processes."""

from __future__ import annotations

import asyncio
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import psutil
import pytest

from chrys.service.mcp import cache as cache_module
from chrys.service.mcp.adapter import MCPAdapter
from chrys.service.mcp.cache import MCPConnectionCache
from chrys.service.mcp.errors import MCPConnectionError, mcp_failure_diagnostic
from chrys.service.profiles.agents.schema import MCPServerConfig
from tests.support.waiting import ENGINE_TURN_TIMEOUT, wait_for

PROBE_SERVER = Path(__file__).parents[2] / "support" / "mcp_reload_probe_server.py"


def _state(tmp_path: Path, name: str = "probe") -> Path:
    state = tmp_path / name
    state.mkdir(exist_ok=True)
    return state


def _config(state: Path, name: str = "probe", **fields) -> MCPServerConfig:
    return MCPServerConfig(
        name=name,
        transport="stdio",
        command=sys.executable,
        args=[str(PROBE_SERVER), "--state-dir", str(state)],
        **fields,
    )


def _starts(state: Path) -> list[int]:
    path = state / "starts"
    return [int(line) for line in path.read_text(encoding="utf-8").split()] if path.exists() else []


def _flag(state: Path, name: str) -> None:
    (state / name).write_text("", encoding="utf-8")


def _schema(lease) -> dict:
    (function,) = lease.functions
    return function.parameters()


async def _gone(pid: int, process: psutil.Process) -> None:
    await wait_for(lambda: not process.is_running(), timeout=ENGINE_TURN_TIMEOUT, description=f"process {pid} closed")


@pytest.fixture
async def cache() -> AsyncIterator[MCPConnectionCache]:
    owned = MCPConnectionCache()
    try:
        yield owned
    finally:
        await owned.close_all()


async def test_a_refresh_adopts_the_new_declarations_then_closes_the_old_connection(cache, tmp_path) -> None:
    state = _state(tmp_path)
    config = _config(state)
    old = await cache.acquire(config)
    (old_pid,) = _starts(state)
    old_process = psutil.Process(old_pid)
    assert "extra" not in str(_schema(old))
    _flag(state, "extra_parameter")
    (state / "instructions").write_text("use probe", encoding="utf-8")

    cache.begin_refresh()
    new = await cache.acquire(config)
    assert new.mcp_tool is not old.mcp_tool
    assert "extra" in str(_schema(new))
    assert new.mcp_tool._server_instructions == "use probe"
    # The running runtime keeps working until the rebuild has committed.
    assert await old.mcp_tool.probe_session(timeout=5) is None

    assert await cache.commit_refresh() == ()
    assert old_process.is_running()
    await cache.release(old)
    await _gone(old_pid, old_process)
    assert await new.mcp_tool.probe_session(timeout=5) is None
    # Ordinary rebuilds go back to sharing the connection the refresh installed.
    again = await cache.acquire(config)
    assert again.mcp_tool is new.mcp_tool
    assert len(_starts(state)) == 2
    await cache.release(again)
    await cache.release(new)


@pytest.mark.parametrize("no_ping", [False, True], ids=["answers-ping", "method-not-found"])
async def test_a_failed_reconnect_keeps_a_session_that_answers(cache, tmp_path, no_ping) -> None:
    state = _state(tmp_path)
    if no_ping:
        _flag(state, "no_ping")
    config = _config(state)
    old = await cache.acquire(config)
    (old_pid,) = _starts(state)
    _flag(state, "refuse")

    cache.begin_refresh()
    adapter = MCPAdapter(cache=cache)
    (tool,) = await adapter.connect_all([config])
    (report,) = await cache.commit_refresh()

    assert len(_starts(state)) == 2, "the reconnect was really tried"
    assert (report.server_name, report.kept_existing) == ("probe", True)
    assert adapter.failures == {}
    (answer,) = await tool.invoke(label="after fallback")
    assert answer.text == f"answered by {old_pid}"
    await adapter.disconnect_all()
    await cache.release(old)


async def test_a_failed_reconnect_drops_a_session_that_is_gone(cache, tmp_path) -> None:
    state = _state(tmp_path)
    config = _config(state)
    old = await cache.acquire(config)
    (old_pid,) = _starts(state)
    process = psutil.Process(old_pid)
    _flag(state, "refuse")
    process.kill()
    await _gone(old_pid, process)

    cache.begin_refresh()
    adapter = MCPAdapter(cache=cache)
    assert await adapter.connect_all([config]) == []
    (report,) = await cache.commit_refresh()

    assert (report.server_name, report.kept_existing) == ("probe", False)
    assert "existing connection:" in report.detail
    assert "probe" in adapter.failures
    await cache.release(old)
    assert cache._retired == set()
    assert not cache._entries, "a later rebuild must not lease the dead connection"


async def test_a_session_that_does_not_answer_in_time_is_not_kept(cache, tmp_path, monkeypatch) -> None:
    state = _state(tmp_path)
    config = _config(state)
    old = await cache.acquire(config)
    _flag(state, "refuse")
    released = asyncio.Event()

    async def never_answers():
        await released.wait()

    monkeypatch.setattr(old.mcp_tool.session, "send_ping", never_answers)
    monkeypatch.setattr(cache_module, "SESSION_PROBE_TIMEOUT_SECONDS", 0.05)

    cache.begin_refresh()
    with pytest.raises(MCPConnectionError):
        await cache.acquire(config)
    await cache.abort_refresh()
    await cache.release(old)


async def test_a_session_replaced_during_the_check_is_not_taken_for_the_original(cache, tmp_path, monkeypatch) -> None:
    config = _config(_state(tmp_path))
    old = await cache.acquire(config)
    tool = old.mcp_tool
    original = tool.session

    async def reconnects():
        tool.session = object()

    monkeypatch.setattr(original, "send_ping", reconnects)
    try:
        assert "replaced" in await tool.probe_session(timeout=5)
    finally:
        tool.session = original
    await cache.release(old)


async def test_every_consumer_of_a_key_shares_one_attempt(cache, tmp_path) -> None:
    state = _state(tmp_path)
    config = _config(state)
    old = await cache.acquire(config)

    cache.begin_refresh()
    main, child = await asyncio.gather(cache.acquire(config), cache.acquire(config))
    assert main.mcp_tool is child.mcp_tool is not old.mcp_tool
    assert len(_starts(state)) == 2
    await cache.commit_refresh()
    for lease in (old, main, child):
        await cache.release(lease)


async def test_a_connection_the_round_opened_is_not_refreshed_again(cache, tmp_path) -> None:
    state = _state(tmp_path)
    config = _config(state)

    cache.begin_refresh()
    main = await cache.acquire(config)
    child = await cache.acquire(config)
    assert main.mcp_tool is child.mcp_tool
    assert len(_starts(state)) == 1
    await cache.commit_refresh()
    await cache.release(main)
    await cache.release(child)


async def test_abort_closes_the_candidate_and_leaves_the_running_connection(cache, tmp_path) -> None:
    state = _state(tmp_path)
    config = _config(state)
    old = await cache.acquire(config)
    (old_pid,) = _starts(state)

    cache.begin_refresh()
    candidate = await cache.acquire(config)
    (candidate_pid,) = [pid for pid in _starts(state) if pid != old_pid]
    candidate_process = psutil.Process(candidate_pid)
    await cache.release(candidate)
    assert candidate_process.is_running(), "the round still holds it for the next consumer"
    await cache.abort_refresh()
    await _gone(candidate_pid, candidate_process)

    assert await old.mcp_tool.probe_session(timeout=5) is None
    again = await cache.acquire(config)
    assert again.mcp_tool is old.mcp_tool
    await cache.release(again)
    await cache.release(old)


async def test_cancelling_a_reconnect_in_progress_leaves_no_process(cache, tmp_path) -> None:
    state = _state(tmp_path)
    config = _config(state)
    old = await cache.acquire(config)
    (old_pid,) = _starts(state)
    _flag(state, "hold")

    cache.begin_refresh()
    waiting = asyncio.create_task(cache.acquire(config))
    await wait_for(
        lambda: len(_starts(state)) == 2, timeout=ENGINE_TURN_TIMEOUT, description="the reconnect started its process"
    )
    (candidate_pid,) = [pid for pid in _starts(state) if pid != old_pid]
    candidate_process = psutil.Process(candidate_pid)
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    await cache.abort_refresh()
    assert cache._detached, "a hung handshake must not hold up the failure the user is waiting for"
    # The handshake gives its connection back once it ends, as its timeout would have made it.
    (state / "hold").unlink()
    await _gone(candidate_pid, candidate_process)
    await wait_for(lambda: not cache._detached, timeout=ENGINE_TURN_TIMEOUT, description="the cancelled connect ended")
    assert await old.mcp_tool.probe_session(timeout=5) is None
    await cache.release(old)


async def test_abort_closes_a_first_connection_the_failed_build_opened(cache, tmp_path) -> None:
    state = _state(tmp_path)
    config = _config(state)

    cache.begin_refresh()
    lease = await cache.acquire(config)
    (pid,) = _starts(state)
    process = psutil.Process(pid)
    await cache.release(lease)
    await cache.abort_refresh()
    await _gone(pid, process)
    assert not cache._entries


async def test_commit_retires_what_the_rebuild_did_not_ask_for(cache, tmp_path) -> None:
    kept_state, removed_state = _state(tmp_path, "kept"), _state(tmp_path, "removed")
    kept, removed = _config(kept_state, "kept"), _config(removed_state, "removed")
    kept_lease = await cache.acquire(kept)
    removed_lease = await cache.acquire(removed)
    (removed_pid,) = _starts(removed_state)
    process = psutil.Process(removed_pid)

    cache.begin_refresh()
    refreshed = await cache.acquire(kept)
    await cache.commit_refresh()
    assert process.is_running(), "the replaced runtime still holds its lease"
    await cache.release(removed_lease)
    await _gone(removed_pid, process)

    await cache.release(kept_lease)
    await cache.release(refreshed)


async def test_a_changed_key_never_falls_back_to_the_old_connection(cache, tmp_path) -> None:
    state = _state(tmp_path)
    old = await cache.acquire(_config(state))
    _flag(state, "refuse")
    narrowed = _config(state, allowed_tools=["other"])

    cache.begin_refresh()
    adapter = MCPAdapter(cache=cache)
    assert await adapter.connect_all([narrowed]) == []
    (report,) = await cache.commit_refresh()

    assert report.kept_existing is False
    assert "probe" in adapter.failures
    # Healthy as it is, the old connection still closes with the runtime that held it.
    process = psutil.Process(_starts(state)[0])
    await cache.release(old)
    await _gone(process.pid, process)


async def test_close_all_during_a_round_closes_the_candidates(tmp_path) -> None:
    state = _state(tmp_path)
    config = _config(state)
    owned = MCPConnectionCache()
    old = await owned.acquire(config)
    owned.begin_refresh()
    candidate = await owned.acquire(config)
    processes = [psutil.Process(pid) for pid in _starts(state)]
    assert candidate.mcp_tool is not old.mcp_tool
    assert owned.refreshing

    await owned.close_all()

    for process in processes:
        await _gone(process.pid, process)


async def test_only_one_round_runs_at_a_time(cache) -> None:
    cache.begin_refresh()
    with pytest.raises(RuntimeError):
        cache.begin_refresh()
    await cache.abort_refresh()


async def test_a_report_holds_no_value_the_server_was_configured_with(cache, tmp_path, monkeypatch) -> None:
    # A value a command supplied is known to nothing here, so no list of values could mask it.
    secret = "sk-resolved-by-a-command"
    kept = _config(_state(tmp_path, "kept"), "kept")
    gone = _config(_state(tmp_path, "gone"), "gone")
    await cache.acquire(kept)

    async def refused(effective):
        cause = RuntimeError(f"Invalid API key: {secret}")
        raise MCPConnectionError(effective.config.name, effective.config.transport, cause)

    monkeypatch.setattr(cache, "_open_mcp_tool", refused)
    cache.begin_refresh()
    await cache.acquire(kept)
    with pytest.raises(MCPConnectionError) as failure:
        await cache.acquire(gone)
    cache.note_connect_failure(gone, failure.value)
    reports = {report.server_name: report for report in await cache.commit_refresh()}

    assert reports["kept"].kept_existing and not reports["gone"].kept_existing
    for report in reports.values():
        assert report.detail == "failed to connect"


def _http_status_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://user:hunter22@host.test/s/path-token/mcp?token=abc123")
    return httpx.HTTPStatusError(
        f"Client error for url {request.url}", request=request, response=httpx.Response(status, request=request)
    )


@pytest.mark.parametrize(
    ("cause", "detail"),
    [
        (_http_status_error(401), "the server rejected the credentials"),
        (_http_status_error(500), "the server reported an error"),
        (ExceptionGroup("task group", [_http_status_error(401)]), "the server rejected the credentials"),
        (httpx.ConnectTimeout("connect to https://host.test/s/path-token"), "connecting timed out"),
        (RuntimeError("Invalid API key: sk-secret-value"), "failed to connect"),
    ],
    ids=["unauthorized", "server-error", "in-a-group", "connect-timeout", "unclassified"],
)
def test_a_diagnostic_is_a_fixed_phrase_for_the_kind_of_failure(cause, detail) -> None:
    error = MCPConnectionError("probe", "http", cause, stderr_tail="stderr holds another-secret")
    assert mcp_failure_diagnostic(error) == detail


def test_a_diagnostic_names_a_command_that_cannot_start_and_the_exit_code() -> None:
    missing = MCPConnectionError("probe", "stdio", FileNotFoundError(2, "No such file", "/secret/path"))
    assert mcp_failure_diagnostic(missing) == "the server command could not be started"
    exited = MCPConnectionError("probe", "stdio", RuntimeError("closed sk-secret-value"), process_exit_code=3)
    assert mcp_failure_diagnostic(exited) == "failed to connect (exit code 3)"
