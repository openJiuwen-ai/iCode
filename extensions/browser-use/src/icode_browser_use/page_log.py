# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Bounded console and network log of the pages a handle owns.

Fed by CDP events the page session already receives (``Runtime`` and
``Network`` are enabled for every driven page), so collecting costs no extra
command. Only what helps diagnose a page is kept: console messages, uncaught
exceptions, API (XHR/fetch) responses, HTTP errors and failed requests.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections import deque
from dataclasses import dataclass
from typing import Any

from .dialog import redact_message
from .policy import sanitize_display_url
from .snapshot.serializer import sanitize_text

#: Entries kept per handle (all tabs together, newest last).
MAX_ENTRIES = 200
#: Request id -> (method, url) remembered until its response or failure.
_MAX_TRACKED_REQUESTS = 500
#: Characters kept per stored URL.
_MAX_URL = 300
#: Raw characters kept per entry; recording does nothing else with page text, the
#: (bounded) redaction runs only for the entries browser_logs actually shows.
_MAX_RAW_TEXT = 1000
#: Maximum headers stored per request/response direction.
_MAX_HEADERS = 40
#: Maximum raw characters stored per header value.
_MAX_HEADER_VALUE = 512
#: Maximum raw characters stored for an XHR/fetch POST request body.
_MAX_RAW_POST_DATA = 16_384
#: Maximum raw characters scanned by :func:`scrub_payload`.
MAX_PAYLOAD_SCAN = 200_000

_ERROR_TYPES = frozenset({"error", "assert"})
_API_TYPES = frozenset({"XHR", "Fetch"})
_SENSITIVE_HEADER_RE = re.compile(
    r"(authorization|cookie|token|secret|api[-_]?key|auth|credential|session|signature|csrf|xsrf|password|passwd)",
    re.IGNORECASE,
)

#: A URL query/fragment (absolute or relative). Masked when it carries a ``=`` value
#: or a long opaque token (>= 16 chars after ?/#); a stack frame's trailing ``:line:col`` is kept.
_QUERY_RE = re.compile(r"[?#][^\s\"'<>()]*")
_POSITION_RE = re.compile(r"(?::\d+){1,2}$")
#: ``user:password@`` or a bare ``token@`` in a URL (matches up to the last ``@`` in authority).
_USERINFO_RE = re.compile(r"\b((?:https?|wss?|ftp)://)[^/?#\s]+@", re.IGNORECASE)
#: Multi-cookie headers/assignments (``Cookie: a="1"; b=2`` or ``cookie=a=1; b=2``):
#: mask the entire value list when the value is not itself a JSON-quoted string or object.
_COOKIE_HEADER_RE = re.compile(
    r"(\b(?:set-)?cookie)\s*([:=])\s*(?![\\]?[\"']|\{)[^\r\n{}]+",
    re.IGNORECASE,
)
#: ``Authorization: <AnyScheme> <credential>`` (handles Api-Key, NTLM, Negotiate, Bot, OAuth, etc.).
_AUTH_HEADER_RE = re.compile(
    r"\b(authorization)\s*[:=]\s*[A-Za-z][A-Za-z0-9._-]{1,24}\s+[^\s,;&\"'}\\]+",
    re.IGNORECASE,
)
#: A secret word (including suffixed compound names like ``secret_key``/``private_key``/``authCode``)
#: and its value: ``access_token=x``, ``"password": "x \"y\""``, ``{\"token\":\"x\"}``,
#: ``Authorization: Bearer x``. Supports JSON backslash escapes without backtracking.
_SECRET_RE = re.compile(
    r"((?:token|secret|password|passwd|api[_-]?key|access[_-]?key|private[_-]?key"
    r"|authorization|cookie|signature|credential|session[_-]?id|jwt|csrf|xsrf)[A-Za-z0-9_-]{0,24}"
    r"|\bsid\b|\bpwd\b|\bauth(?:[_-]?(?:code|key|token|header|secret|info))?\b"
    r"|\bsession(?:[_-]?(?:id|key|token))?\b)"
    r"\\?[\"']?\s*[:=]\s*(?:\\?\"(?:\\.|[^\"\\])*\\?\"|'(?:\\.|[^'\\])*'"
    r"|(?:(?:bearer|basic|token|api[-_]?key|digest|ntlm|negotiate|oauth|bot)\s+)?[^\s,;&\"'}\\]+)",
    re.IGNORECASE,
)
#: Digest auth parameters (``Digest username="alice", response="..."``): mask the whole parameter list.
_DIGEST_AUTH_RE = re.compile(r"\b(digest)\s+[A-Za-z0-9_-]+\s*=[^\r\n{}]+", re.IGNORECASE)
#: Standalone auth credentials (``Bearer eyJ…``, ``Basic dXNlcjpwYXNz``, ``ApiKey k123``, ``Token tok_123``).
_AUTH_SCHEME_RE = re.compile(r"\b(bearer|basic|api[-_]?key|token)\s+([\w.~+/=-]{6,})", re.IGNORECASE)
#: A bare JWT token anywhere in text (including unsigned/empty-signature ``eyJ... . eyJ... .``).
_BARE_JWT_RE = re.compile(r"\beyJ[\w-]{4,}\.[\w-]{4,}\.(?:[\w-]{4,}\b)?")


