# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Event envelope, typed sub-structures, and line (de)serialization.

Every trajectory event shares one envelope; type-specific fields live under
``payload`` and field-level provenance under ``measurements`` (JSON pointers
rooted at ``/payload/...``). Optional keys are **omitted**, never ``null``.
``occurred_at`` is display-only wall-clock time; ``monotonic_ns`` is the sole
basis for ordering, overlap, and duration within one ``runtime_id``.
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final

from chrys.foundation.trajectory.ids import is_id_field, is_valid_id, new_analytics_id

SCHEMA_VERSION: Final = 1
"""Envelope schema version; bumped on every incompatible change."""

LINE_BUDGET_BYTES: Final = 4096
"""Schema/memory budget for one encoded line (envelope + newline, UTF-8)."""

INT64_MIN: Final = -(2**63)
INT64_MAX: Final = 2**63 - 1

SEGMENT_EVENT_TYPE: Final = "event.segment"


class EnvelopeError(ValueError):
    """Raised when a line cannot be decoded as a valid event envelope."""


class ActorKind:
    """Values for ``actor.kind``."""

    AGENT = "agent"
    SIDE_CALL = "side_call"
    SYSTEM = "system"


class ActorRole:
    """Values for ``actor.role``."""

    MAIN = "main"
    SUB_AGENT = "sub_agent"
    WORKFLOW_NODE = "workflow_node"
    APPROVAL_JUDGE = "approval_judge"
    COMPACTION = "compaction"
    TITLE_GEN = "title_gen"
    COMPLETER = "completer"
    SIDE_QUESTION = "side_question"


class LinkRelation:
    """Values for ``links[].relation``."""

    PARENT = "parent"
    CAUSED_BY = "caused_by"
    RETRY_OF = "retry_of"
    SUPERSEDES = "supersedes"
    CONTINUED_BY = "continued_by"
    VALIDATES = "validates"
    INLINES = "inlines"
    BOUNDARY_OF = "boundary_of"


class MeasurementSource:
    """Values for ``measurements[<pointer>].source``."""

    PROVIDER = "provider"
    MONOTONIC_CLOCK = "monotonic_clock"
    WALL_CLOCK = "wall_clock"
    LOCAL_TOKENIZER = "local_tokenizer"
    TEXT_PARSER = "text_parser"
    USER_ANNOTATION = "user_annotation"
    DERIVED_FROM_SESSION = "derived_from_session"


@dataclass(frozen=True, slots=True)
class Actor:
    """Who produced the event: a main/sub agent, a side call, or the system."""

    kind: str
    role: str
    actor_id: str | None = None
    invocation_id: str | None = None

    def to_dict(self) -> dict[str, str]:
        data = {"kind": self.kind, "role": self.role}
        if self.actor_id is not None:
            data["actor_id"] = self.actor_id
        if self.invocation_id is not None:
            data["invocation_id"] = self.invocation_id
        return data


SYSTEM_ACTOR: Final = Actor(kind=ActorKind.SYSTEM, role=ActorRole.MAIN)


@dataclass(frozen=True, slots=True)
class Link:
    """Typed relation from this event's operation to another operation/item."""

    relation: str
    target_operation_id: str
    target_item_id: str | None = None

    def to_dict(self) -> dict[str, str]:
        data = {"relation": self.relation, "target_operation_id": self.target_operation_id}
        if self.target_item_id is not None:
            data["target_item_id"] = self.target_item_id
        return data


@dataclass(frozen=True, slots=True)
class SegmentedField:
    """Declaration on a base event that one field continues in segment events."""

    field_pointer: str
    segment_group_id: str
    segment_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "field_pointer": self.field_pointer,
            "segment_group_id": self.segment_group_id,
            "segment_count": self.segment_count,
        }


def measurement(
    source: str, *, method_version: int | None = None, adapter_version: int | None = None
) -> dict[str, Any]:
    """Build one field-level provenance record."""
    data: dict[str, Any] = {"source": source}
    if method_version is not None:
        data["method_version"] = method_version
    if adapter_version is not None:
        data["adapter_version"] = adapter_version
    return data


def utc_now_rfc3339() -> str:
    """Return the current UTC time as RFC3339 with a ``Z`` suffix."""
    return rfc3339_from_datetime(datetime.now(UTC))


def rfc3339_from_datetime(value: datetime) -> str:
    """Format an aware datetime as RFC3339 UTC with a ``Z`` suffix."""
    naive = value.tzinfo is None or value.utcoffset() is None
    value = value.replace(tzinfo=UTC) if naive else value.astimezone(UTC)
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def monotonic_now_ns() -> int:
    """Return the process monotonic clock in nanoseconds."""
    return time.monotonic_ns()


