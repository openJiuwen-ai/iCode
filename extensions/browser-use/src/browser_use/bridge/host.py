# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Chrome native-messaging host: pump frames between Chrome and the plugin.

Chrome launches this executable when the extension calls ``connectNative``. The
plugin writes ``bridge.json`` (port + token) first; this host dials it, proves
the token, then copies frames in both directions until either side closes.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
import time
from pathlib import Path
from typing import BinaryIO, Mapping

from ..config import BrowserEnv
from .protocol import (
    MAX_TCP_MESSAGE_BYTES,
    ChunkAssembler,
    encode_message,
    read_async_frame,
    read_message,
    split_payload,
    write_message,
)


def bridge_file(env: Mapping[str, str] | None = None) -> Path:
    """Must match the plugin's default (``BrowserConfig.bridge_file``)."""
    source = BrowserEnv(env)
    override = (source.get("BRIDGE_FILE") or "").strip()
    if override:
        return Path(override).expanduser()
    return source.state_dir() / "bridge.json"


async def pump(
    native_reader: BinaryIO,
    native_writer: BinaryIO,
    tcp_reader: asyncio.StreamReader,
    tcp_writer: asyncio.StreamWriter,
) -> None:
    """Copy frames until either side closes; never interpret them."""
    assembler = ChunkAssembler()

    async def native_to_tcp() -> None:
        while True:
            message = await asyncio.to_thread(read_message, native_reader)
            if message is None:
                return
            full = assembler.add(message)
            if full is None:
                continue  # a chunk of a larger native message
            tcp_writer.write(encode_message(full, limit=MAX_TCP_MESSAGE_BYTES))
            await tcp_writer.drain()

    async def tcp_to_native() -> None:
        while True:
            message = await read_async_frame(tcp_reader, limit=MAX_TCP_MESSAGE_BYTES)
            if message is None:
                return
            for frame in split_payload(message):
                await asyncio.to_thread(write_message, native_writer, frame)

    tasks = [
        asyncio.create_task(native_to_tcp()),
        asyncio.create_task(tcp_to_native()),
    ]
    try:
        _done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        for task in pending:
            with contextlib.suppress(asyncio.CancelledError):
                await task
    finally:
        for task in tasks:
            task.cancel()


async def _run(port: int, token: str) -> None:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        writer.write(encode_message({"token": token}))
        await writer.drain()
        # Tell the extension that the host reached the plugin bridge, not merely
        # that Chrome opened a native-messaging port.
        await asyncio.to_thread(write_message, sys.stdout.buffer, {"method": "Bridge.ready", "params": {}})
        await pump(sys.stdin.buffer, sys.stdout.buffer, reader, writer)
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()


def _read_rendezvous(path: Path) -> tuple[int, str]:
    for _ in range(50):
        if path.exists():
            break
        time.sleep(0.1)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return int(data["port"]), str(data["token"])
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise SystemExit(f"browser-use-host: cannot read {path}: {exc}") from exc


def main() -> None:
    port, token = _read_rendezvous(bridge_file())
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_run(port, token))
    except OSError as exc:
        raise SystemExit(f"browser-use-host: cannot reach the plugin: {exc}") from exc
    finally:
        with contextlib.suppress(Exception):
            loop.close()
    # ``pump`` also cancels a thread blocked on stdin; asyncio.run would wait
    # for it at shutdown and keep the native port alive, so the extension would
    # never reconnect after the plugin closes. Exit the host process instead.
    os._exit(0)


if __name__ == "__main__":
    main()
