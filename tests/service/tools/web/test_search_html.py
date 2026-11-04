# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""HTML providers preserve result provenance and reject challenges and unsafe hops."""

from __future__ import annotations

import base64
import json
from unittest.mock import patch

import httpx
import pytest

from chrys.foundation.config.settings import Settings
from chrys.service.tools.builtins.web.build import build_web_tools
from chrys.service.tools.builtins.web.config import parse_provider, parse_search
from chrys.service.tools.builtins.web.http import WebError
from chrys.service.tools.builtins.web.search.html import parse_html, result_url
from tests.service.tools.web._support import Chunks, assemble_agent_tools

_BING = b"""<html><body><ol id="b_results">
<li class="b_algo"><h2><a href="https://docs.python.org/3/library/asyncio.html">Asyncio documentation</a></h2>
<div class="b_caption"><p>Asynchronous I/O reference.</p></div></li>
<li class="b_ad"><h2><a href="https://ad.example/">Advertisement</a></h2></li>
</ol><div id="ai-answer">An invented answer.</div></body></html>"""
_DDG = b"""<html><div class="result"><a class="result__a"
href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fdocs.python.org%2F3%2Flibrary%2Fasyncio.html">Asyncio documentation</a>
<a class="result__snippet">Asynchronous I/O reference.</a></div></html>"""


def config(provider_type):
    return parse_search({"mode": "provider", "provider": "public", "providers": {"public": {"type": provider_type}}})


@pytest.mark.parametrize("provider_type,body", [("bing_html", _BING), ("duckduckgo_html", _DDG)])
def test_organic_results_only_with_decoded_links(provider_type, body):
    hits = parse_html(body, provider_type)
    assert len(hits) == 1
    assert hits[0].url == "https://docs.python.org/3/library/asyncio.html"
    assert hits[0].title == "Asyncio documentation"
    assert hits[0].snippet == "Asynchronous I/O reference."


def test_bing_link_wrapper_and_duplicate_results():
    target = "https://example.com/news?id=42"
    wrapped = "/ck/a?u=a1" + base64.urlsafe_b64encode(target.encode()).decode().rstrip("=")
    assert result_url(wrapped, "bing_html") == target
    duplicate = _BING.replace(
        b"</ol>", _BING[_BING.index(b'<li class="b_algo">') : _BING.index(b'<li class="b_ad">')] + b"</ol>"
    )
    assert len(parse_html(duplicate, "bing_html")) == 1


@pytest.mark.parametrize("target", ["javascript:alert(1)", "file:///etc/passwd", "https://user:pass@example.com/"])
def test_wrapped_unsafe_result_urls_are_rejected(target):
    wrapped = "/ck/a?u=a1" + base64.urlsafe_b64encode(target.encode()).decode().rstrip("=")
    with pytest.raises(ValueError):
        result_url(wrapped, "bing_html")


@pytest.mark.parametrize(
    "provider_type,body,code",
    [
        ("bing_html", b'<div id="b_captcha">Solve this</div>', "search_challenge"),
        ("duckduckgo_html", b'<form id="challenge-form"></form>', "search_challenge"),
        ("duckduckgo_html", b'<form action="/anomaly.js"></form>', "search_challenge"),
        ("bing_html", b"<html>Please log in</html>", "search_parse_failed"),
        ("bing_html", b'<li class="b_algo"><h2><a href="javascript:x">Bad</a></h2></li>', "search_parse_failed"),
    ],
)
def test_challenges_and_unknown_layouts_are_errors(provider_type, body, code):
    with pytest.raises(WebError, match=code):
        parse_html(body, provider_type)


@pytest.mark.parametrize(
    "provider_type,body",
    [
        ("bing_html", b'<ol id="b_results"><li class="b_no">No results found</li></ol>'),
        ("duckduckgo_html", b'<div class="result result--no-result"><div class="no-results">No results</div></div>'),
    ],
)
def test_explicit_empty_results(provider_type, body):
    assert parse_html(body, provider_type) == ()


def test_ddg_ad_blocks_are_excluded():
    ads_and_organic = b"""<html><body>
<div class="result result--ad result--silver"><a class="result__a" href="https://ads.example/buy">Sponsored: Buy now</a>
<a class="result__snippet">Advertisement.</a></div>
<div class="result result--sponsored"><a class="result__a" href="https://more.example/buy">Also sponsored</a></div>
<div class="result"><a class="result__a" href="https://docs.python.org/3/library/asyncio.html">Asyncio documentation</a>
<a class="result__snippet">Asynchronous I/O reference.</a></div>
</body></html>"""
    hits = parse_html(ads_and_organic, "duckduckgo_html")
    assert [hit.url for hit in hits] == ["https://docs.python.org/3/library/asyncio.html"]


