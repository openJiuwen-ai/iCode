# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for on_session_ready, agent-runtime and usage-update handling."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from rich.text import Text

from chrys.app.tui.screens.main.state import (
    MainScreenServices,
    MainScreenState,
    RuntimeState,
    SessionViewState,
    UsageViewState,
)
from chrys.app.tui.support.gc_freeze import (
    GcReclaimReason,
    GcReclaimRequested,
)
from chrys.app.tui.widgets.sidebar.context import ContextUsageState
from chrys.app.tui.widgets.sidebar.tasks import TodoListState
from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import (
    AgentRuntimeDetails,
    AgentRuntimeUpdated,
    RuntimeHookDetails,
    RuntimeHookSourceDetails,
    RuntimeModelDetails,
    RuntimeSkillDetails,
    SessionReady,
    UsageUpdate,
)
from chrys.foundation.models.todos import TodoItem
from tests.support.tui_helpers import (
    fake_session_title,
    make_backend_handler,
    status_trail,
)


def test_session_ready_during_restore_refreshes_memory_file_status() -> None:
    """Restore-specific SessionReady handling must keep loaded memory metadata.

    During restore the chat panel is rebuilt by SessionRestored, but the
    SessionReady event is still the only event carrying auto-loaded memory
    files.  Dropping it made restored AGENTS.md loads invisible until /chdir
    forced a later ProfileSwitched event.
    """

    tool_info: dict[str, str] = {}
    flash_calls: list[str] = []

    class _FakePanel:
        def set_profile(self, _profile: str) -> None:
            return

        def set_tool_kinds(self, _tool_kinds: dict[str, str]) -> None:
            return

        def update_welcome(self, *_args, **_kwargs) -> None:
            raise AssertionError("restore SessionReady must not rebuild chat panel")

        def set_session_id(self, _session_id: str) -> None:
            raise AssertionError("restore SessionReady must not update chat session id")

    class _FakeInputBar:
        def set_clipboard_image_dir(self, _directory: object) -> None:
            return

    class _FakeStatusBar:
        def set_profile(self, _profile: str, *, description: str = "") -> None:
            return

        def set_tool_info(self, trail: str) -> None:
            tool_info["trail"] = trail

        def flash(self, text: str, **_kwargs) -> None:
            flash_calls.append(text)

    panel = _FakePanel()
    input_bar = _FakeInputBar()
    status = _FakeStatusBar()
    # The Hooks tab is told when the number of configured hooks changes.
    hook_runs: list[None] = []
    sidebar = SimpleNamespace(hooks_panel=SimpleNamespace(runs_changed=lambda: hook_runs.append(None)))

    def _query_one(cls):
        if cls.__name__ == "ChatPanel":
            return panel
        if cls.__name__ == "InputBar":
            return input_bar
        if cls.__name__ == "StatusBar":
            return status
        if cls.__name__ == "SidebarPanel":
            return sidebar
        raise AssertionError(f"unexpected query_one({cls.__name__})")

    services = MainScreenServices(bus=EventBus(), active_model_profile_id="old-model")
    screen = SimpleNamespace(
        _state=MainScreenState(session=SessionViewState(restoring_session=True)),
        _services=services,
        query_one=_query_one,
        _update_subtitle=lambda: None,
        _debug=lambda *_args: None,
    )
    handler = make_backend_handler(screen)

    asyncio.run(
        handler.on_session_ready(
            SessionReady(
                agent_profile="Code",
                display_name="Code Agent",
                session_id="session-1",
                memory_files=["AGENTS.md"],
                runtime_details=AgentRuntimeDetails(
                    model=RuntimeModelDetails(profile_id="ready-model", selection_source="active"),
                    hook_sources=[
                        RuntimeHookSourceDetails(
                            scope="global",
                            hooks=[RuntimeHookDetails(id="notify", enabled=True)],
                        )
                    ],
                ),
            )
        )
    )

    assert "1 file" in status_trail(tool_info["trail"])
    assert "1 hook" in status_trail(tool_info["trail"])
    assert "tooltip" not in tool_info
    assert flash_calls == []
    assert services.active_model_profile_id == "ready-model"
    assert hook_runs == [None]


