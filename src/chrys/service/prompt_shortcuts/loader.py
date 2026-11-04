# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Discover prompt shortcut files and parse their frontmatter.

The file format is opencode's for custom commands, so a file copied from
``.opencode/commands`` into a ``shortcuts`` folder works unchanged:

* the command name is the path relative to the ``shortcuts`` directory without
  ``.md``, with ``/`` between directories (``git/commit-msg.md`` is
  ``/git/commit-msg``);
* optional YAML frontmatter carries ``description``, ``agent`` and
  ``model``; other keys (opencode's ``subtask``, Claude Code's
  ``argument-hint``) are ignored;
* the body, stripped, is the template.

A file that cannot be read or parsed is skipped with a warning; it never stops
the others from loading.
"""

from __future__ import annotations

import logging
import os
import stat as stat_mod
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from chrys.foundation.platform import get_platform
from chrys.foundation.text.encoding import decode_bytes
from chrys.service.skills.loader import FRONTMATTER_RE, parse_frontmatter

logger = logging.getLogger(__name__)

SHORTCUTS_DIR_NAME = "shortcuts"
"""The directory under the config dir or a project's ``.chrys`` dir holding prompt shortcuts."""

PROJECT_CONFIG_DIR_NAME = ".chrys"

MAX_COMMAND_FILE_BYTES = 256 * 1024
"""Larger files are skipped: a prompt this long is almost certainly not one."""

MAX_COMMAND_FILES = 500
"""Files read per directory; a directory linked to somewhere large stops here."""

MAX_WALKED_DIRS = 200
"""Folders looked into per directory, its own included."""

MAX_WALKED_ENTRIES = 2000
"""Entries listed per directory, files and folders alike: a folder linked to ``~`` or ``/usr`` stops here."""

_MARKDOWN_SUFFIX = ".md"

_PARSED: dict[tuple[str, str, str], tuple[tuple[int, int], PromptShortcut | None]] = {}
"""Parsed files by (path, name, source kind), with the (mtime_ns, size) they were parsed at.

The name is part of the key: one file read from two folders, one inside the
other, has a different name in each.
"""
_WARNED_DUPLICATES: set[tuple[str, str]] = set()
"""``(skipped, kept)`` file pairs already logged."""
_WARNED_OUTSIDE: set[str] = set()
"""Working-folder shortcut folders and files leading out of their repository, already logged."""
_CACHE_LOCK = threading.Lock()
"""Loads run off the event loop and may overlap; each prunes both caches to what it read."""

type _SourceKind = Literal["project", "global"]
"""Where a folder comes from: a project's ``.chrys``, or the user's own folder."""


@dataclass(frozen=True, slots=True)
class PromptShortcut:
    """One prompt shortcut file."""

    name: str
    """Name typed after ``/``: the relative path without ``.md``, ``/``-separated."""
    template: str
    """The body, stripped of surrounding whitespace."""
    path: str
    """Raw filesystem path of the file; unique among loaded shortcuts."""
    description: str = ""
    agent: str = ""
    model: str = ""


def global_shortcuts_dir(config_dir: Path) -> Path:
    """Return the user's prompt shortcut directory under *config_dir*."""
    return config_dir / SHORTCUTS_DIR_NAME


def project_shortcuts_dir(root: Path) -> Path:
    """Return the prompt shortcut directory of the project at *root*."""
    return root / PROJECT_CONFIG_DIR_NAME / SHORTCUTS_DIR_NAME


def project_roots(workspace: str) -> list[Path]:
    """Return *workspace* and its parents up to the enclosing Git repository's top, nearest first.

    Outside a repository only *workspace* itself is a project root. A ``.git``
    file (worktree, submodule) marks a top as a ``.git`` directory does.
    """
    start = Path(workspace)
    chain: list[Path] = []
    for folder in (start, *start.parents):
        chain.append(folder)
        if (folder / ".git").exists():
            return chain
    return [start]


def prompt_shortcut_folders(workspace: str | None) -> dict[str, Any]:
    """Where shortcuts are read from for *workspace*: :func:`load_prompt_shortcuts`'s arguments.

    The user's folder and the working folder, always: which folders are read is
    not a setting. The one definition both readers use: the ``/`` list, and the
    engine when it checks that a message's shortcut is still one it would load.
    """
    return {"config_dir": get_platform().config_dir, "workspace": workspace}


