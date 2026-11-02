# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Completed side questions survive session restore and fork without changing turn checkpoints."""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path

import pytest

from chrys.foundation.models.side_questions import SideQuestionEntry
from chrys.foundation.platform.files import atomic_write_owner_only_text
from chrys.foundation.util.lock import FileLock
from chrys.kernel import Message
from chrys.service.session.side_questions import BTW_FILE_NAME, SideQuestionSessionSeed, load_side_questions
from chrys.service.state.store import JsonFileStateStore
from tests.support.waiting import wait_for, wait_until

_FIRST = SideQuestionEntry("2026-11-01T15:08:47+00:00", "What language?", "Mostly **Python**.")
_SECOND = SideQuestionEntry("2026-11-01T15:09:47+00:00", "Why?", "Because of its source files.")


async def test_btw_only_session_is_minimal_restorable_and_forks_independently(tmp_path: Path) -> None:
    store = JsonFileStateStore(tmp_path)
    seed = SideQuestionSessionSeed(agent_profile="Code", agent_display_name="Code", model_id="test-model")
    await store.save_side_question("parent", _FIRST, seed)
    await store.save_side_question("parent", _SECOND, seed)
    path = store.session_dir("parent") / BTW_FILE_NAME
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "session_id": "parent",
        "items": [
            {"created_at": _FIRST.created_at, "question": _FIRST.question, "answer": _FIRST.answer},
            {"created_at": _SECOND.created_at, "question": _SECOND.question, "answer": _SECOND.answer},
        ],
    }
    assert await store.load_session("parent") == {"messages": [], "compressed_msgs": [], "turn_counter": 0}
    listed = await store.list_sessions()
    assert [session.session_id for session in listed] == ["parent"]
    restored = JsonFileStateStore(tmp_path)
    assert load_side_questions(restored.session_dir("parent"), "parent") == (_FIRST, _SECOND)
    fork = await asyncio.to_thread(restored.fork_session, "parent")
    assert load_side_questions(restored.session_dir(fork), fork) == (_FIRST, _SECOND)
    await restored.save_side_question(fork, _FIRST, seed)
    assert load_side_questions(path.parent, "parent") == (_FIRST, _SECOND)
    await restored.delete_session("parent")
    assert not path.exists()
    assert len(load_side_questions(restored.session_dir(fork), fork)) == 3


async def test_fork_reads_side_questions_from_parent_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from chrys.service.state import _fork

    store = JsonFileStateStore(tmp_path)
    seed = SideQuestionSessionSeed()
    await store.save_side_question("parent", _FIRST, seed)
    await store.save_side_question("parent", _SECOND, seed)
    parent_dir = store.session_dir("parent")

    def load_parent_only(directory: Path, session_id: str) -> tuple[SideQuestionEntry, ...]:
        if directory != parent_dir:
            raise OSError("copied side question file has a different owner")
        return load_side_questions(directory, session_id)

    monkeypatch.setattr(_fork, "load_side_questions", load_parent_only)
    fork = await asyncio.to_thread(store.fork_session, "parent")
    assert load_side_questions(store.session_dir(fork), fork) == (_FIRST, _SECOND)


async def test_btw_commit_preserves_primary_and_inflight_recovery(tmp_path: Path) -> None:
    store = JsonFileStateStore(tmp_path)
    state = {"messages": [Message("user", ["Main task"])]}
    await store.save_session("main", state, agent_profile="Code")
    directory = store.session_dir("main")
    primary = (directory / "session.json").read_bytes()
    backup = (directory / "session.json.bak").read_bytes()
    recovery = directory / "session.recovery.json"
    await asyncio.to_thread(atomic_write_owner_only_text, recovery, '{"checkpoint": "in-flight"}')
    await store.save_side_question("main", _FIRST, SideQuestionSessionSeed(agent_profile="Other"))
    assert (directory / "session.json").read_bytes() == primary
    assert (directory / "session.json.bak").read_bytes() == backup
    assert recovery.read_text(encoding="utf-8") == '{"checkpoint": "in-flight"}'
    assert load_side_questions(directory, "main") == (_FIRST,)


@pytest.mark.parametrize("payload", ['{"session_id":"other","items":[]}', "{broken json"])
async def test_invalid_btw_history_is_not_overwritten(tmp_path: Path, payload: str) -> None:
    store = JsonFileStateStore(tmp_path)
    await store.save_side_question("main", _FIRST, SideQuestionSessionSeed())
    path = store.session_dir("main") / BTW_FILE_NAME
    await asyncio.to_thread(atomic_write_owner_only_text, path, payload)
    with pytest.raises(ValueError):
        await store.save_side_question("main", _SECOND, SideQuestionSessionSeed())
    assert path.read_text(encoding="utf-8") == payload


