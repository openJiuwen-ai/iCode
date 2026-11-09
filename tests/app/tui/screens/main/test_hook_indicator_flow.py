# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The bottom-right hook indicator, driven by real ``HookExecutionsChanged`` events in the real app.

The backend side is a scripted bus: the test accepts the submitted message as a
turn and publishes snapshots the way the engine does. What is asserted is the
rendered frame.
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from types import ModuleType

import pytest
from rich.cells import cell_len
from textual.pilot import Pilot
from textual.widgets import TabbedContent

from chrys.app.tui.app import ChrysApp
from chrys.app.tui.screens.dialogs.runtime_details import RuntimeDetailsDialog
from chrys.app.tui.screens.main.screen import MainScreen
from chrys.app.tui.widgets.chat.messages import AgentMessage
from chrys.app.tui.widgets.chat.messages import UserMessage as UserMessageWidget
from chrys.app.tui.widgets.chat.panel import ChatPanel
from chrys.app.tui.widgets.chat.renderers.sub_agent import SubAgentToolCall
from chrys.app.tui.widgets.chat.waiting_row import WaitingRow
from chrys.app.tui.widgets.chrome import hook_indicator as hook_indicator_module
from chrys.app.tui.widgets.chrome.footer import ChrysFooter
from chrys.app.tui.widgets.chrome.hook_indicator import HOOK_INDICATOR_WIDTH, HookIndicator
from chrys.app.tui.widgets.chrome.input_bar import InputBar
from chrys.app.tui.widgets.hook_activity import HOOK_SHOW_AFTER_SECONDS
from chrys.app.tui.widgets.markdown import VirtualizedMarkdown
from chrys.app.tui.widgets.sidebar.panel import SIDEBAR_HOOKS_TAB, SidebarPanel
from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import (
    ExecutionChanged,
    HookExecutionsChanged,
    InvocationAborted,
    InvocationMessage,
    InvocationStarted,
    InvocationToolCallStart,
    SessionNew,
    SessionReady,
    SessionRestored,
    UserInterrupt,
    UserMessage,
)
from chrys.foundation.models.execution import ExecutionSnapshot
from chrys.foundation.models.hook_executions import HookExecutionView
from chrys.foundation.models.invocations import InvocationOrigin
from chrys.foundation.tool_kinds import KIND_SUB_AGENT
from chrys.service.profiles.agents.schema import AgentProfile
from chrys.service.profiles.models.registry import ModelProfileRegistry
from chrys.service.profiles.models.schema import ModelProfile
from tests.support.pilot_barrier import screen_is_settled
from tests.support.tui_app_harness import SessionGenerationEngine, make_chrys_app
from tests.support.tui_helpers import click_when_settled, resize_when_settled
from tests.support.waiting import (
    ENGINE_TEST_WAIT_TIMEOUT,
    ENGINE_TURN_TIMEOUT,
    wait_for,
    wait_until,
    wait_until_quiet,
    with_wait_deadline,
)

from ._workflow_support import WorkflowEngine

_SESSION = "test-session"
_PROFILE = AgentProfile(name="Code", display_name="Code", description="Test profile")
_TURN = InvocationOrigin("turn", _SESSION, "turn-1", None)


class _Registry:
    def list_profiles(self) -> list[AgentProfile]:
        return [_PROFILE]

    def load_all(self) -> None:
        return

    def get(self, name: str) -> AgentProfile | None:
        return _PROFILE if name == _PROFILE.name else None


class _Engine(SessionGenerationEngine):
    # The sidebar's Hooks tab reads it once the indicator opens that tab.
    hook_runs = None

    def __init__(self, bus: EventBus, cwd: Path, *, start_session: bool = True) -> None:
        self._bus = bus
        self._cwd = cwd
        self._start_session = start_session
        self.turn_lifecycle_task = None

    async def start(self, _profile: AgentProfile) -> None:
        if self._start_session:
            await self._bus.publish(
                SessionReady(
                    agent_profile=_PROFILE.name,
                    display_name=_PROFILE.display_name,
                    session_id=_SESSION,
                    primary_cwd=str(self._cwd),
                )
            )


class _WorkflowModeEngine(_Engine, WorkflowEngine):
    """The scripted session, and the idle execution the app mode switch asks for."""


def _make_app(
    tmp_path: Path, bus: EventBus, *, start_session: bool = True, engine_type: type[_Engine] = _Engine
) -> ChrysApp:
    workspace = tmp_path / "work"
    workspace.mkdir(exist_ok=True)
    models = ModelProfileRegistry()
    models.register(ModelProfile(id="model-profile", name="Configured Model", model_id="test-model"))
    return make_chrys_app(
        tmp_path / "state",
        engine=engine_type(bus, workspace, start_session=start_session),
        event_bus=bus,
        agent_registry=_Registry(),
        model_registry=models,
    )


def _view(
    hook_id: str,
    *,
    waited_at: str = "call",
    age: float = 5.0,
    queued: bool = False,
    now: float | None = None,
) -> HookExecutionView:
    return HookExecutionView(
        hook_id=hook_id,
        waited_at=waited_at,  # type: ignore[arg-type]
        queued=queued,
        started_at=(time.monotonic() if now is None else now) - age,
    )


class _IndicatorClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now


@pytest.fixture
def indicator_clock(monkeypatch: pytest.MonkeyPatch) -> _IndicatorClock:
    # Only hook ages are controlled. Textual timers, waits and the event loop
    # keep their real clock so the rendered frame still changes on real ticks.
    clock = _IndicatorClock()
    shadow = ModuleType("time")
    shadow.__dict__.update(vars(time))
    shadow.monotonic = clock.monotonic
    monkeypatch.setattr(hook_indicator_module, "time", shadow)
    return clock


@pytest.fixture
def aged_indicator_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """The indicator reads every hook age one display threshold older than it is.

    For runs a real manager stamps with the real clock, where the tests are about which run
    shows, not when: each is past the 1 s threshold as soon as it is reported. The real second
    still runs where the real clock stays (the snapshot under a modal, the long transcript).
    """
    real = time.monotonic
    shadow = ModuleType("time")
    shadow.__dict__.update(vars(time))
    shadow.monotonic = lambda: real() + HOOK_SHOW_AFTER_SECONDS
    monkeypatch.setattr(hook_indicator_module, "time", shadow)


async def _snapshot(bus: EventBus, *views: HookExecutionView, session_id: str = _SESSION) -> None:
    await bus.publish(HookExecutionsChanged(session_id=session_id, executions=views), raise_handler_errors=True)


def _frame(app: ChrysApp) -> str:
    return "\n".join(strip.text for strip in app.screen._compositor.render_strips())


def _indicator(app: ChrysApp) -> str:
    """The cells the indicator occupies on screen; empty when it is not painted."""
    screen = app._main_screen
    if screen is None or app.screen is not screen:
        return ""
    indicator = screen.query_one(HookIndicator)
    region = indicator.region
    if not indicator.display or region.height == 0:
        return ""
    lines = _frame(app).splitlines()
    if region.y >= len(lines):
        return ""
    return lines[region.y][region.x : region.right].strip()


async def _ready(pilot: Pilot) -> MainScreen:
    app = pilot.app
    assert isinstance(app, ChrysApp)

    def ready() -> bool:
        screen = app._main_screen
        if screen is None or app.screen is not screen or screen._state.run.agent_loading:
            return False
        # The main screen paints while the agent loads, so the footer recomposes
        # once it is ready: keys queried before that recompose lands are stale.
        footer = screen.query_one(ChrysFooter)

        return (
            not footer._binding_recompose_in_progress
            and not footer._binding_recompose_dirty
            and footer._visible_binding_signature == footer._binding_signature(screen)
        )

    await wait_for(ready, pilot=pilot, description="session ready with its footer keys")
    main = app._main_screen
    assert main is not None
    return main


async def _start_turn(pilot: Pilot, bus: EventBus, main: MainScreen) -> None:
    async def accept(event: UserMessage) -> None:
        event.started_turn = True

    await bus.subscribe(UserMessage, accept)
    input_bar = main.query_one(InputBar)
    input_bar.value = "hello"
    await input_bar.action_submit()
    await wait_for(lambda: main._state.run.agent_running, pilot=pilot, description="the turn running")
    await bus.unsubscribe(UserMessage, accept)


async def _finish_turn(bus: EventBus) -> None:
    await bus.publish(InvocationMessage(text="done", is_final=True, origin=_TURN, session_id=_SESSION))


def _shown_name(text: str) -> str:
    """The indicator's text after its separator and spinner cells: ``Hooks`` or the hook id."""
    return re.sub(r"^\W+", "", text)


async def _shows(pilot: Pilot, expected: str, *, timeout: float = 5.0, what: str = "") -> None:
    """The indicator shows exactly *expected* (``Hooks`` or a hook id) after its spinner cells."""
    app = pilot.app
    assert isinstance(app, ChrysApp)
    try:
        await wait_for(
            lambda: _shown_name(_indicator(app)) == expected,
            timeout=timeout,
            pilot=pilot,
            description=what or f"indicator shows {expected!r}",
        )
    except AssertionError as exc:
        raise AssertionError(f"{exc}; indicator text={_indicator(app)!r}") from None


