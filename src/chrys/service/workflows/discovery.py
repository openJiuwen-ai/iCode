# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Where workflows live and how their bytes are read once.

Three sources, highest precedence first: the working directory (``<cwd>/.chrys/workflows``),
the user's global directory (``<config_dir>/workflows``), and the builtin
templates shipped inside chrys. A workflow is a file ``<id>.py`` or, in the
working directory and global directories, a folder ``<id>/`` whose entry is
``<id>/<id>.py``. For one id the first readable candidate in ``PRECEDENCE``
wins; one that can't be read is reported and shadows nothing. User files are
read owner-verified (no links, no foreign owners) and those bytes are the ones
every later step hashes, confirms and feeds to the worker. A folder's
confirmation covers every file in it except hidden entries and Python's
bytecode caches in ``__pycache__`` folders, through ``WorkflowPackage.digest``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import stat
import sys
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Literal

from chrys.foundation.models.workdir_migration import SOURCE_KIND_WORKDIR as SOURCE_KIND_WORKDIR
from chrys.foundation.models.workflow_session import WorkflowIdentity
from chrys.foundation.platform.files import secure_open_owner_verified_binary

logger = logging.getLogger(__name__)

SOURCE_KIND_BUILTIN: Final = "builtin"
SOURCE_KIND_GLOBAL: Final = "global"
SOURCE_KINDS: Final = (SOURCE_KIND_BUILTIN, SOURCE_KIND_GLOBAL, SOURCE_KIND_WORKDIR)
WORKFLOWS_DIR_NAME: Final = "workflows"
CHRYS_DIR_NAME: Final = ".chrys"
BUILTIN_DIR: Final = Path(__file__).resolve().parent / "builtins"
MAX_SOURCE_BYTES: Final = 4 * 1024 * 1024

SourceLayout = Literal["file", "package"]
LAYOUT_FILE: Final = "file"
LAYOUT_PACKAGE: Final = "package"

PRECEDENCE: Final[tuple[tuple[str, SourceLayout], ...]] = (
    (SOURCE_KIND_WORKDIR, LAYOUT_PACKAGE),
    (SOURCE_KIND_WORKDIR, LAYOUT_FILE),
    (SOURCE_KIND_GLOBAL, LAYOUT_PACKAGE),
    (SOURCE_KIND_GLOBAL, LAYOUT_FILE),
    (SOURCE_KIND_BUILTIN, LAYOUT_FILE),
)
"""Every place one workflow id can come from, highest precedence first."""

RESERVED_GLOBAL_NAME: Final = "sdk"
"""The global directory keeps the worker SDK cache under this name, in any letter case."""

MAX_PACKAGE_ENTRIES: Final = 1000
MAX_PACKAGE_BYTES: Final = 512 * 1024 * 1024
MAX_PACKAGE_DEPTH: Final = 16
PACKAGE_DIGEST_FORMAT: Final = "chrys-workflow-package-1"
PYCACHE_DIR_NAME: Final = "__pycache__"
_CACHED_BYTECODE_NAME: Final = re.compile(r"[^.]+\..+\.pyc(?:\.\d+)?")
"""How Python names a bytecode cache (and its temporary copy) in ``__pycache__``; no import can name one."""
_HASH_CHUNK_BYTES: Final = 1024 * 1024

SourceErrorReason = Literal[
    "link", "not_regular", "unsupported_file", "unreadable", "too_large", "package_too_large", "reserved"
]


class WorkflowSourceError(OSError):
    """A workflow file or folder that can't be used.

    ``path`` is the path at fault as found (a folder member's own path), and
    ``detail`` the underlying cause without a file name, so a caller can report
    the problem without parsing the message.
    """

    def __init__(self, reason: SourceErrorReason, path: str, message: str, *, detail: str = "") -> None:
        super().__init__(f"{message}: {detail}" if detail else message)
        self.reason: SourceErrorReason = reason
        self.path = path
        self.detail = detail


