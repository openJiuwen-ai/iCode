# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Direct tests of the reminder record — what a user message carried is re-rendered on every later call.

``SystemReminderMiddleware`` records the reminders a user message carried on
an established request (``HistoryMarkerKind.SYSTEM_REMINDERS_KEY``) and
renders them again, byte-identically, on every later call — later turns
included — so the conversation prefix stays stable for provider KV caches.
These tests drive the middleware directly; the engine-driven counterparts
live in ``test_system_reminder_integration.py``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import MagicMock

import pytest

from chrys.foundation.models.history_markers import HistoryMarkerKind, read_last_words_record
from chrys.kernel import EXCLUDED_KEY, ChatResponse, ChatResponseUpdate, Content, Message, ResponseStream
from chrys.kernel.middleware import ChatContext
from chrys.service.agent_middleware.injection import InjectionMiddleware
from chrys.service.agent_middleware.reminders.archive_pointer import _catalog_pointer_text
from chrys.service.agent_middleware.reminders.background_tasks import BackgroundTasksSource
from chrys.service.agent_middleware.reminders.context_usage import CONTEXT_USAGE_WARNING, format_usage_line
from chrys.service.agent_middleware.reminders.skills import SkillsSource
from chrys.service.agent_middleware.reminders.sub_agents import SubAgentsSource
from chrys.service.agent_middleware.reminders.todo import TodoSource
from chrys.service.agent_middleware.system_reminder import SystemReminderMiddleware
from chrys.service.agent_middleware.system_reminder import (
    wrap_system_reminder as _wrap,
)
from chrys.service.llm.anthropic_messages.history import encode_messages
from chrys.service.llm.chat_completions import ChatCompletionsClient
from chrys.service.llm.chat_completions import history as chat_history
from chrys.service.llm.openai_responses.client import OPENAI_RESPONSES
from chrys.service.llm.openai_responses.replay import encode_input
from chrys.service.profiles.models.schema import DEFAULT_MAX_CONTEXT_TOKENS
from tests.support.reminder_calls import enrich_call, establish_request
from tests.support.reminder_stack import reminder_pair

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Sequence

    from chrys.service.agent_middleware.injection import ConsumedInjection
    from chrys.service.context.compaction.last_words_state import LastWordsState

_RECORD = HistoryMarkerKind.SYSTEM_REMINDERS_KEY
_CATALOG = "<available_skills>\n  <name>review</name>\n</available_skills>"
_LAST_WORDS_PREFIX = "<system-reminder>\n[LAST_WORDS] "


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _user(text: str, **properties: Any) -> Message:
    message = Message(role="user", contents=[Content.from_text(text)])
    message.additional_properties.update(properties)
    return message


def _injected(text: str) -> Message:
    return _user(text, **{HistoryMarkerKind.INJECTED_KEY: True})


def _assistant(text: str) -> Message:
    return Message(role="assistant", contents=[Content.from_text(text)])


def _middleware(
    monkeypatch: pytest.MonkeyPatch,
    *,
    hints: Sequence[str] = ("turn one", "turn two", "turn three"),
    catalog: list[str | None] | None = None,
    runtime: list[str | None] | None = None,
    todo: list[str | None] | None = None,
    sub_agent_names: list[str] | None = None,
    background_tasks: list[str | None] | None = None,
) -> SystemReminderMiddleware:
    """A middleware whose turn line is one of *hints* per ``prepare_turn``.

    *catalog* (skills), *runtime*, *todo* and *background_tasks* are read live,
    so a test changes one by assigning its ``[0]``; without them the turn offers none.
    """
    return _middleware_pair(
        monkeypatch,
        hints=hints,
        catalog=catalog,
        runtime=runtime,
        todo=todo,
        sub_agent_names=sub_agent_names,
        background_tasks=background_tasks,
    )[0]


def _middleware_pair(
    monkeypatch: pytest.MonkeyPatch,
    *,
    hints: Sequence[str] = ("turn one", "turn two", "turn three"),
    catalog: list[str | None] | None = None,
    runtime: list[str | None] | None = None,
    todo: list[str | None] | None = None,
    sub_agent_names: list[str] | None = None,
    background_tasks: list[str | None] | None = None,
) -> tuple[SystemReminderMiddleware, LastWordsState]:
    """``_middleware`` plus the LAST_WORDS state it renders."""
    middleware, last_words = reminder_pair(
        runtime=MagicMock(),
        sub_agent_names=sub_agent_names,
        skill_catalog_provider=(lambda: catalog[0]) if catalog is not None else None,
        todo_state_provider=(lambda: todo[0]) if todo is not None else None,
        background_tasks_provider=(lambda: background_tasks[0]) if background_tasks is not None else None,
    )
    remaining = iter(hints)
    monkeypatch.setattr(middleware.sources.turn_line, "clock", lambda: next(remaining))
    monkeypatch.setattr(
        middleware.sources.runtime_env, "snapshot", lambda: (runtime[0] if runtime is not None else None) or None
    )
    return middleware, last_words


def _texts(message: Message) -> list[str]:
    return [content.text for content in message.contents if content.type == "text" and content.text]


def _all_texts(messages: Sequence[Message]) -> list[str]:
    return [text for message in messages for text in _texts(message)]


def _count(messages: Sequence[Message], needle: str) -> int:
    return sum(needle in text for text in _all_texts(messages))


def _record(message: Message) -> list[dict[str, str]]:
    return message.additional_properties[_RECORD]


# ---------------------------------------------------------------------------
# Recording
# ---------------------------------------------------------------------------


