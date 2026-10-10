# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Shared helpers for TUI tests."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from io import BytesIO
from types import SimpleNamespace
from typing import TYPE_CHECKING

from PIL import Image
from rich.console import Console
from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.errors import NoWidget
from textual.events import Click
from textual.message import Message
from textual.messages import Prune
from textual.widget import Widget
from textual.widgets import Static

from chrys.app.tui.i18n import LocaleController
from chrys.app.tui.screens.main.commands import SlashCommandActions
from chrys.app.tui.screens.main.diff_controller import LiveDiffTracker
from chrys.app.tui.screens.main.event_handlers import BackendEventCallbacks, BackendEventHandler
from chrys.app.tui.screens.main.live_diff import LiveFileMutation
from chrys.app.tui.screens.main.session_handlers import SessionCallbacks, SessionHandler
from chrys.app.tui.screens.main.state import MainScreenServices, MainScreenState
from chrys.app.tui.screens.main.suggestions import SuggestionCallbacks, SuggestionHandler
from chrys.app.tui.screens.main.view_adapter import MainScreenViewAdapter
from chrys.app.tui.support.gc_freeze import GcAbsorbRequested, GcReclaimRequested
from chrys.app.tui.widgets.chat.agent_transcript_surface import AgentTranscriptSurface
from chrys.app.tui.widgets.chat.panel import ChatPanel, _ChatBottomSpacer, _ScrollToBottomButton
from chrys.app.tui.widgets.chat.ports import TranscriptLocalizationPort
from chrys.app.tui.widgets.chat.renderers.sub_agent import SubAgentToolCall
from chrys.app.tui.widgets.chat.tool_call import BaseToolCard, ToolCardHeader, ToolGroup
from chrys.app.tui.widgets.chrome.file_scanner import ProjectPathScanResult, ProjectPathSuggestion
from chrys.app.tui.widgets.chrome.suggestion_list import SuggestionItem
from chrys.foundation.config.settings import Settings
from chrys.foundation.events.bus import EventBus
from chrys.foundation.i18n import MessageRef
from chrys.foundation.i18n.formatting import format_message
from chrys.foundation.models.approval_reuse import ReuseChoice
from chrys.service.approval.policy import ApprovalMode
from tests.support.waiting import wait_for

if TYPE_CHECKING:
    import pytest
    from rich.console import RenderableType
    from rich.segment import Segment
    from textual.pilot import Pilot
    from textual.screen import Screen


def install_trajectory_dashboard_query(screen: object) -> None:
    """Add the dashboard surface expected by the real main-screen view adapter."""
    original_query_one = getattr(screen, "query_one", None)
    if original_query_one is None:
        return
    dashboard = type("_TrajectoryDashboardFake", (), {"foreground": False})()

    def query_one(cls: type):
        if cls.__name__ == "TrajectoryDashboard":
            return dashboard
        return original_query_one(cls)

    screen.query_one = query_one


def discard_worker(_work: Callable[[], Awaitable[object]]) -> None:
    """A worker hook whose worker never starts, so its work is never called."""


def start_fake_worker(screen: object, work: Callable[[], Awaitable[object]]) -> object | None:
    """Start *work* the way a screen-fake's worker hook would.

    A fake with ``run_worker`` runs its coroutine there; one with a
    ``_started_workers`` list collects the coroutine for the test to drive;
    otherwise the work is dropped uncalled, as a worker the test does not
    care about.
    """
    run_worker = getattr(screen, "run_worker", None)
    if callable(run_worker):
        return run_worker(work(), thread=False)
    started = getattr(screen, "_started_workers", None)
    if isinstance(started, list):
        started.append(work())
        return None
    return None


def fake_session_title(**overrides: object) -> SimpleNamespace:
    """A ``SessionTitleController`` stand-in for screen fakes.

    Every title update the view adapter forwards is a no-op unless the test
    overrides it by name (``set_terminal_title_for_cwd=recorder.append``).
    """

    def set_session_title_state(
        *, custom: str | None = None, generated: str | None = None, fallback: str | None = None
    ) -> None:
        return None

    def set_terminal_title_for_cwd(cwd: str | None = None) -> None:
        return None

    def set_terminal_title_for_user_message(text: str) -> None:
        return None

    title = SimpleNamespace(
        custom_title="",
        set_session_title_state=set_session_title_state,
        reset_session_title_state=lambda: None,
        set_terminal_title_for_cwd=set_terminal_title_for_cwd,
        set_terminal_title_for_user_message=set_terminal_title_for_user_message,
        clear_terminal_title_result=lambda: None,
        mark_terminal_title_completed=lambda: None,
        mark_terminal_title_failed=lambda: None,
    )
    for name, value in overrides.items():
        if not hasattr(title, name):
            raise AttributeError(f"SessionTitleController has no {name!r}")
        setattr(title, name, value)
    return title


