# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""ACP caller result and audit policies for the common SubAgentTool shell."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from chrys.foundation.events.types import (
    InvocationPaused,
)
from chrys.foundation.i18n import MessageRef
from chrys.foundation.models.failures import FailureFacts
from chrys.foundation.platform.files import atomic_write_owner_only_text
from chrys.foundation.retry import TRANSIENT_RETRY_BACKOFF_SECONDS
from chrys.foundation.trajectory.context import TrajectoryContext
from chrys.kernel import Message
from chrys.orchestration.invoker.contracts import (
    AbortCause,
    ContinuationTicket,
    Failed,
    FailureDisposition,
    InvocationOutcome,
    Ok,
    RunIntent,
    RunRequest,
    SubAgentFailureReason,
    SubAgentStatus,
)
from chrys.service.acp_client.spec import AcpAgentSpec
from chrys.service.tools.result_metadata import record_tool_success, tool_error

if TYPE_CHECKING:
    from collections.abc import Awaitable


from chrys.orchestration.invoker.acp import AcpConversation, AcpExecutionResult
from chrys.orchestration.invoker.acp_protocol import (
    AcpPermissionBroker,
    AcpUpdateTranslator,
    drain_acp_task,
)
from chrys.service.tools.result_metadata import record_tool_failure

from .shell import SubAgentToolShell

logger = logging.getLogger(__name__)


