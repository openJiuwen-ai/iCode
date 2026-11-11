# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Subprocess doubles for code that reads a process's output pipes, and a liveness check for real ones."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import psutil


def still_running(process: psutil.Process) -> bool:
    """Whether *process*, a ``psutil.Process`` captured while alive, still runs; a zombie has ended.

    It can exit between any two reads, so a process that vanishes while read has ended too.
    """
    import psutil

    try:
        return process.is_running() and process.status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


def finished_reader(data: bytes) -> asyncio.StreamReader:
    """A pipe reader holding *data*, then the end of the stream."""
    reader = asyncio.StreamReader()
    reader.feed_data(data)
    reader.feed_eof()
    return reader


class ExitedProcess:
    """A process that already wrote *stdout* and *stderr* and exited with *returncode*.

    Build it inside the running event loop: its pipe readers belong to that loop.
    """

    pid = 0
    """No real process: a tree kill of it does nothing."""

    def __init__(self, stdout: bytes = b"", stderr: bytes = b"", returncode: int = 0) -> None:
        self.stdout = finished_reader(stdout)
        self.stderr = finished_reader(stderr)
        self.returncode = returncode

    async def wait(self) -> int:
        return self.returncode
