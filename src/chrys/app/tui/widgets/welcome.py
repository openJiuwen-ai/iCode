# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Shared, centered welcome surface with a configurable logo and context."""

from __future__ import annotations

from textwrap import dedent
from typing import TYPE_CHECKING, ClassVar

from rich.cells import cell_len
from rich.segment import Segment
from rich.style import Style
from rich.text import Text
from textual.widget import Widget

from chrys.app.tui.clipboard import copy_text_to_clipboards, notify_after_copy
from chrys.app.tui.copy_messages import COPIED_TITLE
from chrys.app.tui.i18n import render_str, widget_localizer

if TYPE_CHECKING:
    from rich.console import Console, ConsoleOptions, RenderResult
    from textual.geometry import Size


def copy_on_click(text: str) -> Style:
    """The style of a notice span that copies ``text`` when it is clicked."""
    return Style(underline=True) + Style.from_meta({"@click": ("copy_text", (text,))})


class _WelcomeRenderable:
    """Fill the available area with a centered logo, title, working directory and notice."""

    def __init__(
        self,
        logo_lines: tuple[str, ...],
        title: str,
        cwd: str,
        width: int,
        height: int,
        logo_style: Style,
        notice_lines: tuple[Text, ...] = (),
    ) -> None:
        self.logo_lines = logo_lines
        self.title = title
        self.cwd = cwd
        self.width = width
        self.height = height
        self.logo_style = logo_style
        self.notice_lines = notice_lines

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        width, height = self.width, self.height
        if width <= 0 or height <= 0:
            return

        logo_width = max((cell_len(line) for line in self.logo_lines), default=0)
        logo_left = max(0, (width - logo_width) // 2)
        logo = [(logo_left, Text(line, style=self.logo_style)) for line in self.logo_lines]
        info: list[tuple[int, Text]] = []
        for value, style in ((self.title, Style(bold=True)), (self.cwd, Style(dim=True))):
            if value:
                line = Text(value, style=style)
                line.truncate(width, overflow="ellipsis")
                info.append((max(0, (width - line.cell_len) // 2), line))
        notice = [(max(0, (width - line.cell_len) // 2), line) for line in self.notice_lines]
        texts = [section for section in (info, notice) if section]
        if logo and texts and len(logo) + sum(1 + len(section) for section in texts) > height:
            # A short area keeps the text under the logo whole and lets the logo go.
            logo = []
        rows: list[tuple[int, Text]] = []
        for section in [logo, *texts] if logo else texts:
            if rows:
                rows.append((0, Text()))
            rows.extend(section)

        top = max(0, (height - len(rows)) // 2)
        for y in range(height):
            row = y - top
            if 0 <= row < len(rows):
                left, line = rows[row]
                yield Segment(" " * left)
                # Text.render leaves out the base style of a Text without spans.
                segments = Segment.apply_style(line.render(console), console.get_style(line.style))
                yield from Segment.adjust_line_length(list(segments), width - left)
            else:
                yield Segment(" " * width)
            yield Segment.line()


class WelcomeWidget(Widget):
    """Reusable empty state; callers own mode selection and localized display text."""

    COMPONENT_CLASSES: ClassVar[set[str]] = {"welcome--notice"}

    DEFAULT_CSS = """
    WelcomeWidget > .welcome--notice {
        color: $warning;
    }
    WelcomeWidget {
        width: 100%;
        height: 100%;
        color: $foreground;
        /* A span to click is part of the notice, so it keeps the notice color. */
        link-color: $warning;
        link-style: bold underline;
    }
    """

    def __init__(
        self,
        logo: str,
        *,
        title: str = "",
        cwd: str = "",
        compact_logo: str = "",
        notice: Text | None = None,
        id: str | None = None,
    ) -> None:
        super().__init__(id=id)
        self._logo_lines = tuple(
            dedent("\n".join(line.rstrip() for line in logo.splitlines())).strip("\n").splitlines()
        )
        self._logo_width = max((cell_len(line) for line in self._logo_lines), default=0)
        self._title = title
        self._cwd = cwd
        self._compact_logo = compact_logo
        self._notice = notice

    def update_info(self, *, title: str | None = None, cwd: str | None = None, compact_logo: str | None = None) -> None:
        """Update context without scheduling a repaint for unchanged values."""
        values = (
            self._title if title is None else title,
            self._cwd if cwd is None else cwd,
            self._compact_logo if compact_logo is None else compact_logo,
        )
        if values != (self._title, self._cwd, self._compact_logo):
            self._title, self._cwd, self._compact_logo = values
            height = self.styles.height
            self.refresh(layout=height is not None and height.is_auto)

    def set_notice(self, notice: Text | None) -> None:
        """Show ``notice`` one blank line below the working directory, or remove it.

        Its spans keep their own styles over the notice color.
        """
        if notice != self._notice:
            self._notice = notice
            height = self.styles.height
            self.refresh(layout=height is not None and height.is_auto)

    def action_copy_text(self, text: str) -> None:
        """``@click`` target of :func:`copy_on_click`."""
        dispatched = copy_text_to_clipboards(self.app, text)
        notify_after_copy(
            self,
            text,
            dispatched=dispatched,
            title=render_str(widget_localizer(self), COPIED_TITLE.bind()),
            timeout=2,
            markup=False,
        )

    def _notice_lines(self, width: int) -> tuple[Text, ...]:
        if self._notice is None or width <= 0:
            return ()
        notice = self._notice.copy()
        # The color alone: the component's stamped background would paint over this widget's own.
        notice.style = Style.from_color(self.get_component_rich_style("welcome--notice").color)
        lines = notice.wrap(self.app.console, width)
        for line in lines:
            line.rstrip()
        return tuple(lines)

    def _visible_logo(self, width: int) -> tuple[str, ...]:
        # A compact wordmark keeps wide ASCII art readable in narrow panels.
        return (self._compact_logo,) if self._compact_logo and width < self._logo_width else self._logo_lines

    def get_content_height(self, container: Size, viewport: Size, width: int) -> int:
        info_height = bool(self._title) + bool(self._cwd)
        notice_height = len(self._notice_lines(width))
        return (
            len(self._visible_logo(width))
            + (1 + info_height if info_height else 0)
            + (1 + notice_height if notice_height else 0)
        )

    def render(self) -> _WelcomeRenderable:
        return _WelcomeRenderable(
            self._visible_logo(self.size.width),
            self._title,
            self._cwd,
            self.size.width,
            self.size.height,
            Style(color=self.rich_style.color, bold=True),
            self._notice_lines(self.size.width),
        )
