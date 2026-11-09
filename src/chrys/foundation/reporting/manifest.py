# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Missing-block negotiation with ``POST /v1/session/{id}/manifest``.

Reconciliation replaces the old "replay everything" recovery: the client names
the snapshot blocks and workspace files it already knows about, and the server
answers which of them it does not hold. Only the gaps are re-queued, so a lost
cursor costs a query instead of a full re-upload.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from chrys.foundation.reporting.config import MANIFEST_ENDPOINT_TEMPLATE, ReportConfig
from chrys.foundation.reporting.frames import canonical_json_bytes
from chrys.foundation.reporting.pump import ReportTransport


@dataclass(frozen=True, slots=True)
class ManifestRequest:
    """What the client already holds for one session."""

    snapshot_content_hash: str = ""
    chunk_hashes: tuple[str, ...] = ()
    workspace_manifest_sha256: str = ""
    file_shas: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ManifestAnswer:
    """What the server still wants."""

    missing_chunk_hashes: tuple[str, ...] = ()
    missing_file_shas: tuple[str, ...] = ()
    acked_last_sequence: int = 0
    known_snapshot: bool = False

    @classmethod
    def parse(cls, body: bytes) -> ManifestAnswer:
        """Parse the negotiation response."""
        document = json.loads(body.decode("utf-8"))
        if not isinstance(document, dict):
            msg = "manifest response is not a JSON object"
            raise ValueError(msg)

        def _hashes(key: str) -> tuple[str, ...]:
            value = document.get(key, [])
            if not isinstance(value, list):
                msg = f"manifest response field {key!r} is not a list"
                raise ValueError(msg)
            return tuple(item for item in value if isinstance(item, str))

        acked = document.get("acked_last_sequence", 0)
        return cls(
            missing_chunk_hashes=_hashes("missing_chunk_hashes"),
            missing_file_shas=_hashes("missing_file_shas"),
            acked_last_sequence=acked if isinstance(acked, int) and not isinstance(acked, bool) else 0,
            known_snapshot=document.get("known_snapshot") is True,
        )


class ManifestReconciler:
    """Ask the server which blocks are missing; never mutates local state."""

    def __init__(self, config: ReportConfig, transport: ReportTransport) -> None:
        self._config = config
        self._transport = transport

    def reconcile(self, session_id: str, request: ManifestRequest) -> ManifestAnswer | None:
        """Return the server's answer, or ``None`` when it could not be asked.

        A failed negotiation only postpones the optimisation: the client keeps
        its cursor and its blocks, and the next session open asks again.
        """
        body = json.dumps(manifest_request_document(request), separators=(",", ":"), ensure_ascii=True).encode()
        url = self._config.url(MANIFEST_ENDPOINT_TEMPLATE.format(session_id=session_id))
        result = self._transport.post_manifest(
            url, self._config.token, body, timeout=self._config.upload_timeout_seconds
        )
        if result.status != 200:
            return None
        try:
            return ManifestAnswer.parse(result.body)
        except ValueError, UnicodeDecodeError:
            return None


def manifest_request_document(request: ManifestRequest) -> dict[str, object]:
    """The JSON document the manifest endpoint reads."""
    return {
        "snapshot": {"content_hash": request.snapshot_content_hash, "chunk_hashes": list(request.chunk_hashes)},
        "workspace": {
            "manifest_sha256": request.workspace_manifest_sha256,
            "file_shas": list(request.file_shas),
        },
    }


def canonical_manifest_request(request: ManifestRequest) -> bytes:
    """The request's canonical bytes, handy for tests and diagnostics."""
    return canonical_json_bytes(manifest_request_document(request))
