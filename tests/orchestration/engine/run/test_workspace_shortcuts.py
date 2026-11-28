# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Workspace shortcut sources follow the committed workspace through real engine admission."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from chrys.foundation.events.types import (
    Error,
    PromptCommandsRunning,
    PromptShortcutRef,
    UserMessage,
    WorkspaceChange,
    WorkspaceOperation,
)
from chrys.service.llm.mock import MockChatClient, MockResponse
from chrys.service.prompt_shortcuts import load_prompt_shortcuts, prompt_shortcut_folders
from chrys.service.state.store import JsonFileStateStore
from tests.support.event_capture import collect_events
from tests.support.waiting import ENGINE_TEST_WAIT_TIMEOUT, await_run_task_chain, with_wait_deadline
from tests.support.workspace_engine import select_workspace, start_workspace_engine
from tests.support.workspace_registry import write_workspace_shortcut

if TYPE_CHECKING:
    from chrys.orchestration.engine.engine import AgentEngine
    from tests.support.engines import AgentEngineFactory


def _ref(engine: AgentEngine, name: str) -> PromptShortcutRef:
    workspace = engine.session.session_workspace
    folders = prompt_shortcut_folders(
        engine.workspace_primary_cwd,
        [directory.path for directory in workspace.working_dirs] if workspace is not None else (),
    )
    (shortcut,) = [shortcut for shortcut in load_prompt_shortcuts(**folders) if shortcut.name == name]
    return PromptShortcutRef(name=shortcut.name, path=shortcut.path)


@pytest.mark.skipif(sys.platform == "win32", reason="pwd and shell redirection use POSIX shell syntax")
@with_wait_deadline(ENGINE_TEST_WAIT_TIMEOUT)
async def test_workspace_shortcut_commands_run_in_the_session_working_directory(
    agent_engine: AgentEngineFactory, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("SHELL", "/bin/sh")
    primary, related = tmp_path / "primary", tmp_path / "related"
    await asyncio.to_thread(primary.mkdir)
    source = await asyncio.to_thread(write_workspace_shortcut, related, "where", "Work here: !`pwd`")
    workspace = await asyncio.to_thread(
        JsonFileStateStore(tmp_path / "sessions").workspace_registry.create, "App", str(primary), [str(related)]
    )
    client = MockChatClient([MockResponse(text="Done")])
    engine = await start_workspace_engine(
        agent_engine,
        monkeypatch,
        primary,
        workspace_id=workspace.id,
        client=client,
        store=JsonFileStateStore(tmp_path / "sessions"),
    )
    ref = await asyncio.to_thread(_ref, engine, "where")
    assert Path(ref.path) == source

    message = UserMessage(text="Work here: !`pwd`", injection_id="where", prompt_shortcut=ref)
    await engine.event_bus.publish(message, raise_handler_errors=True)
    await await_run_task_chain(engine, turn_state=engine.turns.turn_state)

    assert message.started_turn
    messages, _options = client.call_history[0]
    assert any(f"Work here: {primary.resolve()}" in item.text for item in messages if item.role == "user")
    assert engine.workspace_primary_cwd == str(primary)


@pytest.mark.parametrize("change", ["remove_folder", "leave_workspace"])
@with_wait_deadline(ENGINE_TEST_WAIT_TIMEOUT)
async def test_a_workspace_shortcut_draft_is_refused_before_any_command_when_its_source_leaves_the_workspace(
    agent_engine: AgentEngineFactory, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, change: str
) -> None:
    primary, related = tmp_path / "primary", tmp_path / "related"
    await asyncio.to_thread(primary.mkdir)
    marker = tmp_path / "ran"
    typed = f'Run !`echo first > "{marker}"` then !`echo second >> "{marker}"`'
    await asyncio.to_thread(write_workspace_shortcut, related, "removed", typed)
    registry = JsonFileStateStore(tmp_path / "sessions").workspace_registry
    workspace = await asyncio.to_thread(registry.create, "App", str(primary), [str(related)])
    client = MockChatClient([MockResponse(text="Should not be sent")])
    engine = await start_workspace_engine(
        agent_engine,
        monkeypatch,
        primary,
        workspace_id=workspace.id,
        client=client,
        store=JsonFileStateStore(tmp_path / "sessions"),
    )
    ref = await asyncio.to_thread(_ref, engine, "removed")
    errors: list[Error] = []
    commands: list[PromptCommandsRunning] = []
    await engine.event_bus.subscribe(Error, lambda event: collect_events(errors, event))
    await engine.event_bus.subscribe(PromptCommandsRunning, lambda event: collect_events(commands, event))
    if change == "remove_folder":
        await asyncio.to_thread(registry.update, workspace.id, name="App", primary=str(primary), folders=[])
        await select_workspace(engine.event_bus, workspace.id)
    else:
        request = WorkspaceChange(workspace_operation=WorkspaceOperation.UNSPECIFY)
        await engine.event_bus.publish(request, raise_handler_errors=True)
        assert engine.session.workspace_id is None

    message = UserMessage(text=typed, injection_id="removed", prompt_shortcut=ref)
    await engine.event_bus.publish(message, raise_handler_errors=True)
    await await_run_task_chain(engine, turn_state=engine.turns.turn_state)

    assert [error.code for error in errors] == ["prompt_shortcut_unavailable"]
    assert commands == []
    assert not marker.exists()
    assert not message.started_turn
    assert client.call_history == []
    assert "removed" not in [
        shortcut.name
        for shortcut in await asyncio.to_thread(
            load_prompt_shortcuts,
            **prompt_shortcut_folders(
                engine.workspace_primary_cwd,
                (),
            ),
        )
    ]


@with_wait_deadline(ENGINE_TEST_WAIT_TIMEOUT)
async def test_adding_a_workspace_folder_makes_its_shortcut_available_after_rebuild(
    agent_engine: AgentEngineFactory, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    primary, related = tmp_path / "primary", tmp_path / "related"
    await asyncio.to_thread(primary.mkdir)
    source = await asyncio.to_thread(write_workspace_shortcut, related, "added", "Report !`echo ADDED`")
    registry = JsonFileStateStore(tmp_path / "sessions").workspace_registry
    workspace = await asyncio.to_thread(registry.create, "App", str(primary))
    client = MockChatClient([MockResponse(text="Done")])
    engine = await start_workspace_engine(
        agent_engine,
        monkeypatch,
        primary,
        workspace_id=workspace.id,
        client=client,
        store=JsonFileStateStore(tmp_path / "sessions"),
    )
    await asyncio.to_thread(registry.update, workspace.id, name="App", primary=str(primary), folders=[str(related)])
    await select_workspace(engine.event_bus, workspace.id)
    message = UserMessage(
        text="Report !`echo ADDED`",
        injection_id="added",
        prompt_shortcut=PromptShortcutRef(name="added", path=str(source)),
    )

    await engine.event_bus.publish(message, raise_handler_errors=True)
    await await_run_task_chain(engine, turn_state=engine.turns.turn_state)

    assert message.started_turn
    messages, _options = client.call_history[0]
    assert any("Report ADDED" in item.text for item in messages if item.role == "user")
