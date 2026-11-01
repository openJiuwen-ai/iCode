# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""JSON search APIs: the official Tavily, Brave and Exa adapters, keyless Keenable and declarative custom_http."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import httpx

from chrys.foundation.branding import APP_DISPLAY_NAME
from chrys.foundation.net.url import normalize_url
from chrys.service.tools.builtins.web.config import ANONYMOUS_SEARCH_ENDPOINTS, SearchProviderConfig
from chrys.service.tools.builtins.web.credentials import ResolvedSearchCredentials
from chrys.service.tools.builtins.web.http import WebError, bounded_body, check_status
from chrys.service.tools.builtins.web.search.providers import ENVIRONMENT_FAILURES, site_filtered
from chrys.service.tools.builtins.web.search.types import SearchHit, SearchRequest, SearchResponse

_DEFAULT_MAPPING = {
    "results_pointer": "/results",
    "title_pointer": "/title",
    "url_pointer": "/url",
    "snippet_pointer": "/content",
}


def at_pointer(data: Any, path: str) -> Any:
    """Resolve RFC 6901 segments without recursive searches or coercion."""
    for token in path.split("/")[1:] if path else ():
        token = token.replace("~1", "/").replace("~0", "~")
        if isinstance(data, dict):
            if token not in data:
                raise KeyError(token)
            data = data[token]
        elif isinstance(data, list) and token.isdecimal() and (token == "0" or not token.startswith("0")):
            data = data[int(token)]
        else:
            raise KeyError(token)
    return data


def expand(value: Any, inputs: dict[str, Any], secrets: dict[str, str]) -> Any:
    if isinstance(value, dict):
        if "$input" in value:
            return inputs[value["$input"]]
        if "$env" in value:
            return secrets[value["$env"]]
        return {key: expand(item, inputs, secrets) for key, item in value.items()}
    if isinstance(value, list):
        return [expand(item, inputs, secrets) for item in value]
    return value


def query_pairs(params: dict[str, Any]) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for key, value in params.items():
        for item in value if isinstance(value, list) else [value]:
            if item is None or isinstance(item, dict | list):
                raise WebError("invalid_request")
            pairs.append((key, str(item).lower() if isinstance(item, bool) else str(item)))
    return pairs


def parse_hits(data: Any, mapping: dict[str, str], *, highlights: bool = False) -> tuple[SearchHit, ...]:
    try:
        items = at_pointer(data, mapping["results_pointer"])
    except KeyError, IndexError:
        raise WebError("protocol_error") from None
    if not isinstance(items, list):
        raise WebError("protocol_error")
    hits: list[SearchHit] = []
    for item in items:
        try:
            raw_url = at_pointer(item, mapping["url_pointer"])
            url = normalize_url(raw_url)
        except ValueError, KeyError, IndexError, TypeError:
            continue
        values = {"title": url, "snippet": ""}
        for key in values:
            if key + "_pointer" not in mapping:
                continue
            try:
                value = at_pointer(item, mapping[key + "_pointer"])
            except KeyError, IndexError:
                continue
            if highlights and key == "snippet" and isinstance(value, list) and all(isinstance(v, str) for v in value):
                value = "\n".join(value)
            if not isinstance(value, str):
                # A null title/snippet degrades to the default; only wholesale silence is protocol failure.
                continue
            values[key] = value
        hits.append(SearchHit(values["title"][:1000], url, values["snippet"]))
    if items and not hits:
        raise WebError("protocol_error")
    return tuple(hits)


@dataclass(frozen=True)
class _WireRequest:
    method: str
    headers: dict[str, str]
    params: dict[str, Any] = field(default_factory=dict)
    body: dict[str, Any] | None = None
    send_body: bool = False
    """custom_http sends its configured JSON body even when that body is null."""
    mapping: dict[str, str] = field(default_factory=lambda: dict(_DEFAULT_MAPPING))


