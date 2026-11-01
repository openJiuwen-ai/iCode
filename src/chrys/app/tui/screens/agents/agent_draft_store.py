# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Draft-profile validation for the agent configuration screen."""

from __future__ import annotations

import math
import os
import re
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, TypeGuard

from chrys.app.tui.screens.agents.skill_paths import (
    normalize_skill_path_for_compare,
)
from chrys.app.tui.screens.agents.validation_messages import (
    ACP_ALLOW_EXTERNAL_CWD,
    ACP_ALLOW_EXTERNAL_CWD_BOOLEAN,
    ACP_ARGUMENTS_NUL,
    ACP_ARGUMENTS_STRINGS,
    ACP_BEST_EFFORT_BOOLEAN,
    ACP_CONFIG_KEYS,
    ACP_CONFIG_MAPPING,
    ACP_CONFIG_VALUE,
    ACP_CWD_NUL,
    ACP_CWD_OUTSIDE_ENABLE,
    ACP_CWD_STRING,
    ACP_DRAFT_HANDSHAKE_TIMEOUT_LABEL,
    ACP_DRAFT_IDLE_TIMEOUT_LABEL,
    ACP_ENV_MAPPING,
    ACP_ENV_NAME_DUPLICATE,
    ACP_ENV_NAME_INVALID,
    ACP_ENV_NAME_RESERVED,
    ACP_ENV_VALUE_NUL,
    ACP_ENV_VALUE_STRING,
    ACP_EXECUTABLE_NUL,
    ACP_EXECUTABLE_REQUIRED,
    ACP_FIELD_STRING,
    ACP_MODEL_ID_FIELD,
    ACP_NO_BUILTIN_TOOLS,
    ACP_NO_CUSTOM_TOOLS,
    ACP_NO_MCP,
    ACP_NO_NESTED_SUBAGENTS,
    ACP_RESULT_MODE,
    ACP_SESSION_MODE_FIELD,
    ACP_TIMEOUT_NUMBER,
    ACP_TIMEOUT_RANGE,
    AT_LEAST_ONE_AGENT,
    AT_LEAST_ONE_MAIN_AGENT,
    CONTEXT_ERROR,
    DESCRIPTION_FIELD_LOWER,
    DISPLAY_NAME_FIELD_LOWER,
    DUPLICATE_SKILL_DIRECTORY,
    DUPLICATE_SUB_AGENT_PROFILE_LOWER,
    DUPLICATE_SUB_AGENT_TOOL_LOWER,
    FIELD_POSITIVE_INTEGER,
    FIELD_REQUIRED,
    FIELDS_REQUIRED,
    GREATER_THAN_ZERO,
    INSTRUCTIONS_FIELD_LOWER,
    LAST_WORDS_RANGE,
    MAX_CONCURRENCY_FIELD,
    MAX_TOTAL_CONCURRENCY_FIELD_LOWER,
    MCP_COMMAND_REQUIRED,
    MCP_DUPLICATE_SERVER_LOWER,
    MCP_INITIALLY_VISIBLE_TOOLS_VALIDATION,
    MCP_SELECTED_TOOL_NAMES,
    MCP_SERVER_CONTEXT,
    MCP_TIMEOUT_POSITIVE,
    MCP_TOOL_LIST_DUPLICATE,
    MCP_TOOL_LIST_EMPTY,
    MCP_TRANSPORT,
    MCP_URL_REQUIRED,
    MCP_URL_SCHEME,
    NAME_FIELD,
    NAME_ITEM,
    PATH_FIELD,
    PATHS_MATCH_CASE_INSENSITIVE,
    PATHS_MATCH_NORMALIZED,
    PROFILE_ALREADY_EXISTS,
    PROFILE_DOES_NOT_EXIST,
    PROFILE_NAME_FIELD_LOWER,
    PROFILE_NAME_FORMAT,
    PROFILE_SELECTION_REQUIRED,
    SCRIPT_TIMEOUT_FIELD_LOWER,
    SELECTED_MODEL_MISSING,
    SERVER_ERROR,
    SKILL_DIRECTORY_CONTEXT,
    SUB_AGENT_CONTEXT,
    TOOL_NAME_IDENTIFIER,
    ZERO_OR_GREATER,
)
from chrys.foundation.i18n import DisplayBlock, MessageDef, MessageRef
from chrys.foundation.i18n.formatting import format_message
from chrys.foundation.platform.paths import is_absolute_path
from chrys.foundation.util.env_templates import resolve_env_templates
from chrys.service.context.memory_loader import validate_memory_config
from chrys.service.mcp.validation import (
    MCP_PROGRESSIVE_CONTROL_TOOL_NAMES,
    validate_mcp_tool_loading_policy,
    validate_mcp_tool_name_prefix,
)

