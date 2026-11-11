# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for on_session_restored / on_workspace_updated screen reprojection."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from rich.text import Text

from chrys.app.tui.screens.main.session_handlers import (
    SessionHandler,
)
from chrys.app.tui.screens.main.state import MainScreenServices, MainScreenState, SessionViewState, UsageViewState
from chrys.app.tui.support.gc_freeze import (
    GcReclaimReason,
    GcReclaimRequested,
)
from chrys.app.tui.widgets.sidebar.context import ContextUsageState
from chrys.app.tui.widgets.sidebar.tasks import TodoListState
from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import (
    SessionRestored,
    WorkspaceUpdated,
)
from chrys.foundation.i18n import MessageRef
from chrys.foundation.models.todos import TodoItem
from chrys.foundation.util.session_ids import session_short_id
from chrys.kernel import Message
from chrys.service.context.providers.history import CompressedBlock
from tests.support.tui_helpers import (
    fake_session_title,
    main_screen_state_at,
    make_session_handler,
    status_text,
)


def test_restore_session_title_state_honors_prefer_recovery() -> None:
    """Crash-recovered sessions must read title overlays from the same source
    as the replayed history (the recovery sidecar), not the stale primary."""

    calls: list[tuple[str, object]] = []

    class _Store:
        async def load_session_meta(self, session_id: str, *, prefer_recovery: bool = False) -> object:
            calls.append(("meta", (session_id, prefer_recovery)))
            return SimpleNamespace(custom_title="Pinned", generated_title="Auto", title="first msg")

    class _Ui:
        def reset_session_title_state(self) -> None:
            calls.append(("reset", None))

        def set_session_title_state(self, *, custom: str, generated: str, fallback: str) -> None:
            calls.append(("set", (custom, generated, fallback)))

    ui = _Ui()
    host = SimpleNamespace(state_store=_Store(), _ui=lambda: ui)

    asyncio.run(SessionHandler._restore_session_title_state(host, "sess1", prefer_recovery=True))

    assert ("meta", ("sess1", True)) in calls
    assert ("set", ("Pinned", "Auto", "first msg")) in calls


def test_apply_custom_title_clear_publishes_resolved_display_title() -> None:
    """Clearing a custom title falls back to the generated title; the event
    must carry that resolved display so the ACP bridge doesn't clear the
    client's label until the next turn regenerates one."""

    published: list[object] = []

    class _Bus:
        async def publish(self, event: object) -> None:
            published.append(event)

    class _Store:
        async def update_session_titles(
            self,
            session_id: str,
            *,
            custom_title: str | None = None,
            generated_title: str | None = None,
        ) -> object:
            return SimpleNamespace(
                custom_title="",
                generated_title="Auto topic",
                title="first msg",
                display_title="Auto topic",
            )

    class _Ui:
        def chat_session_id(self) -> str:
            return "sess1"

        def set_session_title_state(self, **kwargs: object) -> None:
            pass

        def flash_status(self, message: str) -> None:
            pass

    ui = _Ui()
    host = SimpleNamespace(
        state_store=_Store(),
        _custom_title_apply_lock=asyncio.Lock(),
        bus=_Bus(),
        _ui=lambda: ui,
    )

    asyncio.run(SessionHandler.apply_custom_session_title(host, "", "sess1"))

    [event] = published
    assert event.custom is True
    assert event.title == ""
    assert event.display_title == "Auto topic"


