# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""ConversationToc - Conversation table of contents widget."""

from __future__ import annotations

from typing import TYPE_CHECKING

from rich.segment import Segment
from rich.style import Style
from rich.text import Text
from textual.containers import Vertical
from textual.message import Message
from textual.strip import Strip
from textual.widget import Widget
from textual.widgets import Static, Tree

from chrys.app.tui.i18n import render_str
from chrys.app.tui.widgets.sidebar.empty_state import SidebarEmptyStateLabel
from chrys.app.tui.widgets.sidebar.toc_types import _SUMMARY_MAX_LENGTH, TocItem, summarize_prompt
from chrys.foundation.i18n import MessageRef, msg
from chrys.foundation.i18n.formatting import format_message

if TYPE_CHECKING:
    from textual.app import ComposeResult
    from textual.widgets.tree import TreeNode

    from chrys.app.tui.i18n import LocaleController


__all__ = ["ConversationToc", "TocItem", "summarize_prompt"]

# Rendered labels include the numeric turn prefix ("12. "), so keep the
# display cap above the default summary cap instead of silently re-clipping it.
_MAX_LABEL = _SUMMARY_MAX_LENGTH + 8
_TREE_TAB_SIZE = 8
_TOC_EMPTY = msg("tui.sidebar.toc.empty", fallback="Your conversation will appear here")
_TOC_TITLE = msg("tui.sidebar.toc.title", fallback="Messages")


def _tree_label_text(text: str) -> str:
    """Return text safe for Textual Tree's Rich render path."""
    return text.expandtabs(_TREE_TAB_SIZE)


class _TocTree(Tree[str]):
    """The tree, with its last column kept clear.

    An entry is one line, cut where the room runs out, and a long one would be cut against the
    scrollbar. Padding cannot move it away: Textual lays a widget's padding *outside* its
    scrollbar, so it would take the bar off the panel's edge rather than the text off the bar.
    """

    def render_line(self, y: int) -> Strip:
        strip = super().render_line(y)
        # A line is drawn as wide as the widget, scrollbar included, and the bar covers its end.
        last = self.scrollable_content_region.width - 1
        if not 0 < last < strip.cell_length:
            return strip
        # The clear cell keeps the row's background and its ``line`` meta, so the highlight runs
        # through it and a click on it still lands on the row.
        style = next((segment.style for segment in strip.crop(last, last + 1) if segment.style is not None), None)
        clear = None if style is None else style.background_style + Style.from_meta(style.meta)
        return Strip([*strip.crop(0, last), Segment(" ", clear), *strip.crop(last + 1)], strip.cell_length)


