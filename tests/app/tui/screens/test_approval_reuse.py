# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Real dialog selection reaches the backend and its owner-only grant store."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from unittest.mock import AsyncMock, create_autospec

import pytest
from textual.app import App, ComposeResult
from textual.widgets import Checkbox, Collapsible, RadioButton, Static

from chrys.app.tui.screens.dialogs.approval import ApprovalDialog
from chrys.app.tui.screens.dialogs.approval.body import ApprovalBody
from chrys.app.tui.screens.main import MainScreen
from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import ApprovalRequest, Warning
from chrys.foundation.models.approval_reuse import ApprovalReuseOffer
from chrys.foundation.models.session_env import SessionEnvironment
from chrys.foundation.models.workspace import Workspace
from chrys.kernel.middleware import FunctionInvocationContext
from chrys.service.agent_middleware.control.approval import ApprovalMiddleware
from chrys.service.approval.judge import ApprovalJudge, JudgeVerdict
from chrys.service.approval.policy import ApprovalMode, ApprovalPolicy
from chrys.service.approval.reuse_binding import ApprovalReuseBinding
from chrys.service.profiles.agents.schema import ApprovalConfig
from chrys.service.tools.builtins.shell import ShellTools
from tests.support.tui_app_harness import make_chrys_app
from tests.support.tui_helpers import click_when_settled
from tests.support.waiting import wait_for


@pytest.mark.parametrize("save_ok", [True, False])
@pytest.mark.parametrize("mode", [ApprovalMode.MANUAL, ApprovalMode.AUTO])
async def test_tui_remember_selection_persists_or_reports_failure(tmp_path, monkeypatch, save_ok, mode):
    runtime = SessionEnvironment.capture("session-a", Workspace.from_cwd(str(tmp_path)))
    runtime = replace(runtime, platform=replace(runtime.platform, config_dir=tmp_path / "config"))
    tools = ShellTools(runtime).tools()
    binding = ApprovalReuseBinding(runtime, tools)
    if not save_ok:
        monkeypatch.setattr(binding.service.store, "add_many", lambda rules: False)
    bus, requests, warnings = EventBus(), [], []

    async def record_request(event):
        requests.append(event)

    async def record_warning(event):
        warnings.append(event)

    await bus.subscribe(ApprovalRequest, record_request)
    await bus.subscribe(Warning, record_warning)
    judge = create_autospec(ApprovalJudge, instance=True)
    judge.evaluate.return_value = JudgeVerdict(approved=False, reason="Human review needed")
    middleware = ApprovalMiddleware(
        ApprovalPolicy(ApprovalConfig(default="require")), bus, reuse=binding, approval_mode=mode, approval_judge=judge
    )
    called = AsyncMock(spec=lambda: None)
    app = make_chrys_app(tmp_path / "ui-sessions", event_bus=bus)
    task = None
    try:
        async with app.run_test(size=(120, 45)) as pilot:
            await wait_for(
                lambda: isinstance(app.screen, MainScreen) and app.screen.is_mounted,
                pilot=pilot,
                description="main screen mounted",
            )
            context = FunctionInvocationContext(tools[0], {"command": "npm run test"})
            task = asyncio.create_task(middleware.process(context, called))
            await wait_for(
                lambda: (isinstance(app.screen, ApprovalDialog) and app.screen.is_mounted) or task.done(),
                pilot=pilot,
                description="backend approval dialog",
            )
            if task.done():
                await task
            assert isinstance(app.screen, ApprovalDialog)
            dialog = app.screen
            await wait_for(
                lambda: dialog.is_mounted and bool(dialog.query("#reuse-remember")),
                pilot=pilot,
                description="remember checkbox mounted",
            )
            if mode == ApprovalMode.AUTO:
                assert requests[0].judging is True
                assert dialog._flagged == JudgeVerdict(approved=False, reason="Human review needed")
                judge.evaluate.assert_awaited_once()
            else:
                judge.evaluate.assert_not_awaited()
            await click_when_settled(pilot, "#reuse-remember")
            await click_when_settled(pilot, "#reuse-project")
            await click_when_settled(pilot, "#approval-yes")
            await wait_for(task.done, pilot=pilot, description="approval saved and call executed")
            await task
            called.assert_awaited_once()
            assert len(requests) == 1
            if save_ok:
                grant = binding.service.rules()[0]
                assert grant.scope == "PROJECT"
                await middleware.process(context, called)
                assert called.await_count == 2 and len(requests) == 1
                assert json.loads(middleware.drain_decisions()[-1]["grant_ids"]) == [grant.id]
                assert not warnings
            else:
                assert binding.service.rules() == []
                assert [event.code for event in warnings] == ["approval_reuse_save_failed"]
                assert "Allowed once" in warnings[0].message
    finally:
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await middleware.close()
        await bus.unsubscribe(ApprovalRequest, record_request)
        await bus.unsubscribe(Warning, record_warning)


