# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""ACP server adapter for Chrys."""

from __future__ import annotations

import asyncio
import contextlib
from datetime import datetime
from math import isfinite
from typing import TYPE_CHECKING, Any

from acp import PROTOCOL_VERSION, RequestError
from acp import schema as acp_schema
from acp.interfaces import Client

from chrys import __version__
from chrys.app.acp.bridge import (
    AcpEventBridge,
    acp_tool_kind,
    plan_update_for_todos,
    session_title_info_update,
    tool_call_title,
)
from chrys.app.acp.content import AcpContentError, ContentBlock, convert_prompt_blocks
from chrys.app.acp.history import replay_session_history
from chrys.app.acp.session_manager import AcpSessionError, AcpSessionManager, ManagedSession, jsonable_dataclass
from chrys.foundation.branding import APP_DISPLAY_NAME
from chrys.foundation.events.types import (
    AgentLoadFailed,
    AgentLoadFinished,
    AgentLoadProgress,
    AgentLoadStarted,
    AgentRuntimeUpdated,
    ApprovalCancelled,
    ApprovalModeUpdated,
    ApprovalRequest,
    ApprovalResponse,
    ApprovalReviewed,
    AskUserResponse,
    AskUserTimedOut,
    CompactionFinished,
    CompactionStarted,
    ContextCompressed,
    Error,
    Event,
    InvocationAborted,
    InvocationCascadeAborted,
    InvocationCompactionCommitted,
    InvocationCompactionFinished,
    InvocationCompactionStarted,
    InvocationContextPressure,
    InvocationEvent,
    InvocationPaused,
    InvocationProgress,
    InvocationResumed,
    InvocationRetryAttempt,
    InvocationStarted,
    InvocationToolCallResult,
    InvocationToolCallStart,
    ProfileSwitched,
    QuestionToUser,
    RollbackResult,
    SessionReady,
    SessionRestored,
    SessionSaved,
    SessionTitleUpdated,
    ToolCompacted,
    UsageUpdate,
    UserInjectResult,
    Warning,
    WorkspaceUpdated,
)
from chrys.foundation.models.ask_user import AskUserQuestion, validate_request_input_response
from chrys.foundation.text.encoding import EncodingDetector, decode_bytes
from chrys.orchestration.session_host import Cancelled, EndTurn, Errored

if TYPE_CHECKING:
    from chrys.service.mutations.types import FileHashDiff

_ALLOW_OPTION_ID = "allow"
_REJECT_OPTION_ID = "reject"
_PERMISSION_REQUEST_TIMEOUT_SECONDS = 600.0

# Approval modes surfaced as standard ACP session modes (id, name, description).
_APPROVAL_MODES: tuple[tuple[str, str, str], ...] = (
    ("manual", "Manual", "Every tool call that requires approval shows a prompt."),
    ("auto", "Auto", "An LLM judge evaluates calls; only flagged ones prompt."),
    ("bypass", "Bypass", "All tool calls are auto-approved."),
)
_APPROVAL_MODE_IDS = frozenset(mode_id for mode_id, _, _ in _APPROVAL_MODES)


def _request_input_question_payload(question: AskUserQuestion) -> dict[str, object]:
    return {
        "question": question.question,
        "header": question.header,
        "options": [{"label": option.label, "description": option.description} for option in question.options],
        "multiSelect": question.multi_select,
    }


