# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for ``chrys serve``."""

from __future__ import annotations

import argparse
import asyncio
import errno
import inspect
import io
import json
import subprocess
import sys
from typing import Any, ClassVar

import pytest
from aiohttp import ClientPayloadError, web
from aiohttp.test_utils import TestClient, TestServer
from rich.console import Console

from chrys import __version__
from chrys.app.cli import serve as serve_cli
from chrys.foundation.branding import APP_DISPLAY_NAME


class FakeServer:
    """Minimal textual-serve stand-in for CLI tests."""

    instances: ClassVar[list[FakeServer]] = []

    def __init__(self, command: str, **kwargs: Any) -> None:
        self.command = command
        self.kwargs = kwargs
        self.debug: bool | None = None
        FakeServer.instances.append(self)

    def serve(self, *, debug: bool = False) -> None:
        self.debug = debug


class FakeAiohttpBaseServer:
    """Small aiohttp server stand-in for Chrys serve auth tests."""

    def __init__(
        self,
        command: str,
        *,
        host: str,
        port: int,
        title: str,
        public_url: str | None,
    ) -> None:
        self.command = command
        self.host = host
        self.port = port
        self.title = title
        self.public_url = public_url or f"http://{host}:{port}"
        self.console = Console(file=io.StringIO(), force_terminal=False, no_color=True)
        self.websocket_started = False

    async def _make_app(self) -> web.Application:
        app = web.Application()
        app.add_routes(
            [
                web.get("/", self.handle_index, name="index"),
                web.get("/ws", self.handle_websocket, name="websocket"),
                web.get("/download/{key}", self.handle_download, name="download"),
                web.get("/static/{tail:.*}", self.handle_static, name="static"),
            ]
        )
        return app

    async def handle_index(self, _request: web.Request) -> web.Response:
        return web.Response(text="TUI")

    async def handle_websocket(self, _request: web.Request) -> web.Response:
        self.websocket_started = True
        return web.Response(text="WS")

    async def handle_download(self, _request: web.Request) -> web.Response:
        return web.Response(text="DOWNLOAD")

    async def handle_static(self, _request: web.Request) -> web.Response:
        return web.Response(text="STATIC")


class FakeTextualServeHtmlBaseServer(FakeAiohttpBaseServer):
    async def handle_index(self, _request: web.Request) -> web.Response:
        return web.Response(
            text=(
                "<!doctype html><html><head><title>Chrys</title></head><body>"
                '<div class="intro"><svg></svg><div>Chrys</div><button id="start">Start</button></div>'
                '<div data-session-websocket-url="ws://localhost/ws"></div></body></html>'
            ),
            content_type="text/html",
        )


FakeTextualServeHtmlBaseServer.__module__ = "textual_serve.server"


def test_serve_parser_defaults() -> None:
    args = serve_cli.build_parser().parse_args([])

    assert args.host == "localhost"
    assert args.port == 7777
    assert args.public_url is None
    assert args.auth_required is False
    assert args.auth_password_file is None
    assert args.auth_password_env is None
    assert args.allow_insecure_auth is False
    assert args.auth_trust_forwarded_for is False
    assert args.debug is False


def test_serve_parser_accepts_auth_flags() -> None:
    args = serve_cli.build_parser().parse_args(["--auth", "--auth-password-file", "~/.chrys/serve-password"])

    assert args.auth_required is True
    assert args.auth_password_file == serve_cli.Path("~/.chrys/serve-password")


def test_serve_parser_accepts_auth_password_env() -> None:
    args = serve_cli.build_parser().parse_args(["--auth-required", "--auth-password-env", "CHRYS_SERVE_PASSWORD"])

    assert args.auth_required is True
    assert args.auth_password_env == "CHRYS_SERVE_PASSWORD"


def test_serve_parser_rejects_multiple_auth_password_sources() -> None:
    with pytest.raises(SystemExit):
        serve_cli.build_parser().parse_args(
            ["--auth-password-file", "~/.chrys/serve-password", "--auth-password-env", "CHRYS_SERVE_PASSWORD"]
        )


def test_serve_parser_accepts_auth_required_alias() -> None:
    args = serve_cli.build_parser().parse_args(["--auth-required"])

    assert args.auth_required is True


def test_serve_parser_accepts_auth_trust_forwarded_for() -> None:
    args = serve_cli.build_parser().parse_args(["--auth", "--auth-trust-forwarded-for"])

    assert args.auth_required is True
    assert args.auth_trust_forwarded_for is True


@pytest.mark.parametrize("value", ["0", "65536", "not-a-port"])
def test_serve_parser_rejects_invalid_ports(value: str) -> None:
    with pytest.raises(SystemExit):
        serve_cli.build_parser().parse_args(["--port", value])


def test_to_int_uses_default_for_invalid_values() -> None:
    assert serve_cli._to_int("42", 0) == 42
    assert serve_cli._to_int("bad", 7) == 7
    assert serve_cli._to_int(None, 9) == 9


def test_serve_help_uses_capitalized_help_text() -> None:
    help_text = serve_cli.build_parser().format_help()

    assert "Show this help message and exit" in help_text
    assert "show this help message and exit" not in help_text


def test_build_tui_command_uses_cli_module_with_python(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "executable", "/path with spaces/python")
    monkeypatch.delenv("PYAPP", raising=False)

    assert serve_cli.build_tui_command() == serve_cli._shell_join(
        ["/path with spaces/python", "-m", "chrys.app.cli.app", "__tui_subprocess__"]
    )


def test_build_tui_command_uses_pyapp_launcher(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PYAPP", "/Applications/Chrys/chrys")
    monkeypatch.setattr(sys, "executable", "/Applications/Chrys/chrys")
    monkeypatch.setattr(sys, "argv", ["/Applications/Chrys/chrys", "serve"])

    assert serve_cli.build_tui_command() == serve_cli._shell_join(["/Applications/Chrys/chrys", "__tui_subprocess__"])


def test_build_tui_command_falls_back_to_current_argv_for_launcher(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PYAPP", raising=False)
    monkeypatch.setattr(sys, "executable", "/Applications/Chrys/chrys")
    monkeypatch.setattr(sys, "argv", ["/Applications/Chrys/chrys", "serve"])

    assert serve_cli.build_tui_command() == serve_cli._shell_join(["/Applications/Chrys/chrys", "__tui_subprocess__"])


def test_shell_join_uses_windows_quoting(monkeypatch: pytest.MonkeyPatch) -> None:
    argv = [r"C:\Program Files\Chrys\chrys.exe", "__tui_subprocess__"]
    monkeypatch.setattr(serve_cli.os, "name", "nt")

    assert serve_cli._shell_join(argv) == subprocess.list2cmdline(argv)


def test_browser_clipboard_websocket_message_is_json_payload() -> None:
    message = serve_cli._browser_clipboard_websocket_message("copy me")

    assert json.loads(message) == [serve_cli.BROWSER_CLIPBOARD_META_TYPE, {"text": "copy me"}]


@pytest.mark.asyncio
async def test_browser_clipboard_meta_forwards_to_remote_writer() -> None:
    sent: list[str] = []

    async def write_str(message: str) -> None:
        sent.append(message)

    handled = await serve_cli._send_browser_clipboard_message(
        {"type": serve_cli.BROWSER_CLIPBOARD_META_TYPE, "text": "copy me"},
        write_str,
    )
    ignored = await serve_cli._send_browser_clipboard_message({"type": "other", "text": "no"}, write_str)

    assert handled is True
    assert ignored is False
    assert json.loads(sent[0]) == [serve_cli.BROWSER_CLIPBOARD_META_TYPE, {"text": "copy me"}]


@pytest.mark.asyncio
async def test_browser_clipboard_meta_logs_and_drops_malformed_text(caplog: pytest.LogCaptureFixture) -> None:
    sent: list[str] = []

    async def write_str(message: str) -> None:
        sent.append(message)

    with caplog.at_level("WARNING", logger="chrys.app.cli.serve"):
        handled = await serve_cli._send_browser_clipboard_message(
            {"type": serve_cli.BROWSER_CLIPBOARD_META_TYPE, "text": 123},
            write_str,
        )

    assert handled is True
    assert sent == []
    assert "Ignoring malformed browser clipboard metadata" in caplog.text


def test_browser_clipboard_support_script_wraps_websocket_messages() -> None:
    script = serve_cli._browser_clipboard_support_script()

    assert "data-chrys-browser-clipboard" in script
    assert "WebSocket.prototype.addEventListener" in script
    assert "navigator.clipboard.writeText" in script
    assert "writeClipboardAutomatically" in script
    assert "fallbackCopy(text)" in script
    assert "Copy is blocked" in script
    assert ".chrys-copy-card" in script
    assert ".chrys-copy-actions" in script
    assert 'button[data-primary="true"]' in script
    assert ".chrys-copy-status:empty" in script
    assert json.dumps(serve_cli.BROWSER_CLIPBOARD_META_TYPE) in script


def test_browser_capability_script_authenticates_before_terminal_messages() -> None:
    capability = "process-local-capability"

    script = serve_cli._browser_capability_script(capability)

    assert "data-chrys-serve-capability" in script
    assert "class ChrysAuthenticatedWebSocket extends NativeWebSocket" in script
    assert 'document.querySelector("[data-session-websocket-url]")' in script
    assert "actual.origin === expected.origin && actual.pathname === expected.pathname" in script
    assert 'this.addEventListener("open"' in script
    assert "this.send(JSON.stringify([MESSAGE_TYPE, CAPABILITY]))" in script
    assert json.dumps(serve_cli._BROWSER_CAPABILITY_MESSAGE_TYPE) in script
    assert json.dumps(capability) in script
    assert "searchParams" not in script


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (json.dumps([serve_cli._BROWSER_CAPABILITY_MESSAGE_TYPE, "secret"]), True),
        (json.dumps([serve_cli._BROWSER_CAPABILITY_MESSAGE_TYPE, "wrong"]), False),
        (json.dumps([serve_cli._BROWSER_CAPABILITY_MESSAGE_TYPE, "é"]), False),
        (json.dumps([serve_cli._BROWSER_CAPABILITY_MESSAGE_TYPE, "\ud800"]), False),
        (json.dumps(["resize", {"width": 80, "height": 24}]), False),
        ("not-json", False),
        (b"binary", False),
    ],
)
async def test_receive_browser_capability_requires_valid_first_packet(data: str | bytes, expected: bool) -> None:
    class FakeWebSocket:
        async def receive(self) -> argparse.Namespace:
            return argparse.Namespace(data=data)

    assert await serve_cli._receive_browser_capability(FakeWebSocket(), "secret") is expected


