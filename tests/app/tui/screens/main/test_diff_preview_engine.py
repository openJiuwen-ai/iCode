# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Real engine edits, restore, rollback and clear reach the sidebar through production events."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from pathlib import Path
from unittest.mock import create_autospec

import pytest

import chrys.orchestration.engine.build.builder as builder_module
import chrys.orchestration.sub_agents.tools as sub_agent_module
from chrys.app.tui.screens.diff.screen import load_diff_entries_by_period
from chrys.app.tui.screens.main.diff_controller import sorted_preview_entries
from chrys.app.tui.util.diff_entries import DiffFileEntry
from chrys.app.tui.util.diff_labels import entry_display_key, entry_source_suffix, filter_and_dedupe_entries
from chrys.app.tui.widgets.chrome.input_bar import InputBar
from chrys.app.tui.widgets.sidebar.diff_preview import DiffPreview
from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import SessionRestore, UserRollback
from chrys.foundation.models.session_surface import SessionSurface
from chrys.foundation.models.workspace import Workspace
from chrys.service.approval.policy import ApprovalMode
from chrys.service.llm.mock import MockChatClient, MockResponse
from chrys.service.profiles.agents.registry import AgentProfileRegistry
from chrys.service.profiles.agents.schema import (
    AgentProfile,
    ApprovalConfig,
    CompactionConfig,
    SubAgentRef,
    SubAgentsConfig,
    ToolsConfig,
)
from chrys.service.state.store import JsonFileStateStore
from tests.support.engines import AgentEngineFactory
from tests.support.pipeline_helpers import make_mock_settings_and_registry
from tests.support.tui_app_harness import make_chrys_app
from tests.support.waiting import ENGINE_TURN_TIMEOUT, await_run_task_chain, wait_for


def _display_keys(rows: Sequence[DiffFileEntry]) -> tuple[str, ...]:
    return tuple(entry_display_key(row) for row in rows)


