# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Spec tests for the Base URL corrections the model-list Fetch applies on the configuration screen.

Written from ``.plan/model-list-fetch.md`` (address corrections are tried after the address as typed;
only a strictly marked list found at the corrected address rewrites the Base URL input, opens the
list and puts a warning-coloured hint below the Base URL input; stacked corrections are joined). The
real ``discover_models`` runs over a recording ``httpx.MockTransport``.
"""

from __future__ import annotations

import httpx
import pytest
from textual.widgets import Input

from chrys.service.llm.model_list import ModelDiscovery, ModelInfo, UrlCorrection
from tests.app.tui.screens._model_config_support import open_model_config, single_profile_registry
from tests.app.tui.screens._model_list_support import (
    PART_ADDED_SCHEME_EN,
    PART_ADDED_SCHEME_ZH,
    PART_ADDED_V1_EN,
    PART_ADDED_V1_ZH,
    PART_REMOVED_ENDPOINT_EN,
    PART_REMOVED_ENDPOINT_ZH,
    FakeDiscover,
    RealDiscover,
    click_fetch,
    corrected_en,
    corrected_zh,
    open_model_config_zh,
    row_shows_whole,
    url_hint,
)
from tests.service.llm._model_list_support import html
from tests.support.waiting import wait_for, wait_until_quiet

pytestmark = pytest.mark.usefixtures("isolated_model_config_dir", "clear_model_profile_env", "clear_provider_env")


def serve(real: RealDiscover, list_url: str) -> None:
    """Answer a strict list only at *list_url*, a web page elsewhere."""

    def answer(request: httpx.Request) -> httpx.Response:
        if str(request.url.copy_with(query=None)) == list_url:
            return httpx.Response(
                200,
                json={"object": "list", "data": [{"id": "m-1", "object": "model"}, {"id": "m-2", "object": "model"}]},
            )
        return html()

    real.answer = answer


@pytest.mark.parametrize(
    ("corrections", "expected"),
    [
        (("added_https", "added_v1"), corrected_zh(PART_ADDED_SCHEME_ZH, PART_ADDED_V1_ZH)),
        (("removed_v1",), "已自动去掉 /v1"),
        (("switched_https",), "已自动改为 https"),
        (("switched_http",), "已自动改为 http"),
    ],
)
async def test_correction_hints_in_chinese(
    fake_discover: FakeDiscover, corrections: tuple[UrlCorrection, ...], expected: str
) -> None:
    fake_discover.respond = lambda _s: ModelDiscovery(
        [ModelInfo("m-1")], url_corrections=corrections, corrected_base_url="https://gw.example.test/v1"
    )
    registry, profile = single_profile_registry(base_url="gw.example.test", api_key="sk-a", http_max_retries=0)

    async with open_model_config_zh(registry, global_default_profile_id=profile.id) as (screen, pilot):
        await click_fetch(screen, pilot)

        await wait_for(
            lambda: url_hint(screen) == expected,
            pilot=pilot,
            description="stacked zh hint",
        )

        row = screen.query_one("#mc-url-hint")
        await wait_until_quiet(lambda: row.region, description="Chinese correction laid out", pilot=pilot)
        assert row_shows_whole(screen, "#mc-url-hint", expected)


@pytest.mark.parametrize("locale", ["en", "zh-Hans"])
async def test_stacked_correction_hint_wraps_at_80_columns(real_discover: RealDiscover, locale: str) -> None:
    serve(real_discover, "https://gw.example.test/v1/models")
    registry, profile = single_profile_registry(
        base_url="gw.example.test/chat/completions", api_key="sk-a", http_max_retries=0
    )
    opener = open_model_config_zh if locale == "zh-Hans" else open_model_config
    if locale == "zh-Hans":
        text = corrected_zh(PART_ADDED_SCHEME_ZH, PART_REMOVED_ENDPOINT_ZH, PART_ADDED_V1_ZH)
    else:
        text = corrected_en(PART_ADDED_SCHEME_EN, PART_REMOVED_ENDPOINT_EN, PART_ADDED_V1_EN)

    async with opener(registry, global_default_profile_id=profile.id, size=(80, 30)) as (screen, pilot):
        await click_fetch(screen, pilot)
        await wait_for(lambda: url_hint(screen) == text, pilot=pilot, description="stacked hint")
        row = screen.query_one("#mc-url-hint")
        await wait_until_quiet(lambda: row.region, description="row laid out", pilot=pilot)

        assert row_shows_whole(screen, "#mc-url-hint", text)
        assert screen.query_one("#mc-base-url", Input).value == "https://gw.example.test/v1"