@pytest.mark.parametrize("payload", ['{"session_id":"other","items":[]}', "{broken json"])
async def test_fork_skips_corrupt_side_history_and_preserves_parent(tmp_path: Path, payload: str, caplog) -> None:
    store = JsonFileStateStore(tmp_path)
    await store.save_session("parent", {"messages": [Message("user", ["Main task"])]})
    source = store.session_dir("parent") / BTW_FILE_NAME
    await asyncio.to_thread(atomic_write_owner_only_text, source, payload)
    fork = await asyncio.to_thread(store.fork_session, "parent")
    assert await store.load_session(fork) is not None
    assert not (store.session_dir(fork) / BTW_FILE_NAME).exists()
    assert source.read_text(encoding="utf-8") == payload
    assert "Omitting unreadable side questions" in caplog.text


@pytest.mark.parametrize("question", ["\n  line1\nline2", "long title " * 30])
async def test_btw_session_title_uses_chat_title_normalization(tmp_path: Path, question: str) -> None:
    from chrys.service.state._session_meta import _extract_title

    store = JsonFileStateStore(tmp_path)
    entry = SideQuestionEntry(_FIRST.created_at, question, _FIRST.answer)
    await store.save_side_question("side", entry, SideQuestionSessionSeed())
    (session,) = await store.list_sessions()
    assert session.title == _extract_title([Message("user", [question])])
    assert "\n" not in session.title


async def test_btw_saving_does_not_refresh_existing_session_recency(tmp_path: Path) -> None:
    store = JsonFileStateStore(tmp_path)
    seed = SideQuestionSessionSeed()
    await store.save_side_question("older", _FIRST, seed)
    await store.save_side_question("newer", _FIRST, seed)
    await store.save_side_question("older", _SECOND, seed)
    assert await store.load_latest_session_id(chat_only=True) == "newer"


@pytest.mark.parametrize("cancel_queued", [False, True])
async def test_btw_save_waits_for_main_write_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cancel_queued: bool
) -> None:
    from chrys.service.state import store as store_module

    store = JsonFileStateStore(tmp_path)
    entered = threading.Event()
    cancelled = threading.Event()

    class ObservedLock(FileLock):
        def acquire(self) -> None:
            entered.set()
            super().acquire()

    lock = FileLock(store._write_lock_path("main"))
    await asyncio.to_thread(lock.acquire)
    monkeypatch.setattr(store_module, "FileLock", ObservedLock)
    save = asyncio.create_task(store.save_side_question("main", _FIRST, SideQuestionSessionSeed(), cancelled=cancelled))
    try:
        await wait_for(lambda: entered.is_set() or save.done(), description="side save reaches held write lock")
        if save.done():
            await save
        assert entered.is_set()
        assert not await wait_until(save.done, timeout=0.1), "side save must wait for the main checkpoint"
        if cancel_queued:
            cancelled.set()
    finally:
        lock.release()
        await save
    if cancel_queued:
        assert not store.session_dir("main").exists()
    else:
        assert load_side_questions(store.session_dir("main"), "main") == (_FIRST,)


async def test_cancelled_btw_write_cannot_recreate_a_deleted_session(tmp_path: Path) -> None:
    store = JsonFileStateStore(tmp_path)
    await store.save_side_question("main", _FIRST, SideQuestionSessionSeed())
    cancelled = threading.Event()
    cancelled.set()
    await store.delete_session("main")
    await store.save_side_question("main", _SECOND, SideQuestionSessionSeed(), cancelled=cancelled)
    assert not store.session_dir("main").exists()


def test_legacy_completion_order_is_read_in_question_order(tmp_path: Path) -> None:
    # Include different offsets: ordering compares instants, not ISO text.
    first = {"created_at": "2026-11-01T16:00:00+08:00", "question": "First?", "answer": "First."}
    second = {"created_at": "2026-11-01T09:00:00+00:00", "question": "Second?", "answer": "Second."}
    path = tmp_path / BTW_FILE_NAME
    atomic_write_owner_only_text(path, json.dumps({"session_id": "main", "items": [second, first]}))
    before = path.read_bytes()
    assert [entry.question for entry in load_side_questions(tmp_path, "main")] == ["First?", "Second?"]
    assert path.read_bytes() == before
