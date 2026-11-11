# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Bounded, best-effort model discovery without running credential commands."""

from __future__ import annotations

import asyncio
import ipaddress
import json
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import unquote

from chrys.foundation.text.reading_order import reading_order
from chrys.foundation.util.env_templates import has_command_placeholder as _has_command
from chrys.foundation.util.env_templates import resolve_env_templates
from chrys.foundation.util.header_charset import (
    api_key_charset_error,
    header_name_charset_error,
    header_value_charset_error,
    model_id_charset_error,
)
from chrys.foundation.util.imports import import_off_loop
from chrys.foundation.util.once_close import OnceClose
from chrys.service.llm.clients import _SDK_MODULES, _build_profile_http_client, _configured_connection_value
from chrys.service.llm.model_list_presets import ModelListPreset, match_preset
from chrys.service.llm.providers import PROVIDERS
from chrys.service.profiles.models.schema import ModelProfile

if TYPE_CHECKING:
    import httpx

_TOTAL_SECONDS = 60.0
_REQUEST_SECONDS = 5.0
_RETRY_BASE_SECONDS = 1.0
_RETRY_MAX_PAUSE_SECONDS = 4.0
_TOTAL_ROUNDS = 5
_MAX_RESPONSE_BYTES = 5 * 1024 * 1024
_MAX_MODELS = 5000
_MAX_ITEMS = (
    4 * _MAX_MODELS
)  # Entries looked at per response, so a junk response costs the UI loop no more than a real one.
_MAX_PAGES = 5
_MIN_PAGE_SECONDS = 0.05

UrlCorrection = Literal["added_v1", "removed_v1", "removed_endpoint", "added_https", "switched_https", "switched_http"]
_ENDPOINT_PATHS = (b"/chat/completions", b"/completions", b"/responses", b"/messages", b"/models", b"/embeddings")


@dataclass(frozen=True, slots=True)
class ConnectionSnapshot:
    """Unresolved connection fields captured from a model configuration form."""

    provider: str
    base_url: str
    api_key: str = ""
    http_headers: dict[str, str] = field(default_factory=dict)
    extra_headers: dict[str, str] = field(default_factory=dict)
    verify_ssl: bool = True
    bypass_proxy: bool = False
    connect_timeout: float = 10.0


def resolve_model_connection(snapshot: ConnectionSnapshot) -> ConnectionSnapshot:
    """Resolve the address and key using the conversation's provider fallbacks.

    Templates read the process environment and never run commands.
    Invalid or missing key templates raise ``ValueError`` before provider fallback.
    """
    spec = PROVIDERS.get(snapshot.provider)
    key = resolve_env_templates(
        snapshot.api_key.strip(),
        location="model-list API key",
        allow_commands=False,
    )
    if spec is None:
        return replace(snapshot, base_url=snapshot.base_url.strip(), api_key=key)
    return replace(
        snapshot,
        base_url=_configured_connection_value(
            snapshot.base_url.strip(),
            spec.base_url_env,
            spec.default_base_url,
        ),
        api_key=_configured_connection_value(key, spec.api_key_env),
    )


@dataclass(frozen=True, slots=True)
class ModelInfo:
    """A usable model ID."""

    id: str


@dataclass(frozen=True, slots=True)
class ModelDiscovery:
    """Discovered choices; unreachable means some address could not be checked."""

    models: list[ModelInfo]
    auth_rejected: bool = False
    url_corrections: tuple[UrlCorrection, ...] = ()
    corrected_base_url: str | None = None
    unreachable: bool = False


def has_command_placeholder(snapshot: ConnectionSnapshot) -> bool:
    """Detect unescaped credential commands before resolving any templates."""
    return any(
        _has_command(value)
        for value in (snapshot.api_key, *snapshot.http_headers.values(), *snapshot.extra_headers.values())
    )


def _credentials(snapshot: ConnectionSnapshot) -> tuple[str, dict[str, str]]:
    key = snapshot.api_key
    if api_key_charset_error(key):
        raise ValueError("Invalid model-list API key charset")
    headers: dict[str, str] = {}
    for mapping in (snapshot.http_headers, snapshot.extra_headers):
        for name, value in mapping.items():
            value = resolve_env_templates(
                value,
                location="model-list header",
                allow_commands=False,
            )
            if header_name_charset_error(name) or header_value_charset_error(name, value):
                raise ValueError("Invalid model-list header charset")
            headers[name.casefold()] = value
    return key, headers


