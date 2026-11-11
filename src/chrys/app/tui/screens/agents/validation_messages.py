# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Shared locale-neutral validation prose for agent configuration."""

from chrys.foundation.i18n import msg

# Labels shared by visible controls and validation prose.
DISPLAY_NAME = msg("tui.agent_config.basic.display_name", fallback="Display Name")
DESCRIPTION = msg("tui.agent_config.basic.description", fallback="Description")
MAX_TOTAL_CONCURRENCY = msg(
    "tui.agent_config.subagents.max_total_concurrency",
    fallback="Max Total Concurrency",
)
SKILL_DIRECTORY = msg("tui.agent_config.skills.directory", fallback="Skill Directory")
EXECUTION_TIMEOUT = msg(
    "tui.agent_config.skills.execution_timeout",
    fallback="Script Execution Timeout (seconds)",
)
MEMORY_FILE = msg("tui.agent_config.memory.file", fallback="File")
MEMORY_FOLDER = msg("tui.agent_config.memory.folder", fallback="Folder")
COMPACTION_MAX_OUTPUT_TOKENS = msg(
    "tui.agent_config.compaction.max_output_tokens",
    fallback="Last Words Max Output Tokens ({minimum} - {maximum})",
)
ACP_ALLOW_EXTERNAL_CWD = msg("tui.acp.allow_external_cwd", fallback="Allow cwd outside workspace")
MCP_TOOL_ACCESS_NONE = msg("tui.mcp.tool_access.none", fallback="No tools")
MCP_SELECTED_TOOL_NAMES = msg("tui.mcp.selected_tool_names", fallback="Selected Tool Names")
MCP_INITIALLY_VISIBLE_TOOLS = msg(
    "tui.mcp.initially_visible_tools",
    fallback="Initially Visible Tools (optional)",
)
MCP_HEADERS = msg("tui.mcp.headers", fallback="Headers")
MCP_ENVIRONMENT_VARIABLES = msg("tui.mcp.environment_variables", fallback="Environment Variables")

# Composition primitives. Arbitrary display values use DisplayBlock at bind sites.
CONTEXT_ERROR = msg(
    "tui.agent_config.validation.context_error",
    fallback="{context}: {message}",
    multiline=True,
)
SERVER_ERROR = msg(
    "tui.agent_config.validation.server_error",
    fallback="Server '{name}': {message}",
    multiline=True,
)
FIELD_REQUIRED = msg(
    "tui.agent_config.validation.field_required",
    fallback="{field} is required.",
)
FIELDS_REQUIRED = msg(
    "tui.agent_config.validation.fields_required",
    fallback="{field} are required.",
)
FIELD_VALID_INTEGER = msg(
    "tui.agent_config.validation.field_valid_integer",
    fallback="{field} must be a valid integer.",
)
FIELD_POSITIVE_INTEGER = msg(
    "tui.agent_config.validation.field_positive_integer",
    fallback="{field} must be a positive integer.",
)
PROFILE_NAME_FIELD = msg("tui.agent_config.validation.field.profile_name", fallback="Profile name")
PROFILE_NAME_FIELD_LOWER = msg("tui.agent_config.validation.field.profile_name_lower", fallback="profile name")
DISPLAY_NAME_FIELD = msg("tui.agent_config.validation.field.display_name", fallback="Display name")
DISPLAY_NAME_FIELD_LOWER = msg("tui.agent_config.validation.field.display_name_lower", fallback="display name")
DESCRIPTION_FIELD_LOWER = msg("tui.agent_config.validation.field.description_lower", fallback="description")
INSTRUCTIONS_FIELD = msg("tui.agent_config.validation.field.instructions", fallback="Instructions")
INSTRUCTIONS_FIELD_LOWER = msg("tui.agent_config.validation.field.instructions_lower", fallback="instructions")
MAX_TOTAL_CONCURRENCY_FIELD = msg(
    "tui.agent_config.validation.field.max_total_concurrency",
    fallback="Max total concurrency",
)
MAX_TOTAL_CONCURRENCY_FIELD_LOWER = msg(
    "tui.agent_config.validation.field.max_total_concurrency_lower",
    fallback="max total concurrency",
)
MAX_CONCURRENCY_FIELD = msg(
    "tui.agent_config.validation.field.max_concurrency",
    fallback="max concurrency",
)
SCRIPT_TIMEOUT_FIELD = msg("tui.agent_config.validation.field.script_timeout", fallback="Script timeout")
SCRIPT_TIMEOUT_FIELD_LOWER = msg(
    "tui.agent_config.validation.field.script_timeout_lower",
    fallback="skill script timeout",
)
LAST_WORDS_FIELD = msg(
    "tui.agent_config.validation.field.last_words_max_output_tokens",
    fallback="Last words max output tokens",
)
PATH_FIELD = msg("tui.agent_config.validation.field.path", fallback="path")
NAME_FIELD = msg("tui.agent_config.validation.field.name", fallback="name")
TOOL_NAME_ITEM = msg("tui.agent_config.validation.item.tool_name", fallback="tool name")
NAME_ITEM = msg("tui.agent_config.validation.item.name", fallback="name")

