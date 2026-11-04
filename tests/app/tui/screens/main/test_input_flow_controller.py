# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for InputFlowController retry, interrupt publication and user-message sending."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import suppress
from types import SimpleNamespace

import pytest

from chrys.app.tui.screens.main.input_flow import InputFlowController
from chrys.app.tui.screens.main.screen import MainScreen
from chrys.app.tui.screens.main.state import MainScreenServices, MainScreenState
from chrys.app.tui.support.gc_freeze import (
    GcAbsorbReason,
    GcAbsorbRequested,
)
from chrys.app.tui.widgets.chrome.input_bar import ChatDraft
from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import (
    Error,
    InvocationContentDelta,
    InvocationContentFinished,
    InvocationMessage,
    UserInterrupt,
    UserMessage,
    UserRetry,
    Warning,
)
from chrys.foundation.i18n import MessageRef
from chrys.foundation.models.invocations import InvocationOrigin
from tests.support.tui_helpers import (
    fake_session_title,
    make_backend_handler,
    status_text,
)

_PREPARED_IMAGE = {"type": "image", "media_type": "image/png"}
"""An image content the backend prepared: never text, so the user bubble shows only the message's text."""


class _FakeInputFlowView:
    def __init__(self) -> None:
        self.calls: list[object] = []
        self.user_contents: list[object] = []
        self.gc_messages: list[object] = []
        self.session_id = "panel-session"

    def set_terminal_title_for_user_message(self, text: str) -> None:
        self.calls.append(("title", text))

    def clear_terminal_title_result(self) -> None:
        self.calls.append("clear_title_result")

    def current_chat_session_id(self) -> str:
        return self.session_id

    def add_input_history(self, text: str, *, session_id: str | None) -> None:
        self.calls.append(f"history:{text}:{session_id}")

    def set_retry_mode(self, enabled: bool, *, label: str = "Retry") -> None:
        self.calls.append(("retry_mode", enabled, label))

    def start_run_status(self, label: MessageRef | str) -> None:
        self.calls.append(f"status:{status_text(label)}")

    async def render_user_message(self, text: str, *, created_at: object, contents: object) -> None:
        self.calls.append(f"user:{text}")
        self.user_contents.append(contents)

    async def render_user_retry_note(self, text: str, *, created_at: object) -> None:
        self.calls.append(f"retry-note:{text}")

    def hide_trailing_status_action(self) -> None:
        self.calls.append("hide_status_action")

    def set_retry_pending(self, pending: bool) -> None:
        self.calls.append(("retry_pending", pending))

    def clear_inline_questions(self) -> None:
        self.calls.append("clear_questions")

    def clear_ask_user_inline_prompts(self) -> None:
        self.calls.append("clear_inline")

    def lock_input_with_text(self) -> None:
        self.calls.append("lock_input")

    def unlock_input_keep_if_locked(self) -> None:
        self.calls.append("unlock_keep")

    def flash_interrupted(self) -> None:
        self.calls.append("flash_interrupted")

    async def render_interrupted(self) -> None:
        self.calls.append("interrupted")

    def update_toc(self) -> None:
        self.calls.append("toc")

    def restore_input_draft(self, draft: str | ChatDraft) -> bool:
        self.calls.append(("restore", draft))
        return True

    def flash_submit_still_sending(self) -> None:
        self.calls.append("still_sending")

    def show_status(self, text: MessageRef | str) -> None:
        self.calls.append(f"show_status:{status_text(text)}")

    def clear_status(self) -> None:
        self.calls.append("clear_status")


