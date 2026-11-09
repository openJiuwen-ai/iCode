# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Rollback entry decisions follow targets, actual file plans and click-time revisions."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest
from textual.app import App, ComposeResult
from textual.widgets import Select, TextArea

from chrys.app.tui.screens.diff.rollback_modal import RollbackModal
from chrys.app.tui.screens.main.engine_read_model import RollbackReadState
from chrys.app.tui.screens.main.rollback_controller import RollbackController
from chrys.app.tui.screens.main.screen import MainScreen
from chrys.app.tui.screens.main.state import MainScreenServices
from chrys.app.tui.widgets.chrome.input_bar import InputBar
from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import RollbackResult, UserRollback
from chrys.foundation.i18n import MessageRef
from chrys.foundation.i18n.formatting import format_message
from chrys.service.mutations.store import SnapshotStore
from chrys.service.mutations.tracker import MutationTracker
from chrys.service.mutations.types import MutationOp, MutationSource
from tests.support.tui_app_harness import make_chrys_app
from tests.support.waiting import wait_for


class _EntryEngine:
    mutation_coordinator = None
    current_turn_number = 3
    conversation_revision = 7
    build_generation = 1

    def __init__(self, tracker: MutationTracker, cwd: str) -> None:
        self.mutation_tracker = tracker
        self.workspace_primary_cwd = cwd
        self.fenced = False
        self.target_threads: list[int] = []

    def available_rollback_turns(self) -> list[int]:
        self.target_threads.append(threading.get_ident())
        return list(range(self.current_turn_number))

    def turn_prompt_previews(self) -> dict[int, str]:
        return {}

    async def begin_rollback_projection(self, *, session_id: str | None, session_generation: int) -> str:
        assert session_id == "session-1"
        assert session_generation == 1
        assert self.fenced is False
        self.fenced = True
        return "entry-projection"

    def finish_rollback_projection(self, owner: str) -> None:
        assert owner == "entry-projection"
        assert self.fenced is True
        self.fenced = False


class _EntryView:
    def __init__(self) -> None:
        self.notices: list[str] = []
        self.progress: list[Callable[[], Awaitable[None]]] = []
        self.pickers: list[tuple[int | None, Callable[[], Awaitable[RollbackReadState | None]], object]] = []
        self.confirms: list[tuple[str, Callable[[bool], None]]] = []
        self.targets: set[int] = set()

    def notify(
        self,
        message: str | MessageRef,
        *,
        title: str | MessageRef,
        severity: str = "information",
        timeout: float | None = 3,
    ) -> None:
        self.notices.append(format_message(message) if isinstance(message, MessageRef) else message)

    def open_rollback_progress_modal(self, operation: Callable[[], Awaitable[None]]) -> None:
        self.progress.append(operation)

    def open_confirm_dialog(
        self,
        *,
        title: MessageRef,
        message: MessageRef,
        confirm_label: MessageRef,
        confirm_variant: str,
        on_result: Callable[[bool], None],
    ) -> None:
        assert confirm_variant == "error"
        self.confirms.append((format_message(message), on_result))

    def open_rollback_modal(
        self,
        *,
        cwd: str,
        load_state: Callable[[], Awaitable[RollbackReadState | None]],
        on_result: object,
        initial_selection: int | None = None,
    ) -> None:
        self.pickers.append((initial_selection, load_state, on_result))

    def set_rollback_targets(self, targets: set[int]) -> None:
        self.targets = targets


def _controller(
    engine: _EntryEngine,
    view: _EntryView,
    bus: EventBus,
    *,
    task: Callable[[], asyncio.Task[None] | None] = lambda: None,
    busy: Callable[[], bool] = lambda: False,
    running: Callable[[], bool] = lambda: False,
) -> RollbackController:
    return RollbackController(
        services=MainScreenServices(bus=bus, engine_provider=lambda: engine),
        view=view,
        workspace_cwd=lambda: engine.workspace_primary_cwd,
        is_agent_busy=busy,
        is_agent_running=running,
        current_session_id=lambda: "session-1",
        session_generation=lambda: 1,
        turn_lifecycle_task=task,
        profile_name=lambda: "Code",
        reset_welcome_workspace_marker=lambda _cwd: None,
        set_has_messages=lambda _value: None,
        post_gc_message=lambda _message: None,
        debug=lambda _kind, _detail: None,
        invalidate_file_index=lambda: None,
    )


