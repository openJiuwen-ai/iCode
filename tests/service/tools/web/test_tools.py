# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Offline protocol, budget, authorization and profile integration contracts."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from unittest.mock import patch

import httpx
import pytest

from chrys.foundation.config.settings import Settings
from chrys.foundation.tool_kinds import get_tool_kind
from chrys.foundation.tool_result_metadata import WEB_FETCH_FINAL_URL_METADATA_KEY
from chrys.service.agent_middleware.events.result_persistence import persistable_result_metadata
from chrys.service.approval.policy import ApprovalPolicy
from chrys.service.profiles.agents.loader import AgentProfileLoadError, load_profile_from_yaml
from chrys.service.profiles.agents.schema import ApprovalConfig
from chrys.service.profiles.agents.serializer import profile_to_dict
from chrys.service.tools.builtins.web.build import build_web_tools, create_provider
from chrys.service.tools.builtins.web.config import (
    WebFetchConfigPatch,
    parse_provider,
    parse_search,
)
from chrys.service.tools.builtins.web.credentials import ResolvedSearchCredentials
from chrys.service.tools.builtins.web.fetch.tool import WebFetchTools
from chrys.service.tools.builtins.web.http import DestinationPolicy, WebEgressRuntime
from chrys.service.tools.builtins.web.search.tool import WebSearchTools
from chrys.service.tools.registry import ToolRegistry
from chrys.service.tools.result_metadata import tool_result_metadata
from tests.service.tools.web._support import Chunks, assemble
from tests.support.waiting import wait_for


def response(status: int, text: str, mime: str = "application/json", **headers: str) -> httpx.Response:
    return httpx.Response(status, headers={"Content-Type": mime, **headers}, stream=Chunks(text.encode()))


@pytest.mark.parametrize("provider_type", ["tavily", "brave", "exa"])
async def test_official_provider_contract_and_filter(provider_type):
    seen = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        field = {"tavily": "content", "brave": "description", "exa": "highlights"}[provider_type]
        items = [
            {
                "title": "[Title]",
                "url": "https://example.com/docs",
                field: ["snippet"] if provider_type == "exa" else "snippet",
            },
            {"title": "Fake suffix", "url": "https://notexample.com/", field: [] if provider_type == "exa" else ""},
        ]
        data = {"web": {"results": items}} if provider_type == "brave" else {"results": items}
        return response(200, json.dumps(data))

    runtime = WebEgressRuntime()
    provider = create_provider(
        "main",
        parse_provider({"type": provider_type, "api_key_env": "SEARCH_KEY"}),
        ResolvedSearchCredentials({"SEARCH_KEY": "secret"}),
    )
    tools = WebSearchTools((provider,), runtime, DestinationPolicy(), num_results=3)
    with patch.object(
        runtime,
        "client",
        autospec=True,
        side_effect=lambda policy, *, timeout: httpx.AsyncClient(transport=httpx.MockTransport(handle)),
    ):
        result = await tools.web_search('中 "query"\n\\', allowed_domains=["EXAMPLE.com."])
    data = json.loads(result.split("\nExternal", 1)[0])
    assert len(data["results"]) == 1
    assert data["provider"] == "main"
    request = seen[0]
    assert request.headers["Accept-Encoding"] == "identity"
    if provider_type == "brave":
        assert request.method == "GET"
        assert request.url.params["count"] == "3"
        assert request.headers["X-Subscription-Token"] == "secret"
    else:
        assert request.method == "POST"
        body = json.loads(request.content)
        assert body["query"] == '中 "query"\n\\'
        assert body["max_results" if provider_type == "tavily" else "numResults"] == 3
        assert request.headers["Authorization" if provider_type == "tavily" else "x-api-key"] == (
            "Bearer secret" if provider_type == "tavily" else "secret"
        )
        if provider_type == "tavily":
            assert body["include_domains"] == ["example.com"]
            assert body["include_domains_mode"] == "restrict"