def _run_existing_session_ready(
    *,
    current_max_context_tokens: int,
    event_max_context_tokens: int,
) -> tuple[SimpleNamespace, list[tuple[str, object]], ContextUsageState]:
    calls: list[tuple[str, object]] = []
    initial_context_usage = ContextUsageState.with_window(
        used_tokens=19_635,
        max_context_tokens=current_max_context_tokens,
        total_session_tokens=253_535,
        total_session_input_tokens=120_000,
        total_session_output_tokens=83_535,
        total_session_cache_hit_tokens=50_000,
    )

    class _FakePanel:
        border_subtitle = None

        def set_profile(self, profile: str) -> None:
            calls.append(("profile", profile))

        def set_tool_kinds(self, _tool_kinds: dict[str, str]) -> None:
            return

        def update_welcome(self, *, profile: str = "", cwd: str = "") -> None:
            calls.append(("welcome", (profile, cwd)))

        def set_session_id(self, session_id: str) -> None:
            calls.append(("session_id", session_id))

        def set_workspace_cwd(self, cwd: str) -> None:
            calls.append(("workspace_cwd", cwd))
            self.border_subtitle = Text(cwd)

    class _FakeInputBar:
        def set_clipboard_image_dir(self, directory: object) -> None:
            calls.append(("clipboard_image_dir", directory))

        def set_paste_cwd(self, cwd: str) -> None:
            calls.append(("paste_cwd", cwd))

    class _FakeStatusBar:
        def set_profile(self, profile: str, *, description: str = "") -> None:
            calls.append(("status_profile", (profile, description)))

        def set_tool_info(self, trail: str) -> None:
            calls.append(("tool_info", trail))

        def clear_status(self) -> None:
            calls.append(("clear_status", None))

        def flash(self, text: str, **_kwargs) -> None:
            calls.append(("flash", text))

    panel = _FakePanel()
    input_bar = _FakeInputBar()
    status = _FakeStatusBar()

    def query_one(cls: type):
        if cls.__name__ == "ChatPanel":
            return panel
        if cls.__name__ == "InputBar":
            return input_bar
        if cls.__name__ == "StatusBar":
            return status
        raise AssertionError(f"unexpected query_one({cls.__name__})")

    screen = SimpleNamespace(
        _state=MainScreenState(usage=UsageViewState(last_usage_tokens=19635, last_total_session_tokens=253535)),
        _gc_messages=[],
        context_usage_state=initial_context_usage,
        query_one=query_one,
        _update_subtitle=lambda: calls.append(("subtitle", None)),
        _set_agent_loading=lambda value: calls.append(("agent_loading", value)),
        _debug=lambda *_args: None,
        current=SimpleNamespace(loaded=None, manifest=SimpleNamespace(runtime_details=None)),
    )
    handler = make_backend_handler(screen)

    asyncio.run(
        handler.on_session_ready(
            SessionReady(
                agent_profile="Code",
                display_name="Code Agent",
                session_id="session-existing",
                max_context_tokens=event_max_context_tokens,
                primary_cwd="/workspace/existing",
            )
        )
    )
    return screen, calls, initial_context_usage


def test_session_ready_for_existing_session_preserves_context_usage_when_max_unchanged() -> None:
    """A repeated SessionReady must not zero the Context-panel token breakdown."""

    screen, calls, initial_context_usage = _run_existing_session_ready(
        current_max_context_tokens=200_000,
        event_max_context_tokens=200_000,
    )

    assert screen.context_usage_state is initial_context_usage
    assert screen.chat_workspace_cwd == "/workspace/existing"
    assert ("paste_cwd", "/workspace/existing") in calls
    assert ("session_id", "session-existing") in calls
    assert len(screen._gc_messages) == 1
    assert isinstance(screen._gc_messages[0], GcReclaimRequested)
    assert screen._gc_messages[0].reason is GcReclaimReason.SESSION_READY
    assert screen._gc_messages[0].prompt is True


def test_session_ready_for_existing_session_preserves_breakdown_when_max_changes() -> None:
    """A repeated SessionReady max refresh must carry the Context-panel breakdown forward."""

    screen, calls, initial_context_usage = _run_existing_session_ready(
        current_max_context_tokens=200_000,
        event_max_context_tokens=250_000,
    )

    assert screen.context_usage_state is not initial_context_usage
    assert screen.context_usage_state == ContextUsageState.with_window(
        used_tokens=19_635,
        max_context_tokens=250_000,
        total_session_tokens=253_535,
        total_session_input_tokens=120_000,
        total_session_output_tokens=83_535,
        total_session_cache_hit_tokens=50_000,
    )
    assert screen.chat_workspace_cwd == "/workspace/existing"
    assert ("paste_cwd", "/workspace/existing") in calls
    assert ("session_id", "session-existing") in calls


