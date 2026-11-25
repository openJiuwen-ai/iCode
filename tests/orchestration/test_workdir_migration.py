# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Startup upgrades old stored values without widening saved trust."""

from __future__ import annotations

import json
import stat
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

import pytest
import yaml

from chrys import __version__
from chrys.foundation.config.process_env import freeze_process_env
from chrys.foundation.config.settings import resolve_sessions_dir
from chrys.foundation.config.settings_store import load_settings, persist
from chrys.foundation.config.user_settings import flatten_user_doc
from chrys.foundation.config.warnings import MIGRATION_TRUST_CONFLICT, settings_warning_events
from chrys.foundation.config.yaml_store import backup_path_for, update_yaml_doc
from chrys.foundation.platform import get_platform
from chrys.foundation.platform.files import atomic_write_owner_only_text
from chrys.orchestration.startup import bootstrap_runtime
from chrys.orchestration.workdir_migration import (
    MIGRATION_CODES,
    MIGRATION_FAILURE,
    MIGRATION_NOTICE,
    WorkdirMigration,
)
from chrys.service.approval.grant_store import GRANTS_FILE, ApprovalGrantStore
from chrys.service.approval.reuse import ApprovalReuseService, FileCandidate, FileKey, ReuseContext
from chrys.service.hooks.outbox import DONE, Outbox, OutboxJob
from chrys.service.state.store import JsonFileStateStore
from chrys.service.workflows.ledger import ConfirmationLedger, LedgerEntry
from chrys.service.workflows.store import read_run_header
from tests.support.symlinks import symlink_or_skip

_SESSION_ID = "0123456789ab"
_GLOBAL_FILES = (
    "settings.yaml",
    GRANTS_FILE,
    "workflows/trusted.json",
)
_SESSION_FILES = (
    f"sessions/{_SESSION_ID}/{GRANTS_FILE}",
    f"sessions/{_SESSION_ID}/session.json",
    f"sessions/{_SESSION_ID}/session.json.bak",
    f"sessions/{_SESSION_ID}/workflows/run/run.json",
)


def _start():
    runtime = bootstrap_runtime(apply_patches=False, setup_telemetry=False)
    return runtime, [warning for warning in runtime.warnings if warning.code in MIGRATION_CODES]


