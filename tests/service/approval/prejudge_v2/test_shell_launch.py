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
from pathlib import Path
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
from chrys.service.approval.prejudge_v2.execution import (
    ApprovalInvalidatedError,
    _trusted_locations,
    isolated_shell_call,
    prepare_shell,
)
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
    # Keep native positive controls independent of stale runner PATH entries.
    monkeypatch.setenv(
        "PATH",
        os.pathsep.join(
            path for path in os.environ["PATH"].split(os.pathsep) if Path(path).is_absolute() and Path(path).is_dir()
        ),
    )

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
        ("cp .e'n'v out", True),
        (r"cp $'\x2eenv' out", True),
        ("cat .env", True),
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
    await _write_policy([{"argv_first": [head]} for head in ("review_fixture_tool", "open", "osascript", "cp", "cat")])
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


@pytest.mark.parametrize("changed", ["source", "dialect", "args", "path", "task"])
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
        if changed == "args":
            target = ShellTools(SessionEnvironment.capture(), shell=replace(bash_tool.shell, args=["--norc", "-c"]))
        if changed == "path":
            target = ShellTools(SessionEnvironment.capture(), shell=replace(bash_tool.shell, path="other-bash"))
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
    ("source", "decision", "location"),
    [
        ("echo approved", Route.ALLOW, "outside"),
        ("review_fixture_tool .e'n'v", Route.DEFER, "outside"),
        ("review_fixture_tool sample # note", Route.DEFER, "outside"),
        ("review_fixture_tool sample", Route.DEFER, "workspace"),
        ("review_fixture_tool sample", Route.DEFER, "home"),
        ("review_fixture_tool sample", Route.DEFER, "venv"),
    ],
    ids=["certified", "sensitive", "unsupported", "workspace-path", "home-workspace", "activated-venv"],
)
async def test_bash_shadow_predicts_the_structured_enforce_decision(
    bash_tool,
    source,
    decision,
    location,
    tmp_path,
    monkeypatch,
    caplog,
):
    tool = bash_tool.tools()[0]
    workspace = tmp_path if location == "home" else tmp_path / "workspace"
    lookup_dir = {
        "outside": tmp_path / "trusted-bin",
        "workspace": workspace / "bin",
        "home": workspace / ".local" / "bin",
        "venv": workspace / ".venv" / ("Scripts" if os.name == "nt" else "bin"),
    }[location]
    await asyncio.to_thread(workspace.mkdir, exist_ok=True)
    await asyncio.to_thread(lookup_dir.mkdir, parents=True, exist_ok=True)
    monkeypatch.setenv("PATH", os.pathsep.join((str(lookup_dir), os.environ["PATH"])))
    if location == "venv":
        monkeypatch.setenv("VIRTUAL_ENV", str(workspace / ".venv"))
    logger = "chrys.service.agent_middleware.control.approval.prejudge"
    caplog.set_level(logging.INFO, logger=logger)
    for mode in ("shadow", "enforce"):
        await _write_policy([{"argv_exact": ["echo", "approved"]}, {"argv_first": ["review_fixture_tool"]}], mode=mode)
        async with _approval(tool, workspace_roots=[str(workspace)]) as (middleware, requests):
            context = await _invoke(middleware, tool, source)
            observed = [record.getMessage() for record in caplog.records if record.name == logger]
            (prediction,) = [message for message in observed if message.startswith(f"{mode} route=")]
            assert prediction.startswith(f"{mode} route={decision.value} ")
            if location != "outside":
                assert "reason=untrusted_launch_context" in prediction
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


@pytest.mark.parametrize("mode", ["shadow", "enforce"])
async def test_custom_shell_args_defer_before_launch(bash_tool, mode, caplog, monkeypatch):
    source = "echo normal-launch"
    await _write_policy([{"argv_exact": ["echo", "normal-launch"]}], mode=mode)
    custom_shell = replace(bash_tool.shell, args=["-e", "-c"])
    tool = ShellTools(SessionEnvironment.capture(), shell=custom_shell).tools()[0]
    logger = "chrys.service.agent_middleware.control.approval.prejudge"
    caplog.set_level(logging.INFO, logger=logger)
    normal_launch = shell_module._shell_launch
    launches = []

    def record(shell: ShellInfo, command: str, *, pipe: bool = False):
        assert isolated_shell_call.get() is None
        launches.append((shell.args, command))
        return normal_launch(shell, command, pipe=pipe)

    monkeypatch.setattr(shell_module, "_shell_launch", record)
    async with _approval(tool) as (middleware, requests):
        context = await _invoke(middleware, tool, source)
        assert _stdout(context.result) == "normal-launch"
        assert launches == [(custom_shell.args, source)]
        assert not requests
        assert [entry["status"] for entry in middleware.drain_decisions()] == ["auto_approved"]
        assert f"{mode} route=defer reason=unsupported_shell_configuration" in caplog.text


