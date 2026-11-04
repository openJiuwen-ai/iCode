# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Observable progress, freshness and shutdown while a sidebar count is held."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path
from unittest.mock import create_autospec

import pytest

from chrys.app.tui.screens.main.diff_controller import CurrentDiffLoad, DiffController
from chrys.app.tui.util.diff_entries import DiffLoadResult
from chrys.app.tui.widgets.diff_view import compute
from chrys.app.tui.widgets.sidebar.diff_preview import DiffPreview
from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import SessionReady
from chrys.foundation.platform.process import windows_hidden_subprocess_kwargs
from tests.app.tui.screens.main.test_diff_preview import RefreshHarness, data, entry
from tests.support.pilot_barrier import screen_is_settled
from tests.support.tui_app_harness import make_chrys_app
from tests.support.waiting import ENGINE_TURN_TIMEOUT, wait_for


class HeldDiff:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.thread: threading.Thread | None = None
        self.calls: list[tuple[str, str]] = []
        self.original = compute.compute_hunks

    def compute(self, before: str, after: str):
        self.calls.append((before, after))
        if before == "held":
            self.thread = threading.current_thread()
            self.started.set()
            if not self.release.wait(15):
                raise TimeoutError("test did not release the held diff")
        return self.original(before, after)


@pytest.fixture
def held_diff(monkeypatch: pytest.MonkeyPatch):
    held = HeldDiff()
    monkeypatch.setattr(compute, "compute_hunks", held.compute)
    try:
        yield held
    finally:
        held.release.set()
        if held.thread is not None:
            held.thread.join(timeout=5)
            assert not held.thread.is_alive()


async def test_list_is_immediate_and_each_count_replaces_only_its_pending_row(held_diff: HeldDiff) -> None:
    h = RefreshHarness()
    loaded = data("a.py", "b.py", "c.py")
    loaded.all_entries[1] = replace(loaded.all_entries[1], before_text="held")
    loaded.all_entries[2] = replace(loaded.all_entries[2], before_text="third before")
    h.loader.load_current.return_value = CurrentDiffLoad(loaded, loaded)
    h.controller.request_refresh()
    try:
        await wait_for(held_diff.started.is_set, description="second file counting while list remains available")
        assert [row.rel_path for row in h.shown[-1]] == ["a.py", "b.py", "c.py"]
        initial = next(rows for rows in h.shown if len(rows) == 3)
        assert all(row.line_count_result == "pending" and row.line_counts is None for row in initial)
        assert h.shown[-1][0].line_counts == (1, 1)
        assert h.shown[-1][1].line_count_result == h.shown[-1][2].line_count_result == "pending"
        assert h.loader.load_current.call_count == 1
    finally:
        held_diff.release.set()
        await h.drain()
    assert [row.line_counts for row in h.shown[-1]] == [(1, 1)] * 3
    assert not any(row.line_count_result == "pending" for row in h.shown[-1])
    assert h.loader.load_current.call_count == 1


async def test_new_change_is_published_while_counting_and_gets_priority_over_hidden_rows(held_diff: HeldDiff) -> None:
    h = RefreshHarness()
    loaded = data(*(f"file{i}.py" for i in range(11)))
    loaded.all_entries[0] = replace(loaded.all_entries[0], before_text="held")
    h.loader.load_current.return_value = CurrentDiffLoad(loaded, loaded)
    h.controller.request_refresh()
    try:
        await wait_for(held_diff.started.is_set, description="first count held")
        new = replace(entry("new.py"), last_changed_at=100, before_text="new before")
        changed = DiffLoadResult([*loaded.all_entries, new], {}, 1)
        h.loader.load_current.return_value = CurrentDiffLoad(loaded, changed)
        h.controller.request_refresh()
        await wait_for(
            lambda: bool(h.shown[-1]) and h.shown[-1][0].rel_path == "new.py",
            description="new change visible before old count is released",
        )
        assert not held_diff.release.is_set()
        assert len(held_diff.calls) == 1
        assert any(not task.done() for task in h.tasks)
        assert all(call.kwargs["persisted"] is loaded for call in h.loader.load_current.call_args_list[1:])
    finally:
        held_diff.release.set()
        await h.drain()
    assert held_diff.calls[1] == ("new before", "after")
    assert all(row.line_counts == (1, 1) for row in h.shown[-1])


