# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Spec tests for which address and key ``discover_models`` uses, and for a lookup nothing answered.

Written from ``.plan/model-list-fetch.md`` (form value, then the provider's environment variable,
then its default; the same resolution the chat client uses) and the interface contract
``.plan/model-list-interface.md``; every request goes to an ``httpx.MockTransport``.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import time
from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx
import pytest

from chrys.service.llm import model_list as model_list_module
from chrys.service.llm import model_list_presets
from chrys.service.llm.clients import effective_model_base_url
from chrys.service.llm.model_list import ConnectionSnapshot, discover_models, resolve_model_connection
from chrys.service.llm.model_list_presets import ModelListPreset
from chrys.service.llm.providers import PROVIDERS
from chrys.service.profiles.models.schema import ModelProfile
from tests.service.llm._model_list_support import (
    REAL_REQUEST_SECONDS,
    REAL_RETRY_BASE_SECONDS,
    REAL_RETRY_MAX_PAUSE_SECONDS,
    REAL_TOTAL_ROUNDS,
    REAL_TOTAL_SECONDS,
    Handler,
    Recorder,
    always,
    calls_per_url,
    clear_provider_env,
    discover,
    discover_full,
    fast_retries,
    flaky,
    html,
    ids,
    item_marked,
    openai_list,
    openai_list_answer,
    snap,
    status,
)


@pytest.fixture(autouse=True)
def _no_provider_env(monkeypatch: pytest.MonkeyPatch) -> None:
    clear_provider_env(monkeypatch)
    fast_retries(monkeypatch)


# ── Which key: form, then env ────────────────────────────────────────


async def test_empty_key_uses_the_provider_env_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-env")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-env")
    openai = Recorder(always(lambda: openai_list("m")))
    anthropic = Recorder(always(lambda: openai_list("m")))

    assert ids(await discover(snap(api_key=""), openai)) == ["m"]
    assert ids(await discover(snap(provider="anthropic", api_key=""), anthropic)) == ["m"]

    assert openai.requests[0].headers["authorization"] == "Bearer sk-openai-env"
    assert anthropic.requests[0].headers["x-api-key"] == "sk-ant-env"
    assert all("sk-ant-env" not in json.dumps(dict(r.headers)) for r in openai.requests)
    assert all("sk-openai-env" not in json.dumps(dict(r.headers)) for r in anthropic.requests)


@pytest.mark.parametrize("form_key", ["sk-form", "  sk-form  "])
async def test_form_key_wins_over_the_env_key(monkeypatch: pytest.MonkeyPatch, form_key: str) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-from-env")
    recorder = Recorder(always(lambda: openai_list("m")))

    await discover(snap(api_key=form_key), recorder)

    assert recorder.requests[0].headers["authorization"] == "Bearer sk-form"
    assert all("sk-from-env" not in json.dumps(dict(r.headers)) for r in recorder.requests)


async def test_whitespace_key_falls_back_to_the_env_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-from-env")
    recorder = Recorder(always(lambda: openai_list("m")))

    await discover(snap(api_key="   "), recorder)

    assert recorder.requests[0].headers["authorization"] == "Bearer sk-from-env"


async def test_env_key_of_another_provider_is_not_used(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-env")
    recorder = Recorder(always(lambda: status(401)))

    result = await discover_full(snap(api_key=""), recorder)

    assert result.auth_rejected is True
    for request in recorder.requests:
        assert "authorization" not in request.headers
        assert "x-api-key" not in request.headers


async def test_empty_key_and_empty_env_key_send_no_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "")
    recorder = Recorder(always(lambda: openai_list("m")))

    await discover(snap(api_key=""), recorder)

    assert "authorization" not in recorder.requests[0].headers
    assert "x-api-key" not in recorder.requests[0].headers


# ── Which address: form, then env, then default ──────────────────────


