# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import asyncio

from browser_use.config import BrowserConfig
from browser_use.session import _MAX_REMEMBERED_LOADS, BrowserSession


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
