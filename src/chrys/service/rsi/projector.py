# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""RSI tree/progress projector (push/pull share the same cache)."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from chrys.foundation.platform.files import atomic_write_text
from chrys.service.rsi.models import RsiTreeNode

_ROOT = "ROOT"


class RsiProjector:
    """In-memory + ``tree.json`` projection of provider node/progress events."""

    def __init__(self, tasks_root: Path) -> None:
        self.tasks_root = Path(tasks_root)
        self._lock = threading.RLock()
        self._nodes: dict[str, dict[str, RsiTreeNode]] = {}
        self._metric: dict[str, dict[str, Any]] = {}
        self._ref_index: dict[str, dict[str, str]] = {}

    def register_root(self, task_id: str, *, baseline: float | None = None, description: str = "baseline") -> None:
        with self._lock:
            nodes = self._nodes.setdefault(task_id, {})
            if _ROOT in nodes:
                return
            nodes[_ROOT] = RsiTreeNode(
                node_id=_ROOT,
                iteration=0,
                parent_id=None,
                type="ROOT",
                adopted=True,
                score=baseline,
                description=description,
            )
            self._ref_index.setdefault(task_id, {})["root"] = _ROOT
            self._persist_locked(task_id)

    def on_node_created(self, task_id: str, payload: dict[str, Any]) -> RsiTreeNode | None:
        node = payload.get("node") if isinstance(payload.get("node"), dict) else payload
        if not isinstance(node, dict):
            return None
        node_ref = str(node.get("ref") or "")
        if not node_ref:
            return None
        with self._lock:
            nodes = self._nodes.setdefault(task_id, {})
            derived_seq = sum(1 for item in nodes.values() if item.node_id != _ROOT)
            node_id = _ROOT if node_ref == "root" else f"N{derived_seq + 1}"
            if node_id in nodes:
                return nodes[node_id]
            parent_ref = node.get("parent_ref")
            parent_id = self._map_parent_id(task_id, parent_ref)
            tree_node = RsiTreeNode(
                node_id=node_id,
                iteration=len(nodes),
                parent_id=parent_id,
                type=self._map_type(node),
                adopted=bool(node.get("accepted", False) or node.get("adopted", False)),
                score=_safe_float(node.get("score")),
                description=str(node.get("summary") or node.get("description") or "") or None,
                failure_reason=str(node.get("failure_reason") or "") or None,
                failure_class=str(node.get("failure_class") or "") or None,
            )
            nodes[node_id] = tree_node
            self._ref_index.setdefault(task_id, {})[node_ref] = node_id
            self._persist_locked(task_id)
            return tree_node

    def on_progress_metric(self, task_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            metric = {
                "iteration": _safe_int(payload.get("iteration")),
                "total_iterations": _safe_int(payload.get("total_iterations")),
                "score": _safe_float(payload.get("score")),
                "baseline": _safe_float(payload.get("baseline")),
            }
            self._metric[task_id] = metric
            return dict(metric)

    def derive_progress(self, task_id: str) -> dict[str, Any] | None:
        with self._lock:
            metric = self._metric.get(task_id)
            return dict(metric) if metric else None

    def derive_tree(self, task_id: str) -> dict[str, Any]:
        with self._lock:
            self._ensure_loaded_locked(task_id)
            nodes = self._nodes.get(task_id) or {}
            return {
                "task_id": task_id,
                "nodes": [node.to_dict() for node in nodes.values()],
            }

    def load_from_disk(self, task_id: str) -> None:
        with self._lock:
            self._ensure_loaded_locked(task_id)

    def _map_parent_id(self, task_id: str, parent_ref: Any) -> str | None:
        if parent_ref is None or parent_ref == "":
            return _ROOT
        mapped = self._ref_index.get(task_id, {}).get(str(parent_ref))
        return mapped or _ROOT

    def _map_type(self, node: dict[str, Any]) -> str:
        raw = str(node.get("type") or "").strip().upper()
        if raw in {"ROOT", "ADOPTED", "REJECTED", "PROVISIONAL", "PRUNED"}:
            return raw
        if node.get("accepted") or node.get("adopted"):
            return "ADOPTED"
        return "PROVISIONAL"

    def _tree_path(self, task_id: str) -> Path:
        return Path(self.tasks_root) / task_id / "tree.json"

    def _persist_locked(self, task_id: str) -> None:
        nodes = self._nodes.get(task_id) or {}
        payload = {
            "task_id": task_id,
            "nodes": [node.to_dict() for node in nodes.values()],
            "refs": dict(self._ref_index.get(task_id) or {}),
            "metric": dict(self._metric.get(task_id) or {}),
        }
        path = self._tree_path(task_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(path, json.dumps(payload, ensure_ascii=True, indent=2) + "\n")

    def _ensure_loaded_locked(self, task_id: str) -> None:
        if task_id in self._nodes:
            return
        path = self._tree_path(task_id)
        if not path.is_file():
            self._nodes[task_id] = {}
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except OSError, ValueError, TypeError, json.JSONDecodeError:
            self._nodes[task_id] = {}
            return
        nodes: dict[str, RsiTreeNode] = {}
        for item in data.get("nodes") or []:
            if not isinstance(item, dict):
                continue
            node = RsiTreeNode.from_dict(item)
            if node.node_id:
                nodes[node.node_id] = node
        self._nodes[task_id] = nodes
        refs = data.get("refs")
        if isinstance(refs, dict):
            self._ref_index[task_id] = {str(key): str(value) for key, value in refs.items()}
        metric = data.get("metric")
        if isinstance(metric, dict):
            self._metric[task_id] = dict(metric)


def _safe_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except TypeError, ValueError:
        return None


def _safe_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except TypeError, ValueError:
        return None