# MainScreen keeps its run, runtime, session, usage, workspace, shell, overlay
# and submit state only in ``_state`` (``MainScreenState``), its service handles
# only in ``_services`` (``MainScreenServices``), live-diff tracking only in
# ``_live_diff`` (``LiveDiffTracker``), and git-branch and session-title
# bookkeeping only in their controllers. These are the screen fields and
# properties that replaced, plus the seed names the old fakes used for them: a
# fake carrying one would pre-set nothing, so the fakes refuse them.
_REMOVED_MAIN_SCREEN_FIELDS = frozenset(
    {
        # Former MainScreen fields.
        "_active_model_profile_id",
        "_agent_loading",
        "_agent_registry",
        "_agent_running",
        "_approval_mode",
        "_bus",
        "_chdir_current_cwd",
        "_chdir_original_cwd",
        "_creating_new_session",
        "_fullscreen_terminal",
        "_git_branch_closed",
        "_git_branch_monitor",
        "_git_branch_pending_operation",
        "_git_branch_poll_timer",
        "_git_branch_refresh_timer",
        "_git_branch_retry_cwd_on_display_sync",
        "_git_branch_task",
        "_has_messages",
        "_interrupt_confirm_active",
        "_last_total_session_tokens",
        "_last_usage_tokens",
        "_live_call_paths",
        "_live_file_mutations",
        "_main_usage_source_id",
        "_model_registry",
        "_pending_active_switch",
        "_pending_user_submit_blocked",
        "_profile",
        "_profile_switch_from",
        "_profile_switch_seq",
        "_profile_switch_to",
        "_quit_after_flush_task",
        "_restoring_session",
        "_runtime_details",
        "_sb_saved",
        "_session_custom_title",
        "_session_fallback_title",
        "_session_generated_title",
        "_shell_mode",
        "_state_store",
        "_submit_state",
        "_terminal_title_activity_frame",
        "_terminal_title_activity_timer",
        "_terminal_title_content",
        "_terminal_title_result",
        "_terminal_title_source",
        "_workspace_git_branch",
        # Former MainScreen properties.
        "_deferred_agent_messages",
        "_engine",
        "_pending_user_message_render_active",
        "_pending_user_submit_active",
        "_pending_user_submit_text",
        # Seeds only the old fakes read.
        "_apply_saved_model_on_restore",
    }
)


def _removed_main_screen_field_error(names: Iterable[str]) -> TypeError:
    return TypeError(
        f"main-screen fake sets {', '.join(sorted(names))}, which MainScreen no longer has; "
        "pre-set _state (MainScreenState), _services (MainScreenServices) or _live_diff (LiveDiffTracker), "
        "or fake the controller that now owns it"
    )


def _main_screen_part[T](screen: object, name: str, kind: type[T], make: Callable[[], T]) -> T:
    """The fake's *name* part, made and attached as MainScreen makes it when the fake has none."""
    part = getattr(screen, name, None)
    if part is None:
        part = make()
        setattr(screen, name, part)
    elif not isinstance(part, kind):
        raise TypeError(f"main-screen fake's {name} is {type(part).__name__}, not {kind.__name__}")
    return part


def main_screen_parts(screen: object) -> tuple[MainScreenState, MainScreenServices, LiveDiffTracker]:
    """The ``_state``, ``_services`` and ``_live_diff`` a main-screen fake's handlers share.

    A fake that lacks one gets a fresh one attached, as a real MainScreen has
    all three, so a test reads what a handler wrote from the same place. A fake
    carrying a field these replaced fails instead of silently pre-setting
    nothing. Each call also routes the fake's ``query_one`` through
    :func:`install_trajectory_dashboard_query` and gives a fake without
    ``context_usage_state`` a None one, as the view adapter reads both.
    """
    removed = {
        name
        for name in _REMOVED_MAIN_SCREEN_FIELDS
        if name in getattr(screen, "__dict__", {}) or any(name in vars(cls) for cls in type(screen).__mro__)
    }
    if removed:
        raise _removed_main_screen_field_error(removed)
    install_trajectory_dashboard_query(screen)
    if not hasattr(screen, "context_usage_state"):
        screen.context_usage_state = None
    return (
        _main_screen_part(screen, "_state", MainScreenState, MainScreenState),
        _main_screen_part(screen, "_services", MainScreenServices, lambda: MainScreenServices(bus=EventBus())),
        _main_screen_part(screen, "_live_diff", LiveDiffTracker, LiveDiffTracker),
    )


def main_screen_state_at(cwd: str) -> MainScreenState:
    """A fresh main-screen state in workspace *cwd*, as ``MainScreen._set_workspace_cwd`` leaves it."""
    state = MainScreenState()
    state.workspace.current_cwd = cwd
    state.workspace_marker.current_cwd = cwd
    return state


def _call_screen_hook(screen: object, name: str, *args: object) -> object | None:
    """Call the fake's *name* method when it has one; the result, else None."""
    hook = getattr(screen, name, None)
    return hook(*args) if callable(hook) else None


