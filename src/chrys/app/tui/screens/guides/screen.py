# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""GuideDialog — modal user-guide manual with topic tree and markdown viewer.

The left pane is a topic tree driven by ``docs/index.yaml`` (see
``chrys.app.tui.screens.guides.index``); the right pane renders the active
topic's markdown read live from disk.  Press Space to cycle the document
language (``docs/index.yaml`` ``locales`` order); the initial
language follows the app locale.  Tree labels always follow the app locale;
Space affects only the document body.  Manual language picks live only on
this dialog instance — they are never persisted and never touch global
settings.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, cast

from rich.text import Text
from textual import on
from textual.containers import Horizontal, Vertical
from textual.widgets import Tree

from chrys.app.tui.binding_display import CLOSE_BINDING, localized_binding
from chrys.app.tui.i18n import render_str, widget_localizer
from chrys.app.tui.language import language_label
from chrys.app.tui.screens.dialogs.base import BaseDialog
from chrys.app.tui.screens.guides.index import (
    INDEX_FILENAME,
    GuideIndex,
    GuideIndexError,
    GuideTopic,
    default_topic_id,
    iter_leaf_topics,
    language_cycle,
    load_guide_index,
)
from chrys.app.tui.screens.guides.reader import (
    branch_display_name,
    extract_h1,
    read_topic_markdown,
    resolve_guide_link,
    topic_display_name,
)
from chrys.app.tui.widgets import HatchedEmptyState
from chrys.app.tui.widgets.markdown.widget import VirtualizedMarkdown
from chrys.foundation.branding import APP_DISPLAY_NAME
from chrys.foundation.documentation import resolve_docs_root
from chrys.foundation.i18n import MessageRef, msg
from chrys.foundation.i18n.locale import ENGLISH_LOCALE

if TYPE_CHECKING:
    from textual.app import ComposeResult
    from textual.widgets._tree import TreeNode

    from chrys.app.tui.i18n import LocaleController


_GUIDE_TITLE = msg("tui.guides.title", fallback="{app} User Guide")
_GUIDE_CYCLE_LANGUAGE = msg("tui.guides.cycle_language", fallback="Language")
_GUIDE_LANGUAGE_HINT = msg(
    "tui.guides.language_hint",
    fallback="Language: {language} (Space to switch)",
)
_GUIDE_EMPTY_CONTENT = msg("tui.guides.empty_content", fallback="No content")
_GUIDE_DOCS_UNAVAILABLE = msg("tui.guides.docs_unavailable", fallback="Guide documents not found")
_GUIDE_INDEX_ERROR = msg("tui.guides.index_error", fallback="Guide index could not be loaded")


