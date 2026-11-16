# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Spec tests for ``discover_models``: where it looks, how it authenticates, what counts as a list.

Written from ``.plan/model-list-fetch.md`` §3.3 to §3.5 and the interface contract
``.plan/model-list-interface.md``; every request goes to an ``httpx.MockTransport``.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from chrys.foundation.text.reading_order import reading_order
from chrys.service.llm import model_list as model_list_module
from chrys.service.llm import model_list_presets
from chrys.service.llm.model_list import (
    has_command_placeholder,
)
from chrys.service.llm.model_list_presets import PRESETS, ModelListPreset, match_preset
from tests.service.llm._model_list_support import (
    Recorder,
    always,
    auth_group,
    clear_provider_env,
    discover,
    discover_full,
    fast_retries,
    flaky,
    ids,
    item_marked,
    only_base,
    openai_list,
    snap,
    status,
)


@pytest.fixture(autouse=True)
def _no_provider_env(monkeypatch: pytest.MonkeyPatch) -> None:
    clear_provider_env(monkeypatch)
    fast_retries(monkeypatch)


# ── Where it looks ───────────────────────────────────────────────────


async def test_404_does_not_swap_auth_groups() -> None:
    recorder = Recorder(always(lambda: status(404)))

    assert await discover(snap(), recorder) == []
    # One request per location (convention, the base itself, the /v1-stripped diagnosis), no swap on 404.
    assert recorder.paths() == [
        "https://gw.example.test/v1/models",
        "https://gw.example.test/v1",
        "https://gw.example.test/models",
    ]
    assert [auth_group(r) for r in recorder.requests] == ["A", "A", "A"]


@pytest.mark.parametrize(
    ("provider", "base", "paths", "corrected"),
    [
        *[
            (provider, "https://gw.example.test/v1", ["/v1/models"], None)
            for provider in ("openai", "deepseek-openai", "glm-openai")
        ],
        ("anthropic", "https://gw.example.test/proxy", ["/proxy/v1/models"], None),
        ("openai", "https://gw.example.test/v1/", ["/v1/models"], None),
        ("openai", "https://gw.example.test", ["/models", "", "/v1/models"], "https://gw.example.test/v1"),
        ("anthropic", "https://gw.example.test/v1", ["/v1/v1/models", "/v1", "/v1/models"], "https://gw.example.test"),
        ("anthropic", "https://api.deepseek.com/anthropic", ["/models"], None),
        ("anthropic", "https://api.deepseek.com/anthropic", ["/models", "/anthropic/v1/models"], None),
        ("anthropic", "https://api.deepseek.com/anthropicx", ["/anthropicx/v1/models"], None),
        ("anthropic", "https://api.deepseek.com/anthropic", ["/models", "/anthropic/v1/models", "/anthropic"], None),
        ("openai", "https://gw.example.test/custom/catalog", ["/custom/catalog/models", "/custom/catalog"], None),
        ("openai", "https://gw.example.test/custom/catalog/", ["/custom/catalog/models", "/custom/catalog/"], None),
        (
            "openai",
            "  https://gw.example.test/custom/catalog/ \t",
            ["/custom/catalog/models", "/custom/catalog/"],
            None,
        ),
        ("openai", "https://gw.example.test/openai/v1", ["/openai/v1/models", "/openai/v1"], None),
        ("openai", "https://gw.example.test/llm/v1", ["/llm/v1/models", "/llm/v1", "/v1/models"], None),
        ("openai", "https://gw.example.test/llm/v1", ["/llm/v1/models", "/llm/v1", "/v1/models", "/models"], None),
        ("openai", "https://gw.example.test/openai", ["/openai/models", "/openai", "/v1/models"], None),
        ("openai", "https://gw.example.test/v1", ["/v1/models", "/v1", "/models"], "https://gw.example.test"),
        (
            "openai",
            "https://gw.example.test/openai",
            ["/openai/models", "/openai", "/v1/models", "/models", "/openai/v1/models"],
            "https://gw.example.test/openai/v1",
        ),
        (
            "openai",
            "https://gw.example.test/api/v1",
            ["/api/v1/models", "/api/v1", "/v1/models", "/models", "/api/models"],
            "https://gw.example.test/api",
        ),
    ],
)
async def test_provider_and_address_determine_the_full_request_sequence(
    provider: str, base: str, paths: list[str], corrected: str | None
) -> None:
    host = httpx.URL(base.strip()).host
    expected = [f"https://{host}{path}" for path in paths]
    recorder = Recorder(
        lambda request: (
            openai_list("deepseek-flash", "deepseek-v4-pro")
            if str(request.url.copy_with(query=None)) == expected[-1]
            else status(404)
        )
    )
    result = await discover_full(snap(provider=provider, base_url=base), recorder)
    assert ids(result.models) == ["deepseek-flash", "deepseek-v4-pro"]
    assert recorder.paths() == expected
    assert result.corrected_base_url == corrected
    assert result.unreachable is False
    assert [auth_group(request) for request in recorder.requests] == [("B" if provider == "anthropic" else "A")] * len(
        expected
    )


# ── Preset table ─────────────────────────────────────────────────────


def test_every_real_preset_list_path_is_relative() -> None:
    assert PRESETS
    for preset in PRESETS:
        assert preset.list_path == "" or (preset.list_path.startswith("/") and not preset.list_path.startswith("//"))
        assert "://" not in preset.list_path
        assert preset.evidence, f"preset for {preset.host} must cite its evidence"


@pytest.mark.parametrize(
    ("base_url", "matches"),
    [
        ("https://api.deepseek.com/anthropic", True),
        ("https://api.deepseek.com/anthropic/", True),
        ("https://api.deepseek.com/anthropic/v1", True),
        ("https://api.deepseek.com:443/anthropic", True),
        ("https://api.deepseek.com/anthropicx", False),
        ("https://api.deepseek.com/v1", False),
        ("http://api.deepseek.com/anthropic", False),
        ("https://api.deepseek.com:8443/anthropic", False),
        ("https://evil.api.deepseek.com/anthropic", False),
        ("https://api.deepseek.com.evil.test/anthropic", False),
    ],
)
def test_deepseek_preset_matches_scheme_host_port_exactly_and_path_by_segment(base_url: str, matches: bool) -> None:
    preset = match_preset(base_url)
    if matches:
        assert preset is not None
        assert preset.host == "api.deepseek.com"
        assert preset.path_prefix == "/anthropic"
    else:
        assert preset is None or preset.host != "api.deepseek.com" or preset.path_prefix != "/anthropic"
        if base_url.endswith(("/anthropicx", ":8443/anthropic")) or base_url.startswith("http://"):
            assert preset is None


