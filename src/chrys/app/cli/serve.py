# Copyright (c) 2024 Textualize
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# Contains code adapted from textual-serve (MIT License; see NOTICE).

"""Browser-hosted ``chrys serve`` command."""

from __future__ import annotations

import argparse
import asyncio
import errno
import getpass
import hashlib
import hmac
import html
import ipaddress
import json
import logging
import os
import secrets
import shlex
import subprocess
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

import idna
from rich.console import Console

from chrys import __version__
from chrys.app.cli.app import _TUI_SUBPROCESS_COMMAND
from chrys.app.tui.clipboard import BROWSER_CLIPBOARD_META_TYPE
from chrys.app.tui.util.logo import TUI_LOGO
from chrys.foundation.branding import APP_COMMAND, APP_DISPLAY_NAME

_DEFAULT_HOST = "localhost"
_DEFAULT_PORT = 7777
_WILDCARD_HOSTS = frozenset({"0.0.0.0", "::"})
_WARNING_STYLE = "yellow"
_ERROR_STYLE = "red"
_LISTEN_FAILURE_ERRNOS = frozenset(
    code
    for code in (
        getattr(errno, "EADDRINUSE", None),
        getattr(errno, "EACCES", None),
        getattr(errno, "EADDRNOTAVAIL", None),
        getattr(errno, "WSAEADDRINUSE", None),
        getattr(errno, "WSAEACCES", None),
        getattr(errno, "WSAEADDRNOTAVAIL", None),
    )
    if code is not None
)
"""Listen-address failures the user can fix by changing --host or --port."""


def _is_listen_failure(error: OSError) -> bool:
    """Return true when *error* says the requested listen address is unusable."""
    return error.errno in _LISTEN_FAILURE_ERRNOS or getattr(error, "winerror", None) in _LISTEN_FAILURE_ERRNOS


_AUTH_COOKIE_NAME = "chrys_serve_auth"
_AUTH_SESSION_TTL_SECONDS = 12 * 60 * 60
_AUTH_SCRYPT_N = 2**14
_AUTH_SCRYPT_R = 8
_AUTH_SCRYPT_P = 1
_AUTH_LOGIN_PATH = "/auth/login"
_AUTH_LOGOUT_PATH = "/auth/logout"
_AUTH_MAX_FAILURES = 5
_AUTH_LOCKOUT_MESSAGE = "Too many failed password attempts. Browser access is locked."
_AUTH_CSRF_FIELD = "csrf_token"
_AUTH_CSRF_TTL_SECONDS = 10 * 60
_BROWSER_CLIPBOARD_SCRIPT_MARKER = "data-chrys-browser-clipboard"
_BROWSER_BRANDING_STYLE_MARKER = "data-chrys-serve-branding"
_BROWSER_CAPABILITY_SCRIPT_MARKER = "data-chrys-serve-capability"
_BROWSER_CAPABILITY_MESSAGE_TYPE = "chrys_serve_capability"
_BROWSER_CAPABILITY_TIMEOUT_SECONDS = 5.0
_SECURITY_HEADERS = {
    "Content-Security-Policy": "frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Referrer-Policy": "same-origin",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
}

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ServeAuthConfig:
    """Password authentication settings for browser-hosted Chrys."""

    verifier: PasswordVerifier
    cookie_name: str = _AUTH_COOKIE_NAME
    session_ttl_seconds: int = _AUTH_SESSION_TTL_SECONDS
    trust_forwarded_for: bool = False


class _TextualServeServer(Protocol):
    """Subset of textual-serve's Server used by Chrys."""

    def serve(self, *, debug: bool = False) -> None:
        """Start serving browser sessions."""
        ...


class _TextualServeServerFactory(Protocol):
    """Callable constructor for textual-serve's Server."""

    def __call__(
        self,
        command: str,
        *,
        host: str,
        port: int,
        title: str,
        public_url: str | None,
        auth_config: ServeAuthConfig | None = None,
    ) -> _TextualServeServer:
        """Create a textual-serve server."""
        ...


@dataclass(frozen=True)
class PasswordVerifier:
    """Salted password verifier for browser auth."""

    salt: bytes
    digest: bytes

    @classmethod
    def from_password(cls, password: str) -> PasswordVerifier:
        """Hash *password* for later constant-time verification."""
        if not password:
            msg = "browser auth password cannot be empty"
            raise RuntimeError(msg)
        salt = secrets.token_bytes(16)
        return cls(salt=salt, digest=_hash_password(password, salt))

    def verify(self, password: str) -> bool:
        """Return true when *password* matches this verifier."""
        return hmac.compare_digest(self.digest, _hash_password(password, self.salt))


class _AuthSessionStore:
    """In-memory browser auth sessions."""

    def __init__(self, ttl_seconds: int) -> None:
        self._ttl_seconds = ttl_seconds
        self._sessions: dict[str, float] = {}

    def create(self) -> str:
        """Create a new session id and return it."""
        self._prune_expired()
        session_id = secrets.token_urlsafe(32)
        self._sessions[session_id] = time.monotonic() + self._ttl_seconds
        return session_id

    def validate(self, session_id: str | None) -> bool:
        """Return true when *session_id* exists and has not expired."""
        if not session_id:
            return False
        expires_at = self._sessions.get(session_id)
        now = time.monotonic()
        if expires_at is None:
            return False
        if expires_at <= now:
            self._sessions.pop(session_id, None)
            return False
        return True

    def revoke(self, session_id: str | None) -> None:
        """Remove a browser auth session if present."""
        if session_id:
            self._sessions.pop(session_id, None)

    def _prune_expired(self) -> None:
        """Remove expired sessions opportunistically."""
        now = time.monotonic()
        expired = [session_id for session_id, expires_at in self._sessions.items() if expires_at <= now]
        for session_id in expired:
            self._sessions.pop(session_id, None)


class _LoginCsrfStore:
    """Short-lived one-time CSRF tokens for the browser auth form."""

    def __init__(self, ttl_seconds: int = _AUTH_CSRF_TTL_SECONDS) -> None:
        self._ttl_seconds = ttl_seconds
        self._tokens: dict[str, float] = {}

    def create(self) -> str:
        """Create and remember a new CSRF token."""
        self._prune_expired()
        token = secrets.token_urlsafe(32)
        self._tokens[token] = time.monotonic() + self._ttl_seconds
        return token

    def consume(self, token: str | None) -> bool:
        """Consume *token* and return true when it existed and had not expired."""
        if not token:
            return False
        expires_at = self._tokens.pop(token, None)
        if expires_at is None:
            return False
        return expires_at > time.monotonic()

    def _prune_expired(self) -> None:
        """Remove expired CSRF tokens opportunistically."""
        now = time.monotonic()
        expired = [token for token, expires_at in self._tokens.items() if expires_at <= now]
        for token in expired:
            self._tokens.pop(token, None)


