# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""custom_http search providers: the searxng, serpapi and serply presets, and where credentials may go.

Preset wire checks build the provider the way an agent build does, through
``build_web_tools`` and the user's custom endpoint grant, and answer beneath the
validated transport. The rejection table walks each branch of the strict
mapping parser that decides where a credential may travel.
"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any
from unittest.mock import patch

import httpx
import pytest

from chrys.foundation.config.settings import Settings
from chrys.service.tools.builtins.web.build import build_web_tools
from chrys.service.tools.builtins.web.config import CUSTOM_PRESETS, parse_provider, parse_search
from chrys.service.tools.builtins.web.credentials import ResolvedSearchCredentials
from chrys.service.tools.builtins.web.search.tool import WebSearchTools
from tests.service.tools.web._support import Chunks, socket_network

_PUBLIC = "93.184.215.14"
_ADDRESSES = {"serpapi.com": _PUBLIC, "api.serply.io": _PUBLIC, "searx.example": _PUBLIC}
_RESOLVE = "chrys.service.tools.builtins.web.build.resolve_search_credentials"


# ---------------------------------------------------------------- presets, parsed


def test_the_searxng_preset_supplies_everything_but_the_endpoint():
    config = parse_provider({"type": "custom_http", "preset": "searxng", "endpoint": "https://searx.example/search"})
    assert config.preset == "searxng"
    assert config.endpoint == "https://searx.example/search"
    assert config.method == "GET"
    assert config.auth == {"location": "none"}
    assert config.request == {"query_params": {"q": {"$input": "query"}, "format": "json"}}
    assert config.response == {
        "results_pointer": "/results",
        "url_pointer": "/url",
        "title_pointer": "/title",
        "snippet_pointer": "/content",
    }
    assert config.credential_names() == set()


def test_the_searxng_preset_has_no_endpoint_of_its_own():
    # A SearXNG instance is always self-hosted, so the profile must name it.
    with pytest.raises(ValueError, match="needs an endpoint URL"):
        parse_provider({"type": "custom_http", "preset": "searxng"})


def test_the_serpapi_preset_carries_its_endpoint_and_query_credential():
    config = parse_provider({"type": "custom_http", "preset": "serpapi"})
    assert config.preset == "serpapi"
    assert config.endpoint == "https://serpapi.com/search"
    assert config.method == "GET"
    assert config.auth == {"location": "query", "name": "api_key", "key_env": "SERPAPI_API_KEY"}
    assert config.request == {"query_params": {"engine": "google", "q": {"$input": "query"}}}
    assert config.response == {
        "results_pointer": "/organic_results",
        "url_pointer": "/link",
        "title_pointer": "/title",
        "snippet_pointer": "/snippet",
    }
    assert config.credential_names() == {"SERPAPI_API_KEY"}


def test_the_serply_preset_carries_its_endpoint_and_header_credential():
    config = parse_provider({"type": "custom_http", "preset": "serply"})
    assert config.preset == "serply"
    assert config.endpoint == "https://api.serply.io/v1/search"
    assert config.method == "GET"
    assert config.auth == {"location": "header", "name": "X-Api-Key", "key_env": "SERPLY_API_KEY"}
    assert config.request == {"query_params": {"q": {"$input": "query"}, "num": {"$input": "limit"}}}
    assert config.response == {
        "results_pointer": "/results",
        "url_pointer": "/link",
        "title_pointer": "/title",
        "snippet_pointer": "/description",
    }
    assert config.credential_names() == {"SERPLY_API_KEY"}


