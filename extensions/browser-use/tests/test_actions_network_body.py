# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for ``browser_network_body`` and :class:`PageLog` metadata capture."""

from __future__ import annotations

import asyncio
import base64
from types import SimpleNamespace

import pytest

from icode_browser_use.actions import network_body
from icode_browser_use.errors import BadRequest
from icode_browser_use.page_log import PageLog, scrub_headers, scrub_payload


class _FakeClient:
    def __init__(self, responses: dict[str, dict | Exception]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, dict, str | None]] = []

    async def send(self, method: str, params=None, session_id=None, timeout=None):
        self.calls.append((method, dict(params or {}), session_id))
        req_id = str((params or {}).get("requestId") or "")
        outcome = self.responses.get(req_id)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome or {"body": "", "base64Encoded": False}


def _make_session(log: PageLog, client: _FakeClient, *, target_id: str = "tab-1", session_id: str = "s-main"):
    return SimpleNamespace(
        page_log=log,
        target_id=target_id,
        session_id=session_id,
        cfg=SimpleNamespace(cdp_timeout=2.0),
        require_client=lambda: client,
    )


def _defaults(**overrides):
    base = {
        "url_contains": "",
        "include_headers": True,
        "include_request_body": True,
        "max_chars": 4000,
        "offset": 0,
    }
    base.update(overrides)
    return base


def test_scrub_payload_and_headers_redact_secrets_and_keep_multiline_json() -> None:
    raw = '{\n  "order": "ORD-1",\n  "access_token": "SECRET_TOK",\n  "jwt": "eyJhbGciOi.eyJzdWIi.sig123"\n}'
    cleaned = scrub_payload(raw)
    assert "\n" in cleaned
    assert "ORD-1" in cleaned
    assert "SECRET_TOK" not in cleaned
    assert "eyJhbGciOi" not in cleaned

    headers = scrub_headers(
        (
            ("Authorization", "Bearer TOP_SECRET"),
            ("Set-Cookie", "sid=COOKIE123; HttpOnly"),
            ("X-Api-Key", "KEY_XYZ"),
            ("Content-Type", "application/json; charset=utf-8"),
            ("X-Request-Id", "req-42"),
        )
    )
    as_map = dict(headers)
    assert as_map["Authorization"] == "***"
    assert as_map["Set-Cookie"] == "***"
    assert as_map["X-Api-Key"] == "***"
    assert as_map["Content-Type"] == "application/json; charset=utf-8"
    assert as_map["X-Request-Id"] == "req-42"


def test_network_body_reads_json_headers_request_payload_and_paginates() -> None:
    log = PageLog()
    log.request(
        "tab-1",
        {
            "requestId": "req-1",
            "request": {
                "method": "POST",
                "url": "https://api.example/orders?token=URL_SECRET",
                "headers": {
                    "Authorization": "Bearer HEAD_SECRET",
                    "Content-Type": "application/json",
                },
                "postData": '{"item":"Laptop","password":"PASS_SECRET"}',
                "hasPostData": True,
            },
        },
        session_id="s-oopif",
    )
    log.response(
        "tab-1",
        {
            "requestId": "req-1",
            "type": "Fetch",
            "response": {
                "status": 201,
                "url": "https://api.example/orders?token=URL_SECRET",
                "mimeType": "application/json",
                "headers": {
                    "Set-Cookie": "session=COOK_SECRET",
                    "Content-Type": "application/json",
                },
            },
        },
        session_id="s-oopif",
    )
    log.finished("tab-1", {"requestId": "req-1"})

    long_body = '{"order_id":"ORD-2026","access_token":"BODY_SECRET","pad":"' + ("A" * 260) + '","end":"TAIL_OK"}'
    client = _FakeClient({"req-1": {"body": long_body, "base64Encoded": False}})
    session = _make_session(log, client)

    # First slice by url_contains
    out1 = asyncio.run(network_body.run(session, _defaults(url_contains="/orders", max_chars=200)))[0]["text"]
    assert "#1 network.info POST 201 https://api.example/orders" in out1
    assert "resource_type=Fetch mime_type=application/json" in out1
    assert "Authorization: ***" in out1
    assert "Set-Cookie: ***" in out1
    assert '"item":"Laptop"' in out1
    assert '"order_id":"ORD-2026"' in out1
    assert "offset=200" in out1
    for secret in ("URL_SECRET", "HEAD_SECRET", "PASS_SECRET", "COOK_SECRET", "BODY_SECRET"):
        assert secret not in out1
    # Must route CDP call to the recorded OOPIF session_id!
    assert client.calls[0] == ("Network.getResponseBody", {"requestId": "req-1"}, "s-oopif")

    # Second slice by exact seq + offset
    out2 = asyncio.run(
        network_body.run(
            session,
            _defaults(seq=1, include_headers=False, include_request_body=False, max_chars=400, offset=200),
        )
    )[0]["text"]
    assert "[REQUEST_HEADERS]" not in out2
    assert "[REQUEST_BODY]" not in out2
    assert "TAIL_OK" in out2


