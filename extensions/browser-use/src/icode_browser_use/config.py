# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Configuration read from ``ICODE_BROWSER_*`` process environment variables.

This module is the single source of truth for the numeric constants in the
design's constant table (A.4); every other module imports them from here rather
than repeating magic numbers.
"""

from __future__ import annotations

import ipaddress
import math
import os
import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final
from urllib.parse import urlsplit

# --- numeric constants (single source; design A.4) --------------------------
SNAPSHOT_CHAR_BUDGET: Final = 7000
MAX_STAGE_NODES: Final = 120
EVALUATE_TIMEOUT: Final = 10.0
CDP_TIMEOUT: Final = 20.0
NAV_LOAD_TIMEOUT: Final = 20.0
CHROME_READY_TIMEOUT: Final = 25.0
MAX_TIMEOUTS_BEFORE_RECYCLE: Final = 2
AMBIGUITY_GAP: Final = 25
MIN_TOP_SCORE: Final = 55
WS_PING_INTERVAL: Final = 15.0
LOGIN_WAIT_MAX: Final = 900.0
LOCK_WAIT: Final = 10.0
LINE_LIMIT: Final = 1 << 20
IDLE_CLOSE_SECONDS: Final = 600.0
ACTION_WINDOW_SECONDS: Final = 600.0
MAX_ACTIONS_PER_WINDOW: Final = 100
LIFETIME_MAX_ACTIONS: Final = 500
MAX_IMAGE_BYTES: Final = 2_500_000
SCREENSHOT_KEEP: Final = 20
"""Screenshots kept per extension data folder; older ones are removed by the plugin itself."""
DIALOG_TIMEOUT: Final = 10.0
"""Seconds a JavaScript dialog is held for ``browser_dialog`` before it is dismissed.

Headless runs have no one to click it, so a dialog always ends on its own and the
reason is reported instead of waiting forever.
"""
DOWNLOAD_MAX_MB: Final = 50
"""Largest download kept, in mebibytes; bigger transfers are canceled.