def _auth_groups(provider: str, key: str, headers: dict[str, str]) -> list[dict[str, str]]:
    if not key or {"authorization", "x-api-key", "api-key"}.intersection(headers):
        return [headers]
    bearer = {"authorization": f"Bearer {key}"}
    anthropic = {"x-api-key": key, "anthropic-version": "2023-06-01"}
    groups = (anthropic, bearer) if provider == "anthropic" else (bearer, anthropic)
    return [group | headers for group in groups]


def _origin(url: httpx.URL) -> tuple[str, str, int | None]:
    return url.scheme, url.host, url.port


@dataclass(frozen=True, slots=True)
class _Location:
    url: httpx.URL
    preset: ModelListPreset | None = None
    require_model_marker: bool = False
    require_item_marker: bool = False
    count_auth: bool = True
    url_corrections: tuple[UrlCorrection, ...] = ()
    corrected_base_url: str | None = None


def _append_path(base: httpx.URL, suffix: bytes) -> httpx.URL:
    path, separator, query = base.raw_path.partition(b"?")
    return base.copy_with(raw_path=path.rstrip(b"/") + suffix + separator + query, fragment=None)


def _locations(base: httpx.URL, provider: str, *, missing_scheme: bool = False) -> list[_Location]:
    suffix = b"/v1/models" if provider == "anthropic" else b"/models"
    locations: list[_Location] = []
    path, separator, query = base.raw_path.partition(b"?")
    path = path.rstrip(b"/")
    endpoint = next((suffix for suffix in _ENDPOINT_PATHS if path.endswith(suffix)), None)
    if not missing_scheme:
        conventional = _append_path(base, suffix)
        preset = match_preset(str(base))
        if preset is not None:
            if preset.list_path:
                # A preset may replace the path, never the configured origin.
                if not preset.list_path.startswith("/") or preset.list_path.startswith("//"):
                    raise ValueError("Model-list preset must use a relative absolute path")
                url = base.copy_with(path=preset.list_path, query=None, fragment=None)
            else:
                url = conventional
            if preset.query:
                url = url.copy_with(query=preset.query.encode("ascii"))
            locations.append(_Location(url, preset))
        if not locations or locations[0].url != conventional:
            locations.append(_Location(conventional))
        locations.append(_Location(base, require_model_marker=True))

        has_v1 = path.endswith(b"/v1")
        parent_path = path[:-3] if has_v1 else path
        if parent_path.rstrip(b"/"):
            for root_path in ("/v1/models", "/models"):
                url = base.copy_with(path=root_path, query=None, fragment=None)
                locations.append(_Location(url, require_item_marker=True, count_auth=False))
    paths: list[tuple[httpx.URL, tuple[UrlCorrection, ...]]] = [(base, ())]
    if endpoint is not None:
        stripped = path[: -len(endpoint)]
        if provider == "anthropic" and stripped.endswith(b"/v1"):
            stripped = stripped[:-3]
        paths.append((base.copy_with(raw_path=stripped + separator + query, fragment=None), ("removed_endpoint",)))
    # Expand each base once; this bounds combinations rather than recursively toggling /v1.
    variants: list[tuple[httpx.URL, tuple[UrlCorrection, ...]]] = []
    for candidate, corrections in paths:
        variants.append((candidate, corrections))
        candidate_path, sep, candidate_query = candidate.raw_path.partition(b"?")
        candidate_path = candidate_path.rstrip(b"/")
        if candidate_path.endswith(b"/v1"):
            variants.append(
                (
                    candidate.copy_with(raw_path=candidate_path[:-3] + sep + candidate_query, fragment=None),
                    (*corrections, "removed_v1"),
                )
            )
        elif provider != "anthropic":
            variants.append((_append_path(candidate, b"/v1"), (*corrections, "added_v1")))

    schemes: list[tuple[str, tuple[UrlCorrection, ...]]] = [(base.scheme, ("added_https",) if missing_scheme else ())]
    if base.scheme == "http":
        schemes.append(("https", ("switched_https",)))
    else:
        try:
            loopback = ipaddress.ip_address(base.host).is_loopback
        except ValueError:
            loopback = base.host.casefold() == "localhost"
        if loopback:
            schemes.append(("http", (*schemes[0][1], "switched_http")))
    for scheme, scheme_corrections in schemes:
        for candidate, path_corrections in variants:
            corrections = (*scheme_corrections, *path_corrections)
            if not corrections:
                continue
            corrected = candidate.copy_with(scheme=scheme, fragment=None)
            locations.append(
                _Location(
                    _append_path(corrected, suffix),
                    require_item_marker=True,
                    count_auth=False,
                    url_corrections=corrections,
                    corrected_base_url=str(corrected),
                )
            )
    if endpoint is not None:
        # Reuse an overlapping original probe as strict evidence for the corrected base.
        # In particular, a pasted /models URL must not survive as the conversation base.
        corrections_by_url: dict[httpx.URL, _Location] = {}
        for location in locations:
            if location.corrected_base_url is not None:
                corrections_by_url.setdefault(location.url, location)
        locations = [
            replace(
                location,
                require_item_marker=True,
                corrected_base_url=correction.corrected_base_url,
                url_corrections=correction.url_corrections,
            )
            if location.corrected_base_url is None and (correction := corrections_by_url.get(location.url)) is not None
            else location
            for location in locations
        ]
    return locations