def test_zhipu_preset_matches_any_path() -> None:
    preset = match_preset("https://open.bigmodel.cn/api/paas/v4")
    assert preset is not None
    assert preset.list_field == "rows"
    assert preset.id_field == "modelCode"
    assert preset.list_path.startswith("/api/fine-tuning/model_center/list")


def test_longest_path_prefix_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    short = ModelListPreset(scheme="https", host="gw.example.test", port=443, path_prefix="/a", list_path="/short")
    long = ModelListPreset(scheme="https", host="gw.example.test", port=443, path_prefix="/a/b", list_path="/long")
    anywhere = ModelListPreset(scheme="https", host="gw.example.test", port=443, list_path="/any")
    monkeypatch.setattr(model_list_presets, "PRESETS", (anywhere, short, long))

    assert match_preset("https://gw.example.test/a/b/c") == long
    assert match_preset("https://gw.example.test/a/bc") == short
    assert match_preset("https://gw.example.test/a") == short
    assert match_preset("https://gw.example.test/z") == anywhere


async def test_preset_query_parameters_are_sent() -> None:
    recorder = Recorder(always(lambda: openai_list("Qwen/Qwen3")))

    await discover(snap(base_url="https://api.siliconflow.cn/v1"), recorder)

    first = recorder.requests[0]
    assert first.url.path == "/v1/models"
    assert first.url.params.get("type") == "text"
    assert first.url.params.get("sub_type") == "chat"


async def test_zhipu_preset_reads_rows_and_model_code_with_bearer() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/fine-tuning/model_center/list":
            return httpx.Response(
                200,
                json={"code": 200, "success": True, "rows": [{"modelCode": "glm-5"}, {"modelCode": "glm-4.6"}]},
            )
        return status(404)

    recorder = Recorder(handler)

    models = await discover(snap(provider="glm-openai", base_url="https://open.bigmodel.cn/api/paas/v4"), recorder)

    assert ids(models) == sorted(["glm-5", "glm-4.6"], key=reading_order)
    first = recorder.requests[0]
    assert first.url.path == "/api/fine-tuning/model_center/list"
    assert first.url.params.get("pageSize") == "100"
    assert first.url.params.get("pageNum") == "1"
    assert first.headers["authorization"] == "Bearer sk-test"


async def test_zhipu_success_false_falls_back_to_convention() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/fine-tuning/model_center/list":
            return httpx.Response(200, json={"success": False, "msg": "bad key", "rows": [{"modelCode": "glm-5"}]})
        if request.url.path == "/api/paas/v4/models":
            return openai_list("glm-from-convention")
        return status(404)

    recorder = Recorder(handler)

    models = await discover(snap(provider="glm-openai", base_url="https://open.bigmodel.cn/api/paas/v4"), recorder)

    assert ids(models) == ["glm-from-convention"]


async def test_preset_list_field_does_not_leak_into_generic_parsing() -> None:
    """``rows`` is only read for a preset that names it; the convention location ignores it."""
    recorder = Recorder(always(lambda: httpx.Response(200, json={"rows": [{"modelCode": "x"}]})))

    assert await discover(snap(base_url="https://gw.example.test/v1"), recorder) == []


@pytest.mark.parametrize(
    "list_path",
    ["https://evil.example.test/models", "//evil.example.test/models", "http://evil.example.test/models"],
)
async def test_preset_list_path_never_leaves_the_users_host(monkeypatch: pytest.MonkeyPatch, list_path: str) -> None:
    try:
        bad = ModelListPreset(scheme="https", host="gw.example.test", port=443, list_path=list_path)
    except ValueError:
        return  # rejected at definition: equally acceptable
    monkeypatch.setattr(model_list_presets, "PRESETS", (bad,))
    recorder = Recorder(always(lambda: openai_list("m")))

    assert await discover(snap(base_url="https://gw.example.test/v1"), recorder) == []

    # The bad preset is refused before any request: the key never travels anywhere.
    assert recorder.requests == []


# ── Authentication ───────────────────────────────────────────────────


async def test_openai_family_uses_group_a_only() -> None:
    recorder = Recorder(always(lambda: openai_list("m")))

    await discover(snap(api_key="sk-abc"), recorder)

    (request,) = recorder.requests
    assert request.headers["authorization"] == "Bearer sk-abc"
    assert "x-api-key" not in request.headers
    assert "anthropic-version" not in request.headers


async def test_anthropic_uses_group_b_first() -> None:
    recorder = Recorder(always(lambda: openai_list("m")))

    await discover(snap(provider="anthropic", base_url="https://gw.example.test", api_key="sk-ant"), recorder)

    (request,) = recorder.requests
    assert request.headers["x-api-key"] == "sk-ant"
    assert request.headers["anthropic-version"] == "2023-06-01"
    assert "authorization" not in request.headers


@pytest.mark.parametrize("code", [401, 403])
async def test_auth_rejection_swaps_group_once(code: int) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if auth_group(request) == "B":
            return openai_list("found-with-b")
        return status(code)

    recorder = Recorder(handler)

    models = await discover(snap(api_key="sk-x"), recorder)

    assert ids(models) == ["found-with-b"]
    assert [auth_group(r) for r in recorder.requests] == ["A", "B"]
    assert recorder.requests[1].headers["anthropic-version"] == "2023-06-01"
    assert recorder.requests[1].headers["x-api-key"] == "sk-x"


async def test_anthropic_swaps_to_bearer_on_401() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if auth_group(request) == "A":
            return openai_list("found-with-a")
        return status(401)

    recorder = Recorder(handler)

    models = await discover(snap(provider="anthropic", base_url="https://gw.example.test"), recorder)

    assert ids(models) == ["found-with-a"]
    assert [auth_group(r) for r in recorder.requests] == ["B", "A"]
    assert recorder.requests[1].headers["authorization"] == "Bearer sk-test"


async def test_both_groups_rejected_at_every_location_is_not_found() -> None:
    recorder = Recorder(always(lambda: status(401)))

    assert await discover(snap(), recorder) == []
    pairs = [(path, auth_group(r)) for path, r in zip(recorder.paths(), recorder.requests, strict=True)]
    assert pairs[:4] == [
        ("https://gw.example.test/v1/models", "A"),
        ("https://gw.example.test/v1/models", "B"),
        ("https://gw.example.test/v1", "A"),
        ("https://gw.example.test/v1", "B"),
    ]
    assert list(dict.fromkeys(path for path, _group in pairs)) == [
        "https://gw.example.test/v1/models",
        "https://gw.example.test/v1",
        "https://gw.example.test/models",
    ]


