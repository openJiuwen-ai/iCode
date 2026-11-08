# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The workspace mirror: the repository's dirty tree, one file at a time.

Two uses, one collection: the pair of snapshots around a turn answers "what did
the agent change", and the pre-turn snapshot is the world the agent saw. Only
dirty paths are read; unchanged content is never sent twice because blocks are
content-addressed. Ignored files contribute a path list and nothing else — their
bodies never leave the machine.

The capture is gated on a signature (``HEAD`` plus the porcelain status bytes),
so a turn that changed nothing produces no units at all.
"""

from __future__ import annotations

import hashlib
import os
import stat
import time
from dataclasses import dataclass, field
from typing import Final

from chrys.foundation.reporting.frames import (
    KIND_WORKSPACE_FILE,
    KIND_WORKSPACE_MANIFEST,
    Frame,
    canonical_json_bytes,
    scope_workspace_file,
    scope_workspace_manifest,
    sha256_hex,
)
from chrys.foundation.reporting.wal import DurableWAL
from chrys.foundation.util.time import utc_iso
from chrys.service.reporting.cursors import SessionCursor
from chrys.service.reporting.git import GitResult, nul_fields, run_git

SCHEMA_NAME: Final = "workspace-state/1.0"

ENTRY_MAX_PER_SECTION: Final = 10_000
FILE_MAX_BYTES: Final = 4 << 20
TOTAL_MAX_BYTES: Final = 32 << 20
CAPTURE_MAX_SECONDS: Final = 10.0
FILE_BLOCK_BYTES: Final = 1 << 20
IGNORED_MAX_ENTRIES: Final = 200

STAGED: Final = "staged"
WORKTREE: Final = "worktree"
UNTRACKED: Final = "untracked"
SECTIONS: Final = (STAGED, WORKTREE, UNTRACKED)
SECTION_BASE: Final = {STAGED: "head", WORKTREE: "index"}

REMOTE_KIND_GIT: Final = "git"
REMOTE_KIND_TIMEOUT: Final = "timeout"
REMOTE_KIND_TRUNCATED: Final = "truncated"


@dataclass(slots=True)
class _Pending:
    """One dirty entry awaiting its content."""

    path: str
    status: str
    section: str


@dataclass(slots=True)
class _Captured:
    """One dirty entry whose content was read, before the stream offsets exist."""

    path: str
    status: str
    mode: str
    data: bytes | None
    raced: bool = False


@dataclass(slots=True)
class WorkspaceCaptureResult:
    """What one capture produced."""

    signature: str = ""
    manifest_sha256: str = ""
    file_shas: list[str] = field(default_factory=list)
    units: int = 0
    complete: bool = True
    remote_kind: str = REMOTE_KIND_GIT


class WorkspaceCapture:
    """Collect the dirty tree of one repository as a manifest plus file blocks."""

    def __init__(self, wal: DurableWAL) -> None:
        self._wal = wal

    def signature(self, root: str, *, head_sha: str = "") -> str:
        """``sha256(HEAD + porcelain status bytes)`` — the change gate.

        A caller that already read ``HEAD`` passes it; nothing is read twice.
        """
        status = run_git(["status", "--porcelain=v2", "-z"], root)
        if status is None:
            return ""
        return _signature(head_sha, status)

    def capture(
        self,
        session_id: str,
        root: str,
        cursor: SessionCursor,
        *,
        repository_identity: str = "",
        head_sha: str = "",
        head_tree_sha: str = "",
        branch: str = "",
        detached: bool = False,
        remote_kind: str = "",
    ) -> WorkspaceCaptureResult:
        """Queue the manifest and every changed file block; return what was sent."""
        started = time.monotonic()
        started_at = utc_iso()
        status = run_git(["status", "--porcelain=v2", "-z"], root)
        if status is None:
            result = WorkspaceCaptureResult(signature="", complete=False, remote_kind=REMOTE_KIND_TIMEOUT)
            return result
        signature = _signature(head_sha, status)
        pending = parse_status_entries(status)
        ignored, ignored_truncated = self._ignored(root)
        known_shas = set(cursor.workspace_file_shas)
        frames: list[Frame] = []
        omitted: list[dict[str, str]] = []
        captured: dict[str, list[_Captured]] = {section: [] for section in SECTIONS}
        total_bytes = 0
        complete = True
        remote_kind = remote_kind or REMOTE_KIND_GIT
        emitted_shas: list[str] = []
        for entry in pending:
            if time.monotonic() - started > CAPTURE_MAX_SECONDS:
                complete = False
                remote_kind = REMOTE_KIND_TIMEOUT
                omitted.append({"path": entry.path, "reason": "timeout"})
                break
            if len(captured[entry.section]) >= ENTRY_MAX_PER_SECTION:
                complete = False
                omitted.append({"path": entry.path, "reason": "entry-cap"})
                continue
            if total_bytes >= TOTAL_MAX_BYTES:
                complete = False
                remote_kind = REMOTE_KIND_TRUNCATED
                omitted.append({"path": entry.path, "reason": "total-cap"})
                continue
            content = self._read_entry(root, entry, omitted)
            if content is None:
                captured[entry.section].append(_Captured(entry.path, entry.status, "0", None))
                continue
            data, mode, raced = content
            if len(data) > FILE_MAX_BYTES:
                complete = False
                remote_kind = REMOTE_KIND_TRUNCATED
                omitted.append({"path": entry.path, "reason": "size-cap"})
                continue
            digest = sha256_hex(data)
            total_bytes += len(data)
            captured[entry.section].append(_Captured(entry.path, entry.status, mode, data, raced))
            if digest in known_shas or digest in emitted_shas:
                continue
            emitted_shas.append(digest)
            frames.extend(self._file_frames(session_id, digest, data))
        manifest = {
            "schema": SCHEMA_NAME,
            "repository": {
                "identity": repository_identity,
                "head": {
                    "sha": head_sha,
                    "treeSha": head_tree_sha,
                    "branch": branch,
                    "detached": detached,
                },
                # The remote's spelling only: a URL can carry a credential and
                # the server keys the row on ``kind``.
                "remote": {"kind": remote_kind},
            },
            **self._sections(captured),
            "omitted": omitted,
            "ignored": {
                "count": len(ignored),
                "truncated": ignored_truncated,
                "entries": ignored,
            },
            "capture": {
                "startedAt": started_at,
                "completedAt": utc_iso(),
                "durationMs": int((time.monotonic() - started) * 1000),
                "complete": complete,
            },
        }
        payload = canonical_json_bytes(manifest)
        manifest_sha = sha256_hex(payload)
        frames.append(
            Frame.build(
                KIND_WORKSPACE_MANIFEST,
                session_id,
                scope_workspace_manifest(manifest_sha),
                payload,
            )
        )
        self._wal.append(frames)
        return WorkspaceCaptureResult(
            signature=signature,
            manifest_sha256=manifest_sha,
            file_shas=emitted_shas,
            units=len(frames),
            complete=complete,
            remote_kind=remote_kind,
        )

    def _sections(self, captured: dict[str, list[_Captured]]) -> dict[str, object]:
        """The three section blocks, their contents numbered along the stream.

        The offsets follow the manifest's own order (staged, then worktree, then
        untracked), because the server concatenates the container's contents in
        that order and refuses a stream whose blocks do not line up.
        """
        blocks: dict[str, object] = {}
        offset = 0
        for section in SECTIONS:
            entries: list[dict[str, object]] = []
            for item in captured[section]:
                reference = None
                if item.data is not None:
                    reference = {
                        "offset": offset,
                        "length": len(item.data),
                        "sha256": sha256_hex(item.data),
                    }
                    offset += len(item.data)
                record: dict[str, object] = {
                    "path": item.path,
                    "status": item.status,
                    "mode": item.mode,
                    "content": reference,
                }
                if item.raced:
                    record["raced"] = True
                entries.append(record)
            block: dict[str, object] = {"entries": entries}
            base = SECTION_BASE.get(section)
            if base is not None:
                block = {"base": base, "entries": entries}
            blocks[section] = block
        return blocks

    def _ignored(self, root: str) -> tuple[list[str], bool]:
        result = run_git(
            ["ls-files", "--others", "--ignored", "--exclude-standard", "--directory", "-z"],
            root,
        )
        if result is None or not result.ok:
            return [], False
        paths = nul_fields(result.stdout)
        return paths[:IGNORED_MAX_ENTRIES], len(paths) > IGNORED_MAX_ENTRIES

    def _read_entry(self, root: str, entry: _Pending, omitted: list[dict[str, str]]) -> tuple[bytes, str, bool] | None:
        """Read one entry's content, detecting a file that changed under the read."""
        if "D" in entry.status:
            return None
        path = os.path.join(root, entry.path)
        try:
            before = os.stat(path)
            if not stat.S_ISREG(before.st_mode):
                return None
            with open(path, "rb") as handle:
                data = handle.read(FILE_MAX_BYTES + 1)
            after = os.stat(path)
        except OSError:
            omitted.append({"path": entry.path, "reason": "read-error"})
            return None
        raced = (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns)
        mode = "100755" if before.st_mode & stat.S_IXUSR else "100644"
        return data, mode, raced

    def _file_frames(self, session_id: str, digest: str, data: bytes) -> list[Frame]:
        if len(data) <= FILE_BLOCK_BYTES:
            return [Frame.build(KIND_WORKSPACE_FILE, session_id, scope_workspace_file(digest, 0), data)]
        frames: list[Frame] = []
        for index, start in enumerate(range(0, len(data), FILE_BLOCK_BYTES)):
            block = data[start : start + FILE_BLOCK_BYTES]
            frames.append(Frame.build(KIND_WORKSPACE_FILE, session_id, scope_workspace_file(digest, index), block))
        return frames