@dataclass(frozen=True)
class JsonApiProvider:
    """One selected keyed, keyless or custom JSON API, with an isolated credential snapshot."""

    id: str
    config: SearchProviderConfig
    credentials: ResolvedSearchCredentials = field(repr=False)

    @property
    def endpoint(self) -> str:
        return self.config.url

    def falls_through(self, failure: WebError) -> bool:
        # A 4xx from a keyed API is about our key or quota: the user must see it.
        # A keyless API holds no key of ours, so its 4xx is the service refusing the query.
        if failure.code == "http_4xx" and self.config.type in ANONYMOUS_SEARCH_ENDPOINTS:
            return True
        return failure.code in ENVIRONMENT_FAILURES

    async def search(self, request: SearchRequest, *, http: httpx.AsyncClient) -> SearchResponse:
        wire = self._wire_request(request)
        headers = {"Accept": "application/json", "Accept-Encoding": "identity", **wire.headers}
        for header_value in headers.values():
            if "\r" in header_value or "\n" in header_value:
                raise WebError("invalid_request")
            try:
                header_value.encode("ascii")
            except UnicodeEncodeError:
                # The exception repr carries the full header value; never let it escape to logs.
                raise WebError("non_ascii_header") from None
        content = None
        if wire.body is not None or wire.send_body:
            content = json.dumps(wire.body, ensure_ascii=False, allow_nan=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        url = httpx.URL(self.endpoint, params=query_pairs(wire.params))
        if (
            len(str(url).encode()) > 16384
            or len(json.dumps(headers).encode()) > 16384
            or (content is not None and len(content) > 300 * 1024)
        ):
            raise WebError("request_too_large")
        async with http.stream(wire.method, url, headers=headers, content=content) as response:
            check_status(response)
            mime = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            if mime != "application/json" and not mime.endswith("+json"):
                raise WebError("protocol_error")
            raw = await bounded_body(response, decompress=False)
        try:
            parsed = json.loads(raw)
        except ValueError, UnicodeError, RecursionError:
            raise WebError("protocol_error") from None
        if (
            self.config.type == "brave"
            and isinstance(parsed, dict)
            and parsed.get("type") == "search"
            and parsed.get("web") is None
        ):
            # A Brave search response leaves out (or nulls) its web section when a query has no web
            # results. Anything else without one, such as `{}` or an error object, stays a protocol error.
            return SearchResponse(self.id, ())
        return SearchResponse(self.id, parse_hits(parsed, wire.mapping, highlights=self.config.type == "exa"))

    def _wire_request(self, request: SearchRequest) -> _WireRequest:
        cfg = self.config
        secrets = self.credentials.values
        if cfg.type == "tavily":
            body: dict[str, Any] = {"query": request.query, "max_results": request.limit, "search_depth": "basic"}
            if request.allowed_domains:
                body.update(include_domains=list(request.allowed_domains), include_domains_mode="restrict")
            if request.blocked_domains:
                body["exclude_domains"] = list(request.blocked_domains)
            return _WireRequest("POST", {"Authorization": "Bearer " + secrets[cfg.api_key_env]}, body=body)
        if cfg.type == "exa":
            body = {
                "query": request.query,
                "numResults": request.limit,
                "type": "auto",
                "contents": {"highlights": True},
            }
            if request.allowed_domains:
                body["includeDomains"] = list(request.allowed_domains)
            if request.blocked_domains:
                body["excludeDomains"] = list(request.blocked_domains)
            return _WireRequest(
                "POST",
                {"x-api-key": secrets[cfg.api_key_env]},
                body=body,
                mapping={**_DEFAULT_MAPPING, "snippet_pointer": "/highlights"},
            )
        request = site_filtered(request)
        if cfg.type == "brave":
            return _WireRequest(
                "GET",
                {"X-Subscription-Token": secrets[cfg.api_key_env]},
                params={"q": request.query, "count": request.limit},
                mapping={**_DEFAULT_MAPPING, "results_pointer": "/web/results", "snippet_pointer": "/description"},
            )
        if cfg.type == "keenable":
            # The keyless endpoint requires an app name; its result text is in snippet.
            return _WireRequest(
                "POST",
                {"X-Keenable-Title": APP_DISPLAY_NAME},
                body={"query": request.query, "max_results": request.limit},
                mapping={**_DEFAULT_MAPPING, "snippet_pointer": "/snippet"},
            )
        inputs = {
            "query": request.query,
            "limit": request.limit,
            "allowed_domains": list(request.allowed_domains),
            "blocked_domains": list(request.blocked_domains),
        }
        expanded = expand(cfg.request or {}, inputs, secrets)
        headers = dict(cfg.headers or {})
        params = expanded.get("query_params", {})
        auth = cfg.auth or {}
        if auth.get("location", "none") != "none":
            value = auth.get("prefix", "") + secrets[auth["key_env"]]
            if auth["location"] == "header":
                headers[auth["name"]] = value
            else:
                params[auth["name"]] = value
        return _WireRequest(
            cfg.method,
            headers,
            params=params,
            body=expanded.get("json"),
            send_body="json" in (cfg.request or {}),
            mapping=cfg.response or {},
        )
