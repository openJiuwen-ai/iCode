# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Model discovery for an unsaved ACP connection form."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from typing import Any
from urllib.parse import unquote

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from pydantic.alias_generators import to_camel

from chrys.app.acp.errors import invalid_params
from chrys.foundation.util.env_templates import has_command_placeholder, resolve_env_templates
from chrys.foundation.util.header_charset import (
    api_key_charset_error,
    header_name_charset_error,
    header_value_charset_error,
)
from chrys.service.llm.model_list import (
    ConnectionSnapshot,
    discover_models,
    model_list_base_url,
)
from chrys.service.llm.model_list import (
    has_command_placeholder as has_credential_command,
)
from chrys.service.llm.providers import PROVIDERS
from chrys.service.profiles.models.schema import ModelProfile


class _ConnectionForm(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, strict=True, extra="forbid")
    provider: str
    base_url: str = ""
    api_key: str = ""
    http_headers: dict[str, str] = Field(default_factory=dict)
    extra_headers: dict[str, str] = Field(default_factory=dict)
    verify_ssl: bool = True
    bypass_proxy: bool = False
    connect_timeout: float = Field(default=10.0, gt=0, allow_inf_nan=False)


def _target(address: str) -> httpx.URL:
    try:
        url = httpx.URL(address if "://" in address else "https://" + address)
        if (
            url.scheme in {"http", "https"}
            and (url.port is None or 1 <= url.port <= 65535)
            and url.host
            and not url.userinfo
            and not any(char.isspace() for char in unquote(url.host))
        ):
            path, sep, query = url.raw_path.partition(b"?")
            return url.copy_with(raw_path=(path.rstrip(b"/") or b"/") + sep + query, fragment=None)
    except ValueError, httpx.InvalidURL:
        pass
    raise invalid_params({"details": "baseUrl must be an HTTP(S) address without embedded credentials."})


def _draft_key(
    form: _ConnectionForm, target: httpx.URL, profile_id: str | None, get_profile: Callable[[str], ModelProfile]
) -> str:
    key = form.api_key.strip()
    if key != "***":
        return key
    if not profile_id:
        raise invalid_params({"details": "A masked apiKey requires a saved profileId."})
    profile = get_profile(profile_id)
    if (
        profile.provider != form.provider
        or target != _target(model_list_base_url(profile.provider, profile.base_url))
        or (profile.verify_ssl and not form.verify_ssl)
        or (profile.bypass_proxy and not form.bypass_proxy)
        or any(name.lower() == "host" for name in (*form.http_headers, *form.extra_headers))
        or not profile.api_key.strip()
    ):
        raise invalid_params({"details": "Re-enter the API key for this connection."})
    if has_command_placeholder(profile.api_key):
        raise invalid_params({"details": "Credential commands are unavailable for model discovery."})
    try:
        return resolve_env_templates(profile.api_key.strip(), location="saved API key", allow_commands=False)
    except ValueError:
        raise invalid_params({"details": "The saved API key could not be resolved for discovery."}) from None


async def list_provider_models(
    connection: dict[str, Any], profile_id: str | None, get_profile: Callable[[str], ModelProfile]
) -> dict[str, object]:
    try:
        form = _ConnectionForm.model_validate(connection)
    except ValidationError as exc:
        # ValidationError's formatted message includes the input (possibly an API key).
        field = exc.errors(include_input=False)[0]["loc"][0]
        raise invalid_params({"details": f"Invalid connection field: {field}."}) from None
    if form.provider not in PROVIDERS:
        raise invalid_params({"details": "Unsupported model discovery provider."})
    address = model_list_base_url(form.provider, form.base_url)
    target = _target(address)
    snapshot = ConnectionSnapshot(**form.model_dump())
    if has_credential_command(snapshot):
        raise invalid_params({"details": "Credential commands are unavailable for model discovery."})
    key = _draft_key(form, target, profile_id, get_profile)
    if api_key_charset_error(key):
        raise invalid_params({"details": "Invalid API key characters."})
    for headers in (form.http_headers, form.extra_headers):
        for name, value in headers.items():
            if header_name_charset_error(name) or header_value_charset_error(name, value):
                raise invalid_params({"details": "Invalid HTTP header name or value."})
    result = await discover_models(replace(snapshot, base_url=address, api_key=key), literal_connection=True)
    return {
        "models": [{"id": model.id} for model in result.models],
        "authRejected": result.auth_rejected,
        "unreachable": result.unreachable,
        "correctedBaseUrl": result.corrected_base_url,
        "urlCorrections": list(result.url_corrections),
    }
