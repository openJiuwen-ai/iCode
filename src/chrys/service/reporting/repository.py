# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Who the workspace belongs to: a repository identity the server can join on.

The identity is the digest of the repository's *common* directory real path, so
a clone and its worktrees, or two checkouts at different paths, stay apart while
a moved working tree of the same repository keeps naming it. One ``origin``
remote (or the only remote) supplies the human-readable host/path; a repository
with several remotes and no ``origin`` is reported as ambiguous and not sent.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from chrys.foundation.platform.paths import real_path
from chrys.foundation.reporting.frames import canonical_json_bytes
from chrys.foundation.util.time import utc_iso
from chrys.service.reporting.git import git_available, run_git
from chrys.service.reporting.meta import WORKSPACE_KIND_GIT, WORKSPACE_KIND_NO_REPO, WORKSPACE_KIND_NON_GIT

SCHEMA_NAME: Final = "repository/1.0"

URL_KIND_ORIGIN: Final = "origin"
URL_KIND_SINGLE_REMOTE: Final = "single_remote"

AMBIGUOUS_REMOTES: Final = "ambiguous_remotes"


@dataclass(frozen=True, slots=True)
class RepositoryIdentity:
    """One repository, as the ``repository_registry`` row describes it."""

    identity: str
    host: str
    path: str
    url_kind: str
    remote_digest: str
    observed_at: str

    def payload(self) -> bytes:
        """The canonical ``repository/1.0`` bytes."""
        return canonical_json_bytes(
            {
                "schema": SCHEMA_NAME,
                "repositoryIdentity": self.identity,
                "remote": {"host": self.host, "path": self.path, "urlKind": self.url_kind},
                "remoteDigest": self.remote_digest,
                "observedAt": self.observed_at,
            }
        )


@dataclass(frozen=True, slots=True)
class RepositoryProbe:
    """What one probe learned about the workspace root."""

    workspace_kind: str
    root: str = ""
    head_sha: str = ""
    identity: RepositoryIdentity | None = None
    reason: str = ""


def probe_repository(cwd: str) -> RepositoryProbe:
    """Classify *cwd* and read its identity, or explain why there is none."""
    if not cwd:
        return RepositoryProbe(workspace_kind=WORKSPACE_KIND_NO_REPO, reason="no_cwd")
    if not git_available():
        return RepositoryProbe(workspace_kind=WORKSPACE_KIND_NO_REPO, reason="no_git_executable")
    toplevel = run_git(["rev-parse", "--show-toplevel"], cwd)
    if toplevel is None:
        return RepositoryProbe(workspace_kind=WORKSPACE_KIND_NO_REPO, reason="git_unavailable")
    root = toplevel.text() if toplevel.ok else ""
    if not root:
        return RepositoryProbe(workspace_kind=WORKSPACE_KIND_NON_GIT, reason="not_a_repository")
    head = run_git(["rev-parse", "HEAD"], cwd)
    head_sha = head.text() if head is not None and head.ok else ""
    common = run_git(["rev-parse", "--git-common-dir"], cwd)
    common_path = common.text() if common is not None and common.ok else ""
    identity = _identity(cwd, common_path or root)
    return RepositoryProbe(workspace_kind=WORKSPACE_KIND_GIT, root=root, head_sha=head_sha, identity=identity)


def _identity(cwd: str, common_dir: str) -> RepositoryIdentity | None:
    remotes = run_git(["remote"], cwd)
    names = remotes.text().splitlines() if remotes is not None and remotes.ok else []
    url_kind = ""
    remote_name = ""
    if "origin" in names:
        remote_name, url_kind = "origin", URL_KIND_ORIGIN
    elif len(names) == 1:
        remote_name, url_kind = names[0], URL_KIND_SINGLE_REMOTE
    elif names:
        return None
    url = ""
    if remote_name:
        result = run_git(["remote", "get-url", remote_name], cwd)
        url = result.text() if result is not None and result.ok else ""
    host, path = parse_remote_url(url)
    digest_source = os.fsencode(str(real_path(Path(cwd) / common_dir)))
    return RepositoryIdentity(
        identity=hashlib.sha256(digest_source).hexdigest(),
        host=host,
        path=path,
        url_kind=url_kind,
        remote_digest=hashlib.sha256(url.encode("utf-8", "surrogateescape")).hexdigest() if url else "",
        observed_at=utc_iso(),
    )


def parse_remote_url(url: str) -> tuple[str, str]:
    """Split a remote URL into ``(host, path)``; a local path has no host."""
    raw = url.strip()
    if not raw:
        return "", ""
    if "://" in raw:
        _scheme, _, remainder = raw.partition("://")
        authority, _, path = remainder.partition("/")
        host = authority.rsplit("@", 1)[-1].split(":", 1)[0]
        return host, f"/{path}"
    head, separator, tail = raw.partition(":")
    if separator and ("@" in head or "/" not in head):
        # ``git@example.com:owner/repo.git`` — the scp-like spelling.
        return head.rsplit("@", 1)[-1], f"/{tail.lstrip('/')}"
    return "", raw


def repository_reason(probe: RepositoryProbe) -> str:
    """The diagnostic name for a probe that produced no identity."""
    if probe.identity is not None:
        return ""
    return probe.reason or AMBIGUOUS_REMOTES