@pytest.mark.parametrize("first_turn", [False, True])
@pytest.mark.parametrize("has_file", [False, True])
@pytest.mark.parametrize("older_turn", [False, True])
async def test_entry_confirms_exactly_when_files_or_multiple_turns_are_discarded(
    tmp_path: Path,
    first_turn: bool,
    has_file: bool,
    older_turn: bool,
) -> None:
    tracker = MutationTracker(SnapshotStore(tmp_path))
    current_turn = 1 if first_turn else 3
    tracker.start_turn(current_turn)
    if has_file:
        path = tmp_path / "edited.txt"
        path.write_text("before", encoding="utf-8")
        mutation = tracker.record(str(path), MutationOp.MODIFY, MutationSource.EDIT_FILE, "edit")
        assert mutation is not None
        path.write_text("after", encoding="utf-8")
        tracker.record_after(mutation)
    engine = _EntryEngine(tracker, str(tmp_path))
    engine.current_turn_number = current_turn
    target = max(0, current_turn - (2 if older_turn else 1))
    view = _EntryView()
    bus = EventBus()
    published: list[UserRollback] = []

    async def record(event: UserRollback) -> None:
        published.append(event)

    await bus.subscribe(UserRollback, record)
    complete: list[bool] = []
    controller = _controller(engine, view, bus)
    await controller.rollback_from_message(target, on_complete=lambda: complete.append(True))
    requires_picker = has_file or target < current_turn - 1
    # A header click always asks: the picker when there is something to choose, else a plain confirm.
    assert len(view.pickers) == int(requires_picker)
    assert len(view.confirms) == int(not requires_picker)
    assert view.progress == []
    assert complete == []
    assert all(thread != threading.get_ident() for thread in engine.target_threads)
    if requires_picker:
        initial, load, dismiss = view.pickers[0]
        assert initial == target
        state = await load()
        assert state is not None
        assert state.conversation_revision == 7
        dismiss(None)
        assert published == []
    else:
        message, confirm = view.confirms[0]
        assert message.startswith(f"Discard turn {target + 1}? It is the latest turn")
        confirm(True)
        assert len(view.progress) == 1
        assert complete == []
        await view.progress[0]()
        assert len(published) == 1
        assert published[0].target_turn == target
        assert published[0].expected_conversation_revision == 7
    assert complete == [True]


@pytest.mark.parametrize("clicked_turn", [2, None], ids=["message_header", "interrupt_card"])
async def test_cancelled_confirm_discards_nothing_and_releases_the_entry(
    tmp_path: Path, clicked_turn: int | None
) -> None:
    tracker = MutationTracker(SnapshotStore(tmp_path))
    tracker.start_turn(3)
    engine = _EntryEngine(tracker, str(tmp_path))
    view = _EntryView()
    bus = EventBus()
    published: list[UserRollback] = []

    async def record(event: UserRollback) -> None:
        published.append(event)

    await bus.subscribe(UserRollback, record)
    controller = _controller(engine, view, bus)
    complete: list[str] = []
    await controller.rollback_from_message(clicked_turn, on_complete=lambda: complete.append("first"))
    assert complete == []
    [(_message, answer)] = view.confirms
    answer(False)
    assert complete == ["first"]
    assert view.progress == []
    assert published == []
    # The cancelled click no longer holds the entry: the next click asks again.
    await controller.rollback_from_message(clicked_turn, on_complete=lambda: complete.append("second"))
    assert len(view.confirms) == 2
    assert view.notices == []


@pytest.mark.parametrize("detection_cut_short", [False, True], ids=["detected", "detection-cut-short"])
async def test_interrupt_card_discard_asks_before_it_discards(tmp_path: Path, detection_cut_short: bool) -> None:
    tracker = MutationTracker(SnapshotStore(tmp_path))
    tracker.start_turn(3)
    if detection_cut_short:
        # A failed scan may have missed files the turn changed: the prompt claims only what it can restore.
        tracker.mark_detection_truncated()
    engine = _EntryEngine(tracker, str(tmp_path))
    view = _EntryView()
    bus = EventBus()
    published: list[UserRollback] = []

    async def record(event: UserRollback) -> None:
        published.append(event)

    await bus.subscribe(UserRollback, record)
    complete: list[bool] = []
    await _controller(engine, view, bus).rollback_from_message(None, on_complete=lambda: complete.append(True))
    assert view.progress == []
    [(message, answer)] = view.confirms
    assert message == (
        "Discard turn 3? It is the latest turn and has no files to restore. A discarded turn cannot be recovered."
    )
    answer(True)
    await view.progress[0]()
    assert [(event.relative_turns, event.expected_conversation_revision) for event in published] == [(1, 7)]
    assert complete == [True]


