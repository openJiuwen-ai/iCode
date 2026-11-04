# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Assemble one agent build's web tools, before exposing any of them.

Every build path (the main agent, sub-agents and workflow nodes) calls
``assemble_web_tools`` once and hands the result to the tool registry, its
pass-start hooks, the MCP name reservation and the final name check.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass, replace
from typing import Any
from urllib.parse import urlsplit

from chrys.foundation.config.settings import Settings
from chrys.foundation.config.web_values import parse_custom_endpoint_grants, parse_origins
from chrys.foundation.events.types import Warning
from chrys.foundation.i18n import msg
from chrys.foundation.net.url import origin
from chrys.service.tools.builtins.web.config import (
    HTML_SEARCH_ENDPOINTS,
    OFFICIAL_ENDPOINTS,
    SearchProviderConfig,
    WebFetchConfigPatch,
    WebSearchConfigPatch,
    parse_provider,
    provider_fields,
)
from chrys.service.tools.builtins.web.credentials import ResolvedSearchCredentials, resolve_search_credentials
from chrys.service.tools.builtins.web.fetch.tool import WebFetchTools
from chrys.service.tools.builtins.web.hosted import (
    HOSTED_WEB_TOOL_NAMES,
    declared_web_tool_names,
    hosted_web_tools_warning,
)
from chrys.service.tools.builtins.web.http import DestinationPolicy, WebEgressRuntime
from chrys.service.tools.builtins.web.search.exa_mcp import ExaMcpProvider
from chrys.service.tools.builtins.web.search.html import HtmlSearchProvider
from chrys.service.tools.builtins.web.search.json_api import JsonApiProvider
from chrys.service.tools.builtins.web.search.providers import SearchProvider
from chrys.service.tools.builtins.web.search.tool import WebSearchTools

logger = logging.getLogger(__name__)

_WEB_TOOLS_UNAVAILABLE = msg(
    "builder.web_tools_unavailable",
    fallback="Agent {agent} starts without its web tools: {reason}",
)


@dataclass(frozen=True)
class WebTools:
    """One agent build's web tools, and what its caller publishes, reserves and checks."""

    search: WebSearchTools | None = None
    fetch: WebFetchTools | None = None
    declared: tuple[str, ...] = ()
    """Web tool names the model's provider-hosted declarations claim, repeats included."""
    warnings: tuple[Warning, ...] = ()
    """For the caller to publish: displaced local tools, or web tools a configuration problem left out."""

    @property
    def hosted(self) -> frozenset[str]:
        """Names the provider owns: no local or MCP tool may take them."""
        return frozenset(self.declared)

    def tools(self, category: str) -> list[Any]:
        """The tools a builtin category contributes."""
        instance = {"web_search": self.search, "web_fetch": self.fetch}.get(category)
        return instance.tools() if instance is not None else []

    def begin_pass(self) -> None:
        """Reset the per-pass search call budget; a pass-start hook."""
        if self.search is not None:
            self.search.reset_budget()

    def provider_origins(self) -> dict[str, str]:
        """Selected provider ids and origins, without credentials or endpoint queries."""
        if self.search is None:
            return {}
        return {provider.id: origin(provider.endpoint) for provider in self.search.providers}

    def check_names(self, tools: Iterable[Any]) -> None:
        """Fail the build if a hosted declaration shares its name with a tool or another declaration."""
        local = {tool.name for tool in tools}
        for name in sorted(HOSTED_WEB_TOOL_NAMES):
            if self.declared.count(name) > 1:
                raise ValueError(f"Provider-hosted web tool name {name!r} is declared more than once")
            if name in local and name in self.declared:
                raise ValueError(f"Local and provider-hosted web tools both claim the name {name!r}")


def assemble_web_tools(
    categories: Iterable[str],
    settings: Settings,
    search: WebSearchConfigPatch | None,
    fetch: WebFetchConfigPatch | None,
    *,
    chat_options: dict[str, Any] | None,
    agent: str,
    model_profile_id: str,
    session_id: str | None,
) -> WebTools:
    """Decide one agent build's web tools; a problem with them never fails the build.

    Ownership comes first: a tool the model's provider hosts yields before any
    credential or grant is read for it. A configuration problem then costs the
    agent only its web tools, and the result carries the warning saying so.
    """
    declared = declared_web_tool_names(chat_options)
    requested = [category for category in dict.fromkeys(categories) if category in HOSTED_WEB_TOOL_NAMES]
    hosted = frozenset(declared)
    warnings: list[Warning] = []
    suppressed = hosted.intersection(requested)
    if suppressed:
        warnings.append(hosted_web_tools_warning(suppressed, model_profile_id=model_profile_id, session_id=session_id))
    try:
        built = build_web_tools(
            [category for category in requested if category not in hosted], settings, search, fetch, hosted=hosted
        )
    except ValueError as err:
        logger.warning("Web tools unavailable: %s", err)
        built = WebTools()
        warnings.append(
            Warning(
                code="web_tools_unavailable",
                message=f"Web tools unavailable for agent {agent}: {err}",
                display_message=_WEB_TOOLS_UNAVAILABLE.bind(agent=agent, reason=str(err)),
                session_id=session_id,
            )
        )
    return replace(built, declared=declared, warnings=tuple(warnings))