class _DialogHost(App):
    def compose(self) -> ComposeResult:
        yield Static("placeholder")


@pytest.mark.parametrize(
    ("remember", "scope", "extra", "expected"),
    [
        (False, "session", False, ""),
        (True, "session", False, "EXACT_SESSION"),
        (True, "project", False, "EXACT_PROJECT"),
        (True, "session", True, "PREFIX_SESSION"),
        (True, "project", True, "PREFIX_PROJECT"),
    ],
)
async def test_compact_controls_submit_explicit_reuse_choice(remember, scope, extra, expected):
    dialog = ApprovalDialog(
        caller_name="Code",
        tool_name="shell",
        args={"command": "npm run test"},
        reuse_offer=ApprovalReuseOffer("command", ("npm run test",), prefix=True),
    )
    results = []
    app = _DialogHost()
    async with app.run_test(size=(80, 24)) as pilot:
        await app.push_screen(dialog, results.append)
        await pilot.pause()
        assert not dialog.query_one("#reuse-options").display
        assert dialog.query_one("#approval-reason-section", Collapsible).collapsed
        assert dialog.query_one("#reuse-remember", Checkbox).render().plain.startswith("[ ]")
        if remember:
            await click_when_settled(pilot, "#reuse-remember")
            assert dialog.query_one("#reuse-options").display
            assert dialog.query_one("#reuse-remember", Checkbox).render().plain.startswith("[*]")
            await click_when_settled(pilot, f"#reuse-{scope}")
            assert dialog.query_one(f"#reuse-{scope}", RadioButton).render().plain.startswith("(*)")
        if extra:
            await click_when_settled(pilot, "#reuse-advanced CollapsibleTitle")
            await click_when_settled(pilot, "#reuse-extra-args")
            extra_control = dialog.query_one("#reuse-extra-args", Checkbox)
            warning = dialog.query_one("#reuse-warning")
            assert warning.display
            assert warning.parent is extra_control.parent
            assert warning.region.y >= extra_control.region.bottom
            assert warning.region.x == extra_control.region.x
            await click_when_settled(pilot, "#reuse-advanced CollapsibleTitle")
        await click_when_settled(pilot, "#approval-yes")
        assert results == [(True, "", None)]
        assert dialog.remember_choice == expected


async def test_disabling_remember_clears_hidden_extra_arguments():
    dialog = ApprovalDialog(
        caller_name="",
        tool_name="shell",
        reuse_offer=ApprovalReuseOffer("command", ("npm run test",), prefix=True),
    )
    app = _DialogHost()
    async with app.run_test() as pilot:
        await app.push_screen(dialog)
        await click_when_settled(pilot, "#reuse-remember")
        await click_when_settled(pilot, "#reuse-advanced CollapsibleTitle")
        await click_when_settled(pilot, "#reuse-extra-args")
        await click_when_settled(pilot, "#reuse-remember")
        assert not dialog.query_one("#reuse-extra-args", Checkbox).value
        assert not dialog.query_one("#reuse-warning").display
        assert dialog.query_one("#reuse-advanced", Collapsible).collapsed
        await click_when_settled(pilot, "#reuse-remember")
        await click_when_settled(pilot, "#approval-yes")
        assert dialog.remember_choice == "EXACT_SESSION"