@pytest.mark.parametrize("clicked_turn", [2, None], ids=["message_header", "interrupt_card"])
async def test_last_turn_with_only_unrestorable_changes_opens_the_dialog(
    tmp_path: Path, clicked_turn: int | None
) -> None:
    tracker = MutationTracker(SnapshotStore(tmp_path / "snapshots"))
    tracker.start_turn(3)
    path = tmp_path / "image.bin"
    # Binary content is withheld from snapshots, so rollback cannot restore it.
    path.write_bytes(b"\x00before")
    mutation = tracker.record(str(path), MutationOp.MODIFY, MutationSource.EDIT_FILE, "edit")
    assert mutation is not None
    path.write_bytes(b"\x00after")
    tracker.record_after(mutation)
    engine = _EntryEngine(tracker, str(tmp_path))
    view = _EntryView()
    bus = EventBus()
    published: list[UserRollback] = []

    async def record(event: UserRollback) -> None:
        published.append(event)

    await bus.subscribe(UserRollback, record)
    await _controller(engine, view, bus).rollback_from_message(clicked_turn, on_complete=lambda: None)
    assert view.progress == []
    assert [initial for initial, _load, _dismiss in view.pickers] == [2]
    assert published == []


@pytest.mark.parametrize("entry", ["message", "relative", "absolute", "picker"])
async def test_entry_waits_for_final_save_and_times_out_without_cancelling_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    entry: str,
) -> None:
    import chrys.app.tui.screens.main.rollback_controller as module

    monkeypatch.setattr(module, "ROLLBACK_WAIT_SECONDS", 0.01)
    release = asyncio.Event()
    lifecycle = asyncio.create_task(release.wait())
    tracker = MutationTracker(SnapshotStore(tmp_path))
    engine = _EntryEngine(tracker, str(tmp_path))
    view = _EntryView()
    controller = _controller(engine, view, EventBus(), task=lambda: lifecycle, busy=lambda: not lifecycle.done())
    completed: list[bool] = []
    try:
        if entry == "message":
            await controller.rollback_from_message(None, on_complete=lambda: completed.append(True))
            assert completed == [True]
        else:
            controller.show_rollback({"relative": "1", "absolute": "to 2", "picker": ""}[entry])
            if entry == "picker":
                from chrys.app.tui.screens.diff.rollback_modal import RollbackLoadCancelled

                with pytest.raises(RollbackLoadCancelled):
                    await view.pickers[0][1]()
            else:
                await view.progress[0]()
        assert not lifecycle.done()
        assert lifecycle.cancelling() == 0
        assert view.notices == ["The last turn is still finishing; try again in a moment."]
    finally:
        release.set()
        await lifecycle


@pytest.mark.parametrize("entry", ["message", "relative", "absolute", "picker"])
async def test_waiting_entry_rejects_continue_started_after_the_click(tmp_path: Path, entry: str) -> None:
    release = asyncio.Event()
    lifecycle = asyncio.create_task(release.wait())
    tracker = MutationTracker(SnapshotStore(tmp_path))
    engine = _EntryEngine(tracker, str(tmp_path))
    view = _EntryView()
    bus = EventBus()
    controller = _controller(engine, view, bus, task=lambda: lifecycle)
    published: list[UserRollback] = []

    async def record(event: UserRollback) -> None:
        published.append(event)

    await bus.subscribe(UserRollback, record)
    completed: list[bool] = []
    if entry == "message":
        prepared = controller.rollback_from_message(None, on_complete=lambda: completed.append(True))
    else:
        controller.show_rollback({"relative": "1", "absolute": "to 2", "picker": ""}[entry])
        prepared = view.pickers[0][1]() if entry == "picker" else view.progress[0]()
    operation = asyncio.create_task(prepared)
    await asyncio.sleep(0)
    assert not operation.done()
    engine.conversation_revision += 1
    release.set()
    if entry == "picker":
        from chrys.app.tui.screens.diff.rollback_modal import RollbackLoadCancelled

        with pytest.raises(RollbackLoadCancelled):
            await operation
    else:
        await operation
    await lifecycle
    assert published == []
    assert engine.target_threads == []
    assert view.notices == ["Discard picker cancelled because the conversation changed."]
    if entry == "message":
        assert completed == [True]


