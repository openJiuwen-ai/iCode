# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Checks that a Chat Completions body or event stream can be read at all.

Compatible gateways answer with error envelopes, HTML pages or empty
streams under any status and Content-Type; these checks turn each into one
invalid-response error that quotes a bounded part of the body. An error
envelope is the exception: it is raised as the service's own error
(:class:`ErrorEnvelopeError`), so the classifier reads its code and type.
"""

from __future__ import annotations

import contextlib
import json
import logging
from collections.abc import AsyncIterator, Mapping
from typing import Any, NamedTuple, NoReturn

import httpx
from openai import APIStatusError
from openai.types.chat.chat_completion import ChatCompletion

from chrys.kernel.exceptions import ChatClientException, ChatClientInvalidResponseException

logger = logging.getLogger(__name__)

_INVALID_RESPONSE_BODY_PREVIEW_LIMIT = 2000


def bounded_body_preview(body: str, *, truncated: bool = False) -> str:
    """Return a bounded representation of a provider response body.

    ``truncated`` marks a body that was cut off at capture time, so the preview
    is flagged even when the captured text itself fits within the limit.
    """
    if not body:
        return "<empty>"
    preview = repr(body)
    suffix = "...[truncated]"
    if len(preview) + (len(suffix) if truncated else 0) <= _INVALID_RESPONSE_BODY_PREVIEW_LIMIT:
        return preview + suffix if truncated else preview
    return preview[: _INVALID_RESPONSE_BODY_PREVIEW_LIMIT - len(suffix)] + suffix


def _invalid_response_message(raw_response: Any, detail: str, body_preview: str) -> str:
    """Build a deterministic diagnostic from an OpenAI SDK raw response."""
    status_code = raw_response.status_code
    content_type = raw_response.headers.get("content-type", "<missing>")
    return (
        f"Chat Completions API returned an invalid response "
        f"(HTTP {status_code}, Content-Type {content_type!r}): {detail}. "
        f"Response body: {body_preview}"
    )


def raise_invalid_response(message: str) -> NoReturn:
    """Raise the stable two-level exception shape expected by error presentation."""
    invalid_response = ChatClientInvalidResponseException(message)
    raise ChatClientException(
        f"Chat Completions client received an invalid response: {invalid_response}",
        inner_exception=invalid_response,
    ) from invalid_response


class ErrorEnvelopeError(APIStatusError):
    """The error object a gateway answered with under a success status.

    Some gateways report a failed request with HTTP 200 and the body an error
    status would carry, ``{"error": {"code", "type", "message"}}``. Raised as
    the SDK raises an error status, with the status, code, type and message
    as sent, it is judged as an error a stream reports in-band: a code or
    type a retry meets again (authentication, quota, a rejected request) is
    final, anything else may pass on a retry.
    """


def raise_error_envelope(http_response: httpx.Response, body: str) -> None:
    """Raise :class:`ErrorEnvelopeError` when *body* is an error envelope, and nothing otherwise.

    An envelope is a JSON object with no ``choices`` and a non-empty ``error``
    member: an object, or a message on its own.
    """
    try:
        # A front end may lead the body with a byte order mark.
        payload = json.loads(body.removeprefix("\ufeff"))
    except ValueError:
        return
    if not isinstance(payload, Mapping) or payload.get("choices") is not None:
        return
    error = payload.get("error")
    if isinstance(error, Mapping) and error:
        details: dict[str, Any] = dict(error)
    elif isinstance(error, str) and error.strip():
        details = {"message": error}
    else:
        return
    raise ErrorEnvelopeError(
        f"Chat Completions API answered HTTP {http_response.status_code} with an error: {bounded_body_preview(body)}",
        response=http_response,
        body=details,
    )


def _raise_invalid_body(raw_response: Any, detail: str) -> NoReturn:
    """Reject a fully read (non-streaming) response, quoting its body."""
    body_preview = bounded_body_preview(raw_response.text)
    raise_invalid_response(_invalid_response_message(raw_response, detail, body_preview))


def parse_completion(raw_response: Any) -> ChatCompletion:
    """Parse and validate the minimum Chat Completions response shape."""
    try:
        response = raw_response.parse()
    except json.JSONDecodeError as exc:
        _raise_invalid_body(raw_response, f"response body is not valid JSON ({exc})")
    if not isinstance(response, ChatCompletion):
        detail = f"decoded payload type is {type(response).__name__}; expected a ChatCompletion object"
        _raise_invalid_body(raw_response, detail)
    choices = response.choices
    if choices is None:
        raise_error_envelope(raw_response.http_response, raw_response.text)
        _raise_invalid_body(raw_response, "JSON payload is missing the required 'choices' array")
    if not isinstance(choices, list):
        # Non-strict SDK construction passes any non-list value straight through.
        detail = f"JSON payload 'choices' is {type(choices).__name__}; expected an array"
        _raise_invalid_body(raw_response, detail)
    return response


def raise_invalid_stream_event(chunk: Any, choices: Any) -> NoReturn:
    """Reject a decoded stream event whose ``choices`` is not an array."""
    try:
        payload = chunk.model_dump_json(exclude_unset=True)
    except Exception:
        payload = repr(chunk)
    raise_invalid_response(
        f"Chat Completions API returned a stream event whose 'choices' is {type(choices).__name__}; "
        f"expected an array. Event data: {bounded_body_preview(payload)}"
    )


# SSE framing: every line is a field line (the part before a colon is the
# name, and a line without a colon is a name with an empty value), a comment
# line starts with a colon, and the SDK ignores names it does not know. So a
# JSON or HTML document is rejected outright, and anything else is SSE as soon
# as a colon shows up inside the sniff window; plain text and scalars fail for
# never producing one.
_DOCUMENT_LEADING_BYTES = (b"{", b"[", b"<")
_UTF8_BOM = b"\xef\xbb\xbf"
_SSE_SNIFF_SKIP_BYTES = _UTF8_BOM + b" \t\r\n"
# Raw bytes read while looking for the first decoded, non-blank byte.
_SSE_SNIFF_LIMIT = 512
# Bytes of a rejected body retained for the diagnostic; the remainder is never read.
_INVALID_STREAM_BODY_CAPTURE_LIMIT = 8192


class ReplayByteStream(httpx.AsyncByteStream):
    """Re-yield sniffed leading bytes ahead of the untouched remainder.

    Keeps a bounded copy of everything that flows through so a stream that
    ends without a single event can still be shown in the diagnostic.
    """

    def __init__(
        self,
        prefix: bytes,
        rest: AsyncIterator[bytes],
        original: httpx.AsyncByteStream,
        *,
        response: Any,
        at_eof: bool,
    ) -> None:
        self._prefix = prefix
        self._rest = rest
        self._original = original
        self.response = response
        self.captured = b""
        self.total = 0
        self.exhausted = at_eof
        self._record(prefix)

    def _record(self, chunk: bytes) -> None:
        self.total += len(chunk)
        room = _INVALID_STREAM_BODY_CAPTURE_LIMIT - len(self.captured)
        if room > 0:
            self.captured += chunk[:room]

    async def __aiter__(self) -> AsyncIterator[bytes]:
        if self._prefix:
            yield self._prefix
        async for chunk in self._rest:
            self._record(chunk)
            yield chunk
        self.exhausted = True

    async def aclose(self) -> None:
        await self._original.aclose()


def _decode_stream_prefix(http_response: Any, prefix: bytes) -> bytes | None:
    """Apply the response's Content-Encoding to a raw prefix; ``None`` if undecodable."""
    encoding = http_response.headers.get("content-encoding", "")
    if not prefix or not encoding:
        return prefix
    try:
        # A probe response reuses httpx's own decoders (gzip, deflate, chains)
        # and tolerates a prefix that ends mid-stream.
        return httpx.Response(200, headers={"content-encoding": encoding}, content=prefix).content
    except httpx.DecodingError:
        return None