if TYPE_CHECKING:
    from chrys.service.profiles.agents.schema import AgentProfile

# UI-imposed bounds for the editable compaction fields, shared with
# ``CompactionConfigPanel`` (labels + inline validation) so the panel and
# the draft-store check cannot drift apart.
MIN_LAST_WORDS_TOKENS = 1000
MAX_LAST_WORDS_TOKENS = 64000
_ACP_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_ACP_RESERVED_ENV = "CHRYS_ACP_SUBAGENT_DEPTH"

type _RenderMessage = Callable[[MessageRef], str]


def _context_error(render_message: _RenderMessage, context: str, reference: MessageRef) -> str:
    return _context_message(render_message, context, render_message(reference))


def _context_message(render_message: _RenderMessage, context: str, message: str) -> str:
    return render_message(
        CONTEXT_ERROR.bind(
            context=DisplayBlock(context),
            message=DisplayBlock(message),
        )
    )


def _server_error(render_message: _RenderMessage, name: str, reference: MessageRef) -> str:
    return _server_message(render_message, name, render_message(reference))


def _server_message(render_message: _RenderMessage, name: str, message: str) -> str:
    return render_message(
        SERVER_ERROR.bind(
            name=DisplayBlock(name),
            message=DisplayBlock(message),
        )
    )


def _prefixed_server_error(
    render_message: _RenderMessage,
    prefix: str,
    name: str,
    reference: MessageRef,
) -> str:
    return _context_message(render_message, prefix, _server_error(render_message, name, reference))


def _prefixed_server_message(render_message: _RenderMessage, prefix: str, name: str, message: str) -> str:
    return _context_message(render_message, prefix, _server_message(render_message, name, message))


def _duplicate_match_detail(render_message: _RenderMessage) -> str:
    from chrys.foundation.platform import get_platform

    platform = get_platform()
    definition = PATHS_MATCH_CASE_INSENSITIVE if platform.is_macos or platform.is_windows else PATHS_MATCH_NORMALIZED
    return render_message(definition.bind())


class AgentDraft(Protocol):
    """Draft shape consumed by :class:`AgentDraftStore`."""

    key: str
    original_name: str | None
    profile: AgentProfile
    dirty: bool


