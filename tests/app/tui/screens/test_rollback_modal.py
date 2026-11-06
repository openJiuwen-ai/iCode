# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Integration tests for :class:`RollbackModal` — specifically the
"revert is gated by (checkbox AND selection)" decision rule introduced
when we collapsed the two rollback buttons into one + a checkbox.
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from textual.app import App, ComposeResult
from textual.containers import VerticalGroup
from textual.screen import ModalScreen
from textual.widgets import Button, Select, Static
from textual.widgets._select import SelectOverlay

from chrys.app.tui.app import ChrysApp
from chrys.app.tui.screens.diff.rollback_modal import (
    RollbackLoadCancelled,
    RollbackModal,
    RollbackProgressModal,
    _entries_for_target,
    exclusion_reason_label,
)
from chrys.app.tui.screens.diff.screen import (
    CheckableTree,
    DiffFileEntry,
    DiffPeriodPane,
)
from chrys.app.tui.screens.main.screen import MainScreen
from chrys.app.tui.widgets.chat.messages import AgentMessage, UserMessage
from chrys.app.tui.widgets.chat.panel import ChatPanel
from chrys.app.tui.widgets.checkbox import CHECKED_MARKER, Checkbox
from chrys.app.tui.widgets.chrome.footer import ChrysFooter
from chrys.app.tui.widgets.chrome.input_bar import InputBar
from chrys.app.tui.widgets.loading import ChrysLoadingIndicator
from chrys.foundation.config.settings import Settings
from chrys.foundation.events.bus import EventBus
from chrys.foundation.i18n import MessageRef
from chrys.foundation.i18n.formatting import format_message
from chrys.service.mutations.store import SnapshotPolicy, SnapshotStore
from chrys.service.mutations.tracker import MutationTracker
from chrys.service.mutations.types import MutationOp, MutationSource, RollbackExclusionReason
from chrys.service.state.store import JsonFileStateStore
from tests.support.pilot_barrier import screen_is_settled
from tests.support.tui_app_harness import SessionGenerationEngine
from tests.support.waiting import wait_for


def _make_tracker_with_two_changes(tmp_path: Path) -> MutationTracker:
    """Build a tracker with one turn that modifies two files.

    Returns a tracker that can drive a ``RollbackModal`` — the two files
    show up as two entries in the diff pane's change list so we can test
    both "all selected" and "partial selection" scenarios.
    """
    store = SnapshotStore(tmp_path)
    tracker = MutationTracker(store)

    file_a = tmp_path / "a.txt"
    file_b = tmp_path / "b.txt"
    file_a.write_text("a-original", encoding="utf-8")
    file_b.write_text("b-original", encoding="utf-8")

    tracker.start_turn(1)
    mut_a = tracker.record(str(file_a), MutationOp.MODIFY, MutationSource.EDIT_FILE, "call-a")
    assert mut_a is not None
    file_a.write_text("a-changed", encoding="utf-8")
    tracker.record_after(mut_a)

    mut_b = tracker.record(str(file_b), MutationOp.MODIFY, MutationSource.EDIT_FILE, "call-b")
    assert mut_b is not None
    file_b.write_text("b-changed", encoding="utf-8")
    tracker.record_after(mut_b)

    # Start a second turn so the picker has ``[0, 1]`` under the
    # "keep N turns" convention — ``target_turn=1`` keeps turn 1 (and
    # discards turn 2 onwards), which lets us exercise the
    # revert-the-session-so-far flow.  Turn 2 has no mutations, so the
    # rollback diff for ``target_turn=1`` equals the turn-1 mutations.
    tracker.start_turn(2)
    return tracker


def _make_tracker_with_eol_only_change(tmp_path: Path) -> MutationTracker:
    """Build a tracker whose only byte change has no renderable diff hunks."""
    tracker = MutationTracker(SnapshotStore(tmp_path))
    file_path = tmp_path / "Power.yaml"
    file_path.write_bytes(b"name: Power\n")

    tracker.start_turn(1)
    mutation = tracker.record(str(file_path), MutationOp.MODIFY, MutationSource.EDIT_FILE, "call-eol")
    assert mutation is not None
    file_path.write_bytes(b"name: Power\r\n")
    tracker.record_after(mutation)
    return tracker


def _make_tracker_with_metadata_only_change(tmp_path: Path) -> MutationTracker:
    """Build a tracker whose decoded text is unchanged but bytes differ."""
    tracker = MutationTracker(SnapshotStore(tmp_path))
    file_path = tmp_path / "Power.yaml"
    file_path.write_bytes(b"\xef\xbb\xbfname: Power\n")

    tracker.start_turn(1)
    mutation = tracker.record(str(file_path), MutationOp.MODIFY, MutationSource.EDIT_FILE, "call-bom")
    assert mutation is not None
    file_path.write_bytes(b"name: Power\n")
    tracker.record_after(mutation)
    return tracker


class _ModalHostApp(App):
    """Minimal app that pushes ``RollbackModal`` on startup."""

    def __init__(self, modal: RollbackModal | RollbackProgressModal) -> None:
        super().__init__()
        self._modal = modal
        self.dismiss_result: object = "<unset>"

    def compose(self) -> ComposeResult:  # pragma: no cover - Textual wiring
        return iter([])

    async def on_mount(self) -> None:
        def _on_dismiss(result: object) -> None:
            self.dismiss_result = result

        self.push_screen(self._modal, _on_dismiss)


async def _wait_for_empty_class(container: VerticalGroup, pilot, *, expected: bool) -> None:
    """Poll until the rollback container's ``empty`` class matches ``expected``.

    The initial loading worker and ``Select.Changed`` both finish the
    preview before flipping the class. On Windows under xdist load a single
    ``pilot.pause()`` can return mid-chain, so observe the class state
    directly.
    """

    await wait_for(
        lambda: container.has_class("empty") == expected,
        pilot=pilot,
        description=f"rollback container reaches empty={expected!r}",
    )


async def _wait_for_content_ready(modal: RollbackModal, pilot) -> None:
    """Wait for the deferred initial preview to replace its loading shell."""
    # The loader owns this flag. A Pilot barrier also waits for unrelated
    # widget queues and can hold this readiness check past its deadline.
    await wait_for(
        lambda: modal._content_ready,
        description="rollback modal content is ready",
    )


def _mk_modal(tracker: MutationTracker, tmp_path: Path) -> RollbackModal:
    # Under the "keep N turns" convention: 0 = session start
    # (discard all), 1 = keep turn 1 (discard turn 2 — which is
    # empty).  The dialog's default selection is the latest target,
    # which is 1 here.
    return RollbackModal(
        tracker=tracker,
        available_turns=[0, 1],
        cwd=str(tmp_path),
        turn_prompts={},
    )


# ---------------------------------------------------------------------------
# Core decision rule
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rollback_deferred_state_keeps_first_painted_indicator_until_preview_ready(tmp_path: Path) -> None:
    tracker = _make_tracker_with_two_changes(tmp_path)
    rebuilt_workspace = tmp_path / "rebuilt-workspace"
    rebuilt_workspace.mkdir()
    state_load_started = asyncio.Event()
    release_state_load = asyncio.Event()

    async def load_state() -> object:
        state_load_started.set()
        await release_state_load.wait()
        return SimpleNamespace(
            tracker=tracker,
            available_turns=[0],
            current_turn=2,
            workspace_cwd=str(rebuilt_workspace),
            turn_prompts={},
            plan_augment=None,
            attribution_refresh=None,
            projection_acquire=None,
            projection_release=None,
        )

    modal = RollbackModal(
        tracker=None,
        available_turns=[],
        cwd=str(tmp_path),
        load_state=load_state,
    )
    host = _ModalHostApp(modal)

    async with host.run_test() as pilot:
        await asyncio.wait_for(state_load_started.wait(), timeout=1)
        await pilot.pause()

        assert host.screen is modal
        assert modal.query_one("#rollback-loading", ChrysLoadingIndicator).display is True
        assert modal.query_one("#rollback-initial-loading-state")
        assert list(modal.query("#rollback-turn-select")) == []
        assert modal._content_ready is False

        release_state_load.set()
        await _wait_for_content_ready(modal, pilot)

        assert list(modal.query(ChrysLoadingIndicator)) == []
        assert modal.query_one("#rollback-turn-select", Select).value == 0
        assert modal.query_one(DiffPeriodPane)._initialized is True
        assert modal.query_one("#btn-rollback", Button).disabled is False
        assert modal._cwd == str(rebuilt_workspace)


