# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Publish invocation facts with identity captured before asynchronous work.

Callers bind an immutable InvocationOrigin to a BoundEmitter for each invocation.
Shared model callbacks use invocation_routing_key only to find that registered
publisher, then retain the emitter across awaits and registry release. Neither
current_invocation_origin nor the routing key repairs a missing event origin.

Turn, kernel child and ACP child facts share the Invocation event family. Caller
commands, permission/ask-user correlation and acquire diagnostics keep their own
protocols; frontend consumers select the existing projection by origin.kind.
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass

from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import Event, InvocationEvent
from chrys.foundation.models.invocations import InvocationOrigin
from chrys.foundation.models.invocations import current_invocation_origin as current_invocation_origin

invocation_routing_key: ContextVar[str] = ContextVar("invocation_routing_key", default="")


@dataclass(frozen=True, slots=True)
class BoundEmitter:
    """Bind identity when creating a callback, never infer it when publishing.

    Invocation facts must carry the same origin as this publisher. Missing live
    identity is a routing error, independent of old on-disk audit records.
    """

    bus: EventBus | None
    origin: InvocationOrigin

    def __post_init__(self) -> None:
        if not isinstance(self.origin, InvocationOrigin):
            raise ValueError("Cannot route an invocation event without a live origin")

    async def publish(self, event: Event) -> None:
        if event.session_id and event.session_id != self.origin.session_id:
            raise ValueError("Event session does not match its bound origin")
        if isinstance(event, InvocationEvent) and event.origin != self.origin:
            raise ValueError("Event origin does not match its bound publisher")
        if self.bus is not None:
            await self.bus.publish(event)


class InvocationPublishers:
    """Route shared-client callbacks to publishers bound by their caller.

    The context variable supplies only the existing callback-buffer key. An
    origin is never inferred from it: a caller must register its immutable
    bound publisher before execution. A stale or unregistered callback fails
    independently instead of borrowing a later invocation's identity.
    """

    def __init__(self, bus: EventBus | None) -> None:
        self._bus = bus
        self._emitters: dict[str, BoundEmitter] = {}

    def bind(self, origin: InvocationOrigin) -> BoundEmitter:
        emitter = BoundEmitter(self._bus, origin)
        self._emitters[origin.invocation_id] = emitter
        return emitter

    def unbind(self, origin: InvocationOrigin) -> None:
        self._emitters.pop(origin.invocation_id, None)

    def capture(self) -> BoundEmitter:
        """Capture the explicitly registered publisher before a callback awaits."""
        invocation_id = invocation_routing_key.get()
        try:
            return self._emitters[invocation_id]
        except KeyError as exc:
            raise ValueError("Cannot route an unbound invocation callback") from exc
