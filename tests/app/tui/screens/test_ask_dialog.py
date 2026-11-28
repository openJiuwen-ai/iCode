# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""A cancelled wait for a dialog closes it, but never a screen pushed over it."""

from __future__ import annotations

import asyncio

import pytest

from chrys.app.tui.screens.dialogs.base import ask_dialog
from chrys.app.tui.screens.dialogs.confirm import ConfirmDialog
from tests.support.tui_helpers import PlaceholderHostApp
from tests.support.waiting import wait_for


@pytest.mark.parametrize("covered", [False, True], ids=["on-top", "covered"])
async def test_a_cancelled_wait_closes_its_dialog_once_it_is_on_top(covered):
    async with PlaceholderHostApp().run_test() as pilot:
        app = pilot.app
        base = app.screen
        dialog = ConfirmDialog()
        waiting = asyncio.create_task(ask_dialog(app.push_screen, dialog))
        await wait_for(lambda: app.screen is dialog and dialog.is_mounted, pilot=pilot)
        cover = ConfirmDialog()
        if covered:
            await app.push_screen(cover)
            await wait_for(lambda: app.screen is cover and cover.is_mounted, pilot=pilot)
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        if covered:
            await pilot.pause()
            # The other operation's dialog stays; this one closes once it is uncovered.
            assert app.screen is cover
            cover.dismiss(None)
        await wait_for(lambda: app.screen is base, pilot=pilot)
        assert dialog not in app.screen_stack
