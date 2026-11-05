# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Reusable widgets and builders for detailed tool-call views."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Sequence
from contextlib import suppress
from typing import TYPE_CHECKING, Any

from rich.table import Table
from rich.text import Text
from textual.containers import VerticalGroup
from textual.widget import Widget
from textual.widgets import Static

from chrys.app.tui.i18n import render_str, widget_localizer
from chrys.app.tui.util.source_text import sanitize_source_text
from chrys.app.tui.widgets.chat.image_preview import extract_image_previews
from chrys.app.tui.widgets.chat.image_view import TerminalImages
from chrys.app.tui.widgets.markdown import VirtualizedMarkdown
from chrys.app.tui.widgets.syntax_theme import transparent_syntax
from chrys.foundation.i18n import DisplayBlock, MessageRef, msg
from chrys.foundation.i18n.formatting import format_message, sanitize_legacy_scalar

if TYPE_CHECKING:
    from textual.app import ComposeResult

_MARKDOWN_LANGS = frozenset({"markdown", "md"})
_PLAIN_LANGS = frozenset({"", "text", "txt"})
_PARAM_INLINE_MAX = 200
"""String/JSON values longer than this get their own labeled block, not a table cell."""
_TOOL_VIEW_IMAGE_MAX_ROWS = 500
"""Large enough for proportional modal previews while bounding pathological aspect ratios."""

TOOL_VIEW_EMPTY = msg("tui.tool_view.empty", fallback="(empty)")
TOOL_VIEW_OUTPUT = msg("tui.tool_view.section.output", fallback="Output")
_TOOL_VIEW_PREPARING_DIFF = msg("tui.tool_view.preparing_diff", fallback="Preparing diff...")
_TOOL_VIEW_IMAGE_UNAVAILABLE = msg("tui.tool_view.image_unavailable", fallback="(image unavailable)")
_TOOL_VIEW_DIFF_PREPARE_FAILED = msg(
    "tui.tool_view.diff_prepare_failed",
    fallback="Error: failed to prepare diff — {detail}",
    multiline=True,
)


class ToolViewContent(VerticalGroup):
    """Container that owns styling for tool-view builder output."""

    DEFAULT_CSS = """
    ToolViewContent {
        width: 100%;
        height: auto;
    }

    .tool-view-section-title {
        width: 100%;
        height: auto;
        color: $accent;
        margin: 1 0 0 0;
    }

    .tool-view-section-title-first {
        margin: 0;
    }

    .tool-view-code,
    .tool-view-text,
    .tool-view-md,
    .tool-view-params,
    .tool-view-image {
        width: 100%;
        height: auto;
        margin: 0 0 1 0;
    }

    .tool-view-diff {
        width: 100%;
        height: auto;
        margin: 0 0 1 0;
    }

    .tool-view-diff-placeholder {
        width: 100%;
        height: auto;
        color: $text-muted;
        text-style: italic;
    }
    """

    def __init__(self, widgets: Iterable[Widget]) -> None:
        super().__init__()
        self._widgets = list(widgets)

    def compose(self) -> ComposeResult:
        yield from self._widgets


def build_code_view(
    language: str,
    text: str,
    *,
    dark: bool = True,
    render_message: Callable[[MessageRef], str] = format_message,
) -> Widget:
    """Build a single, untruncated view widget for *text* in *language*.

    ``markdown`` renders via :class:`VirtualizedMarkdown`; plain text renders as
    wrapped literal text; everything else is syntax-highlighted with a
    background-transparent, theme-matched Pygments style.
    """
    lang = (language or "").strip().lower()
    body = sanitize_source_text(
        text or render_message(TOOL_VIEW_EMPTY.bind()), tab_size=8 if lang in _PLAIN_LANGS else 4
    )
    if lang in _MARKDOWN_LANGS:
        return VirtualizedMarkdown(body, classes="tool-view-md")
    if lang in _PLAIN_LANGS:
        return Static(Text(body), classes="tool-view-text")
    return Static(transparent_syntax(body, lang, dark=dark), classes="tool-view-code")


def build_output_views(
    sections: list[tuple[str, str, str]],
    *,
    dark: bool = True,
    render_message: Callable[[MessageRef], str] = format_message,
) -> list[Widget]:
    """Turn ``(title, language, text)`` copy sections into view widgets."""
    widgets: list[Widget] = []
    multi = len(sections) > 1
    for title, language, text in sections:
        if multi:
            widgets.append(Static(Text(sanitize_legacy_scalar(title), style="bold"), classes="tool-view-section-title"))
        widgets.append(build_code_view(language, text, dark=dark, render_message=render_message))
    return widgets


