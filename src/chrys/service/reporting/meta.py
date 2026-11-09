# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The only index-shaped payload this client ships: ``session.meta``.

The values come from state the process already holds — the save hook hands over
primitives, never the session file — so the server never has to parse
``session.json`` and the client never re-reads it. The session's environment
columns (the agent configuration and the workspace classification) are written
only by the ``agent.env`` frame, never here: session reporting must not depend
on configuration collection succeeding.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from typing import Final

from chrys.foundation.reporting.frames import canonical_json_bytes

SCHEMA_NAME: Final = "session/1.0"

MAX_REPOSITORY_IDENTITIES: Final = 16

WORKSPACE_KIND_GIT: Final = "git"
WORKSPACE_KIND_NON_GIT: Final = "non_git"
WORKSPACE_KIND_NO_REPO: Final = "no_repo"


@dataclass(frozen=True, slots=True)
class ReportMeta:
    """One session's index row, built from values the saving scope already has."""

    session_id: str
    title: str = ""
    agent_profile: str = ""
    model_provider: str = ""
    model_id: str = ""
    parent_session_id: str = ""
    primary_cwd: str = ""
    created_at: str = ""
    updated_at: str = ""
    schema_version: int = 0
    turn_count: int = 0
    message_count: int = 0
    total_tokens: int = 0
    repository_identities: tuple[str, ...] = field(default_factory=tuple)


def primary_cwd_digest(primary_cwd: str) -> str:
    """Digest of the workspace root: 32 hex characters, never the path itself.

    ``os.fsencode`` keeps a POSIX path carrying a lone surrogate injective, so
    two different directories can never share a digest.
    """
    if not primary_cwd:
        return ""
    return hashlib.sha256(os.fsencode(primary_cwd)).hexdigest()[:32]


def session_meta_document(meta: ReportMeta) -> dict[str, object]:
    """The ``session/1.0`` whitelist as a JSON-ready mapping."""
    identities = tuple(dict.fromkeys(meta.repository_identities))[:MAX_REPOSITORY_IDENTITIES]
    return {
        "schema": SCHEMA_NAME,
        "sessionId": meta.session_id,
        "title": meta.title,
        "agentProfile": meta.agent_profile,
        "modelProvider": meta.model_provider,
        "modelId": meta.model_id,
        "appVersion": _app_version(),
        "parentSessionId": meta.parent_session_id,
        "primaryCwdDigest": primary_cwd_digest(meta.primary_cwd),
        "createdAt": meta.created_at,
        "updatedAt": meta.updated_at,
        "schemaVersion": meta.schema_version,
        "turnCount": max(0, meta.turn_count),
        "messageCount": max(0, meta.message_count),
        "totalTokens": max(0, meta.total_tokens),
        "repositoryIdentities": list(identities),
    }


def session_meta_payload(meta: ReportMeta) -> bytes:
    """The canonical bytes of one ``session.meta`` unit."""
    return canonical_json_bytes(session_meta_document(meta))


def _app_version() -> str:
    from chrys import __version__

    return __version__
