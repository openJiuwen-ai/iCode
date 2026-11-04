# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for on_error / on_warning user-visible surfaces: modals, toasts and prompt restore."""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from dataclasses import dataclass, field, replace
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from chrys.app.tui.i18n import LocaleController
from chrys.app.tui.screens.main.event_handlers import (
    _UNKNOWN_ERROR,
    BackendEventHandler,
    _WarningDedupeKey,
)
from chrys.app.tui.screens.main.state import MainScreenState, RunState, SessionViewState
from chrys.app.tui.support.gc_freeze import (
    GcAbsorbReason,
    GcAbsorbRequested,
)
from chrys.app.tui.widgets.chrome.input_bar import ChatDraft
from chrys.app.tui.widgets.chrome.prompt_draft import ShortcutIntent
from chrys.foundation.branding import APP_DISPLAY_NAME
from chrys.foundation.config.settings import Settings
from chrys.foundation.events.types import (
    Error,
    Warning,
)
from chrys.foundation.i18n import Localizer, MessageRef
from tests.support.tui_helpers import (
    fake_session_title,
    main_screen_state_at,
    make_backend_handler,
    make_session_handler,
    status_text,
)


def test_session_in_use_error_uses_modal_not_chat_or_status() -> None:
    """Session ownership conflicts should be modal-only, not chat/status noise."""
    from chrys.app.tui.screens.dialogs.confirm import ConfirmDialog

    pushed: list[object] = []
    debug_calls: list[tuple[str, str]] = []
    running: list[bool] = []

    def query_one(_cls: object) -> object:
        raise AssertionError("session_in_use should not query chat, status, or input widgets")

    def push_screen(dialog: object) -> None:
        pushed.append(dialog)

    def set_agent_running(value: bool) -> None:
        running.append(value)

    def debug(key: str, msg: str) -> None:
        debug_calls.append((key, msg))

    restoring: list[bool] = []
    screen = SimpleNamespace(
        _state=MainScreenState(session=SessionViewState(restoring_session=True)),
        _set_restoring_session=restoring.append,
        app=SimpleNamespace(push_screen=push_screen),
        _set_agent_running=set_agent_running,
        query_one=query_one,
        _debug=debug,
    )
    handler = make_backend_handler(screen)
    handler._agent_load_dialog = None

    message = f"Session '40d9a0483e08' is already open in another {APP_DISPLAY_NAME} instance (pid=47219)."
    asyncio.run(handler.on_error(Error(code="session_in_use", message=message)))

    assert screen._state.session.restoring_session is False
    assert restoring == [False]
    assert running == [False]
    assert len(pushed) == 1
    dialog = pushed[0]
    assert isinstance(dialog, ConfirmDialog)
    assert dialog._title == "Session In Use"
    assert dialog._message.plain == f"Session Already Open\n\n{message}"
    assert dialog._message.spans[0].start == 0
    assert dialog._message.spans[0].end == len("Session Already Open")
    assert str(dialog._message.spans[0].style) == "bold"
    assert dialog._confirm_label == "OK"
    assert dialog._cancel_label is None
    assert dialog._confirm_variant == "warning"
    assert dialog.has_class("-warning-border")
    assert debug_calls and debug_calls[0][0] == "Error"


def test_hosted_web_warning_uses_modal_once_per_configuration() -> None:
    """A capability handover reaches the user as a modal, and only once per configuration."""
    from chrys.app.tui.screens.dialogs.confirm import ConfirmDialog

    pushed: list[object] = []
    debug_calls: list[tuple[str, str]] = []
    notified: list[object] = []

    def push_screen(dialog: object) -> None:
        pushed.append(dialog)

    def notify(*args: object, **kwargs: object) -> None:
        notified.append((args, kwargs))

    def debug(key: str, message: str) -> None:
        debug_calls.append((key, message))

    screen = SimpleNamespace(
        app=SimpleNamespace(push_screen=push_screen),
        notify=notify,
        _debug=debug,
    )
    handler = make_backend_handler(screen)
    event = Warning(
        code="hosted_web_tools_preferred",
        message="Provider-hosted web tools own local web_search; model profile test-id",
        session_id="session-1",
    )

    asyncio.run(handler.on_warning(event))

    assert len(pushed) == 1
    dialog = pushed[0]
    assert isinstance(dialog, ConfirmDialog)
    assert dialog._confirm_label == "OK"
    assert dialog._cancel_label is None
    assert dialog._confirm_variant == "warning"
    assert dialog.has_class("-warning-border")
    assert notified == []
    assert debug_calls and debug_calls[0][0] == "Warning"

    # Rebuilding the same configuration must not notify twice.
    asyncio.run(handler.on_warning(event))
    assert len(pushed) == 1

    # A new chat session with the same model configuration is still the same notice.
    asyncio.run(handler.on_warning(replace(event, session_id="session-2")))
    assert len(pushed) == 1

    # A different configuration is a new notice.
    asyncio.run(handler.on_warning(replace(event, message="...; model profile other-id")))
    assert len(pushed) == 2


