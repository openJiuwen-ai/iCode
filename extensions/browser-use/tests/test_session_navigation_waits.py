# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import asyncio

from icode_browser_use.config import BrowserConfig
from icode_browser_use.session import _MAX_REMEMBERED_LOADS, BrowserSession


def session():
    result = BrowserSession(BrowserConfig(allowed_domains=("example.com",)))
    result._session_id = "page"
    result._main_frame_id = "main"
    return result


async def soft_nav(s, url="https://example.com/#x"):
    await s._on_navigated_within_document({"frameId": "main", "url": url}, "page")


def test_second_soft_navigation_waits_for_its_own_event():
    async def run():
        s = session()
        first = s.nav_seq
        await soft_nav(s)
        assert await s.wait_for_navigation(after=first, timeout=0.05) == ""

        second = s.nav_seq
        # No new same-document navigation: an old one must not satisfy the wait.
        assert await s.wait_for_navigation(after=second, timeout=0.05) is None

        waiter = asyncio.create_task(s.wait_for_navigation(after=second, timeout=1))
        await asyncio.sleep(0)
        await soft_nav(s, "https://example.com/#y")
        assert await waiter == ""

    asyncio.run(run())


def test_load_memory_evicts_oldest_not_newest():
    async def run():
        s = session()
        for index in range(_MAX_REMEMBERED_LOADS + 10):
            s._signal_loaded(f"loader-{index}")
        newest = f"loader-{_MAX_REMEMBERED_LOADS + 9}"
        assert await s.wait_for_load(newest, timeout=0.01)
        assert not await s.wait_for_load("loader-0", timeout=0.01)

    asyncio.run(run())


def test_wait_for_ready_returns_domcontentloaded_after_grace_when_load_stalls():
    async def run():
        s = session()

        async def emit_dcl():
            await asyncio.sleep(0.01)
            await s._on_lifecycle_event({"name": "DOMContentLoaded", "loaderId": "L-slow"}, "page")

        task = asyncio.create_task(emit_dcl())
        try:
            state = await s.wait_for_ready("L-slow", timeout=2.0, load_grace=0.04)
            assert state == "domcontentloaded"
        finally:
            await task

    asyncio.run(run())


def test_wait_for_ready_returns_load_within_grace_and_committed_on_timeout():
    async def run():
        s = session()

        async def emit_dcl_then_load():
            await asyncio.sleep(0.01)
            await s._on_lifecycle_event({"name": "DOMContentLoaded", "loaderId": "L-fast"}, "page")
            await asyncio.sleep(0.01)
            await s._on_lifecycle_event({"name": "load", "loaderId": "L-fast"}, "page")

        task = asyncio.create_task(emit_dcl_then_load())
        try:
            assert await s.wait_for_ready("L-fast", timeout=1.0, load_grace=0.2) == "load"
        finally:
            await task

        await s._on_frame_navigated(
            {"frame": {"id": "main", "loaderId": "L-commit", "url": "https://example.com/c"}}, "page"
        )
        assert await s.wait_for_ready("L-commit", timeout=0.03, load_grace=0.01) == "committed"
        assert await s.wait_for_ready("L-none", timeout=0.03, load_grace=0.01) == "timeout"

    asyncio.run(run())