async def test_message_entry_running_guard_and_duplicate_click_do_not_publish(tmp_path: Path) -> None:
    engine = _EntryEngine(MutationTracker(SnapshotStore(tmp_path)), str(tmp_path))
    view = _EntryView()
    controller = _controller(engine, view, EventBus(), busy=lambda: True, running=lambda: True)
    complete: list[str] = []
    first = controller.rollback_from_message(None, on_complete=lambda: complete.append("first"))
    second = controller.rollback_from_message(None, on_complete=lambda: complete.append("second"))
    await asyncio.gather(first, second)
    assert sorted(complete) == ["first", "second"]
    assert view.notices == ["Cannot discard while the agent is running.", "A discard is already in progress."]
    assert view.progress == []
    assert view.pickers == []


@pytest.mark.parametrize("entry", ["message", "relative", "absolute"])
async def test_finalization_busy_guard_is_checked_after_the_last_save(tmp_path: Path, entry: str) -> None:
    save_release = asyncio.Event()
    saved = False

    async def finish_turn() -> None:
        nonlocal saved
        await save_release.wait()
        saved = True

    lifecycle = asyncio.create_task(finish_turn())
    tracker = MutationTracker(SnapshotStore(tmp_path))
    tracker.start_turn(3)
    engine = _EntryEngine(tracker, str(tmp_path))
    view = _EntryView()
    bus = EventBus()
    controller = _controller(engine, view, bus, task=lambda: lifecycle, busy=lambda: not lifecycle.done())
    published: list[UserRollback] = []

    async def record(event: UserRollback) -> None:
        assert saved is True
        assert lifecycle.done()
        published.append(event)

    await bus.subscribe(UserRollback, record)
    if entry == "message":
        operation = asyncio.create_task(controller.rollback_from_message(None, on_complete=lambda: None))
    else:
        controller.show_rollback("1" if entry == "relative" else "to 2")
        operation = asyncio.create_task(view.progress[0]())
    try:
        await asyncio.sleep(0)
        assert not operation.done()
        assert published == []
        assert view.notices == []
        save_release.set()
        await operation
        if entry == "message":
            view.confirms[0][1](True)
            await view.progress[0]()
        assert len(published) == 1
        assert published[0].expected_conversation_revision == 7
        assert view.notices == []
    finally:
        save_release.set()
        await lifecycle


async def test_message_entry_does_not_fall_back_when_its_snapshot_is_unavailable(tmp_path: Path) -> None:
    class _PrunedEngine(_EntryEngine):
        def available_rollback_turns(self) -> list[int]:
            return [0, 2]

    engine = _PrunedEngine(MutationTracker(SnapshotStore(tmp_path)), str(tmp_path))
    view = _EntryView()
    complete: list[bool] = []
    await _controller(engine, view, EventBus()).rollback_from_message(1, on_complete=lambda: complete.append(True))
    assert complete == [True]
    assert view.notices == ["Discard is not available."]
    assert view.progress == []
    assert view.pickers == []


class _PickerHost(App):
    def __init__(self, modal: RollbackModal) -> None:
        super().__init__()
        self.modal = modal

    def compose(self) -> ComposeResult:
        return iter(())

    def on_mount(self) -> None:
        self.push_screen(self.modal)


