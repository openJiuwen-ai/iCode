# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for replayed tool rendering: deferred renderer creation, replay timing suffixes, hatching and batching, and file-snapshot diff rebuilds."""

from __future__ import annotations

import asyncio

import pytest
from textual.await_complete import AwaitComplete
from textual.geometry import Region
from textual.widget import Widget

from chrys.app.tui.widgets.chat import panel as chat_panel_module
from chrys.app.tui.widgets.chat import replay_mount as replay_mount_module
from chrys.app.tui.widgets.chat.file_snapshot import FileSnapshotRef
from chrys.app.tui.widgets.chat.messages import (
    AgentMessage,
    UserMessage,
    _UserHeader,
    format_message_created_at,
)
from chrys.app.tui.widgets.chat.panel import ChatPanel
from chrys.app.tui.widgets.chat.renderers.sub_agent import SubAgentToolCall
from chrys.app.tui.widgets.chat.tool_call import (
    ToolCall,
    ToolCardHeader,
    ToolGroup,
    ToolGroupTitle,
)
from chrys.app.tui.widgets.chat.tool_renderers import register_kind_renderer
from chrys.app.tui.widgets.hatch import HATCH_GLYPH
from chrys.app.tui.widgets.markdown import VirtualizedMarkdown
from chrys.foundation.tool_kinds import (
    KIND_SUB_AGENT,
)
from chrys.foundation.tool_result_metadata import (
    TOOL_FAILED_METADATA_KEY,
    TOOL_INTERRUPTED_METADATA_KEY,
)
from chrys.foundation.trajectory_timing import TRAJECTORY_TIMING_KEY
from chrys.service.mutations.store import SnapshotStore
from tests.support.tui_helpers import (
    ChatPanelApp,
)
from tests.support.waiting import wait_for

# Tests here call register_kind_renderer(KIND_SUB_AGENT, ...) directly; undo it per test.
pytestmark = pytest.mark.usefixtures("frozen_earlier_heap", "restore_kind_renderer_registry")


async def test_replay_history_defers_tool_renderer_creation_until_expand(monkeypatch: pytest.MonkeyPatch) -> None:
    from chrys.app.tui.widgets.chat import tool_renderers as tool_renderers_module

    created: list[tuple[str, str, str, dict[str, object] | None]] = []

    def create_tool_widget(
        call_id: str,
        tool_name: str,
        tool_kind: str,
        args_summary: str = "",
        args: dict[str, object] | None = None,
    ) -> ToolCall:
        created.append((call_id, tool_name, tool_kind, args))
        return ToolCall(call_id, tool_name, args_summary, args=args)

    monkeypatch.setattr(tool_renderers_module, "create_tool_widget", create_tool_widget)
    messages = [
        {"role": "user", "contents": [{"type": "text", "text": "run tool"}]},
        {
            "role": "assistant",
            "contents": [
                {
                    "type": "function_call",
                    "name": "plain_tool",
                    "call_id": "tool1",
                    "arguments": {"value": 1},
                    "additional_properties": {
                        TRAJECTORY_TIMING_KEY: {
                            "started_at": "2026-08-19T01:02:03+00:00",
                            "finished_at": "2026-08-19T01:02:04+00:00",
                            "duration_ms": 987,
                        }
                    },
                }
            ],
        },
        {"role": "tool", "contents": [{"type": "function_result", "call_id": "tool1", "result": "done"}]},
    ]

    async with ChatPanelApp().run_test() as pilot:
        cp = pilot.app.query_one(ChatPanel)
        await cp.replay_history(messages)
        await pilot.pause()

        group = cp.query_one(ToolGroup)
        assert created == []
        assert group.collapsed is True
        assert group._content_mounted is False
        assert len(group._tools) == 0
        assert len(group._tool_records) == 1
        assert group._tool_records["tool1#0"].duration_ms == 987
        assert group._tool_records["tool1#0"].duration_known is True
        expected_tool_timestamp = format_message_created_at("2026-08-19T01:02:03+00:00")
        assert group._tool_records["tool1#0"].timestamp == expected_tool_timestamp
        assert group.query_one(ToolGroupTitle).render().plain == "▸ Called plain_tool"

        group.collapsed = False
        await wait_for(lambda: created, pilot=pilot, description="created")

        assert created == [("tool1#0", "plain_tool", "", {"value": 1})]
        assert group._content_mounted is True
        tool = group.get_tool("tool1#0")
        assert isinstance(tool, ToolCall)
        assert tool.query_one(ToolCardHeader)._label_renderable().plain.endswith(f"(987ms) {expected_tool_timestamp}")


