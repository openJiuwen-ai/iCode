# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Diagnostics cross the worker/runner/store boundary without changing failure facts."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import create_autospec

import pytest

import chrys.service.workflows.store as store_module
from chrys.orchestration.workflows.preview import WorkflowPreviewError
from chrys.service.llm.mock import MockChatClient
from chrys.service.workflows.artifacts import session_runs
from chrys.service.workflows.store import read_node_diagnostics
from tests.orchestration.workflows._hosting import confirm, make_host, make_workdir, patch_runtime, run, write_workflow
from tests.support.workflow_workers import python_workflow

BASE = """
from chrys.workflows import WorkflowBuilder
def same(text):
    print('body output')
    return text
def broken(value):
    raise ValueError('evaluation broke')
wf = WorkflowBuilder('diagnostics')
"""


@pytest.mark.parametrize("kind", ["success", "body", "edge", "switch", "loop", "combine"])
async def test_worker_diagnostics_reach_attempt_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    if kind in {"success", "body"}:
        end = "return text" if kind == "success" else "raise ValueError('body broke')"
        source = python_workflow(f"def fn(text):\n    print('body output')\n    {end}\n", "fn")
        node = "fn"
    elif kind == "loop":
        source = (
            BASE
            + """
def body(scope):
    node = scope.python('fn', same)
    return node, node
node = wf.loop('L', body, until=broken, max_iterations=2)
wf.start(node)
wf.output(node)
workflow = wf.build()
"""
        ).encode()
        node = "L"
    else:
        operation = {
            "edge": "wf.edge(a, b, when=broken)\nwf.output(b)",
            "switch": "wf.switch(a, cases=[(broken, b)], default=c)\nwf.output(b)\nwf.output(c)",
            "combine": "wf.edge(a, b)\nc = wf.python('c', same)\nwf.join([a, b], c, combine=broken)\nwf.output(c)",
        }[kind]
        source = (
            BASE
            + "a = wf.python('a', same)\nb = wf.python('b', same)\n"
            + ("c = wf.python('c', same)\n" if kind == "switch" else "")
            + operation
            + "\nwf.start(a)\nworkflow = wf.build()\n"
        ).encode()
        node = "join:c" if kind == "combine" else "a"
    patch_runtime(monkeypatch, [MockChatClient(responses=[])])
    project = make_workdir(tmp_path)
    write_workflow(project, "diagnostics", source)
    host = make_host(tmp_path, project=project)
    try:
        await confirm(host, "diagnostics")
        result, _events = await run(host, "diagnostics", input_text="hello")
        assert result.outcome.value == ("completed" if kind == "success" else "node_failed")
        session_dir = host.workflow_session_dir
        assert session_dir is not None
        directory = session_runs(session_dir)[0].directory
        record = read_node_diagnostics(directory, f"{node}@iter#1", 1)
        assert record is not None
        phases = record["phases"]
        if kind in {"success", "body", "edge", "switch"}:
            assert phases[0]["phase"] == "body"
            assert phases[0]["stdout"] == {"text": "body output\n", "truncated": False}
        if kind == "success":
            assert not phases[0]["traceback"]
        else:
            expected_phase = {
                "body": "body",
                "edge": "outgoing",
                "switch": "outgoing",
                "loop": "until",
                "combine": "combine",
            }[kind]
            assert phases[-1]["phase"] == expected_phase
            assert "Traceback (most recent call last)" in phases[-1]["traceback"]
            assert "ValueError:" in phases[-1]["traceback"]
            assert "Traceback" not in result.error
    finally:
        await host.shutdown()


@pytest.mark.parametrize("fails", [False, True])
async def test_diagnostics_write_failure_does_not_change_run_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fails: bool
) -> None:
    patch_runtime(monkeypatch, [MockChatClient(responses=[])])
    project = make_workdir(tmp_path)
    end = "raise ValueError('boom')" if fails else "return 'done'"
    write_workflow(project, "diag", python_workflow(f"def fn(text):\n    print('hello')\n    {end}\n", "fn"))
    original = store_module.atomic_write_owner_only_bytes

    def write(path: Path, payload: bytes, *, create_parents: bool = True) -> None:
        if ".diagnostics." in path.name:
            raise OSError("diagnostics disk failure")
        original(path, payload, create_parents=create_parents)

    monkeypatch.setattr(store_module, "atomic_write_owner_only_bytes", create_autospec(original, side_effect=write))
    host = make_host(tmp_path, project=project)
    try:
        await confirm(host, "diag")
        result, _events = await run(host, "diag")
        assert result.outcome.value == ("node_failed" if fails else "completed")
        assert result.error == ("ValueError: boom" if fails else "")
    finally:
        await host.shutdown()


async def test_preview_error_keeps_load_output_and_traceback(tmp_path: Path) -> None:
    project = make_workdir(tmp_path)
    write_workflow(project, "broken", b"print('loading')\nraise ValueError('load broke')\n")
    host = make_host(tmp_path, project=project)
    try:
        with pytest.raises(WorkflowPreviewError) as failure:
            await host.preview_workflow(host.workflow_target("broken"), trust=True)
        assert failure.value.stdout.text == "loading\n"
        assert not failure.value.stdout.truncated
        assert "ValueError: load broke" in failure.value.traceback
        assert failure.value.message == "load_failed: ValueError: load broke"
    finally:
        await host.shutdown()