@pytest.mark.parametrize("provider_type", ["tavily", "brave", "exa"])
async def test_non_ascii_credential_fails_closed_without_leaking(provider_type):
    """httpx ascii-encodes headers; the UnicodeEncodeError repr carries the full value."""

    def handle(request: httpx.Request) -> httpx.Response:
        raise AssertionError("request must not be sent")

    runtime = WebEgressRuntime()
    provider = create_provider(
        "main",
        parse_provider({"type": provider_type, "api_key_env": "SEARCH_KEY"}),
        ResolvedSearchCredentials({"SEARCH_KEY": "tvly-sécret-123"}),
    )
    tools = WebSearchTools((provider,), runtime, DestinationPolicy())
    with patch.object(
        runtime,
        "client",
        autospec=True,
        side_effect=lambda policy, *, timeout: httpx.AsyncClient(transport=httpx.MockTransport(handle)),
    ):
        result = await tools.web_search("query")
    assert result.startswith("Error: non_ascii_header")
    assert "tvly" not in result and "123" not in result


async def test_null_title_or_snippet_degrades_to_defaults():
    def handle(request: httpx.Request) -> httpx.Response:
        items = [
            {"title": None, "url": "https://example.com/a", "content": None},
            {"title": "Real", "url": "https://example.com/b", "content": "text"},
        ]
        return response(200, json.dumps({"results": items}))

    runtime = WebEgressRuntime()
    provider = create_provider(
        "main", parse_provider({"type": "tavily", "api_key_env": "KEY"}), ResolvedSearchCredentials({"KEY": "secret"})
    )
    tools = WebSearchTools((provider,), runtime, DestinationPolicy())
    with patch.object(
        runtime,
        "client",
        autospec=True,
        side_effect=lambda policy, *, timeout: httpx.AsyncClient(transport=httpx.MockTransport(handle)),
    ):
        result = await tools.web_search("query")
    data = json.loads(result.split("\nExternal", 1)[0])
    assert len(data["results"]) == 2
    assert data["results"][0]["title"] == "https://example.com/a"
    assert data["results"][0]["snippet"] == ""


@pytest.mark.parametrize("provider_type", ["brave", "duckduckgo_html"])
async def test_domain_filters_pushed_to_providers_without_native_support(provider_type):
    seen = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if provider_type == "brave":
            items = [
                {"title": "Keep", "url": "https://example.com/a", "description": "d"},
                {"title": "Drop", "url": "https://other.example.org/b", "description": "d"},
            ]
            return response(200, json.dumps({"web": {"results": items}}))
        body = (
            '<html><div class="result"><a class="result__a" href="https://example.com/a">Keep</a>'
            '<a class="result__snippet">d</a></div>'
            '<div class="result"><a class="result__a" href="https://other.example.org/b">Drop</a></div></html>'
        )
        return response(200, body, mime="text/html")

    runtime = WebEgressRuntime()
    if provider_type == "brave":
        provider = create_provider(
            "public",
            parse_provider({"type": "brave", "api_key_env": "KEY"}),
            ResolvedSearchCredentials({"KEY": "secret"}),
        )
    else:
        provider = create_provider("public", parse_provider({"type": provider_type}), ResolvedSearchCredentials({}))
    tools = WebSearchTools((provider,), runtime, DestinationPolicy())
    with patch.object(
        runtime,
        "client",
        autospec=True,
        side_effect=lambda policy, *, timeout: httpx.AsyncClient(transport=httpx.MockTransport(handle)),
    ):
        result = await tools.web_search("query", allowed_domains=["example.com"])
    data = json.loads(result.split("\nExternal", 1)[0])
    assert data["query"] == "query"
    assert data["filtered_results"] == 1
    assert [item["url"] for item in data["results"]] == ["https://example.com/a"]
    sent_query = str(seen[0].url.params["q"])
    assert sent_query.startswith("query")
    assert "site:example.com" in sent_query