class ScreenSetters:
    """The setter callbacks MainScreen hands its controllers, over a fake's parts.

    Each writes the field MainScreen's own setter writes, then calls the fake's
    same-name method (``_set_restoring_session``, …) when it has one, so a test
    can record the call. Anything else a MainScreen setter does (the run
    generation bump, controller calls, widget updates) is left to that method.
    """

    def __init__(self, screen: object, state: MainScreenState, services: MainScreenServices) -> None:
        self._screen = screen
        self._state = state
        self._services = services

    def set_agent_running(self, value: bool) -> None:
        self._state.run.agent_running = value
        _call_screen_hook(self._screen, "_set_agent_running", value)

    def set_agent_loading(self, value: bool) -> None:
        self._state.run.agent_loading = value
        _call_screen_hook(self._screen, "_set_agent_loading", value)

    def set_has_messages(self, value: bool) -> None:
        self._state.run.has_messages = value
        _call_screen_hook(self._screen, "_set_has_messages", value)

    def set_profile_display(self, value: str) -> None:
        self._state.runtime.profile = value
        _call_screen_hook(self._screen, "_set_profile_display", value)

    def set_active_model_profile_id(self, value: str) -> None:
        self._services.active_model_profile_id = value
        _call_screen_hook(self._screen, "_set_active_model_profile_id", value)

    def set_creating_new_session(self, value: bool) -> None:
        self._state.session.creating_new_session = value
        _call_screen_hook(self._screen, "_set_creating_new_session", value)

    def set_restoring_session(self, value: bool) -> None:
        self._state.session.restoring_session = value
        _call_screen_hook(self._screen, "_set_restoring_session", value)

    def set_workspace_cwd(self, value: str) -> None:
        self._state.workspace.current_cwd = value
        self._state.workspace_marker.current_cwd = value
        _call_screen_hook(self._screen, "_set_workspace_cwd", value)


def make_backend_handler(
    screen: object,
    *,
    locale_controller: LocaleController | None = None,
    approval_defer_while_judging: Callable[[], bool] = lambda: False,
) -> BackendEventHandler:
    """Construct a backend event handler around a lightweight screen fake.

    The handler shares the fake's ``_state``, ``_services`` and ``_live_diff``
    (see :func:`main_screen_parts`). A setter method the fake also defines
    (``_set_agent_running``, ``_set_restoring_session``, …) runs after the
    state is updated, as MainScreen's own setter would. Approval requests the
    judge reviews show at once unless *approval_defer_while_judging* says
    otherwise.
    """
    state, services, live_diff = main_screen_parts(screen)
    setters = ScreenSetters(screen, state, services)

    def on_session_fork_error(event: object, message: str, severity: str) -> None:
        sessions = getattr(screen, "_sessions", None)
        if sessions is not None:
            sessions.on_session_fork_error(event, message=message, severity=severity)

    def on_session_clear_error(event: object, message: str) -> None:
        sessions = getattr(screen, "_sessions", None)
        if sessions is not None:
            sessions.on_session_clear_error(event, message=message)

    def handle_approval_response(
        request_id: str,
        approved: bool,
        reason: str,
        modified_args: dict[str, object] | None,
        remember_choice: ReuseChoice,
    ) -> object | None:
        return _call_screen_hook(
            screen, "_handle_approval_response", request_id, approved, reason, modified_args, remember_choice
        )

    def post_gc_message(message: object) -> None:
        messages = getattr(screen, "_gc_messages", None)
        if isinstance(messages, list):
            messages.append(message)

    return BackendEventHandler(
        state=state,
        services=services,
        locale_controller=locale_controller,
        view=MainScreenViewAdapter(
            screen,  # type: ignore[arg-type]
            state=state,
            state_store=services.state_store,
            locale_controller=locale_controller,
        ),
        callbacks=BackendEventCallbacks(
            set_agent_running=setters.set_agent_running,
            set_agent_loading=setters.set_agent_loading,
            set_has_messages=setters.set_has_messages,
            set_profile_display=setters.set_profile_display,
            set_active_model_profile_id=setters.set_active_model_profile_id,
            set_creating_new_session=setters.set_creating_new_session,
            set_restoring_session=setters.set_restoring_session,
            set_workspace_cwd=setters.set_workspace_cwd,
            refresh_git_branch=lambda: None,
            update_subtitle=lambda: _call_screen_hook(screen, "_update_subtitle"),
            update_toc=lambda: _call_screen_hook(screen, "_update_toc"),
            on_session_fork_error=on_session_fork_error,
            on_session_clear_error=on_session_clear_error,
            handle_approval_response=handle_approval_response,
            handle_ask_user_response=lambda request_id, answers: _call_screen_hook(
                screen, "_handle_ask_user_response", request_id, answers
            ),
            question_inline_preferred=lambda: False,
            approval_defer_while_judging=approval_defer_while_judging,
            approval_timeout_seconds=lambda: 0,
            post_gc_message=post_gc_message,
            debug=lambda key, message="": _call_screen_hook(screen, "_debug", key, message),
            refresh_model_indicator=lambda: None,
            refresh_notification_settings=lambda: _call_screen_hook(screen, "_refresh_notification_settings"),
            refresh_trajectory_verify_commands=lambda: _call_screen_hook(screen, "_refresh_trajectory_verify_commands"),
            settings_reloaded=lambda: None,
        ),
        live_diff=live_diff,
    )