def test_a_profile_field_replaces_the_presets_field():
    auth = {"location": "query", "name": "api_key", "key_env": "TEAM_SERPAPI_KEY"}
    config = parse_provider({"type": "custom_http", "preset": "serpapi", "auth": auth})
    assert config.auth == auth
    assert config.credential_names() == {"TEAM_SERPAPI_KEY"}
    # The rest of the preset is untouched, and the preset itself is never mutated.
    assert config.endpoint == "https://serpapi.com/search"
    assert CUSTOM_PRESETS["serpapi"]["auth"]["key_env"] == "SERPAPI_API_KEY"


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        pytest.param({"type": "custom_http", "preset": "google"}, "Unknown custom provider preset", id="unknown"),
        pytest.param({"type": "custom_http", "preset": 1}, "Unknown custom provider preset", id="not-text"),
        pytest.param(
            {"type": "tavily", "api_key_env": "KEY", "preset": "serpapi"},
            "Official adapters do not accept endpoint overrides",
            id="on-an-official-adapter",
        ),
        pytest.param(
            {"type": "bing_html", "preset": "searxng"},
            "Anonymous search adapters do not accept",
            id="on-an-anonymous-adapter",
        ),
    ],
)
def test_a_preset_is_only_a_known_custom_http_shorthand(raw, message):
    with pytest.raises(ValueError, match=message):
        parse_provider(raw)


# ------------------------------------------------------------ presets, on the wire


def _search_tools(provider: dict[str, Any], grant: dict[str, Any], credentials: dict[str, str]) -> WebSearchTools:
    config = parse_search({"mode": "provider", "provider": "custom", "providers": {"custom": provider}})
    settings = replace(Settings(), web_search_custom_endpoints=json.dumps([grant]))
    with patch(_RESOLVE, autospec=True, return_value=ResolvedSearchCredentials(credentials)) as resolve:
        built = build_web_tools(["web_search"], settings, config, None)
    names = set(credentials)
    if names:
        resolve.assert_called_once_with(names)
    else:
        resolve.assert_not_called()
    assert built.search is not None
    return built.search


def _json(body: dict[str, Any]) -> httpx.Response:
    return httpx.Response(200, headers={"Content-Type": "application/json"}, stream=Chunks(json.dumps(body).encode()))


async def _search(tools: WebSearchTools, body: dict[str, Any]) -> tuple[dict[str, Any], httpx.Request]:
    async def handle(request: httpx.Request) -> httpx.Response:
        return _json(body)

    with socket_network(handle, _ADDRESSES) as network:
        result = await tools.web_search("chrys agents")
    assert not result.startswith("Error:"), result
    assert network.send.call_count == 1
    return json.loads(result.split("\nExternal", 1)[0]), network.send.call_args.args[1]


async def test_a_serpapi_search_puts_the_key_in_the_query_and_reads_organic_results():
    tools = _search_tools(
        {"type": "custom_http", "preset": "serpapi"},
        {"url": "https://serpapi.com/search", "credential_env_names": ["SERPAPI_API_KEY"]},
        {"SERPAPI_API_KEY": "serp-secret-value"},
    )
    body = {"organic_results": [{"title": "Chrys", "link": "https://example.com/chrys", "snippet": "Agents."}]}
    data, request = await _search(tools, body)
    assert request.method == "GET"
    assert request.headers["Host"] == "serpapi.com"
    assert request.url.path == "/search"
    assert dict(request.url.params) == {"engine": "google", "q": "chrys agents", "api_key": "serp-secret-value"}
    assert "authorization" not in request.headers
    assert data["results"] == [{"title": "Chrys", "url": "https://example.com/chrys", "snippet": "Agents."}]
    assert "serp-secret-value" not in json.dumps(data)


async def test_a_serpapi_key_env_override_reads_that_variable():
    tools = _search_tools(
        {
            "type": "custom_http",
            "preset": "serpapi",
            "auth": {"location": "query", "name": "api_key", "key_env": "TEAM_SERPAPI_KEY"},
        },
        {"url": "https://serpapi.com/search", "credential_env_names": ["TEAM_SERPAPI_KEY"]},
        {"TEAM_SERPAPI_KEY": "team-secret"},
    )
    _, request = await _search(tools, {"organic_results": []})
    assert request.url.params["api_key"] == "team-secret"


