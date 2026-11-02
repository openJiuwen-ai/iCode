# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The sidebar's Hooks tab in the real app: fed by the engine's run log and ``HookRunsChanged`` (plan 2, 3, 5.2, 6).

Where the log matters, real hook subprocesses run through a real manager wired
to the app's bus the way the engine wires it; a held hook polls for a release
file the test creates.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import re
import sys
import textwrap
import time
from pathlib import Path

import pytest
from textual.geometry import Region
from textual.pilot import Pilot
from textual.screen import ModalScreen
from textual.widgets import Static, TabbedContent

from chrys.app.tui.app import ChrysApp
from chrys.app.tui.screens.dialogs.hook_run import HookRunModal
from chrys.app.tui.screens.main.screen import MainScreen
from chrys.app.tui.widgets.chat.messages import AgentMessage
from chrys.app.tui.widgets.chat.messages import UserMessage as UserMessageWidget
from chrys.app.tui.widgets.chat.panel import ChatPanel
from chrys.app.tui.widgets.chrome.footer import ChrysFooter
from chrys.app.tui.widgets.chrome.hook_indicator import HookIndicator
from chrys.app.tui.widgets.markdown import VirtualizedMarkdown
from chrys.app.tui.widgets.sidebar.hooks import HookRunList
from chrys.app.tui.widgets.sidebar.panel import SIDEBAR_HOOKS_TAB, SidebarPanel
from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import (
    AgentRuntimeDetails,
    AgentRuntimeUpdated,
    HookRunsChanged,
    ProfileSwitched,
    RuntimeHookDetails,
    RuntimeHookSourceDetails,
    SessionReady,
)
from chrys.foundation.models.hook_runs import HookRunDetail, HookRunRecord
from chrys.orchestration.engine.hook_reporting import wire_hook_reporting
from chrys.service.hooks.events import HookEvent
from chrys.service.hooks.manager import HookManager
from chrys.service.hooks.run_log import HookRunLog
from chrys.service.hooks.schema import HookConfig, HookExecution, HookMatch, HookRun, HooksFile
from chrys.service.profiles.agents.schema import AgentProfile
from chrys.service.trajectory.hooks import HookOutcome
from tests.support.pilot_barrier import screen_is_settled
from tests.support.tui_app_harness import SessionGenerationEngine, make_chrys_app
from tests.support.tui_helpers import click_when_settled
from tests.support.waiting import ENGINE_TURN_TIMEOUT, wait_for, wait_until

_SESSION = "test-session"
_PROFILE = AgentProfile(name="Code", display_name="Code", description="Test profile")


class _Registry:
    def list_profiles(self) -> list[AgentProfile]:
        return [_PROFILE]

    def load_all(self) -> None:
        return

    def get(self, name: str) -> AgentProfile | None:
        return _PROFILE if name == _PROFILE.name else None


def _runtime(hooks: int) -> AgentRuntimeDetails:
    return AgentRuntimeDetails(
        hook_sources=[
            RuntimeHookSourceDetails(
                scope="global",
                source_path="/config/hooks/hooks.yaml",
                hooks=[RuntimeHookDetails(id=f"h{n}", event="after_turn") for n in range(hooks)],
            )
        ]
        if hooks
        else []
    )


class _Engine(SessionGenerationEngine):
    def __init__(self, bus: EventBus, cwd: Path, hook_runs: object, *, configured: int = 2) -> None:
        self._bus = bus
        self._cwd = cwd
        self.hook_runs = hook_runs
        self._configured = configured
        self.turn_lifecycle_task = None

    async def start(self, _profile: AgentProfile) -> None:
        await self._bus.publish(
            SessionReady(
                agent_profile=_PROFILE.name,
                display_name=_PROFILE.display_name,
                session_id=_SESSION,
                primary_cwd=str(self._cwd),
                runtime_details=_runtime(self._configured),
            )
        )


def _make_app(tmp_path: Path, bus: EventBus, hook_runs: object, *, configured: int = 2) -> ChrysApp:
    workspace = tmp_path / "work"
    workspace.mkdir(exist_ok=True)
    return make_chrys_app(
        tmp_path / "state",
        engine=_Engine(bus, workspace, hook_runs, configured=configured),
        event_bus=bus,
        agent_registry=_Registry(),
    )


async def _ready(pilot: Pilot) -> MainScreen:
    app = pilot.app
    assert isinstance(app, ChrysApp)

    def ready() -> bool:
        screen = app._main_screen
        return screen is not None and app.screen is screen and not screen._state.run.agent_loading

    await wait_for(ready, pilot=pilot, description="session ready")
    main = app._main_screen
    assert main is not None
    return main


