# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Workflow runs through the headless host: discovery, confirmation, the run record, agent nodes, asks, retries."""

from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

import pytest

from chrys.foundation.events.types import (
    WORKFLOW_NOTICE_DATA_DROPPED,
    ApprovalRequest,
    ApprovalResponse,
    Event,
    InvocationContentDelta,
    InvocationMessage,
    InvocationStarted,
    InvocationToolCallResult,
    InvocationToolCallStart,
    SetApprovalMode,
    WorkflowLoopIteration,
    WorkflowNodeAnswer,
    WorkflowNodeAnswered,
    WorkflowNodeAskUser,
    WorkflowNodeOutput,
    WorkflowNodeRetryRequest,
    WorkflowNodeStateChanged,
    WorkflowRunAccepted,
    WorkflowRunFinished,
    WorkflowRunNotice,
    WorkflowRunStarted,
)
from chrys.foundation.models.ask_user import AskUserAnswer, AskUserOption, AskUserQuestion
from chrys.foundation.models.invocations import InvocationOrigin
from chrys.orchestration.session_host import WorkflowRunRejectedError
from chrys.orchestration.workflows.catalog import WorkflowNotFoundError
from chrys.service.llm.mock import MockChatClient, MockResponse
from chrys.service.state.store import JsonFileStateStore
from chrys.service.workflows.discovery import SOURCE_KIND_BUILTIN, SOURCE_KIND_PROJECT
from chrys.service.workflows.layout import HEADER_FILE, run_dir
from chrys.service.workflows.orphans import read_run_terminal
from chrys.service.workflows.store import (
    DATA_DROPPED_KEY,
    NODE_RECORD_INPUT,
    NODE_RECORD_OUTPUT,
    read_node_emits,
    read_node_value,
    read_run_header,
    read_run_spec,
)
from tests.orchestration.workflows._hosting import (
    PROFILE,
    confirm,
    make_host,
    make_profile,
    make_project,
    of_type,
    patch_runtime,
    run,
    write_workflow,
)
from tests.support.workflow_workers import create_venv, python_workflow

pytestmark = pytest.mark.asyncio

CHAIN = python_workflow(
    "def upper(text):\n    return text.text.upper()\ndef exclaim(text):\n    return text.text + '!'\n",
    "upper",
    "exclaim",
)


def _agent_workflow(*, second: bool = False, data: bool = False) -> bytes:
    produce = "WorkflowValue(text='draft', data={'k': 1})" if data else "'draft'"
    lines = [
        "from chrys.workflows import WorkflowBuilder, WorkflowValue",
        f"def prepare(text):\n    return {produce}",
        "def relay(value, ctx):\n    return WorkflowValue(text=value.text + '|relayed', data=value.data)",
        "wf = WorkflowBuilder('agents')",
        "_prepare = wf.python('prepare', prepare)",
        f"_review = wf.agent('review', profile={PROFILE!r}, instructions_suffix='Be terse.')",
        "wf.start(_prepare)",
        "wf.chain(_prepare, _review)",
    ]
    if second:
        lines += [
            "_relay = wf.python('relay', relay)",
            f"_judge = wf.agent('judge', profile={PROFILE!r})",
            "wf.chain(_review, _relay, _judge)",
            "wf.output(_judge)",
        ]
    else:
        lines.append("wf.output(_review)")
    lines.append("workflow = wf.build()")
    return "\n".join(lines).encode("utf-8") + b"\n"


