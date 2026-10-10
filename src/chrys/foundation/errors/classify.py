# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The one error classifier: kind and retry decision from one pass.

Invariant: apart from the two legacy rules (the owner-terminal veto and the
transient layers), everything that sets ``kind`` or overrides ``retryable``
reads only the explicit graph (:func:`iter_explicit_graph`).  An exception
left in ``__context__`` from an earlier request — a stale 413 or quota error
being handled when a fresh request failed — can neither steer the retry
decision nor the kind shown to the user.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import _legacy
from ._text import _clean_exception_text
from ._walk import iter_explicit_graph, iter_full_chain
from .kinds import ErrorKind, TimeoutPhase
from .network import NetworkFailure, classify_connection_failure
from .provider import (
    ContinuationVerdictError,
    ProviderResponseError,
    ProviderSignal,
    in_band_failure_retryable,
    is_2xx,
    named_context_limit,
    names_context_overflow,
    names_server_error_overflow,
    provider_signal,
    signal_kind,
    status_kind,
    stream_error_retryable,
)
from .route import RouteFacts, route_of


@dataclass(frozen=True, slots=True)
class ErrorClassification:
    """What an exception means for retry policy and for the user."""

    kind: ErrorKind
    retryable: bool
    signal: ProviderSignal | None = None
    timeout_phase: TimeoutPhase | None = None
    # The failed request's route snapshot, when its client stamped one.
    route: RouteFacts | None = None
    # A TCP or DNS answer shows the first hop (the proxy, or the target when
    # direct) itself failed.
    failed_at_first_hop: bool = False
    # The model service itself answered: only the LLM client stamps a route,
    # and model-response types are raised only for its responses.  Another
    # client's HTTP status (an MCP server's 401) classifies the same way but
    # says nothing about the model service.
    from_model_service: bool = False
    invalidates_continuation_token: bool = False
    # Log-only trace of the deciding evidence, e.g. ``"http 429"``.
    evidence: str = ""


# Matched by class name anywhere in the MRO: the owning classes live in
# higher tiers than foundation.
_TYPE_KINDS = (
    (frozenset({"StreamStall", "StreamStallExhausted"}), ErrorKind.STREAM_STALLED),
    (frozenset({"ChatClientContentFilterException", "AgentContentFilterException"}), ErrorKind.CONTENT_FILTERED),
    (
        frozenset({"ChatClientInvalidResponseException", "AgentInvalidResponseException", "ResponseValidationError"}),
        ErrorKind.INVALID_RESPONSE,
    ),
)

# httpx and httpcore timeouts, by class name.
_TIMEOUT_PHASES: dict[str, TimeoutPhase] = {
    "ConnectTimeout": "connect",
    "WriteTimeout": "write",
    "ReadTimeout": "read",
    "PoolTimeout": "pool",
}
_TIMEOUT_MODULES = frozenset({"httpx", "httpcore"})

# What Anthropic says when replayed thinking no longer matches the conversation before it.
_THINKING_BINDING_PHRASE = "bound to a different conversation"

# Kinds no retry can fix, whatever the transient layers say.
_NON_RETRYABLE_KINDS = frozenset({ErrorKind.QUOTA_EXHAUSTED, ErrorKind.CONTEXT_OVERFLOW, ErrorKind.PAYLOAD_TOO_LARGE})


def _status_code(exc: BaseException) -> int | None:
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return status
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None) if response is not None else None
    return status if isinstance(status, int) else None


def _type_kind(exc: BaseException) -> ErrorKind | None:
    names = {cls.__name__ for cls in type(exc).__mro__}
    return next((kind for type_names, kind in _TYPE_KINDS if names & type_names), None)


def _timeout_phase(exc: BaseException) -> TimeoutPhase | None:
    for cls in type(exc).__mro__:
        if cls.__module__.partition(".")[0] in _TIMEOUT_MODULES and cls.__name__ in _TIMEOUT_PHASES:
            return _TIMEOUT_PHASES[cls.__name__]
    return None


def _first_timeout_phase(explicit: tuple[BaseException, ...]) -> TimeoutPhase | None:
    return next((phase for node in explicit if (phase := _timeout_phase(node)) is not None), None)


def _explicit_kind(
    explicit: tuple[BaseException, ...],
    signal: ProviderSignal | None,
    network: NetworkFailure | None,
    overflow: bool,
) -> tuple[ErrorKind, str]:
    """Return the kind explicit evidence names: provider signal, then network leaf, then node types."""
    if overflow:
        return ErrorKind.CONTEXT_OVERFLOW, "context overflow"
    if signal is not None:
        kind, evidence = signal_kind(signal)
        if kind is not ErrorKind.UNKNOWN:
            return kind, evidence
    if network is not None and network.kind is not ErrorKind.UNKNOWN:
        return network.kind, network.evidence
    for node in explicit:
        if node is not (signal.source if signal is not None else None) and (status := _status_code(node)) is not None:
            return status_kind(status), f"http {status}"
        if (kind := _type_kind(node)) is not None:
            return kind, f"type {type(node).__name__}"
    return ErrorKind.UNKNOWN, ""


def _from_model_service(explicit: tuple[BaseException, ...], route: RouteFacts | None) -> bool:
    return route is not None or any(
        isinstance(node, ProviderResponseError) or _type_kind(node) is not None for node in explicit
    )


