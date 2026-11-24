# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Visit the stores that can rewrite retired working-directory names."""

from __future__ import annotations

import contextlib
import errno
import os
import tempfile
from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path

from chrys import __version__
from chrys.foundation.config.settings import resolve_sessions_dir
from chrys.foundation.config.user_settings import migrate_trust_settings
from chrys.foundation.config.warnings import MIGRATION_TRUST_CONFLICT, trust_conflict_warning
from chrys.foundation.events.types import Warning
from chrys.foundation.i18n import DisplayPath, msg
from chrys.foundation.i18n.formatting import format_message
from chrys.foundation.models.workdir_migration import BackupOriginal
from chrys.foundation.platform.files import atomic_write_owner_only_bytes, secure_open_owner_verified_binary
from chrys.service.approval.grant_store import GRANTS_FILE, ApprovalGrantStore
from chrys.service.state._session_files import SESSION_FILE_NAME, migrate_session_envelopes
from chrys.service.workflows.layout import HEADER_FILE, WORKFLOWS_DIR
from chrys.service.workflows.ledger import ledger_path, migrate_ledger
from chrys.service.workflows.store import migrate_run_headers

MIGRATION_NOTICE = "workdir_data_migrated"
MIGRATION_FAILURE = "workdir_data_migration_failed"
MIGRATION_SKIPPED = "workdir_grants_unmigrated"
MIGRATION_CODES = frozenset({MIGRATION_NOTICE, MIGRATION_FAILURE, MIGRATION_SKIPPED, MIGRATION_TRUST_CONFLICT})

_MIGRATED = msg(
    "startup.workdir_data_migrated",
    fallback="Migrated working directory data in {files}. Backups are in {backup}.",
)
_FAILED = msg(
    "startup.workdir_data_migration_failed",
    fallback="Could not read working directory data at {path} ({error}).",
    plural_fallback="Could not read working directory data at {count} paths, including {path} ({error}).",
)

_UNMIGRATED = msg(
    "startup.workdir_grants_unmigrated",
    fallback="Left {count} malformed approval row unmigrated in {path}.",
    plural_fallback="Left {count} malformed approval rows unmigrated in {path}.",
)


def _read_backup(path: Path) -> bytes | None:
    try:
        with secure_open_owner_verified_binary(path) as stream:
            return stream.read()
    except OSError as error:
        if error.errno not in {errno.ENOENT, errno.ENOTDIR}:
            raise
        return None


@dataclass
class WorkdirMigration:
    config_dir: Path
    warnings: list[Warning] = field(default_factory=list)
    _changed: dict[str, None] = field(default_factory=dict)
    _failed: dict[Path, str] = field(default_factory=dict)

    @property
    def backup_dir(self) -> Path:
        return self.config_dir / "backups" / __version__

    def _backup(self, relative: Path, raw: bytes) -> None:
        target = self.config_dir.resolve() / "backups" / __version__ / relative
        previous = _read_backup(target)
        if previous == raw:
            return
        if previous is not None:
            # Older processes may write again after upgrade. Keep every distinct original.
            target = target.with_name(f"{target.name}.{sha256(raw).hexdigest()}")
            if _read_backup(target) == raw:
                return
        atomic_write_owner_only_bytes(target, raw)

    def _backup_config(self, path: Path, raw: bytes) -> None:
        self._backup(path.relative_to(self.config_dir), raw)

    def _can_write_backups(self) -> bool:
        root = self.backup_dir.parent
        try:
            root.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryFile(dir=root):
                pass
            if self.backup_dir.exists():
                with tempfile.TemporaryFile(dir=self.backup_dir):
                    pass
        except OSError:
            return False
        return True

    def _record_read_error(self, path: Path, error: Exception) -> None:
        self._failed.setdefault(path, str(error))

    def _grants(self, path: Path, backup: BackupOriginal, label: str) -> None:
        changed, unmigrated = ApprovalGrantStore(path).migrate(backup=backup, on_read_error=self._record_read_error)
        if changed:
            self._changed[label] = None
        if unmigrated:
            display = _UNMIGRATED.bind(count=unmigrated, path=DisplayPath(path))
            self.warnings.append(
                Warning(code=MIGRATION_SKIPPED, message=format_message(display), display_message=display)
            )

    def migrate_global(self) -> None:
        """Record one full pass over the global stores."""
        marker = self.backup_dir.parent / ".workdir-global.done"
        if marker.exists() or not self._can_write_backups():
            return
        changed, conflicts = migrate_trust_settings(self.config_dir / "settings.yaml", self._backup_config)
        if changed:
            self._changed["settings.yaml"] = None
        if conflicts:
            self.warnings.append(trust_conflict_warning(conflicts))
        self._grants(self.config_dir / GRANTS_FILE, self._backup_config, GRANTS_FILE)
        if migrate_ledger(
            ledger_path(self.config_dir), backup=self._backup_config, on_read_error=self._record_read_error
        ):
            self._changed["workflows/trusted.json"] = None
        with contextlib.suppress(OSError):
            atomic_write_owner_only_bytes(marker, b"")

    def migrate_sessions(self) -> None:
        """Record one full pass over each session storage root."""
        sessions = resolve_sessions_dir(self.config_dir, create=False)
        if not sessions.exists():
            return
        root_id = sha256(os.fsencode(sessions)).hexdigest()[:16]
        marker = self.backup_dir.parent / f".workdir-{root_id}.done"
        if marker.exists() or not self._can_write_backups():
            return

        def backup_session(path: Path, raw: bytes) -> None:
            self._backup(Path("sessions") / path.relative_to(sessions), raw)

        with contextlib.suppress(OSError):
            for session in sessions.iterdir():
                if session.name.startswith(".") or session.is_symlink() or not session.is_dir():
                    continue
                self._grants(session / GRANTS_FILE, backup_session, f"sessions/*/{GRANTS_FILE}")
                if migrate_session_envelopes(session, backup=backup_session, on_read_error=self._record_read_error):
                    self._changed[f"sessions/*/{SESSION_FILE_NAME}"] = None
                if migrate_run_headers(session, backup=backup_session, on_read_error=self._record_read_error):
                    self._changed[f"sessions/*/{WORKFLOWS_DIR}/*/{HEADER_FILE}"] = None
            atomic_write_owner_only_bytes(marker, b"")

    def finish(self) -> list[Warning]:
        if self._failed:
            path, error = next(iter(self._failed.items()))
            display = _FAILED.bind(count=len(self._failed), path=DisplayPath(path), error=error)
            self.warnings.append(
                Warning(code=MIGRATION_FAILURE, message=format_message(display), display_message=display)
            )
        if self._changed:
            display = _MIGRATED.bind(files=", ".join(self._changed), backup=DisplayPath(self.backup_dir))
            self.warnings.append(
                Warning(code=MIGRATION_NOTICE, message=format_message(display), display_message=display)
            )
        return self.warnings
