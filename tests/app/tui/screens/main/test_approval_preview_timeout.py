# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Approval deadlines start after asynchronous preview preparation finishes."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from textual.screen import ModalScreen

from chrys.app.tui.screens.dialogs.approval.bodies.file_edit import ApprovalDiffPreview
from chrys.app.tui.screens.dialogs.approval.dialog import ApprovalDialog
from chrys.app.tui.widgets.diff_view import DiffView
from chrys.foundation.config.settings import Settings
from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import ApprovalCancelled, ApprovalRequest, ApprovalResponse
from chrys.foundation.tool_kinds import KIND_FILESYSTEM_WRITE
from tests.support.tui_app_harness import make_chrys_app
from tests.support.waiting import wait_for, wait_until


class _Cover(ModalScreen[None]):
    """Another screen the user opened over the approval dialog."""


@pytest.mark.parametrize("outcome", ["ready", "error", "cancel", "covered"])
async def test_preview_preparation_does_not_consume_approval_timeout(tmp_path: Path, monkeypatch, outcome: str) -> None:
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

    await bus.subscribe(ApprovalResponse, collect)
    app = make_chrys_app(tmp_path, event_bus=bus, settings=Settings(approval_timeout_seconds=1))
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
            # Outlast the configured deadline while the actual DiffView is blocked.
            assert not await wait_until(
                lambda: bool(responses) or bool(main._events._approval()._timeouts),
                pilot=pilot,
                timeout=1.5,
            )
            if outcome == "cancel":
                await bus.publish(ApprovalCancelled(request_id="preview"), raise_handler_errors=True)
                release.set()
                await wait_for(lambda: app.screen is main, pilot=pilot, description="cancelled preview closes")
                assert not responses
                assert not main._events._approval()._timeouts
            else:
                dialog = app.screen
                if outcome == "covered":
                    await app.push_screen(_Cover())
                release.set()
                if outcome == "covered":

                    def previews_ready() -> bool:
                        previews = list(dialog.query(ApprovalDiffPreview))
                        return bool(previews) and all(preview.is_ready for preview in previews)

                    await wait_for(previews_ready, pilot=pilot, description="preview ready behind another screen")
                    # A covered dialog is not shown, so its deadline has not started.
                    assert not await wait_until(
                        lambda: bool(main._events._approval()._timeouts), pilot=pilot, timeout=0.5
                    )
                    await app.pop_screen()
                await wait_for(
                    lambda: bool(main._events._approval()._timeouts),
                    pilot=pilot,
                    description="ready preview starts approval timer",
                )
                assert not responses
                await wait_for(lambda: bool(responses), pilot=pilot, description="approval times out after preview")
                assert len(responses) == 1
                assert not responses[0].approved
                await wait_for(lambda: app.screen is main, pilot=pilot, description="timed-out preview closes")
    finally:
        release.set()
        await bus.unsubscribe(ApprovalResponse, collect)
