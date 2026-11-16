# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The sessions browser keeps each mode's surface filter between opens while the app runs."""

from __future__ import annotations

from pathlib import Path

from textual.pilot import Pilot

from chrys.app.tui.app import ChrysApp
from chrys.app.tui.screens.sessions.screen import SessionsScreen
from chrys.app.tui.widgets import Checkbox
from chrys.foundation.models.session_surface import SessionSurface
from chrys.kernel import Message
from tests.support.tui_app_harness import make_chrys_app
from tests.support.tui_helpers import click_when_settled
from tests.support.waiting import wait_for

from ._workflow_support import WorkflowEngine, switch_mode


async def _open_browser(app: ChrysApp, pilot: Pilot) -> SessionsScreen:
    await pilot.press("f1")
    await wait_for(
        lambda: isinstance(app.screen, SessionsScreen) and app.screen.is_mounted and not app.screen._loading,
        pilot=pilot,
        description="the sessions browser has loaded",
    )
    browser = app.screen
    if not isinstance(browser, SessionsScreen):
        raise AssertionError(f"expected the sessions browser, got {browser!r}")
    return browser


def _listed(browser: SessionsScreen) -> set[str]:
    return {row.meta.session_id for row in browser._rows}


def _checked(browser: SessionsScreen) -> dict[str | None, bool]:
    return {checkbox.id: checkbox.value for checkbox in browser.query("#surface-filters Checkbox").results(Checkbox)}


async def _close_browser(app: ChrysApp, pilot: Pilot) -> None:
    await pilot.press("escape")
    await wait_for(lambda: app.screen is app._main_screen, pilot=pilot, description="the browser closed")


async def test_surface_filter_is_remembered_between_opens_per_mode(tmp_path: Path) -> None:
    app = make_chrys_app(tmp_path / "sessions", engine=WorkflowEngine())
    async with app.run_test(size=(145, 45)) as pilot:
        main = app._main_screen
        store = main._services.state_store if main is not None else None
        if main is None or store is None:
            raise AssertionError("the app has no main screen or state store")
        for session_id, surface in (
            ("tui-session", SessionSurface.TUI),
            ("cli-session", SessionSurface.CLI),
            ("acp-session", SessionSurface.ACP),
        ):
            await store.save_session(session_id, {"messages": [Message("user", ["hello"])]}, last_surface=surface)

        browser = await _open_browser(app, pilot)
        assert _listed(browser) == {"tui-session"}
        await click_when_settled(pilot, "#surface-cli")
        await wait_for(
            lambda: _listed(browser) == {"tui-session", "cli-session"} and not browser._loading,
            pilot=pilot,
            description="CLI sessions are listed",
        )
        await _close_browser(app, pilot)

        browser = await _open_browser(app, pilot)
        assert _checked(browser) == {"surface-tui": True, "surface-cli": True, "surface-acp": False}
        assert _listed(browser) == {"tui-session", "cli-session"}
        await click_when_settled(pilot, "#surface-acp")
        await wait_for(
            lambda: _listed(browser) == {"tui-session", "cli-session", "acp-session"} and not browser._loading,
            pilot=pilot,
            description="ACP sessions are listed",
        )
        await _close_browser(app, pilot)

        # Workflow mode starts from the default and never inherits ACP, which it offers no checkbox for.
        await switch_mode(main, pilot)
        browser = await _open_browser(app, pilot)
        assert _checked(browser) == {"surface-tui": True, "surface-cli": False}
        # No workflow session is saved, so the filters are hidden: toggle the box itself.
        browser.query_one("#surface-tui", Checkbox).value = False
        # The browser reports its filter as it takes it, before the load it starts.
        await wait_for(lambda: not browser._surfaces, pilot=pilot, description="the workflow filter is cleared")
        await _close_browser(app, pilot)

        await switch_mode(main, pilot)
        browser = await _open_browser(app, pilot)
        assert _checked(browser) == {"surface-tui": True, "surface-cli": True, "surface-acp": True}
        await _close_browser(app, pilot)

        await switch_mode(main, pilot)
        browser = await _open_browser(app, pilot)
        assert _checked(browser) == {"surface-tui": False, "surface-cli": False}
