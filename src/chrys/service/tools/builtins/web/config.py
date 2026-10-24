# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Strict profile patches for local web capabilities.

Resolved secrets deliberately live elsewhere and are never dataclass fields in
these serializable profile values.
"""

from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass
from typing import Any

from chrys.foundation.config.web_values import ENV_NAME
from chrys.foundation.net.url import normalize_url

PROVIDER_ID = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")
OFFICIAL_ENDPOINTS = {
    "tavily": "https://api.tavily.com/search",
    "brave": "https://api.search.brave.com/res/v1/web/search",
    "exa": "https://api.exa.ai/search",
}
HTML_SEARCH_ENDPOINTS = {
    "bing_html": "https://www.bing.com/search",
    "duckduckgo_html": "https://html.duckduckgo.com/html/",
}
ANONYMOUS_SEARCH_ENDPOINTS = {"exa_mcp": "https://mcp.exa.ai/mcp", **HTML_SEARCH_ENDPOINTS}
INPUT_NAMES = {"query", "limit", "allowed_domains", "blocked_domains"}
# custom_http shorthands: a profile names one and overrides only what differs.
CUSTOM_PRESETS: dict[str, dict[str, Any]] = {
    "searxng": {
        "request": {"query_params": {"q": {"$input": "query"}, "format": "json"}},
        "response": {
            "results_pointer": "/results",
            "url_pointer": "/url",
            "title_pointer": "/title",
            "snippet_pointer": "/content",
        },
    },
    "serpapi": {
        "endpoint": "https://serpapi.com/search",
        "auth": {"location": "query", "name": "api_key", "key_env": "SERPAPI_API_KEY"},
        "request": {"query_params": {"engine": "google", "q": {"$input": "query"}}},
        "response": {
            "results_pointer": "/organic_results",
            "url_pointer": "/link",
            "title_pointer": "/title",
            "snippet_pointer": "/snippet",
        },
    },
    "serply": {
        "endpoint": "https://api.serply.io/v1/search",
        "auth": {"location": "header", "name": "X-Api-Key", "key_env": "SERPLY_API_KEY"},
        "request": {"query_params": {"q": {"$input": "query"}, "num": {"$input": "limit"}}},
        "response": {
            "results_pointer": "/results",
            "url_pointer": "/link",
            "title_pointer": "/title",
            "snippet_pointer": "/description",
        },
    },
}


def mapping(raw: object, keys: set[str], section: str, *, nullable: frozenset[str] = frozenset()) -> dict[str, Any]:
    """Reject unknown keys and explicit null at configuration object boundaries."""
    if not isinstance(raw, dict) or any(not isinstance(k, str) for k in raw):
        raise ValueError(f"{section} must be a mapping")
    if raw.keys() - keys or any(value is None and key not in nullable for key, value in raw.items()):
        raise ValueError(f"{section} contains unknown keys or null values")
    return dict(raw)


def integer(value: object, low: int, high: int, name: str) -> int:
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"{name} must be an integer from {low} to {high}")
    return value


def env_name(value: object) -> str:
    if not isinstance(value, str) or ENV_NAME.fullmatch(value) is None:
        raise ValueError("Expected an environment variable name")
    return value


def template_envs(value: Any, *, query: bool = False, depth: int = 0) -> set[str]:
    """Validate whole-node substitution grammar and collect credential names."""
    if depth > 20:
        raise ValueError("Request template is too deeply nested")
    if isinstance(value, dict):
        if "$env" in value or "$input" in value:
            if len(value) != 1:
                raise ValueError("Dynamic markers must occupy the entire node")
            if "$env" in value:
                return {env_name(value["$env"])}
            if not isinstance(value["$input"], str) or value["$input"] not in INPUT_NAMES:
                raise ValueError("Unknown request input marker")
            return set()
        if query:
            raise ValueError("Query parameter values cannot contain objects")
        names: set[str] = set()
        for key, item in value.items():
            if not isinstance(key, str) or key.startswith("$"):
                raise ValueError("Invalid request template key")
            names.update(template_envs(item, depth=depth + 1))
        return names
    if isinstance(value, list):
        names = set()
        for item in value:
            if query and isinstance(item, list | dict):
                raise ValueError("Query parameters require scalar list items")
            names.update(template_envs(item, query=query, depth=depth + 1))
        return names
    if value is None and query:
        raise ValueError("Query parameters cannot be null")
    if value is not None and not isinstance(value, str | int | float | bool):
        raise ValueError("Request values must be JSON values")
    return set()


def pointer(value: object) -> str:
    if not isinstance(value, str) or (value and not value.startswith("/")) or re.search(r"~(?![01])", value):
        raise ValueError("Invalid JSON Pointer")
    return value


@dataclass(frozen=True)
class SearchProviderConfig:
    """Validated wire mapping, before credential resolution."""

    type: str
    preset: str | None = None
    """The custom_http shorthand the fields were expanded from; saving writes it back."""
    api_key_env: str = ""
    endpoint: str = ""
    method: str = "GET"
    auth: dict[str, Any] | None = None
    headers: dict[str, str] | None = None
    request: dict[str, Any] | None = None
    response: dict[str, str] | None = None

    @property
    def url(self) -> str:
        """The URL this provider's requests go to."""
        return ANONYMOUS_SEARCH_ENDPOINTS.get(self.type) or OFFICIAL_ENDPOINTS.get(self.type) or self.endpoint

    def credential_names(self) -> set[str]:
        names = {self.api_key_env} if self.api_key_env else set()
        if self.auth and self.auth.get("key_env"):
            names.add(self.auth["key_env"])
        names.update(template_envs(self.request or {}))
        return names


