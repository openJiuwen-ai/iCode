# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""End-to-end ACP client lifecycle and protocol-policy tests."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
from pathlib import Path

import pytest
from acp.schema import ToolCallProgress, ToolCallStart

from chrys.foundation.platform.process import ManagedStdioProcess
from chrys.service.acp_client import (
    AcpAgentClient,
    AcpAuthRequiredError,
    AcpConfigError,
    AcpConnectError,
    AcpIdleTimeoutError,
    AcpSpawnError,
    AcpTransportError,
    PermissionDecision,
)
from chrys.service.acp_client import _transport as transport_mod
from chrys.service.acp_client import client as client_mod
from tests.support.acp_stub_agent import WIDE_PATCH_FILES
from tests.support.waiting import ENGINE_TURN_TIMEOUT, wait_for, wait_until

from .helpers import CallbackRecorder, connected_client, make_spec, residual_client_tasks


async def test_happy_path_returns_validated_usage_after_ordered_update_drain(tmp_path: Path) -> None:
    client, callbacks = await connected_client(tmp_path)
    try:
        outcome = await client.prompt("hello")
    finally:
        await client.aclose()

    assert outcome.stop_reason == "end_turn"
    assert outcome.usage is not None
    assert outcome.usage.total_tokens == 8
    assert [seq for seq, _notification in callbacks.updates] == [1]
    assert callbacks.updates[0][1].update.content.text == "stub response"


async def test_burst_updates_are_drained_before_prompt_returns(tmp_path: Path) -> None:
    client, callbacks = await connected_client(
        tmp_path,
        scenario="burst",
        extra_env={"CHRYS_ACP_STUB_BURST_COUNT": "250"},
    )
    try:
        outcome = await client.prompt("burst")
    finally:
        await client.force_close()

    assert outcome.stop_reason == "end_turn"
    assert len(callbacks.updates) == 250
    assert [sequence for sequence, _notification in callbacks.updates] == list(range(1, 251))
    assert [notification.update.content.text for _seq, notification in callbacks.updates] == [
        f"{index}," for index in range(250)
    ]


async def test_message_id_boundaries_arrive_verbatim_and_in_wire_order(tmp_path: Path) -> None:
    client, callbacks = await connected_client(tmp_path, scenario="message_id_boundaries")
    try:
        await client.prompt("edge updates")
    finally:
        await client.force_close()

    observed = [
        (notification.update.message_id, notification.update.content.text) for _seq, notification in callbacks.updates
    ]
    assert observed == [
        ("11111111-1111-4111-8111-111111111111", "first"),
        ("11111111-1111-4111-8111-111111111111", " continuation"),
        ("22222222-2222-4222-8222-222222222222", "second"),
    ]


async def test_non_text_chunks_preserve_content_kinds_in_wire_order(tmp_path: Path) -> None:
    client, callbacks = await connected_client(tmp_path, scenario="non_text_chunks")
    try:
        await client.prompt("edge updates")
    finally:
        await client.force_close()

    assert [notification.update.content.type for _seq, notification in callbacks.updates] == [
        "image",
        "audio",
        "resource_link",
    ]


async def test_tool_anomalies_are_forwarded_unreordered_for_the_translator(tmp_path: Path) -> None:
    client, callbacks = await connected_client(tmp_path, scenario="tool_anomalies")
    try:
        await client.prompt("edge updates")
    finally:
        await client.force_close()

    observed = [
        (
            notification.update.session_update,
            getattr(notification.update, "tool_call_id", None),
            getattr(notification.update, "status", None),
        )
        for _seq, notification in callbacks.updates
    ]
    assert observed == [
        ("tool_call_update", "tool-1", "completed"),
        ("tool_call", "tool-1", None),
        ("tool_call_update", "tool-1", "failed"),
        ("tool_call_update", "tool-2", None),
        ("tool_call", "tool-3", "completed"),
        ("agent_message_chunk", None, None),
    ]


async def test_update_queue_overflow_fails_without_dropping_silently(tmp_path: Path) -> None:
    callbacks = CallbackRecorder()
    callbacks.update_gate = asyncio.Event()
    client, _callbacks = await connected_client(
        tmp_path,
        scenario="queue_overflow",
        callbacks=callbacks,
        idle_timeout_seconds=0,
    )
    try:
        with pytest.raises(AcpTransportError):
            await client.prompt("overflow")
    finally:
        callbacks.update_gate.set()
        await client.force_close()

    assert len(callbacks.updates) == 1


async def test_permission_and_ask_user_requests_use_typed_callback_seams(tmp_path: Path) -> None:
    permission_client, permission_callbacks = await connected_client(tmp_path / "permission", scenario="permission")
    try:
        await permission_client.prompt("permission")
    finally:
        await permission_client.force_close()
    ask_client, ask_callbacks = await connected_client(tmp_path / "ask", scenario="ask_user")
    try:
        await ask_client.prompt("ask")
    finally:
        await ask_client.force_close()

    assert len(permission_callbacks.permission_calls) == 1
    assert permission_callbacks.permission_calls[0][0].tool_call_id == "permission-1"
    assert ask_callbacks.ext_calls[0][0] == "chrys/request_input"


async def test_permission_denial_selects_the_reject_option(tmp_path: Path) -> None:
    callbacks = CallbackRecorder()
    callbacks.permission_decision = PermissionDecision.deny("deny")
    client, _callbacks = await connected_client(
        tmp_path,
        scenario="permission",
        callbacks=callbacks,
    )
    try:
        await client.prompt("deny")
    finally:
        await client.force_close()

    assert callbacks.updates[0][1].update.content.text == "permission:deny"


async def test_idle_timeout_is_suspended_only_while_human_callback_waits(tmp_path: Path) -> None:
    callbacks = CallbackRecorder()
    callbacks.permission_gate = asyncio.Event()
    client, _callbacks = await connected_client(
        tmp_path,
        scenario="permission_then_stall",
        callbacks=callbacks,
        idle_timeout_seconds=0.1,
    )
    prompt_task = asyncio.create_task(client.prompt("permission wait"))
    try:
        await wait_for(lambda: len(callbacks.permission_calls) == 1, description="permission callback")
        assert not await wait_until(prompt_task.done, timeout=0.2, interval=0.01)
        callbacks.permission_gate.set()
        with pytest.raises(AcpIdleTimeoutError):
            await prompt_task
    finally:
        callbacks.permission_gate.set()
        await client.force_close()


async def test_idle_stall_is_pause_class_and_cancel_is_nonblocking(tmp_path: Path) -> None:
    client, _callbacks = await connected_client(
        tmp_path,
        scenario="idle_stall",
        idle_timeout_seconds=0.1,
    )
    try:
        with pytest.raises(AcpIdleTimeoutError):
            await client.prompt("stall")
        await client.cancel()
    finally:
        await client.force_close()


