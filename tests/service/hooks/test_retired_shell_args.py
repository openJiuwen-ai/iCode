# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Hooks written for the shell tool's retired argument names keep working (deprecated shim).

The shell tool renamed ``working_dir`` → ``workdir`` and ``max_tokens`` →
``max_output_tokens``. These tests go away with
``service/hooks/retired_shell_args.py``.
"""

from __future__ import annotations

import json
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest

from chrys.service.hooks.events import HookEvent
from chrys.service.hooks.manager import HookManager
from chrys.service.hooks.matcher import matches
from chrys.service.hooks.retired_shell_args import (
    renamed_override,
    retired_arg_names_warning,
    with_new_names,
    with_retired_names,
)
from chrys.service.hooks.run_log import HookRunLog
from chrys.service.hooks.schema import (
    HookArgMatch,
    HookConfig,
    HookExecution,
    HookMatch,
    HookRun,
    HookSettings,
    HooksFile,
)
from tests.support.waiting import ENGINE_TURN_TIMEOUT, wait_for

_BLOCK_WITH_ARG_NAMES = textwrap.dedent(
    """
    import json, os
    with open(os.environ["CHRYS_HOOK_PAYLOAD_FILE"], encoding="utf-8") as f:
        args = json.load(f)["tool"]["args"]
    with open(os.environ["CHRYS_HOOK_RESULT"], "w") as f:
        json.dump({"action": "block", "reason": json.dumps(args, sort_keys=True)}, f)
    """
)


def _echo_args_changing(name: str, value: str) -> str:
    """A script that returns every argument it read, with *name* changed to *value*."""
    return textwrap.dedent(
        f"""
        import json, os
        with open(os.environ["CHRYS_HOOK_PAYLOAD_FILE"], encoding="utf-8") as f:
            args = json.load(f)["tool"]["args"]
        args[{name!r}] = {value!r}
        with open(os.environ["CHRYS_HOOK_RESULT"], "w") as f:
            json.dump({{"action": "modify", "args_override": args}}, f)
        """
    )


def _hook(script: str, match: HookMatch | None = None) -> HookConfig:
    return HookConfig(
        id="h",
        event=HookEvent.BEFORE_TOOL_CALL,
        run=HookRun(type="command", argv=[sys.executable, "-c", script]),
        execution=HookExecution(mode="blocking", timeout_seconds=30.0),
        match=match or HookMatch(),
    )


def _call(kind: str = "shell", **args: object) -> dict[str, Any]:
    return {"profile": "Code", "tool": {"name": "zsh", "kind": kind, "call_id": "c1", "args": args}}


@pytest.fixture(params=[False, True], ids=["no-run-log", "run-log"])
def manager_for(request: pytest.FixtureRequest, tmp_path: Path):
    """A manager running *hooks*, with or without the run log (which writes the payload text itself)."""

    async def _ignore_change() -> None:
        return None

    def build(*hooks: HookConfig, **settings: float) -> HookManager:
        manager = HookManager(file=HooksFile(settings=HookSettings(**settings), hooks=list(hooks)), hooks_dir=tmp_path)
        if request.param:
            manager.record_runs(HookRunLog(), session_id="s1", on_change=_ignore_change)
        return manager

    return build


async def test_match_on_a_retired_name_still_guards_shell_calls(manager_for) -> None:
    guard = _hook(
        _BLOCK_WITH_ARG_NAMES,
        HookMatch(tool_kind="shell", args={"working_dir": HookArgMatch(contains="prod")}),
    )
    manager = manager_for(guard)

    decision = await manager.fire(HookEvent.BEFORE_TOOL_CALL, _call(command="rm -rf build", workdir="prod/app"))
    elsewhere = await manager.fire(HookEvent.BEFORE_TOOL_CALL, _call(command="rm -rf build", workdir="dev/app"))

    assert decision.blocked is True
    assert elsewhere.blocked is False


def test_match_redirect_is_only_for_shell_tools_without_the_old_name() -> None:
    hook = _hook("pass", HookMatch(args={"working_dir": HookArgMatch(contains="prod")}))

    assert matches(hook, _call(workdir="prod")) is True
    assert matches(hook, _call(kind="mcp", workdir="prod")) is False
    # A tool that really takes working_dir is matched on it, not on workdir.
    assert matches(hook, _call(working_dir="dev", workdir="prod")) is False
    assert matches(hook, _call(kind="mcp", working_dir="prod")) is True


async def test_script_reads_retired_names_beside_the_new_ones(manager_for) -> None:
    manager = manager_for(_hook(_BLOCK_WITH_ARG_NAMES))
    payload = _call(command="make", workdir="sub", max_output_tokens=500)

    decision = await manager.fire(HookEvent.BEFORE_TOOL_CALL, payload)

    assert decision.block_reason.endswith(
        '{"command": "make", "max_output_tokens": 500, "max_tokens": 500, "workdir": "sub", "working_dir": "sub"}'
    )
    assert payload["tool"]["args"] == {"command": "make", "workdir": "sub", "max_output_tokens": 500}


async def test_override_with_a_retired_name_sets_the_new_one(manager_for) -> None:
    script = textwrap.dedent(
        """
        import json, os
        with open(os.environ["CHRYS_HOOK_RESULT"], "w") as f:
            json.dump({"action": "modify", "args_override": {"working_dir": "/safe", "max_tokens": 50}}, f)
        """
    )
    manager = manager_for(_hook(script))

    decision = await manager.fire(HookEvent.BEFORE_TOOL_CALL, _call(command="make", workdir="/prod"))

    assert decision.args_override == {"workdir": "/safe", "max_output_tokens": 50}


@pytest.mark.parametrize(("changed", "expected"), [("working_dir", "/old-style"), ("workdir", "/new-style")])
async def test_override_echoing_both_names_keeps_the_one_the_hook_changed(
    manager_for, changed: str, expected: str
) -> None:
    manager = manager_for(_hook(_echo_args_changing(changed, expected)))

    decision = await manager.fire(HookEvent.BEFORE_TOOL_CALL, _call(command="make", workdir="/prod"))

    assert decision.args_override == {"command": "make", "workdir": expected}


def test_renamed_override_leaves_other_tools_and_real_old_names_alone() -> None:
    override = {"working_dir": "/x"}

    assert renamed_override(_call(kind="mcp", workdir="/a")["tool"], override) is override
    assert renamed_override(_call(working_dir="/a")["tool"], override) == {"working_dir": "/x"}
    assert renamed_override(_call(workdir="/a")["tool"], {"workdir": "/b", "working_dir": "/c"}) == {"workdir": "/b"}


@pytest.mark.parametrize(
    ("call", "expected"),
    [
        # The call left workdir out: an explicit null is a change (the session's folder), so the new name wins.
        (_call(command="make"), {"workdir": None}),
        # Unchanged as JSON only when the call already had that very value.
        (_call(command="make", workdir=None), {"workdir": "/other"}),
        (_call(command="make", workdir="/a"), {"workdir": None}),
    ],
    ids=["absent-then-null", "null-kept", "set-then-null"],
)
def test_explicit_null_for_the_new_name_is_a_change(call: dict[str, Any], expected: dict[str, object]) -> None:
    assert renamed_override(call["tool"], {"workdir": None, "working_dir": "/other"}) == expected


async def test_explicit_null_workdir_from_a_hook_keeps_the_session_folder(manager_for) -> None:
    script = textwrap.dedent(
        """
        import json, os
        with open(os.environ["CHRYS_HOOK_RESULT"], "w") as f:
            json.dump({"action": "modify", "args_override": {"workdir": None, "working_dir": "/other"}}, f)
        """
    )
    manager = manager_for(_hook(script))

    decision = await manager.fire(HookEvent.BEFORE_TOOL_CALL, _call(command="make"))

    assert decision.args_override == {"workdir": None}


def test_payload_without_new_names_or_of_other_tools_is_shown_as_is() -> None:
    for payload in (_call(command="ls"), _call(kind="mcp", workdir="/a"), {"event": "after_turn"}):
        assert with_retired_names(payload) is payload


async def test_a_job_pending_since_before_the_rename_shows_its_script_the_new_names(
    manager_for, tmp_path: Path
) -> None:
    seen = tmp_path / "seen.json"
    script = textwrap.dedent(
        f"""
        import json, os
        with open(os.environ["CHRYS_HOOK_PAYLOAD_FILE"], encoding="utf-8") as f:
            args = json.load(f)["tool"]["args"]
        with open({str(seen)!r}, "w", encoding="utf-8") as f:
            json.dump(args, f, sort_keys=True)
        """
    )
    hook = HookConfig(
        id="audit",
        event=HookEvent.AFTER_TOOL_CALL,
        run=HookRun(type="command", argv=[sys.executable, "-c", script]),
        execution=HookExecution(mode="async", timeout_seconds=30.0, delivery="durable"),
    )
    manager = manager_for(hook, outbox_retry_age_seconds=0.0)
    # Written by a release whose shell still took the retired names.
    manager._outbox.write_pending(
        hook_id="audit",
        event=str(HookEvent.AFTER_TOOL_CALL),
        payload=_call(command="make", working_dir="sub", max_tokens=500),
    )

    assert await manager.recover_outbox() == 1
    await wait_for(seen.exists, timeout=ENGINE_TURN_TIMEOUT, description="recovered job ran")
    await manager.drain_session()

    assert json.loads(seen.read_text(encoding="utf-8")) == {
        "command": "make",
        "max_output_tokens": 500,
        "max_tokens": 500,
        "workdir": "sub",
        "working_dir": "sub",
    }


def test_new_names_are_added_only_to_shell_payloads_lacking_them() -> None:
    old_shell = _call(command="ls", working_dir="/a")
    assert with_new_names(old_shell)["tool"]["args"] == {"command": "ls", "working_dir": "/a", "workdir": "/a"}
    assert old_shell["tool"]["args"] == {"command": "ls", "working_dir": "/a"}
    for payload in (_call(working_dir="/a", workdir="/b"), _call(kind="mcp", working_dir="/a"), {"turn": 1}):
        assert with_new_names(payload) is payload


_RENAME = "but rename it before support for the old names ends"
_ONLY_IF_SHELL = "If the hook is for the shell, add tool_kind: shell and use the new name"


@pytest.mark.parametrize(
    ("match", "warned", "advice"),
    [
        (HookMatch(tool_kind="shell", args={"working_dir": HookArgMatch()}), ["working_dir"], _RENAME),
        (HookMatch(tool_kind="shell", args={"max_tokens": HookArgMatch()}), ["max_tokens"], _RENAME),
        (
            HookMatch(tool_kind="shell", tool_name="mksh", args={"working_dir": HookArgMatch()}),
            ["working_dir"],
            _RENAME,
        ),
        # Without tool_kind no name settles it: an MCP tool may be called bash, a shell mksh,
        # and a tool taking working_dir itself keeps it. Renaming is only for the shell.
        (HookMatch(args={"working_dir": HookArgMatch()}), ["working_dir"], _ONLY_IF_SHELL),
        (HookMatch(tool_name="bash", args={"working_dir": HookArgMatch()}), ["working_dir"], _ONLY_IF_SHELL),
        (HookMatch(tool_name="mksh", args={"working_dir": HookArgMatch()}), ["working_dir"], _ONLY_IF_SHELL),
        (
            HookMatch(tool_name="deploy_remote", args={"working_dir": HookArgMatch()}),
            ["working_dir"],
            _ONLY_IF_SHELL,
        ),
        # read_file and web_fetch still take max_tokens: a hook not limited to the shell kind means those.
        (HookMatch(args={"max_tokens": HookArgMatch()}), [], ""),
        (HookMatch(tool_name="zsh", args={"max_tokens": HookArgMatch()}), [], ""),
        (HookMatch(tool_kind="mcp", args={"working_dir": HookArgMatch()}), [], ""),
        (HookMatch(tool_kind="shell", args={"workdir": HookArgMatch()}), [], ""),
    ],
    ids=[
        "shell-kind-working-dir",
        "shell-kind-max-tokens",
        "shell-kind-any-name",
        "no-tool",
        "mcp-tool-named-bash",
        "shell-named-mksh",
        "other-tool-name",
        "no-kind-max-tokens",
        "shell-name-max-tokens",
        "mcp-kind",
        "new-name",
    ],
)
def test_warning_tells_to_rename_only_where_the_hook_is_for_the_shell(
    match: HookMatch, warned: list[str], advice: str
) -> None:
    warning = retired_arg_names_warning(_hook("pass", match))

    if not warned:
        assert warning is None
        return
    assert warning is not None
    assert advice in warning
    assert (_RENAME in warning) is (advice == _RENAME)
    for name in warned:
        assert f"{name!r} is now" in warning