def _stream_body_text(http_response: Any, data: bytes) -> str:
    decoded = _decode_stream_prefix(http_response, data)
    if decoded is None:
        return f"<{len(data)} undecodable bytes>"
    try:
        return decoded.decode(http_response.charset_encoding or "utf-8", errors="replace")
    except LookupError:
        # An unknown ``charset`` parameter must not displace the diagnostic.
        return decoded.decode("utf-8", errors="replace")


class _RejectedStream(NamedTuple):
    """Why a streaming body cannot yield events, and the part of it captured."""

    detail: str
    text: str
    # Only the start of the body was captured.
    truncated: bool


def _sse_framing_verdict(head: bytes, *, complete: bool) -> bool | None:
    """``True`` for SSE framing, ``False`` for none, ``None`` if more bytes could still decide."""
    if head.startswith(_DOCUMENT_LEADING_BYTES):
        return False
    if b":" in head:
        return True
    return False if complete else None


def _strip_leading_bom_from_decoded_bytes(http_response: Any) -> None:
    """Drop a leading UTF-8 BOM from the decoded byte stream the SDK reads.

    The SSE spec ignores one leading BOM, but the SDK decoder folds it into
    the first field name and silently drops that event. Stripping after
    Content-Encoding decoding covers compressed and identity bodies alike.
    """
    original = http_response.aiter_bytes

    async def aiter_bytes(*args: Any, **kwargs: Any) -> AsyncIterator[bytes]:
        buffered = b""
        stripping = True
        async for chunk in original(*args, **kwargs):
            if stripping:
                buffered += chunk
                if _UTF8_BOM.startswith(buffered):
                    if len(buffered) < len(_UTF8_BOM):
                        continue
                    buffered = b""
                elif buffered.startswith(_UTF8_BOM):
                    buffered = buffered[len(_UTF8_BOM) :]
                stripping = False
                chunk = buffered
                if not chunk:
                    continue
            yield chunk

    http_response.aiter_bytes = aiter_bytes


