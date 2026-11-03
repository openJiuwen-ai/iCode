# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for ``chrys workflow``: arguments, listings, error mapping, and the headless loop end to end."""

from __future__ import annotations

import asyncio
import io
import json
import re
import sys
import time
from collections.abc import AsyncIterator, Callable, Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar
from unittest.mock import create_autospec

import psutil
import pytest

import chrys.orchestration.workflows.catalog as catalog_module
from chrys.app.cli import headless
from chrys.app.cli import workflow as workflow_cli
from chrys.foundation.config.settings import Settings
from chrys.foundation.config.settings_store import LoadedSettings
from chrys.foundation.events.types import (
    WORKFLOW_NOTICE_DATA_DROPPED,
    ApprovalRequest,
    ApprovalResponse,
    Event,
    InvocationCompactionFinished,
    InvocationContentDelta,
    InvocationContentFinished,
    Warning,
    WorkflowNodeStateChanged,
    WorkflowRunAccepted,
    WorkflowRunNotice,
    WorkflowRunRejected,
    WorkflowRunStarted,
)
from chrys.foundation.i18n import Localizer
from chrys.foundation.models.invocations import InvocationOrigin
from chrys.foundation.models.workflow_session import WorkflowIdentity, WorkflowSessionSelection, WorkspaceSnapshot
from chrys.orchestration.session_host import (
    ChrysSessionHost,
    WorkflowRunRejectedError,
    WorkflowRunTimeoutError,
)
from chrys.orchestration.workflows.catalog import WorkflowNotFoundError
from chrys.orchestration.workflows.runner import WorkflowRunResult
from chrys.orchestration.workflows.worker_client import WorkflowWorkerClient
from chrys.service.approval.policy import ApprovalMode
from chrys.service.llm.mock import MockChatClient, MockResponse
from chrys.service.profiles.agents.schema import AgentProfile
from chrys.service.state.store import JsonFileStateStore
from chrys.service.workflows.discovery import (
    SOURCE_KIND_BUILTIN,
    SOURCE_KIND_PROJECT,
    Discovery,
    SkippedSource,
    WorkflowSource,
)
from chrys.service.workflows.layout import run_dir
from chrys.service.workflows.orphans import read_run_terminal
from chrys.service.workflows.outcomes import RunOutcome
from chrys.service.workflows.protocol import LIMITS
from chrys.service.workflows.scheduler import RunOutput
from chrys.service.workflows.sdk import WorkflowValue
from tests.orchestration.workflows._hosting import (
    PROFILE,
    confirm,
    hold_workflow_deadline,
    make_host,
    make_profile,
    make_project,
    patch_runtime,
    run,
    write_workflow,
    write_workflow_package,
)
from tests.support.streams import FailingTextStream
from tests.support.waiting import ENGINE_TURN_TIMEOUT, wait_for
from tests.support.workflow_workers import CONDITIONAL_LOOP_WORKFLOW, python_workflow

TITLE = "Workflow Demo · Project Tour"

CHAIN = python_workflow(
    "def upper(text):\n    return text.text.upper()\ndef exclaim(text):\n    return text.text + '!'\n",
    "upper",
    "exclaim",
)
SLEEPER = python_workflow("import asyncio\nasync def fn(value, ctx):\n    await asyncio.sleep(3600)\n", "fn")
EMITS = python_workflow("def fn(value, ctx):\n    ctx.emit('halfway')\n    return 'done'\n", "fn")


@pytest.fixture(autouse=True)
def _stub_runtime(monkeypatch: pytest.MonkeyPatch) -> Callable[..., headless.PreparedRuntime]:
    """The environment bootstrap mutates process-global state; the CLI gets plain settings instead."""

    def prepare(*, restoring_session: bool = False) -> headless.PreparedRuntime:
        return headless.PreparedRuntime(
            LoadedSettings(settings=Settings(model_profile="mock-profile"), provenance={}), Localizer("en"), []
        )

    original = headless.prepare_runtime
    monkeypatch.setattr(headless, "prepare_runtime", create_autospec(original, side_effect=prepare))
    return original


