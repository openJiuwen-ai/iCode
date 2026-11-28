# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""``browser_network_body`` - inspect one recorded XHR/fetch/HTTP-error payload.

Fetches the response body on demand via CDP ``Network.getResponseBody`` using
the owning page (or OOPIF) session recorded by :class:`~icode_browser_use.page_log.PageLog`.
Headers, request POST data and response bodies are redacted before returning;
binary payloads are summarized by MIME type and byte length instead of dumping
base64 noise into the model context.
"""

from __future__ import annotations

import base64
from typing import Any

from ..errors import BadRequest, BrowserError
from ..page_log import LogEntry, scrub, scrub_headers, scrub_payload
from ..types import ToolBlock, text_block

NAME = "browser_network_body"

DESCRIPTION = (
    "Inspect a recorded network request's scrubbed headers, POST request body, and "
    "response body (via CDP Network.getResponseBody). Pass seq=<#seq> from browser_logs "
    "or url_contains=<substring> to pick the newest matching XHR/fetch or HTTP >= 400 "
    "entry in the current tab (omit both for the latest network entry). Sensitive headers "
    "(Authorization, Cookie, Set-Cookie, API keys) and token/password/JWT values are "
    "redacted; binary bodies are omitted with MIME type and byte size. Large text/JSON "
    "bodies paginate with max_chars and offset."
)

PARAMS: dict[str, Any] = {
    "seq": {
        "type": "integer",
        "min": 1,
        "description": "Optional #seq of a network entry from browser_logs",
    },
    "url_contains": {
        "type": "string",
        "default": "",
        "description": "Optional case-insensitive URL substring when seq is omitted (picks newest match)",
    },
    "include_headers": {
        "type": "boolean",
        "default": True,
        "description": "Include scrubbed request and response headers",
    },
    "include_request_body": {
        "type": "boolean",
        "default": True,
        "description": "Include scrubbed request POST payload when present",
    },
    "max_chars": {"type": "integer", "default": 4000, "min": 200, "max": 7000},
    "offset": {"type": "integer", "default": 0, "min": 0},
}

_REQUEST_BODY_LIMIT = 2000

_TEXT_MIME_PREFIXES = ("text/",)
_TEXT_MIME_EXACT = frozenset(
    {
        "application/json",
        "application/javascript",
        "application/ecmascript",
        "application/xml",
        "application/xhtml+xml",
        "application/x-www-form-urlencoded",
        "application/graphql",
        "application/ld+json",
        "application/problem+json",
        "image/svg+xml",
    }
)


def _is_textual_mime(mime_type: str) -> bool:
    clean = (mime_type or "").split(";", 1)[0].strip().lower()
    if not clean:
        return True
    if clean.startswith(_TEXT_MIME_PREFIXES) or clean in _TEXT_MIME_EXACT:
        return True
    return clean.endswith("+json") or clean.endswith("+xml")


def _decode_cdp_body(raw_body: str, *, base64_encoded: bool, mime_type: str) -> tuple[str, str]:
    """Return ``(text, note)`` where *note* explains binary omission when applicable."""
    if not base64_encoded:
        return raw_body, ""
    try:
        decoded = base64.b64decode(raw_body, validate=False)
    except ValueError:
        return "", f"[binary response body omitted: mime={mime_type or 'unknown'}, invalid base64]"
    if not _is_textual_mime(mime_type):
        return (
            "",
            f"[binary response body omitted: mime={mime_type or 'application/octet-stream'}, {len(decoded)} bytes]",
        )
    if b"\x00" in decoded[:4096]:
        return (
            "",
            f"[binary response body omitted: mime={mime_type or 'application/octet-stream'}, {len(decoded)} bytes]",
        )
    return decoded.decode("utf-8", errors="replace"), ""


def _should_wait_and_retry(message: str) -> bool:
    lower = message.lower()
    return "no data found" in lower or "no resource with given identifier" in lower


async def _fetch_body(session: Any, entry: LogEntry) -> tuple[str, str]:
    """Call CDP ``Network.getResponseBody`` and return ``(body_text, unavailable_note)``."""
    if entry.status <= 0 or not entry.request_id:
        return "", f"unavailable (request failed or has no response body: {scrub(entry.text)})"
    try:
        client = session.require_client()
    except BrowserError as exc:
        return "", f"unavailable ({exc})"
    cdp_session_id = entry.cdp_session_id or session.session_id
    timeout = getattr(getattr(session, "cfg", None), "cdp_timeout", 5.0)
    last_error = ""
    for attempt in range(2):
        try:
            payload = await client.send(
                "Network.getResponseBody",
                {"requestId": entry.request_id},
                session_id=cdp_session_id,
                timeout=timeout,
            )
            raw_body = str((payload or {}).get("body") or "")
            base64_encoded = bool((payload or {}).get("base64Encoded"))
            return _decode_cdp_body(raw_body, base64_encoded=base64_encoded, mime_type=entry.mime_type)
        except Exception as exc:
            last_error = str(exc)
            if "not allowed in bridge mode" in last_error.lower():
                return (
                    "",
                    "unavailable (当前 Bridge 扩展版本不支持读取响应体；请运行 icode-browser-use update-bridge 并重载扩展)",
                )
            if (
                attempt == 0
                and _should_wait_and_retry(last_error)
                and not session.page_log.is_finished(entry.target_id, entry.request_id)
            ):
                await session.page_log.wait_finished(entry.target_id, entry.request_id, timeout=1.0)
                continue
            break
    return "", f"unavailable ({scrub(last_error) or 'evicted or empty'})"


async def run(session: Any, arguments: dict[str, Any]) -> list[ToolBlock]:
    log = session.page_log
    if log.disabled_reason:
        return [text_block(f"无法读取网络响应体：{log.disabled_reason}")]

    seq = arguments.get("seq")
    url_contains = str(arguments.get("url_contains") or "")
    include_headers = bool(arguments.get("include_headers", True))
    include_request_body = bool(arguments.get("include_request_body", True))
    max_chars = int(arguments["max_chars"])
    offset = int(arguments["offset"])

    if seq is not None:
        exact = log.find_entry(session.target_id, seq=int(seq))
        if exact is not None and exact.kind != "network":
            raise BadRequest(f"#{seq} 是 console 日志，不是网络请求；请先用 browser_logs(kind='network') 查看网络 #seq")
        entry = exact
    else:
        entry = log.find_network_entry(session.target_id, url_contains=url_contains)

    if entry is None:
        hint = f"seq={seq}" if seq is not None else (f"url_contains={url_contains!r}" if url_contains else "最新请求")
        return [
            text_block(
                f"没有匹配的网络请求记录（{hint}；当前标签页 cursor={log.seq}，"
                "请先用 browser_logs(kind='network') 查看可用 #seq）"
            )
        ]

    header_url = f" {scrub(entry.url)}" if entry.url else ""
    lines = [
        f"#{entry.seq} network.{entry.level} {scrub(entry.text)}{header_url}",
        f"resource_type={entry.resource_type or '-'} mime_type={entry.mime_type or '-'}",
    ]

    if include_headers:
        req_headers = scrub_headers(entry.request_headers)
        if req_headers:
            lines.append("[REQUEST_HEADERS]")
            lines.extend(f"{k}: {v}" for k, v in req_headers)
        resp_headers = scrub_headers(entry.response_headers)
        if resp_headers:
            lines.append("[RESPONSE_HEADERS]")
            lines.extend(f"{k}: {v}" for k, v in resp_headers)

    if include_request_body and (entry.request_post_data or entry.has_post_data):
        lines.append("[REQUEST_BODY]")
        if entry.request_post_data:
            req_body = scrub_payload(entry.request_post_data)
            if len(req_body) > _REQUEST_BODY_LIMIT:
                lines.append(req_body[:_REQUEST_BODY_LIMIT] + f"\n… (truncated {len(req_body)} chars)")
            else:
                lines.append(req_body)
        else:
            lines.append("(request had POST data that CDP did not inline)")

    body_text, unavailable_note = await _fetch_body(session, entry)
    if unavailable_note:
        lines.append("[RESPONSE_BODY]")
        lines.append(unavailable_note)
        return [text_block("\n".join(lines))]

    scrubbed_body = scrub_payload(body_text)
    total = len(scrubbed_body)
    if offset >= total and total > 0:
        lines.append(f"[RESPONSE_BODY] (offset {offset} >= total {total} chars; empty slice)")
        return [text_block("\n".join(lines))]
    window = scrubbed_body[offset : offset + max_chars]
    end = offset + len(window)
    lines.append(f"[RESPONSE_BODY] (chars {offset}..{end} of {total})")
    if window:
        lines.append(window)
    else:
        lines.append("(empty body)")
    if end < total:
        lines.append(f"… 已截断（共 {total} 字符；继续读传 offset={end}，或调大 max_chars）")
    return [text_block("\n".join(lines))]