async def _show_hooks_tab(pilot: Pilot, main: MainScreen) -> HookRunList:
    sidebar = main.query_one(SidebarPanel)
    if not sidebar.is_visible:
        sidebar.toggle()
    sidebar.focus_tab(SIDEBAR_HOOKS_TAB)
    hooks = sidebar.hooks_panel
    await wait_for(lambda: hooks.is_on_screen and hooks.size.width > 0, pilot=pilot, description="Hooks tab shown")
    # Showing the tab scrolls the tab strip to it on a timer, which lays the screen out once more:
    # a spy armed before it stops would count that.
    strip = sidebar.query_one("#tabs-scroll")
    await wait_for(
        lambda: not main.app.animator.is_being_animated(strip, "scroll_x"), pilot=pilot, description="tab strip still"
    )
    return hooks


def _rows(main: MainScreen) -> list[str]:
    rows = main.query_one("#hook-runs-rows", Static)
    width, height = rows.size
    if not width or not height:
        return []
    lines = [strip.text.rstrip() for strip in rows.render_lines(Region(0, 0, width, height)) if strip.text.strip()]
    # From the icon on: drop the run number before it.
    return [re.sub(r"^\s*\d+ ", "", line) for line in lines]


def _is_running(row: str) -> bool:
    """A running row starts with a still dot, as a running background task."""
    return row.startswith("●")


def _empty(main: MainScreen) -> str:
    empty = main.query_one("#hook-runs-empty", Static)
    return str(empty.render()) if empty.display else ""


def _held(release: Path) -> list[str]:
    script = textwrap.dedent(
        f"""
        import os, time
        while not os.path.exists({str(release)!r}):
            time.sleep(0.01)
        """
    )
    return [sys.executable, "-c", script]


def _manager(tmp_path: Path, *hooks: HookConfig) -> HookManager:
    hooks_dir = tmp_path / "hooks"
    hooks_dir.mkdir(exist_ok=True)
    return HookManager(file=HooksFile(hooks=list(hooks)), hooks_dir=hooks_dir)


def _hook(hook_id: str, event: HookEvent, argv: list[str], *, tool_name: str | None = None) -> HookConfig:
    return HookConfig(
        id=hook_id,
        event=event,
        run=HookRun(type="command", argv=argv),
        execution=HookExecution(mode="blocking", timeout_seconds=30.0),
        match=HookMatch(tool_name=tool_name),
    )


def _payload(name: str = "zsh") -> dict[str, object]:
    return {"tool": {"name": name, "kind": "shell", "call_id": "c1", "args": {"command": "ls"}}}


# --- Fed by real runs -----------------------------------------------------------


async def test_real_runs_appear_running_then_ended(tmp_path: Path) -> None:
    release = tmp_path / "release"
    manager = _manager(tmp_path, _hook("slow-shell", HookEvent.BEFORE_TOOL_CALL, _held(release)))
    bus, log = EventBus(), HookRunLog()
    app = _make_app(tmp_path, bus, log)
    async with app.run_test(size=(160, 40)) as pilot:
        main = await _ready(pilot)
        await _show_hooks_tab(pilot, main)
        await wait_for(lambda: _empty(main) == "No hook has run since iCode started", pilot=pilot, description="empty")
        wire_hook_reporting(manager, bus, session_id=_SESSION, run_log=log)
        fire = asyncio.create_task(manager.fire(HookEvent.BEFORE_TOOL_CALL, _payload()))
        try:
            await wait_for(
                lambda: bool(_rows(main)) and _is_running(_rows(main)[0]),
                timeout=ENGINE_TURN_TIMEOUT,
                pilot=pilot,
                description="the running row",
            )
            assert _rows(main)[0].split()[1] == "slow-shell"
            title = str(main.query_one("#hook-runs-title", Static).render())
            assert title == "Hooks (1 active)"
        finally:
            release.touch()
        await asyncio.wait_for(fire, ENGINE_TURN_TIMEOUT)
        await wait_for(
            lambda: bool(_rows(main)) and _rows(main)[0].startswith("✓"),
            pilot=pilot,
            description="the ended row",
        )
        assert str(main.query_one("#hook-runs-title", Static).render()) == "Hooks"
        await manager.drain_session()


