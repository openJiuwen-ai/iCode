# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for Responses service-side history integration."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from chrys.foundation.config.settings import Settings
from chrys.foundation.events.bus import EventBus
from chrys.foundation.models.invocations import InvocationOrigin
from chrys.foundation.models.workspace import WorkingDir, Workspace
from chrys.kernel import AgentSession, ChatResponse, Content, Message, SessionContext
from chrys.orchestration.engine import loader as agent_lifecycle
from chrys.orchestration.engine import session_lifecycle
from chrys.orchestration.engine.assembly import assemble_agent_engine
from chrys.orchestration.engine.run.bindings import TurnBindings
from chrys.orchestration.engine.run.turn_state import TurnRuntimeState
from chrys.orchestration.engine.trajectory import TrajectoryRecorder
from chrys.orchestration.invoker.attempts import HistoryRollback
from chrys.service.agent_middleware import ToolEventMiddleware
from chrys.service.agent_middleware.control.approval import ApprovalRetrySnapshot
from chrys.service.agent_middleware.events.tool_events import ToolBatchRecord
from chrys.service.context.providers.history import (
    PRE_OUTPUT_HISTORY_LEN_STATE_KEY,
    CompressibleHistoryProvider,
    _uses_service_side_context,
)
from chrys.service.llm.clients import effective_model_base_url
from chrys.service.mutations.workspace_changes import WorkspaceChangeTracker
from chrys.service.profiles.agents.schema import AgentProfile
from chrys.service.profiles.models.schema import ModelProfile
from chrys.service.session.history import SessionHistoryManager
from chrys.service.session.message_metadata import MESSAGE_CREATED_AT_KEY
from chrys.service.session.persistence import agent_profile_context_fingerprint, model_profile_context_fingerprint
from chrys.service.state.store import JsonFileStateStore
from tests.support.components import make_current
from tests.support.loaded_agents import install_loaded_agent, make_loaded_agent, make_manifest
from tests.support.turn_services import make_turn_runner


async def _post_run(host: object) -> None:
    await make_turn_runner(
        current=host.current,
        session=host.session,
        turn_state=host._turn_state,
        history=host._history,
        bus=host._bus,
        fsm=host._fsm,
        workspace_change_tracker=host._workspace_change_tracker,
        settings_handle=SimpleNamespace(settings=host.settings),
        trajectory_recorder=host._trajectory_recorder,
        writer=host.writer,
        on_successful_turn=host._on_successful_turn,
    ).finalize_current_run()  # type: ignore[arg-type]


def _restore_lifecycle(*, session: SimpleNamespace, current: SimpleNamespace) -> session_lifecycle.SessionLifecycle:
    """Construct the restore policy with only the queried components populated."""
    return session_lifecycle.SessionLifecycle(
        session=session,
        current=current,
        loader=None,
        permits=None,
        writer=None,
        turn_state=None,
        usage_publisher=None,
        bus=None,
        fsm=None,
        history=None,
        persistence=None,
        settings_handle=None,
        agent_registry=None,
        model_registry=None,
        trajectory_recorder=None,
        workspace_change_tracker=None,
        unregister_current_engine=lambda: None,
        side_questions=None,
    )


