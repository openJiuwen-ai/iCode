# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The loopback fixture site: routes, counters and bounded delays (W05)."""

from __future__ import annotations

import http.client
import json
import runpy
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
SITE = runpy.run_path(str(ROOT / "scripts" / "fixture_site.py"))

OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
"""Never route loopback traffic through a system proxy."""


def fetch(url: str) -> tuple[int, bytes]:
    try:
        with OPENER.open(url, timeout=10) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def post(url: str, body: bytes = b"", headers: dict[str, str] | None = None) -> tuple[int, bytes]:
    request = urllib.request.Request(url, data=body, method="POST", headers=headers or {})
    with OPENER.open(request, timeout=10) as response:
        return response.status, response.read()


def test_pages_and_counters() -> None:
    with SITE["serve"]() as (origin, state):
        status, body = fetch(origin + "/")
        assert status == 200
        assert b"fixture-home" in body
        assert b"type=file" in body
        assert b"<iframe" in body
        assert b"__IFRAME_SRC__" not in body

        assert fetch(origin + "/iframe")[0] == 200
        assert fetch(origin + "/iframe-click")[0] == 204
        status, tall = fetch(origin + "/tall")
        assert status == 200 and b"fixture-tall" in tall and b"3000px" in tall, "the tall page grew"
        assert fetch(origin + "/popup")[0] == 200
        assert fetch(origin + "/popup-target")[0] == 200
        assert fetch(origin + "/api")[0] == 200

        assert post(origin + "/submit", b"email=dev%40local.test")[0] == 200
        assert post(origin + "/submit", b"email=again")[0] == 200
        status, body = post(
            origin + "/upload",
            b'--x\r\nContent-Disposition: form-data; name="f"; filename="<b>avatar.txt</b>"\r\n\r\ndata!\r\n--x--\r\n',
            {"Content-Type": "multipart/form-data; boundary=x"},
        )
        assert status == 200
        assert b"&lt;b&gt;avatar.txt&lt;/b&gt;" in body, "the file name must be HTML-escaped"
        assert b"uploaded &lt;b&gt;avatar.txt&lt;/b&gt; 5 1" in body, "the size is the file part, not the envelope"

        status, body = fetch(origin + "/state")
        assert status == 200
        snapshot = json.loads(body)
        assert snapshot["counts"]["submit"] == 2
        assert snapshot["counts"]["upload"] == 1
        assert snapshot["counts"]["iframe"] == 1
        # /state itself is not counted; the log holds every other request, and
        # /iframe-click records both its GET and the iframe counter.
        assert snapshot["requests"] == 11


def test_delay_is_bounded_and_reported() -> None:
    with SITE["serve"](max_delay_ms=20) as (origin, state):
        status, body = fetch(origin + "/slow?ms=10")
        assert status == 200
        assert b"fixture-slow" in body
        # An absurd delay is clamped instead of wedging the test server.
        status, _ = fetch(origin + "/slow?ms=999999")
        assert status == 200
        assert state.counts["get /slow"] == 2


def test_dom_page_embeds_the_clamped_delay() -> None:
    with SITE["serve"](max_delay_ms=20) as (origin, _state):
        body = fetch(origin + "/dom?delay=999999")[1]
        assert b"<h1 id=title>before</h1>" in body
        assert b"}, 20)" in body


def test_controls_page_serves_native_selects_and_a_delayed_button() -> None:
    with SITE["serve"](max_delay_ms=20) as (origin, _state):
        body = fetch(origin + "/controls?delay=999999")[1]
        assert b"fixture-controls" in body
        assert b"<select id=single name=single>" in body
        assert b"<select id=multi name=multi multiple>" in body
        assert b"id=keys" in body
        assert b"id=key-log" in body
        assert b"id=change-log" in body
        assert b"id=remove" in body
        assert b"id=swap" in body
        assert b"}, 20)" in body, "the delayed button must respect max_delay_ms"


def test_iframe_origin_is_embedded_and_validated() -> None:
    with SITE["serve"](iframe_origin="http://localhost:1234/") as (origin, _state):
        body = fetch(origin + "/")[1]
        assert b'src="http://localhost:1234/iframe"' in body
    for bad in ("localhost:1234", "http://localhost:1234/path", "http://localhost:1234?x=1"):
        try:
            with SITE["serve"](iframe_origin=bad):
                raise AssertionError(f"{bad!r} should be rejected")
        except ValueError:
            pass


def test_download_is_an_attachment() -> None:
    with SITE["serve"]() as (origin, _state):
        request = urllib.request.Request(origin + "/download")
        with OPENER.open(request, timeout=10) as response:
            assert response.headers["Content-Disposition"].startswith("attachment;")
            assert response.read() == b"fixture-download-payload"


def test_bad_content_length_is_rejected_without_a_traceback() -> None:
    with SITE["serve"]() as (origin, _state):
        request = urllib.request.Request(origin + "/submit", data=b"", method="POST")
        request.add_header("Content-Length", "not-a-number")
        try:
            with OPENER.open(request, timeout=10):
                raise AssertionError("a malformed length must be refused")
        except urllib.error.HTTPError as exc:
            assert exc.code == 400


