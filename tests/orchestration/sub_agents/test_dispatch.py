# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for :class:`SubAgentTools`' per-invocation dispatch.

When the user clicks Retry / Abort on a paused sub-agent card, the engine
routes the event by ``invocation_id`` through the ``SubAgentTools``
registry (:attr:`_controllers`). The tools module is a thin dispatcher:
it looks up the right :class:`SubAgentToolShell` and calls the matching
method. Failure modes (missing id, already-resolved, concurrent live
controllers) must all route cleanly.

Here we install fake controllers directly into the registry so the tests
don't depend on an actual ``agent.run()``. Controller internals are
covered in ``tests/orchestration/sub_agents/test_controller.py``.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from unittest.mock import AsyncMock, create_autospec

import pytest

import chrys.orchestration.sub_agents.tools as sub_agent_module
import chrys.service.skills.adapter as skills_adapter
from chrys.foundation.config.settings import Settings
from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import (
    ApprovalAutoFulfillBlocked,
    InvocationCompactionCommitted,
    InvocationCompactionFinished,
    InvocationCompactionStarted,
    InvocationRetryAttempt,
    InvocationStarted,
)
from chrys.foundation.models.session_env import SessionEnvironment
from chrys.foundation.platform import get_platform
from chrys.foundation.retry import RetryAttemptInfo
from chrys.kernel import AgentSession
from chrys.orchestration.invoker import runtime as runtime_factory_module
from chrys.orchestration.invoker.resources import PreparedAgent
from chrys.orchestration.sub_agents.tools import SubAgentTools
from chrys.service.agent_middleware.control.approval import ApprovalMiddleware
from chrys.service.agent_middleware.system_reminder import SystemReminderMiddleware
from chrys.service.approval.policy import ApprovalMode, ApprovalPolicy
from chrys.service.context.compaction.last_words import CompactionStatus
from chrys.service.context.compaction.spill import SpillQuota
from chrys.service.context.middleware.usage import UsageTrackingMiddleware
from chrys.service.llm.mock import MockChatClient
from chrys.service.profiles.agents.schema import (
    AcpAgentConfig,
    AgentProfile,
    ApprovalConfig,
    CompactionConfig,
    SubAgentRef,
    ToolsConfig,
)
from chrys.service.profiles.models.schema import ModelProfile
from chrys.service.session.sub_agent_logs import sessions_dir
from tests.orchestration.invoker._runtime_fixtures import SubAgentPolicyDouble, runtime_for_shell
from tests.support.tool_packs import install_tools


@dataclass
class _FakeController:
    """Minimal stand-in for :class:`SubAgentToolShell`.

    Records which dispatch method was called so tests can assert routing.
    ``request_retry`` / ``request_abort`` mirror the real controller's
    synchronous API returning a bool; ``cascade_abort`` is async.
    """

    invocation_id: str
    retry_calls: int = 0
    abort_calls: int = 0
    cascade_calls: int = 0
    paused: bool = True
    retry_returns: bool = True
    abort_returns: bool = True
    published: list[str] = field(default_factory=list)

    def request_retry(self) -> bool:
        self.retry_calls += 1
        return self.retry_returns if self.paused else False

    def request_abort(self) -> bool:
        self.abort_calls += 1
        return self.abort_returns if self.paused else False

    async def cascade_abort(self) -> None:
        self.cascade_calls += 1


def _make_tools() -> SubAgentTools:
    return SubAgentTools(event_bus=EventBus(), session_id="s-1", session_dir=None)


# --- request_retry / request_abort dispatch ----------------------------


def test_request_retry_routes_to_registered_controller() -> None:
    tools = _make_tools()
    ctrl_a = _FakeController("a")
    ctrl_b = _FakeController("b")
    tools._controllers["a"] = ctrl_a  # type: ignore[assignment]
    tools._controllers["b"] = ctrl_b  # type: ignore[assignment]

    assert tools.request_retry("a") is True
    assert ctrl_a.retry_calls == 1
    assert ctrl_b.retry_calls == 0


def test_request_abort_routes_to_registered_controller() -> None:
    tools = _make_tools()
    ctrl = _FakeController("inv-1")
    tools._controllers["inv-1"] = ctrl  # type: ignore[assignment]

    assert tools.request_abort("inv-1") is True
    assert ctrl.abort_calls == 1
    assert ctrl.retry_calls == 0


def test_request_retry_unknown_invocation_returns_false() -> None:
    """No controller for the id (finished or never existed) → False, no crash."""
    tools = _make_tools()
    assert tools.request_retry("ghost") is False
    assert tools.request_abort("ghost") is False