# ---------------------------------------------------------------------------
# Arguments
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("flag", ["-h", "--help"])
def test_help_lists_every_command(flag: str, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        workflow_cli.main([flag])

    assert exc_info.value.code == 0
    out = capsys.readouterr().out
    # AIxCoding branding: workflow usage line
    assert out.startswith("usage: aixcoding-cli workflow ")
    assert "List the workflows found for the current directory" in out
    assert "Check a workflow file or folder" in out
    assert "Run one workflow until it finishes" in out


def test_run_help_shows_every_option(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        workflow_cli.main(["run", "--help"])

    out = capsys.readouterr().out
    for option in ("--input", "--session", "--trust", "--timeout", "--json"):
        assert option in out
    assert "--timeout-s" not in out


@pytest.mark.parametrize("argv", [[], ["--json"], ["--", "list", "--json"]])
def test_lists_with_no_command_or_after_the_separator(
    argv: list[str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _patch_discovery(
        monkeypatch,
        Discovery(
            sources=(_source("demo-workflow", SOURCE_KIND_BUILTIN, "/lib/builtins/demo-workflow.py"),), skipped=()
        ),
    )

    assert workflow_cli.main(argv) == 0

    out = capsys.readouterr().out
    if "--json" in argv:
        assert [row["id"] for row in json.loads(out)["workflows"]] == ["demo-workflow"]
    else:
        assert out.splitlines()[1].split()[:2] == ["demo-workflow", "builtin"]


def test_run_requires_a_workflow_id(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        workflow_cli.main(["run"])

    assert exc_info.value.code == 2
    assert "workflow_id" in capsys.readouterr().err


@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf", "x"])
def test_run_rejects_a_timeout_that_is_not_a_positive_number(value: str, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        workflow_cli.main(["run", "chain", "--timeout", value])

    assert exc_info.value.code == 2
    assert "--timeout" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


def _source(workflow_id: str, kind: str, path: str) -> WorkflowSource:
    return WorkflowSource(workflow_id=workflow_id, source_kind=kind, canonical_path=path, source=b"")


def _patch_discovery(monkeypatch: pytest.MonkeyPatch, discovery: Discovery) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def discover(**kwargs: Any) -> Discovery:
        calls.append(kwargs)
        return discovery

    monkeypatch.setattr(
        catalog_module, "discover_workflows", create_autospec(catalog_module.discover_workflows, side_effect=discover)
    )
    return calls


def test_list_prints_one_row_per_workflow(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    # Title and path fold at the terminal width; wide enough here to keep one workflow on one line.
    monkeypatch.setenv("COLUMNS", "200")
    calls = _patch_discovery(
        monkeypatch,
        Discovery(
            sources=(
                _source("chain", SOURCE_KIND_PROJECT, "/proj/.chrys/workflows/chain.py"),
                _source("demo-workflow", SOURCE_KIND_BUILTIN, "/lib/builtins/demo-workflow.py"),
            ),
            skipped=(SkippedSource("/proj/.chrys/workflows/huge.py", "too large"),),
        ),
    )

    assert workflow_cli.main(["list"]) == 0

    captured = capsys.readouterr()
    lines = [line.split() for line in captured.out.splitlines() if line.strip()]
    assert lines[0][:3] == ["ID", "Source", "Title"]
    assert lines[1] == ["chain", "project", "-", "/proj/.chrys/workflows/chain.py"]
    assert lines[2] == ["demo-workflow", "builtin", *TITLE.split(), "/lib/builtins/demo-workflow.py"]
    assert captured.err == "Warning: Skipped /proj/.chrys/workflows/huge.py: too large\n"
    assert calls == [{"config_dir": calls[0]["config_dir"], "project_cwd": Path.cwd()}]


def test_list_json(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    _patch_discovery(
        monkeypatch,
        Discovery(
            sources=(_source("demo-workflow", SOURCE_KIND_BUILTIN, "/lib/builtins/demo-workflow.py"),), skipped=()
        ),
    )

    assert workflow_cli.main(["list", "--json"]) == 0

    captured = capsys.readouterr()
    assert json.loads(captured.out) == {
        "workflows": [
            {
                "id": "demo-workflow",
                "source": "builtin",
                "layout": "file",
                "title": TITLE,
                "path": "/lib/builtins/demo-workflow.py",
            }
        ]
    }
    assert captured.err == ""


def test_list_shows_a_workflow_folder_by_its_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    project = make_project(tmp_path)
    monkeypatch.chdir(project)
    entry = write_workflow_package(project, "packaged", CHAIN, {"helpers.py": b"VALUE = 1\n"})
    write_workflow(project, "single", CHAIN)

    assert workflow_cli.main(["list", "--json"]) == 0

    captured = capsys.readouterr()
    rows = {row["id"]: row for row in json.loads(captured.out)["workflows"]}
    assert rows["packaged"] == {
        "id": "packaged",
        "source": "project",
        "layout": "package",
        "title": "",
        "path": str(entry.resolve()),
    }
    assert (rows["single"]["layout"], rows["demo-workflow"]["layout"]) == ("file", "file")
    assert captured.err == ""


def test_list_shows_names_without_terminal_controls(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("COLUMNS", "200")
    _patch_discovery(
        monkeypatch,
        Discovery(
            sources=(_source("a\x1b[2Jb", SOURCE_KIND_PROJECT, "/proj/.chrys/workflows/a\x1b[2Jb\udcff.py"),),
            skipped=(),
        ),
    )

    assert workflow_cli.main(["list"]) == 0

    out = capsys.readouterr().out
    assert "\x1b" not in out and "\udcff" not in out
    assert out.count("[2Jb") == 2


def test_list_without_workflows(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    _patch_discovery(monkeypatch, Discovery(sources=(), skipped=()))

    assert workflow_cli.main(["list"]) == 0

    assert capsys.readouterr().out == "No workflows found.\n"


# ---------------------------------------------------------------------------
# run: error mapping and reporting on a stand-in host
# ---------------------------------------------------------------------------


def _result(outcome: RunOutcome, *, reason: str = "", error: str = "", node_id: str = "") -> WorkflowRunResult:
    return WorkflowRunResult(
        run_id="run-1",
        outcome=outcome,
        outputs=(RunOutput("report", "act-1", WorkflowValue(text="the report", data={"k": 1})),),
        node_id=node_id,
        error=error,
        reason=reason,
        duration=0.5,
    )


_DURATION = re.compile(r"\b\d+\.\ds\b")
_SHORT_ID = re.compile(r"\b(run|session) [0-9a-z]{1,12}\b")


def _progress(err: str) -> list[str]:
    """Progress lines on stderr, with durations and short ids replaced by placeholders."""
    return [_SHORT_ID.sub(r"\1 <id>", _DURATION.sub("<dur>", line)) for line in err.splitlines()]


class FakeHost:
    """A ``ChrysSessionHost`` stand-in: a scripted run that either fails to start or ends with ``result``."""

    instances: ClassVar[list[FakeHost]] = []
    failure: ClassVar[Callable[[], BaseException] | None] = None
    result: ClassVar[WorkflowRunResult | None] = None
    session_dir: ClassVar[Path | None] = None
    run_events: ClassVar[list[Event]] = []
    """Events the scripted run yields after its acceptance."""
    failure_after_events: ClassVar[Callable[[], BaseException] | None] = None

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.started = False
        self.shutdown_called = False
        self.session_id = None
        self.workflow_session_id = "session-1"
        self.workflow_session_dir = type(self).session_dir
        self.loaded_workflow_session = ""
        self.run_kwargs: dict[str, Any] | None = None
        self.engine = SimpleNamespace(workflows=SimpleNamespace(result=self._result))
        FakeHost.instances.append(self)

    def _result(self, run_id: str) -> WorkflowRunResult | None:
        assert run_id == "run-1"
        return type(self).result

    async def start(self) -> None:
        self.started = True

    async def load_workflow_session(self, session_id: str) -> None:
        self.loaded_workflow_session = session_id

    def workflow_target(self, workflow_id: str) -> str:
        return workflow_id

    def iter_workflow_events(self, workflow_id: str, **kwargs: Any) -> AsyncIterator[Event]:
        self.run_kwargs = {"workflow_id": workflow_id, **kwargs}
        failure = type(self).failure
        return self._events(failure() if failure is not None else None)

    async def _events(self, failure: BaseException | None) -> AsyncIterator[Event]:
        if failure is not None:
            raise failure
        yield WorkflowRunAccepted(
            request_id="req",
            run_id="run-1",
            selection=WorkflowSessionSelection(
                self.workflow_session_id,
                WorkflowIdentity("review", "/review.py", "project"),
                WorkspaceSnapshot("/project"),
            ),
        )
        for event in type(self).run_events:
            yield event
        if (failure_after_events := type(self).failure_after_events) is not None:
            raise failure_after_events()

    async def shutdown(self) -> None:
        self.shutdown_called = True


@pytest.fixture
def fake_host(monkeypatch: pytest.MonkeyPatch) -> type[FakeHost]:
    FakeHost.instances.clear()
    FakeHost.failure = None
    FakeHost.result = None
    FakeHost.session_dir = None
    FakeHost.run_events = []
    FakeHost.failure_after_events = None
    monkeypatch.setattr(workflow_cli, "ChrysSessionHost", FakeHost)
    return FakeHost


class _Stdout(io.StringIO):
    def __init__(self, *, tty: bool) -> None:
        super().__init__()
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


@pytest.mark.parametrize("tty", [True, False])
def test_run_neutralizes_terminal_controls_in_outputs_only_on_a_terminal(
    fake_host: type[FakeHost], monkeypatch: pytest.MonkeyPatch, tty: bool
) -> None:
    text = "all:\n\tgo build\x1b[2J\x07\r\n"
    fake_host.result = WorkflowRunResult(
        run_id="run-1",
        outcome=RunOutcome.COMPLETED,
        outputs=(RunOutput("report", "act-1", WorkflowValue(text=text, data=None)),),
        node_id="",
        error="",
        reason="",
        duration=0.5,
    )
    stdout = _Stdout(tty=tty)
    monkeypatch.setattr(workflow_cli.sys, "stdout", stdout)

    assert workflow_cli.main(["run", "chain"]) == 0

    if tty:
        assert stdout.getvalue() == "all:\n\tgo build�[2J�\n"
    else:
        # Redirected output is data for another program: byte-for-byte.
        assert stdout.getvalue() == text


def test_run_passes_the_request_through_and_prints_outputs(
    fake_host: type[FakeHost], capsys: pytest.CaptureFixture[str]
) -> None:
    fake_host.result = _result(RunOutcome.COMPLETED)

    assert workflow_cli.main(["run", "chain", "--input", "hello", "--timeout", "2.5"]) == 0

    captured = capsys.readouterr()
    assert captured.out == "the report\n"
    assert _progress(captured.err) == ["• Starting workflow chain…", "✓ Workflow completed · <dur>"]
    (host,) = fake_host.instances
    assert not host.started and host.shutdown_called
    assert "session_id" not in host.kwargs
    assert host.kwargs["profile_name"] == "Code"
    assert host.run_kwargs == {
        "workflow_id": "chain",
        "input_text": "hello",
        "timeout": 2.5,
        "include_node_activity": True,
    }


def test_run_quiet_prints_only_outputs_and_warnings(
    fake_host: type[FakeHost], capsys: pytest.CaptureFixture[str]
) -> None:
    fake_host.result = _result(RunOutcome.COMPLETED)
    warning = Warning(message="node build: web tools unavailable", code="web_tools_unavailable")
    node = InvocationOrigin("workflow_node", "session-1", "inv-1", None, 1)
    fake_host.run_events = [
        warning,
        WorkflowRunStarted(run_id="run-1", workflow_id="chain"),
        WorkflowNodeStateChanged(
            run_id="run-1", node_id="build", activation_id="a1", attempt=1, state="running", invocation_id="inv-1"
        ),
        InvocationCompactionFinished(origin=node, outcome="failed", failure_reason="too big"),
        warning,
    ]

    assert workflow_cli.main(["run", "chain", "-q"]) == 0

    captured = capsys.readouterr()
    assert captured.out == "the report\n"
    # Node activity still streams so a node's own warnings reach stderr; the activity itself does not print.
    assert captured.err == (
        "Warning: node build: web tools unavailable\n  [build] Warning: compaction failed (too big)\n"
    )
    run_kwargs = fake_host.instances[0].run_kwargs
    assert run_kwargs is not None
    assert run_kwargs["include_node_activity"] is True


@pytest.mark.parametrize("quiet", [False, True])
def test_run_captured_output_precedes_the_summary_line(
    fake_host: type[FakeHost],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    quiet: bool,
) -> None:
    fake_host.result = _result(RunOutcome.COMPLETED)
    diagnostics = {
        "load": {"text": "loading\n  step 2 \x1b[2Jdone\n", "truncated": True},
        "native": {"text": "stray print"},
    }
    read = create_autospec(workflow_cli.read_run_output, return_value=diagnostics)
    monkeypatch.setattr(workflow_cli, "read_run_output", read)
    fake_host.session_dir = tmp_path

    assert workflow_cli.main(["run", "chain", *(["-q"] if quiet else [])]) == 0

    captured = capsys.readouterr()
    assert captured.out == "the report\n"
    captured_lines = [
        "Load output:",
        "loading",
        "  step 2 �[2Jdone",
        "Load output: some output was omitted by the capture limit.",
        "Output outside nodes:",
        "stray print",
    ]
    expected = (
        captured_lines if quiet else ["• Starting workflow chain…", *captured_lines, "✓ Workflow completed · <dur>"]
    )
    assert _progress(captured.err) == expected
    read.assert_called_once_with(tmp_path / "workflows" / "run-1")


def test_run_with_a_closed_stderr_still_writes_the_report(
    fake_host: type[FakeHost], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    fake_host.result = _result(RunOutcome.COMPLETED)
    read = create_autospec(workflow_cli.read_run_output, return_value={"native": {"text": "stray print"}})
    monkeypatch.setattr(workflow_cli, "read_run_output", read)
    fake_host.session_dir = tmp_path
    stderr = FailingTextStream()
    monkeypatch.setattr(sys, "stderr", stderr)

    assert workflow_cli.main(["run", "chain"]) == 0

    assert capsys.readouterr().out == "the report\n"
    assert stderr.writes == 1


def test_run_json_reports_the_full_result(fake_host: type[FakeHost], capsys: pytest.CaptureFixture[str]) -> None:
    fake_host.result = _result(RunOutcome.COMPLETED)

    assert workflow_cli.main(["run", "chain", "--json", "--session", " abc "]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["duration"] >= 0
    del payload["duration"]
    assert payload == {
        "session_id": "session-1",
        "run_id": "run-1",
        "outcome": "completed",
        "reason": "",
        "node_id": "",
        "error": "",
        "diagnostics": None,
        "outputs": [{"node_id": "report", "activation_id": "act-1", "text": "the report", "data": {"k": 1}}],
    }
    assert fake_host.instances[0].loaded_workflow_session == "abc"


@pytest.mark.parametrize(
    ("result", "exit_code", "message"),
    [
        (_result(RunOutcome.NODE_FAILED, node_id="upper", error="ValueError: boom"), 1, "failed at node 'upper'"),
        (_result(RunOutcome.LOOP_EXHAUSTED, node_id="loop"), 1, "exhausted"),
        (_result(RunOutcome.CANCELLED, reason="deadline_exceeded"), 124, "timed out"),
        (_result(RunOutcome.CANCELLED), 1, "cancelled"),
        (_result(RunOutcome.WORKER_LOST, error="exit 3"), 1, "worker was lost: exit 3"),
        (_result(RunOutcome.STORAGE_FAILED, error="disk full"), 1, "could not be written: disk full"),
    ],
    ids=["node_failed", "loop_exhausted", "deadline", "cancelled", "worker_lost", "storage_failed"],
)
def test_run_maps_every_outcome_to_an_exit_code(
    fake_host: type[FakeHost],
    capsys: pytest.CaptureFixture[str],
    result: WorkflowRunResult,
    exit_code: int,
    message: str,
) -> None:
    fake_host.result = result

    assert workflow_cli.main(["run", "chain"]) == exit_code
    captured = capsys.readouterr()
    assert captured.out == "the report\n"
    # A run that does not complete prints no success summary: the error is the last line.
    starting, error = captured.err.splitlines()
    assert starting == "• Starting workflow chain…"
    assert error.startswith("Error: ")
    assert message in error

    assert workflow_cli.main(["run", "chain", "--json"]) == exit_code
    captured = capsys.readouterr()
    assert json.loads(captured.out)["outcome"] == result.outcome.value
    error = json.loads(captured.err)
    assert (error["code"], error["session_id"]) == (result.outcome.value, "session-1")
    assert message in error["error"]


def _rejection(code: str, message: str) -> Callable[[], BaseException]:
    return lambda: WorkflowRunRejectedError(WorkflowRunRejected(request_id="req", error=code, message=message))


@pytest.mark.parametrize(
    ("failure", "code", "fragment"),
    [
        (_rejection("not_confirmed", "Workflow 'chain' is not confirmed."), "not_confirmed", "Pass --trust"),
        (_rejection("workflow_active", "A run is active."), "workflow_active", "A run is active."),
        (lambda: WorkflowNotFoundError("Workflow not found: nope"), "workflow_not_found", "Workflow not found: nope"),
        (lambda: RuntimeError("engine exploded"), "error", "engine exploded"),
    ],
    ids=["not_confirmed", "rejected", "not_found", "unexpected"],
)
def test_run_reports_failures_with_typed_codes(
    fake_host: type[FakeHost],
    capsys: pytest.CaptureFixture[str],
    failure: Callable[[], BaseException],
    code: str,
    fragment: str,
) -> None:
    fake_host.failure = failure

    assert workflow_cli.main(["run", "chain", "--quiet"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("Error: ")
    assert fragment in captured.err
    assert fake_host.instances[-1].shutdown_called

    assert workflow_cli.main(["run", "chain", "--json"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    error = json.loads(captured.err)
    assert error["code"] == code
    assert fragment in error["error"]


def test_run_reports_an_interrupt_with_exit_code_130(
    fake_host: type[FakeHost], capsys: pytest.CaptureFixture[str]
) -> None:
    fake_host.failure = KeyboardInterrupt

    assert workflow_cli.main(["run", "chain"]) == 130
    assert capsys.readouterr().err == "• Starting workflow chain…\nError: Interrupted by user.\n"
    assert fake_host.instances[-1].shutdown_called


def test_an_interrupted_run_shows_the_answer_text_its_nodes_streamed_before_the_error(
    fake_host: type[FakeHost], capsys: pytest.CaptureFixture[str]
) -> None:
    """A call cut off mid-stream ends the text before it as completed; no tool card or final answer follows."""
    node = InvocationOrigin("workflow_node", "session-1", "inv-1", None, 1)
    fake_host.run_events = [
        WorkflowRunStarted(run_id="run-1", workflow_id="chain"),
        WorkflowNodeStateChanged(
            run_id="run-1", node_id="build", activation_id="a1", attempt=1, state="running", invocation_id="inv-1"
        ),
        InvocationContentDelta(origin=node, segment_id="s1", text="Writing the fix."),
        InvocationContentFinished(origin=node, segment_id="s1"),
    ]
    fake_host.failure_after_events = KeyboardInterrupt

    assert workflow_cli.main(["run", "chain"]) == 130
    assert capsys.readouterr().err.splitlines()[-2:] == ["  [build] Writing the fix.", "Error: Interrupted by user."]


@pytest.mark.parametrize("phase", ["admission", "preview"])
def test_a_deadline_before_acceptance_reports_exit_code_124(
    fake_host: type[FakeHost], capsys: pytest.CaptureFixture[str], phase: str
) -> None:
    message = f"Workflow run timed out during {phase}."
    fake_host.failure = lambda: WorkflowRunTimeoutError(message)

    assert workflow_cli.main(["run", "chain", "--quiet"]) == 124
    assert capsys.readouterr().err == f"Error: {message}\n"
    assert workflow_cli.main(["run", "chain", "--json"]) == 124
    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err) == {"code": "deadline_exceeded", "error": message}
    assert all(host.shutdown_called for host in fake_host.instances)


# ---------------------------------------------------------------------------
# run: the headless loop on a real host
# ---------------------------------------------------------------------------


def _real_hosts(
    tmp_path: Path, project: Path, monkeypatch: pytest.MonkeyPatch, *, profiles: Sequence[AgentProfile] = ()
) -> list[ChrysSessionHost]:
    """Every host the CLI builds is a real one rooted in *project*, kept for inspection after the command."""
    hosts: list[ChrysSessionHost] = []

    def build(**kwargs: Any) -> ChrysSessionHost:
        assert "session_id" not in kwargs
        host = make_host(tmp_path, project=project, profiles=profiles)
        hosts.append(host)
        return host

    monkeypatch.setattr(workflow_cli, "ChrysSessionHost", build)
    return hosts


def test_the_headless_loop_lists_confirms_runs_and_prints_outputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    patch_runtime(monkeypatch, [MockChatClient(responses=[]) for _ in range(4)])
    project = make_project(tmp_path)
    write_workflow(project, "chain", CHAIN)
    write_workflow(project, "emits", EMITS)
    monkeypatch.chdir(project)

    assert workflow_cli.main(["list", "--json"]) == 0
    listing = json.loads(capsys.readouterr().out)
    assert [(w["id"], w["source"]) for w in listing["workflows"]] == [
        ("chain", SOURCE_KIND_PROJECT),
        ("demo-workflow", SOURCE_KIND_BUILTIN),
        ("emits", SOURCE_KIND_PROJECT),
    ]

    hosts = _real_hosts(tmp_path, project, monkeypatch)

    assert workflow_cli.main(["run", "chain", "--input", "hello"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "not confirmed" in captured.err
    assert "--trust" in captured.err

    assert workflow_cli.main(["run", "chain", "--input", "hello", "--trust"]) == 0
    captured = capsys.readouterr()
    assert captured.out == "HELLO!\n"
    assert _progress(captured.err) == [
        "• Checking workflow chain…",
        "Workflow t (chain) · run <id> · session <id>",
        "▸ [upper] running",
        "✓ [upper] completed · <dur>",
        "▸ [exclaim] running",
        "✓ [exclaim] completed · <dur>",
        "✓ Workflow completed · <dur>",
    ]

    # Confirmed once, the file runs without --trust from then on.
    assert workflow_cli.main(["run", "chain", "--input", "again", "--json"]) == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["outcome"] == "completed"
    assert [(o["node_id"], o["text"], o["data"]) for o in payload["outputs"]] == [("exclaim", "AGAIN!", None)]
    assert payload["session_id"] == hosts[-1].workflow_session_id
    assert captured.err == ""

    assert workflow_cli.main(["run", "emits", "--trust"]) == 0
    captured = capsys.readouterr()
    assert captured.out == "done\n"
    assert _progress(captured.err)[2:] == [
        "▸ [fn] running",
        "  [fn] halfway",
        "✓ [fn] completed · <dur>",
        "✓ Workflow completed · <dur>",
    ]

    assert len(hosts) == 4
    assert all(host.engine.execution().kind == "idle" for host in hosts)


def test_explicit_session_rejects_another_workflow_without_creating_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    project = make_project(tmp_path)
    write_workflow(project, "chain", CHAIN)
    write_workflow(project, "emits", EMITS)
    hosts = _real_hosts(tmp_path, project, monkeypatch)
    assert workflow_cli.main(["run", "chain", "--trust", "--json"]) == 0
    first = json.loads(capsys.readouterr().out)
    directory = hosts[0].workflow_session_dir
    assert directory is not None
    session_before = (directory / "session.json").read_bytes()
    run_before = (run_dir(directory, first["run_id"]) / "run.json").read_bytes()

    assert workflow_cli.main(["run", "emits", "--session", first["session_id"], "--trust", "--json"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err)["code"] == "spec_changed"
    assert (directory / "session.json").read_bytes() == session_before
    assert (run_dir(directory, first["run_id"]) / "run.json").read_bytes() == run_before
    assert [path.name for path in (directory / "workflows").iterdir()] == [first["run_id"]]
    assert {path.parent for path in directory.parent.glob("*/session.json")} == {directory}

    # Explicitly running the bound workflow still appends a new run to that session.
    assert workflow_cli.main(["run", "chain", "--session", first["session_id"], "--json"]) == 0
    second = json.loads(capsys.readouterr().out)
    assert second["session_id"] == first["session_id"]
    assert second["run_id"] != first["run_id"]
    assert all(host.engine.execution().kind == "idle" for host in hosts)


@pytest.mark.parametrize("interactive_mode", [ApprovalMode.MANUAL, ApprovalMode.AUTO])
async def test_cli_restored_workflow_bypasses_tool_approval_whatever_the_interactive_launch_chose(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, interactive_mode: ApprovalMode
) -> None:
    project = make_project(tmp_path)
    reference = project / "reference.txt"
    reference.write_text("workflow reference contents", encoding="utf-8")
    profile = make_profile(builtins=["filesystem.read"])
    node_client = MockChatClient(
        responses=[
            MockResponse(tool_calls=[("read_file", "read-reference", {"path": str(reference)})]),
            MockResponse(text="reviewed"),
        ]
    )
    patch_runtime(
        monkeypatch,
        [MockChatClient(responses=[]), MockChatClient(responses=[MockResponse(text="initial")]), node_client],
        builtin_tools=True,
    )
    write_workflow(
        project,
        "review",
        (
            "from chrys.workflows import WorkflowBuilder\n"
            "wf = WorkflowBuilder('review')\n"
            f"review = wf.agent('review', profile={profile.name!r})\n"
            "wf.start(review)\nwf.output(review)\nworkflow = wf.build()\n"
        ).encode(),
    )
    interactive = make_host(
        tmp_path, project=project, profiles=[profile], approval_mode=interactive_mode, allow_user_interaction=True
    )
    try:
        await confirm(interactive, "review")
        result, _ = await run(interactive, "review")
        assert result.outcome is RunOutcome.COMPLETED
        session_id = interactive.workflow_session_id
    finally:
        await interactive.shutdown()
    store = JsonFileStateStore(tmp_path / "sessions")
    # The approval mode belongs to a launch, so the checkpoint has nothing the CLI could inherit.
    before = await store.load_workflow_session(session_id)
    assert before is not None and "approval_mode" not in before.encode()

    cli_host = make_host(tmp_path, project=project, profiles=[profile], approval_mode=ApprovalMode.BYPASS)
    factory = create_autospec(ChrysSessionHost, return_value=cli_host)
    monkeypatch.setattr(workflow_cli, "ChrysSessionHost", factory)
    requests: list[ApprovalRequest] = []

    async def reject_unanswered_approval(event: ApprovalRequest) -> None:
        # Fail by evidence instead of hanging if a restored mode leaks into the CLI.
        requests.append(event)
        await cli_host.event_bus.publish(
            ApprovalResponse(request_id=event.request_id, session_id=event.session_id, approved=False)
        )

    await cli_host.event_bus.subscribe(ApprovalRequest, reject_unanswered_approval)
    try:
        args = workflow_cli.build_parser().parse_args(["run", "review", "--session", session_id])
        assert await workflow_cli._run_command(args) == 0
    finally:
        await cli_host.event_bus.unsubscribe(ApprovalRequest, reject_unanswered_approval)
        await cli_host.shutdown()
    assert factory.call_args.kwargs["approval_mode"] is ApprovalMode.BYPASS
    assert requests == []
    assert any(
        content.type == "function_result" and "workflow reference contents" in str(content.result)
        for message in node_client.call_history[1][0]
        for content in message.contents
    )
    after = await store.load_workflow_session(session_id)
    assert after is not None and "approval_mode" not in after.encode()
    assert after.run_count == 2


def test_the_builtin_demo_runs_unattended_to_a_tour(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    scan = MockChatClient(responses=[MockResponse(text="It is a CLI.")])
    writer = MockChatClient(responses=[MockResponse(text="Start at cli.py.")])
    patch_runtime(monkeypatch, [MockChatClient(responses=[]), scan, writer])
    project = make_project(tmp_path)
    hosts = _real_hosts(tmp_path, project, monkeypatch, profiles=[make_profile(), make_profile("QA")])

    # A builtin needs no confirmation. Nobody can answer a headless run, so the demo's own input line says so.
    assert (
        workflow_cli.main(["run", "demo-workflow", "--input", "interactive: false\nWhere does it start?", "--json"])
        == 0
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["outcome"] == "completed"
    assert [output["node_id"] for output in payload["outputs"]] == ["render_tour"]
    assert payload["outputs"][0]["text"] == (
        "# Project tour\n\nFocus: Where does it start?\nDepth: quick\n"
        "Status: NOT reviewed by a human (unattended run)\n\nStart at cli.py."
    )
    assert [client.call_count for client in (scan, writer)] == [1, 1]
    scan_messages, _options = scan.call_history[0]
    assert any("What they want to learn: Where does it start?" in message.text for message in scan_messages)
    assert hosts[0].engine.execution().kind == "idle"


def test_the_builtin_demo_fails_a_headless_run_that_would_have_to_ask(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    patch_runtime(monkeypatch, [MockChatClient(responses=[])])
    project = make_project(tmp_path)
    _real_hosts(tmp_path, project, monkeypatch, profiles=[make_profile(), make_profile("QA")])

    assert workflow_cli.main(["run", "demo-workflow", "--json"]) == 1

    payload = json.loads(capsys.readouterr().out)
    assert (payload["outcome"], payload["node_id"]) == ("node_failed", "choose_depth")
    assert payload["outputs"] == []


DATA_INTO_AGENT = (
    "from chrys.workflows import WorkflowBuilder, WorkflowValue\n"
    "def tag(value):\n    return WorkflowValue(text=value.text, data={'tagged': True})\n"
    "wf = WorkflowBuilder('data into agent')\n"
    "first = wf.python('tag', tag)\n"
    f"reader = wf.agent('reader', profile={PROFILE!r})\n"
    "wf.start(first)\nwf.edge(first, reader)\nwf.output(reader)\nworkflow = wf.build()\n"
).encode()


async def test_the_text_cli_keeps_data_boundary_notices_in_events_but_omits_them_from_progress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    patch_runtime(monkeypatch, [MockChatClient(responses=[]), MockChatClient(responses=[MockResponse(text="read")])])
    project = make_project(tmp_path)
    write_workflow(project, "tagged", DATA_INTO_AGENT)
    host = make_host(tmp_path, project=project)
    monkeypatch.setattr(workflow_cli, "ChrysSessionHost", create_autospec(ChrysSessionHost, return_value=host))
    events: list[WorkflowRunNotice] = []

    async def capture_notice(event: WorkflowRunNotice) -> None:
        events.append(event)

    await host.event_bus.subscribe(WorkflowRunNotice, capture_notice)
    try:
        args = workflow_cli.build_parser().parse_args(["run", "tagged", "--trust", "--input", "x"])
        assert await workflow_cli._run_command(args) == 0
    finally:
        await host.event_bus.unsubscribe(WorkflowRunNotice, capture_notice)
        await host.shutdown()

    captured = capsys.readouterr()
    assert captured.out == "read\n"
    assert [event.code for event in events] == [WORKFLOW_NOTICE_DATA_DROPPED]
    assert _progress(captured.err)[2:] == [
        "▸ [tag] running",
        "✓ [tag] completed · <dur>",
        "▸ [reader] running",
        "✓ [reader] completed · <dur>",
        "✓ Workflow completed · <dur>",
    ]
    assert events[0].message not in captured.err
    assert host.engine.execution().kind == "idle"


async def test_timeout_cancels_the_run_with_exit_code_124(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    patch_runtime(monkeypatch, [MockChatClient(responses=[])])
    project = make_project(tmp_path)
    write_workflow(project, "sleeper", SLEEPER)
    host = make_host(tmp_path, project=project)
    monkeypatch.setattr(workflow_cli, "ChrysSessionHost", create_autospec(ChrysSessionHost, return_value=host))
    expire = hold_workflow_deadline(monkeypatch, 3600)

    async def on_running(event: WorkflowNodeStateChanged) -> None:
        if event.state == "running":
            expire.set()

    await host.event_bus.subscribe(WorkflowNodeStateChanged, on_running)
    try:
        await confirm(host, "sleeper")
        args = workflow_cli.build_parser().parse_args(["run", "sleeper", "--timeout", "3600", "--json"])
        assert await workflow_cli._run_command(args) == 124
    finally:
        await host.event_bus.unsubscribe(WorkflowNodeStateChanged, on_running)
        await host.shutdown()

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert (payload["outcome"], payload["reason"], payload["outputs"]) == ("cancelled", "deadline_exceeded", [])
    error = json.loads(captured.err)
    assert (error["code"], error["error"]) == ("cancelled", "Workflow run timed out.")
    session_dir = host.workflow_session_dir
    assert session_dir is not None
    terminal = read_run_terminal(run_dir(session_dir, payload["run_id"]))
    assert terminal is not None
    assert (terminal.outcome, terminal.reason) == ("cancelled", "deadline_exceeded")
    assert host.engine.execution().kind == "idle"


@pytest.mark.parametrize("phase", ["load", "close"])
async def test_trust_preview_timeout_kills_a_blocked_worker_without_confirming(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    patch_runtime(monkeypatch, [MockChatClient(responses=[])])
    project = make_project(tmp_path)
    marker = tmp_path / "loading"
    write_workflow(
        project,
        "blocked",
        (
            "import os, threading\nfrom pathlib import Path\n"
            f"Path({str(marker)!r}).write_text(str(os.getpid()))\n"
            + ("threading.Event().wait()\n" if phase == "load" else "")
        ).encode()
        + CHAIN,
    )
    host = make_host(tmp_path, project=project)
    monkeypatch.setattr(workflow_cli, "ChrysSessionHost", create_autospec(ChrysSessionHost, return_value=host))
    confirmation = create_autospec(host.confirm_workflow, side_effect=host.confirm_workflow)
    monkeypatch.setattr(host, "confirm_workflow", confirmation)
    deadlines: list[asyncio.Timeout] = []
    real_timeout = asyncio.timeout
    real_close = WorkflowWorkerClient.close
    closing = asyncio.Event()
    close_retried = asyncio.Event()
    release_close = asyncio.Event()
    closing_clients: list[WorkflowWorkerClient] = []

    async def close(self: WorkflowWorkerClient, *, grace: float = LIMITS.shutdown_grace) -> None:
        if closing.is_set():
            close_retried.set()
        closing_clients.append(self)
        closing.set()
        await release_close.wait()
        await real_close(self, grace=grace)

    if phase == "close":
        monkeypatch.setattr(WorkflowWorkerClient, "close", close)

    def timeout(delay: float | None) -> asyncio.Timeout:
        deadline = real_timeout(delay)
        if delay == 3600:
            deadlines.append(deadline)
        return deadline

    monkeypatch.setattr(asyncio, "timeout", create_autospec(real_timeout, side_effect=timeout))
    args = workflow_cli.build_parser().parse_args(["run", "blocked", "--trust", "--timeout", "3600"])
    caller = asyncio.create_task(workflow_cli._run_command(args))
    try:
        await wait_for(
            lambda: (marker.exists() and marker.stat().st_size > 0) or caller.done(),
            description="trust preview entered module loading",
            timeout=ENGINE_TURN_TIMEOUT,
        )
        if caller.done():
            await caller
        assert marker.exists()
        worker = psutil.Process(int(marker.read_text()))
        if phase == "close":
            await wait_for(lambda: closing.is_set() or caller.done(), description="preview reached worker cleanup")
            assert closing.is_set()
        assert len(deadlines) == 1
        deadlines[0].reschedule(asyncio.get_running_loop().time())
        if phase == "close":
            await wait_for(
                lambda: close_retried.is_set() or caller.done(),
                description="expired preview still awaits worker cleanup",
            )
            assert close_retried.is_set() and not caller.done()
            release_close.set()
        await wait_for(caller.done, timeout=15, description="preview deadline finished worker and session cleanup")
        with pytest.raises(WorkflowRunTimeoutError, match="timed out during preview"):
            await caller
        assert not worker.is_running()
        confirmation.assert_not_called()
        assert host.engine.execution().kind == "idle"
    finally:
        release_close.set()
        caller.cancel()
        await asyncio.gather(caller, return_exceptions=True)
        for client in closing_clients:
            await real_close(client)
        await host.shutdown()


async def test_trust_preview_consumes_the_same_deadline_budget_as_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_runtime(monkeypatch, [MockChatClient(responses=[])])
    project = make_project(tmp_path)
    write_workflow(project, "chain", CHAIN)
    host = make_host(tmp_path, project=project)
    monkeypatch.setattr(workflow_cli, "ChrysSessionHost", create_autospec(ChrysSessionHost, return_value=host))
    events = create_autospec(host.iter_workflow_events, side_effect=host.iter_workflow_events)
    monkeypatch.setattr(host, "iter_workflow_events", events)
    clock = create_autospec(time, spec_set=True)
    clock.monotonic.side_effect = [0.0, 1.0, 2.5, 4.0]  # preview consumes 1.5 seconds of the original 25
    monkeypatch.setattr(workflow_cli, "time", clock)
    # The real preview and run still start workers under these deadlines: keep both well above a cold start.
    args = workflow_cli.build_parser().parse_args(["run", "chain", "--trust", "--timeout", "25"])
    try:
        assert await workflow_cli._run_command(args) == 0
        assert events.call_args.kwargs["timeout"] == 23.5
    finally:
        await host.shutdown()


@pytest.mark.parametrize("raw,expected", [(None, 18), ("bad", 18), ("23", 23), ("999", 50)])
@pytest.mark.parametrize("restoring", [False, True])
def test_workflow_command_uses_headless_retry_policy(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    fake_host: type[FakeHost],
    _stub_runtime: Callable[..., headless.PreparedRuntime],
    raw: str | None,
    expected: int,
    restoring: bool,
) -> None:
    from tests.app.cli.test_run import _fake_bootstrap

    if raw is None:
        monkeypatch.delenv("CHRYS_MAX_TRANSIENT_RETRIES", raising=False)
    else:
        monkeypatch.setenv("CHRYS_MAX_TRANSIENT_RETRIES", raw)
    bootstrap = create_autospec(headless.bootstrap_runtime, side_effect=_fake_bootstrap())
    monkeypatch.setattr(headless, "bootstrap_runtime", bootstrap)
    monkeypatch.setattr(headless, "prepare_runtime", _stub_runtime)
    fake_host.result = _result(RunOutcome.COMPLETED)
    assert workflow_cli.main(["run", "chain", "--json", *(["--session", "old"] if restoring else [])]) == 0
    loaded = fake_host.instances[0].kwargs["loaded_settings"]
    assert loaded.settings.frontend_default_max_transient_retries == 18
    assert loaded.settings.effective_max_transient_retries() == expected
    assert bootstrap.call_args.kwargs["project_root"] == (None if restoring else Path.cwd())
    captured = capsys.readouterr()
    if raw in ("bad", "999"):
        warning = json.loads(captured.err)
        assert warning["code"] == "invalid_max_transient_retries"
        assert "CHRYS_MAX_TRANSIENT_RETRIES=" in warning["warning"]
    else:
        assert captured.err == ""


@pytest.mark.parametrize("as_json", [False, True])
def test_trust_displays_manifest_warnings_and_list_reuses_confirmed_title(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    as_json: bool,
) -> None:
    patch_runtime(monkeypatch, [MockChatClient(responses=[])])
    project = make_project(tmp_path)
    write_workflow(project, "conditional", CONDITIONAL_LOOP_WORKFLOW)
    monkeypatch.chdir(project)
    _real_hosts(tmp_path, project, monkeypatch)
    assert workflow_cli.main(["run", "conditional", "--trust", *(["--json"] if as_json else [])]) == 0
    captured = capsys.readouterr()
    if as_json:
        warning = json.loads(captured.err)
        assert warning["code"] == "loop_exit_all_conditional"
        assert "loop_no_value" in warning["warning"]
        assert json.loads(captured.out)["outcome"] == "completed"
    else:
        assert "Warning: loop 'loop' exit 'exit' only has conditional in-edges" in captured.err
    ledger = catalog_module.WorkflowCatalog.ledger
    ledger_reads = create_autospec(ledger, side_effect=ledger)
    monkeypatch.setattr(catalog_module.WorkflowCatalog, "ledger", ledger_reads)
    # Listing can read the source and ledger but must never execute the file.
    write_workflow(project, "conditional", b"raise RuntimeError('do not execute listing')\n")
    assert workflow_cli.main(["list", "--json"]) == 0
    listing = json.loads(capsys.readouterr().out)
    row = next(row for row in listing["workflows"] if row["id"] == "conditional")
    assert row["title"] == "Conditional loop [literal]"
    assert ledger_reads.call_count == 1