def test_session_in_use_error_dismisses_active_load_modal() -> None:
    """A restore ownership conflict should close loading UI before showing the standard modal."""
    from chrys.app.tui.screens.dialogs.confirm import ConfirmDialog

    pushed: list[object] = []
    debug_calls: list[tuple[str, str]] = []
    running: list[bool] = []
    loading: list[bool] = []
    status_snapshot = {"visible": True, "flash": None, "status": "Idle"}
    status_restores: list[dict[str, object]] = []

    class _FakeDialog:
        def __init__(self) -> None:
            self.dismissed = False
            self.result_calls: list[tuple[bool, str, bool]] = []

        def request_dismiss(self) -> None:
            self.dismissed = True

        def set_result(self, success: bool, message: str, allow_esc: bool = False) -> None:
            self.result_calls.append((success, message, allow_esc))

    class _FakeStatusBar:
        def restore(self, state: dict[str, object]) -> None:
            status_restores.append(state)

    def query_one(cls: object) -> object:
        if cls.__name__ == "StatusBar":
            return _FakeStatusBar()
        raise AssertionError(f"unexpected query_one({cls.__name__})")

    def push_screen(dialog: object) -> None:
        pushed.append(dialog)

    def debug(key: str, msg: str) -> None:
        debug_calls.append((key, msg))

    fake_dialog = _FakeDialog()
    restoring: list[bool] = []
    screen = SimpleNamespace(
        _state=MainScreenState(run=RunState(agent_loading=True), session=SessionViewState(restoring_session=True)),
        _set_restoring_session=restoring.append,
        app=SimpleNamespace(push_screen=push_screen),
        _set_agent_running=running.append,
        _set_agent_loading=loading.append,
        query_one=query_one,
        _debug=debug,
    )
    handler = make_backend_handler(screen)
    handler._agent_load_dialog = fake_dialog
    handler._agent_load_status_snapshot = status_snapshot

    message = f"Session '40d9a0483e08' is already open in another {APP_DISPLAY_NAME} instance (pid=47219)."
    asyncio.run(handler.on_error(Error(code="session_in_use", message=message)))

    assert screen._state.session.restoring_session is False
    assert restoring == [False]
    assert fake_dialog.dismissed is True
    assert fake_dialog.result_calls == []
    assert handler._agent_load_dialog is None
    assert loading == [False]
    assert running == [False]
    assert status_restores == [status_snapshot]
    assert len(pushed) == 1
    assert isinstance(pushed[0], ConfirmDialog)
    assert debug_calls and debug_calls[0][0] == "Error"


def test_session_fork_error_uses_notification_without_retry_mode() -> None:
    flashes: list[tuple[str, bool]] = []
    notifications: list[tuple[str, str, str]] = []
    unlocked: list[None] = []
    running: list[bool] = []
    loading: list[bool] = []
    debug_calls: list[tuple[str, str]] = []

    class _FakeStatusBar:
        def flash(self, message: str, *, error: bool = False) -> None:
            flashes.append((message, error))

    class _FakeInputBar(_RestorableDraftFake):
        locked = True
        retry_mode = False

        def unlock_and_keep(self) -> None:
            self.locked = False
            unlocked.append(None)

    input_bar = _FakeInputBar()

    def query_one(cls: type) -> object:
        if cls.__name__ == "StatusBar":
            return _FakeStatusBar()
        if cls.__name__ == "InputBar":
            return input_bar
        raise AssertionError(f"unexpected query_one({cls.__name__})")

    def notify(message: str, *, title: str, severity: str, **_kwargs: object) -> None:
        notifications.append((title, severity, message))

    restoring: list[bool] = []
    screen = SimpleNamespace(
        _state=MainScreenState(run=RunState(agent_running=True), session=SessionViewState(restoring_session=True)),
        _set_restoring_session=restoring.append,
        _set_agent_running=running.append,
        _set_agent_loading=loading.append,
        query_one=query_one,
        notify=notify,
        _debug=lambda key, value: debug_calls.append((key, value)),
    )
    screen._sessions = make_session_handler(screen)
    handler = make_backend_handler(screen)
    handler._agent_load_dialog = None

    asyncio.run(handler.on_error(Error(code="session_fork_empty", message="Cannot fork an empty session.")))

    assert screen._state.session.restoring_session is False
    assert restoring == [False]
    assert running == []
    assert loading == [False]
    assert [(status_text(message), error) for message, error in flashes] == [
        ("Fork: Cannot fork an empty session.", False)
    ]
    assert notifications == [("Fork", "warning", "Cannot fork an empty session.")]
    assert unlocked == [None]
    assert input_bar.retry_mode is False
    assert debug_calls == [("Error", "[session_fork_empty] Cannot fork an empty session.")]