async def test_deferred_picker_keeps_clicked_turn_selected_after_loading(tmp_path: Path) -> None:
    tracker = MutationTracker(SnapshotStore(tmp_path))
    for turn in range(1, 5):
        tracker.start_turn(turn)

    async def load() -> RollbackReadState:
        return RollbackReadState(
            tracker=tracker,
            available_turns=[0, 1, 2, 3],
            current_turn=4,
            conversation_revision=7,
            build_generation=1,
            workspace_cwd=str(tmp_path),
            turn_prompts={},
        )

    modal = RollbackModal(None, [], str(tmp_path), load_state=load, initial_selection=1)
    app = _PickerHost(modal)
    async with app.run_test() as pilot:
        await wait_for(lambda: modal._content_ready, pilot=pilot, description="clicked rollback target preview")
        assert modal.query_one("#rollback-turn-select", Select).value == 1
        await pilot.press("escape")


@pytest.mark.parametrize("existing_draft", ["", "my newer draft"])
async def test_rollback_result_returns_focus_to_composer_and_keeps_a_newer_draft(
    tmp_path: Path,
    existing_draft: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "chrys.app.tui.util.git_branch.GitBranchMonitor._start_observer", lambda _monitor, _target: None
    )
    app = make_chrys_app(tmp_path / "state")
    async with app.run_test() as pilot:
        screen = app.screen
        assert isinstance(screen, MainScreen)
        composer = screen.query_one(InputBar)
        composer.value = existing_draft
        text_area = composer.query_one("#chat-input", TextArea)
        if existing_draft:
            text_area.move_cursor((0, 2))
        screen.set_focus(None)
        await screen._rollback_controller.on_result(
            RollbackResult(session_id="session-1", target_turn=1, rolled_back_user_text="prompt to revise"),
        )
        await pilot.pause()
        expected = existing_draft or "prompt to revise"
        assert composer.value == expected
        assert text_area.has_focus
        assert text_area.cursor_location == ((0, 2) if existing_draft else text_area.document.end)


async def test_pending_entry_reset_allows_new_request_and_ignores_old_completion(tmp_path: Path) -> None:
    engine = _EntryEngine(MutationTracker(SnapshotStore(tmp_path)), str(tmp_path))
    view = _EntryView()
    controller = _controller(engine, view, EventBus())
    completed: list[str] = []
    await controller.rollback_from_message(0, on_complete=lambda: completed.append("old"))
    assert len(view.pickers) == 1
    await controller.rollback_from_message(None, on_complete=lambda: completed.append("duplicate"))
    assert completed == ["duplicate"]
    assert view.notices == ["A discard is already in progress."]

    controller.reset_pending()
    await controller.rollback_from_message(0, on_complete=lambda: completed.append("new"))
    assert len(view.pickers) == 2
    old_dismiss = view.pickers[0][2]
    old_dismiss(None)
    old_dismiss(None)
    assert completed == ["duplicate", "old"]
    await controller.rollback_from_message(None, on_complete=lambda: completed.append("still pending"))
    assert completed == ["duplicate", "old", "still pending"]
    assert len(view.pickers) == 2
    assert view.notices == ["A discard is already in progress."] * 2
    view.pickers[1][2](None)
    assert completed[-1] == "new"


@pytest.mark.parametrize("deferred", [False, True])
@pytest.mark.parametrize("initial", [None, 4])
async def test_pruned_picker_omits_session_start_and_preselects_latest_retained_target(
    tmp_path: Path, deferred: bool, initial: int | None
) -> None:
    tracker = MutationTracker(SnapshotStore(tmp_path))
    for turn in range(1, 8):
        tracker.start_turn(turn)
    state = RollbackReadState(
        tracker=tracker,
        available_turns=[2, 3, 4, 5, 6],
        current_turn=7,
        conversation_revision=7,
        build_generation=1,
        workspace_cwd=str(tmp_path),
        turn_prompts={},
    )

    async def load() -> RollbackReadState:
        return state

    modal = RollbackModal(
        None if deferred else tracker,
        [] if deferred else state.available_turns,
        str(tmp_path),
        current_turn=7,
        load_state=load if deferred else None,
        initial_selection=initial,
    )
    async with _PickerHost(modal).run_test() as pilot:
        await wait_for(lambda: modal._content_ready, pilot=pilot, description="pruned rollback picker ready")
        options = modal._build_turn_options()
        assert [value for _label, value in options] == [6, 5, 4, 3, 2]
        assert all("Session start" not in label.plain for label, _value in options)
        assert modal.query_one("#rollback-turn-select", Select).value == (6 if initial is None else initial)
        await pilot.press("escape")
