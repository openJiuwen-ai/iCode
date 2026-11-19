# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Live tool group and sub-agent routing for chat transcripts.

Concurrency contract: in the live pipeline a tool's InvocationToolCallStart and
InvocationToolCallResult are published from the tool's own invocation task
(``service.agent_middleware.events.tool_events``), and ``EventBus.publish``
awaits the subscribed TUI handler chain inline — so a start's publish does
not return until the card exists and its ownership is registered, and the
tool only executes after that. A result therefore cannot overtake its own
start. The group-open lock, call-id pre-registration and generation
checks keep ownership consistent when distinct tool calls start or finish
in parallel, or the transcript clears during a mount.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from textual.widget import Widget

from chrys.app.tui.widgets.chat.file_snapshot import FileSnapshotPayload
from chrys.app.tui.widgets.chat.ports import ChatMountPort, StatusCleanupPort, TranscriptUiPort
from chrys.app.tui.widgets.chat.renderers.ask_user import AskUserToolCall
from chrys.app.tui.widgets.chat.renderers.sub_agent import SubAgentToolCall
from chrys.app.tui.widgets.chat.tool_call import ToolGroup

if TYPE_CHECKING:
    from collections.abc import Awaitable

    from chrys.app.tui.widgets import PromptDraft
    from chrys.app.tui.widgets.chat.process_controller import ProcessController
    from chrys.foundation.models.ask_user import AskUserQuestion


