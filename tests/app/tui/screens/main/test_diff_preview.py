# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Sidebar refresh policy and navigation contracts, independently of rendered row text."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import replace
from unittest.mock import create_autospec

import pytest

from chrys.app.tui.screens.main.diff_controller import (
    CurrentDiffLoad,
    DiffController,
    DiffPreviewController,
    sorted_preview_entries,
)
from chrys.app.tui.util.diff_entries import DiffFileEntry, DiffLoadResult
from chrys.service.mutations.types import MutationOp
from tests.support.waiting import wait_for


def entry(name: str) -> DiffFileEntry:
    return DiffFileEntry(f"/repo/{name}", name, MutationOp.MODIFY, None, "before", "after", False, "utf-8", True)


def data(*names: str) -> DiffLoadResult:
    entries = [entry(name) for name in names]
    return DiffLoadResult(entries, {1: entries}, 1)


def test_last_change_time_then_display_path_and_net_zero_filtering() -> None:
    a, b, c = (replace(entry(name), last_changed_at=at) for name, at in [("a.py", 30), ("b.py", 40), ("c.py", 20)])
    net_zero = replace(entry("gone.py"), after_text="before", bytes_changed=False)
    loaded = DiffLoadResult([a, b, c, net_zero], {1: [c, a], 2: [b, a]}, 2)
    assert [row.rel_path for row in sorted_preview_entries(loaded)] == ["b.py", "a.py", "c.py"]


class RefreshHarness:
    def __init__(self) -> None:
        self.owner = ("s1", 0, "/repo")
        self.visible = True
        self.shown: list[tuple[DiffFileEntry, ...]] = []
        self.tasks: list[asyncio.Task[object]] = []
        self.loader = create_autospec(DiffController, instance=True, spec_set=True)
        self.disk = data("disk.py")
        self.loader.load_current.return_value = CurrentDiffLoad(self.disk, self.disk)
        self.loader.preview_entries.side_effect = lambda data, _sid: sorted_preview_entries(data)
        self.loader.preview_directory.return_value = None
        self.controller = DiffPreviewController(
            diff=self.loader,
            owner=lambda: self.owner,
            visible=lambda: self.visible,
            show=self.shown.append,
            start_worker=self.start,
        )

    def start(self, work: Callable[[], Awaitable[object]]) -> object:
        task = asyncio.create_task(work())
        self.tasks.append(task)
        return task

    async def drain(self) -> None:
        drained = 0
        while drained < len(self.tasks):
            batch = self.tasks[drained:]
            drained = len(self.tasks)
            await asyncio.gather(*batch)


async def test_tool_refresh_merges_cached_disk_and_hidden_requests_keep_the_reread() -> None:
    h = RefreshHarness()
    h.controller.request_refresh(reread=True)
    await h.drain()
    h.controller.request_refresh()
    await h.drain()
    assert [call.kwargs["persisted"] for call in h.loader.load_current.call_args_list] == [None, h.disk]
    h.visible = False
    h.controller.request_refresh(reread=True)
    h.controller.request_refresh()
    await h.drain()
    assert h.loader.load_current.call_count == 2
    h.visible = True
    h.controller.screen_resumed()
    await h.drain()
    assert h.loader.load_current.call_args.kwargs == {"persisted": None, "include_content": False}


@pytest.mark.parametrize("reread_again", [False, True])
async def test_inflight_read_shows_progress_and_only_repeats_a_new_reread(reread_again: bool) -> None:
    h = RefreshHarness()
    began, release, next_began, next_release = (asyncio.Event() for _ in range(4))
    first = data("first.py")
    calls = 0

    async def load(*, request=None, persisted=None, include_content=True):
        nonlocal calls
        calls += 1
        if calls == 1:
            began.set()
            await release.wait()
            return CurrentDiffLoad(first, first)
        if calls == 2:
            assert persisted is (None if reread_again else first)
            next_began.set()
            await next_release.wait()
        return CurrentDiffLoad(h.disk, h.disk)

    h.loader.load_current.side_effect = load
    h.controller.request_refresh(reread=True)
    await wait_for(began.is_set, description="disk request started")
    h.controller.request_refresh(reread=reread_again)
    release.set()
    try:
        await wait_for(next_began.is_set, description="follow-up computation started")
        assert [row.rel_path for row in h.shown[-1]] == ["first.py"]
    finally:
        next_release.set()
        await h.drain()
    assert [row.rel_path for row in h.shown[-1]] == ["disk.py"]
    h.controller.request_refresh()
    await h.drain()
    assert h.loader.load_current.call_args.kwargs["persisted"] is h.disk


