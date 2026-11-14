# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Leaving a preview, the diff viewer or the rollback modal while its load removes widgets lets that removal finish.

Leaving cancels the load's worker. Each test holds a child of a widget the load removes, so the
removal is still waiting for that child when the user leaves.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import pytest
from textual.app import App, ComposeResult
from textual.containers import Vertical
from textual.widget import Widget
from textual.widgets import Static
from textual.worker import Worker, WorkerCancelled

from chrys.app.tui.screens.dialogs.approval.bodies.file_edit import ApprovalDiffPreview
from chrys.app.tui.screens.diff.rollback_modal import RollbackModal
from chrys.app.tui.screens.diff.screen import DiffScreen
from chrys.app.tui.util.diff_entries import DiffFileEntry, DiffLoadResult
from chrys.app.tui.widgets.chat.tool_view_builders import ToolViewDiff
from chrys.app.tui.widgets.diff_view import DiffView
from chrys.service.mutations.store import SnapshotStore
from chrys.service.mutations.tracker import MutationTracker
from chrys.service.mutations.types import MutationOp, MutationSource
from tests.support.tui_helpers import BusyWidget, assert_app_handles_messages, interrupt_removal
from tests.support.waiting import wait_for


class _HostApp(App):
    def __init__(self, screen: DiffScreen | RollbackModal) -> None:
        super().__init__()
        self._screen = screen
        self.dismiss_result: object = "<unset>"

    def compose(self) -> ComposeResult:
        return iter([])

    def on_mount(self) -> None:
        def on_dismiss(result: object) -> None:
            self.dismiss_result = result

        self.push_screen(self._screen, on_dismiss)


def _tracker(tmp_path: Path) -> MutationTracker:
    tracker = MutationTracker(SnapshotStore(tmp_path))
    path = tmp_path / "a.txt"
    path.write_text("before", encoding="utf-8")
    tracker.start_turn(1)
    mutation = tracker.record(str(path), MutationOp.MODIFY, MutationSource.EDIT_FILE, "call-a")
    assert mutation is not None
    path.write_text("after", encoding="utf-8")
    tracker.record_after(mutation)
    tracker.start_turn(2)
    return tracker


def _rollback_state(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        tracker=_tracker(tmp_path),
        available_turns=[0, 1],
        current_turn=2,
        workspace_cwd=str(tmp_path),
        turn_prompts={},
        plan_augment=None,
        attribution_refresh=None,
        projection_acquire=None,
        projection_release=None,
    )


@pytest.mark.asyncio
async def test_leaving_the_diff_viewer_while_its_loading_shell_is_removed() -> None:
    release_load = asyncio.Event()
    entry = DiffFileEntry(
        path="/repo/a.py",
        rel_path="a.py",
        operation=MutationOp.MODIFY,
        old_path=None,
        before_text="before",
        after_text="after",
        is_binary=False,
        encoding="utf-8",
        bytes_changed=True,
    )

    async def load_data() -> DiffLoadResult:
        await release_load.wait()
        return DiffLoadResult(all_entries=[entry], per_period_entries={1: [entry]}, total_periods=1)

    screen = DiffScreen({}, cwd="/repo", load_data=load_data)
    app = _HostApp(screen)
    async with app.run_test() as pilot:
        await wait_for(lambda: app.screen is screen and screen.is_mounted, pilot=pilot)
        loading_state = screen.query_one("#diff-loading-state")
        busy = BusyWidget()
        await loading_state.mount(busy)

        await interrupt_removal(busy, release_load.set, screen.action_go_back)

        await wait_for(lambda: not screen.is_attached, description="the diff viewer closes")
        assert not loading_state.is_attached
        assert not busy.is_attached
        await assert_app_handles_messages(app)