class ConversationToc(Widget, can_focus=False):
    """Conversation table of contents widget."""

    DEFAULT_CSS = """
    ConversationToc {
        height: 1fr;
    }
    ConversationToc > #toc-box {
        height: 1fr;
        border: round $tui-border-primary-darken-3 $inner-border-opacity;
        border-title-color: $text-muted;
        border-title-style: bold;
        padding: 0;
    }
    ConversationToc #toc-tree {
        height: 1fr;
        background: transparent;
        padding: 0 0 0 1;
        overflow-x: hidden;
        scrollbar-size: 1 1;
    }
    ConversationToc Tree > .tree--cursor {
        background: $primary 18%;
    }
    ConversationToc Tree:focus > .tree--cursor {
        color: $foreground;
        background: $primary 28%;
    }
    ConversationToc Tree:ansi > .tree--cursor {
        background: $tui-ansi-turn-background;
    }
    ConversationToc Tree:ansi:focus > .tree--cursor {
        background: $tui-ansi-turn-focus-background;
    }
    ConversationToc Tree > .tree--guides {
        color: $primary 40%;
    }
    ConversationToc .toc-empty {
        width: 1fr;
        height: 1fr;
        content-align: center middle;
        color: $text-muted;
    }
    """

    class TurnSelected(Message):
        """User selected a conversation turn."""

        def __init__(self, turn_id: str) -> None:
            super().__init__()
            self.turn_id = turn_id

    def __init__(self, *, locale_controller: LocaleController | None = None) -> None:
        super().__init__()
        self._locale_controller = locale_controller
        self._items: list[TocItem] = []
        self._tree: Tree[str] | None = None
        self._empty_label: Static = SidebarEmptyStateLabel("", classes="toc-empty")

    def compose(self) -> ComposeResult:
        box = Vertical(id="toc-box")
        box.border_title = Text(self._render_message(_TOC_TITLE.bind()))
        with box:
            self._tree = _TocTree("TOC", id="toc-tree")
            self._tree.show_root = False
            self._tree.show_guides = True
            self._tree.display = False
            yield self._tree
            self._empty_label = SidebarEmptyStateLabel(
                Text(self._render_message(_TOC_EMPTY.bind())), classes="toc-empty"
            )
            yield self._empty_label

    def refresh_localization(self) -> None:
        """Retranslate the title and empty state without rebuilding the tree."""
        if self.is_mounted:
            self.query_one("#toc-box").border_title = Text(self._render_message(_TOC_TITLE.bind()))
            self._empty_label.update(Text(self._render_message(_TOC_EMPTY.bind())))

    def _render_message(self, reference: MessageRef) -> str:
        controller = self._locale_controller
        if controller is None:
            return format_message(reference)
        return render_str(controller.localizer, reference)

    def update_items(self, items: list[TocItem]) -> None:
        self._items = items
        self._rebuild_tree()

    def update_live_items(self, items: list[TocItem]) -> None:
        """Apply live additions and removals without rebuilding retained nodes.

        Live cleanup preserves the relative order of surviving entries. Only
        the backend's turn index labels a live opener; pending openers have no
        number until TurnStarted provides it.
        """
        self._items = items
        tree = self._tree
        if tree is None:
            return
        self._empty_label.display = not items
        tree.display = bool(items)
        self._update_live_nodes(tree.root, items, top_level=True)

    def _update_live_nodes(self, parent: TreeNode[str], items: list[TocItem], *, top_level: bool) -> None:
        nodes = {node.data: node for node in parent.children}
        wanted = {item.turn_id for item in items}
        for node in tuple(parent.children):
            if node.data not in wanted:
                node.remove()
        for index, item in enumerate(items):
            label = self._item_label(item, turn_index=item.turn_index, top_level=top_level)
            node = nodes.get(item.turn_id)
            if node is None:
                node = parent.add(
                    label,
                    data=item.turn_id,
                    before=index,
                    expand=bool(item.children),
                    allow_expand=False,
                )
            else:
                if node.label != label:
                    node.set_label(label)
                if item.children and not node.children:
                    node.expand()
            self._update_live_nodes(node, item.children, top_level=False)

    @staticmethod
    def _item_label(item: TocItem, *, turn_index: int | None, top_level: bool) -> Text:
        prefix = f"{turn_index}. " if top_level and turn_index is not None else ""
        raw = _tree_label_text(prefix + item.summary)
        limit = _MAX_LABEL if top_level else _MAX_LABEL - 2
        if len(raw) > limit:
            raw = raw[: limit - 3] + "..."
        return Text(raw, style="dim") if item.compressed else Text(raw)

    def _rebuild_tree(self) -> None:
        if self._tree is None:
            return
        self._tree.clear()
        self._empty_label.display = not self._items
        self._tree.display = bool(self._items)
        for i, item in enumerate(self._items, start=1):
            label = self._item_label(
                item, turn_index=item.turn_index if item.turn_index is not None else i, top_level=True
            )
            if item.children:
                node = self._tree.root.add(label, data=item.turn_id)
                node.expand()
                node.allow_expand = False
                for child in item.children:
                    node.add_leaf(self._item_label(child, turn_index=None, top_level=False), data=child.turn_id)
            else:
                self._tree.root.add_leaf(label, data=item.turn_id)

    def on_tree_node_selected(self, event: Tree.NodeSelected[str]) -> None:
        if event.node.data is not None:
            self.post_message(self.TurnSelected(event.node.data))
