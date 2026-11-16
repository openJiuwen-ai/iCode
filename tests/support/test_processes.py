# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""A process a test started has ended once it is gone or a zombie, even if it goes while being read.

The pid it published is read only once its file opens.
"""

from __future__ import annotations

import errno
from pathlib import Path

import psutil
import pytest

from tests.support.processes import published_pid, still_running


class _ReadProcess:
    """A process whose liveness reads answer *running* and *status*; a status of None means it is gone by then."""

    pid = 4242

    def __init__(self, *, running: bool, status: str | None) -> None:
        self._running = running
        self._status = status

    def is_running(self) -> bool:
        return self._running

    def status(self) -> str:
        if self._status is None:
            raise psutil.NoSuchProcess(self.pid)
        return self._status


@pytest.mark.parametrize(
    ("running", "status", "expected"),
    [
        (True, psutil.STATUS_RUNNING, True),
        (True, psutil.STATUS_SLEEPING, True),
        (True, psutil.STATUS_ZOMBIE, False),
        (False, psutil.STATUS_RUNNING, False),
        (True, None, False),
    ],
    ids=["running", "sleeping", "zombie", "gone", "exits-between-reads"],
)
def test_a_process_has_ended_once_gone_or_a_zombie(running: bool, status: str | None, expected: bool) -> None:
    assert still_running(_ReadProcess(running=running, status=status)) is expected  # type: ignore[arg-type]


def test_a_published_pid_is_read_only_once_its_file_opens(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    marker = tmp_path / "pid"
    assert published_pid(marker) is None
    marker.write_text("4242", encoding="utf-8")

    def held(path: Path, encoding: str | None = None, errors: str | None = None, newline: str | None = None) -> str:
        # What Windows raises while the rename that published the file still holds it.
        raise PermissionError(errno.EACCES, "Permission denied", str(path))

    with monkeypatch.context() as patch:
        patch.setattr(Path, "read_text", held)
        assert marker.exists()
        assert published_pid(marker) is None
    assert published_pid(marker) == 4242
