# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for notification migration and saved theme renames."""

from __future__ import annotations

import dataclasses
import logging
from pathlib import Path
from typing import Any
from unittest.mock import create_autospec

import pytest
import yaml

import chrys.foundation.config.migrations as migrations
import chrys.foundation.platform as platform_mod
from chrys.foundation.config.migrations import (
    NOTIFICATIONS_COMPONENT,
    migrate_notifications_v0,
    migrate_renamed_theme,
)
from chrys.foundation.config.process_env import freeze_process_env
from chrys.foundation.config.settings import persist_theme
from chrys.foundation.config.settings_store import load_settings
from chrys.foundation.config.spec import Source
from chrys.foundation.config.user_settings import user_settings_path


@pytest.fixture
def config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    fake = dataclasses.replace(
        platform_mod.get_platform(),
        config_dir=tmp_path / "config",
        data_dir=tmp_path / "data",
    )
    monkeypatch.setattr(platform_mod, "get_platform", lambda: fake)
    return fake.config_dir


def _read_doc(config_dir: Path) -> dict:
    return yaml.safe_load((config_dir / "settings.yaml").read_text(encoding="utf-8"))


def test_user_settings_path_uses_the_config_directory(config_dir: Path) -> None:
    assert user_settings_path() == config_dir / "settings.yaml"


# ── notifications_v0 ───────────────────────────────────────────────────


def _write_notifications(config_dir: Path, doc: dict) -> Path:
    config_dir.mkdir(parents=True, exist_ok=True)
    path = config_dir / "notifications.yaml"
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")
    return path


def test_notifications_migration_moves_values_and_retires_the_file(config_dir: Path) -> None:
    legacy = {
        "schema_version": 1,
        "enabled": False,
        "sound": False,
        "stray": "ignored",
        "events": {"ask_user": False, "bogus_event": False},
    }
    path = _write_notifications(config_dir, legacy)

    result = migrate_notifications_v0()

    assert result.performed
    assert result.migrated == {
        "notifications.enabled": False,
        "notifications.delivery.sound": False,
        "notifications.events.ask_user": False,
    }

    doc = _read_doc(config_dir)
    assert doc["notifications"]["enabled"] is False
    assert doc["notifications"]["delivery"]["sound"] is False
    assert doc["notifications"]["events"]["ask_user"] is False
    assert "stray" not in doc["notifications"]
    assert "bogus_event" not in doc["notifications"]["events"]
    assert isinstance(doc["migrations"][NOTIFICATIONS_COMPONENT], str)

    # Renamed, never deleted: the old document survives for rollback.
    assert not path.exists()
    retired = config_dir / "notifications.yaml.migrated"
    assert yaml.safe_load(retired.read_text(encoding="utf-8")) == legacy


def test_notifications_migration_second_run_is_a_noop(config_dir: Path) -> None:
    _write_notifications(config_dir, {"enabled": False})
    first = migrate_notifications_v0()
    stamp = _read_doc(config_dir)["migrations"][NOTIFICATIONS_COMPONENT]

    second = migrate_notifications_v0()

    assert first.performed
    assert not second.performed
    assert second.migrated == {}
    assert _read_doc(config_dir)["migrations"][NOTIFICATIONS_COMPONENT] == stamp


def test_notifications_migration_prefers_newer_document_edits(config_dir: Path) -> None:
    """A crash-rerun with ``pending_cleanup`` must not roll back a panel edit."""
    path = _write_notifications(config_dir, {"enabled": False})
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "settings.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "notifications": {"enabled": True},
                "migrations": {NOTIFICATIONS_COMPONENT: {"pending_cleanup": True}},
            }
        ),
        encoding="utf-8",
    )

    result = migrate_notifications_v0()

    assert result.performed
    assert result.migrated == {}
    doc = _read_doc(config_dir)
    assert doc["notifications"]["enabled"] is True
    assert isinstance(doc["migrations"][NOTIFICATIONS_COMPONENT], str)
    assert not path.exists()


