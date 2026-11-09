# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Reusable run-retry loop.

Shared by :class:`chrys.orchestration.invoker.attempts.AttemptRunner` so the main agent and
sub-agents can share one implementation of:

- per-attempt retry on :class:`StreamStall` and caller-defined transient errors
- exponential backoff with interruptible sleep
- history snapshot/restore between attempts
- retry event publishing (caller supplies the payload and bound invocation
  publisher, including the explicit retry observation scope)

The loop is intentionally un-opinionated about event types, history shape,
and "what counts as retryable" — everything is injected via callbacks.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, NamedTuple

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping, Sequence

    from chrys.foundation.models.failures import FailureFacts

logger = logging.getLogger(__name__)

TRANSIENT_RETRY_BACKOFF_SECONDS: tuple[int, ...] = (3, 7, 15, 30, 60, 120, 240, 600)
"""Seconds before each transient retry of a model request, a whole run or an ACP connection; later
retries wait the last value. Chat, sub-agents and workflow nodes all default to it, so they back off alike."""


@dataclass(frozen=True, slots=True)
class RetryAttemptInfo:
    """Metadata for a retry that is about to sleep before the next attempt.

    ``attempt`` and ``max_attempts`` are one-based retry counts exposed to
    users, not total provider-call counts including the initial call.
    """

    reason: str
    attempt: int
    max_attempts: int
    delay_seconds: float
    facts: FailureFacts | None = None
    """The classifier's verdict when a provider failure is being retried; None for a retry on iCode's own check."""


class StreamStall(Exception):
    """Raised when a streaming attempt times out waiting for the next chunk."""


class StreamStallExhausted(Exception):
    """Every retry attempt hit :class:`StreamStall`.

    The original stall is chained via ``__cause__``. Callers typically
    fall back to a non-streaming request when they see this.
    """


class HistorySnapshot(NamedTuple):
    """Immutable snapshot of history state for retry rollback.

    ``messages`` is a shallow list copy taken before the attempt; the
    restore callback installs a fresh shallow copy on every rollback so
    later attempts cannot mutate the reusable snapshot. This also undoes
    mid-attempt compressions. ``compressed_count`` is the pre-attempt length of
    ``compressed_msgs`` — anything appended during the rolled-back
    attempt is truncated back to this length. ``service_session_id`` is
    provider-side continuation state; restoring it prevents a retry from
    continuing an incomplete server-side attempt.

    ``message_properties`` captures each message's ``additional_properties``
    (same order as ``messages``) so rollback can restore them *exactly*: the
    Message objects are shared between the snapshot list and the live wire
    view, so a failed attempt's in-place annotations (``_excluded`` flags from
    automatic compaction, ``_group`` summarized-by markers, token counts)
    survive the list swap and must be reverted value-by-value. An exact
    restore both clears the failed attempt's marks and preserves marks that
    legitimately predate the attempt — a reason-based clearing heuristic can
    do only one or the other.

    ``pre_output_history_len`` is request-time compaction's transient
    current-turn metadata floor.  Retry rollback must restore it with the
    rest of the history state, otherwise a floor written by a failed attempt
    can scope the successful retry's post-run metadata to the wrong slice.

    ``caller_state`` is an opaque snapshot owned by the callback provider.
    The reusable retry loop never inspects it; main-agent callers use it to
    roll back auxiliary per-run recorders atomically with history.
    """

    messages: list
    compressed_count: int
    service_session_id: str = ""
    message_properties: tuple[dict[str, Any], ...] = ()
    pre_output_history_len: int | None = None
    caller_state: object | None = None


def snapshot_message_properties(messages: Sequence[Any]) -> tuple[dict[str, Any], ...]:
    """Capture ``additional_properties`` of each message for exact rollback.

    Copies one nested level: top-level dict values (e.g. the ``_group``
    annotation dict, which compaction helpers mutate in place) are copied as
    fresh dicts so a failed attempt cannot mutate the snapshot through the
    shared message objects. Deeper values are annotation scalars/lists that
    are replaced, never mutated, by the compaction stack.
    """
    return tuple(_copy_message_properties(msg.additional_properties) for msg in messages)


def restore_message_properties(messages: Sequence[Any], properties: Sequence[Mapping[str, Any]]) -> None:
    """Restore each message's ``additional_properties`` from a snapshot.

    Copy-on-restore: the same snapshot can be restored multiple times (the
    retry loop rolls back before *every* retry), so the live messages must
    never be handed the snapshot dicts themselves.
    """
    for msg, props in zip(messages, properties, strict=True):
        msg.additional_properties = _copy_message_properties(props)


def _copy_message_properties(props: Mapping[str, Any]) -> dict[str, Any]:
    return {key: dict(value) if isinstance(value, dict) else value for key, value in props.items()}