async def test_record_is_written_only_once_a_request_carries_it(monkeypatch: pytest.MonkeyPatch) -> None:
    middleware = _middleware(monkeypatch)
    opener = _user("first")
    middleware.prepare_turn()

    unsent = await enrich_call(middleware, [opener], requests=0)

    assert _RECORD not in opener.additional_properties
    assert _texts(unsent[0]) == ["first", _wrap("turn one")]

    sent = await enrich_call(middleware, [opener])

    assert _record(opener) == [{"kind": "turn", "text": "turn one"}]
    assert _texts(sent[0]) == ["first", _wrap("turn one")]
    assert _texts(opener) == ["first"]


async def test_repeated_observer_runs_record_once(monkeypatch: pytest.MonkeyPatch) -> None:
    middleware = _middleware(monkeypatch)
    opener = _user("first")
    middleware.prepare_turn()
    middleware.queue_hook_reminders(["hook note"])

    await enrich_call(middleware, [opener], requests=3)

    assert _record(opener) == [
        {"kind": "turn", "text": "turn one"},
        {"kind": "event", "text": "hook note"},
    ]


async def test_record_write_assigns_a_new_list(monkeypatch: pytest.MonkeyPatch) -> None:
    """Retry rollback restores props from a one-level copy that shares list values."""
    middleware = _middleware(monkeypatch)
    opener = _user("first")
    middleware.prepare_turn()
    await enrich_call(middleware, [opener])
    first_record = _record(opener)
    first_snapshot = [dict(entry) for entry in first_record]

    middleware.queue_hook_reminders(["late hook"])
    await enrich_call(middleware, [opener])

    assert _record(opener) is not first_record
    assert first_record == first_snapshot
    assert _record(opener)[-1] == {"kind": "event", "text": "late hook"}


@pytest.mark.parametrize("requests", [1, 0], ids=["established", "never-sent"])
async def test_last_words_has_a_record_of_its_own_written_once_its_request_is_established(
    monkeypatch: pytest.MonkeyPatch, requests: int
) -> None:
    """A note no compaction commit recorded (one restored from an older session) is recorded as it is sent."""
    middleware, last_words = _middleware_pair(monkeypatch)
    opener = _user("first")
    middleware.prepare_turn()
    last_words.set_last_words("progress note")

    wire = await enrich_call(middleware, [opener], requests=requests)

    assert _texts(wire[0])[-1].startswith(_LAST_WORDS_PREFIX)
    # The note replaces itself during a turn, so it is no entry of the append-only reminder record.
    assert all("[LAST_WORDS]" not in entry["text"] for entry in opener.additional_properties.get(_RECORD, []))
    record = read_last_words_record(opener.additional_properties)
    if requests:
        assert record is not None
        assert record.note == "progress note"
        assert _wrap(record.text) == _texts(wire[0])[-1]
    else:
        assert record is None


# ---------------------------------------------------------------------------
# Re-rendering across calls and turns
# ---------------------------------------------------------------------------


async def test_later_turn_renders_the_earlier_opener_byte_identically(monkeypatch: pytest.MonkeyPatch) -> None:
    middleware = _middleware(monkeypatch, catalog=[_CATALOG])
    opener = _user("first")
    middleware.prepare_turn()
    first = await enrich_call(middleware, [opener])

    next_opener = _user("second")
    middleware.prepare_turn()
    second = await enrich_call(middleware, [opener, _assistant("done"), next_opener])

    assert _texts(second[0]) == _texts(first[0])
    assert _texts(first[0]) == ["first", _wrap("turn one"), _wrap(_CATALOG)]
    # The unchanged catalog is still in view on the first opener.
    assert _texts(second[2]) == ["second", _wrap("turn two")]
    assert _texts(opener) == ["first"]
    assert _texts(next_opener) == ["second"]


@pytest.mark.parametrize("preserve", [True, False], ids=["preserve", "fresh-snapshot"])
async def test_retry_of_the_same_turn_does_not_repeat_the_turn_group(
    monkeypatch: pytest.MonkeyPatch,
    preserve: bool,
) -> None:
    middleware = _middleware(monkeypatch)
    opener = _user("first")
    middleware.prepare_turn()
    first = await enrich_call(middleware, [opener])

    middleware.prepare_turn(preserve_turn_reminders=preserve)
    injected = _injected("also this")
    retry = await enrich_call(middleware, [opener, _assistant("partial"), injected])

    assert _texts(retry[0]) == _texts(first[0])
    assert _texts(retry[2]) == ["also this"]
    assert _count(retry, "turn two") == 0
    assert _RECORD not in injected.additional_properties


async def test_event_is_sent_once_per_turn_and_again_in_a_new_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    middleware = _middleware(monkeypatch)
    opener = _user("first")
    middleware.prepare_turn()
    middleware.queue_hook_reminders(["hook note"])
    await enrich_call(middleware, [opener])

    # Same turn: the same text queued again for an injection is not repeated,
    # a new text lands on the injected message and is recorded there.
    middleware.queue_hook_reminders(["hook note", "inject hook"])
    injected = _injected("more")
    same_turn = await enrich_call(middleware, [opener, _assistant("working"), injected])

    assert _count(same_turn, "hook note") == 1
    assert _texts(same_turn[2]) == ["more", _wrap("inject hook")]
    assert _record(injected) == [{"kind": "event", "text": "inject hook"}]

    # A later call of the turn re-renders the injected message as sent.
    later = await enrich_call(middleware, [opener, _assistant("working"), injected, _assistant("still working")])
    assert _texts(later[2]) == _texts(same_turn[2])

    middleware.prepare_turn()
    middleware.queue_hook_reminders(["hook note"])
    next_opener = _user("next")
    new_turn = await enrich_call(
        middleware,
        [opener, _assistant("working"), injected, _assistant("done"), next_opener],
    )

    assert _texts(new_turn[-1]) == ["next", _wrap("turn two"), _wrap("hook note")]
    assert _count(new_turn, "hook note") == 2