# Basic/profile graph validation.
AT_LEAST_ONE_AGENT = msg(
    "tui.agent_config.validation.at_least_one_agent",
    fallback="At least one agent profile is required.",
)
AT_LEAST_ONE_MAIN_AGENT = msg(
    "tui.agent_config.validation.at_least_one_main_agent",
    fallback="At least one main agent profile is required.",
)
PROFILE_NAME_FORMAT = msg(
    "tui.agent_config.validation.profile_name_format",
    fallback=("{field} must start with a letter or digit and contain only letters, digits, hyphens, and underscores."),
)
PROFILE_ALREADY_EXISTS = msg(
    "tui.agent_config.validation.profile_already_exists",
    fallback="A profile named '{name}' already exists (names are case-insensitive).",
    multiline=True,
)
SELECT_MODEL_PROFILE = msg(
    "tui.agent_config.validation.select_model_profile",
    fallback="Select a model profile or re-check 'Use active model profile'.",
)
SELECTED_MODEL_MISSING = msg(
    "tui.agent_config.validation.selected_model_missing",
    fallback="selected model profile no longer exists.",
)
FIX_VALIDATION_ERRORS = msg(
    "tui.agent_config.validation.fix_before_structural_change",
    fallback="Fix validation errors before switching agents or applying structural changes.",
)