@pytest.mark.parametrize(("provider_hosted", "duration_ms"), [(False, 0), (True, 987)])
async def test_replay_tool_timing_is_visible_for_zero_and_hosted_durations(
    provider_hosted: bool,
    duration_ms: int,
) -> None:
    async with ChatPanelApp().run_test() as pilot:
        panel = pilot.app.query_one(ChatPanel)
        group = ToolGroup()
        await panel.mount(group)
        await group.add_collapsed_replay_tool(
            "timed",
            "server_task" if provider_hosted else "plain_tool",
            "",
            result="done",
            duration_ms=duration_ms,
            duration_known=True,
            timestamp="- 9:02 AM",
            provider_hosted=provider_hosted,
            hosted_family="generic" if provider_hosted else "",
            provider="openai" if provider_hosted else "",
            canonical_status="completed",
            lazy=True,
        )

        group.collapsed = False
        await wait_for(
            lambda: group._content_mounted and group._tools,
            pilot=pilot,
            description="group._content_mounted and group._tools",
        )

        header = next(iter(group._tools.values())).query_one(ToolCardHeader)
        expected_duration = f"({duration_ms}ms)"
        assert header._label_renderable().plain.count(expected_duration) == 1
        assert header._label_renderable().plain.endswith(f"{expected_duration} - 9:02 AM")


async def test_replay_user_hides_zero_duration_event_suffix() -> None:
    """User timing stays persisted but its definitionally-zero duration is visual noise."""
    messages = [
        {
            "role": "user",
            "contents": [{"type": "text", "text": "hello"}],
            "additional_properties": {
                TRAJECTORY_TIMING_KEY: {
                    "started_at": "2026-08-19T01:02:03+00:00",
                    "finished_at": "2026-08-19T01:02:03+00:00",
                    "duration_ms": 0,
                }
            },
        }
    ]

    async with ChatPanelApp().run_test() as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.replay_history(messages)
        await pilot.pause()

        header = str(panel.query_one(_UserHeader).content)
        assert "(0ms)" not in header
        assert " - " in header


@pytest.mark.parametrize("provider_hosted", [False, True])
async def test_replay_failed_tool_timing_is_visible(provider_hosted: bool) -> None:
    """Error renderers do not receive set_complete, so replay adds their duration."""
    async with ChatPanelApp().run_test() as pilot:
        panel = pilot.app.query_one(ChatPanel)
        group = ToolGroup()
        await panel.mount(group)
        await group.add_collapsed_replay_tool(
            "failed",
            "server_task" if provider_hosted else "plain_tool",
            "",
            result="Error: failed",
            duration_ms=987,
            duration_known=True,
            timestamp="- 9:02 AM",
            provider_hosted=provider_hosted,
            hosted_family="generic" if provider_hosted else "",
            provider="openai" if provider_hosted else "",
            canonical_status="failed" if provider_hosted else "completed",
            lazy=True,
        )

        group.collapsed = False
        await wait_for(
            lambda: group._content_mounted and group._tools,
            pilot=pilot,
            description="group._content_mounted and group._tools",
        )

        header = next(iter(group._tools.values())).query_one(ToolCardHeader)
        assert header._label_renderable().plain.count("(987ms)") == 1
        assert header._label_renderable().plain.endswith("(987ms) - 9:02 AM")