_OPAQUE_SUBTREE_KEYS: Final = frozenset({"provider_reported", "value"})
"""Subtrees mirroring foreign data verbatim; their keys are not Chrys IDs."""


def _without_nulls(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Return *payload* with every ``None`` member dropped, recursively.

    An optional field says what it has to say by being there. A ``null``
    reads as a value to a consumer testing for the key — a chain root would
    look like it has a parent, and an exchange with no provider usage would
    hand back something to iterate — so absence is written the way the
    envelope writes absence everywhere else: not at all. List members keep
    their positions; only mapping entries are dropped, and an opaque subtree
    is mirrored as its source reported it, nulls included.
    """
    return {
        key: (value if key in _OPAQUE_SUBTREE_KEYS else _pruned(value))
        for key, value in payload.items()
        if value is not None
    }


def _pruned(value: Any) -> Any:
    if isinstance(value, Mapping):
        return _without_nulls(value)
    if isinstance(value, list | tuple):
        return [_pruned(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class EventDraft:
    """Caller-supplied part of an event; the writer assigns identity and sequence.

    Timestamps default to the moment the draft is built, so callers that create
    a draft at an operation boundary capture that boundary — never the write
    completion — in ``occurred_at`` / ``monotonic_ns``.

    ``event_id`` is minted per draft so a caller can reference the event (for
    example from its ``event.segment`` children) before it is written.
    """

    event_type: str
    event_id: str = field(default_factory=new_analytics_id)
    actor: Actor = SYSTEM_ACTOR
    payload: Mapping[str, Any] = field(default_factory=dict)
    measurements: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    turn_id: str | None = None
    operation_id: str | None = None
    parent_operation_id: str | None = None
    links: tuple[Link, ...] = ()
    segmented_fields: tuple[SegmentedField, ...] = ()
    occurred_at: str = field(default_factory=utc_now_rfc3339)
    monotonic_ns: int = field(default_factory=monotonic_now_ns)

    def __post_init__(self) -> None:
        # Producers build payloads by filling a dict, and the field a caller
        # had nothing to say about arrives as ``None``. Dropping it here is
        # the one place every producer passes through — the writer rebuilds
        # a draft for its ``payload_factory`` events too.
        object.__setattr__(self, "payload", _without_nulls(self.payload))


@dataclass(frozen=True, slots=True)
class TrajectoryEvent:
    """One fully addressed event as written to, or read from, the log."""

    schema_version: int
    event_id: str
    sequence: int
    runtime_id: str
    coverage_id: str
    session_id: str
    branch_id: str
    actor: Actor
    event_type: str
    occurred_at: str
    monotonic_ns: int
    payload: Mapping[str, Any]
    measurements: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    turn_id: str | None = None
    operation_id: str | None = None
    parent_operation_id: str | None = None
    links: tuple[Link, ...] = ()
    segmented_fields: tuple[SegmentedField, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON object form with optional keys omitted."""
        data: dict[str, Any] = {
            "schema_version": self.schema_version,
            "event_id": self.event_id,
            "sequence": self.sequence,
            "runtime_id": self.runtime_id,
            "coverage_id": self.coverage_id,
            "session_id": self.session_id,
            "branch_id": self.branch_id,
        }
        if self.turn_id is not None:
            data["turn_id"] = self.turn_id
        data["actor"] = self.actor.to_dict()
        data["event_type"] = self.event_type
        data["occurred_at"] = self.occurred_at
        data["monotonic_ns"] = self.monotonic_ns
        if self.operation_id is not None:
            data["operation_id"] = self.operation_id
        if self.parent_operation_id is not None:
            data["parent_operation_id"] = self.parent_operation_id
        if self.links:
            data["links"] = [link.to_dict() for link in self.links]
        if self.segmented_fields:
            data["segmented_fields"] = [item.to_dict() for item in self.segmented_fields]
        data["payload"] = dict(self.payload)
        if self.measurements:
            data["measurements"] = {pointer: dict(value) for pointer, value in self.measurements.items()}
        return data


def build_event(
    draft: EventDraft,
    *,
    sequence: int,
    runtime_id: str,
    coverage_id: str,
    session_id: str,
    branch_id: str,
) -> TrajectoryEvent:
    """Address a draft with writer-owned identity fields."""
    return TrajectoryEvent(
        schema_version=SCHEMA_VERSION,
        event_id=draft.event_id,
        sequence=sequence,
        runtime_id=runtime_id,
        coverage_id=coverage_id,
        session_id=session_id,
        branch_id=branch_id,
        actor=draft.actor,
        event_type=draft.event_type,
        occurred_at=draft.occurred_at,
        monotonic_ns=draft.monotonic_ns,
        payload=draft.payload,
        measurements=draft.measurements,
        turn_id=draft.turn_id,
        operation_id=draft.operation_id,
        parent_operation_id=draft.parent_operation_id,
        links=draft.links,
        segmented_fields=draft.segmented_fields,
    )


def encode_json_value(value: Any) -> bytes:
    """Encode one JSON value compactly as UTF-8 bytes (no trailing newline).

    ``errors="backslashreplace"`` keeps the encode total: a lone surrogate
    (an ``os.fsdecode`` artifact) becomes its ASCII escape instead of raising
    inside the writer thread.
    """
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    return text.encode("utf-8", errors="backslashreplace")


def encode_json_line(data: Mapping[str, Any]) -> bytes:
    """Encode one JSON object as a compact UTF-8 line ending in ``\\n``."""
    return encode_json_value(data) + b"\n"


def encode_event_line(event: TrajectoryEvent) -> bytes:
    """Serialize an event to its on-disk line."""
    return encode_json_line(event.to_dict())


def check_int64_range(data: Mapping[str, Any], *, pointer: str = "") -> list[str]:
    """Return JSON pointers of integer values outside the int64 range."""
    offenders: list[str] = []
    _check_int64_value(data, pointer=pointer, offenders=offenders)
    return offenders


def _check_int64_value(value: Any, *, pointer: str, offenders: list[str]) -> None:
    # One walker for every shape a payload can take: the rule is about the
    # integers a line carries, not about how deeply they are wrapped.
    if isinstance(value, bool):
        return
    if isinstance(value, int):
        if value < INT64_MIN or value > INT64_MAX:
            offenders.append(pointer)
        return
    if isinstance(value, Mapping):
        for key, member in value.items():
            _check_int64_value(member, pointer=f"{pointer}/{key}", offenders=offenders)
        return
    if isinstance(value, list | tuple):
        for index, item in enumerate(value):
            _check_int64_value(item, pointer=f"{pointer}/{index}", offenders=offenders)


_REQUIRED_KEYS: Final = (
    "schema_version",
    "event_id",
    "sequence",
    "runtime_id",
    "coverage_id",
    "session_id",
    "branch_id",
    "actor",
    "event_type",
    "occurred_at",
    "monotonic_ns",
    "payload",
)


def _require_str(data: Mapping[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise EnvelopeError(f"Envelope field {key!r} must be a non-empty string.")
    return value


def _optional_str(data: Mapping[str, Any], key: str) -> str | None:
    if key not in data:
        return None
    value = data[key]
    if not isinstance(value, str) or not value:
        raise EnvelopeError(f"Envelope field {key!r} must be a non-empty string when present.")
    return value


def _require_int(data: Mapping[str, Any], key: str, *, minimum: int) -> int:
    value = data.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum or value > INT64_MAX:
        raise EnvelopeError(f"Envelope field {key!r} must be an integer >= {minimum}.")
    return value


def _validate_id_fields(data: Mapping[str, Any], *, pointer: str, errors: list[str]) -> None:
    for key, value in data.items():
        child = f"{pointer}/{key}"
        if key in _OPAQUE_SUBTREE_KEYS:
            continue
        if is_id_field(key):
            if value is not None and not is_valid_id(key, value):
                errors.append(child)
            continue
        _validate_id_members(value, pointer=child, errors=errors)


def _validate_id_members(value: Any, *, pointer: str, errors: list[str]) -> None:
    # An id-bearing object is held to its format wherever it sits — a list of
    # entries, a list of lists of them, all the same rule.
    if isinstance(value, Mapping):
        _validate_id_fields(value, pointer=pointer, errors=errors)
    elif isinstance(value, list | tuple):
        for index, item in enumerate(value):
            _validate_id_members(item, pointer=f"{pointer}/{index}", errors=errors)


def malformed_id_pointers(data: Mapping[str, Any]) -> list[str]:
    """JSON pointers of every ID-bearing field whose value fails its format class.

    The writer runs this before a line is written so a malformed id becomes a
    gap rather than a line no reader can decode.
    """
    errors: list[str] = []
    _validate_id_fields(data, pointer="", errors=errors)
    return errors


@dataclass(frozen=True, slots=True)
class EnvelopeHeader:
    """The two fields every schema of this file shares: which version, which slot."""

    schema_version: int
    sequence: int


def peek_envelope_header(raw: bytes) -> EnvelopeHeader | None:
    """Read a line's version and slot without holding it to this build's shape.

    A schema is bumped exactly when lines stop meaning what this build reads
    them as, so a newer line has to be legible enough to be counted and
    stepped over rather than mistaken for damage. Returns ``None`` for a line
    that is not even a JSON object with both fields.
    """
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(data, Mapping):
        return None
    version = data.get("schema_version")
    sequence = data.get("sequence")
    if not _is_slot_int(version) or not _is_slot_int(sequence):
        return None
    return EnvelopeHeader(schema_version=version, sequence=sequence)


def _is_slot_int(value: Any) -> bool:
    # Bounded like every other integer a line may carry: a value this build
    # cannot even encode is not a slot it can count, and treating it as one
    # would resume the log past the largest sequence a reader accepts.
    return isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= INT64_MAX


def decode_event_dict(data: Mapping[str, Any]) -> TrajectoryEvent:
    """Validate and convert one decoded JSON object into an event.

    Unknown keys are ignored. Structural or format violations (missing required
    keys, wrong types, malformed IDs, integers beyond int64) raise
    :class:`EnvelopeError`; callers treat such a line as corruption.
    """
    missing = [key for key in _REQUIRED_KEYS if key not in data]
    if missing:
        raise EnvelopeError(f"Envelope is missing required keys: {', '.join(missing)}.")
    schema_version = _require_int(data, "schema_version", minimum=1)
    sequence = _require_int(data, "sequence", minimum=1)
    monotonic_ns = _require_int(data, "monotonic_ns", minimum=0)
    actor_raw = data["actor"]
    if not isinstance(actor_raw, Mapping):
        raise EnvelopeError("Envelope field 'actor' must be an object.")
    actor = Actor(
        kind=_require_str(actor_raw, "kind"),
        role=_require_str(actor_raw, "role"),
        actor_id=_optional_str(actor_raw, "actor_id"),
        invocation_id=_optional_str(actor_raw, "invocation_id"),
    )
    payload = data["payload"]
    if not isinstance(payload, Mapping):
        raise EnvelopeError("Envelope field 'payload' must be an object.")
    measurements_raw = data.get("measurements", {})
    if not isinstance(measurements_raw, Mapping) or not all(
        isinstance(value, Mapping) for value in measurements_raw.values()
    ):
        raise EnvelopeError("Envelope field 'measurements' must map pointers to objects.")
    links_raw = data.get("links", [])
    if not isinstance(links_raw, list):
        raise EnvelopeError("Envelope field 'links' must be an array.")
    links: list[Link] = []
    for item in links_raw:
        if not isinstance(item, Mapping):
            raise EnvelopeError("Envelope links must be objects.")
        links.append(
            Link(
                relation=_require_str(item, "relation"),
                target_operation_id=_require_str(item, "target_operation_id"),
                target_item_id=_optional_str(item, "target_item_id"),
            )
        )
    segmented_raw = data.get("segmented_fields", [])
    if not isinstance(segmented_raw, list):
        raise EnvelopeError("Envelope field 'segmented_fields' must be an array.")
    segmented: list[SegmentedField] = []
    for item in segmented_raw:
        if not isinstance(item, Mapping):
            raise EnvelopeError("Envelope segmented_fields entries must be objects.")
        segmented.append(
            SegmentedField(
                field_pointer=_require_str(item, "field_pointer"),
                segment_group_id=_require_str(item, "segment_group_id"),
                segment_count=_require_int(item, "segment_count", minimum=1),
            )
        )
    id_errors = malformed_id_pointers(data)
    if id_errors:
        raise EnvelopeError(f"Envelope carries malformed identifiers at: {', '.join(id_errors)}.")
    range_errors = check_int64_range(data)
    if range_errors:
        raise EnvelopeError(f"Envelope carries out-of-range integers at: {', '.join(range_errors)}.")
    return TrajectoryEvent(
        schema_version=schema_version,
        event_id=_require_str(data, "event_id"),
        sequence=sequence,
        runtime_id=_require_str(data, "runtime_id"),
        coverage_id=_require_str(data, "coverage_id"),
        session_id=_require_str(data, "session_id"),
        branch_id=_require_str(data, "branch_id"),
        actor=actor,
        event_type=_require_str(data, "event_type"),
        occurred_at=_require_str(data, "occurred_at"),
        monotonic_ns=monotonic_ns,
        payload=dict(payload),
        measurements={pointer: dict(value) for pointer, value in measurements_raw.items()},
        turn_id=_optional_str(data, "turn_id"),
        operation_id=_optional_str(data, "operation_id"),
        parent_operation_id=_optional_str(data, "parent_operation_id"),
        links=tuple(links),
        segmented_fields=tuple(segmented),
    )


def decode_event_line(line: bytes) -> TrajectoryEvent:
    """Decode one complete line (with or without its trailing newline)."""
    try:
        data = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise EnvelopeError(f"Line is not valid UTF-8 JSON: {exc}") from exc
    if not isinstance(data, Mapping):
        raise EnvelopeError("Line is not a JSON object.")
    return decode_event_dict(data)


def iter_complete_lines(raw: bytes) -> tuple[Sequence[bytes], bytes]:
    """Split *raw* into complete newline-terminated lines and the torn tail."""
    if not raw:
        return (), b""
    lines = raw.split(b"\n")
    tail = lines.pop()
    return lines, tail