def test_service_session_restore_compatibility_requires_enabled_response_storage() -> None:
    enabled = ModelProfile(
        id="openai-responses",
        name="OpenAI Responses",
        provider="openai",
        api_style="responses",
        model_id="gpt-5",
        chat_options='{"store": true}',
    )
    agent_profile = AgentProfile(name="Code", instructions="Be useful.")
    fingerprint = agent_profile_context_fingerprint(agent_profile, memory_text="memory v1")
    model_fingerprint = model_profile_context_fingerprint(enabled, chat_options={"store": True})
    enabled_lifecycle = _restore_lifecycle(
        session=SimpleNamespace(agent_profile=agent_profile, workspace=Workspace(primary_cwd="/workspace/project")),
        current=SimpleNamespace(
            loaded=SimpleNamespace(
                bindings=SimpleNamespace(backend=SimpleNamespace(service_session_storage_enabled=True))
            ),
            manifest=SimpleNamespace(
                agent_profile_fingerprint=fingerprint,
                model_profile_fingerprint=model_fingerprint,
                active_profile=enabled,
            ),
        ),
    )
    disabled_lifecycle = _restore_lifecycle(
        session=SimpleNamespace(agent_profile=agent_profile, workspace=Workspace(primary_cwd="/workspace/project")),
        current=SimpleNamespace(
            loaded=SimpleNamespace(
                bindings=SimpleNamespace(backend=SimpleNamespace(service_session_storage_enabled=False))
            ),
            manifest=SimpleNamespace(
                agent_profile_fingerprint=fingerprint,
                model_profile_fingerprint=model_fingerprint,
                active_profile=enabled,
            ),
        ),
    )
    matching_meta = SimpleNamespace(
        agent_profile_fingerprint=fingerprint,
        model_profile_fingerprint=model_fingerprint,
        model_provider="openai",
        model_api_style="responses",
        model_id="gpt-5",
        model_base_url=effective_model_base_url(enabled),
        primary_cwd="/workspace/project",
        working_dirs=[],
    )
    mismatched_meta = SimpleNamespace(
        agent_profile_fingerprint=fingerprint,
        model_profile_fingerprint=model_fingerprint,
        model_provider="openai",
        model_api_style="responses",
        model_id="gpt-4o",
        model_base_url=effective_model_base_url(enabled),
        primary_cwd="/workspace/project",
        working_dirs=[],
    )
    mismatched_agent_meta = SimpleNamespace(
        agent_profile_fingerprint=agent_profile_context_fingerprint(agent_profile, memory_text="memory v2"),
        model_profile_fingerprint=model_fingerprint,
        model_provider="openai",
        model_api_style="responses",
        model_id="gpt-5",
        model_base_url=effective_model_base_url(enabled),
        primary_cwd="/workspace/project",
        working_dirs=[],
    )
    mismatched_endpoint_meta = SimpleNamespace(
        agent_profile_fingerprint=fingerprint,
        model_profile_fingerprint=model_fingerprint,
        model_provider="openai",
        model_api_style="responses",
        model_id="gpt-5",
        model_base_url="https://different.example.com/v1",
        primary_cwd="/workspace/project",
        working_dirs=[],
    )
    mismatched_workspace_meta = SimpleNamespace(
        agent_profile_fingerprint=fingerprint,
        model_profile_fingerprint=model_fingerprint,
        model_provider="openai",
        model_api_style="responses",
        model_id="gpt-5",
        model_base_url=effective_model_base_url(enabled),
        primary_cwd="/workspace/other",
        working_dirs=[],
    )
    mismatched_model_profile_meta = SimpleNamespace(
        agent_profile_fingerprint=fingerprint,
        model_profile_fingerprint=model_profile_context_fingerprint(
            enabled, chat_options={"store": True, "verbosity": "low"}
        ),
        model_provider="openai",
        model_api_style="responses",
        model_id="gpt-5",
        model_base_url=effective_model_base_url(enabled),
        primary_cwd="/workspace/project",
        working_dirs=[],
    )

    assert agent_lifecycle._is_openai_responses_profile(enabled)
    assert enabled_lifecycle._can_restore_service_session(matching_meta)
    assert not disabled_lifecycle._can_restore_service_session(matching_meta)
    assert not enabled_lifecycle._can_restore_service_session(mismatched_meta)
    assert not enabled_lifecycle._can_restore_service_session(mismatched_agent_meta)
    assert not enabled_lifecycle._can_restore_service_session(mismatched_endpoint_meta)
    assert not enabled_lifecycle._can_restore_service_session(mismatched_workspace_meta)
    assert not enabled_lifecycle._can_restore_service_session(mismatched_model_profile_meta)


def test_restore_warning_display_references_match_protocol_prose_and_localize() -> None:
    from chrys.foundation.branding import APP_DISPLAY_NAME
    from chrys.foundation.i18n import DisplaySequence, Localizer
    from chrys.foundation.i18n.formatting import format_message

    incompatible = session_lifecycle._RESTORE_SERVICE_SESSION_INCOMPATIBLE.bind(app=APP_DISPLAY_NAME)
    assert format_message(incompatible) == (
        "This session was saved with an OpenAI Responses service session. "
        "The active agent/model profile, workspace, service endpoint, or storage mode "
        f"is not compatible, so {APP_DISPLAY_NAME} will continue from local history only."
    )

    discarded = session_lifecycle._RESTORE_SUB_AGENTS_DISCARDED.bind(
        discarded=2,
        names=DisplaySequence(("alpha", "beta")),
    )
    assert format_message(discarded) == "2 paused sub-agent(s) from a previous session were discarded: alpha, beta"

    chinese = Localizer("zh-Hans")
    assert chinese.render(discarded) == "已丢弃上一会话遗留的 2 个暂停子智能体：alpha, beta"  # noqa: RUF001
    assert "OpenAI Responses" in chinese.render(incompatible)
    assert chinese.render(incompatible) != format_message(incompatible)


