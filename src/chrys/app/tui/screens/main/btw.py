# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Frontend ownership and stale-result fences for the disposable /btw card."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from uuid import uuid4

from chrys.foundation.events.types import (
    InvocationContentDelta,
    InvocationContentFinished,
    InvocationSideQuestionEnded,
    InvocationSideQuestionFailed,
    SideQuestionCancelled,
    SideQuestionHistoryRequested,
    SideQuestionRequested,
    SideQuestionsReset,
)
from chrys.foundation.i18n import msg
from chrys.foundation.util.once_close import OnceClose

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from chrys.app.tui.widgets.btw import BtwPanel
    from chrys.foundation.events.bus import EventBus
    from chrys.foundation.models.side_questions import SideQuestionEntry

BTW_BUSY = msg("tui.btw.busy", fallback="A side question is running. Close it before asking another.")


class BtwController:
    """The card has one identity; closing it invalidates answers before cancellation is sent."""

    def __init__(self, *, bus: EventBus, panel: BtwPanel, focus_input: Callable[[], None]) -> None:
        self._bus = bus
        self.panel = panel
        self._focus_input = focus_input
        self._session_id = ""
        self._text = ""
        self._entries: tuple[SideQuestionEntry, ...] = ()
        self._index = -1
        self._tasks: set[asyncio.Task[None]] = set()
        self._close = OnceClose(self._shutdown)

    async def subscribe(self) -> None:
        await self._bus.subscribe(InvocationContentDelta, self.on_delta)
        await self._bus.subscribe(InvocationContentFinished, self.on_finished)
        await self._bus.subscribe(InvocationSideQuestionFailed, self.on_failed)
        await self._bus.subscribe(InvocationSideQuestionEnded, self.on_ended)
        await self._bus.subscribe(SideQuestionsReset, self.on_reset)

    def _start(self, coroutine: Awaitable[None]) -> None:
        task = asyncio.ensure_future(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def ask(self, session_id: str, question: str) -> None:
        self._session_id = session_id
        self._text = ""
        request_id = uuid4().hex
        self.panel.begin(request_id, question)
        self._start(
            self._request(SideQuestionRequested(session_id=session_id, request_id=request_id, question=question))
        )

    def open_history(self, session_id: str) -> None:
        if self.panel.running:
            return
        self._session_id = session_id
        self._entries = ()
        self._index = -1
        request_id = uuid4().hex
        # A history lookup must not open an empty card while the read is in flight.
        self.panel.show_history_notice(request_id, loading=True, reveal=False)
        self._start(self._open_history(request_id))

    async def _read_entries(self, request_id: str) -> bool | None:
        event = SideQuestionHistoryRequested(session_id=self._session_id)
        await self._bus.publish(event)
        if self.panel.request_id != request_id or event.session_id != self._session_id:
            return None
        if not event.accepted:
            return None
        self._entries = event.entries
        return not event.failed

    async def _open_history(self, request_id: str) -> None:
        loaded = await self._read_entries(request_id)
        if self.panel.request_id != request_id:
            return
        if loaded is None:
            self.panel.notify_history_unavailable()
            return
        if not loaded and not self._entries:
            self.panel.show_history_notice(request_id)
            return
        if not self._entries:
            self.panel.notify_empty_history()
            return
        if not loaded:
            self.panel.notify_history_read_failed()
        self._index = len(self._entries) - 1
        await self._show_current(request_id)

    async def _show_current(self, request_id: str) -> None:
        entry = self._entries[self._index]
        await self.panel.show_record(request_id, entry)
        if self.panel.request_id == request_id:
            self.panel.set_history_position(self._index, len(self._entries), entry.created_at)

    async def navigate(self, delta: int) -> None:
        index = self._index + delta
        if self.panel.running or not self.panel.request_id or not 0 <= index < len(self._entries):
            return
        self._index = index
        await self._show_current(uuid4().hex)

    async def _request(self, event: SideQuestionRequested) -> None:
        await self._bus.publish(event)
        if self.panel.request_id != event.request_id:
            # Closing before admission returned still cancels the admitted request.
            await self._bus.publish(SideQuestionCancelled(session_id=event.session_id, request_id=event.request_id))
        elif not event.accepted:
            self.panel.fail(event.request_id)

    def close_question(self, *, focus: bool = True) -> bool:
        request_id = self.panel.request_id
        if not request_id:
            return False
        if focus:
            self._focus_input()
        self.panel.close_question()
        self._text = ""
        self._entries = ()
        self._index = -1
        self._start(self._bus.publish(SideQuestionCancelled(session_id=self._session_id, request_id=request_id)))
        return True

    def _matches(
        self,
        event: InvocationContentDelta
        | InvocationContentFinished
        | InvocationSideQuestionFailed
        | InvocationSideQuestionEnded,
    ) -> bool:
        return (
            event.origin.kind == "side_question"
            and event.origin.session_id == self._session_id
            and event.origin.invocation_id == self.panel.request_id
        )

    async def on_delta(self, event: InvocationContentDelta) -> None:
        if self._matches(event) and event.channel == "answer":
            self._text += event.text

    async def on_finished(self, event: InvocationContentFinished) -> None:
        if self._matches(event) and event.channel == "answer":
            if event.status == "completed":
                request_id = event.origin.invocation_id
                await self.panel.finish(request_id, self._text)
                if await self._read_entries(request_id) is not None and self._entries:
                    self._index = len(self._entries) - 1
                    self.panel.set_history_position(self._index, len(self._entries), self._entries[-1].created_at)
            else:
                self.panel.fail(event.origin.invocation_id)

    async def on_ended(self, event: InvocationSideQuestionEnded) -> None:
        if self._matches(event):
            self.close_question(focus=False)

    async def on_failed(self, event: InvocationSideQuestionFailed) -> None:
        if event.kind == "save":
            if event.origin.kind == "side_question" and event.origin.session_id == self._session_id:
                self.panel.warn_unsaved()
        elif self._matches(event):
            self.panel.fail(
                event.origin.invocation_id, blocked_reason=event.reason if event.kind == "blocked" else None
            )

    async def on_reset(self, event: SideQuestionsReset) -> None:
        if event.session_id == self._session_id:
            self.close_question(focus=False)

    async def shutdown(self) -> None:
        self.close_question(focus=False)
        await self._close()

    async def _shutdown(self) -> None:
        await self._bus.unsubscribe(InvocationContentDelta, self.on_delta)
        await self._bus.unsubscribe(InvocationContentFinished, self.on_finished)
        await self._bus.unsubscribe(InvocationSideQuestionFailed, self.on_failed)
        await self._bus.unsubscribe(InvocationSideQuestionEnded, self.on_ended)
        await self._bus.unsubscribe(SideQuestionsReset, self.on_reset)
        tasks = tuple(self._tasks)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
            self._tasks.difference_update(tasks)