def test_session_restore_resets_terminal_title_to_restored_cwd() -> None:
    """Switching to an old session should clear the previous user-message title preview."""

    calls: list[tuple[str, object]] = []
    terminal_title_cwds: list[str] = []

    class _FakePanel:
        border_subtitle = None

        def update_welcome(self, *, profile: str = "", cwd: str = "") -> None:
            self.welcome_info = (profile, cwd)

        async def clear(self) -> None:
            calls.append(("clear", None))

        def set_session_id(self, session_id: str) -> None:
            calls.append(("session_id", session_id))

        def set_workspace_cwd(self, cwd: str) -> None:
            calls.append(("workspace_cwd", cwd))
            self.border_subtitle = Text(cwd)

        def update_usage(self, tokens: int, total_session_tokens: int = 0) -> None:
            calls.append(("usage", (tokens, total_session_tokens)))

        async def add_error(self, message: str, *, action_label: str | None = "Retry") -> None:
            calls.append(("error", (message, action_label)))

    class _FakeInputBar:
        retry_mode = True

        def set_paste_cwd(self, cwd: str) -> None:
            calls.append(("paste_cwd", cwd))

        def set_clipboard_image_dir(self, directory: object) -> None:
            calls.append(("clipboard_image_dir", directory))

    class _FakeStatusBar:
        def flash(self, text: str) -> None:
            calls.append(("flash", text))

    class _FakeContextPanel:
        def clear_blocks(self) -> None:
            calls.append(("clear_blocks", None))

    class _FakeSidebarPanel:
        context_panel = _FakeContextPanel()

    panel = _FakePanel()
    input_bar = _FakeInputBar()
    status = _FakeStatusBar()
    sidebar = _FakeSidebarPanel()
    context_usage_state = ContextUsageState.with_window(
        used_tokens=7,
        max_context_tokens=55,
        total_session_tokens=11,
    )

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

    state = MainScreenState(
        session=SessionViewState(restoring_session=True),
        usage=UsageViewState(last_usage_tokens=42, last_total_session_tokens=100),
    )
    restoring: list[bool] = []
    screen = SimpleNamespace(
        _state=state,
        _set_restoring_session=restoring.append,
        _gc_messages=[],
        context_usage_state=context_usage_state,
        query_one=query_one,
        _set_has_messages=lambda value: calls.append(("has_messages", value)),
        _session_title=fake_session_title(set_terminal_title_for_cwd=terminal_title_cwds.append),
        _events=SimpleNamespace(finish_agent_load=lambda message: calls.append(("finish", message))),
        _debug=lambda *_args: None,
    )

    asyncio.run(
        make_session_handler(screen).on_session_restored(
            SessionRestored(
                session_id="session-old",
                agent_profile="Code",
                display_name="Code Agent",
                message_count=3,
                primary_cwd="/old/missing/path",
                cwd_warning="Working directory no longer exists: /old/missing/path",
            )
        )
    )

    assert state.session.restoring_session is False
    assert restoring == [False]
    assert input_bar.retry_mode is False
    assert panel.welcome_info == ("Code Agent", "/old/missing/path")
    assert terminal_title_cwds == ["/old/missing/path"]
    assert ("paste_cwd", "/old/missing/path") in calls
    assert ("workspace_cwd", "/old/missing/path") in calls
    # A missing directory is asked about when the user next submits, not reported as a chat error.
    assert not [value for name, value in calls if name == "error"]
    assert ("session_id", "session-old") in calls
    assert screen.context_usage_state == context_usage_state
    assert len(screen._gc_messages) == 1
    assert isinstance(screen._gc_messages[0], GcReclaimRequested)
    assert screen._gc_messages[0].reason is GcReclaimReason.SESSION_RESTORED
    assert screen._gc_messages[0].prompt is True
    flash = next(value for name, value in calls if name == "flash")
    assert isinstance(flash, MessageRef)
    assert flash.definition.key == "tui.status.session_restored"
    assert status_text(flash) == f"Session restored: {session_short_id('session-old')}"


