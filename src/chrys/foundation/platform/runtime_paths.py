# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Runtime executable path helpers.

PyApp-packaged Chrys runs from an embedded Python distribution.  On some
platforms that distribution's executable directories appear early on PATH,
causing child shells and subprocess tools to resolve ``python`` / ``uv`` to
Chrys's private runtime instead of the user's system install.  These helpers
demote those runtime directories only for packaged runs while preserving them
as a fallback.

On Windows the lookups also pass over the ``python`` / ``python3`` that Windows
itself puts on PATH while no Python is installed (see
:func:`is_windows_store_python_stub`).
"""

from __future__ import annotations

import ctypes
import ntpath
import os
import shutil
import struct
import sys
import sysconfig
from collections.abc import Iterator, Mapping, MutableMapping
from ctypes import wintypes
from functools import cache
from pathlib import Path
from typing import Any, cast

from chrys.foundation.platform.c_api import declare_functions

_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_WINDOWS_DEFAULT_PATHEXT = (".COM", ".EXE", ".BAT", ".CMD")
_PYTHON_HOME_OVERRIDE_KEYS = frozenset({"PYTHONHOME"})
_PYTHON_RUNTIME_OVERRIDE_KEYS = frozenset({"PYTHONHOME", "PYTHONPATH"})
_STORE_PYTHON_REDIRECTOR = "appinstallerpythonredirector.exe"
_IO_REPARSE_TAG_APPEXECLINK = 0x8000001B
_FSCTL_GET_REPARSE_POINT = 0x000900A8
_MAXIMUM_REPARSE_DATA_BUFFER_SIZE = 16 * 1024
_FILE_SHARE_READ_WRITE_DELETE = 0x00000007
_OPEN_EXISTING = 3
_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
_INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value


def is_frozen_runtime(env: Mapping[str, str] | None = None) -> bool:
    """Return whether this process is running from a packaged PyApp runtime."""
    source = os.environ if env is None else env
    forced = source.get("CHRYS_FORCE_FROZEN", "").strip().lower()
    return forced in _TRUE_VALUES or bool(source.get("PYAPP"))


def normalize_path(path: str | os.PathLike[str]) -> str:
    """Return an absolute path for comparison/display, or ``""`` for empty inputs."""
    raw = os.fspath(path)
    if not raw:
        return ""
    return os.path.abspath(os.path.expanduser(raw))


def same_path(left: str | os.PathLike[str], right: str | os.PathLike[str]) -> bool:
    """Compare paths using platform normalization."""
    left_norm = normalize_path(left)
    right_norm = normalize_path(right)
    if not left_norm or not right_norm:
        return left_norm == right_norm
    return os.path.normcase(os.path.realpath(left_norm)) == os.path.normcase(os.path.realpath(right_norm))


def runtime_executable_dirs() -> tuple[str, ...]:
    """Return executable/search directories associated with Chrys's Python runtime."""
    dirs: list[str] = []

    executable = normalize_path(sys.executable)
    if executable:
        _append_unique_path(dirs, str(Path(executable).parent))

    for raw_prefix in (sys.prefix, sys.exec_prefix):
        if raw_prefix:
            _append_unique_path(dirs, raw_prefix)

    scripts = sysconfig.get_path("scripts")
    if scripts:
        _append_unique_path(dirs, scripts)

    script_dir_name = "Scripts" if sys.platform == "win32" else "bin"
    for raw_prefix in (sys.prefix, sys.exec_prefix):
        if raw_prefix:
            _append_unique_path(dirs, str(Path(raw_prefix) / script_dir_name))
            if sys.platform == "win32":
                _append_unique_path(dirs, str(Path(raw_prefix) / "Library" / "bin"))

    return tuple(dirs)


def path_env_key(env: Mapping[str, str]) -> str | None:
    """Return the actual PATH key casing present in *env*."""
    if sys.platform == "win32":
        # Plain dicts can contain both Path and PATH after merges; mirror
        # Windows' effective last-writer-wins behavior for that degenerate case.
        keys = _path_env_keys(env)
        return keys[-1] if keys else None
    if "PATH" in env:
        return "PATH"
    keys = _path_env_keys(env)
    if keys:
        return keys[0]
    return None


