# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Immutable invocation identities, independent of execution and persistence."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

type ContentChannel = Literal["answer", "reasoning", "reasoning_summary"]
"""What a content segment of model output carries: answer text, plaintext reasoning, or a summary of the reasoning."""

type ContentStatus = Literal["completed", "failed", "interrupted"]
"""How a content segment ended."""


@dataclass(frozen=True, slots=True)
class InvocationOrigin:
    """A logical caller operation, including its explicitly bound ancestry."""

    kind: Literal["turn", "sub_agent", "workflow_node", "side_question"]
    session_id: str
    invocation_id: str
    parent: InvocationOrigin | None
    attempt: int = 0
    """The workflow scheduler attempt a node pass belongs to; 0 for invocations without attempts.

    Part of equality, so a publisher bound to one attempt rejects another attempt's facts. Backend
    admission and continuation tickets compare with :meth:`same_invocation` instead.
    """

    def __post_init__(self) -> None:
        if self.kind not in ("turn", "sub_agent", "workflow_node", "side_question") or not self.invocation_id:
            raise ValueError("A live invocation requires a kind and invocation identity")

    def same_invocation(self, other: InvocationOrigin | None) -> bool:
        """Whether *other* names this logical invocation, in any of its attempts."""
        return (
            other is not None
            and other.kind == self.kind
            and other.session_id == self.session_id
            and other.invocation_id == self.invocation_id
            and other.parent == self.parent
        )

    @property
    def root(self) -> InvocationOrigin:
        """The outermost bound ancestor: the chat turn or workflow node an operation runs under, or itself."""
        origin = self
        while origin.parent is not None:
            origin = origin.parent
        return origin


@dataclass(frozen=True, slots=True)
class ParentToolOccurrence:
    """Both parent tool identifiers, scoped to the parent invocation."""

    parent_invocation_id: str
    parent_event_call_id: str
    parent_provider_call_id: str


@dataclass(frozen=True, slots=True)
class PassHandle:
    """An immutable cancellation target; never a provider call identifier."""

    invocation_id: str
    pass_id: str
