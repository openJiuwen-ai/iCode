# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""What a classified failure is, carried on the events that report it."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from chrys.foundation.errors.kinds import ErrorKind


@dataclass(frozen=True, slots=True)
class FailureFacts:
    """The classifier's verdict on one failure, for a frontend that acts on it rather than only shows it.

    Built only by :func:`chrys.foundation.errors.failure_facts`, from the
    exception itself; the event's raw ``message`` stays the evidence.
    """

    kind: ErrorKind
    retryable: bool
    """Whether trying again later may succeed without changing anything: a transient failure, not one to fix first.

    True also when iCode's own retries already gave up on it (a rate limit
    outlasting the backoff, a stalled stream); False when credit, key,
    request or configuration must change first, or nothing shows it transient.
    """
    status_code: int | None = None
    """The HTTP status the model service answered with; None when it never answered or named none."""
