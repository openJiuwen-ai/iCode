# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""InjectionMiddleware — delivers user messages sent during agent execution.

When a user sends a message while the agent is running (mid-tool-loop),
the text is queued and appended to the messages before the next LLM call.

This is a **ChatMiddleware** (fires per LLM call within the tool loop).
Before each model call, any pending injection texts are appended to
``context.messages`` so the LLM sees them on its next iteration.

Persistence is handled separately by the engine after agent.run() completes,
to ensure correct message ordering in the session history.

One middleware drain is an immutable transaction: all clean wire messages and
retry records are created before the first callback await. A transient retry
replays that transaction outside the cancellable pending queue, preserving its
consumption identities while re-anchoring it to the successful attempt.
"""

from __future__ import annotations

import hashlib
import json
import weakref
from copy import copy
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from chrys.foundation.models.history_markers import HistoryMarkerKind, copy_reminder_record
from chrys.foundation.trajectory.ids import new_analytics_id
from chrys.foundation.trajectory.metadata import ensure_analytics_item_id
from chrys.kernel import Content, Message
from chrys.kernel.identity import ContentList
from chrys.kernel.middleware import ChatMiddleware

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from chrys.kernel.middleware import ChatContext
    from chrys.service.trajectory.preparation import PreparationTrace


def _message_call_ids(msg: Message) -> tuple[str, ...]:
    """Return call ids carried by framework content items."""
    return tuple(c.call_id for c in msg.contents if c.call_id)


def _content_signature(msg: Message) -> tuple[tuple[str, str | None, str | None, str], ...]:
    """Return a stable, compact signature for message contents."""
    parts: list[tuple[str, str | None, str | None, str]] = []
    for content in msg.contents:
        content_type = content.type
        call_id = content.call_id or None
        name = content.name or None
        payload = _content_payload(content, content_type)
        parts.append((content_type, call_id, name, _payload_digest(payload)))
    return tuple(parts)


def _content_payload(content: Content, content_type: str) -> Any:
    if content_type == "function_call":
        return content.arguments
    if content_type == "function_result":
        return content.result
    if content_type == "text":
        return content.text
    return content.to_dict()


def _payload_digest(payload: Any) -> str:
    try:
        data = json.dumps(payload, sort_keys=True, default=str, ensure_ascii=False)
    except TypeError:
        data = str(payload)
    return hashlib.sha256(data.encode("utf-8", errors="surrogatepass")).hexdigest()


@dataclass(frozen=True)
class QueuedInjection:
    """A user injection waiting to be delivered to the next model call."""

    text: str
    created_at: datetime | str | None = None
    injection_id: str | None = None
    reminders: tuple[str, ...] = ()
    """Hook/skill reminders for this injection. They join the turn's reminders
    only when a call drains it, so they ride the very call that sends the text;
    a cancelled or undelivered injection never queues them."""
    consumption_id: str | None = None
    """Stable persistence identity after the first retryable consumption."""
    analytics_item_id: str | None = None
    """Stable trajectory identity shared by wire, retry and persisted copies."""
    preparation: PreparationTrace | None = None
    """Input-admission trace whose terminal is owned by this queued item."""
    target_turn_id: str | None = None
    """Turn captured when ownership moved out of the admission coroutine."""


@dataclass(frozen=True)
class InjectionAnchor:
    """Where a consumed injection should land in persisted state.

    Captures multiple identifying keys because no single one survives every
    framework path:

    - ``contents_ref``: weak reference to the anchor message's ``contents``
      list. Stable in **non-streaming** runs (state stores the same Message
      instances the middleware saw).  Lost in **streaming** runs because
      ``ChatResponse.from_updates`` / ``AgentResponse.from_updates`` rebuild
      every Message from stream updates with brand-new contents lists.

    - ``content_refs``: weak references to the content OBJECTS themselves.
      The kernel wire hands middleware per-call message views — fresh
      wrapper + fresh contents list — so ``contents_ref`` never matches
      state for a viewed message, but the content objects are shared and
      each lives in exactly one state message.

    Identity tiers are WEAK: a dead reference matches nothing (a recycled
    address can never false-match) and an anchor never keeps its source
    alive; placement degrades to the structural fallback, exactly the
    streaming-rebuild path.

    - ``role`` + ``call_ids``: the anchor message's role together with the
      tuple of ``call_id`` values from its ``function_call`` /
      ``function_result`` contents.  Role
      is required because an assistant ``function_call`` and the matching
      tool ``function_result`` share the same call_id; without role the
      injection would land after the assistant call instead of after the
      tool result.  This pair is kept as a fallback because it survives
      streaming rebuilds, but exact identity/message_id are preferred first
      since call ids can be reused by tests, providers, or old replayed
      sessions.

    - ``message_id``: framework-assigned ``Message.message_id`` when set
      (typically on assistant LLM responses).
    """

    role: str | None = None
    contents_ref: weakref.ref[ContentList] | None = None
    call_ids: tuple[str, ...] = ()
    content_signature: tuple[tuple[str, str | None, str | None, str], ...] = ()
    message_id: str | None = None
    content_refs: tuple[weakref.ref[Content], ...] = ()

    @classmethod
    def from_message(cls, msg: Message | None) -> InjectionAnchor:
        """Build an anchor from the live message the injection lands after."""
        if msg is None:
            return cls()
        return cls(
            role=msg.role,
            contents_ref=weakref.ref(msg.contents),
            call_ids=_message_call_ids(msg),
            content_signature=_content_signature(msg),
            message_id=msg.message_id or None,
            content_refs=tuple(weakref.ref(c) for c in msg.contents),
        )

    def find_in(self, messages: list[Message]) -> int | None:
        """Return the index of the first matching message, or ``None``.

        Tries each captured key in turn so a single missing key does not
        block placement:

        1. ``contents_ref`` — exact identity match (non-streaming runs),
           accepted only when captured structural keys also agree.
        2. ``content_refs`` — exact identity of the shared content objects,
           for anchors captured from per-call wire views whose fresh
           contents lists defeat ``contents_ref``.
        3. ``message_id`` — the assistant response's Message identifier,
           accepted only when captured structural keys also agree.
        4. ``role`` + content signature / ``call_ids`` — survives the
           streaming rebuild and disambiguates assistant function_call from
           tool function_result (both carry the same call_ids).  Search from
           the end so a duplicate call_id in an earlier turn cannot steal
           the anchor.
        """
        snapshot = self.contents_ref() if self.contents_ref is not None else None
        if snapshot is not None:
            for i, m in enumerate(messages):
                if m.contents is snapshot and self._matches_shape(m):
                    return i
        # The wire hands clients per-call views: fresh wrapper + fresh
        # contents LIST, but the content OBJECTS are shared with the state
        # original — matching on their identity keeps identity-level
        # precision for anchors snapshotted from a view. A dead ref matches
        # nothing: ``ref() is c`` cannot hold for a live content.
        if self.content_refs:
            for i, m in enumerate(messages):
                if (
                    len(m.contents) == len(self.content_refs)
                    and all(ref() is c for ref, c in zip(self.content_refs, m.contents, strict=True))
                    and self._matches_shape(m)
                ):
                    return i
        if self.message_id:
            for i, m in enumerate(messages):
                if m.message_id == self.message_id and self._matches_shape(m):
                    return i
        if self.content_signature or self.call_ids:
            for i in range(len(messages) - 1, -1, -1):
                m = messages[i]
                if self._matches_shape(m):
                    return i
        return None

    def _matches_shape(self, msg: Message) -> bool:
        """Return whether *msg* matches the stable structural anchor keys."""
        if self.role is not None and msg.role != self.role:
            return False
        if self.call_ids and _message_call_ids(msg) != self.call_ids:
            return False
        return not self.content_signature or _content_signature(msg) == self.content_signature


@dataclass(frozen=True)
class ConsumedInjection:
    """A delivered injection and the persisted-history anchor it should follow.

    Two identities, deliberately distinct:

    * ``injection_id`` — the frontend's queue handle, round-tripped
      UNCHANGED into ``UserInjectResult`` (``None`` for frontends without
      pending tracking, e.g. the ACP path; the TUI keys input-ownership on
      exactly this value, so a synthesized id here would break its
      id-less-legacy branch).
    * ``consumption_id`` — the per-consumption persistence identity, always
      populated by :meth:`InjectionMiddleware.process` (the queue handle
      when present, synthesized otherwise).  Crash-recovery replay dedups
      on it; ``None`` only for hand-built legacy values, which fall back to
      text matching.
    """

    text: str
    anchor: InjectionAnchor
    created_at: datetime | str | None = None
    injection_id: str | None = None
    consumption_id: str | None = None
    analytics_item_id: str | None = None
    preparation: PreparationTrace | None = None
    target_turn_id: str | None = None
    wire_properties: dict[str, Any] | None = field(default=None, compare=False, repr=False)
    """The consumed wire message's live ``additional_properties``.

    The reminder middleware records the reminders the message carried there
    once a request is established — after consumption — and persistence
    copies that record onto the history copy it builds.
    """


@dataclass(frozen=True)
class _InjectionBatch:
    """An ordered retryable injection transaction and its history anchor."""

    injections: tuple[QueuedInjection, ...]
    anchor: InjectionAnchor
    wire_properties: tuple[dict[str, Any], ...] = ()
    """The live ``additional_properties`` of the wire messages this batch was
    last consumed as, one per injection (empty before the first consumption).

    A retry replays each injection as a fresh message; it takes over the
    reminder record the reminder middleware wrote there once that request
    was established, so the replay re-sends the same bytes.
    """


class InjectionMiddleware(ChatMiddleware):
    """Delivers queued user messages before each LLM call in the tool loop.

    Before each model call (``call_next()``), pending texts are appended
    as ``Message("user", [text])`` to ``context.messages``.  The LLM
    sees these messages on the current iteration.

    The on-consumed callback receives a :class:`ConsumedInjection` with
    the injected text, timestamp metadata, and an :class:`InjectionAnchor`
    describing the message the injection should land *after* in session
    history.  The engine uses this to position the injection
    deterministically across both streaming and non-streaming framework
    paths.
    """

    def __init__(self) -> None:
        self._pending: list[QueuedInjection] = []
        self._retry_replay: list[_InjectionBatch] = []
        self._retry_consumed: list[_InjectionBatch] = []
        self._retry_active = False
        self._consumed_injection_messages: list[Message] = []
        self._on_consumed: Callable[[ConsumedInjection], Awaitable[None]] | None = None
        self._on_consumed_batch: Callable[[tuple[ConsumedInjection, ...]], Awaitable[None]] | None = None
        self._on_drained_reminders: Callable[[list[str]], None] | None = None

    def set_on_drained_reminders(self, callback: Callable[[list[str]], None]) -> None:
        """Set the synchronous sink for the reminders of freshly drained injections."""
        self._on_drained_reminders = callback

    def set_on_consumed_batch(
        self,
        callback: Callable[[tuple[ConsumedInjection, ...]], Awaitable[None]],
    ) -> None:
        """Set one callback for each complete consumed wire transaction."""
        self._on_consumed_batch = callback
        self._on_consumed = None

    def set_on_consumed(self, callback: Callable[[ConsumedInjection], Awaitable[None]]) -> None:
        """Set a callback invoked for each message successfully injected.

        Args:
            callback: ``async (ConsumedInjection) -> None``.
        """
        self._on_consumed = callback
        self._on_consumed_batch = None

    def queue(
        self,
        text: str,
        created_at: datetime | str | None = None,
        injection_id: str | None = None,
        reminders: tuple[str, ...] = (),
        preparation: PreparationTrace | None = None,
        target_turn_id: str | None = None,
    ) -> QueuedInjection:
        """Queue text for injection before the next model call; the queued item is its handle."""
        injection = QueuedInjection(
            text=text,
            created_at=created_at,
            injection_id=injection_id,
            reminders=reminders,
            analytics_item_id=new_analytics_id(),
            preparation=preparation,
            target_turn_id=target_turn_id,
        )
        self._pending.append(injection)
        return injection

    def is_pending(self, injection: QueuedInjection) -> bool:
        """Whether *injection* still waits for a model call: no call drained it, and it was not cancelled or returned."""
        return any(queued is injection for queued in self._pending)

    def drain_consumed_injection_messages(self) -> list[Message]:
        """Return clean injections consumed by the latest provider call."""
        messages = self._consumed_injection_messages
        self._consumed_injection_messages = []
        return messages

    def begin_retry(self) -> None:
        """Track consumed injections for one retryable outer ``Agent.run``."""
        if self._retry_active:
            raise RuntimeError("An injection retry is already active.")
        self._retry_active = True
        self._retry_replay = []
        self._retry_consumed = []
        self._consumed_injection_messages = []

    def restore_for_retry(self) -> None:
        """Reserve the failed attempt's immutable batches for replay."""
        if not self._retry_active:
            return
        # A retry may fail in a context provider before chat middleware runs.
        # Preserve the existing replay unless this attempt actually consumed
        # a replacement batch.
        if self._retry_consumed:
            self._retry_replay = self._retry_consumed.copy()
        self._retry_consumed = []
        self._consumed_injection_messages = []

    def commit_logical_call(self) -> None:
        """Forget retry batches consumed by a completed logical wire call."""
        if not self._retry_active:
            return
        self._retry_replay = []
        self._retry_consumed = []

    def end_retry(self) -> None:
        """Release retry-only state after the outer run terminates."""
        self._retry_active = False
        self._retry_replay = []
        self._retry_consumed = []
        self._consumed_injection_messages = []

    def cancel(self, injection_id: str) -> QueuedInjection | None:
        """Remove and return the pending injection with *injection_id*, if any.

        Other pending injections keep their relative order. Returns ``None``
        when the id is unknown or the injection was already consumed.
        """
        for i, injection in enumerate(self._pending):
            if injection.injection_id == injection_id:
                return self._pending.pop(i)
        return None

    @property
    def has_pending(self) -> bool:
        """Whether queued text waits for the next model call."""
        return bool(self._pending)

    def drain_pending(self) -> list[QueuedInjection]:
        """Return and clear any un-consumed pending injections."""
        drained = self._pending.copy()
        self._pending.clear()
        return drained

    async def process(
        self,
        context: ChatContext,
        call_next: Callable[[], Awaitable[None]],
    ) -> None:
        request_options = context.options
        if request_options and request_options.get("continuation_token") is not None:
            # A continuation poll retrieves an already-created response — the
            # provider ignores request messages, so consuming an injection
            # here would persist text the model never saw. Hold pending and
            # replayed batches for the next request that creates a response.
            await call_next()
            return
        # Before the LLM call, inject queued and retry-reserved transactions.
        current_anchor = InjectionAnchor.from_message(context.messages[-1] if context.messages else None)
        batches = [replace(batch, anchor=current_anchor) if context.messages else batch for batch in self._retry_replay]
        if self._pending:
            injections = tuple(
                replace(
                    injection,
                    consumption_id=injection.consumption_id or injection.injection_id or uuid4().hex[:12],
                    analytics_item_id=injection.analytics_item_id or new_analytics_id(),
                )
                for injection in self._pending
            )
            batches.append(_InjectionBatch(injections, current_anchor))
            # Queue before any await: an injection committed while this call
            # awaits its checkpoint is not in this batch, so its reminders
            # must not ride this call either. A replayed batch queued its
            # reminders when it was first drained.
            drained_reminders = [reminder for injection in injections for reminder in injection.reminders]
            if drained_reminders and self._on_drained_reminders is not None:
                self._on_drained_reminders(drained_reminders)

        self._retry_replay = []
        self._pending.clear()
        if batches:
            mutable: list[Any] = list(context.messages)
            consumed_transaction: list[ConsumedInjection] = []
            for batch in batches:
                batch_properties: list[dict[str, Any]] = []
                stable_injections = tuple(
                    replace(
                        injection,
                        consumption_id=injection.consumption_id or injection.injection_id or uuid4().hex[:12],
                        analytics_item_id=injection.analytics_item_id or new_analytics_id(),
                    )
                    for injection in batch.injections
                )
                for index, injection in enumerate(stable_injections):
                    # Every consumption gets a per-consumption identity — it
                    # travels on the wire copy, the ConsumedInjection mirror, and
                    # the persisted history copy, so crash-recovery replay dedups
                    # by identity instead of text (same-text distinct injections
                    # must both survive).  Queued ids (UI cancel handles) are
                    # reused; id-less queues get one synthesized here.  The
                    # ORIGINAL queue handle is forwarded separately as
                    # ``injection_id`` — event consumers key on it and must see
                    # ``None`` for id-less frontends, never a synthesized value.
                    consumption_id = injection.consumption_id
                    if consumption_id is None:
                        raise RuntimeError("Queued injection is missing its consumption identity.")
                    # Flag the wire copy at creation time so the very call that
                    # consumes the injection classifies it as mid-turn (the
                    # persisted copy is stamped independently by
                    # ``persist_consumed_injections``).
                    msg = Message("user", [injection.text])
                    msg.additional_properties[HistoryMarkerKind.INJECTED_KEY] = True
                    msg.additional_properties[HistoryMarkerKind.INJECTION_ID_KEY] = consumption_id
                    ensure_analytics_item_id(
                        msg.additional_properties,
                        item_id=injection.analytics_item_id,
                    )
                    if index < len(batch.wire_properties):
                        copy_reminder_record(batch.wire_properties[index], msg.additional_properties)
                    batch_properties.append(msg.additional_properties)
                    mutable.append(msg)
                    # Retain a structural copy, never the wire object: the
                    # client receives ``msg`` on this very call and may mutate
                    # its contents list in place, and the retained copy is what
                    # later tool iterations re-send and persistence captures.
                    # The props dict stays shared (the sanctioned metadata
                    # write-through channel) and the content object stays
                    # shared like everywhere else.
                    retained = copy(msg)
                    retained.contents = list(msg.contents)
                    self._consumed_injection_messages.append(retained)
                    consumed_transaction.append(
                        ConsumedInjection(
                            text=injection.text,
                            anchor=batch.anchor,
                            created_at=injection.created_at,
                            injection_id=injection.injection_id,
                            consumption_id=consumption_id,
                            analytics_item_id=injection.analytics_item_id,
                            preparation=injection.preparation,
                            target_turn_id=injection.target_turn_id,
                            wire_properties=msg.additional_properties,
                        )
                    )
                # Register the complete batch before the first callback await.
                # Cancellation can therefore replay all messages or none; it
                # cannot strand the unvisited tail of a multi-message drain.
                if self._retry_active:
                    self._retry_consumed.append(
                        _InjectionBatch(stable_injections, batch.anchor, tuple(batch_properties))
                    )
            context.messages = mutable
            if self._on_consumed_batch is not None:
                await self._on_consumed_batch(tuple(consumed_transaction))
            elif self._on_consumed is not None:
                for consumed in consumed_transaction:
                    await self._on_consumed(consumed)

        await call_next()