def test_deepseek_responses_never_restores_or_preserves_service_sessions() -> None:
    deepseek = ModelProfile(
        id="deepseek-responses",
        name="DeepSeek Responses",
        provider="deepseek-openai",
        api_style="responses",
        model_id="deepseek-reasoner",
        chat_options='{"store": true}',
    )
    agent_profile = AgentProfile(name="Code", instructions="Be useful.")
    agent_fingerprint = agent_profile_context_fingerprint(agent_profile)
    model_fingerprint = model_profile_context_fingerprint(deepseek, chat_options={"store": False})
    workspace = Workspace(primary_cwd="/workspace/project")
    lifecycle = _restore_lifecycle(
        session=SimpleNamespace(agent_profile=agent_profile, workspace=workspace),
        current=SimpleNamespace(
            loaded=SimpleNamespace(
                bindings=SimpleNamespace(backend=SimpleNamespace(service_session_storage_enabled=False))
            ),
            manifest=SimpleNamespace(
                agent_profile_fingerprint=agent_fingerprint,
                model_profile_fingerprint=model_fingerprint,
                active_profile=deepseek,
            ),
        ),
    )
    meta = SimpleNamespace(
        agent_profile_fingerprint=agent_fingerprint,
        model_profile_fingerprint=model_fingerprint,
        model_provider="deepseek-openai",
        model_api_style="responses",
        model_id="deepseek-reasoner",
        model_base_url=effective_model_base_url(deepseek),
        primary_cwd="/workspace/project",
        working_dirs=[],
    )

    assert agent_lifecycle._is_openai_responses_profile(deepseek) is False
    assert lifecycle._can_restore_service_session(meta) is False
    assert (
        agent_lifecycle._can_reuse_responses_service_session(
            old_agent_profile_fingerprint=agent_fingerprint,
            new_agent_profile_fingerprint=agent_fingerprint,
            old_model_profile_fingerprint=model_fingerprint,
            new_model_profile_fingerprint=model_fingerprint,
            old_model_profile=deepseek,
            new_model_profile=deepseek,
            old_model_base_url=effective_model_base_url(deepseek),
            new_model_base_url=effective_model_base_url(deepseek),
            old_workspace=workspace,
            new_workspace=workspace,
            old_storage_enabled=True,
            new_storage_enabled=True,
        )
        is False
    )


def test_service_session_compat_canonicalizes_primary_in_working_dirs() -> None:
    enabled = ModelProfile(
        id="openai-responses",
        name="OpenAI Responses",
        provider="openai",
        api_style="responses",
        model_id="gpt-5",
        chat_options='{"store": true}',
    )
    agent_profile = AgentProfile(name="Code", instructions="Be useful.")
    fingerprint = agent_profile_context_fingerprint(agent_profile, memory_text="memory v1")
    model_fingerprint = model_profile_context_fingerprint(enabled, chat_options={"store": True})

    def _meta(working_dirs: list[str]) -> SimpleNamespace:
        return SimpleNamespace(
            agent_profile_fingerprint=fingerprint,
            model_profile_fingerprint=model_fingerprint,
            model_provider="openai",
            model_api_style="responses",
            model_id="gpt-5",
            model_base_url=effective_model_base_url(enabled),
            primary_cwd="/workspace/project",
            working_dirs=working_dirs,
        )

    def _engine(working_dirs: list[WorkingDir]) -> session_lifecycle.SessionLifecycle:
        return _restore_lifecycle(
            session=SimpleNamespace(
                agent_profile=agent_profile,
                workspace=Workspace(primary_cwd="/workspace/project", working_dirs=working_dirs),
            ),
            current=SimpleNamespace(
                loaded=SimpleNamespace(
                    bindings=SimpleNamespace(backend=SimpleNamespace(service_session_storage_enabled=True))
                ),
                manifest=SimpleNamespace(
                    agent_profile_fingerprint=fingerprint,
                    model_profile_fingerprint=model_fingerprint,
                    active_profile=enabled,
                ),
            ),
        )

    # Restore with additionalDirectories:[] re-inserts the primary, so the live
    # workspace lists [primary] while a no-extra session saved []. The effective
    # roots are identical, so the service session must still be reusable.
    primary_only = _engine([WorkingDir(path="/workspace/project", is_primary=True)])
    assert primary_only._can_restore_service_session(_meta([]))
    # The inverse representation (saved [primary], live []) is equally compatible.
    assert _engine([])._can_restore_service_session(_meta(["/workspace/project"]))

    # A genuine extra root on either side is a real workspace change → no reuse.
    assert not primary_only._can_restore_service_session(_meta(["/workspace/project", "/extra"]))
    with_extra = _engine([WorkingDir(path="/workspace/project", is_primary=True), WorkingDir(path="/extra")])
    assert not with_extra._can_restore_service_session(_meta([]))


