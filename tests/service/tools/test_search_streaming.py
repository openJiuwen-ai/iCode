# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""rg output is read as it arrives: a full result stops rg, and oversized output is refused or named."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
from collections.abc import AsyncIterator, Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import psutil
import pytest

from chrys.foundation.tool_result_metadata import TOOL_ERROR_KIND_METADATA_KEY
from chrys.service.tools.builtins import search
from chrys.service.tools.result_metadata import tool_result_metadata
from tests.support.waiting import ENGINE_TURN_TIMEOUT, wait_for


@dataclass
class _ObservedRg:
    processes: list[psutil.Process] = field(default_factory=list)
    spawned: list[asyncio.subprocess.Process] = field(default_factory=list)
    read: int = 0

    def all_ended(self) -> bool:
        return bool(self.processes) and not any(process.is_running() for process in self.processes)


@pytest.fixture
def observed_rg(monkeypatch: pytest.MonkeyPatch) -> _ObservedRg:
    """Record every process a search starts, a launcher's child included, and how much rg output it read."""
    observed = _ObservedRg()
    spawn = search.managed_subprocess

    @contextlib.asynccontextmanager
    async def recording_spawn(*args: Any, **kwargs: Any) -> AsyncIterator[asyncio.subprocess.Process]:
        async with spawn(*args, **kwargs) as proc:
            observed.spawned.append(proc)
            # A launcher that exits at once may already be reaped.
            with contextlib.suppress(psutil.NoSuchProcess):
                observed.processes.append(psutil.Process(proc.pid))
            yield proc

    class CountingStdout(search._RgStdout):
        def __init__(self, consume: Callable[[bytes], bool] | None) -> None:
            super().__init__(consume)
            self._started = False

        def feed(self, chunk: bytes) -> None:
            if not self._started:
                self._started = True
                # A launcher's child that has output left to write is still alive here.
                with contextlib.suppress(psutil.NoSuchProcess):
                    for launcher in observed.processes[-1:]:
                        observed.processes.extend(launcher.children(recursive=True))
            observed.read += len(chunk)
            super().feed(chunk)

    monkeypatch.setattr(search, "managed_subprocess", recording_spawn)
    monkeypatch.setattr(search, "_RgStdout", CountingStdout)
    return observed


@pytest.mark.parametrize("glob", [None, "*.py"])
async def test_a_full_result_stops_rg_before_it_prints_the_rest(
    tmp_path: Path, observed_rg: _ObservedRg, glob: str | None
) -> None:
    # Each match record is over 100 bytes of JSON: the whole output would pass 10 MB. A globbed
    # search keeps a file's matches until its end record, so the result fills one file at a time.
    for index in range(100):
        (tmp_path / f"dense_{index:03}.py").write_text("NEEDLE\n" * 1_000, encoding="utf-8")

    result = await search.grep("NEEDLE", path=str(tmp_path), glob=glob, context_lines=0)

    assert "Found 50 match(es)" in result and "(limited to 50)" in result
    assert "Error" not in result
    assert 0 < observed_rg.read < 1024 * 1024
    await wait_for(observed_rg.all_ended, description="every rg process ended")


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell launcher")
@pytest.mark.skipif(
    os.environ.get("CHRYS_TEST_SANDBOX") == "1",
    reason="under the file-access sandbox the launcher and its rg are children of the broker, not of the spawned stub "
    "(known deviation; that they end is covered by the next test)",
)
async def test_a_full_result_stops_an_rg_that_a_launcher_started(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, observed_rg: _ObservedRg
) -> None:
    root = tmp_path / "src"
    root.mkdir()
    (root / "dense.txt").write_text("NEEDLE\n" * 200_000, encoding="utf-8")
    launcher = tmp_path / "rg"
    # Not exec: rg runs as the launcher's child, as under a version-manager shim.
    launcher.write_text(f'#!/bin/sh\n"{search._find_rg()}" "$@"\nexit $?\n', encoding="utf-8")
    launcher.chmod(0o755)
    monkeypatch.setattr(search, "_find_rg", lambda: str(launcher))

    result = await search.grep("NEEDLE", path=str(root), context_lines=0)

    assert "Found 50 match(es)" in result and "(limited to 50)" in result
    assert 0 < observed_rg.read < 1024 * 1024
    assert len(observed_rg.processes) >= 2  # The launcher and its rg.
    await wait_for(observed_rg.all_ended, description="the launcher and its rg ended")


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell launcher")
async def test_a_full_result_stops_an_rg_whose_launcher_has_exited(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, observed_rg: _ObservedRg
) -> None:
    root = tmp_path / "src"
    root.mkdir()
    (root / "dense.txt").write_text("NEEDLE\n" * 200_000, encoding="utf-8")
    gate, child_pid = tmp_path / "gate", tmp_path / "child.pid"
    launcher = tmp_path / "rg"
    # rg starts once the launcher has exited; its shell then holds the output open without writing to it.
    launcher.write_text(
        "#!/bin/sh\n"
        f'(while [ ! -e "{gate}" ]; do sleep 0.01; done; "{search._find_rg()}" "$@"; exec sleep 60) &\n'
        f'echo $! > "{child_pid}"\n',
        encoding="utf-8",
    )
    launcher.chmod(0o755)
    monkeypatch.setattr(search, "_find_rg", lambda: str(launcher))

    task = asyncio.ensure_future(search.grep("NEEDLE", path=str(root), context_lines=0))
    try:
        await wait_for(
            lambda: task.done() or (bool(observed_rg.spawned) and observed_rg.spawned[0].returncode is not None),
            timeout=ENGINE_TURN_TIMEOUT,
            description="the launcher exited",
        )
        assert not task.done()
        observed_rg.processes.append(psutil.Process(int(child_pid.read_text(encoding="utf-8"))))
    except BaseException:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        raise
    finally:
        gate.touch()
    result = await task

    assert "Found 50 match(es)" in result and "(limited to 50)" in result
    await wait_for(observed_rg.all_ended, description="the launcher's rg ended")


