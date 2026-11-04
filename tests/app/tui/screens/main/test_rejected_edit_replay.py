# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Rejected edit replay slots stay aligned and render like the live card."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from chrys.app.tui.screens.main.state import MainScreenServices
from chrys.app.tui.widgets.chat.panel import ChatPanel
from chrys.app.tui.widgets.chat.renderers.file_edit import EditFileToolCall
from chrys.app.tui.widgets.chat.tool_call import ToolGroup
from chrys.app.tui.widgets.diff_view.inline import InlineUnifiedDiffLines
from chrys.foundation.events.bus import EventBus
from chrys.foundation.tool_result_metadata import TOOL_FAILED_METADATA_KEY, TOOL_RESULT_METADATA_KEY
from chrys.kernel import Message
from chrys.service.mutations.store import SnapshotStore
from chrys.service.mutations.tracker import MutationTracker
from chrys.service.mutations.types import MutationOp, MutationSource
from chrys.service.state.store import JsonFileStateStore
from tests.support.tui_helpers import ChatPanelApp, make_session_handler
from tests.support.waiting import wait_for


@pytest.mark.parametrize("reuse_call_id", [False, True])
async def test_rejected_then_approved_snapshots_survive_restore_without_a_rejected_diff(
    tmp_path: Path, reuse_call_id: bool
) -> None:
    store = JsonFileStateStore(tmp_path / "sessions")
    await store.save_session("sid", {"messages": [], "compressed_msgs": []})
    tracker = MutationTracker(SnapshotStore(store.session_dir("sid")))
    tracker.start_turn(1)
    target = tmp_path / "work.txt"

    def record_calls() -> None:
        target.write_bytes(b"before\n")
        denied = tracker.record(str(target), MutationOp.MODIFY, MutationSource.EDIT_FILE, "runtime-denied")
        tracker.settle_unexecuted(denied)
        approved = tracker.record(str(target), MutationOp.MODIFY, MutationSource.EDIT_FILE, "runtime-approved")
        target.write_bytes(b"after\n")
        tracker.record_after(approved)

    await asyncio.to_thread(record_calls)
    messages = [{"role": "user", "contents": [{"type": "text", "text": "Edit"}]}]
    for index, rejected in enumerate((True, False)):
        call_id = "reused" if reuse_call_id else f"call-{index}"
        metadata = {TOOL_FAILED_METADATA_KEY: rejected}
        if rejected:
            metadata["approval"] = "user_rejected"
        messages.extend(
            [
                {
                    "role": "assistant",
                    "contents": [
                        {
                            "type": "function_call",
                            "name": "edit_file",
                            "call_id": call_id,
                            "arguments": {"path": str(target), "old_string": "before", "new_string": "after"},
                        }
                    ],
                },
                {
                    "role": "tool",
                    "contents": [
                        {
                            "type": "function_result",
                            "call_id": call_id,
                            "result": "Error: rejected" if rejected else "Edited",
                            "additional_properties": {TOOL_RESULT_METADATA_KEY: metadata},
                        }
                    ],
                },
            ]
        )
    state = {"messages": [Message.from_dict(message) for message in messages], "chrys_mutations": tracker.serialize()}
    await store.save_session("sid", state)
    loaded = await store.load_session("sid")
    screen = SimpleNamespace(_services=MainScreenServices(bus=EventBus(), state_store=store))
    snapshots = await asyncio.to_thread(make_session_handler(screen).load_file_edit_snapshots, "sid", messages, loaded)
    if reuse_call_id:
        assert snapshots == {"reused": [("before\n", "before\n"), ("before\n", "after\n")]}
    else:
        assert snapshots == {"call-0": [("before\n", "before\n")], "call-1": [("before\n", "after\n")]}

    async with ChatPanelApp().run_test() as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.replay_history(messages, file_snapshots=snapshots)
        await panel.wait_replay_complete()
        group = panel.query_one(ToolGroup)
        group.collapsed = False
        await wait_for(lambda: len(group._tools) == 2, pilot=pilot, description="both restored edit cards mounted")
        denied, approved = list(group._tools.values())
        assert isinstance(denied, EditFileToolCall)
        assert isinstance(approved, EditFileToolCall)
        await denied.mount_diff_if_pending()
        await approved.mount_diff_if_pending()

        assert denied._before_content == denied._after_content == "before\n"
        assert denied.status == "rejected"
        assert not denied.query(InlineUnifiedDiffLines)
        assert (approved._before_content, approved._after_content) == ("before\n", "after\n")
        assert len(approved.query(InlineUnifiedDiffLines)) == 1

        live = EditFileToolCall("live", "edit_file", args={"path": str(target)})
        await panel.mount(live)
        live.set_complete("Error: rejected", metadata={TOOL_FAILED_METADATA_KEY: True, "approval": "user_rejected"})
        await pilot.pause()
        assert live.status == denied.status
        assert not live.query(InlineUnifiedDiffLines)
