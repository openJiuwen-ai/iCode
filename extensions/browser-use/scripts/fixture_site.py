# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Loopback fixture site for real-browser checks and host integration tests.

One stdlib HTTP server with the endpoints the roadmap's W05 matrix needs:

===================  =====================================================
``GET /``            form page: text field, file input, submit, iframe,
                     popup link, DOM-replacement button, slow link, download
``POST /submit``     increments ``submit`` and answers with the new count
``POST /upload``     increments ``upload``; echoes the file name and the
                     multipart byte count (bounded, HTML-escaped)
``GET /slow?ms=N``   answers after ``N`` milliseconds (clamped)
``GET /tall``        document taller than any test viewport (screenshot checks)
``GET /dom``         page whose button replaces text after ``delay`` ms
``GET /dense``       same-name text siblings, an unnamed empty section, an
                     unnamed scroller and a named panel with nested controls
``GET /controls?delay=N``
                     native single/multiple ``select``, a text field, a button
                     that removes itself and a button that appears after ``N`` ms
``GET /dialogs``     page whose buttons open alert/confirm/prompt (and a late alert)
``GET /dialog-result?event=...``
                     records what the page saw after a dialog closed (204)
``GET /iframe``      child frame with a button that reports a click
``GET /iframe-click``increments ``iframe`` and answers 204
``GET /popup``       opens ``/popup-target`` in a new window
``GET /popup-target``standalone page for the popup (with a close button)
``GET /oauth/start`` provider page whose button opens ``/oauth/authorize``
``GET /oauth/authorize``
                     provider consent page that redirects to ``/oauth/callback``
``GET /oauth/callback``
                     result page (``oauth complete code=abc``) that can close itself
``GET /download``    attachment response (denied unless controlled downloads are enabled)
``GET /download-file?bytes=N&delay_ms=D&filename=...``
                     attachment of ``N`` bytes (max 5 MiB), written in 16 KiB
                     chunks with an optional per-chunk delay; ``filename`` is
                     echoed in ``Content-Disposition`` for name-sanitising checks
``GET /download-links``
                     page with plain/traversal/reserved/too-big/slow download links
``GET /chat``, ``GET /chat/cN``
                     chat-style SPA: sidebar links with empty accessible names,
                     a ``contenteditable`` composer that sends on Enter, a send
                     button covered by a transparent overlay, and an answer that
                     streams in over ``stream_ms`` (clamped) with a Stop button
``POST /chat/send``  increments ``chat_submits`` and answers JSON with the count
``GET /api``         JSON liveness probe
``GET /state``       JSON with the counters and the request log
===================  =====================================================

Counters live in memory and are exposed through ``/state`` so a test can prove
how many times a submit actually happened -- the only honest way to check that a
retry did not duplicate a side effect. The ``/state`` request itself is not
counted, so ``requests`` means "what the browser asked for".
"""

from __future__ import annotations

import html
import json
import socket
import sys
import threading
import time
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import unquote, urlsplit

MAX_DELAY_MS = 5000
"""Default clamp for every fixture delay (``/slow``, ``/dom``, ``/controls``, ``/chat`` stream, downloads)."""

MAX_UPLOAD_BYTES = 1 << 20
"""Refuse bigger multipart bodies instead of buffering them forever."""


def _frame_origin(value: str) -> str:
    """Validate an ``iframe_origin``: a bare origin, no path or trailing slash."""
    text = (value or "").strip().rstrip("/")
    if not text:
        return ""
    parts = urlsplit(text)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise ValueError(f"iframe_origin must be an http(s) origin, got {value!r}")
    if parts.path or parts.query or parts.fragment:
        raise ValueError(f"iframe_origin must not carry a path, query or fragment: {value!r}")
    return text


def _query_value(query: str, name: str) -> str:
    """The URL-decoded value of one ``a=b`` pair (empty when absent)."""
    for part in query.split("&"):
        key, _, value = part.partition("=")
        if key == name:
            return unquote(value)
    return ""


class FixtureState:
    """Shared, thread-safe record of what the browser actually did."""

    def __init__(self) -> None:
        self.counts: Counter[str] = Counter()
        self.requests: list[str] = []
        self._lock = threading.Lock()

    def note(self, name: str, request: str) -> int:
        with self._lock:
            self.counts[name] += 1
            self.requests.append(request)
            return self.counts[name]

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {"counts": dict(self.counts), "requests": len(self.requests)}


_FORM_PAGE = """<!doctype html><html><head><title>fixture-home</title></head><body>
<h1 id=title>fixture-home</h1>
<form method=post action=/submit>
  <label>Email <input id=email name=email></label>
  <label>Avatar <input id=avatar name=avatar type=file></label>
  <button id=submit type=submit>Send</button>