@pytest.mark.parametrize("glob", [None, "*.js"])
@pytest.mark.parametrize("other_match", [True, False])
async def test_a_match_too_long_to_keep_is_named_without_its_line(
    tmp_path: Path, glob: str | None, other_match: bool
) -> None:
    (tmp_path / "bundle.js").write_text("NEEDLE " + "x" * (2 * 1024 * 1024) + "\n", encoding="utf-8")
    (tmp_path / "short.js").write_text(f"const a = 1;\n{'NEEDLE' if other_match else 'other'} here\n", encoding="utf-8")

    result = await search.grep("NEEDLE", path=str(tmp_path), glob=glob, context_lines=1)

    if other_match:
        assert result.startswith(f"Found 1 match(es) in {tmp_path}\n\n")
        assert "short.js:2" in result and "NEEDLE here" in result and "const a = 1;" in result
    else:
        assert result.startswith(
            f"Found matches for /NEEDLE/ in {tmp_path}, but every matching line is too long to show\n\n"
        )
        assert "short.js" not in result
    assert "x" * 1000 not in result
    assert result.endswith("[Matching lines too long to show (over 1 MiB of search output each): bundle.js:1]")


@pytest.fixture
def small_records(monkeypatch: pytest.MonkeyPatch) -> int:
    """Cut records past 4 KiB, so a test line of 8 KiB is too long to keep."""
    monkeypatch.setattr(search, "_MAX_RECORD_BYTES", 4096)
    monkeypatch.setattr(search, "_RECORD_HEAD_BYTES", 1024)
    return 8192


@pytest.mark.parametrize("glob", [None, "*.txt"])
async def test_matches_too_long_to_keep_fill_the_result_so_their_context_stays_bounded(
    tmp_path: Path, small_records: int, glob: str | None
) -> None:
    long_match = "NEEDLE " + "x" * small_records
    (tmp_path / "wide.txt").write_text("".join(f"{long_match}\ncontext\n" for _ in range(2_000)), encoding="utf-8")

    result = await search.grep("NEEDLE", path=str(tmp_path), glob=glob, context_lines=1, max_results=1)

    assert result.startswith(f"Found matches for /NEEDLE/ in {tmp_path}, but every matching line is too long to show")
    assert "(limited to 1)" in result
    assert 0 < result.count("context") <= 2
    assert result.endswith("each): wide.txt:1]")


async def test_matches_too_long_to_keep_fill_the_result_across_file_batches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, small_records: int
) -> None:
    for name in ("a.txt", "b.txt", "c.txt"):
        (tmp_path / name).write_text(f"NEEDLE {'x' * small_records}\ncontext\n", encoding="utf-8")

    def one_file_per_batch(files: list[str], *, command: list[str]) -> Iterator[list[str]]:
        for name in sorted(files):
            yield [name]

    monkeypatch.setattr(search, "_file_batches", one_file_per_batch)
    result = await search.grep("NEEDLE", path=str(tmp_path), glob="*.txt", context_lines=1, max_results=1)

    assert result.startswith(f"Found matches for /NEEDLE/ in {tmp_path}, but every matching line is too long to show")
    assert "(limited to 1)" in result
    assert result.count("context") == 1
    assert result.endswith("each): a.txt:1]")


