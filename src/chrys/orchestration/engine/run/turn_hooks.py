# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Lifecycle hook dispatch helpers for main-agent turns."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from chrys.foundation.events.types import Error
from chrys.foundation.i18n import msg
from chrys.foundation.models.hook_confirmations import RefusedInput
from chrys.foundation.platform import safe_getcwd
from chrys.service.hooks.events import HookEvent
from chrys.service.hooks.manager import check_operation
from chrys.service.hooks.operation import HookOperation
from chrys.service.hooks.schema import HookDecision

if TYPE_CHECKING:
    from chrys.foundation.events.bus import EventBus
    from chrys.foundation.trajectory.context import TrajectoryContext
    from chrys.orchestration.engine.state.active_session import ActiveSession
    from chrys.orchestration.engine.state.current_agent import CurrentAgent
    from chrys.orchestration.engine.state.machine import EngineStateMachine
    from chrys.service.context.compaction import PreCompactInfo
    from chrys.service.hooks.manager import HookManager


logger = logging.getLogger(__name__)

_TURN_HOOKS_PROMPT_BLOCKED = msg(
    "turn_hooks.prompt_blocked",
    fallback="Prompt blocked by hook.",
)


class TurnHookDispatcher:
    """Dispatch non-prompt turn lifecycle hooks."""

    def __init__(
        self,
        *,
        session: ActiveSession,
        current: CurrentAgent,
    ) -> None:
        self._session = session
        self._current = current
        self._pending: set[asyncio.Task[None]] = set()

    async def fire_before_turn(
        self,
        user_text: str,
        *,
        is_retry: bool = False,
        target_operation_id: str | None = None,
    ) -> None:
        """Publish a ``before_turn`` hook event."""
        if self._session.hook_manager is None or not self._session.hook_manager.has_hooks_for(HookEvent.BEFORE_TURN):
            return
        profile_name = self._session.agent_profile.name if self._session.agent_profile is not None else ""
        await self._session.hook_manager.fire(
            HookEvent.BEFORE_TURN,
            {
                "session_id": self._session.session_id,
                "profile": profile_name,
                "cwd": _workspace_cwd(self._session),
                "turn": self._session.turn_number,
                "user_text": user_text,
                "is_retry": is_retry,
            },
            target_operation_id=target_operation_id,
        )

    async def fire_after_turn(self, *, failed: bool) -> None:
        """Publish an ``after_turn`` hook event."""
        if self._session.hook_manager is None or not self._session.hook_manager.has_hooks_for(HookEvent.AFTER_TURN):
            return
        profile_name = self._session.agent_profile.name if self._session.agent_profile is not None else ""
        status = (
            "failed"
            if (self._current.loaded is not None and self._current.loaded.bindings.state.run_failed)
            else (
                "interrupted"
                if (self._current.loaded is not None and self._current.loaded.bindings.state.was_interrupted)
                else "ok"
            )
        )
        await self._session.hook_manager.fire(
            HookEvent.AFTER_TURN,
            {
                "session_id": self._session.session_id,
                "profile": profile_name,
                "cwd": _workspace_cwd(self._session),
                "turn": self._session.turn_number,
                "status": status,
                "failed": failed,
                "usage": usage_payload(self._session),
            },
        )

    def schedule_user_interrupt(self) -> None:
        """Schedule observer dispatch with the interrupt's session identity."""
        manager = self._session.hook_manager
        if manager is None or not manager.has_hooks_for(HookEvent.USER_INTERRUPT):
            return
        session_id = self._session.session_id
        profile_name = self._session.agent_profile.name if self._session.agent_profile is not None else ""
        cwd = _workspace_cwd(self._session)
        task = asyncio.create_task(
            self.fire_user_interrupt(manager, session_id=session_id, profile_name=profile_name, cwd=cwd)
        )
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)
        task.add_done_callback(self.log_user_interrupt_hook_error)

    async def fire_user_interrupt(
        self,
        manager: HookManager,
        *,
        session_id: str | None,
        profile_name: str,
        cwd: str,
    ) -> None:
        """Publish an interrupt hook using only its captured manager and identity."""
        await manager.fire(
            HookEvent.USER_INTERRUPT,
            {"session_id": session_id, "profile": profile_name, "cwd": cwd},
            scope="detached",
        )

    @staticmethod
    def log_user_interrupt_hook_error(task: asyncio.Task[None]) -> None:
        """Log a failed background interrupt-hook dispatch."""
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("user_interrupt hook dispatch failed")

    async def fire_pre_compact(self, info: PreCompactInfo) -> None:
        """Callback from UnifiedContextStrategy — fire pre_compact hooks."""
        if self._session.hook_manager is None:
            return
        from chrys.service.hooks.events import HookEvent

        if not self._session.hook_manager.has_hooks_for(HookEvent.PRE_COMPACT):
            return
        profile_name = self._session.agent_profile.name if self._session.agent_profile is not None else ""
        await self._session.hook_manager.fire(
            HookEvent.PRE_COMPACT,
            {
                "session_id": self._session.session_id,
                "profile": profile_name,
                "cwd": self._session.workspace_cwd(),
                "trigger": info.trigger,
                "usage_pct": info.usage_pct,
                "tokens_before": info.tokens_before,
            },
            target_operation_id=info.trajectory_operation_id,
        )