@dataclass(slots=True)
class _Budget:
    deadline: float
    requests: int = 0
    # The entire discovery may follow only one same-origin redirect.
    redirected: bool = False
    attempted_urls: set[httpx.URL] = field(default_factory=set)
    pending_urls: set[httpx.URL] = field(default_factory=set)
    responded_urls: set[httpx.URL] = field(default_factory=set)
    auth_rejected: bool = False
    url_corrections: tuple[UrlCorrection, ...] = ()
    corrected_base_url: str | None = None
    partial_models: dict[str, ModelInfo] = field(default_factory=dict)
    attempted_requests: set[tuple[httpx.URL, tuple[tuple[str, str], ...]]] = field(default_factory=set)


async def _read_page(
    client: httpx.AsyncClient,
    location: _Location,
    headers: dict[str, str],
    snapshot: ConnectionSnapshot,
    budget: _Budget,
) -> tuple[int, Any, httpx.URL]:
    import httpx

    url = location.url
    while True:
        normalized = url.copy_with(fragment=None)
        request = (normalized, tuple(headers.items()))
        if request not in budget.attempted_requests:
            if budget.requests <= 0:
                return 0, None, url
            budget.requests -= 1
            budget.attempted_requests.add(request)
        budget.attempted_urls.add(normalized)
        if normalized not in budget.responded_urls:
            budget.pending_urls.add(normalized)
        status = 0
        try:
            remaining = min(_REQUEST_SECONDS, budget.deadline - asyncio.get_running_loop().time())
            if remaining <= 0:
                raise TimeoutError
            timeout = httpx.Timeout(remaining, connect=min(snapshot.connect_timeout, remaining))
            async with (
                asyncio.timeout(remaining),
                client.stream("GET", url, headers=headers, timeout=timeout, auth=None) as response,
            ):
                status = response.status_code
                budget.pending_urls.discard(normalized)
                budget.responded_urls.add(normalized)
                if location.count_auth and status in {401, 403}:
                    budget.auth_rejected = True
                if response.is_redirect:
                    redirect = response.headers.get("location")
                    if redirect and not budget.redirected:
                        target = response.url.join(redirect)
                        if (
                            _origin(target) == _origin(url)
                            and target.copy_with(fragment=None) not in budget.attempted_urls
                        ):
                            budget.redirected = True
                            url = target
                            continue
                    return status, None, url
                if not response.is_success:
                    return status, None, url
                size = response.headers.get("content-length")
                if size and int(size) > _MAX_RESPONSE_BYTES:
                    return status, None, url
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(body) + len(chunk) > _MAX_RESPONSE_BYTES:
                        return status, None, url
                    body.extend(chunk)
                try:
                    return status, json.loads(body), url
                except ValueError:
                    return status, None, url
        except httpx.TransportError, TimeoutError:
            # A response header settles this address even if reading its body fails.
            return status, None, url


def _nested(item: dict[str, Any], key: str, field_name: str) -> Any:
    value = item.get(key)
    return value.get(field_name) if isinstance(value, dict) else None


