# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Reused previews must stand for exactly the preview a test would get, or step aside."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Literal
from unittest.mock import create_autospec

import pytest

from chrys.orchestration.workflows import preview as preview_module
from chrys.orchestration.workflows.preview import WorkflowPreview
from chrys.orchestration.workflows.worker_client import CapturedOutput, LoadResult, WorkflowWorkerClient
from chrys.service.workflows import environment as environment_module
from chrys.service.workflows.discovery import (
    SOURCE_KIND_BUILTIN,
    SOURCE_KIND_WORKDIR,
    WorkflowPackage,
    WorkflowSource,
)
from chrys.service.workflows.environment import PreparedEnvironment
from chrys.service.workflows.sdk_artifact import SdkArtifact
from tests.support import workflow_previews
from tests.support.workflow_previews import reused_preview_workflow

_SDK = SdkArtifact(path=Path("sdk"), digest="sdk-digest")
_CACHE = Path("/pycache")


def _source(kind: str = SOURCE_KIND_WORKDIR, body: bytes = b"workflow = 1\n") -> WorkflowSource:
    return WorkflowSource("wf", kind, "/workflows/wf.py", body)


def _environment(mode: Literal["default", "byo"]) -> PreparedEnvironment:
    return PreparedEnvironment(mode, "python", "cpython", "3.14.0", "linux", "x86_64", "glibc", "sdk-digest", "fp")


class _RealPreview:
    """Stands in for the real pipeline and records every run of it."""

    def __init__(self, mode: Literal["default", "byo"] = "default") -> None:
        self.runs: list[tuple[WorkflowSource, Path]] = []
        self.caches: list[Path] = []
        self.mode: Literal["default", "byo"] = mode

    async def __call__(
        self,
        source: WorkflowSource,
        *,
        sdk: SdkArtifact,
        workspace: Path,
        bytecode_cache: Path,
        on_environment_ready: Callable[[PreparedEnvironment], Awaitable[None]] | None = None,
    ) -> WorkflowPreview:
        self.runs.append((source, workspace))
        self.caches.append(bytecode_cache)
        environment = _environment(self.mode)
        if on_environment_ready is not None:
            await on_environment_ready(environment)
        manifest = {"title": "T", "nodes": [{"id": "a"}]}
        load = LoadResult("entry", "manifest", manifest, CapturedOutput("", truncated=False))
        return WorkflowPreview(source, environment, load, manifest, "spec")


@pytest.fixture
def real_preview(monkeypatch: pytest.MonkeyPatch) -> _RealPreview:
    real = _RealPreview()
    monkeypatch.setattr(workflow_previews, "_REAL_PREVIEW", real)
    monkeypatch.setattr(workflow_previews, "_KEPT", {})
    return real


async def test_a_repeated_preview_runs_once_and_still_reports_its_environment(real_preview: _RealPreview) -> None:
    ready: list[PreparedEnvironment] = []

    async def environment_ready(environment: PreparedEnvironment) -> None:
        ready.append(environment)

    source = _source()
    first = await reused_preview_workflow(
        source, sdk=_SDK, workspace=Path("/w"), bytecode_cache=_CACHE, on_environment_ready=environment_ready
    )
    second = await reused_preview_workflow(
        _source(), sdk=_SDK, workspace=Path("/w"), bytecode_cache=_CACHE, on_environment_ready=environment_ready
    )

    assert real_preview.runs == [(source, Path("/w"))]
    assert real_preview.caches == [_CACHE]
    assert second == first and second is not first
    assert ready == [first.environment, first.environment]
    # Each caller owns its manifest, which is also the one its load result holds.
    second.manifest["nodes"].append({"id": "b"})
    assert first.manifest == {"title": "T", "nodes": [{"id": "a"}]}
    assert second.load.manifest is second.manifest
    third = await reused_preview_workflow(_source(), sdk=_SDK, workspace=Path("/w"), bytecode_cache=_CACHE)
    assert third.manifest == {"title": "T", "nodes": [{"id": "a"}]}


async def test_a_declined_environment_still_fails_a_reused_preview(real_preview: _RealPreview) -> None:
    await reused_preview_workflow(_source(), sdk=_SDK, workspace=Path("/w"), bytecode_cache=_CACHE)

    async def decline(environment: PreparedEnvironment) -> None:
        raise PermissionError(environment.environment_fingerprint)

    with pytest.raises(PermissionError, match="fp"):
        await reused_preview_workflow(
            _source(), sdk=_SDK, workspace=Path("/w"), bytecode_cache=_CACHE, on_environment_ready=decline
        )
    assert len(real_preview.runs) == 1


