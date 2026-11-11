# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""RSI (recursive self-improvement) service domain.

Phase 1: task store, single-slot worker, tree projector, mock provider.
Phase 2: script_based dataset eval (stub or headless CaseRunner).
Phase 3: profile/skills mutations, gated optimize loop, explicit install export.
The default proposer is a stub. ``LlmOptimizer`` proposes the same whitelist from
failure summaries and is selected by the CLI (``--agent`` / ``--optimizer-model``).
"""

from __future__ import annotations

from chrys.service.rsi.context import RsiServiceContext, build_rsi_service_context
from chrys.service.rsi.dataset import RsiDataset, load_dataset, validate_dataset_file
from chrys.service.rsi.errors import (
    RsiBadRequest,
    RsiDatasetInvalid,
    RsiError,
    RsiNotFound,
    RsiTaskCorrupt,
    RsiTaskNotFound,
    RsiTaskStateConflict,
)
from chrys.service.rsi.models import RsiTask, RsiTaskView, RsiTreeNode, TaskStatus
from chrys.service.rsi.optimizer import OptimizeResult, StubOptimizer
from chrys.service.rsi.paths import resolve_rsi_tasks_root
from chrys.service.rsi.provider import MockRsiProvider, RsiProvider

__all__ = [
    "MockRsiProvider",
    "OptimizeResult",
    "RsiBadRequest",
    "RsiDataset",
    "RsiDatasetInvalid",
    "RsiError",
    "RsiNotFound",
    "RsiProvider",
    "RsiServiceContext",
    "RsiTask",
    "RsiTaskCorrupt",
    "RsiTaskNotFound",
    "RsiTaskStateConflict",
    "RsiTaskView",
    "RsiTreeNode",
    "StubOptimizer",
    "TaskStatus",
    "build_rsi_service_context",
    "load_dataset",
    "resolve_rsi_tasks_root",
    "validate_dataset_file",
]
