# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Grant boundaries, protected persistence and the explicitly deferred prefix risk."""

from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from chrys.foundation.platform.files import atomic_write_owner_only_text
from chrys.foundation.util.lock import FileLock
from chrys.service.approval.command_identity import normalize_simple_command
from chrys.service.approval.grant_store import ApprovalGrantStore
from chrys.service.approval.reuse import (
    ApprovalReuseService,
    CommandCandidate,
    CommandKey,
    FileCandidate,
    FileKey,
    ReuseContext,
)
from tests.support.symlinks import symlink_or_skip


@pytest.fixture
def service(tmp_path):
    return ApprovalReuseService(
        ApprovalGrantStore(tmp_path / "project.json"), ApprovalGrantStore(tmp_path / "session.json")
    )


def command(command=("git", "push", "origin", "main"), **changes):
    key = CommandKey(
        cwd="/project", shell="bash", executable="/bin/bash", shell_args=("-c",), command=command, options="{}"
    )
    key = key.model_copy(update=changes)
    return CommandCandidate(ReuseContext("session-a", key.cwd), key, command if isinstance(command, tuple) else None)


def files(*paths):
    return FileCandidate(ReuseContext("session-a", "/project"), frozenset(FileKey(path=path) for path in paths))


@pytest.mark.parametrize("choice", ["EXACT_SESSION", "EXACT_WORKSPACE", "PREFIX_SESSION", "PREFIX_WORKSPACE"])
@pytest.mark.parametrize(
    "change,value",
    [
        ("cwd", "/other"),
        ("shell", "zsh"),
        ("executable", "/other/bash"),
        ("shell_args", ("-l", "-c")),
        ("options", '{"future_option":true}'),
    ],
)
def test_command_grants_bind_execution_fields(service, choice, change, value):
    original = command()
    assert service.remember(original, choice)
    assert service.match(original)
    assert not service.match(command(**{change: value}))


@pytest.mark.parametrize("choice", ["EXACT_SESSION", "EXACT_WORKSPACE"])
def test_exact_command_arguments_cannot_expand(service, choice):
    assert service.remember(command(), choice)
    assert not service.match(command(("git", "push", "origin", "main", "--force")))
    assert not service.match(command(("git", "push", "main", "origin")))


def test_prefix_risk_remains_explicitly_deferred(service):
    # This is a known limitation, not a claim that appended flags are safe.
    assert service.remember(command(), "PREFIX_WORKSPACE")
    assert service.match(command(("git", "push", "origin", "main", "--force")))


@pytest.mark.parametrize("scope", ["SESSION", "WORKSPACE"])
def test_files_are_individual_scoped_grants(service, scope):
    original = files("/a", "/b")
    assert service.remember(original, f"EXACT_{scope}")
    assert len(service.match(original)) == 2
    assert len(service.match(files("/a"))) == 1
    assert not service.match(files("/a", "/c"))
    assert not service.match(files())
    assert not service.remember(files(), f"EXACT_{scope}")
    other = replace(original, context=ReuseContext("session-b", "/project"))
    assert bool(service.match(other)) == (scope == "WORKSPACE")
    assert not service.match(replace(original, context=ReuseContext("session-a", "/other")))


def test_files_need_complete_coverage_in_one_scope(service):
    assert service.remember(files("/a"), "EXACT_SESSION")
    assert service.remember(files("/b"), "EXACT_WORKSPACE")
    assert not service.match(files("/a", "/b"))


