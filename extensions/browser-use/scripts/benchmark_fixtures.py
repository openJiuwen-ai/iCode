# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Five deterministic local HTTP fixtures for CI smoke and A/B model benchmarks.

Scenarios:
1. ``/streaming-chat``       -- streaming reply with explicit "Stop generating" marker.
2. ``/closed-shadow-form``   -- closed Shadow DOM textarea + submit button with light-DOM confirmation.
3. ``/delayed-button``       -- delayed editable input and button enabled after 450 ms.
4. ``/dynamic-ad-page``      -- 80 ms global ad ticker with a localized article region that settles.
5. ``/submit-response-lost`` -- server records POST once then drops/500s the HTTP response.
"""

from __future__ import annotations

import contextlib
import json
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


@dataclass
class FixtureState:
    """Server-side ground truth counters for judging task success and duplicate writes."""

    lock: threading.Lock = field(default_factory=threading.Lock)
    submissions: dict[str, int] = field(
        default_factory=lambda: {
            "streaming_chat": 0,
            "closed_shadow_form": 0,
            "delayed_button": 0,
            "submit_response_lost": 0,
        }
    )
    payloads: dict[str, list[str]] = field(default_factory=dict)

    def record(self, key: str, body: str) -> int:
        with self.lock:
            self.submissions[key] = self.submissions.get(key, 0) + 1
            self.payloads.setdefault(key, []).append(body)
            return self.submissions[key]

    def count(self, key: str) -> int:
        with self.lock:
            return self.submissions.get(key, 0)

    def reset(self) -> None:
        with self.lock:
            for key in list(self.submissions):
                self.submissions[key] = 0
            self.payloads.clear()


_STREAMING_CHAT_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Streaming Chat Fixture</title></head>
<body>
  <h1>Assistant Chat</h1>
  <div id="transcript" role="log" aria-label="Chat Transcript">
    <p>Previous turn: Hello!</p>
  </div>
  <button id="stop-btn" style="display:none">Stop generating</button>
  <form id="chat-form">
    <input id="composer" type="text" aria-label="Ask a follow-up" placeholder="Ask a follow-up" />
    <button id="send-btn" type="submit">Send</button>
  </form>
  <script>
    const form = document.getElementById('chat-form');
    const input = document.getElementById('composer');
    const stopBtn = document.getElementById('stop-btn');
    const transcript = document.getElementById('transcript');
    form.addEventListener('submit', async (ev) => {
      ev.preventDefault();
      const q = input.value.trim();
      if (!q) return;
      input.value = '';
      stopBtn.style.display = 'inline-block';
      const msg = document.createElement('div');
      msg.id = 'latest-reply';
      msg.textContent = 'Thinking...';
      transcript.appendChild(msg);
      await fetch('/api/chat', { method: 'POST', body: q });
      setTimeout(() => { msg.textContent = 'Checking forecast...'; }, 200);
      setTimeout(() => { msg.textContent = 'FINAL_WEATHER_ANSWER: Shanghai 24C clear'; }, 550);
      setTimeout(() => { stopBtn.style.display = 'none'; }, 750);
    });
  </script>
</body></html>
"""

_CLOSED_SHADOW_FORM_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Closed Shadow Form Fixture</title></head>
<body>
  <h1>Support Ticket</h1>
  <shadow-ticket-box id="ticket-host"></shadow-ticket-box>
  <div id="ticket-status" role="status">Idle</div>
  <script>
    class ShadowTicketBox extends HTMLElement {
      constructor() {
        super();
        const root = this.attachShadow({ mode: 'closed' });
        root.innerHTML = `
          <div style="padding:12px;border:1px solid #ccc;">
            <textarea id="note" aria-label="Ticket Note" placeholder="Describe the issue"></textarea>
            <button id="submit-ticket" type="button">Submit Ticket</button>
          </div>
        `;
        const note = root.getElementById('note');
        const btn = root.getElementById('submit-ticket');
        btn.addEventListener('click', async () => {
          await fetch('/api/ticket', { method: 'POST', body: note.value || '' });
          document.getElementById('ticket-status').textContent = 'TICKET_CONFIRMED_#42';
        });
      }
    }
    customElements.define('shadow-ticket-box', ShadowTicketBox);
  </script>