# ACP validation.
ACP_EXECUTABLE_REQUIRED = msg(
    "tui.agent_config.validation.acp.executable_required",
    fallback="ACP executable is required.",
)
ACP_EXECUTABLE_NUL = msg(
    "tui.agent_config.validation.acp.executable_nul",
    fallback="ACP executable must not contain an embedded NUL.",
)
ACP_ARGUMENTS_STRINGS = msg(
    "tui.agent_config.validation.acp.arguments_strings",
    fallback="ACP arguments must be strings.",
)
ACP_ARGUMENTS_NUL = msg(
    "tui.agent_config.validation.acp.arguments_nul",
    fallback="ACP arguments must not contain embedded NUL characters.",
)
ACP_ENV_MAPPING = msg(
    "tui.agent_config.validation.acp.environment_mapping",
    fallback="ACP environment must be a key/value mapping.",
)
ACP_ENV_NAME_INVALID = msg(
    "tui.agent_config.validation.acp.environment_name_invalid",
    fallback="ACP environment variable name {name} is invalid.",
    multiline=True,
)
ACP_ENV_NAME_RESERVED = msg(
    "tui.agent_config.validation.acp.environment_name_reserved",
    fallback="ACP environment variable {name} is reserved.",
    multiline=True,
)
ACP_ENV_NAME_DUPLICATE = msg(
    "tui.agent_config.validation.acp.environment_name_duplicate",
    fallback="ACP environment variable {name} is a case-insensitive duplicate.",
    multiline=True,
)
ACP_ENV_VALUE_STRING = msg(
    "tui.agent_config.validation.acp.environment_value_string",
    fallback="ACP environment value for {name} must be a string.",
    multiline=True,
)
ACP_ENV_VALUE_NUL = msg(
    "tui.agent_config.validation.acp.environment_value_nul",
    fallback="ACP environment value for {name} must not contain an embedded NUL.",
    multiline=True,
)
ACP_CWD_STRING = msg(
    "tui.agent_config.validation.acp.cwd_string",
    fallback="ACP working directory must be a string.",
)
ACP_CWD_NUL = msg(
    "tui.agent_config.validation.acp.cwd_nul",
    fallback="ACP working directory must not contain an embedded NUL.",
)
ACP_CWD_OUTSIDE = msg(
    "tui.agent_config.validation.acp.cwd_outside",
    fallback="ACP working directory resolves outside all workspace roots.",
)
ACP_CWD_OUTSIDE_ENABLE = msg(
    "tui.agent_config.validation.acp.cwd_outside_enable",
    fallback="ACP working directory resolves outside all workspace roots; enable '{label}' to permit it.",
)
ACP_ALLOW_EXTERNAL_CWD_BOOLEAN = msg(
    "tui.agent_config.validation.acp.allow_external_cwd_boolean",
    fallback="ACP allow-external-cwd must be a boolean.",
)
ACP_BEST_EFFORT_BOOLEAN = msg(
    "tui.agent_config.validation.acp.best_effort_boolean",
    fallback="ACP best-effort options must be a boolean.",
)
ACP_FIELD_STRING = msg(
    "tui.agent_config.validation.acp.field_string",
    fallback="ACP {field} must be a string.",
)
ACP_CONFIG_MAPPING = msg(
    "tui.agent_config.validation.acp.config_mapping",
    fallback="ACP config options must be a key/value mapping.",
)
ACP_CONFIG_KEYS = msg(
    "tui.agent_config.validation.acp.config_keys",
    fallback="ACP config option keys must be non-empty strings.",
)
ACP_CONFIG_VALUE = msg(
    "tui.agent_config.validation.acp.config_value",
    fallback="ACP config option {key} must be a string or boolean.",
    multiline=True,
)
ACP_RESULT_MODE = msg(
    "tui.agent_config.validation.acp.result_mode",
    fallback="ACP result mode must be last_segment or transcript.",
)
ACP_TIMEOUT_NUMBER = msg(
    "tui.agent_config.validation.acp.timeout_number",
    fallback="{label} must be a finite number.",
)
ACP_TIMEOUT_RANGE = msg(
    "tui.agent_config.validation.acp.timeout_range",
    fallback="{label} must be finite and {qualifier}.",
)
ZERO_OR_GREATER = msg("tui.agent_config.validation.zero_or_greater", fallback="zero or greater")
GREATER_THAN_ZERO = msg("tui.agent_config.validation.greater_than_zero", fallback="greater than zero")
ACP_NO_NESTED_SUBAGENTS = msg(
    "tui.agent_config.validation.acp.no_nested_subagents",
    fallback="ACP profiles cannot configure nested sub-agents.",
)
ACP_NO_MCP = msg(
    "tui.agent_config.validation.acp.no_mcp",
    fallback="ACP profiles cannot configure MCP servers.",
)
ACP_NO_EXTENSIONS = msg(
    "tui.agent_config.validation.acp.no_extensions",
    fallback="ACP profiles cannot use extensions.",
)
ACP_NO_BUILTIN_TOOLS = msg(
    "tui.agent_config.validation.acp.no_builtin_tools",
    fallback="ACP profiles cannot configure built-in tools.",
)
ACP_LAUNCH_NUL = msg(
    "tui.agent_config.validation.acp.launch_nul",
    fallback="ACP launch fields must not contain embedded NUL characters.",
)
ACP_EXPANDED_LAUNCH_NUL = msg(
    "tui.agent_config.validation.acp.expanded_launch_nul",
    fallback="ACP launch fields must not contain embedded NUL characters after template expansion.",
)
ACP_ENVIRONMENT_ROW_LABEL = msg(
    "tui.agent_config.validation.acp.environment_row_label",
    fallback="Environment",
)
ACP_CONFIG_OPTION_ROW_LABEL = msg(
    "tui.agent_config.validation.acp.config_option_row_label",
    fallback="Config option",
)
ACP_HANDSHAKE_TIMEOUT_LABEL = msg(
    "tui.agent_config.validation.acp.handshake_timeout_label",
    fallback="Handshake timeout",
)
ACP_IDLE_TIMEOUT_LABEL = msg(
    "tui.agent_config.validation.acp.idle_timeout_label",
    fallback="Idle timeout",
)
ACP_DRAFT_HANDSHAKE_TIMEOUT_LABEL = msg(
    "tui.agent_config.validation.acp.draft_handshake_timeout_label",
    fallback="ACP handshake timeout",
)
ACP_DRAFT_IDLE_TIMEOUT_LABEL = msg(
    "tui.agent_config.validation.acp.draft_idle_timeout_label",
    fallback="ACP idle timeout",
)
ACP_SESSION_MODE_FIELD = msg(
    "tui.agent_config.validation.acp.session_mode_field",
    fallback="session mode",
)
ACP_MODEL_ID_FIELD = msg(
    "tui.agent_config.validation.acp.model_id_field",
    fallback="model id",
)
KEY_REQUIRED_ROW = msg(
    "tui.agent_config.validation.key_required_row",
    fallback="{label} row {row}: key is required.",
)
DUPLICATE_KEY_ROW = msg(
    "tui.agent_config.validation.duplicate_key_row",
    fallback="{label} row {row}: duplicate key '{key}'.",
    multiline=True,
)