def _old_record(name: str, directory: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    if name == "settings.yaml":
        return (
            {
                "project": {"config_enabled": True, "hooks_enabled": True, "custom": 4},
                "migrations": {"notifications_v0": "2026-01-01T00:00:00Z"},
                "project.skills_enabled": False,
                "workdir.skills_enabled": False,
            },
            {
                "project": {"custom": 4},
                "migrations": {"notifications_v0": "2026-01-01T00:00:00Z"},
                "workdir.skills_enabled": False,
                "workdir": {"config_enabled": True, "hooks_enabled": True},
            },
        )
    if name.endswith(GRANTS_FILE):
        session = name.startswith("sessions/")
        row = {
            "id": "0" * 32,
            "scope": "SESSION" if session else "PROJECT",
            "scope_id": _SESSION_ID if session else str(directory.resolve()),
            "project": str(directory.resolve()),
            "source": "EXPLICIT_USER",
            "created_at": 1.0,
            "key": {"kind": "file", "path": str(directory / "file.txt")},
        }
        new = dict(row)
        new.pop("project")
        if session:
            new["workdir"] = str(directory.resolve())
        else:
            new["scope"] = "WORKDIR"
        return {"version": 1, "rules": [row]}, {"version": 1, "rules": [new]}
    if "/outbox/" in name:
        job = asdict(
            OutboxJob(
                job_id=Path(name).stem,
                hook_id="archive",
                event="after_turn",
                payload={},
                created_at="2026-01-01T00:00:00Z",
                state=Path(name).parent.name,
                hook_source_path=str(directory / "hooks.yaml"),
            )
        )
        return {**job, "hook_source": "project"}, {**job, "hook_source": "workdir"}
    if name == "workflows/trusted.json":
        return (
            {"version": 1, "entries": [{"source_kind": "project", "canonical_path": str(directory)}]},
            {"version": 1, "entries": [{"source_kind": "workdir", "canonical_path": str(directory)}]},
        )
    if "/session.json" in name:
        return (
            {"state": {"identity": {"source_kind": "project"}, "other": "project"}, "meta": {"kind": "workflow"}},
            {"state": {"identity": {"source_kind": "workdir"}, "other": "project"}, "meta": {"kind": "workflow"}},
        )
    return {"source_kind": "project", "extra": 42}, {"source_kind": "workdir", "extra": 42}


def _file(config: Path, name: str) -> Path:
    if name.startswith("sessions/"):
        return resolve_sessions_dir(config, create=False) / name.removeprefix("sessions/")
    return config / name


def _write_old(config: Path, name: str, directory: Path, *, suffix: str = ""):
    old, expected = _old_record(name, directory)
    path = _file(config, name)
    text = (yaml.safe_dump(old) if name == "settings.yaml" else json.dumps(old)) + suffix
    atomic_write_owner_only_text(path, text)
    return path, path.read_bytes(), expected


def _read(path: Path):
    data = path.read_text(encoding="utf-8")
    return yaml.safe_load(data) if path.name == "settings.yaml" else json.loads(data)


@pytest.mark.parametrize("name", (*_GLOBAL_FILES, *_SESSION_FILES))
def test_old_file_is_upgraded_backed_up_and_reported_once(tmp_path, name):
    config = get_platform().config_dir
    path, raw, expected = _write_old(config, name, tmp_path)
    runtime, warnings = _start()
    assert _read(path) == expected
    if path.name in {"session.json", "session.json.bak"}:
        assert path.read_text(encoding="utf-8") == json.dumps(expected, indent=2, ensure_ascii=False)
    backup = config / "backups" / __version__
    assert (backup / name).read_bytes() == raw
    assert [warning.code for warning in warnings] == [MIGRATION_NOTICE]
    assert str(backup) in warnings[0].message
    if name == "settings.yaml":
        assert runtime.settings.workdir_config_enabled
        assert runtime.settings.workdir_hooks_enabled
        assert not runtime.settings.workdir_skills_enabled
    written = path.stat().st_mtime_ns
    assert _start()[1] == []
    assert path.stat().st_mtime_ns == written


def test_settings_backup_failure_does_not_skip_existing_migrations():
    config = get_platform().config_dir
    path = config / "settings.yaml"
    legacy = config / "notifications.yaml"
    blocked = config / "backups"
    atomic_write_owner_only_text(
        path,
        "project:\n  hooks_enabled: true\nui:\n  theme: chrys-dark\nmodel:\n  role:\n    buddy_model_id: ''\n",
    )
    atomic_write_owner_only_text(legacy, "enabled: false\n")
    atomic_write_owner_only_text(blocked, "Backup directory unavailable\n")

    runtime, warnings = _start()
    assert warnings == []
    assert runtime.settings.notifications_enabled is False
    assert runtime.settings.theme == "chrys"
    assert runtime.settings.workdir_hooks_enabled
    assert not legacy.exists()
    assert legacy.with_suffix(".yaml.migrated").is_file()
    preserved = _read(path)
    assert preserved["notifications"]["enabled"] is False
    assert preserved["ui"]["theme"] == "chrys"
    assert "model" not in preserved
    assert preserved["project"] == {"hooks_enabled": True}
    assert "workdir" not in preserved


@pytest.mark.parametrize("name", _GLOBAL_FILES)
def test_global_pass_leaves_late_legacy_files_for_read_time_compatibility(tmp_path, name):
    config = get_platform().config_dir
    _, original, _ = _write_old(config, name, tmp_path)
    assert [warning.code for warning in _start()[1]] == [MIGRATION_NOTICE]
    path, later, _ = _write_old(config, name, tmp_path, suffix="\n  \n")
    assert _start()[1] == []
    assert path.read_bytes() == later
    backup = config / "backups" / __version__ / name
    assert backup.read_bytes() == original
    assert not list(backup.parent.glob(f"{backup.name}.*"))


def test_global_pass_reports_read_errors_and_unmigrated_grants_once(tmp_path):
    config = get_platform().config_dir
    ledger = config / "workflows" / "trusted.json"
    grants = config / GRANTS_FILE
    atomic_write_owner_only_text(ledger, '{"entries":')
    atomic_write_owner_only_text(grants, '{"version":1,"rules":[null]}')

    _, warnings = _start()
    assert [warning.code for warning in warnings] == ["workdir_grants_unmigrated", MIGRATION_FAILURE]
    assert (config / "backups" / ".workdir-global.done").is_file()
    assert _start()[1] == []
    assert ledger.read_bytes() == b'{"entries":'
    assert grants.read_bytes() == b'{"version":1,"rules":[null]}'


def test_session_scan_completion_is_recorded(tmp_path):
    config = get_platform().config_dir
    name = _SESSION_FILES[1]
    _write_old(config, name, tmp_path)
    assert [warning.code for warning in _start()[1]] == [MIGRATION_NOTICE]
    assert len(list((config / "backups").glob(".workdir-[0-9a-f]*.done"))) == 1
    path, raw, _ = _write_old(config, name, tmp_path)
    assert _start()[1] == []
    assert path.read_bytes() == raw


@pytest.mark.parametrize("blocked", ["backups", f"backups/{__version__}"])
def test_session_scan_skips_traversal_and_marker_when_backups_are_unwritable(tmp_path, monkeypatch, blocked):
    config = get_platform().config_dir
    path, raw, _ = _write_old(config, _SESSION_FILES[1], tmp_path)
    sessions = resolve_sessions_dir(config, create=False)
    atomic_write_owner_only_text(config / blocked, "A file prevents the backup directory from being written")
    iterdir = Path.iterdir

    def refuse_session_scan(path):
        if path == sessions:
            pytest.fail("Session scan should be skipped without writable backups")
        return iterdir(path)

    monkeypatch.setattr(Path, "iterdir", refuse_session_scan)
    migration = WorkdirMigration(config)
    migration.migrate_sessions()
    assert migration.finish() == []
    assert path.read_bytes() == raw
    assert not list((config / "backups").glob(".workdir-[0-9a-f]*.done"))


def test_session_scan_marks_a_checkpoint_skipped_when_backup_fails(tmp_path, monkeypatch):
    config = get_platform().config_dir
    path, raw, _ = _write_old(config, _SESSION_FILES[1], tmp_path)

    def deny_backup(*args):
        raise PermissionError("Backup directory is read-only")

    with monkeypatch.context() as patch:
        patch.setattr(WorkdirMigration, "_backup", deny_backup)
        assert _start()[1] == []
    assert list((config / "backups").glob(".workdir-[0-9a-f]*.done"))
    assert _start()[1] == []
    assert path.read_bytes() == raw


def test_migration_markers_stay_outside_the_version_backup(tmp_path):
    config = get_platform().config_dir
    _write_old(config, _SESSION_FILES[1], tmp_path)
    outbox = Outbox(config / "hooks" / "outbox")
    job = outbox.write_pending(hook_id="archive", event="after_turn", payload={})
    outbox.mark_done(job, exit_code=0)

    assert [warning.code for warning in _start()[1]] == [MIGRATION_NOTICE]

    backups = config / "backups"
    assert (backups / ".workdir-global.done").is_file()
    assert len(list(backups.glob(".workdir-[0-9a-f]*.done"))) == 1
    version = backups / __version__
    assert {path.relative_to(version) for path in version.rglob("*") if path.is_file()} == {Path(_SESSION_FILES[1])}


def test_multiple_session_read_failures_are_reported_once():
    config = get_platform().config_dir
    atomic_write_owner_only_text(_file(config, _SESSION_FILES[0]), '{"rules":')
    checkpoint = _file(config, _SESSION_FILES[1])
    atomic_write_owner_only_text(checkpoint, '{"source_kind":"project","meta":')

    _, warnings = _start()
    assert [warning.code for warning in warnings] == [MIGRATION_FAILURE]
    assert "2 paths" in warnings[0].message
    assert _start()[1] == []
    assert checkpoint.read_bytes() == b'{"source_kind":"project","meta":'


@pytest.mark.asyncio
async def test_old_workflow_records_are_read_after_the_session_scan(tmp_path):
    config = get_platform().config_dir
    _write_old(config, _SESSION_FILES[0], tmp_path)
    assert [warning.code for warning in _start()[1]] == [MIGRATION_NOTICE]

    checkpoint = {
        "identity": {
            "workflow_id": "review",
            "canonical_path": str(tmp_path / ".chrys" / "workflows" / "review.py"),
            "source_kind": "project",
        },
        "workspace": {"primary_cwd": str(tmp_path), "working_dirs": [], "reference_files": []},
        "summary": {"run_count": 1, "latest_run_id": "run"},
    }
    session_file = _file(config, _SESSION_FILES[1])
    atomic_write_owner_only_text(session_file, json.dumps({"state": checkpoint, "meta": {"kind": "workflow"}}))
    raw_session = session_file.read_bytes()
    header, raw_header, _ = _write_old(config, _SESSION_FILES[3], tmp_path)

    assert _start()[1] == []
    store = JsonFileStateStore(resolve_sessions_dir(config))
    state = await store.load_workflow_session(_SESSION_ID)
    assert state is not None
    assert state.identity.source_kind == "workdir"
    assert session_file.read_bytes() == raw_session
    assert read_run_header(header.parent)["source_kind"] == "workdir"
    assert header.read_bytes() == raw_header

    updated = replace(state, run_count=2, latest_run_id="next-run")
    await store.save_workflow_session(_SESSION_ID, updated)
    assert _read(session_file)["state"]["identity"]["source_kind"] == "workdir"
    assert await store.load_workflow_session(_SESSION_ID) == updated


def test_migrated_shared_approval_only_matches_its_original_directory(tmp_path):
    config = get_platform().config_dir
    path, _, _ = _write_old(config, GRANTS_FILE, tmp_path)
    assert [warning.code for warning in _start()[1]] == [MIGRATION_NOTICE]
    service = ApprovalReuseService(ApprovalGrantStore(path), ApprovalGrantStore(tmp_path / "other.json"))
    candidate = FileCandidate(
        ReuseContext(_SESSION_ID, str(tmp_path.resolve())), frozenset({FileKey(path=str(tmp_path / "file.txt"))})
    )
    assert service.match(candidate)
    assert not service.match(replace(candidate, context=ReuseContext(_SESSION_ID, str(tmp_path / "other"))))
    assert not service.match(replace(candidate, context=replace(candidate.context, workspace_id="ws_" + "a" * 32)))


def test_user_settings_can_read_old_trust_from_a_backup():
    settings = get_platform().config_dir / "settings.yaml"
    atomic_write_owner_only_text(settings, "project: [")
    backup = backup_path_for(settings)
    atomic_write_owner_only_text(backup, "project:\n  hooks_enabled: true\n  skills_enabled: true\n")
    original = backup.read_bytes()
    runtime, warnings = _start()
    assert warnings == []
    assert [warning.code for warning in settings_warning_events(runtime.loaded)] == ["settings_document_read_failed"]
    assert runtime.settings.workdir_hooks_enabled
    assert runtime.settings.workdir_skills_enabled
    assert backup.read_bytes() == original
    assert settings.read_bytes() == b"project: ["


@pytest.mark.parametrize("with_conflict", [False, True])
def test_read_only_settings_keep_legacy_trust_and_report_conflicts(with_conflict):
    path = get_platform().config_dir / "settings.yaml"
    legacy_hooks = "false" if with_conflict else "true"
    original = f"project:\n  config_enabled: true\n  hooks_enabled: {legacy_hooks}\n  skills_enabled: true\n"
    original += "migrations:\n  notifications_v0: '2026-01-01T00:00:00Z'\n"
    if with_conflict:
        original += "workdir:\n  hooks_enabled: true\n"
    atomic_write_owner_only_text(path, original)
    path.chmod(stat.S_IRUSR)
    try:
        runtime, warnings = _start()
        assert runtime.settings.workdir_config_enabled
        assert runtime.settings.workdir_hooks_enabled is (not with_conflict)
        assert runtime.settings.workdir_skills_enabled
        assert path.read_bytes() == original.encode()

        reloaded = load_settings()
        assert reloaded.settings.workdir_hooks_enabled is (not with_conflict)
        read_warnings = settings_warning_events(reloaded)
        assert [warning.code for warning in read_warnings] == ([MIGRATION_TRUST_CONFLICT] if with_conflict else [])
        if with_conflict:
            assert "workdir.hooks_enabled" in read_warnings[0].message
        startup_codes = [warning.code for warning in (*warnings, *settings_warning_events(runtime.loaded))]
        assert startup_codes == ([MIGRATION_TRUST_CONFLICT] if with_conflict else [])
    finally:
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)