@pytest.mark.parametrize(
    ("form_url", "env_url", "form_key", "env_key"),
    [
        pytest.param(
            "https://form.example.test/v1", "https://env.example.test/v1", "sk-form", "sk-env", id="form-url-wins"
        ),
        pytest.param("", "https://env.example.test/v1", "sk-form", "sk-env", id="env-url"),
        pytest.param("", None, "sk-form", "sk-env", id="default-url"),
        pytest.param("https://form.example.test/v1", None, "", "sk-env", id="env-key"),
        pytest.param("https://form.example.test/v1", None, "", None, id="no-key"),
    ],
)
def test_resolution_matches_what_the_chat_client_uses(
    monkeypatch: pytest.MonkeyPatch,
    form_url: str,
    env_url: str | None,
    form_key: str,
    env_key: str | None,
) -> None:
    provider = "openai"
    spec = PROVIDERS[provider]
    if env_url is not None:
        monkeypatch.setenv(spec.base_url_env, env_url)
    if env_key is not None:
        monkeypatch.setenv(spec.api_key_env, env_key)

    resolved = resolve_model_connection(ConnectionSnapshot(provider=provider, base_url=form_url, api_key=form_key))

    assert resolved.base_url == effective_model_base_url(
        ModelProfile(id="p", name="P", provider=provider, base_url=form_url)
    )
    assert resolved.api_key == (form_key or env_key or "")


@pytest.mark.parametrize("blank", ["", "   "])
async def test_blank_base_url_queries_the_env_address(monkeypatch: pytest.MonkeyPatch, blank: str) -> None:
    monkeypatch.setenv("OPENAI_BASE_URL", "https://env.example.test/v1")
    recorder = Recorder(always(lambda: openai_list("m")))

    models = await discover(snap(base_url=blank), recorder)

    assert ids(models) == ["m"]
    assert recorder.paths() == ["https://env.example.test/v1/models"]


@pytest.mark.parametrize(
    ("provider", "first_request"),
    [
        ("openai", "https://api.openai.com/v1/models"),
        ("anthropic", "https://api.anthropic.com/v1/models"),
        ("deepseek-openai", "https://api.deepseek.com/models"),
    ],
)
async def test_blank_base_url_without_env_queries_the_provider_default(provider: str, first_request: str) -> None:
    recorder = Recorder(always(lambda: openai_list("m")))

    models = await discover(snap(provider=provider, base_url=""), recorder)

    assert ids(models) == ["m"]
    assert recorder.paths()[0] == first_request
    default_host = httpx.URL(PROVIDERS[provider].default_base_url).host
    assert {request.url.host for request in recorder.requests} == {default_host}


async def test_blank_base_url_for_glm_queries_its_default_host() -> None:
    recorder = Recorder(always(lambda: openai_list("m")))

    assert ids(await discover(snap(provider="glm-openai", base_url=""), recorder)) == ["m"]

    assert {request.url.host for request in recorder.requests} == {
        httpx.URL(PROVIDERS["glm-openai"].default_base_url).host
    }