def test_request_retry_on_non_paused_controller_returns_false() -> None:
    """Controller exists but isn't paused (e.g. already resumed)."""
    tools = _make_tools()
    ctrl = _FakeController("inv-1", paused=False)
    tools._controllers["inv-1"] = ctrl  # type: ignore[assignment]

    assert tools.request_retry("inv-1") is False
    assert ctrl.retry_calls == 1  # dispatch still happened


# --- cascade_abort_all ---------------------------------------------------


@pytest.mark.asyncio
async def test_cascade_abort_all_calls_every_controller() -> None:
    tools = _make_tools()
    controllers = [_FakeController(f"inv-{i}") for i in range(3)]
    for c in controllers:
        tools._controllers[c.invocation_id] = c  # type: ignore[assignment]

    await tools.cascade_abort_all()

    assert all(c.cascade_calls == 1 for c in controllers)


@pytest.mark.asyncio
async def test_cascade_abort_all_snapshot_safe_against_concurrent_removal() -> None:
    """``cascade_abort_all`` iterates a *copy* of the registry values —
    controllers that remove themselves mid-cascade must not break the loop."""
    tools = _make_tools()

    @dataclass
    class _SelfRemoving:
        invocation_id: str
        cascade_calls: int = 0

        async def cascade_abort(self) -> None:
            self.cascade_calls += 1
            # Simulate the real controller's finally-block popping itself.
            tools._controllers.pop(self.invocation_id, None)

    a = _SelfRemoving("a")
    b = _SelfRemoving("b")
    tools._controllers["a"] = a  # type: ignore[assignment]
    tools._controllers["b"] = b  # type: ignore[assignment]

    await tools.cascade_abort_all()

    assert a.cascade_calls == 1
    assert b.cascade_calls == 1
    assert tools._controllers == {}


@pytest.mark.asyncio
async def test_cascade_abort_all_on_empty_registry_is_noop() -> None:
    tools = _make_tools()
    await tools.cascade_abort_all()  # must not raise


async def test_cascade_abort_all_snapshots_and_gathers_failures() -> None:
    tools = SubAgentTools()

    class Controller:
        status = "running"

        def request_retry(self) -> bool:
            return False

        def request_abort(self) -> bool:
            return False

        async def cascade_abort(self) -> None:
            raise RuntimeError("boom")

    tools._controllers = {"a": Controller(), "b": Controller()}
    await tools.cascade_abort_all()


# --- persist_dir layout --------------------------------------------------


def test_pending_dir_is_none_without_session_dir() -> None:
    tools = _make_tools()
    assert tools._pending_dir() is None


def test_pending_dir_is_session_dir_sub_agents_pending(tmp_path) -> None:
    tools = SubAgentTools(event_bus=EventBus(), session_id="s-1", session_dir=tmp_path)
    assert tools._pending_dir() == tmp_path / "sub_agents" / "pending"


# --- invocation reminder preparation ------------------------------------