def test_agent_runtime_updated_refreshes_resource_counts_without_model_trail() -> None:
    tool_info: dict[str, str] = {}
    runtime_details = AgentRuntimeDetails(
        model=RuntimeModelDetails(
            profile_id="deepseek",
            name="DeepSeek-V4-Flash",
            model_id="deepseek-v4-flash",
            max_context_tokens=1_000_000,
        ),
        skill_details=[RuntimeSkillDetails(name="unit-converter", description="Convert units")],
        hook_sources=[
            RuntimeHookSourceDetails(
                scope="workdir",
                hooks=[
                    RuntimeHookDetails(id="guard", enabled=True),
                    RuntimeHookDetails(id="disabled", enabled=False),
                ],
            )
        ],
    )

    class _FakeStatusBar:
        def set_tool_info(self, trail: str) -> None:
            tool_info["trail"] = trail

    # The Hooks tab is told when the number of configured hooks changes.
    hook_runs: list[None] = []
    sidebar = SimpleNamespace(hooks_panel=SimpleNamespace(runs_changed=lambda: hook_runs.append(None)))

    def _query_one(cls):
        if cls.__name__ == "StatusBar":
            return _FakeStatusBar()
        if cls.__name__ == "SidebarPanel":
            return sidebar
        raise AssertionError(f"unexpected query_one({cls.__name__})")

    screen = SimpleNamespace(
        query_one=_query_one, current=SimpleNamespace(loaded=None, manifest=SimpleNamespace(runtime_details=None))
    )
    handler = make_backend_handler(screen)

    asyncio.run(
        handler.on_agent_runtime_updated(
            AgentRuntimeUpdated(
                model_profile_id="deepseek",
                max_context_tokens=1_000_000,
                tool_names=["read_file", "load_skill"],
                skill_names=["unit-converter"],
                memory_files=["AGENTS.md"],
                runtime_details=runtime_details,
            )
        )
    )

    assert handler._state.runtime.details is runtime_details
    trail = status_trail(tool_info["trail"])
    assert trail == "2 tools · 1 skill · 1 hook · 1 file"
    assert hook_runs == [None], "the disabled hook is not counted, the enabled one is"
    assert "DeepSeek-V4-Flash" not in trail
    assert "deepseek-v4-flash" not in trail
    assert "1m" not in trail


def test_session_ready_for_new_session_resets_terminal_title_to_cwd() -> None:
    """Starting a new session should clear the previous user-message title preview."""

    calls: list[tuple[str, object]] = []
    terminal_title_cwds: list[str] = []

    class _FakePanel:
        border_subtitle = None

        def set_profile(self, profile: str) -> None:
            calls.append(("profile", profile))

        def set_tool_kinds(self, _tool_kinds: dict[str, str]) -> None:
            return

        async def clear(self) -> None:
            calls.append(("clear", None))

        def update_welcome(self, *, profile: str = "", cwd: str = "") -> None:
            calls.append(("welcome", (profile, cwd)))

        def set_session_id(self, session_id: str) -> None:
            calls.append(("session_id", session_id))

        def set_workspace_cwd(self, cwd: str) -> None:
            calls.append(("workspace_cwd", cwd))
            self.border_subtitle = Text(cwd)

    class _FakeInputBar:
        retry_mode = True

        def set_paste_cwd(self, cwd: str) -> None:
            calls.append(("paste_cwd", cwd))

        def set_clipboard_image_dir(self, directory: object) -> None:
            calls.append(("clipboard_image_dir", directory))

    class _FakeStatusBar:
        def set_profile(self, profile: str, *, description: str = "") -> None:
            calls.append(("status_profile", (profile, description)))

        def set_tool_info(self, _trail: str) -> None:
            return

        def clear_status(self) -> None:
            calls.append(("clear_status", None))

        def flash(self, text: str, **_kwargs) -> None:
            calls.append(("flash", text))

    class _FakeContextPanel:
        def reset(self, max_context_tokens: int = 0) -> None:
            calls.append(("context_reset", max_context_tokens))

    class _FakeSidebarPanel:
        context_panel = _FakeContextPanel()

    class _FakeStateStore:
        # Fake skips session_short_id; production JsonFileStateStore.session_dir shortens ids.
        def session_dir(self, session_id: str) -> Path:
            return Path("/sessions") / session_id

    panel = _FakePanel()
    input_bar = _FakeInputBar()
    status = _FakeStatusBar()
    sidebar = _FakeSidebarPanel()

    def record_terminal_title_cwd(cwd: str | None = None) -> None:
        terminal_title_cwds.append(cwd or "<current>")

    def query_one(cls: type):
        if cls.__name__ == "ChatPanel":
            return panel
        if cls.__name__ == "InputBar":
            return input_bar
        if cls.__name__ == "StatusBar":
            return status
        if cls.__name__ == "SidebarPanel":
            return sidebar
        raise AssertionError(f"unexpected query_one({cls.__name__})")

    state = MainScreenState(session=SessionViewState(creating_new_session=True))
    creating: list[bool] = []
    screen = SimpleNamespace(
        _state=state,
        _set_creating_new_session=creating.append,
        _services=MainScreenServices(bus=EventBus(), state_store=_FakeStateStore()),
        query_one=query_one,
        _set_has_messages=lambda value: calls.append(("has_messages", value)),
        _set_agent_loading=lambda value: calls.append(("agent_loading", value)),
        _session_title=fake_session_title(set_terminal_title_for_cwd=record_terminal_title_cwd),
        _update_subtitle=lambda: calls.append(("subtitle", None)),
        _update_toc=lambda: calls.append(("toc", None)),
        _debug=lambda *_args: None,
        current=SimpleNamespace(loaded=None, manifest=SimpleNamespace(runtime_details=None)),
    )
    handler = make_backend_handler(screen)
    handler._agent_load_dialog = None

    asyncio.run(
        handler.on_session_ready(
            SessionReady(
                agent_profile="Code",
                display_name="Code Agent",
                session_id="session-new",
                max_context_tokens=123,
                primary_cwd="/workspace/new",
            )
        )
    )

    assert state.session.creating_new_session is False
    assert creating == [False]
    assert input_bar.retry_mode is False
    assert terminal_title_cwds == ["/workspace/new"]
    assert ("session_id", "session-new") in calls
    assert ("paste_cwd", "/workspace/new") in calls
    assert ("clipboard_image_dir", Path("/sessions/session-new/attachments/clipboard")) in calls


