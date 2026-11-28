# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tail-first replay: the newest entries mount before replay returns, older history is prepended above them."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable
from contextlib import ExitStack
from functools import partial
from pathlib import Path

import pytest
from textual import events
from textual.geometry import Offset, Size
from textual.layout import DockArrangeResult
from textual.scrollbar import ScrollTo
from textual.selection import SELECT_ALL
from textual.widget import AwaitMount, Widget
from textual.widgets import Static

from chrys.app.tui.widgets.chat import history_scroll as history_scroll_module
from chrys.app.tui.widgets.chat.context_fold import ContextFoldWidget
from chrys.app.tui.widgets.chat.messages import (
    AgentMessage,
    ErrorMessage,
    MessageRollbackButton,
    ThinkingMessage,
    UserMessage,
)
from chrys.app.tui.widgets.chat.panel import ChatPanel
from chrys.app.tui.widgets.chat.replay_mount import REPLAY_MOUNT_BATCH_SIZE
from chrys.app.tui.widgets.chat.tool_call import ToolGroup, ToolGroupTitle
from chrys.app.tui.widgets.markdown.widget import VirtualizedMarkdown
from chrys.app.tui.widgets.welcome import WelcomeWidget
from chrys.foundation.models.history_markers import ANSWER_SEGMENT_KEY, HistoryMarkerKind
from chrys.foundation.tool_kinds import KIND_FILESYSTEM_READ, TOOL_CALL_KIND_METADATA_KEY
from chrys.foundation.trajectory.metadata import ANALYTICS_ITEM_ID_KEY
from chrys.kernel import Content, Message
from chrys.service.context.providers.history import CompressedBlock
from chrys.service.state.folds import FoldItem
from tests.app.tui.widgets._scroll_gc import install_fake_chat_panel_gc
from tests.app.tui.widgets.chat.replay_fixtures import _SIZE, _TURNS, _PrependGate, _settle, _turns, _wait_complete
from tests.support.tui_helpers import (
    ChatPanelApp,
    _simulate_chat_panel_user_scroll_y,
    chat_content_children,
    title_text,
)
from tests.support.waiting import DEFAULT_WAIT_TIMEOUT, wait_for, wait_until

pytestmark = pytest.mark.usefixtures("frozen_earlier_heap")

# At _SIZE either history replays as a tail batch and two prepend batches,
# so the first and the last prepended batch differ.
_TOOL_TURNS = 24


def _expected_messages(count: int = _TURNS) -> list[tuple[str, str]]:
    expected: list[tuple[str, str]] = []
    for index in range(count):
        expected.append(("You", f"question {index}"))
        expected.append(("Agent", f"answer {index}\n\nmore about {index}"))
    return expected


def _message_texts(panel: ChatPanel) -> list[str]:
    return [
        child.text if isinstance(child, AgentMessage) else child._text
        for child in panel.children
        if isinstance(child, (UserMessage, AgentMessage))
    ]


def _assert_document_order(panel: ChatPanel) -> None:
    seqs = [panel.transcript_seq(child) for child in chat_content_children(panel)]
    assert None not in seqs
    assert seqs == sorted(seqs)
    assert len(set(seqs)) == len(seqs)


def _placement(before: Widget | None, after: Widget | None) -> dict[str, Widget]:
    """The placement a wrapped ``mount`` was given: an explicit ``None`` would skip the panel's own place above its chrome."""
    return {key: widget for key, widget in (("before", before), ("after", after)) if widget is not None}


def _hold_refresh_callbacks(
    panel: ChatPanel, monkeypatch: pytest.MonkeyPatch
) -> list[tuple[Callable[..., object], tuple[object, ...]]]:
    """Hold what the prepend queues for the next refresh, as a slow frame would."""
    queued: list[tuple[Callable[..., object], tuple[object, ...]]] = []
    controller = panel._replay_mount
    after_batch_mounted = controller._after_batch_mounted

    def hold_refresh_callbacks(batch: list[Widget]) -> None:
        with monkeypatch.context() as patch:
            patch.setattr(panel, "call_after_refresh", lambda callback, *args: queued.append((callback, args)) or True)
            after_batch_mounted(batch)

    monkeypatch.setattr(controller, "_after_batch_mounted", hold_refresh_callbacks)
    return queued


async def test_long_replay_keeps_only_the_viewport_in_layout() -> None:
    count = 120
    async with ChatPanelApp().run_test(size=_SIZE) as pilot:
        cp = pilot.app.query_one(ChatPanel)
        await cp.replay_history(_turns(count, with_tools=True))
        await _settle(cp, pilot)

        viewport = cp._history_viewport
        assert viewport is not None
        assert cp._history_window is not None
        placement_count = len(cp.arrange(cp.content_size).placements)
        pending_count = len(cp.pending_transcript_widgets())
        mounted_count = len(chat_content_children(cp))
        assert placement_count < (pending_count + mounted_count) // 3
        assert cp.get_all_messages() == _expected_messages(count)
        first = viewport.turn("turn-1")
        last = cp.query_one(f"#turn-{count}", UserMessage)
        assert first is not None
        assert cp.content_region.overlaps(last.region)
        assert not first.is_attached

        cp.scroll_home(immediate=True, animate=False)
        await wait_for(
            lambda: cp.content_region.overlaps(first.region),
            pilot=pilot,
            description="first page mounts and becomes visible",
        )
        await _settle(cp, pilot)
        assert not last.region or not cp.content_region.overlaps(last.region)
        measured_height = cp.virtual_size.height
        assert 0 < len(cp.pending_transcript_widgets()) < pending_count

        cp.scroll_end(immediate=True, animate=False)
        await _settle(cp, pilot)
        assert cp.content_region.overlaps(last.region)
        assert cp.scroll_y == cp.max_scroll_y
        assert cp.virtual_size.height == measured_height
        assert first.is_attached and not first.region
        placements = cp.arrange(cp.content_size).placements
        assert all(placement.widget is not first for placement in placements)
        assert len(placements) < len(chat_content_children(cp))


async def test_replay_viewport_keeps_turn_anchor_at_margin_on_widen_and_selection() -> None:
    messages = _turns(60)
    for message in messages:
        if message["role"] == "assistant":
            message["contents"] = [{"type": "text", "text": "wrapped prose " * 270}]

    async with ChatPanelApp().run_test(size=(45, 24)) as pilot:
        cp = pilot.app.query_one(ChatPanel)
        await cp.replay_history(messages)
        await _wait_complete(cp)
        await _settle(cp, pilot)
        cp.refresh(layout=True)
        await _settle(cp, pilot)
        assert cp._history_window is not None

        message = cp.query_one("#turn-31", UserMessage)
        cp.scroll_to_turn("turn-31")
        await _settle(cp, pilot)

        # A row of margin above the message has no placement of its own.
        margin = message.region.y - cp.content_region.y
        assert margin == 1

        await pilot.resize_terminal(140, 24)
        await _settle(cp, pilot)
        assert message.region.y - cp.content_region.y == margin

        # Preparing the entire document for exact cross-entry copy means real
        # heights replace estimates even in pages we have never visited at this width.
        leaf = message.query_one(VirtualizedMarkdown)
        cp.screen.selections = {leaf: SELECT_ALL}
        cp.history_selection_changed(True)
        await _settle(cp, pilot)
        assert cp._history_window is None
        assert message.region.y - cp.content_region.y == margin

        cp.screen.selections = {}
        cp.history_selection_changed(False)
        await _settle(cp, pilot)
        assert cp._history_window is not None
        assert message.region.y - cp.content_region.y == margin


async def test_exact_history_layout_keeps_its_anchor_during_a_nested_geometry_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    messages = _turns(80)
    # One cold page with a misleading estimate is enough to change the anchor
    # when native layout takes over.
    url = "https://example.com/" + "x" * 1800
    messages[1]["contents"] = [{"type": "text", "text": f"[link]({url})"}]

    async with ChatPanelApp().run_test(size=_SIZE) as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.replay_history(messages)
        await _settle(panel, pilot)
        panel.scroll_to_turn("turn-55")
        await _settle(panel, pilot)
        anchor = panel.query_one("#turn-55", UserMessage)
        assert anchor.region.y == panel.content_region.y + 1
        assert panel.pending_transcript_widgets()
        estimated_y = panel.scroll_y

        native_arrange = Widget.arrange
        queried_during_layout = False

        def arrange_with_geometry_read(widget: Widget, size: Size, optimal: bool = False) -> DockArrangeResult:
            nonlocal queried_during_layout
            if widget is panel and not optimal and not queried_during_layout:
                # A measuring descendant can read the panel's geometry here.
                queried_during_layout = True
                widget.arrange(size)
            return native_arrange(widget, size, optimal)

        monkeypatch.setattr(Widget, "arrange", arrange_with_geometry_read)
        panel.history_selection_changed(True)
        await _wait_complete(panel)
        await _settle(panel, pilot)

        assert queried_during_layout
        assert panel._history_window is None
        assert panel.scroll_y != estimated_y
        assert anchor.region.y == panel.content_region.y + 1


async def test_exact_history_reuses_its_capture_when_native_layout_is_cached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with ChatPanelApp().run_test(size=_SIZE) as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.replay_history(_turns(120))
        await _settle(panel, pilot)
        await _wait_complete(panel)
        await _settle(panel, pilot)
        panel.history_selection_changed(True)
        await _settle(panel, pilot)
        viewport = panel._history_viewport
        assert viewport is not None
        size = panel.content_size
        result = panel.arrange(size)
        assert viewport.measured and viewport.is_current(result)
        capture = viewport.capture
        captures = []

        def record_capture(result: DockArrangeResult, width: int, anchor_row: int | None = None) -> int:
            captures.append(result)
            return capture(result, width, anchor_row)

        monkeypatch.setattr(viewport, "capture", record_capture)
        assert panel.arrange(size) is result
        assert captures == []


