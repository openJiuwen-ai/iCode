# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Network policy: per-request allowlist enforcement, Python-side DNS guard, OOPIF arming.

Destination rules are enforced per HTTP(S) request through CDP ``Fetch``; a Python-side
DNS guard rejects public destinations that resolve to private addresses before calling
``Fetch.continueRequest`` (note: Chrome performs its own resolution, so DNS-rebinding
TOCTOU and non-``Fetch`` channels such as WebSockets and Service/Shared Workers are
documented limits, not OS-sandbox guarantees). OOPIF arming freezes each cross-origin
child frame until ``Fetch`` covers it.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import re
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlsplit

from .cdp.client import CDPConnection
from .config import CDP_TIMEOUT
from .errors import ConnectionClosed
from .logging import log

_ALLOWED_SCHEMES = frozenset({"http", "https"})
_DEFAULT_PORTS = {"http": 80, "https": 443}
_ULA_NETWORK = ipaddress.ip_network("fc00::/7")

_FETCH_ENABLE_PARAMS: dict[str, Any] = {
    "patterns": [{"urlPattern": "*", "requestStage": "Request"}],
}
_AUTO_ATTACH_PARAMS: dict[str, Any] = {
    "autoAttach": True,
    "waitForDebuggerOnStart": True,
    "flatten": True,
    "filter": [{"type": "page"}, {"type": "iframe"}],
}
#: Browser-level discovery of popups only: iframes are tracked by the scoped
#: auto-attach on our own page session, so a browser-level iframe filter would
#: create a second session for each frame.
_AUTO_ATTACH_PAGE_PARAMS: dict[str, Any] = {
    "autoAttach": True,
    "waitForDebuggerOnStart": True,
    "flatten": True,
    "filter": [{"type": "page"}],
}
#: Best-effort backstop for the private prefixes `Fetch` might miss; the real
#: decision is per request in `NetworkPolicy.evaluate`, this only catches
#: stragglers.
_BLOCKED_URL_PATTERNS = [
    "*://127.*",
    "*://localhost*",
    "*://10.*",
    "*://192.168.*",
    "*://169.254.*",
]
_BLOCKED_URL_PATTERNS += [f"*://172.{octet}.*" for octet in range(16, 32)]


def scheme_allowed(scheme: str) -> bool:
    """Only ``http``/``https`` are ever allowed (used for navigation pre-checks)."""
    return scheme.strip().lower() in _ALLOWED_SCHEMES


def _normalise_host(host: str) -> str:
    """Lower-case, strip a trailing dot, a port suffix and IPv6 brackets."""
    value = (host or "").strip().lower()
    if not value:
        return ""
    if value.startswith("["):
        end = value.find("]")
        return value[1:end].rstrip(".") if end != -1 else value.rstrip(".")
    if ":" in value:
        name, _, port = value.rpartition(":")
        if port.isdigit():
            value = name
    return value.rstrip(".")


def host_allowed(allowed_domains: tuple[str, ...], host: str) -> bool:
    """Dot-boundary suffix match: ``example.com`` covers ``*.example.com``.

    A bare entry also matches the domain itself; a leading-dot entry matches
    subdomains only.
    """
    candidate = _normalise_host(host)
    if not candidate:
        return False
    for entry in allowed_domains:
        rule = (entry or "").strip().lower().rstrip(".")
        if not rule:
            continue
        if rule.startswith("."):
            bare = rule[1:]
            if bare and candidate.endswith("." + bare):
                return True
        elif candidate == rule or candidate.endswith("." + rule):
            return True
    return False


def is_private_ip(ip: str) -> bool:
    """True when *ip* is loopback/private/link-local/ULA/reserved/multicast/unspecified.

    IPv4-mapped and 6to4 IPv6 addresses are unwrapped before the check. An
    unparseable value is treated as unsafe.
    """
    text = (ip or "").strip()
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1]
    try:
        address = ipaddress.ip_address(text)
    except ValueError:
        return True
    if isinstance(address, ipaddress.IPv6Address):
        embedded = address.ipv4_mapped or address.sixtofour
        if embedded is not None:
            address = embedded
        elif address in _ULA_NETWORK:
            return True
    return not address.is_global or address.is_reserved or address.is_multicast