def test_service_session_rebuild_reuse_requires_same_agent_endpoint_and_workspace() -> None:
    agent = AgentProfile(name="Code", instructions="Use Code.")
    model = ModelProfile(
        id="openai-responses",
        name="OpenAI Responses",
        provider="openai",
        api_style="responses",
        model_id="gpt-5",
        chat_options='{"store": true}',
    )
    workspace = Workspace(primary_cwd="/workspace/project")
    fingerprint = agent_profile_context_fingerprint(agent, memory_text="memory v1")
    model_fingerprint = model_profile_context_fingerprint(model, chat_options={"store": True})

    assert agent_lifecycle._can_reuse_responses_service_session(
        old_agent_profile_fingerprint=fingerprint,
        new_agent_profile_fingerprint=fingerprint,
        old_model_profile_fingerprint=model_fingerprint,
        new_model_profile_fingerprint=model_fingerprint,
        old_model_profile=model,
        new_model_profile=model,
        old_model_base_url="https://api.openai.com/v1",
        new_model_base_url="https://api.openai.com/v1",
        old_workspace=workspace,
        new_workspace=workspace,
        old_storage_enabled=True,
        new_storage_enabled=True,
    )
    assert not agent_lifecycle._can_reuse_responses_service_session(
        old_agent_profile_fingerprint=fingerprint,
        new_agent_profile_fingerprint=agent_profile_context_fingerprint(agent, memory_text="memory v2"),
        old_model_profile_fingerprint=model_fingerprint,
        new_model_profile_fingerprint=model_fingerprint,
        old_model_profile=model,
        new_model_profile=model,
        old_model_base_url="https://api.openai.com/v1",
        new_model_base_url="https://api.openai.com/v1",
        old_workspace=workspace,
        new_workspace=workspace,
        old_storage_enabled=True,
        new_storage_enabled=True,
    )
    assert not agent_lifecycle._can_reuse_responses_service_session(
        old_agent_profile_fingerprint=fingerprint,
        new_agent_profile_fingerprint=fingerprint,
        old_model_profile_fingerprint=model_fingerprint,
        new_model_profile_fingerprint=model_fingerprint,
        old_model_profile=model,
        new_model_profile=model,
        old_model_base_url="https://api.openai.com/v1",
        new_model_base_url="https://gateway.example.com/v1",
        old_workspace=workspace,
        new_workspace=workspace,
        old_storage_enabled=True,
        new_storage_enabled=True,
    )
    assert not agent_lifecycle._can_reuse_responses_service_session(
        old_agent_profile_fingerprint=fingerprint,
        new_agent_profile_fingerprint=fingerprint,
        old_model_profile_fingerprint=model_fingerprint,
        new_model_profile_fingerprint=model_fingerprint,
        old_model_profile=model,
        new_model_profile=model,
        old_model_base_url="https://api.openai.com/v1",
        new_model_base_url="https://api.openai.com/v1",
        old_workspace=workspace,
        new_workspace=Workspace(primary_cwd="/workspace/other"),
        old_storage_enabled=True,
        new_storage_enabled=True,
    )
    assert not agent_lifecycle._can_reuse_responses_service_session(
        old_agent_profile_fingerprint=fingerprint,
        new_agent_profile_fingerprint=fingerprint,
        old_model_profile_fingerprint=model_fingerprint,
        new_model_profile_fingerprint=model_profile_context_fingerprint(
            model, chat_options={"store": True, "verbosity": "low"}
        ),
        old_model_profile=model,
        new_model_profile=model,
        old_model_base_url="https://api.openai.com/v1",
        new_model_base_url="https://api.openai.com/v1",
        old_workspace=workspace,
        new_workspace=workspace,
        old_storage_enabled=True,
        new_storage_enabled=True,
    )


@pytest.mark.asyncio
async def test_engine_save_clears_service_session_when_response_storage_disabled(tmp_path: Path) -> None:
    store = JsonFileStateStore(tmp_path)
    engine = assemble_agent_engine(EventBus(), state_store=store)
    engine.session.session_id = "sess-1"
    engine.session.agent_profile = AgentProfile(name="Code")
    install_loaded_agent(
        engine,
        agent_profile_fingerprint=agent_profile_context_fingerprint(engine.session.agent_profile, memory_text=""),
    )
    install_loaded_agent(engine, model_profile_fingerprint="model-fp")
    install_loaded_agent(
        engine,
        active_profile=ModelProfile(
            id="openai-responses",
            name="OpenAI Responses",
            provider="openai",
            api_style="responses",
            model_id="gpt-5",
            chat_options='{"store": false}',
        ),
    )
    install_loaded_agent(
        engine,
        bindings=SimpleNamespace(
            backend=SimpleNamespace(
                history_state={"messages": [Message("user", ["hello"])]},
                service_session_id="resp_123",
                service_session_storage_enabled=False,
            ),
            state=SimpleNamespace(run_failed=False, was_interrupted=False, partial_answer=()),
        ),
    )

    await engine.writer.save_current_session()

    meta = await store.load_session_meta("sess-1")
    assert meta is not None
    assert meta.agent_profile_fingerprint == engine.current.manifest.agent_profile_fingerprint
    assert meta.service_session_id == ""