def make_session_handler(
    screen: object,
    *,
    locale_controller: LocaleController | None = None,
) -> SessionHandler:
    """Construct a session handler around a lightweight screen fake.

    As with :func:`make_backend_handler`, the handler shares the fake's
    ``_state`` and ``_services`` and calls the fake's setter methods.
    """
    state, services, _live_diff = main_screen_parts(screen)
    setters = ScreenSetters(screen, state, services)

    def clear_suggestion_file_cache() -> None:
        suggestions = getattr(screen, "_suggestions", None)
        if suggestions is not None:
            suggestions.file_cache = None

    def post_gc_message(message: object) -> None:
        messages = getattr(screen, "_gc_messages", None)
        if isinstance(messages, list):
            messages.append(message)

    class _AgentLoadPort:
        async def begin_session_restore_load(self, session_id: str) -> None:
            events = getattr(screen, "_events", None)
            handler = getattr(events, "begin_session_restore_load", None)
            if callable(handler):
                result = handler(session_id)
                if inspect.isawaitable(result):
                    await result

        def cancel_agent_load(self) -> None:
            events = getattr(screen, "_events", None)
            handler = getattr(events, "cancel_agent_load", None)
            if callable(handler):
                handler()

        def finish_agent_load(self, message: str = "") -> None:
            events = getattr(screen, "_events", None)
            handler = getattr(events, "finish_agent_load", None)
            if callable(handler):
                handler(message)

    return SessionHandler(
        state=state,
        services=services,
        view=MainScreenViewAdapter(screen, state=state, state_store=services.state_store),  # type: ignore[arg-type]
        callbacks=SessionCallbacks(
            set_agent_loading=setters.set_agent_loading,
            set_has_messages=setters.set_has_messages,
            set_creating_new_session=setters.set_creating_new_session,
            set_restoring_session=setters.set_restoring_session,
            set_profile_display=setters.set_profile_display,
            set_active_model_profile_id=setters.set_active_model_profile_id,
            set_workspace_cwd=setters.set_workspace_cwd,
            update_subtitle=lambda: _call_screen_hook(screen, "_update_subtitle"),
            update_toc=lambda: _call_screen_hook(screen, "_update_toc"),
            clear_suggestion_file_cache=clear_suggestion_file_cache,
            start_worker=lambda work: start_fake_worker(screen, work),
            post_gc_message=post_gc_message,
            debug=lambda key, message="": _call_screen_hook(screen, "_debug", key, message),
            refresh_model_indicator=lambda: None,
        ),
        agent_load=_AgentLoadPort(),
        locale_controller=locale_controller,
    )


def status_text(value: MessageRef | str) -> str:
    """Render a status ``MessageRef`` (or plain string) the way the UI would."""
    return value if isinstance(value, str) else format_message(value)


def status_trail(value: MessageRef | str | tuple[MessageRef | str, ...]) -> str:
    """Render a status trail — a ``MessageRef``/string or a tuple of them."""
    if isinstance(value, tuple):
        return " · ".join(status_text(part) for part in value)
    return status_text(value)


def stale_file_cache(*paths: str) -> list[ProjectPathSuggestion]:
    """Build a suggestion file cache standing in for a prior ``@`` scan."""
    return [ProjectPathSuggestion(path=path, kind="file") for path in paths]


def make_live_mutation(
    before_text: str,
    after_text: str,
    operation: str,
    *,
    bytes_changed: bool = True,
    before_hash: str | None = None,
    after_hash: str | None = None,
    source: str = "",
) -> LiveFileMutation:
    return LiveFileMutation(
        before_text=before_text,
        after_text=after_text,
        operation=operation,
        bytes_changed=bytes_changed,
        before_hash=before_hash,
        after_hash=after_hash,
        source=source,
    )


class BusyWidget(Static):
    """A widget that stays inside a message handler from ``hold()`` until ``release`` is set.

    Mounted inside a widget that a rebuild removes, it keeps that removal waiting: a removed widget
    waits for its children to exit before it detaches. Waits while it holds must not go through
    Pilot, which waits for every message loop to go idle.
    """

    class Hold(Message):
        pass

    def __init__(self) -> None:
        super().__init__("busy")
        self.holding = False
        self.exit_requested = False
        """Set once a removed parent asks this widget to exit; that parent then waits for it."""
        self.release = asyncio.Event()

    def hold(self) -> None:
        self.post_message(self.Hold())

    def post_message(self, message: Message) -> bool:
        if isinstance(message, Prune):
            self.exit_requested = True
        return super().post_message(message)

    @on(Hold)
    async def _hold(self, _: Hold) -> None:
        self.holding = True
        await self.release.wait()


async def interrupt_removal(busy: BusyWidget, begin: Callable[[], object], interrupt: Callable[[], object]) -> None:
    """Hold *busy*, let *begin* start a removal that waits for it, and call *interrupt* while it waits."""
    busy.hold()
    try:
        await wait_for(lambda: busy.holding, description="the busy widget holds its message loop")
        begin()
        await wait_for(lambda: busy.exit_requested, description="a removal waits for the busy widget")
        interrupt()
    finally:
        busy.release.set()


async def assert_app_handles_messages(app: App) -> None:
    """Fail unless the App's message loop still runs."""
    handled = asyncio.Event()
    assert app.call_later(handled.set)
    await wait_for(handled.is_set, description="the App's message loop handles a new message")


class FakeNotificationService:
    """Mock ``App.notification_service`` that records delivered events."""

    def __init__(self) -> None:
        self.events: list[object] = []

    def notify(self, event: object) -> bool:
        self.events.append(event)
        return True


def rich_segment_lines(renderable: RenderableType, *, width: int = 200) -> list[list[Segment]]:
    """Render a Rich renderable (for example a ``Static.content``) at *width*.

    Layout independent: the widget need not be mounted or sized, so live and
    replayed cards can be compared on identical footing.
    """
    console = Console(width=width, force_terminal=False, legacy_windows=False)
    return console.render_lines(renderable, console.options.update_width(width), pad=False)