def _directory_alias(link: Path, target: Path) -> None:
    if get_platform().is_windows:
        junction_or_skip(link, target)
    else:
        symlink_or_skip(link, target, target_is_directory=True)


def test_path_trust_checks_workspace_entries_and_intermediate_link_targets(tmp_path):
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    inside_executable = workspace / "fixture-tool"
    inside_executable.touch()
    outside_executable = outside / "fixture-tool"
    outside_executable.touch()
    outward = workspace / "outward"
    inward = tmp_path / "inward"
    indirect = tmp_path / "indirect"
    _directory_alias(outward, outside)
    _directory_alias(inward, workspace)
    _directory_alias(indirect, outward)
    assert indirect.resolve() == outside.resolve()
    roots = (str(workspace),)
    assert _trusted_locations((str(outside), str(outside_executable)), roots) is not None
    for path in (workspace, inside_executable, outward, inward, indirect, outward / outside_executable.name):
        assert _trusted_locations((str(path),), roots) is None


async def test_workspace_path_aliases_defer_at_approval(bash_tool, tmp_path, monkeypatch, caplog):
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    await asyncio.to_thread(workspace.mkdir)
    await asyncio.to_thread(outside.mkdir)
    outward = workspace / "bin"
    await asyncio.to_thread(_directory_alias, outward, outside)
    monkeypatch.setenv("PATH", str(outward))
    source = "fixture-tool sample"
    await _write_policy([{"argv_exact": source.split()}])
    tool = bash_tool.tools()[0]
    with caplog.at_level(logging.INFO, logger="chrys.service.agent_middleware.control.approval.prejudge"):
        async with _approval(tool, workspace_roots=[str(workspace)]) as (middleware, requests):
            context = await _invoke(middleware, tool, source)
            assert context.result is not None and "rejected by user" in str(context.result).lower()
            assert len(requests) == 1
    assert "enforce route=defer reason=untrusted_launch_context" in caplog.text


