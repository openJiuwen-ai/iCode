# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Local mock server for the iCode device-code login flow.

The production auth service (``82.187.34.98``) and the DEV service
(``81.89.182.150``) both live on the bank intranet and are unreachable from
outside it, so device-code login cannot be exercised while developing off-site.
This script stands up a loopback replacement that speaks exactly the same wire
protocol, including the envelope quirks the reference mock and the real backend
share:

* ``/auth/device/code`` and ``/auth/device/token`` answer under ``result``,
  ``/user/info`` answers under ``data``.
* ``success`` stays ``true`` even while polling reports
  ``authorization_pending``; the outcome lives in ``result.error``.
* An unknown token yields ``{"message": "用户不存在", "code": 400, "data": null}``
  with **no** ``success`` key, and HTTP stays 200.

A client therefore has to read the envelope, never ``status_code``.

Two modes are available:

``manual`` (default)
    A poll stays ``authorization_pending`` until someone confirms the grant on
    the bundled local verify page. This is the honest simulation of "wait for
    the user to finish logging in in a browser", and it closes the loop without
    depending on any external site (the reference mock points at baidu, which
    is clickable but does nothing).

``auto``
    Alternates pending/authorized per poll, per ``device_code``. Matches the
    reference ``mockServer/server.js`` behaviour and suits smoke tests.

Run it from the repository root::

    uv run python aixcoding/mock/auth/server.py                  # manual, :7777
    uv run python aixcoding/mock/auth/server.py --mode=auto      # parity polling
    uv run python aixcoding/mock/auth/server.py --deny           # next grant denied
    uv run python aixcoding/mock/auth/server.py --slow-down 3    # 3 slow_down polls

Then point iCode at it::

    AIXCODING_ENVIRONMENT=local uv run aixcoding-cli

The module is also importable: ``MockAuthConfig`` / ``MockAuthState`` /
``create_server`` let tests start a loopback instance as an integration stub
(loopback traffic is exempt from the ``test_network_egress`` guard).
"""

from __future__ import annotations

import argparse
import json
import logging
import secrets
import socket
import sys
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

_LOGGER = logging.getLogger("chrys.mock_auth")

API_PREFIX = "/api/v1"
DEVICE_CODE_ROUTE = f"{API_PREFIX}/auth/device/code"
DEVICE_TOKEN_ROUTE = f"{API_PREFIX}/auth/device/token"
USER_INFO_ROUTE = f"{API_PREFIX}/user/info"
VERIFY_ROUTE = "/device/verify"
VERIFY_CONFIRM_ROUTE = "/device/verify/confirm"

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 7777
DEFAULT_INTERVAL = 5
DEFAULT_EXPIRES_IN = 600

# Loopback only, per the aixcoding/mock/ conventions (aixcoding/mock/README.md);
# ``create_server`` rejects any other host at startup.
_LOOPBACK_HOSTS = ("127.0.0.1", "::1")

# Errors the device-code grant defines.
PENDING = "authorization_pending"
SLOW_DOWN = "slow_down"
ACCESS_DENIED = "access_denied"
EXPIRED_TOKEN = "expired_token"

# Business code the reference mock returns for an unknown token (HTTP stays 200).
INVALID_TOKEN_CODE = 400
INVALID_TOKEN_MESSAGE = "用户不存在"

# Fixture data shaped like the reference mockServer's so that anything
# observed against either server behaves the same; values are fictitious
# (no real PII).
_FIXTURE_USER: dict[str, Any] = {
    "ehr": "1234567",
    "name": "测试员",
    "region": None,
    "deptName": "模拟服务器开发部",
    "deptId": None,
    "isStWg": 1,
    "userType": 1,
}

# The reference mock hardcodes these inside the token result.
_REFERENCE_TOKEN_TYPE = "aixcoding-cli"
_REFERENCE_ACCESS_TOKEN = "这不重要"

_VERIFY_PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>iCode Mock Device Verification</title>
<style>
  body {{ font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
         max-width: 34rem; margin: 4rem auto; padding: 0 1rem; color: #1f2328; }}
  .code {{ font-size: 2rem; letter-spacing: .25rem; font-weight: 700;
           background: #f6f8fa; border: 1px solid #d0d7de; border-radius: 8px;
           padding: .75rem 1rem; text-align: center; }}
  form {{ display: inline; }} button {{ font-size: 1rem; padding: .55rem 1.25rem;
           border-radius: 6px; border: 1px solid #d0d7de; cursor: pointer; }}
  button.primary {{ background: #1f883d; color: #fff; border-color: #1f883d; }}
  p.hint {{ color: #57606a; font-size: .9rem; }}
</style>
</head>
<body>
<h1>Confirm the device</h1>
<p>iCode is waiting for this user code:</p>
<div class="code">{user_code}</div>
<p class="hint">{status_line}</p>
<p>
  <form method="post" action="/device/verify/confirm">
    <input type="hidden" name="user_code" value="{user_code}">
    <input type="hidden" name="decision" value="allow">
    <button class="primary" type="submit">Confirm authorization</button>
  </form>
  <form method="post" action="/device/verify/confirm">
    <input type="hidden" name="user_code" value="{user_code}">
    <input type="hidden" name="decision" value="deny">
    <button type="submit">Deny</button>
  </form>
</p>
<p class="hint">Mode: {mode}. The CLI polls until this page decides.</p>
</body>
</html>
"""