def canonical_origin(url: str) -> str:
    """Return ``scheme://host:port`` for an http(s) URL, or ``""``.

    The port is always the effective one (``http`` 80, ``https`` 443) so a
    configured origin and a request URL compare the same way. Browsers treat
    ``localhost`` and ``127.0.0.1`` as different origins; so do we.
    """
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return ""
    scheme = (parts.scheme or "").lower()
    if not scheme_allowed(scheme):
        return ""
    host = (parts.hostname or "").lower().rstrip(".")
    if not host:
        return ""
    effective_port = _DEFAULT_PORTS[scheme] if port is None else port
    if effective_port < 1 or effective_port > 65535:
        return ""
    if ":" in host:
        try:
            host = f"[{ipaddress.ip_address(host).compressed}]"
        except ValueError:
            return ""
    if effective_port == _DEFAULT_PORTS[scheme]:
        return f"{scheme}://{host}"
    return f"{scheme}://{host}:{effective_port}"


def sanitize_display_url(url: str) -> str:
    """Origin+path view of a URL with credentials, ``;params``, query and fragment stripped.

    Hierarchical URLs keep ``scheme://host[:port]/path`` (preserving IPv6 brackets and any
    explicit port, never ``user:pass@``); non-hierarchical schemes (``data:``, ``blob:``,
    ``about:``) return only ``scheme:`` so embedded payloads never leak. Callers apply
    their own length limit and truncation suffix.
    """
    raw = url or ""
    try:
        parts = urlsplit(raw)
    except ValueError:  # e.g. an unterminated IPv6 literal in a page-built URL
        return (raw.partition(":")[0] + ":") if ":" in raw else ""
    if not parts.scheme:
        return ""
    if not parts.netloc:
        if parts.scheme.lower() == "about" and parts.path and len(parts.path) <= 32:
            if all(ch.isalnum() or ch in "-_" for ch in parts.path):
                return f"about:{parts.path}"
        return f"{parts.scheme}:"
    host = parts.netloc.rpartition("@")[2]
    path = parts.path.partition(";")[0]
    return f"{parts.scheme}://{host}{path}"


_TITLE_QUERY_SECRET_RE = re.compile(r"[?#][^\s]*=[^\s]*")


def sanitize_display_title(title: str, url: str = "") -> str:
    """Strip query/fragment secrets when a page title falls back to (or embeds) a URL.

    When an HTML page (such as an OAuth callback) has no ``<title>``, Chrome populates
    ``document.title`` / target title with ``host[:port]/path?query#fragment``.
    Sanitizing it alongside :func:`sanitize_display_url` prevents query tokens from
    leaking into snapshot headers or ``browser_tabs``.
    """
    raw = (title or "").strip()
    if not raw:
        return ""
    lower = raw.lower()
    if lower.startswith(("http://", "https://", "data:", "blob:")):
        return sanitize_display_url(raw)
    if url:
        try:
            host = urlsplit(url).netloc.rpartition("@")[2]
        except ValueError:
            host = ""
        if host and lower.startswith(host.lower()) and (len(raw) == len(host) or raw[len(host)] in "/?#"):
            return sanitize_display_url(f"http://{raw}").removeprefix("http://")
    return _TITLE_QUERY_SECRET_RE.sub(lambda match: match.group(0)[0] + "…", raw)


def origin_allowed(allowed_origins: tuple[str, ...], url: str) -> bool:
    """True when *url*'s origin is explicitly authorised for local development."""
    if not allowed_origins:
        return False
    origin = canonical_origin(url)
    return bool(origin) and origin in allowed_origins


def is_loopback_ip(ip: str) -> bool:
    """True only for a loopback address; unparseable input is not loopback."""
    text = (ip or "").strip()
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1]
    try:
        address = ipaddress.ip_address(text)
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address):
        return (address.ipv4_mapped or address).is_loopback
    return address.is_loopback