@dataclass(frozen=True, slots=True)
class WorkflowPackage:
    """What a workflow folder's confirmation covers."""

    directory: str
    digest: str
    file_count: int
    total_bytes: int
    signature: tuple[tuple[str, int, int], ...] = field(default=(), compare=False)
    """``(relpath, st_mtime_ns, st_size)`` of every covered file, to notice a change without reading."""

    def python_files(self, entry_name: str) -> tuple[str, ...]:
        """The covered ``.py``/``.pyw`` files other than the entry named *entry_name*, as paths."""
        return tuple(
            os.path.join(self.directory, *relpath.split("/"))
            for relpath, _, _ in self.signature
            if relpath != entry_name and relpath.casefold().endswith((".py", ".pyw"))
        )


@dataclass(frozen=True, slots=True)
class WorkflowSource:
    """One workflow as read: identity, precedence class, and the exact entry bytes."""

    workflow_id: str
    source_kind: str
    canonical_path: str
    source: bytes
    package: WorkflowPackage | None = None

    @property
    def identity(self) -> WorkflowIdentity:
        return WorkflowIdentity(self.workflow_id, self.canonical_path, self.source_kind)

    @property
    def layout(self) -> SourceLayout:
        return LAYOUT_FILE if self.package is None else LAYOUT_PACKAGE

    @property
    def entry_sha256(self) -> str:
        return hashlib.sha256(self.source).hexdigest()

    @property
    def source_digest(self) -> str:
        """What a confirmation pins: the folder digest of a package, else the entry file's SHA-256."""
        return self.entry_sha256 if self.package is None else self.package.digest


@dataclass(frozen=True, slots=True)
class SkippedSource:
    path: str
    reason: str
    source_kind: str = ""
    workflow_id: str | None = None
    """The id the skipped file or folder would have had; ``None`` when a whole directory could not be listed."""


@dataclass(frozen=True, slots=True)
class ShadowedSource:
    source: WorkflowSource
    shadowed_by: WorkflowSource


@dataclass(frozen=True, slots=True)
class Discovery:
    sources: tuple[WorkflowSource, ...]
    skipped: tuple[SkippedSource, ...]
    shadowed: tuple[ShadowedSource, ...] = ()

    def find(self, workflow_id: str) -> WorkflowSource | None:
        for source in self.sources:
            if source.workflow_id == workflow_id:
                return source
        return None


@dataclass(frozen=True, slots=True)
class WorkflowCandidate:
    """A directory entry that names a workflow, before any of its bytes are read."""

    workflow_id: str
    source_kind: str
    layout: SourceLayout
    path: Path
    """The entry file: ``<root>/<id>.py`` or ``<root>/<id>/<id>.py``."""
    problem: WorkflowSourceError | None = None
    """Why the candidate can't be used, when that is known from its directory entries alone."""

    @property
    def location(self) -> Path:
        """What a report names: the folder of a package, else the file."""
        return self.path.parent if self.layout == LAYOUT_PACKAGE else self.path

    @property
    def rank(self) -> int:
        return precedence(self.source_kind, self.layout)


def precedence(kind: str, layout: SourceLayout) -> int:
    """Position in ``PRECEDENCE``; lower wins."""
    return PRECEDENCE.index((kind, layout))


def global_workflows_dir(config_dir: Path) -> Path:
    return config_dir / WORKFLOWS_DIR_NAME


def workdir_workflows_dir(workdir: Path) -> Path:
    return workdir / CHRYS_DIR_NAME / WORKFLOWS_DIR_NAME


def is_global_workflows_dir(directory: Path, config_dir: Path) -> bool:
    """Whether *directory* is the global workflow folder, as a working directory's is when iCode runs in the home directory."""
    root = global_workflows_dir(config_dir)
    try:
        return directory == root or os.path.samefile(directory, root)
    except OSError:
        return False