async def test_a_cancelled_run_shows_cancelled_without_waiting_for_idle(tmp_path: Path) -> None:
    release = tmp_path / "release"
    manager = _manager(tmp_path, _hook("gate", HookEvent.BEFORE_TOOL_CALL, _held(release)))
    bus, log = EventBus(), HookRunLog()
    app = _make_app(tmp_path, bus, log)
    async with app.run_test(size=(160, 40)) as pilot:
        main = await _ready(pilot)
        await _show_hooks_tab(pilot, main)
        wire_hook_reporting(manager, bus, session_id=_SESSION, run_log=log)
        fire = asyncio.create_task(manager.fire(HookEvent.BEFORE_TOOL_CALL, _payload()))
        try:
            await wait_for(
                lambda: bool(_rows(main)) and _is_running(_rows(main)[0]),
                timeout=ENGINE_TURN_TIMEOUT,
                pilot=pilot,
                description="running",
            )
            # What an interrupt does to the turn's task: no idle event follows here.
            fire.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await fire
            await wait_for(
                lambda: bool(_rows(main)) and _rows(main)[0].startswith("■"),
                pilot=pilot,
                description="shown cancelled",
            )
        finally:
            release.touch()
        await manager.drain_session()


async def test_clicking_a_row_opens_its_details_with_the_payload_the_hook_received(tmp_path: Path) -> None:
    received = tmp_path / "received.json"
    copy = textwrap.dedent(
        f"""
        import os, shutil
        shutil.copyfile(os.environ["CHRYS_HOOK_PAYLOAD_FILE"], {str(received)!r})
        print("guarded")
        """
    )
    manager = _manager(tmp_path, _hook("guard", HookEvent.BEFORE_TOOL_CALL, [sys.executable, "-c", copy]))
    bus, log = EventBus(), HookRunLog()
    app = _make_app(tmp_path, bus, log)
    async with app.run_test(size=(160, 40)) as pilot:
        main = await _ready(pilot)
        await _show_hooks_tab(pilot, main)
        wire_hook_reporting(manager, bus, session_id=_SESSION, run_log=log)
        await manager.fire(HookEvent.BEFORE_TOOL_CALL, _payload())
        await wait_for(lambda: len(_rows(main)) == 1, pilot=pilot, description="one row")
        await click_when_settled(pilot, main.query_one("#hook-runs-rows", Static), offset=(4, 0))
        await wait_for(
            lambda: isinstance(app.screen, HookRunModal) and app.screen.is_mounted,
            pilot=pilot,
            description="the details open",
        )
        modal = app.screen
        assert isinstance(modal, HookRunModal)
        assert str(modal.query_one("#hook-run-container").border_title) == "Hook #1 · guard"
        (record,) = log.records()
        detail = log.detail(record.run_id)
        assert detail is not None and detail.io is not None
        assert detail.io.payload == received.read_text(encoding="utf-8")
        await manager.drain_session()


async def test_runs_of_every_session_stay_in_one_timeline(tmp_path: Path) -> None:
    bus, log = EventBus(), HookRunLog()
    first = _manager(tmp_path, _hook("first-lint", HookEvent.AFTER_TURN, [sys.executable, "-c", "pass"]))
    second = _manager(tmp_path, _hook("second-lint", HookEvent.AFTER_TURN, [sys.executable, "-c", "pass"]))
    app = _make_app(tmp_path, bus, log)
    async with app.run_test(size=(160, 40)) as pilot:
        main = await _ready(pilot)
        await _show_hooks_tab(pilot, main)
        wire_hook_reporting(first, bus, session_id=_SESSION, run_log=log)
        await first.fire(HookEvent.AFTER_TURN, {"turn": 1})
        await wait_for(lambda: len(_rows(main)) == 1, pilot=pilot, description="first session's run")
        # A switch to another session clears nothing.
        await bus.publish(SessionReady(session_id="other", agent_profile="Code", runtime_details=_runtime(1)))
        wire_hook_reporting(second, bus, session_id="other", run_log=log)
        await second.fire(HookEvent.AFTER_TURN, {"turn": 1})
        await wait_for(lambda: len(_rows(main)) == 2, pilot=pilot, description="both sessions' runs")
        assert "first-lint" in _rows(main)[0] and "second-lint" in _rows(main)[1]


async def test_no_configured_hook_shows_no_hooks_configured(tmp_path: Path) -> None:
    bus = EventBus()
    app = _make_app(tmp_path, bus, HookRunLog(), configured=0)
    async with app.run_test(size=(160, 40)) as pilot:
        main = await _ready(pilot)
        await _show_hooks_tab(pilot, main)
        await wait_for(lambda: _empty(main) == "No hooks configured", pilot=pilot, description="unconfigured")


def _runtime_changed(kind: str, details: AgentRuntimeDetails) -> AgentRuntimeUpdated | ProfileSwitched:
    if kind == "runtime-updated":
        return AgentRuntimeUpdated(session_id=_SESSION, runtime_details=details)
    # A settings reload: the same profile, rebuilt.
    return ProfileSwitched(session_id=_SESSION, from_profile="Code", to_profile="Code", runtime_details=details)