class _LoginRateLimiter:
    """Process-lifetime failed-login limiter keyed by client address.

    Clearing a locked client requires restarting ``chrys serve``.
    """

    def __init__(
        self,
        *,
        max_failures: int = _AUTH_MAX_FAILURES,
    ) -> None:
        self._max_failures = max_failures
        self._failures: dict[str, int] = {}
        self._locked: set[str] = set()

    def is_locked(self, key: str) -> bool:
        """Return true when *key* is locked until process restart."""
        return key in self._locked

    def record_failure(self, key: str) -> bool:
        """Record one failed login attempt for *key* and return true if locked."""
        if key in self._locked:
            return True
        failures = self._failures.get(key, 0) + 1
        if failures >= self._max_failures:
            self._locked.add(key)
            self._failures.pop(key, None)
            return True
        self._failures[key] = failures
        return False

    def record_success(self, key: str) -> None:
        """Clear failed-login state for *key*."""
        self._failures.pop(key, None)


def build_parser() -> argparse.ArgumentParser:
    """Build the ``chrys serve`` argument parser."""
    parser = argparse.ArgumentParser(
        prog=f"{APP_COMMAND} serve",
        description=f"Host the {APP_DISPLAY_NAME} TUI in a browser.",
        add_help=False,
    )
    parser.add_argument(
        "-h", "--help", action="help", default=argparse.SUPPRESS, help="Show this help message and exit"
    )
    parser.add_argument(
        "--host",
        default=_DEFAULT_HOST,
        help="Host or IP address to bind; use 0.0.0.0 or :: to listen on all interfaces.",
    )
    parser.add_argument(
        "--port",
        type=_parse_port,
        default=_DEFAULT_PORT,
        help=f"Port to bind (default: {_DEFAULT_PORT}).",
    )
    parser.add_argument(
        "--public-url",
        default=None,
        help=(
            "Externally reachable URL to show in browser assets when behind a proxy or bound to all interfaces. "
            "Reverse proxies must preserve the original Host header. For HTTPS proxy auth, the bound HTTP port "
            "should not be reachable bypassing the proxy."
        ),
    )
    parser.add_argument(
        "--auth-required",
        "--auth",
        dest="auth_required",
        action="store_true",
        help=f"Require password authentication before browsers can access the {APP_DISPLAY_NAME} TUI.",
    )
    password_source = parser.add_mutually_exclusive_group()
    password_source.add_argument(
        "--auth-password-file",
        type=Path,
        default=None,
        help="Read the browser auth password from a file instead of prompting.",
    )
    password_source.add_argument(
        "--auth-password-env",
        default=None,
        metavar="ENV_VAR",
        help="Read the browser auth password from an environment variable instead of prompting.",
    )
    parser.add_argument(
        "--allow-insecure-auth",
        action="store_true",
        help=(
            "Allow browser auth over non-loopback HTTP. Use only on trusted networks or through a tunnel; "
            "passwords can be observed if the port is exposed."
        ),
    )
    parser.add_argument(
        "--auth-trust-forwarded-for",
        action="store_true",
        help=(
            "Trust X-Forwarded-For for browser auth lockout keys. Use only behind a trusted reverse proxy that "
            "overwrites this header."
        ),
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable textual-serve debug mode and Textual devtools for spawned app sessions.",
    )
    return parser


def build_tui_command() -> str:
    """Return the shell command used by textual-serve to launch the Chrys TUI."""
    return _shell_join([*_entrypoint_argv(), _TUI_SUBPROCESS_COMMAND])


def _entrypoint_argv() -> list[str]:
    """Return a Chrys entrypoint argv that works for Python installs and PyApp."""
    pyapp_launcher = os.environ.get("PYAPP")
    if pyapp_launcher:
        return [pyapp_launcher]
    if _looks_like_python(sys.executable):
        return [sys.executable, "-m", "chrys.app.cli.app"]
    return [sys.argv[0]]


def _looks_like_python(executable: str) -> bool:
    name = Path(executable).name.lower()
    return name.startswith(("python", "pypy"))


def _parse_port(value: str) -> int:
    try:
        port = int(value)
    except ValueError as exc:
        msg = f"invalid port: {value!r}"
        raise argparse.ArgumentTypeError(msg) from exc
    if not 1 <= port <= 65535:
        msg = f"port must be between 1 and 65535: {port}"
        raise argparse.ArgumentTypeError(msg)
    return port


def _to_int(value: str | None, default: int) -> int:
    """Parse *value* as an int, returning *default* for invalid values."""
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _shell_join(argv: Sequence[str]) -> str:
    """Join argv for the platform shell used by ``create_subprocess_shell``."""
    if os.name == "nt":
        return subprocess.list2cmdline(list(argv))
    return shlex.join(argv)


def _load_server_class() -> _TextualServeServerFactory:
    from textual_serve.server import Server

    return _make_chrys_server_class(Server)


def _uses_textual_serve_base(base_server_class: type[Any]) -> bool:
    """Return true when *base_server_class* is the real textual-serve server."""
    return base_server_class.__module__.startswith("textual_serve.")


def _browser_clipboard_websocket_message(text: str) -> str:
    """Return the browser websocket message used to request a clipboard write."""
    return json.dumps([BROWSER_CLIPBOARD_META_TYPE, {"text": text}])


async def _send_browser_clipboard_message(meta_data: dict[str, object], remote_write_str: Any) -> bool:
    """Forward a Chrys clipboard meta packet to the browser websocket."""
    if meta_data.get("type") != BROWSER_CLIPBOARD_META_TYPE:
        return False
    text = meta_data.get("text")
    if not isinstance(text, str):
        logger.warning("Ignoring malformed browser clipboard metadata: text must be a string.")
        return True
    await remote_write_str(_browser_clipboard_websocket_message(text))
    return True


