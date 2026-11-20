# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Target-domain wrapper: owning *our own* tab and routing commands to it.

The plugin never attaches to user tabs (that would wedge the command queue);
instead it creates a dedicated target on the browser endpoint and attaches to
it with ``flatten=True`` so page commands are routed by ``sessionId``. This
module is the only place that knows that two-step dance.
"""

from __future__ import annotations

import contextlib

from ..errors import Unavailable
from .client import CDPConnection


class TargetManager:
    """Create, close and activate page targets over a browser-level connection."""

    def __init__(self, client: CDPConnection) -> None:
        self._client = client

    async def create_target(self, url: str = "about:blank") -> str:
        """Create a page target and return its ``targetId`` (no attach yet).

        Kept separate from :meth:`attach` so a caller can record the id *before*
        attaching: Chrome emits ``Target.attachedToTarget`` for an explicit
        ``attachToTarget`` too, and a handler that closes "foreign" page targets
        must already know which target is its own.
        """
        created = await self._client.send("Target.createTarget", {"url": url})
        target_id = str(created.get("targetId", ""))
        if not target_id:
            raise Unavailable("Target.createTarget 未返回 targetId")
        return target_id

    async def attach(self, target_id: str) -> str:
        """Attach to *target_id* with ``flatten=True`` and return its session id."""
        attached = await self._client.send(
            "Target.attachToTarget",
            {"targetId": target_id, "flatten": True},
        )
        session_id = str(attached.get("sessionId", ""))
        if not session_id:
            raise Unavailable("Target.attachToTarget 未返回 sessionId")
        return session_id

    async def create_page(self, url: str = "about:blank") -> tuple[str, str]:
        """Create a page and attach to it; return ``(target_id, session_id)``."""
        target_id = await self.create_target(url)
        return target_id, await self.attach(target_id)

    async def close(self, target_id: str) -> None:
        """Close a target, swallowing failures (best-effort recycling)."""
        with contextlib.suppress(Exception):
            await self._client.send("Target.closeTarget", {"targetId": target_id})

    async def activate(self, target_id: str) -> None:
        """Bring a target to the foreground."""
        await self._client.send("Target.activateTarget", {"targetId": target_id})
