# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for the GC-freeze coordinator running inside the real app lifecycle."""

from __future__ import annotations

import asyncio
import gc
import weakref
from pathlib import Path

import pytest
from textual.geometry import Size
from textual.pilot import Pilot
from textual.screen import Screen
from textual.widget import Widget

from chrys.app.tui import app as chrys_app
from chrys.app.tui.app import ChrysApp
from chrys.app.tui.widgets.chat.panel import ChatPanel
from chrys.app.tui.widgets.chat.tool_call import ToolGroup
from chrys.foundation.config.settings import Settings
from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import Warning
from chrys.foundation.tool_kinds import KIND_SHELL
from chrys.service.state.store import JsonFileStateStore
from tests.support.paths import SRC_ROOT
from tests.support.pilot_barrier import screen_is_settled
from tests.support.tui_app_harness import EmptyAgentRegistry, ShutdownOnlyEngine, make_chrys_app
from tests.support.waiting import wait_for, wait_until, wait_until_quiet

_CHRYS_CSS = SRC_ROOT / "chrys" / "app" / "tui" / "chrys.tcss"


async def test_gc_freeze_opt_out_is_inert(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A disabled coordinator must suppress ownership, timer, and GC mutation."""

    monkeypatch.setattr(chrys_app, "GC_FREEZE_ENABLED", False)
    app = make_chrys_app(tmp_path, settings=Settings.from_env(), gc_freeze_enabled=None)

    async with app.run_test() as pilot:
        await pilot.pause()
        assert app._gc_freeze.enabled is False
        assert app._gc_freeze.started is False
        assert app._gc_freeze_watchdog is None
        assert gc.get_freeze_count() == 0


async def test_enabled_gc_freeze_real_app_lifecycle_returns_two_runs_to_baseline(tmp_path: Path) -> None:
    """Explicit test activation must freeze after mount and fully unwind on every unmount."""
    from chrys.app.tui.support import gc_freeze

    for run_number in range(2):
        app = make_chrys_app(tmp_path / str(run_number), gc_freeze_enabled=True)

        async with app.run_test() as pilot:
            await wait_for(
                lambda run_app=app: run_app._gc_freeze.frozen,
                pilot=pilot,
                description="app gc-freeze coordinator freezes after mount",
            )
            assert app._gc_freeze.started is True
            assert app._gc_freeze.frozen is True
            assert app._gc_freeze_watchdog is not None
            assert app._main_screen is not None
            assert len(app._main_screen._gc_freeze_participants) == 6
            assert gc.get_freeze_count() > 0

        assert gc.get_freeze_count() == 0
        assert gc_freeze._enabled_owner is None


async def test_gc_freeze_waits_for_backend_turn_finalization_task(tmp_path: Path) -> None:
    """A terminal UI event cannot freeze session-save or after-turn-hook state."""
    from chrys.app.tui.support.gc_freeze import GcAbsorbReason, GcFreezeBlockReason
    from chrys.orchestration.engine.assembly import assemble_agent_engine

    bus = EventBus()
    engine = assemble_agent_engine(bus, settings=Settings(), state_store=JsonFileStateStore(tmp_path / "engine"))
    app = make_chrys_app(tmp_path / "app", engine=engine, event_bus=bus, gc_freeze_enabled=True)

    async with app.run_test() as pilot:
        await wait_for(
            lambda: app._gc_freeze.frozen,
            pilot=pilot,
            description="app gc-freeze coordinator freezes after mount",
        )
        assert app._gc_freeze.frozen is True
        assert app._gc_freeze_watchdog is not None
        app._gc_freeze_watchdog.pause()

        release_finalization = asyncio.Event()
        run_task = asyncio.create_task(release_finalization.wait())
        engine.turns.turn_state.lease.run_task = run_task
        try:
            # This is the exact interval missed by the UI/FSM gates: the FSM
            # is idle, while the owned task is still saving/draining hooks.
            assert engine.is_turn_active is False
            assert engine.is_turn_lifecycle_active is True
            assert app.freeze_block_reason() is GcFreezeBlockReason.BACKEND_TURN_LIFECYCLE

            previous_metrics = app._gc_freeze.last_action_metrics
            app._gc_freeze.request_absorb(reason=GcAbsorbReason.TURN_TERMINAL, terminal_boundary=True)
            # This reason has not been logged yet, so log throttling cannot hide this first deferral.
            assert app._gc_freeze._last_deferral_reason is None
            await wait_for(
                lambda: app._gc_freeze._last_deferral_reason is GcFreezeBlockReason.BACKEND_TURN_LIFECYCLE,
                pilot=pilot,
                description="scheduled absorb observes the active backend lifecycle",
            )

            assert app._gc_freeze._absorb_pending is True
            assert app._gc_freeze.last_action_metrics is previous_metrics

            release_finalization.set()
            await run_task
            assert engine.is_turn_lifecycle_active is False
            app._gc_freeze.on_tick()
            await wait_for(
                lambda: app._gc_freeze.last_action_metrics is not previous_metrics,
                pilot=pilot,
                description="GC action completes",
            )

            assert app._gc_freeze._absorb_pending is False
            assert app._gc_freeze.last_action_metrics is not previous_metrics
            assert app._gc_freeze.last_action_metrics is not None
            assert app._gc_freeze.last_action_metrics.action == "absorb"
        finally:
            release_finalization.set()
            await run_task


async def test_gc_freeze_prepare_failure_restores_raising_participant_before_retry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A participant that detaches then raises must be normalized before retry."""
    from chrys.app.tui.support.gc_freeze import DetachedLruCache, GcAbsorbReason
    from chrys.app.tui.widgets.chat.session_json import SessionJsonPanel

    app = make_chrys_app(tmp_path, gc_freeze_enabled=True)

    async with app.run_test() as pilot:
        await wait_for(
            lambda: app._gc_freeze.frozen,
            pilot=pilot,
            description="app gc-freeze coordinator freezes after mount",
        )
        assert app._main_screen is not None
        session_json = app._main_screen.query_one(SessionJsonPanel)
        original_prepare = session_json.prepare_for_gc_freeze
        failed = False

        def _fail_once_between_participants() -> None:
            nonlocal failed
            original_prepare()
            assert isinstance(session_json._strip_cache, DetachedLruCache)
            if not failed:
                failed = True
                raise RuntimeError("injected participant prepare failure")

        monkeypatch.setattr(session_json, "prepare_for_gc_freeze", _fail_once_between_participants)
        previous_metrics = app._gc_freeze.last_action_metrics
        app._gc_freeze.request_absorb(reason=GcAbsorbReason.TURN_TERMINAL, terminal_boundary=True)
        await wait_for(
            lambda: app._gc_freeze._prepare_failures == 1,
            pilot=pilot,
            description="GC preparation failure is recorded",
        )

        assert app._gc_freeze._prepare_failures == 1
        assert not isinstance(session_json._strip_cache, DetachedLruCache)
        session_json._strip_cache.clear()

        app._gc_freeze._prepare_retry_not_before = float("-inf")
        app._gc_freeze.on_tick()
        await wait_for(
            lambda: app._gc_freeze.last_action_metrics is not previous_metrics,
            pilot=pilot,
            description="GC action completes",
        )

        assert app._gc_freeze.last_action_metrics is not previous_metrics
        assert app._gc_freeze.last_action_metrics is not None
        assert app._gc_freeze.last_action_metrics.action == "absorb"
        assert app._gc_freeze._faulted is False


async def test_gc_freeze_after_failure_normalizes_mid_screen_renewal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """With cyclic Textual caches, a mid-screen renewal failure must normalize all caches before fail-open returns."""
    from chrys.app.tui.support import gc_freeze
    from chrys.app.tui.support.gc_freeze import DetachedFifoCache, DetachedLruCache, GcAbsorbReason

    monkeypatch.setattr(gc_freeze, "textual_screen_caches_acyclic", lambda: False)
    app = make_chrys_app(tmp_path, gc_freeze_enabled=True)

    async with app.run_test() as pilot:
        await wait_for(
            lambda: app._gc_freeze.frozen,
            pilot=pilot,
            description="app gc-freeze coordinator freezes after mount",
        )
        assert app._main_screen is not None
        original_renew = gc_freeze.renew_lru_cache
        renew_calls = 0

        def _fail_second_screen_renewal(cache):
            nonlocal renew_calls
            renew_calls += 1
            if renew_calls == 2:
                raise RuntimeError("injected screen renewal failure")
            return original_renew(cache)

        monkeypatch.setattr(gc_freeze, "renew_lru_cache", _fail_second_screen_renewal)
        app._gc_freeze.request_absorb(reason=GcAbsorbReason.TURN_TERMINAL, terminal_boundary=True)
        await wait_for(
            lambda: app._gc_freeze._faulted,
            pilot=pilot,
            description="GC coordinator enters fault state",
        )

        assert renew_calls > 2
        assert app._gc_freeze._faulted is True
        assert app._gc_freeze.frozen is False
        assert gc.get_freeze_count() == 0
        for widget in app._main_screen.walk_children(with_self=True):
            assert not isinstance(widget._box_model_cache, DetachedLruCache)
            assert not isinstance(widget._query_one_cache, DetachedLruCache)
            assert not isinstance(widget._arrangement_cache, DetachedFifoCache)
            widget._box_model_cache.clear()
            widget._query_one_cache.clear()
            widget._arrangement_cache.clear()
        await pilot.pause()


async def _add_tool_turn(panel: ChatPanel, call_id: str) -> tuple[ToolGroup, weakref.ReferenceType[Widget]]:
    """Add one completed shell-tool turn; return its group and a weakref to the pruned tool card."""
    await panel.add_user_message(f"Run the {call_id} check\nand explain the output")
    await panel.add_tool_start(call_id, "shell", KIND_SHELL, args={"command": f"echo {call_id}"})
    await panel.add_tool_result(call_id, "shell", "\n".join(f"{call_id} line {line}" for line in range(20)), 40)
    group = panel._tool_groups_by_call_id[call_id]
    tool = _tool_widget_ref(group, call_id)
    await panel.add_agent_message(
        f"## {call_id}\n\nThe check passed; `echo` printed twenty lines.\n\n- first point\n- second point"
    )
    return group, tool


def _tool_widget_ref(group: ToolGroup, call_id: str) -> weakref.ReferenceType[Widget]:
    """Build a weakref without retaining the widget in an async test frame."""
    tool = group.get_tool(call_id)
    if tool is None:
        raise AssertionError(f"{call_id} has no mounted tool widget")
    return weakref.ref(tool)


def _all_unreachable(references: list[weakref.ReferenceType[Widget]]) -> bool:
    """Collect young garbage, then report whether every referenced widget is gone."""
    gc.collect()
    return all(reference() is None for reference in references)


async def _settle(app: ChrysApp, screen: Screen, pilot: Pilot[None]) -> None:
    await wait_for(lambda: screen_is_settled(app, screen), pilot=pilot, description="MainScreen settles")
    await wait_until_quiet(lambda: screen_is_settled(app, screen), pilot=pilot, description="MainScreen stays settled")
    assert screen_is_settled(app, screen)


async def _run_turn_end_absorb(app: ChrysApp, screen: Screen, pilot: Pilot[None]) -> str:
    """Post the turn-terminal absorb request and settle every layout it scheduled."""
    from chrys.app.tui.support.gc_freeze import GcAbsorbReason, GcAbsorbRequested

    previous_metrics = app._gc_freeze.last_action_metrics
    screen.post_message(GcAbsorbRequested(GcAbsorbReason.TURN_TERMINAL, terminal_boundary=True))
    await wait_for(
        lambda: app._gc_freeze.last_action_metrics is not previous_metrics,
        pilot=pilot,
        description="turn-end GC action completes",
    )
    metrics = app._gc_freeze.last_action_metrics
    assert metrics is not None
    await _settle(app, screen, pilot)
    return metrics.action


async def test_populated_turn_end_absorb_keeps_layout_and_needs_no_reclaim_for_a_young_tool_prune(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A turn-end absorb over a populated transcript neither reflows MainScreen nor queues a full reclaim.

    Textual's acyclic caches stay installed across the action, so nothing forces a layout, and a
    tool turn built and pruned since the last freeze leaves no frozen garbage to reclaim. The
    turns are built behind a freeze gate, as a live backend turn holds one. Forcing the
    cyclic-cache fallback afterwards proves the layout spy.
    """
    from chrys.app.tui.support import gc_freeze
    from chrys.app.tui.support.gc_freeze import GcFreezeBlockReason, GcReclaimReason

    app = make_chrys_app(tmp_path, gc_freeze_enabled=True)
    async with app.run_test(size=(120, 36)) as pilot:
        await wait_for(lambda: app._gc_freeze.frozen, pilot=pilot, description="initial GC freeze")
        main_screen = app._main_screen
        assert main_screen is not None
        panel = main_screen.query_one(ChatPanel)

        with monkeypatch.context() as turn_gate:
            turn_gate.setattr(main_screen, "gc_freeze_block_reason", lambda: GcFreezeBlockReason.AGENT_RUNNING)
            history_tools = [(await _add_tool_turn(panel, f"history-{index}"))[1] for index in range(12)]
            await wait_for(
                lambda: _all_unreachable(history_tools),
                pilot=pilot,
                description="pruned history tool cards become unreachable",
            )
        previous_metrics = app._gc_freeze.last_action_metrics
        app._gc_freeze.request_reclaim(reason=GcReclaimReason.SESSION_READY, prompt=True)
        await wait_for(
            lambda: app._gc_freeze.last_action_metrics is not previous_metrics,
            pilot=pilot,
            description="populated transcript is frozen",
        )
        assert app._gc_freeze.last_action_metrics is not None
        assert app._gc_freeze.last_action_metrics.action == "full"
        assert app._gc_freeze._idle_reclaim_pending is False
        assert len(panel.walk_children()) > 100

        layout_refreshes: list[None] = []
        refresh_layout = main_screen._refresh_layout

        def _record_layout(size: Size | None = None, scroll: bool = False) -> None:
            layout_refreshes.append(None)
            refresh_layout(size, scroll)

        with monkeypatch.context() as turn_gate:
            turn_gate.setattr(main_screen, "gc_freeze_block_reason", lambda: GcFreezeBlockReason.AGENT_RUNNING)
            live_group, live_tool = await _add_tool_turn(panel, "live")
            await wait_for(lambda: not live_group._content_mounted, pilot=pilot, description="live tool turn pruned")
            # The card's cancelled spinner interval keeps it reachable from the event loop
            # until its deadline; a real turn spends far longer on the final answer.
            await wait_for(
                lambda: _all_unreachable([live_tool]),
                pilot=pilot,
                description="pruned live tool card becomes unreachable",
            )
            await _settle(app, main_screen, pilot)
            assert app._gc_freeze._idle_reclaim_pending is False
            monkeypatch.setattr(main_screen, "_refresh_layout", _record_layout)

        assert await _run_turn_end_absorb(app, main_screen, pilot) == "absorb"
        assert layout_refreshes == []
        assert app._gc_freeze._idle_reclaim_pending is False
        assert app._gc_freeze._idle_reclaim_reasons == set()

        with monkeypatch.context() as patch:
            patch.setattr(gc_freeze, "textual_screen_caches_acyclic", lambda: False)
            assert await _run_turn_end_absorb(app, main_screen, pilot) == "absorb"
        assert layout_refreshes


async def test_foreign_freeze_degradation_publishes_user_visible_startup_warning(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from chrys.app.tui.support import gc_freeze

    bus = EventBus()
    warnings: list[Warning] = []

    async def _record_warning(event: Warning) -> None:
        warnings.append(event)

    await bus.subscribe(Warning, _record_warning)
    with monkeypatch.context() as patch:
        patch.setattr(gc_freeze.gc, "get_freeze_count", lambda: 5)
        app = make_chrys_app(tmp_path, event_bus=bus, gc_freeze_enabled=True)

        async with app.run_test() as pilot:
            await pilot.pause()
            assert app._gc_freeze.enabled is False
            assert app._gc_freeze_watchdog is None

    assert [(warning.code, warning.message) for warning in warnings] == [
        (
            "gc_freeze_disabled",
            "GC freeze optimization was disabled because this process already contains 5 externally frozen objects.",
        )
    ]


@pytest.mark.parametrize("lost_hop", [1, 2])
async def test_gc_freeze_recovers_callback_pruned_with_transient_screen(
    lost_hop: int,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A callback accepted by App but stranded on a pruned Screen must be replaced exactly once."""
    from functools import partial

    from chrys.app.tui.support import gc_freeze
    from chrys.app.tui.support.gc_freeze import GcReclaimReason

    class _PruneOnCallbackScreen(Screen):
        def __init__(self, callback_name: str) -> None:
            self._callback_name = callback_name
            super().__init__()

        def _invoke_later(self, callback, sender) -> None:
            super()._invoke_later(callback, sender)
            callback_repr = repr(callback)
            if self._callback_name not in callback_repr:
                return
            app._transient_callbacks += 1
            app._dropped_callbacks.append(callback_repr)
            self.app.pop_screen()
            # Model Textual pruning the transient pump before its accepted
            # callback queue drains.  App forwarding above is real; only the
            # otherwise timing-sensitive prune is made deterministic.
            self._callbacks.clear()

    class _CallbackLossApp(ChrysApp):
        CSS_PATH = _CHRYS_CSS

        def __init__(self, *args: object, **kwargs: object) -> None:
            self._drop_callback_name: str | None = None
            self._drop_pushes = 0
            self._transient_callbacks = 0
            self._dropped_callbacks: list[str] = []
            super().__init__(*args, **kwargs)  # type: ignore[arg-type]

        def arm_callback_loss(self, hop: int) -> None:
            self._drop_callback_name = "_after_first_refresh" if hop == 1 else "_run"

        def call_after_refresh(self, callback, *args: object, **kwargs: object) -> bool:
            callback_name = callback.func.__name__ if isinstance(callback, partial) else ""
            accepted = super().call_after_refresh(callback, *args, **kwargs)
            if accepted and callback_name == self._drop_callback_name:
                self._drop_pushes += 1
                self._drop_callback_name = None
                self.push_screen(_PruneOnCallbackScreen(callback_name))
            return accepted

    monkeypatch.setattr(gc_freeze, "_SCHEDULE_RECOVERY_SECONDS", 0.0)
    app = _CallbackLossApp(
        EventBus(),
        ShutdownOnlyEngine(),  # type: ignore[arg-type]
        settings=Settings(),
        state_store=JsonFileStateStore(tmp_path),
        agent_registry=EmptyAgentRegistry(),  # type: ignore[arg-type]
        gc_freeze_enabled=True,
    )

    async with app.run_test() as pilot:
        await wait_for(
            lambda: app._gc_freeze.frozen,
            pilot=pilot,
            description="app gc-freeze coordinator freezes after mount",
        )
        assert app._gc_freeze.frozen is True
        assert app._gc_freeze_watchdog is not None
        app._gc_freeze_watchdog.pause()

        actions: list[str] = []
        original_log_action = app._gc_freeze._log_action

        def _record_action(action: str) -> None:
            actions.append(action)
            original_log_action(action)

        monkeypatch.setattr(app._gc_freeze, "_log_action", _record_action)
        app.arm_callback_loss(lost_hop)
        app._gc_freeze.request_reclaim(reason=GcReclaimReason.SESSION_READY, prompt=True)

        await wait_for(
            lambda: app._transient_callbacks == 1 and app.screen is app._main_screen,
            pilot=pilot,
            description="transient screen restores MainScreen",
        )

        assert app.screen is app._main_screen
        assert app._gc_freeze._prompt_reclaim_pending is True, repr(app._dropped_callbacks)
        assert app._gc_freeze._scheduled is True
        assert app._drop_pushes == 1
        assert app._transient_callbacks == 1
        expected_callback = "_after_first_refresh" if lost_hop == 1 else "_run"
        assert len(app._dropped_callbacks) == 1
        assert expected_callback in app._dropped_callbacks[0]
        assert actions == []

        app._gc_freeze.on_tick()
        await wait_for(
            lambda: not app._gc_freeze._prompt_reclaim_pending,
            pilot=pilot,
            description="pending GC reclaim completes",
        )

        assert app._gc_freeze._prompt_reclaim_pending is False
        assert actions == ["full"]
        assert not await wait_until(lambda: actions != ["full"], timeout=0.2, pilot=pilot)


async def test_gc_freeze_app_key_activity_demotes_terminal_before_dispatch(tmp_path: Path) -> None:
    """A real non-forwarded Key must update coordinator chronology before widget dispatch."""
    from textual import events
    from textual.geometry import Size

    from chrys.app.tui.support.gc_freeze import GcAbsorbReason

    app = make_chrys_app(tmp_path, gc_freeze_enabled=True)

    async with app.run_test() as pilot:
        await wait_for(
            lambda: app._gc_freeze.frozen,
            pilot=pilot,
            description="app gc-freeze coordinator freezes after mount",
        )
        assert app._gc_freeze.frozen is True

        previous_input_at = app._gc_freeze._last_input_at
        app._gc_freeze._absorb_pending = True
        app._gc_freeze._terminal_boundary_at = previous_input_at
        await pilot.press("x")

        assert app._gc_freeze._last_input_at > previous_input_at
        assert app._gc_freeze._terminal_boundary_at is None
        assert app._gc_freeze._absorb_pending is True

        forwarded = events.Key("y", "y")
        forwarded._set_forwarded()
        last_input_at = app._gc_freeze._last_input_at
        await app.on_event(forwarded)
        assert app._gc_freeze._last_input_at == last_input_at

        passive_move = events.MouseMove(
            None,
            x=1,
            y=1,
            delta_x=1,
            delta_y=0,
            button=0,
            shift=False,
            meta=False,
            ctrl=False,
        )
        await app.on_event(passive_move)
        assert app._gc_freeze._last_input_at == last_input_at

        app._gc_freeze._terminal_boundary_at = last_input_at
        await app.on_event(events.AppFocus())
        assert app._gc_freeze._last_input_at > last_input_at
        assert app._gc_freeze._terminal_boundary_at is None

        app._gc_freeze._absorb_pending = False
        app._gc_freeze._absorb_reasons.clear()
        resize = events.Resize(Size(120, 40), Size(120, 40))
        await app.on_event(resize)
        assert app._gc_freeze._absorb_pending is True
        assert app._gc_freeze._absorb_reasons == {GcAbsorbReason.VIEWPORT_UPDATED}


async def test_gc_freeze_effective_theme_change_requests_idle_reclaim(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from chrys.app.tui.support.gc_freeze import GcReclaimReason

    monkeypatch.setattr("chrys.app.tui.app.persist_theme", lambda _theme: None)
    app = make_chrys_app(tmp_path, settings=Settings(theme="chrys"), gc_freeze_enabled=True)

    async with app.run_test() as pilot:
        await wait_for(
            lambda: app._gc_freeze.frozen,
            pilot=pilot,
            description="app gc-freeze coordinator freezes after mount",
        )
        assert app._gc_freeze.frozen is True

        app._gc_freeze._absorb_pending = False
        app._gc_freeze._absorb_reasons.clear()
        app._gc_freeze._idle_reclaim_pending = False
        app._gc_freeze._idle_reclaim_reasons.clear()
        app.theme = app.theme
        assert app._gc_freeze._absorb_pending is False
        assert app._gc_freeze._idle_reclaim_pending is False

        app.theme = "textual-dark"
        assert app._gc_freeze._absorb_pending is False
        assert app._gc_freeze._idle_reclaim_pending is True
        assert app._gc_freeze._idle_reclaim_reasons == {GcReclaimReason.THEME_UPDATED}
        assert app._gc_freeze._terminal_boundary_at is None


async def test_gc_freeze_pointer_hold_blocks_gc_and_lost_button_paths_reconcile(tmp_path: Path) -> None:
    """Held selection gestures are hard gates; release and lost-button paths restore progress."""
    from textual import events

    from chrys.app.tui.support.gc_freeze import GcFreezeBlockReason, GcReclaimReason

    def _mouse_event(event_type: type[events.MouseEvent], button: int) -> events.MouseEvent:
        return event_type(
            None,
            x=1,
            y=1,
            delta_x=0,
            delta_y=0,
            button=button,
            shift=False,
            meta=False,
            ctrl=False,
        )

    app = make_chrys_app(tmp_path, gc_freeze_enabled=True)

    async with app.run_test() as pilot:
        await wait_for(
            lambda: app._gc_freeze.frozen,
            pilot=pilot,
            description="app gc-freeze coordinator freezes after mount",
        )
        assert app._gc_freeze.frozen is True

        actions: list[str] = []
        original_log_action = app._gc_freeze._log_action

        def _record_action(action: str) -> None:
            actions.append(action)
            original_log_action(action)

        app._gc_freeze._log_action = _record_action
        await app.on_event(_mouse_event(events.MouseDown, 1))
        assert app.freeze_block_reason() is GcFreezeBlockReason.POINTER_BUTTON_HELD

        app._gc_freeze.request_reclaim(reason=GcReclaimReason.SESSION_READY, prompt=True)
        # This is the first deferral; a prior log-throttled occurrence cannot satisfy the wait.
        assert app._gc_freeze._last_deferral_reason is None
        await wait_for(
            lambda: app._gc_freeze._last_deferral_reason is GcFreezeBlockReason.POINTER_BUTTON_HELD,
            pilot=pilot,
            description="scheduled reclaim observes the held pointer",
        )
        for _ in range(1_000):
            app._gc_freeze.on_tick()

        assert actions == []
        assert app._gc_freeze._prompt_reclaim_pending is True
        last_input_at = app._gc_freeze._last_input_at
        drag_move = _mouse_event(events.MouseMove, 1)
        drag_move.time = last_input_at + 1.0
        await app.on_event(drag_move)
        assert app._gc_freeze._last_input_at == drag_move.time
        assert app.freeze_block_reason() is GcFreezeBlockReason.POINTER_BUTTON_HELD

        await app.on_event(_mouse_event(events.MouseUp, 1))
        assert app._gc_pointer_buttons_down == set()
        app._gc_freeze.on_tick()
        await wait_for(
            lambda: not app._gc_freeze._prompt_reclaim_pending,
            pilot=pilot,
            description="pending GC reclaim completes",
        )
        assert actions == ["full"]

        await app.on_event(_mouse_event(events.MouseDown, 1))
        await app.on_event(_mouse_event(events.MouseMove, 0))
        assert app._gc_pointer_buttons_down == set()
        await app.on_event(_mouse_event(events.MouseDown, 1))
        blur_input_at = app._gc_freeze._last_input_at
        await app.on_event(events.AppBlur())
        assert app._gc_pointer_buttons_down == set()
        assert app._gc_freeze._last_input_at == blur_input_at
        await app.on_event(_mouse_event(events.MouseDown, 1))
        focus_input_at = app._gc_freeze._last_input_at
        focus = events.AppFocus()
        focus.time = focus_input_at + 1.0
        await app.on_event(focus)
        assert app._gc_pointer_buttons_down == set()
        assert app._gc_freeze._last_input_at == focus.time
