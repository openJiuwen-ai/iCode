# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""What a classified error means to the user, as locale-neutral display messages.

Producers attach :func:`display_fields` to failure events; frontends render
the message and hint in the current locale and join them with
:data:`DISPLAY_WITH_HINT`.  The raw English text (``clean_error_message``)
stays on the event for the model, logs, ACP and the detail line.  Kinds with
nothing more useful to say than the raw text (a rejected request, an invalid
response, an unknown failure) describe as None, and so does any failure the
model service didn't answer: a network failure without a route snapshot (only
the LLM client stamps one) or another client's HTTP status.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

from chrys.foundation.branding import APP_DISPLAY_NAME
from chrys.foundation.i18n import MessageDef, MessageRef, msg
from chrys.foundation.i18n.formatting import format_message, sanitize_legacy_block
from chrys.foundation.net.route_probe import default_route_available, is_local_target
from chrys.foundation.platform.files import surrogate_safe_text

from .classify import ErrorClassification, classify_error, context_overflow_limit, is_blank_response
from .kinds import NETWORK_KINDS, ErrorKind
from .route import Origin

logger = logging.getLogger(__name__)

_NO_ROUTE = msg(
    "error.kind.no_route",
    fallback="Can't reach the network: there is no route to {host}. Check your network connection or VPN.",
)
_DNS_FAILED = msg(
    "error.kind.dns_failed",
    fallback=(
        "Can't resolve {host}. Check your network connection and DNS; "
        "if the address is wrong, fix the base URL in the model profile."
    ),
)
_HOST_UNREACHABLE = msg(
    "error.kind.host_unreachable",
    fallback="Can't reach {host}: the host is unreachable. Check your network, VPN, or firewall.",
)
_CONNECTION_REFUSED = msg(
    "error.kind.connection_refused",
    fallback="{host} refused the connection. Check the address and port, and that the service is running.",
)
_CONNECT_TIMEOUT = msg(
    "error.kind.connect_timeout", fallback="Connecting to {host} timed out. Check your network and firewall."
)
_CONNECTION_FAILED = msg(
    "error.kind.connection_failed", fallback="Couldn't connect to {host}. Check your network connection."
)
_CONNECTION_LOST = msg("error.kind.connection_lost", fallback="The connection to {host} was interrupted.")
_READ_TIMEOUT = msg("error.kind.read_timeout", fallback="Timed out waiting for a response from {host}.")
_WRITE_TIMEOUT = msg("error.kind.write_timeout", fallback="Timed out sending the request to {host}.")
_NETWORK_GENERIC = msg(
    "error.kind.network_generic",
    fallback="Couldn't connect to the model service. Check your network and proxy settings.",
)
_PROXY_UNREACHABLE = msg(
    "error.kind.proxy_unreachable",
    fallback=(
        "Can't connect to the proxy {proxy}. Check that the proxy is running and configured correctly, "
        'or turn on "Bypass proxy" in the model profile.'
    ),
)
_PROXY_REJECTED = msg("error.kind.proxy_rejected", fallback="The proxy {proxy} couldn't connect to {host}.")
_VIA_PROXY_FAILED = msg(
    "error.kind.via_proxy_failed",
    fallback="Connecting to {host} through the proxy {proxy} failed. Check the proxy and your network connection.",
)
_PROXY_AUTH_FAILED = msg(
    "error.kind.proxy_auth_failed",
    fallback="The proxy {proxy} requires authentication or rejected the credentials.",
)
_TLS_FAILED = msg(
    "error.kind.tls_failed",
    fallback=(
        "Couldn't establish a secure connection to {host}: the certificate isn't trusted or the protocol "
        "doesn't match. On a corporate network, check the proxy certificate setup."
    ),
)
_INVALID_ENDPOINT = msg(
    "error.kind.invalid_endpoint",
    fallback="The service address is invalid. Check the base URL in the model profile.",
)
_RATE_LIMITED = msg("error.kind.rate_limited", fallback="The model service is rate limiting requests.")
_QUOTA_EXHAUSTED = msg(
    "error.kind.quota_exhausted",
    fallback="The model service account is out of quota or credit. Check the account and try again.",
)
_OVERLOADED = msg("error.kind.overloaded", fallback="The model service is overloaded.")
_SERVER_ERROR = msg("error.kind.server_error", fallback="The model service returned an internal error.")
_CONTEXT_OVERFLOW = msg(
    "error.kind.context_overflow",
    fallback=(
        "The request exceeds the model's context window. "
        "Check that the profile's context window matches the model's real one."
    ),
)
_CONTEXT_OVERFLOW_CONFIG_MISMATCH = msg(
    "error.kind.context_overflow_config_mismatch",
    fallback=(
        "The model profile's maximum context window ({configured_max_context_tokens}) is larger than the server's "
        'limit ({server_max_context_tokens}). Set "Max Context Window" in the model profile '
        "to {server_max_context_tokens} or less."
    ),
)
_PAYLOAD_TOO_LARGE = msg(
    "error.kind.payload_too_large",
    fallback="The request is too large (for example, an image or attachment). Make it smaller and try again.",
)
_AUTH_FAILED = msg(
    "error.kind.auth_failed",
    fallback="The API key is invalid or lacks access. Check the API key in the model profile.",
)
_CONTENT_FILTERED = msg(
    "error.kind.content_filtered", fallback="The model service's content filter blocked this request."
)
_STREAM_TRUNCATED = msg("error.kind.stream_truncated", fallback="The model's response was cut off.")
_STREAM_STALLED = msg("retry.stream_stalled", fallback="Stream stalled")
_CONTEXT_OVERFLOW_RESEND = msg(
    "retry.context_overflow", fallback="The context window is full. Compacting the context before one retry."
)
_BLANK_RESPONSE_RESEND = msg("retry.blank_response", fallback="The model returned no answer. Asking again once.")
_BLANK_RESPONSE = msg("error.kind.blank_response", fallback="The model returned no answer.")
# The probe checks the device {app} runs on: under ``icode serve`` that is the
# server, not the device showing the browser.
_MAYBE_OFFLINE = msg(
    "error.hint.maybe_offline",
    fallback="The device running {app} doesn't seem to have a network connection. Check it first.",
)
DISPLAY_WITH_HINT = msg("error.display.with_hint", fallback="{message} {hint}")
"""Joins a rendered message and its rendered hint; zh-Hans joins without a space."""