def workflow_entry_path(root: Path, workflow_id: str, layout: SourceLayout) -> Path:
    """Where the entry of *workflow_id* would be under *root* in *layout*."""
    if layout == LAYOUT_PACKAGE:
        return root / workflow_id / f"{workflow_id}.py"
    return root / f"{workflow_id}.py"


def discover_workflows(*, config_dir: Path, workdir: Path | None) -> Discovery:
    """Every runnable workflow by id (the first readable candidate in ``PRECEDENCE`` wins), sorted by id."""
    layers: list[tuple[str, Path]] = [
        (SOURCE_KIND_BUILTIN, BUILTIN_DIR),
        (SOURCE_KIND_GLOBAL, global_workflows_dir(config_dir)),
    ]
    if workdir is not None:
        layers.append((SOURCE_KIND_WORKDIR, workdir_workflows_dir(workdir)))
    skipped: list[SkippedSource] = []
    found: list[tuple[int, WorkflowSource]] = []
    for kind, directory in layers:
        reserves_sdk = kind == SOURCE_KIND_GLOBAL or (
            kind == SOURCE_KIND_WORKDIR and is_global_workflows_dir(directory, config_dir)
        )
        for candidate in _scan(directory, kind, skipped, reserves_sdk=reserves_sdk):
            problem = candidate.problem
            if problem is None:
                try:
                    found.append((candidate.rank, read_source(candidate.path, kind, layout=candidate.layout)))
                    continue
                except OSError as exc:
                    problem = exc
            skipped.append(SkippedSource(str(candidate.location), str(problem), kind, candidate.workflow_id))
    winners: dict[str, tuple[int, WorkflowSource]] = {}
    for rank, source in found:
        best = winners.get(source.workflow_id)
        if best is None or rank < best[0]:
            winners[source.workflow_id] = (rank, source)
    shadowed = tuple(
        ShadowedSource(source, winners[source.workflow_id][1])
        for _, source in sorted(found, key=lambda item: (item[1].workflow_id, item[0]))
        if source is not winners[source.workflow_id][1]
    )
    return Discovery(tuple(winners[key][1] for key in sorted(winners)), tuple(skipped), shadowed)


def recognize_candidate(
    root: Path, entry: os.DirEntry[str], kind: str, *, reserves_sdk: bool
) -> WorkflowCandidate | None:
    """Whether one entry of a workflow directory names a workflow, judged from directory listings alone.

    Names compare by their listed spelling, never by probing a path, so a
    case-insensitive file system can't match ``Code-Review.py`` to a folder
    ``code-review``. A folder's entry is found by name first and only then
    checked for its type, so a broken entry is reported instead of vanishing.
    *reserves_sdk* says that *root* is the global workflow folder, whose
    ``sdk`` folder is iCode's.
    """
    name = entry.name
    if name.startswith((".", "_")):
        return None
    try:
        if name.endswith(".py") and (entry.is_symlink() or entry.is_file(follow_symlinks=False)):
            return WorkflowCandidate(name.removesuffix(".py"), kind, LAYOUT_FILE, root / name)
        if kind == SOURCE_KIND_BUILTIN:
            return None
        folder = root / name
        entry_name = f"{name}.py"
        if _is_link_entry(entry):
            if _find_exact(folder, entry_name) is None:
                return None
            problem = WorkflowSourceError("link", str(folder), "workflow folders can't be links")
            return WorkflowCandidate(name, kind, LAYOUT_PACKAGE, folder / entry_name, problem)
        if not entry.is_dir(follow_symlinks=False):
            return None
    except OSError:
        return None
    child = _find_exact(folder, entry_name)
    if child is None:
        return None
    if reserves_sdk and name.casefold() == RESERVED_GLOBAL_NAME:
        problem = WorkflowSourceError(
            "reserved", str(folder), f'the name "{RESERVED_GLOBAL_NAME}" is reserved in the global workflow folder'
        )
    else:
        problem = _entry_type_problem(child)
    return WorkflowCandidate(name, kind, LAYOUT_PACKAGE, folder / entry_name, problem)


