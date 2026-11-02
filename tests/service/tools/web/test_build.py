# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Web build: settings parity, provider ownership, degrading to no web tools, and tool descriptions."""

from __future__ import annotations

import logging
from dataclasses import replace

import pytest

from chrys.foundation.config.settings import Settings
from chrys.foundation.config.web_values import web_custom_endpoints_coercer, web_origins_coercer, web_proxy_url_coercer
from chrys.foundation.i18n.formatting import format_message
from chrys.service.tools.builtins.web.build import build_web_tools
from chrys.service.tools.builtins.web.config import WebFetchConfigPatch, WebSearchConfigPatch, parse_search
from chrys.service.tools.builtins.web.credentials import resolve_search_credentials
from chrys.service.tools.builtins.web.fetch.tool import WebFetchTools
from chrys.service.tools.builtins.web.hosted import declared_web_tool_names
from chrys.service.tools.builtins.web.http import DestinationPolicy, WebEgressRuntime
from chrys.service.tools.registry import ToolRegistry
from tests.service.tools.web._support import assemble


@pytest.mark.parametrize(
    "field,coercer,value",
    [
        ("web_egress_proxy_url", web_proxy_url_coercer, "http://corp_proxy:3128"),
        ("web_search_http_origins", web_origins_coercer, '["http://svc_search.corp.example:8080"]'),
        ("web_fetch_private_origins", web_origins_coercer, '["http://10.0.0.5:8080"]'),
        ("web_fetch_denied_origins", web_origins_coercer, '["https://Blocked.Example.:443"]'),
        (
            "web_search_custom_endpoints",
            web_custom_endpoints_coercer,
            '[{"url": "https://svc_search.corp.example/api", "credential_env_names": ["CORP_KEY"]}]',
        ),
    ],
)
def test_every_value_the_settings_layer_keeps_builds(field, coercer, value):
    """The coercers and the build share parsers, so a kept value never breaks an agent build."""
    assert coercer(value).value == value
    built = build_web_tools(["web_search", "web_fetch"], replace(Settings(), **{field: value}), None, None)
    assert built.search is not None
    assert built.fetch is not None


@pytest.mark.parametrize(
    "config,reason",
    [
        ({"mode": "provider"}, "mode provider needs one provider"),
        ({"mode": "auto", "providers": {"a": {"type": "duckduckgo_html"}}}, "nonempty fallback_chain"),
        (
            {"mode": "auto", "fallback_chain": ["b"], "providers": {"a": {"type": "duckduckgo_html"}}},
            "selects provider 'b'",
        ),
        (
            {
                "mode": "provider",
                "provider": "corp",
                "providers": {
                    "corp": {
                        "type": "custom_http",
                        "endpoint": "https://search.example.com/search",
                        "method": "POST",
                        "request": {"json": {"q": {"$input": "query"}}},
                        "response": {"results_pointer": "/items", "url_pointer": "/url"},
                    }
                },
            },
            "tools.web_search.custom_endpoints",
        ),
    ],
)
def test_a_web_configuration_problem_costs_only_the_web_tools(config, reason, caplog):
    with caplog.at_level(logging.WARNING):
        web = assemble(["search", "web_search", "web_fetch"], parse_search(config))
    registry = ToolRegistry()
    tools = registry.load_builtins(["search", "web_search", "web_fetch"], settings=Settings(), web=web)
    assert {tool.name for tool in tools} >= {"grep", "glob"}
    assert registry.get("web_search") is None
    assert registry.get("web_fetch") is None
    assert web.provider_origins() == {}
    [warning] = web.warnings
    assert warning.code == "web_tools_unavailable"
    assert warning.session_id == "s"
    assert warning.display_message is not None
    shown = format_message(warning.display_message)
    assert shown.startswith("Agent Test starts without its web tools: ")
    assert reason in shown
    assert reason in caplog.text


def test_missing_credential_names_the_variable(monkeypatch):
    monkeypatch.delenv("WEB_BUILD_MISSING_KEY", raising=False)
    with pytest.raises(ValueError, match="WEB_BUILD_MISSING_KEY"):
        resolve_search_credentials({"WEB_BUILD_MISSING_KEY"})


def test_each_description_names_its_companion_only_when_the_agent_has_it():
    both = build_web_tools(["web_search", "web_fetch"], Settings(), None, None)
    assert both.search is not None and both.fetch is not None
    assert "web_fetch" in both.search.tools()[0].description
    assert "web_search" in both.fetch.tools()[0].description
    assert "Configured destinations: exa (https://mcp.exa.ai)" in both.search.tools()[0].description

    search_alone = build_web_tools(["web_search", "web_fetch"], Settings(), None, WebFetchConfigPatch(mode="off"))
    assert search_alone.search is not None and search_alone.fetch is None
    assert "web_fetch" not in search_alone.search.tools()[0].description

    fetch_alone = build_web_tools(["web_fetch"], Settings(), None, None)
    assert fetch_alone.search is None and fetch_alone.fetch is not None
    assert "web_search" not in fetch_alone.fetch.tools()[0].description


