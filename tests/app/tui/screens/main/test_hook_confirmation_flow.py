# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""A hook's ``ask`` in the real app: the main screen shows it, and the user's click is the answer (plan §5 TUI)."""

from __future__ import annotations

import asyncio
import sys
import textwrap
from pathlib import Path

import pytest
from textual.pilot import Pilot
from textual.widgets import Button

from chrys.app.tui.app import ChrysApp
from chrys.app.tui.screens.dialogs.confirm import ConfirmDialog
from chrys.app.tui.screens.main.screen import MainScreen
from chrys.app.tui.widgets.value_box import LongValueView, ValueText
from chrys.foundation.config.settings import Settings
from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import SessionReady, SessionRestored
from chrys.service.hooks.confirmation import HookConfirmationService
from chrys.service.hooks.events import HookEvent
from chrys.service.hooks.manager import HookManager, check_operation
from chrys.service.hooks.operation import HookOperation
from chrys.service.hooks.schema import HookConfig, HookExecution, HookRun, HooksFile
from chrys.service.profiles.agents.schema import AgentProfile
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


class _Engine(SessionGenerationEngine):
    def __init__(self, bus: EventBus, cwd: Path) -> None:
        self._bus = bus
        self._cwd = cwd
        self.hook_runs = None
        self.turn_lifecycle_task = None

    async def start(self, _profile: AgentProfile, *, workspace_id: str | None = None) -> None:
        await self._bus.publish(
            SessionReady(
                agent_profile=_PROFILE.name,
                display_name=_PROFILE.display_name,
                session_id=_SESSION,
                primary_cwd=str(self._cwd),
            )
        )


def _manager(hooks_dir: Path, service: HookConfirmationService, *later: HookConfig) -> HookManager:
    script = textwrap.dedent(
        """
        import json, os
        with open(os.environ["CHRYS_HOOK_RESULT"], "w") as f:
            json.dump({"action": "ask", "reason": "this changes production"}, f)
        """
    )
    manager = HookManager(
        file=HooksFile(
            hooks=[
                HookConfig(
                    id="prod-guard",
                    event=HookEvent.BEFORE_TOOL_CALL,
                    run=HookRun(type="command", argv=[sys.executable, "-c", script]),
                    execution=HookExecution(mode="blocking", timeout_seconds=10),
                ),
                *later,
            ]
        ),
        hooks_dir=hooks_dir,
    )
    manager.confirmations = service
    return manager


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


async def _setup(
    tmp_path: Path, *, settings: Settings | None = None
) -> tuple[EventBus, HookConfirmationService, HookManager, ChrysApp]:
    bus = EventBus()
    service = HookConfirmationService(bus=bus, enabled=True)
    await service.start()
    hooks_dir = tmp_path / "hooks"
    hooks_dir.mkdir()
    workspace = tmp_path / "work"
    workspace.mkdir()
    app = make_chrys_app(
        tmp_path / "state",
        settings=settings,
        engine=_Engine(bus, workspace),
        event_bus=bus,
        agent_registry=_Registry(),
    )
    return bus, service, _manager(hooks_dir, service), app


def _operation() -> HookOperation:
    return HookOperation(
        subject="tool", session_id=_SESSION, tool_name="edit_file", arguments={"path": "deploy/prod.yaml"}
    )


def _check(manager: HookManager, operation: HookOperation) -> asyncio.Task:
    return asyncio.create_task(
        check_operation(
            manager,
            HookEvent.BEFORE_TOOL_CALL,
            operation,
            lambda: {"session_id": _SESSION, "tool": {"name": "edit_file", "args": operation.arguments()}},
        )
    )