# Sub-agent validation.
SUB_AGENT_CONTEXT = msg("tui.agent_config.validation.sub_agent_context", fallback="Sub-Agent {index}")
PROFILE_SELECTION_REQUIRED = msg(
    "tui.agent_config.validation.profile_selection_required",
    fallback="profile selection is required.",
)
PROFILE_DOES_NOT_EXIST = msg(
    "tui.agent_config.validation.profile_does_not_exist",
    fallback="profile '{name}' does not exist.",
    multiline=True,
)
DUPLICATE_SUB_AGENT_PROFILE = msg(
    "tui.agent_config.validation.duplicate_sub_agent_profile",
    fallback="Duplicate sub-agent profile: '{name}'.",
    multiline=True,
)
DUPLICATE_SUB_AGENT_PROFILE_LOWER = msg(
    "tui.agent_config.validation.duplicate_sub_agent_profile_lower",
    fallback="duplicate sub-agent profile: '{name}'.",
    multiline=True,
)
DUPLICATE_SUB_AGENT_TOOL = msg(
    "tui.agent_config.validation.duplicate_sub_agent_tool",
    fallback="Duplicate sub-agent tool name: '{name}'.",
    multiline=True,
)
DUPLICATE_SUB_AGENT_TOOL_LOWER = msg(
    "tui.agent_config.validation.duplicate_sub_agent_tool_lower",
    fallback="duplicate sub-agent tool name: '{name}'.",
    multiline=True,
)
TOOL_NAME_IDENTIFIER = msg(
    "tui.agent_config.validation.tool_name_identifier",
    fallback=(
        "tool name must be a valid identifier (letters, digits, underscores, starting with a letter or underscore)."
    ),
)

# Skills, memory, and compaction validation.
SKILL_DIRECTORY_CONTEXT = msg(
    "tui.agent_config.validation.skill_directory_context",
    fallback="Skill Directory {index}",
)
PATH_ABSOLUTE_DISABLE = msg(
    "tui.agent_config.validation.path_absolute_disable",
    fallback="'{path}' is absolute; turn off {label}.",
    multiline=True,
)
PATH_RELATIVE_ENABLE = msg(
    "tui.agent_config.validation.path_relative_enable",
    fallback="'{path}' is relative; enable {label}.",
    multiline=True,
)
DUPLICATE_SKILL_DIRECTORY = msg(
    "tui.agent_config.validation.duplicate_skill_directory",
    fallback="'{path}' duplicates Skill Directory {other_index} ({detail}).",
    multiline=True,
)
PATHS_MATCH_CASE_INSENSITIVE = msg(
    "tui.agent_config.validation.paths_match_case_insensitive",
    fallback="paths are matched case-insensitively after normalization",
)
PATHS_MATCH_NORMALIZED = msg(
    "tui.agent_config.validation.paths_match_normalized",
    fallback="paths are matched after normalization",
)
MEMORY_CONTEXT = msg(
    "tui.agent_config.validation.memory_context",
    fallback="Memory {label} {index}",
)
COMPACTION_REQUIRED = msg(
    "tui.agent_config.validation.compaction_required",
    fallback="{label} is required (use {default_value} for the default).",
)
COMPACTION_WHOLE_NUMBER = msg(
    "tui.agent_config.validation.compaction_whole_number",
    fallback="{label} must be a whole number, got {value}.",
    multiline=True,
)
COMPACTION_RANGE = msg(
    "tui.agent_config.validation.compaction_range",
    fallback="{label} must be between {minimum} and {maximum}.",
)
LAST_WORDS_RANGE = msg(
    "tui.agent_config.validation.last_words_range",
    fallback="last words max output tokens must be between {minimum} and {maximum}.",
)

