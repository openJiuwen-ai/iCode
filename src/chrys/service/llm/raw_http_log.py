# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Raw LLM HTTP request/response logging.

Enabled with ``CHRYS_DEBUG_LLM_RAW_HTTP_LOG=1``.  This is intentionally separate
from OpenTelemetry: OTel records semantic GenAI events, while this module
captures the full HTTP request and response payloads seen by the provider
SDK's httpx client.

The log is unredacted by design.  It contains API keys, prompts, tool
arguments, and model responses, so it must remain strictly opt-in.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final
from uuid import uuid4

import httpx

from chrys.foundation.util.chrys_headers import SESSION_ID_HEADER, X_SESSION_ID_HEADER

if TYPE_CHECKING:
    from collections.abc import Callable

    from chrys.service.profiles.models.schema import ModelProfile

logger = logging.getLogger(__name__)

RAW_HTTP_LOG_ENV: Final[str] = "CHRYS_DEBUG_LLM_RAW_HTTP_LOG"
"""Legacy alias for ``log.raw_http_capture``, kept for the docstring above."""
RAW_HTTP_LOG_FILE: Final[str] = "llm_raw_http.jsonl"
_EXCHANGE_ID_EXTENSION: Final[str] = "chrys_raw_http_exchange_id"
_SESSION_ID_EXTENSION: Final[str] = "chrys_raw_http_session_id"
_WRITE_LOCK = threading.Lock()


def raw_http_logging_enabled() -> bool:
    """Return True when full LLM HTTP logging is switched on for this process."""
    from chrys.foundation.config.process_settings import process_settings

    return process_settings().raw_http_capture


def raw_http_log_path(session_id: str | None, session_dir: Path | None) -> Path | None:
    """Return the session-scoped raw HTTP log path when logging is enabled."""
    if not raw_http_logging_enabled():
        return None
    if session_dir is not None:
        return session_dir / RAW_HTTP_LOG_FILE
    if not session_id:
        return None

    from chrys.foundation.config.settings import resolve_sessions_dir
    from chrys.foundation.util.session_ids import session_short_id

    return resolve_sessions_dir() / session_short_id(session_id) / RAW_HTTP_LOG_FILE


def build_raw_http_event_hooks(
    *,
    log_path: Path,
    profile: ModelProfile,
    session_id: str | None,
) -> dict[str, list[Callable[..., Any]]]:
    """Build async httpx event hooks that append raw request/response JSONL."""

    async def _request_hook(request: httpx.Request) -> None:
        from chrys.service.llm.request_tracking import REQUEST_ATTEMPT_ID_EXTENSION

        attempt_id = request.extensions.get(REQUEST_ATTEMPT_ID_EXTENSION)
        exchange_id = attempt_id if isinstance(attempt_id, str) else uuid4().hex
        request_session_id = _request_session_id(request, fallback=session_id)
        request.extensions[_EXCHANGE_ID_EXTENSION] = exchange_id
        request.extensions[_SESSION_ID_EXTENSION] = request_session_id
        try:
            body = await request.aread()
            body_record = _body_to_record(body)
        except Exception as exc:
            body_record = {"read_error": type(exc).__name__, "message": str(exc)}
        await _append_record_async(
            log_path,
            {
                "timestamp": _timestamp(),
                "event": "request",
                "exchange_id": exchange_id,
                "session_id": request_session_id,
                "model_profile": _profile_record(profile),
                "request": {
                    "method": request.method,
                    "url": str(request.url),
                    "headers": _headers_to_record(request.headers),
                    "body": body_record,
                },
            },
        )

    async def _response_hook(response: httpx.Response) -> None:
        exchange_id = response.request.extensions.get(_EXCHANGE_ID_EXTENSION)
        if not isinstance(exchange_id, str):
            exchange_id = uuid4().hex
        response_session_id = response.request.extensions.get(_SESSION_ID_EXTENSION)
        if not isinstance(response_session_id, str):
            response_session_id = session_id
        response_record: dict[str, Any] = {
            "status_code": response.status_code,
            "reason_phrase": response.reason_phrase,
            "headers": _headers_to_record(response.headers),
        }
        # Mock transports and some custom transports may return an already
        # buffered response.  Real network responses normally reach this hook
        # before ``AsyncClient.send(stream=False)`` performs its final read.
        if hasattr(response, "_content"):
            response_record["body"] = _body_to_record(response.content)
            await _append_response_record(log_path, profile, response_session_id, exchange_id, response_record)
            return
        response.stream = _RawLogAsyncByteStream(
            stream=response.stream,
            log_path=log_path,
            profile=profile,
            session_id=response_session_id,
            exchange_id=exchange_id,
            response_record=response_record,
        )

    return {"request": [_request_hook], "response": [_response_hook]}


