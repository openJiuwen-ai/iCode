# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Spec tests for the address corrections ``discover_models`` tries after the address as typed.

Written from ``.plan/model-list-fetch.md`` (address corrections: missing or extra ``/v1``, a pasted
endpoint path, a missing scheme, ``http`` to ``https``, loopback-only ``https`` to ``http``) and the
interface contract ``.plan/model-list-interface.md``. A correction is reported only when a strictly
marked list was found at the corrected address, and every request stays on the typed host so the
key never leaves it. Every request goes to an ``httpx.MockTransport``.
"""

from __future__ import annotations

import httpx
import pytest

from chrys.service.llm.model_list import UrlCorrection
from tests.service.llm._model_list_support import (
    Handler,
    Recorder,
    clear_provider_env,
    discover_full,
    fast_retries,
    html,
    ids,
    item_marked,
    snap,
)

KEY = "sk-secret-key"


@pytest.fixture(autouse=True)
def _no_provider_env(monkeypatch: pytest.MonkeyPatch) -> None:
    clear_provider_env(monkeypatch)
    fast_retries(monkeypatch)


def site(list_url: str, *, refuse_scheme: str | None = None) -> Handler:
    """A host that answers a strict list only at *list_url* and a web page everywhere else.

    Requests over *refuse_scheme* fail to connect, as https to a plain-http port does.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if refuse_scheme is not None and request.url.scheme == refuse_scheme:
            raise httpx.ConnectError("wrong scheme", request=request)
        if str(request.url.copy_with(query=None)) == list_url:
            return item_marked("m-1", "m-2")
        return html()

    return handler


def nothing_anywhere() -> Handler:
    return lambda _request: html()


def assert_key_stays_on(recorder: Recorder, host: str) -> None:
    """Every request, and so every request carrying the key, went to the host the user typed."""
    assert recorder.requests
    assert {request.url.host for request in recorder.requests} == {host}, recorder.urls()


async def corrected(
    provider: str, base_url: str, handler: Handler
) -> tuple[Recorder, list[str], str | None, tuple[UrlCorrection, ...]]:
    recorder = Recorder(handler)
    result = await discover_full(snap(provider=provider, base_url=base_url, api_key=KEY), recorder)
    return recorder, ids(result.models), result.corrected_base_url, result.url_corrections


# ── Each correction: found → corrected base; the typed address comes first ──


CASES = [
    pytest.param(
        "openai",
        "https://gw.example.test",
        "https://gw.example.test/v1/models",
        "https://gw.example.test/v1",
        ("added_v1",),
        id="added-v1",
    ),
    pytest.param(
        "anthropic",
        "https://gw.example.test/v1",
        "https://gw.example.test/v1/models",
        "https://gw.example.test",
        ("removed_v1",),
        id="removed-v1",
    ),
    pytest.param(
        "openai",
        "http://gw.example.test/v1",
        "https://gw.example.test/v1/models",
        "https://gw.example.test/v1",
        ("switched_https",),
        id="switched-https",
    ),
    pytest.param(
        "openai",
        "https://localhost:11434/v1",
        "http://localhost:11434/v1/models",
        "http://localhost:11434/v1",
        ("switched_http",),
        id="switched-http",
    ),
    pytest.param(
        "openai",
        "https://gw.example.test/v1/chat/completions",
        "https://gw.example.test/v1/models",
        "https://gw.example.test/v1",
        ("removed_endpoint",),
        id="endpoint-chat-completions",
    ),
    pytest.param(
        "openai",
        "https://gw.example.test/v1/completions",
        "https://gw.example.test/v1/models",
        "https://gw.example.test/v1",
        ("removed_endpoint",),
        id="endpoint-completions",
    ),
    pytest.param(
        "openai",
        "https://gw.example.test/v1/responses",
        "https://gw.example.test/v1/models",
        "https://gw.example.test/v1",
        ("removed_endpoint",),
        id="endpoint-responses",
    ),
    pytest.param(
        "openai",
        "https://gw.example.test/v1/embeddings",
        "https://gw.example.test/v1/models",
        "https://gw.example.test/v1",
        ("removed_endpoint",),
        id="endpoint-embeddings",
    ),
    pytest.param(
        "anthropic",
        "https://gw.example.test/v1/messages",
        "https://gw.example.test/v1/models",
        "https://gw.example.test",
        ("removed_endpoint",),
        id="anthropic-endpoint-messages",
    ),
    pytest.param(
        "openai",
        "https://gw.example.test/chat/completions",
        "https://gw.example.test/v1/models",
        "https://gw.example.test/v1",
        ("removed_endpoint", "added_v1"),
        id="endpoint-without-v1-then-added-v1",
    ),
    pytest.param(
        "openai",
        "gw.example.test/v1",
        "https://gw.example.test/v1/models",
        "https://gw.example.test/v1",
        ("added_https",),
        id="added-scheme",
    ),
    pytest.param(
        "openai",
        "gw.example.test:8080/v1",
        "https://gw.example.test:8080/v1/models",
        "https://gw.example.test:8080/v1",
        ("added_https",),
        id="added-scheme-with-port",
    ),
    pytest.param(
        "openai",
        "gw.example.test",
        "https://gw.example.test/v1/models",
        "https://gw.example.test/v1",
        ("added_https", "added_v1"),
        id="added-scheme-and-v1",
    ),
]


