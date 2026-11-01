# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Ambient trajectory context: who is recording, for which turn, under which operation.

The engine binds a :class:`TrajectoryContext` for the duration of a model run;
the kernel loop, the LLM wire client, tool middleware and side calls
read it back through :func:`current_trajectory` and narrow it (cycle, actor)
with :func:`bind_trajectory`. Propagation rides on :mod:`contextvars`, so a
task spawned for a parallel tool call or an in-process sub-agent inherits the
binding it was created under, while a sub-agent that rebinds its own actor
never leaks into its parent.

Every producer treats a missing context as "trajectory disabled" and does
nothing — the contract never makes a caller fail because recording is off.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from contextvars import ContextVar, Token
from dataclasses import dataclass, field, replace
from typing import Any, Protocol

from chrys.foundation.trajectory.envelope import (
    Actor,
    ActorKind,
    ActorRole,
    EventDraft,
    MeasurementSource,
    measurement,
    monotonic_now_ns,
    utc_now_rfc3339,
)
from chrys.foundation.trajectory.event_types import EventType, ExchangeOutcome
from chrys.foundation.trajectory.finalizers import RecordingFinalizers
from chrys.foundation.trajectory.ids import ANALYTICS_ID_HEX_LENGTH
from chrys.foundation.trajectory.revisions import RevisionRegistry
from chrys.foundation.trajectory.writer import EmitResult


class TrajectorySink(Protocol):
    """Where events go: the per-session recorder owned by the engine."""

    async def emit(
        self, draft: EventDraft, *, payload_factory: Callable[[int], Mapping[str, Any]] | None = None
    ) -> EmitResult: ...

    def emit_blocking(
        self, draft: EventDraft, *, payload_factory: Callable[[int], Mapping[str, Any]] | None = None
    ) -> EmitResult: ...

    def emit_soon(
        self, draft: EventDraft, *, payload_factory: Callable[[int], Mapping[str, Any]] | None = None
    ) -> EmitResult | None:
        """Queue the event in order without awaiting its ack (for synchronous hooks).

        ``None`` once the line has taken its sequence — whether it lands is
        the writer's business from there. A result instead means the sink
        answered on the spot and this line will never exist, which is what a
        caller about to close the span it opens has to know.
        """
        ...

    @property
    def fingerprint_key(self) -> bytes | None: ...


def derive_actor_id(session_id: str, role: str, *, invocation_id: str | None = None) -> str:
    """Derive the stable ``actor_id`` of one logical actor.

    The id is a deterministic function of the session and the actor's role
    (plus the invocation for sub-agents), so it survives writer restarts
    without being stored anywhere: the same main agent keeps one id across
    every runtime of its session, and each sub-agent invocation gets exactly
    one. Nothing secret goes in — ``session_id`` already appears on every
    event line — so a plain digest is enough.
    """
    material = f"chrys.trajectory.actor\x00{session_id}\x00{role}\x00{invocation_id or ''}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:ANALYTICS_ID_HEX_LENGTH]


def main_actor(session_id: str) -> Actor:
    return Actor(kind=ActorKind.AGENT, role=ActorRole.MAIN, actor_id=derive_actor_id(session_id, ActorRole.MAIN))


def side_call_actor(session_id: str, role: str) -> Actor:
    return Actor(kind=ActorKind.SIDE_CALL, role=role, actor_id=derive_actor_id(session_id, role))


def sub_agent_actor(session_id: str, invocation_id: str) -> Actor:
    return Actor(
        kind=ActorKind.AGENT,
        role=ActorRole.SUB_AGENT,
        actor_id=derive_actor_id(session_id, ActorRole.SUB_AGENT, invocation_id=invocation_id),
        invocation_id=invocation_id,
    )


def workflow_node_actor(session_id: str, invocation_id: str) -> Actor:
    """A workflow activation has its own revision chain; its invocation uses analytics IDs, not child audit IDs."""
    return Actor(
        kind=ActorKind.AGENT,
        role=ActorRole.WORKFLOW_NODE,
        actor_id=derive_actor_id(session_id, ActorRole.WORKFLOW_NODE, invocation_id=invocation_id),
    )


