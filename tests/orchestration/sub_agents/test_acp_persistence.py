# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Secure ACP audit envelope, key, reference, and fork tests."""

from __future__ import annotations

import asyncio
import errno
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from chrys.foundation.platform import key_files
from chrys.foundation.platform.files import SecureFileError, atomic_write_owner_only_text
from chrys.service.session import sub_agent_logs
from chrys.service.session.message_metadata import TOOL_RESULT_METADATA_KEY
from chrys.service.session.sub_agent_logs import (
    SubAgentSessionArtifactService,
    SubAgentSessionLogWriter,
    load_or_create_acp_audit_key,
    normalize_acp_state,
    validate_acp_state_file_references,
)
from chrys.service.state.store import JsonFileStateStore
from tests.support.platform_fakes import platform_with_config_dir
from tests.support.symlinks import symlink_or_skip


@pytest.mark.asyncio
async def test_acp_audit_envelope_round_trip_redacts_values_and_uses_owner_only_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_dir = tmp_path / "config"
    monkeypatch.setattr(
        sub_agent_logs,
        "get_platform",
        lambda: platform_with_config_dir(config_dir),
    )
    session_dir = tmp_path / "session"
    writer = SubAgentSessionLogWriter(
        parent_session_dir=session_dir,
        parent_session_id="parent",
        invocation_id="a1b2c3d4e5f6",
        tool_name="external",
        agent_profile="external",
        agent_display_name="External",
        prompt="work",
        parent_provider_call_id="provider",
        parent_event_call_id="event",
        runner="acp",
    )
    writer.set_acp_launch("/bin/agent", ["--token=secret", "--flag", "positional", "-Iprivate", "-", "--"])
    await writer.write(
        status="completed",
        ended=True,
        acp_state={
            "agent_info": {"name": "stub"},
            "attempts": [{"attempt": 1, "acp_session_id": "remote"}],
            "stop_reason": "end_turn",
            "translated_updates": [{"seq": 1, "text": "ok"}],
        },
    )
    path = writer.path
    assert path is not None
    envelope = json.loads(path.read_text(encoding="utf-8"))
    assert envelope["meta"]["runner"] == "acp"
    assert "state" not in envelope
    assert envelope["acp_state"]["attempts"][0]["acp_session_id"] == "remote"
    launch = envelope["acp_state"]["launch"]
    assert launch["command"] == "/bin/agent"
    assert launch["arg_count"] == 6
    assert launch["args"] == [
        "--token=<redacted>",
        "--flag",
        "<redacted>",
        "-I<redacted>",
        "<redacted>",
        "--",
    ]
    assert "secret" not in json.dumps(envelope)
    assert len(launch["argv_hmac_sha256"]) == 64
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600
        assert (config_dir / "acp-audit-hmac.key").stat().st_mode & 0o777 == 0o600


@pytest.mark.asyncio
async def test_acp_audit_envelope_survives_surrogateescaped_launch_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A surrogateescaped POSIX launch path (legal str, not strict-UTF-8) must be
    auditable, not crash the tool call. Env-template expansion of an env var that
    holds an undecodable filesystem byte yields such a value; the audit must both
    write and strict-UTF-8 round-trip it."""
    config_dir = tmp_path / "config"
    monkeypatch.setattr(
        sub_agent_logs,
        "get_platform",
        lambda: platform_with_config_dir(config_dir),
    )
    session_dir = tmp_path / "session"
    # A lone surrogate models a filesystem byte os.fsdecode surrogateescaped.
    surrogate_command = "/opt/ag\udcffent"
    writer = SubAgentSessionLogWriter(
        parent_session_dir=session_dir,
        parent_session_id="parent",
        invocation_id="a1b2c3d4e5f6",
        tool_name="external",
        agent_profile="external",
        agent_display_name="External",
        prompt="work",
        parent_provider_call_id="provider",
        parent_event_call_id="event",
        runner="acp",
    )
    writer.set_acp_launch(surrogate_command, ["--flag", "/data/f\udcfeile"])
    written = await writer.write(
        status="running",
        acp_state={"agent_info": None, "attempts": [], "stop_reason": "", "translated_updates": []},
    )
    assert written
    path = writer.path
    assert path is not None
    # Strict-UTF-8 read-back must succeed (reconcile/fork read the audit this way).
    envelope = json.loads(path.read_text(encoding="utf-8"))
    launch = envelope["acp_state"]["launch"]
    # The stored command is surrogate-free yet preserves the escaped byte, and the
    # keyed digest still binds the exact original argv.
    assert "\udcff" not in launch["command"]
    assert "udcff" in launch["command"]
    assert len(launch["argv_hmac_sha256"]) == 64


@pytest.mark.asyncio
async def test_acp_audit_envelope_survives_surrogateescaped_workspace_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A surrogateescaped workspace cwd/root (legal POSIX str, not strict-UTF-8)
    must not crash the audit write. r17 sanitized only the launch argv; the meta
    workspace paths (primary_cwd/working_dirs) flow in raw from tools.py and are
    covered by the whole-envelope boundary sanitize in ``_atomic_write_json``.
    Without it the strict file encode raises and, for an ACP writer, re-raises —
    aborting the invocation before launch."""
    config_dir = tmp_path / "config"
    monkeypatch.setattr(
        sub_agent_logs,
        "get_platform",
        lambda: platform_with_config_dir(config_dir),
    )
    session_dir = tmp_path / "session"
    # Lone surrogates model filesystem bytes os.fsdecode surrogateescaped.
    surrogate_cwd = "/work/pro\udcffject"
    surrogate_root = "/work/ro\udcfeot"
    writer = SubAgentSessionLogWriter(
        parent_session_dir=session_dir,
        parent_session_id="parent",
        invocation_id="a1b2c3d4e5f6",
        tool_name="external",
        agent_profile="external",
        agent_display_name="External",
        prompt="work",
        parent_provider_call_id="provider",
        parent_event_call_id="event",
        runner="acp",
        primary_cwd=surrogate_cwd,
        working_dirs=[surrogate_cwd, surrogate_root],
    )
    writer.set_acp_launch("/bin/agent", ["--flag"])
    # For an ACP writer a failed write RE-RAISES, so a surviving True return here
    # already proves the surrogate meta path did not crash the strict file write.
    written = await writer.write(
        status="running",
        acp_state={"agent_info": None, "attempts": [], "stop_reason": "", "translated_updates": []},
    )
    assert written
    path = writer.path
    assert path is not None
    # The file is strict-UTF-8 — no lone surrogate leaked into the bytes (a
    # reconcile/fork read decodes it exactly this way).
    raw = path.read_bytes().decode("utf-8")
    assert not any(0xD800 <= ord(ch) <= 0xDFFF for ch in raw)
    envelope = json.loads(raw)
    assert envelope["meta"]["runner"] == "acp"
    assert "pro" in envelope["meta"]["primary_cwd"]
    assert len(envelope["meta"]["working_dirs"]) == 2