def _make_screen_for_image_rejection(
    *, text: str, cwd: str | None = None
) -> tuple[SimpleNamespace, SimpleNamespace, list[object], list[bool]]:
    class _FakeInputBar(_RestorableDraftFake):
        def __init__(self) -> None:
            self.value = ""
            self.locked = True
            self.unlocked = False

        def unlock_and_keep(self) -> None:
            self.locked = False
            self.unlocked = True

    input_bar = _FakeInputBar()
    pushed: list[object] = []
    callbacks: list[object | None] = []
    running: list[bool] = []

    class _FakeApp:
        def push_screen(self, screen: object, callback: object | None = None) -> None:
            pushed.append(screen)
            callbacks.append(callback)

    def query_one(cls: object) -> object:
        if cls.__name__ == "InputBar":
            return input_bar
        raise AssertionError(f"unexpected query_one({cls.__name__})")

    state = MainScreenState() if cwd is None else main_screen_state_at(cwd)
    state.submit.begin(text)
    screen = SimpleNamespace(
        _state=state,
        app=_FakeApp(),
        _pushed_callbacks=callbacks,
        _set_agent_running=running.append,
        query_one=query_one,
        _debug=lambda *_args: None,
    )
    return screen, input_bar, pushed, running


def test_image_attachment_error_from_backend_uses_modal_and_restores_prompt() -> None:
    screen, input_bar, pushed, running = _make_screen_for_image_rejection(text="describe @shot.png")
    handler = make_backend_handler(screen)
    handler._agent_load_dialog = None

    _refuse_during_submit(
        handler,
        handler.on_error(
            Error(
                code="vision_unsupported",
                message='The active model profile "Model" does not support image input.',
            )
        ),
    )

    assert handler._state.submit.blocked is True
    assert running == []
    assert input_bar.value == "describe @shot.png"
    assert input_bar.unlocked is True
    assert len(pushed) == 1
    dialog = pushed[0]
    assert dialog._title == "Image Input Not Available"
    assert "does not support image input" in dialog._message
    assert screen._pushed_callbacks[0] is not None


def test_image_rejection_dialog_action_rewrites_image_mentions_to_paths(tmp_path: Path) -> None:
    first = tmp_path / "shot.png"
    second = tmp_path / "screen two.jpg"
    text = f'inspect @{first.name} and @"{second}" but keep @notes.txt'
    screen, input_bar, pushed, running = _make_screen_for_image_rejection(text=text, cwd=str(tmp_path))
    handler = make_backend_handler(screen)
    handler._agent_load_dialog = None

    _refuse_during_submit(
        handler,
        handler.on_error(
            Error(
                code="vision_unsupported",
                message='The active model profile "Model" does not support image input.',
            )
        ),
    )

    from chrys.app.tui.screens.dialogs.vision_unsupported import USE_IMAGE_PATHS_RESULT

    callback = screen._pushed_callbacks[0]
    assert callback is not None
    callback(USE_IMAGE_PATHS_RESULT)

    assert running == []
    assert input_bar.value == f"inspect {first} and {second} but keep @notes.txt"
    assert len(pushed) == 1
    dialog = pushed[0]
    assert dialog._show_path_action is True


