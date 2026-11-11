# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The TUI shows what a failure means in the current locale, with the raw text kept beside it."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest
from anthropic import APIStatusError

from chrys.app.tui.i18n import LocaleController
from chrys.app.tui.screens.main.event_handlers import BackendEventHandler
from chrys.app.tui.screens.main.state import MainScreenState, RunState
from chrys.foundation.branding import APP_DISPLAY_NAME
from chrys.foundation.config.settings import Settings
from chrys.foundation.errors import ROUTE_EXTENSION_KEY, Origin, RouteFacts, clean_error_message
from chrys.foundation.errors.display import _DNS_FAILED, _MAYBE_OFFLINE, display_fields
from chrys.foundation.events.types import (
    AgentLoadFailed,
    Error,
    InvocationOrigin,
    InvocationPaused,
    InvocationRetryAttempt,
)
from chrys.foundation.i18n import Localizer, MessageRef
from tests.support.provider_errors import anthropic_status
from tests.support.tui_helpers import fake_session_title, make_backend_handler

_RAW = "Connection error."
_DNS = _DNS_FAILED.bind(host="api.example.com")
_HINT = _MAYBE_OFFLINE.bind(app=APP_DISPLAY_NAME)
_TURN = InvocationOrigin("turn", "", "turn-1", None)
_CHILD = InvocationOrigin("sub_agent", "Explore", "inv-1", _TURN)


def _joined(locale: str, *, separator: str) -> str:
    localizer = Localizer(locale)
    return f"{localizer.render(_DNS)}{separator}{localizer.render(_HINT)}"


@dataclass
class _Surfaces:
    """What each TUI surface was asked to show."""

    status: list[MessageRef | str] = field(default_factory=list)
    chat_errors: list[str] = field(default_factory=list)
    retry_banners: list[str] = field(default_factory=list)
    sub_agent_retries: list[str] = field(default_factory=list)
    sub_agent_pauses: list[tuple[Any, ...]] = field(default_factory=list)
    debug: list[tuple[str, str]] = field(default_factory=list)


def _handler(
    locale_controller: LocaleController, *, running: bool = False, loading: bool = False
) -> tuple[BackendEventHandler, _Surfaces]:
    """A handler over fake widgets; *running* is whether a turn is live (retry and pause events need one)."""
    surfaces = _Surfaces()

    class _FakeStatusBar:
        def flash(self, message: MessageRef | str, *, error: bool = False) -> None:
            assert error is True
            surfaces.status.append(message)

        def show(self, _message: MessageRef | str) -> None:
            return

    class _FakeInputBar:
        locked = True
        _retry_label = ""
        retry_mode = False
        value = ""

        def unlock_and_keep(self) -> None:
            self.locked = False

        def restore_draft(self, _text: str) -> bool:
            return False

    class _FakeChatPanel:
        async def add_error(self, message: str, *, action_label: str | None = "Retry") -> None:
            surfaces.chat_errors.append(message)

        async def prepare_retry(self) -> None:
            return

        async def add_retry(
            self,
            message: str,
            _attempt: int,
            _max_attempts: int,
            _delay_seconds: int,
            *,
            deadline: float | None = None,
            wait_id: str = "",
        ) -> None:
            surfaces.retry_banners.append(message)

        def sub_agent_retry_attempt(
            self, _invocation_id: str, message: str, _attempt: int, _max_attempts: int, _delay_seconds: int
        ) -> None:
            surfaces.sub_agent_retries.append(message)

        def sub_agent_paused(self, *args: Any) -> None:
            surfaces.sub_agent_pauses.append(args)

    status = _FakeStatusBar()
    input_bar = _FakeInputBar()
    panel = _FakeChatPanel()
    widgets = {"StatusBar": status, "InputBar": input_bar, "ChatPanel": panel}

    screen = SimpleNamespace(
        _state=MainScreenState(run=RunState(agent_running=running, agent_loading=loading)),
        query_one=lambda cls: widgets[cls.__name__],
        _debug=lambda key, message: surfaces.debug.append((key, message)),
        _session_title=fake_session_title(),
    )
    handler = make_backend_handler(screen, locale_controller=locale_controller)
    handler._agent_load_dialog = None
    return handler, surfaces


def _turn_error() -> Error:
    return Error(code="executor_error", message=_RAW, display_message=_DNS, display_hint=_HINT)


def test_a_turn_error_says_what_went_wrong_then_the_raw_text() -> None:
    handler, surfaces = _handler(LocaleController(Settings(locale="en")))

    asyncio.run(handler.on_error(_turn_error()))

    shown = _joined("en", separator=" ")
    assert surfaces.chat_errors == [f"{shown}\n{_RAW}"]
    # The status bar has one line: the meaning alone, without the hint.
    assert [Localizer("en").render(message) for message in surfaces.status] == [
        f"Error: {Localizer('en').render(_DNS)}"
    ]
    assert surfaces.debug == [("Error", f"[executor_error] {_RAW}")]


def test_other_errors_with_a_display_still_show_it_alone() -> None:
    handler, surfaces = _handler(LocaleController(Settings(locale="en")))

    asyncio.run(handler.on_error(Error(code="run_failed", message=_RAW, display_message=_DNS, display_hint=_HINT)))

    assert surfaces.chat_errors == [_joined("en", separator=" ")]


