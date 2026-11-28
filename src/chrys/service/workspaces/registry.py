# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Locked, atomic shared workspace definitions and explicit session membership."""

from __future__ import annotations

import asyncio
import builtins
import functools
import json
import logging
import os
import re
import stat as stat_mod
from collections.abc import Callable, Mapping, Sequence
from contextlib import ExitStack
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import cache
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING
from uuid import uuid4

from chrys.foundation.branding import APP_DISPLAY_NAME
from chrys.foundation.i18n import SUPPORTED_LOCALES, DisplayPath, Localizer, msg
from chrys.foundation.i18n.formatting import format_message
from chrys.foundation.models.workspace import WorkingDir, Workspace, control_chars_as_spaces, path_identity
from chrys.foundation.platform import get_platform
from chrys.foundation.platform.files import atomic_write_text
from chrys.foundation.util.lock import FileLock
from chrys.foundation.util.session_ids import session_id_key
from chrys.service.workspaces.paths import PRIMARY_MISSING, contains

if TYPE_CHECKING:
    from chrys.foundation.i18n import MessageDef, MessageRef

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
_NAME_RESERVED = msg("workspaces.name_reserved", fallback="The workspace name {name} is reserved.")
_NAME_CONTROL_CHAR = msg("workspaces.name_control_char", fallback="A workspace name can't contain control characters.")
UNSPECIFIED = msg("workspaces.unspecified", fallback="Unspecified")
_PRIMARY_OVERLAP = msg(
    "workspaces.primary_overlap", fallback="Reference folder {folder} overlaps with the primary folder {primary}."
)
_REFERENCE_OVERLAP = msg("workspaces.reference_overlap", fallback="Reference folders {first} and {second} overlap.")
_SAME_FOLDERS = msg(
    "workspaces.same_folders",
    fallback="Workspace “{name}” already has the same primary and reference folders. "
    "Add or remove a reference folder, or use “{name}”.",
)
_FOLDER_INVALID = msg("workspaces.folder_invalid", fallback="Workspace folders must use absolute paths.")
_EXPECTED_OBJECT = msg(
    "workspaces.registry_expected_object",
    fallback="Cannot read the workspace list at {path}. Entry {row}, field {field}: Expected an object.",
)
_EXPECTED_STRING = msg(
    "workspaces.registry_expected_string",
    fallback="Cannot read the workspace list at {path}. Entry {row}, field {field}: Expected a string.",
)
_EXPECTED_LIST = msg(
    "workspaces.registry_expected_list",
    fallback="Cannot read the workspace list at {path}. Entry {row}, field {field}: Expected a list.",
)
_EXPECTED_WORKSPACE_ID = msg(
    "workspaces.registry_expected_id",
    fallback=(
        "Cannot read the workspace list at {path}. Entry {row}, field {field}: "
        "Expected ws_ followed by 32 lowercase hexadecimal digits."
    ),
)
_WORKSPACE_ID_EXISTS = msg(
    "workspaces.registry_id_exists",
    fallback="Cannot read the workspace list at {path}. Entry {row}, field {field}: ID {id} already exists.",
)
_REGISTRY_FOLDER_INVALID = msg(
    "workspaces.registry_folder_invalid",
    fallback=(
        "Cannot read the workspace list at {path}. Entry {row}, field {field}: Workspace folders must use absolute paths."
    ),
)
_REGISTRY_NAME_EMPTY = msg(
    "workspaces.registry_name_empty",
    fallback="Cannot read the workspace list at {path}. Entry {row}, field {field}: Enter a workspace name.",
)
_REGISTRY_NAME_EXISTS = msg(
    "workspaces.registry_name_exists",
    fallback=(
        "Cannot read the workspace list at {path}. Entry {row}, field {field}: "
        "A workspace with the name {name} already exists."
    ),
)
_REGISTRY_NAME_CONTROL_CHAR = msg(
    "workspaces.registry_name_control_char",
    fallback=(
        "Cannot read the workspace list at {path}. Entry {row}, field {field}: "
        "A workspace name can't contain control characters."
    ),
)
_REGISTRY_NAME_ERRORS = {
    _NAME_EMPTY: _REGISTRY_NAME_EMPTY,
    _NAME_EXISTS: _REGISTRY_NAME_EXISTS,
    _NAME_CONTROL_CHAR: _REGISTRY_NAME_CONTROL_CHAR,
}


