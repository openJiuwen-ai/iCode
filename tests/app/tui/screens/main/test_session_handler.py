# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for SessionHandler restore, resume, fork and file-edit snapshot loading."""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from pathlib import Path
from types import SimpleNamespace

import pytest

from chrys.app.tui.i18n import LocaleController
from chrys.app.tui.screens.main.state import MainScreenServices, MainScreenState, RunState
from chrys.app.tui.widgets.chat.file_snapshot import FileSnapshotRef, file_snapshot_inline_char_limit
from chrys.foundation.branding import APP_DISPLAY_NAME
from chrys.foundation.config.settings import Settings
from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import (
    Error,
    SessionFork,
    SessionForked,
)
from chrys.foundation.models.history_markers import HistoryMarkerKind
from chrys.foundation.util.session_ids import session_short_id
from chrys.service.mutations.store import SnapshotStore
from chrys.service.mutations.tracker import MutationTracker
from chrys.service.mutations.types import MutationOp, MutationSource
from chrys.service.state.store import JsonFileStateStore
from tests.support.tui_helpers import (
    main_screen_state_at,
    make_session_handler,
    status_text,
)


def test_do_session_restore_ignores_running_agent() -> None:
    """Direct restore calls should preserve state while a turn is running."""
    begin_calls: list[str] = []
    published: list[object] = []

    async def begin_session_restore_load(session_id: str) -> None:
        begin_calls.append(session_id)

    async def publish(event: object) -> None:
        published.append(event)

    restoring: list[bool] = []
    state = MainScreenState(run=RunState(agent_running=True))
    screen = SimpleNamespace(
        _state=state,
        _services=MainScreenServices(bus=SimpleNamespace(publish=publish)),
        _events=SimpleNamespace(begin_session_restore_load=begin_session_restore_load),
        _set_restoring_session=restoring.append,
    )

    asyncio.run(make_session_handler(screen).do_session_restore("busy"))

    assert state.session.restoring_session is False
    assert restoring == []
    assert begin_calls == []
    assert published == []


def test_do_session_restore_can_bypass_loading_guard_for_startup_restore() -> None:
    """Startup --session restore keeps input locked while publishing restore."""
    begin_calls: list[str] = []
    published: list[object] = []

    async def begin_session_restore_load(session_id: str) -> None:
        begin_calls.append(session_id)

    async def publish(event: object) -> None:
        published.append(event)

    restoring: list[bool] = []
    state = MainScreenState(run=RunState(agent_loading=True))
    screen = SimpleNamespace(
        _state=state,
        _services=MainScreenServices(bus=SimpleNamespace(publish=publish)),
        _events=SimpleNamespace(begin_session_restore_load=begin_session_restore_load),
        _set_restoring_session=restoring.append,
    )

    asyncio.run(make_session_handler(screen).do_session_restore("startup-session", allow_while_loading=True))

    assert state.session.restoring_session is True
    assert restoring == [True]
    assert begin_calls == ["startup-session"]
    assert len(published) == 1
    assert published[0].session_id == "startup-session"
    assert published[0].apply_saved_model is True


def test_do_session_restore_disables_saved_model_when_startup_model_is_explicit() -> None:
    published: list[object] = []

    async def begin_session_restore_load(_session_id: str) -> None:
        return None

    async def publish(event: object) -> None:
        published.append(event)

    screen = SimpleNamespace(
        _state=MainScreenState(run=RunState(agent_loading=True)),
        _services=MainScreenServices(bus=SimpleNamespace(publish=publish), apply_saved_model_on_restore=False),
        _events=SimpleNamespace(begin_session_restore_load=begin_session_restore_load),
    )

    asyncio.run(make_session_handler(screen).do_session_restore("startup-session", allow_while_loading=True))

    assert len(published) == 1
    assert published[0].apply_saved_model is False


