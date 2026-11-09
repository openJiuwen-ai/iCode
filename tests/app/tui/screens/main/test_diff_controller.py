# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Async loading tests for the main-screen diff controller."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import create_autospec

import pytest

from chrys.app.tui.screens.main.diff_controller import (
    DiffController,
    LiveDiffOwner,
    LiveDiffTracker,
    _lifecycle_task_completed_successfully,
)
from chrys.app.tui.screens.main.live_diff import LiveFileMutation
from chrys.app.tui.screens.main.state import MainScreenServices, MainScreenState
from chrys.app.tui.screens.main.view_adapter import MainScreenViewAdapter
from chrys.app.tui.util.diff_entries import DiffFileEntry, DiffFocus, DiffLoadResult
from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import InvocationToolCallResult
from chrys.foundation.models.mutation_scope import ChatTurnScope
from chrys.service.mutations.tracker import MutationTracker
from chrys.service.mutations.types import MutationOp, TurnMutations
from tests.support.invocation_events import ORIGINS
from tests.support.tui_helpers import make_live_mutation


def _tracker_with_two_turns() -> MutationTracker:
    tracker = create_autospec(MutationTracker, instance=True, spec_set=True)
    period = TurnMutations(turn_id=77)
    tracker.get_all_turns.return_value = [TurnMutations(turn_id=11), period]
    tracker.current_period = period
    return tracker


def test_file_result_uses_recorded_change_time_instead_of_result_delivery_time(tmp_path: Path) -> None:
    workspace = tmp_path.resolve()
    live = LiveDiffTracker()
    live.record_tool_start("edit", "edit_file", {"path": "file.py"}, workspace_cwd=str(workspace))
    written_at = 123.25
    event = InvocationToolCallResult(
        origin=ORIGINS[0],
        timestamp=datetime(2026, 1, 1, tzinfo=UTC),
        call_id="edit",
        tool_name="edit_file",
        metadata={"file_snapshot": ("before", "after"), "file_changed_at": written_at},
    )

    assert live.record_tool_result(event)
    mutation = live.snapshot_file_mutations()[str(workspace / "file.py")]
    assert mutation.last_changed_at == written_at
    assert mutation.last_changed_at != event.timestamp.timestamp()


def _entry(
    *,
    path: str = "/repo/async.py",
    rel_path: str = "async.py",
    before_text: str = "before\n",
    after_text: str = "after\n",
) -> DiffFileEntry:
    return DiffFileEntry(
        path=path,
        rel_path=rel_path,
        operation=MutationOp.MODIFY,
        old_path=None,
        before_text=before_text,
        after_text=after_text,
        is_binary=False,
        encoding="utf-8",
        bytes_changed=True,
    )


class _DiffView:
    def __init__(self) -> None:
        self.load_data: Callable[[], Awaitable[DiffLoadResult]] | None = None
        self.opened = False

    def current_chat_session_id(self) -> str:
        return "session-1"

    def notify(self, *_args: object, **_kwargs: object) -> None:
        raise AssertionError("the controller should open the loading screen before it knows the result")

    def open_diff_screen(
        self,
        _periods_data: dict[int, list[object]],
        *,
        cwd: str,
        subtitle_parts: tuple[str, ...],
        session_id: str,
        all_entries: list[object],
        load_data: Callable[[], Awaitable[DiffLoadResult]] | None = None,
        focus: DiffFocus | None = None,
    ) -> None:
        assert cwd == "/repo"
        assert session_id == "session-1"
        assert all_entries == []
        assert isinstance(subtitle_parts, tuple)
        self.opened = True
        self.load_data = load_data


def test_show_diff_pushes_first_and_loads_persisted_entries_off_event_loop(monkeypatch) -> None:
    import chrys.app.tui.screens.diff as diff_package

    load_thread_ids: list[int] = []
    result = DiffLoadResult(all_entries=[_entry()], per_period_entries={1: [_entry()]}, total_periods=1)

    def fake_load(_state_store: object, _session_id: str, _cwd: str) -> DiffLoadResult:
        load_thread_ids.append(threading.get_ident())
        return result

    monkeypatch.setattr(diff_package, "load_diff_entries_by_period", fake_load)
    view = _DiffView()
    controller = DiffController(
        services=MainScreenServices(bus=EventBus(), state_store=object()),
        live_diff=LiveDiffTracker(),
        view=view,
        workspace_cwd=lambda: "/repo",
        is_agent_running=lambda: False,
        run_generation=lambda: 0,
        session_generation=lambda: 1,
    )
    event_loop_thread_id = threading.get_ident()

    controller.show_diff()

    assert view.opened is True
    assert load_thread_ids == []
    assert view.load_data is not None

    loaded = asyncio.run(view.load_data())

    assert loaded == result
    assert len(load_thread_ids) == 1
    assert load_thread_ids[0] != event_loop_thread_id