async def test_identical_events_queued_together_go_once(monkeypatch: pytest.MonkeyPatch) -> None:
    middleware = _middleware(monkeypatch)
    opener = _user("first")
    middleware.prepare_turn()
    # Two injections whose hooks return the same text, drained on one call.
    middleware.queue_hook_reminders(["hook note"])
    middleware.queue_hook_reminders(["hook note"])

    sent = await enrich_call(middleware, [opener])

    assert _texts(sent[0]) == ["first", _wrap("turn one"), _wrap("hook note")]
    assert _record(opener) == [{"kind": "turn", "text": "turn one"}, {"kind": "event", "text": "hook note"}]


async def test_catalog_is_resent_only_when_it_changes(monkeypatch: pytest.MonkeyPatch) -> None:
    catalog = [_CATALOG]
    middleware = _middleware(monkeypatch, catalog=catalog)
    opener = _user("first")
    middleware.prepare_turn()
    await enrich_call(middleware, [opener])

    second_opener = _user("second")
    middleware.prepare_turn()
    unchanged = await enrich_call(middleware, [opener, _assistant("a"), second_opener])
    assert _count(unchanged, _CATALOG) == 1
    assert _count([unchanged[2]], _CATALOG) == 0

    changed_catalog = "<available_skills>\n  <name>deploy</name>\n</available_skills>"
    catalog[0] = changed_catalog
    third_opener = _user("third")
    middleware.prepare_turn()
    changed = await enrich_call(
        middleware,
        [opener, _assistant("a"), second_opener, _assistant("b"), third_opener],
    )

    assert _texts(changed[0]) == _texts(unchanged[0])
    assert _texts(changed[-1]) == ["third", _wrap("turn three"), _wrap(changed_catalog)]


async def test_catalog_that_changes_back_is_resent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only the latest version in view counts: A → B → A sends A again."""
    catalog = [_CATALOG]
    middleware = _middleware(monkeypatch, catalog=catalog)
    opener = _user("first")
    middleware.prepare_turn()
    await enrich_call(middleware, [opener])

    changed_catalog = "<available_skills>\n  <name>deploy</name>\n</available_skills>"
    catalog[0] = changed_catalog
    second_opener = _user("second")
    middleware.prepare_turn()
    await enrich_call(middleware, [opener, _assistant("a"), second_opener])

    catalog[0] = _CATALOG
    third_opener = _user("third")
    middleware.prepare_turn()
    reverted = await enrich_call(
        middleware,
        [opener, _assistant("a"), second_opener, _assistant("b"), third_opener],
    )

    assert _texts(reverted[-1]) == ["third", _wrap("turn three"), _wrap(_CATALOG)]


async def test_catalog_no_longer_offered_is_withdrawn(monkeypatch: pytest.MonkeyPatch) -> None:
    """Its last version stays in view, so the model is told once that it no longer applies."""
    catalog: list[str | None] = [_CATALOG]
    middleware = _middleware(monkeypatch, catalog=catalog)
    opener = _user("first")
    middleware.prepare_turn()
    await enrich_call(middleware, [opener])

    catalog[0] = None
    second = _user("second")
    history = [opener, _assistant("a"), second]
    middleware.prepare_turn()
    wire = await enrich_call(middleware, history)

    withdrawn = SkillsSource.withdrawn
    assert _texts(wire[-1]) == ["second", _wrap("turn two"), _wrap(withdrawn)]
    assert _record(second)[-1] == {"kind": "catalog", "text": withdrawn, "name": "skills"}
    later = await enrich_call(middleware, [*history, _assistant("b"), _injected("more")])
    assert _count(later, withdrawn) == 1

    catalog[0] = _CATALOG
    third = _user("third")
    middleware.prepare_turn()
    offered_again = await enrich_call(middleware, [*history, _assistant("b"), third])
    assert _texts(offered_again[-1]) == ["third", _wrap("turn three"), _wrap(_CATALOG)]


async def test_continuation_poll_adds_records_and_consumes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """A poll's messages are ignored by the provider, so nothing counts as delivered."""
    middleware = _middleware(monkeypatch)
    middleware.sources.profile_switch.set_profile_switch("Code", "QA")
    opener = _user("first")
    middleware.prepare_turn()

    polled = await enrich_call(middleware, [opener], options={"continuation_token": "resp_1"})

    assert polled[0] is opener
    assert _RECORD not in opener.additional_properties
    assert middleware.sources.profile_switch.has_pending_switch is True

    sent = await enrich_call(middleware, [opener])
    assert _count(sent, "turn one") == 1
    assert _count(sent, "[Agent profile switched from 'Code' to 'QA']") == 1
    assert middleware.sources.profile_switch.has_pending_switch is False


async def test_profile_switch_before_a_retry_is_sent_and_then_consumed(monkeypatch: pytest.MonkeyPatch) -> None:
    """The turn group is once per turn; a switch made before a retry still goes out."""
    middleware = _middleware(monkeypatch)
    opener = _user("first")
    middleware.prepare_turn()
    first = await enrich_call(middleware, [opener])

    # The failed turn's agent is rebuilt for the new profile, pending switch included.
    rebuilt = _middleware(monkeypatch, hints=("turn again",))
    rebuilt.sources.profile_switch.set_profile_switch("Code", "QA")
    rebuilt.prepare_turn(preserve_turn_reminders=True)
    retry = await enrich_call(rebuilt, [opener])

    notice = "[Agent profile switched from 'Code' to 'QA']"
    assert _texts(retry[0])[: len(_texts(first[0]))] == _texts(first[0])
    assert _count(retry, notice) == 1
    assert _count(retry, "turn again") == 0
    assert rebuilt.sources.profile_switch.consumed_switch_to == "QA"
    assert rebuilt.sources.profile_switch.has_pending_switch is False

    # Later calls of the turn render it from the record, once.
    later = await enrich_call(rebuilt, [opener, _assistant("working")])
    assert _texts(later[0]) == _texts(retry[0])


