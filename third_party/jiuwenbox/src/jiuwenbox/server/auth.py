# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Bearer token authentication for the jiuwenbox HTTP API.

Every request needs ``Authorization: Bearer <token>``. A missing token is
rejected.
"""

from __future__ import annotations

import hmac
import os

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

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


class BearerTokenAuthMiddleware(BaseHTTPMiddleware):
    """Require ``Authorization: Bearer <token>`` on every request.

    A missing token is rejected. An empty or whitespace-only configured token
    is rejected the same way as a wrong bearer.
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        expected = get_configured_token()
        provided = extract_bearer_token(request.headers.get("Authorization"))
        if expected is not None and token_is_valid(provided, expected):
            return await call_next(request)
        return JSONResponse(
            status_code=401,
            content={"error": "unauthorized"},
            headers={"WWW-Authenticate": "Bearer"},
        )