@pytest.mark.parametrize(("button", "blocked"), [("#confirm-yes", False), ("#confirm-no", True)])
@pytest.mark.asyncio
async def test_the_user_s_click_on_the_question_is_the_answer(tmp_path: Path, button: str, blocked: bool) -> None:
    _bus, _service, manager, app = await _setup(tmp_path)
    async with app.run_test(size=(140, 40)) as pilot:
        await _ready(pilot)
        operation = _operation()
        task = _check(manager, operation)
        await wait_for(
            lambda: isinstance(app.screen, ConfirmDialog) and app.screen.is_mounted,
            pilot=pilot,
            timeout=ENGINE_TURN_TIMEOUT,
        )
        dialog = app.screen
        assert isinstance(dialog, ConfirmDialog)
        shown = str(dialog.query_one("#confirm-message").render())
        assert "prod-guard" in shown and "this changes production" in shown and "edit_file" in shown
        # Each argument in a box under its name, as the approval dialog shows a tool call.
        assert _boxes(dialog) == {"path": "deploy/prod.yaml"}
        # The header reads as a block: left-aligned, not centered line by line.
        assert dialog.query_one("#confirm-message").styles.text_align == "left"
        # Continue and Cancel differ in colour, not only by the focus mark.
        assert dialog.query_one("#confirm-yes", Button).variant == "primary"
        assert not task.done()
        await click_when_settled(pilot, button)
        decision = await asyncio.wait_for(task, timeout=ENGINE_TURN_TIMEOUT)

    assert decision.blocked is blocked
    assert decision.confirmation == ("declined" if blocked else None)
    assert (operation.checked_revision == operation.revision) is not blocked


@pytest.mark.asyncio
async def test_a_withdrawn_question_closes_without_answering(tmp_path: Path) -> None:
    _bus, _service, manager, app = await _setup(tmp_path)
    async with app.run_test(size=(140, 40)) as pilot:
        main = await _ready(pilot)
        operation = _operation()
        task = _check(manager, operation)
        await wait_for(
            lambda: isinstance(app.screen, ConfirmDialog) and app.screen.is_mounted,
            pilot=pilot,
            timeout=ENGINE_TURN_TIMEOUT,
        )
        operation.invalidate("turn_ended")
        decision = await asyncio.wait_for(task, timeout=ENGINE_TURN_TIMEOUT)
        await wait_for(lambda: app.screen is main, pilot=pilot, description="the question closed")

    assert (decision.invalidated, decision.blocked) == (True, False)


@pytest.mark.asyncio
async def test_a_question_asked_again_after_a_change_says_what_changed(tmp_path: Path) -> None:
    _bus, service, _manager_without, app = await _setup(tmp_path)
    # A later hook moves the path once after the user continued: the same call is asked about again.
    move = textwrap.dedent(
        """
        import json, os
        with open(os.environ["CHRYS_HOOK_PAYLOAD_FILE"]) as f:
            path = json.load(f)["tool"]["args"]["path"]
        decision = {"action": "modify", "args_override": {"path": "deploy/staging.yaml"}}
        with open(os.environ["CHRYS_HOOK_RESULT"], "w") as f:
            json.dump(decision if path == "deploy/prod.yaml" else {"action": "allow"}, f)
        """
    )
    manager = _manager(
        tmp_path / "hooks",
        service,
        HookConfig(
            id="move",
            event=HookEvent.BEFORE_TOOL_CALL,
            run=HookRun(type="command", argv=[sys.executable, "-c", move]),
            execution=HookExecution(mode="blocking", timeout_seconds=10),
        ),
    )
    async with app.run_test(size=(140, 40)) as pilot:
        await _ready(pilot)
        operation = _operation()
        task = _check(manager, operation)
        await wait_for(
            lambda: isinstance(app.screen, ConfirmDialog) and app.screen.is_mounted,
            pilot=pilot,
            timeout=ENGINE_TURN_TIMEOUT,
        )
        first = app.screen
        assert "Asked again" not in str(first.query_one("#confirm-message").render())
        await click_when_settled(pilot, "#confirm-yes")
        await wait_for(
            lambda: isinstance(app.screen, ConfirmDialog) and app.screen is not first and app.screen.is_mounted,
            pilot=pilot,
            timeout=ENGINE_TURN_TIMEOUT,
            description="the second question",
        )
        shown = str(app.screen.query_one("#confirm-message").render())
        assert "Asked again: the arguments changed after you continued." in shown
        assert 'path: "deploy/prod.yaml" → "deploy/staging.yaml"' in shown
        await click_when_settled(pilot, "#confirm-yes")
        decision = await asyncio.wait_for(task, timeout=ENGINE_TURN_TIMEOUT)

    assert decision.blocked is False
    assert operation.arguments() == {"path": "deploy/staging.yaml"}


