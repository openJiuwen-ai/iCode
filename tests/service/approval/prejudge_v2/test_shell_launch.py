# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Exercise structured Bash authorization at the configured shell boundary."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
from contextlib import asynccontextmanager, suppress
from dataclasses import replace
from typing import TYPE_CHECKING

import pytest

from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import ApprovalRequest, ApprovalResponse
from chrys.foundation.models.session_env import SessionEnvironment
from chrys.foundation.platform import ShellInfo, get_platform
from chrys.foundation.tool_result_metadata import TOOL_ERROR_KIND_METADATA_KEY
from chrys.kernel.middleware import FunctionInvocationContext
from chrys.service.agent_middleware.control.approval import ApprovalMiddleware
from chrys.service.approval.policy import ApprovalMode, ApprovalPolicy
from chrys.service.approval.prejudge_v2 import Route, load_policy, route
from chrys.service.approval.prejudge_v2.execution import isolated_shell_call
from chrys.service.mutations.trace import shell_trace_argv
from chrys.service.profiles.agents.schema import ApprovalConfig
from chrys.service.tools.builtins import shell as shell_module
from chrys.service.tools.builtins.shell import ShellTools
from chrys.service.tools.result_metadata import tool_result_metadata
from tests.support.prejudge import require_grammars
from tests.support.symlinks import junction_or_skip, symlink_or_skip
from tests.support.waiting import ENGINE_TURN_TIMEOUT, wait_for

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable
    from pathlib import Path

    from chrys.kernel.tools import FunctionTool

require_grammars("tree_sitter", "tree_sitter_bash")