@pytest.mark.asyncio
async def test_engine_save_clears_service_session_after_failed_or_interrupted_turn(tmp_path: Path) -> None:
    store = JsonFileStateStore(tmp_path)
    engine = assemble_agent_engine(EventBus(), state_store=store)
    engine.session.session_id = "sess-1"
    engine.session.agent_profile = AgentProfile(name="Code")
    install_loaded_agent(
        engine,
        agent_profile_fingerprint=agent_profile_context_fingerprint(engine.session.agent_profile, memory_text=""),
    )
    install_loaded_agent(engine, model_profile_fingerprint="model-fp")
    install_loaded_agent(
        engine,
        active_profile=ModelProfile(
            id="openai-responses",
            name="OpenAI Responses",
            provider="openai",
            api_style="responses",
            model_id="gpt-5",
            chat_options='{"store": true}',
        ),
    )
    install_loaded_agent(
        engine,
        bindings=SimpleNamespace(
            backend=SimpleNamespace(
                history_state={"messages": [Message("user", ["hello"])]},
                service_session_id="resp_123",
                service_session_storage_enabled=True,
            ),
            state=SimpleNamespace(run_failed=True, was_interrupted=False, partial_answer=()),
        ),
    )

    await engine.writer.save_current_session()

    meta = await store.load_session_meta("sess-1")
    assert meta is not None
    assert meta.service_session_id == ""

    engine.current.loaded.bindings.state.run_failed = False
    engine.current.loaded.bindings.state.was_interrupted = True
    engine.current.loaded.bindings.backend.service_session_id = "resp_456"
    await engine.writer.save_current_session()

    meta = await store.load_session_meta("sess-1")
    assert meta is not None
    assert meta.service_session_id == ""


@pytest.mark.asyncio
async def test_post_run_clears_in_memory_service_session_after_failed_turn() -> None:
    class _History:
        def __init__(self) -> None:
            self.messages: list[Message] = []

        def merge_loop_messages(self, _loop_recorder: object, *, insert_index: int | None = None) -> None:
            pass

        def persist_approval_decisions(self, _decisions: list[dict], *, start_index: int) -> None:
            pass

        def trim_to_last_complete_tool_results(self) -> None:
            pass

        def keep_partial_answer(self, _contents: Sequence[Content]) -> None:
            pass

        def insert_interrupted_marker(self, *, reason: str = "", source: str = "") -> None:
            pass

        def persist_batch_ids(self, _batch_records: list[object]) -> dict[int, object]:
            return {}

        def persist_intermediate_texts(self, _texts: dict[int, str], _batch_anchors: dict[int, object]) -> None:
            pass

        def persist_consumed_injections(self, _injections: list[object]) -> None:
            pass

        def backfill_missing_created_at(self, *, start_index: int) -> None:
            pass

        def remove_awaiting_sub_agents_marker(self) -> None:
            pass

        def insert_turn_marker(self, started_at: object = None) -> None:
            pass

    executor = SimpleNamespace(
        state=SimpleNamespace(run_failed=True, was_interrupted=False, last_error="network failed", partial_answer=()),
        backend=SimpleNamespace(service_session_id="resp_incomplete"),
        approval=SimpleNamespace(drain_decisions=list),
        tool_events=SimpleNamespace(drain_batch_records=list),
    )
    saved_service_ids: list[str] = []

    async def _save_current_session() -> None:
        saved_service_ids.append(executor.backend.service_session_id)

    host = SimpleNamespace(
        _turn_state=TurnRuntimeState(),
        _history=_History(),
        session=SimpleNamespace(shutting_down=True, session_id="sess-1", mutation_tracker=None, hook_manager=None),
        _bus=EventBus(),
        _fsm=SimpleNamespace(try_transition=lambda _trigger: None),
        _workspace_change_tracker=WorkspaceChangeTracker(),
        settings=Settings(workspace_change_notice=False),
        _trajectory_recorder=TrajectoryRecorder(),
        _on_successful_turn=lambda: None,
        writer=SimpleNamespace(save_current_session=_save_current_session),
        current=make_current(
            loaded=make_loaded_agent(
                bindings=executor,
                loop_recorder=None,
                injection=SimpleNamespace(drain_pending=list),
                intermediate_texts={},
                consumed_injections=[],
            ),
            manifest=make_manifest(),
        ),
    )

    await _post_run(host)

    assert executor.backend.service_session_id == ""
    assert saved_service_ids == [""]


@pytest.mark.asyncio
async def test_post_run_uses_phase3_pre_output_floor_for_metadata_and_backfill() -> None:
    call = Content.from_function_call(call_id="call-current", name="zsh", arguments={"command": "true"})
    result = Content.from_function_result(call_id="call-current", result="ok")
    final = Message("assistant", ["done"])
    history_state = {
        "messages": [
            Message("assistant", ["[Compressed context: ctx_old]\nSummary: old"]),
            Message("user", ["current"]),
            Message("assistant", [call]),
            Message("tool", [result]),
            final,
        ],
        "compressed_msgs": [],
        PRE_OUTPUT_HISTORY_LEN_STATE_KEY: 1,
    }
    history = SessionHistoryManager()
    history.bind(history_state)
    executor = SimpleNamespace(
        state=SimpleNamespace(run_failed=False, was_interrupted=False, partial_answer=()),
        approval=SimpleNamespace(
            drain_decisions=lambda: [
                {
                    "request_id": "req-1",
                    "call_id": "call-current",
                    "tool_name": "zsh",
                    "status": "auto_approved",
                }
            ]
        ),
        tool_events=SimpleNamespace(drain_batch_records=list),
        backend=SimpleNamespace(history_state=history_state),
    )
    saved = False

    async def _save_current_session() -> None:
        nonlocal saved
        saved = True

    host = SimpleNamespace(
        _turn_state=TurnRuntimeState(history_start_index=99),
        _history=history,
        session=SimpleNamespace(shutting_down=True, session_id="sess-1", mutation_tracker=None, hook_manager=None),
        _bus=EventBus(),
        _fsm=SimpleNamespace(try_transition=lambda _trigger: None),
        _workspace_change_tracker=WorkspaceChangeTracker(),
        settings=Settings(workspace_change_notice=False),
        _trajectory_recorder=TrajectoryRecorder(),
        _on_successful_turn=lambda: None,
        writer=SimpleNamespace(save_current_session=_save_current_session),
        current=make_current(
            loaded=make_loaded_agent(
                bindings=executor,
                loop_recorder=None,
                injection=SimpleNamespace(drain_pending=list),
                intermediate_texts={},
                consumed_injections=[],
            ),
            manifest=make_manifest(),
        ),
    )

    await _post_run(host)

    assert saved
    assert call.additional_properties["_approval"]["request_id"] == "req-1"
    assert final.additional_properties[MESSAGE_CREATED_AT_KEY]
    assert PRE_OUTPUT_HISTORY_LEN_STATE_KEY not in history_state