def test_browser_serve_branding_style_hides_textual_logo() -> None:
    style = serve_cli._browser_serve_branding_style()

    assert "data-chrys-serve-branding" in style
    assert ".intro > svg" in style
    assert "display: none !important" in style
    assert ".intro .chrys-serve-logo" in style
    assert "flex-direction: column" in style
    assert "white-space: pre" in style


def test_inject_browser_serve_branding_logo_preserves_logo_spacing() -> None:
    body = '<html><body><div class="intro"><svg></svg><div>Chrys</div></div></body></html>'

    branded = serve_cli._inject_browser_serve_branding_logo(body)

    assert f'<pre class="chrys-serve-logo" aria-label="{APP_DISPLAY_NAME}">' in branded
    assert "▀██████▀          █▄" in branded
    assert branded.count("chrys-serve-logo") == 1
    assert serve_cli._inject_browser_serve_branding_logo(branded) == branded


def test_textual_serve_clipboard_bridge_contract() -> None:
    from textual_serve.app_service import AppService
    from textual_serve.server import Server

    assert tuple(inspect.signature(AppService.on_meta).parameters) == ("self", "data")
    app_service_params = inspect.signature(AppService.__init__).parameters
    for name in ("command", "write_bytes", "write_str", "close", "download_manager", "debug"):
        assert name in app_service_params

    assert inspect.iscoroutinefunction(Server.handle_websocket)
    assert inspect.iscoroutinefunction(Server._process_messages)
    websocket_source = inspect.getsource(Server.handle_websocket)
    assert "AppService(" in websocket_source
    assert "_process_messages" in websocket_source


