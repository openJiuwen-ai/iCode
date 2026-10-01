# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Core DAA contracts for matching, persistence, revocation and invalid records."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace

import pytest

from chrys.service.approval.daa import (
    Candidate,
    CommandSessionKey,
    DAAService,
    FileApprovalKey,
    ReuseContext,
    canonical,
)
from chrys.service.approval.daa_command import normalize_simple_command
from chrys.service.approval.daa_store import DAAStore


@pytest.fixture
def service(tmp_path):
    return DAAService(DAAStore(tmp_path / "rules.sqlite3"))


@pytest.mark.parametrize("scope", ["SESSION", "PROJECT"])
def test_paths_are_individual_scoped_grants(service, scope):
    ctx = ReuseContext("a", "/project")
    approved = service.file_candidate(("/a", "/b", "/a"), ctx)
    assert approved is not None
    assert approved.file_keys == {FileApprovalKey(path="/a"), FileApprovalKey(path="/b")}
    assert service.remember(approved, f"EXACT_{scope}")
    assert len(service.rules()) == 2
    for paths, expected in [(("/a",), "HIT_ALLOW"), (("/a", "/b"), "HIT_ALLOW"), (("/a", "/c"), "MISS")]:
        assert service.match(service.file_candidate(paths, ctx)) == expected
    for paths in ((), None):
        assert service.file_candidate(paths, ctx) is None
        assert service.match(service.file_candidate(paths, ctx)) == "MISS"
    assert service.match(replace(approved, file_keys=frozenset())) == "MISS"
    assert not service.remember(replace(approved, file_keys=frozenset()), f"EXACT_{scope}")
    assert service.match(replace(approved, context=replace(ctx, session_id="b"))) == (
        "HIT_ALLOW" if scope == "PROJECT" else "MISS"
    )
    assert service.match(replace(approved, context=replace(ctx, project_id="/other"))) == (
        "HIT_ALLOW" if scope == "SESSION" else "MISS"
    )


def test_paths_must_all_match_in_one_scope(service):
    ctx = ReuseContext("a", "/project")
    a = service.file_candidate(("/a",), ctx)
    b = service.file_candidate(("/b",), ctx)
    assert a and b
    assert service.remember(a, "EXACT_SESSION")
    assert service.remember(b, "EXACT_PROJECT")
    assert service.match(service.file_candidate(("/a", "/b"), ctx)) == "MISS"


@pytest.mark.parametrize("shell", ["bash", "sh", "zsh", "git_bash", "cmd", "powershell", "pwsh"])
def test_command_session_uses_shell_and_local_ordered_tokens(service, shell):
    ctx = ReuseContext("a", "/project", "original legacy context")
    request = service.candidates("tool-v1", {"command": "git   push\torigin main", "reason": "first"}, ctx, shell=shell)
    assert request is not None
    assert request.command_key == CommandSessionKey(shell=shell, command=("git", "push", "origin", "main"))
    assert service.remember(request, "EXACT_SESSION")
    changed = service.candidates(
        "rebuilt-tool",
        {
            "command": "git push origin main",
            "reason": "changed",
            "timeout": 60,
            "max_tokens": 10,
            "profile": "new",
        },
        replace(ctx, execution="changed", project_id="/different"),
        shell=shell,
    )
    assert changed and changed.command_key == request.command_key
    assert service.match(changed) == "HIT_ALLOW"
    assert service.match(replace(changed, context=replace(ctx, session_id="b"))) == "MISS"
    for command in (
        "git push origin feature",
        "git push origin main --force",
        "git push origin",
        "git push main origin",
        "git pushx origin main",
    ):
        assert service.match(service.candidates("tool-v1", {"command": command}, ctx, shell=shell)) == "MISS"


@pytest.mark.parametrize("shell", ["bash", "cmd", "powershell", "pwsh"])
@pytest.mark.parametrize(
    "command",
    [
        "git status && whoami",
        "git status || whoami",
        "git status;whoami",
        "git status | cat",
        "git status >out",
        "git status <in",
        "git status &",
        "git status\nwhoami",
        "echo $HOME",
        "echo $(whoami)",
        "echo `whoami`",
        "if true",
        "for x in y",
        "return 1",
        "break",
        "repeat 2 echo ok",
        "echo *",
        "echo [ab]",
        "echo {a,b}",
    ],
)
def test_unsupported_commands_cannot_create_session_grants(service, shell, command):
    assert normalize_simple_command(command, shell) is None
    request = service.candidates("tool", {"command": command}, ReuseContext("a", "/p"), shell=shell)
    assert request is not None
    assert not service.remember(request, "EXACT_SESSION")
    assert service.match(request) == "MISS"
    assert not service.rules()


def test_multi_path_storage_rolls_back_if_any_insert_fails(service):
    assert not service.store.add_many([("same-id", "first"), ("same-id", "second")])
    assert service.store.load() == []


@pytest.fixture
def context():
    return ReuseContext("session-a", "/project", canonical({"cwd": "/project", "shell": "/bin/bash", "env": "a"}))


def candidate(service, context, arguments=None, tool="bash"):
    result = service.candidates(
        tool, arguments or {"command": "npm run dev"}, context, shell="" if tool == "read_file" else tool
    )
    assert result is not None
    return result


def test_revocation_is_visible_to_existing_service_instances(service, context):
    request = candidate(service, context)
    assert service.remember(request, "EXACT_PROJECT")
    second = DAAService(DAAStore(service.store.path))
    assert second.match(request) == "HIT_ALLOW"
    assert service.revoke(service.rules()[0].id)
    assert second.match(request) == "MISS"
    assert not service.revoke("unknown")
    assert service.remember(request, "PREFIX_SESSION")
    assert service.store.clear()
    assert second.match(request) == "MISS"