def test_session_restore_marks_fully_compacted_session_as_having_messages(tmp_path: Path) -> None:
    """A restored compacted session should still be forkable from the TUI."""

    calls: list[tuple[str, object]] = []
    compressed = CompressedBlock(
        compressed_context_id="ctx_1",
        messages=[Message("user", ["old real turn"]), Message("assistant", ["old reply"])],
        summary_text="old turn",
        marker_id="turn_1",
        turn_range=(1, 1),
        created_at="2026-03-17T00:00:00+00:00",
    )
    state = {"messages": [], "compressed_msgs": [compressed]}

    class _FakePanel:
        border_subtitle = None

        def update_welcome(self, *, profile: str = "", cwd: str = "") -> None:
            self.welcome_info = (profile, cwd)

        async def clear(self) -> None:
            calls.append(("clear", None))

        def set_session_id(self, session_id: str) -> None:
            calls.append(("session_id", session_id))

        def set_workspace_cwd(self, cwd: str) -> None:
            calls.append(("workspace_cwd", cwd))
            self.border_subtitle = Text(cwd)

        def update_usage(self, tokens: int, total_session_tokens: int = 0) -> None:
            calls.append(("usage", (tokens, total_session_tokens)))

        async def add_error(self, message: str, *, action_label: str | None = "Retry") -> None:
            calls.append(("error", (message, action_label)))

    class _FakeInputBar:
        retry_mode = True

        def set_paste_cwd(self, cwd: str) -> None:
            calls.append(("paste_cwd", cwd))

        def set_clipboard_image_dir(self, directory: object) -> None:
            calls.append(("clipboard_image_dir", directory))

    class _FakeStatusBar:
        def flash(self, text: str) -> None:
            calls.append(("flash", text))

    class _FakeContextPanel:
        def clear_blocks(self) -> None:
            calls.append(("clear_blocks", None))

        def add_compressed_block(
            self,
            ctx_id: str,
            summary: str,
            freed_messages: int = 0,
            turn_range: tuple[int, int] = (0, 0),
        ) -> None:
            calls.append(("compressed_block", (ctx_id, summary, freed_messages, turn_range)))

    class _FakeSidebarPanel:
        context_panel = _FakeContextPanel()

    class _FakeStateStore:
        def session_dir(self, session_id: str) -> Path:
            return tmp_path / "sessions" / session_id

        async def load_session(self, session_id: str, *, prefer_recovery: bool = False) -> dict[str, object]:
            calls.append(("load_session", (session_id, prefer_recovery)))
            return state

        async def load_session_raw(self, session_id: str, *, prefer_recovery: bool = False) -> list[dict[str, object]]:
            calls.append(("load_session_raw", (session_id, prefer_recovery)))
            return []

        async def list_sessions(self) -> list[object]:
            return []

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

    screen = SimpleNamespace(
        _state=MainScreenState(session=SessionViewState(restoring_session=True)),
        context_usage_state=ContextUsageState.with_window(
            used_tokens=0,
            max_context_tokens=100_000,
            total_session_tokens=0,
        ),
        _services=MainScreenServices(bus=EventBus(), state_store=_FakeStateStore()),
        query_one=query_one,
        _set_has_messages=lambda value: calls.append(("has_messages", value)),
        _session_title=fake_session_title(set_terminal_title_for_cwd=lambda cwd: calls.append(("title_cwd", cwd))),
        _events=SimpleNamespace(finish_agent_load=lambda message: calls.append(("finish", message))),
        _debug=lambda *_args: None,
    )

    asyncio.run(
        make_session_handler(screen).on_session_restored(
            SessionRestored(
                session_id="session-compacted",
                agent_profile="Code",
                display_name="Code Agent",
                message_count=0,
                primary_cwd="/old/missing/path",
            )
        )
    )

    assert ("has_messages", True) in calls
    assert ("has_messages", False) not in calls
    assert ("workspace_cwd", "/old/missing/path") in calls
    assert ("compressed_block", ("ctx_1", "old turn", 2, (1, 1))) in calls
    assert input_bar.retry_mode is False