def rich_plain(renderable: RenderableType, *, width: int = 200) -> str:
    """Plain text of :func:`rich_segment_lines`, each line right-trimmed."""
    return "\n".join(
        "".join(segment.text for segment in line).rstrip() for line in rich_segment_lines(renderable, width=width)
    )


# ---------------------------------------------------------------------------
# Widget harnesses — minimal apps that mount one widget under test
# ---------------------------------------------------------------------------


class LocalizedApp(App):
    """Bare app carrying an English locale controller for widget tests."""

    locale_controller = LocaleController(Settings(locale="en"))


class WidgetApp(App):
    """Mount whatever *factory* returns as the whole app.

    Replaces the one-off ``class _App(App): def compose(self): yield X(...)``
    harness that every widget test used to declare inline; the factory runs at
    compose time, so it may close over widgets built earlier in the test.
    """

    def __init__(self, factory: Callable[[], Widget | Iterable[Widget]]) -> None:
        self._factory = factory
        super().__init__()

    def compose(self) -> ComposeResult:
        produced = self._factory()
        if isinstance(produced, Widget):
            yield produced
        else:
            yield from produced


class LocalizedWidgetApp(WidgetApp):
    """:class:`WidgetApp` with the English locale controller of :class:`LocalizedApp`."""

    locale_controller = LocaleController(Settings(locale="en"))


class PlaceholderHostApp(App):
    """A bare English-locale host app for a screen or dialog pushed onto it.

    Its own composed widget is an inert placeholder: the subject under test is
    whatever the caller pushes afterwards. Unlike :class:`LocalizedWidgetApp`,
    each instance builds its own :class:`LocaleController` rather than sharing
    one at class level, so a test that leaves a locale-aware surface registered
    cannot reach into the next test's app through a shared controller.
    """

    def __init__(self) -> None:
        self.locale_controller = LocaleController(Settings(locale="en"))
        super().__init__()

    def compose(self) -> ComposeResult:
        yield Static("placeholder")


class ChatPanelApp(LocalizedApp):
    """App whose only widget is a :class:`ChatPanel` (optionally with a localization port)."""

    def __init__(self, localization: TranscriptLocalizationPort | None = None) -> None:
        self._localization = localization
        super().__init__()

    def compose(self) -> ComposeResult:
        yield ChatPanel(localization=self._localization)


class GcMessageChatPanelApp(ChatPanelApp):
    """:class:`ChatPanelApp` that records the GC absorb/reclaim requests it receives."""

    def __init__(self) -> None:
        self.gc_messages: list[GcAbsorbRequested | GcReclaimRequested] = []
        super().__init__()

    def on_gc_absorb_requested(self, message: GcAbsorbRequested) -> None:
        self.gc_messages.append(message)

    def on_gc_reclaim_requested(self, message: GcReclaimRequested) -> None:
        self.gc_messages.append(message)


def chat_content_children(panel: ChatPanel) -> list[object]:
    """Return transcript children, excluding persistent chat-panel infrastructure."""
    return [child for child in panel.children if not isinstance(child, (_ChatBottomSpacer, _ScrollToBottomButton))]


def inline_diff_mount_state(card: Widget) -> str:
    """Describe a file tool card's deferred-diff machinery for wait diagnostics."""
    timer = getattr(card, "_diff_timer", None)
    task = getattr(timer, "_task", None)
    if timer is None:
        timer_state = "none"
    elif task is None:
        timer_state = "stopped"
    elif not task.done():
        timer_state = "pending"
    elif task.cancelled():
        timer_state = "cancelled"
    else:
        timer_state = f"done exc={task.exception()!r}"
    try:
        content = card.query_one("#ft-content")
    except Exception:
        children: list[str] | str = "no-content"
    else:
        children = [
            f"{type(child).__name__}(pruning={getattr(child, '_pruning', False)})" for child in content.children
        ]
    return (
        f"status={getattr(card, 'status', None)!r} pending={getattr(card, '_diff_pending', None)} "
        f"mounting={getattr(card, '_diff_mounting', None)} timer={timer_state} "
        f"done_class={card.has_class('-done')} attached={card.is_attached} content={children}"
    )


def exec_panel_text(tool: Widget) -> str:
    """Plain text currently shown in a shell tool card's ``#exec-panel``."""
    content = tool.query_one("#exec-panel", Static).content
    return content.plain if isinstance(content, Text) else str(content)


def png_bytes(color: tuple[int, int, int], *, size: tuple[int, int] = (32, 20)) -> bytes:
    """Encode a solid-*color* PNG of *size* for image-preview tests."""
    image = Image.new("RGB", size, color)
    out = BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


def make_click(
    widget: Widget, *, x: int = 0, y: int = 0, screen_x: int | None = None, screen_y: int | None = None
) -> Click:
    """Build a plain left-button :class:`Click` at (*x*, *y*) inside *widget*.

    Screen coordinates default to the widget's region offset by (*x*, *y*);
    pass them explicitly to aim at a child widget's region instead.
    """
    return Click(
        widget,
        x=x,
        y=y,
        delta_x=0,
        delta_y=0,
        button=1,
        shift=False,
        meta=False,
        ctrl=False,
        screen_x=widget.region.x + x if screen_x is None else screen_x,
        screen_y=widget.region.y + y if screen_y is None else screen_y,
    )


