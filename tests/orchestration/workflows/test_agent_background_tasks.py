# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""A command an agent node moved to the background stops when the node's attempt ends, not with the run.

Its start and end are recorded on the workflow session's trajectory.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from chrys.foundation.platform import get_platform
from chrys.foundation.trajectory.event_types import EventType
from chrys.foundation.trajectory.reader import read_trajectory
from chrys.service.llm.mock import MockChatClient, MockResponse
from chrys.service.trajectory.session import trajectory_events_path
from tests.orchestration.workflows._hosting import (
    confirm,
    make_host,
    make_profile,
    make_workdir,
    patch_runtime,
    run,
    write_workflow,
)

pytestmark = pytest.mark.asyncio

_WORKFLOW = (
    b"from chrys.workflows import WorkflowBuilder\n"
    b"wf = WorkflowBuilder('serve')\n"
    b"serve = wf.agent('serve', profile='Headless')\n"
    b"wf.start(serve)\nwf.output(serve)\nworkflow = wf.build()\n"
)


async def test_a_nodes_background_command_stops_when_its_attempt_ends(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # PowerShell runs a quoted program path only through its call operator.
    prefix = "& " if sys.platform == "win32" else ""
    command = f'{prefix}"{sys.executable}" -c "import time; time.sleep(60)"'
    shell = (get_platform().shell.name, "call-1", {"command": command, "reason": "serve", "timeout": 1})
    client = MockChatClient(responses=[MockResponse(tool_calls=[shell]), MockResponse(text="served")])
    patch_runtime(monkeypatch, [MockChatClient(responses=[]), client], builtin_tools=True)
    project = make_workdir(tmp_path)
    write_workflow(project, "serve", _WORKFLOW)
    host = make_host(tmp_path, project=project, profiles=[make_profile(builtins=["shell"])])
    try:
        await confirm(host, "serve")
        result, _events = await run(host, "serve", input_text="Start the server.")
        session_dir = host.workflow_session_dir
    finally:
        await host.shutdown()

    assert result.outcome.value == "completed"
    [log] = (tmp_path / "sessions").rglob("background/*.log")
    assert log.read_text(encoding="utf-8").endswith("[icode] process stopped when its agent finished\n")
    assert session_dir is not None
    trace = read_trajectory(trajectory_events_path(session_dir))
    recorded = [event.event_type for event in trace.events if event.event_type.startswith("background.")]
    assert recorded == [EventType.BACKGROUND_TASK_STARTED, EventType.BACKGROUND_TASK_FINISHED]
