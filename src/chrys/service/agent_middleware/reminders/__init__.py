# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The content sources ``SystemReminderMiddleware`` composes, one module per reminder.

A source owns what one reminder says and the state behind it.  The middleware
(``system_reminder``) owns when each one is sent, how it is recorded and the
send order; sources never build reminder items or records.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from .archive_pointer import ArchivePointerSource
from .background_tasks import BackgroundTasksSource
from .context_usage import ContextWarningSource
from .extension_context import ExtensionContextSource
from .file_change import FileChangeSource
from .mcp import McpSource
from .profile_switch import ProfileSwitchSource
from .runtime_env import RuntimeEnvSource
from .skills import SkillsSource
from .sub_agents import SubAgentsSource
from .todo import TodoSource
from .turn_line import TurnLineSource

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path

    from chrys.foundation.models.session_env import SessionEnvironment
    from chrys.service.context.compaction.spill import SpillQuota
    from chrys.service.extensions import BoundContext


@dataclass(frozen=True, slots=True)
class ReminderSources:
    """One middleware's sources, one named field per reminder.

    The middleware drives their turn transitions; code outside it uses only the
    members ``tests/architecture/test_hygiene_reminder_sources.py`` allows.
    """

    turn_line: TurnLineSource
    context_warning: ContextWarningSource
    runtime_env: RuntimeEnvSource
    sub_agents: SubAgentsSource
    todo: TodoSource
    background_tasks: BackgroundTasksSource
    skills: SkillsSource
    mcp: McpSource
    file_change: FileChangeSource
    profile_switch: ProfileSwitchSource
    archive_pointer: ArchivePointerSource
    extension_context: ExtensionContextSource


def build_sources(
    *,
    runtime: SessionEnvironment | None,
    max_context_tokens: int,
    warn_threshold_pct: float,
    shell_tool_enabled: bool,
    sub_agent_names: Sequence[str] | None,
    todo_state_provider: Callable[[], str | None] | None,
    background_tasks_provider: Callable[[], str | None] | None,
    skill_catalog_provider: Callable[[], str | None] | None,
    mcp_instructions_provider: Callable[[], str | None] | None,
    file_change_provider: Callable[[], str | None] | None,
    tool_names: Sequence[str] | None,
    session_root: Path | None,
    file_read_available: bool,
    spill_quota: SpillQuota | None,
    catalog_pointer_enabled: bool,
    extension_contexts: Sequence[BoundContext],
) -> ReminderSources:
    """The sources for one middleware, from its constructor inputs."""
    return ReminderSources(
        turn_line=TurnLineSource(runtime, max_context_tokens=max_context_tokens),
        context_warning=ContextWarningSource(
            max_context_tokens=max_context_tokens,
            warn_threshold_pct=warn_threshold_pct,
        ),
        runtime_env=RuntimeEnvSource(runtime, shell_tool_enabled=shell_tool_enabled),
        sub_agents=SubAgentsSource(sub_agent_names),
        todo=TodoSource(todo_state_provider),
        background_tasks=BackgroundTasksSource(background_tasks_provider),
        skills=SkillsSource(skill_catalog_provider),
        mcp=McpSource(mcp_instructions_provider),
        file_change=FileChangeSource(file_change_provider),
        profile_switch=ProfileSwitchSource(tool_names),
        archive_pointer=ArchivePointerSource(
            session_root=session_root,
            file_read_available=file_read_available,
            spill_quota=spill_quota,
            enabled=catalog_pointer_enabled,
        ),
        extension_context=ExtensionContextSource(extension_contexts),
    )