@pytest.mark.parametrize(("provider", "typed", "list_url", "expected", "expected_corrections"), CASES)
async def test_a_correction_that_finds_a_list_returns_the_corrected_base(
    provider: str, typed: str, list_url: str, expected: str, expected_corrections: tuple[UrlCorrection, ...]
) -> None:
    recorder, found, base, corrections = await corrected(provider, typed, site(list_url))

    assert found == ["m-1", "m-2"]
    assert base == expected
    assert corrections == expected_corrections
    assert recorder.requests[0].url.scheme == (typed.split("://", maxsplit=1)[0] if "://" in typed else "https")
    assert_key_stays_on(recorder, httpx.URL(list_url).host)


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "[::1]"])
async def test_loopback_https_is_switched_to_http(host: str) -> None:
    typed = f"https://{host}:11434/v1"
    recorder, found, base, corrections = await corrected(
        "openai", typed, site(f"http://{host}:11434/v1/models", refuse_scheme="https")
    )

    assert found == ["m-1", "m-2"]
    assert base == f"http://{host}:11434/v1"
    assert corrections == ("switched_http",)
    assert recorder.requests[0].url.scheme == "https"


@pytest.mark.parametrize(
    "typed",
    [
        "https://192.168.1.5:11434/v1",
        "https://10.0.0.7/v1",
        "https://gw.example.test/v1",
        "https://localhost.example.test/v1",
    ],
    ids=["lan-192", "lan-10", "remote", "lookalike"],
)
async def test_https_is_never_downgraded_off_loopback(typed: str) -> None:
    """The key would leave the machine in clear text: no http request, even when https never connects."""
    host = httpx.URL(typed).host
    recorder, found, base, corrections = await corrected(
        "openai", typed, site(f"http://{host}/v1/models", refuse_scheme="https")
    )

    assert found == []
    assert base is None
    assert corrections == ()
    assert all(request.url.scheme == "https" for request in recorder.requests), recorder.urls()


@pytest.mark.parametrize(
    "typed",
    [
        "https://gw.example.test",
        "https://gw.example.test/v1/chat/completions",
        "gw.example.test/v1",
        "http://gw.example.test/v1",
    ],
    ids=["missing-v1", "endpoint", "no-scheme", "http"],
)
async def test_no_correction_is_reported_without_a_strict_list(typed: str) -> None:
    recorder, found, base, corrections = await corrected("openai", typed, nothing_anywhere())

    assert found == []
    assert base is None
    assert corrections == ()
    assert_key_stays_on(recorder, "gw.example.test")