def _mask_auth_scheme(match: re.Match[str]) -> str:
    scheme, value = match.group(1), match.group(2)
    if scheme.lower() == "token" and value.isalpha() and len(value) < 16:
        return match.group(0)
    return f"{scheme} ***"


def _mask_query(match: re.Match[str]) -> str:
    query = match.group(0)
    position = _POSITION_RE.search(query)
    body = query[: position.start()] if position else query
    if "=" not in body and len(body) < 17:
        return query
    return (body[0] + "…") + (position.group(0) if position else "")


def _short_url(url: str) -> str:
    """Origin+path of a hierarchical URL (no userinfo, ``;params`` or query; bounded); else its scheme."""
    return sanitize_display_url(url)[:_MAX_URL]


def _redact_secrets(text: str) -> str:
    """Apply linear-time credential/query redaction without collapsing newlines."""
    text = _QUERY_RE.sub(_mask_query, text)
    text = _USERINFO_RE.sub(r"\1", text)
    text = _DIGEST_AUTH_RE.sub(lambda match: f"{match.group(1)} ***", text)
    text = _COOKIE_HEADER_RE.sub(
        lambda match: f"{match.group(1)}: ***" if match.group(2) == ":" else f"{match.group(1)}=***",
        text,
    )
    text = _AUTH_HEADER_RE.sub(lambda match: f"{match.group(1)}=***", text)
    text = _SECRET_RE.sub(lambda match: f"{match.group(1)}=***", text)
    text = _AUTH_SCHEME_RE.sub(_mask_auth_scheme, text)
    text = _BARE_JWT_RE.sub("***", text)
    return text


def scrub(text: str) -> str:
    """Model-visible form of a stored entry text: query values and secrets masked, then
    the shared redaction (whitespace collapsed, truncated). Linear in the bounded input."""
    return redact_message(_redact_secrets(text[:_MAX_RAW_TEXT]), _already_redacted=True)


def scrub_payload(text: str, *, max_raw: int = MAX_PAYLOAD_SCAN) -> str:
    """Scrub a multiline network request/response payload while preserving line breaks."""
    if not text:
        return ""
    cleaned = sanitize_text(text[:max_raw])
    return _redact_secrets(cleaned)


def _capture_headers(raw: Any) -> tuple[tuple[str, str], ...]:
    """Copy a bounded snapshot of CDP header map ``{name: value}``."""
    if not isinstance(raw, dict):
        return ()
    out: list[tuple[str, str]] = []
    for key, value in raw.items():
        if len(out) >= _MAX_HEADERS:
            break
        name = str(key or "").strip()[:120]
        if not name:
            continue
        out.append((name, str(value if value is not None else "")[:_MAX_HEADER_VALUE]))
    return tuple(out)


def scrub_headers(headers: tuple[tuple[str, str], ...]) -> list[tuple[str, str]]:
    """Redact sensitive header values unconditionally and scrub all remaining header values."""
    result: list[tuple[str, str]] = []
    for name, value in headers:
        clean_name = " ".join(sanitize_text(name).split())
        if not clean_name:
            continue
        if _SENSITIVE_HEADER_RE.search(clean_name):
            result.append((clean_name, "***"))
            continue
        clean_value = " ".join(scrub_payload(value, max_raw=_MAX_HEADER_VALUE).split())
        result.append((clean_name, clean_value))
    return result


@dataclass(frozen=True)
class _RequestMeta:
    """Supplementary request metadata held while a request is in flight."""

    headers: tuple[tuple[str, str], ...] = ()
    post_data: str = ""
    has_post_data: bool = False
    cdp_session_id: str | None = None


@dataclass(frozen=True)
class LogEntry:
    """One console message or network outcome."""

    seq: int
    target_id: str
    kind: str  # "console" | "network"
    level: str  # "error" | "warning" | "info"
    text: str
    url: str = ""
    request_id: str = ""
    cdp_session_id: str | None = None
    method: str = ""
    status: int = 0
    mime_type: str = ""
    resource_type: str = ""
    request_headers: tuple[tuple[str, str], ...] = ()
    response_headers: tuple[tuple[str, str], ...] = ()
    request_post_data: str = ""
    has_post_data: bool = False


