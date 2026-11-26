# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Two authenticated, bounded loopback messages for an idle native TUI."""

from __future__ import annotations

import asyncio
import hmac
import secrets
import socket
import struct
import time
from collections.abc import Callable
from typing import Literal

from chrys.service.state.takeover import TakeoverOwner

_ADDRESS = "127.0.0.1"
_TIMEOUT = 0.65
# Operation, unique request id and monotonic deadline (same host).
_HEADER = struct.Struct("!c16sd")
_MAC_BYTES = 32


class TakeoverListener(asyncio.DatagramProtocol):
    def __init__(
        self, current_owner: Callable[[], TakeoverOwner | None], idle: Callable[[], bool], finish: Callable[[], None]
    ) -> None:
        self._owner = current_owner
        self._idle = idle
        self._finish = finish
        self._transport: asyncio.DatagramTransport | None = None

    async def start(self) -> int:
        transport, _ = await asyncio.get_running_loop().create_datagram_endpoint(
            lambda: self, local_addr=(_ADDRESS, 0), family=socket.AF_INET
        )
        self._transport = transport
        return int(transport.get_extra_info("sockname")[1])

    def close(self) -> None:
        if self._transport is not None:
            self._transport.close()
            self._transport = None

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        owner = self._owner()
        if self._transport is None or owner is None or len(data) != _HEADER.size + _MAC_BYTES:
            return
        header, mac = data[: _HEADER.size], data[_HEADER.size :]
        action, _, expires = _HEADER.unpack(header)
        now = time.monotonic()
        if not (now <= expires <= now + _TIMEOUT) or action not in (b"?", b"!"):
            return
        if not hmac.compare_digest(mac, hmac.digest(owner.key, header, "sha256")):
            return
        idle = self._idle()
        answer = b"Y" if idle else b"N"
        self._transport.sendto(answer + hmac.digest(owner.key, header + answer, "sha256"), addr)
        if idle and action == b"!":
            # No await between checking the live session and committing its exit.
            self._finish()


async def query_owner(owner: TakeoverOwner, action: Literal["status", "take"]) -> bool:
    header = _HEADER.pack(b"!" if action == "take" else b"?", secrets.token_bytes(16), time.monotonic() + _TIMEOUT)
    loop = asyncio.get_running_loop()
    try:
        async with asyncio.timeout(_TIMEOUT):
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.setblocking(False)
                await loop.sock_connect(sock, (_ADDRESS, owner.port))
                await loop.sock_sendall(sock, header + hmac.digest(owner.key, header, "sha256"))
                reply = await loop.sock_recv(sock, 64)
    except OSError, TimeoutError:
        return False
    return hmac.compare_digest(reply, b"Y" + hmac.digest(owner.key, header + b"Y", "sha256"))