# MCP validation.
MCP_SERVER_CONTEXT = msg("tui.agent_config.validation.mcp.server_context", fallback="Server {index}")
MCP_INITIALLY_VISIBLE_TOOLS_VALIDATION = msg(
    "tui.agent_config.validation.mcp.initially_visible_tools_label",
    fallback="Initially Visible Tools",
)
MCP_DUPLICATE_SERVER = msg(
    "tui.agent_config.validation.mcp.duplicate_server",
    fallback="Duplicate MCP server name: '{name}'.",
    multiline=True,
)
MCP_DUPLICATE_SERVER_LOWER = msg(
    "tui.agent_config.validation.mcp.duplicate_server_lower",
    fallback="duplicate MCP server name: '{name}'.",
    multiline=True,
)
MCP_URL_REQUIRED = msg(
    "tui.agent_config.validation.mcp.url_required",
    fallback="URL is required for HTTP transport.",
)
MCP_URL_SCHEME = msg(
    "tui.agent_config.validation.mcp.url_scheme",
    fallback="URL must start with http:// or https://.",
)
MCP_COMMAND_REQUIRED = msg(
    "tui.agent_config.validation.mcp.command_required",
    fallback="command is required for stdio transport.",
)
MCP_CWD_NUL = msg(
    "tui.agent_config.validation.mcp.cwd_nul",
    fallback="Working Directory cannot contain NUL characters.",
)
MCP_TRANSPORT = msg(
    "tui.agent_config.validation.mcp.transport",
    fallback="transport must be stdio or http.",
)
MCP_TIMEOUT_POSITIVE = msg(
    "tui.agent_config.validation.mcp.timeout_positive",
    fallback="timeout must be a positive integer.",
)
MCP_TIMEOUT_VALID = msg(
    "tui.agent_config.validation.mcp.timeout_valid",
    fallback="timeout must be a valid integer.",
)
MCP_INVALID_COMMAND = msg(
    "tui.agent_config.validation.mcp.invalid_command",
    fallback="Invalid command line: {detail}",
    multiline=True,
)
MCP_TOOL_LIST_EMPTY = msg(
    "tui.agent_config.validation.mcp.tool_list_empty",
    fallback="{label} must not contain an empty {item}.",
)
MCP_TOOL_LIST_DUPLICATE = msg(
    "tui.agent_config.validation.mcp.tool_list_duplicate",
    fallback="{label} contains duplicate tool name '{name}'.",
    multiline=True,
)
MCP_SELECT_PERMITTED = msg(
    "tui.agent_config.validation.mcp.select_permitted",
    fallback="select at least one available tool or choose {no_tools}.",
)
MCP_INITIAL_TOOLS_PERMITTED = msg(
    "tui.agent_config.validation.mcp.initial_tools_permitted",
    fallback="{label} must also be included in Available Tool Scope: {names}.",
    multiline=True,
)
MCP_ROW_VALUE_REQUIRED = msg(
    "tui.agent_config.validation.mcp.row_value_required",
    fallback="{label} row {row}: value is required for key '{key}'.",
    multiline=True,
)
MCP_ROW_KEY_REQUIRED = msg(
    "tui.agent_config.validation.mcp.row_key_required",
    fallback="{label} row {row}: key name is required when a value is set.",
)
