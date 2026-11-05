# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Engine-driven: a prompt shortcut's ``!`command``` placeholders run as one step of the message's pre-turn preparation.

Written from ``.plan/prompt-shortcuts-engine.md`` (rules R1-R7, §2, and §7 items 1-10, 16, 25 and the engine half
of 26), not from the implementation. The engine is real (``agent_engine``), prompt hooks and commands are real
subprocesses, and each shortcut is a file in the per-test config dir or the session's working folder, as a user's
would be; the message carries the :class:`PromptShortcutRef` the TUI builds from the loader's answer.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import sys
import textwrap
import threading
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any
from unittest.mock import create_autospec

import psutil
import pytest

import chrys.orchestration.engine.build.builder as builder_module
import chrys.orchestration.engine.run.coordinator as coordinator_module
import chrys.orchestration.engine.run.runner as runner_module
import chrys.orchestration.engine.run.shortcut_commands as shortcut_commands_module
import chrys.service.prompt_shortcuts.shell as shell_module
from chrys.foundation.config.settings import Settings
from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import (
    Error,
    Event,
    ExecutionChanged,
    PromptCommandsEnded,
    PromptCommandsRunning,
    PromptShortcutRef,
    SessionNew,
    SessionRestore,
    SessionRestored,
    SetApprovalMode,
    UserInjectCancel,
    UserInjectResult,
    UserInterrupt,
    UserMessage,
    UserRetry,
    WorkspaceChange,
)
from chrys.foundation.i18n.localizer import Localizer
from chrys.foundation.models.execution import ExecutionSnapshot
from chrys.foundation.models.history_markers import HistoryMarkerKind
from chrys.foundation.models.workspace import Workspace
from chrys.foundation.platform import get_platform
from chrys.foundation.trajectory.event_types import EventType, WaitCategory
from chrys.foundation.trajectory.reader import read_trajectory
from chrys.kernel import Content, FunctionTool, Message
from chrys.orchestration.engine.run.attachments import with_message_text
from chrys.service.approval.judge import ApprovalJudge, JudgeVerdict
from chrys.service.llm.mock import MockResponse
from chrys.service.profiles.agents.registry import AgentProfileRegistry
from chrys.service.profiles.agents.schema import (
    AgentProfile,
    ApprovalConfig,
    CompactionConfig,
    SkillsConfig,
    ToolsConfig,
)
from chrys.service.profiles.models.registry import ModelProfileRegistry
from chrys.service.profiles.models.schema import API_STYLE_CHAT_COMPLETIONS, API_STYLE_RESPONSES, ModelProfile
from chrys.service.prompt_shortcuts import (
    global_shortcuts_dir,
    load_prompt_shortcuts,
    project_shortcuts_dir,
    prompt_shortcut_folders,
)
from chrys.service.state.store import JsonFileStateStore
from chrys.service.tools.registry import ToolRegistry
from chrys.service.trajectory.preparation import PreparationOutcome
from chrys.service.trajectory.session import trajectory_events_path
from chrys.service.trajectory.waits import WaitOutcome
from tests.support.event_capture import capture_event_sequence, collect_events
from tests.support.images import image_bytes
from tests.support.llm_client_engines import parent_profile
from tests.support.scripted_clients import ErrorMockChatClient
from tests.support.scripted_wire import ScriptedWire, pin_wire_inputs, route_clients_to
from tests.support.waiting import (
    ENGINE_TEST_WAIT_TIMEOUT,
    ENGINE_TURN_TIMEOUT,
    await_run_task_chain,
    wait_for,
    with_wait_deadline,
)
from tests.support.wire_cases._kit import (
    anth_replies,
    anth_text,
    cc_replies,
    cc_text,
    resp_message,
    resp_replies,
    resp_response,
)

if TYPE_CHECKING:
    from chrys.foundation.trajectory.envelope import TrajectoryEvent
    from chrys.orchestration.engine.engine import AgentEngine
    from tests.support.engines import AgentEngineFactory
    from tests.support.wire_cases import Reply

_MODEL = "mock-profile"
_SHORTCUT_ID = "shortcut-message-1"
"""The id the TUI gives a message carrying a shortcut (its ``message_id``), so Esc can withdraw it."""
_TYPED_TEXT_KEY = HistoryMarkerKind.TYPED_TEXT_KEY

posix_only = pytest.mark.skipif(
    sys.platform == "win32",
    reason="the commands use POSIX shell syntax; the shell runner has its own Windows coverage",
)


