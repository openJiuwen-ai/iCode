# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Deduplicated Footer with display-only binding localization."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from textual.binding import Binding
from textual.content import Content
from textual.screen import Screen
from textual.widgets import Footer
from textual.widgets._footer import FooterKey, FooterLabel

from chrys.app.tui.binding_display import resolve_binding_display
from chrys.app.tui.i18n import LocaleController, render_str

if TYPE_CHECKING:
    from textual.app import ComposeResult
    from textual.widget import Widget

    from chrys.foundation.i18n import Localizer

type _BindingEntry = tuple[str, Binding, bool, str]
type _BindingSignature = tuple[tuple[_BindingEntry, ...], _BindingEntry | None]


@dataclass(frozen=True, slots=True)
class _FooterKeyDisplay:
    binding: Binding
    description_literal: str
    tooltip_literal: str
    description_visible: bool
    tooltip_uses_description: bool


class ChrysFooter(Footer):
    """Avoid recomposing the Footer when only hidden bindings changed.

    Textual publishes a bindings update after every focus transition. Its
    stock Footer always recomposes in response, and those mount/removal layout
    messages make a large transcript reflow even when the visible footer is
    byte-for-byte unchanged.
    """

    # The corner key takes the right end without the stock palette slot's rule and
    # padding, so it is no wider than the hook indicator it replaces in any locale.
    DEFAULT_CSS = """
    ChrysFooter FooterKey.-corner {
        dock: right;
        .footer-key--key {
            padding: 0 1 0 0;
        }
        .footer-key--description {
            padding: 0;
        }
    }
    """

    def __init__(self, *, locale_controller: LocaleController | None = None, corner_key: str | None = None) -> None:
        """``corner_key`` puts that screen binding in the right corner in place of the quit key."""
        self._locale_controller = locale_controller
        self._corner_key = corner_key
        self._visible_binding_signature: _BindingSignature | None = None
        self._binding_recompose_generation = 0
        self._binding_recompose_in_progress = False
        self._binding_recompose_dirty = False
        self._localization_dirty = False
        self._rendered_locale_revision: int | None = None
        self._rendered_key_displays: dict[FooterKey, _FooterKeyDisplay] = {}
        super().__init__(show_command_palette=corner_key is None)
        # Render the screen-level shortcuts in the initial composition. Textual
        # normally composes an empty Footer and waits for a bindings signal, but
        # Chrys may push the startup loading dialog before that signal's deferred
        # recompose runs. The active-screen guard then intentionally strands the
        # callback until MainScreen resumes, leaving the footer blank throughout
        # startup. Later binding changes still flow through the signature-based
        # recompose path below.
        self._bindings_ready = True

    def on_mount(self) -> None:
        """Subscribe to bindings and join the bounded locale refresh registry."""
        # Invalidate an in-flight recompose when bindings change, before its
        # mount finishes and commits a signature. A queued notification can
        # arrive too late and deduplicate against bindings never rendered.
        # Only this bookkeeping is synchronous; recomposition stays deferred.
        self.screen.bindings_updated_signal.subscribe(self, self.bindings_changed, immediate=True)
        if self._locale_controller is not None:
            self._locale_controller.register_footer(self)
            if self._rendered_locale_revision != self._locale_controller.revision:
                self.invalidate_localization()

    def on_unmount(self) -> None:
        """Leave the locale registry before Textual releases the Footer."""
        if self._locale_controller is not None:
            self._locale_controller.unregister_footer(self)
        super().on_unmount()

    def invalidate_localization(self) -> None:
        """Retranslate mounted key children in place, never by recomposition."""
        if self._locale_controller is None or not self.is_attached:
            return
        if self._binding_recompose_in_progress:
            self._localization_dirty = True
            return
        if not self._rendered_key_displays:
            return
        self._update_localization_in_place()

    def compose(self) -> ComposeResult:
        """Localize stock compose nodes while retaining their source Bindings.

        Only display text is rewritten; widths stay ``auto`` so every locale
        gets its natural key geometry, at startup and after a hot switch.
        """
        controller = self._locale_controller
        if controller is None:
            for child in super().compose():
                yield self._in_corner(child)
            return

        active_bindings = self.screen.active_bindings
        visible_by_action: dict[str, tuple[Binding, str]] = {}
        for _namespace, binding, _enabled, tooltip in active_bindings.values():
            if binding.show:
                visible_by_action.setdefault(binding.action, (binding, tooltip))

        palette_entry: tuple[Binding, str] | None = None
        if self.show_command_palette and self.app.ENABLE_COMMAND_PALETTE:
            palette_key = self.app.COMMAND_PALETTE_BINDING
            if active := active_bindings.get(palette_key):
                _namespace, binding, _enabled, tooltip = active
                palette_entry = binding, tooltip

        localizer = controller.localizer
        compose_revision = controller.revision
        self._rendered_key_displays = {}
        for child in super().compose():
            if isinstance(child, FooterKey):
                is_palette = child.has_class("-command-palette")
                entry = palette_entry if is_palette else visible_by_action.get(child.action)
                if entry is not None:
                    binding, active_tooltip = entry
                    grouped = child.has_class("-grouped")
                    tooltip = child.tooltip
                    display = _FooterKeyDisplay(
                        binding=binding,
                        description_literal=child.description,
                        tooltip_literal=tooltip if isinstance(tooltip, str) else "",
                        description_visible=bool(child.description),
                        tooltip_uses_description=(grouped and not active_tooltip)
                        or (is_palette and not binding.tooltip),
                    )
                    self._rendered_key_displays[child] = display
                    description, tooltip_text = self._resolve_display(display, localizer)
                    child.description = description
                    child.tooltip = tooltip_text or None
            elif isinstance(child, FooterLabel):
                content = child.content
                literal = content if isinstance(content, str) else str(content)
                child.update(Content.from_text(literal, markup=False), layout=False)
            yield self._in_corner(child)
        self._rendered_locale_revision = compose_revision

    def _in_corner(self, child: Widget) -> Widget:
        """Dock the corner key at the right end, as wide as the hook indicator it stands in for."""
        if self._corner_key is not None and isinstance(child, FooterKey) and child.key == self._corner_key:
            child.add_class("-corner")
        return child

    def _resolve_display(self, display: _FooterKeyDisplay, localizer: Localizer) -> tuple[str, str]:
        spec = resolve_binding_display(display.binding)
        if spec is None:
            return display.description_literal, display.tooltip_literal

        description_text = render_str(localizer, spec.description.bind())
        description = description_text if display.description_visible else display.description_literal
        if spec.tooltip is not None:
            tooltip = render_str(localizer, spec.tooltip.bind())
        elif display.tooltip_uses_description:
            tooltip = description_text
        else:
            tooltip = display.tooltip_literal
        return description, tooltip

    def _update_localization_in_place(self) -> None:
        controller = self._locale_controller
        if controller is None:
            return
        localizer = controller.localizer
        for key, display in self._rendered_key_displays.items():
            if not key.is_attached:
                continue
            description, tooltip = self._resolve_display(display, localizer)
            tooltip_value = tooltip or None
            if key.description != description:
                key.description = description
                key.tooltip = tooltip_value
                # ``width: auto`` only re-measures the new text on a layout
                # pass; a repaint alone would keep the old locale's width.
                key.refresh(layout=True)
            elif key.tooltip != tooltip_value:
                key.tooltip = tooltip_value
        self._rendered_locale_revision = controller.revision
        self._localization_dirty = False

    def _finish_localization_after_recompose(self) -> None:
        controller = self._locale_controller
        if controller is None:
            return
        if self._rendered_locale_revision != controller.revision:
            self._update_localization_in_place()
        else:
            self._localization_dirty = False

    def bindings_changed(self, screen: Screen) -> None:
        """Recompose only when the rendered binding set actually changed."""
        self._bindings_ready = True
        # Invalidate every queued callback before deduplication. In particular,
        # an A -> B -> A bounce must make the pending B callback stale even
        # though A already matches the last successfully rendered signature.
        self._binding_recompose_generation += 1
        generation = self._binding_recompose_generation
        if self._binding_recompose_in_progress:
            # The in-flight compose may already have read the prior binding
            # state. Its rendered result is now uncertain, even when this
            # signal happens to equal the last committed signature.
            self._binding_recompose_dirty = True
            return
        signature = self._binding_signature(screen)
        if signature == self._visible_binding_signature and not self._binding_recompose_dirty:
            return
        if not screen.app.app_focus:
            # Preserve the last rendered signature. App focus will call
            # ``sync_bindings`` so a real background change is not lost.
            return
        self._schedule_recompose(screen, generation)

    def _schedule_recompose(self, screen: Screen, generation: int) -> None:
        """Queue one generation, retaining uncertainty if delivery is lost."""
        if not self.call_after_refresh(self._recompose_bindings, screen, generation):
            # A later signal or MainScreen resume must force a retry even when
            # live bindings equal the last committed signature.
            self._binding_recompose_dirty = True
            self._binding_recompose_generation += 1

    async def _recompose_bindings(
        self,
        screen: Screen,
        generation: int,
    ) -> None:
        """Commit the dedup signature only after its recompose completes."""
        if generation != self._binding_recompose_generation:
            return
        if (
            not self.is_attached
            or screen is not self.screen
            or screen is not screen.app.screen
            or not screen.app.app_focus
        ):
            return
        # Binding signals may originate from App/Screen handlers, and
        # ``call_after_refresh`` preserves that sender context. Footer.compose
        # binds Footer.compact, so recomposition must run with this Footer as
        # the active message pump rather than ChrysApp.
        self._binding_recompose_in_progress = True
        self._binding_recompose_dirty = False
        try:
            with self._context():
                await self.recompose()
        except BaseException:
            self._binding_recompose_dirty = True
            raise
        finally:
            self._binding_recompose_in_progress = False
        self._finish_localization_after_recompose()
        if generation == self._binding_recompose_generation and not self._binding_recompose_dirty:
            # Recompose reads live bindings, so record the same live state
            # rather than the potentially stale schedule-time signature.
            self._visible_binding_signature = self._binding_signature(screen)
            return

        # A synchronous cross-pump publish landed while ``recompose`` was
        # awaiting. Force one more pass: deduplicating against the last
        # committed signature is unsafe because the just-rendered state may
        # differ from it.
        self._binding_recompose_dirty = True
        if self.is_attached and screen is self.screen and screen is screen.app.screen and screen.app.app_focus:
            self._schedule_recompose(screen, self._binding_recompose_generation)

    def sync_bindings(self) -> None:
        """Apply a binding change that may have occurred while unfocused."""
        self.bindings_changed(self.screen)

    def _binding_signature(self, screen: Screen) -> _BindingSignature:
        active = screen.active_bindings
        visible = tuple(
            (key, binding, enabled, tooltip)
            for key, (_namespace, binding, enabled, tooltip) in active.items()
            if binding.show
        )
        command_palette: _BindingEntry | None = None
        if self.show_command_palette and screen.app.ENABLE_COMMAND_PALETTE:
            palette_key = screen.app.COMMAND_PALETTE_BINDING
            if palette := active.get(palette_key):
                _namespace, binding, enabled, tooltip = palette
                command_palette = (palette_key, binding, enabled, tooltip)
        return visible, command_palette


__all__ = ["ChrysFooter"]