def click_widget(widget: Widget) -> Click:
    """Deliver a click at the widget's origin straight to its ``on_click`` and return the event."""
    event = make_click(widget)
    widget.on_click(event)
    return event


def header_zone_click(header: ToolCardHeader, zone: str) -> Click:
    """Synthesize a click on a revealed ToolCardHeader action zone."""
    actions_width = header._actions_width()
    if not header.copy_action_visible and zone == "view":
        start, end = 1, actions_width - 1
    else:
        zones = {name: (start, end) for name, start, end in ToolCardHeader._ACTION_ZONES}
        start, end = zones[zone]
    x = header.content_size.width - actions_width + (start + end) // 2
    event = make_click(header, x=x, screen_x=header.region.x + x, screen_y=header.region.y)
    header.on_click(event)
    return event


def click_copy_button(header: ToolCardHeader) -> None:
    """Click the copy zone of a tool card header."""
    header_zone_click(header, "copy")


async def click_when_settled(
    pilot: Pilot, target: Widget | str, *, offset: tuple[int, int] = (0, 0), **modifiers: bool
) -> None:
    """Click *target* where the current layout puts it, and require the click to land on it.

    Pilot reads the target's region before it pumps a single message. A reflow that is still
    pending then moves the widget out from under the click: a row that has just appeared above
    it, or a modal that has not been laid out yet, and the click lands on a neighbour or is
    reported out of bounds. So the geometry is only trusted straight after a refresh of the
    screen that owns it, requested once every widget has handed its layout request to that
    screen, and it is looked at again after another refresh while the target cannot be hit.

    What the click sets off (a ``Pressed`` message, a dismissal) is only drained when the Pilot
    wait is the settled one from ``tests/support/pilot_barrier.py``; wait for the state it produces.
    """
    app = pilot.app
    refresh: tuple[Screen, asyncio.Event] | None = None
    found: list[Widget] = []

    def takes_the_click() -> bool:
        nonlocal refresh
        screen = app.screen
        if refresh is not None and refresh[0] is screen:
            if not refresh[1].is_set():
                return False
            widget = target if isinstance(target, Widget) else next(iter(screen.query(target)), None)
            if widget is not None and widget.region.area:
                x, y = widget.region.offset + offset
                with contextlib.suppress(NoWidget):
                    if screen.region.contains(x, y) and app.get_widget_at(x, y)[0] is widget:
                        found[:] = [widget]
                        return True
        refreshed = asyncio.Event()
        refresh = (screen, refreshed)
        screen.call_after_refresh(refreshed.set)
        return False

    # A widget passes its layout request on when it goes idle, which this waits for; the refresh
    # requested next is queued behind those requests.
    await pilot.pause()
    await wait_for(takes_the_click, pilot=pilot, description=f"{target!r} can take a click")
    assert await pilot.click(found[0], offset=offset, **modifiers)


def delay_resize_dispatch(app: App[None], monkeypatch: pytest.MonkeyPatch, delay: float) -> None:
    """Hold back the App's debounced resize dispatch, as a loaded runner does."""
    dispatch = app._check_resize

    def late_dispatch() -> None:
        app._resize_timer = app.set_timer(delay, dispatch)

    monkeypatch.setattr(app, "_check_resize", late_dispatch)


async def resize_when_settled(pilot: Pilot, width: int, height: int) -> None:
    """Resize the terminal and return once the screen is laid out at the new size.

    The App hands a resize on to its screen from a debounce timer, which the Pilot wait does not
    cover, so ``pilot.resize_terminal`` can return with the screen still laid out at the old size.
    ``Screen.size`` reads the App's size, which changes at once; ``outer_size`` changes with layout.
    """
    await pilot.resize_terminal(width, height)
    await wait_for(
        lambda: pilot.app.screen.outer_size == (width, height),
        pilot=pilot,
        description=f"the screen to be laid out at {width}x{height}",
    )
    # Drain the Resize events that layout posted to the widgets.
    await pilot.pause()


def _simulate_chat_panel_user_scroll_y(panel: ChatPanel, y: float) -> None:
    """Move ChatPanel scroll position through the same watcher path as a user scroll."""
    old_y = panel.scroll_y
    panel._anchor_released = True
    panel.set_reactive(Widget.scroll_y, float(y))
    panel.set_reactive(Widget.scroll_target_y, float(y))
    panel.watch_scroll_y(old_y, float(y))


async def mount_sub_agent_detail(card: SubAgentToolCall, pilot: object) -> AgentTranscriptSurface:
    """Mount and return the same full transcript projection used by the modal."""
    surface = card._tool_view_output_widgets()[0]
    assert isinstance(surface, AgentTranscriptSurface)
    await card.app.mount(surface)
    await wait_for(
        lambda: surface.is_mounted,
        pilot=pilot,
        description="sub-agent detail transcript mounted",
    )
    return surface


async def wait_for_sub_agent_inner_tool(
    card: SubAgentToolCall,
    call_id: str,
    pilot: object,
) -> BaseToolCard:
    """Wait for and return a nested tool from a full detail projection."""
    surface = await mount_sub_agent_detail(card, pilot)
    matched: list[BaseToolCard] = []

    def find_tool() -> bool:
        for group in surface.query(ToolGroup):
            group.collapsed = False
            tool = group.get_tool(call_id)
            if isinstance(tool, BaseToolCard):
                matched[:] = [tool]
                return True
        return False

    await wait_for(find_tool, pilot=pilot, description=f"nested tool {call_id}")
    tool = matched[0]
    assert isinstance(tool, BaseToolCard)
    return tool