@pytest.mark.asyncio
async def test_rollback_modal_closes_cleanly_when_deferred_projection_is_superseded(tmp_path: Path) -> None:
    async def load_state() -> object:
        raise RollbackLoadCancelled

    modal = RollbackModal(
        tracker=None,
        available_turns=[],
        cwd=str(tmp_path),
        load_state=load_state,
    )
    host = _ModalHostApp(modal)

    async with host.run_test() as pilot:
        await wait_for(
            lambda: host.screen is not modal,
            timeout=4.0,
            interval=0.05,
            pilot=pilot,
            description="superseded rollback modal dismissal",
        )

    assert host.dismiss_result is None


@pytest.mark.asyncio
async def test_cancelled_preview_keeps_projection_fenced_until_worker_thread_quiesces(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import chrys.app.tui.screens.diff.rollback_modal as rollback_module

    tracker = _make_tracker_with_two_changes(tmp_path)
    projection_acquired = asyncio.Event()
    projection_released = asyncio.Event()
    build_started = threading.Event()
    release_build = threading.Event()
    original_build = rollback_module._build_preview_state

    async def acquire_projection() -> str:
        projection_acquired.set()
        return "preview-projection"

    def release_projection(owner: str) -> None:
        assert owner == "preview-projection"
        projection_released.set()

    def blocking_build(tracker: MutationTracker, target_turn: int, cwd: str):
        build_started.set()
        assert release_build.wait(timeout=2)
        return original_build(tracker, target_turn, cwd)

    monkeypatch.setattr(rollback_module, "_build_preview_state", blocking_build)
    modal = RollbackModal(
        tracker=tracker,
        available_turns=[0, 1],
        cwd=str(tmp_path),
    )
    modal._projection_acquire = acquire_projection
    modal._projection_release = release_projection

    operation = asyncio.create_task(modal._load_preview_state_guarded())
    await asyncio.wait_for(projection_acquired.wait(), timeout=1)
    assert await asyncio.to_thread(build_started.wait, 1)

    operation.cancel()
    with pytest.raises(asyncio.CancelledError):
        await operation
    assert projection_released.is_set() is False

    release_build.set()
    await asyncio.wait_for(projection_released.wait(), timeout=1)


@pytest.mark.asyncio
async def test_rollback_loading_height_matches_completed_empty_preview(tmp_path: Path) -> None:
    tracker = _make_tracker_with_two_changes(tmp_path)
    state_load_started = asyncio.Event()
    release_state_load = asyncio.Event()

    async def load_state() -> object:
        state_load_started.set()
        await release_state_load.wait()
        return SimpleNamespace(
            tracker=tracker,
            available_turns=[0, 1],
            current_turn=2,
            workspace_cwd=str(tmp_path),
            turn_prompts={},
            plan_augment=None,
            attribution_refresh=None,
            projection_acquire=None,
            projection_release=None,
        )

    modal = RollbackModal(
        tracker=None,
        available_turns=[],
        cwd=str(tmp_path),
        load_state=load_state,
    )
    host = _ModalHostApp(modal)

    async with host.run_test(size=(120, 36)) as pilot:
        await asyncio.wait_for(state_load_started.wait(), timeout=1)
        await pilot.pause()
        container = modal.query_one("#rollback-container", VerticalGroup)
        loading_height = container.outer_size.height
        assert loading_height == 17

        release_state_load.set()
        await _wait_for_content_ready(modal, pilot)
        await pilot.pause()

        assert container.has_class("empty")
        assert modal.query_one("#rollback-diff-placeholder", Static)
        assert container.outer_size.height == loading_height


@pytest.mark.asyncio
async def test_rollback_progress_modal_blocks_until_operation_finishes() -> None:
    operation_started = asyncio.Event()
    release_operation = asyncio.Event()

    async def operation() -> None:
        operation_started.set()
        await release_operation.wait()

    modal = RollbackProgressModal(operation)
    host = _ModalHostApp(modal)

    async with host.run_test(size=(120, 36)) as pilot:
        await asyncio.wait_for(operation_started.wait(), timeout=1)
        await pilot.pause()

        assert host.screen is modal
        assert modal.query_one("#rollback-progress-loading", ChrysLoadingIndicator)
        assert modal.query_one("#rollback-progress-container", VerticalGroup).outer_size.height == 17

        await pilot.press("escape")
        await pilot.click(offset=(0, 0))
        await pilot.pause()
        assert host.screen is modal
        assert host.dismiss_result == "<unset>"

        release_operation.set()
        await wait_for(
            lambda: host.screen is not modal,
            timeout=4.0,
            interval=0.05,
            pilot=pilot,
            description="rollback progress modal dismissal",
        )

    assert host.dismiss_result is None


@pytest.mark.asyncio
async def test_rollback_progress_handoff_does_not_cancel_main_screen_owned_operation() -> None:
    operation_started = asyncio.Event()
    release_operation = asyncio.Event()
    operation_finished = asyncio.Event()
    operation_cancelled = asyncio.Event()
    host: _ModalHostApp

    async def operation() -> None:
        operation_started.set()
        try:
            await release_operation.wait()
        except asyncio.CancelledError:
            operation_cancelled.set()
            raise
        operation_finished.set()

    def start_worker(awaitable) -> object:
        return host.run_worker(awaitable, group="rollback-progress")

    modal = RollbackProgressModal(operation, start_worker=start_worker)
    host = _ModalHostApp(modal)

    async with host.run_test(size=(120, 36)) as pilot:
        await asyncio.wait_for(operation_started.wait(), timeout=1)

        modal.handoff_to_session_restore()
        await wait_for(
            lambda: host.screen is not modal,
            timeout=4.0,
            interval=0.05,
            pilot=pilot,
            description="rollback-to-restore modal handoff",
        )
        assert operation_cancelled.is_set() is False

        release_operation.set()
        await asyncio.wait_for(operation_finished.wait(), timeout=1)


@pytest.mark.asyncio
async def test_rollback_progress_modal_defers_dismissal_until_unexpected_covering_modal_closes() -> None:
    """Fallback completion must not pop an unrelated transient modal above it."""
    covering_modal = ModalScreen[None]()
    operation_finished = asyncio.Event()
    modal: RollbackProgressModal

    async def operation() -> None:
        await modal.app.push_screen(covering_modal)
        operation_finished.set()

    modal = RollbackProgressModal(operation)
    host = _ModalHostApp(modal)

    async with host.run_test(size=(120, 36)) as pilot:
        await asyncio.wait_for(operation_finished.wait(), timeout=1)
        await pilot.pause()

        assert host.screen is covering_modal
        assert modal.is_attached
        assert modal._dismiss_requested is True

        covering_modal.dismiss(None)
        await wait_for(
            lambda: host.screen is not modal,
            timeout=4.0,
            interval=0.05,
            pilot=pilot,
            description="deferred rollback progress dismissal",
        )

    assert host.dismiss_result is None


@pytest.mark.asyncio
async def test_direct_rollback_replaces_progress_modal_with_single_restore_modal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Agent restore startup must hand off the blocker instead of stacking two modals."""
    from chrys.app.tui.screens.dialogs.agent_load import AgentLoadDialog
    from chrys.foundation.events.types import AgentLoadStarted, UserRollback

    restore_modal_pushed = asyncio.Event()
    release_rollback = asyncio.Event()
    rollback_finished = asyncio.Event()
    published_rollbacks: list[UserRollback] = []

    class _Engine(SessionGenerationEngine):
        mutation_tracker = None
        mutation_coordinator = None
        current_turn_number = 2
        conversation_revision = 1
        session_generation = 1
        turn_lifecycle_task = None

        def available_rollback_turns(self) -> list[int]:
            return [0, 1]

        async def shutdown(self) -> None:
            return

    class _EmptyRegistry:
        def list_profiles(self) -> list[object]:
            return []

        def load_all(self) -> None:
            return

        def get(self, _name: str) -> None:
            return None

    bus = EventBus()

    async def simulate_engine_rollback(event: UserRollback) -> None:
        published_rollbacks.append(event)
        await bus.publish(
            AgentLoadStarted(
                operation="restore",
                to_profile="Code",
                to_display_name="Code",
                session_id="session-1",
            )
        )
        restore_modal_pushed.set()
        await release_rollback.wait()
        rollback_finished.set()

    await bus.subscribe(UserRollback, simulate_engine_rollback)
    monkeypatch.delenv("TEXTUAL_DRIVER", raising=False)
    monkeypatch.setattr(
        "chrys.app.tui.util.git_branch.GitBranchMonitor._start_observer", lambda _monitor, _target: None
    )
    app = ChrysApp(
        bus,
        _Engine(),  # type: ignore[arg-type]
        settings=Settings(),
        state_store=JsonFileStateStore(tmp_path / "state"),
        agent_registry=_EmptyRegistry(),  # type: ignore[arg-type]
        gc_freeze_enabled=False,
    )

    async with app.run_test(size=(120, 36)) as pilot:
        main_screen = app.screen
        assert isinstance(main_screen, MainScreen)
        input_bar = main_screen.query_one(InputBar)
        input_bar.value = "/rollback 1"
        input_bar.focus_input()
        await wait_for(
            lambda: input_bar.query_one("#chat-input").has_focus,
            pilot=pilot,
            description="composer focus before interaction",
        )

        await pilot.press("enter")
        await asyncio.wait_for(restore_modal_pushed.wait(), timeout=2)
        await wait_for(
            lambda: isinstance(app.screen, AgentLoadDialog),
            timeout=4.0,
            interval=0.05,
            pilot=pilot,
            description="session restore modal",
        )

        assert len(published_rollbacks) == 1
        assert published_rollbacks[0].target_turn == 0
        assert published_rollbacks[0].relative_turns == 1
        assert published_rollbacks[0].revert_changes is True
        assert sum(isinstance(screen, AgentLoadDialog) for screen in app.screen_stack) == 1
        assert all(not isinstance(screen, RollbackProgressModal) for screen in app.screen_stack)

        release_rollback.set()
        await asyncio.wait_for(rollback_finished.wait(), timeout=1)
        app.screen.dismiss(None)
        await wait_for(
            lambda: app.screen is main_screen,
            timeout=4.0,
            interval=0.05,
            pilot=pilot,
            description="session restore modal close",
        )


@pytest.mark.asyncio
async def test_populated_main_screen_rollback_opens_before_snapshot_scan_without_underlay_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real slash-command path must paint before session-sized reads begin."""
    tracker = _make_tracker_with_two_changes(tmp_path)
    state_read_started = threading.Event()
    release_state_read = threading.Event()

    class _Engine(SessionGenerationEngine):
        mutation_tracker = tracker
        mutation_coordinator = None
        current_turn_number = 2
        conversation_revision = 7
        build_generation = 1
        workspace_primary_cwd = str(tmp_path)
        session_generation = 1
        turn_lifecycle_task = None
        rollback_projection_fenced = False
        projection_acquisitions = 0
        projection_releases = 0

        async def begin_rollback_projection(
            self,
            *,
            session_id: str | None,
            session_generation: int,
        ) -> str:
            assert session_id is None
            assert session_generation == 1
            self.rollback_projection_fenced = True
            self.projection_acquisitions += 1
            return "test-rollback-projection"

        def finish_rollback_projection(self, owner: str) -> None:
            assert owner == "test-rollback-projection"
            assert self.rollback_projection_fenced
            self.rollback_projection_fenced = False
            self.projection_releases += 1

        def available_rollback_turns(self) -> list[int]:
            assert self.rollback_projection_fenced
            state_read_started.set()
            if not release_state_read.wait(timeout=5):
                raise TimeoutError("test did not release rollback state read")
            return [0, 1]

        def turn_prompt_previews(self) -> dict[int, str]:
            return {}

        async def shutdown(self) -> None:
            return

    class _EmptyRegistry:
        def list_profiles(self) -> list[object]:
            return []

        def load_all(self) -> None:
            return

        def get(self, _name: str) -> None:
            return None

    monkeypatch.delenv("TEXTUAL_DRIVER", raising=False)
    monkeypatch.setattr(
        "chrys.app.tui.util.git_branch.GitBranchMonitor._start_observer", lambda _monitor, _target: None
    )
    engine = _Engine()
    app = ChrysApp(
        EventBus(),
        engine,  # type: ignore[arg-type]
        settings=Settings(),
        state_store=JsonFileStateStore(tmp_path / "state"),
        agent_registry=_EmptyRegistry(),  # type: ignore[arg-type]
        gc_freeze_enabled=False,
    )

    async with app.run_test(size=(120, 36)) as pilot:
        main_screen = app.screen
        assert isinstance(main_screen, MainScreen)
        panel = main_screen.query_one(ChatPanel)
        transcript = []
        for index in range(16):
            transcript.extend(
                (
                    UserMessage(f"Question {index}\nwith a second line"),
                    AgentMessage(f"## Answer {index}\n\nA paragraph with `code` and **emphasis**."),
                )
            )
        await panel.mount(*transcript)
        await wait_for(
            lambda: len(panel.walk_children()) >= 64,
            timeout=4.0,
            interval=0.05,
            pilot=pilot,
            description="populated transcript",
        )
        await wait_for(
            lambda: not main_screen._layout_required and not main_screen._scroll_required,
            timeout=4.0,
            interval=0.05,
            pilot=pilot,
            description="settled main-screen layout",
        )

        input_bar = main_screen.query_one(InputBar)
        input_bar.value = "/rollback"
        input_bar.focus_input()
        await wait_for(
            lambda: input_bar.query_one("#chat-input").has_focus and screen_is_settled(app, main_screen),
            pilot=pilot,
            description="composer focus and a settled screen before interaction",
        )

        style_updates: list[bool] = []
        footer_recomposes: list[None] = []
        layout_refreshes: list[None] = []
        monkeypatch.setattr(
            main_screen,
            "update_node_styles",
            lambda animate=True: style_updates.append(animate),
        )
        monkeypatch.setattr(
            main_screen,
            "_refresh_layout",
            lambda *_args, **_kwargs: layout_refreshes.append(None),
        )

        async def record_footer_recompose() -> None:
            footer_recomposes.append(None)

        monkeypatch.setattr(main_screen.query_one(ChrysFooter), "recompose", record_footer_recompose)

        await pilot.press("enter")
        await wait_for(
            lambda: isinstance(app.screen, RollbackModal),
            timeout=4.0,
            interval=0.05,
            pilot=pilot,
            description="rollback modal push",
        )
        modal = app.screen
        assert isinstance(modal, RollbackModal)
        assert await asyncio.to_thread(state_read_started.wait, 2)
        await pilot.pause()

        assert modal.query_one("#rollback-loading", ChrysLoadingIndicator).display is True
        assert modal._content_ready is False
        assert style_updates == []
        assert footer_recomposes == []
        assert layout_refreshes == []
        assert engine.projection_acquisitions == 1
        assert engine.projection_releases == 0

        release_state_read.set()
        await _wait_for_content_ready(modal, pilot)
        await pilot.press("escape")
        await wait_for(
            lambda: app.screen is main_screen,
            timeout=4.0,
            interval=0.05,
            pilot=pilot,
            description="rollback modal close",
        )

        assert style_updates == []
        assert footer_recomposes == []
        assert layout_refreshes == []
        assert engine.projection_acquisitions == 2
        assert engine.projection_releases == 2


@pytest.mark.asyncio
async def test_rollback_paints_loading_shell_before_building_content(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import chrys.app.tui.screens.diff.rollback_modal as rollback_module

    tracker = _make_tracker_with_two_changes(tmp_path)
    refresh_started = asyncio.Event()
    release_refresh = asyncio.Event()
    build_thread_ids: list[int] = []
    event_loop_thread_id = threading.get_ident()
    original_build = rollback_module._build_preview_state

    async def refresh_attribution() -> bool:
        refresh_started.set()
        await release_refresh.wait()
        return False

    def tracked_build(tracker: MutationTracker, target_turn: int, cwd: str):
        build_thread_ids.append(threading.get_ident())
        return original_build(tracker, target_turn, cwd)

    monkeypatch.setattr(rollback_module, "_build_preview_state", tracked_build)
    modal = RollbackModal(
        tracker=tracker,
        available_turns=[0, 1],
        cwd=str(tmp_path),
        attribution_refresh=refresh_attribution,
    )
    modal._selected_turn = 0
    host = _ModalHostApp(modal)

    async with host.run_test() as pilot:
        await asyncio.wait_for(refresh_started.wait(), timeout=1)
        await pilot.pause()

        assert host.screen is modal
        container = modal.query_one("#rollback-container", VerticalGroup)
        # Compact shell paints immediately: Select + footer visible,
        # loading indicator in the placeholder slot, rollback disabled.
        assert container.has_class("empty")
        assert modal.query_one("#rollback-loading", ChrysLoadingIndicator).display is True
        assert modal.query_one("#rollback-loading").parent is modal.query_one("#rollback-diff-wrapper")
        assert modal.query_one("#rollback-turn-select", Select)
        assert modal.query_one("#rollback-buttons")
        assert modal.query_one("#btn-rollback", Button).disabled is True
        assert modal.query_one("#revert-checkbox", Checkbox).display is False
        assert list(modal.query(DiffPeriodPane)) == []
        assert modal.check_action("toggle_view", ()) is False
        assert modal.check_action("toggle_check", ()) is False
        assert modal.check_action("close", ()) is True

        await pilot.press("space", "x")
        assert modal._pane is None

        release_refresh.set()
        await _wait_for_content_ready(modal, pilot)

        assert list(modal.query(ChrysLoadingIndicator)) == []
        assert modal.query_one("#rollback-turn-select", Select)
        assert modal.query_one(DiffPeriodPane)._initialized is True
        assert modal.query_one("#btn-rollback", Button).disabled is False
        # Two file changes → the modal expands to the full overlay.
        assert container.has_class("empty") is False
        assert modal.check_action("toggle_view", ()) is True
        assert modal.check_action("toggle_check", ()) is True
        assert len(build_thread_ids) == 1
        assert build_thread_ids[0] != event_loop_thread_id


@pytest.mark.asyncio
async def test_rollback_escape_cancels_loading_before_dismiss(tmp_path: Path) -> None:
    tracker = _make_tracker_with_two_changes(tmp_path)
    refresh_started = asyncio.Event()
    refresh_cancelled = asyncio.Event()
    never_finish = asyncio.Event()

    async def refresh_attribution() -> bool:
        refresh_started.set()
        try:
            await never_finish.wait()
        except asyncio.CancelledError:
            refresh_cancelled.set()
            raise
        raise AssertionError("unreachable")

    modal = RollbackModal(
        tracker=tracker,
        available_turns=[0, 1],
        cwd=str(tmp_path),
        attribution_refresh=refresh_attribution,
    )
    host = _ModalHostApp(modal)

    async with host.run_test() as pilot:
        await asyncio.wait_for(refresh_started.wait(), timeout=1)

        await pilot.press("escape")
        await asyncio.wait_for(refresh_cancelled.wait(), timeout=1)
        await pilot.pause()

        assert host.dismiss_result is None
        assert host.screen is not modal
        assert modal.is_attached is False
        assert modal._content_ready is False
        assert list(modal.query(DiffPeriodPane)) == []
        assert list(modal.query("#rollback-load-error")) == []


@pytest.mark.asyncio
async def test_rollback_load_error_survives_removed_loading_shell(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tracker = _make_tracker_with_two_changes(tmp_path)
    refresh_started = asyncio.Event()
    release_refresh = asyncio.Event()

    async def refresh_attribution() -> bool:
        refresh_started.set()
        await release_refresh.wait()
        return False

    modal = RollbackModal(
        tracker=tracker,
        available_turns=[0, 1],
        cwd=str(tmp_path),
        attribution_refresh=refresh_attribution,
    )
    modal._selected_turn = 0
    host = _ModalHostApp(modal)

    async with host.run_test() as pilot:
        await asyncio.wait_for(refresh_started.wait(), timeout=1)

        def fail_refresh_bindings() -> None:
            raise RuntimeError("binding refresh failed")

        with monkeypatch.context() as phase_patch:
            phase_patch.setattr(modal, "refresh_bindings", fail_refresh_bindings)
            release_refresh.set()
            await wait_for(
                lambda: list(modal.query("#rollback-load-error")),
                pilot=pilot,
                description="rollback loading failure renders its fallback error",
            )

            assert host.screen is modal
            assert modal._content_ready is False
            # The error replaces whatever occupied the preview slot; the
            # shell (Select + footer) stays visible with rollback disabled.
            assert list(modal.query(ChrysLoadingIndicator)) == []
            assert modal.query_one(".rollback-content").display is True
            assert modal.query_one("#rollback-load-error").parent is modal.query_one("#rollback-diff-wrapper")
            assert modal.query_one("#btn-rollback", Button).disabled is True
            assert modal.query_one("#rollback-container", VerticalGroup).has_class("empty")
            assert (
                modal.query_one("#rollback-load-error", Static)
                .render()
                .plain.startswith("Error: unable to load discard preview")
            )


@pytest.mark.asyncio
async def test_turn_change_preview_failure_clears_old_selection_and_disables_rollback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A new target can never be confirmed with the prior target's checked paths."""
    tracker = _make_tracker_with_two_changes(tmp_path)
    modal = _mk_modal(tracker, tmp_path)
    modal._selected_turn = 0
    host = _ModalHostApp(modal)

    async with host.run_test() as pilot:
        await _wait_for_content_ready(modal, pilot)
        old_pane = modal._pane
        assert old_pane is not None
        assert len(old_pane.selected_paths()) == 2
        original_load = modal._load_preview_state

        async def fail_preview_load() -> tuple[list[DiffFileEntry], object]:
            raise RuntimeError("injected turn preview failure")

        monkeypatch.setattr(modal, "_load_preview_state", fail_preview_load)
        select = modal.query_one("#rollback-turn-select", Select)
        select.value = 1
        await wait_for(
            lambda: modal._preview_failed and list(modal.query("#rollback-preview-error")),
            pilot=pilot,
            description="rollback preview enters its safe error state",
        )

        rollback = modal.query_one("#btn-rollback", Button)
        assert modal._selected_turn == 1
        assert modal._pane is None
        assert old_pane not in list(modal.query(DiffPeriodPane))
        assert rollback.disabled is True
        assert modal.check_action("toggle_view", ()) is False
        rollback.press()
        await pilot.pause()
        assert host.dismiss_result == "<unset>"

        # Choosing another target retries projection and leaves the modal usable.
        monkeypatch.setattr(modal, "_load_preview_state", original_load)
        select.value = 0
        await wait_for(
            lambda: not modal._preview_failed and not modal._preview_refreshing and modal._pane is not None,
            pilot=pilot,
            description="rollback preview recovers after a successful selection",
        )
        assert rollback.disabled is False
        assert modal._pane is not old_pane


@pytest.mark.asyncio
async def test_rollback_default_state_dismisses_with_full_revert(tmp_path: Path) -> None:
    """Fresh modal: checkbox checked, all files selected → revert with
    full path list.
    """
    tracker = _make_tracker_with_two_changes(tmp_path)
    modal = _mk_modal(tracker, tmp_path)
    # Pre-select target_turn=0 (session start) so the diff is non-empty:
    # reverting to session start reverts the turn-1 mutations.  Keeping
    # 1 turn (target_turn=1) would revert only turns >= 2 which have no
    # mutations in this fixture → empty diff.
    modal._selected_turn = 0

    host = _ModalHostApp(modal)
    async with host.run_test() as pilot:
        await pilot.pause()  # let modal mount + build pane
        await _wait_for_content_ready(modal, pilot)
        pane = host.screen.query_one(DiffPeriodPane)
        assert len(pane.selected_paths()) == 2  # both files checked

        cb = host.screen.query_one("#revert-checkbox", Checkbox)
        assert cb.value is True
        assert cb.render().plain.startswith(CHECKED_MARKER)
        assert "Also restore 2 checked files" in str(cb.label)

        # Press the single Rollback button.
        host.screen.query_one("#btn-rollback", Button).press()
        await pilot.pause()

    assert isinstance(host.dismiss_result, tuple), f"expected tuple dismiss, got {host.dismiss_result!r}"
    target, revert, paths = host.dismiss_result
    assert target == 0
    assert revert is True
    assert paths is not None
    assert len(paths) == 2


@pytest.mark.asyncio
async def test_rollback_with_checkbox_unchecked_skips_revert(tmp_path: Path) -> None:
    """Unchecking the footer checkbox → rollback dismisses with
    revert=False even though the tree still has every file ticked.
    """
    tracker = _make_tracker_with_two_changes(tmp_path)
    modal = _mk_modal(tracker, tmp_path)
    modal._selected_turn = 0  # session start (non-empty diff in fixture)

    host = _ModalHostApp(modal)
    async with host.run_test() as pilot:
        await pilot.pause()
        await _wait_for_content_ready(modal, pilot)
        cb = host.screen.query_one("#revert-checkbox", Checkbox)
        cb.value = False
        await pilot.pause()
        host.screen.query_one("#btn-rollback", Button).press()
        await pilot.pause()

    target, revert, paths = host.dismiss_result
    assert target == 0
    assert revert is False
    assert paths is None


@pytest.mark.asyncio
async def test_rollback_with_all_files_unselected_skips_revert(tmp_path: Path) -> None:
    """Even with the checkbox checked, unchecking every file in the
    tree forces revert=False — you can't "revert 0 files".
    """
    tracker = _make_tracker_with_two_changes(tmp_path)
    modal = _mk_modal(tracker, tmp_path)
    modal._selected_turn = 0  # session start (non-empty diff in fixture)

    host = _ModalHostApp(modal)
    async with host.run_test() as pilot:
        await pilot.pause()
        # The tree WIDGET exists from compose, but its leaves are only
        # built once the deferred load finishes — toggling before that
        # silently no-ops over zero leaves on a slow CI worker.
        await _wait_for_content_ready(modal, pilot)
        pane = host.screen.query_one(DiffPeriodPane)
        tree = host.screen.query_one(".workspace-tree", CheckableTree)
        # Uncheck every leaf.
        for leaf in list(tree.root.children):
            if isinstance(leaf.data, DiffFileEntry):
                pane.toggle_check_node(leaf)

        assert pane.selected_paths() == []
        cb = host.screen.query_one("#revert-checkbox", Checkbox)
        # Auto-uncheck kicks in once the count hits zero — mirrors what
        # the user sees before they press confirm.  The sync rides a
        # SelectionChanged message, so poll rather than pause once.
        await wait_for(
            lambda: cb.value is False,
            pilot=pilot,
            description="rollback checkbox becomes unchecked",
        )
        assert cb.value is False
        assert "Also restore 0 checked files" in str(cb.label)

        host.screen.query_one("#btn-rollback", Button).press()
        await pilot.pause()

    _target, revert, paths = host.dismiss_result
    assert revert is False
    assert paths is None


@pytest.mark.asyncio
async def test_rollback_label_reflects_partial_selection(tmp_path: Path) -> None:
    """Un-ticking one file updates the checkbox label to "Also restore 1 checked
    file" (singular) and the dismissal carries only that path."""
    tracker = _make_tracker_with_two_changes(tmp_path)
    modal = _mk_modal(tracker, tmp_path)
    modal._selected_turn = 0  # session start (non-empty diff in fixture)

    host = _ModalHostApp(modal)
    async with host.run_test() as pilot:
        await pilot.pause()
        # Leaves exist only after the deferred load — see the sibling
        # all-files-unselected test.
        await _wait_for_content_ready(modal, pilot)
        pane = host.screen.query_one(DiffPeriodPane)
        tree = host.screen.query_one(".workspace-tree", CheckableTree)
        leaves = [c for c in tree.root.children if isinstance(c.data, DiffFileEntry)]
        # Uncheck the first leaf only.
        pane.toggle_check_node(leaves[0])

        cb = host.screen.query_one("#revert-checkbox", Checkbox)
        # Singular form when selected == 1.  The label update rides a
        # SelectionChanged message, so poll rather than pause once.
        await wait_for(
            lambda: "Also restore 1 checked file" in str(cb.label),
            pilot=pilot,
            description="rollback checkbox describes one file change",
        )
        assert "Also restore 1 checked file" in str(cb.label)
        assert "changes" not in str(cb.label) or str(cb.label).count("changes") == 0
        assert cb.value is True

        host.screen.query_one("#btn-rollback", Button).press()
        await pilot.pause()

    _target, revert, paths = host.dismiss_result
    assert revert is True
    # The single path that was still checked makes it through.
    assert paths == [leaves[1].data.path]


# ---------------------------------------------------------------------------
# Footer checkbox → tree cascade
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unchecking_footer_checkbox_clears_tree_selection(tmp_path: Path) -> None:
    """Clicking the footer checkbox off → every leaf in the tree
    becomes unchecked; re-checking brings them all back.
    """
    tracker = _make_tracker_with_two_changes(tmp_path)
    modal = _mk_modal(tracker, tmp_path)
    modal._selected_turn = 0  # session start (non-empty diff in fixture)

    host = _ModalHostApp(modal)
    async with host.run_test() as pilot:
        await pilot.pause()
        await _wait_for_content_ready(modal, pilot)
        pane = host.screen.query_one(DiffPeriodPane)
        cb = host.screen.query_one("#revert-checkbox", Checkbox)

        assert set(pane.selected_paths()) == {
            str(tmp_path / "a.txt"),
            str(tmp_path / "b.txt"),
        }

        # Uncheck the footer checkbox → tree should empty out.
        cb.value = False
        await pilot.pause()
        assert pane.selected_paths() == []
        assert "Also restore 0 checked files" in str(cb.label)

        # Re-check → tree should re-populate.
        cb.value = True
        await pilot.pause()
        assert set(pane.selected_paths()) == {
            str(tmp_path / "a.txt"),
            str(tmp_path / "b.txt"),
        }
        assert "Also restore 2 checked files" in str(cb.label)


@pytest.mark.asyncio
async def test_footer_checkbox_label_updates_without_selection_changed_message(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The footer checkbox handler updates its own label without waiting
    for the pane's queued ``SelectionChanged`` message.
    """
    tracker = _make_tracker_with_two_changes(tmp_path)
    modal = _mk_modal(tracker, tmp_path)
    modal._selected_turn = 0  # session start (non-empty diff in fixture)

    host = _ModalHostApp(modal)
    async with host.run_test() as pilot:
        await pilot.pause()
        await _wait_for_content_ready(modal, pilot)
        pane = host.screen.query_one(DiffPeriodPane)
        cb = host.screen.query_one("#revert-checkbox", Checkbox)

        cb.value = False
        await pilot.pause()
        await pilot.pause()
        assert pane.selected_paths() == []
        assert "Also restore 0 checked files" in str(cb.label)

        posted_messages: list[object] = []
        with monkeypatch.context() as m:
            m.setattr(pane, "post_message", posted_messages.append)
            cb.value = True
            await pilot.pause()

        assert posted_messages
        assert set(pane.selected_paths()) == {
            str(tmp_path / "a.txt"),
            str(tmp_path / "b.txt"),
        }
        assert "Also restore 2 checked files" in str(cb.label)


@pytest.mark.asyncio
async def test_footer_checkbox_cascade_does_not_loop(tmp_path: Path) -> None:
    """Re-entrancy check: cascading checkbox → tree → (SelectionChanged)
    → checkbox must not re-fire the cascade. Programmatic checkbox changes
    from ``_sync_revert_checkbox`` must suppress their queued
    ``Checkbox.Changed`` message or a delayed tree event can overwrite a
    newer footer click.
    """
    tracker = _make_tracker_with_two_changes(tmp_path)
    modal = _mk_modal(tracker, tmp_path)
    modal._selected_turn = 0  # session start (non-empty diff in fixture)

    host = _ModalHostApp(modal)
    async with host.run_test() as pilot:
        await pilot.pause()
        await _wait_for_content_ready(modal, pilot)
        pane = host.screen.query_one(DiffPeriodPane)
        cb = host.screen.query_one("#revert-checkbox", Checkbox)

        # Uncheck one leaf manually; then toggle checkbox off + on and
        # confirm the selection ends back at "all checked" cleanly.
        tree = host.screen.query_one(".workspace-tree", CheckableTree)
        leaves = [c for c in tree.root.children if isinstance(c.data, DiffFileEntry)]
        pane.toggle_check_node(leaves[0])
        await pilot.pause()
        assert len(pane.selected_paths()) == 1

        cb.value = False
        await pilot.pause()
        assert pane.selected_paths() == []

        cb.value = True
        # Model the Windows CI interleaving: the previous tree-side
        # SelectionChanged(0) arrives after the user has already re-checked
        # the footer. Its programmatic value write must not enqueue a stale
        # Checkbox.Changed(False) behind the user's queued True event.
        modal._sync_revert_checkbox(selected=0, total=2)
        await pilot.pause()
        assert len(pane.selected_paths()) == 2
        # Checkbox value stayed True — message prevention blocked the stale
        # programmatic change from cascading back into the tree.
        assert cb.value is True


# ---------------------------------------------------------------------------
# Button label — destination turn resolution with gapped available_turns
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rollback_button_label_uses_target_directly(tmp_path: Path) -> None:
    """An engine target of 3 labels the first removed turn, 4."""
    tracker = _make_tracker_with_two_changes(tmp_path)
    modal = RollbackModal(
        tracker=tracker,
        available_turns=[0, 3],
        cwd=str(tmp_path),
        turn_prompts={},
    )
    modal._selected_turn = 3

    host = _ModalHostApp(modal)
    async with host.run_test() as pilot:
        await pilot.pause()
        await _wait_for_content_ready(modal, pilot)
        btn = host.screen.query_one("#btn-rollback", Button)
        assert str(btn.label) == "Discard turn 4"


@pytest.mark.asyncio
async def test_rollback_button_label_is_discard_all_on_welcome(tmp_path: Path) -> None:
    """Selecting the welcome target (``target_turn=0``) collapses the
    button label to "Discard all turns"."""
    tracker = _make_tracker_with_two_changes(tmp_path)
    modal = RollbackModal(
        tracker=tracker,
        available_turns=[0, 1],
        cwd=str(tmp_path),
        turn_prompts={},
    )
    modal._selected_turn = 0

    host = _ModalHostApp(modal)
    async with host.run_test() as pilot:
        await pilot.pause()
        await _wait_for_content_ready(modal, pilot)
        btn = host.screen.query_one("#btn-rollback", Button)
        assert str(btn.label) == "Discard all turns"


# ---------------------------------------------------------------------------
# Turn picker dropdown — reframed as "last kept turn"
# ---------------------------------------------------------------------------


def test_turn_picker_preview_keeps_longer_prompt_by_default(tmp_path: Path) -> None:
    tracker = _make_tracker_with_two_changes(tmp_path)
    prompt = "x" * 120
    modal = RollbackModal(
        tracker=tracker,
        available_turns=[0, 1],
        cwd=str(tmp_path),
        turn_prompts={2: prompt},
    )

    labels_by_value = {value: label for label, value in modal._build_turn_options()}
    keep_label = labels_by_value[1]
    assert keep_label.plain.endswith(prompt)
    assert "…" not in keep_label.plain


@pytest.mark.asyncio
async def test_turn_picker_wrapped_prompt_aligns_continuations_with_message(tmp_path: Path) -> None:
    tracker = _make_tracker_with_two_changes(tmp_path)
    prompt = (
        "http://localhost:3000/cards/click-view/411111-masked "
        "masked content is used as the REST URI and should stay aligned"
    )
    modal = RollbackModal(
        tracker=tracker,
        available_turns=[0, 1],
        cwd=str(tmp_path),
        turn_prompts={2: prompt},
        current_turn=2,
    )
    host = _ModalHostApp(modal)

    async with host.run_test(size=(70, 30)) as pilot:
        await _wait_for_content_ready(modal, pilot)
        select = modal.query_one("#rollback-turn-select", Select)
        select.expanded = True
        await pilot.pause()

        overlay = select.query_one(SelectOverlay)
        option_index = next(index for index, (_label, value) in enumerate(select._options) if value == 1)
        option = overlay.get_option_at_index(option_index)
        style = overlay.get_visual_style("option-list--option")
        lines = [strip.text.rstrip() for strip in overlay._get_option_render(option, style)]

        assert len(lines) >= 2
        message_column = lines[0].index("http://")
        continuation_indents = [len(line) - len(line.lstrip()) for line in lines[1:] if line.strip()]
        assert continuation_indents
        assert set(continuation_indents) == {message_column}
        assert overlay._find_search_match("masked content") == option_index


@pytest.mark.asyncio
async def test_turn_picker_layout_with_gapped_available_turns(tmp_path: Path) -> None:
    """Gapped engine targets offer turns 4 and 1, without an informational row."""
    tracker = _make_tracker_with_two_changes(tmp_path)
    modal = RollbackModal(
        tracker=tracker,
        available_turns=[0, 3],
        cwd=str(tmp_path),
        turn_prompts={},
    )
    # The latest target keeps the default selection.
    host = _ModalHostApp(modal)
    async with host.run_test() as pilot:
        await pilot.pause()
        await _wait_for_content_ready(modal, pilot)
        select = host.screen.query_one("#rollback-turn-select", Select)
        # Values remain backend targets.
        values = [value for _prompt, value in select._options]  # type: ignore[attr-defined]
        assert values == [3, 0]
        assert select.value == 3


@pytest.mark.asyncio
async def test_turn_picker_snaps_back_when_preview_refresh_in_flight(tmp_path: Path) -> None:
    """A turn change landing while a preview refresh is in flight cannot
    be honored.  It must snap the Select back rather than silently drop:
    the dropdown would otherwise display a turn ``_selected_turn``
    doesn't hold, and the destructive confirm dismisses with
    ``_selected_turn``."""
    tracker = _make_tracker_with_two_changes(tmp_path)
    modal = RollbackModal(
        tracker=tracker,
        available_turns=[0, 1],
        cwd=str(tmp_path),
        turn_prompts={},
    )
    host = _ModalHostApp(modal)
    async with host.run_test() as pilot:
        await pilot.pause()
        await _wait_for_content_ready(modal, pilot)
        select = host.screen.query_one("#rollback-turn-select", Select)
        assert select.value == 1
        modal._preview_refreshing = True
        try:
            select.value = 0
            await wait_for(
                lambda: select.value == 1,
                pilot=pilot,
                description="rollback turn selector reaches turn one",
            )
        finally:
            modal._preview_refreshing = False
        assert select.value == 1
        assert modal._selected_turn == 1


@pytest.mark.asyncio
async def test_empty_turn_collapses_modal_height(tmp_path: Path) -> None:
    """Turn with no file changes → container gets the ``empty`` class
    so the CSS collapses the 90% overlay to content-height.  Switching
    to a turn that *does* have changes removes the class — the modal
    expands back to its normal layout.
    """
    tracker = _make_tracker_with_two_changes(tmp_path)
    # Fixture has mutations only in turn 1; turn 2 is empty.  Under
    # "keep N turns":
    #   target_turn=1 ("keep turn 1, discard turn 2+") → empty diff
    #     (turn 2 had no mutations).
    #   target_turn=0 ("session start") → full turn-1 changeset.
    # That gives us an empty turn and a non-empty turn to toggle.
    modal = RollbackModal(
        tracker=tracker,
        available_turns=[0, 1],
        cwd=str(tmp_path),
        turn_prompts={},
    )
    # Pre-select the empty turn so on_mount lands on the collapsed layout.
    modal._selected_turn = 1

    host = _ModalHostApp(modal)
    async with host.run_test() as pilot:
        await pilot.pause()
        container = host.screen.query_one("#rollback-container", VerticalGroup)
        # The container composes with ``empty`` already set, so the class
        # alone doesn't prove the preview landed — wait for readiness
        # (a Select change before that would be snapped back).
        await _wait_for_content_ready(modal, pilot)
        await _wait_for_empty_class(container, pilot, expected=True)

        # Switch to target=0 (session start) — full turn-1 changeset.
        select = host.screen.query_one("#rollback-turn-select", Select)
        select.value = 0
        await _wait_for_empty_class(container, pilot, expected=False)

        # And back: target=1 → no changes → collapsed again.
        select.value = 1
        await _wait_for_empty_class(container, pilot, expected=True)


@pytest.mark.asyncio
async def test_eol_only_rollback_preview_stays_selectable(tmp_path: Path) -> None:
    """EOL-only changes should show as selectable zero-line diff rows."""
    tracker = _make_tracker_with_eol_only_change(tmp_path)
    changed_path = str(tmp_path / "Power.yaml")
    modal = RollbackModal(
        tracker=tracker,
        available_turns=[0],
        cwd=str(tmp_path),
        turn_prompts={},
    )

    host = _ModalHostApp(modal)
    async with host.run_test() as pilot:
        await pilot.pause()
        await _wait_for_content_ready(modal, pilot)
        container = host.screen.query_one("#rollback-container", VerticalGroup)
        await _wait_for_empty_class(container, pilot, expected=False)

        pane = host.screen.query_one(DiffPeriodPane)
        assert pane.selected_paths() == [changed_path]
        checkbox = host.screen.query_one("#revert-checkbox", Checkbox)
        assert checkbox.display is True

        host.screen.query_one("#btn-rollback", Button).press()
        await wait_for(
            lambda: host.dismiss_result != "<unset>",
            pilot=pilot,
            description="EOL-only rollback dismissal",
        )
        assert host.dismiss_result == (0, True, [changed_path])


@pytest.mark.asyncio
async def test_metadata_only_rollback_preview_stays_selectable(tmp_path: Path) -> None:
    """Byte-only text changes should show as selectable zero-line diff rows."""
    tracker = _make_tracker_with_metadata_only_change(tmp_path)
    changed_path = str(tmp_path / "Power.yaml")
    modal = RollbackModal(
        tracker=tracker,
        available_turns=[0],
        cwd=str(tmp_path),
        turn_prompts={},
    )

    host = _ModalHostApp(modal)
    async with host.run_test() as pilot:
        await pilot.pause()
        await _wait_for_content_ready(modal, pilot)
        container = host.screen.query_one("#rollback-container", VerticalGroup)
        await _wait_for_empty_class(container, pilot, expected=False)

        pane = host.screen.query_one(DiffPeriodPane)
        assert pane.selected_paths() == [changed_path]
        checkbox = host.screen.query_one("#revert-checkbox", Checkbox)
        assert checkbox.display is True

        host.screen.query_one("#btn-rollback", Button).press()
        await wait_for(
            lambda: host.dismiss_result != "<unset>",
            pilot=pilot,
            description="metadata-only rollback dismissal",
        )
        assert host.dismiss_result == (0, True, [changed_path])


@pytest.mark.asyncio
async def test_turn_picker_full_chain_without_gaps(tmp_path: Path) -> None:
    """Every retained target labels its first removed turn, including target 0."""
    tracker = _make_tracker_with_two_changes(tmp_path)
    modal = RollbackModal(
        tracker=tracker,
        available_turns=[0, 1, 2, 3, 4],
        cwd=str(tmp_path),
        turn_prompts={},
    )
    host = _ModalHostApp(modal)
    async with host.run_test() as pilot:
        await pilot.pause()
        await _wait_for_content_ready(modal, pilot)
        select = host.screen.query_one("#rollback-turn-select", Select)
        # Descending engine targets, with no sentinel.
        values = [value for _prompt, value in select._options]  # type: ignore[attr-defined]
        assert values == [4, 3, 2, 1, 0]


# ---------------------------------------------------------------------------
# SnapshotPolicy-skipped content — the modal must offer exactly what the
# rollback plan can act on (WYSIWYG), including files whose content backup
# was withheld (skipped creates revert via delete; grown files restore
# from their stored before-blob).
# ---------------------------------------------------------------------------

_POLICY_CAP = 256


def _make_policy_tracker(tmp_path: Path) -> MutationTracker:
    """Tracker whose store skips backups for files larger than ``_POLICY_CAP``."""
    store = SnapshotStore(tmp_path / "session", policy=SnapshotPolicy(max_file_bytes=_POLICY_CAP))
    return MutationTracker(store)


def test_skipped_create_is_offered_as_revert_delete(tmp_path: Path) -> None:
    """A file created oversized (backup withheld) still shows in the
    rollback preview — inverted to DELETE, marked ``content_omitted`` —
    matching the plan, which reverts it by deleting (no content needed).
    """
    tracker = _make_policy_tracker(tmp_path)
    big = tmp_path / "artifact.bin"

    tracker.start_turn(1)
    mutation = tracker.record(str(big), MutationOp.CREATE, MutationSource.WRITE_FILE, "call-big")
    assert mutation is not None
    big.write_bytes(b"x" * (_POLICY_CAP + 1))
    tracker.record_after(mutation)

    entries = _entries_for_target(tracker, 0, str(tmp_path))
    assert [e.rel_path for e in entries] == ["artifact.bin"]
    entry = entries[0]
    assert entry.operation is MutationOp.DELETE
    assert entry.content_omitted == "too_large"
    # WYSIWYG with the backend plan: full rollback deletes it too.
    plan_paths = tracker.get_rollback_plan(1).paths
    assert str(big) in plan_paths


def test_skipped_binary_create_marks_reason(tmp_path: Path) -> None:
    """Binary-skipped creates carry the ``binary`` skip reason."""
    tracker = _make_policy_tracker(tmp_path)
    png = tmp_path / "img.png"

    tracker.start_turn(1)
    mutation = tracker.record(str(png), MutationOp.CREATE, MutationSource.WRITE_FILE, "call-png")
    assert mutation is not None
    png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)
    tracker.record_after(mutation)

    entries = _entries_for_target(tracker, 0, str(tmp_path))
    assert len(entries) == 1
    assert entries[0].operation is MutationOp.DELETE
    assert entries[0].content_omitted == "binary"


def test_grown_file_revert_restores_saved_before_content(tmp_path: Path) -> None:
    """A small file that grew past the cap: before-blob is stored, after
    is skipped.  The plan can restore it, so the modal shows a MODIFY
    whose inverted "after" is the saved original content.
    """
    tracker = _make_policy_tracker(tmp_path)
    path = tmp_path / "notes.txt"
    path.write_text("small original", encoding="utf-8")

    tracker.start_turn(1)
    mutation = tracker.record(str(path), MutationOp.MODIFY, MutationSource.EDIT_FILE, "call-grow")
    assert mutation is not None
    path.write_bytes(b"y" * (_POLICY_CAP + 1))
    tracker.record_after(mutation)

    entries = _entries_for_target(tracker, 0, str(tmp_path))
    assert len(entries) == 1
    entry = entries[0]
    assert entry.operation is MutationOp.MODIFY
    assert entry.content_omitted == "too_large"
    # Inverted view: right side shows what the revert writes back;
    # left side is the current (untracked) content, rendered empty.
    assert entry.after_text == "small original"
    assert entry.before_text == ""
    plan_paths = tracker.get_rollback_plan(1).paths
    assert str(path) in plan_paths


def test_preexisting_oversized_modify_stays_hidden(tmp_path: Path) -> None:
    """A file already oversized before the turn has no restorable before
    state — the plan excludes it, so the modal must not offer it either.
    """
    tracker = _make_policy_tracker(tmp_path)
    path = tmp_path / "big.bin"
    path.write_bytes(b"x" * (_POLICY_CAP + 1))

    tracker.start_turn(1)
    mutation = tracker.record(str(path), MutationOp.MODIFY, MutationSource.EDIT_FILE, "call-big-edit")
    assert mutation is not None
    path.write_bytes(b"y" * (_POLICY_CAP + 2))
    tracker.record_after(mutation)

    assert _entries_for_target(tracker, 0, str(tmp_path)) == []
    assert tracker.get_rollback_plan(1).entries == []


@pytest.mark.asyncio
async def test_modal_offers_skipped_create_for_revert(tmp_path: Path) -> None:
    """End-to-end: a skipped create is selectable in the modal and its
    path lands in the confirmed revert selection.
    """
    tracker = _make_policy_tracker(tmp_path)
    big = tmp_path / "artifact.bin"
    small = tmp_path / "a.txt"
    small.write_text("a-original", encoding="utf-8")

    tracker.start_turn(1)
    mut_big = tracker.record(str(big), MutationOp.CREATE, MutationSource.WRITE_FILE, "call-big")
    assert mut_big is not None
    big.write_bytes(b"x" * (_POLICY_CAP + 1))
    tracker.record_after(mut_big)
    mut_small = tracker.record(str(small), MutationOp.MODIFY, MutationSource.EDIT_FILE, "call-a")
    assert mut_small is not None
    small.write_text("a-changed", encoding="utf-8")
    tracker.record_after(mut_small)
    tracker.start_turn(2)

    modal = _mk_modal(tracker, tmp_path)
    modal._selected_turn = 0  # session start → aggregates turn 1
    host = _ModalHostApp(modal)
    async with host.run_test() as pilot:
        await pilot.pause()
        await _wait_for_content_ready(modal, pilot)
        pane = host.screen.query_one(DiffPeriodPane)
        assert sorted(pane.selected_paths()) == sorted([str(big), str(small)])
        host.screen.query_one("#btn-rollback", Button).press()
        await pilot.pause()

    target, revert, paths = host.dismiss_result
    assert target == 0
    assert revert is True
    assert paths is not None
    assert str(big) in paths


def test_modal_hides_binary_move_destination(tmp_path: Path) -> None:
    """A binary ``mv`` cannot be rolled back (source content was never
    backed up) — the plan excludes both sides, so the modal must not
    offer the destination's delete row either (deleting it would
    destroy the only copy of the moved content).
    """
    tracker = _make_policy_tracker(tmp_path)
    src = tmp_path / "src.bin"
    dst = tmp_path / "dst.bin"
    src.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)

    tracker.start_turn(1)
    tracker.pre_snapshot([str(src), str(dst)])
    src.rename(dst)
    mutation = tracker.record(str(dst), MutationOp.MOVE, MutationSource.SHELL, "call-mv", old_path=str(src))
    assert mutation is not None

    assert _entries_for_target(tracker, 0, str(tmp_path)) == []
    # Sanity: the modal mirrors the plan, which is also empty.
    assert tracker.get_rollback_plan_for_turns({1}).entries == []


def test_exclusion_reason_label_accepts_enum_and_raw_strings() -> None:
    """Event payloads carry raw ``.value`` strings; unknown values pass through."""
    contested = exclusion_reason_label(RollbackExclusionReason.CONTESTED)
    raw_contested = exclusion_reason_label("contested")
    foreign = exclusion_reason_label("foreign")
    assert isinstance(contested, MessageRef)
    assert isinstance(raw_contested, MessageRef)
    assert isinstance(foreign, MessageRef)
    assert format_message(contested) == "modified internally and externally"
    assert format_message(raw_contested) == "modified internally and externally"
    assert format_message(foreign) == "changed by another session"
    assert exclusion_reason_label("some_future_reason") == "some_future_reason"


@pytest.mark.asyncio
async def test_plan_augment_drops_peer_excluded_entries_and_notes_them(tmp_path: Path) -> None:
    """The cross-session peer check reshapes the preview (plan-WYSIWYG).

    ``plan_augment`` mirrors the engine's rollback-time augmentation:
    a path it moves to ``PEER_MODIFIED_SINCE`` must disappear from the
    checkable change list AND appear in the exclusion note — otherwise
    the modal offers a revert the backend will refuse.
    """
    from textual.widgets import Static

    from chrys.service.mutations.types import RollbackPlan

    tracker = _make_tracker_with_two_changes(tmp_path)
    excluded_path = str(tmp_path / "a.txt")
    augment_calls: list[str] = []

    def _augment(plan: RollbackPlan, cwd: str) -> RollbackPlan:
        augment_calls.append(cwd)
        kept = [(path, snap) for path, snap in plan.entries if path != excluded_path]
        return RollbackPlan(
            entries=kept,
            exclusions=[*plan.exclusions, (excluded_path, RollbackExclusionReason.PEER_MODIFIED_SINCE)],
            warnings=list(plan.warnings),
        )

    modal = RollbackModal(
        tracker=tracker,
        available_turns=[0, 1],
        cwd=str(tmp_path),
        turn_prompts={},
        plan_augment=_augment,
    )
    modal._selected_turn = 0  # revert everything from turn 1

    host = _ModalHostApp(modal)
    async with host.run_test() as pilot:
        await pilot.pause()
        await _wait_for_content_ready(modal, pilot)
        assert augment_calls == [str(tmp_path)]
        pane = host.screen.query_one(DiffPeriodPane)
        offered = {entry.path for entry in pane._entries}
        assert excluded_path not in offered
        assert str(tmp_path / "b.txt") in offered
        note = host.screen.query_one("#rollback-exclusions-note", Static)
        note_text = str(note.render())
        assert "a.txt" in note_text
        assert "changed by another session since" in note_text


@pytest.mark.asyncio
async def test_attribution_refresh_runs_before_plan_build(tmp_path: Path) -> None:
    """A peer claim landing after the modal opens can demote local rows
    (contested/foreign), which reshapes the plan itself — augmentation
    can't surface that.  ``attribution_refresh`` must therefore run
    BEFORE the tracker is read: a demotion it applies is already
    reflected in the very first preview.
    """
    from chrys.service.mutations.types import MutationProvenance

    tracker = _make_tracker_with_two_changes(tmp_path)
    demoted_path = str(tmp_path / "a.txt")
    refresh_calls: list[bool] = []

    async def _refresh() -> bool:
        refresh_calls.append(True)

        def demote(log):
            for turn in log.turns:
                for mutation in turn.mutations:
                    if mutation.path == demoted_path:
                        mutation.provenance = MutationProvenance.FOREIGN
            return True

        return tracker.with_locked_log(demote)

    modal = RollbackModal(
        tracker=tracker,
        available_turns=[0, 1],
        cwd=str(tmp_path),
        turn_prompts={},
        attribution_refresh=_refresh,
    )
    modal._selected_turn = 0  # revert everything from turn 1

    host = _ModalHostApp(modal)
    async with host.run_test() as pilot:
        await pilot.pause()
        await _wait_for_content_ready(modal, pilot)
        assert refresh_calls == [True]
        pane = host.screen.query_one(DiffPeriodPane)
        offered = {entry.path for entry in pane._entries}
        assert demoted_path not in offered
        assert str(tmp_path / "b.txt") in offered


@pytest.mark.asyncio
async def test_plan_warnings_surface_in_modal_before_confirm(tmp_path: Path) -> None:
    """Peer-active warnings must show in the preview, not only in the
    post-rollback toast — the destructive confirm cannot be the first
    place the user learns another session is mid-command.
    """
    from textual.widgets import Static

    from chrys.service.mutations.types import RollbackPlan, RollbackWarning

    tracker = _make_tracker_with_two_changes(tmp_path)
    warning = RollbackWarning(
        code="peer_active_window",
        message="Another iCode session has an active command in this workspace; its changes may not be attributed yet.",
    )

    def _augment(plan: RollbackPlan, _cwd: str) -> RollbackPlan:
        # Two peers → coordinator emits two identical messages; the
        # note must dedupe to one line.
        return RollbackPlan(
            entries=list(plan.entries),
            exclusions=list(plan.exclusions),
            warnings=[*plan.warnings, warning, warning],
        )

    modal = RollbackModal(
        tracker=tracker,
        available_turns=[0, 1],
        cwd=str(tmp_path),
        turn_prompts={},
        plan_augment=_augment,
    )
    modal._selected_turn = 0

    host = _ModalHostApp(modal)
    async with host.run_test() as pilot:
        await pilot.pause()
        await _wait_for_content_ready(modal, pilot)
        # Entries still offered — warnings are advisory, not exclusions.
        pane = host.screen.query_one(DiffPeriodPane)
        assert len(pane._entries) == 2
        note = host.screen.query_one("#rollback-warnings-note", Static)
        note_text = str(note.render())
        assert "active command" in note_text
        assert note_text.count("active command") == 1
