# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""``settings/reload`` with ``scope: "full"``, from the real ACP server through real hosts and MCP processes."""

from __future__ import annotations

import asyncio
import subprocess
import sys
import threading
from collections.abc import AsyncIterator, Awaitable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any

import psutil
import pytest
from acp import RequestError
from acp import schema as acp_schema
from acp.helpers import text_block

import chrys.app.acp.session_manager as session_manager_module
import chrys.orchestration.engine.build.builder as builder_module
from chrys.app.acp.server import ChrysAcpServer
from chrys.app.acp.session_manager import AcpSessionManager, ManagedSession
from chrys.foundation.config.runtime_pointer import (
    MODEL_POINTER_ENV,
    MODEL_POINTER_KEY,
    get_model_pointer,
    set_model_pointer,
)
from chrys.foundation.config.settings_store import persist
from chrys.foundation.config.spec import SettingOrigin, Source
from chrys.foundation.config.user_settings import user_settings_path
from chrys.foundation.events import types as ev
from chrys.orchestration.startup import bootstrap_runtime
from chrys.service.approval.policy import ApprovalMode
from chrys.service.llm.mock import MockResponse
from chrys.service.profiles.agents.registry import AgentProfileRegistry
from chrys.service.profiles.agents.schema import ApprovalConfig, MCPServerConfig
from chrys.service.profiles.agents.serializer import save_profile as save_agent
from chrys.service.profiles.models.registry import ModelProfileRegistry
from chrys.service.profiles.models.serializer import save_profile as save_model
from tests.app.acp._server_fakes import _FakeClient
from tests.app.tui.screens.test_prompt_shortcut_send import Builds, Hold, ScriptedModel, make_agent, mock_model
from tests.app.tui.screens.test_reload_live_settings import edit_key
from tests.app.tui.screens.test_reload_mcp import PROBE_SERVER, calls, flag, starts, state_dir, stdio_server
from tests.support.waiting import ENGINE_TURN_TIMEOUT, wait_for, wait_until

AGENT = "Probe"
A, B, C = "model-a", "model-b", "model-c"


@dataclass
class Acp:
    """The real server, manager and hosts of one ACP process, on real settings and profile files."""

    server: ChrysAcpServer
    manager: AcpSessionManager
    builds: Builds
    model: ScriptedModel
    settings: Path
    home: Path

    async def open(self, name: str) -> ManagedSession:
        cwd = self.home / name
        cwd.mkdir(exist_ok=True)
        response = await self.server.new_session(cwd=str(cwd), mcp_servers=[])
        return self.manager.get(response.session_id)

    def reload(self, session: ManagedSession, **params: Any) -> Awaitable[dict[str, Any]]:
        return self.server.ext_method("settings/reload", {"sessionId": session.session_id, **params})

    def full(self, session: ManagedSession) -> Awaitable[dict[str, Any]]:
        return self.reload(session, scope="full")

    def set_option(self, session: ManagedSession, key: str, value: Any) -> Awaitable[dict[str, Any]]:
        return self.server.ext_method(
            "session/set_config_option", {"sessionId": session.session_id, "key": key, "value": value}
        )

    async def use_tool(self, session: ManagedSession, label: str) -> str:
        """A real call through the session's tool loop; the MCP process it reached, by pid."""
        self.model.scripted = [
            MockResponse(tool_calls=[("probe", f"call-{label}", {"label": label})]),
            MockResponse(text="Done."),
        ]
        await asyncio.wait_for(
            self.server.prompt([text_block(f"call {label}")], session_id=session.session_id),
            ENGINE_TURN_TIMEOUT,
        )
        return label


def default_model(session: ManagedSession) -> str:
    return session.host.engine.settings_handle.base.settings.model_profile


def model_in_use(session: ManagedSession) -> str | None:
    active = session.host.engine.current.manifest.active_profile
    return active.id if active is not None else None


