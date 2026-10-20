# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for SessionPersistence."""

from __future__ import annotations

import inspect
import json
from types import SimpleNamespace
from typing import Any

import pytest

from chrys.foundation.events.bus import EventBus
from chrys.foundation.models.history_markers import HistoryMarkerKind
from chrys.foundation.models.workspace import WorkingDir, Workspace
from chrys.foundation.tool_execution_stamp import EXECUTION_STAMP_KEY, build_execution_stamp
from chrys.kernel import Content, Message
from chrys.service.profiles.agents.schema import AgentProfile
from chrys.service.profiles.models.schema import ModelProfile
from chrys.service.session.persistence import (
    SessionPersistence,
    agent_profile_context_fingerprint,
    has_real_messages,
    model_profile_context_fingerprint,
)
from chrys.service.state.store import JsonFileStateStore
from tests.support.event_capture import capture_events


@pytest.fixture
def bus() -> EventBus:
    return EventBus()


@pytest.fixture
def store(tmp_path: Any) -> JsonFileStateStore:
    return JsonFileStateStore(directory=tmp_path)


@pytest.fixture
def persistence(store: JsonFileStateStore, bus: EventBus) -> SessionPersistence:
    return SessionPersistence(store, bus)


def _make_msg(role: str = "user", *, chrys_kind: str | None = None) -> SimpleNamespace:
    """Create a mock message with to_dict() for serialization."""
    ap: dict[str, Any] = {}
    if chrys_kind:
        ap[HistoryMarkerKind.KEY] = chrys_kind
    msg = SimpleNamespace(role=role, contents=[], additional_properties=ap)
    msg.to_dict = lambda: {"role": role, "contents": [], "additional_properties": ap}
    return msg


def test_falsy_marker_key_is_not_a_real_message() -> None:
    """Marker grammar is key-presence based even for malformed legacy values."""
    marker = Message("assistant", [Content.from_text("marker")])
    marker.additional_properties[HistoryMarkerKind.KEY] = ""

    assert has_real_messages({"messages": [marker]}) is False
    assert (
        has_real_messages(
            {
                "compressed_msgs": [
                    {
                        "messages": [
                            {
                                "role": "assistant",
                                "contents": [],
                                "additional_properties": {HistoryMarkerKind.KEY: ""},
                            }
                        ]
                    }
                ]
            }
        )
        is False
    )


def test_agent_profile_id_is_required_keyword_only_for_persistence_writes() -> None:
    for method in (
        SessionPersistence.save_session,
        SessionPersistence.save_recovery_session,
        SessionPersistence.save_recovery_session_strict,
    ):
        parameter = inspect.signature(method).parameters["agent_profile_id"]
        assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
        assert parameter.default is inspect.Parameter.empty


# ──────────────── save_session ──────────────────────────────────────────


async def test_save_and_load_session(persistence: SessionPersistence) -> None:
    """Round-trip: save then load a session."""
    state: dict[str, Any] = {"messages": [_make_msg()]}
    await persistence.save_session(
        "sess-1",
        state,
        agent_profile_name="Code",
        agent_display_name="Code Agent",
        agent_profile_id="",
        workspace=None,
    )
    loaded = await persistence.load_session("sess-1")
    assert loaded is not None
    assert "messages" in loaded


