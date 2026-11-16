# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Spec tests for the warning rows that carry the model-list results on the configuration screen.

Written from ``.plan/model-list-interface.md``: ``#mc-url-hint`` sits right under the Base URL input
and ``#mc-model-hint`` right under the model line; they reuse the TLS warning look, start their text
where the input text above starts, take no space when empty and wrap instead of truncating. Fetching and retry progress share the model hint row in a muted colour.
"""

from __future__ import annotations

import asyncio

import pytest
from textual.widgets import Button, Input, Label

from chrys.service.llm.model_list import ModelDiscovery
from tests.app.tui.screens._model_config_support import open_model_config, single_profile_registry
from tests.app.tui.screens._model_list_support import (
    FETCHING_EN,
    FETCHING_ZH,
    KEY_REJECTED_EN,
    KEY_REJECTED_ZH,
    NOT_FOUND_EN,
    NOT_FOUND_ZH,
    QUIET_WINDOW,
    RETRYING_EN,
    RETRYING_ZH,
    UNREACHABLE_EN,
    UNREACHABLE_ZH,
    URL,
    FakeDiscover,
    _hint,
    click_fetch,
    fetch_and_settle,
    fetching_frame,
    hint_row,
    is_muted_progress,
    is_warning_row,
    model_input,
    model_label,
    no_response,
    open_model_config_zh,
    rejected,
    removed_v1,
    row_shows_whole,
    screen_state,
    settle,
)
from tests.support.waiting import wait_for, wait_until, wait_until_quiet

pytestmark = pytest.mark.usefixtures("isolated_model_config_dir", "clear_model_profile_env", "clear_provider_env")


async def test_hint_rows_appear_under_their_inputs_and_take_no_space_when_empty(fake_discover: FakeDiscover) -> None:
    fake_discover.respond = lambda _s: ModelDiscovery(models=[])
    registry, profile = single_profile_registry(base_url=URL, api_key="sk-a")

    async with open_model_config(registry, global_default_profile_id=profile.id) as (screen, pilot):
        await wait_until_quiet(lambda: screen_state(screen), description="form loaded", pilot=pilot)
        line = screen.query_one("#mc-model-line")
        max_tokens_label = screen.query_one("#mc-max-tokens-label", Label)
        base_url = screen.query_one("#mc-base-url", Input)
        key_label = screen.query_one("#mc-key-label", Label)
        before = (max_tokens_label.region.y, key_label.region.y)
        assert hint_row(screen, "#mc-model-hint") is None
        assert hint_row(screen, "#mc-url-hint") is None

        await fetch_and_settle(screen, fake_discover, pilot)
        model_row = screen.query_one("#mc-model-hint", Label)
        await wait_until_quiet(lambda: model_row.region, description="hint row laid out", pilot=pilot)
        assert model_row.region.y == line.region.bottom
        # The hint's text starts where the model input's text starts.
        assert model_row.content_region.x == model_input(screen).content_region.x
        assert max_tokens_label.region.y > before[0]
        assert screen.query_one("#mc-model-label", Label).region.height == 1

        fake_discover.respond = lambda _s: removed_v1()
        await fetch_and_settle(screen, fake_discover, pilot)
        url_row = screen.query_one("#mc-url-hint", Label)
        await wait_until_quiet(lambda: url_row.region, description="url hint laid out", pilot=pilot)
        assert hint_row(screen, "#mc-model-hint") is None
        assert url_row.region.y == base_url.region.bottom
        assert url_row.content_region.x == base_url.content_region.x
        assert key_label.region.y > before[1]

        screen.query_one("#mc-api-key", Input).value = "sk-b"
        await wait_for(lambda: hint_row(screen, "#mc-url-hint") is None, pilot=pilot)
        await wait_until_quiet(lambda: (max_tokens_label.region, key_label.region), description="relaid", pilot=pilot)
        assert (max_tokens_label.region.y, key_label.region.y) == before


@pytest.mark.parametrize(
    ("locale", "answer", "api_key", "text"),
    [
        ("en", no_response(), "sk-a", UNREACHABLE_EN),
        ("zh-Hans", no_response(), "sk-a", UNREACHABLE_ZH),
        ("en", rejected(), "sk-a", KEY_REJECTED_EN),
        ("zh-Hans", rejected(), "sk-a", KEY_REJECTED_ZH),
        ("zh-Hans", rejected(), "", "可尝试填写 API Key"),
    ],
)
async def test_hint_rows_wrap_instead_of_truncating_at_80_columns(
    fake_discover: FakeDiscover, locale: str, answer: ModelDiscovery, api_key: str, text: str
) -> None:
    fake_discover.respond = lambda _s: answer
    registry, profile = single_profile_registry(provider="deepseek-openai", base_url=URL, api_key=api_key)
    opener = open_model_config_zh if locale == "zh-Hans" else open_model_config
    row = "#mc-model-hint"

    async with opener(registry, global_default_profile_id=profile.id, size=(80, 30)) as (screen, pilot):
        await click_fetch(screen, pilot)
        await wait_for(lambda: hint_row(screen, row) == text, pilot=pilot, description="hint row")
        await settle(screen, fake_discover, pilot)

        assert row_shows_whole(screen, row, text), [screen.query_one(row).render_line(y).text for y in range(3)]
        assert screen.query_one("#mc-model-label", Label).region.height == 1


# ── Retry rounds in the progress row ────────────────────────────────


@pytest.mark.parametrize("locale", ["en", "zh-Hans"])
async def test_retry_rounds_show_in_the_muted_progress_row(fake_discover: FakeDiscover, locale: str) -> None:
    gate = asyncio.Event()
    fake_discover.hold = gate
    registry, profile = single_profile_registry(base_url=URL, api_key="sk-a")
    opener = open_model_config_zh if locale == "zh-Hans" else open_model_config
    fetching, retrying, not_found = (
        (FETCHING_ZH, RETRYING_ZH, NOT_FOUND_ZH) if locale == "zh-Hans" else (FETCHING_EN, RETRYING_EN, NOT_FOUND_EN)
    )

    async with opener(registry, global_default_profile_id=profile.id) as (screen, pilot):
        try:
            await click_fetch(screen, pilot)
            await wait_for(lambda: fetching_frame(screen, fetching) is not None, pilot=pilot, description="fetching")
            button = screen.query_one("#mc-model-fetch", Button)
            assert str(button.label) == ("获取列表" if locale == "zh-Hans" else "Fetch")
            assert button.disabled
            line = screen.query_one("#mc-model-line")
            model = model_input(screen)
            await wait_until_quiet(
                lambda: (line.region, button.region, model.region), description="fetch row laid out", pilot=pilot
            )
            regions = (line.region, button.region, model.region)
            assert line.region.height == 1 and button.region.height == 1
            report = fake_discover.on_retry[-1]
            assert report is not None, "the screen passes a progress callback"

            for round_number in (2, 3):
                report(round_number, 5)
                text = retrying.format(round=round_number, rounds=5)
                await wait_for(
                    lambda text=text: fetching_frame(screen, text) is not None, pilot=pilot, description=text
                )
                assert is_muted_progress(screen)
                assert text not in model_label(screen), "the title never carries progress"
                assert (line.region, button.region, model.region) == regions
        finally:
            gate.set()

        await wait_for(lambda: _hint(screen) == not_found, pilot=pilot, description="result replaces progress")
        await wait_until_quiet(
            lambda: (line.region, button.region, model.region), description="result row laid out", pilot=pilot
        )
        assert (line.region, button.region, model.region) == regions
        assert is_warning_row(screen, "#mc-model-hint")


async def test_a_superseded_fetch_cannot_report_retry_rounds(fake_discover: FakeDiscover) -> None:
    gate = asyncio.Event()
    fake_discover.hold = gate
    registry, profile = single_profile_registry(base_url=URL, api_key="sk-a")

    async with open_model_config(registry, global_default_profile_id=profile.id) as (screen, pilot):
        try:
            await click_fetch(screen, pilot)
            await wait_for(lambda: len(fake_discover.calls) == 1, pilot=pilot)
            stale_report = fake_discover.on_retry[0]
            assert stale_report is not None

            screen.query_one("#mc-api-key", Input).value = "sk-b"
            await wait_for(lambda: _hint(screen) is None, pilot=pilot, description="connection change clears")
            await click_fetch(screen, pilot)
            await wait_for(lambda: len(fake_discover.calls) == 2, pilot=pilot)
            await wait_for(lambda: fetching_frame(screen) is not None, pilot=pilot)

            stale_report(4, 5)
            changed = await wait_until(lambda: fetching_frame(screen) is None, timeout=QUIET_WINDOW, pilot=pilot)
            assert not changed, _hint(screen)
        finally:
            gate.set()


async def test_each_click_starts_without_a_retry_round(fake_discover: FakeDiscover) -> None:
    gate = asyncio.Event()
    fake_discover.hold = gate
    registry, profile = single_profile_registry(base_url=URL, api_key="sk-a")

    async with open_model_config(registry, global_default_profile_id=profile.id) as (screen, pilot):
        try:
            await click_fetch(screen, pilot)
            await wait_for(lambda: len(fake_discover.calls) == 1, pilot=pilot)
            report = fake_discover.on_retry[0]
            assert report is not None
            report(5, 5)
            await wait_for(
                lambda: fetching_frame(screen, RETRYING_EN.format(round=5, rounds=5)) is not None, pilot=pilot
            )
            gate.set()
            await wait_for(lambda: _hint(screen) == NOT_FOUND_EN, pilot=pilot)

            gate.clear()
            await click_fetch(screen, pilot)
            await wait_for(lambda: len(fake_discover.calls) == 2, pilot=pilot)

            await wait_for(lambda: fetching_frame(screen) is not None, pilot=pilot, description="plain progress again")
        finally:
            gate.set()