class ToolViewImage(TerminalImages):
    """Full-width terminal image preview for the tool detail Output tab."""

    def __init__(self, image_contents: Sequence[Any]) -> None:
        super().__init__(
            extract_image_previews(image_contents),
            max_rows=_TOOL_VIEW_IMAGE_MAX_ROWS,
            allow_upscale=True,
            classes="tool-view-image",
        )

    def render(self) -> Text:
        return Text(render_str(widget_localizer(self), _TOOL_VIEW_IMAGE_UNAVAILABLE.bind()), style="dim")


def _scalar_text(value: object) -> Text:
    """Render a scalar parameter value as styled text."""
    if value is None:
        return Text("null", style="dim")
    if isinstance(value, bool):
        return Text("true" if value else "false")
    return Text(sanitize_source_text(str(value), tab_size=8))


def _param_label(key: str) -> Static:
    """A bold heading introducing a broken-out parameter value."""
    return Static(Text(f"{sanitize_legacy_scalar(key)}:", style="bold"), classes="tool-view-section-title")


def build_params_view(
    params: dict[str, Any],
    *,
    dark: bool = True,
    render_message: Callable[[MessageRef], str] = format_message,
) -> list[Widget]:
    """Render tool parameters as readable key/value widgets instead of raw JSON."""
    rows: list[tuple[str, Text]] = []
    blocks: list[Widget] = []
    for key, value in params.items():
        if isinstance(value, str) and ("\n" in value or len(value) > _PARAM_INLINE_MAX):
            blocks.append(_param_label(key))
            blocks.append(build_code_view("text", value, dark=dark, render_message=render_message))
        elif value is None or isinstance(value, (str, int, float, bool)):
            rows.append((key, _scalar_text(value)))
        elif isinstance(value, list) and all(not isinstance(item, (dict, list)) for item in value):
            joined = "\n".join("null" if item is None else str(item) for item in value)
            rows.append((key, Text(sanitize_source_text(joined or render_message(TOOL_VIEW_EMPTY.bind()), tab_size=8))))
        else:
            pretty = json.dumps(value, indent=2, ensure_ascii=False, default=str)
            if "\n" in pretty or len(pretty) > _PARAM_INLINE_MAX:
                blocks.append(_param_label(key))
                blocks.append(build_code_view("json", pretty, dark=dark, render_message=render_message))
            else:
                rows.append((key, Text(pretty)))

    widgets: list[Widget] = []
    if rows:
        table = Table(box=None, show_header=False, pad_edge=False, padding=(0, 2, 0, 0), expand=False)
        table.add_column(no_wrap=True)
        table.add_column(overflow="fold")
        for key, value_text in rows:
            table.add_row(Text(sanitize_legacy_scalar(key), style="bold"), value_text)
        widgets.append(Static(table, classes="tool-view-params"))
    widgets.extend(blocks)
    return widgets


class ToolViewDiff(VerticalGroup):
    """Prepare and mount a full unified :class:`DiffView` off the event loop."""

    def __init__(
        self,
        path: str,
        before: str,
        after: str,
        *,
        render_message: Callable[[MessageRef], str] = format_message,
    ) -> None:
        super().__init__(classes="tool-view-diff")
        self._path = path
        self._before = before
        self._after = after
        self._render_message = render_message

    def compose(self) -> ComposeResult:
        yield Static(
            self._render_message(_TOOL_VIEW_PREPARING_DIFF.bind()),
            classes="tool-view-diff-placeholder",
        )

    def on_mount(self) -> None:
        self.run_worker(self._prepare_and_mount(), exclusive=True, group="tool-view-diff")

    async def _prepare_and_mount(self) -> None:
        from chrys.app.tui.widgets.diff_view import DiffView

        dv = DiffView(self._path, self._path, self._before, self._after)
        dv.split = False
        dv.auto_height = True
        try:
            await dv.prepare()
        except Exception as exc:
            with suppress(Exception):
                self.query_one(".tool-view-diff-placeholder", Static).update(
                    Text(
                        render_str(
                            widget_localizer(self),
                            _TOOL_VIEW_DIFF_PREPARE_FAILED.bind(detail=DisplayBlock(str(exc))),
                        )
                    )
                )
            return
        with suppress(Exception):
            await self.mount(dv)
            await self.query_one(".tool-view-diff-placeholder", Static).remove()
