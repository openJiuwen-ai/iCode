# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Endpoint resolution for the AIxCoding auth services.

Auth and user data are two distinct services behind the same host, differing
only in path prefix. Three deployment tiers ship as constants:

=========  ========  ==============================================
Tier       authUrl   dataUrl
=========  ========  ==============================================
LOCAL      :7777     the bundled mock (``aixcoding/mock/auth/server.py``)
DEV        81.89.182.150  ``/csas/api/v1`` vs ``/aicoding/api/v1``
PROD       82.187.34.98   ``/csas/api/v1`` vs ``/aicoding/api/v1``
=========  ========  ==============================================

Resolution order (first match wins):

1. ``AIXCODING_AUTH_SERVER_URL`` -- replaces protocol + host of **both** URLs,
   keeping each path prefix (a port may be included). Invalid values are
   ignored, never fatal.
2. The tier selected by ``AIXCODING_ENVIRONMENT`` (``local``/``dev``/``prod``;
   unknown values fall back to ``prod``). The app startup composition may inject
   the shared TOML-backed tier before the first login session is constructed.

The environment variable names carry the ``AIXCODING_`` prefix of the
customization layer (ADR-0009); ``bootstrap_runtime`` freezes the process
environment before the TUI starts, so reading ``os.environ`` at call time is
stable.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from urllib.parse import urlsplit

from .types import Environment

_BASE_PATHS: dict[Environment, tuple[str, str]] = {
    Environment.LOCAL: ("http://localhost:7777/api/v1", "http://localhost:7777/api/v1"),
    Environment.DEV: ("http://81.89.182.150/csas/api/v1", "http://81.89.182.150/aicoding/api/v1"),
    Environment.PROD: ("http://82.187.34.98/csas/api/v1", "http://82.187.34.98/aicoding/api/v1"),
}

ENVIRONMENT_VARIABLE = "AIXCODING_ENVIRONMENT"
SERVER_URL_VARIABLE = "AIXCODING_AUTH_SERVER_URL"
_configured_environment: Environment | None = None


def configure_environment(raw: str | None) -> None:
    """Inject the app-composed default; ``None`` resets it for test isolation."""
    global _configured_environment
    _configured_environment = Environment(raw) if raw is not None else None


def resolve_environment(environ: Mapping[str, str] | None = None) -> Environment:
    """Read the tier, preferring app injection only on the production path."""
    if environ is None and _configured_environment is not None:
        return _configured_environment
    env = os.environ if environ is None else environ
    raw = (env.get(ENVIRONMENT_VARIABLE) or "").strip().lower()
    try:
        return Environment(raw)
    except ValueError:
        return Environment.PROD


def _replace_origin(url: str, origin: str) -> str:
    """Swap protocol+host of ``url`` for ``origin``, keeping path and query."""
    split = urlsplit(url)
    replacement = urlsplit(origin)
    if not replacement.scheme or not replacement.netloc:
        return url
    return f"{replacement.scheme}://{replacement.netloc}{split.path}" + (f"?{split.query}" if split.query else "")


def resolve_endpoints(
    environment: Environment | None = None,
    environ: Mapping[str, str] | None = None,
) -> tuple[str, str]:
    """Return ``(auth_url, data_url)`` for the resolved tier.

    ``AIXCODING_AUTH_SERVER_URL`` overrides both origins when it parses as a bare
    ``scheme://host[:port]``; anything else is treated as unset.
    """
    env = os.environ if environ is None else environ
    tier = environment or resolve_environment(environ)
    auth_url, data_url = _BASE_PATHS[tier]
    override = (env.get(SERVER_URL_VARIABLE) or "").strip()
    if override:
        candidate = urlsplit(override if "//" in override else f"//{override}", scheme="http")
        # ``urlsplit`` accepts absurd netlocs ("not a url"); demand at least a
        # plausible host: non-empty and free of whitespace.
        if candidate.scheme and candidate.netloc and not any(c.isspace() for c in candidate.netloc):
            origin = f"{candidate.scheme}://{candidate.netloc}"
            auth_url = _replace_origin(auth_url, origin)
            data_url = _replace_origin(data_url, origin)
    return auth_url, data_url