def _modalities(item: Any) -> tuple[str, ...] | None:
    if not isinstance(item, dict):
        return None
    value = item.get("output_modalities", _nested(item, "architecture", "output_modalities"))
    if not isinstance(value, list) or not all(isinstance(mode, str) for mode in value):
        return None
    return tuple(value)


def _parse_models(
    payload: Any,
    preset: ModelListPreset | None,
    *,
    require_model_marker: bool = False,
    require_item_marker: bool = False,
) -> list[ModelInfo]:
    if isinstance(payload, dict):
        if "error" in payload or payload.get("success") is False:
            return []
        items = payload.get(preset.list_field if preset and preset.list_field else "data")
    else:
        items = payload
    if not isinstance(items, list):
        return []
    items = [item for item in items[:_MAX_ITEMS] if (modes := _modalities(item)) is None or "text" in modes]
    if require_item_marker or (
        require_model_marker and not (isinstance(payload, dict) and payload.get("object") == "list")
    ):
        marked = sum(
            isinstance(item, dict)
            and (item.get("object") == "model" or item.get("type") == "model" or "owned_by" in item)
            for item in items
        )
        if marked * 2 < len(items):
            return []
    models: dict[str, ModelInfo] = {}
    identified = 0
    for item in items:
        if isinstance(item, str):
            model_id = item
        elif isinstance(item, dict):
            names = (preset.id_field,) if preset and preset.id_field else ("id", "model", "model_id")
            model_id = next((item[name] for name in names if isinstance(item.get(name), str) and item[name]), "")
        else:
            continue
        # The ratio checks list shape; only validated IDs become choices.
        if model_id:
            identified += 1
        # Cheap checks first: past the cap or on a repeat, only the ratio count above needs the item.
        if len(models) >= _MAX_MODELS or model_id in models:
            continue
        if len(model_id) > 256 or model_id_charset_error(model_id):
            continue
        models[model_id] = ModelInfo(model_id)
    return list(models.values()) if identified * 2 >= len(items) else []


async def _discover(
    snapshot: ConnectionSnapshot,
    transport: httpx.AsyncBaseTransport | None,
    budget: _Budget,
    on_retry: Callable[[int, int], None] | None,
) -> list[ModelInfo]:
    import httpx

    if snapshot.provider not in PROVIDERS or has_command_placeholder(snapshot):
        return []
    snapshot = resolve_model_connection(snapshot)
    base_url = snapshot.base_url
    if not base_url:
        return []
    missing_scheme = "://" not in base_url
    base = httpx.URL("https://" + base_url if missing_scheme else base_url)
    if base.scheme not in {"http", "https"} or not base.host or any(c.isspace() for c in unquote(base.host)):
        return []
    key, headers = _credentials(snapshot)
    groups = _auth_groups(snapshot.provider, key, headers)
    locations = _locations(base, snapshot.provider, missing_scheme=missing_scheme)
    if snapshot.provider == "anthropic":
        locations = [replace(location, url=location.url.copy_merge_params({"limit": "1000"})) for location in locations]
    budget.pending_urls = {location.url.copy_with(fragment=None) for location in locations}
    budget.requests = len(budget.pending_urls) * len(groups) + 1 + (_MAX_PAGES - 1)
    profile = ModelProfile(
        id="",
        name="",
        provider=snapshot.provider,
        verify_ssl=snapshot.verify_ssl,
        bypass_proxy=snapshot.bypass_proxy,
        http_connect_timeout=snapshot.connect_timeout,
    )
    # The client factory imports the provider SDK; load it off the event loop first, as create_client does.
    await import_off_loop(*_SDK_MODULES[PROVIDERS[snapshot.provider].sdk])
    client = _build_profile_http_client(
        profile,
        httpx.Timeout(_REQUEST_SECONDS),
        transport=transport,
        follow_redirects=False,
        enable_hooks=False,
    )
    close = OnceClose(client.aclose)
    try:
        candidates = [(location, groups) for location in locations]
        for round_number in range(1, _TOTAL_ROUNDS + 1):
            if round_number > 1:
                pause = min(_RETRY_BASE_SECONDS * 2 ** (round_number - 2), _RETRY_MAX_PAUSE_SECONDS)
                if budget.deadline - asyncio.get_running_loop().time() <= pause:
                    break
                await asyncio.sleep(pause)
                if budget.deadline - asyncio.get_running_loop().time() < _MIN_PAGE_SECONDS:
                    break
                if on_retry is not None:
                    on_retry(round_number, _TOTAL_ROUNDS)
            unanswered: list[tuple[_Location, list[dict[str, str]]]] = []
            for location, auth_groups in candidates:
                url = location.url
                if round_number == 1 and url.copy_with(fragment=None) in budget.attempted_urls:
                    continue
                for group in auth_groups:
                    try:
                        status, payload, page_url = await _read_page(
                            client, replace(location, url=url), group, snapshot, budget
                        )
                    except httpx.HTTPError, ValueError:
                        break
                    if status == 0:
                        # Keep the failed target, its authentication and its correction evidence.
                        unanswered.append((replace(location, url=page_url), [group]))
                        url = page_url
                        continue
                    if status in {401, 403}:
                        url = page_url
                        continue
                    models = _parse_models(
                        payload,
                        location.preset,
                        require_model_marker=location.require_model_marker,
                        require_item_marker=location.require_item_marker,
                    )
                    if models:
                        if location.corrected_base_url is not None:
                            budget.url_corrections = location.url_corrections
                            budget.corrected_base_url = location.corrected_base_url
                        return await _pages(
                            client,
                            replace(location, url=page_url),
                            group,
                            snapshot,
                            budget,
                            payload,
                            models,
                        )
                    break
            if not unanswered:
                break
            candidates = unanswered
        return []
    finally:
        await close()