def test_deferred_diff_keeps_request_time_live_turn_when_agent_finishes(monkeypatch) -> None:
    import chrys.app.tui.screens.diff as diff_package

    load_started = threading.Event()
    release_load = threading.Event()

    def fake_load(_state_store: object, _session_id: str, _cwd: str) -> DiffLoadResult:
        load_started.set()
        assert release_load.wait(timeout=2)
        return DiffLoadResult(all_entries=[], per_period_entries={}, total_periods=1)

    monkeypatch.setattr(diff_package, "load_diff_entries_by_period", fake_load)
    running = [True]
    live_diff = LiveDiffTracker(
        owner=LiveDiffOwner(session_id="session-1", session_generation=1, run_generation=1),
        file_mutations={
            "/repo/live.py": LiveFileMutation(
                before_text="before\n",
                after_text="after\n",
                operation="modify",
                bytes_changed=True,
            )
        },
    )
    engine = SimpleNamespace(
        mutation_coordinator=None,
        mutation_tracker=_tracker_with_two_turns(),
    )
    view = _DiffView()
    controller = DiffController(
        services=MainScreenServices(bus=EventBus(), state_store=object(), engine_provider=lambda: engine),
        live_diff=live_diff,
        view=view,
        workspace_cwd=lambda: "/repo",
        is_agent_running=lambda: running[0],
        run_generation=lambda: 1,
        session_generation=lambda: 1,
    )
    controller.show_diff()
    assert view.load_data is not None

    async def finish_during_load() -> DiffLoadResult:
        task = asyncio.create_task(view.load_data())
        assert await asyncio.to_thread(load_started.wait, 2)
        running[0] = False
        release_load.set()
        return await task

    loaded = asyncio.run(finish_during_load())

    assert [entry.path for entry in loaded.per_period_entries[2]] == ["/repo/live.py"]
    assert [entry.path for entry in loaded.all_entries] == ["/repo/live.py"]


def test_deferred_diff_does_not_duplicate_live_turn_already_persisted(monkeypatch) -> None:
    import chrys.app.tui.screens.diff as diff_package

    persisted_entry = _entry()
    persisted = DiffLoadResult(
        all_entries=[persisted_entry],
        per_period_entries={2: [persisted_entry]},
        total_periods=2,
    )
    monkeypatch.setattr(diff_package, "load_diff_entries_by_period", lambda *_args: persisted)
    live_diff = LiveDiffTracker(
        owner=LiveDiffOwner(session_id="session-1", session_generation=1, run_generation=1),
        file_mutations={
            persisted_entry.path: LiveFileMutation(
                before_text=persisted_entry.before_text,
                after_text=persisted_entry.after_text,
                operation="modify",
                bytes_changed=True,
            )
        },
    )
    engine = SimpleNamespace(
        mutation_coordinator=None,
        mutation_tracker=_tracker_with_two_turns(),
    )
    view = _DiffView()
    controller = DiffController(
        services=MainScreenServices(bus=EventBus(), state_store=object(), engine_provider=lambda: engine),
        live_diff=live_diff,
        view=view,
        workspace_cwd=lambda: "/repo",
        is_agent_running=lambda: True,
        run_generation=lambda: 1,
        session_generation=lambda: 1,
    )
    controller.show_diff()
    assert view.load_data is not None

    loaded = asyncio.run(view.load_data())

    assert loaded.per_period_entries == {2: [persisted_entry]}
    assert loaded.all_entries == [persisted_entry]