def _browser_capability_script(capability: str) -> str:
    """Return a same-origin bootstrap that authenticates browser WebSockets."""
    script = r"""<script data-chrys-serve-capability>
(() => {
  const MESSAGE_TYPE = __MESSAGE_TYPE__;
  const CAPABILITY = __CAPABILITY__;
  const NativeWebSocket = window.WebSocket;

  function isTerminalSocket(url) {
    const terminal = document.querySelector("[data-session-websocket-url]");
    if (!terminal) {
      return false;
    }
    const expected = new URL(terminal.dataset.sessionWebsocketUrl, window.location.href);
    const actual = new URL(url, window.location.href);
    return actual.origin === expected.origin && actual.pathname === expected.pathname;
  }

  // textual-serve constructs its socket after window.load. Registering this
  // listener in the constructor guarantees the capability packet is queued
  // before textual-serve's open listener sends resize, focus, or input data.
  window.WebSocket = class ChrysAuthenticatedWebSocket extends NativeWebSocket {
    constructor(url, protocols) {
      if (protocols === undefined) {
        super(url);
      } else {
        super(url, protocols);
      }
      if (isTerminalSocket(url)) {
        this.addEventListener("open", () => {
          this.send(JSON.stringify([MESSAGE_TYPE, CAPABILITY]));
        }, {once: true});
      }
    }
  };
})();
</script>"""
    return script.replace("__MESSAGE_TYPE__", json.dumps(_BROWSER_CAPABILITY_MESSAGE_TYPE)).replace(
        "__CAPABILITY__", json.dumps(capability)
    )


async def _receive_browser_capability(websocket: Any, expected_capability: str) -> bool:
    """Validate the first browser WebSocket packet before starting Chrys."""
    try:
        async with asyncio.timeout(_BROWSER_CAPABILITY_TIMEOUT_SECONDS):
            message = await websocket.receive()
    except TimeoutError:
        return False
    if not isinstance(message.data, str):
        return False
    try:
        packet = json.loads(message.data)
    except json.JSONDecodeError:
        return False
    if (
        not isinstance(packet, list)
        or len(packet) != 2
        or packet[0] != _BROWSER_CAPABILITY_MESSAGE_TYPE
        or not isinstance(packet[1], str)
    ):
        return False
    return hmac.compare_digest(
        packet[1].encode("utf-8", errors="surrogatepass"),
        expected_capability.encode(),
    )


def _browser_clipboard_support_script() -> str:
    """Return client-side support for browser clipboard writes and fallback UI."""
    script = r"""<script data-chrys-browser-clipboard>
(() => {
  const TYPE = __TYPE__;
  const DIALOG_ID = "chrys-browser-copy-dialog";
  const STYLE_ID = "chrys-browser-copy-style";

  function fallbackCopy(text) {
    const textarea = document.createElement("textarea");
    textarea.value = text;
    textarea.setAttribute("readonly", "readonly");
    textarea.style.position = "fixed";
    textarea.style.left = "-10000px";
    textarea.style.top = "0";
    document.body.appendChild(textarea);
    textarea.focus();
    textarea.select();
    try {
      return document.execCommand("copy");
    } catch {
      return false;
    } finally {
      textarea.remove();
    }
  }

  async function writeClipboard(text) {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
      return true;
    }
    return fallbackCopy(text);
  }

  async function writeClipboardAutomatically(text) {
    if (!(navigator.clipboard && window.isSecureContext)) {
      return false;
    }
    await navigator.clipboard.writeText(text);
    return true;
  }

  function ensureStyles() {
    if (document.getElementById(STYLE_ID)) {
      return;
    }
    const style = document.createElement("style");
    style.id = STYLE_ID;
    style.textContent = `
      #${DIALOG_ID} {
        position: fixed;
        inset: 0;
        z-index: 2147483647;
        display: flex;
        align-items: center;
        justify-content: center;
        background: rgba(0, 0, 0, 0.54);
        color: #eef1f6;
        font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      }
      #${DIALOG_ID} .chrys-copy-card {
        width: min(780px, calc(100vw - 32px));
        max-height: calc(100vh - 32px);
        box-sizing: border-box;
        display: grid;
        grid-template-rows: auto minmax(260px, auto) auto auto;
        gap: 8px;
        overflow: hidden;
        padding: 18px 18px 16px;
        border: 1px solid #3a4354;
        border-radius: 8px;
        background: #10141c;
        box-shadow: 0 24px 80px rgba(0, 0, 0, 0.5);
      }
      #${DIALOG_ID} .chrys-copy-title {
        font-size: 15px;
        font-weight: 700;
      }
      #${DIALOG_ID} textarea {
        width: 100%;
        height: min(430px, 54vh);
        max-height: calc(100vh - 220px);
        min-height: min(260px, calc(100vh - 220px));
        resize: vertical;
        box-sizing: border-box;
        padding: 10px;
        border: 1px solid #303744;
        border-radius: 6px;
        color: #f8fafc;
        background: #080a0f;
        font: 13px/1.45 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      }
      #${DIALOG_ID} .chrys-copy-actions {
        display: flex;
        justify-content: flex-end;
        gap: 4px;
      }
      #${DIALOG_ID} button {
        display: inline-flex;
        align-items: center;
        justify-content: center;
        min-height: 36px;
        min-width: 64px;
        margin: 0;
        padding: 0 12px;
        border: 1px solid #3a4354;
        border-radius: 6px;
        color: #eef1f6;
        background: #1c2330;
        font: 700 13px/1 ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
        cursor: pointer;
      }
      #${DIALOG_ID} button:hover {
        background: #263244;
      }
      #${DIALOG_ID} button[data-primary="true"] {
        color: #ffffff;
        background: #1769e0;
        border-color: #2f89ff;
        box-shadow: 0 0 0 1px rgba(47, 137, 255, 0.24);
      }
      #${DIALOG_ID} button[data-primary="true"]:hover {
        background: #2180ff;
        border-color: #61a8ff;
      }
      #${DIALOG_ID} .chrys-copy-status {
        min-height: 0;
        color: #c3cbd8;
        font-size: 12px;
      }
      #${DIALOG_ID} .chrys-copy-status:empty {
        display: none;
      }
    `;
    document.head.appendChild(style);
  }

  function closeDialog() {
    document.getElementById(DIALOG_ID)?.remove();
  }

  function showDialog(text) {
    ensureStyles();
    closeDialog();
    const overlay = document.createElement("div");
    overlay.id = DIALOG_ID;

    const card = document.createElement("div");
    card.className = "chrys-copy-card";

    const title = document.createElement("div");
    title.className = "chrys-copy-title";
    title.textContent = __APP_DISPLAY_NAME__;

    const textarea = document.createElement("textarea");
    textarea.value = text;

    const status = document.createElement("div");
    status.className = "chrys-copy-status";

    const actions = document.createElement("div");
    actions.className = "chrys-copy-actions";

    const close = document.createElement("button");
    close.type = "button";
    close.textContent = "Close";
    close.addEventListener("click", closeDialog);

    const copy = document.createElement("button");
    copy.type = "button";
    copy.dataset.primary = "true";
    copy.textContent = "Copy";
    copy.addEventListener("click", async () => {
      try {
        if (await writeClipboard(text)) {
          closeDialog();
          return;
        }
      } catch {}
      textarea.focus();
      textarea.select();
      status.textContent = "Copy is blocked. Use the selected text with your browser copy shortcut.";
    });

    actions.append(close, copy);
    card.append(title, textarea, status, actions);
    overlay.appendChild(card);
    overlay.addEventListener("click", (event) => {
      if (event.target === overlay) {
        closeDialog();
      }
    });
    document.body.appendChild(overlay);
    copy.focus();
  }

  async function handleClipboardMessage(text) {
    try {
      if (await writeClipboardAutomatically(text)) {
        return;
      }
    } catch {}
    showDialog(text);
  }

  // textual-serve serves one WebSocket-driven terminal per page. This wrapper
  // assumes that page shape; if future pages host unrelated WebSockets, move
  // this hook to the terminal's specific socket setup instead of wrapping the
  // prototype globally.
  const originalAddEventListener = WebSocket.prototype.addEventListener;
  WebSocket.prototype.addEventListener = function(type, listener, options) {
    if (type !== "message" || typeof listener !== "function") {
      return originalAddEventListener.call(this, type, listener, options);
    }
    const wrapped = function(event) {
      if (typeof event.data === "string") {
        try {
          const packet = JSON.parse(event.data);
          if (Array.isArray(packet) && packet[0] === TYPE) {
            const payload = packet[1] || {};
            handleClipboardMessage(typeof payload.text === "string" ? payload.text : "");
            return;
          }
        } catch {}
      }
      return listener.call(this, event);
    };
    return originalAddEventListener.call(this, type, wrapped, options);
  };
})();
</script>"""
    return script.replace("__TYPE__", json.dumps(BROWSER_CLIPBOARD_META_TYPE)).replace(
        "__APP_DISPLAY_NAME__", json.dumps(f"Copy from {APP_DISPLAY_NAME}")
    )