async def test_continuous_tool_results_publish_each_completed_computation_without_rereading() -> None:
    h = RefreshHarness()
    final_began, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def load(*, request=None, persisted=None, include_content=True):
        nonlocal calls
        calls += 1
        current = calls
        if current < 5:
            h.controller.request_refresh()
        else:
            final_began.set()
            await release.wait()
        return CurrentDiffLoad(h.disk, data(f"progress{current}.py"))

    h.loader.load_current.side_effect = load
    h.controller.request_refresh(reread=True)
    try:
        await wait_for(final_began.is_set, description="continuous updates still in flight")
        from itertools import groupby

        assert [key for key, _ in groupby(rows[0].rel_path for rows in h.shown if rows)] == [
            f"progress{i}.py" for i in range(1, 5)
        ]
        assert sum(call.kwargs["persisted"] is None for call in h.loader.load_current.call_args_list) == 1
    finally:
        release.set()
        await h.drain()
    assert h.shown[-1][0].rel_path == "progress5.py"


@pytest.mark.parametrize("owner", [("s2", 1, "/repo"), ("s1", 1, "/other"), ("s1", 1, "/repo")])
async def test_session_or_cwd_change_clears_immediately_and_discards_inflight_results(owner) -> None:
    h = RefreshHarness()
    h.controller.request_refresh()
    await h.drain()
    began, release = asyncio.Event(), asyncio.Event()

    async def load(*, request=None, persisted=None, include_content=True):
        began.set()
        await release.wait()
        return CurrentDiffLoad(h.disk, data("new.py"))

    h.loader.load_current.side_effect = load
    h.controller.request_refresh()
    await wait_for(lambda: began.is_set() or h.tasks[-1].done(), description="old request started")
    assert began.is_set()
    h.owner = owner
    h.controller.request_refresh(reread=True)
    assert [row.rel_path for row in h.shown[-1]] == ([] if (owner[0], owner[2]) != ("s1", "/repo") else ["disk.py"])
    release.set()
    await h.drain()
    assert [row.rel_path for row in h.shown[-1]] == ["new.py"]


async def test_failed_compute_preserves_rows_and_next_merge_honors_pending_reread() -> None:
    h = RefreshHarness()
    h.controller.request_refresh()
    await h.drain()
    h.loader.load_current.side_effect = RuntimeError("read failed")
    h.controller.request_refresh(reread=True)
    await h.drain()
    assert [row.rel_path for row in h.shown[-1]] == ["disk.py"]
    h.loader.load_current.side_effect = None
    h.controller.request_refresh()
    await h.drain()
    assert h.loader.load_current.call_args.kwargs["persisted"] is None


def test_move_source_uses_the_latest_period_even_if_previously_modified() -> None:
    source = replace(entry("source.py"), last_changed_at=10)
    destination = replace(entry("destination.py"), operation=MutationOp.MOVE, old_path=source.path, last_changed_at=30)
    deleted = replace(source, operation=MutationOp.DELETE, after_text="", last_changed_at=30)
    data = DiffLoadResult(
        [
            deleted,
            replace(entry("middle.py"), last_changed_at=20),
            replace(entry("destination.py"), last_changed_at=30),
        ],
        {1: [source], 2: [entry("middle.py")], 3: [destination]},
        3,
    )
    assert [row.rel_path for row in sorted_preview_entries(data)] == ["destination.py", "source.py", "middle.py"]


async def test_newer_request_runs_after_the_previous_computation_raises() -> None:
    h = RefreshHarness()
    began, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def load(*, request=None, persisted=None, include_content=True):
        nonlocal calls
        calls += 1
        if calls == 1:
            began.set()
            await release.wait()
            raise OSError("temporary read error")
        return CurrentDiffLoad(h.disk, h.disk)

    h.loader.load_current.side_effect = load
    h.controller.request_refresh(reread=True)
    await wait_for(lambda: began.is_set() or h.tasks[0].done(), description="first read started")
    assert began.is_set()
    h.controller.request_refresh()
    release.set()
    await h.drain()
    assert calls == 2
    assert [row.rel_path for row in h.shown[-1]] == ["disk.py"]


async def test_resume_in_a_different_session_invalidates_cached_rows_before_reading() -> None:
    h = RefreshHarness()
    h.controller.request_refresh()
    await h.drain()
    h.owner = ("s2", 1, "/repo")
    h.controller.screen_resumed()
    assert h.shown[-1] == ()
    await h.drain()
    assert h.loader.load_current.call_args.kwargs["persisted"] is None
