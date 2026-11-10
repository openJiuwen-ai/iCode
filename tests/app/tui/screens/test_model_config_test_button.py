# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Spec tests for the model configuration screen's Test button.

Written from ``.plan/model-list-fetch.md`` §3.6 and the interface contract
``.plan/model-list-interface.md``: ``scoped_client`` and ``get_final_response``
are replaced in the screen module, so no request leaves the process.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

import httpx
import pytest
from rich.text import Text
from textual.app import App
from textual.pilot import Pilot
from textual.widgets import Button, Input, Static
from textual.worker import WorkerState

from chrys.app.tui.screens.dialogs.connection_test import ConnectionTestDialog
from chrys.app.tui.screens.models import screen as screen_module
from chrys.app.tui.screens.models.screen import ModelConfigScreen
from chrys.foundation.errors.classify import ErrorKind
from chrys.foundation.errors.provider import ProviderResponseError
from chrys.kernel import ChatResponse, Message
from chrys.service.llm.model_list import ConnectionSnapshot, ModelDiscovery
from chrys.service.profiles.models.schema import ModelProfile
from tests.app.tui.screens._model_config_support import (
    _wait_for_kv_rows,
    fill_kv_row,
    open_model_config,
    single_profile_registry,
)
from tests.support.tui_helpers import click_when_settled
from tests.support.waiting import wait_for, wait_until, wait_until_quiet

pytestmark = pytest.mark.usefixtures("isolated_model_config_dir", "clear_model_profile_env")

URL = "https://gw.example.test/v1"
REPLY = "OK-REPLY-SENTINEL"
_ADDRESS_AND_ELAPSED = re.compile(rf"{re.escape(URL)} · \d+\.\ds")


# Bounded window for "no (second) dialog / request appears".
NOTHING_HAPPENS_WINDOW = 0.3


class FakeLLM:
    """Records what the Test button would send and answers through *behavior*.

    ``scoped_client`` and ``get_final_response`` mirror the real signatures in
    ``chrys.service.llm.clients`` / ``chrys.service.llm.one_shot``.
    """

    def __init__(self) -> None:
        self.profiles: list[ModelProfile] = []
        self.session_ids: list[str | None] = []
        self.calls: list[tuple[object, list[Message], bool, float | None]] = []
        self.options: list[dict[str, Any] | None] = []
        self.behavior: Callable[[], Awaitable[ChatResponse[Any]]] = self._ok

    @staticmethod
    async def _ok() -> ChatResponse[Any]:
        return ChatResponse(messages=[Message("assistant", [REPLY])])

    @contextlib.asynccontextmanager
    async def scoped_client(
        self,
        profile: ModelProfile,
        on_intermediate_text: Callable[[str], None] | None = None,
        session_id: str | None = None,
        parent_session_id: str | None = None,
        use_route_session_context: bool = False,
        session_dir: Path | None = None,
        tool_result_ceiling_tokens: int | None = None,
    ) -> AsyncIterator[object]:
        self.profiles.append(profile)
        self.session_ids.append(session_id)
        yield object()

    async def get_final_response(
        self,
        client: object,
        messages: Sequence[Message],
        *,
        stream: bool,
        options: Mapping[str, Any] | None = None,
        timeout: float | None = None,
    ) -> ChatResponse[Any]:
        self.calls.append((client, list(messages), stream, timeout))
        self.options.append(dict(options) if options is not None else None)
        return await self.behavior()


async def no_models(
    snapshot: ConnectionSnapshot,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    on_retry: Callable[[int, int], None] | None = None,
) -> ModelDiscovery:
    return ModelDiscovery(models=[])


@pytest.fixture
def fake_llm(monkeypatch: pytest.MonkeyPatch) -> FakeLLM:
    fake = FakeLLM()
    monkeypatch.setattr(screen_module, "scoped_client", fake.scoped_client)
    monkeypatch.setattr(screen_module, "get_final_response", fake.get_final_response)
    monkeypatch.setattr(ModelConfigScreen, "discover_models", staticmethod(no_models))
    return fake


def _dialogs(app: App) -> list[ConnectionTestDialog]:
    return [screen for screen in app.screen_stack if isinstance(screen, ConnectionTestDialog)]


async def _wait_for_result(pilot: Pilot, *, success: bool) -> ConnectionTestDialog:
    expected = "-success" if success else "-error"

    def resolved() -> ConnectionTestDialog | None:
        return next((d for d in _dialogs(pilot.app) if d.has_class(expected)), None)

    await wait_for(resolved, pilot=pilot, description=f"test dialog reaches {expected}")
    dialog = resolved()
    assert dialog is not None
    return dialog


