# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""ChatPanel — scrollable chat area that dynamically mounts message widgets.

Replaces the old WorkPanel (plain Log) with rich, interactive widgets for
each message type: user, agent, tool calls, context folds, errors, etc.
"""

from __future__ import annotations

import itertools
import logging
from collections.abc import Awaitable, Callable, Sequence
from fractions import Fraction
from functools import partial
from typing import TYPE_CHECKING, Any
from weakref import WeakKeyDictionary, WeakSet

from rich.text import Text
from textual import events as textual_events
from textual._arrange import DockArrangeResult
from textual.containers import VerticalScroll
from textual.dom import NoScreen
from textual.geometry import Offset, Region, Size
from textual.message import Message
from textual.reactive import reactive
from textual.scrollbar import ScrollBar, ScrollDown, ScrollTo, ScrollUp
from textual.timer import Timer
from textual.widget import Widget

from chrys.app.tui.behaviors.right_click_copy import cancel_textual_mouse_chain
from chrys.app.tui.support.gc_freeze import GcFreezeBlockReason, raise_gc_freeze_hook_errors
from chrys.app.tui.util.placement import pin_fixed_placement
from chrys.app.tui.util.removal import finish_shielded, remove_shielded
from chrys.app.tui.widgets import ASK_USER_INTERACTIVE_CLASS
from chrys.foundation.patches.textual_precompose import precompose_tree

if TYPE_CHECKING:
    from textual.events import Click
    from textual.layout import Layout

    from chrys.app.tui.i18n import LocaleController
    from chrys.app.tui.widgets import PromptDraft
    from chrys.app.update_check import UpdateNotice
    from chrys.foundation.models.ask_user import AskUserQuestion
    from chrys.foundation.models.invocations import ContentChannel
    from chrys.service.context.providers.history import CompressedBlock
    from chrys.service.session.sub_agent_transcript import PersistedSubAgentTranscript

from chrys.app.tui.widgets.chat.chrome import (
    ChatBottomSpacer,
    ChatPanelChrome,
    ScrollToBottomButton,
    ScrollToBottomRequested,
)
from chrys.app.tui.widgets.chat.file_snapshot import FileSnapshotPayload
from chrys.app.tui.widgets.chat.fold_gap import show_fold_gaps
from chrys.app.tui.widgets.chat.live_renderer import LiveTranscriptRenderer
from chrys.app.tui.widgets.chat.messages import (
    AgentMessage,
    ErrorMessage,
    InterruptedMessage,
    RetryMessage,
    StreamedMarkdownMessage,
    SystemMessage,
    UserMessage,
    remove_withdrawn_answer,
)
from chrys.app.tui.widgets.chat.ports import TranscriptLocalizationPort
from chrys.app.tui.widgets.chat.process_block import ProcessBlock
from chrys.app.tui.widgets.chat.process_controller import ProcessController
from chrys.app.tui.widgets.chat.renderers.ask_user import AskUserInlineResized, AskUserToolCall
from chrys.app.tui.widgets.chat.renderers.execute import ExecuteToolCall
from chrys.app.tui.widgets.chat.renderers.sub_agent import SubAgentToolCall
from chrys.app.tui.widgets.chat.replay import HistoryReplayRenderer
from chrys.app.tui.widgets.chat.replay_mount import ReplayMountController
from chrys.app.tui.widgets.chat.scroll_controller import ChatScrollController
from chrys.app.tui.widgets.chat.toc_model import TurnTocModel
from chrys.app.tui.widgets.chat.tool_call import ToolGroup
from chrys.app.tui.widgets.chat.tool_registry import ToolGroupRegistry
from chrys.app.tui.widgets.chat.transcript_panel import ChatTranscriptPanelMarker
from chrys.app.tui.widgets.chat.transcript_reader import TranscriptReader
from chrys.app.tui.widgets.chat.waiting_row import WaitingRow, waits_after
from chrys.app.tui.widgets.sidebar.toc_types import TocItem
from chrys.app.tui.widgets.welcome import WelcomeWidget

logger = logging.getLogger(__name__)

_ChatBottomSpacer = ChatBottomSpacer
_ScrollToBottomButton = ScrollToBottomButton

# Document-order stamps for top-level transcript widgets. The chat selection
# model orders leaves by (stamp, sibling path) so drag selection never needs
# geometry for widgets outside the viewport. A process-wide counter keeps
# stamps monotonic across session resets and multiple panels.
_transcript_seq_counter = itertools.count(1)
_REPLAY_PLACEHOLDER_CLASS = "-replay-placeholder"
_WAITING_ROW_DELAY_SECONDS = 0.3
"""How long a running turn waits with nothing moving before the waiting row shows."""


class ChatPanel(VerticalScroll, ChatTranscriptPanelMarker, can_focus=True):
    """Main scrollable chat area that dynamically adds message widgets.

    API mirrors the old WorkPanel but mounts rich widgets instead of writing
    plain text lines.
    """

    DEFAULT_CSS = """
    ChatPanel {
        min-width: 35;
        min-height: 3;
        height: 1fr;
        border: round $tui-border-primary $border-opacity;
        scrollbar-size: 1 1;
        padding: 0 0 0 1;
        border-title-align: left;
        border-title-color: $primary;
        border-subtitle-align: right;
        border-subtitle-color: $primary;
    }
    ChatPanel > .-replay-placeholder {
        hatch: left $hatch-color;
    }
    """

    agent_running: reactive[bool] = reactive(False)
    profile_name: reactive[str] = reactive("")
    session_id_value: reactive[str] = reactive("")
    session_title_value: reactive[str] = reactive("")
    workspace_cwd: reactive[str] = reactive("")
    workspace_branch: reactive[str] = reactive("")

    def __init__(
        self,
        cwd: str = "",
        *,
        localization: TranscriptLocalizationPort | None = None,
        locale_controller: LocaleController | None = None,
        tool_groups_expanded: Callable[[], bool] | None = None,
        thinking_expanded: Callable[[], bool] | None = None,
        fold_finished_turns: Callable[[], bool] | None = None,
        sub_agent_transcript_loader: Callable[[str], Awaitable[PersistedSubAgentTranscript | None]] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        # Document-order stamps for top-level transcript children; weak keys
        # so removed widgets drop their entries with the widget itself.
        self._transcript_seqs: WeakKeyDictionary[Widget, Fraction] = WeakKeyDictionary()
        self._fold_finished_turns = fold_finished_turns or (lambda: False)
        self._process_controller = ProcessController(self)
        self._replay_process_protect_last = False
        self._ask_user_tool_widgets: WeakSet[AskUserToolCall] = WeakSet()
        self._replay_progress_callback: Callable[[int, int], None] | None = None
        self._waiting_row_timer: Timer | None = None
        """Shows the waiting row once the turn has waited a moment; None while none is pending."""
        self._waiting_row_moment = 0
        """Counts the moments the turn waited; only the newest one's timer shows the row."""
        self._locale_controller = locale_controller
        self._sub_agent_transcript_loader = sub_agent_transcript_loader
        self._chrome = ChatPanelChrome(self, locale_controller=locale_controller)
        self._scroll_controller = ChatScrollController(self)
        self._toc = TurnTocModel()
        self._tool_registry = ToolGroupRegistry(
            self,
            self,
            self,
            finalize_agent_message=self._settle_answer_before_tools,
            tool_groups_expanded=tool_groups_expanded,
            tool_widget_configurator=self._configure_tool_widget,
            workspace_cwd=self._current_workspace_cwd,
        )
        self._live_renderer = LiveTranscriptRenderer(
            self,
            self,
            self,
            self,
            self._toc,
            self._tool_registry,
            localization=localization,
            thinking_expanded=thinking_expanded,
            fold_passed_text=True,
        )
        self._replay_renderer = HistoryReplayRenderer(
            self,
            self,
            self._toc,
            self._tool_registry.resolve_tool_kind,
            default_profile=lambda: self._profile_name,
            localization=localization,
            tool_widget_configurator=self._configure_tool_widget,
            workspace_cwd=self._current_workspace_cwd,
            show_reasoning=thinking_expanded is not None,
            fold_passed_text=True,
        )
        self._transcript_reader = TranscriptReader(self)
        self._replay_mount = ReplayMountController(self)
        if cwd:
            self.workspace_cwd = cwd

    def _configure_tool_widget(self, widget: Widget) -> None:
        if isinstance(widget, AskUserToolCall):
            self._ask_user_tool_widgets.add(widget)
            widget.handle_chat_viewport_resize(self.size.height)
        if isinstance(widget, SubAgentToolCall) and self._sub_agent_transcript_loader is not None:
            widget.configure_transcript_loader(self._sub_agent_transcript_loader)
        if isinstance(widget, ExecuteToolCall):
            widget.offers_background = True

    def gc_freeze_block_reason(self) -> GcFreezeBlockReason | None:
        """The MainScreen owns ChatPanel's activity and scroll hard gates."""
        return None

    def focus_on_click(self) -> bool:
        """Keep passive transcript clicks from stealing composer focus."""
        return False

    def prepare_for_gc_freeze(self) -> None:
        """Release cyclic transcript render caches before a GC freeze action."""
        for surface in self._gc_freeze_render_cache_surfaces():
            surface.prepare_for_gc_freeze()

    def after_gc_freeze(self) -> None:
        """Renew all transcript render caches, then report every failure."""
        errors: list[Exception] = []
        for surface in self._gc_freeze_render_cache_surfaces():
            try:
                surface.after_gc_freeze()
            except Exception as error:
                errors.append(error)
        raise_gc_freeze_hook_errors("Transcript cache renewal failed", errors)

    def abort_gc_freeze(self) -> None:
        """Best-effort restore every detached transcript render cache."""
        for surface in self._gc_freeze_render_cache_surfaces():
            try:
                surface.abort_gc_freeze()
            except Exception:
                logger.exception("Failed to restore detached transcript cache on %s", type(surface).__name__)

    def _gc_freeze_render_cache_surfaces(self) -> list[Any]:
        """Return mounted transcript descendants with renewable cyclic LRUs."""
        from chrys.app.tui.widgets.diff_view.code import CodeColumn
        from chrys.app.tui.widgets.diff_view.gutter import GutterColumn
        from chrys.app.tui.widgets.diff_view.unified import UnifiedDiffLines
        from chrys.app.tui.widgets.markdown.widget import VirtualizedMarkdown

        surface_types = (VirtualizedMarkdown, UnifiedDiffLines, CodeColumn, GutterColumn)
        return [widget for widget in self.walk_children() if isinstance(widget, surface_types)]

    def watch_agent_running(self, old_running: bool, running: bool) -> None:
        self._scroll_controller.set_agent_running(running)
        self._sync_waiting_row()

    def watch_profile_name(self, profile_name: str) -> None:
        self._live_renderer.set_profile(profile_name)

    def watch_session_id_value(self, session_id: str) -> None:
        self._chrome.set_session_id(session_id)

    def watch_session_title_value(self, title: str) -> None:
        self._chrome.set_session_title(title)

    def watch_workspace_cwd(self, cwd: str) -> None:
        self._chrome.set_workspace_cwd(cwd)
        # A group's one call names its path relative to the folder: once the folder changes, it names it anew.
        for group in self.query(ToolGroup):
            group.refresh_title()

    def _current_workspace_cwd(self) -> str:
        return self.workspace_cwd

    def watch_workspace_branch(self, branch: str) -> None:
        self._chrome.set_workspace_branch(branch)

    def scroll_to_region(self, region: Region, **kwargs: Any) -> Offset:
        """Ignore focus-restoration center scrolls inside the transcript.

        Chat has explicit navigation paths (``top=True`` and bottom-anchor
        sync); modal dismissal focus recovery must not yank it to old content.
        """
        return self._scroll_controller.scroll_to_region(region, **kwargs)

    def _check_anchor(self) -> None:
        """Re-engage bottom-follow only at exact bottom."""
        self._scroll_controller.check_anchor()

    def entry_folded(self) -> None:
        self._scroll_controller.entry_folded()

    def set_tool_kinds(self, tool_kinds: dict[str, str]) -> None:
        """Set tool name → kind map (used during session replay)."""
        self._tool_registry.set_tool_kinds(tool_kinds)

    def set_profile(self, name: str) -> None:
        """Set the current agent profile name (shown in agent message headers)."""
        self.profile_name = name

    @property
    def _current_agent_msg(self) -> AgentMessage | None:
        return self._live_renderer.current_agent_msg

    @_current_agent_msg.setter
    def _current_agent_msg(self, message: AgentMessage | None) -> None:
        self._live_renderer.current_agent_msg = message

    def _settle_answer_before_tools(self) -> None:
        """Show the streamed answer text a tool batch follows as intermediate text, folded with the thinking before it.

        The tool group about to start hides the waiting row now.
        """
        self._live_renderer.fold_before_tools()
        self._sync_waiting_row()

    @property
    def _profile_name(self) -> str:
        return self._live_renderer.profile_name

    @_profile_name.setter
    def _profile_name(self, name: str) -> None:
        self._live_renderer.profile_name = name

    @property
    def _agent_running(self) -> bool:
        return self._scroll_controller.agent_running

    @_agent_running.setter
    def _agent_running(self, running: bool) -> None:
        self._scroll_controller.agent_running = running

    @property
    def _current_tool_group(self) -> ToolGroup | None:
        return self._tool_registry.current_tool_group

    @_current_tool_group.setter
    def _current_tool_group(self, group: ToolGroup | None) -> None:
        self._tool_registry.current_tool_group = group

    @property
    def _sub_agent_widgets(self) -> dict[str, SubAgentToolCall]:
        return self._tool_registry.sub_agent_widgets

    @property
    def _unlinked_sub_agents(self) -> dict[str, list[SubAgentToolCall]]:
        return self._tool_registry.unlinked_sub_agents

    @property
    def _tool_groups_by_call_id(self) -> dict[str, ToolGroup]:
        return self._tool_registry.tool_groups_by_call_id

    @property
    def _tool_kinds(self) -> dict[str, str]:
        return self._tool_registry.tool_kinds

    @_tool_kinds.setter
    def _tool_kinds(self, tool_kinds: dict[str, str]) -> None:
        self._tool_registry.tool_kinds = tool_kinds

    @property
    def _welcome(self) -> WelcomeWidget | None:
        return self._chrome.welcome

    @_welcome.setter
    def _welcome(self, widget: WelcomeWidget | None) -> None:
        self._chrome.welcome = widget

    @property
    def _bottom_spacer(self) -> ChatBottomSpacer | None:
        return self._chrome.bottom_spacer

    @_bottom_spacer.setter
    def _bottom_spacer(self, widget: ChatBottomSpacer | None) -> None:
        self._chrome.bottom_spacer = widget

    @property
    def _scroll_to_bottom_button(self) -> ScrollToBottomButton | None:
        return self._chrome.scroll_to_bottom_button

    @_scroll_to_bottom_button.setter
    def _scroll_to_bottom_button(self, widget: ScrollToBottomButton | None) -> None:
        self._chrome.scroll_to_bottom_button = widget

    @property
    def _session_id(self) -> str:
        return self._chrome.session_id

    @_session_id.setter
    def _session_id(self, session_id: str) -> None:
        self._chrome.set_session_id(session_id)

    @property
    def _held_shrink_height(self) -> int:
        return self._scroll_controller.held_shrink_height

    @_held_shrink_height.setter
    def _held_shrink_height(self, value: int) -> None:
        self._scroll_controller.held_shrink_height = value

    @property
    def _manual_scroll_gc_timer(self) -> Timer | None:
        return self._scroll_controller.manual_scroll_gc_timer

    @_manual_scroll_gc_timer.setter
    def _manual_scroll_gc_timer(self, timer: Timer | None) -> None:
        self._scroll_controller.manual_scroll_gc_timer = timer

    @property
    def _manual_scroll_gc_paused(self) -> bool:
        return self._scroll_controller.manual_scroll_gc_paused

    @_manual_scroll_gc_paused.setter
    def _manual_scroll_gc_paused(self, value: bool) -> None:
        self._scroll_controller.manual_scroll_gc_paused = value

    @property
    def _auto_scroll_paused_by_user(self) -> bool:
        return self._scroll_controller.auto_scroll_paused_by_user

    @_auto_scroll_paused_by_user.setter
    def _auto_scroll_paused_by_user(self, value: bool) -> None:
        self._scroll_controller.auto_scroll_paused_by_user = value

    @property
    def _final_response_started(self) -> bool:
        return self._scroll_controller.final_response_started

    @_final_response_started.setter
    def _final_response_started(self, value: bool) -> None:
        self._scroll_controller.final_response_started = value

    @property
    def _programmatic_scroll(self) -> bool:
        return self._scroll_controller.programmatic_scroll

    @_programmatic_scroll.setter
    def _programmatic_scroll(self, value: bool) -> None:
        self._scroll_controller.programmatic_scroll = value

    @property
    def _anchor_sync_scheduled(self) -> bool:
        return self._scroll_controller.anchor_sync_scheduled

    @_anchor_sync_scheduled.setter
    def _anchor_sync_scheduled(self, value: bool) -> None:
        self._scroll_controller.anchor_sync_scheduled = value

    def compose(self):
        yield from self._chrome.initial_widgets()

    def mount(self, *widgets, **kwargs):  # type: ignore[override]
        """Mount widgets before the waiting row (it and the spacer stay last).

        Gates ``before=`` injection on the row still being an attached
        child: :meth:`clear` nulls the reference before ``remove_children``
        awaits, and a mid-reset mount could otherwise hit ``MountError:
        Unable to find relative location`` (observed on Windows during
        session restore).  Stale reference is then a safe no-op.
        """
        waiting_row = self._chrome.waiting_row
        transcript = not any(self._chrome.is_infrastructure(widget) for widget in widgets)
        if (
            waiting_row is not None
            and waiting_row.parent is self
            and "before" not in kwargs
            and "after" not in kwargs
            and transcript
        ):
            kwargs["before"] = waiting_row
        for widget in widgets:
            if isinstance(widget, Widget) and widget not in self._transcript_seqs:
                self._transcript_seqs[widget] = Fraction(next(_transcript_seq_counter))
        mounted = super().mount(*widgets, **kwargs)
        if transcript and any(widget.display for widget in widgets):
            # Registered as children already: new output hides the row before it paints. A tool group mounts hidden
            # until its first call, and hides the row as it starts (_settle_answer_before_tools).
            self._sync_waiting_row()
        return mounted

    def _sync_waiting_row(self) -> None:
        """Show the waiting row once a running turn has waited a moment with nothing moving; hide it once it moves.

        The moment keeps it from flashing up between one row ending and the next starting.
        """
        row = self._chrome.waiting_row
        if row is None or row.parent is not self:
            return
        if self._turn_waits():
            if not row.display and self._waiting_row_timer is None:
                self._waiting_row_moment += 1
                self._waiting_row_timer = self.set_timer(
                    _WAITING_ROW_DELAY_SECONDS, partial(self._show_waiting_row, self._waiting_row_moment)
                )
            return
        if self._waiting_row_timer is not None:
            self._waiting_row_timer.stop()
            self._waiting_row_timer = None
        if row.display:
            # The rows it gives up go in the layout that hides it, as a fold's do.
            self.entry_folded()
            row.display = False

    def _show_waiting_row(self, moment: int) -> None:
        # A timer hands this to the panel as its moment passes, and stopping the timer after that does not take it
        # back: the turn may have moved since, or be waiting a newer moment, which its own timer ends.
        if moment != self._waiting_row_moment or self._waiting_row_timer is None:
            return
        self._waiting_row_timer = None
        row = self._chrome.waiting_row
        if row is not None and row.parent is self and self._turn_waits():
            row.display = True

    def _turn_waits(self) -> bool:
        """Whether a running turn waits with nothing in the transcript moving."""
        if not self.agent_running or self.replay_in_progress:
            return False
        last = next((child for child in reversed(self.children) if not self._chrome.is_infrastructure(child)), None)
        return waits_after(last)

    def transcript_seq(self, widget: Widget) -> Fraction | None:
        """Document-order stamp for a top-level child; ``None`` if never
        mounted through this panel."""
        return self._transcript_seqs.get(widget)

    def stamp_transcript_order(self, widgets: list[Widget]) -> None:
        """Stamp document order up front for entries mounted newest first."""
        for widget in widgets:
            if widget not in self._transcript_seqs:
                self._transcript_seqs[widget] = Fraction(next(_transcript_seq_counter))

    async def mount_transcript_after(self, anchor: Widget, widget: Widget) -> bool:
        """Insert between neighboring exact order keys, without changing existing selection anchors."""
        if anchor.parent is not self or not self.can_mount_replay():
            return False
        previous = self._transcript_seqs[anchor]
        following = min(
            (
                seq
                for node, seq in self._transcript_seqs.items()
                if seq > previous and not self._chrome.is_infrastructure(node)
            ),
            default=previous + 1,
        )
        self._transcript_seqs[widget] = (previous + following) / 2
        await self.mount(widget, after=anchor)
        return widget.parent is self and not widget._pruning

    def on_tool_group_process_state_changed(self, event: ToolGroup.ProcessStateChanged) -> None:
        event.stop()
        block = self._process_controller.block_for_group(event.group)
        if block is None:
            return
        with self.app.batch_update():
            self._process_controller.member_changed(block)

    def on_process_block_toggle_requested(self, event: ProcessBlock.ToggleRequested) -> None:
        event.stop()
        if event.block.parent is not self:
            return
        self.note_user_scroll()
        self.set_anchor_released(True)
        self._auto_scroll_paused_by_user = True
        with self.app.batch_update():
            event.block.requested_collapsed = not event.block.requested_collapsed
            event.block.try_finalize_fold()

    def on_mount(self) -> None:
        if self._locale_controller is not None:
            self._locale_controller.register_surface(self)
        # Textual's native anchor: pin to bottom as content grows, auto-release
        # when user scrolls up, auto-re-engage when they return to the bottom.
        self.anchor()
        # Scrollbar thumb drags must hold the scroll-GC pause for the whole
        # gesture: a slow drag's gaps between mouse moves can exceed any
        # cadence debounce, and a mid-gesture GC resume triggers a backlog
        # collection spike on the very next drag movement.
        self.watch(self.vertical_scrollbar, "grabbed", self._on_scrollbar_grabbed_changed, init=False)

    def _on_scrollbar_grabbed_changed(self, grabbed: object) -> None:
        """Map scrollbar grab state onto the gesture-scoped GC pause."""
        if grabbed is not None:
            self.note_user_scroll()
            self._scroll_controller.on_scrollbar_grab()
        else:
            self._scroll_controller.on_scrollbar_release()

    def on_unmount(self) -> None:
        if self._locale_controller is not None:
            self._locale_controller.unregister_surface(self)
        self._replay_mount.abandon()
        self._scroll_controller.stop()

    def refresh_localization(self) -> None:
        """Retranslate bounded chat chrome without walking transcript children."""
        self._chrome.refresh_localization()

    def pre_layout(self, layout: Layout) -> None:
        entries = self.displayed_children
        show_fold_gaps(entries, end=self._transcript_end(entries))

    def _transcript_end(self, entries: Sequence[Widget]) -> Widget | None:
        """The last of the *entries* shown, or the waiting row below them: its last line ends the transcript."""
        for entry in reversed(entries):
            if isinstance(entry, WaitingRow) or not self.is_infrastructure(entry):
                return entry
        return None

    def arrange(self, size: Size, optimal: bool = False) -> DockArrangeResult:
        """Defuse Textual anchor pins while chat layout is settling.

        The compositor writes ``scroll_y`` directly from arranged height.
        Keep that math current, clamp stale negative pins, leave out the
        spacer row past content that fills the viewport, and skip one
        anchored shrink frame; ``_size_updated`` handles the validator side.
        """
        return self._scroll_controller.arrange(size, optimal=optimal)

    def _place_scroll_to_bottom_button(self, result: DockArrangeResult, size: Size) -> None:
        """Keep the scroll-bottom affordance fixed to the viewport bottom, centred.

        It stays a ChatPanel child for click routing and lifecycle. It sits at
        the tail of the transcript children, where the pin's search starts:
        rebuilding the spatial map per scroll frame dominated scrolling on
        large transcripts.
        """
        button = self._scroll_to_bottom_button
        if button is None:
            return

        def bottom_centre(arranged: Region) -> Region:
            width = min(max(1, arranged.width), max(1, size.width))
            height = min(max(1, arranged.height), max(1, size.height))
            return Region(max(0, (size.width - width) // 2), max(0, size.height - height), width, height)

        pin_fixed_placement(result, button, bottom_centre)

    def _reanchor_after_settle(self) -> None:
        """Re-engage the anchor and pin to settled ``max_scroll_y``."""
        self._scroll_controller.reanchor_after_settle()

    def _set_scroll_y_programmatically(self, y: float) -> None:
        """Set vertical scroll without treating the movement as a user pause."""
        self._scroll_controller.set_scroll_y_programmatically(y)

    def jump_to_bottom(self) -> None:
        """Scroll to the current bottom and resume bottom-follow."""
        self.note_user_scroll()
        self._scroll_controller.jump_to_bottom()

    def note_user_scroll(self) -> None:
        """A scroll the user asked for supersedes a TOC jump still waiting for its turn to mount.

        Hooked on user intents (wheel, scrollbar, keys), never on ``scroll_y``:
        the prepend itself moves ``scroll_y`` to keep the view steady.
        """
        self._replay_mount.forget_jump()

    def on_resize(self, _event: object | None = None) -> None:
        """Refresh the bottom-jump affordance when viewport height changes."""
        self._scroll_controller.on_resize()
        viewport_height = self.size.height
        for widget in tuple(self._ask_user_tool_widgets):
            if widget.is_attached and not widget._closing and not widget._pruning:
                widget.handle_chat_viewport_resize(viewport_height)

    def _size_updated(
        self,
        size: Size,
        virtual_size: Size,
        container_size: Size,
        layout: bool = True,
    ) -> bool:
        """Hold one anchored shrink frame before validator clamping.

        A transient ``virtual_size`` drop can clamp ``scroll_y`` to a false
        smaller bottom.  Hold each shrink height once, refresh layout, then
        accept it if it persists.
        """
        return self._scroll_controller.size_updated(size, virtual_size, container_size, layout)

    def _release_size_hold(self) -> None:
        """Force a layout pass that either recovers or accepts the held shrink."""
        self._scroll_controller.release_size_hold()

    def watch_scroll_y(self, old_value: float, new_value: float) -> None:
        """Track manual scrolling; no cleanup repaint is scheduled.

        Textual's scroll fast-path marks every moved widget's old and new
        region dirty, so newly exposed rows repaint correctly on their own —
        pinned cell-exactly by ``test_chat_scroll_fastpath.py``.  Manual
        ticks only feed the controller's GC pause and follow-state
        bookkeeping, plus the selection reprojection below.
        """
        self._scroll_controller.watch_scroll_y(old_value, new_value)
        self._notify_screen_selection_scrolled()

    def _notify_screen_selection_scrolled(self) -> None:
        """Let the owning screen re-align chat selection highlights.

        The chat drag-selection model keeps ``screen.selections`` bounded to
        the visible transcript slice, so scrolling must re-project highlights
        onto newly revealed widgets. No-op (a getattr and an ``active`` check)
        unless a chat selection exists.
        """
        try:
            screen = self.screen
        except NoScreen:
            return
        schedule = getattr(screen, "schedule_chat_selection_reprojection", None)
        if schedule is not None:
            schedule(self)

    def _on_mouse_scroll_up(self, event: textual_events.MouseScrollUp) -> None:
        """Pause cyclic GC before Textual mutates scroll state for wheel input."""
        # Do not call super(): Textual will dispatch Widget._on_mouse_scroll_up
        # later in the same MRO handler walk, and a manual super() scrolls twice.
        self.note_user_scroll()
        self._scroll_controller.pause_gc_for_manual_scroll()

    def _on_mouse_scroll_down(self, event: textual_events.MouseScrollDown) -> None:
        """Pause cyclic GC before Textual mutates scroll state for wheel input."""
        # Do not call super(): Textual will dispatch Widget._on_mouse_scroll_down
        # later in the same MRO handler walk, and a manual super() scrolls twice.
        self.note_user_scroll()
        self._scroll_controller.pause_gc_for_manual_scroll()

    def _on_scroll_to(self, message: ScrollTo) -> None:
        """Pause cyclic GC before scrollbar thumb drags update scroll state."""
        # Do not call super(): Textual will dispatch Widget._on_scroll_to
        # later in the same MRO handler walk, restarting the drag scroll work.
        self.note_user_scroll()
        self._scroll_controller.pause_gc_for_manual_scroll()

    def _on_scroll_up(self, event: ScrollUp) -> None:
        # Scrollbar track click; Widget._on_scroll_up pages later in the same MRO walk.
        self.note_user_scroll()

    def _on_scroll_down(self, event: ScrollDown) -> None:
        # Scrollbar track click; Widget._on_scroll_down pages later in the same MRO walk.
        self.note_user_scroll()

    def action_scroll_up(self) -> None:
        self.note_user_scroll()
        super().action_scroll_up()

    def action_scroll_down(self) -> None:
        self.note_user_scroll()
        super().action_scroll_down()

    def action_page_up(self) -> None:
        self.note_user_scroll()
        super().action_page_up()

    def action_page_down(self) -> None:
        self.note_user_scroll()
        super().action_page_down()

    def action_scroll_home(self) -> None:
        self.note_user_scroll()
        super().action_scroll_home()

    def action_scroll_end(self) -> None:
        self.note_user_scroll()
        super().action_scroll_end()

    def _sync_scroll_to_bottom_button(self) -> None:
        """Show the bottom-jump affordance only when there is unseen content below."""
        self._scroll_controller.sync_scroll_to_bottom_button()

    def _pause_gc_for_manual_scroll(self) -> None:
        """Temporarily disable cyclic GC while user scrolling is active."""
        self._scroll_controller.pause_gc_for_manual_scroll()

    def _resume_gc_after_manual_scroll(self) -> None:
        """Restore cyclic GC after manual scrolling has settled."""
        self._scroll_controller.resume_gc_after_manual_scroll()

    def _start_final_response_autoscroll(self) -> None:
        """Resume bottom-follow once when the agent starts its final answer."""
        self._scroll_controller.on_final_response_started()

    def watch_virtual_size(self, old: Size, new: Size) -> None:
        """Post-layout hook: re-engage anchor, sync scrollbar, toggle spacer.

        1. Anchor re-engage on growth: ``add_user_message`` releases the
           anchor when the content lets the user's message reach the viewport
           top (``scroll_user_message_to_top``), so it shows there while the
           response streams in below. Once the response overflows, resume
           following the bottom; rows a replay lands above the view are no
           response.
        2. Drive ``scroll_y`` through its own setter (not the compositor's
           ``set_reactive`` pin) so ``watch_scroll_y`` → ``ScrollBar.position``
           marks the scrollbar dirty on the same frame the content scrolls.
           Otherwise the thumb freezes for one frame because Textual's pin
           writes ``vertical_scrollbar._reactive_position`` directly.
        3. Bottom spacer (``1fr``) — hide when content fills the viewport
           (:meth:`arrange` already left out the one row Textual still gives
           it then), re-show when it shrinks back below (e.g. ``ToolGroup``
           auto-collapse) so the next growth phase has a flexible tail again.

        The negative-pin defense lives in :meth:`arrange` — it must run
        before the compositor reads the anchor flag.
        """
        self._scroll_controller.watch_virtual_size(old, new)

    async def _dismiss_welcome(self) -> None:
        """Remove the welcome widget on first interaction."""
        await self._chrome.dismiss_welcome()

    @property
    def session_id(self) -> str:
        """Current session id, or empty string before ``set_session_id`` is called."""
        return self._session_id

    def set_session_id(self, session_id: str) -> None:
        """Display the current session ID in the panel border title."""
        self.session_id_value = session_id

    def set_session_title(self, title: str) -> None:
        """Display the session title next to the session ID in the border title."""
        self.session_title_value = title

    def set_workspace_cwd(self, cwd: str) -> None:
        """Display the current workspace cwd in the panel border subtitle."""
        self.workspace_cwd = cwd

    def set_workspace_branch(self, branch: str) -> None:
        """Display the current workspace git branch in the panel border subtitle."""
        self.workspace_branch = branch

    class TitleClicked(Message):
        """Posted when the user clicks the border title (session id / title)."""

    class WorkingDirClicked(Message):
        """Posted when the user clicks the border subtitle (working directory)."""

    def on_click(self, event: Click) -> None:
        """Click on border title opens the title editor; subtitle opens dir picker."""
        self._chrome.handle_click(event)

    def on_scroll_to_bottom_requested(self, event: ScrollToBottomRequested) -> None:
        """Handle the chrome bottom-jump affordance."""
        event.stop()
        self.jump_to_bottom()

    async def mount_transcript_widget(self, widget: Widget) -> bool:
        """Mount a transcript widget through the migration mount port."""
        await self.mount(widget)
        return (
            widget.is_attached
            and not widget._closing
            and not widget._pruning
            and not self._closing
            and not self._pruning
        )

    async def mount_transcript_widgets(
        self, widgets: list[Widget], *, process_failures_after: tuple[Widget, ...] = ()
    ) -> None:
        """Mount replayed entries, newest first, and return once the view is filled.

        Entries beyond the first few viewports are prepended above by a
        panel-owned task after this returns (see ``replay_mount``);
        :meth:`wait_replay_complete` waits for them.
        """
        self._process_controller.remember_replay_failures(process_failures_after)
        if self._fold_finished_turns():
            widgets = self._process_controller.prepare_replay(
                widgets, protect_last_turn=self._replay_process_protect_last
            )
        await self._replay_mount.mount(widgets)

    @property
    def replay_in_progress(self) -> bool:
        """Whether older replayed entries are still being prepended."""
        return self._replay_mount.in_progress

    async def wait_replay_complete(self) -> None:
        """Wait until every replayed entry is mounted (or the prepend stopped)."""
        await self._replay_mount.wait_complete()

    async def mount_replay_batch(self, batch: list[Widget], *, before: Widget | None) -> None:
        """Mount one bounded replay batch, hatched until its descendants are ready."""
        for widget in batch:
            widget.add_class(_REPLAY_PLACEHOLDER_CLASS, update=False)
        precompose_tree(batch)
        try:
            # An explicit ``before=None`` would skip ``mount``'s default place above the waiting row.
            if before is None:
                await self.mount(*batch)
            else:
                await self.mount(*batch, before=before)
            # AwaitMount only covers the roots passed to ``mount``. The
            # precomposed descendants run their own async Mount handlers;
            # in particular, VirtualizedMarkdown parses and lays out its
            # document there. Keep the hatch visible and progress at the
            # prior batch boundary until every descendant is actually
            # ready to paint.
            descendants = [descendant for widget in batch for descendant in widget.walk_children()]
            for descendant in descendants:
                await descendant._mounted_event.wait()
        finally:
            attached: list[Widget] = []
            for widget in batch:
                widget.remove_class(_REPLAY_PLACEHOLDER_CLASS, update=False)
                if widget.is_attached:
                    attached.append(widget)
            if attached:
                # Re-evaluate only these batch roots. Hatch is repaint-only,
                # so this never invalidates MainScreen's transcript layout.
                self.app.stylesheet.update_nodes(attached)

    def transcript_entry_after(self, widget: Widget) -> Widget | None:
        """Return the oldest mounted entry stamped after ``widget``: where ``widget`` mounts above.

        Entries mounted before a replay (a restore warning) are stamped before
        every replayed entry, so older history lands below them.
        """
        seq = self._transcript_seqs[widget]
        for child in self.children:
            child_seq = self._transcript_seqs.get(child)
            if child_seq is not None and child_seq > seq and not self._chrome.is_infrastructure(child):
                return child
        return None

    def can_mount_replay(self) -> bool:
        """Whether a deferred replay batch may still mount into this panel."""
        return (
            self.is_attached
            and not self._closing
            and not self._pruning
            and self.app.is_running
            and not self.app._closing
        )

    def report_replay_progress(self, current: int, total: int) -> None:
        """Forward replay mount progress to the screen-owned sink."""
        self._report_replay_progress(current, total)

    def begin_view_hold(self) -> None:
        self._scroll_controller.begin_view_hold()

    def end_view_hold(self) -> None:
        self._scroll_controller.end_view_hold()
        # The prepend has ended: a turn started while it ran may be waiting with nothing moving.
        self._sync_waiting_row()

    def set_replay_progress_callback(self, callback: Callable[[int, int], None] | None) -> None:
        """Set the screen-owned sink for persisted replay progress."""
        self._replay_progress_callback = callback

    def _report_replay_progress(self, current: int, total: int) -> None:
        callback = self._replay_progress_callback
        if callback is not None:
            callback(current, total)

    async def remove_transcript_widget(self, widget: Widget) -> None:
        """Remove a transcript widget if it is still mounted on this panel."""
        if widget.parent is self:
            await remove_shielded(widget)
            self._sync_waiting_row()

    async def dismiss_welcome_for_content(self) -> None:
        """Dismiss the welcome widget before transcript content is mounted."""
        await self._dismiss_welcome()

    def on_live_user_turn_started(self) -> None:
        """Reset live-turn scroll gates without mounting or cleanup side effects."""
        self._scroll_controller.on_user_turn_started()

    def on_replay_started(self) -> None:
        """Reset replay scroll gates after welcome dismissal."""
        self._scroll_controller.on_replay_started()

    def scroll_user_message_to_top(self, widget: Widget) -> None:
        """Scroll a newly mounted user message to the viewport top."""
        # The message is where the user looks now, not a turn a TOC jump still waits for.
        self._replay_mount.forget_jump()
        self._scroll_controller.scroll_user_message_to_top(widget)

    def on_final_response_started(self) -> None:
        """Run the final-response one-shot autoscroll policy."""
        self._scroll_controller.on_final_response_started()

    def schedule_anchor_sync(self) -> None:
        """Schedule a coalesced bottom-anchor sync."""
        self._scroll_controller.schedule_anchor_sync()

    def on_status_message_mounted(self) -> None:
        """Force status messages to the bottom, matching current status rendering."""
        self._scroll_controller.on_status_message_mounted()

    def scroll_inline_prompt_to_top_after_refresh(self, widget: Widget) -> None:
        """Schedule inline ask_user prompt scroll-to-top after refresh."""
        self._scroll_controller.scroll_inline_prompt_to_top_after_refresh(widget)

    def on_inline_prompt_resized(self, group: ToolGroup | None) -> None:
        """Relayout once after a live inline ask_user control changes size."""
        _ = group
        self.refresh(layout=True)
        self._schedule_anchor_sync()

    def after_replay_history_mounted(self) -> None:
        """Schedule the final replay scroll-to-bottom call."""
        self._scroll_controller.after_replay_history_mounted()

    async def remove_trailing_status(self) -> None:
        """StatusCleanupPort wrapper for trailing status removal."""
        await self._remove_trailing_status()

    def failed_turn_status(self) -> Widget | None:
        """Find a failure eligible for replacement by a fresh user message."""
        return self._find_trailing_status(allow_retry_notes=False)

    def preceding_content(self, widget: Widget) -> Widget | None:
        """Find the strict predecessor of a status, independent of later notices."""
        previous = None
        for child in self.children:
            if child is widget:
                return previous
            if not self._chrome.is_infrastructure(child):
                previous = child
        return None

    def mounted_tool_groups(self) -> list[ToolGroup]:
        """Return mounted tool groups in Textual query order."""
        return list(self.query(ToolGroup))

    def mounted_agent_messages(self) -> list[AgentMessage]:
        """Return mounted agent messages in Textual query order."""
        return list(self.query(AgentMessage))

    def mounted_user_messages(self) -> list[UserMessage]:
        """Return mounted user messages in Textual query order."""
        return list(self.query(UserMessage))

    def direct_children(self) -> list[Widget]:
        """Return a snapshot of direct children."""
        return list(self.children)

    def transcript_content_children(self) -> list[Widget]:
        """Return direct children excluding persistent chat chrome infrastructure."""
        return [child for child in self.children if not self._chrome.is_infrastructure(child)]

    def pending_transcript_widgets(self) -> list[Widget]:
        """Return replayed entries still waiting to be prepended, in document order."""
        return self._replay_mount.pending_widgets()

    def set_border_title(self, title: Text | None) -> None:
        self.border_title = title

    def set_border_subtitle(self, subtitle: Text | None) -> None:
        self.border_subtitle = subtitle

    def border_region(self) -> Region:
        return self.region

    def has_border_subtitle(self) -> bool:
        return bool(self.border_subtitle)

    def is_ask_user_interactive_target(self, target: object) -> bool:
        if not isinstance(target, Widget):
            return False
        return any(ancestor.has_class(ASK_USER_INTERACTIVE_CLASS) for ancestor in target.ancestors_with_self)

    def post_title_clicked(self) -> None:
        self.post_message(self.TitleClicked())

    def post_working_dir_clicked(self) -> None:
        self.post_message(self.WorkingDirClicked())

    def call_super_arrange(self, size: Size, *, optimal: bool = False) -> DockArrangeResult:
        return super().arrange(size, optimal=optimal)

    def call_super_scroll_to_region(self, region: Region, **kwargs: Any) -> Offset:
        return super().scroll_to_region(region, **kwargs)

    def call_super_size_updated(
        self,
        size: Size,
        virtual_size: Size,
        container_size: Size,
        layout: bool,
    ) -> bool:
        return super()._size_updated(size, virtual_size, container_size, layout)

    def call_super_watch_scroll_y(self, old_value: float, new_value: float) -> None:
        super().watch_scroll_y(old_value, new_value)

    def is_anchor_released(self) -> bool:
        return self._anchor_released

    def set_anchor_released(self, released: bool) -> None:
        self._anchor_released = released

    def set_scroll_y_reactive(self, y: float) -> None:
        self.set_reactive(Widget.scroll_y, y)

    def set_scroll_target_y_reactive(self, y: float) -> None:
        self.set_reactive(Widget.scroll_target_y, y)

    def set_scroll_target_y(self, y: float) -> None:
        self.scroll_target_y = y

    def stop_scroll_y_animation(self) -> None:
        self.app.animator.force_stop_animation(self, "scroll_y")

    def set_vertical_scrollbar_position(self, y: float) -> None:
        # Like the compositor's own anchor pin: move the thumb without watchers.
        self.vertical_scrollbar.set_reactive(ScrollBar.position, y)

    def get_container_size(self) -> Size:
        return self._container_size

    def set_container_size(self, size: Size) -> None:
        self._container_size = size

    def apply_held_shrink_size_update(self, size: Size, container_size: Size) -> bool:
        changed = False
        if self._size != size:
            self._set_dirty()
            self._size = size
            changed = True
        if self._container_size != container_size:
            self._container_size = container_size
            changed = True
        if changed:
            self._layout_cache.clear()
        return changed

    def place_fixed_scroll_button(self, result: DockArrangeResult, size: Size) -> None:
        self._place_scroll_to_bottom_button(result, size)

    def is_infrastructure(self, widget: Widget) -> bool:
        """Whether *widget* is chat chrome, not a transcript entry."""
        return self._chrome.is_infrastructure(widget)

    def bottom_spacer(self) -> Widget | None:
        return self._bottom_spacer

    def scroll_to_bottom_button(self) -> Widget | None:
        return self._scroll_to_bottom_button

    def set_screen_scroll_required_and_check_idle(self) -> None:
        self.screen._scroll_required = True
        self.screen.check_idle()

    def scroll_to_widget_top(self, widget: Widget) -> None:
        self.scroll_to_widget(widget, animate=False, top=True, immediate=True)

    async def clear(self) -> None:
        """Remove all children and show a fresh welcome widget."""
        # Backend event handlers clear the panel inside the publishing task; a
        # cancelled clear still finishes, so the panel is never left half-built.
        await finish_shielded(self._clear_and_rebuild())

    async def _clear_and_rebuild(self) -> None:
        # A prepend ends at a batch boundary first: never remove children
        # around a mount that is still registering.
        await self._replay_mount.stop()
        self._process_controller.clear()
        self._scroll_controller.reset_for_clear()
        if self.is_attached:
            self.screen.clear_selection()
            cancel_textual_mouse_chain(self.screen)
        # Clear the spacer reference BEFORE awaiting ``remove_children`` —
        # the await yields to the event loop, and any mount scheduled by
        # another coroutine during that window would otherwise see the
        # now-detached spacer via the overridden ``mount()`` and crash with
        # ``MountError: Unable to find relative location``.  The override
        # also guards on ``parent is self`` as a belt-and-braces check.
        self._chrome.detach_infrastructure_refs()
        await self.remove_children()
        self._current_tool_group = None
        self._agent_running = False
        self._live_renderer.reset_for_clear()
        self._tool_registry.reset_for_clear()
        self._toc.clear()

        welcome, waiting_row, bottom_spacer, scroll_button = self._chrome.rebuild_widgets()
        await self.mount(welcome)
        await self.mount(waiting_row)
        await self.mount(bottom_spacer)
        await self.mount(scroll_button, after=bottom_spacer)

    def update_welcome(self, profile: str = "", cwd: str = "") -> None:
        """Keep welcome context in panel state, including while the widget is absent."""
        if profile:
            self.profile_name = profile
        if cwd:
            self.workspace_cwd = cwd
        self._chrome.update_welcome(profile=self.profile_name, cwd=self.workspace_cwd)

    def set_update_notice(self, notice: UpdateNotice | None) -> None:
        """Show a newer release on the welcome screen, now and after every transcript clear."""
        self._chrome.set_update_notice(notice)

    def toggle_fold_all(self) -> bool:
        """Collapse or expand process sections, tool groups, thinking and the text between tool calls.

        If any eligible visible item is expanded, collapse all; otherwise
        expand all, including controls inside process sections. Return the
        new collapsed state.
        """
        folds: list[ToolGroup | StreamedMarkdownMessage] = [
            group for group in self.query(ToolGroup) if not group.collapse_locked
        ]
        # Thinking that still streams keeps the state it has; an answer never folds here.
        folds.extend(message for message in self.query(StreamedMarkdownMessage) if message.foldable)
        processes_collapsed = self._process_controller.folds_collapsed()
        # Entries still waiting to be prepended take the choice as they mount.
        pending_collapsed = self._replay_mount.pending_folds_collapsed()

        if not folds and pending_collapsed is None and processes_collapsed is None:
            return False

        any_expanded = (
            processes_collapsed is False
            or any(not fold.process_hidden and not fold.collapsed for fold in folds)
            or pending_collapsed is False
        )
        with self.app.batch_update():
            self._process_controller.set_fold_state(any_expanded)
            for fold in folds:
                fold.collapsed = any_expanded
            self._replay_mount.set_fold_state(any_expanded)
        return any_expanded

    def get_agent_responses(self) -> list[tuple[str, str]]:
        """Return (profile_name, raw_text) of all agent messages in mount order."""
        return self._transcript_reader.get_agent_responses()

    def get_user_messages(self) -> list[tuple[str, str]]:
        """Return (role_label, raw_text) of all user messages in mount order."""
        return self._transcript_reader.get_user_messages()

    def get_all_messages(self) -> list[tuple[str, str]]:
        """Return (role_label, raw_text) of all user+agent messages in mount order."""
        return self._transcript_reader.get_all_messages()

    # --- helpers ---

    def _end_tool_group(self) -> None:
        """Close current tool group so a new one starts on next tool call."""
        self._tool_registry.end_group()
        self._sync_waiting_row()

    # --- public API ---

    def mark_turn_range_compressed(self, turn_range: tuple[int, int]) -> bool:
        """Mark live TOC entries in the backend turn range as compressed."""
        return self._toc.mark_turn_range_compressed(turn_range)

    async def add_user_message(
        self,
        text: str,
        *,
        is_injection: bool = False,
        created_at: Any = None,
        contents: list[Any] | None = None,
    ) -> None:
        """Display a user message."""
        await self._live_renderer.add_user_message(
            text,
            is_injection=is_injection,
            created_at=created_at,
            contents=contents,
        )

    async def _remove_failed_turn(self) -> None:
        """Remove a trailing failed turn (Error/Interrupted + its preceding UserMessage)."""
        await self._live_renderer._remove_failed_turn()

    async def add_answer_delta(self, segment_id: str, text: str) -> None:
        """Stream new answer text into its segment's message (one message per segment)."""
        await self._live_renderer.add_answer_delta(segment_id, text)
        self._sync_waiting_row()

    def finish_answer_segment(self, segment_id: str, *, completed: bool) -> None:
        """Close a streamed answer segment; a failed or interrupted one keeps its partial text."""
        self._live_renderer.finish_answer_segment(segment_id, completed=completed)
        self._sync_waiting_row()

    async def add_thinking_delta(self, segment_id: str, channel: ContentChannel, text: str) -> None:
        """Stream new thinking text into its segment's message, when thinking is shown."""
        await self._live_renderer.add_thinking_delta(segment_id, channel, text)
        self._sync_waiting_row()

    def finish_thinking_segment(self, segment_id: str) -> None:
        """Close a streamed thinking segment; a failed or interrupted one keeps its partial text."""
        self._live_renderer.finish_thinking_segment(segment_id)
        self._sync_waiting_row()

    async def abandon_thinking(self, segment_ids: tuple[str, ...]) -> None:
        """Remove the thinking a retry gave up with its response."""
        await self._live_renderer.abandon_thinking(segment_ids)
        self._sync_waiting_row()

    async def add_agent_message(
        self,
        text: str,
        *,
        structured_output_completed: bool = False,
        created_at: Any = None,
        turn_started_at: Any = None,
    ) -> None:
        """Complete the turn's answer, whose text already streamed as deltas; it took from *turn_started_at*."""
        await self._live_renderer.add_agent_message(
            text,
            structured_output_completed=structured_output_completed,
            created_at=created_at,
            turn_started_at=turn_started_at,
        )
        self._sync_waiting_row()
        if self._fold_finished_turns():
            with self.app.batch_update():
                await self._process_controller.complete_live()

    def _schedule_anchor_sync(self) -> None:
        """Sync scroll position to the bottom after the next refresh.

        Used by the streaming agent-message path, where
        :class:`VirtualizedMarkdown`'s async ``update()`` means the
        ChatPanel's ``virtual_size`` changes in a later frame — often after
        the tick that triggered it.  ``watch_virtual_size`` may still fire,
        but rapid coalesced ticks can leave the scrollbar out of sync with
        the final content height.  Running ``_sync_anchor_bottom`` via
        ``call_after_refresh`` guarantees it sees the settled
        ``max_scroll_y`` and pushes it through the ``scroll_y`` setter
        (driving ``watch_scroll_y`` → ``ScrollBar.position`` setter →
        scrollbar repaint).
        """
        self._scroll_controller.schedule_anchor_sync()

    def _sync_anchor_bottom(self) -> None:
        """Push ``scroll_y`` to the current ``max_scroll_y`` via setter.

        Re-checks anchor state in case the user scrolled away between the
        schedule and this callback.
        """
        self._scroll_controller.sync_anchor_bottom()

    async def add_tool_start(
        self,
        call_id: str,
        tool_name: str,
        tool_kind: str,
        args_summary: str = "",
        args: dict[str, Any] | None = None,
        *,
        provider_hosted: bool = False,
        hosted_family: str = "",
        provider: str = "",
        provider_item_type: str = "",
        provider_status: str = "",
        provider_call_id: str = "",
        canonical_status: str = "running",
    ) -> None:
        """Add a running tool call (creates tool group if needed)."""
        await self._tool_registry.add_tool_start(
            call_id,
            tool_name,
            tool_kind,
            args_summary,
            args=args,
            provider_hosted=provider_hosted,
            hosted_family=hosted_family,
            provider=provider,
            provider_item_type=provider_item_type,
            provider_status=provider_status,
            provider_call_id=provider_call_id,
            canonical_status=canonical_status,
        )

    async def add_tool_result(
        self,
        call_id: str,
        tool_name: str,
        result: str,
        duration_ms: int = 0,
        *,
        image_contents: list[Any] | None = None,
        file_snapshot: FileSnapshotPayload | None = None,
        approval: str | None = None,
        metadata: dict[str, Any] | None = None,
        artifacts: list[dict[str, Any]] | None = None,
        provider_status: str = "",
        canonical_status: str = "completed",
    ) -> None:
        """Update a tool call as complete."""
        await self._tool_registry.add_tool_result(
            call_id,
            tool_name,
            result,
            duration_ms,
            image_contents=image_contents,
            file_snapshot=file_snapshot,
            approval=approval,
            metadata=metadata,
            artifacts=artifacts,
            provider_status=provider_status,
            canonical_status=canonical_status,
        )
        self._sync_waiting_row()

    def update_tool_progress(
        self,
        call_id: str,
        lines: list[str],
        *,
        image_contents: list[Any] | None = None,
        snapshot_metadata: dict[str, Any] | None = None,
        provider_status: str = "",
    ) -> None:
        """Forward streaming progress lines to a running tool widget."""
        self._tool_registry.update_tool_progress(
            call_id,
            lines,
            image_contents=image_contents,
            snapshot_metadata=snapshot_metadata,
            provider_status=provider_status,
        )

    def update_tool_args(self, call_id: str, args: dict[str, Any]) -> None:
        """Refresh a running tool widget after approval edits its arguments."""
        self._tool_registry.update_tool_args(call_id, args)

    def update_tool_status(
        self,
        call_id: str,
        status: str,
        *,
        provider_status: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Apply a canonical provider-hosted status update."""
        self._tool_registry.update_tool_status(
            call_id,
            status,
            provider_status=provider_status,
            metadata=metadata,
        )

    def show_ask_user_inline(
        self,
        call_id: str,
        request_id: str,
        questions: tuple[AskUserQuestion, ...],
        *,
        draft: PromptDraft | None = None,
    ) -> bool:
        """Show a live ask_user response form inside the matching tool renderer."""
        return self._tool_registry.show_ask_user_inline(call_id, request_id, questions, draft=draft)

    def on_ask_user_inline_resized(self, event: AskUserInlineResized) -> None:
        """Relayout the transcript when live inline ask_user controls grow."""
        self._tool_registry.handle_ask_user_inline_resized(event.call_id)

    def is_tool_running(self, call_id: str) -> bool:
        """Return whether the tool record for ``call_id`` is still running."""
        return self._tool_registry.is_tool_running(call_id)

    def clear_ask_user_inline_prompts(self) -> None:
        """Clear live inline ask_user prompts from every known tool group."""
        self._tool_registry.clear_ask_user_inline_prompts()

    def clear_ask_user_inline(self, call_id: str, request_id: str) -> bool:
        """Clear one request-matched inline ask_user prompt."""
        return self._tool_registry.clear_ask_user_inline(call_id, request_id)

    def link_sub_agent_invocation(
        self,
        parent_call_id: str,
        invocation_id: str,
        agent_name: str,
        sub_agent_log_file: str = "",
        *,
        tool_name: str = "",
    ) -> None:
        """Bind an ``invocation_id`` to the widget mounted for ``parent_call_id``."""
        self._tool_registry.link_sub_agent_invocation(
            parent_call_id,
            invocation_id,
            agent_name,
            sub_agent_log_file,
            tool_name=tool_name,
        )

    def add_sub_agent_answer(self, agent_name: str, invocation_id: str, segment_id: str, text: str) -> None:
        """Add one whole answer segment of a sub-agent to its nested transcript."""
        self._tool_registry.add_sub_agent_answer(agent_name, invocation_id, segment_id, text)

    async def add_sub_agent_tool_start(
        self,
        agent_name: str,
        invocation_id: str,
        tool_name: str,
        args: dict[str, Any],
        call_id: str,
        *,
        tool_kind: str = "",
        provider_hosted: bool = False,
        hosted_family: str = "",
        provider: str = "",
        provider_item_type: str = "",
        provider_status: str = "",
        provider_call_id: str = "",
    ) -> None:
        """Route an inner tool call start to the correct SubAgentToolCall widget.

        :class:`InvocationStarted` establishes the route first, using
        exact parent ownership or the internal-tool-name FIFO fallback in
        :meth:`link_sub_agent_invocation`. Subsequent events resolve strictly
        by ``invocation_id``; display names never participate in identity.
        """
        await self._tool_registry.add_sub_agent_tool_start(
            agent_name,
            invocation_id,
            tool_name,
            args,
            call_id,
            tool_kind=tool_kind,
            provider_hosted=provider_hosted,
            hosted_family=hosted_family,
            provider=provider,
            provider_item_type=provider_item_type,
            provider_status=provider_status,
            provider_call_id=provider_call_id,
        )

    def update_sub_agent_tool_args(self, invocation_id: str, call_id: str, args: dict[str, Any]) -> None:
        """Update one nested sub-agent tool's arguments."""
        self._tool_registry.update_sub_agent_tool_args(invocation_id, call_id, args)

    def update_sub_agent_tool_status(
        self,
        invocation_id: str,
        call_id: str,
        status: str,
        *,
        metadata: dict[str, Any] | None = None,
        provider_status: str = "",
    ) -> None:
        """Apply one nested sub-agent tool lifecycle status."""
        self._tool_registry.update_sub_agent_tool_status(
            invocation_id,
            call_id,
            status,
            metadata=metadata,
            provider_status=provider_status,
        )

    def update_sub_agent_tool_progress(
        self,
        invocation_id: str,
        call_id: str,
        lines: list[str],
        *,
        image_contents: list[Any] | None = None,
        snapshot_metadata: dict[str, Any] | None = None,
        provider_status: str = "",
    ) -> None:
        """Update one nested sub-agent tool progress snapshot."""
        self._tool_registry.update_sub_agent_tool_progress(
            invocation_id,
            call_id,
            lines,
            image_contents=image_contents,
            snapshot_metadata=snapshot_metadata,
            provider_status=provider_status,
        )

    def complete_sub_agent_tool(
        self,
        agent_name: str,
        invocation_id: str,
        call_id: str,
        result: str,
        duration_ms: int,
        image_contents: list[Any] | None = None,
        artifacts: list[dict[str, Any]] | None = None,
        approval: str | None = None,
        metadata: dict[str, Any] | None = None,
        provider_status: str = "",
        canonical_status: str = "completed",
    ) -> None:
        """Route an inner tool call result to the correct SubAgentToolCall widget."""
        self._tool_registry.complete_sub_agent_tool(
            agent_name,
            invocation_id,
            call_id,
            result,
            duration_ms,
            image_contents=image_contents,
            artifacts=artifacts,
            approval=approval,
            metadata=metadata,
            provider_status=provider_status,
            canonical_status=canonical_status,
        )

    def update_sub_agent_progress(
        self,
        invocation_id: str,
        tool_call_count: int,
        total_tokens: int,
        total_usage_tokens: int = 0,
        usage_unreported_attempts: int = 0,
    ) -> None:
        """Update progress stats on a running SubAgentToolCall widget."""
        self._tool_registry.update_sub_agent_progress(
            invocation_id,
            tool_call_count,
            total_tokens,
            total_usage_tokens,
            usage_unreported_attempts,
        )

    def sub_agent_retry_attempt(
        self,
        invocation_id: str,
        message: str,
        attempt: int,
        max_attempts: int,
        delay_seconds: int,
    ) -> None:
        """Dispatch a sub-agent auto-retry banner to the correct card."""
        self._tool_registry.sub_agent_retry_attempt(invocation_id, message, attempt, max_attempts, delay_seconds)

    def sub_agent_paused(
        self,
        invocation_id: str,
        reason: str,
        last_error: str,
        retry_attempts: int,
        diagnostic_path: str | None = None,
        last_error_display: str | None = None,
    ) -> None:
        """Dispatch a pause transition to the correct card."""
        self._tool_registry.sub_agent_paused(
            invocation_id,
            reason,
            last_error,
            retry_attempts,
            diagnostic_path,
            last_error_display,
        )

    def sub_agent_resumed_after_pause(self, invocation_id: str) -> None:
        """Dispatch a resume-after-pause transition to the correct card."""
        self._tool_registry.sub_agent_resumed_after_pause(invocation_id)

    def sub_agent_answer_withdrawn(self, invocation_id: str, segment_ids: tuple[str, ...]) -> None:
        """Dispatch the answer a sub-agent's retry dropped to its card."""
        self._tool_registry.sub_agent_answer_withdrawn(invocation_id, segment_ids)

    def sub_agent_cascade_aborted(self, invocation_id: str) -> None:
        """Dispatch a cascade-abort transition (from global interrupt)."""
        self._tool_registry.sub_agent_cascade_aborted(invocation_id)

    def sub_agent_aborted(self, invocation_id: str, last_error: str) -> None:
        """Dispatch a user-abort transition to the correct card.

        Clears the paused banner as soon as ``InvocationAborted`` arrives,
        independent of the trailing parent ``ToolCallResult``.  The card
        ends in the ``-aborted`` style (distinct from ``-error`` so the
        user can tell their own Abort click from a sub-agent crash).
        """
        self._tool_registry.sub_agent_aborted(invocation_id, last_error)

    async def add_context_fold(
        self,
        compressed_context_id: str,
        summary: str,
        freed_messages: int = 0,
        turn_range: tuple[int, int] = (0, 0),
    ) -> None:
        """Display a context compression event."""
        await self._live_renderer.add_context_fold(compressed_context_id, summary, freed_messages, turn_range)

    async def add_compaction_start(self, compaction_id: str) -> None:
        """Mount a live Phase-4 compaction card for the main agent."""
        await self._live_renderer.add_compaction_start(compaction_id)

    def complete_compaction(
        self,
        compaction_id: str,
        *,
        outcome: str,
        duration_ms: int = 0,
        last_words: str = "",
        format_violation: str = "",
        failure_reason: str = "",
    ) -> None:
        """Finalize the matching live compaction card."""
        self._live_renderer.complete_compaction(
            compaction_id,
            outcome=outcome,
            duration_ms=duration_ms,
            last_words=last_words,
            format_violation=format_violation,
            failure_reason=failure_reason,
        )
        self._sync_waiting_row()

    def show_compaction_retry(self, message: str, attempt: int, max_attempts: int, delay_seconds: int) -> None:
        """Show a Phase-4 side-call retry notice inside the running compaction card."""
        self._live_renderer.show_compaction_retry(message, attempt, max_attempts, delay_seconds)

    def add_sub_agent_compaction_start(self, agent_name: str, invocation_id: str, compaction_id: str) -> None:
        """Show a live compaction line on the matching sub-agent card."""
        self._tool_registry.add_sub_agent_compaction_start(agent_name, invocation_id, compaction_id)

    def complete_sub_agent_compaction(
        self,
        invocation_id: str,
        compaction_id: str,
        *,
        outcome: str,
        duration_ms: int = 0,
        format_violation: str = "",
        failure_reason: str = "",
    ) -> None:
        """Flip the compaction line on the matching sub-agent card."""
        self._tool_registry.complete_sub_agent_compaction(
            invocation_id,
            compaction_id,
            outcome=outcome,
            duration_ms=duration_ms,
            format_violation=format_violation,
            failure_reason=failure_reason,
        )

    def record_sub_agent_compaction_committed(self, invocation_id: str, compaction_id: str) -> None:
        """Bump the compaction counter on the matching sub-agent card."""
        self._tool_registry.record_sub_agent_compaction_committed(invocation_id, compaction_id)

    async def add_system(self, text: str, *, key: str | None = None) -> None:
        """Display a system message.

        If ``key`` is provided, the message will be tagged with this key so it
        can be updated or removed later (used for things like profile switch
        de-duplication).
        """
        await self._live_renderer.add_system(text, key=key)

    async def update_system(self, key: str, new_text: str) -> None:
        """Update the first system message matching the given key (if any)."""
        await self._live_renderer.update_system(key, new_text)

    async def remove_system(self, key: str) -> None:
        """Remove the first system message matching the given key (if any)."""
        await self._live_renderer.remove_system(key)
        self._sync_waiting_row()

    async def add_interrupted(self, reason: str = "Execution interrupted", source: str = "user") -> None:
        """Display an interruption notice, replacing any trailing error/interrupted widget."""
        await self._live_renderer.add_interrupted(reason, source)

    async def add_error(self, text: str, *, action_label: str | None = "Retry") -> None:
        """Display an error message, replacing any trailing error/interrupted widget."""
        await self._live_renderer.add_error(text, action_label=action_label)

    async def add_retry(
        self,
        message: str,
        attempt: int,
        max_attempts: int,
        delay_seconds: int,
        *,
        deadline: float | None = None,
        wait_id: str = "",
    ) -> None:
        """Display a retry notice, replacing any trailing status widget."""
        await self._live_renderer.add_retry(
            message, attempt, max_attempts, delay_seconds, deadline=deadline, wait_id=wait_id
        )

    async def prepare_retry(self) -> None:
        """Finalise pending widgets before a main-agent stream retry.

        The backend's :class:`StreamRetryLoop` rolls ``chrys_history`` back
        to the pre-attempt snapshot before the next attempt, so every
        intermediate widget mounted during the failed run (assistant
        intermediate text, its tool group, any sub-agent cards still
        running inside it) is now disconnected from the authoritative
        history.  Without cleanup, the next attempt's fresh intermediate
        text would create a NEW widget while ``_current_tool_group`` and
        friends still point at the OLD one — causing subsequent
        ``ToolCallStart`` events to mount sub-agent cards under the
        wrong assistant block.

        This method:

        1. Marks any still-running tool cards in the open group as
           "cancelled" (the backend's stream cleanup hooks have already
           cancelled the underlying tool tasks; this is the matching
           visual terminal state for the old invocations).
        2. Closes the open tool group and clears
           ``_current_tool_group``/``_unlinked_sub_agents`` so the next
           ``ToolCallStart`` creates a fresh group.
        3. Clears ``_current_agent_msg`` so the next streamed text
           mounts a new widget under which the next group will attach.

        Completed tool cards from the failed attempt stay visible — they
        carry real results the user may want to inspect.  The retry
        notice itself (``add_retry``) is mounted by the caller AFTER
        this cleanup so it sits between the old run's artefacts and the
        new run's output.
        """
        await self._live_renderer.prepare_retry()
        self._sync_waiting_row()

    def hide_trailing_status_action(self) -> None:
        """Hide the inline action on the current trailing error/interruption."""
        last = self._find_trailing_status()
        if isinstance(last, (ErrorMessage, InterruptedMessage)):
            last.hide_action()

    async def withdraw_answer(self, segment_ids: tuple[str, ...]) -> None:
        """Remove the answer text a bare Continue or Retry dropped from history: the messages these segments showed.

        Tool cards stay, as they do for a retried attempt. Entries a replay has
        yet to mount are mounted first, so none of them comes back.
        """
        if self.replay_in_progress:
            await self.wait_replay_complete()
        await remove_withdrawn_answer(self, segment_ids)
        self._sync_waiting_row()

    def set_trailing_status_action_disabled(self, disabled: bool) -> None:
        """Keep the retry action visible but unavailable during admission."""
        last = self._find_trailing_status()
        if isinstance(last, (ErrorMessage, InterruptedMessage)):
            last.set_action_disabled(disabled)

    async def _remove_trailing_status(self) -> None:
        """Remove the active status widget, allowing trailing retry notes."""
        status = self._find_trailing_status()
        if status is not None:
            await remove_shielded(status)
            self._sync_waiting_row()

    def _find_trailing_status(
        self, *, allow_retry_notes: bool = True
    ) -> ErrorMessage | InterruptedMessage | RetryMessage | None:
        """Find the active status past context notices and mid-turn retry notes.

        Action updates and removal must agree on the same card. A new user
        turn or run output is a boundary; older status cards stay untouched.
        Fresh-submit cleanup also treats a pending retry note as newer input.
        """
        for child in reversed(list(self.children)):
            if self._chrome.is_infrastructure(child):
                continue
            if isinstance(child, (ErrorMessage, InterruptedMessage, RetryMessage)):
                return child
            if allow_retry_notes and isinstance(child, UserMessage) and child.is_injection:
                continue
            if isinstance(child, SystemMessage):
                continue
            return None
        return None

    async def cancel_running_tools(self) -> None:
        """Cancel all in-progress tool calls."""
        await self._live_renderer.cancel_running_tools()
        self._sync_waiting_row()

    async def replay_history(
        self,
        messages: list[dict[str, Any]],
        initial_profile: str = "",
        file_snapshots: dict[str, list[FileSnapshotPayload]] | None = None,
        compressed_blocks: dict[str, CompressedBlock] | None = None,
        recovered_from_sidecar: bool = False,
    ) -> None:
        """Replay saved session messages into the chat panel.

        Reconstructs user messages, agent text responses, and tool call groups
        from the deserialized message history (list of dicts with 'role' and
        'contents').  Intermediate text (agent text returned alongside tool
        calls) is read from each message's
        ``additional_properties["_intermediate_text"]``.

        Args:
            messages: Deserialized session messages.
        """
        self._replay_process_protect_last = recovered_from_sidecar
        try:
            await self._replay_renderer.replay_history(
                messages,
                initial_profile=initial_profile,
                file_snapshots=file_snapshots,
                compressed_blocks=compressed_blocks,
            )
        finally:
            self._replay_process_protect_last = False

    async def _replay_compressed_block(self, block: CompressedBlock, replay_profile: str) -> str:
        """Compatibility delegate for compressed-block replay rendering."""
        return await self._replay_renderer._replay_compressed_block(block, replay_profile)

    async def _replay_assistant(
        self,
        contents: list[Any],
        profile_name: str = "",
        is_intermediate: bool = False,
        created_at: Any = None,
    ) -> None:
        """Compatibility delegate for assistant replay rendering."""
        await self._replay_renderer._replay_assistant(
            contents,
            profile_name=profile_name,
            is_intermediate=is_intermediate,
            created_at=created_at,
        )

    @property
    def toc_items(self) -> list[TocItem]:
        return self._toc.items()

    def scroll_to_turn(self, turn_id: str) -> None:
        # A turn older than the replayed tail, or in a batch not laid out yet, is jumped to once it lays out.
        if self._replay_mount.jump_when_laid_out(turn_id):
            return
        for child in self.children:
            if isinstance(child, UserMessage) and child.id == turn_id:
                for c in self.query(UserMessage):
                    c.remove_class("-highlighted")
                child.add_class("-highlighted")
                if self._agent_running:
                    self._auto_scroll_paused_by_user = True
                self.scroll_to_widget(child, animate=False, top=True, immediate=True)
                # After the scroll: its geometry read can run the layout that queues a settle re-pin.
                self._scroll_controller.note_toc_jump()
                # Bypass the async widget → UpdateScroll → screen idle chain
                # that can lose the race against the next render frame.
                self.set_screen_scroll_required_and_check_idle()
                return
