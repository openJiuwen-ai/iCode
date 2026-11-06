# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The main chat's retry notice counts the wait down and offers Retry Now; nested transcripts show the wait they were told."""

from __future__ import annotations

import time
from pathlib import Path
from types import ModuleType

import pytest
from textual.app import App, ComposeResult
from textual.screen import Screen
from textual.widgets import Button, Static

import chrys.app.tui.widgets.chat.messages as messages_module
from chrys.app.tui.i18n import LocaleController
from chrys.app.tui.theme import TuiVariableDefaultsMixin
from chrys.app.tui.widgets.chat.messages import ErrorMessage, RetryMessage, RetryNowClicked
from chrys.app.tui.widgets.chat.panel import ChatPanel
from chrys.app.tui.widgets.chrome.input_bar import InputBar
from chrys.foundation.config.settings import Settings
from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import InvocationOrigin, InvocationRetryAttempt, RetryWaitSkip, SessionReady
from tests.support.event_capture import capture_events
from tests.support.tui_app_harness import make_chrys_app
from tests.support.tui_helpers import click_when_settled, resize_when_settled
from tests.support.waiting import MAIN_SCREEN_BUILD_TIMEOUT, wait_for, wait_until


class _Clock:
    """``time`` as the messages module reads it, with a monotonic clock the test moves."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.module = ModuleType("time")
        self.module.__dict__.update(time.__dict__)
        self.module.monotonic = lambda: self.now  # type: ignore[attr-defined]


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    fake = _Clock()
    monkeypatch.setattr(messages_module, "time", fake.module)
    return fake


class _Host(TuiVariableDefaultsMixin, App[None]):
    def __init__(self, retry: RetryMessage, *, locale: str = "en") -> None:
        self.locale_controller = LocaleController(Settings(locale=locale))
        super().__init__()
        self._retry = retry
        self.clicked: list[str] = []

    def compose(self) -> ComposeResult:
        yield self._retry

    def on_retry_now_clicked(self, event: RetryNowClicked) -> None:
        self.clicked.append(event.wait_id)


def _countdown(retry: RetryMessage) -> str:
    return str(retry.query_one(".retry-countdown", Static).render())


def _body(retry: RetryMessage) -> str:
    return str(retry.query_one(".status-body", Static).render())


async def test_the_notice_counts_the_wait_down_then_says_the_retry_began(clock: _Clock) -> None:
    retry = RetryMessage("Connection error.", 2, 10, 7, deadline=clock.now + 7, wait_id="w1")
    async with _Host(retry).run_test() as pilot:
        assert _body(retry) == "✗ Error\nConnection error."
        assert _countdown(retry) == "Retrying in 7s (2/10)"
        button = retry.query_one(".retry-now-btn", Button)
        assert button.display

        clock.now += 1.5
        await wait_for(lambda: _countdown(retry) == "Retrying in 6s (2/10)", pilot=pilot, description="6s left")
        clock.now += 1
        await wait_for(lambda: _countdown(retry) == "Retrying in 5s (2/10)", pilot=pilot, description="5s left")
        clock.now += 10
        await wait_for(lambda: retry.retry_started, pilot=pilot, description="the wait ran out")

        assert _countdown(retry) == "Retrying (2/10)..."
        assert not button.display
        assert pilot.app.clicked == []


class _TickCountingRetry(RetryMessage):
    """Counts the card's ticks, painted or not."""

    ticks = 0

    def _tick(self) -> None:
        self.ticks += 1
        super()._tick()


async def test_a_covered_notice_paints_the_time_left_only_once_it_shows_again(clock: _Clock) -> None:
    retry = _TickCountingRetry("Connection error.", 2, 10, 7, deadline=clock.now + 7, wait_id="w1")
    async with _Host(retry).run_test() as pilot:
        cover = Screen()
        await pilot.app.push_screen(cover)
        await wait_for(lambda: pilot.app.screen is cover and cover.is_mounted, pilot=pilot, description="covered")

        clock.now += 1.5
        await wait_for(lambda: retry.ticks >= 1, pilot=pilot, description="a tick under the cover")
        assert _countdown(retry) == "Retrying in 7s (2/10)"

        pilot.app.pop_screen()
        await wait_for(
            lambda: _countdown(retry) == "Retrying in 6s (2/10)", pilot=pilot, description="uncovered: 6s left"
        )


async def test_the_button_stays_put_as_the_seconds_lose_a_digit(clock: _Clock) -> None:
    retry = RetryMessage("Connection error.", 2, 10, 10, deadline=clock.now + 10, wait_id="w1")
    async with _Host(retry).run_test() as pilot:
        button = retry.query_one(".retry-now-btn", Button)
        await wait_for(lambda: button.region.width > 0, pilot=pilot, description="the button laid out")
        x = button.region.x

        clock.now += 1.5
        await wait_for(lambda: _countdown(retry) == "Retrying in 9s (2/10)", pilot=pilot, description="9s left")

        assert not await wait_until(lambda: button.region.x != x, pilot=pilot, timeout=0.3)


async def test_retry_now_moves_under_the_countdown_when_the_card_is_too_narrow_for_both(clock: _Clock) -> None:
    retry = RetryMessage("Connection error.", 2, 10, 7, deadline=clock.now + 7, wait_id="w1")
    async with _Host(retry).run_test(size=(80, 12)) as pilot:
        label = retry.query_one(".retry-countdown", Static)
        button = retry.query_one(".retry-now-btn", Button)

        def placed(*, beside: bool) -> bool:
            content = retry.content_region
            whole = content.contains_region(button.region) and content.contains_region(label.region)
            row = button.region.y == label.region.y if beside else button.region.y == label.region.bottom
            return button.region.width > 0 and whole and row

        await wait_for(lambda: placed(beside=True), pilot=pilot, description="Retry Now beside the countdown")
        # About the card's width in the main chat of an 80-column terminal with the sidebar open.
        await resize_when_settled(pilot, 34, 12)
        await wait_for(lambda: placed(beside=False), pilot=pilot, description="Retry Now under the countdown")
        await resize_when_settled(pilot, 80, 12)
        await wait_for(lambda: placed(beside=True), pilot=pilot, description="Retry Now beside it again")