def _make_input_flow_controller(
    bus: EventBus,
    *,
    state: MainScreenState | None = None,
    view: _FakeInputFlowView | None = None,
    handle_agent_message: object | None = None,
    handle_agent_content: object | None = None,
    handle_error: object | None = None,
    running: list[bool] | None = None,
    has_messages: list[bool] | None = None,
    start_worker: object | None = None,
) -> tuple[InputFlowController, MainScreenState, _FakeInputFlowView]:
    state = state or MainScreenState()
    view = view or _FakeInputFlowView()
    running = running if running is not None else []
    has_messages = has_messages if has_messages is not None else []

    async def _default_handle_agent_message(event: InvocationMessage) -> None:
        view.calls.append(f"agent:{event.text}")

    async def _default_handle_agent_content(event: InvocationContentDelta | InvocationContentFinished) -> None:
        view.calls.append(f"content:{event.segment_id}")

    async def _default_handle_error(event: Error) -> None:
        view.calls.append(f"error:{event.message}")

    def _unexpected_start_worker(work: object) -> None:
        raise AssertionError(f"unexpected worker scheduling: {work!r}")

    def _set_agent_running(value: bool) -> None:
        state.run.agent_running = value
        running.append(value)

    def _set_has_messages(value: bool) -> None:
        state.run.has_messages = value
        has_messages.append(value)

    controller = InputFlowController(
        state=state,
        services=MainScreenServices(bus=bus),
        view=view,
        start_worker=start_worker or _unexpected_start_worker,
        handle_agent_message=handle_agent_message or _default_handle_agent_message,
        handle_agent_content=handle_agent_content or _default_handle_agent_content,
        handle_error=handle_error or _default_handle_error,
        set_agent_running=_set_agent_running,
        set_has_messages=_set_has_messages,
        clear_workspace_marker=lambda: setattr(state.workspace_marker, "original_cwd", None),
        clear_pending_questions=view.clear_inline_questions,
        commit_profile_switch_marker=lambda: setattr(state.profile_marker, "from_profile", None),
        post_gc_message=view.gc_messages.append,
        debug=lambda *_args: None,
        pending_switch=lambda _draft: None,
        model_left_out=lambda: None,
        show_error_notice=lambda error: view.calls.append(f"notice:{error.code}"),
    )
    return controller, state, view


async def _run_started(scheduled: list[Callable[[], Awaitable[object]]]) -> None:
    """Run the work the controller handed its worker hook, as the started workers would.

    Each on a task of its own, as a Textual worker runs, apart from the handler
    that scheduled it.
    """
    while scheduled:
        await asyncio.ensure_future(scheduled.pop(0)())


def test_input_bar_retry_leaves_status_action_until_admission() -> None:
    from chrys.app.tui.widgets.chrome.input_bar import InputBar

    calls: list[object] = []

    class _FakeChatPanel:
        def hide_trailing_status_action(self) -> None:
            calls.append("hide_status_action")

    def query_one(cls: type) -> object:
        if cls.__name__ == "ChatPanel":
            return _FakeChatPanel()
        raise AssertionError(f"unexpected query_one({cls.__name__})")

    flow = SimpleNamespace(request_retry=lambda text, _draft=None: calls.append(("retry", text)))
    screen = SimpleNamespace(
        query_one=query_one,
        _input_flow=flow,
        _model_unconfigured=lambda: False,
        _workflow=SimpleNamespace(workflow_mode=False),
        _suggestions=SimpleNamespace(suggestion_mode=None),
        _services=MainScreenServices(bus=EventBus()),
    )

    MainScreen._on_retry_requested(screen, InputBar.RetryRequested("please continue"))

    assert calls == [("retry", "please continue")]


def test_inline_status_retry_resumes_without_consuming_input_draft() -> None:
    from chrys.app.tui.widgets.chat.messages import ConversationStatusAction
    from chrys.app.tui.widgets.chrome.input_bar import InputBar

    calls: list[object] = []

    class _FakeInputBar:
        retry_mode = True

        def consume_retry_text(self) -> str:
            calls.append("consume_retry_text")
            return "typed note"

    def query_one(cls: type) -> object:
        if cls is InputBar:
            return _FakeInputBar()
        raise AssertionError(f"unexpected query_one({cls.__name__})")

    flow = SimpleNamespace(request_retry=lambda text, _draft=None: calls.append(("retry", text)))
    screen = SimpleNamespace(
        _state=MainScreenState(),
        query_one=query_one,
        _input_flow=flow,
        _model_unconfigured=lambda: False,
    )

    MainScreen._on_conversation_status_action_pressed(screen, ConversationStatusAction.Pressed())

    # The inline card resumes plainly: the typed draft is neither consumed
    # nor sent as the continuation prompt (that is the input bar's button).
    assert calls == [("retry", "")]