@pytest.mark.parametrize(
    "setting,profile,available",
    [
        pytest.param("on", None, True, id="default"),
        pytest.param("off", None, False, id="user-off"),
        pytest.param("off", "on", True, id="profile-on"),
        pytest.param("on", "off", False, id="profile-off"),
    ],
)
def test_the_agent_category_alone_turns_web_fetch_on_and_the_mode_only_turns_it_off(setting, profile, available):
    assert Settings().web_fetch_mode == "on"
    fetch = WebFetchConfigPatch(mode=profile)
    built = build_web_tools(["web_fetch"], replace(Settings(), web_fetch_mode=setting), None, fetch)
    assert (built.fetch is not None) is available
    assert build_web_tools(["web_search"], replace(Settings(), web_fetch_mode=setting), None, fetch).fetch is None


def test_a_provider_run_companion_counts_as_available():
    """With the provider running web_fetch, the local search must not say pages cannot be opened."""
    search = build_web_tools(["web_search"], Settings(), None, None, hosted=frozenset({"web_fetch"})).search
    assert search is not None
    assert "cannot open the pages" not in search.tools()[0].description
    assert "web_fetch" in search.tools()[0].description

    fetch = build_web_tools(["web_fetch"], Settings(), None, None, hosted=frozenset({"web_search"})).fetch
    assert fetch is not None
    assert "web_search" in fetch.tools()[0].description


@pytest.mark.parametrize(
    "declarations,expected",
    [
        ([{"type": "web_search"}], {"web_search"}),
        ([{"type": "web_search_preview"}], {"web_search"}),
        ([{"type": "web_search_2025_08_26"}], {"web_search"}),
        ([{"type": "web_search_preview_2025_03_11"}], {"web_search"}),
        ([{"type": "web_search_20250305"}], {"web_search"}),
        ([{"type": "web_search_20250305", "name": "web_search"}], {"web_search"}),
        ([{"type": "web_fetch"}], {"web_fetch"}),
        ([{"type": "web_fetch_20250910"}], {"web_fetch"}),
        ([{"type": "web_fetch_20250910", "name": "web_fetch"}], {"web_fetch"}),
        ([{"type": "web_search"}, {"type": "web_fetch"}], {"web_search", "web_fetch"}),
        ([{"name": "web_search"}], {"web_search"}),
        # A renamed declaration owns no local category name, so nothing yields.
        ([{"type": "web_search_20250305", "name": "search"}], set()),
        ([{"type": "web_search_latest"}], set()),
        ([{"type": "web_search_custom"}], set()),
        ([{"type": "code_interpreter"}], set()),
        ([], set()),
    ],
)
def test_hosted_declarations_are_recognized_by_type_or_name(declarations, expected):
    assert assemble([], chat_options={"tools": declarations}).hosted == expected


@pytest.mark.parametrize("options", [None, {}, {"tools": None}])
def test_options_without_declarations_host_nothing(options):
    assert declared_web_tool_names(options) == ()


def test_a_hosted_tool_yields_its_local_category_and_says_so():
    web = assemble(["web_search", "web_fetch"], chat_options={"tools": [{"type": "web_search"}]})
    registry = ToolRegistry()
    tools = registry.load_builtins(["web_search", "web_fetch"], settings=Settings(), web=web)
    assert [tool.name for tool in tools] == ["web_fetch", "sleep"]
    assert registry.get("web_search") is None
    assert web.provider_origins() == {}
    [warning] = web.warnings
    assert warning.code == "hosted_web_tools_preferred"
    assert "model profile model" in warning.message


def test_a_hosted_tool_yields_before_its_local_configuration_is_read():
    """Yielding happens before assembly: a rejected local mode never gets a chance to raise."""
    with pytest.raises(ValueError, match=r"Invalid tools\.web_search mode"):
        build_web_tools(["web_search"], Settings(), WebSearchConfigPatch(mode="native"), None)
    web = assemble(
        ["web_search"], WebSearchConfigPatch(mode="native"), chat_options={"tools": [{"type": "web_search"}]}
    )
    assert web.search is None
    assert [warning.code for warning in web.warnings] == ["hosted_web_tools_preferred"]


def test_a_hosted_tool_nobody_loads_locally_needs_no_warning():
    web = assemble(["web_fetch"], chat_options={"tools": [{"type": "web_search"}]})
    assert web.warnings == ()
    assert web.hosted == {"web_search"}


def test_hosted_names_stay_unique_across_every_assembled_tool():
    tool = WebFetchTools(WebEgressRuntime(), DestinationPolicy()).web_fetch
    with pytest.raises(ValueError, match=r"Local and provider-hosted.*web_fetch"):
        assemble([], chat_options={"tools": [{"type": "web_fetch"}]}).check_names([tool])
    with pytest.raises(ValueError, match="declared more than once"):
        assemble([], chat_options={"tools": [{"type": "web_search"}, {"type": "web_search_preview"}]}).check_names([])
    assemble([], chat_options={"tools": [{"type": "web_search"}]}).check_names([tool])


def test_a_repeated_web_category_loads_once():
    web = assemble(["web_search", "web_search"])
    registry = ToolRegistry()
    loaded = registry.load_builtins(["web_search", "web_search"], settings=Settings(), web=web)
    assert [tool.name for tool in loaded] == ["web_search", "sleep"]
    assert web.warnings == ()
    assert web.provider_origins() == {"exa": "https://mcp.exa.ai"}