def test_session_restore_moves_shell_panel_to_existing_restored_cwd(tmp_path: Path) -> None:
    """Restored sessions should keep the embedded terminal aligned with the agent workspace."""

    calls: list[tuple[str, object]] = []
    restored_cwd = tmp_path / "workspace"
    restored_cwd.mkdir()

    class _FakePanel:
        border_subtitle = None

        def update_welcome(self, *, profile: str = "", cwd: str = "") -> None:
            self.welcome_info = (profile, cwd)

        async def clear(self) -> None:
            calls.append(("clear", None))

        def set_session_id(self, session_id: str) -> None:
            calls.append(("session_id", session_id))

        def set_workspace_cwd(self, cwd: str) -> None:
            calls.append(("workspace_cwd", cwd))
            self.border_subtitle = Text(cwd)

        def update_usage(self, tokens: int, total_session_tokens: int = 0) -> None:
            calls.append(("usage", (tokens, total_session_tokens)))

        async def add_error(self, message: str, *, action_label: str | None = "Retry") -> None:
            calls.append(("error", (message, action_label)))

    class _FakeInputBar:
        retry_mode = True

        def set_paste_cwd(self, cwd: str) -> None:
            calls.append(("paste_cwd", cwd))

        def set_clipboard_image_dir(self, directory: object) -> None:
            calls.append(("clipboard_image_dir", directory))

    class _FakeStatusBar:
        def flash(self, text: str) -> None:
            calls.append(("flash", text))

    class _FakeContextPanel:
        def clear_blocks(self) -> None:
            calls.append(("clear_blocks", None))

    class _FakeSidebarPanel:
        context_panel = _FakeContextPanel()

    class _FakeShellPanel:
        async def change_directory(self, cwd: str) -> None:
            calls.append(("shell_cwd", cwd))

    panel = _FakePanel()
    input_bar = _FakeInputBar()
    status = _FakeStatusBar()
    sidebar = _FakeSidebarPanel()
    shell = _FakeShellPanel()

    def query_one(cls: type):
        if cls.__name__ == "ChatPanel":
            return panel
        if cls.__name__ == "InputBar":
            return input_bar
        if cls.__name__ == "StatusBar":
            return status
        if cls.__name__ == "SidebarPanel":
            return sidebar
        if cls.__name__ == "ShellPanel":
            return shell
        raise AssertionError(f"unexpected query_one({cls.__name__})")

    screen = SimpleNamespace(
        _state=MainScreenState(session=SessionViewState(restoring_session=True)),
        context_usage_state=ContextUsageState.with_window(
            used_tokens=0,
            max_context_tokens=100_000,
            total_session_tokens=0,
        ),
        query_one=query_one,
        _set_has_messages=lambda value: calls.append(("has_messages", value)),
        _session_title=fake_session_title(set_terminal_title_for_cwd=lambda cwd: calls.append(("title_cwd", cwd))),
        _events=SimpleNamespace(finish_agent_load=lambda message: calls.append(("finish", message))),
        _debug=lambda *_args: None,
    )

    asyncio.run(
        make_session_handler(screen).on_session_restored(
            SessionRestored(
                session_id="session-old",
                agent_profile="Code",
                display_name="Code Agent",
                message_count=0,
                primary_cwd=str(restored_cwd),
            )
        )
    )

    assert ("shell_cwd", str(restored_cwd)) in calls
    assert ("paste_cwd", str(restored_cwd)) in calls
    assert ("workspace_cwd", str(restored_cwd)) in calls
    assert panel.border_subtitle.plain == str(restored_cwd)


def test_workspace_update_with_messages_updates_terminal_title_and_chdir_marker(tmp_path: Path) -> None:
    """Changing cwd after chat starts should still update the terminal title."""

    calls: list[tuple[str, object]] = []
    system_messages: list[str] = []
    terminal_title_cwds: list[str] = []
    current_cwd = tmp_path / "current"
    next_cwd = tmp_path / "next"
    current_cwd.mkdir()
    next_cwd.mkdir()

    class _FakePanel:
        border_subtitle = None

        async def add_system(self, text: str, *, key: str | None = None) -> None:
            calls.append(("add_system_key", key))
            system_messages.append(text)

        async def update_system(self, _key: str, new_text: str) -> None:
            system_messages.append(new_text)

        async def remove_system(self, key: str) -> None:
            calls.append(("remove_system", key))

        def set_workspace_cwd(self, cwd: str) -> None:
            calls.append(("workspace_cwd", cwd))
            self.border_subtitle = Text(cwd)

    class _FakeShellPanel:
        async def change_directory(self, cwd: str) -> None:
            calls.append(("shell_cwd", cwd))

    class _FakeInputBar:
        def set_paste_cwd(self, cwd: str) -> None:
            calls.append(("paste_cwd", cwd))

        def set_clipboard_image_dir(self, directory: object) -> None:
            calls.append(("clipboard_image_dir", directory))

    panel = _FakePanel()
    shell = _FakeShellPanel()
    input_bar = _FakeInputBar()

    def query_one(cls: type):
        if cls.__name__ == "ChatPanel":
            return panel
        if cls.__name__ == "InputBar":
            return input_bar
        if cls.__name__ == "ShellPanel":
            return shell
        raise AssertionError(f"unexpected query_one({cls.__name__})")

    state = main_screen_state_at(str(current_cwd))
    state.run.has_messages = True
    workspace_cwds: list[str] = []
    screen = SimpleNamespace(
        _state=state,
        _set_workspace_cwd=workspace_cwds.append,
        _suggestions=SimpleNamespace(discard_file_index=lambda: None),
        query_one=query_one,
        _session_title=fake_session_title(set_terminal_title_for_cwd=terminal_title_cwds.append),
        _debug=lambda *_args: None,
    )

    asyncio.run(make_session_handler(screen).on_workspace_updated(WorkspaceUpdated(primary_cwd=str(next_cwd))))

    assert state.workspace_marker.current_cwd == str(next_cwd)
    assert workspace_cwds == [str(next_cwd)]
    assert panel.border_subtitle.plain == str(next_cwd)
    assert ("paste_cwd", str(next_cwd)) in calls
    assert ("shell_cwd", str(next_cwd)) in calls
    assert ("remove_system", "chdir") in calls
    assert system_messages == [f"Working directory → {next_cwd}"]
    assert terminal_title_cwds == [str(next_cwd)]


