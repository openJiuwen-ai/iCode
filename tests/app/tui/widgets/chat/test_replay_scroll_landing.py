# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Scroll input and animations while cold replay publishes a new layout."""

from __future__ import annotations

import asyncio

import pytest
from textual import events
from textual._animator import SimpleAnimation
from textual.scrollbar import ScrollDown, ScrollTo
from textual.widget import Widget

from chrys.app.tui.widgets.chat.history_scroll import HistoryScrollRequest
from chrys.app.tui.widgets.chat.panel import ChatPanel
from tests.app.tui.widgets.chat.quote_popup_fixtures import _MainScreenApp, _open_history_turn
from tests.app.tui.widgets.chat.replay_fixtures import _settle, _turns
from tests.support.tui_helpers import ChatPanelApp
from tests.support.waiting import wait_for

pytestmark = pytest.mark.usefixtures("frozen_earlier_heap")


@pytest.mark.parametrize("turn", [1, 118], ids=["cold", "mounted-tail"])
async def test_toc_jump_before_the_first_indexed_layout(turn: int) -> None:
    messages = _turns(120)
    for index in range(120):
        messages[index * 3 + 1]["contents"] = [
            {"type": "text", "text": "First paragraph.\n\nSecond paragraph.\n\nThird paragraph."}
        ]

    app = _MainScreenApp()
    async with app.run_test(size=(120, 30)) as pilot:
        panel = app.main_screen.query_one(ChatPanel)
        await wait_for(lambda: bool(panel.size), pilot=pilot)
        await panel.replay_history(messages)
        scroll = panel._history_scroll
        viewport = panel._history_viewport
        assert scroll.restoring_tail and scroll.visible_layout is None and viewport is not None
        target = viewport.turn(f"turn-{turn}")
        assert target is not None and target.is_attached == (turn == 118)

        panel.scroll_to_turn(f"turn-{turn}")
        await wait_for(
            lambda: (
                target.is_attached
                and target.region.overlaps(panel.content_region)
                and target.has_class("-highlighted")
                and not scroll.restoring_tail
                and not scroll.has_request
                and panel._history_jump is None
            ),
            pilot=pilot,
            description="the requested turn is highlighted in the viewport",
        )
        assert not panel.query("#turn-60")


@pytest.mark.parametrize("gesture", ["page-up", "shift-wheel", "ctrl-wheel"])
async def test_later_input_supersedes_a_toc_jump_queued_before_the_first_layout(gesture: str) -> None:
    async with ChatPanelApp().run_test(size=(80, 24)) as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.replay_history(_turns(120))
        scroll = panel._history_scroll
        assert scroll.restoring_tail and scroll.visible_layout is None

        panel.scroll_to_turn("turn-1")
        if gesture == "page-up":
            panel.action_page_up()
            distance = panel.scrollable_content_region.height
        else:
            panel._on_mouse_scroll_down(
                events.MouseScrollDown(
                    panel,
                    x=1,
                    y=1,
                    delta_x=0,
                    delta_y=0,
                    button=0,
                    shift=gesture == "shift-wheel",
                    meta=False,
                    ctrl=gesture == "ctrl-wheel",
                )
            )
            distance = 0
        await wait_for(
            lambda: (
                not scroll.restoring_tail
                and scroll.startup is None
                and scroll.relative is None
                and not scroll.has_request
                and panel.scroll_y == panel.max_scroll_y - distance
            ),
            pilot=pilot,
            description="the later gesture supersedes the queued jump",
        )
        assert not panel.query("#turn-1")


async def test_startup_toc_jump_survives_fallback_to_native_layout() -> None:
    async with ChatPanelApp().run_test(size=(80, 24)) as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.replay_history(_turns(120))
        scroll = panel._history_scroll
        viewport = panel._history_viewport
        assert scroll.restoring_tail and scroll.visible_layout is None and viewport is not None
        target = viewport.turn("turn-1")
        assert target is not None and not target.is_attached

        with panel.app.batch_update():
            panel.scroll_to_turn("turn-1")
            # An archive edit invalidates the index before its first layout.
            await panel.query_one("#turn-118").remove()
        await wait_for(
            lambda: (
                panel._history_viewport is None
                and scroll.startup is None
                and target.region.overlaps(panel.content_region)
                and target.has_class("-highlighted")
            ),
            pilot=pilot,
            description="the queued TOC jump lands after the native layout",
        )


