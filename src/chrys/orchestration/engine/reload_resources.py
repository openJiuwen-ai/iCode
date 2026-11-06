# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""What a manual reload hands its rebuild besides settings and profiles, and what it reports back."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from chrys.foundation.i18n import DisplayBlock, MessageRef, msg
from chrys.foundation.platform.files import surrogate_safe_text

if TYPE_CHECKING:
    from collections.abc import Sequence

    from chrys.orchestration.session_hooks import HookFileProblem, StrictHooks
    from chrys.service.mcp.cache import MCPRefreshReport

_HOOKS_KEPT = msg(
    "controls.reload_hooks_kept",
    fallback="Hooks were not updated; the previous rules stay in effect.\n{detail}",
    multiline=True,
)
_HOOKS_PARTLY_LOADED = msg(
    "controls.reload_hooks_partly_loaded",
    fallback="Some hooks could not be loaded; the rest are in effect.\n{detail}",
    multiline=True,
)
_HOOK_TRUST_FAILED_LOADED = msg(
    "controls.reload_hook_trust_failed_loaded",
    fallback=(
        "Reload stopped: the hooks the new project trust setting needs can't be used.\n{detail}\n"
        "Settings and hooks stay as they were, with project hooks still loaded."
    ),
    multiline=True,
)
_HOOK_TRUST_FAILED_NOT_LOADED = msg(
    "controls.reload_hook_trust_failed_not_loaded",
    fallback=(
        "Reload stopped: the hooks the new project trust setting needs can't be used.\n{detail}\n"
        "Settings and hooks stay as they were, with project hooks still not loaded."
    ),
    multiline=True,
)
_MCP_KEPT = msg(
    "controls.reload_mcp_kept",
    fallback="MCP server '{server}': reconnect failed, using the existing connection. {detail}",
)
_MCP_UNAVAILABLE = msg("controls.reload_mcp_unavailable", fallback="MCP server '{server}' is unavailable: {detail}")


@dataclass
class ReloadResources:
    """Attached to one manual reload's rebuild permit: the hooks it read and the MCP outcome its commit found."""

    hooks: StrictHooks
    mcp: tuple[MCPRefreshReport, ...] = ()


def _problem_lines(problems: Sequence[HookFileProblem]) -> DisplayBlock:
    lines = (
        ": ".join(part for part in (surrogate_safe_text(problem.path), problem.hook_id, problem.detail) if part)
        for problem in problems
    )
    return DisplayBlock("\n".join(lines))


def hooks_kept_report(problems: Sequence[HookFileProblem], *, rules_in_force: bool) -> MessageRef:
    """What became of hooks a reload could not read whole: running rules stay, and with none running the rest load."""
    definition = _HOOKS_KEPT if rules_in_force else _HOOKS_PARTLY_LOADED
    return definition.bind(detail=_problem_lines(problems))


def hook_trust_failure(problems: Sequence[HookFileProblem], *, project_hooks_loaded: bool) -> MessageRef:
    """Why a reload that changes project hook trust stopped, naming the hooks that stay in force."""
    definition = _HOOK_TRUST_FAILED_LOADED if project_hooks_loaded else _HOOK_TRUST_FAILED_NOT_LOADED
    return definition.bind(detail=_problem_lines(problems))


def mcp_refresh_report(reports: Sequence[MCPRefreshReport]) -> list[MessageRef]:
    return [
        (_MCP_KEPT if report.kept_existing else _MCP_UNAVAILABLE).bind(server=report.server_name, detail=report.detail)
        for report in reports
    ]