async def test_form_base_url_wins_over_the_env_address(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_BASE_URL", "https://env.example.test/v1")
    recorder = Recorder(always(lambda: openai_list("m")))

    await discover(snap(base_url="https://gw.example.test/v1"), recorder)

    assert {request.url.host for request in recorder.requests} == {"gw.example.test"}


async def test_env_address_of_another_provider_is_not_used(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://ant-env.example.test")
    recorder = Recorder(always(lambda: openai_list("m")))

    await discover(snap(base_url=""), recorder)

    assert {request.url.host for request in recorder.requests} == {"api.openai.com"}


async def test_env_address_and_env_key_together(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://ds-env.example.test")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-ds-env")
    recorder = Recorder(always(lambda: openai_list("m")))

    await discover(snap(provider="deepseek-openai", base_url="", api_key=""), recorder)

    assert recorder.requests[0].url.host == "ds-env.example.test"
    assert recorder.requests[0].headers["authorization"] == "Bearer sk-ds-env"


# ── No response at all ───────────────────────────────────────────────


def _raise(exc_type: type[httpx.TransportError]) -> Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        raise exc_type("no answer", request=request)

    return handler


@pytest.mark.parametrize(("exc_type", "api_key"), [(httpx.ConnectError, ""), (httpx.ProxyError, "sk-a")])
async def test_connection_level_failure_everywhere_is_unreachable(
    exc_type: type[httpx.TransportError], api_key: str
) -> None:
    recorder = Recorder(_raise(exc_type))

    result = await discover_full(snap(base_url="https://gw.example.test/v1", api_key=api_key), recorder)

    assert result.models == []
    assert result.unreachable is True
    assert result.auth_rejected is False
    assert result.corrected_base_url is None
    assert recorder.requests


async def test_no_response_within_the_deadline_is_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(model_list_module, "_TOTAL_SECONDS", 0.3)

    async def never(_request: httpx.Request) -> httpx.Response:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    result = await discover_full(snap(), Recorder(never))

    assert result.models == []
    assert result.unreachable is True


@pytest.mark.parametrize(
    "response",
    [
        html,
        lambda: status(404),
        lambda: status(500),
        lambda: status(401),
    ],
    ids=["html-200", "404", "500", "401"],
)
async def test_any_http_response_is_not_unreachable(response: Callable[[], httpx.Response]) -> None:
    result = await discover_full(snap(api_key="sk-a"), Recorder(always(response)))

    assert result.models == []
    assert result.unreachable is False


async def test_one_location_left_unanswered_is_unreachable_not_not_found() -> None:
    """The convention location answers 404, the rest never connect: not every location was really tried."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/models":
            return status(404)
        raise httpx.ConnectError("refused", request=request)

    recorder = Recorder(handler)
    result = await discover_full(snap(base_url="https://gw.example.test/v1"), recorder)

    assert len(set(recorder.paths())) >= 2
    assert result.models == []
    assert result.unreachable is True


async def test_a_found_list_is_not_unreachable() -> None:
    result = await discover_full(snap(), Recorder(always(lambda: openai_list("m"))))

    assert result.unreachable is False


@pytest.mark.parametrize(
    "kwargs",
    [{"api_key": "{{!pass show key}}"}, {"api_key": "{{ML_TEST_MISSING}}"}],
    ids=["command", "missing-env"],
)
async def test_no_request_sent_is_not_unreachable(monkeypatch: pytest.MonkeyPatch, kwargs: dict[str, Any]) -> None:
    """Nothing was attempted, so nothing failed to connect: the screen shows "not found"."""
    monkeypatch.delenv("ML_TEST_MISSING", raising=False)
    recorder = Recorder(always(lambda: openai_list("m")))

    result = await discover_full(snap(**kwargs), recorder)

    assert recorder.requests == []
    assert result.unreachable is False


# ── Best effort: round one tries everything, later rounds retry only what got no answer ──


def refuse(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("refused", request=request)


async def _planned(where: dict[str, str]) -> list[str]:
    """Every location a lookup plans for *where*, in order (all of them answer 404)."""
    recorder = Recorder(always(lambda: status(404)))
    await discover_full(snap(**where, api_key=""), recorder)
    return list(dict.fromkeys(recorder.paths()))


WHERE = pytest.mark.parametrize(
    "where",
    [
        {"provider": "openai", "base_url": "https://gw.example.test/llm/v1"},
        {"provider": "anthropic", "base_url": "https://gw.example.test/v1"},
        {"provider": "openai", "base_url": "https://gw.example.test"},
    ],
    ids=["openai-sub-path", "anthropic-v1", "openai-missing-v1"],
)


@pytest.mark.parametrize(
    ("where", "failure"),
    [
        ({"provider": "openai", "base_url": "https://gw.example.test/llm/v1"}, failure)
        for failure in (
            httpx.ConnectError,
            httpx.ConnectTimeout,
            httpx.ProxyError,
            httpx.ReadTimeout,
            httpx.RemoteProtocolError,
        )
    ]
    + [
        ({"provider": "anthropic", "base_url": "https://gw.example.test/v1"}, httpx.ConnectError),
        ({"provider": "openai", "base_url": "https://gw.example.test"}, httpx.ConnectError),
    ],
)
async def test_round_one_tries_every_location_once_then_rounds_retry_them(
    where: dict[str, str], failure: type[httpx.TransportError]
) -> None:
    planned = await _planned(where)

    def handler(request: httpx.Request) -> httpx.Response:
        raise failure("no answer", request=request)

    recorder = Recorder(handler)
    rounds: list[tuple[int, int]] = []
    result = await discover_models(
        snap(**where, api_key=""), transport=recorder.transport, on_retry=lambda *args: rounds.append(args)
    )

    # Round one: every location once, in order, with no retry on the spot.
    assert recorder.paths()[: len(planned)] == planned
    # Then four retry rounds, each over the same locations in the same order.
    assert recorder.paths() == planned * REAL_TOTAL_ROUNDS
    assert rounds == [(2, 5), (3, 5), (4, 5), (5, 5)]
    assert result.models == []
    assert result.unreachable is True


async def test_only_unanswered_locations_are_retried() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/models":
            return status(404)
        raise httpx.ConnectError("refused", request=request)

    recorder = Recorder(handler)
    result = await discover_full(snap(base_url="https://gw.example.test/v1", api_key=""), recorder)

    counts = calls_per_url(recorder)
    assert counts.pop("https://gw.example.test/v1/models") == 1
    assert counts
    assert set(counts.values()) == {REAL_TOTAL_ROUNDS}
    assert result.unreachable is True


async def test_a_location_answering_in_round_two_counts_as_answered() -> None:
    recorder = Recorder(flaky(lambda _request: status(404)))
    rounds: list[tuple[int, int]] = []

    result = await discover_models(snap(api_key=""), transport=recorder.transport, on_retry=lambda *a: rounds.append(a))

    assert set(calls_per_url(recorder).values()) == {2}
    assert rounds == [(2, 5)]
    assert result.models == []
    assert result.unreachable is False, "every location answered in round two: nothing is there"


async def test_a_list_found_in_a_retry_round_ends_the_lookup() -> None:
    def answer(request: httpx.Request) -> httpx.Response:
        return openai_list("m") if request.url.path == "/v1/models" else status(404)

    recorder = Recorder(flaky(answer, paths=("/v1/models",)))
    rounds: list[tuple[int, int]] = []

    result = await discover_models(snap(api_key=""), transport=recorder.transport, on_retry=lambda *a: rounds.append(a))

    assert ids(result.models) == ["m"]
    assert rounds == [(2, 5)]
    paths = recorder.paths()
    assert paths[0] == paths[-1] == "https://gw.example.test/v1/models"
    assert paths.count("https://gw.example.test/v1/models") == 2


async def test_a_list_found_in_round_one_needs_no_retry_round() -> None:
    recorder = Recorder(flaky(openai_list_answer("m"), paths=("/v1/models",)))
    rounds: list[tuple[int, int]] = []

    result = await discover_models(
        snap(base_url="https://gw.example.test/v1"), transport=recorder.transport, on_retry=lambda *a: rounds.append(a)
    )

    assert ids(result.models) == ["m"]
    # The convention location failed; the base itself answered with the list in the same round.
    assert rounds == []


async def test_a_per_request_timeout_is_retried_in_the_next_round(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(model_list_module, "_REQUEST_SECONDS", 0.05)
    attempts = 0

    async def slow_then_list(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        if request.url.path != "/v1/models":
            return status(404)
        attempts += 1
        if attempts == 1:
            await asyncio.sleep(1)
        return openai_list("m")

    result = await discover_full(snap(), Recorder(slow_then_list))

    assert ids(result.models) == ["m"]
    assert attempts == 2


async def test_a_failure_while_reading_the_body_is_answered_and_not_retried() -> None:
    """Headers arrived, so the location answered; a broken body is no reason to retry it."""

    class BrokenBody(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            raise httpx.ReadError("reset")
            yield b""  # pragma: no cover

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/models":
            return httpx.Response(200, headers={"content-type": "application/json"}, stream=BrokenBody())
        return status(404)

    recorder = Recorder(handler)
    rounds: list[tuple[int, int]] = []
    result = await discover_models(snap(), transport=recorder.transport, on_retry=lambda *a: rounds.append(a))

    assert rounds == []
    assert set(calls_per_url(recorder).values()) == {1}
    assert result.unreachable is False


async def test_pauses_between_rounds_are_1_2_4_4_seconds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(model_list_module, "_RETRY_BASE_SECONDS", REAL_RETRY_BASE_SECONDS)
    monkeypatch.setattr(model_list_module, "_RETRY_MAX_PAUSE_SECONDS", REAL_RETRY_MAX_PAUSE_SECONDS)
    monkeypatch.setattr(model_list_module, "_TOTAL_SECONDS", REAL_TOTAL_SECONDS)
    pauses: list[float] = []
    real_sleep = asyncio.sleep

    async def recorded_sleep(delay: float, result: Any = None) -> Any:
        pauses.append(delay)
        return await real_sleep(0, result)

    monkeypatch.setattr(model_list_module.asyncio, "sleep", recorded_sleep)

    result = await discover_full(snap(), Recorder(refuse))

    assert pauses == [1.0, 2.0, 4.0, 4.0]
    assert result.unreachable is True


async def test_no_retry_round_when_the_next_pause_does_not_fit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(model_list_module, "_RETRY_BASE_SECONDS", REAL_RETRY_BASE_SECONDS)
    monkeypatch.setattr(model_list_module, "_RETRY_MAX_PAUSE_SECONDS", REAL_RETRY_MAX_PAUSE_SECONDS)
    monkeypatch.setattr(model_list_module, "_TOTAL_SECONDS", 0.3)
    recorder = Recorder(refuse)
    rounds: list[tuple[int, int]] = []

    started = time.monotonic()
    result = await discover_models(snap(api_key=""), transport=recorder.transport, on_retry=lambda *a: rounds.append(a))

    assert time.monotonic() - started < 0.3 + 0.5
    assert rounds == [], "less than the first 1 s pause remained: no retry round"
    assert set(calls_per_url(recorder).values()) == {1}
    assert result.unreachable is True


async def test_retries_do_not_use_up_the_request_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    """Six locations x two auth groups are at least 12 different requests; each may still be retried."""
    preset = ModelListPreset(scheme="https", host="gw.example.test", port=443, list_path="/catalog")
    monkeypatch.setattr(model_list_presets, "PRESETS", (preset,))
    seen: set[tuple[str, str | None]] = set()

    def handler(request: httpx.Request) -> httpx.Response:
        key = (str(request.url), request.headers.get("authorization") or request.headers.get("x-api-key"))
        if key not in seen:
            seen.add(key)
            raise httpx.ConnectError("flaky", request=request)
        return status(401)

    recorder = Recorder(handler)
    result = await discover_full(snap(base_url="https://gw.example.test/api/v1"), recorder)

    # Every distinct request was made and retried once; the retries did not crowd any out.
    assert len(seen) >= 12
    assert len(recorder.requests) == 2 * len(seen)
    assert result.auth_rejected is True


async def test_the_total_deadline_bounds_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(model_list_module, "_TOTAL_SECONDS", 0.4)

    async def slow_failure(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.05)
        raise httpx.ConnectTimeout("slow", request=request)

    started = time.monotonic()
    result = await discover_full(snap(), Recorder(slow_failure))
    elapsed = time.monotonic() - started

    assert elapsed < 0.4 + 1.0, elapsed
    assert result.models == []
    assert result.unreachable is True


@pytest.mark.parametrize("code", [401, 403])
async def test_auth_rejection_wins_over_unanswered_locations(code: int) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/models":
            return status(code)
        raise httpx.ConnectError("refused", request=request)

    result = await discover_full(snap(api_key=""), Recorder(handler))

    assert result.models == []
    assert result.auth_rejected is True


async def test_list_found_after_connection_errors_wins_and_is_not_unreachable() -> None:
    """The reported yibuapi case: 404, then a failed handshake, then the /v1 correction finds the list."""

    def answer(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/models":
            return item_marked("claude-a")
        return status(404)

    recorder = Recorder(flaky(answer, fails=3, paths=("/v1",)))
    result = await discover_full(snap(provider="anthropic", base_url="https://gw.example.test/v1"), recorder)

    assert ids(result.models) == ["claude-a"]
    assert result.corrected_base_url == "https://gw.example.test"
    assert result.unreachable is False


def test_timing_is_5_rounds_within_60_seconds_and_5_per_request() -> None:
    assert REAL_TOTAL_SECONDS == 60.0
    assert REAL_REQUEST_SECONDS == 5.0
    assert REAL_TOTAL_ROUNDS == 5
    assert (REAL_RETRY_BASE_SECONDS, REAL_RETRY_MAX_PAUSE_SECONDS) == (1.0, 4.0)


def test_the_snapshot_has_no_retry_count() -> None:
    """Retrying is bounded by five rounds and the total deadline; the form's HTTP Max Retries is not part of the lookup."""
    assert "http_max_retries" not in {field.name for field in dataclasses.fields(ConnectionSnapshot)}


async def test_with_a_key_round_one_tries_both_auth_groups_of_an_unanswered_location() -> None:
    """No answer says nothing about the credentials, so the other auth group is tried in the same round."""
    recorder = Recorder(refuse)

    await discover_full(snap(api_key="sk-a"), recorder)

    first = recorder.requests[:2]
    assert [str(r.url) for r in first] == ["https://gw.example.test/v1/models"] * 2
    assert {("authorization" in r.headers, "x-api-key" in r.headers) for r in first} == {(True, False), (False, True)}


async def test_discovery_client_uses_transport_settings_without_hooks(monkeypatch: pytest.MonkeyPatch) -> None:
    build = model_list_module._build_profile_http_client
    built: list[httpx.AsyncClient] = []

    def capture(profile: ModelProfile, timeout: Any, **kwargs: Any) -> httpx.AsyncClient:
        assert profile.verify_ssl is False
        assert profile.bypass_proxy is True
        assert profile.http_connect_timeout == 0.75
        assert kwargs["enable_hooks"] is False
        assert kwargs["follow_redirects"] is False
        client = build(profile, timeout, **kwargs)
        assert client.event_hooks == {"request": [], "response": []}
        built.append(client)
        return client

    monkeypatch.setattr(model_list_module, "_build_profile_http_client", capture)
    recorder = Recorder(always(lambda: openai_list("m")))
    result = await discover_full(snap(verify_ssl=False, bypass_proxy=True, connect_timeout=0.75), recorder)
    assert ids(result.models) == ["m"]
    assert len(built) == 1
    assert recorder.requests[0].extensions["timeout"]["connect"] == 0.75
    assert built[0].is_closed


async def test_unanswered_redirect_retries_only_its_target() -> None:
    def answer(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/models":
            return httpx.Response(302, headers={"location": "/catalog"})
        if request.url.path == "/catalog":
            raise httpx.ConnectError("refused", request=request)
        return status(404)

    recorder = Recorder(answer)
    result = await discover_full(snap(api_key=""), recorder)
    assert result.unreachable
    assert recorder.paths() == [
        "https://gw.example.test/v1/models",
        "https://gw.example.test/catalog",
        "https://gw.example.test/v1",
        "https://gw.example.test/models",
        *(["https://gw.example.test/catalog"] * 4),
    ]