def _invalidates_continuation_token(explicit: tuple[BaseException, ...]) -> bool:
    return any(isinstance(node, ContinuationVerdictError) and node.invalidates_continuation_token for node in explicit)


def classify_error(exc: BaseException) -> ErrorClassification:
    """Classify *exc* for retry policy and display."""
    explicit = tuple(iter_explicit_graph(exc))
    route = route_of(explicit)
    signal = provider_signal(explicit)
    network = classify_connection_failure(exc, route)
    overflow = names_context_overflow(
        signal, (_clean_exception_text(node) for node in explicit if _type_kind(node) is None)
    )
    kind, evidence = _explicit_kind(explicit, signal, network, overflow)
    # The full chain feeds only the frozen legacy layers.
    full = tuple(iter_full_chain(exc))
    if _legacy.is_owner_terminal(full):
        retryable = False
    elif signal is not None and signal.explicit_retryable is not None:
        retryable = signal.explicit_retryable
    elif (
        (network is not None and network.deterministic)
        or kind in _NON_RETRYABLE_KINDS
        or _names_a_final_in_band_failure(signal)
    ):
        retryable = False
    elif signal is not None and is_2xx(signal.status_code):
        retryable = stream_error_retryable(signal, kind)
    else:
        # The legacy owner and deterministic vetoes were both decided above.
        retryable = _legacy.is_transient(full)
    return ErrorClassification(
        kind=kind,
        retryable=retryable,
        signal=signal,
        timeout_phase=(network.timeout_phase if network is not None else None) or _first_timeout_phase(explicit),
        route=route,
        failed_at_first_hop=network is not None and network.first_hop_evidence,
        from_model_service=_from_model_service(explicit, route),
        invalidates_continuation_token=_invalidates_continuation_token(explicit),
        evidence=evidence,
    )


def _names_a_final_in_band_failure(signal: ProviderSignal | None) -> bool:
    """Whether *signal* is an error a stream reported in-band whose code a retry meets again.

    The OpenAI SDK raises an error event inside a stream as a bare
    ``APIError`` with no status: its code gets the verdict an adapter gives
    the same failure (:func:`in_band_failure_retryable`). A veto only: an
    unknown code keeps the retry it had.
    """
    return (
        signal is not None
        and signal.status_code is None
        and signal.code is not None
        and not in_band_failure_retryable(signal.code)
    )


def is_retryable(e: BaseException) -> bool:
    """Check if an exception is a transient error worth retrying."""
    return classify_error(e).retryable


def is_context_overflow(exc: BaseException) -> bool:
    """Return whether the provider rejected the request for exceeding the context window.

    ``request_too_large`` / 413 is an oversized payload, not an overflow.
    """
    return classify_error(exc).kind is ErrorKind.CONTEXT_OVERFLOW


def is_thinking_binding_rejection(exc: BaseException) -> bool:
    """Return whether Anthropic refused replayed thinking as bound to a different conversation.

    The service binds each signed thinking block to the request before it and
    answers a changed one with a 400 ``invalid_request_error`` saying so. All
    three are read from the one provider signal: a gateway that rewrites the
    text is missed rather than every invalid request caught. The text may
    also name the context window, so *exc* can be an overflow as well.
    """
    signal = classify_error(exc).signal
    return (
        signal is not None
        and signal.status_code == 400
        and signal.error_type == "invalid_request_error"
        and signal.message is not None
        and _THINKING_BINDING_PHRASE in signal.message
    )


def context_overflow_limit(exc: BaseException) -> int | None:
    """Return the window limit, in tokens, the provider named when it rejected the request as too long.

    Read only from the text that made *exc* an overflow: the provider
    signal's own, or without one the explicit nodes' (never ``__context__``).
    None when *exc* is no overflow or names no single positive limit.
    """
    result = classify_error(exc)
    if result.kind is not ErrorKind.CONTEXT_OVERFLOW:
        return None
    if (signal := result.signal) is not None:
        return named_context_limit((signal.message or "", _clean_exception_text(signal.source)))
    return named_context_limit(
        _clean_exception_text(node) for node in iter_explicit_graph(exc) if _type_kind(node) is None
    )


def may_be_context_overflow(exc: BaseException) -> bool:
    """Return whether *exc* is, or may be, a context-window rejection.

    Only for a caller whose false positive merely shrinks its input, such as
    compaction's last-words shrink sequence: beyond :func:`is_context_overflow`,
    a 5xx whose text names the context window counts, since a gateway may wrap
    an overflow in one.  Retry policy, display and the main turn use
    :func:`is_context_overflow`, which leaves that 5xx retryable.
    """
    classification = classify_error(exc)
    if classification.kind is ErrorKind.CONTEXT_OVERFLOW:
        return True
    return classification.signal is not None and names_server_error_overflow(classification.signal)


def invalidates_continuation_token(exc: BaseException) -> bool:
    """Return whether *exc* judged a terminal response, so a retry must not re-poll it.

    A type check over the explicit graph alone: every failed wire call asks,
    and the retry policy classifies separately.
    """
    return _invalidates_continuation_token(tuple(iter_explicit_graph(exc)))


def is_read_timeout(exc: BaseException) -> bool:
    """Return whether *exc* is a read timeout (connect, write and pool timeouts aren't)."""
    return classify_error(exc).timeout_phase == "read"
