# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Provider fallback: deadline shares, which failures advance, site operators and the card metadata."""

from __future__ import annotations

import asyncio
import json
import ssl
from collections.abc import Callable
from unittest.mock import patch
from urllib.parse import urlsplit

import httpx
import pytest

from chrys.foundation.tool_result_metadata import WEB_SEARCH_METADATA_KEY, WEB_SEARCH_TITLE_MAX_CHARS
from chrys.service.agent_middleware.events.result_persistence import persistable_result_metadata
from chrys.service.tools.builtins.web.build import create_provider
from chrys.service.tools.builtins.web.config import parse_provider
from chrys.service.tools.builtins.web.credentials import ResolvedSearchCredentials
from chrys.service.tools.builtins.web.http import BODY_LIMIT, DestinationPolicy, WebEgressRuntime, WebError
from chrys.service.tools.builtins.web.search.providers import ENVIRONMENT_FAILURES, SearchProvider, site_filtered
from chrys.service.tools.builtins.web.search.tool import WebSearchTools
from chrys.service.tools.builtins.web.search.types import SearchRequest
from chrys.service.tools.result_metadata import tool_result_metadata
from tests.service.tools.web._support import Chunks, SocketNetwork, socket_network

_RESULTS = {"results": [{"title": "Found", "url": "https://example.com/a", "content": "snippet"}]}


def _tavily(name: str) -> SearchProvider:
    return create_provider(
        name, parse_provider({"type": "tavily", "api_key_env": "KEY"}), ResolvedSearchCredentials({"KEY": "secret"})
    )


def _anonymous(name: str) -> SearchProvider:
    return create_provider(name, parse_provider({"type": "duckduckgo_html"}), ResolvedSearchCredentials({}))


def _ok() -> httpx.Response:
    return httpx.Response(
        200, headers={"Content-Type": "application/json"}, stream=Chunks(json.dumps(_RESULTS).encode())
    )


async def _search(tools: WebSearchTools, handle) -> tuple[str, dict]:
    metadata: dict = {}
    token = tool_result_metadata.set(metadata)
    try:
        with patch.object(
            tools.runtime,
            "client",
            autospec=True,
            side_effect=lambda policy, *, timeout: httpx.AsyncClient(transport=httpx.MockTransport(handle)),
        ):
            result = await tools.web_search("query")
    finally:
        tool_result_metadata.reset(token)
    return result, metadata


async def test_a_stalled_provider_leaves_the_next_one_its_share_of_the_deadline():
    stalled = asyncio.Event()
    hosts: list[str] = []

    async def handle(request: httpx.Request) -> httpx.Response:
        hosts.append(request.url.host)
        if len(hosts) == 1:
            await stalled.wait()  # never set: only the provider's deadline share ends it
        return _ok()

    # The stalled provider holds half of the deadline; the rest must also absorb a
    # loaded CI shard's scheduling delay for the next provider's reply and rendering.
    tools = WebSearchTools(
        (_tavily("slow"), _tavily("fast")), WebEgressRuntime(), DestinationPolicy(), timeout_seconds=8
    )
    result, metadata = await _search(tools, handle)
    assert not result.startswith("Error:"), result
    assert len(hosts) == 2  # the stalled share is not retried against the same provider
    assert json.loads(result.split("\nExternal", 1)[0])["provider"] == "fast"
    assert metadata["web_search"]["provider"] == "fast"


async def test_an_anonymous_providers_refusal_falls_through_to_the_next_provider():
    seen: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.host)
        if request.url.host == "html.duckduckgo.com":
            return httpx.Response(403, headers={"Content-Type": "text/html"}, stream=Chunks(b"blocked"))
        return _ok()

    tools = WebSearchTools((_anonymous("ddg"), _tavily("paid")), WebEgressRuntime(), DestinationPolicy())
    result, _ = await _search(tools, handle)
    assert not result.startswith("Error:"), result
    assert seen == ["html.duckduckgo.com", "api.tavily.com"]


async def test_a_credentialed_providers_refusal_is_reported_with_its_status():
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, headers={"Content-Type": "application/json"}, stream=Chunks(b"{}"))

    tools = WebSearchTools((_tavily("first"), _tavily("second")), WebEgressRuntime(), DestinationPolicy())
    result, metadata = await _search(tools, handle)
    assert result == "Error: http_4xx (HTTP 401)"
    assert metadata["tool_error_code"] == "http_4xx"


