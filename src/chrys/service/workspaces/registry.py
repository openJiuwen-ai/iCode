# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Locked, atomic shared workspace definitions and explicit session membership."""

from __future__ import annotations

import builtins
import json
import logging
import os
import re
import stat as stat_mod
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING
from uuid import uuid4

from chrys.foundation.branding import APP_DISPLAY_NAME
from chrys.foundation.i18n import DisplayPath, msg
from chrys.foundation.models.workspace import WorkingDir, Workspace, control_chars_as_spaces
from chrys.foundation.platform import get_platform
from chrys.foundation.platform.files import atomic_write_text
from chrys.foundation.util.lock import FileLock
from chrys.foundation.util.session_ids import session_id_key
from chrys.service.workspaces.paths import PRIMARY_MISSING, contains

if TYPE_CHECKING:
    from chrys.foundation.i18n import MessageRef

logger = logging.getLogger(__name__)

_REGISTRY_UNREADABLE = msg("workspaces.registry_unreadable", fallback="Cannot read the workspace list: {path}")
_REGISTRY_NEWER = msg(
    "workspaces.registry_newer_version",
    fallback="The workspace list was written by a newer version of {app}: {path}",
)
_WRITE_FAILED = msg("workspaces.write_failed", fallback="Workspace list could not be saved: {reason}")
_NOT_FOUND = msg("workspaces.not_found", fallback="This workspace no longer exists.")
_SELECTOR_NOT_FOUND = msg("workspaces.selector_not_found", fallback="No workspace matches {selector}.")
_SELECTOR_AMBIGUOUS = msg("workspaces.selector_ambiguous", fallback="Workspace selector is ambiguous: {selector}")
_NAME_EMPTY = msg("workspaces.name_empty", fallback="Enter a workspace name.")
_NAME_EXISTS = msg("workspaces.name_exists", fallback="A workspace with the name {name} already exists.")
_NAME_CONTROL_CHAR = msg("workspaces.name_control_char", fallback="A workspace name can't contain control characters.")
_OVERLAP = msg("workspaces.overlap", fallback="Overlaps with {path}")
_FOLDER_INVALID = msg("workspaces.folder_invalid", fallback="Workspace folders must use absolute paths.")


_LOCK_TIMEOUT = 1.0
_WORKSPACE_ID_PREFIX = "ws_"
_WORKSPACE_ID = re.compile(rf"{_WORKSPACE_ID_PREFIX}[0-9a-f]{{32}}")


def is_workspace_id(value: str) -> bool:
    """Return whether *value* is a full registry-generated workspace ID."""
    return _WORKSPACE_ID.fullmatch(value) is not None


def _write_failed(error: OSError) -> WorkspaceRegistryError:
    return WorkspaceRegistryError(
        f"Workspace list could not be saved: {error}",
        _WRITE_FAILED.bind(reason=str(error)),
        code="workspace_registry_write_failed",
    )


def _read_regular_text(path: Path) -> str:
    """Read *path* only if it is a regular file: opened non-blocking, so a named pipe cannot hold the open."""
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0))
    with os.fdopen(fd, "rb") as file:
        if not stat_mod.S_ISREG(os.fstat(file.fileno()).st_mode):
            raise OSError(f"Not a regular file: {path}")
        # Accept a leading BOM: the list is edited by hand, and some Windows editors add one.
        return file.read().decode("utf-8-sig")


def _file_signature(stat: os.stat_result) -> tuple[int, int, int, int, int]:
    return stat.st_ino, stat.st_mtime_ns, stat.st_size, stat.st_ctime_ns, stat.st_mode


def _name_key(name: str) -> str:
    return name.strip().casefold()


