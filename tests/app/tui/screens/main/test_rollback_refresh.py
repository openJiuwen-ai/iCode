# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Retained rollback links survive unavailable engines and session transitions."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock

import pytest

from chrys.app.tui.screens.main.screen import MainScreen
from chrys.app.tui.widgets.chat.messages import MessageRollbackButton, UserMessage
from chrys.app.tui.widgets.chat.panel import ChatPanel
from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import RollbackSnapshotsChanged, SessionRestored
from chrys.kernel import Message
from tests.app.tui.screens.main.test_rollback_entries import _controller, _EntryEngine, _EntryView
from tests.support.tui_app_harness import ShutdownOnlyEngine, make_chrys_app


@pytest.fixture(autouse=True)
def no_native_branch_observer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "chrys.app.tui.util.git_branch.GitBranchMonitor._start_observer", lambda _monitor, _target: None
    )


@pytest.mark.parametrize("failure", ["no_engine", "provider", "targets"])
async def test_refresh_failure_keeps_existing_targets_and_only_logs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, failure: str
) -> None:
    from chrys.service.mutations.store import SnapshotStore
    from chrys.service.mutations.tracker import MutationTracker

    engine = _EntryEngine(MutationTracker(SnapshotStore(tmp_path)), str(tmp_path))
    view = _EntryView()
    view.targets = {2}
    controller = _controller(engine, view, EventBus())

    def unavailable() -> None:
        return None

    def broken_provider() -> None:
        raise OSError("engine unavailable")

    def broken_targets() -> list[int]:
        raise OSError("snapshot unavailable")

    if failure == "no_engine":
        monkeypatch.setattr(controller._services, "engine_provider", unavailable)
    elif failure == "provider":
        monkeypatch.setattr(controller._services, "engine_provider", broken_provider)
    else:
        monkeypatch.setattr(engine, "available_rollback_turns", broken_targets)
    await controller.refresh_rollback_targets()
    assert view.targets == {2}
    assert view.notices == []
    assert ("Unable to refresh rollback targets" in caplog.text) is (failure != "no_engine")


async def test_snapshot_refresh_events_ignore_other_sessions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bus = EventBus()
    app = make_chrys_app(tmp_path, event_bus=bus)
    async with app.run_test() as pilot:
        screen = app.screen
        assert isinstance(screen, MainScreen)
        screen.query_one(ChatPanel).set_session_id("current")
        refresh = Mock()
        monkeypatch.setattr(screen._rollback_controller, "refresh_rollback_targets", refresh)
        # Spy on the callback instead of starting a worker with an inert engine.
        from dataclasses import replace

        screen._events._callbacks = replace(screen._events._callbacks, refresh_rollback_targets=refresh)
        await bus.publish(RollbackSnapshotsChanged(session_id="other"))
        refresh.assert_not_called()
        await bus.publish(RollbackSnapshotsChanged(session_id="current"))
        refresh.assert_called_once_with()
        await pilot.pause()


@pytest.mark.parametrize("transition", ["clear", "new", "restore"])
async def test_session_transition_releases_pending_rollback_request(tmp_path: Path, transition: str) -> None:
    app = make_chrys_app(tmp_path)
    async with app.run_test() as pilot:
        screen = app.screen
        assert isinstance(screen, MainScreen)
        controller = screen._rollback_controller
        controller._entry_pending = object()
        if transition == "clear":
            await screen._sessions.delete_current_and_new("current")
        elif transition == "new":
            await screen._sessions.create_new_session()
        else:
            await screen._sessions.on_session_restored(
                SessionRestored(session_id="restored", primary_cwd=str(tmp_path))
            )
        assert controller._entry_pending is None
        await pilot.pause()


class _BrokenTargetEngine(ShutdownOnlyEngine):
    def available_rollback_turns(self) -> list[int]:
        raise OSError("target read failed")


@pytest.mark.parametrize("missing_engine", [False, True])
async def test_session_replay_survives_unavailable_rollback_target_projection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, missing_engine: bool
) -> None:
    app = make_chrys_app(tmp_path / "state", engine=_BrokenTargetEngine())
    async with app.run_test() as pilot:
        screen = app.screen
        assert isinstance(screen, MainScreen)
        store = screen._services.state_store
        assert store is not None
        await store.save_session("restored", {"messages": [Message("user", ["Kept question"])]})
        if missing_engine:
            monkeypatch.setattr(screen._services, "engine_provider", None)
        await screen._sessions.on_session_restored(SessionRestored(session_id="restored", primary_cwd=str(tmp_path)))
        opener = screen.query_one(UserMessage)
        assert opener.text == "Kept question"
        assert not opener.query_one(MessageRollbackButton).display
        assert ("Unable to load restored rollback targets" in caplog.text) is (not missing_engine)
        await pilot.pause()


@pytest.mark.parametrize("transition", ["continue", "empty-replacement"])
async def test_inflight_target_read_survives_a_new_pass_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, transition: str
) -> None:
    import asyncio
    import threading

    from chrys.service.mutations.store import SnapshotStore
    from chrys.service.mutations.tracker import MutationTracker
    from tests.support.tui_helpers import ChatPanelApp
    from tests.support.waiting import wait_for

    engine = _EntryEngine(MutationTracker(SnapshotStore(tmp_path)), str(tmp_path))
    entered, release = threading.Event(), threading.Event()

    def delayed_targets() -> list[int]:
        entered.set()
        assert release.wait(10)
        return [0, 1, 2]

    monkeypatch.setattr(engine, "available_rollback_turns", delayed_targets)
    async with ChatPanelApp().run_test() as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.add_user_message("Previous")
        panel.apply_turn_started(3)
        panel.set_rollback_targets({0})
        view = _EntryView()
        monkeypatch.setattr(view, "set_rollback_targets", panel.set_rollback_targets)
        controller = _controller(engine, view, EventBus())
        refresh = asyncio.create_task(controller.refresh_rollback_targets())
        try:
            await wait_for(entered.is_set, description="snapshot read held")
            engine.conversation_revision += 1
            if transition == "empty-replacement":
                await panel.add_user_message("Replacement")
                await panel.apply_fresh_turn_cleanup(retired_previous_turn=True, removed_item_ids=())
                panel.apply_turn_started(3)
            release.set()
            await refresh
            assert [message.text for message in panel.query(UserMessage)] == [
                "Replacement" if transition == "empty-replacement" else "Previous"
            ]
            assert panel.query_one(UserMessage).query_one(MessageRollbackButton).display
        finally:
            release.set()
            await refresh


async def test_later_target_refresh_wins_even_when_the_earlier_read_finishes_last(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio
    import threading

    from chrys.service.mutations.store import SnapshotStore
    from chrys.service.mutations.tracker import MutationTracker
    from tests.support.waiting import wait_for

    engine = _EntryEngine(MutationTracker(SnapshotStore(tmp_path)), str(tmp_path))
    entered, release = threading.Event(), threading.Event()
    reads = 0

    def targets() -> list[int]:
        nonlocal reads
        reads += 1
        if reads == 1:
            entered.set()
            assert release.wait(10)
            return [0, 1]
        return [2, 3]

    monkeypatch.setattr(engine, "available_rollback_turns", targets)
    view = _EntryView()
    controller = _controller(engine, view, EventBus())
    earlier = asyncio.create_task(controller.refresh_rollback_targets())
    try:
        await wait_for(entered.is_set, description="older snapshot read held")
        await controller.refresh_rollback_targets()
        assert view.targets == {2, 3}
        release.set()
        await earlier
        assert view.targets == {2, 3}
    finally:
        release.set()
        await earlier
