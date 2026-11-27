# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Simulate Windows sharing conflicts in filesystem retry tests."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType

import pytest

from chrys.foundation.platform import files


@dataclass
class LockedRename:
    """What the blocked rename or delete saw: each attempt's target and each retry sleep."""

    attempts: list[str] = field(default_factory=list)
    sleeps: list[float] = field(default_factory=list)


def _retry_as_on(monkeypatch: pytest.MonkeyPatch, record: LockedRename, *, windows: bool) -> None:
    """Let the retry helper wait for held files when *windows* is true, recording its sleeps instead of sleeping."""
    fake_time = ModuleType("time")
    fake_time.sleep = record.sleeps.append  # type: ignore[attr-defined]
    monkeypatch.setattr(files, "_retries_held_files", lambda: windows)
    monkeypatch.setattr(files, "time", fake_time)


def briefly_locked_rename(monkeypatch: pytest.MonkeyPatch, *, failures: int, windows: bool = True) -> LockedRename:
    """Make ``os.replace`` raise ``PermissionError`` *failures* times, then rename.

    The retry helper waits for a held target when *windows* is true, and
    records its sleeps instead of sleeping.
    """
    real_replace = os.replace
    record = LockedRename()

    def replace(source: str | os.PathLike[str], target: str | os.PathLike[str]) -> None:
        record.attempts.append(os.fspath(target))
        if len(record.attempts) <= failures:
            raise PermissionError("the target is open in another process")
        real_replace(source, target)

    _retry_as_on(monkeypatch, record, windows=windows)
    monkeypatch.setattr(files.os, "replace", replace)
    return record


def briefly_held_unlink(
    monkeypatch: pytest.MonkeyPatch, target: Path, *, failures: int, windows: bool = True
) -> LockedRename:
    """Make deleting *target* raise ``PermissionError`` *failures* times, as while a reader holds it on Windows.

    Every other delete goes through. The retry helper waits for a held file
    when *windows* is true, and records its sleeps instead of sleeping.
    """
    real_unlink = os.unlink
    held = os.fspath(target)
    record = LockedRename()

    def unlink(path: str | os.PathLike[str], *, dir_fd: int | None = None) -> None:
        if dir_fd is None and os.fspath(path) == held:
            record.attempts.append(held)
            if len(record.attempts) <= failures:
                raise PermissionError("the file is open in another process")
        real_unlink(path, dir_fd=dir_fd)

    _retry_as_on(monkeypatch, record, windows=windows)
    monkeypatch.setattr(files.os, "unlink", unlink)
    return record


def briefly_held_read(
    monkeypatch: pytest.MonkeyPatch, target: Path, *, failures: int, windows: bool = True
) -> LockedRename:
    """Block a binary read open as Windows does when another handle denies read sharing."""
    real_open = Path.open
    record = LockedRename()

    def open_file(path, mode="r", buffering=-1, encoding=None, errors=None, newline=None):
        if path == target and mode == "rb":
            record.attempts.append(os.fspath(path))
            if len(record.attempts) <= failures:
                raise PermissionError("another handle denies read sharing")
        return real_open(path, mode, buffering, encoding, errors, newline)

    _retry_as_on(monkeypatch, record, windows=windows)
    monkeypatch.setattr(Path, "open", open_file)
    return record