def test_image_attachment_timeout_error_uses_modal_and_restores_prompt() -> None:
    screen, input_bar, pushed, running = _make_screen_for_image_rejection(text="describe @huge.png")
    handler = make_backend_handler(screen)
    handler._agent_load_dialog = None
    message = (
        "We couldn't attach this image.\n\n"
        "- @huge.png: Image preparation took longer than 1 second. "
        "Resize oversized images or send fewer images, then try again."
    )

    _refuse_during_submit(handler, handler.on_error(Error(code="image_attachment_error", message=message)))

    assert handler._state.submit.blocked is True
    assert running == []
    assert input_bar.value == "describe @huge.png"
    assert input_bar.unlocked is True
    assert len(pushed) == 1
    dialog = pushed[0]
    assert dialog._title == "Image Not Attached"
    assert "Image preparation took longer than 1 second" in dialog._message


def _refuse_during_submit(handler: BackendEventHandler, delivery: Coroutine[Any, Any, None]) -> None:
    """Deliver *delivery* as the backend refuses a submit: inside the submit's own publish."""

    async def publish() -> None:
        handler._state.submit.publish_from_current_task()
        await delivery

    asyncio.run(publish())


def _blocked_prompt_state() -> MainScreenState:
    """A submit still being admitted while its user bubble renders."""
    state = MainScreenState()
    state.submit.begin("blocked prompt")
    state.render_gate.begin()
    return state


def test_pending_submit_error_restores_prompt_without_inline_retry_action() -> None:
    class _FakeInputBar(_RestorableDraftFake):
        def __init__(self) -> None:
            self.value = ""
            self.locked = True
            self.unlocked = False

        def unlock_and_keep(self) -> None:
            self.locked = False
            self.unlocked = True

    class _FakeStatusBar:
        def flash(self, _message: str, *, error: bool = False) -> None:
            assert error is True

    class _FakeChatPanel:
        async def add_error(self, message: str, *, action_label: str | None = "Retry") -> None:
            chat_errors.append((message, action_label))

    input_bar = _FakeInputBar()
    chat_errors: list[tuple[str, str | None]] = []
    running: list[bool] = []

    def query_one(cls: object) -> object:
        if cls.__name__ == "InputBar":
            return input_bar
        if cls.__name__ == "StatusBar":
            return _FakeStatusBar()
        if cls.__name__ == "ChatPanel":
            return _FakeChatPanel()
        raise AssertionError(f"unexpected query_one({cls.__name__})")

    screen = SimpleNamespace(
        _state=_blocked_prompt_state(),
        _set_agent_running=running.append,
        query_one=query_one,
        _debug=lambda *_args: None,
    )
    handler = make_backend_handler(screen)
    handler._agent_load_dialog = None

    _refuse_during_submit(handler, handler.on_error(Error(code="hook_blocked", message="Prompt denied")))

    assert handler._state.submit.blocked is True
    assert running == []
    assert input_bar.value == "blocked prompt"
    assert input_bar.unlocked is True
    assert chat_errors == [("Prompt denied", None)]


def test_pending_submit_error_display_localizes_chat_but_debug_keeps_protocol_english() -> None:
    from chrys.orchestration.engine.run.turn_hooks import _TURN_HOOKS_PROMPT_BLOCKED

    class _FakeInputBar(_RestorableDraftFake):
        def __init__(self) -> None:
            self.value = ""
            self.locked = True

        def unlock_and_keep(self) -> None:
            self.locked = False

    class _FakeStatusBar:
        def flash(self, message: MessageRef | str, *, error: bool = False) -> None:
            flashes.append(message)

    class _FakeChatPanel:
        async def add_error(self, message: str, *, action_label: str | None = "Retry") -> None:
            chat_errors.append((message, action_label))

    input_bar = _FakeInputBar()
    flashes: list[MessageRef | str] = []
    chat_errors: list[tuple[str, str | None]] = []
    debug_calls: list[tuple[str, str]] = []

    def query_one(cls: object) -> object:
        if cls.__name__ == "InputBar":
            return input_bar
        if cls.__name__ == "StatusBar":
            return _FakeStatusBar()
        if cls.__name__ == "ChatPanel":
            return _FakeChatPanel()
        raise AssertionError(f"unexpected query_one({cls.__name__})")

    screen = SimpleNamespace(
        _state=_blocked_prompt_state(),
        query_one=query_one,
        _debug=lambda key, msg: debug_calls.append((key, msg)),
    )
    handler = make_backend_handler(screen, locale_controller=LocaleController(Settings(locale="zh-Hans")))
    handler._agent_load_dialog = None

    _refuse_during_submit(
        handler,
        handler.on_error(
            Error(
                code="hook_blocked",
                message="Prompt blocked by hook.",
                display_message=_TURN_HOOKS_PROMPT_BLOCKED.bind(),
            )
        ),
    )

    assert chat_errors == [("提示已被钩子阻止。", None)]
    chinese = Localizer("zh-Hans")
    assert [chinese.render(message) for message in flashes] == ["错误：提示已被钩子阻止。"]  # noqa: RUF001
    assert debug_calls == [("Error", "[hook_blocked] Prompt blocked by hook.")]