def read_source(path: Path, kind: str, *, layout: SourceLayout) -> WorkflowSource:
    """Read one workflow; user files owner-verified, builtins as installed (a system install is root-owned).

    Only the workflow directory is resolved, never a folder below it, so the
    owner-verified reads refuse a workflow folder that is a link.
    """
    if layout == LAYOUT_FILE:
        canonical = path.parent.resolve() / path.name
        return WorkflowSource(canonical.stem, kind, str(canonical), read_entry_bytes(canonical, kind))
    if kind == SOURCE_KIND_BUILTIN:
        raise ValueError("builtin workflows are single files")
    folder_name = path.parent.name
    if path.name != f"{folder_name}.py":
        raise ValueError(f"a workflow folder's entry must be named after the folder: {path}")
    canonical = path.parent.parent.resolve() / folder_name / path.name
    payload = read_entry_bytes(canonical, kind)
    return WorkflowSource(folder_name, kind, str(canonical), payload, _read_package(canonical, payload))


def read_entry_bytes(path: Path, kind: str) -> bytes:
    """The entry file's bytes, read as discovery reads them; *path* is used as given (pass a canonical path)."""
    if kind == SOURCE_KIND_BUILTIN:
        try:
            payload = path.read_bytes()
        except OSError as exc:
            raise WorkflowSourceError(
                "unreadable", str(path), "the workflow file could not be read", detail=_detail(exc)
            ) from exc
    else:
        try:
            info = os.lstat(path)
        except OSError as exc:
            raise WorkflowSourceError(
                "unreadable", str(path), "the workflow file could not be read", detail=_detail(exc)
            ) from exc
        if is_link_stat(info):
            raise WorkflowSourceError("link", str(path), "workflow files can't be links")
        if not stat.S_ISREG(info.st_mode):
            raise WorkflowSourceError("not_regular", str(path), "the workflow file is not a regular file")
        try:
            with secure_open_owner_verified_binary(path) as handle:
                payload = handle.read(MAX_SOURCE_BYTES + 1)
        except OSError as exc:
            raise WorkflowSourceError(
                "unreadable", str(path), "the workflow file could not be read", detail=_detail(exc)
            ) from exc
    if len(payload) > MAX_SOURCE_BYTES:
        raise WorkflowSourceError("too_large", str(path), f"the workflow file is larger than {MAX_SOURCE_BYTES} bytes")
    return payload


def package_signature(directory: Path) -> tuple[tuple[str, int, int], ...]:
    """``WorkflowPackage.signature`` as it is now, from file metadata only; raises like reading the folder."""
    return tuple(sorted((relpath, info.st_mtime_ns, info.st_size) for relpath, _, info in _walk_package(directory)))


def is_link_stat(info: os.stat_result) -> bool:
    """A symbolic link, or a Windows junction (which ``lstat`` reports as a directory)."""
    if stat.S_ISLNK(info.st_mode):
        return True
    return sys.platform == "win32" and info.st_reparse_tag == stat.IO_REPARSE_TAG_MOUNT_POINT


def builtin_manifest_path(workflow_id: str) -> Path:
    """The pre-generated manifest shipped next to a builtin template."""
    return BUILTIN_DIR / f"{workflow_id}.manifest.json"