@pytest.mark.parametrize(
    "owner",
    [("other", 1, "/repo"), ("s1", 1, "/other"), ("s1", 1, "/repo")],
    ids=["session", "cwd", "clear-or-rollback-generation"],
)
async def test_owner_change_discards_old_count_even_with_identical_endpoints(held_diff: HeldDiff, owner) -> None:
    h = RefreshHarness()
    loaded = data("same.py")
    loaded.all_entries[0] = replace(loaded.all_entries[0], before_text="held")
    h.loader.load_current.return_value = CurrentDiffLoad(loaded, loaded)
    h.controller.request_refresh()
    try:
        await wait_for(held_diff.started.is_set, description="old owner's count held")
        h.owner = owner
        h.controller.request_refresh(reread=True)
        await wait_for(
            lambda: (
                h.loader.load_current.call_count == 2
                and bool(h.shown[-1])
                and h.shown[-1][0].line_count_result == "pending"
            ),
            description="new owner's metadata accepted",
        )
        held_diff.release.set()
        await h.drain()
    finally:
        held_diff.release.set()
        await h.drain()
    assert len(held_diff.calls) == 2  # The old owner's result cannot seed the new owner's cache.
    assert h.shown[-1][0].line_counts == (1, 1)


async def test_owner_change_before_its_refresh_stops_the_old_count_and_rereads(held_diff: HeldDiff) -> None:
    h = RefreshHarness()
    loaded = data("same.py")
    loaded.all_entries[0] = replace(loaded.all_entries[0], before_text="held")
    h.loader.load_current.return_value = CurrentDiffLoad(loaded, loaded)
    h.controller.request_refresh()
    try:
        await wait_for(held_diff.started.is_set, description="old owner's count held")
        # A rollback or /new commits the new generation before its event reaches the TUI.
        h.owner = ("s1", 1, "/repo")
        held_diff.release.set()
        await wait_for(lambda: all(task.done() for task in h.tasks), description="workers settle for the new owner")
    finally:
        held_diff.release.set()
        h.visible = False
        await h.drain()
    assert len(held_diff.calls) == 2
    assert h.loader.load_current.call_count == 2
    assert h.shown[-1][0].line_counts == (1, 1)


@pytest.mark.parametrize("snapshots", [True, False], ids=["hashes", "live-text"])
async def test_new_endpoints_cannot_receive_the_old_count(tmp_path: Path, held_diff: HeldDiff, snapshots: bool) -> None:
    from chrys.service.mutations.store import SnapshotStore
    from tests.app.tui.util.test_diff_content import snapshot_entry

    h = RefreshHarness()
    directory = tmp_path / "session"
    store = SnapshotStore(directory)
    old = snapshot_entry(store, tmp_path / "same.py", "held", "old")
    if not snapshots:
        old = replace(old, before_hash=None, after_hash=None, before_text="held", after_text="old")
    h.loader.preview_directory.return_value = directory
    loaded = DiffLoadResult([old], {}, 1)
    h.loader.load_current.return_value = CurrentDiffLoad(loaded, loaded)
    h.controller.request_refresh()
    try:
        await wait_for(held_diff.started.is_set, description="old hash pair held")
        new = snapshot_entry(store, tmp_path / "same.py", "new before", "new\nextra\n")
        if not snapshots:
            new = replace(new, before_hash=None, after_hash=None, before_text="new before", after_text="new\nextra\n")
        changed = DiffLoadResult([new], {}, 1)
        h.loader.load_current.return_value = CurrentDiffLoad(loaded, changed)
        h.controller.request_refresh()
        await wait_for(
            lambda: h.shown[-1][0].after_hash == new.after_hash and h.shown[-1][0].after_text == new.after_text,
            description="new endpoints published",
        )
        boundary = len(h.shown)
    finally:
        held_diff.release.set()
        await h.drain()
    assert h.shown[-1][0].line_counts == (2, 1)
    assert all(rows[0].line_counts != (1, 1) for rows in h.shown[boundary:])
    assert len(held_diff.calls) == 2