async def _keeps(pilot: Pilot, expected: str, *, seconds: float = 0.3) -> None:
    """The indicator keeps showing exactly *expected* for *seconds*.

    What this default window proves is an event's effect: a snapshot or a mode change reaches the
    indicator on the screen's next pump (``call_later``), well inside 0.3 s, and the 0.1 s spinner
    timer, when it runs, ticks about three times in it.
    """
    app = pilot.app
    assert isinstance(app, ChrysApp)
    assert not await wait_until(lambda: _shown_name(_indicator(app)) != expected, timeout=seconds, pilot=pilot), (
        _indicator(app)
    )


async def _keeps_through_ticks(pilot: Pilot, expected: str, *, ticks: int = 3) -> None:
    """The indicator keeps showing exactly *expected* while its running timer ticks *ticks* times.

    Each tick reads the hook ages again: with the ages held by ``indicator_clock``, the ticks are
    what could change the label, so counting them, not seconds, is what proves it stays.
    """
    app = pilot.app
    assert isinstance(app, ChrysApp)
    main = app._main_screen
    assert main is not None
    indicator = main.query_one(HookIndicator)
    start = indicator._frame
    seen: list[str] = []

    def ticked() -> bool:
        text = _indicator(app)
        if _shown_name(text) != expected:
            seen.append(text)
        return bool(seen) or indicator._frame >= start + ticks

    await wait_for(ticked, pilot=pilot, description=f"{ticks} timer ticks showing {expected!r}")
    assert not seen, seen


# --- Idle, threshold, latest-started --------------------------------------------


async def test_idle_shows_hooks_where_the_quit_hint_was(tmp_path: Path) -> None:
    bus = EventBus()
    app = _make_app(tmp_path, bus)
    async with app.run_test(size=(160, 40)) as pilot:
        await _ready(pilot)
        await _shows(pilot, "Hooks")
        last_row = _frame(app).splitlines()[-1]
        assert last_row.rstrip().endswith("Hooks"), last_row
        assert "Quit" not in last_row


async def test_a_run_shows_after_a_second_and_goes_back_to_hooks_when_it_ends(
    tmp_path: Path, indicator_clock: _IndicatorClock
) -> None:
    bus = EventBus()
    app = _make_app(tmp_path, bus)
    async with app.run_test(size=(160, 40)) as pilot:
        main = await _ready(pilot)
        await _start_turn(pilot, bus, main)
        run = _view("protect-env", age=0.0, now=indicator_clock.now)
        await _snapshot(bus, run)
        indicator_clock.now = run.started_at + 0.999
        # The ages are held by the clock, so GC pauses or slow paints cannot advance
        # the hook age under test: the timer's ticks are what read it again.
        await _keeps_through_ticks(pilot, "Hooks")
        indicator_clock.now = run.started_at + 1.0
        await _shows(pilot, "protect-env", timeout=4.0, what="shown after its first second")
        assert "protect-env" in _indicator(app)
        await _snapshot(bus)
        await _shows(pilot, "Hooks")


async def test_a_run_under_a_second_never_shows(tmp_path: Path, indicator_clock: _IndicatorClock) -> None:
    bus = EventBus()
    app = _make_app(tmp_path, bus)
    async with app.run_test(size=(160, 40)) as pilot:
        main = await _ready(pilot)
        await _start_turn(pilot, bus, main)
        run = _view("quick", age=0.0, now=indicator_clock.now)
        await _snapshot(bus, run)
        indicator_clock.now = run.started_at + 0.999
        await _keeps_through_ticks(pilot, "Hooks")
        await _snapshot(bus)
        await wait_for(
            lambda: not main.query_one(HookIndicator)._executions,
            pilot=pilot,
            description="the finished hook snapshot applied",
        )
        indicator_clock.now = run.started_at + 2.0
        await _keeps(pilot, "Hooks")


async def test_the_latest_started_run_shows_and_the_previous_one_comes_back(
    tmp_path: Path, indicator_clock: _IndicatorClock
) -> None:
    bus = EventBus()
    app = _make_app(tmp_path, bus)
    async with app.run_test(size=(160, 40)) as pilot:
        main = await _ready(pilot)
        await _start_turn(pilot, bus, main)
        a = _view("hook-a", age=2.0, now=indicator_clock.now)
        await _snapshot(bus, a)
        await _shows(pilot, "hook-a")

        # B starts later: under a second it does not take over yet, then it does.
        b = _view("hook-b", waited_at="turn", age=0.0, now=indicator_clock.now)
        await _snapshot(bus, a, b)
        indicator_clock.now = b.started_at + 0.999
        await _keeps_through_ticks(pilot, "hook-a")
        indicator_clock.now = b.started_at + 1.0
        await _shows(pilot, "hook-b", timeout=4.0, what="B once it lasted a second")

        await _snapshot(bus, a)
        await _shows(pilot, "hook-a", what="A again once B ended")
        await _snapshot(bus)
        await _shows(pilot, "Hooks")


async def test_a_queued_run_shows_like_a_running_one(tmp_path: Path) -> None:
    bus = EventBus()
    app = _make_app(tmp_path, bus)
    async with app.run_test(size=(160, 40)) as pilot:
        main = await _ready(pilot)
        await _start_turn(pilot, bus, main)
        await _snapshot(bus, _view("guard", queued=True, age=1.5))
        await _shows(pilot, "guard")
        # No clock and no count, however long it lasts and however many run.
        await _snapshot(bus, _view("slow", age=12), _view("older", age=20))
        await _shows(pilot, "slow")
        assert not re.search(r"\(\d+s\)|\+\d|queued", _indicator(app)), _indicator(app)


async def test_runs_nobody_waits_for_never_show(tmp_path: Path) -> None:
    bus = EventBus()
    app = _make_app(tmp_path, bus)
    async with app.run_test(size=(160, 40)) as pilot:
        main = await _ready(pilot)
        await _start_turn(pilot, bus, main)
        await _snapshot(
            bus,
            _view("ff", waited_at="none", age=9),
            _view("on-interrupt", waited_at="none", age=9),
        )
        indicator = main.query_one(HookIndicator)
        await wait_for(
            lambda: len(indicator._executions) == 2, pilot=pilot, description="the unwaited runs' snapshot applied"
        )
        # Both are long past the threshold: the paint that applied them already decided, and a
        # timer started by them would tick about three times in the window.
        await _keeps(pilot, "Hooks")


async def test_a_long_id_is_cut_in_the_middle_to_the_indicators_cap(tmp_path: Path) -> None:
    bus = EventBus()
    app = _make_app(tmp_path, bus)
    async with app.run_test(size=(80, 40)) as pilot:
        main = await _ready(pilot)
        long_id = "a-very-long-hook-identifier-that-cannot-fit-in-the-indicator"
        await _snapshot(bus, _view(long_id, age=2))
        await wait_for(lambda: "…" in _indicator(app), pilot=pilot, description="the id cut")
        name = _shown_name(_indicator(app))
        head, tail = name.split("…")
        assert head and tail, name
        assert long_id.startswith(head) and long_id.endswith(tail), "head and tail kept, the middle cut"
        indicator = main.query_one(HookIndicator)
        assert indicator.region.right <= app.size.width


async def test_the_indicator_has_no_tooltip(tmp_path: Path) -> None:
    bus = EventBus()
    app = _make_app(tmp_path, bus)
    async with app.run_test(size=(160, 40)) as pilot:
        main = await _ready(pilot)
        indicator = main.query_one(HookIndicator)
        assert indicator.tooltip is None
        await _snapshot(bus, _view("guard", age=2))
        await _shows(pilot, "guard")
        assert indicator.tooltip is None
        await pilot.hover(indicator)
        assert indicator.tooltip is None


# --- Per-session snapshots ------------------------------------------------------


async def test_a_snapshot_before_the_first_session_shows_once_it_is_current(tmp_path: Path) -> None:
    bus = EventBus()
    app = _make_app(tmp_path, bus, start_session=False)
    async with app.run_test(size=(160, 40)) as pilot:
        await wait_for(
            lambda: app._main_screen is not None and app.screen is app._main_screen,
            pilot=pilot,
            description="the main screen up",
        )
        await _snapshot(bus, _view("recovered", waited_at="session", age=5))
        await _snapshot(bus, _view("foreign", waited_at="session", age=5), session_id="other")
        await bus.publish(
            SessionReady(
                agent_profile=_PROFILE.name,
                display_name=_PROFILE.display_name,
                session_id=_SESSION,
                primary_cwd=str(tmp_path / "work"),
            )
        )
        await _shows(pilot, "recovered", what="the stored snapshot shown")


