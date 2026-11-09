# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Fake-port tests for extracted chat transcript collaborators."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any, TypedDict, Unpack

import pytest
from textual.widget import Widget

import chrys.app.tui.widgets.chat.tool_registry as tool_registry_module
from chrys.app.tui.widgets.chat.context_fold import ContextFoldWidget
from chrys.app.tui.widgets.chat.live_renderer import LiveTranscriptRenderer
from chrys.app.tui.widgets.chat.messages import AgentMessage, ErrorMessage, UserMessage
from chrys.app.tui.widgets.chat.renderers.ask_user import AskUserToolCall
from chrys.app.tui.widgets.chat.replay import HistoryReplayRenderer
from chrys.app.tui.widgets.chat.toc_model import TurnTocModel
from chrys.app.tui.widgets.chat.tool_call import ToolGroup
from chrys.app.tui.widgets.chat.tool_registry import ToolGroupRegistry
from chrys.foundation.models.ask_user import AskUserOption, AskUserQuestion
from chrys.foundation.models.history_markers import HistoryMarkerKind
from chrys.kernel import (
    GROUP_ANNOTATION_KEY,
    SUMMARY_OF_GROUP_IDS_KEY,
    SUMMARY_OF_MESSAGE_IDS_KEY,
    Content,
    Message,
    set_excluded,
)
from chrys.service.context.providers.history import CompressedBlock


class _FakeMount:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls
        self.widgets: list[object] = []

    async def mount_transcript_widget(self, widget: Widget) -> bool:
        self.calls.append(f"mount:{type(widget).__name__}")
        self.widgets.append(widget)
        return True

    async def mount_transcript_widgets(
        self, widgets: list[Widget], *, process_failures_after: tuple[Widget, ...] = ()
    ) -> None:
        self.calls.append(f"mount_batch:{len(widgets)}")
        self.widgets.extend(widgets)

    async def remove_transcript_widget(self, widget: Widget) -> None:
        self.calls.append(f"remove:{type(widget).__name__}")
        if widget in self.widgets:
            self.widgets.remove(widget)

    def call_after_refresh(self, callback: object, *args: object, **kwargs: object) -> bool:
        self.calls.append("after_refresh")
        return True


class _FakeUi:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    async def dismiss_welcome_for_content(self) -> None:
        self.calls.append("dismiss_welcome")

    def on_live_user_turn_started(self) -> None:
        self.calls.append("live_user_started")

    def on_replay_started(self) -> None:
        self.calls.append("replay_started")

    def scroll_user_message_to_top(self, widget: Widget) -> None:
        self.calls.append(f"scroll_user:{type(widget).__name__}")

    def on_final_response_started(self) -> None:
        self.calls.append("final_started")

    def schedule_anchor_sync(self) -> None:
        self.calls.append("anchor_sync")

    def on_status_message_mounted(self) -> None:
        self.calls.append("status_mounted")

    def scroll_inline_prompt_to_top_after_refresh(self, widget: Widget) -> None:
        self.calls.append(f"inline_scroll:{type(widget).__name__}")

    def on_inline_prompt_resized(self, group: object | None) -> None:
        self.calls.append(f"inline_resized:{group is not None}")

    def after_replay_history_mounted(self) -> None:
        self.calls.append("after_replay")


class _FakeStatus:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls
        self.children: list[Widget] = []

    async def remove_trailing_status(self) -> None:
        self.calls.append("remove_status")

    def failed_turn_status(self) -> Widget | None:
        self.calls.append("failed_status")
        return self.children[-1] if self.children else None

    def preceding_content(self, widget: Widget) -> Widget | None:
        self.calls.append("preceding_content")
        index = self.children.index(widget)
        return self.children[index - 1] if index else None


class _FakeQuery:
    def __init__(self, children: list[Widget] | None = None) -> None:
        self.children = children or []

    def mounted_tool_groups(self) -> list[object]:
        return []

    def mounted_agent_messages(self) -> list[object]:
        return []

    def mounted_user_messages(self) -> list[object]:
        return []

    def direct_children(self) -> list[Widget]:
        return list(self.children)

    def transcript_content_children(self) -> list[Widget]:
        return list(self.children)


class _ToolGroupOptions(TypedDict, total=False):
    """The keywords the registry builds a ``ToolGroup`` with."""

    tool_widget_configurator: Callable[[Widget], None] | None
    workspace_cwd: Callable[[], str] | None


class _FakeToolGroup:
    def __init__(self, **options: Unpack[_ToolGroupOptions]) -> None:
        self.options = options
        self.added: list[tuple[str, str, str]] = []
        self.completed: list[str] = []
        self.progress: list[str] = []
        self.args: list[str] = []
        self.locked: list[str] = []
        self.unlocked: list[str] = []
        self.collapsed = False
        self.collapse_locked = False
        self._cancelled = False
        self.activity_header_finalized = False
        self._tools: dict[str, Widget] = {}
        self.nested_activity: list[tuple[str, str, dict[str, Any]]] = []
        self.sub_agent_display_names: list[tuple[str, str]] = []
        self.sub_agent_activities: list[tuple[str, object]] = []

    async def add_tool(
        self,
        call_id: str,
        tool_name: str,
        tool_kind: str,
        args_summary: str = "",
        *,
        args: dict[str, Any] | None = None,
        started_at: float | None = None,
    ) -> None:
        self.added.append((call_id, tool_name, tool_kind))

    def get_tool(self, call_id: str) -> Widget | None:
        return self._tools.get(call_id)

    def complete_tool(self, call_id: str, result: str, duration_ms: int = 0, **kwargs: object) -> None:
        self.completed.append(call_id)

    def update_tool_progress(self, call_id: str, lines: list[str]) -> None:
        self.progress.append(call_id)

    def update_tool_args(self, call_id: str, args: dict[str, Any]) -> None:
        self.args.append(call_id)

    def update_nested_tool_activity(self, parent_call_id: str, call_id: str, **changes: Any) -> None:
        self.nested_activity.append((parent_call_id, call_id, changes))

    def update_sub_agent_display_name(self, parent_call_id: str, agent_name: str) -> None:
        self.sub_agent_display_names.append((parent_call_id, agent_name))

    def update_sub_agent_activity(self, parent_call_id: str, activity: object) -> None:
        self.sub_agent_activities.append((parent_call_id, activity))

    def reveal_tool(self, call_id: str) -> Widget | None:
        return self._tools.get(call_id)

    def lock_collapse_for(self, call_id: str) -> None:
        if call_id not in self.locked:
            self.locked.append(call_id)
        self.collapsed = False
        self.collapse_locked = True

    def unlock_collapse_for(self, call_id: str) -> None:
        self.unlocked.append(call_id)
        if call_id in self.locked:
            self.locked.remove(call_id)
        self.collapse_locked = bool(self.locked)

    def is_tool_running(self, call_id: str) -> bool:
        # Mirrors the real ToolGroup: cancel_running errors every running
        # record, so nothing reports running afterwards.
        return not self._cancelled and call_id in self._tools

    def finalize_activity_header(self) -> None:
        self.activity_header_finalized = True

    def clear_ask_user_inline_prompts(self) -> None:
        self.locked.clear()
        self.collapse_locked = False

    def cancel_running(self, *, stopped_at: float | None = None) -> None:
        self.completed.append("cancel")
        self._cancelled = True


