# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""One read-only report on whether the browser extension can work here.

``icode-browser-use doctor`` prints the report as short text, or as one JSON object
with ``--json`` (``schema`` 1; keys are added, never renamed). Collecting it
reads files and process ids only: no browser is launched and no Bridge or Broker
is started. ``--connect`` adds a two-second TCP connection to a Bridge or Broker
discovery file that already exists. ``--bridge`` is the one active probe: it
starts this plugin's bridge server, waits for the extension's native host up to
``bridge_wait`` seconds, reports ``bridge.probe`` and stops the server again; a
successful probe records ``bridge.handshake`` (next to the discovery file) so
later tool calls may wait through the extension's 30 s alarm.

Every problem carries a stable ``code`` (``CODES``), a ``message`` and the
``advice`` that fixes it. A problem with severity ``error`` fails the check;
warnings describe an optional backend (Bridge, Broker) that is not ready, or a
version mismatch that only a restart fixes.

``plugin.install`` names how this copy was reached: ``folder`` for a folder
package the host executed by file, ``distribution`` for an imported package.
``distribution`` is null when no installed distribution owns it (a source-tree
run), so ``distribution`` is the field to read for whether the wheel is there.
``plugin.distribution_version`` is the installed wheel's version (null without
one); it differs from ``plugin.version`` when a running iCode still holds the
old module. ``bridge.plugin_version`` is the version the unpacked extension is
compared with. ``plugin.import`` records whether the host could actually import
the extension module; it is null when the host API is too old to try.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import os
import socket
import sys
import time
from pathlib import Path
from typing import Any, Sequence

from . import __version__, distribution
from ._proc import pid_alive
from .bridge.host import bridge_file
from .bridge.install import (
    default_extension_install_dir,
    native_manifest_path,
    registered_manifest,
    same_manifest,
)
from .cdp.launcher import discover_chrome
from .config import BrowserConfig, BrowserEnv, ConfigError, parse_domains, parse_origins

SCHEMA = 1

CODES = (
    "browser_not_found",
    "bridge_host_missing",
    "bridge_not_connected",
    "bridge_not_installed",
    "bridge_not_running",
    "bridge_stale",
    "bridge_unreachable",
    "bridge_version_mismatch",
    "broker_config_differs",
    "broker_not_running",
    "broker_stale",
    "broker_unreachable",
    "broker_unsupported",
    "config_entries_ignored",
    "config_invalid",
    "extension_id_conflict",
    "host_api_incomplete",
    "host_missing",
    "plugin_import_failed",
    "plugin_version_mismatch",
    "state_dir_unwritable",
)

REQUIRED_API = ("Extension", "ToolCall", "ToolContext", "ToolError", "TurnContext")
REQUIRED_CONTEXT = ("agent_name", "cwd", "data_dir", "session_data_dir", "session_id")


