# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""SidebarPanel — toggleable tabbed sidebar."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar, Literal, NamedTuple

from rich.cells import cell_len
from textual.content import Content
from textual.events import Click
from textual.geometry import Region
from textual.message import Message
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Tab, TabbedContent, TabPane, Tabs

from chrys.app.tui.i18n import render_content
from chrys.app.tui.widgets.click_affordance import ClickAffordance
from chrys.app.tui.widgets.sidebar.buddy import BuddyPanel
from chrys.app.tui.widgets.sidebar.context import ContextPanel, ContextUsageState
from chrys.app.tui.widgets.sidebar.debug import DebugPanel
from chrys.app.tui.widgets.sidebar.diff_preview import DiffPreview, SidebarChatPanel
from chrys.app.tui.widgets.sidebar.hooks import HookRunList
from chrys.app.tui.widgets.sidebar.tasks import TasksPanel, TodoListState
from chrys.app.tui.widgets.sidebar.toc import ConversationToc
from chrys.foundation.i18n import MessageDef, MessageRef, msg
from chrys.foundation.i18n.formatting import format_message

_SIDEBAR_CHAT = msg("tui.sidebar.tab.chat", fallback="Chat")
_SIDEBAR_TASKS = msg("tui.sidebar.tab.tasks", fallback="Tasks")
_SIDEBAR_HOOKS = msg("tui.sidebar.tab.hooks", fallback="Hooks")
_SIDEBAR_CONTEXT = msg("tui.sidebar.tab.context", fallback="Context")
_SIDEBAR_DEBUG = msg("tui.sidebar.tab.debug", fallback="Debug")
_SIDEBAR_BUDDY = msg("tui.sidebar.tab.buddy", fallback="Buddy")
_SIDEBAR_TAB_LABELS: tuple[MessageDef, ...] = (
    _SIDEBAR_CHAT,
    _SIDEBAR_TASKS,
    _SIDEBAR_CONTEXT,
    _SIDEBAR_HOOKS,
    _SIDEBAR_DEBUG,
    _SIDEBAR_BUDDY,
)
SIDEBAR_HOOKS_TAB = "tab-hooks"
# Workflow runs build their own hook managers, which the run log does not record.
_WORKFLOW_HIDDEN_TABS = ("tab-toc", "tab-tasks", SIDEBAR_HOOKS_TAB, "tab-context")
_TAB_STRIP_SCROLL_CELLS = 4
"""How far one wheel step moves the tab strip."""
_MARK_CELLS = 2
"""Cells an overflow mark keeps at each end of the strip (its ``width``, and the strip's ``-overflow`` margin)."""
_TAB_PADDING = 1
"""Textual's ``Tab`` padding on each side, dropped on a strip too narrow to keep it."""

if TYPE_CHECKING:
    from textual import events
    from textual.app import ComposeResult
    from textual.events import MouseScrollDown, MouseScrollLeft, MouseScrollRight, MouseScrollUp

    from chrys.app.tui.i18n import LocaleController
    from chrys.foundation.models.background_tasks import BackgroundTaskView


def _sure_character_view(character: int) -> int:
    """The narrowest unpadded strip that shows a whole character wherever it is scrolled.

    One cell less than two characters: any narrower, a strip can sit across two halves.
    """
    return 2 * character - 1


class _TabSpan(NamedTuple):
    """Where one shown tab lies along the tab strip, in cells from its start."""

    start: int
    label_start: int
    label_end: int
    end: int
    label: str

    def characters(self) -> list[tuple[int, int]]:
        """Where each character of the label starts and ends."""
        edges: list[tuple[int, int]] = []
        x = self.label_start
        for char in self.label:
            edges.append((x, x + cell_len(char)))
            x += cell_len(char)
        return edges