class GuideDialog(BaseDialog[None]):
    """Modal showing the user guide manual read from the docs directory."""

    CSS_PATH = "screen.tcss"

    BINDINGS: ClassVar[list] = [
        localized_binding("escape", "dismiss", CLOSE_BINDING, show=False, priority=True),
        localized_binding("q", "dismiss", CLOSE_BINDING, show=False),
        # priority=True: Textual's Tree binds space to toggle_node; the dialog
        # language cycle must win regardless of focus (same pattern as the log
        # viewer's level cycle).
        localized_binding("space", "cycle_language", _GUIDE_CYCLE_LANGUAGE, show=True, priority=True),
    ]

    def __init__(
        self,
        *,
        locale_controller: LocaleController | None = None,
        docs_root: Path | None = None,
    ) -> None:
        super().__init__()
        self._locale_controller = locale_controller
        self._docs_root = docs_root if docs_root is not None else resolve_docs_root()
        self._index: GuideIndex | None = None
        self._index_error = False
        if self._docs_root is not None:
            if not (self._docs_root / INDEX_FILENAME).is_file():
                # No index at all: the docs are simply unavailable (distinct
                # from an index that exists but cannot be parsed).
                self._index = None
            else:
                try:
                    self._index = load_guide_index(self._docs_root)
                except GuideIndexError:
                    self._index_error = True
        self._cycle = language_cycle(self._index) if self._index is not None else ()
        self._lang_index = self._initial_language_index()
        # The UI language (tree labels, chrome) always follows the global
        # locale; Space cycles only the document language (_lang_index).
        self._ui_language = self._cycle[self._lang_index] if self._cycle else ""
        self._manual_language = False
        self._active_topic: GuideTopic | None = None
        self._pending_anchor: str | None = None
        self._tree: Tree[GuideTopic] | None = None
        self._markdown: VirtualizedMarkdown | None = None
        self._empty: HatchedEmptyState | None = None
        self._pending_load: asyncio.Task | None = None
        self._load_generation = 0

    # ------------------------------------------------------------------
    # Compose / lifecycle
    # ------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        with Vertical(id="gd-container"), Horizontal(id="gd-body"):
            yield Tree[GuideTopic]("", id="gd-tree")
            yield VirtualizedMarkdown(id="gd-doc")
            yield HatchedEmptyState("", id="gd-empty")

    def on_mount(self) -> None:
        if self._locale_controller is not None:
            self._locale_controller.register_surface(self)
        self._tree = cast(Tree[GuideTopic], self.query_one("#gd-tree", Tree))
        self._tree.show_root = False
        self._tree.show_guides = True
        self._markdown = self.query_one("#gd-doc", VirtualizedMarkdown)
        self._empty = self.query_one("#gd-empty", HatchedEmptyState)
        if self._index is None:
            self._enter_error_state()
            return
        # The default topic becomes active before the tree is built, so the
        # rebuild highlights it like any other active topic.
        self._active_topic = self._default_leaf()
        self._rebuild_tree()
        self._tree.focus()
        if self._active_topic is None:
            self._show_empty()
        else:
            task = asyncio.create_task(self._load_document())
            self._pending_load = None if task.done() else task
        self._update_chrome()

    def on_unmount(self) -> None:
        if self._locale_controller is not None:
            self._locale_controller.unregister_surface(self)
        if self._pending_load is not None and not self._pending_load.done():
            self._pending_load.cancel()
        self._pending_load = None

    def _enter_error_state(self) -> None:
        """Show a localized error state when docs or the index are unavailable."""
        if self._tree is None or self._markdown is None or self._empty is None:
            raise RuntimeError("The guide widgets have not been mounted.")
        self._tree.display = False
        self._markdown.display = False
        ref = _GUIDE_INDEX_ERROR.bind() if self._index_error else _GUIDE_DOCS_UNAVAILABLE.bind()
        self._empty.update_label(self._render_message(ref))
        self._empty.display = True
        self._update_chrome()

    # ------------------------------------------------------------------
    # Language state
    # ------------------------------------------------------------------

    def _initial_language_index(self) -> int:
        """Follow the app locale; English (the UI's own language) when the docs lack it."""
        if not self._cycle:
            return 0
        controller = self._locale_controller
        if controller is not None and controller.localizer.effective_locale in self._cycle:
            return self._cycle.index(controller.localizer.effective_locale)
        if ENGLISH_LOCALE in self._cycle:
            return self._cycle.index(ENGLISH_LOCALE)
        return 0

    def _current_language(self) -> str:
        """The document language — what Space cycles and the body reads."""
        return self._cycle[self._lang_index] if self._cycle else ""

    # ------------------------------------------------------------------
    # Topic tree
    # ------------------------------------------------------------------

    def _require_tree(self) -> Tree[GuideTopic]:
        tree = self._tree
        if tree is None:
            raise RuntimeError("The guide tree has not been mounted.")
        return tree

    def _rebuild_tree(self) -> None:
        tree = self._require_tree()
        if self._index is None:
            raise RuntimeError("Rebuilding the guide tree requires mounted widgets and a loaded index.")
        tree.clear()
        target = self._active_topic
        for topic in self._index.topics:
            self._add_topic(tree.root, topic)
        # Keep the active topic highlighted across rebuilds (first build,
        # global locale switch).
        if target is not None:
            selected = self._find_topic_node(tree.root, target)
            if selected is not None:
                self._highlight_after_refresh(selected)

    def _highlight_after_refresh(self, node: TreeNode[GuideTopic]) -> None:
        """Move the tree cursor to *node* once a rebuilt or expanded tree is laid out.

        Scrolling it into view before then uses the old height.  The move is
        dropped when another topic became active in the meantime, so a late
        callback cannot pull the highlight away from the document on screen.
        """
        tree = self._require_tree()

        def move() -> None:
            if node.data is self._active_topic:
                self._move_cursor_to(node)

        tree.call_after_refresh(move)

    def _move_cursor_to(self, node: TreeNode[GuideTopic]) -> None:
        """Put the tree cursor on *node* now, scrolling it into view.

        ``Tree.move_cursor`` reads ``node.line`` before the tree numbers the
        rows an add/clear/expand left pending, which happens only when it
        renders or idles.  A stale line (-1 for a new node) lands the cursor on
        whatever row it clamps to, so read ``last_line`` first to number them.
        """
        tree = self._require_tree()
        _ = tree.last_line
        tree.move_cursor(node)

    def _find_topic_node(self, node: TreeNode[GuideTopic], topic: GuideTopic) -> TreeNode[GuideTopic] | None:
        """Locate the tree node whose data is *topic* (object identity)."""
        for child in node.children:
            if child.data is topic:
                return child
            found = self._find_topic_node(child, topic)
            if found is not None:
                return found
        return None

    def _add_topic(self, parent: TreeNode[GuideTopic], topic: GuideTopic) -> None:
        label = Text(self._topic_label(topic))
        if topic.is_branch:
            node = parent.add(label, data=topic)
            node.expand()
            for child in topic.children:
                self._add_topic(node, child)
        else:
            parent.add_leaf(label, data=topic)

    def _topic_label(self, topic: GuideTopic) -> str:
        """Tree labels resolve in the UI language — never the Space-cycled
        document language."""
        index = self._index
        if index is None or self._docs_root is None or not topic.path:
            return branch_display_name(topic, locale=self._ui_language, default_locale=self._ui_language)
        current_h1 = extract_h1(read_topic_markdown(self._docs_root, index, topic, self._ui_language))
        return topic_display_name(
            topic,
            locale=self._ui_language,
            default_locale=self._ui_language,
            current_h1=current_h1,
        )

    @on(Tree.NodeSelected)
    async def on_tree_node_selected(self, event: Tree.NodeSelected) -> None:
        topic = event.node.data
        if topic is None or topic.path is None:
            return
        if topic is self._active_topic:
            # Re-selecting the document on screen keeps its scroll position:
            # a reload would jump back to the top.
            return
        self._cancel_pending_load()
        self._active_topic = topic
        await self._load_document()

    @on(VirtualizedMarkdown.LinkClicked)
    async def _on_doc_link_clicked(self, event: VirtualizedMarkdown.LinkClicked) -> None:
        """Navigate in-dialog to the topic a relative markdown link targets.

        Page-internal ``#anchor`` links are already consumed by the widget's
        built-in handler; external URLs and links outside the topic tree are
        ignored.  A hit moves the tree cursor (highlight + scroll into view)
        and loads the target document, honoring any ``#fragment`` after the
        render completes.
        """
        index = self._index
        topic = self._active_topic
        if index is None or topic is None or self._tree is None:
            return
        target, anchor = resolve_guide_link(index, topic, event.href)
        if target is None:
            return
        node = self._find_topic_node(self._tree.root, target)
        if node is None:
            return
        self._cancel_pending_load()
        self._active_topic = target
        # The cursor row gets the .tree--cursor highlight and the tree
        # scrolls it into view.  A target under a collapsed branch has no
        # row until its ancestors are expanded, and the wider labels that
        # brings can add a horizontal scrollbar, so it scrolls only once the
        # tree has laid out again.  The hidden root stays unexpanded yet
        # never hides its children, so the walk stops below it.
        collapsed = False
        parent = node.parent
        while parent is not None and parent is not self._tree.root:
            if not parent.is_expanded:
                parent.expand()
                collapsed = True
            parent = parent.parent
        if collapsed:
            self._highlight_after_refresh(node)
        else:
            self._move_cursor_to(node)
        self._pending_anchor = anchor or None
        await self._load_document()

    # ------------------------------------------------------------------
    # Document loading
    # ------------------------------------------------------------------

    def _cancel_pending_load(self) -> None:
        """Cancel an in-flight background load (e.g. the on-mount default
        topic): its continuation would clobber freshly switched state."""
        if self._pending_load is not None and not self._pending_load.done():
            self._pending_load.cancel()
        self._pending_load = None

    def _default_leaf(self) -> GuideTopic | None:
        index = self._index
        if index is None:
            return None
        default = default_topic_id(index)
        return next((topic for topic_id, topic in iter_leaf_topics(index.topics) if topic_id == default), None)

    def _read_active_markdown(self) -> str | None:
        index = self._index
        topic = self._active_topic
        if index is None or self._docs_root is None or topic is None:
            return None
        return read_topic_markdown(self._docs_root, index, topic, self._current_language())

    async def _load_document(self) -> None:
        if self._markdown is None or self._empty is None:
            return
        # Loads overlap: a topic selection runs in this screen's message pump
        # while Space runs in the App's.  Only the newest load may touch the
        # panes, scroll and anchor once its render returns.
        self._load_generation += 1
        generation = self._load_generation
        markdown = self._read_active_markdown()
        if markdown is None:
            # A stale anchor from a link click must not fire on a later
            # successful load (e.g. after cycling back to a language that
            # has content).
            self._pending_anchor = None
            self._show_empty()
            return
        await self._markdown.update(markdown)
        if not self.is_mounted or generation != self._load_generation:
            return
        self._empty.display = False
        self._markdown.display = True
        self._markdown.scroll_to(y=0, animate=False)
        if self._pending_anchor is not None:
            anchor, self._pending_anchor = self._pending_anchor, None
            self._markdown.goto_anchor(anchor)

    def _show_empty(self) -> None:
        if self._markdown is None or self._empty is None:
            return
        self._markdown.display = False
        self._empty.update_label(self._render_message(_GUIDE_EMPTY_CONTENT.bind()))
        self._empty.display = True

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    async def action_cycle_language(self) -> None:
        if not self._cycle or self._index is None:
            return
        # Manual picks are instance-only: never persisted, never applied to
        # the global locale, and they stop following global locale switches.
        self._manual_language = True
        self._lang_index = (self._lang_index + 1) % len(self._cycle)
        # Only the document body switches language — tree labels and chrome
        # stay in the UI language, so nothing needs rebuilding.
        self._cancel_pending_load()
        await self._load_document()
        self._update_chrome()

    def refresh_localization(self) -> None:
        """Follow the global locale until the user cycles language manually.

        Tree labels always follow the global locale; the document body
        follows it too until the user cycles it manually.
        """
        if self._index is None:
            self._enter_error_state()
            return
        if self._cycle:
            self._ui_language = self._cycle[self._initial_language_index()]
        self._rebuild_tree()
        if not self._manual_language:
            self._lang_index = self._initial_language_index()
            self._cancel_pending_load()
            task = asyncio.create_task(self._load_document())
            self._pending_load = None if task.done() else task
        self._update_chrome()

    # ------------------------------------------------------------------
    # Chrome
    # ------------------------------------------------------------------

    def _update_chrome(self) -> None:
        container = self.query_one("#gd-container", Vertical)
        container.border_title = Text(self._render_message(_GUIDE_TITLE.bind(app=APP_DISPLAY_NAME)))
        language = self._current_language()
        if not language:
            container.border_subtitle = Text("")
            return
        label = language_label(language)
        language_name = self._render_message(label.bind()) if label is not None else language
        container.border_subtitle = Text(self._render_message(_GUIDE_LANGUAGE_HINT.bind(language=language_name)))

    def _render_message(self, reference: MessageRef) -> str:
        return render_str(widget_localizer(self), reference)
