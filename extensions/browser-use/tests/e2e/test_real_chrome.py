# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Opt-in real-Chrome regression tests for browser-use.

Run explicitly with::

    CHRYS_BROWSER_E2E=1 pytest tests/e2e/test_real_chrome.py -n0

Skipped by default so unit CI runs stay fast and never launch Chrome.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import sys
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

pytestmark = pytest.mark.skipif(
    os.environ.get("CHRYS_BROWSER_E2E") != "1",
    reason="Real-Chrome E2E tests require CHRYS_BROWSER_E2E=1",
)

_REF_RE = re.compile(r"@e(\d+)#(\d+)")
_ORIG_USERPROFILE = os.environ.get("USERPROFILE")
_ORIG_HOME = os.environ.get("HOME")
_ORIG_APPDATA = os.environ.get("APPDATA")
_ORIG_LOCALAPPDATA = os.environ.get("LOCALAPPDATA")


@contextlib.asynccontextmanager
async def _chrome_session(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[tuple[str, object, object, str]]:
    """Launch a fixture server and isolated real Owned Chrome handle."""
    from chrys.extensions import ToolContext
    from fixture_site import serve
    from smoke_common import chrome_executable

    import icode_browser_use.extension as ext

    with tempfile.TemporaryDirectory(prefix="browser-use-e2e-", ignore_cleanup_errors=True) as td:
        root = Path(td).resolve()
        (root / "workspace").mkdir()
        with serve() as (origin, state):
            if _ORIG_USERPROFILE is not None:
                monkeypatch.setenv("USERPROFILE", _ORIG_USERPROFILE)
            if _ORIG_HOME is not None:
                monkeypatch.setenv("HOME", _ORIG_HOME)
            if _ORIG_APPDATA is not None:
                monkeypatch.setenv("APPDATA", _ORIG_APPDATA)
            if _ORIG_LOCALAPPDATA is not None:
                monkeypatch.setenv("LOCALAPPDATA", _ORIG_LOCALAPPDATA)
            monkeypatch.setenv("ICODE_BROWSER_ALLOWED_ORIGINS", origin)
            monkeypatch.setenv("ICODE_BROWSER_STATE_DIR", str(root / "state"))
            monkeypatch.setenv("ICODE_BROWSER_HEADLESS", "1")
            monkeypatch.setenv("ICODE_BROWSER_IDLE_CLOSE_SECONDS", "0")
            monkeypatch.setenv("ICODE_BROWSER_EXECUTABLE", str(chrome_executable()))
            ctx = ToolContext.for_testing(
                root / "workspace",
                data_dir=root / "data",
                session_data_dir=root / "session",
                agent_name="e2e-chrome",
                session_id="e2e-chrome",
            )
            opened = await ext.browser_open(ctx)
            browser_id = opened.split(": ", 1)[1].splitlines()[0].strip()
            try:
                yield origin, state, ctx, browser_id
            finally:
                if browser_id in ext._browsers:
                    with contextlib.suppress(Exception):
                        await ext.browser_close(ctx, browser_id)


def test_real_chrome_offset_iframe_click(monkeypatch: pytest.MonkeyPatch) -> None:
    import icode_browser_use.extension as ext

    async def scenario() -> None:
        async with _chrome_session(monkeypatch) as (origin, _state, ctx, bid):
            await ext.browser_navigate(ctx, f"{origin}/iframe-offset", bid)
            found = await ext.browser_find(ctx, bid, "Offset Frame Click")
            match = _REF_RE.search(found)
            assert match is not None, found
            clicked = await ext.browser_click(ctx, bid, match.group(0))
            assert "dispatch=sent" in clicked, clicked
            snap = await ext.browser_snapshot(ctx, bid)
            assert "iframe-clicks:1" in snap, snap

    asyncio.run(scenario())


def test_real_chrome_small_scroller_marker_and_step(monkeypatch: pytest.MonkeyPatch) -> None:
    import icode_browser_use.extension as ext

    async def scenario() -> None:
        async with _chrome_session(monkeypatch) as (origin, _state, ctx, bid):
            await ext.browser_navigate(ctx, f"{origin}/small-scroller", bid)
            sess = ext._browsers[bid].session
            scrollable_nodes = [n for n in sess.snapshot.nodes if n.scrollable]
            assert scrollable_nodes, "expected small overflow:auto container to be marked scrollable"
            list_node = next(
                (n for n in scrollable_nodes if "Small Scrollable List" in (n.name or "")),
                scrollable_nodes[0],
            )
            tok = f"@e{list_node.ref}#{list_node.epoch}"
            scrolled = await ext.browser_scroll(ctx, bid, direction="down", amount=1, target=tok)
            assert "y=" in scrolled, scrolled
            # 200px container * 0.85 should move ~170px, not jump straight to ~800px
            m = re.search(r"y=(\d+(?:\.\d+)?)", scrolled)
            assert m is not None and 50 <= float(m.group(1)) <= 300, scrolled
            # Non-scrollable item should truthfully report no movement
            static_find = await ext.browser_find(ctx, bid, "Static Non-Scrollable Item")
            static_ref = _REF_RE.search(static_find)
            assert static_ref is not None, static_find
            no_move = await ext.browser_scroll(ctx, bid, direction="down", amount=1, target=static_ref.group(0))
            assert "未滚动" in no_move or "y=0" in no_move, no_move

    asyncio.run(scenario())


def test_real_chrome_huge_node_survives_navigate(monkeypatch: pytest.MonkeyPatch) -> None:
    import icode_browser_use.extension as ext

    async def scenario() -> None:
        async with _chrome_session(monkeypatch) as (origin, _state, ctx, bid):
            result = await ext.browser_navigate(ctx, f"{origin}/huge-node", bid)
            assert f"browser_id: {bid}" in result, result
            # Connection must remain alive for subsequent actions
            nav2 = await ext.browser_navigate(ctx, f"{origin}/button-no-form", bid)
            assert "Menu" in nav2, nav2

    asyncio.run(scenario())


def test_real_chrome_long_label_iframe_not_stale(monkeypatch: pytest.MonkeyPatch) -> None:
    import icode_browser_use.extension as ext

    async def scenario() -> None:
        async with _chrome_session(monkeypatch) as (origin, _state, ctx, bid):
            await ext.browser_navigate(ctx, f"{origin}/long-label-iframe", bid)
            found = await ext.browser_find(ctx, bid, "quarterly financial audit")
            match = _REF_RE.search(found)
            assert match is not None, found
            clicked = await ext.browser_click(ctx, bid, match.group(0))
            assert "dispatch=sent" in clicked, clicked
            snap = await ext.browser_snapshot(ctx, bid)
            assert "clicked:1" in snap, snap

    asyncio.run(scenario())


def test_real_chrome_bare_button_outside_form_not_submit_like(monkeypatch: pytest.MonkeyPatch) -> None:
    import icode_browser_use.extension as ext
    from icode_browser_use.actions.actionability import target_facts
    from icode_browser_use.actions.click import _is_submit_like

    async def scenario() -> None:
        async with _chrome_session(monkeypatch) as (origin, _state, ctx, bid):
            await ext.browser_navigate(ctx, f"{origin}/button-no-form", bid)
            sess = ext._browsers[bid].session
            entry = sess.resolve_target("Menu")
            oid = await sess.resolve_object_id(entry)
            facts = await target_facts(sess, oid, entry.cdp_session_id)
            assert facts.get("inForm") is False
            assert _is_submit_like(facts) is False

    asyncio.run(scenario())


def test_real_chrome_login_hidden_inputs_detected(monkeypatch: pytest.MonkeyPatch) -> None:
    from chrys.extensions import ToolError

    import icode_browser_use.extension as ext

    async def scenario() -> None:
        async with _chrome_session(monkeypatch) as (origin, _state, ctx, bid):
            with pytest.raises(ToolError) as exc_info:
                await ext.browser_navigate(ctx, f"{origin}/login-hidden-inputs", bid)
            assert exc_info.value.code == "browser_login_required"

    asyncio.run(scenario())


def test_real_chrome_nested_slot_click(monkeypatch: pytest.MonkeyPatch) -> None:
    import icode_browser_use.extension as ext

    async def scenario() -> None:
        async with _chrome_session(monkeypatch) as (origin, _state, ctx, bid):
            await ext.browser_navigate(ctx, f"{origin}/slot-nested", bid)
            found = await ext.browser_find(ctx, bid, "SlottedSubmit", role="button")
            match = _REF_RE.search(found)
            assert match is not None, found
            clicked = await ext.browser_click(ctx, bid, match.group(0))
            assert "dispatch=sent" in clicked, clicked
            extracted = await ext.browser_extract(ctx, bid)
            assert "slot-clicks:1" in extracted, extracted

    asyncio.run(scenario())


def test_real_chrome_paragraph_contenteditable_fill(monkeypatch: pytest.MonkeyPatch) -> None:
    import icode_browser_use.extension as ext

    async def scenario() -> None:
        async with _chrome_session(monkeypatch) as (origin, _state, ctx, bid):
            await ext.browser_navigate(ctx, f"{origin}/p-editor", bid)
            found = await ext.browser_find(ctx, bid, "Paragraph Editor")
            match = _REF_RE.search(found)
            assert match is not None, found
            filled = await ext.browser_fill(ctx, bid, match.group(0), " third", clear=False)
            assert "status=succeeded" in filled, filled

    asyncio.run(scenario())


def test_real_chrome_dialog_on_mousedown_cancels_trailing_mouseup(monkeypatch: pytest.MonkeyPatch) -> None:
    import icode_browser_use.extension as ext

    async def scenario() -> None:
        async with _chrome_session(monkeypatch) as (origin, _state, ctx, bid):
            await ext.browser_navigate(ctx, f"{origin}/dialog-on-mousedown", bid)
            blocked = await ext.browser_click(ctx, bid, "Trigger Mousedown Alert")
            assert "status=blocked" in blocked, blocked
            handled = await ext.browser_dialog(ctx, bid, "accept")
            assert "accepted" in handled or "接受" in handled, handled
            text = await ext.browser_extract(ctx, bid)
            assert "mousedown:1 mouseup:0" in text, text
            # Next click on another button must work cleanly without stuck mouse state
            after = await ext.browser_click(ctx, bid, "After Dialog Button")
            assert "dispatch=sent" in after, after
            text2 = await ext.browser_extract(ctx, bid)
            assert "after:1" in text2, text2

    asyncio.run(scenario())


def test_real_chrome_below_fold_iframe_viewport_after_scroll(monkeypatch: pytest.MonkeyPatch) -> None:
    import icode_browser_use.extension as ext

    async def scenario() -> None:
        async with _chrome_session(monkeypatch) as (origin, _state, ctx, bid):
            await ext.browser_navigate(ctx, f"{origin}/iframe-below-fold", bid)
            sess = ext._browsers[bid].session
            btn_before = next(
                (n for n in sess.snapshot.nodes if "Offset Frame Click" in (n.name or n.text_hint or "")),
                None,
            )
            assert btn_before is not None, "expected iframe button in snapshot"
            assert btn_before.in_viewport is False, "button below fold must NOT be in_viewport before scroll"

            # Scroll down so the iframe (at y≈950) enters the viewport
            await ext.browser_scroll(ctx, bid, direction="down", amount=2)
            await ext.browser_snapshot(ctx, bid)
            btn_after = next(
                (n for n in sess.snapshot.nodes if "Offset Frame Click" in (n.name or n.text_hint or "")),
                None,
            )
            assert btn_after is not None, "expected iframe button in snapshot after scroll"
            assert btn_after.in_viewport is True, "button inside scrolled-in iframe MUST be in_viewport after scroll"

    asyncio.run(scenario())


def test_real_chrome_extract_pages_count_emoji_as_one_character(monkeypatch: pytest.MonkeyPatch) -> None:
    import icode_browser_use.extension as ext

    async def scenario() -> None:
        async with _chrome_session(monkeypatch) as (origin, _state, ctx, bid):
            await ext.browser_navigate(ctx, f"{origin}/emoji-text", bid)
            short = _REF_RE.search(await ext.browser_find(ctx, bid, "Short Paragraph"))
            assert short is not None
            whole = await ext.browser_extract(ctx, bid, query=short.group(0), max_chars=200)
            assert whole == "Hello \U0001f600", whole  # fits in one page: no "next offset" hint
            # 199×'a' then the emoji straddling the 200-character page edge.
            found = _REF_RE.search(await ext.browser_find(ctx, bid, "Boundary Paragraph"))
            assert found is not None
            first = await ext.browser_extract(ctx, bid, query=found.group(0), max_chars=200)
            assert first.startswith("a" * 199 + "\U0001f600"), first[190:]
            match = re.search(r"next offset=(\d+)", first)
            assert match is not None and match.group(1) == "200", first[190:]
            second = await ext.browser_extract(ctx, bid, query=found.group(0), max_chars=200, offset=200)
            assert second == "tail-after-emoji", second
            for text in (first, second):
                text.encode("utf-8")  # never a lone surrogate
            page = await ext.browser_extract(ctx, bid, max_chars=200)
            page.encode("utf-8")
            page_next = re.search(r"next offset=(\d+)", page)
            assert page_next is not None
            rest = await ext.browser_extract(ctx, bid, max_chars=200, offset=int(page_next.group(1)))
            rest.encode("utf-8")
            assert "tail-after-emoji" in page + rest

    asyncio.run(scenario())


def test_real_chrome_scroll_moves_the_targets_own_frame_window(monkeypatch: pytest.MonkeyPatch) -> None:
    import icode_browser_use.extension as ext

    async def scenario() -> None:
        async with _chrome_session(monkeypatch) as (origin, _state, ctx, bid):
            await ext.browser_navigate(ctx, f"{origin}/scroll-iframe", bid)
            found = _REF_RE.search(await ext.browser_find(ctx, bid, "Inner Frame Button"))
            assert found is not None
            scrolled = await ext.browser_scroll(ctx, bid, direction="down", amount=1, target=found.group(0))
            sess = ext._browsers[bid].session
            client = sess.require_client()
            outer = await client.send(
                "Runtime.evaluate",
                {
                    "expression": "[window.scrollY, document.getElementById('tall-frame').contentWindow.scrollY]",
                    "returnByValue": True,
                },
                session_id=sess.session_id,
                timeout=5,
            )
            outer_y, frame_y = outer["result"]["value"]
            assert outer_y == 0, f"outer page must not move: {scrolled}"
            assert frame_y > 0, f"the iframe's own window must move: {scrolled}"
            assert f"y={frame_y}" in scrolled, scrolled

    asyncio.run(scenario())


def test_real_chrome_scroll_reports_position_after_to_text(monkeypatch: pytest.MonkeyPatch) -> None:
    import icode_browser_use.extension as ext

    async def scenario() -> None:
        async with _chrome_session(monkeypatch) as (origin, _state, ctx, bid):
            await ext.browser_navigate(ctx, f"{origin}/scroll-to-text", bid)
            found = _REF_RE.search(await ext.browser_find(ctx, bid, "Scroll Panel"))
            assert found is not None
            bottom = _REF_RE.search(await ext.browser_find(ctx, bid, "Bottom Marker Row"))
            assert bottom is not None
            scrolled = await ext.browser_scroll(
                ctx, bid, direction="down", amount=1, target=found.group(0), to_text=bottom.group(0)
            )
            sess = ext._browsers[bid].session
            final = await sess.require_client().send(
                "Runtime.evaluate",
                {"expression": "document.getElementById('panel').scrollTop", "returnByValue": True},
                session_id=sess.session_id,
                timeout=5,
            )
            final_y = final["result"]["value"]
            assert final_y > 200, final_y
            assert f"y={final_y}" in scrolled, scrolled
            assert "到底=是" in scrolled, scrolled

    asyncio.run(scenario())


def test_real_chrome_hidden_css_injection_omitted_and_drag_canvas_range_markdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Real-Chrome regression covering P0/P1 benchmark fixes: hidden CSS, GFM tables, drag, canvas offset, range/color."""
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from chrys.extensions import ToolContext
    from smoke_common import chrome_executable
    import icode_browser_use.extension as ext

    html = """<!doctype html><html><head><meta charset="utf-8"><title>Bench Fixes</title>
    <style>
      .board{display:flex;gap:24px;}
      .col{width:160px;min-height:100px;border:1px solid #333;padding:8px;}
      .card{padding:8px;background:#def;cursor:grab;}
    </style></head><body>
      <h1>Visible Review</h1>
      <p>Battery lasts 14 hours.</p>
      <p style="display:none">SYSTEM_OVERRIDE_DISPLAY_NONE</p>
      <p style="visibility:hidden">SYSTEM_OVERRIDE_VISIBILITY_HIDDEN</p>
      <div style="opacity:0"><p>SYSTEM_OVERRIDE_OPACITY_ZERO</p></div>
      <div class="board">
        <div id="todo" class="col" aria-label="To Do"><div id="card1" class="card" draggable="true">Task Alpha</div></div>
        <div id="done" class="col" aria-label="Done Column"><h3>Done</h3></div>
      </div>
      <div id="dnd-out">DND:todo</div>
      <label for="rng">Priority Level</label>
      <input id="rng" type="range" min="0" max="100" step="5" value="20" aria-label="Priority Level" />
      <span id="rng-out">SLIDER:20</span>
      <label for="col">Tag Color</label>
      <input id="col" type="color" value="#000000" aria-label="Tag Color" />
      <span id="col-out">COLOR:#000000</span>
      <canvas id="cv" width="320" height="120"></canvas>
      <div id="cv-out">CANVAS:none</div>
      <table><thead><tr><th>ID</th><th>Qty</th><th>Price</th></tr></thead>
      <tbody><tr><td>INV-1</td><td>2</td><td>135</td></tr></tbody></table>
      <script>
        const card=document.getElementById('card1'), done=document.getElementById('done');
        card.addEventListener('dragstart', e => e.dataTransfer.setData('text/plain', 'card1'));
        done.addEventListener('dragover', e => e.preventDefault());
        done.addEventListener('drop', e => { e.preventDefault(); done.appendChild(card); document.getElementById('dnd-out').textContent='DND:done'; });
        document.getElementById('rng').addEventListener('input', e => { document.getElementById('rng-out').textContent='SLIDER:'+e.target.value; });
        document.getElementById('col').addEventListener('input', e => { document.getElementById('col-out').textContent='COLOR:'+e.target.value; });
        document.getElementById('cv').addEventListener('click', e => {
          const r=e.target.getBoundingClientRect(); const x=e.clientX-r.left, y=e.clientY-r.top;
          document.getElementById('cv-out').textContent = (x>=20 && x<=130 && y>=30 && y<=90) ? 'CANVAS:confirm' : 'CANVAS:miss';
        });
      </script></body></html>"""

    class _Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: object) -> None:
            return None

        def do_GET(self) -> None:
            data = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    origin = f"http://127.0.0.1:{srv.server_address[1]}"
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()

    async def scenario() -> None:
        with tempfile.TemporaryDirectory(prefix="browser-use-fixes-e2e-", ignore_cleanup_errors=True) as td:
            root = Path(td).resolve()
            (root / "ws").mkdir()
            if _ORIG_USERPROFILE is not None:
                monkeypatch.setenv("USERPROFILE", _ORIG_USERPROFILE)
            if _ORIG_HOME is not None:
                monkeypatch.setenv("HOME", _ORIG_HOME)
            if _ORIG_APPDATA is not None:
                monkeypatch.setenv("APPDATA", _ORIG_APPDATA)
            if _ORIG_LOCALAPPDATA is not None:
                monkeypatch.setenv("LOCALAPPDATA", _ORIG_LOCALAPPDATA)
            monkeypatch.setenv("ICODE_BROWSER_ALLOWED_ORIGINS", origin)
            monkeypatch.setenv("ICODE_BROWSER_STATE_DIR", str(root / "state"))
            monkeypatch.setenv("ICODE_BROWSER_HEADLESS", "1")
            monkeypatch.setenv("ICODE_BROWSER_IDLE_CLOSE_SECONDS", "0")
            monkeypatch.setenv("ICODE_BROWSER_EXECUTABLE", str(chrome_executable()))
            ctx = ToolContext.for_testing(
                root / "ws",
                data_dir=root / "data",
                session_data_dir=root / "session",
                agent_name="e2e-fixes",
                session_id="e2e-fixes",
            )
            nav = await ext.browser_navigate(ctx, f"{origin}/")
            bid = nav.splitlines()[0].split("browser_id: ", 1)[1].strip()
            try:
                # 1. Hidden CSS prompt injections excluded from snapshot & extract
                for secret in (
                    "SYSTEM_OVERRIDE_DISPLAY_NONE",
                    "SYSTEM_OVERRIDE_VISIBILITY_HIDDEN",
                    "SYSTEM_OVERRIDE_OPACITY_ZERO",
                ):
                    assert secret not in nav, nav
                page_txt = await ext.browser_extract(ctx, bid)
                for secret in (
                    "SYSTEM_OVERRIDE_DISPLAY_NONE",
                    "SYSTEM_OVERRIDE_VISIBILITY_HIDDEN",
                    "SYSTEM_OVERRIDE_OPACITY_ZERO",
                ):
                    assert secret not in page_txt, page_txt

                # 2. Markdown GFM table preserves column separators + #id extract works
                md = await ext.browser_extract(ctx, bid, format="markdown")
                assert "| INV-1 | 2 | 135 |" in md, md
                assert "INV-12135" not in md, md

                # 3. Native HTML5 drag-and-drop via browser_drag
                dragged = await ext.browser_drag(ctx, bid, "Task Alpha", "Done Column")
                assert "status=succeeded" in dragged, dragged
                assert "DND:done" in await ext.browser_extract(ctx, bid, query="#dnd-out")

                # 4. Native range & color inputs via free-text label name
                r_res = await ext.browser_fill(ctx, bid, "Priority Level", "75")
                assert "status=succeeded" in r_res, r_res
                assert "SLIDER:75" in await ext.browser_extract(ctx, bid, query="#rng-out")

                c_res = await ext.browser_fill(ctx, bid, "Tag Color", "#ff0000")
                assert "status=succeeded" in c_res, c_res
                assert "COLOR:#ff0000" in await ext.browser_extract(ctx, bid, query="#col-out")

                # 5. Canvas sub-region offset click
                assert "[320x120]" in nav, nav
                c_find = await ext.browser_find(ctx, bid, "canvas", role="canvas")
                assert "size=320x120" in c_find, c_find
                c_ref = re.search(r"@e\d+#\d+", c_find)
                assert c_ref is not None, c_find
                c_click = await ext.browser_click(ctx, bid, c_ref.group(0), offset_x=75.0, offset_y=60.0)
                assert "hit_point=offset(75,60)" in c_click, c_click
                assert "CANVAS:confirm" in await ext.browser_extract(ctx, bid, query="#cv-out")
            finally:
                if bid in ext._browsers:
                    await ext.browser_close(ctx, bid)

    try:
        asyncio.run(scenario())
    finally:
        srv.shutdown()
        srv.server_close()


def test_real_chrome_network_body_capture_and_redaction(monkeypatch: pytest.MonkeyPatch) -> None:
    """Real-Chrome regression for ``browser_network_body``: POST JSON capture, pagination, binary omission & redaction."""
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from chrys.extensions import ToolContext
    from smoke_common import chrome_executable
    import icode_browser_use.extension as ext

    png_1x1 = (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
        b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
        b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
    )
    html = """<!doctype html><html><head><meta charset="utf-8"><title>Net Body E2E</title></head><body>
      <button id="go-api" type="button">Fire API</button>
      <button id="go-bin" type="button">Fire PNG</button>
      <div id="out">IDLE</div>
      <script>
        document.getElementById('go-api').addEventListener('click', async () => {
          const r = await fetch('/api/submit?token=Q_SECRET', {
            method: 'POST',
            headers: {'Content-Type': 'application/json', 'Authorization': 'Bearer H_SECRET'},
            body: JSON.stringify({sku: 'PRO-1', password: 'P_SECRET'})
          });
          const j = await r.json();
          document.getElementById('out').textContent = 'DONE:' + j.id;
        });
        document.getElementById('go-bin').addEventListener('click', async () => {
          const r = await fetch('/api/missing.png');
          document.getElementById('out').textContent = 'BIN:' + r.status;
        });
      </script></body></html>"""

    class _Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: object) -> None:
            return None

        def _send(self, code: int, ctype: str, data: bytes, extra: dict[str, str] | None = None) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self) -> None:
            n = int(self.headers.get("Content-Length") or 0)
            if n > 0:
                self.rfile.read(n)
            body = json.dumps(
                {"id": "ORD-9", "access_token": "R_SECRET", "pad": "Z" * 260, "tail": "TAIL_MARK"}
            ).encode("utf-8")
            self._send(
                201,
                "application/json",
                body,
                extra={"Set-Cookie": "sid=C_SECRET; HttpOnly", "X-Env": "test-e2e"},
            )

        def do_GET(self) -> None:
            path = self.path.split("?", 1)[0]
            if path == "/api/missing.png":
                self._send(404, "image/png", png_1x1)
            else:
                self._send(200, "text/html; charset=utf-8", html.encode("utf-8"))

    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    origin = f"http://127.0.0.1:{srv.server_address[1]}"
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    async def scenario() -> None:
        with tempfile.TemporaryDirectory(prefix="browser-use-netbody-e2e-", ignore_cleanup_errors=True) as td:
            root = Path(td).resolve()
            (root / "ws").mkdir()
            if _ORIG_USERPROFILE is not None:
                monkeypatch.setenv("USERPROFILE", _ORIG_USERPROFILE)
            if _ORIG_HOME is not None:
                monkeypatch.setenv("HOME", _ORIG_HOME)
            if _ORIG_APPDATA is not None:
                monkeypatch.setenv("APPDATA", _ORIG_APPDATA)
            if _ORIG_LOCALAPPDATA is not None:
                monkeypatch.setenv("LOCALAPPDATA", _ORIG_LOCALAPPDATA)
            monkeypatch.setenv("ICODE_BROWSER_ALLOWED_ORIGINS", origin)
            monkeypatch.setenv("ICODE_BROWSER_STATE_DIR", str(root / "state"))
            monkeypatch.setenv("ICODE_BROWSER_HEADLESS", "1")
            monkeypatch.setenv("ICODE_BROWSER_IDLE_CLOSE_SECONDS", "0")
            monkeypatch.setenv("ICODE_BROWSER_EXECUTABLE", str(chrome_executable()))
            ctx = ToolContext.for_testing(
                root / "ws",
                data_dir=root / "data",
                session_data_dir=root / "session",
                agent_name="e2e-netbody",
                session_id="e2e-netbody",
            )
            nav = await ext.browser_navigate(ctx, f"{origin}/")
            bid = nav.splitlines()[0].split("browser_id: ", 1)[1].strip()
            try:
                await ext.browser_click(ctx, bid, "Fire API", expect_condition="text", expect_value="DONE:ORD-9")
                p1 = await ext.browser_network_body(ctx, bid, url_contains="/api/submit", max_chars=200)
                assert "POST 201" in p1 and "ORD-9" in p1 and "PRO-1" in p1, p1
                assert "offset=200" in p1, p1
                assert "x-env: test-e2e" in p1.lower(), p1
                for secret in ("Q_SECRET", "H_SECRET", "P_SECRET", "R_SECRET", "C_SECRET"):
                    assert secret not in p1, (secret, p1)
                p2 = await ext.browser_network_body(
                    ctx,
                    bid,
                    url_contains="/api/submit",
                    include_headers=False,
                    include_request_body=False,
                    max_chars=400,
                    offset=200,
                )
                assert "TAIL_MARK" in p2 and "R_SECRET" not in p2, p2

                await ext.browser_click(ctx, bid, "Fire PNG", expect_condition="text", expect_value="BIN:404")
                bin_out = await ext.browser_network_body(ctx, bid, url_contains="/api/missing.png")
                assert "[binary response body omitted: mime=image/png" in bin_out, bin_out
            finally:
                if bid in ext._browsers:
                    await ext.browser_close(ctx, bid)

    try:
        asyncio.run(scenario())
    finally:
        srv.shutdown()
        srv.server_close()


def test_real_chrome_double_click_guard_native_date_clear_false_and_hidden_cross_oopif(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Real-Chrome regression for reload-409a0893 findings:
    1) double-click stops before click 2 when click 1 replaces the button (0 archive POSTs),
       while regular double-click still fires click-1;click-2;dblclick-2;
    2) browser_fill(text="", clear=False) on native date refuses and preserves existing date;
    3) visibility:hidden cross-origin iframe updates trigger neither page text_changed nor text
       and stay omitted from snapshot, while a visible cross-origin iframe still matches.
    """
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from chrys.extensions import ToolContext, ToolError
    from smoke_common import chrome_executable
    import icode_browser_use.extension as ext

    archive_posts: list[str] = []
    cross_port = [0]

    class _CrossHandler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: object) -> None:
            return None

        def do_GET(self) -> None:
            path = self.path.split("?", 1)[0]
            if path == "/worker":
                body = (
                    "<!doctype html><html><head><title>Worker</title></head><body>"
                    '<p id="w">BACKGROUND-IDLE</p>'
                    "<script>window.addEventListener('message', e => {"
                    " if (e.data === 'refresh') setTimeout(() => { document.getElementById('w').textContent = 'BACKGROUND-READY-934'; }, 50);"
                    "});</script></body></html>"
                )
            elif path == "/visible-worker":
                body = (
                    "<!doctype html><html><head><title>Visible Worker</title></head><body>"
                    '<p id="vw">VISIBLE-IDLE</p>'
                    "<script>window.addEventListener('message', e => {"
                    " if (e.data === 'refresh') setTimeout(() => { document.getElementById('vw').textContent = 'VISIBLE-READY-555'; }, 50);"
                    "});</script></body></html>"
                )
            else:
                self.send_response(404)
                self.end_headers()
                return
            data = body.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    cross_srv = ThreadingHTTPServer(("127.0.0.1", 0), _CrossHandler)
    cross_port[0] = cross_srv.server_address[1]
    cross_origin = f"http://localhost:{cross_port[0]}"
    threading.Thread(target=cross_srv.serve_forever, daemon=True).start()

    class _PrimaryHandler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: object) -> None:
            return None

        def _html(self, body: str) -> None:
            data = body.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self) -> None:
            n = int(self.headers.get("Content-Length") or 0)
            payload = self.rfile.read(n).decode("utf-8", errors="replace") if n > 0 else ""
            if self.path == "/api/archive-row":
                archive_posts.append(payload)
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")

        def do_GET(self) -> None:
            path = self.path.split("?", 1)[0]
            if path == "/moving-double-click":
                self._html(
                    "<!doctype html><html><head><title>Document row</title></head><body>"
                    '<h1>Document row</h1><div id="slot"><button id="open" type="button" style="width:160px;height:40px">Open record</button></div>'
                    '<p id="msg">Waiting</p><script>'
                    "document.getElementById('open').addEventListener('click', () => {"
                    "  const slot = document.getElementById('slot');"
                    '  slot.innerHTML = \'<button id="arch" type="button" style="width:160px;height:40px">Archive record</button>\';'
                    "  document.getElementById('arch').addEventListener('click', () => {"
                    "    fetch('/api/archive-row', {method:'POST', body:'item-42'});"
                    "    document.getElementById('msg').textContent = 'ARCHIVE-UNEXPECTED';"
                    "  });"
                    "});</script></body></html>"
                )
            elif path == "/regular-double-click":
                self._html(
                    "<!doctype html><html><head><title>Regular dblclick</title></head><body>"
                    '<button id="open" type="button">Open record</button><p id="ev">EVENTS:</p><script>'
                    "const ev = document.getElementById('ev'), b = document.getElementById('open');"
                    "b.addEventListener('click', e => { ev.textContent += 'click-' + e.detail + ';'; });"
                    "b.addEventListener('dblclick', e => { ev.textContent += 'dblclick-' + e.detail + ';'; });"
                    "</script></body></html>"
                )
            elif path == "/date-contract":
                self._html(
                    "<!doctype html><html><head><title>Date contract</title></head><body>"
                    '<label for="d">Travel date</label><input id="d" type="date" value="2027-05-01" aria-label="Travel date" />'
                    '<p id="out">DATE:2027-05-01; EVENTS:0</p><script>'
                    "let n=0; const d=document.getElementById('d'), out=document.getElementById('out');"
                    "d.addEventListener('input', () => { n++; out.textContent='DATE:'+d.value+'; EVENTS:'+n; });"
                    "</script></body></html>"
                )
            elif path == "/hidden-cross":
                self._html(
                    "<!doctype html><html><head><title>Visible dashboard</title></head><body>"
                    '<h1>Visible dashboard</h1><button id="go" type="button">Refresh background data</button>'
                    f'<iframe id="bg" title="Background worker" src="{cross_origin}/worker" style="visibility:hidden;width:200px;height:80px"></iframe>'
                    "<script>document.getElementById('go').addEventListener('click', () => {"
                    "  document.getElementById('bg').contentWindow.postMessage('refresh', '*');"
                    "});</script></body></html>"
                )
            elif path == "/visible-cross":
                self._html(
                    "<!doctype html><html><head><title>Visible cross</title></head><body>"
                    '<h1>Visible cross</h1><button id="go" type="button">Refresh visible frame</button>'
                    f'<iframe id="vis" title="Visible worker" src="{cross_origin}/visible-worker" style="width:200px;height:80px"></iframe>'
                    "<script>document.getElementById('go').addEventListener('click', () => {"
                    "  document.getElementById('vis').contentWindow.postMessage('refresh', '*');"
                    "});</script></body></html>"
                )
            else:
                self.send_response(404)
                self.end_headers()

    srv = ThreadingHTTPServer(("127.0.0.1", 0), _PrimaryHandler)
    origin = f"http://127.0.0.1:{srv.server_address[1]}"
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    async def scenario() -> None:
        with tempfile.TemporaryDirectory(prefix="browser-use-409a-e2e-", ignore_cleanup_errors=True) as td:
            root = Path(td).resolve()
            (root / "ws").mkdir()
            if _ORIG_USERPROFILE is not None:
                monkeypatch.setenv("USERPROFILE", _ORIG_USERPROFILE)
            if _ORIG_HOME is not None:
                monkeypatch.setenv("HOME", _ORIG_HOME)
            if _ORIG_APPDATA is not None:
                monkeypatch.setenv("APPDATA", _ORIG_APPDATA)
            if _ORIG_LOCALAPPDATA is not None:
                monkeypatch.setenv("LOCALAPPDATA", _ORIG_LOCALAPPDATA)
            monkeypatch.setenv("ICODE_BROWSER_ALLOWED_ORIGINS", f"{origin},{cross_origin}")
            monkeypatch.setenv("ICODE_BROWSER_STATE_DIR", str(root / "state"))
            monkeypatch.setenv("ICODE_BROWSER_HEADLESS", "1")
            monkeypatch.setenv("ICODE_BROWSER_IDLE_CLOSE_SECONDS", "0")
            monkeypatch.setenv("ICODE_BROWSER_EXECUTABLE", str(chrome_executable()))
            ctx = ToolContext.for_testing(
                root / "ws",
                data_dir=root / "data",
                session_data_dir=root / "session",
                agent_name="e2e-409a",
                session_id="e2e-409a",
            )
            nav = await ext.browser_navigate(ctx, f"{origin}/moving-double-click")
            bid = nav.splitlines()[0].split("browser_id: ", 1)[1].strip()
            try:
                # 1a. Double-click on self-replacing button aborts click 2 -> 0 archive POSTs
                dbl_replaced = await ext.browser_click(ctx, bid, "Open record", click_count=2)
                assert "dispatched_clicks=1/2" in dbl_replaced, dbl_replaced
                assert "stop_reason=target_detached" in dbl_replaced, dbl_replaced
                assert "status=uncertain" in dbl_replaced and "status=succeeded" not in dbl_replaced, dbl_replaced
                await asyncio.sleep(0.15)
                assert archive_posts == [], archive_posts
                assert "ARCHIVE-UNEXPECTED" not in await ext.browser_extract(ctx, bid)

                # 1b. Regular double-click still fires click-1;click-2;dblclick-2;
                await ext.browser_navigate(ctx, f"{origin}/regular-double-click", bid)
                await ext.browser_click(ctx, bid, "Open record", click_count=2)
                assert "EVENTS:click-1;click-2;dblclick-2;" in await ext.browser_extract(ctx, bid, query="#ev")

                # 2. Native date clear=False with empty string refuses and keeps prior date
                await ext.browser_navigate(ctx, f"{origin}/date-contract", bid)
                await ext.browser_fill(ctx, bid, "Travel date", "2027-05-19", clear=False)
                assert "DATE:2027-05-19; EVENTS:1" in await ext.browser_extract(ctx, bid, query="#out")
                with pytest.raises(ToolError) as exc_info:
                    await ext.browser_fill(ctx, bid, "Travel date", "", clear=False)
                assert exc_info.value.code == "browser_bad_request"
                assert "DATE:2027-05-19; EVENTS:1" in await ext.browser_extract(ctx, bid, query="#out")
                await ext.browser_fill(ctx, bid, "Travel date", "", clear=True)
                assert "DATE:; EVENTS:2" in await ext.browser_extract(ctx, bid, query="#out")

                # 3a. visibility:hidden cross-origin iframe does not trigger text_changed/text or leak to snapshot
                snap_hc = await ext.browser_navigate(ctx, f"{origin}/hidden-cross", bid)
                await asyncio.sleep(0.25)
                assert "BACKGROUND-IDLE" not in snap_hc, snap_hc
                click_hc = await ext.browser_click(
                    ctx,
                    bid,
                    "Refresh background data",
                    expect_condition="text_changed",
                    expect_timeout_ms=600,
                    expect_return="text",
                )
                assert "expect_status=timeout" in click_hc, click_hc
                wait_hc = await ext.browser_wait_for(
                    ctx, bid, condition="text", value="BACKGROUND-READY-934", timeout_ms=400, return_mode="text"
                )
                assert "status=timeout" in wait_hc, wait_hc
                snap_hc2 = await ext.browser_snapshot(ctx, bid)
                assert "BACKGROUND-READY-934" not in snap_hc2, snap_hc2

                # 3b. Visible cross-origin iframe still triggers text_changed and text
                await ext.browser_navigate(ctx, f"{origin}/visible-cross", bid)
                await asyncio.sleep(0.25)
                click_vc = await ext.browser_click(
                    ctx,
                    bid,
                    "Refresh visible frame",
                    expect_condition="text_changed",
                    expect_timeout_ms=1500,
                    expect_return="text",
                )
                assert "expect_status=matched" in click_vc and "VISIBLE-READY-555" in click_vc, click_vc
            finally:
                if bid in ext._browsers:
                    await ext.browser_close(ctx, bid)

    try:
        asyncio.run(scenario())
    finally:
        srv.shutdown()
        srv.server_close()
        cross_srv.shutdown()
        cross_srv.server_close()