async def test_keenable_keyless_contract_and_filter():
    seen = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        items = [
            {"title": "Keep", "url": "https://example.com/a", "description": "", "snippet": "page text"},
            {"title": "Drop", "url": "https://other.example.org/b", "description": "", "snippet": "page text"},
        ]
        return response(200, json.dumps({"results": items}))

    runtime = WebEgressRuntime()
    provider = create_provider("free", parse_provider({"type": "keenable"}), ResolvedSearchCredentials({}))
    tools = WebSearchTools((provider,), runtime, DestinationPolicy(), num_results=3)
    with patch.object(
        runtime,
        "client",
        autospec=True,
        side_effect=lambda policy, *, timeout: httpx.AsyncClient(transport=httpx.MockTransport(handle)),
    ):
        result = await tools.web_search('中 "query"', allowed_domains=["example.com"])
    data = json.loads(result.split("\nExternal", 1)[0])
    assert data["provider"] == "free"
    assert data["filtered_results"] == 1
    assert data["results"] == [{"title": "Keep", "url": "https://example.com/a", "snippet": "page text"}]
    request = seen[0]
    assert request.method == "POST"
    assert str(request.url) == "https://api.keenable.ai/v1/search/public"
    assert request.headers["X-Keenable-Title"] == "iCode"
    assert {"authorization", "x-api-key"}.isdisjoint(key.lower() for key in request.headers)
    assert json.loads(request.content) == {"query": '中 "query" site:example.com', "max_results": 3}


async def test_custom_mapping_and_authorization_before_credentials():
    config = parse_search(
        {
            "mode": "provider",
            "provider": "corp",
            "providers": {
                "corp": {
                    "type": "custom_http",
                    "endpoint": "https://search.example.com/search",
                    "method": "POST",
                    "auth": {"location": "header", "name": "Authorization", "key_env": "CORP", "prefix": "Bearer "},
                    "request": {
                        "json": {"q": {"$input": "query"}, "n": {"$input": "limit"}, "secret": {"$env": "EXTRA"}}
                    },
                    "response": {
                        "results_pointer": "/data/items",
                        "url_pointer": "/link",
                        "snippet_pointer": "/summary",
                    },
                }
            },
        }
    )
    with patch("chrys.service.tools.builtins.web.build.resolve_search_credentials", autospec=True) as resolve:
        with pytest.raises(ValueError, match=r"not granted in user setting tools\.web_search\.custom_endpoints"):
            build_web_tools(["web_search"], Settings(), config, None)
        resolve.assert_not_called()
    settings = replace(
        Settings(),
        web_search_custom_endpoints=json.dumps(
            [{"url": "https://search.example.com/search", "credential_env_names": ["CORP", "EXTRA"]}]
        ),
    )
    with patch(
        "chrys.service.tools.builtins.web.build.resolve_search_credentials",
        autospec=True,
        return_value=ResolvedSearchCredentials({"CORP": "secret-value-unique", "EXTRA": "value"}),
    ):
        built = build_web_tools(["web_search"], settings, config, None)
    assert built.search is not None
    requests = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return response(200, '{"data":{"items":[{"link":"https://example.com/","summary":"ok"}]}}')

    with patch.object(
        built.search.runtime,
        "client",
        autospec=True,
        side_effect=lambda policy, *, timeout: httpx.AsyncClient(transport=httpx.MockTransport(handle)),
    ):
        result = await built.search.web_search('"\n中文\\')
    assert not result.startswith("Error:")
    assert json.loads(requests[0].content) == {"q": '"\n中文\\', "n": 8, "secret": "value"}
    assert requests[0].headers["Authorization"] == "Bearer secret-value-unique"
    assert "secret-value-unique" not in repr(built.search.providers[0])


@pytest.mark.parametrize("status,count,success", [(429, 4, False), (500, 4, False), (401, 1, False), (200, 1, True)])
async def test_search_fallback_attempt_bound(status, count, success):
    calls = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request.url)
        return response(status, '{"results":[]}')

    providers = tuple(
        create_provider(
            name, parse_provider({"type": "tavily", "api_key_env": "KEY"}), ResolvedSearchCredentials({"KEY": "secret"})
        )
        for name in ("first", "second")
    )
    runtime = WebEgressRuntime()
    tools = WebSearchTools(providers, runtime, DestinationPolicy())
    with patch.object(
        runtime,
        "client",
        autospec=True,
        side_effect=lambda policy, *, timeout: httpx.AsyncClient(transport=httpx.MockTransport(handle)),
    ):
        result = await tools.web_search("query")
    assert len(calls) == count
    assert result.startswith("Error:") is not success


