# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Error classification and formatting shared by every layer.

Provider and network failures are classified here, once: retry policy
(:func:`is_retryable`), semantic predicates and user-facing descriptions all
read :func:`classify_error`.  Don't add another chain walk or phrase table
elsewhere.  :func:`clean_error_message` stays the raw English text for the
model, logs, ACP and headless output.
"""

from __future__ import annotations

from ._legacy import RETRYABLE_STATUS_CODES, RETRYABLE_TYPE_NAMES
from ._walk import iter_explicit_graph
from .classify import (
    ErrorClassification,
    classify_error,
    context_overflow_limit,
    invalidates_continuation_token,
    is_blank_response,
    is_context_overflow,
    is_read_timeout,
    is_retryable,
    is_thinking_binding_rejection,
    may_be_context_overflow,
)
from .facts import failure_facts
from .formatting import EMPTY_EXCEPTION_MESSAGES, clean_error_message
from .kinds import ErrorKind, TimeoutPhase
from .network import RETRYABLE_PHRASES, is_deterministic_connection_error
from .provider import (
    NON_RETRYABLE_PROVIDER_ERROR_CODES,
    ContinuationVerdictError,
    ProviderResponseError,
    ProviderSignal,
    in_band_failure_retryable,
)
from .route import ROUTE_EXTENSION_KEY, Origin, RouteFacts, origin_of, route_of

__all__ = [
    "EMPTY_EXCEPTION_MESSAGES",
    "NON_RETRYABLE_PROVIDER_ERROR_CODES",
    "RETRYABLE_PHRASES",
    "RETRYABLE_STATUS_CODES",
    "RETRYABLE_TYPE_NAMES",
    "ROUTE_EXTENSION_KEY",
    "ContinuationVerdictError",
    "ErrorClassification",
    "ErrorKind",
    "Origin",
    "ProviderResponseError",
    "ProviderSignal",
    "RouteFacts",
    "TimeoutPhase",
    "classify_error",
    "clean_error_message",
    "context_overflow_limit",
    "failure_facts",
    "in_band_failure_retryable",
    "invalidates_continuation_token",
    "is_blank_response",
    "is_context_overflow",
    "is_deterministic_connection_error",
    "is_read_timeout",
    "is_retryable",
    "is_thinking_binding_rejection",
    "iter_explicit_graph",
    "may_be_context_overflow",
    "origin_of",
    "route_of",
]
