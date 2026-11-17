# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Bearer authentication fails closed when the API token is missing.

Every request needs ``Authorization: Bearer <token>``. A missing, blank, or
wrong token is rejected with 401. A matching bearer reaches the route.
"""

from __future__ import annotations

import httpx
import pytest
from jiuwenbox.server.auth import BearerTokenAuthMiddleware
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route

_API_TOKEN_ENV = "JIUWENBOX_API_TOKEN"


async def _ok(_request: object) -> PlainTextResponse:
    return PlainTextResponse("ok")


def _application() -> Starlette:
    application = Starlette(routes=[Route("/", _ok)])
    application.add_middleware(BearerTokenAuthMiddleware)
    return application


async def _get(headers: dict[str, str] | None = None) -> httpx.Response:
    transport = httpx.ASGITransport(app=_application())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get("/", headers=headers)


def _assert_unauthorized(response: httpx.Response) -> None:
    assert response.status_code == 401
    assert response.json() == {"error": "unauthorized"}
    assert response.headers["WWW-Authenticate"] == "Bearer"


async def test_unset_token_is_unauthorized(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_API_TOKEN_ENV, "placeholder")
    monkeypatch.delenv(_API_TOKEN_ENV)
    _assert_unauthorized(await _get())


async def test_whitespace_token_is_unauthorized(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_API_TOKEN_ENV, "   ")
    _assert_unauthorized(await _get())


async def test_missing_authorization_header_is_unauthorized(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_API_TOKEN_ENV, "sekret")
    _assert_unauthorized(await _get())


async def test_correct_bearer_reaches_the_route(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_API_TOKEN_ENV, "sekret")
    response = await _get({"Authorization": "Bearer sekret"})
    assert response.status_code == 200
    assert response.text == "ok"


async def test_wrong_bearer_is_unauthorized(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_API_TOKEN_ENV, "sekret")
    _assert_unauthorized(await _get({"Authorization": "Bearer wrong"}))


async def test_authorized_route_sees_the_client_disconnect(monkeypatch: pytest.MonkeyPatch) -> None:
    """The exec route cancels its command only when this reports True."""
    monkeypatch.setenv(_API_TOKEN_ENV, "sekret")
    seen: list[bool] = []

    async def _probe(request: Request) -> PlainTextResponse:
        seen.append(await request.is_disconnected())
        return PlainTextResponse("ok")

    application = Starlette(routes=[Route("/", _probe)])
    application.add_middleware(BearerTokenAuthMiddleware)
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/",
        "raw_path": b"/",
        "query_string": b"",
        "root_path": "",
        "headers": [(b"host", b"test"), (b"authorization", b"Bearer sekret")],
        "client": ("127.0.0.1", 1),
        "server": ("test", 80),
    }

    async def _receive() -> dict[str, object]:
        return {"type": "http.disconnect"}

    async def _send(_message: object) -> None:
        return None

    await application(scope, _receive, _send)
    assert seen == [True]


async def test_lifespan_passes_through_without_a_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_API_TOKEN_ENV, "placeholder")
    monkeypatch.delenv(_API_TOKEN_ENV)
    reached: list[str] = []

    async def _inner(scope: dict[str, object], _receive: object, _send: object) -> None:
        reached.append(str(scope["type"]))

    async def _never_called() -> dict[str, object]:
        raise AssertionError("receive was not expected")

    async def _no_send(_message: object) -> None:
        raise AssertionError("send was not expected")

    await BearerTokenAuthMiddleware(_inner)({"type": "lifespan"}, _never_called, _no_send)
    assert reached == ["lifespan"]
