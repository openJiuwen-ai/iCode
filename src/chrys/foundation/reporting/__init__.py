# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Transport-only support for the ``report/1.0`` fact channel.

Nothing here knows what a trajectory event or a workspace is: this package
moves opaque bytes — frame codec, durable queue, sender, block negotiation and
the settings projection. The content collectors live in
``chrys.service.reporting``.

Importing this package costs nothing: the queue, the sender and ``httpx`` are
all reached through submodules, and the runtime only imports them after
``report.enabled`` says so.
"""

from __future__ import annotations

from chrys.foundation.reporting.config import ReportConfig
from chrys.foundation.reporting.diagnostics import DiagnosticsSnapshot, ReportDiagnostics
from chrys.foundation.reporting.frames import (
    KIND_AGENT_ENV,
    KIND_REPO_IDENTITY,
    KIND_SESSION_META,
    KIND_SNAPSHOT_CHUNK,
    KIND_SNAPSHOT_MANIFEST,
    KIND_TRAJECTORY_LINES,
    KIND_WORKSPACE_FILE,
    KIND_WORKSPACE_MANIFEST,
    Frame,
    FrameError,
    FrameHeader,
    canonical_json_bytes,
    decode_frames,
    encode_frames,
    sha256_hex,
    unit_id_for,
)

__all__ = [
    "KIND_AGENT_ENV",
    "KIND_REPO_IDENTITY",
    "KIND_SESSION_META",
    "KIND_SNAPSHOT_CHUNK",
    "KIND_SNAPSHOT_MANIFEST",
    "KIND_TRAJECTORY_LINES",
    "KIND_WORKSPACE_FILE",
    "KIND_WORKSPACE_MANIFEST",
    "DiagnosticsSnapshot",
    "Frame",
    "FrameError",
    "FrameHeader",
    "ReportConfig",
    "ReportDiagnostics",
    "canonical_json_bytes",
    "decode_frames",
    "encode_frames",
    "sha256_hex",
    "unit_id_for",
]