def _signature(head_sha: str, status: GitResult) -> str:
    """Digest of the two facts that decide whether the tree changed."""
    digest = hashlib.sha256()
    digest.update(head_sha.encode("utf-8", "surrogateescape"))
    digest.update(status.stdout)
    return digest.hexdigest()


def parse_status_entries(status: GitResult) -> list[_Pending]:
    """Turn ``git status --porcelain=v2 -z`` into one entry per dirty path."""
    entries: list[_Pending] = []
    fields = status.stdout.decode("utf-8", "surrogateescape").split("\0")
    index = 0
    while index < len(fields):
        record = fields[index]
        index += 1
        if not record:
            continue
        kind = record[0]
        if kind == "?":
            entries.append(_Pending(path=record[2:], status="??", section=UNTRACKED))
            continue
        if kind == "!":
            continue
        if kind == "1":
            parts = record.split(" ", 8)
            if len(parts) < 9:
                continue
            entries.append(_entry(parts))
            continue
        if kind == "u":
            parts = record.split(" ", 10)
            if len(parts) < 11:
                continue
            entries.append(_entry(parts))
            continue
        if kind == "2":
            # A rename/copy: the original path rides in the next NUL field.
            parts = record.split(" ", 9)
            if len(parts) < 10:
                index += 1
                continue
            entries.append(_entry(parts))
            index += 1
    return entries


def _entry(parts: list[str]) -> _Pending:
    xy = parts[1]
    index_status, worktree_status = xy[:1], xy[1:]
    path = parts[-1]
    staged = index_status not in {".", ""} and worktree_status in {".", ""}
    return _Pending(path=path, status=xy, section=STAGED if staged else WORKTREE)