def model_source(session: ManagedSession) -> Source:
    return session.host.engine.loaded_settings.source_for(MODEL_POINTER_KEY).layer


@pytest.fixture
def state(tmp_path: Path) -> Path:
    return state_dir(tmp_path, "mcp")


@pytest.fixture
def servers(state: Path) -> list[MCPServerConfig]:
    return [stdio_server(state)]


@pytest.fixture
async def acp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, servers: list[MCPServerConfig]) -> AsyncIterator[Acp]:
    # Registered with monkeypatch even when absent, so bootstrap's pointer does not outlive the test.
    monkeypatch.setenv(MODEL_POINTER_ENV, "")
    monkeypatch.delenv(MODEL_POINTER_ENV)
    for model in (A, B, C):
        save_model(mock_model(model, model.upper()))
    agent = make_agent(AGENT)
    agent.approval = ApprovalConfig(default="auto", overrides={})
    agent.tools.mcp = list(servers)
    save_agent(agent)
    assert persist({MODEL_POINTER_KEY: A}).ok
    # As the server starts: this seeds the pointer from the file, credited to the file.
    loaded = bootstrap_runtime(apply_patches=False, setup_telemetry=False).loaded
    assert get_model_pointer()[0] == A
    model = ScriptedModel()
    builds = Builds(model)
    monkeypatch.setattr(builder_module, "create_client", builds.create_client)
    agents = AgentProfileRegistry()
    agents.load_all()
    models = ModelProfileRegistry()
    models.load_all()
    manager = AcpSessionManager(
        loaded_settings=loaded.overlay(Source.CLI, ask_user_timeout_seconds=None),
        profile_name=AGENT,
        approval_mode=ApprovalMode.BYPASS,
        process_cwd=str(tmp_path),
        agent_registry=agents,
        model_registry=models,
    )
    server = ChrysAcpServer(manager, initial_vision=False)
    server.on_connect(_FakeClient())
    try:
        yield Acp(server, manager, builds, model, user_settings_path(), tmp_path)
    finally:
        await manager.shutdown()


async def test_only_an_explicit_full_scope_reconnects_and_an_unknown_one_is_refused(acp: Acp, state: Path) -> None:
    session = await acp.open("one")
    assert len(starts(state)) == 1

    assert await acp.reload(session) == {}
    assert await acp.reload(session, scope=None) == {}
    assert (await acp.set_option(session, "rollback_snapshots_keep", "7"))["key"] == "rollback_snapshots_keep"
    assert len(starts(state)) == 1

    for scope in ("everything", "", "FULL", 1, ["full"]):
        with pytest.raises(RequestError):
            await acp.reload(session, scope=scope)
    assert len(starts(state)) == 1

    reply = await acp.full(session)
    assert len(starts(state)) == 2
    assert (reply["scope"], reply["installed"], reply["complete"]) == ("full", True, True)
    assert "detail" not in reply
    done = next(line for line in reply["report"] if line["key"] == "controls.reload_done")
    assert done["message"] == "Reloaded settings, profiles, memory, skills, hooks and MCP servers."

    await acp.use_tool(session, "after-full")
    assert calls(state)[-1] == f"{starts(state)[1]} after-full"


async def test_a_new_definition_reaches_the_session_that_reloaded_and_is_reported_in_english(
    acp: Acp, state: Path
) -> None:
    session = await acp.open("one")
    flag(state, "extra_parameter")

    reply = await acp.full(session)

    assert all(line["message"].isascii() for line in reply["report"])
    await acp.use_tool(session, "new-schema")
    _messages, options = acp.model.call_history[-2]
    (probe,) = [tool for tool in options["tools"] if tool.name == "probe"]
    assert "extra" in probe.parameters()["properties"]