async def test_rollback_preview_does_not_disable_windowed_history_layout() -> None:
    async with ChatPanelApp().run_test(size=_SIZE) as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.replay_history(_turns(120), rollback_targets={119})
        await _settle(panel, pilot)
        await _wait_complete(panel)
        await _settle(panel, pilot)
        assert panel._history_window is not None
        prompt = panel.query_one("#turn-120", UserMessage)
        button = prompt.query_one(MessageRollbackButton)
        await pilot.hover(button)
        await wait_for(
            lambda: panel._rollback_preview_start is prompt,
            pilot=pilot,
            description="active rollback hover",
        )

        panel.refresh(layout=True)
        await _settle(panel, pilot)
        assert panel._rollback_preview_start is prompt
        assert panel._history_window is not None


async def test_replay_viewport_reuses_extent_at_a_previously_measured_width() -> None:
    messages = _turns(60)
    for message in messages:
        if message["role"] == "assistant":
            message["contents"] = [{"type": "text", "text": "wrapped prose " * 190}]

    async with ChatPanelApp().run_test(size=(45, 24)) as pilot:
        cp = pilot.app.query_one(ChatPanel)
        await cp.replay_history(messages)
        await _wait_complete(cp)
        await _settle(cp, pilot)
        cp.refresh(layout=True)
        await _settle(cp, pilot)
        exact_height = cp.virtual_size.height

        await pilot.resize_terminal(140, 24)
        await _settle(cp, pilot)
        cp.scroll_to_turn("turn-21")
        await _settle(cp, pilot)
        cp.scroll_end(immediate=True, animate=False)
        await _settle(cp, pilot)
        await pilot.resize_terminal(45, 24)
        await _settle(cp, pilot)

        assert cp.max_scroll_y == cp.scroll_y
        assert cp.virtual_size.height == exact_height


async def test_replay_viewport_cold_toc_near_bottom_after_resize() -> None:
    messages = _turns(40)
    for message in messages:
        if message["role"] == "assistant":
            message["contents"] = [{"type": "text", "text": "word " * 120}]

    async with ChatPanelApp().run_test(size=(35, 50)) as pilot:
        cp = pilot.app.query_one(ChatPanel)
        await cp.replay_history(messages)
        await _wait_complete(cp)
        await _settle(cp, pilot)
        cp.refresh(layout=True)
        await _settle(cp, pilot)
        cp.scroll_home(immediate=True, animate=False)
        await _settle(cp, pilot)
        await pilot.resize_terminal(180, 50)
        await _settle(cp, pilot)

        child = cp.query_one("#turn-39", UserMessage)
        cp.scroll_to_turn("turn-39")
        await _settle(cp, pilot)
        assert child.region.overlaps(cp.content_region)
        assert child.has_class("-highlighted")
        assert cp.max_scroll_y == cp.scroll_y


async def test_toc_jump_to_bottom_updates_the_scroll_affordance() -> None:
    async with ChatPanelApp().run_test(size=_SIZE) as pilot:
        cp = pilot.app.query_one(ChatPanel)
        await cp.replay_history(_turns(60))
        await _wait_complete(cp)
        await _settle(cp, pilot)
        cp.scroll_home(immediate=True, animate=False)
        await _settle(cp, pilot)
        button = cp.scroll_to_bottom_button()
        assert button is not None and button.visible

        cp.scroll_to_turn("turn-59")
        await _settle(cp, pilot)
        assert cp.scroll_y == cp.max_scroll_y
        assert not button.visible


@pytest.mark.parametrize("with_prefix", [False, True], ids=["first-user", "leading-summary"])
async def test_cold_history_mounts_only_demanded_pages_and_keeps_full_document_copy(with_prefix: bool) -> None:
    count = 120
    history = _turns(count, with_tools=True, leading_summary="earlier conversation" if with_prefix else None)
    async with ChatPanelApp().run_test(size=_SIZE) as pilot:
        cp = pilot.app.query_one(ChatPanel)
        await cp.replay_history(history)
        await _settle(cp, pilot)

        assert cp._replay_mount.windowed
        assert len(cp.transcript_content_children()) <= 4 * REPLAY_MOUNT_BATCH_SIZE
        assert len(cp.query(UserMessage)) < count // 4
        assert cp.get_all_messages() == _expected_messages(count)
        assert cp.query_one("#turn-120").region.overlaps(cp.content_region)
        marker = cp.query_one(ContextFoldWidget) if with_prefix else None
        if marker is not None:
            assert cp.transcript_content_children()[0] is marker

        target = next(widget for widget in cp.pending_transcript_widgets() if widget.id == "turn-17")
        cp.scroll_to_turn("turn-17")
        await wait_for(
            lambda: (
                target.is_attached
                and cp._history_scroll.pending is None
                and target.region.y == cp.content_region.y + 1
                and bool(target.region)
            ),
            pilot=pilot,
            description="cold TOC page mounted and landed at its real row",
        )
        assert len(cp.query(UserMessage)) < count // 2
        assert cp.get_all_messages() == _expected_messages(count)

        first = next(widget for widget in cp.pending_transcript_widgets() if widget.id == "turn-1")
        cp.action_scroll_home()
        await wait_for(
            lambda: first.is_attached and first.region.overlaps(cp.content_region),
            pilot=pilot,
            description="scroll home prepared the original first turn",
        )
        assert target.is_attached
        assert cp.get_all_messages() == _expected_messages(count)
        if marker is not None:
            assert marker.region.overlaps(cp.content_region)
            assert marker.virtual_region.bottom <= first.virtual_region.y


async def test_scroll_inside_a_mounted_window_does_not_relayout_for_cold_overscan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with ChatPanelApp().run_test(size=_SIZE) as pilot:
        cp = pilot.app.query_one(ChatPanel)
        await cp.replay_history(_turns(120))
        await _settle(cp, pilot)
        viewport = cp._history_viewport
        window = cp._history_window
        assert viewport is not None and window is not None
        first_mounted = cp.query(UserMessage).first()
        row = first_mounted.virtual_region.y
        required = viewport.window(row, cp.size.height, following=False, overscan=0)
        overscan = viewport.window(row, cp.size.height, following=False)
        assert window[0] <= required[0] and required[1] <= window[1]
        assert overscan[0] < window[0]
        refresh = cp.refresh
        layout_requests = []

        def watch_refresh(*regions, **kwargs):
            if kwargs.get("layout"):
                layout_requests.append(True)
            return refresh(*regions, **kwargs)

        monkeypatch.setattr(cp, "refresh", watch_refresh)
        cp.release_anchor()
        cp.scroll_to(y=row, animate=False, immediate=True)

        assert cp.scroll_y == row
        assert layout_requests == []
        assert not cp._replay_mount.needs_mount(viewport.widgets_in(required))


async def test_cold_restore_ignores_geometry_reads_before_the_tail_is_sized(monkeypatch: pytest.MonkeyPatch) -> None:
    async with ChatPanelApp().run_test(size=_SIZE) as pilot:
        cp = pilot.app.query_one(ChatPanel)
        finalize = cp.after_replay_history_mounted

        def request_geometry_during_restore() -> None:
            finalize()
            previous = cp._anchor_released
            # Visibility-only compositor reflows temporarily release the
            # anchor without dispatching a user's scroll or a size update.
            cp._anchor_released = True
            cp.arrange(cp.content_region.size)
            cp._anchor_released = previous

        monkeypatch.setattr(cp, "after_replay_history_mounted", request_geometry_during_restore)
        await cp.replay_history(_turns(120, with_tools=True))
        await _settle(cp, pilot)
        assert cp.scroll_y == cp.max_scroll_y
        assert cp.query_one("#turn-120").region.overlaps(cp.content_region)
        assert not cp.query("#turn-1")


async def test_cold_toc_holds_and_lands_only_the_latest_jump(monkeypatch: pytest.MonkeyPatch) -> None:
    async with ChatPanelApp().run_test(size=_SIZE) as pilot:
        cp = pilot.app.query_one(ChatPanel)
        await cp.replay_history(_turns(100))
        await _settle(cp, pilot)
        last = cp.query_one("#turn-100", UserMessage)
        assert last.region.overlaps(cp.content_region)
        gate = _PrependGate(cp, monkeypatch)

        cp.scroll_to_turn("turn-1")
        try:
            await gate.wait_held(pilot)
            assert last.region.overlaps(cp.content_region)
            cp.scroll_to_turn("turn-55")
            await pilot.pause()
            assert last.region.overlaps(cp.content_region)
        finally:
            gate.release.set()

        target = next(widget for widget in cp.pending_transcript_widgets() if widget.id == "turn-55")
        await wait_for(
            lambda: target.is_attached and target.region.y == cp.content_region.y + 1 and bool(target.region),
            pilot=pilot,
            description="replacement TOC target is visible",
        )
        assert [widget.id for widget in cp.query(UserMessage).filter(".-highlighted")] == ["turn-55"]


