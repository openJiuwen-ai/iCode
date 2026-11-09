# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Live tool paths use workspace identity before diff accumulation."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import create_autospec

import pytest

from chrys.app.tui.screens.main.diff_controller import DiffController, LiveDiffTracker
from chrys.app.tui.screens.main.live_diff import build_live_diff_entries
from chrys.app.tui.screens.main.main_screen_presenter import MainScreenPresenter
from chrys.app.tui.screens.main.ports import DiffView
from chrys.app.tui.screens.main.state import MainScreenServices, MainScreenState, RunState
from chrys.app.tui.util.diff_entries import DiffFileEntry, DiffLoadResult
from chrys.app.tui.util.diff_labels import is_within_cwd
from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import InvocationToolCallResult, InvocationToolCallStart
from chrys.foundation.models.invocations import InvocationOrigin
from chrys.service.mutations.types import FileMutationTextSnapshot, MutationOp
from tests.support.invocation_events import ORIGIN_IDS, ORIGINS
from tests.support.symlinks import symlink_or_skip
from tests.support.tui_helpers import make_backend_handler


@pytest.mark.parametrize("origin", ORIGINS[:2], ids=ORIGIN_IDS[:2])
@pytest.mark.parametrize("tool_name", ["edit_file", "write_file"])
async def test_live_tool_paths_use_workspace_for_grouping_and_merging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, origin: InvocationOrigin, tool_name: str
) -> None:
    launch = tmp_path / "launch"
    workspace = tmp_path / "workspace"
    launch.mkdir()
    workspace.mkdir()
    monkeypatch.chdir(launch)
    workspace = workspace.resolve()
    target = str(workspace / "file.py")
    external = str(workspace.parent / "outside.py")
    live = LiveDiffTracker()
    state = MainScreenState(run=RunState(agent_running=True))
    state.workspace.current_cwd = str(workspace)
    handler = make_backend_handler(SimpleNamespace(_state=state, _live_diff=live))
    presenter = create_autospec(MainScreenPresenter, instance=True, spec_set=True)
    presenter.chat_session_id.return_value = "session-1"
    monkeypatch.setattr(handler, "_presenter", presenter)

    refreshes: list[bool] = []
    handler._callbacks = replace(handler._callbacks, refresh_diff_preview=refreshes.append)
    for metadata in ({}, {"shell_file_snapshots": {}}, {"file_snapshot": "invalid"}):
        await handler.on_tool_result(
            InvocationToolCallResult(origin=origin, call_id="read", tool_name="read_file", metadata=metadata)
        )
    assert refreshes == []

    for call_id, path, before, after in [
        ("relative", "file.py", "before", "first"),
        ("absolute", target, "first", "second"),
        ("outside", "../outside.py", "outside-before", "outside-after"),
    ]:
        await handler.on_tool_start(
            InvocationToolCallStart(origin=origin, call_id=call_id, tool_name=tool_name, args={"path": path})
        )
        await handler.on_tool_result(
            InvocationToolCallResult(
                origin=origin,
                call_id=call_id,
                tool_name=tool_name,
                metadata={"file_snapshot": (before, after), "file_mutation_op": "modify"},
            )
        )

    await handler.on_tool_result(
        InvocationToolCallResult(
            origin=origin,
            call_id="shell",
            tool_name="shell",
            metadata={
                "shell_file_snapshots": {
                    target: FileMutationTextSnapshot(
                        before_text="second", after_text="final", operation="modify", bytes_changed=True
                    )
                }
            },
        )
    )

    assert refreshes == [False] * 4
    entries = build_live_diff_entries(live.snapshot_file_mutations(), str(workspace))
    assert {entry.path for entry in entries} == {target, external}
    local_entry = next(entry for entry in entries if entry.path == target)
    assert local_entry.rel_path == "file.py"
    assert is_within_cwd(local_entry.rel_path)
    assert (local_entry.before_text, local_entry.after_text) == ("before", "final")
    outside_entry = next(entry for entry in entries if entry.path == external)
    assert not is_within_cwd(outside_entry.rel_path)
    assert (outside_entry.before_text, outside_entry.after_text) == ("outside-before", "outside-after")

    persisted = DiffFileEntry(
        path=target,
        rel_path="file.py",
        operation=MutationOp.MODIFY,
        old_path=None,
        before_text="session-original",
        after_text="before",
        is_binary=False,
        encoding="utf-8",
        bytes_changed=True,
    )
    controller = DiffController(
        services=MainScreenServices(bus=EventBus()),
        live_diff=live,
        view=create_autospec(DiffView, instance=True, spec_set=True),
        workspace_cwd=lambda: str(workspace),
        is_agent_running=lambda: True,
        run_generation=lambda: 1,
        session_generation=lambda: 1,
    )
    merged = controller._merge_live_diff(
        DiffLoadResult(all_entries=[persisted], per_period_entries={1: [persisted]}, total_periods=1),
        str(workspace),
        live.snapshot_file_mutations(),
        live_tracker_turn_count=2,
        persistence_finalized=False,
    )
    assert merged.per_period_entries[1] == [persisted]
    assert merged.per_period_entries[2] == entries
    assert len(merged.all_entries) == 2
    all_local = next(entry for entry in merged.all_entries if entry.path == target)
    assert (all_local.before_text, all_local.after_text) == ("session-original", "final")


def test_live_tool_path_resolves_workspace_alias_without_following_file_link(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace = workspace.resolve()
    alias = tmp_path / "alias"
    symlink_or_skip(alias, workspace, target_is_directory=True)
    target = tmp_path / "outside.py"
    target.write_text("outside", encoding="utf-8")
    link = workspace / "link.py"
    symlink_or_skip(link, target)

    live = LiveDiffTracker()
    live.record_tool_start("edit", "edit_file", {"path": "link.py"}, workspace_cwd=str(alias))

    assert live.call_paths == {"edit": str(link)}
