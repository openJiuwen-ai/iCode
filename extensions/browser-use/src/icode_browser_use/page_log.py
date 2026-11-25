# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Bounded console and network log of the pages a handle owns.

Fed by CDP events the page session already receives (``Runtime`` and
``Network`` are enabled for every driven page), so collecting costs no extra
command. Only what helps diagnose a page is kept: console messages, uncaught
exceptions, API (XHR/fetch) responses, HTTP errors and failed requests.
"""

from __future__ import annotations

import json
import re
from collections import deque
from dataclasses import dataclass
from typing import Any

from .dialog import redact_message
from .policy import sanitize_display_url

#: Entries kept per handle (all tabs together, newest last).
MAX_ENTRIES = 200
#: Request id -> (method, url) remembered until its response or failure.
_MAX_TRACKED_REQUESTS = 500
#: Characters kept per stored URL.
_MAX_URL = 300
#: Raw characters kept per entry; recording does nothing else with page text, the
#: (bounded) redaction runs only for the entries browser_logs actually shows.
_MAX_RAW_TEXT = 1000

_ERROR_TYPES = frozenset({"error", "assert"})
_API_TYPES = frozenset({"XHR", "Fetch"})

#: A URL query/fragment (absolute or relative). Masked when it carries a ``=`` value
#: or a long opaque token (>= 16 chars after ?/#); a stack frame's trailing ``:line:col`` is kept.
_QUERY_RE = re.compile(r"[?#][^\s\"'<>()]*")
_POSITION_RE = re.compile(r"(?::\d+){1,2}$")
#: ``user:password@`` or a bare ``token@`` in a URL (matches up to the last ``@`` in authority).
_USERINFO_RE = re.compile(r"\b((?:https?|wss?|ftp)://)[^/?#\s]+@", re.IGNORECASE)
#: Multi-cookie headers/assignments (``Cookie: a=1; b=2`` or ``cookie=a=1; b=2``): mask the full value list.
_COOKIE_HEADER_RE = re.compile(
    r"(\b(?:set-)?cookie)\s*([:=])\s*[^\r\n\"'{}]+",
    re.IGNORECASE,
)
#: A secret word and its value: ``access_token=x``, ``"password": "x \"y\""``, ``{\"token\":\"x\"}``,
#: ``Authorization: Bearer x``. Supports JSON backslash escapes without backtracking.
_SECRET_RE = re.compile(
    r"(token|secret|password|passwd|api[_-]?key|authorization|cookie|signature|credential"
    r"|session[_-]?id|\bsid|jwt|\bpwd\b|\bauth\b|\bsession\b)"
    r"\\?[\"']?\s*[:=]\s*(?:\\?\"(?:\\.|[^\"\\])*\\?\"|'(?:\\.|[^'\\])*'"
    r"|(?:(?:bearer|basic|token|apikey|digest)\s+)?[^\s,;&\"'}\\]+)",
    re.IGNORECASE,
)
#: Digest auth parameters (``Digest username="alice", response="..."``): mask the whole parameter list.
_DIGEST_AUTH_RE = re.compile(r"\b(digest)\s+[A-Za-z0-9_-]+\s*=[^\r\n{}]+", re.IGNORECASE)
#: Standalone auth credentials (``Bearer eyJ…``, ``Basic dXNlcjpwYXNz``, ``ApiKey k123``, ``Token tok_123``).
_AUTH_SCHEME_RE = re.compile(r"\b(bearer|basic|apikey|token)\s+([\w.~+/=-]{6,})", re.IGNORECASE)
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


def scrub(text: str) -> str:
    """Model-visible form of a stored entry text: query values and secrets masked, then
    the shared redaction (whitespace collapsed, truncated). Linear in the bounded input."""
    text = _QUERY_RE.sub(_mask_query, text[:_MAX_RAW_TEXT])
    text = _USERINFO_RE.sub(r"\1", text)
    text = _DIGEST_AUTH_RE.sub(lambda match: f"{match.group(1)} ***", text)
    text = _COOKIE_HEADER_RE.sub(
        lambda match: f"{match.group(1)}: ***" if match.group(2) == ":" else f"{match.group(1)}=***",
        text,
    )
    text = _SECRET_RE.sub(lambda match: f"{match.group(1)}=***", text)
    text = _AUTH_SCHEME_RE.sub(_mask_auth_scheme, text)
    text = _BARE_JWT_RE.sub("***", text)
    return redact_message(text)


@dataclass(frozen=True)
class LogEntry:
    """One console message or network outcome."""

    seq: int
    target_id: str
    kind: str  # "console" | "network"
    level: str  # "error" | "warning" | "info"
    text: str
    url: str = ""


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

    @property
    def oldest_seq(self) -> int:
        """Sequence number of the oldest entry still kept (``seq + 1`` when empty)."""
        return self._entries[0].seq if self._entries else self._seq + 1

    def _add(self, target_id: str, kind: str, level: str, text: str, url: str = "") -> None:
        self._seq += 1
        self._entries.append(LogEntry(self._seq, target_id, kind, level, text[:_MAX_RAW_TEXT], url))

    def console(self, target_id: str, params: dict[str, Any]) -> None:
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

    def exception(self, target_id: str, params: dict[str, Any]) -> None:
        details = params.get("exceptionDetails") or {}
        exception = details.get("exception") or {}
        text = str(exception.get("description") or details.get("text") or "exception")[:_MAX_RAW_TEXT]
        # Keep the message and the top of the stack; deep frames are noise.
        self._add(target_id, "console", "error", "uncaught: " + " | ".join(text.splitlines()[:4]))

    def request(self, target_id: str, params: dict[str, Any]) -> None:
        request = params.get("request") or {}
        key = (target_id, str(params.get("requestId", "")))
        self._requests.pop(key, None)  # a redirect reuses the id: keep the newest hop last
        self._requests[key] = (str(request.get("method") or "GET")[:32], _short_url(str(request.get("url") or "")))
        if len(self._requests) > _MAX_TRACKED_REQUESTS:
            del self._requests[next(iter(self._requests))]

    def response(self, target_id: str, params: dict[str, Any]) -> None:
        response = params.get("response") or {}
        if params.get("type") == "Document" and not response.get("url"):
            return  # the Bridge's status-only copy; the full response (if allowed) follows
        # The request is answered: it leaves the in-flight table whether or not it is
        # logged, so finished requests never push out a slow one still pending.
        method, url = self._requests.pop((target_id, str(params.get("requestId", ""))), ("", ""))
        status = int(response.get("status") or 0)
        if status < 400 and params.get("type") not in _API_TYPES:
            return
        url = _short_url(str(response.get("url") or "")) or url
        level = "error" if status >= 500 else "warning" if status >= 400 else "info"
        self._add(target_id, "network", level, f"{method or '?'} {status}", url)

    def failed(self, target_id: str, params: dict[str, Any]) -> None:
        method, url = self._requests.pop((target_id, str(params.get("requestId", ""))), ("", ""))
        if params.get("canceled") and not params.get("blockedReason"):
            return  # navigating away cancels in-flight loads; that is not a failure
        reason = str(params.get("blockedReason") or params.get("errorText") or "failed")
        if reason == "inspector":  # Fetch.failRequest from the plugin's network policy
            reason = "blocked by the allow-list (ICODE_BROWSER_ALLOWED_DOMAINS/ORIGINS)"
        self._add(target_id, "network", "error", f"{method or '?'} FAILED {reason}", url)

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