def test_run_command_starts_textual_serve(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeServer.instances.clear()
    monkeypatch.setattr(serve_cli, "_load_server_class", lambda: FakeServer)
    monkeypatch.setattr(serve_cli, "build_tui_command", lambda: "chrys __tui_subprocess__")

    args = argparse.Namespace(
        host="0.0.0.0",
        port=9000,
        public_url="https://chrys.example",
        auth_required=False,
        auth_password_file=None,
        auth_password_env=None,
        allow_insecure_auth=False,
        debug=True,
    )
    serve_cli.run_command(args)

    assert len(FakeServer.instances) == 1
    server = FakeServer.instances[0]
    assert server.command == "chrys __tui_subprocess__"
    assert server.kwargs == {
        "host": "0.0.0.0",
        "port": 9000,
        "title": APP_DISPLAY_NAME,
        "public_url": "https://chrys.example",
    }
    assert server.debug is True


def test_run_command_passes_normalized_public_url(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeServer.instances.clear()
    monkeypatch.setattr(serve_cli, "_load_server_class", lambda: FakeServer)
    monkeypatch.setattr(serve_cli, "build_tui_command", lambda: "chrys __tui_subprocess__")

    args = argparse.Namespace(
        host="0.0.0.0",
        port=9000,
        public_url="HTTPS://Chrys.Example/",
        auth_required=False,
        auth_password_file=None,
        auth_password_env=None,
        allow_insecure_auth=False,
        debug=False,
    )
    serve_cli.run_command(args)

    assert FakeServer.instances[0].kwargs["public_url"] == "https://chrys.example"


def test_run_command_passes_env_auth_config(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeServer.instances.clear()
    monkeypatch.setattr(serve_cli, "_load_server_class", lambda: FakeServer)
    monkeypatch.setattr(serve_cli, "build_tui_command", lambda: "chrys __tui_subprocess__")
    monkeypatch.setenv("CHRYS_SERVE_PASSWORD", "secret")

    args = argparse.Namespace(
        host="localhost",
        port=9000,
        public_url=None,
        auth_required=True,
        auth_password_file=None,
        auth_password_env="CHRYS_SERVE_PASSWORD",
        allow_insecure_auth=False,
        debug=False,
    )
    serve_cli.run_command(args)

    auth_config = FakeServer.instances[0].kwargs["auth_config"]
    assert auth_config.verifier.verify("secret") is True
    assert auth_config.verifier.verify("wrong") is False
    assert auth_config.trust_forwarded_for is False
    assert "CHRYS_SERVE_PASSWORD" not in serve_cli.os.environ


def test_run_command_passes_trusted_forwarded_for_auth_config(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeServer.instances.clear()
    monkeypatch.setattr(serve_cli, "_load_server_class", lambda: FakeServer)
    monkeypatch.setattr(serve_cli, "build_tui_command", lambda: "chrys __tui_subprocess__")
    monkeypatch.setenv("CHRYS_SERVE_PASSWORD", "secret")

    args = argparse.Namespace(
        host="localhost",
        port=9000,
        public_url=None,
        auth_required=True,
        auth_password_file=None,
        auth_password_env="CHRYS_SERVE_PASSWORD",
        allow_insecure_auth=False,
        auth_trust_forwarded_for=True,
        debug=False,
    )
    serve_cli.run_command(args)

    auth_config = FakeServer.instances[0].kwargs["auth_config"]
    assert auth_config.trust_forwarded_for is True


def test_resolve_auth_config_rejects_mismatched_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    prompts = iter(["one", "two"])
    monkeypatch.setattr(serve_cli.getpass, "getpass", lambda _prompt: next(prompts))
    args = argparse.Namespace(
        host="localhost",
        port=7777,
        public_url=None,
        auth_required=True,
        auth_password_file=None,
        auth_password_env=None,
        allow_insecure_auth=False,
    )

    with pytest.raises(RuntimeError, match="did not match"):
        serve_cli._resolve_auth_config(args)


def test_read_password_file_strips_line_endings(tmp_path) -> None:
    password_file = tmp_path / "serve-password"
    password_file.write_text("secret\r\n", encoding="utf-8")

    assert serve_cli._read_password_file(password_file) == "secret"


@pytest.mark.parametrize(
    ("hostname", "expected"),
    [
        ("localhost", True),
        ("127.0.0.1", True),
        ("::1", True),
        ("[::1]", True),
        ("0.0.0.0", False),
        ("example.com", False),
        (None, False),
    ],
)
def test_loopback_host_detection(hostname: str | None, expected: bool) -> None:
    assert serve_cli._is_loopback_host(hostname) is expected


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://chrys.example", True),
        ("HTTPS://chrys.example", True),
        ("http://chrys.example", False),
        ("https:foo", False),
        ("https://", False),
        ("https://chrys.example:bad", False),
    ],
)
def test_https_url_detection_is_case_insensitive(url: str, expected: bool) -> None:
    assert serve_cli._is_https_url(url) is expected


def test_origin_tuple_normalizes_default_ports() -> None:
    assert serve_cli._origin_tuple("https://Chrys.Example/path") == ("https", "chrys.example", 443)
    assert serve_cli._origin_tuple("http://localhost:7777/path") == ("http", "localhost", 7777)
    assert serve_cli._origin_tuple("https://bücher.example") == ("https", "xn--bcher-kva.example", 443)
    assert serve_cli._origin_tuple("https://foo_.bücher.example") == (
        "https",
        "foo_.xn--bcher-kva.example",
        443,
    )
    assert serve_cli._origin_tuple("https://foo_.xn--bcher-kva.example") == (
        "https",
        "foo_.xn--bcher-kva.example",
        443,
    )
    assert serve_cli._origin_tuple("https://faß.de") == ("https", "xn--fa-hia.de", 443)
    assert serve_cli._origin_tuple("http://[0:0:0:0:0:0:0:1]:7777") == ("http", "::1", 7777)


@pytest.mark.parametrize(
    ("host", "expected_host"),
    [
        ("foo_é.example", "xn--foo_-epa.example"),
        ("xn--foo_-epa.example", "xn--foo_-epa.example"),
        ("-é.example", "xn----bga.example"),
        ("xn----bga.example", "xn----bga.example"),
        ("é-.example", "xn----9fa.example"),
        ("xn----9fa.example", "xn----9fa.example"),
    ],
)
def test_origin_tuple_preserves_browser_relaxed_ascii_inside_idn_labels(
    host: str,
    expected_host: str,
) -> None:
    assert serve_cli._origin_tuple(f"https://{host}") == ("https", expected_host, 443)


@pytest.mark.parametrize(
    ("public_url", "expected"),
    [
        ("HTTPS://Chrys.Example/", "https://chrys.example"),
        ("https://chrys.example?", "https://chrys.example"),
        ("https://chrys.example#", "https://chrys.example"),
        ("https://bücher.example:443/chrys/", "https://xn--bcher-kva.example/chrys"),
        ("https://foo_.bücher.example/", "https://foo_.xn--bcher-kva.example"),
        ("https://foo_é.example/", "https://xn--foo_-epa.example"),
        ("https://xn--foo_-epa.example/", "https://xn--foo_-epa.example"),
        ("HTTP://[0:0:0:0:0:0:0:1]:7777/", "http://[::1]:7777"),
    ],
)
def test_public_url_normalization_matches_browser_serialization(public_url: str, expected: str) -> None:
    assert serve_cli._normalize_public_url(public_url) == expected


@pytest.mark.parametrize(
    ("authority", "public_url", "expected"),
    [
        ("localhost:7777", "http://localhost:7777", True),
        ("chrys.example", "https://chrys.example", True),
        ("[::1]:7777", "http://[::1]:7777", True),
        ("xn--bcher-kva.example", "https://bücher.example", True),
        ("foo_.xn--bcher-kva.example", "https://foo_.bücher.example", True),
        ("xn--foo_-epa.example", "https://foo_é.example", True),
        ("[::1]:7777", "http://[0:0:0:0:0:0:0:1]:7777", True),
        ("127.0.0.1:7777", "http://localhost:7777", False),
        ("attacker.example:7777", "http://localhost:7777", False),
        ("localhost:8888", "http://localhost:7777", False),
        ("user@localhost:7777", "http://localhost:7777", False),
        ("localhost:bad", "http://localhost:7777", False),
        ("", "http://localhost:7777", False),
    ],
)
def test_public_host_matching_rejects_dns_rebinding_authorities(
    authority: str,
    public_url: str,
    expected: bool,
) -> None:
    request = argparse.Namespace(headers={"Host": authority})

    assert serve_cli._request_matches_public_host(request, public_url) is expected


@pytest.mark.parametrize(
    ("headers", "allow_referer", "expected"),
    [
        ({"Origin": "http://localhost:7777"}, False, True),
        ({"Origin": "https://evil.example"}, False, False),
        ({"Origin": "null"}, False, False),
        ({}, False, False),
        ({"Referer": "http://localhost:7777/page"}, False, False),
        ({"Referer": "http://localhost:7777/page"}, True, True),
    ],
)
def test_public_origin_matching_is_fail_closed(
    headers: dict[str, str],
    allow_referer: bool,
    expected: bool,
) -> None:
    request = argparse.Namespace(headers=headers)

    assert (
        serve_cli._request_matches_public_origin(
            request,
            "http://localhost:7777",
            allow_referer=allow_referer,
        )
        is expected
    )


@pytest.mark.parametrize(
    ("origin", "public_url"),
    [
        ("https://xn--bcher-kva.example", "https://bücher.example"),
        ("http://[::1]:7777", "http://[0:0:0:0:0:0:0:1]:7777"),
    ],
)
def test_public_origin_matching_uses_browser_host_canonicalization(origin: str, public_url: str) -> None:
    request = argparse.Namespace(headers={"Origin": origin})

    assert serve_cli._request_matches_public_origin(request, public_url, allow_referer=False) is True


@pytest.mark.parametrize(
    "url",
    [
        "https:foo",
        "https://",
        "http://user@localhost:7777",
        "http://evil.example:bad",
        "http://evil.example:99999",
        "http://[::1",
        "http://[not-ip]/",
        "http://127.1:7777",
        "http://1.2.0x:7777",
        "https://\u200d.example",
        "https://xn--",
    ],
)
def test_origin_tuple_rejects_malformed_urls(url: str) -> None:
    assert serve_cli._origin_tuple(url) is None


def test_default_public_url_brackets_ipv6_literals() -> None:
    assert serve_cli._default_public_url(host="::1", port=7777) == "http://[::1]:7777"
    assert serve_cli._default_public_url(host="::1", port=443) == "https://[::1]"


def test_login_rate_limiter_locks_for_process_lifetime() -> None:
    limiter = serve_cli._LoginRateLimiter(max_failures=2)

    assert limiter.record_failure("client") is False
    assert limiter.record_failure("client") is True
    assert limiter.is_locked("client") is True

    limiter.record_success("client")

    assert limiter.is_locked("client") is True
    assert limiter.record_failure("client") is True


@pytest.mark.parametrize("host", ["0.0.0.0", "::"])
def test_run_command_warns_for_wildcard_host_without_public_url(
    host: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    FakeServer.instances.clear()
    monkeypatch.setattr(serve_cli, "_load_server_class", lambda: FakeServer)
    monkeypatch.setattr(serve_cli, "build_tui_command", lambda: "chrys __tui_subprocess__")

    args = argparse.Namespace(
        host=host,
        port=9000,
        public_url=None,
        auth_required=False,
        auth_password_file=None,
        auth_password_env=None,
        allow_insecure_auth=False,
        debug=False,
    )
    serve_cli.run_command(args)

    out = capsys.readouterr()
    assert out.err == (
        f"Warning: --host {host} binds on all interfaces, but browsers cannot use {host} as the "
        f"{APP_DISPLAY_NAME} serve URL. Pass --public-url http://<reachable-host>:9000 to avoid a blank page "
        "(ex: --public-url http://localhost:9000).\n"
    )
    assert len(FakeServer.instances) == 1


def test_run_command_rejects_auth_over_non_loopback_http_by_default(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    FakeServer.instances.clear()
    monkeypatch.setattr(serve_cli, "_load_server_class", lambda: FakeServer)
    monkeypatch.setattr(serve_cli, "build_tui_command", lambda: "chrys __tui_subprocess__")
    monkeypatch.setenv("CHRYS_SERVE_PASSWORD", "secret")

    args = argparse.Namespace(
        host="0.0.0.0",
        port=9000,
        public_url="http://chrys.example",
        auth_required=True,
        auth_password_file=None,
        auth_password_env="CHRYS_SERVE_PASSWORD",
        allow_insecure_auth=False,
        debug=False,
    )

    with pytest.raises(RuntimeError, match="browser auth over non-loopback HTTP is unsafe"):
        serve_cli.run_command(args)

    out = capsys.readouterr()
    assert out.err == ""
    assert not FakeServer.instances


def test_run_command_rejects_wildcard_auth_without_public_url(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    FakeServer.instances.clear()
    monkeypatch.setattr(serve_cli, "_load_server_class", lambda: FakeServer)
    monkeypatch.setattr(serve_cli, "build_tui_command", lambda: "chrys __tui_subprocess__")
    monkeypatch.setenv("CHRYS_SERVE_PASSWORD", "secret")

    args = argparse.Namespace(
        host="0.0.0.0",
        port=9000,
        public_url=None,
        auth_required=True,
        auth_password_file=None,
        auth_password_env="CHRYS_SERVE_PASSWORD",
        allow_insecure_auth=False,
        debug=False,
    )

    with pytest.raises(RuntimeError, match="browser auth over non-loopback HTTP is unsafe"):
        serve_cli.run_command(args)

    out = capsys.readouterr()
    assert out.err == ""
    assert not FakeServer.instances


def test_run_command_rejects_wildcard_auth_without_explicit_https_public_url_on_port_443(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    FakeServer.instances.clear()
    monkeypatch.setattr(serve_cli, "_load_server_class", lambda: FakeServer)
    monkeypatch.setattr(serve_cli, "build_tui_command", lambda: "chrys __tui_subprocess__")
    monkeypatch.setenv("CHRYS_SERVE_PASSWORD", "secret")

    args = argparse.Namespace(
        host="0.0.0.0",
        port=443,
        public_url=None,
        auth_required=True,
        auth_password_file=None,
        auth_password_env="CHRYS_SERVE_PASSWORD",
        allow_insecure_auth=False,
        debug=False,
    )

    with pytest.raises(RuntimeError, match="browser auth over non-loopback HTTP is unsafe"):
        serve_cli.run_command(args)

    out = capsys.readouterr()
    assert out.err == ""
    assert not FakeServer.instances


def test_run_command_rejects_wildcard_auth_with_http_loopback_public_url(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    FakeServer.instances.clear()
    monkeypatch.setattr(serve_cli, "_load_server_class", lambda: FakeServer)
    monkeypatch.setattr(serve_cli, "build_tui_command", lambda: "chrys __tui_subprocess__")
    monkeypatch.setenv("CHRYS_SERVE_PASSWORD", "secret")

    args = argparse.Namespace(
        host="0.0.0.0",
        port=9000,
        public_url="http://localhost:9000",
        auth_required=True,
        auth_password_file=None,
        auth_password_env="CHRYS_SERVE_PASSWORD",
        allow_insecure_auth=False,
        debug=False,
    )

    with pytest.raises(RuntimeError, match="browser auth over non-loopback HTTP is unsafe"):
        serve_cli.run_command(args)

    out = capsys.readouterr()
    assert out.err == ""
    assert not FakeServer.instances


@pytest.mark.parametrize("public_url", ["https:foo", "https://", "https://chrys.example:bad", "https://[not-ip]/"])
def test_run_command_rejects_auth_with_malformed_public_url(
    public_url: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    FakeServer.instances.clear()
    monkeypatch.setattr(serve_cli, "_load_server_class", lambda: FakeServer)
    monkeypatch.setattr(serve_cli, "build_tui_command", lambda: "chrys __tui_subprocess__")
    monkeypatch.setenv("CHRYS_SERVE_PASSWORD", "secret")

    args = argparse.Namespace(
        host="0.0.0.0",
        port=9000,
        public_url=public_url,
        auth_required=True,
        auth_password_file=None,
        auth_password_env="CHRYS_SERVE_PASSWORD",
        allow_insecure_auth=False,
        debug=False,
    )

    with pytest.raises(RuntimeError, match="--public-url must be an absolute"):
        serve_cli.run_command(args)

    out = capsys.readouterr()
    assert out.err == ""
    assert not FakeServer.instances


@pytest.mark.parametrize(
    "public_url",
    [
        "localhost:7777",
        "https://user@chrys.example",
        "https://chrys.example?query",
        "https://chrys.example#fragment",
        "http://127.1:7777",
        "http://1.2.0x:7777",
        "https://chrys.example/chrys/../other",
        "https://chrys.example/a\\b",
        "https://chrys.example/%2e%2e/other",
    ],
)
def test_run_command_rejects_malformed_public_url_without_auth(
    public_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    FakeServer.instances.clear()
    monkeypatch.setattr(serve_cli, "_load_server_class", lambda: FakeServer)
    monkeypatch.setattr(serve_cli, "build_tui_command", lambda: "chrys __tui_subprocess__")
    args = argparse.Namespace(
        host="localhost",
        port=7777,
        public_url=public_url,
        auth_required=False,
        auth_password_file=None,
        auth_password_env=None,
        allow_insecure_auth=False,
        debug=False,
    )

    with pytest.raises(RuntimeError, match="--public-url must be an absolute"):
        serve_cli.run_command(args)

    assert not FakeServer.instances


def test_run_command_allows_wildcard_auth_with_https_public_url(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeServer.instances.clear()
    monkeypatch.setattr(serve_cli, "_load_server_class", lambda: FakeServer)
    monkeypatch.setattr(serve_cli, "build_tui_command", lambda: "chrys __tui_subprocess__")
    monkeypatch.setenv("CHRYS_SERVE_PASSWORD", "secret")

    args = argparse.Namespace(
        host="0.0.0.0",
        port=9000,
        public_url="https://chrys.example",
        auth_required=True,
        auth_password_file=None,
        auth_password_env="CHRYS_SERVE_PASSWORD",
        allow_insecure_auth=False,
        debug=False,
    )
    serve_cli.run_command(args)

    assert len(FakeServer.instances) == 1
    assert FakeServer.instances[0].kwargs["public_url"] == "https://chrys.example"
    assert "auth_config" in FakeServer.instances[0].kwargs


def test_run_command_allows_auth_on_ipv6_loopback_default_url(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeServer.instances.clear()
    monkeypatch.setattr(serve_cli, "_load_server_class", lambda: FakeServer)
    monkeypatch.setattr(serve_cli, "build_tui_command", lambda: "chrys __tui_subprocess__")
    monkeypatch.setenv("CHRYS_SERVE_PASSWORD", "secret")

    args = argparse.Namespace(
        host="::1",
        port=9000,
        public_url=None,
        auth_required=True,
        auth_password_file=None,
        auth_password_env="CHRYS_SERVE_PASSWORD",
        allow_insecure_auth=False,
        debug=False,
    )
    serve_cli.run_command(args)

    assert len(FakeServer.instances) == 1
    assert FakeServer.instances[0].kwargs["host"] == "::1"
    assert FakeServer.instances[0].kwargs["public_url"] == "http://[::1]:9000"
    assert "auth_config" in FakeServer.instances[0].kwargs


def test_run_command_allows_explicit_insecure_http_auth(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    FakeServer.instances.clear()
    monkeypatch.setattr(serve_cli, "_load_server_class", lambda: FakeServer)
    monkeypatch.setattr(serve_cli, "build_tui_command", lambda: "chrys __tui_subprocess__")
    monkeypatch.setenv("CHRYS_SERVE_PASSWORD", "secret")

    args = argparse.Namespace(
        host="0.0.0.0",
        port=9000,
        public_url="http://chrys.example",
        auth_required=True,
        auth_password_file=None,
        auth_password_env="CHRYS_SERVE_PASSWORD",
        allow_insecure_auth=True,
        debug=False,
    )
    serve_cli.run_command(args)

    out = capsys.readouterr()
    assert out.err == (
        "Warning: browser auth is being served over HTTP for a non-loopback URL. Use HTTPS via a reverse "
        f"proxy before exposing {APP_DISPLAY_NAME} externally; passwords can be observed on the network.\n"
    )
    assert len(FakeServer.instances) == 1


def test_run_command_warns_for_explicit_insecure_wildcard_auth_with_http_loopback_public_url(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    FakeServer.instances.clear()
    monkeypatch.setattr(serve_cli, "_load_server_class", lambda: FakeServer)
    monkeypatch.setattr(serve_cli, "build_tui_command", lambda: "chrys __tui_subprocess__")
    monkeypatch.setenv("CHRYS_SERVE_PASSWORD", "secret")

    args = argparse.Namespace(
        host="0.0.0.0",
        port=9000,
        public_url="http://localhost:9000",
        auth_required=True,
        auth_password_file=None,
        auth_password_env="CHRYS_SERVE_PASSWORD",
        allow_insecure_auth=True,
        debug=False,
    )
    serve_cli.run_command(args)

    out = capsys.readouterr()
    assert out.err == (
        "Warning: browser auth is being served over HTTP for a non-loopback URL. Use HTTPS via a reverse "
        f"proxy before exposing {APP_DISPLAY_NAME} externally; passwords can be observed on the network.\n"
    )
    assert len(FakeServer.instances) == 1


def test_write_warning_styles_terminal_output_yellow(monkeypatch: pytest.MonkeyPatch) -> None:
    output = io.StringIO()

    def console_factory() -> Console:
        return Console(
            file=output,
            highlight=False,
            force_terminal=True,
            color_system="standard",
            no_color=False,
            width=120,
        )

    monkeypatch.setattr(serve_cli, "_make_warning_console", console_factory)

    serve_cli._write_warning("Warning: colored")

    text = output.getvalue()
    assert "\x1b[33m" in text
    assert text.endswith("\x1b[0m\n")
    assert "Warning: colored" in text


def test_main_styles_runtime_error_red(monkeypatch: pytest.MonkeyPatch) -> None:
    output = io.StringIO()

    def console_factory() -> Console:
        return Console(
            file=output,
            highlight=False,
            force_terminal=True,
            color_system="standard",
            no_color=False,
            width=120,
        )

    def raise_runtime_error(_args: argparse.Namespace) -> None:
        msg = (
            "browser auth over non-loopback HTTP is unsafe because passwords can be observed on the network. "
            "Use an HTTPS --public-url through a reverse proxy, bind to localhost, or pass --allow-insecure-auth "
            "only on a trusted network or tunnel."
        )
        raise RuntimeError(msg)

    monkeypatch.setattr(serve_cli, "_make_warning_console", console_factory)
    monkeypatch.setattr(serve_cli, "run_command", raise_runtime_error)

    with pytest.raises(SystemExit) as exc_info:
        serve_cli.main([])

    text = output.getvalue()
    assert exc_info.value.code == 1
    assert "\x1b[31m" in text
    assert text.endswith("\x1b[0m\n")
    assert "Error: browser auth over non-loopback HTTP is unsafe" in text


def test_main_reports_a_listen_address_the_os_refuses(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A busy or privileged port is a typed error, not an uncaught traceback."""

    class BusyServer(FakeServer):
        def serve(self, *, debug: bool = False) -> None:
            raise OSError(errno.EADDRINUSE, "Address already in use")

    monkeypatch.setattr(serve_cli, "_load_server_class", lambda: BusyServer)

    with pytest.raises(SystemExit) as exc_info:
        serve_cli.main(["--port", "7789"])

    assert exc_info.value.code == 1
    assert "Error: cannot listen on localhost:7789: Address already in use" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_serve_rejects_unrecognized_host_before_serving_content() -> None:
    server_class = serve_cli._make_chrys_server_class(FakeAiohttpBaseServer)
    server = server_class(
        "chrys __tui_subprocess__",
        host="localhost",
        port=7777,
        title=APP_DISPLAY_NAME,
        public_url="http://localhost:7777",
    )
    client = TestClient(
        TestServer(await server._make_app()),
        headers={"Host": "attacker.example:7777", "Origin": "http://attacker.example:7777"},
    )
    await client.start_server()
    try:
        response = await client.get("/")

        assert response.status == 421
        assert await response.text() == "Invalid Host header"
        assert response.headers["Content-Security-Policy"].startswith("frame-ancestors 'none'")
        assert response.headers["X-Frame-Options"] == "DENY"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_serve_hardens_and_logs_unexpected_500_responses(caplog: pytest.LogCaptureFixture) -> None:
    class FailingBaseServer(FakeAiohttpBaseServer):
        async def handle_index(self, _request: web.Request) -> web.Response:
            msg = "unexpected handler failure"
            raise RuntimeError(msg)

    server_class = serve_cli._make_chrys_server_class(FailingBaseServer)
    server = server_class(
        "chrys __tui_subprocess__",
        host="localhost",
        port=7777,
        title=APP_DISPLAY_NAME,
        public_url="http://localhost:7777",
    )
    client = TestClient(
        TestServer(await server._make_app()),
        headers={"Host": "localhost:7777", "Origin": "http://localhost:7777"},
    )
    await client.start_server()
    try:
        with caplog.at_level("ERROR", logger="chrys.app.cli.serve"):
            response = await client.get("/")

        assert response.status == 500
        assert await response.text() == "Internal Server Error"
        for name, value in serve_cli._SECURITY_HEADERS.items():
            assert response.headers[name] == value
        assert "Unhandled browser HTTP request" in caplog.text
        assert "unexpected handler failure" in caplog.text
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_websocket_rejects_cross_origin_and_missing_origin() -> None:
    server_class = serve_cli._make_chrys_server_class(FakeAiohttpBaseServer)
    server = server_class(
        "chrys __tui_subprocess__",
        host="localhost",
        port=7777,
        title=APP_DISPLAY_NAME,
        public_url="http://localhost:7777",
    )
    client = TestClient(
        TestServer(await server._make_app()),
        headers={"Host": "localhost:7777"},
    )
    await client.start_server()
    try:
        cross_origin = await client.get("/ws", headers={"Origin": "https://evil.example"})
        assert cross_origin.status == 403
        assert server.websocket_started is False

        missing_origin = await client.get("/ws")
        assert missing_origin.status == 403
        assert server.websocket_started is False

        same_origin = await client.get("/ws", headers={"Origin": "http://localhost:7777"})
        assert same_origin.status == 200
        assert server.websocket_started is True
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_auth_required_protects_tui_and_websocket_until_login() -> None:
    server_class = serve_cli._make_chrys_server_class(FakeAiohttpBaseServer)
    server = server_class(
        "chrys __tui_subprocess__",
        host="localhost",
        port=7777,
        title=APP_DISPLAY_NAME,
        public_url="https://chrys.example",
        auth_config=serve_cli.ServeAuthConfig(
            verifier=serve_cli.PasswordVerifier.from_password("secret"),
        ),
    )
    client = TestClient(
        TestServer(await server._make_app()),
        headers={"Host": "chrys.example", "Origin": "https://chrys.example"},
    )
    await client.start_server()
    try:
        login_response = await client.get("/")
        login_text = await login_response.text()
        assert login_response.status == 200
        assert login_response.headers["Referrer-Policy"] == "same-origin"
        assert 'class="logo"' in login_text
        assert "▀██████▀" in login_text
        assert "overflow: hidden" in login_text
        assert "overflow-x: auto" not in login_text
        assert "10px/1.05" in login_text
        assert "<h1>Chrys</h1>" not in login_text
        assert "Password" in login_text
        assert "TUI" not in login_text
        csrf_token = _csrf_token(login_text)

        websocket_response = await client.get("/ws")
        assert websocket_response.status == 401
        assert server.websocket_started is False

        download_response = await client.get("/download/file")
        assert download_response.status == 401

        bad_login = await client.post(
            _auth_login_url(), data={serve_cli._AUTH_CSRF_FIELD: csrf_token, "password": "wrong"}
        )
        bad_login_text = await bad_login.text()
        assert bad_login.status == 401
        assert "Invalid password." in bad_login_text
        csrf_token = _csrf_token(bad_login_text)

        good_login = await client.post(
            _auth_login_url(),
            data={serve_cli._AUTH_CSRF_FIELD: csrf_token, "password": "secret"},
            allow_redirects=False,
        )
        assert good_login.status == 302
        cookie = good_login.cookies[serve_cli._AUTH_COOKIE_NAME]
        cookie_header = f"{serve_cli._AUTH_COOKIE_NAME}={cookie.value}"
        set_cookie = good_login.headers["Set-Cookie"]
        assert "HttpOnly" in set_cookie
        assert "SameSite=Strict" in set_cookie
        assert "Secure" in set_cookie

        tui_response = await client.get("/", headers={"Cookie": cookie_header})
        assert tui_response.status == 200
        assert await tui_response.text() == "TUI"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_textual_serve_page_injects_browser_clipboard_support() -> None:
    server_class = serve_cli._make_chrys_server_class(FakeTextualServeHtmlBaseServer)
    server = server_class(
        "chrys __tui_subprocess__",
        host="localhost",
        port=7777,
        title="Chrys",
        public_url="http://localhost:7777",
    )
    client = TestClient(
        TestServer(await server._make_app()),
        headers={"Host": "localhost:7777", "Origin": "http://localhost:7777"},
    )
    await client.start_server()
    try:
        response = await client.get("/")
        text = await response.text()

        assert response.status == 200
        assert "data-session-websocket-url" in text
        assert "data-chrys-browser-clipboard" in text
        assert "data-chrys-serve-capability" in text
        assert "data-chrys-serve-branding" in text
        assert ".intro > svg" in text
        assert "▀██████▀" in text
        assert "navigator.clipboard.writeText" in text
        assert server._browser_capability in text
        assert response.headers["Cache-Control"] == "no-store"
        assert response.headers["Content-Security-Policy"].startswith("frame-ancestors 'none'")
        assert response.headers["Cross-Origin-Opener-Policy"] == "same-origin"
        assert response.headers["Cross-Origin-Resource-Policy"] == "same-origin"
        assert response.headers["Referrer-Policy"] == "same-origin"
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert response.headers["X-Frame-Options"] == "DENY"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_real_textual_serve_page_injects_browser_clipboard_support() -> None:
    server_class = serve_cli._load_server_class()
    server = server_class(
        "chrys __tui_subprocess__",
        host="localhost",
        port=7777,
        title="Chrys",
        public_url="http://localhost:7777",
    )
    server.console = Console(file=io.StringIO(), force_terminal=False, no_color=True)
    client = TestClient(
        TestServer(await server._make_app()),
        headers={"Host": "localhost:7777", "Origin": "http://localhost:7777"},
    )
    await client.start_server()
    try:
        response = await client.get("/")
        text = await response.text()

        assert response.status == 200
        assert "data-session-websocket-url" in text
        assert "data-chrys-browser-clipboard" in text
        assert "data-chrys-serve-capability" in text
        assert "data-chrys-serve-branding" in text
        assert "▀██████▀" in text
        assert "WebSocket.prototype.addEventListener" in text
        assert server._browser_capability in text
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_real_textual_serve_streamed_html_download_has_security_headers() -> None:

    class StreamingDownloadManager:
        async def get_download_metadata(self, key: str) -> argparse.Namespace:
            assert key == "page"
            return argparse.Namespace(
                mime_type="text/html",
                encoding="utf-8",
                open_method="browser",
                file_name="page.html",
            )

        async def download(self, key: str) -> Any:
            assert key == "page"
            yield b"<html>download</html>"

    server_class = serve_cli._load_server_class()
    server = server_class(
        "chrys __tui_subprocess__",
        host="localhost",
        port=7777,
        title="Chrys",
        public_url="http://localhost:7777",
    )
    server.console = Console(file=io.StringIO(), force_terminal=False, no_color=True)
    server.download_manager = StreamingDownloadManager()
    client = TestClient(
        TestServer(await server._make_app()),
        headers={"Host": "localhost:7777"},
    )
    await client.start_server()
    try:
        response = await client.get("/download/page")

        assert response.status == 200
        assert await response.text() == "<html>download</html>"
        for name, value in serve_cli._SECURITY_HEADERS.items():
            assert response.headers[name] == value
        assert response.headers["Cache-Control"] == "no-store"
    finally:
        await client.close()


@pytest.mark.parametrize("raise_http_exception", [False, True])
@pytest.mark.asyncio
async def test_real_textual_serve_stream_failure_does_not_append_a_second_response(
    raise_http_exception: bool,
) -> None:

    class FailingStreamingDownloadManager:
        async def get_download_metadata(self, key: str) -> argparse.Namespace:
            assert key == "page"
            return argparse.Namespace(
                mime_type="text/plain",
                encoding="utf-8",
                open_method="download",
                file_name="page.txt",
            )

        async def download(self, key: str) -> Any:
            assert key == "page"
            yield b"partial"
            if raise_http_exception:
                raise web.HTTPInternalServerError(text="stream failed")
            msg = "stream failed"
            raise RuntimeError(msg)

    server_class = serve_cli._load_server_class()
    server = server_class(
        "chrys __tui_subprocess__",
        host="localhost",
        port=7777,
        title="Chrys",
        public_url="http://localhost:7777",
    )
    server.console = Console(file=io.StringIO(), force_terminal=False, no_color=True)
    server.download_manager = FailingStreamingDownloadManager()
    client = TestClient(
        TestServer(await server._make_app()),
        headers={"Host": "localhost:7777"},
    )
    await client.start_server()
    try:
        response = await client.get("/download/page")

        assert response.status == 200
        with pytest.raises(ClientPayloadError, match="payload is not completed"):
            await response.read()
    finally:
        await client.close()


@pytest.mark.parametrize(
    ("public_url", "expected_websocket_url"),
    [
        ("https://chrys.example/", "wss://chrys.example/ws"),
        ("https://chrys.example?", "wss://chrys.example/ws"),
        ("https://chrys.example#", "wss://chrys.example/ws"),
        ("HTTPS://chrys.example", "wss://chrys.example/ws"),
        ("https://chrys.example/chrys/", "wss://chrys.example/chrys/ws"),
    ],
)
@pytest.mark.asyncio
async def test_real_textual_serve_uses_normalized_public_url(
    public_url: str,
    expected_websocket_url: str,
) -> None:
    server_class = serve_cli._load_server_class()
    server = server_class(
        "chrys __tui_subprocess__",
        host="localhost",
        port=7777,
        title="Chrys",
        public_url=public_url,
    )
    server.console = Console(file=io.StringIO(), force_terminal=False, no_color=True)
    client = TestClient(
        TestServer(await server._make_app()),
        headers={"Host": "chrys.example", "Origin": "https://chrys.example"},
    )
    await client.start_server()
    try:
        response = await client.get("/")
        text = await response.text()

        assert response.status == 200
        assert f'data-session-websocket-url="{expected_websocket_url}"' in text
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_real_textual_serve_rejects_unicode_capability_without_logging_exception(
    caplog: pytest.LogCaptureFixture,
) -> None:
    server_class = serve_cli._load_server_class()
    server = server_class(
        "chrys __tui_subprocess__",
        host="localhost",
        port=7777,
        title="Chrys",
        public_url="http://localhost:7777",
    )
    server.console = Console(file=io.StringIO(), force_terminal=False, no_color=True)
    client = TestClient(
        TestServer(await server._make_app()),
        headers={"Host": "localhost:7777", "Origin": "http://localhost:7777"},
    )
    await client.start_server()
    try:
        with caplog.at_level("ERROR", logger="chrys.app.cli.serve"):
            websocket = await client.ws_connect("/ws")
            await websocket.send_json([serve_cli._BROWSER_CAPABILITY_MESSAGE_TYPE, "é"])
            close_message = await websocket.receive()

        assert close_message.type is web.WSMsgType.CLOSE
        assert close_message.data == 1008
        assert "Browser websocket session failed" not in caplog.text
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_real_textual_serve_starts_app_after_valid_capability(monkeypatch: pytest.MonkeyPatch) -> None:
    from textual_serve import app_service

    started = asyncio.Event()
    starts: list[tuple[int, int]] = []

    class FakeAppService:
        def __init__(self, _command: str, **_kwargs: Any) -> None:
            pass

        async def start(self, width: int, height: int) -> None:
            starts.append((width, height))
            started.set()

        async def stop(self) -> None:
            pass

    monkeypatch.setattr(app_service, "AppService", FakeAppService)
    server_class = serve_cli._load_server_class()
    server = server_class(
        "chrys __tui_subprocess__",
        host="localhost",
        port=7777,
        title="Chrys",
        public_url="http://localhost:7777",
    )
    server.console = Console(file=io.StringIO(), force_terminal=False, no_color=True)
    client = TestClient(
        TestServer(await server._make_app()),
        headers={"Host": "localhost:7777", "Origin": "http://localhost:7777"},
    )
    await client.start_server()
    try:
        websocket = await client.ws_connect("/ws?width=100&height=40")
        await websocket.send_json(
            [serve_cli._BROWSER_CAPABILITY_MESSAGE_TYPE, server._browser_capability],
        )
        await asyncio.wait_for(started.wait(), timeout=1)

        assert starts == [(100, 40)]
        await websocket.close()
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_auth_login_uses_public_url_path_prefix() -> None:
    server_class = serve_cli._make_chrys_server_class(FakeAiohttpBaseServer)
    server = server_class(
        "chrys __tui_subprocess__",
        host="localhost",
        port=7777,
        title="Chrys",
        public_url="https://chrys.example/chrys",
        auth_config=serve_cli.ServeAuthConfig(
            verifier=serve_cli.PasswordVerifier.from_password("secret"),
        ),
    )
    client = TestClient(
        TestServer(await server._make_app()),
        headers={"Host": "chrys.example", "Origin": "https://chrys.example"},
    )
    await client.start_server()
    try:
        login_response = await client.get("/")
        login_text = await login_response.text()
        csrf_token = _csrf_token(login_text)
        assert 'action="/chrys/auth/login"' in login_text

        good_login = await client.post(
            _auth_login_url(),
            data={serve_cli._AUTH_CSRF_FIELD: csrf_token, "password": "secret"},
            allow_redirects=False,
        )
        assert good_login.status == 302
        assert good_login.headers["Location"] == "/chrys/"
        assert "Path=/chrys" in good_login.headers["Set-Cookie"]

        cookie = good_login.cookies[serve_cli._AUTH_COOKIE_NAME]
        cookie_header = f"{serve_cli._AUTH_COOKIE_NAME}={cookie.value}"
        logout = await client.get("/auth/logout", headers={"Cookie": cookie_header}, allow_redirects=False)

        assert logout.status == 302
        assert logout.headers["Location"] == "/chrys/auth/login"
        assert "Path=/chrys" in logout.headers["Set-Cookie"]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_auth_lockout_rejects_correct_password_until_restart() -> None:
    server_class = serve_cli._make_chrys_server_class(FakeAiohttpBaseServer)
    server = server_class(
        "chrys __tui_subprocess__",
        host="localhost",
        port=7777,
        title="Chrys",
        public_url="https://chrys.example",
        auth_config=serve_cli.ServeAuthConfig(
            verifier=serve_cli.PasswordVerifier.from_password("secret"),
        ),
    )
    client = TestClient(
        TestServer(await server._make_app()),
        headers={"Host": "chrys.example", "Origin": "https://chrys.example"},
    )
    await client.start_server()
    try:
        for _ in range(serve_cli._AUTH_MAX_FAILURES - 1):
            login_response = await client.get("/")
            csrf_token = _csrf_token(await login_response.text())
            response = await client.post(
                _auth_login_url(),
                data={serve_cli._AUTH_CSRF_FIELD: csrf_token, "password": "wrong"},
            )
            assert response.status == 401

        login_response = await client.get("/")
        csrf_token = _csrf_token(await login_response.text())
        lockout_response = await client.post(
            _auth_login_url(),
            data={serve_cli._AUTH_CSRF_FIELD: csrf_token, "password": "wrong"},
        )
        lockout_text = await lockout_response.text()
        assert lockout_response.status == 429
        assert serve_cli._AUTH_LOCKOUT_MESSAGE in lockout_text

        csrf_token = _csrf_token(lockout_text)
        correct_response = await client.post(
            _auth_login_url(),
            data={serve_cli._AUTH_CSRF_FIELD: csrf_token, "password": "secret"},
            allow_redirects=False,
        )
        correct_text = await correct_response.text()
        assert correct_response.status == 429
        assert serve_cli._AUTH_COOKIE_NAME not in correct_response.cookies
        assert serve_cli._AUTH_LOCKOUT_MESSAGE in correct_text
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_auth_lockout_can_key_by_trusted_forwarded_for() -> None:
    server_class = serve_cli._make_chrys_server_class(FakeAiohttpBaseServer)
    server = server_class(
        "chrys __tui_subprocess__",
        host="localhost",
        port=7777,
        title="Chrys",
        public_url="https://chrys.example",
        auth_config=serve_cli.ServeAuthConfig(
            verifier=serve_cli.PasswordVerifier.from_password("secret"),
            trust_forwarded_for=True,
        ),
    )
    client = TestClient(
        TestServer(await server._make_app()),
        headers={"Host": "chrys.example", "Origin": "https://chrys.example"},
    )
    await client.start_server()
    try:
        locked_ip = "203.0.113.10"
        for _ in range(serve_cli._AUTH_MAX_FAILURES - 1):
            login_response = await client.get("/")
            csrf_token = _csrf_token(await login_response.text())
            response = await client.post(
                _auth_login_url(),
                data={serve_cli._AUTH_CSRF_FIELD: csrf_token, "password": "wrong"},
                headers={"X-Forwarded-For": locked_ip},
            )
            assert response.status == 401

        login_response = await client.get("/")
        csrf_token = _csrf_token(await login_response.text())
        lockout_response = await client.post(
            _auth_login_url(),
            data={serve_cli._AUTH_CSRF_FIELD: csrf_token, "password": "wrong"},
            headers={"X-Forwarded-For": locked_ip},
        )
        assert lockout_response.status == 429

        login_response = await client.get("/")
        csrf_token = _csrf_token(await login_response.text())
        same_ip = await client.post(
            _auth_login_url(),
            data={serve_cli._AUTH_CSRF_FIELD: csrf_token, "password": "secret"},
            headers={"X-Forwarded-For": locked_ip},
            allow_redirects=False,
        )
        assert same_ip.status == 429
        assert serve_cli._AUTH_COOKIE_NAME not in same_ip.cookies

        login_response = await client.get("/")
        csrf_token = _csrf_token(await login_response.text())
        other_ip = await client.post(
            _auth_login_url(),
            data={serve_cli._AUTH_CSRF_FIELD: csrf_token, "password": "secret"},
            headers={"X-Forwarded-For": "203.0.113.20"},
            allow_redirects=False,
        )
        assert other_ip.status == 302
        assert serve_cli._AUTH_COOKIE_NAME in other_ip.cookies
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_auth_lockout_does_not_consume_csrf_token() -> None:
    server_class = serve_cli._make_chrys_server_class(FakeAiohttpBaseServer)
    server = server_class(
        "chrys __tui_subprocess__",
        host="localhost",
        port=7777,
        title="Chrys",
        public_url="https://chrys.example",
        auth_config=serve_cli.ServeAuthConfig(
            verifier=serve_cli.PasswordVerifier.from_password("secret"),
        ),
    )
    client = TestClient(
        TestServer(await server._make_app()),
        headers={"Host": "chrys.example", "Origin": "https://chrys.example"},
    )
    await client.start_server()
    try:
        for _ in range(serve_cli._AUTH_MAX_FAILURES):
            login_response = await client.get("/")
            csrf_token = _csrf_token(await login_response.text())
            await client.post(_auth_login_url(), data={serve_cli._AUTH_CSRF_FIELD: csrf_token, "password": "wrong"})

        login_response = await client.get("/")
        csrf_token = _csrf_token(await login_response.text())
        locked = await client.post(
            _auth_login_url(),
            data={serve_cli._AUTH_CSRF_FIELD: csrf_token, "password": "secret"},
        )
        assert locked.status == 429

        locked_again = await client.post(
            _auth_login_url(),
            data={serve_cli._AUTH_CSRF_FIELD: csrf_token, "password": "secret"},
        )
        assert locked_again.status == 429
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_auth_login_requires_csrf_before_counting_password_failure() -> None:
    server_class = serve_cli._make_chrys_server_class(FakeAiohttpBaseServer)
    server = server_class(
        "chrys __tui_subprocess__",
        host="localhost",
        port=7777,
        title="Chrys",
        public_url="https://chrys.example",
        auth_config=serve_cli.ServeAuthConfig(
            verifier=serve_cli.PasswordVerifier.from_password("secret"),
        ),
    )
    client = TestClient(
        TestServer(await server._make_app()),
        headers={"Host": "chrys.example", "Origin": "https://chrys.example"},
    )
    await client.start_server()
    try:
        for _ in range(serve_cli._AUTH_MAX_FAILURES + 1):
            response = await client.post(_auth_login_url(), data={"password": "wrong"})
            assert response.status == 403

        login_response = await client.get("/")
        csrf_token = _csrf_token(await login_response.text())
        good_login = await client.post(
            _auth_login_url(),
            data={serve_cli._AUTH_CSRF_FIELD: csrf_token, "password": "secret"},
            allow_redirects=False,
        )

        assert good_login.status == 302
        assert serve_cli._AUTH_COOKIE_NAME in good_login.cookies
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_auth_login_rejects_cross_origin_before_counting_password_failure() -> None:
    server_class = serve_cli._make_chrys_server_class(FakeAiohttpBaseServer)
    server = server_class(
        "chrys __tui_subprocess__",
        host="localhost",
        port=7777,
        title="Chrys",
        public_url="https://chrys.example",
        auth_config=serve_cli.ServeAuthConfig(
            verifier=serve_cli.PasswordVerifier.from_password("secret"),
        ),
    )
    client = TestClient(
        TestServer(await server._make_app()),
        headers={"Host": "chrys.example", "Origin": "https://chrys.example"},
    )
    await client.start_server()
    try:
        login_response = await client.get("/")
        csrf_token = _csrf_token(await login_response.text())
        rejected = await client.post(
            _auth_login_url(),
            data={serve_cli._AUTH_CSRF_FIELD: csrf_token, "password": "wrong"},
            headers={"Origin": "https://evil.example"},
        )
        assert rejected.status == 403

        good_login = await client.post(
            _auth_login_url(),
            data={serve_cli._AUTH_CSRF_FIELD: csrf_token, "password": "secret"},
            headers={"Origin": "https://chrys.example"},
            allow_redirects=False,
        )
        assert good_login.status == 302
        assert serve_cli._AUTH_COOKIE_NAME in good_login.cookies
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_auth_login_rejects_invalid_origin_port() -> None:
    server_class = serve_cli._make_chrys_server_class(FakeAiohttpBaseServer)
    server = server_class(
        "chrys __tui_subprocess__",
        host="localhost",
        port=7777,
        title="Chrys",
        public_url="https://chrys.example",
        auth_config=serve_cli.ServeAuthConfig(
            verifier=serve_cli.PasswordVerifier.from_password("secret"),
        ),
    )
    client = TestClient(
        TestServer(await server._make_app()),
        headers={"Host": "chrys.example", "Origin": "https://chrys.example"},
    )
    await client.start_server()
    try:
        login_response = await client.get("/")
        csrf_token = _csrf_token(await login_response.text())
        rejected = await client.post(
            _auth_login_url(),
            data={serve_cli._AUTH_CSRF_FIELD: csrf_token, "password": "wrong"},
            headers={"Origin": "https://evil.example:bad"},
        )
        assert rejected.status == 403

        good_login = await client.post(
            _auth_login_url(),
            data={serve_cli._AUTH_CSRF_FIELD: csrf_token, "password": "secret"},
            headers={"Origin": "https://chrys.example"},
            allow_redirects=False,
        )
        assert good_login.status == 302
        assert serve_cli._AUTH_COOKIE_NAME in good_login.cookies
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_auth_static_assets_are_protected() -> None:
    server_class = serve_cli._make_chrys_server_class(FakeAiohttpBaseServer)
    server = server_class(
        "chrys __tui_subprocess__",
        host="localhost",
        port=7777,
        title="Chrys",
        public_url=None,
        auth_config=serve_cli.ServeAuthConfig(
            verifier=serve_cli.PasswordVerifier.from_password("secret"),
        ),
    )
    client = TestClient(
        TestServer(await server._make_app()),
        headers={"Host": "localhost:7777", "Origin": "http://localhost:7777"},
    )
    await client.start_server()
    try:
        response = await client.get("/static/textual.js")

        assert response.status == 401

        login_response = await client.get("/")
        csrf_token = _csrf_token(await login_response.text())
        good_login = await client.post(
            _auth_login_url(),
            data={serve_cli._AUTH_CSRF_FIELD: csrf_token, "password": "secret"},
            allow_redirects=False,
        )
        cookie = good_login.cookies[serve_cli._AUTH_COOKIE_NAME]
        cookie_header = f"{serve_cli._AUTH_COOKIE_NAME}={cookie.value}"
        response = await client.get("/static/textual.js", headers={"Cookie": cookie_header})

        assert response.status == 200
        assert await response.text() == "STATIC"
    finally:
        await client.close()


def _auth_login_url() -> str:
    return serve_cli._AUTH_LOGIN_PATH


def _csrf_token(html: str) -> str:
    marker = f'name="{serve_cli._AUTH_CSRF_FIELD}" value="'
    return html.split(marker, 1)[1].split('"', 1)[0]


def test_main_sets_process_title(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[bool] = []
    monkeypatch.setattr("chrys.orchestration.startup.set_process_title", lambda: calls.append(True))
    monkeypatch.setattr(serve_cli, "run_command", lambda _args: None)

    assert serve_cli.main([]) == 0
    assert calls == [True]


def test_textual_serve_banner_is_icode_branded() -> None:
    server_class = serve_cli._load_server_class()
    server = server_class(
        "chrys __tui_subprocess__",
        host="localhost",
        port=7777,
        title="Chrys",
        public_url=None,
    )
    output = io.StringIO()
    server.console = Console(file=output, force_terminal=False, no_color=True)

    asyncio.run(server.on_startup(object()))

    text = output.getvalue()
    assert "░▀█▀░█▀▀░█▀█░█▀▄░█▀▀" in text
    assert f"v{__version__}" in text
    assert "TEXTUAL-SERVE" not in text
    assert f"Serving {APP_DISPLAY_NAME} TUI on http://localhost:7777" in text


def test_chrys_server_normalizes_ipv6_default_public_url() -> None:
    server_class = serve_cli._make_chrys_server_class(FakeAiohttpBaseServer)
    server = server_class(
        "chrys __tui_subprocess__",
        host="::1",
        port=7777,
        title=APP_DISPLAY_NAME,
        public_url=None,
    )

    assert server.public_url == "http://[::1]:7777"


def test_chrys_server_rejects_malformed_public_url_without_auth() -> None:
    server_class = serve_cli._make_chrys_server_class(FakeAiohttpBaseServer)

    with pytest.raises(RuntimeError, match="--public-url must be an absolute"):
        server_class(
            "chrys __tui_subprocess__",
            host="localhost",
            port=7777,
            title="Chrys",
            public_url="https:foo",
        )


def test_chrys_server_banner_shows_version_without_the_subprocess_command() -> None:
    class FakeBaseServer:
        def __init__(
            self,
            command: str,
            *,
            host: str,
            port: int,
            title: str,
            public_url: str | None,
        ) -> None:
            self.command = command
            self.host = host
            self.port = port
            self.title = title
            self.public_url = public_url or f"http://{host}:{port}"
            self.console = Console(file=io.StringIO(), force_terminal=False, no_color=True)

        def serve(self, *, debug: bool = False) -> None:
            self.debug = debug

    server_class = serve_cli._make_chrys_server_class(FakeBaseServer)
    server = server_class(
        "chrys __tui_subprocess__",
        host="localhost",
        port=7777,
        title="Chrys",
        public_url=None,
    )

    asyncio.run(server.on_startup(object()))

    text = server.console.file.getvalue()
    assert f"v{__version__}" in text
    assert f"Serving {APP_DISPLAY_NAME} TUI on http://localhost:7777" in text
    assert "chrys __tui_subprocess__" not in text
    assert "[cyan]" not in text
