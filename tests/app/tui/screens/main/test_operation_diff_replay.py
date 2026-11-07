# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""File diff projection retains occurrence identity across archives and child tools."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from chrys.app.tui.screens.main.state import MainScreenServices
from chrys.foundation.events.bus import EventBus
from chrys.service.mutations.store import SnapshotStore
from chrys.service.mutations.tracker import MutationTracker
from chrys.service.mutations.types import MutationOp, MutationSource
from chrys.service.state.store import JsonFileStateStore
from tests.support.tui_helpers import make_session_handler


async def test_diff_loader_uses_operations_even_when_only_live_call_is_in_messages(tmp_path: Path) -> None:
    store = JsonFileStateStore(tmp_path / "sessions")
    await store.save_session("sid", {"messages": []})
    tracker = MutationTracker(SnapshotStore(store.session_dir("sid")))
    path = tmp_path / "a.txt"

    def record() -> None:
        path.write_text("before", encoding="utf-8")
        for index, operation in enumerate(("child-op", "archive-op", "live-op"), start=1):
            tracker.start_turn(index)
            mutation = tracker.record(
                str(path),
                MutationOp.MODIFY,
                MutationSource.EDIT_FILE,
                f"framework-{index}",
                tool_operation_id=operation,
            )
            path.write_text(operation, encoding="utf-8")
            tracker.record_after(mutation)

    await asyncio.to_thread(record)
    # The archived messages and child's transcript are absent from the live array.
    messages = [
        {
            "role": "assistant",
            "contents": [
                {
                    "type": "function_call",
                    "name": "edit_file",
                    "call_id": "provider-id",
                    "additional_properties": {"_chrys_operation_id": "live-op"},
                }
            ],
        }
    ]
    screen = SimpleNamespace(_services=MainScreenServices(bus=EventBus(), state_store=store))
    snapshots = await asyncio.to_thread(
        make_session_handler(screen).load_file_edit_snapshots, "sid", messages, {"chrys_mutations": tracker.serialize()}
    )
    assert snapshots == {
        ("operation", "child-op"): [("before", "child-op")],
        ("operation", "archive-op"): [("child-op", "archive-op")],
        ("operation", "live-op"): [("archive-op", "live-op")],
    }


async def test_unstamped_mutation_follows_its_stamped_call(tmp_path: Path) -> None:
    """Tool calls carried operation ids before mutations did; replay looks such a call up by operation."""
    store = JsonFileStateStore(tmp_path / "sessions")
    await store.save_session("sid", {"messages": []})
    tracker = MutationTracker(SnapshotStore(store.session_dir("sid")))
    path = tmp_path / "a.txt"

    def record() -> None:
        path.write_text("before", encoding="utf-8")
        tracker.start_turn(1)
        for index, content in enumerate(("legacy", "unstamped call")):
            mutation = tracker.record(str(path), MutationOp.MODIFY, MutationSource.EDIT_FILE, f"call-{index}")
            path.write_text(content, encoding="utf-8")
            tracker.record_after(mutation)

    await asyncio.to_thread(record)
    calls = [
        {
            "type": "function_call",
            "name": "edit_file",
            "call_id": "call-0",
            "additional_properties": {"_chrys_operation_id": "legacy-op"},
        },
        {"type": "function_call", "name": "edit_file", "call_id": "call-1"},
    ]
    screen = SimpleNamespace(_services=MainScreenServices(bus=EventBus(), state_store=store))
    snapshots = await asyncio.to_thread(
        make_session_handler(screen).load_file_edit_snapshots,
        "sid",
        [{"role": "assistant", "contents": calls}],
        {"chrys_mutations": tracker.serialize()},
    )
    assert snapshots == {
        ("operation", "legacy-op"): [("before", "legacy")],
        "call-1": [("legacy", "unstamped call")],
    }