def test_oversized_body_is_rejected_without_a_traceback() -> None:
    with SITE["serve"]() as (origin, _state):
        # Send the headers only: the server must answer 413 from Content-Length
        # alone. Sending a real oversized body would race the server closing the
        # connection, which the client can observe as a reset instead of a response.
        parts = urlsplit(origin)
        connection = http.client.HTTPConnection(parts.hostname, parts.port, timeout=10)
        try:
            connection.putrequest("POST", "/upload")
            connection.putheader("Content-Length", str(SITE["MAX_UPLOAD_BYTES"] + 1))
            connection.endheaders()
            response = connection.getresponse()
            assert response.status == 413
            response.read()
        finally:
            connection.close()


def test_tab_and_oauth_fixture_pages() -> None:
    with SITE["serve"](iframe_origin="http://localhost:1234/") as (origin, _state):
        home = fetch(origin + "/")[1]
        assert b'id=new-tab href="/tall" target=_blank' in home
        assert b'id=oauth href="http://localhost:1234/oauth/start" target=_blank' in home
        start = fetch(origin + "/oauth/start")[1]
        assert b"oauth-start" in start and b"/oauth/authorize" in start
        authorize = fetch(origin + "/oauth/authorize")[1]
        assert b"oauth-authorize" in authorize and b"/oauth/callback" in authorize
        callback = fetch(origin + "/oauth/callback")[1]
        assert b"oauth complete code=abc" in callback and b"id=close-oauth" in callback
        popup = fetch(origin + "/popup-target")[1]
        assert b"fixture-popup-target" in popup and b"id=close-popup" in popup


def test_dialog_page_and_result_recording() -> None:
    with SITE["serve"]() as (origin, state):
        body = fetch(origin + "/dialogs")[1]
        assert b"fixture-dialogs" in body
        assert b"window.alert('fixture alert message')" in body
        assert b"window.confirm('fixture confirm?')" in body
        assert b"window.prompt('fixture prompt?', 'default-name')" in body
        assert b"id=late" in body, "a dialog opened by a timer must be reachable"
        assert fetch(origin + "/dialog-result?event=dialog-confirm%3Atrue")[0] == 204
        assert state.counts["dialog dialog-confirm:true"] == 1


def test_download_file_variants_and_links() -> None:
    with SITE["serve"](max_delay_ms=20) as (origin, _state):
        request = urllib.request.Request(origin + "/download-file?bytes=64&filename=..%2F..%2Fevil.exe")
        with OPENER.open(request, timeout=10) as response:
            assert response.headers["Content-Disposition"].endswith('filename="../../evil.exe"')
            assert response.read() == b"x" * 64
        body = fetch(origin + "/download-links")[1]
        assert b"fixture-download-links" in body
        for link in (b"id=plain", b"id=traversal", b"id=reserved", b"id=big", b"id=slow"):
            assert link in body


def test_chat_page_conversations_and_send_counter() -> None:
    with SITE["serve"](max_delay_ms=50) as (origin, state):
        status, body = fetch(origin + "/chat")
        assert status == 200
        assert b"<title>chat-home</title>" in body
        assert body.count(b'<a href="/chat/c') == 3, "three sidebar links"
        assert b"alt=" not in body, "sidebar icons carry no accessible name on purpose"
        assert b"id=overlay" in body and b'aria-label="Send message"' in body
        assert b"contenteditable=true" in body
        assert b", 300);" not in body and b", 50);" in body, "stream_ms is clamped by the server"
        assert b", 50);" in fetch(origin + "/chat?stream_ms=999999")[1], "an oversized stream_ms is clamped"

        status, body = fetch(origin + "/chat/c2?stream_ms=10")
        assert status == 200 and b"<title>chat-c2</title>" in body and b", 10);" in body
        assert fetch(origin + "/chat/cx")[0] == 404
        assert fetch(origin + "/chat/send")[0] == 404

        status, body = post(origin + "/chat/send", b'{"text":"hi"}', {"Content-Type": "application/json"})
        assert status == 200 and json.loads(body) == {"ok": True, "count": 1}
        assert state.snapshot()["counts"]["chat_submits"] == 1
        assert state.snapshot()["counts"].get("submit") is None, "chat sends never count as form submits"


def test_dense_page_has_fold_hidden_and_scroller_targets() -> None:
    with SITE["serve"]() as (origin, _state):
        status, body = fetch(origin + "/dense")
        assert status == 200
        assert b"<title>fixture-dense</title>" in body
        assert body.count(b'aria-label="Item row"') == 3, "three same-name siblings"
        assert b"<section id=ghost></section>" in body, "an unnamed empty node"
        assert b"overflow:auto" in body and b"height:900px" in body, "an unnamed scroller bigger than the viewport"
        assert b'aria-label="Panel"' in body and b'aria-label="Nested"' in body, "a named panel with a nested control"


def test_unknown_path_is_404() -> None:
    with SITE["serve"]() as (origin, _state):
        assert fetch(origin + "/missing")[0] == 404