async def test_deepseek_anthropic_locations_with_root_dedup_and_no_anthropic_missing_v1_diagnosis() -> None:
    recorder = Recorder(always(lambda: status(401)))

    assert await discover(snap(provider="anthropic", base_url="https://api.deepseek.com/anthropic"), recorder) == []
    distinct = list(dict.fromkeys(recorder.paths()))
    # Preset, convention, base itself, root R/v1/models (R/models was the preset location already);
    # Anthropic never tries B/v1/v1/models.
    assert distinct == [
        "https://api.deepseek.com/models",
        "https://api.deepseek.com/anthropic/v1/models",
        "https://api.deepseek.com/anthropic",
        "https://api.deepseek.com/v1/models",
    ]
    assert len(recorder.requests) == 8
    assert sorted(auth_group(r) or "" for r in recorder.requests) == ["A"] * 4 + ["B"] * 4


async def test_request_budget_is_six_locations_times_two_groups(monkeypatch: pytest.MonkeyPatch) -> None:
    preset = ModelListPreset(scheme="https", host="gw.example.test", port=443, list_path="/catalog")
    monkeypatch.setattr(model_list_presets, "PRESETS", (preset,))
    recorder = Recorder(always(lambda: status(401)))

    assert await discover(snap(base_url="https://gw.example.test/api/v1"), recorder) == []

    assert list(dict.fromkeys(recorder.paths())) == [
        "https://gw.example.test/catalog",
        "https://gw.example.test/api/v1/models",
        "https://gw.example.test/api/v1",
        "https://gw.example.test/v1/models",
        "https://gw.example.test/models",
        "https://gw.example.test/api/models",
    ]
    assert len(recorder.requests) == 12


@pytest.mark.parametrize("provider", ["openai", "anthropic"])
async def test_empty_key_sends_no_auth_and_does_not_swap(provider: str) -> None:
    recorder = Recorder(always(lambda: status(401)))

    assert await discover(snap(provider=provider, base_url="https://gw.example.test", api_key=""), recorder) == []

    # One request per location: no swap.
    assert len(recorder.requests) == len(set(recorder.paths())) >= 2
    for request in recorder.requests:
        assert "authorization" not in request.headers
        assert "x-api-key" not in request.headers


@pytest.mark.parametrize(
    "header_name", ["Authorization", "authorization", "x-api-key", "X-API-KEY", "api-key", "Api-Key"]
)
async def test_user_auth_header_suppresses_added_auth_and_swap(header_name: str) -> None:
    recorder = Recorder(always(lambda: status(401)))

    result = await discover(snap(api_key="sk-form", http_headers={header_name: "user-secret"}), recorder)

    assert result == []
    # One request per location: no swap.
    assert len(recorder.requests) == len(set(recorder.paths())) >= 2
    for request in recorder.requests:
        assert request.headers[header_name] == "user-secret"
        sent = {name.lower(): value for name, value in request.headers.items()}
        assert "sk-form" not in json.dumps(sent)
        for auth_name in ("authorization", "x-api-key"):
            if auth_name != header_name.lower():
                assert auth_name not in sent


async def test_user_auth_header_in_extra_headers_also_suppresses_auth() -> None:
    recorder = Recorder(always(lambda: status(403)))

    await discover(snap(api_key="sk-form", extra_headers={"Authorization": "Bearer mine"}), recorder)

    assert len(recorder.requests) == len(set(recorder.paths())) >= 2
    assert all(r.headers["authorization"] == "Bearer mine" for r in recorder.requests)


async def test_http_headers_and_extra_headers_are_sent() -> None:
    recorder = Recorder(always(lambda: openai_list("m")))

    await discover(snap(http_headers={"X-Team": "platform"}, extra_headers={"X-Trace": "on"}), recorder)

    request = recorder.requests[0]
    assert request.headers["x-team"] == "platform"
    assert request.headers["x-trace"] == "on"


async def test_env_templates_expand_in_key_and_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ML_TEST_KEY", "sk-expanded")
    monkeypatch.setenv("ML_TEST_TEAM", "team-expanded")
    recorder = Recorder(always(lambda: openai_list("m")))

    models = await discover(
        snap(
            api_key="{{ML_TEST_KEY}}",
            http_headers={"X-Team": "{{ML_TEST_TEAM}}"},
            extra_headers={"X-Extra": "{{ML_TEST_TEAM}}"},
        ),
        recorder,
    )

    assert ids(models) == ["m"]
    request = recorder.requests[0]
    assert request.headers["authorization"] == "Bearer sk-expanded"
    assert request.headers["x-team"] == "team-expanded"
    assert request.headers["x-extra"] == "team-expanded"


@pytest.mark.parametrize("where", ["api_key", "http_headers", "extra_headers"])
async def test_missing_env_var_returns_empty_without_requests(monkeypatch: pytest.MonkeyPatch, where: str) -> None:
    monkeypatch.delenv("ML_TEST_MISSING", raising=False)
    recorder = Recorder(always(lambda: openai_list("m")))
    kwargs: dict[str, Any] = {"api_key": "sk"}
    if where == "api_key":
        kwargs["api_key"] = "{{ML_TEST_MISSING}}"
    else:
        kwargs[where] = {"X-Thing": "{{ML_TEST_MISSING}}"}

    assert await discover(snap(**kwargs), recorder) == []
    assert recorder.requests == []


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"api_key": "{{!pass show key}}"}, True),
        ({"api_key": "prefix-{{!echo k}}"}, True),
        ({"http_headers": {"X-Token": "{{!get-token}}"}}, True),
        ({"extra_headers": {"X-Token": "{{!get-token}}"}}, True),
        ({"api_key": "{{{{!not a command}}}}"}, False),
        ({"http_headers": {"X-Literal": "{{{{!not a command}}}}"}}, False),
        ({"api_key": "{{SOME_ENV}}"}, False),
        ({"api_key": "sk-plain"}, False),
    ],
)
def test_has_command_placeholder(kwargs: dict[str, Any], expected: bool) -> None:
    base: dict[str, Any] = {"api_key": "sk"}
    base.update(kwargs)
    assert has_command_placeholder(snap(**base)) is expected


async def test_command_placeholder_is_never_run_and_nothing_is_queried(tmp_path: Path) -> None:
    marker = tmp_path / "ran"
    recorder = Recorder(always(lambda: openai_list("m")))

    result = await discover(snap(api_key=f"{{{{!touch {marker}}}}}"), recorder)

    assert result == []
    assert recorder.requests == []
    assert not marker.exists()


# ── What counts as a list ────────────────────────────────────────────