def _arg_text(arg: dict[str, Any]) -> str:
    if "value" in arg:
        value = arg["value"]
        return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return str(arg.get("unserializableValue") or arg.get("description") or f"[{arg.get('type', '?')}]")


class PageLog:
    """Ring buffer of :class:`LogEntry` plus the request bookkeeping it needs."""

    def __init__(self) -> None:
        self._entries: deque[LogEntry] = deque(maxlen=MAX_ENTRIES)
        self._requests: dict[tuple[str, str], tuple[str, str]] = {}
        self._request_meta: dict[tuple[str, str], _RequestMeta] = {}
        self._finished_order: deque[tuple[str, str]] = deque()
        self._finished_set: set[tuple[str, str]] = set()
        self._finish_waiters: dict[tuple[str, str], list[asyncio.Event]] = {}
        self._seq = 0
        #: Why nothing is being collected for this browser ("" when collecting).
        self.disabled_reason = ""

    @property
    def seq(self) -> int:
        """Sequence number of the newest entry (0 when none was recorded)."""
        return self._seq

    def clear(self) -> None:
        """Forget every entry of a browser that went away; ``seq`` keeps counting so
        a cursor handed out earlier never matches a later entry."""
        self._entries.clear()
        self._requests.clear()
        self._request_meta.clear()
        self._finished_order.clear()
        self._finished_set.clear()
        for events in self._finish_waiters.values():
            for event in events:
                event.set()
        self._finish_waiters.clear()

    @property
    def oldest_seq(self) -> int:
        """Sequence number of the oldest entry still kept (``seq + 1`` when empty)."""
        return self._entries[0].seq if self._entries else self._seq + 1

    def _add(
        self,
        target_id: str,
        kind: str,
        level: str,
        text: str,
        url: str = "",
        **network_fields: Any,
    ) -> None:
        self._seq += 1
        self._entries.append(LogEntry(self._seq, target_id, kind, level, text[:_MAX_RAW_TEXT], url, **network_fields))

    def console(self, target_id: str, params: dict[str, Any], _session_id: str | None = None) -> None:
        kind = str(params.get("type") or "log")
        level = "error" if kind in _ERROR_TYPES else "warning" if kind == "warning" else "info"
        parts: list[str] = []
        size = 0
        for arg in params.get("args") or []:
            if isinstance(arg, dict) and size < _MAX_RAW_TEXT:  # a page can pass thousands of arguments
                part = _arg_text(arg)[:_MAX_RAW_TEXT]
                parts.append(part)
                size += len(part) + 1
        self._add(target_id, "console", level, f"console.{kind}: {' '.join(parts)}")

    def exception(self, target_id: str, params: dict[str, Any], _session_id: str | None = None) -> None:
        details = params.get("exceptionDetails") or {}
        exception = details.get("exception") or {}
        text = str(exception.get("description") or details.get("text") or "exception")[:_MAX_RAW_TEXT]
        # Keep the message and the top of the stack; deep frames are noise.
        self._add(target_id, "console", "error", "uncaught: " + " | ".join(text.splitlines()[:4]))

    def request(self, target_id: str, params: dict[str, Any], session_id: str | None = None) -> None:
        request = params.get("request") or {}
        request_id = str(params.get("requestId", ""))
        key = (target_id, request_id)
        self._requests.pop(key, None)  # a redirect reuses the id: keep the newest hop last
        self._request_meta.pop(key, None)
        self._requests[key] = (str(request.get("method") or "GET")[:32], _short_url(str(request.get("url") or "")))
        raw_post = request.get("postData")
        post_data = str(raw_post)[:_MAX_RAW_POST_DATA] if isinstance(raw_post, str) else ""
        has_post = bool(request.get("hasPostData") or post_data)
        self._request_meta[key] = _RequestMeta(
            headers=_capture_headers(request.get("headers")),
            post_data=post_data,
            has_post_data=has_post,
            cdp_session_id=session_id,
        )
        if len(self._requests) > _MAX_TRACKED_REQUESTS:
            oldest = next(iter(self._requests))
            del self._requests[oldest]
            self._request_meta.pop(oldest, None)

    def response(self, target_id: str, params: dict[str, Any], session_id: str | None = None) -> None:
        response = params.get("response") or {}
        if params.get("type") == "Document" and not response.get("url"):
            return  # the Bridge's status-only copy; the full response (if allowed) follows
        # The request is answered: it leaves the in-flight table whether or not it is
        # logged, so finished requests never push out a slow one still pending.
        request_id = str(params.get("requestId", ""))
        key = (target_id, request_id)
        method, url = self._requests.pop(key, ("", ""))
        meta = self._request_meta.pop(key, _RequestMeta())
        status = int(response.get("status") or 0)
        resource_type = str(params.get("type") or "")
        if status < 400 and resource_type not in _API_TYPES:
            return
        url = _short_url(str(response.get("url") or "")) or url
        level = "error" if status >= 500 else "warning" if status >= 400 else "info"
        self._add(
            target_id,
            "network",
            level,
            f"{method or '?'} {status}",
            url,
            request_id=request_id,
            cdp_session_id=session_id or meta.cdp_session_id,
            method=method or "?",
            status=status,
            mime_type=str(response.get("mimeType") or "")[:120],
            resource_type=resource_type[:40],
            request_headers=meta.headers,
            response_headers=_capture_headers(response.get("headers")),
            request_post_data=meta.post_data,
            has_post_data=meta.has_post_data,
        )

    def failed(self, target_id: str, params: dict[str, Any], session_id: str | None = None) -> None:
        request_id = str(params.get("requestId", ""))
        key = (target_id, request_id)
        method, url = self._requests.pop(key, ("", ""))
        meta = self._request_meta.pop(key, _RequestMeta())
        self._mark_finished(key)
        if params.get("canceled") and not params.get("blockedReason"):
            return  # navigating away cancels in-flight loads; that is not a failure
        reason = str(params.get("blockedReason") or params.get("errorText") or "failed")
        if reason == "inspector":  # Fetch.failRequest from the plugin's network policy
            reason = "blocked by the allow-list (ICODE_BROWSER_ALLOWED_DOMAINS/ORIGINS)"
        self._add(
            target_id,
            "network",
            "error",
            f"{method or '?'} FAILED {reason}",
            url,
            request_id=request_id,
            cdp_session_id=session_id or meta.cdp_session_id,
            method=method or "?",
            status=0,
            resource_type=str(params.get("type") or "")[:40],
            request_headers=meta.headers,
            request_post_data=meta.post_data,
            has_post_data=meta.has_post_data,
        )

    def finished(self, target_id: str, params: dict[str, Any], _session_id: str | None = None) -> None:
        """Record ``Network.loadingFinished`` so body fetches can await slow responses."""
        request_id = str(params.get("requestId", ""))
        if not request_id:
            return
        self._mark_finished((target_id, request_id))

    def _mark_finished(self, key: tuple[str, str]) -> None:
        if key not in self._finished_set:
            self._finished_set.add(key)
            self._finished_order.append(key)
            if len(self._finished_order) > _MAX_TRACKED_REQUESTS:
                evicted = self._finished_order.popleft()
                self._finished_set.discard(evicted)
        waiters = self._finish_waiters.pop(key, ())
        for event in waiters:
            event.set()

    def is_finished(self, target_id: str, request_id: str) -> bool:
        """Whether ``Network.loadingFinished`` (or failure) has been observed for *request_id*."""
        return (target_id, request_id) in self._finished_set

    async def wait_finished(self, target_id: str, request_id: str, *, timeout: float = 1.0) -> bool:
        """Wait briefly for ``Network.loadingFinished`` when a response body is still streaming."""
        key = (target_id, request_id)
        if key in self._finished_set:
            return True
        event = asyncio.Event()
        self._finish_waiters.setdefault(key, []).append(event)
        try:
            await asyncio.wait_for(event.wait(), timeout=timeout)
            return True
        except TimeoutError:
            return key in self._finished_set
        finally:
            waiters = self._finish_waiters.get(key)
            if waiters and event in waiters:
                waiters.remove(event)
                if not waiters:
                    self._finish_waiters.pop(key, None)

    def find_entry(self, target_id: str, *, seq: int) -> LogEntry | None:
        """Look up one entry of *target_id* by exact ``#seq``."""
        for entry in self._entries:
            if entry.target_id == target_id and entry.seq == seq:
                return entry
        return None

    def find_network_entry(
        self,
        target_id: str,
        *,
        seq: int | None = None,
        url_contains: str = "",
    ) -> LogEntry | None:
        """Return a network entry for *target_id* by exact *seq* or newest URL substring match."""
        if seq is not None:
            entry = self.find_entry(target_id, seq=seq)
            if entry is None or entry.kind != "network":
                return None
            return entry
        needle = url_contains.strip().casefold()
        for entry in reversed(self._entries):
            if entry.target_id != target_id or entry.kind != "network":
                continue
            if not needle or needle in entry.url.casefold():
                return entry
        return None

    def entries(
        self, target_id: str, *, since: int = 0, kind: str = "all", errors_only: bool = False
    ) -> list[LogEntry]:
        """Entries of *target_id* newer than *since*, oldest first."""
        return [
            entry
            for entry in self._entries
            if entry.target_id == target_id
            and entry.seq > since
            and (kind == "all" or entry.kind == kind)
            and (not errors_only or entry.level != "info")
        ]
