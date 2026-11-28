# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Scrolling and resize while a restored transcript has unmounted pages."""

from __future__ import annotations

import asyncio
import math

import pytest
from textual import events
from textual._animator import SimpleAnimation
from textual.geometry import Offset, Size
from textual.layout import DockArrangeResult
from textual.scrollbar import ScrollDown, ScrollTo, ScrollUp
from textual.widget import Widget

from chrys.app.tui.util.visibility import set_widget_visibility_without_layout
from chrys.app.tui.widgets.chat import history_viewport
from chrys.app.tui.widgets.chat.guide_fold import GuideFoldRequested
from chrys.app.tui.widgets.chat.history_scroll import HistoryScrollRequest
from chrys.app.tui.widgets.chat.messages import AgentMessage, ThinkingMessage, UserMessage
from chrys.app.tui.widgets.chat.panel import ChatPanel
from chrys.app.tui.widgets.chat.process_block import ProcessBlock
from chrys.app.tui.widgets.chat.tool_call import ToolGroup
from chrys.app.tui.widgets.markdown import VirtualizedMarkdown
from chrys.kernel import Content
from tests.app.tui.widgets.chat.replay_fixtures import _SIZE, _PrependGate, _settle, _turns, _wait_complete
from tests.support.pilot_barrier import screen_is_settled
from tests.support.tui_helpers import ChatPanelApp, delay_resize_dispatch, resize_when_settled
from tests.support.waiting import wait_for

pytestmark = pytest.mark.usefixtures("frozen_earlier_heap")


def _turns_with_overestimated_links(*, with_tools: bool = False) -> list[dict[str, object]]:
    messages = _turns(120, with_tools=with_tools)
    for message in messages:
        if message["role"] == "assistant" and message["contents"][0]["type"] == "text":
            message["contents"] = [{"type": "text", "text": f"[doc](https://example.com/{'x' * 1600})"}]
    return messages


@pytest.mark.parametrize(("direction", "expanded"), [(-1, False), (-1, True), (1, False)])
async def test_consecutive_pages_keep_adjacent_rows_in_cold_tool_history(direction: int, expanded: bool) -> None:
    async with ChatPanelApp().run_test(size=_SIZE) as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.replay_history(_turns(300, with_tools=True))
        await _settle(panel, pilot)
        if expanded:
            assert panel.toggle_fold_all() is False
            await _settle(panel, pilot)
        if direction > 0:
            panel.scroll_to_turn("turn-20")
            await wait_for(
                lambda: (
                    not panel.history_layout_is_held() and panel._history_jump is None and bool(panel.query("#turn-20"))
                ),
                pilot=pilot,
                description="the cold TOC jump has landed",
            )
            await _settle(panel, pilot)
        scroll = panel._history_scroll
        before = len(panel.pending_transcript_widgets())
        for _ in range(14):
            layout = scroll.visible_layout
            assert layout is not None
            top = panel.scroll_y
            anchor = next(p for p in layout.placements if not p.fixed and p.region.bottom > top)
            height = panel.scrollable_content_region.height

            panel.user_scroll_page(direction)
            await wait_for(
                lambda old_layout=layout, old_top=top: (
                    not panel.history_layout_is_held()
                    and scroll.relative is None
                    and (panel.scroll_y != old_top or scroll.visible_layout is not old_layout)
                ),
                pilot=pilot,
                description="the previous page is laid out",
            )
            await _settle(panel, pilot)
            layout = scroll.visible_layout
            assert layout is not None
            previous_top = next(p.region.y for p in layout.placements if p.widget is anchor.widget)
            assert previous_top + (top - anchor.region.y) == panel.scroll_y - direction * height

        assert 0 < len(panel.pending_transcript_widgets()) < before


async def test_wheel_keeps_a_three_row_step_at_an_expanded_cold_boundary() -> None:
    async with ChatPanelApp().run_test(size=(100, 40)) as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.replay_history(_turns(120, with_tools=True))
        await _settle(panel, pilot)
        assert panel.toggle_fold_all() is False
        await _settle(panel, pilot)
        layout = panel._history_scroll.visible_layout
        assert layout is not None
        anchor = next(p.widget for p in layout.placements if isinstance(p.widget, UserMessage))
        panel.scroll_to(y=anchor.virtual_region.y + 1, animate=False, immediate=True)
        await _settle(panel, pilot)
        start = panel.scroll_y
        assert start > 3 and start == anchor.virtual_region.y + 1
        pilot.app.scroll_sensitivity_y = 3
        x, y = panel.region.x + 1, panel.content_region.y + 3
        pilot.app.post_message(events.MouseScrollUp(None, x, y, 0, 1, 0, False, False, False))
        await wait_for(
            lambda: (
                panel.scroll_y != start
                and panel._history_scroll.relative is None
                and not panel.history_layout_is_held()
            ),
            pilot=pilot,
            description="wheel motion has landed in the measured page",
        )
        await _settle(panel, pilot)
        assert panel.scroll_y == anchor.virtual_region.y + 1 - 3


async def test_two_warm_page_keys_before_a_refresh_advance_two_pages() -> None:
    async with ChatPanelApp().run_test(size=_SIZE) as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.replay_history(_turns(36))
        await _wait_complete(panel)
        await _settle(panel, pilot)
        start = panel.scroll_y
        height = panel.scrollable_content_region.height
        panel.user_scroll_page(-1)
        panel.user_scroll_page(-1)
        await _settle(panel, pilot)
        assert panel.scroll_y == start - 2 * height


@pytest.mark.parametrize("source", ["wheel", "page", "track", "line-key"])
async def test_vertical_scroll_uses_the_visible_row_when_superseding_a_cold_toc_jump(
    monkeypatch: pytest.MonkeyPatch, source: str
) -> None:
    async with ChatPanelApp().run_test(size=_SIZE) as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.replay_history(_turns(120, with_tools=True))
        await _settle(panel, pilot)
        start = panel.scroll_y
        gate = _PrependGate(panel, monkeypatch)
        panel.scroll_to_turn("turn-4")
        try:
            await gate.wait_held(pilot)
            assert panel._history_jump is not None and panel.scroll_y == start
            if source == "wheel":
                x, y = panel.region.x + 1, panel.content_region.y + 3
                pilot.app.post_message(events.MouseScrollUp(None, x, y, 0, 1, 0, False, False, False))
                distance = pilot.app.scroll_sensitivity_y
            elif source == "line-key":
                panel.action_scroll_up()
                distance = 1
            else:
                if source == "page":
                    panel.user_scroll_page(-1)
                else:
                    panel._on_scroll_up(ScrollUp())
                distance = panel.scrollable_content_region.height
            await wait_for(
                lambda: (
                    panel._history_jump is None
                    and panel._history_scroll.pending is None
                    and panel.scroll_y == start - distance
                ),
                pilot=pilot,
                description="the scroll is relative to the previously visible view",
            )
        finally:
            gate.release.set()


@pytest.mark.parametrize("return_to_start", [False, True], ids=["queued-reversal", "cancel-cold-mount"])
async def test_multiple_page_steps_use_the_painted_origin_while_a_cold_page_mounts(
    monkeypatch: pytest.MonkeyPatch, return_to_start: bool
) -> None:
    async with ChatPanelApp().run_test(size=(100, 40)) as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.replay_history(_turns(120, with_tools=True))
        await _settle(panel, pilot)
        anchor = panel.query(UserMessage).first()
        start = anchor.virtual_region.y + 1
        panel.scroll_to(y=start, animate=False, immediate=True)
        await _settle(panel, pilot)
        assert panel.scroll_y == start
        mount = panel.mount_replay_batch
        entered, release = asyncio.Event(), asyncio.Event()

        async def hold_mount(batch: list[Widget], *, before: Widget | None) -> None:
            if before is not None:
                entered.set()
                await release.wait()
            await mount(batch, before=before)

        monkeypatch.setattr(panel, "mount_replay_batch", hold_mount)
        try:
            panel.action_page_up()
            await wait_for(entered.is_set, pilot=pilot, description="the first cold page is loading")
            assert panel.scroll_y == start
            if not return_to_start:
                panel.action_page_up()
            panel.action_page_down()
            if return_to_start:
                assert panel._history_scroll.relative is None
                assert panel._history_scroll.pending is None
                assert panel.scroll_y == panel.scroll_target_y == start
        finally:
            release.set()

        await wait_for(
            lambda: not panel.history_layout_is_held() and panel._history_scroll.relative is None,
            pilot=pilot,
            description="the queued page steps have landed",
        )
        await _settle(panel, pilot)
        distance = 0 if return_to_start else panel.scrollable_content_region.height
        assert panel.scroll_y == anchor.virtual_region.y + 1 - distance


async def test_page_down_canceling_a_cold_toc_jump_resumes_bottom_follow(monkeypatch: pytest.MonkeyPatch) -> None:
    async with ChatPanelApp().run_test(size=_SIZE) as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.replay_history(_turns(120, with_tools=True))
        await _settle(panel, pilot)
        assert not panel.is_anchor_released()
        gate = _PrependGate(panel, monkeypatch)
        panel.scroll_to_turn("turn-4")
        try:
            await gate.wait_held(pilot)
            assert panel._history_jump is not None
            panel.user_scroll_page(1)
            assert panel._history_jump is None
            assert panel.scroll_y == panel.max_scroll_y
            assert not panel.is_anchor_released()
        finally:
            gate.release.set()