async def test_switching_back_and_forth_across_retries_sends_every_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    """A switch compares with the turn's latest switch, not every earlier one."""
    middleware = _middleware(monkeypatch)
    opener = _user("first")
    middleware.prepare_turn()
    await enrich_call(middleware, [opener])

    to_qa = "[Agent profile switched from 'Code' to 'QA']"
    to_code = "[Agent profile switched from 'QA' to 'Code']"
    for source, target in (("Code", "QA"), ("QA", "Code"), ("Code", "QA")):
        rebuilt = _middleware(monkeypatch)
        rebuilt.sources.profile_switch.set_profile_switch(source, target)
        rebuilt.prepare_turn(preserve_turn_reminders=True)
        wire = await enrich_call(rebuilt, [opener])
        assert rebuilt.sources.profile_switch.consumed_switch_to == target
        assert rebuilt.sources.profile_switch.has_pending_switch is False

    notices = [text.splitlines()[1] for text in _texts(wire[0]) if "Agent profile switched" in text]
    assert notices == [to_qa, to_code, to_qa]
    recorded = [entry["text"].splitlines()[0] for entry in _record(opener) if entry["kind"] == "switch"]
    assert recorded == [to_qa, to_code, to_qa]


@pytest.mark.parametrize("out_of_view", ["excluded", "absent"])
async def test_catalog_is_resent_when_its_carrier_leaves_view(
    monkeypatch: pytest.MonkeyPatch,
    out_of_view: str,
) -> None:
    middleware = _middleware(monkeypatch, catalog=[_CATALOG])
    opener = _user("first")
    middleware.prepare_turn()
    await enrich_call(middleware, [opener])

    next_opener = _user("second")
    if out_of_view == "excluded":
        opener.additional_properties[EXCLUDED_KEY] = True
        history = [opener, _assistant("summary"), next_opener]
    else:
        history = [_assistant("summary"), next_opener]
    middleware.prepare_turn()
    wire = await enrich_call(middleware, history)

    assert _texts(wire[-1]) == ["second", _wrap("turn two"), _wrap(_CATALOG)]
    assert _record(next_opener)[-1] == {"kind": "catalog", "text": _CATALOG, "name": "skills"}


async def test_malformed_record_entries_are_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    middleware = _middleware(monkeypatch)
    malformed = _user(
        "old",
        **{
            _RECORD: [
                {"kind": "bogus", "text": "unknown kind"},
                {"kind": ["turn"], "text": "unhashable kind"},
                {"kind": {"turn": 1}, "text": "object kind"},
                "loose string",
                {"kind": "turn", "text": ""},
                {"kind": "event", "text": 5},
                {"kind": "event", "text": "kept"},
            ]
        },
    )
    not_a_list = _user("older", **{_RECORD: "garbage"})
    latest = _user("new")
    middleware.prepare_turn()

    wire = await enrich_call(middleware, [not_a_list, _assistant("a"), malformed, _assistant("b"), latest])

    assert wire[0] is not_a_list
    assert _texts(wire[2]) == ["old", _wrap("kept")]
    assert _texts(wire[4]) == ["new", _wrap("turn one")]


async def test_enriched_copy_keeps_identity_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    middleware = _middleware(monkeypatch)
    opener = Message(role="user", contents=[Content.from_text("first")], author_name="alice", message_id="msg_7")
    middleware.prepare_turn()

    wire = await enrich_call(middleware, [opener])

    assert wire[0] is not opener
    assert wire[0].message_id == "msg_7"
    assert wire[0].author_name == "alice"
    assert wire[0].additional_properties is opener.additional_properties
    assert _texts(opener) == ["first"]