class TabOverflowMark(ClickAffordance):
    """One edge of the tab strip: a clickable chevron that shifts hidden tabs into view.

    While the tabs are wider than the sidebar, the cells stay reserved and only
    their content appears and disappears, so scrolling never moves the tabs under
    a mark. When every tab fits, the marks and their cells go away. The chevron
    keeps one blank cell toward the tabs; its colour is the unselected tab's
    (``chrys.tcss``).
    """

    DEFAULT_CSS = """
    TabOverflowMark {
        width: 2;
        height: 1;
    }
    TabOverflowMark.-active {
        pointer: pointer;
    }
    """

    class Pressed(Message):
        """A visible mark was clicked."""

        def __init__(self, direction: Literal["left", "right"]) -> None:
            super().__init__()
            self.direction = direction

    ALLOW_SELECT = False

    _GLYPHS: ClassVar[dict[str, str]] = {"left": "\u276e ", "right": " \u276f"}

    def __init__(self, direction: Literal["left", "right"], *, id: str | None = None) -> None:
        super().__init__("", id=id)
        self.direction = direction
        self._active = False

    @property
    def active(self) -> bool:
        """Whether this side currently hides a tab."""
        return self._active

    def set_active(self, active: bool) -> None:
        """Show or clear the glyph without moving or resizing the cell."""
        if active == self._active:
            return
        self._active = active
        self.set_class(active, "-active")
        self.update(self._GLYPHS[self.direction] if active else "", layout=False)

    def set_state(self, *, reserved: bool, active: bool) -> None:
        """Keep or give up the mark's cells, and show the glyph while that side hides a tab.

        Hidden, not removed: the mark sits on its own layer, so it takes no room
        from the tabs either way.
        """
        shown_again = reserved and not self.visible
        self.visible = reserved
        self.set_active(active)
        if shown_again:
            # Textual applies the visibility on its next refresh, and a glyph set
            # meanwhile is not repainted on its own: repaint once it is visible.
            self.call_after_refresh(self.refresh)

    def on_click(self, event: Click) -> None:
        if not self._active:
            return
        event.prevent_default()
        event.stop()
        self.post_message(self.Pressed(self.direction))