def test_a_legacy_notification_the_document_outranks_is_named_in_the_log(
    config_dir: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Retiring the file is the last moment to report the superseded preference."""
    path = _write_notifications(config_dir, {"enabled": False})
    config_dir.joinpath("settings.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "notifications": {"enabled": True},
                "migrations": {NOTIFICATIONS_COMPONENT: {"pending_cleanup": True}},
            }
        ),
        encoding="utf-8",
    )

    with caplog.at_level(logging.INFO, logger="chrys.foundation.config.migrations"):
        migrate_notifications_v0()

    assert any("notifications.enabled" in message and str(path) in message for message in caplog.messages)


def test_notifications_cleanup_defers_when_the_file_moved_since_it_was_read(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Retiring a file that changed since phase A carries away what nobody imported."""
    path = _write_notifications(config_dir, {"enabled": False})
    real_update = migrations.update_yaml_doc
    raced: list[bool] = []

    def rewrite_legacy_after_phase_a(*args: Any, **kwargs: Any) -> dict:
        doc = real_update(*args, **kwargs)
        if not raced:
            raced.append(True)
            _write_notifications(config_dir, {"enabled": False, "desktop": False})
        return doc

    monkeypatch.setattr(migrations, "update_yaml_doc", rewrite_legacy_after_phase_a)

    first = migrate_notifications_v0()

    assert first.migrated == {"notifications.enabled": False}
    assert path.is_file()
    assert not (config_dir / "notifications.yaml.migrated").exists()
    assert _read_doc(config_dir)["migrations"][NOTIFICATIONS_COMPONENT]["pending_cleanup"] is True

    second = migrate_notifications_v0()

    # Both fields: once the file has moved since phase A read it, the pending
    # document is no longer the newer of the two.
    assert second.migrated == {"notifications.enabled": False, "notifications.delivery.desktop": False}
    assert not path.exists()
    assert isinstance(_read_doc(config_dir)["migrations"][NOTIFICATIONS_COMPONENT], str)


def test_a_notifications_file_written_after_completion_wins_over_the_document(config_dir: Path) -> None:
    """Completion renames this path away, so anything back at it was written since."""
    # The old format cannot say "unset" — its reader defaulted every absent
    # field to on — so a partial file that comes back still describes a whole
    # state. Importing only its explicit fields would leave ``sound`` off here
    # while the build that wrote the file is showing the user a sound on.
    _write_notifications(config_dir, {"enabled": False, "sound": False})
    migrate_notifications_v0()
    assert _read_doc(config_dir)["notifications"]["delivery"]["sound"] is False

    path = _write_notifications(config_dir, {"enabled": True, "desktop": False})
    result = migrate_notifications_v0()

    assert result.performed
    assert result.migrated == {
        "notifications.enabled": True,
        "notifications.delivery.desktop": False,
        "notifications.delivery.sound": True,
        "notifications.suppress_when_focused": True,
        "notifications.events.approval_required": True,
        "notifications.events.ask_user": True,
        "notifications.events.turn_complete": True,
        "notifications.events.turn_error": True,
    }
    doc = _read_doc(config_dir)
    assert doc["notifications"]["enabled"] is True
    assert doc["notifications"]["delivery"]["desktop"] is False
    assert doc["notifications"]["delivery"]["sound"] is True
    assert not path.exists()
    assert isinstance(doc["migrations"][NOTIFICATIONS_COMPONENT], str)


def test_an_unfinished_notifications_migration_does_not_invent_the_absent_fields(config_dir: Path) -> None:
    """Before completion the sparse read is the right one — defaults would overwrite."""
    # A crash-rerun folding in "on" for every field the legacy file omits would
    # undo panel edits made between the two phases, which is the opposite of
    # what the document-wins rule is there for.
    _write_notifications(config_dir, {"enabled": False})
    (config_dir / "settings.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "notifications": {"delivery": {"sound": False}},
                "migrations": {NOTIFICATIONS_COMPONENT: {"pending_cleanup": True}},
            }
        ),
        encoding="utf-8",
    )

    result = migrate_notifications_v0()

    assert result.migrated == {"notifications.enabled": False}
    assert _read_doc(config_dir)["notifications"]["delivery"]["sound"] is False