async def test_recorded_catalog_pointer_names_this_sessions_catalog(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A session copied elsewhere (fork, a new session root) renders the pointer at its own catalog."""
    session_root = tmp_path / "sessions" / "moved"
    middleware = SystemReminderMiddleware(runtime=MagicMock(), session_root=session_root)
    monkeypatch.setattr(middleware.sources.runtime_env, "snapshot", lambda: "runtime")
    old_catalog = "/old/root/sessions/abc/compactions/dropped/catalog.jsonl"
    pointer = (
        "Earlier context compaction archived 2 records from previous turns; "
        f"catalog: {old_catalog} (contains each record's relative path)."
    )
    event = f"hook note; catalog: {old_catalog} (contains each record's relative path)."
    opener = _user("first", **{_RECORD: [{"kind": "turn", "text": pointer}, {"kind": "event", "text": event}]})
    middleware.prepare_turn()

    sent = await enrich_call(middleware, [opener, _assistant("a"), _user("second")])

    current = (session_root.resolve() / "compactions" / "dropped" / "catalog.jsonl").as_posix()
    assert _texts(sent[0]) == ["first", _wrap(pointer.replace(old_catalog, current)), _wrap(event)]
    assert _record(opener)[0]["text"] == pointer


@pytest.mark.parametrize(
    "text",
    [
        "Todo:\n- grep old records; catalog: see pointer above\n- keep (contains each record's relative path).",
        (
            "Note. Earlier context compaction archived 2 records from previous turns; "
            "catalog: /old/catalog.jsonl (contains each record's relative path)."
        ),
        (
            "Earlier context compaction archived 0 records from previous turns; "
            "catalog: /old/catalog.jsonl (contains each record's relative path)."
        ),
    ],
    ids=["todo-quoting-parts", "sentence-before-pointer", "zero-records"],
)
async def test_turn_reminder_shaped_like_part_of_the_pointer_renders_as_recorded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, text: str
) -> None:
    middleware = SystemReminderMiddleware(runtime=MagicMock(), session_root=tmp_path / "sessions" / "moved")
    monkeypatch.setattr(middleware.sources.runtime_env, "snapshot", lambda: "runtime")
    opener = _user("first", **{_RECORD: [{"kind": "turn", "text": text}]})
    middleware.prepare_turn()

    sent = await enrich_call(middleware, [opener, _assistant("a"), _user("second")])

    assert _texts(sent[0]) == ["first", _wrap(text)]


async def test_recorded_pointers_resolve_the_session_catalog_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Every call re-renders each recorded pointer; the path lookup must not run per pointer."""
    session_root = tmp_path / "sessions" / "moved"
    middleware = SystemReminderMiddleware(runtime=MagicMock(), session_root=session_root)
    monkeypatch.setattr(middleware.sources.runtime_env, "snapshot", lambda: "runtime")
    pointer = _catalog_pointer_text(2, "/old/root/sessions/abc/compactions/dropped/catalog.jsonl")
    history: list[Message] = []
    for index in range(3):
        history += [_user(f"turn {index}", **{_RECORD: [{"kind": "turn", "text": pointer}]}), _assistant("a")]
    resolved: list[Path] = []
    real_resolve = type(session_root).resolve

    def _counting_resolve(path: Path, strict: bool = False) -> Path:
        resolved.append(path)
        return real_resolve(path, strict=strict)

    monkeypatch.setattr(type(session_root), "resolve", _counting_resolve)
    middleware.prepare_turn()

    await enrich_call(middleware, [*history, _user("next")])
    await enrich_call(middleware, [*history, _user("next")])

    assert resolved == [session_root]


# ---------------------------------------------------------------------------
# Standing context, the turn line and the context-usage warning
# ---------------------------------------------------------------------------


def _usage(percent: int) -> dict[str, int]:
    return {"total_token_count": DEFAULT_MAX_CONTEXT_TOKENS * percent // 100}


def _turn_line(hint: str, percent: int) -> str:
    return f"{hint}\n{format_usage_line(_usage(percent), max_context_tokens=DEFAULT_MAX_CONTEXT_TOKENS)}"


async def _next_turn(
    middleware: SystemReminderMiddleware,
    history: list[Message],
    text: str,
    *,
    usage: dict[str, int] | None = None,
) -> list[str]:
    """Start a turn opened by *text* after *history*, send it, and return what the opener carried."""
    middleware.prepare_turn(usage=usage)
    history.append(_user(text))
    wire = await enrich_call(middleware, history)
    history.append(_assistant(f"answered {text}"))
    return _texts(wire[-1])


async def test_runtime_environment_is_resent_only_when_it_changes(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime: list[str | None] = ["env A"]
    middleware = _middleware(monkeypatch, runtime=runtime)
    history: list[Message] = []

    assert await _next_turn(middleware, history, "first") == ["first", _wrap("turn one"), _wrap("env A")]
    assert _record(history[0]) == [
        {"kind": "turn", "text": "turn one"},
        {"kind": "catalog", "text": "env A", "name": "runtime"},
    ]
    assert await _next_turn(middleware, history, "second") == ["second", _wrap("turn two")]
    runtime[0] = "env B"
    assert await _next_turn(middleware, history, "third") == ["third", _wrap("turn three"), _wrap("env B")]


async def test_todo_list_is_resent_when_it_changes_and_withdrawn_once_cleared(monkeypatch: pytest.MonkeyPatch) -> None:
    todo: list[str | None] = ["todo v1"]
    middleware = _middleware(monkeypatch, hints=[f"turn {n}" for n in range(1, 6)], todo=todo)
    history: list[Message] = []

    assert await _next_turn(middleware, history, "first") == ["first", _wrap("turn 1"), _wrap("todo v1")]
    assert await _next_turn(middleware, history, "second") == ["second", _wrap("turn 2")]
    todo[0] = "todo v2"
    assert await _next_turn(middleware, history, "third") == ["third", _wrap("turn 3"), _wrap("todo v2")]
    todo[0] = None
    cleared = TodoSource.withdrawn
    assert await _next_turn(middleware, history, "fourth") == ["fourth", _wrap("turn 4"), _wrap(cleared)]
    assert await _next_turn(middleware, history, "fifth") == ["fifth", _wrap("turn 5")]


async def test_background_commands_are_resent_when_they_change_and_withdrawn_once_none_are_left(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    listed: list[str | None] = ["bg-1 running"]
    middleware = _middleware(monkeypatch, hints=[f"turn {n}" for n in range(1, 6)], background_tasks=listed)
    history: list[Message] = []

    assert await _next_turn(middleware, history, "first") == ["first", _wrap("turn 1"), _wrap("bg-1 running")]
    assert _record(history[0])[-1] == {"kind": "catalog", "text": "bg-1 running", "name": "background_tasks"}
    assert await _next_turn(middleware, history, "second") == ["second", _wrap("turn 2")]
    listed[0] = "bg-1 exited with code 0"
    assert await _next_turn(middleware, history, "third") == [
        "third",
        _wrap("turn 3"),
        _wrap("bg-1 exited with code 0"),
    ]
    listed[0] = None
    withdrawn = BackgroundTasksSource.withdrawn
    assert await _next_turn(middleware, history, "fourth") == ["fourth", _wrap("turn 4"), _wrap(withdrawn)]
    assert await _next_turn(middleware, history, "fifth") == ["fifth", _wrap("turn 5")]


async def test_sub_agent_tip_goes_once_and_is_withdrawn_when_none_are_left(monkeypatch: pytest.MonkeyPatch) -> None:
    middleware = _middleware(monkeypatch, sub_agent_names=["Explore"])
    history: list[Message] = []

    first = await _next_turn(middleware, history, "first")
    assert first[:2] == ["first", _wrap("turn one")]
    assert len(first) == 3
    assert "Sub-agents are available (`Explore`)" in first[2]
    assert await _next_turn(middleware, history, "second") == ["second", _wrap("turn two")]

    # A profile without sub-agents rebuilds the middleware.
    rebuilt = _middleware(monkeypatch, hints=("turn after switch",))
    withdrawn = SubAgentsSource.withdrawn
    assert await _next_turn(rebuilt, history, "third") == ["third", _wrap("turn after switch"), _wrap(withdrawn)]


async def test_context_warning_goes_once_per_threshold_crossing(monkeypatch: pytest.MonkeyPatch) -> None:
    middleware = _middleware(monkeypatch, hints=[f"turn {n}" for n in range(1, 5)])
    history: list[Message] = []

    crossed = await _next_turn(middleware, history, "first", usage=_usage(60))
    assert crossed == ["first", _wrap(_turn_line("turn 1", 60)), _wrap(CONTEXT_USAGE_WARNING)]
    assert _record(history[0])[-1] == {"kind": "event", "text": CONTEXT_USAGE_WARNING}
    # Still above the threshold: the warning already went out.
    still_high = await _next_turn(middleware, history, "second", usage=_usage(70))
    assert still_high == ["second", _wrap(_turn_line("turn 2", 70))]
    # Falling below re-arms it; the next crossing warns again.
    assert await _next_turn(middleware, history, "third", usage=_usage(30)) == [
        "third",
        _wrap(_turn_line("turn 3", 30)),
    ]
    crossed_again = await _next_turn(middleware, history, "fourth", usage=_usage(55))
    assert crossed_again == ["fourth", _wrap(_turn_line("turn 4", 55)), _wrap(CONTEXT_USAGE_WARNING)]


async def test_context_warning_whose_request_never_went_out_warns_on_the_next_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    middleware = _middleware(monkeypatch)
    middleware.prepare_turn(usage=_usage(60))
    unsent = await enrich_call(middleware, [_user("first")], requests=0)
    assert _texts(unsent[0])[-1] == _wrap(CONTEXT_USAGE_WARNING)

    opener = _user("again")
    middleware.prepare_turn(usage=_usage(60))
    sent = await enrich_call(middleware, [opener])

    assert _texts(sent[0])[-1] == _wrap(CONTEXT_USAGE_WARNING)
    assert _record(opener)[-1] == {"kind": "event", "text": CONTEXT_USAGE_WARNING}


@pytest.mark.parametrize("preserve", [True, False], ids=["preserve", "fresh-snapshot"])
async def test_retry_does_not_repeat_the_context_warning(monkeypatch: pytest.MonkeyPatch, preserve: bool) -> None:
    middleware = _middleware(monkeypatch)
    opener = _user("first")
    middleware.prepare_turn(usage=_usage(60))
    first = await enrich_call(middleware, [opener])

    middleware.prepare_turn(usage=_usage(60), preserve_turn_reminders=preserve)
    retry = await enrich_call(middleware, [opener, _assistant("partial"), _injected("more")])

    assert _texts(retry[0]) == _texts(first[0])
    assert _texts(retry[2]) == ["more"]
    assert _count(retry, CONTEXT_USAGE_WARNING) == 1


# ---------------------------------------------------------------------------
# Folds: restore_folded_reminders
# ---------------------------------------------------------------------------


async def _folded_second_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[SystemReminderMiddleware, Message, Message, list[Message]]:
    """Turn 1 carries the catalog; turn 2's first call has LAST_WORDS and no catalog."""
    middleware, last_words = _middleware_pair(monkeypatch, catalog=[_CATALOG])
    opener = _user("first")
    middleware.prepare_turn()
    await enrich_call(middleware, [opener])

    next_opener = _user("second")
    middleware.prepare_turn()
    last_words.set_last_words("progress note")
    wire = await enrich_call(middleware, [opener, _assistant("a"), next_opener])
    return middleware, opener, next_opener, wire


async def test_restore_folded_reminders_resends_the_catalog_before_last_words(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    middleware, opener, next_opener, wire = await _folded_second_turn(monkeypatch)
    before = wire[2]
    before_texts = _texts(before)
    assert before_texts[-1].startswith(_LAST_WORDS_PREFIX)
    record_before = _record(next_opener)

    # A fold below the middleware excludes the catalog's carrier in place.
    opener.additional_properties[EXCLUDED_KEY] = True
    index = middleware.restore_folded_reminders(wire)

    assert index == 2
    rebuilt = wire[2]
    assert rebuilt is not before
    assert rebuilt.additional_properties is next_opener.additional_properties
    assert rebuilt.message_id == before.message_id
    assert _texts(rebuilt) == [*before_texts[:-1], _wrap(_CATALOG), before_texts[-1]]
    assert _texts(before) == before_texts
    assert _record(next_opener) == [*record_before, {"kind": "catalog", "text": _CATALOG, "name": "skills"}]
    assert _record(next_opener) is not record_before

    # The next call renders the catalog from the record, in the same place.
    next_call = await enrich_call(middleware, [opener, _assistant("a"), next_opener])
    assert _texts(next_call[2]) == _texts(rebuilt)


async def test_restore_folded_reminders_is_a_no_op_while_the_catalog_is_in_view(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    middleware, _opener, _next_opener, wire = await _folded_second_turn(monkeypatch)
    before = list(wire)

    assert middleware.restore_folded_reminders(wire) is None
    assert all(after is original for after, original in zip(wire, before, strict=True))


async def test_restore_folded_reminders_skips_an_excluded_last_user_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    middleware, opener, next_opener, wire = await _folded_second_turn(monkeypatch)
    opener.additional_properties[EXCLUDED_KEY] = True
    next_opener.additional_properties[EXCLUDED_KEY] = True
    record_before = _record(next_opener)

    assert middleware.restore_folded_reminders(wire) is None
    assert _record(next_opener) is record_before


async def test_excluded_last_user_message_gets_no_new_reminders(monkeypatch: pytest.MonkeyPatch) -> None:
    middleware = _middleware(monkeypatch, catalog=[_CATALOG])
    opener = _user("first", **{EXCLUDED_KEY: True})
    middleware.prepare_turn()

    sent = await enrich_call(middleware, [opener])

    assert _RECORD not in opener.additional_properties
    assert _texts(sent[0]) == ["first"]

    opener.additional_properties.pop(EXCLUDED_KEY)
    visible = await enrich_call(middleware, [opener])

    assert _texts(visible[0]) == ["first", _wrap("turn one"), _wrap(_CATALOG)]


# ---------------------------------------------------------------------------
# The record never reaches a provider request
# ---------------------------------------------------------------------------


class _FakeAsyncOpenAI:
    base_url = "https://api.test"


def _chat_completions(messages: list[Message]) -> object:
    client = ChatCompletionsClient(model="glm-5.2", sdk_client=_FakeAsyncOpenAI())
    return chat_history.encode_messages(messages, variant=client.VARIANT)


def _responses(messages: list[Message]) -> object:
    return encode_input(messages, service_side=True, variant=OPENAI_RESPONSES)


def _anthropic(messages: list[Message]) -> object:
    return encode_messages(messages)


@pytest.mark.parametrize(
    "prepare",
    [_chat_completions, _responses, _anthropic],
    ids=["chat-completions", "responses", "anthropic"],
)
def test_record_never_reaches_a_provider_request(prepare: Callable[[list[Message]], object]) -> None:
    message = _user("hello", **{_RECORD: [{"kind": "turn", "text": "recorded runtime"}]})

    payload = json.dumps(prepare([message]), default=str)

    assert "hello" in payload
    assert _RECORD not in payload
    assert "recorded runtime" not in payload


# ---------------------------------------------------------------------------
# Injection reminders
# ---------------------------------------------------------------------------


async def _chain_call(
    injection: InjectionMiddleware,
    reminder: SystemReminderMiddleware,
    messages: list[Message],
    *,
    options: dict[str, Any] | None = None,
) -> list[Message]:
    """One call through the production order: injection, then reminder, then the request."""
    context = ChatContext(client=None, messages=list(messages), options=options)

    async def _request() -> None:
        await establish_request(context)

    await injection.process(context, lambda: reminder.process(context, _request))
    return cast("list[Message]", context.messages)


async def test_injection_reminders_ride_the_call_that_drains_the_injection(monkeypatch: pytest.MonkeyPatch) -> None:
    """An injection committed while a call awaits its checkpoint is not that call's to carry."""
    reminder = _middleware(monkeypatch)
    injection = InjectionMiddleware()
    injection.set_on_drained_reminders(reminder.queue_drained_injection_reminders)

    async def _checkpoint(batch: tuple[ConsumedInjection, ...]) -> None:
        if [consumed.text for consumed in batch] == ["A"]:
            # B's admission completes while A's recovery checkpoint flushes.
            injection.queue("B", injection_id="inj-b", reminders=("hook B",))

    injection.set_on_consumed_batch(_checkpoint)
    opener = _user("first")
    reminder.prepare_turn()
    await _chain_call(injection, reminder, [opener])

    injection.queue("A", injection_id="inj-a", reminders=("hook A",))
    second = await _chain_call(injection, reminder, [opener, _assistant("a")])
    assert _texts(second[-1]) == ["A", _wrap("hook A")]
    assert _count(second, "hook B") == 0
    (message_a,) = injection.drain_consumed_injection_messages()

    injection.queue("C", injection_id="inj-c", reminders=("hook C",))
    assert injection.cancel("inj-c") is not None
    history = [opener, _assistant("a"), message_a, _assistant("b")]
    polled = await _chain_call(injection, reminder, history, options={"continuation_token": "resp_1"})
    assert _count(polled, "hook B") == 0

    third = await _chain_call(injection, reminder, history)
    assert _texts(third[2]) == ["A", _wrap("hook A")]
    assert _texts(third[-1]) == ["B", _wrap("hook B")]
    assert _count(third, "hook C") == 0
    assert _record(message_a) == [{"kind": "event", "text": "hook A"}]


# ---------------------------------------------------------------------------
# Service-side continuation: the conversation holds what earlier calls sent
# ---------------------------------------------------------------------------


async def _no_updates() -> AsyncIterator[ChatResponseUpdate]:
    return
    yield


async def _service_call(
    middleware: SystemReminderMiddleware,
    messages: list[Message],
    *,
    handle: str | None,
    answer: str | None,
    stream: bool = False,
    fail: bool = False,
    options: dict[str, Any] | None = None,
) -> list[Message]:
    """One call continuing *handle* (None: full local history), answered with the handle *answer*.

    A streamed answer gets the chain's result hooks attached, as the pipeline
    does, and is consumed to the end.
    """
    call_options = dict(options or {})
    if handle is not None:
        call_options["conversation_id"] = handle
    context = ChatContext(client=None, messages=list(messages), options=call_options, stream=stream)
    response = ChatResponse(messages=[], conversation_id=answer)

    async def _call_next() -> None:
        await establish_request(context)
        if fail:
            raise RuntimeError("provider failed")
        if stream:
            context.result = ResponseStream(_no_updates(), finalizer=lambda _updates: response)
        else:
            context.result = response

    if fail:
        with pytest.raises(RuntimeError, match="provider failed"):
            await middleware.process(context, _call_next)
    else:
        await middleware.process(context, _call_next)
    result = context.result
    if isinstance(result, ResponseStream):
        for hook in context.stream_result_hooks:
            result.with_result_hook(hook)
        assert await result.get_final_response() is response
    return cast("list[Message]", context.messages)


async def test_service_continuation_does_not_resend_the_catalog_it_holds(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each turn sends only its new messages; the conversation already holds the catalog."""
    middleware = _middleware(monkeypatch, catalog=[_CATALOG])
    first, second, third = _user("first"), _user("second"), _user("third")
    middleware.prepare_turn()
    sent = await _service_call(middleware, [first], handle=None, answer="resp_1")
    assert _count(sent, _CATALOG) == 1

    middleware.prepare_turn()
    sent = await _service_call(middleware, [second], handle="resp_1", answer="resp_2")
    assert _count(sent, "turn two") == 1
    assert _count(sent, _CATALOG) == 0
    assert [entry["kind"] for entry in _record(second)] == ["turn"]
    # A tool-loop call sends no user message; the next answer still holds it.
    await _service_call(middleware, [_assistant("tool call")], handle="resp_2", answer="resp_3")

    middleware.prepare_turn()
    sent = await _service_call(middleware, [third], handle="resp_3", answer="resp_4")
    assert _count(sent, _CATALOG) == 0

    # After a reset the full history renders what the conversation held: one copy.
    replay = await _service_call(
        middleware, [first, _assistant("a"), second, _assistant("b"), third], handle=None, answer=None
    )
    assert _count(replay, _CATALOG) == 1


async def test_service_continuation_sends_a_changed_or_withdrawn_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    catalog: list[str | None] = [_CATALOG]
    middleware = _middleware(monkeypatch, catalog=catalog)
    middleware.prepare_turn()
    await _service_call(middleware, [_user("first")], handle=None, answer="resp_1")

    changed = _CATALOG.replace("review", "deploy")
    catalog[0] = changed
    middleware.prepare_turn()
    sent = await _service_call(middleware, [_user("second")], handle="resp_1", answer="resp_2")
    assert _count(sent, changed) == 1

    catalog[0] = None
    middleware.prepare_turn()
    sent = await _service_call(middleware, [_user("third")], handle="resp_2", answer="resp_3")
    assert _count(sent, SkillsSource.withdrawn) == 1


@pytest.mark.parametrize(
    ("previous", "handle"),
    [
        pytest.param({"answer": "resp_1"}, "resp_other", id="another-handle"),
        pytest.param({"answer": None}, "resp_1", id="answer-without-handle"),
    ],
)
async def test_catalog_is_resent_unless_the_call_continues_the_answered_handle(
    monkeypatch: pytest.MonkeyPatch,
    previous: dict[str, Any],
    handle: str,
) -> None:
    middleware = _middleware(monkeypatch, catalog=[_CATALOG])
    middleware.prepare_turn()
    await _service_call(middleware, [_user("first")], handle=None, **previous)

    middleware.prepare_turn()
    sent = await _service_call(middleware, [_user("second")], handle=handle, answer="resp_2")

    assert _count(sent, _CATALOG) == 1


async def test_failed_call_forgets_what_the_conversation_holds(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed call may have left its catalog in a stable conversation (``conv_…``)."""
    catalog: list[str | None] = [_CATALOG]
    middleware = _middleware(monkeypatch, catalog=catalog)
    middleware.prepare_turn()
    await _service_call(middleware, [_user("first")], handle=None, answer="conv_1")

    catalog[0] = _CATALOG.replace("review", "deploy")
    middleware.prepare_turn()
    await _service_call(middleware, [_user("second")], handle="conv_1", answer=None, fail=True)

    catalog[0] = _CATALOG
    middleware.prepare_turn()
    sent = await _service_call(middleware, [_user("third")], handle="conv_1", answer="conv_1")

    assert _count(sent, _CATALOG) == 1


async def test_streamed_answer_holds_the_catalog_once_it_finalizes(monkeypatch: pytest.MonkeyPatch) -> None:
    middleware = _middleware(monkeypatch, catalog=[_CATALOG])
    middleware.prepare_turn()
    await _service_call(middleware, [_user("first")], handle=None, answer="resp_1", stream=True)

    middleware.prepare_turn()
    sent = await _service_call(middleware, [_user("second")], handle="resp_1", answer="resp_2", stream=True)

    assert _count(sent, _CATALOG) == 0


async def test_continuation_poll_keeps_the_held_catalogs(monkeypatch: pytest.MonkeyPatch) -> None:
    middleware = _middleware(monkeypatch, catalog=[_CATALOG])
    middleware.prepare_turn()
    await _service_call(middleware, [_user("first")], handle=None, answer="resp_1")
    await _service_call(
        middleware, [_user("first")], handle=None, answer=None, options={"continuation_token": "resp_1"}
    )

    middleware.prepare_turn()
    sent = await _service_call(middleware, [_user("second")], handle="resp_1", answer="resp_2")

    assert _count(sent, _CATALOG) == 0