</form>
<p><a id=new-tab href="/tall" target=_blank>new tab</a></p>
<p><a id=oauth href="__OAUTH_SRC__" target=_blank>sign in</a></p>
<p><a id=popup href="/popup" target=_blank>open popup</a></p>
<p><a id=slow href="/slow?ms=800">slow page</a></p>
<p><a id=download href="/download">download</a></p>
<iframe id=child src="__IFRAME_SRC__" width=320 height=120></iframe>
</body></html>"""

_OAUTH_START_PAGE = """<!doctype html><html><head><title>oauth-start</title></head><body>
<h1>oauth-start</h1>
<button id=open-signin onclick="window.open('/oauth/authorize?state=xyz&redirect_uri=/oauth/callback', '_blank')">Open sign-in</button>
</body></html>"""

_OAUTH_AUTHORIZE_PAGE = """<!doctype html><html><head><title>oauth-authorize</title></head><body>
<h1>oauth-authorize</h1>
<button id=approve onclick="location = '/oauth/callback?code=abc&state=xyz'">Approve</button>
</body></html>"""

_OAUTH_CALLBACK_PAGE = """<!doctype html><html><head><title>oauth-callback</title></head><body>
<h1 id=callback-title>oauth complete code=abc</h1>
<button id=close-oauth onclick="window.close()">Close window</button>
</body></html>"""

_NOOPENER_PAGE = """<!doctype html><html><head><title>noopener-open</title></head><body>
<h1>noopener-open</h1>
<button id=open-noopener onclick="window.open(new URLSearchParams(location.search).get('target'), '_blank', 'noopener')">Open without opener</button>
</body></html>"""

_POPUP_TARGET_PAGE = """<!doctype html><html><head><title>fixture-popup-target</title></head><body>
<h1>fixture-popup-target</h1>
<button id=close-popup onclick="window.close()">Close window</button>
</body></html>"""

_IFRAME_PAGE = """<!doctype html><html><head><title>fixture-iframe</title></head><body>
<p id=iframe-title>fixture-iframe</p>
<button id=iframe-button onclick="fetch('/iframe-click')">frame action</button>
</body></html>"""

_DOM_PAGE = """<!doctype html><html><head><title>fixture-dom</title></head><body>
<h1 id=title>before</h1>
<button id=open-now aria-expanded="false" onclick="this.setAttribute('aria-expanded','true'); this.textContent='opened'">open now</button>
<button id=replace onclick="setTimeout(() => {{ document.getElementById('title').textContent = 'replaced-late'; }}, {delay})">
replace later</button>
</body></html>"""

_CONTROLS_PAGE = """<!doctype html><html><head><title>fixture-controls</title></head><body>
<h1 id=title>fixture-controls</h1>
<label>Choice <select id=single name=single>
  <option value=alpha>Alpha</option>
  <option value=beta selected>Beta</option>
  <option value=gamma>Gamma</option>
</select></label>
<label>Tags <select id=multi name=multi multiple>
  <option value=red>Red</option>
  <option value=green>Green</option>
  <option value=blue>Blue</option>
</select></label>
<label>Keys <input id=keys value="seed"></label>
<div id=key-log>none</div>
<div id=change-log>none</div>
<button id=remove onclick="this.remove()">remove me</button>
<button id=swap onclick="var n=document.createElement('button'); n.id='swap'; n.textContent='swap me';
  n.addEventListener('click', function(){{ this.remove(); }}); this.replaceWith(n);">swap me</button>
<div id=late-host></div>
<script>setTimeout(function(){{
  document.getElementById('late-host').innerHTML = '<button id=late>late button</button>';
}}, {delay});
document.getElementById('keys').addEventListener('keydown', function(event) {{
  var combo = (event.ctrlKey ? 'ctrl+' : '') + (event.altKey ? 'alt+' : '')
    + (event.metaKey ? 'meta+' : '') + (event.shiftKey ? 'shift+' : '');
  document.getElementById('key-log').textContent = combo + (event.key || '') + '|' + this.value;
}});
document.getElementById('keys').addEventListener('input', function() {{
  document.getElementById('key-log').textContent = 'value:' + this.value;
}});
for (const id of ['single', 'multi']) {{
  document.getElementById(id).addEventListener('change', function() {{
    var picked = Array.from(this.selectedOptions).map(function(option) {{ return option.value; }});
    document.getElementById('change-log').textContent = 'change:' + id + ':' + picked.join(',');
  }});
}}</script>
</body></html>"""

_DENSE_PAGE = """<!doctype html><html><head><title>fixture-dense</title></head><body>
<h1>fixture-dense</h1>
<div id=rows>
  <div class=row role=listitem aria-label="Item row">Item row</div>
  <div class=row role=listitem aria-label="Item row">Item row</div>
  <div class=row role=listitem aria-label="Item row">Item row</div>
</div>
<section id=ghost></section>
<div id=scroller style="height:900px;width:300px;overflow:auto"><div style="height:1600px">tall content</div></div>
<button id=save>Save</button>
<div id=panel aria-label="Panel">
  <button aria-label="Inner A">A</button>
  <button aria-label="Inner B">B</button>
  <div id=nested><button aria-label="Nested">N</button></div>
