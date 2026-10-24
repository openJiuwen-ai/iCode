# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""A provider's profile form rebuilds the same provider and stays minimal."""

from __future__ import annotations

import pytest

from chrys.service.tools.builtins.web.config import parse_provider, provider_fields

_CUSTOM = {
    "type": "custom_http",
    "endpoint": "https://search.example.com/api",
    "method": "POST",
    "auth": {"location": "header", "name": "Authorization", "key_env": "CORP_KEY", "prefix": "Bearer "},
    "headers": {"Accept": "application/json"},
    "request": {"json": {"q": {"$input": "query"}, "size": {"$input": "limit"}}},
    "response": {"results_pointer": "/items", "url_pointer": "/url", "title_pointer": "/title"},
}


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param({"type": "exa_mcp"}, id="exa_mcp"),
        pytest.param({"type": "bing_html"}, id="bing_html"),
        pytest.param({"type": "duckduckgo_html"}, id="duckduckgo_html"),
        pytest.param({"type": "tavily", "api_key_env": "TAVILY_API_KEY"}, id="tavily"),
        pytest.param({"type": "brave", "api_key_env": "BRAVE_API_KEY"}, id="brave"),
        pytest.param({"type": "exa", "api_key_env": "EXA_API_KEY"}, id="exa"),
        pytest.param(_CUSTOM, id="custom_http"),
        pytest.param(
            {
                "type": "custom_http",
                "endpoint": "https://search.example.com/api",
                "response": {"results_pointer": "/r", "url_pointer": "/u"},
            },
            id="custom_http-defaults",
        ),
        pytest.param(
            {"type": "custom_http", "preset": "searxng", "endpoint": "https://searx.example/search"}, id="searxng"
        ),
        pytest.param({"type": "custom_http", "preset": "serpapi"}, id="serpapi"),
        pytest.param(
            {"type": "custom_http", "preset": "serpapi", "request": {"query_params": {"q": {"$input": "query"}}}},
            id="serpapi-override",
        ),
        pytest.param({"type": "custom_http", "preset": "serply"}, id="serply"),
    ],
)
def test_the_profile_form_is_what_was_written_and_rebuilds_the_provider(raw):
    config = parse_provider(raw)
    fields = provider_fields(config)
    assert fields == raw
    assert parse_provider(fields) == config


def test_the_profile_form_shares_nothing_with_the_provider():
    config = parse_provider(_CUSTOM)
    fields = provider_fields(config)
    fields["request"]["json"]["q"] = "changed"
    fields["auth"]["prefix"] = "Token "
    assert provider_fields(config) == _CUSTOM