@pytest.mark.asyncio
async def test_the_question_is_in_the_app_s_language_and_follows_a_switch(tmp_path: Path) -> None:
    _bus, _service, manager, app = await _setup(tmp_path, settings=Settings(locale="zh-Hans"))
    async with app.run_test(size=(140, 40)) as pilot:
        await _ready(pilot)
        task = _check(manager, _operation())
        await wait_for(
            lambda: isinstance(app.screen, ConfirmDialog) and app.screen.is_mounted,
            pilot=pilot,
            timeout=ENGINE_TURN_TIMEOUT,
        )
        dialog = app.screen
        shown = str(dialog.query_one("#confirm-message").render())
        # The app's words are translated; what the hook and the model wrote stays as written.
        assert "钩子 prod-guard（全局）请你确认：" in shown  # noqa: RUF001
        assert "this changes production" in shown
        assert "工具：edit_file" in shown  # noqa: RUF001
        assert "Hook prod-guard" not in shown

        app.locale_controller.switch_locale("en")
        shown = str(dialog.query_one("#confirm-message").render())
        assert "Hook prod-guard (global) asks you to confirm:" in shown
        assert "Tool: edit_file" in shown
        assert "钩子" not in shown
        await click_when_settled(pilot, "#confirm-no")
        decision = await asyncio.wait_for(task, timeout=ENGINE_TURN_TIMEOUT)

    assert decision.blocked is True


def _boxes(dialog: ConfirmDialog) -> dict[str, str]:
    """Each value box's title and the value it shows (as given)."""
    return {
        str(box.border_title): box.value
        for box in dialog.query(".hook-confirmation-value")
        if isinstance(box, ValueText | LongValueView)
    }


@pytest.mark.asyncio
async def test_long_arguments_show_whole_and_a_long_one_scrolls_in_its_own_box(tmp_path: Path) -> None:
    _bus, _service, manager, app = await _setup(tmp_path)
    script = "\n".join(f": filler-{n:05d}" for n in range(6000)) + "\necho TAIL-MARK"
    operation = HookOperation(
        subject="tool",
        session_id=_SESSION,
        tool_name="zsh",
        arguments={"command": script, "timeout": 30, "env": {"A": "b"}},
    )
    async with app.run_test(size=(140, 40)) as pilot:
        await _ready(pilot)
        task = _check(manager, operation)
        await wait_for(
            lambda: isinstance(app.screen, ConfirmDialog) and app.screen.is_mounted,
            pilot=pilot,
            timeout=ENGINE_TURN_TIMEOUT,
        )
        dialog = app.screen
        assert isinstance(dialog, ConfirmDialog)
        # Nothing the user agrees to is cut: every argument, whole, under its name.
        assert _boxes(dialog) == {"command": script, "timeout": "30", "env": '{\n  "A": "b"\n}'}
        # The long one is not laid out at once: it scrolls in its box, drawing only what is in view.
        (long_box,) = dialog.query(LongValueView)
        assert long_box.value == script
        # The buttons stay on screen however long the arguments are.
        yes = dialog.query_one("#confirm-yes", Button)
        await wait_for(lambda: yes.region.bottom <= app.size.height and yes.region.height > 0, pilot=pilot)
        await click_when_settled(pilot, "#confirm-yes")
        decision = await asyncio.wait_for(task, timeout=ENGINE_TURN_TIMEOUT)

    assert decision.blocked is False


