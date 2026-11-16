# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for the shared Previous / "Page X of Y" / Next control."""

from __future__ import annotations

import pytest
from textual import on
from textual.app import App, ComposeResult
from textual.widgets import Button, Static

from chrys.app.tui.widgets import PageNavigator
from tests.support.waiting import wait_for


class _NavigatorApp(App[None]):
    def __init__(self) -> None:
        super().__init__()
        self.requested: list[int] = []
        self.views: list[object] = []

    def compose(self) -> ComposeResult:
        yield PageNavigator(id="pages")

    @on(PageNavigator.Changed)
    def _record(self, event: PageNavigator.Changed) -> None:
        self.requested.append(event.page)
        self.views.append(event.view)


def _label(navigator: PageNavigator) -> str:
    return str(navigator.query_one("#page-number", Static).content)


@pytest.mark.asyncio
async def test_buttons_show_their_labels_in_their_one_row() -> None:
    async with _NavigatorApp().run_test(size=(80, 5)) as pilot:
        navigator = pilot.app.query_one(PageNavigator)
        navigator.show(2, 3)
        await pilot.pause()

        for selector, label in (("#previous-page", "Previous"), ("#next-page", "Next")):
            button = navigator.query_one(selector, Button)
            await pilot.hover(selector)
            for state in ("hovered", "at rest"):
                # Button's own borders would leave no row for the label.
                assert button.region.height == 1, state
                assert button.content_region.height == 1, state
                assert button.content_region.width >= len(label), state
                await pilot.hover("#page-number")
            assert str(button.label) == label


@pytest.mark.asyncio
async def test_buttons_ask_for_the_next_page_and_stop_at_either_end() -> None:
    async with _NavigatorApp().run_test(size=(80, 5)) as pilot:
        app = pilot.app
        navigator = app.query_one(PageNavigator)
        previous = navigator.query_one("#previous-page", Button)
        following = navigator.query_one("#next-page", Button)
        assert (previous.disabled, _label(navigator), following.disabled) == (True, "Page 1 of 1", True)

        navigator.show(1, 2)
        assert (previous.disabled, _label(navigator), following.disabled) == (True, "Page 1 of 2", False)
        following.press()
        await wait_for(lambda: app.requested == [2], pilot=pilot, description="page 2 is requested")
        # The owner reports the page once it has loaded it.
        assert navigator.page == 1

        navigator.show(2, 2)
        assert (previous.disabled, _label(navigator), following.disabled) == (False, "Page 2 of 2", True)
        previous.press()
        await wait_for(lambda: app.requested == [2, 1], pilot=pilot, description="page 1 is requested")


@pytest.mark.asyncio
async def test_a_request_carries_the_view_shown_when_its_button_was_pressed() -> None:
    async with _NavigatorApp().run_test(size=(80, 5)) as pilot:
        app = pilot.app
        navigator = app.query_one(PageNavigator)
        following = navigator.query_one("#next-page", Button)

        # An owner whose pages always show the same thing passes no view.
        navigator.show(1, 2)
        following.press()
        await wait_for(lambda: app.views == [None], pilot=pilot, description="Next is pressed")
        # The same page of the same count each time, but of another view.
        for shown, view in enumerate(("listing", "results"), start=2):
            navigator.show(1, 2, view=view)
            following.press()
            await wait_for(
                lambda shown=shown: len(app.views) == shown, pilot=pilot, description=f"Next is pressed on {view!r}"
            )
            assert app.views[-1] == view
        assert app.requested == [2, 2, 2]