async def test_a_serply_search_puts_the_key_in_a_header_and_reads_results():
    tools = _search_tools(
        {"type": "custom_http", "preset": "serply"},
        {"url": "https://api.serply.io/v1/search", "credential_env_names": ["SERPLY_API_KEY"]},
        {"SERPLY_API_KEY": "serply-secret-value"},
    )
    body = {"results": [{"title": "Chrys", "link": "https://example.com/chrys", "description": "Agents."}]}
    data, request = await _search(tools, body)
    assert request.method == "GET"
    assert request.headers["Host"] == "api.serply.io"
    assert request.url.path == "/v1/search"
    assert dict(request.url.params) == {"q": "chrys agents", "num": "8"}
    assert request.headers["X-Api-Key"] == "serply-secret-value"
    assert data["results"] == [{"title": "Chrys", "url": "https://example.com/chrys", "snippet": "Agents."}]
    assert "serply-secret-value" not in json.dumps(data)


async def test_a_searxng_search_asks_for_json_without_credentials():
    tools = _search_tools(
        {"type": "custom_http", "preset": "searxng", "endpoint": "https://searx.example/search"},
        {"url": "https://searx.example/search"},
        {},
    )
    body = {"results": [{"title": "Chrys", "url": "https://example.com/chrys", "content": "Agents."}]}
    data, request = await _search(tools, body)
    assert request.method == "GET"
    assert request.headers["Host"] == "searx.example"
    assert request.url.path == "/search"
    assert dict(request.url.params) == {"q": "chrys agents", "format": "json"}
    assert "authorization" not in request.headers
    assert data["results"] == [{"title": "Chrys", "url": "https://example.com/chrys", "snippet": "Agents."}]


@pytest.mark.parametrize(
    ("provider", "grant", "message"),
    [
        pytest.param(
            {"type": "custom_http", "preset": "serpapi"},
            {"url": "https://serpapi.com/search"},
            r"uses credential SERPAPI_API_KEY, which its tools\.web_search\.custom_endpoints grant does not list",
            id="serpapi-key-not-granted",
        ),
        pytest.param(
            {
                "type": "custom_http",
                "preset": "serpapi",
                "auth": {"location": "query", "name": "api_key", "key_env": "HOME"},
            },
            {"url": "https://serpapi.com/search", "credential_env_names": ["SERPAPI_API_KEY"]},
            r"uses credential HOME, which",
            id="overridden-key-not-granted",
        ),
        pytest.param(
            {"type": "custom_http", "preset": "searxng", "endpoint": "https://searx.example/search"},
            {"url": "https://searx.example/other"},
            r"endpoint https://searx\.example/search is not granted",
            id="searxng-endpoint-not-granted",
        ),
    ],
)
def test_an_ungranted_preset_fails_the_build_before_any_credential_is_read(provider, grant, message):
    config = parse_search({"mode": "provider", "provider": "custom", "providers": {"custom": provider}})
    settings = replace(Settings(), web_search_custom_endpoints=json.dumps([grant]))
    with patch(_RESOLVE, autospec=True) as resolve, pytest.raises(ValueError, match=message):
        build_web_tools(["web_search"], settings, config, None)
    resolve.assert_not_called()


# ------------------------------------------------------ the strict mapping parser

_BASE: dict[str, Any] = {
    "type": "custom_http",
    "endpoint": "https://search.example/api",
    "request": {"query_params": {"q": {"$input": "query"}}},
    "response": {"results_pointer": "/results", "url_pointer": "/url"},
}
_POST: dict[str, Any] = {"method": "POST", "request": {"json": {"q": {"$input": "query"}}}}
_DROP = object()


def _provider(**changes: Any) -> dict[str, Any]:
    raw = {**_BASE, **changes}
    return {key: value for key, value in raw.items() if value is not _DROP}


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param(_provider(), id="unauthenticated-get"),
        pytest.param(
            _provider(auth={"location": "header", "name": "Authorization", "key_env": "KEY", "prefix": "Bearer "}),
            id="authorization-header",
        ),
        pytest.param(
            _provider(auth={"location": "header", "name": "X-Api-Key", "key_env": "KEY"}), id="api-key-header"
        ),
        pytest.param(_provider(auth={"location": "query", "name": "key", "key_env": "KEY"}), id="query-credential"),
        pytest.param(
            _provider(
                **_POST,
                headers={"Accept": "application/json", "Content-Type": "application/json"},
                auth={"location": "header", "name": "X-Subscription-Token", "key_env": "KEY"},
            ),
            id="post-with-json-headers",
        ),
        pytest.param(
            _provider(method="POST", request={"json": {"q": {"$input": "query"}, "tenant": {"$env": "TENANT"}}}),
            id="whole-node-env-in-the-body",
        ),
        pytest.param(
            _provider(request={"query_params": {"q": {"$input": "query"}, "site": ["a", 1, True]}}),
            id="scalar-query-list",
        ),
    ],
)
def test_a_well_formed_custom_mapping_is_accepted(raw):
    config = parse_provider(raw)
    assert config.type == "custom_http"
    assert config.endpoint == "https://search.example/api"


