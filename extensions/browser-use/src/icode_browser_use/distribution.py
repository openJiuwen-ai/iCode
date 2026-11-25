# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""How this extension is installed, and what that install can start.

The extension reaches iCode one of two ways: an installed distribution's
``chrys.extensions`` entry point (the wheel), or a folder package at
``<config_dir>/extensions/browser/`` that the host executes from a file. The
host keeps a source only while it is the single source of an id, so a wheel and
a folder package for ``browser`` cancel each other out: this module reports that
pair and refuses to create it. Both folder layouts (``browser.py`` and
``browser/__init__.py``) count as sources too; they also starve each other.

The wheel side is only visible to the interpreter that runs this code. A
distribution installed into iCode's own environment cannot be seen from another
Python, so installing a folder package from a system Python is the one path that
can still produce a duplicate this module cannot detect.

``config_dir`` follows the host: ``~/.chrys`` on macOS/Linux, ``%APPDATA%/chrys``
on Windows. ``install_pack`` and ``uninstall_pack`` touch only the extension's
own folder. Browser profiles, logins and action journals live in the state
directory (``ICODE_BROWSER_STATE_DIR``, default ``~/.cache/icode-browser-use``) and
are never removed here.

A folder package runs the Owned and Attach backends. Bridge and Broker need
executables that only an installed wheel provides, so ``broker_command`` returns
``None`` instead of spawning a command that cannot work.
"""

from __future__ import annotations

import importlib.metadata
import os
import re
import shutil
import stat
import sys
from pathlib import Path

ENTRY_POINT_GROUP = "chrys.extensions"
EXTENSION_ID = "browser"

_FOLDER_MODULE = "_chrys_extension_"
_PYTHON_EXECUTABLE = re.compile(r"python(\d+(\.\d+)*)?(\.exe)?$")


def host_config_dir() -> Path:
    """The host's configuration folder (``~/.chrys``, ``%APPDATA%/chrys`` on Windows)."""
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming") / "chrys"
    return Path.home() / ".chrys"


def extensions_dir(config_dir: Path | None = None) -> Path:
    """The folder the host reads user extension files from."""
    return (Path(config_dir) if config_dir is not None else host_config_dir()) / "extensions"


def folder_sources(config_dir: Path | None = None) -> list[Path]:
    """Every folder/file source the host would read for this id (normally zero or one)."""
    root = extensions_dir(config_dir)
    return [path for path in (root / f"{EXTENSION_ID}.py", root / EXTENSION_ID / "__init__.py") if path.is_file()]


def folder_pack(config_dir: Path | None = None) -> Path | None:
    """The folder package the host would load for this id, or ``None``."""
    sources = folder_sources(config_dir)
    return sources[0] if sources else None


def wheel_entry_point() -> importlib.metadata.EntryPoint | None:
    """The installed distribution's entry point for this id, or ``None``."""
    for entry_point in importlib.metadata.entry_points(group=ENTRY_POINT_GROUP):
        if entry_point.name == EXTENSION_ID:
            return entry_point
    return None


def conflict(config_dir: Path | None = None) -> tuple[Path, str] | None:
    """``(folder, distribution label)`` when one id is installed twice."""
    folder = folder_pack(config_dir)
    entry_point = wheel_entry_point()
    if folder is None or entry_point is None:
        return None
    return folder, distribution_label(entry_point)


def duplicate_folders(config_dir: Path | None = None) -> list[Path]:
    """Both host layouts present: the host then loads neither, exactly like a wheel conflict."""
    sources = folder_sources(config_dir)
    return sources if len(sources) > 1 else []


def loaded_from(module_name: str) -> str:
    """``folder`` when the host executed a folder package, else ``distribution``."""
    return "folder" if module_name.startswith(_FOLDER_MODULE) else "distribution"


def broker_command(*, module_name: str | None = None, executable: str | None = None) -> list[str] | None:
    """The argv that starts the Broker here, or ``None`` when this runtime cannot.

    ``python -m icode_browser_use.broker`` only works when the package is
    importable under its real name and ``sys.executable`` is a Python
    interpreter. A folder package loads as ``_chrys_extension_browser.…``, and a
    bundled iCode runtime's ``sys.executable`` is the app binary, not Python: in
    both cases the Broker must be started outside the plugin.
    """
    name = __name__ if module_name is None else module_name
    if name != "icode_browser_use" and not name.startswith("icode_browser_use."):
        return None
    python = sys.executable if executable is None else executable
    if not _PYTHON_EXECUTABLE.fullmatch(Path(python).name.lower()):
        return None
    return [python, "-m", "icode_browser_use.broker"]


