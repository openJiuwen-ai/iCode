# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Opt-in local-development origin check against a real Chrome (W02).

Starts a loopback HTTP fixture on two ports, launches the plugin's Owned
browser with only one origin authorised, and checks that:

1. the authorised origin loads, runs page script and serves its own API;
2. a second loopback port is refused both for navigation and for page fetches
   (its server receives no request at all);
3. a redirect from the authorised origin to that second port is refused.

Every browser and server this script starts is closed in ``finally``; all
browser state stays in a temporary directory. Run it by hand::

    python scripts/local_origin_smoke.py --executable "C:/Program Files/Google/Chrome/Application/chrome.exe"

It exits non-zero on the first failed check and prints one JSON line per stage.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import re
import socket
import socketserver
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys
import tempfile

_ROOT = Path(__file__).resolve().parents[1]
if (_ROOT / "src" / "icode_browser_use").is_dir():
    # Exercise the working tree, not an older installed copy.
    sys.path.insert(0, str(_ROOT / "src"))


def report(event: str, **fields: object) -> None:
    print(json.dumps({"event": event, **fields}, ensure_ascii=True), flush=True)


class Fixture(ThreadingHTTPServer):
    seen: list[str]
    target_url: str = ""


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
        self.server.seen.append(self.path)  # type: ignore[attr-defined]
        if self.path.startswith("/redirect-denied"):
            self.send_response(302)
            self.send_header("Location", self.server.target_url)  # type: ignore[attr-defined]
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if self.path.startswith("/api"):
            body = b'{"ok": true}'
            content_type = "application/json"
        else:
            body = (
                b"<!doctype html><title>local-fixture</title><h1 id=title>local-fixture</h1>"
                b"<form><label>Email <input id=email name=email></label>"
                b"<label>Avatar <input id=avatar name=avatar type=file></label></form>"
            )
            content_type = "text/html; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - base signature
        return


class FixtureV6(Fixture):
    address_family = socket.AF_INET6


@contextmanager
def serving(target_url: str = "", host: str = "127.0.0.1") -> Iterator[Fixture]:
    if ":" in host:
        server = FixtureV6((host, 0), _Handler)
    else:
        server = Fixture((host, 0), _Handler)
    server.seen = []
    server.target_url = target_url
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def origin_of(server: Fixture) -> str:
    host = str(server.server_address[0])
    if ":" in host:
        host = f"[{host}]"
    return f"http://{host}:{server.server_address[1]}"


class _WsHandler(socketserver.StreamRequestHandler):
    """Minimal RFC6455 handshake: enough for the browser to report ``open``."""

    def handle(self) -> None:
        headers: dict[str, str] = {}
        while True:
            line = self.rfile.readline()
            if line in (b"\r\n", b"\n", b""):
                break
            name, _, value = line.decode("latin-1").partition(":")
            headers[name.strip().lower()] = value.strip()
        key = headers.get("sec-websocket-key", "")
        accept = base64.b64encode(
            hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()
        ).decode()
        self.wfile.write(
            (
                "HTTP/1.1 101 Switching Protocols\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
            ).encode()
        )
        self.wfile.flush()
        self.server.upgrades += 1  # type: ignore[attr-defined]
        time.sleep(0.5)


class _WsServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True
    upgrades: int = 0


