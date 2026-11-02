# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for ``ApprovalMiddleware``: auto-approval carve-outs (read-only shell,
workspace-git writes, session-archive reads), MANUAL/AUTO/BYPASS modes,
dev-mode sub-agent review, args parsing, user decisions, judge arbitration,
interrupt resolution, and the small helper methods (``set_user_message``,
``set_approval_mode``, ``drain_decisions``, ``reset``)."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import (
    ApprovalAutoFulfillBlocked,
    ApprovalRequest,
    ApprovalResponse,
    ApprovalReviewed,
    InvocationToolCallArgsUpdated,
)
from chrys.foundation.models.invocations import InvocationOrigin
from chrys.foundation.tool_kinds import KIND_CONTEXT, KIND_SKILL, KIND_SUB_AGENT
from chrys.foundation.trajectory.context import trajectory_scope
from chrys.foundation.trajectory.event_types import EventType
from chrys.foundation.trajectory.metadata import OPERATION_ID_KEY
from chrys.orchestration.invoker.origin import BoundEmitter
from chrys.service.agent_middleware._metadata_keys import (
    _APPROVAL_REJECTED_KEY,
    _REJECTION_MESSAGE_KEY,
    _REJECTION_SOURCE_KEY,
)
from chrys.service.agent_middleware.control.approval import (
    ApprovalMiddleware,
    _is_workspace_git_path,
)
from chrys.service.agent_middleware.events.hook_dispatch import set_call_id, set_tool_invocation_order
from chrys.service.approval.judge import JudgeVerdict
from chrys.service.approval.policy import ApprovalMode, ApprovalPolicy
from chrys.service.hooks.events import HookEvent
from chrys.service.hooks.operation import HookOperation
from chrys.service.hooks.schema import HookDecision
from chrys.service.profiles.agents.schema import ApprovalConfig
from chrys.service.trajectory.approvals import ApprovalDecider, ApprovalDecision
from tests.service.trajectory._fakes import FakeSink, make_context
from tests.support.symlinks import symlink_or_skip
from tests.support.waiting import ENGINE_TURN_TIMEOUT, wait_for, wait_until


def _mock_tool(name: str, kind: str | None = None) -> MagicMock:
    t = MagicMock()
    t.name = name
    t.chrys_kind = kind
    return t


def _ctx(tool_name: str, tool_kind: str, args) -> MagicMock:
    """Make a mock FunctionInvocationContext. *args* may be dict, str, or other."""
    c = MagicMock()
    c.function = SimpleNamespace(name=tool_name, chrys_kind=tool_kind)
    c.arguments = args
    c.metadata = {}
    c.result = None
    return c


def _require_all_policy(kind: str = "shell", tool_names: tuple[str, ...] = ("write_file",)) -> ApprovalPolicy:
    """Policy that requires approval for all *tool_names* with *kind*."""
    tools = [_mock_tool(n, kind=kind) for n in tool_names]
    return ApprovalPolicy(
        ApprovalConfig(default="auto", overrides={kind: "require"}),
        tools=tools,
    )


class _Harness:
    """Records ``ApprovalRequest`` events on *bus* and whether ``call_next`` ran.

    The bus stays in the test's hands (some tests inspect ``bus._handlers``);
    the harness only replaces the per-test collect/subscribe/next scaffold.
    """

    def __init__(self, bus: EventBus) -> None:
        self.bus = bus
        self.requests: list[ApprovalRequest] = []
        self.called = False

    @classmethod
    async def attach(cls, bus: EventBus) -> _Harness:
        harness = cls(bus)
        await bus.subscribe(ApprovalRequest, harness._collect)
        return harness

    async def _collect(self, event: ApprovalRequest) -> None:
        self.requests.append(event)

    async def call_next(self) -> None:
        self.called = True

    async def run(self, mw: ApprovalMiddleware, ctx: MagicMock) -> None:
        """Drive ``mw.process`` to completion with the recording ``call_next``."""
        await mw.process(ctx, self.call_next)

    def start(self, mw: ApprovalMiddleware, ctx: MagicMock) -> asyncio.Task[None]:
        """Run ``mw.process`` in the background so the test can answer its request."""
        return asyncio.create_task(mw.process(ctx, self.call_next))

    async def next_request(self) -> ApprovalRequest:
        """Wait for the request after the ones already seen and return it."""
        seen = len(self.requests)
        assert await wait_until(lambda: len(self.requests) > seen)
        return self.requests[seen]

    async def no_request(self) -> bool:
        """Negative wait: True when no ``ApprovalRequest`` arrives within the grace window."""
        return not await wait_until(lambda: bool(self.requests), timeout=0.2, interval=0.01)


# ───────────────────────── helper functions ───────────────────────────


def test_is_workspace_git_path_outside_workspace(tmp_path) -> None:
    """Path outside any resolved root → False."""
    inside = tmp_path / "inside"
    inside.mkdir()
    (inside / ".git").mkdir()
    other = tmp_path / "other" / "file.txt"
    other.parent.mkdir()
    other.write_text("x")

    roots = [os.path.realpath(str(inside))]
    assert _is_workspace_git_path(str(other), roots) is False


def test_is_workspace_git_path_inside_but_no_git(tmp_path) -> None:
    """Inside a root but no .git anywhere up the tree → False."""
    (tmp_path / "sub").mkdir()
    target = tmp_path / "sub" / "x.txt"
    target.write_text("x")
    roots = [os.path.realpath(str(tmp_path))]
    assert _is_workspace_git_path(str(target), roots) is False


def test_is_workspace_git_path_realpath_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """If ``os.path.realpath`` raises (e.g. OS-level error), return False."""

    def _boom(_: str) -> str:
        raise OSError("simulated realpath failure")

    monkeypatch.setattr(os.path, "realpath", _boom)
    assert _is_workspace_git_path("/any/path", ["/roots"]) is False


def test_is_workspace_git_path_git_in_ancestor(tmp_path) -> None:
    """``.git`` in an ancestor directory → True (walks up from file's dir)."""
    (tmp_path / ".git").mkdir()
    nested = tmp_path / "a" / "b" / "c"
    nested.mkdir(parents=True)
    target = nested / "file.txt"
    target.write_text("x")
    roots = [os.path.realpath(str(tmp_path))]
    assert _is_workspace_git_path(str(target), roots) is True


@pytest.mark.parametrize("target", [".git", ".git/config", ".git/hooks/pre-commit", ".git/hooks/post-checkout"])
def test_is_workspace_git_path_rejects_git_internals(tmp_path, target: str) -> None:
    """Git internals are not recoverable tracked workspace writes."""
    (tmp_path / ".git" / "hooks").mkdir(parents=True)
    roots = [os.path.realpath(str(tmp_path))]
    assert _is_workspace_git_path(str(tmp_path / target), roots) is False


def test_is_workspace_git_path_rejects_symlinked_git_internals(tmp_path) -> None:
    """The git-internals guard compares real paths so .git symlinks cannot bypass it."""
    git_dir = tmp_path / "actual-git-dir"
    git_dir.mkdir()
    symlink_or_skip(tmp_path / ".git", git_dir, target_is_directory=True)
    roots = [os.path.realpath(str(tmp_path))]
    assert _is_workspace_git_path(str(tmp_path / ".git" / "config"), roots) is False


def test_is_workspace_git_path_git_file_in_ancestor(tmp_path) -> None:
    """``.git`` file in an ancestor directory → True for worktrees/submodules."""
    (tmp_path / ".git").write_text("gitdir: ../.git/modules/example\n")
    nested = tmp_path / "a" / "b" / "c"
    nested.mkdir(parents=True)
    target = nested / "file.txt"
    target.write_text("x")
    roots = [os.path.realpath(str(tmp_path))]
    assert _is_workspace_git_path(str(target), roots) is True


def test_is_workspace_git_path_rejects_gitdir_pointer_target(tmp_path) -> None:
    """Real gitdir targets referenced by .git pointer files are metadata internals."""
    git_dir = tmp_path / "actual-git-dir"
    git_dir.mkdir()
    (tmp_path / ".git").write_text("gitdir: actual-git-dir\n")
    roots = [os.path.realpath(str(tmp_path))]
    assert _is_workspace_git_path(str(git_dir / "config"), roots) is False