@pytest.mark.parametrize("error_code", ["executor_error", "retry_missing_user_anchor"])
@pytest.mark.parametrize("pending_submit", [False, True], ids=["running", "submitting"])
def test_prior_run_error_cannot_interrupt_newer_run(error_code: str, pending_submit: bool) -> None:
    """A rejection delivered after a newer run starts must retain stale-error protection."""
    event = Error(code=error_code, message="old failure")
    state = MainScreenState()
    state.run.agent_running = True
    state.run.started_at = event.timestamp + timedelta(seconds=1)
    if pending_submit:
        state.submit.begin("newer input")

    def query_one(_cls: object) -> object:
        raise AssertionError("stale errors must not touch the current run's widgets")

    screen = SimpleNamespace(
        _state=state,
        query_one=query_one,
        _debug=lambda *_args: None,
        _session_title=fake_session_title(),
    )
    handler = make_backend_handler(screen)

    asyncio.run(handler.on_error(event))

    assert state.run.agent_running is True
    assert state.submit.active is pending_submit
    assert state.submit.blocked is False


def test_backend_handler_defers_error_while_user_bubble_is_rendering() -> None:
    state = MainScreenState()
    state.render_gate.begin()
    state.run.agent_running = True
    state.submit.begin("hello")

    def query_one(_cls: object) -> object:
        raise AssertionError("error should be deferred before querying live widgets")

    screen = SimpleNamespace(
        _state=state,
        query_one=query_one,
    )
    handler = make_backend_handler(screen)
    event = Error(code="executor_error", message="fast failure")

    asyncio.run(handler.on_error(event))

    assert state.run.agent_running is True
    assert state.submit.blocked is False
    replayed: list[object] = []

    async def replay(deferred: object) -> None:
        replayed.append(deferred)

    asyncio.run(state.render_gate.drain(replay))
    assert replayed == [event]


def test_live_turn_error_requests_terminal_absorb_after_render() -> None:
    order: list[str] = []

    class _GcMessages(list[object]):
        def append(self, message: object) -> None:
            order.append("gc")
            super().append(message)

    class _FakeStatusBar:
        def flash(self, _message: str, *, error: bool = False) -> None:
            assert error is True
            order.append("status")

    class _FakeInputBar(_RestorableDraftFake):
        locked = True
        retry_mode = False
        _retry_label = ""

        def unlock_and_keep(self) -> None:
            self.locked = False
            order.append("unlock")

    class _FakeChatPanel:
        async def add_error(self, _message: str, *, action_label: str | None = "Retry") -> None:
            assert action_label == "Retry"
            assert handler._state.run.agent_running is True
            assert input_bar.locked is True
            order.append("render")

    status = _FakeStatusBar()
    input_bar = _FakeInputBar()
    panel = _FakeChatPanel()

    def query_one(cls: type) -> object:
        if cls.__name__ == "StatusBar":
            return status
        if cls.__name__ == "InputBar":
            return input_bar
        if cls.__name__ == "ChatPanel":
            return panel
        raise AssertionError(f"unexpected query_one({cls.__name__})")

    gc_messages = _GcMessages()
    screen = SimpleNamespace(
        _state=MainScreenState(run=RunState(agent_running=True)),
        _gc_messages=gc_messages,
        _session_title=fake_session_title(mark_terminal_title_failed=lambda: order.append("failed")),
        _set_agent_running=lambda _value: order.append("idle"),
        query_one=query_one,
        _debug=lambda *_args: None,
    )

    handler = make_backend_handler(screen)
    asyncio.run(handler.on_error(Error(code="executor_error", message="failed")))

    assert order == ["status", "render", "failed", "idle", "unlock", "gc"]
    assert len(gc_messages) == 1
    assert isinstance(gc_messages[0], GcAbsorbRequested)
    assert gc_messages[0].reason is GcAbsorbReason.TURN_TERMINAL
    assert gc_messages[0].terminal_boundary is True