@pytest.mark.parametrize(
    "arguments",
    [
        {"query": " "},
        {"query": "q", "num_results": True},
        {"query": "q", "num_results": 21},
        {"query": "q", "allowed_domains": ["https://example.com"]},
        {"query": "q", "allowed_domains": ["example.com"], "blocked_domains": ["example.net"]},
    ],
)
async def test_invalid_search_never_opens_client(arguments):
    runtime = WebEgressRuntime()
    tools = WebSearchTools((), runtime, DestinationPolicy())
    with patch.object(runtime, "client", autospec=True) as client:
        assert (await tools.web_search(**arguments)).startswith("Error:")
        client.assert_not_called()


async def test_fetch_conversion_cache_and_single_flight():
    calls = []

    async def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request.url)
        return response(
            200,
            "<title>[Title]</title><script>SECRET_SCRIPT</script><h1>Heading</h1><table><tr><th>A</th></tr><tr><td>B</td></tr></table><ul><li>One<ul><li>Two</li></ul></li></ul>",
            "text/html; charset=utf-8",
        )

    runtime = WebEgressRuntime()
    fetch = WebFetchTools(runtime, DestinationPolicy())
    with patch.object(
        runtime,
        "client",
        autospec=True,
        side_effect=lambda policy, *, timeout: httpx.AsyncClient(transport=httpx.MockTransport(handle)),
    ):
        results = await asyncio.gather(
            fetch.web_fetch("https://example.com/#a", "Read"), fetch.web_fetch("https://EXAMPLE.com:443/#b", "Read")
        )
    assert len(calls) == 1
    assert all("SECRET_SCRIPT" not in item and "# Heading" in item and "Two" in item for item in results)
    assert "cache" in results[1]
    assert not fetch.cache.locks


@pytest.mark.parametrize(
    "status,mime,expected",
    [(404, "text/html", "http_4xx (HTTP 404)"), (200, "application/pdf", "unsupported_content_type")],
)
async def test_fetch_rejects_without_body_disclosure(status, mime, expected):
    runtime = WebEgressRuntime()
    fetch = WebFetchTools(runtime, DestinationPolicy())
    with patch.object(
        runtime,
        "client",
        autospec=True,
        side_effect=lambda policy, *, timeout: httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: response(status, "REMOTE_SECRET", mime))
        ),
    ):
        result = await fetch.web_fetch("https://example.com", "Read")
    assert result == "Error: " + expected
    assert not fetch.cache.entries


async def _fetch_with_metadata(fetch: WebFetchTools, **arguments) -> tuple[str, dict]:
    metadata: dict = {}
    token = tool_result_metadata.set(metadata)
    try:
        return await fetch.web_fetch(**arguments), metadata
    finally:
        tool_result_metadata.reset(token)


@pytest.mark.parametrize(
    ("arguments", "code"),
    [
        pytest.param({"prompt": ""}, "invalid_input", id="empty-prompt"),
        pytest.param({"prompt": " \n\t "}, "invalid_input", id="blank-prompt"),
        pytest.param({"prompt": "x" * 2001}, "invalid_input", id="prompt-over-2000"),
        pytest.param({"prompt": None}, "invalid_input", id="prompt-not-text"),
        pytest.param({"prompt": "Read", "max_tokens": -1}, "invalid_input", id="negative-budget"),
        pytest.param({"prompt": "Read", "max_tokens": 1001}, "invalid_input", id="budget-over-configured-maximum"),
        pytest.param({"prompt": "Read", "max_tokens": True}, "invalid_input", id="boolean-budget"),
        pytest.param({"prompt": "Read", "max_tokens": 10.0}, "invalid_input", id="fractional-budget"),
        pytest.param({"prompt": "Read", "url": "ftp://example.com/"}, "invalid_url", id="unsupported-scheme"),
        pytest.param(
            {"prompt": "Read", "url": "https://user:pw@example.com/"}, "invalid_url", id="embedded-credentials"
        ),
    ],
)
async def test_invalid_fetch_input_never_opens_client(arguments, code):
    runtime = WebEgressRuntime()
    fetch = WebFetchTools(runtime, DestinationPolicy(), max_tokens=1000)
    with patch.object(runtime, "client", autospec=True) as client:
        result, metadata = await _fetch_with_metadata(fetch, **{"url": "https://example.com/", **arguments})
    assert result.startswith("Error:")
    assert metadata["tool_error_code"] == code
    assert metadata["tool_error_retryable"] is False
    client.assert_not_called()


