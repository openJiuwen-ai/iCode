# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""ContextManager — unified entry point for context management.

Coordinates UsageTrackingMiddleware and CompressibleHistoryProvider.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from chrys.service.context.compaction.budgets import derive_compaction_budgets
from chrys.service.context.middleware.usage import UsageTrackingMiddleware
from chrys.service.context.providers.context_management import ContextManagementProvider
from chrys.service.context.providers.history import CompressibleHistoryProvider
from chrys.service.context.providers.memory import MemoryProvider
from chrys.service.profiles.agents.schema import CompactionConfig
from chrys.service.profiles.models.options import uses_responses_compact_continuation
from chrys.service.profiles.models.schema import uses_responses_wire_dialect

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping
    from typing import Any

    from chrys.service.context.compaction import CompactionInfo, CompressInfo, PreCompactInfo
    from chrys.service.context.compaction.last_words_state import DropRoundBreakerState
    from chrys.service.context.compaction.spill import SpillQuota
    from chrys.service.profiles.models.schema import ModelProfile


# Default warn-threshold (fraction of context window) used when the
# caller does not supply one.  Mirrors ``Settings.warn_threshold_pct``
# so the default UX is unchanged.  Callers that want to override this
# (e.g. the engine threading ``settings.warn_threshold_pct``) pass
# ``warn_threshold_pct=`` explicitly.
_DEFAULT_WARN_THRESHOLD_PCT = 0.50


def _uses_aggregate_hosted_usage(
    profile: ModelProfile,
    service_context_default_options: Mapping[str, Any] | None,
) -> bool:
    """Return whether hosted execution usage is not final context occupancy."""
    if profile.provider == "anthropic":
        # Anthropic server tools execute in a server-side agentic loop. One
        # response can include multiple search/code iterations, and its input
        # usage is the billable sum across those internal sampling passes.
        return True
    return uses_responses_wire_dialect(profile) and not uses_responses_compact_continuation(
        profile,
        dict(service_context_default_options or {}),
    )


class ContextManager:
    """Creates and manages all context-related framework components.

    Builds the compaction strategy from model-derived trigger/target budgets.
    ``profile`` supplies the context and output limits; ``warn_threshold_pct`` is an
    app-level UI knob and defaults to :data:`_DEFAULT_WARN_THRESHOLD_PCT`.

    Usage::

        ctx = ContextManager(active_profile, compaction_config=profile.compaction)
        agent = Agent(
            ...,
            context_providers=ctx.providers,
            middleware=ctx.middleware,
        )
    """

    def __init__(
        self,
        profile: ModelProfile,
        on_usage: Callable[..., Any] | None = None,
        on_compaction: Callable[[CompactionInfo], Awaitable[None]] | None = None,
        on_pre_compact: Callable[[PreCompactInfo], Awaitable[None]] | None = None,
        on_compress: Callable[[CompressInfo], Awaitable[None]] | None = None,
        on_context_pressure: Callable[[str, DropRoundBreakerState, int], Awaitable[None] | None] | None = None,
        spill_quota: SpillQuota | None = None,
        spill_record_dir: Path | None = None,
        debug_log_dir: Path | None = None,
        compaction_config: CompactionConfig | None = None,
        include_context_management: bool = True,
        memory_text: str = "",
        warn_threshold_pct: float = _DEFAULT_WARN_THRESHOLD_PCT,
        session_id: str | None = None,
        parent_session_id: str | None = None,
        session_dir: Path | None = None,
        skip_local_history_for_service_context: bool = False,
        service_context_default_options: Mapping[str, Any] | None = None,
        tool_result_ceiling_tokens: int | None = None,
    ) -> None:
        cfg = compaction_config or CompactionConfig()
        budgets = derive_compaction_budgets(profile)

        # Build unified strategy — always created so compress_context works
        # even when budget-triggered compaction is disabled.
        from chrys.service.context.compaction import UnifiedContextStrategy

        compaction_enabled = cfg.enabled
        compaction_strategy = UnifiedContextStrategy(
            max_context_tokens=profile.max_context_tokens,
            # Derived even when disabled: a skip past the trigger is still
            # reported against the model's real threshold.
            trigger_pct=budgets.trigger_pct,
            target_pct=budgets.target_pct,
            satisfied_pct=budgets.satisfied_pct,
            force_pct=budgets.force_compress_pct,
            on_compaction=on_compaction,
            on_pre_compact=on_pre_compact,
            on_compress=on_compress,
            on_context_pressure=on_context_pressure,
            spill_quota=spill_quota,
            spill_root=session_dir,
            spill_record_dir=spill_record_dir,
            spill_session_id=parent_session_id or session_id or "",
            debug_log_dir=debug_log_dir,
            phase4_side_call_token_budget=cfg.phase4_side_call_token_budget,
            compaction_enabled=compaction_enabled,
        )

        self.compaction_strategy = compaction_strategy
        self.budgets = budgets
        # Some providers report hosted execution as a billable aggregate over
        # internal sampling passes, not as the final context occupancy. Keep
        # that aggregate in session totals, but use the local request estimate
        # for window calibration and compaction thresholds.
        self.use_local_context_estimate_for_hosted_usage = _uses_aggregate_hosted_usage(
            profile,
            service_context_default_options,
        )

        self.history_provider = CompressibleHistoryProvider(
            compaction_strategy=compaction_strategy,
            max_context_tokens=profile.max_context_tokens,
            force_compress_pct=budgets.force_compress_pct if compaction_enabled else 1.0,
            skip_local_history_for_service_context=skip_local_history_for_service_context,
            service_context_default_options=service_context_default_options,
            use_local_context_estimate_for_hosted_usage=self.use_local_context_estimate_for_hosted_usage,
        )
        self.context_mgmt_provider: ContextManagementProvider | None = (
            ContextManagementProvider(
                profile,
                strategy=compaction_strategy,
                session_id=session_id,
                parent_session_id=parent_session_id,
                session_dir=session_dir,
                tool_result_ceiling_tokens=tool_result_ceiling_tokens,
            )
            if include_context_management
            else None
        )
        self.memory_provider: MemoryProvider | None = MemoryProvider(memory_text) if memory_text else None
        self.usage_middleware = UsageTrackingMiddleware(
            max_context_tokens=profile.max_context_tokens,
            warn_threshold_pct=warn_threshold_pct,
            on_usage=on_usage,
            compaction_strategy=compaction_strategy,
            use_local_context_estimate_for_hosted_usage=self.use_local_context_estimate_for_hosted_usage,
        )

    @property
    def providers(self) -> list:
        """Context providers in correct order (context management, memory, history last)."""
        result: list = []
        if self.context_mgmt_provider is not None:
            result.append(self.context_mgmt_provider)
        if self.memory_provider is not None:
            result.append(self.memory_provider)
        result.append(self.history_provider)
        return result

    @property
    def middleware(self) -> list:
        """Chat middleware list."""
        return [self.usage_middleware]