def _is_link_or_junction(path: Path) -> bool:
    """True when *path* is a symlink or Windows NTFS junction (must never be resolved & deleted)."""
    if path.is_symlink():
        return True
    is_junction = getattr(os.path, "isjunction", None)
    if callable(is_junction):
        try:
            if is_junction(path):
                return True
        except OSError:
            pass
    if sys.platform == "win32":
        try:
            st = os.lstat(path)
            reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x0400)
            if getattr(st, "st_file_attributes", 0) & reparse:
                return True
        except OSError:
            pass
    return False


def install_pack(source: Path, *, config_dir: Path | None = None, replace: bool = False) -> Path:
    """Copy a folder package to ``<config_dir>/extensions/browser``; return the target.

    Refusing a second source for this id is limited to what this interpreter can
    see: the entry point of a distribution installed into *another* environment
    (for example iCode's own interpreter) is invisible from here.
    """
    source = Path(source).resolve()
    if not (source / "__init__.py").is_file() or not (source / "icode_browser_use" / "__init__.py").is_file():
        raise ValueError(f"{source} is not a browser extension folder package")
    entry_point = wheel_entry_point()
    if entry_point is not None:
        raise ValueError(
            f"extension id {EXTENSION_ID!r} is already installed as {distribution_label(entry_point)}; the host would load "
            "neither copy. Uninstall the distribution first (for example: uv pip uninstall icode-browser-use)"
        )
    others = [path for path in folder_sources(config_dir) if path.name == f"{EXTENSION_ID}.py"]
    if others:
        raise ValueError(
            f"{others[0]} already provides extension id {EXTENSION_ID!r} as a single file; the host would load neither "
            "copy. Remove it first (icode-browser-use uninstall)"
        )
    target_raw = extensions_dir(config_dir) / EXTENSION_ID
    if _is_link_or_junction(target_raw):
        raise ValueError(
            f"refusing to replace symlink/junction {target_raw}; remove the link manually before installing"
        )
    target = target_raw.resolve()
    if source == target:
        raise ValueError(f"source and target must be different directories: {source}")
    if target.exists() and not replace:
        raise ValueError(f"{target} already exists; pass --replace to overwrite it")
    if target.exists() and not (target / "__init__.py").is_file():
        raise ValueError(f"refusing to replace {target}: it does not look like an installed extension folder")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{EXTENSION_ID}.{os.getpid()}.tmp")
    backup = target.with_name(f".{EXTENSION_ID}.{os.getpid()}.bak")
    if temporary.exists():
        shutil.rmtree(temporary)
    if backup.exists():
        shutil.rmtree(backup)
    shutil.copytree(source, temporary, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    moved_backup = False
    try:
        if target.exists():
            target.rename(backup)
            moved_backup = True
        try:
            os.replace(temporary, target)
        except OSError:
            if moved_backup and backup.exists():
                try:
                    backup.rename(target)
                    moved_backup = False
                except OSError:
                    pass
            raise
    finally:
        if temporary.exists():
            shutil.rmtree(temporary, ignore_errors=True)
        if moved_backup and backup.exists():
            shutil.rmtree(backup, ignore_errors=True)
    return target


def uninstall_pack(*, config_dir: Path | None = None) -> list[Path]:
    """Remove every installed source of this id; return what was removed.

    Only the extension's own folder or file is touched: browser profiles, logins
    and action journals live in the state directory and always stay.
    """
    removed: list[Path] = []
    root = extensions_dir(config_dir)
    for candidate in (root / EXTENSION_ID, root / f"{EXTENSION_ID}.py"):
        if candidate.is_dir():
            shutil.rmtree(candidate)
            removed.append(candidate)
        elif candidate.is_file():
            candidate.unlink()
            removed.append(candidate)
    return removed


def distribution_label(entry_point: importlib.metadata.EntryPoint) -> str:
    dist = entry_point.dist
    if dist is None:
        return entry_point.value
    return f"{dist.name} {dist.version}".strip()


def distribution_version(entry_point: importlib.metadata.EntryPoint | None = None) -> str | None:
    """The installed distribution's version for this id, or None when no wheel provides it."""
    entry = wheel_entry_point() if entry_point is None else entry_point
    if entry is None or entry.dist is None:
        return None
    return entry.dist.version or None