def _new_device_code() -> str:
    """Return a fresh opaque device code."""
    return secrets.token_hex(20)


def _new_user_code() -> str:
    """Return a short human-readable user code shaped like ``ABCD-EFGH``."""
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    half = "".join(secrets.choice(alphabet) for _ in range(4))
    other = "".join(secrets.choice(alphabet) for _ in range(4))
    return f"{half}-{other}"


def _now_ms() -> int:
    """Return the current wall clock in milliseconds, like ``Date.now()``."""
    return int(time.time() * 1000)


@dataclass
class MockGrant:
    """One device-code authorization in flight."""

    device_code: str
    user_code: str
    issued_at: float
    expires_at: float
    status: str = "pending"  # pending | granted | denied
    polls: int = 0
    token: str = ""


@dataclass
class MockAuthConfig:
    """Knobs a mock instance honours."""

    mode: str = "manual"  # manual | auto
    interval: int = DEFAULT_INTERVAL
    expires_in: int = DEFAULT_EXPIRES_IN
    slow_down_polls: int = 0
    deny_next: bool = False
    user: dict[str, Any] = field(default_factory=lambda: dict(_FIXTURE_USER))
    base_url: str = ""  # filled in by create_server so URLs point back at us

    @property
    def base(self) -> str:
        """Absolute origin used to build ``verification_uri_complete``."""
        return self.base_url or f"http://{DEFAULT_HOST}:{DEFAULT_PORT}"