@pytest.mark.asyncio
async def test_parallel_sub_agent_invocations_prepare_independent_runtime_snapshots(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each sub-agent invocation prepares runtime-only reminders in its own task context."""
    import chrys.orchestration.sub_agents.tools as sub_agent_module

    class _RecordingReminder(SystemReminderMiddleware):
        def __init__(self) -> None:
            super().__init__(runtime=object())
            self.prepare_calls: list[dict[str, object]] = []
            self._next_runtime_id = 0
            self._runtime_label = ""
            monkeypatch.setattr(self.sources.runtime_env, "snapshot", lambda: self._runtime_label)

        def prepare_turn(self, **kwargs: object) -> None:
            self._next_runtime_id += 1
            self._runtime_label = f"runtime-{self._next_runtime_id}"
            self.prepare_calls.append(kwargs)
            super().prepare_turn(**kwargs)

    @dataclass
    class _Agent:
        client = MockChatClient()

        reminder: _RecordingReminder

        def create_session(self) -> AgentSession:
            return AgentSession()

    reminder = _RecordingReminder()
    ready = asyncio.Event()
    started = 0
    observations: list[tuple[str, str, str]] = []

    class _Controller(SubAgentPolicyDouble):
        def __init__(self, *, shell, agent: _Agent, prompt: str, **_kwargs: object) -> None:
            super().__init__(shell=shell, prompt=prompt)
            self.agent = agent
            self.prompt = prompt

        async def execute(self) -> str:
            nonlocal started
            # The runtime environment is the last reminder this turn offers.
            initial = self.agent.reminder._build_reminders()[-1]
            started += 1
            if started == 2:
                ready.set()
            await ready.wait()
            after_overlap = self.agent.reminder._build_reminders()[-1]
            observations.append((self.prompt, initial, after_overlap))
            return after_overlap

    monkeypatch.setattr(sub_agent_module, "KernelSubAgentPolicy", _Controller)

    tools = SubAgentTools(max_total_concurrency=2, event_bus=None, session_id="s-1", session_dir=None)
    tools._agent_max["Explore"] = 2
    tools._agent_active["Explore"] = 0
    tool = tools._make_tool(
        "Explore",
        "Delegate to Explore",
        prepared=PreparedAgent(),
        runtime_factory=runtime_for_shell(_Agent(reminder=reminder), reminder),
        stream=False,
        stream_attempt_timeout=1.0,
    )

    results = await asyncio.gather(tool.func(prompt="one"), tool.func(prompt="two"))

    assert reminder.prepare_calls == [{}, {}]
    assert sorted(observations) == [
        ("one", "runtime-1", "runtime-1"),
        ("two", "runtime-2", "runtime-2"),
    ]
    assert sorted(results) == ["runtime-1", "runtime-2"]
    assert tools._total_active == 0
    assert tools._agent_active["Explore"] == 0


@pytest.mark.asyncio
async def test_sub_agent_concurrency_limit_rejects_excess_and_releases_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    active = 0

    class _Controller(SubAgentPolicyDouble):
        def __init__(self, *, shell, prompt: str, **_kwargs: object) -> None:
            super().__init__(shell=shell, prompt=prompt)
            return

        async def execute(self) -> str:
            nonlocal active
            active += 1
            if active == 2:
                started.set()
            try:
                await release.wait()
                return "done"
            finally:
                active -= 1

    monkeypatch.setattr(sub_agent_module, "KernelSubAgentPolicy", _Controller)

    tools = SubAgentTools(max_total_concurrency=2, event_bus=None, session_id="s-1", session_dir=None)
    tools._agent_max["Explore"] = 3
    tools._agent_active["Explore"] = 0

    class _Reminder:
        def prepare_turn(self) -> None:
            return None

    class _Agent:
        client = MockChatClient()

        def create_session(self) -> AgentSession:
            return AgentSession()

    tool = tools._make_tool(
        "Explore",
        "Delegate to Explore",
        prepared=PreparedAgent(),
        runtime_factory=runtime_for_shell(_Agent(), _Reminder()),
        stream=False,
        stream_attempt_timeout=1.0,
    )
    first = asyncio.create_task(tool.func(prompt="one"))
    second = asyncio.create_task(tool.func(prompt="two"))
    await asyncio.wait_for(started.wait(), timeout=5)

    rejected = await tool.func(prompt="three")

    assert rejected.startswith("Error: total sub-agent concurrency limit reached (2/2 active).")
    release.set()
    assert await asyncio.gather(first, second) == ["done", "done"]
    assert await tool.func(prompt="four") == "done"
    assert tools._total_active == 0
    assert tools._agent_active["Explore"] == 0


@pytest.mark.asyncio
async def test_local_sub_agent_missing_limit_uses_shared_default() -> None:
    tools = SubAgentTools(max_total_concurrency=10, event_bus=None, session_id="s-1", session_dir=None)
    tools._agent_active["Explore"] = 3

    class _Reminder:
        def prepare_turn(self) -> None:
            return None

    class _Agent:
        client = MockChatClient()

        def create_session(self) -> AgentSession:
            return AgentSession()

    tool = tools._make_tool(
        "Explore",
        "Delegate to Explore",
        prepared=PreparedAgent(),
        runtime_factory=runtime_for_shell(_Agent(), _Reminder()),
        stream=False,
        stream_attempt_timeout=1.0,
    )

    rejected = await tool.func(prompt="blocked")

    assert rejected.startswith("Error: concurrency limit for 'Explore' reached (3/3 active).")


@pytest.mark.asyncio
async def test_acp_sub_agent_missing_limit_uses_shared_default() -> None:
    tools = SubAgentTools(max_total_concurrency=10, event_bus=None, session_id="s-1", session_dir=None)
    tools._agent_active["Explore"] = 3
    runtime = SessionEnvironment(cwd="", platform=get_platform())
    tool = tools._make_acp_tool(
        "Explore",
        "Delegate to Explore",
        AgentProfile(name="Explore"),
        runtime,
    )

    rejected = await tool.func(prompt="blocked")

    assert rejected.startswith("Error: concurrency limit for 'Explore' reached (3/3 active).")


@pytest.mark.asyncio
async def test_sub_agent_invocation_closes_approval_middleware(monkeypatch: pytest.MonkeyPatch) -> None:
    """Per-invocation approval middleware should not leave bus handlers behind."""
    import chrys.orchestration.sub_agents.tools as sub_agent_module

    bus = EventBus()
    controller_kwargs: dict[str, object] = {}

    class _Controller(SubAgentPolicyDouble):
        def __init__(self, *, shell, prompt: str, run_kwargs: dict, **kwargs: object) -> None:
            super().__init__(shell=shell, prompt=prompt)
            self.run_kwargs = run_kwargs
            controller_kwargs.update(kwargs)

        async def execute(self) -> str:
            approval_mw = next(m for m in self.run_kwargs["middleware"] if isinstance(m, ApprovalMiddleware))
            await approval_mw._ensure_auto_fulfill_block_subscription()
            assert approval_mw._on_auto_fulfill_blocked in bus._handlers[ApprovalAutoFulfillBlocked]
            return "done"

    monkeypatch.setattr(sub_agent_module, "KernelSubAgentPolicy", _Controller)

    tools = SubAgentTools(
        max_total_concurrency=1,
        event_bus=bus,
        session_id="s-1",
        session_dir=None,
        approval_mode=ApprovalMode.AUTO,
        max_transient_retries=9,
    )
    tools._agent_max["Explore"] = 1
    tools._agent_active["Explore"] = 0
    tools._approval_policies["Explore"] = ApprovalPolicy(ApprovalConfig(default="require"))

    class _Reminder:
        def prepare_turn(self) -> None:
            pass

    class _Agent:
        client = MockChatClient()

        def create_session(self) -> AgentSession:
            return AgentSession()

    reminder = _Reminder()
    tool = tools._make_tool(
        "Explore",
        "Delegate to Explore",
        prepared=PreparedAgent(),
        runtime_factory=runtime_for_shell(_Agent(), reminder),
        stream=False,
        stream_attempt_timeout=1.0,
    )

    assert await tool.func(prompt="hi") == "done"
    assert controller_kwargs["max_retries"] == 9
    assert bus._handlers[ApprovalAutoFulfillBlocked] == []


@pytest.mark.asyncio
async def test_sub_agent_cleanup_continues_when_usage_drain_is_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancellation during usage-drain cleanup must not leak invocation counters."""
    import chrys.orchestration.sub_agents.tools as sub_agent_module

    class _Controller(SubAgentPolicyDouble):
        def __init__(self, *, shell, prompt: str, **kwargs: object) -> None:
            super().__init__(shell=shell, prompt=prompt)
            assert "max_retries" not in kwargs
            return

        async def execute(self) -> str:
            return "done"

    monkeypatch.setattr(sub_agent_module, "KernelSubAgentPolicy", _Controller)

    async def _cancel_drain() -> None:
        raise asyncio.CancelledError

    tools = SubAgentTools(
        max_total_concurrency=1,
        event_bus=None,
        session_id="s-1",
        session_dir=None,
        drain_parent_usage_publishes=_cancel_drain,
    )
    tools._agent_max["Explore"] = 1
    tools._agent_active["Explore"] = 0

    class _Reminder:
        def prepare_turn(self) -> None:
            return None

    class _Agent:
        client = MockChatClient()

        def create_session(self) -> AgentSession:
            return AgentSession()

    tool = tools._make_tool(
        "Explore",
        "Delegate to Explore",
        prepared=PreparedAgent(),
        runtime_factory=runtime_for_shell(_Agent(), _Reminder()),
        stream=False,
        stream_attempt_timeout=1.0,
    )

    with pytest.raises(asyncio.CancelledError):
        await tool.func(prompt="hi")

    assert tools._controllers == {}
    assert tools._total_active == 0
    assert tools._agent_active["Explore"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("runner", ["kernel", "acp"])
async def test_pre_controller_hook_cancellation_keeps_terminal_audit_discoverable(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    runner: str,
) -> None:
    """InvocationStart owns the stable audit reference before controller construction."""
    bus = EventBus()
    starts: list[InvocationStarted] = []
    hook_entered = asyncio.Event()

    async def capture_start(event: InvocationStarted) -> None:
        starts.append(event)

    async def block_start_hook(*_args: object, **_kwargs: object) -> None:
        hook_entered.set()
        await asyncio.Event().wait()

    await bus.subscribe(InvocationStarted, capture_start)
    tools = SubAgentTools(
        max_total_concurrency=1,
        event_bus=bus,
        session_id="s-1",
        session_dir=tmp_path,
        workspace_cwd=str(tmp_path),
    )
    monkeypatch.setattr(tools, "_fire_sub_agent_hook", block_start_hook)
    tools._agent_active["Explore"] = 0
    tools._agent_max["Explore"] = 1
    tools._display_names["Explore"] = "Explore Agent"
    tools._hook_profile_names["Explore"] = "Explore"

    if runner == "acp":
        runtime = SessionEnvironment(cwd=str(tmp_path), platform=get_platform())
        profile = AgentProfile(
            name="Explore",
            display_name="Explore Agent",
            acp=AcpAgentConfig(command="stub-agent"),
        )
        tools._prepared_by_tool["Explore"] = PreparedAgent()
        tool = tools._make_acp_tool("Explore", "Delegate to Explore", profile, runtime)
    else:

        class _Reminder:
            def prepare_turn(self) -> None:
                return None

        class _Agent:
            client = MockChatClient()

            def create_session(self) -> AgentSession:
                return AgentSession()

        tool = tools._make_tool(
            "Explore",
            "Delegate to Explore",
            prepared=PreparedAgent(),
            runtime_factory=runtime_for_shell(_Agent(), _Reminder()),
            stream=False,
            stream_attempt_timeout=1.0,
        )

    invocation = asyncio.create_task(tool.func(prompt="inspect"))
    await asyncio.wait_for(hook_entered.wait(), timeout=5)

    assert len(starts) == 1
    assert starts[0].sub_agent_log_file

    invocation.cancel()
    with pytest.raises(asyncio.CancelledError):
        await invocation

    envelope = json.loads((sessions_dir(tmp_path) / starts[0].sub_agent_log_file).read_text(encoding="utf-8"))
    assert envelope["meta"]["status"] == "cancelled"
    assert envelope["meta"]["ended_at"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("exc", "expected_type"),
    [
        (RuntimeError("skill refresh failed"), RuntimeError),
        (asyncio.CancelledError(), asyncio.CancelledError),
    ],
    ids=["exception", "cancellation"],
)
async def test_sub_agent_cleanup_handles_failure_before_event_middleware_assignment(
    monkeypatch: pytest.MonkeyPatch,
    exc: BaseException,
    expected_type: type[BaseException],
) -> None:
    """Early failures before middleware creation must still release invocation counters."""
    tools = SubAgentTools(
        max_total_concurrency=1,
        event_bus=None,
        session_id="s-1",
        session_dir=None,
    )
    tools._agent_max["Explore"] = 1
    tools._agent_active["Explore"] = 0

    async def _fail_refresh(_provider: object, _tool_name: str) -> None:
        raise exc

    monkeypatch.setattr(tools, "_refresh_skill_catalog", _fail_refresh)

    class _Reminder:
        def prepare_turn(self) -> None:
            return None

    class _Agent:
        client = MockChatClient()

        def create_session(self) -> AgentSession:
            return AgentSession()

    tool = tools._make_tool(
        "Explore",
        "Delegate to Explore",
        prepared=PreparedAgent(),
        runtime_factory=runtime_for_shell(_Agent(), _Reminder()),
        stream=False,
        stream_attempt_timeout=1.0,
        skills_provider=object(),
    )

    with pytest.raises(expected_type):
        await tool.func(prompt="hi")

    assert tools._controllers == {}
    assert tools._total_active == 0
    assert tools._agent_active["Explore"] == 0


@pytest.mark.asyncio
async def test_sub_agent_last_words_retry_callback_publishes_current_invocation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """LAST_WORDS retry events should attach to the live sub-agent invocation."""
    bus = EventBus()
    starts: list[InvocationStarted] = []
    retries: list[InvocationRetryAttempt] = []
    captured_publish_retry = None

    async def _capture_start(event: InvocationStarted) -> None:
        starts.append(event)

    async def _capture_retry(event: InvocationRetryAttempt) -> None:
        retries.append(event)

    await bus.subscribe(InvocationStarted, _capture_start)
    await bus.subscribe(InvocationRetryAttempt, _capture_retry)

    class _Agent:
        def __init__(self, **kwargs: object) -> None:
            self.client = kwargs.get("client")
            self.context_providers = list(kwargs.get("context_providers") or [])
            self.middleware = list(kwargs.get("middleware") or [])
            self.tools = list(kwargs.get("tools") or [])
            self.name = kwargs.get("name")
            self.instructions = kwargs.get("instructions")

        async def __aenter__(self) -> _Agent:
            return self

        async def __aexit__(self, *_exc: object) -> None:
            return None

        def create_session(self) -> AgentSession:
            return AgentSession()

    class _LastWordsGenerator:
        def __init__(self, *, publish_retry, **_kwargs: object) -> None:  # type: ignore[no-untyped-def]
            nonlocal captured_publish_retry
            captured_publish_retry = publish_retry

        async def aclose(self) -> None:
            pass

    class _Controller(SubAgentPolicyDouble):
        def __init__(self, *, shell, prompt: str, **_kwargs: object) -> None:
            super().__init__(shell=shell, prompt=prompt)
            return

        async def execute(self) -> str:
            assert captured_publish_retry is not None
            await captured_publish_retry(
                RetryAttemptInfo(reason="connection dropped", attempt=1, max_attempts=5, delay_seconds=3)
            )
            return "done"

    install_tools(monkeypatch)
    monkeypatch.setattr(runtime_factory_module, "Agent", _Agent)
    monkeypatch.setattr(runtime_factory_module, "LastWordsGenerator", _LastWordsGenerator)
    monkeypatch.setattr(sub_agent_module, "KernelSubAgentPolicy", _Controller)
    monkeypatch.setattr(
        sub_agent_module,
        "create_client",
        create_autospec(sub_agent_module.create_client, side_effect=lambda *_args, **_kwargs: MockChatClient()),
    )
    monkeypatch.setattr(skills_adapter, "create_skills_provider", AsyncMock(return_value=(None, [])))

    tools = SubAgentTools(event_bus=bus, session_id="s-1", session_dir=tmp_path)
    runtime = SessionEnvironment(cwd=str(tmp_path), platform=get_platform())
    await tools.register(
        SubAgentRef(profile="Explore", tool_name="Explore"),
        AgentProfile(name="Explore", display_name="Explore Agent", tools=ToolsConfig(builtins=[])),
        runtime,
        settings=Settings(),
        fallback_profile=ModelProfile(id="mock", name="mock", provider="mock", model_id="mock"),
    )

    result = await tools.get_tools()[0].func(prompt="hi")

    assert result == "done"
    assert len(starts) == 1
    assert len(retries) == 1
    assert retries[0].agent_name == "Explore Agent"
    assert retries[0].origin.invocation_id == starts[0].origin.invocation_id
    assert retries[0].message == "LAST_WORDS compaction: connection dropped"
    assert retries[0].attempt == 1
    assert retries[0].max_attempts == 5
    assert retries[0].delay_seconds == 3
    assert retries[0].session_id == "s-1"


@pytest.mark.asyncio
async def test_sub_agent_compaction_status_callback_publishes_current_invocation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """Phase-4 status signals surface as SubAgentCompaction* events on the live invocation."""
    bus = EventBus()
    starts: list[InvocationStarted] = []
    compaction_started: list[InvocationCompactionStarted] = []
    compaction_finished: list[InvocationCompactionFinished] = []
    compaction_committed: list[InvocationCompactionCommitted] = []
    captured_publish_status = None
    captured_generator_kwargs: list[dict[str, object]] = []

    async def _capture_start(event: InvocationStarted) -> None:
        starts.append(event)

    async def _capture_compaction_started(event: InvocationCompactionStarted) -> None:
        compaction_started.append(event)

    async def _capture_compaction_finished(event: InvocationCompactionFinished) -> None:
        compaction_finished.append(event)

    async def _capture_compaction_committed(event: InvocationCompactionCommitted) -> None:
        compaction_committed.append(event)

    await bus.subscribe(InvocationStarted, _capture_start)
    await bus.subscribe(InvocationCompactionStarted, _capture_compaction_started)
    await bus.subscribe(InvocationCompactionFinished, _capture_compaction_finished)
    await bus.subscribe(InvocationCompactionCommitted, _capture_compaction_committed)

    class _Agent:
        def __init__(self, **kwargs: object) -> None:
            self.client = kwargs.get("client")
            self.context_providers = list(kwargs.get("context_providers") or [])
            self.middleware = list(kwargs.get("middleware") or [])
            self.tools = list(kwargs.get("tools") or [])
            self.name = kwargs.get("name")
            self.instructions = kwargs.get("instructions")

        async def __aenter__(self) -> _Agent:
            return self

        async def __aexit__(self, *_exc: object) -> None:
            return None

        def create_session(self) -> AgentSession:
            return AgentSession()

    class _LastWordsGenerator:
        def __init__(self, *, publish_status=None, **kwargs: object) -> None:  # type: ignore[no-untyped-def]
            nonlocal captured_publish_status
            captured_publish_status = publish_status
            captured_generator_kwargs.append(kwargs)

        async def aclose(self) -> None:
            pass

    class _Controller(SubAgentPolicyDouble):
        def __init__(self, *, shell, prompt: str, **_kwargs: object) -> None:
            super().__init__(shell=shell, prompt=prompt)
            return

        async def execute(self) -> str:
            assert captured_publish_status is not None
            await captured_publish_status(CompactionStatus(compaction_id="c-1", stage="started"))
            await captured_publish_status(
                CompactionStatus(
                    compaction_id="c-1",
                    stage="finished",
                    outcome="ok",
                    duration_ms=2500,
                    format_violation='missing required heading "## Next"',
                )
            )
            await captured_publish_status(CompactionStatus(compaction_id="c-1", stage="committed"))
            return "done"

    install_tools(monkeypatch)
    monkeypatch.setattr(runtime_factory_module, "Agent", _Agent)
    monkeypatch.setattr(runtime_factory_module, "LastWordsGenerator", _LastWordsGenerator)
    monkeypatch.setattr(sub_agent_module, "KernelSubAgentPolicy", _Controller)
    monkeypatch.setattr(
        sub_agent_module,
        "create_client",
        create_autospec(sub_agent_module.create_client, side_effect=lambda *_args, **_kwargs: MockChatClient()),
    )
    monkeypatch.setattr(skills_adapter, "create_skills_provider", AsyncMock(return_value=(None, [])))

    tools = SubAgentTools(
        event_bus=bus,
        session_id="s-1",
        session_dir=tmp_path,
        max_transient_retries=9,
    )
    runtime = SessionEnvironment(cwd=str(tmp_path), platform=get_platform())
    supplement = "Preserve exact sub-agent search evidence."
    await tools.register(
        SubAgentRef(profile="Explore", tool_name="Explore"),
        AgentProfile(
            name="Explore",
            display_name="Explore Agent",
            tools=ToolsConfig(builtins=[]),
            compaction=CompactionConfig(last_words_template=supplement),
        ),
        runtime,
        settings=Settings(),
        fallback_profile=ModelProfile(id="mock", name="mock", provider="mock", model_id="mock"),
    )

    result = await tools.get_tools()[0].func(prompt="hi")

    assert result == "done"
    assert len(starts) == 1
    assert len(compaction_started) == 1
    assert compaction_started[0].agent_name == "Explore Agent"
    assert compaction_started[0].origin.invocation_id == starts[0].origin.invocation_id
    assert compaction_started[0].compaction_id == "c-1"
    assert compaction_started[0].session_id == "s-1"
    assert len(compaction_finished) == 1
    assert compaction_finished[0].origin.invocation_id == starts[0].origin.invocation_id
    assert compaction_finished[0].compaction_id == "c-1"
    assert compaction_finished[0].outcome == "ok"
    assert compaction_finished[0].duration_ms == 2500
    assert compaction_finished[0].format_violation == 'missing required heading "## Next"'
    assert compaction_finished[0].session_id == "s-1"
    # The committed stage maps to its own event — never a second finish.
    assert len(compaction_committed) == 1
    assert compaction_committed[0].agent_name == "Explore Agent"
    assert compaction_committed[0].origin.invocation_id == starts[0].origin.invocation_id
    assert compaction_committed[0].compaction_id == "c-1"
    assert compaction_committed[0].session_id == "s-1"
    assert len(captured_generator_kwargs) == 2
    assert all(kwargs["template"] == supplement for kwargs in captured_generator_kwargs)
    assert all(kwargs["max_transient_retries"] == 9 for kwargs in captured_generator_kwargs)


@pytest.mark.asyncio
async def test_registered_parallel_invocations_get_independent_compaction_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """Same-tool concurrent invocations must not share usage/compaction strategy objects."""

    class _Agent:
        def __init__(self, **kwargs: object) -> None:
            self.client = kwargs.get("client")
            self.context_providers = list(kwargs.get("context_providers") or [])
            self.middleware = list(kwargs.get("middleware") or [])
            self.tools = list(kwargs.get("tools") or [])
            self.name = kwargs.get("name")
            self.instructions = kwargs.get("instructions")

        async def __aenter__(self) -> _Agent:
            return self

        async def __aexit__(self, *_exc: object) -> None:
            return None

        def create_session(self) -> AgentSession:
            return AgentSession()

    seen: list[tuple[object, object, object]] = []
    usage_reports: list[tuple[object, ...]] = []
    aggregate_flags: list[tuple[bool, bool]] = []
    both_started = asyncio.Event()

    class _Controller(SubAgentPolicyDouble):
        def __init__(self, *, shell, prompt: str, agent: _Agent, run_kwargs: dict, **_kwargs: object) -> None:
            super().__init__(shell=shell, prompt=prompt)
            self.agent = agent
            self.run_kwargs = run_kwargs

        async def execute(self) -> str:
            strategy = self.run_kwargs["compaction_strategy"]
            assert self.run_kwargs["tokenizer"] is strategy.tokenizer
            usage_mw = next(m for m in self.run_kwargs["middleware"] if isinstance(m, UsageTrackingMiddleware))
            history_provider = next(
                p for p in self.agent.context_providers if p.__class__.__name__ == "CompressibleHistoryProvider"
            )
            seen.append((strategy, usage_mw._compaction_strategy, history_provider._compaction_strategy))
            aggregate_flags.append(
                (
                    usage_mw._use_local_context_estimate_for_hosted_usage,
                    history_provider._use_local_context_estimate_for_hosted_usage,
                )
            )
            assert usage_mw.on_usage is not None
            usage_mw.on_usage(80_500, 80_000, 500, 10_000, 1.0, 0, None, False, True)
            if len(seen) == 2:
                both_started.set()
            await both_started.wait()
            return "done"

    install_tools(monkeypatch)
    monkeypatch.setattr(runtime_factory_module, "Agent", _Agent)
    monkeypatch.setattr(sub_agent_module, "KernelSubAgentPolicy", _Controller)
    monkeypatch.setattr(
        sub_agent_module,
        "create_client",
        create_autospec(sub_agent_module.create_client, side_effect=lambda *_args, **_kwargs: MockChatClient()),
    )
    monkeypatch.setattr(skills_adapter, "create_skills_provider", AsyncMock(return_value=(None, [])))

    spill_quota = SpillQuota()
    tools = SubAgentTools(
        max_total_concurrency=2,
        event_bus=EventBus(),
        session_id="s-1",
        session_dir=tmp_path,
        spill_quota=spill_quota,
        on_sub_agent_usage=lambda *args: usage_reports.append(args),
    )
    runtime = SessionEnvironment(cwd=str(tmp_path), platform=get_platform())
    await tools.register(
        SubAgentRef(profile="Explore", tool_name="Explore", max_concurrency=2),
        AgentProfile(name="Explore", display_name="Explore Agent", tools=ToolsConfig(builtins=[])),
        runtime,
        settings=Settings(),
        fallback_profile=ModelProfile(
            id="deepseek",
            name="deepseek",
            provider="deepseek-openai",
            api_style="responses",
            model_id="deepseek",
        ),
    )

    results = await asyncio.gather(
        tools.get_tools()[0].func(prompt="one"),
        tools.get_tools()[0].func(prompt="two"),
    )

    assert results == ["done", "done"]
    assert len(seen) == 2
    assert aggregate_flags == [(True, True), (True, True)]
    assert len(usage_reports) == 2
    assert all(report[-1] is True for report in usage_reports)
    assert all(report[:7] == (80_500, 80_000, 500, 10_000, 1.0, 0, None) for report in usage_reports)
    first_strategy, first_usage_strategy, first_history_strategy = seen[0]
    second_strategy, second_usage_strategy, second_history_strategy = seen[1]
    assert first_strategy is first_usage_strategy is first_history_strategy
    assert second_strategy is second_usage_strategy is second_history_strategy
    assert first_strategy is not second_strategy
    assert first_strategy is not tools._context_managers["Explore"].compaction_strategy
    assert second_strategy is not tools._context_managers["Explore"].compaction_strategy
    assert first_strategy._spill_quota is spill_quota
    assert second_strategy._spill_quota is spill_quota
    assert first_strategy._spill_root == tmp_path
    assert second_strategy._spill_root == tmp_path
    assert first_strategy._spill_record_dir != second_strategy._spill_record_dir
    assert first_strategy._spill_record_dir.parts[:3] == ("compactions", "sub_agents", "Explore")
    assert second_strategy._spill_record_dir.parts[:3] == ("compactions", "sub_agents", "Explore")
    assert first_strategy._spill_record_dir.name == "dropped"
    assert second_strategy._spill_record_dir.name == "dropped"
    assert first_strategy._persist_recovery_now is None
    assert second_strategy._persist_recovery_now is None
    registered_strategy = tools._context_managers["Explore"].compaction_strategy
    assert registered_strategy._spill_quota is spill_quota
    assert registered_strategy._spill_record_dir.as_posix() == "compactions/sub_agents/Explore/registered/dropped"
    assert registered_strategy._persist_recovery_now is None