@contextmanager
def serving_websocket() -> Iterator[tuple[str, _WsServer]]:
    server = _WsServer(("127.0.0.1", 0), _WsHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"ws://127.0.0.1:{server.server_address[1]}/hmr", server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def ipv6_loopback_available() -> bool:
    try:
        with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as probe:
            probe.bind(("::1", 0))
    except OSError:
        return False
    return True


async def run(args: argparse.Namespace) -> None:
    with tempfile.TemporaryDirectory(prefix="local-origin-") as directory:
        root = Path(directory).resolve()
        with serving() as denied:
            denied_origin = origin_of(denied)
            with serving(denied_origin + "/api") as allowed:
                with serving_websocket() as (ws_url, ws_server):
                    await _check_maybe_ipv6(args, root, origin_of(allowed), denied_origin, denied, ws_url)
                report("websocket_upgrades", count=ws_server.upgrades)
            if denied.seen:
                raise RuntimeError(f"the unauthorised origin received requests: {denied.seen!r}")
    report("passed", executable=str(args.executable or "auto"))


async def _check_maybe_ipv6(
    args: argparse.Namespace,
    root: Path,
    allowed_origin: str,
    denied_origin: str,
    denied: Fixture,
    ws_url: str,
) -> None:
    if ipv6_loopback_available():
        with serving(host="::1") as ipv6:
            origin = origin_of(ipv6)
            await _check(args, root, allowed_origin, denied_origin, denied, extra_origin=origin, ws_url=ws_url)
            report("ipv6_checked", origin=origin)
    else:
        report("ipv6_unavailable", reason="no IPv6 loopback listener")
        await _check(args, root, allowed_origin, denied_origin, denied, ws_url=ws_url)


async def _check(
    args: argparse.Namespace,
    root: Path,
    allowed_origin: str,
    denied_origin: str,
    denied: Fixture,
    *,
    extra_origin: str | None = None,
    ws_url: str,
) -> None:
    from icode_browser_use.actions import build_tool_defs
    from icode_browser_use.config import BrowserConfig
    from icode_browser_use.errors import Blocked
    from icode_browser_use.runtime import call_tool
    from icode_browser_use.session import BrowserSession

    origins = [allowed_origin] + ([extra_origin] if extra_origin else [])
    upload_root = root / "upload"
    upload_root.mkdir(parents=True, exist_ok=True)
    upload_file = upload_root / "avatar.txt"
    upload_file.write_text("local fixture upload", encoding="utf-8")
    env = {
        "ICODE_BROWSER_ALLOWED_ORIGINS": ",".join(origins),
        "ICODE_BROWSER_STATE_DIR": str(root / "state"),
        "ICODE_BROWSER_IDLE_CLOSE_SECONDS": "0",
        "ICODE_BROWSER_EVALUATE": "allow",
        "ICODE_BROWSER_UPLOAD_ROOTS": str(upload_root),
        "ICODE_BROWSER_HEADLESS": "0" if args.headed else "1",
    }
    if args.executable:
        env["ICODE_BROWSER_EXECUTABLE"] = args.executable
    cfg = BrowserConfig.from_env(env)
    if cfg.allowed_origins != tuple(origins) or cfg.allowed_domains:
        raise RuntimeError(f"unexpected policy: {cfg.allowed_origins!r} {cfg.allowed_domains!r}")
    report("fixture", allowed=allowed_origin, denied=denied_origin, state_dir=str(cfg.state_dir))

    tools = {tool.name: tool for tool in build_tool_defs(evaluate_enabled=True)}

    async def call(name: str, **arguments: object) -> str:
        return "\n".join(block["text"] for block in await call_tool(session, tools[name], arguments))

    session = BrowserSession(cfg)
    try:
        await session.ensure_started()

        page = await call("browser_navigate", url=allowed_origin + "/index.html")
        if "local-fixture" not in page:
            raise RuntimeError(f"authorised origin did not load: {page[:200]}")
        report("authorised_page", url=session.snapshot.url)

        same_origin = await call(
            "browser_evaluate", expression="fetch('/api').then((r) => r.text())", await_promise=True
        )
        if "true" not in same_origin:
            raise RuntimeError(f"authorised API call failed: {same_origin[:200]}")
        report("authorised_api", result=same_origin.strip())

        extracted = await call("browser_extract", format="text")
        if "local-fixture" not in extracted:
            raise RuntimeError("extract did not read the local page")
        report("authorised_extract", chars=len(extracted))

        await call("browser_fill", target="email", text="dev@local.test")
        typed = await call(
            "browser_evaluate",
            expression="document.getElementById('email').value",
            await_promise=False,
        )
        if "dev@local.test" not in typed:
            raise RuntimeError(f"form fill did not reach the local page: {typed[:200]}")
        report("authorised_form_fill", value="dev@local.test")

        uploaded = await call("browser_upload", target="avatar", path=str(upload_file))
        if "avatar.txt" not in uploaded:
            raise RuntimeError(f"file upload on the local page failed: {uploaded[:200]}")
        report("authorised_upload", result=uploaded.strip())

        match = re.search(r"action_id=([0-9a-f]+)", uploaded)
        if match is None:
            raise RuntimeError(f"upload result carried no action_id: {uploaded[:200]}")
        action_id = match.group(1)
        queried = await call("browser_action_status", action_id=action_id)
        if '"status": "succeeded"' not in queried or '"phase": "result"' not in queried:
            raise RuntimeError(f"journal did not record the upload: {queried[:400]}")
        report("journal_query", action_id=action_id)

        unknown = await call("browser_action_status", action_id="000000000000")
        if "status=unknown" not in unknown:
            raise RuntimeError(f"an unknown action_id was not reported as unknown: {unknown[:200]}")
        report("journal_unknown_action")

        cross_origin = await call(
            "browser_evaluate",
            expression=(f"fetch('{denied_origin}/api').then((r) => 'loaded:' + r.status).catch(() => 'blocked')"),
            await_promise=True,
        )
        if "blocked" not in cross_origin:
            raise RuntimeError(f"cross-origin fetch to a loopback port was not blocked: {cross_origin}")
        report("denied_cross_origin_fetch", result=cross_origin.strip())

        redirected = await call(
            "browser_evaluate",
            expression="fetch('/redirect-denied').then((r) => 'loaded:' + r.status).catch(() => 'blocked')",
            await_promise=True,
        )
        if "blocked" not in redirected:
            raise RuntimeError(f"redirect to an unauthorised origin was not blocked: {redirected}")
        report("denied_redirect", result=redirected.strip())

        if denied.seen:
            raise RuntimeError(f"the unauthorised origin received requests: {denied.seen!r}")

        try:
            await call("browser_navigate", url=denied_origin + "/index.html")
        except Blocked:
            report("denied_navigation_precheck", url=denied_origin)
        else:
            raise RuntimeError("an unauthorised loopback port was not refused")

        if not (session.snapshot.url or "").startswith(allowed_origin):
            raise RuntimeError(f"the page left the authorised origin: {session.snapshot.url}")
        report("stayed_on_allowed_origin", url=session.snapshot.url)

        if extra_origin:
            ipv6_page = await call("browser_navigate", url=extra_origin + "/index.html")
            if "local-fixture" not in ipv6_page:
                raise RuntimeError(f"authorised IPv6 loopback origin did not load: {ipv6_page[:200]}")
            report("authorised_ipv6_page", url=session.snapshot.url)

        # HMR channels are WebSockets, which the CDP Fetch domain does not
        # intercept. Record the measured behaviour instead of assuming a block.
        socket_state = await call(
            "browser_evaluate",
            expression=(
                "new Promise((resolve) => {"
                f"const ws = new WebSocket('{ws_url}');"
                "ws.onopen = () => { ws.close(); resolve('ws-open'); };"
                "ws.onerror = () => resolve('ws-error');"
                "setTimeout(() => resolve('ws-timeout'), 4000);"
                "})"
            ),
            await_promise=True,
        )
        report("websocket_policy", state=socket_state.strip(), url=ws_url)
    finally:
        await session.aclose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--executable", help="Explicit Chrome/Edge executable; otherwise use plugin discovery")
    parser.add_argument("--headed", action="store_true", help="Show the browser window (default: headless)")
    args = parser.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
