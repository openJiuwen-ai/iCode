# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Bound native clipboard calls by owning and retiring their helper process."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

from chrys.foundation.platform.process import ManagedStdioProcess, spawn_managed_stdio_process
from chrys.foundation.util.once_close import finish_close

_START_TIMEOUT_SECONDS = 5.0
_WRITE_TIMEOUT_SECONDS = 1.0
_STOP_TIMEOUT_SECONDS = 0.5
_unconfirmed_exit = False


def _worker_python_flags() -> tuple[str, ...]:
    # Preserve the interpreter's actual isolation policy: PyApp uses -I,
    # while ordinary user-site and PYTHONPATH installs must remain available.
    if sys.flags.isolated:
        return ("-I",)
    flags = ["-P"]
    if sys.flags.ignore_environment:
        flags.append("-E")
    if sys.flags.no_user_site:
        flags.append("-s")
    return tuple(flags)


def _worker_environment() -> dict[str, str]:
    env = dict(os.environ)
    if not sys.flags.ignore_environment and env.get("PYTHONPATH"):
        # CPython already resolved explicit PYTHONPATH entries at startup.
        # Reuse those search paths across both helper and TUI workdir changes,
        # excluding the implicit script/cwd entry that the helper's -P forbids.
        search_path = sys.path if sys.flags.safe_path else sys.path[1:]
        env["PYTHONPATH"] = os.pathsep.join(os.path.abspath(path) for path in search_path)
    return env


def copy_windows_text(text: str) -> None:
    """Run from the serial clipboard thread, never the UI's event loop."""
    if _unconfirmed_exit:
        raise OSError("Clipboard worker exit was not confirmed; restart the application before copying again")
    asyncio.run(_copy(text))


async def _stop(process: ManagedStdioProcess) -> None:
    """A kill request alone is not proof that an old write can no longer run."""
    global _unconfirmed_exit
    try:
        process.kill_tree()
        async with asyncio.timeout(_STOP_TIMEOUT_SECONDS):
            await process.wait()
    except BaseException:
        # Keep failing promptly, rather than queueing forever or permitting
        # another writer to race a process whose death was not confirmed.
        _unconfirmed_exit = True
        raise


async def _copy(text: str) -> None:
    process: ManagedStdioProcess | None = None
    try:
        async with asyncio.timeout(_START_TIMEOUT_SECONDS):
            # The managed Windows child joins a kill-on-close Job before it
            # runs. Parent death therefore cannot leave a late clipboard write.
            process = await spawn_managed_stdio_process(
                sys.executable,
                *_worker_python_flags(),
                "-m",
                "chrys.foundation.platform.clipboard_worker",
                cwd=str(Path(sys.executable).parent),
                env=_worker_environment(),
                limit=64 * 1024,
            )
            # Cold interpreter startup may be slow (for example, antivirus
            # scanning). Start the native deadline only after imports finish.
            if await process.stdout.readexactly(1) != b"R":
                raise OSError("Clipboard worker did not become ready")
        async with asyncio.timeout(_WRITE_TIMEOUT_SECONDS):
            process.stdin.write(json.dumps(text, ensure_ascii=False).encode("utf-8"))
            await process.stdin.drain()
            process.close_stdin()
            result = await process.wait()
        if result == 2:
            raise PermissionError("System clipboard is busy")
        if result != 0:
            raise OSError(f"Clipboard worker exited with status {result}")
    finally:
        if process is not None:
            try:
                if process.returncode is None:
                    await finish_close(asyncio.create_task(_stop(process)))
            finally:
                process.close_transports()