async def test_a_python_chain_runs_to_completion_with_a_durable_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_runtime(monkeypatch, [MockChatClient(responses=[])])
    project = make_project(tmp_path)
    path = write_workflow(project, "chain", CHAIN)
    host = make_host(tmp_path, project=project)
    try:
        found = host.list_workflows()
        assert [(s.workflow_id, s.source_kind, s.canonical_path) for s in found.sources][:1] == [
            ("chain", SOURCE_KIND_PROJECT, str(path.resolve()))
        ]
        assert {s.source_kind for s in found.sources[1:]} <= {SOURCE_KIND_BUILTIN}
        with pytest.raises(WorkflowNotFoundError):
            await host.preview_workflow(host.workflow_target("missing"))

        with pytest.raises(WorkflowRunRejectedError) as rejected:
            await run(host, "chain", input_text="hello")
        assert rejected.value.event.error == "not_confirmed"
        assert host.engine.execution().kind == "idle"

        preview = await confirm(host, "chain")
        assert preview.title == "t"
        result, events = await run(host, "chain", input_text="hello")

        assert result.outcome.value == "completed"
        assert [(output.node_id, output.value.text) for output in result.outputs] == [("exclaim", "HELLO!")]
        assert result.error == ""

        accepted = of_type(events, WorkflowRunAccepted)
        assert len(accepted) == 1
        assert accepted[0].run_id == result.run_id
        started = of_type(events, WorkflowRunStarted)
        assert len(started) == 1
        assert (started[0].workflow_id, started[0].source_kind, started[0].title) == ("chain", SOURCE_KIND_PROJECT, "t")
        assert started[0].spec_digest == preview.spec_digest
        assert started[0].resolved_nodes == []
        states = [(e.node_id, e.attempt, e.state) for e in of_type(events, WorkflowNodeStateChanged)]
        assert states == [
            ("upper", 1, "running"),
            ("upper", 1, "completed"),
            ("exclaim", 1, "running"),
            ("exclaim", 1, "completed"),
        ]
        finals = [(e.node_id, e.kind, e.ordinal, e.summary_text) for e in of_type(events, WorkflowNodeOutput)]
        assert finals == [("upper", "final", 1, "HELLO"), ("exclaim", "final", 1, "HELLO!")]
        finished = of_type(events, WorkflowRunFinished)
        assert len(finished) == 1
        assert finished[0].outcome == "completed"
        assert [(o.node_id, o.summary_text) for o in finished[0].outputs] == [("exclaim", "HELLO!")]
        assert finished[0].reason == ""
        assert not finished[0].degraded
        assert events[-1] is finished[0]

        session_dir = host.workflow_session_dir
        assert session_dir is not None
        record = run_dir(session_dir, result.run_id)
        header = read_run_header(record)
        assert (header["workflow_id"], header["mode"], header["input_excerpt"]) == ("chain", "headless", "hello")
        assert (read_run_terminal(record).outcome, header["spec_digest"]) == ("completed", preview.spec_digest)
        assert (
            read_run_spec(record)["environment"]["environment_fingerprint"]
            == preview.environment.environment_fingerprint
        )
        assert (record / HEADER_FILE).is_file()
        exclaim = result.outputs[0]
        assert read_node_value(record, exclaim.activation_id, 1, NODE_RECORD_OUTPUT) == {
            "value": {"text": "HELLO!", "data": None}
        }
        assert read_node_value(record, exclaim.activation_id, 1, NODE_RECORD_INPUT) == {
            "value": {"text": "HELLO", "data": None}
        }
        assert host.engine.execution().kind == "idle"
        assert host.engine.workflows.active_run_id is None
    finally:
        await host.shutdown()


