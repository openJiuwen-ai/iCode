# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Loopback mock of the AIxCoding TUI update-detection service.

Simulates ``GET /csas/api/v2/releases/check`` — the release-check contract the
TUI update-detection feature will call at startup. The real backend does not
exist yet; this mock freezes the wire contract so client implementation, manual
runs, and integration tests can proceed against it:

* four required query params (``clientType`` / ``platform`` / ``architecture``
  / ``currentVersion``), strict mode: unknown/missing/invalid →
  ``400 {"error": "invalid_app_release_query"}``;
* ``200`` serves the v2 manifest JSON with an ``ETag`` header (SHA-256 of the
  body) and honours ``If-None-Match`` with ``304``;
* ``204`` when there is no applicable release.

The manifest is built per request from the query — ``kind`` mirrors
``clientType`` and the artifact platform (friendly spelling derived from
``platform``) / architecture reflect the caller — around a configurable
version and download URL, so any caller gets a coherent answer.

The only CLI flag is ``--port`` (default loopback :7778; ``0`` picks a free
port and reports it through the returned server's ``base_url``). Otherwise the
process starts with defaults (update mode, candidate ``999.0.0``) and every
response-shaping knob is steered at runtime via ``POST /debug/config``;
``POST /debug/reset`` restores all defaults and drops the request log in one call.

Four modes (runtime ``POST /debug/config``):

``update`` (default)
    Answer like the real service would: the manifest (200, or 304 on a
    matching ``If-None-Match``) only when the candidate version is strictly
    newer than the request's ``currentVersion`` under SemVer 2.0.0
    precedence; ``204`` otherwise. ``force_update`` skips the comparison so
    a smaller candidate can be served deliberately (dirty-data drills).

``no_update``
    Valid queries get ``204`` — the "already latest" path.

``error``
    Valid queries get ``500`` — exercises the client's silent-failure path.

``garbage``
    Valid queries get ``200`` with an unparseable body — exercises the
    client's parse-failure path.

Invalid queries answer the contract's ``400`` in every mode.

Malformed-response drills (a ``/debug/config`` parameter group of their own,
both scoped to the 200 answer): ``raw_body`` replaces the manifest with any
literal body (e.g. a truncated answer) and ``drop_fields`` omits manifest
fields (dotted paths such as ``artifact.url`` allowed) — covering the
client's parse-failure and structure-mismatch silent paths respectively.

Run it from the repository root::

    uv run python aixcoding/mock/update/server.py    # loopback :7778, update mode

Then steer it at runtime::

    curl -X POST http://127.0.0.1:7778/debug/config -d '{"mode": "no_update"}'

Importable for tests: ``MockUpdateConfig`` / ``MockUpdateState`` /
``create_server`` start a loopback instance on an ephemeral port.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
import socket
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from dataclasses import fields as dataclass_fields
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

_LOGGER = logging.getLogger("chrys.mock_update")

API_PREFIX = "/csas/api/v2"
RELEASE_ROUTE = f"{API_PREFIX}/releases/check"
DEBUG_CONFIG_ROUTE = "/debug/config"
DEBUG_REQUESTS_ROUTE = "/debug/requests"
DEBUG_RESET_ROUTE = "/debug/reset"

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 7778
MAX_LOGGED_REQUESTS = 200

# Loopback only, per the aixcoding/mock/ conventions (aixcoding/mock/README.md);
# ``create_server`` rejects any other host at startup.
_LOOPBACK_HOSTS = ("127.0.0.1", "::1")

# Modes: see the module docstring. Invalid queries answer 400 in every mode.
MODES = ("update", "no_update", "error", "garbage")

# The contract's strict-mode rejection body — byte-stable across all 400s.
INVALID_QUERY_BODY = {"error": "invalid_app_release_query"}

# Strict-mode value domains and the SemVer shape of ``currentVersion``
# (mirrors the client-side validation).
# Final contract: four params — distribution/channel were dropped, and win64
# merged back into win32 (bitness lives solely in ``architecture``).
_ALLOWED_CLIENT_TYPES = ("desktop", "cli")
_ALLOWED_PLATFORMS = ("win32", "darwin", "linux")
_ALLOWED_ARCHITECTURES = ("arm64", "x64")
_VERSION_MIN_LENGTH, _VERSION_MAX_LENGTH = 1, 64
_SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$")

# v2 artifact.platform uses the friendly OS spelling; the query's platform
# domain maps 1:1 (win32→windows, darwin→macos, linux stays linux).
_FRIENDLY_PLATFORM = {"win32": "windows", "darwin": "macos", "linux": "linux"}


@dataclass
class MockUpdateConfig:
    """Knobs a mock instance honours; every field is runtime-writable via ``POST /debug/config``."""

    mode: str = "update"  # update | no_update | error | garbage
    # Candidate version advertised in the manifest. Default 999.0.0 so the
    # out-of-the-box answer to any plausible client is "there is an update";
    # pass a smaller/equal version (or no_update mode) to simulate "latest".
    version: str = "999.0.0"
    download_url: str = ""  # artifact.url; empty → derived from base_url
    slow_ms: int = 0  # artificial delay before answering, to exercise timeouts
    force_update: bool = False  # serve 200 without the newer-version comparison
    force_upgrade: bool = False  # forceUpgrade flag advertised in the manifest
    # Malformed-response drills, scoped to the 200 answer (see module docstring):
    # raw_body replaces the manifest with any literal body; drop_fields omits
    # manifest fields (dotted paths allowed) to break the structure instead.
    raw_body: str = ""
    drop_fields: list[str] = field(default_factory=list)
    base_url: str = ""  # filled in by create_server after binding

    def artifact_url(self) -> str:
        """The download URL handed out in the manifest."""
        if self.download_url:
            return self.download_url
        return f"{self.base_url}/mock-attachments/AIxCoding-{self.version}-arm64.zip"


class MockUpdateState:
    """Configuration holder plus the request log behind ``/debug/requests``."""

    def __init__(self, config: MockUpdateConfig) -> None:
        self._config = config
        self._lock = threading.RLock()
        self._requests: deque[dict[str, Any]] = deque(maxlen=MAX_LOGGED_REQUESTS)

    @property
    def config(self) -> MockUpdateConfig:
        """The configuration this state was built with."""
        return self._config

    def record(self, query: dict[str, str], status: int) -> None:
        """Append one served request (oldest entries fall off the cap)."""
        with self._lock:
            self._requests.append(
                {
                    "received_at": int(time.time() * 1000),
                    "query": dict(query),
                    "status": status,
                    "mode": self._config.mode,
                }
            )

    def requests(self) -> list[dict[str, Any]]:
        """Return the request log, oldest first (diagnostics and tests)."""
        with self._lock:
            return list(self._requests)

    def reset(self) -> dict[str, Any]:
        """Restore every config knob to its default and drop the request log.

        The ``base_url`` filled in by ``create_server`` after binding is kept —
        it identifies the instance, it is not a behaviour knob. Returns the
        post-reset snapshot so the caller sees the restored state.
        """
        defaults = MockUpdateConfig()
        with self._lock:
            for spec in dataclass_fields(MockUpdateConfig):
                if spec.name == "base_url":
                    continue
                value = getattr(defaults, spec.name)
                setattr(self._config, spec.name, list(value) if isinstance(value, list) else value)
            self._requests.clear()
            return self.snapshot()

    def apply(self, changes: dict[str, Any]) -> dict[str, Any]:
        """Update config knobs at runtime (``POST /debug/config``) and return the new snapshot."""
        unknown = sorted(
            set(changes)
            - {"mode", "version", "download_url", "slow_ms", "force_update", "force_upgrade", "raw_body", "drop_fields"}
        )
        if unknown:
            raise ValueError(f"unknown config key(s): {', '.join(unknown)}")
        mode = changes.get("mode")
        if mode is not None and mode not in MODES:
            raise ValueError(f"invalid mode: {mode} (expected one of {', '.join(MODES)})")
        if "version" in changes:
            version_problem = _semver_problem(str(changes["version"]))
            if version_problem is not None:
                raise ValueError(f"invalid version: {version_problem}")
        if "raw_body" in changes and not isinstance(changes["raw_body"], str):
            raise ValueError("invalid raw_body: expected a string")
        if "drop_fields" in changes:
            drop_problem = _drop_fields_problem(changes["drop_fields"])
            if drop_problem is not None:
                raise ValueError(f"invalid drop_fields: {drop_problem}")
        with self._lock:
            for key, value in changes.items():
                setattr(self._config, key, value)
            return self.snapshot()

    def snapshot(self) -> dict[str, Any]:
        """Return the current configuration plus the request count."""
        with self._lock:
            return {
                "mode": self._config.mode,
                "version": self._config.version,
                "download_url": self._config.artifact_url(),
                "slow_ms": self._config.slow_ms,
                "force_update": self._config.force_update,
                "force_upgrade": self._config.force_upgrade,
                "raw_body": self._config.raw_body,
                "drop_fields": list(self._config.drop_fields),
                "requests": len(self._requests),
            }


def _semver_problem(version: str) -> str | None:
    """Rejection reason for a strict-SemVer string, or ``None`` when valid.

    Shared by query validation (``currentVersion``) and config validation so
    every version this mock accepts or advertises passes the same strictness.
    """
    if not _VERSION_MIN_LENGTH <= len(version) <= _VERSION_MAX_LENGTH:
        return f"length {len(version)} outside {_VERSION_MIN_LENGTH}..{_VERSION_MAX_LENGTH}"
    if _SEMVER_RE.fullmatch(version) is None:
        return "not strict SemVer (MAJOR.MINOR.PATCH[-PRERELEASE])"
    return None


def _drop_fields_problem(fields: Any) -> str | None:
    """Rejection reason for a ``drop_fields`` list, or ``None`` when usable."""
    if not isinstance(fields, list) or not all(isinstance(item, str) and item.strip() for item in fields):
        return "expected a list of non-empty manifest field paths"
    return None


def _drop_fields(manifest: dict[str, Any], fields: list[str]) -> dict[str, Any]:
    """Remove dotted-path fields (``version``, ``artifact.url``, ...) from the manifest.

    Unknown paths are no-ops, so a drill can name fields that may vanish from
    the manifest as the v2 shape evolves.
    """
    for path in fields:
        parent, _, leaf = path.rpartition(".")
        node: Any = manifest
        for part in parent.split(".") if parent else ():
            node = node.get(part) if isinstance(node, dict) else None
        if isinstance(node, dict):
            node.pop(leaf, None)
    return manifest


def _prerelease_key(prerelease: str) -> list[tuple[int, int, str]]:
    """Encode a dot-separated prerelease for SemVer 2.0.0 identifier ordering.

    Numeric identifiers compare numerically and rank below alphanumeric ones,
    so each becomes ``(0, value, "")`` or ``(1, 0, text)`` — comparable tuples
    instead of an int/str mix Python refuses to order.
    """
    return [(0, int(part), "") if part.isdigit() else (1, 0, part) for part in prerelease.split(".")]


def _semver_key(version: str) -> tuple[int, int, int, int, list[tuple[int, int, str]]]:
    """Total-order sort key implementing SemVer 2.0.0 precedence.

    A release outranks every prerelease of the same core, hence the release
    flag sitting between the numeric core and the identifier list.
    """
    core, _, prerelease = version.partition("-")
    major, minor, patch = (int(part) for part in core.split("."))
    if not prerelease:
        return major, minor, patch, 1, []
    return major, minor, patch, 0, _prerelease_key(prerelease)


def _is_newer(candidate: str, current: str) -> bool:
    """True when ``candidate`` is strictly newer than ``current`` per SemVer 2.0.0."""
    return _semver_key(candidate) > _semver_key(current)


def _validate_query(params: dict[str, list[str]]) -> str | None:
    """Strict-mode check over the four contract parameters.

    Returns the problem description (for the server log; the body is fixed by
    the contract), or ``None`` when every parameter is present and valid.
    """
    known = ("clientType", "platform", "architecture", "currentVersion")
    unknown = sorted(set(params) - set(known))
    if unknown:
        return f"unknown parameter(s): {', '.join(unknown)}"
    missing = [name for name in known if name not in params]
    if missing:
        return f"missing parameter(s): {', '.join(missing)}"
    query = {name: params[name][-1] for name in known}
    if query["clientType"] not in _ALLOWED_CLIENT_TYPES:
        return f"invalid clientType: {query['clientType']}"
    if query["platform"] not in _ALLOWED_PLATFORMS:
        return f"invalid platform: {query['platform']}"
    if query["architecture"] not in _ALLOWED_ARCHITECTURES:
        return f"invalid architecture: {query['architecture']}"
    version_problem = _semver_problem(query["currentVersion"])
    if version_problem is not None:
        return f"invalid currentVersion: {version_problem}"
    return None


def _build_manifest(config: MockUpdateConfig, query: dict[str, str]) -> dict[str, Any]:
    """Assemble the v2 release manifest, mirroring the caller's clientType/platform."""
    kind = query["clientType"]
    artifact_platform = _FRIENDLY_PLATFORM[query["platform"]]
    return _drop_fields(
        {
            "schemaVersion": 2,
            "kind": kind,
            "environmentId": "production",
            "channelId": "stable",  # channel param dropped from the contract; fixed default
            "releaseId": f"{kind}-{config.version}-{artifact_platform}-{query['architecture']}",
            "version": config.version,
            "publishedAt": "2026-09-28T10:00:00.000Z",
            "releaseNotes": "## 新增\n- mock 更新说明\n## 修复\n- ...",
            "forceUpgrade": config.force_upgrade,
            "artifact": {
                "platform": artifact_platform,
                "architecture": query["architecture"],
                "url": config.artifact_url(),
            },
        },
        config.drop_fields,
    )


def _etag_of(body: bytes) -> str:
    """Return the contract's ETag: the SHA-256 of the manifest body, hex."""
    return hashlib.sha256(body).hexdigest()


def _etag_matches(header_value: str, etag: str) -> bool:
    """Compare an ``If-None-Match`` header against ``etag``.

    Tolerates comma-separated lists, the ``W/`` weak prefix, and quoted
    values, so both curl-style quoted tags and bare hex tags hit.
    """
    candidates = (part.strip().removeprefix("W/").strip('"') for part in header_value.split(","))
    return etag in candidates


def build_handler_class(state: MockUpdateState) -> type[BaseHTTPRequestHandler]:
    """Return a request handler class bound to ``state``."""

    class MockUpdateHandler(BaseHTTPRequestHandler):
        """Routes the release-check endpoint plus the runtime-control debug helpers."""

        server_version = "ChrysMockUpdate/1.0"

        def _send(
            self,
            status: int,
            body: bytes,
            content_type: str,
            extra_headers: dict[str, str] | None = None,
        ) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            for name, value in (extra_headers or {}).items():
                self.send_header(name, value)
            if status in (204, 304):
                # 204/304 carry no body and no Content-Length by definition.
                self.end_headers()
                return
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if body:
                self.wfile.write(body)

        def _send_json(self, status: int, payload: dict[str, Any], extra_headers: dict[str, str] | None = None) -> None:
            self._send(
                status,
                json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                "application/json; charset=utf-8",
                extra_headers,
            )

        def _read_json_body(self) -> dict[str, Any]:
            """Decode a JSON request body; malformed input raises ``ValueError``."""
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                return {}
            raw = self.rfile.read(length)
            try:
                parsed = json.loads(raw.decode("utf-8"))
            except UnicodeDecodeError, json.JSONDecodeError:
                raise ValueError("body is not valid JSON") from None
            return parsed if isinstance(parsed, dict) else {}

        def do_GET(self) -> None:
            """Dispatch the release check and the read-only helpers."""
            split = urlsplit(self.path)
            if split.path == RELEASE_ROUTE:
                self._handle_release(split.query)
            elif split.path == DEBUG_REQUESTS_ROUTE:
                self._send_json(200, {"requests": state.requests()})
            else:
                self._send_json(404, {"error": "not_found"})

        def do_POST(self) -> None:
            """Dispatch the runtime-control debug endpoints."""
            split = urlsplit(self.path)
            if split.path == DEBUG_CONFIG_ROUTE:
                self._handle_debug_config()
            elif split.path == DEBUG_RESET_ROUTE:
                self._send_json(200, state.reset())
            else:
                self._send_json(404, {"error": "not_found"})

        def _handle_release(self, raw_query: str) -> None:
            params = parse_qs(raw_query, keep_blank_values=True)
            problem = _validate_query(params)
            if problem is not None:
                _LOGGER.info("rejected query (%s): %s", problem, raw_query)
                state.record({}, 400)
                self._send_json(400, INVALID_QUERY_BODY)
                return
            query = {name: params[name][-1] for name in ("clientType", "platform", "architecture", "currentVersion")}
            config = state.config
            if config.slow_ms > 0:
                time.sleep(config.slow_ms / 1000.0)
            if config.mode == "no_update":
                state.record(query, 204)
                self._send(204, b"", "application/json")
                return
            if config.mode == "error":
                state.record(query, 500)
                self._send_json(500, {"error": "internal_error"})
                return
            if not config.force_update and not _is_newer(config.version, query["currentVersion"]):
                # update mode answers like the real service: a manifest only for
                # a strictly newer candidate; an equal/older one is "already latest".
                state.record(query, 204)
                self._send(204, b"", "application/json")
                return
            body = json.dumps(_build_manifest(config, query), ensure_ascii=False).encode("utf-8")
            if config.mode == "garbage" or config.raw_body:
                # Malformed-response drills on the 200 answer: garbage serves a
                # fixed unparseable body, raw_body serves any literal (e.g. a
                # truncated answer). drop_fields has no effect here by design —
                # it breaks the structure, not the JSON parseability.
                state.record(query, 200)
                raw = config.raw_body.encode("utf-8") if config.raw_body else b'{"releaseId": <unparseable>'
                self._send(200, raw, "application/json; charset=utf-8")
                return
            etag = _etag_of(body)
            match_header = self.headers.get("If-None-Match")
            if match_header is not None and _etag_matches(match_header, etag):
                state.record(query, 304)
                self._send(304, b"", "application/json")
                return
            state.record(query, 200)
            self._send(200, body, "application/json; charset=utf-8", {"ETag": etag})

        def _handle_debug_config(self) -> None:
            try:
                changes = self._read_json_body()
                snapshot = state.apply(changes)
            except ValueError as error:
                self._send_json(400, {"error": str(error)})
                return
            _LOGGER.info("config updated: %s", changes)
            self._send_json(200, snapshot)

        def log_message(self, format: str, *args: Any) -> None:
            """Route the stdlib access log through ``logging``."""
            _LOGGER.debug("mock_update %s", format % args)

    return MockUpdateHandler


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
    config: MockUpdateConfig | None = None,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
) -> ThreadingHTTPServer:
    """Build a stopped mock server ready for ``serve_forever()``."""
    if host not in _LOOPBACK_HOSTS:
        raise ValueError("The update-detection mock server may bind only to a loopback address.")
    config = config or MockUpdateConfig()
    if config.mode not in MODES:
        raise ValueError(f"invalid mode: {config.mode} (expected one of {', '.join(MODES)})")
    version_problem = _semver_problem(config.version)
    if version_problem is not None:
        raise ValueError(f"invalid candidate version: {version_problem}")
    if not isinstance(config.raw_body, str):
        raise ValueError("invalid raw_body: expected a string")
    drop_problem = _drop_fields_problem(config.drop_fields)
    if drop_problem is not None:
        raise ValueError(f"invalid drop_fields: {drop_problem}")
    state = MockUpdateState(config)
    server = _ExclusiveBindHTTPServer((host, port), build_handler_class(state))
    server.daemon_threads = True
    # Resolve after binding so the ephemeral port (``port=0``, used by tests)
    # still hands out a download URL that points back at this instance.
    display_host = f"[{host}]" if ":" in host else host
    config.base_url = f"http://{display_host}:{server.server_address[1]}"
    server.mock_state = state  # type: ignore[attr-defined]
    server.mock_config = config  # type: ignore[attr-defined]
    return server


def main(argv: list[str] | None = None) -> int:
    """Run the mock server (loopback, default :7778, update mode) until interrupted.

    ``--port`` overrides the bind port; steer every other knob — mode,
    candidate version, delay, dirty-data flags, malformed-response drills —
    at runtime via ``POST /debug/config``. Returns a process exit code.
    """
    parser = argparse.ArgumentParser(
        prog="mock_update_server",
        description="Loopback mock of the AIxCoding TUI update-detection service.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help=f"bind port (default {DEFAULT_PORT}); 0 picks a free port",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    try:
        server = create_server(port=args.port)
    except (OSError, ValueError) as exc:
        _LOGGER.error("cannot start mock server: %s", exc)
        return 1

    base = server.mock_config.base_url  # type: ignore[attr-defined]
    _LOGGER.info("mock update-detection server on %s (update mode, candidate 999.0.0)", base)
    _LOGGER.info("  GET  %s%s", base, RELEASE_ROUTE)
    _LOGGER.info("  GET  %s%s", base, DEBUG_REQUESTS_ROUTE)
    _LOGGER.info("  POST %s%s  # steer every knob at runtime", base, DEBUG_CONFIG_ROUTE)
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        _LOGGER.info("shutting down")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