def _dialog_text(dialog: ConnectionTestDialog) -> str:
    """The plain-text result body used by model connection tests."""
    return str(dialog.query_one("#connection-test-message", Static).content)


async def _press_test(screen: ModelConfigScreen, pilot: Pilot) -> None:
    await click_when_settled(pilot, "#mc-test")


# ── Placement ────────────────────────────────────────────────────────


async def test_test_button_sits_just_left_of_save(fake_llm: FakeLLM) -> None:
    registry, profile = single_profile_registry(base_url=URL)

    async with open_model_config(registry, global_default_profile_id=profile.id) as (screen, pilot):
        test = screen.query_one("#mc-test", Button)
        save = screen.query_one("#mc-save", Button)
        await wait_until_quiet(lambda: (test.region, save.region), description="footer laid out", pilot=pilot)
        row = [b.id for b in screen.query_one("#mc-buttons").query(Button)]

        assert test.display
        assert row.index("mc-test") == row.index("mc-save") - 1
        assert test.region.right <= save.region.x
        assert test.region.y == save.region.y


async def test_read_only_hides_the_test_button(fake_llm: FakeLLM) -> None:
    registry, profile = single_profile_registry(base_url=URL)

    async with open_model_config(registry, global_default_profile_id=profile.id, read_only=True) as (screen, pilot):
        test = screen.query_one("#mc-test", Button)

        assert test.display is False
        test.press()

        assert not await wait_until(
            lambda: _dialogs(pilot.app) or fake_llm.profiles, timeout=NOTHING_HAPPENS_WINDOW, pilot=pilot
        )


# ── Validation ───────────────────────────────────────────────────────


async def test_validation_errors_are_listed_without_a_request(fake_llm: FakeLLM) -> None:
    registry, profile = single_profile_registry(base_url=URL)

    async with open_model_config(registry, global_default_profile_id=profile.id) as (screen, pilot):
        screen.query_one("#mc-model", Input).value = ""
        screen.query_one("#mc-max-tokens", Input).value = "abc"
        errors = screen._validate()
        assert errors

        await _press_test(screen, pilot)
        dialog = await _wait_for_result(pilot, success=False)

        body = _dialog_text(dialog)
        for error in errors:
            assert error in body
        assert fake_llm.profiles == []
        assert fake_llm.calls == []


# ── Request ──────────────────────────────────────────────────────────


async def test_uses_unsaved_form_values_without_retries(fake_llm: FakeLLM, tmp_path: Path) -> None:
    registry, profile = single_profile_registry(base_url=URL, api_key="sk-saved", stream=True)

    async with open_model_config(registry, global_default_profile_id=profile.id) as (screen, pilot):
        screen.query_one("#mc-name", Input).value = "Renamed"
        screen.query_one("#mc-model", Input).value = "unsaved-model"
        screen.query_one("#mc-base-url", Input).value = "https://unsaved.example.test/v1"
        screen.query_one("#mc-api-key", Input).value = "sk-unsaved"

        await _press_test(screen, pilot)
        await _wait_for_result(pilot, success=True)
        assert screen.query_one("#mc-name", Input).value == "Renamed"
        assert [p.id for p in registry.list_profiles()] == [profile.id]

    (tested,) = fake_llm.profiles
    assert tested.model_id == "unsaved-model"
    assert tested.base_url == "https://unsaved.example.test/v1"
    assert tested.api_key == "sk-unsaved"
    assert tested.http_max_retries == 0
    assert fake_llm.session_ids == [None]

    (_client, messages, stream, _timeout) = fake_llm.calls[0]
    assert [m.text for m in messages] == ["Reply with OK."]
    assert stream is True

    assert registry.get(profile.id) == profile
    assert not (tmp_path / "models").exists()


# ── Results ──────────────────────────────────────────────────────────


async def test_success_shows_connection_details_not_the_reply(fake_llm: FakeLLM) -> None:
    registry, profile = single_profile_registry(
        base_url=URL, model_id="gpt-test", provider="openai", api_style="responses"
    )

    async with open_model_config(registry, global_default_profile_id=profile.id) as (screen, pilot):
        await _press_test(screen, pilot)
        dialog = await _wait_for_result(pilot, success=True)
        body = _dialog_text(dialog)

    assert REPLY not in body
    lines = body.splitlines()
    assert len(lines) == 2, body
    assert lines[0] == "gpt-test · Responses"
    assert _ADDRESS_AND_ELAPSED.fullmatch(lines[1]), lines[1]
    assert "Connection Successful" not in body