@pytest.mark.parametrize(
    ("heights", "overlap"), [((20,), False), ((120,), True), ((20, 70), False)], ids=["disjoint", "overlap", "regrow"]
)
async def test_narrowing_a_cold_window_only_fails_if_the_batch_overlaps(
    monkeypatch: pytest.MonkeyPatch, heights: tuple[int, ...], overlap: bool
) -> None:
    async with ChatPanelApp().run_test(size=(85, 200)) as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.replay_history(_turns(200))
        await _settle(panel, pilot)
        scroll = panel._history_scroll
        viewport = panel._history_viewport
        assert viewport is not None
        target = viewport.turn("turn-21")
        assert target is not None and not target.is_attached
        mount = panel.mount_replay_batch
        entered, release = asyncio.Event(), asyncio.Event()
        in_flight: set[Widget] = set()

        async def fail_first_batch(batch: list[Widget], *, before: Widget | None) -> None:
            if not entered.is_set():
                in_flight.update(batch)
                entered.set()
                await release.wait()
                raise RuntimeError("test failed the old viewport's active batch")
            await mount(batch, before=before)

        monkeypatch.setattr(panel, "mount_replay_batch", fail_first_batch)
        panel.scroll_to_turn("turn-21")
        try:
            await wait_for(entered.is_set, pilot=pilot, description="the wide viewport started mounting")
            initial = scroll.pending
            assert initial is not None
            previous_window = initial.window
            for height in heights:
                await pilot.resize_terminal(85, height)
                await wait_for(
                    lambda window=previous_window: scroll.pending is not None and scroll.pending.window != window,
                    pilot=pilot,
                    description="the cold request follows the viewport resize",
                )
                current = scroll.pending
                assert current is not None and scroll.mount_request is current
                assert initial.window[0] <= current.window[0] < current.window[1] <= initial.window[1]
                previous_window = current.window
            assert (not in_flight.isdisjoint(viewport.widgets_in(previous_window))) == overlap
        finally:
            release.set()
        await wait_for(lambda: not panel._replay_mount.in_progress, pilot=pilot)
        await _settle(panel, pilot)
        assert scroll.mount_failed == overlap
        assert target.region.overlaps(panel.content_region) == (not overlap)
        assert target.has_class("-highlighted") == (not overlap)


