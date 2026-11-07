# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import json
from dataclasses import dataclass, field

import pytest

from chrys.foundation.reporting.config import ReportConfig
from chrys.foundation.reporting.manifest import (
    ManifestAnswer,
    ManifestReconciler,
    ManifestRequest,
    manifest_request_document,
)
from chrys.foundation.reporting.pump import TransportResult


@dataclass
class _Transport:
    result: TransportResult
    urls: list[str] = field(default_factory=list)
    bodies: list[bytes] = field(default_factory=list)

    def post_batch(self, url: str, token: str, body: bytes, *, gzip_encoded: bool, timeout: float) -> TransportResult:
        return self.result

    def post_manifest(self, url: str, token: str, body: bytes, *, timeout: float) -> TransportResult:
        self.urls.append(url)
        self.bodies.append(body)
        return self.result

    def close(self) -> None:
        return


def _answer(**overrides: object) -> TransportResult:
    document: dict[str, object] = {
        "missing_chunk_hashes": ["b"],
        "missing_file_shas": ["f"],
        "acked_last_sequence": 12,
        "known_snapshot": True,
    }
    document.update(overrides)
    return TransportResult(status=200, body=json.dumps(document).encode())


def test_the_request_names_the_blocks_the_client_already_holds() -> None:
    request = ManifestRequest(
        snapshot_content_hash="content",
        chunk_hashes=("a", "b"),
        workspace_manifest_sha256="manifest",
        file_shas=("f",),
    )

    assert manifest_request_document(request) == {
        "snapshot": {"content_hash": "content", "chunk_hashes": ["a", "b"]},
        "workspace": {"manifest_sha256": "manifest", "file_shas": ["f"]},
    }


def test_the_answer_parses_the_missing_blocks_and_the_acknowledged_sequence() -> None:
    answer = ManifestAnswer.parse(_answer().body)

    assert answer.missing_chunk_hashes == ("b",)
    assert answer.missing_file_shas == ("f",)
    assert answer.acked_last_sequence == 12
    assert answer.known_snapshot is True


@pytest.mark.parametrize("body", [b"[]", b'{"missing_chunk_hashes": "b"}', b'{"missing_file_shas": 3}'])
def test_a_malformed_answer_is_refused(body: bytes) -> None:
    with pytest.raises(ValueError):
        ManifestAnswer.parse(body)


def test_reconcile_posts_to_the_session_manifest_endpoint(report_config: ReportConfig) -> None:
    transport = _Transport(_answer())
    reconciler = ManifestReconciler(report_config, transport)

    answer = reconciler.reconcile("session-1", ManifestRequest(snapshot_content_hash="content"))

    assert answer is not None
    assert transport.urls == [f"{report_config.endpoint}/v1/session/session-1/manifest"]
    assert json.loads(transport.bodies[0])["snapshot"]["content_hash"] == "content"


@pytest.mark.parametrize("result", [TransportResult(status=500), TransportResult(status=0)])
def test_a_failed_negotiation_returns_none(report_config: ReportConfig, result: TransportResult) -> None:
    reconciler = ManifestReconciler(report_config, _Transport(result))

    assert reconciler.reconcile("session-1", ManifestRequest()) is None


def test_an_empty_answer_leaves_the_client_holding_everything(report_config: ReportConfig) -> None:
    reconciler = ManifestReconciler(report_config, _Transport(TransportResult(status=200, body=b"{}")))

    answer = reconciler.reconcile("session-1", ManifestRequest())

    assert answer is not None
    assert answer == ManifestAnswer()