_LOCK_TIMEOUT = 1.0
_LOCK_RETRY_INTERVAL = 0.05
WORKSPACE_ID_PREFIX = "ws_"
_WORKSPACE_ID = re.compile(rf"{WORKSPACE_ID_PREFIX}[0-9a-f]{{32}}")


def is_workspace_id(value: str) -> bool:
    """Return whether *value* is a full registry-generated workspace ID."""
    return _WORKSPACE_ID.fullmatch(value) is not None


def _write_failed(error: OSError) -> WorkspaceRegistryError:
    return WorkspaceRegistryError(
        f"Workspace list could not be saved: {error}",
        _WRITE_FAILED.bind(reason=str(error)),
        code="workspace_registry_write_failed",
    )


class _RegistryLockBusy(Exception):
    """A one-shot write attempt could not acquire the registry lock."""

    def __init__(self, error: TimeoutError) -> None:
        super().__init__(str(error))
        self.error = error


async def retry_workspace_write(write: Callable[[], object]) -> None:
    """Retry lock acquisition, with validation and the write in one synchronous attempt."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _LOCK_TIMEOUT
    while True:
        try:
            write()
        except _RegistryLockBusy as busy:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise _write_failed(busy.error) from busy.error
            await asyncio.sleep(min(_LOCK_RETRY_INTERVAL, remaining))
        else:
            return


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


def is_unspecified_workspace_name(name: str, localizer: Localizer | None = None) -> bool:
    """Match the English name and the *localizer*'s; without one, any supported language's."""
    key = _name_key(name)
    if localizer is None:
        return key in _reserved_workspace_names()
    return key in (_name_key(UNSPECIFIED.fallback), _name_key(localizer.render(UNSPECIFIED.bind())))


@cache
def _reserved_workspace_names() -> frozenset[str]:
    return frozenset(_name_key(Localizer(locale).render(UNSPECIFIED.bind())) for locale in SUPPORTED_LOCALES)


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


class WorkspaceNameError(WorkspaceRegistryError):
    """A rejected workspace name."""


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


WORKSPACE_CHANGED = msg("workspaces.selection_changed", fallback="The workspace changed. Try again.")


def _check_expected_workspace(workspace: Workspace | None, expected: Workspace | None) -> None:
    if expected is not None and workspace != expected:
        raise WorkspaceRegistryError("The workspace changed. Try again.", WORKSPACE_CHANGED.bind())


PRIMARY_IMMUTABLE = msg(
    "workspaces.primary_immutable",
    fallback="The primary folder cannot be changed. Create a new workspace to use a different primary folder.",
)


def _tidy_path(path: str) -> str:
    """Drop `.`, repeated and trailing separators; keep `..` for the platform's realpath when the workspace opens."""
    drive, rest = os.path.splitdrive(path)
    separators = os.sep + (os.altsep or "")
    parts = [part for part in re.split(f"[{re.escape(separators)}]", rest) if part not in ("", ".")]
    return drive + (os.sep if rest[:1] in separators and rest else "") + os.sep.join(parts)


def workspace_folder_path(cwd: str, path: str) -> str:
    """Return the entered path's absolute realpath, resolving symlinks.

    Unlike ``workspace_folder_spelling``, this returns the physical path rather than keeping symlinks.
    """
    try:
        return _tidy_path(os.path.realpath(os.path.join(cwd, os.path.expanduser(path))))
    except (OSError, ValueError) as error:
        raise WorkspaceRegistryError("Invalid workspace folder path.", _FOLDER_INVALID.bind()) from error


def workspace_folder_spelling(cwd: str, path: str) -> str:
    """The entered path made absolute, symlinks kept, to compare with registered spellings."""
    return _tidy_path(os.path.join(cwd, os.path.expanduser(path)))


