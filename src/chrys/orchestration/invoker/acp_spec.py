# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""ACP transport specification assembly with explicit caller workspace inputs."""

from __future__ import annotations

import os
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from chrys.foundation.util.env_templates import resolve_env_templates
from chrys.service.acp_client import AcpAgentSpec, AcpConfigError

if TYPE_CHECKING:
    from chrys.foundation.models.session_env import SessionEnvironment
    from chrys.service.profiles.agents.schema import AcpAgentConfig


def resolve_acp_spec(
    config: AcpAgentConfig,
    runtime: SessionEnvironment,
    stderr_path: Path,
    *,
    workspace_roots: Sequence[str],
) -> AcpAgentSpec:
    """Resolve each fresh ACP launch with the caller's existing workspace policy."""
    try:
        # The executable and working directory decide what runs and where, so a command can't supply them.
        command = resolve_env_templates(config.command, location="ACP command", allow_commands=False)
        args = tuple(resolve_env_templates(arg, location="ACP argument") for arg in config.args)
        env = {
            key: resolve_env_templates(value, location=f"ACP environment {key!r}") for key, value in config.env.items()
        }
        cwd_raw = (
            resolve_env_templates(config.cwd, location="ACP cwd", allow_commands=False) if config.cwd else runtime.cwd
        )
    except ValueError as exc:
        raise AcpConfigError(str(exc), cause=exc) from exc
    if any("\x00" in value for value in (command, *args, cwd_raw, *env.values())):
        raise AcpConfigError("ACP launch values cannot contain embedded NUL characters.")
    cwd_candidate = Path(cwd_raw)
    if not cwd_candidate.is_absolute():
        cwd_candidate = Path(runtime.cwd) / cwd_candidate
    cwd = os.path.realpath(cwd_candidate)
    resolved_roots: list[str] = []
    seen_roots: set[str] = set()
    for root in workspace_roots or [runtime.cwd]:
        resolved = os.path.realpath(root)
        folded = os.path.normcase(resolved)
        if folded not in seen_roots:
            seen_roots.add(folded)
            resolved_roots.append(resolved)

    def is_within_root(root: str) -> bool:
        try:
            return os.path.commonpath([os.path.normcase(cwd), os.path.normcase(root)]) == os.path.normcase(root)
        except ValueError:
            return False

    if not config.allow_external_cwd and not any(is_within_root(root) for root in resolved_roots):
        raise AcpConfigError("ACP cwd resolves outside the active workspace.")
    depth_raw = os.environ.get("CHRYS_ACP_SUBAGENT_DEPTH", "0")
    try:
        depth = max(0, int(depth_raw))
    except ValueError:
        depth = 0
    return AcpAgentSpec(
        command=command,
        args=args,
        env=env,
        cwd=cwd,
        stderr_log_path=stderr_path,
        additional_directories=tuple(root for root in resolved_roots if root != cwd),
        session_mode=config.session_mode,
        model_id=config.model_id,
        config_options=dict(config.config_options),
        best_effort_options=config.best_effort_options,
        handshake_timeout_seconds=config.handshake_timeout_seconds,
        idle_timeout_seconds=config.idle_timeout_seconds,
        depth=depth,
    )