@pytest.mark.asyncio
async def test_equal_turn_count_waits_for_captured_lifecycle_then_reloads_final_persistence(monkeypatch) -> None:
    import chrys.app.tui.screens.diff as diff_package

    load_started = threading.Event()
    release_first_load = threading.Event()
    release_finalization = asyncio.Event()
    checkpoint_entry = _entry(after_text="checkpoint\n")
    persisted_entry = _entry(after_text="final\n")
    persisted = [
        DiffLoadResult(all_entries=[checkpoint_entry], per_period_entries={2: [checkpoint_entry]}, total_periods=2)
    ]
    load_calls = 0

    def fake_load(_state_store: object, _session_id: str, _cwd: str) -> DiffLoadResult:
        nonlocal load_calls
        load_calls += 1
        if load_calls == 1:
            load_started.set()
            assert release_first_load.wait(timeout=2)
        return persisted[0]

    monkeypatch.setattr(diff_package, "load_diff_entries_by_period", fake_load)
    running = [True]
    live_diff = LiveDiffTracker(
        owner=LiveDiffOwner(session_id="session-1", session_generation=1, run_generation=7),
        file_mutations={
            persisted_entry.path: LiveFileMutation(
                before_text=persisted_entry.before_text,
                after_text=persisted_entry.after_text,
                operation="modify",
                bytes_changed=True,
            )
        },
    )

    async def finalize_and_save() -> None:
        await release_finalization.wait()
        persisted[0] = DiffLoadResult(
            all_entries=[persisted_entry],
            per_period_entries={2: [persisted_entry]},
            total_periods=2,
        )

    lifecycle_task = asyncio.create_task(finalize_and_save())
    engine = SimpleNamespace(
        mutation_coordinator=None,
        mutation_tracker=_tracker_with_two_turns(),
    )
    view = _DiffView()
    controller = DiffController(
        services=MainScreenServices(bus=EventBus(), state_store=object(), engine_provider=lambda: engine),
        live_diff=live_diff,
        view=view,
        workspace_cwd=lambda: "/repo",
        is_agent_running=lambda: running[0],
        run_generation=lambda: 7,
        session_generation=lambda: 1,
        turn_lifecycle_task=lambda: lifecycle_task,
        turn_lifecycle_saved=lambda task: task is lifecycle_task,
    )
    controller.show_diff()
    assert view.load_data is not None

    load_task = asyncio.create_task(view.load_data())
    assert await asyncio.to_thread(load_started.wait, 2)
    running[0] = False
    release_first_load.set()
    await asyncio.sleep(0.05)
    assert load_task.done() is False

    release_finalization.set()
    loaded = await load_task

    assert lifecycle_task.done() is True
    assert load_calls == 2
    assert loaded.per_period_entries == {2: [persisted_entry]}
    assert loaded.all_entries == [persisted_entry]


@pytest.mark.parametrize(
    ("owner_session_id", "owner_session_generation"),
    [("session-old", 2), ("session-1", 1)],
)
def test_live_mutations_are_not_overlaid_across_session_or_rollback_boundaries(
    monkeypatch,
    owner_session_id: str,
    owner_session_generation: int,
) -> None:
    import chrys.app.tui.screens.diff as diff_package

    new_entry = _entry(path="/repo/new.py", rel_path="new.py", after_text="new\n")
    persisted = DiffLoadResult(all_entries=[new_entry], per_period_entries={1: [new_entry]}, total_periods=1)
    monkeypatch.setattr(diff_package, "load_diff_entries_by_period", lambda *_args: persisted)
    live_diff = LiveDiffTracker(
        owner=LiveDiffOwner(
            session_id=owner_session_id,
            session_generation=owner_session_generation,
            run_generation=4,
        ),
        file_mutations={
            "/repo/old.py": LiveFileMutation(
                before_text="before\n",
                after_text="old\n",
                operation="modify",
                bytes_changed=True,
            )
        },
    )
    view = _DiffView()
    controller = DiffController(
        services=MainScreenServices(bus=EventBus(), state_store=object()),
        live_diff=live_diff,
        view=view,
        workspace_cwd=lambda: "/repo",
        is_agent_running=lambda: False,
        run_generation=lambda: 4,
        session_generation=lambda: 2,
    )

    controller.show_diff()
    assert view.load_data is not None
    loaded = asyncio.run(view.load_data())

    assert loaded == persisted
    assert [entry.path for entry in loaded.all_entries] == ["/repo/new.py"]