def test_do_session_restore_cleans_up_if_loading_ui_fails() -> None:
    """A pre-restore modal failure must not leave the screen stuck restoring."""
    cancel_calls: list[None] = []
    published: list[object] = []
    debug_calls: list[tuple[str, str]] = []

    async def begin_session_restore_load(_session_id: str) -> None:
        raise RuntimeError("push failed")

    async def publish(event: object) -> None:
        published.append(event)

    restoring: list[bool] = []
    state = MainScreenState()
    screen = SimpleNamespace(
        _state=state,
        _services=MainScreenServices(bus=SimpleNamespace(publish=publish)),
        _events=SimpleNamespace(
            begin_session_restore_load=begin_session_restore_load,
            cancel_agent_load=lambda: cancel_calls.append(None),
        ),
        _debug=lambda key, msg: debug_calls.append((key, msg)),
        _set_restoring_session=restoring.append,
    )

    asyncio.run(make_session_handler(screen).do_session_restore("broken"))

    assert state.session.restoring_session is False
    assert restoring == [True, False]
    assert cancel_calls == [None]
    assert published == []
    assert debug_calls == [("SessionRestore", "failed to open loading UI: push failed")]


def _resume_screen(
    *,
    latest: object,
    calls: list[tuple[str, object]],
    published: list[object],
    lookup_started: asyncio.Event | None = None,
    lookup_release: asyncio.Event | None = None,
    restoring: list[bool] | None = None,
) -> SimpleNamespace:
    async def begin_session_restore_load(session_id: str) -> None:
        calls.append(("lookup_modal" if not session_id else "restore_modal", session_id))

    class _FakeStateStore:
        async def load_latest_session_id(self, *, chat_only: bool = False) -> str | None:
            assert chat_only
            calls.append(("load_latest", None))
            if lookup_started is not None:
                lookup_started.set()
            if lookup_release is not None:
                await lookup_release.wait()
            if isinstance(latest, BaseException):
                raise latest
            return latest  # type: ignore[return-value]

        async def list_sessions(self) -> list[object]:
            raise AssertionError("resume must not scan list_sessions()")

    async def publish(event: object) -> None:
        published.append(event)

    return SimpleNamespace(
        _state=MainScreenState(),
        _services=MainScreenServices(bus=SimpleNamespace(publish=publish), state_store=_FakeStateStore()),
        _events=SimpleNamespace(
            begin_session_restore_load=begin_session_restore_load,
            cancel_agent_load=lambda: calls.append(("cancel", None)),
        ),
        _debug=lambda key, msg: calls.append(("debug", (key, msg))),
        notify=lambda message, **kwargs: calls.append(("notify", (message, kwargs.get("severity")))),
        _set_restoring_session=(restoring if restoring is not None else []).append,
    )


def test_resume_last_session_opens_modal_before_lookup_and_restores_latest() -> None:
    calls: list[tuple[str, object]] = []
    published: list[object] = []
    restoring: list[bool] = []
    screen = _resume_screen(latest="latest-session", calls=calls, published=published, restoring=restoring)

    asyncio.run(make_session_handler(screen).resume_last_session())

    assert [name for name, _ in calls] == ["lookup_modal", "load_latest", "restore_modal"]
    assert ("restore_modal", "latest-session") in calls
    assert screen._state.session.restoring_session is True
    # Once for the lookup modal, once more for the restore itself.
    assert restoring == [True, True]
    assert len(published) == 1
    assert published[0].session_id == "latest-session"


def test_resume_last_session_modal_is_open_while_lookup_blocks() -> None:
    calls: list[tuple[str, object]] = []
    published: list[object] = []

    async def scenario() -> None:
        started = asyncio.Event()
        release = asyncio.Event()
        screen = _resume_screen(
            latest="slow-session",
            calls=calls,
            published=published,
            lookup_started=started,
            lookup_release=release,
        )
        handler = make_session_handler(screen)
        task = asyncio.create_task(handler.resume_last_session())
        await started.wait()
        assert calls == [("lookup_modal", ""), ("load_latest", None)]
        assert published == []
        release.set()
        await task

    asyncio.run(scenario())

    assert ("restore_modal", "slow-session") in calls
    assert len(published) == 1


def test_resume_last_session_without_sessions_closes_modal_and_notifies() -> None:
    calls: list[tuple[str, object]] = []
    published: list[object] = []
    restoring: list[bool] = []
    screen = _resume_screen(latest=None, calls=calls, published=published, restoring=restoring)

    asyncio.run(make_session_handler(screen).resume_last_session())

    assert [name for name, _ in calls] == ["lookup_modal", "load_latest", "cancel", "notify"]
    assert calls[-1][1][1] == "warning"
    assert screen._state.session.restoring_session is False
    assert restoring == [True, False]
    assert published == []


