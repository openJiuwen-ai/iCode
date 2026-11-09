# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The classifier's verdict on a failure, as the facts its events carry."""

from __future__ import annotations

import logging

from chrys.foundation.models.failures import FailureFacts

from .classify import classify_error
from .provider import is_2xx

logger = logging.getLogger(__name__)


def failure_facts(exc: BaseException) -> FailureFacts | None:
    """Return what *exc* is for a frontend that acts on it; None on any internal error.

    ``retryable`` is the classifier's ``transient``, not its lane policy: a
    person retrying later starts a new attempt, so a failure only an owner's
    exhausted budget concluded still reads as one that may pass. The status is
    the model service's own error status, kept only when the model service
    answered with one: another client's HTTP status (an MCP server's 401) never
    reads as the model's, nor does the 200 a stream that reported its failure
    in-band was sent under (Anthropic's ``overloaded_error`` event).
    """
    try:
        result = classify_error(exc)
    except Exception:
        logger.debug("Classifying an error for its facts failed", exc_info=True)
        return None
    signal = result.signal
    status = signal.status_code if signal is not None and result.from_model_service else None
    if is_2xx(status):
        status = None
    return FailureFacts(kind=result.kind, retryable=result.transient, status_code=status)