def test_request_retry_ignores_duplicate_while_retry_submit_is_pending() -> None:
    state = MainScreenState()
    state.submit.active = True
    state.submit.kind = "retry"
    controller, _state, view = _make_input_flow_controller(EventBus(), state=state)

    controller.request_retry("duplicate note")

    assert view.calls == []


def test_a_note_refused_while_a_message_is_on_its_way_comes_back() -> None:
    # A message's admission can take a while (a prompt shortcut's commands): a Continue
    # note typed meanwhile is the user's, not dropped.
    state = MainScreenState()
    state.submit.active = True
    state.submit.kind = "turn"
    controller, _state, view = _make_input_flow_controller(EventBus(), state=state)

    controller.request_retry("my note")

    assert view.calls == [("restore", "my note"), "still_sending"]


def test_a_note_whose_continue_meets_a_turn_started_since_the_keypress_comes_back_whole() -> None:
    state = MainScreenState()
    state.run.agent_running = True
    controller, _state, view = _make_input_flow_controller(EventBus(), state=state)
    draft = ChatDraft("my note", fields=((3, 7),))

    controller.request_retry("my note", draft)

    assert view.calls == [("restore", draft)]
    assert state.submit.active is False


def test_a_message_whose_send_meets_a_load_started_since_the_keypress_comes_back_whole() -> None:
    """Enter cleared the composer and queued the send; a load began before the send ran."""
    bus = EventBus()
    published: list[UserMessage] = []
    state = MainScreenState()
    controller, _state, view = _make_input_flow_controller(bus, state=state)
    draft = ChatDraft("Review this", fields=((7, 11),))

    async def _record(event: UserMessage) -> None:
        published.append(event)

    async def _run() -> None:
        await bus.subscribe(UserMessage, _record)
        state.run.agent_loading = True
        await controller.send_user_message("Review this", draft)

    asyncio.run(_run())

    assert published == []
    assert view.calls == [("restore", draft)]
    assert state.submit.active is False


@pytest.mark.parametrize("outcome", ["accepted", "rejected", "cancelled"])
async def test_retry_reserves_before_worker_start_and_releases_after_admission(outcome: str) -> None:
    bus = EventBus()
    scheduled = []
    controller, state, view = _make_input_flow_controller(bus, start_worker=scheduled.append)
    entered = asyncio.Event()
    release = asyncio.Event()
    published = []

    async def admit(event: UserRetry) -> None:
        published.append(event.text)
        entered.set()
        await release.wait()
        state.submit.blocked = outcome == "rejected"

    await bus.subscribe(UserRetry, admit)
    controller.request_retry("first note")
    controller.request_retry("same-tick duplicate")
    assert state.submit.active
    assert state.submit.text == "first note"
    assert view.calls == [("retry_pending", True)]
    assert len(scheduled) == 1
    task = asyncio.create_task(scheduled.pop()())
    try:
        async with asyncio.timeout(5):
            await entered.wait()
            controller.request_retry("duplicate click")
            assert not scheduled
            assert published == ["first note"]
            if outcome == "cancelled":
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                release.set()
                await task
    finally:
        release.set()
        task.cancel()
        async with asyncio.timeout(5):
            with suppress(asyncio.CancelledError):
                await task

    assert not state.submit.active
    assert not state.render_gate.active
    assert view.calls.count(("retry_pending", False)) == 1
    assert state.run.agent_running is (outcome == "accepted")
    if outcome == "accepted":
        assert "hide_status_action" in view.calls
        controller.request_retry("already running")
        assert not scheduled
    else:
        assert "hide_status_action" not in view.calls
        # A later intentional attempt is available again after rejection/cancel.
        controller.request_retry("try again")
        await _run_started(scheduled)
        assert published == ["first note", "try again"]


