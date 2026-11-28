# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Shared dialog button rows."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from rich.text import Text
from textual.containers import HorizontalGroup
from textual.content import Content
from textual.widgets import Button

if TYPE_CHECKING:
    from textual.app import ComposeResult


class KeepFocusButton(Button):
    """A button that keeps keyboard focus where it was when clicked."""

    FOCUS_ON_CLICK = False


@dataclass(frozen=True, slots=True)
class DialogButtonSpec:
    """Button configuration for :class:`DialogButtonRow`."""

    label: Content | Text | str
    id: str
    variant: Literal["default", "primary", "success", "warning", "error"] = "primary"
    flat: bool = True
    disabled: bool = False
    focus_on_click: bool = True


class DialogButtonRow(HorizontalGroup):
    """Standard docked button row used by modal dialogs."""

    DEFAULT_CSS = """
    DialogButtonRow {
        dock: bottom;
        align: center top;
        height: 3;
        padding: 0 1;
    }

    DialogButtonRow > Button {
        margin: 0 1;
    }
    """

    def __init__(self, *buttons: DialogButtonSpec, id: str | None = None, classes: str | None = None) -> None:
        super().__init__(id=id, classes=classes)
        self._buttons = buttons

    def compose(self) -> ComposeResult:
        for button in self._buttons:
            button_type = Button if button.focus_on_click else KeepFocusButton
            yield button_type(
                button.label,
                id=button.id,
                variant=button.variant,
                flat=button.flat,
                disabled=button.disabled,
            )
