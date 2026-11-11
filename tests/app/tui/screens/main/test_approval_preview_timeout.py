# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Approval deadlines start after asynchronous preview preparation finishes."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from textual.screen import ModalScreen

from chrys.app.tui.screens.dialogs.approval.bodies.file_edit import ApprovalDiffPreview
from chrys.app.tui.screens.dialogs.approval.dialog import ApprovalDialog
from chrys.app.tui.screens.main.dialog_controllers import ApprovalQueueController
from chrys.app.tui.screens.main.screen import MainScreen
from chrys.app.tui.widgets.diff_view import DiffView
from chrys.foundation.config.settings import Settings
from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import ApprovalCancelled, ApprovalRequest, ApprovalResponse
from chrys.foundation.tool_kinds import KIND_FILESYSTEM_WRITE
from tests.support.tui_app_harness import make_chrys_app
from tests.support.waiting import wait_for, wait_until


class _Cover(ModalScreen[None]):
    """Another screen the user opened over the approval dialog."""


# The deadline the configured setting gives: the "ready" case runs it, through the setting.
_CONFIGURED_DEADLINE = 1
# The other cases are about when the deadline starts, not how long it is: a short one, outlasted the same way.
_SHORT_DEADLINE = 0.2


@pytest.mark.parametrize("outcome", ["ready", "error", "cancel", "covered"])
async def test_preview_preparation_does_not_consume_approval_timeout(tmp_path: Path, monkeypatch, outcome: str) -> None:
    deadline: float = _CONFIGURED_DEADLINE
    if outcome != "ready":
        deadline = _SHORT_DEADLINE
        monkeypatch.setattr(MainScreen, "_approval_timeout_seconds", lambda _self: _SHORT_DEADLINE)
    # What happened, in order: the preview released (and, covered, the cover popped), the
    # deadline armed, the response. A short deadline's timer can arm and expire between two
    # polls, so arming is recorded when it happens, not read off the live timer table.
    steps: list[str] = []
    arm_timeout = ApprovalQueueController._arm_timeout

    def recording_arm_timeout(self: ApprovalQueueController, request_id: str) -> None:
        armed = request_id in self._timeouts
        arm_timeout(self, request_id)
        if not armed and request_id in self._timeouts:
            steps.append("armed")

    monkeypatch.setattr(ApprovalQueueController, "_arm_timeout", recording_arm_timeout)
    started = asyncio.Event()
    release = asyncio.Event()
    original = DiffView.prepare

    async def prepare(self) -> None:
        started.set()
        await release.wait()
        if outcome == "error":
            raise RuntimeError("preview preparation failed")
        await original(self)

    monkeypatch.setattr(DiffView, "prepare", prepare)
    path = tmp_path / "preview.txt"
    path.write_text("old\n", encoding="utf-8")
    bus = EventBus()
    responses: list[ApprovalResponse] = []

    async def collect(event: ApprovalResponse) -> None:
        responses.append(event)
        steps.append("response")

    await bus.subscribe(ApprovalResponse, collect)
    app = make_chrys_app(tmp_path, event_bus=bus, settings=Settings(approval_timeout_seconds=_CONFIGURED_DEADLINE))
    try:
        async with app.run_test(size=(120, 36)) as pilot:
            main = app._main_screen
            assert main is not None
            await bus.publish(
                ApprovalRequest(
                    request_id="preview",
                    tool_name="write_file",
                    tool_kind=KIND_FILESYSTEM_WRITE,
                    args={"path": str(path), "content": "new\n", "overwrite": True},
                ),
                raise_handler_errors=True,
            )
            await wait_for(started.is_set, pilot=pilot, description="diff preview preparation started")
            await wait_for(
                lambda: isinstance(app.screen, ApprovalDialog) and app.screen.is_mounted,
                pilot=pilot,
                description="approval dialog mounted while preparing preview",
            )
            # Outlast the deadline while the actual DiffView is blocked.
            assert not await wait_until(lambda: bool(steps), pilot=pilot, timeout=deadline + 0.5), steps
            if outcome == "cancel":
                await bus.publish(ApprovalCancelled(request_id="preview"), raise_handler_errors=True)
                steps.append("released")
                release.set()
                await wait_for(lambda: app.screen is main, pilot=pilot, description="cancelled preview closes")
                assert not responses
                assert not main._events._approval()._timeouts
                assert steps == ["released"]
            else:
                dialog = app.screen
                if outcome == "covered":
                    await app.push_screen(_Cover())
                steps.append("released")
                release.set()
                if outcome == "covered":

                    def previews_ready() -> bool:
                        previews = list(dialog.query(ApprovalDiffPreview))
                        return bool(previews) and all(preview.is_ready for preview in previews)

                    await wait_for(previews_ready, pilot=pilot, description="preview ready behind another screen")
                    # A covered dialog is not shown, so its deadline has not started: the ready preview's
                    # callback would arm it on the next pumps, well inside 0.3 s.
                    assert not await wait_until(lambda: "armed" in steps, pilot=pilot, timeout=0.3), steps
                    steps.append("uncovered")
                    await app.pop_screen()
                await wait_for(lambda: "armed" in steps, pilot=pilot, description="ready preview starts approval timer")
                await wait_for(lambda: bool(responses), pilot=pilot, description="approval times out after preview")
                # The deadline started only once the preview was ready and shown, and its expiry answered.
                shown = ["released", "uncovered"] if outcome == "covered" else ["released"]
                assert steps == [*shown, "armed", "response"]
                assert len(responses) == 1
                assert not responses[0].approved
                await wait_for(lambda: app.screen is main, pilot=pilot, description="timed-out preview closes")
    finally:
        release.set()
        await bus.unsubscribe(ApprovalResponse, collect)
