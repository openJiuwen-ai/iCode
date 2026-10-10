# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Shared fake ACP client, manager, host, engine, and session doubles for the server tests."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

from acp import schema as acp_schema

from chrys.app.acp.session_manager import AcpSessionError
from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import (
    AgentRuntimeDetails,
    ApprovalModeUpdated,
    Event,
    ModelProfileSwitched,
    ProfileSwitched,
    RollbackResult,
    UsageUpdate,
    Warning,
    WorkspaceUpdated,
)
from chrys.service.approval.policy import ApprovalMode
from chrys.service.mutations.types import (
    FileHashDiff,
    RestoreOutcome,
    RestoreResult,
    TurnMutations,
)
from chrys.service.todos.tracker import TodoTracker
from tests.support.acp_wire import acp_outgoing_json


class _FakeClient:
    """Records what the server sends, failing any payload the wire would reject."""

    def __init__(
        self,
        *,
        option_id: str = "allow",
        permission_outcome: acp_schema.AllowedOutcome | acp_schema.DeniedOutcome | None = None,
        permission_exc: Exception | None = None,
        permission_responder: Any = None,
        input_responder: Any = None,
    ) -> None:
        self.option_id = option_id
        self.permission_outcome = permission_outcome
        self.permission_exc = permission_exc
        self.permission_responder = permission_responder
        self.input_responder = input_responder
        self.permission_requests: list[acp_schema.RequestPermissionRequest] = []
        self.input_requests: list[tuple[str, dict[str, Any]]] = []
        self.updates: list[acp_schema.SessionNotification] = []
        self.ext_notifications: list[tuple[str, dict[str, Any]]] = []

    async def request_permission(
        self,
        options: list[acp_schema.PermissionOption],
        session_id: str,
        tool_call: acp_schema.ToolCallUpdate,
        **kwargs: Any,
    ) -> acp_schema.RequestPermissionResponse:
        request = acp_schema.RequestPermissionRequest(
            options=options, sessionId=session_id, toolCall=tool_call, **kwargs
        )
        acp_outgoing_json(request)
        self.permission_requests.append(request)
        if self.permission_exc is not None:
            raise self.permission_exc
        if self.permission_responder is not None:
            return await self.permission_responder(session_id, tool_call)
        if self.permission_outcome is not None:
            return acp_schema.RequestPermissionResponse(outcome=self.permission_outcome)
        return acp_schema.RequestPermissionResponse(
            outcome=acp_schema.AllowedOutcome(outcome="selected", optionId=self.option_id)
        )

    async def session_update(self, session_id: str, update: Any, **kwargs: Any) -> None:
        notification = acp_schema.SessionNotification(sessionId=session_id, update=update, **kwargs)
        acp_outgoing_json(notification)
        self.updates.append(notification)

    async def ext_notification(self, method: str, params: dict[str, Any]) -> None:
        acp_outgoing_json(params)
        self.ext_notifications.append((method, params))

    async def ext_method(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        acp_outgoing_json(params)
        self.input_requests.append((method, params))
        if self.input_responder is not None:
            return await self.input_responder(method, params)
        return {"answers": [{"values": ["ok"], "note": ""}], "cancelled": False}


@dataclass
class _FakeBlobStore:
    blobs: dict[str, bytes]

    def read_blob(self, blob_hash: str) -> bytes:
        return self.blobs[blob_hash]


@dataclass
class _FakeMutationTracker:
    store: _FakeBlobStore
    turns: list[TurnMutations] = field(default_factory=list)
    session_summary: dict[str, FileHashDiff] = field(default_factory=dict)
    turn_summaries: dict[int, dict[str, FileHashDiff]] = field(default_factory=dict)

    def get_all_turns(self) -> list[TurnMutations]:
        return self.turns

    def get_session_file_summary(self) -> dict[str, FileHashDiff]:
        return self.session_summary

    def get_turn_file_summary(self, turn_id: int) -> dict[str, FileHashDiff]:
        return self.turn_summaries.get(turn_id, {})


@dataclass
class _FakeEngine:
    runtime_details: AgentRuntimeDetails = field(default_factory=AgentRuntimeDetails)
    mutation_tracker: _FakeMutationTracker | None = None
    current_turn_number: int = 0
    usage: UsageUpdate = field(default_factory=UsageUpdate)
    rollback_turns: list[int] = field(default_factory=list)
    approval_mode: ApprovalMode = ApprovalMode.MANUAL
    todo_tracker: TodoTracker | None = None

    @property
    def usage_publisher(self):
        return SimpleNamespace(make_usage_event=self._snapshot_usage)

    def _snapshot_usage(self, *, session_id: str | None = None) -> UsageUpdate:
        return UsageUpdate(
            session_id=session_id or self.usage.session_id,
            agent_profile=self.usage.agent_profile,
            usage_source_id=self.usage.usage_source_id,
            input_tokens=self.usage.input_tokens,
            output_tokens=self.usage.output_tokens,
            total_tokens=self.usage.total_tokens,
            pct=self.usage.pct,
            max_context_tokens=self.usage.max_context_tokens,
            total_session_tokens=self.usage.total_session_tokens,
            total_session_input_tokens=self.usage.total_session_input_tokens,
            total_session_output_tokens=self.usage.total_session_output_tokens,
            cache_hit_tokens=self.usage.cache_hit_tokens,
            total_session_cache_hit_tokens=self.usage.total_session_cache_hit_tokens,
            local_tokens=self.usage.local_tokens,
            calibration_ratio=self.usage.calibration_ratio,
            system_overhead_tokens=self.usage.system_overhead_tokens,
        )

    def available_rollback_turns(self) -> list[int]:
        return list(self.rollback_turns)


@dataclass
class _FakeHost:
    event_bus: EventBus
    events: list[Event] = field(default_factory=list)
    outcome: Any = None
    vision_enabled: bool = False
    last_turn_outcome: Any = None
    engine: _FakeEngine = field(default_factory=_FakeEngine)

    async def iter_turn_events(self, _message: Any):
        for event in self.events:
            yield event
        self.last_turn_outcome = self.outcome


@dataclass
class _FakeSession:
    host: _FakeHost
    profile_name: str = "Code"
    session_id: str = "s1"
    prompt_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    closing: bool = False


class _FakeStateStore:
    async def load_session_raw(
        self,
        session_id: str,
        *,
        prefer_recovery: bool = False,
    ) -> list[dict[str, Any]]:
        _ = prefer_recovery
        return [{"role": "user", "contents": [{"type": "text", "text": f"hello {session_id}"}]}]


@dataclass
class _LoadedSession:
    session_id: str
    profile_name: str = "Code"
    host: _FakeHost = field(default_factory=lambda: _FakeHost(event_bus=EventBus()))
    prompt_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


@dataclass
class _LoadResult:
    session: _LoadedSession
    reused_existing: bool = False
    recovered_from_sidecar: bool = False


class _FakeManager:
    def __init__(self, host: _FakeHost) -> None:
        self._session = _FakeSession(host=host)
        self.injected: list[tuple[str, str]] = []
        self.rollbacks: list[dict[str, Any]] = []
        self.sub_agent_retries: list[tuple[str, str]] = []
        self.sub_agent_aborts: list[tuple[str, str]] = []
        self.approval_modes: list[tuple[str, str]] = []
        self.agent_switches: list[tuple[str, str]] = []
        self.settings_reloads: list[str] = []
        self.reload_warnings: list[Warning] = []
        self.session_warnings: list[Warning] = []
        self.workspace_updates: list[tuple[str, str]] = []
        self.model_switches: list[tuple[str, str]] = []
        self.mcp_tests: list[dict[str, object]] = []
        self.config_updates: list[tuple[str, object]] = []
        self.config_option_queries: list[str | None] = []
        self.deleted_sessions: list[tuple[str | None, str]] = []
        self.last_delete_cwd: str | None = None
        self.history_reads: list[tuple[str | None, str]] = []
        self.cancelled_sessions: list[str] = []
        self.closed_sessions: list[str] = []
        self.lifecycle_calls: list[str] = []
        self.state_store = _FakeStateStore()

    def get(self, session_id: str) -> _FakeSession:
        assert session_id == "s1"
        if self._session.closing:
            raise AcpSessionError(f"ACP session is not active: {session_id}")
        return self._session

    async def new_session(
        self,
        *,
        cwd: str | None,
        additional_directories: list[str] | None,
        mcp_servers: Any,
        warnings: list[Warning] | None = None,
    ) -> _FakeSession:
        _ = cwd, additional_directories, mcp_servers
        if warnings is not None:
            warnings.extend(self.session_warnings)
        return self._session

    def tool_kind_resolver(self, session_id: str) -> Any:
        _ = session_id
        return None

    async def inject(self, session_id: str, text: str) -> None:
        self.injected.append((session_id, text))

    async def rollback(
        self,
        session_id: str,
        *,
        target_turn: int,
        revert_changes: bool,
        selected_paths: list[str] | None,
    ) -> Any:
        self.rollbacks.append(
            {
                "session_id": session_id,
                "target_turn": target_turn,
                "revert_changes": revert_changes,
                "selected_paths": selected_paths,
            }
        )
        return RollbackResult(
            session_id=session_id,
            target_turn=target_turn,
            rolled_back_user_text="discarded prompt",
            files_reverted=1,
            restore_results=[RestoreResult(path="/workspace/a.py", outcome=RestoreOutcome.APPLIED)],
        )

    async def retry_sub_agent(self, session_id: str, invocation_id: str) -> None:
        self.sub_agent_retries.append((session_id, invocation_id))

    async def abort_sub_agent(self, session_id: str, invocation_id: str) -> None:
        self.sub_agent_aborts.append((session_id, invocation_id))

    async def set_approval_mode(self, session_id: str, mode: str) -> ApprovalModeUpdated:
        self.approval_modes.append((session_id, mode))
        return ApprovalModeUpdated(mode=mode)

    async def switch_agent(self, session_id: str, profile_name: str) -> ProfileSwitched:
        self.agent_switches.append((session_id, profile_name))
        return ProfileSwitched(from_profile="Code", to_profile=profile_name, to_display_name=profile_name)

    async def reload_settings(self, session_id: str, *, warnings: list[Warning] | None = None) -> None:
        self.settings_reloads.append(session_id)
        if warnings is not None:
            warnings.extend(self.reload_warnings)

    async def session_history(self, *, cwd: str | None, session_id: str) -> tuple[str, list[dict[str, Any]]]:
        self.history_reads.append((cwd, session_id))
        return session_id, await self.state_store.load_session_raw(session_id)

    async def set_workspace(self, session_id: str, primary_cwd: str) -> WorkspaceUpdated:
        self.workspace_updates.append((session_id, primary_cwd))
        return WorkspaceUpdated(primary_cwd=primary_cwd, working_dirs=[primary_cwd], reference_files=[])

    def list_agent_profiles(self) -> list[dict[str, object]]:
        return [{"name": "Code", "displayName": "Code"}]

    def read_agent_profile(self, name: str) -> dict[str, object]:
        return {"name": name, "description": "agent"}

    def write_agent_profile(self, data: dict[str, object]) -> dict[str, object]:
        return {"profile": {"name": data["name"]}, "path": "/agents/test.yaml"}

    def delete_agent_profile(self, name: str) -> dict[str, object]:
        return {"name": name, "deleted": True}

    def reset_agent_profile(self, name: str) -> dict[str, object]:
        return {"profile": {"name": name, "builtin": True}, "changed": True}

    def list_model_profiles(self) -> list[dict[str, object]]:
        return [{"id": "m1", "name": "Model", "modelId": "gpt"}]

    def read_model_profile(self, profile_id: str) -> dict[str, object]:
        return {"id": profile_id, "api_key": ""}

    def write_model_profile(self, data: dict[str, object]) -> dict[str, object]:
        return {"profile": {"id": data.get("id", "m2"), "name": data["name"]}, "path": "/models/m2.yaml"}

    def delete_model_profile(self, profile_id: str) -> dict[str, object]:
        return {"id": profile_id, "deleted": True}

    async def set_model_profile(self, session_id: str, profile_id: str) -> ModelProfileSwitched:
        self.model_switches.append((session_id, profile_id))
        return ModelProfileSwitched(model_profile_id=profile_id, max_context_tokens=128000)

    async def test_mcp_server(self, data: dict[str, object]) -> dict[str, object]:
        self.mcp_tests.append(data)
        return {"ok": True, "name": data.get("name", "server"), "message": "Connected."}

    def get_config_options(self, session_id: str | None = None) -> dict[str, object]:
        self.config_option_queries.append(session_id)
        return {"options": [{"key": "theme", "envKey": "CHRYS_THEME", "settingKey": "ui.theme", "value": "chrys"}]}

    def set_config_option(self, key: str, value: object) -> dict[str, object]:
        self.config_updates.append((key, value))
        return {"key": key, "envKey": "CHRYS_THEME", "settingKey": "ui.theme", "value": value}

    async def apply_config_option(
        self,
        session_id: str,
        key: str,
        value: object,
        *,
        warnings: list[Warning] | None = None,
    ) -> dict[str, object]:
        result = self.set_config_option(key, value)
        await self.reload_settings(session_id, warnings=warnings)
        return result

    async def begin_delete_session(self, *, cwd: str | None, session_id: str) -> str:
        self.lifecycle_calls.append(f"begin_delete:{session_id}")
        # The real manager resolves saved metadata from disk before it can
        # validate and mark the session closing; model that async gap so the
        # regressions exercise the real interleaving.
        await asyncio.sleep(0)
        if session_id not in ("s1", "short1"):
            raise AcpSessionError(f"Session not found: {session_id}")
        if cwd is not None and cwd != "/tmp/project":
            raise AcpSessionError(f"Saved session is not in workspace: {session_id}")
        self.last_delete_cwd = cwd
        self._session.closing = True
        return "s1"

    async def finish_delete_session(self, canonical_id: str) -> None:
        self.deleted_sessions.append((self.last_delete_cwd, canonical_id))
        self.lifecycle_calls.append(f"delete:{canonical_id}")
        # Generous async gap before the teardown reaches the prompt lock, like
        # the real manager's lock and state-store awaits: if the session is not
        # already closing when the server releases waits, a queued prompt gets
        # admitted here and this acquisition wedges behind its new turn.
        for _ in range(50):
            await asyncio.sleep(0)
        self._session.closing = True
        async with self._session.prompt_lock:
            pass

    async def cancel(self, session_id: str) -> None:
        self.cancelled_sessions.append(session_id)
        self.lifecycle_calls.append(f"cancel:{session_id}")

    async def begin_close(self, session_id: str) -> None:
        self.lifecycle_calls.append(f"begin_close:{session_id}")
        self._session.closing = True

    async def close(self, session_id: str) -> None:
        self.closed_sessions.append(session_id)
        self.lifecycle_calls.append(f"close:{session_id}")
        async with self._session.prompt_lock:
            pass


class _FakeLoadManager:
    def __init__(self, *, reused_existing: bool = False) -> None:
        self.state_store = object()
        self.closed: list[str] = []
        self.reused_existing = reused_existing
        self.session = _LoadedSession(session_id="s1")
        self.session_warnings: list[Warning] = []

    async def load_session(
        self,
        *,
        cwd: str,
        session_id: str,
        additional_directories: list[str] | None = None,
        mcp_servers: Any,
        warnings: list[Warning] | None = None,
    ) -> _LoadResult:
        _ = cwd, additional_directories, mcp_servers
        assert session_id == self.session.session_id
        if warnings is not None:
            warnings.extend(self.session_warnings)
        return _LoadResult(session=self.session, reused_existing=self.reused_existing)

    def get(self, session_id: str) -> _LoadedSession:
        assert session_id == self.session.session_id
        return self.session

    def tool_kind_resolver(self, session_id: str) -> Any:
        _ = session_id
        return None

    def list_model_profiles(self) -> list[dict[str, object]]:
        return [{"id": "m1", "name": "Model", "modelId": "gpt"}]

    async def close(self, session_id: str) -> None:
        self.closed.append(session_id)


async def _append_async(target: list[Any], value: Any) -> None:
    target.append(value)


def _plan_updates(client: _FakeClient) -> list[Any]:
    return [notification.update for notification in client.updates if notification.update.session_update == "plan"]


class _WarningRejectingClient(_FakeClient):
    async def ext_notification(self, method: str, params: dict[str, Any]) -> None:
        if method == "chrys/warning":
            raise RuntimeError("warning send boom")
        await super().ext_notification(method, params)