def test_real_chrome_pr325_takeover_oopif_rollback_and_broker_compact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Real-Chrome regression for PR #325 review findings:
    1) takeover -> idle -> stale handle navigate rejected -> stale handle close preserves B's cookies & record;
    2) static page with visible cross-origin iframe times out on text_changed (850ms) instead of false success;
    3) ancestor visibility:hidden cross-origin iframe triggers neither text nor text_changed;
    4) fresh sub-agent first implicit relative navigate fails and leaves 0 profile dirs on disk;
    5) compact client against a default full Broker gets compact popup hint (no browser_tab_switch recommendation).
    """
    import importlib
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from chrys.extensions import ToolContext, ToolError
    from smoke_common import chrome_executable

    import icode_browser_use.extension as ext
    from icode_browser_use import registry
    from icode_browser_use.broker import BrokerServer
    from icode_browser_use.config import BrowserConfig

    cross_port = [0]

    class _CrossHandler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: object) -> None:
            return None

        def do_GET(self) -> None:
            path = self.path.split("?", 1)[0]
            if path == "/static-child":
                body = "<!doctype html><html><body><p id='c'>STATIC-CHILD-FRAME</p></body></html>"
            elif path == "/hidden-ancestor-child":
                body = (
                    "<!doctype html><html><body><p id='h'>ANCESTOR-HIDDEN-INIT</p>"
                    "<script>window.addEventListener('message', e => {"
                    " if (e.data === 'mut') setTimeout(() => { document.getElementById('h').textContent = 'ANCESTOR-HIDDEN-UPDATED'; }, 50);"
                    "});</script></body></html>"
                )
            elif path == "/other-origin":
                body = "<!doctype html><html><body><h1>Other origin</h1></body></html>"
            else:
                self.send_response(404)
                self.end_headers()
                return
            data = body.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    cross_srv = ThreadingHTTPServer(("127.0.0.1", 0), _CrossHandler)
    cross_port[0] = cross_srv.server_address[1]
    cross_origin = f"http://localhost:{cross_port[0]}"
    threading.Thread(target=cross_srv.serve_forever, daemon=True).start()

    class _PrimaryHandler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: object) -> None:
            return None

        def _html(self, body: str) -> None:
            data = body.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:
            path = self.path.split("?", 1)[0]
            if path == "/start":
                self._html("<!doctype html><html><body><h1>Start A</h1></body></html>")
            elif path == "/set-cookie":
                data = b"<!doctype html><html><body><h1>Cookie Set</h1></body></html>"
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Set-Cookie", "persist_b=alive-from-b; Max-Age=3600; Path=/")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            elif path == "/check-cookie":
                cookie_hdr = self.headers.get("Cookie", "")
                self._html(f"<!doctype html><html><body><p id='ck'>COOKIE:{cookie_hdr}</p></body></html>")
            elif path == "/static-oopif":
                self._html(
                    "<!doctype html><html><body><p>Static parent</p>"
                    f'<iframe src="{cross_origin}/static-child" style="width:220px;height:90px"></iframe>'
                    "</body></html>"
                )
            elif path == "/hidden-ancestor-oopif":
                self._html(
                    "<!doctype html><html><body><p>Pending</p>"
                    '<button id="mut" type="button">Mutate hidden</button>'
                    f'<div style="visibility:hidden"><iframe id="ha" src="{cross_origin}/hidden-ancestor-child" style="width:220px;height:90px"></iframe></div>'
                    "<script>document.getElementById('mut').addEventListener('click', () => {"
                    " document.getElementById('ha').contentWindow.postMessage('mut', '*');"
                    "});</script></body></html>"
                )
            elif path == "/popup-opener":
                self._html(
                    "<!doctype html><html><body>"
                    f'<a id="pop" href="{origin}/start" target="_blank" rel="opener">Open Popup</a>'
                    "</body></html>"
                )
            else:
                self.send_response(404)
                self.end_headers()

    srv = ThreadingHTTPServer(("127.0.0.1", 0), _PrimaryHandler)
    origin = f"http://127.0.0.1:{srv.server_address[1]}"
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    async def scenario() -> None:
        with tempfile.TemporaryDirectory(prefix="browser-use-pr325-e2e-", ignore_cleanup_errors=True) as td:
            root = Path(td).resolve()
            (root / "ws").mkdir()
            if _ORIG_USERPROFILE is not None:
                monkeypatch.setenv("USERPROFILE", _ORIG_USERPROFILE)
            if _ORIG_HOME is not None:
                monkeypatch.setenv("HOME", _ORIG_HOME)
            if _ORIG_APPDATA is not None:
                monkeypatch.setenv("APPDATA", _ORIG_APPDATA)
            if _ORIG_LOCALAPPDATA is not None:
                monkeypatch.setenv("LOCALAPPDATA", _ORIG_LOCALAPPDATA)
            monkeypatch.setenv("ICODE_BROWSER_ALLOWED_ORIGINS", f"{origin},{cross_origin}")
            monkeypatch.setenv("ICODE_BROWSER_STATE_DIR", str(root / "state"))
            monkeypatch.setenv("ICODE_BROWSER_HEADLESS", "1")
            monkeypatch.setenv("ICODE_BROWSER_IDLE_CLOSE_SECONDS", "0")
            monkeypatch.setenv("ICODE_BROWSER_EXECUTABLE", str(chrome_executable()))

            # 1. Takeover -> idle -> stale navigate rejected -> stale close preserves B's cookie & record
            wf_ctx = ToolContext.for_testing(
                root / "ws",
                data_dir=root / "data",
                session_data_dir=root / "session",
                agent_name="wf-agent",
                session_id="e2e-pr325-wf",
                invocation_type="workflow_node",
                caller_name="wf",
                invocation_id="inv-pr325-1",
            )
            nav_a = await ext.browser_navigate(wf_ctx, f"{origin}/start")
            bid_a = nav_a.splitlines()[0].split("browser_id: ", 1)[1].strip()
            browser_a = ext._browsers[bid_a]
            assert browser_a.record is not None
            resume_id_a = browser_a.record.resume_id
            state_dir = browser_a.slot_state_dir
            assert state_dir is not None
            await browser_a.session._shutdown_idle()

            # Simulate Host B taking over in a fresh process registry
            saved_a = ext._browsers.pop(bid_a)
            resumed_b = await ext.browser_resume(wf_ctx, resume_id_a)
            bid_b = resumed_b.splitlines()[0].split("browser_id: ", 1)[1].strip()
            browser_b = ext._browsers[bid_b]
            assert browser_b.record is not None
            resume_id_b = browser_b.record.resume_id
            await ext.browser_navigate(wf_ctx, f"{origin}/set-cookie", bid_b)
            await browser_b.session._shutdown_idle()
            ext._browsers.pop(bid_b)

            # Host A attempts stale navigate to another origin -> rejected; then closes -> B survives
            ext._browsers[bid_a] = saved_a
            with pytest.raises(ToolError) as stale_exc:
                await ext.browser_navigate(wf_ctx, f"{cross_origin}/other-origin", bid_a)
            assert stale_exc.value.code == "browser_superseded"
            await ext.browser_close(wf_ctx, bid_a)

            assert state_dir.is_dir()
            recs, _ = registry.load(registry.manifest_path(wf_ctx))
            assert [r.resume_id for r in recs] == [resume_id_b]

            # Resume B and confirm persistent cookie survived
            resumed_c = await ext.browser_resume(wf_ctx, resume_id_b)
            bid_c = resumed_c.splitlines()[0].split("browser_id: ", 1)[1].strip()
            try:
                await ext.browser_navigate(wf_ctx, f"{origin}/check-cookie", bid_c)
                ck_text = await ext.browser_extract(wf_ctx, bid_c, query="#ck")
                assert "persist_b=alive-from-b" in ck_text, ck_text

                # 2. Static page with visible cross-origin iframe must timeout on 850ms text_changed
                await ext.browser_navigate(wf_ctx, f"{origin}/static-oopif", bid_c)
                await asyncio.sleep(0.25)
                wait_static = await ext.browser_wait_for(
                    wf_ctx, bid_c, condition="text_changed", timeout_ms=850, return_mode="text"
                )
                assert "status=timeout" in wait_static, wait_static
                assert "status=succeeded" not in wait_static, wait_static

                # 3. Ancestor visibility:hidden cross-origin iframe triggers neither text nor text_changed
                await ext.browser_navigate(wf_ctx, f"{origin}/hidden-ancestor-oopif", bid_c)
                await asyncio.sleep(0.25)
                click_ha = await ext.browser_click(
                    wf_ctx,
                    bid_c,
                    "Mutate hidden",
                    expect_condition="text_changed",
                    expect_timeout_ms=600,
                    expect_return="text",
                )
                assert "expect_status=timeout" in click_ha, click_ha
                wait_ha = await ext.browser_wait_for(
                    wf_ctx, bid_c, condition="text", value="ANCESTOR-HIDDEN-UPDATED", timeout_ms=400
                )
                assert "status=timeout" in wait_ha, wait_ha
            finally:
                if bid_c in ext._browsers:
                    await ext.browser_close(wf_ctx, bid_c)

            # 4. Fresh sub-agent implicit relative navigate fails and leaves 0 profile dirs
            sub_ctx = ToolContext.for_testing(
                root / "ws",
                data_dir=root / "data",
                session_data_dir=root / "session",
                agent_name="sub-rel",
                session_id="e2e-pr325-sub",
                invocation_type="sub_agent",
                caller_name="worker",
                invocation_id="sub-rel-e2e-1",
            )
            sub_slot_dir = (root / "state") / "agents" / ext._owner_key(sub_ctx) / "slot-0"
            with pytest.raises(ToolError) as rel_exc:
                await ext.browser_navigate(sub_ctx, "/relative-path")
            assert rel_exc.value.code == "browser_bad_request"
            assert "open=0 recoverable=0" in await ext.browser_list(sub_ctx)
            assert not sub_slot_dir.exists(), f"profile dir leaked: {sub_slot_dir}"

            # 5. Compact client connected to a default full Broker gets compact popup hint
            broker_state = root / "broker-state"
            broker_cfg = BrowserConfig(
                allowed_origins=(origin, cross_origin),
                state_dir=broker_state,
                headless=True,
                idle_close_seconds=0,
                executable=str(chrome_executable()),
                toolset="full",
            )
            broker = BrokerServer(broker_cfg)
            await broker.start()
            try:
                monkeypatch.setenv("ICODE_BROWSER_STATE_DIR", str(broker_state))
                monkeypatch.setenv("ICODE_BROWSER_BROKER", "1")
                monkeypatch.setenv("ICODE_BROWSER_TOOLSET", "compact")
                reloaded = importlib.reload(ext)
                reloaded._browsers.clear()
                b_ctx = ToolContext.for_testing(
                    root / "ws",
                    data_dir=root / "data",
                    session_data_dir=root / "session",
                    agent_name="compact-cli",
                    session_id="e2e-pr325-broker",
                )
                nav_b = await reloaded.browser_navigate(b_ctx, f"{origin}/popup-opener")
                bid_br = nav_b.splitlines()[0].split("browser_id: ", 1)[1].strip()
                try:
                    pop_res = await reloaded.browser_click(b_ctx, bid_br, "Open Popup")
                    assert "effect=new_tab" in pop_res, pop_res
                    assert "compact 工具集不包含标签切换工具" in pop_res, pop_res
                    assert "可用 browser_tab_switch" not in pop_res, pop_res
                finally:
                    if bid_br in reloaded._browsers:
                        await reloaded.browser_close(b_ctx, bid_br)
            finally:
                await broker.aclose()
                monkeypatch.delenv("ICODE_BROWSER_BROKER", raising=False)
                monkeypatch.delenv("ICODE_BROWSER_TOOLSET", raising=False)
                importlib.reload(ext)

    try:
        asyncio.run(scenario())
    finally:
        srv.shutdown()
        srv.server_close()
        cross_srv.shutdown()
        cross_srv.server_close()