@pytest.mark.parametrize(
    "error_code",
    [
        "executor_error",
        "retry_missing_user_anchor",
        "hook_blocked",
        "not_ready",
        "prompt_admission_conflict",
        "sub_agent_paused",
    ],
)
@pytest.mark.parametrize("text", ["", "https://example.com/document"], ids=["plain", "with-note"])
@pytest.mark.parametrize("initial_retry_mode", [False, True], ids=["ordinary-input", "continue-input"])
def test_retry_distinguishes_admission_rejections_from_fast_run_errors(
    error_code: str, text: str, initial_retry_mode: bool
) -> None:
    bus = EventBus()
    state = MainScreenState()
    order: list[str] = []

    class _FlowView(_FakeInputFlowView):
        def start_run_status(self, label: MessageRef | str) -> None:
            super().start_run_status(label)
            order.append(f"status:{status_text(label)}")

        def hide_trailing_status_action(self) -> None:
            order.append("hide_status_action")

    class _FakeStatusBar:
        def flash(self, message: str, *, error: bool = False) -> None:
            order.append(f"flash:{message}:{error}")

    class _FakeInputBar:
        def __init__(self) -> None:
            self.locked = True
            self._retry_label = "Continue" if initial_retry_mode else ""
            self.retry_mode = initial_retry_mode
            self.value = ""
            self.restored: str | ChatDraft | None = None

        def restore_draft(self, draft: str | ChatDraft) -> bool:
            self.value = draft.text if isinstance(draft, ChatDraft) else draft
            self.restored = draft
            return True

        def unlock_and_keep(self) -> None:
            self.locked = False
            order.append("unlock")

    class _FakeChatPanel:
        async def add_error(self, message: str, *, action_label: str | None = "Retry") -> None:
            order.append(f"error:{message}:{action_label}")

    def query_one(cls: object) -> object:
        if cls.__name__ == "StatusBar":
            return _FakeStatusBar()
        if cls.__name__ == "InputBar":
            return input_bar
        if cls.__name__ == "ChatPanel":
            return _FakeChatPanel()
        raise AssertionError(f"unexpected query_one({cls.__name__})")

    input_bar = _FakeInputBar()
    view = _FlowView()
    screen = SimpleNamespace(
        _state=state,
        _session_title=fake_session_title(),
        query_one=query_one,
        _debug=lambda *_args: None,
        notify=lambda message, **kwargs: order.append(
            f"notify:{status_text(kwargs['title'])}:{status_text(message)}:{kwargs['severity']}"
        ),
    )
    handler = make_backend_handler(screen)
    handler._agent_load_dialog = None
    scheduled: list[Callable[[], Awaitable[object]]] = []
    controller, _state, _view = _make_input_flow_controller(
        bus,
        state=state,
        view=view,
        handle_error=handler.on_error,
        start_worker=scheduled.append,
    )

    # A note typed over a shortcut's draft whose field is still unfilled: a refusal gives it back whole.
    draft = ChatDraft(text, fields=((0, 5),)) if text else None

    async def _fail_retry(_event: UserRetry) -> None:
        event_type = Warning if error_code == "sub_agent_paused" else Error
        await bus.publish(event_type(code=error_code, message="retry failed"))

    async def _run() -> None:
        await bus.subscribe(UserRetry, _fail_retry)
        await bus.subscribe(Error, handler.on_error)
        await bus.subscribe(Warning, handler.on_warning)
        controller.request_retry(text, draft)
        await _run_started(scheduled)

    asyncio.run(_run())

    assert state.run.agent_running is False
    assert state.render_gate.active is False
    if error_code != "executor_error":
        severity = "warning" if error_code == "sub_agent_paused" else "error"
        title = "Warning" if error_code == "sub_agent_paused" else "Retry not started"
        assert f"notify:{title}:retry failed:{severity}" in order
        assert not any(item.startswith("error:") for item in order)
        assert "hide_status_action" not in order
        assert "status:Resuming" not in order
        assert f"retry-note:{text}" not in view.calls
        assert input_bar.value == text
        if draft is not None:
            assert input_bar.restored == draft
        assert input_bar.locked is False
        assert input_bar.retry_mode is initial_retry_mode
        assert input_bar._retry_label == ("Continue" if initial_retry_mode else "")
    else:
        assert order.index("hide_status_action") < order.index("status:Resuming")
        assert order.index("status:Resuming") < order.index("error:retry failed:Retry")
        assert state.submit.blocked is False
        assert input_bar.retry_mode is True
        if text:
            assert f"retry-note:{text}" in view.calls