@pytest.mark.parametrize(
    ("revert", "interrupt", "delegate"),
    [(False, False, False), (True, False, False), (None, False, False), (False, True, False), (False, False, True)],
    ids=["conversation-only", "revert-files", "clear", "interrupted", "sub-agent"],
)
async def test_engine_edit_restore_and_rollback_refresh_the_preview(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    agent_engine: AgentEngineFactory,
    revert: bool | None,
    interrupt: bool,
    delegate: bool,
) -> None:
    settings, models = make_mock_settings_and_registry(stream=False)
    target = tmp_path / "edited.txt"
    target.write_text("before\n", encoding="utf-8")
    later_target = tmp_path / "z-later.txt"
    later_target.write_text("before\n", encoding="utf-8")
    profile = AgentProfile(
        name="Editor",
        instructions="Edit files.",
        tools=ToolsConfig(builtins=["filesystem.write"]),
        approval=ApprovalConfig(default="auto"),
        compaction=CompactionConfig(enabled=False),
    )
    client = MockChatClient(
        responses=[
            MockResponse(
                tool_calls=[
                    ("edit_file", "edit1", {"path": str(target), "old_string": "before", "new_string": "after"})
                ]
            ),
            MockResponse(
                tool_calls=[
                    (
                        "edit_file",
                        "edit2",
                        {"path": str(later_target), "old_string": "before", "new_string": "later"},
                    )
                ]
            ),
            MockResponse(
                tool_calls=[
                    (
                        "edit_file",
                        "edit3-failed",
                        {"path": str(target), "old_string": "not in this file", "new_string": "must not appear"},
                    )
                ]
            ),
            MockResponse(text="Edited"),
        ]
    )

    child_profile = AgentProfile(
        name="Worker",
        sub_agent_only=True,
        instructions="Edit the requested file.",
        tools=ToolsConfig(builtins=["filesystem.write"]),
        approval=ApprovalConfig(default="auto"),
        compaction=CompactionConfig(enabled=False),
    )
    if delegate:
        profile.sub_agents = SubAgentsConfig(agents=[SubAgentRef(profile="Worker")])
        child_client = client
        monkeypatch.setattr(
            sub_agent_module,
            "create_client",
            create_autospec(sub_agent_module.create_client, return_value=child_client),
        )
        client = MockChatClient(
            responses=[
                MockResponse(tool_calls=[("Worker", "delegate1", {"prompt": "Edit the requested file"})]),
                MockResponse(text="Delegated edit complete"),
            ]
        )

    def signature(rows):
        return sorted(
            (entry_display_key(row), row.operation.value, row.content_omitted, entry_source_suffix(row))
            for row in filter_and_dedupe_entries(list(rows))
        )

    held, release = asyncio.Event(), asyncio.Event()
    original_response = client._mock_response

    async def response(resp, model_id, finish_reason, on_text):
        if resp.text in {"Edited", "Delegated edit complete"}:
            held.set()
            await release.wait()
        return await original_response(resp, model_id, finish_reason, on_text)

    monkeypatch.setattr(client, "_mock_response", response)
    monkeypatch.setattr(
        builder_module, "create_client", create_autospec(builder_module.create_client, return_value=client)
    )
    bus = EventBus()
    registry = AgentProfileRegistry()
    registry.register(profile)
    if delegate:
        registry.register(child_profile)
    store = JsonFileStateStore(tmp_path / "sessions")
    engine = agent_engine(
        bus,
        settings=settings,
        agent_registry=registry,
        model_registry=models,
        state_store=store,
        initial_workspace=Workspace.from_cwd(str(tmp_path)),
        surface=SessionSurface.TUI,
        initial_approval_mode=ApprovalMode.BYPASS,
    )
    engine.pin_model_profile()
    app = make_chrys_app(
        tmp_path / "sessions",
        settings=engine.settings_handle.settings,
        settings_handle=engine.settings_handle,
        engine=engine,
        event_bus=bus,
        agent_registry=registry,
        model_registry=models,
        profile_name="Editor",
    )
    async with app.run_test(size=(140, 40)) as pilot:
        await wait_for(
            lambda: (
                app._main_screen is not None
                and bool(app._main_screen.chat_session_id)
                and engine.current.loaded is not None
                and not app._main_screen.query_one(InputBar).agent_loading
            ),
            pilot=pilot,
            timeout=ENGINE_TURN_TIMEOUT,
            description="engine ready",
        )
        main = app._main_screen
        preview = main.query_one(DiffPreview)
        assert preview.entries == ()
        await app.workers.wait_for_complete()
        main._submit_user_text("Edit the file")
        await wait_for(
            lambda: client.call_count > 0,
            pilot=pilot,
            timeout=ENGINE_TURN_TIMEOUT,
            description="submitted turn admitted",
        )
        task = engine.turn_lifecycle_task
        assert task is not None
        try:
            await wait_for(
                lambda: held.is_set() or task.done(),
                timeout=ENGINE_TURN_TIMEOUT,
                description="answer held after two edits and the failed retry",
            )
            if task.done():
                await task
            assert held.is_set()
            await wait_for(lambda: len(preview.entries) == 2, pilot=pilot, description="live edits shown before save")
            tracker = engine.session.mutation_tracker
            assert tracker is not None
            recorded_times: dict[str, float] = {}
            for turn in tracker.get_all_turns():
                for mutation in turn.mutations:
                    if (changed_at := mutation.changed_at) is not None:
                        recorded_times[mutation.path] = max(recorded_times.get(mutation.path, 0.0), changed_at)
            failed = [
                mutation
                for turn in tracker.get_all_turns()
                for mutation in turn.mutations
                if mutation.path == str(target) and mutation.proves_no_change
            ]
            assert len(failed) == 1
            assert failed[0].t_end is not None
            assert failed[0].t_end > recorded_times[str(later_target)]
            assert failed[0].changed_at is None
            assert recorded_times[str(later_target)] > recorded_times[str(target)] > 0
            # These are still-running tool results, not entries recovered from the final save.
            assert {
                path: mutation.last_changed_at for path, mutation in main._live_diff.snapshot_file_mutations().items()
            } == recorded_times
            assert {entry.path: entry.last_changed_at for entry in preview.entries} == recorded_times
            expected_order = [str(later_target), str(target)]
            assert [entry.path for entry in preview.entries] == expected_order
            viewer = (await main._diff_controller.load_current()).merged
            assert signature(preview.entries) == signature(viewer.all_entries)
            if interrupt:
                await main._input_flow.publish_interrupt()
        finally:
            release.set()
        await await_run_task_chain(engine, turn_state=engine.turns.turn_state)
        await wait_for(lambda: len(preview.entries) == 2, pilot=pilot, description="persisted edits in sidebar")
        viewer = (await main._diff_controller.load_current()).merged
        assert signature(preview.entries) == signature(viewer.all_entries)
        assert target.read_text(encoding="utf-8") == "after\n"
        assert later_target.read_text(encoding="utf-8") == "later\n"
        assert [entry.path for entry in preview.entries] == expected_order
        session_id = engine.session.session_id
        persisted = load_diff_entries_by_period(store, session_id, str(tmp_path), include_content=False)
        assert {entry.path: entry.last_changed_at for entry in persisted.all_entries} == recorded_times
        assert [entry.path for entry in sorted_preview_entries(persisted)] == expected_order
        await main._sessions.create_new_session()
        await wait_for(
            lambda: main.chat_session_id != session_id and not preview.entries,
            pilot=pilot,
            timeout=ENGINE_TURN_TIMEOUT,
            description="new session clears rows",
        )
        await bus.publish(SessionRestore(session_id=session_id), raise_handler_errors=True)
        await wait_for(
            lambda: main.chat_session_id == session_id and len(preview.entries) == 2,
            pilot=pilot,
            timeout=ENGINE_TURN_TIMEOUT,
            description="restored session reloads rows",
        )
        assert {entry.path: entry.last_changed_at for entry in preview.entries} == recorded_times
        assert [entry.path for entry in preview.entries] == expected_order
        if revert is None:
            await main._sessions.delete_current_and_new(session_id)
        else:
            await bus.publish(UserRollback(target_turn=0, revert_changes=revert), raise_handler_errors=True)
        await wait_for(
            lambda: not preview.entries,
            pilot=pilot,
            timeout=ENGINE_TURN_TIMEOUT,
            description="rollback removes discarded changes",
        )
        assert target.read_text(encoding="utf-8") == ("before\n" if revert else "after\n")
        assert later_target.read_text(encoding="utf-8") == ("before\n" if revert else "later\n")