@pytest.mark.parametrize(
    "arguments",
    [
        pytest.param({"prompt": "  " + "x" * 2000 + "  "}, id="prompt-of-2000-after-trimming"),
        pytest.param({"prompt": "Read", "max_tokens": 1000}, id="budget-at-configured-maximum"),
        pytest.param({"prompt": "Read", "max_tokens": 0}, id="zero-uses-configured-maximum"),
    ],
)
async def test_fetch_input_boundaries_are_accepted(arguments):
    runtime = WebEgressRuntime()
    fetch = WebFetchTools(runtime, DestinationPolicy(), max_tokens=1000)
    with patch.object(
        runtime,
        "client",
        autospec=True,
        side_effect=lambda policy, *, timeout: httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: response(200, "page body", "text/plain"))
        ),
    ):
        result, metadata = await _fetch_with_metadata(fetch, url="https://example.com/", **arguments)
    assert "page body" in result, result
    assert metadata["web_fetch_truncated"] is False


async def test_session_history_keeps_only_the_url_the_fetch_card_shows():
    runtime = WebEgressRuntime()
    fetch = WebFetchTools(runtime, DestinationPolicy(), max_tokens=1000)
    with patch.object(
        runtime,
        "client",
        autospec=True,
        side_effect=lambda policy, *, timeout: httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: response(200, "page body", "text/plain"))
        ),
    ):
        _, metadata = await _fetch_with_metadata(fetch, url="https://example.com/page", prompt="Read")
    assert metadata["web_fetch_status"] == 200
    assert persistable_result_metadata(metadata) == {WEB_FETCH_FINAL_URL_METADATA_KEY: "https://example.com/page"}


async def test_a_budget_too_small_for_the_envelope_is_an_error_and_the_page_stays_cached():
    from chrys.foundation.text.tokenizer import MixedLanguageTokenizer

    calls = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return response(200, "word " * 2000, "text/plain")

    runtime = WebEgressRuntime()
    fetch = WebFetchTools(runtime, DestinationPolicy())
    with patch.object(
        runtime,
        "client",
        autospec=True,
        side_effect=lambda policy, *, timeout: httpx.AsyncClient(transport=httpx.MockTransport(handle)),
    ):
        # The header, focus line, truncation note and footer alone exceed ten tokens.
        result, metadata = await _fetch_with_metadata(fetch, url="https://example.com/", prompt="Read", max_tokens=10)
        assert result == "Error: budget_too_small"
        assert metadata["tool_error_code"] == "budget_too_small"
        assert metadata["tool_error_retryable"] is False
        # A retry with a workable budget is served from the page fetched a moment ago.
        retried, metadata = await _fetch_with_metadata(fetch, url="https://example.com/", prompt="Read", max_tokens=200)
    assert len(calls) == 1
    assert metadata["web_fetch_cache_hit"] is True
    assert metadata["web_fetch_truncated"] is True
    assert MixedLanguageTokenizer().count_tokens(retried) <= 200


async def test_fetch_cross_site_redirect_no_second_request():
    runtime = WebEgressRuntime()
    fetch = WebFetchTools(runtime, DestinationPolicy())
    calls = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return response(302, "", "text/html", location="http://127.0.0.1/admin")

    metadata = {}
    token = tool_result_metadata.set(metadata)
    try:
        with patch.object(
            runtime,
            "client",
            autospec=True,
            side_effect=lambda policy, *, timeout: httpx.AsyncClient(transport=httpx.MockTransport(handle)),
        ):
            result = await fetch.web_fetch("https://example.com", "Read")
    finally:
        tool_result_metadata.reset(token)
    assert "Redirect detected" in result
    assert len(calls) == 1
    assert metadata["web_fetch_redirect_to"] == "http://127.0.0.1/admin"