async def test_workspace_git_auto_approval_resolves_relative_path_from_workspace_cwd(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Relative file paths should be checked against the middleware workspace cwd."""
    workspace = tmp_path / "workspace"
    other = tmp_path / "other"
    workspace.mkdir()
    other.mkdir()
    (workspace / ".git").mkdir()
    (workspace / "src").mkdir()
    (workspace / "src" / "file.txt").write_text("x")
    monkeypatch.chdir(other)

    bus = EventBus()
    mw = ApprovalMiddleware(
        approval_policy=_require_all_policy("filesystem.write"),
        event_bus=bus,
        workspace_roots=[str(workspace)],
        workspace_cwd=str(workspace),
    )
    h = await _Harness.attach(bus)

    await h.run(mw, _ctx("write_file", "filesystem.write", {"path": "src/file.txt"}))

    assert h.called is True
    assert await h.no_request()
    assert mw.drain_decisions() == [{"request_id": "", "tool_name": "write_file", "status": "auto_approved"}]


# ──────────────────── property / mutator helpers ──────────────────────


def test_set_user_message_and_property_roundtrip() -> None:
    mw = ApprovalMiddleware(approval_policy=_require_all_policy(), event_bus=EventBus())
    assert mw._user_message == ""
    assert mw._user_messages == []
    mw.set_user_message("hello world")
    assert mw._user_message == "hello world"
    assert mw._user_messages == ["hello world"]


def test_set_user_messages_stores_current_turn_context_and_latest() -> None:
    mw = ApprovalMiddleware(approval_policy=_require_all_policy(), event_bus=EventBus())
    mw.set_user_messages(["repo overview", "how many lines?"])
    assert mw._user_messages == ["repo overview", "how many lines?"]
    assert mw._user_message == "how many lines?"


def test_set_approval_mode_and_property() -> None:
    mw = ApprovalMiddleware(approval_policy=_require_all_policy(), event_bus=EventBus())
    assert mw.approval_mode == ApprovalMode.MANUAL
    mw.set_approval_mode(ApprovalMode.BYPASS)
    assert mw.approval_mode == ApprovalMode.BYPASS


def test_drain_decisions_returns_copy_and_clears() -> None:
    mw = ApprovalMiddleware(approval_policy=_require_all_policy(), event_bus=EventBus())
    mw._decisions.append({"request_id": "a", "tool_name": "t", "status": "user_approved"})
    mw._decisions.append({"request_id": "b", "tool_name": "t", "status": "user_rejected"})

    out = mw.drain_decisions()
    assert len(out) == 2
    assert mw._decisions == []  # cleared
    # Mutating the returned list must not affect internal state
    out.append({"x": "y"})
    assert mw._decisions == []


def test_reset_clears_decisions_and_user_message() -> None:
    mw = ApprovalMiddleware(approval_policy=_require_all_policy(), event_bus=EventBus())
    mw._decisions.append({"request_id": "a", "tool_name": "t", "status": "user_approved"})
    mw.set_user_message("hi")

    mw.reset()
    assert mw._decisions == []
    assert mw._user_message == ""
    assert mw._user_messages == []


def test_retry_snapshot_drops_failed_decision_and_keeps_completed_baseline() -> None:
    middleware = ApprovalMiddleware(approval_policy=_require_all_policy(), event_bus=EventBus())
    baseline = {"request_id": "baseline", "tool_name": "write_file", "status": "user_approved"}
    failed = {"request_id": "failed", "tool_name": "write_file", "status": "user_rejected"}
    successful = {"request_id": "successful", "tool_name": "write_file", "status": "user_approved"}
    middleware._decisions.append(baseline)

    snapshot = middleware.snapshot_retry_state()
    middleware._decisions.append(failed)
    middleware.restore_retry_state(snapshot)
    middleware._decisions.append(successful)

    assert middleware.drain_decisions() == [baseline, successful]


# ───────────────────────── auto path (no approval) ─────────────────────


async def test_should_not_require_calls_next_immediately() -> None:
    """Policy says no approval needed → call_next runs, no event published."""
    policy = ApprovalPolicy(ApprovalConfig(default="auto"))
    bus = EventBus()
    mw = ApprovalMiddleware(approval_policy=policy, event_bus=bus)
    h = await _Harness.attach(bus)

    await h.run(mw, _ctx("any_tool", "", {}))

    assert h.called
    assert await h.no_request()


async def test_dev_mode_requires_sub_agent_and_applies_modified_prompt() -> None:
    """Developer mode gates sub-agent handoff even when policy default is auto."""
    policy = ApprovalPolicy(ApprovalConfig(default="auto"))
    bus = EventBus()
    mw = ApprovalMiddleware(approval_policy=policy, event_bus=bus, dev_mode=True)
    mw.bind_publisher(BoundEmitter(bus, InvocationOrigin("turn", "", "approval-test", None)))
    ctx = _ctx("explore_agent", KIND_SUB_AGENT, {"prompt": "inspect src"})
    set_call_id(ctx, "call-123")
    h = await _Harness.attach(bus)

    task = h.start(mw, ctx)
    request = await h.next_request()

    assert not h.called
    assert len(h.requests) == 1
    assert request.call_id == "call-123"
    assert request.tool_kind == KIND_SUB_AGENT
    assert request.judging is False
    # No fabricated intent: an "Execute {tool_name}" placeholder would win
    # over the ACP server's informative title fallbacks downstream.
    assert request.intent_summary == ""

    await bus.publish(
        ApprovalResponse(
            request_id=request.request_id,
            approved=True,
            modified_args={"prompt": "inspect src and tests"},
        )
    )
    await task

    assert h.called
    assert ctx.arguments == {"prompt": "inspect src and tests"}


async def test_dev_mode_sub_agent_review_runs_in_auto_mode() -> None:
    """Developer-mode sub-agent handoff review runs in AUTO approval mode."""
    policy = ApprovalPolicy(ApprovalConfig(default="auto"))
    bus = EventBus()
    mw = ApprovalMiddleware(
        approval_policy=policy,
        event_bus=bus,
        approval_mode=ApprovalMode.AUTO,
        dev_mode=True,
    )
    ctx = _ctx("explore_agent", KIND_SUB_AGENT, {"prompt": "inspect src"})
    h = await _Harness.attach(bus)

    task = h.start(mw, ctx)
    request = await h.next_request()

    assert not h.called
    assert len(h.requests) == 1
    assert request.tool_name == "explore_agent"
    assert request.judging is False

    await bus.publish(ApprovalResponse(request_id=request.request_id, approved=True))
    await task

    assert h.called


async def test_dev_mode_sub_agent_review_skips_bypass_mode() -> None:
    """BYPASS mode skips developer-mode sub-agent handoff review."""
    policy = ApprovalPolicy(ApprovalConfig(default="auto"))
    bus = EventBus()
    mw = ApprovalMiddleware(
        approval_policy=policy,
        event_bus=bus,
        approval_mode=ApprovalMode.BYPASS,
        dev_mode=True,
    )
    ctx = _ctx("explore_agent", KIND_SUB_AGENT, {"prompt": "inspect src"})
    h = await _Harness.attach(bus)

    task = h.start(mw, ctx)
    # BYPASS short-circuits: the task completes on its own, so awaiting it is
    # the barrier — no fixed sleep needed before asserting.
    await task

    assert h.called
    assert h.requests == []
    assert ctx.arguments == {"prompt": "inspect src"}


async def test_dev_mode_modified_prompt_re_dispatches_blocking_hook() -> None:
    """Hook gating on ``args.prompt`` sees the user's edited prompt, not the original.

    Without re-dispatch, a hook that approves the original could let the edited
    prompt execute unchecked — the P1 bug this guards against.
    """

    class _BlockingHookManager:
        def __init__(self, block_substring: str) -> None:
            self._block = block_substring
            self.payloads: list[dict[str, Any]] = []

        def has_hooks_for(self, event: HookEvent) -> bool:
            return event == HookEvent.BEFORE_TOOL_CALL

        async def fire(self, _event: HookEvent, payload: dict[str, Any], **_kwargs: object) -> HookDecision:
            self.payloads.append(payload)
            prompt = payload.get("tool", {}).get("args", {}).get("prompt", "")
            if self._block in prompt:
                return HookDecision(blocked=True, block_reason=f"forbidden token: {self._block}")
            return HookDecision()

    hook_mgr = _BlockingHookManager(block_substring="rm -rf")
    policy = ApprovalPolicy(ApprovalConfig(default="auto"))
    bus = EventBus()
    mw = ApprovalMiddleware(
        approval_policy=policy,
        event_bus=bus,
        dev_mode=True,
        hook_manager=hook_mgr,
        profile_name="Code",
    )
    mw.bind_publisher(BoundEmitter(bus, InvocationOrigin("turn", "", "approval-test", None)))
    ctx = _ctx("explore_agent", KIND_SUB_AGENT, {"prompt": "inspect src"})
    set_call_id(ctx, "call-77")
    h = await _Harness.attach(bus)
    updates: list[InvocationToolCallArgsUpdated] = []

    async def _collect_update(ev: InvocationToolCallArgsUpdated) -> None:
        updates.append(ev)

    await bus.subscribe(InvocationToolCallArgsUpdated, _collect_update)

    task = h.start(mw, ctx)
    # Wait for the request to be pending before responding to it.
    request = await h.next_request()

    # User edits the prompt to something the hook would have blocked at dispatch time.
    await bus.publish(
        ApprovalResponse(
            request_id=request.request_id,
            approved=True,
            modified_args={"prompt": "rm -rf /"},
        )
    )
    await task

    assert h.called is False
    assert len(hook_mgr.payloads) == 1
    assert hook_mgr.payloads[0]["tool"]["args"] == {"prompt": "rm -rf /"}
    assert len(updates) == 1
    assert updates[0].call_id == "call-77"
    assert updates[0].args == {"prompt": "rm -rf /"}
    assert ctx.metadata.get("_approval_rejected") is True
    assert "forbidden token" in (ctx.result or "")


async def test_re_dispatched_hooks_target_the_tool_operation_they_can_still_block() -> None:
    """The second dispatch gates the same call as the first, so it belongs to the same operation.

    Without the target, a hook that blocks the edited arguments is recorded
    against the exchange instead of the tool call it just stopped.
    """

    class _RecordingHookManager:
        def __init__(self) -> None:
            self.targets: list[str | None] = []

        def has_hooks_for(self, event: HookEvent) -> bool:
            return event == HookEvent.BEFORE_TOOL_CALL

        async def fire(
            self,
            _event: HookEvent,
            _payload: dict[str, Any],
            *,
            scope: str = "turn",
            target_operation_id: str | None = None,
            trajectory_context: object | None = None,
            origin: InvocationOrigin | None = None,
            operation: HookOperation | None = None,
        ) -> HookDecision:
            assert scope == "turn"
            self.targets.append(target_operation_id)
            return HookDecision()

    hook_mgr = _RecordingHookManager()
    bus = EventBus()
    mw = ApprovalMiddleware(
        approval_policy=ApprovalPolicy(ApprovalConfig(default="auto")),
        event_bus=bus,
        dev_mode=True,
        hook_manager=hook_mgr,
        profile_name="Code",
    )
    mw.bind_publisher(BoundEmitter(bus, InvocationOrigin("turn", "", "approval-test", None)))
    ctx = _ctx("explore_agent", KIND_SUB_AGENT, {"prompt": "inspect src"})
    set_call_id(ctx, "call-88")
    ctx.metadata[OPERATION_ID_KEY] = "op-88"
    h = await _Harness.attach(bus)

    task = h.start(mw, ctx)
    request = await h.next_request()
    await bus.publish(
        ApprovalResponse(
            request_id=request.request_id,
            approved=True,
            modified_args={"prompt": "inspect tests"},
        )
    )
    await task

    assert hook_mgr.targets == ["op-88"]


async def test_dev_mode_modified_prompt_re_dispatched_hook_can_rewrite() -> None:
    """A non-blocking hook can still rewrite edited args before execution."""

    class _RewriteHookManager:
        def has_hooks_for(self, event: HookEvent) -> bool:
            return event == HookEvent.BEFORE_TOOL_CALL

        async def fire(self, _event: HookEvent, _payload: dict[str, Any], **_kwargs: object) -> HookDecision:
            return HookDecision(args_override={"prompt": "inspect src --safe"})

    policy = ApprovalPolicy(ApprovalConfig(default="auto"))
    bus = EventBus()
    mw = ApprovalMiddleware(
        approval_policy=policy,
        event_bus=bus,
        dev_mode=True,
        hook_manager=_RewriteHookManager(),
        profile_name="Code",
    )
    mw.bind_publisher(BoundEmitter(bus, InvocationOrigin("turn", "", "approval-test", None)))
    ctx = _ctx("explore_agent", KIND_SUB_AGENT, {"prompt": "inspect src"})
    set_call_id(ctx, "call-88")
    h = await _Harness.attach(bus)
    updates: list[InvocationToolCallArgsUpdated] = []

    async def _collect_update(ev: InvocationToolCallArgsUpdated) -> None:
        updates.append(ev)

    await bus.subscribe(InvocationToolCallArgsUpdated, _collect_update)

    task = h.start(mw, ctx)
    # Wait for the request to be pending before responding to it.
    request = await h.next_request()

    await bus.publish(
        ApprovalResponse(
            request_id=request.request_id,
            approved=True,
            modified_args={"prompt": "inspect src and tests"},
        )
    )
    await task

    assert ctx.arguments == {"prompt": "inspect src --safe"}
    assert ctx.metadata.get("_approval_modified_args") == {"prompt": "inspect src --safe"}
    assert len(updates) == 1
    assert updates[0].call_id == "call-88"
    assert updates[0].args == {"prompt": "inspect src --safe"}


# ───────────────── BYPASS mode + auto-approval carve-outs ─────────────


async def test_bypass_mode_short_circuits_and_records_decision() -> None:
    """BYPASS mode: no event published, call_next runs, decision recorded."""
    bus = EventBus()
    mw = ApprovalMiddleware(
        approval_policy=_require_all_policy("filesystem.write"),
        event_bus=bus,
        approval_mode=ApprovalMode.BYPASS,
    )
    h = await _Harness.attach(bus)

    await h.run(mw, _ctx("write_file", "filesystem.write", {"path": "/tmp/x"}))

    assert h.called
    assert await h.no_request()
    decisions = mw.drain_decisions()
    assert decisions == [{"request_id": "", "tool_name": "write_file", "status": "bypass_approved"}]


# Each builder returns the middleware kwargs (minus ``event_bus``) and the
# invocation that must take an auto-approval carve-out.
_AutoApprovalCase = Callable[[Path], tuple[dict[str, Any], MagicMock]]


def _git_root(tmp_path: Path, name: str = "repo") -> Path:
    root = tmp_path / name
    root.mkdir()
    (root / ".git").mkdir()
    return root


def _safe_shell_case(tmp_path: Path) -> tuple[dict[str, Any], MagicMock]:
    """A safe read-only shell command under a shell-require policy."""
    return (
        {"approval_policy": _require_all_policy("shell", tool_names=("zsh",))},
        _ctx("zsh", "shell", {"command": "ls"}),
    )


def _powershell_null_redirect_case(tmp_path: Path) -> tuple[dict[str, Any], MagicMock]:
    """PowerShell null redirection stays on the read-only auto-approval path."""
    return (
        {"approval_policy": _require_all_policy("shell", tool_names=("pwsh",))},
        _ctx("pwsh", "shell", {"command": "Get-ChildItem 2> $null"}),
    )


def _workspace_git_file_case(tmp_path: Path) -> tuple[dict[str, Any], MagicMock]:
    """A ``write_file`` inside a workspace root that carries ``.git``."""
    root = _git_root(tmp_path)
    return (
        {"approval_policy": _require_all_policy("filesystem.write"), "workspace_roots": [str(root)]},
        _ctx("write_file", "filesystem.write", {"path": str(root / "file.txt")}),
    )


def _edit_file_in_git_workspace_case(tmp_path: Path) -> tuple[dict[str, Any], MagicMock]:
    """``edit_file`` is a workspace-git write too, even into a not-yet-created subdirectory."""
    root = _git_root(tmp_path)
    return (
        {
            "approval_policy": _require_all_policy("filesystem.write", tool_names=("edit_file",)),
            "workspace_roots": [str(root)],
        },
        _ctx(
            "edit_file",
            "filesystem.write",
            {"path": os.path.join(str(root), "src", "main.py"), "old_string": "a", "new_string": "b"},
        ),
    )


def _secondary_git_workspace_root_case(tmp_path: Path) -> tuple[dict[str, Any], MagicMock]:
    """A write inside a secondary workspace root (the primary has no git)."""
    primary = tmp_path / "primary"
    primary.mkdir()
    secondary = _git_root(tmp_path, "secondary")
    return (
        {"approval_policy": _require_all_policy("filesystem.write"), "workspace_roots": [str(primary), str(secondary)]},
        _ctx("write_file", "filesystem.write", {"path": str(secondary / "test.py"), "content": "print(1)"}),
    )


def _session_archive_read_case(tmp_path: Path) -> tuple[dict[str, Any], MagicMock]:
    """A read of a session compaction archive under a read-require policy."""
    archive_root = tmp_path / "compactions"
    record = archive_root / "dropped" / "turn001" / "001_read_file_aaaaaaaa.md"
    record.parent.mkdir(parents=True)
    record.write_text("archived", encoding="utf-8")
    return (
        {
            "approval_policy": _require_all_policy("filesystem.read", tool_names=("read_file",)),
            "approval_mode": ApprovalMode.MANUAL,
            "session_archive_read_roots": [archive_root],
        },
        _ctx("read_file", "filesystem.read", {"path": str(record)}),
    )


def _session_archive_sensitive_component_case(tmp_path: Path) -> tuple[dict[str, Any], MagicMock]:
    """A sensitive-looking archived component (e.g. a sub-agent tool named
    ``credentials``) is whitelisted inside the archive root instead of gated."""
    archive_root = tmp_path / "compactions"
    record = archive_root / "sub_agents" / "credentials" / "inv1" / "dropped" / "turn001" / "001_fetch_bbbbbbbb.md"
    record.parent.mkdir(parents=True)
    record.write_text("archived", encoding="utf-8")
    return (
        {
            # Default policy: reads gate only through the sensitive classifier.
            "approval_policy": ApprovalPolicy(ApprovalConfig(default="auto")),
            "approval_mode": ApprovalMode.MANUAL,
            "session_archive_read_roots": [archive_root],
        },
        _ctx("read_file", "filesystem.read", {"path": str(record)}),
    )


@pytest.mark.parametrize(
    "build_case",
    [
        pytest.param(_safe_shell_case, id="safe_shell"),
        pytest.param(_powershell_null_redirect_case, id="powershell_null_redirect"),
        pytest.param(_workspace_git_file_case, id="workspace_git_file"),
        pytest.param(_edit_file_in_git_workspace_case, id="edit_file_in_git_workspace"),
        pytest.param(_secondary_git_workspace_root_case, id="secondary_git_workspace_root"),
        pytest.param(_session_archive_read_case, id="session_archive_read"),
        pytest.param(_session_archive_sensitive_component_case, id="session_archive_sensitive_component"),
    ],
)
async def test_auto_approval_records_alignment_placeholder(build_case: _AutoApprovalCase, tmp_path: Path) -> None:
    """Every carve-out skips the dialog yet records an ``auto_approved`` placeholder to keep decision order aligned."""
    middleware_kwargs, ctx = build_case(tmp_path)
    bus = EventBus()
    mw = ApprovalMiddleware(event_bus=bus, **middleware_kwargs)
    h = await _Harness.attach(bus)

    await h.run(mw, ctx)

    assert h.called
    assert await h.no_request()
    assert mw.drain_decisions() == [{"request_id": "", "tool_name": ctx.function.name, "status": "auto_approved"}]


async def test_session_archive_read_policy_still_gates_reads_outside_the_archive(tmp_path) -> None:
    """Control for the archive carve-out: the same policy still gates reads outside the archive root."""
    archive_root = tmp_path / "compactions"
    archive_root.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("not archived", encoding="utf-8")

    bus = EventBus()
    mw = ApprovalMiddleware(
        approval_policy=_require_all_policy("filesystem.read", tool_names=("read_file",)),
        event_bus=bus,
        approval_mode=ApprovalMode.MANUAL,
        session_archive_read_roots=[archive_root],
    )
    h = await _Harness.attach(bus)

    task = h.start(mw, _ctx("read_file", "filesystem.read", {"path": str(outside)}))
    request = await h.next_request()
    assert h.called is False
    await bus.publish(ApprovalResponse(request_id=request.request_id, approved=True))
    await task
    assert h.called


@pytest.mark.parametrize("target", [".git/config", ".git/hooks/pre-commit", ".git/hooks/post-checkout"])
async def test_workspace_git_internals_require_approval_in_manual_mode(tmp_path, target: str) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / ".git" / "hooks").mkdir(parents=True)

    bus = EventBus()
    mw = ApprovalMiddleware(
        approval_policy=_require_all_policy("filesystem.write"),
        event_bus=bus,
        approval_mode=ApprovalMode.MANUAL,
        workspace_roots=[str(root)],
        workspace_cwd=str(root),
    )
    h = await _Harness.attach(bus)

    task = h.start(mw, _ctx("write_file", "filesystem.write", {"path": target}))
    request = await h.next_request()

    assert h.called is False
    assert request.args == {"path": target}

    await bus.publish(ApprovalResponse(request_id=request.request_id, approved=False, reason="review required"))
    await task

    assert h.called is False
    assert mw.drain_decisions() == [
        {
            "request_id": request.request_id,
            "tool_name": "write_file",
            "status": "user_rejected",
            "reason": "review required",
        }
    ]


async def test_session_archive_read_symlink_escape_is_not_auto_approved(tmp_path) -> None:
    """A link planted inside the archive cannot smuggle an outside read."""
    archive_root = tmp_path / "compactions"
    (archive_root / "dropped").mkdir(parents=True)
    secret = tmp_path / "secret.md"
    secret.write_text("outside", encoding="utf-8")
    link = archive_root / "dropped" / "link.md"
    symlink_or_skip(link, secret)

    bus = EventBus()
    mw = ApprovalMiddleware(
        approval_policy=_require_all_policy("filesystem.read", tool_names=("read_file",)),
        event_bus=bus,
        approval_mode=ApprovalMode.MANUAL,
        session_archive_read_roots=[archive_root],
    )
    h = await _Harness.attach(bus)

    task = h.start(mw, _ctx("read_file", "filesystem.read", {"path": str(link)}))
    request = await h.next_request()
    assert h.called is False
    await bus.publish(ApprovalResponse(request_id=request.request_id, approved=False, reason="escape"))
    await task
    assert h.called is False


async def test_middleware_requires_approval_for_unsafe_shell() -> None:
    """Unsafe shell commands still publish an ``ApprovalRequest`` that carries the workspace cwd."""
    bus = EventBus()
    mw = ApprovalMiddleware(
        approval_policy=_require_all_policy("shell", tool_names=("zsh",)),
        event_bus=bus,
        workspace_cwd="/workspace",
    )
    h = await _Harness.attach(bus)

    # Run middleware in background — it will block waiting for ApprovalResponse
    task = h.start(mw, _ctx("zsh", "shell", {"command": "git push origin main", "reason": "push code"}))
    request = await h.next_request()

    assert len(h.requests) == 1, "unsafe command should trigger approval request"
    assert request.tool_name == "zsh"
    assert request.workspace_cwd == "/workspace"

    # Clean up — cancel the blocked task
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_middleware_requires_approval_outside_workspace(tmp_path) -> None:
    """File write OUTSIDE workspace roots should still require approval."""
    root = _git_root(tmp_path)
    bus = EventBus()
    mw = ApprovalMiddleware(
        approval_policy=_require_all_policy("filesystem.write"),
        event_bus=bus,
        workspace_roots=[str(root)],
    )
    h = await _Harness.attach(bus)

    # Target outside workspace
    task = h.start(mw, _ctx("write_file", "filesystem.write", {"path": "/etc/passwd", "content": "bad"}))
    await h.next_request()

    assert len(h.requests) == 1, "write outside workspace should require approval"
    assert h.called is False

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_middleware_requires_approval_no_git(tmp_path) -> None:
    """File write inside workspace but WITHOUT git should require approval."""
    workspace = tmp_path / "no_git"
    workspace.mkdir()
    bus = EventBus()
    mw = ApprovalMiddleware(
        approval_policy=_require_all_policy("filesystem.write"),
        event_bus=bus,
        workspace_roots=[str(workspace)],
    )
    h = await _Harness.attach(bus)

    target = os.path.join(str(workspace), "file.txt")
    task = h.start(mw, _ctx("write_file", "filesystem.write", {"path": target, "content": "hello"}))
    await h.next_request()

    assert len(h.requests) == 1, "write without git should require approval"
    assert h.called is False

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_parallel_same_name_manual_decision_keeps_invocation_order() -> None:
    """A later safe auto-approval must not leapfrog an earlier pending request."""
    bus = EventBus()
    mw = ApprovalMiddleware(
        approval_policy=_require_all_policy("shell", tool_names=("zsh",)),
        event_bus=bus,
    )
    h = await _Harness.attach(bus)

    unsafe_called = False
    safe_called = False

    async def _unsafe_next() -> None:
        nonlocal unsafe_called
        unsafe_called = True

    async def _safe_next() -> None:
        nonlocal safe_called
        safe_called = True

    unsafe_ctx = _ctx("zsh", "shell", {"command": "rm file"})
    safe_ctx = _ctx("zsh", "shell", {"command": "ls"})
    set_call_id(unsafe_ctx, "unsafe-call")
    set_call_id(safe_ctx, "safe-call")
    set_tool_invocation_order(unsafe_ctx, 0)
    set_tool_invocation_order(safe_ctx, 1)

    unsafe_task = asyncio.create_task(mw.process(unsafe_ctx, _unsafe_next))
    request = await h.next_request()
    assert len(h.requests) == 1

    await mw.process(safe_ctx, _safe_next)
    assert safe_called

    await bus.publish(ApprovalResponse(request_id=request.request_id, approved=False, reason="too broad"))
    await unsafe_task

    assert not unsafe_called
    assert mw.drain_decisions() == [
        {
            "request_id": request.request_id,
            "tool_name": "zsh",
            "status": "user_rejected",
            "call_id": "unsafe-call",
            "tool_order": "0",
            "reason": "too broad",
        },
        {
            "request_id": "",
            "tool_name": "zsh",
            "status": "auto_approved",
            "call_id": "safe-call",
            "tool_order": "1",
        },
    ]


# ───────────────────────── args parsing branches ───────────────────────


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        pytest.param(
            '{"path": "/tmp/a.txt", "content": "x"}',
            {"path": "/tmp/a.txt", "content": "x"},
            id="json_string",
        ),
        pytest.param("not a json {{{", {}, id="invalid_json_string"),
        pytest.param(None, {}, id="none"),
    ],
)
async def test_args_normalized_to_dict(arguments: object, expected: dict[str, str]) -> None:
    """A JSON string is decoded; malformed JSON and non-dict, non-str values fall through to ``{}``."""
    bus = EventBus()
    mw = ApprovalMiddleware(approval_policy=_require_all_policy("filesystem.write"), event_bus=bus)
    h = await _Harness.attach(bus)

    task = h.start(mw, _ctx("write_file", "filesystem.write", arguments))
    request = await h.next_request()

    assert len(h.requests) == 1
    assert request.args == expected

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


# ───────────────────────── approval flow ───────────────────────────────


@pytest.mark.parametrize(
    ("tool_name", "tool_kind", "override_key"),
    [
        ("load_skill", KIND_SKILL, "skill"),
        ("compress_context", KIND_CONTEXT, "context"),
    ],
)
async def test_runtime_added_tool_kind_rule_is_enforced_by_middleware(
    tool_name: str,
    tool_kind: str,
    override_key: str,
) -> None:
    """The middleware passes live provenance for dynamically injected tools."""
    bus = EventBus()
    policy = ApprovalPolicy(
        ApprovalConfig(default="auto", overrides={override_key: "require"}),
        tools=[],
    )
    mw = ApprovalMiddleware(approval_policy=policy, event_bus=bus)
    h = await _Harness.attach(bus)

    async def _respond(ev: ApprovalRequest) -> None:
        asyncio.create_task(  # noqa: RUF006
            bus.publish(ApprovalResponse(request_id=ev.request_id, approved=True, reason="approved"))
        )

    await bus.subscribe(ApprovalRequest, _respond)

    await h.run(mw, _ctx(tool_name, tool_kind, {}))

    assert h.called
    assert len(h.requests) == 1
    assert h.requests[0].tool_kind == tool_kind


async def test_run_skill_script_floor_rejects_before_tool_process_can_start() -> None:
    """An auto-default custom profile still gates the skill subprocess call."""
    bus = EventBus()
    policy = ApprovalPolicy(ApprovalConfig(default="auto", overrides={}), tools=[])
    mw = ApprovalMiddleware(approval_policy=policy, event_bus=bus)
    h = await _Harness.attach(bus)

    async def _respond(ev: ApprovalRequest) -> None:
        asyncio.create_task(  # noqa: RUF006
            bus.publish(ApprovalResponse(request_id=ev.request_id, approved=False, reason="untrusted skill"))
        )

    await bus.subscribe(ApprovalRequest, _respond)

    ctx = _ctx("run_skill_script", KIND_SKILL, {"skill_name": "example", "script_path": "scripts/run.py"})
    await h.run(mw, ctx)

    assert not h.called  # the tool process never started
    assert len(h.requests) == 1
    assert h.requests[0].tool_kind == KIND_SKILL
    assert ctx.result == "Error: Tool execution was rejected by user.\nUser reason: untrusted skill"


async def test_user_approval_runs_next_and_records_decision() -> None:
    """Approved response → call_next runs, ``user_approved`` recorded."""
    bus = EventBus()
    mw = ApprovalMiddleware(approval_policy=_require_all_policy("filesystem.write"), event_bus=bus)
    h = await _Harness.attach(bus)
    ctx = _ctx("write_file", "filesystem.write", {"path": "/tmp/x"})

    async def _respond(rid: str) -> None:
        await bus.publish(ApprovalResponse(request_id=rid, approved=True, reason="looks fine"))

    async def _responder(ev: ApprovalRequest) -> None:
        asyncio.create_task(_respond(ev.request_id))  # noqa: RUF006

    await bus.subscribe(ApprovalRequest, _responder)
    await h.run(mw, ctx)

    assert h.called
    decisions = mw.drain_decisions()
    assert len(decisions) == 1
    assert decisions[0]["status"] == "user_approved"
    assert decisions[0]["reason"] == "looks fine"


async def test_user_rejection_returns_error_and_sets_flag() -> None:
    """Rejected response → error string set, call_next NOT run, metadata flagged."""
    bus = EventBus()
    mw = ApprovalMiddleware(approval_policy=_require_all_policy("filesystem.write"), event_bus=bus)
    h = await _Harness.attach(bus)
    ctx = _ctx("write_file", "filesystem.write", {"path": "/tmp/x"})

    async def _respond(rid: str) -> None:
        await bus.publish(ApprovalResponse(request_id=rid, approved=False, reason=""))

    async def _responder(ev: ApprovalRequest) -> None:
        asyncio.create_task(_respond(ev.request_id))  # noqa: RUF006

    await bus.subscribe(ApprovalRequest, _responder)
    await h.run(mw, ctx)

    assert not h.called
    assert ctx.result == "Error: Tool execution was rejected by user."
    assert ctx.metadata.get(_APPROVAL_REJECTED_KEY) is True
    assert ctx.metadata.get(_REJECTION_SOURCE_KEY) == "user"
    assert ctx.metadata.get(_REJECTION_MESSAGE_KEY) == "Tool execution was rejected by user."

    decisions = mw.drain_decisions()
    assert decisions[0]["status"] == "user_rejected"
    # No reason in decision when empty
    assert "reason" not in decisions[0]


async def test_user_rejection_includes_reason_in_error_result() -> None:
    """Rejected response with reason → tool result tells the model why it was rejected."""
    bus = EventBus()
    mw = ApprovalMiddleware(approval_policy=_require_all_policy("filesystem.write"), event_bus=bus)
    ctx = _ctx("write_file", "filesystem.write", {"path": "/tmp/x"})

    async def _respond(rid: str) -> None:
        await bus.publish(ApprovalResponse(request_id=rid, approved=False, reason="  Use a safer path  "))

    async def _responder(ev: ApprovalRequest) -> None:
        asyncio.create_task(_respond(ev.request_id))  # noqa: RUF006

    await bus.subscribe(ApprovalRequest, _responder)
    await mw.process(ctx, MagicMock())

    assert ctx.result == "Error: Tool execution was rejected by user.\nUser reason: Use a safer path"
    assert ctx.metadata.get(_APPROVAL_REJECTED_KEY) is True
    assert ctx.metadata.get(_REJECTION_SOURCE_KEY) == "user"
    assert ctx.metadata.get(_REJECTION_MESSAGE_KEY) == (
        "Tool execution was rejected by user. User reason: Use a safer path"
    )

    decisions = mw.drain_decisions()
    assert decisions[0]["status"] == "user_rejected"
    assert decisions[0]["reason"] == "Use a safer path"


# ───────────────────────── AUTO + judge ────────────────────────────────


class _FakeJudge:
    """Stand-in for ``ApprovalJudge``: returns a pre-built verdict (or raises)."""

    def __init__(
        self,
        verdict: JudgeVerdict | None = None,
        exc: BaseException | None = None,
        delay: float = 0.0,
    ) -> None:
        self.verdict = verdict
        self.exc = exc
        self.delay = delay
        self.called_with: dict | None = None

    async def evaluate(self, **kwargs) -> JudgeVerdict:
        self.called_with = kwargs
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.exc is not None:
            raise self.exc
        assert self.verdict is not None
        return self.verdict


async def _judge_reviews(
    tool_name: str,
    tool_kind: str,
    args: dict[str, str],
    *,
    policy: ApprovalPolicy,
    tmp_path: Path,
    workspace_root: Path | None = None,
) -> None:
    """Assert that AUTO mode sends the call to the judge instead of an auto-approval carve-out."""
    bus = EventBus()
    judge = _FakeJudge(verdict=JudgeVerdict(approved=True, reason="reviewed"))
    mw = ApprovalMiddleware(
        approval_policy=policy,
        event_bus=bus,
        approval_mode=ApprovalMode.AUTO,
        approval_judge=judge,
        approval_log_dir=tmp_path,
        workspace_roots=[str(workspace_root)] if workspace_root is not None else None,
        workspace_cwd=str(workspace_root) if workspace_root is not None else None,
    )
    h = await _Harness.attach(bus)

    await h.run(mw, _ctx(tool_name, tool_kind, args))

    assert h.called
    assert len(h.requests) == 1
    assert h.requests[0].judging is True
    assert judge.called_with is not None
    assert judge.called_with["tool_name"] == tool_name
    assert judge.called_with["args"] == args
    assert mw.drain_decisions()[0]["status"] == "user_approved"


async def test_auto_judge_approved_auto_fulfils(tmp_path) -> None:
    """AUTO + judge returns approved → future is fulfilled automatically; ApprovalReviewed published; call_next runs."""
    bus = EventBus()
    judge = _FakeJudge(verdict=JudgeVerdict(approved=True, reason="safe read"))
    mw = ApprovalMiddleware(
        approval_policy=_require_all_policy("filesystem.write"),
        event_bus=bus,
        approval_mode=ApprovalMode.AUTO,
        approval_judge=judge,
        approval_log_dir=tmp_path,
        session_id="sess-1",
    )

    reviews: list[ApprovalReviewed] = []

    async def _rev(ev: ApprovalReviewed) -> None:
        reviews.append(ev)

    await bus.subscribe(ApprovalReviewed, _rev)
    h = await _Harness.attach(bus)

    ctx = _ctx("write_file", "filesystem.write", {"path": "/tmp/x"})
    mw.set_user_message("please write")

    await h.run(mw, ctx)

    assert h.called
    assert len(h.requests) == 1
    assert h.requests[0].judging is True  # dialog shows "evaluating"
    assert len(reviews) == 1
    assert reviews[0].approved is True
    assert reviews[0].reason == "safe read"

    # Judge was called with user message + args
    assert judge.called_with is not None
    assert judge.called_with["user_message"] == "please write"
    assert judge.called_with["user_messages"] == ["please write"]
    assert judge.called_with["tool_name"] == "write_file"
    assert judge.called_with["log_dir"] == tmp_path


@pytest.mark.parametrize(
    ("tool_name", "command"),
    [
        # POSIX shells: sensitive read-only-looking commands.
        ("zsh", "cat .env"),
        ("zsh", "printenv"),
        ("zsh", "env -0"),
        ("zsh", "env --null"),
        ("zsh", "env -u SECRET"),
        ("zsh", "env VAR=value printenv"),
        ("zsh", "env VAR=value env"),
        ("zsh", 'env FOO="bar baz" printenv'),
        ("zsh", "env FOO='bar baz' env"),
        ("zsh", r"env FOO=bar\ baz printenv"),
        ("zsh", "cat ~/.aws/credentials"),
        ("zsh", "echo $OPENAI_API_KEY"),
        ("zsh", 'printf "%s\n" "$GITHUB_TOKEN"'),
        ("zsh", "echo ${AWS_SECRET_ACCESS_KEY}"),
        ("zsh", "echo \"'$OPENAI_API_KEY'\""),
        # env NAME=value <program> can execute arbitrary code.
        ("zsh", "env FOO=bar ls"),
        ("zsh", "env NODE_ENV=production npm --version"),
        ("zsh", 'env FOO="bar baz" ls'),
        ("zsh", r"env FOO=bar\ baz ls"),
        # cmd-style %VAR% secret reads.
        ("cmd", "echo %OPENAI_API_KEY%"),
        # PowerShell Env: provider reads can expose secrets.
        ("pwsh", "Get-ChildItem Env:"),
        ("pwsh", "Get-Item Env:OPENAI_API_KEY"),
        ("pwsh", "Get-Content Env:OPENAI_API_KEY"),
        ("pwsh", "dir Env:"),
        ("pwsh", "Get-ChildItem Env:\\"),
        ("pwsh", r"Get-Item Env:\OPENAI_API_KEY"),
        ("pwsh", r"Get-Content Env:\OPENAI_API_KEY"),
        ("pwsh", "dir Env:\\"),
        ("pwsh", r"cat Env:\OPENAI_API_KEY"),
        ("pwsh", "Write-Output $Env:OPENAI_API_KEY"),
        ("pwsh", "Write-Output ${Env:OPENAI_API_KEY}"),
        ("pwsh", "Write-Output ok\nGet-ChildItem Env:"),
    ],
)
async def test_auto_judge_reviews_sensitive_shell_commands_over_readonly_carve_out(
    tool_name: str,
    command: str,
    tmp_path,
) -> None:
    """Sensitive commands never take the shell read-only auto-approval, whatever the shell dialect."""
    await _judge_reviews(
        tool_name,
        "shell",
        {"command": command},
        policy=_require_all_policy("shell", tool_names=(tool_name,)),
        tmp_path=tmp_path,
    )


async def test_quoted_sensitive_env_reference_keeps_readonly_auto_approval(tmp_path) -> None:
    """Single-quoted env references are inert text, not secret expansion."""
    bus = EventBus()
    judge = _FakeJudge(verdict=JudgeVerdict(approved=True, reason="reviewed"))
    mw = ApprovalMiddleware(
        approval_policy=_require_all_policy("shell", tool_names=("zsh",)),
        event_bus=bus,
        approval_mode=ApprovalMode.AUTO,
        approval_judge=judge,
        approval_log_dir=tmp_path,
    )
    h = await _Harness.attach(bus)

    await h.run(mw, _ctx("zsh", "shell", {"command": "echo '$OPENAI_API_KEY'"}))

    assert h.called
    assert h.requests == []
    assert judge.called_with is None
    assert mw.drain_decisions()[0]["status"] == "auto_approved"


@pytest.mark.parametrize(
    ("tool_name", "tool_kind", "args"),
    [
        # Sensitive shell/write targets are hard approval overrides.
        ("zsh", "shell", {"command": "cat .env"}),
        ("zsh", "shell", {"command": "echo $OPENAI_API_KEY"}),
        ("zsh", "shell", {"command": "echo $(printenv)"}),
        ("zsh", "shell", {"command": "echo `printenv`"}),
        ("zsh", "shell", {"command": "cat <(printenv)"}),
        ("zsh", "shell", {"command": "bash -lc 'echo $OPENAI_API_KEY'"}),
        ("zsh", "shell", {"command": "echo ok\nprintenv"}),
        ("zsh", "shell", {"command": "echo ok\nbash -lc 'echo $OPENAI_API_KEY'"}),
        ("zsh", "shell", {"command": "bash --norc -c 'printenv'"}),
        ("zsh", "shell", {"command": "bash --rcfile x -c 'printenv'"}),
        ("zsh", "shell", {"command": "bash --rcfile=x -c 'printenv'"}),
        ("zsh", "shell", {"command": 'command sh -c "printenv"'}),
        ("zsh", "shell", {"command": 'sudo sh -c "printenv"'}),
        ("zsh", "shell", {"command": 'echo $(command sh -c "printenv")'}),
        ("pwsh", "shell", {"command": "Write-Output ok\nGet-ChildItem Env:"}),
        ("pwsh", "shell", {"command": "Write-Output ${Env:OPENAI_API_KEY}"}),
        ("write_file", "filesystem.write", {"path": ".env", "content": "TOKEN=x"}),
        # Sensitive read targets must not silently pass through policy default=auto.
        ("read_file", "filesystem.read", {"path": ".env"}),
        ("read_file", "filesystem.read", {"path": ".envrc"}),
        ("view_image", "filesystem.read", {"path": ".aws/credentials"}),
        ("read_file", "filesystem.read", {"path": "aws_credentials"}),
        ("read_file", "filesystem.read", {"path": "credentials.yaml"}),
        ("read_file", "filesystem.read", {"path": ".config/gcloud/application_default_credentials.json"}),
        ("read_file", "filesystem.read", {"path": "~/.git-credentials"}),
        ("read_file", "filesystem.read", {"path": "~/Library/Keychains/login.keychain-db"}),
        ("read_file", "filesystem.read", {"path": "~/.kube/config"}),
        ("read_file", "filesystem.read", {"path": "~/.config/gh/hosts.yml"}),
        ("read_file", "filesystem.read", {"path": "~/.azure/accessTokens.json"}),
        ("read_file", "filesystem.read", {"path": "~/.azure/msal_token_cache.bin"}),
    ],
    ids=[
        "shell-cat-dotenv",
        "shell-env-var",
        "shell-dollar-subshell-printenv",
        "shell-backtick-printenv",
        "shell-process-substitution-printenv",
        "shell-bash-login-child",
        "shell-second-line-printenv",
        "shell-second-line-bash-child",
        "shell-bash-norc-child",
        "shell-bash-rcfile-arg-child",
        "shell-bash-rcfile-equals-child",
        "shell-command-builtin-child",
        "shell-sudo-child",
        "shell-nested-command-builtin-child",
        "pwsh-second-line-env-drive",
        "pwsh-env-variable",
        "write-dotenv",
        "read-dotenv",
        "read-dotenvrc",
        "view-image-aws-credentials",
        "read-aws-credentials-flat",
        "read-credentials-yaml",
        "read-gcloud-adc",
        "read-git-credentials",
        "read-macos-keychain",
        "read-kube-config",
        "read-gh-hosts",
        "read-azure-access-tokens",
        "read-azure-msal-cache",
    ],
)
async def test_auto_judge_reviews_sensitive_targets_even_when_policy_is_auto(
    tool_name: str,
    tool_kind: str,
    args: dict[str, str],
    tmp_path,
) -> None:
    """Sensitive shell, write, and read targets are hard approval overrides, not policy-auto calls."""
    policy = ApprovalPolicy(
        ApprovalConfig(default="auto", overrides={}),
        tools=[
            _mock_tool("zsh", kind="shell"),
            _mock_tool("pwsh", kind="shell"),
            _mock_tool("write_file", kind="filesystem.write"),
            _mock_tool("read_file", kind="filesystem.read"),
            _mock_tool("view_image", kind="filesystem.read"),
        ],
    )
    await _judge_reviews(tool_name, tool_kind, args, policy=policy, tmp_path=tmp_path)


@pytest.mark.parametrize(
    ("tool_name", "args"),
    [
        ("write_file", {"path": ".env", "content": "TOKEN=x"}),
        ("write_file", {"path": ".envrc", "content": "export TOKEN=x"}),
        ("edit_file", {"path": ".npmrc", "old_string": "a", "new_string": "b"}),
        ("write_file", {"path": ".aws/credentials", "content": "secret"}),
        ("write_file", {"path": "aws_credentials", "content": "secret"}),
        ("edit_file", {"path": "credentials.yml", "old_string": "a", "new_string": "b"}),
        (
            "write_file",
            {"path": ".config/gcloud/application_default_credentials.json", "content": "secret"},
        ),
    ],
)
async def test_auto_judge_reviews_sensitive_filesystem_writes_in_git_workspace(
    tool_name: str,
    args: dict[str, str],
    tmp_path,
) -> None:
    """Sensitive write/edit targets must not use workspace-git auto-approval."""
    root = _git_root(tmp_path)
    if args["path"].startswith(".aws/"):
        (root / ".aws").mkdir()

    await _judge_reviews(
        tool_name,
        "filesystem.write",
        args,
        policy=_require_all_policy("filesystem.write", tool_names=("write_file", "edit_file")),
        tmp_path=tmp_path,
        workspace_root=root,
    )


async def test_auto_judge_receives_all_current_turn_user_messages(tmp_path) -> None:
    """AUTO judge sees prior current-turn user messages plus the latest one."""
    bus = EventBus()
    judge = _FakeJudge(verdict=JudgeVerdict(approved=True, reason="safe read"))
    mw = ApprovalMiddleware(
        approval_policy=_require_all_policy("filesystem.write"),
        event_bus=bus,
        approval_mode=ApprovalMode.AUTO,
        approval_judge=judge,
        approval_log_dir=tmp_path,
    )
    h = await _Harness.attach(bus)

    mw.set_user_messages(["这个代码仓是做什么的?", "当前代码仓有多少行代码?"])
    await h.run(mw, _ctx("write_file", "filesystem.write", {"path": "/tmp/x"}))

    assert judge.called_with is not None
    assert judge.called_with["user_message"] == "当前代码仓有多少行代码?"
    assert judge.called_with["user_messages"] == ["这个代码仓是做什么的?", "当前代码仓有多少行代码?"]


async def test_auto_judge_approved_blocked_by_frontend_waits_for_user_response() -> None:
    """If the TUI has a user decision in flight, judge approval must not win the race."""
    bus = EventBus()
    judge = _FakeJudge(verdict=JudgeVerdict(approved=True, reason="safe enough"))
    mw = ApprovalMiddleware(
        approval_policy=_require_all_policy("filesystem.write"),
        event_bus=bus,
        approval_mode=ApprovalMode.AUTO,
        approval_judge=judge,
    )

    reviews: list[ApprovalReviewed] = []

    async def _rev(ev: ApprovalReviewed) -> None:
        reviews.append(ev)
        await bus.publish(ApprovalAutoFulfillBlocked(request_id=ev.request_id))
        await bus.publish(ApprovalResponse(request_id=ev.request_id, approved=False, reason="User declined"))

    await bus.subscribe(ApprovalReviewed, _rev)
    h = await _Harness.attach(bus)

    ctx = _ctx("write_file", "filesystem.write", {"path": "/tmp/x"})
    await h.run(mw, ctx)

    assert len(reviews) == 1
    assert not h.called
    assert ctx.result == "Error: Tool execution was rejected by user.\nUser reason: User declined"
    assert mw.drain_decisions() == [
        {
            "request_id": reviews[0].request_id,
            "tool_name": "write_file",
            "status": "user_rejected",
            "reason": "User declined",
        }
    ]


async def test_auto_fulfill_block_subscription_close_unsubscribes() -> None:
    """ApprovalMiddleware.close should release its EventBus handler."""
    bus = EventBus()
    judge = _FakeJudge(verdict=JudgeVerdict(approved=True, reason="safe enough"))
    mw = ApprovalMiddleware(
        approval_policy=_require_all_policy("filesystem.write"),
        event_bus=bus,
        approval_mode=ApprovalMode.AUTO,
        approval_judge=judge,
    )
    h = await _Harness.attach(bus)

    await h.run(mw, _ctx("write_file", "filesystem.write", {"path": "/tmp/x"}))
    assert h.called
    assert mw._on_auto_fulfill_blocked in bus._handlers[ApprovalAutoFulfillBlocked]

    await mw.close()

    assert mw._on_auto_fulfill_blocked not in bus._handlers[ApprovalAutoFulfillBlocked]
    assert mw._approval_arbiter._subscribed is False


async def test_auto_fulfill_block_subscription_close_is_idempotent() -> None:
    bus = EventBus()
    mw = ApprovalMiddleware(
        approval_policy=_require_all_policy("filesystem.write"),
        event_bus=bus,
        approval_mode=ApprovalMode.AUTO,
        approval_judge=_FakeJudge(verdict=JudgeVerdict(approved=True, reason="safe enough")),
    )

    await mw._ensure_auto_fulfill_block_subscription()
    await mw.close()
    await mw.close()

    assert mw._on_auto_fulfill_blocked not in bus._handlers[ApprovalAutoFulfillBlocked]


async def test_auto_judge_flagged_waits_for_user(tmp_path) -> None:
    """AUTO + judge flags → future is NOT auto-fulfilled; user still decides."""
    bus = EventBus()
    judge = _FakeJudge(verdict=JudgeVerdict(approved=False, reason="rm -rf detected"))
    mw = ApprovalMiddleware(
        approval_policy=_require_all_policy("filesystem.write"),
        event_bus=bus,
        approval_mode=ApprovalMode.AUTO,
        approval_judge=judge,
    )

    reviews: list[ApprovalReviewed] = []

    async def _rev(ev: ApprovalReviewed) -> None:
        reviews.append(ev)

    await bus.subscribe(ApprovalReviewed, _rev)

    # User approves manually after the judge has flagged. Use a background
    # task so the on_request handler returns immediately — otherwise it
    # blocks publish() and prevents the judge task from being created.
    async def _respond_later(rid: str) -> None:
        # Respond only after the judge has flagged (review published), so the
        # manual override deterministically follows the judge verdict.
        await wait_for(
            lambda: len(reviews) >= 1,
            timeout=ENGINE_TURN_TIMEOUT,
            description="approval review before response",
        )
        await bus.publish(ApprovalResponse(request_id=rid, approved=True, reason="user override"))

    called = False

    async def _next() -> None:
        nonlocal called
        called = True

    ctx = _ctx("write_file", "filesystem.write", {"path": "/tmp/x"})
    async with asyncio.TaskGroup() as responders:

        async def _on_request(ev: ApprovalRequest) -> None:
            responders.create_task(_respond_later(ev.request_id))

        await bus.subscribe(ApprovalRequest, _on_request)
        await mw.process(ctx, _next)

    assert called
    assert len(reviews) == 1
    assert reviews[0].approved is False
    assert reviews[0].reason == "rm -rf detected"


async def test_auto_judge_exception_flags_as_failure() -> None:
    """If the judge raises, publish a flagged ApprovalReviewed with the error reason."""
    bus = EventBus()
    judge = _FakeJudge(exc=RuntimeError("boom"))
    mw = ApprovalMiddleware(
        approval_policy=_require_all_policy("filesystem.write"),
        event_bus=bus,
        approval_mode=ApprovalMode.AUTO,
        approval_judge=judge,
    )

    reviews: list[ApprovalReviewed] = []

    async def _rev(ev: ApprovalReviewed) -> None:
        reviews.append(ev)

    await bus.subscribe(ApprovalReviewed, _rev)

    async def _respond_later(rid: str) -> None:
        # Respond only after the judge has flagged the failure (review
        # published), so the manual response follows the judge verdict.
        await wait_for(
            lambda: len(reviews) >= 1,
            timeout=ENGINE_TURN_TIMEOUT,
            description="rewritten approval review before response",
        )
        await bus.publish(ApprovalResponse(request_id=rid, approved=True, reason=""))

    async def _noop() -> None:
        pass

    async with asyncio.TaskGroup() as responders:

        async def _on_request(ev: ApprovalRequest) -> None:
            responders.create_task(_respond_later(ev.request_id))

        await bus.subscribe(ApprovalRequest, _on_request)
        await mw.process(_ctx("write_file", "filesystem.write", {"path": "/x"}), _noop)

    assert len(reviews) == 1
    assert reviews[0].approved is False
    assert "boom" in reviews[0].reason


async def test_auto_judge_cancelled_when_user_decides_first() -> None:
    """User responds before judge finishes → judge task is cancelled, no ApprovalReviewed published."""
    bus = EventBus()
    # Slow judge relative to the immediate user response.
    judge = _FakeJudge(verdict=JudgeVerdict(approved=True, reason="eventual"), delay=0.01)
    mw = ApprovalMiddleware(
        approval_policy=_require_all_policy("filesystem.write"),
        event_bus=bus,
        approval_mode=ApprovalMode.AUTO,
        approval_judge=judge,
    )

    reviews: list[ApprovalReviewed] = []

    async def _rev(ev: ApprovalReviewed) -> None:
        reviews.append(ev)

    await bus.subscribe(ApprovalReviewed, _rev)

    async def _respond_fast(rid: str) -> None:
        # User decides almost immediately (0 delay — runs as soon as control yields)
        await bus.publish(ApprovalResponse(request_id=rid, approved=True, reason="I trust it"))

    async def _on_request(ev: ApprovalRequest) -> None:
        asyncio.create_task(_respond_fast(ev.request_id))  # noqa: RUF006

    await bus.subscribe(ApprovalRequest, _on_request)

    async def _noop() -> None:
        pass

    await mw.process(_ctx("write_file", "filesystem.write", {"path": "/x"}), _noop)

    # Judge was cancelled mid-sleep → no review ever published
    assert reviews == []


async def test_auto_mode_without_judge_falls_back_to_manual() -> None:
    """AUTO mode but no judge configured → behaves like MANUAL (no judging, no review)."""
    bus = EventBus()
    mw = ApprovalMiddleware(
        approval_policy=_require_all_policy("filesystem.write"),
        event_bus=bus,
        approval_mode=ApprovalMode.AUTO,
        approval_judge=None,
    )
    h = await _Harness.attach(bus)
    reviews: list[ApprovalReviewed] = []

    async def _rev(ev: ApprovalReviewed) -> None:
        reviews.append(ev)

    await bus.subscribe(ApprovalReviewed, _rev)

    async def _respond(rid: str) -> None:
        await bus.publish(ApprovalResponse(request_id=rid, approved=True, reason=""))

    async def _on_req(ev: ApprovalRequest) -> None:
        asyncio.create_task(_respond(ev.request_id))  # noqa: RUF006

    await bus.subscribe(ApprovalRequest, _on_req)

    await h.run(mw, _ctx("write_file", "filesystem.write", {"path": "/x"}))

    assert len(h.requests) == 1
    assert h.requests[0].judging is False  # no judge → dialog shows normal state
    assert reviews == []  # no auto-review ever published


class _CancelOnRequestedSink(FakeSink):
    """A sink whose request marker is interrupted, as an Esc on its write ack would be."""

    async def emit(self, draft, *, payload_factory=None):  # type: ignore[no-untyped-def]
        if draft.event_type == EventType.APPROVAL_REQUESTED:
            raise asyncio.CancelledError
        return await super().emit(draft, payload_factory=payload_factory)


class _WatchedJudge:
    """A judge that reports when it was reached and whether it was cancelled."""

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.cancelled = False

    async def evaluate(self, **kwargs) -> JudgeVerdict:
        self.entered.set()
        try:
            await asyncio.sleep(ENGINE_TURN_TIMEOUT)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        return JudgeVerdict(approved=True, reason="never reached")


async def test_an_interrupted_request_marker_reaps_the_handler_without_starting_the_judge(tmp_path) -> None:
    """Cancelling the request write clears its subscription without starting a model call."""
    bus = EventBus()
    judge = _WatchedJudge()
    mw = ApprovalMiddleware(
        approval_policy=_require_all_policy("filesystem.write"),
        event_bus=bus,
        approval_mode=ApprovalMode.AUTO,
        approval_judge=judge,
        approval_log_dir=tmp_path,
    )
    subscribed_before = len(bus._handlers[ApprovalResponse])

    async def _next() -> None:
        raise AssertionError("the tool must not run")

    with (
        trajectory_scope(make_context(_CancelOnRequestedSink())),
        pytest.raises(asyncio.CancelledError),
    ):
        await mw.process(_ctx("write_file", "filesystem.write", {"path": "/tmp/x"}), _next)

    # Nothing of this request outlives it: no handler left on the bus, no
    # judge left calling the model for a dialog that is already gone.
    assert len(bus._handlers[ApprovalResponse]) == subscribed_before
    assert not judge.entered.is_set()
    assert judge.cancelled is False


async def test_interrupting_a_pending_approval_records_its_resolution() -> None:
    """An abandoned request still resolves — a reader can never close it otherwise."""
    bus = EventBus()
    mw = ApprovalMiddleware(approval_policy=_require_all_policy(), event_bus=bus)
    requests: list[ApprovalRequest] = []

    async def _collect(ev: ApprovalRequest) -> None:
        requests.append(ev)

    await bus.subscribe(ApprovalRequest, _collect)

    async def _next() -> None:
        raise AssertionError("the tool must not run")

    sink = FakeSink()
    with trajectory_scope(make_context(sink)):
        task = asyncio.create_task(mw.process(_ctx("write_file", "shell", {"path": "x"}), _next))
        assert await wait_until(lambda: len(requests) >= 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    resolved = sink.only(EventType.APPROVAL_RESOLVED)
    assert resolved.payload["decision"] == ApprovalDecision.INTERRUPTED
    assert resolved.payload["decider"] == ApprovalDecider.NONE
    assert (
        resolved.payload["approval_request_id"]
        == sink.only(EventType.APPROVAL_REQUESTED).payload["approval_request_id"]
    )
    assert resolved.payload["wait_ms"] >= 0


@pytest.mark.asyncio
async def test_approval_cancelled_during_request_publication_unsubscribes() -> None:
    bus = EventBus()
    publishing = asyncio.Event()
    blocked = asyncio.Event()

    async def block_request(_event: ApprovalRequest) -> None:
        publishing.set()
        await blocked.wait()

    await bus.subscribe(ApprovalRequest, block_request)
    middleware = ApprovalMiddleware(approval_policy=_require_all_policy(), event_bus=bus)
    harness = await _Harness.attach(bus)
    task = harness.start(middleware, _ctx("write_file", "shell", {"command": "touch file"}))
    try:
        await wait_for(lambda: publishing.is_set() or task.done(), description="request publication or early exit")
        if task.done():
            await task  # Surface an early failure instead of a publication timeout.
        assert publishing.is_set()
        assert len(bus._handlers[ApprovalResponse]) == 1
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert bus._handlers[ApprovalResponse] == []
    assert harness.called is False