def validate_workspace_name(
    name: str, workspaces: tuple[Workspace, ...], workspace_id: str | None = None, *, check_reserved: bool = True
) -> str:
    """Validate a workspace name against a definition snapshot."""
    name = name.strip()
    if not name:
        raise WorkspaceNameError("Enter a workspace name.", _NAME_EMPTY.bind())
    if control_chars_as_spaces(name) != name:
        raise WorkspaceNameError("A workspace name can't contain control characters.", _NAME_CONTROL_CHAR.bind())
    key = _name_key(name)
    if (
        check_reserved
        and key in _reserved_workspace_names()
        and not any(workspace.id == workspace_id and workspace.name == name for workspace in workspaces)
    ):
        message = _NAME_RESERVED.bind(name=name)
        raise WorkspaceNameError(format_message(message), message)
    if any(workspace.id != workspace_id and _name_key(workspace.name) == key for workspace in workspaces):
        raise WorkspaceNameError(f"A workspace with the name {name} already exists.", _NAME_EXISTS.bind(name=name))
    return name


def _validate_folder(path: str) -> str:
    if "\x00" in path:
        raise WorkspaceRegistryError("Workspace folders must use absolute paths.", _FOLDER_INVALID.bind())
    try:
        os.fsencode(path)
    except UnicodeEncodeError as error:
        raise WorkspaceRegistryError("Workspace folders must use encodable paths.", _FOLDER_INVALID.bind()) from error
    path = os.path.expanduser(path)
    if not os.path.isabs(path):
        raise WorkspaceRegistryError("Workspace folders must use absolute paths.", _FOLDER_INVALID.bind())
    return _tidy_path(path)


def _validate_folders(primary: str, folders: Sequence[str]) -> tuple[str, tuple[str, ...]]:
    return _validate_folder(primary), tuple(_validate_folder(path) for path in folders)


def _probe_folders(primary: str, folders: Sequence[str], *, new: bool) -> None:
    """The folder checks that read the filesystem; writers run them before taking the registry lock.

    A folder on a mount that stops answering then holds up only this edit, not
    every process waiting to write the registry.
    """
    primary, related = _validate_folders(primary, folders)
    if new and not os.path.isdir(primary):
        raise WorkspaceRegistryError(
            f"Primary folder does not exist: {primary}", PRIMARY_MISSING.bind(path=DisplayPath(primary))
        )
    # Each path and its ancestors are resolved once, so checking a pair is a set lookup.
    identity_of, realpath = functools.cache(path_identity), functools.cache(os.path.realpath)

    @functools.cache
    def lineage(path: str) -> frozenset[tuple[int, int] | str]:
        resolved = Path(realpath(path))
        return frozenset(identity_of(candidate) for candidate in (resolved, *resolved.parents))

    def inside(folder: str, directory: str) -> bool:
        identity = identity_of(folder)
        if isinstance(identity, str):  # A missing folder compares through its existing ancestor.
            return contains(folder, directory, identity_of=identity_of, realpath=realpath)
        return identity in lineage(directory)

    def overlap(first: str, second: str) -> bool:
        return inside(first, second) or inside(second, first)

    for index, path in enumerate(related):
        if overlap(path, primary):
            message = _PRIMARY_OVERLAP.bind(folder=DisplayPath(path), primary=DisplayPath(primary))
            raise WorkspaceRegistryError(format_message(message), message)
        for other in related[:index]:
            if overlap(path, other):
                message = _REFERENCE_OVERLAP.bind(first=DisplayPath(other), second=DisplayPath(path))
                raise WorkspaceRegistryError(format_message(message), message)


