# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Rollback modal and rollback-result handling for the main screen."""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from chrys.app.tui.i18n import render_str
from chrys.app.tui.screens.diff.rollback_modal import (
    EXCLUSION_PREVIEW_ITEM,
    ROLLBACK_TITLE,
    RollbackLoadCancelled,
    exclusion_reason_label,
)
from chrys.app.tui.screens.main.engine_read_model import EngineReadModel, EngineSnapshotSource, RollbackReadState
from chrys.app.tui.screens.main.state import MainScreenServices
from chrys.app.tui.support.gc_freeze import GcReclaimReason, GcReclaimRequested
from chrys.foundation.events.types import RollbackResult, UserRollback
from chrys.foundation.i18n import DisplayPath, MessageRef, msg
from chrys.foundation.i18n.formatting import format_message
from chrys.foundation.platform import safe_getcwd
from chrys.foundation.util.async_tasks import await_task_quiescence

if TYPE_CHECKING:
    from chrys.app.tui.i18n import LocaleController
    from chrys.app.tui.screens.main.ports import RollbackView

logger = logging.getLogger(__name__)

_AGENT_RUNNING = msg(
    "tui.rollback.agent_running",
    fallback="Cannot roll back while the agent is running.",
)
_UNAVAILABLE = msg("tui.rollback.unavailable", fallback="Rollback is not available.")
_TARGET_NONNEGATIVE = msg(
    "tui.rollback.target_nonnegative",
    fallback="The target turn must be zero or greater.",
)
_TURN_COUNT_POSITIVE = msg(
    "tui.rollback.turn_count_positive",
    fallback="The number of turns to roll back must be positive.",
)
_USAGE = msg(
    "tui.rollback.usage",
    fallback="Usage: /rollback, /rollback N, or /rollback to N.",
)
_PICKER_CONVERSATION_CHANGED = msg(
    "tui.rollback.picker_conversation_changed",
    fallback="Rollback picker cancelled because the conversation changed.",
)
_PICKER_RUNTIME_CHANGED = msg(
    "tui.rollback.picker_runtime_changed",
    fallback="Rollback picker cancelled because the workspace or runtime changed.",
)
_ACTIVE_SESSION_CHANGED = msg(
    "tui.rollback.active_session_changed",
    fallback="Rollback cancelled because the active session changed.",
)
_ROLLED_BACK_SESSION_START = msg(
    "tui.rollback.result.session_start",
    fallback="Rolled back to Session start.",
)
_ROLLED_BACK_TURN = msg(
    "tui.rollback.result.turn",
    fallback="Rolled back to Turn {turn}.",
)
_FILES_RESTORED = msg(
    "tui.rollback.result.files_restored",
    fallback="{count} file restored",
    plural_fallback="{count} files restored",
)
_ALREADY_MATCHED = msg("tui.rollback.result.already_matched", fallback="{matched_count} already matched")
_FAILED_COUNT = msg("tui.rollback.result.failed_count", fallback="{failed_count} failed")
_FAILED_PREVIEW = msg("tui.rollback.result.failed_preview", fallback="Failed: {preview}{suffix}")
_FILES_EXCLUDED = msg(
    "tui.rollback.result.files_excluded",
    fallback="{count} file excluded from revert: {preview}{suffix}",
    plural_fallback="{count} files excluded from revert: {preview}{suffix}",
)
_FILES_REVERTED = msg(
    "tui.rollback.result.files_reverted",
    fallback="{count} file reverted",
    plural_fallback="{count} files reverted",
)
_WARNINGS_COUNT = msg(
    "tui.rollback.result.warnings_count",
    fallback="{count} warning",
    plural_fallback="{count} warnings",
)
_ERROR_REASON = msg("tui.rollback.result.error_reason", fallback="error")
_MORE_SUFFIX = msg("tui.rollback.result.more_suffix", fallback=" (+{extra_count} more)")
_TARGET_SESSION_START = msg("tui.rollback.debug_target.session_start", fallback="Session start")
_TARGET_TURN = msg("tui.rollback.debug_target.turn", fallback="Turn {turn}")


@dataclass(frozen=True, slots=True)
class _RollbackRequestOwner:
    """Session identity that owns one deferred rollback request."""

    session_id: str | None
    session_generation: int


@dataclass(frozen=True, slots=True)
class _RollbackCommand:
    """Parsed TUI rollback selector."""

    target_turn: int | None = None
    relative_turns: int | None = None