class _FakeSubAgentWidget:
    def __init__(self, call_id: str = "") -> None:
        self.call_id = call_id
        self.events: list[tuple[object, ...]] = []

    async def add_inner_tool_start(
        self,
        call_id: str,
        tool_name: str,
        args: dict[str, Any],
        *,
        tool_kind: str = "",
        provider_hosted: bool = False,
        hosted_family: str = "",
        provider: str = "",
        provider_item_type: str = "",
        provider_status: str = "",
        provider_call_id: str = "",
    ) -> bool:
        self.events.append(("start", call_id, tool_name, args, tool_kind))
        return True

    def complete_inner_tool(
        self,
        call_id: str,
        result: str,
        duration_ms: int,
        *,
        image_contents: list[Any] | None = None,
        artifacts: list[dict[str, Any]] | None = None,
        approval: str | None = None,
        metadata: dict[str, Any] | None = None,
        provider_status: str = "",
        canonical_status: str = "completed",
    ) -> None:
        self.events.append(("complete", call_id, result, duration_ms, image_contents, artifacts, approval, metadata))

    def update_progress(
        self,
        tool_call_count: int,
        total_tokens: int,
        total_usage_tokens: int,
        usage_unreported_attempts: int = 0,
    ) -> None:
        self.events.append(("progress", tool_call_count, total_tokens, total_usage_tokens, usage_unreported_attempts))

    def update_inner_tool_args(self, call_id: str, args: dict[str, Any]) -> None:
        self.events.append(("inner_args", call_id, args))

    def update_inner_tool_status(
        self,
        call_id: str,
        status: str,
        *,
        metadata: dict[str, Any] | None = None,
        provider_status: str = "",
    ) -> None:
        self.events.append(("inner_status", call_id, status, metadata))

    def update_inner_tool_progress(
        self,
        call_id: str,
        lines: list[str],
        *,
        image_contents: list[Any] | None = None,
        snapshot_metadata: dict[str, Any] | None = None,
        provider_status: str = "",
    ) -> None:
        self.events.append(("inner_progress", call_id, lines, image_contents))

    def set_retry_attempt(self, message: str, attempt: int, max_attempts: int, delay_seconds: int) -> None:
        self.events.append(("retry", message, attempt, max_attempts, delay_seconds))

    def set_paused(
        self,
        reason: str,
        last_error: str,
        retry_attempts: int,
        diagnostic_path: str | None = None,
        last_error_display: str | None = None,
    ) -> None:
        self.events.append(("paused", reason, last_error, retry_attempts, diagnostic_path, last_error_display))

    def set_resumed_after_pause(self) -> None:
        self.events.append(("resumed",))

    def set_cascade_aborted(self) -> None:
        self.events.append(("cascade_aborted",))

    def set_aborted(self, last_error: str) -> None:
        self.events.append(("aborted", last_error))


@pytest.mark.asyncio
async def test_tool_registry_routes_sub_agent_inner_tool_start_and_completion() -> None:
    calls: list[str] = []
    registry = ToolGroupRegistry(_FakeMount(calls), _FakeUi(calls), _FakeStatus(calls))
    first: Any = _FakeSubAgentWidget()
    second: Any = _FakeSubAgentWidget()
    registry.sub_agent_widgets.update({"inv-a": first, "inv-b": second})

    await registry.add_sub_agent_tool_start(
        "Explore",
        "inv-a",
        "read_file",
        {"path": "notes.txt"},
        "call-a",
        tool_kind="filesystem.read",
    )
    registry.complete_sub_agent_tool(
        "Explore",
        "inv-a",
        "call-a",
        "done",
        17,
        image_contents=[{"image": "preview"}],
        artifacts=[{"name": "report.csv"}],
        approval="auto",
        metadata={"source": "test"},
    )
    await registry.add_sub_agent_tool_start("Explore", "missing", "ignored", {}, "call-missing")
    registry.complete_sub_agent_tool("Explore", "missing", "call-missing", "ignored", 0)

    assert first.events == [
        ("start", "call-a", "read_file", {"path": "notes.txt"}, "filesystem.read"),
        (
            "complete",
            "call-a",
            "done",
            17,
            [{"image": "preview"}],
            [{"name": "report.csv"}],
            "auto",
            {"source": "test"},
        ),
    ]
    assert second.events == []


@pytest.mark.asyncio
async def test_nested_activity_stays_with_invocation_owner_after_parent_call_id_reuse() -> None:
    """A reused parent call id must not redirect an older invocation's header updates."""
    calls: list[str] = []
    registry = ToolGroupRegistry(_FakeMount(calls), _FakeUi(calls), _FakeStatus(calls))
    old_group = _FakeToolGroup()
    old_widget: Any = _FakeSubAgentWidget("reused")
    old_group._tools["reused"] = old_widget
    registry.tool_groups_by_call_id["reused"] = old_group
    registry.sub_agent_widgets["old-invocation"] = old_widget

    # Non-tool prose closes the old group, but its sub-agent invocation keeps
    # running. A later provider exchange then reuses the same parent call id.
    registry.end_group()
    new_group = _FakeToolGroup()
    new_widget: Any = _FakeSubAgentWidget("reused")
    new_group._tools["reused"] = new_widget
    registry.tool_groups_by_call_id["reused"] = new_group

    await registry.add_sub_agent_tool_start(
        "Explore",
        "old-invocation",
        "read_file",
        {"path": "old.txt"},
        "inner",
        tool_kind="filesystem.read",
    )
    registry.update_sub_agent_tool_args("old-invocation", "inner", {"path": "updated.txt"})
    registry.update_sub_agent_tool_status("old-invocation", "inner", "running")
    registry.update_sub_agent_tool_progress("old-invocation", "inner", ["streaming"])
    registry.complete_sub_agent_tool("Explore", "old-invocation", "inner", "done", 17)

    assert old_group.nested_activity == [
        (
            "reused",
            "inner",
            {
                "tool_name": "read_file",
                "tool_kind": "filesystem.read",
                "args": {"path": "old.txt"},
            },
        ),
        ("reused", "inner", {"args": {"path": "updated.txt"}}),
        ("reused", "inner", {"status": "running"}),
        ("reused", "inner", {"status": "completed"}),
    ]
    assert new_group.nested_activity == []
    assert old_widget.events == [
        ("start", "inner", "read_file", {"path": "old.txt"}, "filesystem.read"),
        ("inner_args", "inner", {"path": "updated.txt"}),
        ("inner_status", "inner", "running", None),
        ("inner_progress", "inner", ["streaming"], None),
        ("complete", "inner", "done", 17, None, None, None, None),
    ]
    assert new_widget.events == []


def test_tool_registry_routes_sub_agent_inner_hosted_mutations() -> None:
    calls: list[str] = []
    registry = ToolGroupRegistry(_FakeMount(calls), _FakeUi(calls), _FakeStatus(calls))
    widget: Any = _FakeSubAgentWidget()
    registry.sub_agent_widgets["inv-a"] = widget

    registry.update_sub_agent_tool_args("inv-a", "hosted-a", {"query": "Chrys"})
    registry.update_sub_agent_tool_status("inv-a", "hosted-a", "running", metadata={"phase": "snapshot"})
    registry.update_sub_agent_tool_progress(
        "inv-a",
        "hosted-a",
        ["Searching"],
        image_contents=[{"uri": "data:image/png;base64,AAA"}],
    )

    assert widget.events == [
        ("inner_args", "hosted-a", {"query": "Chrys"}),
        ("inner_status", "hosted-a", "running", {"phase": "snapshot"}),
        ("inner_progress", "hosted-a", ["Searching"], [{"uri": "data:image/png;base64,AAA"}]),
    ]


def test_tool_registry_routes_sub_agent_progress_retry_and_pause() -> None:
    calls: list[str] = []
    registry = ToolGroupRegistry(_FakeMount(calls), _FakeUi(calls), _FakeStatus(calls))
    first: Any = _FakeSubAgentWidget()
    second: Any = _FakeSubAgentWidget("parent-b")
    registry.sub_agent_widgets.update({"inv-a": first, "inv-b": second})
    group = _FakeToolGroup()
    group.collapsed = True
    group._tools["parent-b"] = second
    registry.tool_groups_by_call_id["parent-b"] = group  # type: ignore[assignment]

    registry.update_sub_agent_progress("inv-b", 3, 120, 145, 1)
    registry.sub_agent_retry_attempt("inv-b", "rate limited", 2, 4, 8)
    registry.sub_agent_paused("inv-b", "retry exhausted", "timeout", 4, None, "Timed out waiting.")
    registry.sub_agent_paused("missing", "ignored", "ignored", 0)

    assert first.events == []
    assert second.events == [
        ("progress", 3, 120, 145, 1),
        ("retry", "rate limited", 2, 4, 8),
        ("paused", "retry exhausted", "timeout", 4, None, "Timed out waiting."),
    ]
    assert group.collapsed is False
    assert group.collapse_locked is True
    assert group.locked == ["parent-b"]
    assert calls == ["anchor_sync"]