async def test_other_bytes_sdk_or_workspace_preview_again(real_preview: _RealPreview) -> None:
    await reused_preview_workflow(_source(), sdk=_SDK, workspace=Path("/w"), bytecode_cache=_CACHE)
    await reused_preview_workflow(
        _source(body=b"workflow = 2\n"), sdk=_SDK, workspace=Path("/w"), bytecode_cache=_CACHE
    )
    await reused_preview_workflow(
        _source(), sdk=SdkArtifact(path=Path("sdk"), digest="other"), workspace=Path("/w"), bytecode_cache=_CACHE
    )
    await reused_preview_workflow(_source(), sdk=_SDK, workspace=Path("/elsewhere"), bytecode_cache=_CACHE)

    assert len(real_preview.runs) == 4


async def test_a_folder_with_other_covered_files_previews_again(real_preview: _RealPreview) -> None:
    def folder(digest: str) -> WorkflowSource:
        package = WorkflowPackage("/workflows/wf", digest, file_count=2, total_bytes=20)
        return WorkflowSource("wf", SOURCE_KIND_WORKDIR, "/workflows/wf/wf.py", b"workflow = 1\n", package)

    await reused_preview_workflow(folder("one"), sdk=_SDK, workspace=Path("/w"), bytecode_cache=_CACHE)
    await reused_preview_workflow(folder("one"), sdk=_SDK, workspace=Path("/w"), bytecode_cache=_CACHE)
    await reused_preview_workflow(folder("two"), sdk=_SDK, workspace=Path("/w"), bytecode_cache=_CACHE)

    assert [source.package for source, _ in real_preview.runs] == [folder("one").package, folder("two").package]


async def test_a_builtin_is_reused_across_workspaces(real_preview: _RealPreview) -> None:
    builtin = _source(SOURCE_KIND_BUILTIN)
    await reused_preview_workflow(builtin, sdk=_SDK, workspace=Path("/w"), bytecode_cache=_CACHE)
    await reused_preview_workflow(
        _source(SOURCE_KIND_BUILTIN), sdk=_SDK, workspace=Path("/elsewhere"), bytecode_cache=_CACHE
    )

    assert real_preview.runs == [(builtin, Path("/w"))]


async def test_a_bring_your_own_interpreter_is_never_reused(monkeypatch: pytest.MonkeyPatch) -> None:
    real = _RealPreview("byo")
    monkeypatch.setattr(workflow_previews, "_REAL_PREVIEW", real)
    monkeypatch.setattr(workflow_previews, "_KEPT", {})
    await reused_preview_workflow(_source(), sdk=_SDK, workspace=Path("/w"), bytecode_cache=_CACHE)
    await reused_preview_workflow(_source(), sdk=_SDK, workspace=Path("/w"), bytecode_cache=_CACHE)

    assert len(real.runs) == 2


@pytest.mark.parametrize(
    ("owner", "name"),
    [
        (preview_module, "load_workflow"),
        (preview_module, "prepare_workflow_environment"),
        (WorkflowWorkerClient, "launch"),
        (environment_module, "probe_interpreter"),
    ],
    ids=["load", "environment", "launch", "probe"],
)
async def test_a_replaced_pipeline_step_gets_the_real_preview_and_is_not_kept(
    real_preview: _RealPreview, monkeypatch: pytest.MonkeyPatch, owner: object, name: str
) -> None:
    await reused_preview_workflow(_source(), sdk=_SDK, workspace=Path("/w"), bytecode_cache=_CACHE)
    with monkeypatch.context() as patch:
        # A test that stalls, fails or counts a step must reach its stand-in every time.
        patch.setattr(owner, name, create_autospec(getattr(owner, name)))
        await reused_preview_workflow(_source(), sdk=_SDK, workspace=Path("/w"), bytecode_cache=_CACHE)
        await reused_preview_workflow(
            _source(body=b"workflow = 2\n"), sdk=_SDK, workspace=Path("/w"), bytecode_cache=_CACHE
        )
    assert len(real_preview.runs) == 3
    # Nothing previewed under the replacement was kept.
    await reused_preview_workflow(
        _source(body=b"workflow = 2\n"), sdk=_SDK, workspace=Path("/w"), bytecode_cache=_CACHE
    )
    assert len(real_preview.runs) == 4