async def test_cold_layout_holds_the_reading_anchor_until_mount_publishes_it(monkeypatch: pytest.MonkeyPatch) -> None:
    messages = _turns(120)
    for message in messages:
        if message["role"] == "assistant":
            message["contents"] = [{"type": "text", "text": f"[doc](https://example.com/{'x' * 1600})"}]

    async with ChatPanelApp().run_test(size=_SIZE) as pilot:
        cp = pilot.app.query_one(ChatPanel)
        await cp.replay_history(messages)
        await _settle(cp, pilot)
        viewport = cp._history_viewport
        assert viewport is not None
        target = next(widget for widget in cp.pending_transcript_widgets() if widget.id == "turn-25")
        target_row = viewport.page_row(target)
        assert target_row is not None
        held_layout = cp._history_scroll.visible_layout
        held_window = cp._history_window
        held_row = cp._history_scroll.visible_row
        ready_to_publish = asyncio.Event()
        publish = asyncio.Event()
        mount_batch = cp._replay_mount._mount_window_batch

        async def gate_final_batch(batch: list[Widget]) -> None:
            nonlocal held_row
            await mount_batch(batch)
            request = cp._history_scroll.pending
            if request is not None and not cp._replay_mount.needs_mount(viewport.widgets_in(request.window)):
                held_row = cp._history_scroll.visible_row
                ready_to_publish.set()
                await publish.wait()

        monkeypatch.setattr(cp._replay_mount, "_mount_window_batch", gate_final_batch)
        cp.release_anchor()
        cp.vertical_scrollbar.post_message(ScrollTo(y=target_row, animate=False))
        try:
            await wait_for(ready_to_publish.is_set, pilot=pilot, description="the cold window is initialized")
            cp.refresh(layout=True)
            await pilot.pause()
            assert cp._history_window == held_window
            assert cp._history_scroll.visible_layout is held_layout
            assert cp.scroll_y == held_row
        finally:
            publish.set()

        await wait_for(
            lambda: cp._history_scroll.pending is None and target.region.overlaps(cp.content_region),
            pilot=pilot,
            description="the cold window is published at the requested turn",
        )


async def test_shrinking_a_cold_page_keeps_the_published_layout_until_its_neighbor_mounts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    messages = _turns(120)
    for message in messages:
        if message["role"] == "assistant":
            message["contents"] = [{"type": "text", "text": f"[doc](https://example.com/{'x' * 1600})"}]

    async with ChatPanelApp().run_test(size=_SIZE) as pilot:
        cp = pilot.app.query_one(ChatPanel)
        await cp.replay_history(messages)
        await _settle(cp, pilot)
        viewport = cp._history_viewport
        assert viewport is not None
        next_page = next(widget for widget in cp.pending_transcript_widgets() if widget.id == "turn-29")
        next_row = viewport.page_row(next_page)
        assert next_row is not None
        published = viewport.last_layout
        assert published is not None
        target = next_row - cp.scrollable_content_region.height - 1
        requests = []
        request_widgets = cp._history_scroll.request_widgets

        def hold_second_page(request, widgets):
            requests.append(request)
            if len(requests) == 1:
                request_widgets(request, widgets)

        monkeypatch.setattr(cp._history_scroll, "request_widgets", hold_second_page)
        cp.vertical_scrollbar.post_message(ScrollTo(y=target, animate=False))
        await wait_for(lambda: len(requests) == 2, pilot=pilot, description="the measured page exposes a cold neighbor")

        assert requests[1].window[1] > requests[0].window[1]
        assert cp._history_scroll.visible_layout is published
        assert viewport.last_layout is not published


async def test_cold_scrollbar_release_does_not_cancel_the_requested_view(monkeypatch: pytest.MonkeyPatch) -> None:
    async with ChatPanelApp().run_test(size=_SIZE) as pilot:
        cp = pilot.app.query_one(ChatPanel)
        await cp.replay_history(_turns(100))
        await _settle(cp, pilot)
        gate = _PrependGate(cp, monkeypatch)
        old_bottom = cp.scroll_y
        cp.release_anchor()
        # The scrollbar messages can arrive after its mouse capture has ended.
        assert cp.vertical_scrollbar.grabbed is None
        cp.vertical_scrollbar.post_message(ScrollTo(y=old_bottom / 2, animate=True))
        try:
            await gate.wait_held(pilot)
            assert cp.scroll_y == old_bottom
            assert cp._history_scroll.pending is not None
            cp._check_anchor()
            assert cp._anchor_released
        finally:
            gate.release.set()
        await wait_for(
            lambda: cp._history_scroll.pending is None and 0 < cp.scroll_y < cp.max_scroll_y,
            pilot=pilot,
            description="cold drag lands after the mouse is released",
        )
        assert cp._history_window is not None
        assert any(
            content.region.overlaps(cp.content_region) for content in cp.query(UserMessage) if content.id != "turn-100"
        )


async def test_thumb_reuses_an_in_flight_native_window_until_release(monkeypatch: pytest.MonkeyPatch) -> None:
    async with ChatPanelApp().run_test(size=_SIZE) as pilot:
        cp = pilot.app.query_one(ChatPanel)
        await cp.replay_history(_turns(120, with_tools=True))
        await _settle(cp, pilot)
        viewport = cp._history_viewport
        assert viewport is not None
        target = viewport.turn("turn-25")
        assert target is not None
        row = viewport.page_row(target)
        assert row is not None
        height = cp.scrollable_content_region.height
        assert viewport.window(row, height, following=False, overscan=0) == viewport.window(
            row + 1, height, following=False, overscan=0
        )
        gate = _PrependGate(cp, monkeypatch)
        bar = cp.vertical_scrollbar
        original_row = cp.scroll_y
        grab = Offset(bar.region.x, bar.region.bottom - 2)
        bar._on_mouse_capture(events.MouseCapture(grab))
        try:
            bar.post_message(ScrollTo(y=row))
            await gate.wait_held(pilot)
            mount_request = cp._history_scroll.mount_request
            assert mount_request is not None

            bar.post_message(ScrollTo(y=row + 1))
            await wait_for(
                lambda: cp._history_scroll.pending is not None and cp._history_scroll.pending.row == row + 1,
                pilot=pilot,
                description="the latest thumb position is queued",
            )
            assert cp._history_scroll.mount_request is mount_request
            assert cp.scroll_y == original_row
            assert bar.position == row + 1
            gate.release.set()
            await wait_for(
                lambda: cp._history_scroll.pending is None and target.region.overlaps(cp.content_region),
                pilot=pilot,
                description="the native destination lands while the thumb is held",
            )
            assert bar.grabbed is not None
            final_row = cp.scroll_y
        finally:
            gate.release.set()
            bar._on_mouse_release(events.MouseRelease(grab))
        await _settle(cp, pilot)
        assert cp.scroll_y == cp.scroll_target_y == bar.position == final_row


async def test_thumb_releases_the_latest_cold_request_without_waiting_for_the_timer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(history_scroll_module, "_DRAG_MOUNT_INTERVAL", 60)
    async with ChatPanelApp().run_test(size=_SIZE) as pilot:
        cp = pilot.app.query_one(ChatPanel)
        await cp.replay_history(_turns(120, with_tools=True))
        await _settle(cp, pilot)
        viewport = cp._history_viewport
        assert viewport is not None
        first = viewport.turn("turn-25")
        latest = viewport.turn("turn-65")
        assert first is not None and latest is not None
        first_row = viewport.page_row(first)
        latest_row = viewport.page_row(latest)
        assert first_row is not None and latest_row is not None
        bar = cp.vertical_scrollbar
        grab = Offset(bar.region.x, bar.region.bottom - 2)
        bar._on_mouse_capture(events.MouseCapture(grab))

        def target_is_pending(row: int) -> bool:
            pending = cp._history_scroll.pending
            return pending is not None and pending.row == row

        try:
            bar.post_message(ScrollTo(y=first_row))
            await wait_for(lambda: target_is_pending(first_row), pilot=pilot, description="first cold request")
            timer = cp._history_scroll.mount_timer
            assert timer is not None
            # Two events can reach the panel before the compositor restores
            # the held row. The transient reactive row is already latest_row.
            cp._on_scroll_to(ScrollTo(y=latest_row))
            assert cp.scroll_y == latest_row
            cp._on_scroll_to(ScrollTo(y=latest_row))
            await wait_for(lambda: target_is_pending(latest_row), pilot=pilot, description="latest cold request")
            assert cp._history_scroll.mount_timer is timer
            assert cp._history_scroll.mount_request is None
        finally:
            bar._on_mouse_release(events.MouseRelease(grab))

        await wait_for(
            lambda: cp._history_scroll.pending is None and latest.region.overlaps(cp.content_region),
            pilot=pilot,
            description="release publishes the latest native page immediately",
        )
        assert timer.cancelled()
        assert not first.is_attached


async def test_cold_layout_request_keeps_following_a_growing_tail(monkeypatch: pytest.MonkeyPatch) -> None:
    async with ChatPanelApp().run_test(size=_SIZE) as pilot:
        cp = pilot.app.query_one(ChatPanel)
        await cp.replay_history(_turns(100))
        await _settle(cp, pilot)
        viewport = cp._history_viewport
        assert viewport is not None
        gate = _PrependGate(cp, monkeypatch)
        old_bottom = cp.max_scroll_y
        assert cp._defer_history_window(viewport, (0, 1), old_bottom, cp.size, following=True)

        try:
            await gate.wait_held(pilot)
            await cp.mount(AgentMessage("\n".join("new live text" for _ in range(30))))
            await wait_for(
                lambda: cp.max_scroll_y > old_bottom,
                pilot=pilot,
                description="new tail extends beyond the cold request's initial bottom",
            )
        finally:
            gate.release.set()

        await wait_for(
            lambda: cp._history_scroll.pending is None and not cp._anchor_released and cp.scroll_y == cp.max_scroll_y,
            pilot=pilot,
            description="still following the new bottom when cold history becomes ready",
        )