</body></html>
"""

_DELAYED_BUTTON_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Delayed Button Fixture</title></head>
<body>
  <h1>Release Gate</h1>
  <input id="code" type="text" aria-label="Release Code" disabled />
  <button id="approve" type="button" disabled>Approve Release</button>
  <div id="release-status">Waiting for checks...</div>
  <script>
    setTimeout(() => {
      document.getElementById('code').disabled = false;
      document.getElementById('approve').disabled = false;
      document.getElementById('release-status').textContent = 'Checks passed';
    }, 450);
    document.getElementById('approve').addEventListener('click', async () => {
      const code = document.getElementById('code').value || '';
      await fetch('/api/release', { method: 'POST', body: code });
      document.getElementById('release-status').textContent = 'RELEASE_APPROVED_OK';
    });
  </script>
</body></html>
"""

_DYNAMIC_AD_PAGE_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Dynamic Ad Page Fixture</title></head>
<body>
  <aside id="ad-banner" style="padding:8px;background:#fee;">Ad tick #0</aside>
  <main>
    <h1>Quarterly Brief</h1>
    <section id="article-region" aria-label="Q4 report region">
      <h2>Q4 report</h2>
      <p id="report-body">Loading metrics...</p>
    </section>
  </main>
  <script>
    let adTick = 0;
    setInterval(() => {
      adTick += 1;
      document.getElementById('ad-banner').textContent = 'Ad tick #' + adTick;
    }, 80);
    setTimeout(() => {
      document.getElementById('report-body').textContent = 'Compiling Q4 figures...';
    }, 150);
    setTimeout(() => {
      document.getElementById('report-body').textContent = 'REGION_SUMMARY: Q4 revenue grew 18% YoY';
    }, 350);
  </script>
</body></html>
"""

_SUBMIT_RESPONSE_LOST_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Submit Response Lost Fixture</title></head>
<body>
  <h1>Order Checkout</h1>
  <button id="confirm-order" type="button">Confirm Order</button>
  <div id="order-status">Pending confirmation</div>
  <script>
    document.getElementById('confirm-order').addEventListener('click', async () => {
      const status = document.getElementById('order-status');
      status.textContent = 'Submitting order...';
      try {
        const resp = await fetch('/api/order-drop', { method: 'POST', body: 'order-1001' });
        if (!resp.ok) {
          status.textContent = 'Order response lost (HTTP ' + resp.status + ') — do not resubmit';
        }
      } catch (_err) {
        status.textContent = 'Order response lost (network drop) — do not resubmit';
      }
    });
  </script>
</body></html>
"""


@contextlib.contextmanager
def serve() -> Iterator[tuple[str, FixtureState]]:
    """Start the 5-scenario local loopback fixture server and yield ``(origin, state)``."""
    state = FixtureState()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            return None

        def _send_html(self, html: str) -> None:
            data = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _send_json(self, payload: dict[str, object], status: int = 200) -> None:
            data = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:
            routes = {
                "/streaming-chat": _STREAMING_CHAT_HTML,
                "/closed-shadow-form": _CLOSED_SHADOW_FORM_HTML,
                "/delayed-button": _DELAYED_BUTTON_HTML,
                "/dynamic-ad-page": _DYNAMIC_AD_PAGE_HTML,
                "/submit-response-lost": _SUBMIT_RESPONSE_LOST_HTML,
            }
            html = routes.get(self.path.split("?", 1)[0])
            if html is not None:
                self._send_html(html)
                return
            self.send_response(404)
            self.end_headers()

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length") or "0")
            body = self.rfile.read(length).decode("utf-8", errors="replace") if length > 0 else ""
            path = self.path.split("?", 1)[0]
            if path == "/api/chat":
                count = state.record("streaming_chat", body)
                self._send_json({"ok": True, "count": count})
                return
            if path == "/api/ticket":
                count = state.record("closed_shadow_form", body)
                self._send_json({"ok": True, "count": count})
                return
            if path == "/api/release":
                count = state.record("delayed_button", body)
                self._send_json({"ok": True, "count": count})
                return
            if path == "/api/order-drop":
                count = state.record("submit_response_lost", body)
                time.sleep(0.25)
                self._send_json({"error": "upstream_timeout", "count": count}, status=504)
                return
            self.send_response(404)
            self.end_headers()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    origin = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        yield origin, state
    finally:
        server.shutdown()
        server.server_close()