</div>
</body></html>"""


_DOWNLOAD_LINKS_PAGE = """<!doctype html><html><head><title>fixture-download-links</title></head><body>
<h1 id=title>fixture-download-links</h1>
<p><a id=plain href="/download">plain download</a></p>
<p><a id=traversal href="/download-file?bytes=64&filename=..%2F..%2Fevil.exe">traversal name</a></p>
<p><a id=reserved href="/download-file?bytes=8&filename=CON.txt">reserved name</a></p>
<p><a id=big href="/download-file?bytes=2097152">too big</a></p>
<p><a id=slow href="/download-file?bytes=1048576&delay_ms=25">slow</a></p>
</body></html>"""

_DIALOGS_PAGE = """<!doctype html><html><head><title>fixture-dialogs</title></head><body>
<h1 id=title>fixture-dialogs</h1>
<div id=result>none</div>
<button id=alert onclick="window.alert('fixture alert message'); report('dialog-alert')">alert</button>
<button id=confirm onclick="report('dialog-confirm:' + window.confirm('fixture confirm?'))">confirm</button>
<button id=prompt onclick="var v = window.prompt('fixture prompt?', 'default-name');
  report('dialog-prompt:' + (v === null ? 'null' : v))">prompt</button>
<button id=late onclick="setTimeout(function(){ window.alert('fixture late alert'); report('dialog-late'); }, 600)">
late alert</button>
<script>
function report(value) {
  document.getElementById('result').textContent = value;
  fetch('/dialog-result?event=' + encodeURIComponent(value));
}
</script>
</body></html>"""

_POPUP_PAGE = """<!doctype html><html><head><title>fixture-popup</title></head><body>
<h1>fixture-popup</h1>
<script>window.open('/popup-target', '_blank');</script>
</body></html>"""

_TALL_PAGE = """<!doctype html><html><head><title>fixture-tall</title></head><body>
<h1>fixture-tall</h1>
<div style="height:3000px">a document taller than the viewport</div>
<button id="bottom-btn" type="button" style="padding:8px 16px;background:#1a73e8;color:#fff;">Bottom Action</button>
</body></html>"""

_PIXEL = "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7"
"""Inline 1x1 GIF so the sidebar icons never hit the server."""

_CHAT_PAGE = """<!doctype html><html><head><title>chat-__CONV__</title>
<style>
#send-wrap { position: relative; display: inline-block; }
.conv img { width: 24px; height: 24px; vertical-align: middle; }
#overlay { position: absolute; inset: 25%; background: transparent; }
#composer { min-height: 1.5em; border: 1px solid #888; padding: 4px; width: 20em; }
#composer:empty::before { content: attr(data-placeholder); color: #888; }
</style></head><body>
<h1 id=title>chat-__CONV__</h1>
<nav id=sidebar aria-label="Recent">
  <div class=conv><a href="/chat/c1"><img src="__PIXEL__"></a><span>Weather in Shanghai</span></div>
  <div class=conv><a href="/chat/c2"><img src="__PIXEL__"></a><span>Trip planning</span></div>
  <div class=conv><a href="/chat/c3"><img src="__PIXEL__"></a><span>Unit conversion</span></div>
</nav>
<main>
  <div id=thread></div>
  <div id=composer contenteditable=true role=textbox aria-label="Enter a prompt here"
       data-placeholder="Enter a prompt here"></div>
  <span id=send-wrap><button id=send aria-label="Send message">&#10148;</button><div id=overlay></div></span>
  <button id=stop hidden>Stop</button>
  <div id=status>idle</div>
  <div id=attempts>attempts: 0</div>
