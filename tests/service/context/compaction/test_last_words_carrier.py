# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The progress note of a turn is recorded on one user message and sent from that record ever after.

Driven through the real reminder middleware and strategy, the way a request
is established: the middleware enriches the call's list, the strategy
compacts it, and the provider views are what goes out.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import pytest

from chrys.foundation.models.history_markers import HistoryMarkerKind, copy_reminder_record, read_last_words_record
from chrys.foundation.retry import restore_message_properties, snapshot_message_properties
from chrys.kernel import EXCLUDE_REASON_KEY, EXCLUDED_KEY, Message, apply_compaction
from chrys.service.agent_middleware.system_reminder import wrap_system_reminder as _wrap
from chrys.service.context.compaction.spill import SpillQuota
from chrys.service.state.serializers import deserialize_state, serialize_state
from tests.service.context.compaction._compaction_helpers import (
    _assistant_text,
    _build_tool_group,
    _estimate_tokens,
    _injected,
    _make_strategy,
    _tokenizer,
    _user,
)
from tests.support.phase4_stubs import StubLastWordsGenerator
from tests.support.reminder_calls import request_views
from tests.support.reminder_stack import reminder_pair, restore_phase4

if TYPE_CHECKING:
    from pathlib import Path

    from chrys.service.agent_middleware.system_reminder import SystemReminderMiddleware
    from chrys.service.context.compaction import UnifiedContextStrategy
    from chrys.service.context.compaction.last_words_state import LastWordsState

_RECORD = HistoryMarkerKind.LAST_WORDS_RECORD_KEY
_GROUPS = 12
_GROUP_WORDS = 600
_TODO = "[TODO]\n- [ ] port the lexer"


def _work(first: int, count: int = _GROUPS) -> list[Message]:
    """*count* tool groups, numbered from *first*: about nine tenths of the window together."""
    return [
        message
        for index in range(first, first + count)
        for message in _build_tool_group(f"call_{index}", "read_file", "word " * _GROUP_WORDS)
    ]


_WINDOW = int(_estimate_tokens([_user("Refactor the parser"), *_work(0)]) / 0.88)
"""A turn's opener and ``_work`` reach the trigger; the recent-tail allowance holds one group."""


class _Session:
    """One conversation's reminder stack and strategy over the history they share."""

    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        history: list[Message],
        *,
        generator: StubLastWordsGenerator | None = None,
    ) -> None:
        self.history = history
        self.generator = generator or StubLastWordsGenerator(text="## Task\nPort.\n## Progress\nRead 20 files.")
        quota = SpillQuota()
        self.middleware, self.last_words = reminder_pair(
            runtime=MagicMock(),
            session_root=tmp_path,
            spill_quota=quota,
            file_read_available=True,
            todo_state_provider=lambda: _TODO,
        )
        hints = iter(f"turn line {number}" for number in range(1, 50))
        monkeypatch.setattr(self.middleware.sources.turn_line, "clock", lambda: next(hints))
        monkeypatch.setattr(self.middleware.sources.runtime_env, "snapshot", lambda: None)
        self.strategy: UnifiedContextStrategy = _make_strategy(
            max_context_tokens=_WINDOW,
            last_words_generator=self.generator,
            reminder_middleware=self.middleware,
            last_words=self.last_words,
            spill_root=tmp_path,
            spill_quota=quota,
            spill_session_id="carrier",
        )

    async def request(self) -> list[Message]:
        """Establish one request over the history and return what the provider is sent."""

        async def _compact(messages: list[Message]) -> list[Message]:
            return await apply_compaction(messages, strategy=self.strategy, tokenizer=_tokenizer)

        return await request_views(self.middleware, self.history, prepare=_compact)


def _texts(message: Message) -> list[str]:
    return [content.text for content in message.contents if content.type == "text" and content.text]


def _wire_texts(messages: list[Message]) -> list[list[str]]:
    return [[content.text or content.type for content in message.contents] for message in messages]


def _note_blocks(message: Message) -> list[str]:
    return [text for text in _texts(message) if text.startswith("<system-reminder>\n[LAST_WORDS] ")]


def _dropped(history: list[Message]) -> list[Message]:
    return [m for m in history if m.additional_properties.get(EXCLUDE_REASON_KEY) == "current_turn_drop"]


def _carriers(history: list[Message]) -> list[Message]:
    return [m for m in history if read_last_words_record(m.additional_properties) is not None]


