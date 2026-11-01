# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Web settings values: JSON-list text rows and their parsers.

The coercers and the web tool build share the parsers below, so a value the
settings layer accepts is exactly a value the web tools can use.
"""

from __future__ import annotations

import json
import re

from chrys.foundation.config.coercion import (
    MISSING,
    Coerced,
    Coercer,
    CoerceReason,
    CoerceStatus,
    choice_coercer,
    invalid,
)
from chrys.foundation.net.url import normalize_origin, normalize_url

ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


def _json_list(raw: object) -> list[object]:
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except RecursionError as err:
        raise ValueError("The JSON value is nested too deeply") from err
    if not isinstance(value, list):
        raise ValueError("Expected a JSON array")
    return value


def parse_origins(raw: object) -> frozenset[str]:
    """Normalize a list of exact http(s) origins; raise ValueError on any bad entry."""
    origins: set[str] = set()
    for item in _json_list(raw):
        if not isinstance(item, str):
            raise ValueError("Web origin grants must be strings")
        origins.add(normalize_origin(item))
    return frozenset(origins)


def parse_custom_endpoint_grants(raw: object) -> dict[str, frozenset[str]]:
    """Map each normalized endpoint URL to its granted credential names."""
    grants: dict[str, frozenset[str]] = {}
    for entry in _json_list(raw):
        if not isinstance(entry, dict) or entry.keys() - {"url", "credential_env_names"} or "url" not in entry:
            raise ValueError("A custom endpoint grant needs a url and optional credential_env_names")
        if not isinstance(entry["url"], str):
            raise ValueError("A custom endpoint url must be a string")
        url = normalize_url(entry["url"], endpoint=True)
        if url in grants:
            raise ValueError(f"Duplicate custom endpoint grant {url}")
        names = entry.get("credential_env_names", [])
        if not isinstance(names, list) or any(not isinstance(n, str) or ENV_NAME.fullmatch(n) is None for n in names):
            raise ValueError(f"Invalid credential_env_names for {url}")
        grants[url] = frozenset(names)
    return grants


def web_mode_coercer(*, on: str) -> Coercer:
    """Pick ``off`` or *on*, reading a YAML boolean as the matching mode.

    ``settings.yaml`` is read as YAML 1.1, where a bare ``off`` or ``on`` is a
    boolean; the agent profile loader already reads them this way.
    """
    choose = choice_coercer(choices=("off", on))

    def coerce(raw: object) -> Coerced:
        if isinstance(raw, bool):
            raw = on if raw else "off"
        return choose(raw)

    return coerce


def web_list_coercer(raw: object) -> Coerced:
    """Keep list settings editable by the existing text row without hidden state.

    A blank field is the empty list, which is also every list's default.
    """
    if raw is None:
        return MISSING
    if isinstance(raw, str) and not raw.strip():
        return Coerced(CoerceStatus.VALID, value="[]")
    try:
        value = _json_list(raw)
        return Coerced(CoerceStatus.VALID, value=json.dumps(value, ensure_ascii=False, allow_nan=False))
    except ValueError, TypeError, RecursionError:
        return invalid(raw, CoerceReason.EXPECTED_JSON_ARRAY)


def web_proxy_url_coercer(raw: object) -> Coerced:
    """Accept only a bare http(s) proxy origin, without credentials or a path."""
    if raw is None:
        return MISSING
    if not isinstance(raw, str):
        return invalid(raw, CoerceReason.EXPECTED_TEXT)
    value = raw.strip()
    if not value:
        return Coerced(CoerceStatus.VALID, value="")
    try:
        normalize_origin(value)
    except ValueError:
        return invalid(raw, CoerceReason.EXPECTED_WEB_ORIGIN)
    return Coerced(CoerceStatus.VALID, value=value)


def web_origins_coercer(raw: object) -> Coerced:
    """Each entry must be a bare http(s) origin; paths and credentials belong to no policy list."""
    coerced = web_list_coercer(raw)
    if coerced.status != CoerceStatus.VALID:
        return coerced
    try:
        parse_origins(coerced.value)
    except ValueError:
        return invalid(raw, CoerceReason.EXPECTED_WEB_ORIGIN)
    return coerced


def web_custom_endpoints_coercer(raw: object) -> Coerced:
    """Grant entries carry one endpoint URL plus optional credential environment names."""
    coerced = web_list_coercer(raw)
    if coerced.status != CoerceStatus.VALID:
        return coerced
    try:
        parse_custom_endpoint_grants(coerced.value)
    except ValueError:
        return invalid(raw, CoerceReason.EXPECTED_ENDPOINT_GRANT)
    return coerced