@pytest.mark.parametrize("kind", ["command", "files"])
async def test_offer_without_prefix_hides_extra_arguments(kind):
    dialog = ApprovalDialog(
        caller_name="",
        tool_name="tool",
        reuse_offer=ApprovalReuseOffer(kind, ("target",)),
    )
    app = _DialogHost()
    async with app.run_test() as pilot:
        await app.push_screen(dialog)
        await click_when_settled(pilot, "#reuse-remember")
        assert not dialog.query("#reuse-extra-args")
        label = dialog.query_one("#reuse-remember", Checkbox).label.plain
        assert label == (
            "Don't ask again to modify these files" if kind == "files" else "Don't ask again for this command"
        )
        await click_when_settled(pilot, "#reuse-project")
        await click_when_settled(pilot, "#approval-yes")
        assert dialog.remember_choice == "EXACT_PROJECT"


@pytest.mark.parametrize("decision", ["edited", "declined", "auto_approved"])
async def test_remember_controls_do_not_grant_on_non_explicit_or_edited_approval(decision):
    body = ApprovalBody(modified_args=lambda: {"command": "npm run lint"}) if decision == "edited" else None
    dialog = ApprovalDialog(
        caller_name="",
        tool_name="shell",
        approval_body=body,
        reuse_offer=ApprovalReuseOffer("command", ("npm run test",), prefix=True),
    )
    results = []
    app = _DialogHost()
    async with app.run_test() as pilot:
        await app.push_screen(dialog, results.append)
        await click_when_settled(pilot, "#reuse-remember")
        if decision == "auto_approved":
            dialog.receive_verdict(JudgeVerdict(approved=True, reason="Safe command"))
            await pilot.pause()
        else:
            await click_when_settled(pilot, "#approval-no" if decision == "declined" else "#approval-yes")
        assert dialog.remember_choice == ""
        assert results == [(decision != "declined", "", {"command": "npm run lint"} if decision == "edited" else None)]


async def test_reuse_scope_keyboard_selection_does_not_approve():
    dialog = ApprovalDialog(
        caller_name="",
        tool_name="shell",
        reuse_offer=ApprovalReuseOffer("command", ("npm run test",)),
    )
    results = []
    app = _DialogHost()
    async with app.run_test() as pilot:
        await app.push_screen(dialog, results.append)
        await click_when_settled(pilot, "#reuse-remember")
        await pilot.press("tab", "right", "space", "escape")
        assert dialog.query_one("#reuse-project", RadioButton).value
        assert app.screen is dialog and results == []
        await pilot.press("y")
        assert dialog.remember_choice == "EXACT_PROJECT"
        assert results == [(True, "", None)]


async def test_deferred_auto_approval_never_saves_a_reuse_grant(tmp_path):
    runtime = SessionEnvironment.capture("session-a", Workspace.from_cwd(str(tmp_path)))
    runtime = replace(runtime, platform=replace(runtime.platform, config_dir=tmp_path / "config"))
    tools = ShellTools(runtime).tools()
    binding = ApprovalReuseBinding(runtime, tools)
    bus, requests = EventBus(), []

    async def record_request(event):
        requests.append(event)

    await bus.subscribe(ApprovalRequest, record_request)
    judge = create_autospec(ApprovalJudge, instance=True)
    judge.evaluate.return_value = JudgeVerdict(approved=True, reason="Safe command")
    middleware = ApprovalMiddleware(
        ApprovalPolicy(ApprovalConfig(default="require")),
        bus,
        reuse=binding,
        approval_mode=ApprovalMode.AUTO,
        approval_judge=judge,
    )
    called = AsyncMock(spec=lambda: None)
    app = make_chrys_app(tmp_path / "ui-sessions", event_bus=bus)
    try:
        async with app.run_test(size=(120, 45)) as pilot:
            await wait_for(
                lambda: isinstance(app.screen, MainScreen) and app.screen.is_mounted,
                pilot=pilot,
                description="main screen mounted",
            )
            screen = app.screen
            for _ in range(2):
                await middleware.process(FunctionInvocationContext(tools[0], {"command": "npm run test"}), called)
                assert app.screen is screen
                assert binding.service.rules() == []
            assert len(requests) == judge.evaluate.await_count == called.await_count == 2
            assert all(request.judging and request.reuse_offer is not None for request in requests)
    finally:
        await middleware.close()
        await bus.unsubscribe(ApprovalRequest, record_request)