@pytest.mark.parametrize("when", ["mounting", "ready"])
@pytest.mark.parametrize("action", ["none", "wheel", "down", "up", "page-down"])
async def test_scroll_from_a_thumb_destination_beyond_the_old_bottom(
    monkeypatch: pytest.MonkeyPatch, when: str, action: str
) -> None:
    messages = _turns(88)
    for index in range(87, -1, -1):
        reply = f"answer {index}" if not 60 <= index < 64 else f"[doc](https://example.com/{'x' * 3000})"
        messages[index * 3 + 1]["contents"] = [{"type": "text", "text": reply}]
        if 56 <= index < 60:
            tools: list[dict[str, object]] = []
            for step in range(16):
                call = f"call-{index}-{step}"
                tools.extend(
                    (
                        {
                            "role": "assistant",
                            "contents": [
                                {
                                    "type": "function_call",
                                    "name": "read_file",
                                    "call_id": call,
                                    "arguments": {"path": f"/tmp/file-{index}-{step}.py"},
                                }
                            ],
                        },
                        {
                            "role": "tool",
                            "contents": [{"type": "function_result", "call_id": call, "result": "line\n" * 6}],
                        },
                    )
                )
            messages[index * 3 + 1 : index * 3 + 1] = tools

    async with ChatPanelApp().run_test(size=(80, 40)) as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.replay_history(messages)
        await _settle(panel, pilot)
        assert panel.toggle_fold_all() is False
        await _settle(panel, pilot)
        viewport = panel._history_viewport
        assert viewport is not None
        target = viewport.turn("turn-61")
        assert target is not None
        old_row = viewport.page_row(target)
        assert old_row is not None and not target.is_attached
        old_bottom = panel.max_scroll_y

        scroll = panel._history_scroll
        request_widgets = scroll.request_widgets
        land = scroll._ready
        held: tuple[HistoryScrollRequest, list[Widget]] | None = None
        requested, landed = asyncio.Event(), asyncio.Event()
        calls = 0

        def hold_second_request(request: HistoryScrollRequest, widgets: list[Widget]) -> None:
            nonlocal calls, held
            calls += 1
            if calls == 2:
                held = request, widgets
                requested.set()
            else:
                request_widgets(request, widgets)

        def track_landing(request: HistoryScrollRequest) -> None:
            land(request)
            if held is not None and request is held[0]:
                landed.set()

        def apply_input(origin: float) -> float:
            if action == "wheel":
                panel._on_mouse_scroll_down(
                    events.MouseScrollDown(
                        panel, x=1, y=1, delta_x=0, delta_y=0, button=0, shift=False, meta=False, ctrl=False
                    )
                )
                return round(origin) + pilot.app.scroll_sensitivity_y
            if action == "down":
                panel.action_scroll_down()
                return round(origin) + 1
            if action == "up":
                panel.action_scroll_up()
                return round(origin) - 1
            if action == "page-down":
                panel.user_scroll_page(1)
                return origin + panel.scrollable_content_region.height
            return origin

        monkeypatch.setattr(scroll, "request_widgets", hold_second_request)
        monkeypatch.setattr(scroll, "_ready", track_landing)
        panel.vertical_scrollbar.post_message(ScrollTo(y=old_row - 0.6, animate=False))
        await wait_for(requested.is_set, pilot=pilot, description="the thumb reached a second cold window")
        pending = scroll.pending
        assert held is not None and pending is not None
        assert pending.row > panel.max_scroll_y == old_bottom
        assert pending.row != round(pending.row)
        monkeypatch.setattr(scroll, "request_widgets", request_widgets)

        if when == "ready":
            with panel.app.batch_update():
                request_widgets(*held)
                await wait_for(landed.is_set, description="the cold window is ready before the next layout")
                assert scroll.pending is None and viewport.last_layout is not scroll.visible_layout
                assert panel.scroll_y == pending.row
                expected = apply_input(panel.scroll_y)
        else:
            expected = apply_input(pending.row)
            if scroll.mount_request is held[0]:
                request_widgets(*held)
        await wait_for(
            lambda: target.is_attached and not panel.history_layout_is_held() and scroll.relative is None,
            pilot=pilot,
            description="the thumb destination has been measured",
        )
        await _settle(panel, pilot)
        assert panel.scroll_y == pytest.approx(expected)
        if action != "page-down":
            assert target.region.overlaps(panel.content_region)
        assert panel.is_anchor_released()


async def test_resizing_during_track_animation_keeps_the_painted_row(monkeypatch: pytest.MonkeyPatch) -> None:
    app = _MainScreenApp()
    async with app.run_test(size=(100, 28)) as pilot:
        panel = app.main_screen.query_one(ChatPanel)
        await wait_for(lambda: bool(panel.size), pilot=pilot)
        await panel.replay_history(_turns(120, with_tools=True))
        await _settle(panel, pilot)
        await _open_history_turn(pilot, panel, 60)
        scroll = panel._history_scroll
        painted = scroll.visible_row
        mounted = panel.mount_replay_batch
        entered, release = asyncio.Event(), asyncio.Event()

        async def gate_batch(batch: list[Widget], *, before: Widget | None) -> None:
            entered.set()
            await release.wait()
            await mounted(batch, before=before)

        monkeypatch.setattr(panel, "mount_replay_batch", gate_batch)
        animation_count = app._realtime_animation_count
        panel._on_scroll_down(ScrollDown())
        animator = app.animator
        animation = animator._animations[(id(panel), "scroll_y")]
        assert isinstance(animation, SimpleAnimation)
        # Keep the track animation on its first frame until resize interrupts it.
        animation.duration = 60.0
        animation.easing = lambda _: 0.0
        destination = panel.scroll_target_y
        assert destination > painted
        try:
            await pilot.resize_terminal(100, 90)
            await wait_for(entered.is_set, pilot=pilot, description="resize needs cold history")
            assert panel.history_layout_is_held()
            assert not animator.is_being_animated(panel, "scroll_y")
            assert panel.scroll_y == scroll.visible_row == painted
            assert panel.scroll_target_y == painted
        finally:
            release.set()
        await _settle(panel, pilot)
        assert panel.scroll_y == destination
        assert panel.is_anchor_released()
        assert app._realtime_animation_count == animation_count