@pytest.mark.parametrize("glob", [None, "*.txt"])
async def test_a_line_too_long_under_one_encoding_only_is_counted_once(
    tmp_path: Path, small_records: int, glob: str | None
) -> None:
    # Its record fits as UTF-8 (3,000 bytes of text); read as GBK, the same bytes are
    # 1,500 characters of 3 UTF-8 bytes each, so the GBK pass cuts it.
    shown_once = ("NEEDLE " + "中" * 1_000).encode("utf-8")
    (tmp_path / "a.txt").write_bytes(shown_once + b"\n" + "中文 GBK only".encode("gbk") + b"\n")

    result = await search.grep("NEEDLE|中文", path=str(tmp_path), glob=glob, context_lines=0, max_results=2)

    assert result.startswith(f"Found 2 match(es) in {tmp_path}")
    assert "1 | NEEDLE 中中" in result and "2 | 中文 GBK only" in result
    assert "too long to show" not in result


@pytest.mark.parametrize("chunk_size", [1, 7, 1 << 20])
def test_a_match_too_long_to_keep_is_named_by_its_file_and_line(
    tmp_path: Path, small_records: int, chunk_size: int
) -> None:
    # The line itself spells a record's line number; JSON escapes its quotes, so it is never read as one.
    line = "NEEDLE " + "x" * small_records + '","line_number":7,\n'
    record = {
        "type": "match",
        "data": {
            "path": {"text": "a.txt"},
            "lines": {"text": line},
            "line_number": 12345,
            "absolute_offset": 0,
            "submatches": [{"match": {"text": "NEEDLE"}, "start": 0, "end": 6}],
        },
    }
    output = json.dumps(record, separators=(",", ":")).encode() + b"\n"
    stream = search._GrepStream(str(tmp_path), 50)

    for start in range(0, len(output), chunk_size):
        stream.feed(output[start : start + chunk_size])

    assert stream.result.oversized == {("a.txt", 12345)}
    assert stream.result.match_count == 1 and not stream.result.entries


def test_the_too_long_note_names_ten_lines_in_order_and_counts_the_rest() -> None:
    lines = {("b.js", line) for line in range(1, 7)} | {("a.js", line) for line in (10, 2, 3, 4, 5, 6)}

    note = search._oversized_note(lines)

    assert note.endswith(
        "each): a.js:2, a.js:3, a.js:4, a.js:5, a.js:6, a.js:10, b.js:1, b.js:2, b.js:3, b.js:4 and 2 more line(s)]"
    )


async def test_a_globbed_file_keeps_its_too_long_match_note_when_its_later_matches_fill_the_result(
    tmp_path: Path, small_records: int
) -> None:
    (tmp_path / "mixed.js").write_text(f"NEEDLE {'x' * small_records}\nNEEDLE one\nNEEDLE two\n", encoding="utf-8")

    result = await search.grep("NEEDLE", path=str(tmp_path), glob="*.js", context_lines=0, max_results=2)

    assert result.startswith(f"Found 1 match(es) in {tmp_path} (limited to 2)")
    assert "mixed.js:2" in result and "NEEDLE two" not in result
    assert result.endswith("each): mixed.js:1]")


async def test_a_globbed_file_builds_only_the_matches_that_could_fill_the_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "dense.txt").write_text("NEEDLE\n" * 5_000, encoding="utf-8")
    real_rel_path = search._GrepStream._rel_path
    built: list[dict[str, Any]] = []

    def counting_rel_path(self: search._GrepStream, path_data: dict[str, Any]) -> str:
        built.append(path_data)
        return real_rel_path(self, path_data)

    monkeypatch.setattr(search._GrepStream, "_rel_path", counting_rel_path)

    result = await search.grep("NEEDLE", path=str(tmp_path), glob="*.txt", context_lines=0, max_results=3)

    assert result.startswith(f"Found 3 match(es) in {tmp_path} (limited to 3)")
    # The file's matches wait for its end record; once one more than the result shows waits, the rest are dropped unbuilt.
    assert len(built) == 4


@pytest.mark.parametrize("tool", ["grep", "glob"])
async def test_a_file_listing_past_its_limit_is_refused_as_too_broad(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tool: str
) -> None:
    for index in range(40):
        (tmp_path / f"module_{index:02}.py").write_text("NEEDLE\n", encoding="utf-8")
    monkeypatch.setattr(search, "_MAX_LISTING_BYTES", 256)
    metadata: dict[str, object] = {}
    token = tool_result_metadata.set(metadata)
    try:
        result = (
            await search.grep("NEEDLE", path=str(tmp_path), glob="*.py")
            if tool == "grep"
            else await search.glob("*.py", path=str(tmp_path))
        )
    finally:
        tool_result_metadata.reset(token)

    assert result.startswith(f"Error: too many files under {tmp_path} to list")
    assert result.endswith(" — search a narrower path")
    assert "module_" not in result
    assert metadata[TOOL_ERROR_KIND_METADATA_KEY] == "search_too_broad"
