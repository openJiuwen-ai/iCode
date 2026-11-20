# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Ctrl+Insert / Shift+Insert clipboard fallbacks inside modal dialogs."""

from __future__ import annotations

import importlib
import pkgutil

import pytest
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.screen import ModalScreen
from textual.widgets import Input, Static

from chrys.app.tui import screens as tui_screens
from chrys.app.tui.behaviors.insert_clipboard import (
    COPY_WITH_INSERT_ACTION,
    PASTE_WITH_INSERT_ACTION,
    InsertClipboardScreenMixin,
)
from chrys.app.tui.screens.dialogs.base import BaseDialog
from chrys.app.tui.screens.dialogs.confirm import ConfirmDialog
from chrys.app.tui.widgets.text_area import EnhancedTextArea
from tests.support.waiting import wait_for


class _FormDialog(BaseDialog[None]):
    def compose(self) -> ComposeResult:
        yield Input(id="stock-input", select_on_focus=False)
        yield EnhancedTextArea(id="editor")


class _HostApp(App[None]):
    def compose(self) -> ComposeResult:
        yield Static("host")


@pytest.fixture
def clipboard(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """Fake host OS clipboard shared by copy and paste helpers."""
    state = {"text": "outside"}
    monkeypatch.delenv("TEXTUAL_DRIVER", raising=False)
    monkeypatch.setattr(
        "chrys.app.tui.clipboard.clipboard_copy",
        lambda text: state.__setitem__("text", text),
    )
    monkeypatch.setattr("chrys.app.tui.clipboard.platform_helpers.clipboard_paste", lambda: state["text"])
    return state


async def test_ctrl_insert_copies_screen_selection_inside_confirm_dialog(
    monkeypatch: pytest.MonkeyPatch,
    clipboard: dict[str, str],
) -> None:
    """Textual hides app-level bindings behind a modal, so the dialog must own the fallback."""
    app = _HostApp()
    async with app.run_test() as pilot:
        app.push_screen(ConfirmDialog())
        await pilot.pause()
        dialog = app.screen
        assert isinstance(dialog, ConfirmDialog)
        monkeypatch.setattr(dialog, "get_selected_text", lambda: "dialog selection")

        await pilot.press("ctrl+insert")
        await pilot.pause()

        assert clipboard["text"] == "dialog selection"
        assert app.clipboard == "dialog selection"


async def test_modal_insert_fallbacks_keep_editor_precedence_and_reach_stock_controls(
    monkeypatch: pytest.MonkeyPatch,
    clipboard: dict[str, str],
) -> None:
    app = _HostApp()
    async with app.run_test() as pilot:
        app.push_screen(_FormDialog())
        await pilot.pause()
        dialog = app.screen
        assert isinstance(dialog, _FormDialog)
        monkeypatch.setattr(dialog, "get_selected_text", lambda: "dialog selection")

        editor = dialog.query_one("#editor", EnhancedTextArea)
        editor.text = "editor selection"
        editor.select_all()
        editor.focus()
        await wait_for(lambda: editor.has_focus, pilot=pilot, description="control focus before interaction")
        await pilot.pause()

        await pilot.press("ctrl+insert")
        await pilot.pause()

        assert clipboard["text"] == "editor selection"

        stock_input = dialog.query_one("#stock-input", Input)
        stock_input.focus()
        await wait_for(lambda: stock_input.has_focus, pilot=pilot, description="control focus before interaction")
        await pilot.pause()
        clipboard["text"] = "pasted value"

        await pilot.press("shift+insert")
        await pilot.pause()

        assert stock_input.value == "pasted value"

        # A stock control without its own selection still copies rendered text.
        await pilot.press("ctrl+insert")
        await pilot.pause()

        assert clipboard["text"] == "dialog selection"


def _chrys_modal_screen_classes() -> list[type[ModalScreen]]:
    for module in pkgutil.walk_packages(tui_screens.__path__, prefix=f"{tui_screens.__name__}."):
        importlib.import_module(module.name)
    found: list[type[ModalScreen]] = []
    pending: list[type[ModalScreen]] = list(ModalScreen.__subclasses__())
    while pending:
        cls = pending.pop()
        if cls in found:
            continue
        found.append(cls)
        pending.extend(cls.__subclasses__())
    return sorted(
        (cls for cls in found if cls.__module__.startswith("chrys.")),
        key=lambda cls: f"{cls.__module__}.{cls.__qualname__}",
    )


def _declared_actions(cls: type[ModalScreen], key: str) -> set[str]:
    actions: set[str] = set()
    for base in cls.__mro__:
        for binding in base.__dict__.get("BINDINGS", ()):
            assert isinstance(binding, Binding), (cls, binding)
            if key in binding.key.split(","):
                actions.add(binding.action)
    return actions


def test_every_chrys_modal_screen_declares_insert_clipboard_fallbacks() -> None:
    """Every modal root must repeat the fallbacks; the app copy never reaches a modal."""
    modal_classes = _chrys_modal_screen_classes()

    assert ConfirmDialog in modal_classes
    for cls in modal_classes:
        assert issubclass(cls, InsertClipboardScreenMixin), cls
        assert COPY_WITH_INSERT_ACTION in _declared_actions(cls, "ctrl+insert"), cls
        assert PASTE_WITH_INSERT_ACTION in _declared_actions(cls, "shift+insert"), cls
