# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""A workflow folder's confirmation covers every file in it: runs, re-confirmation and refusals end to end."""

from __future__ import annotations

import asyncio
import os
import shutil
import stat
import sys
from pathlib import Path
from unittest.mock import create_autospec

import pytest

import chrys.orchestration.workflows.catalog as catalog_module
import chrys.orchestration.workflows.coordinator as coordinator_module
import chrys.service.workflows.environment as environment_module
from chrys.foundation.platform import get_platform
from chrys.foundation.platform.files import atomic_write_owner_only_bytes
from chrys.orchestration.session_host import WorkflowRunRejectedError
from chrys.orchestration.workflows.catalog import WorkflowCatalog
from chrys.orchestration.workflows.preview import (
    WorkflowInspection,
    WorkflowPreviewError,
    worker_bytecode_cache_dir,
)
from chrys.service.llm.mock import MockChatClient
from chrys.service.workflows.interpreter import InterpreterError
from tests.orchestration.workflows._hosting import (
    confirm,
    make_host,
    make_workdir,
    patch_runtime,
    run,
    write_workflow_package,
)
from tests.support.workflow_workers import python_workflow


def _entry(marker: Path) -> bytes:
    """An entry that records its load and answers with what its helper and data file say."""
    return python_workflow(
        "import pathlib\n"
        "from helpers import greet\n"
        f"pathlib.Path({str(marker)!r}).write_text('loaded')\n"
        "def fn(value):\n"
        "    data = pathlib.Path(__file__).with_name('data') / 'name.txt'\n"
        "    return greet(data.read_text().strip())\n",
        "fn",
    )


def _helpers(greeting: str) -> dict[str, bytes]:
    return {
        "helpers.py": f"def greet(name):\n    return {greeting!r} + ', ' + name\n".encode(),
        "data/name.txt": b"world\n",
    }


async def test_a_confirmed_folder_runs_with_its_helpers_and_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_runtime(monkeypatch, [MockChatClient(responses=[])])
    project = make_workdir(tmp_path)
    write_workflow_package(project, "pkg", _entry(tmp_path / "loaded"), _helpers("hello"))
    host = make_host(tmp_path, project=project)
    try:
        await confirm(host, "pkg")
        result, _events = await run(host, "pkg", input_text="x")
    finally:
        await host.shutdown()

    assert [(output.node_id, output.value.text) for output in result.outputs] == [("fn", "hello, world")]
    # The worker's bytecode never lands next to the source.
    assert not any(path.name == "__pycache__" for path in (project / ".chrys").rglob("*"))


async def test_previews_and_runs_keep_bytecode_only_in_the_private_cache_of_the_config_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # This suite shares one warm cache between tests; here previews and runs use the real one.
    monkeypatch.setattr(catalog_module, "worker_bytecode_cache_dir", worker_bytecode_cache_dir)
    monkeypatch.setattr(coordinator_module, "worker_bytecode_cache_dir", worker_bytecode_cache_dir)
    patch_runtime(monkeypatch, [MockChatClient(responses=[])])
    project = make_workdir(tmp_path)
    write_workflow_package(project, "pkg", _entry(tmp_path / "loaded"), _helpers("hello"))
    cache = get_platform().config_dir / "workflows" / ".pycache"
    host = make_host(tmp_path, project=project)
    try:
        await confirm(host, "pkg")
        assert list(cache.rglob("helpers.*.pyc"))
        await asyncio.to_thread(shutil.rmtree, cache)
        await run(host, "pkg", input_text="x")
    finally:
        await host.shutdown()

    assert list(cache.rglob("helpers.*.pyc"))
    if sys.platform != "win32":
        assert stat.S_IMODE(cache.stat().st_mode) == 0o700
    assert not list(project.rglob("*.pyc"))


async def test_a_helper_edit_that_keeps_its_size_and_time_runs_once_confirmed_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_runtime(monkeypatch, [MockChatClient(responses=[])])
    project = make_workdir(tmp_path)
    entry = write_workflow_package(project, "pkg", _entry(tmp_path / "loaded"), _helpers("hello"))
    helper = entry.parent / "helpers.py"
    host = make_host(tmp_path, project=project)
    try:
        await confirm(host, "pkg")
        first, _events = await run(host, "pkg", input_text="x")
        before = helper.stat()
        atomic_write_owner_only_bytes(helper, _helpers("howdy")["helpers.py"])
        # Cached bytecode is checked only against the size and whole-second time, both kept here.
        os.utime(helper, ns=(before.st_atime_ns, before.st_mtime_ns))

        await confirm(host, "pkg")
        second, _events = await run(host, "pkg", input_text="x")
    finally:
        await host.shutdown()

    assert [output.value.text for output in first.outputs] == ["hello, world"]
    assert [output.value.text for output in second.outputs] == ["howdy, world"]


async def test_a_helper_edited_after_confirmation_needs_a_new_confirmation(tmp_path: Path) -> None:
    project = make_workdir(tmp_path)
    marker = tmp_path / "loaded"
    entry = write_workflow_package(project, "pkg", _entry(marker), _helpers("hello"))
    catalog = WorkflowCatalog(config_dir=tmp_path / "config", workdir=project)
    catalog.confirm(await catalog.preview("pkg", trust=True))
    marker.unlink()
    await catalog.preview("pkg")
    marker.unlink()

    atomic_write_owner_only_bytes(entry.parent / "data" / "name.txt", b"moon\n")

    with pytest.raises(WorkflowPreviewError, match="Trust the workflow"):
        await catalog.preview("pkg")
    assert not marker.exists()


async def test_admission_refuses_a_folder_changed_since_its_confirmation_before_any_code_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_runtime(monkeypatch, [MockChatClient(responses=[])])
    project = make_workdir(tmp_path)
    marker = tmp_path / "loaded"
    entry = write_workflow_package(project, "pkg", _entry(marker), _helpers("hello"))
    host = make_host(tmp_path, project=project)
    try:
        await confirm(host, "pkg")
        marker.unlink()
        atomic_write_owner_only_bytes(entry.parent / "helpers.py", _helpers("bye")["helpers.py"])
        probe = create_autospec(
            environment_module.probe_interpreter, side_effect=InterpreterError("the unconfirmed folder was probed")
        )
        monkeypatch.setattr(environment_module, "probe_interpreter", probe)

        with pytest.raises(WorkflowRunRejectedError) as rejected:
            await run(host, "pkg", input_text="x")

        assert rejected.value.event.error == "not_confirmed"
        probe.assert_not_awaited()
        assert not marker.exists()
    finally:
        await host.shutdown()


async def test_a_helper_edited_during_confirmation_cannot_be_authorized(tmp_path: Path) -> None:
    project = make_workdir(tmp_path)
    marker = tmp_path / "loaded"
    entry = write_workflow_package(project, "pkg", _entry(marker), _helpers("hello"))
    catalog = WorkflowCatalog(config_dir=tmp_path / "config", workdir=project)

    async def approve(inspection: WorkflowInspection) -> bool:
        assert inspection.source.package is not None and inspection.source.package.file_count == 3
        atomic_write_owner_only_bytes(entry.parent / "helpers.py", _helpers("bye")["helpers.py"])
        return True

    with pytest.raises(WorkflowPreviewError, match="changed during confirmation") as raised:
        await catalog.preview("pkg", authorize=approve)

    assert raised.value.code == "spec_changed"
    assert not marker.exists()