async def test_card_metadata_keeps_only_the_provider_and_sources_and_reaches_session_history():
    long_title = "T" * 1000
    results = [*_RESULTS["results"], {"title": long_title, "url": "https://example.com/b", "content": "snippet"}]

    def handle(request: httpx.Request) -> httpx.Response:
        body = json.dumps({"results": results}).encode()
        return httpx.Response(200, headers={"Content-Type": "application/json"}, stream=Chunks(body))

    tools = WebSearchTools((_tavily("main"),), WebEgressRuntime(), DestinationPolicy())
    result, metadata = await _search(tools, handle)
    card = {
        "provider": "main",
        "results": [
            {"title": "Found", "url": "https://example.com/a"},
            # History keeps the card's sources, so a title is no longer than the card shows.
            {"title": long_title[:WEB_SEARCH_TITLE_MAX_CHARS], "url": "https://example.com/b"},
        ],
    }
    assert metadata[WEB_SEARCH_METADATA_KEY] == card
    assert long_title in result
    # Replay renders the card from what history kept; without it a reopened session shows the raw JSON.
    assert persistable_result_metadata(metadata) == {WEB_SEARCH_METADATA_KEY: card}


_PUBLIC = "93.184.215.14"
_BRAVE_HOST = "api.search.brave.com"


def _brave(name: str) -> SearchProvider:
    return create_provider(
        name, parse_provider({"type": "brave", "api_key_env": "KEY"}), ResolvedSearchCredentials({"KEY": "secret"})
    )


def _brave_ok() -> httpx.Response:
    body = {"web": {"results": [{"title": "Found", "url": "https://example.com/a", "description": "snippet"}]}}
    return httpx.Response(200, headers={"Content-Type": "application/json"}, stream=Chunks(json.dumps(body).encode()))


def _response(status: int, mime: str = "application/json", body: bytes = b"{}", **headers: str) -> httpx.Response:
    return httpx.Response(status, headers={"Content-Type": mime, **headers}, stream=Chunks(body))


def _certificate_rejected() -> httpx.Response:
    """What httpx raises when TLS rejects a certificate: the verification error is its cause."""
    error = httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed")
    error.__cause__ = ssl.SSLCertVerificationError(1, "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed")
    raise error


def _never_sent() -> httpx.Response:
    raise AssertionError("a request refused before connecting reached the socket layer")


# code -> (first provider's factory, its DNS answer, its reply at the socket layer)
_PROVIDER_FAILURES: dict[str, tuple[Callable[[str], SearchProvider], str | Exception, Callable[[], httpx.Response]]] = {
    "protocol_error": (_tavily, _PUBLIC, lambda: _response(200, "text/html", b"<html></html>")),
    "search_parse_failed": (_anonymous, _PUBLIC, lambda: _response(200, "text/html", b"<html>Please log in</html>")),
    "search_challenge": (_anonymous, _PUBLIC, lambda: _response(202, "text/html")),
    "connection_failed": (_tavily, _PUBLIC, _certificate_rejected),
    "private_address_blocked": (_tavily, "10.0.0.9", _never_sent),
    "dns_failure": (_tavily, WebError("dns_failure"), _never_sent),
    "redirect_not_allowed": (_tavily, _PUBLIC, lambda: _response(302, Location="https://elsewhere.example/")),
    "unsupported_content_encoding": (_tavily, _PUBLIC, lambda: _response(200, **{"Content-Encoding": "br"})),
    "response_too_large": (_tavily, _PUBLIC, lambda: _response(200, **{"Content-Length": str(BODY_LIMIT + 1)})),
}


async def _search_on_network(
    tools: WebSearchTools, reply: Callable[[httpx.Request], httpx.Response], addresses: dict, query: str = "query"
) -> tuple[str, dict, SocketNetwork, list[str]]:
    """Search through the real egress client, answering beneath its validated transport."""
    hosts: list[str] = []

    async def handle(request: httpx.Request) -> httpx.Response:
        hosts.append(request.headers["Host"])
        return reply(request)

    metadata: dict = {}
    token = tool_result_metadata.set(metadata)
    try:
        with socket_network(handle, addresses) as network:
            result = await tools.web_search(query)
    finally:
        tool_result_metadata.reset(token)
    return result, metadata, network, hosts


