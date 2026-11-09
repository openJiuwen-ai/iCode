# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import contextlib
import shutil
import subprocess
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from chrys.foundation.reporting.diagnostics import ReportDiagnostics
from chrys.foundation.reporting.frames import Frame, decode_frames
from chrys.foundation.reporting.wal import DurableWAL


@pytest.fixture(autouse=True)
def clean_reporting_state() -> Iterator[None]:
    """Reset the process-wide reporting handles around every test.

    ``startup``'s armed configuration, ``hooks``' installed runtime and the
    atexit registration are process globals: without this, one test's runtime
    leaks into the next and a test's atexit hook outlives the worker.
    """
    from chrys.service.reporting import hooks, startup

    startup.clear_armed()
    hooks.clear_reporting()
    startup.reset_atexit_registration()
    yield
    service = hooks.clear_reporting()
    startup.clear_armed()
    if service is not None:
        # A test that armed a real runtime and never stopped it must not leak
        # its sender thread or hold the queue's files open into other tests.
        with contextlib.suppress(Exception):
            service.shutdown_sync(timeout=0.5)


@pytest.fixture
def queue(tmp_path: Path) -> Iterator[DurableWAL]:
    """An open queue in this test's own directory."""
    wal = DurableWAL(tmp_path / "reporting", queue_bytes_max=8 << 20, diagnostics=ReportDiagnostics())
    wal.open()
    try:
        yield wal
    finally:
        wal.close()


@pytest.fixture
def git_run() -> Callable[..., None]:
    """Run one git command inside a test repository.

    The fixtures' repositories carry their committer identity repo-locally, so
    nothing here reads the developer's own configuration.
    """

    def _run(repo: Path, *args: str) -> None:
        _git(repo, *args)

    return _run


@pytest.fixture
def network_repo(git_repo_factory: Callable[[Path], Path], tmp_path: Path, git_run: Callable[..., None]) -> Path:
    """A git repository whose remote the registry row can be keyed on.

    ``git_repo_factory``'s repositories have no remote at all, which is a
    workspace the reporter deliberately says nothing about.
    """
    repo = git_repo_factory(tmp_path / "repo")
    git_run(repo, "remote", "add", "origin", "https://example.com/owner/repo.git")
    return repo


def _git(repo: Path, *args: str) -> None:
    executable = shutil.which("git")
    if executable is None:  # the repository fixture this depends on needs it too
        pytest.skip("git executable is not available")
    subprocess.run([executable, *args], cwd=repo, check=True, capture_output=True)


@pytest.fixture
def queued() -> Callable[[DurableWAL], list[Frame]]:
    """Read every frame this queue currently holds."""

    def _read(wal: DurableWAL) -> list[Frame]:
        batch = wal.next_batch(max_bytes=8 << 20)
        if batch is None:
            return []
        return decode_frames(wal.read(batch))

    return _read
