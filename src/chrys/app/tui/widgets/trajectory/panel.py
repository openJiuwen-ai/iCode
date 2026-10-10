# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The trajectory dashboard widget: tabs, loading and live refresh, and the
presentation cache that feeds the text view the pages render into."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from enum import StrEnum
from functools import partial
from pathlib import Path
from threading import Event
from time import monotonic
from typing import TYPE_CHECKING, Any, ClassVar

from rich.style import Style
from rich.text import Text
from textual import events, on
from textual.cache import LRUCache
from textual.containers import Container, VerticalGroup
from textual.css.query import NoMatches
from textual.geometry import Size
from textual.message import Message
from textual.widgets import Tab, Tabs

from chrys.app.tui.binding_display import localized_binding
from chrys.app.tui.clipboard import copy_text_to_clipboards
from chrys.app.tui.copy_messages import COPIED_TITLE
from chrys.app.tui.support.file_manager import can_open_in_file_manager, open_in_file_manager
from chrys.app.tui.support.gc_freeze import (
    DetachedLruCache,
    GcFreezeBlockReason,
    detach_lru_cache,
    renew_lru_cache,
)
from chrys.app.tui.widgets.chat.session_json import SessionJsonPanel
from chrys.app.tui.widgets.hatch import hatch_text_style, hatched_text_line
from chrys.app.tui.widgets.loading import ChrysLoadingIndicator
from chrys.app.tui.widgets.trajectory.insights import has_diagnostic_content, insights_lines
from chrys.app.tui.widgets.trajectory.overview import overview_lines
from chrys.app.tui.widgets.trajectory.presentation import (
    NO_TURNS,
    PRECISION_SYMBOLS,
    TURN_LABEL,
    UNAVAILABLE,
    DashboardLook,
    RenderContext,
    ResponsiveTier,
    precision_label,
    precision_style,
    render_message,
)
from chrys.app.tui.widgets.trajectory.session_info import SessionStorage, collect_session_storage
from chrys.app.tui.widgets.trajectory.text_view import TrajectoryTextView
from chrys.app.tui.widgets.trajectory.timeline import dependency_graph_lines, timeline_lines
from chrys.foundation.config.settings import DEFAULT_TRAJECTORY_VERIFY_COMMANDS
from chrys.foundation.i18n import Localizer, MessageDef, MessageRef, msg
from chrys.foundation.platform.files import surrogate_safe_text
from chrys.foundation.util.session_ids import session_short_id
from chrys.service.analytics import (
    AnalysisAvailability,
    TrajectoryAnalysis,
    TrajectoryAnalyzer,
    TrajectoryScanCancelled,
    TurnAnalysis,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from textual.app import ComposeResult
    from textual.timer import Timer
    from textual.worker import Worker

    from chrys.app.tui.i18n import LocaleController

# The session folder's sizes live outside the events log, so the live refresh
# recollects them on this coarser clock instead of walking the tree every tick.
_STORAGE_REFRESH_INTERVAL_S = 5.0

_DASHBOARD_TITLE = msg("tui.trajectory.title", fallback="Trajectory")
_OVERVIEW_TAB = msg("tui.trajectory.tab.overview", fallback="Overview")
_TIMELINE_TAB = msg("tui.trajectory.tab.timeline", fallback="Timeline")
_INSIGHTS_TAB = msg("tui.trajectory.tab.insights", fallback="Insights")
_SESSION_DATA_TAB = msg("tui.trajectory.tab.session_data", fallback="Session data")
_TOGGLE_GRAPH_BINDING = msg("tui.binding.toggle_graph", fallback="Dependency graph")
_READ_ERROR = msg("tui.trajectory.read_error", fallback="Trajectory read error: {error}")
_NO_ACTIVE_SESSION = msg("tui.main.session_json.no_active_session", fallback="No active session.")
_SESSION_INFO_PATH_COPIED = msg("tui.trajectory.session_info.path_copied", fallback="Path copied")
_SESSION_INFO_OPEN_FAILED = msg(
    "tui.trajectory.session_info.open_failed",
    fallback="Could not open the session folder: {error}",
)
_SESSION_INFO_OPEN_UNAVAILABLE = msg(
    "tui.trajectory.session_info.open_unavailable",
    fallback="Opening the session folder is not available in the current environment.",
)

_TABS_HEIGHT = 2
# Blank cells the Timeline leaves at its right edge; see _right_gap.
_TIMELINE_RIGHT_GAP = 1


class DashboardTab(StrEnum):
    """The frozen four-tab dashboard information architecture."""

    OVERVIEW = "overview"
    TIMELINE = "timeline"
    INSIGHTS = "insights"
    SESSION_DATA = "session-data"


class TrajectoryDashboard(Container):
    """Own dashboard foreground and active-tab state as one atomic pair."""

    can_focus = True

    BINDINGS: ClassVar[list] = [
        localized_binding("space", "toggle_timeline_dependencies", _TOGGLE_GRAPH_BINDING, show=False),
    ]

    COMPONENT_CLASSES: ClassVar[set[str]] = {"trajectory-dashboard--muted-label", "hatch--pattern"}

    DEFAULT_CSS = """
    TrajectoryDashboard > .hatch--pattern {
        color: $hatch-color;
    }
    TrajectoryDashboard > .trajectory-dashboard--muted-label {
        color: $text-muted;
    }
    TrajectoryDashboard {
        height: 1fr;
        display: none;
        layer: overlay;
        layers: default loading;
        background: $background;
        border: round $tui-border-accent $border-opacity;
        border-title-align: left;
        border-title-color: $tui-border-title-accent;
        border-subtitle-align: right;
        border-subtitle-color: $tui-border-title-accent;
    }
    TrajectoryDashboard > Tabs {
        height: 2;
    }
    TrajectoryDashboard > #timeline-turn-tabs {
        display: none;
    }
    /* The loading state floats on its own layer below the tab strip, so the
       same indicator covers either the hidden text view or the Session Data
       viewer while that viewer's background load is still running. */
    TrajectoryDashboard > #trajectory-loading-state {
        display: none;
        layer: loading;
        margin-top: 2;
        width: 100%;
        height: 1fr;
        min-height: 1;
        align: center middle;
        background: $background;
    }
    TrajectoryDashboard > #trajectory-loading-state > ChrysLoadingIndicator {
        width: 12;
        height: 1;
        color: $primary;
    }
    TrajectoryDashboard > SessionJsonPanel {
        height: 1fr;
        border: none;
    }
    """

    class StateChanged(Message):
        """The foreground derivations need synchronization by MainScreen."""

    def __init__(
        self,
        *,
        locale_controller: LocaleController | None = None,
        verify_commands: str = DEFAULT_TRAJECTORY_VERIFY_COMMANDS,
    ) -> None:
        super().__init__()
        self._locale_controller = locale_controller
        self._verify_commands = verify_commands
        self.foreground = False
        self.active_tab = DashboardTab.OVERVIEW
        self._timeline_dependencies = False
        self._selected_turn_id: str | None = None
        self._session_id = ""
        self._path: Path | None = None
        self._session_json_loaded = False
        self._turn_tab_key: tuple[tuple[str, str], ...] = ()
        self._analyzer: TrajectoryAnalyzer | None = None
        self._analysis: TrajectoryAnalysis | None = None
        self._session_storage: SessionStorage | None = None
        self._can_open_session_folder = can_open_in_file_manager()
        self._storage_collected_at = -_STORAGE_REFRESH_INTERVAL_S
        self._load_generation = 0
        self._load_pending = False
        self._worker: Worker[Any] | None = None
        self._scan_cancel_event: Event | None = None
        self._live_timer: Timer | None = None
        self._shell_restore_foreground = False
        self._available_size = Size(0, 0)
        self._presentation_key: tuple[int, int, int] | None = None
        self._presentation_revision = 0
        self._render_identity: tuple[DashboardTab, str | None] | None = None
        # Each entry is a page's lines and the width the view scrolls over.
        self._presentation_cache: LRUCache[tuple[object, ...], tuple[tuple[Text, ...], int]] | DetachedLruCache = (
            LRUCache(maxsize=64)
        )
        self._update_border_labels()

    @property
    def chat_foreground(self) -> bool:
        return not self.foreground

    @property
    def session_json_visible(self) -> bool:
        return self.foreground and self.active_tab is DashboardTab.SESSION_DATA

    @property
    def responsive_tier(self) -> ResponsiveTier:
        return ResponsiveTier.for_width(self._available_width())

    def compose(self) -> ComposeResult:
        yield Tabs(
            Tab(Text(self._render_message(_OVERVIEW_TAB.bind())), id=DashboardTab.OVERVIEW),
            Tab(Text(self._render_message(_TIMELINE_TAB.bind())), id=DashboardTab.TIMELINE),
            Tab(Text(self._render_message(_INSIGHTS_TAB.bind())), id=DashboardTab.INSIGHTS),
            Tab(Text(self._render_message(_SESSION_DATA_TAB.bind())), id=DashboardTab.SESSION_DATA),
            active=self.active_tab,
            id="trajectory-tabs",
        )
        yield Tabs(id="timeline-turn-tabs")
        with VerticalGroup(id="trajectory-loading-state"):
            yield ChrysLoadingIndicator(id="trajectory-loading")
        yield TrajectoryTextView(
            on_resized=self._render_for_settled_view,
            open_session_folder=self.open_session_folder,
            copy_session_path=self.copy_session_path,
        )
        yield SessionJsonPanel(locale_controller=self._locale_controller)

    def on_mount(self) -> None:
        if self._locale_controller is not None:
            self._locale_controller.register_surface(self)
        self._live_timer = self.set_interval(0.5, self._refresh_live)
        self._live_timer.pause()
        self._sync_content_visibility()

    def on_unmount(self) -> None:
        if self._locale_controller is not None:
            self._locale_controller.unregister_surface(self)
        self._cancel_worker()
        analyzer = self._analyzer
        if analyzer is not None:
            analyzer.release()
        self._analyzer = None
        self._analysis = None

    def on_resize(self, event: events.Resize) -> None:
        """Invalidate presentation only; trajectory analysis never depends on size."""
        content_size = self.size
        if content_size == self._available_size:
            return
        self._available_size = content_size
        if self.foreground and not self.session_json_visible:
            self._render_active_view()

    def _render_for_settled_view(self) -> None:
        """Re-render after the text view's own size settles asynchronously."""
        if self.is_mounted and self.foreground and not self.session_json_visible:
            self._render_active_view()

    def notify_style_update(self) -> None:
        """Rebuild presentation lines that contain resolved theme colors."""
        super().notify_style_update()
        self._presentation_revision += 1
        self._update_border_labels()
        self._clear_presentation_cache()
        self._render_active_view()

    def refresh_localization(self) -> None:
        self._presentation_revision += 1
        self._update_border_labels()
        for tab, label in (
            (DashboardTab.OVERVIEW, _OVERVIEW_TAB),
            (DashboardTab.TIMELINE, _TIMELINE_TAB),
            (DashboardTab.INSIGHTS, _INSIGHTS_TAB),
            (DashboardTab.SESSION_DATA, _SESSION_DATA_TAB),
        ):
            self.query_one(f"#{tab}", Tab).label = Text(self._render_message(label.bind()))
        self._clear_presentation_cache()
        self._render_active_view()

    def show_session(self, session_id: str, path: Path | None) -> None:
        changed = session_id != self._session_id or path != self._path
        self.foreground = True
        self.display = True
        self._session_id = session_id
        self._path = path
        self._update_border_labels()
        if changed:
            self._session_json_loaded = False
        if changed or self._analysis is None:
            self._release_analysis()
            self._start_load()
        self._sync_live_timer()
        self._sync_content_visibility()
        self.query_one("#trajectory-tabs", Tabs).focus()

    def set_verify_commands(self, value: str) -> None:
        """Apply the live classification word list and rebuild the current projection."""
        if value == self._verify_commands:
            return
        self._verify_commands = value
        if self.foreground and self._path is not None:
            self._release_analysis()
            self._start_load()

    def hide_dashboard(self) -> None:
        self.foreground = False
        self.display = False
        self._sync_live_timer()
        self._sync_content_visibility()
        self._release_analysis()

    def select_turn(self, turn_id: str) -> None:
        turn = self._analysis.turn(turn_id) if self._analysis is not None else None
        self._selected_turn_id = turn.turn_id if turn is not None else turn_id
        self._timeline_dependencies = False
        self.active_tab = DashboardTab.TIMELINE
        self.query_one("#trajectory-tabs", Tabs).active = DashboardTab.TIMELINE
        self._update_border_labels()
        self._sync_live_timer()
        self._sync_content_visibility()
        self._render_active_view()
        self.post_message(self.StateChanged())

    def suspend_for_shell_mode(self) -> bool:
        self._shell_restore_foreground = self.foreground
        restore_json = self.session_json_visible
        self.foreground = False
        self.display = False
        self._sync_live_timer()
        self._release_analysis()
        return restore_json

    def finish_shell_mode(self) -> bool:
        restore = self._shell_restore_foreground
        self._shell_restore_foreground = False
        if restore:
            self.show_session(self._session_id, self._path)
        return restore

    def gc_freeze_block_reason(self) -> GcFreezeBlockReason | None:
        if self.foreground and not self.session_json_visible:
            return GcFreezeBlockReason.TRAJECTORY_DASHBOARD_VISIBLE
        return None

    def prepare_for_gc_freeze(self) -> None:
        if self.foreground and not self.session_json_visible:
            return
        self.query_one(TrajectoryTextView).detach_cache()
        self._presentation_cache = detach_lru_cache(self._presentation_cache)

    def after_gc_freeze(self) -> None:
        self.query_one(TrajectoryTextView).renew_cache()
        self._presentation_cache = renew_lru_cache(self._presentation_cache)

    def abort_gc_freeze(self) -> None:
        self.after_gc_freeze()

    @on(Tabs.TabActivated, "#trajectory-tabs")
    def _on_tab_activated(self, event: Tabs.TabActivated) -> None:
        if event.tab.id is None:
            return
        self.active_tab = DashboardTab(event.tab.id)
        self._update_border_labels()
        self._sync_live_timer()
        self._sync_content_visibility()
        self._render_active_view()
        if self.foreground:
            if self.session_json_visible:
                self.query_one(SessionJsonPanel).focus()
            else:
                self.query_one(TrajectoryTextView).focus()
        self.post_message(self.StateChanged())

    @on(Tabs.TabActivated, "#timeline-turn-tabs")
    def _on_turn_tab_activated(self, event: Tabs.TabActivated) -> None:
        event.stop()
        if event.tabs is not self._query_turn_tabs() or event.tab.id is None:
            return
        index = int(event.tab.id.removeprefix("turn-"))
        if index >= len(self._turn_tab_key):
            return
        turn_id = self._turn_tab_key[index][0]
        if turn_id == self._selected_turn_id:
            return
        self._selected_turn_id = turn_id
        self._render_active_view()

    def action_toggle_timeline_dependencies(self) -> None:
        """Flip the timeline tab between the time axis and the dependency graph."""
        if not self.foreground or self.active_tab is not DashboardTab.TIMELINE or self.session_json_visible:
            return
        self._timeline_dependencies = not self._timeline_dependencies
        self._render_active_view()

    @on(SessionJsonPanel.LoadStateChanged)
    def _on_session_json_load_state_changed(self, event: SessionJsonPanel.LoadStateChanged) -> None:
        event.stop()
        self._sync_content_visibility()

    def _sync_content_visibility(self) -> None:
        if not self.is_mounted:
            return
        session_json = self.query_one(SessionJsonPanel)
        text_view = self.query_one(TrajectoryTextView)
        show_json = self.session_json_visible
        if show_json:
            session_json.display = True
            if not self._session_json_loaded:
                if self._session_id:
                    session_json.load_session(self._session_id)
                else:
                    session_json.set_status(self._render_message(_NO_ACTIVE_SESSION.bind()))
                self._session_json_loaded = True
        else:
            session_json.hide_session_json()
            self._session_json_loaded = False
        # One indicator for both slow paths: the analysis scan behind the
        # hidden text view, and the Session Data viewer's background load
        # (which must stay displayed so its worker can commit).
        show_loading = self.foreground and (session_json.is_loading if show_json else self._load_pending)
        text_view.set_class(self.active_tab is DashboardTab.OVERVIEW, "-overview")
        text_view.set_class(self.active_tab is DashboardTab.TIMELINE, "-timeline")
        text_view.display = self.foreground and not show_json and not show_loading
        self.query_one("#trajectory-loading-state").display = show_loading
        turn_tabs = self._query_turn_tabs()
        if turn_tabs is not None:
            turn_tabs.display = self._turn_tabs_visible()

    def _turn_tabs_visible(self) -> bool:
        return (
            self.foreground
            and not self.session_json_visible
            and bool(self._turn_tab_key)
            and self.active_tab is DashboardTab.TIMELINE
        )

    def _sync_turn_tabs(self, analysis: TrajectoryAnalysis | None) -> None:
        turns = (
            analysis.turns if analysis is not None and analysis.availability is AnalysisAvailability.AVAILABLE else ()
        )
        key = tuple(
            (turn.turn_id, self._render_message(TURN_LABEL.bind(turn=turn.turn_number or "—"))) for turn in turns
        )
        if key != self._turn_tab_key:
            self._turn_tab_key = key
            # Rebuild by replacement: remove-then-mount runs atomically inside
            # one callback, and building at execution time keeps the active tab
            # aligned with whatever the selection is once the callback runs.
            self.call_next(self._replace_turn_tabs)
            return
        tabs = self._query_turn_tabs()
        if tabs is None:
            return
        active = self._active_turn_tab_id()
        if active is not None and tabs.active != active and tabs.query(f"#{active}"):
            tabs.active = active
        tabs.display = self._turn_tabs_visible()

    def _query_turn_tabs(self) -> Tabs | None:
        # A pending replacement leaves a gap between remove and mount; callers
        # skip the sync then because the mount applies the fresh state itself.
        try:
            return self.query_one("#timeline-turn-tabs", Tabs)
        except NoMatches:
            return None

    def _active_turn_tab_id(self) -> str | None:
        return next(
            (
                f"turn-{index}"
                for index, (turn_id, _) in enumerate(self._turn_tab_key)
                if turn_id == self._selected_turn_id
            ),
            None,
        )

    async def _replace_turn_tabs(self) -> None:
        old = self.query_one("#timeline-turn-tabs", Tabs)
        anchor = self.query_one("#trajectory-tabs", Tabs)
        replacement = Tabs(
            *(Tab(Text(label), id=f"turn-{index}") for index, (_, label) in enumerate(self._turn_tab_key)),
            active=self._active_turn_tab_id(),
            id="timeline-turn-tabs",
        )
        await old.remove()
        replacement.display = self._turn_tabs_visible()
        await self.mount(replacement, after=anchor)

    def _update_border_labels(self) -> None:
        self.border_title = Text(self._render_message(_DASHBOARD_TITLE.bind()))
        if not self._session_id:
            self.border_subtitle = Text("")
            return
        if self.active_tab is DashboardTab.SESSION_DATA and self._path is not None:
            self.border_subtitle = Text(surrogate_safe_text(str(self._path.parents[1] / "session.json")))
            return
        subtitle = self._precision_legend()
        subtitle.append(" · ")
        subtitle.append(session_short_id(self._session_id))
        self.border_subtitle = subtitle

    def _sync_live_timer(self) -> None:
        timer = self._live_timer
        if timer is None:
            return
        if self.foreground and self.active_tab in {DashboardTab.OVERVIEW, DashboardTab.TIMELINE}:
            timer.resume()
        else:
            timer.pause()

    def _start_load(self) -> None:
        self._cancel_worker()
        self._load_generation += 1
        generation = self._load_generation
        analyzer = TrajectoryAnalyzer(verify_commands=self._verify_commands)
        path = self._path
        cancel_event = Event()
        self._analyzer = analyzer
        self._scan_cancel_event = cancel_event
        self.query_one(TrajectoryTextView).clear_lines()
        # The text view stays hidden behind the loading indicator until the
        # first analysis lands; a long scan must not read as "no data".
        self._load_pending = True
        self._worker = self.run_worker(
            partial(self._load, generation, analyzer, path, cancel_event),
            group="trajectory-analysis",
            exclusive=True,
        )
        self._sync_turn_tabs(None)
        self._sync_content_visibility()

    async def _load(
        self,
        generation: int,
        analyzer: TrajectoryAnalyzer,
        path: Path | None,
        cancel_event: Event,
    ) -> None:
        storage: SessionStorage | None = None
        if path is None:
            analysis = None
        else:
            try:
                analysis, storage = await asyncio.to_thread(
                    partial(_load_with_storage, analyzer, path, cancel_event=cancel_event)
                )
            except TrajectoryScanCancelled:
                return
        if generation != self._load_generation or analyzer is not self._analyzer or not self.foreground:
            return
        self._analysis = analysis
        self._session_storage = storage
        if storage is not None:
            self._storage_collected_at = monotonic()
        self._load_pending = False
        self._presentation_revision += 1
        self._sync_content_visibility()
        self._render_active_view()
        # The display flip above lands in the next layout pass, so the text
        # view's region is still the placeholder's here; render again once the
        # view settles or the first frame keeps the stale measurements.
        self.call_after_refresh(self._render_for_settled_view)

    async def _refresh_live(self) -> None:
        analyzer = self._analyzer
        worker = self._worker
        if (
            not self.foreground
            or analyzer is None
            or self._analysis is None
            or (worker is not None and worker.is_running)
        ):
            return
        self._load_generation += 1
        generation = self._load_generation
        cancel_event = Event()
        self._scan_cancel_event = cancel_event
        self._worker = self.run_worker(
            partial(self._refresh, generation, analyzer, cancel_event),
            group="trajectory-analysis",
            exclusive=True,
        )

    async def _refresh(self, generation: int, analyzer: TrajectoryAnalyzer, cancel_event: Event) -> None:
        # Storage lives outside the events log, so a quiet log must not pin it
        # forever: recollect on a coarse clock even when the analysis is unchanged.
        collect_storage = monotonic() - self._storage_collected_at >= _STORAGE_REFRESH_INTERVAL_S
        try:
            analysis, storage = await asyncio.to_thread(
                partial(
                    _refresh_with_storage,
                    analyzer,
                    collect_storage=collect_storage,
                    cancel_event=cancel_event,
                )
            )
        except TrajectoryScanCancelled:
            return
        if generation != self._load_generation or analyzer is not self._analyzer or not self.foreground:
            return
        changed = analysis is not self._analysis
        if storage is not None:
            self._storage_collected_at = monotonic()
            if storage != self._session_storage:
                self._session_storage = storage
                changed = True
        if not changed:
            return
        self._analysis = analysis
        self._presentation_revision += 1
        self._render_active_view()

    def _cancel_worker(self) -> None:
        self._load_generation += 1
        self._load_pending = False
        cancel_event = self._scan_cancel_event
        if cancel_event is not None:
            cancel_event.set()
        self._scan_cancel_event = None
        worker = self._worker
        if worker is not None:
            worker.cancel()
        self._worker = None

    def _release_analysis(self) -> None:
        self._cancel_worker()
        analyzer = self._analyzer
        if analyzer is not None:
            analyzer.release()
        self._analyzer = None
        self._analysis = None
        self._session_storage = None
        self._storage_collected_at = -_STORAGE_REFRESH_INTERVAL_S
        self._presentation_key = None
        self._render_identity = None
        self._clear_presentation_cache()
        if self.is_mounted:
            self.query_one(TrajectoryTextView).release()

    def _clear_presentation_cache(self) -> None:
        if not isinstance(self._presentation_cache, DetachedLruCache):
            self._presentation_cache.clear()

    def _render_active_view(self) -> None:
        if not self.is_mounted or self.session_json_visible or self._load_pending:
            return
        analysis = self._analysis
        generation = analysis.generation if analysis is not None else -1
        width = self._available_width()
        height = self._available_height()
        self._presentation_key = (generation, width, height)
        turn = self._resolve_selected_turn(analysis) if self.active_tab is DashboardTab.TIMELINE else None
        selected = self._selected_turn_id if self.active_tab is DashboardTab.TIMELINE else None
        # The empty state and the hatch fill below short content are built for
        # the text view's own region, which settles a beat after this widget
        # resizes; the region (and the scrollbar state feeding the settle math)
        # must key the cache so a build for a transitional layout is missed --
        # not hit -- by the re-render that follows the settled view.
        text_view = self.query_one(TrajectoryTextView)
        region = text_view.scrollable_content_region
        cache_key = (
            self.active_tab,
            self._timeline_dependencies,
            selected,
            generation,
            width,
            height,
            region.width,
            region.height,
            text_view.show_vertical_scrollbar,
            text_view.show_horizontal_scrollbar,
            self._verify_commands,
            self._presentation_revision,
        )
        cache = None if isinstance(self._presentation_cache, DetachedLruCache) else self._presentation_cache
        if analysis is None or analysis.availability is not AnalysisAvailability.AVAILABLE or not analysis.turns:
            cache = None
        if not region.width or not region.height:
            cache = None
        cached = cache.get(cache_key) if cache is not None else None
        if cached is not None:
            cached_lines, scroll_width = cached
            self._commit_lines(list(cached_lines), scroll_width)
        else:
            lines, scroll_width = self._settled_lines(partial(self._active_lines, analysis, turn))
            if cache is not None:
                cache[cache_key] = (tuple(lines), scroll_width)
            self._commit_lines(lines, scroll_width)
        if self.active_tab is DashboardTab.TIMELINE:
            self._sync_turn_tabs(analysis)

    def _resolve_selected_turn(self, analysis: TrajectoryAnalysis | None) -> TurnAnalysis | None:
        """Settle which turn the Timeline shows before anything keys on it.

        A selection the analysis still has stays; otherwise (a first visit, or
        the turn is gone) the newest turn is picked. The pick is written back,
        so turns a live session adds later don't move the reader off it.
        """
        if analysis is None or analysis.availability is not AnalysisAvailability.AVAILABLE or not analysis.turns:
            return None
        turn = analysis.turn(self._selected_turn_id) if self._selected_turn_id is not None else None
        turn = turn or analysis.turns[-1]
        self._selected_turn_id = turn.turn_id
        return turn

    def _active_lines(
        self,
        analysis: TrajectoryAnalysis | None,
        turn: TurnAnalysis | None,
        context: RenderContext,
        height: int,
    ) -> list[Text]:
        """The active tab's lines for *context*; *height* only sizes the empty state."""
        if analysis is None or analysis.availability is AnalysisAvailability.UNAVAILABLE:
            return self._empty_state_lines(UNAVAILABLE, width=context.width, height=height)
        if analysis.availability is AnalysisAvailability.READ_ERROR:
            return [Text(self._render_message(_READ_ERROR.bind(error=analysis.read_error or "")))]
        if self.active_tab is DashboardTab.OVERVIEW:
            if not analysis.turns:
                return self._empty_state_lines(NO_TURNS, width=context.width, height=height)
            return overview_lines(
                self._look(),
                context,
                analysis,
                folder=self._displayed_folder(),
                storage=self._session_storage,
                can_open_folder=self._can_open_session_folder,
            )
        if self.active_tab is DashboardTab.INSIGHTS:
            # Diagnostics alone (a log of only corrupt lines) still make a page.
            if not analysis.turns and not has_diagnostic_content(analysis.diagnostics):
                return self._empty_state_lines(NO_TURNS, width=context.width, height=height)
            return insights_lines(self._look(), context, analysis)
        if self.active_tab is DashboardTab.TIMELINE:
            # The selection is resolved before rendering: no turn means the
            # session has none yet.
            if turn is None:
                return self._empty_state_lines(NO_TURNS, width=context.width, height=height)
            if self._timeline_dependencies:
                return dependency_graph_lines(self._look(), turn)
            return timeline_lines(self._look(), context, turn)
        return []

    def _settled_lines(self, render: Callable[[RenderContext, int], list[Text]]) -> tuple[list[Text], int]:
        """Build for the region the asynchronous scrollbars settle on.

        The scrollbars this commit provokes (or retires) resize the region one
        cell after the fact; building for the settled region keeps every right
        edge real instead of cropping it off. *render* takes the layout and the
        height the empty state fills. Returns the lines and the width the view
        scrolls over (see _scroll_width).
        """
        context = self._render_context()
        lines = render(context, self._content_height())
        text_view = self.query_one(TrajectoryTextView)
        region = text_view.scrollable_content_region
        if not region.width or not region.height:
            return lines, self._scroll_width(lines, settled_width=None)
        region_width = self._effective_region_width()
        available = self._available_width()
        base_width = region_width + (1 if text_view.show_vertical_scrollbar else 0)
        if available:
            base_width = min(base_width, available)
        base_height = region.height + (1 if text_view.show_horizontal_scrollbar else 0)
        overflows_y = len(lines) > base_height
        settled_width = base_width - (1 if overflows_y else 0)
        overflows_x = any(line.cell_len > settled_width for line in lines)
        settled_height = base_height - (1 if overflows_x else 0)
        page_width = max(1, settled_width - self._right_gap())
        if settled_width != region_width or settled_height != region.height:
            lines = render(replace(context, width=page_width), max(1, settled_height))
        lines = self._hatch_filled(lines, width=page_width, height=settled_height)
        return lines, self._scroll_width(lines, settled_width=settled_width)

    def _scroll_width(self, lines: list[Text], *, settled_width: int | None) -> int:
        """The width the view scrolls over: the widest line and the page's right gap.

        The gap counts toward the scrollable width instead of being left off
        the lines' end: Textual re-checks a scroll view's scrollbars only when
        its virtual size differs from its region, and a page one column short
        of the settled width equals the region beside a scrollbar that should
        retire, which would keep that scrollbar on screen. A page whose lines
        fit the settled width never scrolls for the gap alone: a line drawn at
        its own width (the Timeline's closing note, the dependency graph, an
        error) that leaves less than the gap takes those cells instead. This
        keeps the overflow the settle math predicts the overflow the view gets.
        """
        widest = max((line.cell_len for line in lines), default=0)
        width = widest + self._right_gap()
        if settled_width is not None and widest <= settled_width:
            return min(width, settled_width)
        return width

    def _hatch_filled(self, lines: list[Text], *, width: int, height: int) -> list[Text]:
        """Fill the viewport rows below short content with the hatch pattern."""
        if len(lines) >= height:
            return lines
        hatch_style = self._hatch_style()
        label_style = self._muted_label_style()
        return [
            *lines,
            *(
                hatched_text_line(width, hatch_style=hatch_style, label_style=label_style)
                for _ in range(height - len(lines))
            ),
        ]

    def _effective_region_width(self) -> int:
        """The text view's region width, bounded by the dashboard's own size.

        The view's region lags a display flip by one layout pass, so right
        after a load it can still report the width of a previous layout; the
        dashboard's content size is current and caps it.
        """
        width = self.query_one(TrajectoryTextView).scrollable_content_region.width
        available = self._available_width()
        return min(width, available) if width and available else width

    def _content_width(self) -> int:
        """The width pages lay out in; the dashboard's own before the view has a region."""
        width = self._effective_region_width() or self._available_width()
        return max(1, width - self._right_gap())

    def _right_gap(self) -> int:
        """Blank cells the active page leaves at its right edge.

        The Timeline's text view pads only its left side: Textual draws a
        scrollbar inside the padding, so a right padding would stand between
        the scrollbar and the border. The page leaves the gap instead, which
        keeps it beside the content and the scrollbar against the border.
        """
        return _TIMELINE_RIGHT_GAP if self.active_tab is DashboardTab.TIMELINE else 0

    def _content_height(self) -> int:
        """The height the empty state fills; the dashboard's own before the view has a region."""
        height = self.query_one(TrajectoryTextView).scrollable_content_region.height
        return max(1, height) if height else self._available_height()

    def _render_context(self) -> RenderContext:
        """The layout a page renders for before the scrollbars settle."""
        return RenderContext(width=self._content_width(), tier=self.responsive_tier)

    def _empty_state_lines(self, message: MessageDef, *, width: int, height: int) -> list[Text]:
        hatch_style = self._hatch_style()
        label_style = self._muted_label_style()
        label = self._render_message(message.bind())
        return [
            hatched_text_line(
                width, label if y == height // 2 else None, hatch_style=hatch_style, label_style=label_style
            )
            for y in range(height)
        ]

    def _commit_lines(self, lines: list[Text], scroll_width: int) -> None:
        # The identity deliberately excludes the analysis generation: a live
        # session bumps it on every appended event, and a refresh of the view
        # the user is already reading must keep their scroll position. Session
        # switches reset the identity through ``_release_analysis``.
        identity = (
            self.active_tab,
            self._selected_turn_id if self.active_tab is DashboardTab.TIMELINE else None,
        )
        reset_scroll = identity != self._render_identity
        self._render_identity = identity
        self.query_one(TrajectoryTextView).set_lines(lines, reset_scroll=reset_scroll, min_width=scroll_width)

    def _displayed_folder(self) -> Path | None:
        """The folder the session info section names: the session directory when
        the events log sits in the store layout, otherwise the log's own parent."""
        path = self._path
        if path is None:
            return None
        session_dir = _session_directory(path)
        return session_dir if session_dir is not None else path.parent

    def open_session_folder(self) -> None:
        """Reveal the displayed session folder in the desktop file manager."""
        folder = self._displayed_folder()
        if folder is None:
            return
        if not can_open_in_file_manager():
            self.notify(self._render_message(_SESSION_INFO_OPEN_UNAVAILABLE.bind()), severity="warning", markup=False)
            return
        try:
            open_in_file_manager(folder)
        except OSError as error:
            # The OS error text is data, not markup: "[Errno 2] ..." would
            # otherwise be parsed as a content tag.
            self.notify(
                self._render_message(_SESSION_INFO_OPEN_FAILED.bind(error=surrogate_safe_text(str(error)))),
                severity="error",
                markup=False,
            )

    def copy_session_path(self) -> None:
        """Copy the displayed session folder path to the available clipboards."""
        folder = self._displayed_folder()
        if folder is None:
            return
        copy_text_to_clipboards(self.app, str(folder))
        self.notify(
            self._render_message(_SESSION_INFO_PATH_COPIED.bind()),
            title=self._render_message(COPIED_TITLE.bind()),
            timeout=1,
            markup=False,
        )

    def _precision_legend(self) -> Text:
        # Only the glyphs carry semantic colour; the labels inherit the border
        # subtitle's own colour so they read like the session id beside them.
        look = self._look()
        legend = Text()
        for index, precision in enumerate(PRECISION_SYMBOLS):
            if index:
                legend.append("   ")
            legend.append(PRECISION_SYMBOLS[precision], precision_style(look, precision))
            legend.append(" ")
            legend.append(precision_label(look, precision))
        return legend

    def _hatch_style(self) -> Style:
        return hatch_text_style(self)

    def _muted_label_style(self) -> Style:
        # $text-muted is a composite value on every theme ("auto 60%",
        # "ansi_white 40%"), which a plain color parse silently drops; only
        # the stylesheet can resolve the alpha blend. Strip the stamped
        # background so the label stays transparent over the hatch.
        full = self.get_component_rich_style("trajectory-dashboard--muted-label")
        return full.without_color + Style.from_color(full.color)

    def _available_width(self) -> int:
        return self._available_size.width or self.size.width

    def _available_height(self) -> int:
        height = self._available_size.height or self.size.height
        return max(1, height - _TABS_HEIGHT)

    def _look(self) -> DashboardLook:
        return DashboardLook(
            console=self.app.console,
            theme_variables=self.app.theme_variables,
            localizer=self._localizer(),
        )

    def _localizer(self) -> Localizer | None:
        return None if self._locale_controller is None else self._locale_controller.localizer

    def _render_message(self, reference: MessageRef) -> str:
        # Takes only the localizer, never _look(): the constructor sets the
        # border title before any App runs.
        return render_message(self._localizer(), reference)


def _session_directory(path: Path) -> Path | None:
    """The session folder owning *path*, only when it sits in the store layout.

    Anything else (a loose events file under a scratch directory) has no
    session folder to size or open; walking its parent would measure an
    unrelated tree.
    """
    if (
        len(path.parents) < 3
        or path.name != "events.jsonl"
        or path.parent.name != "trajectory"
        or path.parents[2].name != "sessions"
    ):
        return None
    return path.parents[1]


def _load_with_storage(
    analyzer: TrajectoryAnalyzer,
    path: Path,
    *,
    cancel_event: Event,
) -> tuple[TrajectoryAnalysis, SessionStorage | None]:
    analysis = analyzer.load(path, cancel_event=cancel_event)
    session_dir = _session_directory(path)
    if session_dir is None:
        return analysis, None
    return analysis, collect_session_storage(session_dir, cancel_event=cancel_event)


def _refresh_with_storage(
    analyzer: TrajectoryAnalyzer,
    *,
    collect_storage: bool,
    cancel_event: Event,
) -> tuple[TrajectoryAnalysis, SessionStorage | None]:
    analysis = analyzer.refresh(cancel_event=cancel_event)
    if not collect_storage:
        # Storage recollection stays on the caller's coarse clock even while
        # the log is appending, or a busy session walks the directory tree on
        # every poll tick; the panel keeps the previous figures meanwhile.
        return analysis, None
    session_dir = _session_directory(analysis.path)
    if session_dir is None:
        return analysis, None
    return analysis, collect_session_storage(session_dir, cancel_event=cancel_event)