async def test_replay_interrupted_non_hosted_tool_is_not_recorded_as_successful() -> None:
    async with ChatPanelApp().run_test() as pilot:
        panel = pilot.app.query_one(ChatPanel)
        group = ToolGroup()
        await panel.mount(group)
        await group.add_collapsed_replay_tool(
            "interrupted",
            "plain_tool",
            "",
            result="(interrupted)",
            metadata={TOOL_INTERRUPTED_METADATA_KEY: True},
            canonical_status="interrupted",
            lazy=True,
        )

        record = group._tool_records["interrupted"]
        assert record.status == "error"
        assert record.canonical_status == "interrupted"


async def test_replay_failed_subagent_uses_persisted_duration() -> None:
    """A rebuilt sub-agent error must not replace persisted timing with widget elapsed time."""
    register_kind_renderer(KIND_SUB_AGENT, SubAgentToolCall)
    async with ChatPanelApp().run_test() as pilot:
        panel = pilot.app.query_one(ChatPanel)
        group = ToolGroup()
        await panel.mount(group)
        await group.add_collapsed_replay_tool(
            "failed-subagent",
            "explore_agent",
            KIND_SUB_AGENT,
            result="Error: sub-agent failed",
            duration_ms=987,
            duration_known=True,
            timestamp="- 9:02 AM",
            metadata={TOOL_FAILED_METADATA_KEY: True},
            lazy=True,
        )

        group.collapsed = False
        await wait_for(
            lambda: group._content_mounted and group._tools,
            pilot=pilot,
            description="group._content_mounted and group._tools",
        )

        tool = next(iter(group._tools.values()))
        assert isinstance(tool, SubAgentToolCall)
        header = tool.query_one(ToolCardHeader)._label_renderable().plain
        assert header.count("(987ms)") == 1
        assert header.endswith("(987ms) - 9:02 AM")


async def test_live_zero_duration_tool_does_not_gain_replay_suffix_after_rebuild() -> None:
    """Rebuilding a live card must preserve its original zero-duration label."""
    async with ChatPanelApp().run_test() as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.add_user_message("run")
        await panel.add_tool_start("live", "plain_tool", "", args={})
        await panel.add_tool_result("live", "plain_tool", "done", 0)
        await panel.add_agent_message("finished")

        group = panel.query_one(ToolGroup)
        await wait_for(lambda: not group._content_mounted, pilot=pilot, description="not group._content_mounted")

        assert group._content_mounted is False
        group.collapsed = False
        await wait_for(
            lambda: group._content_mounted and group._tools,
            pilot=pilot,
            description="group._content_mounted and group._tools",
        )

        header = group.get_tool("live").query_one(ToolCardHeader)
        assert "(0ms)" not in header._label_renderable().plain


async def test_replay_history_hatches_batch_until_descendants_finish(monkeypatch: pytest.MonkeyPatch) -> None:
    """A mounted replay root stays hatched while async markdown setup is incomplete."""
    update_started = asyncio.Event()
    release_update = asyncio.Event()
    original_update = VirtualizedMarkdown.update

    def delayed_update(self: VirtualizedMarkdown, markdown: str) -> AwaitComplete:
        async def await_update() -> None:
            update_started.set()
            await release_update.wait()
            await original_update(self, markdown)

        return AwaitComplete(await_update())

    monkeypatch.setattr(VirtualizedMarkdown, "update", delayed_update)
    messages = [
        {
            "role": "assistant",
            "contents": [{"type": "text", "text": "# Restored\n\nPending content"}],
        }
    ]

    async with ChatPanelApp().run_test(size=(80, 24)) as pilot:
        cp = pilot.app.query_one(ChatPanel)
        progress: list[tuple[int, int]] = []
        cp.set_replay_progress_callback(lambda current, total: progress.append((current, total)))
        replay = asyncio.create_task(cp.replay_history(messages))

        await asyncio.wait_for(update_started.wait(), timeout=1)
        message = cp.query_one(AgentMessage)
        hatch = message.styles.hatch

        try:
            await wait_for(
                lambda: message.size.width > 0 and message.size.height > 0,
                description="replay placeholder message laid out",
            )
            assert message.has_class(chat_panel_module._REPLAY_PLACEHOLDER_CLASS)
            assert hatch != "none"
            assert hatch[0] == HATCH_GLYPH
            assert progress == [(0, 1)]
            rendered = message.render_lines(Region(0, 0, message.size.width, min(3, message.size.height)))
            assert any(HATCH_GLYPH in strip.text for strip in rendered)
        finally:
            release_update.set()
        await asyncio.wait_for(replay, timeout=1)
        await pilot.pause()

        assert not message.has_class(chat_panel_module._REPLAY_PLACEHOLDER_CLASS)
        assert not message.styles.has_rule("hatch")
        assert progress == [(0, 1), (1, 1)]


