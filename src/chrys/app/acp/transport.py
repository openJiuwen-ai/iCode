# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""ACP server output teardown must not wait for a departed client to read."""

from __future__ import annotations

import asyncio
from typing import Any

from acp.task.sender import MessageSender


class AbortableMessageSender(MessageSender):
    """Discard unsent output on close so SDK request cancellation can proceed.

    The pinned SDK's close waits for this sender before cancelling its request
    tasks. Its default sender flushes the queue and propagates an earlier write
    failure, either of which can prevent requests from releasing session locks.
    ``_closed`` and ``_task`` are SDK internals covered by transport regressions.
    """

    async def send(self, payload: dict[str, Any]) -> None:
        # A request handler that consumes the shutdown cancellation still
        # returns, and the SDK then sends its response. The stopped loop would
        # never resolve that send, and SDK shutdown waits for the handler.
        if self._closed:
            return
        await super().send(payload)

    async def close(self) -> None:
        self._closed = True
        self._task.cancel()
        # Consume both cancellation and an already-failed pipe write. No flush
        # or writer.wait_closed: either can depend on the peer reading stdout.
        await asyncio.gather(self._task, return_exceptions=True)