async def test_same_height_preview_refresh_does_not_relayout_or_restyle_a_populated_main_screen(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from chrys.app.tui.screens.main.diff_controller import CurrentDiffLoad, DiffController
    from chrys.app.tui.widgets.chat.panel import ChatPanel
    from chrys.app.tui.widgets.chrome.footer import ChrysFooter
    from chrys.foundation.events.types import SessionReady, SessionSaved
    from tests.app.tui.screens.main.test_diff_preview import data
    from tests.support.pilot_barrier import screen_is_settled

    bus = EventBus()
    app = make_chrys_app(tmp_path / "sessions", event_bus=bus)
    baseline = data(*(f"file{i:02}.py" for i in range(11)))
    current = [baseline]

    async def load(self, *, request=None, persisted=None, include_content=True):
        return CurrentDiffLoad(current[0], current[0])

    monkeypatch.setattr(DiffController, "load_current", load)
    async with app.run_test(size=(140, 45)) as pilot:
        main = app._main_screen
        assert main is not None
        panel = main.query_one(ChatPanel)
        for index in range(12):
            await panel.add_user_message(f"Question {index}")
            await panel.add_agent_message(f"## Answer {index}\n\nA paragraph with `code` and **emphasis**.")
        await bus.publish(SessionReady(session_id="s1", agent_profile="Code"), raise_handler_errors=True)
        preview = main.query_one(DiffPreview)
        await wait_for(
            lambda: (
                preview.rows > 0
                and _display_keys(preview.entries) == _display_keys(baseline.all_entries)
                and all(row.line_count_result != "pending" for row in preview.entries)
            ),
            pilot=pilot,
            description="initial preview shown",
        )
        assert [row.line_counts for row in preview.entries] == [(1, 1)] * 11
        visible_rows = preview.rows
        await wait_for(
            lambda: screen_is_settled(app, main) and not main._dirty_widgets and not main._repaint_required,
            pilot=pilot,
            description="populated screen settled",
        )
        layout = create_autospec(main._refresh_layout, side_effect=main._refresh_layout)
        styles = create_autospec(main.update_node_styles, side_effect=main.update_node_styles)
        footer = main.query_one(ChrysFooter)
        recompose = create_autospec(footer.recompose, side_effect=footer.recompose)
        monkeypatch.setattr(main, "_refresh_layout", layout)
        monkeypatch.setattr(main, "update_node_styles", styles)
        monkeypatch.setattr(footer, "recompose", recompose)
        current[0] = data(*(f"new{i:02}.py" for i in range(12)))
        await bus.publish(SessionSaved(session_id="s1"), raise_handler_errors=True)
        await wait_for(
            lambda: (
                _display_keys(preview.entries) == _display_keys(current[0].all_entries)
                and all(row.line_count_result != "pending" for row in preview.entries)
            ),
            pilot=pilot,
            description="saved changes repainted",
        )
        assert [row.line_counts for row in preview.entries] == [(1, 1)] * 12
        await wait_for(
            lambda: screen_is_settled(app, main) and not main._dirty_widgets and not main._repaint_required,
            pilot=pilot,
            description="repaint settled",
        )
        assert preview.rows == visible_rows
        layout.assert_not_called()
        styles.assert_not_called()
        recompose.assert_not_called()


async def test_hidden_or_covered_sidebar_chat_catches_up_when_shown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from textual.screen import Screen

    from chrys.app.tui.screens.main.diff_controller import CurrentDiffLoad, DiffController
    from chrys.app.tui.util.visibility import is_widget_shown_on_active_screen
    from chrys.app.tui.widgets.sidebar.diff_preview import SidebarChatPanel
    from chrys.app.tui.widgets.sidebar.panel import SidebarPanel
    from chrys.foundation.events.types import SessionReady, SessionSaved
    from tests.app.tui.screens.main.test_diff_preview import data

    current = [data("first.py")]
    calls = []

    async def load(self, *, request=None, persisted=None, include_content=True):
        calls.append(persisted)
        return CurrentDiffLoad(current[0], current[0])

    monkeypatch.setattr(DiffController, "load_current", load)
    bus = EventBus()
    app = make_chrys_app(tmp_path / "sessions", event_bus=bus)
    async with app.run_test(size=(140, 40)) as pilot:
        main = app._main_screen
        assert main is not None
        await bus.publish(SessionReady(session_id="s1", agent_profile="Code"), raise_handler_errors=True)
        preview = main.query_one(DiffPreview)
        sidebar_chat = main.query_one(SidebarChatPanel)
        sidebar = main.query_one(SidebarPanel)
        await wait_for(
            lambda: (
                _display_keys(preview.entries) == _display_keys(current[0].all_entries)
                and all(row.line_count_result != "pending" for row in preview.entries)
            ),
            pilot=pilot,
            description="initial changes shown",
        )
        assert preview.entries[0].line_counts == (1, 1)
        for hidden in ("overlay", "sidebar", "workflow"):
            if hidden == "overlay":
                app.push_screen(Screen())
            elif hidden == "sidebar":
                main.action_toggle_sidebar()
            else:
                sidebar.set_workflow_mode(True)
            await wait_for(
                lambda: not is_widget_shown_on_active_screen(sidebar_chat),
                pilot=pilot,
                description=f"Chat hidden by {hidden}",
            )
            previous, before_calls = preview.entries, len(calls)
            current[0] = data(f"{hidden}.py")
            await bus.publish(SessionSaved(session_id="s1"), raise_handler_errors=True)
            assert preview.entries == previous
            assert len(calls) == before_calls
            if hidden == "overlay":
                app.pop_screen()
            elif hidden == "sidebar":
                main.action_toggle_sidebar()
            else:
                sidebar.set_workflow_mode(False)
            await wait_for(
                lambda: (
                    _display_keys(preview.entries) == _display_keys(current[0].all_entries)
                    and all(row.line_count_result != "pending" for row in preview.entries)
                ),
                pilot=pilot,
                description=f"Chat catches up after {hidden}",
            )
            assert preview.entries[0].line_counts == (1, 1)


async def test_workspace_updated_clears_old_display_keys_before_reloading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from textual.screen import Screen

    from chrys.app.tui.screens.main.diff_controller import CurrentDiffLoad, DiffController
    from chrys.foundation.events.types import SessionReady, WorkspaceUpdated
    from tests.app.tui.screens.main.test_diff_preview import data

    old_cwd, new_cwd = tmp_path / "old", tmp_path / "new"
    old_cwd.mkdir()
    new_cwd.mkdir()
    current = [data("before.py")]
    baselines = []

    async def load(self, *, request=None, persisted=None, include_content=True):
        baselines.append(persisted)
        return CurrentDiffLoad(current[0], current[0])

    monkeypatch.setattr(DiffController, "load_current", load)
    bus = EventBus()
    app = make_chrys_app(tmp_path / "sessions", event_bus=bus)
    async with app.run_test(size=(140, 40)) as pilot:
        main = app._main_screen
        assert main is not None
        await bus.publish(
            SessionReady(session_id="s1", agent_profile="Code", primary_cwd=str(old_cwd)), raise_handler_errors=True
        )
        preview = main.query_one(DiffPreview)
        await wait_for(
            lambda: (
                _display_keys(preview.entries) == _display_keys(current[0].all_entries)
                and all(row.line_count_result != "pending" for row in preview.entries)
            ),
            pilot=pilot,
            description="old cwd rows visible",
        )
        assert preview.entries[0].line_counts == (1, 1)
        overlay = Screen()
        app.push_screen(overlay)
        await wait_for(lambda: app.screen is overlay and overlay.is_mounted, pilot=pilot, description="overlay active")
        current[0] = data("after.py")
        await bus.publish(WorkspaceUpdated(session_id="s1", primary_cwd=str(new_cwd)), raise_handler_errors=True)
        assert main._workspace_cwd() == str(new_cwd)
        assert preview.entries == ()
        app.pop_screen()
        await wait_for(
            lambda: (
                _display_keys(preview.entries) == _display_keys(current[0].all_entries)
                and all(row.line_count_result != "pending" for row in preview.entries)
            ),
            pilot=pilot,
            description="new cwd rows loaded",
        )
        assert preview.entries[0].line_counts == (1, 1)
        assert baselines[-1] is None