async def test_replay_history_bounds_each_textual_registration_batch(monkeypatch: pytest.MonkeyPatch) -> None:
    """Large restores register bounded bursts: the newest batch first, older ones prepended."""
    batch_size = replay_mount_module.REPLAY_MOUNT_BATCH_SIZE
    messages = [
        {"role": "user", "contents": [{"type": "text", "text": f"message {index}"}]} for index in range(batch_size + 8)
    ]

    async with ChatPanelApp().run_test() as pilot:
        cp = pilot.app.query_one(ChatPanel)
        real_mount = cp.mount
        batch_sizes: list[int] = []
        progress: list[tuple[int, int]] = []

        def mount_spy(*widgets: Widget, **kwargs: object):
            batch_sizes.append(len(widgets))
            return real_mount(*widgets, **kwargs)

        monkeypatch.setattr(cp, "mount", mount_spy)
        cp.set_replay_progress_callback(lambda current, total: progress.append((current, total)))
        await cp.replay_history(messages)

        # Visible entries retain the original foreground cap even when
        # their rows would fit in the three-viewport budget.
        assert batch_sizes == [batch_size]
        assert progress == [(0, batch_size), (batch_size, batch_size)]

        await cp.wait_replay_complete()

        assert batch_sizes == [batch_size, 8]
        assert progress == [(0, batch_size), (batch_size, batch_size)]
        assert [child._text for child in cp.children if isinstance(child, UserMessage)] == [
            f"message {index}" for index in range(len(messages))
        ]


async def test_replay_foreground_has_a_hard_cap_even_for_hidden_entries(monkeypatch: pytest.MonkeyPatch) -> None:
    widgets: list[Widget] = [UserMessage("prompt")]
    for _ in range(300):
        hidden = AgentMessage("intermediate")
        hidden.set_process_hidden(True)
        widgets.append(hidden)
    widgets.append(AgentMessage("answer"))
    async with ChatPanelApp().run_test() as pilot:
        panel = pilot.app.query_one(ChatPanel)
        mount = panel.mount
        batches: list[int] = []

        def record_mount(*entries: Widget, **kwargs: object):
            batches.append(len(entries))
            return mount(*entries, **kwargs)

        monkeypatch.setattr(panel, "mount", record_mount)
        await panel.mount_transcript_widgets(widgets)
        assert batches == [32, 32, 32, 32]
        assert len(panel.pending_transcript_widgets()) == len(widgets) - 128
        await panel.wait_replay_complete()
        assert not panel.pending_transcript_widgets()
        assert max(batches) <= 32