async def test_retry_now_looks_like_the_other_status_buttons_in_the_ansi_theme(tmp_path: Path) -> None:
    app = make_chrys_app(tmp_path / "sessions")
    async with app.run_test(size=(120, 40)) as pilot:
        main = app._main_screen
        assert main is not None
        app.theme = "chrys-ansi"
        retry = RetryMessage("Connection error.", 2, 10, 600, deadline=time.monotonic() + 600, wait_id="w1")
        error = ErrorMessage("Connection error.", action_label="Retry")
        await main.mount(retry, error)
        retry_now = retry.query_one(".retry-now-btn", Button)
        error_action = error.query_one(".status-action-btn", Button)
        idle = (retry_now.styles.background, error_action.styles.background)
        for button in (retry_now, error_action):
            button.add_class("-active")

        await wait_for(
            lambda: idle[0] != retry_now.styles.background and idle[1] != error_action.styles.background,
            pilot=pilot,
            description="both buttons restyled as pressed",
        )
        assert retry_now.styles.background == error_action.styles.background
        assert retry_now.styles.color == error_action.styles.color


async def test_retry_now_asks_once_and_ends_the_countdown(clock: _Clock) -> None:
    retry = RetryMessage("Connection error.", 2, 10, 7, deadline=clock.now + 7, wait_id="w1")
    async with _Host(retry).run_test() as pilot:
        button = retry.query_one(".retry-now-btn", Button)
        # A quick double click queues both presses before the card handles the first.
        button.post_message(Button.Pressed(button))
        button.post_message(Button.Pressed(button))
        await wait_for(lambda: pilot.app.clicked, pilot=pilot, description="Retry Now clicked")
        await pilot.pause()

        assert pilot.app.clicked == ["w1"]
        assert retry.retry_started
        assert _countdown(retry) == "Retrying (2/10)..."
        assert not button.display


async def test_a_wait_that_cannot_be_skipped_counts_down_without_a_button(clock: _Clock) -> None:
    retry = RetryMessage("Connection error.", 1, 1, 0, deadline=clock.now)
    async with _Host(retry).run_test():
        assert not retry.query(".retry-now-btn")
        assert _countdown(retry) == "Retrying (1/1)..."
        assert retry.retry_started


async def test_a_notice_without_a_deadline_shows_the_wait_it_was_told() -> None:
    retry = RetryMessage("Connection error.", 2, 10, 7)
    async with _Host(retry).run_test():
        assert _body(retry) == "✗ Error\nConnection error. Retrying in 7s (2/10)..."
        assert not retry.query(".retry-countdown-row")


async def test_the_countdown_and_button_speak_the_current_locale(clock: _Clock) -> None:
    retry = RetryMessage("无法连接。", 2, 10, 7, deadline=clock.now + 7, wait_id="w1")
    async with _Host(retry, locale="zh-Hans").run_test():
        assert _countdown(retry) == "7 秒后重试（2/10）"  # noqa: RUF001
        assert str(retry.query_one(".retry-now-btn", Button).label) == "立刻重试"


async def test_the_notice_renders_the_message_as_text_not_markup(clock: _Clock) -> None:
    retry = RetryMessage("bad [bold]markup[/] \x1b[31m", 1, 3, 3, deadline=clock.now + 3, wait_id="w1")
    async with _Host(retry).run_test():
        body = _body(retry)
        assert "[bold]markup[/]" in body
        assert "\x1b" not in body


async def test_retry_now_on_the_main_chat_notice_asks_the_backend_to_retry(tmp_path: Path) -> None:
    bus = EventBus()
    skips = await capture_events(bus, RetryWaitSkip)
    app = make_chrys_app(tmp_path / "sessions", event_bus=bus)
    async with app.run_test(size=(120, 40)) as pilot:
        main = app._main_screen
        assert main is not None
        await bus.publish(SessionReady(session_id="s1", agent_profile="Code"), raise_handler_errors=True)
        main._state.run.agent_running = True
        await bus.publish(
            InvocationRetryAttempt(
                origin=InvocationOrigin("turn", "s1", "turn-1", None),
                message="Connection error.",
                attempt=2,
                max_attempts=10,
                delay_seconds=600,
                session_id="s1",
                wait_id="w1",
            ),
            raise_handler_errors=True,
        )
        panel = main.query_one(ChatPanel)
        await wait_for(
            lambda: bool(panel.query(RetryMessage)),
            pilot=pilot,
            timeout=MAIN_SCREEN_BUILD_TIMEOUT,
            description="the retry notice",
        )
        retry = panel.query_one(RetryMessage)
        assert _countdown(retry) in {"Retrying in 600s (2/10)", "Retrying in 599s (2/10)"}

        await click_when_settled(pilot, retry.query_one(".retry-now-btn", Button))
        await wait_for(lambda: skips, pilot=pilot, description="the skip request")

        assert [skip.wait_id for skip in skips] == ["w1"]
        assert retry.retry_started
        # The click leaves the composer the focus it had.
        assert app.focused is not None
        assert main.query_one(InputBar) in app.focused.ancestors_with_self
