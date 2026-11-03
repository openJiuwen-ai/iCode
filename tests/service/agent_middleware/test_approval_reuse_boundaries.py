# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Approval reuse must preserve execution boundaries and unrelated tool behavior."""

from __future__ import annotations

import asyncio
import os
from dataclasses import replace
from datetime import date
from types import ModuleType
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from pydantic import create_model

from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import ApprovalRequest, ApprovalResponse
from chrys.foundation.models.session_env import SessionEnvironment
from chrys.foundation.models.workspace import Workspace
from chrys.kernel import FunctionTool
from chrys.kernel.middleware import FunctionInvocationContext
from chrys.service.agent_middleware.control.approval import ApprovalMiddleware
from chrys.service.approval import reuse as reuse_module
from chrys.service.approval.policy import ApprovalPolicy
from chrys.service.approval.reuse import FileCandidate, FileKey, ReuseContext, project_path
from chrys.service.approval.reuse_binding import ApprovalReuseBinding
from chrys.service.profiles.agents.schema import ApprovalConfig
from chrys.service.tools import approval_targets
from chrys.service.tools.approval_targets import file_write_target
from chrys.service.tools.builtins import shell as shell_module
from chrys.service.tools.builtins.filesystem import FilesystemTools
from chrys.service.tools.builtins.shell import ShellTools
from tests.kernel._fakes import _call_response, _result_contents, _stack, _text_response, _user
from tests.support.symlinks import symlink_or_skip


@pytest.fixture
def runtime(tmp_path):
    env = SessionEnvironment.capture("session-a", Workspace.from_cwd(str(tmp_path)))
    return replace(env, platform=replace(env.platform, config_dir=tmp_path / "config"))


def reuse_candidate(binding, context):
    return binding.prepare(context, reusable=True).candidate


def retarget(link, destination):
    link.unlink()
    symlink_or_skip(link, destination, target_is_directory=True)


def fold_case_like_windows(monkeypatch):
    """Let path identities see the case folding Windows' normcase applies."""
    windows_like = ModuleType("os")
    windows_like.__dict__.update(vars(os))
    windows_like.path = ModuleType("os.path")
    windows_like.path.__dict__.update(vars(os.path))
    windows_like.path.normcase = str.lower
    for module in (approval_targets, reuse_module):
        monkeypatch.setattr(module, "os", windows_like)


@pytest.mark.parametrize("change", ["cwd", "case", "path", "args"])
def test_session_grant_binds_execution_context(runtime, monkeypatch, change):
    fold_case_like_windows(monkeypatch)
    if change == "case":
        runtime = replace(runtime, cwd=os.path.join(runtime.cwd, "Project"))
    tools = ShellTools(runtime).tools()
    binding = ApprovalReuseBinding(runtime, tools)
    args = {"command": "git reset --hard"}
    candidate = reuse_candidate(binding, FunctionInvocationContext(tools[0], args))
    assert candidate is not None
    assert binding.service.remember(candidate, "EXACT_SESSION")
    if change == "cwd":
        runtime = replace(runtime, cwd=runtime.cwd + "/other")
    elif change == "case":
        # A case-sensitive directory can hold both as different projects.
        runtime = replace(runtime, cwd=os.path.join(os.path.dirname(runtime.cwd), "project"))
    else:
        shell = replace(runtime.platform.shell, **{change: "/other/shell" if change == "path" else ["-l", "-c"]})
        runtime = replace(runtime, platform=replace(runtime.platform, shell=shell))
    tools = ShellTools(runtime).tools()
    rebuilt = ApprovalReuseBinding(runtime, tools)
    assert not rebuilt.service.match(reuse_candidate(rebuilt, FunctionInvocationContext(tools[0], args)))