async def test_case_insensitive_workspace_path_alias_defers(bash_tool, tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    directory = workspace / "bin"
    await asyncio.to_thread(directory.mkdir, parents=True)
    alias = tmp_path / "WORKSPACE" / "BIN"
    if not await asyncio.to_thread(alias.exists):
        pytest.skip("Temporary filesystem is case-sensitive")
    assert await asyncio.to_thread(alias.samefile, directory)
    roots = (str(workspace),)
    assert _trusted_locations((str(alias),), roots) is None
    monkeypatch.setenv("PATH", str(alias))
    assert prepare_shell(bash_tool.shell, "echo fixture", ("echo",), roots) is None


async def test_explicit_workspace_executable_defers_and_unavailable_path_invalidates(bash_tool, tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    directory = tmp_path / "external-bin"
    await asyncio.to_thread(workspace.mkdir)
    await asyncio.to_thread(directory.mkdir)
    executable = workspace / "fixture-tool"
    await asyncio.to_thread(executable.touch)
    monkeypatch.setenv("PATH", str(directory))
    roots = (str(workspace),)
    assert prepare_shell(bash_tool.shell, str(executable), (str(executable),), roots) is None
    approved = prepare_shell(bash_tool.shell, "echo fixture", ("echo",), roots)
    assert approved is not None
    assert approved.claim(bash_tool.shell, "echo fixture")
    await asyncio.to_thread(directory.rmdir)
    assert prepare_shell(bash_tool.shell, "echo fixture", ("echo",), roots) is not None
    with pytest.raises(ApprovalInvalidatedError):
        approved.require_launch(bash_tool.shell, "echo fixture")


async def test_bash_approval_uses_realistic_path_with_missing_and_inert_entries(
    bash_tool, tmp_path, monkeypatch, caplog
):
    first, second, workspace = (tmp_path / name for name in ("system-bin", "extra-bin", "workspace"))
    inert = tmp_path / "not-a-directory"
    for directory in (first, second, workspace):
        await asyncio.to_thread(directory.mkdir)
    await asyncio.to_thread(inert.touch)
    search_path = (first, tmp_path / "removed-bin", second, tmp_path / "uninstalled" / "bin", inert)
    monkeypatch.setenv("PATH", os.pathsep.join(str(path) for path in search_path))
    source = "echo configured-bash"
    await _write_policy([{"argv_exact": source.split()}])
    roots = [str(workspace)]
    assert prepare_shell(bash_tool.shell, source, ("echo",), tuple(roots)) is not None
    tool = bash_tool.tools()[0]
    async with _approval(tool, workspace_roots=roots) as (middleware, requests):
        result = await _invoke(middleware, tool, source)
        assert _stdout(result.result) == "configured-bash"
        assert not requests
        assert [entry["status"] for entry in middleware.drain_decisions()] == ["prejudge_approved"]
        monkeypatch.setenv("PATH", os.pathsep.join((os.environ["PATH"], str(workspace))))
        with caplog.at_level(logging.INFO, logger="chrys.service.agent_middleware.control.approval.prejudge"):
            result = await _invoke(middleware, tool, source)
        assert _stdout(result.result) == "configured-bash"
        assert not requests
        assert [entry["status"] for entry in middleware.drain_decisions()] == ["auto_approved"]
        assert "enforce route=defer reason=untrusted_launch_context" in caplog.text


async def test_bash_path_tolerance_excludes_workspace_and_required_locations(bash_tool, tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    await asyncio.to_thread(workspace.mkdir)
    await asyncio.to_thread(outside.mkdir)
    workspace_link = workspace / "alias"
    await asyncio.to_thread(_directory_alias, workspace_link, outside)
    roots = (str(workspace),)
    source = "echo fixture"
    for path in ("", "relative", str(workspace), str(workspace / "missing" / "bin"), str(workspace_link / "missing")):
        monkeypatch.setenv("PATH", os.pathsep.join((str(outside), path)))
        assert prepare_shell(bash_tool.shell, source, ("echo",), roots) is None
    monkeypatch.setenv("PATH", str(outside))
    missing = str(outside / "missing")
    for required in (missing, str(outside)):
        assert prepare_shell(replace(bash_tool.shell, path=required), source, ("echo",), roots) is None
        assert prepare_shell(bash_tool.shell, required, (required,), roots) is None
    assert prepare_shell(bash_tool.shell, source, ("echo",), (missing,)) is None

    lstat = Path.lstat

    def uninspectable(path: Path) -> os.stat_result:
        if str(path) == missing:
            raise PermissionError("Uninspectable path")
        return lstat(path)

    monkeypatch.setattr(Path, "lstat", uninspectable)
    monkeypatch.setenv("PATH", os.pathsep.join((str(outside), missing)))
    assert prepare_shell(bash_tool.shell, source, ("echo",), roots) is None


@pytest.mark.parametrize("change", ["missing-created", "inert-replaced", "candidate-added", "ancestor-replaced"])
async def test_bash_path_changes_invalidate_approval_before_subprocess(bash_tool, tmp_path, monkeypatch, change):
    parent = tmp_path / "external"
    directory = parent / "search-bin"
    await asyncio.to_thread(parent.mkdir)
    if change == "inert-replaced":
        await asyncio.to_thread(directory.touch)
    elif change == "candidate-added":
        await asyncio.to_thread(directory.mkdir)
    monkeypatch.setenv("PATH", os.pathsep.join((str(directory), os.environ["PATH"])))
    source = "echo approved-source"
    await _write_policy([{"argv_exact": source.split()}])
    tool = bash_tool.tools()[0]
    context = FunctionInvocationContext(tool, {"command": source, "reason": "Recheck recorded PATH state"})

    def forbidden(shell: ShellInfo, command: str, *, startup_args: tuple[str, ...] = ()):
        pytest.fail("A changed PATH snapshot reached the subprocess boundary")

    def change_path() -> None:
        if change == "inert-replaced":
            directory.unlink()
        if change in {"inert-replaced", "missing-created"}:
            directory.mkdir()
        elif change == "candidate-added":
            (directory / "echo").touch()
        else:
            parent.rename(tmp_path / "old-external")
            parent.mkdir()

    monkeypatch.setattr(shell_module, "_shell_argv_variants", forbidden)

    async def execute() -> None:
        assert isolated_shell_call.get() is not None
        await asyncio.to_thread(change_path)
        context.result = await tool.invoke(context=context, skip_parsing=True)

    async with _approval(tool) as (middleware, requests):
        metadata: dict[str, object] = {}
        token = tool_result_metadata.set(metadata)
        try:
            await middleware.process(context, execute)
        finally:
            tool_result_metadata.reset(token)
        assert not requests
        assert metadata[TOOL_ERROR_KIND_METADATA_KEY] == "approval_invalidated"


@pytest.mark.parametrize("new_target", ["workspace", "outside"])
async def test_bash_approved_launch_rechecks_a_retargeted_path_link(bash_tool, tmp_path, monkeypatch, new_target):
    outside = tmp_path / "outside-workspace"
    workspace = tmp_path / "workspace"
    await asyncio.to_thread(outside.mkdir)
    await asyncio.to_thread(workspace.mkdir)
    replacement = workspace if new_target == "workspace" else tmp_path / "other-external-directory"
    await asyncio.to_thread(replacement.mkdir, exist_ok=True)
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
            await asyncio.to_thread(junction_or_skip, link, replacement)
        else:
            await asyncio.to_thread(link.unlink)
            await asyncio.to_thread(symlink_or_skip, link, replacement, target_is_directory=True)
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