@dataclass(frozen=True, slots=True)
class TrajectoryContext:
    """The ambient recording scope a producer emits under."""

    sink: TrajectorySink
    session_id: str
    actor: Actor
    turn_id: str | None = None
    run_operation_id: str | None = None
    cycle_operation_id: str | None = None
    exchange_operation_id: str | None = None
    turn_preamble_operation_id: str | None = None
    """Preparation container that causally precedes the turn's first model run."""
    exchange_facts: Mapping[str, Any] = field(default_factory=dict)
    """Static facts every ``model.exchange.*`` under this context carries (profile ids/fingerprints)."""
    revisions: RevisionRegistry = field(default_factory=RevisionRegistry, compare=False)
    """Context-revision chains per actor, shared by every context derived from this one."""
    finalizers: RecordingFinalizers = field(default_factory=RecordingFinalizers, compare=False)
    """Pending operation settlement, shared for the recorder's entire lifetime."""

    def draft(
        self,
        event_type: str,
        *,
        payload: Mapping[str, Any] | None = None,
        operation_id: str | None = None,
        parent_operation_id: str | None = None,
        **kwargs: Any,
    ) -> EventDraft:
        """Build a draft stamped with this context's actor and turn."""
        return EventDraft(
            event_type=event_type,
            actor=self.actor,
            turn_id=self.turn_id,
            operation_id=operation_id,
            parent_operation_id=parent_operation_id,
            payload=payload if payload is not None else {},
            **kwargs,
        )

    def with_cycle(self, cycle_operation_id: str | None) -> TrajectoryContext:
        return replace(self, cycle_operation_id=cycle_operation_id, exchange_operation_id=None)

    def with_exchange(self, exchange_operation_id: str | None) -> TrajectoryContext:
        return replace(self, exchange_operation_id=exchange_operation_id)

    def with_actor(self, actor: Actor) -> TrajectoryContext:
        return replace(self, actor=actor)

    def with_turn(self, turn_id: str | None) -> TrajectoryContext:
        return replace(self, turn_id=turn_id)

    def with_run(self, run_operation_id: str | None) -> TrajectoryContext:
        return replace(self, run_operation_id=run_operation_id, cycle_operation_id=None, exchange_operation_id=None)

    def with_turn_preamble(self, operation_id: str | None) -> TrajectoryContext:
        """Name the preparation container that precedes this turn's first model run."""
        return replace(self, turn_preamble_operation_id=operation_id)

    def with_exchange_facts(self, facts: Mapping[str, Any]) -> TrajectoryContext:
        return replace(self, exchange_facts=dict(facts))

    @property
    def innermost_model_operation_id(self) -> str | None:
        """The closest model operation a child event should hang under."""
        return self.exchange_operation_id or self.cycle_operation_id or self.run_operation_id


_CURRENT: ContextVar[TrajectoryContext | None] = ContextVar("chrys_trajectory_context", default=None)


def current_trajectory() -> TrajectoryContext | None:
    """Return the ambient recording context, or ``None`` when recording is off."""
    return _CURRENT.get()


def bind_trajectory(context: TrajectoryContext | None) -> Token[TrajectoryContext | None]:
    """Install *context* for the current task; reset with :func:`reset_trajectory`."""
    return _CURRENT.set(context)


def reset_trajectory(token: Token[TrajectoryContext | None]) -> None:
    _CURRENT.reset(token)


class trajectory_scope:
    """``with trajectory_scope(ctx):`` binds and restores the ambient context."""

    __slots__ = ("_context", "_previous", "_token")

    def __init__(self, context: TrajectoryContext | None) -> None:
        self._context = context
        self._token: Token[TrajectoryContext | None] | None = None
        self._previous: TrajectoryContext | None = None

    def __enter__(self) -> TrajectoryContext | None:
        self._previous = _CURRENT.get()
        self._token = _CURRENT.set(self._context)
        return self._context

    def __exit__(self, *_exc: object) -> None:
        token, self._token = self._token, None
        if token is None:
            return
        try:
            _CURRENT.reset(token)
        except ValueError:
            # Entered and exited in different contexts (async generators can
            # do this): restore the value we saw instead of failing the caller.
            _CURRENT.set(self._previous)


_CURRENT_TOOL_OPERATION: ContextVar[str | None] = ContextVar("chrys_trajectory_tool_operation", default=None)


