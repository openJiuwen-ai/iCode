# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""ACP workspace behavior follows registry membership while retaining its existing workspace contract."""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from functools import partial
from pathlib import Path
from threading import Event
from unittest.mock import create_autospec

import pytest
from acp import RequestError, text_block
from acp import schema as acp_schema

from chrys.app.acp import session_manager
from chrys.app.acp.server import ChrysAcpServer
from chrys.app.acp.session_manager import AcpSessionError, AcpSessionManager
from chrys.foundation.config.settings_store import load_settings
from chrys.foundation.config.user_settings import user_settings_path
from chrys.foundation.events.types import WorkspaceUpdated
from chrys.foundation.util.lock import FileLock
from chrys.orchestration.engine import workspaces as engine_workspaces
from chrys.orchestration.engine.build import builder
from chrys.orchestration.engine.loader import AgentLoader
from chrys.service.approval.policy import ApprovalMode
from chrys.service.llm.clients import create_client
from chrys.service.llm.mock import MockResponse
from chrys.service.state.store import JsonFileStateStore
from chrys.service.workspaces import registry as registry_module
from chrys.service.workspaces.registry import WorkspaceRegistry
from tests.app.acp._server_fakes import _FakeClient
from tests.support.async_tasks import running_task, wait_for_started
from tests.support.event_capture import capture_event_sequence
from tests.support.rollback_spec import PROFILE, SpecClient, answer, ask
from tests.support.symlinks import symlink_or_skip
from tests.support.waiting import ENGINE_TURN_TIMEOUT, wait_for
from tests.support.workspace_engine import (
    HeldFirstCallClient,
    WorkspaceBoundary,
    before_await,
    freeze_workspace_settings,
    mock_workspace_runtime,
    running_acp_manager,
    select_workspace,
)
from tests.support.workspace_registry import default_workspace_registry as default_workspace_registry
from tests.support.workspace_registry import (
    make_workdir_alias,
    make_workdirs,
    registry_disk_full,
    retarget_workdir_alias,
    save_chat_session,
)