async def test_top_level_array_of_strings_and_objects() -> None:
    recorder = Recorder(always(lambda: httpx.Response(200, json=["alpha", {"id": "beta"}])))

    assert ids(await discover(snap(), recorder)) == ["alpha", "beta"]


@pytest.mark.parametrize(
    "body",
    [
        {"models": [{"name": "llama3"}, {"model": "qwen"}]},
        {"errors": [{"id": "x"}, {"id": "y"}]},
        {"result": [{"id": "x"}]},
        {"data": {"id": "x"}},
        {"data": [{"id": "x"}], "error": {"message": "denied"}},
        {"data": [{"id": "x"}], "success": False},
        {"error": "unauthorized"},
        {"data": []},
        [],
    ],
)
async def test_bodies_that_are_not_lists(body: Any) -> None:
    recorder = Recorder(always(lambda: httpx.Response(200, json=body)))

    assert await discover(snap(), recorder) == []


async def test_data_list_with_success_true_is_accepted() -> None:
    recorder = Recorder(
        always(lambda: httpx.Response(200, json={"success": True, "object": "list", "data": [{"id": "x"}]}))
    )

    assert ids(await discover(snap(), recorder)) == ["x"]


async def test_llama_cpp_shape_reads_data_not_models() -> None:
    body = {
        "models": [{"name": "from-models", "model": "from-models"}],
        "object": "list",
        "data": [{"id": "from-data", "object": "model"}],
    }
    recorder = Recorder(always(lambda: httpx.Response(200, json=body)))

    models = await discover(snap(), recorder)

    assert ids(models) == ["from-data"]


@pytest.mark.parametrize("code", [201, 204, 301, 400, 404, 500, 503])
async def test_non_2xx_or_empty_responses_are_not_found(code: int) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        if code == 204:
            return httpx.Response(204)
        if code == 201:
            return openai_list("created-is-2xx")
        return httpx.Response(code, json={"object": "list", "data": [{"id": "x"}]})

    recorder = Recorder(handler)
    result = await discover(snap(), recorder)

    if code == 201:
        assert ids(result) == ["created-is-2xx"]
    else:
        assert result == []


@pytest.mark.parametrize(
    "response",
    [
        lambda: httpx.Response(200, text="<html><body>Welcome</body></html>", headers={"content-type": "text/html"}),
        lambda: httpx.Response(200, text="not json at all", headers={"content-type": "text/plain"}),
        lambda: httpx.Response(200, text='{"data": [', headers={"content-type": "application/json"}),
    ],
)
async def test_html_and_bad_json_are_not_found(response: Callable[[], httpx.Response]) -> None:
    recorder = Recorder(always(response))

    assert await discover(snap(), recorder) == []


@pytest.mark.parametrize(
    ("item", "expected"),
    [
        ({"id": "a", "model": "b", "model_id": "c"}, "a"),
        ({"model": "b", "model_id": "c"}, "b"),
        ({"model_id": "c"}, "c"),
        ({"id": "", "model": "b"}, "b"),
    ],
)
async def test_id_field_priority(item: dict[str, str], expected: str) -> None:
    recorder = Recorder(always(lambda: httpx.Response(200, json={"data": [item]})))

    models = await discover(snap(), recorder)

    assert ids(models) == [expected]


async def test_at_least_half_of_the_elements_must_yield_ids() -> None:
    half = [{"id": "a"}, {"id": "b"}, {"name": "c"}, {"name": "d"}]
    less = [{"id": "a"}, {"name": "b"}, {"name": "c"}, {"name": "d"}]

    accepted = await discover(snap(), Recorder(always(lambda: httpx.Response(200, json={"data": half}))))
    rejected = await discover(snap(), Recorder(always(lambda: httpx.Response(200, json={"data": less}))))

    assert ids(accepted) == ["a", "b"]
    assert rejected == []


async def test_invalid_ids_are_dropped() -> None:
    long_ok = "y" * 256
    too_long = "z" * 257
    data = [
        "ok-1",
        "ok-2",
        "ok-3",
        long_ok,
        "provider/model:tag@v1",
        "",
        too_long,
        "中文模型",
        " leading",
        "tab\tinside",
        42,
    ]
    recorder = Recorder(always(lambda: httpx.Response(200, json={"data": data})))

    models = await discover(snap(), recorder)

    assert set(ids(models)) == {"ok-1", "ok-2", "ok-3", long_ok, "provider/model:tag@v1"}


async def test_ids_are_kept_verbatim_deduplicated_and_sorted_by_reading_order() -> None:
    raw = ["model-10", "Model-9", "alpha", "anthropic/claude-haiku-4.5", "model-10", "GPT-5.10", "GPT-5.9"]
    recorder = Recorder(always(lambda: httpx.Response(200, json={"data": [{"id": i} for i in raw]})))

    models = await discover(snap(), recorder)

    assert ids(models) == sorted(set(raw), key=reading_order)


async def test_oversized_response_is_not_read_past_5_mb() -> None:
    padding = "x" * 200
    count = (6 * 1024 * 1024) // (len(padding) + 40)
    body = {"data": [{"id": f"m-{i}", "pad": padding} for i in range(count)]}
    recorder = Recorder(always(lambda: httpx.Response(200, content=json.dumps(body).encode())))

    assert await discover(snap(), recorder) == []


async def test_streamed_response_is_not_pulled_past_5_mib() -> None:
    """Six 1 MiB chunks, then a closing chunk that would complete a valid list: it is never pulled."""
    mib = 1024 * 1024
    head = b'{"data": ["m-1", "m-2"'
    sentinel_pulled = False

    async def body() -> AsyncIterator[bytes]:
        nonlocal sentinel_pulled
        yield head + b" " * (mib - len(head))
        for _ in range(5):
            yield b" " * mib
        sentinel_pulled = True
        yield b"]}"

    recorder = Recorder(lambda _request: httpx.Response(200, content=body()))

    result = await discover_full(snap(), recorder)

    assert recorder.requests, "the list location was never requested"
    assert result.models == []
    assert not sentinel_pulled


# ── Step 3: the Base URL itself ──────────────────────────────────────


async def test_base_url_is_tried_after_auth_rejection_at_the_convention() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/models":
            return status(401)
        return openai_list("from-base")

    recorder = Recorder(handler)

    assert ids(await discover(snap(), recorder)) == ["from-base"]
    assert recorder.paths()[-1] == "https://gw.example.test/v1"