def _browser_serve_branding_style() -> str:
    """Return CSS that replaces textual-serve splash branding with Chrys branding."""
    return f"""<style {_BROWSER_BRANDING_STYLE_MARKER}>
      .intro {{
        width: min(720px, calc(100vw - 32px));
        height: auto;
        min-height: 260px;
        box-sizing: border-box;
        flex-direction: column;
      }}
      .intro > svg,
      .intro > svg + div {{
        display: none !important;
      }}
      .intro .chrys-serve-logo {{
        margin: 0;
        padding: 0;
        color: rgba(255, 255, 255, 0.95);
        font: 700 16px/1 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
        letter-spacing: 0;
        white-space: pre;
        text-align: left;
      }}
    </style>"""


def _inject_browser_serve_branding_logo(body: str) -> str:
    """Inject the Chrys TUI logo into textual-serve's splash markup."""
    if "chrys-serve-logo" in body:
        return body
    logo = html.escape(TUI_LOGO.strip("\n"))
    intro_marker = '<div class="intro">'
    if intro_marker not in body:
        return body
    label = html.escape(APP_DISPLAY_NAME, quote=True)
    return body.replace(
        intro_marker, f'{intro_marker}<pre class="chrys-serve-logo" aria-label="{label}">{logo}</pre>', 1
    )