@pytest.mark.parametrize(
    "mutation",
    [
        "bad_json",
        "missing_key",
        "missing_shell",
        "extra",
        "kind",
        "scope",
        "source",
        "wrong_type",
        "empty_prefix",
        "row_id",
    ],
)
def test_corrupt_records_never_allow(service, context, mutation):
    request = candidate(service, context)
    assert service.remember(request, "EXACT_SESSION")
    rule = json.loads(service.store.load()[0][1])
    assert rule["kind"] == "COMMAND_SESSION"
    if mutation == "missing_key":
        del rule["key"]
    elif mutation == "missing_shell":
        del rule["key"]["shell"]  # Legacy session grants must not authorize any shell.
    elif mutation == "extra":
        rule["future"] = True
    elif mutation in {"kind", "scope", "source"}:
        rule[mutation] = "UNKNOWN"
    elif mutation == "wrong_type":
        rule["scope_id"] = 12
    elif mutation == "empty_prefix":
        rule.pop("key")
        rule.update(kind="PREFIX", prefix=[], prefix_base=request.prefix_base)
    if mutation == "row_id":
        rule["id"] = "f" * 32
    payload = "{" if mutation == "bad_json" else json.dumps(rule)
    with sqlite3.connect(service.store.path) as db:
        db.execute("UPDATE rules SET payload=?", (payload,))
    assert service.match(request) == "MISS"


@pytest.mark.parametrize("shell", ["bash", "sh", "zsh", "git_bash", "cmd", "powershell", "pwsh"])
@pytest.mark.parametrize("working_dir", ["/elsewhere", "../main", ".", "~", r"C:\work", "", None])
def test_explicit_working_dir_cannot_reuse_command_session(service, shell, working_dir):
    ctx = ReuseContext("a", "/project")
    arguments = {"command": "git reset --hard"}
    approved = service.candidates("tool", arguments, ctx, shell=shell)
    assert approved and service.remember(approved, "EXACT_SESSION")
    redirected = {**arguments, "working_dir": working_dir}
    request = service.candidates("tool", redirected, ctx, shell=shell)
    assert service.match(request) == "MISS"
    assert request is None
    assert service.command_candidate(redirected, ctx, shell=shell) is None
    assert service.match(approved) == "HIT_ALLOW"


@pytest.mark.parametrize("choice", ["EXACT_PROJECT", "PREFIX_SESSION", "PREFIX_PROJECT"])
@pytest.mark.parametrize("field", ["reason", "timeout", "max_tokens", "environment"])
def test_command_reuse_ignores_non_execution_options(service, context, choice, field):
    args = {"command": "npm run test", "reason": "first", "timeout": 30, "max_tokens": 8000}
    approved = candidate(service, context, args)
    assert service.remember(approved, choice)
    if field == "environment":
        context = replace(context, execution=canonical({"env": "after restart"}))
    else:
        args[field] = {"reason": "check again", "timeout": 90, "max_tokens": 1000}[field]
    if choice.startswith("PREFIX"):
        args["command"] += " -- --runInBand"
    if choice.endswith("PROJECT"):
        context = replace(context, session_id="next-session")
    reopened = DAAService(DAAStore(service.store.path))
    assert reopened.match(candidate(reopened, context, args)) == "HIT_ALLOW"
    for field in ("reason", "timeout", "max_tokens"):
        args.pop(field, None)
    assert reopened.match(candidate(reopened, context, args)) == "HIT_ALLOW"


@pytest.mark.parametrize("choice", ["EXACT_PROJECT", "PREFIX_SESSION", "PREFIX_PROJECT"])
@pytest.mark.parametrize("change", ["command", "cwd", "shell", "unknown_option"])
def test_command_reuse_still_binds_execution_inputs(service, context, choice, change):
    args = {"command": "npm run test"}
    approved = candidate(service, context, args)
    assert service.remember(approved, choice)
    shell = "bash"
    if change == "command":
        args["command"] = "npm run build"
    elif change == "cwd":
        context = replace(context, project_id="/other")
    elif change == "shell":
        shell = "zsh"
    else:
        args["future_execution_option"] = "changed"
    assert service.match(candidate(service, context, args, tool=shell)) == "MISS"


def test_read_exact_retains_full_request_identity(service, context):
    args = {"path": "a.txt", "reason": "first"}
    approved = candidate(service, context, args, tool="read_file")
    assert service.remember(approved, "EXACT_PROJECT")
    assert service.match(candidate(service, context, args, tool="read_file")) == "HIT_ALLOW"
    assert service.match(candidate(service, context, {**args, "reason": "changed"}, tool="read_file")) == "MISS"
    assert service.match(candidate(service, replace(context, execution="changed"), args, tool="read_file")) == "MISS"


@pytest.mark.parametrize("choice", ["EXACT_PROJECT", "PREFIX_SESSION", "PREFIX_PROJECT"])
def test_legacy_command_hashes_require_fresh_human_grants(service, context, choice):
    args = {"command": "npm run test", "reason": "first"}
    legacy_display = canonical(
        {"tool": "bash", "arguments": args, "execution": context.execution, "project": context.project_id}
    )
    legacy = Candidate(
        context, legacy_display, shell="bash", legacy_display=legacy_display, prefix=("npm", "run", "test")
    )
    assert service.remember(legacy, choice)
    current = candidate(service, context, args)
    assert service.match(current) == "MISS"
    assert service.remember(current, choice)
    assert service.match(current) == "HIT_ALLOW"