@pytest.mark.parametrize(
    "body",
    [
        {"object": "list", "data": [{"id": "x"}, {"id": "y"}]},
        {"data": [{"id": "x", "object": "model"}, {"id": "y", "object": "model"}]},
        [{"id": "x", "owned_by": "me"}, {"id": "y", "owned_by": "me"}],
        {"data": [{"id": "x", "owned_by": "me"}, {"id": "y"}]},
        {"data": [{"id": "x", "object": "model"}, {"id": "y"}]},
        {"data": [{"type": "model", "id": "x"}, {"type": "model", "id": "y"}], "has_more": False},
        [{"type": "model", "id": "x"}, {"id": "y"}],
    ],
    ids=[
        "object-list",
        "object-model",
        "owned-by",
        "half-owned-by",
        "half-object-model",
        "anthropic-type-model",
        "half-type-model",
    ],
)
async def test_base_url_list_with_model_list_markers_is_found(body: Any) -> None:
    recorder = Recorder(only_base("/v1", lambda: httpx.Response(200, json=body)))

    assert ids(await discover(snap(), recorder)) == ["x", "y"]


@pytest.mark.parametrize(
    "body",
    [
        [{"id": "x"}, {"id": "y"}],
        ["x", "y"],
        {"data": [{"id": "x"}, {"id": "y"}]},
        {"data": [{"id": "x", "owned_by": "me"}, {"id": "y"}, {"id": "z"}]},
        {"data": [{"id": "x", "object": "file"}, {"id": "y", "object": "file"}]},
        {"data": [{"id": "x", "type": "file"}, {"id": "y", "type": "file"}]},
        {"data": [{"id": "x", "type": "model"}, {"id": "y"}, {"id": "z"}]},
        {"object": "page", "data": [{"id": "x"}, {"id": "y"}]},
        {"object": "list", "data": [{"id": "x"}], "error": {"message": "denied"}},
        {"object": "list", "data": [{"id": "x"}], "success": False},
    ],
    ids=[
        "bare-array",
        "string-array",
        "bare-data",
        "owned-by-under-half",
        "other-resource",
        "other-type",
        "type-model-under-half",
        "other-object",
        "error",
        "success-false",
    ],
)
async def test_base_url_list_without_markers_is_not_found(body: Any) -> None:
    recorder = Recorder(only_base("/v1", lambda: httpx.Response(200, json=body)))

    assert await discover(snap(), recorder) == []
    assert "https://gw.example.test/v1" in recorder.paths()


@pytest.mark.parametrize(
    "response",
    [
        lambda: httpx.Response(200, text="<html><body>Gateway</body></html>", headers={"content-type": "text/html"}),
        lambda: httpx.Response(200, text="Ollama is running", headers={"content-type": "text/plain"}),
        lambda: httpx.Response(404, json={"object": "list", "data": [{"id": "x"}]}),
    ],
    ids=["html", "plain-text", "non-2xx"],
)
async def test_base_url_html_text_or_error_is_not_found(response: Callable[[], httpx.Response]) -> None:
    recorder = Recorder(only_base("/v1", response))

    assert await discover(snap(), recorder) == []


@pytest.mark.parametrize(
    "body",
    [[{"id": "x"}, {"id": "y"}], {"data": [{"id": "x"}, {"id": "y"}]}],
    ids=["bare-array", "bare-data"],
)
async def test_convention_location_keeps_lenient_parsing(body: Any) -> None:
    recorder = Recorder(only_base("/v1/models", lambda: httpx.Response(200, json=body)))

    assert ids(await discover(snap(), recorder)) == ["x", "y"]
    assert recorder.paths() == ["https://gw.example.test/v1/models"]


async def test_preset_location_keeps_lenient_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    preset = ModelListPreset(scheme="https", host="gw.example.test", port=443, list_path="/catalog")
    monkeypatch.setattr(model_list_presets, "PRESETS", (preset,))
    recorder = Recorder(only_base("/catalog", lambda: httpx.Response(200, json=[{"id": "x"}])))

    assert ids(await discover(snap(base_url="https://gw.example.test/v1"), recorder)) == ["x"]
    assert recorder.paths() == ["https://gw.example.test/catalog"]


async def test_base_url_equal_to_the_preset_location_is_not_requested_twice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    preset = ModelListPreset(scheme="https", host="gw.example.test", port=443, list_path="/catalog")
    monkeypatch.setattr(model_list_presets, "PRESETS", (preset,))
    recorder = Recorder(always(lambda: status(404)))

    assert await discover(snap(base_url="https://gw.example.test/catalog"), recorder) == []

    # Step 3 is skipped (same URL as step 1); then root and the missing-/v1 diagnosis.
    assert recorder.paths() == [
        "https://gw.example.test/catalog",
        "https://gw.example.test/catalog/models",
        "https://gw.example.test/v1/models",
        "https://gw.example.test/models",
        "https://gw.example.test/catalog/v1/models",
    ]