@pytest.mark.parametrize("kind", ["runtime-updated", "profile-reloaded"])
async def test_the_empty_state_follows_the_hooks_the_runtime_details_name(tmp_path: Path, kind: str) -> None:
    bus = EventBus()
    app = _make_app(tmp_path, bus, HookRunLog(), configured=0)
    async with app.run_test(size=(160, 40)) as pilot:
        main = await _ready(pilot)
        await _show_hooks_tab(pilot, main)
        await wait_for(lambda: _empty(main) == "No hooks configured", pilot=pilot, description="unconfigured")
        # A hook was configured since, and nothing has run.
        await bus.publish(_runtime_changed(kind, _runtime(1)), raise_handler_errors=True)
        await wait_for(
            lambda: _empty(main) == "No hook has run since iCode started", pilot=pilot, description="configured"
        )


# --- Entry and workflow mode (section 2) ----------------------------------------


async def test_clicking_the_indicator_opens_a_closed_sidebar_on_the_hooks_tab(tmp_path: Path) -> None:
    bus = EventBus()
    app = _make_app(tmp_path, bus, HookRunLog())
    async with app.run_test(size=(160, 40)) as pilot:
        main = await _ready(pilot)
        sidebar = main.query_one(SidebarPanel)
        sidebar.focus_tab("tab-tasks")
        if sidebar.is_visible:
            sidebar.toggle()
        await wait_for(lambda: not sidebar.is_visible, pilot=pilot, description="closed")
        indicator = main.query_one(HookIndicator)
        await wait_for(lambda: indicator.region.width > 2, pilot=pilot, description="indicator laid out")
        await click_when_settled(pilot, indicator, offset=(indicator.region.width - 3, 0))
        tabs = sidebar.query_one(TabbedContent)
        await wait_for(
            lambda: sidebar.is_visible and tabs.active == SIDEBAR_HOOKS_TAB,
            pilot=pilot,
            description="open on Hooks",
        )


async def test_clicking_the_idle_indicator_while_the_sidebar_is_held_closed_shows_the_configured_hooks(
    tmp_path: Path,
) -> None:
    from chrys.app.tui.screens.dialogs.runtime_details import RUNTIME_DETAILS_HOOKS_TAB, RuntimeDetailsDialog

    bus = EventBus()
    app = _make_app(tmp_path, bus, HookRunLog())
    async with app.run_test(size=(160, 40)) as pilot:
        main = await _ready(pilot)
        sidebar = main.query_one(SidebarPanel)
        # As the theme editor holds it while it is open in a narrow terminal: no toggle shows it.
        sidebar.suppress_visibility("theme-editor", True)
        await wait_for(lambda: not sidebar.is_visible, pilot=pilot, description="held closed")
        main.post_message(HookIndicator.Clicked(""))
        await wait_for(
            lambda: isinstance(app.screen, RuntimeDetailsDialog) and app.screen.is_mounted,
            pilot=pilot,
            description="the configured hooks",
        )
        assert app.screen.query_one("#runtime-details-tabs", TabbedContent).active == RUNTIME_DETAILS_HOOKS_TAB
        assert not sidebar.is_visible


async def test_clicking_the_indicator_while_it_shows_a_run_opens_that_runs_details(tmp_path: Path) -> None:
    release = tmp_path / "release"
    manager = _manager(
        tmp_path,
        _hook("quick", HookEvent.BEFORE_TOOL_CALL, [sys.executable, "-c", "pass"]),
        _hook("slow-gate", HookEvent.BEFORE_TOOL_CALL, _held(release)),
    )
    bus, log = EventBus(), HookRunLog()
    app = _make_app(tmp_path, bus, log)
    async with app.run_test(size=(160, 40)) as pilot:
        main = await _ready(pilot)
        sidebar = main.query_one(SidebarPanel)
        sidebar.focus_tab("tab-tasks")
        if sidebar.is_visible:
            sidebar.toggle()
        await wait_for(lambda: not sidebar.is_visible, pilot=pilot, description="closed")
        wire_hook_reporting(manager, bus, session_id=_SESSION, run_log=log)
        fire = asyncio.create_task(manager.fire(HookEvent.BEFORE_TOOL_CALL, _payload()))
        try:
            indicator = main.query_one(HookIndicator)
            # The indicator names a waited-on run once it has lasted a second.
            await wait_for(
                lambda: "slow-gate" in str(indicator.render()) or fire.done(),
                timeout=ENGINE_TURN_TIMEOUT,
                pilot=pilot,
                description="the indicator shows the running hook",
            )
            assert not fire.done()
            await click_when_settled(pilot, indicator, offset=(indicator.region.width - 3, 0))
            await wait_for(
                lambda: isinstance(app.screen, HookRunModal) and app.screen.is_mounted,
                pilot=pilot,
                description="that run's details open",
            )
            modal = app.screen
            assert isinstance(modal, HookRunModal)
            assert str(modal.query_one("#hook-run-container").border_title) == "Hook #2 · slow-gate"
            # Still running: it opens on its input.
            assert modal.query_one(TabbedContent).active == "hook-run-input"
            # Behind them, the list they belong to: closing the details lands on it.
            assert sidebar.is_visible
            assert sidebar.query_one(TabbedContent).active == SIDEBAR_HOOKS_TAB
        finally:
            release.touch()
        await asyncio.wait_for(fire, ENGINE_TURN_TIMEOUT)
        await manager.drain_session()