async def test_the_report_does_not_claim_which_settings_took_effect(acp: Acp) -> None:
    session = await acp.open("one")
    edit_key(acp.settings, "ui.theme", "nord")
    edit_key(acp.settings, "tools.ask_user.inline", False)
    edit_key(acp.settings, "context.warn_threshold_pct", 50)
    edit_key(acp.settings, "ui.chat.image_graphics", False)

    lines = {line["key"]: line["message"] for line in (await acp.full(session))["report"]}

    assert "controls.reload_done" in lines
    assert "controls.reload_applied" not in lines
    assert lines["controls.settings_restart_required"] == "Not applied by this reload: ui.chat.image_graphics"


async def test_the_agent_a_full_reload_fell_back_to_is_the_one_the_client_is_told(acp: Acp) -> None:
    session = await acp.open("one")
    agent_file = acp.settings.parent / "agents" / f"{AGENT}.yaml"
    saved = agent_file.read_bytes()
    agent_file.unlink()

    await acp.full(session)
    payload = acp.server._runtime_payload(session.session_id)
    assert payload["agentProfile"] == session.host.engine.current_profile_snapshot().to_profile != AGENT

    agent_file.write_bytes(saved)
    await acp.full(session)
    await acp.manager.switch_agent(session.session_id, AGENT)
    assert session.host.engine.current_profile_snapshot().to_profile == AGENT


async def test_the_default_agent_and_approval_mode_are_reported_as_what_this_server_does_with_them(
    acp: Acp,
) -> None:
    session = await acp.open("one")
    edit_key(acp.settings, "approval.default_mode", "auto")

    lines = {line["key"]: line["message"] for line in (await acp.full(session))["report"]}

    assert lines["controls.reload_next_launch"] == (
        "Not used for new sessions, which start with the agent and approval mode this server was launched with: "
        "approval.default_mode"
    )
    assert session.host.engine.approval_mode is ApprovalMode.BYPASS


async def test_the_model_a_full_reload_selects_stays_with_that_session_through_every_later_reload(
    acp: Acp, state: Path
) -> None:
    session = await acp.open("adopter")
    other = await acp.open("other")
    await acp.server.set_session_model(model_id=C, session_id=other.session_id)
    pointer = get_model_pointer()
    other_runtime = other.host.engine.current.loaded
    (_session_pid, other_pid) = starts(state)
    other_cwd = other.host.engine.workspace_primary_cwd
    assert model_in_use(session) == A

    # Adopted from the file, and only here.
    edit_key(acp.settings, MODEL_POINTER_KEY, B)
    await acp.full(session)
    assert model_in_use(session) == B
    assert model_source(session) is Source.USER
    assert get_model_pointer() == pointer

    # Ordinary reloads, also running together, keep it even though the pointer still says A.
    results = await asyncio.gather(
        acp.reload(session),
        acp.set_option(session, "rollback_snapshots_keep", "9"),
        acp.reload(session),
    )
    assert results[0] == {} and results[2] == {}
    assert model_in_use(session) == B
    assert model_source(session) is Source.USER

    # So does a change of working directory.
    moved = acp.home / "moved"
    moved.mkdir()
    await acp.server.ext_method("session/set_workspace", {"sessionId": session.session_id, "primaryCwd": str(moved)})
    assert model_in_use(session) == B
    assert session.host.engine.workspace_primary_cwd == str(moved.resolve())

    # A later session starts from the file, not from what startup seeded, and stays there.
    newer = await acp.open("newer")
    assert model_in_use(newer) == B
    assert await acp.reload(newer) == {}
    assert model_in_use(newer) == B

    # The next full reload decides again from the file as it is then.
    edit_key(acp.settings, MODEL_POINTER_KEY, C)
    await acp.full(session)
    assert model_in_use(session) == C
    assert model_in_use(newer) == B

    # A removed key takes the default away: no load brings A back from the pointer.
    edit_key(acp.settings, MODEL_POINTER_KEY, None)
    await acp.full(session)
    assert default_model(session) == ""
    assert model_source(session) is Source.DEFAULT
    assert model_in_use(session) != A or model_in_use(session) is None
    in_use = model_in_use(session)
    await acp.reload(session)
    assert default_model(session) == ""
    assert model_in_use(session) == in_use
    latest = await acp.open("latest")
    assert default_model(latest) == ""
    await acp.reload(latest)
    assert default_model(latest) == ""

    # Nothing moved the pointer, or anything of the other session's.
    assert get_model_pointer() == pointer
    assert model_in_use(other) == C
    assert other.host.engine.current.loaded is other_runtime
    assert other.host.engine.workspace_primary_cwd == other_cwd
    await acp.use_tool(other, "other-untouched")
    assert calls(state)[-1] == f"{other_pid} other-untouched"


