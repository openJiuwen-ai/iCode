# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Shared loading indicator widgets."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, ClassVar, Protocol, cast

from rich.style import Style
from rich.text import Text
from textual.color import Color, Gradient
from textual.geometry import Region
from textual.strip import Strip
from textual.widgets import LoadingIndicator

from chrys.app.tui.i18n import render_text
from chrys.app.tui.util.shimmer import FRAME_SECONDS, RUNNING_DOT, running_dot, shimmer
from chrys.app.tui.util.visibility import is_widget_shown_on_active_screen
from chrys.foundation.i18n import msg
from chrys.foundation.i18n.formatting import format_message

if TYPE_CHECKING:
    from rich.color import Color as RichColor
    from textual.css.styles import RenderStyles
    from textual.widget import Widget

    from chrys.app.tui.i18n import LocaleController

_LOADING = msg("tui.loading.label", fallback="Loading...")


class _LocaleControllerApp(Protocol):
    @property
    def locale_controller(self) -> LocaleController: ...


def component_text_style(widget: Widget, name: str) -> Style:
    """*widget*'s *name* component style: its colour and text style, without a background.

    The full style blends an alpha colour such as ``$text-muted`` over the background; the text keeps the widget's
    own background, so a theme passing the terminal's through stays transparent.
    """
    full = widget.get_component_rich_style(name)
    return full.without_color + Style.from_color(full.color)


if TYPE_CHECKING:
    _GatedAnimationBase = Widget
else:
    # A plain mixin at runtime: Textual reads default CSS along each class's first DOMNode base only, so a Widget
    # base listed first would hide the CSS of the widget class it is mixed into.
    _GatedAnimationBase = object


class GatedAnimation(_GatedAnimationBase):
    """Mixin for a widget whose ``auto_refresh`` timer animates it only while it is seen and wanted.

    The timer ticks only while the widget is composited, between Textual's Show
    and Hide events, and its owner wants it animated (``resume_animation``). The
    compositor maps neither ``display: none`` nor ``visibility: hidden`` widgets,
    and Chrys's layout-free visibility flips post the same Show/Hide as a reflow,
    so a spinner inside a hidden container, a collapsed tool group or an idle
    status bar costs no timer ticks at all; on Windows every tick used to be an
    executor-thread hop.
    """

    _composited = False
    """Whether the compositor maps this widget: set by Show, cleared by Hide."""
    _animating = True
    """The owner's intent: ``pause_animation`` parks the timer even while composited."""
    _text_styles: dict[str, tuple[RenderStyles, Style]] | None = None
    """Each component's text style, with the component styles it was resolved from (:meth:`text_style_of`)."""

    def text_style_of(self, name: str) -> Style:
        """:func:`component_text_style` of this widget, kept while the component's styles stay the same.

        Each frame repaints the widget, which drops Textual's own cache of its component styles. Restyling the
        widget, for a theme, a class or a class of an ancestor, gives each component new styles, resolved again.
        """
        source = self._component_styles.get(name)
        cached = None if self._text_styles is None else self._text_styles.get(name)
        if cached is not None and cached[0] is source:
            return cached[1]
        style = component_text_style(self, name)
        if source is not None:
            if self._text_styles is None:
                self._text_styles = {}
            self._text_styles[name] = (source, style)
        return style

    def pause_animation(self) -> None:
        """Park the refresh timer while the owner shows the widget idle."""
        self._animating = False
        self._sync_timer()

    def resume_animation(self) -> None:
        """Let the refresh timer run again once the owner shows activity."""
        self._animating = True
        self._sync_timer()

    def on_show(self) -> None:
        self._composited = True
        self._sync_timer()

    def on_hide(self) -> None:
        self._composited = False
        self._sync_timer()

    def _sync_timer(self) -> None:
        timer = self._auto_refresh_timer
        if timer is None:
            return
        if self._animating and self._composited:
            timer.resume()
        else:
            timer.pause()

    def automatic_refresh(self) -> None:
        """Animate only on the active screen, without arranging the tree.

        ``DOMNode.automatic_refresh`` gates on ``is_on_screen``, which goes
        through ``Screen.find_widget`` and recomputes the compositor's full
        map whenever it was invalidated. With ``auto_refresh`` at 16/s that
        turns every invalidation (scroll ticks, any layout change) into an
        O(all mounted widgets) arrange pass — even while the widget is
        hidden inside a collapsed status section.
        """
        if is_widget_shown_on_active_screen(self):
            self.refresh()
        if not (self._animating and self._composited):
            # No Show is coming for a spinner mounted inside a hidden container,
            # and a timer may start after the owner's pause: the first tick parks it.
            # Painting is gated above on the compositor's cut, not on Show having
            # been processed, so a freshly shown spinner still paints this tick.
            self._sync_timer()


