# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Profile discovery commands for the Chrys CLI."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass

from rich.console import Console
from rich.table import Table
from rich.text import Text

from chrys.foundation.branding import APP_COMMAND, APP_DISPLAY_NAME
from chrys.foundation.config.settings import DEFAULT_AGENT_PROFILE, Settings
from chrys.foundation.config.warnings import settings_warning_events
from chrys.foundation.events.types import Warning
from chrys.orchestration.startup import bootstrap_runtime
from chrys.service.profiles.agents.registry import AgentProfileRegistry
from chrys.service.profiles.agents.registry import resolve_profile_selector as resolve_agent_profile_selector
from chrys.service.profiles.agents.schema import AgentProfile
from chrys.service.profiles.models.registry import ModelProfileRegistry
from chrys.service.profiles.models.resolver import resolve_profile_selector as resolve_model_profile_selector
from chrys.service.profiles.models.schema import API_STYLE_CHAT_COMPLETIONS, ModelProfile


@dataclass(frozen=True)
class _AgentRow:
    profile: AgentProfile
    is_default: bool
    source: str
    model: str


@dataclass(frozen=True)
class _ModelRow:
    profile: ModelProfile
    is_active: bool
    flags: str


def build_agents_parser() -> argparse.ArgumentParser:
    """Build the ``chrys agent`` parser."""
    parser = argparse.ArgumentParser(
        prog=f"{APP_COMMAND} agent",
        description=f"List {APP_DISPLAY_NAME} agent profiles that can be selected with --agent.",
        add_help=False,
    )
    parser.add_argument(
        "-h", "--help", action="help", default=argparse.SUPPRESS, help="Show this help message and exit"
    )
    parser.add_argument(
        "command", nargs="?", choices=["list"], default="list", help="List the agent profiles (the default)"
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON output")
    return parser


def build_models_parser() -> argparse.ArgumentParser:
    """Build the ``chrys model`` parser."""
    parser = argparse.ArgumentParser(
        prog=f"{APP_COMMAND} model",
        description=f"List {APP_DISPLAY_NAME} model profiles that can be selected with --model.",
        add_help=False,
    )
    parser.add_argument(
        "-h", "--help", action="help", default=argparse.SUPPRESS, help="Show this help message and exit"
    )
    parser.add_argument(
        "command", nargs="?", choices=["list"], default="list", help="List the model profiles (the default)"
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON output")
    return parser


def agents_main(argv: list[str] | None = None) -> int:
    """Entry point for ``chrys agent``."""
    parser = build_agents_parser()
    args = parser.parse_args(argv)
    _ = args.command

    settings = _prepare_runtime()
    agent_registry = AgentProfileRegistry()
    model_registry = ModelProfileRegistry()
    agent_registry.load_all()
    model_registry.load_all()

    profiles = sorted(
        agent_registry.list_profiles(include_sub_agent_only=True),
        key=lambda profile: profile.name.casefold(),
    )
    default_name = _default_agent_name(profiles, settings)
    rows = [
        _AgentRow(
            profile=profile,
            is_default=profile.name == default_name,
            source="builtin" if agent_registry.is_builtin(profile.name) else "user",
            model=_agent_model_label(profile, model_registry),
        )
        for profile in profiles
    ]

    if args.json:
        _write_json({"agents": [_agent_payload(row) for row in rows], "default": default_name})
        return 0

    _print_agents(rows)
    return 0


def models_main(argv: list[str] | None = None) -> int:
    """Entry point for ``chrys model``."""
    parser = build_models_parser()
    args = parser.parse_args(argv)
    _ = args.command

    settings = _prepare_runtime()
    registry = ModelProfileRegistry()
    registry.load_all()

    active = resolve_model_profile_selector(registry, settings.model_profile) if settings.model_profile else None
    active_id = active.id if active is not None else ""
    profiles = sorted(registry.list_profiles(), key=lambda profile: (profile.name.casefold(), profile.id))
    rows = [
        _ModelRow(profile=profile, is_active=profile.id == active_id, flags=_model_flags(profile))
        for profile in profiles
    ]

    if args.json:
        _write_json({"models": [_model_payload(row) for row in rows], "active": active_id})
        return 0

    _print_models(rows)
    return 0


def _prepare_runtime() -> Settings:
    bootstrap = bootstrap_runtime(configure_stdio=True, setup_telemetry=False)
    for warning in (*bootstrap.warnings, *settings_warning_events(bootstrap.loaded)):
        _write_warning(warning)
    return bootstrap.settings


def _write_warning(warning: Warning) -> None:
    sys.stderr.write(f"Warning: {warning.message}\n")


def _default_agent_name(profiles: list[AgentProfile], settings: Settings) -> str:
    profiles = [profile for profile in profiles if profile.acp is None and not profile.sub_agent_only]
    requested = settings.default_agent.strip() or DEFAULT_AGENT_PROFILE
    profile = resolve_agent_profile_selector(profiles, requested)
    if profile is not None:
        return profile.name
    if requested != DEFAULT_AGENT_PROFILE:
        profile = resolve_agent_profile_selector(profiles, DEFAULT_AGENT_PROFILE)
        if profile is not None:
            return profile.name
    return profiles[0].name if profiles else ""


def _agent_model_label(profile: AgentProfile, registry: ModelProfileRegistry) -> str:
    profile_id = profile.model.profile_id
    if not profile_id:
        return "active"
    model = registry.get(profile_id)
    if model is None:
        return f"missing:{profile_id}"
    return model.name


def _model_flags(profile: ModelProfile) -> str:
    flags: list[str] = []
    if profile.stream:
        flags.append("stream")
    if profile.vision:
        flags.append("vision")
    return ", ".join(flags) if flags else "-"


def _format_context(tokens: int) -> str:
    if tokens >= 1_000_000 and tokens % 1_000_000 == 0:
        return f"{tokens // 1_000_000}m"
    if tokens >= 1_000 and tokens % 1_000 == 0:
        return f"{tokens // 1_000}k"
    return str(tokens)


def _format_api_style(api_style: str) -> str:
    if api_style == API_STYLE_CHAT_COMPLETIONS:
        return "chat"
    return api_style


def _write_json(payload: dict[str, object]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False))
    sys.stdout.write("\n")


def _agent_payload(row: _AgentRow) -> dict[str, object]:
    profile = row.profile
    return {
        "id": profile.id,
        "name": profile.name,
        "displayName": profile.display_name,
        "description": profile.description,
        "default": row.is_default,
        "model": row.model,
        "source": row.source,
        "kind": "acp" if profile.acp is not None else "kernel",
        "subAgentOnly": profile.sub_agent_only,
        "launchableAsMain": profile.acp is None and not profile.sub_agent_only,
    }


def _model_payload(row: _ModelRow) -> dict[str, object]:
    profile = row.profile
    return {
        "id": profile.id,
        "name": profile.name,
        "provider": profile.provider,
        "apiStyle": profile.api_style,
        "modelId": profile.model_id,
        "maxContextTokens": profile.max_context_tokens,
        "stream": profile.stream,
        "vision": profile.vision,
        "active": row.is_active,
    }


def _print_agents(rows: list[_AgentRow]) -> None:
    console = Console(file=sys.stdout, highlight=False)
    if not rows:
        console.print(Text("No agent profiles found."))
        return

    table = Table(box=None, show_edge=False, pad_edge=False)
    table.add_column("Default", no_wrap=True, justify="center")
    table.add_column("Name", no_wrap=True)
    table.add_column("Display Name", overflow="fold")
    table.add_column("ID", no_wrap=True)
    table.add_column("Model", overflow="fold")
    table.add_column("Source", no_wrap=True)
    table.add_column("Description", overflow="fold")
    for row in rows:
        profile = row.profile
        table.add_row(
            Text("*" if row.is_default else ""),
            Text(profile.name),
            Text(profile.display_name or "-"),
            Text(profile.id or "-"),
            Text(row.model),
            Text(row.source),
            Text(profile.description or "-"),
        )
    console.print(table)


def _print_models(rows: list[_ModelRow]) -> None:
    console = Console(file=sys.stdout, highlight=False)
    if not rows:
        console.print(Text("No model profiles found."))
        return

    table = Table(box=None, show_edge=False, pad_edge=False)
    table.add_column("Active", no_wrap=True, justify="center")
    table.add_column("ID", no_wrap=True)
    table.add_column("Name", overflow="fold")
    table.add_column("Provider", overflow="fold")
    table.add_column("API", no_wrap=True)
    table.add_column("Model", overflow="fold")
    table.add_column("Context", no_wrap=True, justify="right")
    table.add_column("Flags", overflow="fold")
    for row in rows:
        profile = row.profile
        table.add_row(
            Text("*" if row.is_active else ""),
            Text(profile.id or "-"),
            Text(profile.name),
            Text(profile.provider or "-"),
            Text(_format_api_style(profile.api_style)),
            Text(profile.model_id or "-"),
            Text(_format_context(profile.max_context_tokens)),
            Text(row.flags),
        )
    console.print(table)
