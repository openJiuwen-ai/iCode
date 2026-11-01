# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Adapter-backed presentation facade for backend handlers."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from chrys.app.tui.screens.main.ports import (
    InputLabel,
    InputRestore,
    MainScreenPresentationView,
    StatusMessage,
    StatusTrail,
)

if TYPE_CHECKING:
    from chrys.app.tui.screens.main.state import MainScreenState
    from chrys.app.tui.widgets.chrome.input_bar import ChatDraft
    from chrys.app.tui.widgets.sidebar.context import ContextUsageState
    from chrys.foundation.models.background_tasks import BackgroundTaskView
    from chrys.foundation.models.hook_executions import HookExecutionView
    from chrys.foundation.models.invocations import ContentChannel
    from chrys.foundation.models.todos import TodoItem
    from chrys.service.approval.policy import ApprovalMode


class MainScreenPresenter:
    """Facade exposing backend/session presentation methods."""

    def __init__(self, view: MainScreenPresentationView, state: MainScreenState | None = None) -> None:
        self._view = view
        self._state = state

    def chat_session_id(self) -> str:
        return self._view.chat_session_id()

    def set_tool_info(self, trail: StatusTrail) -> None:
        self._view.set_tool_info(trail)

    def show_status(self, text: StatusMessage) -> None:
        self._view.show_status(text)

    def note_output_text(self, text: str) -> None:
        self._view.note_output_text(text)

    def clear_status(self) -> None:
        self._view.clear_status()

    def flash_status(self, *args: Any, **kwargs: Any) -> None:
        self._view.flash_status(*args, **kwargs)

    def flash_turn_complete(self) -> None:
        self._view.flash_turn_complete()

    def mark_terminal_title_completed(self) -> None:
        self._view.mark_terminal_title_completed()

    def mark_terminal_title_failed(self) -> None:
        self._view.mark_terminal_title_failed()

    def start_tool_status(self, tool_name: str) -> None:
        self._view.start_tool_status(tool_name)

    def set_chat_profile(self, profile: str) -> None:
        self._view.set_chat_profile(profile)

    def set_chat_tool_kinds(self, tool_kinds: dict[str, str]) -> None:
        self._view.set_chat_tool_kinds(tool_kinds)

    async def clear_chat(self) -> None:
        await self._view.clear_chat()

    def set_chat_workspace_cwd(self, cwd: str) -> None:
        self._view.set_chat_workspace_cwd(cwd)

    def update_welcome(self, *, profile: str = "", cwd: str = "") -> None:
        self._view.update_welcome(profile=profile, cwd=cwd)

    def set_chat_session_id(self, session_id: str) -> None:
        self._view.set_chat_session_id(session_id)

    def set_session_title_state(
        self,
        *,
        custom: str | None = None,
        generated: str | None = None,
        fallback: str | None = None,
    ) -> None:
        self._view.set_session_title_state(custom=custom, generated=generated, fallback=fallback)

    def reset_session_title_state(self) -> None:
        self._view.reset_session_title_state()

    def session_custom_title(self) -> str:
        return self._view.session_custom_title()

    def set_context_usage_state(self, state: ContextUsageState | None) -> None:
        self._view.set_context_usage_state(state)

    def set_todo_state(self, items: tuple[TodoItem, ...]) -> None:
        self._view.set_todo_state(items)

    def clear_todos(self) -> None:
        self._view.clear_todos()

    def set_background_tasks(self, tasks: tuple[BackgroundTaskView, ...]) -> None:
        self._view.set_background_tasks(tasks)

    def set_hook_executions(self, executions: tuple[HookExecutionView, ...]) -> None:
        self._view.set_hook_executions(executions)

    def hook_runs_changed(self) -> None:
        self._view.hook_runs_changed()

    def set_header_approval_mode(self, mode: ApprovalMode) -> None:
        self._view.set_header_approval_mode(mode)

    def set_terminal_title_for_cwd(self, cwd: str | None = None) -> None:
        self._view.set_terminal_title_for_cwd(cwd)

    def set_terminal_title_for_user_message(self, text: str) -> None:
        self._view.set_terminal_title_for_user_message(text)

    def set_status_profile(self, profile: str, *, description: str = "") -> None:
        self._view.set_status_profile(profile, description=description)

    def set_input_clipboard_dir(self, session_id: str | None) -> None:
        self._view.set_input_clipboard_dir(session_id)

    def set_input_retry_mode(self, value: bool) -> None:
        self._view.set_input_retry_mode(value)

    def set_input_retry(self, label: InputLabel, value: bool = True) -> None:
        self._view.set_input_retry(label, value)

    def set_input_paste_cwd(self, cwd: str) -> None:
        self._view.set_input_paste_cwd(cwd)

    def restore_input_text(self, draft: str | ChatDraft) -> InputRestore | None:
        return self._view.restore_input_text(draft)

    def rewrite_restored_input(self, restore: InputRestore | None, text: str) -> bool:
        return self._view.rewrite_restored_input(restore, text)

    def unlock_input_keep_if_locked(self) -> None:
        self._view.unlock_input_keep_if_locked()

    async def change_shell_directory(self, cwd: str) -> None:
        await self._view.change_shell_directory(cwd)

    def reset_context_usage(self, max_context_tokens: int = 0) -> None:
        self._view.reset_context_usage(max_context_tokens)

    def clear_compressed_blocks(self) -> None:
        self._view.clear_compressed_blocks()

    async def add_error(self, message: str, *, action_label: str | None = "Retry") -> None:
        await self._view.add_error(message, action_label=action_label)

    async def add_system(self, text: str, *, key: str | None = None) -> None:
        await self._view.add_system(text, key=key)

    async def update_system(self, key: str, text: str) -> None:
        await self._view.update_system(key, text)

    async def remove_system(self, key: str) -> None:
        await self._view.remove_system(key)

    async def replay_history(self, *args: Any, **kwargs: Any) -> None:
        await self._view.replay_history(*args, **kwargs)

    async def add_answer_delta(self, segment_id: str, text: str) -> None:
        await self._view.add_answer_delta(segment_id, text)

    def finish_answer_segment(self, segment_id: str, *, completed: bool) -> None:
        self._view.finish_answer_segment(segment_id, completed=completed)

    async def withdraw_answer(self, segment_ids: tuple[str, ...]) -> None:
        await self._view.withdraw_answer(segment_ids)

    async def add_thinking_delta(self, segment_id: str, channel: ContentChannel, text: str) -> None:
        await self._view.add_thinking_delta(segment_id, channel, text)

    def finish_thinking_segment(self, segment_id: str) -> None:
        self._view.finish_thinking_segment(segment_id)

    async def abandon_thinking(self, segment_ids: tuple[str, ...]) -> None:
        await self._view.abandon_thinking(segment_ids)

    async def add_agent_message(self, *args: Any, **kwargs: Any) -> None:
        await self._view.add_agent_message(*args, **kwargs)

    async def add_user_injection(self, *args: Any, **kwargs: Any) -> None:
        await self._view.add_user_injection(*args, **kwargs)

    def unlock_input_after_consumed_injection(self) -> None:
        self._view.unlock_input_after_consumed_injection()

    def unlock_input_after_abandoned_injection(self) -> None:
        self._view.unlock_input_after_abandoned_injection()

    async def add_tool_start(self, *args: Any, **kwargs: Any) -> None:
        await self._view.add_tool_start(*args, **kwargs)

    def update_tool_progress(self, call_id: str, lines: list[str], **kwargs: Any) -> None:
        self._view.update_tool_progress(call_id, lines, **kwargs)

    def update_tool_args(self, call_id: str, args: dict[str, Any]) -> None:
        self._view.update_tool_args(call_id, args)

    def update_tool_status(self, call_id: str, status: str, **kwargs: Any) -> None:
        self._view.update_tool_status(call_id, status, **kwargs)

    async def add_tool_result(self, *args: Any, **kwargs: Any) -> None:
        await self._view.add_tool_result(*args, **kwargs)

    async def add_context_fold(self, *args: Any, **kwargs: Any) -> bool:
        return await self._view.add_context_fold(*args, **kwargs)

    def add_compressed_block(
        self,
        context_id: str,
        summary: str,
        freed_messages: int = 0,
        turn_range: tuple[int, int] = (0, 0),
    ) -> None:
        self._view.add_compressed_block(context_id, summary, freed_messages, turn_range)

    async def add_compaction_start(self, compaction_id: str) -> None:
        await self._view.add_compaction_start(compaction_id)

    def complete_compaction(self, *args: Any, **kwargs: Any) -> None:
        self._view.complete_compaction(*args, **kwargs)

    def show_compaction_retry(self, *args: Any, **kwargs: Any) -> None:
        self._view.show_compaction_retry(*args, **kwargs)

    def add_sub_agent_compaction_start(self, agent_name: str, invocation_id: str, compaction_id: str) -> None:
        self._view.add_sub_agent_compaction_start(agent_name, invocation_id, compaction_id)

    def complete_sub_agent_compaction(self, *args: Any, **kwargs: Any) -> None:
        self._view.complete_sub_agent_compaction(*args, **kwargs)

    def record_sub_agent_compaction_committed(self, invocation_id: str, compaction_id: str) -> None:
        self._view.record_sub_agent_compaction_committed(invocation_id, compaction_id)

    def link_sub_agent_invocation(
        self,
        parent_call_id: str,
        invocation_id: str,
        agent_name: str,
        sub_agent_log_file: str = "",
        *,
        tool_name: str = "",
    ) -> None:
        self._view.link_sub_agent_invocation(
            parent_call_id,
            invocation_id,
            agent_name,
            sub_agent_log_file,
            tool_name=tool_name,
        )

    def add_sub_agent_answer(self, agent_name: str, invocation_id: str, segment_id: str, text: str) -> None:
        self._view.add_sub_agent_answer(agent_name, invocation_id, segment_id, text)

    async def add_sub_agent_tool_start(self, *args: Any, **kwargs: Any) -> None:
        await self._view.add_sub_agent_tool_start(*args, **kwargs)

    def update_sub_agent_tool_args(self, *args: Any, **kwargs: Any) -> None:
        self._view.update_sub_agent_tool_args(*args, **kwargs)

    def update_sub_agent_tool_status(self, *args: Any, **kwargs: Any) -> None:
        self._view.update_sub_agent_tool_status(*args, **kwargs)

    def update_sub_agent_tool_progress(self, *args: Any, **kwargs: Any) -> None:
        self._view.update_sub_agent_tool_progress(*args, **kwargs)

    def complete_sub_agent_tool(self, *args: Any, **kwargs: Any) -> None:
        self._view.complete_sub_agent_tool(*args, **kwargs)

    def update_sub_agent_progress(self, *args: Any, **kwargs: Any) -> None:
        self._view.update_sub_agent_progress(*args, **kwargs)

    def sub_agent_retry_attempt(self, *args: Any, **kwargs: Any) -> None:
        self._view.sub_agent_retry_attempt(*args, **kwargs)

    def sub_agent_paused(self, *args: Any, **kwargs: Any) -> None:
        self._view.sub_agent_paused(*args, **kwargs)

    def sub_agent_resumed_after_pause(self, invocation_id: str) -> None:
        self._view.sub_agent_resumed_after_pause(invocation_id)

    def sub_agent_answer_withdrawn(self, invocation_id: str, segment_ids: tuple[str, ...]) -> None:
        self._view.sub_agent_answer_withdrawn(invocation_id, segment_ids)

    def sub_agent_cascade_aborted(self, invocation_id: str) -> None:
        self._view.sub_agent_cascade_aborted(invocation_id)

    def sub_agent_aborted(self, invocation_id: str, last_error: str) -> None:
        self._view.sub_agent_aborted(invocation_id, last_error)

    async def show_retry_attempt(self, *args: Any, **kwargs: Any) -> None:
        await self._view.show_retry_attempt(*args, **kwargs)