</main>
<script>
var composer = document.getElementById('composer');
var statusEl = document.getElementById('status');
var stopBtn = document.getElementById('stop');
var timer = null;
var answers = 0;
var attempts = 0;
var ANSWER = 'Tomorrow in Shanghai: sunny, 13°C / 4°C, 10% chance of rain. A light jacket is enough.';
function send() {
  // Raw send attempts (every button click or Enter), counted before the guard below
  // so a double dispatch is visible even though only one request leaves the page.
  document.getElementById('attempts').textContent = 'attempts: ' + (++attempts);
  var text = composer.textContent.trim();
  if (!text || timer) { return; }
  composer.textContent = '';
  var user = document.createElement('div');
  user.className = 'msg user';
  user.textContent = text;
  document.getElementById('thread').appendChild(user);
  fetch('/chat/send', {method: 'POST', headers: {'Content-Type': 'application/json'},
                       body: JSON.stringify({text: text})});
  var answer = document.createElement('div');
  answer.className = 'msg model';
  answer.id = 'answer-' + (++answers);
  document.getElementById('thread').appendChild(answer);
  var words = ANSWER.split(' ');
  var shown = 0;
  statusEl.textContent = 'streaming';
  stopBtn.hidden = false;
  timer = setInterval(function() {
    shown += 3;
    answer.textContent = words.slice(0, shown).join(' ');
    if (shown >= words.length) { finish(); }
  }, __STREAM_MS__);
}
function finish() {
  clearInterval(timer);
  timer = null;
  stopBtn.hidden = true;
  statusEl.textContent = 'done';
}
stopBtn.addEventListener('click', finish);
composer.addEventListener('keydown', function(event) {
  if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); send(); }
});
document.getElementById('send').addEventListener('click', send);
</script>
</body></html>"""


_SHADOW_NESTED_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>fixture-shadow</title>
<style>
  body { font-family: sans-serif; margin: 24px; }
  .mask { position: fixed; inset: 0; background: rgba(0,0,0,0.4); z-index: 999; display: none; }
  .mask.show { display: block; }
</style>
</head>
<body>
<h1>fixture-shadow</h1>
<div id="light-status">ready</div>
<x-open-host id="open-host"></x-open-host>
<x-closed-outer id="closed-outer"></x-closed-outer>
<div id="light-mask" class="mask"></div>
<button id="toggle-mask" type="button">Toggle Mask</button>
<script>
// 1. Open Shadow DOM with readable text and a button
const openHost = document.getElementById('open-host');
const openRoot = openHost.attachShadow({mode: 'open'});
openRoot.innerHTML = `
  <div class="box">
    <p>Open shadow greeting</p>
    <button id="open-btn" type="button" style="padding:8px 16px;">Open Shadow Action</button>
    <span id="open-out">open:0</span>
  </div>
`;
let openCount = 0;
openRoot.getElementById('open-btn').addEventListener('click', () => {
  openCount += 1;
  openRoot.getElementById('open-out').textContent = 'open:' + openCount;
});

// 2. Nested closed Shadow DOM: outer -> inner -> contenteditable + submit button + occluded button
const outer = document.getElementById('closed-outer');
const outerRoot = outer.attachShadow({mode: 'closed'});
outerRoot.innerHTML = `
  <div style="padding:12px;border:1px solid #ccc;">
    <h2>Shadow Comments</h2>
    <x-closed-inner id="inner-host"></x-closed-inner>
  </div>
`;
const inner = outerRoot.getElementById('inner-host');
const innerRoot = inner.attachShadow({mode: 'closed'});
innerRoot.innerHTML = `
  <div style="position:relative;padding:8px;">
    <div id="shadow-editor" contenteditable="true" role="textbox" aria-label="Shadow Comment Box"
         style="min-height:36px;border:1px solid #888;padding:6px;"></div>
    <button id="shadow-post" type="button" style="margin-top:8px;padding:8px 16px;">Post Shadow Comment</button>
    <div id="shadow-log">posted:none</div>
    <div style="position:relative;display:inline-block;margin-top:8px;">
      <button id="shadow-covered" type="button" style="padding:8px 16px;">Covered Shadow Button</button>
      <div id="shadow-cover" style="position:absolute;inset:0;background:rgba(255,0,0,0.2);"></div>
    </div>
    <div id="covered-log">covered:0</div>
  </div>
`;
const editor = innerRoot.getElementById('shadow-editor');
const logEl = innerRoot.getElementById('shadow-log');
innerRoot.getElementById('shadow-post').addEventListener('click', () => {
  const val = (editor.innerText || '').trim();
  logEl.textContent = 'posted:' + val;
  document.getElementById('light-status').textContent = 'posted:' + val;
});
let cov = 0;
innerRoot.getElementById('shadow-covered').addEventListener('click', () => {
  cov += 1;
  innerRoot.getElementById('covered-log').textContent = 'covered:' + cov;
});

document.getElementById('toggle-mask').addEventListener('click', () => {
  document.getElementById('light-mask').classList.toggle('show');
});
</script>
</body></html>"""


_ASYNC_TOGGLE_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>fixture-async-toggle</title></head>
<body>
<h1>fixture-async-toggle</h1>
<form id="follow-form" onsubmit="return false;">
  <input id="follow-btn" type="submit" value="Follow" aria-label="Follow zhuyuloveai" style="padding:8px 16px;" />
</form>
<div id="follow-state">following:false</div>
<div id="modal-wrap" style="margin-top:16px;padding:12px;border:1px solid #888;">
  <p>Delete this comment?</p>
  <button id="confirm-del" type="button" style="padding:8px 16px;">确定</button>
</div>
<div id="delete-state">deleted:false</div>
<script>
const followBtn = document.getElementById('follow-btn');
followBtn.addEventListener('click', (e) => {
  e.preventDefault();
  setTimeout(() => {
    followBtn.value = 'Unfollow';
    followBtn.setAttribute('aria-label', 'Unfollow zhuyuloveai');
    document.getElementById('follow-state').textContent = 'following:true';
  }, 450);
});
document.getElementById('confirm-del').addEventListener('click', () => {
  document.getElementById('delete-state').textContent = 'deleted:true';
  const wrap = document.getElementById('modal-wrap');
  if (wrap) wrap.remove();
});
</script>
</body></html>"""


_DYNAMIC_COMMENTS_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>fixture-dynamic-comments</title></head>
<body>
<h1>fixture-dynamic-comments</h1>
<div id="ad-ticker">ad tick: 0</div>
<x-comments-area id="comments-area"></x-comments-area>
<script>
let ticks = 0;
setInterval(() => {
  ticks += 1;
  document.getElementById('ad-ticker').textContent = 'ad tick: ' + ticks;
}, 80);
const host = document.getElementById('comments-area');
const root = host.attachShadow({mode: 'closed'});
root.innerHTML = '<div id="loading">Loading comments...</div>';
setTimeout(() => {
  root.innerHTML = `
    <div>
      <div id="c-editor" contenteditable="true" role="textbox" aria-label="Dynamic Comment Input"
           style="min-height:32px;border:1px solid #888;padding:4px;"></div>
      <p>没有更多评论</p>
    </div>
  `;
}, 350);
</script>
</body></html>"""


_IFRAME_OFFSET_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>fixture-iframe-offset</title></head>
<body>
<h1>fixture-iframe-offset</h1>
<iframe id="offset-frame" src="/iframe-offset-child"
        style="position:absolute;left:250px;top:300px;width:260px;height:140px;border:2px solid #444;"></iframe>