@pytest.mark.parametrize("code", sorted(_PROVIDER_FAILURES))
async def test_a_deterministic_provider_failure_advances_to_the_next_provider(code):
    factory, address, failure = _PROVIDER_FAILURES[code]
    first = factory("first")
    first_host = urlsplit(first.endpoint).hostname
    addresses = {first_host: address, _BRAVE_HOST: _PUBLIC}

    def reply(request: httpx.Request) -> httpx.Response:
        return failure() if request.headers["Host"] == first_host else _brave_ok()

    # Alone, the provider reports exactly this code, so the chain below advances past it.
    alone, metadata, _, hosts = await _search_on_network(
        WebSearchTools((first,), WebEgressRuntime(), DestinationPolicy()), reply, addresses
    )
    assert alone.startswith(f"Error: {code}"), alone
    assert metadata["tool_error_code"] == code
    assert metadata["tool_error_retryable"] is False
    first_hosts = [] if address != _PUBLIC else [first_host]
    assert hosts == first_hosts  # a deterministic failure is not retried

    result, metadata, _, hosts = await _search_on_network(
        WebSearchTools((first, _brave("second")), WebEgressRuntime(), DestinationPolicy()), reply, addresses
    )
    assert not result.startswith("Error:"), result
    assert metadata["web_search"]["provider"] == "second"
    assert hosts == [*first_hosts, _BRAVE_HOST]


async def test_a_malformed_credential_stops_the_chain():
    # A configuration error is not the provider failing: the next provider would not fix it.
    first = create_provider(
        "first",
        parse_provider({"type": "tavily", "api_key_env": "KEY"}),
        ResolvedSearchCredentials({"KEY": "secret\r\nInjected: header"}),
    )
    tools = WebSearchTools((first, _brave("second")), WebEgressRuntime(), DestinationPolicy())
    result, metadata, network, _ = await _search_on_network(
        tools, lambda request: _never_sent(), {"api.tavily.com": _PUBLIC, _BRAVE_HOST: _PUBLIC}
    )
    assert result == "Error: invalid_request"
    assert metadata["tool_error_code"] == "invalid_request"
    assert metadata["tool_error_retryable"] is False
    network.resolve.assert_not_called()
    network.send.assert_not_called()


# Percent-encoded, 2000 CJK characters make an 18 KB GET URL, over the 16 KiB limit,
# while the same query is a 6 KB JSON body for a POST provider.
_LONG_QUERY = "中" * 2000
_TAVILY_HOST = "api.tavily.com"


@pytest.mark.parametrize(
    "first",
    [
        pytest.param(_brave("first"), id="brave"),
        pytest.param(
            create_provider("first", parse_provider({"type": "bing_html"}), ResolvedSearchCredentials({})),
            id="bing_html",
        ),
        pytest.param(_anonymous("first"), id="duckduckgo_html"),
    ],
)
async def test_a_query_too_long_for_a_get_url_falls_through_to_a_post_provider(first):
    first_host = urlsplit(first.endpoint).hostname
    addresses = {first_host: _PUBLIC, _TAVILY_HOST: _PUBLIC}

    def reply(request: httpx.Request) -> httpx.Response:
        assert request.headers["Host"] == _TAVILY_HOST, "an oversized GET reached the socket layer"
        return _ok()

    alone, metadata, network, _ = await _search_on_network(
        WebSearchTools((first,), WebEgressRuntime(), DestinationPolicy()), reply, addresses, _LONG_QUERY
    )
    assert alone == "Error: request_too_large"
    assert metadata["tool_error_code"] == "request_too_large"
    assert metadata["tool_error_retryable"] is False
    # Refused while the request is built: nothing is resolved or sent.
    network.resolve.assert_not_called()
    network.send.assert_not_called()

    result, metadata, network, hosts = await _search_on_network(
        WebSearchTools((first, _tavily("second")), WebEgressRuntime(), DestinationPolicy()),
        reply,
        addresses,
        _LONG_QUERY,
    )
    assert not result.startswith("Error:"), result
    assert metadata["web_search"]["provider"] == "second"
    assert hosts == [_TAVILY_HOST]
    assert [call.args[0] for call in network.resolve.call_args_list] == [_TAVILY_HOST]
    assert json.loads(await network.send.call_args.args[1].aread())["query"] == _LONG_QUERY


