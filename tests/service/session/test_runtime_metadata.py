# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for engine-owned session runtime metadata."""

from __future__ import annotations

from chrys.service.context.compaction import UnifiedContextStrategy
from chrys.service.session.runtime_metadata import CONTEXT_CALIBRATION_VERSION, SessionRuntimeMetadata


def test_from_state_dict_defaults_missing_values() -> None:
    meta = SessionRuntimeMetadata.from_state_dict({})

    assert meta.total_session_tokens == 0
    assert meta.total_session_input_tokens == 0
    assert meta.total_session_output_tokens == 0
    assert meta.last_usage_details == {}


def test_from_state_dict_ignores_malformed_last_usage() -> None:
    meta = SessionRuntimeMetadata.from_state_dict(
        {
            "last_usage": "not a dict",
            "total_session_tokens": 12,
            "total_session_input_tokens": 5,
            "total_session_output_tokens": 7,
        }
    )

    assert meta.last_usage_details == {}
    assert meta.total_session_tokens == 12
    assert meta.total_session_input_tokens == 5
    assert meta.total_session_output_tokens == 7


def test_to_state_dict_writes_all_owned_keys_unconditionally() -> None:
    state = SessionRuntimeMetadata().to_state_dict()

    assert state == {
        "last_usage": {},
        "total_session_tokens": 0,
        "total_session_input_tokens": 0,
        "total_session_output_tokens": 0,
        "total_session_cache_hit_tokens": None,
        "relay_risk_seen": False,
    }
    assert "turn_counter" not in state


def test_relay_risk_latch_survives_the_state_round_trip() -> None:
    """Once a session has run on a relay, the latch persists across restore."""
    meta = SessionRuntimeMetadata()
    meta.relay_risk_seen = True

    assert SessionRuntimeMetadata.from_state_dict(meta.to_state_dict()).relay_risk_seen is True
    # Absent key (a legacy session, or the truthy save filter) means never relayed.
    assert SessionRuntimeMetadata.from_state_dict({}).relay_risk_seen is False


def test_record_usage_preserves_float_calibration_ratio_and_fallback_total() -> None:
    meta = SessionRuntimeMetadata()

    meta.record_usage(
        25,
        input_tokens=0,
        output_tokens=0,
        local_tokens=20,
        calibration_ratio=1.25,
        system_overhead_tokens=5,
    )

    assert meta.total_session_tokens == 25
    assert meta.total_session_input_tokens == 0
    assert meta.total_session_output_tokens == 0
    assert meta.last_usage_details == {
        "input_token_count": 0,
        "output_token_count": 0,
        "total_token_count": 25,
        "local_tokens": 20,
        "calibration_ratio": 1.25,
        "system_overhead_tokens": 5,
    }
    assert meta.to_state_dict()["last_usage"]["calibration_ratio"] == 1.25


def test_record_usage_prefers_input_output_sum_for_session_total() -> None:
    meta = SessionRuntimeMetadata()

    meta.record_usage(999, input_tokens=4, output_tokens=6)

    assert meta.total_session_tokens == 10
    assert meta.total_session_input_tokens == 4
    assert meta.total_session_output_tokens == 6


def test_record_aggregate_provider_usage_separates_window_from_session_billing() -> None:
    meta = SessionRuntimeMetadata()

    meta.record_usage(
        275_586,
        input_tokens=273_399,
        output_tokens=2_187,
        local_tokens=20_240,
        cache_hit_tokens=259_200,
        use_local_context_estimate=True,
    )

    assert meta.last_usage_details == {
        "input_token_count": 20_240,
        "output_token_count": 2_187,
        "total_token_count": 22_427,
        "local_tokens": 20_240,
        "calibration_ratio": 1.0,
        "system_overhead_tokens": 0,
        "cache_hit_tokens": 259_200,
    }
    assert meta.total_session_tokens == 275_586
    assert meta.total_session_input_tokens == 273_399
    assert meta.total_session_output_tokens == 2_187
    assert meta.total_session_cache_hit_tokens == 259_200