</body></html>"""


_IFRAME_OFFSET_CHILD_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>fixture-iframe-offset-child</title></head>
<body>
<button id="offset-btn" type="button" style="padding:8px 16px;">Offset Frame Click</button>
<div id="offset-count">iframe-clicks:0</div>
<script>
let n = 0;
document.getElementById('offset-btn').addEventListener('click', () => {
  n += 1;
  document.getElementById('offset-count').textContent = 'iframe-clicks:' + n;
});
</script>
</body></html>"""


_IFRAME_RELOAD_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>Iframe reload</title></head>
<body>
<button type="button" onclick="document.querySelector('output').textContent='Main action done'">
  Stable main control
</button>
<button type="button" onclick="document.querySelector('iframe').src='/iframe-reload-replacement'">
  Replace embedded document
</button>
<output></output>
<iframe src="/iframe-offset-child" title="Embedded control"></iframe>
</body></html>"""


_IFRAME_BELOW_FOLD_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>fixture-iframe-below-fold</title></head>
<body style="margin:0;padding:0;">
<div style="height:950px;background:#eee;"><h1>fixture-iframe-below-fold</h1></div>
<iframe id="below-frame" src="/iframe-offset-child"
        style="display:block;margin-left:20px;width:300px;height:180px;border:4px solid #444;"></iframe>
<div style="height:600px;"></div>
</body></html>"""


_SMALL_SCROLLER_PAGE = (
    '<!doctype html><html><head><meta charset="utf-8"><title>fixture-small-scroller</title></head>'
    "<body><h1>fixture-small-scroller</h1>"
    '<div id="small-list" aria-label="Small Scrollable List" '
    'style="width:300px;height:200px;overflow:auto;border:1px solid #333;">'
    + "".join(f'<div class="row" style="height:24px;">Row {i}</div>' for i in range(1, 201))
    + "</div>"
    '<div id="non-scroller" style="width:300px;height:40px;border:1px solid #999;">'
    '<span id="static-item">Static Non-Scrollable Item</span></div>'
    "</body></html>"
)


#: Paging by characters must count an emoji (a UTF-16 surrogate pair) as one
#: character and never split it: the boundary emoji sits at index 199..200.
_EMOJI_TEXT_PAGE = (
    '<!doctype html><html><head><meta charset="utf-8"><title>fixture-emoji-text</title></head><body>'
    '<p id="short" aria-label="Short Paragraph">Hello \U0001f600</p>'
    '<p id="boundary" aria-label="Boundary Paragraph">' + "a" * 199 + "\U0001f600" + "tail-after-emoji</p>"
    "</body></html>"
)


#: A tall same-origin iframe inside a tall page: scrolling a target inside the
#: iframe must move the iframe's own window, never the outer page.
_SCROLL_IFRAME_PAGE = (
    '<!doctype html><html><head><meta charset="utf-8"><title>fixture-scroll-iframe</title></head>'
    '<body style="margin:0;height:3000px;"><h1>fixture-scroll-iframe</h1>'
    '<iframe id="tall-frame" style="width:400px;height:300px;border:0;" srcdoc="'
    "&lt;body style='margin:0;height:3000px;'&gt;&lt;button id='inner'&gt;Inner Frame Button&lt;/button&gt;&lt;/body&gt;"
    '"></iframe></body></html>'
)


#: target + to_text: the reported position must be read after to_text has
#: scrolled the panel to its last row, not after the first step.
_SCROLL_TO_TEXT_PAGE = (
    '<!doctype html><html><head><meta charset="utf-8"><title>fixture-scroll-to-text</title></head>'
    "<body><h1>fixture-scroll-to-text</h1>"
    '<div id="panel" aria-label="Scroll Panel" style="width:300px;height:100px;overflow:auto;border:1px solid #333;">'
    + "".join(f'<div style="height:20px;">Panel Row {i}</div>' for i in range(1, 51))
    + '<div style="height:20px;">Bottom Marker Row</div></div>'
    "</body></html>"
)


_LONG_LABEL_TEXT = (
    "Perform the comprehensive multi-step verification action for the quarterly financial audit report "
    "with extended descriptive context exceeding one hundred and twenty characters"
)


_LONG_LABEL_IFRAME_PAGE = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>fixture-long-label-iframe</title></head>
<body>
<h1>fixture-long-label-iframe</h1>
<iframe id="long-label-frame" style="width:420px;height:160px;border:1px solid #555;"
  srcdoc='<!doctype html><html><body>
    <button id="long-btn" type="button" aria-label="{_LONG_LABEL_TEXT}" style="padding:8px 14px;">Long Label Action</button>
    <div id="long-state">clicked:0</div>
    <script>
      let c = 0;
      document.getElementById("long-btn").addEventListener("click", () => {{
        c += 1;
        document.getElementById("long-state").textContent = "clicked:" + c;
      }});
    </script>
  </body></html>'></iframe>
</body></html>"""


_BUTTON_NO_FORM_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>fixture-button-no-form</title></head>
<body>
<h1>fixture-button-no-form</h1>
<button id="menu-btn" style="padding:8px 16px;">Menu</button>
<div id="menu-state">menu:closed</div>
<script>
document.getElementById('menu-btn').addEventListener('click', () => {
  document.getElementById('menu-state').textContent = 'menu:open';
});
</script>
</body></html>"""


