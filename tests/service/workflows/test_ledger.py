# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The confirmation ledger: structured entries, replacement per file, and a file that degrades to empty."""

from __future__ import annotations

import json
import logging
import os
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from chrys.foundation.platform.files import atomic_write_owner_only_bytes
from chrys.service.workflows.ledger import LEDGER_VERSION, ConfirmationLedger, LedgerEntry, ledger_path


def _entry(**overrides: object) -> LedgerEntry:
    base = LedgerEntry(
        canonical_path="/home/me/.chrys/workflows/review.py",
        source_kind="global",
        workflow_id="review",
        title="Review",
        entry_digest="e" * 64,
        manifest_digest="m" * 64,
        schema_version=1,
        spec_digest="s" * 64,
        environment_fingerprint="f" * 64,
    )
    return replace(base, **overrides)  # type: ignore[arg-type]


def test_confirm_then_reload_answers_for_the_exact_triple_only(tmp_path: Path) -> None:
    path = ledger_path(tmp_path)
    ledger = ConfirmationLedger(path)
    assert not ledger.is_confirmed(_entry())
    ledger.confirm(_entry())
    reloaded = ConfirmationLedger(path)
    assert reloaded.is_confirmed(_entry())
    assert not reloaded.is_confirmed(_entry(entry_digest="x" * 64))
    assert not reloaded.is_confirmed(_entry(spec_digest="x" * 64))
    assert not reloaded.is_confirmed(_entry(environment_fingerprint="x" * 64))
    assert not reloaded.is_confirmed(_entry(source_kind="workdir"))


def test_confirming_the_same_file_again_replaces_its_earlier_entry(tmp_path: Path) -> None:
    path = ledger_path(tmp_path)
    ledger = ConfirmationLedger(path)
    ledger.confirm(_entry())
    ledger.confirm(_entry(canonical_path="/elsewhere/review.py", source_kind="workdir"))
    ledger.confirm(_entry(spec_digest="n" * 64))
    reloaded = ConfirmationLedger(path)
    assert reloaded.is_confirmed(_entry(canonical_path="/elsewhere/review.py", source_kind="workdir"))
    assert reloaded.is_confirmed(_entry(spec_digest="n" * 64))
    assert not reloaded.is_confirmed(_entry())


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX mode bits")
def test_the_ledger_file_is_owner_only(tmp_path: Path) -> None:
    path = ledger_path(tmp_path)
    ConfirmationLedger(path).confirm(_entry())
    assert os.stat(path).st_mode & 0o077 == 0


@pytest.mark.parametrize(
    "payload",
    [
        b"not json",
        b"\xff\xfe",
        json.dumps({"version": LEDGER_VERSION + 1, "entries": [_entry().to_dict()]}).encode(),
        json.dumps({"version": LEDGER_VERSION, "entries": "nope"}).encode(),
        json.dumps({"version": LEDGER_VERSION, "entries": [{"canonical_path": 3}, "x", {}]}).encode(),
        json.dumps([1, 2]).encode(),
    ],
)
def test_an_unusable_ledger_file_reads_as_empty_and_is_replaced_on_confirm(tmp_path: Path, payload: bytes) -> None:
    path = ledger_path(tmp_path)
    path.parent.mkdir(parents=True)
    atomic_write_owner_only_bytes(path, payload)  # as the product writes it (see tests/support/secure_files.py)
    ledger = ConfirmationLedger(path)
    assert ledger.recorded(_entry().canonical_path, _entry().source_kind) is None
    ledger.confirm(_entry())
    assert ConfirmationLedger(path).is_confirmed(_entry())


@pytest.mark.parametrize("missing_field", ["environment_fingerprint", "title"])
def test_entries_missing_a_field_are_dropped_and_the_rest_kept(tmp_path: Path, missing_field: str) -> None:
    path = ledger_path(tmp_path)
    path.parent.mkdir(parents=True)
    partial = _entry().to_dict()
    del partial[missing_field]
    wrong_type = _entry(canonical_path="/other.py").to_dict()
    wrong_type["schema_version"] = "1"
    payload = {"version": LEDGER_VERSION, "entries": [partial, wrong_type, _entry(workflow_id="kept").to_dict()]}
    atomic_write_owner_only_bytes(path, json.dumps(payload).encode("utf-8"))
    assert ConfirmationLedger(path).recorded(_entry().canonical_path, _entry().source_kind) == _entry(
        workflow_id="kept"
    )


