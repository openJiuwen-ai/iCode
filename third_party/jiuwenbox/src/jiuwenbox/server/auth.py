# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Bearer token authentication for the jiuwenbox HTTP API.

Every request needs ``Authorization: Bearer <token>``. A missing token is
rejected.
"""

from __future__ import annotations

import hmac
import os

from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

_ENV_API_TOKEN_NAME = "JIUWENBOX_API_TOKEN"


def get_configured_token() -> str | None:
    """Return the configured API token, or ``None`` when it is missing or blank."""
    raw = os.environ.get(_ENV_API_TOKEN_NAME)
    if raw is None:
        return None
    token = raw.strip()
    return token or None


def extract_bearer_token(authorization: str | None) -> str | None:
    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None
    stripped = token.strip()
    return stripped or None


def token_is_valid(provided: str | None, expected: str) -> bool:
    if provided is None:
        return False
    return hmac.compare_digest(
        provided.encode("utf-8"),
        expected.encode("utf-8"),
    )


class BearerTokenAuthMiddleware:
    """Require ``Authorization: Bearer <token>`` on every HTTP request.

    A missing token is rejected. An empty or whitespace-only configured token
    is rejected the same way as a wrong bearer. Non-HTTP scopes (lifespan)
    pass through untouched.

    Plain ASGI on purpose: ``BaseHTTPMiddleware`` hands the route a wrapped
    ``receive`` on which ``Request.is_disconnected()`` never reports the
    client's ``http.disconnect``, and the exec route cancels the command
    only through that signal.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        expected = get_configured_token()
        provided = extract_bearer_token(Headers(scope=scope).get("Authorization"))
        if expected is not None and token_is_valid(provided, expected):
            await self.app(scope, receive, send)
            return
        response = JSONResponse(
            status_code=401,
            content={"error": "unauthorized"},
            headers={"WWW-Authenticate": "Bearer"},
        )
        await response(scope, receive, send)
