# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Pointer interaction on the guides beside transcript folds."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal
from weakref import WeakKeyDictionary

from textual.geometry import Offset
from textual.message import Message
from textual.widget import Widget

from chrys.app.tui.widgets.chat.transcript_panel import ChatTranscriptPanelMarker

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from textual.color import Color
    from textual.events import Click, MouseDown, MouseMove

    from chrys.app.tui.widgets.chat.fold_gap import FoldGap

    _WidgetBase = Widget
else:
    _WidgetBase = object


class SameCellClick(_WidgetBase):
    """A mouse gesture on a selectable fold header toggles only within one cell."""

    _pressed_cell: Offset | None = None

    def on_mouse_down(self, event: MouseDown) -> None:
        self._pressed_cell = event.screen_offset if event.button == 1 else None

    def on_mouse_move(self, event: MouseMove) -> None:
        if event.screen_offset != self._pressed_cell:
            self._pressed_cell = None

    def fold_click_at(self, origin: Widget, cell: Offset) -> bool:
        """Whether a press at this screen cell would target this widget's fold gesture."""
        return False

    def clicked_same_cell(self, event: Click) -> bool:
        pressed, self._pressed_cell = self._pressed_cell, None
        return event.button == 1 and event.screen_offset == pressed


@dataclass
class GuideFoldRequested(Message):
    """Fold the member, or its containing process, at the mouse's screen row."""

    member: GuideFoldMixin
    process: bool
    screen_y: int


@dataclass(frozen=True)
class _Guide:
    painters: tuple[Widget, ...]
    title: Widget
    member: GuideFoldMixin
    process: bool = False
    gap: FoldGap | None = None
    clickable: bool = True


@dataclass
class _Lit:
    border: tuple[str, Color] | None
    holders: set[int]


_LIT: WeakKeyDictionary[Widget, _Lit] = WeakKeyDictionary()
_GAP_LIT: WeakKeyDictionary[FoldGap, set[int]] = WeakKeyDictionary()


def _light(painter: Widget, holder: object, color: Color, *, resample: bool) -> None:
    from chrys.app.tui.widgets.chat.fold_gap import FoldGap

    styles = painter.styles
    lit = _LIT.get(painter)
    if lit is None:
        inline = styles.inline
        lit = _LIT[painter] = _Lit(inline.border_left if inline.has_rule("border_left") else None, set())
        resample = True
    if resample:
        edge, original = lit.border or styles.base.border_left
        if isinstance(painter, FoldGap):
            painter.gap_base_color = original
        styles.set_rule("border_left", (edge, color))
        painter.refresh(layout=False)
    lit.holders.add(id(holder))


def _unlight(painter: Widget, holder: object) -> None:
    from chrys.app.tui.widgets.chat.fold_gap import FoldGap

    lit = _LIT.get(painter)
    if lit is None:
        return
    lit.holders.discard(id(holder))
    if not lit.holders:
        del _LIT[painter]
        if isinstance(painter, FoldGap):
            painter.gap_base_color = None
        if painter.styles.set_rule("border_left", lit.border):
            painter.refresh(layout=False)


def _light_guide(guide: _Guide, holder: object, *, resample: bool = False) -> None:
    color = guide.title.visual_style.foreground
    if color is None:
        return
    if (gap := guide.gap) is not None:
        holders = _GAP_LIT.setdefault(gap, set())
        if resample or not holders:
            gap.set_gap_hover(color)
        holders.add(id(holder))
    else:
        for painter in guide.painters:
            _light(painter, holder, color, resample=resample)


def _unlight_guide(guide: _Guide, holder: object) -> None:
    if (gap := guide.gap) is not None:
        holders = _GAP_LIT.get(gap)
        if holders is not None:
            holders.discard(id(holder))
            if not holders:
                del _GAP_LIT[gap]
                gap.set_gap_hover(None)
    else:
        for painter in guide.painters:
            _unlight(painter, holder)


def _member_guide(member: GuideFoldMixin, *, clickable: bool) -> _Guide | None:
    from chrys.app.tui.widgets.chat.fold_gap import FoldGap

    if not member.display or not member.can_toggle_from_guide():
        return None
    title = member.guide_title()
    body = member.guide_surface()
    if body is not None:
        return _Guide((body,), title, member, clickable=clickable)
    if not isinstance(member, FoldGap) or not member.ends_folded() or member.parent is None:
        return None
    entries = member.parent.displayed_children
    index = entries.index(member)
    following = entries[index + 1] if index + 1 < len(entries) else None
    if isinstance(following, FoldGap) and following.gap_above_from is member:
        return _Guide((), title, member, gap=following, clickable=clickable)
    return None