@pytest.mark.parametrize("gate", ["config_enabled", "hooks_enabled", "skills_enabled"])
@pytest.mark.parametrize("remove", [False, True])
def test_explicit_trust_edit_supersedes_legacy_spellings_after_backup_failure(gate, remove):
    config = get_platform().config_dir
    path = config / "settings.yaml"
    legacy = "true" if remove else "false"
    original = (
        f"project: &trust\n  {gate}: {legacy}\n  custom: keep\n"
        f"project.{gate}: {legacy}\nother: *trust\n"
        "migrations:\n  notifications_v0: 2026-01-01T00:00:00Z\n"
    )
    atomic_write_owner_only_text(path, original)
    atomic_write_owner_only_text(config / "backups", "Backup directory unavailable\n")
    runtime, warnings = _start()
    assert getattr(runtime.settings, f"workdir_{gate}") is remove
    assert warnings == []
    assert path.read_bytes() == original.encode()

    key = f"workdir.{gate}"
    if remove:
        assert persist({}, remove=[key]).ok
    else:
        assert persist({key: True}).ok
    loaded = load_settings()
    assert getattr(loaded.settings, f"workdir_{gate}") is not remove
    assert loaded.trust_conflicts == ()
    doc = _read(path)
    assert doc["project"] == {"custom": "keep"}
    assert f"project.{gate}" not in doc
    assert doc["other"] == {gate: remove, "custom": "keep"}