def strip_python_runtime_overrides(
    env: MutableMapping[str, str],
    *,
    strip_pythonpath: bool = True,
) -> MutableMapping[str, str]:
    """Remove inherited Python runtime path overrides from a child environment.

    This is intentionally not gated on :func:`is_frozen_runtime`: a normal
    Chrys process may spawn a PyApp or standalone Python command whose CPython
    bootstrap would otherwise honor the parent's ``PYTHONHOME`` / ``PYTHONPATH``
    before the child application can defend itself.

    ``strip_pythonpath=False`` keeps user module search paths for managed
    commands that do not have their own explicit environment override.
    """
    blocked_keys = _PYTHON_RUNTIME_OVERRIDE_KEYS if strip_pythonpath else _PYTHON_HOME_OVERRIDE_KEYS
    for key in list(env):
        if key.upper() in blocked_keys:
            del env[key]
    return env


def reorder_path_demoting_runtime(env: MutableMapping[str, str]) -> MutableMapping[str, str]:
    """Move runtime PATH entries behind user/system entries for packaged runs.

    The operation is a stable partition: non-runtime entries keep their
    original relative order at the front; runtime entries keep their original
    relative order at the back.  Duplicate entries are preserved.
    """
    if not is_frozen_runtime(env):
        return env

    key = path_env_key(env)
    if key is None:
        return env
    _drop_duplicate_path_keys(env, keep=key)

    entries = _path_entries(env[key])
    if not entries:
        env[key] = ""
        return env

    runtime_dirs = runtime_executable_dirs()
    if not runtime_dirs:
        return env

    normal_entries: list[str] = []
    runtime_entries: list[str] = []
    for entry in entries:
        if _is_runtime_dir(entry, runtime_dirs):
            runtime_entries.append(entry)
        else:
            normal_entries.append(entry)

    env[key] = os.pathsep.join([*normal_entries, *runtime_entries])
    return env


def build_shell_subprocess_env() -> dict[str, str]:
    """Build the environment for a user's shell command whose output is captured, not shown on a terminal.

    The shell tool (its PTY and pipe paths) and a prompt shortcut's
    ``!`command``` placeholders share it: plain, uncoloured UTF-8 output, no
    pager, and the user's own tools ahead of the packaged runtime's.
    """
    env = os.environ.copy()
    env["TERM"] = "dumb"
    env["CHRYS"] = "1"
    env["NO_COLOR"] = "1"
    # Force UTF-8 output from Python subprocesses (and other tools
    # that respect these variables) so CJK text is never garbled.
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    # Prevent git (and other tools) from launching a pager, which
    # would block forever waiting for interactive input.
    env["GIT_PAGER"] = "cat"
    env["PAGER"] = "cat"
    # Suppress colour-forcing variables
    env.pop("FORCE_COLOR", None)
    env.pop("COLORTERM", None)
    env.pop("CLICOLOR_FORCE", None)
    # Shell commands intentionally mirror the user's environment more closely
    # than managed hook/skill/MCP subprocesses, so keep PYTHONHOME/PYTHONPATH.
    reorder_path_demoting_runtime(env)
    return env


def which_excluding_runtime(
    name: str,
    *,
    env: Mapping[str, str] | None = None,
    frozen_only: bool = True,
    fallback_to_runtime: bool = True,
    exclude_paths: tuple[str, ...] = (),
) -> str:
    """Resolve *name* from PATH, preferring non-runtime directories.

    By default the runtime-dir preference only activates for packaged PyApp
    runs.  ``fallback_to_runtime`` keeps Chrys's embedded tools available when
    the system does not provide the requested executable.
    """
    source = os.environ if env is None else env
    key = path_env_key(source)
    if key is None:
        found = normalize_path(shutil.which(name) or "") if env is None else ""
        return "" if not found or is_windows_store_python_stub(found) else found

    entries = _path_entries(source[key])
    if not entries:
        return ""

    should_exclude = not frozen_only or is_frozen_runtime(source)
    if not should_exclude:
        return _which_on_path_entries(name, entries, env=source, exclude_paths=exclude_paths)

    runtime_dirs = runtime_executable_dirs()
    non_runtime_entries = [entry for entry in entries if not _is_runtime_dir(entry, runtime_dirs)]
    found = _which_on_path_entries(name, non_runtime_entries, env=source, exclude_paths=exclude_paths)
    if found or not fallback_to_runtime:
        return found
    return _which_on_path_entries(name, entries, env=source, exclude_paths=exclude_paths)


