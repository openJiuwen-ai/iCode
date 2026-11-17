# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for global persisted prompt history."""

from __future__ import annotations

import asyncio
import contextlib
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from chrys.app.tui.support import prompt_history
from tests.support.async_tasks import wait_for_completion, wait_for_started
from tests.support.windows_replace import briefly_locked_rename


@pytest.fixture
def history_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    fake_platform = type("P", (), {"config_dir": tmp_path})()
    monkeypatch.setattr(prompt_history, "get_platform", lambda: fake_platform)
    monkeypatch.delenv("CHRYS_HISTORY_DISABLE", raising=False)
    return tmp_path


def _read_jsonl(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


@pytest.mark.asyncio
async def test_writer_serializes_appends_off_event_loop() -> None:
    loop_thread = threading.current_thread()
    calls: list[tuple[str, threading.Thread]] = []

    def append(text: str, **_kwargs: object) -> None:
        calls.append((text, threading.current_thread()))

    writer = prompt_history.PromptHistoryWriter(append)
    writer.enqueue("first")
    writer.enqueue("second")
    await writer.close(timeout_seconds=None)

    assert [text for text, _thread in calls] == ["first", "second"]
    assert all(thread is not loop_thread for _text, thread in calls)


@pytest.mark.asyncio
async def test_writer_flush_makes_queued_entries_visible_without_closing() -> None:
    calls: list[str] = []
    writer = prompt_history.PromptHistoryWriter(lambda text, **_kwargs: calls.append(text))

    writer.enqueue("first")
    await writer.flush()
    writer.enqueue("second")
    await writer.flush()

    assert calls == ["first", "second"]
    await writer.close(timeout_seconds=None)


@pytest.mark.asyncio
async def test_flush_after_external_cancel_with_pending_queue_does_not_hang() -> None:
    calls: list[str] = []

    def append(text: str, *, session_id: str | None, instance_id: str | None, cwd: str | None) -> None:
        calls.append(text)

    writer = prompt_history.PromptHistoryWriter(append)
    writer.enqueue("pending")
    task = writer._task
    assert task is not None
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    assert not writer._queue.empty()
    try:
        await asyncio.wait_for(writer.flush(), timeout=1)
        writer.enqueue("after")
        await asyncio.wait_for(writer.flush(), timeout=1)
        assert calls == ["pending", "after"]
    finally:
        # Restart even after a failing assertion, so teardown drains stranded work.
        writer.enqueue("cleanup")
        await writer.close(timeout_seconds=None)


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_target", ["worker", "close", "flush"])
async def test_flush_handles_cancellation_while_waiting(monkeypatch: pytest.MonkeyPatch, cancel_target: str) -> None:
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    joining = asyncio.Event()
    calls: list[str] = []

    def append(text: str, *, session_id: str | None, instance_id: str | None, cwd: str | None) -> None:
        started.set()
        try:
            assert release.wait(timeout=5)
            calls.append(text)
        finally:
            finished.set()

    writer = prompt_history.PromptHistoryWriter(append)
    real_join = writer._queue.join

    async def join() -> None:
        joining.set()
        await real_join()

    monkeypatch.setattr(writer._queue, "join", join)
    writer.enqueue("first")
    writer.enqueue("second")
    worker = writer._task
    assert worker is not None
    flushing = asyncio.create_task(writer.flush())
    try:
        assert await asyncio.to_thread(started.wait, 5)
        await wait_for_started(joining, flushing, description="history flush joining the writer")
        assert not flushing.done()
        if cancel_target == "flush":
            flushing.cancel()
            with pytest.raises(asyncio.CancelledError):
                await flushing
            assert not worker.done()
            release.set()
            await wait_for_completion(writer.flush())
            assert calls == ["first", "second"]
        else:
            if cancel_target == "close":
                await writer.close(timeout_seconds=0)
            else:
                worker.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await worker
            await wait_for_completion(flushing)
            assert not writer._queue.empty()
            # Also cover a later Ctrl+R after a timed-out close/dead worker.
            await wait_for_completion(writer.flush())
    finally:
        release.set()
        flushing.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await flushing
        await writer.close(timeout_seconds=None)
        assert await asyncio.to_thread(finished.wait, 5)


def test_append_then_load_roundtrip(history_dir: Path) -> None:
    prompt_history.append_history("first")
    prompt_history.append_history("second")

    assert prompt_history.load_history() == ["first", "second"]
    assert prompt_history.history_path() == history_dir / "prompt_history.jsonl"


def test_append_uses_bounded_file_lock(history_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    timeouts: list[float | None] = []

    class RecordingFileLock:
        def __init__(self, _path: Path, *, timeout: float | None = None) -> None:
            timeouts.append(timeout)

        def __enter__(self) -> RecordingFileLock:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    monkeypatch.setattr(prompt_history, "FileLock", RecordingFileLock)

    prompt_history.append_history("first")

    assert timeouts == [prompt_history._HISTORY_WRITE_LOCK_TIMEOUT_SECONDS]


def test_load_uses_bounded_file_lock(history_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = history_dir / prompt_history.HISTORY_FILENAME
    path.write_text('{"text":"first"}\n', encoding="utf-8")
    timeouts: list[float | None] = []

    class RecordingFileLock:
        def __init__(self, _path: Path, *, timeout: float | None = None) -> None:
            timeouts.append(timeout)

        def __enter__(self) -> RecordingFileLock:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    monkeypatch.setattr(prompt_history, "FileLock", RecordingFileLock)

    assert prompt_history.load_history() == ["first"]
    assert timeouts == [prompt_history._HISTORY_READ_LOCK_TIMEOUT_SECONDS]


def test_writes_codex_style_metadata_without_filtering_on_read(history_dir: Path) -> None:
    prompt_history.append_history("from a", session_id="session-a", cwd="/work/a")
    prompt_history.append_history("from b", session_id="session-b", cwd="/work/b")

    assert prompt_history.load_history() == ["from a", "from b"]
    records = _read_jsonl(history_dir / "prompt_history.jsonl")
    assert records[0]["session_id"] == "session-a"
    assert records[0]["cwd"] == "/work/a"
    assert records[1]["session_id"] == "session-b"
    assert records[1]["cwd"] == "/work/b"


def test_load_caps_to_max_entries(history_dir: Path) -> None:
    path = history_dir / "prompt_history.jsonl"
    path.write_text("".join(json.dumps({"text": f"item-{i}"}) + "\n" for i in range(5)), encoding="utf-8")

    assert prompt_history.load_history(max_entries=3) == ["item-2", "item-3", "item-4"]


def test_instance_filtered_load_caps_after_filtering(history_dir: Path) -> None:
    path = history_dir / "prompt_history.jsonl"
    rows = [
        {"text": "a-1", "instance_id": "a"},
        {"text": "b-1", "instance_id": "b"},
        {"text": "a-2", "instance_id": "a"},
        {"text": "a-3", "instance_id": "a"},
    ]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")

    assert prompt_history.load_history(max_entries=2, instance_id="a") == ["a-2", "a-3"]


def test_compaction_after_overflow(history_dir: Path) -> None:
    for i in range(7):
        prompt_history.append_history(f"item-{i}", max_entries=5)

    assert prompt_history.load_history(max_entries=10) == ["item-2", "item-3", "item-4", "item-5", "item-6"]
    assert len((history_dir / "prompt_history.jsonl").read_text(encoding="utf-8").splitlines()) == 5


def test_compaction_retries_a_briefly_locked_history_file(history_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for i in range(6):
        prompt_history.append_history(f"item-{i}", max_entries=5)
    rename = briefly_locked_rename(monkeypatch, failures=2)

    prompt_history.append_history("item-6", max_entries=5)

    assert len(rename.attempts) == 3
    assert prompt_history.load_history(max_entries=10) == ["item-2", "item-3", "item-4", "item-5", "item-6"]


def test_default_history_limit_is_1000_and_compacts_after_overflow_buffer(history_dir: Path) -> None:
    path = history_dir / "prompt_history.jsonl"
    path.write_text(
        "".join(json.dumps({"text": f"item-{i}"}) + "\n" for i in range(1001)),
        encoding="utf-8",
    )

    assert prompt_history.load_history() == [f"item-{i}" for i in range(1, 1001)]
    assert len(_read_jsonl(path)) == 1001

    path.write_text(
        "".join(json.dumps({"text": f"item-{i}"}) + "\n" for i in range(1100)),
        encoding="utf-8",
    )
    prompt_history.append_history("item-1100")

    assert prompt_history.load_history() == [f"item-{i}" for i in range(101, 1101)]
    assert len(_read_jsonl(path)) == 1000


def test_disable_env_var(history_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHRYS_HISTORY_DISABLE", "1")

    prompt_history.append_history("hidden")

    assert prompt_history.load_history() == []
    assert not (history_dir / "prompt_history.jsonl").exists()


def test_corrupt_line_skipped(history_dir: Path) -> None:
    path = history_dir / "prompt_history.jsonl"
    path.write_text('{"text": "ok"}\nnot-json\n{"text": "still ok"}\n', encoding="utf-8")

    assert prompt_history.load_history() == ["ok", "still ok"]


def test_invalid_utf8_line_skipped(history_dir: Path) -> None:
    path = history_dir / "prompt_history.jsonl"
    path.write_bytes(b'{"text":"ok"}\n\xff\xfe\xfd\n{"text":"still ok"}\n')

    assert prompt_history.load_history() == ["ok", "still ok"]


def test_append_after_unterminated_invalid_utf8_line(history_dir: Path) -> None:
    path = history_dir / "prompt_history.jsonl"
    path.write_bytes(b'{"text":"ok"}\n\xff\xfe\xfd')

    prompt_history.append_history("new")

    assert prompt_history.load_history() == ["ok", "new"]


def test_append_after_unterminated_invalid_json_line(history_dir: Path) -> None:
    path = history_dir / "prompt_history.jsonl"
    path.write_bytes(b'{"text":"ok"}\n{"text":"torn"')

    prompt_history.append_history("new")

    assert prompt_history.load_history() == ["ok", "new"]


def test_non_object_or_missing_text_lines_skipped(history_dir: Path) -> None:
    path = history_dir / "prompt_history.jsonl"
    rows: list[object] = [{"text": "ok"}, ["bad"], {"ts": "missing"}, {"text": 42}, {"text": "still ok"}]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")

    assert prompt_history.load_history() == ["ok", "still ok"]


def test_consecutive_duplicate_deduped_under_lock(history_dir: Path) -> None:
    prompt_history.append_history("same")
    prompt_history.append_history("same")

    assert prompt_history.load_history() == ["same"]
    assert len(_read_jsonl(history_dir / "prompt_history.jsonl")) == 1


def test_same_text_from_different_instances_is_not_deduped(history_dir: Path) -> None:
    prompt_history.append_history("same", session_id="session-a", instance_id="instance-a")
    prompt_history.append_history("same", session_id="session-b", instance_id="instance-b")

    assert prompt_history.load_history() == ["same", "same"]
    assert prompt_history.load_history(instance_id="instance-a") == ["same"]
    assert prompt_history.load_history(instance_id="instance-b") == ["same"]
    records = _read_jsonl(history_dir / "prompt_history.jsonl")
    assert records[0]["instance_id"] == "instance-a"
    assert records[1]["instance_id"] == "instance-b"


def test_concurrent_appends(history_dir: Path) -> None:
    texts = [f"item-{i}" for i in range(40)]

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda text: prompt_history.append_history(text, max_entries=100), texts))

    loaded = prompt_history.load_history(max_entries=100)
    assert sorted(loaded) == sorted(texts)
    assert len(loaded) == len(texts)


def test_empty_text_ignored(history_dir: Path) -> None:
    prompt_history.append_history("")
    prompt_history.append_history("   ")

    assert prompt_history.load_history() == []
    assert not (history_dir / "prompt_history.jsonl").exists()


def test_load_without_history_file_creates_nothing(history_dir: Path) -> None:
    """Reading before any append must not create the lock file as a side effect."""

    assert prompt_history.load_history() == []
    assert not (history_dir / "prompt_history.jsonl").exists()
    assert not (history_dir / "prompt_history.jsonl.lock").exists()


def test_non_ascii_text_written_as_raw_utf8(history_dir: Path) -> None:
    """CJK/emoji content must land on disk as UTF-8 bytes, not ``\\uXXXX`` escapes."""
    prompt_history.append_history("你好", cwd="/Users/0x7c13/项目/chrys")
    prompt_history.append_history("hello 🌏")

    raw = (history_dir / "prompt_history.jsonl").read_bytes()
    assert "你好".encode() in raw
    assert "项目".encode() in raw
    assert "🌏".encode() in raw
    assert b"\\u" not in raw

    assert prompt_history.load_history() == ["你好", "hello 🌏"]