@pytest.mark.parametrize("status", ["error", "interrupted"])
@pytest.mark.parametrize("rejection", ["retry_missing_user_anchor", "sub_agent_paused"])
@pytest.mark.parametrize("context_changed", [False, True], ids=["trailing-status", "trailing-system-messages"])
async def test_rejected_retry_keeps_mounted_action_until_next_retry_is_accepted(
    status: str, rejection: str, context_changed: bool
) -> None:
    from textual.widgets import Button

    from chrys.app.tui.screens.main.view_adapter import MainScreenViewAdapter
    from chrys.app.tui.widgets.chat.messages import ErrorMessage, InterruptedMessage
    from chrys.app.tui.widgets.chat.panel import ChatPanel
    from tests.support.tui_helpers import ChatPanelApp

    state = MainScreenState()
    bus = EventBus()
    notifications: list[str] = []

    class Input:
        retry_mode = True
        _retry_label = "Continue"
        value = ""
        locked = True

        def restore_draft(self, draft: str | ChatDraft) -> bool:
            text = draft.text if isinstance(draft, ChatDraft) else draft
            self.value = text
            return True

        def unlock_and_keep(self) -> None:
            self.locked = False

    input_bar = Input()
    async with ChatPanelApp().run_test() as pilot:
        panel = pilot.app.query_one(ChatPanel)
        if status == "error":
            await panel.add_error("Previous run failed")
            card = panel.query_one(ErrorMessage)
        else:
            await panel.add_interrupted()
            card = panel.query_one(InterruptedMessage)
        if context_changed:
            await panel.add_system("Workspace changed: /old → /new")
            await panel.add_system("Agent profile switched: Code → QA")
        await pilot.pause()
        row = card.query_one(".status-action-row")
        button = card.query_one(Button)

        class View(_FakeInputFlowView):
            def hide_trailing_status_action(self) -> None:
                panel.hide_trailing_status_action()

            def set_retry_pending(self, pending: bool) -> None:
                MainScreenViewAdapter(screen, state=MainScreenState()).set_retry_pending(pending)

        def query_one(cls):
            if cls is ChatPanel:
                return panel
            if cls.__name__ == "InputBar":
                return input_bar
            if cls.__name__ == "StatusBar":
                return SimpleNamespace(flash=lambda *_args, **_kwargs: None)
            raise AssertionError(f"Unexpected widget: {cls}")

        screen = SimpleNamespace(
            _state=state,
            query_one=query_one,
            notify=lambda message, **_kwargs: notifications.append(status_text(message)),
            _debug=lambda *_args: None,
        )
        handler = make_backend_handler(screen)
        scheduled: list[Callable[[], Awaitable[object]]] = []
        controller, _, _ = _make_input_flow_controller(
            bus, state=state, view=View(), handle_error=handler.on_error, start_worker=scheduled.append
        )
        attempts = 0

        async def retry(_event: UserRetry) -> None:
            nonlocal attempts
            attempts += 1
            assert button.disabled and row.display
            assert input_bar.retry_pending
            if attempts <= 2:
                event_type = Warning if rejection == "sub_agent_paused" else Error
                await bus.publish(event_type(code=rejection, message="Cannot start yet"), raise_handler_errors=True)

        await bus.subscribe(UserRetry, retry)
        await bus.subscribe(Error, handler.on_error)
        await bus.subscribe(Warning, handler.on_warning)
        for attempt in range(1, 3):
            controller.request_retry("https://example.com/document")
            await _run_started(scheduled)
            assert notifications == ["Cannot start yet"] * attempt
            assert card.is_mounted and row.display
            assert not button.disabled and not input_bar.retry_pending
            assert input_bar.value == "https://example.com/document"
            assert not state.run.agent_running
            assert not state.submit.active and state.submit.kind == "turn"

        controller.request_retry("https://example.com/document")
        await _run_started(scheduled)
        assert attempts == 3
        assert card.is_mounted and not row.display
        assert not input_bar.retry_pending
        assert state.run.agent_running
        assert not state.submit.active and state.submit.kind == "turn"


