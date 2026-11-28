# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""What a cancelled sandbox command forgets from the backend's sandbox-id cache."""

from __future__ import annotations

import asyncio

from chrys.service.sandbox import backend


async def test_a_cancelled_writable_command_does_not_wait_for_the_cache_lock() -> None:
    """Only a read-only id is forgotten on cancel; a writable one returns while another command holds the lock."""
    backend._writable_by_root["/work"] = "writable-id"
    backend._readonly_id = "readonly-id"
    lock = backend._gate()
    await lock.acquire()
    forgetting: asyncio.Task[None] | None = None
    try:
        await asyncio.wait_for(backend._forget_cached_id("writable-id"), timeout=1)
        assert backend._writable_by_root == {"/work": "writable-id"}
        forgetting = asyncio.create_task(backend._forget_cached_id("readonly-id"))
        await asyncio.sleep(0)
        assert not forgetting.done()
    finally:
        lock.release()
    await forgetting
    assert backend._readonly_id is None
    assert backend._retiring == {"readonly-id"}


async def test_a_cancel_forgets_a_read_only_id_its_server_parked() -> None:
    backend._use_server_cache("http://a.test")
    backend._readonly_id = "a-readonly"
    backend._readonly_root = "/ro"
    backend._use_server_cache("http://b.test")
    assert backend._readonly_id is None
    assert backend._is_cached("a-readonly")
    await backend._forget_cached_id("a-readonly")
    assert not backend._is_cached("a-readonly")
    assert backend._retiring == {"a-readonly"}
    backend._use_server_cache("http://a.test")
    assert backend._readonly_id is None
    assert backend._readonly_root is None