class AgentDraftStore:
    """Validate the modal-local graph of staged agent profile drafts."""

    def __init__(
        self,
        drafts: Iterable[AgentDraft],
        registry_names: Iterable[str],
        *,
        model_profile_exists: Callable[[str], bool] | None = None,
        workspace_cwd: str | None = None,
        workspace_roots: Iterable[str] | None = None,
    ) -> None:
        self._drafts = list(drafts)
        self._registry_names = list(registry_names)
        self._model_profile_exists = model_profile_exists
        self._workspace_cwd = workspace_cwd
        roots = [root for root in workspace_roots or () if root]
        self._workspace_roots = roots or ([workspace_cwd] if workspace_cwd else [])

    def validate(
        self,
        retargeted_profiles: Iterable[AgentProfile] | None = None,
        *,
        render_message: _RenderMessage = format_message,
    ) -> list[str]:
        """Validate the staged draft graph before writing anything to disk."""
        errors: list[str] = []
        retargeted_profiles = list(retargeted_profiles or [])
        if not self._drafts:
            return [render_message(AT_LEAST_ONE_AGENT.bind())]
        if not any(not draft.profile.sub_agent_only for draft in self._drafts):
            errors.append(render_message(AT_LEAST_ONE_MAIN_AGENT.bind()))

        represented_originals = {draft.original_name for draft in self._drafts if draft.original_name}
        dirty = [draft for draft in self._drafts if draft.dirty]
        dirty_keys = {draft.key for draft in dirty}
        name_owner: dict[str, str] = {}
        for draft in self._drafts:
            profile = draft.profile
            label = profile.display_name or profile.name or "Agent"
            name = profile.name.strip()
            if draft.dirty and not name:
                errors.append(
                    _context_error(
                        render_message,
                        label,
                        FIELD_REQUIRED.bind(field=render_message(PROFILE_NAME_FIELD_LOWER.bind())),
                    )
                )
            elif draft.dirty and not re.match(r"^[a-zA-Z0-9][a-zA-Z0-9_-]*$", name):
                errors.append(
                    _context_error(
                        render_message,
                        label,
                        PROFILE_NAME_FORMAT.bind(field=render_message(PROFILE_NAME_FIELD_LOWER.bind())),
                    )
                )
            lowered = name.casefold()
            if lowered:
                previous = name_owner.get(lowered)
                if previous is not None and (draft.dirty or previous in dirty_keys):
                    errors.append(render_message(PROFILE_ALREADY_EXISTS.bind(name=DisplayBlock(name))))
                name_owner[lowered] = draft.key

        for existing in self._registry_names:
            if existing in represented_originals:
                continue
            owner = name_owner.get(existing.casefold())
            if owner is not None and owner in dirty_keys:
                errors.append(render_message(PROFILE_ALREADY_EXISTS.bind(name=DisplayBlock(existing))))

        available_profile_names = {draft.profile.name for draft in self._drafts}
        for existing in self._registry_names:
            if existing not in represented_originals:
                available_profile_names.add(existing)

        for draft in dirty:
            profile = draft.profile
            prefix = profile.display_name or profile.name or "Agent"
            errors.extend(
                validate_agent_profile_draft(
                    prefix,
                    profile,
                    available_profile_names,
                    model_profile_exists=self._model_profile_exists,
                    workspace_cwd=self._workspace_cwd,
                    workspace_roots=self._workspace_roots,
                    render_message=render_message,
                )
            )

        for profile in retargeted_profiles:
            prefix = profile.display_name or profile.name or "Agent"
            errors.extend(
                validate_agent_profile_draft(
                    prefix,
                    profile,
                    available_profile_names,
                    model_profile_exists=self._model_profile_exists,
                    workspace_cwd=self._workspace_cwd,
                    workspace_roots=self._workspace_roots,
                    render_message=render_message,
                )
            )

        return errors


def validate_agent_profile_draft(
    prefix: str,
    profile: AgentProfile,
    available_profile_names: set[str],
    *,
    model_profile_exists: Callable[[str], bool] | None = None,
    workspace_cwd: str | None = None,
    workspace_roots: Iterable[str] | None = None,
    render_message: _RenderMessage = format_message,
) -> list[str]:
    """Validate one profile object in the staged draft graph."""
    errors: list[str] = []
    if not profile.display_name.strip():
        errors.append(
            _context_error(
                render_message,
                prefix,
                FIELD_REQUIRED.bind(field=render_message(DISPLAY_NAME_FIELD_LOWER.bind())),
            )
        )
    if not profile.description.strip():
        errors.append(
            _context_error(
                render_message,
                prefix,
                FIELD_REQUIRED.bind(field=render_message(DESCRIPTION_FIELD_LOWER.bind())),
            )
        )
    if profile.acp is not None:
        errors.extend(
            validate_acp_draft(
                prefix,
                profile,
                workspace_cwd=workspace_cwd,
                workspace_roots=workspace_roots,
                render_message=render_message,
            )
        )
        return errors
    if not profile.instructions.strip():
        errors.append(
            _context_error(
                render_message,
                prefix,
                FIELDS_REQUIRED.bind(field=render_message(INSTRUCTIONS_FIELD_LOWER.bind())),
            )
        )
    if (
        profile.model.profile_id
        and model_profile_exists is not None
        and not model_profile_exists(profile.model.profile_id)
    ):
        errors.append(_context_error(render_message, prefix, SELECTED_MODEL_MISSING.bind()))

    errors.extend(
        validate_sub_agents_draft(
            prefix,
            profile,
            available_profile_names,
            render_message=render_message,
        )
    )
    errors.extend(validate_compaction_draft(prefix, profile, render_message=render_message))
    errors.extend(
        _context_message(render_message, prefix, error)
        for error in validate_memory_config(profile.memory, workspace_cwd=workspace_cwd)
    )
    errors.extend(validate_skills_draft(prefix, profile, workspace_cwd=workspace_cwd, render_message=render_message))
    errors.extend(validate_mcp_draft(prefix, profile, render_message=render_message))
    return errors