def test_resume_last_session_lookup_failure_cleans_up() -> None:
    calls: list[tuple[str, object]] = []
    published: list[object] = []
    restoring: list[bool] = []
    screen = _resume_screen(
        latest=RuntimeError("index exploded"), calls=calls, published=published, restoring=restoring
    )

    asyncio.run(make_session_handler(screen).resume_last_session())

    assert ("cancel", None) in calls
    assert ("debug", ("SessionRestore", "failed to look up latest session: index exploded")) in calls
    assert screen._state.session.restoring_session is False
    assert restoring == [True, False]
    assert screen._state.run.agent_loading is False
    assert published == []


def test_resume_last_session_ignores_running_or_loading_agent() -> None:
    calls: list[tuple[str, object]] = []
    published: list[object] = []
    screen = _resume_screen(latest="ignored", calls=calls, published=published)
    screen._state.run.agent_running = True

    asyncio.run(make_session_handler(screen).resume_last_session())

    assert calls == []
    assert published == []


def test_fork_current_session_rejects_missing_session_identity() -> None:
    published: list[object] = []
    notifications: list[tuple[str, str, str]] = []

    async def publish(event: object) -> None:
        published.append(event)

    screen = SimpleNamespace(
        _services=MainScreenServices(bus=SimpleNamespace(publish=publish)),
        notify=lambda message, **_kwargs: notifications.append(message),
        query_one=lambda _type: SimpleNamespace(session_id=""),
    )

    asyncio.run(make_session_handler(screen).fork_current_session())

    assert published == []
    assert notifications == ["No active session to fork"]


def test_fork_current_session_publishes_session_fork_and_opens_loading_modal(monkeypatch: pytest.MonkeyPatch) -> None:
    from chrys.app.tui.terminal import launcher

    published: list[object] = []
    pushed: list[object] = []
    callbacks: list[object] = []
    loading: list[bool] = []
    monkeypatch.delenv("SSH_CONNECTION", raising=False)
    monkeypatch.delenv("SSH_CLIENT", raising=False)
    monkeypatch.delenv("SSH_TTY", raising=False)
    monkeypatch.setattr(launcher, "can_access_local_desktop", lambda _env=None: True)

    async def publish(event: object) -> None:
        assert state.run.agent_loading is True
        published.append(event)

    def query_one(cls: type) -> object:
        if cls.__name__ == "ChatPanel":
            return SimpleNamespace(session_id="session-1")
        raise AssertionError(cls)

    def push_screen(dialog: object, callback=None) -> None:
        pushed.append(dialog)
        callbacks.append(callback)

    state = MainScreenState(run=RunState(has_messages=True))
    screen = SimpleNamespace(
        _state=state,
        _services=MainScreenServices(bus=SimpleNamespace(publish=publish)),
        app=SimpleNamespace(push_screen=push_screen),
        query_one=query_one,
        notify=lambda *_args, **_kwargs: None,
        _set_agent_loading=loading.append,
        _debug=lambda *_args: None,
    )

    asyncio.run(make_session_handler(screen).fork_current_session())

    assert len(published) == 1
    assert isinstance(published[0], SessionFork)
    assert published[0].session_id == "session-1"
    assert [type(dialog).__name__ for dialog in pushed] == ["ForkSessionDialog"]
    assert pushed[0]._state == "loading"
    assert pushed[0]._show_new_window is True
    assert callbacks[0] is not None
    assert loading == [True]


def test_session_fork_dialog_hides_new_window_over_ssh(monkeypatch: pytest.MonkeyPatch) -> None:
    pushed: list[object] = []
    loading: list[bool] = []
    monkeypatch.setenv("SSH_CONNECTION", "127.0.0.1 50000 127.0.0.1 22")

    def push_screen(dialog: object, callback=None) -> None:
        del callback
        pushed.append(dialog)

    screen = SimpleNamespace(
        app=SimpleNamespace(push_screen=push_screen),
        _set_agent_loading=loading.append,
    )
    handler = make_session_handler(screen)

    asyncio.run(handler._open_session_fork_dialog("session-1"))

    assert pushed[0]._show_new_window is False
    assert loading == [True]