def test_ddg_page_with_only_ads_fails_closed():
    ads_only = b"""<html><body>
<div class="result result--ad"><a class="result__a" href="https://ads.example/buy">Sponsored</a></div>
</body></html>"""
    with pytest.raises(WebError, match="search_parse_failed"):
        parse_html(ads_only, "duckduckgo_html")


@pytest.mark.parametrize("provider_type", ["bing_html", "duckduckgo_html"])
async def test_build_and_registry_require_no_credentials(provider_type):
    with patch(
        "chrys.service.tools.builtins.web.build.resolve_search_credentials",
        autospec=True,
        side_effect=AssertionError("secret lookup"),
    ):
        assembled = await assemble_agent_tools(["web_search"], config(provider_type))
        assert assembled.by_pack["web_search"] == ["web_search"]


@pytest.mark.parametrize("provider_type", ["bing_html", "duckduckgo_html"])
@pytest.mark.parametrize("extra", [{"api_key_env": "SECRET"}, {"endpoint": "https://evil.example/search"}])
def test_public_adapters_cannot_receive_credentials_or_arbitrary_endpoints(provider_type, extra):
    with pytest.raises(ValueError, match="do not accept"):
        parse_provider({"type": provider_type, **extra})


async def test_bing_regional_redirect_and_domain_filter():
    search = build_web_tools(["web_search"], Settings(), config("bing_html"), None).search
    seen = []

    def handle(request):
        seen.append(request)
        if len(seen) == 1:
            # Providers without native domain filtering get site: terms in the query.
            assert str(request.url.params["q"]).startswith("Python asyncio")
            assert "site:docs.python.org" in str(request.url.params["q"])
            # A parent-domain cookie is in scope for the regional host, so only the
            # per-hop cookie clear keeps it off the next request.
            return httpx.Response(
                302,
                headers={
                    "Location": "https://cn.bing.com/search?q=Python%20asyncio",
                    "Set-Cookie": "a=secret; Domain=bing.com; Path=/",
                },
            )
        assert str(request.url.params["q"]) == "Python asyncio"
        return httpx.Response(200, headers={"Content-Type": "text/html"}, stream=Chunks(_BING))

    with patch.object(
        search.runtime,
        "client",
        autospec=True,
        side_effect=lambda policy, *, timeout: httpx.AsyncClient(transport=httpx.MockTransport(handle)),
    ):
        result = await search.web_search("Python asyncio", allowed_domains=["docs.python.org"], num_results=3)
    payload = json.loads(result.split("\nExternal", 1)[0])
    assert [request.url.host for request in seen] == ["www.bing.com", "cn.bing.com"]
    assert [request.headers.get("cookie") for request in seen] == [None, None]
    assert not any("authorization" in request.headers for request in seen)
    assert payload["results"][0]["url"] == "https://docs.python.org/3/library/asyncio.html"


@pytest.mark.parametrize(
    "location",
    [
        "https://evil.example/search",
        "https://127.0.0.1/search",
        "http://cn.bing.com/search",
        "https://cn.bing.com:8443/search",
        "https://cn.bing.com/login",
    ],
)
async def test_redirect_cannot_leave_fixed_search_endpoints(location):
    search = build_web_tools(["web_search"], Settings(), config("bing_html"), None).search
    seen = []

    def handle(request):
        seen.append(request)
        return httpx.Response(302, headers={"Location": location})

    with patch.object(
        search.runtime,
        "client",
        autospec=True,
        side_effect=lambda policy, *, timeout: httpx.AsyncClient(transport=httpx.MockTransport(handle)),
    ):
        result = await search.web_search("query")
    assert result == "Error: redirect_not_allowed"
    assert len(seen) == 1


async def test_http_202_is_a_challenge_not_an_empty_success():
    search = build_web_tools(["web_search"], Settings(), config("duckduckgo_html"), None).search
    with patch.object(
        search.runtime,
        "client",
        autospec=True,
        side_effect=lambda policy, *, timeout: httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: httpx.Response(202))
        ),
    ):
        assert await search.web_search("query") == "Error: search_challenge"