def collect(*, env: Any = None, config_dir: Path | None = None, connect: bool = False) -> dict[str, Any]:
    """Build the report; nothing here launches a browser or a helper process."""
    problems: list[dict[str, str]] = []
    entry_point = distribution.wheel_entry_point()
    folder = distribution.folder_pack(config_dir)
    label = distribution.distribution_label(entry_point) if entry_point is not None else None
    if entry_point is not None and folder is not None:
        problems.append(
            _problem(
                "extension_id_conflict",
                f"extension id {distribution.EXTENSION_ID!r} is installed twice: {label} and {folder}",
                "remove one source; iCode loads neither while both provide the same id",
            )
        )
    duplicates = distribution.duplicate_folders(config_dir)
    if duplicates:
        problems.append(
            _problem(
                "extension_id_conflict",
                f"extension id {distribution.EXTENSION_ID!r} has two folder sources: "
                + " and ".join(str(path) for path in duplicates),
                "remove one source; iCode loads neither while both provide the same id",
            )
        )
    plugin = {
        "version": __version__,
        "install": distribution.loaded_from(__name__),
        "module": __name__,
        "distribution": label,
        "distribution_version": distribution.distribution_version(entry_point),
        "folder": str(folder) if folder is not None else None,
    }
    installed = plugin["distribution_version"]
    if installed and installed != __version__ and plugin["install"] == "distribution":
        newer = _version_tuple(installed) > _version_tuple(__version__)
        advice = (
            f"restart iCode to load {installed}; this running process keeps {__version__} until then"
            if newer
            else f"this process runs a newer copy than the installed distribution; reinstall {installed} "
            "or run from a clean environment"
        )
        problems.append(
            _problem(
                "plugin_version_mismatch",
                f"this process imported {__name__} {__version__}, but the installed distribution is "
                f"{label or installed}",
                advice,
                "warning",
            )
        )
    host, host_problems = _host()
    probe, probe_problems = _import_probe(host)
    plugin.update(probe)
    problems.extend((*host_problems, *probe_problems))
    config, cfg, config_problems = _config(env)
    backend = config["backend"] or "unknown"
    browser, browser_problems = _browser(cfg, backend)
    state, state_problems = _state(cfg, BrowserEnv(env))
    bridge, bridge_problems = _bridge(backend, connect, BrowserEnv(env))
    broker, broker_problems = _broker(cfg, BrowserEnv(env), backend, connect)
    problems.extend((*config_problems, *browser_problems, *state_problems))
    problems.extend((*bridge_problems, *broker_problems))
    return {
        "schema": SCHEMA,
        "ok": not any(problem["severity"] == "error" for problem in problems),
        "plugin": plugin,
        "host": host,
        "config": config,
        "browser": browser,
        "state": state,
        "bridge": bridge,
        "broker": broker,
        "problems": problems,
    }


def summary(document: dict[str, Any]) -> list[str]:
    """The short human report: one line per area, then each problem and its advice."""
    plugin, host, config = document["plugin"], document["host"], document["config"]
    lines = [
        f"plugin: {plugin['version']} ({plugin['install']}, import: {plugin['import'] or 'not checked'})",
        f"host: {host['version'] or 'not importable'} (extension api: {host['extension_api'] or 'unknown'})",
        (
            f"config: {'ok' if config['ok'] else 'invalid'} backend={config['backend'] or 'unknown'} "
            f"network={config.get('network') or 'unknown'} "
            f"domains={len(config['allowed_domains'])} origins={len(config['allowed_origins'])}"
        ),
        f"browser: {document['browser']['executable'] or 'not found'}",
        f"state: {document['state']['dir']} ({'writable' if document['state']['writable'] else 'not writable'})",
        f"bridge: {document['bridge']['state']}",
        f"broker: {document['broker']['state']}",
    ]
    probe = document["bridge"].get("probe")
    if probe is not None:
        if probe["connected"]:
            lines.append(
                f"bridge probe: connected (extension {probe['extension_version'] or 'unknown'}, "
                f"waited {probe['waited_seconds']}s)"
            )
        else:
            lines.append(f"bridge probe: not connected ({probe['error']})")
    for problem in document["problems"]:
        lines.append(f"problem: {problem['code']} [{problem['severity']}] {problem['message']}")
        lines.append(f"advice: {problem['advice']}")
    errors = sum(1 for problem in document["problems"] if problem["severity"] == "error")
    lines.append(f"doctor: {'ok' if document['ok'] else f'{errors} error(s)'}")
    return lines


def main(argv: Sequence[str] | None = None) -> int:
    """``icode-browser-use doctor [--json] [--connect] [--bridge] [--config-dir PATH]``."""
    parser = argparse.ArgumentParser(
        prog="icode-browser-use doctor",
        description="Report whether the browser extension can work here; never launches a browser.",
    )
    parser.add_argument("--json", action="store_true", help="print the machine-readable document")
    parser.add_argument(
        "--connect",
        action="store_true",
        help="also open a short connection to a running Bridge or Broker",
    )
    parser.add_argument(
        "--bridge",
        action="store_true",
        help="actively wait for the browser extension to connect (up to ICODE_BROWSER_BRIDGE_WAIT seconds)",
    )
    parser.add_argument("--config-dir", type=Path, default=None, help="iCode config dir (default: ~/.chrys)")
    args = parser.parse_args(argv)
    document = collect(config_dir=args.config_dir, connect=args.connect)
    if args.bridge:
        probe, probe_problems = probe_bridge()
        document["bridge"]["probe"] = probe
        if probe["connected"]:
            document["bridge"]["state"] = "connected"
            document["problems"] = [
                problem
                for problem in document["problems"]
                if problem["code"]
                not in {
                    "bridge_not_running",
                    "bridge_stale",
                    "bridge_unreachable",
                    "bridge_not_installed",
                    "bridge_host_missing",
                }
            ]
        document["problems"].extend(probe_problems)
        document["ok"] = not any(problem["severity"] == "error" for problem in document["problems"])
    if args.json:
        print(json.dumps(document, indent=2, ensure_ascii=True))
    else:
        print("\n".join(summary(document)))
    return 0 if document["ok"] else 1