def test_fork_survives_surrogate_bearing_acp_audit(tmp_path: Path) -> None:
    """A same-uid child can plant a valid-JSON ACP audit whose value decodes to a
    lone surrogate (a surrogateescaped path). The fork re-serializes with
    ``ensure_ascii=False``; without the serialization-boundary sanitize the strict
    write raised ``UnicodeEncodeError`` OUTSIDE the NaN guard, aborting EVERY fork
    with ``SessionForkError``."""
    sessions = tmp_path / "sub_agents" / "sessions"
    sessions.mkdir(parents=True)
    # ensure_ascii=True stores the lone surrogate as its ASCII \udcXX escape on
    # disk; json.loads decodes it back to the surrogate when the fork reads it.
    planted = sessions / "acp.json"
    atomic_write_owner_only_text(
        planted,
        json.dumps(
            {
                "meta": {"runner": "acp", "parent_session_id": "old", "last_error": "byte /x/\udcff here"},
                "acp_state": {"attempts": [], "translated_updates": []},
            }
        ),
    )
    good = sessions / "good.json"
    atomic_write_owner_only_text(
        good,
        json.dumps({"meta": {"runner": "acp", "parent_session_id": "old"}, "acp_state": {"attempts": []}}),
    )

    # Must not raise (pre-fix: UnicodeEncodeError escapes the ValueError guard).
    JsonFileStateStore._rewrite_fork_sub_agent_logs(
        tmp_path,
        new_session_id="fork-session",
        parent_clipboard_root=tmp_path / "parent",
        fork_clipboard_root=tmp_path / "fork",
    )

    # Both audits were rewritten with the new parent id — the fork completed...
    planted_bytes = planted.read_bytes().decode("utf-8")
    assert not any(0xD800 <= ord(ch) <= 0xDFFF for ch in planted_bytes)
    assert json.loads(planted_bytes)["meta"]["parent_session_id"] == "fork-session"
    assert json.loads(good.read_text(encoding="utf-8"))["meta"]["parent_session_id"] == "fork-session"


def test_redact_acp_argv_redacts_every_value_after_end_of_options_marker() -> None:
    """Doc §10: every attached AND positional value is replaced.

    After ``--`` all arguments are positional even when they start with ``-``,
    so a secret can no longer masquerade as a flag and survive redaction.
    """
    from chrys.service.session.sub_agent_logs import redact_acp_argv

    assert redact_acp_argv(["--flag", "--", "-----BEGIN PRIVATE KEY-----", "-v", "--still-secret", "plain"]) == [
        "--flag",
        "--",
        "<redacted>",
        "<redacted>",
        "<redacted>",
        "<redacted>",
    ]


def test_owner_verified_bounded_read_rejects_oversized_artifact(tmp_path: Path) -> None:
    from chrys.service.session.sub_agent_logs import read_owner_verified_bounded

    path = tmp_path / "record.json"
    atomic_write_owner_only_text(path, "x" * 4096)
    assert read_owner_verified_bounded(path, max_bytes=8192) == b"x" * 4096
    with pytest.raises(ValueError, match="read ceiling"):
        read_owner_verified_bounded(path, max_bytes=1024)