def load_prompt_shortcuts(
    *,
    config_dir: Path,
    workspace: str | None,
) -> list[PromptShortcut]:
    """Load prompt shortcuts, each name once, sorted by name (case-insensitively).

    See :func:`prompt_shortcut_sources` for where they are read from and which
    of several with one name is kept.
    """
    commands = [
        command
        for _folder, found in prompt_shortcut_sources(config_dir=config_dir, workspace=workspace)
        for command in found
    ]
    # The list shows shortcuts in this order; where a file came from is not shown, so it is no key.
    return sorted(commands, key=lambda command: (command.name.casefold(), command.name))


def prompt_shortcut_sources(
    *,
    config_dir: Path,
    workspace: str | None,
) -> list[tuple[Path, list[PromptShortcut]]]:
    """Load prompt shortcuts by the folder they came from, folders in reading order.

    Each folder lists the shortcuts kept from it, sorted by name; a folder that
    gave none is left out.

    Folders are read in this order: the user folder (``<config_dir>/shortcuts``),
    then the working folder: ``.chrys/shortcuts`` of *workspace* and its
    parents up to the Git repository's top, nearest first. Each holds ``*.md``
    files, its subfolders included (``git/commit-msg.md`` is ``/git/commit-msg``).

    Like skills, the first shortcut loaded with a name (compared
    case-insensitively) is kept and later ones are skipped with a warning, so
    the user's own shortcuts win over a project's and a nearer project folder
    wins over a farther one. A folder is read at most once, and the user
    folder is never read as a project folder (a workspace at the user's home
    on macOS/Linux).
    """
    user_dirs = [global_shortcuts_dir(config_dir)]
    sources: list[tuple[list[Path], _SourceKind]] = [(user_dirs, "global")]
    # A project folder reads nothing a link leads out of its repository: the folder itself
    # (``working_folder_dirs``) and each linked file in it (``_load_file``).
    project_top: Path | None = None
    if workspace:
        project_top = project_roots(workspace)[-1]
        sources.extend(([directory], "project") for directory in working_folder_dirs(workspace))
    seen_dirs: list[Path] = []
    kept: dict[str, PromptShortcut] = {}
    by_folder: dict[Path, list[PromptShortcut]] = {}
    read: set[tuple[str, str, str]] = set()
    duplicates: set[tuple[str, str]] = set()
    for dirs, scope in sources:
        for directory in dirs:
            skip = (*seen_dirs, *user_dirs) if scope == "project" else seen_dirs
            if not directory.is_dir() or any(_same_directory(directory, other) for other in skip):
                continue
            seen_dirs.append(directory)
            inside = project_top if scope == "project" else None
            for command in _load_directory(directory, scope, read, inside):
                key = command.name.casefold()
                if key in kept:
                    # The list reloads each time it opens: say so once per pair, not on every open.
                    pair = (command.path, kept[key].path)
                    duplicates.add(pair)
                    with _CACHE_LOCK:
                        warn = pair not in _WARNED_DUPLICATES
                        _WARNED_DUPLICATES.add(pair)
                    if warn:
                        logger.warning(
                            "Prompt shortcuts: skipping %s: /%s is already defined by %s",
                            command.path,
                            command.name,
                            kept[key].path,
                        )
                    continue
                kept[key] = command
                by_folder.setdefault(directory, []).append(command)
    # Keep only what this load read, so files and folders left behind are not held for the whole run.
    with _CACHE_LOCK:
        for stale in [key for key in _PARSED if key not in read]:
            del _PARSED[stale]
        _WARNED_DUPLICATES.intersection_update(duplicates)
    return [
        (folder, sorted(found, key=lambda command: (command.name.casefold(), command.name)))
        for folder, found in by_folder.items()
    ]


def working_folder_dirs(workspace: str) -> list[Path]:
    """Return the working-folder shortcut directories that exist for *workspace*, nearest first.

    A directory that a link leads out of the repository (or, outside one, out
    of *workspace*) is left out with a warning: a repository decides what is in
    it, not what is read elsewhere on the machine. The user's own folders can
    point anywhere.
    """
    roots = project_roots(workspace)
    found: list[Path] = []
    for root in roots:
        directory = project_shortcuts_dir(root)
        if directory.is_dir() and _inside(directory, roots[-1]):
            found.append(directory)
    return found


