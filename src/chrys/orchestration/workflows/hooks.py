# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Workflow hook lifetimes: session attachments outlive individual run resources.

Browsing history is passive. First admitted execution attaches a session (start
for a new session, restored for an existing one); later runs may reload hook
configuration without synthesizing another session lifetime. Host shutdown or
explicit session release ends the attachment. Run hooks are observers, once per
admitted run, including cancellation and failure.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from chrys.foundation.trajectory.context import trajectory_scope
from chrys.foundation.trajectory.event_types import RuntimeFinishReason
from chrys.foundation.util.once_close import finish_close
from chrys.service.hooks.events import HookEvent
from chrys.service.trajectory.session import SessionStartInfo, SessionTrajectory

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from pathlib import Path

    from chrys.orchestration.workflows.session import WorkflowSessionOwner
    from chrys.service.hooks.manager import HookManager
    from chrys.service.state.locks import ActiveSessionGuard


@dataclass
class _Attachment:
    manager: HookManager | None
    payload: dict[str, Any]
    recorder: SessionTrajectory
    guard: ActiveSessionGuard
    write_lock_path: Path | None
    recovery: asyncio.Task[int] | None = None

    def new_recorder(self) -> SessionTrajectory:
        return SessionTrajectory(
            session_id=self.recorder.session_id,
            session_dir=self.recorder.session_dir,
            write_lock_path=self.write_lock_path,
            session_start_info=lambda: SessionStartInfo(self.payload["cwd"], "", ""),
        )

    async def settle_recovery(self, *, cancel: bool = False) -> None:
        task = self.recovery
        if task is None:
            return
        if cancel and not task.done():
            task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            if not cancel:
                raise
        except Exception:
            logger.exception("Workflow hook outbox recovery failed")
        finally:
            self.recovery = None


class WorkflowSessionHooks:
    def __init__(self) -> None:
        self._sessions: dict[str, _Attachment] = {}

    async def attach(self, resources: WorkflowSessionOwner) -> None:
        session = resources.session
        if not session.session_id or not session.workspace or (not resources.identity):
            raise RuntimeError("Attaching workflow hooks requires a session id, workspace and workflow identity.")
        manager = session.hook_manager
        recorder = resources.trajectory
        if recorder is None:
            raise RuntimeError("Attaching workflow hooks requires a trajectory recorder.")
        context = recorder.context()
        if manager is not None:
            manager.trajectory_context_provider = lambda: context
        payload = {
            "session_id": session.session_id,
            "cwd": session.workspace.primary_cwd,
            "profile": "",
            "session_kind": "workflow",
            "workflow_id": resources.identity.workflow_id,
        }
        previous = self._sessions.get(session.session_id)
        # Transfer ownership before the first suspension, so cancellation during
        # attachment still leaves a valid owner for run-end and session-end.
        attachment = _Attachment(
            manager, payload, recorder, session.guard, session.session_write_lock_path(session.session_id)
        )
        self._sessions[session.session_id] = attachment
        resources.hooks_transferred = True
        if previous is not None:
            # Recovery belongs to the attachment, not the per-run manager.
            # Finish dispatch before retiring the manager that owns it.
            try:
                await previous.settle_recovery()
            finally:
                if previous.manager is not None:
                    await finish_close(asyncio.create_task(previous.manager.drain_session()))
        if manager is not None and (previous is None or previous.manager is None):
            with trajectory_scope(context):
                attachment.recovery = asyncio.create_task(manager.recover_outbox())
        if previous is not None:
            return
        if manager is not None:
            with trajectory_scope(context):
                if resources.restoring:
                    # ``session_restored`` alone says the session came back; firing
                    # ``session_start`` too would change what an existing hook sees
                    # on a restore.
                    await manager.fire(
                        HookEvent.SESSION_RESTORED,
                        {**payload, "restored_session_id": session.session_id},
                        scope="session",
                    )
                else:
                    await manager.fire(HookEvent.SESSION_START, payload, scope="session")

    async def run_event(self, session_id: str, event: HookEvent, *, run_id: str, **fields: Any) -> None:
        attachment = self._sessions[session_id]
        if attachment.manager is not None:
            with trajectory_scope(attachment.recorder.context(run_operation_id=run_id)):
                await attachment.manager.fire(
                    event, {**attachment.payload, "run_id": run_id, **fields}, scope="session"
                )
                await attachment.manager.drain_session(close=False)

    async def release(self, session_id: str) -> None:
        attachment = self._sessions.pop(session_id, None)
        if attachment is None or attachment.manager is None:
            return
        await finish_close(asyncio.create_task(self._end_session(attachment)))

    async def _end_session(self, attachment: _Attachment) -> None:
        """Idle end hooks borrow the same session guard before opening a fresh writer."""
        manager = attachment.manager
        if manager is None:
            raise RuntimeError("Ending an attached hook session requires its hook manager.")
        session_id = attachment.recorder.session_id
        recorder = None
        acquired = False
        manager.trajectory_context_provider = None
        try:
            await attachment.settle_recovery(cancel=True)
            if attachment.recorder.is_closed:
                try:
                    lock = await asyncio.to_thread(attachment.guard.acquire_for_restore, session_id, timeout=0)
                except TimeoutError:
                    logger.warning("Session-end hook trajectory unavailable: workflow session %s is in use", session_id)
                else:
                    attachment.guard.install(session_id, lock)
                    acquired = True
                    directory = attachment.recorder.session_dir
                    # SessionDeleted also releases attachments. Never recreate
                    # the deleted directory just to record its end hooks.
                    if directory is not None and directory.is_dir():
                        recorder = attachment.new_recorder()
            else:
                recorder = attachment.recorder
            context = recorder.context() if recorder is not None else None
            with trajectory_scope(context):
                await manager.fire(HookEvent.SESSION_END, attachment.payload, scope="session")
        finally:
            try:
                await manager.drain_session()
            finally:
                try:
                    if acquired and recorder is not None:
                        await recorder.close(reason=RuntimeFinishReason.GRACEFUL_SHUTDOWN)
                finally:
                    if acquired:
                        attachment.guard.release()

    async def close(self) -> None:
        for session_id in tuple(self._sessions):
            await self.release(session_id)