type _GuideKey = Literal["process", "process_title", "member_title", "member_body"] | GuideFoldMixin


class GuideHoverMixin(SameCellClick):
    """Share guide hover ownership between titles, bodies and gap painters."""

    _guide_hover: _Guide | None = None
    _guide_hover_key: _GuideKey | None = None
    _guide_pointer: object = None

    def _guide_key_at(self, cell: Offset) -> _GuideKey | None:
        from chrys.app.tui.widgets.chat.fold_gap import FoldGap

        if not isinstance(self, FoldGap) or not self.display or cell not in self.region:
            return None
        content = self.content_region
        member = self.gap_above_from if cell.y == content.y else None
        if isinstance(member, GuideFoldMixin):
            gap_x = content.x + self.gap_guide_offset()
            if gap_x <= cell.x < gap_x + 2:
                return member
        return None

    def _guide_for_key(self, key: _GuideKey) -> _Guide | None:
        if isinstance(key, GuideFoldMixin) and key.display and key.can_toggle_from_guide():
            from chrys.app.tui.widgets.chat.fold_gap import FoldGap

            if isinstance(self, FoldGap):
                return _Guide((), key.guide_title(), key, gap=self)
        return None

    def _guide_at(self, cell: Offset) -> _Guide | None:
        key = self._guide_key_at(cell)
        return self._guide_for_key(key) if key is not None else None

    def fold_click_at(self, origin: Widget, cell: Offset) -> bool:
        # Forwarded guide clicks can start on the entry or its own guide surface.
        if origin is not self and not (isinstance(origin, GuideSurfaceMixin) and origin._guide_owner() is self):
            return False
        guide = self._guide_at(cell)
        return guide is not None and guide.clickable

    def _click_guide(self, event: Click) -> bool:
        guide = self._guide_at(event.screen_offset)
        if guide is None or not guide.clickable:
            return False
        event.prevent_default()
        event.stop()
        if self.clicked_same_cell(event) and event.chain == 1:
            self.clear_guide_hover()
            self.post_message(GuideFoldRequested(guide.member, guide.process, event.screen_y))
        return True

    def on_click(self, event: Click) -> None:
        self._click_guide(event)

    def on_mouse_move(self, event: MouseMove) -> None:
        self._update_guide_hover(event.screen_offset)

    def on_leave(self) -> None:
        mouse_over = self.app.mouse_over
        if mouse_over is None or self not in mouse_over.ancestors_with_self:
            self.clear_guide_hover()

    def on_resize(self) -> None:
        # Opening a member moves its guide from the gap to its body under the same stationary pointer.
        self.refresh_guide_hover()

    def on_hide(self) -> None:
        self.clear_guide_hover()

    def on_unmount(self) -> None:
        self.clear_guide_hover()

    def refresh_guide_hover(self) -> None:
        mouse_over = self.app.mouse_over
        if mouse_over is not None and self in mouse_over.ancestors_with_self:
            self._update_guide_hover(self.app.mouse_position, force=True)
        else:
            self.clear_guide_hover()

    def _recolor_hovered_guide(self, _theme: object) -> None:
        if (guide := self._guide_hover) is not None:
            _light_guide(guide, self, resample=True)

    def _update_guide_hover(self, cell: Offset, *, force: bool = False) -> None:
        key = self._guide_key_at(cell)
        if not force and key == self._guide_hover_key:
            return
        self._guide_hover_key = key
        guide = self._guide_for_key(key) if key is not None else None
        old = self._guide_hover
        if guide == old:
            return
        # Enter the next piece before releasing the old one: shared painters keep their holders.
        if guide is not None:
            if old is None:
                self._guide_pointer = self.styles.inline.get_rule("pointer", None)
                self.app.theme_changed_signal.subscribe(self, self._recolor_hovered_guide)
            _light_guide(guide, self)
        self._guide_hover = guide
        if old is not None:
            # The same holder can light overlapping guides. Release only the pieces it left.
            if old.gap is not None:
                if guide is None or old.gap is not guide.gap:
                    _unlight_guide(old, self)
            else:
                next_painters = set(guide.painters) if guide is not None else set()
                for painter in old.painters:
                    if painter not in next_painters:
                        _unlight(painter, self)
        if guide is None:
            self.app.theme_changed_signal.unsubscribe(self)
            self.styles.set_rule("pointer", self._guide_pointer)
            self._guide_pointer = None
        else:
            self.styles.set_rule("pointer", "pointer")
        self.screen.update_pointer_shape()

    def clear_guide_hover(self) -> None:
        self._guide_hover_key = None
        guide, self._guide_hover = self._guide_hover, None
        if guide is None:
            return
        self.app.theme_changed_signal.unsubscribe(self)
        _unlight_guide(guide, self)
        self.styles.set_rule("pointer", self._guide_pointer)
        self._guide_pointer = None
        if self.is_attached:
            self.screen.update_pointer_shape()