@pytest.mark.asyncio
async def test_post_run_uses_refreshed_floor_after_force_compress_rewrites_phase3_state() -> None:
    history_state: dict = {"messages": [], "compressed_msgs": [], "turn_counter": 0}
    for turn in range(1, 3):
        history_state["messages"].append(Message("user", [f"old request {turn}"]))
        history_state["messages"].append(Message("assistant", [f"old answer {turn}"]))
        CompressibleHistoryProvider.insert_marker(history_state, turn)
    CompressibleHistoryProvider.compress(history_state, "turn_1", "old turn 1")
    history_state[PRE_OUTPUT_HISTORY_LEN_STATE_KEY] = len(history_state["messages"])

    current_user = Message("user", ["current"])
    call = Content.from_function_call(call_id="call-current", name="zsh", arguments={"command": "true"})
    result = Content.from_function_result(call_id="call-current", result="ok")
    call_msg = Message("assistant", [call])
    result_msg = Message("tool", [result])
    final = Message("assistant", ["done"])

    provider = CompressibleHistoryProvider(max_context_tokens=100, force_compress_pct=0.10)
    session = AgentSession(session_id="sess-force")
    context = SessionContext(session_id="sess-force", input_messages=[current_user])
    context._response = ChatResponse(
        messages=[call_msg, result_msg, final],
        usage_details={"input_token_count": 100},
    )

    await provider.after_run(agent=object(), session=session, context=context, state=history_state)

    assert len(history_state["compressed_msgs"]) == 2
    assert history_state[PRE_OUTPUT_HISTORY_LEN_STATE_KEY] == history_state["messages"].index(current_user)

    history = SessionHistoryManager()
    history.bind(history_state)
    executor = SimpleNamespace(
        state=SimpleNamespace(run_failed=False, was_interrupted=False, partial_answer=()),
        approval=SimpleNamespace(
            drain_decisions=lambda: [
                {
                    "request_id": "req-1",
                    "call_id": "call-current",
                    "tool_name": "zsh",
                    "status": "auto_approved",
                }
            ]
        ),
        tool_events=SimpleNamespace(drain_batch_records=list),
        backend=SimpleNamespace(history_state=history_state),
    )
    saved = False

    async def _save_current_session() -> None:
        nonlocal saved
        saved = True

    host = SimpleNamespace(
        _turn_state=TurnRuntimeState(history_start_index=99),
        _history=history,
        session=SimpleNamespace(shutting_down=True, session_id="sess-force", mutation_tracker=None, hook_manager=None),
        _bus=EventBus(),
        _fsm=SimpleNamespace(try_transition=lambda _trigger: None),
        _workspace_change_tracker=WorkspaceChangeTracker(),
        settings=Settings(workspace_change_notice=False),
        _trajectory_recorder=TrajectoryRecorder(),
        _on_successful_turn=lambda: None,
        writer=SimpleNamespace(save_current_session=_save_current_session),
        current=make_current(
            loaded=make_loaded_agent(
                bindings=executor,
                loop_recorder=None,
                injection=SimpleNamespace(drain_pending=list),
                intermediate_texts={},
                consumed_injections=[],
            ),
            manifest=make_manifest(),
        ),
    )

    await _post_run(host)

    assert saved
    assert call.additional_properties["_approval"]["request_id"] == "req-1"
    assert final.additional_properties[MESSAGE_CREATED_AT_KEY]
    assert PRE_OUTPUT_HISTORY_LEN_STATE_KEY not in history_state