def test_notifications_migration_fresh_install_records_completion(config_dir: Path) -> None:
    result = migrate_notifications_v0()

    assert result.performed
    assert result.migrated == {}
    assert isinstance(_read_doc(config_dir)["migrations"][NOTIFICATIONS_COMPONENT], str)
    assert not (config_dir / "notifications.yaml.migrated").exists()


def test_a_notifications_edit_between_parse_and_digest_cannot_be_certified(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fingerprint has to describe the bytes that were parsed, not a re-read."""
    # One write from an editor that ignores the sidecar lock is enough if the
    # two are separate reads: phase A parses ``enabled: false``, the file
    # becomes ``enabled: true``, and the fingerprint taken afterwards belongs
    # to the *true* file. Phase B then retires a file nobody read and records
    # the state it replaced as migrated.
    path = _write_notifications(config_dir, {"enabled": False})
    real_read_bytes, real_read_text = Path.read_bytes, Path.read_text
    reads: list[bool] = []

    def edit_once_after_the_first_read(self: Path) -> None:
        # Whichever call touches the file first — the editor does not know or
        # care which of our reads it lands behind.
        if self == path and not reads:
            reads.append(True)
            _write_notifications(config_dir, {"enabled": True})

    def read_bytes(self: Path) -> bytes:
        data = real_read_bytes(self)
        edit_once_after_the_first_read(self)
        return data

    def read_text(self: Path, *args: Any, **kwargs: Any) -> str:
        data = real_read_text(self, *args, **kwargs)
        edit_once_after_the_first_read(self)
        return data

    monkeypatch.setattr(Path, "read_bytes", read_bytes)
    monkeypatch.setattr(Path, "read_text", read_text)

    result = migrate_notifications_v0()

    assert result.migrated == {"notifications.enabled": False}
    # The imported state and the retired file must be the same file, so the one
    # that arrived since survives for the next start to fold in.
    assert path.is_file()
    assert yaml.safe_load(path.read_text(encoding="utf-8")) == {"enabled": True}
    assert _read_doc(config_dir)["migrations"][NOTIFICATIONS_COMPONENT]["pending_cleanup"] is True


def test_a_notifications_file_that_cannot_be_read_at_all_is_not_retired(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two failed digests are not proof that the file is the one phase A read."""
    # A file nobody could read has no identity: phase A's digest is ``None`` and
    # so is phase B's, and comparing them equal would rename away preferences
    # that were never imported — a perfectly good file, as here, that this
    # machine simply cannot read right now.
    path = _write_notifications(config_dir, {"enabled": False})
    real_read_bytes = Path.read_bytes

    def unreadable(self: Path) -> bytes:
        if self == path:
            raise OSError(5, "Input/output error")
        return real_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", unreadable)

    with pytest.raises(OSError, match="Input/output error"):
        migrate_notifications_v0()
    assert path.is_file()
    assert not (config_dir / "settings.yaml").exists()


def test_notifications_migration_preserves_corrupt_source_and_ledger(config_dir: Path) -> None:
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "notifications.yaml").write_text("enabled: [not valid yaml", encoding="utf-8")
    (config_dir / "notifications.yaml.bak").write_text(yaml.safe_dump({"desktop": False}), encoding="utf-8")

    with pytest.raises(OSError, match=r"notifications\.yaml"):
        migrate_notifications_v0()
    assert (config_dir / "notifications.yaml").read_text(encoding="utf-8") == "enabled: [not valid yaml"
    assert (config_dir / "notifications.yaml.bak").is_file()
    assert not (config_dir / "settings.yaml").exists()