@pytest.mark.asyncio
async def test_completed_lifecycle_without_successful_save_keeps_live_snapshot(monkeypatch) -> None:
    import chrys.app.tui.screens.diff as diff_package

    checkpoint_entry = _entry(after_text="checkpoint\n")
    persisted = DiffLoadResult(
        all_entries=[checkpoint_entry],
        per_period_entries={2: [checkpoint_entry]},
        total_periods=2,
    )
    load_calls = 0

    def fake_load(*_args: object) -> DiffLoadResult:
        nonlocal load_calls
        load_calls += 1
        return persisted

    monkeypatch.setattr(diff_package, "load_diff_entries_by_period", fake_load)
    lifecycle_task = asyncio.create_task(asyncio.sleep(0))
    await lifecycle_task
    live_diff = LiveDiffTracker(
        owner=LiveDiffOwner(session_id="session-1", session_generation=1, run_generation=7),
        file_mutations={
            checkpoint_entry.path: LiveFileMutation(
                before_text=checkpoint_entry.before_text,
                after_text="final\n",
                operation="modify",
                bytes_changed=True,
            )
        },
    )
    engine = SimpleNamespace(
        mutation_coordinator=None,
        mutation_tracker=_tracker_with_two_turns(),
    )
    view = _DiffView()
    controller = DiffController(
        services=MainScreenServices(bus=EventBus(), state_store=object(), engine_provider=lambda: engine),
        live_diff=live_diff,
        view=view,
        workspace_cwd=lambda: "/repo",
        is_agent_running=lambda: False,
        run_generation=lambda: 7,
        session_generation=lambda: 1,
        turn_lifecycle_task=lambda: lifecycle_task,
        turn_lifecycle_saved=lambda _task: False,
    )

    controller.show_diff()
    assert view.load_data is not None
    loaded = await view.load_data()

    assert load_calls == 1
    assert loaded.per_period_entries[2][0].after_text == "final\n"
    assert loaded.all_entries[0].after_text == "final\n"


@pytest.mark.asyncio
async def test_successful_save_remains_authoritative_when_after_hook_fails(monkeypatch) -> None:
    import chrys.app.tui.screens.diff as diff_package

    checkpoint_entry = _entry(after_text="checkpoint\n")
    final_entry = _entry(after_text="final\n")
    persisted = [
        DiffLoadResult(
            all_entries=[checkpoint_entry],
            per_period_entries={2: [checkpoint_entry]},
            total_periods=2,
        ),
        DiffLoadResult(all_entries=[final_entry], per_period_entries={2: [final_entry]}, total_periods=2),
    ]
    load_calls = 0

    def fake_load(*_args: object) -> DiffLoadResult:
        nonlocal load_calls
        result = persisted[min(load_calls, 1)]
        load_calls += 1
        return result

    async def fail_after_save() -> None:
        raise RuntimeError("after-turn hook failed")

    monkeypatch.setattr(diff_package, "load_diff_entries_by_period", fake_load)
    lifecycle_task = asyncio.create_task(fail_after_save())
    live_diff = LiveDiffTracker(
        owner=LiveDiffOwner(session_id="session-1", session_generation=1, run_generation=7),
        file_mutations={
            final_entry.path: LiveFileMutation(
                before_text=final_entry.before_text,
                after_text="captured-before-save\n",
                operation="modify",
                bytes_changed=True,
            )
        },
    )
    engine = SimpleNamespace(
        mutation_coordinator=None,
        mutation_tracker=_tracker_with_two_turns(),
    )
    view = _DiffView()
    controller = DiffController(
        services=MainScreenServices(bus=EventBus(), state_store=object(), engine_provider=lambda: engine),
        live_diff=live_diff,
        view=view,
        workspace_cwd=lambda: "/repo",
        is_agent_running=lambda: False,
        run_generation=lambda: 7,
        session_generation=lambda: 1,
        turn_lifecycle_task=lambda: lifecycle_task,
        turn_lifecycle_saved=lambda task: task is lifecycle_task,
    )

    controller.show_diff()
    assert view.load_data is not None
    loaded = await view.load_data()

    assert load_calls == 2
    assert loaded == persisted[1]