def _acp_template_error(
    prefix: str,
    value: str,
    *,
    location: str,
    allow_commands: bool = True,
    render_message: _RenderMessage = format_message,
) -> list[str]:
    """Report ``{{ENV}}`` templates real invocation could not resolve.

    Invocation (orchestration/sub_agents/tools.py) expands every launch field
    before spawning, so a template the current environment cannot resolve is a
    guaranteed runtime failure — surface it at validation time instead.
    """
    try:
        resolve_env_templates(value, location=location, allow_commands=allow_commands)
    except ValueError as exc:
        return [_context_message(render_message, prefix, str(exc))]
    return []


def validate_acp_draft(
    prefix: str,
    profile: AgentProfile,
    *,
    workspace_cwd: str | None = None,
    workspace_roots: Iterable[str] | None = None,
    render_message: _RenderMessage = format_message,
) -> list[str]:
    """Validate the external ACP discriminator and its launch boundary."""
    config = profile.acp
    if config is None:
        return []
    errors: list[str] = []
    if type(config.command) is not str or not config.command.strip():
        errors.append(_context_error(render_message, prefix, ACP_EXECUTABLE_REQUIRED.bind()))
    elif "\0" in config.command:
        errors.append(_context_error(render_message, prefix, ACP_EXECUTABLE_NUL.bind()))
    else:
        errors.extend(
            _acp_template_error(
                prefix,
                config.command,
                location="ACP command",
                allow_commands=False,
                render_message=render_message,
            )
        )
    if type(config.args) is not list or any(type(arg) is not str for arg in config.args):
        errors.append(_context_error(render_message, prefix, ACP_ARGUMENTS_STRINGS.bind()))
    elif any("\0" in arg for arg in config.args):
        errors.append(_context_error(render_message, prefix, ACP_ARGUMENTS_NUL.bind()))
    else:
        for arg in config.args:
            errors.extend(
                _acp_template_error(
                    prefix,
                    arg,
                    location="ACP argument",
                    render_message=render_message,
                )
            )

    if type(config.env) is not dict:
        errors.append(_context_error(render_message, prefix, ACP_ENV_MAPPING.bind()))
    else:
        seen_env: set[str] = set()
        for key, value in config.env.items():
            if type(key) is not str or _ACP_ENV_NAME_RE.fullmatch(key) is None:
                errors.append(
                    _context_error(
                        render_message,
                        prefix,
                        ACP_ENV_NAME_INVALID.bind(name=DisplayBlock(repr(key))),
                    )
                )
                continue
            folded = key.casefold()
            if folded == _ACP_RESERVED_ENV.casefold():
                errors.append(
                    _context_error(
                        render_message,
                        prefix,
                        ACP_ENV_NAME_RESERVED.bind(name=DisplayBlock(repr(_ACP_RESERVED_ENV))),
                    )
                )
            if folded in seen_env:
                errors.append(
                    _context_error(
                        render_message,
                        prefix,
                        ACP_ENV_NAME_DUPLICATE.bind(name=DisplayBlock(repr(key))),
                    )
                )
            seen_env.add(folded)
            if type(value) is not str:
                errors.append(
                    _context_error(
                        render_message,
                        prefix,
                        ACP_ENV_VALUE_STRING.bind(name=DisplayBlock(repr(key))),
                    )
                )
            elif "\0" in value:
                errors.append(
                    _context_error(
                        render_message,
                        prefix,
                        ACP_ENV_VALUE_NUL.bind(name=DisplayBlock(repr(key))),
                    )
                )
            else:
                errors.extend(
                    _acp_template_error(
                        prefix,
                        value,
                        location=f"ACP environment {key!r}",
                        render_message=render_message,
                    )
                )

    if type(config.cwd) is not str:
        errors.append(_context_error(render_message, prefix, ACP_CWD_STRING.bind()))
    elif "\0" in config.cwd:
        errors.append(_context_error(render_message, prefix, ACP_CWD_NUL.bind()))
    elif config.cwd:
        # Containment must judge the EXPANDED cwd — real invocation
        # (orchestration/sub_agents/tools.py) expands ``{{ENV}}`` templates
        # before its workspace check, so validating the literal template
        # would pass a template that expands outside the workspace.
        expanded_cwd: str | None
        try:
            expanded_cwd = resolve_env_templates(config.cwd, location="ACP working directory", allow_commands=False)
        except ValueError as exc:
            errors.append(_context_message(render_message, prefix, str(exc)))
            expanded_cwd = None
        if expanded_cwd is not None and not config.allow_external_cwd:
            primary = workspace_cwd or next(iter(workspace_roots or ()), "")
            candidate = expanded_cwd if is_absolute_path(expanded_cwd) else str(Path(primary) / expanded_cwd)
            resolved_cwd = os.path.realpath(candidate)
            resolved_roots = [os.path.realpath(root) for root in workspace_roots or () if root]
            if not resolved_roots and primary:
                resolved_roots = [os.path.realpath(primary)]

            def within(root: str) -> bool:
                try:
                    return os.path.commonpath(
                        [os.path.normcase(resolved_cwd), os.path.normcase(root)]
                    ) == os.path.normcase(root)
                except ValueError:
                    return False

            if not any(within(root) for root in resolved_roots):
                errors.append(
                    _context_error(
                        render_message,
                        prefix,
                        ACP_CWD_OUTSIDE_ENABLE.bind(label=render_message(ACP_ALLOW_EXTERNAL_CWD.bind())),
                    )
                )

    if type(config.allow_external_cwd) is not bool:
        errors.append(_context_error(render_message, prefix, ACP_ALLOW_EXTERNAL_CWD_BOOLEAN.bind()))
    if type(config.best_effort_options) is not bool:
        errors.append(_context_error(render_message, prefix, ACP_BEST_EFFORT_BOOLEAN.bind()))
    for field_definition, value in (
        (ACP_SESSION_MODE_FIELD, config.session_mode),
        (ACP_MODEL_ID_FIELD, config.model_id),
    ):
        if type(value) is not str:
            errors.append(
                _context_error(
                    render_message,
                    prefix,
                    ACP_FIELD_STRING.bind(field=render_message(field_definition.bind())),
                )
            )
    if type(config.config_options) is not dict:
        errors.append(_context_error(render_message, prefix, ACP_CONFIG_MAPPING.bind()))
    else:
        for key, value in config.config_options.items():
            if type(key) is not str or not key:
                errors.append(_context_error(render_message, prefix, ACP_CONFIG_KEYS.bind()))
            if type(value) not in {str, bool}:
                errors.append(
                    _context_error(
                        render_message,
                        prefix,
                        ACP_CONFIG_VALUE.bind(key=DisplayBlock(repr(key))),
                    )
                )
    if config.result_mode not in {"last_segment", "transcript"}:
        errors.append(_context_error(render_message, prefix, ACP_RESULT_MODE.bind()))
    errors.extend(
        _validate_acp_timeout(
            prefix,
            ACP_DRAFT_HANDSHAKE_TIMEOUT_LABEL,
            config.handshake_timeout_seconds,
            allow_zero=False,
            render_message=render_message,
        )
    )
    errors.extend(
        _validate_acp_timeout(
            prefix,
            ACP_DRAFT_IDLE_TIMEOUT_LABEL,
            config.idle_timeout_seconds,
            allow_zero=True,
            render_message=render_message,
        )
    )
    if profile.sub_agents.agents:
        errors.append(_context_error(render_message, prefix, ACP_NO_NESTED_SUBAGENTS.bind()))
    if profile.tools.mcp:
        errors.append(_context_error(render_message, prefix, ACP_NO_MCP.bind()))
    if profile.tools.custom:
        errors.append(_context_error(render_message, prefix, ACP_NO_CUSTOM_TOOLS.bind()))
    if profile.tools.builtins:
        errors.append(_context_error(render_message, prefix, ACP_NO_BUILTIN_TOOLS.bind()))
    return errors