def test_workspace_update_with_missing_cwd_does_not_change_shell_directory(tmp_path: Path) -> None:
    """A restored session can point at a workspace that was deleted between launches."""

    calls: list[tuple[str, object]] = []
    terminal_title_cwds: list[str] = []
    missing_cwd = tmp_path / "deleted-workspace"

    class _FakePanel:
        border_subtitle = None

        def update_welcome(self, *, profile: str = "", cwd: str = "") -> None:
            calls.append(("welcome", (profile, cwd)))

        def set_workspace_cwd(self, cwd: str) -> None:
            calls.append(("workspace_cwd", cwd))
            self.border_subtitle = Text(cwd)

    class _FakeShellPanel:
        async def change_directory(self, cwd: str) -> None:
            raise FileNotFoundError(cwd)

    class _FakeInputBar:
        def set_paste_cwd(self, cwd: str) -> None:
            calls.append(("paste_cwd", cwd))

        def set_clipboard_image_dir(self, directory: object) -> None:
            calls.append(("clipboard_image_dir", directory))

    panel = _FakePanel()
    shell = _FakeShellPanel()
    input_bar = _FakeInputBar()

    def query_one(cls: type):
        if cls.__name__ == "ChatPanel":
            return panel
        if cls.__name__ == "InputBar":
            return input_bar
        if cls.__name__ == "ShellPanel":
            return shell
        raise AssertionError(f"unexpected query_one({cls.__name__})")

    state = main_screen_state_at(str(tmp_path))
    state.runtime.profile = "Code Agent"
    workspace_cwds: list[str] = []
    screen = SimpleNamespace(
        _state=state,
        _set_workspace_cwd=workspace_cwds.append,
        _suggestions=SimpleNamespace(discard_file_index=lambda: None),
        query_one=query_one,
        _session_title=fake_session_title(set_terminal_title_for_cwd=terminal_title_cwds.append),
        _debug=lambda *_args: None,
    )

    asyncio.run(make_session_handler(screen).on_workspace_updated(WorkspaceUpdated(primary_cwd=str(missing_cwd))))

    assert state.workspace_marker.current_cwd == str(missing_cwd)
    assert workspace_cwds == [str(missing_cwd)]
    assert panel.border_subtitle.plain == str(missing_cwd)
    assert ("paste_cwd", str(missing_cwd)) in calls
    assert ("welcome", ("Code Agent", str(missing_cwd))) in calls
    assert terminal_title_cwds == [str(missing_cwd)]