async def test_a_new_sessions_snapshot_before_its_ready_shows_once_it_is_current(tmp_path: Path) -> None:
    bus = EventBus()
    app = _make_app(tmp_path, bus)
    news: list[SessionNew] = []

    async def on_new(event: SessionNew) -> None:
        news.append(event)

    await bus.subscribe(SessionNew, on_new)
    async with app.run_test(size=(160, 40)) as pilot:
        main = await _ready(pilot)
        input_bar = main.query_one(InputBar)
        input_bar.value = "/new"
        await input_bar.action_submit()
        await wait_for(lambda: news, pilot=pilot, description="the new session requested")

        # The old session closes: its session_end run shows while it is current.
        await _snapshot(bus, _view("on-end", age=2))
        await _shows(pilot, "on-end")
        # The new session's build registers a run before its SessionReady.
        await _snapshot(bus, _view("recovered", waited_at="session", age=5), session_id="s2")
        await _keeps(pilot, "on-end")

        await bus.publish(
            SessionReady(agent_profile=_PROFILE.name, display_name=_PROFILE.display_name, session_id="s2")
        )
        await _shows(pilot, "recovered", what="the new session's stored snapshot")
        assert "on-end" not in _indicator(app)


@pytest.mark.parametrize("switch", ["ready", "restored"])
async def test_a_late_snapshot_from_the_old_session_leaves_the_new_sessions_alone(tmp_path: Path, switch: str) -> None:
    bus = EventBus()
    app = _make_app(tmp_path, bus)
    async with app.run_test(size=(160, 40)) as pilot:
        await _ready(pilot)
        await _snapshot(bus, _view("old-running", age=5))
        await _shows(pilot, "old-running")
        if switch == "ready":
            event: SessionReady | SessionRestored = SessionReady(
                agent_profile=_PROFILE.name, display_name=_PROFILE.display_name, session_id="s2"
            )
        else:
            event = SessionRestored(session_id="s2", agent_profile=_PROFILE.name, display_name=_PROFILE.display_name)
        await bus.publish(event)
        await _shows(pilot, "Hooks", what="the old session's runs cleared at the switch")

        await _snapshot(bus, _view("new-bg", waited_at="session", age=5), session_id="s2")
        await _shows(pilot, "new-bg")
        # The old manager closing late: neither its runs nor its empty set may win.
        await _snapshot(bus, _view("old-late", age=2))
        await _keeps(pilot, "new-bg")
        await _snapshot(bus)
        await _keeps(pilot, "new-bg")


async def test_a_restore_after_its_ready_keeps_showing_the_restored_sessions_runs(tmp_path: Path) -> None:
    """The engine publishes SessionReady before SessionRestored: the restored session's runs, stored by then, stay."""
    bus = EventBus()
    app = _make_app(tmp_path, bus)
    async with app.run_test(size=(160, 40)) as pilot:
        await _ready(pilot)
        await _snapshot(bus, _view("old-running", age=5))
        await _shows(pilot, "old-running")
        await bus.publish(
            SessionReady(agent_profile=_PROFILE.name, display_name=_PROFILE.display_name, session_id="s2")
        )
        await _snapshot(bus, _view("recovered", waited_at="session", age=5), session_id="s2")
        await _shows(pilot, "recovered")
        # The old manager's closing report lands between the two events.
        await _snapshot(bus)
        await bus.publish(
            SessionRestored(session_id="s2", agent_profile=_PROFILE.name, display_name=_PROFILE.display_name)
        )
        await _keeps(pilot, "recovered")
        await _snapshot(bus, session_id="s2")
        await _shows(pilot, "Hooks")


# --- Fallback cleanup -----------------------------------------------------------


@pytest.mark.parametrize("how", ["interrupt-button", "bus"])
async def test_after_an_interrupt_the_managers_report_removes_the_cancelled_run(tmp_path: Path, how: str) -> None:
    """No fence on UserInterrupt: the last snapshot is the truth, and the manager reports the removal."""
    bus = EventBus()
    app = _make_app(tmp_path, bus)
    interrupts: list[UserInterrupt] = []

    async def on_interrupt(event: UserInterrupt) -> None:
        interrupts.append(event)

    await bus.subscribe(UserInterrupt, on_interrupt)
    async with app.run_test(size=(160, 40)) as pilot:
        main = await _ready(pilot)
        await _start_turn(pilot, bus, main)
        session_bg = _view("session-bg", waited_at="session", age=9)
        ff = _view("ff", waited_at="none", age=8.5)
        turn_bg = _view("turn-bg", waited_at="turn", age=8)
        await _snapshot(bus, session_bg, ff, turn_bg, _view("stuck", age=5))
        await _shows(pilot, "stuck")
        if how == "interrupt-button":
            # The user's interrupt: the input bar's button reads Interrupt while a turn runs.
            await click_when_settled(pilot, main.query_one(InputBar).query_one("#send-btn"))
            await wait_for(lambda: interrupts, pilot=pilot, description="the TUI published UserInterrupt")
        else:
            await bus.publish(UserInterrupt(session_id=_SESSION))
        # The interrupt alone changes nothing on the indicator.
        await _keeps(pilot, "stuck")
        # The manager reports the cancelled blocking run removed; the others stay.
        await _snapshot(bus, session_bg, ff, turn_bg)
        await _shows(pilot, "turn-bg")
        await _snapshot(bus, session_bg, ff)
        await _shows(pilot, "session-bg")


@pytest.mark.usefixtures("aged_indicator_clock")
async def test_a_real_interrupted_blocking_hook_disappears_through_the_managers_report(tmp_path: Path) -> None:
    """Real hook subprocess: cancelling the firing task (what an interrupt does) reports the run removed."""
    import asyncio
    import sys
    import textwrap

    from chrys.orchestration.engine.hook_reporting import wire_hook_reporting
    from chrys.service.hooks.events import HookEvent
    from chrys.service.hooks.manager import HookManager
    from chrys.service.hooks.schema import HookConfig, HookExecution, HookRun, HooksFile

    release = tmp_path / "release"
    script = textwrap.dedent(
        f"""
        import os, time
        while not os.path.exists({str(release)!r}):
            time.sleep(0.01)
        """
    )
    hooks_dir = tmp_path / "hooks"
    hooks_dir.mkdir()
    manager = HookManager(
        file=HooksFile(
            hooks=[
                HookConfig(
                    id="slow-guard",
                    event=HookEvent.BEFORE_TOOL_CALL,
                    run=HookRun(type="command", argv=[sys.executable, "-c", script]),
                    execution=HookExecution(mode="blocking", timeout_seconds=30),
                )
            ]
        ),
        hooks_dir=hooks_dir,
    )
    bus = EventBus()
    app = _make_app(tmp_path, bus)
    fire: asyncio.Task[object] | None = None

    async def cancel_on_interrupt(_event: UserInterrupt) -> None:
        # The engine cancels the turn's task; the hook's blocking fire goes with it.
        if fire is not None:
            fire.cancel()

    await bus.subscribe(UserInterrupt, cancel_on_interrupt)
    async with app.run_test(size=(160, 40)) as pilot:
        main = await _ready(pilot)
        await _start_turn(pilot, bus, main)
        wire_hook_reporting(manager, bus, session_id=_SESSION)
        try:
            fire = asyncio.create_task(
                manager.fire(HookEvent.BEFORE_TOOL_CALL, {"tool": {"name": "shell", "args": {}}})
            )
            await _shows(pilot, "slow-guard", timeout=ENGINE_TURN_TIMEOUT)
            await click_when_settled(pilot, main.query_one(InputBar).query_one("#send-btn"))
            await _shows(pilot, "Hooks", what="the cancelled run reported removed")
            assert manager.executions() == ()
        finally:
            release.touch()
            if fire is not None:
                await asyncio.gather(fire, return_exceptions=True)
            await manager.drain_session()


async def _sub_agent_card(pilot: Pilot, bus: EventBus) -> SubAgentToolCall:
    await bus.publish(
        InvocationToolCallStart(
            origin=_TURN,
            session_id=_SESSION,
            tool_kind=KIND_SUB_AGENT,
            call_id="call-explore",
            args={"prompt": "look around"},
        )
    )
    await bus.publish(
        InvocationStarted(
            origin=InvocationOrigin("sub_agent", _SESSION, "inv-1", _TURN),
            session_id=_SESSION,
            agent_name="Explore",
            parent_call_id="call-explore",
        )
    )
    await wait_for(lambda: pilot.app.screen.query(SubAgentToolCall), pilot=pilot, description="the card")
    return pilot.app.screen.query_one(SubAgentToolCall)