async def test_save_and_load_session_round_trips_real_chrys_history(
    persistence: SessionPersistence,
    store: JsonFileStateStore,
) -> None:
    """Real Chrys messages survive the engine-facing persist/reload path."""
    stamped_result = Content.from_function_result("call_1", result="rain")
    stamped_result.additional_properties[EXECUTION_STAMP_KEY] = build_execution_stamp(
        {"city": "Seattle"},
        outcome="ok",
    )
    messages = [
        Message("user", [Content.from_text("check the weather")]),
        Message(
            "assistant",
            [
                Content.from_text("I'll look that up."),
                Content.from_function_call("call_1", "weather", arguments={"city": "Seattle"}),
            ],
        ),
        Message("tool", [stamped_result]),
        Message("assistant", [Content.from_text("It is raining.")]),
    ]
    state: dict[str, Any] = {"messages": messages, "compressed_msgs": [], "turn_counter": 1}

    await persistence.save_session(
        "sess-1",
        state,
        agent_profile_name="Code",
        agent_display_name="Code Agent",
        agent_profile_id="",
        workspace=None,
    )

    envelope = json.loads((store.session_dir("sess-1") / "session.json").read_text(encoding="utf-8"))
    raw_messages = envelope["state"]["messages"]
    assert [m["type"] for m in raw_messages] == ["message", "message", "message", "message"]
    assert raw_messages[1]["contents"][1]["type"] == "function_call"
    assert raw_messages[2]["contents"][0]["type"] == "function_result"
    assert (
        raw_messages[2]["contents"][0]["additional_properties"][EXECUTION_STAMP_KEY]
        == (stamped_result.additional_properties[EXECUTION_STAMP_KEY])
    )

    loaded = await persistence.load_session("sess-1")
    assert loaded is not None
    restored_messages = loaded["messages"]
    assert [type(message) for message in restored_messages] == [Message, Message, Message, Message]
    assert [message.to_dict() for message in restored_messages] == [message.to_dict() for message in messages]
    assert all(type(content) is Content for message in restored_messages for content in message.contents)
    assert (
        restored_messages[2].contents[0].additional_properties[EXECUTION_STAMP_KEY]
        == (stamped_result.additional_properties[EXECUTION_STAMP_KEY])
    )


async def test_save_session_strips_provider_objects_from_annotation_raw_representation(
    persistence: SessionPersistence,
    store: JsonFileStateStore,
) -> None:
    citation = Content.from_text(
        "cited answer",
        annotations=[
            {
                "type": "citation",
                "title": "Docs",
                "url": "https://example.test/docs",
                "raw_representation": SimpleNamespace(type="url_citation"),
            }
        ],
    )

    await persistence.save_session(
        "sess-annotation",
        {"messages": [Message("assistant", [citation])], "compressed_msgs": [], "turn_counter": 1},
        agent_profile_name="Code",
        agent_display_name="Code Agent",
        agent_profile_id="",
        workspace=None,
    )

    session_path = store.session_dir("sess-annotation") / "session.json"
    envelope = json.loads(session_path.read_text(encoding="utf-8"))
    annotation = envelope["state"]["messages"][0]["contents"][0]["annotations"][0]
    assert annotation == {
        "type": "citation",
        "title": "Docs",
        "url": "https://example.test/docs",
    }

    loaded = await persistence.load_session("sess-annotation")
    assert loaded is not None
    restored = loaded["messages"][0].contents[0]
    assert restored.annotations == [annotation]


async def test_save_skips_no_store(bus: EventBus) -> None:
    """save_session is a no-op when state_store is None."""
    p = SessionPersistence(None, bus)
    await p.save_session(
        "sess-1",
        {"messages": [_make_msg()]},
        agent_profile_name="Code",
        agent_display_name="Code",
        agent_profile_id="",
        workspace=None,
    )


async def test_save_skips_no_session_id(persistence: SessionPersistence) -> None:
    """save_session is a no-op when session_id is None."""
    await persistence.save_session(
        None,
        {"messages": [_make_msg()]},
        agent_profile_name="Code",
        agent_display_name="Code",
        agent_profile_id="",
        workspace=None,
    )


async def test_save_skips_empty_messages(persistence: SessionPersistence) -> None:
    """save_session is a no-op when messages list is empty."""
    await persistence.save_session(
        "sess-1",
        {"messages": []},
        agent_profile_name="Code",
        agent_display_name="Code",
        agent_profile_id="",
        workspace=None,
    )
    loaded = await persistence.load_session("sess-1")
    assert loaded is None