def _split(url: str) -> tuple[str, str]:
    try:
        parts = urlsplit(url)
    except ValueError:
        return "", ""
    return (parts.scheme or "").lower(), (parts.hostname or "").lower()


def url_host_allowed(url: str, allowed_domains: tuple[str, ...], allowed_origins: tuple[str, ...]) -> bool:
    """Check the destination before DNS; an empty allowlist accepts public hosts."""
    scheme, host = _split(url)
    if not scheme_allowed(scheme) or not host:
        return False
    if origin_allowed(allowed_origins, url):
        return True
    if allowed_domains or allowed_origins:
        return host_allowed(allowed_domains, host)
    host = host.rstrip(".")
    if not host or host == "localhost" or host.endswith(".localhost"):
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return True
    return not is_private_ip(str(address))


def decide(
    url: str,
    allowed_domains: tuple[str, ...],
    ip_lookup: Callable[[str], list[str]],
    *,
    allowed_origins: tuple[str, ...] = (),
) -> bool:
    """Return True to allow *url*, False to block it (fail-closed).

    A public domain must resolve only to public addresses and match any configured allowlist.
    An explicitly authorised loopback origin is allowed on its own, but only
    while the name resolves to loopback addresses alone.
    """
    if not url_host_allowed(url, allowed_domains, allowed_origins):
        return False
    _, host = _split(url)
    addresses = ip_lookup(host)
    if origin_allowed(allowed_origins, url):
        return bool(addresses) and all(is_loopback_ip(ip) for ip in addresses)
    return bool(addresses) and all(not is_private_ip(ip) for ip in addresses)


async def _default_resolver(host: str) -> list[str]:
    """Resolve *host* to a de-duplicated IP list; ``[]`` on failure."""
    try:
        loop = asyncio.get_running_loop()
        infos = await loop.getaddrinfo(host, None)
    except OSError:
        return []
    addresses: list[str] = []
    for info in infos:
        try:
            address = str(info[4][0])
        except (IndexError, TypeError):
            continue
        if address and address not in addresses:
            addresses.append(address)
    return addresses


