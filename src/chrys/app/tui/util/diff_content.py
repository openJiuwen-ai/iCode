# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Snapshot decoding shared by the diff viewer and its compact preview."""

from __future__ import annotations

import asyncio
import contextlib
import re
import threading
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING

from chrys.app.tui.util.diff_entries import MAX_COUNTED_LINES, DiffFileEntry
from chrys.foundation.text.encoding import EncodingDetector, decode_bytes
from chrys.service.mutations.store import SnapshotStore

if TYPE_CHECKING:
    from collections.abc import Sequence


@dataclass(frozen=True)
class DiffContent:
    before_text: str = ""
    after_text: str = ""
    is_binary: bool = False
    encoding: str = "utf-8"
    available: bool = True


def read_diff_content(store: SnapshotStore, before_hash: str | None, after_hash: str | None) -> DiffContent:
    """Decode exactly the same endpoints on every file-change surface."""
    before = store.read_blob(before_hash) if before_hash else None
    after = store.read_blob(after_hash) if after_hash else None
    available = (before_hash is None or before is not None) and (after_hash is None or after is not None)
    binary = bool(
        (before and EncodingDetector.looks_binary(before)) or (after and EncodingDetector.looks_binary(after))
    )
    encoding = "utf-8"
    sample = after or before
    if sample and not binary:
        encoding = EncodingDetector().detect_from_bytes(sample).encoding or "utf-8"
    return DiffContent(
        before_text="" if binary or before is None else decode_bytes(before),
        after_text="" if binary or after is None else decode_bytes(after),
        is_binary=binary,
        encoding=encoding,
        available=available,
    )


type _CountKey = tuple[Path | None, str | None, str | None, str, str]


def _count_key(entry: DiffFileEntry, directory: Path | None) -> _CountKey:
    return (
        directory,
        entry.before_hash,
        entry.after_hash,
        entry.before_text if entry.before_hash is None else "",
        entry.after_text if entry.after_hash is None else "",
    )


def _over_line_limit(text: str) -> bool:
    # Match the viewer's CR/LF lines, including an unterminated last line.
    # Unicode separators within a line are visible characters in the diff.
    lines = int(bool(text) and not text.endswith(("\r", "\n")))
    for _ in re.finditer(r"\r\n|\r|\n", text):
        lines += 1
        if lines > MAX_COUNTED_LINES:
            return True
    return lines > MAX_COUNTED_LINES


def compute_preview_counts(entry: DiffFileEntry, directory: Path | None) -> DiffFileEntry:
    """Read immutable endpoints and count one file, away from the UI thread."""
    from chrys.app.tui.widgets.diff_view.compute import compute_hunks
    from chrys.app.tui.widgets.diff_view.rows import change_counts

    result = replace(entry, line_count_result="unavailable")
    if entry.content_omitted or entry.is_binary:
        return result
    if entry.before_hash is not None or entry.after_hash is not None:
        if directory is None:
            return result
        content = read_diff_content(SnapshotStore(directory), entry.before_hash, entry.after_hash)
        if not content.available or content.is_binary:
            return result
        before = content.before_text if entry.before_hash is not None else entry.before_text
        after = content.after_text if entry.after_hash is not None else entry.after_text
    elif entry.before_text or entry.after_text:
        before, after = entry.before_text, entry.after_text
    else:
        return result
    if _over_line_limit(before) or _over_line_limit(after):
        return replace(result, line_count_result="large")
    return replace(result, line_count_result=change_counts(compute_hunks(before, after)))


async def count_preview_entry(entry: DiffFileEntry, directory: Path | None) -> DiffFileEntry:
    """Let cancellation/exit abandon a count without joining difflib's thread.

    Neither asyncio's default executor nor ThreadPoolExecutor(wait=False)
    suffices: both join running work at shutdown. This daemon only reads
    snapshots; the event loop alone owns cache and widget updates.
    """
    loop = asyncio.get_running_loop()
    answer: asyncio.Future[DiffFileEntry] = loop.create_future()

    def deliver(result: DiffFileEntry | Exception) -> None:
        if answer.done():
            return
        if isinstance(result, Exception):
            answer.set_exception(result)
        else:
            answer.set_result(result)

    def work() -> None:
        result: DiffFileEntry | Exception
        try:
            result = compute_preview_counts(entry, directory)
        except Exception as exc:
            result = exc
        with contextlib.suppress(RuntimeError):  # The app may already have closed its loop.
            loop.call_soon_threadsafe(deliver, result)

    threading.Thread(target=work, name="icode-preview-count", daemon=True).start()
    return await answer


class PreviewCountCache:
    """UI-thread cache of only the current immutable net-change endpoints."""

    def __init__(self) -> None:
        self._counts: dict[_CountKey, DiffFileEntry] = {}
        self.entries: tuple[DiffFileEntry, ...] = ()
        self.directory: Path | None = None

    def fill(self, entries: Sequence[DiffFileEntry], directory: Path | None) -> tuple[DiffFileEntry, ...]:
        """Publish metadata immediately, without any snapshot reads or diff work."""
        retained: dict[_CountKey, DiffFileEntry] = {}
        result: list[DiffFileEntry] = []
        for entry in entries:
            key = _count_key(entry, directory)
            cached = self._counts.get(key)
            if entry.content_omitted or entry.is_binary:
                result.append(replace(entry, line_count_result="unavailable"))
            elif cached is not None:
                retained[key] = cached
                result.append(replace(entry, line_count_result=cached.line_count_result))
            else:
                result.append(replace(entry, line_count_result="pending"))
        self._counts = retained
        self.directory = directory
        self.entries = tuple(result)
        return self.entries

    def next_pending(self) -> DiffFileEntry | None:
        """The sorted visible prefix has priority over the remaining total."""
        return next((entry for entry in self.entries if entry.line_count_result == "pending"), None)

    def accept(self, result: DiffFileEntry, directory: Path | None, *, remember: bool = True) -> bool:
        """Discard results whose directory or endpoints have left the current set."""
        key = _count_key(result, directory)
        if directory != self.directory:
            return False
        changed = False
        updated: list[DiffFileEntry] = []
        for entry in self.entries:
            if entry.line_count_result == "pending" and _count_key(entry, directory) == key:
                entry = replace(entry, line_count_result=result.line_count_result)
                changed = True
            updated.append(entry)
        if changed:
            if remember:
                self._counts[key] = result
            self.entries = tuple(updated)
        return changed