def test_profile_round_trip_and_registry_gate(tmp_path):
    import yaml

    path = tmp_path / "profile.yaml"
    path.write_text(
        'name: test\ntools:\n  builtins: [web_search, web_fetch]\n  web_search:\n    mode: "off"\n    fallback_chain: []\n    providers:\n      first:\n        type: tavily\n        api_key_env: KEY\n  web_fetch:\n    mode: "on"\n    max_tokens: 1234\n',
        encoding="utf-8",
    )
    profile = load_profile_from_yaml(path)
    serialized = profile_to_dict(profile)
    assert serialized["tools"]["web_search"]["fallback_chain"] == []
    path.write_text(yaml.safe_dump(serialized), encoding="utf-8")
    assert load_profile_from_yaml(path) == profile
    registry = ToolRegistry()
    tools = registry.load_builtins(
        profile.tools.builtins,
        settings=Settings(),
        web=assemble(profile.tools.builtins, profile.tools.web_search, profile.tools.web_fetch),
    )
    assert [tool.name for tool in tools] == ["web_fetch", "sleep"]
    assert tools[0].kind is None and get_tool_kind(tools[0]) == "web_fetch"
    unwired = ToolRegistry()
    unwired.load_builtins(["web_fetch"])
    assert unwired.get_by_category("web_fetch") == []


@pytest.mark.parametrize(
    "fragment",
    [
        "web_fetch: {private_origins: []}",
        "web_search: {num_results: true}",
    ],
)
def test_profile_rejects_bad_web_fields(tmp_path, fragment):
    path = tmp_path / "profile.yaml"
    path.write_text("name: test\ntools:\n  " + fragment + "\n", encoding="utf-8")
    with pytest.raises(AgentProfileLoadError):
        load_profile_from_yaml(path)


def test_profile_null_web_section_means_absent(tmp_path):
    """A null web section (YAML comment leftovers) loads as unconfigured."""
    path = tmp_path / "profile.yaml"
    path.write_text("name: test\ntools:\n  web_fetch: null\n", encoding="utf-8")
    assert load_profile_from_yaml(path).tools.web_fetch is None


def test_approval_kinds_are_independent():
    policy = ApprovalPolicy(ApprovalConfig(default="skip", overrides={"search": "skip", "web_search": "skip"}))
    assert not policy.should_require_approval("web_search", "web_search")
    assert policy.should_require_approval("web_fetch", "web_fetch")


def test_fetch_profile_limit_reaches_build_and_shared_gate():
    built = build_web_tools(
        ["web_search", "web_fetch"],
        Settings(),
        None,
        WebFetchConfigPatch(max_tokens=1234, timeout_seconds=7),
    )
    assert built.search is not None
    assert built.fetch is not None
    assert built.fetch.max_tokens == 1234
    assert built.fetch.timeout_seconds == 7
    # One build shares one egress runtime, so both tools queue on the same concurrency gate.
    assert built.search.runtime is built.fetch.runtime
    assert built.search.runtime.gate is built.fetch.runtime.gate
    # Separate builds never share a gate.
    other = build_web_tools(["web_fetch"], Settings(), None, None)
    assert other.fetch is not None
    assert other.fetch.runtime.gate is not built.fetch.runtime.gate