def read_builtin_manifest(workflow_id: str) -> dict[str, Any] | None:
    """The pre-generated manifest of builtin *workflow_id*; ``None`` when there is none or it is unreadable."""
    try:
        payload = json.loads(builtin_manifest_path(workflow_id).read_text(encoding="utf-8"))
    except OSError, ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def _scan(directory: Path, kind: str, skipped: list[SkippedSource], *, reserves_sdk: bool) -> list[WorkflowCandidate]:
    """The candidates directly under *directory*, sorted by entry path; *reserves_sdk* when it holds the SDK."""
    try:
        with os.scandir(directory) as listing:
            entries = list(listing)
    except FileNotFoundError:
        return []
    except OSError as exc:
        skipped.append(SkippedSource(str(directory), str(exc), kind))
        logger.warning("workflow directory %s could not be listed", directory, exc_info=True)
        return []
    root = directory.resolve()
    found = [
        candidate
        for entry in entries
        if (candidate := recognize_candidate(root, entry, kind, reserves_sdk=reserves_sdk)) is not None
    ]
    found.sort(key=lambda candidate: candidate.path)
    return found


def _find_exact(directory: Path, name: str) -> os.DirEntry[str] | None:
    """The entry of *directory* spelled exactly *name*, if the directory can be listed."""
    try:
        with os.scandir(directory) as listing:
            for entry in listing:
                if entry.name == name:
                    return entry
    except OSError:
        return None
    return None


def _entry_type_problem(entry: os.DirEntry[str]) -> WorkflowSourceError | None:
    try:
        if _is_link_entry(entry):
            found = "a link"
        elif entry.is_dir(follow_symlinks=False):
            found = "a folder"
        elif entry.is_file(follow_symlinks=False):
            return None
        else:
            found = "a special file"
    except OSError as exc:
        return WorkflowSourceError(
            "unreadable", entry.path, f"the entry {entry.name} could not be read", detail=_detail(exc)
        )
    return WorkflowSourceError("not_regular", entry.path, f"the entry {entry.name} must be a regular file, not {found}")