def build_web_tools(
    categories: list[str],
    settings: Settings,
    search: WebSearchConfigPatch | None,
    fetch: WebFetchConfigPatch | None,
    *,
    hosted: frozenset[str] = frozenset(),
) -> WebTools:
    """Authorize endpoint/name bindings before reading any credential values.

    Every configuration problem raises ValueError with a message naming the
    setting or profile field at fault. *hosted* names the web tools the model's
    provider runs itself, which each local description may point the model at.
    """
    search = search or WebSearchConfigPatch()
    fetch = fetch or WebFetchConfigPatch()
    if not HOSTED_WEB_TOOL_NAMES.intersection(categories):
        return WebTools()
    runtime = WebEgressRuntime(proxy_url=settings.web_egress_proxy_url, proxy_dns=settings.web_egress_proxy_dns)
    search_mode = search.mode if search.mode is not None else settings.web_search_mode
    fetch_on = "web_fetch" in categories and (fetch.mode if fetch.mode is not None else settings.web_fetch_mode) == "on"
    search_tools = None
    if "web_search" in categories and search_mode != "off":
        search_tools = WebSearchTools(
            _search_providers(search, search_mode, settings),
            runtime,
            DestinationPolicy(
                private_origins=parse_origins(settings.web_search_private_origins),
                http_origins=parse_origins(settings.web_search_http_origins),
            ),
            num_results=search.num_results if search.num_results is not None else settings.web_search_num_results,
            timeout_seconds=search.timeout_seconds
            if search.timeout_seconds is not None
            else settings.web_search_timeout_seconds,
            fetch_available=fetch_on or "web_fetch" in hosted,
        )
    fetch_tools = None
    if fetch_on:
        fetch_tools = WebFetchTools(
            runtime,
            DestinationPolicy(
                private_origins=parse_origins(settings.web_fetch_private_origins),
                http_origins=parse_origins(settings.web_fetch_http_origins),
                allowed_origins=parse_origins(settings.web_fetch_allowed_origins),
                denied_origins=parse_origins(settings.web_fetch_denied_origins),
            ),
            max_tokens=fetch.max_tokens if fetch.max_tokens is not None else settings.web_fetch_max_tokens,
            timeout_seconds=fetch.timeout_seconds
            if fetch.timeout_seconds is not None
            else settings.web_fetch_timeout_seconds,
            search_available=search_tools is not None or "web_search" in hosted,
        )
    return WebTools(search_tools, fetch_tools)


def create_provider(
    provider_id: str, config: SearchProviderConfig, credentials: ResolvedSearchCredentials
) -> SearchProvider:
    """The adapter for one validated provider configuration."""
    if config.type == "exa_mcp":
        return ExaMcpProvider(provider_id)
    if config.type in HTML_SEARCH_ENDPOINTS:
        return HtmlSearchProvider(provider_id, config.type)
    if config.type in OFFICIAL_ENDPOINTS or config.type in {"keenable", "custom_http"}:
        return JsonApiProvider(provider_id, config, credentials)
    raise ValueError(f"Unknown search provider type {config.type!r}")


def _search_chain(search: WebSearchConfigPatch, mode: str) -> tuple[list[str], dict[str, SearchProviderConfig]]:
    """Return the selected provider ids and the provider map they refer to."""
    configured = search.providers or {}
    if mode == "provider":
        if not search.provider or search.fallback_chain is not None:
            raise ValueError("tools.web_search mode provider needs one provider and no fallback_chain")
        chain = [search.provider]
    elif mode == "auto":
        if search.provider is None and search.fallback_chain is None and search.providers is None:
            return ["exa"], {"exa": SearchProviderConfig(type="exa_mcp")}
        if search.provider is not None or not search.fallback_chain:
            raise ValueError(
                "tools.web_search mode auto needs a nonempty fallback_chain (and no provider) "
                "when providers are configured"
            )
        chain = search.fallback_chain
    else:
        raise ValueError(f"Invalid tools.web_search mode {mode!r}")
    for provider_id in chain:
        if provider_id not in configured:
            raise ValueError(f"tools.web_search selects provider {provider_id!r}, which is not in providers")
    return chain, configured


def _search_providers(search: WebSearchConfigPatch, mode: str, settings: Settings) -> tuple[SearchProvider, ...]:
    chain, configured = _search_chain(search, mode)
    try:
        grants = parse_custom_endpoint_grants(settings.web_search_custom_endpoints)
    except ValueError as err:
        raise ValueError(f"Invalid tools.web_search.custom_endpoints setting: {err}") from err
    http_origins = parse_origins(settings.web_search_http_origins)
    configs = []
    for provider_id in chain:
        # Round-trip through the strict parser: the copy shares nothing with the
        # profile, so a later edit cannot change an in-flight endpoint, template
        # or credential name.
        config = parse_provider(provider_fields(configured[provider_id]))
        names = config.credential_names()
        if config.type == "custom_http":
            if config.endpoint not in grants:
                raise ValueError(
                    f"Search provider {provider_id!r} endpoint {config.endpoint} is not granted in "
                    "user setting tools.web_search.custom_endpoints"
                )
            ungranted = sorted(names - grants[config.endpoint])
            if ungranted:
                raise ValueError(
                    f"Search provider {provider_id!r} uses credential {', '.join(ungranted)}, which its "
                    "tools.web_search.custom_endpoints grant does not list"
                )
        if urlsplit(config.url).scheme != "https" and origin(config.url) not in http_origins:
            raise ValueError(
                f"Search provider {provider_id!r} uses plain HTTP; grant {origin(config.url)} in "
                "user setting tools.web_search.http_origins"
            )
        configs.append((provider_id, config, names))
    # Every endpoint and credential name is authorized; only now read the values.
    return tuple(
        create_provider(
            provider_id, config, resolve_search_credentials(names) if names else ResolvedSearchCredentials({})
        )
        for provider_id, config, names in configs
    )