def test_usage_update_uses_source_id_for_session_window() -> None:
    """Same-profile sub-agent usage must not replace the main session window.

    Sub-agent UsageUpdates only update the session totals row in the sidebar;
    the used/max gauge stays at the main session's value so it does not
    flicker between contexts.  Session totals advance on every event because
    the cumulative figure is global.
    """

    debug_log: list[tuple[str, str]] = []
    context_usage_states: list[ContextUsageState] = []

    screen_state = MainScreenState(
        runtime=RuntimeState(main_usage_source_id="session-main"),
        usage=UsageViewState(last_usage_tokens=13_614, last_total_session_tokens=13_614),
    )
    screen = SimpleNamespace(
        _state=screen_state,
        context_usage_state=ContextUsageState.with_window(
            used_tokens=13_614,
            max_context_tokens=180_000,
            total_session_tokens=13_614,
        ),
        _debug=lambda event_type, detail="": debug_log.append((event_type, detail)),
    )
    handler = make_backend_handler(screen)

    for idx, total_tokens in enumerate((72_000, 68_500, 71_250), start=1):
        asyncio.run(
            handler.on_usage_update(
                UsageUpdate(
                    agent_profile="Code",
                    usage_source_id=f"sub-agent-{idx}",
                    total_tokens=total_tokens,
                    pct=round(total_tokens / 200_000 * 100, 1),
                    max_context_tokens=200_000,
                    local_tokens=total_tokens - 900,
                    total_session_tokens=233_900 + idx,
                )
            )
        )
        context_usage_states.append(screen.context_usage_state)

    assert debug_log == [
        ("Usage[Code]", "72,000 (36.0%) local=71,100 source=sub-agent-1"),
        ("Usage[Code]", "68,500 (34.2%) local=67,600 source=sub-agent-2"),
        ("Usage[Code]", "71,250 (35.6%) local=70,350 source=sub-agent-3"),
    ]
    # Main window must not move while a sub-agent fires usage updates...
    assert screen_state.usage.last_usage_tokens == 13_614
    # ...but cumulative session totals always advance.
    assert screen_state.usage.last_total_session_tokens == 233_903
    # Sidebar used/max gauge is NOT touched by sub-agent events; only the
    # session-totals state advances.
    assert [(state.used_tokens, state.max_context_tokens, state.update_window) for state in context_usage_states] == [
        (13_614, 180_000, False),
        (13_614, 180_000, False),
        (13_614, 180_000, False),
    ]
    assert [state.total_session_tokens for state in context_usage_states] == [233_901, 233_902, 233_903]

    asyncio.run(
        handler.on_usage_update(
            UsageUpdate(
                agent_profile="Code",
                usage_source_id="session-main",
                total_tokens=19_635,
                pct=9.8,
                max_context_tokens=200_000,
                local_tokens=17_960,
                total_session_tokens=253_535,
            )
        )
    )

    assert screen_state.usage.last_usage_tokens == 19_635
    assert screen_state.usage.last_total_session_tokens == 253_535
    # Main-session event refreshes the gauge via explicit routed view-state.
    assert screen.context_usage_state == ContextUsageState.with_window(
        used_tokens=19_635,
        max_context_tokens=200_000,
        total_session_tokens=253_535,
    )