@pytest.mark.parametrize("reverse", [False, True], ids=["bottom-follow", "reverse-from-bottom"])
async def test_cold_page_steps_clamp_to_the_measured_bottom(reverse: bool) -> None:
    async with ChatPanelApp().run_test(size=_SIZE) as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.replay_history(_turns(120, with_tools=True))
        await _settle(panel, pilot)
        height = panel.scrollable_content_region.height
        panel.scroll_to(y=panel.max_scroll_y - 10 * height, animate=False, immediate=True)
        await _settle(panel, pilot)

        for _ in range(12):
            panel.user_scroll_page(1)
        if reverse:
            panel.user_scroll_page(-1)
        assert panel._history_scroll.relative is not None
        await wait_for(
            lambda: panel._history_scroll.relative is None and not panel.history_layout_is_held(),
            pilot=pilot,
            description="the queued pages landed against the measured bottom",
        )
        await _settle(panel, pilot)
        assert panel.scroll_y == panel.max_scroll_y - (height if reverse else 0)
        assert panel.is_anchor_released() is reverse


@pytest.mark.parametrize(
    "action", ["no-input", "page-up", "cancel", "home", "thumb-top", "thumb-middle", "thumb-bottom", "thumb-held"]
)
async def test_navigation_waits_for_native_fallback(monkeypatch: pytest.MonkeyPatch, action: str) -> None:
    async with ChatPanelApp().run_test(size=_SIZE) as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.replay_history(_turns_with_overestimated_links())
        await _settle(panel, pilot)
        panel.scroll_to_turn("turn-60")
        await _settle(panel, pilot)
        anchor = panel.query_one("#turn-57")
        panel.scroll_to(y=anchor.virtual_region.y + 1, animate=False, immediate=True)
        await _settle(panel, pilot)

        entered, release = asyncio.Event(), asyncio.Event()
        wait_complete = panel._replay_mount.wait_complete
        bar = panel.vertical_scrollbar
        grab: Offset | None = None
        fraction = 0.0

        async def hold_native_drain(*, widgets=None):
            if widgets is None:
                entered.set()
                await release.wait()
            await wait_complete(widgets=widgets)

        gated = action not in {"no-input", "page-up"}
        if gated:
            monkeypatch.setattr(panel._replay_mount, "wait_complete", hold_native_drain)
        with panel.app.batch_update():
            if action != "no-input":
                panel.user_scroll_page(-1)
                assert panel._history_scroll.relative is not None
            await panel.query_one("#turn-119").remove()

        if gated:
            try:
                await wait_for(entered.is_set, pilot=pilot, description="native history drain is waiting")
                if action.startswith("thumb-"):
                    if action == "thumb-held":
                        grab = Offset(bar.region.x, bar.region.y + bar.window_size // 2)
                        bar._on_mouse_capture(events.MouseCapture(grab))
                        await bar._on_mouse_move(
                            events.MouseMove(
                                bar, 0, 0, 0, 0, 1, False, False, False, screen_x=grab.x, screen_y=grab.y + 2
                            )
                        )
                    else:
                        fraction = {"thumb-top": 0.0, "thumb-middle": 0.5, "thumb-bottom": 1.0}[action]
                        bar.post_message(ScrollTo(y=fraction * panel.max_scroll_y))
                    await wait_for(
                        lambda: (
                            (relative := panel._history_scroll.relative) is not None
                            and relative.native_origin_fraction is not None
                        ),
                        pilot=pilot,
                        description="the captured thumb target is waiting for native layout",
                    )
                    if grab is not None:
                        relative = panel._history_scroll.relative
                        assert relative is not None and relative.native_origin_fraction is not None
                        fraction = relative.native_origin_fraction
                        assert 0 < fraction < 1
                elif action == "home":
                    panel.action_scroll_home()
                else:
                    panel.note_user_scroll()
            finally:
                release.set()

        await wait_for(
            lambda: panel._history_viewport is None and panel._history_scroll.relative is None,
            pilot=pilot,
            description="the page step has landed after falling back to native layout",
        )
        await _settle(panel, pilot)
        if action.startswith("thumb-"):
            assert panel.scroll_y == pytest.approx(fraction * panel.max_scroll_y)
            assert panel.is_anchor_released() is (fraction != 1)
            if grab is not None:
                landed = panel.scroll_y
                await bar._on_mouse_move(
                    events.MouseMove(bar, 0, 0, 0, 0, 1, False, False, False, screen_x=grab.x + 1, screen_y=grab.y + 2)
                )
                await pilot.pause()
                assert panel.scroll_y == pytest.approx(landed)
                bar._on_mouse_release(events.MouseRelease(grab))
        elif action == "home":
            assert panel.scroll_y == 0
        else:
            distance = panel.scrollable_content_region.height if action == "page-up" else 0
            assert panel.scroll_y == anchor.virtual_region.y + 1 - distance


async def test_cold_mount_failure_during_resize_waits_for_navigation(monkeypatch: pytest.MonkeyPatch) -> None:
    async with ChatPanelApp().run_test(size=(100, 22)) as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.replay_history(_turns(128))
        await wait_for(lambda: panel._replay_mount.windowed and not panel._history_scroll.restoring_tail, pilot=pilot)
        panel.scroll_to_turn("turn-41")
        await _settle(panel, pilot)
        turn = panel.query_one("#turn-41")
        painted = panel._history_scroll.visible_layout
        assert turn.region.overlaps(panel.content_region)
        row = panel.scroll_y
        mount = panel.mount_replay_batch
        fail = True
        attempts = 0

        async def mount_archive(batch: list[Widget], *, before: Widget | None) -> None:
            nonlocal attempts
            if before is not None:
                attempts += 1
                if fail:
                    raise RuntimeError("interrupted archive mount")
            await mount(batch, before=before)

        monkeypatch.setattr(panel, "mount_replay_batch", mount_archive)
        await resize_when_settled(pilot, 100, 110)
        await wait_for(lambda: attempts > 0, pilot=pilot)
        for _ in range(3):
            panel.refresh(layout=True)
            await pilot.pause()
        assert attempts == 1
        assert panel._history_scroll.visible_layout is painted
        assert panel.scroll_y == row and turn.region.overlaps(panel.content_region)

        fail = False
        panel.scroll_to_turn("turn-41")
        await wait_for(
            lambda: attempts > 1 and not panel.history_layout_is_held() and panel._history_jump is None,
            pilot=pilot,
            description="the TOC jump retried the expanded viewport",
        )
        assert panel._history_scroll.visible_layout is not painted
        assert turn.region.overlaps(panel.content_region)


async def test_reentrant_layout_omits_a_root_removed_since_the_last_paint(monkeypatch: pytest.MonkeyPatch) -> None:
    async with ChatPanelApp().run_test(size=_SIZE) as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.replay_history(_turns(80))
        await panel.add_answer_delta("temporary", "Temporary response")
        await _settle(panel, pilot)
        temporary = next(message for message in panel.query(AgentMessage) if "temporary" in message.segment_ids)
        viewport = panel._history_viewport
        assert viewport is not None and viewport.last_layout is not None
        assert temporary in viewport.last_layout.widgets

        arrange = panel._arrange_history_entries
        checked = False

        def query_during_measurement(size: Size, entries: list[Widget]) -> DockArrangeResult:
            nonlocal checked
            borrowed = panel.arrange(size)
            assert temporary not in borrowed.widgets
            assert all(placement.widget is not temporary for placement in borrowed.placements)
            checked = True
            return arrange(size, entries)

        with panel.app.batch_update():
            await panel.withdraw_answer(("temporary",))
            monkeypatch.setattr(panel, "_arrange_history_entries", query_during_measurement)
            panel.refresh(layout=True)
        await wait_for(lambda: checked, pilot=pilot, description="chat geometry queried during measurement")


async def test_warm_fold_all_remeasures_only_the_viewport(monkeypatch: pytest.MonkeyPatch) -> None:
    async with ChatPanelApp().run_test(size=_SIZE) as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.replay_history(_turns(80, with_tools=True))
        await _wait_complete(panel)
        await _settle(panel, pilot)
        panel.scroll_to_turn("turn-41")
        await _settle(panel, pilot)
        turn = panel.query_one("#turn-41")
        top = turn.region.y
        assert panel._history_window is not None and panel.content_region.y <= top < panel.content_region.bottom
        set_window = panel._set_history_window
        windows: list[tuple[int, int] | None] = []

        def record_window(window: tuple[int, int] | None) -> None:
            windows.append(window)
            set_window(window)

        monkeypatch.setattr(panel, "_set_history_window", record_window)
        assert panel.toggle_fold_all() is False
        await _settle(panel, pilot)
        assert turn.region.y == top
        assert panel.toggle_fold_all() is True
        await _settle(panel, pilot)
        assert turn.region.y == top
        assert windows and None not in windows

        panel.scroll_to_turn("turn-4")
        await _settle(panel, pilot)
        assert panel.query_one("#turn-4").region.y == top


async def test_native_drain_failure_waits_for_an_explicit_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    async with ChatPanelApp().run_test(size=_SIZE) as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.replay_history(_turns(120))
        await _settle(panel, pilot)
        wait_complete = panel._replay_mount.wait_complete
        attempts = 0

        async def fail_first_drain(*, widgets=None):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("interrupted archive mount")
            await wait_complete(widgets=widgets)

        monkeypatch.setattr(panel._replay_mount, "wait_complete", fail_first_drain)
        await panel.query_one("#turn-119").remove()
        await wait_for(lambda: panel._history_native_failed, pilot=pilot, description="the drain failed")
        for _ in range(3):
            panel.refresh(layout=True)
            await pilot.pause()
        assert attempts == 1

        panel.action_scroll_home()
        await wait_for(
            lambda: panel._history_viewport is None and panel.scroll_y == 0,
            pilot=pilot,
            description="Home retried the drain and then reached the top",
        )
        assert attempts == 2


@pytest.mark.parametrize("retry", [False, True], ids=["active-animation", "page-after-failure"])
async def test_native_fallback_retires_scroll_animation_in_the_old_coordinates(
    monkeypatch: pytest.MonkeyPatch, retry: bool
) -> None:
    async with ChatPanelApp().run_test(size=_SIZE) as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.replay_history(_turns(72, with_tools=True))
        await _settle(panel, pilot)
        assert panel.toggle_fold_all() is False
        await _settle(panel, pilot)
        distance = panel.scrollable_content_region.height if retry else 0
        panel.scroll_to(y=panel.max_scroll_y - 13, animate=False, immediate=True)
        await _settle(panel, pilot)
        layout = panel._history_scroll.visible_layout
        assert layout is not None
        row = panel.scroll_y
        anchor = next(p for p in layout.placements if not p.fixed and p.region.bottom > row)
        animator = pilot.app.animator

        wait_complete = panel._replay_mount.wait_complete
        fail = retry

        async def fail_first_drain(*, widgets: list[Widget] | None = None) -> None:
            nonlocal fail
            if fail and widgets is None:
                fail = False
                raise RuntimeError("interrupted archive mount")
            await wait_complete(widgets=widgets)

        monkeypatch.setattr(panel._replay_mount, "wait_complete", fail_first_drain)
        with panel.app.batch_update():
            if not retry:
                panel.scroll_to(y=panel.max_scroll_y, animate=True, duration=60.0, immediate=True)
                animation = animator._animations[(id(panel), "scroll_y")]
                assert isinstance(animation, SimpleAnimation)
                animation.easing = lambda _: 0.0
            await panel.query_one("#turn-71").remove()
        if retry:
            await wait_for(lambda: panel._history_native_failed, pilot=pilot, description="the first drain failed")
            panel.action_page_up()
            assert panel._history_scroll.relative is not None

        await wait_for(lambda: panel._history_viewport is None, pilot=pilot, description="native layout accepted")
        await _settle(panel, pilot)
        expected = anchor.widget.virtual_region.y + (row - anchor.region.y) - distance
        assert not animator.is_being_animated(panel, "scroll_y")
        assert panel.scroll_y == expected
        assert panel.is_anchor_released()


@pytest.mark.parametrize("when", ["loading", "failed", "failed-down", "ready", "separator", "same-thumb"])
async def test_canceling_a_second_cold_mount_keeps_the_painted_anchor(
    monkeypatch: pytest.MonkeyPatch, when: str
) -> None:
    separator = when == "separator"
    async with ChatPanelApp().run_test(size=(80, 40 if separator else 36)) as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.replay_history(_turns_with_overestimated_links(with_tools=separator))
        await _settle(panel, pilot)
        panel.scroll_to_turn("turn-60")
        await _settle(panel, pilot)
        anchor = panel.query_one("#turn-57")
        start = anchor.virtual_region.bottom if separator else anchor.virtual_region.y + 1
        panel.scroll_to(y=start, animate=False, immediate=True)
        await _settle(panel, pilot)

        scroll = panel._history_scroll
        published = scroll.visible_layout
        request_widgets = scroll.request_widgets
        ready = scroll._ready
        second_request = asyncio.Event()
        canceled = asyncio.Event()
        second_mount: tuple[HistoryScrollRequest, list[Widget]] | None = None
        calls = 0

        def hold_second_request(request: HistoryScrollRequest, widgets: list[Widget]) -> None:
            nonlocal calls, second_mount
            calls += 1
            if calls == 2:
                second_mount = request, widgets
                second_request.set()
            else:
                request_widgets(request, widgets)

        def cancel_after_ready(request):
            ready(request)
            if second_request.is_set() and not canceled.is_set():
                assert scroll.relative is not None and not scroll.has_request
                panel.note_user_scroll()
                canceled.set()

        monkeypatch.setattr(scroll, "request_widgets", hold_second_request)
        if when == "ready":
            monkeypatch.setattr(scroll, "_ready", cancel_after_ready)
        panel.user_scroll_page(-1)
        await wait_for(second_request.is_set, pilot=pilot, description="remeasurement exposed another cold page")
        if when == "ready":
            assert second_mount is not None
            # Let the real mount callback run before another layout can accept
            # the measured window and retire the request itself.
            with panel.app.batch_update():
                request_widgets(*second_mount)
                await wait_for(canceled.is_set, description="the second mount has called back")
        else:
            assert scroll.visible_layout is published
            assert panel._history_viewport is not None and panel._history_viewport.last_layout is not published
            if when in ("failed", "failed-down"):
                panel._history_window_failed()
                if when == "failed-down":
                    panel.action_scroll_down()
            elif when == "same-thumb":
                panel.vertical_scrollbar.post_message(ScrollTo(y=start))
            else:
                panel.note_user_scroll()
        await wait_for(
            lambda: not panel.history_layout_is_held() and scroll.relative is None,
            pilot=pilot,
            description="the second mount was canceled",
        )
        await _settle(panel, pilot)
        expected = anchor.virtual_region.bottom if separator else anchor.virtual_region.y + 1
        if when == "failed-down":
            expected += 1
        assert panel.scroll_y == expected


@pytest.mark.parametrize("action", ["cancel", "page-up", "native", "cancel-native", "same-thumb-native"])
async def test_canceling_a_distant_cold_jump_keeps_the_painted_page(
    monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    async with ChatPanelApp().run_test(size=(80, 36)) as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.replay_history(_turns_with_overestimated_links())
        await _settle(panel, pilot)
        panel.scroll_to_turn("turn-100")
        await _settle(panel, pilot)
        anchor = panel.query_one("#turn-100")
        offset = panel.scroll_y - anchor.virtual_region.y
        height = panel.scrollable_content_region.height
        scroll = panel._history_scroll
        request_widgets = scroll.request_widgets
        second_request = asyncio.Event()
        calls = 0

        def hold_second_request(request, widgets):
            nonlocal calls
            calls += 1
            if calls == 2:
                second_request.set()
            else:
                request_widgets(request, widgets)

        monkeypatch.setattr(scroll, "request_widgets", hold_second_request)
        panel.scroll_to_turn("turn-50")
        await wait_for(second_request.is_set, pilot=pilot, description="the distant TOC jump needs a second mount")
        viewport = panel._history_viewport
        assert viewport is not None and viewport.last_layout is not None and anchor not in viewport.last_layout.widgets
        if action.endswith("native"):
            with panel.app.batch_update():
                if action == "cancel-native":
                    panel.note_user_scroll()
                elif action == "same-thumb-native":
                    panel._on_scroll_to(ScrollTo(y=scroll.visible_row))
                panel.user_scroll_page(-1)
                await panel.query_one("#turn-119").remove()
        elif action == "page-up":
            panel.user_scroll_page(-1)
        else:
            panel.note_user_scroll()

        await wait_for(
            lambda: scroll.relative is None and not panel.history_layout_is_held(),
            pilot=pilot,
            description="navigation resumed from the painted page",
        )
        await _settle(panel, pilot)
        assert panel.scroll_y == anchor.virtual_region.y + offset - (height if action != "cancel" else 0)
        if action.endswith("native"):
            assert panel._history_viewport is None


@pytest.mark.parametrize("source", ["old-thumb", "old-wheel", "exact-thumb", "exact-wheel", "cancel"])
async def test_returning_during_cold_remeasurement_restores_bottom_follow(
    monkeypatch: pytest.MonkeyPatch, source: str
) -> None:
    async with ChatPanelApp().run_test(size=(80, 36)) as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.replay_history(_turns_with_overestimated_links())
        await _settle(panel, pilot)
        panel.agent_running = True
        panel.final_response_started = True
        old_bottom = panel.max_scroll_y
        scroll = panel._history_scroll
        request_widgets = scroll.request_widgets
        remeasuring = asyncio.Event()
        calls = 0

        def hold_second_request(request, widgets):
            nonlocal calls
            calls += 1
            if calls == 2:
                remeasuring.set()
            else:
                request_widgets(request, widgets)

        monkeypatch.setattr(scroll, "request_widgets", hold_second_request)
        bar = panel.vertical_scrollbar
        bar.post_message(ScrollTo(y=old_bottom / 2))
        await wait_for(remeasuring.is_set, pilot=pilot, description="the distant jump has a second cold mount")
        assert scroll.pending is not None and panel.scroll_y == old_bottom
        viewport = panel._history_viewport
        assert viewport is not None and viewport.last_layout is not None
        bottom = viewport.last_layout.total_region.bottom - panel.scrollable_content_region.height
        assert bottom < old_bottom
        target = bottom if source.startswith("exact-") else old_bottom
        if source == "cancel":
            panel.note_user_scroll()
        elif source.endswith("-thumb"):
            bar.post_message(ScrollTo(y=target))
        else:
            pilot.app.scroll_sensitivity_y = target - round(scroll.pending.row)
            panel._on_mouse_scroll_down(events.MouseScrollDown(panel, 2, 5, 0, 0, 0, False, False, False))

        await wait_for(
            lambda: panel.scroll_y == panel.max_scroll_y < old_bottom and not panel.is_anchor_released(),
            pilot=pilot,
            description="the measured bottom resumes following",
        )
        bottom = panel.max_scroll_y
        await panel.mount(AgentMessage("\n\n".join("Reply paragraph." for _ in range(40))))
        await wait_for(
            lambda: panel.scroll_y == panel.max_scroll_y > bottom,
            pilot=pilot,
            description="the viewport follows new output",
        )


async def test_page_down_from_a_fractional_thumb_position_mounts_the_last_visible_row() -> None:
    async with ChatPanelApp().run_test(size=(80, 17)) as pilot:
        panel = pilot.app.query_one(ChatPanel)
        messages = _turns(80)
        messages[1]["contents"][0]["text"] = "word " * 17
        await panel.replay_history(messages)
        await _settle(panel, pilot)
        panel.scroll_to_turn("turn-1")
        await wait_for(
            lambda: panel._history_jump is None and not panel.history_layout_is_held(),
            pilot=pilot,
            description="the first turn is visible",
        )
        await _settle(panel, pilot)

        panel.vertical_scrollbar.post_message(ScrollTo(y=2.5))
        await wait_for(lambda: panel.scroll_y == 2.5, pilot=pilot, description="the thumb reached the fractional row")
        await _settle(panel, pilot)
        assert not panel.query("#turn-5")

        panel.user_scroll_page(1)
        await wait_for(
            lambda: panel._history_scroll.relative is None and not panel.history_layout_is_held(),
            pilot=pilot,
            description="Page Down has laid out the next screen",
        )
        await _settle(panel, pilot)
        assert panel.scroll_y == 17.5
        region = panel.scrollable_content_region
        assert panel.query_one("#turn-5").region.y == region.bottom - 1
        strips = pilot.app.screen._compositor.render_strips()
        assert "You" in strips[region.bottom - 1].crop(region.x, region.right).text


@pytest.mark.parametrize("scroll", ["page", "wheel", "track", "line-key", "end", "home", "home-page"])
async def test_early_scroll_input_is_relative_to_the_restored_tail(scroll: str) -> None:
    async with ChatPanelApp().run_test(size=_SIZE) as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.replay_history(_turns(120))
        assert panel._history_scroll.restoring_tail
        suffix_bottom = panel.max_scroll_y
        distance = panel.scrollable_content_region.height
        if scroll == "wheel":
            panel._on_mouse_scroll_up(events.MouseScrollUp(panel, 2, 5, 0, 0, 0, False, False, False))
            distance = pilot.app.scroll_sensitivity_y
        elif scroll == "track":
            panel._on_scroll_up(ScrollUp())
        elif scroll == "line-key":
            panel.action_scroll_up()
            distance = 1
        else:
            panel.user_scroll_page(-1)
            panel.user_scroll_page(-1)
            panel.user_scroll_page(1)
            if scroll == "end":
                panel.action_scroll_end()
                distance = 0
            elif scroll in {"home", "home-page"}:
                panel.action_scroll_home()
                if scroll == "home-page":
                    panel.user_scroll_page(1)

        from_home = scroll in {"home", "home-page"}
        if scroll == "home":
            distance = 0
        await wait_for(
            lambda: (
                not panel.history_layout_is_held()
                and panel.max_scroll_y > suffix_bottom
                and panel.scroll_y == (distance if from_home else panel.max_scroll_y - distance)
            ),
            pilot=pilot,
            description="early scroll input reached its destination in the indexed transcript",
        )
        if from_home:
            assert panel.query("#turn-1")
            return
        assert not panel.query("#turn-1")
        assert any(
            message.region.overlaps(panel.content_region)
            for message in panel.query(UserMessage)
            if message.turn_number is not None and message.turn_number > 110
        )


@pytest.mark.parametrize("expanded", [False, True], ids=["folded", "expanded"])
async def test_early_home_then_page_down_measures_the_first_screen(expanded: bool) -> None:
    async with ChatPanelApp().run_test(size=(35, 24)) as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.replay_history(_turns(80, with_tools=True))
        assert panel._history_scroll.restoring_tail
        if expanded:
            assert panel.toggle_fold_all() is False
        height = panel.scrollable_content_region.height
        panel.action_scroll_home()
        panel.user_scroll_page(1)

        await _settle(panel, pilot)
        assert panel.scroll_y == height


@pytest.mark.parametrize("home", [False, True], ids=["page-up", "home"])
async def test_early_scroll_input_keeps_the_visible_tail_while_the_first_cold_page_mounts(
    monkeypatch: pytest.MonkeyPatch,
    home: bool,
) -> None:
    # Keep the last archived page plus the live tail, less than the viewport.
    monkeypatch.setattr(history_viewport, "REPLAY_FOREGROUND_ENTRY_LIMIT", 10)
    async with ChatPanelApp().run_test(size=(100, 60)) as pilot:
        panel = pilot.app.query_one(ChatPanel)
        mount = panel.mount_replay_batch
        entered, release = asyncio.Event(), asyncio.Event()

        async def hold_cold_mount(batch: list[Widget], *, before: Widget | None) -> None:
            if before is not None:
                entered.set()
                await release.wait()
            await mount(batch, before=before)

        monkeypatch.setattr(panel, "mount_replay_batch", hold_cold_mount)
        try:
            await panel.replay_history(_turns(121))
            assert panel._history_scroll.restoring_tail
            await wait_for(entered.is_set, pilot=pilot, description="the first indexed viewport needs another page")
            assert panel._history_scroll.visible_row == panel.max_scroll_y
            height = panel.scrollable_content_region.height
            panel.user_scroll_page(-1)
            if home:
                panel.action_scroll_home()
            await pilot.pause()

            region = panel.scrollable_content_region
            strips = pilot.app.screen._compositor.render_strips()
            assert any(
                "question 120" in strip.crop(region.x, region.right).text for strip in strips[region.y : region.bottom]
            )
        finally:
            release.set()

        await wait_for(
            lambda: (
                panel.scroll_y == (0 if home else panel.max_scroll_y - height) and not panel.history_layout_is_held()
            ),
            pilot=pilot,
            description="the scroll reached its destination",
        )
        assert bool(panel.query("#turn-1")) == home


async def test_a_toc_jump_clamped_to_the_bottom_resumes_following_live_output() -> None:
    async with ChatPanelApp().run_test(size=_SIZE) as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.replay_history(_turns(120))
        await _settle(panel, pilot)
        panel.scroll_to_turn("turn-1")
        await wait_for(lambda: panel.scroll_y == 0, pilot=pilot, description="oldest turn is visible")

        panel.agent_running = True
        panel.final_response_started = True
        panel.scroll_to_turn("turn-119")
        await wait_for(
            lambda: panel.scroll_y == panel.max_scroll_y and panel.query_one("#turn-119").region,
            pilot=pilot,
            description="near-tail TOC jump landed at the bottom",
        )
        old_bottom = panel.max_scroll_y
        await panel.mount(AgentMessage("\n\n".join("Reply paragraph." for _ in range(40))))
        await wait_for(
            lambda: panel.scroll_y == panel.max_scroll_y > old_bottom,
            pilot=pilot,
            description="the viewport followed new output",
        )
        assert not panel.is_anchor_released()


async def test_early_home_then_scrolling_past_the_bottom_keeps_following_live_output() -> None:
    async with ChatPanelApp().run_test(size=_SIZE) as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.replay_history(_turns(120))
        assert panel._history_scroll.restoring_tail
        panel.action_scroll_home()
        for _ in range(60):
            panel.user_scroll_page(1)

        await wait_for(
            lambda: (
                not panel._history_scroll.restoring_tail
                and panel._history_scroll.startup is None
                and panel._history_scroll.relative is None
                and panel.scroll_y == panel.max_scroll_y
            ),
            pilot=pilot,
            description="the queued scroll landed at the restored bottom",
        )
        assert not panel.is_anchor_released()
        old_bottom = panel.max_scroll_y
        await panel.mount(AgentMessage("\n\n".join("Reply paragraph." for _ in range(40))))
        await wait_for(
            lambda: panel.scroll_y == panel.max_scroll_y > old_bottom,
            pilot=pilot,
            description="the viewport followed new output",
        )


@pytest.mark.parametrize("from_home", [False, True], ids=["past-top", "past-bottom"])
async def test_early_page_reversal_moves_away_from_the_overshot_edge(from_home: bool) -> None:
    async with ChatPanelApp().run_test(size=(100, 160)) as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.replay_history(_turns(65))
        assert panel._history_scroll.restoring_tail
        if from_home:
            panel.action_scroll_home()
        direction = 1 if from_home else -1
        for _ in range(5):
            panel.user_scroll_page(direction)
        panel.user_scroll_page(-direction)

        await wait_for(
            lambda: (
                not panel._history_scroll.restoring_tail
                and panel._history_scroll.startup is None
                and panel._history_scroll.relative is None
            ),
            pilot=pilot,
            description="the queued page steps were applied to the indexed transcript",
        )
        await _settle(panel, pilot)
        height = panel.scrollable_content_region.height
        assert height < panel.max_scroll_y < 4 * height
        assert panel.scroll_y == (panel.max_scroll_y - height if from_home else height)


@pytest.mark.parametrize(
    ("from_home", "expand_after_layout"),
    [(False, False), (True, False), (False, True)],
    ids=["from-tail", "from-home", "expand-after-layout"],
)
async def test_early_page_steps_back_from_an_estimated_edge_use_the_expanded_extent(
    monkeypatch: pytest.MonkeyPatch, from_home: bool, expand_after_layout: bool
) -> None:
    async with ChatPanelApp().run_test(size=(100, 140)) as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.replay_history(_turns(65, with_tools=True))
        assert panel._history_scroll.restoring_tail
        tail_group = panel.query(ToolGroup).last()
        restore = ToolGroup._restore_completed_tool_widgets
        opened, preparing, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def hold_tail_group(group: ToolGroup) -> None:
            if group is tail_group:
                if opened.is_set():
                    preparing.set()
                opened.set()
                await release.wait()
            await restore(group)

        monkeypatch.setattr(ToolGroup, "_restore_completed_tool_widgets", hold_tail_group)
        if expand_after_layout:
            apply_scroll = panel._apply_restore_scroll

            def expand_before_applying(generation: int, prepared_fold: int | None) -> None:
                if prepared_fold is None:
                    layout = panel._history_scroll.visible_layout
                    assert layout is not None and tail_group not in layout.widgets
                    assert panel.toggle_fold_all() is False
                apply_scroll(generation, prepared_fold)

            monkeypatch.setattr(panel, "_apply_restore_scroll", expand_before_applying)
        else:
            assert panel.toggle_fold_all() is False
        if from_home:
            panel.action_scroll_home()

        direction = 1 if from_home else -1
        for _ in range(6):
            panel.user_scroll_page(direction)
        for _ in range(5):
            panel.user_scroll_page(-direction)

        scroll = panel._history_scroll
        try:
            await wait_for(preparing.is_set, description="startup navigation is waiting for the expanded tail")
            assert scroll.startup is not None
        finally:
            release.set()
        await wait_for(
            lambda: not (scroll.restoring_tail or scroll.startup or scroll.relative or panel.history_layout_is_held()),
            pilot=pilot,
            description="the queued page steps reached the actual expanded transcript rows",
        )
        await _settle(panel, pilot)
        height = panel.scrollable_content_region.height
        assert panel.max_scroll_y > 6 * height
        assert panel.scroll_y == (height if from_home else panel.max_scroll_y - height)


async def test_page_down_while_cold_home_is_loading_starts_at_the_top(monkeypatch: pytest.MonkeyPatch) -> None:
    async with ChatPanelApp().run_test(size=(100, 320)) as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.replay_history(_turns(65, with_tools=True))
        await _settle(panel, pilot)
        assert panel.toggle_fold_all() is False
        await _settle(panel, pilot)
        height = panel.scrollable_content_region.height
        gate = _PrependGate(panel, monkeypatch)
        panel.action_scroll_home()
        try:
            await gate.wait_held(pilot)
            assert panel._history_scroll.pending is not None and panel._history_scroll.pending.row == 0
            panel.user_scroll_page(1)
            assert panel._history_scroll.relative is not None
        finally:
            gate.release.set()

        await wait_for(
            lambda: panel._history_scroll.relative is None and not panel.history_layout_is_held(),
            pilot=pilot,
            description="Page Down landed one measured screen from the top",
        )
        await _settle(panel, pilot)
        assert panel.scroll_y == height


async def test_page_reversal_that_clamps_at_both_edges_keeps_measuring(monkeypatch: pytest.MonkeyPatch) -> None:
    async with ChatPanelApp().run_test(size=(100, 160)) as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.replay_history(_turns(65, with_tools=True))
        await _settle(panel, pilot)
        assert panel.toggle_fold_all() is False
        panel.scroll_to_turn("turn-35")
        await _settle(panel, pilot)
        height = panel.scrollable_content_region.height
        panel.scroll_to(y=panel.max_scroll_y - 2 * height, animate=False, immediate=True)
        await _settle(panel, pilot)
        estimated_bottom = panel.max_scroll_y
        start = panel.scroll_y
        layout = panel._history_scroll.visible_layout
        assert layout is not None and start == estimated_bottom - 2 * height
        anchor = next(
            placement for placement in layout.placements if not placement.fixed and placement.region.bottom > start
        )

        gate = _PrependGate(panel, monkeypatch)
        try:
            for _ in range(3):
                panel.user_scroll_page(-1)
            await gate.wait_held(pilot)
            for _ in range(5):
                panel.user_scroll_page(1)
            assert panel._history_scroll.relative is not None
        finally:
            gate.release.set()

        await wait_for(
            lambda: panel._history_scroll.relative is None and not panel.history_layout_is_held(),
            pilot=pilot,
            description="the reversal reached its measured destination",
        )
        await _settle(panel, pilot)
        assert panel.max_scroll_y > estimated_bottom
        await _wait_complete(panel)
        await _settle(panel, pilot)
        viewport = panel._history_viewport
        assert viewport is not None
        viewport.invalidate_measurements()
        layout = panel.arrange(panel.scrollable_content_region.size)
        current_anchor = next(placement for placement in layout.placements if placement.widget is anchor.widget)
        rebased = current_anchor.region.y + start - anchor.region.y
        expected = min(panel.max_scroll_y, max(0, rebased - 3 * height) + 5 * height)
        assert panel.scroll_y == expected


@pytest.mark.parametrize(
    ("first", "second"),
    [("home", "pagedown"), ("pagedown", "home"), ("end", "pageup"), ("pageup", "end")],
)
async def test_page_and_boundary_keys_apply_in_the_order_received(first: str, second: str) -> None:
    async with ChatPanelApp().run_test(size=_SIZE) as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.replay_history(_turns(120))
        await _settle(panel, pilot)
        panel.scroll_to_turn("turn-59")
        await _settle(panel, pilot)
        assert 0 < panel.scroll_y < panel.max_scroll_y
        pilot.app.screen.set_focus(panel, scroll_visible=False)
        for key in (first, second):
            pilot.app.post_message(events.Key(key, None))

        await pilot.pause()
        await pilot.app.animator.wait_until_complete()
        await wait_for(
            lambda: panel._history_scroll.relative is None and not panel.history_layout_is_held(),
            pilot=pilot,
            description="the navigation key burst completed",
        )
        await _settle(panel, pilot)
        height = panel.scrollable_content_region.height
        expected = {"home": 0, "pagedown": height, "end": panel.max_scroll_y, "pageup": panel.max_scroll_y - height}
        assert panel.scroll_y == expected[second]


async def test_a_cold_toc_jump_supersedes_a_held_thumb_request(monkeypatch: pytest.MonkeyPatch) -> None:
    async with ChatPanelApp().run_test(size=_SIZE) as pilot:
        cp = pilot.app.query_one(ChatPanel)
        await cp.replay_history(_turns(120, with_tools=True))
        await _settle(cp, pilot)
        assert cp.toggle_fold_all() is False
        await _settle(cp, pilot)
        bar = cp.vertical_scrollbar
        gate = _PrependGate(cp, monkeypatch)

        def painted_prompts() -> set[str]:
            region = cp.scrollable_content_region
            strips = pilot.app.screen._compositor.render_strips()
            rows = (strip.crop(region.x, region.right).text for strip in strips[region.y : region.bottom])
            return {row[row.index("question ") :].rstrip() for row in rows if "question " in row}

        bar.grabbed = Offset(0, 0)
        bar.post_message(ScrollTo(y=cp.max_scroll_y * 0.65))
        try:
            await gate.wait_held(pilot)
            bar.grabbed = None
            await pilot.pause()
            assert cp._history_scroll.pending is not None and cp.scroll_y == cp.max_scroll_y
            held_prompts = painted_prompts()
            assert "question 119" in held_prompts

            cp.scroll_to_turn("turn-62")
            await pilot.pause()
            assert painted_prompts() == held_prompts
        finally:
            bar.grabbed = None
            gate.release.set()

        await wait_for(lambda: "question 61" in painted_prompts(), pilot=pilot, description="native TOC destination")
        target = cp.query_one("#turn-62", UserMessage)
        top_with_margin = cp.content_region.y + 1
        assert target.region.y == top_with_margin
        assert any(not group.collapsed and group.region.overlaps(cp.content_region) for group in cp.query(ToolGroup))

        cp.scroll_to_turn("turn-63")
        await _settle(cp, pilot)
        cp.scroll_to_turn("turn-62")
        await _settle(cp, pilot)
        assert target.region.y == top_with_margin


async def test_widening_while_next_page_is_cold_keeps_the_reading_anchor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    messages = _turns(120)
    for message in messages:
        if message["role"] == "assistant":
            message["contents"] = [{"type": "text", "text": "word " * 120}]

    async with ChatPanelApp().run_test(size=(35, 36)) as pilot:
        cp = pilot.app.query_one(ChatPanel)
        await cp.replay_history(messages)
        await _settle(cp, pilot)
        cp.scroll_to_turn("turn-3")
        await _settle(cp, pilot)
        message = cp.query_one("#turn-3", UserMessage)
        assert message.region.y == cp.content_region.y + 1
        assert not cp.query("#turn-5")

        gate = _PrependGate(cp, monkeypatch)
        try:
            await pilot.resize_terminal(180, 36)
            await gate.wait_held(pilot)
            pending = cp._history_scroll.pending
            viewport = cp._history_viewport
            assert pending is not None and viewport is not None and viewport.last_layout is not None
            measured_top = next(
                p.region.y - p.margin.top for p in viewport.last_layout.placements if p.widget is message
            )
            assert pending.row == measured_top
        finally:
            gate.release.set()
        await _settle(cp, pilot)
        assert cp._history_scroll.pending is None
        assert message.region.y == cp.content_region.y + 1


def _near_bottom_wrapped_history(count: int) -> list[dict[str, object]]:
    messages = _turns(count)
    replies = [message for message in messages if message["role"] == "assistant"]
    for reply in replies[-5:]:
        reply["contents"] = [{"type": "text", "text": "word " * 210}]
    return messages


async def test_widening_near_bottom_keeps_the_prior_frame_until_cold_rows_mount(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with ChatPanelApp().run_test(size=(36, 62)) as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.replay_history(_near_bottom_wrapped_history(85))
        await _settle(panel, pilot)
        panel.scroll_to(y=panel.max_scroll_y - 2, animate=False, immediate=True)
        await _settle(panel, pilot)
        cold_opener = "#turn-77"
        assert not panel.query(cold_opener)
        visible = panel._history_scroll.visible_layout
        row = panel.scroll_y
        gate = _PrependGate(panel, monkeypatch)
        try:
            await pilot.resize_terminal(220, 62)
            await gate.wait_held(pilot)
            assert panel.history_layout_is_held()
            assert panel._history_scroll.visible_layout is visible
            assert panel.scroll_y == row
        finally:
            gate.release.set()
        await wait_for(
            lambda: not panel.history_layout_is_held(), pilot=pilot, description="the resized view has landed"
        )
        await _settle(panel, pilot)
        assert panel.query(cold_opener)


@pytest.mark.parametrize("turns", [85, 87], ids=["cold-edge", "mounted-edge"])
async def test_widening_near_bottom_preserves_an_animated_page_up(monkeypatch: pytest.MonkeyPatch, turns: int) -> None:
    async with ChatPanelApp().run_test(size=(36, 62)) as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.replay_history(_near_bottom_wrapped_history(turns))
        await _settle(panel, pilot)
        panel.scroll_to(y=panel.max_scroll_y - 2, animate=False, immediate=True)
        await _settle(panel, pilot)
        scroll = panel._history_scroll
        layout = scroll.visible_layout
        assert layout is not None
        origin, height = panel.scroll_y, panel.scrollable_content_region.height
        animator = pilot.app.animator
        panel.action_page_up()
        animation = animator._animations[(id(panel), "scroll_y")]
        assert isinstance(animation, SimpleAnimation)
        easing = animation.easing
        animation.duration = 60.0
        animation.easing = lambda _: 0.0

        delay_resize_dispatch(pilot.app, monkeypatch, 0.25)
        await resize_when_settled(pilot, 220, 62)
        if animator.is_being_animated(panel, "scroll_y"):
            animation.duration = 0.1
            animation.easing = easing
        await wait_for(
            lambda: not panel.history_layout_is_held() and not animator.is_being_animated(panel, "scroll_y"),
            pilot=pilot,
            description="the rebased Page Up has landed",
        )
        await _settle(panel, pilot)
        assert scroll.visible_layout is not None
        rebased = history_viewport.rebase_history_row(layout, scroll.visible_layout, origin)
        assert rebased is not None
        assert panel.scroll_y == rebased - height < panel.max_scroll_y
        assert panel.is_anchor_released()


def _history_with_wrapped_pages(count: int) -> list[dict[str, object]]:
    messages = _turns(count)
    replies = [message for message in messages if message["role"] == "assistant"]
    # One long reply per archive page makes its height width-dependent.
    # Other replies still create pages without parsing another long Markdown block.
    for reply in replies[::4]:
        reply["contents"] = [{"type": "text", "text": "wrapped prose " * 120}]
    return messages


async def test_animated_home_reaches_the_top_after_widening() -> None:
    async with ChatPanelApp().run_test(size=(45, 24)) as pilot:
        cp = pilot.app.query_one(ChatPanel)
        await cp.replay_history(_history_with_wrapped_pages(36))
        await _wait_complete(cp)
        await _settle(cp, pilot)
        narrow_extent = cp.max_scroll_y
        await pilot.resize_terminal(140, 24)
        await _settle(cp, pilot)
        assert cp._history_window is not None
        assert 0 < cp.scroll_y <= cp.max_scroll_y < narrow_extent
        cp.action_scroll_home()
        await pilot.pause()
        await pilot.app.animator.wait_until_complete()
        await _settle(cp, pilot)
        assert cp.scroll_y == cp.scroll_target_y == 0


async def test_cold_tail_window_is_stable_during_chrome_visibility_reflow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with ChatPanelApp().run_test(size=_SIZE) as pilot:
        cp = pilot.app.query_one(ChatPanel)
        await cp.replay_history(_turns(120))
        await cp.mount(AgentMessage("\n\n".join("Live reply paragraph." for _ in range(60))))
        await _settle(cp, pilot)
        assert cp.scroll_y == cp.max_scroll_y
        window = cp._history_window
        assert window is not None

        windows = []
        set_window = cp._set_history_window

        def record_window(window):
            windows.append(window)
            set_window(window)

        monkeypatch.setattr(cp, "_set_history_window", record_window)
        button = cp.scroll_to_bottom_button()
        assert button is not None and not button.visible
        assert set_widget_visibility_without_layout(button, True)
        assert cp._history_window == window
        assert all(seen == window for seen in windows)


@pytest.mark.parametrize("track", [False, True], ids=["wheel", "track"])
async def test_relative_scroll_steps_accumulate_while_the_next_history_page_mounts(
    monkeypatch: pytest.MonkeyPatch, track: bool
) -> None:
    async with ChatPanelApp().run_test(size=_SIZE) as pilot:
        cp = pilot.app.query_one(ChatPanel)
        await cp.replay_history(_turns(120))
        await _settle(cp, pilot)
        first_mounted = cp.query(UserMessage).first()
        start = first_mounted.virtual_region.y + 1
        cp.release_anchor()
        cp.scroll_to(y=start, animate=False, immediate=True)
        await _settle(cp, pilot)
        assert cp.scroll_y == start
        entered, release = asyncio.Event(), asyncio.Event()
        mount_batch = cp.mount_replay_batch

        async def hold_cold_mount(batch: list[Widget], *, before: Widget | None) -> None:
            entered.set()
            await release.wait()
            await mount_batch(batch, before=before)

        monkeypatch.setattr(cp, "mount_replay_batch", hold_cold_mount)
        step = cp.scrollable_content_region.height if track else pilot.app.scroll_sensitivity_y

        def scroll_up() -> None:
            if track:
                cp.post_message(ScrollUp())
            else:
                cp.post_message(events.MouseScrollUp(cp, 2, 5, 0, 0, 0, False, False, False))

        def transcript_frame() -> tuple[str, ...]:
            region = cp.scrollable_content_region
            strips = pilot.app.screen._compositor.render_strips()
            return tuple(strip.crop(region.x, region.right).text for strip in strips[region.y : region.bottom])

        try:
            scroll_up()
            await wait_for(entered.is_set, pilot=pilot, description="the first cold request is waiting to mount")
            assert cp._history_scroll.pending is not None
            first_target = cp._history_scroll.pending.row
            first_frame = transcript_frame()
            for _ in range(3):
                scroll_up()
                await pilot.pause()
                if track:
                    await pilot.app.animator.wait_until_complete()
            assert cp._history_scroll.pending is not None
            assert cp._history_scroll.pending.row == first_target - 3 * step
            assert first_frame == transcript_frame()
        finally:
            release.set()
        await _settle(cp, pilot)
        assert cp._history_scroll.pending is None


@pytest.mark.parametrize(("return_with", "offset"), [("wheel", 0), ("wheel", 1), ("page", 0), ("track", 0)])
async def test_reversing_to_the_warm_view_cancels_the_cold_request(
    monkeypatch: pytest.MonkeyPatch, return_with: str, offset: int
) -> None:
    async with ChatPanelApp().run_test(size=_SIZE) as pilot:
        cp = pilot.app.query_one(ChatPanel)
        await cp.replay_history(_turns(120))
        await _settle(cp, pilot)
        start = cp.query(UserMessage).first().virtual_region.y + 1
        cp.release_anchor()
        cp.scroll_to(y=start, animate=False, immediate=True)
        await _settle(cp, pilot)
        assert cp.scroll_y == cp.scroll_target_y == start

        # Keep the requested page cold until the input reverses.
        monkeypatch.setattr(cp._history_scroll, "request_widgets", lambda *_args: None)
        bar = cp.vertical_scrollbar
        if return_with == "wheel":
            step = pilot.app.scroll_sensitivity_y
            up = events.MouseScrollUp(cp, 2, 5, 0, 0, 0, False, False, False)
            up._set_forwarded()
            cp.post_message(up)
        else:
            step = cp.scrollable_content_region.height
            bar.post_message(ScrollTo(y=start - step))
        await wait_for(
            lambda: cp._history_scroll.pending is not None, pilot=pilot, description="the input entered a cold page"
        )
        assert cp.scroll_y == start
        assert cp._history_scroll.pending is not None
        assert cp._history_scroll.pending.row == start - step

        if return_with == "page":
            cp.action_page_down()
        elif return_with == "track":
            bar.post_message(ScrollDown())
        else:
            pilot.app.scroll_sensitivity_y = step + offset
            down = events.MouseScrollDown(cp, 2, 5, 0, 0, 0, False, False, False)
            down._set_forwarded()
            cp.post_message(down)
        await pilot.pause()
        assert cp.scroll_y == cp.scroll_target_y == bar.position == start + offset
        assert cp._history_scroll.pending is None
        assert cp._history_scroll.mount_request is None


async def test_returning_to_a_held_scroll_row_reflows_after_resize(monkeypatch: pytest.MonkeyPatch) -> None:
    async with ChatPanelApp().run_test(size=(80, 24)) as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.replay_history(_turns(120))
        await _settle(panel, pilot)
        held_row = panel.scroll_y
        answer = panel.query(AgentMessage).last()
        original_width = answer.region.width
        monkeypatch.setattr(panel._history_scroll, "request_widgets", lambda *_: None)

        bar = panel.vertical_scrollbar
        bar.post_message(ScrollTo(y=held_row / 2, animate=False))
        await wait_for(lambda: panel._history_scroll.pending is not None, pilot=pilot)
        await pilot.resize_terminal(120, 24)
        await pilot.pause()
        assert panel.content_size.width > original_width
        assert answer.region.width == original_width

        bar.post_message(ScrollTo(y=held_row, animate=False))
        await pilot.pause()
        await _settle(panel, pilot)
        assert panel._history_scroll.pending is None
        assert answer.region.width > original_width
        assert answer.region.overlaps(panel.content_region)


@pytest.mark.parametrize("return_with", ["thumb", "end", "home", "wheel"])
async def test_canceling_a_cold_scroll_from_bottom_preserves_the_next_scroll_intent(
    monkeypatch: pytest.MonkeyPatch, return_with: str
) -> None:
    async with ChatPanelApp().run_test(size=_SIZE) as pilot:
        cp = pilot.app.query_one(ChatPanel)
        await cp.replay_history(_turns(120))
        await _settle(cp, pilot)
        cp.agent_running = True
        await _settle(cp, pilot)
        bar = cp.vertical_scrollbar
        button = cp._scroll_to_bottom_button
        assert button is not None
        assert cp.scroll_y == cp.max_scroll_y and not button.visible

        monkeypatch.setattr(cp._history_scroll, "request_widgets", lambda *_: None)
        target = cp.max_scroll_y / 2
        bar.post_message(ScrollTo(y=target, animate=False))
        await wait_for(
            lambda: cp._history_scroll.pending is not None, pilot=pilot, description="the thumb entered a cold page"
        )
        assert cp.scroll_y == cp.max_scroll_y
        assert cp._scroll_controller.auto_scroll_paused_by_user

        if return_with == "home":
            original_scroll_to = cp._scroll_to
            pause_before_home = []

            def record_scroll_to(*args, **kwargs):
                if len(args) > 1 and args[1] == 0:
                    pause_before_home.append(cp._scroll_controller.auto_scroll_paused_by_user)
                return original_scroll_to(*args, **kwargs)

            monkeypatch.setattr(cp, "_scroll_to", record_scroll_to)
            cp.action_scroll_home()
            await wait_for(
                lambda: cp._history_scroll.pending is not None and cp._history_scroll.pending.row == 0,
                pilot=pilot,
                description="Home starts loading the first archive page",
            )
            assert pause_before_home == [True]
            return

        if return_with == "end":
            cp.action_scroll_end()
        elif return_with == "wheel":
            pilot.app.scroll_sensitivity_y = cp.max_scroll_y - round(target)
            down = events.MouseScrollDown(cp, 2, 5, 0, 0, 0, False, False, False)
            down._set_forwarded()
            cp.post_message(down)
        else:
            # Messages from thumb motion may arrive after Textual cleared its capture.
            assert bar.grabbed is None
            bar.post_message(ScrollTo(y=cp.max_scroll_y + 5, animate=False))

        await pilot.pause()
        assert cp.scroll_y == cp.scroll_target_y == bar.position == cp.max_scroll_y
        assert cp._history_scroll.pending is None
        assert cp._history_scroll.mount_request is None
        assert not cp._anchor_released
        assert not cp._scroll_controller.auto_scroll_paused_by_user
        assert not button.visible


@pytest.mark.parametrize("next_input", ["wheel", "up"])
async def test_relative_scroll_after_canceling_a_cold_jump_starts_from_the_visible_row(
    monkeypatch: pytest.MonkeyPatch, next_input: str
) -> None:
    async with ChatPanelApp().run_test(size=_SIZE) as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.replay_history(_turns(120))
        await _settle(panel, pilot)
        start = panel.scroll_y
        assert start == panel.max_scroll_y
        gate = _PrependGate(panel, monkeypatch)
        pilot.app.screen.set_focus(panel, scroll_visible=False)

        def wheel_up(*, shift: bool = False) -> None:
            x = panel.region.x + 1
            y = panel.content_region.y + 3
            pilot.app.post_message(events.MouseScrollUp(None, x, y, 0, 1, 0, shift, False, False))

        try:
            panel.scroll_to_turn("turn-2")
            await gate.wait_held(pilot)
            assert panel._history_scroll.pending is not None
            assert panel.scroll_y == start

            wheel_up(shift=True)
            await pilot.pause()
            assert panel._history_scroll.pending is None
            assert panel.scroll_y == start

            gate.release.set()
            await wait_for(lambda: not panel._replay_mount.in_progress, pilot=pilot)
            if next_input == "wheel":
                expected = start - pilot.app.scroll_sensitivity_y
                wheel_up()
            else:
                expected = start - 1
                await pilot.press("up")
            await pilot.pause()
            await pilot.app.animator.wait_until_complete()
            await _settle(panel, pilot)
            assert panel.scroll_y == panel.scroll_target_y == expected
            assert panel._history_scroll.pending is None
        finally:
            gate.release.set()


async def test_regrabbing_and_resizing_keeps_the_drag_target(monkeypatch: pytest.MonkeyPatch) -> None:
    async with ChatPanelApp().run_test(size=_SIZE) as pilot:
        cp = pilot.app.query_one(ChatPanel)
        await cp.replay_history(_turns(120))
        await _settle(cp, pilot)
        monkeypatch.setattr(cp._history_scroll, "request_widgets", lambda *_: None)
        bar = cp.vertical_scrollbar
        bar.post_message(ScrollTo(y=cp.max_scroll_y / 2, animate=False))
        await wait_for(
            lambda: cp._history_scroll.pending is not None, pilot=pilot, description="a cold page is requested"
        )
        request = cp._history_scroll.pending
        assert request is not None
        grab = Offset(bar.region.x, bar.region.y + round(bar.window_size / 2))
        bar._on_mouse_capture(events.MouseCapture(grab))
        try:
            await pilot.pause()
            assert cp._history_scroll.pending == request
            assert bar.grabbed_position == bar.position == request.row

            pointer_y = grab.y + 2
            await bar._on_mouse_move(
                events.MouseMove(bar, 0, 0, 0, 0, 1, False, False, False, screen_x=grab.x, screen_y=pointer_y)
            )
            await wait_for(
                lambda: cp._history_scroll.pending is not None and cp._history_scroll.pending != request,
                pilot=pilot,
                description="the drag moves from the regrabbed row",
            )
            dragged = cp._history_scroll.pending
            assert dragged is not None
            window_before = bar.window_size
            await pilot.resize_terminal(_SIZE[0], _SIZE[1] + 8)
            await wait_for(lambda: bar.window_size != window_before, pilot=pilot, description="the track resized")
            scale = bar.window_virtual_size / bar.window_size
            assert bar.grabbed_position + (pointer_y - grab.y) * scale == pytest.approx(dragged.row, abs=1)
            assert bar.position == pytest.approx(dragged.row, abs=1)
        finally:
            bar._on_mouse_release(events.MouseRelease(grab))


async def test_cold_layout_does_not_publish_removed_live_widgets(monkeypatch: pytest.MonkeyPatch) -> None:
    async with ChatPanelApp().run_test(size=_SIZE) as pilot:
        cp = pilot.app.query_one(ChatPanel)
        await cp.replay_history(_turns(120))
        await cp.add_agent_message("a live answer that will be withdrawn")
        await _settle(cp, pilot)
        live = cp.query(AgentMessage).last()
        assert cp._history_scroll.visible_layout is not None and live in cp._history_scroll.visible_layout.widgets
        monkeypatch.setattr(cp._history_scroll, "request_widgets", lambda *_: None)
        cp.vertical_scrollbar.post_message(ScrollTo(y=cp.max_scroll_y / 2, animate=False))
        await wait_for(
            lambda: cp._history_scroll.pending is not None,
            pilot=pilot,
            description="the cold page is waiting for its mount",
        )
        await cp.remove_transcript_widget(live)
        await pilot.pause()

        assert cp._history_scroll.pending is not None
        assert live not in cp.screen._compositor.widgets


async def test_thumb_overshoot_stays_at_the_top_when_cold_rows_shrink() -> None:
    async with ChatPanelApp().run_test(size=_SIZE) as pilot:
        cp = pilot.app.query_one(ChatPanel)
        await cp.replay_history(_turns_with_overestimated_links())
        await _settle(cp, pilot)
        assert cp._replay_mount.windowed
        viewport = cp._history_viewport
        first_turn = next(widget for widget in cp.pending_transcript_widgets() if widget.id == "turn-1")
        assert viewport is not None
        first_turn_row = viewport.page_row(first_turn)
        before = cp.virtual_size.height
        bar = cp.vertical_scrollbar
        grab = Offset(bar.region.x, bar.region.bottom - 2)
        bar.grabbed = grab
        bar.grabbed_position = bar.position
        cp.release_anchor()
        edge = grab.y - math.ceil(bar.position * bar.window_size / bar.window_virtual_size)

        async def move_pointer(y: int) -> None:
            await bar._on_mouse_move(
                events.MouseMove(bar, 0, 0, 0, 0, 1, False, False, False, screen_x=grab.x, screen_y=y)
            )

        try:
            await move_pointer(edge - 5)
            await wait_for(
                lambda: cp.scroll_y == 0 and cp.virtual_size.height < before,
                pilot=pilot,
                description="the cold first page measured at the upper edge",
            )
            assert first_turn.is_attached
            assert viewport.page_row(first_turn) == first_turn_row
            await move_pointer(edge - 4)
            await _settle(cp, pilot)
            assert cp.scroll_y == 0
        finally:
            bar.grabbed = None


async def test_replay_viewport_rebases_thumb_during_cold_width_drag() -> None:
    async with ChatPanelApp().run_test(size=(45, 24)) as pilot:
        cp = pilot.app.query_one(ChatPanel)
        await cp.replay_history(_history_with_wrapped_pages(36))
        await _wait_complete(cp)
        await _settle(cp, pilot)
        cp.refresh(layout=True)
        await _settle(cp, pilot)
        await pilot.resize_terminal(140, 24)
        await _settle(cp, pilot)
        assert cp._history_window is not None
        estimated_extent = cp.max_scroll_y

        bar = cp.vertical_scrollbar
        grab = Offset(bar.region.x, bar.region.bottom - 2)
        bar.grabbed = grab
        bar.grabbed_position = bar.position
        cp.release_anchor()
        previous = cp.scroll_y
        target_y = grab.y - 11

        def move_pointer(x: int) -> events.MouseMove:
            return events.MouseMove(bar, 0, 0, 0, 0, 1, False, False, False, screen_x=x, screen_y=target_y)

        await bar._on_mouse_move(move_pointer(grab.x))
        await wait_for(lambda: abs(cp.scroll_y - previous) >= 2, pilot=pilot, description="the first drag moved")
        await _settle(cp, pilot)
        landed = cp.scroll_y
        assert cp.max_scroll_y != estimated_extent
        assert 0 < landed < cp.max_scroll_y
        await bar._on_mouse_move(move_pointer(grab.x + 1))
        await pilot.pause()
        await _settle(cp, pilot)
        assert round(cp.scroll_y) == round(landed)
        bar.grabbed = None


@pytest.mark.parametrize("turn", [49, 120], ids=["archive", "tail"])
async def test_guide_fold_lays_out_the_destination_before_moving_the_view(
    monkeypatch: pytest.MonkeyPatch, turn: int
) -> None:
    messages = _turns(120, with_tools=True)
    call = next(
        message
        for message in messages
        if message["role"] == "assistant" and message["contents"][0].get("call_id") == f"call-{turn - 1}"
    )
    call["contents"].insert(
        0, Content.from_text_reasoning(text="\n\n".join(f"Long process line {i}" for i in range(100))).to_dict()
    )
    async with ChatPanelApp(thinking_expanded=lambda: True).run_test(size=(100, 30)) as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await wait_for(lambda: bool(panel.size), pilot=pilot)
        await panel.replay_history(messages)
        await wait_for(
            lambda: not panel._history_scroll.restoring_tail and panel.scroll_y == panel.max_scroll_y, pilot=pilot
        )
        # Open the archive from its last turn so the preceding page stays cold.
        panel.scroll_to_turn(f"turn-{turn + 3 if turn == 49 else turn}")
        await wait_for(
            lambda: (
                bool(panel.query(ThinkingMessage))
                and panel.query_one(ThinkingMessage).is_attached
                and panel._history_scroll.pending is None
                and screen_is_settled(pilot.app, pilot.app.screen)
            ),
            pilot=pilot,
        )
        thinking = panel.query_one(ThinkingMessage)
        block = panel._process_controller.owner_of(thinking)
        assert isinstance(block, ProcessBlock) and block.collapsed
        panel._toggle_process_block(block)
        thinking.collapsed = False
        await wait_for(
            lambda: len(thinking.query(VirtualizedMarkdown)) > 0 and thinking.virtual_region.height > 35, pilot=pilot
        )
        await _settle(panel, pilot)
        panel.release_anchor()
        row = thinking.virtual_region.y + thinking.virtual_region.height - 22
        panel.scroll_to(y=row, animate=False, immediate=True)
        await _settle(panel, pilot)
        assert thinking.region.y < panel.content_region.y < thinking.region.bottom
        screen_y = panel.content_region.y + 16
        assert screen_y < thinking.region.bottom
        uncovered: list[tuple[float, tuple[int, int], tuple[int, int]]] = []
        real = panel.arrange

        def observe(size: Size, optimal: bool = False) -> DockArrangeResult:
            result = real(size, optimal=optimal)
            viewport = panel._history_viewport
            window = panel._history_window
            if not optimal and viewport is not None and window is not None and panel._history_scroll.pending is None:
                required = viewport.window(panel.scroll_y, size.height, following=False, overscan=0)
                if not (window[0] <= required[0] <= required[1] <= window[1]):
                    uncovered.append((panel.scroll_y, window, required))
            return result

        monkeypatch.setattr(panel, "arrange", observe)
        gate = _PrependGate(panel, monkeypatch) if turn == 49 else None
        if gate is not None:
            assert not panel.query("#turn-48")
        try:
            panel.post_message(GuideFoldRequested(thinking, process=True, screen_y=screen_y))
            await wait_for(lambda: block.collapsed, pilot=pilot)
            if gate is not None:
                await gate.wait_held(pilot)
                assert panel._history_scroll.pending is not None
                # The folded header and the next answer paint natively
                # while the cold page needed for the exact pointer row mounts.
                assert block.region.overlaps(panel.content_region)
                region = panel.content_region
                strips = pilot.app.screen._compositor.render_strips()
                chat = "\n".join(strip.crop(region.x, region.right).text for strip in strips[region.y : region.bottom])
                assert "answer 48" in chat
        finally:
            if gate is not None:
                gate.release.set()

        await wait_for(lambda: panel._history_scroll.pending is None, pilot=pilot)
        await _settle(panel, pilot)
        assert not uncovered
        assert block.region.overlaps(panel.content_region)
        if turn == 49:
            assert block.region.y == screen_y


@pytest.mark.parametrize("target_kind", ["tool-group", "process", "process-then-page-down"])
async def test_guide_fold_places_a_shrinking_tool_group_at_the_pointer(target_kind: str) -> None:
    messages = _turns(120, with_tools=True)
    call_index = next(
        index
        for index, message in enumerate(messages)
        if message["role"] == "assistant" and message["contents"][0].get("call_id") == "call-51"
    )
    calls = messages[call_index]["contents"]
    results = messages[call_index + 1]["contents"]
    for index in range(34):
        call_id = f"additional-call-{index}"
        calls.append(
            {
                "type": "function_call",
                "name": "read_file",
                "call_id": call_id,
                "arguments": {"path": f"file_{index}.py"},
            }
        )
        results.append({"type": "function_result", "call_id": call_id, "result": "ok"})

    async with ChatPanelApp().run_test(size=(100, 30)) as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.replay_history(messages)
        await _settle(panel, pilot)
        panel.scroll_to_turn("turn-52")

        def long_groups() -> list[ToolGroup]:
            return [group for group in panel.query(ToolGroup) if len(group._tool_records) == 35]

        await wait_for(lambda: long_groups() and screen_is_settled(pilot.app, pilot.app.screen), pilot=pilot)
        group = long_groups()[0]
        block = panel._process_controller.owner_of(group)
        assert isinstance(block, ProcessBlock) and block.collapsed
        panel._toggle_process_block(block)
        group.collapsed = False
        await wait_for(lambda: group.virtual_region.height > 45, pilot=pilot)
        panel.release_anchor()
        panel.scroll_to(y=group.virtual_region.y + 15, animate=False, immediate=True)
        await _settle(panel, pilot)

        whole_process = target_kind != "tool-group"
        target = block if whole_process else group
        point = panel.content_region.y + (5 if whole_process else 16)
        assert target.region.y < panel.content_region.y and point < group.region.bottom
        request = GuideFoldRequested(group, process=whole_process, screen_y=point)
        if target_kind == "process-then-page-down":
            with pilot.app.batch_update():
                panel.on_guide_fold_requested(request)
                fold = panel._scroll_controller._guide_fold
                assert fold is not None and not fold.placing
                panel.user_scroll_page(1)
                assert panel._scroll_controller._guide_fold is None
            await wait_for(lambda: target.collapsed, pilot=pilot)
            return
        panel.post_message(request)
        await wait_for(lambda: target.collapsed and panel._history_scroll.pending is None, pilot=pilot)
        await _settle(panel, pilot)
        assert target.region.y + target.styles.gutter.top + int(target.gap_above) == point