async def test_a_sub_agent_abort_clears_no_hook_and_still_ends_its_card(tmp_path: Path) -> None:
    bus = EventBus()
    app = _make_app(tmp_path, bus)
    async with app.run_test(size=(160, 40)) as pilot:
        main = await _ready(pilot)
        await _start_turn(pilot, bus, main)
        card = await _sub_agent_card(pilot, bus)
        await _snapshot(bus, _view("main-guard", age=6), _view("child-guard", age=5))
        await _shows(pilot, "child-guard")
        child = InvocationOrigin("sub_agent", _SESSION, "inv-1", _TURN)
        await bus.publish(InvocationAborted(origin=child, session_id=_SESSION, agent_name="Explore", last_error="x"))
        await wait_for(lambda: card.has_class("-error"), pilot=pilot, description="the card marked failed")
        await _keeps(pilot, "child-guard")
        # The main agent's blocking run is untouched too.
        await _snapshot(bus, _view("main-guard", age=6))
        await _shows(pilot, "main-guard")


async def test_a_blocking_run_still_running_after_the_turn_goes_idle_keeps_showing(tmp_path: Path) -> None:
    """No fence on idle. E.g. a user_prompt_submit hook for a mid-run injection outlives the turn."""
    bus = EventBus()
    app = _make_app(tmp_path, bus)
    async with app.run_test(size=(160, 40)) as pilot:
        main = await _ready(pilot)
        await _start_turn(pilot, bus, main)
        session_bg = _view("session-bg", waited_at="session", age=9)
        turn_bg = _view("turn-bg", waited_at="turn", age=7)
        inject_guard = _view("inject-guard", age=6)
        await _snapshot(bus, session_bg, turn_bg, inject_guard)
        await _shows(pilot, "inject-guard")
        await _finish_turn(bus)
        await bus.publish(ExecutionChanged(snapshot=ExecutionSnapshot("idle"), session_id=_SESSION))
        await _keeps(pilot, "inject-guard")
        # Only the manager's reports change what shows.
        await _snapshot(bus, session_bg, turn_bg)
        await _shows(pilot, "turn-bg")
        await _snapshot(bus, session_bg)
        await _shows(pilot, "session-bg")


# --- Coalescing, click, quit, hiding --------------------------------------------


async def test_snapshots_in_one_cycle_render_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bus = EventBus()
    app = _make_app(tmp_path, bus)
    async with app.run_test(size=(160, 40)) as pilot:
        main = await _ready(pilot)
        await wait_for(lambda: screen_is_settled(app, main), pilot=pilot, description="settled")
        indicator = main.query_one(HookIndicator)
        calls: list[tuple[HookExecutionView, ...]] = []
        original = indicator.set_executions

        def record(executions: tuple[HookExecutionView, ...]) -> None:
            calls.append(executions)
            original(executions)

        monkeypatch.setattr(indicator, "set_executions", record)
        for index in range(5):
            await _snapshot(bus, _view(f"hook-{index}", age=5))
        await _shows(pilot, "hook-4")
        await wait_for(lambda: screen_is_settled(app, main), pilot=pilot, description="settled after")
        assert len(calls) == 1, calls
        assert [view.hook_id for view in calls[0]] == ["hook-4"]


async def test_clicking_the_indicator_opens_the_hooks_tab(tmp_path: Path) -> None:
    bus = EventBus()
    app = _make_app(tmp_path, bus)
    async with app.run_test(size=(160, 40)) as pilot:
        main = await _ready(pilot)
        await _shows(pilot, "Hooks")
        indicator = main.query_one(HookIndicator)
        _start, end = _label_span(app)
        sidebar = main.query_one(SidebarPanel)
        if sidebar.is_visible:
            sidebar.toggle()
        await wait_for(lambda: not sidebar.is_visible, pilot=pilot, description="the sidebar closed")
        # Clicking the label (its last text cell) opens the sidebar on its Hooks tab.
        await click_when_settled(pilot, indicator, offset=(end - 1 - indicator.region.x, 0))
        tabs = sidebar.query_one(TabbedContent)
        await wait_for(
            lambda: sidebar.is_visible and tabs.active == SIDEBAR_HOOKS_TAB,
            pilot=pilot,
            description="the sidebar open on its Hooks tab",
        )
        assert app.screen is main


async def test_ctrl_q_still_quits(tmp_path: Path) -> None:
    bus = EventBus()
    app = _make_app(tmp_path, bus)
    async with app.run_test(size=(160, 40)) as pilot:
        await _ready(pilot)
        await pilot.press("ctrl+q")
        await wait_for(lambda: app.return_code is not None or not app.is_running, description="the app quitting")


async def test_shell_mode_hides_the_indicator_with_the_footer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from chrys.app.tui.terminal.panel import ShellPanel

    bus = EventBus()
    app = _make_app(tmp_path, bus)
    async with app.run_test(size=(160, 40)) as pilot:
        main = await _ready(pilot)
        await _shows(pilot, "Hooks")
        monkeypatch.setattr(main.query_one(ShellPanel), "_start_shell", lambda _cwd: None)
        main.shell_mode_state = True
        await wait_for(
            lambda: _indicator(app) == "" and "Hooks" not in _frame(app).splitlines()[-1],
            pilot=pilot,
            description="indicator hidden in shell mode",
        )
        main.shell_mode_state = False
        await _shows(pilot, "Hooks", what="back after shell mode")


async def test_workflow_mode_hides_the_indicator_until_the_chat_is_back(tmp_path: Path) -> None:
    """A workflow's hooks are not reported here: the chat's runs must not show over a workflow."""
    bus = EventBus()
    app = _make_app(tmp_path, bus, engine_type=_WorkflowModeEngine)
    async with app.run_test(size=(160, 40)) as pilot:
        main = await _ready(pilot)
        indicator = main.query_one(HookIndicator)
        await _snapshot(bus, _view("chat-hook", age=2))
        await _shows(pilot, "chat-hook")
        main._set_workflow_mode(True)
        await wait_for(
            lambda: main._workflow.workflow_mode and not indicator.display and _indicator(app) == "",
            pilot=pilot,
            description="the indicator hidden in workflow mode",
        )
        assert "chat-hook" not in _frame(app)
        main._set_workflow_mode(False)
        await _shows(pilot, "chat-hook", what="back with the chat")


# --- Performance ----------------------------------------------------------------


async def _populated(pilot: Pilot, main: MainScreen) -> None:
    panel = main.query_one(ChatPanel)
    transcript = []
    for index in range(24):
        transcript.extend(
            (
                UserMessageWidget(f"Question {index}\nwith a second line"),
                AgentMessage(f"## Answer {index}\n\nA paragraph with `code` and **emphasis**."),
            )
        )
    await panel.mount(*transcript)
    await wait_for(
        lambda: len(panel.walk_children()) >= 96 and all(md.source for md in panel.query(VirtualizedMarkdown)),
        pilot=pilot,
        description="populated transcript",
    )