async def test_rollback_removing_the_file_drops_its_inflight_count(held_diff: HeldDiff) -> None:
    h = RefreshHarness()
    loaded = data("rolled-back.py")
    loaded.all_entries[0] = replace(loaded.all_entries[0], before_text="held")
    h.loader.load_current.return_value = CurrentDiffLoad(loaded, loaded)
    h.controller.request_refresh()
    try:
        await wait_for(held_diff.started.is_set, description="removed file's count held")
        empty = DiffLoadResult.empty()
        h.loader.load_current.return_value = CurrentDiffLoad(empty, empty)
        h.controller.request_refresh(reread=True)
        await wait_for(lambda: h.shown[-1] == (), description="rollback removed preview immediately")
        boundary = len(h.shown)
    finally:
        held_diff.release.set()
        await h.drain()
    assert h.shown[-1] == ()
    assert h.shown[boundary:] == []


async def test_snapshot_directory_change_cannot_adopt_the_old_directorys_count(
    tmp_path: Path, held_diff: HeldDiff
) -> None:
    from chrys.service.mutations.store import SnapshotStore
    from tests.app.tui.util.test_diff_content import snapshot_entry

    h = RefreshHarness()
    first, second = tmp_path / "first", tmp_path / "second"
    old = snapshot_entry(SnapshotStore(first), tmp_path / "same.py", "held", "after")
    new = snapshot_entry(SnapshotStore(second), tmp_path / "same.py", "held", "after")
    loaded = DiffLoadResult([old], {}, 1)
    h.loader.preview_directory.return_value = first
    h.loader.load_current.return_value = CurrentDiffLoad(loaded, loaded)
    h.controller.request_refresh()
    try:
        await wait_for(held_diff.started.is_set, description="first snapshot directory's count held")
        changed = DiffLoadResult([new], {}, 1)
        h.loader.preview_directory.return_value = second
        h.loader.load_current.return_value = CurrentDiffLoad(changed, changed)
        h.controller.request_refresh()
        refresh = h.tasks[-1]
        await refresh
        assert h.shown[-1][0].line_count_result == "pending"
    finally:
        held_diff.release.set()
        await h.drain()
    assert len(held_diff.calls) == 2
    assert h.shown[-1][0].line_counts == (1, 1)


async def test_count_finishing_behind_an_overlay_appears_on_resume_without_a_session_read(held_diff: HeldDiff) -> None:
    h = RefreshHarness()
    loaded = data("covered.py")
    loaded.all_entries[0] = replace(loaded.all_entries[0], before_text="held")
    h.loader.load_current.return_value = CurrentDiffLoad(loaded, loaded)
    h.controller.request_refresh()
    try:
        await wait_for(held_diff.started.is_set, description="count active before overlay")
        h.visible = False
        held_diff.release.set()
        await h.drain()
        assert h.shown[-1][0].line_count_result == "pending"
        h.visible = True
        h.controller.screen_resumed()
        assert h.shown[-1][0].line_counts == (1, 1)
        assert h.loader.load_current.call_count == 1
    finally:
        held_diff.release.set()
        await h.drain()


async def test_one_unreadable_count_does_not_block_other_rows_and_is_retried_on_refresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    h = RefreshHarness()
    loaded = data("a.py", "b.py")
    loaded.all_entries[0] = replace(loaded.all_entries[0], before_text="unreadable")
    h.loader.load_current.return_value = CurrentDiffLoad(loaded, loaded)
    original = compute.compute_hunks
    failed = False
    calls: list[str] = []

    def sometimes_unreadable(before: str, after: str):
        nonlocal failed
        calls.append(before)
        if before == "unreadable" and not failed:
            failed = True
            raise OSError("temporary snapshot failure")
        return original(before, after)

    monkeypatch.setattr(compute, "compute_hunks", sometimes_unreadable)
    h.controller.request_refresh()
    await h.drain()
    assert not any(row.line_count_result == "pending" for row in h.shown[-1])
    assert [row.line_counts for row in h.shown[-1]] == [None, (1, 1)]
    assert h.loader.load_current.call_count == 1
    h.controller.request_refresh()
    await h.drain()
    assert [row.line_counts for row in h.shown[-1]] == [(1, 1), (1, 1)]
    assert calls == ["unreadable", "before", "unreadable"]