async def test_replay_history_duplicate_call_id_distinct_snapshots() -> None:
    """Duplicate call_ids (LLM reuse after rejection) keep distinct snapshots.

    Regression for a subtle variant of the ``Tools (3/2)`` bug: when an
    LLM reuses a ``call_id`` across a rejected file edit and its retry,
    the old ``dict[call_id, tuple]`` contract collapsed both snapshots
    into one — causing both widgets to render the same diff.  The fix
    buckets snapshots by call_id and consumes that list as a file-tool
    snapshot cursor so each widget gets its own.
    """
    messages = [
        {"role": "user", "contents": [{"type": "text", "text": "edit twice"}]},
        {
            "role": "assistant",
            "contents": [
                {
                    "type": "function_call",
                    "name": "edit_file",
                    "call_id": "shared_id",
                    "arguments": {"path": "/tmp/a.py"},
                }
            ],
        },
        {
            "role": "tool",
            "contents": [{"type": "function_result", "call_id": "shared_id", "result": "rejected"}],
        },
        {
            "role": "assistant",
            "contents": [
                {
                    "type": "function_call",
                    "name": "edit_file",
                    "call_id": "shared_id",
                    "arguments": {"path": "/tmp/a.py"},
                }
            ],
        },
        {
            "role": "tool",
            "contents": [{"type": "function_result", "call_id": "shared_id", "result": "ok"}],
        },
    ]
    file_snapshots = {
        "shared_id": [("rejected_before", ""), ("success_before", "success_after")],
    }

    async with ChatPanelApp().run_test() as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.replay_history(messages, file_snapshots=file_snapshots)
        await pilot.pause()

        groups = list(panel.query(ToolGroup))
        assert len(groups) == 1
        tg = groups[0]
        # Both invocations got their own slot via the ``{call_id}#{idx}``
        # disambiguator, and each got its own positional snapshot.
        assert tg._tool_records["shared_id#0"].file_snapshot == ("rejected_before", "")
        assert tg._tool_records["shared_id#1"].file_snapshot == ("success_before", "success_after")
        tg.collapsed = False
        await pilot.pause()
        first = tg._tools.get("shared_id#0")
        second = tg._tools.get("shared_id#1")
        assert first is not None
        assert second is not None
        assert first._before_content == "rejected_before"
        assert first._after_content == ""
        assert second._before_content == "success_before"
        assert second._after_content == "success_after"


async def test_collapsed_replay_tool_group_rebuilds_file_diff_from_snapshot_ref(tmp_path) -> None:
    """Large replay snapshots stay disk-backed while collapsed and resolve on expansion."""
    from chrys.app.tui.widgets.diff_view import DiffView
    from chrys.app.tui.widgets.diff_view.inline import InlineUnifiedDiffLines

    store = SnapshotStore(tmp_path)
    before_hash = store.save_data_as_blob(b"old\n").content_hash
    after_hash = store.save_data_as_blob(b"new\n").content_hash
    ref = FileSnapshotRef(store.mutations_dir, before_hash, after_hash)
    messages = [
        {"role": "user", "contents": [{"type": "text", "text": "edit"}]},
        {
            "role": "assistant",
            "contents": [
                {
                    "type": "function_call",
                    "name": "edit_file",
                    "call_id": "edit1",
                    "arguments": {"path": "src/app.py"},
                }
            ],
        },
        {"role": "tool", "contents": [{"type": "function_result", "call_id": "edit1", "result": "Edited"}]},
    ]

    async with ChatPanelApp().run_test() as pilot:
        cp = pilot.app.query_one(ChatPanel)
        await cp.replay_history(messages, file_snapshots={"edit1": [ref]})
        await pilot.pause()

        group = cp.query_one(ToolGroup)
        assert group._content_mounted is False
        assert len(group._tools) == 0
        record = next(iter(group._tool_records.values()))
        assert record.file_snapshot is ref
        assert not list(cp.query(DiffView))

        group.collapsed = False
        await wait_for(
            lambda: list(cp.query(InlineUnifiedDiffLines)),
            pilot=pilot,
            description="list(cp.query(InlineUnifiedDiffLines))",
        )

        assert not list(cp.query(DiffView))
        assert list(cp.query(InlineUnifiedDiffLines))