# --- Suggestion handler doubles -------------------------------------------------
#
# `SuggestionScreen` mirrors the private `MainScreen` attributes the suggestion
# handler reads through `MainScreenViewAdapter`; every new field the handler
# starts reading has to be added here once. `TaskWorker` needs a running event
# loop, so sync tests that use it drive the handler through `asyncio.run(...)`.


class TaskWorker:
    """A Textual worker double that runs its coroutine on the current event loop."""

    def __init__(self, work) -> None:
        self._task = asyncio.create_task(work)

    @property
    def is_finished(self) -> bool:
        return self._task.done()

    async def wait(self):
        return await self._task


class DeferredWorker:
    """A worker double that starts its coroutine only when it is awaited."""

    def __init__(self, work) -> None:
        self._work = work
        self._task: asyncio.Task | None = None

    @property
    def is_finished(self) -> bool:
        return self._task is not None and self._task.done()

    async def wait(self):
        if self._task is None:
            self._task = asyncio.create_task(self._work)
        return await self._task


class SuggestionListStub:
    """Records what the suggestion handler shows without mounting a widget."""

    def __init__(self) -> None:
        self.last_mode: str | None = None
        self.last_items: list[object] = []
        self.last_title: str | None = None
        self.is_visible = False
        self.is_loading = False
        self.select_result = False

    def show(self, mode: str, items=None, *_args, **_kwargs) -> None:
        self.last_mode = mode
        self.last_items = list(items or [])
        self.last_title = _kwargs.get("title")
        self.is_visible = True
        self.is_loading = False

    def show_loading(self, mode: str, *, title: str = "") -> None:
        self.last_mode = mode
        self.last_items = []
        self.last_title = title
        self.is_visible = True
        self.is_loading = True

    def update(self, items=None, *_args, **_kwargs) -> None:
        self.last_items = list(items or [])
        self.is_loading = False
        title = _kwargs.get("title")
        if title is not None:
            self.last_title = title

    def hide(self) -> None:
        self.last_mode = ""
        self.is_visible = False
        self.is_loading = False
        return

    def select_highlighted(self, *, execute: bool = False) -> bool:
        _ = execute
        return self.select_result

    @property
    def mode(self) -> str:
        return self.last_mode or ""


@dataclass(frozen=True, slots=True)
class AgentProfileStub:
    name: str
    display_name: str = ""
    description: str = ""


class AgentRegistryStub:
    """Serves a fixed set of agent profiles to the `#` trigger."""

    def __init__(self, profiles: list[AgentProfileStub]) -> None:
        self._profiles = profiles

    def list_profiles(self) -> list[AgentProfileStub]:
        return list(self._profiles)


class DismissInputBarStub:
    """Input-bar double that records trigger replacements and prompt-history loads."""

    def __init__(self) -> None:
        self.value = ""
        self.replacements: list[tuple[str, str]] = []
        self.prompt_history: list[str] = []
        self.prompt_history_limits: list[int] = []
        self.suggestions_active = False
        self.suggestion_mode: str | None = None

    def set_suggestions_active(self, active: bool, *, mode: str | None = None) -> None:
        self.suggestions_active = active
        self.suggestion_mode = mode if active else None

    def focus_input(self) -> None:
        return

    def replace_trigger_text(self, trigger: str, replacement: str) -> None:
        self.replacements.append((trigger, replacement))

    async def load_prompt_history(self, *, max_entries: int) -> list[str]:
        self.prompt_history_limits.append(max_entries)
        return list(self.prompt_history)


