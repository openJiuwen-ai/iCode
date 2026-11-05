# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Context variables shared between sub-agent event middleware and tools."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

SUB_AGENT_RESULT_COMMIT_CALLBACK_KEY = "_chrys_sub_agent_result_commit_callback"
SUB_AGENT_TRANSCRIPT_FINAL_TEXT_METADATA_KEY = "sub_agent_transcript_final_text"
SUB_AGENT_PROGRESS_METADATA_KEY = "sub_agent_progress"
"""Parent result metadata: the sub-agent run's final counters (:class:`SubAgentProgressTotals`)."""

# Carries the parent tool call id into the sub-agent tool body. Tool event
# middleware sets it before invoking KIND_SUB_AGENT tools; the sub-agent tool
# reads it when emitting InvocationStarted.
sub_agent_parent_call_id: ContextVar[str] = ContextVar("sub_agent_parent_call_id", default="")


@dataclass(frozen=True, slots=True)
class SubAgentProgressTotals:
    """A sub-agent run's final counters: the ones its card shows live, kept with the parent result.

    A restored session has no live progress to show, so its sub-agent cards
    read these back from the persisted parent result.
    """

    tool_calls: int = 0
    context_tokens: int = 0
    """Context-window tokens of the run's latest model call."""
    usage_tokens: int = 0
    """Tokens the run spent across all its model calls."""
    unreported_attempts: int = 0
    """ACP attempts whose terminal response reported no spend."""
    compactions: int = 0
    """Committed context compactions."""

    def to_metadata(self) -> dict[str, int]:
        """The persisted form: only the counters that are not zero."""
        counts = {
            "tool_calls": self.tool_calls,
            "context_tokens": self.context_tokens,
            "usage_tokens": self.usage_tokens,
            "unreported_attempts": self.unreported_attempts,
            "compactions": self.compactions,
        }
        return {name: count for name, count in counts.items() if count > 0}

    @classmethod
    def from_metadata(cls, value: object) -> SubAgentProgressTotals:
        """Read a persisted record; a missing or malformed counter reads as zero."""
        if not isinstance(value, Mapping):
            return cls()

        def count(name: str) -> int:
            raw = value.get(name)
            return raw if isinstance(raw, int) and not isinstance(raw, bool) and raw > 0 else 0

        return cls(
            tool_calls=count("tool_calls"),
            context_tokens=count("context_tokens"),
            usage_tokens=count("usage_tokens"),
            unreported_attempts=count("unreported_attempts"),
            compactions=count("compactions"),
        )


@dataclass(slots=True)
class SubAgentParentResultMetadata:
    """Mutable parent-call metadata populated by sub-agent tool invocations."""

    parent_provider_call_id: str = ""
    parent_event_call_id: str = ""
    sub_agent_invocation_id: str = ""
    sub_agent_log_file: str = ""
    sub_agent_audit_complete: bool = False
    transcript_final_text: str | None = None
    progress: SubAgentProgressTotals | None = None
    """The run's final counters, set once they can no longer change."""
    interrupted_result_committed: bool = False
    commit_interrupted_result: Callable[[Mapping[str, Any]], None] | None = None


sub_agent_parent_result_metadata: ContextVar[SubAgentParentResultMetadata | None] = ContextVar(
    "sub_agent_parent_result_metadata",
    default=None,
)
