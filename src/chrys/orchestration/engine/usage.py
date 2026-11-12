# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Engine-internal usage and compaction event publishing."""

from __future__ import annotations

from typing import TYPE_CHECKING

from chrys.foundation.events.types import ContextCompressed, ToolCompacted, UsageUpdate
from chrys.orchestration.session_usage import SessionUsagePublisher
from chrys.service.profiles.models.schema import DEFAULT_MAX_CONTEXT_TOKENS

if TYPE_CHECKING:
    from chrys.foundation.events.bus import EventBus
    from chrys.orchestration.engine.state.active_session import ActiveSession
    from chrys.orchestration.engine.state.current_agent import CurrentAgent
    from chrys.service.context.compaction import CompactionInfo, CompressInfo


class UsagePublisher(SessionUsagePublisher):
    """Own session usage accounting and ordered usage and compaction publication."""

    def __init__(self, *, bus: EventBus, session: ActiveSession, current: CurrentAgent) -> None:
        super().__init__(bus=bus, session=session)
        self._chat_session = session
        self._current = current

    def make_usage_event(self, *, session_id: str | None = None) -> UsageUpdate:
        """Build a UsageUpdate event — pct always computed from the active profile."""
        max_ctx = (
            self._current.manifest.active_profile.max_context_tokens
            if self._current.manifest.active_profile
            else DEFAULT_MAX_CONTEXT_TOKENS
        )
        last_usage = self._chat_session.runtime_meta.last_usage_details
        input_tok = last_usage.get("input_token_count", 0)
        output_tok = last_usage.get("output_token_count", 0)
        total = last_usage.get("total_token_count", 0)
        pct = round(total / max_ctx * 100, 1) if max_ctx else 0.0
        event_session_id = session_id or self._session.session_id
        return UsageUpdate(
            agent_profile=self._chat_session.agent_profile.name if self._chat_session.agent_profile else "",
            usage_source_id=event_session_id or "",
            input_tokens=input_tok,
            output_tokens=output_tok,
            total_tokens=total,
            pct=pct,
            max_context_tokens=max_ctx,
            total_session_tokens=self._chat_session.runtime_meta.total_session_tokens,
            total_session_input_tokens=self._chat_session.runtime_meta.total_session_input_tokens,
            total_session_output_tokens=self._chat_session.runtime_meta.total_session_output_tokens,
            total_session_cache_hit_tokens=self._chat_session.runtime_meta.total_session_cache_hit_tokens,
            cache_hit_tokens=last_usage.get("cache_hit_tokens"),
            local_tokens=last_usage.get("local_tokens", 0),
            calibration_ratio=last_usage.get("calibration_ratio", 1.0),
            system_overhead_tokens=last_usage.get("system_overhead_tokens", 0),
            session_id=event_session_id,
        )

    def publish_usage(
        self,
        total_tokens: int,
        input_tokens: int = 0,
        output_tokens: int = 0,
        local_tokens: int = 0,
        calibration_ratio: float = 1.0,
        system_overhead_tokens: int = 0,
        cache_hit_tokens: int | None = None,
        calibration_initialized: bool = False,
        use_local_context_estimate: bool = False,
    ) -> None:
        """Callback from UsageTrackingMiddleware — publish UsageUpdate event."""
        self._chat_session.runtime_meta.record_usage(
            total_tokens,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            local_tokens=local_tokens,
            calibration_ratio=calibration_ratio,
            system_overhead_tokens=system_overhead_tokens,
            cache_hit_tokens=cache_hit_tokens,
            use_local_context_estimate=use_local_context_estimate,
        )
        if calibration_initialized:
            self._chat_session.runtime_meta.record_context_calibration(
                system_overhead_tokens=system_overhead_tokens,
                calibration_ratio=calibration_ratio,
                model_profile_fingerprint=self._current.manifest.model_profile_fingerprint,
                agent_profile_fingerprint=self._current.manifest.agent_profile_fingerprint,
            )
        self.enqueue_usage_event(self.make_usage_event())

    async def publish_compress(self, info: CompressInfo) -> None:
        """Callback from ContextManagementProvider or a Phase 3 fold — publish ContextCompressed event."""
        await self._bus.publish(
            ContextCompressed(
                compressed_context_id=info.compressed_context_id,
                summary=info.summary,
                freed_messages=info.freed_messages,
                turn_range=info.turn_range,
                source=info.source,
                session_id=self._session.session_id,
            )
        )

    async def publish_compaction(self, info: CompactionInfo) -> None:
        """Callback from UnifiedContextStrategy — publish ToolCompacted event."""
        await self._bus.publish(
            ToolCompacted(
                compacted_groups=info.compacted_groups,
                phase=info.phase,
                turn_numbers=info.turn_numbers,
                compacted_tool_names=info.tool_names,
                tokens_before=info.tokens_before,
                tokens_after=info.tokens_after,
                last_words_generated=info.last_words_generated,
                session_id=self._session.session_id,
            )
        )