class MockAuthState:
    """Grant bookkeeping, keyed by ``device_code`` so runs never interfere.

    Unsynchronised on purpose: a single-user loopback dev mock with negligible
    concurrency (the update mock's RLock is its own implementation detail, not
    a convention this package must follow).
    """

    def __init__(self, config: MockAuthConfig) -> None:
        self._config = config
        self._grants: dict[str, MockGrant] = {}
        self._tokens: dict[str, MockGrant] = {}

    @property
    def config(self) -> MockAuthConfig:
        """The configuration this state was built with."""
        return self._config

    def issue(self) -> MockGrant:
        """Create and register a brand-new pending grant."""
        now = time.monotonic()
        grant = MockGrant(
            device_code=_new_device_code(),
            user_code=_new_user_code(),
            issued_at=now,
            expires_at=now + self._config.expires_in,
        )
        self._grants[grant.device_code] = grant
        return grant

    def get(self, device_code: str) -> MockGrant | None:
        """Return the grant for ``device_code``, or ``None`` when unknown."""
        return self._grants.get(device_code)

    def by_user_code(self, user_code: str) -> MockGrant | None:
        """Return the grant whose ``user_code`` matches, if any."""
        return next((grant for grant in self._grants.values() if grant.user_code == user_code), None)

    def confirm(self, user_code: str, decision: str) -> MockGrant | None:
        """Record the verify page's decision for ``user_code``."""
        grant = self.by_user_code(user_code)
        if grant is None:
            return None
        denied = decision == "deny" or self._config.deny_next
        if denied:
            self._revoke(grant)
            grant.status = "denied"
        else:
            grant.token = grant.token or f"mock-token-{secrets.token_hex(16)}"
            self._tokens[grant.token] = grant
            grant.status = "granted"
        return grant

    def token_owner(self, token: str) -> MockGrant | None:
        """Return the grant that owns ``token``, or None when unknown/revoked.

        catalog-auth-gating (2026-11-04, supersedes review F2): an issued token
        does NOT expire with its grant — PROD keeps a token valid until the
        user logs out (no server-side expiry), so the mock does too. Grant
        expiry only bounds the device-code login window (see poll()).
        """
        return self._tokens.get(token)

    def poll(self, device_code: str) -> tuple[str, MockGrant | None]:
        """Advance the poll state machine and return ``(error, grant)``.

        ``error`` is ``""`` once the grant is authorized; otherwise it is the
        OAuth device-flow error the caller should surface.
        """
        grant = self._grants.get(device_code)
        if grant is None:
            return EXPIRED_TOKEN, None
        if time.monotonic() >= grant.expires_at:
            self._revoke(grant)
            grant.status = "denied"
            return EXPIRED_TOKEN, grant
        if grant.status == "denied":
            return ACCESS_DENIED, grant
        if grant.status == "granted":
            return "", grant

        grant.polls += 1
        if grant.polls <= self._config.slow_down_polls:
            return SLOW_DOWN, grant
        if self._config.mode == "auto":
            if grant.polls % 2 == 1:
                return PENDING, grant
            return self._authorize(grant), grant
        return PENDING, grant

    def grants(self) -> list[MockGrant]:
        """Return every grant, oldest first (diagnostics and tests)."""
        return list(self._grants.values())

    def _authorize(self, grant: MockGrant) -> str:
        grant.token = grant.token or f"mock-token-{secrets.token_hex(16)}"
        self._tokens[grant.token] = grant
        grant.status = "granted"
        return ""

    def _revoke(self, grant: MockGrant) -> None:
        if grant.token:
            self._tokens.pop(grant.token, None)
            grant.token = ""


def _ok(payload: dict[str, Any]) -> dict[str, Any]:
    """Wrap a successful payload the way the reference mock does."""
    return {"success": True, "message": None, "code": 200, "timestamp": _now_ms(), "e": None, **payload}


def _not_found() -> dict[str, Any]:
    """Return the reference mock's unmatched-route body."""
    return {"success": False, "message": "Mock service not found."}


def _invalid_token() -> dict[str, Any]:
    """Return the reference mock's bad-token body (no ``success`` key)."""
    return {"message": INVALID_TOKEN_MESSAGE, "code": INVALID_TOKEN_CODE, "data": None}


def _grant_payload(state: MockAuthState, grant: MockGrant) -> dict[str, Any]:
    """Return the ``device/code`` result for ``grant``."""
    return {
        "result": {
            "interval": state.config.interval,
            "device_code": grant.device_code,
            "user_code": grant.user_code,
            "verification_uri": f"{state.config.base}{VERIFY_ROUTE}",
            "verification_uri_complete": (
                f"{state.config.base}{VERIFY_ROUTE}?user_code={grant.user_code}&mode={state.config.mode}"
            ),
            "expires_in": state.config.expires_in,
        }
    }