@pytest.mark.parametrize("legacy,current,effective", [("true", "1", True), ("0", "'off'", False)])
def test_equivalent_boolean_spellings_do_not_conflict(legacy, current, effective):
    config = get_platform().config_dir
    path = config / "settings.yaml"
    atomic_write_owner_only_text(path, f"project.hooks_enabled: {legacy}\nworkdir.hooks_enabled: {current}\n")

    freeze_process_env()
    loaded = load_settings()
    assert loaded.settings.workdir_hooks_enabled is effective
    assert loaded.trust_conflicts == ()

    runtime, warnings = _start()
    assert [warning.code for warning in warnings] == [MIGRATION_NOTICE]
    assert runtime.settings.workdir_hooks_enabled is effective
    assert "project.hooks_enabled" not in _read(path)


def test_settings_upgrade_keeps_effective_order_and_yaml_alias_values():
    path = get_platform().config_dir / "settings.yaml"
    original = (
        "approval.default_mode: auto\napproval:\n  default_mode: manual\n"
        "project: &trust\n  hooks_enabled: true\nworkdir: *trust\n"
        "other: *trust\nmigrations:\n  notifications_v0: 2026-01-01T00:00:00Z\n"
    )
    atomic_write_owner_only_text(path, original)
    before = yaml.safe_load(original)
    old_values, _ = flatten_user_doc(before, frozenset({"approval.default_mode", "project.hooks_enabled"}))
    runtime, warnings = _start()
    assert runtime.settings.default_approval_mode == old_values["approval.default_mode"] == "manual"
    assert runtime.settings.workdir_hooks_enabled is old_values["project.hooks_enabled"] is True
    assert [warning.code for warning in warnings] == [MIGRATION_NOTICE]
    after = _read(path)
    del before["project"]
    assert list(after.items()) == list(before.items())