def test_publish_interrupt_publishes_before_marking_agent_idle(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sleep ToolCallResult handlers must still see the screen as running."""
    from textual import _time, events

    bus = EventBus()
    observed_running: list[bool] = []
    order: list[str] = []
    input_events: list[events.Key] = []

    class _RunningStates(list[bool]):
        def append(self, value: bool) -> None:
            order.append("idle")
            super().append(value)

    class _GcMessages(list[object]):
        def append(self, message: object) -> None:
            order.append("gc")
            super().append(message)

    class _OrderedView(_FakeInputFlowView):
        def clear_terminal_title_result(self) -> None:
            order.append("clear-title")
            super().clear_terminal_title_result()

        async def render_interrupted(self) -> None:
            assert state.run.agent_running is True
            input_events.append(events.Key("x", "x"))
            order.append("render")
            await super().render_interrupted()

    running = _RunningStates()
    state = MainScreenState()
    state.run.agent_running = True
    view = _OrderedView()
    view.gc_messages = _GcMessages()
    controller, state, view = _make_input_flow_controller(bus, state=state, view=view, running=running)

    async def _on_interrupt(_event: UserInterrupt) -> None:
        observed_running.append(state.run.agent_running)

    async def _run() -> None:
        await bus.subscribe(UserInterrupt, _on_interrupt)
        await controller.publish_interrupt()

    source_times = iter([10.0, 20.0])
    monkeypatch.setattr(_time, "get_time", lambda: next(source_times))
    asyncio.run(_run())

    assert observed_running == [True]
    assert "interrupted" in view.calls
    assert "clear_questions" in view.calls
    assert running == [False]
    assert state.run.agent_running is False
    assert order == ["render", "clear-title", "idle", "gc"]
    assert "clear_title_result" in view.calls
    assert len(view.gc_messages) == 1
    assert isinstance(view.gc_messages[0], GcAbsorbRequested)
    assert view.gc_messages[0].reason is GcAbsorbReason.TURN_TERMINAL
    assert view.gc_messages[0].terminal_boundary is True
    assert view.gc_messages[0].time == 10.0
    assert input_events[0].time == 20.0


def test_publish_interrupt_reasserts_flash_after_idle_gate_closes() -> None:
    """A teardown event that flips the status bar back to run mode while the
    interrupt awaits (``agent_running`` still True) must be overwritten by a
    second interrupted flash after the gate closes."""
    bus = EventBus()
    order: list[str] = []

    class _RunningStates(list[bool]):
        def append(self, value: bool) -> None:
            order.append("idle")
            super().append(value)

    class _RaceView(_FakeInputFlowView):
        def flash_interrupted(self) -> None:
            order.append("flash")
            super().flash_interrupted()

        async def render_interrupted(self) -> None:
            # Simulate a CompactionFinished/late ToolCallResult handler racing
            # the teardown window and re-showing the run-mode status while
            # ``agent_running`` is still True.
            order.append("raced-show-status")
            await super().render_interrupted()

    state = MainScreenState()
    state.run.agent_running = True
    controller, state, view = _make_input_flow_controller(bus, state=state, view=_RaceView(), running=_RunningStates())

    asyncio.run(controller.publish_interrupt())

    assert order == ["flash", "raced-show-status", "idle", "flash"]
    assert "clear_title_result" in view.calls
    assert view.calls.count("flash_interrupted") == 2


def test_publish_interrupt_render_failure_releases_turn_without_absorb() -> None:
    bus = EventBus()
    running: list[bool] = []

    class _FailingView(_FakeInputFlowView):
        async def render_interrupted(self) -> None:
            self.calls.append("interrupted")
            raise RuntimeError("render failed")

    state = MainScreenState()
    state.run.agent_running = True
    state.pending_injection.begin("pending-id", "queued")
    view = _FailingView()
    controller, state, view = _make_input_flow_controller(bus, state=state, view=view, running=running)

    with pytest.raises(RuntimeError, match="render failed"):
        asyncio.run(controller.publish_interrupt())

    assert running == [False]
    assert state.run.agent_running is False
    assert state.pending_injection.active is False
    assert "unlock_keep" in view.calls
    assert "clear_title_result" in view.calls
    assert view.gc_messages == []
    assert ("retry_mode", True, "Continue") not in view.calls


def test_send_user_message_real_bus_rejection_sets_pending_blocked_before_render() -> None:
    bus = EventBus()
    running: list[bool] = []
    published: list[str] = []
    controller, state, view = _make_input_flow_controller(bus, running=running)

    async def _reject_image_prompt(event: UserMessage) -> None:
        published.append(event.text)
        state.submit.block()

    async def _run() -> None:
        await bus.subscribe(UserMessage, _reject_image_prompt)
        await controller.send_user_message("describe @shot.png")

    asyncio.run(_run())

    assert published == ["describe @shot.png"]
    assert state.submit.active is False
    assert state.submit.blocked is False
    assert state.render_gate.active is False
    assert running == []
    assert "user:describe @shot.png" not in view.calls


def test_send_user_message_defers_fast_agent_message_until_user_bubble_is_rendered() -> None:
    bus = EventBus()
    state = MainScreenState()
    view = _FakeInputFlowView()

    async def _on_agent_message(event: InvocationMessage) -> None:
        if state.render_gate.defers(event):
            state.render_gate.defer(event)
            return
        view.calls.append(f"agent:{event.text}")

    async def _on_agent_content(event: InvocationContentDelta | InvocationContentFinished) -> None:
        if state.render_gate.defers(event):
            state.render_gate.defer(event)
            return
        view.calls.append(f"delta:{event.text}" if isinstance(event, InvocationContentDelta) else "finished")

    controller, _state, view = _make_input_flow_controller(
        bus,
        state=state,
        view=view,
        handle_agent_message=_on_agent_message,
        handle_agent_content=_on_agent_content,
    )

    async def _accept_and_finish(event: UserMessage) -> None:
        event.prepared_contents = ["hello", _PREPARED_IMAGE]
        event.started_turn = True
        origin = InvocationOrigin("turn", event.session_id or "", "turn-test", None)
        await bus.publish(
            InvocationContentDelta(
                origin=origin, session_id=event.session_id, segment_id="s1", channel="answer", text="done"
            )
        )
        await bus.publish(
            InvocationContentFinished(origin=origin, session_id=event.session_id, segment_id="s1", channel="answer")
        )
        await bus.publish(
            InvocationMessage(text=f"done:{event.text}", is_final=True, session_id=event.session_id, origin=origin)
        )

    async def _run() -> None:
        await bus.subscribe(UserMessage, _accept_and_finish)
        await bus.subscribe(InvocationContentDelta, _on_agent_content)
        await bus.subscribe(InvocationContentFinished, _on_agent_content)
        await bus.subscribe(InvocationMessage, _on_agent_message)
        await controller.send_user_message("hello")

    asyncio.run(_run())

    # Streamed text and the final both wait for the user bubble, then replay in order.
    user_index = view.calls.index("user:hello")
    assert view.calls[user_index + 1 : user_index + 4] == ["delta:done", "finished", "agent:done:hello"]
    assert view.user_contents == [["hello", _PREPARED_IMAGE]]


def test_text_arriving_while_deferred_text_replays_waits_behind_it() -> None:
    """A replayed delta can await a mount; a live one arriving meanwhile must not overtake the backlog."""
    bus = EventBus()
    state = MainScreenState()
    view = _FakeInputFlowView()
    origin = InvocationOrigin("turn", "", "turn-test", None)

    def _delta(text: str) -> InvocationContentDelta:
        return InvocationContentDelta(origin=origin, segment_id="s1", channel="answer", text=text)

    live = _delta("C")

    async def _on_agent_content(event: InvocationContentDelta | InvocationContentFinished) -> None:
        if state.render_gate.defers(event):
            state.render_gate.defer(event)
            return
        if isinstance(event, InvocationContentDelta):
            view.calls.append(f"delta:{event.text}")
            if event.text == "A":
                # The engine streams on while this delta's mount is awaited.
                await bus.publish(live)

    controller, _state, view = _make_input_flow_controller(
        bus, state=state, view=view, handle_agent_content=_on_agent_content
    )

    async def _accept_and_stream(event: UserMessage) -> None:
        event.started_turn = True
        await bus.publish(_delta("A"))
        await bus.publish(_delta("B"))

    async def _run() -> None:
        await bus.subscribe(UserMessage, _accept_and_stream)
        await bus.subscribe(InvocationContentDelta, _on_agent_content)
        await controller.send_user_message("hello")

    asyncio.run(_run())

    assert [call for call in view.calls if str(call).startswith("delta:")] == ["delta:A", "delta:B", "delta:C"]
    assert state.render_gate.active is False


def test_send_user_message_defers_fast_error_until_user_bubble_is_rendered() -> None:
    bus = EventBus()
    state = MainScreenState()
    order: list[str] = []

    class _FlowView(_FakeInputFlowView):
        async def render_user_message(self, text: str, *, created_at: object, contents: object) -> None:
            await super().render_user_message(text, created_at=created_at, contents=contents)
            order.append(f"user:{text}")

    class _FakeStatusBar:
        def flash(self, message: str, *, error: bool = False) -> None:
            order.append(f"status:{message}:{error}")

    class _FakeInputBar:
        def __init__(self) -> None:
            self.locked = True
            self._retry_label = ""
            self.retry_mode = False

        def unlock_and_keep(self) -> None:
            self.locked = False
            order.append("unlock")

    class _FakeChatPanel:
        async def add_error(self, message: str, *, action_label: str | None = "Retry") -> None:
            order.append(f"error:{message}:{action_label}")

    def query_one(cls: object) -> object:
        if cls.__name__ == "StatusBar":
            return _FakeStatusBar()
        if cls.__name__ == "InputBar":
            return input_bar
        if cls.__name__ == "ChatPanel":
            return _FakeChatPanel()
        raise AssertionError(f"unexpected query_one({cls.__name__})")

    input_bar = _FakeInputBar()
    view = _FlowView()
    screen = SimpleNamespace(
        _state=state,
        _session_title=fake_session_title(),
        query_one=query_one,
        _debug=lambda *_args: None,
    )
    handler = make_backend_handler(screen)
    handler._agent_load_dialog = None

    controller, _state, view = _make_input_flow_controller(
        bus,
        state=state,
        view=view,
        handle_error=handler.on_error,
    )

    async def _accept_and_fail(event: UserMessage) -> None:
        event.prepared_contents = ["hello", _PREPARED_IMAGE]
        event.started_turn = True
        await bus.publish(Error(code="executor_error", message=f"failed:{event.text}", session_id=event.session_id))

    async def _run() -> None:
        await bus.subscribe(UserMessage, _accept_and_fail)
        await bus.subscribe(Error, handler.on_error)
        await controller.send_user_message("hello")

    asyncio.run(_run())

    user_index = order.index("user:hello")
    error_index = order.index("error:failed:hello:Retry")
    assert user_index < error_index
    assert view.user_contents == [["hello", _PREPARED_IMAGE]]
    assert state.submit.blocked is False
    assert input_bar.retry_mode is True