def with_display_hint(
    message: str,
    hint: str | None,
    *,
    render: Callable[[MessageRef], str] | None = None,
) -> str:
    """Join an already rendered message and hint, avoiding a repeated hint."""
    if hint is None or hint in message:
        return message
    if "\n" in hint:
        return sanitize_legacy_block(surrogate_safe_text(f"{message}\n\n{hint}"))
    return (render or format_message)(DISPLAY_WITH_HINT.bind(message=message, hint=hint))


# Messages that name the target host.  TLS keeps its own wording through a
# proxy: an untrusted certificate is usually the proxy's own.
_HOST_MESSAGES: dict[ErrorKind, MessageDef] = {
    ErrorKind.NO_ROUTE: _NO_ROUTE,
    ErrorKind.DNS_FAILED: _DNS_FAILED,
    ErrorKind.HOST_UNREACHABLE: _HOST_UNREACHABLE,
    ErrorKind.CONNECTION_REFUSED: _CONNECTION_REFUSED,
    ErrorKind.CONNECT_TIMEOUT: _CONNECT_TIMEOUT,
    ErrorKind.CONNECTION_FAILED: _CONNECTION_FAILED,
    ErrorKind.CONNECTION_LOST: _CONNECTION_LOST,
    ErrorKind.READ_TIMEOUT: _READ_TIMEOUT,
    ErrorKind.WRITE_TIMEOUT: _WRITE_TIMEOUT,
    ErrorKind.TLS_FAILED: _TLS_FAILED,
}
# The model service's own answers, shown only when it answered.
_HOSTLESS_MESSAGES: dict[ErrorKind, MessageDef] = {
    ErrorKind.RATE_LIMITED: _RATE_LIMITED,
    ErrorKind.QUOTA_EXHAUSTED: _QUOTA_EXHAUSTED,
    ErrorKind.OVERLOADED: _OVERLOADED,
    ErrorKind.SERVER_ERROR: _SERVER_ERROR,
    ErrorKind.CONTEXT_OVERFLOW: _CONTEXT_OVERFLOW,
    ErrorKind.PAYLOAD_TOO_LARGE: _PAYLOAD_TOO_LARGE,
    ErrorKind.AUTH_FAILED: _AUTH_FAILED,
    ErrorKind.CONTENT_FILTERED: _CONTENT_FILTERED,
    ErrorKind.STREAM_TRUNCATED: _STREAM_TRUNCATED,
}
# Failures an offline machine produces; refused, unreachable and no-route
# already name their cause.
_PROBED_KINDS = frozenset({ErrorKind.DNS_FAILED, ErrorKind.CONNECT_TIMEOUT, ErrorKind.CONNECTION_FAILED})
_DEFAULT_PORTS = {"http": 80, "https": 443}


@dataclass(frozen=True, slots=True)
class ErrorDescription:
    """A classified error's display message, plus an optional hint shown after it."""

    kind: ErrorKind
    message: MessageRef
    hint: MessageRef | None = None