@pytest.mark.asyncio
async def test_lifecycle_wait_timeout_keeps_task_alive_for_live_overlay() -> None:
    release = asyncio.Event()

    async def slow_lifecycle() -> None:
        await release.wait()

    lifecycle_task = asyncio.create_task(slow_lifecycle())
    try:
        assert (
            await _lifecycle_task_completed_successfully(
                lifecycle_task,
                timeout_seconds=0.01,
            )
            is False
        )
        assert lifecycle_task.done() is False
        assert lifecycle_task.cancelled() is False
    finally:
        release.set()
        await lifecycle_task


@pytest.mark.asyncio
async def test_lifecycle_wait_propagates_caller_cancellation_when_both_tasks_cancel() -> None:
    lifecycle_started = asyncio.Event()

    async def lifecycle() -> None:
        lifecycle_started.set()
        await asyncio.Event().wait()

    lifecycle_task = asyncio.create_task(lifecycle())
    waiter = asyncio.create_task(_lifecycle_task_completed_successfully(lifecycle_task))
    await lifecycle_started.wait()
    lifecycle_task.cancel()
    waiter.cancel()

    with pytest.raises(asyncio.CancelledError):
        await waiter


@pytest.mark.asyncio
async def test_lifecycle_wait_treats_lifecycle_only_cancellation_as_incomplete() -> None:
    lifecycle_task = asyncio.create_task(asyncio.sleep(10))
    lifecycle_task.cancel()

    assert await _lifecycle_task_completed_successfully(lifecycle_task) is False


def test_equal_turn_count_without_completed_persistence_overlays_captured_live_rows() -> None:
    checkpoint_entry = _entry(after_text="checkpoint\n")
    persisted_only = _entry(
        path="/repo/persisted-only.py",
        rel_path="persisted-only.py",
        after_text="persisted\n",
    )
    persisted = DiffLoadResult(
        all_entries=[checkpoint_entry, persisted_only],
        per_period_entries={2: [checkpoint_entry, persisted_only]},
        total_periods=2,
    )
    controller = DiffController(
        services=MainScreenServices(bus=EventBus()),
        live_diff=LiveDiffTracker(),
        view=_DiffView(),
        workspace_cwd=lambda: "/repo",
        is_agent_running=lambda: False,
        run_generation=lambda: 1,
        session_generation=lambda: 1,
    )

    loaded = controller._merge_live_diff(
        persisted,
        "/repo",
        {
            checkpoint_entry.path: LiveFileMutation(
                before_text=checkpoint_entry.before_text,
                after_text="final\n",
                operation="modify",
                bytes_changed=True,
            )
        },
        2,
        False,
        ChatTurnScope(77),
    )

    by_path = {entry.path: entry for entry in loaded.per_period_entries[2]}
    assert loaded.scopes[2] == ChatTurnScope(77)
    assert by_path[checkpoint_entry.path].after_text == "final\n"
    assert by_path[persisted_only.path] == persisted_only


def test_equal_turn_count_prefers_disk_only_with_completed_persistence_boundary() -> None:
    persisted_entry = _entry(after_text="final\n")
    persisted = DiffLoadResult(
        all_entries=[persisted_entry],
        per_period_entries={2: [persisted_entry]},
        total_periods=2,
    )
    controller = DiffController(
        services=MainScreenServices(bus=EventBus()),
        live_diff=LiveDiffTracker(),
        view=_DiffView(),
        workspace_cwd=lambda: "/repo",
        is_agent_running=lambda: False,
        run_generation=lambda: 1,
        session_generation=lambda: 1,
    )

    loaded = controller._merge_live_diff(
        persisted,
        "/repo",
        {
            persisted_entry.path: LiveFileMutation(
                before_text=persisted_entry.before_text,
                after_text="captured-before-final\n",
                operation="modify",
                bytes_changed=True,
            )
        },
        2,
        True,
    )

    assert loaded == persisted