async def test_explicit_interactive_checkpoint_retains_an_empty_native_session(
    persistence: SessionPersistence, store: JsonFileStateStore, tmp_path
) -> None:
    assert (
        await persistence.save_session(
            "interactive-empty",
            {"messages": []},
            agent_profile_name="Code",
            agent_display_name="Code",
            agent_profile_id="code-test",
            workspace=Workspace(primary_cwd=str(tmp_path), working_dirs=[]),
            retain_empty=True,
            raise_on_error=True,
        )
        is True
    )
    loaded = await persistence.load_session("interactive-empty")
    assert loaded is not None
    assert loaded["messages"] == []
    meta = await store.load_session_meta("interactive-empty", strict=True)
    assert meta is not None
    assert meta.session_id == "interactive-empty"
    assert meta.agent_profile_id == "code-test"
    assert meta.primary_cwd == str(tmp_path)


async def test_save_skips_only_markers(persistence: SessionPersistence) -> None:
    """save_session skips if all messages are chrys markers."""
    marker = _make_msg("assistant", chrys_kind=HistoryMarkerKind.TURN)
    await persistence.save_session(
        "sess-1",
        {"messages": [marker]},
        agent_profile_name="Code",
        agent_display_name="Code",
        agent_profile_id="",
        workspace=None,
    )
    loaded = await persistence.load_session("sess-1")
    assert loaded is None