class StreamRetryLoop[T]:
    """Run an attempt with retry, backoff, and rollback.

    All policy is injected via callbacks so the same loop drives both the
    main :class:`~chrys.orchestration.engine.run.bindings.TurnBindings` path and the per-sub-agent runner.
    ``may_retry`` vetoes a retry that would repeat work the failed attempt
    already committed or showed.
    """

    def __init__(
        self,
        *,
        max_retries: int,
        backoff_schedule: Sequence[int],
        is_retryable: Callable[[BaseException], bool],
        snapshot_history: Callable[[], HistorySnapshot],
        restore_history: Callable[[HistorySnapshot], None],
        publish_retry_attempt: Callable[[str, int, int, int, BaseException], Awaitable[None]],
        is_interrupted: Callable[[], bool],
        interruptible_sleep: Callable[[int], Awaitable[bool]],
        clean_error_message: Callable[[BaseException], str],
        restore_on_stall_exhaustion: bool = False,
        after_restore: Callable[[], None] | None = None,
        may_retry: Callable[[BaseException], bool] | None = None,
    ) -> None:
        self._max_retries = max_retries
        self._backoff = tuple(backoff_schedule)
        self._is_retryable = is_retryable
        self._snapshot_history = snapshot_history
        self._restore_history = restore_history
        self._publish_retry = publish_retry_attempt
        self._is_interrupted = is_interrupted
        self._sleep = interruptible_sleep
        self._clean_err = clean_error_message
        self._restore_on_stall_exhaustion = restore_on_stall_exhaustion
        self._after_restore = after_restore
        self._may_retry = may_retry

    def _restore_attempt(self, snapshot: HistorySnapshot) -> None:
        """Restore history and caller-owned retry state without an await gap."""
        self._restore_history(snapshot)
        if self._after_restore is not None:
            self._after_restore()

    def _delay(self, attempt: int) -> int:
        if not self._backoff:
            return 0
        return self._backoff[min(attempt, len(self._backoff) - 1)]

    async def run(self, attempt_fn: Callable[[], Awaitable[T]]) -> T:
        """Drive ``attempt_fn`` with retry.

        Returns the attempt's result on success. On non-retryable
        exceptions or exhausted retries re-raises the original exception.
        When every attempt ended in :class:`StreamStall` raises
        :class:`StreamStallExhausted` so callers can distinguish "stream
        won't move" from ordinary transient errors (and e.g. fall back to
        a blocking request).

        The loop captures the history snapshot once on entry and restores
        it before every retry. If the interrupt callback fires during an
        attempt or during backoff, the loop raises
        :class:`asyncio.CancelledError` (matching the main pass observer's
        original semantics).
        """
        snapshot = self._snapshot_history()

        transient_attempt = 0
        while True:
            if self._is_interrupted():
                raise asyncio.CancelledError

            err_msg: str
            last_exc: BaseException
            retry_attempt: int
            retry_max_attempts: int
            delay: int

            try:
                return await attempt_fn()

            except StreamStall as e:
                if self._may_retry is not None and not self._may_retry(e):
                    raise RuntimeError(
                        "Streaming stalled after this run already showed output or completed tool work; "
                        "stopping it to keep what was done."
                    ) from e
                if transient_attempt >= self._max_retries:
                    # The main AttemptRunner policy replaces an exhausted stream with a
                    # blocking request and opts into one final rollback so the
                    # replacement starts clean. Other callers (notably the
                    # sub-agent pause/retry flow) retain their historical stall
                    # semantics and do not discard completed attempt work.
                    if self._restore_on_stall_exhaustion:
                        self._restore_attempt(snapshot)
                    raise StreamStallExhausted from e
                err_msg = "Stream stalled"
                last_exc = e
                retry_attempt = transient_attempt + 1
                retry_max_attempts = self._max_retries
                delay = self._delay(transient_attempt)
                transient_attempt += 1
                logger.warning("Stream stall on attempt %d, retrying", retry_attempt)

            except Exception as e:
                if not self._is_retryable(e) or (self._may_retry is not None and not self._may_retry(e)):
                    raise
                if transient_attempt >= self._max_retries:
                    raise
                last_exc = e
                err_msg = self._clean_err(e)
                retry_attempt = transient_attempt + 1
                retry_max_attempts = self._max_retries
                delay = self._delay(transient_attempt)
                transient_attempt += 1

            logger.warning(
                "Retrying in %ds (attempt %d/%d): %s",
                delay,
                retry_attempt,
                retry_max_attempts,
                err_msg,
            )
            # Caller-owned state (notably LoopRecorder and injection replay)
            # must be restored in the same synchronous phase as history.
            # Waiting until the next attempt would let an interrupt during
            # retry-event delivery or backoff discard already-consumed input.
            self._restore_attempt(snapshot)
            await self._publish_retry(err_msg, retry_attempt, retry_max_attempts, delay, last_exc)
            if await self._sleep(delay):
                raise asyncio.CancelledError