def path_matches(name: str) -> Iterator[str]:
    """Every program *name* resolves to on this process's PATH, in search order, each looked up only once reached."""
    key = path_env_key(os.environ)
    if key is None:
        found = which_excluding_runtime(name)
        return iter([found] if found else [])
    return _path_matches_on_entries(name, _path_entries(os.environ[key]), env=os.environ)


def is_windows_store_python_stub(path: str) -> bool:
    """Whether *path* is what Windows puts on PATH as ``python`` / ``python3`` while no Python is installed.

    That App Execution Alias runs App Installer's redirector, which only says where to get Python from the Microsoft
    Store and exits 9009. The alias of a Python actually installed from the Store starts that Python, and is kept.
    """
    return sys.platform == "win32" and _names_store_python_redirector(_app_execution_alias_data(path))


def is_app_execution_alias(path: str) -> bool:
    """Whether *path* is a Windows App Execution Alias: the empty file through which a packaged app is started."""
    if sys.platform != "win32":
        return False
    try:
        return os.lstat(path).st_reparse_tag == _IO_REPARSE_TAG_APPEXECLINK
    except OSError:
        return False


def _app_execution_alias_data(path: str) -> bytes:
    """The reparse data of the App Execution Alias at *path*; ``b""`` for anything else."""
    # Only an alias is opened: an ordinary program costs one lstat.
    return _read_reparse_point(path) if is_app_execution_alias(path) else b""


def _read_reparse_point(path: str) -> bytes:
    """The REPARSE_DATA_BUFFER of *path* itself, not of what it names; ``b""`` when it cannot be read."""
    if sys.platform != "win32":
        return b""
    kernel32 = _windows_kernel32()
    handle = kernel32.CreateFileW(
        path,
        0,
        _FILE_SHARE_READ_WRITE_DELETE,
        None,
        _OPEN_EXISTING,
        _FILE_FLAG_OPEN_REPARSE_POINT | _FILE_FLAG_BACKUP_SEMANTICS,
        None,
    )
    if handle == _INVALID_HANDLE_VALUE:
        return b""
    try:
        buffer = ctypes.create_string_buffer(_MAXIMUM_REPARSE_DATA_BUFFER_SIZE)
        returned = wintypes.DWORD()
        if not kernel32.DeviceIoControl(
            handle, _FSCTL_GET_REPARSE_POINT, None, 0, buffer, len(buffer), ctypes.byref(returned), None
        ):
            return b""
        return buffer.raw[: returned.value]
    finally:
        kernel32.CloseHandle(handle)


@cache
def _windows_kernel32() -> Any:
    """The kernel32 calls that read a reparse point."""
    kernel32 = cast(Any, ctypes).WinDLL("kernel32", use_last_error=True)
    declare_functions(
        kernel32,
        {
            "CreateFileW": (
                wintypes.HANDLE,
                [
                    wintypes.LPCWSTR,
                    wintypes.DWORD,
                    wintypes.DWORD,
                    wintypes.LPVOID,
                    wintypes.DWORD,
                    wintypes.DWORD,
                    wintypes.HANDLE,
                ],
            ),
            "DeviceIoControl": (
                wintypes.BOOL,
                [
                    wintypes.HANDLE,
                    wintypes.DWORD,
                    wintypes.LPVOID,
                    wintypes.DWORD,
                    wintypes.LPVOID,
                    wintypes.DWORD,
                    ctypes.POINTER(wintypes.DWORD),
                    wintypes.LPVOID,
                ],
            ),
            "CloseHandle": (wintypes.BOOL, [wintypes.HANDLE]),
        },
    )
    return kernel32


