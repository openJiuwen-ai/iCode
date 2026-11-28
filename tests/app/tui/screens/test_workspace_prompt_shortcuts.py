# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Workspace shortcut sources stay consistent across the composer, runtime dialog and engine."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import create_autospec

import pytest
from textual.widgets import Static

from chrys.app.tui.screens.dialogs.runtime_details import RuntimeDetailsDialog
from chrys.app.tui.widgets.chrome.suggestion_list import SuggestionList
from chrys.foundation.config.settings_store import LoadedSettings
from chrys.foundation.events import types as ev
from chrys.orchestration.engine.state import controls
from chrys.service.state.store import JsonFileStateStore
from tests.app.tui.screens.test_prompt_shortcut_send import (
    Session,
    await_error,
    running_session,
    send_plain,
    turn_finished,
    wait_ready,
)
from tests.support.waiting import ENGINE_TEST_WAIT_TIMEOUT, wait_for, with_wait_deadline
from tests.support.workspace_registry import write_workspace_shortcut

if TYPE_CHECKING:
    from tests.support.engines import AgentEngineFactory


def _keep_settings(session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    loaded = LoadedSettings(settings=session.engine.settings_handle.settings, provenance={})
    monkeypatch.setattr(controls, "load_settings", create_autospec(controls.load_settings, return_value=loaded))


async def _select(session: Session, workspace_id: str | None) -> None:
    request = ev.WorkspaceChange(
        workspace_operation=ev.WorkspaceOperation.SELECT if workspace_id else ev.WorkspaceOperation.UNSPECIFY,
        workspace_id=workspace_id,
    )
    await session.bus.publish(request, raise_handler_errors=True)
    assert session.engine.session.workspace_id == workspace_id
    await wait_ready(session)


def _shown_names(session: Session) -> list[str]:
    suggestions = session.main.query_one(SuggestionList)
    return [line.split()[0] for line in suggestions.render().plain.splitlines() if line.startswith("/zz")]


async def _list_names(session: Session, expected: list[str]) -> None:
    suggestions = session.main.query_one(SuggestionList)
    if suggestions.is_visible:
        await session.pilot.press("escape")
    session.chat.clear()
    session.chat.insert("/zz")
    await wait_for(
        lambda: suggestions.is_visible and not suggestions.is_loading and _shown_names(session) == expected,
        pilot=session.pilot,
        description=f"workspace shortcut rows {expected}",
    )
    assert _shown_names(session) == expected
    await session.pilot.press("escape")
    session.chat.clear()


@with_wait_deadline(ENGINE_TEST_WAIT_TIMEOUT)
async def test_workspace_selection_at_the_same_cwd_updates_slash_rows_and_runtime_sources(
    agent_engine: AgentEngineFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cwd, related = Path.cwd(), tmp_path / "related"
    await asyncio.to_thread(write_workspace_shortcut, cwd, "zzworkspace", "Workspace body")
    await asyncio.to_thread(write_workspace_shortcut, related, "zzrelated", "Related body")
    workspace = await asyncio.to_thread(
        JsonFileStateStore(tmp_path / "sessions").workspace_registry.create, "App", str(cwd), (str(related),)
    )

    async with running_session(agent_engine, tmp_path, monkeypatch) as session:
        _keep_settings(session, monkeypatch)
        await _list_names(session, ["/zzworkspace"])
        await _select(session, workspace.id)
        assert session.engine.workspace is not None
        assert session.engine.workspace.primary_cwd == str(cwd)
        await _list_names(session, ["/zzrelated", "/zzworkspace"])

        session.main.action_runtime_details()
        await wait_for(
            lambda: isinstance(session.app.screen, RuntimeDetailsDialog) and session.app.screen.is_mounted,
            pilot=session.pilot,
            description="runtime dialog shown",
        )
        dialog = session.app.screen
        assert isinstance(dialog, RuntimeDetailsDialog)

        def sources() -> list[Static]:
            return list(dialog.query("#runtime-shortcuts .runtime-detail-section").results(Static))

        await wait_for(
            lambda: any("/zzrelated" in section.content.plain for section in sources()),
            pilot=session.pilot,
            description="runtime lists workspace shortcuts",
        )
        assert [section.content.plain.split()[0] for section in sources()] == ["/zzworkspace", "/zzrelated"]
        assert str(sources()[1].border_title).endswith(str(Path("related/.chrys/shortcuts")))


@with_wait_deadline(ENGINE_TEST_WAIT_TIMEOUT)
async def test_editing_workspace_updates_shortcuts_and_leaving_removes_their_rows(
    agent_engine: AgentEngineFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cwd, removed, added = Path.cwd(), tmp_path / "removed", tmp_path / "added"
    for root, name in ((cwd, "zzworkspace"), (removed, "zzremoved"), (added, "zzadded")):
        await asyncio.to_thread(write_workspace_shortcut, root, name, f"Body of {name}")
    registry = JsonFileStateStore(tmp_path / "sessions").workspace_registry
    workspace = await asyncio.to_thread(registry.create, "App", str(cwd), (str(removed),))

    async with running_session(agent_engine, tmp_path, monkeypatch) as session:
        _keep_settings(session, monkeypatch)
        await _select(session, workspace.id)
        await _list_names(session, ["/zzremoved", "/zzworkspace"])
        await asyncio.to_thread(registry.update, workspace.id, name="App", primary=str(cwd), folders=(str(added),))
        await _select(session, workspace.id)
        await send_plain(session, "Use the edited workspace", typed=False)
        await turn_finished(session)
        await _list_names(session, ["/zzadded", "/zzworkspace"])

        await _select(session, None)
        await _list_names(session, ["/zzworkspace"])


@with_wait_deadline(ENGINE_TEST_WAIT_TIMEOUT)
async def test_removing_a_workspace_folder_refuses_its_expanded_shortcut_and_keeps_the_draft(
    agent_engine: AgentEngineFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cwd, related = Path.cwd(), tmp_path / "related"
    draft = "Review !`echo ran > workspace-shortcut-ran.txt`"
    await asyncio.to_thread(write_workspace_shortcut, related, "zzremoved", draft)
    registry = JsonFileStateStore(tmp_path / "sessions").workspace_registry
    workspace = await asyncio.to_thread(registry.create, "App", str(cwd), (str(related),))

    async with running_session(agent_engine, tmp_path, monkeypatch) as session:
        _keep_settings(session, monkeypatch)
        await _select(session, workspace.id)
        session.chat.insert("/zzremoved")
        await wait_for(
            lambda: session.main.query_one(SuggestionList).is_visible and _shown_names(session) == ["/zzremoved"],
            pilot=session.pilot,
            description="workspace shortcut listed",
        )
        await session.pilot.press("tab")
        await wait_for(lambda: session.chat.text == draft, pilot=session.pilot, description="shortcut expanded")
        await asyncio.to_thread(registry.update, workspace.id, name="App", primary=str(cwd), folders=())
        await _select(session, workspace.id)

        await session.pilot.press("enter")
        await await_error(session, "prompt_shortcut_unavailable")
        await wait_for(
            lambda: session.chat.text == draft and session.chat.has_focus,
            pilot=session.pilot,
            description="refused draft kept in composer",
        )
        assert not (cwd / "workspace-shortcut-ran.txt").exists()
        assert session.seen.of(ev.PromptCommandsRunning) == []
        assert session.seen.of(ev.TurnStarted) == []
        assert session.model.call_history == []
