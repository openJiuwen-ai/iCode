# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Race-free one-shot EventBus correlation."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Protocol, cast
from uuid import uuid4

if TYPE_CHECKING:
    from collections.abc import Callable

    from chrys.foundation.events.bus import EventBus
    from chrys.foundation.events.types import Event


class _CorrelatedEvent(Protocol):
    """Event shape accepted by request-id correlation."""

    request_id: str


class OneShotCorrelation[E: Event]:
    """Subscribe before publish and unsubscribe reliably after one response."""

    def __init__(
        self,
        bus: EventBus,
        event_type: type[E],
        *,
        request_id: str | None = None,
        snapshot: Callable[[E], E] | None = None,
    ) -> None:
        self.request_id = request_id or uuid4().hex
        self._bus = bus
        self._event_type = event_type
        self._snapshot = snapshot
        self._future: asyncio.Future[E] | None = None
        self._resolved_by_event = False

    @property
    def future(self) -> asyncio.Future[E]:
        if self._future is None:
            raise RuntimeError("Correlation has not been entered.")
        return self._future

    @property
    def resolved_by_event(self) -> bool:
        """Whether the subscribed response event, rather than another racer, settled the future."""
        return self._resolved_by_event

    async def __aenter__(self) -> OneShotCorrelation[E]:
        self._future = asyncio.get_running_loop().create_future()
        await self._bus.subscribe(self._event_type, self._on_event)
        return self

    async def __aexit__(self, _exc_type: object, _exc: object, _tb: object) -> None:
        await self._bus.unsubscribe(self._event_type, self._on_event)

    async def _on_event(self, event: E) -> None:
        # This helper is instantiated only for response event classes whose
        # correlation contract includes ``request_id``.
        correlated = cast("_CorrelatedEvent", event)
        if correlated.request_id == self.request_id and not self.future.done():
            response = self._snapshot(event) if self._snapshot is not None else event
            self._resolved_by_event = True
            self.future.set_result(response)
