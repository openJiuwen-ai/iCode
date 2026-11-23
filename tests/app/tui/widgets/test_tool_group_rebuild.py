# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for ToolGroup prune/rebuild: collapsed-group pruning, inline diff deferral, expand/collapse races, awaited removal, structure locks, and replay metadata."""

from __future__ import annotations

import asyncio
import logging

import pytest
from textual.pilot import Pilot
from textual.widget import Widget
from textual.widgets import Static

from chrys.app.tui.support import gc_freeze
from chrys.app.tui.support.gc_freeze import (
    GcAbsorbReason,
    GcAbsorbRequested,
    GcReclaimReason,
    GcReclaimRequested,
    GcRemovedContent,
)
from chrys.app.tui.widgets.chat.live_process_row import LiveProcessRow
from chrys.app.tui.widgets.chat.panel import ChatPanel
from chrys.app.tui.widgets.chat.process_block import ProcessBlock
from chrys.app.tui.widgets.chat.renderers.sub_agent import SubAgentToolCall
from chrys.app.tui.widgets.chat.tool_call import (
    ToolCall,
    ToolGroup,
)
from chrys.app.tui.widgets.chat.tool_renderers import register_kind_renderer
from chrys.foundation.tool_kinds import (
    KIND_FILESYSTEM_WRITE,
    KIND_SHELL,
    KIND_SUB_AGENT,
)
from chrys.foundation.tool_result_metadata import (
    TOOL_FAILED_METADATA_KEY,
)
from tests.support.tui_helpers import (
    ChatPanelApp,
    GcMessageChatPanelApp,
    LocalizedWidgetApp,
    exec_panel_text,
    inline_diff_mount_state,
)
from tests.support.waiting import wait_for

# Tests here call register_kind_renderer(KIND_SUB_AGENT, ...) directly; undo it per test.
pytestmark = pytest.mark.usefixtures("restore_kind_renderer_registry")


def _show_process(group: ToolGroup) -> None:
    """Open the containing process before exercising an individual group's restore."""
    panel = group.parent
    if isinstance(panel, ChatPanel):
        owner = panel._process_controller.owner_of(group)
        if owner is not None and owner.collapsed:
            if isinstance(owner, LiveProcessRow):
                panel._toggle_live_process_row(owner)
            elif isinstance(owner, ProcessBlock):
                panel._toggle_process_block(owner)


def _model_coordinator_freeze(monkeypatch: pytest.MonkeyPatch) -> None:
    """Advance the GC freeze epoch as a coordinator freeze would, without freezing the process."""
    monkeypatch.setattr(gc_freeze, "_freeze_epoch", gc_freeze.current_gc_freeze_epoch() + 1)


async def _prune_drained(group: ToolGroup, pilot: Pilot[None]) -> None:
    await wait_for(lambda: not group._content_mounted, pilot=pilot, description="completed subtree pruned")
    release_drained = asyncio.Event()
    assert group.call_later(release_drained.set)
    await wait_for(release_drained.is_set, pilot=pilot, description="prune callbacks drain")


