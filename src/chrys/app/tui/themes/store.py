# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Validated user-theme files with atomic writes and optimistic conflict checks."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, fields
from pathlib import Path

import yaml
from textual.theme import Theme

from chrys.app.tui.theme_loader import (
    _theme_from_data,
    default_theme_directory,
    theme_is_read_only,
    theme_name_is_valid,
)
from chrys.foundation.i18n import MessageRef, msg
from chrys.foundation.platform.files import atomic_write_text
from chrys.foundation.text.encoding import decode_bytes
from chrys.foundation.util.lock import FileLock

_INVALID_NAME = msg(
    "tui.theme_store.invalid_name", fallback="Use a short name with letters, digits, dots, dashes or underscores."
)
_READ_ONLY = msg("tui.theme_store.read_only", fallback="Built-in themes are read-only. Choose a new name.")
_EXISTS = msg("tui.theme_store.exists", fallback="A theme with this name already exists. Choose another name.")
_CONFLICT = msg(
    "tui.theme_store.conflict", fallback="This theme changed on disk. Close the editor and reopen it before saving."
)
_IO_ERROR = msg("tui.theme_store.io_error", fallback="Could not save the theme: {reason}")
_LOAD_ERROR = msg("tui.theme_store.load_error", fallback="Could not open the theme: {reason}")
_DELETE_ERROR = msg("tui.theme_store.delete_error", fallback="Could not delete the theme: {reason}")
_DELETE_CONFLICT = msg(
    "tui.theme_store.delete_conflict",
    fallback="This theme changed on disk. Close the editor and reopen it before deleting.",
)


class ThemeStoreError(Exception):
    """A storage failure with a localizable display message."""

    def __init__(self, display: MessageRef) -> None:
        super().__init__(display)
        self.display = display

    @property
    def retryable(self) -> bool:
        """A save IO failure may be retried without changing the document."""
        return self.display.definition == _IO_ERROR


@dataclass(frozen=True)
class ThemeFileRevision:
    path: Path
    digest: str


def theme_data(theme: Theme) -> dict[str, object]:
    """Persist declared values, preserving expressions and omitted color fields."""
    return {
        field.name: dict(theme.variables) if field.name == "variables" else getattr(theme, field.name)
        for field in fields(Theme)
        if field.name != "name" and getattr(theme, field.name) is not None
    }


class UserThemeStore:
    def __init__(self, directory: Path | None = None) -> None:
        self.directory = directory if directory is not None else default_theme_directory()

    def _matches(self, name: str) -> list[Path]:
        if not self.directory.is_dir():
            return []
        return sorted(
            path
            for path in self.directory.iterdir()
            if path.suffix.lower() in {".yaml", ".yml"} and path.stem.casefold() == name.casefold()
        )

    def validate_name(self, name: str, revision: ThemeFileRevision | None = None) -> None:
        # Names are filenames on every supported platform, including Windows.
        device = name.split(".", 1)[0].upper()
        if (
            not theme_name_is_valid(name)
            or len(name) > 100
            or name.endswith(".")
            or device
            in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
        ):
            raise ThemeStoreError(_INVALID_NAME.bind())
        if theme_is_read_only(name):
            raise ThemeStoreError(_READ_ONLY.bind())
        try:
            matches = self._matches(name)
        except OSError as error:
            raise ThemeStoreError(_IO_ERROR.bind(reason=str(error))) from error
        if revision is not None:
            if name != revision.path.stem or matches != [revision.path] or revision.path.is_symlink():
                raise ThemeStoreError(_CONFLICT.bind())
        elif matches:
            raise ThemeStoreError(_EXISTS.bind())

    def suggest_name(self, base: str) -> str:
        stem = base[:85].rstrip(".") or "my-theme"
        for index in range(1, 10000):
            name = f"{stem}-{index}"
            try:
                self.validate_name(name)
            except ThemeStoreError as error:
                if error.display.definition == _EXISTS:
                    continue
                raise
            return name
        raise ThemeStoreError(_EXISTS.bind())

    def load(self, name: str) -> tuple[Theme, ThemeFileRevision]:
        if theme_is_read_only(name):
            raise ThemeStoreError(_READ_ONLY.bind())
        try:
            paths = self._matches(name)
            if len(paths) != 1 or paths[0].is_symlink() or paths[0].stem != name:
                raise ThemeStoreError(_CONFLICT.bind())
            path = paths[0]
            payload = path.read_bytes()
            # Decoded as the startup loader decodes it, so every theme it
            # lists opens here; the revision stays the digest of the bytes.
            data = yaml.safe_load(decode_bytes(payload, errors="strict"))
            if not isinstance(data, dict):
                raise ValueError("Theme must contain a mapping")
            theme = _theme_from_data(name, data, path)
            return theme, ThemeFileRevision(path, hashlib.sha256(payload).hexdigest())
        except ThemeStoreError:
            raise
        except Exception as error:
            raise ThemeStoreError(_LOAD_ERROR.bind(reason=str(error))) from error

    def save(self, theme: Theme, revision: ThemeFileRevision | None) -> ThemeFileRevision:
        """Call off the UI thread, after complete CSS preview validation."""
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            with FileLock(self.directory / ".themes.lock", timeout=3):
                self.validate_name(theme.name, revision)
                path = revision.path if revision is not None else self.directory / f"{theme.name}.yaml"
                if revision is not None and hashlib.sha256(path.read_bytes()).hexdigest() != revision.digest:
                    raise ThemeStoreError(_CONFLICT.bind())
                data = theme_data(theme)
                _theme_from_data(theme.name, data, path)
                payload = yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
                written = atomic_write_text(path, payload)
                return ThemeFileRevision(path, hashlib.sha256(written).hexdigest())
        except ThemeStoreError:
            raise
        except Exception as error:
            raise ThemeStoreError(_IO_ERROR.bind(reason=str(error))) from error

    def delete(self, name: str, revision: ThemeFileRevision) -> None:
        """Remove only the user file the editor opened, under the save lock."""
        if theme_is_read_only(name):
            raise ThemeStoreError(_READ_ONLY.bind())
        try:
            with FileLock(self.directory / ".themes.lock", timeout=3):
                if (
                    not theme_name_is_valid(name)
                    or revision.path.stem != name
                    or self._matches(name) != [revision.path]
                    or revision.path.is_symlink()
                    or hashlib.sha256(revision.path.read_bytes()).hexdigest() != revision.digest
                ):
                    raise ThemeStoreError(_DELETE_CONFLICT.bind())
                revision.path.unlink()
        except ThemeStoreError:
            raise
        except FileNotFoundError as error:
            raise ThemeStoreError(_DELETE_CONFLICT.bind()) from error
        except Exception as error:
            raise ThemeStoreError(_DELETE_ERROR.bind(reason=str(error))) from error
