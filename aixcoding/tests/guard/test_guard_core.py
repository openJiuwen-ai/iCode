# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Behaviour tests for rule-engine paths that need a real filesystem."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from chrys.kernel.aixcoding.guard.pack import _guard_core as guard


def _workspace_symlink(tmp_path: Path) -> tuple[Path, Path]:
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    link = workspace / "link"
    if os.name == "nt":
        # Directory symlinks need Developer Mode/admin; junctions do not and
        # exercise the same canonicalisation path.
        completed = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(outside)],
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            check=False,
        )
        if completed.returncode != 0:
            pytest.skip(f"junction creation is unavailable: {completed.stderr or completed.stdout}")
    else:
        try:
            link.symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            pytest.skip(f"directory symlinks are unavailable: {exc}")
    return workspace, link


def _tool_call(kind: str, name: str, args: dict[str, object], cwd: Path) -> list[str]:
    config = guard.load_config()
    payload = {
        "schema": 1,
        "event": "before_tool_call",
        "session_id": "guard-core",
        "profile": "Code",
        "cwd": str(cwd),
        "tool": {"name": name, "kind": kind, "call_id": "c1", "args": args},
    }
    return [hit.rule_id for hit in guard.evaluate_tool_call(config, payload)]


def test_workspace_symlink_cannot_escape_shell_write_scope(tmp_path: Path) -> None:
    workspace, _link = _workspace_symlink(tmp_path)

    ids = _tool_call("shell", "bash", {"command": "echo x > link/escaped.txt", "reason": ""}, workspace)

    assert "T-SHELL-05" in ids


def test_workspace_symlink_cannot_escape_filesystem_write_scope(tmp_path: Path) -> None:
    workspace, _link = _workspace_symlink(tmp_path)

    ids = _tool_call(
        "filesystem.write",
        "write_file",
        {"path": "link/escaped.txt", "content": "x"},
        workspace,
    )

    assert "T-FS-01" in ids