def _inside(path: Path, top: Path) -> bool:
    """Whether *path*, every link in it followed, is under *top*, its links followed too; warns once when not."""
    try:
        inside = path.resolve().is_relative_to(top.resolve())
    except OSError, RuntimeError:
        inside = False
    if not inside:
        key = os.fspath(path)
        with _CACHE_LOCK:
            warn = key not in _WARNED_OUTSIDE
            _WARNED_OUTSIDE.add(key)
        if warn:
            logger.warning(
                "Prompt shortcuts: skipping %s: it leads out of %s",
                key,
                os.fspath(top),
            )
    return inside


def _same_directory(left: Path, right: Path) -> bool:
    try:
        return os.path.samefile(left, right)
    except OSError:
        return False


def _load_directory(
    directory: Path, scope: _SourceKind, read: set[tuple[str, str, str]], inside: Path | None
) -> list[PromptShortcut]:
    """Load *directory*'s shortcuts; with *inside*, a linked file leading out of that folder is skipped."""
    commands: list[PromptShortcut] = []
    for relative in _markdown_files(directory):
        command = _load_file(directory, relative, scope, read, inside)
        if command is not None:
            commands.append(command)
    return commands


def _markdown_files(directory: Path) -> list[str]:
    """Return ``/``-separated relative paths of visible ``.md`` files, in name order, folder by folder.

    Links and junctions below *directory* are not followed. The walk stops at
    :data:`MAX_COMMAND_FILES` files, :data:`MAX_WALKED_DIRS` folders or
    :data:`MAX_WALKED_ENTRIES` listed entries, whichever comes first.
    """
    found: list[str] = []
    pending = [""]
    walked = listed = 0
    while pending:
        relative_dir = pending.pop()
        walked += 1
        if walked > MAX_WALKED_DIRS:
            _warn_stopped(directory, f"{MAX_WALKED_DIRS} folders")
            break
        entries: list[os.DirEntry[str]] = []
        try:
            with os.scandir(directory / relative_dir if relative_dir else directory) as listing:
                for entry in listing:
                    listed += 1
                    if listed > MAX_WALKED_ENTRIES:
                        _warn_stopped(directory, f"{MAX_WALKED_ENTRIES} entries")
                        return found
                    entries.append(entry)
        except OSError as exc:
            logger.warning("Prompt shortcuts: cannot list %s: %s", os.fspath(directory / relative_dir), exc)
            continue
        subdirs: list[str] = []
        for entry in sorted(entries, key=lambda entry: entry.name):
            if entry.name.startswith("."):
                continue
            relative = f"{relative_dir}/{entry.name}" if relative_dir else entry.name
            try:
                is_dir = entry.is_dir(follow_symlinks=False)
                # A Windows junction is a link to a folder, like a symlink: not followed.
                if is_dir and entry.is_junction():
                    continue
            except OSError:
                continue
            if is_dir:
                subdirs.append(relative)
            elif entry.name.endswith(_MARKDOWN_SUFFIX):
                found.append(relative)
                if len(found) >= MAX_COMMAND_FILES:
                    _warn_stopped(directory, f"{MAX_COMMAND_FILES} files")
                    return found
        # Depth first, in name order: a folder's files, then each subfolder in turn.
        pending.extend(reversed(subdirs))
    return found


def _warn_stopped(directory: Path, limit: str) -> None:
    logger.warning("Prompt shortcuts: stopped after %s in %s", limit, os.fspath(directory))


def command_name_from_path(relative: str) -> str | None:
    """Return the command name for a ``/``-separated relative ``.md`` path, or None if it cannot be typed."""
    name = relative[: -len(_MARKDOWN_SUFFIX)]
    segments = name.split("/")
    if any(not segment or segment.startswith(".") for segment in segments):
        return None
    if any(char.isspace() or not char.isprintable() for char in name):
        return None
    return name