async def test_a_loose_list_at_a_corrected_address_is_not_enough() -> None:
    """Only an item-marked list proves the correction; a bare array of objects does not."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/models":
            return httpx.Response(200, json=[{"id": "x"}, {"id": "y"}])
        return html()

    _recorder, found, base, corrections = await corrected("openai", "https://gw.example.test", handler)

    assert found == []
    assert base is None
    assert corrections == ()


async def test_the_typed_address_wins_when_it_has_the_list() -> None:
    recorder, found, base, corrections = await corrected(
        "openai", "http://gw.example.test/v1", site("http://gw.example.test/v1/models")
    )

    assert found == ["m-1", "m-2"]
    assert base is None
    assert corrections == ()
    assert all(request.url.scheme == "http" for request in recorder.requests)


async def test_corrections_never_leave_the_typed_host() -> None:
    """Even with every correction tried and nothing found, no request goes to another host."""
    hosts: set[str] = set()

    def handler(request: httpx.Request) -> httpx.Response:
        hosts.add(request.url.host)
        return html()

    for typed in ("gw.example.test", "http://gw.example.test/chat/completions", "https://gw.example.test/v1/messages"):
        await corrected("openai", typed, handler)
        await corrected("anthropic", typed, handler)

    assert hosts == {"gw.example.test"}


async def test_a_correction_found_after_connection_errors_still_counts() -> None:
    """Best effort: the typed address never connects; the corrected one answers with the list."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/models":
            return item_marked("m")
        raise httpx.ConnectError("refused", request=request)

    recorder = Recorder(handler)
    result = await discover_full(snap(base_url="https://gw.example.test"), recorder)

    assert ids(result.models) == ["m"]
    assert result.corrected_base_url == "https://gw.example.test/v1"
    assert result.unreachable is False


# ── A pasted list URL: the list answers there, but it is no base for chat ──


@pytest.mark.parametrize(
    ("provider", "typed", "list_url", "expected_base", "expected_corrections"),
    [
        pytest.param(
            "openai",
            "https://gw.example.test/v1/models",
            "https://gw.example.test/v1/models",
            "https://gw.example.test/v1",
            ("removed_endpoint",),
            id="openai-v1-models",
        ),
        pytest.param(
            "anthropic",
            "https://gw.example.test/v1/models",
            "https://gw.example.test/v1/models",
            "https://gw.example.test",
            ("removed_endpoint",),
            id="anthropic-v1-models",
        ),
        pytest.param(
            "openai",
            "https://gw.example.test/models",
            "https://gw.example.test/models",
            "https://gw.example.test",
            ("removed_endpoint",),
            id="openai-models-list-there",
        ),
        pytest.param(
            "openai",
            "https://gw.example.test/models",
            "https://gw.example.test/v1/models",
            "https://gw.example.test/v1",
            ("removed_endpoint", "added_v1"),
            id="openai-models-list-under-v1",
        ),
    ],
)
async def test_a_pasted_list_url_is_corrected_to_its_base(
    provider: str, typed: str, list_url: str, expected_base: str, expected_corrections: tuple[str, ...]
) -> None:
    recorder = Recorder(site(list_url))

    result = await discover_full(snap(provider=provider, base_url=typed, api_key=KEY), recorder)

    assert ids(result.models) == ["m-1", "m-2"]
    assert result.corrected_base_url == expected_base
    assert result.url_corrections == expected_corrections
    assert_key_stays_on(recorder, "gw.example.test")


async def test_a_pasted_list_url_without_a_strict_list_is_not_corrected() -> None:
    """A loose list at the pasted URL is not proof enough to rewrite the address."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/models":
            return httpx.Response(200, json=[{"id": "x"}, {"id": "y"}])
        return html()

    recorder = Recorder(handler)
    result = await discover_full(snap(base_url="https://gw.example.test/v1/models"), recorder)

    assert result.corrected_base_url is None
    assert result.url_corrections == ()