class SuggestionScreen:
    """The main-screen surface the suggestion handler reads through its view adapter.

    Its state and services are ``state`` and ``services``, which the handler
    shares; writing a field they replaced (see ``main_screen_parts``) fails.
    """

    def __setattr__(self, name: str, value: object) -> None:
        if name in _REMOVED_MAIN_SCREEN_FIELDS:
            raise _removed_main_screen_field_error((name,))
        super().__setattr__(name, value)

    def __init__(self) -> None:
        self.app = type("_App", (), {"available_themes": ["textual-dark"], "theme": "textual-dark"})()
        self.state = MainScreenState()
        self.state.runtime.profile = "Code"
        self._set_workspace_cwd("")
        self.services = MainScreenServices(bus=EventBus(), state_store=object())
        self.is_attached = True
        self.opened: list[str] = []
        self.notifications: list[str] = []
        self.submitted: list[str] = []
        self.picked_models: list[str] = []
        self.fork_requests = 0
        self.clear_requests = 0
        self.title_editor_requests = 0
        self.applied_titles: list[str] = []
        self.suggestion_list = SuggestionListStub()
        self.input_bar = DismissInputBarStub()

    def _set_workspace_cwd(self, cwd: str) -> None:
        """Move the workspace as ``MainScreen._set_workspace_cwd`` does."""
        self.state.workspace.current_cwd = cwd
        self.state.workspace_marker.current_cwd = cwd

    def _debug(self, *_args, **_kwargs) -> None:
        return

    def action_pick_theme(self) -> None:
        return

    def _resume_last_session(self) -> None:
        return

    def _fork_current_session(self) -> None:
        self.fork_requests += 1

    def open_title_editor(self) -> None:
        self.title_editor_requests += 1

    def apply_custom_title(self, custom_title: str) -> None:
        self.applied_titles.append(custom_title)

    def _create_new_session(self) -> None:
        return

    def _clear_current_session(self) -> None:
        self.clear_requests += 1

    def action_quit(self) -> None:
        return

    def action_sessions(self) -> None:
        return

    def start_chdir(self, _arg: str) -> None:
        return

    def _copy_agent_responses(self, _arg: str) -> None:
        return

    def _toggle_fold(self) -> None:
        return

    def action_show_diff(self) -> None:
        return

    def action_show_rollback(self, _arg: str = "") -> None:
        return

    def start_approval_mode_change(self, _arg: str) -> None:
        return

    def _open_model_config(self) -> None:
        return

    def _open_agent_config(self) -> None:
        self.opened.append("agent")

    def _open_agent_config_tab(self, tab: str) -> None:
        self.opened.append(tab)

    def action_runtime_details(self) -> None:
        self.opened.append("runtime")

    def _open_settings(self, tab: str) -> None:
        self.opened.append(f"settings:{tab}")

    def _submit_user_text(self, text: str) -> None:
        self.submitted.append(text)

    def notify(self, message: MessageRef | str, **_kwargs) -> None:
        self.notifications.append(format_message(message) if isinstance(message, MessageRef) else message)

    def run_worker(self, work, **_kwargs) -> TaskWorker:
        return TaskWorker(work)

    def query_one(self, cls):
        name = cls.__name__
        if name == "InputBar":
            return self.input_bar
        return self.suggestion_list


def make_suggestion_screen() -> SuggestionScreen:
    """Build the lightweight main-screen double the suggestion handler drives."""
    return SuggestionScreen()


def make_slash_actions(screen: SuggestionScreen) -> SlashCommandActions:
    """Wire every slash-command action to the recording screen double."""
    return SlashCommandActions(
        list_themes=lambda: sorted(screen.app.available_themes),
        get_theme=lambda: screen.app.theme,
        apply_theme=lambda name: setattr(screen.app, "theme", name),
        pick_theme=screen.action_pick_theme,
        list_languages=list,
        get_language=lambda: "system",
        apply_language=lambda _requested_locale: None,
        pick_language=lambda: None,
        render_unknown_language_warning=lambda requested_locale: f"Unknown /language locale: {requested_locale}",
        debug_event=screen._debug,
        new_session=screen._create_new_session,
        clear_session=screen._clear_current_session,
        quit_app=screen.action_quit,
        resume_session=screen._resume_last_session,
        fork_session=screen._fork_current_session,
        workflow_selection=lambda: None,
        open_guide=lambda: None,
        browse_session_list=screen.action_sessions,
        edit_session_title=screen.open_title_editor,
        apply_session_title=screen.apply_custom_title,
        change_directory=screen.start_chdir,
        copy_conversation=screen._copy_agent_responses,
        fold_tools=screen._toggle_fold,
        open_diff=screen.action_show_diff,
        open_rollback=screen.action_show_rollback,
        get_approval_mode=lambda: ApprovalMode.MANUAL.value,
        change_approval_mode=screen.start_approval_mode_change,
        configure_model=screen._open_model_config,
        configure_agent=screen._open_agent_config,
        configure_agent_tab=screen._open_agent_config_tab,
        show_runtime_details=screen.action_runtime_details,
        configure_settings=screen._open_settings,
        show_manual_pages=lambda _pages, _start_index: None,
        warn=lambda message, title, timeout: screen.notify(message, title=title, timeout=timeout),
    )


def make_suggestion_handler(
    screen: SuggestionScreen,
    *,
    locale_controller: LocaleController | None = None,
) -> SuggestionHandler:
    """Build a suggestion handler around the screen double."""
    view = MainScreenViewAdapter(screen, state=screen.state)  # type: ignore[arg-type]
    return SuggestionHandler(
        state=screen.state,
        services=screen.services,
        view=view,
        command_actions=make_slash_actions(screen),
        callbacks=SuggestionCallbacks(
            notify_warning=lambda message, title, timeout: screen.notify(message, title=title, timeout=timeout),
            start_worker=discard_worker,
            submit_user_text=screen._submit_user_text,
            start_agent_profile_switch=lambda _profile: None,
            start_model_profile_switch=screen.picked_models.append,
        ),
        buddy_view=view,
        locale_controller=locale_controller,
    )


def suggestion_values(items: list[object]) -> list[str]:
    """Read the values out of suggestion items or plain (value, label) pairs."""
    values: list[str] = []
    for item in items:
        if isinstance(item, SuggestionItem):
            values.append(item.value)
        else:
            value, _label = item  # type: ignore[misc]
            values.append(value)
    return values


def scan_result(root: str, paths: list[ProjectPathSuggestion], **kwargs: object) -> ProjectPathScanResult:
    """Build a project-path scan result for a fake scanner."""
    return ProjectPathScanResult.from_suggestions(root=root, paths=paths, **kwargs)


async def wait_for_file_query(handler: SuggestionHandler) -> None:
    """Await the handler's in-flight `@` file query, if there is one."""
    worker = handler._file_query_worker
    if worker is not None:
        await worker.wait()