@pytest.mark.parametrize(("annotation", "wire"), [(date, "2026-10-01"), (UUID, "00000000-0000-0000-0000-000000000001")])
@pytest.mark.parametrize("enabled", [False, True])
async def test_custom_typed_tool_keeps_ordinary_approval(runtime, annotation, wire, enabled):
    executed, requests = [], []

    async def consume(value: Any) -> str:
        executed.append(value)
        return "done"

    tool = FunctionTool(name="typed_tool", func=consume, input_model=create_model("Typed", value=(annotation, ...)))
    bus = EventBus()
    binding = ApprovalReuseBinding(runtime, [tool])
    middleware = ApprovalMiddleware(
        ApprovalPolicy(ApprovalConfig(default="require")), bus, reuse=binding if enabled else None
    )

    async def approve(event):
        requests.append(event)
        await bus.publish(ApprovalResponse(request_id=event.request_id, approved=True))

    await bus.subscribe(ApprovalRequest, approve)
    try:
        layer, _ = _stack(
            [_call_response(("typed", tool.name, {"value": wire})), _text_response()], middleware=middleware
        )
        response = await layer.get_response([_user()], options={"tools": [tool]})
        assert len(requests) == len(executed) == 1
        assert isinstance(executed[0], annotation)
        assert _result_contents(response)[0].result == "done"
        assert not binding.service.rules()
    finally:
        await middleware.close()
        await bus.unsubscribe(ApprovalRequest, approve)


async def test_write_through_an_alias_to_credentials_is_never_remembered(runtime, tmp_path):
    docker = tmp_path / "home" / ".docker"
    docker.mkdir(parents=True)
    symlink_or_skip(tmp_path / "out", docker, target_is_directory=True)
    tools = FilesystemTools(runtime).tools()
    tool = next(tool for tool in tools if tool.name == "write_file")
    binding = ApprovalReuseBinding(runtime, tools)
    args = {"path": "out/config.json", "content": "{}", "overwrite": True}
    # Even a grant naming the physical file, as an older build may have saved, is not used.
    physical = file_write_target(args["path"], base_cwd=runtime.cwd).path
    stale = FileCandidate(
        ReuseContext(binding.session_id, project_path(runtime.cwd)), frozenset({FileKey(path=physical)})
    )
    assert binding.service.remember(stale, "EXACT_SESSION")
    bus, requests = EventBus(), []

    async def approve(event):
        requests.append(event)
        await bus.publish(ApprovalResponse(request_id=event.request_id, approved=True, remember_choice="EXACT_SESSION"))

    called = AsyncMock(spec=lambda: None)
    middleware = ApprovalMiddleware(ApprovalPolicy(ApprovalConfig(default="require")), bus, reuse=binding)
    await bus.subscribe(ApprovalRequest, approve)
    try:
        for _ in range(2):
            await middleware.process(FunctionInvocationContext(tool, dict(args)), called)
        assert len(requests) == called.await_count == 2
        assert all(event.reuse_offer is None for event in requests)
        assert len(binding.service.rules()) == 1
    finally:
        await middleware.close()
        await bus.unsubscribe(ApprovalRequest, approve)


@pytest.mark.parametrize("approved_by", ["grant", "person"])
@pytest.mark.parametrize("names", [("first", "second"), ("Project", "project")], ids=["other", "case"])
async def test_shell_call_does_not_follow_a_retargeted_working_directory(
    runtime, tmp_path, monkeypatch, approved_by, names
):
    first, second, link = tmp_path / names[0], tmp_path / names[1], tmp_path / "work"
    first.mkdir()
    if second.exists():
        pytest.skip("Directories differing only in case need a case-sensitive filesystem")
    second.mkdir()
    symlink_or_skip(link, first, target_is_directory=True)
    fold_case_like_windows(monkeypatch)
    args = {"command": "echo ran > ran.txt", "reason": "test"}
    if approved_by == "grant":
        runtime = replace(runtime, cwd=str(link))
    else:
        args["workdir"] = link.name
    tools = ShellTools(runtime).tools()
    binding = ApprovalReuseBinding(runtime, tools)
    context = FunctionInvocationContext(tools[0], args)
    bus, requests = EventBus(), []
    if approved_by == "grant":
        assert binding.service.remember(reuse_candidate(binding, context), "EXACT_SESSION")
        match = binding.service.match

        def match_then_retarget(candidate):
            grants = match(candidate)
            retarget(link, second)
            return grants

        monkeypatch.setattr(binding.service, "match", match_then_retarget)

    async def approve(event):
        requests.append(event)
        await asyncio.to_thread(retarget, link, second)
        await bus.publish(ApprovalResponse(request_id=event.request_id, approved=True))

    async def execute():
        context.result = await context.function.invoke(context=context, skip_parsing=True)

    middleware = ApprovalMiddleware(ApprovalPolicy(ApprovalConfig(default="require")), bus, reuse=binding)
    await bus.subscribe(ApprovalRequest, approve)
    try:
        await middleware.process(context, execute)
        assert len(requests) == (approved_by == "person")
        assert "Working directory changed after approval" in str(context.result)
        assert not (first / "ran.txt").exists()
        assert not (second / "ran.txt").exists()
    finally:
        await middleware.close()
        await bus.unsubscribe(ApprovalRequest, approve)