async def test_cancelled_fetch_waiter_leaves_no_task_or_lock():
    runtime = WebEgressRuntime()
    fetch = WebFetchTools(runtime, DestinationPolicy())
    entered = asyncio.Event()
    released = asyncio.Event()
    key = "https://example.com/"

    def flight_users() -> int:
        return fetch.cache.locks.get(key, (None, 0))[1]

    async def handle(request: httpx.Request) -> httpx.Response:
        entered.set()
        await released.wait()
        return response(200, "page", "text/plain")

    with patch.object(
        runtime,
        "client",
        autospec=True,
        side_effect=lambda policy, *, timeout: httpx.AsyncClient(transport=httpx.MockTransport(handle)),
    ):
        owner = asyncio.create_task(fetch.web_fetch("https://example.com", "Read"))
        waiter = None
        try:
            await wait_for(
                lambda: entered.is_set() or owner.done(), description="the owner fetch reached the transport"
            )
            # A finished owner never reached the transport: surface its outcome.
            assert entered.is_set(), await owner
            assert flight_users() == 1
            waiter = asyncio.create_task(fetch.web_fetch("https://example.com", "Read"))
            # The waiter joins the owner's flight and blocks on its lock before it is cancelled.
            await wait_for(
                lambda: flight_users() == 2 or waiter.done(), description="the waiter queued on the flight lock"
            )
            assert not waiter.done(), await waiter
            waiter.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiter
            # The cancelled waiter gave back its share without disturbing the owner's.
            assert flight_users() == 1
            assert not owner.done()
            released.set()
            assert "page" in await owner
        finally:
            released.set()
            for task in (owner, waiter):
                if task is not None and not task.done():
                    task.cancel()
            await asyncio.gather(*(task for task in (owner, waiter) if task is not None), return_exceptions=True)
    assert not fetch.cache.locks
    assert fetch.cache.get(key) is not None


async def test_search_pass_budget_and_queue_timeout():
    runtime = WebEgressRuntime()
    tools = WebSearchTools((), runtime, DestinationPolicy(), timeout_seconds=0.01)
    tools._standalone_budget[0] = 20
    assert "call limit" in await tools.web_search("query")
    tools.reset_budget()
    for _ in range(3):
        await runtime.gate.acquire()
    metadata = {}
    token = tool_result_metadata.set(metadata)
    try:
        assert "deadline" in await tools.web_search("query")
        assert metadata["tool_error_code"] == "queue_timeout"
    finally:
        tool_result_metadata.reset(token)
        for _ in range(3):
            runtime.gate.release()


async def test_output_budget_stays_json_and_fetch_envelope_fits():
    from chrys.foundation.text.tokenizer import MixedLanguageTokenizer

    runtime = WebEgressRuntime()
    provider = create_provider(
        "main", parse_provider({"type": "tavily", "api_key_env": "KEY"}), ResolvedSearchCredentials({"KEY": "secret"})
    )
    search = WebSearchTools((provider,), runtime, DestinationPolicy())
    fetch = WebFetchTools(runtime, DestinationPolicy(), max_tokens=600)
    items = [{"title": "T" * 10000, "url": "https://example.com/", "content": "中" * 30000} for _ in range(8)]
    tokenizer = MixedLanguageTokenizer()
    with patch.object(
        runtime,
        "client",
        autospec=True,
        side_effect=lambda policy, *, timeout: httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: response(200, json.dumps({"results": items})))
        ),
    ):
        text = await search.web_search("query")
    assert json.loads(text.split("\nExternal", 1)[0])["truncated"] is True
    assert tokenizer.count_tokens(text) <= 8000
    with patch.object(
        runtime,
        "client",
        autospec=True,
        side_effect=lambda policy, *, timeout: httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: response(200, "中" * 10000, "text/plain"))
        ),
    ):
        text = await fetch.web_fetch("https://example.com/", "Read")
    assert "Truncated:" in text
    assert tokenizer.count_tokens(text) <= 600


async def test_http_logs_do_not_expose_query_credentials_and_scope_resets(caplog):
    import logging

    runtime = WebEgressRuntime()
    transport = httpx.MockTransport(lambda request: response(200, "{}"))
    caplog.set_level(logging.INFO, logger="httpx")
    with patch("chrys.service.tools.builtins.web.http.ValidatedAsyncTransport", autospec=True, return_value=transport):
        async with runtime.client(DestinationPolicy(), timeout=5) as client:
            await client.get("https://example.com/?api_key=secret-query-value")
    logging.getLogger("httpx").info("outside-web-scope")
    assert "secret-query-value" not in caplog.text
    assert "outside-web-scope" in caplog.text