def test_live_turn_error_render_failure_releases_input_without_absorb() -> None:
    class _FakeStatusBar:
        def flash(self, _message: str, *, error: bool = False) -> None:
            assert error is True

    class _FakeInputBar(_RestorableDraftFake):
        locked = True
        retry_mode = False
        _retry_label = ""

        def unlock_and_keep(self) -> None:
            self.locked = False

    class _FailingChatPanel:
        async def add_error(self, _message: str, *, action_label: str | None = "Retry") -> None:
            assert action_label == "Retry"
            assert handler._state.run.agent_running is True
            raise RuntimeError("render failed")

    status = _FakeStatusBar()
    input_bar = _FakeInputBar()
    panel = _FailingChatPanel()

    def query_one(cls: type) -> object:
        if cls.__name__ == "StatusBar":
            return status
        if cls.__name__ == "InputBar":
            return input_bar
        if cls.__name__ == "ChatPanel":
            return panel
        raise AssertionError(f"unexpected query_one({cls.__name__})")

    running: list[bool] = []
    gc_messages: list[object] = []
    screen = SimpleNamespace(
        _state=MainScreenState(run=RunState(agent_running=True)),
        _gc_messages=gc_messages,
        _session_title=fake_session_title(),
        _set_agent_running=running.append,
        query_one=query_one,
        _debug=lambda *_args: None,
    )
    handler = make_backend_handler(screen)

    with pytest.raises(RuntimeError, match="render failed"):
        asyncio.run(handler.on_error(Error(code="executor_error", message="failed")))

    assert running == [False]
    assert handler._state.run.agent_running is False
    assert input_bar.locked is False
    assert gc_messages == []
    assert input_bar.retry_mode is False


def test_image_attachment_warning_from_backend_uses_modal_and_restores_prompt() -> None:
    screen, input_bar, pushed, running = _make_screen_for_image_rejection(text="continue with @shot.png")
    handler = make_backend_handler(screen)
    message = "Images cannot be attached to retry or continuation prompts."
    handler._seen_warnings = {
        _WarningDedupeKey(
            session_id=None,
            code="image_attachment_retry_unsupported",
            message=message,
        )
    }

    _refuse_during_submit(
        handler,
        handler.on_warning(
            Warning(
                code="image_attachment_retry_unsupported",
                message=message,
            )
        ),
    )

    assert handler._state.submit.blocked is True
    assert running == []
    assert input_bar.value == "continue with @shot.png"
    assert input_bar.unlocked is True
    assert len(pushed) == 1
    dialog = pushed[0]
    assert dialog._title == "Image Not Attached"
    assert "retry or continuation prompts" in dialog._message


def test_submit_blocking_warning_restores_prompt_without_modal() -> None:
    class _FakeInputBar(_RestorableDraftFake):
        def __init__(self) -> None:
            self.value = ""
            self.locked = True
            self.unlocked = False

        def unlock_and_keep(self) -> None:
            self.locked = False
            self.unlocked = True

    input_bar = _FakeInputBar()
    notifications: list[tuple[str, str, str]] = []
    pushed: list[object] = []

    def query_one(cls: object) -> object:
        if cls.__name__ == "InputBar":
            return input_bar
        raise AssertionError(f"unexpected query_one({cls.__name__})")

    def notify(message: str, *, title: str, severity: str, **_kwargs: object) -> None:
        notifications.append((message, title, severity))

    state = MainScreenState()
    state.submit.begin("retry after the other agent finishes")
    screen = SimpleNamespace(
        _state=state,
        app=SimpleNamespace(push_screen=pushed.append),
        notify=notify,
        query_one=query_one,
        _debug=lambda *_args: None,
    )
    handler = make_backend_handler(screen)
    handler._seen_warnings = set()

    _refuse_during_submit(
        handler,
        handler.on_warning(
            Warning(
                code="sub_agent_paused",
                message="Resolve paused sub-agent(s) first.",
            )
        ),
    )

    assert handler._state.submit.blocked is True
    assert input_bar.value == "retry after the other agent finishes"
    assert input_bar.unlocked is True
    assert pushed == []
    assert notifications == [("Resolve paused sub-agent(s) first.", "Warning", "warning")]


