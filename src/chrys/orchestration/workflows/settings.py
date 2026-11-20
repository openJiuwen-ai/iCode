# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Shared target-workspace settings and fallback-model resolution for preview and execution."""

from __future__ import annotations

import asyncio
from dataclasses import asdict, replace
from pathlib import Path
from typing import TYPE_CHECKING

from chrys.foundation.config.context import EvalContext
from chrys.foundation.config.process_settings import reattribute_command_line, route_restart_settings
from chrys.foundation.config.settings import Settings
from chrys.foundation.config.settings_store import LoadedSettings, load_settings
from chrys.foundation.config.spec import Source, specs_by_field
from chrys.foundation.models.workflow_session import WorkflowModelSelection
from chrys.service.profiles.models.resolver import resolve_active_profile
from chrys.service.workflows.admission import admit_manifest
from chrys.service.workflows.model_selection import resolve_workflow_model

if TYPE_CHECKING:
    from chrys.foundation.config.settings_store import SettingsHandle
    from chrys.foundation.models.workflow_session import WorkspaceSnapshot
    from chrys.service.profiles.agents.registry import AgentProfileRegistry
    from chrys.service.profiles.models.registry import ModelProfileRegistry


# Live Chat overrides belong to Chat; only launch selectors cross the boundary.
_SHARED_STARTUP_FIELDS = frozenset({"model_profile", "ask_user_timeout_seconds", "max_transient_retries"})


async def load_workflow_settings(workdir: Path, *, startup: LoadedSettings, handle: SettingsHandle) -> LoadedSettings:
    if not startup.provenance:
        # Embedders can supply an explicit Settings object instead of a layered load.
        effective = startup.overlay(Source.SESSION, default_approval_mode=handle.settings.default_approval_mode)
    else:
        values = asdict(startup.settings)
        pins = {
            name: values[name]
            for name, spec in specs_by_field(Settings).items()
            if name in _SHARED_STARTUP_FIELDS and startup.source_for(spec.key).layer in {Source.CLI, Source.SESSION}
        }
        candidate = await asyncio.to_thread(
            load_settings,
            workdir=workdir,
            eval_context=EvalContext(
                frontend_default_max_transient_retries=startup.settings.frontend_default_max_transient_retries
            ),
            **pins,
        )
        effective, _ = route_restart_settings(reattribute_command_line(candidate, startup), startup)
    # This is a global default persisted by both modes, not a session's
    # active approval policy. No other Chat runtime overrides are inherited.
    live = handle.loaded
    if live.source_for("approval.default_mode").layer is Source.RUNTIME:
        effective = effective.overlay(Source.RUNTIME, default_approval_mode=live.settings.default_approval_mode)
    return replace(
        effective,
        settings=replace(effective.settings, model_profile_override="", model_profile_override_sub_agents=False),
    )


def admission_settings(
    settings: Settings, selected: WorkflowModelSelection | None, registry: ModelProfileRegistry | None
) -> tuple[Settings, WorkflowModelSelection | None]:
    selector = selected.profile_id if selected is not None else resolve_active_profile(registry, settings).id
    try:
        model = resolve_workflow_model(registry, selector)
    except ValueError:
        # A missing unused default must not block explicit node models, Python or ACP.
        model = selected
    return replace(settings, model_profile=selector), model


async def preview_bindings(
    manifest: dict,
    *,
    workspace: WorkspaceSnapshot,
    selected: WorkflowModelSelection | None,
    agent_registry: AgentProfileRegistry,
    model_registry: ModelProfileRegistry | None,
    settings_handle: SettingsHandle,
    startup: LoadedSettings,
) -> list[dict]:
    loaded = await load_workflow_settings(Path(workspace.primary_cwd), startup=startup, handle=settings_handle)
    settings, _model = admission_settings(loaded.settings, selected, model_registry)
    admitted = await asyncio.to_thread(
        admit_manifest,
        manifest,
        agent_registry=agent_registry,
        model_registry=model_registry,
        settings=settings,
    )
    return admitted.resolved_nodes()