class AcpSubAgentPolicy:
    """ACP terminal metadata and pending/audit projection, without a decision loop."""

    def __init__(
        self,
        *,
        shell: SubAgentToolShell,
        prompt: str,
        spec_factory: Callable[[int], AcpAgentSpec],
        broker: AcpPermissionBroker,
        result_mode: Literal["last_segment", "transcript"] = "last_segment",
        usage_callback: Callable[..., None] | None = None,
        attempt_callback: Callable[[int, Any, AcpUpdateTranslator], Awaitable[None]] | None = None,
        translator_callback: Callable[[AcpUpdateTranslator], Awaitable[None]] | None = None,
        pause_callback: Callable[[], Awaitable[None]] | None = None,
        max_connect_retries: int = 5,
        backoff_schedule: tuple[int, ...] = TRANSIENT_RETRY_BACKOFF_SECONDS,
        persist_dir: Path | None = None,
        parent_provider_call_id: str = "",
        parent_event_call_id: str = "",
        sub_agent_log_file: str = "",
        pending_record_finalizer: Callable[[Path | None], None] | None = None,
        cancellation_finalizer: Callable[[], Awaitable[None]] | None = None,
        trajectory_context: TrajectoryContext | None = None,
        trajectory_boundary_operation_id: str | None = None,
    ) -> None:
        self._shell = shell
        invocation_id = shell.invocation_id
        tool_name = shell.tool_name
        agent_name = shell.agent_name
        event_bus = shell.bus
        origin = shell.origin
        session_id = origin.session_id or None
        self._invocation_id = invocation_id
        self.origin = origin
        self._tool_name = tool_name
        self._agent_name = agent_name
        self._prompt = prompt
        self._bus = event_bus
        self._session_id = session_id
        self._pause_callback = pause_callback
        self._persist_dir = persist_dir
        self._parent_provider_call_id = parent_provider_call_id
        self._parent_event_call_id = parent_event_call_id
        self._sub_agent_log_file = sub_agent_log_file
        self._pending_record_finalizer = pending_record_finalizer
        self._cancellation_finalizer = cancellation_finalizer
        self.backend = AcpConversation(
            tool_name=tool_name,
            agent_name=agent_name,
            prompt=prompt,
            origin=origin,
            spec_factory=spec_factory,
            broker=broker,
            event_bus=event_bus,
            terminal_projection=self._project_terminal,
            pass_started=self._begin_pass,
            session_id=session_id,
            result_mode=result_mode,
            usage_callback=usage_callback,
            attempt_callback=attempt_callback,
            translator_callback=translator_callback,
            max_connect_retries=max_connect_retries,
            backoff_schedule=backoff_schedule,
            trajectory_context=trajectory_context,
            trajectory_boundary_operation_id=trajectory_boundary_operation_id,
        )

    def _persist_path(self) -> Path | None:
        if self._persist_dir is None:
            return None
        return self._persist_dir / f"{self._invocation_id}.json"

    def _write_pending_record(self) -> None:
        path = self._persist_path()
        if path is None:
            return
        now = datetime.now(UTC).isoformat()
        payload = {
            "schema_version": 1,
            "record_type": "sub_agent_pending",
            "runner": "acp",
            "invocation_id": self._invocation_id,
            "tool_name": self._tool_name,
            "agent_name": self._agent_name,
            "prompt_preview": self.backend.prompt_preview,
            "session_id": self._session_id or "",
            "parent_call_id": self._parent_provider_call_id,
            "parent_provider_call_id": self._parent_provider_call_id,
            "parent_event_call_id": self._parent_event_call_id,
            "sub_agent_log_file": self._sub_agent_log_file,
            "created_at": now,
            "paused_at": now,
            "failure_reason": SubAgentFailureReason.ACP_TRANSPORT,
            "last_error": self.backend.last_error,
            "retry_attempts_total": self.backend.retry_attempts,
        }
        try:
            atomic_write_owner_only_text(
                path,
                json.dumps(payload, indent=2, ensure_ascii=False, allow_nan=False),
            )
        except OSError as exc:
            # Kernel parity: a failed pause-record write must degrade the
            # restore experience, not crash the pause flow (and an OSError
            # here may embed a filesystem path the model must never see).
            logger.warning("Failed to persist paused ACP sub-agent %s: %s", self._invocation_id, exc)

    def _project_terminal(self, result: AcpExecutionResult) -> None:
        self._shell.set_status(SubAgentStatus.COMPLETED)
        if result.succeeded:
            record_tool_success()
        else:
            record_tool_failure(result.error_kind, result.text)

    def _begin_pass(self) -> None:
        self._shell.set_status(SubAgentStatus.RUNNING)

    def request(self, ticket: ContinuationTicket | None) -> RunRequest:
        return RunRequest(
            [Message("user", [self._prompt])],
            RunIntent.RETRY if ticket is not None else RunIntent.FRESH,
            self.origin,
            ticket,
        )

    async def project_result(self, outcome: InvocationOutcome) -> str | None:
        if isinstance(outcome, Ok):
            return "".join(content.text or "" for content in outcome.segments)
        if isinstance(outcome, Failed) and outcome.disposition is FailureDisposition.TERMINAL:
            return outcome.error
        return None

    async def prepare_pause(self) -> None:
        pass

    async def record_pause(self) -> None:
        self._write_pending_record()
        if self._pause_callback is not None:
            try:
                await self._pause_callback()
            except Exception:
                logger.warning(
                    "Failed to record paused status for ACP sub-agent %s", self._invocation_id, exc_info=True
                )

    def pause_event(self) -> InvocationPaused:
        return InvocationPaused(
            origin=self.origin,
            agent_name=self._agent_name,
            tool_name=self._tool_name,
            reason=SubAgentFailureReason.ACP_TRANSPORT,
            last_error=self.backend.last_error,
            retry_attempts=self.backend.retry_attempts,
            diagnostic_path=self.backend.diagnostic_path,
            session_id=self._session_id,
        )

    def resolve_late_cascade(self, decision: asyncio.Future[str]) -> None:
        if self._shell.cascade_requested and not decision.done():
            decision.set_result("cascade_abort")

    def check_terminal_race(self) -> None:
        if self._shell.cascade_requested:
            raise asyncio.CancelledError

    def prepare_retry(self) -> tuple[str, ...]:
        # The backend ticket authorizes a new session from the prompt: the failed pass's final answer goes.
        return self.backend.withdraw_final_answer()

    @property
    def last_error(self) -> str:
        return self.backend.last_error

    # The transport's failure is told by its pause reason; the backend keeps only its text.
    @property
    def last_error_display(self) -> MessageRef | None:
        return None

    @property
    def last_error_hint(self) -> MessageRef | None:
        return None

    @property
    def last_error_facts(self) -> FailureFacts | None:
        return None

    async def abort_result(self, *, by_user: bool) -> str:
        details = {"tool_name": self._tool_name, "invocation_id": self._invocation_id}
        if by_user:
            return tool_error(
                "sub_agent_aborted",
                f"sub-agent '{self._tool_name}' aborted by user after failure — {self.backend.last_error}",
                details=details,
            )
        return tool_error(
            "sub_agent_failed", f"sub-agent '{self._tool_name}' failed — {self.backend.last_error}", details=details
        )

    def latch_abort(self, cause: AbortCause) -> None:
        self.backend.latch_abort(cause)

    async def cancel_active(self) -> None:
        await self.backend.cancel_transport(
            self._shell.owner_close_cause or AbortCause.CASCADE,
            after_permissions=self._shell.schedule_cascade_event,
        )

    async def finalize_cancellation(self) -> None:
        if self._shell.cascade_requested:
            await self._shell.await_cascade_publish(drain_acp_task)
        elif self._shell.status not in {SubAgentStatus.COMPLETED, SubAgentStatus.ABORTED}:
            self._shell.set_status(SubAgentStatus.ABORTED)
        await self.backend.drain_cancellation()
        if self._cancellation_finalizer is not None:
            await self._cancellation_finalizer()

    async def before_cascade_event(self) -> None:
        pass

    async def run_cancelled(self) -> None:
        if self._shell.cascade_requested:
            self._shell.set_status(SubAgentStatus.CASCADE_ABORTED)
            await self._shell.await_cascade_publish(drain_acp_task)

    async def finish_run(self) -> None:
        await self.backend.finish_transport_watchdog()
        if self._shell.status != SubAgentStatus.PAUSED and self._pending_record_finalizer is not None:
            self._pending_record_finalizer(self._persist_path())
