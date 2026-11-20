# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Folds that take a catalog reminder's carrier out of view re-send the catalog on the same call.

A catalog reminder (skill catalog, MCP instructions) rides only the user
message that first carried it.  ``compress_context`` and Phase 3 folds run
below the reminder middleware, so the strategy asks the middleware to
re-attach whatever the fold removed to the request it is preparing.
"""

from __future__ import annotations

import itertools
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

from chrys.foundation.models.history_markers import HistoryMarkerKind
from chrys.foundation.trajectory.context import trajectory_scope
from chrys.foundation.trajectory.event_types import EventType
from chrys.kernel import (
    EXCLUDED_KEY,
    Content,
    Message,
    annotate_token_counts,
    apply_compaction,
    included_token_count,
    project_included_messages,
)
from chrys.service.agent_middleware.system_reminder import SystemReminderMiddleware
from chrys.service.agent_middleware.system_reminder import wrap_system_reminder as _wrap
from chrys.service.context.providers.history import CompressibleHistoryProvider
from tests.service.context.compaction._compaction_helpers import (
    _assistant_text,
    _estimate_tokens,
    _folded_text_state,
    _make_strategy,
    _markerless_wire,
    _tokenizer,
    _user,
)
from tests.service.trajectory._fakes import FakeSink, make_context
from tests.support.reminder_calls import request_views
from tests.support.reminder_stack import reminder_pair

if TYPE_CHECKING:
    import pytest

    from chrys.service.context.compaction.last_words_state import LastWordsState

_RECORD = HistoryMarkerKind.SYSTEM_REMINDERS_KEY
_CATALOG = "<available_skills>\n  <name>review</name>\n</available_skills>"
_RUNTIME = "[Runtime Environment]\n  Working directory: /work"


def _middleware(monkeypatch: pytest.MonkeyPatch) -> tuple[SystemReminderMiddleware, LastWordsState]:
    """Each turn opens with its own turn line; the runtime environment and skills stay the same."""
    middleware, last_words = reminder_pair(runtime=MagicMock(), skill_catalog_provider=lambda: _CATALOG)
    hints = iter(["turn one", "turn two", "turn three"])
    monkeypatch.setattr(middleware.sources.turn_line, "clock", lambda: next(hints))
    monkeypatch.setattr(middleware.sources.runtime_env, "snapshot", lambda: _RUNTIME)
    return middleware, last_words


def _texts(message: Message) -> list[str]:
    return [content.text for content in message.contents if content.type == "text" and content.text]


def _catalog_count(messages: list[Message]) -> int:
    return sum(_CATALOG in text for message in messages for text in _texts(message))


def _last_user(messages: list[Message]) -> Message:
    return next(message for message in reversed(messages) if message.role == "user")


async def test_compress_context_fold_resends_the_catalog_on_the_same_call(monkeypatch: pytest.MonkeyPatch) -> None:
    middleware, last_words = _middleware(monkeypatch)
    first_user = _user("Turn one request")
    middleware.prepare_turn()
    assert _catalog_count(await request_views(middleware, [first_user])) == 1

    first_answer = _assistant_text("Turn one answer")
    state: dict = {"messages": [first_user, first_answer], "compressed_msgs": [], "turn_counter": 0}
    marker_id = CompressibleHistoryProvider.insert_marker(state, 1)
    current_user = _user("Current turn")
    strategy = _make_strategy(compaction_enabled=False, reminder_middleware=middleware, last_words=last_words)
    strategy.bind_state(state)
    strategy.queue_compression(marker_id, "Turn one summary")
    folded: list[list[Message]] = []

    async def _fold(messages: list[Message]) -> list[Message]:
        # The pipeline's delivery already went out without the catalog.
        assert _catalog_count([_last_user(messages)]) == 0
        folded.append(messages)
        return await apply_compaction(messages, strategy=strategy, tokenizer=_tokenizer)

    middleware.prepare_turn()
    sink = FakeSink()
    with trajectory_scope(make_context(sink)):
        sent = await request_views(middleware, [first_user, first_answer, current_user], prepare=_fold)

    assert [message.additional_properties.get(HistoryMarkerKind.KEY) for message in sent] == [
        HistoryMarkerKind.SUMMARY,
        None,
    ]
    # The runtime environment and the catalog left view with turn one.
    assert _texts(sent[-1]) == ["Current turn", _wrap("turn two"), _wrap(_RUNTIME), _wrap(_CATALOG)]
    assert current_user.additional_properties[_RECORD] == [
        {"kind": "turn", "text": "turn two"},
        {"kind": "catalog", "text": _RUNTIME, "name": "runtime"},
        {"kind": "catalog", "text": _CATALOG, "name": "skills"},
    ]
    assert _texts(current_user) == ["Current turn"]

    [wire] = folded
    counted = included_token_count(wire)
    annotate_token_counts(wire, tokenizer=_tokenizer, force_retokenize=True)
    assert included_token_count(wire) == counted
    # The fold reports the request it left, the re-sent catalog included.
    assert sink.only(EventType.COMPACTION_FINISHED).payload["tokens_after"] == counted
    assert counted > _estimate_tokens([sent[0], _user("Current turn")]) + _tokenizer.count_tokens(_CATALOG)

    # The next turn sees the catalog in view on the current opener and
    # re-renders that opener exactly as the fold call sent it.
    summary = project_included_messages(wire)[0]
    next_user = _user("Next turn")
    middleware.prepare_turn()
    next_wire = await request_views(middleware, [summary, current_user, _assistant_text("Current answer"), next_user])
    assert _texts(next_wire[1]) == _texts(sent[-1])
    assert _texts(next_wire[-1]) == ["Next turn", _wrap("turn three")]
    assert _catalog_count(next_wire) == 1


async def test_phase3_fold_resends_the_catalog_on_the_same_call(monkeypatch: pytest.MonkeyPatch) -> None:
    middleware, last_words = _middleware(monkeypatch)
    state = _folded_text_state(3, fill=2000)
    first_user = state["messages"][0]
    # Turn 1 carried the catalog on an earlier request.
    first_user.additional_properties[_RECORD] = [{"kind": "catalog", "text": _CATALOG, "name": "skills"}]
    current_user = _user("Current turn")
    folded: list[list[Message]] = []

    async def _fold(messages: list[Message]) -> list[Message]:
        # The pipeline's delivery already went out with the catalog on turn 1 only.
        assert _catalog_count(messages) == 1
        assert _catalog_count([_last_user(messages)]) == 0
        strategy = _make_strategy(
            max_context_tokens=_estimate_tokens(messages) + 100,
            trigger_pct=0.85,
            target_pct=0.50,
            reminder_middleware=middleware,
            last_words=last_words,
        )
        strategy.bind_state(state)
        folded.append(messages)
        return await apply_compaction(messages, strategy=strategy, tokenizer=_tokenizer)

    # The turn's first call: its turn line, the runtime environment nothing
    # showed yet and the re-sent catalog go out together.
    middleware.prepare_turn()
    sink = FakeSink()
    with trajectory_scope(make_context(sink)):
        sent = await request_views(middleware, [*_markerless_wire(state), current_user], prepare=_fold)

    assert first_user.additional_properties.get(EXCLUDED_KEY) is True
    assert _catalog_count(sent) == 1
    assert _texts(_last_user(sent)) == ["Current turn", _wrap("turn one"), _wrap(_RUNTIME), _wrap(_CATALOG)]
    assert current_user.additional_properties[_RECORD] == [
        {"kind": "turn", "text": "turn one"},
        {"kind": "catalog", "text": _RUNTIME, "name": "runtime"},
        {"kind": "catalog", "text": _CATALOG, "name": "skills"},
    ]

    [wire] = folded
    counted = included_token_count(wire)
    annotate_token_counts(wire, tokenizer=_tokenizer, force_retokenize=True)
    assert included_token_count(wire) == counted
    # Each fold reports the request it left, the re-sent catalog included from the fold that took its carrier.
    folds = [
        event.payload
        for event in sink.of_type(EventType.COMPACTION_PHASE_FINISHED)
        if event.payload["phase"] == "phase3"
    ]
    assert len(folds) > 1
    for earlier, later in itertools.pairwise(folds):
        assert earlier["tokens_after"] == later["tokens_before"]
    assert folds[-1]["tokens_after"] == sink.only(EventType.COMPACTION_FINISHED).payload["tokens_after"] == counted

    # The next call re-renders the opener byte-identically.
    next_wire = await request_views(
        middleware, [*_markerless_wire(state), current_user, _assistant_text("Answer"), _user("n")]
    )
    opener = next(
        message for message in next_wire if message.additional_properties is current_user.additional_properties
    )
    assert _texts(opener) == _texts(_last_user(sent))


async def test_fold_summary_lands_where_an_enriched_user_only_range_was(monkeypatch: pytest.MonkeyPatch) -> None:
    """An enriched user message shares only its props dict with state; that alone places the summary."""
    middleware, last_words = _middleware(monkeypatch)
    lone_user = Message(role="user", contents=[Content.from_text("Interrupted request")])
    middleware.prepare_turn()
    await request_views(middleware, [lone_user])
    state: dict = {"messages": [lone_user], "compressed_msgs": [], "turn_counter": 0}
    marker_id = CompressibleHistoryProvider.insert_marker(state, 1)
    current_user = _user("Current turn")
    middleware.prepare_turn()
    wire = await request_views(middleware, [lone_user, current_user])
    # Fresh contents: neither contents-list nor content-object identity links it to state.
    shared = {id(content) for content in lone_user.contents}
    assert not any(id(content) in shared for content in wire[0].contents)

    strategy = _make_strategy(compaction_enabled=False, reminder_middleware=middleware, last_words=last_words)
    strategy.bind_state(state)
    strategy.queue_compression(marker_id, "Interrupted turn summary")

    assert await strategy(wire)

    projected = project_included_messages(wire)
    assert [message.additional_properties.get(HistoryMarkerKind.KEY) for message in projected] == [
        HistoryMarkerKind.SUMMARY,
        None,
    ]
    assert projected[-1].additional_properties is current_user.additional_properties

    # The next call of the same run re-enriches the folded opener with fresh
    # contents again; the cached summary must still find its place.
    next_wire = await request_views(middleware, [lone_user, current_user])
    assert await strategy(next_wire) is False
    next_projected = project_included_messages(next_wire)
    assert [message.additional_properties.get(HistoryMarkerKind.KEY) for message in next_projected] == [
        HistoryMarkerKind.SUMMARY,
        None,
    ]


async def test_emergency_fold_finds_an_enriched_user_only_range(monkeypatch: pytest.MonkeyPatch) -> None:
    """Phase 3's eligibility scan places the fold by the shared props dict alone."""
    middleware, last_words = _middleware(monkeypatch)
    lone_user = Message(role="user", contents=[Content.from_text("Interrupted request")])
    middleware.prepare_turn()
    await request_views(middleware, [lone_user])
    state: dict = {"messages": [lone_user], "compressed_msgs": [], "turn_counter": 0}
    CompressibleHistoryProvider.insert_marker(state, 1)
    current_user = _user("Current turn")
    state["messages"].append(current_user)
    middleware.prepare_turn()
    wire = await request_views(middleware, [lone_user, current_user])
    shared = {id(content) for content in lone_user.contents}
    assert not any(id(content) in shared for content in wire[0].contents)

    strategy = _make_strategy(compaction_enabled=False, reminder_middleware=middleware, last_words=last_words)
    strategy.bind_state(state)

    assert await strategy._emergency_compress_oldest_turn(
        wire, usage_pct=0.9, tokens_before=_estimate_tokens(wire)
    ) == (True, True)

    projected = project_included_messages(wire)
    assert [message.additional_properties.get(HistoryMarkerKind.KEY) for message in projected] == [
        HistoryMarkerKind.SUMMARY,
        None,
    ]
    assert projected[-1].additional_properties is current_user.additional_properties