async def test_replay_returns_once_the_newest_entries_are_mounted(monkeypatch: pytest.MonkeyPatch) -> None:
    """A long restore mounts one bounded tail batch before returning, then prepends the rest."""
    messages = _turns()

    async with ChatPanelApp().run_test(size=_SIZE) as pilot:
        cp = pilot.app.query_one(ChatPanel)
        real_mount = cp.mount
        mounted_before_return: list[int] = []

        def mount_spy(*widgets: Widget, **kwargs: object):
            mounted_before_return.append(len(widgets))
            return real_mount(*widgets, **kwargs)

        monkeypatch.setattr(cp, "mount", mount_spy)
        await cp.replay_history(messages)
        tail = chat_content_children(cp)
        tail_mount_calls = list(mounted_before_return)
        monkeypatch.setattr(cp, "mount", real_mount)

        assert tail_mount_calls == [len(tail)]
        assert 2 <= len(tail) <= REPLAY_MOUNT_BATCH_SIZE
        assert _message_texts(cp)[-1] == f"answer {_TURNS - 1}\n\nmore about {_TURNS - 1}"
        assert cp.replay_in_progress
        assert len(cp.pending_transcript_widgets()) == 2 * _TURNS - len(tail)

        await _wait_complete(cp)

        assert not cp.replay_in_progress
        assert cp.pending_transcript_widgets() == []
        assert _message_texts(cp) == [text for _, text in _expected_messages()]
        _assert_document_order(cp)


async def test_replay_bounds_the_foreground_when_the_last_turn_has_many_steps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    messages = _turns(65)
    final_answer = messages.pop()
    for index in range(70):
        call_id = f"final-step-{index}"
        messages.extend(
            [
                {
                    "role": "assistant",
                    "contents": [
                        {"type": "text", "text": f"Checking file {index}."},
                        {
                            "type": "function_call",
                            "name": "read_file",
                            "call_id": call_id,
                            "arguments": {"path": f"/tmp/file_{index}.py"},
                        },
                    ],
                },
                {"role": "tool", "contents": [{"type": "function_result", "call_id": call_id, "result": "ok"}]},
            ]
        )
    messages.append(final_answer)

    async with ChatPanelApp().run_test(size=(80, 24)) as pilot:
        panel = pilot.app.query_one(ChatPanel)
        mount = panel.mount_replay_batch
        foreground_roots = 0

        async def count_foreground(batch: list[Widget], *, before: Widget | None) -> None:
            nonlocal foreground_roots
            if before is None:
                foreground_roots += len(batch)
            await mount(batch, before=before)

        monkeypatch.setattr(panel, "mount_replay_batch", count_foreground)
        await panel.replay_history(messages)

        assert panel.query(AgentMessage).last().text == "answer 64\n\nmore about 64"
        assert 0 < foreground_roots <= 4 * REPLAY_MOUNT_BATCH_SIZE
        target = next(widget for widget in panel.pending_transcript_widgets() if widget.id == "turn-1")
        panel.scroll_to_turn("turn-1")
        await wait_for(
            lambda: target.is_attached and target.region.overlaps(panel.content_region),
            pilot=pilot,
            description="the early turn is visible after a TOC jump",
        )


async def test_replay_trims_overscan_for_folded_tool_history(monkeypatch: pytest.MonkeyPatch) -> None:
    history = _turns(100)
    messages = []
    for index, message in enumerate(history):
        if message["role"] == "assistant" and index < len(history) - 1:
            for step in range(5):
                call_id = f"tool-{index}-{step}"
                messages.extend(
                    (
                        Message(
                            "assistant",
                            [
                                Content.from_text(f"Checking file {step}."),
                                Content.from_function_call(
                                    call_id, "read_file", arguments={"path": f"/tmp/file_{step}.py"}
                                ),
                            ],
                        ).to_dict(),
                        Message("tool", [Content.from_function_result(call_id, result="ok")]).to_dict(),
                    )
                )
        messages.append(message)

    async with ChatPanelApp().run_test(size=(120, 48)) as pilot:
        panel = pilot.app.query_one(ChatPanel)
        mount = panel.mount_replay_batch
        foreground_roots = 0

        async def count_foreground(batch: list[Widget], *, before: Widget | None) -> None:
            nonlocal foreground_roots
            if before is None:
                foreground_roots += len(batch)
            await mount(batch, before=before)

        monkeypatch.setattr(panel, "mount_replay_batch", count_foreground)
        await panel.replay_history(messages)

        assert 0 < foreground_roots <= 4 * REPLAY_MOUNT_BATCH_SIZE
        assert panel._replay_mount.windowed
        assert not panel.replay_in_progress
        assert any(group.process_hidden for group in panel.query(ToolGroup))
        newest = panel.query(AgentMessage).last()
        assert newest.text == "answer 99\n\nmore about 99"
        await _settle(panel, pilot)
        assert panel.scroll_y == panel.max_scroll_y
        assert newest.region.overlaps(panel.content_region)
        oldest = next(widget for widget in panel.pending_transcript_widgets() if widget.id == "turn-1")

        panel.scroll_to_turn("turn-1")
        await wait_for(
            lambda: oldest.is_attached and oldest.region.overlaps(panel.content_region),
            pilot=pilot,
            description="the first cold turn is visible after a TOC jump",
        )
        assert not panel.query("#turn-50")