def _make_chrys_server_class(base_server_class: type[Any]) -> _TextualServeServerFactory:
    """Return a textual-serve server class with Chrys-branded startup output."""
    from aiohttp import web

    use_browser_clipboard_bridge = _uses_textual_serve_base(base_server_class)
    response_prepared_request_key = web.RequestKey("chrys_serve_response_prepared", bool)

    if use_browser_clipboard_bridge:
        from textual_serve.app_service import AppService as TextualServeAppService

        # Relies on textual-serve 1.1.3 internals: AppService.on_meta() and
        # Server.handle_websocket() constructing AppService directly. Keep
        # tests/app/cli/test_serve.py's contract check aligned when upgrading.
        class ChrysAppService(TextualServeAppService):
            """textual-serve app service that forwards Chrys clipboard meta packets."""

            async def on_meta(self, data: bytes) -> None:
                meta_data: dict[str, object] = json.loads(data)
                if await _send_browser_clipboard_message(meta_data, self.remote_write_str):
                    return
                await super().on_meta(data)

    class ChrysServeServer(base_server_class):
        """textual-serve Server with Chrys-branded startup output."""

        def __init__(
            self,
            command: str,
            *,
            host: str,
            port: int,
            title: str,
            public_url: str | None,
            auth_config: ServeAuthConfig | None = None,
        ) -> None:
            """Create a Chrys browser server."""
            effective_public_url = _normalize_public_url(
                _effective_public_url(host=host, port=port, public_url=public_url)
            )
            super().__init__(
                command,
                host=host,
                port=port,
                title=title,
                public_url=effective_public_url,
            )
            self._auth_config = auth_config
            self._auth_sessions = (
                _AuthSessionStore(auth_config.session_ttl_seconds) if auth_config is not None else None
            )
            self._auth_rate_limiter = _LoginRateLimiter() if auth_config is not None else None
            self._auth_csrf_tokens = _LoginCsrfStore() if auth_config is not None else None
            self._browser_capability = secrets.token_urlsafe(32)

        async def _make_app(self) -> Any:
            """Create the aiohttp app and install security/auth middleware."""
            app = await super()._make_app()
            app.middlewares.insert(0, self._security_middleware)
            app.on_response_prepare.append(self._prepare_browser_security_headers)
            if use_browser_clipboard_bridge:
                app.middlewares.append(self._browser_support_middleware)
            if self._auth_config is not None:
                app.middlewares.append(self._auth_middleware)
                app.add_routes(
                    [
                        web.get(_AUTH_LOGIN_PATH, self.handle_auth_login_page, name="auth_login"),
                        web.post(_AUTH_LOGIN_PATH, self.handle_auth_login_submit, name="auth_login_submit"),
                        web.get(_AUTH_LOGOUT_PATH, self.handle_auth_logout, name="auth_logout"),
                    ]
                )
            return app

        async def _prepare_browser_security_headers(self, request: Any, response: Any) -> None:
            """Apply security headers before aiohttp sends a prepared response."""
            _apply_browser_security_headers(response)
            request[response_prepared_request_key] = True

        # Relies on textual-serve 1.1.3 internals: on_startup(), self.console,
        # and self.public_url. Re-audit this override when bumping textual-serve.
        async def on_startup(self, _app: object) -> None:
            """Print the Chrys serve banner."""
            self.console.print("")
            self.console.print(_icode_serve_logo(), highlight=False)
            self.console.print(
                f"Serving {APP_DISPLAY_NAME} TUI on {self.public_url} [cyan](Press Ctrl+C to quit)[/cyan]\n"
            )

        async def handle_websocket(self, request: Any) -> Any:
            """Handle browser websocket sessions with Chrys clipboard metadata support."""
            if not _request_matches_public_origin(request, self.public_url, allow_referer=False):
                raise web.HTTPForbidden(text="Invalid WebSocket origin")
            if not use_browser_clipboard_bridge:
                return await super().handle_websocket(request)

            # Intentional copy of textual-serve 1.1.3's handle_websocket()
            # with AppService swapped for ChrysAppService. This preserves
            # upstream's stop-on-normal-path plus stop-in-finally behavior.
            websocket = web.WebSocketResponse(heartbeat=15)
            width = _to_int(request.query.get("width"), 80)
            height = _to_int(request.query.get("height"), 24)

            app_service: Any | None = None
            try:
                await websocket.prepare(request)
                if not await _receive_browser_capability(websocket, self._browser_capability):
                    await websocket.close(code=1008, message=b"Invalid browser capability")
                    return websocket

                async def close_websocket() -> None:
                    await websocket.close()

                app_service = ChrysAppService(
                    self.command,
                    write_bytes=websocket.send_bytes,
                    write_str=websocket.send_str,
                    close=close_websocket,
                    download_manager=self.download_manager,
                    debug=self.debug,
                )
                await app_service.start(width, height)
                try:
                    await self._process_messages(websocket, app_service)
                finally:
                    await app_service.stop()
            except asyncio.CancelledError:
                await websocket.close()
            except Exception:
                logger.exception("Browser websocket session failed")
            finally:
                if app_service is not None:
                    await app_service.stop()

            return websocket

        @web.middleware
        async def _security_middleware(self, request: Any, handler: Any) -> Any:
            """Reject unrecognized authorities and apply browser hardening headers."""
            if not _request_matches_public_host(request, self.public_url):
                response = web.Response(status=421, text="Invalid Host header")
                return _apply_browser_security_headers(response)
            try:
                response = await handler(request)
            except web.HTTPException as exc:
                if request.get(response_prepared_request_key, False):
                    msg = "HTTP exception raised after response headers were sent"
                    raise RuntimeError(msg) from exc
                _apply_browser_security_headers(exc)
                raise
            except Exception:
                if request.get(response_prepared_request_key, False):
                    raise
                logger.exception("Unhandled browser HTTP request")
                response = web.Response(status=500, text="Internal Server Error")
                return _apply_browser_security_headers(response)
            if not response.prepared:
                _apply_browser_security_headers(response)
            return response

        @web.middleware
        async def _browser_support_middleware(self, request: Any, handler: Any) -> Any:
            """Inject Chrys browser support into textual-serve's app page."""
            response = await handler(request)
            if request.method != "GET" or not isinstance(response, web.Response):
                return response
            if response.content_type != "text/html" or response.body is None:
                return response

            charset = response.charset or "utf-8"
            try:
                body = response.body.decode(charset)
            except UnicodeDecodeError:
                return response
            if "data-session-websocket-url" not in body or "</head>" not in body:
                return response

            branding = "" if _BROWSER_BRANDING_STYLE_MARKER in body else _browser_serve_branding_style()
            capability = (
                ""
                if _BROWSER_CAPABILITY_SCRIPT_MARKER in body
                else _browser_capability_script(self._browser_capability)
            )
            clipboard = "" if _BROWSER_CLIPBOARD_SCRIPT_MARKER in body else _browser_clipboard_support_script()
            body = _inject_browser_serve_branding_logo(body)
            response.text = body.replace(
                "</head>",
                f"{branding}{capability}{clipboard}</head>",
                1,
            )
            return response

        @web.middleware
        async def _auth_middleware(self, request: Any, handler: Any) -> Any:
            """Require a valid auth session for TUI entrypoints."""
            if self._auth_config is None or self._auth_sessions is None:
                return await handler(request)
            if _is_auth_public_path(request.path):
                return await handler(request)
            if self._auth_sessions.validate(request.cookies.get(self._auth_config.cookie_name)):
                return await handler(request)
            if request.method == "GET" and request.path == "/":
                return self._render_auth_login_page()
            raise web.HTTPUnauthorized(text="Authentication required")

        async def handle_auth_login_page(self, _request: Any) -> Any:
            """Render the browser auth login page."""
            return self._render_auth_login_page()

        async def handle_auth_login_submit(self, request: Any) -> Any:
            """Verify the browser auth password and create a session cookie."""
            if (
                self._auth_config is None
                or self._auth_sessions is None
                or self._auth_rate_limiter is None
                or self._auth_csrf_tokens is None
            ):
                raise web.HTTPNotFound()
            if not _request_matches_public_origin(request, self.public_url):
                raise web.HTTPForbidden(text="Invalid login origin")

            remote_key = _request_remote_key(request, trust_forwarded_for=self._auth_config.trust_forwarded_for)
            if self._auth_rate_limiter.is_locked(remote_key):
                return self._render_auth_login_page(status=429, error=_AUTH_LOCKOUT_MESSAGE)

            form = await request.post()
            token = form.get(_AUTH_CSRF_FIELD, "")
            if not isinstance(token, str) or not self._auth_csrf_tokens.consume(token):
                raise web.HTTPForbidden(text="Invalid login token")

            password = form.get("password", "")
            if not isinstance(password, str) or not self._auth_config.verifier.verify(password):
                if self._auth_rate_limiter.record_failure(remote_key):
                    return self._render_auth_login_page(status=429, error=_AUTH_LOCKOUT_MESSAGE)
                return self._render_auth_login_page(status=401, error="Invalid password.")

            self._auth_rate_limiter.record_success(remote_key)
            session_id = self._auth_sessions.create()
            response = web.HTTPFound(_browser_root_path(self.public_url))
            response.set_cookie(
                self._auth_config.cookie_name,
                session_id,
                max_age=self._auth_config.session_ttl_seconds,
                httponly=True,
                secure=_is_https_url(self.public_url),
                samesite="Strict",
                path=_auth_cookie_path(self.public_url),
            )
            raise response

        async def handle_auth_logout(self, request: Any) -> Any:
            """Revoke the browser auth session cookie."""
            if self._auth_config is not None and self._auth_sessions is not None:
                self._auth_sessions.revoke(request.cookies.get(self._auth_config.cookie_name))
            cookie_name = self._auth_config.cookie_name if self._auth_config is not None else _AUTH_COOKIE_NAME
            response = web.HTTPFound(_browser_path(self.public_url, _AUTH_LOGIN_PATH))
            response.del_cookie(cookie_name, path=_auth_cookie_path(self.public_url))
            raise response

        def _render_auth_login_page(self, *, status: int = 200, error: str = "") -> Any:
            """Render a standalone login page before the TUI starts."""
            title = html.escape(self.title)
            logo = html.escape(TUI_LOGO.strip("\n"))
            error_html = f'<div class="error">{html.escape(error)}</div>' if error else ""
            csrf_token = html.escape(self._auth_csrf_tokens.create() if self._auth_csrf_tokens is not None else "")
            login_action = html.escape(_browser_path(self.public_url, _AUTH_LOGIN_PATH), quote=True)
            return web.Response(
                status=status,
                text=f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>{title} Authentication</title>
    <style>
      :root {{
        color-scheme: dark;
        font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
        background: #141923;
        color: #eef1f6;
      }}
      body {{
        min-height: 100vh;
        margin: 0;
        display: grid;
        place-items: center;
        background: #141923;
      }}
      main {{
        width: min(440px, calc(100vw - 32px));
        box-sizing: border-box;
        padding: 32px;
        background: #10141c;
        border: 1px solid #333b49;
        border-radius: 8px;
        box-shadow: 0 30px 90px rgba(0, 0, 0, 0.56), 0 0 0 1px rgba(255, 255, 255, 0.03);
      }}
      .logo {{
        width: fit-content;
        max-width: 100%;
        margin: 0 auto 30px;
        overflow: hidden;
        color: #f8fafc;
        font: 700 10px/1.05 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
        letter-spacing: 0;
        white-space: pre;
      }}
      label {{
        display: block;
        margin-bottom: 8px;
        color: #a8b0bd;
        font-size: 13px;
        line-height: 1.3;
      }}
      input {{
        width: 100%;
        box-sizing: border-box;
        min-height: 44px;
        padding: 10px 12px;
        color: #f8fafc;
        background: #080a0f;
        border: 1px solid #303744;
        border-radius: 6px;
        font: 15px/1.4 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      }}
      input:focus {{
        border-color: #8ca3ff;
        outline: 2px solid #8ca3ff;
        outline-offset: 2px;
      }}
      button {{
        width: 100%;
        min-height: 44px;
        margin-top: 18px;
        border: 0;
        border-radius: 6px;
        color: #080a0f;
        background: #f8fafc;
        font: 700 14px/1.2 ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
        cursor: pointer;
      }}
      button:hover {{
        background: #dbe4f0;
      }}
      .error {{
        margin-bottom: 16px;
        color: #ffd7d7;
        background: #2a1116;
        border: 1px solid #67313a;
        border-radius: 6px;
        padding: 10px 12px;
        font-size: 13px;
        line-height: 1.4;
      }}
    </style>
  </head>
  <body>
    <main>
      <pre class="logo" aria-label="{title}">{logo}</pre>
      {error_html}
      <form method="post" action="{login_action}">
        <input type="hidden" name="{_AUTH_CSRF_FIELD}" value="{csrf_token}">
        <label for="password">Password</label>
        <input id="password" name="password" type="password" autocomplete="current-password" autofocus required>
        <button type="submit">Unlock</button>
      </form>
    </main>
  </body>
</html>
""",
                content_type="text/html",
                charset="utf-8",
                headers={
                    "Cache-Control": "no-store",
                    "Content-Security-Policy": (
                        "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; "
                        "base-uri 'none'; frame-ancestors 'none'"
                    ),
                },
            )

    return ChrysServeServer


def _icode_serve_logo() -> str:
    """Return the iCode-branded serve startup banner."""
    return f"""[bold magenta]░▀█▀░█▀▀░█▀█░█▀▄░█▀▀░░░░░█▀▀░█▀▀░█▀▄░█░█░█▀▀
░░█░░█░░░█░█░█░█░█▀▀░▄▄▄░▀▀█░█▀▀░█▀▄░▀▄▀░█▀▀
░▀▀▀░▀▀▀░▀▀▀░▀▀░░▀▀▀░░░░░▀▀▀░▀▀▀░▀░▀░░▀░░▀▀▀ v{__version__}[/bold magenta]\n"""


def run_command(args: argparse.Namespace) -> None:
    """Run textual-serve with the parsed command arguments."""
    effective_public_url = _normalize_public_url(
        _effective_public_url(host=args.host, port=args.port, public_url=args.public_url)
    )
    auth_requested = _auth_requested(args)
    if auth_requested:
        _validate_auth_transport_security(args)
    auth_config = _resolve_auth_config(args)
    _warn_if_public_url_missing_for_wildcard_host(host=args.host, port=args.port, public_url=args.public_url)
    if auth_config is not None and args.allow_insecure_auth:
        _warn_if_auth_uses_plain_http(host=args.host, port=args.port, public_url=args.public_url)
    server_class = _load_server_class()
    server_kwargs: dict[str, Any] = {
        "host": args.host,
        "port": args.port,
        "title": APP_DISPLAY_NAME,
        "public_url": effective_public_url,
    }
    if auth_config is not None:
        server_kwargs["auth_config"] = auth_config
    server = server_class(
        build_tui_command(),
        **server_kwargs,
    )
    try:
        server.serve(debug=args.debug)
    except OSError as exc:
        if not _is_listen_failure(exc):
            raise
        raise RuntimeError(f"cannot listen on {args.host}:{args.port}: {exc.strerror or exc}") from exc


def _resolve_auth_config(args: argparse.Namespace) -> ServeAuthConfig | None:
    """Resolve browser auth configuration from parsed CLI args."""
    if not _auth_requested(args):
        return None
    if args.auth_password_file is not None:
        password = _read_password_file(args.auth_password_file)
    elif args.auth_password_env is not None:
        password = _read_password_env(args.auth_password_env)
    else:
        password = _prompt_for_password()
    return ServeAuthConfig(
        verifier=PasswordVerifier.from_password(password),
        trust_forwarded_for=getattr(args, "auth_trust_forwarded_for", False),
    )


def _auth_requested(args: argparse.Namespace) -> bool:
    """Return true when any browser auth option requests auth."""
    return args.auth_required or args.auth_password_file is not None or args.auth_password_env is not None


def _validate_auth_transport_security(args: argparse.Namespace) -> None:
    """Reject browser auth over externally reachable HTTP unless explicitly allowed."""
    if args.allow_insecure_auth:
        return
    if _auth_transport_requires_insecure_override(host=args.host, port=args.port, public_url=args.public_url):
        msg = (
            "browser auth over non-loopback HTTP is unsafe because passwords can be observed on the network. "
            "Use an HTTPS --public-url through a reverse proxy, bind to localhost, or pass --allow-insecure-auth "
            "only on a trusted network or tunnel."
        )
        raise RuntimeError(msg)


def _normalize_public_url(public_url: str) -> str:
    """Validate and normalize a browser-facing URL for textual-serve."""
    origin = _origin_tuple(public_url)
    if origin is None:
        msg = "--public-url must be an absolute http:// or https:// URL without credentials, query, or fragment"
        raise RuntimeError(msg)
    parsed = urlsplit(public_url)
    if parsed.query or parsed.fragment or _path_requires_browser_rewrite(parsed.path):
        msg = (
            "--public-url must be an absolute http:// or https:// URL without credentials, query, fragment, "
            "backslashes, or dot path segments"
        )
        raise RuntimeError(msg)
    scheme, host, port = origin
    url_host = _format_url_host(host)
    default_port = 80 if scheme == "http" else 443
    authority = url_host if port == default_port else f"{url_host}:{port}"
    path = parsed.path.strip("/")
    base_path = f"/{path}" if path else ""
    return f"{scheme}://{authority}{base_path}"


def _path_requires_browser_rewrite(path: str) -> bool:
    """Return true when a special-URL browser would rewrite *path*."""
    if "\\" in path:
        return True
    browser_dot_segments = {".", "%2e", "..", ".%2e", "%2e.", "%2e%2e"}
    return any(segment.lower() in browser_dot_segments for segment in path.split("/"))


def _read_password_file(path: Path) -> str:
    """Read a browser auth password from *path*."""
    expanded = path.expanduser()
    try:
        return expanded.read_text(encoding="utf-8").rstrip("\r\n")
    except (OSError, UnicodeError) as exc:
        msg = f"failed to read --auth-password-file {expanded}: {exc}"
        raise RuntimeError(msg) from exc


def _read_password_env(env_name: str) -> str:
    """Read a browser auth password from an environment variable, then remove it."""
    password = os.environ.get(env_name)
    if password is None:
        msg = f"--auth-password-env {env_name!r} is not set"
        raise RuntimeError(msg)
    os.environ.pop(env_name, None)
    return password


def _prompt_for_password() -> str:
    """Prompt securely for the browser auth password."""
    password = getpass.getpass("Password for browser access: ")
    confirm = getpass.getpass("Confirm password: ")
    if password != confirm:
        msg = "browser auth passwords did not match"
        raise RuntimeError(msg)
    return password


def _hash_password(password: str, salt: bytes) -> bytes:
    """Hash a browser auth password with scrypt."""
    return hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_AUTH_SCRYPT_N,
        r=_AUTH_SCRYPT_R,
        p=_AUTH_SCRYPT_P,
    )


def _is_auth_public_path(path: str) -> bool:
    """Return true for routes that must be reachable before login."""
    return path == _AUTH_LOGIN_PATH


def _request_remote_key(request: Any, *, trust_forwarded_for: bool = False) -> str:
    """Return the client key used for login rate limiting."""
    if trust_forwarded_for:
        forwarded_for = _trusted_forwarded_for(request)
        if forwarded_for is not None:
            return forwarded_for
    if request.remote:
        return str(request.remote)
    peername = request.transport.get_extra_info("peername") if request.transport is not None else None
    if isinstance(peername, tuple) and peername:
        return str(peername[0])
    return "unknown"


def _trusted_forwarded_for(request: Any) -> str | None:
    """Return a trusted X-Forwarded-For client address if the header is valid."""
    forwarded_for = request.headers.get("X-Forwarded-For")
    if forwarded_for is None:
        return None
    candidate = forwarded_for.split(",", 1)[0].strip().removeprefix("[").removesuffix("]")
    try:
        return str(ipaddress.ip_address(candidate))
    except ValueError:
        return None


def _apply_browser_security_headers(response: Any) -> Any:
    """Apply browser security headers to an aiohttp response."""
    for name, value in _SECURITY_HEADERS.items():
        response.headers.setdefault(name, value)
    if response.content_type == "text/html":
        response.headers.setdefault("Cache-Control", "no-store")
    return response


def _request_matches_public_host(request: Any, public_url: str) -> bool:
    """Return true when the HTTP Host matches the configured public URL."""
    expected = _origin_tuple(public_url)
    if expected is None:
        return False
    actual = _authority_tuple(request.headers.get("Host"), scheme=expected[0])
    return actual == expected[1:]


def _request_matches_public_origin(request: Any, public_url: str, *, allow_referer: bool = True) -> bool:
    """Return true only when Origin or an allowed Referer matches the public URL."""
    expected = _origin_tuple(public_url)
    if expected is None:
        return False
    origin = request.headers.get("Origin")
    if origin:
        return _origin_tuple(origin) == expected
    if allow_referer:
        referer = request.headers.get("Referer")
        if referer:
            return _origin_tuple(referer) == expected
    return False


def _authority_tuple(authority: str | None, *, scheme: str) -> tuple[str, int] | None:
    """Return normalized ``(host, port)`` for an HTTP authority."""
    if not authority or any(separator in authority for separator in "/?#"):
        return None
    try:
        parsed = urlsplit(f"{scheme}://{authority}")
    except ValueError:
        return None
    if parsed.username is not None or parsed.password is not None:
        return None
    origin = _origin_tuple(parsed.geturl())
    if origin is None:
        return None
    return origin[1], origin[2]


def _origin_tuple(url: str) -> tuple[str, str, int] | None:
    """Return normalized ``(scheme, host, port)`` for *url*."""
    try:
        parsed = urlsplit(url)
    except ValueError:
        return None
    if not parsed.scheme or parsed.hostname is None:
        return None
    if parsed.username is not None or parsed.password is not None:
        return None
    host = _canonicalize_browser_host(parsed.hostname)
    if host is None:
        return None
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"}:
        return None
    try:
        port = parsed.port
    except ValueError:
        return None
    if port is None:
        port = 80 if scheme == "http" else 443
    return scheme, host, port


def _canonicalize_browser_host(host: str) -> str | None:
    """Return an ASCII/IP host matching browser URL serialization."""
    if "%" in host:
        return None
    try:
        return ipaddress.ip_address(host).compressed.lower()
    except ValueError:
        pass

    if any(ord(character) <= 0x20 or ord(character) == 0x7F for character in host):
        return None
    if any(character in "#/:<>?@[\\]^|" for character in host):
        return None

    if host.isascii() and not any(label.lower().startswith("xn--") for label in host.split(".")):
        ascii_host = host.lower()
    else:
        try:
            remapped_host = idna.uts46_remap(
                host,
                std3_rules=False,
                transitional=False,
            )
        except UnicodeError:
            return None
        ascii_labels: list[str] = []
        for label in remapped_host.split("."):
            if label.isascii() and not label.lower().startswith("xn--"):
                ascii_labels.append(label.lower())
                continue
            ascii_label = _browser_idna_label_to_ascii(label)
            if ascii_label is None:
                return None
            ascii_labels.append(ascii_label)
        ascii_host = ".".join(ascii_labels)

    try:
        return ipaddress.ip_address(ascii_host).compressed.lower()
    except ValueError:
        if _ends_in_ipv4_number(ascii_host):
            return None
    return ascii_host


def _browser_idna_label_to_ascii(label: str) -> str | None:
    """Encode one UTS #46 label while preserving browser-relaxed ASCII."""
    normalized_label = label.lower()
    is_ascii_label = normalized_label.startswith("xn--")
    if is_ascii_label:
        punycode_payload = normalized_label.removeprefix("xn--")
        if not punycode_payload:
            return None
        try:
            unicode_label = punycode_payload.encode("ascii").decode("punycode")
        except UnicodeError:
            return None
    else:
        unicode_label = label

    try:
        unicode_label = idna.uts46_remap(
            unicode_label,
            std3_rules=False,
            transitional=False,
        )
        idna.check_initial_combiner(unicode_label)
        idna.check_bidi(unicode_label)
        validation_label = "".join(
            character if not character.isascii() or character.isalnum() else "a" for character in unicode_label
        )
        idna.check_label(validation_label)
        ascii_label = f"xn--{unicode_label.encode('punycode').decode('ascii')}"
    except UnicodeError:
        return None
    if is_ascii_label and ascii_label != normalized_label:
        return None
    return ascii_label


def _ends_in_ipv4_number(host: str) -> bool:
    """Return true when the final domain label uses an IPv4 number syntax."""
    last_label = host.removesuffix(".").rsplit(".", 1)[-1].lower()
    if not last_label:
        return False
    if last_label.startswith("0x"):
        return all(character in "0123456789abcdef" for character in last_label[2:])
    return last_label.isascii() and last_label.isdigit()


def _warn_if_public_url_missing_for_wildcard_host(*, host: str, port: int, public_url: str | None) -> None:
    """Warn when textual-serve would publish an unusable browser URL."""
    if host not in _WILDCARD_HOSTS or public_url:
        return
    _write_warning(
        f"Warning: --host {host} binds on all interfaces, but browsers cannot use {host} as the "
        f"{APP_DISPLAY_NAME} serve URL. Pass --public-url http://<reachable-host>:{port} to avoid a blank page "
        f"(ex: --public-url http://localhost:{port})."
    )


def _warn_if_auth_uses_plain_http(*, host: str, port: int, public_url: str | None) -> None:
    """Warn when browser auth may send passwords over non-loopback HTTP."""
    if not _auth_transport_requires_insecure_override(host=host, port=port, public_url=public_url):
        return
    _write_warning(
        "Warning: browser auth is being served over HTTP for a non-loopback URL. Use HTTPS via a reverse "
        f"proxy before exposing {APP_DISPLAY_NAME} externally; passwords can be observed on the network."
    )


def _auth_transport_requires_insecure_override(*, host: str, port: int, public_url: str | None) -> bool:
    """Return true when auth would be reachable through non-loopback plain HTTP."""
    if not _is_loopback_host(host):
        return public_url is None or not _is_https_url(public_url)
    effective_url = _effective_public_url(host=host, port=port, public_url=public_url)
    parsed = urlsplit(effective_url)
    return parsed.scheme.lower() == "http" and not _is_loopback_host(parsed.hostname)


def _effective_public_url(*, host: str, port: int, public_url: str | None) -> str:
    """Return the configured public URL or Chrys' normalized default."""
    return public_url or _default_public_url(host=host, port=port)


def _browser_root_path(public_url: str) -> str:
    """Return the browser-facing root path for redirects."""
    base_path = _public_base_path(public_url)
    if not base_path:
        return "/"
    return f"{base_path}/"


def _browser_path(public_url: str, path: str) -> str:
    """Return *path* under the browser-facing public URL base path."""
    base_path = _public_base_path(public_url)
    normalized_path = path if path.startswith("/") else f"/{path}"
    return f"{base_path}{normalized_path}" if base_path else normalized_path


def _auth_cookie_path(public_url: str) -> str:
    """Return the browser cookie path for auth sessions."""
    return _public_base_path(public_url) or "/"


def _public_base_path(public_url: str) -> str:
    """Return the normalized path prefix from *public_url*."""
    origin = _origin_tuple(public_url)
    if origin is None:
        return ""
    path = urlsplit(public_url).path.strip("/")
    if not path:
        return ""
    return f"/{path}"


def _default_public_url(*, host: str, port: int) -> str:
    """Match textual-serve's default public URL construction."""
    url_host = _format_url_host(host)
    if port == 80:
        return f"http://{url_host}"
    if port == 443:
        return f"https://{url_host}"
    return f"http://{url_host}:{port}"


def _format_url_host(host: str) -> str:
    """Return *host* formatted for use in a URL authority; non-IP hosts are returned verbatim."""
    normalized = host.removeprefix("[").removesuffix("]")
    try:
        address = ipaddress.ip_address(normalized)
    except ValueError:
        return host
    if address.version == 6:
        return f"[{normalized}]"
    return normalized


def _is_loopback_host(hostname: str | None) -> bool:
    """Return true when *hostname* identifies this local host."""
    if hostname is None:
        return False
    normalized = hostname.lower().removeprefix("[").removesuffix("]")
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def _is_https_url(url: str) -> bool:
    """Return true when *url* uses HTTPS."""
    origin = _origin_tuple(url)
    return origin is not None and origin[0] == "https"


def _write_warning(message: str) -> None:
    """Write a terminal-styled warning without requiring ANSI in captured output."""
    _write_styled_stderr(message, style=_WARNING_STYLE)


def _write_error(message: str) -> None:
    """Write a terminal-styled error without requiring ANSI in captured output."""
    _write_styled_stderr(message, style=_ERROR_STYLE)


def _write_styled_stderr(message: str, *, style: str) -> None:
    """Write a styled stderr message."""
    _make_warning_console().print(message, style=style, markup=False, soft_wrap=True)


def _make_warning_console() -> Console:
    """Create the Rich console used for terminal warnings."""
    return Console(file=sys.stderr, highlight=False)


def main(argv: list[str] | None = None) -> int:
    """Entry point for ``chrys serve``."""
    from chrys.orchestration.startup import set_process_title

    set_process_title()

    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        run_command(args)
    except RuntimeError as exc:
        _write_error(f"Error: {exc}")
        parser.exit(1)
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