@pytest.mark.parametrize(
    "body",
    [
        pytest.param({"type": "search", "query": {"original": "query"}, "mixed": {"type": "mixed", "main": []}}),
        pytest.param({"type": "search", "web": None}),
    ],
    ids=["web-absent", "web-null"],
)
async def test_a_brave_reply_without_web_results_is_an_empty_answer_that_ends_the_chain(body):
    def reply(request: httpx.Request) -> httpx.Response:
        assert request.headers["Host"] == _BRAVE_HOST, "an empty answer passed the search on"
        return _response(200, body=json.dumps(body).encode())

    result, metadata, _, hosts = await _search_on_network(
        WebSearchTools((_brave("first"), _tavily("second")), WebEgressRuntime(), DestinationPolicy()),
        reply,
        {_BRAVE_HOST: _PUBLIC, _TAVILY_HOST: _PUBLIC},
    )
    assert result.startswith('{"query": "query", "provider": "first", "results": [], '), result
    assert metadata["web_search"] == {"provider": "first", "results": []}
    assert hosts == [_BRAVE_HOST]


@pytest.mark.parametrize(
    "body",
    [
        pytest.param({"type": "search", "web": {"type": "search"}}, id="web-without-results"),
        pytest.param({}, id="empty-object"),
        pytest.param({"type": "ErrorResponse", "error": {"code": "OPTION_NOT_IN_PLAN", "status": 200}}, id="error"),
        pytest.param({"type": "summarizer", "status": "complete"}, id="another-response-type"),
        pytest.param([], id="not-an-object"),
    ],
)
async def test_a_brave_reply_that_is_not_a_search_response_advances_as_a_protocol_error(body):
    def reply(request: httpx.Request) -> httpx.Response:
        if request.headers["Host"] == _BRAVE_HOST:
            return _response(200, body=json.dumps(body).encode())
        return _ok()

    addresses = {_BRAVE_HOST: _PUBLIC, _TAVILY_HOST: _PUBLIC}
    alone, metadata, _, _ = await _search_on_network(
        WebSearchTools((_brave("first"),), WebEgressRuntime(), DestinationPolicy()), reply, addresses
    )
    assert alone.startswith("Error: protocol_error"), alone
    assert metadata["tool_error_code"] == "protocol_error"
    result, metadata, _, hosts = await _search_on_network(
        WebSearchTools((_brave("first"), _tavily("second")), WebEgressRuntime(), DestinationPolicy()), reply, addresses
    )
    assert metadata["web_search"]["provider"] == "second", result
    assert hosts == [_BRAVE_HOST, _TAVILY_HOST]


def _exa_mcp(name: str) -> SearchProvider:
    return create_provider(name, parse_provider({"type": "exa_mcp"}), ResolvedSearchCredentials({}))


def _keenable(name: str) -> SearchProvider:
    return create_provider(name, parse_provider({"type": "keenable"}), ResolvedSearchCredentials({}))


@pytest.mark.parametrize("code", sorted(ENVIRONMENT_FAILURES))
@pytest.mark.parametrize("factory", [_tavily, _brave, _anonymous, _exa_mcp, _keenable])
def test_every_adapter_leaves_a_failure_of_its_environment_to_the_next_provider(factory, code):
    assert factory("p").falls_through(WebError(code))


@pytest.mark.parametrize(
    "code,advancing",
    [
        # A keyless service refusing the query is the service's choice; a keyed one names the credential.
        ("http_4xx", {"duckduckgo_html", "exa_mcp", "keenable"}),
        ("exa_rpc_error", {"exa_mcp"}),
        ("exa_search_failed", {"exa_mcp"}),
        # The request itself is wrong: every provider would reject it.
        ("invalid_request", set()),
        ("non_ascii_header", set()),
    ],
)
def test_which_adapter_failures_end_the_chain(code, advancing):
    providers = {
        "tavily": _tavily,
        "brave": _brave,
        "duckduckgo_html": _anonymous,
        "exa_mcp": _exa_mcp,
        "keenable": _keenable,
    }
    assert {name for name, factory in providers.items() if factory(name).falls_through(WebError(code))} == advancing


@pytest.mark.parametrize(
    "allowed,blocked,query",
    [
        ((), (), "rust async"),
        (("docs.rs",), (), "rust async site:docs.rs"),
        (
            ("docs.rs", "rust-lang.org"),
            ("reddit.com",),
            "rust async (site:docs.rs OR site:rust-lang.org) -site:reddit.com",
        ),
        ((), ("a.example", "b.example"), "rust async -site:a.example -site:b.example"),
    ],
)
def test_filters_become_site_operators_for_services_without_native_filtering(allowed, blocked, query):
    request = SearchRequest("rust async", allowed, blocked, 5)
    filtered = site_filtered(request)
    assert filtered.query == query
    assert (filtered.allowed_domains, filtered.blocked_domains, filtered.limit) == (allowed, blocked, 5)
