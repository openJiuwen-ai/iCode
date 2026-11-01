# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Small event-log builder for trajectory aggregation tests."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from chrys.foundation.trajectory.envelope import (
    SYSTEM_ACTOR,
    Actor,
    EventDraft,
    Link,
    LinkRelation,
    SegmentedField,
    build_event,
    encode_event_line,
    measurement,
)
from chrys.foundation.trajectory.event_types import EventType

RUNTIME_ID = "1" * 32
COVERAGE_ID = "2" * 32
SESSION_ID = "12345678-1234-1234-1234-123456789abc"
BRANCH_ID = "3" * 32
TURN_ID = "4" * 32
NS = 1_000_000_000


def caused_by(operation_id: str) -> tuple[Link, ...]:
    """One ``caused_by`` link to ``operation_id``."""
    return (Link(relation=LinkRelation.CAUSED_BY, target_operation_id=operation_id),)


def installed_events_path(root: Path, *, session_id: str = "abcd1234") -> Path:
    """``root/sessions/<session_id>/trajectory/events.jsonl`` with its parents created.

    The layout is load-bearing: fingerprint-key discovery and the session projection
    resolve ``session.json`` and the config directory relative to this path.
    """
    path = root / "sessions" / session_id / "trajectory" / "events.jsonl"
    path.parent.mkdir(parents=True)
    return path