def test_on_session_forked_routes_dialog_results(monkeypatch: pytest.MonkeyPatch) -> None:
    launcher_calls: list[tuple[str, str | None]] = []
    monkeypatch.setattr(
        "chrys.app.tui.terminal.launcher.launch_new_chrys_window",
        lambda session_id, *, cwd=None: launcher_calls.append((session_id, cwd)),
    )

    def run_case(
        result: str, new_session_id: str
    ) -> tuple[list[str], list[str], list[tuple[str, str, str]], list[bool]]:
        pushes: list[object] = []
        callbacks: list[object] = []
        flashes: list[str] = []
        restores: list[str] = []
        notifications: list[tuple[str, str, str]] = []
        loading: list[bool] = []
        debug_calls: list[tuple[str, str]] = []

        class _FakeStatusBar:
            def flash(self, message: str) -> None:
                flashes.append(message)

        def query_one(cls: type) -> object:
            if cls.__name__ == "ChatPanel":
                return SimpleNamespace(session_id="current-session")
            if cls.__name__ == "StatusBar":
                return _FakeStatusBar()
            raise AssertionError(cls)

        def push_screen(dialog: object, callback=None) -> None:
            pushes.append(dialog)
            callbacks.append(callback)

        def set_agent_loading(value: bool) -> None:
            loading.append(value)

        started_workers: list[Coroutine[object, object, None]] = []
        screen = SimpleNamespace(
            app=SimpleNamespace(push_screen=push_screen),
            query_one=query_one,
            _started_workers=started_workers,
            notify=lambda message, *, title, severity="information", **_kwargs: notifications.append(
                (title, severity, message)
            ),
            _set_agent_loading=set_agent_loading,
            _state=main_screen_state_at("/workspace"),
            _debug=lambda key, value: debug_calls.append((key, value)),
        )
        handler = make_session_handler(screen)

        async def record_restore(session_id: str) -> None:
            restores.append(session_id)

        handler.do_session_restore = record_restore  # type: ignore[method-assign]

        asyncio.run(handler._open_session_fork_dialog("current-session"))
        assert pushes[0]._state == "loading"
        asyncio.run(
            handler.on_session_forked(
                SessionForked(
                    session_id="current-session",
                    parent_session_id="current-session",
                    new_session_id=new_session_id,
                )
            )
        )
        assert pushes[0]._state == "success"
        assert loading == [True, False]
        callbacks[0](result)
        for worker in started_workers:
            asyncio.run(worker)
        assert debug_calls == [("SessionForked", session_short_id(new_session_id))]
        return flashes, restores, notifications, loading

    flashes, restores, notifications, loading = run_case("stay", "fork-stay")
    assert [status_text(message) for message in flashes] == [f"Fork created: {session_short_id('fork-stay')}"]
    assert restores == []
    assert notifications == [("Fork", "information", f"Created fork {session_short_id('fork-stay')}")]
    assert loading == [True, False, False]

    flashes, restores, notifications, loading = run_case("switch", "fork-switch")
    assert flashes == []
    assert restores == ["fork-switch"]
    assert notifications == [("Fork", "information", f"Created fork {session_short_id('fork-switch')}")]
    assert loading == [True, False, False]

    flashes, restores, notifications, loading = run_case("new_window", "fork-window")
    assert [status_text(message) for message in flashes] == [f"Opened fork: {session_short_id('fork-window')}"]
    assert restores == []
    assert notifications == [("Fork", "information", f"Created fork {session_short_id('fork-window')}")]
    assert loading == [True, False, False]
    assert launcher_calls == [("fork-window", "/workspace")]


def test_on_session_forked_new_window_launcher_failure_warns(monkeypatch: pytest.MonkeyPatch) -> None:
    pushes: list[object] = []
    callbacks: list[object] = []
    notifications: list[tuple[str, str, str]] = []
    loading: list[bool] = []

    class _FakeStatusBar:
        def flash(self, _message: str) -> None:
            raise AssertionError("status flash should not run on launcher failure")

    def query_one(cls: type) -> object:
        if cls.__name__ == "ChatPanel":
            return SimpleNamespace(session_id="current-session")
        if cls.__name__ == "StatusBar":
            return _FakeStatusBar()
        raise AssertionError(cls)

    def push_screen(dialog: object, callback=None) -> None:
        pushes.append(dialog)
        callbacks.append(callback)

    monkeypatch.setenv("SSH_TTY", "/dev/pts/1")
    from chrys.app.tui.terminal import launcher

    with pytest.raises(launcher.TerminalLaunchError) as captured:
        launcher.launch_new_chrys_window("probe")

    def raise_launch_error(_session_id: str, *, cwd: str | None = None) -> None:
        del cwd
        raise captured.value

    monkeypatch.setattr("chrys.app.tui.terminal.launcher.launch_new_chrys_window", raise_launch_error)
    screen = SimpleNamespace(
        app=SimpleNamespace(push_screen=push_screen),
        query_one=query_one,
        notify=lambda message, *, title, severity="information", **_kwargs: notifications.append(
            (title, severity, message)
        ),
        _set_agent_loading=loading.append,
        _state=main_screen_state_at("/workspace"),
        _debug=lambda *_args: None,
    )
    handler = make_session_handler(screen, locale_controller=LocaleController(Settings(locale="zh-Hans")))

    asyncio.run(handler._open_session_fork_dialog("current-session"))
    asyncio.run(
        handler.on_session_forked(
            SessionForked(
                session_id="current-session",
                parent_session_id="current-session",
                new_session_id="fork-window",
            )
        )
    )
    callbacks[0]("new_window")

    assert notifications == [
        ("Fork", "information", f"Created fork {session_short_id('fork-window')}"),
        ("Fork", "warning", f"当前环境无法打开新的 {APP_DISPLAY_NAME} 窗口。"),
    ]
    assert loading == [True, False, False]