async def _pages(
    client: httpx.AsyncClient,
    location: _Location,
    headers: dict[str, str],
    snapshot: ConnectionSnapshot,
    budget: _Budget,
    payload: Any,
    models: list[ModelInfo],
) -> list[ModelInfo]:
    """Keep a successful first page, bounding Anthropic cursors and total entries."""
    import httpx

    url = location.url
    seen: set[str] = set()
    found = {model.id: model for model in models}
    budget.partial_models = found
    for _ in range(_MAX_PAGES - 1):
        if snapshot.provider != "anthropic" or not isinstance(payload, dict) or payload.get("has_more") is not True:
            break
        cursor = payload.get("last_id")
        if not isinstance(cursor, str) or not cursor or cursor in seen or len(found) >= _MAX_MODELS:
            break
        # Leave enough time to close the pool instead of starting a doomed page.
        if budget.deadline - asyncio.get_running_loop().time() < _MIN_PAGE_SECONDS:
            break
        seen.add(cursor)
        url = url.copy_merge_params({"after_id": cursor, "limit": "1000"})
        try:
            status, payload, url = await _read_page(client, replace(location, url=url), headers, snapshot, budget)
        except httpx.HTTPError, ValueError, TimeoutError:
            break
        more = (
            _parse_models(
                payload,
                location.preset,
                require_model_marker=location.require_model_marker,
                require_item_marker=location.require_item_marker,
            )
            if 200 <= status < 300
            else []
        )
        if not more:
            break
        for model in more:
            if len(found) >= _MAX_MODELS:
                break
            found.setdefault(model.id, model)
    return sorted(found.values(), key=lambda model: reading_order(model.id))


async def discover_models(
    snapshot: ConnectionSnapshot,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    on_retry: Callable[[int, int], None] | None = None,
) -> ModelDiscovery:
    """Return bounded discovery facts; failures never interrupt form editing.

    *on_retry* gets (round, total_rounds) as retry rounds 2 through 5 begin.
    """
    budget = _Budget(0.0)
    timed_out = False
    try:
        async with asyncio.timeout(_TOTAL_SECONDS):
            budget.deadline = asyncio.get_running_loop().time() + _TOTAL_SECONDS
            models = await _discover(snapshot, transport, budget, on_retry)
    except TimeoutError:
        timed_out = True
        models = sorted(budget.partial_models.values(), key=lambda model: reading_order(model.id))
    except Exception:
        models = []
    return ModelDiscovery(
        models,
        auth_rejected=not models and budget.auth_rejected,
        url_corrections=budget.url_corrections if models else (),
        corrected_base_url=budget.corrected_base_url if models else None,
        unreachable=not models and bool(budget.pending_urls) and (bool(budget.attempted_urls) or timed_out),
    )