def probe_bridge(*, env: Any = None) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """Actively wait for the extension's native host (explicit ``--bridge`` only).

    Starts this plugin's bridge server in the configured state folder exactly as a
    turn does, waits for the extension to connect (up to ``bridge_wait``), reads its
    hello and closes again. The extension only connects on its own alarm, so even a
    working install can take most of that wait.
    """
    from .bridge.protocol import PROTOCOL_VERSION
    from .bridge.server import BridgeServer

    result: dict[str, Any] = {
        "connected": False,
        "started": False,
        "extension_version": None,
        "capabilities": [],
        "protocol": None,
        "browser": None,
        "error": None,
        "waited_seconds": None,
    }
    try:
        cfg = BrowserConfig.from_env(env)
    except ConfigError as exc:
        result["error"] = str(exc)
        return result, [
            _problem(
                "bridge_not_connected",
                f"the bridge probe cannot start: {exc}",
                "fix the configuration reported above, then retry with --bridge",
            )
        ]
    discovery = Path(cfg.bridge_file) if cfg.bridge_file else cfg.state_dir / "bridge.json"

    async def probe() -> None:
        server = BridgeServer(discovery)
        started = time.monotonic()
        try:
            await server.start()
        except Exception as exc:  # noqa: BLE001 - any start failure is the finding
            result["error"] = str(exc)
            return
        result["started"] = True
        try:
            try:
                client = await server.wait_for_client(timeout=cfg.bridge_wait)
            except Exception as exc:  # noqa: BLE001 - the message names the reason
                result["error"] = str(exc)
                result["waited_seconds"] = round(time.monotonic() - started, 1)
                return
            result["connected"] = True
            result["waited_seconds"] = round(time.monotonic() - started, 1)
            try:
                hello = await client.send("Bridge.hello", {"protocol": PROTOCOL_VERSION}, timeout=5.0)
            except Exception as exc:  # noqa: BLE001 - a failed hello is still a failed probe
                result["connected"] = False
                result["error"] = f"handshake failed: {exc}"
                return
            if isinstance(hello, dict):
                result["protocol"] = hello.get("protocol")
                result["extension_version"] = hello.get("extensionVersion")
                capabilities = hello.get("capabilities")
                if isinstance(capabilities, list):
                    result["capabilities"] = [item for item in capabilities if isinstance(item, str)]
                result["browser"] = hello.get("browser")
        finally:
            await server.aclose()

    asyncio.run(probe())
    if result["connected"]:
        return result, []
    if not result["started"]:
        return result, [
            _problem(
                "bridge_not_connected",
                f"the state folder {discovery.parent} is held by another plugin instance: {result['error']}",
                "stop that iCode session (or point ICODE_BROWSER_STATE_DIR elsewhere) and retry",
                "warning",
            )
        ]
    payload, _path = _discovery(discovery)
    if payload is not None and pid_alive(_pid(payload)):
        return result, [
            _problem(
                "bridge_not_connected",
                "another bridge server already owns this state folder; the extension connects there, not to this probe",
                "rerun the probe while no iCode turn holds the bridge, or just run any browser tool",
                "warning",
            )
        ]
    extra_advice = ""
    if (BrowserEnv(env).get("STATE_DIR") or "").strip():
        extra_advice = (
            " (note: ICODE_BROWSER_STATE_DIR is set, which Chrome's native host does not inherit "
            "unless bridge-pointer.txt points to this state dir)"
        )
    return result, [
        _problem(
            "bridge_not_connected",
            f"the browser extension did not reach the bridge within {cfg.bridge_wait:.0f}s: {result['error']}",
            "install or reload the extension in chrome://extensions so it can start the native host, "
            f"then retry{extra_advice}",
        )
    ]