def test_tool_registry_routes_sub_agent_terminal_transitions() -> None:
    calls: list[str] = []
    registry = ToolGroupRegistry(_FakeMount(calls), _FakeUi(calls), _FakeStatus(calls))
    first: Any = _FakeSubAgentWidget("parent-a")
    second: Any = _FakeSubAgentWidget()
    registry.sub_agent_widgets.update({"inv-a": first, "inv-b": second})
    group = _FakeToolGroup()
    group._tools["parent-a"] = first
    group.lock_collapse_for("parent-a")
    registry.tool_groups_by_call_id["parent-a"] = group  # type: ignore[assignment]

    registry.sub_agent_resumed_after_pause("inv-a")
    registry.sub_agent_cascade_aborted("inv-a")
    registry.sub_agent_aborted("inv-a", "cancelled by user")
    registry.sub_agent_aborted("missing", "ignored")

    assert first.events == [("resumed",), ("cascade_aborted",), ("aborted", "cancelled by user")]
    assert second.events == []
    assert group.collapse_locked is False
    assert group.unlocked == ["parent-a", "parent-a", "parent-a"]


@pytest.mark.asyncio
async def test_tool_registry_uses_status_cleanup_before_mount_and_preserves_routing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(tool_registry_module, "ToolGroup", _FakeToolGroup)
    registry = ToolGroupRegistry(
        _FakeMount(calls),
        _FakeUi(calls),
        _FakeStatus(calls),
        finalize_agent_message=lambda: calls.append("finalize_agent"),
    )

    await registry.add_tool_start("a", "zsh", "shell")
    first = registry.current_tool_group
    assert isinstance(first, _FakeToolGroup)
    registry.end_group()
    await registry.add_tool_start("b", "read_file", "filesystem.read")
    second = registry.current_tool_group
    assert isinstance(second, _FakeToolGroup)

    await registry.add_tool_result("a", "zsh", "done")
    registry.update_tool_progress("a", ["line"])
    registry.update_tool_args("a", {"cmd": "echo"})

    # The text above folds once the group is mounted: its first call shows it before anything else lays out.
    assert calls[:3] == ["remove_status", "mount:_FakeToolGroup", "finalize_agent"]
    assert calls[4:7] == ["remove_status", "mount:_FakeToolGroup", "finalize_agent"]
    # Result, progress, and args all route to the OWNING group: sending the
    # result to the newer current group would silently drop it, leaving the
    # call rendered as running forever.
    assert first.completed == ["a"]
    assert second.completed == []
    assert first.progress == ["a"]
    assert first.args == ["a"]
    assert calls.count("anchor_sync") == 5


@pytest.mark.asyncio
async def test_tool_registry_end_group_during_add_keeps_stable_routing(monkeypatch: pytest.MonkeyPatch) -> None:
    entered_add = asyncio.Event()
    release_add = asyncio.Event()

    class _DelayedToolGroup(_FakeToolGroup):
        async def add_tool(
            self,
            call_id: str,
            tool_name: str,
            tool_kind: str,
            args_summary: str = "",
            *,
            args: dict[str, Any] | None = None,
            started_at: float | None = None,
        ) -> None:
            entered_add.set()
            await asyncio.wait_for(release_add.wait(), timeout=5)
            await super().add_tool(call_id, tool_name, tool_kind, args_summary, args=args)

    calls: list[str] = []
    monkeypatch.setattr(tool_registry_module, "ToolGroup", _DelayedToolGroup)
    registry = ToolGroupRegistry(_FakeMount(calls), _FakeUi(calls), _FakeStatus(calls))

    add_task = asyncio.create_task(registry.add_tool_start("a", "zsh", "shell"))
    await asyncio.wait_for(entered_add.wait(), timeout=5)
    group = registry.current_tool_group
    registry.end_group()
    release_add.set()
    await add_task

    assert group is not None
    assert registry.current_tool_group is None
    assert registry.tool_groups_by_call_id["a"] is group
    registry.clear_ask_user_inline_prompts()


@pytest.mark.asyncio
async def test_tool_registry_delayed_start_does_not_clobber_reused_call_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stale start resuming after end_group must not steal a reused call_id.

    Interleaving: start A blocks in ``add_tool``; the group closes and a
    new group registers the SAME (provider-reused) call_id; A resumes.
    A's resume must not re-claim the mapping from the newer owner, or the
    new result would complete the old card while the current card spins
    forever.
    """
    entered_add = asyncio.Event()
    release_add = asyncio.Event()
    instances: list[_FakeToolGroup] = []

    class _FirstAddBlocksToolGroup(_FakeToolGroup):
        def __init__(self, **options: Unpack[_ToolGroupOptions]) -> None:
            super().__init__(**options)
            instances.append(self)
            self._blocks = len(instances) == 1

        async def add_tool(
            self,
            call_id: str,
            tool_name: str,
            tool_kind: str,
            args_summary: str = "",
            *,
            args: dict[str, Any] | None = None,
            started_at: float | None = None,
        ) -> None:
            if self._blocks:
                entered_add.set()
                await asyncio.wait_for(release_add.wait(), timeout=5)
            await super().add_tool(call_id, tool_name, tool_kind, args_summary, args=args)

    calls: list[str] = []
    monkeypatch.setattr(tool_registry_module, "ToolGroup", _FirstAddBlocksToolGroup)
    registry = ToolGroupRegistry(_FakeMount(calls), _FakeUi(calls), _FakeStatus(calls))

    stale_task = asyncio.create_task(registry.add_tool_start("dup", "zsh", "shell"))
    await asyncio.wait_for(entered_add.wait(), timeout=5)
    registry.end_group()
    await registry.add_tool_start("dup", "zsh", "shell")
    release_add.set()
    await stale_task

    old_group, new_group = instances
    assert registry.tool_groups_by_call_id["dup"] is new_group
    await registry.add_tool_result("dup", "zsh", "done")
    assert new_group.completed == ["dup"]
    assert old_group.completed == []


@pytest.mark.asyncio
async def test_tool_registry_delayed_start_supersedes_older_reused_call_id_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A delayed occurrence must advance the mapping past an OLDER owner.

    Interleaving: "dup" completes in G1 (its mapping persists across
    end_group); a new start reuses "dup" in G2 and blocks in ``add_tool``;
    end_group crosses; G2 resumes with no newer G3 registered. The mapping
    must end at G2 — leaving G1 as owner would deliver G2's result to
    G1's card.
    """
    entered_add = asyncio.Event()
    release_add = asyncio.Event()
    instances: list[_FakeToolGroup] = []

    class _SecondAddBlocksToolGroup(_FakeToolGroup):
        def __init__(self, **options: Unpack[_ToolGroupOptions]) -> None:
            super().__init__(**options)
            instances.append(self)
            self._blocks = len(instances) == 2

        async def add_tool(
            self,
            call_id: str,
            tool_name: str,
            tool_kind: str,
            args_summary: str = "",
            *,
            args: dict[str, Any] | None = None,
            started_at: float | None = None,
        ) -> None:
            if self._blocks:
                entered_add.set()
                await asyncio.wait_for(release_add.wait(), timeout=5)
            await super().add_tool(call_id, tool_name, tool_kind, args_summary, args=args)

    calls: list[str] = []
    monkeypatch.setattr(tool_registry_module, "ToolGroup", _SecondAddBlocksToolGroup)
    registry = ToolGroupRegistry(_FakeMount(calls), _FakeUi(calls), _FakeStatus(calls))

    await registry.add_tool_start("dup", "zsh", "shell")
    registry.end_group()
    delayed_task = asyncio.create_task(registry.add_tool_start("dup", "zsh", "shell"))
    await asyncio.wait_for(entered_add.wait(), timeout=5)
    registry.end_group()
    release_add.set()
    await delayed_task

    g1, g2 = instances
    assert registry.tool_groups_by_call_id["dup"] is g2
    await registry.add_tool_result("dup", "zsh", "done")
    assert g2.completed == ["dup"]
    assert g1.completed == []