class EventLog:
    """Address drafts sequentially and write one valid trajectory JSONL file."""

    def __init__(self) -> None:
        self._drafts: list[tuple[EventDraft, str, str]] = []

    @property
    def next_sequence(self) -> int:
        """Sequence the next draft receives with the default write origin."""
        return len(self._drafts) + 1

    def add(
        self,
        event_type: str,
        monotonic_ns: int,
        *,
        turn_id: str | None = TURN_ID,
        operation_id: str | None = None,
        parent_operation_id: str | None = None,
        payload: dict[str, Any] | None = None,
        measurements: dict[str, dict[str, Any]] | None = None,
        links: tuple[Link, ...] = (),
        segmented_fields: tuple[SegmentedField, ...] = (),
        runtime_id: str = RUNTIME_ID,
        branch_id: str = BRANCH_ID,
        actor: Actor = SYSTEM_ACTOR,
        occurred_at: str | None = None,
    ) -> EventDraft:
        clock: dict[str, str] = {} if occurred_at is None else {"occurred_at": occurred_at}
        draft = EventDraft(
            event_type=event_type,
            actor=actor,
            turn_id=turn_id,
            operation_id=operation_id,
            parent_operation_id=parent_operation_id,
            payload=payload or {},
            measurements=measurements or {},
            links=links,
            segmented_fields=segmented_fields,
            monotonic_ns=monotonic_ns,
            **clock,
        )
        self._drafts.append((draft, runtime_id, branch_id))
        return draft

    def coverage(self) -> None:
        self.add(EventType.COVERAGE_STARTED, 0, turn_id=None, payload={"coverage_reason": "session_started"})

    def span(
        self,
        family: str,
        operation_id: str,
        start_ns: int,
        end_ns: int,
        *,
        turn_id: str | None = TURN_ID,
        parent_operation_id: str | None = None,
        start_payload: dict[str, Any] | None = None,
        finish_payload: dict[str, Any] | None = None,
        links: tuple[Link, ...] = (),
        runtime_id: str = RUNTIME_ID,
        branch_id: str = BRANCH_ID,
        actor: Actor = SYSTEM_ACTOR,
    ) -> None:
        self.add(
            f"{family}.started",
            start_ns,
            turn_id=turn_id,
            operation_id=operation_id,
            parent_operation_id=parent_operation_id,
            payload=start_payload,
            links=links,
            runtime_id=runtime_id,
            branch_id=branch_id,
            actor=actor,
        )
        payload = {"outcome": "success", "duration_ms": max(0, (end_ns - start_ns) // 1_000_000)}
        payload.update(finish_payload or {})
        self.add(
            f"{family}.finished",
            end_ns,
            turn_id=turn_id,
            operation_id=operation_id,
            parent_operation_id=parent_operation_id,
            payload=payload,
            measurements={"/payload/duration_ms": measurement("monotonic_clock", method_version=1)},
            runtime_id=runtime_id,
            branch_id=branch_id,
            actor=actor,
        )

    def turn(self, start_ns: int, end_ns: int, *, end_reason: str = "cancelled") -> None:
        self.add(EventType.TURN_STARTED, start_ns, payload={"turn_number": 1})
        self.add(EventType.TURN_FINISHED, end_ns, payload={"end_reason": end_reason, "duration_ms": 0})

    def settled(
        self,
        monotonic_ns: int,
        *,
        turn_id: str = TURN_ID,
        waited_hook_ids: list[str] | None = None,
        drained_scopes: list[str] | None = None,
    ) -> None:
        waited = waited_hook_ids or []
        drained = ["turn"] if drained_scopes is None else drained_scopes
        group_id = "5" * 32
        marker = self.add(
            EventType.TURN_RESPONSE_SETTLED,
            monotonic_ns,
            turn_id=turn_id,
            payload={"outcome": "settled", "drained_scopes": drained, "waited_hook_operation_count": len(waited)},
            segmented_fields=(
                SegmentedField(
                    field_pointer="/payload/waited_hook_operation_ids",
                    segment_group_id=group_id,
                    segment_count=1,
                ),
            ),
        )
        self.add(
            EventType.SEGMENT,
            monotonic_ns,
            turn_id=turn_id,
            operation_id=None,
            payload={
                "parent_event_id": marker.event_id,
                "field_pointer": "/payload/waited_hook_operation_ids",
                "segment_group_id": group_id,
                "segment_index": 0,
                "segment_count": 1,
                "encoding": "array_slice",
                "entries": waited,
            },
        )

    def exchange_usage(
        self,
        operation_id: str,
        start_ns: int,
        end_ns: int,
        normalized: dict[str, int],
        *,
        provider_buckets: tuple[str, ...] | None = None,
        parent_operation_id: str | None = None,
        start_payload: dict[str, object] | None = None,
        actor: Actor = SYSTEM_ACTOR,
        outcome: str = "success",
    ) -> None:
        """One model exchange whose terminal reports normalized usage with provider provenance."""
        buckets = tuple(normalized) if provider_buckets is None else provider_buckets
        self.add(
            "model.exchange.started",
            start_ns,
            operation_id=operation_id,
            parent_operation_id=parent_operation_id,
            payload=start_payload,
            actor=actor,
        )
        self.add(
            "model.exchange.finished",
            end_ns,
            operation_id=operation_id,
            parent_operation_id=parent_operation_id,
            payload={
                "outcome": outcome,
                "duration_ms": max(1, (end_ns - start_ns) // 1_000_000),
                "usage": {"normalized": normalized},
            },
            measurements={
                "/payload/duration_ms": {"source": "monotonic_clock", "method_version": 1},
                **{
                    f"/payload/usage/normalized/{bucket}": {"source": "provider", "adapter_version": 1}
                    for bucket in buckets
                },
            },
            actor=actor,
        )

    def resolved_rollback(
        self,
        superseded_from_sequence: int,
        monotonic_ns: int,
        *,
        target_turn_id: str | None = None,
    ) -> None:
        """Roll back every draft from ``superseded_from_sequence`` through the last one onto a new branch."""
        old_branch = "3" * 32
        new_branch = "6" * 32
        payload: dict[str, object] = {
            "old_branch_id": old_branch,
            "new_branch_id": new_branch,
            "superseded_from_sequence": superseded_from_sequence,
            "superseded_to_sequence": self.next_sequence - 1,
        }
        if target_turn_id is not None:
            payload["target_turn_id"] = target_turn_id
        self.add(EventType.SESSION_ROLLBACK, monotonic_ns, turn_id=None, branch_id=new_branch, payload=payload)
        self.add(
            EventType.BRANCH_SUPERSEDED,
            monotonic_ns,
            turn_id=None,
            branch_id=new_branch,
            payload={"branch_id": old_branch, "superseded_by": new_branch},
        )

    def write(self, path: Path, *, start_sequence: int = 1) -> None:
        path.write_bytes(
            b"".join(
                encode_event_line(
                    build_event(
                        draft,
                        sequence=sequence,
                        runtime_id=runtime_id,
                        coverage_id=COVERAGE_ID,
                        session_id=SESSION_ID,
                        branch_id=branch_id,
                    )
                )
                for sequence, (draft, runtime_id, branch_id) in enumerate(self._drafts, start=start_sequence)
            )
        )


def operation_index(turn, family: str, operation_id: str) -> int:
    """Index of the ``family``/``operation_id`` row in a resolved turn's operations."""
    for index, operation in enumerate(turn.operations):
        if operation.family == family and operation.operation_id == operation_id:
            return index
    raise AssertionError(f"{family}:{operation_id} has no operations row")