def _read_package(entry: Path, entry_payload: bytes) -> WorkflowPackage:
    """Hash every covered file of *entry*'s folder; the entry's bytes are the ones already read."""
    files: list[list[str]] = []
    signature: list[tuple[str, int, int]] = []
    total = 0
    for relpath, path, info in _walk_package(entry.parent):
        if relpath == entry.name:
            digest, size = hashlib.sha256(entry_payload).hexdigest(), len(entry_payload)
        else:
            digest, size = _hash_member(path, relpath, MAX_PACKAGE_BYTES - total)
        total += size
        if total > MAX_PACKAGE_BYTES:
            raise _package_too_large(entry.parent, f"is larger than {MAX_PACKAGE_BYTES} bytes in total")
        files.append([relpath, digest])
        signature.append((relpath, info.st_mtime_ns, info.st_size))
    if not any(relpath == entry.name for relpath, _ in files):
        raise WorkflowSourceError("unreadable", str(entry), "the workflow folder changed while it was read")
    files.sort()
    document = {"format": PACKAGE_DIGEST_FORMAT, "files": files}
    # ensure_ascii keeps a surrogate-escaped name apart from one that spells ``\udcXX`` literally.
    encoded = json.dumps(document, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("ascii")
    return WorkflowPackage(
        str(entry.parent), hashlib.sha256(encoded).hexdigest(), len(files), total, tuple(sorted(signature))
    )


def _hash_member(path: Path, relpath: str, budget: int) -> tuple[str, int]:
    """*path*'s SHA-256 and size, read in chunks and only one byte past *budget*, so a big file is never held whole."""
    digest = hashlib.sha256()
    size = 0
    limit = max(budget, 0) + 1
    try:
        with secure_open_owner_verified_binary(path) as handle:
            while size < limit and (chunk := handle.read(min(_HASH_CHUNK_BYTES, limit - size))):
                digest.update(chunk)
                size += len(chunk)
    except OSError as exc:
        raise WorkflowSourceError("unreadable", str(path), f"could not read {relpath}", detail=_detail(exc)) from exc
    return digest.hexdigest(), size


def _walk_package(directory: Path) -> Iterator[tuple[str, Path, os.stat_result]]:
    """Every covered regular file under *directory* with its relative POSIX path, enforcing the folder rules.

    Hidden entries and the regular files named like bytecode caches in
    ``__pycache__`` folders are not covered and not counted, nor is such a
    folder itself; anything else in it can be imported, so it is. Links are
    refused rather than followed, as are files that are not regular.
    """
    try:
        info = os.lstat(directory)
    except OSError as exc:
        raise WorkflowSourceError(
            "unreadable", str(directory), "the workflow folder could not be read", detail=_detail(exc)
        ) from exc
    if is_link_stat(info):
        raise WorkflowSourceError("link", str(directory), "workflow folders can't be links")
    if not stat.S_ISDIR(info.st_mode):
        raise WorkflowSourceError("unreadable", str(directory), "the workflow folder changed while it was read")
    entries = 0
    total = 0
    pending: list[tuple[str, tuple[str, ...], bool]] = [(str(directory), (), False)]
    while pending:
        current, parts, in_cache = pending.pop()
        try:
            with os.scandir(current) as listing:
                children = sorted(listing, key=lambda child: child.name)
        except OSError as exc:
            where = "/".join(parts) or "the workflow folder"
            raise WorkflowSourceError("unreadable", current, f"could not list {where}", detail=_detail(exc)) from exc
        for child in children:
            name = child.name
            if name.startswith(".") or (in_cache and _CACHED_BYTECODE_NAME.fullmatch(name) and _is_plain_file(child)):
                continue
            member = (*parts, name)
            relpath = "/".join(member)
            cache_folder = name == PYCACHE_DIR_NAME and _is_plain_folder(child)
            entries += 0 if cache_folder else 1
            if entries > MAX_PACKAGE_ENTRIES:
                raise _package_too_large(directory, f"has more than {MAX_PACKAGE_ENTRIES} files and folders")
            if len(member) > MAX_PACKAGE_DEPTH:
                raise _package_too_large(directory, f"is nested deeper than {MAX_PACKAGE_DEPTH} levels at {relpath}")
            try:
                is_link = _is_link_entry(child)
                child_info = child.stat(follow_symlinks=False)
            except OSError as exc:
                raise WorkflowSourceError(
                    "unreadable", child.path, f"could not read {relpath}", detail=_detail(exc)
                ) from exc
            if is_link or is_link_stat(child_info):
                raise WorkflowSourceError("link", child.path, f"contains a link: {relpath}")
            if stat.S_ISDIR(child_info.st_mode):
                pending.append((child.path, member, cache_folder))
                continue
            if not stat.S_ISREG(child_info.st_mode):
                raise WorkflowSourceError("unsupported_file", child.path, f"unsupported file type: {relpath}")
            total += child_info.st_size
            if total > MAX_PACKAGE_BYTES:
                raise _package_too_large(directory, f"is larger than {MAX_PACKAGE_BYTES} bytes in total")
            yield relpath, Path(child.path), child_info


def _package_too_large(directory: Path, problem: str) -> WorkflowSourceError:
    return WorkflowSourceError(
        "package_too_large",
        str(directory),
        f"the workflow folder {problem} (hidden entries such as .venv and bytecode caches in __pycache__ don't count)",
    )


def _is_link_entry(entry: os.DirEntry[str]) -> bool:
    return entry.is_symlink() or entry.is_junction()


def _is_plain_file(entry: os.DirEntry[str]) -> bool:
    """Whether *entry* is a regular file and not a link; one that can't be told is checked like any member."""
    try:
        return entry.is_file(follow_symlinks=False) and not _is_link_entry(entry)
    except OSError:
        return False


def _is_plain_folder(entry: os.DirEntry[str]) -> bool:
    """Whether *entry* is a folder and not a link; one that can't be told is checked like any member."""
    try:
        return entry.is_dir(follow_symlinks=False) and not _is_link_entry(entry)
    except OSError:
        return False


def _detail(exc: OSError) -> str:
    """The cause without the file name some ``OSError`` messages repeat."""
    return exc.strerror or str(exc)