class RollbackController:
    """Coordinate rollback modal actions and rollback result presentation."""

    def __init__(
        self,
        *,
        services: MainScreenServices,
        view: RollbackView,
        workspace_cwd: Callable[[], str],
        is_agent_busy: Callable[[], bool],
        current_session_id: Callable[[], str],
        session_generation: Callable[[], int],
        turn_lifecycle_task: Callable[[], asyncio.Task[None] | None],
        profile_name: Callable[[], str],
        reset_welcome_workspace_marker: Callable[[str], None],
        set_has_messages: Callable[[bool], None],
        post_gc_message: Callable[[GcReclaimRequested], object],
        debug: Callable[[str, str], None],
        refresh_diff_preview: Callable[[], None] = lambda: None,
        locale_controller: LocaleController | None = None,
        confirm_stopping_background_tasks: Callable[[int], Awaitable[bool]] | None = None,
    ) -> None:
        self._refresh_diff_preview = refresh_diff_preview
        self._services = services
        self._view = view
        self._workspace_cwd = workspace_cwd
        self._is_agent_busy = is_agent_busy
        self._current_session_id = current_session_id
        self._session_generation = session_generation
        self._turn_lifecycle_task = turn_lifecycle_task
        self._profile_name = profile_name
        self._reset_welcome_workspace_marker = reset_welcome_workspace_marker
        self._set_has_messages = set_has_messages
        self._post_gc_message = post_gc_message
        self._debug = debug
        self._locale_controller = locale_controller
        self._confirm_stopping_background_tasks = confirm_stopping_background_tasks
        """Whether the user lets the background commands that the turns after a turn started stop."""

    def _render_message(self, reference: MessageRef) -> str:
        controller = self._locale_controller
        return format_message(reference) if controller is None else render_str(controller.localizer, reference)

    def show_rollback(self, arg: str = "") -> None:
        """Open the rollback modal or dispatch a direct rollback shortcut."""
        if self._is_agent_busy():
            self._view.notify(
                _AGENT_RUNNING.bind(),
                title=ROLLBACK_TITLE.bind(),
                severity="warning",
                timeout=3,
            )
            return

        engine_provider = self._services.engine_provider
        engine = engine_provider() if engine_provider is not None else None
        if engine is None:
            self._view.notify(_UNAVAILABLE.bind(), title=ROLLBACK_TITLE.bind(), severity="warning", timeout=3)
            return

        # Open the loading shell immediately. The modal owns attribution
        # refresh and preview construction after its first painted frame.
        self._open_rollback_ui(engine, arg)

    def _open_rollback_ui(self, engine: EngineSnapshotSource, arg: str) -> None:
        """Push the modal before resolving session-sized rollback state."""
        command = self._parse_command(arg)
        if command is None:
            return
        request_owner = self._request_owner()
        if command.target_turn is not None or command.relative_turns is not None:
            target_turn = (
                command.target_turn
                if command.target_turn is not None
                else EngineReadModel(engine).current_turn_number - (command.relative_turns or 0)
            )
            self._view.open_rollback_progress_modal(
                lambda: self._publish_direct_rollback(
                    command,
                    request_owner,
                    target_turn,
                )
            )
            return

        read_model = EngineReadModel(engine)
        captured_lifecycle_task = self._turn_lifecycle_task()
        cwd = self._workspace_cwd()
        projected_current_turn: int | None = None
        projected_conversation_revision: int | None = None
        projected_build_generation: int | None = None
        projected_workspace_cwd: str | None = None

        async def _load_state() -> RollbackReadState | None:
            nonlocal projected_build_generation, projected_conversation_revision
            nonlocal projected_current_turn, projected_workspace_cwd
            state = await self._load_picker_state(
                read_model,
                request_owner,
                captured_lifecycle_task,
            )
            projected_current_turn = state.current_turn if state is not None else None
            projected_conversation_revision = state.conversation_revision if state is not None else None
            projected_build_generation = state.build_generation if state is not None else None
            projected_workspace_cwd = state.workspace_cwd if state is not None else None
            return state

        def _on_dismiss(choice: tuple[int, bool, list[str] | None] | None) -> None:
            if choice is None:
                return
            if (
                projected_current_turn is None
                or projected_conversation_revision is None
                or projected_build_generation is None
                or projected_workspace_cwd is None
            ):
                self._notify_picker_conversation_changed()
                return
            target_turn, revert_changes, selected_paths = choice
            event = UserRollback(
                target_turn=target_turn,
                expected_current_turn=projected_current_turn,
                expected_conversation_revision=projected_conversation_revision,
                expected_build_generation=projected_build_generation,
                expected_workspace_cwd=projected_workspace_cwd,
                revert_changes=revert_changes,
                selected_paths=selected_paths,
                session_id=request_owner.session_id,
            )

            async def _publish_confirmed_rollback() -> None:
                if not await self._may_stop_background_tasks(target_turn):
                    return
                await await_task_quiescence(captured_lifecycle_task)
                if self._cancel_if_session_changed(request_owner) or self._cancel_if_agent_busy():
                    return
                await self._services.bus.publish(event, raise_handler_errors=True)

            self._view.open_rollback_progress_modal(_publish_confirmed_rollback)

        self._view.open_rollback_modal(
            cwd=cwd,
            load_state=_load_state,
            on_result=_on_dismiss,
        )

    def _parse_command(self, arg: str) -> _RollbackCommand | None:
        """Parse relative ``N`` and absolute ``to N`` rollback selectors."""
        tokens = arg.split()
        if not tokens:
            return _RollbackCommand(target_turn=None)

        absolute = tokens[0].lower() == "to"
        number_index = 1 if absolute else 0
        expected_tokens = 2 if absolute else 1
        if len(tokens) != expected_tokens:
            self._notify_invalid_command()
            return None
        try:
            value = int(tokens[number_index])
        except ValueError:
            self._notify_invalid_command()
            return None

        if absolute:
            if value < 0:
                self._view.notify(
                    _TARGET_NONNEGATIVE.bind(),
                    title=ROLLBACK_TITLE.bind(),
                    severity="warning",
                    timeout=3,
                )
                return None
            return _RollbackCommand(target_turn=value)
        if value <= 0:
            self._view.notify(
                _TURN_COUNT_POSITIVE.bind(),
                title=ROLLBACK_TITLE.bind(),
                severity="warning",
                timeout=3,
            )
            return None
        return _RollbackCommand(relative_turns=value)

    def _notify_invalid_command(self) -> None:
        self._view.notify(
            _USAGE.bind(),
            title=ROLLBACK_TITLE.bind(),
            severity="warning",
            timeout=4,
        )

    async def _publish_direct_rollback(
        self,
        command: _RollbackCommand,
        request_owner: _RollbackRequestOwner,
        target_turn: int,
    ) -> None:
        """Dispatch a direct selector so the backend can fence and validate it."""
        if not await self._may_stop_background_tasks(target_turn):
            return
        if self._cancel_if_session_changed(request_owner) or self._cancel_if_agent_busy():
            return
        await self._services.bus.publish(
            UserRollback(
                target_turn=command.target_turn or 0,
                relative_turns=command.relative_turns,
                revert_changes=True,
                session_id=request_owner.session_id,
            ),
            raise_handler_errors=True,
        )

    async def _may_stop_background_tasks(self, target_turn: int) -> bool:
        confirm = self._confirm_stopping_background_tasks
        return confirm is None or await confirm(target_turn)

    async def _load_picker_state(
        self,
        read_model: EngineReadModel,
        request_owner: _RollbackRequestOwner,
        captured_lifecycle_task: asyncio.Task[None] | None,
    ) -> RollbackReadState | None:
        """Project picker state only after the opening turn is durably quiescent."""
        await await_task_quiescence(captured_lifecycle_task)
        projection_owner = await self._acquire_picker_projection(
            read_model,
            request_owner,
        )
        release_deferred = False
        try:
            expected_turn = read_model.current_turn_number
            expected_revision = read_model.conversation_revision
            expected_build_generation = read_model.build_generation
            expected_workspace_cwd = read_model.workspace_cwd
            projection_lifecycle_task = self._turn_lifecycle_task()
            projection_task = asyncio.create_task(read_model.load_rollback_state())
            try:
                state = await asyncio.shield(projection_task)
            except asyncio.CancelledError:
                release_deferred = True

                def _finish_background_projection(task: asyncio.Task[RollbackReadState | None]) -> None:
                    try:
                        task.result()
                    except asyncio.CancelledError:
                        pass
                    except Exception:
                        logger.debug("Cancelled rollback state projection failed", exc_info=True)
                    finally:
                        read_model.finish_rollback_projection(projection_owner)

                projection_task.add_done_callback(_finish_background_projection)
                raise
            if self._cancel_if_session_changed(request_owner) or self._cancel_if_agent_busy():
                raise RollbackLoadCancelled
            if (
                read_model.current_turn_number != expected_turn
                or read_model.conversation_revision != expected_revision
                or self._turn_lifecycle_task() is not projection_lifecycle_task
            ):
                self._notify_picker_conversation_changed()
                raise RollbackLoadCancelled
            if (
                read_model.build_generation != expected_build_generation
                or read_model.workspace_cwd != expected_workspace_cwd
            ):
                self._notify_picker_runtime_changed()
                raise RollbackLoadCancelled
            if state is None:
                return None
            projected_turn = state.current_turn
            projected_revision = state.conversation_revision
            projected_build_generation = state.build_generation
            projected_workspace_cwd = state.workspace_cwd

            async def _acquire_preview_projection() -> str:
                return await self._acquire_picker_projection(
                    read_model,
                    request_owner,
                    expected_turn=projected_turn,
                    expected_revision=projected_revision,
                    expected_build_generation=projected_build_generation,
                    expected_workspace_cwd=projected_workspace_cwd,
                )

            return replace(
                state,
                projection_acquire=_acquire_preview_projection,
                projection_release=read_model.finish_rollback_projection,
            )
        finally:
            if not release_deferred:
                read_model.finish_rollback_projection(projection_owner)

    async def _acquire_picker_projection(
        self,
        read_model: EngineReadModel,
        request_owner: _RollbackRequestOwner,
        *,
        expected_turn: int | None = None,
        expected_revision: int | None = None,
        expected_build_generation: int | None = None,
        expected_workspace_cwd: str | None = None,
    ) -> str:
        """Fence prompt admission and accept only the picker owner's revision."""
        if self._cancel_if_session_changed(request_owner) or self._cancel_if_agent_busy():
            raise RollbackLoadCancelled
        projection_owner = await read_model.begin_rollback_projection(
            session_id=request_owner.session_id,
            session_generation=request_owner.session_generation,
        )
        if projection_owner is None:
            if not self._cancel_if_session_changed(request_owner):
                self._notify_picker_conversation_changed()
            raise RollbackLoadCancelled

        accepted = False
        try:
            if self._cancel_if_session_changed(request_owner) or self._cancel_if_agent_busy():
                raise RollbackLoadCancelled
            lifecycle_task = self._turn_lifecycle_task()
            if lifecycle_task is not None and not lifecycle_task.done():
                self._notify_picker_conversation_changed()
                raise RollbackLoadCancelled
            if expected_turn is not None and read_model.current_turn_number != expected_turn:
                self._notify_picker_conversation_changed()
                raise RollbackLoadCancelled
            if expected_revision is not None and read_model.conversation_revision != expected_revision:
                self._notify_picker_conversation_changed()
                raise RollbackLoadCancelled
            if expected_build_generation is not None and read_model.build_generation != expected_build_generation:
                self._notify_picker_runtime_changed()
                raise RollbackLoadCancelled
            if expected_workspace_cwd is not None and read_model.workspace_cwd != expected_workspace_cwd:
                self._notify_picker_runtime_changed()
                raise RollbackLoadCancelled
            accepted = True
            return projection_owner
        finally:
            if not accepted:
                read_model.finish_rollback_projection(projection_owner)

    def _notify_picker_conversation_changed(self) -> None:
        """Explain why an in-flight picker projection was discarded."""
        self._view.notify(
            _PICKER_CONVERSATION_CHANGED.bind(),
            title=ROLLBACK_TITLE.bind(),
            severity="warning",
            timeout=3,
        )

    def _notify_picker_runtime_changed(self) -> None:
        """Explain why a picker from an older build/workspace was discarded."""
        self._view.notify(
            _PICKER_RUNTIME_CHANGED.bind(),
            title=ROLLBACK_TITLE.bind(),
            severity="warning",
            timeout=3,
        )

    def _request_owner(self) -> _RollbackRequestOwner:
        """Capture the active session without yielding between identity fields."""
        return _RollbackRequestOwner(
            session_id=self._current_session_id() or None,
            session_generation=self._session_generation(),
        )

    def _cancel_if_session_changed(self, request_owner: _RollbackRequestOwner) -> bool:
        """Cancel a deferred request that no longer belongs to the active session."""
        current_owner = self._request_owner()
        if current_owner.session_generation == request_owner.session_generation and (
            request_owner.session_id is None or current_owner.session_id == request_owner.session_id
        ):
            return False
        self._view.notify(
            _ACTIVE_SESSION_CHANGED.bind(),
            title=ROLLBACK_TITLE.bind(),
            severity="warning",
            timeout=3,
        )
        return True

    def _cancel_if_agent_busy(self) -> bool:
        """Recheck the preflight busy guard immediately before dispatch."""
        if not self._is_agent_busy():
            return False
        self._view.notify(
            _AGENT_RUNNING.bind(),
            title=ROLLBACK_TITLE.bind(),
            severity="warning",
            timeout=3,
        )
        return True

    async def on_result(self, event: RollbackResult) -> None:
        """Surface rollback outcomes, refresh welcome state, and restore rolled-back input."""
        self._refresh_diff_preview()
        is_welcome = event.target_turn == 0
        if is_welcome:
            cwd = self._workspace_cwd() or safe_getcwd()
            self._set_has_messages(False)
            self._reset_welcome_workspace_marker(cwd)
            await self._view.restore_welcome_rollback(
                session_id=event.session_id,
                profile=self._profile_name(),
                cwd=cwd,
            )
            self._post_gc_message(GcReclaimRequested(GcReclaimReason.ROLLBACK_WELCOME, prompt=True))
        if event.rolled_back_user_text:
            self._view.restore_input_text(event.rolled_back_user_text)

        target_desc = self._render_message(
            _TARGET_SESSION_START.bind() if is_welcome else _TARGET_TURN.bind(turn=event.target_turn)
        )
        headline = _ROLLED_BACK_SESSION_START.bind() if is_welcome else _ROLLED_BACK_TURN.bind(turn=event.target_turn)
        lines = [self._render_message(headline)]
        severity = "information"
        failed_count = 0
        if event.restore_results:
            results = event.restore_results
            applied = event.files_reverted
            noop = sum(1 for result in results if result.ok and not result.changed)
            failed = [result for result in results if not result.ok]
            failed_count = len(failed)
            parts: list[str] = [self._render_message(_FILES_RESTORED.bind(count=applied))]
            if noop:
                parts.append(self._render_message(_ALREADY_MATCHED.bind(matched_count=noop)))
            if failed:
                parts.append(self._render_message(_FAILED_COUNT.bind(failed_count=failed_count)))
                severity = "warning"
            lines.append(", ".join(parts) + ".")
            if failed:
                preview = "; ".join(
                    f"{os.path.basename(result.path)}: {result.reason or self._render_message(_ERROR_REASON.bind())}"
                    for result in failed[:3]
                )
                suffix = (
                    "" if len(failed) <= 3 else self._render_message(_MORE_SUFFIX.bind(extra_count=len(failed) - 3))
                )
                lines.append(self._render_message(_FAILED_PREVIEW.bind(preview=preview, suffix=suffix)))
        # Exclusions/warnings surface independently of restore_results:
        # a plan whose every file is excluded restores nothing, so the
        # terminal result must still explain that outcome.
        if event.exclusions:
            severity = "warning"
            excluded = event.exclusions
            preview_parts: list[str] = []
            for path, reason in excluded[:3]:
                label = exclusion_reason_label(reason)
                rendered_label = self._render_message(label) if isinstance(label, MessageRef) else label
                preview_parts.append(
                    self._render_message(
                        EXCLUSION_PREVIEW_ITEM.bind(
                            path=DisplayPath(os.path.basename(path)),
                            reason=rendered_label,
                        )
                    )
                )
            preview = "; ".join(preview_parts)
            suffix = (
                "" if len(excluded) <= 3 else self._render_message(_MORE_SUFFIX.bind(extra_count=len(excluded) - 3))
            )
            lines.append(
                self._render_message(
                    _FILES_EXCLUDED.bind(
                        count=len(excluded),
                        preview=preview,
                        suffix=suffix,
                    )
                )
            )
        if event.warnings:
            severity = "warning"
            warning_line = " ".join(event.warnings[:2])
            if len(event.warnings) > 2:
                warning_line += self._render_message(_MORE_SUFFIX.bind(extra_count=len(event.warnings) - 2))
            lines.append(warning_line)
        self._view.notify("\n".join(lines), title=ROLLBACK_TITLE.bind(), severity=severity, timeout=4)

        detail = target_desc
        if event.restore_results:
            detail += ", " + format_message(_FILES_REVERTED.bind(count=event.files_reverted))
            if failed_count:
                detail += f", {failed_count} failed"
        if event.exclusions:
            detail += f", {len(event.exclusions)} excluded"
        if event.warnings:
            detail += ", " + format_message(_WARNINGS_COUNT.bind(count=len(event.warnings)))
        self._debug("RollbackResult", detail)