def test_drain_quarantines_oversized_pending_record(tmp_path: Path) -> None:
    """A same-uid untrusted process cannot OOM restore with a huge record."""
    from chrys.service.session.sub_agent_logs import _MAX_PENDING_RECORD_BYTES

    session = tmp_path / "session"
    pending = session / "sub_agents" / "pending" / "huge.json"
    good = session / "sub_agents" / "pending" / "good.json"
    atomic_write_owner_only_text(pending, "[" + "0," * (_MAX_PENDING_RECORD_BYTES // 2) + "0]")
    atomic_write_owner_only_text(
        good,
        json.dumps(
            {
                "runner": "acp",
                "invocation_id": "a1b2c3d4e5f6",
                "tool_name": "external",
                "created_at": "2026-01-01T00:00:00+00:00",
            }
        ),
    )
    service = SubAgentSessionArtifactService(session)
    records = service.drain_paused_records()
    assert [record["invocation_id"] for record in records] == ["a1b2c3d4e5f6"]
    # The over-cap record is dropped outright, never copied forward into
    # quarantine (copying it would reintroduce the unbounded read).
    assert not pending.exists()
    assert not (session / "sub_agents" / "pending" / "corrupt" / "huge.json").exists()


def test_make_fork_copy_ignore_excludes_only_root_sub_agents(tmp_path: Path) -> None:
    """Only the SESSION-ROOT sub_agents subtree is excluded; case variants too."""
    ignore = JsonFileStateStore._make_fork_copy_ignore(tmp_path)
    # Root: exact-case AND case-variant (NTFS-planted SUB_AGENTS junction) excluded.
    assert ignore(str(tmp_path), ["session.json", "sub_agents"]) == {"sub_agents"}
    assert ignore(str(tmp_path), ["session.json", "SUB_AGENTS"]) == {"SUB_AGENTS"}
    # A NON-root directory that merely contains a child named sub_agents is kept
    # (kernel compaction spill lives under compactions/sub_agents/...).
    assert ignore(str(tmp_path / "compactions"), ["sub_agents", "dropped"]) == set()
    # The excluded dir's own children are never touched.
    assert ignore(str(tmp_path / "sub_agents"), ["sessions", "pending"]) == set()


def test_fork_copy_preserves_nested_compactions_sub_agents(tmp_path: Path) -> None:
    """A real copytree with the fork ignore drops root sub_agents but keeps spill."""
    import shutil

    src = tmp_path / "src"
    (src / "sub_agents" / "sessions").mkdir(parents=True)
    (src / "sub_agents" / "sessions" / "a.json").write_text("{}")
    spill = src / "compactions" / "sub_agents" / "external" / "inv" / "dropped" / "turn001"
    spill.mkdir(parents=True)
    (spill / "rec.md").write_text("kept")

    dst = tmp_path / "dst"
    shutil.copytree(src, dst, symlinks=True, ignore=JsonFileStateStore._make_fork_copy_ignore(src))

    # Root sub_agents subtree excluded (rebuilt separately by the secure copier).
    assert not (dst / "sub_agents").exists()
    # Nested compaction spill preserved verbatim — otherwise the fork keeps
    # audit/catalog references to dropped-turn records that no longer exist.
    assert (dst / "compactions" / "sub_agents" / "external" / "inv" / "dropped" / "turn001" / "rec.md").read_text() == (
        "kept"
    )


def test_reconcile_drops_over_sized_audit_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An over-cap audit envelope is dropped, not re-scanned on every restore."""
    monkeypatch.setattr(sub_agent_logs, "MAX_SUB_AGENT_AUDIT_BYTES", 64)
    service = SubAgentSessionArtifactService(tmp_path)
    sessions = sub_agent_logs.sessions_dir(tmp_path)
    sessions.mkdir(parents=True)
    huge = sessions / "huge.json"
    atomic_write_owner_only_text(huge, "x" * 200)  # over the (patched) 64-byte ceiling

    # Neither reconcile nor the invocation-map scan raises; the file is dropped.
    assert service.reconcile_orphaned_running_logs([], []) == 0
    assert not huge.exists()


def test_rewrite_fork_logs_skips_non_encodable_artifact(tmp_path: Path) -> None:
    """A legacy NaN artifact must skip, not abort the entire session fork."""
    sessions = tmp_path / "sub_agents" / "sessions"
    sessions.mkdir(parents=True)
    # json.loads accepts NaN (legacy allow_nan=True writers emitted it); the
    # strict re-encode rejects it. This one file must not fail the whole fork.
    bad = sessions / "bad.json"
    atomic_write_owner_only_text(bad, '{"meta": {"runner": "kernel"}, "state": {"x": NaN}}')
    good = sessions / "good.json"
    atomic_write_owner_only_text(good, json.dumps({"meta": {"runner": "kernel"}, "state": {}}))

    JsonFileStateStore._rewrite_fork_sub_agent_logs(
        tmp_path,
        new_session_id="fork-session",
        parent_clipboard_root=tmp_path / "parent",
        fork_clipboard_root=tmp_path / "fork",
    )

    # The good artifact was still rewritten with the new parent id...
    assert json.loads(good.read_text())["meta"]["parent_session_id"] == "fork-session"
    # ...and the non-encodable one was left in place (fork not aborted).
    assert bad.exists()


def test_make_fork_copy_ignore_excludes_junctions_at_every_level(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A planted junction is dropped at ANY depth, not just the session root.

    copytree(symlinks=True) FOLLOWS NT junctions (os.path.islink is False for
    them), so a junction nested under compactions/sub_agents/... would be
    materialized through plain CopyFile2 — or recurse to exhaustion if it points
    at an ancestor — unless the ignore callback drops it everywhere. Junctions
    do not exist on POSIX, so simulate the reparse point via os.path.isjunction.
    """
    nested = tmp_path / "compactions" / "sub_agents" / "external" / "inv" / "dropped"
    nested.mkdir(parents=True)
    planted = nested / "evil"
    planted.mkdir()
    (nested / "innocent").mkdir()
    planted_key = os.path.normcase(os.path.abspath(planted))
    real_isjunction = os.path.isjunction

    def fake_isjunction(candidate: object) -> bool:
        return os.path.normcase(os.path.abspath(candidate)) == planted_key or real_isjunction(candidate)

    monkeypatch.setattr(os.path, "isjunction", fake_isjunction)
    ignore = JsonFileStateStore._make_fork_copy_ignore(tmp_path)
    # The nested junction is excluded even though its name is not "sub_agents"
    # and its parent is not the session root; the sibling real dir is kept.
    assert ignore(str(nested), ["evil", "innocent"]) == {"evil"}
    # A nested real dir literally named sub_agents still copies through.
    assert ignore(str(tmp_path / "compactions"), ["sub_agents"]) == set()


def test_read_audit_envelope_preserves_deeply_nested_json(tmp_path: Path) -> None:
    """A byte-bounded but deeply nested audit blows json's stack; skip, never abort/drop."""
    service = SubAgentSessionArtifactService(tmp_path)
    sessions = sub_agent_logs.sessions_dir(tmp_path)
    sessions.mkdir(parents=True)
    deep = sessions / "deep.json"
    atomic_write_owner_only_text(deep, "[" * 100_000 + "]" * 100_000)  # ~200 KB, far under the cap
    # json.loads raises RecursionError (NOT a ValueError) here; it must be caught
    # as malformed, return None, and PRESERVE the file (not over-cap → never dropped).
    assert service._read_audit_envelope_or_drop(deep) is None
    assert deep.exists()
    # End-to-end: the reconcile scan likewise never aborts session hydration.
    assert service.reconcile_orphaned_running_logs([], []) == 0
    assert deep.exists()


def test_read_audit_envelope_preserves_oversized_integer_json(tmp_path: Path) -> None:
    """A valid JSON integer over Python's digit limit raises a PLAIN ValueError.

    That ValueError must be classified as malformed (preserve), not confused with
    read_owner_verified_bounded's over-cap ValueError (which drops the file).
    """
    service = SubAgentSessionArtifactService(tmp_path)
    sessions = sub_agent_logs.sessions_dir(tmp_path)
    sessions.mkdir(parents=True)
    bigint = sessions / "bigint.json"
    atomic_write_owner_only_text(bigint, "1" * 5000)  # ~5 KB, far under the audit cap
    assert service._read_audit_envelope_or_drop(bigint) is None
    # Regression: previously the plain ValueError hit the over-cap arm and unlinked it.
    assert bigint.exists()


def test_drain_quarantines_deeply_nested_pending_record(tmp_path: Path) -> None:
    """A deeply nested pending record must not RecursionError out of drain."""
    service = SubAgentSessionArtifactService(tmp_path)
    pending = sub_agent_logs.pending_dir(tmp_path)
    pending.mkdir(parents=True)
    deep = pending / "deep.json"
    atomic_write_owner_only_text(deep, "[" * 100_000 + "]" * 100_000)
    # No RecursionError escapes; the record is quarantined and drain returns cleanly.
    assert service.drain_paused_records() == []
    assert not deep.exists()  # moved into the pending/corrupt quarantine
    assert (pending / "corrupt" / "deep.json").exists()


@pytest.mark.parametrize("publication", ["native", "no_hard_links", "directory"])
def test_acp_hmac_key_creation_is_locked_and_stable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, publication: str
) -> None:
    if publication == "no_hard_links":
        monkeypatch.setattr(os, "link", Mock(side_effect=OSError(errno.ENOTSUP, "Hard links unavailable")))
    elif publication == "directory":
        monkeypatch.setattr(
            key_files, "publish_file_no_replace", Mock(side_effect=OSError(errno.ENOTSUP, "No atomic file publish"))
        )

    async def create_many() -> list[bytes]:
        return await asyncio.gather(
            *(asyncio.to_thread(load_or_create_acp_audit_key, config_dir=tmp_path) for _ in range(8))
        )

    keys = asyncio.run(create_many())
    assert len(set(keys)) == 1
    assert len(keys[0]) == 32
    key_path = tmp_path / "acp-audit-hmac.key"
    assert key_path.is_dir() is (publication == "directory")
    assert key_files.read_owner_only_key(key_path, max_bytes=33) == keys[0]
    assert load_or_create_acp_audit_key(config_dir=tmp_path) == keys[0]
    assert list(tmp_path.glob("*.tmp")) == []


def test_acp_hmac_key_rejects_symlink(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.write_bytes(b"x" * 32)
    key_path = tmp_path / "acp-audit-hmac.key"
    symlink_or_skip(key_path, target)
    with pytest.raises(SecureFileError):
        load_or_create_acp_audit_key(config_dir=tmp_path)


def test_acp_hmac_key_rejects_insecure_mode(tmp_path: Path) -> None:
    key_path = tmp_path / "acp-audit-hmac.key"
    key_path.write_bytes(b"x" * 32)
    if os.name != "nt":
        key_path.chmod(0o644)
        with pytest.raises(SecureFileError):
            load_or_create_acp_audit_key(config_dir=tmp_path)


def test_acp_hmac_key_rejects_crash_debris_at_final_key_path(tmp_path: Path) -> None:
    key_path = tmp_path / "acp-audit-hmac.key"
    atomic_write_owner_only_text(key_path, "partial")
    with pytest.raises(SecureFileError, match="invalid length"):
        load_or_create_acp_audit_key(config_dir=tmp_path)
    assert key_path.read_text() == "partial"


@pytest.mark.parametrize("reference", ["/absolute/log", "../escape", "nested/../../escape"])
def test_acp_state_rejects_unsafe_file_references(tmp_path: Path, reference: str) -> None:
    with pytest.raises(ValueError):
        validate_acp_state_file_references(
            {"stderr_log_path": reference},
            parent_session_dir=tmp_path,
        )


def test_acp_state_rejects_symlink_escape(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    session = tmp_path / "session"
    session.mkdir()
    symlink_or_skip(session / "linked", outside, target_is_directory=True)
    with pytest.raises(ValueError):
        validate_acp_state_file_references(
            {"stderr_log_path": "linked/stderr.log"},
            parent_session_dir=session,
        )


def test_acp_state_bounds_update_and_attempt_rings(tmp_path: Path) -> None:
    session = tmp_path / "session"
    session.mkdir()
    normalized = normalize_acp_state(
        {"translated_updates": [{"seq": index, "text": "x" * 1_000} for index in range(600)]},
        parent_session_dir=session,
    )
    assert len(normalized["translated_updates"]) <= 500
    assert (
        len(
            json.dumps(
                normalized["translated_updates"],
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode()
        )
        <= 256 * 1024
    )

    # Manual retries are unlimited: the attempt history must ride a
    # newest-wins ring like the update trail, not grow without bound.
    bounded = normalize_acp_state(
        {"attempts": [{"attempt": index, "acp_session_id": f"s{index}"} for index in range(60)]},
        parent_session_dir=session,
    )
    assert bounded["attempts"] == [{"attempt": index, "acp_session_id": f"s{index}"} for index in range(10, 60)]


def test_fork_rewrites_runner_matrix_and_securely_recreates_artifacts(tmp_path: Path) -> None:
    directory = tmp_path / "session" / "sub_agents" / "sessions"
    directory.mkdir(parents=True)
    legacy = {"meta": {"parent_session_id": "old"}, "state": {"messages": []}}
    acp = {
        "meta": {"runner": "acp", "parent_session_id": "old"},
        "acp_state": {"attempts": [], "translated_updates": []},
    }
    unknown = {"meta": {"runner": "future", "parent_session_id": "old"}, "opaque": {"x": 1}}
    for name, envelope in (("legacy.json", legacy), ("acp.json", acp), ("unknown.json", unknown)):
        atomic_write_owner_only_text(directory / name, json.dumps(envelope))

    JsonFileStateStore._rewrite_fork_sub_agent_logs(
        tmp_path / "session",
        new_session_id="new",
        parent_clipboard_root=tmp_path / "old-clipboard",
        fork_clipboard_root=tmp_path / "new-clipboard",
    )
    rewritten_legacy = json.loads((directory / "legacy.json").read_text())
    rewritten_acp = json.loads((directory / "acp.json").read_text())
    copied_unknown = json.loads((directory / "unknown.json").read_text())
    assert rewritten_legacy["meta"]["parent_session_id"] == "new"
    assert rewritten_acp["meta"]["parent_session_id"] == "new"
    assert copied_unknown == unknown
    if os.name != "nt":
        assert all(path.stat().st_mode & 0o777 == 0o600 for path in directory.glob("*.json"))


def test_fork_omits_known_acp_envelope_with_unsafe_file_reference(tmp_path: Path) -> None:
    directory = tmp_path / "session" / "sub_agents" / "sessions"
    directory.mkdir(parents=True)
    path = directory / "unsafe.json"
    atomic_write_owner_only_text(
        path,
        json.dumps(
            {
                "meta": {"runner": "acp", "parent_session_id": "old"},
                "acp_state": {"stderr_log_path": "../secret"},
            }
        ),
    )
    JsonFileStateStore._rewrite_fork_sub_agent_logs(
        tmp_path / "session",
        new_session_id="new",
        parent_clipboard_root=tmp_path / "old-clipboard",
        fork_clipboard_root=tmp_path / "new-clipboard",
    )
    assert not path.exists()


def test_fork_secure_recreation_omits_symlinked_source(tmp_path: Path) -> None:
    parent = tmp_path / "parent"
    source_dir = parent / "sub_agents" / "sessions"
    source_dir.mkdir(parents=True)
    outside = tmp_path / "outside.json"
    outside.write_text('{"secret": true}', encoding="utf-8")
    symlink_or_skip(source_dir / "linked.json", outside)
    destination = tmp_path / "destination"
    destination.mkdir()
    JsonFileStateStore._secure_copy_sub_agent_artifacts(parent, destination)
    assert not (destination / "sub_agents" / "sessions" / "linked.json").exists()


def test_fork_secure_recreation_bounds_flooded_source_scan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The parent audit dir is same-uid ACP-writable; a planted flood must not
    be fully materialized by the fork copy. With the scan cap lowered, only a
    bounded subset is recreated — an eager iterdir would copy every file."""
    monkeypatch.setattr("chrys.service.state._fork.MAX_SCANNED_AUDIT_FILES", 3)
    parent = tmp_path / "parent"
    source_dir = parent / "sub_agents" / "sessions"
    source_dir.mkdir(parents=True)
    for index in range(9):
        atomic_write_owner_only_text(source_dir / f"audit{index}.json", "{}")
    destination = tmp_path / "destination"
    destination.mkdir()

    JsonFileStateStore._secure_copy_sub_agent_artifacts(parent, destination)

    copied = list((destination / "sub_agents" / "sessions").glob("*.json"))
    assert 0 < len(copied) <= 3


def test_fork_secure_recreation_copies_history_referenced_audit_beyond_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A referenced audit past the bounded scan cap is still carried into the
    fork (bounded by history, NOT the attacker-writable file count), so a large
    session never forks with a dangling sub_agent_log_file link — and the
    audit's stderr sibling rides along."""
    # Cap the anti-flood scan at zero so ONLY the reference-driven pass can copy.
    monkeypatch.setattr("chrys.service.state._fork.MAX_SCANNED_AUDIT_FILES", 0)
    parent = tmp_path / "parent"
    source_dir = parent / "sub_agents" / "sessions"
    source_dir.mkdir(parents=True)
    referenced = "external_abc123def456.json"
    atomic_write_owner_only_text(source_dir / referenced, json.dumps({"meta": {"status": "completed"}}))
    atomic_write_owner_only_text(source_dir / f"{referenced}.stderr.log", "boom")
    destination = tmp_path / "destination"
    destination.mkdir()
    # The forked history (already placed by copytree) references the audit.
    atomic_write_owner_only_text(
        destination / "session.json",
        json.dumps(
            {
                "state": {
                    "messages": [{"contents": [{"additional_properties": {"m": {"sub_agent_log_file": referenced}}}]}]
                }
            }
        ),
    )

    JsonFileStateStore._secure_copy_sub_agent_artifacts(parent, destination)

    dest_sessions = destination / "sub_agents" / "sessions"
    assert (dest_sessions / referenced).exists()
    assert (dest_sessions / f"{referenced}.stderr.log").exists()


def test_fork_rewrite_covers_referenced_audit_copied_beyond_scan_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An audit the reference-driven pass copied PAST the anti-flood scan cap
    must still be id-rewritten. The rewrite iterates the copier's exact output
    (not a re-scan under the same cap), or the audit keeps a stale
    parent_session_id — the follow-on hazard of the reference-driven copy."""
    monkeypatch.setattr("chrys.service.state._fork.MAX_SCANNED_AUDIT_FILES", 0)
    parent = tmp_path / "parent"
    source_dir = parent / "sub_agents" / "sessions"
    source_dir.mkdir(parents=True)
    referenced = "external_abc123def456.json"
    atomic_write_owner_only_text(
        source_dir / referenced,
        json.dumps({"meta": {"runner": "kernel", "parent_session_id": "PARENT"}, "state": {}}),
    )
    fork = tmp_path / "fork"
    fork.mkdir()
    atomic_write_owner_only_text(
        fork / "session.json",
        json.dumps({"state": {"messages": [{"sub_agent_log_file": referenced}]}}),
    )

    copied = JsonFileStateStore._secure_copy_sub_agent_artifacts(parent, fork)
    assert referenced in copied
    JsonFileStateStore._rewrite_fork_sub_agent_logs(
        fork,
        new_session_id="fork-session",
        parent_clipboard_root=tmp_path / "p",
        fork_clipboard_root=tmp_path / "f",
        artifact_names=copied,
    )

    envelope = json.loads((fork / "sub_agents" / "sessions" / referenced).read_text(encoding="utf-8"))
    assert envelope["meta"]["parent_session_id"] == "fork-session"


def test_collect_sub_agent_log_file_references_walks_nested_structures() -> None:
    from chrys.service.session.sub_agent_logs import collect_sub_agent_log_file_references

    envelope = {
        "state": {
            "messages": [
                {"contents": [{"additional_properties": {"m": {"sub_agent_log_file": "a.json"}}}]},
                {"contents": [{"sub_agent_log_file": "b.json"}]},
            ],
            "compressed_msgs": [{"messages": [{"sub_agent_log_file": "c.json"}]}],
        },
    }
    assert collect_sub_agent_log_file_references(envelope) == {"a.json", "b.json", "c.json"}


def test_is_safe_sub_agent_log_basename_rejects_traversal_and_non_json() -> None:
    from chrys.service.session.sub_agent_logs import is_safe_sub_agent_log_basename

    assert is_safe_sub_agent_log_basename("external_abc.json")
    assert not is_safe_sub_agent_log_basename("../escape.json")
    assert not is_safe_sub_agent_log_basename("sub/dir.json")
    assert not is_safe_sub_agent_log_basename("nested\\dir.json")
    assert not is_safe_sub_agent_log_basename(".hidden.json")
    assert not is_safe_sub_agent_log_basename("notjson.txt")
    assert not is_safe_sub_agent_log_basename("")


def test_make_sub_agent_log_file_name_sanitizes_traversal_invocation_id() -> None:
    """The invocation id is a filename suffix; a traversal payload in it must not
    survive into the basename (it can arrive from an attacker-plantable pause
    record and is later path-joined against the sessions directory)."""
    name = sub_agent_logs.make_sub_agent_log_file_name("external", "/../../../../victim")
    assert "/" not in name
    assert "\\" not in name
    assert name == os.path.basename(name)
    assert sub_agent_logs.is_safe_sub_agent_log_basename(name)
    # A legitimate 12-hex id is untouched (safe_fs_name is a 1:1 substitution),
    # so existing on-disk audits stay reconcilable.
    assert sub_agent_logs.make_sub_agent_log_file_name("external", "abc123def456") == "external_abc123def456.json"


def test_reconcile_ignores_pending_record_with_traversal_invocation_id(tmp_path: Path) -> None:
    """A same-uid child that plants a pause record with a traversal invocation_id
    must not make restore read+overwrite a JSON file OUTSIDE the sessions dir.

    Without the guard, `sessions / make_sub_agent_log_file_name(tool, payload)`
    escapes to the victim, and a "running" audit there is atomically flipped to
    "orphaned" — an arbitrary-overwrite primitive. The recomputed name glues the
    tool stem before the payload (`external_<payload>`), so a real POSIX escape
    needs an existing first component; the same-uid attacker plants that dir too
    (`sessions/external_seg`) to walk `..` out of the session."""
    session = tmp_path / "session"
    sessions = sub_agent_logs.sessions_dir(session)
    sessions.mkdir(parents=True)
    # Planted intermediate dir so `external_seg/../../../../victim.json` resolves
    # (POSIX requires each non-final component to exist).
    (sessions / "external_seg").mkdir()
    # A pre-existing "running" audit-shaped file four levels above `sessions`.
    victim = tmp_path / "victim.json"
    victim_body = {"meta": {"status": "running", "invocation_id": "abc123def456"}}
    atomic_write_owner_only_text(victim, json.dumps(victim_body))
    record = {"invocation_id": "seg/../../../../victim", "tool_name": "external"}
    service = SubAgentSessionArtifactService(session)

    assert service.reconcile_orphaned_running_logs([], [record]) == 0

    # The victim is untouched: still "running", never flipped to "orphaned".
    assert json.loads(victim.read_text(encoding="utf-8")) == victim_body


def test_fork_cleanup_never_follows_planted_sub_agent_layout_links(tmp_path: Path) -> None:
    """A symlinked sub_agents level in the fork copy is removed, not traversed.

    copytree(symlinks=True) preserves a directory symlink the same-uid
    untrusted ACP process may have planted; the fork cleanup must never
    rmtree/unlink through it into data outside the session.
    """
    victim = tmp_path / "victim"
    (victim / "pending").mkdir(parents=True)
    (victim / "pending" / "keep.json").write_text("{}", encoding="utf-8")
    (victim / "pending" / "keep.tmp").write_text("", encoding="utf-8")
    (victim / "legacy.json").write_text("{}", encoding="utf-8")
    (victim / "sessions").mkdir()
    (victim / "sessions" / "spy.json").write_text("{}", encoding="utf-8")
    fork_copy = tmp_path / "fork"
    fork_copy.mkdir()
    symlink_or_skip(fork_copy / "sub_agents", victim, target_is_directory=True)

    JsonFileStateStore._sanitize_fork_sub_agent_layout(fork_copy)
    JsonFileStateStore._prepare_fork_sub_agent_artifacts(fork_copy)

    assert not (fork_copy / "sub_agents").exists()
    assert (victim / "pending" / "keep.json").exists()
    assert (victim / "pending" / "keep.tmp").exists()
    assert (victim / "legacy.json").exists()

    nested = tmp_path / "fork-nested"
    (nested / "sub_agents").mkdir(parents=True)
    symlink_or_skip(nested / "sub_agents" / "pending", victim / "pending", target_is_directory=True)
    JsonFileStateStore._sanitize_fork_sub_agent_layout(nested)
    JsonFileStateStore._prepare_fork_sub_agent_artifacts(nested)
    assert not (nested / "sub_agents" / "pending").exists()
    assert (victim / "pending" / "keep.json").exists()

    linked_source = tmp_path / "parent-linked"
    (linked_source / "sub_agents").parent.mkdir(parents=True, exist_ok=True)
    symlink_or_skip(linked_source / "sub_agents", victim, target_is_directory=True)
    clean_destination = tmp_path / "destination-linked"
    clean_destination.mkdir()
    JsonFileStateStore._secure_copy_sub_agent_artifacts(linked_source, clean_destination)
    assert not (clean_destination / "sub_agents").exists()


def test_session_artifact_repair_uses_record_tool_names_without_profile_registry(tmp_path: Path) -> None:
    session = tmp_path / "session"
    pending = session / "sub_agents" / "pending" / "paused.json"
    audit = session / "sub_agents" / "sessions" / "audit.json"
    atomic_write_owner_only_text(
        pending,
        json.dumps(
            {
                "runner": "acp",
                "invocation_id": "a1b2c3d4e5f6",
                "tool_name": "removed-profile-tool",
                "created_at": "2026-01-01T00:00:00+00:00",
            }
        ),
    )
    atomic_write_owner_only_text(
        audit,
        json.dumps(
            {
                "meta": {
                    "runner": "acp",
                    "status": "running",
                    "invocation_id": "a1b2c3d4e5f6",
                    "tool_name": "removed-profile-tool",
                },
                "acp_state": {"attempts": [], "translated_updates": []},
            }
        ),
    )
    service = SubAgentSessionArtifactService(session)
    records = service.drain_paused_records()
    assert [record["tool_name"] for record in records] == ["removed-profile-tool"]
    assert records[0]["sub_agent_log_file"] == "audit.json"
    # A still-"running" audit beside a pending record means the process died
    # between the pause-record and audit-"paused" writes — repaired here, in
    # the only restore that still sees the record.
    assert service.reconcile_orphaned_running_logs([], records) == 1
    repaired = json.loads(audit.read_text(encoding="utf-8"))
    assert repaired["meta"]["status"] == "orphaned"


def test_reconcile_repairs_pending_matched_running_audit_despite_backlinks(tmp_path: Path) -> None:
    """The injected error result backlinks the log before reconcile runs; the
    pending match must still win or the stale "running" status survives every
    later restore (record deleted, backlink skip permanent)."""
    session = tmp_path / "session"
    audit = session / "sub_agents" / "sessions" / "audit.json"
    atomic_write_owner_only_text(
        session / "sub_agents" / "pending" / "paused.json",
        json.dumps(
            {
                "runner": "acp",
                "invocation_id": "a1b2c3d4e5f6",
                "tool_name": "external",
                "created_at": "2026-01-01T00:00:00+00:00",
            }
        ),
    )
    atomic_write_owner_only_text(
        audit,
        json.dumps(
            {
                "meta": {
                    "runner": "acp",
                    "status": "running",
                    "invocation_id": "a1b2c3d4e5f6",
                    "tool_name": "external",
                },
                "acp_state": {"attempts": [], "translated_updates": []},
            }
        ),
    )
    backlinked_message = SimpleNamespace(
        contents=[
            SimpleNamespace(
                additional_properties={
                    TOOL_RESULT_METADATA_KEY: {
                        "sub_agent_invocation_id": "a1b2c3d4e5f6",
                        "sub_agent_log_file": "audit.json",
                    }
                }
            )
        ]
    )
    service = SubAgentSessionArtifactService(session)
    records = service.drain_paused_records()
    assert service.reconcile_orphaned_running_logs([backlinked_message], records) == 1
    repaired = json.loads(audit.read_text(encoding="utf-8"))
    assert repaired["meta"]["status"] == "orphaned"
    assert repaired["meta"]["failure_reason"] == "process_terminated"
    # A backlinked running audit WITHOUT a pending record keeps its
    # accounted-for exemption.
    rewound = json.loads(audit.read_text(encoding="utf-8"))
    rewound["meta"]["status"] = "running"
    atomic_write_owner_only_text(audit, json.dumps(rewound))
    assert service.reconcile_orphaned_running_logs([backlinked_message], []) == 0


def test_reconcile_repairs_pending_record_audit_beyond_scan_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pending record's still-"running" audit must be flipped to "orphaned"
    THIS restore even when the bounded scan never reaches it (an attacker flood
    or a legitimately huge session pushes it past the cap). Otherwise injection
    deletes the record + backlinks the log and the stale status is exempted from
    repair on every future restore. The deterministic-path targeted pass does
    this independent of the scan; here the scan cap is zero so ONLY it can."""
    monkeypatch.setattr(sub_agent_logs, "MAX_SCANNED_AUDIT_FILES", 0)
    session = tmp_path / "session"
    sessions = sub_agent_logs.sessions_dir(session)
    sessions.mkdir(parents=True)
    invocation_id = "abc123def456"
    tool_name = "external"
    audit_name = sub_agent_logs.make_sub_agent_log_file_name(tool_name, invocation_id)
    atomic_write_owner_only_text(
        sessions / audit_name,
        json.dumps(
            {
                "meta": {"runner": "acp", "status": "running", "invocation_id": invocation_id},
                "acp_state": {"attempts": [], "translated_updates": []},
            }
        ),
    )
    record = {"invocation_id": invocation_id, "tool_name": tool_name}
    service = SubAgentSessionArtifactService(session)

    assert service.reconcile_orphaned_running_logs([], [record]) == 1

    repaired = json.loads((sessions / audit_name).read_text(encoding="utf-8"))
    assert repaired["meta"]["status"] == "orphaned"
    assert repaired["meta"]["failure_reason"] == "process_terminated"


def test_drain_quarantines_records_whose_field_types_would_crash_restore(tmp_path: Path) -> None:
    session = tmp_path / "session"
    pending = session / "sub_agents" / "pending"
    atomic_write_owner_only_text(
        pending / "null-name.json",
        json.dumps({"invocation_id": "a1b2c3d4e5f6", "tool_name": None}),
    )
    atomic_write_owner_only_text(
        pending / "list-call-id.json",
        json.dumps(
            {
                "invocation_id": "b1b2c3d4e5f6",
                "tool_name": "external",
                "parent_call_id": ["not", "a", "string"],
            }
        ),
    )
    atomic_write_owner_only_text(
        pending / "good.json",
        json.dumps({"invocation_id": "c1b2c3d4e5f6", "tool_name": "external"}),
    )
    service = SubAgentSessionArtifactService(session)
    records = service.drain_paused_records()
    assert [record["invocation_id"] for record in records] == ["c1b2c3d4e5f6"]
    quarantined = sorted(path.name for path in (pending / "corrupt").glob("*.json"))
    assert quarantined == ["list-call-id.json", "null-name.json"]
    assert not (pending / "null-name.json").exists()
    assert not (pending / "list-call-id.json").exists()


def test_drain_defers_records_past_aggregate_count_cap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A same-uid child cannot OOM restore by planting many small valid records:
    drain caps how many it materializes per restore and DEFERS the rest (they
    stay on disk to drain over later restores, not dropped/quarantined)."""
    monkeypatch.setattr(sub_agent_logs, "MAX_PENDING_RECORDS", 3)
    session = tmp_path / "session"
    pending = session / "sub_agents" / "pending"
    for index in range(5):
        atomic_write_owner_only_text(
            pending / f"rec{index}.json",
            json.dumps(
                {
                    "runner": "acp",
                    "invocation_id": f"{index:012x}",
                    "tool_name": "external",
                    "created_at": f"2026-01-01T00:00:0{index}+00:00",
                }
            ),
        )
    service = SubAgentSessionArtifactService(session)
    records = service.drain_paused_records()
    assert [record["invocation_id"] for record in records] == [
        "000000000000",
        "000000000001",
        "000000000002",
    ]
    # Every record — drained AND deferred — remains on disk (drain never
    # deletes; finalize does, and only for the ones consumed this restore).
    assert len(list(pending.glob("*.json"))) == 5
    assert not (pending / "corrupt").exists()


def test_drain_defers_records_past_aggregate_byte_cap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The aggregate BYTE ceiling also bounds restore memory when a child plants
    fewer, larger (but still per-record-legal) records."""
    monkeypatch.setattr(sub_agent_logs, "MAX_PENDING_RECORDS", 1000)
    monkeypatch.setattr(sub_agent_logs, "MAX_PENDING_RECORDS_TOTAL_BYTES", 400)
    session = tmp_path / "session"
    pending = session / "sub_agents" / "pending"
    for index in range(5):
        atomic_write_owner_only_text(
            pending / f"rec{index}.json",
            json.dumps(
                {
                    "runner": "acp",
                    "invocation_id": f"{index:012x}",
                    "tool_name": "external",
                    "created_at": f"2026-01-01T00:00:0{index}+00:00",
                    "note": "x" * 200,
                }
            ),
        )
    service = SubAgentSessionArtifactService(session)
    records = service.drain_paused_records()
    # Each record is >250 bytes; the loop stops once the running total crosses
    # 400 bytes (bounded overshoot of at most one record), so it drains some but
    # not all — and never quarantines the deferred remainder.
    assert 0 < len(records) < 5
    assert len(list(pending.glob("*.json"))) == 5
    assert not (pending / "corrupt").exists()


def test_reconcile_write_failure_keeps_pending_record_for_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If flipping a still-"running" audit to "orphaned" fails to WRITE, the
    pending record must be flagged and kept so the NEXT restore retries the
    repair — deleting it would strand the audit "running" + backlinked, hence
    permanently exempt from repair (the backlink skip in reconcile)."""
    session = tmp_path / "session"
    pending_path = session / "sub_agents" / "pending" / "paused.json"
    audit = session / "sub_agents" / "sessions" / "audit.json"
    atomic_write_owner_only_text(
        pending_path,
        json.dumps(
            {
                "runner": "acp",
                "invocation_id": "a1b2c3d4e5f6",
                "tool_name": "external",
                "created_at": "2026-01-01T00:00:00+00:00",
            }
        ),
    )
    atomic_write_owner_only_text(
        audit,
        json.dumps(
            {
                "meta": {
                    "runner": "acp",
                    "status": "running",
                    "invocation_id": "a1b2c3d4e5f6",
                    "tool_name": "external",
                },
                "acp_state": {"attempts": [], "translated_updates": []},
            }
        ),
    )
    service = SubAgentSessionArtifactService(session)
    records = service.drain_paused_records()
    assert len(records) == 1

    real_write = sub_agent_logs.atomic_write_owner_only_text

    def _failing_write(path: Path, text: str) -> None:
        if Path(path) == audit:
            raise OSError("disk full")
        real_write(path, text)

    monkeypatch.setattr(sub_agent_logs, "atomic_write_owner_only_text", _failing_write)
    # The audit could not be re-written, so nothing is counted as repaired...
    assert service.reconcile_orphaned_running_logs([], records) == 0
    assert records[0].get(sub_agent_logs.SUB_AGENT_REPAIR_FAILED_KEY) is True
    monkeypatch.setattr(sub_agent_logs, "atomic_write_owner_only_text", real_write)
    # ...and even marking it CONSUMED must not delete it — the repair-failed
    # flag wins over the consumed→unlink branch so the retry survives.
    records[0][sub_agent_logs.SUB_AGENT_RESTORE_CONSUMED_KEY] = True
    service.finalize_restored_paused_records(records)
    assert pending_path.exists()


def test_scan_bounded_json_files_caps_and_flags_truncation(tmp_path: Path) -> None:
    """The scan helper returns at most `max_files` sorted entries + a truncated
    flag; a missing directory yields empty rather than raising."""
    from chrys.service.session.sub_agent_logs import scan_bounded_json_files

    for index in range(5):
        atomic_write_owner_only_text(tmp_path / f"f{index}.json", "{}")
    capped, truncated = scan_bounded_json_files(tmp_path, max_files=3)
    assert len(capped) == 3 and truncated is True
    full, not_truncated = scan_bounded_json_files(tmp_path, max_files=10)
    assert len(full) == 5 and not_truncated is False
    assert scan_bounded_json_files(tmp_path / "absent", max_files=3) == ([], False)


def test_scan_bounded_json_files_enumerates_lazily_via_scandir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Path.glob/Path.iterdir are EAGER on 3.14 (they list scandir before
    yielding), so the helper MUST drive os.scandir and stop early — else a
    flooded directory is fully materialized. Instrument scandir to prove
    enumeration halts at max_files+1 entries, not the full 50-file listing."""
    for index in range(50):
        atomic_write_owner_only_text(tmp_path / f"f{index:03d}.json", "{}")

    consumed = {"n": 0}
    real_scandir = os.scandir

    class _Counting:
        def __init__(self, inner: object) -> None:
            self._inner = inner

        def __iter__(self) -> object:
            return self

        def __next__(self) -> object:
            entry = next(self._inner)
            consumed["n"] += 1
            return entry

        def __enter__(self) -> object:
            return self

        def __exit__(self, *exc: object) -> object:
            return self._inner.__exit__(*exc)

    monkeypatch.setattr(sub_agent_logs.os, "scandir", lambda path: _Counting(real_scandir(path)))
    paths, truncated = sub_agent_logs.scan_bounded_json_files(tmp_path, max_files=3)
    assert len(paths) == 3 and truncated is True
    # The core invariant: enumeration halted early — NOT all 50 entries consumed.
    assert consumed["n"] == 4


def test_scan_bounded_json_files_bounds_cumulative_bytes(tmp_path: Path) -> None:
    """A bounded FILE count is not enough: huge/sparse files must be refused by
    cumulative on-disk size (from scandir's free st_size) WITHOUT being read."""
    from chrys.service.session.sub_agent_logs import scan_bounded_json_files

    for index in range(5):
        atomic_write_owner_only_text(tmp_path / f"f{index}.json", "x" * 1024)
    paths, truncated = scan_bounded_json_files(tmp_path, max_files=100, max_total_bytes=1500)
    assert 0 < len(paths) < 5 and truncated is True
    unbounded, not_truncated = scan_bounded_json_files(tmp_path, max_files=100)
    assert len(unbounded) == 5 and not_truncated is False


def test_drain_bounds_scan_against_flood_of_corrupt_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Corrupt files bypass the per-record cap (they never advance the counters),
    so the SCAN itself must be bounded — else a flood of tiny corrupt files
    stalls every restore. Only the scan cap's worth are processed per restore."""
    monkeypatch.setattr(sub_agent_logs, "MAX_SCANNED_PENDING_FILES", 3)
    session = tmp_path / "session"
    pending = session / "sub_agents" / "pending"
    for index in range(5):
        atomic_write_owner_only_text(pending / f"bad{index}.json", "not valid json {")
    service = SubAgentSessionArtifactService(session)
    assert service.drain_paused_records() == []
    # Exactly the cap is archived this restore; the remainder defers (still on
    # disk, uncorrupted-directory), draining over later restores.
    assert len(list((pending / "corrupt").glob("*.json"))) == 3
    assert len(list(pending.glob("*.json"))) == 2


def test_reconcile_bounds_audit_scan_against_flood(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A flood of planted audit files must not force reading+repairing every one
    before drain's per-record caps apply; the audit scan is bounded per restore."""
    monkeypatch.setattr(sub_agent_logs, "MAX_SCANNED_AUDIT_FILES", 3)
    service = SubAgentSessionArtifactService(tmp_path)
    sessions = sub_agent_logs.sessions_dir(tmp_path)
    for index in range(5):
        atomic_write_owner_only_text(
            sessions / f"audit{index}.json",
            json.dumps({"meta": {"runner": "kernel", "status": "running", "invocation_id": f"{index:012x}"}}),
        )
    # Only the cap's worth are examined (and thus repaired) this restore.
    assert service.reconcile_orphaned_running_logs([], []) == 3