async def test_base_url_equal_to_the_preset_location_keeps_its_lenient_verdict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The URL was already tried as step 1 (lenient) and found nothing; step 3 does not retry it."""
    preset = ModelListPreset(scheme="https", host="gw.example.test", port=443, list_path="/catalog")
    monkeypatch.setattr(model_list_presets, "PRESETS", (preset,))
    recorder = Recorder(always(lambda: status(401)))

    assert await discover(snap(base_url="https://gw.example.test/catalog"), recorder) == []

    assert recorder.paths().count("https://gw.example.test/catalog") == 2  # A then B, once each


async def test_a_connection_error_at_the_convention_does_not_stop_round_one() -> None:
    def answer(request: httpx.Request) -> httpx.Response:
        return status(404) if request.url.path == "/v1/models" else openai_list("from-base")

    recorder = Recorder(flaky(answer, fails=2, paths=("/v1/models",)))

    # No key: one request per location, so the order is easy to read.
    result = await discover_full(snap(api_key=""), recorder)

    assert ids(result.models) == ["from-base"]
    assert result.unreachable is False
    # Not retried on the spot: the next location answers in the same round.
    assert recorder.paths() == ["https://gw.example.test/v1/models", "https://gw.example.test/v1"]


@pytest.mark.parametrize("code", [401, 403])
async def test_auth_rejection_at_the_base_url_swaps_once(code: int) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1" and auth_group(request) == "B":
            return openai_list("from-base")
        if request.url.path == "/v1":
            return status(code)
        return status(404)

    recorder = Recorder(handler)

    assert ids(await discover(snap(), recorder)) == ["from-base"]
    base_groups = [
        auth_group(r) for r, path in zip(recorder.requests, recorder.paths(), strict=True) if path.endswith("/v1")
    ]
    assert base_groups == ["A", "B"]


async def test_anthropic_base_url_is_paged_with_limit() -> None:
    pages = {
        None: {"object": "list", "data": [{"id": "a-1"}], "has_more": True, "last_id": "a-1"},
        "a-1": {"object": "list", "data": [{"id": "b-1"}], "has_more": False, "last_id": "b-1"},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/catalog":
            return httpx.Response(200, json=pages[request.url.params.get("after_id")])
        return status(404)

    recorder = Recorder(handler)

    models = await discover(snap(provider="anthropic", base_url="https://gw.example.test/catalog"), recorder)

    assert ids(models) == ["a-1", "b-1"]
    base_requests = [r for r in recorder.requests if r.url.path == "/catalog"]
    assert len(base_requests) == 2
    assert all(r.url.params.get("limit") == "1000" for r in base_requests)
    assert base_requests[1].url.params.get("after_id") == "a-1"


async def test_openai_base_url_is_requested_without_a_query() -> None:
    recorder = Recorder(only_base("/v1", lambda: openai_list("from-base")))

    await discover(snap(), recorder)

    assert recorder.requests[-1].url.query == b""


# ── Filtering (§3.4) ─────────────────────────────────────────────────


async def test_filters_only_models_that_explicitly_lack_text_output() -> None:
    data = [
        {"id": "chat", "output_modalities": ["text"]},
        {"id": "multi", "output_modalities": ["text", "image"]},
        {"id": "image-only", "output_modalities": ["image"]},
        {"id": "speech-only", "output_modalities": ["audio"]},
        {"id": "router-image", "architecture": {"output_modalities": ["image"]}},
        {"id": "router-text", "architecture": {"output_modalities": ["text"]}},
        {"id": "text-embedding-3-large"},
        {"id": "tts-1"},
    ]
    recorder = Recorder(always(lambda: httpx.Response(200, json={"data": data})))

    models = await discover(snap(), recorder)

    assert ids(models) == sorted(["chat", "multi", "router-text", "text-embedding-3-large", "tts-1"], key=reading_order)


async def test_filtering_happens_before_the_half_rule() -> None:
    data = [
        {"id": "img-1", "output_modalities": ["image"]},
        {"id": "img-2", "output_modalities": ["image"]},
        {"id": "img-3", "output_modalities": ["image"]},
        {"id": "chat", "output_modalities": ["text"]},
        {"name": "no-id", "output_modalities": ["image"]},
    ]
    recorder = Recorder(always(lambda: httpx.Response(200, json={"data": data})))

    assert ids(await discover(snap(), recorder)) == ["chat"]


async def test_api_style_capabilities_do_not_filter() -> None:
    data = [{"id": "deepseek-flash", "api_capabilities": ["anthropic_messages"]}]
    recorder = Recorder(always(lambda: httpx.Response(200, json={"data": data})))

    assert ids(await discover(snap(provider="openai"), recorder)) == ["deepseek-flash"]


# ── Anthropic pagination ─────────────────────────────────────────────


async def test_anthropic_pages_until_has_more_is_false() -> None:
    pages = {
        None: {"data": [{"id": "a-1"}, {"id": "a-2"}], "has_more": True, "last_id": "a-2"},
        "a-2": {"data": [{"id": "b-1"}], "has_more": False, "last_id": "b-1"},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=pages[request.url.params.get("after_id")])

    recorder = Recorder(handler)

    models = await discover(snap(provider="anthropic", base_url="https://api.anthropic.test"), recorder)

    assert ids(models) == ["a-1", "a-2", "b-1"]
    assert len(recorder.requests) == 2
    assert all(r.url.params.get("limit") == "1000" for r in recorder.requests)
    assert recorder.requests[1].url.params.get("after_id") == "a-2"
    assert all(r.headers.get("x-api-key") == "sk-test" for r in recorder.requests)


async def test_anthropic_paging_stops_after_five_pages() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        page = len(recorder.requests)
        return httpx.Response(200, json={"data": [{"id": f"p{page}"}], "has_more": True, "last_id": f"p{page}"})

    recorder = Recorder(handler)

    models = await discover(snap(provider="anthropic", base_url="https://api.anthropic.test"), recorder)

    assert len(recorder.requests) == 5
    assert "p1" in ids(models)


async def test_anthropic_paging_stops_on_repeated_cursor() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"id": "same"}], "has_more": True, "last_id": "same"})

    recorder = Recorder(handler)

    models = await discover(snap(provider="anthropic", base_url="https://api.anthropic.test"), recorder)

    assert ids(models) == ["same"]
    assert len(recorder.requests) <= 2


# ── Redirects ────────────────────────────────────────────────────────


async def test_one_same_origin_redirect_is_followed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/models":
            return httpx.Response(301, headers={"location": "/v2/models"})
        return openai_list("moved")

    recorder = Recorder(handler)

    models = await discover(snap(base_url="https://gw.example.test/v1"), recorder)

    assert ids(models) == ["moved"]
    assert recorder.paths() == ["https://gw.example.test/v1/models", "https://gw.example.test/v2/models"]
    assert recorder.requests[1].headers["authorization"] == "Bearer sk-test"


async def test_second_redirect_is_not_followed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/models":
            return httpx.Response(302, headers={"location": "https://gw.example.test/v2/models"})
        if request.url.path == "/v2/models":
            return httpx.Response(302, headers={"location": "https://gw.example.test/v3/models"})
        if request.url.path == "/v3/models":
            return openai_list("too-far")
        return status(404)

    recorder = Recorder(handler)

    assert await discover(snap(base_url="https://gw.example.test/v1"), recorder) == []
    assert "https://gw.example.test/v3/models" not in recorder.paths()


@pytest.mark.parametrize(
    "location",
    [
        "https://other.example.test/v1/models",
        "http://gw.example.test/v1/models2",
        "https://gw.example.test:8443/v1/models",
    ],
)
@pytest.mark.parametrize("provider", ["openai", "anthropic"])
async def test_cross_origin_redirect_is_not_followed_and_key_stays_home(location: str, provider: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "gw.example.test" and request.url.scheme == "https" and request.url.port in (None, 443):
            return httpx.Response(307, headers={"location": location})
        return openai_list("elsewhere")

    recorder = Recorder(handler)

    result = await discover(
        snap(provider=provider, base_url="https://gw.example.test/v1", api_key="sk-secret"), recorder
    )

    assert result == []
    for request in recorder.requests:
        assert request.url.host == "gw.example.test"
        assert request.url.scheme == "https"
        assert request.url.port in (None, 443)


async def test_only_one_same_origin_redirect_per_whole_lookup() -> None:
    """The redirect budget is shared by every request of one lookup, not one per request."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/models":
            return httpx.Response(302, headers={"location": "/v2/models"})
        if request.url.path == "/v2/models":
            return status(404)
        if request.url.path == "/v1":
            return httpx.Response(302, headers={"location": "/v1/list"})
        if request.url.path == "/v1/list":
            return openai_list("second-redirect")
        return status(404)

    recorder = Recorder(handler)

    assert await discover(snap(base_url="https://gw.example.test/v1"), recorder) == []
    assert "https://gw.example.test/v2/models" in recorder.paths()
    assert "https://gw.example.test/v1" in recorder.paths()
    assert "https://gw.example.test/v1/list" not in recorder.paths()