class SidebarPanel(Widget, can_focus=False):
    """Toggleable sidebar with tabbed panels."""

    context_usage_state: reactive[ContextUsageState | None] = reactive(None, always_update=True)
    todo_state: reactive[TodoListState | None] = reactive(None, always_update=True)
    background_tasks: reactive[tuple[BackgroundTaskView, ...]] = reactive(())

    DEFAULT_CSS = """
    SidebarPanel {
        layers: content marks;
        width: 30%;
        min-width: 42;
        min-height: 3;
        max-width: 60;
        border: round $tui-border-primary $border-opacity;
    }
    SidebarPanel.-shell-active {
        border: round $tui-border-warning $border-opacity;
    }
    /* Here, not in the boxes' own CSS: Textual scopes a widget's rule to under the widget. */
    SidebarPanel.-shell-active DiffPreview > #diff-preview-box,
    SidebarPanel.-shell-active ConversationToc > #toc-box,
    SidebarPanel.-shell-active TasksPanel > #tasks-todos,
    SidebarPanel.-shell-active BackgroundTaskList {
        border: round $tui-border-warning-darken-1 $inner-border-opacity;
    }
    SidebarPanel > TabbedContent {
        layer: content;
        height: 100%;
    }
    SidebarPanel > TabbedContent > Tabs.-tight Tab {
        padding: 0;
    }
    SidebarPanel > TabbedContent > Tabs.-overflow {
        margin: 0 2;
    }
    SidebarPanel > TabOverflowMark {
        layer: marks;
    }
    SidebarPanel > #sidebar-tabs-overflow-left {
        dock: left;
    }
    SidebarPanel > #sidebar-tabs-overflow-right {
        dock: right;
    }
    SidebarPanel TabPane {
        height: 1fr;
        padding: 0;
    }
    """

    def __init__(self, *, locale_controller: LocaleController | None = None) -> None:
        super().__init__()
        self._locale_controller = locale_controller
        self._visibility_suppressors: set[str] = set()
        self._display_before_suppression = True
        self._room: int | None = None
        self._workflow_mode = False
        self._tab_strip_focusable = False
        self._chat_tab = "tab-toc"

    def compose(self) -> ComposeResult:
        with TabbedContent(initial="tab-toc"):
            with TabPane(self._localized_content(_SIDEBAR_CHAT.bind()), id="tab-toc"):
                yield SidebarChatPanel(locale_controller=self._locale_controller)
            with TabPane(self._localized_content(_SIDEBAR_TASKS.bind()), id="tab-tasks"):
                yield TasksPanel(locale_controller=self._locale_controller).data_bind(
                    todo_state=SidebarPanel.todo_state,
                    background_tasks=SidebarPanel.background_tasks,
                )
            with TabPane(self._localized_content(_SIDEBAR_CONTEXT.bind()), id="tab-context"):
                yield ContextPanel(locale_controller=self._locale_controller).data_bind(
                    usage_state=SidebarPanel.context_usage_state
                )
            with TabPane(self._localized_content(_SIDEBAR_HOOKS.bind()), id=SIDEBAR_HOOKS_TAB):
                yield HookRunList(locale_controller=self._locale_controller)
            with TabPane(self._localized_content(_SIDEBAR_DEBUG.bind()), id="tab-debug"):
                yield DebugPanel(locale_controller=self._locale_controller)
            with TabPane(self._localized_content(_SIDEBAR_BUDDY.bind()), id="tab-buddy"):
                yield BuddyPanel(locale_controller=self._locale_controller)
        yield TabOverflowMark("left", id="sidebar-tabs-overflow-left")
        yield TabOverflowMark("right", id="sidebar-tabs-overflow-right")

    def on_mount(self) -> None:
        if self._locale_controller is not None:
            self._locale_controller.register_surface(self)
        tabs = self.query_one(TabbedContent).query_one(Tabs)
        tabs.can_focus = self._tab_strip_focusable
        strip = self._tab_strip()
        # The strip hides its overflow, so it has no scrollbar and no scroll
        # message of its own: watch what its scroll and size become instead of
        # polling the geometry every frame.
        self.watch(strip, "scroll_x", self._sync_tab_overflow, init=False)
        self.watch(strip, "virtual_size", self._sync_tab_overflow, init=False)
        self.refresh_localization()

    def set_tab_strip_focusable(self, focusable: bool) -> None:
        """Let the tab strip take focus only where the main screen lets sidebar focus stay.

        Where the main screen hands sidebar focus straight back to the input bar, a
        focusable strip is focused twice per click (on mouse down, then by ``Tabs``
        itself) only to be blurred again, restyling it and repainting the screen each time.
        """
        self._tab_strip_focusable = focusable
        if self.is_mounted:
            self.query_one(TabbedContent).query_one(Tabs).can_focus = focusable

    def on_unmount(self) -> None:
        if self._locale_controller is not None:
            self._locale_controller.unregister_surface(self)

    def refresh_localization(self) -> None:
        """Relabel tabs and delegate current-text refreshes to owned panels."""
        if not self.is_mounted:
            return
        tabs = self.query_one(TabbedContent).query_one(Tabs)
        for tab, definition in zip(tabs.query(Tab).results(Tab), _SIDEBAR_TAB_LABELS, strict=True):
            tab.label = self._localized_content(definition.bind())
        self.context_panel.refresh_localization()
        self.tasks_panel.refresh_localization()
        self.hooks_panel.refresh_localization()
        self.toc_panel.refresh_localization()
        self.query_one(DiffPreview).refresh_localization()
        self.buddy_panel.refresh_localization()
        self._fit_room()
        self.debug_panel.refresh_localization()
        # The relabelled tabs change width, and after a layout the new virtual
        # size reaches ``_sync_tab_overflow`` through its watcher too.
        self._sync_tab_overflow()

    def _localized_content(self, reference: MessageRef) -> Content:
        controller = self._locale_controller
        if controller is None:
            return Content.from_text(format_message(reference), markup=False)
        return render_content(controller.localizer, reference)

    # The tab strip is wider than a narrow sidebar: the wheel over it (up and
    # down too, as most mice have no sideways wheel) slides it left and right.
    # Its own scroll box hides overflow and ignores the wheel, so these bubble here.
    def on_mouse_scroll_down(self, event: MouseScrollDown) -> None:
        self._slide_tab_strip(event, _TAB_STRIP_SCROLL_CELLS)

    def on_mouse_scroll_up(self, event: MouseScrollUp) -> None:
        self._slide_tab_strip(event, -_TAB_STRIP_SCROLL_CELLS)

    def on_mouse_scroll_right(self, event: MouseScrollRight) -> None:
        self._slide_tab_strip(event, _TAB_STRIP_SCROLL_CELLS)

    def on_mouse_scroll_left(self, event: MouseScrollLeft) -> None:
        self._slide_tab_strip(event, -_TAB_STRIP_SCROLL_CELLS)

    def _slide_tab_strip(
        self, event: MouseScrollDown | MouseScrollUp | MouseScrollLeft | MouseScrollRight, cells: int
    ) -> None:
        tabs = self.query_one(TabbedContent).query_one(Tabs)
        # The whole tab row, overflow marks included. Where the mouse is, not
        # ``event.widget``: the terminal's wheel events name no widget.
        row = Region(self.content_region.x, tabs.region.y, self.content_region.width, tabs.region.height)
        if not row.contains_point(event.screen_offset):
            return
        event.stop()
        # Forced: the strip hides its overflow, so it takes no scroll of its own.
        tabs.query_one("#tabs-scroll").scroll_relative(x=cells, animate=False, force=True)

    def _tab_strip(self) -> Widget:
        tabs = self.query_one(TabbedContent).query_one(Tabs)
        return tabs.query_one("#tabs-scroll")

    def _overflow_mark(self, direction: Literal["left", "right"]) -> TabOverflowMark:
        return self.query_one(f"#sidebar-tabs-overflow-{direction}", TabOverflowMark)

    def _tab_spans(self) -> list[_TabSpan]:
        """Where each shown tab and its label lie along the strip, from the tabs' own widths."""
        spans: list[_TabSpan] = []
        start = 0
        for tab in self.query_one(TabbedContent).query_one(Tabs).query(Tab).results(Tab):
            if not tab.display:
                continue
            width = tab.outer_size.width
            padding = tab.styles.padding
            spans.append(
                _TabSpan(start, start + padding.left, start + width - padding.right, start + width, tab.label.plain)
            )
            start += width
        return spans

    def _sync_tab_overflow(self) -> None:
        """Reserve the mark cells only while a label does not fit; show each mark only while that side cuts a label.

        A tab cut only in its padding counts as shown, everywhere: here, and where
        a click on a mark slides the strip.
        """
        if not self.is_mounted:
            return
        tabs = self.query_one(TabbedContent).query_one(Tabs)
        strip = tabs.query_one("#tabs-scroll")
        if not strip.container_size.width:
            # Laid out for the first time, the strip has its content width but
            # not yet its visible one: there is no overflow to judge.
            return
        spans = self._tab_spans()
        # Judged against the whole row, which the reserved cells do not narrow,
        # and with the tabs' usual padding, which ``-tight`` drops: neither change
        # this decision makes can feed back into it.
        overflow = self._padded_label_end() > self.content_region.width
        tabs.set_class(overflow, "-overflow")
        position = round(strip.scroll_x)
        view = self._tab_view_width()
        # With the padding, two padded edges can meet between labels: the strip keeps
        # it only where any position still shows a whole character. Judged without
        # the tabs' current padding, so dropping it cannot undo this decision.
        tabs.set_class(overflow and view < _sure_character_view(self._widest_character()) + 2 * _TAB_PADDING, "-tight")
        self._overflow_mark("left").set_state(
            reserved=overflow, active=overflow and any(span.label_start < position for span in spans)
        )
        self._overflow_mark("right").set_state(
            reserved=overflow, active=overflow and any(span.label_end > position + view for span in spans)
        )

    def _padded_label_end(self) -> int:
        """Where the last shown label would end with every shown tab keeping its usual padding."""
        widths = [
            tab.label.cell_length + 2 * _TAB_PADDING
            for tab in self.query_one(TabbedContent).query_one(Tabs).query(Tab).results(Tab)
            if tab.display
        ]
        return sum(widths) - _TAB_PADDING if widths else 0

    def _tab_view_width(self) -> int:
        """The strip's visible width while the marks keep their cells, known before that layout lands."""
        return self.content_region.width - 2 * _MARK_CELLS

    def on_resize(self, _event: events.Resize) -> None:
        # The strip keeps its content width across a resize; its container does
        # not, so how much of the strip is hidden is only known after the layout.
        self.call_after_refresh(self._sync_tab_overflow)

    def on_tab_overflow_mark_pressed(self, event: TabOverflowMark.Pressed) -> None:
        event.stop()
        self._reveal_hidden_tab(event.direction)

    def _reveal_hidden_tab(self, direction: Literal["left", "right"]) -> None:
        """Slide the strip to show the next tab whose label is cut on that side.

        The whole tab when it fits. A label wider than the strip is shown from its
        start, then from its first character not yet shown whole on each click,
        never past its end.
        """
        strip = self._tab_strip()
        spans = self._tab_spans()
        position = round(strip.scroll_x)
        view = self._tab_view_width()
        if direction == "right":
            target = next((span for span in spans if span.label_end > position + view), None)
            if target is None:
                return
            destination = min(target.end - view, target.label_start)
            if destination <= position:
                cut = next(start for start, end in target.characters() if end > position + view)
                destination = min(target.label_end - view, cut)
        else:
            target = next((span for span in reversed(spans) if span.label_start < position), None)
            if target is None:
                return
            destination = max(target.start, target.label_end - view)
            if destination >= position:
                cut = next(end for start, end in reversed(target.characters()) if start < position)
                destination = max(target.label_start, cut - view)
        strip.scroll_to(x=destination, animate=False, force=True)

    def fit_beside(self, room: int) -> None:
        """Keep the sidebar within the *room* cells its row leaves beside the main column.

        On a terminal too narrow for the usual minimum, the sidebar takes exactly
        the room left instead of running past the screen's edge, where its right
        border and the right overflow mark would be cut off. Where that room no
        longer gives the tab strip a whole character at any scroll position, the
        sidebar hides until the terminal is wide enough again; where it does so
        only without the tabs' padding, the tabs drop it. Otherwise the
        stylesheet's bounds apply.
        """
        self._room = room
        self._fit_room()

    def _widest_character(self) -> int:
        """The cells of the widest character in any tab label."""
        return max(
            cell_len(char)
            for tab in self.query_one(TabbedContent).query_one(Tabs).query(Tab).results(Tab)
            for char in tab.label.plain
        )

    def _fit_room(self) -> None:
        """Size the sidebar to the room left, and hide it where its tab strip could show no whole character."""
        if self._room is None or not self.is_mounted:
            return
        room = self._room
        hidden = room < self.styles.gutter.width + 2 * _MARK_CELLS + _sure_character_view(self._widest_character())
        self.suppress_visibility("narrow-terminal", hidden)
        # Both bounds: below the usual minimum, the sidebar's 30% can still be wider than the room.
        # The stylesheet's minimum, not the inline one this sets.
        usual = self.styles.base.min_width
        usual_min_width = 0 if usual is None else int(usual.resolve(self.app.size, self.app.size))
        narrow = max(room, 0) if room < usual_min_width else None
        self.styles.min_width = narrow
        self.styles.max_width = narrow

    def toggle(self) -> None:
        if not self._visibility_suppressors:
            self.display = not self.display

    def set_workflow_mode(self, workflow: bool) -> None:
        """Keep chat-only panels out of workflow navigation, retaining the chat selection."""
        if workflow == self._workflow_mode:
            return
        self._workflow_mode = workflow
        tabs = self.query_one(TabbedContent)
        if workflow:
            self._chat_tab = tabs.active
            if tabs.active in _WORKFLOW_HIDDEN_TABS:
                tabs.active = "tab-debug"
            for pane in _WORKFLOW_HIDDEN_TABS:
                tabs.hide_tab(pane)
        else:
            for pane in _WORKFLOW_HIDDEN_TABS:
                tabs.show_tab(pane)
            tabs.active = self._chat_tab
        # Shown or hidden tabs change the strip's width; the mark update is
        # repeated by the virtual-size watcher once the new layout is in.
        self._sync_tab_overflow()

    def suppress_visibility(
        self, owner: Literal["theme-editor", "alternate-screen", "narrow-terminal"], active: bool
    ) -> None:
        """Restore the user's visibility only after every temporary hide ends."""
        if active:
            if owner in self._visibility_suppressors:
                return
            if not self._visibility_suppressors:
                self._display_before_suppression = self.display
            self._visibility_suppressors.add(owner)
            self.display = False
        elif owner in self._visibility_suppressors:
            self._visibility_suppressors.remove(owner)
            if not self._visibility_suppressors:
                self.display = self._display_before_suppression

    @property
    def is_visible(self) -> bool:
        return self.display

    @property
    def debug_panel(self) -> DebugPanel:
        return self.query_one(DebugPanel)

    @property
    def context_panel(self) -> ContextPanel:
        return self.query_one(ContextPanel)

    @property
    def tasks_panel(self) -> TasksPanel:
        return self.query_one(TasksPanel)

    @property
    def hooks_panel(self) -> HookRunList:
        return self.query_one(HookRunList)

    @property
    def toc_panel(self) -> ConversationToc:
        return self.query_one(ConversationToc)

    @property
    def buddy_panel(self) -> BuddyPanel:
        return self.query_one(BuddyPanel)

    def focus_tab(self, tab_id: str) -> None:
        if self._workflow_mode and tab_id in _WORKFLOW_HIDDEN_TABS:
            return
        tc = self.query_one(TabbedContent)
        tc.active = tab_id