def test_session_restore_reseeds_todo_state_from_saved_state_tolerantly(tmp_path: Path) -> None:
    """Restoring a session reseeds the Tasks panel, skipping malformed entries."""

    calls: list[tuple[str, object]] = []
    state = {
        "messages": [],
        "chrys_todos": [
            {"content": "restored", "status": "completed", "active_form": ""},
            {"content": "", "status": "pending"},
            {"content": "bad status", "status": "someday"},
            "garbage",
            {"content": "kept"},
        ],
    }

    class _FakePanel:
        border_subtitle = None

        def update_welcome(self, *, profile: str = "", cwd: str = "") -> None:
            self.welcome_info = (profile, cwd)

        async def clear(self) -> None:
            return

        def set_session_id(self, session_id: str) -> None:
            return

        def set_workspace_cwd(self, cwd: str) -> None:
            self.border_subtitle = Text(cwd)

        def update_usage(self, tokens: int, total_session_tokens: int = 0) -> None:
            return

        async def add_error(self, message: str, *, action_label: str | None = "Retry") -> None:
            return

    class _FakeInputBar:
        retry_mode = True

        def set_paste_cwd(self, cwd: str) -> None:
            return

        def set_clipboard_image_dir(self, directory: object) -> None:
            return

    class _FakeStatusBar:
        def flash(self, text: str) -> None:
            return

    class _FakeContextPanel:
        def clear_blocks(self) -> None:
            return

    class _FakeSidebarPanel:
        context_panel = _FakeContextPanel()

    class _FakeStateStore:
        def session_dir(self, session_id: str) -> Path:
            return tmp_path / "sessions" / session_id

        async def load_session(self, session_id: str, *, prefer_recovery: bool = False) -> dict[str, object]:
            calls.append(("load_session", (session_id, prefer_recovery)))
            return state

        async def load_session_raw(self, session_id: str, *, prefer_recovery: bool = False) -> list[dict[str, object]]:
            return []

        async def list_sessions(self) -> list[object]:
            return []

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

    screen = SimpleNamespace(
        _state=MainScreenState(session=SessionViewState(restoring_session=True)),
        context_usage_state=ContextUsageState.with_window(
            used_tokens=0,
            max_context_tokens=100_000,
            total_session_tokens=0,
        ),
        _services=MainScreenServices(bus=EventBus(), state_store=_FakeStateStore()),
        query_one=query_one,
        _set_has_messages=lambda value: calls.append(("has_messages", value)),
        _session_title=fake_session_title(set_terminal_title_for_cwd=lambda cwd: None),
        _events=SimpleNamespace(finish_agent_load=lambda message: None),
        _debug=lambda *_args: None,
    )

    asyncio.run(
        make_session_handler(screen).on_session_restored(
            SessionRestored(
                session_id="session-with-todos",
                agent_profile="Code",
                display_name="Code Agent",
                message_count=0,
                primary_cwd="/old/missing/path",
            )
        )
    )

    assert screen.todo_state == TodoListState(
        items=(
            TodoItem(content="restored", status="completed"),
            TodoItem(content="kept"),
        )
    )


def test_session_restore_without_saved_todos_clears_stale_todo_state() -> None:
    """Switching to a session without todos must clear the previous session's list."""

    class _FakePanel:
        border_subtitle = None

        def update_welcome(self, *, profile: str = "", cwd: str = "") -> None:
            self.welcome_info = (profile, cwd)

        async def clear(self) -> None:
            return

        def set_session_id(self, session_id: str) -> None:
            return

        def set_workspace_cwd(self, cwd: str) -> None:
            self.border_subtitle = Text(cwd)

        def update_usage(self, tokens: int, total_session_tokens: int = 0) -> None:
            return

        async def add_error(self, message: str, *, action_label: str | None = "Retry") -> None:
            return

    class _FakeInputBar:
        retry_mode = True

        def set_paste_cwd(self, cwd: str) -> None:
            return

        def set_clipboard_image_dir(self, directory: object) -> None:
            return

    class _FakeStatusBar:
        def flash(self, text: str) -> None:
            return

    class _FakeContextPanel:
        def clear_blocks(self) -> None:
            return

    class _FakeSidebarPanel:
        context_panel = _FakeContextPanel()

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

    screen = SimpleNamespace(
        _state=MainScreenState(session=SessionViewState(restoring_session=True)),
        context_usage_state=None,
        todo_state=TodoListState(items=(TodoItem(content="stale"),)),
        query_one=query_one,
        _set_has_messages=lambda value: None,
        _session_title=fake_session_title(set_terminal_title_for_cwd=lambda cwd: None),
        _events=SimpleNamespace(finish_agent_load=lambda message: None),
        _debug=lambda *_args: None,
    )

    asyncio.run(
        make_session_handler(screen).on_session_restored(
            SessionRestored(
                session_id="session-bare",
                agent_profile="Code",
                display_name="Code Agent",
                message_count=1,
                primary_cwd="/old/missing/path",
            )
        )
    )

    assert screen.todo_state == TodoListState()
