# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The files a dataset's judge reads, held in memory for one evaluation.

Every case ``script`` and ``support_files`` entry is read when the evaluation
starts. A case's check script then runs from a copy written only after that
case's agent finished, outside every case folder, so nothing the agent did
reaches the files the judge uses.

The dataset file is captured with them. A task's ``input/`` belongs to RSI:
an evaluation of a task compares it with the captured content after every
case run and puts back whatever the agent changed (the case then fails), so
an edit can neither raise a score nor outlive the run. Seeds under
``input/seeds/`` are not captured.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Literal

from chrys.service.rsi.dataset import RsiDataset
from chrys.service.rsi.errors import RsiBadRequest
from chrys.service.rsi.paths import safe_relpath_under
from chrys.service.rsi.workdir_seed import refuse_escaping_symlinks

_EntryKind = Literal["file", "dir", "link"]


@dataclass(slots=True, frozen=True)
class _Entry:
    kind: _EntryKind
    data: bytes = b""
    """A file's bytes, or a link's target."""
    mode: int = 0o644

    def fingerprint(self) -> str:
        executable = int(self.kind == "file" and bool(self.mode & stat.S_IXUSR))
        digest = hashlib.sha256(f"{self.kind}:{executable}:".encode())
        digest.update(self.data)
        return digest.hexdigest()


@dataclass(slots=True, frozen=True)
class JudgeInputs:
    """One evaluation's copy of the judge's files, keyed by their path relative to ``source_dir``."""

    source_dir: Path
    staged_roots: tuple[str, ...]
    """Every case script and ``support_files`` entry: what a check script sees."""
    dataset_name: str
    entries: Mapping[str, _Entry]

    @classmethod
    def capture(cls, dataset: RsiDataset) -> JudgeInputs:
        source_dir = dataset.source_path.parent
        scripts = sorted({_clean(case.script) for case in dataset.cases if case.script})
        roots = tuple(dict.fromkeys((*scripts, *(_clean(rel) for rel in dataset.support_files))))
        name = dataset.source_path.name
        entries = _read_entries(source_dir, (*roots, name), strict=True)
        return cls(source_dir, roots, name, MappingProxyType(entries))

    def fingerprints(self) -> dict[str, str]:
        return {rel: entry.fingerprint() for rel, entry in sorted(self.entries.items())}

    def differs_from(self, fingerprints: Mapping[str, str]) -> tuple[str, ...]:
        """Paths whose content is not what *fingerprints* recorded."""
        current = self.fingerprints()
        return tuple(
            sorted(rel for rel in current.keys() | fingerprints.keys() if current.get(rel) != fingerprints.get(rel))
        )

    def stage(self, dest: Path) -> Path:
        """Write every script and ``support_files`` entry under *dest*, at the same relative paths."""
        dest.mkdir(parents=True, exist_ok=True)
        for rel in sorted(self.entries):
            if any(rel == root or rel.startswith(f"{root}/") for root in self.staged_roots):
                _write(dest / rel, self.entries[rel])
        return dest

    def changed(self) -> tuple[str, ...]:
        """Paths under ``source_dir`` that no longer hold the captured content."""
        now = _read_entries(self.source_dir, (*self.staged_roots, self.dataset_name), strict=False)
        return self.differs_from({rel: entry.fingerprint() for rel, entry in now.items()})

    def restore(self) -> None:
        """Put the captured content back under ``source_dir``."""
        for root in dict.fromkeys((*self.staged_roots, self.dataset_name)):
            target = self._target(root)
            entry = self.entries[root]
            if entry.kind == "file":
                _replace_file(target, entry)
                continue
            _remove(target)
            for rel in sorted(self.entries):
                if rel == root or rel.startswith(f"{root}/"):
                    _write(self._target(rel), self.entries[rel])

    def _target(self, rel: str) -> Path:
        target = self.source_dir / rel
        # Writing through a folder link would land outside source_dir.
        for parent in Path(rel).parents:
            if parent != Path(".") and (self.source_dir / parent).is_symlink():
                raise RsiBadRequest(f"cannot restore judge file {rel}: {self.source_dir / parent} is a link")
        return target


def _clean(rel: str) -> str:
    return rel.strip().replace("\\", "/").rstrip("/")


def _read_entries(source_dir: Path, roots: tuple[str, ...], *, strict: bool) -> dict[str, _Entry]:
    """Read *roots*; with *strict* off a missing, escaping or unreadable root is left out instead of raising."""
    entries: dict[str, _Entry] = {}
    for rel in roots:
        try:
            src = safe_relpath_under(source_dir, rel, field="dataset file")
        except ValueError as exc:
            if strict:
                raise RsiBadRequest(f"dataset file escapes the dataset directory: {rel}") from exc
            continue
        try:
            if src.is_dir():
                if strict:
                    refuse_escaping_symlinks(src, label="support_files")
                _read_dir(src, rel, entries)
            elif src.is_file():
                entries[rel] = _read_file(src)
            elif strict:
                raise RsiBadRequest(f"dataset file not found: {rel}")
        except OSError as exc:
            if strict:
                raise RsiBadRequest(f"could not read dataset file {rel}: {exc}") from exc
    return entries


def _read_file(path: Path) -> _Entry:
    return _Entry("file", path.read_bytes(), stat.S_IMODE(path.stat().st_mode))


def _read_dir(root: Path, rel: str, entries: dict[str, _Entry]) -> None:
    entries[rel] = _Entry("dir")
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for name in (*dirnames, *filenames):
            path = Path(dirpath) / name
            key = f"{rel}/{path.relative_to(root).as_posix()}"
            if path.is_symlink():
                entries[key] = _Entry("link", os.fsencode(os.readlink(path)))
            elif path.is_dir():
                entries[key] = _Entry("dir")
            else:
                entries[key] = _read_file(path)


def _remove(target: Path) -> None:
    try:
        if target.is_symlink() or target.is_file():
            target.unlink()
        elif target.is_dir():
            shutil.rmtree(target)
    except OSError as exc:
        raise RsiBadRequest(f"cannot restore judge file {target}: {exc}") from exc


def _replace_file(target: Path, entry: _Entry) -> None:
    """Swap *entry* in at *target* in one step, whatever is there now."""
    if target.is_dir() and not target.is_symlink():
        _remove(target)
    temp = target.with_name(f".{target.name}.rsi-restore")
    _remove(temp)
    _write(temp, entry)
    try:
        os.replace(temp, target)
    except OSError as exc:
        raise RsiBadRequest(f"cannot restore judge file {target}: {exc}") from exc


def _write(target: Path, entry: _Entry) -> None:
    try:
        if entry.kind == "dir":
            target.mkdir(parents=True, exist_ok=True)
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        if entry.kind == "link":
            os.symlink(os.fsdecode(entry.data), target)
            return
        target.write_bytes(entry.data)
        target.chmod(entry.mode)
    except OSError as exc:
        raise RsiBadRequest(f"could not write judge file {target}: {exc}") from exc