def validate_workspace_folders(
    primary: str,
    folders: Sequence[str],
    previous: Workspace | None,
    *,
    workspaces: Sequence[Workspace] = (),
    probed: bool = False,
) -> tuple[str, tuple[str, ...]]:
    """Check the proposed folder set independently of its name; *probed* when :func:`_probe_folders` already ran."""
    if not probed:
        _probe_folders(primary, folders, new=previous is None)
    primary, related = _validate_folders(primary, folders)
    if previous is not None and primary != previous.primary_cwd:
        raise WorkspaceRegistryError("The primary folder cannot be changed.", PRIMARY_IMMUTABLE.bind())
    # Other definitions are compared by spelling: checking this workspace never reads another workspace's
    # folders, which may sit on a mount that stopped answering.
    key = os.path.normcase
    related_keys = {key(path) for path in related}
    for workspace in workspaces:
        if previous is not None and workspace.id == previous.id:
            continue
        if (
            key(workspace.primary_cwd) == key(primary)
            and {key(directory.path) for directory in workspace.working_dirs} == related_keys
        ):
            message = _SAME_FOLDERS.bind(name=workspace.name)
            raise WorkspaceRegistryError(format_message(message), message)
    return primary, related


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

    def _invalid_row(
        self, row: int, field: str, message: MessageDef, *, workspace_id: str | None = None, name: str | None = None
    ) -> WorkspaceRegistryError:
        path = DisplayPath(self.path)
        if workspace_id is not None:
            display = message.bind(path=path, row=row, field=field, id=workspace_id)
        elif name is not None:
            display = message.bind(path=path, row=row, field=field, name=name)
        else:
            display = message.bind(path=path, row=row, field=field)
        return WorkspaceRegistryError(format_message(display), display, code="workspace_registry_unreadable")

    def _read_folder(self, path: object, row: int, field: str) -> str:
        if not isinstance(path, str):
            raise self._invalid_row(row, field, _REGISTRY_FOLDER_INVALID)
        try:
            return _validate_folder(path)
        except WorkspaceRegistryError as error:
            raise self._invalid_row(row, field, _REGISTRY_FOLDER_INVALID) from error

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
        for row, item in enumerate(raw["workspaces"], start=1):
            if not isinstance(item, dict):
                raise self._invalid_row(row, "workspaces", _EXPECTED_OBJECT)
            workspace_id = item.get("id")
            if not isinstance(workspace_id, str) or not is_workspace_id(workspace_id):
                raise self._invalid_row(row, "id", _EXPECTED_WORKSPACE_ID)
            if workspace_id in used:
                raise self._invalid_row(row, "id", _WORKSPACE_ID_EXISTS, workspace_id=workspace_id)
            name = item.get("name")
            if not isinstance(name, str):
                raise self._invalid_row(row, "name", _EXPECTED_STRING)
            try:
                name = validate_workspace_name(name, tuple(workspaces), check_reserved=False)
            except WorkspaceNameError as error:
                reason = error.display_message.definition
                duplicate_name = name.strip() if reason == _NAME_EXISTS else None
                raise self._invalid_row(row, "name", _REGISTRY_NAME_ERRORS[reason], name=duplicate_name) from error
            primary = self._read_folder(item.get("primary_cwd"), row, "primary_cwd")
            folders = item.get("working_dirs")
            if not isinstance(folders, list):
                raise self._invalid_row(row, "working_dirs", _EXPECTED_LIST)
            related = [self._read_folder(path, row, f"working_dirs[{index}]") for index, path in enumerate(folders)]
            used_at = item.get("last_used_at", "")
            if not isinstance(used_at, str):
                raise self._invalid_row(row, "last_used_at", _EXPECTED_STRING)
            used[workspace_id] = used_at
            workspaces.append(_definition(workspace_id, name, primary, related))
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
                error=error if isinstance(error, WorkspaceRegistryError) else _registry_unreadable(self.path)
            )

    @staticmethod
    def _validate_proposed(
        name: str,
        primary: str,
        folders: Sequence[str],
        *,
        workspaces: tuple[Workspace, ...],
        previous: Workspace | None,
        probed: bool = False,
    ) -> tuple[str, str, tuple[str, ...]]:
        name = validate_workspace_name(name, workspaces, previous.id if previous is not None else None)
        primary, related = validate_workspace_folders(primary, folders, previous, workspaces=workspaces, probed=probed)
        return name, primary, related

    @property
    def _lock_path(self) -> Path:
        return self.path.with_suffix(".json.lock")

    def _mutate[T](
        self, change: Callable[[builtins.list[Workspace], dict[str, str], dict[str, str]], T], *, try_lock: bool = False
    ) -> T:
        """Commit definitions and membership in one locked read-merge-write."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with ExitStack() as stack:
                try:
                    stack.enter_context(FileLock(self._lock_path, timeout=0 if try_lock else _LOCK_TIMEOUT))
                except TimeoutError as error:
                    if try_lock:
                        raise _RegistryLockBusy(error) from error
                    raise
                try:
                    snapshot = self._read()
                    entries = list(snapshot.workspaces)
                    sessions = dict(snapshot.sessions)
                    used = dict(snapshot.last_used_at)
                except WorkspaceRegistryError:
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
            valid_name, valid_primary, related = self._validate_proposed(
                name, primary, folders, workspaces=tuple(entries), previous=None, probed=True
            )
            workspace = _definition(WORKSPACE_ID_PREFIX + uuid4().hex, valid_name, valid_primary, related)
            entries.append(workspace)
            used[workspace.id] = ""
            return workspace

        _probe_folders(primary, folders, new=True)
        return self._mutate(change)

    @staticmethod
    def check_folders(primary: str, folders: Sequence[str]) -> None:
        """Run an edit's folder checks that read the filesystem, outside the registry lock."""
        _probe_folders(primary, folders, new=False)

    def preview_update(
        self, snapshot: RegistrySnapshot, workspace: Workspace, name: str, folders: Sequence[str]
    ) -> Workspace:
        """Validate an edit before building the runtime that would use it."""
        if snapshot.error is not None:
            raise snapshot.error
        _check_expected_workspace(snapshot.workspace(workspace.id), workspace)
        valid_name, primary, related = self._validate_proposed(
            name, workspace.primary_cwd, folders, workspaces=snapshot.workspaces, previous=workspace
        )
        return _definition(workspace.id, valid_name, primary, related)

    def update(
        self,
        workspace_id: str,
        *,
        name: str,
        primary: str,
        folders: Sequence[str] = (),
        expected: Workspace | None = None,
        try_lock: bool = False,
        folders_checked: bool = False,
    ) -> Workspace:
        """Replace a definition; *folders_checked* when :meth:`check_folders` already ran for these folders."""

        def proposed(workspaces: tuple[Workspace, ...], previous: Workspace) -> Workspace:
            valid_name, valid_primary, related = self._validate_proposed(
                name, primary, folders, workspaces=workspaces, previous=previous, probed=True
            )
            return _definition(workspace_id, valid_name, valid_primary, related)

        def change(entries: list[Workspace], _sessions: dict[str, str], _used: dict[str, str]) -> Workspace:
            for index, previous in enumerate(entries):
                if previous.id == workspace_id:
                    _check_expected_workspace(previous, expected)
                    workspace = proposed(tuple(entries), previous)
                    entries[index] = workspace
                    return workspace
            _check_expected_workspace(None, expected)
            raise workspace_not_found(workspace_id)

        if not folders_checked:
            self.check_folders(primary, folders)
        return self._mutate(change, try_lock=try_lock)

    def delete(
        self,
        workspace_id: str,
        *,
        expected: Workspace | None = None,
        clear_session_id: str | None = None,
        try_lock: bool = False,
    ) -> None:
        clear_key = session_id_key(clear_session_id) if clear_session_id is not None else None

        def change(entries: list[Workspace], sessions: dict[str, str], used: dict[str, str]) -> None:
            for index, workspace in enumerate(entries):
                if workspace.id == workspace_id:
                    _check_expected_workspace(workspace, expected)
                    del entries[index]
                    used.pop(workspace_id, None)
                    for session_id in [
                        session_id
                        for session_id, record in sessions.items()
                        if record == workspace_id or (clear_key is not None and session_id_key(session_id) == clear_key)
                    ]:
                        del sessions[session_id]
                    return
            _check_expected_workspace(None, expected)
            raise workspace_not_found(workspace_id)

        self._mutate(change, try_lock=try_lock)

    def set_session_workspace(
        self,
        session_id: str,
        workspace_id: str | None,
        *,
        if_absent: bool = False,
        leaving: str | None = None,
        try_lock: bool = False,
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

        return self._mutate(change, try_lock=try_lock)

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
            self._mutate(change, try_lock=True)
        except _RegistryLockBusy, OSError, ValueError:
            logger.debug("Could not record workspace use for %s", workspace_id, exc_info=True)