def _problem(code: str, message: str, advice: str, severity: str = "error") -> dict[str, str]:
    return {"code": code, "severity": severity, "message": message, "advice": advice}


def _version_tuple(value: str) -> tuple[int, ...]:
    """A comparable key for the dotted versions this project uses (non-digits count as 0)."""
    parts: list[int] = []
    for piece in str(value).split("."):
        digits = "".join(ch for ch in piece if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def _import_probe(host: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """Whether the host could import this extension at all (best effort, read-only).

    The folder package's ``doctor.py`` deliberately loads the core without
    executing the pack root, so this is the only place that notices a broken
    extension module — for example a host API member this build needs but does
    not check structurally, or a vendored file that does not import.
    """
    section: dict[str, Any] = {"import": None, "import_error": None}
    if host["extension_api"] != "ok":
        return section, []
    module = f"{_core_package()}.extension"
    try:
        importlib.import_module(module)
    except Exception as exc:  # noqa: BLE001 - any import failure is the finding
        section["import"] = "failed"
        section["import_error"] = f"{type(exc).__name__}: {exc}"
        return section, [
            _problem(
                "plugin_import_failed",
                f"iCode cannot import {module}: {section['import_error']}",
                "reinstall the plugin for this host, or upgrade iCode if the extension API changed",
            )
        ]
    section["import"] = "ok"
    return section, []


def _core_package() -> str:
    """The core package name in this load: ``icode_browser_use`` or its folder-package alias."""
    return __package__ or __name__.rpartition(".")[0]


def _host() -> tuple[dict[str, Any], list[dict[str, str]]]:
    section: dict[str, Any] = {"version": None, "extension_api": None, "missing": [], "error": None}
    problems: list[dict[str, str]] = []
    try:
        import chrys
        from chrys import extensions as host_api
    except Exception as exc:  # noqa: BLE001 - the host may be absent or half-installed
        section["error"] = f"{type(exc).__name__}: {exc}"
        problems.append(
            _problem(
                "host_missing",
                "the iCode extension API is not importable from this interpreter",
                "run the check with the Python that runs iCode, or install iCode-TUI into this environment",
            )
        )
        return section, problems
    section["version"] = str(getattr(chrys, "__version__", "") or "") or None
    if section["version"] is None:
        problems.append(
            _problem(
                "host_api_incomplete",
                "the imported chrys package reports no version",
                "reinstall iCode-TUI into this environment; a half-installed host cannot load extensions",
                "warning",
            )
        )
    # SDK boundary: the host's version decides which members exist, so probing is the point.
    missing = [name for name in REQUIRED_API if not hasattr(host_api, name)]
    context = host_api.ToolContext if hasattr(host_api, "ToolContext") else None
    if context is not None:
        missing.extend(name for name in REQUIRED_CONTEXT if not hasattr(context, name))
    section["missing"] = missing
    section["extension_api"] = "incomplete" if missing else "ok"
    if missing:
        problems.append(
            _problem(
                "host_api_incomplete",
                f"iCode {section['version']} lacks extension API members this plugin calls: {', '.join(missing)}",
                "upgrade iCode; this plugin is verified against iCode-TUI 0.35.2",
            )
        )
    return section, problems


def _config(env: Any) -> tuple[dict[str, Any], BrowserConfig | None, list[dict[str, str]]]:
    problems: list[dict[str, str]] = []
    source = BrowserEnv(env)
    domains, rejected = parse_domains(source.get("ALLOWED_DOMAINS"))
    origins, rejected_origins = parse_origins(source.get("ALLOWED_ORIGINS"))
    ignored = len(rejected) + len(rejected_origins)
    section: dict[str, Any] = {
        "ok": False,
        "error": None,
        "backend": None,
        "network": None,
        "allowed_domains": list(domains),
        "allowed_origins": list(origins),
        "ignored_entries": ignored,
        "evaluate": source.enabled("EVALUATE"),
        "save_state": source.enabled("SAVE_STATE"),
    }
    try:
        cfg = BrowserConfig.from_env(env)
    except ConfigError as exc:
        section["error"] = str(exc)
        problems.append(
            _problem(
                "config_invalid",
                str(exc),
                "check ICODE_BROWSER_* settings; clear both allowlists to use public-web mode",
            )
        )
        return section, None, problems
    section["ok"] = True
    section["backend"] = cfg.backend
    section["network"] = "public-web" if cfg.public_web else "allowlist"
    if ignored:
        problems.append(
            _problem(
                "config_entries_ignored",
                f"{ignored} allowlist entry/entries are invalid and are ignored",
                "fix or remove them in ICODE_BROWSER_ALLOWED_DOMAINS / ICODE_BROWSER_ALLOWED_ORIGINS",
                "warning",
            )
        )
    return section, cfg, problems


def _browser(cfg: BrowserConfig | None, backend: str) -> tuple[dict[str, Any], list[dict[str, str]]]:
    configured = bool(cfg is not None and cfg.executable)
    executable = cfg.executable if configured else discover_chrome()
    section = {"executable": executable, "configured": configured}
    if executable and configured and not _executable_exists(executable):
        # Attach and Bridge never launch a browser, so a stale path is theirs to ignore.
        if backend in {"attach", "bridge"}:
            return section, []
        return section, [
            _problem(
                "browser_not_found",
                "ICODE_BROWSER_EXECUTABLE names no existing executable",
                "fix ICODE_BROWSER_EXECUTABLE, or unset it to use browser discovery",
            )
        ]
    if executable or backend in {"attach", "bridge"}:
        # Attach uses a browser that is already running; Bridge drives the user's
        # own Chrome through the extension. Neither launches a browser here.
        return section, []
    return section, [
        _problem(
            "browser_not_found",
            "no Chrome, Chromium or Edge executable was found",
            "install one, or set ICODE_BROWSER_EXECUTABLE to its path",
        )
    ]


def _executable_exists(executable: str) -> bool:
    from ._proc import which_trusted

    return Path(executable).expanduser().is_file() or which_trusted(executable) is not None


def _state(cfg: BrowserConfig | None, source: BrowserEnv) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """Best-effort writability: ``os.access`` ignores Windows ACLs, so a denied folder can look writable."""
    state_dir = cfg.state_dir if cfg is not None else source.state_dir()
    exists = state_dir.is_dir()
    probe = state_dir if exists else _existing_ancestor(state_dir)
    writable = probe is not None and os.access(probe, os.W_OK)
    profile = cfg.chrome_profile_dir if cfg is not None else None
    section = {
        "dir": str(state_dir),
        "exists": exists,
        "writable": writable,
        "profile": str(profile) if profile is not None else None,
        "profile_exists": profile.is_dir() if profile is not None else None,
    }
    if writable:
        return section, []
    return section, [
        _problem(
            "state_dir_unwritable",
            f"{state_dir} cannot be written",
            "point ICODE_BROWSER_STATE_DIR at a writable folder; profiles and journals live there",
        )
    ]


def _find_host_executable() -> str | None:
    """Locate ``icode-browser-use-host`` on PATH or beside ``sys.executable`` (uv tool envs)."""
    from ._proc import which_trusted

    found = which_trusted("icode-browser-use-host")
    if found:
        return found
    name = "icode-browser-use-host.exe" if sys.platform == "win32" else "icode-browser-use-host"
    try:
        sibling = Path(sys.executable).resolve().parent / name
    except OSError:
        return None
    return str(sibling) if sibling.is_file() else None


def _bridge(backend: str, connect: bool, source: BrowserEnv) -> tuple[dict[str, Any], list[dict[str, str]]]:
    problems: list[dict[str, str]] = []
    extension_dir = default_extension_install_dir()
    manifest_file = native_manifest_path()
    discovery_file = bridge_file(source.source)
    handshake_file = discovery_file.with_name(f"{discovery_file.stem}.handshake")
    section: dict[str, Any] = {
        "state": "not installed",
        "extension": str(extension_dir),
        "extension_installed": (extension_dir / "manifest.json").is_file(),
        "extension_version": None,
        "manifest": str(manifest_file),
        "manifest_installed": manifest_file.is_file(),
        "host": None,
        "host_executable": _find_host_executable(),
        "registered": [],
        "registered_matches": False,
        "file": str(discovery_file),
        "handshake": {"file": str(handshake_file), "exists": handshake_file.is_file()},
        "discovery": None,
    }
    severity = "error" if backend == "bridge" else "warning"
    plugin_version = distribution.distribution_version() or __version__
    section["plugin_version"] = plugin_version
    if section["extension_installed"]:
        try:
            manifest = json.loads((extension_dir / "manifest.json").read_text(encoding="utf-8"))
            section["extension_version"] = str(manifest.get("version", "")) or None
        except (OSError, ValueError, AttributeError):
            section["extension_version"] = None
        if section["extension_version"] and section["extension_version"] != plugin_version:
            problems.append(
                _problem(
                    "bridge_version_mismatch",
                    f"the unpacked extension is {section['extension_version']} but the installed plugin is {plugin_version}",
                    "re-run: icode-browser-use-bridge install (and restart iCode if the wheel was upgraded)",
                    "warning",
                )
            )
    if section["manifest_installed"]:
        try:
            manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
            host = Path(str(manifest.get("path", "")))
            section["host"] = str(host) if host.is_file() else None
        except (OSError, ValueError, AttributeError):
            section["host"] = None
    registered = [name for name in ("chrome", "chromium", "edge") if _registration(name, manifest_file)]
    section["registered"] = registered
    section["registered_matches"] = bool(registered)
    installed = (
        section["extension_installed"] and section["manifest_installed"] and bool(registered) and bool(section["host"])
    )
    section["state"] = "installed" if installed else "not installed"
    if not installed:
        problems.append(
            _problem(
                "bridge_not_installed",
                "the Bridge is not set up: the unpacked extension, native host manifest or browser "
                "registration is missing",
                "run: icode-browser-use-bridge install (Bridge is optional; Owned and Attach work without it)",
                severity,
            )
        )
    if section["host_executable"] is None:
        problems.append(
            _problem(
                "bridge_host_missing",
                "icode-browser-use-host is not on PATH, so Chrome cannot start the native host",
                "install the icode-browser-use wheel to get icode-browser-use-host; a folder package cannot provide it",
                "warning",
            )
        )
    payload, path = _discovery(discovery_file)
    if payload is None:
        problems.append(
            _problem(
                "bridge_not_running",
                "no live bridge discovery file: the plugin's bridge server is not running",
                "start an iCode turn with ICODE_BROWSER_BRIDGE=1, then reload the browser extension",
                severity,
            )
        )
    else:
        alive = pid_alive(_pid(payload))
        reachable = _reachable(payload.get("port")) if connect and alive else None
        section["discovery"] = {
            "file": str(path),
            "pid": payload.get("pid"),
            "generation": payload.get("generation"),
            "alive": alive,
            "reachable": reachable,
        }
        section["state"] = ("reachable" if reachable else "running") if alive else "stale"
        if not alive:
            problems.append(
                _problem(
                    "bridge_stale",
                    f"{path} names pid {payload.get('pid')}, which is not running",
                    "the discovery file is stale; the next bridge start replaces it",
                    severity,
                )
            )
        elif reachable is False:
            problems.append(
                _problem(
                    "bridge_unreachable",
                    f"the bridge discovery file names port {payload.get('port')}, which refused a connection",
                    "restart the iCode turn that hosts the bridge and reload the extension",
                    severity,
                )
            )
    return section, problems


def _broker(
    cfg: BrowserConfig | None, source: BrowserEnv, backend: str, connect: bool
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    problems: list[dict[str, str]] = []
    state_dir = cfg.state_dir if cfg is not None else source.state_dir()
    command = distribution.broker_command(module_name=_core_package())
    section: dict[str, Any] = {
        "state": "not running",
        "file": str(state_dir / "broker" / "broker.json"),
        "launchable": command is not None,
        "autostart": bool(cfg is not None and cfg.broker_autostart),
        "pid": None,
        "protocol": None,
        "config_matches": None,
        "reachable": None,
    }
    severity = "error" if backend == "broker" else "warning"
    payload, path = _discovery(state_dir / "broker" / "broker.json")
    if payload is None:
        problems.append(
            _problem(
                "broker_not_running",
                "the optional Broker is not running (Owned and Attach tests do not need it)",
                "start icode-browser-use-broker, or set ICODE_BROWSER_BROKER_AUTOSTART=1 from an installed wheel",
                severity,
            )
        )
    else:
        alive = pid_alive(_pid(payload))
        reachable = _reachable(payload.get("port")) if connect and alive else None
        section.update(
            {
                "state": ("reachable" if reachable else "running") if alive else "stale",
                "pid": payload.get("pid"),
                "protocol": payload.get("protocol_version"),
                "reachable": reachable,
                "config_matches": _broker_config_matches(payload, cfg),
            }
        )
        if not alive:
            problems.append(
                _problem(
                    "broker_stale",
                    f"{path} names pid {payload.get('pid')}, which is not running",
                    "start icode-browser-use-broker again; the stale file is replaced on start",
                    severity,
                )
            )
        elif payload.get("protocol_version") != 1:
            problems.append(
                _problem(
                    "broker_config_differs",
                    f"the Broker speaks protocol {payload.get('protocol_version')!r}, this plugin speaks 1",
                    "stop the old broker (icode-browser-use-broker --stop) and start the current one",
                    severity,
                )
            )
        elif section["config_matches"] is False:
            problems.append(
                _problem(
                    "broker_config_differs",
                    "the running Broker was started with different domains, origins or flags",
                    "stop the broker (icode-browser-use-broker --stop) and start it with this configuration",
                    severity,
                )
            )
        elif reachable is False:
            problems.append(
                _problem(
                    "broker_unreachable",
                    f"the Broker discovery file names port {payload.get('port')}, which refused a connection",
                    "restart the Broker; it rewrites its discovery file on start",
                    severity,
                )
            )
    external_broker_live = payload is not None and pid_alive(_pid(payload))
    if (backend == "broker" or section["autostart"]) and command is None:
        # When an externally-started Broker is already live and autostart is off,
        # the client can connect to it without spawning, so report a warning only.
        if external_broker_live and not section["autostart"]:
            problems.append(
                _problem(
                    "broker_unsupported",
                    "this install cannot autostart the Broker, but an external Broker is already running",
                    "keep starting icode-browser-use-broker externally when needed",
                    "warning",
                )
            )
        else:
            problems.append(
                _problem(
                    "broker_unsupported",
                    "this install cannot start the Broker: it is loaded as a folder package or the runtime's "
                    "Python is not exposed as sys.executable",
                    "start icode-browser-use-broker from a Python where the wheel is installed, or use Owned/Attach",
                )
            )
    return section, problems


def _broker_config_matches(payload: dict[str, Any], cfg: BrowserConfig | None) -> bool | None:
    if cfg is None:
        return None
    return (
        payload.get("allowed_domains") == list(cfg.allowed_domains)
        and payload.get("allowed_origins") == list(cfg.allowed_origins)
        and all(
            payload.get(key) == expected
            for key, expected in (
                ("headless", cfg.headless),
                ("bridge", cfg.bridge),
                ("evaluate", cfg.evaluate_enabled),
            )
        )
        and all(
            payload.get(key) == expected
            for key, expected in (
                ("save_state", cfg.save_state_enabled),
                ("chrome_devtools", cfg.chrome_devtools),
                ("cdp_url", cfg.cdp_url or ""),
                ("upload_roots", [str(p) for p in cfg.upload_roots]),
            )
            if key in payload
        )
    )


def _registration(browser: str, manifest_file: Path) -> Path | None:
    path = registered_manifest(browser)
    if not path or not path.is_file():
        return None
    return path if same_manifest(path, manifest_file) else None


def _discovery(path: Path) -> tuple[dict[str, Any] | None, Path]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, path
    return (payload, path) if isinstance(payload, dict) else (None, path)


def _pid(payload: dict[str, Any]) -> int:
    """A discovery file written by an older or broken build must not crash the report."""
    try:
        return int(payload.get("pid") or 0)
    except (TypeError, ValueError):
        return 0


def _reachable(port: Any) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=2):
            return True
    except (OSError, TypeError, ValueError):
        return False


def _existing_ancestor(path: Path) -> Path | None:
    probe = path
    while not probe.exists():
        if probe == probe.parent:
            return None
        probe = probe.parent
    return probe


if __name__ == "__main__":
    raise SystemExit(main())
