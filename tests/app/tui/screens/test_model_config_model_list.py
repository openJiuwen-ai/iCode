# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Spec tests for the model-list Fetch button on the model configuration screen.

Written from ``.plan/model-list-fetch.md`` (when and how the list is fetched, the hints) and the
interface contract ``.plan/model-list-interface.md``. ``ModelConfigScreen.discover_models`` is
replaced by a recording fake, or by the real lookup over an ``httpx.MockTransport``, so no request
leaves the process. The dropdown's own keyboard, mouse and filter rules are in
``test_model_config_model_picker.py``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from textual.containers import Horizontal
from textual.pilot import Pilot
from textual.widget import Widget
from textual.widgets import Button, Checkbox, Input, Label, Select
from textual.worker import WorkerState

from chrys.app.tui.screens.models.screen import ModelConfigScreen
from chrys.app.tui.util.spinner import SPINNER_FRAMES, STATIC_SPINNER
from chrys.service.llm.model_list import ConnectionSnapshot, ModelDiscovery
from chrys.service.llm.providers import PROVIDERS
from chrys.service.profiles.models.registry import ModelProfileRegistry
from chrys.service.profiles.models.schema import ModelProfile
from tests.app.tui.screens._model_config_support import (
    _wait_for_kv_rows,
    fill_kv_row,
    open_model_config,
    single_profile_registry,
)
from tests.app.tui.screens._model_list_support import (
    BASE_MODEL_LABEL,
    ENV_URL,
    FETCH_EN,
    FETCHING_EN,
    KEY_NEEDED_EN,
    KEY_PLACEHOLDER,
    KEY_REJECTED_EN,
    NOT_FOUND_EN,
    UNREACHABLE_EN,
    URL,
    URL_ADDED_V1_EN,
    URL_REMOVED_V1_EN,
    FakeDiscover,
    RealDiscover,
    _close_list,
    _hint,
    _select_profile,
    _two_profiles,
    _without_default_url,
    arrow_shown,
    assert_overlay_visible,
    bring_into_view,
    click_fetch,
    fetch_and_settle,
    fetch_button,
    fetch_list,
    fetch_ready,
    fetching_frame,
    focus_handled,
    hint_row,
    input_fills_picker,
    is_muted_progress,
    is_warning_row,
    listed,
    model_input,
    model_label,
    models,
    no_new_query,
    no_response,
    open_model_config_zh,
    overlay,
    overlay_expanded,
    overlay_texts,
    picker,
    rejected,
    removed_v1,
    screen_state,
    settle,
    type_into,
    url_hint,
    url_hinted,
    url_label,
    v1_fixed,
)
from tests.support.tui_helpers import click_when_settled
from tests.support.waiting import wait_for, wait_until_quiet

pytestmark = pytest.mark.usefixtures("isolated_model_config_dir", "clear_model_profile_env", "clear_provider_env")


# ── The removed automatic lookup ─────────────────────────────────────


# ── The Fetch button: place and look ─────────────────────────────────


async def test_fields_are_ordered_provider_style_url_key_model(fake_discover: FakeDiscover) -> None:
    registry, profile = single_profile_registry(base_url=URL, api_key="sk-a")

    async with open_model_config(registry, global_default_profile_id=profile.id, size=(120, 60)) as (screen, pilot):
        await wait_until_quiet(lambda: screen_state(screen), description="form loaded", pilot=pilot)
        order = [
            "#mc-provider",
            "#mc-api-style",
            "#mc-url-label",
            "#mc-base-url",
            "#mc-key-label",
            "#mc-api-key",
            "#mc-model-label",
            "#mc-model",
            "#mc-max-tokens-label",
            "#mc-max-tokens",
            "#mc-max-output-label",
            "#mc-max-output-tokens",
        ]
        ys = [screen.query_one(selector).region.y for selector in order]

        assert all(region_y > 0 for region_y in ys), dict(zip(order, ys, strict=True))
        assert ys == sorted(ys), dict(zip(order, ys, strict=True))
        assert len(set(ys)) == len(ys), dict(zip(order, ys, strict=True))


async def test_tab_order_follows_the_fields(fake_discover: FakeDiscover) -> None:
    registry, profile = single_profile_registry(base_url=URL, api_key="sk-a")

    async with open_model_config(registry, global_default_profile_id=profile.id) as (screen, pilot):
        url = screen.query_one("#mc-base-url", Input)
        url.focus()
        await wait_for(lambda: url.has_focus, pilot=pilot)
        await focus_handled(url, pilot)

        # The ▼ is not focusable; Fetch comes right after the model input.
        for expected in ("#mc-api-key", "#mc-model", "#mc-model-fetch", "#mc-max-tokens"):
            await pilot.press("tab")
            target = screen.query_one(expected)
            await wait_for(lambda target=target: target.has_focus, pilot=pilot, description=f"focus on {expected}")

        model = model_input(screen)
        model.focus()
        await wait_for(lambda: model.has_focus, pilot=pilot)
        await focus_handled(model, pilot)
        await pilot.press("shift+tab")
        key = screen.query_one("#mc-api-key", Input)
        await wait_for(lambda: key.has_focus, pilot=pilot, description="shift+tab from the model input lands on Key")


async def test_fetch_follows_the_model_input_on_its_line(fake_discover: FakeDiscover) -> None:
    registry, profile = single_profile_registry(base_url=URL, api_key="sk-a")

    async with open_model_config(registry, global_default_profile_id=profile.id) as (screen, pilot):
        await wait_until_quiet(lambda: screen_state(screen), description="form loaded", pilot=pilot)
        line = screen.query_one("#mc-model-line", Horizontal)
        label = screen.query_one("#mc-model-label", Label)
        button = fetch_button(screen)
        base_url = screen.query_one("#mc-base-url", Input)

        assert not list(screen.query("#mc-model-label-row"))
        assert label.parent is not line
        assert picker(screen).parent is line
        assert button.parent is line
        assert label.region.height == 1
        assert line.region.height == 1 and button.region.height == 1
        assert label.region.bottom <= line.region.y
        assert button.region.y == model_input(screen).region.y == line.region.y
        assert picker(screen).region.right <= button.region.x
        assert button.region.right == line.region.right == base_url.region.right
        assert line.region.x == base_url.region.x
        assert str(button.label) == FETCH_EN
        assert input_fills_picker(screen)
        assert model_input(screen).region.width < base_url.region.width


async def test_read_only_hides_fetch_and_never_looks_up(fake_discover: FakeDiscover) -> None:
    fake_discover.respond = lambda _s: rejected()
    registry, profile = single_profile_registry(base_url=URL, api_key="")

    async with open_model_config(registry, global_default_profile_id=profile.id, read_only=True) as (screen, pilot):
        await no_new_query(fake_discover, pilot, after=0)

        assert fetch_button(screen).display is False
        assert not arrow_shown(screen)
        assert model_label(screen) == BASE_MODEL_LABEL
        assert screen.query_one("#mc-test", Button).display is False


# ── The Fetch button: when it can be clicked ─────────────────────────


@pytest.mark.parametrize("provider", ["openai", "anthropic", "deepseek-openai", "glm-openai"])
async def test_fetch_is_enabled_with_a_blank_url_because_of_the_default(
    fake_discover: FakeDiscover, provider: str
) -> None:
    registry, profile = single_profile_registry(provider=provider, base_url="", api_key="")

    async with open_model_config(registry, global_default_profile_id=profile.id) as (screen, pilot):
        await wait_until_quiet(lambda: screen_state(screen), description="form loaded", pilot=pilot)

        assert fetch_ready(screen)


async def test_fetch_is_disabled_when_no_address_resolves(
    fake_discover: FakeDiscover, monkeypatch: pytest.MonkeyPatch
) -> None:
    _without_default_url(monkeypatch, "openai")
    registry, profile = single_profile_registry(base_url="", api_key="sk-a")

    async with open_model_config(registry, global_default_profile_id=profile.id) as (screen, pilot):
        await wait_for(lambda: fetch_button(screen).disabled, pilot=pilot, description="nothing to query")
        assert str(fetch_button(screen).label) == FETCH_EN

        url = screen.query_one("#mc-base-url", Input)
        url.value = URL
        await wait_for(lambda: fetch_ready(screen), pilot=pilot, description="a typed URL enables Fetch")

        url.value = "   "
        await wait_for(lambda: fetch_button(screen).disabled, pilot=pilot, description="blank URL disables again")
        await no_new_query(fake_discover, pilot, after=0)


async def test_env_address_enables_fetch_without_a_default(
    fake_discover: FakeDiscover, monkeypatch: pytest.MonkeyPatch
) -> None:
    _without_default_url(monkeypatch, "openai")
    monkeypatch.setenv("OPENAI_BASE_URL", ENV_URL)
    registry, profile = single_profile_registry(base_url="", api_key="sk-a")

    async with open_model_config(registry, global_default_profile_id=profile.id) as (screen, pilot):
        await wait_until_quiet(lambda: screen_state(screen), description="form loaded", pilot=pilot)

        assert fetch_ready(screen)