def test_equal_turn_count_overlays_fresher_live_rows_on_partial_persistence() -> None:
    stale_entry = _entry(after_text="stale\n")
    persisted_only = _entry(
        path="/repo/persisted-only.py",
        rel_path="persisted-only.py",
        after_text="persisted\n",
    )
    persisted = DiffLoadResult(
        all_entries=[stale_entry, persisted_only],
        per_period_entries={2: [stale_entry, persisted_only]},
        total_periods=2,
    )
    controller = DiffController(
        services=MainScreenServices(bus=EventBus()),
        live_diff=LiveDiffTracker(),
        view=_DiffView(),
        workspace_cwd=lambda: "/repo",
        is_agent_running=lambda: True,
        run_generation=lambda: 1,
        session_generation=lambda: 1,
    )

    loaded = controller._merge_live_diff(
        persisted,
        "/repo",
        {
            stale_entry.path: LiveFileMutation(
                before_text=stale_entry.before_text,
                after_text="fresh\n",
                operation="modify",
                bytes_changed=True,
            )
        },
        2,
        False,
    )

    by_path = {entry.path: entry for entry in loaded.per_period_entries[2]}
    assert by_path[stale_entry.path].after_text == "fresh\n"
    assert by_path[persisted_only.path] == persisted_only
    all_by_path = {entry.path: entry for entry in loaded.all_entries}
    assert all_by_path[stale_entry.path].after_text == "fresh\n"


def test_live_overlay_preserves_persisted_only_row_metadata() -> None:
    persisted_entry = DiffFileEntry(
        path="/repo/moved.py",
        rel_path="moved.py",
        operation=MutationOp.MOVE,
        old_path="/repo/old.py",
        before_text="before\n",
        after_text="checkpoint\n",
        is_binary=True,
        encoding="latin-1",
        bytes_changed=True,
    )
    persisted = DiffLoadResult(
        all_entries=[persisted_entry],
        per_period_entries={2: [persisted_entry]},
        total_periods=2,
    )
    controller = DiffController(
        services=MainScreenServices(bus=EventBus()),
        live_diff=LiveDiffTracker(),
        view=_DiffView(),
        workspace_cwd=lambda: "/repo",
        is_agent_running=lambda: True,
        run_generation=lambda: 1,
        session_generation=lambda: 1,
    )

    loaded = controller._merge_live_diff(
        persisted,
        "/repo",
        {
            persisted_entry.path: LiveFileMutation(
                before_text="before\n",
                after_text="live\n",
                operation="modify",
                bytes_changed=True,
            )
        },
        2,
        False,
    )

    overlaid = loaded.per_period_entries[2][0]
    assert overlaid.after_text == "live\n"
    assert overlaid.old_path == "/repo/old.py"
    assert overlaid.is_binary is True
    assert overlaid.encoding == "latin-1"


def test_known_new_live_turn_is_not_deduplicated_against_identical_prior_turn() -> None:
    persisted_entry = _entry()
    persisted = DiffLoadResult(
        all_entries=[persisted_entry],
        per_period_entries={1: [persisted_entry]},
        total_periods=1,
    )
    controller = DiffController(
        services=MainScreenServices(bus=EventBus()),
        live_diff=LiveDiffTracker(),
        view=_DiffView(),
        workspace_cwd=lambda: "/repo",
        is_agent_running=lambda: True,
        run_generation=lambda: 1,
        session_generation=lambda: 1,
    )

    loaded = controller._merge_live_diff(
        persisted,
        "/repo",
        {
            persisted_entry.path: LiveFileMutation(
                before_text=persisted_entry.before_text,
                after_text=persisted_entry.after_text,
                operation="modify",
                bytes_changed=True,
            )
        },
        2,
        True,
    )

    assert loaded.per_period_entries == {1: [persisted_entry], 2: [persisted_entry]}
    assert loaded.all_entries == [persisted_entry]


def test_persistence_newer_than_captured_live_turn_remains_authoritative() -> None:
    persisted_entry = _entry(after_text="final\n")
    persisted = DiffLoadResult(
        all_entries=[persisted_entry],
        per_period_entries={3: [persisted_entry]},
        total_periods=3,
    )
    controller = DiffController(
        services=MainScreenServices(bus=EventBus()),
        live_diff=LiveDiffTracker(),
        view=_DiffView(),
        workspace_cwd=lambda: "/repo",
        is_agent_running=lambda: True,
        run_generation=lambda: 1,
        session_generation=lambda: 1,
    )

    loaded = controller._merge_live_diff(
        persisted,
        "/repo",
        {
            persisted_entry.path: LiveFileMutation(
                before_text="before\n",
                after_text="captured-stale\n",
                operation="modify",
                bytes_changed=True,
            )
        },
        2,
        True,
    )

    assert loaded == persisted


