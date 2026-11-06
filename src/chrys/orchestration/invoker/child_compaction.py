# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Child compaction rollback and invocation-scoped hook and status reporting."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from chrys.foundation.events.types import (
    InvocationCompactionCommitted,
    InvocationCompactionFinished,
    InvocationCompactionStarted,
    InvocationContextPressure,
    InvocationRetryAttempt,
)
from chrys.service.hooks.events import HookEvent

if TYPE_CHECKING:
    from chrys.foundation.retry import RetryAttemptInfo
    from chrys.orchestration.invoker.origin import BoundEmitter
    from chrys.service.context.compaction import PreCompactInfo
    from chrys.service.context.compaction.last_words import CompactionStatus
    from chrys.service.context.compaction.last_words_state import DropRoundBreakerState
    from chrys.service.context.compaction.strategy import CompactionRetrySnapshot, UnifiedContextStrategy
    from chrys.service.hooks.manager import HookManager


@dataclass(frozen=True, slots=True)
class CompactionRollback:
    strategy: UnifiedContextStrategy | None

    def snapshot(self) -> object:
        return self.strategy.snapshot_retry_state() if self.strategy is not None else None

    def restore(self, state: object) -> None:
        if self.strategy is not None and state is not None:
            self.strategy.restore_retry_state(cast("CompactionRetrySnapshot", state))


@dataclass(frozen=True, slots=True)
class ChildCompactionEvents:
    emitter: Callable[[], BoundEmitter]
    """The publisher of the pass in flight, read once per report: a retried pass may publish as another attempt."""
    name: str
    tool_name: str
    profile: str
    workspace_cwd: str
    hook_manager: HookManager | None

    async def pre_compact(self, info: PreCompactInfo) -> None:
        hooks = self.hook_manager
        if hooks is None or not hooks.has_hooks_for(HookEvent.PRE_COMPACT):
            return
        await hooks.fire(
            HookEvent.PRE_COMPACT,
            {
                "session_id": self.emitter().origin.session_id,
                "profile": self.profile,
                "cwd": self.workspace_cwd,
                "trigger": info.trigger,
                "usage_pct": info.usage_pct,
                "tokens_before": info.tokens_before,
                "sub_agent": {"name": self.name, "tool_name": self.tool_name},
            },
            target_operation_id=info.trajectory_operation_id,
        )

    async def context_pressure(self, reason: str, breaker: DropRoundBreakerState, budget: int) -> None:
        emitter = self.emitter()
        await emitter.publish(
            InvocationContextPressure(
                origin=emitter.origin,
                reason=reason,
                attempts=breaker.attempts,
                side_call_tokens=breaker.side_call_tokens,
                side_call_token_budget=budget,
                source=emitter.origin.kind,
                session_id=emitter.origin.session_id,
            )
        )

    async def retry(self, info: RetryAttemptInfo) -> None:
        emitter = self.emitter()
        await emitter.publish(
            InvocationRetryAttempt(
                scope="compaction",
                origin=emitter.origin,
                agent_name=self.name,
                message=f"LAST_WORDS compaction: {info.reason}",
                attempt=info.attempt,
                max_attempts=info.max_attempts,
                delay_seconds=int(info.delay_seconds),
                session_id=emitter.origin.session_id,
                facts=info.facts,
            )
        )

    async def status(self, status: CompactionStatus) -> None:
        emitter = self.emitter()
        if status.stage == "started":
            await emitter.publish(
                InvocationCompactionStarted(
                    origin=emitter.origin,
                    agent_name=self.name,
                    compaction_id=status.compaction_id,
                    session_id=emitter.origin.session_id,
                )
            )
        elif status.stage == "committed":
            await emitter.publish(
                InvocationCompactionCommitted(
                    origin=emitter.origin,
                    agent_name=self.name,
                    compaction_id=status.compaction_id,
                    session_id=emitter.origin.session_id,
                )
            )
        else:
            await emitter.publish(
                InvocationCompactionFinished(
                    origin=emitter.origin,
                    agent_name=self.name,
                    compaction_id=status.compaction_id,
                    outcome=status.outcome,
                    duration_ms=status.duration_ms,
                    format_violation=status.format_violation,
                    failure_reason=status.failure_reason,
                    session_id=emitter.origin.session_id,
                )
            )