def test_session_fork_error_updates_pending_dialog() -> None:
    pushes: list[object] = []
    callbacks: list[object] = []
    flashes: list[tuple[str, bool]] = []
    notifications: list[tuple[str, str, str]] = []
    loading: list[bool] = []
    unlocked: list[None] = []

    class _FakeStatusBar:
        def flash(self, message: str, *, error: bool = False) -> None:
            flashes.append((message, error))

    class _FakeInputBar:
        locked = True

        def unlock_and_keep(self) -> None:
            self.locked = False
            unlocked.append(None)

    input_bar = _FakeInputBar()

    def query_one(cls: type) -> object:
        if cls.__name__ == "StatusBar":
            return _FakeStatusBar()
        if cls.__name__ == "InputBar":
            return input_bar
        raise AssertionError(cls)

    def push_screen(dialog: object, callback=None) -> None:
        pushes.append(dialog)
        callbacks.append(callback)

    def set_agent_loading(value: bool) -> None:
        loading.append(value)

    screen = SimpleNamespace(
        app=SimpleNamespace(push_screen=push_screen),
        query_one=query_one,
        notify=lambda message, *, title, severity, **_kwargs: notifications.append((title, severity, message)),
        _set_agent_loading=set_agent_loading,
    )
    handler = make_session_handler(screen)

    asyncio.run(handler._open_session_fork_dialog("current-session"))
    assert pushes[0]._state == "loading"

    handler.on_session_fork_error(
        Error(session_id="current-session", code="session_fork_busy", message="Session is busy."),
        message="Session is busy.",
        severity="warning",
    )

    assert pushes[0]._state == "error"
    assert loading == [True, False]
    assert [(status_text(message), error) for message, error in flashes] == [("Fork: Session is busy.", False)]
    assert notifications == [("Fork", "warning", "Session is busy.")]
    assert unlocked == [None]

    callbacks[0](None)
    assert loading == [True, False, False]


def _snapshot_tracker(tmp_path: Path) -> tuple[JsonFileStateStore, MutationTracker]:
    """Create a saved "sid" session plus a tracker writing into its snapshot store."""
    store = JsonFileStateStore(tmp_path / "sessions")
    asyncio.run(store.save_session("sid", {"messages": [], "compressed_msgs": []}))
    tracker = MutationTracker(SnapshotStore(store.session_dir("sid")))
    tracker.start_turn(1)
    return store, tracker


def _record_edit(tracker: MutationTracker, target: Path, before: str, after: str, call_id: str) -> None:
    """Record one edit_file MODIFY mutation with the given before/after texts."""
    target.write_text(before, encoding="utf-8")
    mutation = tracker.record(str(target), MutationOp.MODIFY, MutationSource.EDIT_FILE, call_id)
    assert mutation is not None
    target.write_text(after, encoding="utf-8")
    tracker.record_after(mutation)


def test_load_file_edit_snapshots_uses_loaded_state_not_primary_session_file(tmp_path: Path) -> None:
    store, tracker = _snapshot_tracker(tmp_path)
    _record_edit(tracker, tmp_path / "work.py", "before", "after", "runtime-call")
    recovered_state = {"chrys_mutations": tracker.serialize()}
    messages = [
        {
            "role": "assistant",
            "contents": [{"type": "function_call", "name": "edit_file", "call_id": "fw-call"}],
        }
    ]
    screen = SimpleNamespace(_services=MainScreenServices(bus=EventBus(), state_store=store))

    snapshots = make_session_handler(screen).load_file_edit_snapshots("sid", messages, recovered_state)

    assert snapshots == {"fw-call": [("before", "after")]}