def _is_exact_number(value: object) -> TypeGuard[int | float]:
    """Narrow JSON numbers without accepting bools or numeric subclasses."""
    return type(value) in {int, float}


def _validate_acp_timeout(
    prefix: str,
    label_definition: MessageDef,
    value: object,
    *,
    allow_zero: bool,
    render_message: _RenderMessage = format_message,
) -> list[str]:
    label = render_message(label_definition.bind())
    if not _is_exact_number(value):
        return [
            _context_error(
                render_message,
                prefix,
                ACP_TIMEOUT_NUMBER.bind(label=label),
            )
        ]
    numeric = float(value)
    if not math.isfinite(numeric) or numeric < 0 or (not allow_zero and numeric == 0):
        qualifier_definition = ZERO_OR_GREATER if allow_zero else GREATER_THAN_ZERO
        return [
            _context_error(
                render_message,
                prefix,
                ACP_TIMEOUT_RANGE.bind(
                    label=label,
                    qualifier=render_message(qualifier_definition.bind()),
                ),
            )
        ]
    return []


def validate_sub_agents_draft(
    prefix: str,
    profile: AgentProfile,
    available_profile_names: set[str],
    *,
    render_message: _RenderMessage = format_message,
) -> list[str]:
    """Validate one profile's sub-agent config after widget state is converted."""
    errors: list[str] = []
    if profile.sub_agents.max_total_concurrency <= 0:
        errors.append(
            _context_error(
                render_message,
                prefix,
                FIELD_POSITIVE_INTEGER.bind(
                    field=render_message(MAX_TOTAL_CONCURRENCY_FIELD_LOWER.bind()),
                ),
            )
        )
    seen_profiles: set[str] = set()
    seen_tool_names: set[str] = set()
    for index, ref in enumerate(profile.sub_agents.agents, start=1):
        display = render_message(SUB_AGENT_CONTEXT.bind(index=index))
        if not ref.profile:
            errors.append(
                _context_error(
                    render_message,
                    prefix,
                    CONTEXT_ERROR.bind(
                        context=DisplayBlock(display),
                        message=DisplayBlock(render_message(PROFILE_SELECTION_REQUIRED.bind())),
                    ),
                )
            )
            continue
        if ref.profile not in available_profile_names:
            errors.append(
                _context_error(
                    render_message,
                    prefix,
                    CONTEXT_ERROR.bind(
                        context=DisplayBlock(display),
                        message=DisplayBlock(
                            render_message(PROFILE_DOES_NOT_EXIST.bind(name=DisplayBlock(ref.profile)))
                        ),
                    ),
                )
            )
        if ref.profile in seen_profiles:
            errors.append(
                _context_error(
                    render_message,
                    prefix,
                    DUPLICATE_SUB_AGENT_PROFILE_LOWER.bind(name=DisplayBlock(ref.profile)),
                )
            )
        seen_profiles.add(ref.profile)
        if ref.tool_name and not re.match(r"^[a-zA-Z_][a-zA-Z0-9_]*$", ref.tool_name):
            errors.append(
                _context_error(
                    render_message,
                    prefix,
                    CONTEXT_ERROR.bind(
                        context=DisplayBlock(display),
                        message=DisplayBlock(render_message(TOOL_NAME_IDENTIFIER.bind())),
                    ),
                )
            )
        effective_name = ref.tool_name or ref.profile
        if effective_name in seen_tool_names:
            errors.append(
                _context_error(
                    render_message,
                    prefix,
                    DUPLICATE_SUB_AGENT_TOOL_LOWER.bind(name=DisplayBlock(effective_name)),
                )
            )
        seen_tool_names.add(effective_name)
        if ref.max_concurrency <= 0:
            errors.append(
                _context_error(
                    render_message,
                    prefix,
                    CONTEXT_ERROR.bind(
                        context=DisplayBlock(display),
                        message=DisplayBlock(
                            render_message(
                                FIELD_POSITIVE_INTEGER.bind(
                                    field=render_message(MAX_CONCURRENCY_FIELD.bind()),
                                )
                            )
                        ),
                    ),
                )
            )
    return errors