def _names_store_python_redirector(reparse: bytes) -> bool:
    """Whether *reparse*, a REPARSE_DATA_BUFFER, is an alias that runs App Installer's Python redirector.

    An alias's data is undocumented: a four-byte prefix, then NUL-terminated UTF-16 strings (its package, its app, the
    program it starts, ...). Data of any other shape is not taken for the redirector.
    """
    if len(reparse) < 8:
        return False
    tag, length = struct.unpack_from("<IH", reparse)
    payload = reparse[8 : 8 + length]
    if tag != _IO_REPARSE_TAG_APPEXECLINK or len(payload) != length or length < 4 or length % 2:
        return False
    try:
        text = payload[4:].decode("utf-16-le")
    except UnicodeDecodeError:
        return False
    # What follows the last NUL, if anything, is no complete string.
    return any(ntpath.basename(value).casefold() == _STORE_PYTHON_REDIRECTOR for value in text.split("\0")[:-1])


def _append_unique_path(paths: list[str], new_path: str | os.PathLike[str]) -> None:
    normalized = normalize_path(new_path)
    if not normalized:
        return
    if any(same_path(normalized, existing) for existing in paths):
        return
    paths.append(normalized)


def _path_entries(path_value: str) -> list[str]:
    return [entry for entry in path_value.split(os.pathsep) if entry]


def _path_env_keys(env: Mapping[str, str]) -> list[str]:
    return [key for key in env if key.upper() == "PATH"]


def _drop_duplicate_path_keys(env: MutableMapping[str, str], *, keep: str) -> None:
    if sys.platform != "win32":
        return
    for key in list(env):
        if key != keep and key.upper() == "PATH":
            del env[key]


def _is_runtime_dir(path: str, runtime_dirs: tuple[str, ...]) -> bool:
    return any(same_path(path, runtime_dir) for runtime_dir in runtime_dirs)


def _which_on_path_entries(
    name: str,
    entries: list[str],
    *,
    env: Mapping[str, str],
    exclude_paths: tuple[str, ...],
) -> str:
    return next(_path_matches_on_entries(name, entries, env=env, exclude_paths=exclude_paths), "")


def _path_matches_on_entries(
    name: str,
    entries: list[str],
    *,
    env: Mapping[str, str],
    exclude_paths: tuple[str, ...] = (),
) -> Iterator[str]:
    for raw_dir in entries:
        directory = normalize_path(raw_dir)
        if not directory:
            continue
        path = _which_in_directory(name, directory, env=env)
        if not path:
            continue
        if any(same_path(path, excluded_path) for excluded_path in exclude_paths if excluded_path):
            continue
        yield path


def _which_in_directory(name: str, directory: str, *, env: Mapping[str, str]) -> str:
    if sys.platform != "win32":
        return normalize_path(shutil.which(name, path=directory) or "")

    directory_entries = _windows_directory_entries(Path(directory))
    for candidate_name in _windows_candidate_names(name, env):
        path = directory_entries.get(candidate_name.casefold()) or normalize_path(Path(directory) / candidate_name)
        if _access_check(path) and not is_windows_store_python_stub(path):
            return path
    return ""


def _windows_directory_entries(directory: Path) -> dict[str, str]:
    """Return child paths keyed case-insensitively, preserving on-disk casing."""
    try:
        return {child.name.casefold(): normalize_path(child) for child in directory.iterdir()}
    except OSError:
        return {}


def _windows_candidate_names(name: str, env: Mapping[str, str]) -> tuple[str, ...]:
    pathext = _windows_pathext(env)
    suffix = Path(name).suffix
    names: list[str] = []
    if suffix and any(suffix.upper() == ext.upper() for ext in pathext):
        names.append(name)
    names.extend(f"{name}{ext}" for ext in pathext)
    return tuple(dict.fromkeys(names))


def _windows_pathext(env: Mapping[str, str]) -> tuple[str, ...]:
    raw = _case_insensitive_env_value(env, "PATHEXT")
    if raw is None:
        return _WINDOWS_DEFAULT_PATHEXT
    exts = tuple(ext.rstrip(".") for ext in raw.split(";") if ext)
    return exts or _WINDOWS_DEFAULT_PATHEXT


def _case_insensitive_env_value(env: Mapping[str, str], key: str) -> str | None:
    for env_key, value in env.items():
        if env_key.upper() == key.upper():
            return value
    return None


def _access_check(path: str) -> bool:
    return os.path.exists(path) and os.access(path, os.F_OK | os.X_OK) and not os.path.isdir(path)
