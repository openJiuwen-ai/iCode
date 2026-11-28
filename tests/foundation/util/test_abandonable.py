# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

import asyncio
import threading
import time

from chrys.foundation.util.abandonable import run_abandonable


def test_exit_does_not_wait_for_a_held_read() -> None:
    started, release = threading.Event(), threading.Event()

    def held_read() -> None:
        started.set()
        release.wait(30)

    async def open_and_quit() -> None:
        read = asyncio.create_task(run_abandonable("test-held-read", held_read))
        await asyncio.to_thread(started.wait, 5)
        read.cancel()

    begun = time.monotonic()
    try:
        asyncio.run(open_and_quit())
        assert time.monotonic() - begun < 3
    finally:
        release.set()


async def test_result_and_error_reach_the_caller() -> None:
    assert await run_abandonable("test-read", sum, (1, 2)) == 3

    def fail() -> None:
        raise OSError("unreadable")

    try:
        await run_abandonable("test-read", fail)
    except OSError as exc:
        assert str(exc) == "unreadable"
    else:
        raise AssertionError("error was not raised")