@pytest.fixture
async def workspace_acp(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> AsyncIterator[AcpSessionManager]:
    loaded, profiles, models = mock_workspace_runtime(monkeypatch)
    monkeypatch.setattr(
        session_manager, "load_settings", create_autospec(session_manager.load_settings, return_value=loaded)
    )
    async with running_acp_manager(
        loaded,
        profiles,
        models,
        JsonFileStateStore(tmp_path / "sessions", workspace_registry=WorkspaceRegistry()),
        profile_name=PROFILE.name,
        approval_mode=ApprovalMode.BYPASS,
        process_cwd=str(tmp_path),
    ) as manager:
        yield manager


@pytest.fixture
def save_acp_chat(workspace_acp):
    return partial(save_chat_session, workspace_acp.state_store, profile=PROFILE.name)


@pytest.fixture
def connected_acp(workspace_acp):
    server = ChrysAcpServer(workspace_acp, initial_vision=False)
    client = _FakeClient()
    server.on_connect(client)
    return server, client


async def test_a_workspace_change_tells_the_client_its_workspace_notice_once(
    registry, save_acp_chat, connected_acp, tmp_path, monkeypatch
):
    from chrys.service.workspaces import registry as registry_module

    primary, outside = make_workdirs(tmp_path, "primary", "outside")
    workspace = registry.create("Team", str(primary))
    await save_acp_chat("target", primary)
    registry.set_session_workspace("target", workspace.id)
    server, client = connected_acp
    await server.load_session("target", cwd=str(primary))

    monkeypatch.setattr(registry_module, "atomic_write_text", registry_disk_full)
    update = await server.ext_method("session/set_workspace", {"sessionId": "target", "primaryCwd": str(outside)})
    assert update["primaryCwd"] == str(outside)
    (warning,) = [payload for method, payload in client.ext_notifications if method == "chrys/warning"]
    assert (warning["sessionId"], warning["code"]) == ("target", "workspace_membership_write_failed")
    assert "Team" in warning["message"] and "reopened" in warning["message"]
    assert registry.snapshot().workspace_id_for_session("target") == workspace.id
    await server.close_session("target")


@pytest.mark.skipif(sys.platform == "win32", reason="Win32 directory creation strips trailing spaces")
async def test_acp_lists_and_loads_registered_workspace_with_trailing_space(
    registry, save_acp_chat, connected_acp, tmp_path
):
    _, primary = make_workdirs(tmp_path, "project", "project ")
    workspace = registry.create("Team", str(primary))
    await save_acp_chat("target", primary)
    registry.set_session_workspace("target", workspace.id)
    server, _ = connected_acp

    listing = await server.list_sessions(cwd=str(primary))
    assert [row.session_id for row in listing.sessions] == ["target"]
    await server.load_session("target", cwd=str(primary))
    await server.close_session("target")


async def test_acp_failed_restore_reports_error_and_keeps_checkpoint(
    registry, workspace_acp, save_acp_chat, connected_acp, tmp_path, monkeypatch
):
    primary, previous = make_workdirs(tmp_path, "primary", "previous")
    workspace = registry.create("Team", str(primary))
    await save_acp_chat("target", previous)
    registry.set_session_workspace("target", workspace.id)
    checkpoint = workspace_acp.state_store.session_dir("target") / "session.json"
    original = checkpoint.read_bytes()
    server, _ = connected_acp
    with monkeypatch.context() as patch:
        patch.setattr(
            AgentLoader,
            "finish_load",
            create_autospec(AgentLoader.finish_load, side_effect=RuntimeError("load failed")),
        )
        with pytest.raises(RequestError) as error:
            await server.load_session("target", cwd=str(primary))
        assert "load failed" in error.value.data["details"]
    assert checkpoint.read_bytes() == original
    await server.load_session("target", cwd=str(primary))
    await server.close_session("target")


@pytest.mark.parametrize("target_turn", [0, 1])
async def test_acp_rollback_build_failure_reports_error_without_waiting_for_timeout(
    workspace_acp, connected_acp, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, target_turn: int
) -> None:
    server, client = connected_acp
    monkeypatch.setattr(
        builder,
        "create_client",
        create_autospec(create_client, return_value=SpecClient([answer("one"), answer("two")])),
    )
    opened = await server.new_session(cwd=str(tmp_path))
    sid = opened.session_id
    engine = workspace_acp.get(sid).host.engine
    await ask(engine, "first")
    await ask(engine, "second")

    with monkeypatch.context() as patch:
        patch.setattr(
            builder, "create_client", create_autospec(create_client, side_effect=RuntimeError("build failed"))
        )
        async with asyncio.timeout(10):
            with pytest.raises(RequestError) as caught:
                await server.ext_method("session/rollback", {"sessionId": sid, "targetTurn": target_turn})
    assert caught.value.data["details"] == "build failed"
    assert engine.session_id is None
    assert engine.history_messages == []
    assert all(method != "chrys/rollback_result" for method, _ in client.ext_notifications)

    await server.close_session(sid)
    if target_turn == 0:
        assert not (workspace_acp.state_store.session_dir(sid) / "session.json").exists()
    else:
        await server.load_session(sid, cwd=str(tmp_path))
        reopened = workspace_acp.get(sid).host.engine
        assert [message.text for message in reopened.history_messages if message.role == "user"] == ["first"]


@pytest.mark.parametrize("control", ["agent", "approval", "config"])
async def test_acp_rejects_old_controls_then_reopens_committed_rollback_history(
    workspace_acp, connected_acp, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, control: str
) -> None:
    server, _ = connected_acp
    workspace_acp._agent_registry.register(replace(PROFILE, name="Other"))
    opened = await server.new_session(cwd=str(tmp_path))
    sid = opened.session_id
    outgoing = workspace_acp.get(sid)
    await ask(outgoing.host.engine, "first")
    await ask(outgoing.host.engine, "second")

    with monkeypatch.context() as patch:
        patch.setattr(
            AgentLoader,
            "finish_load",
            create_autospec(AgentLoader.finish_load, side_effect=RuntimeError("restore build failed")),
        )
        with pytest.raises((RequestError, TimeoutError)) as rollback_error:
            async with asyncio.timeout(2):
                await server.ext_method("session/rollback", {"sessionId": sid, "targetTurn": 1})
    assert outgoing.host.engine.current.loaded is None
    settings = user_settings_path()
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text("ui:\n  theme: textual-dark\n", encoding="utf-8")
    before = settings.read_bytes()
    rejection = None
    try:
        async with asyncio.timeout(2):
            if control == "agent":
                await server.ext_method("session/switch_agent", {"sessionId": sid, "agentProfile": "Other"})
            elif control == "approval":
                await server.set_session_mode("manual", sid)
            else:
                await server.ext_method(
                    "session/set_config_option", {"sessionId": sid, "key": "rollback_snapshots_keep", "value": 7}
                )
    except (RequestError, TimeoutError) as exc:
        rejection = exc

    assert settings.read_bytes() == before
    assert outgoing.host.engine.session_id is None
    assert outgoing.host.engine.approval_mode is ApprovalMode.BYPASS
    assert isinstance(rejection, RequestError)
    assert rejection.data["details"] == f"ACP session is not active: {sid}"
    assert isinstance(rollback_error.value, RequestError)
    assert rollback_error.value.data["details"] == "restore build failed"
    store = workspace_acp.state_store
    with FileLock(store.active_lock_path(sid), timeout=0):
        state = await store.load_session(sid)
        assert state is not None and state["turn_counter"] == 1

    await server.load_session(sid, cwd=str(tmp_path))
    restored = workspace_acp.get(sid)
    assert restored is not outgoing
    async with asyncio.timeout(ENGINE_TURN_TIMEOUT):
        result = await server.prompt([text_block("continue")], session_id=sid)
    assert result.stop_reason == "end_turn"
    assert [message.text for message in restored.host.engine.history_messages if message.role == "user"] == [
        "first",
        "continue",
    ]


@pytest.mark.parametrize("build_fails", [False, True], ids=["success", "failure"])
async def test_acp_prompt_admission_waits_for_rollback(
    workspace_acp, save_acp_chat, connected_acp, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, build_fails: bool
) -> None:
    server, _ = connected_acp
    sid = (await server.new_session(cwd=str(tmp_path))).session_id
    outgoing = workspace_acp.get(sid)
    engine = outgoing.host.engine
    await ask(engine, "first")
    await ask(engine, "second")
    await save_acp_chat("saved-b", tmp_path)

    boundary = WorkspaceBoundary()
    build = engine.loader.build

    async def blocked_build(*args, **kwargs):
        await boundary.wait()
        if build_fails:
            raise RuntimeError("restore build failed")
        return await build(*args, **kwargs)

    monkeypatch.setattr(engine.loader, "build", blocked_build)
    prompt_at_gate = asyncio.Event()

    async def observe_prompt_gate():
        prompt_at_gate.set()

    before_await(monkeypatch, engine.permits, "wait_for_agent_load_idle", observe_prompt_gate)
    load_started = asyncio.Event()

    async def reload_a():
        load_started.set()
        return await workspace_acp.load_session(cwd=str(tmp_path), session_id=sid, mcp_servers=None)

    rollback = asyncio.create_task(server.ext_method("session/rollback", {"sessionId": sid, "targetTurn": 1}))
    tasks = [rollback]
    try:
        async with asyncio.timeout(ENGINE_TURN_TIMEOUT):
            await boundary.entered.wait()
            assert engine.current.loaded is None
            prompt = asyncio.create_task(server.prompt([text_block("continue")], session_id=sid))
            tasks.append(prompt)
            await prompt_at_gate.wait()
            assert not prompt.done()
            if not build_fails:
                reload = asyncio.create_task(reload_a())
                tasks.append(reload)
                await load_started.wait()
                new_b, _ = await asyncio.gather(
                    server.new_session(cwd=str(tmp_path)), server.load_session("saved-b", cwd=str(tmp_path))
                )
                assert new_b.session_id != sid
                loaded = await reload
                assert loaded.reused_existing and loaded.session is outgoing
                assert not rollback.done() and not prompt.done()
            boundary.open()
            if build_fails:
                with pytest.raises(RequestError) as prompt_error:
                    await prompt
                assert "turnId" not in prompt_error.value.data
                assert engine.session_id is None
                assert engine.current.loaded is None
                state = await workspace_acp.state_store.load_session(sid)
                assert state is not None and state["turn_counter"] == 1
            else:
                await rollback
                assert (await prompt).stop_reason == "end_turn"
                assert workspace_acp.get(sid) is outgoing
                assert [message.text for message in engine.history_messages if message.role == "user"] == [
                    "first",
                    "continue",
                ]
    finally:
        boundary.open()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def test_acp_concurrent_loads_install_one_host(
    workspace_acp, save_acp_chat, connected_acp, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await save_acp_chat("saved-a", tmp_path)
    server, _ = connected_acp
    start = WorkspaceBoundary()
    before_await(monkeypatch, workspace_acp, "_start_host", start.wait)
    watched = []
    watch = server._watch_session_titles

    async def record_watcher(session):
        watched.append(session)
        await watch(session)

    monkeypatch.setattr(server, "_watch_session_titles", record_watcher)
    second_started = asyncio.Event()

    async def load_second():
        second_started.set()
        return await server.load_session("saved-a", cwd=str(tmp_path))

    first = asyncio.create_task(server.load_session("saved-a", cwd=str(tmp_path)))
    try:
        async with asyncio.timeout(ENGINE_TURN_TIMEOUT):
            await start.entered.wait()
            second = asyncio.create_task(load_second())
            try:
                await second_started.wait()
                assert not second.done()
                start.open()
                await asyncio.gather(first, second)
            finally:
                if not second.done():
                    second.cancel()
                    await asyncio.gather(second, return_exceptions=True)
    finally:
        start.open()
        if not first.done():
            first.cancel()
            await asyncio.gather(first, return_exceptions=True)
    assert len(watched) == 1
    assert watched[0] is workspace_acp.get("saved-a")


async def test_acp_load_stops_replaying_when_its_host_is_replaced(
    workspace_acp, connected_acp, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server, client = connected_acp
    sid = (await server.new_session(cwd=str(tmp_path))).session_id
    await ask(workspace_acp.get(sid).host.engine, "first")
    await ask(workspace_acp.get(sid).host.engine, "second")
    await server.close_session(sid)

    replay = WorkspaceBoundary()
    update = client.session_update

    async def block_after_first_replay(*args, **kwargs):
        await update(*args, **kwargs)
        if kwargs["update"].session_update == "user_message_chunk":
            await replay.wait()

    monkeypatch.setattr(client, "session_update", block_after_first_replay)
    old_load = asyncio.create_task(server.load_session(sid, cwd=str(tmp_path)))
    try:
        async with asyncio.timeout(ENGINE_TURN_TIMEOUT):
            await replay.entered.wait()
            old = workspace_acp.get(sid)
            await server.close_session(sid)
            await server.load_session(sid, cwd=str(tmp_path))
            replacement = workspace_acp.get(sid)
            assert replacement is not old
            updates = len(client.updates)
            replay.open()
            with pytest.raises(RequestError) as error:
                await old_load
            assert error.value.data["details"] == f"ACP session is not active: {sid}"
            assert len(client.updates) == updates
            assert workspace_acp.get(sid) is replacement
    finally:
        replay.open()
        if not old_load.done():
            old_load.cancel()
        await asyncio.gather(old_load, return_exceptions=True)


@pytest.mark.parametrize("candidates", ["mixed", "workspace"])
async def test_stale_additional_directory_only_blocks_unspecified_list_candidates(
    registry, workspace_acp, tmp_path, candidates
):
    store = JsonFileStateStore(tmp_path / "sessions")
    workspace = registry.create("Team", str(tmp_path))
    await store.save_session("workspace", {"messages": []}, primary_cwd=str(tmp_path))
    registry.set_session_workspace("workspace", workspace.id)
    if candidates == "mixed":
        (tmp_path / "other").mkdir()
        await store.save_session("unspecified", {"messages": []}, primary_cwd=str(tmp_path))
        rows, _ = await workspace_acp.list_sessions(
            cwd=str(tmp_path), cursor=None, additional_directories=[str(tmp_path / "other")]
        )
        assert [row.session_id for row in rows] == ["workspace"]
    stale = str(tmp_path / "deleted")

    if candidates != "workspace":
        with pytest.raises(AcpSessionError) as caught:
            await workspace_acp.list_sessions(cwd=str(tmp_path), cursor=None, additional_directories=[stale])
        assert str(caught.value) == f"cwd does not exist: {stale}"
    else:
        rows, cursor = await workspace_acp.list_sessions(cwd=str(tmp_path), cursor=None, additional_directories=[stale])
        assert [row.session_id for row in rows] == ["workspace"]
        assert cursor is None


async def test_acp_list_silently_falls_back_to_saved_cwds_with_corrupt_registry(
    registry, workspace_acp, connected_acp, tmp_path
):
    store = workspace_acp._state_store
    extra = tmp_path / "extra"
    extra.mkdir()
    workspace = registry.create("Team", str(tmp_path))
    registry.set_session_workspace("workspace", workspace.id)
    for session_id in ("workspace", "unspecified"):
        await save_chat_session(store, session_id, tmp_path, workdirs=[tmp_path, extra])
    registry.path.write_text("broken", encoding="utf-8")
    server, client = connected_acp
    listing = await server.list_sessions(cwd=str(tmp_path), additional_directories=[str(extra)])
    assert {row.session_id: (row.cwd, row.additional_directories) for row in listing.sessions} == {
        session_id: (str(tmp_path), [str(extra)]) for session_id in ("workspace", "unspecified")
    }
    assert not [payload for method, payload in client.ext_notifications if method == "chrys/warning"]
    assert registry.snapshot().error is not None


@pytest.mark.parametrize("primary_state", ["deleted-after-validation", "missing-before-validation"])
async def test_cold_load_validates_the_workspace_primary(
    registry, workspace_acp, save_acp_chat, tmp_path, monkeypatch, primary_state
):
    primary = tmp_path / "primary"
    primary.mkdir()
    workspace = registry.create("Team", str(primary))
    registry.set_session_workspace("target", workspace.id)
    await save_acp_chat("target", tmp_path)
    if primary_state == "missing-before-validation":
        primary.rmdir()

        with pytest.raises(AcpSessionError) as caught:
            await workspace_acp.load_session(cwd=str(tmp_path), session_id="target", mcp_servers=None)
        assert str(caught.value) == f"The primary folder of workspace “Team”, {primary}, does not exist."
        return

    load = workspace_acp._session_loaded_settings

    def delete_after_cwd_validation(cwd):
        registry.delete(workspace.id)
        return load(cwd)

    with monkeypatch.context() as patch:
        patch.setattr(workspace_acp, "_session_loaded_settings", delete_after_cwd_validation)
        loaded = await workspace_acp.load_session(cwd=str(primary), session_id="target", mcp_servers=None)

    assert loaded.session.cwd == str(primary)
    assert loaded.session.host.engine.workspace.name == "Team"
    await workspace_acp.close("target")
    with pytest.raises(AcpSessionError, match="belongs to a different workspace"):
        await workspace_acp.load_session(cwd=str(primary), session_id="target", mcp_servers=None)


async def test_cold_load_uses_the_validated_cwd_when_the_workspace_primary_retargets(
    registry, workspace_acp, save_acp_chat, tmp_path, monkeypatch
):
    from chrys.service.profiles.agents.schema import MCPServerConfig
    from tests.service.mcp.test_stdio_cwd import CWD_SERVER

    first, second, alias = make_workdir_alias(tmp_path, "first", "second", "primary")
    workspace = registry.create("Team", str(alias))
    registry.set_session_workspace("target", workspace.id)
    await save_acp_chat("target", alias)
    report = tmp_path / "mcp-cwd.txt"
    probe = (
        "import os, runpy, sys\n"
        "from pathlib import Path\n"
        "Path(sys.argv[1]).write_text(os.getcwd(), encoding='utf-8')\n"
        "runpy.run_path(sys.argv[2], run_name='__main__')\n"
    )
    server = MCPServerConfig(
        name="cwd", transport="stdio", command=sys.executable, args=["-c", probe, str(report), str(CWD_SERVER)]
    )
    workspace_acp._agent_registry.register(replace(PROFILE, tools=replace(PROFILE.tools, mcp=[server])))
    load_settings = workspace_acp._session_loaded_settings

    def retarget_before_loading_settings(cwd):
        retarget_workdir_alias(alias, second)
        return load_settings(cwd)

    monkeypatch.setattr(workspace_acp, "_session_loaded_settings", retarget_before_loading_settings)
    result = await workspace_acp.load_session(cwd=str(first), session_id="target", mcp_servers=None)
    assert Path(report.read_text(encoding="utf-8")).samefile(first)
    engine = result.session.host.engine
    assert engine.workspace.primary_cwd == engine.current.require_loaded().runtime.cwd == str(first.resolve())
    assert registry.snapshot().workspace_id_for_session("target") == workspace.id


async def test_acp_unspecified_change_keeps_the_destination_unspecified(registry, connected_acp, tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    registry.create("Team", str(target))
    server, _ = connected_acp
    session_id = (await server.new_session(cwd=str(tmp_path))).session_id
    reply = await server.ext_method("session/set_workspace", {"sessionId": session_id, "primaryCwd": str(target)})
    assert reply == {"sessionId": session_id, "primaryCwd": str(target), "workingDirs": [], "referenceFiles": []}
    assert not registry.snapshot().sessions
    await server.close_session(session_id)


@pytest.fixture
async def acp_wire(workspace_acp, monkeypatch):
    async with _acp_connection(workspace_acp, monkeypatch) as connection:
        yield connection


@asynccontextmanager
async def _acp_connection(workspace_acp, monkeypatch):
    """Run the production ACP transport over a local stream pair."""
    import asyncio
    import socket

    from chrys.app.cli import acp as acp_cli
    from tests.support.async_tasks import running_task

    agent_socket, client_socket = socket.socketpair()
    agent_input, agent_output = await asyncio.open_connection(sock=agent_socket)
    client_input, client_output = await asyncio.open_connection(sock=client_socket)

    async def streams(**kwargs):
        return agent_input, agent_output

    monkeypatch.setattr(acp_cli, "stdio_streams", streams)
    server = ChrysAcpServer(workspace_acp, initial_vision=False)
    next_id = 0

    async def request(method, **params):
        nonlocal next_id
        next_id += 1
        frame = {"jsonrpc": "2.0", "id": next_id, "method": method, "params": params}
        client_output.write(json.dumps(frame).encode() + b"\n")
        await client_output.drain()
        notices = []
        async with asyncio.timeout(10):
            while line := await client_input.readline():
                message = json.loads(line)
                if message.get("id") == next_id:
                    return message, notices
                notices.append(message)
        pytest.fail(f"ACP stream closed before answering {method}")

    try:
        async with running_task(acp_cli._serve_agent(server)):
            initialized, _ = await request("initialize", protocolVersion=1)
            assert "result" in initialized
            yield server, request
    finally:
        for writer in (client_output, agent_output):
            writer.close()
        await asyncio.gather(client_output.wait_closed(), agent_output.wait_closed())


async def test_acp_workspace_commands_update_the_live_session_without_a_turn(
    registry, workspace_acp, tmp_path, acp_wire
):
    _, request = acp_wire
    cwd, reference, other_cwd = make_workdirs(tmp_path, "project", "Reference Files", "other `folder")
    other = registry.create("# Other workspace", str(other_cwd))
    new, updates = await request("session/new", cwd=str(cwd), mcpServers=[])
    sid = new["result"]["sessionId"]
    assert "configOptions" not in new["result"]
    _assert_workspace_command_list(updates)
    engine = workspace_acp.get(sid).host.engine

    async def command(text):
        message, _ = await _workspace_slash_reply(request, sid, text)
        return message

    assert await command("/workspace rename No workspace") == (
        "This session is not in a workspace. Use /workspace switch \\<name\\> to enter one."
    )
    assert await command("/workspace") == (
        "Usage\n\n```text\n/workspace switch [<name>]\n/workspace new <name>\n"
        "/workspace rename <new name>\n/workspace add-folder <path>\n/workspace remove-folder <path>\n"
        "/workspace delete <name>\n```\n\n"
        "The primary folder cannot be changed. Create a new workspace to use a different primary folder."
    )
    assert await command("/workspace delete Unspecified") == "Unspecified is a reserved item and cannot be deleted."
    assert await command("/workspace status") == "Unknown workspace action “status”. Run /workspace for usage."
    assert await command("/workspace new switch") == (
        "Created workspace “switch” and moved this session into it.\n\n"
        r"To add reference folders, use /workspace add-folder \<path\>."
    )
    created = registry.snapshot().session_workspace(sid)
    assert created is not None and Path(created.primary_cwd).samefile(cwd)
    assert created.working_dirs == []
    assert engine.workspace == created
    listed = await command("/workspace switch")
    assert listed == (
        rf"- \# Other workspace (``{other_cwd}``)"
        f"\n- switch (`{cwd}`) — current workspace"
        "\n- Unspecified"
        "\n\nRun /workspace for usage."
    )
    assert (await command(f"/workspace switch {other.id}")).replace(r"\_", "_") == f"No workspace matches {other.id}."
    assert engine.workspace == created

    assert await command("/workspace rename My Workspace") == "Renamed the workspace to “My Workspace”."
    assert engine.workspace.name == "My Workspace"
    assert await command("/workspace add-folder ../Reference Files") == f"Added reference folder `{reference}`."
    assert [Path(folder.path).resolve() for folder in engine.workspace.working_dirs] == [reference.resolve()]
    assert await command("/workspace remove-folder ../Reference Files") == f"Removed reference folder `{reference}`."
    assert engine.workspace.working_dirs == []
    assert await command("/workspace rename switch") == "Renamed the workspace to “switch”."
    assert await command("/workspace delete SWITCH") == "Deleted workspace “switch”. This session is now Unspecified."
    assert registry.snapshot().workspace(created.id) is None
    assert registry.snapshot().workspace(other.id) == other
    assert engine.session.workspace_id is None
    assert Path(engine.workspace.primary_cwd).samefile(cwd)

    await command("/workspace new Switch from here")
    assert await command("/workspace switch Unspecified") == "This session is Unspecified."
    assert engine.session.workspace_id is None
    assert registry.snapshot().session_workspace(sid) is None
    assert Path(engine.workspace.primary_cwd).samefile(cwd)
    message, notices = await _workspace_slash_reply(request, sid, "/workspace switch uNsPeCiFiEd")
    assert message == "This session is Unspecified."
    assert not any(notice["method"] == "_chrys/workspace_updated" for notice in notices)
    assert "- Unspecified — current" in await command("/workspace switch")
    assert not (await workspace_acp.state_store.load_session_transcript(sid))

    legacy = "/workspace-manage new Legacy"
    reply, _ = await request("session/prompt", sessionId=sid, prompt=[{"type": "text", "text": legacy}])
    assert reply["result"]["_meta"]["chrys"]["turnNumber"] == 1
    await request("session/close", sessionId=sid)
    reopened, updates = await request("session/load", cwd=str(cwd), sessionId=sid, mcpServers=[])
    assert "result" in reopened
    assert "configOptions" not in reopened["result"]
    _assert_workspace_command_list(updates)
    user_messages = _acp_workspace_updates(updates, "user_message_chunk")
    assert [message["content"]["text"] for message in user_messages] == [legacy]
    assert len(_acp_workspace_updates(updates, "agent_message_chunk")) == 1
    await request("session/close", sessionId=sid)


async def test_acp_cold_reconnect_uses_empty_definition_when_creating_a_workspace(workspace_acp, tmp_path, monkeypatch):
    cwd, retained = make_workdirs(tmp_path, "primary", "Retained")
    options = {"cwd": str(cwd), "mcpServers": []}
    async with _acp_connection(workspace_acp, monkeypatch) as (_, request):
        new, _ = await request("session/new", **options, additionalDirectories=[str(retained)])
        sid = new["result"]["sessionId"]
        prompt, _ = await request("session/prompt", sessionId=sid, prompt=[{"type": "text", "text": "hello"}])
        assert prompt["result"]["stopReason"] == "end_turn"
        closed, _ = await request("session/close", sessionId=sid)
        assert "result" in closed

    async with _acp_connection(workspace_acp, monkeypatch) as (_, request):
        loaded, _ = await request("session/load", sessionId=sid, **options)
        assert "result" in loaded
        workspace = workspace_acp.get(sid).host.engine.workspace
        assert not workspace.id
        assert [d.path for d in workspace.working_dirs if not d.is_primary] == [str(retained)]
        message, notices = await _workspace_slash_reply(request, sid, "/workspace new Reopened Workspace")
        assert message == (
            "Created workspace “Reopened Workspace” and moved this session into it.\n\n"
            r"To add reference folders, use /workspace add-folder \<path\>."
        )
        (update,) = [notice["params"] for notice in notices if notice["method"] == "_chrys/workspace_updated"]
        assert update["workingDirs"] == []
        listed, _ = await request("session/list", cwd=str(cwd))
        (session,) = listed["result"]["sessions"]
        assert session["sessionId"] == sid
        assert not session.get("additionalDirectories")
        await request("session/close", sessionId=sid)


@pytest.mark.parametrize(
    ("with_history", "expected"),
    [
        (False, "Moved to workspace “switch”."),
        (
            True,
            (
                "Moved this session to workspace “switch”. To start a new session in this workspace, "
                "start a new conversation in your editor, then enter /workspace switch switch."
            ),
        ),
    ],
    ids=["empty", "with-history"],
)
async def test_acp_can_join_a_workspace_in_the_current_folder(
    registry, workspace_acp, tmp_path, acp_wire, with_history, expected
):
    _, request = acp_wire
    cwd, refs = make_workdirs(tmp_path, "current", "reference")
    target = registry.create("switch", str(cwd), [str(refs)])
    new, _ = await request("session/new", cwd=str(cwd), mcpServers=[])
    sid = new["result"]["sessionId"]
    if with_history:
        prompt, _ = await request("session/prompt", sessionId=sid, prompt=[{"type": "text", "text": "hello"}])
        assert prompt["result"]["stopReason"] == "end_turn"
    engine = workspace_acp.get(sid).host.engine
    before = [(message.role, message.text) for message in engine.history_messages]

    message, _ = await _workspace_slash_reply(request, sid, "/workspace switch SWITCH")

    assert message == expected
    assert registry.snapshot().session_workspace(sid) == target
    assert engine.workspace.id == target.id
    assert Path(engine.workspace.primary_cwd).samefile(cwd)
    assert [Path(folder.path).resolve() for folder in engine.workspace.working_dirs] == [refs.resolve()]
    assert [(message.role, message.text) for message in engine.history_messages] == before
    installed = engine.current.require_loaded()
    saved = registry.path.read_bytes()
    message, notices = await _workspace_slash_reply(request, sid, "/workspace switch SWITCH")
    assert message == "This session is already in workspace “switch”."
    assert engine.current.require_loaded() is installed
    assert engine.workspace == target
    assert registry.snapshot().session_workspace(sid) == target
    assert registry.path.read_bytes() == saved
    assert all(notice["method"] != "_chrys/workspace_updated" for notice in notices)
    if with_history:
        await request("session/close", sessionId=sid)
        listed, _ = await request("session/list", cwd=str(cwd))
        assert [row["sessionId"] for row in listed["result"]["sessions"]] == [sid]
        reopened, _ = await request("session/load", sessionId=sid, cwd=str(cwd), mcpServers=[])
        assert "result" in reopened
    await request("session/close", sessionId=sid)


async def test_acp_workspace_selection_uses_physical_directory_identity(registry, workspace_acp, tmp_path, acp_wire):
    _, request = acp_wire
    cwd, _, alias = make_workdir_alias(tmp_path, "current", "other", "alias")
    target = registry.create("Alias", str(alias))
    new, _ = await request("session/new", cwd=str(cwd), mcpServers=[])
    sid = new["result"]["sessionId"]

    message, _ = await _workspace_slash_reply(request, sid, "/workspace switch alias")

    assert message == "Moved to workspace “Alias”."
    assert registry.snapshot().session_workspace(sid) == target
    assert Path(workspace_acp.get(sid).cwd).samefile(cwd)
    await request("session/close", sessionId=sid)


async def test_acp_reselect_after_failed_switch_keeps_editor_directories(
    registry, workspace_acp, save_acp_chat, tmp_path, acp_wire, monkeypatch
):
    _, request = acp_wire
    primary, registered, editor, added = make_workdirs(tmp_path, "primary", "registered", "editor", "added")
    current = registry.create("Current", str(primary), [str(registered)])
    target = registry.create("Target", str(primary))
    sid = "target"
    await save_acp_chat(sid, primary)
    registry.set_session_workspace(sid, current.id)
    load = {"sessionId": sid, "cwd": str(primary), "mcpServers": [], "additionalDirectories": [str(editor)]}
    loaded, _ = await request("session/load", **load)
    assert "result" in loaded
    engine = workspace_acp.get(sid).host.engine
    installed = engine.current.require_loaded()
    before = engine.workspace
    assert [folder.path for folder in before.working_dirs] == [str(editor)]
    # Matching explicit roots and omission reuse the installed editor scope; conflicting roots do not.
    options = {key: value for key, value in load.items() if key != "additionalDirectories"}
    for roots in ({"additionalDirectories": [str(editor)]}, {}):
        reopened, _ = await request("session/load", **options, **roots)
        assert "result" in reopened
    refused, _ = await request("session/load", **options, additionalDirectories=[])
    assert "Close it before reloading with additional directories" in refused["error"]["data"]["details"]
    assert [folder.path for folder in engine.workspace.working_dirs] == [str(editor)]
    assert registry.snapshot().workspace(current.id) == current
    with monkeypatch.context() as patch:
        patch.setattr(AgentLoader, "build", create_autospec(AgentLoader.build, side_effect=RuntimeError("load failed")))
        message, _ = await _workspace_slash_reply(request, sid, "/workspace switch Target")
    assert "saved" in message and "failed" in message
    assert registry.snapshot().workspace_id_for_session(sid) == target.id
    # A concurrent definition edit must survive the repair without changing installed roots.
    changed = registry.update(
        current.id, name=current.name, primary=str(primary), folders=[str(registered), str(added)]
    )

    async with capture_event_sequence(engine.event_bus, WorkspaceUpdated) as broadcasts:
        message, notices = await _workspace_slash_reply(request, sid, "/workspace switch Current")

    assert message == "This session is already in workspace “Current”."
    assert engine.current.require_loaded() is installed
    assert engine.workspace is before
    assert workspace_acp.get(sid).cwd == before.primary_cwd
    assert [folder.path for folder in installed.runtime.working_dirs] == [str(editor)]
    assert WorkspaceRegistry(registry.path).snapshot().workspace_id_for_session(sid) == current.id
    assert registry.snapshot().workspace(current.id) == changed
    assert broadcasts == []
    assert all(notice["method"] != "_chrys/warning" for notice in notices)
    reloaded, _ = await request("session/load", **load)
    assert "result" in reloaded
    assert engine.current.require_loaded() is installed
    await request("session/close", sessionId=sid)


@pytest.mark.parametrize("repair", ["switch Unspecified", "delete Current"])
async def test_acp_repairs_saved_membership_after_switch_load_failure(
    registry, workspace_acp, tmp_path, acp_wire, monkeypatch, repair
):
    _, request = acp_wire
    (primary,) = make_workdirs(tmp_path, "primary")
    missing = str(tmp_path / "missing-reference")
    current = registry.create("Current", str(primary), [missing])
    target = registry.create("Target", str(primary))
    new, _ = await request("session/new", cwd=str(primary), mcpServers=[])
    sid = new["result"]["sessionId"]
    if repair != "switch Unspecified":
        await _workspace_slash_reply(request, sid, "/workspace switch Current")
    engine = workspace_acp.get(sid).host.engine
    installed = engine.current.require_loaded()
    with monkeypatch.context() as patch:
        patch.setattr(
            AgentLoader,
            "build",
            create_autospec(AgentLoader.build, side_effect=RuntimeError("load failed")),
        )
        message, _ = await _workspace_slash_reply(request, sid, "/workspace switch Target")
    assert "saved" in message and "failed" in message
    assert engine.current.require_loaded() is installed
    assert registry.snapshot().workspace_id_for_session(sid) == target.id

    _, notices = await _workspace_slash_reply(request, sid, f"/workspace {repair}")

    expected_id = current.id if repair == "switch Current" else None
    # Use a fresh reader to check the committed file, including short/full session identities.
    saved = WorkspaceRegistry(registry.path).snapshot()
    assert saved.workspace_id_for_session(sid) == expected_id
    assert engine.session.workspace_id == expected_id
    if repair.startswith("switch"):
        assert engine.current.require_loaded() is installed
    else:
        assert saved.workspace(current.id) is None
    assert all(notice["method"] != "_chrys/warning" for notice in notices)
    await request("session/close", sessionId=sid)


async def test_acp_workspace_in_another_folder_only_explains_how_to_open_it(
    registry, workspace_acp, tmp_path, acp_wire
):
    _, request = acp_wire
    cwd, outside = make_workdirs(tmp_path, "current", "other folder")
    current = registry.create("Here", str(cwd))
    registry.create("Over There", str(outside))
    new, _ = await request("session/new", cwd=str(cwd), mcpServers=[])
    sid = new["result"]["sessionId"]
    await _workspace_slash_reply(request, sid, "/workspace switch Here")
    session = workspace_acp.get(sid)
    engine = session.host.engine
    loaded = engine.current.require_loaded()
    registry_before = registry.path.read_bytes()
    cwd_before = session.cwd

    message, notices = await _workspace_slash_reply(request, sid, "/workspace switch over THERE")

    assert message == (
        f"The primary folder of workspace “Over There” is `{outside}`. Open this folder in your editor, "
        "start a new conversation, then enter /workspace switch Over There."
    )
    assert registry.path.read_bytes() == registry_before
    assert engine.workspace == current and session.cwd == cwd_before
    assert engine.current.require_loaded() is loaded
    assert not engine.history_messages
    assert all(notice["method"] != "_chrys/workspace_updated" for notice in notices)
    outside.rmdir()
    message, _ = await _workspace_slash_reply(request, sid, "/workspace switch Over There")
    assert message == f"The primary folder of workspace “Over There”, `{outside}`, does not exist."
    await request("session/close", sessionId=sid)


async def _workspace_slash_reply(request, sid, text):
    reply, notices = await request("session/prompt", sessionId=sid, prompt=[{"type": "text", "text": text}])
    assert reply["result"] == {"stopReason": "end_turn"}
    assert all(notice["method"] != "_chrys/turn_started" for notice in notices)
    (message,) = _acp_workspace_updates(notices, "agent_message_chunk")
    return message["content"]["text"], notices


def _acp_workspace_updates(notices, kind):
    return [
        notice["params"]["update"]
        for notice in notices
        if notice["method"] == "session/update" and notice["params"]["update"]["sessionUpdate"] == kind
    ]


def _assert_workspace_command_list(notices):
    (update,) = _acp_workspace_updates(notices, "available_commands_update")
    assert update["availableCommands"] == [
        {
            "name": "workspace",
            "description": "Switch, create, edit or delete workspaces",
            "input": {"hint": "switch | new | rename | add-folder | remove-folder | delete …"},
        },
    ]


async def test_acp_workspace_command_is_refused_during_a_live_turn(registry, connected_acp, tmp_path, monkeypatch):
    model = HeldFirstCallClient([MockResponse(text="done")])
    mock_workspace_runtime(monkeypatch, model)
    server, client = connected_acp
    sid = (await server.new_session(cwd=str(tmp_path))).session_id
    prompt = asyncio.create_task(server.prompt([acp_schema.TextContentBlock(type="text", text="hello")], sid))
    try:
        await asyncio.wait_for(model.first_call_entered.wait(), ENGINE_TURN_TIMEOUT)
        response = await asyncio.wait_for(
            server.prompt([acp_schema.TextContentBlock(type="text", text="/workspace new Busy")], sid), 2
        )
        assert response.stop_reason == "end_turn" and response.field_meta is None
        update = client.updates[-1].update
        assert isinstance(update, acp_schema.AgentMessageChunk)
        assert update.content.text == "/workspace is not available while agent is running"
    finally:
        model.release_first_call.set()
        await prompt
    assert not registry.snapshot().workspaces


async def test_workspace_acp_catalog_roots_round_trip_with_a_missing_reference(
    registry, workspace_acp, save_acp_chat, tmp_path, acp_wire
):
    _, request = acp_wire
    primary, missing, sibling = make_workdirs(tmp_path, "primary", "missing", "refs")
    physical = tmp_path / "physical"
    physical.mkdir()
    child, reference = make_workdirs(physical, "child", "refs")
    alias, primary_alias = tmp_path / "alias", tmp_path / "primary-alias"
    symlink_or_skip(alias, child, target_is_directory=True)
    symlink_or_skip(primary_alias, primary, target_is_directory=True)
    workspace = registry.create("Team", str(primary_alias), [str(missing), str(alias / ".." / "refs")])
    # POSIX follows the alias before climbing to physical/refs; Windows folds
    # `..` lexically first, which is the sibling `refs`.
    expected_reference = sibling if sys.platform == "win32" else reference
    await save_acp_chat("target", primary)
    registry.set_session_workspace("target", workspace.id)
    missing.rmdir()

    listed, _ = await request("session/list", cwd=str(primary))
    (closed,) = listed["result"]["sessions"]
    options = {key: closed[key] for key in ("sessionId", "cwd", "additionalDirectories")}
    for _ in range(2):
        loaded, _ = await request("session/load", **options, mcpServers=[])
        assert "result" in loaded, loaded

    listed, _ = await request("session/list", cwd=str(primary))
    (active,) = listed["result"]["sessions"]
    assert active["cwd"] == closed["cwd"] == str(primary.resolve())
    assert active["additionalDirectories"] == closed["additionalDirectories"] == [str(expected_reference.resolve())]
    # The explicit roots set this load's scope; a registered folder that was skipped is not reported.
    runtime = workspace_acp.get("target").host.engine.current.require_loaded().runtime
    assert runtime.missing_working_dirs == ()
    assert [d.path for d in runtime.working_dirs if not d.is_primary] == [str(expected_reference.resolve())]


async def test_workspace_acp_catalog_load_history_and_delete_on_normal_volume(
    registry, workspace_acp, save_acp_chat, tmp_path, acp_wire
):
    from chrys.kernel import Message

    _, request = acp_wire
    old, primary, reference = make_workdirs(tmp_path, "old", "primary", "reference")
    workspace = registry.create("Team", str(primary), [str(reference)])
    store = workspace_acp.state_store
    for sid in ("target", "survivor"):
        await save_acp_chat(sid, old, messages=[Message("user", [f"saved conversation {sid}"])], workdirs=[reference])
        registry.set_session_workspace(sid, workspace.id)
    target = {"sessionId": "target"}
    stale = [str(tmp_path / "stale")]
    listing, _ = await request("session/list", cwd=str(primary), additionalDirectories=stale)
    rows = listing["result"]["sessions"]
    assert {row["sessionId"] for row in rows} == {"target", "survivor"}
    assert {(row["cwd"], tuple(row["additionalDirectories"])) for row in rows} == {(str(primary), (str(reference),))}
    old_listing, _ = await request("session/list", cwd=str(old))
    assert old_listing["result"]["sessions"] == []
    for method in ("session/load", "_session/history", "_session/delete"):
        refusal, _ = await request(
            method, **target, cwd=str(old), **({"mcpServers": []} if method == "session/load" else {})
        )
        assert "belongs to a different workspace" in refusal["error"]["data"]["details"]

    listed_roots = rows[0]["additionalDirectories"]
    for options in ({}, {"additionalDirectories": listed_roots}):
        loaded, notices = await request(
            "session/load",
            **target,
            cwd=str(primary),
            mcpServers=[],
            **options,
        )
        assert "result" in loaded
        indicator = next(i for i, notice in enumerate(notices) if notice["method"] == "_chrys/session_restored")
        replay = next(i for i, notice in enumerate(notices) if "saved conversation target" in json.dumps(notice))
        assert notices[indicator]["params"] == target
        assert indicator < replay
        assert all("saved conversation survivor" not in json.dumps(notice) for notice in notices)
    refusal, _ = await request("session/load", **target, cwd=str(primary), mcpServers=[], additionalDirectories=[])
    assert "already active" in refusal["error"]["data"]["details"]

    await workspace_acp.close("target")
    loaded, _ = await request("session/load", **target, cwd=str(primary), mcpServers=[], additionalDirectories=[])
    assert "result" in loaded
    assert workspace_acp.get("target").host.engine.workspace.working_dirs == []
    assert registry.snapshot().workspace(workspace.id).working_dirs[0].path == str(reference)
    listing, _ = await request("session/list", cwd=str(primary))
    by_id = {row["sessionId"]: row for row in listing["result"]["sessions"]}
    assert not by_id["target"].get("additionalDirectories")
    assert by_id["survivor"]["additionalDirectories"] == listed_roots
    history, _ = await request("_session/history", **target, cwd=str(primary))
    assert history["result"]["sessionId"] == "target"
    assert "saved conversation target" in json.dumps(history["result"]["messages"])
    deleted, _ = await request("_session/delete", **target, cwd=str(primary))
    assert deleted["result"] == {}
    assert await store.load_session("target") is None
    assert (await store.load_session("survivor"))["messages"][0].text == "saved conversation survivor"
    assert registry.snapshot().sessions == {"survivor": workspace.id}


@pytest.mark.parametrize("registered", [False, True])
async def test_acp_uses_main_location_rules_after_moving_to_unspecified(
    workspace_acp, save_acp_chat, acp_wire, tmp_path, registered
):
    _, request = acp_wire
    first, second, extra = make_workdirs(tmp_path, "first", "second", "extra")
    store = workspace_acp.state_store
    await save_acp_chat("target", first, workdirs=[extra])
    if registered:
        registry = WorkspaceRegistry()
        workspace = registry.create("Team", str(first))
        registry.set_session_workspace("target", workspace.id)
    await workspace_acp.load_session(cwd=str(first), session_id="target", mcp_servers=None, additional_directories=[])
    await workspace_acp.set_workspace("target", str(second))

    old_list, _ = await request("session/list", cwd=str(first))
    (row,) = old_list["result"]["sessions"]
    assert row["sessionId"] == "target"
    assert Path(row["cwd"]).samefile(first)
    assert row["additionalDirectories"] == [str(extra)]
    new_list, _ = await request("session/list", cwd=str(second))
    assert new_list["result"]["sessions"] == []
    for method in ("session/load", "_session/history"):
        options = {"mcpServers": []} if method == "session/load" else {}
        refusal, _ = await request(method, cwd=str(first), sessionId="target", **options)
        assert "belongs to cwd" in refusal["error"]["data"]["details"]
        reply, _ = await request(method, cwd=str(second), sessionId="target", **options)
        assert "result" in reply
    refusal, _ = await request("_session/delete", cwd=str(second), sessionId="target")
    assert "belongs to a different workspace" in refusal["error"]["data"]["details"]
    deleted, _ = await request("_session/delete", cwd=str(first), sessionId="target")
    assert deleted["result"] == {}
    assert await store.load_session("target") is None


async def test_delete_uses_active_primary_after_workspace_alias_retargets(
    registry, workspace_acp, save_acp_chat, tmp_path
):

    first, second, alias = make_workdir_alias(tmp_path, "first", "second", "alias")
    workspace = registry.create("Team", str(alias))
    store = workspace_acp.state_store
    await save_acp_chat("target", first)
    registry.set_session_workspace("target", workspace.id)
    await workspace_acp.load_session(cwd=str(first), session_id="target", mcp_servers=None)
    retarget_workdir_alias(alias, second)

    with pytest.raises(AcpSessionError, match="belongs to cwd"):
        await workspace_acp.begin_delete_session(cwd=str(second), session_id="target")
    assert not workspace_acp.get("target").closing
    await workspace_acp.delete_session(cwd=str(first), session_id="target")
    assert await store.load_session("target") is None


async def test_acp_same_directory_keeps_definition_until_explicit_reselect(
    registry, workspace_acp, save_acp_chat, tmp_path, acp_wire
):
    from chrys.foundation.events.types import WorkspaceUpdated

    server, request = acp_wire
    primary, first, second = make_workdirs(tmp_path, "primary", "first", "second")
    for path in (first, second):
        (path / "AGENTS.md").write_text(path.name, encoding="utf-8")
    workspace = registry.create("Original", str(primary), [str(first)])
    await save_acp_chat("target", primary)
    registry.set_session_workspace("target", workspace.id)
    reply, _ = await request("session/load", sessionId="target", cwd=str(primary), mcpServers=[])
    assert "result" in reply
    host = workspace_acp.get("target").host

    async def send_workspace(event):
        await server._handle_chrys_extension_event("target", event)

    await host.event_bus.subscribe(WorkspaceUpdated, send_workspace)
    registry.update(workspace.id, name="Renamed", primary=str(primary), folders=[str(second)])
    same, notices = await request("_session/set_workspace", sessionId="target", primaryCwd=str(primary))
    expected = {"sessionId": "target", "primaryCwd": str(primary), "workingDirs": [str(first)], "referenceFiles": []}
    assert same["result"] == expected
    assert [notice["params"] for notice in notices if notice["method"] == "_chrys/workspace_updated"] == [expected]
    assert host.engine.workspace.name == "Original"
    assert [directory.path for directory in host.engine.workspace.working_dirs] == [str(first)]

    # The ACP protocol exposes set_workspace by directory. An explicit workspace
    # selection arrives through the shared engine command.
    await select_workspace(host.event_bus, workspace.id)
    assert host.engine.workspace.name == "Renamed"
    assert [directory.path for directory in host.engine.workspace.working_dirs] == [str(second)]
    reply, notices = await request("session/close", sessionId="target")
    assert "result" in reply
    assert [notice["params"] for notice in notices if notice["method"] == "_chrys/workspace_updated"] == [
        {**expected, "workingDirs": [str(second)]}
    ]


async def test_cold_load_keeps_the_explicit_roots_when_registered_references_change(
    registry, workspace_acp, save_acp_chat, tmp_path, monkeypatch
):
    # The client's explicit roots decide this load, even if a registered folder
    # reappears or its link is retargeted between validation and the build.
    primary, missing = make_workdirs(tmp_path, "primary", "missing")
    old, new, alias = make_workdir_alias(tmp_path, "old", "new", "ref")
    workspace = registry.create("Team", str(primary), [str(alias), str(missing)])
    registry.set_session_workspace("target", workspace.id)
    await save_acp_chat("target", primary)
    missing.rmdir()
    load_settings = workspace_acp._session_loaded_settings

    def change_references_before_loading_settings(cwd):
        missing.mkdir()
        retarget_workdir_alias(alias, new)
        return load_settings(cwd)

    monkeypatch.setattr(workspace_acp, "_session_loaded_settings", change_references_before_loading_settings)
    result = await workspace_acp.load_session(
        cwd=str(primary), session_id="target", mcp_servers=None, additional_directories=[str(old.resolve())]
    )
    runtime = result.session.host.engine.current.require_loaded().runtime
    assert [d.path for d in runtime.working_dirs if not d.is_primary] == [str(old.resolve())]
    assert runtime.missing_working_dirs == ()
    assert registry.snapshot().workspace(workspace.id).working_dirs == workspace.working_dirs


@pytest.mark.parametrize("use_alias", [False, True], ids=["direct", "alias"])
async def test_cold_load_only_loads_skills_from_registered_and_effective_references(
    registry, workspace_acp, save_acp_chat, tmp_path, monkeypatch, use_alias
):
    primary, registered, omitted, editor = make_workdirs(tmp_path, "primary", "registered", "omitted", "editor")
    reference = registered
    if use_alias:
        reference = tmp_path / "registered-alias"
        symlink_or_skip(reference, registered, target_is_directory=True)
    workspace = registry.create("Team", str(primary), [str(reference), str(omitted)])
    registry.set_session_workspace("target", workspace.id)
    await save_acp_chat("target", primary)
    for root, name in ((registered, "registered-skill"), (omitted, "omitted-skill"), (editor, "editor-only")):
        folder = root / ".agents" / "skills" / name
        folder.mkdir(parents=True)
        (folder / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: Test skill\n---\nUse it.\n", encoding="utf-8"
        )
    loaded = workspace_acp._loaded_settings
    loaded = replace(loaded, settings=replace(loaded.settings, workdir_skills_enabled=True))
    freeze_workspace_settings(monkeypatch, loaded)
    monkeypatch.setattr(session_manager, "load_settings", create_autospec(load_settings, return_value=loaded))
    workspace_acp._agent_registry.register(
        replace(PROFILE, skills=replace(PROFILE.skills, auto_load_cwd_agents_skills=True))
    )

    result = await workspace_acp.load_session(
        cwd=str(primary), session_id="target", mcp_servers=None, additional_directories=[str(registered), str(editor)]
    )
    engine = result.session.host.engine
    runtime = engine.current.require_loaded().runtime
    assert engine.session.workspace_id == workspace.id
    assert [directory.path for directory in runtime.working_dirs] == [str(registered.resolve()), str(editor.resolve())]
    assert {skill.name for skill in engine.runtime_details.skill_details} == {"registered-skill"}


@pytest.mark.parametrize("variant", ["retargeted-link", "hand-written"])
async def test_workspace_acp_round_trip_when_a_reference_is_the_primary(
    registry, workspace_acp, save_acp_chat, tmp_path, acp_wire, variant
):
    # A reference folder that is the primary folder is not reported, so list output loads again unchanged.
    _, request = acp_wire
    primary, reference = make_workdirs(tmp_path, "primary", "reference")
    if variant == "retargeted-link":
        alias = tmp_path / "alias"
        symlink_or_skip(alias, reference, target_is_directory=True)
        workspace = registry.create("Team", str(primary), [str(alias)])
        retarget_workdir_alias(alias, primary)
    else:
        workspace = registry.create("Team", str(primary), [str(reference)])
        data = json.loads(registry.path.read_text(encoding="utf-8"))
        for entry in data["workspaces"]:
            entry["working_dirs"] = [str(primary) if path == str(reference) else path for path in entry["working_dirs"]]
        registry.path.write_text(json.dumps(data), encoding="utf-8")
    await save_acp_chat("target", primary)
    registry.set_session_workspace("target", workspace.id)

    listed, _ = await request("session/list", cwd=str(primary))
    (closed,) = listed["result"]["sessions"]
    assert closed.get("additionalDirectories") in (None, [])
    options = {key: closed[key] for key in ("sessionId", "cwd", "additionalDirectories") if key in closed}
    for _ in range(2):
        loaded, _ = await request("session/load", **options, mcpServers=[])
        assert "result" in loaded, loaded


async def test_acp_shows_a_missing_workspace_reference_at_load(registry, save_acp_chat, connected_acp, tmp_path):
    primary, reference = make_workdirs(tmp_path, "primary", "reference")
    workspace = registry.create("Project workspace", str(primary), [str(reference)])
    await save_acp_chat("target", primary)
    registry.set_session_workspace("target", workspace.id)
    reference.rmdir()
    server, client = connected_acp

    await server.load_session("target", cwd=str(primary))

    (notice,) = [
        payload
        for method, payload in client.ext_notifications
        if method == "chrys/warning" and str(reference) in payload["message"]
    ]
    assert notice["sessionId"] == "target"
    assert workspace.name in notice["message"]
    await server.close_session("target")


async def test_acp_switch_uses_workspace_references_after_new_with_editor_roots(
    registry, workspace_acp, tmp_path, acp_wire
):
    _, request = acp_wire
    primary, editor, reference = make_workdirs(tmp_path, "project", "editor", "reference")
    team = registry.create("Team", str(primary), [str(reference)])
    new, _ = await request("session/new", cwd=str(primary), mcpServers=[], additionalDirectories=[str(editor)])
    sid = new["result"]["sessionId"]
    engine = workspace_acp.get(sid).host.engine
    assert engine.workspace.id == ""
    assert [d.path for d in engine.workspace.working_dirs if not d.is_primary] == [str(editor)]

    await _workspace_slash_reply(request, sid, "/workspace switch Team")

    assert engine.workspace == team
    await request("session/close", sessionId=sid)


async def test_acp_add_folder_reloads_definition_after_explicit_cold_load(
    registry, workspace_acp, save_acp_chat, tmp_path, acp_wire
):
    _, request = acp_wire
    primary, editor, reference, extra = make_workdirs(tmp_path, "project", "editor", "reference", "extra")
    team = registry.create("Team", str(primary), [str(reference)])
    await save_acp_chat("target", primary)
    registry.set_session_workspace("target", team.id)
    loaded, _ = await request(
        "session/load", sessionId="target", cwd=str(primary), mcpServers=[], additionalDirectories=[str(editor)]
    )
    assert "result" in loaded
    engine = workspace_acp.get("target").host.engine
    assert [d.path for d in engine.workspace.working_dirs] == [str(editor)]
    assert registry.snapshot().workspace(team.id) == team

    message, _ = await _workspace_slash_reply(request, "target", f"/workspace add-folder {extra}")

    assert message == f"Added reference folder `{extra}`."
    assert [d.path for d in engine.workspace.working_dirs] == [str(reference), str(extra)]
    assert registry.snapshot().workspace(team.id) == engine.workspace
    await request("session/close", sessionId="target")


async def test_acp_switch_to_unspecified_keeps_only_the_primary_folder(
    registry, workspace_acp, save_acp_chat, tmp_path, acp_wire
):
    _, request = acp_wire
    primary, reference, editor = make_workdirs(tmp_path, "project", "reference", "editor")
    team = registry.create("Team", str(primary), [str(reference)])
    await save_acp_chat("target", primary)
    registry.set_session_workspace("target", team.id)
    loaded, _ = await request(
        "session/load", sessionId="target", cwd=str(primary), mcpServers=[], additionalDirectories=[str(editor)]
    )
    assert "result" in loaded
    engine = workspace_acp.get("target").host.engine
    assert [d.path for d in engine.workspace.working_dirs] == [str(editor)]

    _, notices = await _workspace_slash_reply(request, "target", "/workspace switch Unspecified")

    (update,) = [notice["params"] for notice in notices if notice["method"] == "_chrys/workspace_updated"]
    assert update["primaryCwd"] == str(primary)
    assert update["workingDirs"] == []
    assert engine.workspace.id == ""
    assert engine.workspace.primary_cwd == str(primary)
    assert engine.workspace.working_dirs == []
    assert registry.snapshot().workspace(team.id) == team
    assert registry.snapshot().session_workspace("target") is None
    await request("session/close", sessionId="target")


@pytest.mark.parametrize("include_primary", [True, False], ids=["primary-and-reference", "reference-only"])
async def test_active_named_load_accepts_the_same_directories_twice(
    registry, workspace_acp, save_acp_chat, tmp_path, acp_wire, include_primary
):
    """An explicit primary is redundant: cold and repeated active loads accept either spelling."""
    _, request = acp_wire
    primary, reference = make_workdirs(tmp_path, "project", "reference")
    team = registry.create("Team", str(primary), [str(reference)])
    await save_acp_chat("target", primary)
    registry.set_session_workspace("target", team.id)
    roots = [str(primary), str(reference)] if include_primary else [str(reference)]
    options = {"sessionId": "target", "cwd": str(primary), "mcpServers": [], "additionalDirectories": roots}
    loaded, _ = await request("session/load", **options)
    assert "result" in loaded, loaded
    active = workspace_acp.get("target")
    for _ in range(2):
        reopened, _ = await request("session/load", **options)
        assert "result" in reopened, reopened
        assert workspace_acp.get("target") is active
    assert [folder.path for folder in active.host.engine.workspace.working_dirs if not folder.is_primary] == [
        str(reference)
    ]
    await request("session/close", sessionId="target")


async def test_acp_workspace_join_warns_once_for_unavailable_reference_folders(registry, tmp_path, acp_wire):
    _, request = acp_wire
    primary, first, second = make_workdirs(tmp_path, "primary", "first", "second")
    registry.create("Team", str(primary), [str(first), str(second)])
    first.rmdir()
    second.rmdir()
    second.touch()
    new, _ = await request("session/new", cwd=str(primary), mcpServers=[])
    sid = new["result"]["sessionId"]
    _, notices = await _workspace_slash_reply(request, sid, "/workspace switch Team")
    (warning,) = [notice["params"] for notice in notices if notice["method"] == "_chrys/warning"]
    assert warning == {
        "code": "workspace_reference_missing",
        "sessionId": sid,
        "message": f"Workspace “Team” has 2 missing reference folders: {first}, {second}",
    }
    prompt, _ = await request("session/prompt", sessionId=sid, prompt=[{"type": "text", "text": "hello"}])
    assert prompt["result"]["stopReason"] == "end_turn"
    _, notices = await _workspace_slash_reply(request, sid, "/workspace rename Renamed")
    assert not [notice for notice in notices if notice["method"] == "_chrys/warning"]
    await request("session/close", sessionId=sid)
    loaded, notices = await request("session/load", sessionId=sid, cwd=str(primary), mcpServers=[])
    assert "result" in loaded
    (warning,) = [notice["params"] for notice in notices if notice["method"] == "_chrys/warning"]
    assert warning == {
        "code": "workspace_reference_missing",
        "sessionId": sid,
        "message": f"Workspace “Renamed” has 2 missing reference folders: {first}, {second}",
    }
    await request("session/close", sessionId=sid)


async def test_reopening_a_session_uses_the_rebuilt_scope_without_blocking_other_sessions(
    workspace_acp, connected_acp, registry, tmp_path, monkeypatch
):
    server, _ = connected_acp
    primary, reference, extra, other = make_workdirs(tmp_path, "project", "reference", "extra", "other")
    workspace = registry.create("Team", str(primary), [str(reference)])
    session = await workspace_acp.new_session(cwd=str(primary), mcp_servers=None)
    engine = session.host.engine
    await select_workspace(session.host.event_bus, workspace.id)
    assert engine.workspace == workspace

    boundary = WorkspaceBoundary()
    before_await(monkeypatch, engine.loader, "build", boundary.wait)
    prompt = [acp_schema.TextContentBlock(type="text", text=f"/workspace add-folder {extra}")]
    async with running_task(server.prompt(prompt, session.session_id)) as rebuild:
        try:
            await wait_for_started(boundary.entered, rebuild, description="workspace rebuild enters runtime build")
            async with (
                asyncio.timeout(ENGINE_TURN_TIMEOUT),
                running_task(
                    workspace_acp.load_session(
                        cwd=str(primary),
                        session_id=session.session_id,
                        additional_directories=[str(reference)],
                        mcp_servers=None,
                    )
                ) as reload,
            ):
                companion = await workspace_acp.new_session(cwd=str(other), mcp_servers=None)
                await workspace_acp.close(companion.session_id)
                assert not reload.done()
                assert engine.workspace == workspace
                boundary.open()
                assert (await rebuild).stop_reason == "end_turn"
                with pytest.raises(AcpSessionError, match="Close it before reloading with additional directories"):
                    await reload
        finally:
            boundary.open()
    assert [folder.path for folder in engine.workspace.working_dirs] == [str(reference), str(extra)]


@pytest.mark.parametrize("registered", [True, False], ids=["registered", "unspecified"])
async def test_acp_workspace_cwd_change_rebuilds_as_unspecified(
    registry, workspace_acp, tmp_path, acp_wire, registered
):
    _, request = acp_wire
    old, new, editor, registered_ref = make_workdirs(tmp_path, "old", "new", "editor", "ref")
    registry.create("Old", str(old))
    registry.create("New", str(new), [str(registered_ref)])
    result, _ = await request("session/new", cwd=str(old), mcpServers=[], additionalDirectories=[str(editor)])
    sid = result["result"]["sessionId"]
    session = workspace_acp.get(sid)
    if registered:
        await _workspace_slash_reply(request, sid, "/workspace switch Old")

    same, _ = await request("_session/set_workspace", sessionId=sid, primaryCwd=str(old))
    assert "result" in same
    assert session.host.engine.workspace.working_dirs == []
    moved, _ = await request("_session/set_workspace", sessionId=sid, primaryCwd=str(new))
    assert "result" in moved
    assert session.host.engine.workspace.id == ""
    assert session.host.engine.workspace.working_dirs == []
    await _workspace_slash_reply(request, sid, "/workspace switch New")
    assert [d.path for d in session.host.engine.workspace.working_dirs] == [str(registered_ref)]


async def test_stop_while_a_workspace_command_waits_drops_it(registry, workspace_acp, connected_acp, tmp_path):
    server, client = connected_acp
    (primary,) = make_workdirs(tmp_path, "primary")
    workspace = registry.create("Target", str(primary))
    session = await workspace_acp.new_session(cwd=str(primary), mcp_servers=None)
    prompt = [acp_schema.TextContentBlock(type="text", text="/workspace delete Target")]
    async with session.mutation_lock:
        pending = asyncio.create_task(server.prompt(prompt, session.session_id))
        await wait_for(lambda: session.turns_reserved, timeout=ENGINE_TURN_TIMEOUT)
        await server.cancel(session.session_id)
    async with asyncio.timeout(ENGINE_TURN_TIMEOUT):
        response = await pending
    assert response.stop_reason == "cancelled"
    assert registry.snapshot().workspace(workspace.id) == workspace
    assert not [update for update in client.updates if update.session_id == session.session_id]


async def test_stop_during_workspace_switch_read_drops_the_change(
    registry, workspace_acp, connected_acp, tmp_path, monkeypatch
):
    server, client = connected_acp
    (primary,) = make_workdirs(tmp_path, "primary")
    workspace = registry.create("Target", str(primary))
    session = await workspace_acp.new_session(cwd=str(primary), mcp_servers=None)
    before = registry.path.read_bytes()
    select = session_manager.select_workspace_for_acp
    entered, release = Event(), Event()

    def held_select(registry, selector, *, current, cwd, session_id, has_messages):
        entered.set()
        if not release.wait(ENGINE_TURN_TIMEOUT):
            raise TimeoutError("test did not release the workspace lookup")
        return select(registry, selector, current=current, cwd=cwd, session_id=session_id, has_messages=has_messages)

    monkeypatch.setattr(session_manager, "select_workspace_for_acp", create_autospec(select, side_effect=held_select))
    prompt = [acp_schema.TextContentBlock(type="text", text="/workspace switch Target")]
    async with running_task(server.prompt(prompt, session.session_id)) as pending:
        try:
            await wait_for(lambda: entered.is_set() or pending.done(), timeout=ENGINE_TURN_TIMEOUT)
            if pending.done():
                await pending
            assert entered.is_set()
            await server.cancel(session.session_id)
        finally:
            release.set()
        async with asyncio.timeout(ENGINE_TURN_TIMEOUT):
            response = await pending

    assert response.stop_reason == "cancelled"
    assert session.host.engine.session.workspace_id is None
    assert registry.path.read_bytes() == before
    assert registry.snapshot().workspace(workspace.id) == workspace
    assert registry.snapshot().workspace_id_for_session(session.session_id) is None
    assert not [update for update in client.updates if update.session_id == session.session_id]


async def test_stop_and_close_do_not_wait_for_a_switch_read_that_never_answers(
    registry, workspace_acp, connected_acp, tmp_path, monkeypatch
):
    server, _client = connected_acp
    (primary,) = make_workdirs(tmp_path, "primary")
    registry.create("Target", str(primary))
    session = await workspace_acp.new_session(cwd=str(primary), mcp_servers=None)
    select = session_manager.select_workspace_for_acp
    entered, release = Event(), Event()

    def hung_select(registry, selector, *, current, cwd, session_id, has_messages):
        entered.set()
        release.wait(ENGINE_TURN_TIMEOUT)
        raise TimeoutError("the folder never answered")

    monkeypatch.setattr(session_manager, "select_workspace_for_acp", create_autospec(select, side_effect=hung_select))
    prompt = [acp_schema.TextContentBlock(type="text", text="/workspace switch Target")]
    try:
        async with running_task(server.prompt(prompt, session.session_id)) as pending:
            await wait_for(lambda: entered.is_set() or pending.done(), timeout=ENGINE_TURN_TIMEOUT)
            assert entered.is_set()
            await server.cancel(session.session_id)
            # The read is still hung: Stop answers without it, and closing the session does not queue behind it.
            async with asyncio.timeout(2):
                response = await pending
                await server.close_session(session.session_id)
        assert response.stop_reason == "cancelled"
        assert session.host.engine.session.workspace_id is None
    finally:
        release.set()


async def test_stop_and_close_do_not_wait_for_a_switch_planning_a_folder_that_never_answers(
    registry, workspace_acp, connected_acp, tmp_path, monkeypatch
):
    server, client = connected_acp
    primary, reference = make_workdirs(tmp_path, "primary", "slow-mount")
    target = registry.create("Target", str(primary), [str(reference)])
    session = await workspace_acp.new_session(cwd=str(primary), mcp_servers=None)
    before = registry.snapshot()
    entered, release = Event(), Event()
    resolve = engine_workspaces.resolve_workspace_directory

    def hung_resolve(path):
        if path == str(reference):
            entered.set()
            release.wait(ENGINE_TURN_TIMEOUT)
        return resolve(path)

    monkeypatch.setattr(engine_workspaces, "resolve_workspace_directory", hung_resolve)
    prompt = [acp_schema.TextContentBlock(type="text", text="/workspace switch Target")]
    try:
        async with running_task(server.prompt(prompt, session.session_id)) as pending:
            await wait_for(lambda: entered.is_set() or pending.done(), timeout=ENGINE_TURN_TIMEOUT)
            assert entered.is_set()
            await server.cancel(session.session_id)
            # The engine is still waiting on the folder: Stop ends the switch before it writes anything.
            async with asyncio.timeout(2):
                response = await pending
            assert response.stop_reason == "cancelled"
            assert registry.snapshot().workspace_id_for_session(session.session_id) is None
            assert registry.snapshot().last_used_at == before.last_used_at
            assert session.host.engine.session.workspace_id is None
            assert not [
                item
                for item in client.updates
                if item.session_id == session.session_id and isinstance(item.update, acp_schema.AgentMessageChunk)
            ]
            async with asyncio.timeout(2):
                await server.close_session(session.session_id)
    finally:
        release.set()
    assert registry.snapshot().workspace(target.id) == target


async def test_stop_while_a_switch_waits_for_the_registry_lock_writes_nothing(
    registry, workspace_acp, connected_acp, tmp_path, monkeypatch
):
    server, _client = connected_acp
    primary, reference = make_workdirs(tmp_path, "primary", "reference")
    target = registry.create("Target", str(primary), [str(reference)])
    session = await workspace_acp.new_session(cwd=str(primary), mcp_servers=None)
    busy = Event()
    save = engine_workspaces.MembershipWrite.save

    def noted_save(self, session_id: str | None, *, try_lock: bool = False, folders_checked: bool = False) -> None:
        try:
            save(self, session_id, try_lock=try_lock, folders_checked=folders_checked)
        except registry_module._RegistryLockBusy:
            busy.set()
            raise

    monkeypatch.setattr(engine_workspaces.MembershipWrite, "save", noted_save)
    prompt = [acp_schema.TextContentBlock(type="text", text="/workspace switch Target")]
    # Another window holds the list: the switch retries the lock before its first write.
    with FileLock(registry._lock_path, timeout=0):
        async with running_task(server.prompt(prompt, session.session_id)) as pending:
            await wait_for(lambda: busy.is_set() or pending.done(), timeout=ENGINE_TURN_TIMEOUT)
            assert busy.is_set()
            await server.cancel(session.session_id)
            async with asyncio.timeout(2):
                response = await pending
            assert response.stop_reason == "cancelled"
    assert registry.snapshot().workspace_id_for_session(session.session_id) is None
    assert session.host.engine.session.workspace_id is None
    assert registry.snapshot().workspace(target.id) == target


async def test_stop_during_workspace_delete_keeps_the_change_and_reply(
    registry, workspace_acp, connected_acp, tmp_path, monkeypatch
):
    server, client = connected_acp
    (primary,) = make_workdirs(tmp_path, "primary")
    workspace = registry.create("Target", str(primary))
    session = await workspace_acp.new_session(cwd=str(primary), mcp_servers=None)
    manage = session_manager.manage_workspace
    entered, release = Event(), Event()

    def held_manage(registry, command, *, current, cwd, session_id):
        result = manage(registry, command, current=current, cwd=cwd, session_id=session_id)
        entered.set()
        if not release.wait(ENGINE_TURN_TIMEOUT):
            raise TimeoutError("test did not release the workspace command")
        return result

    monkeypatch.setattr(session_manager, "manage_workspace", create_autospec(manage, side_effect=held_manage))
    prompt = [acp_schema.TextContentBlock(type="text", text="/workspace delete Target")]
    async with running_task(server.prompt(prompt, session.session_id)) as pending:
        try:
            await wait_for(lambda: entered.is_set() or pending.done(), timeout=ENGINE_TURN_TIMEOUT)
            if pending.done():
                await pending
            assert entered.is_set()
            await server.cancel(session.session_id)
        finally:
            release.set()
        async with asyncio.timeout(ENGINE_TURN_TIMEOUT):
            response = await pending

    assert response.stop_reason == "end_turn"
    assert registry.snapshot().workspace(workspace.id) is None
    (reply,) = [
        item.update
        for item in client.updates
        if item.session_id == session.session_id and isinstance(item.update, acp_schema.AgentMessageChunk)
    ]
    assert reply.content.text == "Deleted workspace “Target”."


async def test_acp_set_workspace_reports_active_named_scope(acp_wire, registry, workspace_acp, save_acp_chat, tmp_path):
    _, request = acp_wire
    primary, reference = make_workdirs(tmp_path, "project", "editor")
    team = registry.create("Team", str(primary))
    sid = "target"
    await save_acp_chat(sid, primary)
    registry.set_session_workspace(sid, team.id)
    loaded, _ = await request(
        "session/load", sessionId=sid, cwd=str(primary), mcpServers=[], additionalDirectories=[str(reference)]
    )
    assert "result" in loaded
    answered, _ = await request("session/prompt", sessionId=sid, prompt=[{"type": "text", "text": "First turn"}])
    assert answered["result"]["stopReason"] == "end_turn"
    updated, _ = await request("_session/set_workspace", sessionId=sid, primaryCwd=str(primary))
    assert updated["result"]["workingDirs"] == [str(reference)]
    workspace = workspace_acp.get(sid).host.engine.workspace
    assert [folder.path for folder in workspace.working_dirs if not folder.is_primary] == [str(reference)]
    listed, _ = await request("session/list", cwd=str(primary), additionalDirectories=[str(reference)])
    (row,) = listed["result"]["sessions"]
    assert row["sessionId"] == sid
    assert row["additionalDirectories"] == updated["result"]["workingDirs"]
    await request("session/close", sessionId=sid)


async def test_unspecified_acp_editor_dirs_survive_two_cold_reconnects(acp_wire, workspace_acp, tmp_path):
    _, request = acp_wire
    primary, reference = make_workdirs(tmp_path, "project", "editor")
    options = {"cwd": str(primary), "mcpServers": []}
    new, _ = await request("session/new", **options, additionalDirectories=[str(reference)])
    sid = new["result"]["sessionId"]
    for prompt in ("First turn", "After reconnect"):
        answered, _ = await request("session/prompt", sessionId=sid, prompt=[{"type": "text", "text": prompt}])
        assert answered["result"]["stopReason"] == "end_turn"
        closed, _ = await request("session/close", sessionId=sid)
        assert "result" in closed
        loaded, _ = await request("session/load", **options, sessionId=sid)
        assert "result" in loaded
        workspace = workspace_acp.get(sid).host.engine.workspace
        assert workspace.id == ""
        assert [folder.path for folder in workspace.working_dirs if not folder.is_primary] == [str(reference)]
    await request("session/close", sessionId=sid)
    listed, _ = await request("session/list", cwd=str(primary), additionalDirectories=[str(reference)])
    assert [(row["sessionId"], row["additionalDirectories"]) for row in listed["result"]["sessions"]] == [
        (sid, [str(reference)])
    ]


async def test_a_created_workspace_that_cannot_be_entered_says_it_was_saved(
    acp_wire, workspace_acp, registry, tmp_path, monkeypatch
):
    from chrys.foundation.events.types import Error

    _, request = acp_wire
    (cwd,) = make_workdirs(tmp_path, "project")
    new, _ = await request("session/new", cwd=str(cwd), mcpServers=[])
    sid = new["result"]["sessionId"]

    async def fail_to_enter(*_args, **_kwargs):
        raise session_manager._WorkspaceChangeError(Error(code="workspace_update_failed", message="Rebuild failed."))

    monkeypatch.setattr(workspace_acp, "_set_workspace_locked", fail_to_enter)
    message, _ = await _workspace_slash_reply(request, sid, "/workspace new Team")
    assert message == "Created workspace “Team”.\n\nWorkspace update failed."
    assert [workspace.name for workspace in registry.snapshot().workspaces] == ["Team"]
    await request("session/close", sessionId=sid)