class PromptSubmitGate:
    """Check user input with ``user_prompt_submit`` hooks and apply accepted reminders.

    Every input entry (a new turn's message, an injected message, a retry
    note) checks its input the same way: it makes the input's operation
    (:meth:`new_operation`), tracks it while checking (``TurnRuntimeState.
    input_operations``), checks it here, and commits it at the point it
    submits the input.
    """

    def __init__(
        self,
        *,
        session: ActiveSession,
        current: CurrentAgent,
        bus: EventBus,
        fsm: EngineStateMachine,
    ) -> None:
        self._session = session
        self._current = current
        self._bus = bus
        self._fsm = fsm

    def new_operation(self, text: str) -> HookOperation:
        """The operation of submitting *text* in the current session."""
        return HookOperation(subject="input", session_id=self._session.session_id, input_text=text)

    async def evaluate(
        self,
        operation: HookOperation,
        *,
        injected: bool | None,
        target_operation_id: str | None = None,
        trajectory_context: TrajectoryContext | None = None,
    ) -> HookDecision:
        """Run ``user_prompt_submit`` hooks on *operation*'s input without applying reminder side effects."""
        profile_name = self._session.agent_profile.name if self._session.agent_profile is not None else ""
        is_injected = self._fsm.is_running() if injected is None else injected
        return await check_operation(
            self._session.hook_manager,
            HookEvent.USER_PROMPT_SUBMIT,
            operation,
            lambda: {
                "session_id": self._session.session_id,
                "profile": profile_name,
                "cwd": _workspace_cwd(self._session),
                "text": operation.input_text,
                "injected": is_injected,
                "usage": usage_payload(self._session),
                "system_prompt": self.system_prompt(),
            },
            target_operation_id=target_operation_id,
            trajectory_context=trajectory_context,
        )

    def system_prompt(self) -> str:
        """The text the installed build puts at the top of every model call.

        A prompt-injection guard checks what the model will read as its own instructions.
        The order matches the model's actual call: the profile's own instructions, then
        every context provider on the live agent (memory files, skills, context management,
        documentation) in the order they run.

        Using ``manifest.instructions`` as the base would duplicate memory and skills because
        that string is already assembled from them; using ``profile.instructions`` alone would
        miss invoker-added providers. The live agent's list is the authoritative one.
        """
        loaded = self._current.loaded
        if loaded is None:
            return ""
        profile_instructions = self._current.manifest.profile_instructions
        appended = [text for p in loaded.agent.context_providers for text in [p.contributed_instructions()] if text]
        return "\n".join([text for text in (profile_instructions, *appended) if text])

    async def handle_decision(
        self,
        decision: HookDecision | None,
        *,
        refused_input: RefusedInput,
        session_id: str | None = None,
    ) -> bool:
        """Return True after publishing when a prompt-submit hook blocked.

        *refused_input* names the input the hooks checked, so the published error says
        whether it ends the request that brought it or only refuses itself.
        """
        if decision is None:
            return False
        if not decision.blocked:
            return False
        await self._bus.publish(
            Error(
                code="hook_blocked",
                message=decision.block_reason or "Prompt blocked by hook.",
                display_message=None if decision.block_reason else _TURN_HOOKS_PROMPT_BLOCKED.bind(),
                session_id=self._session.session_id if session_id is None else session_id,
                refused_input=refused_input,
            )
        )
        return True

    def queue_reminders(self, decision: HookDecision | None, *, injected: bool | None) -> None:
        """Apply non-blocking prompt-submit hook reminders after prompt validation passes."""
        if decision is None or not decision.system_reminders or self._current.loaded is None:
            return
        is_injected = self._fsm.is_running() if injected is None else injected
        self._current.loaded.reminder_middleware.queue_hook_reminders(
            decision.system_reminders,
            for_next_turn=not is_injected,
        )

    @staticmethod
    def reminder_texts(decision: HookDecision | None) -> list[str]:
        """Return non-empty prompt-hook reminders from an accepted decision."""
        if decision is None:
            return []
        return [reminder for reminder in decision.system_reminders if reminder]


def _workspace_cwd(session: ActiveSession) -> str:
    """Return the current workspace cwd, falling back only before workspace initialization."""
    if session.workspace is not None:
        return session.workspace.primary_cwd
    return safe_getcwd()


def usage_payload(session: ActiveSession) -> dict[str, Any]:
    """What a hook measures a turn's spend with: the last call's own counts, and the session's totals.

    The last call's numbers are what a context-budget hook needs (its window occupancy), the
    totals what a spend hook needs. Both are the engine's own accounting, so a hook does not
    have to re-tokenize text it was handed or keep its own running total.
    """
    meta = getattr(session, "runtime_meta", None)
    if meta is None:
        # A session that has not started accounting yet: zeros, not a crash in the hook path.
        return {
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "cache_hit_tokens": None,
            "total_session_tokens": 0,
            "total_session_input_tokens": 0,
            "total_session_output_tokens": 0,
            "total_session_cache_hit_tokens": None,
        }
    last = meta.last_usage_details or {}
    return {
        "input_tokens": int(last.get("input_token_count", 0)),
        "output_tokens": int(last.get("output_token_count", 0)),
        "total_tokens": int(last.get("total_token_count", 0)),
        "cache_hit_tokens": last.get("cache_hit_tokens"),
        "total_session_tokens": meta.total_session_tokens,
        "total_session_input_tokens": meta.total_session_input_tokens,
        "total_session_output_tokens": meta.total_session_output_tokens,
        # The one figure that survives a call reporting no cache information: the per-call
        # field above goes back to None there, and this keeps what the session has seen.
        "total_session_cache_hit_tokens": meta.total_session_cache_hit_tokens,
    }