def parse_provider(raw: object) -> SearchProviderConfig:
    data = mapping(
        raw,
        {"type", "api_key_env", "preset", "endpoint", "method", "auth", "headers", "request", "response"},
        "provider",
    )
    provider_type = data.get("type")
    if not isinstance(provider_type, str):
        raise ValueError("Provider type must be a string")
    if provider_type in ANONYMOUS_SEARCH_ENDPOINTS:
        if data.keys() != {"type"}:
            raise ValueError("Anonymous search adapters do not accept credentials or endpoint overrides")
        return SearchProviderConfig(type=provider_type)
    if provider_type in OFFICIAL_ENDPOINTS:
        if data.keys() - {"type", "api_key_env"}:
            raise ValueError("Official adapters do not accept endpoint overrides")
        return SearchProviderConfig(type=provider_type, api_key_env=env_name(data.get("api_key_env")))
    if provider_type != "custom_http":
        raise ValueError("Unknown search provider type")
    preset = data.pop("preset", None)
    if preset is not None and (not isinstance(preset, str) or preset not in CUSTOM_PRESETS):
        raise ValueError("Unknown custom provider preset")
    defaults = copy.deepcopy(CUSTOM_PRESETS[preset]) if preset is not None else {}
    data = defaults | data
    if not data.get("endpoint"):
        raise ValueError("A custom_http provider needs an endpoint URL")
    endpoint = normalize_url(data["endpoint"], endpoint=True)
    method = data.get("method", "GET")
    if not isinstance(method, str) or method not in {"GET", "POST"} or "api_key_env" in data:
        raise ValueError("Invalid custom HTTP method or authentication")
    auth = mapping(data.get("auth", {"location": "none"}), {"location", "name", "key_env", "prefix"}, "auth")
    location = auth.get("location", "none")
    if not isinstance(location, str):
        raise ValueError("Invalid authentication location")
    if location == "none":
        if auth.keys() - {"location"}:
            raise ValueError("Unauthenticated mapping cannot contain credentials")
    elif location in {"header", "query"}:
        env_name(auth.get("key_env"))
        if not isinstance(auth.get("name"), str) or not re.fullmatch(r"[A-Za-z0-9_-]+", auth["name"]):
            raise ValueError("Invalid authentication field name")
        if not isinstance(auth.get("prefix", ""), str) or any(c in auth.get("prefix", "") for c in "\r\n"):
            raise ValueError("Invalid authentication prefix")
    else:
        raise ValueError("Invalid authentication location")
    headers = data.get("headers", {})
    allowed_headers = {"accept", "content-type", "authorization", "x-api-key", "x-subscription-token"}
    if not isinstance(headers, dict):
        raise ValueError("Headers must be a mapping")
    seen: set[str] = set()
    for key, value in headers.items():
        if not isinstance(key, str) or key.lower() not in allowed_headers or key.lower() in seen:
            raise ValueError("Unsupported or duplicate HTTP header")
        if not isinstance(value, str) or any(c in value for c in "\r\n"):
            raise ValueError("Invalid HTTP header value")
        if key.lower() not in {"accept", "content-type"}:
            raise ValueError("Credential headers must use auth with an environment variable")
        seen.add(key.lower())
        if key.lower() == "content-type" and value.lower() != "application/json":
            raise ValueError("POST content type must be application/json")
    if location == "header" and (auth["name"].lower() not in allowed_headers or auth["name"].lower() in seen):
        raise ValueError("Unsupported or conflicting authentication header")
    request = mapping(data.get("request", {}), {"query_params", "json"}, "request", nullable=frozenset({"json"}))
    if method == "GET" and "json" in request:
        raise ValueError("GET cannot contain a JSON body")
    params = request.get("query_params", {})
    if not isinstance(params, dict):
        raise ValueError("query_params must be a mapping")
    for key, value in params.items():
        if not isinstance(key, str):
            raise ValueError("Query parameter names must be strings")
        template_envs(value, query=True)
    template_envs(request)
    if location == "query" and auth["name"] in params:
        raise ValueError("Authentication conflicts with a query parameter")
    response = mapping(
        data.get("response", {}), {"results_pointer", "url_pointer", "title_pointer", "snippet_pointer"}, "response"
    )
    for key in ("results_pointer", "url_pointer"):
        if key not in response:
            raise ValueError("Response mapping requires results and URL pointers")
    for value in response.values():
        pointer(value)
    if len(json.dumps(request, allow_nan=False).encode()) > 300 * 1024 or len(json.dumps(headers).encode()) > 16 * 1024:
        raise ValueError("HTTP configuration exceeds size limit")
    return SearchProviderConfig(
        type="custom_http",
        preset=preset,
        endpoint=endpoint,
        method=method,
        auth=auth,
        headers=headers,
        request=request,
        response=response,
    )