async def test_bottom_following_view_stays_at_the_bottom_while_history_prepends(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with ChatPanelApp().run_test(size=_SIZE) as pilot:
        cp = pilot.app.query_one(ChatPanel)
        gate = _PrependGate(cp, monkeypatch)
        await cp.replay_history(_turns())
        await gate.wait_held(pilot)
        await _settle(cp, pilot)

        assert not cp._anchor_released
        assert cp.scroll_y == cp.max_scroll_y

        gate.release.set()
        await _wait_complete(cp)
        await _settle(cp, pilot)
        newest = next(child for child in reversed(cp.children) if isinstance(child, AgentMessage))

        assert not cp._anchor_released
        assert cp.scroll_y == cp.max_scroll_y
        assert cp.virtual_size.height > 3 * cp.size.height
        assert newest.text == f"answer {_TURNS - 1}\n\nmore about {_TURNS - 1}"
        assert newest.virtual_region.bottom > cp.scroll_y


async def test_scrolled_up_view_keeps_its_content_while_history_prepends(monkeypatch: pytest.MonkeyPatch) -> None:
    """Entries landing above a scrolled-up view shift the offset, not the content being read."""
    async with ChatPanelApp().run_test(size=_SIZE) as pilot:
        cp = pilot.app.query_one(ChatPanel)
        gate = _PrependGate(cp, monkeypatch)
        await cp.replay_history(_turns())
        await gate.wait_held(pilot)
        await _settle(cp, pilot)

        _simulate_chat_panel_user_scroll_y(cp, cp.max_scroll_y - 2 * cp.size.height)
        await _settle(cp, pilot)
        anchor = next(child for child in chat_content_children(cp) if child.virtual_region.bottom > cp.scroll_y + 1)
        view_offset = anchor.virtual_region.y - cp.scroll_y
        height_before = cp.virtual_size.height

        gate.release.set()
        await _wait_complete(cp)
        await _settle(cp, pilot)

        assert cp.virtual_size.height > height_before
        assert cp._anchor_released
        assert anchor.virtual_region.y - cp.scroll_y == view_offset


async def test_clear_mid_prepend_waits_for_the_batch_then_stops(monkeypatch: pytest.MonkeyPatch) -> None:
    scroll_controller_module, fake_gc = install_fake_chat_panel_gc(monkeypatch)

    async with ChatPanelApp().run_test(size=_SIZE) as pilot:
        cp = pilot.app.query_one(ChatPanel)
        gate = _PrependGate(cp, monkeypatch)
        await cp.replay_history(_turns())
        await gate.wait_held(pilot)

        clear = asyncio.create_task(cp.clear())
        assert not await wait_until(clear.done, timeout=0.2)
        assert chat_content_children(cp)

        gate.release.set()
        await asyncio.wait_for(clear, timeout=DEFAULT_WAIT_TIMEOUT)
        # The emptied panel's next layout clamps its scroll offset, which is no
        # scroll of the user's and holds no pause.
        await _settle(cp, pilot)

        assert [type(child) for child in chat_content_children(cp)] == [WelcomeWidget]
        assert not cp.replay_in_progress
        assert cp.pending_transcript_widgets() == []
        assert not scroll_controller_module.scroll_gc_paused()
        assert fake_gc.enabled is True
        # Only the held batch's young pass ran. The cleared transcript is
        # garbage now, so no full pass walks it; the next reclaim frees it.
        assert fake_gc.collect_generations == [0]
        assert not await wait_until(lambda: gate.prepended_batches > 1, timeout=0.2)


async def test_unmount_mid_prepend_cancels_it_and_releases_the_gc_pause(monkeypatch: pytest.MonkeyPatch) -> None:
    scroll_controller_module, fake_gc = install_fake_chat_panel_gc(monkeypatch)

    async with ChatPanelApp().run_test(size=_SIZE) as pilot:
        cp = pilot.app.query_one(ChatPanel)
        gate = _PrependGate(cp, monkeypatch)
        await cp.replay_history(_turns())
        await gate.wait_held(pilot)
        prepend = cp._replay_mount._task
        assert prepend is not None

        await cp.remove()

        assert not scroll_controller_module.scroll_gc_paused()
        assert fake_gc.enabled is True
        assert fake_gc.collect_generations == []
        await wait_for(prepend.done, pilot=pilot, description="prepend task finished")
        assert prepend.cancelled()
        assert gate.prepended_batches == 1


async def test_replay_gc_pause_spans_the_prepend_and_ends_with_one_collection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No automatic passes walk the growing transcript; young passes free each batch's cycles."""
    scroll_controller_module, fake_gc = install_fake_chat_panel_gc(monkeypatch)

    async with ChatPanelApp().run_test(size=_SIZE) as pilot:
        cp = pilot.app.query_one(ChatPanel)
        gate = _PrependGate(cp, monkeypatch)
        await cp.replay_history(_turns())
        await gate.wait_held(pilot)

        assert scroll_controller_module.scroll_gc_paused()
        assert fake_gc.enabled is False
        assert fake_gc.collect_generations == []

        gate.release.set()
        await _wait_complete(cp)
        # Replay's final scroll-to-end counts as a scroll and holds its own
        # debounced pause; wait that out to observe the replay's release.
        await wait_for(
            lambda: not cp._manual_scroll_gc_paused,
            pilot=pilot,
            description="scroll GC pause resumed",
        )

        assert not scroll_controller_module.scroll_gc_paused()
        assert fake_gc.enabled is True
        generations = fake_gc.collect_generations
        assert generations.count(2) == 1
        # Every prepend batch but the last is followed by a young pass; the
        # full pass right after the last one covers it.
        assert gate.prepended_batches >= 2
        assert generations[: generations.index(2)] == [0] * (gate.prepended_batches - 1)


async def test_short_replay_pauses_gc_while_mounting_and_collects_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replay allocates only long-lived trees: no automatic passes over it, one full pass after."""
    scroll_controller_module, fake_gc = install_fake_chat_panel_gc(monkeypatch)

    async with ChatPanelApp().run_test(size=_SIZE) as pilot:
        cp = pilot.app.query_one(ChatPanel)
        real_mount = cp.mount
        gc_enabled_at_mount: list[bool] = []

        def mount_spy(*widgets: Widget, **kwargs: object):
            gc_enabled_at_mount.append(fake_gc.enabled)
            return real_mount(*widgets, **kwargs)

        monkeypatch.setattr(cp, "mount", mount_spy)
        await cp.replay_history(_turns(2))

        assert gc_enabled_at_mount == [False]
        assert not cp.replay_in_progress
        assert not scroll_controller_module.scroll_gc_paused()
        assert fake_gc.enabled is True
        assert fake_gc.collect_generations == [2]


async def test_copy_reads_the_whole_transcript_while_history_prepends(monkeypatch: pytest.MonkeyPatch) -> None:
    async with ChatPanelApp().run_test(size=_SIZE) as pilot:
        cp = pilot.app.query_one(ChatPanel)
        gate = _PrependGate(cp, monkeypatch)
        await cp.replay_history(_turns())
        await gate.wait_held(pilot)
        expected = _expected_messages()

        assert cp.pending_transcript_widgets()
        assert cp.get_all_messages() == expected
        assert cp.get_user_messages() == [entry for entry in expected if entry[0] == "You"]
        assert cp.get_agent_responses() == [entry for entry in expected if entry[0] == "Agent"]

        gate.release.set()
        await _wait_complete(cp)
        assert cp.get_all_messages() == expected


async def test_live_entry_during_prepend_stays_below_the_replayed_history(monkeypatch: pytest.MonkeyPatch) -> None:
    async with ChatPanelApp().run_test(size=_SIZE) as pilot:
        cp = pilot.app.query_one(ChatPanel)
        gate = _PrependGate(cp, monkeypatch)
        await cp.replay_history(_turns())
        await gate.wait_held(pilot)

        await cp.add_user_message("live question")
        gate.release.set()
        await _wait_complete(cp)

        assert _message_texts(cp) == [*(text for _, text in _expected_messages()), "live question"]
        _assert_document_order(cp)


async def test_entries_mounted_before_the_replay_stay_above_all_replayed_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A restore warning mounted before the replay stays on top; copy mid-prepend reads it first too."""
    async with ChatPanelApp().run_test(size=_SIZE) as pilot:
        cp = pilot.app.query_one(ChatPanel)
        gate = _PrependGate(cp, monkeypatch)
        await cp.add_user_message("earlier question")
        # SessionHandlers.on_session_restored: clear, the cwd warning, then the replay.
        await cp.add_error("Working directory no longer exists: /gone", action_label=None)
        await cp.replay_history(_turns())
        await gate.wait_held(pilot)
        expected = [("You", "earlier question"), *_expected_messages()]

        assert cp.pending_transcript_widgets()
        assert cp.get_all_messages() == expected
        assert cp.get_user_messages() == [entry for entry in expected if entry[0] == "You"]

        gate.release.set()
        await _wait_complete(cp)

        entries = chat_content_children(cp)
        assert isinstance(entries[0], UserMessage)
        assert isinstance(entries[1], ErrorMessage)
        assert _message_texts(cp) == [text for _, text in expected]
        assert cp.get_all_messages() == expected
        _assert_document_order(cp)


async def test_toc_jump_to_a_turn_still_pending_lands_once_it_mounts(monkeypatch: pytest.MonkeyPatch) -> None:
    async with ChatPanelApp().run_test(size=_SIZE) as pilot:
        cp = pilot.app.query_one(ChatPanel)
        gate = _PrependGate(cp, monkeypatch)
        await cp.replay_history(_turns())
        await gate.wait_held(pilot)
        oldest_turn = cp.toc_items[0].turn_id
        assert not any(isinstance(child, UserMessage) and child.id == oldest_turn for child in cp.children)

        cp.scroll_to_turn(oldest_turn)
        gate.release.set()
        await _wait_complete(cp)
        target = next(child for child in cp.children if isinstance(child, UserMessage) and child.id == oldest_turn)
        await wait_for(lambda: target.has_class("-highlighted"), pilot=pilot, description="pending turn highlighted")
        await _settle(cp, pilot)

        assert cp.scroll_y <= target.virtual_region.y <= cp.scroll_y + 1


def _jump_into_a_prepended_batch(
    panel: ChatPanel, monkeypatch: pytest.MonkeyPatch, *, batch: str, when: str
) -> tuple[list[tuple[str, bool]], ExitStack]:
    """Jump to a turn of the ``first`` or ``last`` prepended batch at ``when``, as a click in that window would.

    ``registered``: Textual has made the batch children, not mounted them.
    ``mounted``: the batch has mounted, before the prepend takes it back.
    ``handed_back``: the prepend is done with the batch, which lays out on the next refresh.

    Records each jump's turn id and whether the turn had geometry. The returned
    stack holds the screen's layout from the chosen batch's mount until the
    jump, as a frame that has not come yet would; the test owns closing it.
    """
    jumped: list[tuple[str, bool]] = []
    layout_hold = ExitStack()
    held: list[list[Widget]] = []
    real_mount = panel.mount
    real_mount_batch = panel.mount_replay_batch
    controller = panel._replay_mount
    after_batch_mounted = controller._after_batch_mounted

    def chosen() -> bool:
        return not jumped and not held and (batch == "first" or not panel.pending_transcript_widgets())

    def jump(entries: list[Widget]) -> None:
        turn = next(entry for entry in entries if isinstance(entry, UserMessage))
        if turn.id is not None:
            jumped.append((turn.id, bool(turn.region)))
            panel.scroll_to_turn(turn.id)

    def mount(*widgets: Widget, before: Widget | None = None, after: Widget | None = None) -> AwaitMount:
        mounting = real_mount(*widgets, **_placement(before, after))
        if when == "registered" and before is not None and chosen():
            jump(list(widgets))
        return mounting

    async def mount_replay_batch(entries: list[Widget], *, before: Widget | None) -> None:
        if when == "registered" or before is None or not chosen():
            await real_mount_batch(entries, before=before)
            return
        compositor = panel.screen._compositor
        # Whether an earlier scroll left the screen's map invalid here depends
        # on timing (a loaded runner): forced, so every run takes that path.
        compositor._full_map_invalidated = True
        layout_hold.enter_context(panel.app.batch_update())
        # A geometry read rebuilds an invalid map, and would place the held
        # batch without a frame: built here, before the batch mounts, the map
        # stays valid under the hold, so the batch has no geometry until the
        # hold ends.
        full_map = compositor.full_map
        assert panel in full_map
        held.append(entries)
        await real_mount_batch(entries, before=before)
        if when == "mounted":
            jump(entries)
            layout_hold.close()

    def hand_back(entries: list[Widget]) -> None:
        after_batch_mounted(entries)
        if when == "handed_back" and held and entries is held[0]:
            jump(entries)
            layout_hold.close()

    monkeypatch.setattr(panel, "mount", mount)
    monkeypatch.setattr(panel, "mount_replay_batch", mount_replay_batch)
    monkeypatch.setattr(controller, "_after_batch_mounted", hand_back)
    return jumped, layout_hold


@pytest.mark.parametrize("when", ["registered", "mounted", "handed_back"])
@pytest.mark.parametrize("batch", ["first", "last"])
async def test_a_toc_jump_into_a_batch_not_laid_out_yet_lands_once_it_is(
    monkeypatch: pytest.MonkeyPatch, batch: str, when: str
) -> None:
    """A prepended batch is among the panel's children from registration, without geometry until it lays out.

    The last batch lays out after the prepend has ended, and the jump still lands.
    """
    async with ChatPanelApp().run_test(size=_SIZE) as pilot:
        cp = pilot.app.query_one(ChatPanel)
        jumped, layout_hold = _jump_into_a_prepended_batch(cp, monkeypatch, batch=batch, when=when)
        with layout_hold:
            await cp.replay_history(_turns())
            await _wait_complete(cp)
        [(turn_id, had_geometry)] = jumped
        assert not had_geometry
        if batch == "last":
            assert turn_id == cp.toc_items[0].turn_id
        target = next(child for child in cp.children if isinstance(child, UserMessage) and child.id == turn_id)
        await wait_for(lambda: target.has_class("-highlighted"), pilot=pilot, description="the turn highlighted")
        await _settle(cp, pilot)

        assert cp.scroll_y <= target.virtual_region.y <= cp.scroll_y + 1
        assert cp.scroll_y < cp.max_scroll_y


@pytest.mark.parametrize("when", ["registered", "ended"])
@pytest.mark.parametrize(
    ("move", "start"),
    [("held_jump", "bottom"), ("jump", "bottom"), ("scroll", "bottom"), ("jump", "top"), ("scroll", "top")],
)
async def test_a_view_moved_before_the_last_batch_lays_out_stays_on_its_turn(
    monkeypatch: pytest.MonkeyPatch, move: str, start: str, when: str
) -> None:
    """The view moves to a turn while the last prepended batch is a child without geometry.

    ``held_jump``: a jump held for the first prepended batch lands, a slow
    frame having run that batch's refresh callback this late. ``jump``: a TOC
    click on a turn of that batch, which has its geometry. ``scroll``: the
    user scrolls that turn to the top. The view moves from the ``bottom`` it
    follows, or from the ``top``, where it shows a warning mounted before the
    replay, which no batch moves. ``registered``: right after the last batch
    registers; ``ended``: once the prepend has handed it back and ended. The
    last batch then lays out above the turn, and the view moves with it.
    """
    async with ChatPanelApp().run_test(size=_SIZE) as pilot:
        cp = pilot.app.query_one(ChatPanel)
        if start == "top":
            await cp.add_error("Working directory no longer exists: /gone", action_label=None)
        warning = next((child for child in cp.children if isinstance(child, ErrorMessage)), None)
        queued = _hold_refresh_callbacks(cp, monkeypatch)
        layout_hold = ExitStack()
        turns: list[UserMessage] = []
        last_batch: list[Widget] = []
        real_mount = cp.mount
        real_mount_batch = cp.mount_replay_batch

        def run_refresh_callbacks() -> None:
            callbacks = list(queued)
            queued.clear()
            for callback, args in callbacks:
                callback(*args)

        def move_view() -> None:
            if start == "bottom":
                # Following the bottom until now, so the view tracks no child yet.
                assert not cp.is_anchor_released()
            else:
                assert cp.is_anchor_released()
                assert cp.scroll_y == 0
            if move == "held_jump":
                # The first batch's callback; the last batch's, once queued, waits for its layout.
                callback, args = queued.pop(0)
                callback(*args)
            elif move == "jump":
                assert turns[0].id is not None
                cp.scroll_to_turn(turns[0].id)
            else:
                _simulate_chat_panel_user_scroll_y(cp, turns[0].virtual_region.y)

        def mount(*widgets: Widget, before: Widget | None = None, after: Widget | None = None) -> AwaitMount:
            mounting = real_mount(*widgets, **_placement(before, after))
            if before is not None and not turns:
                turns.append(next(widget for widget in widgets if isinstance(widget, UserMessage)))
                if move == "held_jump" and turns[0].id is not None:
                    cp.scroll_to_turn(turns[0].id)
            elif before is not None and when == "registered":
                move_view()
            return mounting

        async def mount_replay_batch(entries: list[Widget], *, before: Widget | None) -> None:
            if before is None or cp.pending_transcript_widgets():
                await real_mount_batch(entries, before=before)
                return
            last_batch.extend(entries)
            await wait_for(lambda: bool(turns[0].region), description="the first prepended batch laid out")
            if move != "held_jump":
                run_refresh_callbacks()
            if start == "top":
                _simulate_chat_panel_user_scroll_y(cp, 0)
                await wait_for(
                    lambda: (tracked := cp._scroll_controller._view_hold_child) is not None and tracked[0] is warning,
                    description="the view tracking the warning",
                )
            layout_hold.enter_context(cp.app.batch_update())
            await real_mount_batch(entries, before=before)
            if when == "registered":
                layout_hold.close()

        monkeypatch.setattr(cp, "mount", mount)
        monkeypatch.setattr(cp, "mount_replay_batch", mount_replay_batch)
        with layout_hold:
            await cp.replay_history(_turns())
            await _wait_complete(cp)
            if when == "ended":
                move_view()
        await wait_for(
            lambda: all(entry.region for entry in last_batch if isinstance(entry, UserMessage)),
            pilot=pilot,
            description="the last prepended batch laid out",
        )
        assert cp._scroll_controller.view_hold_active
        run_refresh_callbacks()
        await _settle(cp, pilot)

        assert cp.scroll_y <= turns[0].virtual_region.y <= cp.scroll_y + 1
        assert cp.scroll_y < cp.max_scroll_y
        # Nothing lands above the view any more.
        assert not cp._scroll_controller.view_hold_active


@pytest.mark.parametrize("gesture", ["wheel", "page_up_key"])
async def test_user_scroll_drops_a_toc_jump_still_waiting_for_its_turn(
    monkeypatch: pytest.MonkeyPatch, gesture: str
) -> None:
    """A pending jump must not yank a reader who scrolled elsewhere before its turn mounted."""
    async with ChatPanelApp().run_test(size=_SIZE) as pilot:
        cp = pilot.app.query_one(ChatPanel)
        gate = _PrependGate(cp, monkeypatch)
        await cp.replay_history(_turns())
        await gate.wait_held(pilot)
        await _settle(cp, pilot)
        oldest_turn = cp.toc_items[0].turn_id

        cp.scroll_to_turn(oldest_turn)
        before_gesture = cp.scroll_y
        if gesture == "wheel":
            cp.post_message(events.MouseScrollUp(None, 0, 0, 0, 0, 0, False, False, False))
        else:
            cp.focus()
            await pilot.press("pageup")
        await wait_for(lambda: cp.scroll_y < before_gesture, pilot=pilot, description="the user scrolled up")
        await _settle(cp, pilot)
        reading = next(child for child in chat_content_children(cp) if child.virtual_region.bottom > cp.scroll_y + 1)
        offset = reading.virtual_region.y - cp.scroll_y

        gate.release.set()
        await _wait_complete(cp)
        await _settle(cp, pilot)

        target = next(child for child in cp.children if isinstance(child, UserMessage) and child.id == oldest_turn)
        assert not target.has_class("-highlighted")
        assert reading.virtual_region.y - cp.scroll_y == offset


async def test_a_message_sent_drops_a_toc_jump_still_waiting_for_its_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    """The new message is what the user looks at now; the turn they jumped to before must not take the view."""
    async with ChatPanelApp().run_test(size=_SIZE) as pilot:
        cp = pilot.app.query_one(ChatPanel)
        gate = _PrependGate(cp, monkeypatch)
        await cp.replay_history(_turns())
        await gate.wait_held(pilot)
        await _settle(cp, pilot)
        oldest_turn = cp.toc_items[0].turn_id

        cp.scroll_to_turn(oldest_turn)
        cp.agent_running = True
        await cp.add_user_message("new question")
        message = cp.query(UserMessage).last()
        await wait_for(lambda: bool(message.outer_size), pilot=pilot, description="the message laid out")

        gate.release.set()
        await _wait_complete(cp)
        await _settle(cp, pilot)

        target = next(child for child in cp.children if isinstance(child, UserMessage) and child.id == oldest_turn)
        assert not target.has_class("-highlighted")
        assert cp.scroll_y <= message.virtual_region.y < cp.scroll_y + cp.size.height


@pytest.mark.parametrize("refreshes", [0, 1])
async def test_a_toc_jump_right_after_a_restore_is_not_pulled_back_to_the_bottom(
    monkeypatch: pytest.MonkeyPatch, refreshes: int
) -> None:
    """The replay scrolls to the bottom once it has laid out; a jump before then, or in the next frame, stays put.

    Every callback the panel queues for a refresh from the end of the replay
    waits for the test, as slow frames would; the jump comes after
    ``refreshes`` rounds of them.
    """
    async with ChatPanelApp().run_test(size=_SIZE) as pilot:
        cp = pilot.app.query_one(ChatPanel)
        queued: list[Callable[[], object]] = []
        holding = False
        real_call_after_refresh = cp.call_after_refresh
        real_after_replay_history_mounted = cp.after_replay_history_mounted

        def call_after_refresh(callback: Callable[..., object], *args: object, **kwargs: object) -> bool:
            if not holding:
                return real_call_after_refresh(callback, *args, **kwargs)
            queued.append(partial(callback, *args, **kwargs))
            return True

        def after_replay_history_mounted() -> None:
            nonlocal holding
            holding = True
            real_after_replay_history_mounted()

        async def run_refresh_callbacks() -> None:
            callbacks = list(queued)
            queued.clear()
            for callback in callbacks:
                result = callback()
                if inspect.isawaitable(result):
                    await result

        monkeypatch.setattr(cp, "call_after_refresh", call_after_refresh)
        monkeypatch.setattr(cp, "after_replay_history_mounted", after_replay_history_mounted)
        # Short enough to mount in the tail batch alone.
        await cp.replay_history(_turns(8))
        assert holding
        assert not cp.replay_in_progress
        await _settle(cp, pilot)
        for _ in range(refreshes):
            await run_refresh_callbacks()
        target = [child for child in cp.children if isinstance(child, UserMessage)][1]
        assert target.id is not None
        cp.scroll_to_turn(target.id)
        for _ in range(3):
            await run_refresh_callbacks()
        assert not queued
        holding = False
        await _settle(cp, pilot)

        assert cp.scroll_y <= target.virtual_region.y <= cp.scroll_y + 1
        assert cp.scroll_y < cp.max_scroll_y


@pytest.mark.parametrize("superseded_by", [None, "user_scroll", "newer_jump"])
async def test_a_toc_jump_queued_for_the_next_refresh_yields_to_later_navigation(
    monkeypatch: pytest.MonkeyPatch, superseded_by: str | None
) -> None:
    """Once its turn mounts, a jump waits for a refresh; a scroll or jump before then wins."""
    async with ChatPanelApp().run_test(size=_SIZE) as pilot:
        cp = pilot.app.query_one(ChatPanel)
        gate = _PrependGate(cp, monkeypatch)
        queued = _hold_refresh_callbacks(cp, monkeypatch)
        await cp.replay_history(_turns())
        await gate.wait_held(pilot)
        oldest_turn = cp.toc_items[0].turn_id
        mounted_turn = next(child for child in cp.children if isinstance(child, UserMessage)).id
        assert mounted_turn is not None and mounted_turn != oldest_turn

        height_before_prepend = cp.virtual_size.height
        with pilot.app.batch_update():
            cp.scroll_to_turn(oldest_turn)
            gate.release.set()
            await _wait_complete(cp)
            assert queued, "the jump waits for the refresh after its turn mounted"
            if superseded_by == "user_scroll":
                cp.note_user_scroll()
            elif superseded_by == "newer_jump":
                cp.scroll_to_turn(mounted_turn)
        # Deliver the intercepted layout callbacks once their batch has
        # geometry. A refresh callback can run before a queued Layout.
        await wait_for(
            lambda: cp.virtual_size.height > height_before_prepend,
            pilot=pilot,
            description="the prepended batch has been laid out",
        )
        for callback, args in queued:
            callback(*args)

        oldest = next(child for child in cp.children if isinstance(child, UserMessage) and child.id == oldest_turn)
        assert oldest.has_class("-highlighted") is (superseded_by is None)
        if superseded_by == "newer_jump":
            newer = next(child for child in cp.children if isinstance(child, UserMessage) and child.id == mounted_turn)
            await wait_for(
                lambda: newer.has_class("-highlighted") and cp.scroll_y <= newer.virtual_region.y <= cp.scroll_y + 1,
                pilot=pilot,
                description="the newer TOC jump reached its turn",
            )


async def test_a_toc_jump_queued_before_a_clear_stays_out_of_the_next_transcript(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The next transcript reuses the turn ids, so a jump queued for the old one must not land in it."""
    async with ChatPanelApp().run_test(size=_SIZE) as pilot:
        cp = pilot.app.query_one(ChatPanel)
        gate = _PrependGate(cp, monkeypatch)
        queued = _hold_refresh_callbacks(cp, monkeypatch)
        await cp.replay_history(_turns())
        await gate.wait_held(pilot)
        oldest_turn = cp.toc_items[0].turn_id
        cp.scroll_to_turn(oldest_turn)
        gate.release.set()
        # The prepend has ended; only the jump it queued is left of it.
        await _wait_complete(cp)
        assert queued, "the jump waits for the refresh after its turn mounted"

        await cp.clear()
        await cp.replay_history(_turns())
        await _wait_complete(cp)
        await _settle(cp, pilot)
        assert cp.toc_items[0].turn_id == oldest_turn
        assert cp.scroll_y == cp.max_scroll_y > 0
        for callback, args in queued:
            callback(*args)
        await _settle(cp, pilot)

        assert not any(child.has_class("-highlighted") for child in cp.children if isinstance(child, UserMessage))
        assert cp.scroll_y == cp.max_scroll_y


async def test_a_toc_jump_queued_before_an_unmount_never_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    async with ChatPanelApp().run_test(size=_SIZE) as pilot:
        cp = pilot.app.query_one(ChatPanel)
        gate = _PrependGate(cp, monkeypatch)
        queued = _hold_refresh_callbacks(cp, monkeypatch)
        await cp.replay_history(_turns())
        await gate.wait_held(pilot)
        cp.scroll_to_turn(cp.toc_items[0].turn_id)
        gate.release.set()
        await _wait_complete(cp)
        assert queued, "the jump waits for the refresh after its turn mounted"

        await cp.remove()
        jumps: list[str] = []
        monkeypatch.setattr(cp, "scroll_to_turn", jumps.append)
        for callback, args in queued:
            callback(*args)

        assert jumps == []


async def test_fold_all_during_prepend_reaches_tool_groups_mounted_later(monkeypatch: pytest.MonkeyPatch) -> None:
    # Folded turns hide entries, so the tail and the first batch mount all of the unfolded history but its first
    # prompt: the turns past it leave a tool group each pending, six here. More would only add work the wait must
    # outlast.
    count = _TOOL_TURNS + 6
    async with ChatPanelApp().run_test(size=_SIZE) as pilot:
        cp = pilot.app.query_one(ChatPanel)
        gate = _PrependGate(cp, monkeypatch)
        await cp.replay_history(_turns(count, with_tools=True))
        await gate.wait_held(pilot)
        mounted_groups = list(cp.query(ToolGroup))
        assert mounted_groups
        assert all(group.collapsed for group in mounted_groups)
        assert any(isinstance(widget, ToolGroup) for widget in cp.pending_transcript_widgets())

        # Every group is collapsed (pending ones included), so fold-all expands.
        assert cp.toggle_fold_all() is False

        gate.release.set()
        await _wait_complete(cp)
        groups = list(cp.query(ToolGroup))

        assert len(groups) == count
        assert [group.collapsed for group in groups] == [False] * count
        # Everything is expanded now, so the next fold-all collapses.
        assert cp.toggle_fold_all() is True


async def test_fold_all_during_prepend_reaches_thinking_mounted_later(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only the oldest turns thought, so fold-all finds no fold mounted yet: the pending thinking takes the choice."""
    thought_turns = 4
    messages = _turns()
    for message in messages[: 3 * thought_turns]:
        if message["role"] == "assistant":
            message["contents"] = [Content.from_text_reasoning(text="Thinking first.").to_dict(), *message["contents"]]

    async with ChatPanelApp(thinking_expanded=lambda: True).run_test(size=_SIZE) as pilot:
        cp = pilot.app.query_one(ChatPanel)
        gate = _PrependGate(cp, monkeypatch)
        await cp.replay_history(messages)
        await gate.wait_held(pilot)
        assert not cp.query(ThinkingMessage)
        assert any(isinstance(widget, ThinkingMessage) for widget in cp.pending_transcript_widgets())

        assert cp.toggle_fold_all() is False

        gate.release.set()
        await _wait_complete(cp)
        thinking = list(cp.query(ThinkingMessage))

        assert len(thinking) == thought_turns
        assert [message.collapsed for message in thinking] == [False] * len(thinking)
        assert all(message.query(VirtualizedMarkdown) for message in thinking)
        assert cp.toggle_fold_all() is True


@pytest.mark.parametrize("pending_only", [False, True], ids=["mounted-and-pending", "only-pending"])
async def test_fold_all_during_prepend_opens_summaries_mounted_later(
    pending_only: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    folds = [FoldItem(f"ctx-{index}", f"Summary {index}") for index in range(1 if pending_only else 72)]
    async with ChatPanelApp().run_test(size=_SIZE) as pilot:
        panel = pilot.app.query_one(ChatPanel)
        gate = _PrependGate(panel, monkeypatch)
        await panel.replay_history(_turns() if pending_only else [], folds=folds)
        await gate.wait_held(pilot)
        mounted = list(panel.query(ContextFoldWidget))
        assert bool(mounted) is not pending_only
        assert all(not fold.query_one("#summary").display for fold in mounted)
        assert any(isinstance(widget, ContextFoldWidget) for widget in panel.pending_transcript_widgets())

        try:
            assert panel.toggle_fold_all() is False
        finally:
            gate.release.set()
        await _wait_complete(panel)
        markers = list(panel.query(ContextFoldWidget))
        assert len(markers) == len(folds)
        for marker in markers:
            title, body = marker.query(Static)
            assert body.display
            assert str(title.render()).strip() == "═══ ▾ Compressed ═══"


async def test_fold_all_opens_a_cold_compaction_summary_on_demand() -> None:
    summary = "Archived context summary"
    block = CompressedBlock(
        "ctx", messages=[Message.from_dict(item) for item in _turns(24)], summary_text=summary, turn_range=(1, 24)
    )
    async with ChatPanelApp().run_test(size=_SIZE) as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.replay_history(_turns(96), folds=[FoldItem("ctx", summary, block=block)])
        await _settle(panel, pilot)
        pending = panel.pending_transcript_widgets()
        marker = next(widget for widget in pending if isinstance(widget, ContextFoldWidget))
        oldest = next(widget for widget in pending if widget.id == "turn-1")
        assert marker.collapsed and not marker.is_attached

        assert panel.toggle_fold_all() is False
        assert not marker.is_attached
        panel.scroll_to_turn("turn-24")
        await wait_for(
            lambda: not marker.collapsed and marker.region.overlaps(panel.content_region),
            pilot=pilot,
            description="the cold compaction summary opens when its page mounts",
        )
        assert not oldest.is_attached
        assert marker.query_one("#summary", Static).content == summary
        assert panel.toggle_fold_all() is True
        assert marker.collapsed


async def test_fold_all_while_the_last_batch_mounts_outlasts_the_choice_before_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The last batch has left ``pending`` while it mounts; a fold-all then still decides how it ends up."""
    async with ChatPanelApp().run_test(size=_SIZE) as pilot:
        cp = pilot.app.query_one(ChatPanel)
        gate = _PrependGate(cp, monkeypatch)
        real_mount_batch = cp.mount_replay_batch
        folds: list[bool] = []

        async def mount_replay_batch(entries: list[Widget], *, before: Widget | None) -> None:
            await real_mount_batch(entries, before=before)
            if before is not None and not cp.pending_transcript_widgets():
                folds.append(cp.toggle_fold_all())

        monkeypatch.setattr(cp, "mount_replay_batch", mount_replay_batch)
        await cp.replay_history(_turns(_TOOL_TURNS, with_tools=True))
        await gate.wait_held(pilot)
        assert cp.toggle_fold_all() is False

        gate.release.set()
        await _wait_complete(cp)

        assert folds == [True]
        assert [group.collapsed for group in cp.query(ToolGroup)] == [True] * _TOOL_TURNS


async def test_a_folder_change_during_prepend_reaches_tool_groups_mounted_later(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Each turn's group read one file in the chat's folder, so its row names that file relative to the folder.
    folder = Path(Path(__file__).anchor, "work")
    messages = _turns(_TOOL_TURNS, with_tools=True)
    calls = [content for message in messages for content in message["contents"] if content["type"] == "function_call"]
    for index, call in enumerate(calls):
        call["arguments"] = {"path": str(folder / f"file_{index}.py")}
        call["additional_properties"] = {TOOL_CALL_KIND_METADATA_KEY: KIND_FILESYSTEM_READ}
    async with ChatPanelApp().run_test(size=_SIZE) as pilot:
        cp = pilot.app.query_one(ChatPanel)
        cp.set_workspace_cwd(str(folder))
        gate = _PrependGate(cp, monkeypatch)
        await cp.replay_history(messages)
        await gate.wait_held(pilot)
        assert any(isinstance(widget, ToolGroup) for widget in cp.pending_transcript_widgets())

        # The chat moves to another folder while older history waits to mount: the files lie outside it now. Mounted
        # rows name them anew, and the rows still to mount read the folder as they mount, not as replay planned them.
        cp.set_workspace_cwd(str(folder.parent / "elsewhere"))
        gate.release.set()
        await _wait_complete(cp)

        titles = [title_text(group.query_one(ToolGroupTitle))[2:] for group in cp.query(ToolGroup)]
        assert titles == [f"Read {folder / f'file_{index}.py'}" for index in range(_TOOL_TURNS)]


async def test_a_withdrawal_mid_prepend_waits_for_the_replay_then_removes_each_answer_it_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The answers a bare Continue drops leave the view whichever replay batch mounted them."""
    messages = _turns()
    for index, segment in ((1, "oldest"), (-1, "newest")):
        contents = messages[index]["contents"]
        assert isinstance(contents, list)
        contents[0]["additional_properties"] = {ANSWER_SEGMENT_KEY: segment}
    async with ChatPanelApp().run_test(size=_SIZE) as pilot:
        cp = pilot.app.query_one(ChatPanel)
        gate = _PrependGate(cp, monkeypatch)
        await cp.replay_history(messages)
        await gate.wait_held(pilot)

        withdrawal = asyncio.create_task(cp.withdraw_answer(("oldest", "newest")))
        await asyncio.sleep(0)
        assert not withdrawal.done()
        gate.release.set()
        await asyncio.wait_for(withdrawal, timeout=DEFAULT_WAIT_TIMEOUT)

        withdrawn = {"answer 0\n\nmore about 0", f"answer {_TURNS - 1}\n\nmore about {_TURNS - 1}"}
        assert _message_texts(cp) == [text for _, text in _expected_messages() if text not in withdrawn]


async def test_withdrawing_the_restored_tail_answer_keeps_unrelated_history_cold() -> None:
    messages = _turns(120)
    last_answer = messages[-1]["contents"]
    assert isinstance(last_answer, list)
    last_answer[0]["additional_properties"] = {ANSWER_SEGMENT_KEY: "tail-answer"}

    async with ChatPanelApp().run_test(size=_SIZE) as pilot:
        cp = pilot.app.query_one(ChatPanel)
        await cp.replay_history(messages)
        await _settle(cp, pilot)
        pending = cp.pending_transcript_widgets()
        assert pending

        await cp.withdraw_answer(("tail-answer",))
        await _settle(cp, pilot)

        assert cp.get_all_messages() == _expected_messages(120)[:-1]
        assert cp.pending_transcript_widgets() == pending


async def test_removing_a_restored_tail_note_keeps_older_history_cold() -> None:
    messages = _turns(120)
    messages.append(
        {
            "role": "user",
            "contents": [{"type": "text", "text": "Unanswered note"}],
            "additional_properties": {
                HistoryMarkerKind.INJECTED_KEY: True,
                ANALYTICS_ITEM_ID_KEY: "tail-note",
            },
        }
    )
    async with ChatPanelApp().run_test(size=_SIZE) as pilot:
        cp = pilot.app.query_one(ChatPanel)
        await cp.replay_history(messages)
        await _settle(cp, pilot)
        pending = cp.pending_transcript_widgets()
        assert pending
        assert cp.get_user_messages()[-1] == ("You", "Unanswered note")

        await cp.add_user_message("New question")
        await cp.apply_fresh_turn_cleanup(retired_previous_turn=False, removed_item_ids=("tail-note",))
        await _settle(cp, pilot)

        assert cp.get_all_messages() == [*_expected_messages(120), ("You", "New question")]
        assert cp.pending_transcript_widgets() == pending


async def test_retiring_the_restored_tail_keeps_older_history_cold() -> None:
    messages = _turns(120)[:-1]
    messages.append(
        {
            "role": "assistant",
            "contents": [{"type": "text", "text": "Stopped"}],
            "additional_properties": {HistoryMarkerKind.KEY: HistoryMarkerKind.INTERRUPTED},
        }
    )
    async with ChatPanelApp().run_test(size=_SIZE) as pilot:
        cp = pilot.app.query_one(ChatPanel)
        await cp.replay_history(messages)
        await _settle(cp, pilot)
        pending = cp.pending_transcript_widgets()
        viewport = cp._history_viewport
        assert pending and viewport is not None

        await cp.add_user_message("Replacement")
        await cp.apply_fresh_turn_cleanup(retired_previous_turn=True, removed_item_ids=())
        await _settle(cp, pilot)

        assert cp.get_all_messages() == [*_expected_messages(119), ("You", "Replacement")]
        assert cp.pending_transcript_widgets() == pending
        assert cp._history_viewport is viewport
        reply_to = cp.query_one("#turn-121", UserMessage)
        assert reply_to.region.overlaps(cp.content_region)


async def test_withdrawing_an_archived_answer_releases_the_obsolete_viewport() -> None:
    messages = _turns(120)
    last_archived_answer = messages[-4]["contents"]
    assert isinstance(last_archived_answer, list)
    last_archived_answer[0]["additional_properties"] = {ANSWER_SEGMENT_KEY: "archived-answer"}

    async with ChatPanelApp().run_test(size=_SIZE) as pilot:
        cp = pilot.app.query_one(ChatPanel)
        await cp.replay_history(messages)
        await _wait_complete(cp)
        await _settle(cp, pilot)
        cp.refresh(layout=True)
        await _settle(cp, pilot)
        assert cp._history_viewport is not None

        await cp.withdraw_answer(("archived-answer",))
        await _settle(cp, pilot)

        expected = _expected_messages(120)
        assert cp.get_all_messages() == [*expected[:-3], *expected[-2:]]
        assert cp._history_viewport is None
        assert cp._history_scroll.visible_layout is None


async def test_retirement_mid_prepend_waits_and_removes_the_whole_failed_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from chrys.foundation.models.history_markers import HistoryMarkerKind
    from chrys.kernel import Message

    messages = [Message("user", ["Retired opener"]).to_dict()]
    for index in range(90):
        messages.append(
            Message(
                "user",
                [f"Unanswered note {index}"],
                additional_properties={HistoryMarkerKind.INJECTED_KEY: True},
            ).to_dict()
        )
    messages.append(
        Message(
            "assistant", ["Stopped"], additional_properties={HistoryMarkerKind.KEY: HistoryMarkerKind.INTERRUPTED}
        ).to_dict()
    )
    async with ChatPanelApp().run_test(size=_SIZE) as pilot:
        cp = pilot.app.query_one(ChatPanel)
        gate = _PrependGate(cp, monkeypatch)
        cleanup = None
        try:
            await cp.replay_history(messages)
            await gate.wait_held(pilot)
            assert not any(message.text == "Retired opener" for message in cp.query(UserMessage))
            await cp.add_user_message("Replacement")
            cleanup = asyncio.create_task(cp.apply_fresh_turn_cleanup(retired_previous_turn=True, removed_item_ids=()))
            await asyncio.sleep(0)
            assert not cleanup.done()
            gate.release.set()
            await asyncio.wait_for(cleanup, timeout=DEFAULT_WAIT_TIMEOUT)
            cp.apply_turn_started(1)
            assert [message.text for message in cp.query(UserMessage)] == ["Replacement"]
            assert [(item.summary, item.turn_index) for item in cp.toc_items] == [("Replacement", 1)]
        finally:
            gate.release.set()
            if cleanup is not None:
                await cleanup
            await cp.wait_replay_complete()