@dataclass(frozen=True, slots=True)
class RegistrySnapshot:
    """A missing file is an empty readable registry; an error is explicitly distinct."""

    workspaces: tuple[Workspace, ...] = ()
    error: WorkspaceRegistryError | None = None
    sessions: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))

    last_used_at: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))
    _session_keys: dict[str, list[str]] = field(init=False, repr=False, compare=False)
    _workspaces_by_id: dict[str, Workspace] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        session_keys: dict[str, list[str]] = {}
        for key in self.sessions:
            session_keys.setdefault(session_id_key(key), []).append(key)
        object.__setattr__(self, "_session_keys", session_keys)
        object.__setattr__(self, "_workspaces_by_id", {workspace.id: workspace for workspace in self.workspaces})

    def maps_session(self, session_id: str) -> bool:
        """Return whether the saved session id has a mapping."""
        return session_id_key(session_id) in self._session_keys

    def workspace_id_for_session(self, session_id: str) -> str | None:
        """Return the workspace id for a session in this snapshot."""
        workspace = self.session_workspace(session_id)
        return workspace.id if workspace is not None else None

    def workspace(self, workspace_id: str | None) -> Workspace | None:
        """Return the workspace definition from this snapshot."""
        if self.error is not None or not workspace_id:
            return None
        return self._workspaces_by_id.get(workspace_id)

    def session_workspace(self, session_id: str) -> Workspace | None:
        if self.error is not None:
            return None
        keys = self._session_keys.get(session_id_key(session_id))
        return self.workspace(self.sessions[keys[0]]) if keys else None


class WorkspaceRegistryError(ValueError):
    """A rejected read or mutation with a localized display counterpart."""

    def __init__(self, message: str, display_message: MessageRef, *, code: str = "workspace_registry_error") -> None:
        super().__init__(message)
        self.display_message = display_message
        self.code = code


def workspace_not_found(workspace_id: str | None) -> WorkspaceRegistryError:
    """A workspace id missing from the list; the id is internal, so only the log names it."""
    return WorkspaceRegistryError(
        f"Workspace does not exist: {workspace_id}", _NOT_FOUND.bind(), code="workspace_not_found"
    )


def workspace_selector_not_found(selector: str) -> WorkspaceRegistryError:
    """No workspace has this ID, name or ID prefix; unlike a deleted ID, the user typed it, so it is shown."""
    return WorkspaceRegistryError(
        f"No workspace matches: {selector}",
        _SELECTOR_NOT_FOUND.bind(selector=selector),
        code="workspace_not_found",
    )


def workspace_selector_ambiguous(selector: str) -> WorkspaceRegistryError:
    return WorkspaceRegistryError(
        f"Workspace selector is ambiguous: {selector}",
        _SELECTOR_AMBIGUOUS.bind(selector=selector),
        code="workspace_selector_ambiguous",
    )


def _registry_unreadable(path: Path) -> WorkspaceRegistryError:
    return WorkspaceRegistryError(
        f"Cannot read the workspace list: {path}",
        _REGISTRY_UNREADABLE.bind(path=DisplayPath(path)),
        code="workspace_registry_unreadable",
    )


class _NewerRegistry(WorkspaceRegistryError):
    def __init__(self, path: Path) -> None:
        super().__init__(
            f"The workspace list was written by a newer version of {APP_DISPLAY_NAME}: {path}",
            _REGISTRY_NEWER.bind(app=APP_DISPLAY_NAME, path=DisplayPath(path)),
            code="workspace_registry_unreadable",
        )


_PRIMARY_IMMUTABLE = msg(
    "workspaces.primary_immutable",
    fallback="The primary folder cannot be changed. Create a new workspace to use a different primary folder.",
)


def _tidy_path(path: str) -> str:
    """Drop `.`, repeated and trailing separators; keep `..` for the platform's realpath when the workspace opens."""
    drive, rest = os.path.splitdrive(path)
    separators = os.sep + (os.altsep or "")
    parts = [part for part in re.split(f"[{re.escape(separators)}]", rest) if part not in ("", ".")]
    return drive + (os.sep if rest[:1] in separators and rest else "") + os.sep.join(parts)