async def test_indicator_updates_on_a_long_transcript_do_not_restyle_relayout_or_recompose(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bus = EventBus()
    app = _make_app(tmp_path, bus)
    async with app.run_test(size=(160, 40)) as pilot:
        main = await _ready(pilot)
        await _populated(pilot, main)
        await _start_turn(pilot, bus, main)
        # The turn's waiting row shows a moment after it starts; it is the transcript's, not the indicator's.
        await wait_for(
            lambda: main.query_one(WaitingRow).display and screen_is_settled(app, main),
            pilot=pilot,
            description="settled underlay",
        )
        seen: dict[str, list[object]] = {"styles": [], "layout": [], "footer": []}
        monkeypatch.setattr(main, "update_node_styles", lambda animate=True: seen["styles"].append(animate))
        monkeypatch.setattr(main, "_refresh_layout", lambda *_a, **_k: seen["layout"].append(None))

        async def footer_recompose() -> None:
            seen["footer"].append(None)

        monkeypatch.setattr(main.query_one(ChrysFooter), "recompose", footer_recompose)

        a = _view("hook-a", age=0.0)
        await _snapshot(bus, a)
        await _shows(pilot, "hook-a", timeout=4.0)
        assert time.monotonic() - a.started_at > 1.0
        b = _view("hook-b", queued=True, age=1.0)
        await _snapshot(bus, a, b)
        # Running first: the later queued run does not take over while A runs. Both are past
        # the threshold, so the ticks only repaint the order the snapshot set.
        await _keeps_through_ticks(pilot, "hook-a")
        await _snapshot(bus, _view("hook-b", age=1.0))
        await _shows(pilot, "hook-b", what="B once it got the slot")
        await _snapshot(bus, a)
        await _shows(pilot, "hook-a")
        await _snapshot(bus)
        await _shows(pilot, "Hooks")
        await wait_for(lambda: screen_is_settled(app, main), pilot=pilot, description="settled after")
        assert seen == {"styles": [], "layout": [], "footer": []}


async def test_interactive_mode_keeps_the_indicator_hidden_until_shell_mode_ends(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from chrys.app.tui.terminal.panel import ShellPanel
    from chrys.app.tui.terminal.widget import Terminal

    bus = EventBus()
    app = _make_app(tmp_path, bus)
    async with app.run_test(size=(160, 40)) as pilot:
        main = await _ready(pilot)
        await _snapshot(bus, _view("guard", age=5))
        await _shows(pilot, "guard")
        panel = main.query_one(ShellPanel)
        monkeypatch.setattr(panel, "_start_shell", lambda _cwd: None)
        main.shell_mode_state = True
        await wait_for(lambda: _indicator(app) == "", pilot=pilot, description="hidden in shell mode")
        await wait_for(lambda: main.query(Terminal), pilot=pilot, description="the shell's terminal")
        terminal = main.query_one(Terminal)
        # Each message reaches the screen on its next pump: 0.3 s is many pumps.
        terminal.post_message(Terminal.AlternateScreenChanged(terminal, True))
        assert not await wait_until(lambda: _indicator(app) != "", timeout=0.3, pilot=pilot)
        terminal.post_message(Terminal.AlternateScreenChanged(terminal, False))
        assert not await wait_until(lambda: _indicator(app) != "", timeout=0.3, pilot=pilot)
        main.shell_mode_state = False
        await _shows(pilot, "guard", what="back with the footer")


# --- Running first, and the hidden timer ----------------------------------------


async def test_a_running_run_outranks_a_later_queued_one(tmp_path: Path) -> None:
    bus = EventBus()
    app = _make_app(tmp_path, bus)
    async with app.run_test(size=(160, 40)) as pilot:
        main = await _ready(pilot)
        await _start_turn(pilot, bus, main)
        holder = _view("slot-holder", age=6)
        waiting = _view("waiting", queued=True, age=3)
        await _snapshot(bus, holder, waiting)
        await _shows(pilot, "slot-holder")
        await _keeps_through_ticks(pilot, "slot-holder")
        # The holder ends; the queued one gets the slot and runs.
        await _snapshot(bus, _view("waiting", age=3))
        await _shows(pilot, "waiting")
        # Only queued runs: the latest queued shows.
        await _snapshot(bus, _view("q-old", queued=True, age=5), _view("q-new", queued=True, age=2))
        await _shows(pilot, "q-new")


async def test_while_hidden_a_snapshot_does_not_repaint_and_the_run_shows_once_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, indicator_clock: _IndicatorClock
) -> None:
    from textual.widget import Widget

    from chrys.app.tui.terminal.panel import ShellPanel

    bus = EventBus()
    app = _make_app(tmp_path, bus)
    async with app.run_test(size=(160, 40)) as pilot:
        main = await _ready(pilot)
        indicator = main.query_one(HookIndicator)
        panel = main.query_one(ShellPanel)
        monkeypatch.setattr(panel, "_start_shell", lambda _cwd: None)
        main.shell_mode_state = True
        await wait_for(lambda: _indicator(app) == "", pilot=pilot, description="hidden in shell mode")
        await wait_for(lambda: screen_is_settled(app, main), pilot=pilot, description="settled")

        repaints: list[str] = []
        original_refresh = Widget.refresh

        def counting_refresh(self: Widget, *args: object, **kwargs: object) -> Widget:
            if self is indicator or indicator in self.ancestors:
                repaints.append(type(self).__name__)
            return original_refresh(self, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(Widget, "refresh", counting_refresh)
        # A young run crosses the 1 s threshold while the indicator is hidden.
        run = _view("hidden-run", age=0.0, now=indicator_clock.now)
        await _snapshot(bus, run)
        settled = len(repaints)
        await wait_for(lambda: indicator._executions == (run,), pilot=pilot, description="the snapshot applied")
        indicator_clock.now = run.started_at + 2.0
        # A timer left running would repaint every 0.1 s: about three times in the window.
        assert not await wait_until(lambda: len(repaints) > settled + 1, timeout=0.3, pilot=pilot), repaints
        assert indicator._timer is not None and not indicator._timer._active.is_set(), "the timer runs while hidden"
        hidden = len(repaints)

        main.shell_mode_state = False
        await _shows(pilot, "hidden-run", what="the run shown once the indicator is back")
        # Positive control: the same probe sees the indicator repaint once it is shown.
        assert len(repaints) > hidden


# --- Styling --------------------------------------------------------------------


def _cells(app: ChrysApp) -> list[tuple[str, object]]:
    """The indicator's cells on screen as (character, rich style), separator included."""
    indicator = app.screen.query_one(HookIndicator)
    region = indicator.region
    strip = app.screen._compositor.render_strips()[region.y]
    cells: list[tuple[str, object]] = []
    for segment in strip:
        for char in segment.text:
            cells.append((char, segment.style))
    return cells[region.x : region.right]


def _body(app: ChrysApp) -> str:
    """The indicator's text after its left separator: ``⠿ Hooks`` or ``<spinner> <id>``."""
    return re.sub(r"^[▏│┃|\s]+", "", _indicator(app))


def _word_color(app: ChrysApp, word: str) -> object:
    cells = _cells(app)
    text = "".join(char for char, _style in cells)
    start = text.index(word)
    styles = {cells[index][1].color for index in range(start, start + len(word))}  # type: ignore[attr-defined]
    assert len(styles) == 1, styles
    return styles.pop()


def _glyph_color(app: ChrysApp) -> object:
    cells = _cells(app)
    text = "".join(char for char, _style in cells)
    match = re.search(r"[⠿⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏]", text)
    assert match is not None, text
    return cells[match.start()][1].color  # type: ignore[attr-defined]


async def test_idle_reads_exactly_dot_grid_hooks_with_no_divider(tmp_path: Path) -> None:
    bus = EventBus()
    app = _make_app(tmp_path, bus)
    async with app.run_test(size=(160, 40)) as pilot:
        await _ready(pilot)
        await _shows(pilot, "Hooks")
        assert _body(app) == "⠿ Hooks"
        cells = "".join(char for char, _style in _cells(app))
        assert not any(char in cells for char in "▏│┃|"), cells
        assert _glyph_color(app) == _word_color(app, "Hooks"), "glyph and word share the muted color"


@pytest.mark.parametrize("level", ["full", "none"])
async def test_running_reads_spinner_space_id(tmp_path: Path, level: str) -> None:
    from chrys.app.tui.util.spinner import SPINNER_FRAMES

    bus = EventBus()
    app = _make_app(tmp_path, bus)
    async with app.run_test(size=(160, 40)) as pilot:
        await _ready(pilot)
        app.animation_level = level  # type: ignore[assignment]
        await _snapshot(bus, _view("slow-shell", age=2))
        await _shows(pilot, "slow-shell")
        # Animations off: the still full dot grid, as when idle but in the running color.
        glyphs = SPINNER_FRAMES if level == "full" else "⠿"
        assert re.fullmatch(rf"[{glyphs}] slow-shell", _body(app)), _body(app)
        assert _glyph_color(app) == _word_color(app, "slow-shell")


async def test_the_spinner_turns_while_a_run_is_shown(tmp_path: Path) -> None:
    from chrys.app.tui.util.spinner import SPINNER_FRAMES

    bus = EventBus()
    app = _make_app(tmp_path, bus)
    async with app.run_test(size=(160, 40)) as pilot:
        await _ready(pilot)
        app.animation_level = "full"
        await _snapshot(bus, _view("turning", age=2))
        await _shows(pilot, "turning")
        frames: set[str] = set()

        def collect() -> bool:
            body = _body(app)
            if body and body[0] in SPINNER_FRAMES:
                frames.add(body[0])
            return len(frames) >= 3

        await wait_for(collect, timeout=3.0, interval=0.03, pilot=pilot, description="three spinner frames")


async def test_idle_and_running_colors_differ_and_follow_the_theme(tmp_path: Path) -> None:
    bus = EventBus()
    app = _make_app(tmp_path, bus)
    async with app.run_test(size=(160, 40)) as pilot:
        await _ready(pilot)
        await _shows(pilot, "Hooks")
        idle = _word_color(app, "Hooks")
        await _snapshot(bus, _view("guard", age=2))
        await _shows(pilot, "guard")
        running = _word_color(app, "guard")
        assert idle != running

        first_theme = app.theme
        other = next(name for name in ("ansi-dark", "chrys", "textual-light") if name != first_theme)
        app.theme = other
        await wait_for(
            lambda: _word_color(app, "guard") != running, pilot=pilot, description="running color follows theme"
        )
        await _snapshot(bus)
        await _shows(pilot, "Hooks")
        await wait_for(lambda: _word_color(app, "Hooks") != idle, pilot=pilot, description="idle color follows theme")


async def test_the_indicator_stays_whole_when_footer_keys_overflow(tmp_path: Path) -> None:
    bus = EventBus()
    app = _make_app(tmp_path, bus)
    async with app.run_test(size=(100, 40)) as pilot:
        main = await _ready(pilot)
        await _shows(pilot, "Hooks")
        indicator = main.query_one(HookIndicator)
        assert indicator.region.width == len(" ⠿ Hooks ")
        assert indicator.region.right == app.size.width
        assert _body(app) == "⠿ Hooks"
        long_id = "x" * 40
        await _snapshot(bus, _view(long_id, age=2))
        await wait_for(lambda: "…" in _indicator(app), pilot=pilot, description="the long id cut")
        name = _shown_name(_indicator(app))
        assert cell_len(name) <= 26, name
        assert "x…x" in name, "cut in the middle"
        assert indicator.region.width == HOOK_INDICATOR_WIDTH
        assert indicator.region.right == app.size.width


async def test_the_footer_keys_keep_every_cell_the_label_does_not_take(tmp_path: Path) -> None:
    """At 120 columns the keys and the idle label fit side by side; a long id takes room only while it shows."""
    from textual.widgets._footer import FooterKey

    bus = EventBus()
    app = _make_app(tmp_path, bus)
    async with app.run_test(size=(120, 40)) as pilot:
        main = await _ready(pilot)
        await _shows(pilot, "Hooks")
        indicator = main.query_one(HookIndicator)
        footer = main.query_one(ChrysFooter)
        await wait_for(lambda: footer.query(FooterKey), pilot=pilot, description="the footer keys")
        keys = list(footer.query(FooterKey))

        def keys_whole() -> bool:
            return footer.region.right == indicator.region.x and all(
                key.region.width and key.region.right <= footer.region.right for key in keys
            )

        await wait_for(keys_whole, pilot=pilot, description="every key whole beside the idle label")
        # A box of the indicator's full width would leave the keys too little room.
        assert sum(key.region.width for key in keys) > app.size.width - HOOK_INDICATOR_WIDTH
        assert indicator.region.width == len(" ⠿ Hooks ")
        assert indicator.region.right == app.size.width

        await _snapshot(bus, _view("a-hook-whose-id-is-long-enough", age=2))
        await wait_for(lambda: "…" in _indicator(app), pilot=pilot, description="the long id shown")
        assert indicator.region.width == HOOK_INDICATOR_WIDTH
        assert footer.region.right == indicator.region.x
        await _snapshot(bus)
        await _shows(pilot, "Hooks")
        await wait_for(keys_whole, pilot=pilot, description="the keys whole again")

        # The whole box is the label: its first cell opens the hooks too.
        sidebar = main.query_one(SidebarPanel)
        await click_when_settled(pilot, indicator, offset=(0, 0))
        await wait_for(
            lambda: sidebar.is_visible and sidebar.query_one(TabbedContent).active == SIDEBAR_HOOKS_TAB,
            pilot=pilot,
            description="the sidebar open on its Hooks tab",
        )


async def test_the_label_fits_the_row_again_after_the_terminal_widens(tmp_path: Path) -> None:
    """The id cut to a narrow row gets its full width back once the terminal widens."""
    bus = EventBus()
    app = _make_app(tmp_path, bus)
    async with app.run_test(size=(20, 40)) as pilot:
        main = await _ready(pilot)
        indicator = main.query_one(HookIndicator)
        long_id = "left-part-of-the-hook-name-right-part"
        await _snapshot(bus, _view(long_id, age=2))
        await wait_for(lambda: "…" in _indicator(app), pilot=pilot, description="the id cut to the narrow row")
        assert indicator.region.width == 20
        await resize_when_settled(pilot, 160, 40)
        await wait_for(
            lambda: indicator.region.width == HOOK_INDICATOR_WIDTH,
            pilot=pilot,
            description="the label at its full width again",
        )
        name = _shown_name(_indicator(app))
        head, tail = name.split("…")
        assert cell_len(name) == HOOK_INDICATOR_WIDTH - 4, name
        assert long_id.startswith(head) and long_id.endswith(tail), name
        assert indicator.region.right == app.size.width


# --- Idle theme, timer gating, hover --------------------------------------------


def _glyph(app: ChrysApp) -> tuple[str, object]:
    cells = _cells(app)
    text = "".join(char for char, _style in cells)
    match = re.search(r"[⠿⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏]", text)
    assert match is not None, text
    char, style = cells[match.start()]
    return char, style.color  # type: ignore[attr-defined]


async def test_a_theme_switch_while_idle_recolors_at_once(tmp_path: Path) -> None:
    bus = EventBus()
    app = _make_app(tmp_path, bus)
    async with app.run_test(size=(160, 40)) as pilot:
        await _ready(pilot)
        await _shows(pilot, "Hooks")
        before = (_glyph(app)[1], _word_color(app, "Hooks"))
        other = next(name for name in ("ansi-dark", "chrys", "textual-light") if name != app.theme)
        app.theme = other
        # No snapshot is published: the theme change alone repaints the idle text.
        await wait_for(
            lambda: (_glyph(app)[1], _word_color(app, "Hooks")) != before,
            pilot=pilot,
            description="idle text recolored by the theme",
        )
        assert _body(app) == "⠿ Hooks"


async def test_only_unwaited_runs_keep_the_timer_off_and_a_waited_one_starts_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from textual.widget import Widget

    bus = EventBus()
    app = _make_app(tmp_path, bus)
    async with app.run_test(size=(160, 40)) as pilot:
        main = await _ready(pilot)
        app.animation_level = "full"
        indicator = main.query_one(HookIndicator)
        await wait_for(lambda: screen_is_settled(app, main), pilot=pilot, description="settled")
        repaints: list[str] = []
        original_refresh = Widget.refresh

        def counting_refresh(self: Widget, *args: object, **kwargs: object) -> Widget:
            if self is indicator or indicator in self.ancestors:
                repaints.append(type(self).__name__)
            return original_refresh(self, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(Widget, "refresh", counting_refresh)
        await _snapshot(
            bus,
            _view("ff", waited_at="none", age=0.0),
            _view("on-interrupt", waited_at="none", age=0.0),
        )
        after_snapshot = len(repaints)
        # A running timer would repaint every 0.1 s, about three times in the window; at most the
        # snapshot's own render may follow.
        assert not await wait_until(lambda: len(repaints) > after_snapshot + 1, timeout=0.3, pilot=pilot), repaints
        assert indicator._timer is not None and not indicator._timer._active.is_set(), "the timer runs"
        assert _body(app) == "⠿ Hooks"

        # Positive control: a waited-on run starts the timer, and the spinner repaints keep coming.
        await _snapshot(bus, _view("ff", waited_at="none", age=2), _view("guard", age=2))
        await _shows(pilot, "guard")
        started = len(repaints)
        await wait_for(lambda: len(repaints) >= started + 5, timeout=3.0, pilot=pilot, description="timer repaints")


def _label_span(app: ChrysApp) -> tuple[int, int]:
    """Screen columns ``[start, end)`` of the label: the text plus one cell on each side."""
    indicator = app.screen.query_one(HookIndicator)
    region = indicator.region
    cells = "".join(char for char, _style in _cells(app))
    first = len(cells) - len(cells.lstrip())
    last = len(cells.rstrip())
    return region.x + first - 1, region.x + last + 1


def _row_backgrounds(app: ChrysApp) -> list[object]:
    """The background color of every cell on the indicator's row, by screen column."""
    region = app.screen.query_one(HookIndicator).region
    strip = app.screen._compositor.render_strips()[region.y]
    colors: list[object] = []
    for segment in strip:
        colors.extend(segment.style.bgcolor if segment.style else None for _ in segment.text)
    return colors


def _highlighted(app: ChrysApp, hover: object) -> list[int]:
    """The screen columns inside the indicator's box painted in *hover*."""
    region = app.screen.query_one(HookIndicator).region
    colors = _row_backgrounds(app)
    return [x for x in range(region.x, min(region.right, len(colors))) if colors[x] == hover]


async def _footer_key_hover(pilot: Pilot, app: ChrysApp) -> object:
    """A footer key's settled hover background (footer keys still fade in over 150 ms)."""
    from textual.widgets._footer import FooterKey

    key = app.screen.query_one(ChrysFooter).query(FooterKey).first()
    await wait_for(lambda: key.region.width > 0, pilot=pilot, description="the footer key laid out")

    def key_backgrounds() -> frozenset[object]:
        return frozenset(_row_backgrounds(app)[key.region.x : key.region.right])

    plain = key_backgrounds()
    await pilot.hover(key)
    await wait_for(lambda: key.mouse_hover and key_backgrounds() != plain, pilot=pilot, description="key hovered")
    await wait_until_quiet(key_backgrounds, description="the key's hover fade", pumps=5, pilot=pilot)
    (color,) = key_backgrounds()
    return color


async def test_hover_paints_only_the_label_with_the_footer_key_color_and_a_pointer(tmp_path: Path) -> None:
    bus = EventBus()
    app = _make_app(tmp_path, bus)
    async with app.run_test(size=(160, 40)) as pilot:
        main = await _ready(pilot)
        await _shows(pilot, "Hooks")
        indicator = main.query_one(HookIndicator)
        hover = await _footer_key_hover(pilot, app)
        plain = set(_row_backgrounds(app)[indicator.region.x : indicator.region.right])
        assert hover not in plain

        start, end = _label_span(app)
        assert end - start == len("⠿ Hooks") + 2
        await pilot.hover(indicator, offset=(end - 1 - indicator.region.x, 0))
        await wait_for(
            lambda: _highlighted(app, hover) == list(range(start, end)),
            pilot=pilot,
            description="exactly the label highlighted",
        )
        assert app.screen._pointer_shape == "pointer"
        assert indicator.tooltip is None
        # No fade: the highlight is complete at once and stays put.
        await wait_until_quiet(lambda: tuple(_highlighted(app, hover)), description="steady highlight", pilot=pilot)
        assert _highlighted(app, hover) == list(range(start, end))


async def test_the_highlight_follows_the_label_while_the_mouse_stays(tmp_path: Path) -> None:
    bus = EventBus()
    app = _make_app(tmp_path, bus)
    async with app.run_test(size=(160, 40)) as pilot:
        main = await _ready(pilot)
        await _shows(pilot, "Hooks")
        indicator = main.query_one(HookIndicator)
        hover = await _footer_key_hover(pilot, app)
        _start, end = _label_span(app)
        # The mouse rests on the label's last text cell, which every label covers (right-aligned).
        await pilot.hover(indicator, offset=(end - 2 - indicator.region.x, 0))
        await wait_for(
            lambda: _highlighted(app, hover) == list(range(*_label_span(app))),
            pilot=pilot,
            description="the idle label highlighted",
        )
        idle_width = len(_highlighted(app, hover))

        hook_id = "a-much-longer-hook-name"
        await _snapshot(bus, _view(hook_id, age=2))
        await _shows(pilot, hook_id)
        await wait_for(
            lambda: _highlighted(app, hover) == list(range(*_label_span(app))),
            pilot=pilot,
            description="the longer label highlighted",
        )
        assert len(_highlighted(app, hover)) == len(hook_id) + 4 > idle_width
        assert app.screen._pointer_shape == "pointer"

        await _snapshot(bus)
        await _shows(pilot, "Hooks")
        await wait_for(
            lambda: (
                len(_highlighted(app, hover)) == idle_width
                and _highlighted(app, hover) == list(range(*_label_span(app)))
            ),
            pilot=pilot,
            description="back to the idle label",
        )


async def test_hovering_and_relabelling_do_not_restyle_relayout_or_recompose(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bus = EventBus()
    app = _make_app(tmp_path, bus)
    async with app.run_test(size=(160, 40)) as pilot:
        main = await _ready(pilot)
        await _populated(pilot, main)
        await _shows(pilot, "Hooks")
        indicator = main.query_one(HookIndicator)
        hover = await _footer_key_hover(pilot, app)
        await pilot.hover(main.query_one(ChatPanel))
        await wait_for(lambda: screen_is_settled(app, main), pilot=pilot, description="settled")
        seen: dict[str, list[object]] = {"styles": [], "layout": [], "footer": []}
        monkeypatch.setattr(main, "update_node_styles", lambda animate=True: seen["styles"].append(animate))
        monkeypatch.setattr(main, "_refresh_layout", lambda *_a, **_k: seen["layout"].append(None))

        async def footer_recompose() -> None:
            seen["footer"].append(None)

        monkeypatch.setattr(main.query_one(ChrysFooter), "recompose", footer_recompose)

        _start, end = _label_span(app)
        await pilot.hover(indicator, offset=(end - 2 - indicator.region.x, 0))
        await wait_for(lambda: _highlighted(app, hover), pilot=pilot, description="label hovered")
        await _snapshot(bus, _view("relabelled-while-hovered", age=2))
        await _shows(pilot, "relabelled-while-hovered")
        await pilot.hover(main.query_one(ChatPanel))
        await wait_for(lambda: not _highlighted(app, hover), pilot=pilot, description="off the label")
        await wait_for(lambda: screen_is_settled(app, main), pilot=pilot, description="settled after")
        assert seen == {"styles": [], "layout": [], "footer": []}


async def test_the_box_hugs_its_label_in_the_corner(tmp_path: Path) -> None:
    """The box is the label, padding included, in the screen's corner; idle and running, ids up to 26 cells."""
    bus = EventBus()
    app = _make_app(tmp_path, bus)
    async with app.run_test(size=(160, 40)) as pilot:
        main = await _ready(pilot)
        indicator = main.query_one(HookIndicator)

        def box() -> str:
            return "".join(char for char, _style in _cells(app))

        await _shows(pilot, "Hooks")
        assert box() == " ⠿ Hooks "
        assert indicator.region.right == app.size.width

        for hook_id in ("x", "guard", "slow-shell-blocking", "n" * 26):
            await _snapshot(bus, _view(hook_id, age=2))
            await _shows(pilot, hook_id)
            assert re.fullmatch(rf" \S {re.escape(hook_id)} ", box()), box()
            assert indicator.region.right == app.size.width
        # One cell more and the middle is cut.
        too_long = "m" * 13 + "-" + "n" * 13
        await _snapshot(bus, _view(too_long, age=2))
        await wait_for(lambda: "…" in _indicator(app), pilot=pilot, description="a 27-cell id cut")
        name = _shown_name(_indicator(app))
        assert cell_len(name) == 26 and name.startswith("m") and name.endswith("n"), name
        assert indicator.region.width == HOOK_INDICATOR_WIDTH
        await _snapshot(bus)
        await _shows(pilot, "Hooks")
        assert box() == " ⠿ Hooks "


# --- Real hooks behind an unwaited slot holder ----------------------------------


@pytest.mark.usefixtures("aged_indicator_clock")
@with_wait_deadline(ENGINE_TEST_WAIT_TIMEOUT)
async def test_a_blocking_hook_queued_behind_a_fire_and_forget_shows_itself_not_the_holder(tmp_path: Path) -> None:
    """Real hook subprocesses on the app's bus, ``max_parallel_hooks=1``: F holds the slot, B queues behind it."""
    import asyncio
    import sys
    import textwrap

    from chrys.orchestration.engine.hook_reporting import wire_hook_reporting
    from chrys.service.hooks.events import HookEvent
    from chrys.service.hooks.manager import HookManager
    from chrys.service.hooks.schema import HookConfig, HookExecution, HookMatch, HookRun, HookSettings, HooksFile

    def held(release: Path) -> list[str]:
        script = textwrap.dedent(
            f"""
            import os, time
            while not os.path.exists({str(release)!r}):
                time.sleep(0.01)
            """
        )
        return [sys.executable, "-c", script]

    release_f, release_b = tmp_path / "release-f", tmp_path / "release-b"
    hooks_dir = tmp_path / "hooks"
    hooks_dir.mkdir()
    manager = HookManager(
        file=HooksFile(
            settings=HookSettings(max_parallel_hooks=1),
            hooks=[
                HookConfig(
                    id="observer-ff",
                    event=HookEvent.AFTER_TOOL_CALL,
                    run=HookRun(type="command", argv=held(release_f)),
                    execution=HookExecution(mode="fire_and_forget", timeout_seconds=30),
                ),
                HookConfig(
                    id="guard",
                    event=HookEvent.BEFORE_TOOL_CALL,
                    run=HookRun(type="command", argv=held(release_b)),
                    execution=HookExecution(mode="blocking", timeout_seconds=30),
                    match=HookMatch(tool_name="shell"),
                ),
            ],
        ),
        hooks_dir=hooks_dir,
    )
    payload = {"tool": {"name": "shell", "kind": "shell", "call_id": "c1", "args": {}}}
    bus = EventBus()
    app = _make_app(tmp_path, bus)
    fire_b = None
    async with app.run_test(size=(160, 40)) as pilot:
        await _ready(pilot)
        wire_hook_reporting(manager, bus, session_id=_SESSION)
        try:
            await manager.fire(HookEvent.AFTER_TOOL_CALL, payload)
            await wait_for(
                lambda: any(v.hook_id == "observer-ff" and not v.queued for v in manager.executions()),
                timeout=ENGINE_TURN_TIMEOUT,
                pilot=pilot,
                description="the fire-and-forget run holding the slot",
            )
            # Alone, the holder nobody waits for never shows. It is past the display threshold as
            # soon as it is reported (the aged clock), so the report's render already decides.
            await _keeps(pilot, "Hooks")

            fire_b = asyncio.create_task(manager.fire(HookEvent.BEFORE_TOOL_CALL, payload))
            await wait_for(
                lambda: any(v.hook_id == "guard" and v.queued for v in manager.executions()),
                timeout=ENGINE_TURN_TIMEOUT,
                pilot=pilot,
                description="the blocking run queued behind it",
            )
            await _shows(pilot, "guard", timeout=4.0, what="the queued waited-on run, not its holder")
            assert "observer-ff" not in _indicator(app)

            release_f.touch()
            await wait_for(
                lambda: any(v.hook_id == "guard" and not v.queued for v in manager.executions()),
                timeout=ENGINE_TURN_TIMEOUT,
                pilot=pilot,
                description="the blocking run took the slot",
            )
            await _keeps(pilot, "guard")
            release_b.touch()
            await wait_for(
                fire_b.done, timeout=ENGINE_TURN_TIMEOUT, pilot=pilot, description="the blocking fire returned"
            )
            await fire_b
            await _shows(pilot, "Hooks")
        finally:
            release_f.touch()
            release_b.touch()
            if fire_b is not None:
                await asyncio.gather(fire_b, return_exceptions=True)
            await manager.drain_session()


@pytest.mark.parametrize("columns", [30, 24, 12])
async def test_on_a_terminal_narrower_than_the_box_the_indicator_fits_and_cuts_to_its_width(
    tmp_path: Path, columns: int
) -> None:
    """Below 30 columns the id is cut in the middle to what the row leaves, the box never past the row."""
    bus = EventBus()
    app = _make_app(tmp_path, bus)
    async with app.run_test(size=(columns, 40)) as pilot:
        main = await _ready(pilot)
        indicator = main.query_one(HookIndicator)
        await _shows(pilot, "Hooks")
        assert "".join(char for char, _style in _cells(app)) == " ⠿ Hooks "

        long_id = "left-part-of-the-hook-name-right-part"
        await _snapshot(bus, _view(long_id, age=2))
        await wait_for(lambda: "…" in _indicator(app), pilot=pilot, description="the id cut to the narrow width")
        body = "".join(char for char, _style in _cells(app)).strip()
        assert re.match(r"\S ", body), body
        name = body[2:]
        head, tail = name.split("…")
        assert head and tail and long_id.startswith(head) and long_id.endswith(tail), name
        assert cell_len(body) <= columns
        # The whole box, padding included, is inside the screen: nothing of it is clipped.
        assert indicator.region.x >= 0 and indicator.region.right <= columns, indicator.region


# --- The timer under and after a modal -----------------------------------------


async def _open_runtime_details(pilot: Pilot, app: ChrysApp) -> None:
    main = app.screen
    assert isinstance(main, MainScreen)
    main.action_runtime_details()
    await wait_for(
        lambda: isinstance(app.screen, RuntimeDetailsDialog) and app.screen.is_mounted,
        pilot=pilot,
        description="the runtime details dialog open",
    )


async def _close_modal(pilot: Pilot, app: ChrysApp) -> None:
    await pilot.press("escape")
    await wait_for(
        lambda: app._main_screen is not None and app.screen is app._main_screen,
        pilot=pilot,
        description="back on the main screen",
    )


async def _spinner_turns(pilot: Pilot, app: ChrysApp) -> None:
    from chrys.app.tui.util.spinner import SPINNER_FRAMES

    frames: set[str] = set()

    def collect() -> bool:
        body = _body(app)
        if body and body[0] in SPINNER_FRAMES:
            frames.add(body[0])
        return len(frames) >= 3

    await wait_for(collect, timeout=3.0, interval=0.03, pilot=pilot, description="the spinner turning")


async def test_a_snapshot_received_under_a_modal_shows_and_turns_after_it_closes(tmp_path: Path) -> None:
    bus = EventBus()
    app = _make_app(tmp_path, bus)
    async with app.run_test(size=(160, 40)) as pilot:
        await _ready(pilot)
        app.animation_level = "full"
        await _shows(pilot, "Hooks")
        await _open_runtime_details(pilot, app)
        # A young waited-on run arrives while the modal covers MainScreen.
        await _snapshot(bus, _view("under-modal", age=0.0))
        await _close_modal(pilot, app)
        await _shows(pilot, "under-modal", timeout=4.0, what="shown once it reached 1 s")
        await _spinner_turns(pilot, app)


async def test_a_spinner_showing_before_a_modal_does_not_freeze_after_it(tmp_path: Path) -> None:
    bus = EventBus()
    app = _make_app(tmp_path, bus)
    async with app.run_test(size=(160, 40)) as pilot:
        await _ready(pilot)
        app.animation_level = "full"
        first = _view("first", age=2)
        await _snapshot(bus, first)
        await _shows(pilot, "first")
        await _spinner_turns(pilot, app)
        await _open_runtime_details(pilot, app)
        await _snapshot(bus, first, _view("second", age=0.0))
        await _close_modal(pilot, app)
        await _spinner_turns(pilot, app)
        await _shows(pilot, "second", timeout=4.0, what="the newer run once it reached 1 s")
        await _spinner_turns(pilot, app)


async def test_the_spinner_stops_turning_while_a_modal_covers_it(tmp_path: Path) -> None:
    bus = EventBus()
    app = _make_app(tmp_path, bus)
    async with app.run_test(size=(160, 40)) as pilot:
        main = await _ready(pilot)
        app.animation_level = "full"
        indicator = main.query_one(HookIndicator)
        timer = indicator._timer
        assert timer is not None
        await _snapshot(bus, _view("covered", age=2))
        await _shows(pilot, "covered")
        await _spinner_turns(pilot, app)
        assert timer._active.is_set()
        await _open_runtime_details(pilot, app)
        # Nothing sends the covered widget a Hide: its own tick parks the timer.
        await wait_for(lambda: not timer._active.is_set(), pilot=pilot, description="the timer parked under the modal")
        frame = indicator._frame
        assert not await wait_until(lambda: indicator._frame != frame, timeout=0.3, pilot=pilot), (
            "the spinner kept turning under the modal"
        )
        await _close_modal(pilot, app)
        assert timer._active.is_set()
        await _spinner_turns(pilot, app)


# --- Hover across a dialog or a hidden footer row -------------------------------


async def _hover_label(pilot: Pilot, app: ChrysApp) -> tuple[object, list[int]]:
    """Hover the label's last text cell; return the hover color and the highlighted columns."""
    hover = await _footer_key_hover(pilot, app)
    indicator = app.screen.query_one(HookIndicator)
    _start, end = _label_span(app)
    await pilot.hover(indicator, offset=(end - 2 - indicator.region.x, 0))
    await wait_for(
        lambda: _highlighted(app, hover) == list(range(*_label_span(app))),
        pilot=pilot,
        description="the label highlighted",
    )
    assert app.screen._pointer_shape == "pointer"
    return hover, _highlighted(app, hover)


async def test_a_mouse_that_left_under_a_dialog_drops_the_highlight_when_it_closes(tmp_path: Path) -> None:
    bus = EventBus()
    app = _make_app(tmp_path, bus)
    async with app.run_test(size=(160, 40)) as pilot:
        await _ready(pilot)
        await _shows(pilot, "Hooks")
        hover, _label = await _hover_label(pilot, app)
        await _open_runtime_details(pilot, app)
        # The mouse moves to the dialog, far from the bottom-right corner, while it covers MainScreen.
        dialog = app.screen
        await pilot.hover(dialog, offset=(2, 2))
        await wait_for(lambda: app.mouse_position == (2, 2), pilot=pilot, description="the mouse moved away")
        await pilot.press("escape")
        await wait_for(
            lambda: app._main_screen is not None and app.screen is app._main_screen,
            pilot=pilot,
            description="back on the main screen",
        )
        await wait_for(lambda: not _highlighted(app, hover), pilot=pilot, description="the highlight dropped")
        await wait_for(lambda: app.screen._pointer_shape == "default", pilot=pilot, description="the default pointer")


async def test_a_mouse_that_stayed_on_the_label_keeps_the_highlight_after_a_dialog(tmp_path: Path) -> None:
    bus = EventBus()
    app = _make_app(tmp_path, bus)
    async with app.run_test(size=(160, 40)) as pilot:
        await _ready(pilot)
        await _shows(pilot, "Hooks")
        hover, label = await _hover_label(pilot, app)
        # Opened from the keyboard: the mouse stays where it is, on the label.
        await _open_runtime_details(pilot, app)
        await pilot.press("escape")
        await wait_for(
            lambda: app._main_screen is not None and app.screen is app._main_screen,
            pilot=pilot,
            description="back on the main screen",
        )
        await wait_for(lambda: _highlighted(app, hover) == label, pilot=pilot, description="still highlighted")
        # A Leave queued while the dialog covered the label lands on the next pumps: 0.3 s is many.
        assert not await wait_until(lambda: _highlighted(app, hover) != label, timeout=0.3, pilot=pilot)


async def test_hiding_the_footer_row_drops_the_hover(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from chrys.app.tui.terminal.panel import ShellPanel

    bus = EventBus()
    app = _make_app(tmp_path, bus)
    async with app.run_test(size=(160, 40)) as pilot:
        main = await _ready(pilot)
        await _shows(pilot, "Hooks")
        hover, _label = await _hover_label(pilot, app)
        monkeypatch.setattr(main.query_one(ShellPanel), "_start_shell", lambda _cwd: None)
        main.shell_mode_state = True
        await wait_for(lambda: _indicator(app) == "", pilot=pilot, description="hidden in shell mode")
        # The mouse moves elsewhere while the indicator is hidden.
        await pilot.hover(main, offset=(2, 2))
        main.shell_mode_state = False
        await _shows(pilot, "Hooks")
        # Shown again, the label paints on its Show: a stale hover would be back at once.
        assert not await wait_until(lambda: _highlighted(app, hover), timeout=0.3, pilot=pilot), _highlighted(
            app, hover
        )