@pytest.mark.parametrize("gate", ["config_enabled", "hooks_enabled", "skills_enabled"])
def test_older_settings_writer_cannot_undo_a_trust_revocation(gate):
    path = get_platform().config_dir / "settings.yaml"
    update_yaml_doc(path, lambda doc: {**doc, "project": {gate: True}})
    assert [warning.code for warning in _start()[1]] == [MIGRATION_NOTICE]
    update_yaml_doc(path, lambda doc: {**doc, "project": {gate: False}})
    assert _read(path)["workdir"][gate] is True

    original = path.read_bytes()
    runtime, warnings = _start()
    assert warnings == []
    assert getattr(runtime.settings, f"workdir_{gate}") is False
    (conflict,) = [
        warning for warning in settings_warning_events(runtime.loaded) if warning.code == MIGRATION_TRUST_CONFLICT
    ]
    assert f"workdir.{gate}" in conflict.message
    assert path.read_bytes() == original


def test_settings_migration_replaces_the_link_target(tmp_path):
    path = get_platform().config_dir / "settings.yaml"
    target = tmp_path / "dotfiles" / "settings.yaml"
    original = (
        "zeta: 1\nproject:\n  hooks_enabled: true\nalpha: 2\nmigrations:\n  notifications_v0: '2026-01-01T00:00:00Z'\n"
    )
    atomic_write_owner_only_text(target, original)
    path.parent.mkdir(parents=True, exist_ok=True)
    symlink_or_skip(path, target)

    runtime, warnings = _start()
    assert [warning.code for warning in warnings] == [MIGRATION_NOTICE]
    assert runtime.settings.workdir_hooks_enabled
    assert path.is_symlink()
    assert path.samefile(target)
    doc = _read(target)
    assert list(doc) == ["zeta", "alpha", "migrations", "workdir"]
    assert doc["workdir"] == {"hooks_enabled": True}
    assert (get_platform().config_dir / "backups" / __version__ / path.name).read_bytes() == original.encode()
    assert _start()[1] == []