async def test_an_agent_node_runs_on_the_bound_profile_and_its_invocation_is_pinned_ahead(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_runtime(
        monkeypatch, [MockChatClient(responses=[]), MockChatClient(responses=[MockResponse(text="LGTM, ship it")])]
    )
    project = make_project(tmp_path)
    write_workflow(project, "review", _agent_workflow())
    host = make_host(tmp_path, project=project)
    try:
        await confirm(host, "review")
        result, events = await run(host, "review", input_text="diff")
        assert result.outcome.value == "completed"
        assert [(o.node_id, o.value.text) for o in result.outputs] == [("review", "LGTM, ship it")]
        started = of_type(events, WorkflowRunStarted)[0]
        assert [
            (n["node_id"], n["agent"], n["model_profile_id"], n["instructions_suffix"]) for n in started.resolved_nodes
        ] == [("review", PROFILE, "mock-profile", "Be terse.")]
        review_states = [e for e in of_type(events, WorkflowNodeStateChanged) if e.node_id == "review"]
        assert [e.state for e in review_states] == ["running", "completed"]
        assert review_states[0].invocation_id
        assert review_states[0].invocation_id == review_states[1].invocation_id
        # Node-origin invocation events stay off the headless stream.
        assert not of_type(events, InvocationMessage)
        assert not of_type(events, InvocationContentDelta)
        assert not of_type(events, WorkflowRunNotice)
        session_dir = host.workflow_session_dir
        assert session_dir is not None
        record = run_dir(session_dir, result.run_id)
        assert read_node_value(record, review_states[0].activation_id, 1, NODE_RECORD_INPUT) == {
            "value": {"text": "draft", "data": None}
        }
    finally:
        await host.shutdown()


async def test_node_activity_reaches_the_stream_only_on_request_and_only_for_this_runs_invocations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = make_project(tmp_path)
    reference = project / "reference.txt"
    reference.write_text("reference", encoding="utf-8")

    def reviewer() -> MockChatClient:
        return MockChatClient(
            responses=[
                MockResponse(text="Reading it.", tool_calls=[("read_file", "call_1", {"path": str(reference)})]),
                MockResponse(text="LGTM"),
            ]
        )

    patch_runtime(monkeypatch, [MockChatClient(responses=[]), reviewer(), reviewer()], builtin_tools=True)
    write_workflow(project, "review", _agent_workflow())
    host = make_host(tmp_path, project=project, profiles=[make_profile(builtins=["filesystem.read"])])
    try:
        await confirm(host, "review")
        _result, plain = await run(host, "review", input_text="diff")
        assert not of_type(plain, InvocationToolCallStart)

        streamed: list[Event] = []
        async for event in host.iter_workflow_events(
            host.workflow_target("review"), input_text="diff", include_node_activity=True
        ):
            streamed.append(event)
            if isinstance(event, WorkflowRunStarted):
                # Same session and a node-rooted origin, but no state change of this run announced it.
                assert event.session_id is not None
                stranger = InvocationOrigin("workflow_node", event.session_id, "stranger", None, 1)
                await host.event_bus.publish(
                    InvocationToolCallStart(
                        origin=stranger, session_id=event.session_id, call_id="x", tool_name="read_file"
                    )
                )

        announced = {e.invocation_id for e in of_type(streamed, WorkflowNodeStateChanged) if e.invocation_id}
        starts = of_type(streamed, InvocationToolCallStart)
        assert [(e.tool_name, e.origin.root.invocation_id in announced) for e in starts] == [("read_file", True)]
        assert [e.call_id for e in of_type(streamed, InvocationToolCallResult)] == [starts[0].call_id]
        segments: dict[str, str] = {}
        for delta in of_type(streamed, InvocationContentDelta):
            assert (delta.origin.kind, delta.origin.invocation_id in announced) == ("workflow_node", True)
            segments[delta.segment_id] = segments.get(delta.segment_id, "") + delta.text
        assert list(segments.values()) == ["Reading it.", "LGTM"]
        # Node activity arrives between the node's running and completed states.
        kinds = [type(e) for e in streamed if isinstance(e, WorkflowNodeStateChanged | InvocationToolCallStart)]
        assert kinds.index(InvocationToolCallStart) > kinds.index(WorkflowNodeStateChanged)
    finally:
        await host.shutdown()


async def test_an_approval_mode_change_during_the_run_reaches_the_live_agent_node(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Switched from bypass to manual while its pass is open, the node's next tool call asks, as the session's agents do."""
    outside = tmp_path / "outside.txt"
    write = ("write_file", "call_1", {"path": str(outside), "content": "written under bypass"})
    patch_runtime(
        monkeypatch,
        [
            MockChatClient(responses=[]),
            MockChatClient(responses=[MockResponse(tool_calls=[write]), MockResponse(text="done")]),
        ],
        builtin_tools=True,
    )
    project = make_project(tmp_path)
    write_workflow(project, "review", _agent_workflow())
    host = make_host(tmp_path, project=project, profiles=[make_profile(builtins=["filesystem.write"])])
    asked: list[str] = []

    async def _switch_to_manual(event: InvocationStarted) -> None:
        if event.origin.kind == "workflow_node":
            await host.event_bus.publish(SetApprovalMode(mode="manual", persist=False))

    async def _deny(event: ApprovalRequest) -> None:
        asked.append(event.tool_name)
        await host.event_bus.publish(
            ApprovalResponse(request_id=event.request_id, approved=False, session_id=host.workflow_session_id)
        )

    await host.event_bus.subscribe(InvocationStarted, _switch_to_manual)
    await host.event_bus.subscribe(ApprovalRequest, _deny)
    try:
        await confirm(host, "review")
        result, _events = await run(host, "review", input_text="diff")
        assert result.outcome.value == "completed"
        assert asked == ["write_file"]
        assert not outside.exists()
    finally:
        await host.shutdown()


async def test_structured_data_is_dropped_at_the_agent_boundary_and_noticed_once_per_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_runtime(
        monkeypatch,
        [
            MockChatClient(responses=[]),
            MockChatClient(responses=[MockResponse(text="reviewed")]),
            MockChatClient(responses=[MockResponse(text="judged")]),
        ],
    )
    project = make_project(tmp_path)
    write_workflow(project, "data", _agent_workflow(second=True, data=True))
    host = make_host(tmp_path, project=project)
    try:
        await confirm(host, "data")
        result, events = await run(host, "data", input_text="diff")
        assert result.outcome.value == "completed"
        assert [(o.node_id, o.value.text) for o in result.outputs] == [("judge", "judged")]
        notices = of_type(events, WorkflowRunNotice)
        assert [(n.node_id, n.code) for n in notices] == [("review", WORKFLOW_NOTICE_DATA_DROPPED)]
        assert "'review'" in notices[0].message
        session_dir = host.workflow_session_dir
        assert session_dir is not None
        record = run_dir(session_dir, result.run_id)
        activation = {e.node_id: e.activation_id for e in of_type(events, WorkflowNodeStateChanged)}
        assert read_node_value(record, activation["review"], 1, NODE_RECORD_INPUT) == {
            "value": {"text": "draft", "data": {"k": 1}},
            DATA_DROPPED_KEY: True,
        }
        # The agent's text output carries no data, so the python node after it sees none and passes none on.
        assert read_node_value(record, activation["relay"], 1, NODE_RECORD_INPUT) == {
            "value": {"text": "reviewed", "data": None}
        }
        assert read_node_value(record, activation["judge"], 1, NODE_RECORD_INPUT) == {
            "value": {"text": "reviewed|relayed", "data": None}
        }
    finally:
        await host.shutdown()


async def test_a_second_agent_boundary_with_data_does_not_repeat_the_notice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _agent_workflow(second=True, data=True).replace(
        b"return WorkflowValue(text=value.text + '|relayed', data=value.data)",
        b"return WorkflowValue(text=value.text + '|relayed', data=[1, 2])",
    )
    patch_runtime(
        monkeypatch,
        [
            MockChatClient(responses=[]),
            MockChatClient(responses=[MockResponse(text="reviewed")]),
            MockChatClient(responses=[MockResponse(text="judged")]),
        ],
    )
    project = make_project(tmp_path)
    write_workflow(project, "data2", source)
    host = make_host(tmp_path, project=project)
    try:
        await confirm(host, "data2")
        result, events = await run(host, "data2", input_text="diff")
        assert result.outcome.value == "completed"
        assert [n.node_id for n in of_type(events, WorkflowRunNotice)] == ["review"]
        session_dir = host.workflow_session_dir
        assert session_dir is not None
        activation = {e.node_id: e.activation_id for e in of_type(events, WorkflowNodeStateChanged)}
        judge_input = read_node_value(run_dir(session_dir, result.run_id), activation["judge"], 1, NODE_RECORD_INPUT)
        assert judge_input == {"value": {"text": "reviewed|relayed", "data": [1, 2]}, DATA_DROPPED_KEY: True}
    finally:
        await host.shutdown()


async def test_emits_and_a_loop_stream_in_order_and_land_in_the_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = b"""
from chrys.workflows import WorkflowBuilder, WorkflowValue

def count(value, ctx):
    n = int(value.text) + 1
    ctx.emit('tick ' + str(n))
    ctx.emit('tock ' + str(n))
    return str(n)

def body(scope):
    node = scope.python('count', count)
    return node, node

wf = WorkflowBuilder('loop')
_loop = wf.loop('grow', body, until=lambda value: int(value.text) >= 3, max_iterations=5)
wf.start(_loop)
wf.output(_loop)
workflow = wf.build()
"""
    patch_runtime(monkeypatch, [MockChatClient(responses=[])])
    project = make_project(tmp_path)
    write_workflow(project, "loop", source)
    host = make_host(tmp_path, project=project)
    try:
        await confirm(host, "loop")
        result, events = await run(host, "loop", input_text="0")
        assert result.outcome.value == "completed"
        assert [(o.node_id, o.value.text) for o in result.outputs] == [("grow", "3")]
        outputs = [
            (e.node_id, e.attempt, e.kind, e.ordinal, e.summary_text) for e in of_type(events, WorkflowNodeOutput)
        ]
        assert outputs[:3] == [
            ("count", 1, "emit", 1, "tick 1"),
            ("count", 1, "emit", 2, "tock 1"),
            ("count", 1, "final", 3, "1"),
        ]
        # The loop activation has a final of its own, after the last iteration's and before the run's terminal.
        assert outputs[-1] == ("grow", 1, "final", 1, "3")
        assert len(outputs) == 10
        assert [(e.loop_id, e.iteration, e.verdict) for e in of_type(events, WorkflowLoopIteration)] == [
            ("grow", 1, "continue"),
            ("grow", 2, "continue"),
            ("grow", 3, "exit"),
        ]
        directory = run_dir(host.workflow_session_dir, result.run_id)
        assert read_node_emits(directory, "count@iter#2", 1) == [(1, "tick 2"), (2, "tock 2")]
        loop_input = read_node_value(directory, "grow@iter#1", 1, NODE_RECORD_INPUT)
        loop_output = read_node_value(directory, "grow@iter#1", 1, NODE_RECORD_OUTPUT)
        assert loop_input is not None and loop_input["value"]["text"] == "0"
        assert loop_output is not None and loop_output["value"]["text"] == "3"
    finally:
        await host.shutdown()


async def test_a_loop_that_exits_at_once_still_records_its_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = b"""
from chrys.workflows import WorkflowBuilder

def body(scope):
    node = scope.python('fn', lambda text: text.text + '!')
    return node, node

wf = WorkflowBuilder('loop')
_loop = wf.loop('loop', body, until=lambda value: True, max_iterations=3)
wf.start(_loop)
wf.output(_loop)
workflow = wf.build()
"""
    patch_runtime(monkeypatch, [MockChatClient(responses=[])])
    project = make_project(tmp_path)
    write_workflow(project, "loop", source)
    host = make_host(tmp_path, project=project)
    try:
        await confirm(host, "loop")
        result, events = await run(host, "loop", input_text="input")
        assert [(o.node_id, o.activation_id, o.value.text) for o in result.outputs] == [
            ("loop", "loop@iter#1", "input!")
        ]
        finals = [(e.node_id, e.summary_text) for e in of_type(events, WorkflowNodeOutput) if e.kind == "final"]
        assert finals == [("fn", "input!"), ("loop", "input!")]
        directory = run_dir(host.workflow_session_dir, result.run_id)
        stored = read_node_value(directory, "loop@iter#1", 1, NODE_RECORD_OUTPUT)
        assert stored is not None and stored["value"]["text"] == "input!"
    finally:
        await host.shutdown()


async def test_a_long_emit_is_summarised_live_and_kept_whole_in_the_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = python_workflow(
        "def fn(value, ctx):\n    ctx.emit('x' * 600 + 'TAIL')\n    ctx.emit('short')\n    return 'done'\n", "fn"
    )
    patch_runtime(monkeypatch, [MockChatClient(responses=[])])
    project = make_project(tmp_path)
    write_workflow(project, "emits", source)
    host = make_host(tmp_path, project=project)
    try:
        await confirm(host, "emits")
        result, events = await run(host, "emits", input_text="x")
        assert result.outcome.value == "completed"
        emitted = [e.summary_text for e in of_type(events, WorkflowNodeOutput) if e.kind == "emit"]
        assert len(emitted[0]) == 512 and emitted[0].endswith("…") and "TAIL" not in emitted[0]
        assert emitted[1] == "short"
        directory = run_dir(host.workflow_session_dir, result.run_id)
        assert read_node_emits(directory, "fn@iter#1", 1) == [(1, "x" * 600 + "TAIL"), (2, "short")]
    finally:
        await host.shutdown()


async def test_a_headless_ask_fails_the_node_and_the_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = python_workflow("async def fn(value, ctx):\n    return await ctx.ask('colour?')\n", "fn")
    patch_runtime(monkeypatch, [MockChatClient(responses=[])])
    project = make_project(tmp_path)
    write_workflow(project, "ask", source)
    host = make_host(tmp_path, project=project)
    try:
        await confirm(host, "ask")
        result, events = await run(host, "ask", input_text="x")
        assert (result.outcome.value, result.node_id) == ("node_failed", "fn")
        assert not of_type(events, WorkflowNodeAskUser)
        failed = [e for e in of_type(events, WorkflowNodeStateChanged) if e.state == "failed"]
        assert [(e.node_id, e.error_class) for e in failed] == [("fn", "ask_unavailable")]
        assert "headless" in failed[0].error
        assert result.error == failed[0].error
    finally:
        await host.shutdown()


STRUCTURED_ASK = python_workflow(
    "from chrys.workflows import Option, Question\n"
    "async def fn(value, ctx):\n"
    "    colour = await ctx.ask('colour?')\n"
    "    size, extras = await ctx.ask([\n"
    "        Question('size?', header='Size', options=[Option('S', 'small'), 'M']),\n"
    "        Question('extras?', header='Extras', options=['cheese', 'olives', 'basil'], multi_select=True),\n"
    "    ])\n"
    "    return f'{colour}/{size.choice}/{\",\".join(extras.selected)}/{extras.text}'\n",
    "fn",
)


def _answer(run_id: str, ask: WorkflowNodeAskUser, answers: tuple[AskUserAnswer, ...]) -> WorkflowNodeAnswer:
    return WorkflowNodeAnswer(
        run_id=run_id,
        node_id=ask.node_id,
        activation_id=ask.activation_id,
        request_id=ask.request_id,
        answers=answers,
    )


async def test_an_interactive_ask_round_trips_through_the_bus(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    patch_runtime(monkeypatch, [MockChatClient(responses=[])])
    project = make_project(tmp_path)
    write_workflow(project, "ask", STRUCTURED_ASK)
    host = make_host(tmp_path, project=project, allow_user_interaction=True)
    open_answers = [
        (AskUserAnswer(values=("blue",)),),
        (AskUserAnswer(values=("M",)), AskUserAnswer(values=("basil", "cheese"), note="extra")),
    ]
    try:
        await confirm(host, "ask")
        asks: list[WorkflowNodeAskUser] = []
        answered: list[WorkflowNodeAnswered] = []
        run_id = ""
        async for event in host.iter_workflow_events(host.workflow_target("ask"), input_text="x"):
            if isinstance(event, WorkflowRunAccepted):
                run_id = event.run_id
            if isinstance(event, WorkflowNodeAnswered):
                answered.append(event)
            if isinstance(event, WorkflowNodeAskUser):
                asks.append(event)

                # A stray answer for another activation, and answers that do not fit the question (two
                # values on a single-select, a count mismatch), leave the ask open; the real one resolves it.
                stray = replace(_answer(run_id, event, (AskUserAnswer(values=("no",)),)), activation_id="other")
                await host.event_bus.publish(stray)
                await host.event_bus.publish(
                    _answer(run_id, event, (AskUserAnswer(values=("S", "M")), AskUserAnswer()))
                )
                await host.event_bus.publish(_answer(run_id, event, ()))
                await host.event_bus.publish(_answer(run_id, event, open_answers.pop(0)))
        result = host.engine.workflows.result(run_id)
        assert result is not None
        assert [(a.node_id, a.questions) for a in asks] == [
            ("fn", (AskUserQuestion(question="colour?"),)),
            (
                "fn",
                (
                    AskUserQuestion(
                        question="size?",
                        header="Size",
                        options=(AskUserOption(label="S", description="small"), AskUserOption(label="M")),
                    ),
                    AskUserQuestion(
                        question="extras?",
                        header="Extras",
                        options=(
                            AskUserOption(label="cheese"),
                            AskUserOption(label="olives"),
                            AskUserOption(label="basil"),
                        ),
                        multi_select=True,
                    ),
                ),
            ),
        ]
        assert [a.answer for a in answered] == ["blue", "Size: M\nExtras: basil, cheese — extra"]
        assert [(o.node_id, o.value.text) for o in result.outputs] == [("fn", "blue/M/cheese,basil/extra")]
        session_dir = host.workflow_session_dir
        assert session_dir is not None
        assert read_run_header(run_dir(session_dir, run_id))["mode"] == "interactive"
    finally:
        await host.shutdown()


async def test_a_failed_node_auto_retries_then_awaits_a_manual_retry_in_interactive_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = tmp_path / "attempts"
    source = python_workflow(
        "import pathlib\n"
        f"MARKER = pathlib.Path({str(marker)!r})\n"
        "def fn(text):\n"
        "    count = int(MARKER.read_text()) + 1 if MARKER.exists() else 1\n"
        "    MARKER.write_text(str(count))\n"
        "    if count < 3:\n"
        "        raise ValueError('boom ' + str(count))\n"
        "    return 'ok after ' + str(count)\n",
        "fn",
    ).replace(b"wf.python('fn', fn)", b"wf.python('fn', fn, retry=Retry(max_attempts=2, backoff=0.01))")
    patch_runtime(monkeypatch, [MockChatClient(responses=[])])
    project = make_project(tmp_path)
    write_workflow(project, "retry", source)
    host = make_host(tmp_path, project=project, allow_user_interaction=True)
    try:
        await confirm(host, "retry")
        run_id = ""
        retried: list[WorkflowNodeStateChanged] = []
        async for event in host.iter_workflow_events(host.workflow_target("retry"), input_text="x"):
            if isinstance(event, WorkflowRunAccepted):
                run_id = event.run_id
            if isinstance(event, WorkflowNodeStateChanged) and event.state == "awaiting_retry":
                retried.append(event)
                await host.event_bus.publish(
                    WorkflowNodeRetryRequest(
                        run_id=run_id,
                        node_id=event.node_id,
                        activation_id=event.activation_id,
                        request_id=f"retry-{len(retried)}",
                        expected_failed_attempt=event.attempt,
                    )
                )
        result = host.engine.workflows.result(run_id)
        assert result is not None
        assert result.outcome.value == "completed"
        assert [(o.node_id, o.value.text) for o in result.outputs] == [("fn", "ok after 3")]
        assert [(e.attempt, e.error, e.error_class) for e in retried] == [(2, "ValueError: boom 2", "python_exception")]
    finally:
        await host.shutdown()


async def test_a_byo_interpreter_runs_the_workflow(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project = make_project(tmp_path)
    venv = create_venv(project / ".chrys" / "workflows" / "venv")
    source = b"# /// script\n# [tool.chrys]\n# python = 'venv'\n# ///\n" + python_workflow(
        "import sys\ndef fn(text):\n    return sys.executable\n", "fn"
    )
    patch_runtime(monkeypatch, [MockChatClient(responses=[])])
    write_workflow(project, "byo", source)
    host = make_host(tmp_path, project=project)
    try:
        preview = await confirm(host, "byo")
        assert preview.environment.mode == "byo"
        result, _events = await run(host, "byo", input_text="x")
        assert result.outcome.value == "completed"
        expected = venv / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
        assert Path(result.outputs[0].value.text).resolve() == expected.resolve()
    finally:
        await host.shutdown()


# -- session lifecycle: a workflow-only session is a real session, and a restored one keeps its project ---


async def test_a_workflow_only_session_is_saved_and_restores_into_a_new_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_runtime(monkeypatch, [MockChatClient(responses=[]), MockChatClient(responses=[MockResponse(text="Hello")])])
    project = make_project(tmp_path)
    write_workflow(project, "chain", CHAIN)
    host = make_host(tmp_path, project=project)
    await confirm(host, "chain")
    first, _events = await run(host, "chain", input_text="hi")
    session_id = host.workflow_session_id
    session_dir = host.workflow_session_dir
    assert session_dir is not None
    await host.shutdown()
    # No conversation happened, yet the session is saved: its run records need a session to belong to.
    assert (session_dir / "session.json").exists()
    assert read_run_terminal(run_dir(session_dir, first.run_id)).outcome == "completed"
    state_store = JsonFileStateStore(tmp_path / "sessions")
    meta = await state_store.load_session_meta(session_id)
    assert meta is not None and meta.kind == "workflow"
    assert await state_store.load_latest_session_id(chat_only=True) is None

    restored = make_host(tmp_path, project=project)
    try:
        await restored.load_workflow_session(session_id)
        assert restored.workflow_session_id == session_id
        assert restored.workflow_session_dir == session_dir
        second, _events = await run(restored, "chain", input_text="again")
        assert second.outcome.value == "completed"
        assert read_run_terminal(run_dir(session_dir, second.run_id)).outcome == "completed"
        assert read_run_terminal(run_dir(session_dir, first.run_id)).outcome == "completed"
        meta = await state_store.load_session_meta(session_id)
        assert meta is not None and meta.kind == "workflow"
        # A real chat turn has its own identity; workflow history stays isolated.
        async for _event in restored.iter_run_events("Hello"):
            pass
        meta = await state_store.load_session_meta(session_id)
        assert meta is not None and meta.kind == "workflow"
        await run(restored, "chain", input_text="after chatting")
        meta = await state_store.load_session_meta(session_id)
        assert meta is not None and meta.kind == "workflow"
        assert await state_store.load_latest_session_id(chat_only=True) == restored.session_id
        assert restored.session_id != session_id
    finally:
        await restored.shutdown()


async def test_a_restored_session_discovers_workflows_in_its_own_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_runtime(monkeypatch, [MockChatClient(responses=[]), MockChatClient(responses=[])])
    project = make_project(tmp_path)
    path = write_workflow(project, "only_here", CHAIN)
    host = make_host(tmp_path, project=project)
    await confirm(host, "only_here")
    first, _events = await run(host, "only_here", input_text="x")
    assert first.outcome.value == "completed"
    session_id = host.workflow_session_id
    await host.shutdown()

    # The CLI resumes a session without binding a cwd: the process may sit anywhere at that point.
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    restored = make_host(tmp_path, project=None)
    try:
        await restored.load_workflow_session(session_id)
        assert restored.workflow_session_id == session_id
        found = restored.list_workflows().find("only_here")
        assert found is not None
        assert (found.source_kind, found.canonical_path) == (SOURCE_KIND_PROJECT, str(path.resolve()))
        preview = await restored.preview_workflow(restored.workflow_target("only_here"))
        assert preview.preview.source.canonical_path == str(path.resolve())
        second, _events = await run(restored, "only_here", input_text="y")
        assert second.outcome.value == "completed"
    finally:
        await restored.shutdown()


async def test_new_workflow_node_reads_later_model_editor_save_after_profile_reload(tmp_path, monkeypatch):
    from chrys.foundation.config.process_env import freeze_process_env
    from chrys.foundation.events.types import SettingsReload
    from chrys.service.profiles.models.schema import ModelProfile
    from chrys.service.profiles.models.serializer import save_profile
    from tests.orchestration.workflows._hosting import agent_node_module

    monkeypatch.setenv("CHRYS_MODEL_PROFILE", "mock-profile")
    freeze_process_env()
    patch_runtime(monkeypatch, [MockChatClient(responses=[])])
    project = make_project(tmp_path)
    write_workflow(
        project, "review", _agent_workflow().replace(b"instructions_suffix=", b"model='disk', instructions_suffix=")
    )
    host = make_host(tmp_path, project=project)
    model = ModelProfile(id="disk", name="On disk", provider="mock", model_id="from-disk", stream=False)
    save_profile(model)

    async def client_for(model, **kwargs):
        return MockChatClient(responses=[MockResponse(text=model.model_id)])

    monkeypatch.setattr(agent_node_module, "create_client", client_for)
    try:
        await host.start()
        registry = host.engine.model_registry
        await host.event_bus.publish(
            SettingsReload(session_id=host.engine.session_id, manual=True), raise_handler_errors=True
        )
        assert registry is host.engine.model_registry
        assert registry.get("disk").model_id == "from-disk"
        # The editor writes the definition, then registers the saved model on its shared instance.
        updated = replace(model, model_id="after-editor-save")
        save_profile(updated)
        registry.register(updated)
        await confirm(host, "review")
        result, _ = await run(host, "review", input_text="diff")
        assert result.outcome.value == "completed"
        assert [out.value.text for out in result.outputs] == ["after-editor-save"]
    finally:
        await host.shutdown()