def test_network_body_omits_binary_payload_and_decodes_base64_json() -> None:
    log = PageLog()
    log.request("tab-1", {"requestId": "img-1", "request": {"method": "GET", "url": "https://api.example/logo.png"}})
    log.response(
        "tab-1",
        {
            "requestId": "img-1",
            "type": "Image",
            "response": {"status": 404, "url": "https://api.example/logo.png", "mimeType": "image/png"},
        },
    )
    log.finished("tab-1", {"requestId": "img-1"})

    log.request("tab-1", {"requestId": "b64-1", "request": {"method": "GET", "url": "https://api.example/data"}})
    log.response(
        "tab-1",
        {
            "requestId": "b64-1",
            "type": "XHR",
            "response": {"status": 200, "url": "https://api.example/data", "mimeType": "application/json"},
        },
    )
    log.finished("tab-1", {"requestId": "b64-1"})

    png_bytes = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
    json_b64 = base64.b64encode(b'{"ok":true,"msg":"hello"}').decode("ascii")
    client = _FakeClient(
        {
            "img-1": {"body": base64.b64encode(png_bytes).decode("ascii"), "base64Encoded": True},
            "b64-1": {"body": json_b64, "base64Encoded": True},
        }
    )
    session = _make_session(log, client)

    img_out = asyncio.run(network_body.run(session, _defaults(seq=1)))[0]["text"]
    assert f"[binary response body omitted: mime=image/png, {len(png_bytes)} bytes]" in img_out

    json_out = asyncio.run(network_body.run(session, _defaults(seq=2)))[0]["text"]
    assert '{"ok":true,"msg":"hello"}' in json_out


def test_network_body_handles_console_seq_failed_request_eviction_and_bridge_error() -> None:
    log = PageLog()
    log.console("tab-1", {"type": "log", "args": [{"type": "string", "value": "hello"}]})
    log.request("tab-1", {"requestId": "f-1", "request": {"method": "POST", "url": "https://blocked.test/api"}})
    log.failed("tab-1", {"requestId": "f-1", "errorText": "net::ERR_FAILED", "blockedReason": "inspector"})
    log.request("tab-1", {"requestId": "ev-1", "request": {"method": "GET", "url": "https://api.example/old"}})
    log.response(
        "tab-1",
        {
            "requestId": "ev-1",
            "type": "Fetch",
            "response": {"status": 200, "url": "https://api.example/old", "mimeType": "text/plain"},
        },
    )
    log.finished("tab-1", {"requestId": "ev-1"})
    log.request("tab-1", {"requestId": "br-1", "request": {"method": "GET", "url": "https://api.example/bridge"}})
    log.response(
        "tab-1",
        {
            "requestId": "br-1",
            "type": "Fetch",
            "response": {"status": 200, "url": "https://api.example/bridge", "mimeType": "text/plain"},
        },
    )
    log.finished("tab-1", {"requestId": "br-1"})

    client = _FakeClient(
        {
            "ev-1": RuntimeError("Request content was evicted from inspector cache"),
            "br-1": RuntimeError("Network.getResponseBody is not allowed in bridge mode"),
        }
    )
    session = _make_session(log, client)

    with pytest.raises(BadRequest, match="console 日志"):
        asyncio.run(network_body.run(session, _defaults(seq=1)))

    failed_out = asyncio.run(network_body.run(session, _defaults(seq=2)))[0]["text"]
    assert "unavailable" in failed_out and not client.calls

    evicted_out = asyncio.run(network_body.run(session, _defaults(seq=3)))[0]["text"]
    assert "evicted" in evicted_out

    bridge_out = asyncio.run(network_body.run(session, _defaults(seq=4)))[0]["text"]
    assert "update-bridge" in bridge_out

    missing_out = asyncio.run(network_body.run(session, _defaults(url_contains="/never")))[0]["text"]
    assert "没有匹配的网络请求记录" in missing_out


def test_network_body_awaits_loading_finished_before_second_attempt() -> None:
    log = PageLog()
    log.request("tab-1", {"requestId": "slow-1", "request": {"method": "GET", "url": "https://api.example/stream"}})
    log.response(
        "tab-1",
        {
            "requestId": "slow-1",
            "type": "Fetch",
            "response": {"status": 200, "url": "https://api.example/stream", "mimeType": "application/json"},
        },
    )

    attempts = 0

    class _StreamingClient:
        async def send(self, method: str, params=None, session_id=None, timeout=None):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                asyncio.get_running_loop().call_later(0.02, log.finished, "tab-1", {"requestId": "slow-1"})
                raise RuntimeError("No data found for resource with given identifier")
            return {"body": '{"streamed":true}', "base64Encoded": False}

    session = _make_session(log, _StreamingClient())
    out = asyncio.run(network_body.run(session, _defaults(seq=1)))[0]["text"]
    assert attempts == 2
    assert '{"streamed":true}' in out
