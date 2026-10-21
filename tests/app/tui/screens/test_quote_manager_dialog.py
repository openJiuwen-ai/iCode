# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Quote-manager dialog row removal, clear-all, and controller wiring."""

from __future__ import annotations

import pytest
from textual.app import App, ComposeResult

from chrys.app.tui.screens.dialogs.quote_manager import QuoteManagerDialog
from chrys.app.tui.screens.main.quotes import QuoteController
from chrys.app.tui.widgets.chat.quote_format import SOURCE_ASSISTANT, SOURCE_USER, Quote
from tests.support.tui_helpers import click_when_settled
from tests.support.waiting import wait_for


class _DialogHostApp(App):
    """Bare host; the dialog under test carries all its own dependencies."""

    def __init__(self, dialog: QuoteManagerDialog) -> None:
        super().__init__()
        self._dialog = dialog

    def compose(self) -> ComposeResult:
        yield from ()

    async def on_mount(self) -> None:
        await self.push_screen(self._dialog)


def _remove_button_ids(dialog: QuoteManagerDialog) -> list[str]:
    return [button.id or "" for button in dialog.query("#quote-manager-list Button")]


@pytest.mark.asyncio
async def test_removing_one_of_several_quotes_updates_rows_and_controller() -> None:
    controller = QuoteController()
    assert controller.add("first quoted text", SOURCE_ASSISTANT) is not None
    assert controller.add("second quoted text", SOURCE_USER) is not None
    dialog = QuoteManagerDialog(
        controller.quotes,
        on_remove=controller.remove_at,
        on_clear_all=controller.clear,
    )
    app = _DialogHostApp(dialog)
    async with app.run_test() as pilot:
        await wait_for(
            lambda: len(dialog.query("#quote-manager-list Button")) == 2,
            pilot=pilot,
            description="both quote rows are mounted",
        )

        # Removing the first quote must actually remove it — the button id is
        # parsed back into the row index, so a nested-prefix id breaks this.
        await click_when_settled(pilot, "#quote-remove-0")
        await wait_for(
            lambda: controller.quotes == (Quote("second quoted text", SOURCE_USER),),
            pilot=pilot,
            description="the first pending quote is removed from the controller",
        )
        await wait_for(
            lambda: len(dialog.query("#quote-manager-list Button")) == 1,
            pilot=pilot,
            description="the removed row leaves the dialog",
        )

        # The remaining row's button is renumbered and still removes fine.
        assert _remove_button_ids(dialog) == ["quote-remove-0"]
        await click_when_settled(pilot, "#quote-remove-0")
        await wait_for(
            lambda: not controller.quotes,
            pilot=pilot,
            description="the remaining quote is removed too",
        )
        assert not dialog.query("#quote-manager-list Button")


@pytest.mark.asyncio
async def test_clear_all_button_empties_controller_and_rows() -> None:
    controller = QuoteController()
    controller.add("one", SOURCE_USER)
    controller.add("two", None)
    dialog = QuoteManagerDialog(
        controller.quotes,
        on_remove=controller.remove_at,
        on_clear_all=controller.clear,
    )
    app = _DialogHostApp(dialog)
    async with app.run_test() as pilot:
        await wait_for(
            lambda: len(dialog.query("#quote-manager-list Button")) == 2,
            pilot=pilot,
            description="both quote rows are mounted",
        )

        await click_when_settled(pilot, "#quote-clear")
        await wait_for(
            lambda: not controller.quotes,
            pilot=pilot,
            description="clear all empties the controller",
        )
        assert not dialog.query("#quote-manager-list Button")


@pytest.mark.asyncio
async def test_out_of_range_and_malformed_remove_ids_are_ignored() -> None:
    controller = QuoteController()
    controller.add("kept", SOURCE_ASSISTANT)
    dialog = QuoteManagerDialog(
        controller.quotes,
        on_remove=controller.remove_at,
        on_clear_all=controller.clear,
    )
    app = _DialogHostApp(dialog)
    async with app.run_test() as pilot:
        await wait_for(
            lambda: len(dialog.query("#quote-manager-list Button")) == 1,
            pilot=pilot,
            description="the quote row is mounted",
        )

        dialog._remove_row("9")
        dialog._remove_row("not-a-number")
        await pilot.pause()

        assert controller.quotes == (Quote("kept", SOURCE_ASSISTANT),)
        assert len(dialog.query("#quote-manager-list Button")) == 1


@pytest.mark.asyncio
async def test_row_button_ids_carry_the_bare_index() -> None:
    controller = QuoteController()
    controller.add("a", SOURCE_USER)
    controller.add("b", None)
    controller.add("c", SOURCE_ASSISTANT)
    dialog = QuoteManagerDialog(controller.quotes, on_remove=controller.remove_at, on_clear_all=controller.clear)
    app = _DialogHostApp(dialog)
    async with app.run_test() as pilot:
        await wait_for(
            lambda: len(dialog.query("#quote-manager-list Button")) == 3,
            pilot=pilot,
            description="all quote rows are mounted",
        )

        # The handler's parse contract: every button id minus its prefix is
        # exactly the row index as an int — no nested row prefixes.
        for button in dialog.query("#quote-manager-list Button"):
            assert button.id is not None
            int(button.id.removeprefix("quote-remove-"))