def test_accumulate_invocation_usage_does_not_change_last_usage() -> None:
    meta = SessionRuntimeMetadata(last_usage_details={"total_token_count": 10})

    meta.accumulate_invocation_usage(15)

    assert meta.total_session_tokens == 15
    assert meta.total_session_input_tokens == 0
    assert meta.total_session_output_tokens == 0
    assert meta.last_usage_details == {"total_token_count": 10}


def test_accumulate_out_of_band_usage_sums_totals_and_cache_without_last_usage() -> None:
    meta = SessionRuntimeMetadata(last_usage_details={"total_token_count": 10})

    meta.accumulate_out_of_band_usage(999, input_tokens=4, output_tokens=6, cache_hit_tokens=3)
    meta.accumulate_out_of_band_usage(20, cache_hit_tokens=None)

    # input+output sum wins over the reported total; total-only falls back.
    assert meta.total_session_tokens == 10 + 20
    assert meta.total_session_input_tokens == 4
    assert meta.total_session_output_tokens == 6
    # None means "provider did not report" — the running cache total survives.
    assert meta.total_session_cache_hit_tokens == 3
    assert meta.last_usage_details == {"total_token_count": 10}


def test_absent_usage_is_unreported_and_authoritative_total_reaches_runtime_metadata() -> None:
    metadata = SessionRuntimeMetadata()
    metadata.accumulate_invocation_usage(
        5,
        input_tokens=2,
        output_tokens=3,
        cache_hit_tokens=4,
        authoritative_total=11,
    )
    assert metadata.total_session_tokens == 11
    assert metadata.total_session_input_tokens == 2
    assert metadata.total_session_output_tokens == 3
    assert metadata.total_session_cache_hit_tokens == 4


def test_context_calibration_round_trip_requires_both_cached_fingerprints() -> None:
    meta = SessionRuntimeMetadata()
    meta.record_context_calibration(
        system_overhead_tokens=12,
        calibration_ratio=1.25,
        model_profile_fingerprint="model-fingerprint",
        agent_profile_fingerprint="agent-fingerprint",
    )
    restored = SessionRuntimeMetadata.from_state_dict(meta.to_state_dict())
    strategy = UnifiedContextStrategy(max_context_tokens=100)

    assert meta.context_calibration is not None
    assert meta.context_calibration["v"] == CONTEXT_CALIBRATION_VERSION == 2
    assert restored.restore_context_calibration(
        strategy,
        model_profile_fingerprint="model-fingerprint",
        agent_profile_fingerprint="agent-fingerprint",
    )
    assert strategy.system_overhead_tokens == 12
    assert strategy.calibration_ratio == 1.25
    assert strategy.calibration_initialized

    assert not restored.restore_context_calibration(
        UnifiedContextStrategy(max_context_tokens=100),
        model_profile_fingerprint="different",
        agent_profile_fingerprint="agent-fingerprint",
    )
    assert not restored.restore_context_calibration(
        UnifiedContextStrategy(max_context_tokens=100),
        model_profile_fingerprint="model-fingerprint",
        agent_profile_fingerprint="different",
    )


def test_context_calibration_restore_skips_missing_malformed_and_wrong_version() -> None:
    strategy = UnifiedContextStrategy(max_context_tokens=100)
    assert not SessionRuntimeMetadata().restore_context_calibration(
        strategy,
        model_profile_fingerprint="model",
        agent_profile_fingerprint="agent",
    )
    assert SessionRuntimeMetadata.from_state_dict({"context_calibration": "bad"}).context_calibration is None
    wrong_version = SessionRuntimeMetadata(
        context_calibration={
            "v": 1,
            "system_overhead_tokens": 10,
            "calibration_ratio": 1.0,
            "model_profile_fingerprint": "model",
            "agent_profile_fingerprint": "agent",
        }
    )
    assert not wrong_version.restore_context_calibration(
        strategy,
        model_profile_fingerprint="model",
        agent_profile_fingerprint="agent",
    )
    assert not strategy.calibration_initialized