def _validate_entry(
    name: str,
    primary: str,
    folders: Sequence[str],
    *,
    workspaces: tuple[Workspace, ...],
    previous: Workspace | None = None,
    require_primary_exists: bool = True,
) -> tuple[str, str, tuple[str, ...]]:
    """Validate persisted and proposed entries; only a new primary must still exist."""
    name = name.strip()
    if not name:
        raise WorkspaceRegistryError("Enter a workspace name.", _NAME_EMPTY.bind())
    if control_chars_as_spaces(name) != name:
        raise WorkspaceRegistryError("A workspace name can't contain control characters.", _NAME_CONTROL_CHAR.bind())
    workspace_id = previous.id if previous is not None else None
    if any(workspace.id != workspace_id and _name_key(workspace.name) == _name_key(name) for workspace in workspaces):
        raise WorkspaceRegistryError(f"A workspace with the name {name} already exists.", _NAME_EXISTS.bind(name=name))
    if any("\x00" in path for path in (primary, *folders)):
        raise WorkspaceRegistryError("Workspace folders must use absolute paths.", _FOLDER_INVALID.bind())
    try:
        for path in (primary, *folders):
            os.fsencode(path)
    except UnicodeEncodeError as error:
        raise WorkspaceRegistryError("Workspace folders must use encodable paths.", _FOLDER_INVALID.bind()) from error
    primary, *related_paths = (os.path.expanduser(path) for path in (primary, *folders))
    if any(not os.path.isabs(path) for path in (primary, *related_paths)):
        raise WorkspaceRegistryError("Workspace folders must use absolute paths.", _FOLDER_INVALID.bind())
    primary = _tidy_path(primary)
    related = tuple(_tidy_path(path) for path in related_paths)
    if previous is not None and primary != previous.primary_cwd:
        raise WorkspaceRegistryError("The primary folder cannot be changed.", _PRIMARY_IMMUTABLE.bind())
    if require_primary_exists and previous is None and not os.path.isdir(primary):
        raise WorkspaceRegistryError(
            f"Primary folder does not exist: {primary}", PRIMARY_MISSING.bind(path=DisplayPath(primary))
        )
    return name, primary, related


def _definition(workspace_id: str, name: str, primary_cwd: str, folders: Sequence[str]) -> Workspace:
    return Workspace(primary_cwd, [WorkingDir(path) for path in folders], id=workspace_id, name=name)


def _serialize_workspace(workspace: Workspace, last_used_at: str) -> dict[str, object]:
    return {
        "id": workspace.id,
        "name": workspace.name,
        "primary_cwd": workspace.primary_cwd,
        "working_dirs": [directory.path for directory in workspace.working_dirs],
        "last_used_at": last_used_at,
    }


