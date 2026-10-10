# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Per-HTTP-attempt identity, carried by an explicitly bound wire-call scope.

Hooks observe preparation and response headers, not stream completion. A
prepared attempt without received headers may have failed before reaching the
provider; the enclosing exchange records the eventual stream/run outcome.
No bodies are read and no additional synchronous persistence is introduced.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from chrys.foundation.trajectory.event_types import EventType
from chrys.foundation.trajectory.ids import new_analytics_id
from chrys.foundation.util.chrys_headers import REQUEST_ATTEMPT_ID_HEADER
from chrys.service.llm.provider_request_ids import read_provider_request_id

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    import httpx

    from chrys.foundation.trajectory.context import TrajectoryContext

REQUEST_ATTEMPT_ID_EXTENSION = "chrys_request_attempt_id"
REQUEST_ATTEMPT_ID_METADATA = "_chrys_request_attempt_id"
_PROVIDER_REQUEST_ID_METADATA = "provider_request_id"
_CONTEXT_EXTENSION = "chrys_request_trajectory_context"
_CURRENT: ContextVar[RequestTracking | None] = ContextVar("chrys_wire_request_tracking", default=None)


@dataclass
class RequestTracking:
    """One wire acquisition; its HTTP retries/redirects each get a new ID."""

    context: TrajectoryContext | None = None
    request_attempt_id: str | None = None
    provider_request_id: str | None = None

    @contextmanager
    def scope(self) -> Iterator[None]:
        token = _CURRENT.set(self)
        try:
            yield
        finally:
            _CURRENT.reset(token)

    def response_facts(self) -> dict[str, str]:
        facts: dict[str, str] = {}
        if self.request_attempt_id is not None:
            facts["request_attempt_id"] = self.request_attempt_id
        if self.provider_request_id is not None:
            facts[_PROVIDER_REQUEST_ID_METADATA] = self.provider_request_id
        return facts


def build_request_tracking_hooks() -> dict[str, list[Callable[..., Any]]]:
    """Stamp every actual HTTP attempt, including SDK retries and redirects."""

    async def prepared(request: httpx.Request) -> None:
        attempt_id = new_analytics_id()
        request.headers[REQUEST_ATTEMPT_ID_HEADER] = attempt_id
        request.extensions[REQUEST_ATTEMPT_ID_EXTENSION] = attempt_id
        tracking = _CURRENT.get()
        context = tracking.context if tracking is not None else None
        # Redirects copy extensions: always replace the previous hop's scope.
        request.extensions[_CONTEXT_EXTENSION] = context
        if tracking is not None:
            tracking.request_attempt_id = attempt_id
            tracking.provider_request_id = None
        if context is not None:
            context.sink.emit_soon(
                context.draft(
                    EventType.MODEL_REQUEST_PREPARED,
                    operation_id=context.exchange_operation_id,
                    payload={"request_attempt_id": attempt_id},
                )
            )

    async def received(response: httpx.Response) -> None:
        from chrys.foundation.trajectory.context import TrajectoryContext

        attempt_id = response.request.extensions.get(REQUEST_ATTEMPT_ID_EXTENSION)
        if not isinstance(attempt_id, str):
            return
        provider_id = read_provider_request_id(response.headers)
        tracking = _CURRENT.get()
        if tracking is not None and tracking.request_attempt_id == attempt_id:
            tracking.provider_request_id = provider_id
        context = response.request.extensions.get(_CONTEXT_EXTENSION)
        if isinstance(context, TrajectoryContext):
            facts: dict[str, Any] = {"request_attempt_id": attempt_id, "status_code": response.status_code}
            if provider_id is not None:
                facts[_PROVIDER_REQUEST_ID_METADATA] = provider_id
            context.sink.emit_soon(
                context.draft(
                    EventType.MODEL_REQUEST_HEADERS_RECEIVED,
                    operation_id=context.exchange_operation_id,
                    payload=facts,
                )
            )

    return {"request": [prepared], "response": [received]}
