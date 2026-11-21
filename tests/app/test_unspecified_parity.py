# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unspecified chat opens with the same directory rules on TUI, run and ACP."""

from __future__ import annotations

import asyncio
import json
from contextlib import AsyncExitStack
from dataclasses import replace

import pytest

from chrys.app.acp.server import ChrysAcpServer
from chrys.app.acp.session_manager import AcpSessionError
from chrys.app.cli import headless, run
from chrys.foundation.config.settings_store import LoadedSettings
from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import SessionReady, SessionRestored
from chrys.foundation.i18n import Localizer
from chrys.kernel import Message
from chrys.orchestration.engine import session_lifecycle
from chrys.orchestration.session_host import HeadlessRunError
from chrys.service.approval.policy import ApprovalMode
from chrys.service.llm.mock import MockChatClient, MockResponse
from chrys.service.session.persistence import open_active_session_store
from chrys.service.state.locks import ActiveSessionGuard
from tests.app.acp._server_fakes import _FakeClient
from tests.orchestration import _session_host_helpers as helpers
from tests.support.tui_app_harness import make_chrys_app
from tests.support.waiting import wait_for
from tests.support.workspace_engine import patch_headless_host, running_acp_manager
from tests.support.workspace_registry import corrupt_workspace_registry, make_workdirs, save_chat_session

SAVED = "saved-session"
UNREADABLE = "workspace_registry_unreadable"


@pytest.fixture
def unspecified(monkeypatch, tmp_path):
    cwd, extra, other = make_workdirs(tmp_path, "primary", "extra", "other")
    monkeypatch.chdir(cwd)
    store = open_active_session_store()
    settings, models = helpers._make_settings_and_models()
    loaded = LoadedSettings(settings=replace(settings, locale="en"), provenance={})
    profiles = helpers._make_agent_registry(helpers._make_profile())
    helpers._patch_runtime(monkeypatch, [MockChatClient(responses=[MockResponse(text="done")]) for _ in range(2)], [])
    monkeypatch.setattr(session_lifecycle, "load_settings", lambda **_: loaded)
    return cwd, extra, other, store, loaded, profiles, models


@pytest.fixture
def open_events(monkeypatch):
    events = []
    publish = EventBus.publish

    async def record(bus, event, **kwargs):
        if isinstance(event, SessionReady | SessionRestored):
            events.append(event)
        return await publish(bus, event, **kwargs)

    monkeypatch.setattr(EventBus, "publish", record)
    return events


async def _save(store, cwd, extra):
    await save_chat_session(
        store, SAVED, cwd, profile="Headless", messages=[Message("user", ["previous conversation"])], workdirs=[extra]
    )


def _assert_open(events, session_id, cwd, dirs, *, restore):
    assert any(isinstance(event, SessionReady) for event in events)
    assert any(isinstance(event, SessionRestored) for event in events) == restore
    assert {(event.session_id, event.primary_cwd, tuple(event.working_dirs)) for event in events} == {
        (session_id, str(cwd), tuple(map(str, dirs)))
    }
    assert all(event.workspace is None for event in events)


@pytest.fixture
async def acp_manager(unspecified):
    _, _, _, store, loaded, profiles, models = unspecified
    async with running_acp_manager(
        loaded, profiles, models, store, profile_name="Headless", approval_mode=ApprovalMode.MANUAL
    ) as manager:
        yield manager