def side_call_scope(role: str, *, context: TrajectoryContext | None = None) -> trajectory_scope:
    """``with side_call_scope(role):`` attributes the enclosed model call to the session's *role* side-call actor.

    Rebinding starts from *context* when given, else from the ambient
    context; with neither there is nothing to record and the scope is inert.
    The caller's exchange facts are dropped: they describe the agent whose
    turn this side call serves, not the profile or model the side call runs
    on, and an inherited fact would be recorded as this exchange's own.
    """
    base = context if context is not None else current_trajectory()
    if base is None:
        return trajectory_scope(None)
    return trajectory_scope(base.with_actor(side_call_actor(base.session_id, role)).with_exchange_facts({}))


def current_tool_operation_id() -> str | None:
    """The tool operation whose body is executing in this task, if any.

    Tool bodies that open nested work (a sub-agent delegation) read it to
    hang their boundary under the right operation without the invocation
    context being threaded through the tool signature.
    """
    return _CURRENT_TOOL_OPERATION.get()


def bind_tool_operation(operation_id: str | None) -> Token[str | None]:
    """Install the executing tool operation; reset with :func:`reset_tool_operation`."""
    return _CURRENT_TOOL_OPERATION.set(operation_id)


def reset_tool_operation(token: Token[str | None]) -> None:
    _CURRENT_TOOL_OPERATION.reset(token)


# ------------------------------------------------------------------ exchange


TRAJECTORY_CONTEXT_KWARG = "trajectory_context"
"""``client_kwargs`` key the run owner uses to hand its :class:`TrajectoryContext` to the kernel loop."""

TRAJECTORY_EXCHANGE_KWARG = "trajectory_exchange"
"""``client_kwargs`` key the kernel uses to hand an :class:`ExchangeTrace` to the wire client."""


def _percentile(sorted_values: list[int], fraction: float) -> int:
    if not sorted_values:
        return 0
    index = min(len(sorted_values) - 1, max(0, round((len(sorted_values) - 1) * fraction)))
    return sorted_values[index]