async def test_workflow_mode_hides_the_hooks_tab() -> None:
    from textual.app import App, ComposeResult

    class _SidebarApp(App):
        def compose(self) -> ComposeResult:
            yield SidebarPanel()

    async with _SidebarApp().run_test(size=(80, 30)) as pilot:
        sidebar = pilot.app.query_one(SidebarPanel)
        tabs = sidebar.query_one(TabbedContent)
        sidebar.focus_tab(SIDEBAR_HOOKS_TAB)
        await wait_for(lambda: tabs.active == SIDEBAR_HOOKS_TAB, pilot=pilot, description="on Hooks")
        sidebar.set_workflow_mode(True)
        await wait_for(lambda: tabs.active != SIDEBAR_HOOKS_TAB, pilot=pilot, description="moved off Hooks")
        assert not tabs.get_tab(SIDEBAR_HOOKS_TAB).display
        sidebar.focus_tab(SIDEBAR_HOOKS_TAB)
        assert tabs.active != SIDEBAR_HOOKS_TAB
        sidebar.set_workflow_mode(False)
        await wait_for(lambda: tabs.get_tab(SIDEBAR_HOOKS_TAB).display, pilot=pilot, description="back")


# --- Performance (section 6) ----------------------------------------------------


class _FixedLog:
    """The run log's read side over fixed records, counting reads."""

    def __init__(self, records: tuple[HookRunRecord, ...]) -> None:
        self.rows = records
        self.reads = 0
        self.dropped = 0

    def records(self) -> tuple[HookRunRecord, ...]:
        self.reads += 1
        return self.rows

    def record(self, run_id: str) -> HookRunRecord | None:
        return next((r for r in self.rows if r.run_id == run_id), None)

    def detail(self, run_id: str) -> HookRunDetail | None:
        return None


def _record(run_id: str, *, state: str = "ended", age: float = 1.0) -> HookRunRecord:
    now = time.monotonic()
    ended = state == "ended"
    return HookRunRecord(
        run_id=run_id,
        session_id=_SESSION,
        hook_id=f"hook-{run_id}",
        hook_event="after_tool_call",
        execution_mode="blocking",
        detach=False,
        tool_name="write_file",
        sub_agent="",
        retry=False,
        registered_at=now - age - 0.1,
        started_at=now - age - 0.1,
        ended_at=now - age if ended else None,
        registered_wall=time.time() - age,
        state=state,  # type: ignore[arg-type]
        outcome=HookOutcome.SUCCESS if ended else None,
        exit_code=0 if ended else None,
        effect="added_context" if ended else None,
    )


async def _populate(pilot: Pilot, main: MainScreen) -> None:
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


