# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""ToolDetailModal — full, untruncated tabbed view of a tool call's input/output.

Opened from the per-tool ``view`` affordance next to ``copy``.  Tool renderers
supply ready-to-mount widgets for the Input and Output tabs; the default
builders here turn the existing copy payload (``_tool_copy_input`` /
``_tool_copy_sections``) into syntax-highlighted, markdown, or plain-text views
so each tool gets a sensible presentation without bespoke code.  Specialized
renderers (e.g. file edits) override the mixin hooks to inject richer widgets
such as a full :class:`DiffView`.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, ClassVar

from rich.text import Text
from textual import on
from textual.containers import ScrollableContainer, VerticalGroup
from textual.widget import Widget
from textual.widgets import Static, TabbedContent, TabPane

from chrys.app.tui.binding_display import CLOSE_BINDING, COPY_BINDING, localized_binding
from chrys.app.tui.clipboard import (
    OSC52_COPY_MAX_BYTES,
    copy_text_to_clipboards,
    notify_after_copy,
    terminal_clipboard_size,
)
from chrys.app.tui.copy_messages import COPIED_TITLE
from chrys.app.tui.i18n import render_str, widget_localizer
from chrys.app.tui.screens.dialogs.base import BaseDialog
from chrys.app.tui.widgets.chat.agent_transcript_surface import AgentTranscriptSurface
from chrys.app.tui.widgets.chat.tool_call import ToolViewRequested
from chrys.app.tui.widgets.chat.tool_view_builders import ToolViewContent
from chrys.foundation.i18n import MessageDef, MessageRef, msg

if TYPE_CHECKING:
    from textual.app import ComposeResult
    from textual.screen import Screen


_TITLE_MAX = 72

COPIED_INPUT = msg("tui.tool_view.copy.input", fallback="Copied input")
COPIED_OUTPUT = msg("tui.tool_view.copy.output", fallback="Copied output")
COPIED_INPUT_TOO_LARGE = msg(
    "tui.tool_view.copy.input_too_large",
    fallback="Copied input (terminal clipboard skipped: payload too large)",
)
COPIED_OUTPUT_TOO_LARGE = msg(
    "tui.tool_view.copy.output_too_large",
    fallback="Copied output (terminal clipboard skipped: payload too large)",
)
COPIED_INPUT_UNAVAILABLE = msg(
    "tui.tool_view.copy.input_unavailable",
    fallback="Copied input (terminal clipboard unavailable)",
)
COPIED_OUTPUT_UNAVAILABLE = msg(
    "tui.tool_view.copy.output_unavailable",
    fallback="Copied output (terminal clipboard unavailable)",
)
_COPY_HINT = msg("tui.tool_view.copy_hint", fallback="Press c to copy")
_INPUT = msg("tui.tool_view.tab.input", fallback="Input")
_OUTPUT = msg("tui.tool_view.tab.output", fallback="Output")
_NO_INPUT = msg("tui.tool_view.empty.input", fallback="(no input)")
_NO_OUTPUT = msg("tui.tool_view.empty.output", fallback="(no output)")


def copy_and_notify(
    screen: Screen[object],
    payload: str,
    *,
    copied: MessageDef | MessageRef,
    too_large: MessageDef | MessageRef,
    unavailable: MessageDef | MessageRef,
) -> None:
    """Copy *payload* to the clipboards and tell how that went; a message with placeholders comes bound."""
    terminal_payload_too_large = terminal_clipboard_size(payload) > OSC52_COPY_MAX_BYTES
    dispatched = copy_text_to_clipboards(screen.app, payload, max_terminal_bytes=OSC52_COPY_MAX_BYTES)
    if dispatched:
        message, timeout = copied, 2
    elif terminal_payload_too_large:
        message, timeout = too_large, 3
    else:
        message, timeout = unavailable, 3
    localizer = widget_localizer(screen)
    notify_after_copy(
        screen,
        render_str(localizer, message.bind() if isinstance(message, MessageDef) else message),
        dispatched=dispatched,
        title=render_str(localizer, COPIED_TITLE.bind()),
        timeout=timeout,
        markup=False,
    )