@pytest.fixture
def client_server(tmp_path: Path) -> Iterator[tuple[Path, Path]]:
    """A real Streamable HTTP server, for a client to name when it opens a session."""
    served = state_dir(tmp_path, "served")
    (served / "token").write_text("Bearer client-token", encoding="utf-8")
    port_file = served / "port"
    process = subprocess.Popen(
        [sys.executable, str(PROBE_SERVER), "--state-dir", str(served), "--port-file", str(port_file)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        yield served, port_file
    finally:
        process.kill()
        process.wait()


@pytest.mark.parametrize("servers", [[]])
async def test_what_belongs_to_the_session_outlasts_a_full_reload_with_the_servers_its_client_supplied(
    acp: Acp, client_server: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NO_PROXY", "*")
    served, port_file = client_server
    await wait_for(port_file.exists, timeout=ENGINE_TURN_TIMEOUT, description="the server is listening")
    project = tmp_path / "project"
    project.mkdir()
    response = await acp.server.new_session(
        cwd=str(project),
        mcp_servers=[
            acp_schema.HttpMcpServer(
                name="client-server",
                url=f"http://127.0.0.1:{port_file.read_text(encoding='utf-8')}/mcp",
                headers=[acp_schema.HttpHeader(name="Authorization", value="Bearer client-token")],
                type="http",
            )
        ],
    )
    session = acp.manager.get(response.session_id)
    await acp.use_tool(session, "before")
    assert calls(served)[-1].endswith(" before")

    assert (await acp.full(session))["complete"]

    await acp.use_tool(session, "after")
    assert calls(served)[-1].endswith(" after")
    assert session.host.engine.workspace_primary_cwd == str(project.resolve())
    assert session.host.engine.loaded_settings.settings.ask_user_timeout_seconds is None
    assert session.host.engine.loaded_settings.source_for("tools.ask_user.timeout_seconds").layer is not Source.ENV


async def test_a_file_back_on_the_value_startup_saw_is_followed(acp: Acp) -> None:
    session = await acp.open("one")
    edit_key(acp.settings, MODEL_POINTER_KEY, B)
    await acp.full(session)
    assert model_in_use(session) == B

    edit_key(acp.settings, MODEL_POINTER_KEY, A)
    await acp.full(session)
    assert model_in_use(session) == A


async def default_model_for_new_sessions(acp: Acp) -> str:
    options = await asyncio.to_thread(acp.manager.get_config_options)
    (model,) = [option for option in options["options"] if option["key"] == "model_profile"]
    return model["baseValue"]


async def test_a_full_reload_refreshes_what_future_sessions_are_told_and_survives_failing_to(acp: Acp) -> None:
    session = await acp.open("one")
    edit_key(acp.settings, MODEL_POINTER_KEY, B)

    await acp.full(session)
    assert await default_model_for_new_sessions(acp) == B

    good = acp.settings.read_bytes()
    bus = session.host.event_bus

    async def broken_after_the_reloads_own_read(_event: ev.SettingsReloaded) -> None:
        acp.settings.write_text("{: not yaml", encoding="utf-8")

    await bus.subscribe(ev.SettingsReloaded, broken_after_the_reloads_own_read)
    try:
        assert (await acp.full(session))["complete"]
    finally:
        await bus.unsubscribe(ev.SettingsReloaded, broken_after_the_reloads_own_read)
    acp.settings.write_bytes(good)
    assert await default_model_for_new_sessions(acp) == B


async def test_after_a_process_wide_choice_new_sessions_are_told_what_they_will_get(acp: Acp) -> None:
    session = await acp.open("one")
    await acp.set_option(session, "model_profile", C)
    edit_key(acp.settings, MODEL_POINTER_KEY, B)

    await acp.full(session)

    assert model_in_use(session) == B
    assert await default_model_for_new_sessions(acp) == C
    assert model_in_use(await acp.open("two")) == C


async def test_a_later_process_wide_choice_reaches_sessions_that_settled_on_the_file_before_it(acp: Acp) -> None:
    edit_key(acp.settings, MODEL_POINTER_KEY, B)
    opened_after_the_edit = await acp.open("late")
    reloaded = await acp.open("full")
    await acp.full(reloaded)
    assert (model_in_use(opened_after_the_edit), model_in_use(reloaded)) == (B, B)

    await acp.set_option(await acp.open("chooser"), "model_profile", C)
    await acp.reload(opened_after_the_edit)
    await acp.reload(reloaded)

    assert (model_in_use(opened_after_the_edit), model_in_use(reloaded)) == (C, C)


async def test_a_choice_set_back_to_what_it_was_still_reaches_a_session_that_settled_in_between(acp: Acp) -> None:
    chooser = await acp.open("chooser")
    await acp.set_option(chooser, "model_profile", C)
    session = await acp.open("one")
    edit_key(acp.settings, MODEL_POINTER_KEY, B)
    await acp.full(session)
    assert model_in_use(session) == B

    await acp.set_option(chooser, "model_profile", A)
    await acp.set_option(chooser, "model_profile", C)
    await acp.reload(session)

    assert model_in_use(session) == C


async def test_a_display_name_passed_to_another_model_leaves_the_session_on_the_one_it_meant(acp: Acp) -> None:
    session = await acp.open("one")
    save_model(mock_model(A, "Shared"))
    edit_key(acp.settings, MODEL_POINTER_KEY, "Shared")
    await acp.full(session)
    assert model_in_use(session) == A

    save_model(mock_model(A, "Renamed"))
    save_model(mock_model(C, "Shared"))
    await acp.full(session)

    assert model_in_use(session) == A


async def test_a_model_the_user_chose_whose_file_is_gone_gives_way_on_a_full_reload(acp: Acp) -> None:
    session = await acp.open("one")
    await acp.server.set_session_model(model_id=C, session_id=session.session_id)
    (acp.settings.parent / "models" / f"{C}.yaml").unlink()

    reply = await acp.full(session)

    assert reply["complete"]
    assert "controls.reload_session_model_removed" in {line["key"] for line in reply["report"]}
    assert model_in_use(session) == A
    edit_key(acp.settings, MODEL_POINTER_KEY, B)
    await acp.full(session)
    assert model_in_use(session) == B


async def test_a_removed_model_gives_way_after_another_session_already_reloaded(acp: Acp) -> None:
    edit_key(acp.settings, MODEL_POINTER_KEY, C)
    one = await acp.open("one")
    two = await acp.open("two")
    await acp.server.set_session_model(model_id=C, session_id=two.session_id)
    assert model_in_use(one) == model_in_use(two) == C
    (acp.settings.parent / "models" / f"{C}.yaml").unlink()

    assert (await acp.full(one))["complete"]
    reply = await acp.full(two)

    assert reply["complete"]
    assert "controls.reload_session_model_removed" in {line["key"] for line in reply["report"]}
    assert model_in_use(one) == model_in_use(two) == A


async def test_a_removed_default_gives_way_in_a_session_whose_agent_binds_another_model(
    acp: Acp, servers: list[MCPServerConfig]
) -> None:
    edit_key(acp.settings, MODEL_POINTER_KEY, C)
    agent = make_agent(AGENT, model=B)
    agent.approval = ApprovalConfig(default="auto", overrides={})
    agent.tools.mcp = list(servers)
    save_agent(agent)
    one = await acp.open("one")
    await acp.full(one)
    two = await acp.open("two")
    assert (model_in_use(two), default_model(two)) == (B, C)
    (acp.settings.parent / "models" / f"{C}.yaml").unlink()

    await acp.full(one)
    reply = await acp.full(two)

    assert "controls.reload_model_fallback" in {line["key"] for line in reply["report"]}
    assert (model_in_use(two), default_model(two)) == (B, A)


async def test_a_default_that_fell_back_falls_back_again_when_its_stand_in_goes(acp: Acp) -> None:
    session = await acp.open("one")
    (acp.settings.parent / "models" / f"{A}.yaml").unlink()
    await acp.full(session)
    assert default_model(session) == B
    (acp.settings.parent / "models" / f"{B}.yaml").unlink()

    reply = await acp.full(session)

    assert reply["complete"]
    assert (default_model(session), model_in_use(session)) == (C, C)


async def test_a_model_the_user_chose_for_the_session_outlasts_a_full_reload(acp: Acp) -> None:
    session = await acp.open("one")
    await acp.server.set_session_model(model_id=C, session_id=session.session_id)
    edit_key(acp.settings, MODEL_POINTER_KEY, B)

    await acp.full(session)
    assert model_in_use(session) == C

    await acp.reload(session)
    assert model_in_use(session) == C
    assert session.host.engine.settings_handle.base.session_default_model is None


async def test_setting_the_default_model_option_is_followed_by_a_session_that_had_settled_on_another(acp: Acp) -> None:
    session = await acp.open("one")
    edit_key(acp.settings, MODEL_POINTER_KEY, B)
    await acp.full(session)
    assert model_in_use(session) == B

    await acp.set_option(session, "model_profile", C)

    assert model_in_use(session) == C
    assert get_model_pointer() == (C, SettingOrigin(layer=Source.PROCESS_RUNTIME))
    await acp.reload(session)
    assert model_in_use(session) == C


async def test_a_new_session_keeps_a_process_wide_choice_and_a_pointer_it_cannot_check(acp: Acp) -> None:
    set_model_pointer(C, origin=SettingOrigin(layer=Source.PROCESS_RUNTIME))
    edit_key(acp.settings, MODEL_POINTER_KEY, B)
    assert model_in_use(await acp.open("chosen")) == C

    set_model_pointer(A, origin=SettingOrigin(layer=Source.USER, path=acp.settings))
    acp.settings.write_text("{: not yaml", encoding="utf-8")
    assert model_in_use(await acp.open("unreadable")) == A


async def test_a_failed_full_reload_commits_nothing_and_leaves_the_other_session_working(acp: Acp, state: Path) -> None:
    session = await acp.open("one")
    other = await acp.open("other")
    (session_pid, other_pid) = starts(state)
    base = session.host.engine.settings_handle.base
    runtime = session.host.engine.current.loaded
    pointer = get_model_pointer()

    edit_key(acp.settings, MODEL_POINTER_KEY, B)
    acp.builds.failing.add(B)
    with pytest.raises(RequestError):
        await acp.full(session)

    assert session.host.engine.settings_handle.base is base
    assert session.host.engine.current.loaded is runtime
    assert model_in_use(session) == A
    assert get_model_pointer() == pointer
    await acp.use_tool(session, "kept")
    assert calls(state)[-1] == f"{session_pid} kept"
    await acp.use_tool(other, "other-after-failure")
    assert calls(state)[-1] == f"{other_pid} other-after-failure"

    # The failure left no selection behind to follow, and the next one starts clean.
    await acp.reload(session)
    assert model_in_use(session) == A
    acp.builds.failing.clear()
    assert (await acp.full(session))["complete"]
    assert model_in_use(session) == B


async def test_an_unreadable_file_fails_a_full_reload_but_not_an_ordinary_one(acp: Acp, state: Path) -> None:
    session = await acp.open("one")
    acp.settings.write_text("{: not yaml", encoding="utf-8")

    with pytest.raises(RequestError):
        await acp.full(session)

    assert len(starts(state)) == 1
    assert model_in_use(session) == A
    assert await acp.reload(session) == {}


@dataclass
class Budget:
    """A full reload's real deadlines, which the test expires once the request is where it wants it."""

    deadlines: list[asyncio.Timeout] = field(default_factory=list)

    def expire(self) -> None:
        self.deadlines[-1].reschedule(asyncio.get_running_loop().time())

    async def expire_once_stalled(self, request: asyncio.Task[Any]) -> None:
        await wait_for(
            lambda: self.deadlines or request.done(),
            timeout=ENGINE_TURN_TIMEOUT,
            description="the request started its deadline",
        )
        assert not await wait_until(request.done, timeout=0.2)
        self.expire()


@pytest.fixture
def budget(monkeypatch: pytest.MonkeyPatch) -> Budget:
    budget = Budget()

    def recorded(when: float | None) -> asyncio.Timeout:
        budget.deadlines.append(asyncio.timeout_at(when))
        return budget.deadlines[-1]

    shadow = ModuleType("asyncio")
    vars(shadow).update(vars(asyncio))
    vars(shadow)["timeout_at"] = recorded
    monkeypatch.setattr(session_manager_module, "asyncio", shadow)
    return budget


async def expect_timed_out(request: Awaitable[dict[str, Any]]) -> None:
    with pytest.raises(RequestError) as caught:
        await asyncio.wait_for(request, ENGINE_TURN_TIMEOUT)
    assert "Timed out" in str(caught.value.data)
    assert "Nothing was changed" in str(caught.value.data)


async def test_the_budget_counts_the_wait_for_the_sessions_lock(acp: Acp, state: Path, budget: Budget) -> None:
    session = await acp.open("one")

    async with session.mutation_lock:
        request = asyncio.create_task(acp.full(session))
        await budget.expire_once_stalled(request)
        await expect_timed_out(request)

    assert len(starts(state)) == 1
    assert (await acp.full(session))["complete"]


async def test_the_budget_counts_a_turn_being_finished_and_never_cancels_it(
    acp: Acp, monkeypatch: pytest.MonkeyPatch, budget: Budget
) -> None:
    session = await acp.open("one")
    store = acp.manager.state_store
    real_save = store.save_session
    hold = Hold()

    async def held_save(*args: Any, **kwargs: Any) -> Any:
        await hold.pass_through()
        return await real_save(*args, **kwargs)

    monkeypatch.setattr(store, "save_session", held_save)
    turn = asyncio.create_task(acp.server.prompt([text_block("hello")], session_id=session.session_id))
    await wait_for(hold.entered.is_set, timeout=ENGINE_TURN_TIMEOUT, description="the turn is saving")

    try:
        request = asyncio.create_task(acp.full(session))
        await budget.expire_once_stalled(request)
        await expect_timed_out(request)

        assert not turn.done()
        assert not session.mutation_lock.locked()
    finally:
        hold.release()
    response = await asyncio.wait_for(turn, ENGINE_TURN_TIMEOUT)
    assert response.stop_reason == "end_turn"
    assert (await acp.full(session))["complete"]


async def test_the_budget_counts_a_connection_and_closes_what_it_opened(acp: Acp, state: Path, budget: Budget) -> None:
    session = await acp.open("one")
    (old_pid,) = starts(state)
    flag(state, "hold")

    request = asyncio.create_task(acp.full(session))
    await wait_for(
        lambda: len(starts(state)) == 2 or request.done(),
        timeout=ENGINE_TURN_TIMEOUT,
        description="the reconnect started its process",
    )
    assert not request.done()
    candidate = psutil.Process(starts(state)[1])
    budget.expire()
    await expect_timed_out(request)

    assert not session.mutation_lock.locked()
    assert not session.host.engine._loader._mcp_cache.refreshing
    (state / "hold").unlink()
    await wait_for(lambda: not candidate.is_running(), timeout=ENGINE_TURN_TIMEOUT, description="the candidate closed")
    await acp.use_tool(session, "old-connection")
    assert calls(state)[-1] == f"{old_pid} old-connection"


async def test_the_budget_counts_the_completion_event_and_the_reply_says_what_was_installed(
    acp: Acp, budget: Budget
) -> None:
    session = await acp.open("one")
    edit_key(acp.settings, MODEL_POINTER_KEY, B)
    hold = Hold()
    bus = session.host.event_bus

    async def held(_event: ev.SettingsReloaded) -> None:
        await hold.pass_through()

    await bus.subscribe(ev.SettingsReloaded, held)
    request = asyncio.create_task(acp.full(session))
    try:
        await wait_for(
            lambda: hold.entered.is_set() or request.done(),
            timeout=ENGINE_TURN_TIMEOUT,
            description="the completion event was published",
        )
        assert not request.done()
        budget.expire()
        reply = await asyncio.wait_for(request, ENGINE_TURN_TIMEOUT)
    finally:
        hold.release()
        await bus.unsubscribe(ev.SettingsReloaded, held)

    assert (reply["installed"], reply["complete"]) == (True, False)
    assert "Timed out" in reply["detail"]
    assert reply["report"] == []
    assert model_in_use(session) == B
    assert not session.mutation_lock.locked()
    assert (await acp.full(session))["complete"]


async def test_the_budget_counts_refreshing_the_defaults_after_the_install(acp: Acp, budget: Budget) -> None:
    session = await acp.open("one")
    edit_key(acp.settings, MODEL_POINTER_KEY, B)
    locked, unlock = threading.Event(), threading.Event()

    def another_writer() -> None:
        with acp.manager._global_settings_lock:
            locked.set()
            unlock.wait()

    writer = threading.Thread(target=another_writer)
    writer.start()
    try:
        await wait_for(locked.is_set, timeout=ENGINE_TURN_TIMEOUT, description="the other writer holds the lock")
        request = asyncio.create_task(acp.full(session))
        await wait_for(
            lambda: len(budget.deadlines) == 2 or request.done(),
            timeout=ENGINE_TURN_TIMEOUT,
            description="the reload was installed and went on to the defaults",
        )
        assert not request.done()
        assert not session.mutation_lock.locked()
        budget.expire()
        reply = await asyncio.wait_for(request, ENGINE_TURN_TIMEOUT)
    finally:
        unlock.set()
        await asyncio.to_thread(writer.join)

    assert reply["complete"]
    assert model_in_use(session) == B


async def test_a_failure_after_the_install_answers_what_was_installed(acp: Acp) -> None:
    session = await acp.open("one")
    edit_key(acp.settings, MODEL_POINTER_KEY, B)
    bus = session.host.event_bus

    async def later_step_fails(_event: ev.SettingsReloaded) -> None:
        await bus.publish(ev.Error(session_id=session.host.session_id, message="A later step failed."))

    await bus.subscribe(ev.SettingsReloaded, later_step_fails)
    try:
        reply = await acp.full(session)
    finally:
        await bus.unsubscribe(ev.SettingsReloaded, later_step_fails)

    assert (reply["installed"], reply["complete"]) == (True, False)
    assert reply["detail"] == "A later step failed."
    assert model_in_use(session) == B