def test_executor_retry_snapshot_restores_service_session_id() -> None:
    executor = object.__new__(TurnBindings)
    session = AgentSession(session_id="local", service_session_id="resp_original")
    session.state["chrys_history"] = {
        "messages": [Message("user", ["before retry"])],
        "compressed_msgs": [],
    }
    executor._session = session
    executor.tool_events = ToolEventMiddleware(EventBus(), origin=InvocationOrigin("turn", "", "turn-test", None))
    run_one_record = ToolBatchRecord("run-one", "read_file", 0, 1)
    executor.tool_events._tool_batch_records.append(run_one_record)
    executor.tool_events._tool_invocation_order = 1
    executor._loop_recorder = None
    approval_snapshot = ApprovalRetrySnapshot(
        ({"request_id": "run-one", "tool_name": "read_file", "status": "user_approved"},)
    )
    restored_approval_snapshots: list[ApprovalRetrySnapshot] = []
    executor.approval = SimpleNamespace(
        snapshot_retry_state=lambda: approval_snapshot,
        restore_retry_state=restored_approval_snapshots.append,
    )
    anchors_snapshot = ("pre-attempt-anchor",)
    restored_anchor_snapshots: list[tuple[str, ...]] = []
    executor._compaction_strategy = SimpleNamespace(
        snapshot_retry_state=lambda: anchors_snapshot,
        restore_retry_state=restored_anchor_snapshots.append,
    )

    rollback = HistoryRollback(
        session,
        snapshot_caller=executor._snapshot_retry_state,
        restore_caller=executor._restore_retry_state,
        history_state=lambda: session.state.get("chrys_history", {}),
    )
    snapshot = rollback.snapshot()
    executor._session.service_session_id = "resp_partial_attempt"
    session.state["chrys_history"]["messages"].append(Message("assistant", ["partial response"]))
    session.state["chrys_history"][PRE_OUTPUT_HISTORY_LEN_STATE_KEY] = 99
    executor.tool_events._tool_batch_records.append(ToolBatchRecord("failed", "write_file", 1, 2))
    executor.tool_events._tool_invocation_order = 2

    rollback.restore(snapshot)

    assert executor._session.service_session_id == "resp_original"
    assert [message.text for message in session.state["chrys_history"]["messages"]] == ["before retry"]
    assert PRE_OUTPUT_HISTORY_LEN_STATE_KEY not in session.state["chrys_history"]
    assert executor.tool_events.drain_batch_records() == [run_one_record]
    assert executor.tool_events._tool_invocation_order == 1
    assert restored_approval_snapshots == [approval_snapshot]
    # Compaction exclusion anchors ride the same snapshot: anchors created
    # by the rolled-back attempt must not survive into after_run.
    assert restored_anchor_snapshots == [anchors_snapshot]


def test_executor_retry_snapshot_restores_existing_pre_output_floor() -> None:
    executor = object.__new__(TurnBindings)
    session = AgentSession(session_id="local", service_session_id="resp_original")
    session.state["chrys_history"] = {
        "messages": [Message("user", ["before retry"])],
        "compressed_msgs": [],
        PRE_OUTPUT_HISTORY_LEN_STATE_KEY: 1,
    }
    executor._session = session
    executor.tool_events = ToolEventMiddleware(EventBus(), origin=InvocationOrigin("turn", "", "turn-test", None))
    executor._loop_recorder = None
    executor._compaction_strategy = None
    approval_snapshot = ApprovalRetrySnapshot(())
    restored_approval_snapshots: list[ApprovalRetrySnapshot] = []
    executor.approval = SimpleNamespace(
        snapshot_retry_state=lambda: approval_snapshot,
        restore_retry_state=restored_approval_snapshots.append,
    )

    rollback = HistoryRollback(
        session,
        snapshot_caller=executor._snapshot_retry_state,
        restore_caller=executor._restore_retry_state,
        history_state=lambda: session.state.get("chrys_history", {}),
    )
    snapshot = rollback.snapshot()
    session.state["chrys_history"][PRE_OUTPUT_HISTORY_LEN_STATE_KEY] = 99

    rollback.restore(snapshot)

    assert session.state["chrys_history"][PRE_OUTPUT_HISTORY_LEN_STATE_KEY] == 1
    assert restored_approval_snapshots == [approval_snapshot]


@pytest.mark.asyncio
async def test_history_provider_skips_local_replay_when_service_session_active() -> None:
    provider = CompressibleHistoryProvider(skip_local_history_for_service_context=True)
    state = {"messages": [Message("user", ["old context"])]}
    session = AgentSession(session_id="local", service_session_id="resp_123")
    context = SessionContext(
        session_id="local",
        service_session_id="resp_123",
        input_messages=[Message("user", ["new prompt"])],
    )

    await provider.before_run(agent=object(), session=session, context=context, state=state)

    assert context.get_messages() == []
    assert session.state["_chrys_history_len"] == 1


@pytest.mark.asyncio
async def test_history_provider_skips_local_replay_for_explicit_service_continuation() -> None:
    provider = CompressibleHistoryProvider(skip_local_history_for_service_context=True)
    state = {"messages": [Message("user", ["old context"])]}
    session = AgentSession(session_id="local")
    context = SessionContext(
        session_id="local",
        service_session_id=None,
        input_messages=[Message("user", ["new prompt"])],
        options={"store": False, "previous_response_id": "resp_existing"},
    )

    await provider.before_run(agent=object(), session=session, context=context, state=state)

    assert context.get_messages() == []
    assert session.state["_chrys_history_len"] == 1