@pytest.mark.parametrize("shell", ["bash", "cmd", "powershell", "pwsh"])
@pytest.mark.parametrize(
    "text",
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
def test_complex_commands_are_remembered_only_exactly(service, shell, text):
    assert normalize_simple_command(text, shell) is None
    assert not service.remember(command(text, shell=shell), "PREFIX_SESSION")
    assert service.remember(command(text, shell=shell), "EXACT_SESSION")
    assert service.match(command(text, shell=shell))


@pytest.mark.parametrize("text", ["rm =deploy", "rm ''=deploy", 'rm ""=deploy', "rm '=deploy'"])
def test_zsh_equals_words_keep_their_raw_text(text):
    # zsh turns =deploy into the path of the deploy command, also after empty quotes.
    assert normalize_simple_command(text, "zsh") is None


@pytest.mark.parametrize("shell", ["bash", "sh", "zsh", "git_bash", "cmd", "powershell", "pwsh"])
def test_simple_normalization_preserves_all_ordered_tokens(shell):
    assert normalize_simple_command("git   push\torigin main", shell) == ("git", "push", "origin", "main")


def test_revocation_and_clear_are_visible_without_rebuild(service):
    candidate = command()
    assert service.remember(candidate, "EXACT_WORKSPACE")
    other = ApprovalReuseService(service.store, service.session_store)
    (grant_id,) = other.match(candidate)
    assert service.store.revoke(grant_id)
    assert not other.match(candidate)
    assert not service.store.revoke("unknown")
    assert service.remember(candidate, "EXACT_SESSION")
    assert service.session_store.clear()
    assert not other.match(candidate)


@pytest.mark.parametrize(
    "mutation", ["missing_key", "missing_shell", "extra", "scope", "source", "wrong_type", "empty_prefix"]
)
def test_invalid_records_never_allow(service, mutation):
    candidate = command()
    assert service.remember(candidate, "EXACT_WORKSPACE")
    rule = service.store.load()[0]
    if mutation == "missing_key":
        del rule["key"]
    elif mutation == "missing_shell":
        del rule["key"]["shell"]
    elif mutation == "extra":
        rule["future"] = True
    elif mutation in {"scope", "source"}:
        rule[mutation] = "UNKNOWN"
    elif mutation == "wrong_type":
        rule["scope_id"] = 12
    else:
        rule["prefix"] = True
        rule["key"]["command"] = []
    atomic_write_owner_only_text(service.store.path, json.dumps({"version": 1, "rules": [rule]}))
    assert not service.match(candidate)


@pytest.mark.parametrize("data", ["{", '{"version":99,"rules":[]}', '{"version":1,"rules":null}'])
def test_bad_store_does_not_allow_or_get_overwritten(service, data):
    atomic_write_owner_only_text(service.store.path, data)
    assert not service.match(command())
    assert not service.remember(command(), "EXACT_WORKSPACE")
    assert service.store.path.read_text(encoding="utf-8") == data


def test_store_refuses_symlink_and_writes_owner_only(service, tmp_path):
    candidate = command()
    assert service.remember(candidate, "EXACT_WORKSPACE")
    if os.name != "nt":
        assert service.store.path.stat().st_mode & 0o777 == 0o600
    target = tmp_path / "other.json"
    service.store.path.rename(target)
    symlink_or_skip(service.store.path, target)
    assert not service.match(candidate)
    assert not service.remember(candidate, "EXACT_WORKSPACE")
    assert not service.store.clear()
    assert target.read_text(encoding="utf-8")


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits")
def test_world_readable_grants_are_not_trusted(service):
    assert service.remember(command(), "EXACT_WORKSPACE")
    service.store.path.chmod(0o644)
    assert not service.match(command())


def test_two_writers_preserve_grants(service):
    other = ApprovalReuseService(service.store, service.session_store)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(service.remember, command(), "EXACT_WORKSPACE")
        second = pool.submit(other.remember, command(("npm", "test")), "EXACT_WORKSPACE")
        assert first.result() and second.result()
    assert service.match(command())
    assert service.match(command(("npm", "test")))


def test_atomic_write_failure_preserves_previous_grants(service, monkeypatch):
    assert service.remember(files("/old"), "EXACT_WORKSPACE")
    import chrys.service.approval.grant_store as store_module

    def fail(path, payload):
        raise OSError("disk full")

    monkeypatch.setattr(store_module, "atomic_write_owner_only_bytes", fail)
    assert not service.remember(files("/a", "/b"), "EXACT_WORKSPACE")
    assert service.match(files("/old"))
    assert not service.match(files("/a"))


def test_store_bound_and_duplicate_replacement(service, monkeypatch):
    import chrys.service.approval.grant_store as store_module

    monkeypatch.setattr(store_module, "MAX_GRANTS", 1)
    assert service.remember(command(), "EXACT_WORKSPACE")
    assert service.remember(command(), "EXACT_WORKSPACE")
    assert len(service.rules()) == 1
    assert not service.remember(command(("npm", "test")), "EXACT_WORKSPACE")
    assert service.match(command())


def test_session_grants_in_different_workdirs_are_kept_apart(service):
    def in_workdir(workdir):
        return FileCandidate(ReuseContext("session-a", workdir), frozenset({FileKey(path="/shared/file")}))

    assert service.remember(in_workdir("/a"), "EXACT_SESSION")
    assert service.remember(in_workdir("/b"), "EXACT_SESSION")
    assert service.match(in_workdir("/a"))
    assert service.match(in_workdir("/b"))
    assert service.remember(in_workdir("/a"), "EXACT_SESSION")
    assert sum(rule.scope == "SESSION" for rule in service.rules()) == 2


@pytest.mark.parametrize("blocked", ["read-only", "lock", "re-read"])
def test_grant_migration_uses_the_store_write_lock_and_respects_permissions(service, tmp_path, monkeypatch, blocked):
    store = service.store
    assert service.remember(files("/existing"), "EXACT_WORKSPACE")
    current = store.load()[0]
    current["scope_id"] = str(tmp_path.resolve())
    legacy = {**current, "scope": "PROJECT", "project": current["scope_id"]}
    old = json.dumps({"version": 1, "rules": [legacy]})
    atomic_write_owner_only_text(store.path, old)

    def unexpected(*_args):
        pytest.fail("This grant file should be left in place")

    if blocked == "read-only":
        store.path.chmod(0o400)
        try:
            assert store.migrate(backup=unexpected, on_read_error=unexpected) == (False, 0)
        finally:
            store.path.chmod(0o600)
        expected = old
    else:
        expected = old
        acquire = FileLock.acquire

        def acquire_after_other_writer(lock):
            if lock._path == store.lock_path:
                if blocked == "lock":
                    raise TimeoutError("busy")
                atomic_write_owner_only_text(store.path, expected)
            acquire(lock)

        if blocked == "re-read":
            expected = json.dumps({"version": 1, "rules": [current]})
        monkeypatch.setattr(FileLock, "acquire", acquire_after_other_writer)
        assert store.migrate(backup=unexpected, on_read_error=unexpected) == (False, 0)
    assert store.path.read_text(encoding="utf-8") == expected


def test_workspace_file_grant_follows_id_across_primary_edits_and_stops_after_leaving(service):
    workspace_id = "ws_" + "a" * 32
    original = files("/shared/file")
    candidate = replace(original, context=replace(original.context, workspace_id=workspace_id))
    assert service.remember(candidate, "EXACT_WORKSPACE")
    [grant] = service.rules()
    assert (grant.scope, grant.scope_id, grant.workdir) == ("WORKSPACE", workspace_id, None)
    assert service.match(candidate)
    assert service.match(replace(candidate, context=ReuseContext("session-b", "/different-primary", workspace_id)))
    assert not service.match(replace(candidate, context=ReuseContext("session-b", "/project", "ws_" + "b" * 32)))
    assert not service.match(original)