def test_migrated_notification_values_load_as_the_user_layer(config_dir: Path) -> None:
    freeze_process_env()
    _write_notifications(config_dir, {"enabled": False, "events": {"turn_error": False}})

    migrate_notifications_v0()
    loaded = load_settings()

    assert loaded.settings.notifications_enabled is False
    assert loaded.settings.notifications_event_turn_error is False
    assert loaded.settings.notifications_desktop is True
    assert loaded.source_for("notifications.enabled").layer is Source.USER


# ── A built-in theme that changed its name ──────────────────────────


def _write_settings(config_dir: Path, text: str) -> Path:
    config_dir.mkdir(parents=True, exist_ok=True)
    path = config_dir / "settings.yaml"
    path.write_text(text, encoding="utf-8")
    return path


@pytest.mark.parametrize(
    "text", ["ui:\n  theme: chrys-dark\n  locale: en\n", "ui.theme: chrys-dark\nui:\n  locale: en\n"]
)
def test_a_saved_theme_is_renamed_to_what_it_goes_by_now(config_dir: Path, text: str) -> None:
    path = _write_settings(config_dir, text + "mystery: kept\n")

    result = migrate_renamed_theme()

    assert result.performed
    assert result.migrated == {"ui.theme": "chrys"}
    doc = _read_doc(config_dir)
    assert doc["ui"] == {"theme": "chrys", "locale": "en"}
    assert doc["mystery"] == "kept"
    # The rewrite is its own record: nothing is left to do, so nothing is kept to say so.
    assert "migrations" not in doc
    assert "chrys-dark" not in path.read_text(encoding="utf-8")
    assert not migrate_renamed_theme().performed


@pytest.mark.parametrize(
    "text",
    [
        "ui:\n  theme: chrys\n",
        "ui:\n  theme: chrys-legacy\n",
        # A user theme saved from the old one kept its own name, and keeps it.
        "ui:\n  theme: chrys-dark-1\n",
        "ui:\n  theme: 7\n",
        "ui: chrys-dark\n",
        "ui:\n  locale: en\n",
        ": not yaml\n",
    ],
)
def test_a_theme_that_was_not_renamed_costs_no_write(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch, text: str
) -> None:
    path = _write_settings(config_dir, text)
    write = create_autospec(migrations.update_yaml_doc, side_effect=AssertionError("nothing to write"))
    monkeypatch.setattr(migrations, "update_yaml_doc", write)

    assert not migrate_renamed_theme().performed

    write.assert_not_called()
    assert path.read_text(encoding="utf-8") == text


def test_renaming_a_theme_does_not_create_the_config_directory(config_dir: Path) -> None:
    assert not migrate_renamed_theme().performed
    assert not config_dir.exists()


def test_a_theme_renamed_between_the_look_and_the_lock_is_left_alone(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _write_settings(config_dir, "ui:\n  theme: nord\n")
    look = create_autospec(migrations.read_yaml_doc, return_value={"ui": {"theme": "chrys-dark"}})
    monkeypatch.setattr(migrations, "read_yaml_doc", look)

    assert not migrate_renamed_theme().performed

    look.assert_called_once()
    assert path.read_text(encoding="utf-8") == "ui:\n  theme: nord\n"


def test_a_retired_theme_name_loads_as_the_new_one_from_a_layer_nothing_rewrites(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CHRYS_THEME", "chrys-dark")
    freeze_process_env()

    loaded = load_settings()

    assert loaded.settings.theme == "chrys"
    assert loaded.source_for("ui.theme").layer is Source.ENV
    assert loaded.warnings == ()


def test_a_retired_theme_name_loads_as_the_new_one_before_the_document_is_rewritten(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CHRYS_THEME", raising=False)
    freeze_process_env()
    _write_settings(config_dir, "ui:\n  theme: chrys-dark\n")

    loaded = load_settings()

    assert loaded.settings.theme == "chrys"
    assert loaded.source_for("ui.theme").layer is Source.USER


def test_a_retired_theme_name_cannot_be_saved_again(config_dir: Path) -> None:
    persist_theme("chrys-dark")

    assert _read_doc(config_dir)["ui"]["theme"] == "chrys"