def _show_diff_for_test_screen(
    screen: SimpleNamespace,
    *,
    state_store: object | None,
    agent_running: bool,
    file_mutations: dict[str, LiveFileMutation],
) -> None:
    session_id = screen.query_one(object).session_id
    live_diff = LiveDiffTracker(
        owner=LiveDiffOwner(session_id=session_id, session_generation=0, run_generation=0),
        file_mutations=file_mutations,
    )
    controller = DiffController(
        services=MainScreenServices(bus=EventBus(), state_store=state_store),
        live_diff=live_diff,
        view=MainScreenViewAdapter(screen, state=MainScreenState()),  # type: ignore[arg-type]
        workspace_cwd=lambda: "/repo",
        is_agent_running=lambda: agent_running,
        run_generation=lambda: 0,
        session_generation=lambda: 0,
    )
    controller.show_diff()


def test_action_show_diff_opens_when_only_all_entries_survive(monkeypatch) -> None:
    import chrys.app.tui.screens.diff as diff_pkg
    from chrys.app.tui.screens.diff import DiffFileEntry, DiffLoadResult, DiffScreen

    seen_cwds: list[str] = []
    entry = DiffFileEntry(
        path="/repo/Power.yaml",
        rel_path="Power.yaml",
        operation=MutationOp.MODIFY,
        old_path=None,
        before_text="name: Power\n",
        after_text="name: Power\r\n",
        is_binary=False,
        encoding="utf-8",
    )

    def fake_load_diff_entries_by_period(_state_store, _session_id, cwd):
        seen_cwds.append(cwd)
        return DiffLoadResult(all_entries=[entry], per_period_entries={}, total_periods=1)

    monkeypatch.setattr(diff_pkg, "load_diff_entries_by_period", fake_load_diff_entries_by_period)
    pushed: list[object] = []
    notifications: list[tuple[tuple, dict]] = []
    screen = SimpleNamespace(
        app=SimpleNamespace(push_screen=pushed.append),
        query_one=lambda _cls: SimpleNamespace(session_id="session-1"),
        notify=lambda *args, **kwargs: notifications.append((args, kwargs)),
    )

    _show_diff_for_test_screen(screen, state_store=object(), agent_running=False, file_mutations={})

    assert notifications == []
    assert len(pushed) == 1
    assert isinstance(pushed[0], DiffScreen)
    loader = pushed[0]._load_data
    assert loader is not None
    result = asyncio.run(loader())

    assert result.per_period_entries == {}
    assert result.all_entries == [entry]
    assert seen_cwds == ["/repo"]


def test_action_show_diff_keeps_live_metadata_only_change() -> None:
    from chrys.app.tui.screens.diff import DiffScreen

    pushed: list[object] = []
    notifications: list[tuple[tuple, dict]] = []
    screen = SimpleNamespace(
        app=SimpleNamespace(push_screen=pushed.append),
        query_one=lambda _cls: SimpleNamespace(session_id=""),
        notify=lambda *args, **kwargs: notifications.append((args, kwargs)),
    )
    file_mutations = {
        "/repo/Power.yaml": make_live_mutation(
            "name: Power\n",
            "name: Power\n",
            "modify",
            bytes_changed=True,
            before_hash="hash-with-bom",
            after_hash="hash-without-bom",
        )
    }

    _show_diff_for_test_screen(screen, state_store=None, agent_running=True, file_mutations=file_mutations)

    assert notifications == []
    assert len(pushed) == 1
    assert isinstance(pushed[0], DiffScreen)
    loader = pushed[0]._load_data
    assert loader is not None
    result = asyncio.run(loader())
    entry = result.per_period_entries[1][0]
    assert entry.before_text == "name: Power\n"
    assert entry.after_text == "name: Power\n"
    assert entry.bytes_changed is True
    assert entry.before_hash == "hash-with-bom"
    assert entry.after_hash == "hash-without-bom"