def provider_fields(config: SearchProviderConfig) -> dict[str, Any]:
    """The profile form of *config*: exactly what ``parse_provider`` needs to rebuild it.

    A preset provider keeps its preset plus the fields that differ from it, so a
    later release's fix to the preset still reaches the profile. The result
    shares nothing with *config*.
    """
    if config.type in ANONYMOUS_SEARCH_ENDPOINTS:
        return {"type": config.type}
    if config.type in OFFICIAL_ENDPOINTS:
        return {"type": config.type, "api_key_env": config.api_key_env}
    preset = CUSTOM_PRESETS[config.preset] if config.preset is not None else {}
    baseline: dict[str, Any] = {
        "endpoint": normalize_url(preset["endpoint"], endpoint=True) if "endpoint" in preset else "",
        "method": preset.get("method", "GET"),
        "auth": preset.get("auth", {"location": "none"}),
        "headers": preset.get("headers", {}),
        "request": preset.get("request", {}),
        "response": preset.get("response", {}),
    }
    current: dict[str, Any] = {
        "endpoint": config.endpoint,
        "method": config.method,
        "auth": config.auth or {"location": "none"},
        "headers": config.headers or {},
        "request": config.request or {},
        "response": config.response or {},
    }
    fields: dict[str, Any] = {"type": config.type}
    if config.preset is not None:
        fields["preset"] = config.preset
    fields.update((key, value) for key, value in current.items() if value != baseline[key])
    return copy.deepcopy(fields)


@dataclass
class WebSearchConfigPatch:
    """None means absent; explicit empty lists survive serialization."""

    mode: str | None = None
    provider: str | None = None
    fallback_chain: list[str] | None = None
    providers: dict[str, SearchProviderConfig] | None = None
    num_results: int | None = None
    timeout_seconds: int | None = None


@dataclass
class WebFetchConfigPatch:
    """Per-profile fetch limits; origins cannot be granted by a profile."""

    mode: str | None = None
    max_tokens: int | None = None
    timeout_seconds: int | None = None


def parse_search(raw: object) -> WebSearchConfigPatch:
    data = mapping(
        raw, {"mode", "provider", "fallback_chain", "providers", "num_results", "timeout_seconds"}, "web_search"
    )
    if "mode" in data and (not isinstance(data["mode"], str) or data["mode"] not in {"off", "provider", "auto"}):
        raise ValueError("web_search mode must be off, auto or provider")
    if "num_results" in data:
        integer(data["num_results"], 1, 20, "num_results")
    if "timeout_seconds" in data:
        integer(data["timeout_seconds"], 1, 120, "timeout_seconds")
    if "provider" in data and (not isinstance(data["provider"], str) or not PROVIDER_ID.fullmatch(data["provider"])):
        raise ValueError("Invalid provider ID")
    if "fallback_chain" in data:
        chain = data["fallback_chain"]
        if (
            not isinstance(chain, list)
            or len(chain) > 5
            or any(not isinstance(i, str) or not PROVIDER_ID.fullmatch(i) for i in chain)
            or len(chain) != len(set(chain))
        ):
            raise ValueError("Invalid fallback chain")
    if "providers" in data:
        providers = data["providers"]
        if not isinstance(providers, dict) or any(
            not isinstance(i, str) or not PROVIDER_ID.fullmatch(i) for i in providers
        ):
            raise ValueError("Invalid providers mapping")
        data["providers"] = {key: parse_provider(value) for key, value in providers.items()}
    return WebSearchConfigPatch(**data)


def parse_fetch(raw: object) -> WebFetchConfigPatch:
    data = mapping(raw, {"mode", "max_tokens", "timeout_seconds"}, "web_fetch")
    if "mode" in data and (not isinstance(data["mode"], str) or data["mode"] not in {"off", "on"}):
        raise ValueError("Invalid web_fetch mode")
    for key, high in (("max_tokens", 64000), ("timeout_seconds", 120)):
        if key in data:
            integer(data[key], 1, high, key)
    return WebFetchConfigPatch(**data)
