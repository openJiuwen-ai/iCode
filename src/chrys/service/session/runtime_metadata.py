# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Session-scoped runtime metadata owned by the engine."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, NotRequired, Self, TypedDict, cast

LAST_USAGE_KEY = "last_usage"
TOTAL_SESSION_TOKENS_KEY = "total_session_tokens"
TOTAL_SESSION_INPUT_TOKENS_KEY = "total_session_input_tokens"
TOTAL_SESSION_OUTPUT_TOKENS_KEY = "total_session_output_tokens"
TOTAL_SESSION_CACHE_HIT_TOKENS_KEY = "total_session_cache_hit_tokens"
CONTEXT_CALIBRATION_KEY = "context_calibration"
CONTEXT_CALIBRATION_VERSION = 2
RELAY_RISK_SEEN_KEY = "relay_risk_seen"


class LastUsageDetails(TypedDict, total=False):
    """Precise parent-call usage snapshot written by ``record_usage``."""

    input_token_count: int
    output_token_count: int
    total_token_count: int
    local_tokens: int
    calibration_ratio: float
    system_overhead_tokens: int
    cache_hit_tokens: NotRequired[int]


@dataclass
class SessionUsageMetadata:
    """Cumulative spend shared by all sessions, independent of any conversation context."""

    total_session_tokens: int = 0
    total_session_input_tokens: int = 0
    total_session_output_tokens: int = 0
    total_session_cache_hit_tokens: int | None = None
    """Cumulative cache-read tokens across the session, or ``None`` when no
    LLM call has reported cache info yet. Transitions to ``int`` on the first
    cache-aware response (even a reported 0) and stays ``int`` thereafter."""
    relay_risk_seen: bool = False
    """Latched once this session runs on a relay model profile.

    Relay-sourced content stays in the conversation after a model switch, so
    the conservative marker keeps relay-enhanced approval review on for the
    session's remaining life (and across restore) instead of silently
    treating old content as trusted again."""

    @classmethod
    def from_state_dict(cls, state: Mapping[str, Any]) -> Self:
        return cls(
            total_session_tokens=state.get(TOTAL_SESSION_TOKENS_KEY, 0),
            total_session_input_tokens=state.get(TOTAL_SESSION_INPUT_TOKENS_KEY, 0),
            total_session_output_tokens=state.get(TOTAL_SESSION_OUTPUT_TOKENS_KEY, 0),
            total_session_cache_hit_tokens=state.get(TOTAL_SESSION_CACHE_HIT_TOKENS_KEY),
            relay_risk_seen=bool(state.get(RELAY_RISK_SEEN_KEY, False)),
        )

    def to_state_dict(self) -> dict[str, Any]:
        return {
            TOTAL_SESSION_TOKENS_KEY: self.total_session_tokens,
            TOTAL_SESSION_INPUT_TOKENS_KEY: self.total_session_input_tokens,
            TOTAL_SESSION_OUTPUT_TOKENS_KEY: self.total_session_output_tokens,
            TOTAL_SESSION_CACHE_HIT_TOKENS_KEY: self.total_session_cache_hit_tokens,
            RELAY_RISK_SEEN_KEY: self.relay_risk_seen,
        }

    def accumulate_out_of_band_usage(
        self,
        total_tokens: int,
        *,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_hit_tokens: int | None = None,
    ) -> None:
        """Accumulate LLM spend independently of a conversation's context reading.

        Agent invocations and Phase-4 side calls spend real tokens that belong
        in the session totals. This shared ledger has no context window;
        Chat tracks its main invocation's last usage separately.
        """
        if cache_hit_tokens is not None:
            self.total_session_cache_hit_tokens = (self.total_session_cache_hit_tokens or 0) + cache_hit_tokens
        self._accumulate(total_tokens, input_tokens=input_tokens, output_tokens=output_tokens)

    def accumulate_invocation_usage(
        self,
        total_tokens: int,
        *,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_hit_tokens: int | None = None,
        authoritative_total: int | None = None,
    ) -> None:
        """Accumulate one invocation into session totals."""
        if authoritative_total is not None:
            if cache_hit_tokens is not None:
                self.total_session_cache_hit_tokens = (self.total_session_cache_hit_tokens or 0) + cache_hit_tokens
            self.total_session_tokens += authoritative_total
            self.total_session_input_tokens += input_tokens
            self.total_session_output_tokens += output_tokens
            return
        self.accumulate_out_of_band_usage(
            total_tokens,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_hit_tokens=cache_hit_tokens,
        )

    def _accumulate(self, total_tokens: int, *, input_tokens: int = 0, output_tokens: int = 0) -> None:
        """Accumulate tokens, preserving provider fallback semantics."""
        self.total_session_tokens += (input_tokens + output_tokens) or total_tokens
        self.total_session_input_tokens += input_tokens
        self.total_session_output_tokens += output_tokens


