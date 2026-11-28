# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Chrome native-messaging host: pump frames between Chrome and the plugin.

Chrome launches this executable when the extension calls ``connectNative``. The
plugin writes ``bridge.json`` (port + token) first; this host dials it, proves
the token and verifies the server's HMAC over a fresh nonce, then copies frames
in both directions until either side closes.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
import os
import secrets
import sys
import time
from collections.abc import Mapping
from pathlib import Path
from typing import BinaryIO

from .._proc import atomic_write_private
from ..config import BrowserEnv
from .protocol import (
    MAX_TCP_MESSAGE_BYTES,
    ChunkAssembler,
    ProtocolError,
    encode_message,
    read_async_frame,
    read_message,
    split_payload,
    write_message,
)


def bridge_pointer_file(env: Mapping[str, str] | None = None) -> Path:
    """Fixed per-user pointer file recording a non-default ``bridge.json`` path."""
    source = os.environ if env is None else env
    if sys.platform == "win32":
        local_appdata = source.get("LOCALAPPDATA")
        base = Path(local_appdata) if local_appdata else Path.home() / "AppData" / "Local"
    else:
        base = Path.home() / ".local" / "share"
    return base / "icode-browser-use" / "bridge-pointer.txt"


def write_bridge_pointer(target: Path, env: Mapping[str, str] | None = None) -> None:
    """Record *target* in the fixed per-user pointer file so Chrome's host can find it."""
    pointer = bridge_pointer_file(env)
    with contextlib.suppress(OSError):
        atomic_write_private(pointer, str(target.resolve()) + "\n", strict=False)


def bridge_file(env: Mapping[str, str] | None = None) -> Path:
    """Must match the plugin's default (``BrowserConfig.bridge_file``).

    When Chrome launches the host, it does not inherit iCode's custom
    ``ICODE_BROWSER_STATE_DIR`` / ``ICODE_BROWSER_BRIDGE_FILE`` env vars. If
    neither env var is set, fall back to the pointer file written by the plugin
    when it exists and targets an existing discovery file.
    """
    source = BrowserEnv(env)
    override = (source.get("BRIDGE_FILE") or "").strip()
    if override:
        return Path(override).expanduser()
    state_override = (source.get("STATE_DIR") or "").strip()
    if state_override:
        return Path(state_override).expanduser() / "bridge.json"
    pointer = bridge_pointer_file(env)
    if pointer.is_file():
        with contextlib.suppress(OSError):
            pointed = pointer.read_text(encoding="utf-8").strip()
            if pointed:
                candidate = Path(pointed).expanduser()
                if candidate.is_file():
                    return candidate
    return source.state_dir() / "bridge.json"


def compute_server_hmac(token: str, nonce: str) -> str:
    """Compute the expected server handshake MAC proving possession of *token*."""
    return hmac.new(token.encode("utf-8"), nonce.encode("utf-8"), hashlib.sha256).hexdigest()


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


async def _authenticate(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter, token: str, *, timeout: float = 5.0
) -> None:
    """Mutually authenticate: send nonce first, verify server HMAC, then reveal token."""
    nonce = secrets.token_hex(16)
    writer.write(encode_message({"nonce": nonce}))
    await writer.drain()
    try:
        reply = await asyncio.wait_for(read_async_frame(reader, limit=65536), timeout)
    except (asyncio.TimeoutError, ProtocolError, OSError) as exc:
        raise OSError(f"bridge server handshake failed: {exc}") from exc
    received = reply.get("hmac") if isinstance(reply, dict) else None
    expected = compute_server_hmac(token, nonce)
    if not isinstance(received, str) or not secrets.compare_digest(received, expected):
        raise OSError("bridge server failed mutual authentication (invalid HMAC)")
    writer.write(encode_message({"token": token}))
    await writer.drain()


async def _run(port: int, token: str) -> None:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        await _authenticate(reader, writer, token)
        # Tell the extension that the host reached the verified plugin bridge, not merely
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
        raise SystemExit(f"icode-browser-use-host: cannot read {path}: {exc}") from exc


def main() -> None:
    port, token = _read_rendezvous(bridge_file())
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_run(port, token))
    except OSError as exc:
        raise SystemExit(f"icode-browser-use-host: cannot reach the plugin: {exc}") from exc
    finally:
        with contextlib.suppress(Exception):
            loop.close()
    # ``pump`` also cancels a thread blocked on stdin; asyncio.run would wait
    # for it at shutdown and keep the native port alive, so the extension would
    # never reconnect after the plugin closes. Exit the host process instead.
    os._exit(0)


if __name__ == "__main__":
    main()