@pytest.mark.parametrize("locale", ["en", "zh-Hans"])
async def test_gateway_content_filter_shows_localized_reason_and_raw_verdict(locale: str) -> None:
    handler, surfaces = _handler(LocaleController(Settings(locale=locale)))
    error = await anthropic_status(500, {"error": {"type": "new_api_error", "message": "sensitive_words_detected"}})
    # The production HTTP client stamps the model route on the SDK request.
    assert isinstance(error, APIStatusError)
    error.request.extensions[ROUTE_EXTENSION_KEY] = RouteFacts(
        Origin("https", "api.example.test", 443), None, first_hop_reached=True
    )
    display, hint = display_fields(error)
    assert display is not None
    assert display.definition.key == "error.kind.content_filtered"

    await handler.on_error(
        Error(code="executor_error", message=clean_error_message(error), display_message=display, display_hint=hint)
    )

    assert surfaces.chat_errors == [f"{Localizer(locale).render(display)}\nError code: 500 - sensitive_words_detected"]
    assert surfaces.retry_banners == []


def test_error_display_follows_the_current_locale() -> None:
    controller = LocaleController(Settings(locale="zh-Hans"))
    handler, surfaces = _handler(controller)

    asyncio.run(handler.on_error(_turn_error()))
    controller.switch_locale("en")
    asyncio.run(handler.on_error(_turn_error()))

    chinese = _joined("zh-Hans", separator="")
    assert chinese != _joined("en", separator="")
    # zh-Hans joins without a space; a bubble already shown keeps its words.
    assert surfaces.chat_errors == [f"{chinese}\n{_RAW}", f"{_joined('en', separator=' ')}\n{_RAW}"]


def test_retry_banners_say_what_went_wrong() -> None:
    handler, surfaces = _handler(LocaleController(Settings(locale="en")), running=True)

    for origin in (_TURN, _CHILD):
        event = InvocationRetryAttempt(
            message=_RAW,
            attempt=1,
            max_attempts=7,
            delay_seconds=3,
            display_message=_DNS,
            display_hint=_HINT,
            origin=origin,
        )
        asyncio.run(handler.on_retry_attempt(event) if origin is _TURN else handler.on_sub_agent_retry_attempt(event))

    shown = _joined("en", separator=" ")
    assert (surfaces.retry_banners, surfaces.sub_agent_retries) == ([shown], [shown])


def test_a_sub_agent_retry_without_a_display_keeps_the_raw_text() -> None:
    handler, surfaces = _handler(LocaleController(Settings(locale="en")), running=True)

    event = InvocationRetryAttempt(message=_RAW, attempt=1, max_attempts=7, delay_seconds=3, origin=_CHILD)
    asyncio.run(handler.on_sub_agent_retry_attempt(event))

    assert surfaces.sub_agent_retries == [_RAW]


def test_a_paused_sub_agent_card_gets_the_rendered_display() -> None:
    handler, surfaces = _handler(LocaleController(Settings(locale="zh-Hans")), running=True)

    for display, hint in ((_DNS, _HINT), (None, None)):
        paused = InvocationPaused(
            reason="framework_exc",
            last_error=_RAW,
            retry_attempts=0,
            last_error_display=display,
            last_error_hint=hint,
            origin=_CHILD,
        )
        asyncio.run(handler.on_sub_agent_paused(paused))

    assert surfaces.sub_agent_pauses == [
        ("inv-1", "framework_exc", _RAW, 0, None, _joined("zh-Hans", separator="")),
        ("inv-1", "framework_exc", _RAW, 0, None, None),
    ]


def test_a_turn_error_while_an_agent_loads_shows_the_load_dialog_both() -> None:
    handler, surfaces = _handler(LocaleController(Settings(locale="en")), loading=True)
    failed: list[Any] = []
    handler._agent_load().fail = failed.append  # type: ignore[method-assign]

    asyncio.run(handler.on_error(_turn_error()))
    asyncio.run(handler.on_error(Error(code="run_failed", message=_RAW, display_message=_DNS)))

    # As in the chat: what went wrong, then the raw text; other producers' display stands alone.
    shown = _joined("en", separator=" ")
    assert failed == [f"{shown}\n{_RAW}", Localizer("en").render(_DNS)]
    assert surfaces.chat_errors[0] == f"{shown}\n{_RAW}"


def test_an_agent_load_failure_passes_the_rendered_display() -> None:
    handler, _surfaces = _handler(LocaleController(Settings(locale="en")))
    received: list[tuple[str | None, str | None]] = []

    def on_failed(_event: AgentLoadFailed, *, display: str | None = None, summary: str | None = None) -> None:
        received.append((display, summary))

    handler._agent_load().on_failed = on_failed  # type: ignore[method-assign]
    failures = (
        AgentLoadFailed(
            operation="startup", agent_profile="Code", message=_RAW, display_message=_DNS, display_hint=_HINT
        ),
        AgentLoadFailed(operation="startup", agent_profile="Code", message=_RAW),
    )
    for failure in failures:
        asyncio.run(handler.on_agent_load_failed(failure))

    # The load dialog gets the hint; the status bar's one line gets the message alone.
    assert received == [(_joined("en", separator=" "), Localizer("en").render(_DNS)), (None, None)]
