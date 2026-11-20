# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Short-lived HTTP and Unix-socket client for one sandbox call.

Each call opens an ``httpx.AsyncClient`` and closes it before returning.
Error text raised here is written by this package. Transport exceptions are
not stringified: their text can repeat a URL userinfo or a bearer token.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import urlsplit

import httpx

from chrys.service.sandbox.backend import SandboxError


def _new_client(**kwargs: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(**kwargs)


@asynccontextmanager
async def open_client(
    base_url: str,
    token: str,
    timeout_seconds: float,
    *,
    trust_env: bool = True,
) -> AsyncGenerator[httpx.AsyncClient]:
    """Yield a client for ``base_url``. The caller closes it by leaving the block.

    ``unix:///absolute/path`` uses an UDS transport and a placeholder HTTP
    base URL (the host is not dialed). Any other ``http`` or ``https`` URL
    is used as given. A non-empty token is sent as ``Authorization: Bearer``.

    ``trust_env`` is passed to the HTTP client. It defaults to True, so a
    configured ``sandbox.url`` keeps the process proxy environment. Pass
    False only for the server this process started, so that client does not
    send the bearer token through ``HTTP_PROXY``.
    """
    parsed = urlsplit(base_url)
    scheme = parsed.scheme.lower()
    kwargs: dict[str, Any] = {
        "timeout": httpx.Timeout(timeout_seconds),
        "headers": _authorization(token),
        "trust_env": trust_env,
    }
    if scheme == "unix":
        if parsed.netloc or not parsed.path.startswith("/"):
            raise SandboxError("Sandbox unix URL must be unix:///absolute/path.")
        if parsed.path == "/" or parsed.path.endswith("/"):
            raise SandboxError("Sandbox unix URL must name a socket file.")
        # The host is not contacted; the supervisor owns the placeholder name.
        from chrys.service.sandbox.supervisor import uds_http_base

        kwargs["base_url"] = uds_http_base()
        kwargs["transport"] = httpx.AsyncHTTPTransport(uds=parsed.path)
    elif scheme in {"http", "https"}:
        if not parsed.netloc:
            raise SandboxError("Sandbox URL is missing a host.")
        kwargs["base_url"] = base_url
    else:
        raise SandboxError("Sandbox URL scheme is not supported.")
    async with _new_client(**kwargs) as client:
        yield client


@asynccontextmanager
async def connect(
    base_url: str,
    token: str,
    timeout_seconds: float,
    *,
    trust_env: bool = True,
) -> AsyncGenerator[httpx.AsyncClient]:
    """Like ``open_client``, but transport failures become ``SandboxError``.

    ``trust_env`` is forwarded unchanged and defaults to True.
    """
    try:
        async with open_client(base_url, token, timeout_seconds, trust_env=trust_env) as client:
            yield client
    except SandboxError:
        raise
    except httpx.TimeoutException:
        raise SandboxError("Sandbox request timed out.") from None
    except httpx.HTTPError, OSError:
        raise SandboxError("Sandbox server is unreachable.") from None


def _authorization(token: str) -> dict[str, str]:
    if not token:
        return {}
    return {"Authorization": f"Bearer {token}"}
