# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Choosing a session in F1 while the previous message is still being sent."""

from __future__ import annotations

from pathlib import Path

import pytest
from textual.widgets import DataTable

from chrys.app.tui.screens.main.session_handlers import RestoreRequest
from chrys.app.tui.screens.sessions.presenter import WorkflowSessionPick
from chrys.app.tui.screens.sessions.screen import SessionsScreen
from chrys.kernel import Message
from tests.support.tui_app_harness import make_chrys_app
from tests.support.waiting import wait_for

from ._workflow_support import WorkflowEngine


@pytest.mark.parametrize("sending", [True, False])
async def test_a_skipped_f1_restore_says_the_previous_message_is_still_sending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sending: bool
) -> None:
    app = make_chrys_app(tmp_path / "sessions", engine=WorkflowEngine())
    async with app.run_test(size=(145, 45)) as pilot:
        main = app._main_screen
        store = main._services.state_store if main is not None else None
        if main is None or store is None:
            raise AssertionError("the app has no main screen or state store")
        await store.save_session("saved", {"messages": [Message("user", ["hello"])]}, primary_cwd=str(tmp_path))
        await pilot.press("f1")
        await wait_for(
            lambda: isinstance(app.screen, SessionsScreen) and app.screen.is_mounted and not app.screen._loading,
            pilot=pilot,
        )
        # The submit has taken the execution lease, so the restore is skipped either way.
        monkeypatch.setattr(main._services, "execution_busy", lambda: True)
        main._state.submit.active = sending
        flashes: list[None] = []
        monkeypatch.setattr(main._view_adapter, "flash_submit_still_sending", lambda: flashes.append(None))
        restore = main._sessions.do_session_restore
        results: list[RestoreRequest] = []

        async def recorded_restore(session_id: str | WorkflowSessionPick) -> RestoreRequest:
            results.append(await restore(session_id))
            return results[-1]

        monkeypatch.setattr(main._sessions, "do_session_restore", recorded_restore)
        restore_or_flash = main._restore_session_or_flash
        finished: list[None] = []

        async def recorded_restore_or_flash(session_id: str | WorkflowSessionPick) -> None:
            await restore_or_flash(session_id)
            finished.append(None)

        monkeypatch.setattr(main, "_restore_session_or_flash", recorded_restore_or_flash)
        app.screen.query_one(DataTable).focus()
        await pilot.press("enter")
        # Wait until the flash decision is made, so the negative case cannot pass early.
        await wait_for(lambda: bool(finished), pilot=pilot)

        assert results == [RestoreRequest.SKIPPED]
        assert main.chat_session_id != "saved"
        assert len(flashes) == (1 if sending else 0)