def _token_payload(error: str, grant: MockGrant | None) -> dict[str, Any]:
    """Return the ``device/token`` result: an error, or the token triple.

    ``success`` stays ``true`` in both branches, exactly like the reference
    mock — the caller must branch on ``result.error``.
    """
    token = grant.token if (grant is not None and not error) else None
    return {
        "result": {
            "error": error or None,
            "token": token,
            # The reference mock returns a throwaway access_token; keep it so a
            # client that prefers ``access_token`` still gets a string.
            "access_token": _REFERENCE_ACCESS_TOKEN if token else None,
            "refresh_token": f"mock-refresh-{grant.device_code[:12]}" if (grant is not None and token) else None,
            "token_type": _REFERENCE_TOKEN_TYPE if token else None,
            "expires_in": 0,
            "scope": None,
        }
    }


def _read_json_body(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    """Decode a JSON request body, tolerating an empty or malformed one."""
    length = int(handler.headers.get("Content-Length") or 0)
    if length <= 0:
        return {}
    raw = handler.rfile.read(length)
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except UnicodeDecodeError, json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _read_form_body(handler: BaseHTTPRequestHandler) -> dict[str, str]:
    """Decode an ``application/x-www-form-urlencoded`` body."""
    length = int(handler.headers.get("Content-Length") or 0)
    if length <= 0:
        return {}
    raw = handler.rfile.read(length).decode("utf-8")
    return {key: values[-1] for key, values in parse_qs(raw).items()}


def _verify_page(state: MockAuthState, user_code: str) -> str:
    """Render the local verification page for ``user_code``."""
    grant = state.by_user_code(user_code)
    if grant is None:
        status_line = "No pending device matches this code. Start a new login."
    elif grant.status == "granted":
        status_line = "Already confirmed. You can close this page."
    else:
        status_line = "Denied. Start a new login to try again."
    return _VERIFY_PAGE.format(user_code=user_code, status_line=status_line, mode=state.config.mode)


def build_handler_class(state: MockAuthState) -> type[BaseHTTPRequestHandler]:
    """Return a request handler class bound to ``state``."""

    class MockAuthHandler(BaseHTTPRequestHandler):
        """Routes the three API endpoints plus the local verify page."""

        server_version = "ChrysMockAuth/1.0"

        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_json(self, payload: dict[str, Any]) -> None:
            self._send(200, json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

        def _send_html(self, markup: str) -> None:
            self._send(200, markup.encode("utf-8"), "text/html; charset=utf-8")

        def do_GET(self) -> None:
            """Serve the verification page; everything else 404s."""
            split = urlsplit(self.path)
            if split.path != VERIFY_ROUTE:
                self._send_json(_not_found())
                return
            user_code = parse_qs(split.query).get("user_code", [""])[0]
            self._send_html(_verify_page(state, user_code))

        def do_POST(self) -> None:
            """Dispatch the three API routes and the confirm action."""
            path = urlsplit(self.path).path
            if path == DEVICE_CODE_ROUTE:
                self._handle_device_code()
            elif path == DEVICE_TOKEN_ROUTE:
                self._handle_device_token()
            elif path == USER_INFO_ROUTE:
                self._handle_user_info()
            elif path == VERIFY_CONFIRM_ROUTE:
                self._handle_confirm()
            else:
                self._send_json(_not_found())

        def _handle_device_code(self) -> None:
            _read_json_body(self)
            grant = state.issue()
            _LOGGER.info("issued device_code=%s user_code=%s", grant.device_code, grant.user_code)
            self._send_json(_ok(_grant_payload(state, grant)))

        def _handle_device_token(self) -> None:
            body = _read_json_body(self)
            device_code = str(body.get("device_code") or "")
            error, grant = state.poll(device_code)
            _LOGGER.info("poll device_code=%s -> %s", device_code, error or "authorized")
            self._send_json(_ok(_token_payload(error, grant)))

        def _handle_user_info(self) -> None:
            body = _read_json_body(self)
            token = str(body.get("token") or "")
            if state.token_owner(token) is None:
                self._send_json(_invalid_token())
                return
            self._send_json(_ok({"data": dict(state.config.user)}))

        def _handle_confirm(self) -> None:
            form = _read_form_body(self)
            user_code = form.get("user_code", "")
            grant = state.confirm(user_code, form.get("decision", "allow"))
            if grant is None:
                self._send_html(_verify_page(state, user_code))
                return
            _LOGGER.info("confirm user_code=%s -> %s", user_code, grant.status)
            self.send_response(303)
            self.send_header("Location", f"{VERIFY_ROUTE}?user_code={user_code}")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, format: str, *args: Any) -> None:
            """Route the stdlib access log through ``logging``."""
            _LOGGER.debug("mock_auth %s", format % args)

    return MockAuthHandler


class _ExclusiveBindHTTPServer(ThreadingHTTPServer):
    """Bind without the Windows port-hijack hole.

    ``HTTPServer`` sets ``allow_reuse_address`` (SO_REUSEADDR), which on
    Windows lets a second instance bind a port the first one still holds —
    exactly the stale-mock-process case the fail-fast promise must catch.
    Windows therefore binds with SO_EXCLUSIVEADDRUSE; other platforms keep
    SO_REUSEADDR (a Linux restart needs it to slip past TIME_WAIT).
    """

    allow_reuse_address = 0

    def server_bind(self) -> None:
        if sys.platform == "win32":
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        super().server_bind()


def create_server(
    config: MockAuthConfig | None = None,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
) -> ThreadingHTTPServer:
    """Build a stopped mock server ready for ``serve_forever()``."""
    if host not in _LOOPBACK_HOSTS:
        raise ValueError("The auth mock server may bind only to a loopback address.")
    config = config or MockAuthConfig()
    state = MockAuthState(config)
    server = _ExclusiveBindHTTPServer((host, port), build_handler_class(state))
    server.daemon_threads = True
    # Resolve after binding so ``--port 0`` (ephemeral, used by tests) still
    # hands out a verification URL that points back at this instance.
    config.base_url = f"http://{host}:{server.server_address[1]}"
    server.mock_state = state  # type: ignore[attr-defined]
    server.mock_config = config  # type: ignore[attr-defined]
    return server


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="mock_auth_server",
        description="Loopback mock of the iCode device-code login service.",
    )
    parser.add_argument("--host", default=DEFAULT_HOST, help=f"bind address (default {DEFAULT_HOST})")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"bind port (default {DEFAULT_PORT})")
    parser.add_argument(
        "--mode",
        choices=("manual", "auto"),
        default="manual",
        help="manual waits for the verify page; auto alternates pending/authorized",
    )
    parser.add_argument("--interval", type=int, default=DEFAULT_INTERVAL, help="polling interval handed to the client")
    parser.add_argument("--expires-in", type=int, default=DEFAULT_EXPIRES_IN, help="device code lifetime in seconds")
    parser.add_argument("--deny", action="store_true", help="make every confirmation return access_denied")
    parser.add_argument(
        "--slow-down",
        type=int,
        default=0,
        metavar="N",
        help="answer the first N polls with slow_down to exercise backoff",
    )
    parser.add_argument("--quiet", action="store_true", help="log warnings and errors only")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Run the mock server until interrupted. Returns a process exit code."""
    args = _parse_args(argv)
    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    config = MockAuthConfig(
        mode=args.mode,
        interval=args.interval,
        expires_in=args.expires_in,
        slow_down_polls=args.slow_down,
        deny_next=args.deny,
    )
    try:
        server = create_server(config, host=args.host, port=args.port)
    except OSError as exc:
        _LOGGER.error("cannot bind %s:%s: %s", args.host, args.port, exc)
        return 1

    base = f"http://{args.host}:{args.port}"
    _LOGGER.info("mock auth server on %s (mode=%s)", base, config.mode)
    _LOGGER.info("  POST %s%s", base, DEVICE_CODE_ROUTE)
    _LOGGER.info("  POST %s%s", base, DEVICE_TOKEN_ROUTE)
    _LOGGER.info("  POST %s%s", base, USER_INFO_ROUTE)
    if config.mode == "manual":
        _LOGGER.info("  open  %s%s after starting a login to confirm it", base, VERIFY_ROUTE)
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        _LOGGER.info("shutting down")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