@pytest.mark.parametrize("how", ["switch", "new", "clone"])
async def test_fetch_is_disabled_while_a_profile_loads(
    fake_discover: FakeDiscover, monkeypatch: pytest.MonkeyPatch, how: str
) -> None:
    registry = _two_profiles()

    async with open_model_config(registry, global_default_profile_id="a") as (screen, pilot):
        await wait_for(lambda: fetch_ready(screen), pilot=pilot, description="first load finished")
        original = screen._load_profile
        seen: list[bool] = []

        def spy(profile_id: str) -> None:
            original(profile_id)
            seen.append(fetch_button(screen).disabled)

        monkeypatch.setattr(screen, "_load_profile", spy)
        if how == "switch":
            await _select_profile(screen, pilot, "b")
        else:
            await click_when_settled(pilot, f"#mc-{how}")
            await wait_for(lambda: screen._selected_profile_id not in ("a", None), pilot=pilot)

        await wait_for(lambda: seen, pilot=pilot, description="a profile was loaded")
        assert seen[0] is True, "Fetch is disabled from the moment the load starts"
        await wait_for(lambda: fetch_ready(screen), pilot=pilot, description="enabled once the load finished")
        await no_new_query(fake_discover, pilot, after=0)


async def test_rows_a_profile_load_mounts_never_block_changed_messages_elsewhere(
    fake_discover: FakeDiscover, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A widget built inside ``prevent()`` re-applies its set on the App-wide stack while it mounts.

    The load fills the form under ``prevent``; rows built there would drop every widget's
    ``Input.Changed`` while they mount. A probe in each loaded row posts one from the name input.
    """
    accepted: list[bool] = []

    class _Probe(Widget):
        def on_mount(self) -> None:
            name = self.screen.query_one("#mc-name", Input)
            accepted.append(name.post_message(Input.Changed(name, name.value)))

    build_row = ModelConfigScreen._compose_kv_row

    def build_row_with_probe(
        screen: ModelConfigScreen, key: str, val: str, prefix: str, *, read_only: bool = False
    ) -> Horizontal:
        row = build_row(screen, key, val, prefix, read_only=read_only)
        row.compose_add_child(_Probe())
        return row

    monkeypatch.setattr(ModelConfigScreen, "_compose_kv_row", build_row_with_probe)
    async with open_model_config(_two_profiles(), global_default_profile_id="a") as (screen, pilot):
        await wait_for(lambda: fetch_ready(screen), pilot=pilot, description="first load finished")
        accepted.clear()
        await _select_profile(screen, pilot, "b")
        # Profile B has no headers or options: the load builds one blank row in each list.
        await wait_for(lambda: len(accepted) == 2, pilot=pilot, description="loaded rows mounted")

    assert accepted == [True, True]


async def test_fetch_is_enabled_with_a_command_credential(fake_discover: FakeDiscover) -> None:
    registry, profile = single_profile_registry(base_url=URL, api_key="{{!pass show key}}")

    async with open_model_config(registry, global_default_profile_id=profile.id) as (screen, pilot):
        await wait_until_quiet(lambda: screen_state(screen), description="form loaded", pilot=pilot)

        assert fetch_ready(screen)


async def test_fetch_is_disabled_with_a_progress_hint_while_fetching(fake_discover: FakeDiscover) -> None:
    gate = asyncio.Event()
    fake_discover.hold = gate
    fake_discover.respond = lambda _s: models("a")
    registry, profile = single_profile_registry(base_url=URL, api_key="sk-a")

    async with open_model_config(registry, global_default_profile_id=profile.id) as (screen, pilot):
        await click_fetch(screen, pilot)
        await wait_for(lambda: fake_discover.calls, pilot=pilot)

        button = fetch_button(screen)
        await wait_for(lambda: button.disabled and fetching_frame(screen) is not None, pilot=pilot)
        assert str(button.label) == FETCH_EN
        assert not arrow_shown(screen)

        gate.set()
        await wait_for(lambda: arrow_shown(screen), pilot=pilot)
        await wait_for(lambda: fetch_ready(screen), pilot=pilot, description="button restored")
        assert _hint(screen) is None, "a found list replaces the progress hint"
        assert len(fake_discover.calls) == 1


async def test_read_only_without_profiles_shows_the_default_url_placeholder(fake_discover: FakeDiscover) -> None:
    async with open_model_config(ModelProfileRegistry(), read_only=True) as (screen, pilot):
        await wait_for(
            lambda: screen.query_one("#mc-base-url", Input).placeholder == PROVIDERS["openai"].default_base_url,
            pilot=pilot,
            description="default Base URL placeholder",
        )

        assert fetch_button(screen).display is False
        await no_new_query(fake_discover, pilot, after=0)


@pytest.mark.parametrize("how", ["double-press", "late-message"])
async def test_a_queued_press_while_fetching_starts_no_second_lookup(fake_discover: FakeDiscover, how: str) -> None:
    gate = asyncio.Event()
    fake_discover.hold = gate
    fake_discover.respond = lambda _s: models("a")
    registry, profile = single_profile_registry(base_url=URL, api_key="sk-a")

    async with open_model_config(registry, global_default_profile_id=profile.id) as (screen, pilot):
        button = fetch_button(screen)
        await wait_for(lambda: fetch_ready(screen), pilot=pilot)
        try:
            if how == "double-press":
                # Both Pressed messages are queued before the first one is handled.
                button.press()
                button.press()
            else:
                await click_fetch(screen, pilot)
                await wait_for(lambda: button.disabled, pilot=pilot)
                # A press that was already in the queue when the button turned disabled.
                button.post_message(Button.Pressed(button))
            await wait_for(lambda: fake_discover.calls, pilot=pilot)
            await no_new_query(fake_discover, pilot, after=1)
        finally:
            gate.set()
        await wait_for(lambda: arrow_shown(screen), pilot=pilot)
        await no_new_query(fake_discover, pilot, after=1)


# ── The fetching hint's spinner and the hint colours ────────────────


async def _end_by_result(screen: ModelConfigScreen, pilot: Pilot, fake: FakeDiscover, gate: asyncio.Event) -> None:
    fake.respond = lambda _s: models("a")
    gate.set()
    await wait_for(lambda: overlay_expanded(screen), pilot=pilot)


async def _end_by_failure(screen: ModelConfigScreen, pilot: Pilot, fake: FakeDiscover, gate: asyncio.Event) -> None:
    gate.set()
    await wait_for(lambda: _hint(screen) == NOT_FOUND_EN, pilot=pilot)


async def _end_by_connection_change(
    screen: ModelConfigScreen, pilot: Pilot, _fake: FakeDiscover, _gate: asyncio.Event
) -> None:
    screen.query_one("#mc-api-key", Input).value = "sk-other"
    await wait_for(lambda: _hint(screen) is None, pilot=pilot)


async def _end_by_profile_switch(
    screen: ModelConfigScreen, pilot: Pilot, _fake: FakeDiscover, _gate: asyncio.Event
) -> None:
    await _select_profile(screen, pilot, "b")
    await wait_for(lambda: _hint(screen) is None, pilot=pilot)


@pytest.mark.parametrize(
    "end",
    [_end_by_result, _end_by_failure, _end_by_connection_change, _end_by_profile_switch],
    ids=["result", "failure", "connection-change", "profile-switch"],
)
async def test_fetching_spinner_turns_while_fetching_and_stops_after(
    fake_discover: FakeDiscover,
    end: Callable[[ModelConfigScreen, Pilot, FakeDiscover, asyncio.Event], Awaitable[None]],
) -> None:
    gate = asyncio.Event()
    fake_discover.hold = gate
    registry = _two_profiles()

    async with open_model_config(registry, global_default_profile_id="a") as (screen, pilot):
        pilot.app.animation_level = "full"
        try:
            await click_fetch(screen, pilot)
            await wait_for(lambda: fetching_frame(screen) is not None, pilot=pilot, description="fetching hint")
            seen: set[str] = set()

            def turned() -> bool:
                frame = fetching_frame(screen)
                if frame is not None:
                    seen.add(frame)
                return len(seen) >= 3

            await wait_for(turned, pilot=pilot, description="spinner advanced")
            assert seen <= set(SPINNER_FRAMES), seen
            assert STATIC_SPINNER not in seen

            await end(screen, pilot, fake_discover, gate)
        finally:
            gate.set()

        assert fetching_frame(screen) is None
        # The spinner's interval is gone, not just invisible.
        assert screen._fetch_spinner is None


async def test_fetching_spinner_is_static_with_animations_off(fake_discover: FakeDiscover) -> None:
    gate = asyncio.Event()
    fake_discover.hold = gate
    registry, profile = single_profile_registry(base_url=URL, api_key="sk-a")

    async with open_model_config(registry, global_default_profile_id=profile.id) as (screen, pilot):
        pilot.app.animation_level = "none"
        try:
            await click_fetch(screen, pilot)
            await wait_for(lambda: fetching_frame(screen) == STATIC_SPINNER, pilot=pilot, description="static frame")

            assert fetching_frame(screen) == STATIC_SPINNER
            assert screen._fetch_spinner is None
        finally:
            gate.set()
        await wait_for(lambda: _hint(screen) == NOT_FOUND_EN, pilot=pilot)


@pytest.mark.parametrize(
    ("answer", "api_key", "label"),
    [
        (rejected(), "", "#mc-model-hint"),
        (rejected(), "sk-a", "#mc-model-hint"),
        (ModelDiscovery(models=[]), "sk-a", "#mc-model-hint"),
        (no_response(), "sk-a", "#mc-model-hint"),
        (v1_fixed("missing_v1", URL + "/v1", "m"), "sk-a", "#mc-url-hint"),
        (removed_v1(), "sk-a", "#mc-url-hint"),
    ],
    ids=["key-needed", "key-rejected", "not-found", "unreachable", "added-v1", "removed-v1"],
)
async def test_result_hints_use_the_warning_colour(
    fake_discover: FakeDiscover, answer: ModelDiscovery, api_key: str, label: str
) -> None:
    fake_discover.respond = lambda _s: answer
    registry, profile = single_profile_registry(base_url=URL, api_key=api_key)

    async with open_model_config(registry, global_default_profile_id=profile.id) as (screen, pilot):
        await fetch_and_settle(screen, fake_discover, pilot)

        assert is_warning_row(screen, label)


async def test_fetching_progress_is_muted_under_the_model_input(fake_discover: FakeDiscover) -> None:
    gate = asyncio.Event()
    fake_discover.hold = gate
    registry, profile = single_profile_registry(base_url=URL, api_key="sk-a")

    async with open_model_config(registry, global_default_profile_id=profile.id) as (screen, pilot):
        try:
            await click_fetch(screen, pilot)
            await wait_for(lambda: fetching_frame(screen) is not None, pilot=pilot)

            assert is_muted_progress(screen)
            assert model_label(screen) == BASE_MODEL_LABEL
        finally:
            gate.set()


# ── The Fetch button: what it sends ──────────────────────────────────


async def test_fetch_by_keyboard(fake_discover: FakeDiscover) -> None:
    """Enter on the focused button, as every other iCode button."""
    fake_discover.respond = lambda _s: models("a", "b")
    registry, profile = single_profile_registry(base_url=URL, api_key="sk-a")

    async with open_model_config(registry, global_default_profile_id=profile.id) as (screen, pilot):
        button = fetch_button(screen)
        # As a user would: the button takes focus once the profile has loaded and enabled it.
        await wait_for(lambda: fetch_ready(screen), pilot=pilot, description="profile loaded")
        button.focus()
        await wait_for(lambda: button.has_focus, pilot=pilot)

        await pilot.press("enter")

        await wait_for(lambda: overlay_expanded(screen), pilot=pilot, description="list opened")
        assert len(fake_discover.calls) == 1


async def test_every_click_really_looks_up(fake_discover: FakeDiscover) -> None:
    fake_discover.respond = lambda _s: models("a", "b")
    registry, profile = single_profile_registry(base_url=URL, api_key="sk-a")

    async with open_model_config(registry, global_default_profile_id=profile.id) as (screen, pilot):
        await fetch_and_settle(screen, fake_discover, pilot)
        await fetch_and_settle(screen, fake_discover, pilot)

        assert len(fake_discover.calls) == 2
        assert fake_discover.calls[0] == fake_discover.calls[1]
        assert arrow_shown(screen)


async def test_fetch_uses_the_unsaved_form_values(fake_discover: FakeDiscover) -> None:
    registry, profile = single_profile_registry(base_url=URL, api_key="sk-a")

    async with open_model_config(registry, global_default_profile_id=profile.id) as (screen, pilot):
        screen.query_one("#mc-base-url", Input).value = "https://typed.example.test/v1"
        screen.query_one("#mc-api-key", Input).value = "sk-typed"

        await fetch_and_settle(screen, fake_discover, pilot)

        assert (fake_discover.calls[0].base_url, fake_discover.calls[0].api_key) == (
            "https://typed.example.test/v1",
            "sk-typed",
        )
    stored = registry.get(profile.id)
    assert stored is not None
    assert (stored.base_url, stored.api_key) == (URL, "sk-a")


async def test_snapshot_carries_every_connection_field(
    fake_discover: FakeDiscover, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SOME_KEY", "sk-some")
    registry, profile = single_profile_registry(
        provider="anthropic",
        base_url=URL,
        api_key="{{SOME_KEY}}",
        http_headers='{"X-Team": "platform"}',
        chat_options='{"extra_headers": {"X-Opt": "1"}}',
        verify_ssl=False,
        bypass_proxy=True,
        http_connect_timeout=7.0,
    )

    async with open_model_config(registry, global_default_profile_id=profile.id) as (screen, pilot):
        await fetch_and_settle(screen, fake_discover, pilot)

        snapshot = fake_discover.calls[0]
        assert snapshot.provider == "anthropic"
        assert snapshot.base_url == URL
        assert snapshot.api_key == "{{SOME_KEY}}"
        assert snapshot.http_headers == {"X-Team": "platform"}
        assert snapshot.extra_headers == {"X-Opt": "1"}
        assert snapshot.verify_ssl is False
        assert snapshot.bypass_proxy is True
        assert snapshot.connect_timeout == 7.0


async def test_base_url_is_passed_stripped_with_its_trailing_slash(fake_discover: FakeDiscover) -> None:
    registry, profile = single_profile_registry(base_url="  https://gw.example.test/v1/ ", api_key="sk-a")

    async with open_model_config(registry, global_default_profile_id=profile.id) as (screen, pilot):
        await fetch_and_settle(screen, fake_discover, pilot)

        assert fake_discover.calls[0].base_url == "https://gw.example.test/v1/"


async def test_api_key_with_surrounding_spaces_is_sent_trimmed(fake_discover: FakeDiscover) -> None:
    registry, profile = single_profile_registry(base_url=URL, api_key="")

    async with open_model_config(registry, global_default_profile_id=profile.id) as (screen, pilot):
        key = screen.query_one("#mc-api-key", Input)
        await type_into(pilot, key, "  sk-new  ")
        await wait_for(lambda: key.value == "  sk-new  ", pilot=pilot)

        await fetch_and_settle(screen, fake_discover, pilot)

        assert fake_discover.calls[0].api_key == "sk-new"
        assert key.value == "  sk-new  "


async def test_escaped_command_placeholder_still_looks_up(fake_discover: FakeDiscover) -> None:
    fake_discover.respond = lambda _s: models("a")
    registry, profile = single_profile_registry(base_url=URL, api_key="{{{{!literal}}}}")

    async with open_model_config(registry, global_default_profile_id=profile.id) as (screen, pilot):
        await fetch_list(screen, pilot)

        assert [s.api_key for s in fake_discover.calls] == ["{{{{!literal}}}}"]


# ── Environment fallbacks, end to end (real lookup, mock transport) ──


def _hosts(real: RealDiscover) -> set[str]:
    return {request.url.host for request in real.requests}


async def test_blank_url_and_key_use_the_provider_env(
    real_discover: RealDiscover, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_BASE_URL", ENV_URL)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-from-env")
    registry, profile = single_profile_registry(base_url="", api_key="", model_id="")

    async with open_model_config(registry, global_default_profile_id=profile.id) as (screen, pilot):
        await fetch_list(screen, pilot)

        assert overlay_texts(screen) == ["m-1"]
        assert _hosts(real_discover) == {"env.example.test"}
        assert real_discover.requests[0].headers["authorization"] == "Bearer sk-from-env"


# ── Placeholders ─────────────────────────────────────────────────────


def _placeholders(screen: ModelConfigScreen) -> tuple[str, str]:
    return (
        str(screen.query_one("#mc-base-url", Input).placeholder),
        str(screen.query_one("#mc-api-key", Input).placeholder),
    )


async def test_placeholders_show_the_default_url_and_sk_without_env(fake_discover: FakeDiscover) -> None:
    registry, profile = single_profile_registry(base_url="", api_key="")

    async with open_model_config(registry, global_default_profile_id=profile.id) as (screen, pilot):
        await wait_until_quiet(lambda: _placeholders(screen), description="form loaded", pilot=pilot)

        assert _placeholders(screen) == (PROVIDERS["openai"].default_base_url, KEY_PLACEHOLDER)


@pytest.mark.parametrize("locale", ["en", "zh-Hans"])
async def test_placeholders_show_the_env_url_and_the_env_key_name(
    fake_discover: FakeDiscover, monkeypatch: pytest.MonkeyPatch, locale: str
) -> None:
    monkeypatch.setenv("OPENAI_BASE_URL", ENV_URL)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-secret-from-env")
    registry, profile = single_profile_registry(base_url="", api_key="")

    opener = open_model_config_zh if locale == "zh-Hans" else open_model_config
    expected = "使用环境变量 OPENAI_API_KEY" if locale == "zh-Hans" else "Using OPENAI_API_KEY"
    async with opener(registry, global_default_profile_id=profile.id) as (screen, pilot):
        await wait_for(lambda: _placeholders(screen) == (ENV_URL, expected), pilot=pilot)

        assert "sk-secret-from-env" not in "".join(_placeholders(screen))
        if locale == "zh-Hans":
            key = screen.query_one("#mc-api-key", Input)
            await bring_into_view(screen, key, pilot)
            assert expected in key.render_line(0).text


async def test_placeholders_follow_a_provider_switch(
    fake_discover: FakeDiscover, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_BASE_URL", ENV_URL)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-env")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-ds-env")
    registry, profile = single_profile_registry(base_url="", api_key="")

    async with open_model_config(registry, global_default_profile_id=profile.id) as (screen, pilot):
        await wait_for(lambda: _placeholders(screen) == (ENV_URL, "Using OPENAI_API_KEY"), pilot=pilot)

        provider = screen.query_one("#mc-provider", Select)
        provider.value = "anthropic"
        await wait_for(
            lambda: _placeholders(screen) == (PROVIDERS["anthropic"].default_base_url, KEY_PLACEHOLDER),
            pilot=pilot,
            description="anthropic placeholders",
        )

        provider.value = "deepseek-openai"
        await wait_for(
            lambda: _placeholders(screen) == (PROVIDERS["deepseek-openai"].default_base_url, "Using DEEPSEEK_API_KEY"),
            pilot=pilot,
            description="deepseek placeholders",
        )


async def test_switching_profiles_updates_the_placeholders(
    fake_discover: FakeDiscover, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-env")
    registry = ModelProfileRegistry()
    registry.register(ModelProfile(id="a", name="A", provider="openai", model_id="m"))
    registry.register(ModelProfile(id="b", name="B", provider="anthropic", model_id="m"))

    async with open_model_config(registry, global_default_profile_id="a") as (screen, pilot):
        await wait_for(
            lambda: _placeholders(screen) == (PROVIDERS["openai"].default_base_url, KEY_PLACEHOLDER), pilot=pilot
        )

        await _select_profile(screen, pilot, "b")

        await wait_for(
            lambda: _placeholders(screen) == (PROVIDERS["anthropic"].default_base_url, "Using ANTHROPIC_API_KEY"),
            pilot=pilot,
        )


# ── Success: the list opens with focus in it ─────────────────────────


async def test_found_list_shows_the_arrow_and_opens_with_focus(fake_discover: FakeDiscover) -> None:
    fake_discover.respond = lambda _s: models("gpt-test", "gpt-other")
    registry, profile = single_profile_registry(base_url=URL, api_key="sk-a")

    async with open_model_config(registry, global_default_profile_id=profile.id) as (screen, pilot):
        base_width = model_input(screen).region.width

        await fetch_list(screen, pilot)

        assert arrow_shown(screen)
        await wait_for(lambda: overlay(screen).has_focus, pilot=pilot, description="focus in the list")
        assert_overlay_visible(screen)
        await wait_for(lambda: model_input(screen).region.width < base_width, pilot=pilot)
        assert model_input(screen).value == "gpt-test"
        assert screen.query_one("#mc-model-arrow").can_focus is False
        await wait_for(lambda: fetch_ready(screen), pilot=pilot)
        assert _hint(screen) is None


async def test_a_list_that_arrives_while_typing_elsewhere_does_not_take_focus(fake_discover: FakeDiscover) -> None:
    gate = asyncio.Event()
    fake_discover.hold = gate
    fake_discover.respond = lambda _s: models("gpt-test", "gpt-other")
    registry, profile = single_profile_registry(base_url=URL, api_key="sk-a", model_id="gpt-test")

    async with open_model_config(registry, global_default_profile_id=profile.id) as (screen, pilot):
        name = screen.query_one("#mc-name", Input)
        name_before = name.value
        try:
            await click_fetch(screen, pilot)
            await wait_for(lambda: len(fake_discover.calls) == 1, pilot=pilot)
            name.focus()
            await wait_for(lambda: name.has_focus, pilot=pilot)
            name.cursor_position = len(name.value)  # focus selects all; type after the text instead
            await pilot.press("a", "b")
        finally:
            gate.set()
        await wait_for(lambda: arrow_shown(screen) and fetch_ready(screen), pilot=pilot, description="list arrived")

        await pilot.press("c", "d")

        assert name.has_focus
        assert name.value == name_before + "abcd"
        assert model_input(screen).value == "gpt-test"
        assert not overlay_expanded(screen)


async def test_a_failure_does_not_pull_focus_back_from_another_field(fake_discover: FakeDiscover) -> None:
    gate = asyncio.Event()
    fake_discover.hold = gate
    registry, profile = single_profile_registry(base_url=URL, api_key="sk-a")

    async with open_model_config(registry, global_default_profile_id=profile.id) as (screen, pilot):
        button = fetch_button(screen)
        await wait_for(lambda: fetch_ready(screen), pilot=pilot, description="profile loaded")
        button.focus()
        await wait_for(lambda: button.has_focus, pilot=pilot)
        name = screen.query_one("#mc-name", Input)
        try:
            await pilot.press("enter")
            await wait_for(lambda: len(fake_discover.calls) == 1, pilot=pilot)
            name.focus()
            await wait_for(lambda: name.has_focus, pilot=pilot)
        finally:
            gate.set()

        await wait_for(lambda: _hint(screen) == NOT_FOUND_EN and fetch_ready(screen), pilot=pilot)
        await wait_until_quiet(lambda: screen.focused, description="focus settled", pilot=pilot)
        assert name.has_focus


async def test_empty_result_shows_no_arrow(fake_discover: FakeDiscover) -> None:
    registry, profile = single_profile_registry(base_url=URL, api_key="sk-a")

    async with open_model_config(registry, global_default_profile_id=profile.id) as (screen, pilot):
        await fetch_and_settle(screen, fake_discover, pilot)

        assert not arrow_shown(screen)
        assert not overlay_expanded(screen)
        assert input_fills_picker(screen)


# ── Failure: hints and focus ─────────────────────────────────────────


async def test_plain_not_found_shows_the_not_found_hint(fake_discover: FakeDiscover) -> None:
    registry, profile = single_profile_registry(base_url=URL, api_key="sk-a")

    async with open_model_config(registry, global_default_profile_id=profile.id) as (screen, pilot):
        await fetch_and_settle(screen, fake_discover, pilot)

        assert _hint(screen) == NOT_FOUND_EN
        assert screen.query_one("#mc-base-url", Input).value == URL
        assert not url_hinted(screen)
        assert not arrow_shown(screen)


async def test_progress_and_results_share_the_row_under_the_model_input(fake_discover: FakeDiscover) -> None:
    """The title never changes; the row under the model input shows muted progress, then the result."""
    gate = asyncio.Event()
    fake_discover.hold = gate
    registry, profile = single_profile_registry(base_url=URL, api_key="sk-a")

    async with open_model_config(registry, global_default_profile_id=profile.id) as (screen, pilot):
        try:
            await click_fetch(screen, pilot)
            await wait_for(lambda: fetching_frame(screen) is not None, pilot=pilot)
            assert model_label(screen) == BASE_MODEL_LABEL
            assert hint_row(screen, "#mc-model-hint") == f"{fetching_frame(screen)} {FETCHING_EN}"
            assert is_muted_progress(screen)
        finally:
            gate.set()
        await wait_for(lambda: _hint(screen) == NOT_FOUND_EN, pilot=pilot)
        # Results go to the row under the model line; the title keeps only its name.
        assert model_label(screen) == BASE_MODEL_LABEL
        assert hint_row(screen, "#mc-model-hint") == NOT_FOUND_EN
        assert is_warning_row(screen, "#mc-model-hint")

        fake_discover.respond = lambda _s: removed_v1()
        await fetch_and_settle(screen, fake_discover, pilot)
        assert url_label(screen) == "OpenAI Base URL"
        assert hint_row(screen, "#mc-url-hint") == URL_REMOVED_V1_EN
        assert model_label(screen) == BASE_MODEL_LABEL
        assert hint_row(screen, "#mc-model-hint") is None
        assert " · " in str(screen.query_one("#mc-max-tokens-label", Label).render())


async def test_failure_after_a_click_does_not_move_focus_to_another_button(fake_discover: FakeDiscover) -> None:
    """A clicked config button keeps no focus (ConfigActionButton); disabling it must not push focus elsewhere."""
    gate = asyncio.Event()
    fake_discover.hold = gate
    registry, profile = single_profile_registry(base_url=URL, api_key="sk-a")

    async with open_model_config(registry, global_default_profile_id=profile.id) as (screen, pilot):
        await click_fetch(screen, pilot)
        await wait_for(lambda: fetch_button(screen).disabled, pilot=pilot)
        gate.set()

        await wait_for(lambda: _hint(screen) == NOT_FOUND_EN, pilot=pilot)
        await wait_for(lambda: fetch_ready(screen), pilot=pilot)
        await wait_until_quiet(lambda: screen.focused, description="focus settled", pilot=pilot)
        assert screen.focused in (None, fetch_button(screen)), screen.focused


async def test_failure_keeps_focus_on_fetch_after_enter(fake_discover: FakeDiscover) -> None:
    registry, profile = single_profile_registry(base_url=URL, api_key="sk-a")

    async with open_model_config(registry, global_default_profile_id=profile.id) as (screen, pilot):
        button = fetch_button(screen)
        # As a user would: the button takes focus once the profile has loaded and enabled it.
        await wait_for(lambda: fetch_ready(screen), pilot=pilot, description="profile loaded")
        button.focus()
        await wait_for(lambda: button.has_focus, pilot=pilot)

        await pilot.press("enter")

        await wait_for(lambda: _hint(screen) == NOT_FOUND_EN, pilot=pilot)
        await wait_for(lambda: fetch_ready(screen) and button.has_focus, pilot=pilot, description="focus kept")


async def test_key_needed_when_neither_form_nor_env_has_a_key(fake_discover: FakeDiscover) -> None:
    fake_discover.respond = lambda _s: rejected()
    registry, profile = single_profile_registry(base_url=URL, api_key="")

    async with open_model_config(registry, global_default_profile_id=profile.id) as (screen, pilot):
        await fetch_and_settle(screen, fake_discover, pilot)

        assert _hint(screen) == KEY_NEEDED_EN
        assert not arrow_shown(screen)


async def test_key_rejected_when_the_form_has_a_key(fake_discover: FakeDiscover) -> None:
    fake_discover.respond = lambda _s: rejected()
    registry, profile = single_profile_registry(base_url=URL, api_key="sk-bad")

    async with open_model_config(registry, global_default_profile_id=profile.id) as (screen, pilot):
        await fetch_and_settle(screen, fake_discover, pilot)

        assert _hint(screen) == KEY_REJECTED_EN


@pytest.mark.parametrize("provider", ["openai", "anthropic"])
async def test_key_rejected_when_only_the_env_has_a_key(
    fake_discover: FakeDiscover, monkeypatch: pytest.MonkeyPatch, provider: str
) -> None:
    monkeypatch.setenv(PROVIDERS[provider].api_key_env, "sk-env")
    fake_discover.respond = lambda _s: rejected()
    registry, profile = single_profile_registry(provider=provider, base_url=URL, api_key="")

    async with open_model_config(registry, global_default_profile_id=profile.id) as (screen, pilot):
        await fetch_and_settle(screen, fake_discover, pilot)

        assert _hint(screen) == KEY_REJECTED_EN, model_label(screen)


async def test_another_providers_env_key_does_not_count(
    fake_discover: FakeDiscover, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-env")
    fake_discover.respond = lambda _s: rejected()
    registry, profile = single_profile_registry(provider="openai", base_url=URL, api_key="")

    async with open_model_config(registry, global_default_profile_id=profile.id) as (screen, pilot):
        await fetch_and_settle(screen, fake_discover, pilot)

        assert _hint(screen) == KEY_NEEDED_EN


# ── /v1 corrected by the lookup ──────────────────────────────────────


async def test_v1_fix_rewrites_the_base_url_and_opens_the_list(
    fake_discover: FakeDiscover, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_BASE_URL", "https://gw.example.test")
    typed, corrected, text = "", "https://gw.example.test/v1", URL_ADDED_V1_EN
    fake_discover.respond = lambda _s: v1_fixed("missing_v1", corrected, "m-1", "m-2")
    registry, profile = single_profile_registry(base_url=typed, api_key="sk-a", model_id="")

    async with open_model_config(registry, global_default_profile_id=profile.id) as (screen, pilot):
        await fetch_list(screen, pilot)
        await settle(screen, fake_discover, pilot)

        base_url = screen.query_one("#mc-base-url", Input)
        assert base_url.value == corrected
        assert arrow_shown(screen)
        assert overlay_expanded(screen)
        await wait_for(lambda: overlay(screen).has_focus, pilot=pilot)
        assert overlay_texts(screen) == ["m-1", "m-2"]
        assert url_hint(screen) == text, url_label(screen)
        assert is_warning_row(screen, "#mc-url-hint")
        assert _hint(screen) is None
        assert fetch_ready(screen)
        # Writing the corrected address is not a connection change: no clearing, no new lookup.
        await no_new_query(fake_discover, pilot, after=1)
        assert arrow_shown(screen)
        assert url_hint(screen) == text
        assert screen._build_profile_from_form().base_url == corrected
    stored = registry.get(profile.id)
    assert stored is not None
    assert stored.base_url == typed, "the correction is in the form only until the user saves"


# ── Nothing answered ─────────────────────────────────────────────────


async def test_no_response_shows_the_unreachable_hint(fake_discover: FakeDiscover) -> None:
    fake_discover.respond = lambda _s: no_response()
    registry, profile = single_profile_registry(base_url=URL, api_key="sk-a")

    async with open_model_config(registry, global_default_profile_id=profile.id) as (screen, pilot):
        await fetch_and_settle(screen, fake_discover, pilot)

        assert _hint(screen) == UNREACHABLE_EN
        assert _hint(screen) != NOT_FOUND_EN
        assert is_warning_row(screen, "#mc-model-hint")
        assert not arrow_shown(screen)
        assert url_hint(screen) is None
        # No automatic retry: the user clicks Fetch again.
        await no_new_query(fake_discover, pilot, after=1)
        assert fetch_ready(screen)


@pytest.mark.parametrize("api_key", ["", "sk-a"])
async def test_auth_rejection_wins_over_unreachable(fake_discover: FakeDiscover, api_key: str) -> None:
    fake_discover.respond = lambda _s: ModelDiscovery(models=[], auth_rejected=True, unreachable=True)
    registry, profile = single_profile_registry(base_url=URL, api_key=api_key)

    async with open_model_config(registry, global_default_profile_id=profile.id) as (screen, pilot):
        await fetch_and_settle(screen, fake_discover, pilot)

        assert _hint(screen) == (KEY_REJECTED_EN if api_key else KEY_NEEDED_EN)


async def test_url_hint_survives_an_api_style_change(fake_discover: FakeDiscover) -> None:
    fake_discover.respond = lambda _s: removed_v1()
    registry, profile = single_profile_registry(base_url=URL, api_key="sk-a")

    async with open_model_config(registry, global_default_profile_id=profile.id) as (screen, pilot):
        await fetch_and_settle(screen, fake_discover, pilot)
        assert url_hinted(screen)

        screen.query_one("#mc-api-style", Select).value = "responses"
        await wait_until_quiet(lambda: screen_state(screen), description="restyled", pilot=pilot)

        assert url_hint(screen) == URL_REMOVED_V1_EN
        assert arrow_shown(screen)
        await no_new_query(fake_discover, pilot, after=1)


@pytest.mark.parametrize(
    "first",
    [rejected(), ModelDiscovery(models=[]), no_response(), removed_v1()],
    ids=["key-hint", "not-found", "unreachable", "v1-fix"],
)
async def test_a_new_click_replaces_the_hints_with_progress(fake_discover: FakeDiscover, first: ModelDiscovery) -> None:
    fake_discover.respond = lambda _s: first
    registry, profile = single_profile_registry(base_url=URL, api_key="")

    async with open_model_config(registry, global_default_profile_id=profile.id) as (screen, pilot):
        await fetch_and_settle(screen, fake_discover, pilot)
        assert _hint(screen) is not None or url_hinted(screen)

        gate = asyncio.Event()
        fake_discover.hold = gate
        await click_fetch(screen, pilot)
        await wait_for(lambda: len(fake_discover.calls) == 2, pilot=pilot)

        await wait_for(
            lambda: fetching_frame(screen) is not None and not url_hinted(screen),
            pilot=pilot,
            description="old hints replaced by the progress hint",
        )
        gate.set()
        await settle(screen, fake_discover, pilot)
        assert fetching_frame(screen) is None
        assert _hint(screen) is not None or url_hinted(screen)


@pytest.mark.parametrize(
    "first",
    [rejected(), ModelDiscovery(models=[]), no_response(), removed_v1()],
    ids=["key-hint", "not-found", "unreachable", "v1-fix"],
)
async def test_a_found_list_clears_the_hints(fake_discover: FakeDiscover, first: ModelDiscovery) -> None:
    fake_discover.respond = lambda _s: first
    registry, profile = single_profile_registry(base_url=URL, api_key="")

    async with open_model_config(registry, global_default_profile_id=profile.id) as (screen, pilot):
        await fetch_and_settle(screen, fake_discover, pilot)

        fake_discover.respond = lambda _s: models("m")
        await fetch_and_settle(screen, fake_discover, pilot)

        assert arrow_shown(screen)
        assert _hint(screen) is None
        assert not url_hinted(screen)


async def test_profile_switch_clears_the_hints(fake_discover: FakeDiscover) -> None:
    fake_discover.respond = lambda _s: v1_fixed("extra_v1", "https://a.example.test", "m")
    registry = _two_profiles()

    async with open_model_config(registry, global_default_profile_id="a") as (screen, pilot):
        await fetch_and_settle(screen, fake_discover, pilot)
        assert url_hinted(screen)
        fake_discover.respond = lambda _s: rejected()
        await fetch_and_settle(screen, fake_discover, pilot)
        assert _hint(screen) == KEY_REJECTED_EN

        await _select_profile(screen, pilot, "b")

        await wait_for(lambda: _hint(screen) is None and not url_hinted(screen), pilot=pilot)
        await no_new_query(fake_discover, pilot, after=2)


# ── Credentials the lookup must not run ──────────────────────────────


@pytest.mark.parametrize("where", ["api_key", "http_headers", "extra_headers"])
async def test_command_credential_is_not_run_and_shows_not_found(
    real_discover: RealDiscover, tmp_path: Path, where: str
) -> None:
    marker = tmp_path / "ran"
    command = f"{{{{!touch {marker}}}}}"
    fields: dict[str, Any] = {"base_url": URL, "api_key": "sk-a"}
    if where == "api_key":
        fields["api_key"] = command
    elif where == "http_headers":
        # json.dumps escapes the Windows path's backslashes, which a hand-written JSON string would not.
        fields["http_headers"] = json.dumps({"X-Token": command})
    else:
        fields["chat_options"] = json.dumps({"extra_headers": {"X-Token": command}})
    registry, profile = single_profile_registry(**fields)

    async with open_model_config(registry, global_default_profile_id=profile.id) as (screen, pilot):
        await click_fetch(screen, pilot)

        await wait_for(lambda: _hint(screen) == NOT_FOUND_EN, pilot=pilot, description="not-found hint")
        await wait_for(lambda: fetch_ready(screen), pilot=pilot)
        assert real_discover.requests == []
        assert not marker.exists()
        assert not arrow_shown(screen)


async def test_changing_the_key_to_a_command_clears_the_list(fake_discover: FakeDiscover) -> None:
    fake_discover.respond = lambda _s: models("a")
    registry, profile = single_profile_registry(base_url=URL, api_key="sk-a")

    async with open_model_config(registry, global_default_profile_id=profile.id) as (screen, pilot):
        await fetch_list(screen, pilot)

        screen.query_one("#mc-api-key", Input).value = "{{!pass show key}}"

        await wait_for(lambda: not arrow_shown(screen), pilot=pilot)
        await no_new_query(fake_discover, pilot, after=1)


@pytest.mark.parametrize("failure", ["invalid-extra-headers", "missing-key-env"])
async def test_invalid_connection_shows_not_found_without_requests(
    real_discover: RealDiscover, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    monkeypatch.delenv("ML_SCREEN_MISSING_VAR", raising=False)
    key = "{{ML_SCREEN_MISSING_VAR}}" if failure == "missing-key-env" else "sk-a"
    registry, profile = single_profile_registry(base_url=URL, api_key=key)

    async with open_model_config(registry, global_default_profile_id=profile.id) as (screen, pilot):
        if failure == "invalid-extra-headers":
            options = screen.query_one("#mc-options-list")
            await _wait_for_kv_rows(options, pilot, 1)
            fill_kv_row(options, "extra_headers", "[1, 2]")

        await click_fetch(screen, pilot)

        await wait_for(lambda: _hint(screen) == NOT_FOUND_EN, pilot=pilot, description="not-found hint")
        await wait_for(lambda: fetch_ready(screen), pilot=pilot)
        assert real_discover.requests == []
        assert pilot.app.is_running


# ── Connection changes clear and never re-query ──────────────────────


HEADERS = '{"X-One": "1", "X-Two": "2"}'

Change = Callable[[ModelConfigScreen, Pilot], Awaitable[None]]


async def _change_provider(screen: ModelConfigScreen, _pilot: Pilot) -> None:
    screen.query_one("#mc-provider", Select).value = "anthropic"


async def _change_url(screen: ModelConfigScreen, pilot: Pilot) -> None:
    await type_into(pilot, screen.query_one("#mc-base-url", Input), "x")


async def _clear_url(screen: ModelConfigScreen, pilot: Pilot) -> None:
    url = screen.query_one("#mc-base-url", Input)
    url.focus()
    await wait_for(lambda: url.has_focus, pilot=pilot)
    await focus_handled(url, pilot)
    await pilot.press("ctrl+a", "backspace")


async def _change_key(screen: ModelConfigScreen, pilot: Pilot) -> None:
    await type_into(pilot, screen.query_one("#mc-api-key", Input), "b")


async def _edit_header(screen: ModelConfigScreen, _pilot: Pilot) -> None:
    fill_kv_row(screen.query_one("#mc-headers-list"), "X-Team", "platform")


async def _remove_header(screen: ModelConfigScreen, _pilot: Pilot) -> None:
    screen.query_one("#mc-headers-list").query(".mc-kv-item-row").first().query_one(".mc-kv-remove-btn", Button).press()


async def _add_header(screen: ModelConfigScreen, pilot: Pilot) -> None:
    headers = screen.query_one("#mc-headers-list")
    screen.query_one("#mc-hadd", Button).press()
    await _wait_for_kv_rows(headers, pilot, 3)
    fill_kv_row(headers, "X-Three", "3", index=2)


async def _change_extra_headers(screen: ModelConfigScreen, _pilot: Pilot) -> None:
    fill_kv_row(screen.query_one("#mc-options-list"), "extra_headers", '{"X-Opt": "1"}')


async def _toggle_tls(screen: ModelConfigScreen, _pilot: Pilot) -> None:
    box = screen.query_one("#mc-skip-tls", Checkbox)
    box.value = not box.value


async def _toggle_proxy(screen: ModelConfigScreen, _pilot: Pilot) -> None:
    box = screen.query_one("#mc-bypass-proxy", Checkbox)
    box.value = not box.value


async def _change_connect_timeout(screen: ModelConfigScreen, pilot: Pilot) -> None:
    await type_into(pilot, screen.query_one("#mc-connect-timeout", Input), "5")


async def _change_max_retries(screen: ModelConfigScreen, pilot: Pilot) -> None:
    await type_into(pilot, screen.query_one("#mc-max-retries", Input), "1")


CONNECTION_CHANGES = pytest.mark.parametrize(
    "change",
    [
        _change_provider,
        _change_url,
        _clear_url,
        _change_key,
        _edit_header,
        _remove_header,
        _add_header,
        _change_extra_headers,
        _toggle_tls,
        _toggle_proxy,
        _change_connect_timeout,
        _change_max_retries,
    ],
    ids=[
        "provider",
        "url",
        "clear-url",
        "key",
        "edit-header",
        "remove-header",
        "add-header",
        "extra-headers",
        "tls",
        "proxy",
        "connect-timeout",
        "max-retries",
    ],
)


@contextlib.asynccontextmanager
async def _connection_form() -> AsyncIterator[tuple[ModelConfigScreen, Pilot]]:
    registry, profile = single_profile_registry(base_url=URL, api_key="sk-a", http_headers=HEADERS, model_id="")
    async with open_model_config(registry, global_default_profile_id=profile.id) as (screen, pilot):
        await _wait_for_kv_rows(screen.query_one("#mc-headers-list"), pilot, 2)
        await _wait_for_kv_rows(screen.query_one("#mc-options-list"), pilot, 1)
        yield screen, pilot


@pytest.mark.parametrize("change", [_change_key])
@pytest.mark.parametrize(
    "first",
    [rejected(), ModelDiscovery(models=[]), no_response(), removed_v1()],
    ids=["key-hint", "not-found", "unreachable", "v1-fix"],
)
async def test_connection_change_clears_the_hints(
    fake_discover: FakeDiscover, change: Change, first: ModelDiscovery
) -> None:
    fake_discover.respond = lambda _s: first

    async with _connection_form() as (screen, pilot):
        await fetch_and_settle(screen, fake_discover, pilot)
        assert _hint(screen) is not None or url_hinted(screen)

        await change(screen, pilot)

        await wait_for(
            lambda: _hint(screen) is None and not url_hinted(screen), pilot=pilot, description="hints cleared"
        )
        await no_new_query(fake_discover, pilot, after=1)


async def test_a_result_landing_before_the_url_change_event_does_not_rewrite_the_new_url(
    fake_discover: FakeDiscover,
) -> None:
    """The new Base URL is in the input but its Changed has not reached the screen when the result lands."""
    gate = asyncio.Event()
    fake_discover.hold = gate
    fake_discover.respond = lambda _s: removed_v1()
    registry, profile = single_profile_registry(base_url=URL, api_key="sk-a", model_id="")
    new_url = "https://new.example.test/v1"

    async with open_model_config(registry, global_default_profile_id=profile.id) as (screen, pilot):
        url = screen.query_one("#mc-base-url", Input)
        try:
            await click_fetch(screen, pilot)
            await wait_for(lambda: len(fake_discover.calls) == 1, pilot=pilot)
            worker = screen._model_list_worker
            assert worker is not None and worker.is_running
            # Hold back the Changed event so only the result can react to the new value.
            with url.prevent(Input.Changed):
                url.value = new_url
        finally:
            gate.set()
        await wait_for(lambda: worker.is_finished and fetch_ready(screen), pilot=pilot, description="result handled")

        assert url.value == new_url
        assert not arrow_shown(screen)
        assert not url_hinted(screen)
        assert _hint(screen) is None
        assert fetching_frame(screen) is None


@CONNECTION_CHANGES
async def test_connection_change_cancels_a_running_fetch(fake_discover: FakeDiscover, change: Change) -> None:
    fake_discover.respond = lambda _s: removed_v1()

    async with _connection_form() as (screen, pilot):
        await fetch_list(screen, pilot)
        assert url_hinted(screen)
        gate = asyncio.Event()
        fake_discover.hold = gate
        try:
            await click_fetch(screen, pilot)
            await wait_for(lambda: len(fake_discover.calls) == 2, pilot=pilot)
            assert not gate.is_set() and fake_discover.finished == 1
            worker = screen._model_list_worker
            assert worker is not None and worker.is_running

            assert fetching_frame(screen) is not None

            await change(screen, pilot)
            before_pending = screen.query_one("#mc-base-url", Input).value

            await wait_for(lambda: fetch_ready(screen), pilot=pilot, description="button restored")
            await wait_for(lambda: _hint(screen) is None, pilot=pilot, description="progress hint cleared")
            await wait_for(lambda: worker.state is WorkerState.CANCELLED, pilot=pilot)
            assert fake_discover.finished == 2 and not gate.is_set()
        finally:
            fake_discover.hold = None
            gate.set()
        await settle(screen, fake_discover, pilot)

        assert not arrow_shown(screen)
        assert not overlay_expanded(screen)
        assert _hint(screen) is None
        assert not url_hinted(screen)
        # A dropped result never rewrites the Base URL.
        assert screen.query_one("#mc-base-url", Input).value == before_pending
        await no_new_query(fake_discover, pilot, after=2)


async def test_connection_change_with_fetch_disabled_by_no_address_leaves_it_disabled(
    fake_discover: FakeDiscover, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The button's restored state after a change is the resolver's verdict, not just "enabled"."""
    _without_default_url(monkeypatch, "openai")
    gate = asyncio.Event()
    fake_discover.hold = gate
    registry, profile = single_profile_registry(base_url=URL, api_key="sk-a")

    async with open_model_config(registry, global_default_profile_id=profile.id) as (screen, pilot):
        try:
            await click_fetch(screen, pilot)
            await wait_for(lambda: len(fake_discover.calls) == 1, pilot=pilot)
            assert not gate.is_set() and fake_discover.finished == 0
            worker = screen._model_list_worker
            assert worker is not None and worker.is_running

            await _clear_url(screen, pilot)

            await wait_for(
                lambda: fetch_button(screen).disabled and str(fetch_button(screen).label) == FETCH_EN,
                pilot=pilot,
                description="idle label, still disabled: nothing to query",
            )
            await wait_for(lambda: worker.state is WorkerState.CANCELLED, pilot=pilot)
            assert fake_discover.finished == 1 and not gate.is_set()
        finally:
            fake_discover.hold = None
            gate.set()


@pytest.mark.parametrize(
    "change",
    ["#mc-name", "#mc-max-tokens", "#mc-max-output-tokens", "#mc-model", "api-style", "#mc-stream"],
)
async def test_non_connection_changes_keep_the_list(fake_discover: FakeDiscover, change: str) -> None:
    fake_discover.respond = lambda _s: models("a", "b")
    registry, profile = single_profile_registry(base_url=URL, api_key="sk-a", model_id="")

    async with open_model_config(registry, global_default_profile_id=profile.id) as (screen, pilot):
        await fetch_list(screen, pilot)
        await _close_list(screen, pilot)

        if change == "api-style":
            screen.query_one("#mc-api-style", Select).value = "responses"
        elif change == "#mc-stream":
            box = screen.query_one(change, Checkbox)
            box.value = not box.value
        else:
            screen.query_one(change, Input).value = "1234"
        await wait_until_quiet(lambda: screen_state(screen), description="change applied", pilot=pilot)

        assert arrow_shown(screen)
        await no_new_query(fake_discover, pilot, after=1)


# ── Lifecycle of a running fetch ─────────────────────────────────────


async def test_switching_profiles_during_a_fetch_drops_its_result(fake_discover: FakeDiscover) -> None:
    fake_discover.respond = lambda _s: models("late")
    registry = _two_profiles()

    async with open_model_config(registry, global_default_profile_id="a") as (screen, pilot):
        gate = asyncio.Event()
        fake_discover.hold = gate
        try:
            await click_fetch(screen, pilot)
            await wait_for(lambda: len(fake_discover.calls) == 1, pilot=pilot)
            assert not gate.is_set() and fake_discover.finished == 0
            worker = screen._model_list_worker
            assert worker is not None and worker.is_running

            await _select_profile(screen, pilot, "b")
            await wait_for(lambda: fetch_ready(screen), pilot=pilot, description="button restored")
            await wait_for(lambda: worker.state is WorkerState.CANCELLED, pilot=pilot)
            assert fake_discover.finished == 1 and not gate.is_set()
        finally:
            fake_discover.hold = None
            gate.set()
        await settle(screen, fake_discover, pilot)

        assert not arrow_shown(screen)
        assert _hint(screen) is None
        await no_new_query(fake_discover, pilot, after=1)


async def test_closing_the_dialog_cancels_a_running_fetch(fake_discover: FakeDiscover) -> None:
    gate = asyncio.Event()
    fake_discover.hold = gate
    fake_discover.respond = lambda _s: models("late")
    registry, profile = single_profile_registry(base_url=URL, api_key="sk-a")

    async with open_model_config(registry, global_default_profile_id=profile.id) as (screen, pilot):
        try:
            await click_fetch(screen, pilot)
            await wait_for(lambda: len(fake_discover.calls) == 1, pilot=pilot)
            assert not gate.is_set() and fake_discover.finished == 0
            worker = screen._model_list_worker
            assert worker is not None and worker.is_running

            await pilot.press("escape")
            await wait_for(lambda: pilot.app.screen is not screen, pilot=pilot, description="dialog closed")
            await wait_for(lambda: worker.state is WorkerState.CANCELLED, pilot=pilot)
            assert fake_discover.finished == 1 and not gate.is_set()
        finally:
            gate.set()
        await pilot.pause()

        assert pilot.app.is_running
        assert pilot.app.return_code is None
        assert not arrow_shown(screen)


class LateAnsweringDiscover:
    """The lookup for ``sk-old`` ignores cancellation and answers only after ``late`` is set.

    Every other key answers ``fresh`` at once. This produces a real stale result for the
    screen to drop, instead of a cancelled call that never returns.
    """

    def __init__(self, stale: ModelDiscovery) -> None:
        self.calls: list[ConnectionSnapshot] = []
        self.late = asyncio.Event()
        self.stale = stale
        self.stale_returned = False
        self.cancel_ignored = 0

    async def __call__(
        self,
        snapshot: ConnectionSnapshot,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        on_retry: Callable[[int, int], None] | None = None,
    ) -> ModelDiscovery:
        self.calls.append(snapshot)
        if snapshot.api_key != "sk-old":
            return ModelDiscovery(models=models("fresh"))
        while not self.late.is_set():
            try:
                await self.late.wait()
            except asyncio.CancelledError:
                self.cancel_ignored += 1
        self.stale_returned = True
        return self.stale


@pytest.mark.parametrize("stale", [v1_fixed("missing_v1", "https://stale.example.test/v1", "stale"), rejected()])
async def test_a_superseded_fetch_that_still_answers_is_dropped(
    monkeypatch: pytest.MonkeyPatch, stale: ModelDiscovery
) -> None:
    fake = LateAnsweringDiscover(stale)
    monkeypatch.setattr(ModelConfigScreen, "discover_models", staticmethod(fake))
    registry, profile = single_profile_registry(base_url=URL, api_key="sk-old", model_id="")

    async with open_model_config(registry, global_default_profile_id=profile.id) as (screen, pilot):
        try:
            await click_fetch(screen, pilot)
            await wait_for(lambda: fake.calls, pilot=pilot, description="old fetch running")

            screen.query_one("#mc-api-key", Input).value = "sk-new"
            await wait_for(lambda: fetch_ready(screen), pilot=pilot, description="button restored")
            await fetch_list(screen, pilot)
            assert overlay_texts(screen) == ["fresh"]
        finally:
            # Always let the old fetch finish before the app closes: it ignores cancellation.
            fake.late.set()
        await wait_for(lambda: fake.stale_returned, pilot=pilot, description="stale fetch answered")
        assert fake.cancel_ignored >= 1
        await wait_until_quiet(lambda: screen_state(screen), description="stale answer handled", pilot=pilot)

        assert await listed(screen, pilot) == ["fresh"]
        assert _hint(screen) is None
        assert not url_hinted(screen)
        assert screen.query_one("#mc-base-url", Input).value == URL
        assert fetch_ready(screen)


# ── Provider-dependent labels after loading ──────────────────────────


def _assert_provider_labels(screen: ModelConfigScreen, provider: str, words: str) -> None:
    assert words in model_label(screen), model_label(screen)
    assert words in url_label(screen), url_label(screen)
    assert screen.query_one("#mc-base-url", Input).placeholder == PROVIDERS[provider].default_base_url
    assert screen.query_one("#mc-provider", Select).value == provider


async def _settle_labels(screen: ModelConfigScreen, pilot: Pilot, provider: str, words: str) -> None:
    def matches() -> bool:
        try:
            _assert_provider_labels(screen, provider, words)
        except AssertionError:
            return False
        return True

    await wait_for(matches, pilot=pilot, description=f"labels for {provider}")
    await wait_until_quiet(lambda: screen_state(screen), description="form settled", pilot=pilot)
    # A queued change from the load must not flip them back once the form settles.
    _assert_provider_labels(screen, provider, words)


@pytest.mark.parametrize("base_url", ["", URL])
async def test_opening_a_deepseek_profile_shows_its_labels_and_tls_hint(
    fake_discover: FakeDiscover, base_url: str
) -> None:
    registry, profile = single_profile_registry(provider="deepseek-openai", base_url=base_url, verify_ssl=False)

    async with open_model_config(registry, global_default_profile_id=profile.id) as (screen, pilot):
        await _settle_labels(screen, pilot, "deepseek-openai", "DeepSeek")

        assert screen.query_one("#mc-skip-tls", Checkbox).value is True
        assert screen.query_one("#mc-skip-tls-hint", Label).display is True


async def test_switching_between_openai_and_deepseek_profiles_updates_labels(fake_discover: FakeDiscover) -> None:
    registry = ModelProfileRegistry()
    registry.register(ModelProfile(id="a", name="A", provider="openai", model_id="m", base_url=URL))
    registry.register(
        ModelProfile(id="b", name="B", provider="deepseek-openai", model_id="m", base_url=URL, verify_ssl=False)
    )

    async with open_model_config(registry, global_default_profile_id="a") as (screen, pilot):
        await _settle_labels(screen, pilot, "openai", "OpenAI")
        assert screen.query_one("#mc-skip-tls-hint", Label).display is False

        await _select_profile(screen, pilot, "b")
        await _settle_labels(screen, pilot, "deepseek-openai", "DeepSeek")
        assert screen.query_one("#mc-skip-tls-hint", Label).display is True

        await _select_profile(screen, pilot, "a")
        await _settle_labels(screen, pilot, "openai", "OpenAI")
        assert "DeepSeek" not in model_label(screen)
        assert screen.query_one("#mc-skip-tls-hint", Label).display is False
        await no_new_query(fake_discover, pilot, after=0)


async def test_provider_switch_rewrites_the_labels_and_drops_the_url_hint(fake_discover: FakeDiscover) -> None:
    fake_discover.respond = lambda _s: removed_v1()
    registry, profile = single_profile_registry(base_url=URL, api_key="sk-a")

    async with open_model_config(registry, global_default_profile_id=profile.id) as (screen, pilot):
        await fetch_and_settle(screen, fake_discover, pilot)
        assert url_hinted(screen)

        screen.query_one("#mc-provider", Select).value = "deepseek-openai"

        await wait_for(lambda: "DeepSeek" in url_label(screen) and not url_hinted(screen), pilot=pilot)
        await wait_until_quiet(lambda: screen_state(screen), description="provider switched", pilot=pilot)
        assert not url_hinted(screen)
        await no_new_query(fake_discover, pilot, after=1)


@pytest.mark.parametrize("action", ["open", "switch", "new", "clone", "type"])
async def test_form_actions_do_not_automatically_query(fake_discover: FakeDiscover, action: str) -> None:
    fake_discover.respond = lambda _s: models("m1", "a-model")
    async with open_model_config(_two_profiles(), global_default_profile_id="a") as (screen, pilot):
        await wait_for(lambda: fetch_ready(screen), pilot=pilot)
        assert input_fills_picker(screen)
        count = 0
        if action in {"switch", "new", "clone"}:
            await fetch_list(screen, pilot)
            count = 1
            if action == "switch":
                await _select_profile(screen, pilot, "b")
            else:
                await click_when_settled(pilot, f"#mc-{action}")
                await wait_for(lambda: screen._selected_profile_id != "a", pilot=pilot)
        elif action == "type":
            await type_into(pilot, screen.query_one("#mc-base-url", Input), "x")
            await pilot.press("enter")
            await type_into(pilot, screen.query_one("#mc-api-key", Input), "b")
            await pilot.press("tab")
        await no_new_query(fake_discover, pilot, after=count)
        assert not arrow_shown(screen)
        assert not overlay_expanded(screen)
        assert model_label(screen) == BASE_MODEL_LABEL
        assert not url_hinted(screen)
        assert fetch_ready(screen)


@pytest.mark.parametrize("kind", ["last-header", "add-extra-headers", "remove-extra-headers"])
async def test_header_row_structure_changes_clear_the_list(fake_discover: FakeDiscover, kind: str) -> None:
    fake_discover.respond = lambda _s: models("a")
    fields = (
        {"http_headers": '{"X-One": "1"}'}
        if kind == "last-header"
        else {"chat_options": '{"extra_headers": {"X-One": "1"}}'}
    )
    registry, profile = single_profile_registry(base_url=URL, api_key="sk-a", model_id="", **fields)
    async with open_model_config(registry, global_default_profile_id=profile.id) as (screen, pilot):
        options = screen.query_one("#mc-options-list")
        await _wait_for_kv_rows(options, pilot, 1)
        if kind == "add-extra-headers":
            # Adding an empty draft does not affect the connection; filling it does.
            fill_kv_row(options, "temperature", "0.5")
        await fetch_list(screen, pilot)
        if kind == "last-header":
            container = screen.query_one("#mc-headers-list")
            container.query_one(".mc-kv-remove-btn", Button).press()
            await wait_for(
                lambda: (
                    len(container.query(".mc-kv-item-row")) == 1
                    and not container.query_one(".mc-kv-key-input", Input).value
                ),
                pilot=pilot,
            )
            assert screen._read_kv_list("mc-headers-list") == {}
        elif kind == "add-extra-headers":
            screen.query_one("#mc-oadd", Button).press()
            await _wait_for_kv_rows(options, pilot, 2)
            assert arrow_shown(screen)
            fill_kv_row(options, "extra_headers", '{"X-New": "2"}', index=1)
        else:
            options.query_one(".mc-kv-remove-btn", Button).press()
            await wait_for(lambda: not screen._read_kv_list("mc-options-list"), pilot=pilot)
        await wait_for(lambda: not arrow_shown(screen) and not overlay_expanded(screen), pilot=pilot)
        assert _hint(screen) is None
        await no_new_query(fake_discover, pilot, after=1)


async def test_removed_screen_ignores_late_row_changes(fake_discover: FakeDiscover) -> None:
    registry, profile = single_profile_registry(base_url=URL)
    async with open_model_config(registry, global_default_profile_id=profile.id) as (screen, pilot):
        await pilot.app.pop_screen()
        await wait_for(lambda: not screen._composed_live, pilot=pilot)
        screen._model_connection_rows_changed("mc-options-list")
        assert fake_discover.calls == []


async def test_fetch_spinner_does_not_repaint_under_another_screen(fake_discover: FakeDiscover) -> None:
    from textual.screen import ModalScreen

    gate = asyncio.Event()
    fake_discover.hold = gate
    registry, profile = single_profile_registry(base_url=URL)
    async with open_model_config(registry, global_default_profile_id=profile.id) as (screen, pilot):
        pilot.app.animation_level = "full"
        try:
            await click_fetch(screen, pilot)
            await wait_for(lambda: fetching_frame(screen) is not None, pilot=pilot)
            covered = ModalScreen()
            await pilot.app.push_screen(covered)
            frame = screen._fetch_frame
            screen._advance_fetch_spinner()
            assert screen._fetch_frame == frame
            await pilot.app.pop_screen()
            await wait_for(lambda: screen._fetch_frame != frame, pilot=pilot, description="visible spinner resumes")
        finally:
            gate.set()