def validate_compaction_draft(
    prefix: str,
    profile: AgentProfile,
    *,
    render_message: _RenderMessage = format_message,
) -> list[str]:
    """Validate one profile's compaction config after widget state is converted."""
    errors: list[str] = []
    max_tokens = profile.compaction.last_words_max_output_tokens
    if not MIN_LAST_WORDS_TOKENS <= max_tokens <= MAX_LAST_WORDS_TOKENS:
        errors.append(
            _context_error(
                render_message,
                prefix,
                LAST_WORDS_RANGE.bind(
                    minimum=MIN_LAST_WORDS_TOKENS,
                    maximum=MAX_LAST_WORDS_TOKENS,
                ),
            )
        )
    return errors


def validate_skills_draft(
    prefix: str,
    profile: AgentProfile,
    *,
    workspace_cwd: str | None = None,
    render_message: _RenderMessage = format_message,
) -> list[str]:
    """Validate one profile's skills config after widget state is converted."""
    errors: list[str] = []
    seen_paths: dict[str, int] = {}
    duplicate_match_description = _duplicate_match_detail(render_message)
    for index, path in enumerate(profile.skills.paths, start=1):
        display = render_message(SKILL_DIRECTORY_CONTEXT.bind(index=index))
        expanded = path.strip()
        if not expanded:
            errors.append(
                _context_error(
                    render_message,
                    prefix,
                    CONTEXT_ERROR.bind(
                        context=DisplayBlock(display),
                        message=DisplayBlock(
                            render_message(FIELD_REQUIRED.bind(field=render_message(PATH_FIELD.bind())))
                        ),
                    ),
                )
            )
            continue
        is_relative = not expanded.startswith("~") and not is_absolute_path(expanded)
        compare_path = str(Path(workspace_cwd) / expanded) if is_relative and workspace_cwd else expanded
        normalized = normalize_skill_path_for_compare(compare_path)
        if normalized in seen_paths:
            errors.append(
                _context_error(
                    render_message,
                    prefix,
                    CONTEXT_ERROR.bind(
                        context=DisplayBlock(display),
                        message=DisplayBlock(
                            render_message(
                                DUPLICATE_SKILL_DIRECTORY.bind(
                                    path=DisplayBlock(path),
                                    other_index=seen_paths[normalized],
                                    detail=DisplayBlock(duplicate_match_description),
                                )
                            )
                        ),
                    ),
                )
            )
        else:
            seen_paths[normalized] = index
    if profile.skills.script_timeout <= 0:
        errors.append(
            _context_error(
                render_message,
                prefix,
                FIELD_POSITIVE_INTEGER.bind(
                    field=render_message(SCRIPT_TIMEOUT_FIELD_LOWER.bind()),
                ),
            )
        )
    return errors


