# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Strict workflow-session state codec, independent of Chat history serialization."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from chrys.foundation.models.session_surface import SessionSurface, parse_session_surface
from chrys.foundation.models.workdir_migration import source_edit
from chrys.foundation.models.workflow_session import (
    WorkflowIdentity,
    WorkflowModelSelection,
    WorkflowSessionSelection,
    WorkingDirSnapshot,
    WorkspaceSnapshot,
)
from chrys.service.session.runtime_metadata import SessionUsageMetadata
from chrys.service.workflows.discovery import SOURCE_KINDS


def _mapping(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"Invalid workflow state: {name} must be an object.")
    return value


def _text(value: Any, name: str, *, empty: bool = False, nonblank: bool = False) -> str:
    if (
        not isinstance(value, str)
        or (not empty and not value)
        or (nonblank and isinstance(value, str) and not value.strip())
    ):
        raise ValueError(f"Invalid workflow state: {name} must be a string.")
    return value


def _count(value: Any, name: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"Invalid workflow state: {name} must be a nonnegative integer.")
    return value


def _sequence(value: Any, name: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"Invalid workflow state: {name} must be an array.")
    return value


def _raw_surface(value: Any) -> str | None:
    # Lenient on purpose: a malformed surface only loses its browser filter.
    return value if isinstance(value, str) and value else None


@dataclass
class WorkflowSessionState:
    """Durable session identity and settings survive every run-resource lifetime."""

    identity: WorkflowIdentity
    workspace: WorkspaceSnapshot
    run_count: int = 0
    latest_run_id: str = ""
    runtime: SessionUsageMetadata | None = None
    mutations: dict[str, Any] | None = None
    model: WorkflowModelSelection | None = None
    # Set when a run is admitted, so a discarded admission restores the previous value.
    # Kept raw so a surface a newer version recorded survives this one's saves; read it via ``surface``.
    last_surface: str | None = None

    @property
    def surface(self) -> SessionSurface | None:
        return parse_session_surface(self.last_surface)

    def selection(self, session_id: str) -> WorkflowSessionSelection:
        return WorkflowSessionSelection(session_id, self.identity, self.workspace, self.model)

    @classmethod
    def decode(cls, value: Any) -> WorkflowSessionState:
        data = _mapping(value, "state")
        identity_data = _mapping(data.get("identity"), "identity").copy()
        workspace = _mapping(data.get("workspace"), "workspace")
        summary = _mapping(data.get("summary"), "summary")
        source_edit(identity_data)
        source_kind = _text(identity_data.get("source_kind"), "source_kind")
        if source_kind not in SOURCE_KINDS:
            raise ValueError("Invalid workflow state: unknown source_kind.")
        identity = WorkflowIdentity(
            _text(identity_data.get("workflow_id"), "workflow_id", nonblank=True),
            _text(identity_data.get("canonical_path"), "canonical_path"),
            source_kind,
        )
        directories = []
        for value in _sequence(workspace.get("working_dirs"), "working_dirs"):
            directory = _mapping(value, "working directory")
            primary = directory.get("is_primary")
            if type(primary) is not bool:
                raise ValueError("Invalid workflow state: is_primary must be a boolean.")
            directories.append(
                WorkingDirSnapshot(
                    _text(directory.get("path"), "directory path"),
                    _text(directory.get("label"), "directory label", empty=True),
                    primary,
                )
            )
        snapshot = WorkspaceSnapshot(
            _text(workspace.get("primary_cwd"), "primary_cwd"),
            tuple(directories),
            tuple(
                _text(item, "reference file") for item in _sequence(workspace.get("reference_files"), "reference_files")
            ),
        )
        run_count = _count(summary.get("run_count"), "run_count")
        latest = _text(summary.get("latest_run_id"), "latest_run_id", empty=run_count == 0)
        if run_count == 0 and latest:
            raise ValueError("Invalid workflow state: a session without runs cannot name a latest run.")
        if latest and (latest in {".", ".."} or "/" in latest or "\\" in latest):
            raise ValueError("Invalid workflow state: latest_run_id must name a run.")
        for key in ("total_session_tokens", "total_session_input_tokens", "total_session_output_tokens"):
            _count(data.get(key, 0), key)
        cached = data.get("total_session_cache_hit_tokens")
        if cached is not None:
            _count(cached, "total_session_cache_hit_tokens")
        mutations = _mapping(data["chrys_mutations"], "chrys_mutations") if "chrys_mutations" in data else None
        return cls(
            identity,
            snapshot,
            run_count,
            latest,
            SessionUsageMetadata.from_state_dict(data),
            mutations,
            decode_workflow_model(data.get("model")),
            _raw_surface(data.get("last_surface")),
        )

    def encode(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "identity": asdict(self.identity),
            "workspace": {
                "primary_cwd": self.workspace.primary_cwd,
                "working_dirs": [asdict(directory) for directory in self.workspace.working_dirs],
                "reference_files": list(self.workspace.reference_files),
            },
            "model": asdict(self.model) if self.model is not None else None,
            "summary": {"run_count": self.run_count, "latest_run_id": self.latest_run_id},
            **(self.runtime or SessionUsageMetadata()).to_state_dict(),
        }
        if self.mutations is not None:
            result["chrys_mutations"] = self.mutations
        if self.last_surface is not None:
            result["last_surface"] = str(self.last_surface)
        return result


def decode_workflow_model(value: object) -> WorkflowModelSelection | None:
    if value is None:
        return None
    record = _mapping(value, "model")
    return WorkflowModelSelection(
        *(_text(record.get(key), key, nonblank=True) for key in ("profile_id", "name", "model_id"))
    )
