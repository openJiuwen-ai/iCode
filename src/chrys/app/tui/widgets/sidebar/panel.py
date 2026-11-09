# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""SidebarPanel — toggleable tabbed sidebar."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from textual.content import Content
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Tab, TabbedContent, TabPane, Tabs

from chrys.app.tui.i18n import render_content
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

if TYPE_CHECKING:
    from textual.app import ComposeResult
    from textual.events import MouseScrollDown, MouseScrollLeft, MouseScrollRight, MouseScrollUp

    from chrys.app.tui.i18n import LocaleController
    from chrys.foundation.models.background_tasks import BackgroundTaskView


class SidebarPanel(Widget, can_focus=False):
    """Toggleable sidebar with tabbed panels."""

    context_usage_state: reactive[ContextUsageState | None] = reactive(None, always_update=True)
    todo_state: reactive[TodoListState | None] = reactive(None, always_update=True)
    background_tasks: reactive[tuple[BackgroundTaskView, ...]] = reactive(())

    DEFAULT_CSS = """
    SidebarPanel {
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
        height: 100%;
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

    def on_mount(self) -> None:
        if self._locale_controller is not None:
            self._locale_controller.register_surface(self)
        self.query_one(TabbedContent).query_one(Tabs).can_focus = self._tab_strip_focusable
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
        self.debug_panel.refresh_localization()

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
        # Where the mouse is, not ``event.widget``: the terminal's wheel events name no widget.
        if not tabs.region.contains_point(event.screen_offset):
            return
        event.stop()
        # Forced: the strip hides its overflow, so it takes no scroll of its own.
        tabs.query_one("#tabs-scroll").scroll_relative(x=cells, animate=False, force=True)

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

    def suppress_visibility(self, owner: Literal["theme-editor", "alternate-screen"], active: bool) -> None:
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