# ── Step 4: the root ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    "body",
    [
        {"object": "list", "data": [{"id": "x"}, {"id": "y"}]},
        [{"id": "x"}, {"id": "y"}],
        {"data": [{"id": "x", "owned_by": "o"}, {"id": "y"}, {"id": "z"}]},
    ],
    ids=["top-level-object-list-only", "bare-array", "markers-under-half"],
)
async def test_root_requires_item_level_markers(body: Any) -> None:
    recorder = Recorder(only_base("/v1/models", lambda: httpx.Response(200, json=body)))

    result = await discover_full(snap(base_url="https://gw.example.test/llm/v1"), recorder)

    assert result.models == []
    assert "https://gw.example.test/v1/models" in recorder.paths()


@pytest.mark.parametrize(
    "body",
    [
        {"data": [{"id": "x", "object": "model"}, {"id": "y"}]},
        {"data": [{"id": "x", "type": "model"}, {"id": "y", "type": "model"}]},
        [{"id": "x", "owned_by": "o"}, {"id": "y", "owned_by": "o"}],
    ],
    ids=["half-object-model", "type-model", "owned-by"],
)
async def test_root_accepts_item_level_markers(body: Any) -> None:
    recorder = Recorder(only_base("/v1/models", lambda: httpx.Response(200, json=body)))

    result = await discover_full(snap(base_url="https://gw.example.test/llm/v1"), recorder)

    assert ids(result.models) == ["x", "y"]


# ── Step 5: diagnosis ────────────────────────────────────────────────


async def test_anthropic_never_tries_v1_v1_for_a_base_without_v1() -> None:
    recorder = Recorder(always(lambda: status(404)))

    result = await discover_full(snap(provider="anthropic", base_url="https://gw.example.test/proxy"), recorder)

    assert result.corrected_base_url is None
    assert "https://gw.example.test/proxy/v1/v1/models" not in recorder.paths()


@pytest.mark.parametrize("code", [401, 403])
async def test_diagnosis_rejection_sets_nothing(code: int) -> None:
    recorder = Recorder(only_base("/v1/models", lambda: status(code)))

    result = await discover_full(snap(base_url="https://gw.example.test"), recorder)

    assert result.models == []
    assert result.corrected_base_url is None
    assert result.auth_rejected is False
    assert "https://gw.example.test/v1/models" in recorder.paths()


async def test_diagnosis_is_still_tried_after_flaky_connections() -> None:
    recorder = Recorder(flaky(lambda _request: status(404)))

    result = await discover_full(snap(base_url="https://gw.example.test"), recorder)

    assert result.corrected_base_url is None
    assert result.unreachable is False, "every location answered once retried"
    assert "https://gw.example.test/v1/models" in recorder.paths()


async def test_corrected_base_from_an_env_address(monkeypatch: pytest.MonkeyPatch) -> None:
    """A blank form URL resolves to the env address; step 5 corrects that address."""
    monkeypatch.setenv("OPENAI_BASE_URL", "https://env.example.test")
    recorder = Recorder(only_base("/v1/models", lambda: item_marked("m")))

    result = await discover_full(snap(base_url=""), recorder)

    assert ids(result.models) == ["m"]
    assert result.corrected_base_url == "https://env.example.test/v1"


# ── Ratios measured before the 5000 cap ──────────────────────────────


async def test_marker_ratio_is_measured_on_all_items_before_the_5000_model_cap() -> None:
    """5001 items, 2500 marked first: under half of all items, though exactly half of the first 5000."""
    items = [{"id": f"m-{i:05d}", "object": "model"} for i in range(2500)]
    items += [{"id": f"u-{i:05d}"} for i in range(2501)]
    recorder = Recorder(only_base("/llm/v1", lambda: httpx.Response(200, json={"data": items})))

    result = await discover_full(snap(base_url="https://gw.example.test/llm/v1"), recorder)

    assert result.models == []
    assert "https://gw.example.test/llm/v1" in recorder.paths()


async def test_id_ratio_is_measured_on_all_items_before_the_5000_model_cap() -> None:
    items: list[Any] = [{"id": f"m-{i:05d}"} for i in range(2500)]
    items += [{"name": f"n-{i:05d}"} for i in range(2501)]
    recorder = Recorder(only_base("/v1/models", lambda: httpx.Response(200, json={"data": items})))

    result = await discover_full(snap(), recorder)

    assert result.models == []


async def test_a_valid_5001_item_list_returns_exactly_5000() -> None:
    recorder = Recorder(only_base("/v1/models", lambda: openai_list(*(f"m-{i:05d}" for i in range(5001)))))

    models = await discover(snap(), recorder)

    assert ids(models) == [f"m-{i:05d}" for i in range(5000)]


# ── Only the first 20000 items of a response are examined (§3.3 limits) ──

EXAMINED_ITEMS = 20_000
JUNK = " "  # never a valid id


def _strings_body(items: list[str]) -> httpx.Response:
    content = json.dumps({"data": items}).encode()
    assert len(content) < 5 * 1024 * 1024, len(content)
    return httpx.Response(200, content=content, headers={"content-type": "application/json"})


@pytest.mark.parametrize("valid_count", [100, 25_000], ids=["few-after", "majority-after"])
async def test_valid_ids_past_the_examined_prefix_are_not_found(valid_count: int) -> None:
    """With 25000 valid ids after the junk, the whole body is mostly valid; only the junk prefix counts."""
    items = [JUNK] * EXAMINED_ITEMS + [f"m-{i:05d}" for i in range(valid_count)]
    recorder = Recorder(always(lambda: _strings_body(items)))

    result = await discover_full(snap(), recorder)

    assert result.models == []
    assert recorder.requests, "the list location was never requested"


async def test_ratio_is_judged_on_the_examined_prefix_only() -> None:
    """20000 valid ids then 40000 junk: a third valid overall, all valid in the prefix."""
    items = [f"m-{i:05d}" for i in range(EXAMINED_ITEMS)] + [JUNK] * (2 * EXAMINED_ITEMS)
    recorder = Recorder(always(lambda: _strings_body(items)))

    models = await discover(snap(), recorder)

    assert ids(models) == [f"m-{i:05d}" for i in range(5000)]


# ── Deadline during Anthropic paging ─────────────────────────────────