def test_error_event_empty_message_uses_code_fallback() -> None:
    """Blank backend error messages should not render as an empty chat/status error."""
    flashes: list[tuple[str, bool]] = []
    chat_errors: list[str] = []
    running: list[bool] = []
    unlocked: list[bool] = []
    debug_calls: list[tuple[str, str]] = []

    class _FakeStatusBar:
        def flash(self, message: str, *, error: bool = False) -> None:
            flashes.append((message, error))

    class _FakeInputBar(_RestorableDraftFake):
        def __init__(self) -> None:
            self.locked = True
            self._retry_label = ""
            self.retry_mode = False

        def unlock_and_keep(self) -> None:
            unlocked.append(True)
            self.locked = False

    class _FakeChatPanel:
        async def add_error(self, message: str, *, action_label: str | None = "Retry") -> None:
            assert action_label == "Retry"
            chat_errors.append(message)

    status = _FakeStatusBar()
    input_bar = _FakeInputBar()
    panel = _FakeChatPanel()

    def query_one(cls: type):
        if cls.__name__ == "StatusBar":
            return status
        if cls.__name__ == "InputBar":
            return input_bar
        if cls.__name__ == "ChatPanel":
            return panel
        raise AssertionError(f"unexpected query_one({cls})")

    def set_agent_running(value: bool) -> None:
        running.append(value)

    def debug(key: str, msg: str) -> None:
        debug_calls.append((key, msg))

    restoring: list[bool] = []
    screen = SimpleNamespace(
        _state=MainScreenState(session=SessionViewState(restoring_session=True)),
        _set_restoring_session=restoring.append,
        _set_agent_running=set_agent_running,
        query_one=query_one,
        _debug=debug,
    )
    handler = make_backend_handler(screen)
    handler._agent_load_dialog = None

    asyncio.run(handler.on_error(Error(code="executor_error", message="")))

    assert screen._state.session.restoring_session is False
    assert restoring == [False]
    assert running == [False]
    assert [(status_text(message), error) for message, error in flashes] == [("Error: Executor error", True)]
    assert chat_errors == ["Executor error"]
    assert unlocked == [True]
    assert isinstance(input_bar._retry_label, MessageRef)
    assert status_text(input_bar._retry_label) == "Retry"
    assert input_bar.retry_mode is True
    assert debug_calls == [("Error", "[executor_error] Executor error")]


def test_error_display_message_localizes_ui_surfaces_and_keeps_raw_debug() -> None:
    """Errors carrying a display reference show it on the status bar and in
    the chat panel; debug lines keep the raw protocol message."""
    flashes: list[tuple[MessageRef | str, bool]] = []
    chat_errors: list[str] = []
    debug_calls: list[tuple[str, str]] = []

    class _FakeStatusBar:
        def flash(self, message: MessageRef | str, *, error: bool = False) -> None:
            flashes.append((message, error))

    class _FakeInputBar(_RestorableDraftFake):
        def __init__(self) -> None:
            self.locked = True
            self._retry_label = ""
            self.retry_mode = False

        def unlock_and_keep(self) -> None:
            self.locked = False

    class _FakeChatPanel:
        async def add_error(self, message: str, *, action_label: str | None = "Retry") -> None:
            chat_errors.append(message)

    status = _FakeStatusBar()
    input_bar = _FakeInputBar()
    panel = _FakeChatPanel()

    def query_one(cls: type):
        if cls.__name__ == "StatusBar":
            return status
        if cls.__name__ == "InputBar":
            return input_bar
        if cls.__name__ == "ChatPanel":
            return panel
        raise AssertionError(f"unexpected query_one({cls})")

    screen = SimpleNamespace(
        query_one=query_one,
        _debug=lambda key, msg: debug_calls.append((key, msg)),
    )
    handler = make_backend_handler(screen, locale_controller=LocaleController(Settings(locale="zh-Hans")))
    handler._agent_load_dialog = None

    asyncio.run(
        handler.on_error(Error(code="run_failed", message="Unknown error", display_message=_UNKNOWN_ERROR.bind()))
    )

    chinese = Localizer("zh-Hans")
    assert [(chinese.render(message), error) for message, error in flashes] == [("错误：未知错误", True)]  # noqa: RUF001
    assert chat_errors == ["未知错误"]
    assert debug_calls == [("Error", "[run_failed] Unknown error")]