@pytest.mark.asyncio
async def test_tool_registry_result_racing_replacement_start_routes_to_new_card(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A result landing while the replacement start is mid-mount hits the new card.

    Interleaving: G1 owns a provider-reused call_id; a new start in G2
    suspends inside ``add_tool`` while its widget mounts; the (fast) result
    arrives during that suspension. Ownership must already point at G2 —
    routing through the old owner would complete G1's card and leave G2's
    card running forever once its mount finishes.
    """
    entered_add = asyncio.Event()
    release_add = asyncio.Event()
    instances: list[_FakeToolGroup] = []

    class _SecondAddBlocksToolGroup(_FakeToolGroup):
        def __init__(self, **options: Unpack[_ToolGroupOptions]) -> None:
            super().__init__(**options)
            instances.append(self)
            self._blocks = len(instances) == 2

        async def add_tool(
            self,
            call_id: str,
            tool_name: str,
            tool_kind: str,
            args_summary: str = "",
            *,
            args: dict[str, Any] | None = None,
            started_at: float | None = None,
        ) -> None:
            if self._blocks:
                entered_add.set()
                await asyncio.wait_for(release_add.wait(), timeout=5)
            await super().add_tool(call_id, tool_name, tool_kind, args_summary, args=args)

    calls: list[str] = []
    monkeypatch.setattr(tool_registry_module, "ToolGroup", _SecondAddBlocksToolGroup)
    registry = ToolGroupRegistry(_FakeMount(calls), _FakeUi(calls), _FakeStatus(calls))

    await registry.add_tool_start("dup", "zsh", "shell")
    registry.end_group()
    delayed_task = asyncio.create_task(registry.add_tool_start("dup", "zsh", "shell"))
    await asyncio.wait_for(entered_add.wait(), timeout=5)
    # The fast result lands while the replacement start is still mounting.
    await registry.add_tool_result("dup", "zsh", "done")
    release_add.set()
    await delayed_task

    g1, g2 = instances
    assert g2.completed == ["dup"]
    assert g1.completed == []
    assert registry.tool_groups_by_call_id["dup"] is g2


@pytest.mark.asyncio
async def test_tool_registry_delayed_start_does_not_resurrect_cleared_routing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A start suspended across reset_for_clear must not repopulate the map.

    Clear detaches every transcript widget and empties the routing map;
    the resuming continuation must not re-insert its detached group, or
    the cleared map would hold a dead entry until the next clear.
    """
    entered_add = asyncio.Event()
    release_add = asyncio.Event()

    class _DelayedToolGroup(_FakeToolGroup):
        async def add_tool(
            self,
            call_id: str,
            tool_name: str,
            tool_kind: str,
            args_summary: str = "",
            *,
            args: dict[str, Any] | None = None,
            started_at: float | None = None,
        ) -> None:
            entered_add.set()
            await asyncio.wait_for(release_add.wait(), timeout=5)
            await super().add_tool(call_id, tool_name, tool_kind, args_summary, args=args)

    calls: list[str] = []
    monkeypatch.setattr(tool_registry_module, "ToolGroup", _DelayedToolGroup)
    registry = ToolGroupRegistry(_FakeMount(calls), _FakeUi(calls), _FakeStatus(calls))

    stale_task = asyncio.create_task(registry.add_tool_start("a", "zsh", "shell"))
    await asyncio.wait_for(entered_add.wait(), timeout=5)
    registry.reset_for_clear()
    release_add.set()
    await stale_task

    assert registry.current_tool_group is None
    assert registry.tool_groups_by_call_id == {}
    assert registry.unlinked_sub_agents == {}


@pytest.mark.asyncio
async def test_tool_registry_delayed_sub_agent_start_does_not_requeue_after_end_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stale sub-agent start must not repopulate the cleared fallback queue.

    ``end_group`` clears ``_unlinked_sub_agents``; a start that was blocked
    in ``add_tool`` across that transition must not append its widget again,
    or a later invocation would claim the ended group's stale card.
    """
    from chrys.app.tui.widgets.chat.renderers.sub_agent import SubAgentToolCall

    entered_add = asyncio.Event()
    release_add = asyncio.Event()

    class _DelayedSubAgentToolGroup(_FakeToolGroup):
        async def add_tool(
            self,
            call_id: str,
            tool_name: str,
            tool_kind: str,
            args_summary: str = "",
            *,
            args: dict[str, Any] | None = None,
            started_at: float | None = None,
        ) -> None:
            entered_add.set()
            await asyncio.wait_for(release_add.wait(), timeout=5)
            await super().add_tool(call_id, tool_name, tool_kind, args_summary, args=args)
            self._tools[call_id] = SubAgentToolCall(call_id, tool_name)

    calls: list[str] = []
    monkeypatch.setattr(tool_registry_module, "ToolGroup", _DelayedSubAgentToolGroup)
    mount = _FakeMount(calls)
    registry = ToolGroupRegistry(mount, _FakeUi(calls), _FakeStatus(calls))

    stale_task = asyncio.create_task(registry.add_tool_start("sa", "explore_agent", "sub_agent"))
    await asyncio.wait_for(entered_add.wait(), timeout=5)
    registry.end_group()
    release_add.set()
    await stale_task

    assert registry.unlinked_sub_agents == {}
    assert registry._resolve_sub_agent("inv-1") is None
    # Late results still route to the owning card (owning-map contract).
    old_group = mount.widgets[0]
    assert registry.tool_groups_by_call_id["sa"] is old_group


@pytest.mark.asyncio
async def test_invocation_link_fallback_uses_internal_tool_name_queue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A display name must not be used to claim the tool-name fallback queue."""
    from chrys.app.tui.widgets.chat.renderers.sub_agent import SubAgentToolCall

    class _SubAgentToolGroup(_FakeToolGroup):
        async def add_tool(
            self,
            call_id: str,
            tool_name: str,
            tool_kind: str,
            args_summary: str = "",
            *,
            args: dict[str, Any] | None = None,
            started_at: float | None = None,
        ) -> None:
            await super().add_tool(call_id, tool_name, tool_kind, args_summary, args=args)
            self._tools[call_id] = SubAgentToolCall(call_id, tool_name, args=args)

    calls: list[str] = []
    monkeypatch.setattr(tool_registry_module, "ToolGroup", _SubAgentToolGroup)
    registry = ToolGroupRegistry(_FakeMount(calls), _FakeUi(calls), _FakeStatus(calls))
    await registry.add_tool_start(
        "parent",
        "explore_agent",
        "sub_agent",
        args={"prompt": "investigate"},
    )
    queued = registry.unlinked_sub_agents["explore_agent"][0]

    registry.link_sub_agent_invocation(
        "missing-parent",
        "invocation",
        "Explore Agent",
        "Explore_invocation.json",
        tool_name="explore_agent",
    )
    registry.add_sub_agent_answer("Explore Agent", "invocation", "segment", "Still routed.")
    await registry.add_sub_agent_tool_start(
        "Explore Agent",
        "invocation",
        "read_file",
        {"path": "README.md"},
        "inner",
        tool_kind="filesystem.read",
    )

    assert registry.sub_agent_widgets["invocation"] is queued
    assert registry.unlinked_sub_agents == {}
    assert queued._invocation_id == "invocation"
    assert queued._transcript_profile_name == "Explore Agent"
    assert queued._transcript_journal.has_activity is True
    assert queued._inner_tools["inner"].tool_name == "read_file"


def test_end_group_keeps_live_routing_for_running_sub_agent_parents() -> None:
    """Closing the group must not orphan sub-agent cards whose call still runs.

    Intermediate prose between tool batches ends the current group; a
    parallel sub-agent batch split this way still streams progress and
    inner tool events by invocation id, so routing entries whose parent
    tool call is running stay live. Finished parents retire as before.
    """
    calls: list[str] = []
    registry = ToolGroupRegistry(_FakeMount(calls), _FakeUi(calls), _FakeStatus(calls))
    group = _FakeToolGroup()
    running: Any = _FakeSubAgentWidget("call-run")
    finished: Any = _FakeSubAgentWidget("call-done")
    group._tools["call-run"] = running  # _FakeToolGroup.is_tool_running keys on _tools
    registry.tool_groups_by_call_id.update({"call-run": group, "call-done": group})
    registry.sub_agent_widgets.update({"inv-run": running, "inv-done": finished})

    registry.end_group()

    assert set(registry.sub_agent_widgets) == {"inv-run"}
    registry.update_sub_agent_progress("inv-run", 2, 100, 120)
    registry.update_sub_agent_progress("inv-done", 9, 900, 990)
    assert running.events == [("progress", 2, 100, 120, 0)]
    assert finished.events == []

    # Once the parent call completes, the next group closure retires it.
    del group._tools["call-run"]
    registry.end_group()
    assert registry.sub_agent_widgets == {}


def test_sub_agent_link_after_group_split_routes_by_parent_call_id() -> None:
    """Invocation-start events landing after a mid-batch group split still link.

    Pins the contract the live-routing hardening relies on:
    ``link_sub_agent_invocation`` resolves through the call-id owning map,
    which survives ``end_group`` — unlike the FIFO fallback queue.
    """
    from chrys.app.tui.widgets.chat.renderers.sub_agent import SubAgentToolCall

    calls: list[str] = []
    registry = ToolGroupRegistry(_FakeMount(calls), _FakeUi(calls), _FakeStatus(calls))
    group = _FakeToolGroup()
    first = SubAgentToolCall("sa-1", "explore_agent", args={"prompt": "one"})
    second = SubAgentToolCall("sa-2", "explore_agent", args={"prompt": "two"})
    group._tools.update({"sa-1": first, "sa-2": second})
    registry.tool_groups_by_call_id.update({"sa-1": group, "sa-2": group})
    registry.unlinked_sub_agents["explore_agent"] = [first, second]

    registry.end_group()  # mid-batch prose split

    registry.link_sub_agent_invocation("sa-2", "inv-2", "Explore Agent")
    registry.link_sub_agent_invocation("sa-1", "inv-1", "Explore Agent")

    assert registry.sub_agent_widgets == {"inv-1": first, "inv-2": second}
    assert first._invocation_id == "inv-1"
    assert second._invocation_id == "inv-2"
    assert group.sub_agent_display_names == [
        ("sa-2", "Explore Agent"),
        ("sa-1", "Explore Agent"),
    ]


def test_interrupt_cancels_older_groups_retained_by_live_sub_agent_routes() -> None:
    """Interrupt must cancel every group a live sub-agent route keeps open.

    After a mid-batch split, ``end_group`` retains invocations whose parent
    call still runs in an already-closed group. Cancelling only the current
    group left those records running forever (frozen spinner behind the
    collapsed bar) and their routes alive past teardown.
    """
    calls: list[str] = []
    registry = ToolGroupRegistry(_FakeMount(calls), _FakeUi(calls), _FakeStatus(calls))
    old_group = _FakeToolGroup()
    retained: Any = _FakeSubAgentWidget("call-old")
    old_group._tools["call-old"] = retained
    registry.tool_groups_by_call_id["call-old"] = old_group
    registry.sub_agent_widgets["inv-old"] = retained
    current_group: Any = _FakeToolGroup()
    registry.current_tool_group = current_group

    registry.cancel_running_and_end_group()

    assert "cancel" in old_group.completed
    assert "cancel" in current_group.completed
    assert old_group.collapsed
    assert current_group.collapsed
    # Cancelled records no longer count as running, so the route retires.
    assert registry.sub_agent_widgets == {}
    assert registry.current_tool_group is None


def test_interrupt_cancels_unlinked_older_group_via_call_id_ownership() -> None:
    """Interrupt must cancel older groups even when no invocation has linked.

    A sub-agent whose invocation-start event has not arrived yet has no
    ``_sub_agent_widgets`` entry, and an earlier ``end_group`` cleared the
    unlinked queue — the running record is reachable only through the
    call-id ownership map. Discovering older groups solely through linked
    widgets skipped it, leaving the record visually running after interrupt.
    """
    calls: list[str] = []
    registry = ToolGroupRegistry(_FakeMount(calls), _FakeUi(calls), _FakeStatus(calls))
    old_group = _FakeToolGroup()
    unlinked: Any = _FakeSubAgentWidget("call-unlinked")
    old_group._tools["call-unlinked"] = unlinked
    registry.tool_groups_by_call_id["call-unlinked"] = old_group

    registry.cancel_running_and_end_group()

    assert "cancel" in old_group.completed
    assert old_group.collapsed


@pytest.mark.asyncio
async def test_tool_registry_interrupt_during_status_cleanup_does_not_reopen_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered_cleanup = asyncio.Event()
    release_cleanup = asyncio.Event()

    class _DelayedStatus(_FakeStatus):
        async def remove_trailing_status(self) -> None:
            entered_cleanup.set()
            await asyncio.wait_for(release_cleanup.wait(), timeout=5)
            await super().remove_trailing_status()

    calls: list[str] = []
    monkeypatch.setattr(tool_registry_module, "ToolGroup", _FakeToolGroup)
    registry = ToolGroupRegistry(_FakeMount(calls), _FakeUi(calls), _DelayedStatus(calls))

    add_task = asyncio.create_task(registry.add_tool_start("late", "zsh", "shell"))
    await asyncio.wait_for(entered_cleanup.wait(), timeout=5)
    registry.cancel_running_and_end_group()
    release_cleanup.set()
    await add_task

    assert registry.current_tool_group is None
    assert registry.tool_groups_by_call_id == {}
    assert not any(call.startswith("mount:") for call in calls)


@pytest.mark.asyncio
async def test_tool_registry_concurrent_start_waits_for_group_mount(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A start racing a group mount must wait for readiness, not crash.

    Mirrors the real failure: ``ToolGroup.add_tool`` queries the group's
    composed content container, so touching a group whose mount has not
    completed raises ``NoMatches``.
    """
    entered_mount = asyncio.Event()
    release_mount = asyncio.Event()

    class _DelayedMount(_FakeMount):
        async def mount_transcript_widget(self, widget: Widget) -> bool:
            entered_mount.set()
            await asyncio.wait_for(release_mount.wait(), timeout=5)
            mounted = await super().mount_transcript_widget(widget)
            widget.mount_completed = True
            return mounted

    class _MountAwareToolGroup(_FakeToolGroup):
        def __init__(self, **options: Unpack[_ToolGroupOptions]) -> None:
            super().__init__(**options)
            self.mount_completed = False

        async def add_tool(
            self,
            call_id: str,
            tool_name: str,
            tool_kind: str,
            args_summary: str = "",
            *,
            args: dict[str, Any] | None = None,
            started_at: float | None = None,
        ) -> None:
            assert self.mount_completed, "add_tool touched a group before its mount finished"
            await super().add_tool(call_id, tool_name, tool_kind, args_summary, args=args)

    calls: list[str] = []
    monkeypatch.setattr(tool_registry_module, "ToolGroup", _MountAwareToolGroup)
    registry = ToolGroupRegistry(_DelayedMount(calls), _FakeUi(calls), _FakeStatus(calls))

    first = asyncio.create_task(registry.add_tool_start("a", "zsh", "shell"))
    await asyncio.wait_for(entered_mount.wait(), timeout=5)
    # The group must not be observable until its mount completes.
    assert registry.current_tool_group is None
    second = asyncio.create_task(registry.add_tool_start("b", "read_file", "filesystem.read"))
    await asyncio.sleep(0)
    release_mount.set()
    await asyncio.gather(first, second)

    group = registry.current_tool_group
    assert isinstance(group, _MountAwareToolGroup)
    assert [added[0] for added in group.added] == ["a", "b"]
    assert calls.count("mount:_MountAwareToolGroup") == 1
    assert registry.tool_groups_by_call_id == {"a": group, "b": group}


@pytest.mark.asyncio
async def test_tool_registry_interrupt_during_group_mount_does_not_add_late_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered_mount = asyncio.Event()
    release_mount = asyncio.Event()

    class _DelayedMount(_FakeMount):
        pending: Widget | None = None

        async def mount_transcript_widget(self, widget: Widget) -> bool:
            self.pending = widget
            entered_mount.set()
            await asyncio.wait_for(release_mount.wait(), timeout=5)
            return await super().mount_transcript_widget(widget)

    calls: list[str] = []
    monkeypatch.setattr(tool_registry_module, "ToolGroup", _FakeToolGroup)
    mount = _DelayedMount(calls)
    registry = ToolGroupRegistry(mount, _FakeUi(calls), _FakeStatus(calls))

    add_task = asyncio.create_task(registry.add_tool_start("late", "zsh", "shell"))
    await asyncio.wait_for(entered_mount.wait(), timeout=5)
    # Publish-after-mount: the group is not observable while it mounts.
    assert registry.current_tool_group is None
    registry.cancel_running_and_end_group()
    release_mount.set()
    await add_task

    group = mount.pending
    assert isinstance(group, _FakeToolGroup)
    assert group.added == []
    assert mount.widgets == []
    assert registry.current_tool_group is None
    assert registry.tool_groups_by_call_id == {}


@pytest.mark.asyncio
async def test_tool_registry_rejects_group_when_mount_parent_is_pruning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A no-op Textual mount must not publish an unattached tool group."""

    class _RejectedMount(_FakeMount):
        async def mount_transcript_widget(self, widget: Widget) -> bool:
            self.calls.append(f"reject_mount:{type(widget).__name__}")
            return False

    calls: list[str] = []
    monkeypatch.setattr(tool_registry_module, "ToolGroup", _FakeToolGroup)
    registry = ToolGroupRegistry(_RejectedMount(calls), _FakeUi(calls), _FakeStatus(calls))

    await registry.add_tool_start("late", "write_file", "filesystem.write")

    assert registry.current_tool_group is None
    assert registry.tool_groups_by_call_id == {}
    assert calls == ["remove_status", "reject_mount:_FakeToolGroup", "remove:_FakeToolGroup"]


def test_tool_registry_inline_ask_user_scrolls_only_after_success(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    registry = ToolGroupRegistry(_FakeMount(calls), _FakeUi(calls), _FakeStatus(calls))
    group = _FakeToolGroup()
    widget = AskUserToolCall("ask-1", "ask_user", "", args={"question": "Proceed?"})
    group._tools["ask-1"] = widget
    registry.tool_groups_by_call_id["ask-1"] = group  # type: ignore[assignment]

    assert registry.show_ask_user_inline("missing", "req", ()) is False
    assert calls == []

    monkeypatch.setattr(AskUserToolCall, "show_inline_prompt", lambda self, *args, **kwargs: True)
    questions = (AskUserQuestion(question="Proceed?", options=(AskUserOption(label="yes"),)),)
    assert registry.show_ask_user_inline("ask-1", "req", questions) is True

    assert group.locked == ["ask-1"]
    assert calls == ["inline_scroll:AskUserToolCall"]


@pytest.mark.asyncio
async def test_live_renderer_only_removes_status_until_backend_cleanup() -> None:
    calls: list[str] = []
    status = _FakeStatus(calls)
    error = ErrorMessage("failed")
    user = UserMessage("prompt")

    async def remove_error() -> None:
        calls.append("remove_error")

    async def remove_user() -> None:
        calls.append("remove_user")

    error.remove = remove_error  # type: ignore[method-assign]
    user.remove = remove_user  # type: ignore[method-assign]
    status.children = [user, error]
    toc = TurnTocModel()
    turn_id, _item = toc.begin_user_turn("prompt")
    user.id = turn_id
    renderer = LiveTranscriptRenderer(
        _FakeMount(calls),
        _FakeUi(calls),
        status,
        _FakeQuery(),
        toc,
        ToolGroupRegistry(_FakeMount(calls), _FakeUi(calls), status),
    )

    await renderer.add_user_message("new prompt")

    assert calls[:4] == ["dismiss_welcome", "live_user_started", "failed_status", "remove_error"]
    assert "preceding_content" not in calls
    assert "remove_user" not in calls
    assert [item.summary for item in toc.items()] == ["prompt", "new prompt"]
    assert toc.items()[-1].turn_index is None


@pytest.mark.asyncio
async def test_live_renderer_whitespace_only_answer_segment_is_noop() -> None:
    calls: list[str] = []
    status = _FakeStatus(calls)
    registry = ToolGroupRegistry(_FakeMount(calls), _FakeUi(calls), status)
    group = _FakeToolGroup()
    registry.current_tool_group = group  # type: ignore[assignment]
    renderer = LiveTranscriptRenderer(
        _FakeMount(calls),
        _FakeUi(calls),
        status,
        _FakeQuery(),
        TurnTocModel(),
        registry,
    )

    await renderer.add_answer_delta("blank", "   \n")
    renderer.finish_answer_segment("blank", completed=True)

    assert calls == []
    assert registry.current_tool_group is group


@pytest.mark.asyncio
async def test_live_renderer_derives_checkmark_for_successful_structured_only_output() -> None:
    calls: list[str] = []
    status = _FakeStatus(calls)
    mount = _FakeMount(calls)
    renderer = LiveTranscriptRenderer(
        mount,
        _FakeUi(calls),
        status,
        _FakeQuery(),
        TurnTocModel(),
        ToolGroupRegistry(_FakeMount(calls), _FakeUi(calls), status),
    )
    renderer.profile_name = "Code Agent"

    await renderer.add_agent_message("", structured_output_completed=True)
    await renderer.add_agent_message("")

    messages = [widget for widget in mount.widgets if isinstance(widget, AgentMessage)]
    assert len(messages) == 1
    assert messages[0].text == "✓"
    assert messages[0]._is_structured_completion is True


@pytest.mark.asyncio
async def test_live_renderer_normalizes_whitespace_structured_completion_to_checkmark() -> None:
    calls: list[str] = []
    status = _FakeStatus(calls)
    mount = _FakeMount(calls)
    renderer = LiveTranscriptRenderer(
        mount,
        _FakeUi(calls),
        status,
        _FakeQuery(),
        TurnTocModel(),
        ToolGroupRegistry(_FakeMount(calls), _FakeUi(calls), status),
    )

    await renderer.add_agent_message(" \n\t", structured_output_completed=True)

    messages = [widget for widget in mount.widgets if isinstance(widget, AgentMessage)]
    assert len(messages) == 1
    assert messages[0].text == "✓"
    assert messages[0]._is_structured_completion is True


@pytest.mark.asyncio
async def test_live_renderer_streams_each_segment_into_its_own_message_and_never_repeats_the_final() -> None:
    calls: list[str] = []
    status = _FakeStatus(calls)
    mount = _FakeMount(calls)
    renderer = LiveTranscriptRenderer(
        mount,
        _FakeUi(calls),
        status,
        _FakeQuery(),
        TurnTocModel(),
        ToolGroupRegistry(_FakeMount(calls), _FakeUi(calls), status),
    )

    await renderer.add_answer_delta("partial", "Half an ")
    await renderer.add_answer_delta("partial", "answer")
    # A failed segment keeps its text; nothing is retracted.
    renderer.finish_answer_segment("partial", completed=False)
    await renderer.add_answer_delta("answer", "\n\nThe ")
    await renderer.add_answer_delta("answer", "answer.")
    renderer.finish_answer_segment("answer", completed=True)
    await renderer.add_agent_message("The answer.")

    messages = [widget for widget in mount.widgets if isinstance(widget, AgentMessage)]
    assert [(message.text, message._is_final) for message in messages] == [
        ("Half an answer", True),
        ("The answer.", True),
    ]
    assert "remove:AgentMessage" not in calls


@pytest.mark.asyncio
async def test_replay_renderer_start_order_and_finish_use_ui_port() -> None:
    calls: list[str] = []
    renderer = HistoryReplayRenderer(
        _FakeMount(calls),
        _FakeUi(calls),
        TurnTocModel(),
        lambda _name: "",
        default_profile=lambda: "Code",
    )

    await renderer.replay_history([])

    assert calls == ["dismiss_welcome", "replay_started", "after_replay"]


def _compressed_fold_message(context_id: str) -> dict[str, Any]:
    return {
        "role": "assistant",
        "contents": [{"type": "text", "text": f"[Compressed context: {context_id}]\nSummary: condensed"}],
        "additional_properties": {HistoryMarkerKind.KEY: HistoryMarkerKind.SUMMARY, "_block_id": context_id},
    }


def _block_user(text: str, **extra: Any) -> Message:
    msg = Message(role="user", contents=[Content.from_text(text)])
    msg.additional_properties.update(extra)
    return msg


@pytest.mark.asyncio
async def test_replay_context_fold_uses_persisted_turn_range() -> None:
    calls: list[str] = []
    mount = _FakeMount(calls)
    renderer = HistoryReplayRenderer(
        mount,
        _FakeUi(calls),
        TurnTocModel(),
        lambda _name: "",
        default_profile=lambda: "Code",
    )
    block = CompressedBlock(compressed_context_id="ctx-1", turn_range=(7, 7))

    await renderer.replay_history([_compressed_fold_message("ctx-1")], compressed_blocks={"ctx-1": block})

    fold = next(widget for widget in mount.widgets if isinstance(widget, ContextFoldWidget))
    assert fold.render().plain.splitlines()[0] == "  ═══ Compressed (Turn 7) ═══"


@pytest.mark.asyncio
async def test_replay_legacy_context_fold_uses_persisted_message_count() -> None:
    calls: list[str] = []
    mount = _FakeMount(calls)
    renderer = HistoryReplayRenderer(
        mount,
        _FakeUi(calls),
        TurnTocModel(),
        lambda _name: "",
        default_profile=lambda: "Code",
    )
    block = CompressedBlock(
        compressed_context_id="ctx-legacy",
        messages=[_block_user("first"), _block_user("second")],
    )

    await renderer.replay_history(
        [_compressed_fold_message("ctx-legacy")],
        compressed_blocks={"ctx-legacy": block},
    )

    fold = next(widget for widget in mount.widgets if isinstance(widget, ContextFoldWidget))
    assert fold.render().plain.splitlines()[0] == "  ═══ Compressed (2 messages) ═══"


@pytest.mark.asyncio
async def test_replay_compressed_block_skips_flagged_nudge_keeps_injection() -> None:
    """A flagged synthetic ``continue`` inside a compressed block mounts no
    widget; the opener and the flagged injection still mount."""
    calls: list[str] = []
    mount = _FakeMount(calls)
    renderer = HistoryReplayRenderer(
        mount,
        _FakeUi(calls),
        TurnTocModel(),
        lambda _name: "",
        default_profile=lambda: "Code",
    )
    block = CompressedBlock(
        compressed_context_id="ctx-1",
        messages=[
            _block_user("start task"),
            _block_user("continue", **{HistoryMarkerKind.CONTINUATION_KEY: True}),
            _block_user("mid-turn note", **{HistoryMarkerKind.INJECTED_KEY: True}),
        ],
    )

    await renderer.replay_history([_compressed_fold_message("ctx-1")], compressed_blocks={"ctx-1": block})

    users = [w for w in mount.widgets if isinstance(w, UserMessage)]
    assert [(w._text, w.is_injection) for w in users] == [("start task", False), ("mid-turn note", True)]


@pytest.mark.asyncio
async def test_replay_compressed_block_nudge_does_not_flip_seen_user() -> None:
    """If the skipped nudge flipped ``seen_user``, the following real opener
    would mount as an injection."""
    calls: list[str] = []
    mount = _FakeMount(calls)
    renderer = HistoryReplayRenderer(
        mount,
        _FakeUi(calls),
        TurnTocModel(),
        lambda _name: "",
        default_profile=lambda: "Code",
    )
    block = CompressedBlock(
        compressed_context_id="ctx-1",
        messages=[
            _block_user("continue", **{HistoryMarkerKind.CONTINUATION_KEY: True}),
            _block_user("start task"),
        ],
    )

    await renderer.replay_history([_compressed_fold_message("ctx-1")], compressed_blocks={"ctx-1": block})

    users = [w for w in mount.widgets if isinstance(w, UserMessage)]
    assert [(w._text, w.is_injection) for w in users] == [("start task", False)]


@pytest.mark.asyncio
async def test_replay_compressed_block_groups_consecutive_hosted_calls() -> None:
    calls: list[str] = []
    mount = _FakeMount(calls)
    renderer = HistoryReplayRenderer(
        mount,
        _FakeUi(calls),
        TurnTocModel(),
        lambda _name: "",
        default_profile=lambda: "Code",
    )
    contents: list[Content] = []
    for index in range(2):
        provider_id = f"search-{index}"
        contents.extend(
            [
                Content.from_hosted_tool_call(
                    provider_id,
                    tool_name="web_search",
                    arguments={"query": str(index)},
                    status="completed",
                    hosted_family="search",
                    hosted_provider="openai",
                    provider_phase="terminal",
                ),
                Content.from_hosted_tool_result(
                    provider_id,
                    tool_name="web_search",
                    items=[Content.from_text(f"result {index}")],
                    status="completed",
                    hosted_family="search",
                    hosted_provider="openai",
                    provider_phase="terminal",
                ),
            ]
        )
    block = CompressedBlock(compressed_context_id="ctx-hosted", messages=[Message("assistant", contents)])

    await renderer.replay_history(
        [_compressed_fold_message("ctx-hosted")],
        compressed_blocks={"ctx-hosted": block},
    )

    groups = [widget for widget in mount.widgets if isinstance(widget, ToolGroup)]
    assert len(groups) == 1
    assert len(groups[0]._tool_records) == 2
    assert [record.result for record in groups[0]._tool_records.values()] == ["result 0", "result 1"]


@pytest.mark.asyncio
async def test_replay_compressed_block_pairs_hosted_result_from_later_message() -> None:
    calls: list[str] = []
    mount = _FakeMount(calls)
    renderer = HistoryReplayRenderer(
        mount,
        _FakeUi(calls),
        TurnTocModel(),
        lambda _name: "",
        default_profile=lambda: "Code",
    )
    call = Content.from_hosted_tool_call(
        "anthropic-search",
        tool_name="web_search",
        arguments={"query": "Chrys"},
        status="running",
        hosted_family="search",
        hosted_provider="anthropic",
        provider_phase="start",
        provider_status="running",
    )
    result = Content.from_hosted_tool_result(
        "anthropic-search",
        tool_name="web_search",
        items=[Content.from_text("paired result")],
        status="completed",
        hosted_family="search",
        hosted_provider="anthropic",
        provider_phase="terminal",
        provider_status="completed",
    )
    block = CompressedBlock(
        compressed_context_id="ctx-hosted-split",
        messages=[Message("assistant", [call]), Message("assistant", [result])],
    )

    await renderer.replay_history(
        [_compressed_fold_message("ctx-hosted-split")],
        compressed_blocks={"ctx-hosted-split": block},
    )

    group = next(widget for widget in mount.widgets if isinstance(widget, ToolGroup))
    record = next(iter(group._tool_records.values()))
    assert record.result == "paired result"
    assert record.status == "complete"
    assert record.canonical_status == "completed"
    assert record.provider_status == "completed"


@pytest.mark.asyncio
async def test_replay_compressed_block_preserves_hosted_images_and_artifacts() -> None:
    calls: list[str] = []
    mount = _FakeMount(calls)
    renderer = HistoryReplayRenderer(
        mount,
        _FakeUi(calls),
        TurnTocModel(),
        lambda _name: "",
        default_profile=lambda: "Code",
    )
    image = Content.from_uri("data:image/png;base64,AA==", media_type="image/png")
    artifact = Content.from_hosted_file("file_1", media_type="text/plain", name="result.txt")
    call = Content.from_hosted_tool_call(
        "image-1",
        tool_name="image_generation",
        status="running",
        hosted_family="image",
        hosted_provider="anthropic",
        provider_phase="start",
        provider_status="running",
    )
    result = Content.from_hosted_tool_result(
        "image-1",
        tool_name="image_generation",
        items=[image, artifact],
        status="completed",
        hosted_family="image",
        hosted_provider="anthropic",
        provider_phase="terminal",
        provider_status="completed",
    )
    block = CompressedBlock(
        compressed_context_id="ctx-hosted-structured",
        messages=[Message("assistant", [call]), Message("assistant", [result])],
    )

    await renderer.replay_history(
        [_compressed_fold_message("ctx-hosted-structured")],
        compressed_blocks={"ctx-hosted-structured": block},
    )

    group = next(widget for widget in mount.widgets if isinstance(widget, ToolGroup))
    record = next(iter(group._tool_records.values()))
    assert record.image_contents == [image.to_dict()]
    assert record.artifacts == [{"id": "file_1", "path": "result.txt", "mime": "text/plain"}]


@pytest.mark.asyncio
async def test_replay_compressed_block_keeps_local_tool_and_result_in_a_dimmed_group() -> None:
    calls: list[str] = []
    mount = _FakeMount(calls)
    renderer = HistoryReplayRenderer(
        mount,
        _FakeUi(calls),
        TurnTocModel(),
        lambda _name: "filesystem.read",
        default_profile=lambda: "Code",
    )
    block = CompressedBlock(
        compressed_context_id="ctx-local-tool",
        messages=[
            Message(
                "assistant",
                [Content.from_function_call("local-1", "read_file", arguments={"path": "README.md"})],
            ),
            Message("tool", [Content.from_function_result("local-1", result="contents")]),
        ],
    )

    await renderer.replay_history(
        [_compressed_fold_message("ctx-local-tool")],
        compressed_blocks={"ctx-local-tool": block},
    )

    groups = [widget for widget in mount.widgets if isinstance(widget, ToolGroup)]
    assert len(groups) == 1
    assert groups[0].has_class("--compressed")
    assert [(record.tool_name, record.result) for record in groups[0]._tool_records.values()] == [
        ("read_file", "contents")
    ]


@pytest.mark.asyncio
async def test_replay_compressed_block_skips_tool_summary_but_keeps_archived_originals() -> None:
    """A compressed block archives its turns as they were, including a
    compaction tool summary beside the excluded originals it replaced.

    Unlike the session transcript, which loads without excluded messages,
    the block renders those originals by its own rules: their narration
    and local tool cards both show. Only the summary is skipped; a
    real answer that reads exactly like it still renders.
    """
    calls: list[str] = []
    mount = _FakeMount(calls)
    renderer = HistoryReplayRenderer(
        mount,
        _FakeUi(calls),
        TurnTocModel(),
        lambda _name: "",
        default_profile=lambda: "Code",
    )
    summary_text = '[Tool call: read_file(path="a.py") → contents]'
    summary = Message(
        "assistant",
        [Content.from_text(summary_text)],
        additional_properties={
            GROUP_ANNOTATION_KEY: {
                "id": "group_1",
                "kind": "assistant_text",
                SUMMARY_OF_MESSAGE_IDS_KEY: ["msg_1", "msg_2"],
                SUMMARY_OF_GROUP_IDS_KEY: ["group_1"],
            }
        },
    )
    originals = [
        Message(
            "assistant",
            [
                Content.from_text("Reading a.py first."),
                Content.from_function_call("c1", "read_file", arguments={"path": "a.py"}),
            ],
        ),
        Message("tool", [Content.from_function_result("c1", result="contents")]),
    ]
    for original in originals:
        set_excluded(original, excluded=True, reason="budget_tool_compaction")
    block = CompressedBlock(
        compressed_context_id="ctx-summary",
        messages=[
            _block_user("inspect"),
            summary,
            *originals,
            Message("assistant", [Content.from_text(summary_text)]),
        ],
    )

    await renderer.replay_history(
        [_compressed_fold_message("ctx-summary")],
        compressed_blocks={"ctx-summary": block},
    )

    assert [widget._text for widget in mount.widgets if isinstance(widget, UserMessage)] == ["inspect"]
    assert [widget.text for widget in mount.widgets if isinstance(widget, AgentMessage)] == [
        "Reading a.py first.",
        summary_text,
    ]
    groups = [widget for widget in mount.widgets if isinstance(widget, ToolGroup)]
    assert len(groups) == 1
    assert groups[0].has_class("--compressed")
    assert [(record.tool_name, record.result) for record in groups[0]._tool_records.values()] == [
        ("read_file", "contents")
    ]


@pytest.mark.asyncio
async def test_replay_derives_checkmark_for_turn_ending_structured_only_output() -> None:
    calls: list[str] = []
    mount = _FakeMount(calls)
    renderer = HistoryReplayRenderer(
        mount,
        _FakeUi(calls),
        TurnTocModel(),
        lambda _name: "",
        default_profile=lambda: "Code Agent",
    )
    call = Content.from_image_generation_tool_call(
        image_id="img_1",
        hosted_provider="openai",
        provider_phase="start",
        provider_status="generating",
    )
    result = Content.from_image_generation_tool_result(
        image_id="img_1",
        outputs=[Content.from_uri("data:image/png;base64,AAA", media_type="image/png")],
        hosted_provider="openai",
        provider_phase="terminal",
        provider_status="completed",
    )
    messages = [
        Message("user", [Content.from_text("make an image")]).to_dict(),
        Message("assistant", [call, result, Content.from_text("")]).to_dict(),
        Message(
            "assistant",
            [Content.from_text("")],
            additional_properties={HistoryMarkerKind.KEY: HistoryMarkerKind.TURN},
        ).to_dict(),
    ]

    await renderer.replay_history(messages)

    agent_messages = [widget for widget in mount.widgets if isinstance(widget, AgentMessage)]
    assert len(agent_messages) == 1
    assert agent_messages[0].text == "✓"
    assert agent_messages[0]._is_structured_completion is True


@pytest.mark.asyncio
async def test_large_hosted_restore_keeps_500_plus_cards_as_lazy_records() -> None:
    calls: list[str] = []
    mount = _FakeMount(calls)
    renderer = HistoryReplayRenderer(
        mount,
        _FakeUi(calls),
        TurnTocModel(),
        lambda _name: "",
        default_profile=lambda: "Code",
    )
    hosted_calls = [
        Content.from_hosted_tool_call(
            f"search-{index}",
            tool_name="web_search",
            arguments={"query": str(index)},
            status="completed",
            hosted_family="search",
            hosted_provider="openai",
            provider_phase="terminal",
        ).to_dict()
        for index in range(520)
    ]

    await renderer.replay_history([{"role": "assistant", "contents": hosted_calls}])

    groups = [widget for widget in mount.widgets if isinstance(widget, ToolGroup)]
    assert len(groups) == 1
    assert len(groups[0]._tool_records) == 520
    assert groups[0]._tools == {}
    assert groups[0]._content_mounted is False