_LOGIN_HIDDEN_INPUTS_PAGE = (
    '<!doctype html><html><head><meta charset="utf-8"><title>fixture-login-hidden</title></head>'
    '<body><h1>Sign in</h1><form method="post" action="/submit">'
    + "".join(f'<input type="hidden" name="csrf_{i}" value="tok_{i}">' for i in range(11))
    + '<input type="text" name="username" placeholder="Username">'
    + '<input type="password" name="password" placeholder="Password">'
    + '<button type="submit">Sign In</button>'
    + "</form></body></html>"
)


_SLOT_NESTED_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>fixture-slot-nested</title></head>
<body>
<h1>fixture-slot-nested</h1>
<my-slot-host id="slot-host"><div class="wrap"><span id="slot-label">SlottedSubmit</span></div></my-slot-host>
<div id="slot-state">slot-clicks:0</div>
<script>
const host = document.getElementById('slot-host');
const sr = host.attachShadow({mode: 'open'});
sr.innerHTML = '<button id="slot-btn" type="button" style="padding:10px 18px;"><slot></slot></button>';
let count = 0;
sr.getElementById('slot-btn').addEventListener('click', () => {
  count += 1;
  document.getElementById('slot-state').textContent = 'slot-clicks:' + count;
});
</script>
</body></html>"""


_P_EDITOR_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>fixture-p-editor</title></head>
<body>
<h1>fixture-p-editor</h1>
<div id="p-editor" contenteditable="true" role="textbox" aria-label="Paragraph Editor"
     style="width:320px;min-height:90px;border:1px solid #555;padding:8px;"><p>first</p><p>second</p></div>
</body></html>"""