class _RestorableDraftFake:
    """Mirrors ``InputBar.restore_draft`` for the composer fakes in this module.

    The hand-back writes only into an empty composer, so a draft that landed
    while the rejection was in flight survives — the invariant these tests are
    here to pin, not an implementation detail of the fakes.
    """

    value = ""
    draft_owner: ShortcutIntent | None = None

    def restore_draft(self, draft: str | ChatDraft) -> bool:
        text = draft.text if isinstance(draft, ChatDraft) else draft
        if not text or self.value:
            return False
        self.value = text
        return True

    def replace_draft(self, text: str) -> None:
        self.value = text

    def set_draft(self, draft: ChatDraft) -> None:
        self.value = draft.text
        self.draft_owner = draft.owner


@dataclass
class _AgentLoadErrorProbe:
    """Surfaces an error raised while the agent is still loading must reach."""

    flashes: list[str] = field(default_factory=list)
    chat_errors: list[str] = field(default_factory=list)
    loading: list[bool] = field(default_factory=list)
    running: list[bool] = field(default_factory=list)


def _make_agent_load_error_handler(dialog: object) -> tuple[BackendEventHandler, _AgentLoadErrorProbe]:
    """Wire a handler that is mid agent-load with ``dialog`` as its load dialog."""
    probe = _AgentLoadErrorProbe()

    class _FakeStatusBar:
        def flash(self, message: str, *, error: bool = False) -> None:
            probe.flashes.append(message)
            assert error is True

    class _FakeInputBar(_RestorableDraftFake):
        locked = False
        _retry_label = ""
        retry_mode = False

    class _FakeChatPanel:
        async def add_error(self, message: str, *, action_label: str | None = "Retry") -> None:
            assert action_label == "Retry"
            probe.chat_errors.append(message)

    def query_one(cls: type):
        if cls.__name__ == "StatusBar":
            return _FakeStatusBar()
        if cls.__name__ == "InputBar":
            return _FakeInputBar()
        if cls.__name__ == "ChatPanel":
            return _FakeChatPanel()
        raise AssertionError(f"unexpected query_one({cls})")

    screen = SimpleNamespace(
        _state=MainScreenState(run=RunState(agent_loading=True), session=SessionViewState(restoring_session=True)),
        _set_agent_loading=probe.loading.append,
        _set_agent_running=probe.running.append,
        query_one=query_one,
        _debug=lambda *_args: None,
    )
    handler = make_backend_handler(screen)
    handler._agent_load_dialog = dialog
    return handler, probe


def test_error_event_empty_message_during_agent_loading_uses_load_fallback() -> None:
    """Startup fallback errors should preserve the agent-load specific message."""
    dialog_results: list[tuple[bool, str, bool]] = []

    class _FakeDialog:
        def set_result(self, success: bool, message: str, *, allow_esc: bool = False) -> None:
            dialog_results.append((success, message, allow_esc))

    handler, probe = _make_agent_load_error_handler(_FakeDialog())
    handler._agent_load_status_snapshot = {"visible": True, "flash": None, "status": "Before load"}

    asyncio.run(handler.on_error(Error(code="executor_error", message="   ")))

    assert dialog_results == [(False, "Agent failed to load.", True)]
    assert handler._agent_load_status_snapshot is None
    assert probe.loading == [False]
    assert probe.running == [False]
    assert [status_text(message) for message in probe.flashes] == ["Error: Agent failed to load."]
    assert probe.chat_errors == ["Agent failed to load."]


def test_error_event_during_agent_loading_clears_loading_if_dialog_update_fails() -> None:
    """A stale loading dialog must not prevent the input bar from unlocking."""

    class _BrokenDialog:
        def set_result(self, *_args: object, **_kwargs: object) -> None:
            raise RuntimeError("dialog already gone")

    handler, probe = _make_agent_load_error_handler(_BrokenDialog())

    asyncio.run(handler.on_error(Error(code="executor_error", message="   ")))

    assert handler._agent_load_dialog is None
    assert probe.loading == [False]
    assert probe.running == [False]
    assert [status_text(message) for message in probe.flashes] == ["Error: Agent failed to load."]
    assert probe.chat_errors == ["Agent failed to load."]