async def test_deadline_during_anthropic_paging_keeps_collected_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    anthropic = snap(provider="anthropic", base_url="https://api.anthropic.test")
    # The budget clock starts before the service's cold SDK imports (about 0.35 s, more under load).
    # A throwaway lookup at the full budget warms them, so the shrunk budget covers only paging.
    await discover(anthropic, Recorder(always(lambda: openai_list("warm-up"))))
    monkeypatch.setattr(model_list_module, "_TOTAL_SECONDS", 0.5)

    async def handler(request: httpx.Request) -> httpx.Response:
        cursor = request.url.params.get("after_id")
        if cursor is None:
            return httpx.Response(200, json={"data": [{"id": "p1"}], "has_more": True, "last_id": "p1"})
        if cursor == "p1":
            return httpx.Response(200, json={"data": [{"id": "p2"}], "has_more": True, "last_id": "p2"})
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    recorder = Recorder(handler)
    started = time.monotonic()

    models = await asyncio.wait_for(discover(anthropic, recorder), timeout=8)

    assert ids(models) == ["p1", "p2"]
    assert time.monotonic() - started < 5
    assert len(recorder.requests) == 3


# ── auth_rejected ────────────────────────────────────────────────────


@pytest.mark.parametrize("refusing_path", ["/v1/models", "/models"], ids=["root", "root-or-diagnosis"])
async def test_401_at_guessed_locations_does_not_count_as_auth_rejected(refusing_path: str) -> None:
    recorder = Recorder(only_base(refusing_path, lambda: status(401)))

    result = await discover_full(snap(api_key="", base_url="https://gw.example.test/llm/v1"), recorder)

    assert refusing_path in [r.url.path for r in recorder.requests]
    assert result.models == []
    assert result.auth_rejected is False


async def test_not_auth_rejected_when_a_list_is_found_after_a_401() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return openai_list("m") if auth_group(request) == "B" else status(401)

    result = await discover_full(snap(), Recorder(handler))

    assert ids(result.models) == ["m"]
    assert result.auth_rejected is False


# ── Odd Base URLs ────────────────────────────────────────────────────


async def test_fragment_does_not_swallow_the_convention_suffix() -> None:
    recorder = Recorder(always(lambda: openai_list("m")))

    await discover(snap(base_url="https://gw.example.test/v1#section"), recorder)

    first = recorder.requests[0]
    assert first.url.path == "/v1/models"
    assert first.url.host == "gw.example.test"


async def test_anthropic_connection_error_on_page_two_keeps_page_one() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("after_id") is None:
            return httpx.Response(
                200, json={"data": [{"id": "a-1"}, {"id": "a-2"}], "has_more": True, "last_id": "a-2"}
            )
        raise httpx.ConnectError("reset", request=request)

    recorder = Recorder(handler)

    models = await discover(snap(provider="anthropic", base_url="https://api.anthropic.test"), recorder)

    assert ids(models) == ["a-1", "a-2"]


# ── Errors, deadline, cancellation ───────────────────────────────────


@pytest.mark.parametrize(
    "exc",
    [
        lambda request: httpx.ConnectTimeout("slow", request=request),
        lambda request: httpx.ReadError("reset", request=request),
        lambda request: RuntimeError("unexpected"),
        lambda request: ValueError("odd"),
    ],
)
async def test_transport_failures_never_raise(exc: Callable[[httpx.Request], BaseException]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise exc(request)

    assert await discover(snap(), Recorder(handler)) == []


async def test_invalid_base_url_never_raises() -> None:
    recorder = Recorder(always(lambda: openai_list("m")))

    for base_url in ("not a url", "ftp://gw.example.test/v1", "https://", "://nohost"):
        assert await discover(snap(base_url=base_url), recorder) == []


async def test_header_with_invalid_characters_never_raises() -> None:
    recorder = Recorder(always(lambda: openai_list("m")))

    result = await discover(snap(api_key="sk-bad\nkey"), recorder)

    assert result == []


async def test_total_deadline_bounds_a_hanging_server(monkeypatch: pytest.MonkeyPatch) -> None:
    """The total deadline (60 s in production, shortened here) ends the lookup, retries and all."""
    monkeypatch.setattr(model_list_module, "_TOTAL_SECONDS", 0.5)

    async def hang(_request: httpx.Request) -> httpx.Response:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    recorder = Recorder(hang)
    started = time.monotonic()

    result = await asyncio.wait_for(discover(snap(), recorder), timeout=10)

    assert result == []
    assert time.monotonic() - started < 3


async def test_cancellation_propagates() -> None:
    entered = asyncio.Event()

    async def hang(_request: httpx.Request) -> httpx.Response:
        entered.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    task = asyncio.create_task(discover(snap(), Recorder(hang)))
    await asyncio.wait_for(entered.wait(), timeout=5)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task


# ── Provider SDK import off the event loop ───────────────────────────


@pytest.mark.parametrize(
    ("provider", "sdk_module"),
    [("openai", "openai"), ("deepseek-openai", "openai"), ("glm-openai", "openai"), ("anthropic", "anthropic")],
)
async def test_provider_sdk_is_imported_off_loop_before_the_first_request(
    monkeypatch: pytest.MonkeyPatch, provider: str, sdk_module: str
) -> None:
    """A cold SDK import must not hold the TUI's event loop: it is awaited before any request."""
    imports: list[tuple[str, ...]] = []
    requests_at_import: list[int] = []
    recorder = Recorder(always(lambda: openai_list("m-1")))

    async def recording_import(*names: str) -> None:
        imports.append(names)
        requests_at_import.append(len(recorder.requests))

    monkeypatch.setattr(model_list_module, "import_off_loop", recording_import)

    models = await discover(snap(provider=provider), recorder)

    assert ids(models) == ["m-1"]
    assert imports, "the provider SDK was not imported off the loop"
    assert sdk_module in imports[0]
    assert requests_at_import[0] == 0


@pytest.mark.parametrize(
    ("key", "code", "path", "expected"),
    [
        ("", 401, None, True),
        ("sk-bad", 403, None, True),
        ("sk-test", 401, "/v1/models", True),
        ("", 401, "/v1", True),
        ("sk-test", 404, None, False),
        ("{{!pass show key}}", 401, None, False),
    ],
)
async def test_auth_rejected_result(key: str, code: int, path: str | None, expected: bool) -> None:
    recorder = Recorder(lambda request: status(code if path is None or request.url.path == path else 404))
    result = await discover_full(snap(api_key=key), recorder)
    assert result.models == []
    assert result.auth_rejected is expected
    assert result.corrected_base_url is None
    if key.startswith("{{!"):
        assert recorder.requests == []