async def test_hundred_thousand_line_snapshot_does_not_hold_up_a_new_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from chrys.app.tui.util import diff_content
    from chrys.service.mutations.store import SnapshotStore
    from tests.app.tui.util.test_diff_content import snapshot_entry

    h = RefreshHarness()
    directory = tmp_path / "session"
    big = snapshot_entry(SnapshotStore(directory), tmp_path / "big.txt", "line\n" * 100000, "changed\n" * 100000)
    loaded = DiffLoadResult([big], {}, 1)
    h.loader.preview_directory.return_value = directory
    h.loader.load_current.return_value = CurrentDiffLoad(loaded, loaded)
    began, release = threading.Event(), threading.Event()
    thread: threading.Thread | None = None
    read = diff_content.read_diff_content
    diff = create_autospec(compute.compute_hunks, side_effect=compute.compute_hunks)
    monkeypatch.setattr(compute, "compute_hunks", diff)

    def held_read(store, before_hash, after_hash):
        nonlocal thread
        thread = threading.current_thread()
        began.set()
        if not release.wait(15):
            raise TimeoutError("test did not release snapshot read")
        return read(store, before_hash, after_hash)

    monkeypatch.setattr(diff_content, "read_diff_content", held_read)
    h.controller.request_refresh()
    try:
        await wait_for(began.is_set, description="100000-line snapshot read in flight")
        small = replace(entry("new.py"), last_changed_at=100)
        changed = DiffLoadResult([big, small], {}, 1)
        h.loader.load_current.return_value = CurrentDiffLoad(loaded, changed)
        h.controller.request_refresh()
        await wait_for(
            lambda: len(h.shown[-1]) == 2 and h.shown[-1][0].rel_path == "new.py",
            description="new file published while large snapshot count is held",
        )
        assert not release.is_set()
        assert h.shown[-1][1].line_count_result == "pending"
    finally:
        release.set()
        await h.drain()
        if thread is not None:
            thread.join(timeout=5)
            assert not thread.is_alive()
    assert h.shown[-1][1].line_count_result == "large"
    assert h.shown[-1][1].line_counts is None
    assert h.shown[-1][0].line_counts == (1, 1)
    diff.assert_called_once_with("before", "after")


async def test_count_completion_only_repaints_a_populated_main_screen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, held_diff: HeldDiff
) -> None:
    from chrys.app.tui.widgets.chat.messages import AgentMessage, UserMessage
    from chrys.app.tui.widgets.chat.panel import ChatPanel
    from chrys.app.tui.widgets.chrome.footer import ChrysFooter

    loaded = data("held.py")
    loaded.all_entries[0] = replace(loaded.all_entries[0], before_text="held")

    async def load(self, *, request=None, persisted=None, include_content=True):
        return CurrentDiffLoad(loaded, loaded)

    loader = create_autospec(DiffController.load_current, side_effect=load)
    monkeypatch.setattr(DiffController, "load_current", loader)
    bus = EventBus()
    app = make_chrys_app(tmp_path / "sessions", event_bus=bus)
    try:
        async with app.run_test(size=(140, 40)) as pilot:
            main = app._main_screen
            assert main is not None
            transcript = [
                widget for i in range(24) for widget in (UserMessage(f"Question {i}"), AgentMessage(f"Answer {i}"))
            ]
            await main.query_one(ChatPanel).mount(*transcript)
            await bus.publish(SessionReady(session_id="s1", agent_profile="Code"), raise_handler_errors=True)
            await wait_for(held_diff.started.is_set, pilot=pilot, description="count active behind populated chat")
            preview = main.query_one(DiffPreview)
            await wait_for(
                lambda: preview.rows == 1 and screen_is_settled(app, main),
                pilot=pilot,
                description="settled pending preview",
            )
            layout = create_autospec(main._refresh_layout, side_effect=main._refresh_layout)
            restyle = create_autospec(main.update_node_styles, side_effect=main.update_node_styles)
            footer = main.query_one(ChrysFooter)
            recompose = create_autospec(footer.recompose, side_effect=footer.recompose)
            monkeypatch.setattr(main, "_refresh_layout", layout)
            monkeypatch.setattr(main, "update_node_styles", restyle)
            monkeypatch.setattr(footer, "recompose", recompose)
            reads = loader.call_count
            held_diff.release.set()
            await wait_for(
                lambda: preview.entries[0].line_counts == (1, 1) and screen_is_settled(app, main),
                pilot=pilot,
                description="count rendered without moving chat geometry",
            )
            assert loader.call_count == reads
            layout.assert_not_called()
            restyle.assert_not_called()
            recompose.assert_not_called()
    finally:
        held_diff.release.set()