def test_a_missing_ledger_is_empty_and_its_directory_is_created_on_confirm(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = ledger_path(tmp_path / "fresh")
    with caplog.at_level(logging.WARNING, logger="chrys.service.workflows.ledger"):
        ledger = ConfirmationLedger(path)
    assert ledger.recorded(_entry().canonical_path, _entry().source_kind) is None
    assert caplog.records == []  # a first run has no ledger; that is not worth a warning
    ledger.confirm(_entry())
    assert path.is_file()


def test_a_confirmation_recorded_by_another_ledger_since_loading_is_kept(tmp_path: Path) -> None:
    """Two chrys processes running --trust at once loaded the same file; neither rewrite drops the other's entry."""
    path = ledger_path(tmp_path)
    first = ConfirmationLedger(path)
    second = ConfirmationLedger(path)
    other = _entry(canonical_path="/elsewhere/other.py", source_kind="workdir", workflow_id="other")
    first.confirm(_entry())
    second.confirm(other)
    reloaded = ConfirmationLedger(path)
    assert reloaded.is_confirmed(_entry())
    assert reloaded.is_confirmed(other)
    assert second.is_confirmed(_entry())  # merged into the later writer's own view as well
    assert path.with_name("trusted.json.lock").is_file()


def test_remove_reloads_under_lock_and_forgets_only_the_requested_path(tmp_path: Path) -> None:
    path = ledger_path(tmp_path)
    first, second = ConfirmationLedger(path), ConfirmationLedger(path)
    first.confirm(_entry())
    other = _entry(canonical_path="/other.py", title="Other title")
    second.confirm(other)
    first.remove(_entry().canonical_path)
    assert ConfirmationLedger(path).is_confirmed(other)
    assert not ConfirmationLedger(path).is_confirmed(_entry())
    first.remove("/never-confirmed.py")
    assert ConfirmationLedger(path).is_confirmed(other)
    assert not ConfirmationLedger(path).is_confirmed(_entry())


def test_compact_ledger_stays_readable_if_either_writer_would_exceed_the_limit(tmp_path):
    from chrys.service.workflows.ledger import MAX_LEDGER_BYTES, migrate_ledger

    path = ledger_path(tmp_path)
    entry = _entry(source_kind="project", title="")
    payload = {"version": LEDGER_VERSION, "entries": [entry.to_dict()]}
    compact = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    title = "x" * (MAX_LEDGER_BYTES - len(compact) - 1)
    payload["entries"][0]["title"] = title
    original = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    assert len(original) == MAX_LEDGER_BYTES - 1
    atomic_write_owner_only_bytes(path, original)
    confirmed = replace(entry, source_kind="workdir", title=title)

    def unexpected(*_args):
        pytest.fail("A valid ledger should be left alone when the new bytes would exceed the size limit")

    assert not migrate_ledger(path, backup=unexpected, on_read_error=unexpected)
    assert path.read_bytes() == original

    ledger = ConfirmationLedger(path)
    other = _entry(canonical_path="/other.py")
    assert ledger.is_confirmed(confirmed)
    with pytest.raises(ValueError, match="too large") as error:
        ledger.confirm(other)
    assert str(path) in str(error.value)
    assert f"{MAX_LEDGER_BYTES:,} bytes" in str(error.value)
    assert ledger.is_confirmed(confirmed)
    assert not ledger.is_confirmed(other)
    assert path.read_bytes() == original


def test_ledger_migration_skips_a_read_only_file(tmp_path):
    from chrys.service.workflows.ledger import migrate_ledger

    path = ledger_path(tmp_path)
    original = json.dumps({"version": LEDGER_VERSION, "entries": [_entry(source_kind="project").to_dict()]}).encode()
    atomic_write_owner_only_bytes(path, original)
    path.chmod(0o400)
    try:
        assert not migrate_ledger(path, backup=lambda *_: pytest.fail("read-only"), on_read_error=lambda *_: None)
    finally:
        path.chmod(0o600)
    assert path.read_bytes() == original


@pytest.mark.parametrize("newer_source", ["project", "workdir"])
def test_legacy_and_current_confirmations_keep_only_the_last_entry(tmp_path, newer_source):
    from chrys.service.workflows.ledger import migrate_ledger

    path = ledger_path(tmp_path)
    older_source = "workdir" if newer_source == "project" else "project"
    older = _entry(source_kind=older_source, spec_digest="old")
    newer = _entry(source_kind=newer_source, spec_digest="new")
    unrelated = _entry(source_kind="global")
    payload = {"version": LEDGER_VERSION, "entries": [older.to_dict(), unrelated.to_dict(), newer.to_dict()]}
    original = json.dumps(payload).encode("utf-8")
    atomic_write_owner_only_bytes(path, original)
    expected = replace(newer, source_kind="workdir")
    stale = replace(older, source_kind="workdir")
    ledger = ConfirmationLedger(path)
    assert ledger.recorded(newer.canonical_path, "workdir") == expected
    assert ledger.is_confirmed(expected)
    assert not ledger.is_confirmed(stale)
    assert ledger.is_confirmed(unrelated)

    backups = {}
    assert migrate_ledger(path, backup=backups.__setitem__, on_read_error=lambda *_: pytest.fail("invalid"))
    assert backups == {path: original}
    assert json.loads(path.read_bytes())["entries"] == [unrelated.to_dict(), expected.to_dict()]