@pytest.mark.asyncio
async def test_closing_the_rollback_modal_while_its_preview_slot_is_cleared(tmp_path: Path) -> None:
    release_refresh = asyncio.Event()

    async def refresh_attribution() -> bool:
        await release_refresh.wait()
        return False

    modal = RollbackModal(
        tracker=_tracker(tmp_path),
        available_turns=[0, 1],
        cwd=str(tmp_path),
        attribution_refresh=refresh_attribution,
    )
    app = _HostApp(modal)
    async with app.run_test() as pilot:
        await wait_for(lambda: app.screen is modal and modal.is_mounted, pilot=pilot)
        busy = BusyWidget()
        holder = Vertical(busy)
        await modal.query_one("#rollback-diff-wrapper").mount(holder)

        await interrupt_removal(busy, release_refresh.set, modal.action_close)

        await wait_for(lambda: not modal.is_attached, description="the rollback modal closes")
        assert app.dismiss_result is None
        assert not holder.is_attached
        assert not busy.is_attached
        await assert_app_handles_messages(app)


@pytest.mark.asyncio
async def test_closing_the_rollback_modal_while_its_loading_shell_is_removed(tmp_path: Path) -> None:
    release_state = asyncio.Event()

    async def load_state() -> SimpleNamespace:
        await release_state.wait()
        return _rollback_state(tmp_path)

    modal = RollbackModal(tracker=None, available_turns=[], cwd=str(tmp_path), load_state=load_state)
    app = _HostApp(modal)
    async with app.run_test() as pilot:
        await wait_for(lambda: app.screen is modal and modal.is_mounted, pilot=pilot)
        loading_state = modal.query_one("#rollback-initial-loading-state")
        busy = BusyWidget()
        await loading_state.mount(busy)

        await interrupt_removal(busy, release_state.set, modal.action_close)

        await wait_for(lambda: not modal.is_attached, description="the rollback modal closes")
        assert app.dismiss_result is None
        assert not loading_state.is_attached
        assert not busy.is_attached
        await assert_app_handles_messages(app)


class _PreviewApp(App):
    """Host a diff preview as the only screen content."""

    def __init__(self, preview: Widget) -> None:
        super().__init__()
        self._preview = preview

    def compose(self) -> ComposeResult:
        yield self._preview


def _approval_diff_preview() -> Widget:
    return ApprovalDiffPreview("a.py", "before\n", "after\n", title="Planned Diff")


def _tool_card_diff_preview() -> Widget:
    return ToolViewDiff("a.py", "before\n", "after\n")


@pytest.mark.parametrize(
    ("preview_factory", "placeholder_class"),
    [
        pytest.param(_approval_diff_preview, "approval-diff-placeholder", id="approval-diff"),
        pytest.param(_tool_card_diff_preview, "tool-view-diff-placeholder", id="tool-view-diff"),
    ],
)
@pytest.mark.asyncio
async def test_cancelling_a_diff_preview_while_its_placeholder_is_removed(
    preview_factory: Callable[[], Widget],
    placeholder_class: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release_prepare = asyncio.Event()
    original_prepare = DiffView.prepare

    async def prepare(self: DiffView) -> None:
        await release_prepare.wait()
        await original_prepare(self)

    monkeypatch.setattr(DiffView, "prepare", prepare)
    preview = preview_factory()
    app = _PreviewApp(preview)
    async with app.run_test() as pilot:
        await wait_for(lambda: preview.is_mounted, pilot=pilot, description="the diff preview mounts")
        placeholder = preview.query_one(f".{placeholder_class}", Static)
        busy = BusyWidget()
        await placeholder.mount(busy)
        workers: list[Worker[object]] = []

        def cancel_preview_worker() -> None:
            workers.extend(preview.workers.cancel_node(preview))

        # The worker reaches its placeholder removal once the preparation gate opens; the removal
        # then waits for `busy`, and the cancel lands while the worker awaits that removal.
        await interrupt_removal(busy, release_prepare.set, cancel_preview_worker)

        await wait_for(lambda: not placeholder.is_attached, description="the placeholder still leaves")
        assert not busy.is_attached
        assert preview.query(DiffView)
        await assert_app_handles_messages(app)
        assert len(workers) == 1
        with pytest.raises(WorkerCancelled):
            await workers[0].wait()