def test_session_ready_for_new_session_clears_todo_state() -> None:
    """Creating a new session resets the Tasks panel at the post-success point."""

    calls: list[tuple[str, object]] = []

    class _FakePanel:
        border_subtitle = None

        def set_profile(self, profile: str) -> None:
            return

        def set_tool_kinds(self, _tool_kinds: dict[str, str]) -> None:
            return

        async def clear(self) -> None:
            calls.append(("clear", None))

        def update_welcome(self, *, profile: str = "", cwd: str = "") -> None:
            return

        def set_session_id(self, session_id: str) -> None:
            return

        def set_workspace_cwd(self, cwd: str) -> None:
            self.border_subtitle = Text(cwd)

    class _FakeInputBar:
        retry_mode = True

        def set_paste_cwd(self, cwd: str) -> None:
            return

        def set_clipboard_image_dir(self, directory: object) -> None:
            return

    class _FakeStatusBar:
        def set_profile(self, _profile: str, *, description: str = "") -> None:
            return

        def set_tool_info(self, _trail: str) -> None:
            return

        def clear_status(self) -> None:
            return

        def flash(self, text: str, **_kwargs) -> None:
            return

    class _FakeContextPanel:
        def reset(self, max_context_tokens: int = 0) -> None:
            return

    class _FakeSidebarPanel:
        context_panel = _FakeContextPanel()

    class _FakeStateStore:
        def session_dir(self, session_id: str) -> Path:
            return Path("/sessions") / session_id

    panel = _FakePanel()
    input_bar = _FakeInputBar()
    status = _FakeStatusBar()
    sidebar = _FakeSidebarPanel()

    def query_one(cls: type):
        if cls.__name__ == "ChatPanel":
            return panel
        if cls.__name__ == "InputBar":
            return input_bar
        if cls.__name__ == "StatusBar":
            return status
        if cls.__name__ == "SidebarPanel":
            return sidebar
        raise AssertionError(f"unexpected query_one({cls.__name__})")

    state = MainScreenState(session=SessionViewState(creating_new_session=True))
    creating: list[bool] = []
    screen = SimpleNamespace(
        _state=state,
        _set_creating_new_session=creating.append,
        _services=MainScreenServices(bus=EventBus(), state_store=_FakeStateStore()),
        todo_state=TodoListState(items=(TodoItem(content="stale from old session"),)),
        query_one=query_one,
        _set_has_messages=lambda value: calls.append(("has_messages", value)),
        _set_agent_loading=lambda value: calls.append(("agent_loading", value)),
        _session_title=fake_session_title(set_terminal_title_for_cwd=lambda cwd=None: calls.append(("title", cwd))),
        _update_subtitle=lambda: None,
        _update_toc=lambda: None,
        _debug=lambda *_args: None,
        current=SimpleNamespace(loaded=None, manifest=SimpleNamespace(runtime_details=None)),
    )
    handler = make_backend_handler(screen)
    handler._agent_load_dialog = None

    asyncio.run(
        handler.on_session_ready(
            SessionReady(
                agent_profile="Code",
                display_name="Code Agent",
                session_id="session-new",
                max_context_tokens=123,
                primary_cwd="/workspace/new",
            )
        )
    )

    assert state.session.creating_new_session is False
    assert creating == [False]
    assert screen.todo_state == TodoListState()


def test_session_ready_for_existing_session_preserves_todo_state() -> None:
    """A repeated SessionReady (no new-session flow) must not clear the Tasks panel."""

    screen, _calls, _initial = _run_existing_session_ready(
        current_max_context_tokens=200_000,
        event_max_context_tokens=200_000,
    )

    # clear_todos() would have stamped an empty TodoListState onto the screen.
    assert not hasattr(screen, "todo_state")
