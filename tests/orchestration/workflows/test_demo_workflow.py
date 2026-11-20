# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The shipped demo workflow on real workers: both depths, both questions, and rounds that run out."""

from __future__ import annotations

import runpy
from pathlib import Path
from typing import Any

import pytest

from chrys.foundation.events.types import (
    Event,
    WorkflowNodeAnswer,
    WorkflowNodeAskUser,
    WorkflowNodeStateChanged,
    WorkflowRunAccepted,
    WorkflowRunNotice,
)
from chrys.foundation.models.ask_user import AskUserAnswer
from chrys.orchestration.session_host import ChrysSessionHost
from chrys.orchestration.workflows.runner import WorkflowRunResult
from chrys.service.llm.mock import MockChatClient, MockResponse
from chrys.service.workflows.discovery import BUILTIN_DIR
from chrys.workflows import WorkflowValue
from tests.orchestration.workflows._hosting import (
    confirm,
    make_host,
    make_profile,
    make_workdir,
    of_type,
    patch_runtime,
    write_workflow,
)

DEMO = "demo-workflow"
DEMO_SOURCE = BUILTIN_DIR / f"{DEMO}.py"
READERS = ("architecture", "entry_points", "conventions")


def _client(text: str) -> MockChatClient:
    return MockChatClient(responses=[MockResponse(text=text)])


def _prompt(client: MockChatClient) -> str:
    messages, _options = client.call_history[0]
    return "\n".join(message.text for message in messages)


def _instructions(client: MockChatClient) -> str:
    _messages, options = client.call_history[0]
    return str(options["instructions"])