def test_load_file_edit_snapshots_skips_marker_carried_file_calls(tmp_path: Path) -> None:
    # The tracker records snapshots only for executed calls; a marker-carried
    # file call is chrome that replay never renders, so it must not shift the
    # positional call↔snapshot zip away from the visible real call.
    store, tracker = _snapshot_tracker(tmp_path)
    _record_edit(tracker, tmp_path / "work.py", "before", "after", "runtime-call")
    recovered_state = {"chrys_mutations": tracker.serialize()}
    messages = [
        {
            "role": "assistant",
            "contents": [{"type": "function_call", "name": "edit_file", "call_id": "hidden"}],
            "additional_properties": {HistoryMarkerKind.KEY: HistoryMarkerKind.INTERRUPTED},
        },
        {
            "role": "assistant",
            "contents": [{"type": "function_call", "name": "edit_file", "call_id": "fw-call"}],
        },
    ]
    screen = SimpleNamespace(_services=MainScreenServices(bus=EventBus(), state_store=store))

    snapshots = make_session_handler(screen).load_file_edit_snapshots("sid", messages, recovered_state)

    assert snapshots == {"fw-call": [("before", "after")]}


def test_load_file_edit_snapshots_externalizes_large_snapshot(tmp_path: Path) -> None:
    store, tracker = _snapshot_tracker(tmp_path)
    large_after = "x" * (file_snapshot_inline_char_limit() + 1)
    _record_edit(tracker, tmp_path / "large.py", "before", large_after, "runtime-call")
    recovered_state = {"chrys_mutations": tracker.serialize()}
    messages = [
        {
            "role": "assistant",
            "contents": [{"type": "function_call", "name": "edit_file", "call_id": "fw-call"}],
        }
    ]
    screen = SimpleNamespace(_services=MainScreenServices(bus=EventBus(), state_store=store))

    snapshots = make_session_handler(screen).load_file_edit_snapshots("sid", messages, recovered_state)

    payload = snapshots["fw-call"][0]
    assert isinstance(payload, FileSnapshotRef)
    assert payload.resolve() == ("before", large_after)


def test_load_file_edit_snapshots_truthy_unhashable_id_discards_all_buckets(tmp_path: Path) -> None:
    """A truthy unhashable snapshot call id aborts the whole load fail-soft:
    every bucket is discarded, including valid ones."""
    store, tracker = _snapshot_tracker(tmp_path)
    for index in range(2):
        _record_edit(tracker, tmp_path / f"work{index}.py", "before", "after", f"runtime-{index}")
    recovered_state = {"chrys_mutations": tracker.serialize()}
    messages = [
        {
            "role": "assistant",
            "contents": [
                {"type": "function_call", "name": "edit_file", "call_id": "fw-call"},
                {"type": "function_call", "name": "edit_file", "call_id": ["x"]},
            ],
        }
    ]
    screen = SimpleNamespace(_services=MainScreenServices(bus=EventBus(), state_store=store))

    snapshots = make_session_handler(screen).load_file_edit_snapshots("sid", messages, recovered_state)

    assert snapshots == {}


def test_load_file_edit_snapshots_falsy_unhashable_id_skipped_buckets_kept(tmp_path: Path) -> None:
    """A falsy unhashable snapshot call id fails the truthiness gate and is
    skipped; valid buckets survive."""
    store, tracker = _snapshot_tracker(tmp_path)
    _record_edit(tracker, tmp_path / "work.py", "before", "after", "runtime-call")
    recovered_state = {"chrys_mutations": tracker.serialize()}
    messages = [
        {
            "role": "assistant",
            "contents": [
                {"type": "function_call", "name": "edit_file", "call_id": []},
                {"type": "function_call", "name": "edit_file", "call_id": "fw-call"},
            ],
        }
    ]
    screen = SimpleNamespace(_services=MainScreenServices(bus=EventBus(), state_store=store))

    snapshots = make_session_handler(screen).load_file_edit_snapshots("sid", messages, recovered_state)

    assert snapshots == {"fw-call": [("before", "after")]}
