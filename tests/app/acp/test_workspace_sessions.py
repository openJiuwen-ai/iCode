# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""ACP workspace behavior follows registry membership while retaining its existing workspace contract."""

from __future__ import annotations

import json
import sys
from collections.abc import AsyncIterator
from dataclasses import replace
from functools import partial
from pathlib import Path
from unittest.mock import create_autospec

import pytest
from acp import RequestError

from chrys.app.acp import session_manager
from chrys.app.acp.server import ChrysAcpServer
from chrys.app.acp.session_manager import AcpSessionError, AcpSessionManager
from chrys.orchestration.engine.loader import AgentLoader
from chrys.service.approval.policy import ApprovalMode
from chrys.service.state.store import JsonFileStateStore
from chrys.service.workspaces.registry import WorkspaceRegistry
from tests.app.acp._server_fakes import _FakeClient
from tests.support.rollback_spec import PROFILE
from tests.support.symlinks import symlink_or_skip
from tests.support.workspace_engine import (
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

        with pytest.raises(AcpSessionError, match="no longer exists"):
            await workspace_acp.load_session(cwd=str(tmp_path), session_id="target", mcp_servers=None)
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
