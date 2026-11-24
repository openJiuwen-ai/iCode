# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The dialog a hook's ``ask`` shows: which hook asks, why, and about what; Continue or Cancel.

Everything the hook and the model wrote (the reason, the tool arguments, the
user's message) is external text: sanitized and shown as plain text, never
markup. The hook's source is shown too: a working directory hook comes from the
repository, whose reason may be written to talk the user into continuing.

What it asks about shows whole, as the approval dialog shows a tool call: each
argument (or the message) in a box under its name, a long one scrolling inside
its own box (``widgets/value_box.py``), so nothing the user agrees to is hidden.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from rich.cells import cell_len
from rich.text import Text
from textual.containers import VerticalGroup, VerticalScroll
from textual.widgets import Static

from chrys.app.tui.screens.dialogs import confirm as confirm_module
from chrys.app.tui.screens.dialogs.confirm import ConfirmDialog
from chrys.app.tui.widgets.value_box import VALUE_DISPLAY_MAX_CHARS, shown_line, value_box
from chrys.foundation.i18n import MessageDef, msg
from chrys.foundation.i18n.formatting import sanitize_legacy_block

if TYPE_CHECKING:
    from collections.abc import Callable

    from textual.app import ComposeResult

    from chrys.app.tui.i18n import LocaleController
    from chrys.app.tui.screens.dialogs.confirm import ConfirmMessage
    from chrys.foundation.events.types import HookConfirmationRequest
    from chrys.foundation.i18n import MessageRef

_TITLE = msg("tui.hook_confirmation.title", fallback="Hook confirmation")
_ASKS = msg("tui.hook_confirmation.asks", fallback="Hook {hook} asks you to confirm:")
_ASKS_FROM = msg("tui.hook_confirmation.asks_from", fallback="Hook {hook} ({source}) asks you to confirm:")
_SOURCE_WORKDIR = msg("tui.hook_confirmation.source.workdir", fallback="working directory")
_SOURCE_GLOBAL = msg("tui.hook_confirmation.source.global", fallback="global")
_NO_REASON = msg("tui.hook_confirmation.no_reason", fallback="(no reason given)")
_TOOL = msg("tui.hook_confirmation.tool", fallback="Tool: {tool}")
_SUB_AGENT_TOOL = msg("tui.hook_confirmation.sub_agent_tool", fallback="Tool: {tool} (sub-agent {agent})")
_ASKED_AGAIN = msg(
    "tui.hook_confirmation.asked_again",
    fallback="Asked again: the arguments changed after you continued.",
)
_CHANGED = msg("tui.hook_confirmation.changed", fallback="Changed:")
_ABSENT = msg("tui.hook_confirmation.absent", fallback="(none)")
_MESSAGE = msg("tui.hook_confirmation.message", fallback="Your message")
_CONTINUE = msg("tui.hook_confirmation.continue", fallback="Continue")
_CANCEL = msg("tui.hook_confirmation.cancel", fallback="Cancel")

# How much of a value's lines the dialog's width reads: enough to find a wide line,
# never a whole file.
_WIDTH_SAMPLE_CHARS = 4000


class HookConfirmationDialog(ConfirmDialog):
    """Which hook asks and why, then what it asks about in boxes; Continue or Cancel."""

    # A relative path is read beside the subclass: point at the confirm dialog's own sheet.
    CSS_PATH = str(Path(confirm_module.__file__).with_name("confirm.tcss"))

    DEFAULT_CSS = """
    HookConfirmationDialog #hook-confirmation-values {
        width: 100%;
        height: auto;
        padding: 0 2;
    }
    """

    def __init__(self, event: HookConfirmationRequest, *, locale_controller: LocaleController | None = None) -> None:
        # Set first: the dialog renders its message while it is built.
        self._event = event
        super().__init__(
            title=_TITLE.bind(),
            message=Text(),
            confirm_label=_CONTINUE.bind(),
            cancel_label=_CANCEL.bind(),
            confirm_variant="primary",
            locale_controller=locale_controller,
            bold_message_prefix=False,
            message_align="left",
        )
        self._values = _values(event)
        """Each box's (title, value): the tool's arguments, or the user's message."""

    def compose_message(self) -> ComposeResult:
        with VerticalScroll(id="notice-scroll", classes="confirm-scroll"):
            body = self._render_dialog_message(self._message_value)
            yield Static(body if isinstance(body, Text) else Text(body), id="confirm-message")
            with VerticalGroup(id="hook-confirmation-values"):
                # Laid out whole up to the limit in all; past it, a value scrolls in its box.
                room = VALUE_DISPLAY_MAX_CHARS
                for label, value in self._values:
                    whole = len(value) <= room
                    if whole:
                        room -= len(value)
                    yield value_box(value, self._label(label), whole=whole, classes="hook-confirmation-value")

    def _render_dialog_message(self, message: ConfirmMessage) -> Text:
        """The header, rendered in the app's language each time: when built, and on a language switch.

        *message* is ignored on purpose: the header is built from ``self._event``, which ``__init__``
        sets before the base class first calls this.
        """
        return _header(self._event, self._render_message)

    def _label(self, label: str | MessageDef) -> str:
        return label if isinstance(label, str) else self._render_message(label.bind())

    def refresh_localization(self) -> None:
        super().refresh_localization()
        # The message's box is titled in the app's words; an argument's by its name.
        for box, (label, _value) in zip(self.query(".hook-confirmation-value"), self._values, strict=True):
            if not isinstance(label, str):
                box.border_title = Text(shown_line(self._label(label)))

    def _dialog_width(self, available_width: int) -> int:
        """Wide enough for the header and the values' lines too, as far as the tiers go."""
        width = super()._dialog_width(available_width)
        # A box adds its border and padding to the header's own.
        needed = max(
            (cell_len(line) + 10 for _label, value in self._values for line in value[:_WIDTH_SAMPLE_CHARS].split("\n")),
            default=0,
        )
        desired = next((tier for tier in (width, 80, 100) if tier >= needed), 100)
        return max(width, min(desired, int(available_width * 0.9)))


def hook_confirmation_dialog(
    event: HookConfirmationRequest, *, locale_controller: LocaleController | None = None
) -> HookConfirmationDialog:
    """A dialog that dismisses with True when the user continues, False when they cancel or close it."""
    return HookConfirmationDialog(event, locale_controller=locale_controller)


def _header(event: HookConfirmationRequest, render: Callable[[MessageRef], str]) -> Text:
    hook = sanitize_legacy_block(event.hook_id)
    sources = {"workdir": _SOURCE_WORKDIR, "global": _SOURCE_GLOBAL}
    source = sources.get(event.hook_source)
    header = (
        render(_ASKS_FROM.bind(hook=hook, source=render(source.bind())))
        if source is not None
        else render(_ASKS.bind(hook=hook))
    )
    text = Text(header, style="bold")
    reason = sanitize_legacy_block(event.reason) if event.reason else render(_NO_REASON.bind())
    text.append(f"\n{reason}")
    if event.subject != "tool":
        return text
    text.append("\n\n")
    if event.asked_again:
        text.append(f"{render(_ASKED_AGAIN.bind())}\n", style="bold")
        text.append(f"{render(_CHANGED.bind())}\n")
        absent = render(_ABSENT.bind())
        for change in event.changed_arguments:
            before = sanitize_legacy_block(change.before) if change.before is not None else absent
            after = sanitize_legacy_block(change.after) if change.after is not None else absent
            text.append(f"  {sanitize_legacy_block(change.name)}: {before} → {after}\n")
        text.append("\n")
    tool = sanitize_legacy_block(event.tool_name)
    in_sub_agent = event.origin is not None and event.origin.kind == "sub_agent"
    agent = event.caller_name if in_sub_agent else ""
    line = _SUB_AGENT_TOOL.bind(tool=tool, agent=sanitize_legacy_block(agent)) if agent else _TOOL.bind(tool=tool)
    text.append(render(line))
    return text


def _values(event: HookConfirmationRequest) -> list[tuple[str | MessageDef, str]]:
    """What the boxes show: each argument under its name, or the message under ``_MESSAGE``."""
    if event.subject != "tool":
        return [(_MESSAGE, event.input_text)]
    return [(str(name), _value_text(value)) for name, value in event.arguments.items()]


def _value_text(value: object) -> str:
    """A string as it is (a script reads as its lines); anything else as JSON."""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, indent=2, default=str)
    except TypeError, ValueError, RecursionError:
        return str(value)