async def test_crash_mid_prompt_is_pause_class_and_reaps_sdk_task(tmp_path: Path) -> None:
    client, _callbacks = await connected_client(tmp_path, scenario="crash_mid_prompt")
    try:
        with pytest.raises(AcpTransportError):
            await client.prompt("crash")
    finally:
        await client.force_close()


async def test_prompt_response_wins_over_immediately_buffered_eof(tmp_path: Path) -> None:
    client, _callbacks = await connected_client(tmp_path, scenario="response_then_eof")
    try:
        outcome = await client.prompt("last response")
    finally:
        await client.force_close()

    assert outcome.stop_reason == "end_turn"
    assert outcome.usage is not None
    assert outcome.usage.total_tokens == 5


async def test_connect_complete_invalid_banner_is_config_error_and_rolls_back(tmp_path: Path) -> None:
    client = AcpAgentClient(make_spec(tmp_path, scenario="banner"), CallbackRecorder())

    with pytest.raises(AcpConfigError):
        await client.connect()

    assert client._spawn is not None
    assert client._spawn.process.returncode is not None
    await client.force_close()


@pytest.mark.parametrize("scenario", ["protocol_mismatch", "protocol_bool", "protocol_float"])
async def test_protocol_version_mismatch_and_sdk_coercions_are_config_errors(
    tmp_path: Path,
    scenario: str,
) -> None:
    client = AcpAgentClient(make_spec(tmp_path, scenario=scenario), CallbackRecorder())

    with pytest.raises(AcpConfigError, match="protocol"):
        await client.connect()

    await client.force_close()


async def test_connect_cancellation_rolls_back_spawn_and_preserves_cancellation(tmp_path: Path) -> None:
    client = AcpAgentClient(make_spec(tmp_path, scenario="initialize_stall"), CallbackRecorder())
    connect_task = asyncio.create_task(client.connect())
    # Spawning the stub is a cold process start.
    await wait_for(
        lambda: client._spawn is not None or connect_task.done(),
        timeout=ENGINE_TURN_TIMEOUT,
        description="ACP child spawn",
    )
    spawn = client._spawn
    assert spawn is not None, connect_task.result()
    await client.cancel()
    assert not client.stateful_phase_started

    connect_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await connect_task

    assert spawn.process.returncode is not None