def validate_mcp_draft(
    prefix: str,
    profile: AgentProfile,
    *,
    render_message: _RenderMessage = format_message,
) -> list[str]:
    """Validate one profile's MCP config after widget state is converted."""
    return [
        error
        for server_errors in mcp_draft_errors_by_server(prefix, profile, render_message=render_message)
        for error in server_errors
    ]


def mcp_draft_errors_by_server(
    prefix: str,
    profile: AgentProfile,
    *,
    render_message: _RenderMessage = format_message,
) -> list[list[str]]:
    """Return each MCP server's validation errors, in ``profile.tools.mcp`` order."""
    by_server: list[list[str]] = []
    seen_names: set[str] = set()
    for index, server in enumerate(profile.tools.mcp, start=1):
        errors: list[str] = []
        by_server.append(errors)
        display = server.name or render_message(MCP_SERVER_CONTEXT.bind(index=index))
        if not server.name:
            errors.append(
                _context_error(
                    render_message,
                    prefix,
                    CONTEXT_ERROR.bind(
                        context=DisplayBlock(render_message(MCP_SERVER_CONTEXT.bind(index=index))),
                        message=DisplayBlock(
                            render_message(FIELD_REQUIRED.bind(field=render_message(NAME_FIELD.bind())))
                        ),
                    ),
                )
            )
        lower_name = server.name.casefold()
        if lower_name:
            if lower_name in seen_names:
                errors.append(
                    _context_error(
                        render_message,
                        prefix,
                        MCP_DUPLICATE_SERVER_LOWER.bind(name=DisplayBlock(server.name)),
                    )
                )
            seen_names.add(lower_name)
        if server.transport == "http":
            if not server.url:
                errors.append(_prefixed_server_error(render_message, prefix, display, MCP_URL_REQUIRED.bind()))
            elif not server.url.startswith(("http://", "https://")):
                errors.append(_prefixed_server_error(render_message, prefix, display, MCP_URL_SCHEME.bind()))
        elif server.transport == "stdio":
            if not server.command:
                errors.append(_prefixed_server_error(render_message, prefix, display, MCP_COMMAND_REQUIRED.bind()))
        else:
            errors.append(_prefixed_server_error(render_message, prefix, display, MCP_TRANSPORT.bind()))
        if server.request_timeout is not None and (
            type(server.request_timeout) is not int or server.request_timeout <= 0
        ):
            errors.append(_prefixed_server_error(render_message, prefix, display, MCP_TIMEOUT_POSITIVE.bind()))
        prefix_error = validate_mcp_tool_name_prefix(
            server.tool_name_prefix,
            generated_suffixes=(MCP_PROGRESSIVE_CONTROL_TOOL_NAMES if server.use_progressive_disclosure else ()),
        )
        if prefix_error is not None:
            errors.append(_prefixed_server_message(render_message, prefix, display, prefix_error))
        if server.allowed_tools is not None:
            seen_allowed: set[str] = set()
            selected_label = render_message(MCP_SELECTED_TOOL_NAMES.bind())
            for tool_name in server.allowed_tools:
                normalized_name = tool_name.strip()
                if not normalized_name:
                    errors.append(
                        _prefixed_server_error(
                            render_message,
                            prefix,
                            display,
                            MCP_TOOL_LIST_EMPTY.bind(
                                label=selected_label,
                                item=render_message(NAME_ITEM.bind()),
                            ),
                        )
                    )
                    continue
                if normalized_name in seen_allowed:
                    errors.append(
                        _prefixed_server_error(
                            render_message,
                            prefix,
                            display,
                            MCP_TOOL_LIST_DUPLICATE.bind(
                                label=selected_label,
                                name=DisplayBlock(normalized_name),
                            ),
                        )
                    )
                seen_allowed.add(normalized_name)
        seen_always_load: set[str] = set()
        initially_visible_label = render_message(MCP_INITIALLY_VISIBLE_TOOLS_VALIDATION.bind())
        for tool_name in server.always_load:
            normalized_name = tool_name.strip()
            if not normalized_name:
                errors.append(
                    _prefixed_server_error(
                        render_message,
                        prefix,
                        display,
                        MCP_TOOL_LIST_EMPTY.bind(
                            label=initially_visible_label,
                            item=render_message(NAME_ITEM.bind()),
                        ),
                    )
                )
                continue
            if normalized_name in seen_always_load:
                errors.append(
                    _prefixed_server_error(
                        render_message,
                        prefix,
                        display,
                        MCP_TOOL_LIST_DUPLICATE.bind(
                            label=initially_visible_label,
                            name=DisplayBlock(normalized_name),
                        ),
                    )
                )
            seen_always_load.add(normalized_name)
        errors.extend(
            _prefixed_server_message(render_message, prefix, display, policy_error)
            for policy_error in validate_mcp_tool_loading_policy(
                allowed_tools=server.allowed_tools,
                use_progressive_disclosure=server.use_progressive_disclosure,
                always_load=server.always_load,
            )
        )
    return by_server