async def _start_release_with_held_removal(
    app: GcMessageChatPanelApp, monkeypatch: pytest.MonkeyPatch, call_id: str
) -> tuple[ToolGroup, Widget, asyncio.Event, asyncio.Task[None]]:
    """Complete one *call_id* tool, then start a collapsed release whose subtree removal is held.

    Returns the group, its content container, the event that lets the removal
    proceed, and the release task, so a test can race work into the window.
    """
    panel = app.query_one(ChatPanel)
    await panel.add_user_message("tools")
    await panel.add_tool_start(call_id, "plain_tool", "", args={"value": 1})
    await panel.add_tool_result(call_id, "plain_tool", "done", 10)
    group = panel.query_one(ToolGroup)
    content = group.query_one("#tg-content")
    original_remove_children = content.remove_children
    removal_started = asyncio.Event()
    release_removal = asyncio.Event()

    def delayed_remove_children():
        pending_remove = original_remove_children()
        removal_started.set()

        async def wait_for_release():
            await release_removal.wait()
            return await pending_remove

        return wait_for_release()

    monkeypatch.setattr(group, "call_later", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(content, "remove_children", delayed_remove_children)
    group.collapsed = True
    release_task = asyncio.create_task(group._release_completed_tool_widgets())
    await removal_started.wait()
    return group, content, release_removal, release_task


async def test_collapsed_completed_tool_group_prunes_and_rebuilds_file_diff() -> None:
    """Collapsed historical file tools should not keep their diff widget subtree mounted."""
    from chrys.app.tui.widgets.diff_view import DiffView
    from chrys.app.tui.widgets.diff_view.inline import InlineUnifiedDiffLines

    async with ChatPanelApp().run_test() as pilot:
        cp = pilot.app.query_one(ChatPanel)
        await cp.add_user_message("edit")
        await cp.add_tool_start("edit1", "edit_file", "filesystem.write", args={"path": "src/app.py"})
        _show_process(cp.query_one(ToolGroup))
        await cp.add_tool_result(
            "edit1",
            "edit_file",
            "Edited src/app.py",
            25,
            file_snapshot=("old\n", "new\n"),
        )
        await cp.add_agent_message("done")

        group = cp.query_one(ToolGroup)
        await wait_for(lambda: not group._content_mounted, pilot=pilot, description="not group._content_mounted")

        assert group.collapsed is True
        assert group.all_complete is True
        assert group._content_mounted is False
        assert len(group._tools) == 0
        assert not list(cp.query(DiffView))

        _show_process(group)
        group.collapsed = False
        await wait_for(
            lambda: list(cp.query(InlineUnifiedDiffLines)),
            pilot=pilot,
            description="list(cp.query(InlineUnifiedDiffLines))",
        )

        assert group._content_mounted is True
        assert len(group._tools) == 1
        assert not list(cp.query(DiffView))
        assert list(cp.query(InlineUnifiedDiffLines))


async def test_completed_tool_subtree_posts_idle_reclaim_after_awaited_removal() -> None:
    app = GcMessageChatPanelApp()
    async with app.run_test() as pilot:
        panel = app.query_one(ChatPanel)
        await panel.add_user_message("tools")
        await panel.add_tool_start("tool1", "plain_tool", "", args={"value": 1})
        await panel.add_tool_result("tool1", "plain_tool", "done", 10)
        await panel.add_agent_message("done")
        group = panel.query_one(ToolGroup)

        await wait_for(lambda: app.gc_messages, pilot=pilot, description="app.gc_messages")

        assert group._content_mounted is False
        assert not group.query_one("#tg-content").children
        assert len(app.gc_messages) == 1
        message = app.gc_messages[0]
        assert isinstance(message, GcReclaimRequested)
        assert message.reason is GcReclaimReason.STABLE_CONTENT_REMOVED
        assert message.prompt is False

        await group._release_completed_tool_widgets()
        await pilot.pause()
        assert len(app.gc_messages) == 1


@pytest.mark.parametrize("frozen_after_build", [False, True], ids=["young", "frozen"])
async def test_completed_tool_subtree_removal_names_its_nodes_only_while_no_freeze_captured_them(
    monkeypatch: pytest.MonkeyPatch, frozen_after_build: bool
) -> None:
    app = GcMessageChatPanelApp()
    async with app.run_test() as pilot:
        panel = app.query_one(ChatPanel)
        await panel.add_user_message("tools")
        built_at_epoch = gc_freeze.current_gc_freeze_epoch()
        await panel.add_tool_start("tool1", "plain_tool", "", args={"value": 1})
        await panel.add_tool_result("tool1", "plain_tool", "done", 10)
        group = panel.query_one(ToolGroup)
        tool = group.get_tool("tool1")
        assert tool is not None
        subtree = tool.walk_children(with_self=True)
        if frozen_after_build:
            _model_coordinator_freeze(monkeypatch)
        await panel.add_agent_message("done")

        await _prune_drained(group, pilot)

        assert len(app.gc_messages) == 1
        message = app.gc_messages[0]
        assert isinstance(message, GcReclaimRequested)
        assert message.reason is GcReclaimReason.STABLE_CONTENT_REMOVED
        assert message.prompt is False
        if frozen_after_build:
            assert message.removed is None
        else:
            assert isinstance(message.removed, GcRemovedContent)
            assert message.removed.built_at_epoch == built_at_epoch
            assert [node() for node in message.removed.nodes] == subtree


async def test_restored_tool_subtree_removal_names_its_nodes_only_while_no_freeze_captured_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = GcMessageChatPanelApp()
    async with app.run_test() as pilot:
        panel = app.query_one(ChatPanel)
        await panel.add_user_message("tools")
        await panel.add_tool_start("tool1", "plain_tool", "", args={"value": 1})
        await panel.add_tool_result("tool1", "plain_tool", "done", 10)
        await panel.add_agent_message("done")
        group = panel.query_one(ToolGroup)
        await _prune_drained(group, pilot)

        for frozen_after_restore in (False, True):
            app.gc_messages.clear()
            _show_process(group)
            group.collapsed = False
            await wait_for(lambda: group._content_mounted, pilot=pilot, description="subtree restored")
            if frozen_after_restore:
                _model_coordinator_freeze(monkeypatch)
            group.collapsed = True
            await _prune_drained(group, pilot)

            removals = [message for message in app.gc_messages if isinstance(message, GcReclaimRequested)]
            assert len(removals) == 1
            assert (removals[0].removed is None) is frozen_after_restore


async def test_plain_restored_tool_subtree_posts_stable_content_absorb() -> None:
    app = GcMessageChatPanelApp()
    async with app.run_test() as pilot:
        panel = app.query_one(ChatPanel)
        await panel.add_user_message("tools")
        await panel.add_tool_start("tool1", "plain_tool", "", args={"value": 1})
        await panel.add_tool_result("tool1", "plain_tool", "done", 10)
        await panel.add_agent_message("done")
        group = panel.query_one(ToolGroup)

        await wait_for(
            lambda: not group._content_mounted and app.gc_messages,
            pilot=pilot,
            description="not group._content_mounted and app.gc_messages",
        )
        assert isinstance(app.gc_messages[-1], GcReclaimRequested)

        # Model the coordinator having consumed the removal reclaim. The
        # subsequent expansion must independently announce its rebuilt tree.
        app.gc_messages.clear()
        _show_process(group)
        group.collapsed = False
        await wait_for(lambda: app.gc_messages, pilot=pilot, description="app.gc_messages")

        assert group._content_mounted is True
        assert len(group.query_one("#tg-content").children) == 1
        assert len(app.gc_messages) == 1
        message = app.gc_messages[0]
        assert isinstance(message, GcAbsorbRequested)
        assert message.reason is GcAbsorbReason.STABLE_CONTENT_MOUNTED
        assert message.terminal_boundary is False


async def test_lazy_tool_expand_posts_stable_content_absorb_after_mounts_finish() -> None:
    app = GcMessageChatPanelApp()
    async with app.run_test() as pilot:
        panel = app.query_one(ChatPanel)
        await panel.add_user_message("edit")
        group = ToolGroup()
        await panel.mount(group)
        await group.add_collapsed_replay_tool(
            "edit1",
            "edit_file",
            "filesystem.write",
            args={"path": "src/app.py"},
            result="Edited src/app.py",
            file_snapshot=("old\n", "new\n"),
            lazy=True,
        )
        await pilot.pause()

        _show_process(group)
        group.collapsed = False
        await wait_for(
            lambda: any(isinstance(message, GcAbsorbRequested) for message in app.gc_messages),
            pilot=pilot,
            description="any(isinstance(message, GcAbsorbRequested) for message in app.gc_messages)",
        )

        absorbs = [message for message in app.gc_messages if isinstance(message, GcAbsorbRequested)]
        assert len(absorbs) == 1
        assert absorbs[0].reason is GcAbsorbReason.STABLE_CONTENT_MOUNTED
        assert absorbs[0].terminal_boundary is False


async def test_subagent_final_answer_does_not_mount_deferred_inline_markdown() -> None:
    register_kind_renderer(KIND_SUB_AGENT, SubAgentToolCall)
    app = GcMessageChatPanelApp()
    large_result = "# Result\n\n" + "\n".join(f"- item {index}" for index in range(2_000))
    async with app.run_test() as pilot:
        panel = app.query_one(ChatPanel)
        panel.set_tool_kinds({"explore_agent": KIND_SUB_AGENT})
        await panel.add_user_message("delegate")
        await panel.add_tool_start("sa1", "explore_agent", KIND_SUB_AGENT, args={"prompt": "investigate"})
        await panel.add_tool_start("sibling", "plain_tool", "", args={"value": 1})
        await panel.add_tool_result("sa1", "explore_agent", large_result, 2500)
        subagent = panel.query_one(SubAgentToolCall)
        await pilot.pause()

        assert subagent.has_class("-done")
        assert not list(subagent.query("#sa-result"))
        assert app.gc_messages == []


async def test_tool_group_expand_restores_cards_before_pending_diff_prepare_finishes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Expand should not block cheap card restore on deferred diff preparation."""
    from chrys.app.tui.widgets.diff_view.inline import InlineUnifiedDiffLines

    prepare_started = asyncio.Event()
    release_prepare = asyncio.Event()
    original_prepare = InlineUnifiedDiffLines.prepare

    async def delayed_prepare(self: InlineUnifiedDiffLines) -> None:
        prepare_started.set()
        await release_prepare.wait()
        await original_prepare(self)

    monkeypatch.setattr(InlineUnifiedDiffLines, "prepare", delayed_prepare)

    async with ChatPanelApp().run_test() as pilot:
        cp = pilot.app.query_one(ChatPanel)
        await cp.add_user_message("edit")
        await cp.add_tool_start("edit1", "edit_file", "filesystem.write", args={"path": "src/app.py"})
        group = cp.query_one(ToolGroup)
        group.complete_tool(
            "edit1",
            "Edited src/app.py",
            25,
            file_snapshot=("old\n", "new\n"),
            lazy=True,
        )
        await cp.add_agent_message("done")

        await wait_for(lambda: not group._content_mounted, pilot=pilot, description="not group._content_mounted")
        assert group._content_mounted is False

        _show_process(group)
        group.collapsed = False
        await wait_for(
            lambda: group._content_mounted and len(group._tools) == 1,
            pilot=pilot,
            description="group._content_mounted and len(group._tools) == 1",
        )

        assert group._content_mounted is True
        assert len(group._tools) == 1
        assert not list(cp.query(InlineUnifiedDiffLines))

        await asyncio.wait_for(prepare_started.wait(), timeout=5)
        assert group._content_mounted is True
        assert not list(cp.query(InlineUnifiedDiffLines))

        release_prepare.set()
        await wait_for(
            lambda: list(cp.query(InlineUnifiedDiffLines)),
            pilot=pilot,
            description="list(cp.query(InlineUnifiedDiffLines))",
        )

        assert list(cp.query(InlineUnifiedDiffLines))


@pytest.mark.parametrize("reexpand_during_mount", [False, True])
async def test_tool_group_collapse_during_inline_diff_mount_keeps_diff_deferred(
    monkeypatch: pytest.MonkeyPatch,
    reexpand_during_mount: bool,
) -> None:
    """A collapse landing inside the live diff mount must leave the diff rebuildable.

    With ``reexpand_during_mount`` the group is expanded again before the mount
    resumes, so the card has to rebuild on its own: the expand pass saw it
    mid-mount and skipped it.
    """
    from chrys.app.tui.widgets.chat.renderers.file_edit import EditFileToolCall
    from chrys.app.tui.widgets.diff_view import inline as inline_module
    from chrys.app.tui.widgets.diff_view.inline import InlineUnifiedDiffLines

    register_kind_renderer(KIND_SUB_AGENT, SubAgentToolCall)
    collapsed_during_mount = asyncio.Event()
    prune_release = asyncio.Event()
    stalled_children: list[InlineUnifiedDiffLines] = []

    class CollapsingDiffLines(InlineUnifiedDiffLines):
        """Diff widget whose own mount collapses the enclosing group.

        The hooks live on a fresh subclass instead of being patched onto
        ``InlineUnifiedDiffLines``: the dispatch-cache runtime patch pins a
        class's handler plan on its first dispatch, so a handler added to a
        class that already mounted earlier in the worker is never called.
        """

        def on_mount(self) -> None:
            # Runs while the card is still awaiting ``content.mount(...)``: the
            # Mount event is dispatched before the mount awaitable resolves.
            if collapsed_during_mount.is_set():
                return
            group = next(ancestor for ancestor in self.ancestors if isinstance(ancestor, ToolGroup))
            group.collapsed = True
            if reexpand_during_mount:
                _show_process(group)
                group.collapsed = False
            collapsed_during_mount.set()

        async def on_unmount(self) -> None:
            # Park the released child in the node list with ``_pruning`` set, so
            # the rebuild has to look past it instead of mistaking it for a mount.
            if reexpand_during_mount and not stalled_children:
                stalled_children.append(self)
                await prune_release.wait()

    # ``_build_diff`` imports the class at call time, so the card builds the
    # subclass while every ``isinstance`` check against the base still holds.
    monkeypatch.setattr(inline_module, "InlineUnifiedDiffLines", CollapsingDiffLines)

    async with ChatPanelApp().run_test() as pilot:
        cp = pilot.app.query_one(ChatPanel)
        cp.set_tool_kinds({"edit_file": KIND_FILESYSTEM_WRITE, "explore_agent": KIND_SUB_AGENT})
        await cp.add_user_message("edit")
        # A sub-agent sibling keeps the group unprunable, so expanding must
        # rebuild the diff through the card itself rather than via replay.
        await cp.add_tool_start("sa1", "explore_agent", KIND_SUB_AGENT, args={"prompt": "verify"})
        await cp.add_tool_result("sa1", "explore_agent", "ok", 5)
        await cp.add_tool_start("edit1", "edit_file", KIND_FILESYSTEM_WRITE, args={"path": "src/app.py"})
        group = cp.query_one(ToolGroup)
        _show_process(group)
        group.collapsed = False
        await cp.add_tool_result("edit1", "edit_file", "Edited src/app.py", 25, file_snapshot=("old\n", "new\n"))
        card = cp.query_one(EditFileToolCall)

        try:
            await wait_for(
                collapsed_during_mount.is_set,
                description="group collapses inside the live diff mount await",
            )
        except AssertionError as exc:
            raise AssertionError(f"{exc}; collapsed={group.collapsed} {inline_diff_mount_state(card)}") from None

        if reexpand_during_mount:
            try:
                # No pilot here: ``pilot.pause`` waits for every widget to drain
                # its queue, and the stalled child deliberately never does.
                await wait_for(
                    lambda: not card._diff_mounting and not card._diff_pending and card._has_inline_diff_child(),
                    description="diff rebuilt after a collapse and expand inside the mount await",
                )
                assert group.collapsed is False
                content = card.query_one("#ft-content")
                assert stalled_children and stalled_children[0]._pruning
                assert stalled_children[0] in content.children
                rebuilt = [
                    child
                    for child in content.children
                    if isinstance(child, InlineUnifiedDiffLines) and not child._pruning
                ]
                assert len(rebuilt) == 1
                assert rebuilt[0] is not stalled_children[0]
            finally:
                prune_release.set()
            await wait_for(
                lambda: stalled_children[0] not in content.children,
                pilot=pilot,
                description="released child leaves the node list once its prune resumes",
            )
            assert list(cp.query(InlineUnifiedDiffLines)) == rebuilt
            return

        await wait_for(
            lambda: not card._diff_mounting and not list(cp.query(InlineUnifiedDiffLines)),
            pilot=pilot,
            description="released diff settles after the mid-mount collapse",
        )
        assert group.collapsed is True
        assert card._diff_pending is True

        _show_process(group)
        group.collapsed = False
        # The widget appears in the DOM before the mount coroutine finishes,
        # so wait for the card's own state to settle, not just the query.
        await wait_for(
            lambda: not card._diff_mounting and not card._diff_pending and card._has_inline_diff_child(),
            pilot=pilot,
            description="expand rebuilds the released diff",
        )
        assert list(cp.query(InlineUnifiedDiffLines))


async def test_tool_group_expand_retries_inline_diff_after_failed_mount(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A failed inline diff mount must stay deferred so the next expand retries it."""
    from textual.widget import Widget

    from chrys.app.tui.widgets.chat.renderers.file_edit import EditFileToolCall
    from chrys.app.tui.widgets.diff_view.inline import InlineUnifiedDiffLines

    register_kind_renderer(KIND_SUB_AGENT, SubAgentToolCall)
    original_mount = Widget.mount
    failures: list[str] = []

    def failing_mount(self: Widget, *widgets: Widget, **kwargs: object):
        if not failures and self.id == "ft-content" and any(isinstance(w, InlineUnifiedDiffLines) for w in widgets):
            failures.append("simulated")
            raise RuntimeError("simulated inline diff mount failure")
        return original_mount(self, *widgets, **kwargs)

    monkeypatch.setattr(Widget, "mount", failing_mount)

    async with ChatPanelApp().run_test() as pilot:
        cp = pilot.app.query_one(ChatPanel)
        cp.set_tool_kinds({"edit_file": KIND_FILESYSTEM_WRITE, "explore_agent": KIND_SUB_AGENT})
        await cp.add_user_message("edit")
        await cp.add_tool_start("sa1", "explore_agent", KIND_SUB_AGENT, args={"prompt": "verify"})
        await cp.add_tool_result("sa1", "explore_agent", "ok", 5)
        await cp.add_tool_start("edit1", "edit_file", KIND_FILESYSTEM_WRITE, args={"path": "src/app.py"})
        group = cp.query_one(ToolGroup)
        _show_process(group)
        group.collapsed = False
        with caplog.at_level(logging.WARNING, logger="chrys.app.tui.widgets.chat.renderers.file_edit"):
            await cp.add_tool_result("edit1", "edit_file", "Edited src/app.py", 25, file_snapshot=("old\n", "new\n"))
            card = cp.query_one(EditFileToolCall)
            await wait_for(
                lambda: failures and not card._diff_mounting,
                pilot=pilot,
                description="first live diff mount fails",
            )
        assert not list(cp.query(InlineUnifiedDiffLines))
        assert card._diff_pending is True
        assert "Inline diff mount failed for edit1" in caplog.text

        group.collapsed = True
        await pilot.pause()
        _show_process(group)
        group.collapsed = False
        await wait_for(
            lambda: not card._diff_mounting and not card._diff_pending and card._has_inline_diff_child(),
            pilot=pilot,
            description="expand retries the failed diff mount",
        )
        assert list(cp.query(InlineUnifiedDiffLines))
        assert failures == ["simulated"]


async def test_rejected_file_tool_stays_text_after_collapse_expand_with_running_sibling() -> None:
    """Rejected file tools are text terminals, not lazy diff candidates."""
    from chrys.app.tui.widgets.diff_view import DiffView

    async with ChatPanelApp().run_test() as pilot:
        cp = pilot.app.query_one(ChatPanel)
        await cp.add_user_message("tools")
        await cp.add_tool_start("edit1", "edit_file", "filesystem.write", args={"path": "src/app.py"})
        await cp.add_tool_result(
            "edit1",
            "edit_file",
            "Error: Tool execution was rejected by user.",
            25,
            approval="user_rejected",
            file_snapshot=("old\n", "new\n"),
        )
        await cp.add_tool_start("tool2", "plain_tool", "", args={"value": 2})
        await pilot.pause()

        group = cp.query_one(ToolGroup)
        _show_process(group)
        group.collapsed = False
        await wait_for(lambda: group._content_mounted, pilot=pilot, description="the rejected card is shown")
        rejected_tool = group._tools["edit1"]
        assert rejected_tool.has_class("-rejected")

        group.collapsed = True
        await pilot.pause()
        assert rejected_tool._diff_pending is False

        _show_process(group)
        group.collapsed = False
        worker = group._pending_content_worker
        assert worker is not None
        await wait_for(lambda: worker.is_finished, pilot=pilot, description="expanded tool content worker completes")
        await worker.wait()

        assert rejected_tool.has_class("-rejected")
        assert rejected_tool._diff_pending is False
        assert not list(rejected_tool.query(DiffView))
        content = rejected_tool.query_one("#ft-content")
        assert any(isinstance(child, Static) and "rejected" in str(child.content).lower() for child in content.children)


async def test_shell_tool_nonzero_exit_rebuild_uses_complete_path_metadata() -> None:
    """Non-zero shell results should replay through set_complete, preserving parsed display state."""
    from chrys.app.tui.widgets.chat.renderers.execute import ExecuteToolCall

    result = "boom\n[exit_code: 42]"

    async with ChatPanelApp().run_test() as pilot:
        cp = pilot.app.query_one(ChatPanel)
        await cp.add_user_message("run")
        await cp.add_tool_start("sh1", "bash", KIND_SHELL, args={"command": "false"})
        group = cp.query_one(ToolGroup)
        _show_process(group)
        group.collapsed = False
        await cp.add_tool_result("sh1", "bash", result, 1234)
        await pilot.pause()

        shell = cp.query_one(ExecuteToolCall)
        panel = shell.query_one("#exec-panel")
        assert shell.status == "error"
        assert shell.duration_ms == 1234
        assert str(panel.border_subtitle) == "Errored [42]"
        assert exec_panel_text(shell) == "boom"

        await cp.add_agent_message("done")
        group = cp.query_one(ToolGroup)
        await wait_for(lambda: not group._content_mounted, pilot=pilot, description="not group._content_mounted")

        _show_process(group)
        group.collapsed = False
        await wait_for(lambda: group._content_mounted, pilot=pilot, description="group._content_mounted")

        rebuilt = group._tools["sh1"]
        assert isinstance(rebuilt, ExecuteToolCall)
        rebuilt_panel = rebuilt.query_one("#exec-panel")
        assert rebuilt.status == "error"
        assert rebuilt.duration_ms == 1234
        assert str(rebuilt_panel.border_subtitle) == "Errored [42]"
        assert exec_panel_text(rebuilt) == "boom"


async def test_tool_group_restore_reprunes_when_collapsed_mid_rebuild(monkeypatch: pytest.MonkeyPatch) -> None:
    """Rapid expand/collapse during async restore must not leave children mounted."""
    async with ChatPanelApp().run_test() as pilot:
        cp = pilot.app.query_one(ChatPanel)
        await cp.add_user_message("tools")
        await cp.add_tool_start("tool1", "plain_tool", "", args={"value": 1})
        await cp.add_tool_result("tool1", "plain_tool", "done", 10)
        await cp.add_agent_message("done")

        group = cp.query_one(ToolGroup)
        await wait_for(lambda: not group._content_mounted, pilot=pilot, description="not group._content_mounted")
        assert group.collapsed is True
        assert group._content_mounted is False

        content = group.query_one("#tg-content")
        original_mount = content.mount

        def _drop_call_later(*args, **kwargs):
            return None

        async def _mount_then_collapse(*args, **kwargs):
            result = await original_mount(*args, **kwargs)
            group.collapsed = True
            return result

        monkeypatch.setattr(group, "call_later", _drop_call_later)
        monkeypatch.setattr(content, "mount", _mount_then_collapse)

        _show_process(group)
        group.collapsed = False
        await group._restore_completed_tool_widgets()
        await pilot.pause()

        assert group.collapsed is True
        assert group._content_mounted is False
        assert len(group._tools) == 0
        assert not list(content.children)


async def test_tool_group_release_restores_when_expand_races_awaited_removal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Re-expanding while subtree removal is suspended must not leave an empty group."""
    app = GcMessageChatPanelApp()
    async with app.run_test() as pilot:
        group, content, release_removal, release_task = await _start_release_with_held_removal(
            app, monkeypatch, "tool1"
        )

        _show_process(group)
        group.collapsed = False
        release_removal.set()
        await release_task
        await wait_for(lambda: len(app.gc_messages) == 2, pilot=pilot, description="len(app.gc_messages) == 2")

        assert group.collapsed is False
        assert group._content_mounted is True
        assert list(content.children)
        assert group.get_tool("tool1") is not None
        assert len(app.gc_messages) == 2
        assert isinstance(app.gc_messages[0], GcReclaimRequested)
        assert app.gc_messages[0].reason is GcReclaimReason.STABLE_CONTENT_REMOVED
        assert isinstance(app.gc_messages[1], GcAbsorbRequested)
        assert app.gc_messages[1].reason is GcAbsorbReason.STABLE_CONTENT_MOUNTED


async def test_tool_group_release_mounts_only_live_tool_added_during_awaited_removal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tool added after release starts must remain mounted and running."""
    app = GcMessageChatPanelApp()
    async with app.run_test() as pilot:
        group, content, release_removal, release_task = await _start_release_with_held_removal(app, monkeypatch, "old")

        add_task = asyncio.create_task(group.add_tool("new", "plain_tool", "", args={"value": 2}))
        await pilot.pause()
        assert add_task.done() is False
        assert group.is_tool_running("new") is True
        release_removal.set()
        await release_task
        await add_task
        await pilot.pause()

        assert group.collapsed is True
        assert group._content_mounted is False
        assert group.is_tool_running("new") is True
        live = group.get_tool("new")
        assert live is not None
        assert set(group._tools) == {"new"}
        assert list(content.children) == [live]
        assert len(app.gc_messages) == 1
        assert isinstance(app.gc_messages[0], GcReclaimRequested)

        _show_process(group)
        group.collapsed = False
        await group._restore_completed_tool_widgets()
        assert group.get_tool("new") is live
        assert [card.call_id for card in group.query(ToolCall)] == ["old", "new"]


async def test_tool_group_release_normalizes_tool_completed_during_awaited_removal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A racing tool that finishes during prune must leave no orphaned child."""
    app = GcMessageChatPanelApp()
    async with app.run_test() as pilot:
        group, content, release_removal, release_task = await _start_release_with_held_removal(app, monkeypatch, "old")

        add_task = asyncio.create_task(group.add_tool("new", "plain_tool", "", args={"value": 2}))
        await pilot.pause()
        group.complete_tool("new", "done", 20)
        assert group.all_complete is True
        release_removal.set()
        await release_task
        await add_task
        await pilot.pause()

        assert group._content_mounted is False
        assert group._tools == {}
        assert not content.children

        _show_process(group)
        group.collapsed = False
        await group._restore_completed_tool_widgets()
        await pilot.pause()

        assert set(group._tools) == {"old", "new"}
        assert len(content.children) == 2
        assert all(tool.status != "running" for tool in group._tools.values())


async def test_tool_group_serializes_add_with_in_progress_full_restore(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A concurrent add must not re-enter a full restore or duplicate records."""
    async with ChatPanelApp().run_test() as pilot:
        panel = pilot.app.query_one(ChatPanel)
        group = ToolGroup()
        await panel.mount(group)
        monkeypatch.setattr(group, "call_later", lambda *_args, **_kwargs: None)
        await group.add_collapsed_replay_tool(
            "old",
            "plain_tool",
            "",
            result="done",
        )
        content = group.query_one("#tg-content")
        original_mount = content.mount
        mount_started = asyncio.Event()
        release_mount = asyncio.Event()
        delayed_once = False

        def delayed_mount(*widgets, **kwargs):
            nonlocal delayed_once
            pending_mount = original_mount(*widgets, **kwargs)
            if delayed_once:
                return pending_mount
            delayed_once = True
            mount_started.set()

            async def wait_for_release():
                await release_mount.wait()
                return await pending_mount

            return wait_for_release()

        monkeypatch.setattr(content, "mount", delayed_mount)
        _show_process(group)
        group.collapsed = False
        restore_task = asyncio.create_task(group._restore_completed_tool_widgets())
        await mount_started.wait()

        add_task = asyncio.create_task(group.add_tool("new", "plain_tool", "", args={"value": 2}))
        await pilot.pause()
        assert add_task.done() is False
        release_mount.set()
        await restore_task
        await add_task
        await pilot.pause()

        assert set(group._tools) == {"old", "new"}
        assert len(content.children) == 2
        assert len({id(child) for child in content.children}) == 2
        assert group.get_tool("new") is not None
        assert group.is_tool_running("new") is True


async def test_collapsed_replay_writer_waits_for_content_structure_lock() -> None:
    async with ChatPanelApp().run_test() as pilot:
        panel = pilot.app.query_one(ChatPanel)
        group = ToolGroup()
        await panel.mount(group)
        await group._content_structure_lock.acquire()
        add_task = asyncio.create_task(
            group.add_collapsed_replay_tool(
                "replay",
                "plain_tool",
                "",
                result="done",
            )
        )
        try:
            await pilot.pause()
            assert add_task.done() is False
            assert "replay" not in group._tool_records
        finally:
            group._content_structure_lock.release()
        await add_task

        assert group._tool_records["replay"].result == "done"
        assert group._content_mounted is False


def test_tool_call_completion_before_compose_is_fail_soft() -> None:
    tool = ToolCall("late", "plain_tool")

    tool.set_complete("done", 10)

    assert tool.status == "complete"
    assert tool.result_text == "done"
    assert tool.has_class("-done")
    assert tool.has_class("-success")


async def test_tool_group_replay_metadata_approval_renders_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    async with ChatPanelApp().run_test() as pilot:
        cp = pilot.app.query_one(ChatPanel)
        await cp.add_user_message("tools")
        group = ToolGroup()
        await cp.mount(group)
        monkeypatch.setattr(group, "_schedule_pending_content_mounts", lambda **_kwargs: None)
        await group.add_collapsed_replay_tool(
            "reject1",
            "plain_tool",
            "",
            result="Error: rejected",
            duration_ms=10,
            metadata={TOOL_FAILED_METADATA_KEY: True, "approval": "user_rejected"},
        )
        await pilot.pause()

        assert group._tool_records["reject1"].status == "rejected"

        _show_process(group)
        group.collapsed = False
        await group._restore_completed_tool_widgets()
        await pilot.pause()

        tool = group._tools["reject1"]
        assert isinstance(tool, ToolCall)
        assert tool.status == "rejected"
        assert tool.has_class("-rejected")
        assert not tool.has_class("-error")


async def test_mount_pending_diffs_survives_collapse_during_await() -> None:
    """Pending diff mounting must tolerate collapse pruning the tool map mid-loop."""

    class _PendingTool(Static):
        status = "complete"

        def __init__(self, group: ToolGroup, events: list[str], name: str) -> None:
            super().__init__(name)
            self.group = group
            self.events = events
            self._label = name

        async def mount_diff_if_pending(self) -> bool:
            self.events.append(self._label)
            if self._label == "first":
                self.group.collapsed = True
                await self.group._release_completed_tool_widgets()
            await asyncio.sleep(0)
            return True

    async with ChatPanelApp().run_test() as pilot:
        cp = pilot.app.query_one(ChatPanel)
        await cp.add_user_message("tools")
        group = ToolGroup()
        await cp.mount(group)
        events: list[str] = []
        first = _PendingTool(group, events, "first")
        second = _PendingTool(group, events, "second")
        group._tool_records["first"] = group._tool_records["second"] = None  # type: ignore[assignment]
        group._tools = {"first": first, "second": second}  # type: ignore[assignment]
        group._done = 2
        await group.query_one("#tg-content").mount(first)
        await group.query_one("#tg-content").mount(second)

        await group._mount_pending_diffs()
        await pilot.pause()

        assert events == ["first"]
        assert group.collapsed is True


async def test_collapsed_running_tool_group_prunes_when_last_tool_completes() -> None:
    """Manual collapse before completion should prune once the final result arrives."""
    async with ChatPanelApp().run_test() as pilot:
        cp = pilot.app.query_one(ChatPanel)
        await cp.add_user_message("tools")
        await cp.add_tool_start("tool1", "plain_tool", "", args={"value": 1})
        await cp.add_tool_start("tool2", "plain_tool", "", args={"value": 2})

        group = cp.query_one(ToolGroup)
        group.collapsed = True
        await pilot.pause()
        assert group._content_mounted is True

        await cp.add_tool_result("tool1", "plain_tool", "one", 10)
        await pilot.pause()
        assert group._content_mounted is True

        await cp.add_tool_result("tool2", "plain_tool", "two", 10)
        await wait_for(lambda: not group._content_mounted, pilot=pilot, description="not group._content_mounted")

        assert group.collapsed is True
        assert group.all_complete is True
        assert group._content_mounted is False
        assert len(group._tools) == 0


async def test_pruned_open_tool_group_restores_completed_tools_when_expanded() -> None:
    """A later start mounts only its own card until the group opens."""
    async with ChatPanelApp().run_test() as pilot:
        cp = pilot.app.query_one(ChatPanel)
        await cp.add_user_message("tools")
        await cp.add_tool_start("a", "plain_tool", "", args={"value": "a"})
        await cp.add_tool_result("a", "plain_tool", "a done", 10)

        group = cp.query_one(ToolGroup)
        group.collapsed = True
        await wait_for(lambda: not group._content_mounted, pilot=pilot, description="not group._content_mounted")
        assert group.collapsed is True
        assert group._content_mounted is False
        assert len(group._tools) == 0
        assert set(group._tool_records) == {"a"}

        await cp.add_tool_start("b", "plain_tool", "", args={"value": "b"})
        await pilot.pause()

        assert group.collapsed is True
        assert group._content_mounted is False
        assert set(group._tools) == {"b"}
        live = group._tools["b"]
        assert live.status == "running"

        _show_process(group)
        group.collapsed = False
        await wait_for(lambda: group._content_mounted, pilot=pilot, description="missing cards restored")

        assert set(group._tools) == {"a", "b"}
        assert group._tools["a"].status == "complete"
        assert group._tools["b"] is live
        assert [child.call_id for child in group.query(ToolCall)] == ["a", "b"]


async def test_tool_group_abandons_locked_mount_and_restore_after_detach() -> None:
    """Lock waiters and deferred restores must recheck the group lifecycle."""
    group = ToolGroup()

    async with LocalizedWidgetApp(lambda: group).run_test() as pilot:
        await group._content_structure_lock.acquire()
        add_task = asyncio.create_task(
            group.add_tool(
                "late-write",
                "write_file",
                KIND_FILESYSTEM_WRITE,
                args={"path": "generated.py", "content": "pass"},
            )
        )
        await wait_for(
            lambda: "late-write" in group._tool_records,
            pilot=pilot,
            description="tool start waiting on the structure lock",
        )

        await group.remove()
        group._content_structure_lock.release()
        await add_task

        assert "late-write" not in group._tool_records
        assert group.get_tool("late-write") is None

        group._content_mounted = False
        await group._restore_completed_tool_widgets()