async def test_approved_command_starts_in_the_checked_directory(runtime, tmp_path, monkeypatch):
    first, second, link = tmp_path / "First", tmp_path / "Second", tmp_path / "work"
    first.mkdir()
    second.mkdir()
    symlink_or_skip(link, first, target_is_directory=True)
    fold_case_like_windows(monkeypatch)
    runtime = replace(runtime, cwd=str(link))
    tools = ShellTools(runtime).tools()
    context = FunctionInvocationContext(tools[0], {"command": "echo ran > ran.txt", "reason": "test"})
    bus, started_in = EventBus(), []
    checked = shell_module.physical_dir

    def check_then_retarget(path):
        # The link changes right after the final check, before the command starts.
        result = checked(path)
        retarget(link, second)
        return result

    for name in ("_execute_pipe", "_execute_pty"):
        backend = getattr(ShellTools, name)

        async def record(self, command, shell, cwd, timeout, reason="", backend=backend):
            started_in.append(cwd)
            return await backend(self, command, shell, cwd, timeout, reason)

        monkeypatch.setattr(ShellTools, name, record)

    async def approve(event):
        await bus.publish(ApprovalResponse(request_id=event.request_id, approved=True))

    async def execute():
        monkeypatch.setattr(shell_module, "physical_dir", check_then_retarget)
        context.result = await context.function.invoke(context=context, skip_parsing=True)

    middleware = ApprovalMiddleware(
        ApprovalPolicy(ApprovalConfig(default="require")), bus, reuse=ApprovalReuseBinding(runtime, tools)
    )
    await bus.subscribe(ApprovalRequest, approve)
    try:
        await middleware.process(context, execute)
        assert "[exit_code: 0]" in str(context.result)
        # The real directory, in its own casing.
        assert started_in == [os.path.realpath(first)]
        assert (first / "ran.txt").exists()
        assert not (second / "ran.txt").exists()
    finally:
        await middleware.close()
        await bus.unsubscribe(ApprovalRequest, approve)


@pytest.mark.parametrize("operation", ["write_file", "edit_file"])
async def test_approved_write_to_a_session_document_handle_is_still_refused(runtime, tmp_path, operation):
    tools = FilesystemTools(runtime).tools()
    tool = next(tool for tool in tools if tool.name == operation)
    binding = ApprovalReuseBinding(runtime, tools)
    args = {"path": "chrys-session-document:report.md"}
    if operation == "write_file":
        args.update(content="changed", overwrite=True)
    else:
        args.update(old_string="original", new_string="changed")
    context = FunctionInvocationContext(tool, args)
    bus, requests = EventBus(), []

    async def approve(event):
        requests.append(event)
        await bus.publish(ApprovalResponse(request_id=event.request_id, approved=True))

    async def execute():
        context.result = await context.function.invoke(context=context, skip_parsing=True)

    middleware = ApprovalMiddleware(ApprovalPolicy(ApprovalConfig(default="require")), bus, reuse=binding)
    await bus.subscribe(ApprovalRequest, approve)
    try:
        await middleware.process(context, execute)
        assert len(requests) == 1 and requests[0].reuse_offer is None
        assert "read-only" in str(context.result)
        assert not any(path.name.startswith("chrys-session-document") for path in tmp_path.iterdir())
    finally:
        await middleware.close()
        await bus.unsubscribe(ApprovalRequest, approve)