class ChrysAcpServer:
    """ACP request handlers backed by ``ChrysSessionHost``."""

    def __init__(
        self,
        manager: AcpSessionManager,
        *,
        initial_vision: bool,
        permission_timeout_seconds: float = _PERMISSION_REQUEST_TIMEOUT_SECONDS,
    ) -> None:
        if not isfinite(permission_timeout_seconds) or permission_timeout_seconds <= 0:
            raise ValueError("permission_timeout_seconds must be finite and greater than zero.")
        self._manager = manager
        self._initial_vision = initial_vision
        self._permission_timeout_seconds = permission_timeout_seconds
        self._client: Client | None = None
        self._pending_input_cancels: dict[tuple[str, str], asyncio.Future[None]] = {}
        self._pending_permission_cancels: dict[tuple[str, str], asyncio.Future[None]] = {}
        self._pending_permission_tasks: dict[tuple[str, str], asyncio.Task[acp_schema.RequestPermissionResponse]] = {}
        self._input_cancel_tombstones: set[tuple[str, str]] = set()
        self._permission_cancel_tombstones: set[tuple[str, str]] = set()

    def on_connect(self, conn: Client) -> None:
        """Capture the ACP client connection used for server-to-client requests."""
        self._client = conn

    async def initialize(
        self,
        protocol_version: int,
        client_capabilities: acp_schema.ClientCapabilities | None = None,
        client_info: acp_schema.Implementation | None = None,
        **kwargs: Any,
    ) -> acp_schema.InitializeResponse:
        """Return Chrys ACP capabilities."""
        _ = client_capabilities, client_info, kwargs
        return acp_schema.InitializeResponse(
            protocolVersion=min(protocol_version, PROTOCOL_VERSION),
            agentInfo=acp_schema.Implementation(name=APP_DISPLAY_NAME, version=__version__),
            agentCapabilities=acp_schema.AgentCapabilities(
                # Keep this explicit until the ACP SDK replaces its invalid
                # bare-dict auth default; the TUI normalizes omitted remote
                # values in panels/acp.py::_handshake_payload.
                auth=None,
                loadSession=True,
                promptCapabilities=acp_schema.PromptCapabilities(
                    embeddedContext=True,
                    image=self._initial_vision,
                    audio=False,
                ),
                mcpCapabilities=acp_schema.McpCapabilities(http=True, sse=False),
                sessionCapabilities=acp_schema.SessionCapabilities(
                    close=acp_schema.SessionCloseCapabilities(),
                    list=acp_schema.SessionListCapabilities(),
                    additionalDirectories=acp_schema.SessionAdditionalDirectoriesCapabilities(),
                ),
            ),
        )

    async def _watch_session_titles(self, session: ManagedSession) -> None:
        """Forward ``SessionTitleUpdated`` as ``session_info_update`` for a session's lifetime.

        Title generation is a post-turn side call, so its event lands after
        the prompt's event stream has already closed — only a long-lived
        subscription on the session's bus can deliver it live.  The
        subscription dies with the host bus when the session closes.
        """
        session_id = session.session_id

        async def _on_title_updated(event: SessionTitleUpdated) -> None:
            if event.session_id != session_id or self._client is None:
                return
            with contextlib.suppress(Exception):
                await self._client.session_update(
                    session_id=session_id,
                    update=session_title_info_update(event),
                )

        await session.host.event_bus.subscribe(SessionTitleUpdated, _on_title_updated)

    async def _watch_nested_wait_cancellations(self, session: ManagedSession) -> None:
        """Subscribe out of band so serialized prompt delivery cannot hide cancellation."""
        session_id = session.session_id

        async def _on_approval_cancelled(event: ApprovalCancelled) -> None:
            if event.session_id != session_id:
                return
            self._deliver_or_record_cancellation(
                self._pending_permission_cancels,
                self._permission_cancel_tombstones,
                (session_id, event.request_id),
            )

        async def _on_ask_user_cleared(event: AskUserTimedOut) -> None:
            if event.session_id != session_id:
                return
            self._deliver_or_record_cancellation(
                self._pending_input_cancels,
                self._input_cancel_tombstones,
                (session_id, event.request_id),
            )

        await session.host.event_bus.subscribe(ApprovalCancelled, _on_approval_cancelled)
        await session.host.event_bus.subscribe(AskUserTimedOut, _on_ask_user_cleared)

    async def new_session(
        self,
        cwd: str | None = None,
        additional_directories: list[str] | None = None,
        mcp_servers: list[acp_schema.HttpMcpServer | acp_schema.SseMcpServer | acp_schema.McpServerStdio] | None = None,
        **kwargs: Any,
    ) -> acp_schema.NewSessionResponse:
        """Create a new Chrys session."""
        _ = kwargs
        # Session creation runs outside any prompt turn, so the settings
        # warnings its load produced — a denied project key, say — have no
        # forwarder; collect and send them once the session exists.
        warnings: list[Warning] = []
        try:
            session = await self._manager.new_session(
                cwd=cwd,
                additional_directories=additional_directories,
                mcp_servers=mcp_servers,
                warnings=warnings,
            )
        except Exception as exc:
            raise _request_error(exc) from exc
        # The session is in the active map from here on, but its id has not
        # reached the caller yet: any failure below — the sends losing the
        # client, or this task being cancelled — would strand a session
        # nobody can ever address, so the just-created session is closed
        # before the failure propagates.
        try:
            await self._watch_session_titles(session)
            await self._watch_nested_wait_cancellations(session)
            for warning in warnings:
                await self._send_warning(session.session_id, warning)
            await self._send_runtime_update(session.session_id)
            await self._send_plan_update(session.session_id)
        except BaseException:
            with contextlib.suppress(Exception):
                await self._manager.close(session.session_id)
            raise
        return acp_schema.NewSessionResponse(
            sessionId=session.session_id,
            modes=self._session_mode_state(session.session_id),
            models=self._session_model_state(session.session_id),
        )

    async def load_session(
        self,
        session_id: str,
        cwd: str | None = None,
        additional_directories: list[str] | None = None,
        mcp_servers: list[acp_schema.HttpMcpServer | acp_schema.SseMcpServer | acp_schema.McpServerStdio] | None = None,
        **kwargs: Any,
    ) -> acp_schema.LoadSessionResponse:
        """Load a saved Chrys session."""
        _ = kwargs
        # Same gap as ``new_session``: the load happens outside any prompt
        # turn, so its settings warnings are only delivered if collected here.
        warnings: list[Warning] = []
        try:
            result = await self._manager.load_session(
                cwd=cwd,
                session_id=session_id,
                additional_directories=additional_directories,
                mcp_servers=mcp_servers,
                warnings=warnings,
            )
            session = result.session
            # One cleanup scope for everything after the load: a session this
            # request created must not stay in the active map when any of the
            # follow-up sends — warnings included — loses the client, nor when
            # the request task is cancelled mid-establishment.
            try:
                if not result.reused_existing:
                    # A reused active session already has a title watcher from
                    # the request that created it.
                    await self._watch_session_titles(session)
                    await self._watch_nested_wait_cancellations(session)
                if self._client is not None:
                    for warning in warnings:
                        await self._send_warning(session.session_id, warning)
                    await self._send_session_restored(session.session_id)
                    await replay_session_history(
                        self._client,
                        self._manager.state_store,
                        session.session_id,
                        prefer_recovery=result.recovered_from_sidecar,
                        tool_kind_resolver=self._manager.tool_kind_resolver(session.session_id),
                    )
                    await self._send_runtime_update(session.session_id)
                    await self._send_plan_update(session.session_id)
            except BaseException:
                if not result.reused_existing:
                    with contextlib.suppress(Exception):
                        await self._manager.close(session.session_id)
                raise
        except Exception as exc:
            raise _request_error(exc) from exc
        return acp_schema.LoadSessionResponse(
            modes=self._session_mode_state(session.session_id),
            models=self._session_model_state(session.session_id),
        )

    async def list_sessions(
        self,
        additional_directories: list[str] | None = None,
        cursor: str | None = None,
        cwd: str | None = None,
        **kwargs: Any,
    ) -> acp_schema.ListSessionsResponse:
        """List saved sessions for this workspace cwd."""
        _ = kwargs
        try:
            sessions, next_cursor = await self._manager.list_sessions(
                cwd=cwd, cursor=cursor, additional_directories=additional_directories
            )
        except Exception as exc:
            raise _request_error(exc) from exc
        return acp_schema.ListSessionsResponse(sessions=sessions, nextCursor=next_cursor)

    async def prompt(
        self,
        prompt: list[ContentBlock],
        session_id: str,
        message_id: str | None = None,
        **kwargs: Any,
    ) -> acp_schema.PromptResponse:
        """Run one ACP prompt turn."""
        # AIxCoding telemetry: desktop channel from the prompt _meta envelope,
        # plus telemetry ids returned to the client on the response.
        from chrys.aixcoding.telemetry.acp_meta import read_ide_channel_meta, telemetry_response_meta

        read_ide_channel_meta(kwargs)
        if self._client is None:
            raise RequestError.internal_error({"details": "ACP client connection is not ready."})
        bridge = AcpEventBridge()
        pending_approvals: dict[str, ApprovalRequest] = {}
        outcome: EndTurn | Cancelled | Errored | None = None
        usage_payload: dict[str, int] | None = None
        execution_started = False
        try:
            session = self._manager.get(session_id)
            async with session.prompt_lock:
                try:
                    # A close/delete may have started while this prompt waited for
                    # the per-session lock. Re-resolve before touching the host so
                    # a queued prompt cannot run against a closing session.
                    if self._manager.get(session_id) is not session:
                        raise AcpSessionError(f"ACP session is not active: {session_id}")
                    converted = convert_prompt_blocks(prompt, vision_enabled=session.host.vision_enabled)
                    execution_started = True
                    async for event in session.host.iter_turn_events(converted.message):
                        await self._handle_event(session_id, event, bridge, pending_approvals)
                    outcome = session.host.last_turn_outcome
                finally:
                    # This snapshot stays under prompt_lock: a queued turn must
                    # not mutate cumulative session usage before this response
                    # (or error) captures the completed turn's total.
                    if execution_started:
                        usage_payload = _prompt_usage_payload(
                            session.host.engine.usage_publisher.make_usage_event(session_id=session_id)
                        )
                    # A cancellation tombstone only bridges to a request event
                    # still queued in THIS turn's per-turn stream. Judge-aborted
                    # approvals and requests dropped by cancellation never send
                    # that event, so whatever survives the stream is dead —
                    # sweep it or a long-lived session accumulates tombstones.
                    self._drop_session_tombstones(session_id)
        except AcpContentError as exc:
            raise RequestError.invalid_params({"details": str(exc)}) from exc
        except Exception as exc:
            raise _request_error(exc, usage=usage_payload) from exc

        if isinstance(outcome, EndTurn):
            return acp_schema.PromptResponse(
                stopReason="end_turn",
                userMessageId=message_id,
                usage=acp_schema.Usage.model_validate(usage_payload),
                _meta=telemetry_response_meta(session_id),
            )
        if isinstance(outcome, Cancelled):
            return acp_schema.PromptResponse(
                stopReason="cancelled",
                userMessageId=message_id,
                usage=acp_schema.Usage.model_validate(usage_payload),
                _meta=telemetry_response_meta(session_id),
            )
        if isinstance(outcome, Errored):
            error = outcome.error
            raise RequestError.internal_error(
                {
                    "code": error.code,
                    "message": error.message,
                    "usage": usage_payload,
                }
            )
        raise RequestError.internal_error(
            {
                "details": f"{APP_DISPLAY_NAME} turn ended without an outcome.",
                "usage": usage_payload,
            }
        )

    async def cancel(self, session_id: str, **kwargs: Any) -> None:
        """Cancel the active prompt for a session."""
        _ = kwargs
        try:
            await self._manager.cancel(session_id)
        except AcpSessionError:
            pass
        finally:
            # Interrupt the engine first. A rejected approval is not turn
            # terminating, so releasing the bridge wait before interruption
            # could feed one extra doomed model call.
            self._cancel_pending_waits(session_id)

    async def close_session(self, session_id: str, **kwargs: Any) -> acp_schema.CloseSessionResponse:
        """Close an active ACP session."""
        _ = kwargs
        # Stop prompt admission before releasing waits: once the pending
        # ask-user/permission callbacks resolve, a prompt queued on the
        # session's prompt lock would otherwise pass its re-check and start a
        # fresh turn that manager.close then wedges behind.
        await self._manager.begin_close(session_id)
        # The wait release is inside the try: cancelling this RPC mid-release
        # must still drop the session's cancellation state in the finally.
        try:
            await self._interrupt_and_cancel_pending_waits(session_id)
            await self._manager.close(session_id)
        except Exception as exc:
            raise _request_error(exc) from exc
        finally:
            self._clear_cancellation_state(session_id)
        return acp_schema.CloseSessionResponse()

    async def set_session_mode(self, mode_id: str, session_id: str, **kwargs: Any) -> acp_schema.SetSessionModeResponse:
        """Set the session approval mode (standard ACP session mode)."""
        _ = kwargs
        if mode_id not in _APPROVAL_MODE_IDS:
            raise RequestError.invalid_params({"details": "mode_id must be manual, auto, or bypass."})
        try:
            result = await self._manager.set_approval_mode(session_id, mode_id)
        except Exception as exc:
            raise _request_error(exc) from exc
        if self._client is not None:
            with contextlib.suppress(Exception):
                await self._client.session_update(
                    session_id=session_id,
                    update=acp_schema.CurrentModeUpdate(
                        sessionUpdate="current_mode_update", currentModeId=result.mode or mode_id
                    ),
                )
        return acp_schema.SetSessionModeResponse()

    async def set_session_model(
        self, model_id: str, session_id: str, **kwargs: Any
    ) -> acp_schema.SetSessionModelResponse:
        """Switch the session model (standard ACP) — per-session, no global .env write."""
        _ = kwargs
        try:
            await self._manager.set_model_profile(session_id, model_id)
        except Exception as exc:
            raise _request_error(exc) from exc
        await self._send_runtime_update(session_id)
        return acp_schema.SetSessionModelResponse()

    async def delete_session(
        self,
        session_id: str,
        cwd: str | None = None,
        additional_directories: list[str] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Delete a saved Chrys session scoped to the requested workspace."""
        _ = kwargs
        self._reject_additional_directories(additional_directories)
        try:
            canonical_id = await self._manager.begin_delete_session(cwd=cwd, session_id=session_id)
        except Exception as exc:
            raise _request_error(exc) from exc
        # Deleting an active session tears it down like close: once the
        # request validates, release pending client waits under the canonical
        # id so the manager's prompt-lock-protected teardown cannot wedge
        # behind an unresolved ask-user/permission callback. The raw id may be
        # a short id that would miss the engine and the wait maps, and a
        # request rejected for workspace scoping must not interrupt the turn.
        # begin_delete_session also marks the session closing, so a prompt
        # queued behind the released waits is rejected instead of being
        # admitted into a fresh turn the teardown would wedge behind.
        try:
            await self._interrupt_and_cancel_pending_waits(canonical_id)
            await self._manager.finish_delete_session(canonical_id)
        except Exception as exc:
            raise _request_error(exc) from exc
        finally:
            self._clear_cancellation_state(canonical_id)
        return {}

    async def ext_method(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """Handle Chrys ACP extension requests from clients."""
        if method == "session/delete":
            session_id = _string_param(params, "sessionId") or _string_param(params, "session_id")
            cwd = _optional_string_param(params, "cwd")
            additional_directories = _string_list_param(params, "additionalDirectories")
            await self.delete_session(
                session_id=session_id,
                cwd=cwd,
                additional_directories=additional_directories,
            )
            return {}
        if method == "chrys/session_runtime":
            session_id = _string_param(params, "sessionId") or _string_param(params, "session_id")
            return self._runtime_payload(session_id)
        if method == "session/inject":
            session_id = _string_param(params, "sessionId") or _string_param(params, "session_id")
            text = _string_param(params, "text")
            if not text:
                raise RequestError.invalid_params({"details": "text is required."})
            try:
                await self._manager.inject(session_id, text)
            except Exception as exc:
                raise _request_error(exc) from exc
            return {}
        if method == "session/mutations":
            session_id = _string_param(params, "sessionId") or _string_param(params, "session_id")
            await self._refresh_mutation_attribution(session_id)
            return self._mutations_payload(session_id)
        if method == "session/diff":
            session_id = _string_param(params, "sessionId") or _string_param(params, "session_id")
            path = _optional_string_param(params, "path")
            turn = _optional_int_param(params, "turn")
            await self._refresh_mutation_attribution(session_id)
            return self._diff_payload(session_id, path=path, turn=turn)
        if method == "session/rollback":
            session_id = _string_param(params, "sessionId") or _string_param(params, "session_id")
            target_turn = _int_param(params, "targetTurn")
            revert_changes = _bool_param(params, "revertChanges", default=False)
            selected_paths = _string_list_param(params, "selectedPaths")
            try:
                result = await self._manager.rollback(
                    session_id,
                    target_turn=target_turn,
                    revert_changes=revert_changes,
                    selected_paths=selected_paths,
                )
            except Exception as exc:
                raise _request_error(exc) from exc
            payload = _rollback_result_payload(result)
            await self._client_or_error().ext_notification("chrys/rollback_result", payload)
            if target_turn > 0:
                await replay_session_history(
                    self._client_or_error(),
                    self._manager.state_store,
                    session_id,
                    tool_kind_resolver=self._manager.tool_kind_resolver(session_id),
                )
                await self._send_runtime_update(session_id)
            # Both branches: >0 re-seeds the rolled-back list, ==0 (reset
            # to welcome) clears the client plan.
            await self._send_plan_update(session_id)
            return payload
        if method == "sub_agent/retry":
            session_id = _string_param(params, "sessionId") or _string_param(params, "session_id")
            invocation_id = _string_param(params, "invocationId") or _string_param(params, "invocation_id")
            if not invocation_id:
                raise RequestError.invalid_params({"details": "invocationId is required."})
            try:
                await self._manager.retry_sub_agent(session_id, invocation_id)
            except Exception as exc:
                raise _request_error(exc) from exc
            return {}
        if method == "sub_agent/abort":
            session_id = _string_param(params, "sessionId") or _string_param(params, "session_id")
            invocation_id = _string_param(params, "invocationId") or _string_param(params, "invocation_id")
            if not invocation_id:
                raise RequestError.invalid_params({"details": "invocationId is required."})
            try:
                await self._manager.abort_sub_agent(session_id, invocation_id)
            except Exception as exc:
                raise _request_error(exc) from exc
            return {}
        if method == "session/skip_sleep":
            session_id = _string_param(params, "sessionId") or _string_param(params, "session_id")
            call_id = _string_param(params, "callId") or _string_param(params, "call_id")
            if not call_id:
                raise RequestError.invalid_params({"details": "callId is required."})
            try:
                await self._manager.skip_sleep(session_id, call_id)
            except Exception as exc:
                raise _request_error(exc) from exc
            return {}
        if method == "session/switch_agent":
            session_id = _string_param(params, "sessionId") or _string_param(params, "session_id")
            profile_name = _string_param(params, "agentProfile") or _string_param(params, "profileName")
            if not profile_name:
                raise RequestError.invalid_params({"details": "agentProfile is required."})
            try:
                result = await self._manager.switch_agent(session_id, profile_name)
            except Exception as exc:
                raise _request_error(exc) from exc
            return _profile_switched_payload(result, session_id=session_id)
        if method == "settings/reload":
            session_id = _string_param(params, "sessionId") or _string_param(params, "session_id")
            # A reload happens outside any prompt turn, and ``_handle_event`` only
            # runs inside one — so the warnings saying which of the client's
            # values were dropped had nowhere to go and were simply lost.
            warnings: list[Warning] = []
            try:
                await self._manager.reload_settings(session_id, warnings=warnings)
            except Exception as exc:
                raise _request_error(exc) from exc
            for warning in warnings:
                await self._send_warning(session_id, warning)
            await self._send_runtime_update(session_id)
            return {}
        if method == "settings/options":
            session_id = _string_param(params, "sessionId") or _string_param(params, "session_id")
            try:
                # Reads the settings document under its file lock, so it waits
                # on whatever holds it — inline, that wait is the whole ACP
                # loop's, and every other session stalls behind a read.
                return await asyncio.to_thread(self._manager.get_config_options, session_id or None)
            except Exception as exc:
                raise _request_error(exc) from exc
        if method == "session/set_config_option":
            session_id = _string_param(params, "sessionId") or _string_param(params, "session_id")
            key = _string_param(params, "key")
            # Same gap as ``settings/reload``, and worse: this route writes the
            # value to disk before reloading it, so a clamped or rejected input
            # is persisted while the client is told only that it succeeded.
            warnings = []
            try:
                result = await self._manager.apply_config_option(
                    session_id, key, params.get("value", ""), warnings=warnings
                )
            except Exception as exc:
                raise _request_error(exc) from exc
            for warning in warnings:
                await self._send_warning(session_id, warning)
            await self._send_runtime_update(session_id)
            return {"sessionId": session_id, **result}
        if method == "session/set_workspace":
            session_id = _string_param(params, "sessionId") or _string_param(params, "session_id")
            primary_cwd = _string_param(params, "primaryCwd") or _string_param(params, "primary_cwd")
            if not primary_cwd:
                raise RequestError.invalid_params({"details": "primaryCwd is required."})
            try:
                result = await self._manager.set_workspace(session_id, primary_cwd)
            except Exception as exc:
                raise _request_error(exc) from exc
            # A workspace change soft-restarts the agent, which can alter skills,
            # MCP/runtime details, memory, and workspace-loaded context — push the
            # refreshed runtime so the client doesn't keep stale state.
            await self._send_runtime_update(session_id)
            return {
                "sessionId": result.session_id or session_id,
                "primaryCwd": result.primary_cwd,
                "workingDirs": result.working_dirs,
                "referenceFiles": result.reference_files,
            }
        if method == "session/history":
            session_id = _string_param(params, "sessionId") or _string_param(params, "session_id")
            cwd = _optional_string_param(params, "cwd")
            try:
                canonical_id, messages = await self._manager.session_history(cwd=cwd, session_id=session_id)
            except Exception as exc:
                raise _request_error(exc) from exc
            return {"sessionId": canonical_id, "messages": messages}
        if method == "profiles/agents/list":
            return {"agents": self._manager.list_agent_profiles()}
        if method == "profiles/agents/read":
            name = _string_param(params, "name")
            return {"profile": self._manager.read_agent_profile(name)}
        if method == "profiles/agents/write":
            profile = _dict_param(params, "profile")
            try:
                return self._manager.write_agent_profile(profile)
            except Exception as exc:
                raise _request_error(exc) from exc
        if method == "profiles/agents/delete":
            name = _string_param(params, "name")
            try:
                return self._manager.delete_agent_profile(name)
            except Exception as exc:
                raise _request_error(exc) from exc
        if method == "profiles/agents/reset":
            name = _string_param(params, "name")
            try:
                return self._manager.reset_agent_profile(name)
            except Exception as exc:
                raise _request_error(exc) from exc
        if method == "profiles/models/list":
            return {"models": self._manager.list_model_profiles()}
        if method == "profiles/models/read":
            profile_id = _string_param(params, "id") or _string_param(params, "profileId")
            return {"profile": self._manager.read_model_profile(profile_id)}
        if method == "profiles/models/write":
            profile = _dict_param(params, "profile")
            try:
                return self._manager.write_model_profile(profile)
            except Exception as exc:
                raise _request_error(exc) from exc
        if method == "profiles/models/delete":
            profile_id = _string_param(params, "id") or _string_param(params, "profileId")
            try:
                return self._manager.delete_model_profile(profile_id)
            except Exception as exc:
                raise _request_error(exc) from exc
        if method == "mcp/test":
            config = _dict_param(params, "server")
            try:
                return await self._manager.test_mcp_server(config)
            except Exception as exc:
                raise _request_error(exc) from exc
        if method == "mcp/list":
            session_id = _string_param(params, "sessionId") or _string_param(params, "session_id")
            runtime = self._runtime_payload(session_id)
            details = runtime.get("runtimeDetails", {})
            return {
                "sessionId": session_id,
                "mcpTools": details.get("mcp_tools", {}),
                "mcpFailures": details.get("mcp_failures", {}),
            }
        if method == "skills/list":
            session_id = _string_param(params, "sessionId") or _string_param(params, "session_id")
            runtime = self._runtime_payload(session_id)
            details = runtime.get("runtimeDetails", {})
            return {
                "sessionId": session_id,
                "skillSources": details.get("skill_sources", {}),
                "skillDetails": details.get("skill_details", []),
            }
        raise RequestError.method_not_found(method)

    async def _handle_event(
        self,
        session_id: str,
        event: Event,
        bridge: AcpEventBridge,
        pending_approvals: dict[str, ApprovalRequest],
    ) -> None:
        if isinstance(event, ApprovalRequest):
            if event.judging:
                pending_approvals[event.request_id] = event
            else:
                await self._request_permission(session_id, event)
            return
        if isinstance(event, ApprovalReviewed):
            await self._client_or_error().ext_notification(
                "chrys/approval_reviewed",
                {
                    "sessionId": session_id,
                    "requestId": event.request_id,
                    "approved": event.approved,
                    "reason": event.reason,
                },
            )
            request = pending_approvals.pop(event.request_id, None)
            if request is not None and not event.approved:
                await self._request_permission(session_id, request, reason=event.reason)
            return
        if isinstance(event, QuestionToUser):
            await self._request_input(session_id, event)
            return
        if await self._handle_chrys_extension_event(session_id, event):
            return
        for update in bridge.updates_for_event(event):
            await self._client_or_error().session_update(session_id=session_id, update=update)

    async def _handle_chrys_extension_event(self, session_id: str, event: Event) -> bool:
        if isinstance(event, InvocationEvent) and event.origin.root.kind == "workflow_node":
            return False
        client = self._client_or_error()
        event_session_id = _event_session_id(event, session_id)
        if isinstance(event, AgentLoadStarted):
            await client.ext_notification(
                "chrys/agent_load_started",
                {
                    "sessionId": event_session_id,
                    "operation": event.operation,
                    "fromProfile": event.from_profile,
                    "toProfile": event.to_profile,
                    "fromDisplayName": event.from_display_name,
                    "toDisplayName": event.to_display_name,
                },
            )
            return True
        if isinstance(event, AgentLoadProgress):
            await client.ext_notification(
                "chrys/agent_load_progress",
                {
                    "sessionId": event_session_id,
                    "phase": event.phase,
                    "message": event.message,
                    "serverName": event.server_name,
                    "current": event.current,
                    "total": event.total,
                    "failed": event.failed,
                },
            )
            return True
        if isinstance(event, AgentLoadFinished):
            await client.ext_notification(
                "chrys/agent_load_finished",
                {
                    "sessionId": event_session_id,
                    "operation": event.operation,
                    "agentProfile": event.agent_profile,
                    "displayName": event.display_name,
                },
            )
            return True
        if isinstance(event, AgentLoadFailed):
            await client.ext_notification(
                "chrys/agent_load_failed",
                {
                    "sessionId": event_session_id,
                    "operation": event.operation,
                    "agentProfile": event.agent_profile,
                    "displayName": event.display_name,
                    "message": event.message,
                },
            )
            return True
        if isinstance(event, SessionReady):
            await client.ext_notification(
                "chrys/runtime_update",
                {
                    "sessionId": event_session_id,
                    "runtime": {
                        "sessionId": event_session_id,
                        "agentProfile": event.agent_profile,
                        "displayName": event.display_name,
                        "modelProfileId": event.model_profile_id,
                        "maxContextTokens": event.max_context_tokens,
                        "toolNames": event.tool_names,
                        "toolKinds": event.tool_kinds,
                        "skillNames": event.skill_names,
                        "subAgentToolNames": event.sub_agent_tool_names,
                        "memoryFiles": event.memory_files,
                        "runtimeDetails": jsonable_dataclass(event.runtime_details),
                    },
                },
            )
            return True
        if isinstance(event, AgentRuntimeUpdated):
            await client.ext_notification(
                "chrys/runtime_update",
                {
                    "sessionId": session_id,
                    "runtime": _runtime_update_payload(event),
                },
            )
            return True
        if isinstance(event, ProfileSwitched):
            await client.ext_notification(
                "chrys/profile_switched", _profile_switched_payload(event, session_id=session_id)
            )
            return True
        if isinstance(event, WorkspaceUpdated):
            await client.ext_notification(
                "chrys/workspace_updated",
                {
                    "sessionId": event_session_id,
                    "primaryCwd": event.primary_cwd,
                    "workingDirs": event.working_dirs,
                    "referenceFiles": event.reference_files,
                },
            )
            return True
        if isinstance(event, UsageUpdate):
            await client.ext_notification(
                "chrys/usage_update",
                {
                    "sessionId": event_session_id,
                    "agentProfile": event.agent_profile,
                    "usageSourceId": event.usage_source_id,
                    "inputTokens": event.input_tokens,
                    "outputTokens": event.output_tokens,
                    "totalTokens": event.total_tokens,
                    "pct": event.pct,
                    "maxContextTokens": event.max_context_tokens,
                    "totalSessionTokens": event.total_session_tokens,
                    "totalSessionInputTokens": event.total_session_input_tokens,
                    "totalSessionOutputTokens": event.total_session_output_tokens,
                    "cacheHitTokens": event.cache_hit_tokens,
                    "totalSessionCacheHitTokens": event.total_session_cache_hit_tokens,
                    "localTokens": event.local_tokens,
                    "calibrationRatio": event.calibration_ratio,
                    "systemOverheadTokens": event.system_overhead_tokens,
                },
            )
            return False
        if isinstance(event, SessionRestored):
            await client.ext_notification(
                "chrys/session_restored",
                {
                    "sessionId": event_session_id,
                    "agentProfile": event.agent_profile,
                    "displayName": event.display_name,
                    "messageCount": event.message_count,
                    "cwdWarning": event.cwd_warning,
                    "primaryCwd": event.primary_cwd,
                },
            )
            return True
        if isinstance(event, Error):
            await client.ext_notification(
                "chrys/error",
                {
                    "sessionId": event_session_id,
                    "code": event.code,
                    "message": event.message,
                    "recoverable": event.recoverable,
                },
            )
            return True
        if isinstance(event, Warning):
            await self._send_warning(event_session_id, event)
            return True
        if isinstance(event, ApprovalModeUpdated):
            await client.session_update(
                session_id=event_session_id,
                update=acp_schema.CurrentModeUpdate(sessionUpdate="current_mode_update", currentModeId=event.mode),
            )
            return True
        if isinstance(event, ContextCompressed):
            await client.ext_notification(
                "chrys/context_compressed",
                {
                    "sessionId": event_session_id,
                    "compressedContextId": event.compressed_context_id,
                    "summary": event.summary,
                    "freedMessages": event.freed_messages,
                    "turnRange": list(event.turn_range),
                    "source": event.source,
                },
            )
            return True
        if isinstance(event, InvocationContextPressure):
            await client.ext_notification(
                "chrys/context_pressure",
                {
                    "sessionId": event_session_id,
                    "reason": event.reason,
                    "attempts": event.attempts,
                    "sideCallTokens": event.side_call_tokens,
                    "sideCallTokenBudget": event.side_call_token_budget,
                    "source": event.source,
                    "invocationId": event.origin.invocation_id if event.origin.kind == "sub_agent" else "",
                },
            )
            return True
        if isinstance(event, ToolCompacted):
            await client.ext_notification(
                "chrys/tool_compacted",
                {
                    "sessionId": event_session_id,
                    "compactedGroups": event.compacted_groups,
                    "phase": event.phase,
                    "turnNumbers": event.turn_numbers,
                    "compactedToolNames": event.compacted_tool_names,
                    "tokensBefore": event.tokens_before,
                    "tokensAfter": event.tokens_after,
                    "lastWordsGenerated": event.last_words_generated,
                },
            )
            return True
        if isinstance(event, CompactionStarted):
            await client.ext_notification(
                "chrys/compaction_started",
                {
                    "sessionId": event_session_id,
                    "compactionId": event.compaction_id,
                    "phase": event.phase,
                },
            )
            return True
        if isinstance(event, CompactionFinished):
            await client.ext_notification(
                "chrys/compaction_finished",
                {
                    "sessionId": event_session_id,
                    "compactionId": event.compaction_id,
                    "outcome": event.outcome,
                    "durationMs": event.duration_ms,
                    "lastWords": event.last_words,
                    "formatViolation": event.format_violation,
                    "failureReason": event.failure_reason,
                },
            )
            return True
        if isinstance(event, UserInjectResult):
            await client.ext_notification(
                "chrys/user_inject_result",
                {
                    "sessionId": event_session_id,
                    "text": event.text,
                    "consumed": event.consumed,
                    "createdAt": event.created_at.isoformat()
                    if isinstance(event.created_at, datetime)
                    else event.created_at,
                    "injectionId": event.injection_id,
                },
            )
            return True
        if isinstance(event, InvocationEvent) and event.origin.kind != "sub_agent":
            return False
        # Sub-agent events are dual-path (like UsageUpdate): the Chrys extension
        # notification carries the rich nested detail, and returning False lets
        # AcpEventBridge also project standard `session/update` tool-call progress
        # onto the parent tool call — so standard-only clients aren't regressed.
        if isinstance(event, InvocationStarted):
            await client.ext_notification(
                "chrys/sub_agent_invocation_start",
                {
                    "sessionId": event_session_id,
                    "agentName": event.agent_name,
                    "invocationId": event.origin.invocation_id,
                    "toolName": event.tool_name,
                    "parentCallId": event.parent_call_id,
                },
            )
            return False
        if isinstance(event, InvocationToolCallStart):
            await client.ext_notification(
                "chrys/sub_agent_tool_call_start",
                {
                    "sessionId": event_session_id,
                    "agentName": event.agent_name,
                    "invocationId": event.origin.invocation_id,
                    "toolName": event.tool_name,
                    "args": event.args,
                    "callId": event.call_id,
                },
            )
            return False
        if isinstance(event, InvocationToolCallResult):
            await client.ext_notification(
                "chrys/sub_agent_tool_call_result",
                {
                    "sessionId": event_session_id,
                    "agentName": event.agent_name,
                    "invocationId": event.origin.invocation_id,
                    "toolName": event.tool_name,
                    "callId": event.call_id,
                    "result": event.result,
                    "durationMs": event.duration_ms,
                    "metadata": event.metadata,
                },
            )
            return False
        if isinstance(event, InvocationProgress):
            await client.ext_notification(
                "chrys/sub_agent_progress",
                {
                    "sessionId": event_session_id,
                    "agentName": event.agent_name,
                    "invocationId": event.origin.invocation_id,
                    "toolCallCount": event.tool_call_count,
                    "totalTokens": event.total_tokens,
                    "totalUsageTokens": event.total_usage_tokens,
                    "usageUnreportedAttempts": event.usage_unreported_attempts,
                },
            )
            return False
        if isinstance(event, InvocationRetryAttempt):
            await client.ext_notification(
                "chrys/sub_agent_retry_attempt",
                {
                    "sessionId": event_session_id,
                    "agentName": event.agent_name,
                    "invocationId": event.origin.invocation_id,
                    "message": event.message,
                    "attempt": event.attempt,
                    "maxAttempts": event.max_attempts,
                    "delaySeconds": event.delay_seconds,
                },
            )
            return False
        if isinstance(event, InvocationCompactionStarted):
            await client.ext_notification(
                "chrys/sub_agent_compaction_started",
                {
                    "sessionId": event_session_id,
                    "agentName": event.agent_name,
                    "invocationId": event.origin.invocation_id,
                    "compactionId": event.compaction_id,
                    "phase": event.phase,
                },
            )
            return False
        if isinstance(event, InvocationCompactionFinished):
            await client.ext_notification(
                "chrys/sub_agent_compaction_finished",
                {
                    "sessionId": event_session_id,
                    "agentName": event.agent_name,
                    "invocationId": event.origin.invocation_id,
                    "compactionId": event.compaction_id,
                    "outcome": event.outcome,
                    "durationMs": event.duration_ms,
                    "formatViolation": event.format_violation,
                    "failureReason": event.failure_reason,
                },
            )
            return False
        if isinstance(event, InvocationCompactionCommitted):
            # Post-commit confirmation trailing finished(ok) — a finished-ok
            # round without this notification was abandoned (spill failure).
            await client.ext_notification(
                "chrys/sub_agent_compaction_committed",
                {
                    "sessionId": event_session_id,
                    "agentName": event.agent_name,
                    "invocationId": event.origin.invocation_id,
                    "compactionId": event.compaction_id,
                    "phase": event.phase,
                },
            )
            return False
        if isinstance(event, InvocationPaused):
            await client.ext_notification(
                "chrys/sub_agent_paused",
                {
                    "sessionId": event_session_id,
                    "agentName": event.agent_name,
                    "invocationId": event.origin.invocation_id,
                    "toolName": event.tool_name,
                    "reason": event.reason,
                    "lastError": event.last_error,
                    "retryAttempts": event.retry_attempts,
                },
            )
            return False
        if isinstance(event, InvocationResumed):
            await client.ext_notification(
                "chrys/sub_agent_resumed",
                {
                    "sessionId": event_session_id,
                    "agentName": event.agent_name,
                    "invocationId": event.origin.invocation_id,
                },
            )
            return False
        if isinstance(event, InvocationAborted):
            await client.ext_notification(
                "chrys/sub_agent_aborted",
                {
                    "sessionId": event_session_id,
                    "agentName": event.agent_name,
                    "invocationId": event.origin.invocation_id,
                    "lastError": event.last_error,
                },
            )
            return False
        if isinstance(event, InvocationCascadeAborted):
            await client.ext_notification(
                "chrys/sub_agent_cascade_aborted",
                {
                    "sessionId": event_session_id,
                    "agentName": event.agent_name,
                    "invocationId": event.origin.invocation_id,
                },
            )
            return False
        if isinstance(event, SessionSaved):
            return False
        return False

    def _session_mode_state(self, session_id: str) -> acp_schema.SessionModeState:
        current = self._manager.get(session_id).host.engine.approval_mode.value
        return acp_schema.SessionModeState(
            availableModes=[
                acp_schema.SessionMode(id=mode_id, name=name, description=desc)
                for mode_id, name, desc in _APPROVAL_MODES
            ],
            currentModeId=current,
        )

    def _session_model_state(self, session_id: str) -> acp_schema.SessionModelState:
        current = self._manager.get(session_id).host.engine.runtime_details.model.profile_id
        return acp_schema.SessionModelState(
            availableModels=[
                acp_schema.ModelInfo(
                    modelId=str(profile["id"]),
                    name=str(profile.get("name") or profile["id"]),
                    description=_model_info_description(profile),
                )
                for profile in self._manager.list_model_profiles()
            ],
            currentModelId=current,
        )

    async def _send_session_restored(self, session_id: str) -> None:
        payload = {"sessionId": session_id}
        await self._client_or_error().ext_notification("chrys/session_restored", payload)

    async def _send_plan_update(self, session_id: str) -> None:
        # Seed/refresh the client's plan at session-establishment points
        # (new_session, load_session, rollback) where no TodoListUpdated
        # event fires.  Sent unconditionally: empty entries clear a stale
        # plan left over from a previous session in the same client pane.
        tracker = self._manager.get(session_id).host.engine.todo_tracker
        items = tracker.snapshot() if tracker is not None else ()
        await self._client_or_error().session_update(
            session_id=session_id,
            update=plan_update_for_todos(items),
        )

    async def _send_warning(self, session_id: str, warning: Warning) -> None:
        """Send one ``chrys/warning``, whether it came from a turn or an RPC."""
        await self._client_or_error().ext_notification(
            "chrys/warning",
            {
                "sessionId": session_id,
                "code": warning.code,
                "message": warning.message,
            },
        )

    async def _send_runtime_update(self, session_id: str) -> None:
        # Same envelope as the event-bridged senders (SessionReady /
        # AgentRuntimeUpdated): `chrys/runtime_update` is always
        # {sessionId, runtime: {...}} so a client handler reads one shape.
        await self._client_or_error().ext_notification(
            "chrys/runtime_update",
            {"sessionId": session_id, "runtime": self._runtime_payload(session_id)},
        )

    def _runtime_payload(self, session_id: str) -> dict[str, Any]:
        session = self._manager.get(session_id)
        engine = session.host.engine
        details = engine.runtime_details
        model = details.model
        usage = engine.usage_publisher.make_usage_event(session_id=session_id)
        return {
            "sessionId": session_id,
            "agentProfile": session.profile_name,
            "displayName": session.profile_name,
            "modelProfileId": model.profile_id,
            "maxContextTokens": model.max_context_tokens,
            "inputTokens": usage.input_tokens,
            "outputTokens": usage.output_tokens,
            "totalTokens": usage.total_tokens,
            "pct": usage.pct,
            "totalSessionTokens": usage.total_session_tokens,
            "totalSessionInputTokens": usage.total_session_input_tokens,
            "totalSessionOutputTokens": usage.total_session_output_tokens,
            "cacheHitTokens": usage.cache_hit_tokens,
            "totalSessionCacheHitTokens": usage.total_session_cache_hit_tokens,
            "localTokens": usage.local_tokens,
            "calibrationRatio": usage.calibration_ratio,
            "systemOverheadTokens": usage.system_overhead_tokens,
            "runtimeDetails": jsonable_dataclass(details),
        }

    async def _refresh_mutation_attribution(self, session_id: str) -> None:
        """Fold newly published peer claims in before a net-summary read.

        Best-effort: failures (including unknown session ids) fall
        through — the payload builder raises the canonical error.
        """
        with contextlib.suppress(Exception):
            engine = self._manager.get(session_id).host.engine
            await engine.refresh_mutation_attribution()

    def _mutations_payload(self, session_id: str) -> dict[str, Any]:
        session = self._manager.get(session_id)
        engine = session.host.engine
        tracker = engine.mutation_tracker
        if tracker is None:
            return {
                "sessionId": session_id,
                "currentTurn": engine.current_turn_number,
                "availableRollbackTurns": [],
                "turns": [],
                "files": [],
            }
        file_summary = tracker.get_session_file_summary()
        return {
            "sessionId": session_id,
            "currentTurn": engine.current_turn_number,
            "availableRollbackTurns": engine.available_rollback_turns(),
            "turns": [
                {
                    "turnId": turn.turn_id,
                    "mutationCount": len(turn.mutations),
                    "mutations": [_mutation_payload(mutation) for mutation in turn.mutations],
                }
                for turn in tracker.get_all_turns()
            ],
            "files": [
                {
                    "path": path,
                    "beforeHash": diff.before,
                    "afterHash": diff.after,
                    "operation": _operation_from_hashes(diff.before, diff.after),
                    # Folded provenance badges: a peer session also wrote
                    # this path / the net change includes window-diff
                    # inference.  Foreign-only paths are excluded from
                    # this net-level list entirely (raw turn payloads
                    # keep them).
                    "contested": diff.contested,
                    "inferred": diff.inferred,
                }
                for path, diff in sorted(file_summary.items())
                # Content withheld by SnapshotPolicy (too large / binary):
                # a None hash would mis-read as create/delete.
                if not diff.content_unavailable
            ],
        }

    def _diff_payload(self, session_id: str, *, path: str | None, turn: int | None) -> dict[str, Any]:
        session = self._manager.get(session_id)
        tracker = session.host.engine.mutation_tracker
        if tracker is None:
            return {"sessionId": session_id, "entries": []}
        summary = tracker.get_turn_file_summary(turn) if turn is not None else tracker.get_session_file_summary()
        entries = []
        for file_path, diff in sorted(summary.items()):
            if path is not None and file_path != path:
                continue
            if diff.content_unavailable:
                continue
            entries.append(_diff_entry_payload(tracker, file_path, diff))
        return {
            "sessionId": session_id,
            "turn": turn,
            "entries": entries,
        }

    async def _request_input(self, session_id: str, event: QuestionToUser) -> None:
        client = self._client_or_error()
        event_bus = self._manager.get(session_id).host.event_bus
        loop = asyncio.get_running_loop()
        cancel_future: asyncio.Future[None] = loop.create_future()
        cancel_key = (session_id, event.request_id)
        already_cancelled = self._install_pending_cancel(
            self._pending_input_cancels,
            self._input_cancel_tombstones,
            cancel_key,
            cancel_future,
        )
        if already_cancelled:
            # The wait died before we could ask: sending the request anyway
            # would render a stale dialog on the client (nothing revokes it).
            await event_bus.publish(
                AskUserResponse(
                    request_id=event.request_id,
                    cancelled=True,
                    session_id=event.session_id or session_id,
                )
            )
            return

        async def _request(params: dict[str, Any]) -> object | None:
            if cancel_future.done():
                return None
            request_task = asyncio.create_task(client.ext_method("chrys/request_input", params))
            try:
                done, _pending = await asyncio.wait(
                    {request_task, cancel_future},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if cancel_future in done:
                    return None
                try:
                    return request_task.result()
                except asyncio.CancelledError:
                    # Only the client callback died; the prompt itself keeps
                    # running and reads this as "no answer". Cancellation of
                    # this coroutine propagates out of the wait above so an
                    # owner tearing the prompt down never sees a synthetic
                    # response.
                    return None
            except Exception:
                return None
            finally:
                if request_task.done():
                    _observe_task_exception(request_task)
                else:
                    request_task.cancel()
                    request_task.add_done_callback(_observe_task_exception)

        try:
            response = await _request(
                {
                    "sessionId": session_id,
                    "requestId": event.request_id,
                    "questions": [_request_input_question_payload(item) for item in event.questions],
                    "callerName": event.caller_name,
                }
            )
        finally:
            self._pending_input_cancels.pop(cancel_key, None)
        outcome = validate_request_input_response(response, questions=event.questions)
        if cancel_future.done() or not isinstance(outcome, tuple):
            response_event = AskUserResponse(request_id=event.request_id, cancelled=True)
        else:
            response_event = AskUserResponse(request_id=event.request_id, answers=outcome)
        response_event.session_id = event.session_id or session_id
        await event_bus.publish(response_event)

    async def _request_permission(self, session_id: str, event: ApprovalRequest, *, reason: str = "") -> None:
        client = self._client_or_error()
        event_bus = self._manager.get(session_id).host.event_bus
        tool_call = acp_schema.ToolCallUpdate(
            toolCallId=event.request_id,
            title=tool_call_title(event.tool_name, event.tool_kind, event.args, intent_summary=event.intent_summary),
            kind=acp_tool_kind(event.tool_kind),
            status="pending",
            rawInput=event.args,
            rawOutput=reason or None,
            field_meta={
                "chrys": {
                    "tool_name": event.tool_name,
                    "tool_kind": event.tool_kind,
                }
            },
        )
        loop = asyncio.get_running_loop()
        cancel_future: asyncio.Future[None] = loop.create_future()
        cancel_key = (session_id, event.request_id)
        already_cancelled = self._install_pending_cancel(
            self._pending_permission_cancels,
            self._permission_cancel_tombstones,
            cancel_key,
            cancel_future,
        )
        if already_cancelled:
            # The wait died before we could ask: sending the request anyway
            # would render a stale dialog on the client (nothing revokes it).
            await event_bus.publish(
                ApprovalResponse(
                    request_id=event.request_id,
                    approved=False,
                    reason="ACP permission request was cancelled.",
                    session_id=event.session_id or session_id,
                )
            )
            return
        request_task = asyncio.create_task(
            client.request_permission(
                session_id=session_id,
                tool_call=tool_call,
                options=[
                    acp_schema.PermissionOption(
                        optionId=_ALLOW_OPTION_ID,
                        name="Allow once",
                        kind="allow_once",
                    ),
                    acp_schema.PermissionOption(
                        optionId=_REJECT_OPTION_ID,
                        name="Reject once",
                        kind="reject_once",
                    ),
                ],
            )
        )
        self._pending_permission_tasks[cancel_key] = request_task
        try:
            done, _pending = await asyncio.wait(
                {request_task, cancel_future},
                timeout=self._permission_timeout_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if cancel_future in done:
                approved = False
                reason_text = "ACP permission request was cancelled."
            elif request_task in done:
                try:
                    response = request_task.result()
                except asyncio.CancelledError:
                    approved = False
                    reason_text = "ACP permission request failed or was cancelled."
                else:
                    approved = (
                        isinstance(response.outcome, acp_schema.AllowedOutcome)
                        and response.outcome.option_id == _ALLOW_OPTION_ID
                    )
                    if approved:
                        reason_text = ""
                    elif isinstance(response.outcome, acp_schema.DeniedOutcome):
                        reason_text = "ACP permission request was cancelled."
                    else:
                        reason_text = "Rejected by ACP client."
            else:
                approved = False
                reason_text = "ACP permission request timed out."
        except asyncio.CancelledError:
            raise
        except Exception:
            approved = False
            reason_text = "ACP permission request failed or was cancelled."
        finally:
            if self._pending_permission_cancels.get(cancel_key) is cancel_future:
                self._pending_permission_cancels.pop(cancel_key, None)
            if self._pending_permission_tasks.get(cancel_key) is request_task:
                self._pending_permission_tasks.pop(cancel_key, None)
            if request_task.done():
                _observe_task_exception(request_task)
            else:
                request_task.cancel()
                request_task.add_done_callback(_observe_task_exception)
        await event_bus.publish(
            ApprovalResponse(
                request_id=event.request_id,
                approved=approved,
                reason=reason_text,
                session_id=event.session_id or session_id,
            )
        )

    def _client_or_error(self) -> Client:
        if self._client is None:
            raise RequestError.internal_error({"details": "ACP client connection is not ready."})
        return self._client

    async def _interrupt_and_cancel_pending_waits(self, session_id: str) -> None:
        try:
            await self._manager.cancel(session_id)
        except AcpSessionError:
            pass
        finally:
            self._cancel_pending_waits(session_id)

    def _cancel_pending_waits(self, session_id: str) -> None:
        self._resolve_pending_cancels(self._pending_input_cancels, session_id)
        self._resolve_pending_cancels(self._pending_permission_cancels, session_id)

    @staticmethod
    def _deliver_or_record_cancellation(
        pending_cancels: dict[tuple[str, str], asyncio.Future[None]],
        tombstones: set[tuple[str, str]],
        key: tuple[str, str],
    ) -> None:
        future = pending_cancels.get(key)
        if future is None:
            tombstones.add(key)
        elif not future.done():
            future.set_result(None)

    @staticmethod
    def _install_pending_cancel(
        pending_cancels: dict[tuple[str, str], asyncio.Future[None]],
        tombstones: set[tuple[str, str]],
        key: tuple[str, str],
        future: asyncio.Future[None],
    ) -> bool:
        """Install a waiter and consume an early cancellation without an await gap.

        Returns True when an early-cancellation tombstone was consumed: the
        wait is already dead, so the caller must not send the client a
        request nobody is waiting on.
        """
        if key in tombstones:
            tombstones.remove(key)
            future.set_result(None)
            return True
        pending_cancels[key] = future
        return False

    def _clear_cancellation_state(self, session_id: str) -> None:
        self._cancel_pending_waits(session_id)
        self._drop_session_tombstones(session_id)

    def _drop_session_tombstones(self, session_id: str) -> None:
        self._input_cancel_tombstones = {key for key in self._input_cancel_tombstones if key[0] != session_id}
        self._permission_cancel_tombstones = {key for key in self._permission_cancel_tombstones if key[0] != session_id}

    @staticmethod
    def _resolve_pending_cancels(
        pending_cancels: dict[tuple[str, str], asyncio.Future[None]],
        session_id: str,
    ) -> None:
        for (pending_session_id, _request_id), future in list(pending_cancels.items()):
            if pending_session_id == session_id and not future.done():
                future.set_result(None)

    @staticmethod
    def _reject_additional_directories(additional_directories: list[str] | None) -> None:
        if additional_directories:
            raise RequestError.invalid_params(
                {"details": f"additionalDirectories are not supported by {APP_DISPLAY_NAME} ACP phase 1."}
            )


def _request_error(exc: Exception, *, usage: dict[str, int] | None = None) -> RequestError:
    if isinstance(exc, RequestError):
        if usage is None:
            return exc
        data = dict(exc.data) if isinstance(exc.data, dict) else {}
        data["usage"] = usage
        return RequestError(exc.code, str(exc), data)
    if isinstance(exc, AcpSessionError):
        data: dict[str, object] = {"details": str(exc)}
        if usage is not None:
            data["usage"] = usage
        return RequestError.invalid_params(data)
    details = str(exc)
    details = f"{type(exc).__name__}: {details}" if details else type(exc).__name__
    data: dict[str, object] = {"details": details}
    if usage is not None:
        data["usage"] = usage
    return RequestError.internal_error(data)


def _prompt_usage_payload(event: UsageUpdate) -> dict[str, int]:
    """Return strict cumulative ACP spend aliases from one locked engine snapshot."""
    payload = {
        "inputTokens": max(0, event.total_session_input_tokens),
        "outputTokens": max(0, event.total_session_output_tokens),
        "totalTokens": max(0, event.total_session_tokens),
    }
    if event.total_session_cache_hit_tokens is not None:
        payload["cachedReadTokens"] = max(0, event.total_session_cache_hit_tokens)
    return payload


def _observe_task_exception(task: asyncio.Task[Any]) -> None:
    if task.cancelled():
        return
    with contextlib.suppress(Exception):
        task.exception()


def _runtime_update_payload(event: AgentRuntimeUpdated) -> dict[str, Any]:
    return {
        "modelProfileId": event.model_profile_id,
        "maxContextTokens": event.max_context_tokens,
        "toolNames": event.tool_names,
        "skillNames": event.skill_names,
        "memoryFiles": event.memory_files,
        "runtimeDetails": jsonable_dataclass(event.runtime_details),
    }


def _rollback_result_payload(event: RollbackResult) -> dict[str, Any]:
    return {
        "sessionId": event.session_id,
        "targetTurn": event.target_turn,
        "rolledBackUserText": event.rolled_back_user_text,
        "filesReverted": event.files_reverted,
        "restoreResults": [
            {
                "path": result.path,
                "outcome": result.outcome.value,
                "reason": result.reason,
                "changed": result.changed,
                "ok": result.ok,
            }
            for result in event.restore_results
        ],
        # Files the rollback plan dropped, with a RollbackExclusionReason
        # value ("unrestorable" / "move_poisoned" / ...) — never silent.
        "exclusions": [{"path": path, "reason": reason} for path, reason in event.exclusions],
        # Advisory repo-level notices (plan warnings), plain strings.
        "warnings": list(event.warnings),
    }


def _profile_switched_payload(event: ProfileSwitched, *, session_id: str) -> dict[str, Any]:
    return {
        "sessionId": session_id,
        "fromProfile": event.from_profile,
        "toProfile": event.to_profile,
        "fromDisplayName": event.from_display_name,
        "toDisplayName": event.to_display_name,
        "messageCount": event.message_count,
        "modelProfileId": event.model_profile_id,
        "maxContextTokens": event.max_context_tokens,
        "toolNames": event.tool_names,
        "skillNames": event.skill_names,
        "subAgentToolNames": event.sub_agent_tool_names,
        "memoryFiles": event.memory_files,
        "runtimeDetails": jsonable_dataclass(event.runtime_details),
    }


def _mutation_payload(mutation: Any) -> dict[str, Any]:
    return {
        "path": mutation.path,
        "operation": mutation.operation.value,
        "source": mutation.source.value,
        "toolCallId": mutation.tool_call_id,
        "timestamp": mutation.timestamp,
        "oldPath": mutation.old_path,
        "beforeHash": mutation.before_hash,
        "afterHash": mutation.after_hash,
        # Why a side's content backup was withheld ("too_large" /
        # "binary"), or None when the hash is real / the file was absent.
        "beforeSkip": mutation.before_skip.value if mutation.before_skip else None,
        "afterSkip": mutation.after_skip.value if mutation.after_skip else None,
        # Attribution confidence ("proven" / "assumed" / "foreign") and
        # peer-conflict flag.  Raw turn payloads stay unfiltered —
        # foreign rows remain present so clients can filter or badge
        # themselves (net-level lists exclude them instead).
        "provenance": mutation.provenance.value if mutation.provenance else None,
        "contested": mutation.contested,
    }


def _diff_entry_payload(tracker: Any, path: str, diff: FileHashDiff) -> dict[str, Any]:
    before_bytes = tracker.store.read_blob(diff.before) if diff.before else None
    after_bytes = tracker.store.read_blob(diff.after) if diff.after else None
    is_binary = bool(
        (before_bytes and EncodingDetector.looks_binary(before_bytes))
        or (after_bytes and EncodingDetector.looks_binary(after_bytes))
    )
    before_text = "" if before_bytes is None or is_binary else decode_bytes(before_bytes)
    after_text = "" if after_bytes is None or is_binary else decode_bytes(after_bytes)
    return {
        "path": path,
        "operation": _operation_from_hashes(diff.before, diff.after),
        "beforeHash": diff.before,
        "afterHash": diff.after,
        "beforeText": before_text,
        "afterText": after_text,
        "isBinary": is_binary,
        "bytesChanged": diff.before != diff.after,
        # Folded provenance badges — same semantics as the ``files``
        # list in session/mutations, so diff clients can badge entries
        # without joining against that endpoint.
        "contested": diff.contested,
        "inferred": diff.inferred,
    }


def _operation_from_hashes(before_hash: str | None, after_hash: str | None) -> str:
    if before_hash is None and after_hash is not None:
        return "create"
    if before_hash is not None and after_hash is None:
        return "delete"
    return "modify"


def _event_session_id(event: Event, fallback: str) -> str:
    value = event.session_id
    return value if isinstance(value, str) and value else fallback


def _model_info_description(profile: dict[str, Any]) -> str | None:
    parts = [str(profile[key]) for key in ("provider", "modelId") if profile.get(key)]
    return " · ".join(parts) or None


def _string_param(params: dict[str, Any], key: str) -> str:
    value = params.get(key, "")
    return value if isinstance(value, str) else ""


def _optional_string_param(params: dict[str, Any], key: str) -> str | None:
    value = params.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise RequestError.invalid_params({"details": f"{key} must be a string."})
    return value


def _int_param(params: dict[str, Any], key: str) -> int:
    value = params.get(key)
    if type(value) is not int:
        raise RequestError.invalid_params({"details": f"{key} must be an integer."})
    return value


def _optional_int_param(params: dict[str, Any], key: str) -> int | None:
    value = params.get(key)
    if value is None:
        return None
    if type(value) is not int:
        raise RequestError.invalid_params({"details": f"{key} must be an integer."})
    return value


def _bool_param(params: dict[str, Any], key: str, *, default: bool) -> bool:
    value = params.get(key, default)
    if not isinstance(value, bool):
        raise RequestError.invalid_params({"details": f"{key} must be a boolean."})
    return value


def _dict_param(params: dict[str, Any], key: str) -> dict[str, Any]:
    value = params.get(key)
    if isinstance(value, dict):
        return value
    raise RequestError.invalid_params({"details": f"{key} must be an object."})


def _string_list_param(params: dict[str, Any], key: str) -> list[str] | None:
    value = params.get(key)
    if value is None:
        return None
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise RequestError.invalid_params({"details": f"{key} must be a list of strings."})
    return value