def _nested(depth: int) -> Any:
    value: Any = "leaf"
    for _ in range(depth):
        value = [value]
    return value


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        # Method and the official-adapter credential field.
        pytest.param(_provider(api_key_env="KEY"), "Invalid custom HTTP method or authentication", id="api-key-env"),
        pytest.param(_provider(method="PUT"), "Invalid custom HTTP method or authentication", id="method-put"),
        pytest.param(_provider(method="post"), "Invalid custom HTTP method or authentication", id="method-lowercase"),
        # Where the credential goes.
        pytest.param(
            _provider(auth={"location": "cookie", "name": "sid", "key_env": "KEY"}),
            "Invalid authentication location",
            id="auth-in-a-cookie",
        ),
        pytest.param(_provider(auth={"location": 1}), "Invalid authentication location", id="auth-location-not-text"),
        pytest.param(
            _provider(auth={"location": "none", "key_env": "KEY"}),
            "Unauthenticated mapping cannot contain credentials",
            id="unauthenticated-with-a-key",
        ),
        pytest.param(
            _provider(auth={"location": "header", "name": "Authorization", "key_env": "KEY", "value": "literal"}),
            "auth contains unknown keys or null values",
            id="auth-unknown-key",
        ),
        pytest.param(
            _provider(auth={"location": "header", "name": "Authorization"}),
            "Expected an environment variable name",
            id="auth-without-key-env",
        ),
        pytest.param(
            _provider(auth={"location": "header", "name": "Authorization", "key_env": "BAD-NAME"}),
            "Expected an environment variable name",
            id="auth-invalid-key-env",
        ),
        pytest.param(
            _provider(auth={"location": "query", "name": "api key", "key_env": "KEY"}),
            "Invalid authentication field name",
            id="auth-invalid-name",
        ),
        pytest.param(
            _provider(auth={"location": "header", "name": "Authorization", "key_env": "KEY", "prefix": "Bearer\r\n"}),
            "Invalid authentication prefix",
            id="auth-prefix-with-a-line-break",
        ),
        pytest.param(
            _provider(auth={"location": "header", "name": "X-Custom-Token", "key_env": "KEY"}),
            "Unsupported or conflicting authentication header",
            id="auth-header-outside-the-allowed-set",
        ),
        pytest.param(
            _provider(
                headers={"Accept": "application/json"}, auth={"location": "header", "name": "accept", "key_env": "KEY"}
            ),
            "Unsupported or conflicting authentication header",
            id="auth-header-conflicts-with-a-static-header",
        ),
        pytest.param(
            _provider(auth={"location": "query", "name": "q", "key_env": "KEY"}),
            "Authentication conflicts with a query parameter",
            id="query-credential-conflicts-with-a-parameter",
        ),
        # Static headers never carry a credential.
        pytest.param(
            _provider(headers={"Authorization": "Bearer literal"}),
            "Credential headers must use auth with an environment variable",
            id="literal-authorization-header",
        ),
        pytest.param(
            _provider(headers={"X-Api-Key": "literal"}),
            "Credential headers must use auth with an environment variable",
            id="literal-api-key-header",
        ),
        pytest.param(
            _provider(headers={"User-Agent": "chrys"}), "Unsupported or duplicate HTTP header", id="unsupported-header"
        ),
        pytest.param(
            _provider(headers={"Accept": "application/json", "accept": "*/*"}),
            "Unsupported or duplicate HTTP header",
            id="duplicate-header",
        ),
        pytest.param(
            _provider(headers={"Accept": "application/json\r\nX-Injected: 1"}),
            "Invalid HTTP header value",
            id="header-value-with-a-line-break",
        ),
        pytest.param(_provider(headers={"Accept": 1}), "Invalid HTTP header value", id="header-value-not-text"),
        pytest.param(_provider(headers=["Accept"]), "Headers must be a mapping", id="headers-not-a-mapping"),
        pytest.param(
            _provider(**_POST, headers={"Content-Type": "text/plain"}),
            "POST content type must be application/json",
            id="non-json-content-type",
        ),
        # Request templates.
        pytest.param(_provider(**{**_POST, "method": "GET"}), "GET cannot contain a JSON body", id="get-with-a-body"),
        pytest.param(
            _provider(request={"body": {}}), "request contains unknown keys or null values", id="request-unknown-key"
        ),
        pytest.param(
            _provider(request={"query_params": ["q"]}),
            "query_params must be a mapping",
            id="query-params-not-a-mapping",
        ),
        pytest.param(
            _provider(request={"query_params": {"q": {"$env": "KEY", "fallback": "x"}}}),
            "Dynamic markers must occupy the entire node",
            id="env-marker-with-a-sibling",
        ),
        pytest.param(
            _provider(request={"query_params": {"q": {"nested": "x"}}}),
            "Query parameter values cannot contain objects",
            id="object-in-a-query-parameter",
        ),
        pytest.param(
            _provider(request={"query_params": {"q": [["a"]]}}),
            "Query parameters require scalar list items",
            id="nested-list-in-a-query-parameter",
        ),
        pytest.param(
            _provider(request={"query_params": {"q": None}}),
            "Query parameters cannot be null",
            id="null-query-parameter",
        ),
        pytest.param(
            _provider(request={"query_params": {"q": {"$env": "bad name"}}}),
            "Expected an environment variable name",
            id="env-marker-invalid-name",
        ),
        pytest.param(
            _provider(request={"query_params": {"q": {"$input": "page"}}}),
            "Unknown request input marker",
            id="unknown-input-marker",
        ),
        pytest.param(
            _provider(method="POST", request={"json": {"$ref": "#/q"}}),
            "Invalid request template key",
            id="dollar-key-in-the-body",
        ),
        pytest.param(
            _provider(method="POST", request={"json": {"q": _nested(25)}}),
            "Request template is too deeply nested",
            id="too-deeply-nested",
        ),
        pytest.param(
            _provider(method="POST", request={"json": {"pad": "x" * (300 * 1024)}}),
            "HTTP configuration exceeds size limit",
            id="body-template-over-300-kib",
        ),
        # Response mapping.
        pytest.param(
            _provider(response={"results_pointer": "/results"}),
            "Response mapping requires results and URL pointers",
            id="no-url-pointer",
        ),
        pytest.param(
            _provider(response={"results_pointer": "results", "url_pointer": "/url"}),
            "Invalid JSON Pointer",
            id="pointer-without-a-leading-slash",
        ),
        pytest.param(
            _provider(response={"results_pointer": "/results", "url_pointer": "/a~2b"}),
            "Invalid JSON Pointer",
            id="pointer-with-a-bad-escape",
        ),
        pytest.param(
            _provider(response={"results_pointer": "/results", "url_pointer": "/url", "score_pointer": "/s"}),
            "response contains unknown keys or null values",
            id="response-unknown-key",
        ),
        # The endpoint itself.
        pytest.param(
            _provider(endpoint="https://search.example/api?key=literal"),
            "Endpoints cannot contain query strings or fragments",
            id="endpoint-with-a-query-string",
        ),
        pytest.param(
            _provider(endpoint="https://user:secret@search.example/api"),
            "without credentials is required",
            id="endpoint-with-userinfo",
        ),
        pytest.param(_provider(endpoint=_DROP), "URL", id="no-endpoint"),
    ],
)
def test_a_malformed_custom_mapping_is_rejected(raw, message):
    with pytest.raises(ValueError, match=message):
        parse_provider(raw)