@pytest.fixture(params=["bash", "git_bash"])
def bash_tool(request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ShellTools:
    platform = get_platform()
    if platform.is_windows != (request.param == "git_bash"):
        pytest.skip("This shell test runs on its native platform")

    if platform.is_windows:
        shell = next((item for item in platform.extra_shells if item.name == "git_bash"), None)
    else:
        binary = shutil.which("bash")
        shell = ShellInfo("bash", binary, ["-c"]) if binary else None
    if shell is None:
        if os.environ.get("CI"):
            pytest.fail(f"{request.param} is required for native CI coverage")
        pytest.skip(f"{request.param} is unavailable")

    for key in ("HOME", "USERPROFILE", "APPDATA"):
        monkeypatch.setenv(key, str(tmp_path))
    for key in tuple(os.environ):
        if key.upper() in {"BASH_ENV", "ENV"} or key.upper().startswith("BASH_FUNC_"):
            monkeypatch.delenv(key)

    return ShellTools(SessionEnvironment.capture(), shell=shell)


async def _write_policy(matches: list[dict[str, list[str]]], *, mode: str = "enforce") -> None:
    path = get_platform().config_dir / "prejudge.yaml"
    document = {
        "schema_version": 1,
        "mode": mode,
        "policy_id": "native-launch-test",
        "rules": [
            {"id": f"rule-{index}", "effect": "allow", "target": "command", "match": match}
            for index, match in enumerate(matches)
        ],
    }
    await asyncio.to_thread(path.parent.mkdir, parents=True, exist_ok=True)
    await asyncio.to_thread(path.write_text, json.dumps(document), encoding="utf-8")


@asynccontextmanager
async def _approval(
    tool: FunctionTool,
    *,
    approve: bool = False,
    workspace_roots: list[str] | None = None,
) -> AsyncIterator[tuple[ApprovalMiddleware, list[ApprovalRequest]]]:
    bus = EventBus()
    middleware = ApprovalMiddleware(
        ApprovalPolicy(ApprovalConfig(default="require"), tools=[tool]),
        bus,
        approval_mode=ApprovalMode.MANUAL,
        workspace_roots=workspace_roots,
    )
    requests: list[ApprovalRequest] = []

    async def respond(request: ApprovalRequest) -> None:
        requests.append(request)
        await bus.publish(ApprovalResponse(request_id=request.request_id, approved=approve))

    await bus.subscribe(ApprovalRequest, respond)
    try:
        yield middleware, requests
    finally:
        await bus.unsubscribe(ApprovalRequest, respond)
        await middleware.close()


async def _invoke(middleware: ApprovalMiddleware, tool: FunctionTool, source: str) -> FunctionInvocationContext:
    context = FunctionInvocationContext(tool, {"command": source, "reason": "Verify configured shell", "timeout": 15})

    async def execute() -> None:
        context.result = await tool.invoke(context=context, skip_parsing=True)

    await middleware.process(context, execute)
    return context


def _stdout(result: str) -> str:
    body, _, status = result.rpartition("\n")
    assert status == "[exit_code: 0]"
    return body.rstrip("\n")


@pytest.mark.parametrize(
    ("source", "commands", "expected"),
    [
        ("echo hello", [["echo", "hello"]], "hello\n"),
        ("printf 'hello\\n' | head -n 1", [["printf", "hello\\n"], ["head", "-n", "1"]], "hello\n"),
        ("{ echo left; echo right; }", [["echo", "left"], ["echo", "right"]], "left\nright\n"),
    ],
    ids=["builtin", "pipeline", "compound"],
)
async def test_certified_bash_source_uses_the_configured_shell(bash_tool, source, commands, expected, monkeypatch):
    await _write_policy([{"argv_exact": argv} for argv in commands])
    tool = bash_tool.tools()[0]
    original_launch = shell_module._shell_launch
    seen: list[tuple[str, ...]] = []

    def capture(shell: ShellInfo, command: str, *, pipe: bool = False):
        variants, env = original_launch(shell, command, pipe=pipe)
        seen.extend(variants)
        return variants, env

    monkeypatch.setattr(shell_module, "_shell_launch", capture)
    async with _approval(tool) as (middleware, requests):
        result = await _invoke(middleware, tool, source)
        assert _stdout(result.result) == expected.rstrip("\n")
        assert not requests
        assert [entry["status"] for entry in middleware.drain_decisions()] == ["prejudge_approved"]
        assert isolated_shell_call.get() is None

    (argv,) = seen
    assert argv[0] == bash_tool.shell.path
    assert argv[-1] == source
    assert argv[1:3] == ("--noprofile", "--norc")


async def test_bash_git_status_allows_exact_bare_name(bash_tool, git_repo_factory: Callable, tmp_path: Path):
    repository = await asyncio.to_thread(git_repo_factory, tmp_path / "repo")
    tool = bash_tool.tools()[0]
    await _write_policy([{"argv_exact": ["git", "status", "--short", "--branch"]}])
    async with _approval(tool) as (middleware, requests):
        context = FunctionInvocationContext(
            tool,
            {"command": "git status --short --branch", "reason": "Verify bare Git name", "workdir": str(repository)},
        )

        async def execute() -> None:
            context.result = await tool.invoke(context=context, skip_parsing=True)

        await middleware.process(context, execute)
        assert context.result.startswith("## ")
        assert context.result.endswith("[exit_code: 0]")
        assert not requests
        assert [entry["status"] for entry in middleware.drain_decisions()] == ["prejudge_approved"]


async def test_bash_startup_isolation_is_local_to_an_approved_call_and_retry(bash_tool, tmp_path, monkeypatch):
    initializer = tmp_path / "ordinary-startup.sh"
    await asyncio.to_thread(initializer.write_text, "printf 'ordinary-startup\\n'\n", encoding="utf-8", newline="\n")
    inherited = {
        "BASH_ENV": initializer.as_posix(),
        "ENV": initializer.as_posix(),
        "BASH_FUNC_ICODE_TEST_PROBE%%": "() { printf 'function-control\\n'; }",
        "ICODE_TEST_CONTROL": "keep-me",
    }
    for key, value in inherited.items():
        monkeypatch.setenv(key, value)
    path_before = os.environ["PATH"]
    approved_source = "echo isolated-call"
    manual_source = "declare -F ICODE_TEST_PROBE; echo manual-call"
    await _write_policy([{"argv_exact": ["echo", "isolated-call"]}])
    original_launch = shell_module._shell_launch
    observed: dict[str, tuple[list[tuple[str, ...]], dict[str, str]]] = {}

    def capture(shell: ShellInfo, command: str, *, pipe: bool = False):
        variants, env = original_launch(shell, command, pipe=pipe)
        observed[command] = (variants, env.copy())
        return variants, env

    monkeypatch.setattr(shell_module, "_shell_launch", capture)
    tool = bash_tool.tools()[0]
    # The missing wrapper forces the real launch code to retry with its bare variant.
    wrapper = (str(tmp_path / "missing-trace-wrapper"),)
    entered: set[str] = set()

    async with _approval(tool, approve=True) as (middleware, requests):

        async def invoke(source: str, *, isolated: bool) -> FunctionInvocationContext:
            context = FunctionInvocationContext(tool, {"command": source, "reason": "Check startup environment"})

            async def execute() -> None:
                assert (isolated_shell_call.get() is not None) is isolated
                entered.add(source)
                await wait_for(lambda: len(entered) == 2, timeout=ENGINE_TURN_TIMEOUT)
                context.result = await tool.invoke(context=context, skip_parsing=True)

            trace = shell_trace_argv.set(wrapper if isolated else ())
            try:
                await middleware.process(context, execute)
                assert isolated_shell_call.get() is None
            finally:
                shell_trace_argv.reset(trace)
            return context

        async with asyncio.TaskGroup() as calls:
            isolated = calls.create_task(invoke(approved_source, isolated=True))
            manual = calls.create_task(invoke(manual_source, isolated=False))

        assert _stdout(isolated.result().result) == "isolated-call"
        ordinary = _stdout(manual.result().result)
        assert "ordinary-startup\n" in ordinary
        assert ordinary.endswith("manual-call")
        if not get_platform().is_macos:
            assert "ICODE_TEST_PROBE\n" in ordinary
        assert len(requests) == 1 and requests[0].args["command"] == manual_source
        assert {entry["status"] for entry in middleware.drain_decisions()} == {"prejudge_approved", "user_approved"}

    ((traced, bare), isolated_env) = observed[approved_source]
    ((ordinary_argv,), ordinary_env) = observed[manual_source]
    assert traced == (*wrapper, *bare)
    assert bare[0] == ordinary_argv[0] == bash_tool.shell.path
    assert bare[-1] == approved_source and ordinary_argv[-1] == manual_source
    assert isolated_env["PATH"] == ordinary_env["PATH"]
    assert isolated_env["ICODE_TEST_CONTROL"] == inherited["ICODE_TEST_CONTROL"]
    for key, value in inherited.items():
        assert ordinary_env[key] == os.environ[key] == value
        if key != "ICODE_TEST_CONTROL":
            assert key not in isolated_env
    assert os.environ["PATH"] == path_before


@pytest.mark.parametrize(
    ("source", "expected_sensitive"),
    [
        ("review_fixture_tool sample\r", False),
        ("review_fixture_tool sample\f", False),
        ("review_fixture_tool sample\v", False),
        ("review_fixture_tool sample\\\t", False),
        ("review_fixture_tool sample\\\r\n", False),
        ("review_fixture_tool sample $", False),
        ('review_fixture_tool $"sample"', False),
        ("review_fixture_tool .e'n'v", True),
        (r"review_fixture_tool $'\x2eenv'", True),
        ("review_fixture_tool sample | head -n 1", False),
        ("open harmless.txt", False),
        ("osascript harmless.fixture", False),
    ],
)
async def test_uncertified_or_sensitive_bash_source_never_reaches_execution(
    bash_tool,
    source,
    expected_sensitive,
    monkeypatch,
):
    await _write_policy(
        [{"argv_first": ["review_fixture_tool"]}, {"argv_first": ["open"]}, {"argv_first": ["osascript"]}]
    )
    policy = load_policy(get_platform().config_dir / "prejudge.yaml")
    decision = route(source, bash_tool.shell.name, policy)
    assert decision.route is Route.DEFER
    assert decision.sensitive is expected_sensitive
    tool = bash_tool.tools()[0]

    def forbidden(shell: ShellInfo, command: str, *, pipe: bool = False):
        pytest.fail("Uncertified or protected source reached the subprocess boundary")

    monkeypatch.setattr(shell_module, "_shell_launch", forbidden)
    async with _approval(tool) as (middleware, requests):
        context = await _invoke(middleware, tool, source)
        assert "rejected by user" in str(context.result).lower()
        assert len(requests) == 1
        assert [entry["status"] for entry in middleware.drain_decisions()] == ["user_rejected"]


@pytest.mark.parametrize("changed", ["source", "dialect", "task"])
async def test_bash_approval_invalidates_when_identity_changes(bash_tool, changed, monkeypatch):
    source = "echo approved-source"
    await _write_policy([{"argv_exact": ["echo", "approved-source"]}])
    tool = bash_tool.tools()[0]
    context = FunctionInvocationContext(tool, {"command": source, "reason": "Check authorization binding"})

    def forbidden(shell: ShellInfo, command: str, *, pipe: bool = False):
        pytest.fail("A changed invocation reached the subprocess boundary")

    monkeypatch.setattr(shell_module, "_shell_launch", forbidden)

    async def execute() -> None:
        target = bash_tool
        actual_source = source
        if changed == "dialect":
            target = ShellTools(SessionEnvironment.capture(), shell=replace(bash_tool.shell, name="sh"))
        if changed == "source":
            # Simulate a downstream hook changing benign source after approval.
            actual_source = "echo changed-source"

        async def launch() -> str:
            return await target.execute(actual_source, reason="Check authorization binding")

        if changed == "task":
            context.result = await asyncio.create_task(launch())
        else:
            context.result = await launch()

    async with _approval(tool) as (middleware, requests):
        metadata: dict[str, object] = {}
        token = tool_result_metadata.set(metadata)
        try:
            await middleware.process(context, execute)
        finally:
            tool_result_metadata.reset(token)
        assert not requests
        assert metadata[TOOL_ERROR_KIND_METADATA_KEY] == "approval_invalidated"
        assert isolated_shell_call.get() is None


@pytest.mark.parametrize("cancel", [False, True], ids=["exception", "cancellation"])
async def test_bash_approval_binding_is_cleared_on_interrupted_execution(bash_tool, cancel):
    source = "echo approved-source"
    await _write_policy([{"argv_exact": ["echo", "approved-source"]}])
    tool = bash_tool.tools()[0]
    context = FunctionInvocationContext(tool, {"command": source, "reason": "Check authorization cleanup"})
    entered = asyncio.Event()
    parked = asyncio.Event()
    bindings = []

    async def execute() -> None:
        binding = isolated_shell_call.get()
        assert binding is not None
        bindings.append(binding)
        entered.set()
        if cancel:
            await parked.wait()
        raise RuntimeError("fixture failure")

    async with _approval(tool) as (middleware, requests):

        async def invoke() -> None:
            try:
                await middleware.process(context, execute)
            finally:
                assert isolated_shell_call.get() is None

        if cancel:
            task = asyncio.create_task(invoke())
            try:
                await wait_for(lambda: entered.is_set() or task.done(), timeout=ENGINE_TURN_TIMEOUT)
                assert entered.is_set() and not task.done()
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            finally:
                if not task.done():
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task
        else:
            with pytest.raises(RuntimeError, match="fixture failure"):
                await invoke()

        assert len(bindings) == 1 and not bindings[0].active
        assert not requests
        assert isolated_shell_call.get() is None


@pytest.mark.parametrize(
    ("source", "decision", "workspace_path"),
    [
        ("echo approved", Route.ALLOW, False),
        ("review_fixture_tool .e'n'v", Route.DEFER, False),
        ("review_fixture_tool sample # note", Route.DEFER, False),
        ("review_fixture_tool sample", Route.DEFER, True),
    ],
    ids=["certified", "sensitive", "unsupported", "workspace-path"],
)
async def test_bash_shadow_predicts_the_structured_enforce_decision(
    bash_tool,
    source,
    decision,
    workspace_path,
    tmp_path,
    monkeypatch,
    caplog,
):
    tool = bash_tool.tools()[0]
    workspace = tmp_path / "workspace"
    await asyncio.to_thread(workspace.mkdir)
    if workspace_path:
        monkeypatch.setenv("PATH", os.pathsep.join((str(workspace), os.environ["PATH"])))
    logger = "chrys.service.agent_middleware.control.approval.prejudge"
    caplog.set_level(logging.INFO, logger=logger)
    for mode in ("shadow", "enforce"):
        await _write_policy([{"argv_exact": ["echo", "approved"]}, {"argv_first": ["review_fixture_tool"]}], mode=mode)
        async with _approval(tool, workspace_roots=[str(workspace)]) as (middleware, requests):
            context = await _invoke(middleware, tool, source)
            observed = [record.getMessage() for record in caplog.records if record.name == logger]
            (prediction,) = [message for message in observed if message.startswith(f"{mode} route=")]
            assert prediction.startswith(f"{mode} route={decision.value} ")
            assert source not in prediction
            (recorded,) = middleware.drain_decisions()
            if decision is Route.ALLOW:
                assert _stdout(context.result) == "approved"
                assert not requests
                assert recorded["status"] == ("prejudge_approved" if mode == "enforce" else "auto_approved")
            else:
                assert "rejected by user" in str(context.result).lower()
                assert len(requests) == 1
                assert recorded["status"] == "user_rejected"


async def test_bash_approved_launch_rechecks_a_retargeted_path_link(bash_tool, tmp_path, monkeypatch):
    outside = tmp_path / "outside-workspace"
    workspace = tmp_path / "workspace"
    await asyncio.to_thread(outside.mkdir)
    await asyncio.to_thread(workspace.mkdir)
    link = tmp_path / "path-link"
    if get_platform().is_windows:
        await asyncio.to_thread(junction_or_skip, link, outside)
    else:
        await asyncio.to_thread(symlink_or_skip, link, outside, target_is_directory=True)
    monkeypatch.setenv("PATH", os.pathsep.join((str(link), os.environ["PATH"])))
    source = "echo approved-source"
    await _write_policy([{"argv_exact": ["echo", "approved-source"]}])
    tool = bash_tool.tools()[0]
    context = FunctionInvocationContext(tool, {"command": source, "reason": "Verify path resolution recheck"})

    def forbidden(shell: ShellInfo, command: str, *, startup_args: tuple[str, ...] = ()):
        pytest.fail("Retargeted path reached the subprocess boundary")

    monkeypatch.setattr(shell_module, "_shell_argv_variants", forbidden)

    async def execute() -> None:
        assert isolated_shell_call.get() is not None
        if get_platform().is_windows:
            await asyncio.to_thread(link.rmdir)
            await asyncio.to_thread(junction_or_skip, link, workspace)
        else:
            await asyncio.to_thread(link.unlink)
            await asyncio.to_thread(symlink_or_skip, link, workspace, target_is_directory=True)
        context.result = await tool.invoke(context=context, skip_parsing=True)

    async with _approval(tool, workspace_roots=[str(workspace)]) as (middleware, requests):
        metadata: dict[str, object] = {}
        token = tool_result_metadata.set(metadata)
        try:
            await middleware.process(context, execute)
        finally:
            tool_result_metadata.reset(token)
        assert not requests
        assert metadata[TOOL_ERROR_KIND_METADATA_KEY] == "approval_invalidated"