async def test_a_drop_records_the_note_on_the_turns_user_message_as_it_is_sent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    opener = _user("Refactor the parser")
    session = _Session(monkeypatch, tmp_path, [opener, *_work(0)])
    session.middleware.prepare_turn()

    sent = await session.request()

    assert len(_dropped(session.history)) == 2 * (_GROUPS - 1)
    record = read_last_words_record(opener.additional_properties)
    assert record is not None
    assert record.note == session.generator.text
    # Note, todo list and dropped-call manifest are one text, recorded as sent.
    assert record.text == session.last_words.render()[0]
    assert session.generator.text in record.text
    assert _TODO in record.text
    assert "--- Dropped this turn (records under " in record.text
    # After the message's own text and its other reminders.
    assert _texts(sent[0]) == ["Refactor the parser", _wrap("turn line 1"), _wrap(_TODO), _wrap(record.text)]
    # The message's own contents never hold it.
    assert _texts(opener) == ["Refactor the parser"]
    # The newest group stays, after the carrier.
    assert [message.role for message in sent] == ["user", "assistant", "tool"]


async def test_later_calls_and_a_mid_turn_message_leave_the_carrier_as_it_was(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    opener = _user("Refactor the parser")
    session = _Session(monkeypatch, tmp_path, [opener, *_work(0)])
    session.middleware.prepare_turn()
    await session.request()
    session.history.extend(_work(100, 1))
    before = await session.request()

    injected = _injected("Keep the old token names")
    session.history.extend([injected, *_work(101, 1)])
    after = await session.request()

    # Everything sent before the message arrived is sent again unchanged.
    assert _wire_texts(after[: len(before)]) == _wire_texts(before)
    assert _note_blocks(after[0])
    assert _carriers(session.history) == [opener]
    assert not _note_blocks(next(message for message in after if "Keep the old token names" in _texts(message)))


async def test_a_second_drop_of_the_turn_replaces_the_note_on_the_same_message(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    opener = _user("Refactor the parser")
    session = _Session(monkeypatch, tmp_path, [opener, *_work(0)])
    session.middleware.prepare_turn()
    await session.request()
    first = read_last_words_record(opener.additional_properties)
    injected = _injected("Keep the old token names")
    session.history.extend([injected, *_work(100)])
    session.generator.text = "## Task\nPort.\n## Progress\nRead 40 files, kept the token names."

    sent = await session.request()

    second = read_last_words_record(opener.additional_properties)
    assert first is not None
    assert second is not None
    assert second.note == session.generator.text
    assert first.note not in second.text
    assert opener.additional_properties[_RECORD] is not None
    assert _carriers(session.history) == [opener]
    assert _note_blocks(sent[0]) == [_wrap(second.text)]
    assert [_note_blocks(message) for message in sent[1:]] == [[] for _ in sent[1:]]
    # The second note was merged from the first.
    assert session.generator.calls[-1]["previous_last_words"] == first.note


async def test_a_carrier_that_left_the_view_hands_the_note_to_the_last_user_message(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Nothing excludes a user message of the running turn; if one is, the note is not lost with it."""
    opener = _user("Refactor the parser")
    session = _Session(monkeypatch, tmp_path, [opener, *_work(0)])
    session.middleware.prepare_turn()
    await session.request()
    assert _carriers(session.history) == [opener]
    injected = _injected("Keep the old token names")
    session.history.extend([injected, *_work(100)])
    opener.additional_properties[EXCLUDED_KEY] = True
    session.generator.text = "## Task\nPort.\n## Progress\nRead 40 files, kept the token names."

    with caplog.at_level(logging.WARNING, logger="chrys.service.agent_middleware.system_reminder"):
        sent = await session.request()

    # One record in the turn, on a message the request holds.
    assert _carriers(session.history) == [injected]
    record = read_last_words_record(injected.additional_properties)
    assert record is not None
    assert record.note == session.generator.text
    [carrier] = [message for message in sent if _note_blocks(message)]
    assert _texts(carrier)[0] == "Keep the old token names"
    assert _note_blocks(carrier) == [_wrap(record.text)]
    warnings = [entry.getMessage() for entry in caplog.records if entry.levelno == logging.WARNING]
    assert len([text for text in warnings if "LAST_WORDS note is no longer in view" in text]) == 1


async def test_the_next_turn_still_sends_the_note_of_the_turn_before(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    opener = _user("Refactor the parser")
    session = _Session(monkeypatch, tmp_path, [opener, *_work(0)])
    session.middleware.prepare_turn()
    during = await session.request()
    next_opener = _user("Now the lexer")
    session.history.extend([_assistant_text("The parser is refactored."), next_opener])

    session.middleware.prepare_turn()
    assert session.last_words.get_last_words() is None
    after = await session.request()

    assert _texts(after[0]) == _texts(during[0])
    assert _note_blocks(after[0])
    assert _texts(after[-1])[:2] == ["Now the lexer", _wrap("turn line 2")]
    assert not _note_blocks(after[-1])
    assert len(_dropped(session.history)) == 2 * (_GROUPS - 1)


async def test_a_reloaded_session_sends_the_carrier_byte_for_byte(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    opener = _user("Refactor the parser")
    session = _Session(monkeypatch, tmp_path, [opener, *_work(0)])
    session.middleware.prepare_turn()
    during = await session.request()
    session.history.append(_assistant_text("The parser is refactored."))

    reloaded_history = deserialize_state(serialize_state({"messages": session.history}))["messages"]
    reloaded = _Session(monkeypatch, tmp_path, [*reloaded_history, _user("Now the lexer")])
    reloaded.middleware.prepare_turn()
    after = await reloaded.request()

    assert _texts(after[0]) == _texts(during[0])
    assert _TODO in _note_blocks(after[0])[0]
    assert "--- Dropped this turn (records under " in _note_blocks(after[0])[0]
    assert [message.role for message in after] == ["user", "assistant", "tool", "assistant", "user"]


async def test_a_copy_of_the_session_sends_the_note_with_its_own_copy_of_the_records(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A fork copies the records and the recorded note alike; the note it sends names the copies it owns."""
    parent_root, fork_root = tmp_path / "parent", tmp_path / "fork"
    parent_root.mkdir()
    opener = _user("Refactor the parser")
    parent = _Session(monkeypatch, parent_root, [opener, *_work(0)])
    parent.middleware.prepare_turn()
    during = await parent.request()
    parent.history.append(_assistant_text("The parser is refactored."))
    record = read_last_words_record(opener.additional_properties)
    assert record is not None
    assert record.records_dir
    parent_records = (parent_root / record.records_dir).absolute().as_posix()
    assert f"(records under {parent_records}/)" in _note_blocks(during[0])[0]

    shutil.copytree(parent_root, fork_root)
    forked_history = deserialize_state(serialize_state({"messages": parent.history}))["messages"]
    fork = _Session(monkeypatch, fork_root, [*forked_history, _user("Now the lexer")])
    fork.middleware.prepare_turn()
    after = await fork.request()

    fork_records = (fork_root / record.records_dir).absolute().as_posix()
    assert _note_blocks(after[0]) == [_note_blocks(during[0])[0].replace(parent_records, fork_records)]
    assert parent_root.as_posix() not in "".join(_texts(after[0]))
    assert list((fork_root / record.records_dir).iterdir())
    # The parent goes on sending what it sent.
    parent.history.append(_user("Now the lexer"))
    parent.middleware.prepare_turn()
    assert _note_blocks((await parent.request())[0]) == _note_blocks(during[0])


async def test_a_rolled_back_attempt_takes_its_note_and_its_drop_back_together(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    opener = _user("Refactor the parser")
    session = _Session(monkeypatch, tmp_path, [opener, *_work(0)])
    session.middleware.prepare_turn()
    baseline = snapshot_message_properties(session.history)
    retry_snapshot = session.strategy.snapshot_retry_state()

    await session.request()
    assert _dropped(session.history)
    assert _carriers(session.history) == [opener]

    # Mirror the retry loop: history properties, then the strategy's own state.
    restore_message_properties(session.history, baseline)
    session.strategy.restore_retry_state(retry_snapshot)

    assert not _dropped(session.history)
    assert not _carriers(session.history)
    assert session.last_words.get_last_words() is None

    # The retry drops again and records its own note.
    session.generator.text = "## Task\nPort.\n## Progress\nSecond attempt."
    session.middleware.prepare_turn(preserve_last_words=True, preserve_turn_reminders=True)
    sent = await session.request()
    record = read_last_words_record(opener.additional_properties)
    assert record is not None
    assert record.note == "## Task\nPort.\n## Progress\nSecond attempt."
    assert _note_blocks(sent[0]) == [_wrap(record.text)]


class _CancelsOnceCommitted(StubLastWordsGenerator):
    """Cancels at the first await after the drop's synchronous commit."""

    def __init__(self, text: str) -> None:
        super().__init__(text=text)
        self.cancel = False

    async def publish_committed(self) -> None:
        if self.cancel:
            raise asyncio.CancelledError


async def _interrupted_second_drop(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> tuple[_Session, Message, str, str]:
    """A turn whose second drop is cancelled right after its commit; returns the session, its carrier and both notes."""
    opener = _user("Refactor the parser")
    generator = _CancelsOnceCommitted("## Task\nPort.\n## Progress\nOLD note: read 20 files.")
    session = _Session(monkeypatch, tmp_path, [opener, *_work(0)], generator=generator)
    session.middleware.prepare_turn()
    await session.request()
    old_note = generator.text
    session.history.extend(_work(100))
    generator.text = "## Task\nPort.\n## Progress\nNEW note: read 40 files."
    generator.cancel = True
    with pytest.raises(asyncio.CancelledError):
        await session.request()
    generator.cancel = False
    return session, opener, old_note, generator.text


async def test_a_drop_cancelled_after_its_commit_shows_the_new_note_to_the_next_user_message(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    session, _opener, old_note, new_note = await _interrupted_second_drop(monkeypatch, tmp_path)
    dropped = _dropped(session.history)
    assert len(dropped) == 2 * (2 * _GROUPS - 1)

    session.history.append(_user("What have you done so far?"))
    session.middleware.prepare_turn()
    sent = await session.request()

    assert _dropped(session.history) == dropped
    [block] = _note_blocks(sent[0])
    assert new_note in block
    assert old_note not in block
    assert not _note_blocks(sent[-1])
    assert all(not message.additional_properties.get(EXCLUDED_KEY, False) for message in sent)


async def test_a_drop_cancelled_after_its_commit_shows_the_new_note_to_a_continued_turn(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    session, opener, old_note, new_note = await _interrupted_second_drop(monkeypatch, tmp_path)

    session.middleware.prepare_turn(preserve_last_words=True, preserve_turn_reminders=True)
    sent = await session.request()

    record = read_last_words_record(opener.additional_properties)
    assert record is not None
    # The live note the next drop merges is the one the carrier records.
    assert session.last_words.get_last_words() == record.note == new_note
    assert _note_blocks(sent[0]) == [_wrap(record.text)]
    assert old_note not in record.text


async def test_a_drop_cancelled_after_its_commit_shows_the_new_note_after_a_reload(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    session, opener, old_note, new_note = await _interrupted_second_drop(monkeypatch, tmp_path)
    recorded = read_last_words_record(opener.additional_properties)
    assert recorded is not None
    saved: dict[str, Any] = {
        "last_words": session.last_words.get_last_words(),
        "last_words_manifest": session.last_words.get_last_words_manifest(),
    }

    reloaded_history = deserialize_state(serialize_state({"messages": session.history}))["messages"]
    reloaded = _Session(monkeypatch, tmp_path, reloaded_history)
    restore_phase4(
        _stack_of(reloaded.middleware, reloaded.last_words),
        saved,
        available_relative_paths={row["relative_path"] for row in saved["last_words_manifest"]},
    )
    reloaded.middleware.prepare_turn(preserve_last_words=True)
    sent = await reloaded.request()

    assert _note_blocks(sent[0]) == [_wrap(recorded.text)]
    assert new_note in recorded.text
    assert old_note not in recorded.text
    assert reloaded.last_words.get_last_words() == new_note
    assert len(_dropped(reloaded.history)) == len(_dropped(session.history))


def _stack_of(middleware: SystemReminderMiddleware, last_words: LastWordsState) -> Any:
    from tests.support.reminder_stack import ReminderStack

    return ReminderStack(
        middleware=middleware,
        last_words=last_words,
        switch=middleware.sources.profile_switch,
        pointer=middleware.sources.archive_pointer,
    )


class _FailsBeforeCommit(StubLastWordsGenerator):
    """Fails or is cancelled where the round still holds nothing: while the note is written."""

    def __init__(self, failure: BaseException) -> None:
        super().__init__()
        self.failure = failure

    async def generate(
        self,
        scoped_groups: list,
        previous_last_words: str | None,
        *,
        degraded_opener: bool,
        has_continuation_nudges: bool,
        completer: object | None = None,
        tokenizer: object | None = None,
        system_overhead_tokens: int = 0,
        tool_definition_tokens: int = 0,
        request_overhead_tokens: int = 0,
        calibration_ratio: float = 1.0,
        spend_side_call_tokens: object | None = None,
        affords_side_call_tokens: object | None = None,
        entry_messages: object | None = None,
    ) -> str:
        raise self.failure


@pytest.mark.parametrize("failure", [asyncio.CancelledError(), RuntimeError("provider down")], ids=["cancel", "error"])
async def test_a_round_stopped_before_its_commit_leaves_neither_a_drop_nor_a_note(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, failure: BaseException
) -> None:
    opener = _user("Refactor the parser")
    session = _Session(monkeypatch, tmp_path, [opener, *_work(0)], generator=_FailsBeforeCommit(failure))
    session.middleware.prepare_turn()

    with pytest.raises(type(failure)):
        await session.request()

    assert not _dropped(session.history)
    assert not _carriers(session.history)


async def test_dropped_messages_and_the_note_that_stands_for_them_exist_together(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Checked wherever a run can stop: after each request and after a cancelled commit."""
    session, opener, _old, new_note = await _interrupted_second_drop(monkeypatch, tmp_path)

    def _holds() -> bool:
        record = read_last_words_record(opener.additional_properties)
        return bool(_dropped(session.history)) == (record is not None) and (
            record is None or record.note == session.last_words.get_last_words()
        )

    assert _holds()
    assert read_last_words_record(opener.additional_properties).note == new_note  # type: ignore[union-attr]
    session.history.extend(_work(200))
    session.generator.text = "## Task\nPort.\n## Progress\nThird note."
    await session.request()
    assert _holds()
    assert len(_dropped(session.history)) == 2 * (3 * _GROUPS - 1)


async def test_a_note_without_a_record_goes_on_the_last_user_message_and_is_recorded_there(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A session saved before notes were recorded: the note is live state only."""
    opener = _user("Refactor the parser")
    injected = _injected("Keep the old token names")
    session = _Session(monkeypatch, tmp_path, [opener, *_work(0, 1), injected])
    session.middleware.prepare_turn()
    session.last_words.set_last_words("legacy note")

    sent = await session.request()

    assert not _note_blocks(sent[0])
    [block] = _note_blocks(sent[-1])
    assert "legacy note" in block
    record = read_last_words_record(injected.additional_properties)
    assert record is not None
    assert _wrap(record.text) == block
    assert record.note == "legacy note"

    # From then on the message it was recorded on carries it, wherever the turn goes.
    session.history.extend([*_work(1, 1), _injected("And the old error codes")])
    later = await session.request()
    assert _carriers(session.history) == [injected]
    assert [bool(_note_blocks(message)) for message in later if message.role == "user"] == [False, True, False]


async def test_a_newer_note_held_only_by_the_state_takes_the_records_place_on_the_same_message(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A build that keeps the note in the state alone ran the turn in between and wrote a newer one."""
    opener = _user("Refactor the parser")
    session = _Session(monkeypatch, tmp_path, [opener, *_work(0)])
    session.middleware.prepare_turn()
    await session.request()
    old_record = read_last_words_record(opener.additional_properties)
    assert old_record is not None
    newer_note = "## Task\nPort.\n## Progress\nRead 40 files."
    session.last_words.set_last_words(newer_note)
    session.history.append(_injected("Keep the old token names"))

    sent = await session.request()

    [block] = _note_blocks(sent[0])
    assert newer_note in block
    assert old_record.note not in block
    assert not _note_blocks(sent[-1])
    record = read_last_words_record(opener.additional_properties)
    assert record is not None
    assert record.note == newer_note
    assert _wrap(record.text) == block
    assert _carriers(session.history) == [opener]

    again = await session.request()
    assert _texts(again[0]) == _texts(sent[0])


async def test_a_state_without_a_note_leaves_the_recorded_one_as_it_is(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Only a note in the state can be newer than the record; the list of dropped calls alone is not one."""
    opener = _user("Refactor the parser")
    session = _Session(monkeypatch, tmp_path, [opener, *_work(0)])
    session.middleware.prepare_turn()
    during = await session.request()
    session.last_words.set_last_words(None)
    assert session.last_words.render()

    after = await session.request()

    assert _texts(after[0]) == _texts(during[0])


def test_a_rebuilt_copy_of_the_carrier_keeps_the_note_record() -> None:
    source = {_RECORD: {"text": "[LAST_WORDS] block", "note": "note"}}
    target: dict[str, Any] = {}

    copy_reminder_record(source, target)

    assert target == source
    assert target[_RECORD] is not source[_RECORD]
    for malformed in ({_RECORD: "text"}, {_RECORD: {"note": "note"}}, {_RECORD: {"text": ""}}):
        copied: dict[str, Any] = {}
        copy_reminder_record(malformed, copied)
        assert copied == {}