async def test_recovery_persistence_strict_propagates_while_wrapper_swallows(
    persistence: SessionPersistence,
    store: JsonFileStateStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fail_write(*_args: object, **_kwargs: object) -> None:
        raise OSError("disk unavailable")

    monkeypatch.setattr(store, "save_recovery_session", _fail_write)
    state = {"messages": [Message("user", ["keep this"])], "compressed_msgs": []}
    kwargs = {
        "agent_profile_name": "Code",
        "agent_display_name": "Code Agent",
        "agent_profile_id": "",
        "workspace": None,
    }

    await persistence.save_recovery_session("sess-1", state, **kwargs)

    with pytest.raises(OSError, match="disk unavailable"):
        await persistence.save_recovery_session_strict("sess-1", state, **kwargs)


async def test_save_publishes_session_saved(persistence: SessionPersistence, bus: EventBus) -> None:
    """save_session should publish a SessionSaved event."""
    from chrys.foundation.events.types import SessionSaved

    events = await capture_events(bus, SessionSaved)

    await persistence.save_session(
        "sess-1",
        {"messages": [_make_msg()]},
        agent_profile_name="Code",
        agent_display_name="Code",
        agent_profile_id="",
        workspace=None,
    )
    assert len(events) == 1
    assert events[0].session_id == "sess-1"


async def test_save_persists_usage_tokens(persistence: SessionPersistence) -> None:
    state: dict[str, Any] = {
        "messages": [_make_msg()],
        "last_usage": {"input_token_count": 4000, "output_token_count": 1000, "total_token_count": 5000},
    }
    await persistence.save_session(
        "sess-1",
        state,
        agent_profile_name="Code",
        agent_display_name="Code",
        agent_profile_id="",
        workspace=None,
    )
    assert state["last_usage"]["total_token_count"] == 5000


async def test_save_extracts_profile_history(persistence: SessionPersistence) -> None:
    state: dict[str, Any] = {
        "messages": [_make_msg()],
        "agent_profile_switches": [
            {"from_display": "Code", "to_display": "Explore"},
        ],
    }
    await persistence.save_session(
        "sess-1",
        state,
        agent_profile_name="Explore",
        agent_display_name="Explore",
        agent_profile_id="",
        workspace=None,
    )
    loaded = await persistence.load_session("sess-1")
    assert loaded is not None


async def test_save_with_workspace(persistence: SessionPersistence, tmp_path) -> None:
    """Workspace working_dirs should be passed to the store."""
    project = tmp_path / "project"
    workspace = Workspace(
        primary_cwd=str(tmp_path),
        working_dirs=[WorkingDir(path=str(project), is_primary=True, label="main")],
    )
    await persistence.save_session(
        "sess-1",
        {"messages": [_make_msg()]},
        agent_profile_name="Code",
        agent_display_name="Code",
        agent_profile_id="",
        workspace=workspace,
    )
    loaded = await persistence.load_session("sess-1")
    assert loaded is not None
    meta = await persistence.load_session_meta("sess-1")
    assert meta is not None
    assert meta.primary_cwd == str(tmp_path)
    assert meta.working_dirs == [str(project)]


async def test_save_omitted_service_session_id_preserves_existing_meta(
    persistence: SessionPersistence,
    store: JsonFileStateStore,
) -> None:
    """Wrapper default must preserve provider-side session ids unless explicitly cleared."""
    await store.save_session(
        "sess-1",
        {"messages": [_make_msg()]},
        model_provider="openai",
        model_api_style="responses",
        service_session_id="resp_123",
    )

    await persistence.save_session(
        "sess-1",
        {"messages": [_make_msg()]},
        agent_profile_name="Code",
        agent_display_name="Code",
        agent_profile_id="",
        workspace=None,
    )

    meta = await persistence.load_session_meta("sess-1")
    assert meta is not None
    assert meta.model_provider == "openai"
    assert meta.model_api_style == "responses"
    assert meta.service_session_id == "resp_123"


async def test_save_stamps_deepseek_responses_with_actual_api_style(persistence: SessionPersistence) -> None:
    await persistence.save_session(
        "sess-1",
        {"messages": [_make_msg()]},
        agent_profile_name="Code",
        agent_display_name="Code",
        agent_profile_id="",
        workspace=None,
        model_profile=ModelProfile(
            id="deepseek",
            name="DeepSeek",
            provider="deepseek-openai",
            api_style="responses",
            model_id="deepseek-chat",
        ),
        service_session_id="must-not-persist",
    )

    meta = await persistence.load_session_meta("sess-1")
    assert meta is not None
    assert meta.model_api_style == "responses"
    assert meta.model_profile_id == "deepseek"
    assert meta.service_session_id == ""


async def test_recovery_save_stamps_deepseek_responses_with_actual_api_style(
    persistence: SessionPersistence,
    store: JsonFileStateStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def _capture(*_args: object, **kwargs: object) -> None:
        captured.update(kwargs)

    monkeypatch.setattr(store, "save_recovery_session", _capture)

    await persistence.save_recovery_session_strict(
        "sess-1",
        {"messages": [_make_msg()]},
        agent_profile_name="Code",
        agent_display_name="Code",
        agent_profile_id="",
        workspace=None,
        model_profile=ModelProfile(
            id="deepseek",
            name="DeepSeek Responses",
            provider="deepseek-openai",
            api_style="responses",
            model_id="deepseek-chat",
        ),
    )

    assert captured["model_provider"] == "deepseek-openai"
    assert captured["model_api_style"] == "responses"
    assert captured["model_profile_id"] == "deepseek"
    assert captured["model_profile_fingerprint"] == ""


async def test_save_stamps_glm_as_chat_completions(persistence: SessionPersistence) -> None:
    await persistence.save_session(
        "sess-1",
        {"messages": [_make_msg()]},
        agent_profile_name="Code",
        agent_display_name="Code",
        agent_profile_id="",
        workspace=None,
        model_profile=ModelProfile(
            id="glm",
            name="GLM",
            provider="glm-openai",
            api_style="responses",
            model_id="glm-5.2",
        ),
    )

    meta = await persistence.load_session_meta("sess-1")
    assert meta is not None
    assert meta.model_api_style == "chat_completions"


async def test_save_persists_effective_model_base_url(
    persistence: SessionPersistence,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_BASE_URL", "https://env-openai.example.com/v1")

    await persistence.save_session(
        "sess-1",
        {"messages": [_make_msg()]},
        agent_profile_name="Code",
        agent_display_name="Code",
        agent_profile_id="",
        workspace=None,
        model_profile=ModelProfile(
            id="openai-responses",
            name="OpenAI Responses",
            provider="openai",
            api_style="responses",
            model_id="gpt-5",
        ),
    )

    meta = await persistence.load_session_meta("sess-1")
    assert meta is not None
    assert meta.model_base_url == "https://env-openai.example.com/v1"


async def test_save_persists_agent_profile_fingerprint(persistence: SessionPersistence) -> None:
    agent_profile = AgentProfile(name="Code", instructions="Use the Code profile.")
    fingerprint = agent_profile_context_fingerprint(agent_profile, memory_text="Loaded AGENTS.md")

    await persistence.save_session(
        "sess-1",
        {"messages": [_make_msg()]},
        agent_profile_name=agent_profile.name,
        agent_display_name="Code",
        agent_profile_id="",
        agent_profile_fingerprint=fingerprint,
        workspace=None,
    )

    meta = await persistence.load_session_meta("sess-1")
    assert meta is not None
    assert meta.agent_profile_fingerprint == fingerprint


async def test_save_persists_agent_profile_id(persistence: SessionPersistence) -> None:
    agent_profile = AgentProfile(id="agent-code-id", name="Code")

    await persistence.save_session(
        "sess-1",
        {"messages": [_make_msg()]},
        agent_profile_name=agent_profile.name,
        agent_display_name="Code",
        agent_profile_id=agent_profile.id,
        workspace=None,
    )

    meta = await persistence.load_session_meta("sess-1")
    assert meta is not None
    assert meta.agent_profile_id == "agent-code-id"


async def test_save_persists_model_profile_fingerprint(persistence: SessionPersistence) -> None:
    model_profile = ModelProfile(
        id="openai-responses",
        name="OpenAI Responses",
        provider="openai",
        api_style="responses",
        model_id="gpt-5",
    )
    fingerprint = model_profile_context_fingerprint(model_profile, chat_options={"store": True})

    await persistence.save_session(
        "sess-1",
        {"messages": [_make_msg()]},
        agent_profile_name="Code",
        agent_display_name="Code",
        agent_profile_id="",
        model_profile=model_profile,
        model_profile_fingerprint=fingerprint,
        workspace=None,
    )

    meta = await persistence.load_session_meta("sess-1")
    assert meta is not None
    assert meta.model_profile_fingerprint == fingerprint


async def test_save_default_model_profile_fingerprint_uses_effective_chat_options(
    persistence: SessionPersistence,
) -> None:
    model_profile = ModelProfile(
        id="openai-responses",
        name="OpenAI Responses",
        provider="openai",
        api_style="responses",
        model_id="gpt-5",
    )

    await persistence.save_session(
        "sess-1",
        {"messages": [_make_msg()]},
        agent_profile_name="Code",
        agent_display_name="Code",
        agent_profile_id="",
        model_profile=model_profile,
        workspace=None,
    )

    meta = await persistence.load_session_meta("sess-1")
    assert meta is not None
    assert meta.model_profile_fingerprint == model_profile_context_fingerprint(
        model_profile,
        chat_options={"store": False, "max_tokens": 32000},
    )


async def test_save_clears_service_session_when_effective_store_is_false(
    persistence: SessionPersistence,
) -> None:
    await persistence.save_session(
        "sess-1",
        {"messages": [_make_msg()]},
        agent_profile_name="Code",
        agent_display_name="Code",
        agent_profile_id="",
        workspace=None,
        model_profile=ModelProfile(
            id="openai-responses",
            name="OpenAI Responses",
            provider="openai",
            api_style="responses",
            model_id="gpt-5",
        ),
        service_session_id="resp_123",
    )

    meta = await persistence.load_session_meta("sess-1")
    assert meta is not None
    assert meta.service_session_id == ""


async def test_save_preserves_service_session_when_effective_store_is_true(
    persistence: SessionPersistence,
) -> None:
    await persistence.save_session(
        "sess-1",
        {"messages": [_make_msg()]},
        agent_profile_name="Code",
        agent_display_name="Code",
        agent_profile_id="",
        workspace=None,
        model_profile=ModelProfile(
            id="openai-responses",
            name="OpenAI Responses",
            provider="openai",
            api_style="responses",
            model_id="gpt-5",
            chat_options='{"store": true}',
        ),
        service_session_id="resp_123",
    )

    meta = await persistence.load_session_meta("sess-1")
    assert meta is not None
    assert meta.service_session_id == "resp_123"


async def test_model_profile_context_fingerprint_includes_chat_options() -> None:
    model_profile = ModelProfile(
        id="openai-responses",
        name="OpenAI Responses",
        provider="openai",
        api_style="responses",
        model_id="gpt-5",
    )

    assert model_profile_context_fingerprint(
        model_profile,
        chat_options={"store": True, "instructions": "one"},
    ) != model_profile_context_fingerprint(
        model_profile,
        chat_options={"store": True, "instructions": "two"},
    )


async def test_model_profile_context_fingerprint_ignores_streaming() -> None:
    """Toggling streaming keeps a service session resumable: it is not part of the fingerprint."""
    streaming = ModelProfile(id="p", name="P", provider="openai", api_style="responses", model_id="gpt-5")
    non_streaming = ModelProfile(
        id="p", name="P", provider="openai", api_style="responses", model_id="gpt-5", stream=False
    )

    assert streaming.stream is True
    assert model_profile_context_fingerprint(streaming) == model_profile_context_fingerprint(non_streaming)


async def test_model_profile_context_fingerprint_includes_openai_account_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model_profile = ModelProfile(
        id="openai-responses",
        name="OpenAI Responses",
        provider="openai",
        api_style="responses",
        model_id="gpt-5",
    )
    monkeypatch.setenv("OPENAI_ORG_ID", "org-one")
    first = model_profile_context_fingerprint(model_profile, chat_options={"store": True})

    monkeypatch.setenv("OPENAI_ORG_ID", "org-two")
    second = model_profile_context_fingerprint(model_profile, chat_options={"store": True})

    assert first != second

    monkeypatch.setenv("OPENAI_ORG_ID", "org-one")
    monkeypatch.setenv("OPENAI_PROJECT_ID", "project-two")
    third = model_profile_context_fingerprint(model_profile, chat_options={"store": True})

    assert third != first


async def test_model_profile_context_fingerprint_tracks_glm_env_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A blank glm-openai profile key resolves through ZAI_API_KEY, so account
    changes must still rotate the fingerprint."""
    model_profile = ModelProfile(
        id="glm",
        name="GLM",
        provider="glm-openai",
        model_id="glm-5.2",
        api_key="",
    )
    monkeypatch.setenv("ZAI_API_KEY", "sk-glm-one")
    first = model_profile_context_fingerprint(model_profile, chat_options={})

    monkeypatch.setenv("ZAI_API_KEY", "sk-glm-two")
    second = model_profile_context_fingerprint(model_profile, chat_options={})

    assert first != second


async def test_agent_profile_context_fingerprint_includes_loaded_memory_text() -> None:
    agent_profile = AgentProfile(name="Code", instructions="Use the Code profile.")

    assert agent_profile_context_fingerprint(
        agent_profile, memory_text="memory v1"
    ) != agent_profile_context_fingerprint(
        agent_profile,
        memory_text="memory v2",
    )


async def test_agent_profile_context_fingerprint_includes_user_interaction_capability() -> None:
    agent_profile = AgentProfile(name="Code", instructions="Use the Code profile.")

    assert agent_profile_context_fingerprint(
        agent_profile,
        allow_user_interaction=True,
    ) != agent_profile_context_fingerprint(
        agent_profile,
        allow_user_interaction=False,
    )


async def test_save_clears_service_session_provenance_for_non_openai_profile(
    persistence: SessionPersistence,
) -> None:
    await persistence.save_session(
        "sess-1",
        {"messages": [_make_msg()]},
        agent_profile_name="Code",
        agent_display_name="Code",
        agent_profile_id="",
        workspace=None,
        model_profile=ModelProfile(
            id="openai-responses",
            name="OpenAI Responses",
            provider="openai",
            api_style="responses",
            model_id="gpt-5",
        ),
        service_session_id="resp_123",
    )
    await persistence.save_session(
        "sess-1",
        {"messages": [_make_msg()]},
        agent_profile_name="Code",
        agent_display_name="Code",
        agent_profile_id="",
        workspace=None,
        model_profile=ModelProfile(
            id="anthropic",
            name="Anthropic",
            provider="anthropic",
            model_id="claude-sonnet-4-6",
        ),
    )

    meta = await persistence.load_session_meta("sess-1")
    assert meta is not None
    assert meta.model_provider == "anthropic"
    assert meta.model_api_style == ""
    assert meta.model_profile_fingerprint == ""
    assert meta.service_session_id == ""


# ──────────────── load / list / delete ──────────────────────────────────


async def test_load_nonexistent_returns_none(persistence: SessionPersistence) -> None:
    assert await persistence.load_session("nonexistent") is None


async def test_load_no_store(bus: EventBus) -> None:
    p = SessionPersistence(None, bus)
    assert await p.load_session("x") is None


async def test_list_sessions_empty(persistence: SessionPersistence) -> None:
    sessions = await persistence.list_sessions()
    assert sessions == []


async def test_list_sessions_no_store(bus: EventBus) -> None:
    p = SessionPersistence(None, bus)
    assert await p.list_sessions() == []


async def test_delete_session(persistence: SessionPersistence) -> None:
    await persistence.save_session(
        "sess-1",
        {"messages": [_make_msg()]},
        agent_profile_name="Code",
        agent_display_name="Code",
        agent_profile_id="",
        workspace=None,
    )
    await persistence.delete_session("sess-1")
    assert await persistence.load_session("sess-1") is None


async def test_delete_no_store(bus: EventBus) -> None:
    p = SessionPersistence(None, bus)
    await p.delete_session("x")  # should not raise


async def test_a_checkpoint_names_only_the_session_and_save_it_came_from(
    persistence: SessionPersistence, store: JsonFileStateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Provenance the file cannot confirm is never handed out."""
    state: dict[str, Any] = {"messages": [_make_msg()]}
    await persistence.save_session(
        "sess-1",
        state,
        agent_profile_name="Code",
        agent_display_name="Code Agent",
        agent_profile_id="",
        workspace=None,
    )
    first = persistence.checkpoint_for("sess-1")
    assert first is not None and first.session_checkpoint_id
    # Another session never inherits it, however recently it was minted.
    assert persistence.checkpoint_for("sess-2") is None

    real_save = store.save_session

    async def failing_save(*args: Any, **kwargs: Any) -> Any:
        raise OSError("disk full")

    monkeypatch.setattr(store, "save_session", failing_save)
    assert (
        await persistence.save_session(
            "sess-1",
            state,
            agent_profile_name="Code",
            agent_display_name="Code Agent",
            agent_profile_id="",
            workspace=None,
        )
        is False
    )
    # The turn that failed to save has no revision to point at — the previous
    # one describes different content and must not stand in for it.
    assert persistence.checkpoint_for("sess-1") is None

    monkeypatch.setattr(store, "save_session", real_save)
    await persistence.save_session(
        "sess-1",
        state,
        agent_profile_name="Code",
        agent_display_name="Code Agent",
        agent_profile_id="",
        workspace=None,
    )
    latest = persistence.checkpoint_for("sess-1")
    assert latest is not None and latest.session_checkpoint_id != first.session_checkpoint_id