@pytest.mark.parametrize("provider", ["anthropic", "glm-openai"])
async def test_success_without_api_style_names_only_the_model(fake_llm: FakeLLM, provider: str) -> None:
    registry, profile = single_profile_registry(base_url=URL, model_id="the-model", provider=provider)

    async with open_model_config(registry, global_default_profile_id=profile.id) as (screen, pilot):
        await _press_test(screen, pilot)
        dialog = await _wait_for_result(pilot, success=True)
        body = _dialog_text(dialog)

    lines = body.splitlines()
    assert len(lines) == 2, body
    assert lines[0] == "the-model"
    assert _ADDRESS_AND_ELAPSED.fullmatch(lines[1]), lines[1]


async def test_failure_shows_the_described_error_and_the_raw_error(fake_llm: FakeLLM) -> None:
    async def fail() -> Any:
        raise ProviderResponseError("rate_limit", "too many requests zz", retryable=False, kind=ErrorKind.RATE_LIMITED)

    fake_llm.behavior = fail
    registry, profile = single_profile_registry(base_url=URL)

    async with open_model_config(registry, global_default_profile_id=profile.id) as (screen, pilot):
        await _press_test(screen, pilot)
        dialog = await _wait_for_result(pilot, success=False)
        body = _dialog_text(dialog)

        assert "The model service is rate limiting requests." in body
        assert "too many requests zz" in body
        assert body.index("rate limiting") < body.index("too many requests zz")

        await pilot.press("escape")
        dismissed = await wait_until(
            lambda: dialog not in pilot.app.screen_stack, timeout=NOTHING_HAPPENS_WINDOW, pilot=pilot
        )
        assert not dismissed, "a non-timeout result needs Ok"


async def test_failure_without_a_description_shows_only_the_raw_error(fake_llm: FakeLLM) -> None:
    async def fail() -> Any:
        raise RuntimeError("Error code: 404 - model nope-404 does not exist")

    fake_llm.behavior = fail
    registry, profile = single_profile_registry(base_url=URL)

    async with open_model_config(registry, global_default_profile_id=profile.id) as (screen, pilot):
        await _press_test(screen, pilot)
        dialog = await _wait_for_result(pilot, success=False)
        body = _dialog_text(dialog)

    assert "model nope-404 does not exist" in body
    assert "rate limiting" not in body


async def test_client_setup_failure_is_reported(fake_llm: FakeLLM, monkeypatch: pytest.MonkeyPatch) -> None:
    @contextlib.asynccontextmanager
    async def broken(
        profile: ModelProfile,
        on_intermediate_text: Callable[[str], None] | None = None,
        session_id: str | None = None,
        parent_session_id: str | None = None,
        use_route_session_context: bool = False,
        session_dir: Path | None = None,
        tool_result_ceiling_tokens: int | None = None,
    ) -> AsyncIterator[object]:
        raise ValueError("environment variable MISSING_KEY_ZZ is not set")
        yield  # pragma: no cover

    monkeypatch.setattr(screen_module, "scoped_client", broken)
    registry, profile = single_profile_registry(base_url=URL)

    async with open_model_config(registry, global_default_profile_id=profile.id) as (screen, pilot):
        await _press_test(screen, pilot)
        dialog = await _wait_for_result(pilot, success=False)

        assert "MISSING_KEY_ZZ" in _dialog_text(dialog)
        assert pilot.app.is_running


async def test_timeout_reports_failure_and_allows_escape(fake_llm: FakeLLM, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ModelConfigScreen, "MODEL_TEST_TIMEOUT_SECONDS", 0.2)

    async def hang() -> Any:
        await asyncio.Event().wait()

    fake_llm.behavior = hang
    registry, profile = single_profile_registry(base_url=URL)

    async with open_model_config(registry, global_default_profile_id=profile.id) as (screen, pilot):
        await _press_test(screen, pilot)
        dialog = await _wait_for_result(pilot, success=False)
        assert _dialog_text(dialog)

        await pilot.press("escape")

        await wait_for(lambda: dialog not in pilot.app.screen_stack, pilot=pilot)
        assert pilot.app.screen is screen


async def test_default_timeout_is_thirty_seconds() -> None:
    assert ModelConfigScreen.MODEL_TEST_TIMEOUT_SECONDS == 30.0


# ── Repeated presses ─────────────────────────────────────────────────