async def test_a_full_list_updating_and_ticking_on_a_long_transcript_does_not_restyle_relayout_or_recompose(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ended = tuple(_record(f"e{n}", age=1.0 + n) for n in range(100))
    log = _FixedLog((_record("live", state="running", age=0.5), *ended))
    bus = EventBus()
    app = _make_app(tmp_path, bus, log)
    async with app.run_test(size=(160, 40)) as pilot:
        main = await _ready(pilot)
        await _populate(pilot, main)
        await _show_hooks_tab(pilot, main)
        await wait_for(lambda: len(_rows(main)) > 5, pilot=pilot, description="rows painted")
        await wait_for(lambda: screen_is_settled(app, main), pilot=pilot, description="settled underlay")
        seen: dict[str, list[object]] = {"styles": [], "layout": [], "footer": []}
        monkeypatch.setattr(main, "update_node_styles", lambda animate=True: seen["styles"].append(animate))
        monkeypatch.setattr(main, "_refresh_layout", lambda *_a, **_k: seen["layout"].append(None))

        async def footer_recompose() -> None:
            seen["footer"].append(None)

        monkeypatch.setattr(main.query_one(ChrysFooter), "recompose", footer_recompose)
        first = _rows(main)[0]
        # The timer ticks the live row.
        await wait_for(lambda: _rows(main)[0] != first, timeout=4.0, pilot=pilot, description="a tick")
        # Changes land with the row count kept (a count change may lay the list out once): one more
        # ended run, then the live one ends.
        log.rows = (log.rows[0], _record("new", age=0.2), *ended[:99])
        await bus.publish(HookRunsChanged(session_id=_SESSION), raise_handler_errors=True)
        await wait_for(lambda: any("hook-new" in row for row in _rows(main)), pilot=pilot, description="new row")
        log.rows = (_record("live", age=0.1), _record("new", age=0.2), *ended[:99])
        await bus.publish(HookRunsChanged(session_id=_SESSION), raise_handler_errors=True)
        await wait_for(lambda: _rows(main)[0].startswith("✓"), pilot=pilot, description="live ended")
        await wait_for(lambda: screen_is_settled(app, main), pilot=pilot, description="settled after")
        assert seen == {"styles": [], "layout": [], "footer": []}


async def test_a_hidden_tab_neither_reads_nor_renders_on_a_change(tmp_path: Path) -> None:
    log = _FixedLog((_record("live", state="running"), _record("e1")))
    bus = EventBus()
    app = _make_app(tmp_path, bus, log)
    async with app.run_test(size=(160, 40)) as pilot:
        main = await _ready(pilot)
        hooks = await _show_hooks_tab(pilot, main)
        await wait_for(lambda: len(_rows(main)) == 2, pilot=pilot, description="rows")
        main.query_one(SidebarPanel).focus_tab("tab-tasks")
        await wait_for(lambda: not hooks.is_on_screen, pilot=pilot, description="Hooks tab hidden")
        reads = log.reads
        rows = main.query_one("#hook-runs-rows", Static)
        painted = rows.content
        log.rows = (_record("e2"), _record("e1"))
        for _ in range(3):
            await bus.publish(HookRunsChanged(session_id=_SESSION), raise_handler_errors=True)
        assert not await wait_until(lambda: log.reads > reads or rows.content is not painted, timeout=1.5, pilot=pilot)
        main.query_one(SidebarPanel).focus_tab(SIDEBAR_HOOKS_TAB)
        await wait_for(
            lambda: len(_rows(main)) == 2 and _rows(main)[0].startswith("✓"), pilot=pilot, description="reread"
        )
        assert log.reads == reads + 1


# --- Under a modal (section 6) --------------------------------------------------


_ELAPSED = re.compile(r" (\d+)s$")


def _running_seconds(main: MainScreen) -> int | None:
    rows = _rows(main)
    match = _ELAPSED.search(rows[0]) if rows and _is_running(rows[0]) else None
    return int(match.group(1)) if match else None


async def _open_modal(pilot: Pilot, app: ChrysApp, main: MainScreen) -> None:
    main.action_runtime_details()
    await wait_for(
        lambda: app.screen is not main and isinstance(app.screen, ModalScreen) and app.screen.is_mounted,
        pilot=pilot,
        description="a modal over MainScreen",
    )


async def _close_modal(pilot: Pilot, app: ChrysApp, main: MainScreen) -> None:
    await pilot.press("escape")
    await wait_for(lambda: app.screen is main, pilot=pilot, description="back on MainScreen")


async def test_the_running_time_stops_under_a_modal_and_catches_up_once_it_closes(tmp_path: Path) -> None:
    live = _record("live", state="running", age=5.0)
    assert live.started_at is not None
    log = _FixedLog((live, _record("e1")))
    app = _make_app(tmp_path, EventBus(), log)
    async with app.run_test(size=(160, 40)) as pilot:
        main = await _ready(pilot)
        await _show_hooks_tab(pilot, main)
        await wait_for(lambda: _running_seconds(main) is not None, pilot=pilot, description="the running row")
        shown = _running_seconds(main)
        await wait_for(lambda: _running_seconds(main) != shown, timeout=3.0, pilot=pilot, description="a tick")

        await _open_modal(pilot, app, main)
        covered = _running_seconds(main)
        # Nothing sends the covered list a Hide: its own first tick parks the timer.
        assert not await wait_until(lambda: _running_seconds(main) != covered, timeout=2.5, pilot=pilot), (
            "the running time kept counting under the modal"
        )

        await _close_modal(pilot, app, main)
        # At once, not on the next tick: the row shows the time the run has really been running.
        await wait_for(
            lambda: (
                (seconds := _running_seconds(main)) is not None
                and abs(seconds - int(time.monotonic() - live.started_at)) <= 1
                and seconds > (covered or 0) + 1
            ),
            timeout=0.5,
            pilot=pilot,
            description="caught up",
        )
        caught_up = _running_seconds(main)
        await wait_for(
            lambda: (_running_seconds(main) or 0) > (caught_up or 0),
            timeout=3.0,
            pilot=pilot,
            description="ticking again",
        )


async def test_a_change_under_a_modal_is_read_once_it_closes(tmp_path: Path) -> None:
    log = _FixedLog((_record("live", state="running", age=2.0), _record("e1")))
    bus = EventBus()
    app = _make_app(tmp_path, bus, log)
    async with app.run_test(size=(160, 40)) as pilot:
        main = await _ready(pilot)
        await _show_hooks_tab(pilot, main)
        await wait_for(lambda: len(_rows(main)) == 2, pilot=pilot, description="rows")
        await _open_modal(pilot, app, main)
        rows = main.query_one("#hook-runs-rows", Static)
        reads = log.reads
        painted = rows.content
        # The live run ends while the modal covers the list.
        log.rows = (_record("live", age=0.1), _record("e1"))
        for _ in range(3):
            await bus.publish(HookRunsChanged(session_id=_SESSION), raise_handler_errors=True)
        assert not await wait_until(lambda: log.reads > reads or rows.content is not painted, timeout=1.5, pilot=pilot)
        await _close_modal(pilot, app, main)
        await wait_for(lambda: _rows(main)[0].startswith("✓"), pilot=pilot, description="the ended run shown")
        assert log.reads == reads + 1


async def test_an_empty_list_on_a_long_transcript_does_not_relayout_when_a_modal_closes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _make_app(tmp_path, EventBus(), _FixedLog(()))
    async with app.run_test(size=(160, 40)) as pilot:
        main = await _ready(pilot)
        await _populate(pilot, main)
        hooks = await _show_hooks_tab(pilot, main)
        await wait_for(lambda: _empty(main) == "No hook has run since iCode started", pilot=pilot, description="empty")
        await wait_for(lambda: screen_is_settled(app, main), pilot=pilot, description="settled underlay")
        seen: dict[str, list[object]] = {"styles": [], "layout": [], "footer": []}
        monkeypatch.setattr(main, "update_node_styles", lambda animate=True: seen["styles"].append(animate))
        monkeypatch.setattr(main, "_refresh_layout", lambda *_a, **_k: seen["layout"].append(None))

        async def footer_recompose() -> None:
            seen["footer"].append(None)

        monkeypatch.setattr(main.query_one(ChrysFooter), "recompose", footer_recompose)
        paints: list[None] = []
        paint = hooks._paint

        def counted_paint() -> None:
            paints.append(None)
            paint()

        monkeypatch.setattr(hooks, "_paint", counted_paint)
        # Every close resumes MainScreen, which paints the shown list again.
        for closes in (1, 2):
            await _open_modal(pilot, app, main)
            await _close_modal(pilot, app, main)
            await wait_for(lambda closes=closes: len(paints) >= closes, pilot=pilot, description="painted on resume")
        await wait_for(lambda: screen_is_settled(app, main), pilot=pilot, description="settled after")
        assert _empty(main) == "No hook has run since iCode started"
        assert seen == {"styles": [], "layout": [], "footer": []}


# --- Where the list stands ------------------------------------------------------


class _DetailedLog(_FixedLog):
    def detail(self, run_id: str) -> HookRunDetail | None:
        record = self.record(run_id)
        if record is None:
            return None
        return HookRunDetail(
            record=record,
            tool_call_id="",
            timeout_seconds=30.0,
            on_error="warn",
            delivery="",
            source="global",
            source_path="",
            run_type="command",
            run_argv=("true",),
            run_cwd="",
            run_env_names=(),
            log_path="",
            shared_log=False,
            io=None,
            applied=None,
        )


def _numbered(count: int) -> tuple[HookRunRecord, ...]:
    return tuple(dataclasses.replace(_record(f"r{number}"), number=number) for number in range(1, count + 1))


def _row_background(main: MainScreen, row: int) -> object:
    rows = main.query_one("#hook-runs-rows", Static)
    (strip,) = rows.render_lines(Region(0, row, rows.size.width, 1))
    return next(iter(strip)).style.bgcolor if strip.cell_length else None


async def test_the_hooks_tab_first_shown_stands_at_the_newest_run(tmp_path: Path) -> None:
    app = _make_app(tmp_path, EventBus(), _FixedLog(_numbered(60)))
    async with app.run_test(size=(160, 30)) as pilot:
        main = await _ready(pilot)
        await _show_hooks_tab(pilot, main)
        box = main.query_one("#hook-runs-box")
        await wait_for(
            lambda: box.max_scroll_y > 0 and box.scroll_y == box.max_scroll_y, pilot=pilot, description="at the end"
        )


async def test_the_indicator_scrolls_the_list_to_the_run_it_opens_and_marks_its_row(tmp_path: Path) -> None:
    app = _make_app(tmp_path, EventBus(), _DetailedLog(_numbered(60)))
    async with app.run_test(size=(160, 30)) as pilot:
        main = await _ready(pilot)
        sidebar = main.query_one(SidebarPanel)
        sidebar.focus_tab("tab-tasks")
        if sidebar.is_visible:
            sidebar.toggle()
        await wait_for(lambda: not sidebar.is_visible, pilot=pilot, description="closed")
        main.post_message(HookIndicator.Clicked("r20"))
        await wait_for(
            lambda: isinstance(app.screen, HookRunModal) and app.screen.is_mounted,
            pilot=pilot,
            description="that run's details open",
        )
        box = main.query_one("#hook-runs-box")
        row = 19

        def shows_row() -> bool:
            return box.size.height > 0 and 0 < box.scroll_y <= row < box.scroll_y + box.size.height

        await wait_for(shows_row, pilot=pilot, description="row #20 in view, not at either end")
        assert box.scroll_y < box.max_scroll_y
        assert _row_background(main, row) != _row_background(main, row - 1) == _row_background(main, row + 1)


# --- Only the TUI keeps a log (5.1) ---------------------------------------------


def test_the_tui_entry_point_assembles_an_engine_that_records_hook_runs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fake_platform
) -> None:
    import os

    from chrys.app.tui import app as tui_app
    from chrys.app.tui.screens import logs as logs_mod
    from chrys.foundation.config.settings import Settings
    from chrys.foundation.config.settings_store import LoadedSettings, SettingsHandle
    from chrys.orchestration import startup as startup_mod
    from chrys.orchestration.startup import RuntimeBootstrap

    captured: dict[str, object] = {}

    class _App:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            self._return_code = 0

        def run(self) -> None:
            return

    class _Registry:
        def load_all(self) -> None:
            return

        def get(self, _profile_id: str) -> None:
            return None

        def list_profiles(self) -> list[object]:
            return []

    class _Engine:
        def __init__(self, *_args: object, **kwargs: object) -> None:
            captured.update(kwargs)
            loaded = kwargs["loaded_settings"]
            assert isinstance(loaded, LoadedSettings)
            self._handle = SettingsHandle(loaded)

        @property
        def settings_handle(self) -> SettingsHandle:
            return self._handle

    monkeypatch.setattr(sys, "argv", ["icode"])
    monkeypatch.setattr("chrys.foundation.platform.get_platform", lambda: fake_platform(config_dir=tmp_path))
    monkeypatch.setattr(startup_mod, "configure_utf8_stdio", lambda: None)
    monkeypatch.setattr(
        startup_mod,
        "bootstrap_runtime",
        lambda *, project_root: RuntimeBootstrap(loaded=LoadedSettings(settings=Settings(), provenance={})),
    )
    monkeypatch.setattr(logs_mod, "install_log_handler", lambda: None)
    monkeypatch.setattr(tui_app, "configure_debug_logging", lambda _path: object())
    monkeypatch.setattr(tui_app, "stop_debug_logging", lambda _runtime: None)
    monkeypatch.setattr(tui_app, "_restore_terminal_after_run", lambda _app: None)
    monkeypatch.setattr(tui_app, "_reset_terminal_title_after_run", lambda: None)
    monkeypatch.setattr(tui_app, "ask_terminal", lambda _env, *, enabled: None)
    monkeypatch.setattr(tui_app, "EventBus", object)
    monkeypatch.setattr(tui_app, "AgentProfileRegistry", _Registry)
    monkeypatch.setattr(tui_app, "ModelProfileRegistry", _Registry)
    # ``main`` imports these inside, from their own modules.
    monkeypatch.setattr("chrys.service.state.store.JsonFileStateStore", object)
    monkeypatch.setattr("chrys.orchestration.engine.assembly.assemble_agent_engine", _Engine)
    original_cwd = Path.cwd()
    try:
        tui_app.main(app_cls=_App)  # type: ignore[arg-type]
    finally:
        os.chdir(original_cwd)
    assert captured["record_hook_runs"] is True