class GuideFoldMixin(GuideHoverMixin):
    """Hit-test a member's title and the process and member guides beside it."""

    def guide_surface(self) -> Widget | None:
        raise NotImplementedError

    def guide_title(self) -> Widget:
        return self

    def can_toggle_from_guide(self) -> bool:
        """Whether its title and guide can perform the same toggle."""
        raise NotImplementedError

    def fold_from_guide(self) -> None:
        raise NotImplementedError

    def fold_click_at(self, origin: Widget, cell: Offset) -> bool:
        return (self.can_toggle_from_guide() and origin is self.guide_title()) or super().fold_click_at(origin, cell)

    def fold_member_guide(self, parent: Widget, *, before_fold: Callable[[], None] | None = None) -> None:
        if self.parent is parent and self.display and self.can_toggle_from_guide():
            if before_fold is not None:
                before_fold()
            self.fold_from_guide()

    def _guide_key_at(self, cell: Offset) -> _GuideKey | None:
        if not self.display or cell not in self.region:
            return None
        if (gap := super()._guide_key_at(cell)) is not None:
            return gap
        if cell.x < self.region.x + 2 and isinstance(self.parent, ChatTranscriptPanelMarker):
            return "process"
        if cell in self.guide_title().region:
            return "member_title"
        body = self.guide_surface()
        if body is not None and cell in body.region and cell.x < body.region.x + 2:
            return "member_body"
        return None

    def _guide_for_key(self, key: _GuideKey) -> _Guide | None:
        if key == "process":
            parent = self.parent
            if isinstance(parent, ChatTranscriptPanelMarker):
                process = parent.process_guide_for(self)
                if isinstance(process, ProcessGuideMixin):
                    return process._process_guide(self)
        elif key == "member_title" or key == "member_body":
            return _member_guide(self, clickable=key == "member_body")
        return super()._guide_for_key(key)


class ProcessGuideMixin(GuideHoverMixin):
    """An open process title lights the same outer guide as its members."""

    def guide_members(self) -> Sequence[Widget]:
        raise NotImplementedError

    def _process_guide(self, member: GuideFoldMixin, *, clickable: bool = True) -> _Guide:
        parent = self.parent
        painters = tuple(entry for entry in self.guide_members() if entry.display and entry.parent is parent)
        return _Guide(painters, self, member, process=True, clickable=clickable)

    def fold_click_at(self, origin: Widget, cell: Offset) -> bool:
        return (origin is self and self._process_title_at(cell)) or super().fold_click_at(origin, cell)

    def _process_title_at(self, cell: Offset) -> bool:
        from chrys.app.tui.widgets.chat.fold_gap import FOLD_GAP_ROWS, FoldGap

        title_y = self.content_region.y + (FOLD_GAP_ROWS if isinstance(self, FoldGap) and self.gap_above else 0)
        return self.display and cell in self.region and cell.y == title_y

    def _guide_key_at(self, cell: Offset) -> _GuideKey | None:
        if (gap := super()._guide_key_at(cell)) is not None:
            return gap
        return "process_title" if self._process_title_at(cell) else None

    def _guide_for_key(self, key: _GuideKey) -> _Guide | None:
        if key == "process_title":
            member = next((entry for entry in self.guide_members() if isinstance(entry, GuideFoldMixin)), None)
            parent = self.parent
            if (
                member is not None
                and isinstance(parent, ChatTranscriptPanelMarker)
                and parent.process_guide_for(member) is self
            ):
                return self._process_guide(member, clickable=False)
        return super()._guide_for_key(key)


class GuideSurfaceMixin(_WidgetBase):
    """Let a fold guide consume clicks before Textual selects the body text."""

    def _guide_owner(self) -> GuideFoldMixin | None:
        return next((node for node in self.ancestors if isinstance(node, GuideFoldMixin)), None)

    def focus_on_click(self) -> bool:
        owner = self._guide_owner()
        return (owner is None or owner._guide_at(self.app.mouse_position) is None) and super().focus_on_click()

    def on_mouse_down(self, event: MouseDown) -> None:
        owner = self._guide_owner()
        if owner is not None and owner._guide_at(event.screen_offset) is not None:
            owner.on_mouse_down(event)
            event.stop()

    def on_click(self, event: Click) -> None:
        if (owner := self._guide_owner()) is not None:
            owner._click_guide(event)