_DIALOG_ON_MOUSEDOWN_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>fixture-dialog-on-mousedown</title></head>
<body>
<h1>fixture-dialog-on-mousedown</h1>
<button id="md-alert-btn" type="button" style="padding:10px 16px;">Trigger Mousedown Alert</button>
<button id="after-btn" type="button" style="padding:10px 16px;margin-left:12px;">After Dialog Button</button>
<div id="md-state">mousedown:0 mouseup:0 after:0</div>
<script>
let md = 0, mu = 0, af = 0;
function render() {
  document.getElementById('md-state').textContent = `mousedown:${md} mouseup:${mu} after:${af}`;
}
const btn = document.getElementById('md-alert-btn');
btn.addEventListener('mousedown', () => {
  md += 1;
  render();
  window.alert('mousedown dialog');
});
btn.addEventListener('mouseup', () => {
  mu += 1;
  render();
});
document.getElementById('after-btn').addEventListener('click', () => {
  af += 1;
  render();
});
</script>
</body></html>"""


class _Handler(BaseHTTPRequestHandler):
    server_version = "chrys-fixture"

    def _write(self, body: bytes) -> None:
        try:
            self.wfile.write(body)
        except (ConnectionError, TimeoutError):
            # The browser aborted the request (for example a denied download).
            return

    def _send(self, status: int, body: bytes, content_type: str = "text/html; charset=utf-8") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self._write(body)

    def _json(self, payload: object) -> None:
        self._send(200, json.dumps(payload).encode(), "application/json")

    def _clamp(self, query: str, name: str, default: int) -> int:
        limit: int = self.server.max_delay_ms  # type: ignore[attr-defined]
        for part in query.split("&"):
            key, _, value = part.partition("=")
            if key == name and value.isdigit():
                return min(int(value), limit)
        return min(default, limit)

    def _download_file(self, query: str) -> None:
        try:
            size = int(_query_value(query, "bytes") or 4096)
        except ValueError:
            size = 4096
        size = min(max(size, 0), 5 * 1024 * 1024)
        delay_ms = self._clamp(query, "delay_ms", 0)
        name = (_query_value(query, "filename") or "fixture-download.bin").replace("\r", "").replace("\n", "")
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Disposition", f'attachment; filename="{name}"')
        self.send_header("Content-Length", str(size))
        self.end_headers()
        chunk = b"x" * 16384
        remaining = size
        try:
            while remaining > 0:
                part = chunk[: min(len(chunk), remaining)]
                self.wfile.write(part)
                self.wfile.flush()
                remaining -= len(part)
                if delay_ms:
                    time.sleep(delay_ms / 1000)
        except (ConnectionError, TimeoutError):
            # The browser canceled the transfer (size limit or browser_download_cancel).
            return

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
        state: FixtureState = self.server.state  # type: ignore[attr-defined]
        path, _, query = self.path.partition("?")
        if path == "/state":
            # Reading the counters must not itself change them.
            self._json(state.snapshot())
            return
        state.note(f"get {path}", self.path)
        if path == "/":
            frame_src = self.server.iframe_origin  # type: ignore[attr-defined]
            oauth_src = (frame_src or "") + "/oauth/start"
            body = _FORM_PAGE.replace("__IFRAME_SRC__", frame_src + "/iframe").replace("__OAUTH_SRC__", oauth_src)
            self._send(200, body.encode())
        elif path == "/iframe":
            self._send(200, _IFRAME_PAGE.encode())
        elif path == "/iframe-click":
            state.note("iframe", self.path)
            self._send(204, b"")
        elif path == "/dom":
            delay = self._clamp(query, "delay", 0)
            self._send(200, _DOM_PAGE.format(delay=delay).encode())
        elif path == "/controls":
            delay = self._clamp(query, "delay", 300)
            self._send(200, _CONTROLS_PAGE.format(delay=delay).encode())
        elif path == "/dense":
            self._send(200, _DENSE_PAGE.encode())
        elif path == "/shadow-nested":
            self._send(200, _SHADOW_NESTED_PAGE.encode())
        elif path == "/async-toggle":
            self._send(200, _ASYNC_TOGGLE_PAGE.encode())
        elif path == "/dynamic-comments":
            self._send(200, _DYNAMIC_COMMENTS_PAGE.encode())
        elif path == "/iframe-offset":
            self._send(200, _IFRAME_OFFSET_PAGE.encode())
        elif path == "/iframe-offset-child":
            self._send(200, _IFRAME_OFFSET_CHILD_PAGE.encode())
        elif path == "/iframe-reload":
            self._send(200, _IFRAME_RELOAD_PAGE.encode())
        elif path == "/iframe-reload-replacement":
            self._send(200, b"<!doctype html><button>Replacement child control</button>")
        elif path == "/iframe-below-fold":
            self._send(200, _IFRAME_BELOW_FOLD_PAGE.encode())
        elif path == "/small-scroller":
            self._send(200, _SMALL_SCROLLER_PAGE.encode())
        elif path == "/emoji-text":
            self._send(200, _EMOJI_TEXT_PAGE.encode())
        elif path == "/scroll-iframe":
            self._send(200, _SCROLL_IFRAME_PAGE.encode())
        elif path == "/scroll-to-text":
            self._send(200, _SCROLL_TO_TEXT_PAGE.encode())
        elif path == "/huge-node":
            raw_mb = _query_value(query, "mb") or "34"
            mb = min(max(int(raw_mb) if raw_mb.isdigit() else 34, 1), 40)
            payload = (
                b'<!doctype html><html><head><meta charset="utf-8"><title>fixture-huge-node</title></head>'
                b'<body><h1>fixture-huge-node</h1><button id="huge-ok" type="button">Huge Ready</button>'
                b'<script type="text/plain" id="huge-blob">' + (b"A" * (mb * 1024 * 1024)) + b"</script></body></html>"
            )
            self._send(200, payload)
        elif path == "/long-label-iframe":
            self._send(200, _LONG_LABEL_IFRAME_PAGE.encode())
        elif path == "/button-no-form":
            self._send(200, _BUTTON_NO_FORM_PAGE.encode())
        elif path == "/login-hidden-inputs":
            self._send(200, _LOGIN_HIDDEN_INPUTS_PAGE.encode())
        elif path == "/slot-nested":
            self._send(200, _SLOT_NESTED_PAGE.encode())
        elif path == "/p-editor":
            self._send(200, _P_EDITOR_PAGE.encode())
        elif path == "/dialog-on-mousedown":
            self._send(200, _DIALOG_ON_MOUSEDOWN_PAGE.encode())
        elif path == "/dialogs":
            self._send(200, _DIALOGS_PAGE.encode())
        elif path == "/dialog-result":
            # What the page saw once a dialog closed: proof of accept/dismiss/prompt text.
            state.note(f"dialog {_query_value(query, 'event')}", self.path)
            self._send(204, b"")
        elif path == "/slow":
            time.sleep(self._clamp(query, "ms", 100) / 1000)
            self._send(200, b"<!doctype html><title>fixture-slow</title><h1>fixture-slow</h1>")
        elif path == "/slow-image":
            self._send(
                200,
                (
                    b'<!doctype html><html><head><meta charset="utf-8"><title>Slow Image Ready</title></head>'
                    b'<body><main><h1>Readable Before Image</h1><p id="ready-body">Main copy ready now.</p>'
                    b'<img src="/hang-image?ms=9000" alt="Slow Banner" width="40" height="20"></main></body></html>'
                ),
            )
        elif path == "/hang-image":
            time.sleep(self._clamp(query, "ms", 9000) / 1000)
            self._send(200, b"", content_type="image/gif")
        elif path == "/redirect-nav":
            self.send_response(302)
            self.send_header("Location", "/spa-delayed")
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif path == "/spa-delayed":
            self._send(
                200,
                (
                    b'<!doctype html><html><head><meta charset="utf-8"><title>SPA Delayed</title></head>'
                    b'<body><main><div id="spa-out">Loading...</div></main>'
                    b"<script>setTimeout(()=>{document.getElementById('spa-out').textContent='SPA HYDRATED OK';},150);</script>"
                    b"</body></html>"
                ),
            )
        elif path == "/extract-cards":
            self._send(
                200,
                (
                    b'<!doctype html><html><head><meta charset="utf-8"><title>Extract Cards</title></head>'
                    b'<body><nav><a href="#content">Skip to Main Content</a><span>EN</span><span>0</span></nav>'
                    b'<main id="content">'
                    b"<button type=\"button\" onclick=\"document.getElementById('flt').textContent='FILTERS_OPEN'\">"
                    b"<span>Filters</span></button>"
                    b'<div id="flt">CLOSED</div>'
                    b'<input id="space-box" type="checkbox" aria-label="Accept Terms" />'
                    b'<img src="data:image/gif;base64,R0lGODlhAQABAAAAACw=" alt="Catalog Hero" width="40" height="20">'
                    b'<a href="#p1"><h3><span>Product Card Alpha</span></h3></a>'
                    b'<a href="#p2"><h3><span>Product Card Beta</span></h3></a>'
                    b'<a href="#p3"><h3><span>Product Card Gamma</span></h3></a>'
                    b"</main></body></html>"
                ),
            )
        elif path == "/tall":
            # A document taller than any test viewport: full_page screenshots must grow.
            self._send(200, _TALL_PAGE.encode())
        elif path == "/popup":
            self._send(200, _POPUP_PAGE.encode())
        elif path == "/popup-target":
            self._send(200, _POPUP_TARGET_PAGE.encode())
        elif path == "/oauth/start":
            self._send(200, _OAUTH_START_PAGE.encode())
        elif path == "/oauth/authorize":
            self._send(200, _OAUTH_AUTHORIZE_PAGE.encode())
        elif path == "/oauth/callback":
            self._send(200, _OAUTH_CALLBACK_PAGE.encode())
        elif path == "/noopener-open":
            self._send(200, _NOOPENER_PAGE.encode())
        elif path == "/download":
            body = b"fixture-download-payload"
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Disposition", 'attachment; filename="fixture.txt"')
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self._write(body)
        elif path == "/download-file":
            self._download_file(query)
        elif path == "/download-links":
            self._send(200, _DOWNLOAD_LINKS_PAGE.encode())
        elif path == "/chat" or (path.startswith("/chat/c") and path[7:].isdigit()):
            conv = path[6:] or "home"
            stream_ms = self._clamp(query, "stream_ms", 300)
            body = _CHAT_PAGE.replace("__CONV__", conv).replace("__PIXEL__", _PIXEL)
            self._send(200, body.replace("__STREAM_MS__", str(stream_ms)).encode())
        elif path == "/api":
            self._json({"ok": True})
        else:
            self._send(404, b"not found")

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
        state: FixtureState = self.server.state  # type: ignore[attr-defined]
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._send(400, b"bad content length")
            return
        if length < 0 or length > MAX_UPLOAD_BYTES:
            self._send(413, b"body too large")
            return
        body = self.rfile.read(length) if length else b""
        path = self.path.partition("?")[0]
        if path == "/submit":
            count = state.note("submit", self.path)
            self._send(200, f"<!doctype html><title>submitted</title><h1>submitted {count}</h1>".encode())
        elif path == "/upload":
            count = state.note("upload", self.path)
            name = "unknown"
            for part in body.split(b"\r\n"):
                if b'filename="' in part:
                    name = part.split(b'filename="', 1)[1].split(b'"', 1)[0].decode("utf-8", "replace")
                    break
            payload = body.split(b"\r\n\r\n", 1)[1] if b"\r\n\r\n" in body else b""
            payload = payload.rsplit(b"\r\n--", 1)[0]
            self._send(
                200,
                f"<!doctype html><h1>uploaded {html.escape(name)} {len(payload)} {count}</h1>".encode(),
            )
        elif path == "/chat/send":
            # The only honest proof that a chat message went out exactly once.
            self._json({"ok": True, "count": state.note("chat_submits", self.path)})
        else:
            self._send(404, b"not found")

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - base signature
        return


class FixtureSite(ThreadingHTTPServer):
    daemon_threads = True
    state: FixtureState
    iframe_origin: str
    max_delay_ms: int

    def handle_error(self, request: object, client_address: object) -> None:
        # A browser aborting a request (denied download, navigation away) is normal;
        # anything else is a real defect and must stay visible.
        error = sys.exc_info()[1]
        if isinstance(error, (ConnectionError, TimeoutError)):
            return
        super().handle_error(request, client_address)  # type: ignore[arg-type]


@contextmanager
def serve(
    host: str = "127.0.0.1",
    *,
    public_host: str | None = None,
    iframe_origin: str = "",
    max_delay_ms: int = MAX_DELAY_MS,
    poll_interval: float = 0.05,
) -> Iterator[tuple[str, FixtureState]]:
    """Run the fixture on an ephemeral port; yield ``(origin, state)``.

    *public_host* only changes the host name in the returned origin (bind stays
    on *host*). Pass the second server's origin as *iframe_origin* on the first
    one to embed a different-site frame -- that, not *public_host*, is what makes
    the child an OOPIF.

    *max_delay_ms* clamps every fixture delay (``/slow``, ``/dom``, ``/controls``, ``/chat`` ``stream_ms``, downloads); *poll_interval* shortens the
    server's shutdown latency (the default 0.5 s per server adds up in tests).
    """
    frame_origin = _frame_origin(iframe_origin)

    class Bound(FixtureSite):
        address_family = socket.AF_INET6 if ":" in host else socket.AF_INET

    server = Bound((host, 0), _Handler)
    server.state = FixtureState()
    server.iframe_origin = frame_origin
    server.max_delay_ms = max(0, max_delay_ms)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": poll_interval}, daemon=True)
    thread.start()
    advertised = public_host or host
    display = f"[{advertised}]" if ":" in advertised else advertised
    try:
        yield f"http://{display}:{server.server_address[1]}", server.state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