class ToolGroupRegistry:
    """Own live tool groups, call-id routing, sub-agent links, and tool kinds."""

    def __init__(
        self,
        mount: ChatMountPort,
        ui: TranscriptUiPort,
        status: StatusCleanupPort,
        finalize_agent_message: Callable[[], None] | None = None,
        tool_widget_configurator: Callable[[Widget], None] | None = None,
        workspace_cwd: Callable[[], str] | None = None,
        process: ProcessController | None = None,
        mount_live_group: Callable[[ToolGroup], Awaitable[bool]] | None = None,
    ) -> None:
        self._mount = mount
        self._ui = ui
        self._status = status
        self._workspace_cwd = workspace_cwd
        self._finalize_agent_message = finalize_agent_message
        self._tool_widget_configurator = tool_widget_configurator
        self._process = process
        self._mount_live_group = mount_live_group
        self._current_tool_group: ToolGroup | None = None
        self._sub_agent_widgets: dict[str, SubAgentToolCall] = {}
        self._retired_sub_agent_widgets: dict[str, SubAgentToolCall] = {}
        self._sub_agent_owning_groups: dict[SubAgentToolCall, ToolGroup] = {}
        self._unlinked_sub_agents: dict[str, list[SubAgentToolCall]] = {}
        self._tool_groups_by_call_id: dict[str, ToolGroup] = {}
        self._tool_kinds: dict[str, str] = {}
        self._group_generation = 0
        self._group_open_lock = asyncio.Lock()

    @property
    def current_tool_group(self) -> ToolGroup | None:
        """Current live tool group, if one is open."""
        return self._current_tool_group

    @current_tool_group.setter
    def current_tool_group(self, group: ToolGroup | None) -> None:
        self._current_tool_group = group

    @property
    def tool_groups_by_call_id(self) -> dict[str, ToolGroup]:
        """Live call-id to tool-group map."""
        return self._tool_groups_by_call_id

    @property
    def sub_agent_widgets(self) -> dict[str, SubAgentToolCall]:
        """Live invocation-id to sub-agent widget map."""
        return self._sub_agent_widgets

    @property
    def unlinked_sub_agents(self) -> dict[str, list[SubAgentToolCall]]:
        """FIFO fallback queues for not-yet-linked sub-agent widgets."""
        return self._unlinked_sub_agents

    @property
    def tool_kinds(self) -> dict[str, str]:
        """Runtime tool-name to kind catalog used by replay rendering."""
        return self._tool_kinds

    @tool_kinds.setter
    def tool_kinds(self, tool_kinds: dict[str, str]) -> None:
        self._tool_kinds = tool_kinds

    def set_tool_kinds(self, tool_kinds: dict[str, str]) -> None:
        """Set the runtime tool-name to kind catalog."""
        self._tool_kinds = tool_kinds

    def _sub_agent_owning_group(self, widget: SubAgentToolCall) -> ToolGroup | None:
        """Resolve stable ownership, accepting only identity-verified fallback."""
        group = self._sub_agent_owning_groups.get(widget)
        if group is not None:
            return group
        candidate = self._tool_groups_by_call_id.get(widget.call_id)
        if candidate is not None and candidate.get_tool(widget.call_id) is widget:
            self._sub_agent_owning_groups[widget] = candidate
            return candidate
        return None

    def _set_sub_agent_action_required(self, widget: SubAgentToolCall, *, required: bool) -> None:
        """Expose paused controls until the invocation resumes or terminates."""
        group = self._sub_agent_owning_group(widget)
        if group is None:
            return
        if required:
            group.lock_collapse_for(widget.call_id)
            self._ui.schedule_anchor_sync()
        else:
            group.unlock_collapse_for(widget.call_id)

    def resolve_tool_kind(self, tool_name: str) -> str:
        """Resolve a tool kind by name for replay rendering."""
        return self._tool_kinds.get(tool_name, "")

    def reset_for_clear(self) -> None:
        """Clear live per-transcript routing state while preserving tool kinds."""
        self._group_generation += 1
        self._current_tool_group = None
        self._sub_agent_widgets.clear()
        self._retired_sub_agent_widgets.clear()
        self._sub_agent_owning_groups.clear()
        self._unlinked_sub_agents.clear()
        self._tool_groups_by_call_id.clear()

    def end_group(self) -> None:
        """Close the current tool group so the next tool starts a new group."""
        if self._current_tool_group is not None:
            self._current_tool_group.finalize_activity_header()
        self._group_generation += 1
        self._current_tool_group = None
        # Committed-compaction signals are delivered on detached tasks and can
        # arrive after an interrupt closes the group; the cards stay in the
        # transcript, so retire their routing entries instead of dropping
        # them (cleared with the transcript in reset_for_clear).
        # Invocations whose parent tool call is still running stay live:
        # closing the group (intermediate prose between tool batches) must
        # not orphan an in-flight sub-agent card — its progress and inner
        # tool events still route by invocation_id. The interrupt path marks
        # records cancelled before calling here (cancel_running_and_end_group),
        # so cancelled invocations retire as before.
        for invocation_id, widget in list(self._sub_agent_widgets.items()):
            group = self._sub_agent_owning_group(widget)
            if group is not None and group.is_tool_running(widget.call_id):
                continue
            self._retired_sub_agent_widgets[invocation_id] = widget
            del self._sub_agent_widgets[invocation_id]
            self._sub_agent_owning_groups.pop(widget, None)
        for queue in self._unlinked_sub_agents.values():
            for widget in queue:
                self._sub_agent_owning_groups.pop(widget, None)
        self._unlinked_sub_agents.clear()

    def cancel_running_and_end_group(self, *, stopped_at: float | None = None) -> None:
        """Cancel in-progress tools in every open group and close them, stopped at *stopped_at*, now if not given.

        Besides the current group, older groups can still hold running
        records: ``end_group`` retains sub-agent invocations whose parent
        call is in flight, so after a mid-batch group split those parents
        live in an already-closed group. Stable discovery starts with the
        widget-identity ownership map. The call-id map is a secondary scan
        for tools that have not linked an invocation yet; skipping either
        source could leave a record visually running forever.
        """
        groups: list[ToolGroup] = []
        seen: set[int] = set()
        if self._current_tool_group is not None:
            groups.append(self._current_tool_group)
            seen.add(id(self._current_tool_group))
        for group in self._sub_agent_owning_groups.values():
            if id(group) not in seen:
                groups.append(group)
                seen.add(id(group))
        for call_id, group in self._tool_groups_by_call_id.items():
            if id(group) not in seen and group.is_tool_running(call_id):
                groups.append(group)
                seen.add(id(group))
        for group in groups:
            group.cancel_running(stopped_at=stopped_at)
            group.collapsed = True
        # Always advance the lifecycle, even when a InvocationToolCallStart is still
        # awaiting status cleanup and has not installed its group yet.
        self.end_group()

    async def add_tool_start(
        self,
        call_id: str,
        tool_name: str,
        tool_kind: str,
        args_summary: str = "",
        *,
        args: dict[str, Any] | None = None,
        provider_hosted: bool = False,
        hosted_family: str = "",
        provider: str = "",
        provider_item_type: str = "",
        provider_status: str = "",
        provider_call_id: str = "",
        canonical_status: str = "running",
        running_in_place: bool = False,
        started_at: float | None = None,
    ) -> None:
        """Add a running tool call, creating a group if needed.

        With *running_in_place*, the start of a call whose card still runs updates that card in its own group.
        *started_at* is when the call started, as ``time.monotonic()`` read it, if not now.
        """
        group_generation = self._group_generation
        owner = self._tool_groups_by_call_id.get(call_id)
        if owner is not None and owner.ignores_repeated_start(call_id):
            return
        group = self._current_tool_group
        if running_in_place and owner is not None and owner.is_tool_running(call_id):
            group = owner
        if group is None:
            group = await self._open_tool_group(group_generation)
            if group is None:
                return

        # Publish ownership BEFORE the mount await. ``add_tool`` creates the
        # logical record before mounting. A result arriving during mount can
        # update the record immediately and the card picks it up afterward.
        # The continuation leaves routing alone: clear or a newer start may
        # have changed it while the mount awaited. The sub-agent fallback
        # queue belongs to the generation in which the group opened.
        self._tool_groups_by_call_id[call_id] = group
        if provider_hosted:
            await group.add_tool(
                call_id,
                tool_name,
                tool_kind,
                args_summary,
                args=args,
                provider_hosted=True,
                hosted_family=hosted_family,
                provider=provider,
                provider_item_type=provider_item_type,
                provider_status=provider_status,
                provider_call_id=provider_call_id,
                canonical_status=canonical_status,
                started_at=started_at,
            )
        else:
            await group.add_tool(call_id, tool_name, tool_kind, args_summary, args=args, started_at=started_at)
        widget = group.get_tool(call_id)
        if self._group_generation == group_generation and isinstance(widget, SubAgentToolCall):
            self._sub_agent_owning_groups[widget] = group
            self._unlinked_sub_agents.setdefault(tool_name, []).append(widget)
        self._ui.schedule_anchor_sync()

    async def _open_tool_group(self, group_generation: int) -> ToolGroup | None:
        """Resolve or create the current tool group, returned fully mounted.

        Group creation is serialized, and ``_current_tool_group`` is
        published only after the mount await completes: ``add_tool`` queries
        the group's composed content container, so a concurrent
        InvocationToolCallStart that observed a published-but-unmounted group would
        crash on it (NoMatches). Lock waiters re-read the published group
        and reuse it instead of mounting a duplicate. Returns None when the
        group lifecycle advanced (interrupt/clear) while this call awaited.
        """
        async with self._group_open_lock:
            if self._group_generation != group_generation:
                return None
            group = self._current_tool_group
            if group is not None:
                return group
            await self._status.remove_trailing_status()
            if self._group_generation != group_generation:
                return None
            mount_live = self._mount_live_group if self._process is not None else None
            group = ToolGroup(
                tool_widget_configurator=self._tool_widget_configurator,
                workspace_cwd=self._workspace_cwd,
                collapsed=True,
                on_collapse_lock_changed=self._process.group_lock_changed if self._process is not None else None,
            )
            if mount_live is None:
                mounted = await self._mount.mount_transcript_widget(group)
            else:
                mounted = await mount_live(group)
            if not mounted or self._group_generation != group_generation:
                try:
                    if self._process is not None:
                        await self._process.remove_live_members((group,))
                finally:
                    await self._mount.remove_transcript_widget(group)
                return None
            # The text above settles and folds only now, with nothing awaited before the group's first call shows
            # it: a layout during the mount would show the text folded and the waiting row gone, with nothing in
            # their place, for a frame.
            if mount_live is None and self._finalize_agent_message is not None:
                self._finalize_agent_message()
            self._current_tool_group = group
            return group

    async def add_tool_result(
        self,
        call_id: str,
        tool_name: str,
        result: str,
        duration_ms: int = 0,
        *,
        image_contents: list[Any] | None = None,
        file_snapshot: FileSnapshotPayload | None = None,
        approval: str | None = None,
        metadata: dict[str, Any] | None = None,
        artifacts: list[dict[str, Any]] | None = None,
        provider_status: str = "",
        canonical_status: str = "completed",
    ) -> None:
        """Update a live tool call as complete."""
        # The owning group wins: a call whose group closed (end_group) while a
        # newer group is already open must not send its result to the newer
        # group, where it would be silently dropped. The current group is only
        # a fallback for results racing in before add_tool_start registered.
        group = self._tool_groups_by_call_id.get(call_id) or self._current_tool_group
        if group is not None:
            group.complete_tool(
                call_id,
                result,
                duration_ms,
                image_contents=image_contents,
                file_snapshot=file_snapshot,
                approval=approval,
                metadata=metadata,
                artifacts=artifacts,
                provider_status=provider_status,
                canonical_status=canonical_status,
            )
            self._ui.schedule_anchor_sync()

    def update_tool_progress(
        self,
        call_id: str,
        lines: list[str],
        *,
        image_contents: list[Any] | None = None,
        snapshot_metadata: dict[str, Any] | None = None,
        provider_status: str = "",
    ) -> None:
        """Forward streaming progress lines to a running tool widget."""
        group = self._tool_groups_by_call_id.get(call_id)
        if group is not None:
            if image_contents is not None or snapshot_metadata is not None or provider_status:
                group.update_tool_progress(
                    call_id,
                    lines,
                    image_contents=image_contents,
                    snapshot_metadata=snapshot_metadata,
                    provider_status=provider_status,
                )
            else:
                group.update_tool_progress(call_id, lines)
            self._ui.schedule_anchor_sync()

    def update_tool_args(self, call_id: str, args: dict[str, Any]) -> None:
        """Refresh a running tool widget after approval edits its arguments."""
        group = self._tool_groups_by_call_id.get(call_id)
        if group is not None:
            group.update_tool_args(call_id, args)
            self._ui.schedule_anchor_sync()

    def update_tool_status(
        self,
        call_id: str,
        status: str,
        *,
        provider_status: str = "",
        metadata: dict[str, Any] | None = None,
        stopped_at: float | None = None,
    ) -> None:
        """Apply a canonical hosted lifecycle transition; one that ends the call ends it at *stopped_at*."""
        group = self._tool_groups_by_call_id.get(call_id)
        if group is not None:
            group.update_tool_status(
                call_id,
                status,
                provider_status=provider_status,
                metadata=metadata,
                stopped_at=stopped_at,
            )
            self._ui.schedule_anchor_sync()

    def show_ask_user_inline(
        self,
        call_id: str,
        request_id: str,
        questions: tuple[AskUserQuestion, ...],
        *,
        draft: PromptDraft | None = None,
    ) -> bool:
        """Show a live ask_user response form inside the matching tool renderer."""
        if not call_id or not request_id:
            return False
        group = self._tool_groups_by_call_id.get(call_id)
        if group is None:
            return False
        widget = group.get_tool(call_id)
        if not isinstance(widget, AskUserToolCall):
            return False
        if not widget.show_inline_prompt(request_id, questions, draft=draft):
            return False
        group.lock_collapse_for(call_id)
        self._ui.scroll_inline_prompt_to_top_after_refresh(widget)
        return True

    def handle_ask_user_inline_resized(self, call_id: str) -> None:
        """Delegate inline ask_user resize relayout for the matching live group."""
        self._ui.on_inline_prompt_resized(self._tool_groups_by_call_id.get(call_id))

    def is_tool_running(self, call_id: str) -> bool:
        """Return whether the tool record for ``call_id`` is still running."""
        group = self._tool_groups_by_call_id.get(call_id)
        return group is not None and group.is_tool_running(call_id)

    def clear_ask_user_inline_prompts(self) -> None:
        """Clear live inline ask_user prompts from every known tool group."""
        seen_group_ids: set[int] = set()
        for group in self._tool_groups_by_call_id.values():
            group_id = id(group)
            if group_id in seen_group_ids:
                continue
            seen_group_ids.add(group_id)
            group.clear_ask_user_inline_prompts()

    def clear_ask_user_inline(self, call_id: str, request_id: str) -> bool:
        """Clear one timed-out inline prompt without disturbing a newer request."""
        group = self._tool_groups_by_call_id.get(call_id)
        if group is None:
            return False
        widget = group.get_tool(call_id)
        if not isinstance(widget, AskUserToolCall) or not widget.clear_inline_prompt_for(request_id):
            return False
        group.unlock_collapse_for(call_id)
        return True

    def link_sub_agent_invocation(
        self,
        parent_call_id: str,
        invocation_id: str,
        agent_name: str,
        sub_agent_log_file: str = "",
        *,
        tool_name: str = "",
    ) -> None:
        """Bind an invocation id by exact parent ownership or tool-name FIFO."""
        if not invocation_id:
            return
        if invocation_id in self._sub_agent_widgets:
            return
        group = self._tool_groups_by_call_id.get(parent_call_id)
        widget = group.get_tool(parent_call_id) if group is not None else None
        if not isinstance(widget, SubAgentToolCall) and tool_name:
            unlinked = self._unlinked_sub_agents.get(tool_name)
            if unlinked:
                widget = unlinked.pop(0)
                if not unlinked:
                    del self._unlinked_sub_agents[tool_name]
                group = self._sub_agent_owning_group(widget)
        if not isinstance(widget, SubAgentToolCall):
            return
        widget.claim_invocation(invocation_id, agent_name, sub_agent_log_file)
        self._sub_agent_widgets[invocation_id] = widget
        if group is not None:
            self._sub_agent_owning_groups[widget] = group
            group.update_sub_agent_display_name(widget.call_id, agent_name)
        for name, queue in list(self._unlinked_sub_agents.items()):
            if widget in queue:
                queue.remove(widget)
                if not queue:
                    del self._unlinked_sub_agents[name]

    def _resolve_sub_agent(self, invocation_id: str) -> SubAgentToolCall | None:
        """Resolve a widget after invocation-start established its route."""
        return self._sub_agent_widgets.get(invocation_id)

    def add_sub_agent_answer(self, agent_name: str, invocation_id: str, segment_id: str, text: str) -> None:
        """Route one whole answer segment to one sub-agent transcript."""
        widget = self._resolve_sub_agent(invocation_id)
        if widget is not None:
            activity = widget.add_answer(segment_id, text, profile_name=agent_name)
            group = self._sub_agent_owning_group(widget)
            if activity is not None and group is not None:
                group.update_sub_agent_activity(widget.call_id, activity)

    async def add_sub_agent_tool_start(
        self,
        agent_name: str,
        invocation_id: str,
        tool_name: str,
        args: dict[str, Any],
        call_id: str,
        *,
        tool_kind: str = "",
        provider_hosted: bool = False,
        hosted_family: str = "",
        provider: str = "",
        provider_item_type: str = "",
        provider_status: str = "",
        provider_call_id: str = "",
    ) -> None:
        """Route an inner tool call start to the correct SubAgentToolCall widget."""
        widget = self._resolve_sub_agent(invocation_id)
        if widget is not None:
            changed = await widget.add_inner_tool_start(
                call_id,
                tool_name,
                args,
                tool_kind=tool_kind,
                provider_hosted=provider_hosted,
                hosted_family=hosted_family,
                provider=provider,
                provider_item_type=provider_item_type,
                provider_status=provider_status,
                provider_call_id=provider_call_id,
            )
            group = self._sub_agent_owning_group(widget)
            if changed and group is not None:
                group.update_nested_tool_activity(
                    widget.call_id,
                    call_id,
                    tool_name=tool_name,
                    tool_kind=tool_kind,
                    args=args,
                )

    def update_sub_agent_tool_args(self, invocation_id: str, call_id: str, args: dict[str, Any]) -> None:
        """Route nested argument snapshots to the owning sub-agent card."""
        widget = self._sub_agent_widgets.get(invocation_id)
        if widget is not None:
            widget.update_inner_tool_args(call_id, args)
            group = self._sub_agent_owning_group(widget)
            if group is not None:
                group.update_nested_tool_activity(widget.call_id, call_id, args=args)

    def update_sub_agent_tool_status(
        self,
        invocation_id: str,
        call_id: str,
        status: str,
        *,
        metadata: dict[str, Any] | None = None,
        provider_status: str = "",
    ) -> None:
        """Route nested lifecycle status to the owning sub-agent card."""
        widget = self._sub_agent_widgets.get(invocation_id)
        if widget is not None:
            widget.update_inner_tool_status(
                call_id,
                status,
                metadata=metadata,
                provider_status=provider_status,
            )
            group = self._sub_agent_owning_group(widget)
            if group is not None:
                group.update_nested_tool_activity(widget.call_id, call_id, status=status)

    def update_sub_agent_tool_progress(
        self,
        invocation_id: str,
        call_id: str,
        lines: list[str],
        *,
        image_contents: list[Any] | None = None,
        snapshot_metadata: dict[str, Any] | None = None,
        provider_status: str = "",
    ) -> None:
        """Route nested progress snapshots to the owning sub-agent card."""
        widget = self._sub_agent_widgets.get(invocation_id)
        if widget is not None:
            widget.update_inner_tool_progress(
                call_id,
                lines,
                image_contents=image_contents,
                snapshot_metadata=snapshot_metadata,
                provider_status=provider_status,
            )

    def add_sub_agent_compaction_start(self, agent_name: str, invocation_id: str, compaction_id: str) -> None:
        """Route a Phase-4 compaction start line to the correct sub-agent card."""
        widget = self._resolve_sub_agent(invocation_id)
        if widget is not None:
            widget.add_compaction_start(compaction_id)

    def complete_sub_agent_compaction(
        self,
        invocation_id: str,
        compaction_id: str,
        *,
        outcome: str,
        duration_ms: int = 0,
        format_violation: str = "",
        failure_reason: str = "",
    ) -> None:
        """Flip the compaction line on the matching sub-agent card."""
        widget = self._sub_agent_widgets.get(invocation_id)
        if widget is not None:
            widget.complete_compaction(
                compaction_id,
                outcome=outcome,
                duration_ms=duration_ms,
                format_violation=format_violation,
                failure_reason=failure_reason,
            )

    def record_sub_agent_compaction_committed(self, invocation_id: str, compaction_id: str) -> None:
        """Bump the compaction counter on the matching sub-agent card.

        Falls back to retired routing entries: the committed signal rides a
        detached task, so an interrupt can close the group before it lands
        while the card — and the durable round it must count — remain.
        """
        widget = self._sub_agent_widgets.get(invocation_id) or self._retired_sub_agent_widgets.get(invocation_id)
        if widget is not None:
            widget.record_compaction_committed(compaction_id)

    def complete_sub_agent_tool(
        self,
        agent_name: str,
        invocation_id: str,
        call_id: str,
        result: str,
        duration_ms: int,
        image_contents: list[Any] | None = None,
        artifacts: list[dict[str, Any]] | None = None,
        approval: str | None = None,
        metadata: dict[str, Any] | None = None,
        provider_status: str = "",
        canonical_status: str = "completed",
    ) -> None:
        """Route an inner tool call result to the correct SubAgentToolCall widget."""
        widget = self._sub_agent_widgets.get(invocation_id)
        if widget is not None:
            widget.complete_inner_tool(
                call_id,
                result,
                duration_ms,
                image_contents=image_contents,
                artifacts=artifacts,
                approval=approval,
                metadata=metadata,
                provider_status=provider_status,
                canonical_status=canonical_status,
            )
            group = self._sub_agent_owning_group(widget)
            if group is not None:
                group.update_nested_tool_activity(widget.call_id, call_id, status="completed")

    def update_sub_agent_progress(
        self,
        invocation_id: str,
        tool_call_count: int,
        total_tokens: int,
        total_usage_tokens: int = 0,
        usage_unreported_attempts: int = 0,
    ) -> None:
        """Update progress stats on a running SubAgentToolCall widget."""
        widget = self._sub_agent_widgets.get(invocation_id)
        if widget is not None:
            widget.update_progress(
                tool_call_count,
                total_tokens,
                total_usage_tokens,
                usage_unreported_attempts,
            )

    def sub_agent_retry_attempt(
        self,
        invocation_id: str,
        message: str,
        attempt: int,
        max_attempts: int,
        delay_seconds: int,
    ) -> None:
        """Dispatch a sub-agent auto-retry banner to the correct card."""
        widget = self._sub_agent_widgets.get(invocation_id)
        if widget is not None:
            widget.set_retry_attempt(message, attempt, max_attempts, delay_seconds)

    def sub_agent_paused(
        self,
        invocation_id: str,
        reason: str,
        last_error: str,
        retry_attempts: int,
        diagnostic_path: str | None = None,
        last_error_display: str | None = None,
    ) -> None:
        """Dispatch a pause transition to the correct card."""
        widget = self._sub_agent_widgets.get(invocation_id)
        if widget is not None:
            widget.set_paused(reason, last_error, retry_attempts, diagnostic_path, last_error_display)
            self._set_sub_agent_action_required(widget, required=True)

    def sub_agent_answer_withdrawn(self, invocation_id: str, segment_ids: tuple[str, ...]) -> None:
        """Withdraw the answer a sub-agent's retry dropped from its transcript and its latest activity."""
        widget = self._sub_agent_widgets.get(invocation_id)
        if widget is not None and widget.withdraw_answer(segment_ids):
            group = self._sub_agent_owning_group(widget)
            if group is not None:
                group.update_sub_agent_activity(widget.call_id, None)

    def sub_agent_resumed_after_pause(self, invocation_id: str) -> None:
        """Dispatch a resume-after-pause transition to the correct card."""
        widget = self._sub_agent_widgets.get(invocation_id)
        if widget is not None:
            widget.set_resumed_after_pause()
            self._set_sub_agent_action_required(widget, required=False)

    def sub_agent_cascade_aborted(self, invocation_id: str) -> None:
        """Dispatch a cascade-abort transition from global interrupt."""
        widget = self._sub_agent_widgets.get(invocation_id)
        if widget is not None:
            widget.set_cascade_aborted()
            self._set_sub_agent_action_required(widget, required=False)

    def sub_agent_aborted(self, invocation_id: str, last_error: str) -> None:
        """Dispatch a user-abort transition to the correct card."""
        widget = self._sub_agent_widgets.get(invocation_id)
        if widget is not None:
            widget.set_aborted(last_error)
            self._set_sub_agent_action_required(widget, required=False)