async def _sniff_stream(http_response: Any) -> ReplayByteStream | _RejectedStream:
    """Classify a streaming body by its decoded leading bytes.

    Returns the installed replay stream when the body opens with SSE framing,
    or what was captured of a body that cannot yield events (JSON, HTML,
    plain text, blank). Classification reads decoded
    bytes because the SDK decodes Content-Encoding before parsing, while the
    replay carries the raw transport bytes untouched. A rejected body is
    captured only up to a fixed size: a gateway that keeps sending must not pin
    memory or hold the turn open, and a transport failure during that capture
    must not displace the verdict already made.
    """
    original = http_response.stream
    rest = original.__aiter__()
    prefix = b""
    at_eof = False
    head = b""
    decoded: bytes | None = None
    verdict: bool | None = None
    while verdict is None and not at_eof and len(prefix) < _SSE_SNIFF_LIMIT:
        try:
            prefix += await rest.__anext__()
        except StopAsyncIteration:
            at_eof = True
        decoded = _decode_stream_prefix(http_response, prefix)
        head = decoded.lstrip(_SSE_SNIFF_SKIP_BYTES) if decoded is not None else b""
        if head:
            verdict = _sse_framing_verdict(head, complete=at_eof)
    if verdict is None and head:
        # The window filled with non-blank content and never showed a colon.
        verdict = False
    if verdict is False:
        leading = head[:1].decode("ascii", "replace")
        detail = f"streaming response body starts with {leading!r}; expected an SSE event stream"
    elif not head and at_eof:
        detail = "streaming response body is blank"
    else:
        # SSE framing, or nothing decodable inside the window: hand the raw
        # bytes to the decoder; the zero-event guard has the last word.
        if decoded is not None and decoded.startswith(_UTF8_BOM):
            _strip_leading_bom_from_decoded_bytes(http_response)
        replay = ReplayByteStream(prefix, rest, original, response=http_response, at_eof=at_eof)
        http_response.stream = replay
        return replay
    while not at_eof and len(prefix) < _INVALID_STREAM_BODY_CAPTURE_LIMIT:
        try:
            prefix += await rest.__anext__()
        except StopAsyncIteration:
            at_eof = True
        except Exception:
            logger.debug("Transport failed while capturing an invalid stream body", exc_info=True)
            break
    captured = prefix[:_INVALID_STREAM_BODY_CAPTURE_LIMIT]
    truncated = not at_eof or len(captured) < len(prefix)
    return _RejectedStream(detail, _stream_body_text(http_response, captured), truncated)


def zero_event_message(replay: ReplayByteStream) -> str:
    truncated = not replay.exhausted or len(replay.captured) < replay.total
    body_preview = bounded_body_preview(_stream_body_text(replay.response, replay.captured), truncated=truncated)
    return _invalid_response_message(replay.response, "stream ended without any events", body_preview)


async def validate_stream_response(raw_response: Any) -> ReplayByteStream:
    """Reject bodies that cannot be an SSE event stream before the SDK yields nothing.

    The SDK's SSE decoder never consults Content-Type, and compatible gateways
    mislabel in both directions (valid SSE as ``application/json``, error
    envelopes as ``text/event-stream``), so the decoded body is the only
    trustworthy signal. Only the leading bytes are read, and they are replayed
    to the SDK decoder so incremental streaming is preserved. Returns the
    replay stream, which records what the decoder saw for the zero-event guard.
    """
    http_response = raw_response.http_response
    try:
        verdict = await _sniff_stream(http_response)
    except BaseException:
        # Nothing downstream owns the response yet, so release the connection here.
        with contextlib.suppress(Exception):
            await http_response.aclose()
        raise
    if isinstance(verdict, ReplayByteStream):
        return verdict
    # Rejected: release the connection without draining the remainder.
    with contextlib.suppress(Exception):
        await http_response.aclose()
    if not verdict.truncated:
        raise_error_envelope(http_response, verdict.text)
    body_preview = bounded_body_preview(verdict.text, truncated=verdict.truncated)
    raise_invalid_response(_invalid_response_message(raw_response, verdict.detail, body_preview))