def exit_probe() -> None:
    """Executed in a child so interpreter/executor joins are part of the assertion."""
    from chrys.app.tui.util import diff_content

    os.chdir(sys.argv[2])
    started = threading.Event()
    finished = threading.Event()
    never_release = threading.Event()
    original = compute.compute_hunks

    def held(before: str, after: str):
        started.set()
        if sys.argv[1] != "actual-diff":
            never_release.wait(5 if sys.argv[1] == "default-executor" else 30)
        try:
            return original(before, after)
        finally:
            finished.set()

    compute.compute_hunks = held
    if sys.argv[1] == "default-executor":

        async def old_count(entry, directory):
            return await asyncio.to_thread(diff_content.compute_preview_counts, entry, directory)

        import chrys.app.tui.screens.main.diff_controller as controller_module

        controller_module.count_preview_entry = old_count

    loaded = data("counting.py")
    if sys.argv[1] == "actual-diff":
        # Adjacent swaps keep SequenceMatcher busy long enough to quit mid-diff.
        lines = [str(i) for i in range(20000)]
        swapped = [line for i in range(0, len(lines), 2) for line in (lines[i + 1], lines[i])]
        loaded.all_entries[0] = replace(
            loaded.all_entries[0], before_text="\n".join(lines), after_text="\n".join(swapped)
        )

    async def load(self, *, request=None, persisted=None, include_content=True):
        return CurrentDiffLoad(loaded, loaded)

    DiffController.load_current = load
    measurements: dict[str, float] = {}

    async def run() -> None:
        from textual.events import Key

        bus = EventBus()
        app = make_chrys_app(Path.cwd() / "sessions", event_bus=bus)
        async with app.run_test(size=(120, 36)):
            await bus.publish(SessionReady(session_id="s1", agent_profile="Code"), raise_handler_errors=True)
            await wait_for(started.is_set, description="real app is counting in a thread")
            if finished.is_set():
                raise RuntimeError("exit probe did not catch an active diff")
            measurements["exit_started"] = time.monotonic()
            # Flush a timestamp before quit: the parent also measures interpreter shutdown.
            Path("exit-started.json").write_text(json.dumps(measurements), encoding="utf-8")
            # Pilot.press waits for CPU idleness, which includes difflib's CPU
            # time. Deliver the same key through the driver without that wait.
            driver = app._driver
            if driver is None:
                raise RuntimeError("exit probe has no input driver")
            driver.send_message(Key("ctrl+q", None))
            await wait_for(lambda: not app.is_running, description="Ctrl+Q handled")
        measurements["app_closed"] = time.monotonic()
        measurements["count_finished_at_app_close"] = float(finished.is_set())

    asyncio.run(run())
    measurements["loop_closed"] = time.monotonic()
    measurements["count_finished_at_loop_close"] = float(finished.is_set())
    sys.stdout.write(json.dumps(measurements) + "\n")
    sys.stdout.flush()


def test_exit_does_not_join_a_running_count(tmp_path: Path) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from tests.app.tui.screens.main.test_diff_preview_counting import exit_probe; exit_probe()",
            "daemon",
            str(tmp_path),
        ],
        # Allow posix_spawn on macOS: forking after Textual initialized native
        # notification services can crash in libSystem's atfork callback.
        close_fds=False,
        env=os.environ
        | {key: str(tmp_path) for key in ("HOME", "USERPROFILE", "APPDATA")}
        | {"PYTHONPATH": str(Path(__file__).resolve().parents[5])},
        stdin=subprocess.DEVNULL,
        capture_output=True,
        encoding="utf-8",
        timeout=ENGINE_TURN_TIMEOUT,
        check=False,
        **windows_hidden_subprocess_kwargs(),
    )
    exited = time.monotonic()
    assert result.returncode == 0, result.stdout + result.stderr
    measurements = json.loads(result.stdout.strip().splitlines()[-1])
    elapsed = exited - measurements["exit_started"]
    assert elapsed < 3, (elapsed, result.stdout, result.stderr)
    assert measurements["count_finished_at_loop_close"] == 0
    (tmp_path / "exit-measurements.json").write_text(
        json.dumps(measurements | {"process_exited": exited, "exit_seconds": elapsed}), encoding="utf-8"
    )
