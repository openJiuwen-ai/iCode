# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""HTTP and UDS client behavior, with no listening socket."""

from __future__ import annotations

from collections.abc import Callable
from types import TracebackType
from typing import Any

import httpx
import pytest

from chrys.service.sandbox import client
from chrys.service.sandbox.backend import SandboxError
from chrys.service.sandbox.supervisor import uds_http_base


def _client_with(monkeypatch: pytest.MonkeyPatch, handler) -> None:
    def new_client(**kwargs: Any) -> httpx.AsyncClient:
        built = dict(kwargs)
        built["transport"] = httpx.MockTransport(handler)
        return httpx.AsyncClient(**built)

    monkeypatch.setattr(client, "_new_client", new_client)


async def test_http_url_does_not_build_a_unix_transport(monkeypatch: pytest.MonkeyPatch):
    seen: dict[str, object] = {}

    def new_client(**kwargs: Any) -> httpx.AsyncClient:
        seen["base_url"] = kwargs.get("base_url")
        seen["timeout"] = kwargs.get("timeout")
        seen["headers"] = kwargs.get("headers")
        assert "transport" not in kwargs
        built = dict(kwargs)
        built["transport"] = httpx.MockTransport(lambda _request: httpx.Response(200, json={"status": "ok"}))
        return httpx.AsyncClient(**built)

    def refuse_uds(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("unix transport")

    monkeypatch.setattr(client, "_new_client", new_client)
    monkeypatch.setattr(client.httpx, "AsyncHTTPTransport", refuse_uds)
    async with client.connect("https://sandbox.test/api", "", 9) as http:
        response = await http.get("/health")
    assert response.status_code == 200
    assert seen["base_url"] == "https://sandbox.test/api"
    assert seen["headers"] == {}
    timeout = seen["timeout"]
    assert isinstance(timeout, httpx.Timeout)
    assert timeout.read == 9


async def test_unix_url_uses_uds_transport_and_placeholder_base(monkeypatch: pytest.MonkeyPatch):
    captured: dict[str, object] = {}

    class _Transport:
        def __init__(self, *, uds: str) -> None:
            captured["uds"] = uds

    def new_client(**kwargs: Any) -> httpx.AsyncClient:
        captured["base_url"] = kwargs["base_url"]
        captured["headers"] = kwargs["headers"]
        built = dict(kwargs)
        built["transport"] = httpx.MockTransport(lambda _request: httpx.Response(200, json={"ok": True}))
        return httpx.AsyncClient(**built)

    monkeypatch.setattr(client.httpx, "AsyncHTTPTransport", _Transport)
    monkeypatch.setattr(client, "_new_client", new_client)
    async with client.connect("unix:///tmp/chrys.sock", "sekret", 4) as http:
        response = await http.get("/health")
    assert response.status_code == 200
    assert captured["uds"] == "/tmp/chrys.sock"
    assert captured["base_url"] == uds_http_base()
    assert captured["base_url"] == "http://jiuwenbox"
    assert captured["headers"] == {"Authorization": "Bearer sekret"}


async def test_relative_unix_url_is_rejected():
    with pytest.raises(SandboxError, match="unix:///absolute/path") as caught:
        async with client.connect("unix://tmp/chrys.sock", "tok-value", 1):
            pass
    assert "tok-value" not in str(caught.value)


async def test_unsupported_scheme_hides_userinfo_and_token():
    with pytest.raises(SandboxError, match="scheme is not supported") as caught:
        async with client.connect("ftp://user:userinfo-secret@sandbox.test", "bearer-token", 1):
            pass
    text = str(caught.value)
    assert "userinfo-secret" not in text
    assert "bearer-token" not in text


async def test_timeout_and_connect_errors_hide_transport_text(monkeypatch: pytest.MonkeyPatch):
    def time_out(_request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow bearer-token http://user:userinfo-secret@sandbox.test")

    _client_with(monkeypatch, time_out)
    with pytest.raises(SandboxError, match="timed out") as timed:
        async with client.connect("http://user:userinfo-secret@sandbox.test", "bearer-token", 3) as http:
            await http.get("/health")
    text = str(timed.value)
    assert "bearer-token" not in text
    assert "userinfo-secret" not in text

    def refuse(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down bearer-token http://user:userinfo-secret@sandbox.test")

    _client_with(monkeypatch, refuse)
    with pytest.raises(SandboxError, match="unreachable") as refused:
        async with client.connect("http://user:userinfo-secret@sandbox.test", "bearer-token", 3) as http:
            await http.get("/health")
    text = str(refused.value)
    assert "bearer-token" not in text
    assert "userinfo-secret" not in text


class _ClosingClient(httpx.AsyncClient):
    """Spies ``aclose`` and ``__aexit__``.

    ``async with`` on httpx 0.28 closes through ``__aexit__`` and does not call
    ``aclose``. Either path sets ``is_closed``.
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.close_events: list[str] = []

    async def aclose(self) -> None:
        self.close_events.append("aclose")
        await super().aclose()

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None = None,
        exc_value: BaseException | None = None,
        traceback: TracebackType | None = None,
    ) -> None:
        self.close_events.append("aexit")
        await super().__aexit__(exc_type, exc_value, traceback)


def _closing_client(
    opened: list[_ClosingClient],
    handler: Callable[[httpx.Request], httpx.Response],
) -> Callable[..., httpx.AsyncClient]:
    def new_client(**kwargs: Any) -> httpx.AsyncClient:
        built = dict(kwargs)
        built["transport"] = httpx.MockTransport(handler)
        http = _ClosingClient(**built)
        opened.append(http)
        return http

    return new_client


def _assert_client_closed(opened: list[_ClosingClient]) -> None:
    assert len(opened) == 1
    assert opened[0].is_closed
    assert opened[0].close_events == ["aexit"]


async def test_connect_closes_the_client_after_the_call(monkeypatch: pytest.MonkeyPatch):
    opened: list[_ClosingClient] = []
    monkeypatch.setattr(
        client,
        "_new_client",
        _closing_client(opened, lambda _request: httpx.Response(200, json={"status": "ok"})),
    )
    async with client.connect("https://sandbox.test/api", "", 9) as http:
        response = await http.get("/health")
    assert response.status_code == 200
    _assert_client_closed(opened)


async def test_dropped_connection_is_sandbox_error_and_closes_the_client(monkeypatch: pytest.MonkeyPatch):
    # A peer drop is an HTTPError, distinct from the timeout and connect failures above.
    opened: list[_ClosingClient] = []

    def drop(_request: httpx.Request) -> httpx.Response:
        raise httpx.RemoteProtocolError("Server disconnected bearer-token http://user:userinfo-secret@sandbox.test")

    monkeypatch.setattr(client, "_new_client", _closing_client(opened, drop))
    with pytest.raises(SandboxError, match="unreachable") as caught:
        async with client.connect("http://user:userinfo-secret@sandbox.test", "bearer-token", 3) as http:
            await http.get("/health")
    text = str(caught.value)
    assert "bearer-token" not in text
    assert "userinfo-secret" not in text
    assert "disconnected" not in text
    _assert_client_closed(opened)