@pytest.mark.asyncio
async def test_history_provider_skips_local_replay_for_default_service_continuation() -> None:
    provider = CompressibleHistoryProvider(
        skip_local_history_for_service_context=True,
        service_context_default_options={"conversation_id": "resp_existing", "store": False},
    )
    state = {"messages": [Message("user", ["old context"])]}
    session = AgentSession(session_id="local")
    context = SessionContext(
        session_id="local",
        service_session_id=None,
        input_messages=[Message("user", ["new prompt"])],
    )

    await provider.before_run(agent=object(), session=session, context=context, state=state)

    assert context.get_messages() == []
    assert session.state["_chrys_history_len"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "options",
    [
        {"conversation": {"id": "conv_existing"}},
        {"extra_body": {"previous_response_id": "resp_existing"}},
        {"continuation_token": {"response_id": "resp_bg"}},
    ],
    ids=["mapping-form", "extra-body", "continuation-token"],
)
async def test_history_provider_skips_local_replay_for_normalized_handle_spellings(
    options: dict[str, Any],
) -> None:
    # The service-side sniffer runs on the same normalized handle view as
    # the kernel: mapping-form conversations, extra_body copies, and
    # continuation tokens all reach the wire as service-side continuation
    # state, so any of them must suppress the local replay — the plain
    # string check used to miss these spellings and double the history.
    provider = CompressibleHistoryProvider(skip_local_history_for_service_context=True)
    state = {"messages": [Message("user", ["old context"])]}
    session = AgentSession(session_id="local")
    context = SessionContext(
        session_id="local",
        service_session_id=None,
        input_messages=[Message("user", ["new prompt"])],
        options={"store": False, **options},
    )

    await provider.before_run(agent=object(), session=session, context=context, state=state)

    assert context.get_messages() == []
    assert session.state["_chrys_history_len"] == 1


@pytest.mark.asyncio
async def test_history_provider_replays_local_history_when_default_handle_invalidated() -> None:
    # The Agent sanitizes the request options by REMOVING invalidated
    # handles, and an absent key cannot override a configured provider
    # default through the merge: without applying the session's invalidation
    # verdicts to the defaults too, the provider would treat the invalidated
    # default as live and skip replay while the wire choke point strips the
    # handle — the request then carries neither remote nor local history.
    provider = CompressibleHistoryProvider(
        skip_local_history_for_service_context=True,
        service_context_default_options={"previous_response_id": "resp_old", "store": False},
    )
    prior = Message("user", ["old context"])
    state = {"messages": [prior]}
    session = AgentSession(session_id="local")
    session.invalidated_service_session_ids.add("resp_old")
    context = SessionContext(
        session_id="local",
        service_session_id=None,
        input_messages=[Message("user", ["new prompt"])],
    )

    await provider.before_run(agent=object(), session=session, context=context, state=state)

    messages = context.get_messages()
    assert len(messages) == 1
    assert messages[0].contents[0].text == "old context"


def test_uses_service_side_context_applies_invalidation_to_defaults() -> None:
    context = SessionContext(
        session_id="local",
        service_session_id=None,
        input_messages=[Message("user", ["new prompt"])],
    )
    defaults = {"conversation_id": "conv_old"}
    assert _uses_service_side_context(context, defaults) is True
    assert _uses_service_side_context(context, defaults, {"conv_old"}) is False
    assert _uses_service_side_context(context, defaults, {"conv_other"}) is True, "live defaults stay live"


@pytest.mark.asyncio
async def test_history_provider_replays_local_history_when_default_store_false_has_service_session() -> None:
    provider = CompressibleHistoryProvider(
        skip_local_history_for_service_context=True,
        service_context_default_options={"store": False},
    )
    prior = Message("user", ["old context"])
    state = {"messages": [prior]}
    session = AgentSession(session_id="local", service_session_id="resp_123")
    context = SessionContext(
        session_id="local",
        service_session_id="resp_123",
        input_messages=[Message("user", ["new prompt"])],
    )

    await provider.before_run(agent=object(), session=session, context=context, state=state)

    messages = context.get_messages()
    assert len(messages) == 1
    assert messages[0].role == "user"
    assert messages[0].contents[0].text == "old context"


@pytest.mark.asyncio
async def test_history_provider_replays_local_history_before_service_session_exists() -> None:
    provider = CompressibleHistoryProvider(skip_local_history_for_service_context=True)
    prior = Message("user", ["old context"])
    state = {"messages": [prior]}
    session = AgentSession(session_id="local")
    context = SessionContext(
        session_id="local",
        service_session_id=None,
        input_messages=[Message("user", ["new prompt"])],
    )

    await provider.before_run(agent=object(), session=session, context=context, state=state)

    messages = context.get_messages()
    assert len(messages) == 1
    assert messages[0].role == "user"
    assert messages[0].contents[0].text == "old context"