async def test_pressing_twice_while_running_opens_one_dialog(fake_llm: FakeLLM) -> None:
    gate = asyncio.Event()

    async def held() -> ChatResponse[Any]:
        await gate.wait()
        return ChatResponse(messages=[Message("assistant", [REPLY])])

    fake_llm.behavior = held
    registry, profile = single_profile_registry(base_url=URL)

    async with open_model_config(registry, global_default_profile_id=profile.id) as (screen, pilot):
        button = screen.query_one("#mc-test", Button)
        button.press()
        await wait_for(lambda: len(fake_llm.calls) == 1, pilot=pilot)
        # The label never changes while a test runs; the button is only disabled.
        await wait_for(lambda: button.disabled, pilot=pilot, description="Test disabled while running")
        assert str(button.label) == "Test"
        button.press()
        button.press()

        assert not await wait_until(
            lambda: len(_dialogs(pilot.app)) > 1 or len(fake_llm.profiles) > 1,
            timeout=NOTHING_HAPPENS_WINDOW,
            pilot=pilot,
        )

        gate.set()
        dialog = await _wait_for_result(pilot, success=True)
        await click_when_settled(pilot, "#connection-test-ok")
        await wait_for(lambda: dialog not in pilot.app.screen_stack, pilot=pilot)
        await wait_for(lambda: not button.disabled, pilot=pilot, description="Test enabled again")
        assert str(button.label) == "Test"

        button.press()
        await _wait_for_result(pilot, success=True)
        assert len(fake_llm.profiles) == 2


# ── Cancellation ─────────────────────────────────────────────────────


async def test_a_cancelled_error_from_the_request_is_reported_and_the_button_recovers(fake_llm: FakeLLM) -> None:
    """A CancelledError the request raises itself (the worker is not being cancelled) is a failed test."""

    async def cancelled_inside() -> ChatResponse[Any]:
        raise asyncio.CancelledError("Cancelled via cancel scope 10d360440")

    fake_llm.behavior = cancelled_inside
    registry, profile = single_profile_registry(base_url=URL)

    async with open_model_config(registry, global_default_profile_id=profile.id) as (screen, pilot):
        button = screen.query_one("#mc-test", Button)
        idle_label = str(button.label)
        await _press_test(screen, pilot)
        dialog = await _wait_for_result(pilot, success=False)
        assert pilot.app.is_running
        await click_when_settled(pilot, "#connection-test-ok")
        await wait_for(lambda: dialog not in pilot.app.screen_stack, pilot=pilot)
        await wait_for(lambda: not button.disabled, pilot=pilot, description="Test enabled again")
        assert str(button.label) == idle_label

        fake_llm.behavior = FakeLLM._ok
        button.press()
        await _wait_for_result(pilot, success=True)
        assert len(fake_llm.profiles) == 2


async def test_cancelling_a_running_test_reraises_and_leaves_the_button_usable(fake_llm: FakeLLM) -> None:
    entered = asyncio.Event()

    async def hang() -> ChatResponse[Any]:
        entered.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    fake_llm.behavior = hang
    registry, profile = single_profile_registry(base_url=URL)

    async with open_model_config(registry, global_default_profile_id=profile.id) as (screen, pilot):
        button = screen.query_one("#mc-test", Button)
        idle_label = str(button.label)
        button.press()
        await wait_for(entered.is_set, pilot=pilot, description="test request running")
        running = [worker for worker in pilot.app.workers if worker.node is screen and worker.is_running]
        assert running, "the test runs in a worker of the screen"

        for worker in running:
            worker.cancel()

        await wait_for(lambda: all(w.state is WorkerState.CANCELLED for w in running), pilot=pilot)
        assert pilot.app.is_running
        await wait_for(lambda: not button.disabled, pilot=pilot, description="Test enabled again")
        assert str(button.label) == idle_label

        fake_llm.behavior = FakeLLM._ok
        button.press()
        await wait_for(lambda: len(fake_llm.profiles) == 2, pilot=pilot, description="a second test starts")


# ── Footer at narrow widths ──────────────────────────────────────────

FOOTER_BUTTONS = ("mc-new", "mc-clone", "mc-delete", "mc-test", "mc-save", "mc-cancel")
FOOTER_WIDTHS = [60, 76, 77, 80, 120]
# Below this the footer uses its narrow layout (HORIZONTAL_BREAKPOINTS).
REGULAR_FOOTER_FROM = 77