def _label(origin: Origin, *, with_port: bool) -> str:
    host = f"[{origin.host}]" if ":" in origin.host else origin.host
    return f"{host}:{origin.port}" if with_port else host


def _host(origin: Origin) -> str:
    """The target as the user wrote it: the port only when it isn't the scheme's default."""
    return _label(origin, with_port=_DEFAULT_PORTS.get(origin.scheme) != origin.port)


def _proxy(origin: Origin) -> str:
    return _label(origin, with_port=True)


def _describe_network(result: ErrorClassification, route_probe: Callable[[], bool | None]) -> ErrorDescription | None:
    kind = result.kind
    route = result.route
    if route is None:
        return None
    if kind is ErrorKind.INVALID_ENDPOINT:
        return ErrorDescription(kind, _INVALID_ENDPOINT.bind())
    host = _host(route.target)
    if route.proxy is not None:
        proxy = _proxy(route.proxy)
        if kind is ErrorKind.PROXY_AUTH_FAILED:
            return ErrorDescription(kind, _PROXY_AUTH_FAILED.bind(proxy=proxy))
        if kind is ErrorKind.PROXY_REJECTED:
            return ErrorDescription(kind, _PROXY_REJECTED.bind(host=host, proxy=proxy))
        if result.failed_at_first_hop:
            return ErrorDescription(kind, _PROXY_UNREACHABLE.bind(proxy=proxy))
        if kind is ErrorKind.TLS_FAILED:
            return ErrorDescription(kind, _TLS_FAILED.bind(host=host))
        # Timeouts, a dropped tunnel, a lost connection: which hop failed is unknown.
        return ErrorDescription(kind, _VIA_PROXY_FAILED.bind(host=host, proxy=proxy))
    definition = _HOST_MESSAGES.get(kind)
    if definition is None:
        # A proxy verdict with no proxy on the route.
        return ErrorDescription(kind, _NETWORK_GENERIC.bind())
    hint = None
    if kind in _PROBED_KINDS and not is_local_target(route.target.host) and route_probe() is False:
        hint = _MAYBE_OFFLINE.bind(app=APP_DISPLAY_NAME)
    return ErrorDescription(kind, definition.bind(host=host), hint)


def describe_error(
    exc: BaseException,
    *,
    route_probe: Callable[[], bool | None] = default_route_available,
    retry_notice: bool = False,
    max_context_tokens: int | None = None,
) -> ErrorDescription | None:
    """Describe *exc* for the user, or None when the raw text is the best description.

    *route_probe* runs only for a direct request to a public host that
    failed to resolve or connect; it adds a hint and never changes the
    message.  A *retry_notice* also names a stalled stream; a paused
    sub-agent's card already labels a stall by its pause reason.  A retry
    notice for a context overflow is the one resend after compacting, and
    one for a response that showed nothing the one resend of its request.
    *max_context_tokens*, the failed request's configured window, lets an
    overflow whose provider names a smaller limit say which value to set.
    """
    result = classify_error(exc)
    kind = result.kind
    if kind is ErrorKind.STREAM_STALLED and retry_notice:
        return ErrorDescription(kind, _STREAM_STALLED.bind())
    if kind is ErrorKind.CONTEXT_OVERFLOW and retry_notice:
        return ErrorDescription(kind, _CONTEXT_OVERFLOW_RESEND.bind())
    if is_blank_response(exc):
        return ErrorDescription(kind, (_BLANK_RESPONSE_RESEND if retry_notice else _BLANK_RESPONSE).bind())
    if (definition := _HOSTLESS_MESSAGES.get(kind)) is not None:
        if not result.from_model_service:
            return None
        if kind is ErrorKind.CONTEXT_OVERFLOW and max_context_tokens is not None:
            limit = context_overflow_limit(exc)
            if limit is not None and limit < max_context_tokens:
                return ErrorDescription(
                    kind,
                    _CONTEXT_OVERFLOW_CONFIG_MISMATCH.bind(
                        configured_max_context_tokens=max_context_tokens, server_max_context_tokens=limit
                    ),
                )
        return ErrorDescription(kind, definition.bind())
    if kind in NETWORK_KINDS:
        return _describe_network(result, route_probe)
    return None


def display_fields(
    exc: BaseException, *, retry_notice: bool = False, max_context_tokens: int | None = None
) -> tuple[MessageRef | None, MessageRef | None]:
    """Return ``(display_message, display_hint)`` for a failure event; ``(None, None)`` on any internal error."""
    try:
        description = describe_error(exc, retry_notice=retry_notice, max_context_tokens=max_context_tokens)
    except Exception:
        logger.debug("Describing an error for display failed", exc_info=True)
        return None, None
    if description is None:
        return None, None
    return description.message, description.hint