@pytest.mark.asyncio
async def test_a_message_shows_whole_in_a_box(tmp_path: Path) -> None:
    _bus, _service, manager, app = await _setup(tmp_path)
    text = "lab-secret " + "很长的消息 " * 3000
    operation = HookOperation(subject="input", session_id=_SESSION, input_text=text)
    async with app.run_test(size=(140, 40)) as pilot:
        await _ready(pilot)
        task = asyncio.create_task(
            check_operation(
                manager, HookEvent.BEFORE_TOOL_CALL, operation, lambda: {"session_id": _SESSION, "text": text}
            )
        )
        await wait_for(
            lambda: isinstance(app.screen, ConfirmDialog) and app.screen.is_mounted,
            pilot=pilot,
            timeout=ENGINE_TURN_TIMEOUT,
        )
        assert _boxes(app.screen) == {"Your message": text}
        await click_when_settled(pilot, "#confirm-no")
        await asyncio.wait_for(task, timeout=ENGINE_TURN_TIMEOUT)


@pytest.mark.parametrize("switch", ["new", "restore"])
@pytest.mark.asyncio
async def test_switching_sessions_with_one_question_shown_and_one_queued(tmp_path: Path, switch: str) -> None:
    # Verifies the contract the queue relies on (no clear of its own): the switch withdraws every
    # question of the old session, so the shown one closes, the queued one never shows, and the new
    # session's questions show as usual.
    bus, _service, manager, app = await _setup(tmp_path)
    async with app.run_test(size=(140, 40)) as pilot:
        main = await _ready(pilot)
        queue = main._events._hook_confirmations
        shown, queued = _operation(), _operation()
        tasks = [_check(manager, shown), _check(manager, queued)]
        await wait_for(
            lambda: isinstance(app.screen, ConfirmDialog) and app.screen.is_mounted and len(queue.queue) == 1,
            pilot=pilot,
            timeout=ENGINE_TURN_TIMEOUT,
            description="one question shown, one queued",
        )
        old_dialog = app.screen

        # What the engine does on a switch (its own tests prove it): the old session's operations end,
        # then the new session is ready or restored.
        for operation in (shown, queued):
            operation.invalidate("session_switched")
        decisions = [await asyncio.wait_for(task, timeout=ENGINE_TURN_TIMEOUT) for task in tasks]
        event: SessionReady | SessionRestored = (
            SessionReady(agent_profile=_PROFILE.name, display_name=_PROFILE.display_name, session_id="s2")
            if switch == "new"
            else SessionRestored(session_id="s2", agent_profile=_PROFILE.name, display_name=_PROFILE.display_name)
        )
        await bus.publish(event)
        await wait_for(lambda: app.screen is main, pilot=pilot, description="the old question closed")
        assert not queue.awaiting_user
        # The queued question never shows.
        assert not await wait_until(lambda: isinstance(app.screen, ConfirmDialog), timeout=0.3)

        fresh = HookOperation(subject="tool", session_id="s2", tool_name="edit_file", arguments={"path": "a.txt"})
        task = _check(manager, fresh)
        await wait_for(
            lambda: isinstance(app.screen, ConfirmDialog) and app.screen is not old_dialog and app.screen.is_mounted,
            pilot=pilot,
            timeout=ENGINE_TURN_TIMEOUT,
            description="the new session's question",
        )
        await click_when_settled(pilot, "#confirm-yes")
        decision = await asyncio.wait_for(task, timeout=ENGINE_TURN_TIMEOUT)

    assert [(d.invalidated, d.blocked) for d in decisions] == [(True, False), (True, False)]
    assert (decision.invalidated, decision.blocked) == (False, False)