class RunningTitle(GatedAnimation):
    """Mixin for a title that, while its work runs, shows a blinking dot before it and a band of light across it.

    The band rises from the widget's own colour to that of its ``SHIMMER_COMPONENT`` component, which also lights
    the dot. The refresh timer starts the first time the title runs, so a title that never runs keeps none.
    """

    SHIMMER_COMPONENT: ClassVar[str]
    _work_running = False
    """Whether the title shows its work running; never ``_running``, which is MessagePump's own state."""
    _animating = False

    def on_mount(self) -> None:
        if self._work_running:
            self.auto_refresh = FRAME_SECONDS

    def set_running(self, running: bool) -> bool:
        """Start or stop the dot and the band; whether that changed anything."""
        if running == self._work_running:
            return False
        self._work_running = running
        if running and self._auto_refresh_timer is None and self.is_mounted:
            self.auto_refresh = FRAME_SECONDS
        if running:
            self.resume_animation()
        else:
            self.pause_animation()
        return True

    def running_title(
        self, title: Text, end: int | None = None, *, base: RichColor | None = None, show_dot: bool = True
    ) -> Text:
        """*title* with a band across its first *end* characters, or all of them, optionally after the dot.

        The band rises from *base*, the colour of the title's text, or else the widget's own.
        """
        lit = self.text_style_of(self.SHIMMER_COMPONENT)
        if self.app.animation_level == "none":
            return Text.assemble((f"{RUNNING_DOT} ", lit), title) if show_dot else title
        now = time.monotonic()
        start = self.rich_style.color if base is None else base
        shimmer(title, len(title.plain) if end is None else end, start, lit.color, now)
        if not show_dot:
            return title
        dot, on = running_dot(now)
        return Text.assemble((f"{dot} ", lit if on else Style()), title)


class ChrysLoadingIndicator(GatedAnimation, LoadingIndicator):
    """LoadingIndicator with a brighter gradient floor for transparent themes; its 16/s timer is gated."""

    def render_lines(self, crop: Region) -> list[Strip]:
        if not self.is_attached:
            return [Strip.blank(crop.width) for _ in crop.line_range]
        return super().render_lines(crop)

    def render(self) -> Text:
        if not self.is_attached:
            return Text(format_message(_LOADING.bind()))
        if self.app.animation_level == "none":
            try:
                localizer = cast("_LocaleControllerApp", self.app).locale_controller.localizer
            except AttributeError, RuntimeError:
                return Text(format_message(_LOADING.bind()))
            return render_text(localizer, _LOADING.bind())
        elapsed = time.time() - self._start_time
        speed = 0.8
        dot = "\u25cf"
        _, _, _bg, color = self.colors
        # Dim end: desaturate towards grey for a muted appearance.
        dim = color.blend(Color(50, 50, 50), 0.75)
        gradient = Gradient(
            (0.0, dim),
            (0.7, color),
            (1.0, color.lighten(0.1)),
        )
        blends = [(elapsed * speed - i / 8) % 1 for i in range(5)]
        dots = [(f"{dot} ", Style.from_color(gradient.get_color((1 - b) ** 2).rich_color)) for b in blends]
        indicator = Text.assemble(*dots)
        indicator.rstrip()
        return indicator