def _shorten(value: str, *, limit: int = _TITLE_MAX) -> str:
    """Return *value* truncated in the middle to fit the modal title."""
    if len(value) <= limit:
        return value
    head = max(8, (limit - 3) // 2)
    tail = max(8, limit - 3 - head)
    return f"{value[:head]}...{value[-tail:]}"


class ToolDetailModal(BaseDialog[None]):
    """Modal presenting a tool call's full input and output in two tabs.

    Input/output widgets are selectable; ``BaseDialog`` handles right-click
    copying for the current Textual screen selection.
    """

    CSS_PATH = "tool_view.tcss"

    BINDINGS: ClassVar[list] = [
        localized_binding("escape", "close", CLOSE_BINDING, show=False, priority=True),
        localized_binding("q", "close", CLOSE_BINDING, show=False),
        localized_binding("c", "copy", COPY_BINDING, show=False),
    ]

    def __init__(
        self,
        *,
        title: str,
        input_widgets: list[Widget],
        output_widgets: list[Widget],
        raw_input: str = "",
        raw_output: str = "",
        copy_enabled: bool = True,
        initial_tab: str = "input",
    ) -> None:
        super().__init__()
        self._title = title
        self._input_widgets = input_widgets
        self._output_widgets = output_widgets
        self._raw_input = raw_input
        self._raw_output = raw_output
        self._copy_enabled = copy_enabled
        self._initial = "tool-view-output" if initial_tab == "output" else "tool-view-input"

    @classmethod
    def from_request(cls, event: ToolViewRequested) -> ToolDetailModal:
        """Build a modal from the shared tool-card detail request."""
        return cls(
            title=event.title,
            input_widgets=event.input_widgets,
            output_widgets=event.output_widgets,
            raw_input=event.raw_input,
            raw_output=event.raw_output,
            copy_enabled=event.copy_enabled,
            initial_tab=event.initial_tab,
        )

    def compose(self) -> ComposeResult:
        localizer = widget_localizer(self)
        with VerticalGroup(id="tool-view-container") as container:
            container.border_title = Text(_shorten(self._title))
            if self._copy_enabled:
                container.border_subtitle = render_str(localizer, _COPY_HINT.bind())
            # No ``initial=``: that makes ContentTabs validate the active tab during
            # its own mount, before the per-pane Tab widgets exist, which raised
            # "No Tab with id ..." intermittently on slow CI. Default to the first
            # tab and switch (if needed) after the tabs have mounted.
            with TabbedContent(id="tool-view-tabs"):
                with (
                    TabPane(render_str(localizer, _INPUT.bind()), id="tool-view-input"),
                    ScrollableContainer(classes="tool-view-scroll"),
                ):
                    yield ToolViewContent(
                        self._input_widgets or [Static(Text(render_str(localizer, _NO_INPUT.bind())))]
                    )
                with (
                    TabPane(render_str(localizer, _OUTPUT.bind()), id="tool-view-output"),
                ):
                    if len(self._output_widgets) == 1 and isinstance(self._output_widgets[0], AgentTranscriptSurface):
                        # The transcript is already a high-performance scroll
                        # owner. Wrapping it in another ScrollableContainer
                        # makes wheel animation and scrollbar state compete.
                        yield self._output_widgets[0]
                    else:
                        with ScrollableContainer(classes="tool-view-scroll"):
                            yield ToolViewContent(
                                self._output_widgets or [Static(Text(render_str(localizer, _NO_OUTPUT.bind())))]
                            )

    def on_mount(self) -> None:
        if self._initial != "tool-view-input":
            self.call_after_refresh(self._activate_initial_tab)

    def _activate_initial_tab(self) -> None:
        with contextlib.suppress(Exception):
            self.query_one(TabbedContent).active = self._initial

    @on(ToolViewRequested)
    def _on_nested_tool_view_requested(self, event: ToolViewRequested) -> None:
        """Stack a detail modal for a tool rendered inside this modal."""
        event.stop()
        self.app.push_screen(ToolDetailModal.from_request(event))

    def action_copy(self) -> None:
        """Copy the active tab's raw payload (input JSON / output string) to the clipboard."""
        if not self._copy_enabled:
            return
        if self.query_one(TabbedContent).active == "tool-view-output":
            copy_and_notify(
                self,
                self._raw_output,
                copied=COPIED_OUTPUT,
                too_large=COPIED_OUTPUT_TOO_LARGE,
                unavailable=COPIED_OUTPUT_UNAVAILABLE,
            )
        else:
            copy_and_notify(
                self,
                self._raw_input,
                copied=COPIED_INPUT,
                too_large=COPIED_INPUT_TOO_LARGE,
                unavailable=COPIED_INPUT_UNAVAILABLE,
            )

    def action_close(self) -> None:
        """Close the dialog."""
        self.dismiss(None)