def test_action_show_diff_drops_all_entry_when_live_change_restores_original_bytes(monkeypatch) -> None:
    import chrys.app.tui.screens.diff as diff_pkg
    from chrys.app.tui.screens.diff import DiffFileEntry, DiffLoadResult, DiffScreen

    persisted = DiffFileEntry(
        path="/repo/Power.yaml",
        rel_path="Power.yaml",
        operation=MutationOp.MODIFY,
        old_path=None,
        before_text="A\n",
        after_text="B\n",
        is_binary=False,
        encoding="utf-8",
        bytes_changed=True,
        before_hash="hash-A",
        after_hash="hash-B",
    )

    def fake_load_diff_entries_by_period(_state_store, _session_id, _cwd):
        return DiffLoadResult(all_entries=[persisted], per_period_entries={1: [persisted]}, total_periods=1)

    monkeypatch.setattr(diff_pkg, "load_diff_entries_by_period", fake_load_diff_entries_by_period)
    pushed: list[object] = []
    screen = SimpleNamespace(
        app=SimpleNamespace(push_screen=pushed.append),
        query_one=lambda _cls: SimpleNamespace(session_id="session-1"),
        notify=lambda *_args, **_kwargs: None,
    )
    file_mutations = {
        "/repo/Power.yaml": make_live_mutation(
            "B\n",
            "A\n",
            "modify",
            bytes_changed=True,
            before_hash="hash-B",
            after_hash="hash-A",
        ),
    }

    _show_diff_for_test_screen(screen, state_store=object(), agent_running=True, file_mutations=file_mutations)

    assert len(pushed) == 1
    assert isinstance(pushed[0], DiffScreen)
    loader = pushed[0]._load_data
    assert loader is not None
    result = asyncio.run(loader())

    assert result.all_entries == []
    assert result.per_period_entries[2][0].after_text == "A\n"


@pytest.mark.parametrize(
    ("coordinator", "expected_refreshes"),
    [
        pytest.param(object(), [False], id="coordinated"),
        pytest.param(None, [], id="uncoordinated"),
    ],
)
def test_show_diff_opens_loading_screen_before_attribution_refresh(
    coordinator: object | None,
    expected_refreshes: list[bool],
) -> None:
    """The loading screen opens immediately; only a coordinated engine refreshes attribution."""
    refreshed: list[bool] = []
    notified: list[str] = []
    pushed: list[object] = []

    class _FakeEngine:
        mutation_coordinator = coordinator

        async def refresh_mutation_attribution(self, *, force: bool = False) -> bool:
            refreshed.append(force)
            return False

    screen = SimpleNamespace(
        query_one=lambda _cls: SimpleNamespace(session_id=""),
        notify=lambda message, **_kwargs: notified.append(message),
        app=SimpleNamespace(push_screen=pushed.append),
    )
    controller = DiffController(
        services=MainScreenServices(bus=EventBus(), engine_provider=_FakeEngine),
        live_diff=LiveDiffTracker(),
        view=MainScreenViewAdapter(screen, state=MainScreenState()),  # type: ignore[arg-type]
        workspace_cwd=lambda: "/repo/workspace",
        is_agent_running=lambda: False,
        run_generation=lambda: 0,
        session_generation=lambda: 0,
    )

    controller.show_diff()

    assert len(pushed) == 1
    assert refreshed == []
    loader = pushed[0]._load_data
    assert loader is not None
    result = asyncio.run(loader())

    assert refreshed == expected_refreshes
    assert result == DiffLoadResult.empty()
    assert notified == []


async def test_already_saved_turn_reads_once_and_cached_refresh_stays_in_memory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def complete() -> None:
        return

    lifecycle = asyncio.create_task(complete())
    await lifecycle
    live = LiveDiffTracker(owner=LiveDiffOwner("session-1", 1, 7))
    live.accumulate_live_mutation("/repo/async.py", "before\n", "after\n")
    controller = DiffController(
        services=MainScreenServices(bus=EventBus()),
        live_diff=live,
        view=_DiffView(),
        workspace_cwd=lambda: "/repo",
        is_agent_running=lambda: False,
        run_generation=lambda: 7,
        session_generation=lambda: 1,
        turn_lifecycle_task=lambda: lifecycle,
        turn_lifecycle_saved=lambda task: task is lifecycle,
    )
    disk = DiffLoadResult([_entry()], {1: [_entry()]}, 1)
    read = create_autospec(controller._load_persisted_diff, return_value=disk)
    monkeypatch.setattr(controller, "_load_persisted_diff", read)
    assert (await controller.load_current()).merged == disk
    assert read.call_count == 1
    assert (await controller.load_current(persisted=disk)).merged == disk
    assert read.call_count == 1