def _request_session_id(request: httpx.Request, *, fallback: str | None) -> str | None:
    """Return the final wire session id stamped on this request."""
    return request.headers.get(X_SESSION_ID_HEADER) or request.headers.get(SESSION_ID_HEADER) or fallback


class _RawLogAsyncByteStream(httpx.AsyncByteStream):
    """Tee response bytes into the raw log without pre-consuming streams."""

    def __init__(
        self,
        *,
        stream: httpx.AsyncByteStream,
        log_path: Path,
        profile: ModelProfile,
        session_id: str | None,
        exchange_id: str,
        response_record: dict[str, Any],
    ) -> None:
        self._stream = stream
        self._log_path = log_path
        self._profile = profile
        self._session_id = session_id
        self._exchange_id = exchange_id
        self._response_record = response_record
        self._chunks = bytearray()
        self._logged = False

    async def __aiter__(self):
        try:
            async for chunk in self._stream:
                self._chunks.extend(chunk)
                yield chunk
        except Exception as exc:
            await self._log_once({"read_error": type(exc).__name__, "message": str(exc)})
            raise
        else:
            await self._log_once(_body_to_record(bytes(self._chunks)))

    async def aclose(self) -> None:
        try:
            await self._stream.aclose()
        finally:
            await self._log_once(
                {
                    **_body_to_record(bytes(self._chunks)),
                    "closed_before_complete": True,
                }
            )

    async def _log_once(self, body_record: dict[str, Any]) -> None:
        if self._logged:
            return
        self._logged = True
        self._response_record["body"] = body_record
        await _append_response_record(
            self._log_path,
            self._profile,
            self._session_id,
            self._exchange_id,
            self._response_record,
        )


async def _append_response_record(
    log_path: Path,
    profile: ModelProfile,
    session_id: str | None,
    exchange_id: str,
    response_record: dict[str, Any],
) -> None:
    await _append_record_async(
        log_path,
        {
            "timestamp": _timestamp(),
            "event": "response",
            "exchange_id": exchange_id,
            "session_id": session_id,
            "model_profile": _profile_record(profile),
            "response": response_record,
        },
    )


async def _append_record_async(path: Path, record: dict[str, Any]) -> None:
    try:
        await asyncio.to_thread(_append_record, path, record)
    except OSError:
        logger.warning("Failed to write raw LLM HTTP log %s", path, exc_info=True)


def _append_record(path: Path, record: dict[str, Any]) -> None:
    line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
    with _WRITE_LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(line)


def _timestamp() -> str:
    return datetime.now(UTC).isoformat()


def _profile_record(profile: ModelProfile) -> dict[str, str]:
    return {
        "id": profile.id,
        "name": profile.name,
        "provider": profile.provider,
        "model_id": profile.model_id,
    }


def _headers_to_record(headers: httpx.Headers) -> list[list[str]]:
    return [[name, value] for name, value in headers.multi_items()]


def _body_to_record(body: bytes) -> dict[str, Any]:
    record: dict[str, Any] = {"size_bytes": len(body)}
    try:
        record["encoding"] = "utf-8"
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        record["encoding"] = "base64"
        record["base64"] = base64.b64encode(body).decode("ascii")
    else:
        try:
            record["json"] = json.loads(text)
        except json.JSONDecodeError:
            sse_events = _parse_sse_events(text)
            if sse_events is not None:
                record["sse"] = sse_events
            else:
                record["text"] = text
    return record


def _parse_sse_events(text: str) -> list[dict[str, Any]] | None:
    """Parse a Server-Sent Events body into structured events when possible."""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    events: list[dict[str, Any]] = []
    for block in normalized.split("\n\n"):
        if not block.strip():
            continue
        event: dict[str, Any] = {}
        data_lines: list[str] = []
        comments: list[str] = []
        for line in block.split("\n"):
            if not line:
                continue
            if line.startswith(":"):
                comments.append(line[1:].lstrip(" "))
                continue
            field, sep, value = line.partition(":")
            if not sep:
                value = ""
            elif value.startswith(" "):
                value = value[1:]
            if field == "data":
                data_lines.append(value)
            elif field in {"event", "id", "retry"}:
                event[field] = int(value) if field == "retry" and value.isdecimal() else value

        if comments:
            event["comments"] = comments
        if data_lines:
            data = "\n".join(data_lines)
            if data.strip() == "[DONE]":
                event["data"] = "[DONE]"
            else:
                try:
                    event["data_json"] = json.loads(data)
                except json.JSONDecodeError:
                    event["data"] = data
        if event:
            events.append(event)
    if any("data" in event or "data_json" in event for event in events):
        return events
    return None