class NetworkPolicy:
    """Per-request allowlist enforcement plus OOPIF arming for one browser."""

    def __init__(
        self,
        allowed_domains: tuple[str, ...],
        *,
        allowed_origins: tuple[str, ...] = (),
        resolver: Callable[[str], Awaitable[list[str]]] | None = None,
        owns_session: Callable[[str, str], bool] | None = None,
    ) -> None:
        self.allowed_domains = tuple(allowed_domains)
        self.allowed_origins = tuple(allowed_origins)
        # The session answers "is this CDP session the one driving target_id?" so
        # the policy can close an unguardable page it owns without ever closing a
        # user's page (or a duplicate session of a page we already drive).
        self._owns_session = owns_session or (lambda _target, _session: False)
        # The private-range backstop cannot express "except this loopback
        # origin"; with origins configured the per-request decision is the only
        # gate for 127/8 and localhost, so those two patterns are dropped.
        self.blocked_patterns = [
            pattern
            for pattern in _BLOCKED_URL_PATTERNS
            if not (self.allowed_origins and pattern in {"*://127.*", "*://localhost*"})
        ]
        self.blocked = 0
        self.allowed = 0
        self.last_blocked_origin = ""
        self.last_blocked_document_origin = ""
        self.detached_workers = 0
        self.arm_failures = 0
        """Child frames whose interception could not be installed (left paused, fail-closed)."""
        self.arm_degraded = 0
        """Page targets armed with Fetch but whose secondary hardening failed (logged, still guarded)."""
        self.arm_failed_pages = 0
        """Owned page targets closed because their interception could not be installed."""
        self.detached_pages = 0
        """Foreign or duplicate page sessions detached without closing the page."""
        self._resolver = resolver or _default_resolver
        self._attached = False
        self._client: CDPConnection | None = None
        self._armed_sessions: set[str] = set()
        self._arm_lock = asyncio.Lock()

    # -------------------------------------------------------------- handlers

    def attach(self, client: CDPConnection, *, register_attach_handler: bool = True) -> None:
        """Register the event handlers; idempotent.

        When *register_attach_handler* is False, the caller (``BrowserSession``)
        routes ``Target.attachedToTarget`` synchronously through
        :meth:`handle_attached_to_target` after resolving ownership/duplicates so
        the session and policy never race on the same attach event.
        """
        if self._attached:
            return
        self._attached = True
        self._client = client
        client.on("Fetch.requestPaused", self._on_request_paused)
        if register_attach_handler:
            client.on("Target.attachedToTarget", self._on_attached_to_target)
        client.on("Target.detachedFromTarget", self._on_detached_from_target)

    async def _reply(self, method: str, params: dict[str, Any], session_id: str | None) -> None:
        client = self._client
        if client is None:
            return
        try:
            await client.send(method, params, session_id=session_id, timeout=CDP_TIMEOUT)
        except Exception as exc:
            log(f"网络策略命令失败（{method}）：{exc!r}")

    async def _send_quiet(self, method: str, params: dict[str, Any] | None, session_id: str) -> bool:
        client = self._client
        if client is None:
            return False
        try:
            await client.send(method, params, session_id=session_id, timeout=CDP_TIMEOUT)
            return True
        except ConnectionClosed:
            # The browser is going away (teardown, crash); a policy command on a
            # dead connection is expected, not a policy failure worth a warning.
            return False
        except Exception as exc:
            log(f"网络策略命令失败（{method}）：{exc!r}")
            return False

    async def evaluate(self, url: str) -> bool:
        """Allow public hosts under the configured rules and explicit loopback origins."""
        if not url_host_allowed(url, self.allowed_domains, self.allowed_origins):
            return False
        _, host = _split(url)
        try:
            resolved = await self._resolver(host)
        except Exception as exc:
            log(f"DNS 解析异常，拒绝请求：{host}（{exc!r}）")
            return False
        if not resolved:
            log(f"DNS 解析失败或为空，拒绝请求：{host}")
            return False
        return decide(url, self.allowed_domains, lambda _host: resolved, allowed_origins=self.allowed_origins)

    async def _on_request_paused(self, params: dict[str, Any], session_id: str | None) -> None:
        request = params.get("request") or {}
        request_id = str(params.get("requestId", ""))
        url = str(request.get("url", ""))
        if await self.evaluate(url):
            self.allowed += 1
            await self._reply("Fetch.continueRequest", {"requestId": request_id}, session_id)
        else:
            self.blocked += 1
            origin = canonical_origin(url)
            if origin:
                self.last_blocked_origin = origin
                if params.get("resourceType") == "Document":
                    self.last_blocked_document_origin = origin
            await self._reply(
                "Fetch.failRequest",
                {"requestId": request_id, "errorReason": "BlockedByClient"},
                session_id,
            )

    async def _arm_child(self, child_session: str, *, page: bool = False, target_id: str = "") -> None:
        """Cover a child session with Fetch, then release it from the debugger.

        A failed install fails closed (the child stays paused and is never
        released); the attach event arrives once, so one retry runs before the
        frame is written off, and ``arm_failures`` keeps that visible.

        Page targets are different: Chrome answers ``Fetch.enable`` while they
        wait for the debugger but blocks ``Network.enable`` until the renderer
        runs, so the resume must come *before* the secondary commands. Fetch is
        installed first, so the paused-before-first-request guarantee still
        holds. If the page cannot be guarded, the session callback decides
        whether it is ours to close; a user page or a duplicate session is only
        detached (which resumes it).
        """
        if page:
            await self._arm_page(child_session, target_id)
            return
        if await self._install_child(child_session):
            await self._send_quiet("Runtime.runIfWaitingForDebugger", None, child_session)
            return
        client = self._client
        if client is not None and not client.closed and await self._install_child(child_session):
            await self._send_quiet("Runtime.runIfWaitingForDebugger", None, child_session)
            return
        self.arm_failures += 1
        log(f"子框架拦截安装失败，保持暂停（fail-closed）：{child_session}")

    async def _arm_page(self, child_session: str, target_id: str) -> None:
        """Arm a page target: Fetch first, resume, then best-effort hardening."""
        if not await self._send_quiet("Fetch.enable", _FETCH_ENABLE_PARAMS, child_session):
            client = self._client
            if (
                client is None
                or client.closed
                or not await self._send_quiet("Fetch.enable", _FETCH_ENABLE_PARAMS, child_session)
            ):
                await self._fail_page(target_id, child_session, "Fetch.enable")
                return
        # Arm Target.setAutoAttach BEFORE resuming the page so it cannot spawn
        # an uncovered cross-origin iframe in the window before auto-attach lands.
        auto_attach_ok = await self._send_quiet("Target.setAutoAttach", _AUTO_ATTACH_PARAMS, child_session)
        if not auto_attach_ok:
            auto_attach_ok = await self._send_quiet("Target.setAutoAttach", _AUTO_ATTACH_PARAMS, child_session)
        if not auto_attach_ok:
            self.arm_degraded += 1
            log(f"页面附加拦截未安装（fail-closed）：Target.setAutoAttach {child_session}")
            await self._fail_page(target_id, child_session, "Target.setAutoAttach")
            return
        if not await self._send_quiet("Runtime.runIfWaitingForDebugger", None, child_session):
            self.arm_failures += 1
            log(f"页面恢复失败（fail-closed）：{child_session}")
            await self._fail_page(target_id, child_session, "Runtime.runIfWaitingForDebugger")
            return
        for method, params in (
            ("Network.enable", None),
            ("Network.setBlockedURLs", {"urls": self.blocked_patterns}),
        ):
            if await self._send_quiet(method, params, child_session):
                continue
            self.arm_degraded += 1
            log(f"页面附加拦截未安装（仍受 Fetch 拦截）：{method} {child_session}")

    async def _fail_page(self, target_id: str, child_session: str, reason: str) -> None:
        """Close a page this session drives; only detach a duplicate or foreign one."""
        if target_id and self._owns_session(target_id, child_session):
            self.arm_failed_pages += 1
            log(f"页面无法安装网络拦截，已关闭（fail-closed）：{target_id}（{reason}）")
            await self._reply("Target.closeTarget", {"targetId": target_id}, None)
            return
        # A user page, a no-opener page or a second session of a page we already
        # guard: not ours to close. Detaching resumes it.
        self.detached_pages += 1
        await self._reply("Target.detachFromTarget", {"sessionId": child_session}, None)

    async def _install_child(self, child_session: str) -> bool:
        for method, params in (
            ("Fetch.enable", _FETCH_ENABLE_PARAMS),
            ("Network.enable", None),
            ("Network.setBlockedURLs", {"urls": self.blocked_patterns}),
            ("Target.setAutoAttach", _AUTO_ATTACH_PARAMS),
        ):
            if not await self._send_quiet(method, params, child_session):
                # Fail closed on the first failure: the child is never released without it.
                return False
        return True

    async def handle_attached_to_target(self, params: dict[str, Any], session_id: str | None) -> None:
        """Serialised entry point for ``Target.attachedToTarget`` (skips already-armed sessions)."""
        async with self._arm_lock:
            await self._on_attached_to_target_unlocked(params, session_id)

    async def _on_attached_to_target(self, params: dict[str, Any], session_id: str | None) -> None:
        await self.handle_attached_to_target(params, session_id)

    async def _on_attached_to_target_unlocked(self, params: dict[str, Any], session_id: str | None) -> None:
        info = params.get("targetInfo") or {}
        target_type = str(info.get("type", ""))
        child_session = str(params.get("sessionId", ""))
        if not child_session:
            return
        if child_session in self._armed_sessions:
            return
        if str(info.get("url", "")).startswith("chrome-extension://"):
            # Another extension's frame/worker: never intercept or pause it.
            self.detached_workers += 1
            await self._reply("Target.detachFromTarget", {"sessionId": child_session}, None)
            return
        if target_type in {"page", "iframe"}:
            self._armed_sessions.add(child_session)
            await self._arm_child(child_session, page=target_type == "page", target_id=str(info.get("targetId", "")))
        elif target_type in {"service_worker", "shared_worker"}:
            # chrome.debugger worker targets reject Fetch.enable; detach them
            # rather than leave them paused. Dedicated worker requests are still
            # covered by the page's Fetch session.
            self.detached_workers += 1
            await self._reply("Target.detachFromTarget", {"sessionId": child_session}, None)

    async def _on_detached_from_target(self, params: dict[str, Any], session_id: str | None) -> None:
        child_session = str(params.get("sessionId", ""))
        if child_session:
            self._armed_sessions.discard(child_session)
        return None

    # ------------------------------------------------------------- bootstrap

    async def bootstrap(
        self,
        client: CDPConnection,
        *,
        session_id: str,
        scoped: bool = False,
        discover_pages: bool = False,
        download_dir: str | None = None,
    ) -> None:
        """Enable interception on the root session and arm auto-attach.

        *scoped* keeps interception on our own page session: attaching to the
        user's browser must not intercept their other tabs or change browser-wide
        settings. *discover_pages* (Owned mode) additionally watches browser-level
        page targets so popups opened from our page are adopted; it uses a
        page-only filter so each iframe still has exactly one session (the scoped
        one below).

        *download_dir* opts an owned browser into controlled downloads: Chrome
        saves each transfer under its GUID in that folder and emits
        ``Browser.download*`` events, so the session can size-check, rename and
        cancel it. Without it downloads stay denied; scoped (Attach/Bridge) mode
        never changes browser-wide download settings.
        """
        self._armed_sessions.add(session_id)
        await client.send("Fetch.enable", _FETCH_ENABLE_PARAMS, session_id=session_id, timeout=CDP_TIMEOUT)
        await client.send("Network.enable", None, session_id=session_id, timeout=CDP_TIMEOUT)
        await client.send(
            "Network.setBlockedURLs", {"urls": self.blocked_patterns}, session_id=session_id, timeout=CDP_TIMEOUT
        )
        if scoped and not discover_pages:
            with contextlib.suppress(Exception):
                await client.send(
                    "Page.setDownloadBehavior", {"behavior": "deny"}, session_id=session_id, timeout=CDP_TIMEOUT
                )
        elif download_dir is not None:
            await client.send(
                "Browser.setDownloadBehavior",
                {"behavior": "allowAndName", "downloadPath": download_dir, "eventsEnabled": True},
                timeout=CDP_TIMEOUT,
            )
        else:
            await client.send("Browser.setDownloadBehavior", {"behavior": "deny"}, timeout=CDP_TIMEOUT)
        await client.send(
            "Target.setAutoAttach",
            _AUTO_ATTACH_PARAMS,
            session_id=session_id,
            timeout=CDP_TIMEOUT,
        )
        if discover_pages:
            await client.send(
                "Target.setAutoAttach",
                _AUTO_ATTACH_PAGE_PARAMS,
                timeout=CDP_TIMEOUT,
            )


async def install_network_policy(
    client: CDPConnection,
    allowed_domains: tuple[str, ...],
    *,
    allowed_origins: tuple[str, ...] = (),
    session_id: str,
    scoped: bool = False,
    discover_pages: bool = False,
    download_dir: str | None = None,
    owns_session: Callable[[str, str], bool] | None = None,
    route_attach_events: bool = False,
) -> NetworkPolicy:
    """Build, attach and bootstrap a :class:`NetworkPolicy` for *client*.

    Failures propagate (typically ``Unavailable``/``CDPTimeout``) so the session
    can treat an un-armable network policy as a fatal start error.
    """
    policy = NetworkPolicy(
        allowed_domains,
        allowed_origins=allowed_origins,
        owns_session=owns_session,
    )
    policy.attach(client, register_attach_handler=not route_attach_events)
    await policy.bootstrap(
        client,
        session_id=session_id,
        scoped=scoped,
        discover_pages=discover_pages,
        download_dir=download_dir,
    )
    return policy
