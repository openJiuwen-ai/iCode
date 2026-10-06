# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The buddy save file.

Every running instance shares one ``buddy.json``. Changes are read-modify-write
under an exclusive lock on a sidecar file and land through an atomic replace,
so readers need no lock: they see the old file or the new one, never half of
either. A second copy, ``buddy.json.bak``, is what a reader falls back to when
the primary is unreadable; the next change rewrites both.

The file is signed, not hidden::

    {"v": 2, "buddy": {...}, "sig": "<HMAC-SHA256 of the canonical buddy document>"}

The key is a constant in this module, so the signature is no protection from
anyone willing to read the source. It is there to catch accidental damage and
to make a casual hand edit fail instead of quietly producing a different buddy.
A file that does not verify, or that was written in an earlier format, reads as
"no buddy yet".
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import time
from pathlib import Path
from typing import TYPE_CHECKING

from chrys.app.features.buddy.model import BuddyRecord
from chrys.foundation.platform import get_platform
from chrys.foundation.platform.files import atomic_write_text
from chrys.foundation.util.lock import FileLock

if TYPE_CHECKING:
    from collections.abc import Callable

SAVE_VERSION = 2

_SAVE_FILE = Path("extras") / "buddy" / "buddy.json"
_SIGNING_KEY = b"icode-buddy-save-file"
# Bounded, so a wedged peer instance cannot hang the caller for good.
_LOCK_TIMEOUT_SECONDS = 10.0
# On Windows a file cannot be replaced while another process has it open, and readers take
# no lock: every shown panel of every instance opens the save file for a moment. A refused
# replace is tried again a few times before the change is given up.
_REPLACE_ATTEMPTS = 5
_REPLACE_RETRY_SECONDS = 0.05


class BuddyStore:
    """One save file. With no *path*, the one under the platform config directory.

    Strict clients read only the primary, reject damage and write no backup.
    Only a missing primary and backup together count as first use.
    """

    def __init__(self, path: Path | None = None, *, strict: bool = False) -> None:
        self._path = path
        self._strict = strict

    @property
    def path(self) -> Path:
        return self._path if self._path is not None else get_platform().config_dir / _SAVE_FILE

    @property
    def backup_path(self) -> Path:
        return _second_copy(self.path)

    def load(self) -> BuddyRecord | None:
        """The saved buddy, or None when there is none that can be read."""
        if self._strict:
            try:
                text = self.path.read_text(encoding="utf-8")
            except FileNotFoundError:
                # A missing primary with an existing backup is damaged storage, not first use.
                try:
                    self.backup_path.stat()
                except FileNotFoundError:
                    return None
                raise ValueError("Buddy primary save is missing while a backup exists") from None
            record = _unseal(text)
            if record is None:
                raise ValueError("Invalid Buddy save: unsupported version, signature or record")
            return record
        return _read(self.path)

    def update(self, change: Callable[[BuddyRecord | None], BuddyRecord | None]) -> BuddyRecord | None:
        """Apply *change* to the saved buddy under the lock, and return what is saved afterwards.

        *change* gets the current record, or None when there is none, and returns
        the record to save. Returning None leaves the file as it is.

        Raises:
            OSError: the save file could not be written. Another instance holding the
                lock for too long is the ``TimeoutError`` kind.
        """
        path = self.path
        path.parent.mkdir(parents=True, exist_ok=True)
        with FileLock(path.with_suffix(".lock"), timeout=_LOCK_TIMEOUT_SECONDS):
            current = self.load()
            changed = change(current)
            if changed is None or changed == current:
                return current
            sealed = _seal(changed)
            _write(path, sealed)
            if self._strict:
                # The primary is authoritative; strict clients do not recover from backups.
                return changed
            # The primary is what counts. A failed second copy costs the fallback, not the change.
            with contextlib.suppress(OSError):
                _write(_second_copy(path), sealed)
            return changed


def _second_copy(path: Path) -> Path:
    return path.with_name(path.name + ".bak")


def _write(path: Path, text: str) -> None:
    for attempt in range(1, _REPLACE_ATTEMPTS + 1):
        try:
            atomic_write_text(path, text)
            return
        except PermissionError:
            if attempt == _REPLACE_ATTEMPTS or not get_platform().is_windows:
                raise
            time.sleep(_REPLACE_RETRY_SECONDS)


def _read(path: Path) -> BuddyRecord | None:
    for candidate in (path, _second_copy(path)):
        with contextlib.suppress(OSError, UnicodeDecodeError):
            record = _unseal(candidate.read_text(encoding="utf-8"))
            if record is not None:
                return record
    return None


def _signature(document: object) -> str:
    canonical = json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hmac.new(_SIGNING_KEY, canonical.encode("ascii"), hashlib.sha256).hexdigest()


def _seal(record: BuddyRecord) -> str:
    document = record.to_json()
    envelope = {"v": SAVE_VERSION, "buddy": document, "sig": _signature(document)}
    return json.dumps(envelope, indent=2, ensure_ascii=False) + "\n"


def _unseal(text: str) -> BuddyRecord | None:
    try:
        envelope = json.loads(text)
    except ValueError, RecursionError:  # not JSON, or nested deeper than any save file
        return None
    if not isinstance(envelope, dict) or envelope.get("v") != SAVE_VERSION:
        return None
    document, signature = envelope.get("buddy"), envelope.get("sig")
    if not isinstance(signature, str):
        return None
    if not hmac.compare_digest(_signature(document).encode("ascii"), signature.encode("utf-8", "replace")):
        return None
    return BuddyRecord.from_json(document)