@dataclass
class SessionRuntimeMetadata(SessionUsageMetadata):
    """Live usage metadata that is mirrored into persisted session state."""

    last_usage_details: LastUsageDetails = field(default_factory=dict)
    context_calibration: dict[str, Any] | None = None

    @classmethod
    def from_state_dict(cls, state: Mapping[str, Any]) -> Self:
        """Build runtime metadata from persisted session state."""
        last_usage = state.get(LAST_USAGE_KEY)
        return cls(
            **SessionUsageMetadata.from_state_dict(state).to_state_dict(),
            # Chrys writers persist exactly ``LastUsageDetails``. Keep the
            # deliberately tolerant, byte-identical blind copy for legacy or
            # externally edited sessions; the cast has no runtime effect.
            last_usage_details=cast("LastUsageDetails", dict(last_usage)) if isinstance(last_usage, dict) else {},
            context_calibration=(
                dict(record) if isinstance((record := state.get(CONTEXT_CALIBRATION_KEY)), Mapping) else None
            ),
        )

    def to_state_dict(self) -> dict[str, Any]:
        """Return state keys to write into ``executor.history_state`` before saving."""
        state = {
            LAST_USAGE_KEY: dict(self.last_usage_details),
            **super().to_state_dict(),
        }
        if self.context_calibration is not None:
            state[CONTEXT_CALIBRATION_KEY] = dict(self.context_calibration)
        return state

    def record_context_calibration(
        self,
        *,
        system_overhead_tokens: int,
        calibration_ratio: float,
        model_profile_fingerprint: str,
        agent_profile_fingerprint: str,
    ) -> None:
        """Persist initialized calibration with its exact build provenance."""
        self.context_calibration = {
            "v": CONTEXT_CALIBRATION_VERSION,
            "system_overhead_tokens": system_overhead_tokens,
            "calibration_ratio": calibration_ratio,
            "model_profile_fingerprint": model_profile_fingerprint,
            "agent_profile_fingerprint": agent_profile_fingerprint,
        }

    def restore_context_calibration(
        self,
        strategy: Any,
        *,
        model_profile_fingerprint: str,
        agent_profile_fingerprint: str,
    ) -> bool:
        """Hydrate calibration only when version and both fingerprints match."""
        record = self.context_calibration
        if record is None:
            return False
        version = record.get("v")
        if not isinstance(version, int) or isinstance(version, bool) or version != CONTEXT_CALIBRATION_VERSION:
            return False
        if (
            record.get("model_profile_fingerprint") != model_profile_fingerprint
            or record.get("agent_profile_fingerprint") != agent_profile_fingerprint
        ):
            return False
        return strategy.restore_calibration(
            record.get("system_overhead_tokens"),
            record.get("calibration_ratio"),
        )

    def record_usage(
        self,
        total_tokens: int,
        *,
        input_tokens: int = 0,
        output_tokens: int = 0,
        local_tokens: int = 0,
        calibration_ratio: float = 1.0,
        system_overhead_tokens: int = 0,
        cache_hit_tokens: int | None = None,
        use_local_context_estimate: bool = False,
    ) -> None:
        """Record parent-agent usage and update cumulative totals."""
        window_input_tokens = local_tokens if use_local_context_estimate else input_tokens
        window_total_tokens = window_input_tokens + output_tokens if use_local_context_estimate else total_tokens
        details: LastUsageDetails = {
            "input_token_count": window_input_tokens,
            "output_token_count": output_tokens,
            "total_token_count": window_total_tokens,
            "local_tokens": local_tokens,
            "calibration_ratio": calibration_ratio,
            "system_overhead_tokens": system_overhead_tokens,
        }
        if cache_hit_tokens is not None:
            details["cache_hit_tokens"] = cache_hit_tokens
            self.total_session_cache_hit_tokens = (self.total_session_cache_hit_tokens or 0) + cache_hit_tokens
        self.last_usage_details = details
        self._accumulate(total_tokens, input_tokens=input_tokens, output_tokens=output_tokens)