async def test_concurrent_connect_attempts_spawn_exactly_one_child(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spawned = 0
    real_spawn = client_mod.spawn_acp_process

    async def counting_spawn(spec: object) -> object:
        nonlocal spawned
        spawned += 1
        return await real_spawn(spec)

    monkeypatch.setattr(client_mod, "spawn_acp_process", counting_spawn)
    client = AcpAgentClient(make_spec(tmp_path), CallbackRecorder())
    first = asyncio.create_task(client.connect())
    second = asyncio.create_task(client.connect())
    try:
        # _conn installs only after the spawn await, so without a synchronous
        # claim both calls spawn and the second installation strands the
        # first child beyond force_close's reach.
        with pytest.raises(RuntimeError, match="already been started"):
            await second
        await first
        assert spawned == 1
    finally:
        await client.force_close()


async def test_prompt_is_single_use_per_client(tmp_path: Path) -> None:
    client, _callbacks = await connected_client(tmp_path)
    try:
        outcome = await client.prompt("hello")
        assert outcome.stop_reason == "end_turn"
        # A second prompt would reset the shared registration state and read
        # ACP's session-cumulative usage as per-attempt usage.
        with pytest.raises(RuntimeError, match="prompt has already been started"):
            await client.prompt("again")
    finally:
        await client.aclose()


async def test_connect_after_force_close_is_rejected(tmp_path: Path) -> None:
    client = AcpAgentClient(make_spec(tmp_path), CallbackRecorder())
    await client.force_close()

    # The cached force-close task has already completed and never runs again,
    # so a connect succeeding here would leak an unkillable child.
    with pytest.raises(RuntimeError, match="closed"):
        await client.connect()

    assert client._spawn is None


async def test_force_close_completing_during_spawn_disposes_the_late_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from chrys.service.acp_client.spawn import AcpSpawnResult

    client = AcpAgentClient(make_spec(tmp_path), CallbackRecorder())
    spawned: list[AcpSpawnResult] = []
    real_spawn = client_mod.spawn_acp_process

    async def spawn_after_close(spec: object) -> AcpSpawnResult:
        # The whole force-close ladder completes while connect is still
        # suspended in process creation.
        await client.force_close()
        result = await real_spawn(spec)
        spawned.append(result)
        return result

    monkeypatch.setattr(client_mod, "spawn_acp_process", spawn_after_close)

    with pytest.raises(AcpConnectError):
        await client.connect()

    assert len(spawned) == 1
    assert spawned[0].process.returncode is not None


async def test_auth_required_is_nonretryable_and_lists_methods(tmp_path: Path) -> None:
    client = AcpAgentClient(make_spec(tmp_path, scenario="auth_required"), CallbackRecorder())
    await client.connect()
    try:
        with pytest.raises(AcpAuthRequiredError) as info:
            await client.open_session()
    finally:
        await client.force_close()

    assert info.value.method_names == ("Agent login",)


async def test_fail_closed_mode_rejection_and_best_effort_override(tmp_path: Path) -> None:
    strict = AcpAgentClient(
        make_spec(tmp_path / "strict", scenario="mode_rejection", session_mode="review"),
        CallbackRecorder(),
    )
    await strict.connect()
    try:
        with pytest.raises(AcpConfigError):
            await strict.open_session()
    finally:
        await strict.force_close()

    best_effort = AcpAgentClient(
        make_spec(
            tmp_path / "best-effort",
            scenario="mode_rejection",
            session_mode="review",
            best_effort_options=True,
        ),
        CallbackRecorder(),
    )
    await best_effort.connect()
    try:
        handshake = await best_effort.open_session()
        assert handshake.session_id == "stub-session"
    finally:
        await best_effort.force_close()


@pytest.mark.parametrize(
    ("scenario", "kwargs"),
    [
        ("mode_parse_error", {"session_mode": "review", "best_effort_options": True}),
        ("model_resource_error", {"model_id": "stub-model"}),
        ("option_parse_error", {"config_options": {"safe": False}, "best_effort_options": True}),
    ],
)
async def test_best_effort_tolerates_only_method_not_found_and_invalid_params(
    tmp_path: Path,
    scenario: str,
    kwargs: dict[str, object],
) -> None:
    client = AcpAgentClient(make_spec(tmp_path, scenario=scenario, **kwargs), CallbackRecorder())
    await client.connect()
    try:
        with pytest.raises(AcpConfigError):
            await client.open_session()
    finally:
        await client.force_close()


async def test_best_effort_config_option_accepts_explicit_invalid_params(tmp_path: Path) -> None:
    client = AcpAgentClient(
        make_spec(
            tmp_path,
            scenario="option_rejection",
            config_options={"safe": False},
            best_effort_options=True,
        ),
        CallbackRecorder(),
    )
    await client.connect()
    try:
        handshake = await client.open_session()
    finally:
        await client.force_close()

    assert handshake.session_id == "stub-session"


async def test_dependent_config_options_follow_the_returned_state(tmp_path: Path) -> None:
    client = AcpAgentClient(
        make_spec(
            tmp_path,
            scenario="dependent_config_options",
            config_options={"safe": False, "turbo": True},
        ),
        CallbackRecorder(),
    )
    await client.connect()
    try:
        handshake = await client.open_session()
    finally:
        await client.force_close()

    # "turbo" only exists after "safe" is applied: later options must be
    # validated against each returned replacement set, and the handshake
    # carries the final state, not the session/new snapshot.
    assert [option.id for option in handshake.config_options] == ["safe", "turbo"]
    assert handshake.config_options[1].current_value is True


async def test_handshake_reports_applied_mode_and_model_not_snapshot(tmp_path: Path) -> None:
    client = AcpAgentClient(
        make_spec(tmp_path, session_mode="review", model_id="alt-model"),
        CallbackRecorder(),
    )
    await client.connect()
    try:
        handshake = await client.open_session()
    finally:
        await client.force_close()

    # session/new snapshots default/stub-model; both switches were accepted,
    # so the handshake must carry the applied state, not the stale snapshot
    # (config options already behave this way via the returned sets).
    assert handshake.modes is not None
    assert handshake.modes.current_mode_id == "review"
    assert handshake.models is not None
    assert handshake.models.current_model_id == "alt-model"


async def test_unadvertised_model_is_narrowly_best_effort(tmp_path: Path) -> None:
    client = AcpAgentClient(make_spec(tmp_path, model_id="missing-model"), CallbackRecorder())
    await client.connect()
    try:
        handshake = await client.open_session()
    finally:
        await client.force_close()

    assert handshake.session_id == "stub-session"


async def test_advertised_model_deterministic_rejection_is_narrowly_best_effort(tmp_path: Path) -> None:
    client = AcpAgentClient(
        make_spec(tmp_path, scenario="model_rejection", model_id="stub-model"),
        CallbackRecorder(),
    )
    await client.connect()
    try:
        handshake = await client.open_session()
    finally:
        await client.force_close()

    assert handshake.session_id == "stub-session"


async def test_additional_directories_are_capability_gated_before_session_request(tmp_path: Path) -> None:
    other_root = (tmp_path / "other").resolve()
    other_root.mkdir(parents=True)
    client = AcpAgentClient(
        make_spec(
            tmp_path,
            scenario="no_additional_capability",
            additional_directories=(str(other_root),),
        ),
        CallbackRecorder(),
    )
    await client.connect()
    try:
        handshake = await client.open_session()
    finally:
        await client.force_close()

    assert handshake.session_id == "stub-session"


@pytest.mark.parametrize(("scenario", "reason"), [("refusal", "refusal"), ("empty", "end_turn")])
async def test_terminal_stop_reasons_remain_client_outcomes(
    tmp_path: Path,
    scenario: str,
    reason: str,
) -> None:
    client, _callbacks = await connected_client(tmp_path, scenario=scenario)
    try:
        outcome = await client.prompt("outcome")
    finally:
        await client.force_close()

    assert outcome.stop_reason == reason


async def test_raw_usage_survives_malformed_prompt_response(tmp_path: Path) -> None:
    client, _callbacks = await connected_client(tmp_path, scenario="malformed_prompt_response")
    try:
        with pytest.raises(AcpTransportError) as info:
            await client.prompt("malformed")
    finally:
        await client.force_close()

    assert info.value.usage is not None
    assert info.value.usage.total_tokens == 34


async def test_raw_usage_survives_prompt_error_response(tmp_path: Path) -> None:
    client, _callbacks = await connected_client(tmp_path, scenario="prompt_internal_with_usage")
    try:
        with pytest.raises(AcpTransportError) as info:
            await client.prompt("error usage")
    finally:
        await client.force_close()

    assert info.value.usage is not None
    assert info.value.usage.total_tokens == 21


async def test_absent_prompt_usage_stays_unreported(tmp_path: Path) -> None:
    client, _callbacks = await connected_client(tmp_path, scenario="usage_absent")
    try:
        outcome = await client.prompt("no usage")
    finally:
        await client.force_close()

    assert outcome.usage is None


async def test_unpaired_surrogate_prompt_fails_config_without_hanging(tmp_path: Path) -> None:
    client, _callbacks = await connected_client(tmp_path)
    try:
        with pytest.raises(AcpConfigError, match="Unicode"):
            await client.prompt(chr(0xD800))
    finally:
        await client.force_close()


async def test_foreign_updates_and_invalid_usage_updates_are_dropped(tmp_path: Path) -> None:
    foreign, foreign_callbacks = await connected_client(tmp_path / "foreign", scenario="foreign_update")
    try:
        await foreign.prompt("foreign")
    finally:
        await foreign.force_close()
    invalid, invalid_callbacks = await connected_client(
        tmp_path / "invalid",
        scenario="invalid_usage_updates",
    )
    try:
        await invalid.prompt("invalid usage")
    finally:
        await invalid.force_close()

    assert len(foreign_callbacks.updates) == 1
    assert foreign_callbacks.updates[0][1].update.content.text == "local"
    assert len(invalid_callbacks.updates) == 1
    assert invalid_callbacks.updates[0][1].update.content.text == "valid after invalid usage"


async def test_raw_meta_collision_and_unknown_extension_never_reach_callbacks(tmp_path: Path) -> None:
    collision, collision_callbacks = await connected_client(tmp_path / "collision", scenario="meta_collision")
    try:
        await collision.prompt("collision")
    finally:
        await collision.force_close()
    unknown, unknown_callbacks = await connected_client(tmp_path / "unknown", scenario="unknown_ext")
    try:
        await unknown.prompt("unknown")
    finally:
        await unknown.force_close()
    extra, extra_callbacks = await connected_client(
        tmp_path / "extra",
        scenario="ask_user_extra_field",
    )
    try:
        await extra.prompt("extra")
    finally:
        await extra.force_close()

    assert collision_callbacks.permission_calls == []
    assert [update.update.content.text for _seq, update in collision_callbacks.updates] == [
        "preflight:_meta:-32602",
        "preflight:_meta:-32602",
        "preflight:field_meta:-32602",
    ]
    assert unknown_callbacks.ext_calls == []
    assert unknown_callbacks.updates[0][1].update.content.text == "extension:-32601"
    assert extra_callbacks.ext_calls == []
    assert extra_callbacks.updates[0][1].update.content.text == "ask-preflight:-32602"


async def test_depth_is_injected_as_parent_depth_plus_one(tmp_path: Path) -> None:
    client, callbacks = await connected_client(tmp_path, scenario="depth", depth=2)
    try:
        await client.prompt("depth")
    finally:
        await client.force_close()

    assert callbacks.updates[0][1].update.content.text == "3"


async def test_duplicate_outstanding_inbound_id_fails_attempt(tmp_path: Path) -> None:
    client, _callbacks = await connected_client(tmp_path, scenario="duplicate_inbound_id")
    try:
        with pytest.raises(AcpTransportError):
            await client.prompt("duplicate")
    finally:
        await client.force_close()


async def test_callback_failure_fails_attempt(tmp_path: Path) -> None:
    callbacks = CallbackRecorder()
    callbacks.fail_updates = True
    client, _callbacks = await connected_client(tmp_path, callbacks=callbacks)
    try:
        with pytest.raises(AcpTransportError):
            await client.prompt("callback failure")
    finally:
        await client.force_close()


@pytest.mark.parametrize(
    ("scenario", "failure_field"),
    [("permission", "fail_permission"), ("ask_user", "fail_ext")],
)
async def test_human_callback_failure_fails_attempt(
    tmp_path: Path,
    scenario: str,
    failure_field: str,
) -> None:
    callbacks = CallbackRecorder()
    setattr(callbacks, failure_field, True)
    client, _callbacks = await connected_client(tmp_path, scenario=scenario, callbacks=callbacks)
    try:
        with pytest.raises(AcpTransportError):
            await client.prompt("callback failure")
    finally:
        await client.force_close()


async def test_usage_survives_callback_drain_failure_after_response(tmp_path: Path) -> None:
    callbacks = CallbackRecorder()
    callbacks.fail_updates = True
    callbacks.update_gate = asyncio.Event()
    client, _callbacks = await connected_client(tmp_path, callbacks=callbacks, idle_timeout_seconds=0)
    prompt_task = asyncio.create_task(client.prompt("callback failure after response"))
    try:
        await wait_for(
            lambda: (
                client._store is not None
                and client._store.prompt_id is not None
                and (record := client._store.record_for(client._store.prompt_id)) is not None
                and record.response_observed
            ),
            description="prompt response observation",
        )
        callbacks.update_gate.set()
        with pytest.raises(AcpTransportError) as info:
            await prompt_task
    finally:
        callbacks.update_gate.set()
        await client.force_close()
        await asyncio.gather(prompt_task, return_exceptions=True)

    assert info.value.usage is not None
    assert info.value.usage.total_tokens == 8


async def test_force_close_is_idempotent_and_wedged_close_rpc_is_bounded(tmp_path: Path) -> None:
    client, _callbacks = await connected_client(tmp_path, scenario="wedged_close")

    await client.aclose()
    await client.force_close()


async def test_force_close_resolves_an_inflight_prompt_completion_barrier(tmp_path: Path) -> None:
    client, _callbacks = await connected_client(
        tmp_path,
        scenario="idle_stall",
        idle_timeout_seconds=0,
    )
    prompt_task = asyncio.create_task(client.prompt("stall"))
    await wait_for(
        lambda: client._store is not None and client._store.prompt_id is not None,
        description="prompt request registration",
    )

    await client.force_close()

    with pytest.raises(AcpTransportError):
        await asyncio.wait_for(prompt_task, timeout=2)


async def test_aclose_cancellation_still_completes_force_close_ladder(tmp_path: Path) -> None:
    client, _callbacks = await connected_client(tmp_path, scenario="wedged_close")
    close_task = asyncio.create_task(client.aclose())
    await wait_for(
        lambda: (
            client._store is not None
            and any(record.method == "session/close" for record in client._store._outgoing.values())
        ),
        description="session close request",
    )
    spawn = client._spawn
    assert spawn is not None

    close_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await close_task

    assert spawn.process.returncode is not None


async def test_permission_flood_beyond_cap_is_auto_rejected_without_callbacks(tmp_path: Path) -> None:
    callbacks = CallbackRecorder()
    callbacks.permission_gate = asyncio.Event()
    client, _callbacks = await connected_client(tmp_path, scenario="permission_flood", callbacks=callbacks)
    prompt_task = asyncio.create_task(client.prompt("flood"))
    try:
        await wait_for(
            lambda: (
                client._pump is not None and client._pump._request_frames == 12 and len(callbacks.permission_calls) == 8
            ),
            description="permission flood arrival",
        )
        callbacks.permission_gate.set()
        await prompt_task
    finally:
        callbacks.permission_gate.set()
        await client.force_close()
        await asyncio.gather(prompt_task, return_exceptions=True)

    assert len(callbacks.permission_calls) == 8
    assert callbacks.updates[-1][1].update.content.text == "flood:8,4"


async def test_open_session_fails_before_enqueue_when_terminal_already_settled(tmp_path: Path) -> None:
    client = AcpAgentClient(make_spec(tmp_path), CallbackRecorder())
    await client.connect()
    try:
        client._require_terminal_failure().set_result(EOFError("settled after connect"))
        # Failing at entry keeps the classification connect-phase (retryable)
        # and never enters the stateful phase — pre-fix this sent session/new
        # and raised the stateful AcpTransportError only afterwards.
        with pytest.raises(AcpConnectError):
            await client.open_session()
        assert not client.stateful_phase_started
        assert client._session_id is None
    finally:
        await client.force_close()


async def test_prompt_never_schedules_after_a_settled_terminal_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _callbacks = await connected_client(tmp_path)
    sent_methods: list[object] = []
    real_send = client_mod._RetainedSender.send

    async def recording_send(self: object, payload: dict) -> None:
        sent_methods.append(payload.get("method"))
        return await real_send(self, payload)

    monkeypatch.setattr(client_mod._RetainedSender, "send", recording_send)
    try:
        client._require_terminal_failure().set_result(EOFError("settled before prompt"))
        with pytest.raises(AcpTransportError, match="before a prompt response"):
            await client.prompt("never")
        # Even an immediately-cancelled conn.prompt reaches the sender queue;
        # the only safe point to fail is before create_task.
        assert "session/prompt" not in sent_methods
    finally:
        await client.force_close()


async def test_stateful_phase_begins_at_open_session_entry_before_enqueue(tmp_path: Path) -> None:
    client = AcpAgentClient(make_spec(tmp_path, scenario="crash_on_new_session"), CallbackRecorder())
    await client.connect()
    try:
        with pytest.raises(AcpTransportError):
            await client.open_session()
    finally:
        await client.force_close()


async def test_truncated_handshake_frame_is_a_retryable_connect_failure(tmp_path: Path) -> None:
    client = AcpAgentClient(make_spec(tmp_path, scenario="truncated"), CallbackRecorder())

    with pytest.raises(AcpConnectError):
        await client.connect()

    await client.force_close()


async def test_oversized_stdout_frame_hits_the_full_reader_guard(tmp_path: Path) -> None:
    # A long idle timeout keeps the watchdog out of the race: the flood is one
    # unfinished line, which never counts as protocol activity by design.
    client, _callbacks = await connected_client(tmp_path, scenario="oversized_frame", idle_timeout_seconds=60.0)
    try:
        with pytest.raises(AcpTransportError, match="oversized"):
            await client.prompt("flood")
    finally:
        await client.force_close()


async def test_oversized_ext_notification_is_transport_fatal_before_sdk_forwarding(tmp_path: Path) -> None:
    # Only session/update rides the observer caps; an ext/unknown notification
    # past the item cap must die at the pump before the SDK re-parses it.
    client, _callbacks = await connected_client(tmp_path, scenario="oversized_notification")
    try:
        with pytest.raises(AcpTransportError, match="notification"):
            await client.prompt("flood")
    finally:
        await client.force_close()


async def test_a_sub_agent_writing_a_large_file_keeps_its_connection(tmp_path: Path) -> None:
    client, callbacks = await connected_client(tmp_path, scenario="large_payloads")
    try:
        outcome = await client.prompt("write a large file")
    finally:
        await client.force_close()

    assert outcome.stop_reason == "end_turn"
    content_length = 1024 * 1024 + 64
    [(permission_call, _options)] = callbacks.permission_calls
    assert len(permission_call.raw_input["content"]) == content_length
    updates = [notification.update for _seq, notification in callbacks.updates]
    assert [len(update.raw_input["content"]) for update in updates if isinstance(update, ToolCallStart)] == [
        content_length
    ]
    assert [len(update.raw_output) for update in updates if isinstance(update, ToolCallProgress)] == [content_length]
    assert updates[-1].content.text == "permission:allow"


async def test_a_sub_agent_patching_hundreds_of_files_keeps_its_connection(tmp_path: Path) -> None:
    client, callbacks = await connected_client(tmp_path, scenario="wide_patch")
    try:
        outcome = await client.prompt("rename across the repository")
    finally:
        await client.force_close()

    assert outcome.stop_reason == "end_turn"
    [(permission_call, _options)] = callbacks.permission_calls
    assert len(permission_call.raw_input["changes"]) == WIDE_PATCH_FILES
    assert len(permission_call.content) == WIDE_PATCH_FILES
    updates = [notification.update for _seq, notification in callbacks.updates]
    [start] = [update for update in updates if isinstance(update, ToolCallStart)]
    assert len(start.content) == len(start.locations) == WIDE_PATCH_FILES
    assert updates[-1].content.text == "permission:allow"


async def test_crash_recovery_leaves_no_residual_tasks_or_pending_futures(tmp_path: Path) -> None:
    before = asyncio.all_tasks()
    client, _callbacks = await connected_client(tmp_path, scenario="crash_mid_prompt")
    with pytest.raises(AcpTransportError):
        await client.prompt("crash")
    await client.force_close()

    assert client._store is not None
    assert client._store._outgoing == {}
    await wait_for(lambda: not residual_client_tasks(before), description="client task reaping")


async def test_force_close_releases_every_resource_when_the_process_wait_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _callbacks = await connected_client(tmp_path, scenario="idle_stall", idle_timeout_seconds=0)
    kills: list[str] = []
    real_kill_tree = ManagedStdioProcess.kill_tree

    def recording_kill_tree(self: ManagedStdioProcess) -> None:
        kills.append("kill")
        real_kill_tree(self)

    async def failing_wait(self: ManagedStdioProcess) -> int:
        raise OSError(22, "wait failed")

    monkeypatch.setattr(ManagedStdioProcess, "kill_tree", recording_kill_tree)
    monkeypatch.setattr(ManagedStdioProcess, "wait", failing_wait)

    # The close task is cached, so an escaping wait failure would leave every
    # later force_close re-raising with the transports still open.
    await client.force_close()

    assert kills == ["kill"]
    assert client._consumer_task is not None and client._consumer_task.done()
    spawn = client._spawn
    assert spawn is not None
    assert spawn.stderr._task is None
    await client.force_close()


async def test_kill_sweep_runs_even_when_terminate_reaps_the_direct_child(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from chrys.service.acp_client import client as client_mod

    client, _callbacks = await connected_client(tmp_path, scenario="idle_stall", idle_timeout_seconds=0)
    calls: list[str] = []
    real_kill_tree = ManagedStdioProcess.kill_tree
    monkeypatch.setattr(ManagedStdioProcess, "terminate_tree", lambda self: calls.append("terminate"))

    def recording_kill_tree(self: ManagedStdioProcess) -> None:
        calls.append("kill")
        real_kill_tree(self)

    monkeypatch.setattr(ManagedStdioProcess, "kill_tree", recording_kill_tree)
    # Deterministically simulate the round-1 P1 interleaving: stdin EOF does
    # not end the child, but the direct child is reaped right after SIGTERM
    # while a descendant survives — the sweep must still run.
    waits = iter([False, True])

    async def scripted_wait(spawn: object, timeout_seconds: float) -> bool:
        return next(waits, True)

    monkeypatch.setattr(client_mod, "_wait_process_bounded", scripted_wait)
    await client.force_close()

    assert calls == ["terminate", "kill"]


async def test_wedged_sdk_close_takes_the_emergency_path_without_leaks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(transport_mod, "_CLOSE_STAGE_TIMEOUT_SECONDS", 0.2)
    kill_sweeps: list[None] = []
    original_kill_tree = ManagedStdioProcess.kill_tree

    def recording_kill_tree(self: ManagedStdioProcess) -> None:
        kill_sweeps.append(None)
        original_kill_tree(self)

    monkeypatch.setattr(ManagedStdioProcess, "kill_tree", recording_kill_tree)
    before = asyncio.all_tasks()
    client, _callbacks = await connected_client(tmp_path)
    outcome = await client.prompt("hello")
    assert outcome.stop_reason == "end_turn"

    async def wedged_close() -> None:
        await asyncio.Event().wait()

    assert client._conn is not None
    monkeypatch.setattr(client._conn, "close", wedged_close)
    await client.force_close()

    assert client._store is not None
    assert client._store._outgoing == {}
    assert kill_sweeps
    await wait_for(lambda: not residual_client_tasks(before), description="emergency task reaping")


async def test_cancellation_swallowing_sdk_close_cannot_stall_the_ladder(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(transport_mod, "_CLOSE_STAGE_TIMEOUT_SECONDS", 0.2)
    kill_sweeps: list[None] = []
    original_kill_tree = ManagedStdioProcess.kill_tree

    def recording_kill_tree(self: ManagedStdioProcess) -> None:
        kill_sweeps.append(None)
        original_kill_tree(self)

    monkeypatch.setattr(ManagedStdioProcess, "kill_tree", recording_kill_tree)
    before = asyncio.all_tasks()
    client, _callbacks = await connected_client(tmp_path)
    outcome = await client.prompt("hello")
    assert outcome.stop_reason == "end_turn"

    release = asyncio.Event()

    async def swallowing_wedged_close() -> None:
        while True:
            try:
                await release.wait()
                return
            except asyncio.CancelledError:
                continue

    assert client._conn is not None
    monkeypatch.setattr(client._conn, "close", swallowing_wedged_close)
    force_close_task = asyncio.ensure_future(client.force_close())
    done, _ = await asyncio.wait({force_close_task}, timeout=10.0)

    assert force_close_task in done
    assert kill_sweeps
    release.set()
    await wait_for(lambda: not residual_client_tasks(before), description="abandoned close release")


async def test_final_drain_is_bounded_when_the_sink_wedges_and_idle_expiry_is_disabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(client_mod, "_FINAL_DRAIN_TIMEOUT_SECONDS", 0.5)
    recorder = CallbackRecorder()
    recorder.update_gate = asyncio.Event()
    client, _callbacks = await connected_client(
        tmp_path,
        callbacks=recorder,
        idle_timeout_seconds=0,
    )

    try:
        with pytest.raises(AcpTransportError, match="before prompt completion"):
            await client.prompt("hello")
    finally:
        recorder.update_gate = None
        await client.aclose()


async def test_forwarded_sdk_reader_uses_the_full_acp_guard(tmp_path: Path) -> None:
    client, _callbacks = await connected_client(tmp_path)
    try:
        pump = client._pump
        assert pump is not None
        assert pump._sdk_reader._limit == client_mod.ACP_STDIO_LIMIT_BYTES
        assert pump._raw_reader._limit == client_mod.ACP_STDIO_LIMIT_BYTES
    finally:
        await client.force_close()


async def test_pump_announces_the_live_session_before_open_session_resumes(tmp_path: Path) -> None:
    client, _callbacks = await connected_client(tmp_path)
    try:
        # The live pump must have sniffed the session/new response itself:
        # a no-op on_response wiring leaves the announcement unset.
        assert client._session_id is not None
        assert client._announced_session_id == client._session_id
    finally:
        await client.force_close()


async def test_open_session_is_single_use(tmp_path: Path) -> None:
    client, _callbacks = await connected_client(tmp_path)
    try:
        with pytest.raises(RuntimeError, match="already been opened"):
            await client.open_session()
    finally:
        await client.force_close()


async def test_empty_session_id_fails_open_session_instead_of_wedging_the_client(tmp_path: Path) -> None:
    callbacks = CallbackRecorder()
    client = AcpAgentClient(make_spec(tmp_path, scenario="empty_session_id"), callbacks)
    await client.connect()
    try:
        with pytest.raises(AcpConfigError, match="invalid session id"):
            await client.open_session()
    finally:
        await client.force_close()


async def test_close_drain_lets_inflight_permission_responses_reach_the_agent(tmp_path: Path) -> None:
    callbacks = CallbackRecorder()
    callbacks.permission_gate = asyncio.Event()
    client, _callbacks = await connected_client(tmp_path, scenario="permission", callbacks=callbacks)
    prompt_task = asyncio.create_task(client.prompt("permission wait"))
    await wait_for(lambda: len(callbacks.permission_calls) == 1, description="permission callback")

    close_task = asyncio.create_task(client.force_close())
    await wait_for(lambda: client._closing, description="close ladder started")
    callbacks.permission_gate.set()
    await close_task
    with contextlib.suppress(BaseException):
        await prompt_task

    # The ladder drains runners before the sender's terminal stop (responses
    # are exempt from begin_close by design — pinned separately), so the
    # response bytes reached the agent; its stderr proof survives teardown.
    stderr_log = tmp_path.resolve() / "stub.stderr.log"

    def _saw_outcome() -> bool:
        try:
            return "permission-outcome:allow" in stderr_log.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return False

    await wait_for(_saw_outcome, description="stub-side permission response proof")


async def test_protocol_mismatch_with_immediate_exit_stays_a_config_error(tmp_path: Path) -> None:
    client = AcpAgentClient(make_spec(tmp_path, scenario="protocol_mismatch_exit"), CallbackRecorder())

    # The agent's EOF can land before connect() resumes; the deterministic
    # protocol rejection must not be displaced by the retryable EOF terminal.
    with pytest.raises(AcpConfigError, match="protocol"):
        await client.connect()

    await client.force_close()


async def test_updates_after_the_prompt_response_never_reach_the_sink(tmp_path: Path) -> None:
    callbacks = CallbackRecorder()
    callbacks.update_gate = asyncio.Event()
    client, _callbacks = await connected_client(tmp_path, scenario="updates_after_response", callbacks=callbacks)
    try:
        prompt_task = asyncio.create_task(client.prompt("sealed"))
        # The EOF behind the post-response update settles the terminal future
        # only after the pump observed every preceding frame, so releasing the
        # gate here guarantees the late update already went through intake.
        await wait_for(
            lambda: client._terminal_failure is not None and client._terminal_failure.done(),
            description="post-update EOF observed",
        )
        callbacks.update_gate.set()
        outcome = await prompt_task

        assert outcome.stop_reason == "end_turn"
        assert outcome.usage is not None
        assert outcome.usage.total_tokens == 5
        # The pre-response update drains; the post-response one is dropped at
        # intake instead of leaking into the sink after prompt() returned.
        assert [notification.update.content.text for _seq, notification in callbacks.updates] == ["pre"]
    finally:
        await client.aclose()


async def test_terminal_failure_settled_after_the_response_still_fails_the_prompt(tmp_path: Path) -> None:
    callbacks = CallbackRecorder()
    callbacks.update_gate = asyncio.Event()
    client, _callbacks = await connected_client(tmp_path, callbacks=callbacks)
    injected = AcpTransportError("A protocol failure settled after the prompt response.")
    try:
        prompt_task = asyncio.create_task(client.prompt("post-response failure"))
        await wait_for(lambda: bool(callbacks.updates), description="update sink gated")
        # Settle the terminal future the instant the completion barrier
        # resolves — deterministically after response-wins bookkeeping, before
        # prompt() resumes — the exact window the old tail treated as success.
        completion = client._completion_future
        assert completion is not None
        completion.add_done_callback(lambda _future: client._fail(injected))
        callbacks.update_gate.set()
        with pytest.raises(AcpTransportError) as excinfo:
            await prompt_task
    finally:
        await client.aclose()

    assert excinfo.value is injected
    assert excinfo.value.usage is not None
    assert excinfo.value.usage.input_tokens == 3
    assert excinfo.value.usage.output_tokens == 5


async def test_idle_expiry_never_governs_the_local_drain_after_the_response(tmp_path: Path) -> None:
    callbacks = CallbackRecorder()
    callbacks.update_gate = asyncio.Event()
    client, _callbacks = await connected_client(tmp_path, callbacks=callbacks, idle_timeout_seconds=0.2)
    try:
        prompt_task = asyncio.create_task(client.prompt("slow sink"))
        await wait_for(lambda: bool(callbacks.updates), description="update sink gated")

        def response_observed() -> bool:
            store = client._store
            if store is None or store.prompt_id is None:
                return False
            record = store.record_for(store.prompt_id)
            return record is not None and record.response_observed

        await wait_for(response_observed, description="prompt response observed")

        # The watchdog must retire at the response seal; while the sink is
        # still gated, its idle budget alone would otherwise expire the run.
        def watchdog_gone() -> bool:
            return all(task.get_name() != "chrys.acp.idle-watchdog" for task in asyncio.all_tasks())

        await wait_for(watchdog_gone, description="idle watchdog retired")
        callbacks.update_gate.set()
        outcome = await prompt_task

        assert outcome.stop_reason == "end_turn"
        assert outcome.usage is not None
        assert outcome.usage.total_tokens == 8
    finally:
        await client.aclose()


class _PermissionCancellationRecorder(CallbackRecorder):
    def __init__(self) -> None:
        super().__init__()
        self.permission_gate = asyncio.Event()
        self.permission_cancelled = asyncio.Event()

    async def on_permission_request(
        self,
        tool_call: object,
        options: tuple[object, ...],
    ) -> PermissionDecision:
        try:
            return await super().on_permission_request(tool_call, options)  # type: ignore[arg-type]
        except asyncio.CancelledError:
            self.permission_cancelled.set()
            raise


async def test_caller_cancellation_at_the_sdk_response_await_stays_a_cancellation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _callbacks = await connected_client(tmp_path, scenario="response_then_eof")
    store = client._store
    assert store is not None
    original_resolve = store.resolve_outgoing
    original_record_for = store.record_for
    reached_response_await = asyncio.Event()
    prompt_task: asyncio.Task[object] | None = None

    # Keep the SDK prompt future pending across BOTH settlement paths — the
    # response resolution and the SDK's EOF reject_all sweep ("Connection
    # closed") — so prompt() provably parks on the pending SDK task.
    def suppress_prompt_resolution(request_id: int, result: object) -> None:
        if request_id == store.prompt_id:
            return
        original_resolve(request_id, result)

    def suppress_prompt_rejection(error: object) -> None:
        for request_id in tuple(store._outgoing):
            if request_id == store.prompt_id:
                continue
            record = store._outgoing.pop(request_id)
            store._completed[request_id] = record
            if not record.future.done():
                record.future.set_exception(error)

    def record_for_with_proof(request_id: int) -> transport_mod._OutgoingRecord | None:
        record = original_record_for(request_id)
        # prompt() calls record_for exactly once on its own task, right after
        # the terminal-woken wait, and has NO further await before
        # `await sdk_prompt_task` — so this signal proves where the prompt
        # parks next, without counted scheduler yields.
        if (
            prompt_task is not None
            and asyncio.current_task() is prompt_task
            and request_id == store.prompt_id
            and client._terminal_failure is not None
            and client._terminal_failure.done()
        ):
            reached_response_await.set()
        return record

    monkeypatch.setattr(store, "resolve_outgoing", suppress_prompt_resolution)
    monkeypatch.setattr(store, "reject_all_outgoing", suppress_prompt_rejection)
    monkeypatch.setattr(store, "record_for", record_for_with_proof)
    try:
        prompt_task = asyncio.create_task(client.prompt("cancel at the response await"))
        await wait_for(reached_response_await.is_set, description="prompt parked at the SDK-response await")
        prompt_task.cancel()
        # A swallowed cancellation would surface as a classified transport
        # error with the task uncancelled instead.
        with pytest.raises(asyncio.CancelledError):
            await prompt_task
        assert prompt_task.cancelled()
    finally:
        await client.aclose()


async def test_deferred_eof_closes_before_a_deterministic_error_response_raises(tmp_path: Path) -> None:
    callbacks = CallbackRecorder()
    callbacks.update_gate = asyncio.Event()
    client, _callbacks = await connected_client(
        tmp_path,
        scenario="update_error_response_then_eof",
        callbacks=callbacks,
    )
    try:
        prompt_task = asyncio.create_task(client.prompt("deterministic rejection on a dead transport"))
        # The gated sink parks prompt() at the drain until the EOF behind the
        # error response has settled the deferred-disconnect terminal.
        await wait_for(
            lambda: client._terminal_failure is not None and client._terminal_failure.done(),
            description="deferred disconnect settled",
        )
        callbacks.update_gate.set()
        with pytest.raises(AcpConfigError):
            await prompt_task
        # The error response wins the classification, but the known-dead
        # transport must already be closed when it is raised — the deferred
        # disconnect spawned no failure-close of its own.
        assert client._closing
    finally:
        await client.aclose()


async def test_non_disconnect_terminal_failure_beats_an_error_response(tmp_path: Path) -> None:
    callbacks = CallbackRecorder()
    callbacks.update_gate = asyncio.Event()
    client, _callbacks = await connected_client(tmp_path, scenario="update_then_internal_error", callbacks=callbacks)
    injected = AcpTransportError("A protocol failure settled after the error response.")
    try:
        prompt_task = asyncio.create_task(client.prompt("error response arbitration"))
        await wait_for(lambda: bool(callbacks.updates), description="update sink gated")
        completion = client._completion_future
        assert completion is not None
        completion.add_done_callback(lambda _future: client._fail(injected))
        callbacks.update_gate.set()
        with pytest.raises(AcpTransportError) as excinfo:
            await prompt_task
    finally:
        await client.aclose()

    # The settled protocol failure wins over the in-band RPC error, and the
    # usage reported on the error frame rides along.
    assert excinfo.value is injected
    assert excinfo.value.usage is not None
    assert excinfo.value.usage.total_tokens == 21


async def test_error_response_completion_still_cancels_in_flight_permission_callbacks(tmp_path: Path) -> None:
    callbacks = _PermissionCancellationRecorder()
    flag = tmp_path / "release-error-response.flag"
    client, _callbacks = await connected_client(
        tmp_path,
        scenario="permission_unresolved_error",
        callbacks=callbacks,
        extra_env={"CHRYS_ACP_STUB_FLAG_FILE": str(flag)},
    )
    try:
        prompt_task = asyncio.create_task(client.prompt("unresolved permission, error response"))
        await wait_for(lambda: bool(callbacks.permission_calls), description="permission callback in flight")
        flag.write_text("go")
        # A deterministic RPC rejection classifies as a config error and
        # leaves the client to the caller — but the sealed cleanup must still
        # run so the human wait does not outlive the completed prompt.
        with pytest.raises(AcpConfigError):
            await prompt_task
        assert callbacks.permission_cancelled.is_set()
    finally:
        callbacks.permission_gate.set()
        await client.aclose()


class _BlockingWaitController:
    """PR-2 seam double whose cancel_pending_waits never returns on its own."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancellations = 0

    async def cancel_pending_waits(self) -> None:
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancellations += 1
            raise


async def test_prompt_cancellation_survives_a_blocking_wait_seam_at_completion(tmp_path: Path) -> None:
    controller = _BlockingWaitController()
    client = AcpAgentClient(
        make_spec(tmp_path, scenario="empty"),
        CallbackRecorder(),
        wait_controller=controller,
    )
    await client.connect()
    await client.open_session()
    try:
        prompt_task = asyncio.create_task(client.prompt("cancel during seam"))
        # The seam child only runs once the caller is parked in the bounded
        # wait, so a started seam proves the cancellation lands there.
        await wait_for(controller.started.is_set, description="wait seam started")
        prompt_task.cancel()
        # A swallowed cancellation would hand back a successful end_turn
        # outcome instead of reaching prompt()'s CancelledError handler.
        with pytest.raises(asyncio.CancelledError):
            await prompt_task
        assert prompt_task.cancelled()
        assert controller.cancellations >= 1
        assert client._closing
    finally:
        await client.aclose()


async def test_permission_callback_in_flight_at_the_response_is_cancelled_at_completion(tmp_path: Path) -> None:
    callbacks = _PermissionCancellationRecorder()
    flag = tmp_path / "release-response.flag"
    client, _callbacks = await connected_client(
        tmp_path,
        scenario="permission_unresolved",
        callbacks=callbacks,
        extra_env={"CHRYS_ACP_STUB_FLAG_FILE": str(flag)},
    )
    try:
        prompt_task = asyncio.create_task(client.prompt("unresolved permission"))
        await wait_for(lambda: bool(callbacks.permission_calls), description="permission callback in flight")
        # Only now may the agent answer the prompt: the callback is provably
        # outstanding when the response arrives.
        flag.write_text("go")
        outcome = await prompt_task

        assert outcome.stop_reason == "end_turn"
        # prompt() must not return with the human wait still parked.
        assert callbacks.permission_cancelled.is_set()
    finally:
        callbacks.permission_gate.set()
        await client.aclose()


async def test_deny_decision_with_an_allow_option_id_never_selects(tmp_path: Path) -> None:
    callbacks = CallbackRecorder()
    callbacks.permission_decision = PermissionDecision.deny("allow")
    client, _callbacks = await connected_client(tmp_path, scenario="permission", callbacks=callbacks)
    try:
        await client.prompt("deny naming an allow option")
    finally:
        await client.force_close()

    # The id must never transmute the decision: the agent sees a cancelled
    # outcome, not a selection of its allow option.
    assert callbacks.updates[0][1].update.content.text == "permission:cancelled"


async def test_allow_decision_with_a_reject_option_id_never_selects(tmp_path: Path) -> None:
    callbacks = CallbackRecorder()
    callbacks.permission_decision = PermissionDecision.allow("deny")
    client, _callbacks = await connected_client(tmp_path, scenario="permission", callbacks=callbacks)
    try:
        await client.prompt("allow naming a reject option")
    finally:
        await client.force_close()

    assert callbacks.updates[0][1].update.content.text == "permission:cancelled"


async def test_duplicate_permission_option_ids_are_rejected_before_the_callback(tmp_path: Path) -> None:
    client, callbacks = await connected_client(tmp_path, scenario="permission_duplicate_ids")
    try:
        outcome = await client.prompt("duplicate option ids")
    finally:
        await client.force_close()

    assert outcome.stop_reason == "end_turn"
    # The ambiguity is rejected at the protocol boundary: no human
    # interaction happens for a request whose answer cannot be transmitted.
    assert callbacks.permission_calls == []
    assert callbacks.updates[0][1].update.content.text == "dup:-32602"


async def test_malformed_foreign_update_cannot_end_the_active_attempt(tmp_path: Path) -> None:
    client, callbacks = await connected_client(tmp_path, scenario="malformed_foreign_update")
    try:
        outcome = await client.prompt("foreign hazard")
    finally:
        await client.force_close()

    assert outcome.stop_reason == "end_turn"
    assert [notification.update.content.text for _seq, notification in callbacks.updates] == ["local"]


async def test_an_oversized_foreign_update_cannot_end_the_active_attempt(tmp_path: Path) -> None:
    client, callbacks = await connected_client(tmp_path, scenario="oversized_foreign_update")
    try:
        outcome = await client.prompt("oversized foreign hazard")
    finally:
        await client.force_close()

    assert outcome.stop_reason == "end_turn"
    assert [notification.update.content.text for _seq, notification in callbacks.updates] == ["local"]


async def test_a_foreign_item_in_the_update_lane_is_dropped_before_validation(tmp_path: Path) -> None:
    flag = tmp_path / "finish.flag"
    client, callbacks = await connected_client(
        tmp_path,
        scenario="text_then_flag_then_done",
        extra_env={"CHRYS_ACP_STUB_FLAG_FILE": str(flag)},
    )
    try:
        prompt_task = asyncio.create_task(client.prompt("inject a foreign item"))
        await wait_for(lambda: bool(callbacks.updates), description="first update consumed")
        # Bypass the observer's intake filter: only the consumer's raw
        # session peek stands between this item and schema validation.
        client._require_updates().put_update(
            {"sessionId": "foreign-session", "update": {"sessionUpdate": "bogus_kind"}}
        )
        flag.write_text("go")
        outcome = await prompt_task
    finally:
        await client.force_close()

    assert outcome.stop_reason == "end_turn"
    assert [notification.update.content.text for _seq, notification in callbacks.updates] == ["pre"]


async def test_handshake_cap_violation_is_a_deterministic_config_failure(tmp_path: Path) -> None:
    client = AcpAgentClient(
        make_spec(tmp_path, scenario="oversized_handshake_notification"),
        CallbackRecorder(),
    )
    # The violation replays identically on every respawn, so the connect
    # window must not classify it as a retryable connect failure.
    with pytest.raises(AcpConfigError):
        await client.connect()

    assert client._closing


async def test_deterministic_spawn_failure_survives_a_concurrent_close(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = AcpAgentClient(make_spec(tmp_path), CallbackRecorder())
    real_spawn = client_mod.spawn_acp_process

    async def close_then_fail_spawn(spec: client_mod.AcpAgentSpec) -> client_mod.AcpSpawnResult:
        # A force_close landing while process creation is pending settles the
        # terminal future with the retryable close classification before the
        # launch failure surfaces.
        await client.force_close()
        return await real_spawn(dataclasses.replace(spec, command=str(tmp_path / "missing-agent")))

    monkeypatch.setattr(client_mod, "spawn_acp_process", close_then_fail_spawn)

    # ENOENT replays identically on every retry: the settled terminal must
    # not displace the deterministic spawn classification.
    with pytest.raises(AcpSpawnError):
        await client.connect()

    assert client._closing