class WorkspaceRegistry:
    """Cache successful reads by file revision, and merge every write under one lock."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path if path is not None else get_platform().config_dir / "workspaces.json"
        self._cached: tuple[tuple[int, int, int, int, int], RegistrySnapshot] | None = None

    def _read(self) -> RegistrySnapshot:
        try:
            raw = json.loads(_read_regular_text(self.path))
        except FileNotFoundError:
            return RegistrySnapshot()
        version = raw.get("version") if isinstance(raw, dict) else None
        if type(version) is int and version > 1:
            raise _NewerRegistry(self.path)
        if type(version) is not int or version != 1:
            raise ValueError("Invalid workspace registry version")
        if not isinstance(raw.get("workspaces"), list):
            raise ValueError("Invalid workspace registry")
        workspaces: list[Workspace] = []
        used: dict[str, str] = {}
        for item in raw["workspaces"]:
            if not isinstance(item, dict):
                raise ValueError("Invalid workspace definition")
            if any(not isinstance(item.get(key), str) or not item[key] for key in ("id", "name", "primary_cwd")):
                raise ValueError("Invalid workspace definition")
            folders = item.get("working_dirs")
            used_at = item.get("last_used_at", "")
            if (
                not isinstance(folders, list)
                or any(not isinstance(path, str) or not os.path.isabs(path) for path in folders)
                or not os.path.isabs(item["primary_cwd"])
                or not isinstance(used_at, str)
                or not is_workspace_id(item["id"])
                or item["id"] in used
            ):
                raise ValueError("Invalid workspace definition")
            name, primary, related = _validate_entry(
                item["name"], item["primary_cwd"], folders, workspaces=tuple(workspaces), require_primary_exists=False
            )
            used[item["id"]] = used_at
            workspaces.append(_definition(item["id"], name, primary, related))
        sessions = raw.get("sessions", {})
        if not isinstance(sessions, dict) or any(
            not isinstance(session_id, str) or not session_id or not isinstance(workspace_id, str) or not workspace_id
            for session_id, workspace_id in sessions.items()
        ):
            raise ValueError("Invalid workspace session mapping")
        sessions = {session_id: workspace_id for session_id, workspace_id in sessions.items() if workspace_id in used}
        return RegistrySnapshot(
            tuple(workspaces), sessions=MappingProxyType(sessions), last_used_at=MappingProxyType(used)
        )

    def snapshot(self) -> RegistrySnapshot:
        """Read without the write lock: a write replaces the file atomically, so one read sees one whole file.

        The signature is read twice so a write that landed in between is never cached
        under the newer signature (that would serve the older list until the next
        write); it is read again by the next caller instead.
        """
        try:
            signature = _file_signature(self.path.stat())
            cached = self._cached
            if cached is not None and signature == cached[0]:
                return cached[1]
            result = self._read()
            if _file_signature(self.path.stat()) == signature:
                self._cached = signature, result
            return result
        except (OSError, ValueError, RecursionError) as error:
            self._cached = None
            # A missing file is an empty registry; other failures are unreadable.
            if isinstance(error, FileNotFoundError):
                return RegistrySnapshot()
            return RegistrySnapshot(
                error=error if isinstance(error, _NewerRegistry) else _registry_unreadable(self.path)
            )

    def validate(
        self,
        name: str,
        primary: str,
        folders: Sequence[str] = (),
        *,
        workspaces: tuple[Workspace, ...],
        workspace_id: str | None = None,
    ) -> tuple[str, str, tuple[str, ...]]:
        """The same validation serves the dialog and all locked write interfaces."""
        previous = next((workspace for workspace in workspaces if workspace.id == workspace_id), None)
        if workspace_id is not None and previous is None:
            raise workspace_not_found(workspace_id)
        return self._validate_proposed(name, primary, folders, workspaces=workspaces, previous=previous)

    @staticmethod
    def _validate_proposed(
        name: str,
        primary: str,
        folders: Sequence[str],
        *,
        workspaces: tuple[Workspace, ...],
        previous: Workspace | None,
    ) -> tuple[str, str, tuple[str, ...]]:
        name, primary, related = _validate_entry(name, primary, folders, workspaces=workspaces, previous=previous)
        paths = (primary, *related)
        for index, path in enumerate(paths):
            for other in paths[:index]:
                if contains(path, other) or contains(other, path):
                    raise WorkspaceRegistryError(f"Overlaps with {other}", _OVERLAP.bind(path=DisplayPath(other)))
        return name, primary, related

    @property
    def _lock_path(self) -> Path:
        return self.path.with_suffix(".json.lock")

    def _mutate[T](self, change: Callable[[builtins.list[Workspace], dict[str, str], dict[str, str]], T]) -> T:
        """Commit definitions and membership in one locked read-merge-write."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with FileLock(self._lock_path, timeout=_LOCK_TIMEOUT):
                try:
                    snapshot = self._read()
                    entries = list(snapshot.workspaces)
                    sessions = dict(snapshot.sessions)
                    used = dict(snapshot.last_used_at)
                except _NewerRegistry:
                    raise
                except (OSError, ValueError, RecursionError) as error:
                    raise _registry_unreadable(self.path) from error
                result = change(entries, sessions, used)
                if (
                    tuple(entries) == snapshot.workspaces
                    and sessions == snapshot.sessions
                    and used == snapshot.last_used_at
                ):
                    return result
                atomic_write_text(
                    self.path,
                    json.dumps(
                        {
                            "version": 1,
                            "workspaces": [
                                _serialize_workspace(workspace, used.get(workspace.id, "")) for workspace in entries
                            ],
                            "sessions": sessions,
                        },
                        ensure_ascii=False,
                        indent=2,
                    )
                    + "\n",
                )
                self._cached = None
                return result
        except OSError as error:
            raise _write_failed(error) from error

    def create(self, name: str, primary: str, folders: Sequence[str] = ()) -> Workspace:
        def change(entries: list[Workspace], _sessions: dict[str, str], used: dict[str, str]) -> Workspace:
            valid_name, valid_primary, related = self.validate(name, primary, folders, workspaces=tuple(entries))
            workspace = _definition(_WORKSPACE_ID_PREFIX + uuid4().hex, valid_name, valid_primary, related)
            entries.append(workspace)
            used[workspace.id] = ""
            return workspace

        return self._mutate(change)

    def update(self, workspace_id: str, *, name: str, primary: str, folders: Sequence[str] = ()) -> Workspace:
        def proposed(workspaces: tuple[Workspace, ...], previous: Workspace) -> Workspace:
            valid_name, valid_primary, related = self._validate_proposed(
                name, primary, folders, workspaces=workspaces, previous=previous
            )
            return _definition(workspace_id, valid_name, valid_primary, related)

        def change(entries: list[Workspace], _sessions: dict[str, str], _used: dict[str, str]) -> Workspace:
            for index, previous in enumerate(entries):
                if previous.id == workspace_id:
                    workspace = proposed(tuple(entries), previous)
                    entries[index] = workspace
                    return workspace
            raise workspace_not_found(workspace_id)

        snapshot = self.snapshot()
        previous = snapshot.workspace(workspace_id)
        if previous is not None and previous == proposed(snapshot.workspaces, previous):
            return previous
        return self._mutate(change)

    def delete(self, workspace_id: str) -> None:
        def change(entries: list[Workspace], sessions: dict[str, str], used: dict[str, str]) -> None:
            for index, workspace in enumerate(entries):
                if workspace.id == workspace_id:
                    del entries[index]
                    used.pop(workspace_id, None)
                    for session_id in [session_id for session_id, record in sessions.items() if record == workspace_id]:
                        del sessions[session_id]
                    return
            raise workspace_not_found(workspace_id)

        self._mutate(change)

    def set_session_workspace(
        self, session_id: str, workspace_id: str | None, *, if_absent: bool = False, leaving: str | None = None
    ) -> bool:
        """Save membership and report whether the background-write conditions were met."""
        if not session_id:
            raise ValueError("Session id must not be empty")
        identity = session_id_key(session_id)

        if not if_absent and leaving is None:
            # A change must read again under the write lock.
            snapshot = self.snapshot()
            if snapshot.error is None:
                keys = snapshot._session_keys.get(identity)
                if workspace_id is None and not keys:
                    return True
                if (
                    workspace_id is not None
                    and keys == [session_id]
                    and snapshot.sessions[session_id] == workspace_id
                    and snapshot.workspace(workspace_id) is not None
                ):
                    return True

        def change(entries: list[Workspace], sessions: dict[str, str], _used: dict[str, str]) -> bool:
            keys = [key for key in sessions if session_id_key(key) == identity]
            if if_absent and keys:
                return False
            if leaving is not None and {sessions[key] for key in keys} != {leaving}:
                return False
            if workspace_id is not None and not any(workspace.id == workspace_id for workspace in entries):
                raise workspace_not_found(workspace_id)
            for key in keys:
                del sessions[key]
            if workspace_id is not None:
                sessions[session_id] = workspace_id
            return True

        return self._mutate(change)

    def remove_session_workspace(self, session_id: str) -> None:
        """Forget a deleted session's mapping if one was recorded."""
        self.set_session_workspace(session_id, None)

    def touch(self, workspace_id: str) -> None:
        def change(entries: list[Workspace], _sessions: dict[str, str], used: dict[str, str]) -> None:
            for workspace in entries:
                if workspace.id == workspace_id:
                    used[workspace_id] = datetime.now(UTC).isoformat()
                    return
            raise workspace_not_found(workspace_id)

        try:
            self._mutate(change)
        except OSError, ValueError:
            logger.debug("Could not record workspace use for %s", workspace_id, exc_info=True)