def _load_file(
    directory: Path, relative: str, scope: _SourceKind, read: set[tuple[str, str, str]], inside: Path | None
) -> PromptShortcut | None:
    path = directory / relative
    name = command_name_from_path(relative)
    if name is None:
        logger.warning("Prompt shortcuts: skipping %s: its name cannot be typed after /", os.fspath(path))
        return None
    # Only the file itself can be a link here: the walk follows no linked folder below *directory*.
    if inside is not None and path.is_symlink() and not _inside(path, inside):
        return None
    try:
        stat = path.stat()
    except OSError as exc:
        logger.warning("Prompt shortcuts: cannot read %s: %s", os.fspath(path), exc)
        return None
    if not stat_mod.S_ISREG(stat.st_mode):
        # A device or pipe named *.md (``/dev/zero``, a FIFO) never ends or blocks the read.
        logger.warning("Prompt shortcuts: skipping %s: not a regular file", os.fspath(path))
        return None
    # The list rereads shortcuts each time it opens: reparse (and re-warn) only files that changed.
    key = (os.fspath(path), name, scope)
    read.add(key)
    version = (stat.st_mtime_ns, stat.st_size)
    with _CACHE_LOCK:
        cached = _PARSED.get(key)
    if cached is not None and cached[0] == version:
        return cached[1]
    try:
        command = _parse_file(path, name, size=stat.st_size)
    except Exception as exc:
        # One file that breaks the parser (a YAML date with no such day, say) is skipped alone,
        # and cached like any other skip, so it warns again only once it changes.
        logger.warning("Prompt shortcuts: skipping %s: %s", os.fspath(path), exc)
        command = None
    with _CACHE_LOCK:
        _PARSED[key] = (version, command)
    return command


def _parse_file(path: Path, name: str, *, size: int) -> PromptShortcut | None:
    if size > MAX_COMMAND_FILE_BYTES:
        logger.warning("Prompt shortcuts: skipping %s: larger than %d bytes", os.fspath(path), MAX_COMMAND_FILE_BYTES)
        return None
    try:
        raw = _read_regular_file(path)
    except OSError as exc:
        logger.warning("Prompt shortcuts: cannot read %s: %s", os.fspath(path), exc)
        return None
    if raw is None:
        logger.warning(
            "Prompt shortcuts: skipping %s: not a regular file of at most %d bytes",
            os.fspath(path),
            MAX_COMMAND_FILE_BYTES,
        )
        return None
    # One newline form, as skills read theirs: a file saved with Windows line endings fills the same prompt.
    content = decode_bytes(raw).replace("\r\n", "\n").replace("\r", "\n")
    fields: dict[str, object] = {}
    body = content.removeprefix("\ufeff")
    if body.startswith("---"):
        match = FRONTMATTER_RE.search(content)
        if match is None:
            logger.warning("Prompt shortcuts: skipping %s: frontmatter is not closed by a --- line", os.fspath(path))
            return None
        # Files written for opencode or Claude Code often hold YAML that only
        # tolerant parsing reads (``argument-hint: [a] [b]``); that is expected here.
        parsed, error = parse_frontmatter(content, file_name=os.fspath(path), fallback_log_level=logging.DEBUG)
        if parsed is None:
            logger.warning("Prompt shortcuts: skipping %s: %s", os.fspath(path), error)
            return None
        fields = parsed
        body = content[match.end() :]
    template = body.strip()
    if not template:
        logger.warning("Prompt shortcuts: skipping %s: the prompt is empty", os.fspath(path))
        return None
    return PromptShortcut(
        name=name,
        template=template,
        path=os.fspath(path),
        description=_text_field(fields, "description"),
        agent=_text_field(fields, "agent"),
        model=_text_field(fields, "model"),
    )


def _read_regular_file(path: Path) -> bytes | None:
    """Read *path* if it is still a regular file of at most :data:`MAX_COMMAND_FILE_BYTES`, else None.

    Checked again on the open file: the path may have been swapped for a pipe or
    device since it was listed. Opened non-blocking, so a FIFO cannot hold the open.
    """
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
    fd = os.open(path, flags)
    with os.fdopen(fd, "rb") as file:
        if not stat_mod.S_ISREG(os.fstat(file.fileno()).st_mode):
            return None
        data = file.read(MAX_COMMAND_FILE_BYTES + 1)
    return data if len(data) <= MAX_COMMAND_FILE_BYTES else None


def _text_field(fields: dict[str, object], key: str) -> str:
    """Return a scalar frontmatter field as single-line text; lists and mappings count as absent."""
    value = fields.get(key)
    if isinstance(value, bool) or value is None or isinstance(value, (list, dict)):
        return ""
    return " ".join(str(value).split())