def test_old_session_grant_can_be_reused_after_the_scan(tmp_path):
    config = get_platform().config_dir
    resolve_sessions_dir(config)
    _start()
    assert list((config / "backups").glob(".workdir-[0-9a-f]*.done"))
    path, raw, _ = _write_old(config, _SESSION_FILES[0], tmp_path)
    assert _start()[1] == []
    service = ApprovalReuseService(ApprovalGrantStore(config / GRANTS_FILE), ApprovalGrantStore(path))
    candidate = FileCandidate(
        ReuseContext(_SESSION_ID, str(tmp_path.resolve())), frozenset({FileKey(path=str(tmp_path / "file.txt"))})
    )
    assert service.match(candidate) == ("0" * 32,)
    assert path.read_bytes() == raw


@pytest.mark.parametrize("folder", ["done", "failed"])
def test_finished_outbox_records_are_left_as_written(tmp_path, folder):
    config = get_platform().config_dir
    path, raw, _ = _write_old(config, f"hooks/outbox/{folder}/job.json", tmp_path)
    assert _start()[1] == []
    assert path.read_bytes() == raw
    assert not (config / "backups" / __version__ / "hooks").exists()


def test_old_outbox_job_is_recoverable_without_migration(tmp_path):
    config = get_platform().config_dir
    path, raw, _ = _write_old(config, "hooks/outbox/pending/0123456789abcdef.json", tmp_path)
    outbox = Outbox(config / "hooks" / "outbox")
    assert _start()[1] == []
    [job] = outbox.list_stale_pending(retry_age_seconds=0)
    assert job.hook_source == "workdir"
    assert path.read_bytes() == raw
    assert outbox.mark_started(job)
    assert _read(path)["hook_source"] == "workdir"
    [reloaded] = outbox.list_stale_pending(retry_age_seconds=0)
    assert reloaded == job
    outbox.mark_done(reloaded, exit_code=0)
    assert _read(outbox.root / DONE / path.name)["hook_source"] == "workdir"


def test_old_workflow_ledger_entry_is_read_without_migration(tmp_path):
    from dataclasses import asdict

    entry = LedgerEntry(str(tmp_path / "workflow.py"), "workdir", "name", "a", "b", 1, "c", "d", "Title")
    path = tmp_path / "trust.json"
    atomic_write_owner_only_text(
        path, json.dumps({"version": 1, "entries": [{**asdict(entry), "source_kind": "project"}]})
    )
    assert ConfirmationLedger(path).is_confirmed(entry)
    assert json.loads(path.read_bytes())["entries"][0]["source_kind"] == "project"


def test_grant_migration_preserves_malformed_rows_and_reports_count(tmp_path):
    config = get_platform().config_dir
    path = _file(config, _SESSION_FILES[0])
    old, expected = _old_record(_SESSION_FILES[0], tmp_path)
    shared_old, shared_expected = _old_record(GRANTS_FILE, tmp_path)
    malformed = [
        '{ "scope": "PROJECT", "scope_id":"unrelated", "project":"other", "odd": "\u0061" }',
        '[  "unexpected",  null  ]',
        '{ "scope": "SESSION", "project": 5 }',
    ]
    rows = [json.dumps(old["rules"][0]), malformed[0], json.dumps(shared_old["rules"][0]), *malformed[1:]]
    payload = '{ "version" : 1, "rules" : [\n  ' + ",\n  ".join(rows) + "\n]}\n"
    atomic_write_owner_only_text(path, payload)
    store = ApprovalGrantStore(path)
    assert store.load() == []
    assert not store.add_many(expected["rules"])
    assert path.read_bytes() == payload.encode()
    _, warnings = _start()
    result = path.read_text(encoding="utf-8")
    assert json.loads(result)["rules"] == [
        expected["rules"][0],
        json.loads(malformed[0]),
        shared_expected["rules"][0],
        *map(json.loads, malformed[1:]),
    ]
    assert all(row in result for row in malformed)
    assert store.load() == []
    [notice] = [warning for warning in warnings if "unmigrated" in warning.message]
    assert "3" in notice.message
    assert (config / "backups" / __version__ / _SESSION_FILES[0]).read_bytes() == payload.encode()
    assert list((config / "backups").glob(".workdir-[0-9a-f]*.done"))
    assert _start()[1] == []