def _populate(project: Path) -> None:
    """A small project with everything the inventory must skip next to what it must list."""
    (project / "README.md").write_text("# Sample\n\nA sample project.\n", encoding="utf-8")
    (project / "pyproject.toml").write_text("[project]\nname = 'sample'\n", encoding="utf-8")
    (project / "src" / "sample").mkdir(parents=True)
    (project / "src" / "sample" / "cli.py").write_text("def main() -> None: ...\n", encoding="utf-8")
    for skipped in (".git", "node_modules/left-pad", "tools/.venv-like"):
        (project / skipped).mkdir(parents=True)
    (project / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (project / "node_modules" / "left-pad" / "index.js").write_text("module.exports = 0\n", encoding="utf-8")
    (project / "tools" / ".venv-like" / "pyvenv.cfg").write_text("home = /usr\n", encoding="utf-8")
    (project / "tools" / ".venv-like" / "secret.py").write_text("x = 1\n", encoding="utf-8")


def _tree(project: Path) -> dict[str, bytes]:
    return {
        path.relative_to(project).as_posix(): path.read_bytes() for path in sorted(project.rglob("*")) if path.is_file()
    }


def _states(events: list[Event]) -> dict[str, list[str]]:
    states: dict[str, list[str]] = {}
    for event in of_type(events, WorkflowNodeStateChanged):
        states.setdefault(event.node_id, []).append(event.state)
    return states


async def _run(
    host: ChrysSessionHost, workflow_id: str, *, input_text: str, answers: dict[str, list[str]] | None = None
) -> tuple[WorkflowRunResult, list[Event], list[WorkflowNodeAskUser]]:
    """Run to the terminal, answering each node's questions from *answers* in order.

    Each scripted reply is the one value the dialog hands back: a clicked option's label or typed text.
    """
    events: list[Event] = []
    asks: list[WorkflowNodeAskUser] = []
    scripted = {node: list(replies) for node, replies in (answers or {}).items()}
    run_id = ""
    async for event in host.iter_workflow_events(host.workflow_target(workflow_id), input_text=input_text):
        events.append(event)
        if isinstance(event, WorkflowRunAccepted):
            run_id = event.run_id
        if isinstance(event, WorkflowNodeAskUser):
            asks.append(event)
            await host.event_bus.publish(
                WorkflowNodeAnswer(
                    run_id=run_id,
                    node_id=event.node_id,
                    activation_id=event.activation_id,
                    request_id=event.request_id,
                    answers=(AskUserAnswer(values=(scripted[event.node_id].pop(0),)),),
                )
            )
    result = host.engine.workflows.result(run_id)
    assert result is not None
    return result, events, asks


def _host(tmp_path: Path, project: Path, *, interactive: bool) -> ChrysSessionHost:
    return make_host(
        tmp_path, project=project, profiles=[make_profile(), make_profile("QA")], allow_user_interaction=interactive
    )


async def test_an_unattended_quick_tour_activates_two_agents_and_is_labelled_unreviewed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scan, writer = _client("It is a CLI."), _client("Start at src/sample/cli.py:1.")
    patch_runtime(monkeypatch, [MockChatClient(responses=[]), scan, writer])
    project = make_workdir(tmp_path)
    _populate(project)
    before = _tree(project)
    host = _host(tmp_path, project, interactive=False)
    try:
        result, events, asks = await _run(host, DEMO, input_text="interactive: false\nWhere does it start?")
    finally:
        await host.shutdown()

    assert result.outcome.value == "completed"
    assert asks == []
    # No agent is handed structured data: the state goes around every one of them on a side edge.
    assert of_type(events, WorkflowRunNotice) == []
    tour = (
        "# Project tour\n\nFocus: Where does it start?\nDepth: quick\n"
        "Status: NOT reviewed by a human (unattended run)\n\nStart at src/sample/cli.py:1."
    )
    assert [(output.node_id, output.value.text) for output in result.outputs] == [("render_tour", tour)]
    assert (scan.call_count, writer.call_count) == (1, 1)
    states = _states(events)
    skipped_nodes = ("choose_depth", "fan_out", *READERS, "merge_findings", "tour_text", "next_steps")
    assert {node: states[node] for node in skipped_nodes} == {node: ["skipped"] for node in skipped_nodes}
    # The reader's task is the focus plus a bounded inventory that leaves the skipped directories out.
    task = _prompt(scan)
    assert "What they want to learn: Where does it start?" in task
    assert "interactive:" not in task
    for listed in (
        "README.md",
        "src/",
        "    cli.py",
        "tools/",
        "Project files at the top level: pyproject.toml",
        "# Sample",
    ):
        assert listed in task
    for skipped in (".git", "HEAD", "node_modules", "left-pad", ".venv-like", "secret.py"):
        assert skipped not in task
    # The findings came back through an agent as text; the focus reached the writer on the side edge.
    assert "What they want to learn: Where does it start?" in _prompt(writer)
    assert "It is a CLI." in _prompt(writer)
    assert "read-only tour" in _instructions(scan) and "read-only tour" in _instructions(writer)
    assert _tree(project) == before


async def test_an_interactive_deep_tour_asks_twice_and_adds_next_steps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    readers = [_client("reader notes") for _ in READERS]
    writer, steps = _client("The tour."), _client("Read cli.py next.")
    patch_runtime(monkeypatch, [MockChatClient(responses=[]), *readers, writer, steps])
    project = make_workdir(tmp_path)
    _populate(project)
    host = _host(tmp_path, project, interactive=True)
    try:
        result, events, asks = await _run(
            host, DEMO, input_text="", answers={"choose_depth": ["deep"], "review": ["OK."]}
        )
    finally:
        await host.shutdown()

    assert result.outcome.value == "completed"
    assert of_type(events, WorkflowRunNotice) == []
    assert [ask.node_id for ask in asks] == ["choose_depth", "review"]
    (depth_question,) = asks[0].questions
    assert (depth_question.header, [option.label for option in depth_question.options]) == ("", ["quick", "deep"])
    assert not depth_question.multi_select
    # The review question carries the draft: the dialog is all the reviewer can see while it is open.
    assert asks[1].questions[0].question.startswith("The tour.\n\n---\n\n**Review, round 1 of 2.**")
    outputs = {output.node_id: output.value.text for output in result.outputs}
    assert list(outputs) == ["render_tour", "next_steps"]
    assert "Depth: deep\nStatus: accepted by the reader in round 1\n\nThe tour." in outputs["render_tour"]
    assert outputs["next_steps"] == "Read cli.py next."
    assert (_states(events)["hand_over"], _states(events)["quick_scan"]) == (["skipped"], ["skipped"])
    # Parallel activations take clients in any order, so a reader is identified by its instructions.
    dimensions = sorted(
        next(reader for reader in READERS if f"dimension is {reader.replace('_', ' ')}" in _instructions(client))
        for client in readers
    )
    assert dimensions == sorted(READERS)
    assert all("one of three readers" in _prompt(client) for client in readers)
    # The default join hands the writer one section per reader, in declared order.
    written = _prompt(writer)
    assert [written.index(f"## {reader}\nreader notes") for reader in READERS] == sorted(
        written.index(f"## {reader}\nreader notes") for reader in READERS
    )
    # The session appends its runtime reminder to every agent prompt; the node's input comes first.
    assert _prompt(steps).startswith("The tour.")


async def test_a_typed_depth_that_differs_from_the_label_keeps_the_quick_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scan, writer = _client("notes"), _client("The tour.")
    patch_runtime(monkeypatch, [MockChatClient(responses=[]), scan, writer])
    project = make_workdir(tmp_path)
    host = _host(tmp_path, project, interactive=True)
    try:
        result, events, asks = await _run(
            host, DEMO, input_text="", answers={"choose_depth": ["Deep"], "review": ["OK."]}
        )
    finally:
        await host.shutdown()

    # Labels match case-sensitively: "Deep" is the person's own text, so `answer.choice` is None.
    assert [ask.node_id for ask in asks] == ["choose_depth", "review"]
    states, deep_nodes = _states(events), ("fan_out", *READERS)
    assert {node: states[node] for node in deep_nodes} == {node: ["skipped"] for node in deep_nodes}
    assert (scan.call_count, writer.call_count) == (1, 1)
    (output,) = result.outputs
    assert "Depth: quick\nStatus: accepted by the reader in round 1\n\nThe tour." in output.value.text


async def test_feedback_reaches_the_second_round_and_an_unaccepted_draft_stays_a_draft(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scan, first, second = _client("notes"), _client("Draft one."), _client("Draft two.")
    patch_runtime(monkeypatch, [MockChatClient(responses=[]), scan, first, second])
    project = make_workdir(tmp_path)
    host = _host(tmp_path, project, interactive=True)
    try:
        result, events, asks = await _run(
            host, DEMO, input_text="depth: quick", answers={"review": ["Mention the CLI.", "Still too long."]}
        )
    finally:
        await host.shutdown()

    # A preset depth leaves nothing to ask there, so the conditional edges route around the node.
    assert [ask.node_id for ask in asks] == ["review", "review"]
    assert of_type(events, WorkflowRunNotice) == []
    assert _states(events)["choose_depth"] == ["skipped"]
    revision = _prompt(second)
    assert "asked for this change:\n\nMention the CLI." in revision
    assert "--- current tour ---\nDraft one." in revision
    assert asks[1].questions[0].question.startswith("Draft two.\n\n---\n\n**Review, round 2 of 2.**")
    # on_exhausted="continue": the run completes, and the output says what it is.
    assert result.outcome.value == "completed"
    (output,) = result.outputs
    assert output.node_id == "render_tour"
    assert "Status: DRAFT, not accepted after 2 rounds. Still open: Still too long.\n\nDraft two." in output.value.text


async def test_the_fail_variant_in_the_loop_comment_ends_the_run_when_the_rounds_run_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = DEMO_SOURCE.read_text(encoding="utf-8")
    assert source.count('on_exhausted="continue",') == 1
    patch_runtime(monkeypatch, [MockChatClient(responses=[]), _client("notes"), _client("one"), _client("two")])
    project = make_workdir(tmp_path)
    write_workflow(
        project, "demo-fail", source.replace('on_exhausted="continue",', 'on_exhausted="fail",').encode("utf-8")
    )
    host = _host(tmp_path, project, interactive=True)
    try:
        await confirm(host, "demo-fail")
        result, _events, asks = await _run(
            host, "demo-fail", input_text="depth: quick", answers={"review": ["Shorter.", "Shorter still."]}
        )
    finally:
        await host.shutdown()

    assert len(asks) == 2
    assert (result.outcome.value, result.node_id) == ("loop_exhausted", "refine")
    assert result.outputs == ()


async def test_a_headless_run_that_has_to_ask_fails_rather_than_assuming_an_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No agent client is provided: reaching an agent would fail the test on the empty list.
    patch_runtime(monkeypatch, [MockChatClient(responses=[])])
    project = make_workdir(tmp_path)
    host = _host(tmp_path, project, interactive=False)
    try:
        result, events, asks = await _run(host, DEMO, input_text="Where does it start?")
    finally:
        await host.shutdown()

    assert asks == []
    assert (result.outcome.value, result.node_id) == ("node_failed", "choose_depth")
    failed = [event for event in of_type(events, WorkflowNodeStateChanged) if event.state == "failed"]
    assert [(event.node_id, event.error_class) for event in failed] == [("choose_depth", "ask_unavailable")]


@pytest.fixture(scope="module")
def demo() -> dict[str, Any]:
    return runpy.run_path(str(DEMO_SOURCE), run_name="chrys_workflow_template")


@pytest.mark.parametrize(
    ("text", "focus", "interactive", "depth"),
    [
        ("", "a general orientation for someone who is new to this project", True, None),
        ("How are errors handled?", "How are errors handled?", True, None),
        ("Depth: DEEP\nerrors", "errors", True, "deep"),
        ("interactive: false", "a general orientation for someone who is new to this project", False, "quick"),
        ("interactive: no\ndepth: deep\nline one\nline two", "line one\nline two", False, "deep"),
        ("see http://example.test/x", "see http://example.test/x", True, None),
    ],
)
def test_the_input_convention(
    demo: dict[str, Any], text: str, focus: str, interactive: bool, depth: str | None
) -> None:
    value = demo["read_request"](WorkflowValue(text=text))

    assert value.text == focus
    assert (value.data["focus"], value.data["interactive"], value.data["depth"]) == (focus, interactive, depth)
    # Whatever is asked, a fresh request is never already reviewed or accepted.
    assert (value.data["round"], value.data["reviewed"], value.data["accepted"]) == (0, False, False)


@pytest.mark.parametrize("text", ["interactive: maybe", "depth: medium"])
def test_an_option_the_demo_cannot_interpret_is_an_error_not_a_guess(demo: dict[str, Any], text: str) -> None:
    with pytest.raises(ValueError, match="is not understood"):
        demo["read_request"](WorkflowValue(text=text))


def test_the_documented_command_works_when_copied_from_the_source_view(demo: dict[str, Any]) -> None:
    # People copy the command from the Source tab, which shows the file's text, not the docstring's
    # value: an escape that Python would undo reaches the shell as written.
    [command] = [line.strip() for line in DEMO_SOURCE.read_text(encoding="utf-8").splitlines() if "--input $'" in line]
    assert command in demo["__doc__"]
    quoted = command.partition("--input $'")[2]
    assert quoted.endswith("'") and "\\\\" not in quoted

    # The only escape in it is the newline of shell $'...' quoting.
    value = demo["read_request"](WorkflowValue(text=quoted[:-1].replace("\\n", "\n")))

    assert (value.data["interactive"], value.data["depth"]) == (False, "deep")
    assert value.text == "how are errors handled?"


def test_the_documented_powershell_command_works(demo: dict[str, Any]) -> None:
    lines = [line.strip() for line in DEMO_SOURCE.read_text(encoding="utf-8").splitlines()]
    [command] = [line for line in lines if line.startswith("icode workflow run") and '--input "' in line]
    assert command in demo["__doc__"]
    quoted = command.partition('--input "')[2]
    assert quoted.endswith('"') and "\\" not in quoted

    # Inside PowerShell double quotes, `n is a line break.
    value = demo["read_request"](WorkflowValue(text=quoted[:-1].replace("`n", "\n")))

    assert (value.data["interactive"], value.data["depth"]) == (False, "deep")
    assert value.text == "how are errors handled?"


@pytest.mark.parametrize(
    "text",
    [
        # What PowerShell passes for the bash command: `$'...'` is not its quoting.
        "$interactive: false\\ndepth: deep\\nhow are errors handled?",
        "interactive: false\\ndepth: deep",
        "  Depth: deep\\nerrors",
    ],
)
def test_bash_quoting_that_reached_the_workflow_as_written_is_an_error(demo: dict[str, Any], text: str) -> None:
    with pytest.raises(ValueError, match=r"bash quoting.*PowerShell") as raised:
        demo["read_request"](WorkflowValue(text=text))

    # The message carries a command that works in PowerShell.
    hint = str(raised.value).partition("--input ")[2]
    value = demo["read_request"](WorkflowValue(text=hint.strip('"').replace("`n", "\n")))
    assert (value.data["interactive"], value.data["depth"]) == (False, "deep")


@pytest.mark.parametrize(
    "text",
    [
        "what does \\n mean in a regex?",
        "interactive: false\nwhat does \\n mean in a regex?",
        "the depth: of \\n escapes",
    ],
)
def test_a_literal_backslash_n_in_an_ordinary_request_is_text(demo: dict[str, Any], text: str) -> None:
    value = demo["read_request"](WorkflowValue(text=text))

    assert "\\n" in value.text