@pytest.mark.parametrize(
    ("surface", "entry"),
    [
        *(
            pytest.param(surface, entry, id=f"{surface}-{entry}")
            for surface in ("run", "tui", "acp")
            for entry in ("new-in-workspace-folder", "new-with-corrupt-registry", "restore")
        ),
        pytest.param("run", "other-cwd", id="run-other-cwd"),
    ],
)
async def test_unspecified_session_parity(
    agent_engine, monkeypatch, capsys, tmp_path, unspecified, open_events, surface, entry
):
    cwd, extra, other, store, loaded, profiles, models = unspecified
    restore = entry in {"restore", "other-cwd"}
    registry = store.workspace_registry
    registry.create("Team", str(cwd))
    if entry in {"restore", "new-with-corrupt-registry"}:
        corrupt_workspace_registry(registry)
    if restore:
        await _save(store, cwd, extra)
    dirs = [extra] if entry == "restore" else []
    warnings = [UNREADABLE] if entry == "restore" else []
    async with AsyncExitStack() as stack:
        if surface == "run":
            args = ["hello", "-a", "Headless", "--json"]
            if restore:
                args.extend(["-s", SAVED])
            if entry == "other-cwd":
                cwd = other
                args.extend(["-C", str(cwd)])
            patch_headless_host(monkeypatch, profiles, models, store)
            monkeypatch.setattr(
                headless, "prepare_runtime", lambda **_: headless.PreparedRuntime(loaded, Localizer("en"), [])
            )
            assert await asyncio.to_thread(run.main, args) == 0
            stdout, stderr = capsys.readouterr()
            result = json.loads(stdout)
            assert result["result"] == "done"
            session_id = result["session_id"]
            assert [json.loads(line)["code"] for line in stderr.splitlines()] == warnings
            meta = json.loads((store.session_dir(session_id) / "session.json").read_bytes())["meta"]
            assert (meta["primary_cwd"], meta["working_dirs"]) == (str(cwd), list(map(str, dirs)))
        elif surface == "tui":
            engine = agent_engine(
                EventBus(), loaded_settings=loaded, state_store=store, agent_registry=profiles, model_registry=models
            )
            app = make_chrys_app(
                tmp_path / "sessions",
                settings=loaded.settings,
                engine=engine,
                event_bus=engine.event_bus,
                state_store=store,
                agent_registry=profiles,
                model_registry=models,
                profile_name="Headless",
                startup_session_id=SAVED if restore else "",
            )
            pilot = await stack.enter_async_context(app.run_test())
            await wait_for(
                lambda: app._startup_task is not None and app._startup_task.done(),
                pilot=pilot,
                description="TUI startup finished",
            )
            notices = [str(notice.message) for notice in app._notifications]
            assert sum("workspace list" in notice.lower() for notice in notices) == int(restore)
            session_id = engine.session_id
        else:
            manager = await stack.enter_async_context(
                running_acp_manager(
                    loaded, profiles, models, store, profile_name="Headless", approval_mode=ApprovalMode.MANUAL
                )
            )
            server = ChrysAcpServer(manager, initial_vision=False)
            client = _FakeClient()
            server.on_connect(client)
            if restore:
                await server.load_session(SAVED, cwd=str(cwd))
                session_id = SAVED
            else:
                reply = await server.new_session(cwd=str(cwd), additional_directories=[str(extra)])
                session_id, dirs = reply.session_id, [cwd, extra]
            assert [
                payload["code"] for method, payload in client.ext_notifications if method == "chrys/warning"
            ] == warnings
        if restore and surface != "acp":
            assert session_id == SAVED
        _assert_open(open_events, session_id, cwd, dirs, restore=restore)
        assert not registry.snapshot().sessions
        if surface == "acp":
            with pytest.raises(AcpSessionError, match="additional directories"):
                await manager.load_session(
                    cwd=str(cwd), session_id=session_id, additional_directories=[], mcp_servers=None
                )
            await server.close_session(session_id)


async def test_unspecified_acp_reports_wrong_cwd_before_active_lock(acp_manager, unspecified, monkeypatch):
    cwd, extra, other, store, *_ = unspecified
    await _save(store, cwd, extra)
    holder = ActiveSessionGuard(store)
    assert holder.ensure(SAVED)
    acquire = ActiveSessionGuard.acquire_for_restore
    monkeypatch.setattr(
        ActiveSessionGuard, "acquire_for_restore", lambda guard, target, **_: acquire(guard, target, timeout=0)
    )
    try:
        with pytest.raises(AcpSessionError, match="belongs to a different workspace"):
            await acp_manager.load_session(cwd=str(other), session_id=SAVED, mcp_servers=None)
        with pytest.raises(HeadlessRunError) as failure:
            await acp_manager.load_session(cwd=str(cwd), session_id=SAVED, mcp_servers=None)
        assert failure.value.event.code == "session_in_use"
    finally:
        holder.release()
