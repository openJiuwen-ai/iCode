# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""ChatPanel — scrollable chat area that dynamically mounts message widgets.

Replaces the old WorkPanel (plain Log) with rich, interactive widgets for
each message type: user, agent, tool calls, context folds, errors, etc.
"""

from __future__ import annotations

import itertools
import logging
from asyncio import Lock
from collections.abc import Awaitable, Callable, Iterable, Sequence
from contextlib import nullcontext
from fractions import Fraction
from functools import partial
from typing import TYPE_CHECKING, Any
from weakref import ReferenceType, WeakKeyDictionary, WeakSet
from weakref import ref as weakref_ref

from rich.text import Text
from textual import events as textual_events
from textual._animator import SimpleAnimation
from textual._arrange import DockArrangeResult, arrange
from textual.color import Color
from textual.containers import VerticalScroll
from textual.dom import NoScreen
from textual.geometry import Offset, Region, Size, Spacing
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
    from asyncio import Task

    from textual.dom import DOMNode
    from textual.events import Click
    from textual.layout import Layout

    from chrys.app.tui.i18n import LocaleController
    from chrys.app.tui.widgets import PromptDraft
    from chrys.app.update_check import UpdateNotice
    from chrys.foundation.i18n import MessageRef
    from chrys.foundation.models.ask_user import AskUserQuestion
    from chrys.foundation.models.invocations import ContentChannel
    from chrys.service.context.providers.history import CompressedBlock
    from chrys.service.session.sub_agent_transcript import PersistedSubAgentTranscript
    from chrys.service.state.folds import FoldItem

from chrys.app.tui.widgets.chat.chrome import (
    ChatBottomSpacer,
    ChatPanelChrome,
    ScrollToBottomButton,
    ScrollToBottomRequested,
)
from chrys.app.tui.widgets.chat.context_fold import ContextFoldWidget
from chrys.app.tui.widgets.chat.file_snapshot import FileSnapshotKey, FileSnapshotPayload
from chrys.app.tui.widgets.chat.fold_gap import show_fold_gaps
from chrys.app.tui.widgets.chat.guide_fold import GuideFoldRequested, GuideHoverMixin
from chrys.app.tui.widgets.chat.history_scroll import (
    HistoryRestoreTurn,
    HistoryScrollController,
    HistoryScrollRequest,
    RelativeHistoryScroll,
)
from chrys.app.tui.widgets.chat.history_viewport import HistorySelectionRange, HistoryViewport, rebase_history_row
from chrys.app.tui.widgets.chat.live_process_row import LiveProcessRow
from chrys.app.tui.widgets.chat.live_renderer import LiveTranscriptRenderer
from chrys.app.tui.widgets.chat.messages import (
    ERROR_RETRY_ACTION,
    AgentMessage,
    ErrorMessage,
    InterruptedMessage,
    MessageRollbackButton,
    RetryMessage,
    StreamedMarkdownMessage,
    SystemMessage,
    UserMessage,
    remove_withdrawn_answer,
)
from chrys.app.tui.widgets.chat.ports import TranscriptLocalizationPort
from chrys.app.tui.widgets.chat.process_block import ProcessBlock
from chrys.app.tui.widgets.chat.process_controller import ProcessController
from chrys.app.tui.widgets.chat.process_visibility import LiveStatusChanged
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
from chrys.app.tui.widgets.chat.waiting_row import waits_after
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


def _history_turn_top(layout: DockArrangeResult, turn: UserMessage) -> int | None:
    # Match Textual's scroll_to_widget, which keeps the target's margin in view.
    return next((max(0, p.region.y - p.margin.top) for p in layout.placements if p.widget is turn), None)


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
        thinking_expanded: Callable[[], bool] | None = None,
        sub_agent_transcript_loader: Callable[[str], Awaitable[PersistedSubAgentTranscript | None]] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        # Document-order stamps for top-level transcript children; weak keys
        # so removed widgets drop their entries with the widget itself.
        self._transcript_seqs: WeakKeyDictionary[Widget, Fraction] = WeakKeyDictionary()
        self._transcript_occurrences: WeakKeyDictionary[Widget, int] = WeakKeyDictionary()
        self._guide_refresh_pending = False
        self._process_controller = ProcessController(self)
        # Keep replay, stop, Continue and detached injection delivery from changing an in-flight process placement.
        self._turn_completion_lock = Lock()
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
        self._rollback_targets: set[int] = {0}
        self._rollback_preview_borders: dict[Widget, tuple[object, Callable[[Task[None]], None]]] = {}
        self._rollback_preview_start: UserMessage | None = None
        # The card captured when the user starts a retry; ``captured`` also
        # records "there was no old card", so a later TurnStarted never re-reads
        # the transcript after a new interrupt card has mounted.
        self._retry_settled_status: ReferenceType[Widget] | None = None
        self._retry_settled_status_captured = False
        self._tool_registry = ToolGroupRegistry(
            self,
            self,
            self,
            tool_widget_configurator=self._configure_tool_widget,
            workspace_cwd=self._current_workspace_cwd,
            process=self._process_controller,
            mount_live_group=self._mount_live_tool_group,
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
            rollback_targets=lambda: self._rollback_targets,
            process=self._process_controller,
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
            rollback_targets=lambda: self._rollback_targets,
        )
        self._transcript_reader = TranscriptReader(self)
        self._replay_mount = ReplayMountController(self)
        self._replay_lock = Lock()
        self._replay_generation = 0
        self._history_viewport: HistoryViewport | None = None
        self._history_window: tuple[int, int] | None = None
        self._history_layout_entries: list[Widget] | None = None
        self._arranging_history = False
        self._history_jump: UserMessage | None = None
        self._history_selection = False
        self._history_selection_window: tuple[int, int] | None = None
        self._history_selection_applied = False
        self._history_scroll = HistoryScrollController(
            self._replay_mount,
            self._cancel_history_scroll_animation,
            self._history_window_ready,
            self._history_window_failed,
        )
        self._history_native_pending = False
        self._history_native_failed = False
        self._history_highlight: UserMessage | None = None
        self._history_drag_pointer: float | None = None
        self._history_drag_rebase = False
        self._history_drag_geometry: tuple[int, int] | None = None
        self._history_selection_needs_layout = False
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
        if old_running and not running:
            self._process_controller.release_live_rows()
            self.run_worker(self._remove_released_process_rows, group="live-process-removal", exit_on_error=False)
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

    def reveal_transcript_region(self, region: Region) -> None:
        """Reveal rows above the floating bottom button with the shortest scroll."""
        button = self._scroll_to_bottom_button
        bottom = button.region.height if button is not None and self.scroll_y < self.max_scroll_y else 0
        self.scroll_to_region(region, spacing=Spacing(0, 0, bottom, 0), animate=False, immediate=True, x_axis=False)

    def _check_anchor(self) -> None:
        """Re-engage bottom-follow only at exact bottom."""
        if not self.history_layout_is_held() and self._history_scroll.relative is None:
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

    async def _mount_live_tool_group(self, group: ToolGroup) -> bool:
        """Settle prose before the new group so the live header starts above both."""
        process = self._process_controller
        if (intermediate := self._live_renderer.fold_before_tools()) is not None:
            await process.add_intermediate(intermediate)
        mounted = await process.mount_live_member(group)
        self._sync_waiting_row()
        return mounted

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
        if transcript:
            self.record_transcript_occurrences(widgets)
        for widget in widgets:
            if isinstance(widget, Widget) and widget not in self._transcript_seqs:
                self._transcript_seqs[widget] = Fraction(next(_transcript_seq_counter))
        mounted = super().mount(*widgets, **kwargs)
        if transcript and self._rollback_preview_start is not None:
            self.call_after_refresh(self._extend_rollback_preview, widgets)
        if transcript and any(widget.display for widget in widgets):
            # Registered as children already: new output hides the row before it paints. A tool group mounts hidden
            # until its first call, and hides the row as it starts.
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
        last: Widget | None = None
        latest = 0
        for row in self._process_controller.waiting_live_rows():
            occurrence = self._waiting_occurrence(row)
            if occurrence > latest:
                last, latest = row, occurrence

        # Completed segments can reorder an answer after their activity. Check the last two candidates.
        remaining = 2
        for child in reversed(self.children):
            occurrence = self._transcript_occurrences.get(child)
            if occurrence is not None and not isinstance(child, (LiveProcessRow, ProcessBlock)):
                shown = child.display
                if shown and occurrence > latest:
                    last, latest = child, occurrence
                # A hidden entry already covered by a newer candidate can use a
                # slot too. This lets us stop inside a long folded segment.
                if shown or occurrence <= latest:
                    remaining -= 1
                    if not remaining:
                        break
            if isinstance(child, UserMessage) and not child.is_injection:
                break
        return waits_after(last)

    def _waiting_occurrence(self, row: LiveProcessRow) -> int:
        # A folded preview represents its latest member while that member is hidden.
        if (
            (member := row.latest) is not None
            and not member.display
            and (stamp := self.transcript_occurrence(member)) is not None
        ):
            return stamp
        return self._transcript_occurrences.get(row, 0)

    def record_transcript_occurrences(self, widgets: Iterable[Widget]) -> None:
        """Record live arrival order or raw replay order before display placement."""
        for widget in widgets:
            if widget not in self._transcript_occurrences:
                self._transcript_occurrences[widget] = next(_transcript_seq_counter)

    def transcript_occurrence(self, widget: Widget) -> int | None:
        return self._transcript_occurrences.get(widget)

    def apply_transcript_order(self, widgets: list[Widget]) -> None:
        """Apply display order within the supplied entries, keeping their document key slots."""
        wanted = set(widgets)
        current = [child for child in self.current_turn_children() if child in wanted]
        if len(current) != len(widgets):
            # Replay can land a continued turn's prompt after a newer turn has already started.
            current = [child for child in self.children if child in wanted]
        if current == widgets or len(current) != len(widgets):
            return
        slots = sorted(self._transcript_seqs[widget] for widget in current)
        positions = {widget: index for index, widget in enumerate(widgets)}
        for index, widget in enumerate(widgets):
            if current[index] is widget:
                continue
            displaced = current[index]
            if positions[displaced] > index + 1 and current[index + 1] is widget:
                end = positions[displaced]
                self.move_child(displaced, after=current[end])
                current.insert(end, current.pop(index))
            else:
                self.move_child(widget, before=displaced)
                current.insert(index, current.pop(current.index(widget, index + 1)))
        for widget, stamp in zip(widgets, slots, strict=True):
            self._transcript_seqs[widget] = stamp
        self._schedule_selection_reprojection()
        self._sync_waiting_row()

    def transcript_seq(self, widget: Widget) -> Fraction | None:
        """Document-order stamp for a top-level child; ``None`` if never
        mounted through this panel."""
        return self._transcript_seqs.get(widget)

    def forget_transcript_widget(self, widget: Widget) -> None:
        self._transcript_seqs.pop(widget, None)

    def stamp_transcript_order(self, widgets: list[Widget]) -> None:
        """Stamp document order up front for entries mounted newest first."""
        for widget in widgets:
            if widget not in self._transcript_seqs:
                self._transcript_seqs[widget] = Fraction(next(_transcript_seq_counter))

    async def place_transcript_entry(self, widget: Widget, anchor: Widget, *, before: bool = False) -> bool:
        """Place a header next to its anchor without changing other order keys."""
        if not self.can_mount_replay() or anchor.parent is not self or anchor._pruning:
            return False
        anchor_seq = self._transcript_seqs[anchor]
        entries = self.current_turn_children()
        if entries and anchor_seq < self._transcript_seqs.get(entries[0], anchor_seq):
            # A replayed title or its late prompt can be above the latest turn; use its actual neighbors.
            entries = self.transcript_content_children()
        neighbors = (seq for node in entries if node is not widget and (seq := self.transcript_seq(node)) is not None)
        neighbor_seq = (
            max((seq for seq in neighbors if seq < anchor_seq), default=anchor_seq - 1)
            if before
            else min((seq for seq in neighbors if seq > anchor_seq), default=anchor_seq + 1)
        )
        if before and (pending := self._replay_mount.last_pending_entry()) is not None:
            pending_seq = self._transcript_seqs[pending]
            if neighbor_seq < pending_seq < anchor_seq:
                neighbor_seq = pending_seq
        self._transcript_seqs[widget] = (anchor_seq + neighbor_seq) / 2
        if widget.parent is self:
            if before:
                self.move_child(widget, before=anchor)
            else:
                self.move_child(widget, after=anchor)
            self._schedule_selection_reprojection()
        else:
            await self.mount(widget, before=anchor if before else None, after=None if before else anchor)
        return widget.parent is self and not widget._pruning

    def on_tool_group_process_state_changed(self, event: ToolGroup.ProcessStateChanged) -> None:
        event.stop()
        if self._process_controller.owner_of(event.group) is None:
            return
        with self.app.batch_update():
            self._process_controller.member_changed(event.group)
        self.process_members_changed()
        self._sync_waiting_row()

    def process_members_changed(self) -> None:
        """Refresh a stationary guide once newly visible members have laid out."""
        if not self._guide_refresh_pending:
            self._guide_refresh_pending = self.call_after_refresh(self._refresh_hovered_guide)

    def _refresh_hovered_guide(self) -> None:
        self._guide_refresh_pending = False
        if (hover := self.app.mouse_over) is not None:
            for node in hover.ancestors_with_self:
                if isinstance(node, GuideHoverMixin) and node.parent is self:
                    node.refresh_guide_hover()
                    return

    def on_live_status_changed(self, event: LiveStatusChanged) -> None:
        event.stop()
        self._process_controller.live_status_changed(event.member)

    def on_live_process_row_toggle_requested(self, event: LiveProcessRow.ToggleRequested) -> None:
        event.stop()
        self._toggle_live_process_row(event.row)

    def _toggle_live_process_row(self, row: LiveProcessRow) -> None:
        if row.parent is self:
            with self.app.batch_update():
                self._process_controller.set_live_row_collapsed(row, not row.collapsed)
            self._sync_waiting_row()

    def on_process_block_toggle_requested(self, event: ProcessBlock.ToggleRequested) -> None:
        event.stop()
        self._toggle_process_block(event.block)

    def _toggle_process_block(self, block: ProcessBlock, *, guide_row: int | None = None) -> None:
        if block.parent is not self:
            return
        was_following = self._anchored and not self._anchor_released
        self.pause_following()
        with self.app.batch_update():
            if guide_row is not None and not was_following:
                self._scroll_controller.place_guide_fold_at(block, guide_row)
            block.requested_collapsed = not block.requested_collapsed
            block.try_finalize_fold()
        self.process_members_changed()

    def pause_following(self) -> None:
        """Hold the view while the user opens transcript content to read it."""
        self.note_user_scroll()
        self.set_anchor_released(True)
        self._auto_scroll_paused_by_user = True

    def process_guide_for(self, member: Widget) -> Widget | None:
        owner = self._process_controller.guide_owner_of(member)
        if owner is None or owner.parent is not self or owner.collapsed:
            return None
        if isinstance(owner, ProcessBlock) and owner.blocked:
            return None
        if any(isinstance(entry, ToolGroup) and entry.collapse_locked for entry in owner.members):
            return None
        return owner

    def on_guide_fold_requested(self, event: GuideFoldRequested) -> None:
        event.stop()
        member = event.member
        edge = event.screen_y - self.content_region.y
        if not event.process:
            with self.app.batch_update():
                member.fold_member_guide(
                    self, before_fold=partial(self._scroll_controller.place_guide_fold_at, member, edge)
                )
            return
        if member.parent is not self or not member.display:
            return
        target = self.process_guide_for(member)
        if target is None:
            return
        with self.app.batch_update():
            if isinstance(target, LiveProcessRow):
                self._scroll_controller.place_guide_fold_at(target, edge)
                self._toggle_live_process_row(target)
            elif isinstance(target, ProcessBlock):
                self._toggle_process_block(target, guide_row=edge)

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
        """Hold GC for the whole gesture and preserve the pending thumb origin."""
        self._history_drag_pointer = None
        self._history_drag_rebase = False
        self._history_drag_geometry = None
        if grabbed is not None:
            if self._history_scroll.pending is not None:
                # The visible thumb already points to the pending page. Keep
                # it as the grab origin if the page mounts during this drag.
                self._history_drag_pointer = 0.0
            elif not self._history_scroll.restoring_tail:
                self.note_user_scroll()
            self._scroll_controller.on_scrollbar_grab()
        else:
            self._scroll_controller.on_scrollbar_release()
            if self._history_scroll.mount_timer is not None and (viewport := self._history_viewport) is not None:
                self._history_scroll.start_pending_mount(viewport)
            self._check_anchor_after_history_scroll()

    def on_unmount(self) -> None:
        self._history_scroll.stop_mount_timer()
        self._clear_rollback_preview()
        self._process_controller.clear()
        if self._locale_controller is not None:
            self._locale_controller.unregister_surface(self)
        self._replay_mount.abandon()
        self._scroll_controller.stop()

    def refresh_localization(self) -> None:
        """Retranslate bounded chat chrome without walking transcript children."""
        self._chrome.refresh_localization()
        self._process_controller.refresh_localization()

    def pre_layout(self, layout: Layout) -> None:
        if self._history_layout_entries is None:
            entries = self.displayed_children
        else:
            entries = [entry for entry in self._history_layout_entries if entry.display]
        waiting = self._chrome.waiting_row
        tail = next(
            (entry for entry in reversed(entries) if entry is waiting or not self.is_infrastructure(entry)), None
        )
        show_fold_gaps(entries, tail=tail)

    def arrange(self, size: Size, optimal: bool = False) -> DockArrangeResult:
        """Defuse Textual anchor pins while chat layout is settling.

        The compositor writes ``scroll_y`` directly from arranged height.
        Keep that math current, clamp stale negative pins, leave out the
        spacer row past content that fills the viewport, and skip one
        anchored shrink frame; ``_size_updated`` handles the validator side.
        """
        if self._arranging_history:
            # A descendant being measured can query the compositor and
            # reenter this method. The outer pass alone commits row changes.
            if (
                self.history_layout_is_held() or self._history_scroll.relative is not None
            ) and self._history_scroll.visible_layout is not None:
                return self._held_history_layout(self._history_scroll.visible_layout)
            viewport = self._history_viewport
            if viewport is not None and (previous := viewport.last_layout) is not None:
                return self._held_history_layout(previous)
            return super().arrange(size, optimal=optimal)
        self._arranging_history = self._history_viewport is not None
        try:
            result = self._scroll_controller.arrange(size, optimal=optimal)
        finally:
            self._arranging_history = False
        if not optimal and (jump := self._history_jump) is not None and not self.history_layout_is_held():
            self._history_jump = None
            if (top := _history_turn_top(result, jump)) is not None:
                max_y = max(0, result.total_region.bottom - size.height)
                self._apply_history_scroll_offset(min(max_y, top) - self.scroll_y)
                self._check_anchor_after_history_scroll(maximum=max_y)
        if not optimal and self._history_viewport is not None and not self.history_layout_is_held():
            self._publish_history_layout(result, self.scroll_y)
        return result

    def _publish_history_layout(self, result: DockArrangeResult, scroll_y: float) -> None:
        self._history_scroll.publish(result, scroll_y, self._nodes._updates)

    def history_layout_is_held(self) -> bool:
        """Whether the last visible geometry is still waiting for its replacement."""
        scroll = self._history_scroll
        return (
            scroll.pending is not None
            or scroll.holding_failed_layout
            or self._history_native_pending
            or self._history_native_failed
        )

    def history_layout_is_windowed(self) -> bool:
        """Whether the arrangement has a viewport-sized set of archive pages."""
        return self._history_window is not None

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
        if self._history_scroll.restoring_tail:
            self._history_scroll.startup = None
            return
        self.note_user_scroll()
        if self.scroll_y != self.max_scroll_y:
            self._schedule_selection_reprojection()
        self._scroll_controller.jump_to_bottom()

    def note_user_scroll(self, *, keep_history_mount: bool = False) -> None:
        """A scroll the user asked for supersedes a TOC jump still waiting for its turn to mount.

        Hooked on user intents (wheel, scrollbar, keys), never on ``scroll_y``:
        the prepend itself moves ``scroll_y`` to keep the view steady.
        """
        self._replay_mount.forget_jump()
        self._retry_failed_history_layout()
        self._history_scroll.restoring_tail = False
        self._history_scroll.startup = None
        self._history_scroll.check_bottom_after_layout = False
        self._cancel_history_jump()
        self._scroll_controller.cancel_pending_guide_fold()
        if keep_history_mount:
            self._history_scroll.relative = None
            if self._history_scroll.pending is not None:
                self._history_scroll.pending = None
                self.vertical_scrollbar.position = self.scroll_y
        else:
            self._cancel_history_request()

    def on_resize(self, _event: object | None = None) -> None:
        """Refresh the bottom-jump affordance when viewport height changes."""
        self._chrome.resize()
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
        changed = self._scroll_controller.size_updated(size, virtual_size, container_size, layout)
        if layout and self._history_selection_needs_layout and self._history_selection_measured():
            self._history_selection_needs_layout = False
        viewport = self._history_viewport
        scroll = self._history_scroll
        if (
            layout
            and viewport is not None
            and scroll.visible_layout is not None
            and not self.history_layout_is_held()
            and viewport.is_current(scroll.visible_layout)
        ):
            # Textual can pin or clamp the offset after arrange() publishes.
            scroll.visible_row = self.scroll_y
            if scroll.restoring_tail:
                scroll.restoring_tail = False
                self._anchor_released = False
                if scroll.startup is not None:
                    self._queue_restored_scroll_after_layout(self._replay_generation)
        if self._history_drag_pointer is not None:
            scrollbar = self.vertical_scrollbar
            geometry = scrollbar.window_virtual_size, scrollbar.window_size
            if self._history_drag_rebase or geometry != self._history_drag_geometry:
                self._history_drag_geometry = geometry
                self._rebase_history_drag()
        if (
            layout
            and scroll.check_bottom_after_layout
            and self.virtual_size == virtual_size
            and not self.history_layout_is_held()
            and scroll.relative is None
        ):
            scroll.check_bottom_after_layout = False
            self._check_anchor_after_history_scroll()
        return changed

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
        # User events retire a pending request before choosing their next
        # coordinate. Geometry reads and scroll clamps also run this watcher
        # and can occur between the request and its first visible layout.
        self._scroll_controller.watch_scroll_y(old_value, new_value)
        if self._history_window is not None and (viewport := self._history_viewport) is not None:
            height = self.size.height
            required = viewport.window(new_value, height, following=False, overscan=0)
            first, stop = required
            if (
                viewport.last_layout is self._history_scroll.visible_layout
                and self._history_window[0] <= first
                and stop <= self._history_window[1]
            ):
                self._history_scroll.visible_row = new_value
                if (
                    self._history_scroll.relative is None
                    and self._history_scroll.pending is None
                    and self._history_scroll.has_request
                ):
                    self._cancel_history_request()
            else:
                self.refresh(layout=True)
        self._schedule_selection_reprojection()

    def _schedule_selection_reprojection(self, *, preserve_pending_quote: bool = False) -> None:
        """Let the owning screen re-align chat selection highlights.

        The chat drag-selection model keeps ``screen.selections`` bounded to
        the visible transcript slice. Scrolling or moving an entry can change
        which widgets the selection covers.
        """
        try:
            screen = self.screen
        except NoScreen:
            return
        schedule = getattr(screen, "schedule_chat_selection_reprojection", None)
        if schedule is not None:
            schedule(
                self,
                preserve_pending_quote=preserve_pending_quote or self._scroll_controller.programmatic_scroll,
            )

    def _history_selection_measured(self) -> bool:
        if self.history_layout_is_held():
            return False
        window = self._history_window
        selection = self._history_selection_window
        viewport = self._history_viewport
        return window is None or (
            selection is not None
            and (
                selection[0] == selection[1]
                or window[0] <= selection[0] <= selection[1] <= window[1]
                or (
                    viewport is not None
                    and viewport.last_selection_window == selection
                    and self._history_scroll.visible_layout is viewport.last_layout
                )
            )
        )

    def history_selection_changed(self, selecting: bool, *, selection: HistorySelectionRange | None = None) -> None:
        """Use exact layout while a selection needs offscreen history."""
        if selecting == self._history_selection:
            return
        self._history_selection = selecting
        self._history_selection_applied = False
        self._history_selection_needs_layout = selecting and self._history_window is not None
        viewport = self._history_viewport
        self._history_selection_window = (
            viewport.selection_window(selection, self.transcript_seq)
            if selecting and selection is not None and viewport is not None
            else None
        )
        if viewport is not None:
            self.refresh(layout=True)

    async def prepare_history_selection(self) -> None:
        """Mount the archive pages needed for the current selection."""
        viewport = self._history_viewport
        window = self._history_selection_window
        if viewport is None or window is None:
            await self.wait_replay_complete()
        else:
            await self._replay_mount.wait_complete(widgets=viewport.widgets_in(window))

    def history_selection_applied(self) -> None:
        """Release the range required for the completed selection's next action."""
        self._history_selection_applied = True

    def _refresh_history_layout(self) -> None:
        self.refresh(layout=True)

    def _on_mouse_scroll_up(self, event: textual_events.MouseScrollUp) -> None:
        self._scroll_history_wheel(event, -1)

    def _on_mouse_scroll_down(self, event: textual_events.MouseScrollDown) -> None:
        self._scroll_history_wheel(event, 1)

    def _scroll_history_wheel(
        self, event: textual_events.MouseScrollUp | textual_events.MouseScrollDown, direction: int
    ) -> None:
        if not (event.ctrl or event.shift) and self._scroll_history_lines(direction * self.app.scroll_sensitivity_y):
            event.prevent_default()
            event.stop()
        elif isinstance(self._history_scroll.startup, HistoryRestoreTurn):
            self._history_scroll.startup = None
        elif not self._history_scroll.restoring_tail and self._history_scroll.startup is None:
            # Textual applies ordinary wheel input later in the MRO.
            self.note_user_scroll()
        self._scroll_controller.pause_gc_for_manual_scroll()

    def _on_scroll_to(self, message: ScrollTo) -> None:
        """Pause cyclic GC before scrollbar thumb drags update scroll state."""
        # Do not call super(): Textual will dispatch Widget._on_scroll_to
        # later in the same MRO handler walk, restarting the drag scroll work.
        viewport = self._history_viewport
        if message.y is None or (viewport is None and self._history_drag_pointer is None):
            self.note_user_scroll()
        else:
            scrollbar = self.vertical_scrollbar
            position = self.validate_scroll_y(message.y)
            if scrollbar.grabbed is not None and scrollbar.window_virtual_size:
                # Keep the grab threshold at the edge when the pointer has
                # overshot it. Otherwise a small move outside the track can
                # move content after the virtual extent changes.
                self._history_drag_pointer = (
                    (position - scrollbar.grabbed_position) * scrollbar.window_size / scrollbar.window_virtual_size
                )
                self._history_drag_geometry = scrollbar.window_virtual_size, scrollbar.window_size
            # ScrollTo messages can be delivered after ScrollBar has already
            # released its grab. The queued coordinate remains a drag update.
            if viewport is None:
                self.note_user_scroll()
            elif self._history_native_pending or self._history_native_failed:
                # Once the index is abandoned, the thumb's fraction is the
                # position that also makes sense in the native document.
                fraction = position / self.max_scroll_y if self.max_scroll_y else 0.0
                relative = RelativeHistoryScroll(position, native_origin_fraction=fraction)
                self._stage_relative_history_scroll(relative, moving_up=position < self.scroll_y)
                scrollbar.position = position
            else:
                self._scroll_to_history_intent(position, x=message.x)
            if viewport is not None:
                message.prevent_default()
                message.stop()
        self._scroll_controller.pause_gc_for_manual_scroll()

    def _check_anchor_after_history_scroll(self, *, maximum: int | None = None) -> None:
        # Finish handling the gesture first: a no-op scroll can release the
        # anchor even though the scroll_y watcher never gets another tick.
        if (
            self.scroll_y == self.scroll_target_y
            and self.vertical_scrollbar.grabbed is None
            and not self.history_layout_is_held()
            and self._history_scroll.relative is None
            and (maximum is not None or not self._history_scroll.check_bottom_after_layout)
        ):
            bottom = self.max_scroll_y if maximum is None else maximum
            self._scroll_controller.resume_following_at_bottom(maximum=bottom)
            if self.scroll_y >= bottom:
                self._history_scroll.check_bottom_after_layout = False

    def _on_scroll_up(self, event: ScrollUp) -> None:
        self._scroll_history_track(event, -1)

    def _on_scroll_down(self, event: ScrollDown) -> None:
        self._scroll_history_track(event, 1)

    def _scroll_history_track(self, event: ScrollUp | ScrollDown, direction: int) -> None:
        if (
            not self.allow_vertical_scroll
            and not self._history_scroll.restoring_tail
            and self._history_scroll.startup is None
        ):
            # Textual handles the track click later in its MRO dispatch.
            self.note_user_scroll()
            return
        # Textual pages from the painted row. Include the request that is
        # still waiting for its archive page before advancing another page.
        self.user_scroll_page(direction, animate=True)
        event.prevent_default()
        event.stop()

    def _scroll_to_history_intent(self, position: float, *, x: float | None = None) -> None:
        """Cancel loading only when an input returns to the last published row."""
        same_row = position == self._history_scroll.visible_row
        already_painted = same_row and self.scroll_y == position
        measuring = self.history_layout_is_held() or self._history_scroll.relative is not None
        self.note_user_scroll(keep_history_mount=not same_row)
        self._history_scroll.check_bottom_after_layout |= measuring
        if x is not None or not already_painted:
            self.scroll_to(x=x, y=None if already_painted else position, animate=False, immediate=True)
        if same_row:
            self._check_anchor_after_history_scroll()

    def _scroll_history_lines(self, distance: float) -> bool:
        """Advance from the requested row using Textual's rounded line target."""
        if self._history_scroll.queue_startup_step(distance):
            return True
        if not self.allow_vertical_scroll:
            return False
        if self._request_relative_history_scroll(distance, use_target=True):
            return True
        pending = self._history_scroll.pending
        if not self._history_scroll.has_request:
            return False
        row = self.scroll_target_y if pending is None else self.validate_scroll_target_y(pending.row)
        self._scroll_to_history_intent(self.validate_scroll_y(row + distance))
        return True

    def action_scroll_up(self) -> None:
        if not self._scroll_history_lines(-1):
            self.note_user_scroll()
            super().action_scroll_up()

    def action_scroll_down(self) -> None:
        if not self._scroll_history_lines(1):
            self.note_user_scroll()
            super().action_scroll_down()

    def action_page_up(self) -> None:
        if self.allow_vertical_scroll or self._history_scroll.restoring_tail:
            self.user_scroll_page(-1, animate=True)
        else:
            super().action_page_up()

    def action_page_down(self) -> None:
        if self.allow_vertical_scroll or self._history_scroll.restoring_tail:
            self.user_scroll_page(1, animate=True)
        else:
            super().action_page_down()

    def user_scroll_page(self, direction: int, *, animate: bool = False) -> None:
        """Page relative to the latest requested row, including a cold viewport."""
        distance = direction * self.scrollable_content_region.height
        if self._history_scroll.queue_startup_step(distance):
            return
        if self._request_relative_history_scroll(distance):
            return
        pending = self._history_scroll.pending
        if self._history_scroll.has_request:
            row = self._relative_scroll_base() if pending is None else pending.row
            position = self.validate_scroll_y(row + distance)
            self._scroll_to_history_intent(position)
            return
        target = self._relative_scroll_base() + distance
        self.note_user_scroll()
        self.scroll_to(y=target, animate=animate, immediate=True)

    def _relative_scroll_base(self) -> float:
        if self.app.animator.is_being_animated(self, "scroll_y"):
            return self.scroll_target_y
        return self.scroll_y

    def _request_relative_history_scroll(self, distance: float, *, use_target: bool = False) -> bool:
        viewport, window = self._history_viewport, self._history_window
        if viewport is None or window is None:
            return False
        scroll = self._history_scroll
        if self._history_jump is not None:
            # A relative input cancels an unresolved TOC jump and starts
            # from the view the user actually saw.
            remeasure = viewport.last_layout is not scroll.visible_layout
            visible = self._painted_row_in_history_index()
            painted_origin = scroll.visible_anchor()
            self.note_user_scroll()
            self._commit_history_scroll(visible, visible)
            if remeasure:
                relative = RelativeHistoryScroll(visible, [distance], painted_origin=painted_origin)
                self._stage_relative_history_scroll(relative, moving_up=distance < 0)
                return True
            if distance > 0 and visible >= self.max_scroll_y:
                self._check_anchor_after_history_scroll(maximum=self.max_scroll_y)
                return True
        relative = scroll.relative
        index_is_painted = viewport.last_layout is scroll.visible_layout
        native_pending = self._history_native_pending or self._history_native_failed
        height = self.scrollable_content_region.height
        if relative is None:
            if native_pending:
                origin = self._painted_row_in_history_index()
            elif (pending := scroll.pending) is not None:
                # The pending row can already be in a newer index than Textual's scroll limits.
                origin = pending.row
            else:
                # Landing and cancellation both commit their row before the next paint.
                origin = self.scroll_target_y if use_target else self._relative_scroll_base()
            if use_target:
                origin = round(origin)
            # Use Textual's scroll limits only when they describe this index.
            # A newer index resolves the steps during arrangement instead.
            if index_is_painted and not native_pending:
                target = self.validate_scroll_y(origin + distance)
                # Home needs its first page measured before committing the step.
                first_row = 0.0 if origin == 0 else target
                first, stop = viewport.window(
                    first_row, height + round(target) - round(first_row), following=False, overscan=0
                )
                if window[0] <= first <= stop <= window[1]:
                    if not scroll.has_request:
                        return False
                    self._scroll_to_history_intent(target)
                    return True
            relative = RelativeHistoryScroll(origin)
            if native_pending or origin == self._painted_row_in_history_index():
                relative.painted_origin = scroll.visible_anchor()
        relative.steps.append(distance)
        if (
            not native_pending
            and relative.origin == scroll.visible_row
            and index_is_painted
            and not relative.from_bottom
        ):
            target, _, _, clamped = relative.target_and_span(self.max_scroll_y, height)
            if not clamped:
                first, stop = viewport.window(target, height, following=False, overscan=0)
                if window[0] <= first <= stop <= window[1]:
                    self._scroll_to_history_intent(target)
                    return True
        self._stage_relative_history_scroll(relative, moving_up=distance < 0)
        return True

    def _stage_relative_history_scroll(self, relative: RelativeHistoryScroll, *, moving_up: bool) -> None:
        self.note_user_scroll(keep_history_mount=True)
        self._cancel_history_scroll_animation()
        self.release_anchor()
        if self._agent_running and moving_up:
            self._auto_scroll_paused_by_user = True
        self._history_scroll.relative = relative
        self.refresh(layout=True)

    async def _prepare_restore_scroll(self, generation: int, widgets: list[Widget]) -> None:
        try:
            await self._replay_mount.wait_for_visible_content(widgets)
        except Exception:
            logger.exception("Preparing expanded restored history for navigation failed")
        if generation == self._replay_generation and self._history_scroll.startup is not None:
            self.refresh(layout=True)
            self._queue_restored_scroll_after_layout(generation, prepared_fold=self._replay_mount.fold_generation)

    def _queue_restored_scroll_after_layout(self, generation: int, *, prepared_fold: int | None = None) -> None:
        if not self.call_after_refresh(self._apply_restore_scroll, generation, prepared_fold):
            self._history_scroll.startup = None

    def _apply_restore_scroll(self, generation: int, prepared_fold: int | None) -> None:
        scroll = self._history_scroll
        if generation != self._replay_generation or scroll.restoring_tail or (pending := scroll.startup) is None:
            return
        viewport, window = self._history_viewport, self._history_window
        if self._replay_mount.expanding_replay and prepared_fold != self._replay_mount.fold_generation:
            # The last arrangement may omit folds that opened after it was published.
            widgets = [child for child in self.children if viewport is None or viewport.includes(window, child)]
            self.run_worker(
                partial(self._prepare_restore_scroll, generation, widgets),
                name="prepare expanded restored history scroll",
                exit_on_error=False,
            )
            return
        if isinstance(pending, HistoryRestoreTurn):
            self.scroll_to_turn(pending.turn_id)
            return
        origin = float(self.max_scroll_y) if pending.from_bottom else 0.0
        relative = RelativeHistoryScroll(origin, pending.steps, from_bottom=pending.from_bottom)
        row, span_start, span_height, _ = relative.target_and_span(
            self.max_scroll_y, self.scrollable_content_region.height
        )
        if pending.steps and viewport is not None and window is not None:
            first, stop = viewport.window(span_start, span_height, following=False, overscan=0)
            if not window[0] <= first <= stop <= window[1]:
                self._stage_relative_history_scroll(relative, moving_up=row < self.scroll_y)
                return
        self._scroll_to_history_intent(row)

    def action_scroll_home(self) -> None:
        if self._history_scroll.queue_startup_home():
            return
        if self._history_native_pending or self._history_native_failed:
            self._stage_relative_history_scroll(RelativeHistoryScroll(0.0), moving_up=True)
            return
        self.note_user_scroll()
        if self.allow_vertical_scroll:
            self.scroll_home(animate=False, immediate=True, x_axis=self.scroll_y == 0)
        else:
            super().action_scroll_home()

    def action_scroll_end(self) -> None:
        if self.allow_vertical_scroll or self._history_scroll.restoring_tail:
            self.jump_to_bottom()
            return
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

        Older entries mount on demand when a viewport index is available.
        Otherwise a background task prepends them. :meth:`wait_replay_complete`
        mounts any remaining entries and waits for them.
        """
        generation = self._replay_generation
        async with self._replay_lock:
            if generation != self._replay_generation or not self.can_mount_replay():
                return
            can_start_windowed = self._replay_mount.can_start_windowed
            if self._replay_mount.windowed:
                await self._replay_mount.wait_complete()
                if generation != self._replay_generation or not self.can_mount_replay():
                    return
            self.record_transcript_occurrences(widgets)
            self._process_controller.remember_replay_failures(process_failures_after)
            widgets = self._process_controller.prepare_replay(
                widgets, protect_last_turn=self._replay_process_protect_last
            )
            viewport = HistoryViewport(widgets)
            cold = viewport.prime(self.size) if can_start_windowed else None
            self._drop_history_viewport()
            if cold is None:
                await self._replay_mount.mount(widgets)
                if generation == self._replay_generation and viewport.available and self.can_mount_replay():
                    self._history_viewport = viewport
                    self.refresh(layout=True)
                return
            prefix_count, tail_start, initial_window = cold
            if not await self._replay_mount.mount(
                widgets, windowed_tail_start=tail_start, windowed_prefix_count=prefix_count
            ):
                return
            self._history_viewport = viewport
            self._history_scroll.restoring_tail = True
            self._history_scroll.visible_row = self.scroll_y
            self._set_history_window(initial_window)
            self.refresh(layout=True)

    @property
    def replay_in_progress(self) -> bool:
        """Whether older replayed entries are still being prepended."""
        return self._replay_mount.in_progress and not self._replay_mount.windowed

    async def wait_replay_complete(self) -> None:
        """Wait until every replayed entry is mounted (or the prepend stopped)."""
        draining = self._replay_mount.windowed and self._replay_mount.has_pending
        viewport = self._history_viewport
        try:
            await self._replay_mount.wait_complete()
        finally:
            if (
                draining
                and viewport is not None
                and self._history_viewport is viewport
                and not self._replay_mount.has_pending
            ):
                viewport.invalidate_measurements()
                self.refresh(layout=True)

    async def mount_replay_batch(self, batch: list[Widget], *, before: Widget | None) -> None:
        """Mount one bounded replay batch and wait for its descendants to be ready."""
        # Archive windows stay outside the published layout until they are
        # ready. Only progressive replay can show an initializing batch.
        show_placeholder = not self._replay_mount.needs_mount(batch)
        if show_placeholder:
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
            # document there. Keep the prior batch boundary until every
            # descendant is actually ready to paint.
            descendants = [descendant for widget in batch for descendant in widget.walk_children()]
            for descendant in descendants:
                await descendant._mounted_event.wait()
        finally:
            if show_placeholder:
                attached: list[Widget] = []
                for widget in batch:
                    widget.remove_class(_REPLAY_PLACEHOLDER_CLASS, update=False)
                    if widget.is_attached:
                        attached.append(widget)
                if attached:
                    self.app.stylesheet.update_nodes(attached)
        mounted_prompt = any(isinstance(widget, UserMessage) and not widget.is_injection for widget in batch)
        if mounted_prompt or any(isinstance(widget, ProcessBlock) for widget in batch):
            try:
                async with self._turn_completion_lock:
                    with self.app.batch_update():
                        if mounted_prompt and await self._process_controller.complete_replayed_turn(
                            self._replay_mount.fold_choice
                        ):
                            self.process_members_changed()
                        await self._process_controller.resume_replayed_title()
                        self._sync_waiting_row()
            except Exception:
                logger.exception("Updating the continued replayed turn failed")

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
            self._clear_selection_touching(lambda node: node is widget)
            await remove_shielded(widget)
            self._sync_waiting_row()

    def _clear_selection_touching(self, removes: Callable[[DOMNode], bool]) -> None:
        """Retire a selection and its quote offer before any of its widgets go away."""
        if not self.is_attached:
            return
        screen = self.screen
        leaves = list(screen.selections)
        state = screen._select_state
        if state is not None:
            leaves.extend(point.content_widget or point.container for point in (state.start, state.end) if point)
        if any(removes(node) for leaf in leaves for node in leaf.ancestors_with_self):
            dragging = screen._selecting
            screen.clear_selection()
            if dragging:
                cancel_textual_mouse_chain(screen)

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
        self.note_user_scroll()
        self._scroll_controller.scroll_user_message_to_top(widget)

    def on_final_response_started(self) -> None:
        """Run the final-response one-shot autoscroll policy."""
        if not self._scroll_controller.final_response_started:
            self.note_user_scroll()
        self._scroll_controller.on_final_response_started()

    def schedule_anchor_sync(self) -> None:
        """Schedule a coalesced bottom-anchor sync."""
        self._scroll_controller.schedule_anchor_sync()

    def on_status_message_mounted(self) -> None:
        """Force status messages to the bottom, matching current status rendering."""
        self.note_user_scroll()
        self._scroll_controller.on_status_message_mounted()

    def scroll_inline_prompt_to_top_after_refresh(self, widget: Widget) -> None:
        """Schedule inline ask_user prompt scroll-to-top after refresh."""
        self.note_user_scroll()
        self._scroll_controller.scroll_inline_prompt_to_top_after_refresh(widget)

    def on_inline_prompt_resized(self, group: ToolGroup | None) -> None:
        """Relayout once after a live inline ask_user control changes size."""
        _ = group
        self.refresh(layout=True)
        self._schedule_anchor_sync()

    def after_replay_history_mounted(self) -> None:
        """Schedule the final replay scroll-to-bottom call."""
        if self._replay_mount.windowed and self._history_viewport is not None:
            # The mounted suffix's max_scroll_y can still be in effect here.
            # Let the first indexed layout resolve the actual bottom.
            self._anchor_released = False
            self.refresh(layout=True)
        else:
            self._scroll_controller.after_replay_history_mounted()

    async def remove_trailing_status(self, *, replacing: bool = False) -> None:
        """StatusCleanupPort wrapper for trailing status removal."""
        await self._remove_trailing_status(replacing=replacing)

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

    def current_turn_children(self) -> list[Widget]:
        """Return the mounted turn in display order, starting at its prompt."""
        entries = []
        for child in reversed(self.children):
            if self._chrome.is_infrastructure(child):
                continue
            entries.append(child)
            if isinstance(child, UserMessage) and not child.is_injection:
                break
        entries.reverse()
        return entries

    def pending_transcript_widgets(self) -> list[Widget]:
        """Return replayed entries still waiting to mount, in document order."""
        return self._replay_mount.pending_widgets()

    def selection_needs_history_layout(self, first: Fraction | int | None, last: Fraction | int | None) -> bool:
        """Whether a logical selection would cross unmounted or unmeasured entries."""

        if self._history_selection_needs_layout:
            return True

        def in_range(widget: Widget) -> bool:
            stamp = self.transcript_seq(widget)
            return stamp is not None and (first is None or first <= stamp) and (last is None or stamp <= last)

        viewport = self._history_viewport
        archived_end = viewport.last_archived_entry if viewport is not None else None
        end_stamp = self.transcript_seq(archived_end) if archived_end is not None else None
        tail_only = first is not None and end_stamp is not None and first > end_stamp
        if not tail_only and self._replay_mount.has_cold_entry(in_range):
            return True
        if self._history_window is None or self._history_scroll.visible_layout is None:
            return False
        children: Iterable[Widget] = self.children
        if tail_only:
            # Skip the archive, but check live entries inserted after the last layout.
            children = itertools.takewhile(lambda widget: widget is not archived_end, reversed(self.children))
        laid_out = self._history_scroll.visible_layout.widgets
        return any(
            widget not in laid_out and widget.display and widget.visible and in_range(widget) for widget in children
        )

    def set_border_title(self, title: Text | None) -> None:
        if title is None and self.border_title is None:
            return
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
        viewport = self._history_viewport
        if viewport is None or optimal or self.replay_in_progress:
            return super().arrange(size, optimal=optimal)
        relative = self._history_scroll.relative
        jump = self._history_jump
        following = (
            relative is None
            and jump is None
            and (self._history_scroll.restoring_tail or (self._anchored and not self._anchor_released))
        )
        self._history_scroll.holding_failed_layout = False
        cold_pending = self._replay_mount.windowed and self._replay_mount.has_pending
        native_pending = self._history_native_pending or self._history_native_failed
        if not cold_pending and not viewport.finish_mounting(self.children, self._nodes._updates):
            return self._arrange_without_history_viewport(viewport, size)
        if native_pending and cold_pending:
            return self._history_layout_during_drain(viewport, size)
        selection_window = self._history_selection_window
        full_selection = self._history_selection and selection_window is None and not cold_pending
        if not viewport.measured or full_selection or native_pending:
            rebase = self._history_window is not None and not following
            self._set_history_window(None)
            result = super().arrange(size, optimal=optimal)
            bottom = self._scroll_controller.history_scroll_bottom(result, size)
            positioned = self._finish_native_history_scroll(viewport, result, bottom, size.height)
            if not viewport.measured or not viewport.is_current(result):
                delta = viewport.capture(
                    result,
                    size.width,
                    round(self.scroll_y) if rebase and not positioned else None,
                )
                if not positioned:
                    self._apply_history_scroll_offset(delta, maximum=bottom)
            if following and self._history_scroll.pending is None:
                # Selection reads this frame's geometry before Textual accepts
                # the smaller virtual size, so keep the measured tail in view.
                self._apply_history_scroll_offset(bottom - self.scroll_y)
            if native_pending:
                self._drop_history_viewport(after_native_layout=True)
            return result
        jump_row = viewport.page_row(jump) if jump is not None else None
        scroll_row = self.scroll_y if jump_row is None else jump_row
        if relative is not None:
            last_layout = viewport.last_layout
            bottom = self.max_scroll_y if last_layout is None else max(0, last_layout.total_region.bottom - size.height)
            scroll_row, span_start, span_height, _ = relative.target_and_span(bottom, size.height)
        elif jump_row is None and self._history_scroll.pending is not None:
            scroll_row = self._history_scroll.pending.row
        elif cold_pending and not following and jump_row is None:
            scroll_row = self._cold_animation_destination(viewport, scroll_row, size.height)
        previous_window = self._history_window
        previous_layout = self._history_scroll.visible_layout
        if relative is None:
            span_start, span_height = scroll_row, size.height
        required = viewport.window(span_start, span_height, following=following, overscan=0)
        if self._history_selection_applied or (
            selection_window is not None and viewport.needs_mount(selection_window, self._replay_mount.needs_mount)
        ):
            selection_window = None
        key = (size, self._nodes._updates, optimal)
        folding = self._scroll_controller.fold_layout_pending
        if (
            relative is None
            and jump is None
            and not folding
            and not following
            and not self._history_scroll.has_request
            and previous_window is not None
            and previous_window[0] <= required[0] <= required[1] <= previous_window[1]
            and (cached := self._arrangement_cache.get(key)) is not None
            and viewport.is_current(cached)
            and viewport.last_selection_window == selection_window
        ):
            return cached
        window = viewport.window(scroll_row, size.height, following=following)
        if relative is not None:
            window = min(window[0], required[0]), max(window[1], required[1])
        if cold_pending and self._history_window is not None:
            if self._defer_history_window(viewport, required, scroll_row, size, following=following):
                if previous_layout is not None:
                    # Keep both the published geometry and the corrected
                    # destination until the requested window can be arranged.
                    return self._held_history_layout(previous_layout)
                window = self._history_window
            else:
                window = viewport.ready_window(required, window, self._replay_mount.needs_mount)
        self._set_history_window(window)
        cached = self._arrangement_cache.get(key)
        if (
            relative is None
            and cached is not None
            and jump is None
            and not folding
            and viewport.is_current(cached)
            and viewport.last_selection_window == selection_window
        ):
            return cached
        while True:
            anchor_row = None if following or jump is not None else round(self.scroll_y)
            if relative is not None:
                anchor_row = None if relative.from_bottom else round(relative.origin)
            measured = viewport.arrange(
                self.children,
                self._nodes._updates,
                size.width,
                window,
                anchor_row,
                partial(self._arrange_history_entries, size),
                cold_count=self._replay_mount.unregistered_cold_count,
                selection_window=selection_window,
            )
            if measured is None:
                if cold_pending:
                    self._history_native_pending = True
                    self._cancel_history_request(keep_relative=relative is not None)
                    self.run_worker(
                        partial(self._drain_for_native_history_layout, viewport),
                        name="drain history for native layout",
                        exit_on_error=False,
                    )
                    return self._history_layout_during_drain(viewport, size)
                return self._arrange_without_history_viewport(viewport, size)
            result, delta = measured
            bottom = self._scroll_controller.history_scroll_bottom(result, size)
            # Remeasurement can shrink a previously estimated page enough to
            # expose another page. Measure that page in this frame too.
            if relative is not None:
                if relative.origin:
                    relative.origin += delta
                top, span_start, span_height, _ = relative.target_and_span(bottom, size.height)
            else:
                self._apply_history_scroll_offset(delta, maximum=bottom)
                top = bottom if following else min(bottom, self.scroll_y)
                if jump is not None and (jump_top := _history_turn_top(result, jump)) is not None:
                    top = min(bottom, jump_top)
                elif folding:
                    top = self._scroll_controller.fold_scroll_target(result, size.height, bottom, top)
                elif (
                    jump is None
                    and not following
                    and self.scroll_y > bottom
                    and self.app.animator.is_being_animated(self, "scroll_y")
                ):
                    # Complete the rebased scroll when shrinking displaced its starting row.
                    top = min(bottom, self.scroll_target_y)
                span_start, span_height = top, size.height
            first, stop = viewport.window(span_start, span_height, following=False, overscan=0)
            expanded = min(window[0], first), max(window[1], stop)
            if expanded == window:
                break
            requested_y = top if jump is None else scroll_row
            if cold_pending and self._defer_history_window(viewport, expanded, requested_y, size, following=following):
                if folding and jump is None and not following:
                    # The fold can already have removed content from the
                    # previous frame. Keep the folded header in a covered
                    # viewport while the exact pointer row's pages mount.
                    covered = viewport.covered_scroll_row(window, top, size.height, bottom)
                    if covered is not None:
                        self._publish_history_layout(result, covered)
                        self._hold_history_view()
                        return result
                if previous_layout is not None:
                    # Keep publishing the held arrangement. The request row
                    # and its next anchor both use the new measurement.
                    self._set_history_window(previous_window)
                    return self._held_history_layout(previous_layout)
                break
            window = expanded
            self._set_history_window(window)
        self._arrangement_cache[key] = result
        if relative is not None and self._history_scroll.pending is None:
            self._finish_relative_history_scroll(relative, bottom, size.height)
        elif jump is None and not following and self._history_scroll.pending is None:
            self._apply_history_scroll_offset(top - self.scroll_y)
        if following and (self._history_scroll.pending is None or previous_layout is None):
            # Do not expose a frame below the estimated extent while the
            # controller defers the final anchored shrink to the next layout.
            self._apply_history_scroll_offset(bottom - self.scroll_y)
            if self._history_scroll.pending is not None:
                # The first indexed layout may still need to mount the page
                # above this suffix. Hold the measured tail, not a stale row
                # from before the archive was indexed.
                self._publish_history_layout(result, self.scroll_y)
        return result

    def _history_layout_during_drain(self, viewport: HistoryViewport, size: Size) -> DockArrangeResult:
        layout = self._history_scroll.visible_layout or viewport.last_layout
        if layout is None:
            return super().arrange(size)
        self._hold_history_view()
        return self._attached_history_layout(layout)

    def _held_history_layout(self, layout: DockArrangeResult) -> DockArrangeResult:
        if self._history_scroll.visible_nodes_version != self._nodes._updates:
            return self._attached_history_layout(layout)
        return layout

    def _attached_history_layout(self, layout: DockArrangeResult) -> DockArrangeResult:
        """Preserve held positions without publishing roots removed since measurement."""
        widgets = {widget for widget in layout.widgets if widget.parent is self}
        if len(widgets) == len(layout.widgets):
            return layout
        placements = [placement for placement in layout.placements if placement.widget.parent is self]
        return DockArrangeResult(placements, widgets, layout.scroll_spacing)

    async def _drain_for_native_history_layout(self, viewport: HistoryViewport) -> None:
        if self._history_viewport is not viewport or not self._history_native_pending:
            return
        try:
            await self._replay_mount.wait_complete()
        except Exception:
            if self._history_viewport is viewport:
                self._history_window_failed()
                self._history_native_failed = True
            raise
        if self._history_viewport is viewport and self.can_mount_replay():
            self._refresh_history_layout()

    def _cold_animation_destination(self, viewport: HistoryViewport, row: float, height: int) -> float:
        """Keep the destination when an animation encounters unmounted history."""
        target = self.scroll_target_y
        if round(row) == round(target) or not self.app.animator.is_being_animated(self, "scroll_y"):
            return row
        current_window = viewport.window(row, height, following=False, overscan=0)
        target_window = viewport.window(target, height, following=False, overscan=0)
        target_is_cold = viewport.needs_mount(target_window, self._replay_mount.needs_mount)
        if target_is_cold:
            return target
        if viewport.needs_mount(current_window, self._replay_mount.needs_mount):
            self._apply_history_scroll_offset(target - self.scroll_y)
            return target
        return row

    def _defer_history_window(
        self, viewport: HistoryViewport, window: tuple[int, int], row: float, size: Size, *, following: bool
    ) -> bool:
        pending = self._history_scroll.pending
        relative = self._history_scroll.relative
        request = HistoryScrollRequest(window, row, following, size)
        dragging = relative is None and self._history_jump is None and self.vertical_scrollbar.grabbed is not None
        if not self._history_scroll.defer(viewport, request, dragging=dragging):
            self._cancel_history_request(keep_relative=relative is not None)
            if pending is not None and relative is None:
                # A resize may make the destination ready before its callback.
                self._land_history_scroll(row, following)
            return False
        self._hold_history_view()
        if not self._history_scroll.holding_failed_layout:
            self.vertical_scrollbar.position = min(self.max_scroll_y, max(0.0, row))
        return True

    def _hold_history_view(self) -> None:
        self.set_scroll_y_reactive(self._history_scroll.visible_row)
        self._history_drag_rebase = False

    def _history_window_ready(self, request: HistoryScrollRequest) -> None:
        if self._history_scroll.relative is None:
            self._land_history_scroll(request.row, request.follow_tail)
        self.refresh(layout=True)

    def _finish_relative_history_scroll(self, relative: RelativeHistoryScroll, bottom: int, height: int) -> None:
        row, _, _, _ = relative.target_and_span(bottom, height)
        self._history_scroll.relative = None
        self._commit_history_scroll(row, row)
        self._check_anchor_after_history_scroll(maximum=bottom)

    def _arrange_without_history_viewport(self, viewport: HistoryViewport, size: Size) -> DockArrangeResult:
        self._set_history_window(None)
        result = super().arrange(size)
        bottom = self._scroll_controller.history_scroll_bottom(result, size)
        self._finish_native_history_scroll(viewport, result, bottom, size.height)
        self._drop_history_viewport(after_native_layout=True)
        return result

    def _finish_native_history_scroll(
        self, viewport: HistoryViewport, result: DockArrangeResult, bottom: int, height: int
    ) -> bool:
        scroll = self._history_scroll
        preserve_painted = (
            (self._history_native_pending or self._history_native_failed)
            and self._history_jump is None
            and self._anchor_released
        )
        painted_layout, painted_row = scroll.visible_layout, scroll.visible_row
        if scroll.relative is not None or preserve_painted:
            # An animation's coordinates belong to the old layout.
            self._cancel_history_scroll_animation()
        self._history_native_pending = False
        self._history_native_failed = False
        if (relative := scroll.relative) is not None:
            if relative.native_origin_fraction is None:
                origin = None
                if relative.painted_origin is not None:
                    layout, row = relative.painted_origin
                    origin = rebase_history_row(layout, result, row)
                if origin is None:
                    origin = rebase_history_row(viewport.last_layout, result, relative.origin)
                if origin is not None:
                    relative.origin = origin
            else:
                relative.origin = bottom * relative.native_origin_fraction
            self._finish_relative_history_scroll(relative, bottom, height)
            return True
        if preserve_painted:
            row = rebase_history_row(painted_layout, result, painted_row)
            row = min(bottom, painted_row if row is None else row)
            self._commit_history_scroll(row, row)
            return True
        return False

    def _land_history_scroll(self, row: float, following: bool) -> None:
        # The request uses measured history rows, while max_scroll_y still
        # belongs to the painted layout. The next layout clamps and checks it.
        self._history_scroll.check_bottom_after_layout = True
        self._anchor_released = not following
        row = self.max_scroll_y if following else max(0.0, row)
        self._commit_history_scroll(row, row)

    def _history_window_failed(self) -> None:
        self._history_native_pending = False
        self._history_scroll.restoring_tail = False
        self._history_scroll.startup = None
        self._cancel_history_jump()
        self._cancel_history_request()
        self.set_scroll_target_y_reactive(self.scroll_y)

    def _retry_failed_history_layout(self) -> None:
        if self._history_scroll.allow_mount_retry() or self._history_native_failed:
            self._history_native_failed = False
            self.refresh(layout=True)

    def _painted_row_in_history_index(self) -> float:
        scroll = self._history_scroll
        viewport = self._history_viewport
        return (
            scroll.visible_row if viewport is None else viewport.row_in_index(scroll.visible_layout, scroll.visible_row)
        )

    def _cancel_history_request(self, *, keep_relative: bool = False) -> None:
        scroll = self._history_scroll
        had_request = scroll.has_request
        held = scroll.pending is not None
        recover = (held or scroll.relative is not None) and not keep_relative and not self._arranging_history
        scroll.cancel(keep_relative=keep_relative)
        if not had_request and not recover:
            return
        # A correction pass can update the page index before its layout is
        # shown. Keep the painted anchor on cancellation.
        if recover and self.scroll_y == scroll.visible_row:
            scroll.check_bottom_after_layout |= scroll.visible_row == self.max_scroll_y
            row = self._painted_row_in_history_index()
            self._commit_history_scroll(row, row)
        elif held or recover:
            self.set_scroll_target_y_reactive(self.scroll_y)
        self.vertical_scrollbar.position = self.scroll_y
        self.call_after_refresh(self._sync_scroll_to_bottom_button)
        if recover and self.can_mount_replay():
            # Returning to a held row can be a no-op scroll after a resize.
            self.refresh(layout=True)

    def _cancel_history_jump(self) -> None:
        if self._history_jump is not None:
            self._history_jump.remove_class("-highlighted")
            self._history_jump = None
            self._history_highlight = None

    def _arrange_history_entries(self, size: Size, entries: list[Widget]) -> DockArrangeResult:
        self._history_layout_entries = entries
        try:
            return arrange(self, entries, size, self.screen.size)
        finally:
            self._history_layout_entries = None

    def _set_history_window(self, window: tuple[int, int] | None) -> None:
        if self._history_window != window:
            self._history_window = window
            self._clear_arrangement_cache()

    def _drop_history_viewport(self, *, after_native_layout: bool = False) -> None:
        self._cancel_history_request()
        if self._history_jump is not None and self._history_jump.parent is not self:
            self._cancel_history_jump()
        self._set_history_window(None)
        self._history_viewport = None
        self._history_selection_window = None
        self._history_scroll.visible_layout = None
        self._history_scroll.allow_mount_retry()
        self._history_scroll.check_bottom_after_layout = False
        self._history_native_pending = False
        self._history_native_failed = False
        self._history_scroll.restoring_tail = False
        if after_native_layout and isinstance(self._history_scroll.startup, HistoryRestoreTurn):
            self._queue_restored_scroll_after_layout(self._replay_generation)
        else:
            self._history_scroll.startup = None
        if not after_native_layout:
            self._history_drag_pointer = None
            self._history_drag_rebase = False
            self._history_drag_geometry = None

    def _apply_history_scroll_offset(self, delta: float, *, maximum: int | None = None) -> None:
        if not delta:
            return
        # A measurement can move the first content row without moving the
        # Home destination or the scrollbar's upper edge.
        row = 0.0 if maximum is not None and self.scroll_y == 0 else max(0.0, self.scroll_y + delta)
        target = row
        animator = self.app.animator
        animation = animator._animations.get((id(self), "scroll_y"))
        if maximum is not None and isinstance(animation, SimpleAnimation):
            # Rebase Textual's active scroll without losing its deadline or
            # completion callback. Home and End still target the two edges.
            previous_target = self.scroll_target_y
            if previous_target == 0:
                target = 0.0
            elif previous_target == self.max_scroll_y:
                target = float(maximum)
            else:
                target = min(maximum, max(0.0, previous_target + delta))
            now = animator._get_time()
            animation.duration = max(0.0, animation.start_time + animation.duration - now)
            animation.start_time = now
            animation.start_value = row
            animation.end_value = animation.final_value = target
        else:
            self._cancel_history_scroll_animation()
        self._commit_history_scroll(row, target)

    def _commit_history_scroll(self, row: float, target: float) -> None:
        changed = row != self.scroll_y
        self.set_scroll_y_reactive(row)
        self.set_scroll_target_y_reactive(target)
        self.set_vertical_scrollbar_position(row)
        if changed:
            self._history_drag_rebase = self._history_drag_pointer is not None
            self.call_after_refresh(self._sync_scroll_to_bottom_button)
            self._schedule_selection_reprojection(preserve_pending_quote=True)

    def _rebase_history_drag(self) -> None:
        self._history_drag_rebase = False
        pointer = self._history_drag_pointer
        if pointer is None:
            return
        scrollbar = self.vertical_scrollbar
        if scrollbar.grabbed is None or not scrollbar.window_size:
            return
        # Textual's next absolute drag target uses the current extent but the
        # original grab position. Keep the last pointer row fixed as we learn
        # more page heights and the extent changes under the thumb.
        scale = scrollbar.window_virtual_size / scrollbar.window_size
        pending = self._history_scroll.pending
        row = self.scroll_y if pending is None else pending.row
        position = min(self.max_scroll_y, max(0, row))
        scrollbar.grabbed_position = position - pointer * scale
        if pending is not None:
            scrollbar.position = position

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

    def _cancel_history_scroll_animation(self) -> None:
        """Retire the active scroll without publishing its endpoint during layout."""
        animator = self.app.animator
        if (animation := animator._animations.pop((id(self), "scroll_y"), None)) is not None:
            self.set_scroll_target_y_reactive(self.scroll_y)
            # Textual queues this callback to release its realtime animation claim.
            # Its animator timer signals idle on the next tick.
            if animation.on_complete is not None:
                animation.on_complete()

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
        self._replay_generation += 1
        self._replay_mount.request_stop()
        async with self._replay_lock:
            await self._reset_transcript()

    async def _reset_transcript(self) -> None:
        self._clear_rollback_preview()
        self._drop_history_viewport()
        self._history_jump = None
        self._history_selection = False
        self._history_selection_needs_layout = False
        self._history_highlight = None
        self._history_scroll.visible_row = 0.0
        # Replay ends at a batch boundary first: never remove children
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
        self._transcript_seqs.clear()
        self._transcript_occurrences.clear()
        self._current_tool_group = None
        self._agent_running = False
        self._live_renderer.reset_for_clear()
        self._tool_registry.reset_for_clear()
        self._toc.clear()
        self._rollback_targets = {0}

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
        """Toggle process sections, tool groups, thinking, text between tool calls and compaction summaries.

        If any eligible visible item is expanded, collapse all; otherwise
        expand all, including controls inside process sections. Return the
        new collapsed state, or False if there is nothing eligible. Empty
        summaries are ignored; pending replay entries take the same choice.
        """
        folds: list[ToolGroup | StreamedMarkdownMessage | ContextFoldWidget] = [
            group for group in self.query(ToolGroup) if not group.collapse_locked
        ]
        # Thinking that still streams keeps the state it has; an answer never folds here.
        folds.extend(message for message in self.query(StreamedMarkdownMessage) if message.foldable)
        folds.extend(marker for marker in self.query(ContextFoldWidget) if marker.has_summary())
        processes_collapsed = self._process_controller.folds_collapsed()
        # Entries still waiting to be prepended take the choice as they mount.
        pending_collapsed = self._replay_mount.pending_folds_collapsed()

        if not folds and pending_collapsed is None and processes_collapsed is None:
            return False
        viewport = self._history_viewport
        if viewport is not None:
            viewport.invalidate_measurements(complete=False)

        any_expanded = (
            processes_collapsed is False
            or any(
                (isinstance(fold, ContextFoldWidget) or not fold.process_hidden) and not fold.collapsed
                for fold in folds
            )
            or pending_collapsed is False
        )
        with self.app.batch_update():
            self._process_controller.set_fold_state(any_expanded)
            for fold in folds:
                fold.collapsed = any_expanded
            self._replay_mount.set_fold_state(any_expanded)
        self._sync_waiting_row()
        return any_expanded

    def get_agent_responses(self) -> list[tuple[str, str]]:
        """Return (profile_name, raw_text) of all agent messages in document order."""
        return self._transcript_reader.get_agent_responses()

    def get_user_messages(self) -> list[tuple[str, str]]:
        """Return (role_label, raw_text) of all user messages in document order."""
        return self._transcript_reader.get_user_messages()

    def get_all_messages(self) -> list[tuple[str, str]]:
        """Return (role_label, raw_text) of all user and agent messages in document order."""
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
        item_id: str | None = None,
        client_message_id: str | None = None,
    ) -> None:
        """Display a user message."""
        process = self._process_controller
        async with self._turn_completion_lock if is_injection else nullcontext():
            with self.app.batch_update():
                if not is_injection:
                    await process.end_live()
                await self._live_renderer.add_user_message(
                    text,
                    is_injection=is_injection,
                    created_at=created_at,
                    contents=contents,
                    item_id=item_id,
                    client_message_id=client_message_id,
                )
                if is_injection and not self.agent_running:
                    # A detached delivery can finish after the turn's completion event.
                    await process.complete_live(late_injection=True)

    async def apply_fresh_turn_cleanup(self, *, retired_previous_turn: bool, removed_item_ids: tuple[str, ...]) -> None:
        """Apply backend cleanup after replay so queued entries cannot reappear."""
        viewport = self._history_viewport
        if self._replay_mount.has_pending and (
            not self._replay_mount.windowed or viewport is None or not viewport.tail_is_attached_to(self)
        ):
            await self.wait_replay_complete()
        with self.app.batch_update():
            await self._live_renderer.apply_fresh_turn_cleanup(
                retired_previous_turn=retired_previous_turn, removed_item_ids=removed_item_ids
            )

    def apply_turn_started(self, turn_number: int, *, opening_item_id: str | None = None) -> None:
        """Set the live opener's authoritative turn number and history item."""
        self._live_renderer.apply_turn_started(turn_number, opening_item_id=opening_item_id)

    def apply_retry_started(self, note_message_id: str | None, opening_item_id: str | None) -> None:
        """Bind a retry's guidance, named by its client id, to its history item."""
        self._live_renderer.apply_retry_started(note_message_id, opening_item_id)

    async def add_answer_delta(self, segment_id: str, text: str) -> None:
        """Stream new answer text into its segment's message (one message per segment)."""
        current = self._current_agent_msg
        settles_intermediate = current is not None and segment_id not in current.segment_ids
        with self.app.batch_update() if settles_intermediate else nullcontext():
            await self._live_renderer.add_answer_delta(segment_id, text)

    def finish_answer_segment(self, segment_id: str, *, completed: bool) -> None:
        """Close a streamed answer segment; a failed or interrupted one keeps its partial text."""
        self._live_renderer.finish_answer_segment(segment_id, completed=completed)
        self._sync_waiting_row()

    async def add_thinking_delta(self, segment_id: str, channel: ContentChannel, text: str) -> None:
        """Stream new thinking text into its segment's message, when thinking is shown."""
        if self._live_renderer.starts_thinking(segment_id):
            with self.app.batch_update():
                if await self._live_renderer.add_thinking_delta(segment_id, channel, text):
                    self._sync_waiting_row()
        else:
            await self._live_renderer.add_thinking_delta(segment_id, channel, text)

    def finish_thinking_segment(self, segment_id: str) -> None:
        """Close a streamed thinking segment; a failed or interrupted one keeps its partial text."""
        self._live_renderer.finish_thinking_segment(segment_id)
        self._sync_waiting_row()

    async def abandon_thinking(self, segment_ids: tuple[str, ...]) -> None:
        """Remove the thinking a retry gave up with its response."""
        with self.app.batch_update():
            await self._live_renderer.abandon_thinking(segment_ids)
        self._sync_waiting_row()

    async def end_steered_pass(self) -> None:
        """End what a steer gave up while the turn goes on: the pass's running tool cards, and a retry wait it cut short.

        The steer cancelled those calls, and no result follows for them. As
        before a retry, the open tool group and answer close, so the next
        pass's output starts new ones.
        """
        await self._live_renderer.prepare_retry(steered=True)
        self._process_controller.seal_segment()
        if isinstance(status := self._find_trailing_status(), RetryMessage):
            await remove_shielded(status)
        self._sync_waiting_row()

    async def add_agent_message(
        self,
        text: str,
        *,
        structured_output_completed: bool = False,
        answer_segment_id: str | None = None,
        created_at: Any = None,
        turn_started_at: Any = None,
    ) -> None:
        """Complete the turn's answer, whose text already streamed as deltas; it took from *turn_started_at*."""
        async with self._turn_completion_lock:
            with self.app.batch_update():
                await self._live_renderer.add_agent_message(
                    text,
                    structured_output_completed=structured_output_completed,
                    answer_segment_id=answer_segment_id,
                    created_at=created_at,
                    turn_started_at=turn_started_at,
                )
                await self._end_live_process()
        self._sync_waiting_row()

    async def _remove_released_process_rows(self) -> None:
        try:
            await self._process_controller.remove_released_rows()
        except Exception:
            logger.exception("Failed to remove released live process rows")

    async def _end_live_process(self) -> None:
        with self.app.batch_update():
            await self._process_controller.end_live()
            await self._process_controller.complete_live(self._replay_mount.fold_choice)
        self.process_members_changed()
        self._sync_waiting_row()

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
        with self.app.batch_update():
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
            self._sync_waiting_row()

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

    async def add_interrupted(self, reason: MessageRef | str | None = None, source: str = "user") -> None:
        """Display an interruption notice, replacing any trailing error/interrupted widget."""
        async with self._turn_completion_lock:
            with self.app.batch_update():
                await self._live_renderer.add_interrupted(reason, source)
                await self._end_live_process()

    async def add_error(self, text: str, *, action_label: MessageRef | str | None = ERROR_RETRY_ACTION) -> None:
        """Display an error message, replacing any trailing error/interrupted widget."""
        async with self._turn_completion_lock:
            with self.app.batch_update():
                await self._live_renderer.add_error(text, action_label=action_label)
                await self._end_live_process()

    async def add_error_notice(self, text: str, *, action_label: MessageRef | str | None = None) -> None:
        """Display an error from outside a turn."""
        with self.app.batch_update():
            await self._live_renderer.show_error(text, action_label=action_label)

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

    def capture_settled_status(self) -> None:
        """Record the card a retry may dismiss, including that there is none.

        Called when the user starts the retry, before any new card can mount.
        A later ``TurnStarted`` must not re-read the transcript: an interrupt
        card may have replaced the old one by then. The weak reference holds no
        strong reference, so a replaced card dies with its widget.
        """
        status = self._find_trailing_status()
        self._retry_settled_status = (
            weakref_ref(status) if isinstance(status, (ErrorMessage, InterruptedMessage)) else None
        )
        self._retry_settled_status_captured = True

    async def clear_settled_status(self) -> None:
        """Remove exactly the card captured when the retry started, if still trailing.

        A retry attempt or an interrupt may mount a newer card while the retry's
        ``TurnStarted`` waits in the render gate. That card was not captured and
        must stay.
        """
        if not self._retry_settled_status_captured:
            return
        status = self._retry_settled_status
        self._retry_settled_status = None
        self._retry_settled_status_captured = False
        captured = status() if status is not None else None
        if not isinstance(captured, (ErrorMessage, InterruptedMessage)):
            return
        # A replayed stop may still be mounting its title. It must settle before Continue takes it over.
        async with self._turn_completion_lock:
            if captured.is_mounted and self._find_trailing_status() is captured:
                self._process_controller.continue_stopped_turn()
                with self.app.batch_update():
                    await remove_shielded(captured)
                    await self._process_controller.resume_stopped_title()
                self._sync_waiting_row()

    async def withdraw_answer(self, segment_ids: tuple[str, ...]) -> None:
        """Remove the answer text a Continue, Retry or steer dropped from history: the messages these segments showed.

        Tool cards stay, as they do for a retried attempt. Complete replay
        first if one of its pending entries might show a withdrawn answer.
        """
        withdrawn = frozenset(segment_ids)
        if self._replay_mount.has_pending and (
            not self._replay_mount.windowed
            or self._replay_mount.has_cold_entry(
                lambda widget: isinstance(widget, AgentMessage) and bool(widget.segment_ids & withdrawn)
            )
        ):
            await self.wait_replay_complete()
        with self.app.batch_update():
            self._live_renderer.withdraw_answer(withdrawn)
            self._clear_selection_touching(
                lambda node: (
                    isinstance(node, AgentMessage) and node.parent is self and bool(node.segment_ids & withdrawn)
                )
            )
            try:
                await self._process_controller.withdraw_live_answers(withdrawn)
            finally:
                removed = await remove_withdrawn_answer(self, withdrawn)
                await self._process_controller.forget_removed_members(removed)
        self._sync_waiting_row()

    def set_trailing_status_action_disabled(self, disabled: bool) -> None:
        """Keep the retry action visible but unavailable during admission."""
        last = self._find_trailing_status()
        if isinstance(last, (ErrorMessage, InterruptedMessage)):
            last.set_action_disabled(disabled)

    async def _remove_trailing_status(self, *, replacing: bool = False) -> None:
        """Remove the active status widget, allowing trailing retry notes.

        New output removes it as the turn it stopped goes on; a new status card only takes its place.
        """
        status = self._find_trailing_status()
        if status is None:
            return
        if not replacing and isinstance(status, (ErrorMessage, InterruptedMessage)):
            self._process_controller.continue_stopped_turn()
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
        file_snapshots: dict[FileSnapshotKey, list[FileSnapshotPayload]] | None = None,
        folds: list[FoldItem] | None = None,
        recovered_from_sidecar: bool = False,
        rollback_targets: set[int] | None = None,
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
        if rollback_targets is not None:
            self._rollback_targets = rollback_targets
        try:
            await self._replay_renderer.replay_history(
                messages,
                initial_profile=initial_profile,
                file_snapshots=file_snapshots,
                folds=folds,
            )
        finally:
            self._replay_process_protect_last = False

    def _clear_rollback_preview(self) -> None:
        self._rollback_preview_start = None
        for widget, (original, on_removed) in self._rollback_preview_borders.items():
            if widget._task is not None:
                widget._task.remove_done_callback(on_removed)
            widget.styles.set_rule("border_left", original)
            widget.refresh(layout=False)
        self._rollback_preview_borders.clear()

    def _forget_rollback_preview_widget(self, widget: Widget, _task: Task[None]) -> None:
        """Release a border record when Textual finishes unmounting its widget."""
        if widget is self._rollback_preview_start:
            self._clear_rollback_preview()
        elif (record := self._rollback_preview_borders.pop(widget, None)) is not None:
            widget.styles.set_rule("border_left", record[0])

    def on_message_rollback_button_preview_changed(self, event: MessageRollbackButton.PreviewChanged) -> None:
        event.stop()
        self._clear_rollback_preview()
        if not event.active or event.button.disabled:
            return
        message = next((node for node in event.button.ancestors if isinstance(node, UserMessage)), None)
        children = self.transcript_content_children()
        if message is None or message not in children or message.turn_number is None:
            return
        self._rollback_preview_start = message
        self._extend_rollback_preview(children[children.index(message) :])

    def _extend_rollback_preview(self, mounted: Sequence[Widget]) -> None:
        """Paint newly mounted entries in the active suffix after their styles are ready."""
        message = self._rollback_preview_start
        if message is None:
            return
        children = self.transcript_content_children()
        if message not in children:
            self._clear_rollback_preview()
            return
        affected = set(children[children.index(message) :])
        error = Color.parse(self.app.get_css_variables()["error"])
        for child in mounted:
            if child not in affected:
                continue
            for widget in child.walk_children(Widget, with_self=True):
                if widget in self._rollback_preview_borders:
                    continue
                border, _color = widget.styles.border_left
                if border in {"", "none", "hidden"}:
                    continue
                task = widget._task
                if task is None:
                    continue
                # Keep border geometry and the prior inline override unchanged;
                # a pointer preview repaints only and never asks for layout/style propagation.
                on_removed = partial(self._forget_rollback_preview_widget, widget)
                self._rollback_preview_borders[widget] = (
                    widget.styles._inline_styles.get_rule("border_left", None),
                    on_removed,
                )
                task.add_done_callback(on_removed)
                widget.styles.set_rule("border_left", (border, error))
                widget.refresh(layout=False)

    def set_rollback_targets(self, targets: set[int]) -> None:
        """Update only message headers whose retained snapshot availability changed."""
        if targets == self._rollback_targets:
            return
        self._rollback_targets = targets
        for widget in [*self.mounted_user_messages(), *self.pending_transcript_widgets()]:
            if isinstance(widget, UserMessage) and widget.turn_number is not None:
                widget.set_rollback_available(UserMessage.undo_link_available(widget.turn_number, targets))

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
        if self._history_scroll.queue_startup_turn(turn_id):
            self._scroll_controller.cancel_pending_guide_fold()
            return
        self._retry_failed_history_layout()
        self._history_scroll.startup = None
        self._history_scroll.check_bottom_after_layout = False
        self._scroll_controller.cancel_pending_guide_fold()
        self._cancel_history_jump()
        self._cancel_history_request()
        viewport = self._history_viewport
        if self._replay_mount.windowed:
            cold = viewport.turn(turn_id) if viewport is not None else None
            if cold is not None and self._replay_mount.needs_mount((cold,)):
                self._seek_history_turn(viewport, cold)
                return
        elif self._replay_mount.jump_when_laid_out(turn_id):
            return
        for child in self.children:
            if isinstance(child, UserMessage) and child.id == turn_id:
                if not self._seek_history_turn(viewport, child):
                    self._land_turn(child)
                return

    def _seek_history_turn(self, viewport: HistoryViewport | None, child: UserMessage) -> bool:
        if viewport is None or viewport.includes(self._history_window, child):
            return False
        row = viewport.page_row(child)
        if row is None:
            return False
        self._history_scroll.restoring_tail = False
        self._highlight_turn(child)
        self._scroll_controller.note_toc_jump()
        self._history_jump = child
        self._anchor_released = True
        self.scroll_to(y=row, animate=False, immediate=True)
        self.refresh(layout=True)
        return True

    def _highlight_turn(self, child: UserMessage) -> None:
        if self._history_highlight is not None and self._history_highlight is not child:
            self._history_highlight.remove_class("-highlighted")
        self._history_highlight = child
        for message in self.query(UserMessage).filter(".-highlighted"):
            message.remove_class("-highlighted")
        child.add_class("-highlighted")
        if self._agent_running:
            self._auto_scroll_paused_by_user = True

    def _land_turn(self, child: UserMessage) -> None:
        self._history_scroll.restoring_tail = False
        self._highlight_turn(child)
        self.scroll_to_widget(child, animate=False, top=True, immediate=True)
        # The geometry read above can queue a settle re-pin.
        self._scroll_controller.note_toc_jump()
        self.set_screen_scroll_required_and_check_idle()