class ExchangeTrace:
    """One provider acquisition — the ``model.exchange.started/finished`` pair.

    The kernel creates one per wire attempt (a wire retry is a new exchange)
    and hands it to the wire client through ``client_kwargs``; the client
    reports the request facts at :meth:`started`, every streamed chunk at
    :meth:`chunk_observed` and the terminal :meth:`finished`. Both markers
    are queued in order without awaiting the ack, because the client's
    stream hooks are synchronous; :meth:`finished` is idempotent so the
    kernel can close an abandoned exchange the client never finished.
    """

    __slots__ = (
        "_chunk_count",
        "_context_revision_id",
        "_finished",
        "_first_chunk_ns",
        "_first_visible_ns",
        "_gaps_ms",
        "_last_chunk_ns",
        "_outcome_override",
        "_request_model",
        "_stall_count",
        "_started",
        "_started_at",
        "_started_ns",
        "context",
    )

    def __init__(self, context: TrajectoryContext) -> None:
        if context.exchange_operation_id is None:
            raise ValueError("ExchangeTrace requires a context narrowed to an exchange operation.")
        self.context = context
        self._reset_state()

    def _reset_state(self) -> None:
        self._started = False
        self._finished = False
        self._started_ns = 0
        self._started_at = ""
        self._first_chunk_ns: int | None = None
        self._first_visible_ns: int | None = None
        self._last_chunk_ns: int | None = None
        self._chunk_count = 0
        self._gaps_ms: list[int] = []
        self._stall_count = 0
        self._outcome_override: str | None = None
        self._context_revision_id: str | None = None
        self._request_model: object = None

    @property
    def operation_id(self) -> str:
        if self.context.exchange_operation_id is None:
            raise RuntimeError("An exchange scope requires an operation id.")
        return self.context.exchange_operation_id

    @property
    def is_started(self) -> bool:
        return self._started

    @property
    def is_finished(self) -> bool:
        return self._finished

    @property
    def context_revision_id(self) -> str | None:
        return self._context_revision_id

    def set_context_revision(self, revision_id: str | None) -> None:
        """Name the context revision this acquisition sent (carried by both markers)."""
        self._context_revision_id = revision_id

    def _revision_facts(self) -> dict[str, Any]:
        if self._context_revision_id is None:
            return {}
        return {"context_revision_id": self._context_revision_id}

    def started(self, *, payload: Mapping[str, Any] | None = None) -> None:
        """Record the request start (once); *payload* carries the request facts."""
        if self._started:
            return
        self._started = True
        self._started_ns = monotonic_now_ns()
        self._started_at = utc_now_rfc3339()
        data: dict[str, Any] = {
            **self.context.exchange_facts,
            **self._revision_facts(),
            **(payload or {}),
            "started_at": self._started_at,
        }
        self._request_model = data.get("request_model")
        self.context.sink.emit_soon(
            self.context.draft(
                EventType.MODEL_EXCHANGE_STARTED,
                operation_id=self.operation_id,
                parent_operation_id=self.context.cycle_operation_id or self.context.run_operation_id,
                payload=data,
                occurred_at=self._started_at,
                monotonic_ns=self._started_ns,
            )
        )

    def chunk_observed(self, *, visible: bool) -> None:
        """Note one streamed chunk; *visible* = user-visible output (text) arrived in it."""
        now = monotonic_now_ns()
        if self._first_chunk_ns is None:
            self._first_chunk_ns = now
        elif self._last_chunk_ns is not None:
            self._gaps_ms.append(max(0, (now - self._last_chunk_ns) // 1_000_000))
        if visible and self._first_visible_ns is None:
            self._first_visible_ns = now
        self._last_chunk_ns = now
        self._chunk_count += 1

    def stall_observed(self) -> None:
        self._stall_count += 1

    def mark_outcome(self, outcome: str) -> None:
        """Pin the terminal outcome the owner knows better than the client (stalled, interrupted)."""
        self._outcome_override = outcome

    def finished(
        self,
        *,
        outcome: str,
        payload: Mapping[str, Any] | None = None,
        measurements: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> None:
        """Record the terminal marker (once) with the timing derived here."""
        if self._finished:
            return
        self._finished = True
        if not self._started:
            # No start marker means no acquisition: the client reports the
            # start immediately before the call, so anything that ends the
            # trace first (a cancel while the request is still being prepared)
            # ends an exchange that never reached the provider. Closing it
            # would write a terminal against a start nothing recorded, which
            # is the one span shape a reader cannot tell from corruption.
            return
        now = monotonic_now_ns()
        ended_at = utc_now_rfc3339()
        data: dict[str, Any] = {
            **self.context.exchange_facts,
            **self._revision_facts(),
            **(payload or {}),
            "outcome": self._outcome_override or outcome,
            "started_at": self._started_at,
            "ended_at": ended_at,
            "duration_ms": max(0, (now - self._started_ns) // 1_000_000),
            "chunk_count": self._chunk_count,
            "stall_count": self._stall_count,
        }
        if "response_model" not in data and self._request_model is not None:
            # A response that named no model, or a failed one, is still the model the start marker
            # asked (a per-request override, not the profile's): its tokens count under it.
            data["request_model"] = self._request_model
        clock = measurement(MeasurementSource.MONOTONIC_CLOCK, method_version=1)
        provenance: dict[str, Mapping[str, Any]] = {"/payload/duration_ms": clock}
        if self._first_chunk_ns is not None:
            data["ttfc_ms"] = max(0, (self._first_chunk_ns - self._started_ns) // 1_000_000)
            provenance["/payload/ttfc_ms"] = clock
        if self._first_visible_ns is not None:
            data["ttfv_ms"] = max(0, (self._first_visible_ns - self._started_ns) // 1_000_000)
            provenance["/payload/ttfv_ms"] = clock
        if self._gaps_ms:
            ordered = sorted(self._gaps_ms)
            data["inter_chunk_p50_ms"] = _percentile(ordered, 0.5)
            data["inter_chunk_p95_ms"] = _percentile(ordered, 0.95)
            data["max_chunk_gap_ms"] = ordered[-1]
            provenance["/payload/inter_chunk_p50_ms"] = clock
            provenance["/payload/inter_chunk_p95_ms"] = clock
            provenance["/payload/max_chunk_gap_ms"] = clock
        if measurements:
            provenance.update(measurements)
        self.context.sink.emit_soon(
            self.context.draft(
                EventType.MODEL_EXCHANGE_FINISHED,
                operation_id=self.operation_id,
                parent_operation_id=self.context.cycle_operation_id or self.context.run_operation_id,
                payload=data,
                measurements=provenance,
                occurred_at=ended_at,
                monotonic_ns=now,
            )
        )

    def abandon(self) -> None:
        """Close an exchange the client never finished (stream dropped before its end)."""
        self.finished(outcome=ExchangeOutcome.ABANDONED)