@pytest.mark.parametrize("locale", ["en", "zh-Hans"])
@pytest.mark.parametrize("width", FOOTER_WIDTHS)
async def test_all_six_footer_buttons_fit_inside_the_dialog(fake_llm: FakeLLM, width: int, locale: str) -> None:
    """No footer button is clipped at any terminal width the dialog opens at (main clipped Close at 60-65)."""
    registry, profile = single_profile_registry(base_url=URL)

    async with open_model_config(registry, locale=locale, size=(width, 30), global_default_profile_id=profile.id) as (
        screen,
        pilot,
    ):
        if locale == "zh-Hans":
            # "测试" is 4 cells, narrower than the buttons' minimum width.
            assert str(screen.query_one("#mc-test", Button).label) == "测试"
        buttons = [screen.query_one(f"#{button_id}", Button) for button_id in FOOTER_BUTTONS]
        container = screen.query_one("#mc-container")
        row = screen.query_one("#mc-buttons")
        await wait_until_quiet(
            lambda: (container.region, row.region, *(b.region for b in buttons)),
            description="footer laid out",
            pilot=pilot,
        )

        for button in buttons:
            assert button.display, button.id
            assert button.region.width > 0, button.id
            assert container.region.contains_region(button.region), (button.id, button.region, container.region)
            assert row.region.contains_region(button.region), (button.id, button.region, row.region)
            assert button.content_region.width >= Text(str(button.label)).cell_len, (button.id, button.content_region)
        for left, right in itertools.pairwise(buttons):
            assert left.region.right <= right.region.x, (left.id, right.id)
        # New/Clone/Delete keep a gap between them at every width.
        for left, right in itertools.pairwise(buttons[:3]):
            assert right.region.x - left.region.right >= 1, (left.id, right.id, left.region, right.region)

        widths = {button.id: button.region.width for button in buttons}
        assert widths["mc-test"] == widths["mc-save"] == widths["mc-cancel"], widths
        if width >= REGULAR_FOOTER_FROM:
            assert widths["mc-test"] == 11, widths
            assert all(widths[button_id] >= 10 for button_id in ("mc-new", "mc-clone", "mc-delete")), widths


# ── Keyboard focus after a test ──────────────────────────────────────


async def test_keyboard_started_test_returns_focus_to_the_test_button(fake_llm: FakeLLM) -> None:
    registry, profile = single_profile_registry(base_url=URL)

    async with open_model_config(registry, global_default_profile_id=profile.id) as (screen, pilot):
        test = screen.query_one("#mc-test", Button)
        idle_label = str(test.label)
        test.focus()
        await wait_for(lambda: test.has_focus, pilot=pilot)

        await pilot.press("enter")
        dialog = await _wait_for_result(pilot, success=True)
        await wait_for(lambda: dialog.query_one("#connection-test-ok", Button).has_focus, pilot=pilot)
        await pilot.press("enter")
        await wait_for(lambda: dialog not in pilot.app.screen_stack, pilot=pilot)

        await wait_for(lambda: test.has_focus, pilot=pilot, description="focus back on Test")
        assert not screen.query_one("#mc-new", Button).has_focus
        await wait_for(
            lambda: str(test.label) == idle_label and not test.disabled, pilot=pilot, description="Test ready again"
        )
        # Textual ignores presses while a button still shows its press animation (``-active``).
        await wait_for(lambda: not test.has_class("-active"), pilot=pilot, description="press animation over")
        assert test.has_focus

        await pilot.press("enter")
        await _wait_for_result(pilot, success=True)

        assert len(fake_llm.profiles) == 2
        assert [p.id for p in registry.list_profiles()] == [profile.id]


async def test_request_carries_model_max_tokens_and_chat_options(fake_llm: FakeLLM) -> None:
    registry, profile = single_profile_registry(base_url=URL, model_id="saved-model", max_output_tokens=32_000)

    async with open_model_config(registry, global_default_profile_id=profile.id) as (screen, pilot):
        options_list = screen.query_one("#mc-options-list")
        await _wait_for_kv_rows(options_list, pilot, 1)
        fill_kv_row(options_list, "temperature", "0.3")
        screen.query_one("#mc-model", Input).value = "form-model"
        screen.query_one("#mc-max-output-tokens", Input).value = "4096"

        await _press_test(screen, pilot)
        await _wait_for_result(pilot, success=True)

    (options,) = fake_llm.options
    assert options is not None
    assert options.get("model") == "form-model"
    assert options.get("max_tokens") == 4096
    assert options.get("temperature") == 0.3