@pytest.fixture(autouse=True)
def _posix_shell(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run placeholders under ``/bin/sh`` whatever the developer's login shell is (Windows uses ``cmd.exe``)."""
    monkeypatch.setenv("SHELL", "/bin/sh")


@pytest.fixture
def release(tmp_path: Path) -> Iterator[Path]:
    """A file a held command waits for; created at teardown so nothing outlives the test."""
    path = tmp_path / "release"
    yield path
    path.touch()


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


@dataclass
class _Started:
    """A started engine, its one mock model client and every event this file asserts on, in publication order."""

    engine: AgentEngine
    client: ErrorMockChatClient
    workspace: Path
    store: JsonFileStateStore
    events: list[Event]

    def of[E: Event](self, kind: type[E]) -> list[E]:
        return [event for event in self.events if isinstance(event, kind)]

    def error_codes(self) -> list[str]:
        return [event.code for event in self.of(Error)]

    def execution_kinds(self, since: int = 0) -> list[str]:
        """The ``ExecutionChanged`` snapshot kinds published since *since* (an index into :attr:`events`)."""
        return [event.snapshot.kind for event in self.events[since:] if isinstance(event, ExecutionChanged)]


_OBSERVED = (Error, PromptCommandsRunning, PromptCommandsEnded, UserInjectResult, ExecutionChanged)


def _profile(approval: ApprovalConfig) -> AgentProfile:
    return AgentProfile(
        name="Code",
        instructions="You are a test assistant.",
        tools=ToolsConfig(builtins=[]),
        skills=SkillsConfig(auto_load_user_agents_skills=False, auto_load_cwd_agents_skills=False),
        approval=approval,
        compaction=CompactionConfig(enabled=False),
    )


async def _start(
    agent_engine: AgentEngineFactory,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    outcomes: Sequence[MockResponse | BaseException],
    *,
    vision: bool = False,
    tools: Sequence[FunctionTool] = (),
    approval: ApprovalConfig | None = None,
) -> _Started:
    """Start an engine on one mock model whose calls answer with *outcomes*, in a fresh working folder."""
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    models = ModelProfileRegistry()
    models.register(ModelProfile(id=_MODEL, name=_MODEL, provider="mock", model_id=_MODEL, stream=False, vision=vision))
    client = ErrorMockChatClient(list(outcomes))

    def client_for(*_args: object, **kwargs: object) -> ErrorMockChatClient:
        # Intermediate text numbers tool batches; the main client gets the callback on every build.
        client._on_intermediate_text = kwargs.get("on_intermediate_text")
        return client

    monkeypatch.setattr(
        builder_module, "create_client", create_autospec(builder_module.create_client, side_effect=client_for)
    )
    if tools:
        registered = list(tools)

        def load_builtins(registry: ToolRegistry, *_args: object, **_kwargs: object) -> list[FunctionTool]:
            for tool in registered:
                registry.register(tool)
            return registered

        monkeypatch.setattr(
            ToolRegistry, "load_builtins", create_autospec(ToolRegistry.load_builtins, side_effect=load_builtins)
        )
    profile = _profile(approval or ApprovalConfig(default="auto"))
    agents = AgentProfileRegistry()
    agents.register(profile)
    bus = EventBus()
    events: list[Event] = []
    for kind in _OBSERVED:
        await bus.subscribe(kind, lambda event, _events=events: collect_events(_events, event))
    store = JsonFileStateStore(tmp_path / "sessions")
    engine = agent_engine(
        bus,
        settings=Settings(model_profile=_MODEL, workspace_change_notice=False, mutation_coordination=False),
        agent_registry=agents,
        model_registry=models,
        state_store=store,
        initial_workspace=Workspace.from_cwd(str(workspace)),
    )
    await engine.start(profile)
    return _Started(engine=engine, client=client, workspace=workspace, store=store, events=events)


def _write_shortcut(folder: Path, name: str, body: str) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{name}.md"
    path.write_text(f"---\ndescription: {name}\n---\n{body}\n", encoding="utf-8")
    return path


def _loaded_ref(engine: AgentEngine, name: str) -> PromptShortcutRef:
    """The ref the TUI sends for shortcut *name*: its name and the path the loader returned, as the ``/`` list loads it."""
    shortcuts = load_prompt_shortcuts(**prompt_shortcut_folders(engine.workspace_primary_cwd))
    (shortcut,) = [shortcut for shortcut in shortcuts if shortcut.name == name]
    return PromptShortcutRef(name=shortcut.name, path=shortcut.path)


def _user_shortcut(started: _Started, name: str, body: str) -> PromptShortcutRef:
    """Write shortcut *name* into the user's own folder and return its ref."""
    _write_shortcut(global_shortcuts_dir(get_platform().config_dir), name, body)
    return _loaded_ref(started.engine, name)


def _shortcut_message(text: str, ref: PromptShortcutRef, injection_id: str = _SHORTCUT_ID) -> UserMessage:
    """What the TUI sends for a shortcut's draft with commands: the typed text, the ref, an id Esc can withdraw."""
    return UserMessage(text=text, injection_id=injection_id, prompt_shortcut=ref)


async def _drain(started: _Started) -> None:
    await await_run_task_chain(started.engine, turn_state=started.engine.turns.turn_state)


async def _send(started: _Started, message: UserMessage) -> None:
    """Publish *message* and wait for its turn, if it started one, to finish and save."""
    await started.engine.event_bus.publish(message)
    if message.started_turn:
        await _drain(started)


def _user_messages(messages: Sequence[Message]) -> list[Message]:
    """User-role messages that are not engine markers (an injected copy would be one of them)."""
    return [
        message
        for message in messages
        if message.role == "user" and HistoryMarkerKind.KEY not in message.additional_properties
    ]


def _request_users(started: _Started, call: int) -> list[Message]:
    """The user messages of the model's *call*-th request (0-based), as the client received them."""
    messages, _options = started.client.call_history[call]
    return [message for message in messages if message.role == "user"]


async def _restore(engine: AgentEngine, session_id: str) -> None:
    async with capture_event_sequence(engine.event_bus, SessionRestored) as restored:
        await engine.event_bus.publish(SessionRestore(session_id=session_id), raise_handler_errors=True)
    assert [event.session_id for event in restored if isinstance(event, SessionRestored)] == [session_id]


def _lines(path: Path) -> int:
    return len(path.read_text(encoding="utf-8").splitlines()) if path.exists() else 0


# --- Hooks ----------------------------------------------------------------------


def _write_hooks(*entries: dict[str, Any]) -> None:
    hooks_dir = get_platform().config_dir / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    document = {"version": 1, "hooks": list(entries)}
    (hooks_dir / "hooks.json").write_text(json.dumps(document), encoding="utf-8")


def _prompt_hook(hook_id: str, script: str) -> dict[str, Any]:
    return {
        "id": hook_id,
        "event": "user_prompt_submit",
        "run": {"type": "command", "argv": [sys.executable, "-c", textwrap.dedent(script)]},
        "execution": {"mode": "blocking", "timeout_seconds": 30},
    }


# --- Tools and the approval judge -----------------------------------------------


def _held_tool(entered: asyncio.Event, release: asyncio.Event) -> FunctionTool:
    """A tool that holds its turn mid-run until the test releases it."""

    async def hold(note: Annotated[str, "Anything"]) -> str:
        entered.set()
        await release.wait()
        return f"held: {note}"

    return FunctionTool(func=hold, name="hold", description="Wait until released")


def _guarded_tool() -> FunctionTool:
    def guarded_echo(message: Annotated[str, "Message to echo"]) -> str:
        return f"echo: {message}"

    return FunctionTool(func=guarded_echo, name="guarded_echo", description="Echo with approval")


_JUDGED = ApprovalConfig(default="auto", overrides={"guarded_echo": "require"})
"""``guarded_echo`` needs approval; under the ``auto`` mode the judge gives it."""


def _spy_judge(monkeypatch: pytest.MonkeyPatch) -> Any:
    evaluate = create_autospec(ApprovalJudge.evaluate, return_value=JudgeVerdict(approved=True, reason="in scope"))
    monkeypatch.setattr(ApprovalJudge, "evaluate", evaluate)
    return evaluate


_EVALUATE_SIGNATURE = inspect.signature(ApprovalJudge.evaluate)


def _judged_inputs(evaluate: Any) -> list[tuple[str, list[str]]]:
    """``(user_message, user_messages)`` of every judge call, however the arguments were passed."""
    inputs: list[tuple[str, list[str]]] = []
    for call in evaluate.await_args_list:
        arguments = _EVALUATE_SIGNATURE.bind(*call.args, **call.kwargs).arguments
        inputs.append((arguments["user_message"], list(arguments.get("user_messages") or [])))
    return inputs


# --- Commands -------------------------------------------------------------------


def _pid_command(pid_file: Path) -> str:
    # Written atomically so a reader never sees a partial pid.
    return f"echo $$ > '{pid_file}.tmp' && mv '{pid_file}.tmp' '{pid_file}'; sleep 30; echo done"


def _held_command(started_file: Path, release_file: Path, output: str) -> str:
    return f"touch '{started_file}'; while [ ! -e '{release_file}' ]; do sleep 0.05; done; echo {output}"


async def _capture_running(pid_file: Path, publish: asyncio.Task[None]) -> list[psutil.Process]:
    """The command's shell and its children, captured while alive."""
    await wait_for(
        lambda: pid_file.exists() or publish.done(), timeout=ENGINE_TURN_TIMEOUT, description="the command started"
    )
    assert not publish.done()
    shell = psutil.Process(int(pid_file.read_text(encoding="utf-8")))
    await wait_for(
        lambda: shell.children(recursive=True), timeout=ENGINE_TURN_TIMEOUT, description="the shell started its sleep"
    )
    return [shell, *shell.children(recursive=True)]


async def _assert_all_gone(processes: list[psutil.Process]) -> None:
    await wait_for(
        lambda: not any(process.is_running() for process in processes),
        timeout=ENGINE_TURN_TIMEOUT,
        description="every process of the command ended",
    )


# --- Trajectory -----------------------------------------------------------------


def _read_log(session_dir: Path) -> list[TrajectoryEvent]:
    path = trajectory_events_path(session_dir)
    return list(read_trajectory(path).events) if path.exists() else []


def _commands_waits(log: Sequence[TrajectoryEvent]) -> list[TrajectoryEvent]:
    return [
        event
        for event in log
        if event.event_type == EventType.WAIT_FINISHED and event.payload.get("category") == WaitCategory.PROMPT_COMMANDS
    ]


def _preparations_of(log: Sequence[TrajectoryEvent], wait: TrajectoryEvent) -> list[TrajectoryEvent]:
    return [
        event
        for event in log
        if event.event_type == EventType.PREPARATION_FINISHED and event.operation_id == wait.parent_operation_id
    ]


async def _logged_commands_wait(session_dir: Path) -> tuple[TrajectoryEvent, TrajectoryEvent, list[TrajectoryEvent]]:
    """Wait for the commands' finished wait and the finished preparation it is a child of; return both and the log."""
    log: list[TrajectoryEvent] = []

    def logged() -> bool:
        nonlocal log
        log = _read_log(session_dir)
        waits = _commands_waits(log)
        return len(waits) == 1 and len(_preparations_of(log, waits[0])) == 1

    await wait_for(logged, timeout=ENGINE_TURN_TIMEOUT, description="the commands' wait and its preparation logged")
    (wait,) = _commands_waits(log)
    (preparation,) = _preparations_of(log, wait)
    return wait, preparation, log


def _session_dir(engine: AgentEngine) -> Path:
    session_dir = engine.session_dir
    assert session_dir is not None
    return session_dir


# ---------------------------------------------------------------------------
# Item 1: the prompt hooks pass before any command runs (R2, R4)
# ---------------------------------------------------------------------------


@with_wait_deadline(ENGINE_TEST_WAIT_TIMEOUT)
async def test_a_blocking_prompt_hook_refuses_the_message_before_any_command_runs(
    agent_engine: AgentEngineFactory, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    marker = tmp_path / "ran"
    _write_hooks(
        _prompt_hook(
            "gate",
            """
            import json, os
            with open(os.environ["CHRYS_HOOK_RESULT"], "w") as f:
                json.dump({"action": "block", "reason": "not now"}, f)
            """,
        )
    )
    started = await _start(agent_engine, monkeypatch, tmp_path, [MockResponse(text="never")])
    typed = f'Go !`echo ran > "{marker}"`'
    message = _shortcut_message(typed, _user_shortcut(started, "go", typed))

    await started.engine.event_bus.publish(message)

    assert started.error_codes() == ["hook_blocked"]
    assert not marker.exists()
    assert started.of(PromptCommandsRunning) == []
    assert message.started_turn is False
    assert started.client.call_count == 0


@with_wait_deadline(ENGINE_TEST_WAIT_TIMEOUT)
async def test_prompt_hooks_check_the_typed_text_before_the_commands_run(
    agent_engine: AgentEngineFactory, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The positive control of the test above: a passing hook sees what was typed, and nothing has run yet."""
    marker, seen = tmp_path / "ran", tmp_path / "seen.json"
    _write_hooks(
        _prompt_hook(
            "look",
            f"""
            import json, os
            with open(os.environ["CHRYS_HOOK_PAYLOAD_FILE"], encoding="utf-8") as f:
                text = json.load(f)["text"]
            with open({str(seen)!r}, "w", encoding="utf-8") as f:
                json.dump({{"text": text, "marker_existed": os.path.exists({str(marker)!r})}}, f)
            """,
        )
    )
    started = await _start(agent_engine, monkeypatch, tmp_path, [MockResponse(text="done")])
    typed = f'Go !`echo ran > "{marker}"`!`echo HOOKED`'
    message = _shortcut_message(typed, _user_shortcut(started, "go", typed))

    await _send(started, message)

    assert message.started_turn is True
    assert json.loads(seen.read_text(encoding="utf-8")) == {"text": typed, "marker_existed": False}
    assert marker.exists()
    (user,) = _request_users(started, 0)
    assert "HOOKED" in user.text


# ---------------------------------------------------------------------------
# Item 2: what is typed versus what is sent (R4)
# ---------------------------------------------------------------------------


@with_wait_deadline(ENGINE_TEST_WAIT_TIMEOUT)
async def test_the_model_history_and_replay_get_the_sent_text_and_the_opener_records_the_typed_text(
    agent_engine: AgentEngineFactory, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    started = await _start(agent_engine, monkeypatch, tmp_path, [MockResponse(text="first"), MockResponse(text="next")])
    engine = started.engine
    typed, sent = "Report: !`echo OUT-MARK`", "Report: OUT-MARK"
    message = _shortcut_message(typed, _user_shortcut(started, "report", typed))

    await _send(started, message)

    assert message.started_turn is True
    # The event keeps what was typed; the prepared contents the live bubble renders are what was sent.
    assert message.text == typed
    assert message.prepared_contents is not None
    assert message.prepared_contents[0] == sent
    (user,) = _request_users(started, 0)
    assert sent in user.text
    assert "!`" not in user.text
    (opener,) = _user_messages(engine.history_messages)
    assert opener.text == sent
    assert opener.additional_properties.get(_TYPED_TEXT_KEY) == typed

    # A plain message sends what was typed, so nothing is recorded on its opener.
    await _send(started, UserMessage(text="plain"))
    plain = _user_messages(engine.history_messages)[-1]
    assert plain.text == "plain"
    assert _TYPED_TEXT_KEY not in plain.additional_properties

    # Replay reads the saved, sent text; the record survives the session file.
    session_id = engine.session_id
    assert session_id is not None
    saved = await started.store.load_session(session_id)
    assert saved is not None
    assert [m.text for m in _user_messages(saved["messages"])] == [sent, "plain"]
    await engine.event_bus.publish(SessionNew(), raise_handler_errors=True)
    await _restore(engine, session_id)
    restored = _user_messages(engine.history_messages)
    assert [m.text for m in restored] == [sent, "plain"]
    assert restored[0].additional_properties.get(_TYPED_TEXT_KEY) == typed


# ---------------------------------------------------------------------------
# Item 3: images come from the typed text only (R4)
# ---------------------------------------------------------------------------


@with_wait_deadline(ENGINE_TEST_WAIT_TIMEOUT)
async def test_an_image_mention_attaches_from_the_typed_text_and_never_from_command_output(
    agent_engine: AgentEngineFactory, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    started = await _start(agent_engine, monkeypatch, tmp_path, [MockResponse(text="seen")], vision=True)
    (started.workspace / "typed.png").write_bytes(image_bytes(size=(2, 2)))
    (started.workspace / "printed.png").write_bytes(image_bytes(size=(5, 5)))
    typed = "Compare @typed.png with !`echo @printed.png`"
    message = _shortcut_message(typed, _user_shortcut(started, "compare", typed))

    await _send(started, message)

    assert message.started_turn is True
    (user,) = _request_users(started, 0)
    # The command ran: its output is in the sent text, the placeholder is not.
    assert "with @printed.png" in user.text
    assert "!`" not in user.text
    images = [content for content in user.contents if (content.media_type or "").startswith("image/")]
    # Only typed.png (2x2): the mention a command printed stays text.
    assert [
        (image.additional_properties.get("width"), image.additional_properties.get("height")) for image in images
    ] == [(2, 2)]


# ---------------------------------------------------------------------------
# Item 4: a command's failures (R3)
# ---------------------------------------------------------------------------


@posix_only
@with_wait_deadline(ENGINE_TEST_WAIT_TIMEOUT)
async def test_a_command_that_exits_non_zero_still_sends_its_output(
    agent_engine: AgentEngineFactory, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    started = await _start(agent_engine, monkeypatch, tmp_path, [MockResponse(text="noted")])
    typed = "Status: !`echo partial; exit 1`"
    message = _shortcut_message(typed, _user_shortcut(started, "status", typed))

    await _send(started, message)

    assert message.started_turn is True
    assert started.error_codes() == []
    (user,) = _request_users(started, 0)
    assert "Status: partial" in user.text
    assert [event.outcome for event in started.of(PromptCommandsEnded)] == ["completed"]


@posix_only
@pytest.mark.parametrize("failure", ["timeout", "start_failed"])
@with_wait_deadline(ENGINE_TEST_WAIT_TIMEOUT)
async def test_a_command_that_times_out_or_cannot_start_refuses_the_message(
    agent_engine: AgentEngineFactory, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, failure: str
) -> None:
    if failure == "timeout":
        monkeypatch.setattr(shell_module, "SHELL_TIMEOUT_SECONDS", 0.5)
        command = "sleep 30"
    else:
        monkeypatch.setenv("SHELL", str(tmp_path / "no-such-shell"))
        command = "echo never-printed"
    started = await _start(agent_engine, monkeypatch, tmp_path, [MockResponse(text="never")])
    engine = started.engine
    typed = f"Run !`{command}` please"
    message = _shortcut_message(typed, _user_shortcut(started, "run", typed))

    await engine.event_bus.publish(message)

    # Nothing is sent and no turn starts; the error names the command.
    assert message.started_turn is False
    assert started.client.call_count == 0
    assert engine.turns.turn_state.lease.run_task is None
    (error,) = started.of(Error)
    assert error.code == "prompt_shortcut_command_failed"
    assert command in error.message
    assert error.display_message is not None
    assert command in Localizer("en").render(error.display_message)
    (ended,) = started.of(PromptCommandsEnded)
    assert ended.outcome == "command_failed"
    # The end comes first, so a frontend clearing what it showed for the commands keeps the failure.
    assert started.events.index(ended) < started.events.index(error)

    # The preparation ends ``command_failed`` and holds the commands' wait; the log keeps outcome and
    # duration only, never the command. The wait's own outcome for a failed command is not given by the spec,
    # so only its presence (as a child of that preparation) is asserted.
    wait, preparation, log = await _logged_commands_wait(_session_dir(engine))
    assert preparation.payload["outcome"] == PreparationOutcome.COMMAND_FAILED
    assert "duration_ms" in wait.payload
    assert command not in json.dumps([event.to_dict() for event in log])


# ---------------------------------------------------------------------------
# Item 5: whatever ends the preparation ends the commands, process tree included (R2)
# ---------------------------------------------------------------------------


@posix_only
@pytest.mark.parametrize("ending", ["withdraw", "interrupt", "session_switch", "shutdown"])
@with_wait_deadline(ENGINE_TEST_WAIT_TIMEOUT)
async def test_ending_the_preparation_stops_a_running_command_and_reaps_it(
    agent_engine: AgentEngineFactory, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, ending: str
) -> None:
    started = await _start(agent_engine, monkeypatch, tmp_path, [MockResponse(text="seeded"), MockResponse(text="no")])
    engine = started.engine
    # A finished turn first, so the session keeps its folder, and its log, once it closes.
    await _send(started, UserMessage(text="seed"))
    session_id, session_dir = engine.session_id, _session_dir(engine)
    calls = started.client.call_count
    pid_file = tmp_path / "pid"
    typed = f"Wait !`{_pid_command(pid_file)}`"
    message = _shortcut_message(typed, _user_shortcut(started, "wait", typed))

    publish = asyncio.create_task(engine.event_bus.publish(message))
    processes = await _capture_running(pid_file, publish)
    ended_by: asyncio.Task[None] | None = None
    if ending == "withdraw":
        await engine.event_bus.publish(UserInjectCancel(injection_id=_SHORTCUT_ID))
    elif ending == "interrupt":
        await engine.event_bus.publish(UserInterrupt())
    elif ending == "session_switch":
        ended_by = asyncio.create_task(engine.event_bus.publish(SessionNew()))
    else:
        ended_by = asyncio.create_task(engine.shutdown())
    await wait_for(publish.done, timeout=ENGINE_TURN_TIMEOUT, description="the message's publish to return")
    if ending == "withdraw":
        # Esc is an ordinary outcome for the sender: the publish returns, it does not raise.
        assert publish.result() is None
    else:
        assert publish.cancelled() or publish.exception() is None
    if ended_by is not None:
        await asyncio.wait_for(ended_by, ENGINE_TURN_TIMEOUT)

    await _assert_all_gone(processes)
    assert message.started_turn is False
    assert started.client.call_count == calls
    (running,) = started.of(PromptCommandsRunning)
    (ended,) = started.of(PromptCommandsEnded)
    assert ended.outcome == "cancelled"
    assert running.injection_id == ended.injection_id == _SHORTCUT_ID
    # Both name the message's session, also when a session transition is what ended the commands.
    assert running.session_id == ended.session_id == session_id

    wait, preparation, _log = await _logged_commands_wait(session_dir)
    assert wait.payload["outcome"] == WaitOutcome.CANCELLED
    if ending in ("withdraw", "interrupt"):
        assert preparation.payload["outcome"] == PreparationOutcome.CANCELLED
    else:
        # A session close ends the admission itself (``clear_active_admissions``), which may finish the
        # preparation as an owner change before the step sees its operation invalidated: either ends it.
        assert preparation.payload["outcome"] in (PreparationOutcome.CANCELLED, PreparationOutcome.OWNER_CHANGED)


@with_wait_deadline(ENGINE_TEST_WAIT_TIMEOUT)
async def test_a_message_withdrawn_while_its_commands_are_announced_starts_none(
    agent_engine: AgentEngineFactory, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    started = await _start(agent_engine, monkeypatch, tmp_path, [MockResponse(text="never")])
    engine = started.engine
    interpolated: list[str] = []
    interpolate = shortcut_commands_module.interpolate_shell

    async def recording_interpolate(text: str, *, cwd: str) -> str:
        interpolated.append(text)
        return await interpolate(text, cwd=cwd)

    monkeypatch.setattr(shortcut_commands_module, "interpolate_shell", recording_interpolate)

    async def withdraw(_event: PromptCommandsRunning) -> None:
        # Esc lands while the frontends are being told the commands start.
        await engine.event_bus.publish(UserInjectCancel(injection_id=_SHORTCUT_ID))

    await engine.event_bus.subscribe(PromptCommandsRunning, withdraw)
    typed = "Run !`echo never` please"
    message = _shortcut_message(typed, _user_shortcut(started, "run", typed))

    await engine.event_bus.publish(message)

    assert interpolated == []
    assert message.started_turn is False
    assert started.client.call_count == 0
    assert [event.outcome for event in started.of(PromptCommandsEnded)] == ["cancelled"]
    assert engine.execution_busy() is False


@with_wait_deadline(ENGINE_TEST_WAIT_TIMEOUT)
async def test_no_command_runs_once_the_admission_s_session_or_build_moved_on(
    agent_engine: AgentEngineFactory, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The admission stops being its session's and build's while the commands are announced, with its operation
    still pending (a seam: every real move also withdraws the operation): its message is refused, so none runs."""
    started = await _start(agent_engine, monkeypatch, tmp_path, [MockResponse(text="never")])
    engine = started.engine
    interpolated: list[str] = []
    interpolate = shortcut_commands_module.interpolate_shell

    async def recording_interpolate(text: str, *, cwd: str) -> str:
        interpolated.append(text)
        return await interpolate(text, cwd=cwd)

    monkeypatch.setattr(shortcut_commands_module, "interpolate_shell", recording_interpolate)
    moved_on = False
    owner_is_current = coordinator_module.TurnCoordinator._admission_owner_is_current

    def owner_still_current(coordinator: Any, admission: Any) -> bool:
        return not moved_on and owner_is_current(coordinator, admission)

    monkeypatch.setattr(coordinator_module.TurnCoordinator, "_admission_owner_is_current", owner_still_current)

    async def move_on(_event: PromptCommandsRunning) -> None:
        nonlocal moved_on
        moved_on = True

    await engine.event_bus.subscribe(PromptCommandsRunning, move_on)
    typed = "Run !`echo never` please"
    message = _shortcut_message(typed, _user_shortcut(started, "run", typed))

    await engine.event_bus.publish(message)

    assert interpolated == []
    assert message.started_turn is False
    assert started.client.call_count == 0
    assert [event.outcome for event in started.of(PromptCommandsEnded)] == ["cancelled"]
    assert engine.execution_busy() is False


@pytest.mark.parametrize("held", [PromptCommandsRunning, PromptCommandsEnded])
@with_wait_deadline(ENGINE_TEST_WAIT_TIMEOUT)
async def test_a_cancel_while_the_commands_are_announced_or_ended_still_tells_every_frontend_both(
    agent_engine: AgentEngineFactory,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    held: type[PromptCommandsRunning | PromptCommandsEnded],
) -> None:
    """The admission is cancelled while a slow frontend holds one of the two events: the frontends after it get
    that event all the same, and the end always follows the start."""
    started = await _start(agent_engine, monkeypatch, tmp_path, [MockResponse(text="never")])
    engine = started.engine
    entered = asyncio.Event()
    release = asyncio.Event()
    later: list[Event] = []

    async def slow_frontend(_event: Event) -> None:
        entered.set()
        await release.wait()

    async def later_frontend(event: Event) -> None:
        later.append(event)

    await engine.event_bus.subscribe(held, slow_frontend)
    for kind in (PromptCommandsRunning, PromptCommandsEnded):
        await engine.event_bus.subscribe(kind, later_frontend)
    typed = "Run !`echo hi` please"
    message = _shortcut_message(typed, _user_shortcut(started, "run", typed))
    publish = asyncio.create_task(engine.event_bus.publish(message))
    await wait_for(
        lambda: entered.is_set() or publish.done(),
        timeout=ENGINE_TURN_TIMEOUT,
        description=f"{held.__name__} going out",
    )
    assert not publish.done()

    publish.cancel()
    release.set()
    with contextlib.suppress(asyncio.CancelledError):
        await publish

    assert [type(event) for event in later] == [PromptCommandsRunning, PromptCommandsEnded]
    assert message.started_turn is False
    assert started.client.call_count == 0
    assert engine.execution_busy() is False


# ---------------------------------------------------------------------------
# Item 6: a shortcut the engine no longer loads from that file is refused unrun (R1)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("case", ["other_working_folder", "file_removed", "path_differs"])
@with_wait_deadline(ENGINE_TEST_WAIT_TIMEOUT)
async def test_a_shortcut_the_engine_no_longer_loads_from_its_file_is_refused_unrun(
    agent_engine: AgentEngineFactory, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, case: str
) -> None:
    marker = tmp_path / "ran"
    typed = f'Go !`echo ran > "{marker}"`'
    started = await _start(agent_engine, monkeypatch, tmp_path, [MockResponse(text="never")])
    engine = started.engine
    if case == "other_working_folder":
        written = _write_shortcut(project_shortcuts_dir(started.workspace), "go", typed)
    else:
        written = _write_shortcut(global_shortcuts_dir(get_platform().config_dir), "go", typed)
    ref = _loaded_ref(engine, "go")
    if case == "other_working_folder":
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        await engine.event_bus.publish(WorkspaceChange(primary_cwd=str(elsewhere)), raise_handler_errors=True)
    elif case == "file_removed":
        written.unlink()
    else:
        # Same name, another file: a copy elsewhere is not the shortcut the engine loads.
        other = _write_shortcut(tmp_path / "copies", "go", typed)
        ref = PromptShortcutRef(name=ref.name, path=str(other))
    message = _shortcut_message(typed, ref)

    await engine.event_bus.publish(message)

    assert started.error_codes() == ["prompt_shortcut_unavailable"]
    assert not marker.exists()
    assert started.of(PromptCommandsRunning) == []
    assert message.started_turn is False
    assert started.client.call_count == 0

    # The refusal is REJECTED (§2.3); with no wait to link it by, it is the log's last finished preparation.
    def refused() -> bool:
        finished = [e for e in _read_log(_session_dir(engine)) if e.event_type == EventType.PREPARATION_FINISHED]
        return bool(finished) and finished[-1].payload.get("outcome") == PreparationOutcome.REJECTED

    await wait_for(refused, timeout=ENGINE_TURN_TIMEOUT, description="the preparation logged as rejected")
    assert _commands_waits(_read_log(_session_dir(engine))) == []


@with_wait_deadline(ENGINE_TEST_WAIT_TIMEOUT)
async def test_a_working_folder_shortcut_the_engine_still_loads_runs_its_commands(
    agent_engine: AgentEngineFactory, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The positive control of the refusals above: the same file, folder and settings, unchanged."""
    marker = tmp_path / "ran"
    typed = f'Go !`echo ran > "{marker}"`!`echo LOADED`'
    started = await _start(agent_engine, monkeypatch, tmp_path, [MockResponse(text="done")])
    _write_shortcut(project_shortcuts_dir(started.workspace), "go", typed)
    message = _shortcut_message(typed, _loaded_ref(started.engine, "go"))

    await _send(started, message)

    assert started.error_codes() == []
    assert message.started_turn is True
    assert marker.exists()
    (user,) = _request_users(started, 0)
    assert "LOADED" in user.text


# ---------------------------------------------------------------------------
# Item 7: a message carrying a shortcut never joins a running turn (R5)
# ---------------------------------------------------------------------------


@with_wait_deadline(ENGINE_TEST_WAIT_TIMEOUT)
async def test_a_shortcut_message_sent_while_a_turn_runs_is_refused_and_never_injected(
    agent_engine: AgentEngineFactory, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    entered, release_tool = asyncio.Event(), asyncio.Event()
    started = await _start(
        agent_engine,
        monkeypatch,
        tmp_path,
        [MockResponse(tool_calls=[("hold", "hold-1", {"note": "x"})]), MockResponse(text="done")],
        tools=[_held_tool(entered, release_tool)],
    )
    engine = started.engine
    marker = tmp_path / "ran"
    typed = f'Also !`echo ran > "{marker}"`'
    ref = _user_shortcut(started, "also", typed)
    message = _shortcut_message(typed, ref)
    try:
        await engine.event_bus.publish(UserMessage(text="work"))
        await wait_for(entered.is_set, timeout=ENGINE_TURN_TIMEOUT, description="the turn holding in its tool")

        await engine.event_bus.publish(message)

        assert started.error_codes() == ["prompt_shortcut_busy"]
        assert not marker.exists()
        assert started.of(PromptCommandsRunning) == []
        assert message.started_turn is False
    finally:
        release_tool.set()
    await _drain(started)

    assert not marker.exists()
    assert [m.text for m in _user_messages(engine.history_messages)] == ["work"]
    assert not [result for result in started.of(UserInjectResult) if result.consumed]
    assert started.client.call_count == 2


# ---------------------------------------------------------------------------
# Item 8 and the engine half of item 26: the commands' busy state and its announcements (R6)
# ---------------------------------------------------------------------------


@posix_only
@pytest.mark.parametrize("outcome", ["completed", "command_failed", "cancelled"])
@with_wait_deadline(ENGINE_TEST_WAIT_TIMEOUT)
async def test_running_and_ended_pair_up_and_the_execution_is_busy_only_between_them(
    agent_engine: AgentEngineFactory, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, release: Path, outcome: str
) -> None:
    if outcome == "command_failed":
        monkeypatch.setenv("SHELL", str(tmp_path / "no-such-shell"))
    started = await _start(agent_engine, monkeypatch, tmp_path, [MockResponse(text="done")])
    engine = started.engine
    started_file = tmp_path / "started"
    typed = f"Hold !`{_held_command(started_file, release, 'HELD-OUT')}`"
    message = _shortcut_message(typed, _user_shortcut(started, "hold", typed))
    assert engine.execution_busy() is False
    since = len(started.events)

    # Busy is checked while the command is held, between the two events, and after the publish: the spec does
    # not order the busy token's start against the Running publish, so no assertion is made at either delivery.
    publish = asyncio.create_task(engine.event_bus.publish(message))
    if outcome != "command_failed":
        await wait_for(
            lambda: started_file.exists() or publish.done(),
            timeout=ENGINE_TURN_TIMEOUT,
            description="the command running",
        )
        assert not publish.done()
        # Between Running and Ended: busy, reported as a turn nobody can cancel with the turn's own control
        # (Esc withdraws the message instead), and announced.
        assert engine.execution_busy() is True
        assert engine.execution() == ExecutionSnapshot("turn", cancellable=False)
        assert len(started.of(PromptCommandsRunning)) == 1
        assert started.of(PromptCommandsEnded) == []
        await wait_for(
            lambda: "turn" in started.execution_kinds(since), description="ExecutionChanged for the commands' start"
        )
        if outcome == "completed":
            release.touch()
        else:
            await engine.event_bus.publish(UserInjectCancel(injection_id=_SHORTCUT_ID))
    await wait_for(publish.done, timeout=ENGINE_TURN_TIMEOUT, description="the message's publish to return")
    await publish
    if outcome == "completed":
        assert message.started_turn is True
        await _drain(started)
    else:
        assert message.started_turn is False

    (running,) = started.of(PromptCommandsRunning)
    (ended,) = started.of(PromptCommandsEnded)
    assert started.events.index(running) < started.events.index(ended)
    assert running.count == 1
    assert ended.outcome == outcome
    assert running.injection_id == ended.injection_id == _SHORTCUT_ID
    assert running.session_id == ended.session_id == engine.session_id
    assert engine.execution_busy() is False
    await wait_for(lambda: started.execution_kinds(since)[-1:] == ["idle"], description="ExecutionChanged back to idle")
    kinds = started.execution_kinds(since)
    # Busy from the commands' start, then idle once; a completed run goes from its commands straight into its
    # turn, with no idle in between.
    assert kinds[0] == "turn"
    assert "idle" not in kinds[:-1]


@posix_only
@with_wait_deadline(ENGINE_TEST_WAIT_TIMEOUT)
async def test_a_new_session_during_the_commands_leaves_no_busy_state_behind(
    agent_engine: AgentEngineFactory, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, release: Path
) -> None:
    started = await _start(
        agent_engine, monkeypatch, tmp_path, [MockResponse(text="seeded"), MockResponse(text="after")]
    )
    engine = started.engine
    await _send(started, UserMessage(text="seed"))
    started_file = tmp_path / "started"
    typed = f"Hold !`{_held_command(started_file, release, 'NEVER')}`"
    message = _shortcut_message(typed, _user_shortcut(started, "hold", typed))
    publish = asyncio.create_task(engine.event_bus.publish(message))
    await wait_for(
        lambda: started_file.exists() or publish.done(), timeout=ENGINE_TURN_TIMEOUT, description="the command running"
    )
    assert not publish.done()
    assert engine.execution_busy() is True

    switch = asyncio.create_task(engine.event_bus.publish(SessionNew()))
    await wait_for(
        lambda: publish.done() and switch.done(), timeout=ENGINE_TURN_TIMEOUT, description="/new and the message"
    )
    await publish
    await switch

    assert message.started_turn is False
    assert engine.execution_busy() is False
    assert engine.execution() == ExecutionSnapshot("idle")
    # The new session runs another shortcut's commands and turn, and is idle after them: no count was left over.
    since = len(started.events)
    typed_next = "Next !`echo AFTER`"
    following = _shortcut_message(typed_next, _user_shortcut(started, "next", typed_next), injection_id="next-1")
    await _send(started, following)
    assert following.started_turn is True
    assert engine.execution_busy() is False
    await wait_for(lambda: started.execution_kinds(since)[-1:] == ["idle"], description="idle after the next turn")


# ---------------------------------------------------------------------------
# Items 9 and 25: retries resend the stored expansion; the typed-text record goes everywhere the opener goes
# ---------------------------------------------------------------------------


@with_wait_deadline(ENGINE_TEST_WAIT_TIMEOUT)
async def test_a_retry_resends_the_stored_expansion_and_the_judge_reads_the_typed_text(
    agent_engine: AgentEngineFactory, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    evaluate = _spy_judge(monkeypatch)
    count = tmp_path / "count"
    typed = f'Check !`echo run >> "{count}"`!`echo OUT-RETRY`'
    sent = "Check OUT-RETRY"
    started = await _start(
        agent_engine,
        monkeypatch,
        tmp_path,
        [
            MockResponse(tool_calls=[("guarded_echo", "g-1", {"message": "first"})]),
            RuntimeError("model went away"),
            MockResponse(tool_calls=[("guarded_echo", "g-2", {"message": "second"})]),
            MockResponse(text="done"),
        ],
        tools=[_guarded_tool()],
        approval=_JUDGED,
    )
    engine = started.engine
    await engine.event_bus.publish(SetApprovalMode(mode="auto", persist=False), raise_handler_errors=True)
    message = _shortcut_message(typed, _user_shortcut(started, "check", typed))

    await _send(started, message)
    assert message.started_turn is True
    assert _lines(count) == 1
    await engine.event_bus.publish(UserRetry())
    await _drain(started)

    # The retry resent what was stored: the commands did not run again.
    assert _lines(count) == 1
    assert started.client.call_count == 4
    retry_users = _request_users(started, 2)
    assert sent in retry_users[0].text
    assert all("!`" not in user.text for user in retry_users)
    (opener,) = _user_messages(engine.history_messages)
    assert opener.text == sent
    assert opener.additional_properties.get(_TYPED_TEXT_KEY) == typed
    # The judge read what was typed on the fresh pass and on the retry, and never the command output.
    judged = _judged_inputs(evaluate)
    assert [user_message for user_message, _context in judged] == [typed, typed]
    assert all(typed in context for _user_message, context in judged)
    assert not [text for _user_message, context in judged for text in context if sent in text]


@with_wait_deadline(ENGINE_TEST_WAIT_TIMEOUT)
async def test_a_stop_after_the_commands_and_before_the_model_keeps_the_typed_text_for_the_retry(
    agent_engine: AgentEngineFactory, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Stop lands while the turn is still preparing (its rollback snapshot), after the commands ran: no request
    went out, so the opener reaches history through the failure fallback, and still records what was typed."""
    evaluate = _spy_judge(monkeypatch)
    typed, sent = "Check !`echo OUT-STOP`", "Check OUT-STOP"
    started = await _start(
        agent_engine,
        monkeypatch,
        tmp_path,
        [MockResponse(tool_calls=[("guarded_echo", "g-1", {"message": "first"})]), MockResponse(text="done")],
        tools=[_guarded_tool()],
        approval=_JUDGED,
    )
    engine = started.engine
    await engine.event_bus.publish(SetApprovalMode(mode="auto", persist=False), raise_handler_errors=True)
    entered, release = threading.Event(), threading.Event()
    capture = runner_module.capture_snapshot_writer

    def held_capture(*args: Any) -> Callable[[], None]:
        write = capture(*args)

        def held_write() -> None:
            entered.set()
            release.wait(ENGINE_TURN_TIMEOUT)
            write()

        return held_write

    monkeypatch.setattr(runner_module, "capture_snapshot_writer", held_capture)
    message = _shortcut_message(typed, _user_shortcut(started, "check", typed))
    try:
        await engine.event_bus.publish(message)
        assert message.started_turn is True
        await wait_for(entered.is_set, timeout=ENGINE_TURN_TIMEOUT, description="the turn preparing")
        await engine.event_bus.publish(UserInterrupt())
    finally:
        release.set()
    await _drain(started)

    assert started.client.call_count == 0
    (opener,) = _user_messages(engine.history_messages)
    assert opener.text == sent
    assert opener.additional_properties.get(_TYPED_TEXT_KEY) == typed

    monkeypatch.setattr(runner_module, "capture_snapshot_writer", capture)
    await engine.event_bus.publish(UserRetry())
    await _drain(started)

    assert started.client.call_count == 2
    # The retry's judge reads what was typed, never the command output.
    judged = _judged_inputs(evaluate)
    assert [user_message for user_message, _context in judged] == [typed]
    assert not [text for _user_message, context in judged for text in context if sent in text]


@with_wait_deadline(ENGINE_TEST_WAIT_TIMEOUT)
async def test_the_typed_text_record_survives_a_retry_replay_and_its_failure_fallback(
    agent_engine: AgentEngineFactory, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The first pass and the first retry's replay of the opener both fail; the second retry is judged on it."""
    evaluate = _spy_judge(monkeypatch)
    typed, sent = "Check !`echo OUT-FALLBACK`", "Check OUT-FALLBACK"
    started = await _start(
        agent_engine,
        monkeypatch,
        tmp_path,
        [
            RuntimeError("the first pass fails"),
            RuntimeError("the replay fails too"),
            MockResponse(tool_calls=[("guarded_echo", "g-1", {"message": "judged"})]),
            MockResponse(text="done"),
        ],
        tools=[_guarded_tool()],
        approval=_JUDGED,
    )
    engine = started.engine
    await engine.event_bus.publish(SetApprovalMode(mode="auto", persist=False), raise_handler_errors=True)

    def openers() -> list[tuple[str, object]]:
        return [(m.text, m.additional_properties.get(_TYPED_TEXT_KEY)) for m in _user_messages(engine.history_messages)]

    await _send(started, _shortcut_message(typed, _user_shortcut(started, "check", typed)))
    assert openers() == [(sent, typed)]
    await engine.event_bus.publish(UserRetry())
    await _drain(started)
    assert started.client.call_count == 2
    assert openers() == [(sent, typed)]
    await engine.event_bus.publish(UserRetry())
    await _drain(started)

    assert started.client.call_count == 4
    assert openers() == [(sent, typed)]
    assert sent in _request_users(started, 2)[0].text
    assert [user_message for user_message, _context in _judged_inputs(evaluate)] == [typed]
    session_id = engine.session_id
    assert session_id is not None
    saved = await started.store.load_session(session_id)
    assert saved is not None
    assert [(m.text, m.additional_properties.get(_TYPED_TEXT_KEY)) for m in _user_messages(saved["messages"])] == [
        (sent, typed)
    ]


# ---------------------------------------------------------------------------
# Items 10 and 25: the opener is never duplicated by a recovery checkpoint or a failed pass
# ---------------------------------------------------------------------------


@with_wait_deadline(ENGINE_TEST_WAIT_TIMEOUT)
async def test_a_recovery_checkpoint_keeps_one_opener_with_the_sent_text_and_its_record(
    agent_engine: AgentEngineFactory, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    entered, release_tool = asyncio.Event(), asyncio.Event()
    started = await _start(
        agent_engine,
        monkeypatch,
        tmp_path,
        [MockResponse(tool_calls=[("hold", "hold-1", {"note": "x"})]), MockResponse(text="done")],
        tools=[_held_tool(entered, release_tool)],
    )
    engine = started.engine
    typed, sent = "Keep !`echo OUT-CHECKPOINT`", "Keep OUT-CHECKPOINT"
    try:
        await engine.event_bus.publish(_shortcut_message(typed, _user_shortcut(started, "keep", typed)))
        await wait_for(entered.is_set, timeout=ENGINE_TURN_TIMEOUT, description="the turn holding in its tool")
        session_id = engine.session_id
        assert session_id is not None

        assert await engine.writer.persist_now() is True
        recovered = await started.store.load_recovery_session(session_id)
    finally:
        release_tool.set()
    await _drain(started)

    assert recovered is not None
    assert [(m.text, m.additional_properties.get(_TYPED_TEXT_KEY)) for m in _user_messages(recovered["messages"])] == [
        (sent, typed)
    ]


@with_wait_deadline(ENGINE_TEST_WAIT_TIMEOUT)
async def test_a_failed_pass_keeps_one_opener_with_the_sent_text_and_its_record(
    agent_engine: AgentEngineFactory, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    started = await _start(agent_engine, monkeypatch, tmp_path, [RuntimeError("model went away")])
    engine = started.engine
    typed, sent = "Keep !`echo OUT-FAILED`", "Keep OUT-FAILED"

    message = _shortcut_message(typed, _user_shortcut(started, "keep", typed))
    await _send(started, message)

    assert message.started_turn is True
    assert [
        (m.text, m.additional_properties.get(_TYPED_TEXT_KEY)) for m in _user_messages(engine.history_messages)
    ] == [(sent, typed)]
    session_id = engine.session_id
    assert session_id is not None
    saved = await started.store.load_session(session_id)
    assert saved is not None
    assert [m.text for m in _user_messages(saved["messages"])] == [sent]


# ---------------------------------------------------------------------------
# Item 25: the typed-text record never reaches the wire, on any protocol
# ---------------------------------------------------------------------------

_WIRE_MODEL = "wire-model"


def _wire_answers(provider: str, api_style: str) -> tuple[Reply, ...]:
    if provider == "anthropic":
        return anth_replies(
            [anth_text("first", message_id="msg_1"), anth_text("next", message_id="msg_2")], stream=False
        )
    if api_style == API_STYLE_RESPONSES:
        return resp_replies(
            [
                resp_response(response_id="resp_1", output=[resp_message("msg_1", "first")]),
                resp_response(response_id="resp_2", output=[resp_message("msg_2", "next")]),
            ],
            stream=False,
        )
    return cc_replies(
        [cc_text("first", response_id="chatcmpl_1"), cc_text("next", response_id="chatcmpl_2")], stream=False
    )


@pytest.mark.parametrize(
    ("provider", "api_style"),
    [
        ("openai", API_STYLE_CHAT_COMPLETIONS),
        ("openai", API_STYLE_RESPONSES),
        ("anthropic", API_STYLE_CHAT_COMPLETIONS),
    ],
    ids=["chat-completions", "responses", "anthropic-messages"],
)
@with_wait_deadline(ENGINE_TEST_WAIT_TIMEOUT)
async def test_the_typed_text_record_never_reaches_the_wire(
    agent_engine: AgentEngineFactory, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, provider: str, api_style: str
) -> None:
    pin_wire_inputs(monkeypatch)
    wire = ScriptedWire(_wire_answers(provider, api_style))
    route_clients_to(wire.transport, monkeypatch)
    models = ModelProfileRegistry()
    models.register(
        ModelProfile(
            id=_WIRE_MODEL,
            name=_WIRE_MODEL,
            provider=provider,
            api_style=api_style,
            model_id="model-test",
            api_key="sk-test",
            base_url="https://model.example.test",
            http_max_retries=0,
            stream=False,
        )
    )
    profile = parent_profile(sub_agent=False)
    agents = AgentProfileRegistry()
    agents.register(profile)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    engine = agent_engine(
        EventBus(),
        settings=Settings(model_profile=_WIRE_MODEL, workspace_change_notice=False),
        agent_registry=agents,
        model_registry=models,
        state_store=JsonFileStateStore(tmp_path / "sessions"),
        initial_workspace=Workspace.from_cwd(str(workspace)),
    )
    await engine.start(profile)
    typed, sent = "Report: !`echo WIRE-OUT`", "Report: WIRE-OUT"
    _write_shortcut(global_shortcuts_dir(get_platform().config_dir), "report", typed)
    message = _shortcut_message(typed, _loaded_ref(engine, "report"))

    # The second turn sends the first one's opener again, from history.
    for event in (message, UserMessage(text="and then")):
        await engine.event_bus.publish(event)
        assert event.started_turn is True
        await await_run_task_chain(engine, turn_state=engine.turns.turn_state)

    (opener, _plain) = _user_messages(engine.history_messages)
    assert opener.additional_properties.get(_TYPED_TEXT_KEY) == typed
    assert len(wire.requests) == 2
    for request in wire.requests:
        body = request.content.decode("utf-8")
        assert sent in body
        assert "!`" not in body
        assert _TYPED_TEXT_KEY not in body


# ---------------------------------------------------------------------------
# Item 16: with_message_text
# ---------------------------------------------------------------------------


def test_with_message_text_replaces_the_leading_text_and_keeps_the_images() -> None:
    image = Content.from_data(data=image_bytes(), media_type="image/png")

    replaced = with_message_text(["typed", image], "sent")

    assert replaced[0] == "sent"
    assert len(replaced) == 2
    assert replaced[1] is image


def test_with_message_text_refuses_contents_not_led_by_text() -> None:
    # Read narrowly: contents led by an image. Empty contents and a ``Content`` text part first are left out;
    # the spec says only that a non-``str`` first element raises, and no caller builds either (R4 contents
    # are always ``[text, *images]``).
    image = Content.from_data(data=image_bytes(), media_type="image/png")

    with pytest.raises(ValueError):
        with_message_text([image, "typed"], "sent")