Chrome reports ``totalBytes``/``receivedBytes`` while it writes, so the limit is
enforced during the transfer and checked again before the file is renamed.
"""

DEFAULT_STATE_DIRNAME: Final = "icode-browser-use"
CHROME_LOGIN_PREFERENCE: Final = "reuse-chrome-login"
CHROME_DEVTOOLS_PREFERENCE: Final = "reuse-chrome-devtools"
VALID_TOOLSETS: Final = frozenset({"full", "compact"})

#: Allowlist entries are a bare domain or a leading-dot subdomain-only entry.
#: IP literals are rejected (they cannot be expressed as an allowlist entry).
_DOMAIN_RE = re.compile(r"^\.?[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+$")


class ConfigError(RuntimeError):
    """The environment cannot produce a usable configuration (fail-closed)."""


def is_valid_domain(entry: str) -> bool:
    """Return True when *entry* is a usable allowlist domain."""
    candidate = entry.strip().lower().rstrip(".")
    if not candidate or len(candidate) > 253:
        return False
    if ":" in candidate or "/" in candidate:
        return False
    if candidate.replace(".", "").isdigit():
        return False
    return bool(_DOMAIN_RE.match(candidate))


_DEFAULT_PORTS: Final = {"http": 80, "https": 443}


def _loopback_host(host: str) -> bool:
    """True for ``localhost``/``*.localhost`` or a loopback IP literal."""
    if not host:
        return False
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    mapped = address.ipv4_mapped if isinstance(address, ipaddress.IPv6Address) else None
    return (mapped or address).is_loopback


def normalise_origin(entry: str) -> str | None:
    """Return ``scheme://host:port`` for an allowed-origin entry, else ``None``.

    Only loopback hosts are accepted: the runtime decision additionally proves
    the name resolves to loopback, so a public name can never become a private
    network grant. Userinfo, paths, query and fragment are rejected.
    """
    text = (entry or "").strip()
    if not text or any(ord(char) < 32 or ord(char) == 127 for char in text):
        return None
    try:
        parts = urlsplit(text)
        port = parts.port
    except ValueError:
        return None
    scheme = (parts.scheme or "").lower()
    if scheme not in _DEFAULT_PORTS:
        return None
    if parts.username is not None or parts.password is not None:
        return None
    if parts.query or parts.fragment or parts.path not in ("", "/"):
        return None
    host = (parts.hostname or "").lower().rstrip(".")
    if not _loopback_host(host):
        return None
    effective_port = _DEFAULT_PORTS[scheme] if port is None else port
    if effective_port < 1 or effective_port > 65535:
        return None
    if ":" in host:
        host = f"[{host}]"
    return f"{scheme}://{host}:{effective_port}"


def parse_origins(raw: str | None) -> tuple[tuple[str, ...], list[str]]:
    """Split a comma-separated allowed-origin list into ``(accepted, rejected)``."""
    accepted: list[str] = []
    rejected: list[str] = []
    for part in (raw or "").split(","):
        item = part.strip()
        if not item:
            continue
        normalised = normalise_origin(item)
        if normalised is None:
            rejected.append(item)
        elif normalised not in accepted:
            accepted.append(normalised)
    return tuple(accepted), rejected


def parse_domains(raw: str | None) -> tuple[tuple[str, ...], list[str]]:
    """Split a comma-separated allowlist into ``(accepted, rejected)`` entries."""
    accepted: list[str] = []
    rejected: list[str] = []
    for part in (raw or "").split(","):
        item = part.strip()
        if not item:
            continue
        if is_valid_domain(item):
            normalised = item.lower().rstrip(".")
            if normalised not in accepted:
                accepted.append(normalised)
        else:
            rejected.append(item)
    return tuple(accepted), rejected


class BrowserEnv:
    """Lightweight alias resolution, independent of runtime allowlist validation."""

    def __init__(self, env: Mapping[str, str] | None = None) -> None:
        self.source = os.environ if env is None else env

    def get(self, name: str, default: str | None = None) -> str | None:
        suffix = name.removeprefix("ICODE_BROWSER_")
        return self.source.get(f"ICODE_BROWSER_{suffix}", default)

    def enabled(self, name: str) -> bool:
        return (self.get(name) or "").strip().lower() == "allow"

    def state_dir(self) -> Path:
        override = self.get("STATE_DIR")
        return Path(override).expanduser() if override else Path.home() / ".cache" / DEFAULT_STATE_DIRNAME


def _default_state_dir() -> Path:
    return BrowserEnv().state_dir()


def _as_bool(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class BrowserConfig:
    """Resolved runtime configuration (design section 11.5)."""

    executable: str | None = None
    headless: bool = False
    cdp_url: str | None = None
    """Attach to this browser's CDP endpoint instead of launching a dedicated Chrome."""
    chrome_devtools: bool = False
    """Discover Chrome's user-approved DevTools endpoint on each connection."""
    bridge: bool = False
    """Connect through the extension bridge instead of a CDP endpoint."""
    bridge_file: str | None = None
    """Where the bridge server writes its port/token (default: ``state_dir/bridge.json``)."""
    bridge_wait: float = 35.0
    """Seconds to wait for the extension host; covers the 30s service-worker alarm."""
    prewarm: bool = False
    """Start the browser/bridge at plugin startup instead of on the first tool call."""
    allowed_domains: tuple[str, ...] = ()
    allowed_origins: tuple[str, ...] = ()
    """Explicit loopback origins (``scheme://host:port``) for local development."""
    state_dir: Path = field(default_factory=_default_state_dir)
    state_archive_root: Path | None = None
    """Explicit directory for ``browser_save_state`` archives; defaults to ``state_dir / 'state'``."""
    evaluate_enabled: bool = False
    save_state_enabled: bool = False
    max_actions_per_window: int = MAX_ACTIONS_PER_WINDOW
    lifetime_max_actions: int = LIFETIME_MAX_ACTIONS
    snapshot_char_budget: int = SNAPSHOT_CHAR_BUDGET
    max_stage_nodes: int = MAX_STAGE_NODES
    evaluate_timeout: float = EVALUATE_TIMEOUT
    cdp_timeout: float = CDP_TIMEOUT
    nav_load_timeout: float = NAV_LOAD_TIMEOUT
    ready_timeout: float = CHROME_READY_TIMEOUT
    idle_close_seconds: float = IDLE_CLOSE_SECONDS
    broker: bool = False
    """Use the persistent local Broker instead of this process's BrowserSession."""
    broker_autostart: bool = False
    """Spawn the Broker from the plugin process; off by default so it is not killed by
    the plugin job."""
    broker_owner: str = "default"
    """Stable owner id used to reacquire the same session after a plugin rebuild."""
    browser_session_id: str = "default"
    """Stable id; the Broker keeps this BrowserSession alive across plugin restarts."""
    broker_lease_ttl: float = 300.0
    """Seconds without activity after which a Broker lease is reclaimed."""
    broker_idle_exit: float = 0.0
    """Seconds with no clients/leases/sessions before the Broker exits; 0 disables."""
    upload_roots: tuple[Path, ...] = ()
    """Directories browser_upload may read from; empty disables uploads."""
    screenshot_keep: int = SCREENSHOT_KEEP
    """Screenshots kept in the extension's own screenshots folder; older ones are pruned."""
    dialog_timeout: float = DIALOG_TIMEOUT
    """Seconds an open JavaScript dialog waits for ``browser_dialog`` before it is dismissed."""
    download_dir: Path | None = None
    """Where owned-browser downloads are saved; ``None`` keeps downloads denied."""
    download_max_bytes: int = DOWNLOAD_MAX_MB * 1024 * 1024
    """Largest download kept; a transfer that reaches it is canceled."""
    toolset: str = "full"
    """Exposed tool surface: ``full`` (all tools) or ``compact`` (12 core tools)."""

    @property
    def backend(self) -> str:
        """Which transport this configuration selects: Broker, Bridge, Attach or Owned."""
        if self.broker:
            return "broker"
        if self.bridge:
            return "bridge"
        if self.cdp_url or self.chrome_devtools:
            return "attach"
        return "owned"

    @property
    def chrome_profile_dir(self) -> Path:
        """Dedicated, persistent Chromium profile directory (never the default one)."""
        return self.state_dir / "chrome"

    @property
    def public_web(self) -> bool:
        """Whether no explicit destination allowlist restricts public HTTP(S)."""
        return not self.allowed_domains and not self.allowed_origins

    @property
    def state_archive_dir(self) -> Path:
        """Directory holding named ``browser_save_state`` archives."""
        return self.state_archive_root if self.state_archive_root is not None else self.state_dir / "state"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> BrowserConfig:
        """Build from ``ICODE_BROWSER_*``.

        Raise :class:`ConfigError` when configuration is invalid.

        Blank settings use public-web mode. An invalid, explicitly configured
        allowlist must never silently fall back to that mode.
        """
        source = BrowserEnv(env)
        domains_setting = source.get("ALLOWED_DOMAINS")
        origins_setting = source.get("ALLOWED_ORIGINS")
        accepted, rejected = parse_domains(domains_setting)
        origins, rejected_origins = parse_origins(origins_setting)
        has_allowlist = (domains_setting or "").strip() or (origins_setting or "").strip()
        if has_allowlist and not (accepted or origins):
            raise ConfigError(
                "ICODE_BROWSER_ALLOWED_DOMAINS / ICODE_BROWSER_ALLOWED_ORIGINS must contain "
                "at least one valid entry when configured."
            )
        if rejected:
            print(
                f"icode-browser-use: ignoring {len(rejected)} invalid allowlist entries in ICODE_BROWSER_ALLOWED_DOMAINS",
                file=sys.stderr,
            )
        if rejected_origins:
            print(
                f"icode-browser-use: ignoring {len(rejected_origins)} invalid origin entries in "
                "ICODE_BROWSER_ALLOWED_ORIGINS; "
                "only http(s) loopback origins such as http://localhost:3000 are supported",
                file=sys.stderr,
            )
        executable = (source.get("EXECUTABLE") or "").strip() or None
        cdp_url = (source.get("CDP_URL") or "").strip() or None
        bridge_file = (source.get("BRIDGE_FILE") or "").strip() or None
        try:
            bridge_wait = float(source.get("BRIDGE_WAIT") or 35.0)
        except ValueError as exc:
            raise ConfigError("ICODE_BROWSER_BRIDGE_WAIT must be a number of seconds") from exc
        if not math.isfinite(bridge_wait) or bridge_wait <= 0:
            raise ConfigError("ICODE_BROWSER_BRIDGE_WAIT must be positive")
        try:
            idle_close = float(source.get("IDLE_CLOSE_SECONDS") or IDLE_CLOSE_SECONDS)
        except ValueError as exc:
            raise ConfigError("ICODE_BROWSER_IDLE_CLOSE_SECONDS must be a number of seconds") from exc
        if not math.isfinite(idle_close) or idle_close < 0:
            raise ConfigError("ICODE_BROWSER_IDLE_CLOSE_SECONDS must be zero or positive")
        try:
            screenshot_keep = int(source.get("SCREENSHOT_KEEP") or SCREENSHOT_KEEP)
        except ValueError as exc:
            raise ConfigError("ICODE_BROWSER_SCREENSHOT_KEEP must be a whole number") from exc
        if screenshot_keep < 1:
            raise ConfigError("ICODE_BROWSER_SCREENSHOT_KEEP must be at least 1")
        try:
            dialog_timeout = float(source.get("DIALOG_TIMEOUT") or DIALOG_TIMEOUT)
        except ValueError as exc:
            raise ConfigError("ICODE_BROWSER_DIALOG_TIMEOUT must be a number of seconds") from exc
        if not math.isfinite(dialog_timeout) or dialog_timeout <= 0:
            raise ConfigError(
                "ICODE_BROWSER_DIALOG_TIMEOUT must be positive; headless runs must never wait for a dialog indefinitely"
            )
        try:
            download_max_mb = int(source.get("DOWNLOAD_MAX_MB") or DOWNLOAD_MAX_MB)
        except ValueError as exc:
            raise ConfigError("ICODE_BROWSER_DOWNLOAD_MAX_MB must be a whole number") from exc
        if download_max_mb < 1:
            raise ConfigError("ICODE_BROWSER_DOWNLOAD_MAX_MB must be at least 1")
        try:
            broker_lease_ttl = float(source.get("LEASE_TTL") or 300.0)
        except ValueError as exc:
            raise ConfigError("ICODE_BROWSER_LEASE_TTL must be a number of seconds") from exc
        if not math.isfinite(broker_lease_ttl) or broker_lease_ttl < 0:
            raise ConfigError("ICODE_BROWSER_LEASE_TTL must be zero or positive")
        try:
            broker_idle_exit = float(source.get("BROKER_IDLE_EXIT") or 0.0)
        except ValueError as exc:
            raise ConfigError("ICODE_BROWSER_BROKER_IDLE_EXIT must be a number of seconds") from exc
        if not math.isfinite(broker_idle_exit) or broker_idle_exit < 0:
            raise ConfigError("ICODE_BROWSER_BROKER_IDLE_EXIT must be zero or positive")
        toolset = (source.get("TOOLSET") or "full").strip().lower() or "full"
        if toolset not in VALID_TOOLSETS:
            raise ConfigError("ICODE_BROWSER_TOOLSET must be 'full' or 'compact'")
        state_dir = source.state_dir()
        broker = _as_bool(source.get("BROKER"))
        bridge_setting = source.get("BRIDGE")
        bridge = (
            _as_bool(bridge_setting)
            if bridge_setting is not None
            else not (cdp_url or broker) and (state_dir / CHROME_LOGIN_PREFERENCE).is_file()
        )
        chrome_devtools = (
            bridge_setting is None
            and not (cdp_url or broker or bridge)
            and (state_dir / CHROME_DEVTOOLS_PREFERENCE).is_file()
        )
        download_dir: Path | None = None
        if source.enabled("DOWNLOADS"):
            override = (source.get("DOWNLOAD_DIR") or "").strip()
            download_dir = Path(override).expanduser() if override else state_dir / "downloads"
        return cls(
            executable=executable,
            headless=_as_bool(source.get("HEADLESS")),
            cdp_url=cdp_url,
            chrome_devtools=chrome_devtools,
            bridge=bridge,
            bridge_file=bridge_file,
            bridge_wait=bridge_wait,
            prewarm=_as_bool(source.get("PREWARM")),
            allowed_domains=accepted,
            allowed_origins=origins,
            state_dir=state_dir,
            evaluate_enabled=source.enabled("EVALUATE"),
            save_state_enabled=source.enabled("SAVE_STATE"),
            idle_close_seconds=idle_close,
            broker=broker,
            broker_autostart=_as_bool(source.get("BROKER_AUTOSTART")),
            broker_owner=(source.get("OWNER_ID") or "default").strip() or "default",
            browser_session_id=(source.get("BROWSER_SESSION_ID") or "default").strip() or "default",
            broker_lease_ttl=broker_lease_ttl,
            broker_idle_exit=broker_idle_exit,
            upload_roots=tuple(
                Path(item).expanduser() for item in (source.get("UPLOAD_ROOTS") or "").split(os.pathsep) if item.strip()
            ),
            screenshot_keep=screenshot_keep,
            dialog_timeout=dialog_timeout,
            download_dir=download_dir,
            download_max_bytes=download_max_mb * 1024 * 1024,
            toolset=toolset,
        )
