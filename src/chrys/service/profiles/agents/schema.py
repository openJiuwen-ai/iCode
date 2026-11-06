# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""AgentProfile data model — defines the structure of an agent profile configuration."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from chrys.foundation.tool_kinds import KIND_FILESYSTEM_WRITE, KIND_SHELL, KIND_TODO
from chrys.service.tools.builtins.web.config import WebFetchConfigPatch, WebSearchConfigPatch


@dataclass
class AcpAgentConfig:
    """Configuration for an external ACP agent launched over stdio."""

    command: str = ""
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    cwd: str = ""
    allow_external_cwd: bool = False
    session_mode: str = ""
    model_id: str = ""
    config_options: dict[str, str | bool] = field(default_factory=dict)
    best_effort_options: bool = False
    result_mode: Literal["last_segment", "transcript"] = "last_segment"
    handshake_timeout_seconds: float = 30.0
    idle_timeout_seconds: float = 600.0


@dataclass
class MCPServerConfig:
    """Configuration for an MCP server.

    Supports two transport types via the ``transport`` discriminator:

    - ``stdio``: Spawn a local process (requires ``command``).
    - ``http``: Connect via Streamable HTTP/SSE (requires ``url``).

    Common optional fields: ``description``, ``tool_name_prefix``,
    ``allowed_tools``, ``request_timeout``.
    """

    name: str
    transport: Literal["stdio", "http"]

    # stdio transport
    command: str = ""
    args: list[str] = field(default_factory=list)
    encoding: str | None = None

    # http transport
    url: str = ""
    headers: dict[str, str] = field(default_factory=dict)  # http only
    resolve_header_templates: bool = True  # http only; profile YAML headers may use {{ENV_VAR}} templates
    terminate_on_close: bool | None = None  # http only
    verify_ssl: bool = True  # http only — set False to disable TLS cert verification (insecure)
    bypass_proxy: bool = False  # http only — route this server around configured proxies

    # stdio only — env vars exported to the spawned subprocess (the MCP
    # server itself sees them). Ignored for HTTP transport.
    env: dict[str, str] = field(default_factory=dict)

    # common options
    load_prompts: bool = True
    """Expose prompts advertised by the server as callable tools in the model's tool list.

    Connection-level flag: it changes the tool catalog the connection
    delivers, so it is part of the MCP connection-cache key (unlike
    ``expose_instructions`` below).
    """
    expose_instructions: bool = True
    """Inject the server's ``InitializeResult.instructions`` into system reminders.

    Presentation-level flag: it never affects the connection itself (and is
    deliberately excluded from the MCP connection-cache key), so profiles
    sharing a cached connection may disagree on it.
    """
    enabled: bool = True
    description: str = ""
    tool_name_prefix: str = ""
    # ``None`` means no allow-list was specified, so all server tools are exposed.
    # An explicit empty list is meaningful: expose no tools.
    allowed_tools: list[str] | None = None
    use_progressive_disclosure: bool = False
    """Loading strategy: False exposes the permitted catalog fully; True loads it on demand."""
    always_load: list[str] = field(default_factory=list)
    """Initial visible subset for progressive loading; must also be permitted by ``allowed_tools``."""
    request_timeout: int | None = None
    max_tool_result_tokens: int | None = None


@dataclass
class SkillResourceConfig:
    """Configuration for a skill resource (supplementary content)."""

    name: str
    description: str = ""
    content: str = ""  # inline content
    path: str = ""  # or file path (relative to skill directory)


@dataclass
class SkillScriptConfig:
    """Configuration for a skill script (executable file)."""

    name: str
    description: str = ""
    path: str = ""  # script file path


@dataclass
class SkillConfig:
    """Inline skill definition.

    Maps to chrys's ``Skill`` model (``chrys.service.skills.model``). The agent
    discovers skills via progressive disclosure: names/descriptions are
    advertised in the runtime skill catalog, and the agent calls
    ``load_skill`` to get full instructions.
    """

    name: str
    description: str = ""
    instructions: str = ""
    resources: list[SkillResourceConfig] = field(default_factory=list)
    scripts: list[SkillScriptConfig] = field(default_factory=list)


@dataclass
class SkillsConfig:
    """Skills configuration for an agent profile.

    Skills can come from two sources:
    - ``paths``: directories scanned for SKILL.md files (file-based skills)
    - ``inline``: skill definitions declared directly in the YAML profile
    """

    paths: list[str] = field(default_factory=list)
    inline: list[SkillConfig] = field(default_factory=list)
    script_timeout: int = 300  # seconds
    script_extensions: list[str] = field(default_factory=lambda: [".py", ".sh", ".ps1"])
    auto_load_user_agents_skills: bool = True
    """When True, also auto-load skills from ``~/.agents/skills`` (the shared
    user agents skills directory) in addition to the always-on
    ``~/.chrys/skills``. Default True so new profiles opt in by default."""
    auto_load_cwd_agents_skills: bool = True
    """When True, also auto-load skills from ``<cwd>/.agents/skills`` — the
    project-local agents skills directory under the current working directory.
    Reloaded automatically when the workspace cwd changes (``/chdir`` or the
    file picker) because the agent is rebuilt with a fresh ``SessionEnvironment``.
    Default True; harmless when the directory does not exist."""


@dataclass
class ApprovalConfig:
    """Approval policy for an agent profile."""

    default: Literal["auto", "require", "skip"] = "auto"
    overrides: dict[str, Literal["auto", "require", "skip"]] = field(
        default_factory=lambda: {KIND_SHELL: "require", KIND_FILESYSTEM_WRITE: "require", KIND_TODO: "skip"}
    )
    user_can_override: bool = False


@dataclass
class ModelConfig:
    """Optional model-profile binding for an agent profile.

    ``profile_id`` references a ``ModelProfile`` by **id** (UUID hex,
    stable across renames).  Empty string means:

    - for the main agent: use the session-active profile
      (``Settings.model_profile``);
    - for sub-agents: inherit the parent agent's resolved profile.

    If the id is set but missing from the registry (e.g. the referenced
    profile was deleted), resolution falls back to the same rule as an
    empty id — callers never raise on a stale reference.

    Designed for future extension: per-agent overrides such as
    ``temperature`` or ``max_tokens`` can be added here later and
    layered on top of the resolved ``ModelProfile`` at build time
    without breaking existing YAML.
    """

    profile_id: str = ""


@dataclass
class ShellFilterConfig:
    """Shell command filter configuration.

    Can reference a preset (e.g. ``read_only``) or define a custom
    whitelist/blacklist of allowed/blocked command names.
    """

    preset: str = ""  # "read_only", "unrestricted", or "" (none)
    mode: Literal["whitelist", "blacklist"] = "whitelist"
    commands: list[str] = field(default_factory=list)
    allow_redirections: bool = True
    allow_subshells: bool = True


ALWAYS_LOADED_TOOL_CATEGORIES: tuple[str, ...] = ("sleep",)
"""Built-in tool categories every agent loads, whatever its ``tools.builtins`` lists.

Profiles written while ``sleep`` was a switch may list it: the loader drops it, so a
save no longer writes it, and the tool registry ignores it in a list built in code.
"""


@dataclass
class ToolsConfig:
    """Tool set configuration for an agent profile."""

    builtins: list[str] = field(default_factory=list)
    mcp: list[MCPServerConfig] = field(default_factory=list)
    shell_filter: ShellFilterConfig | None = None
    web_search: WebSearchConfigPatch | None = None
    web_fetch: WebFetchConfigPatch | None = None


DEFAULT_SUB_AGENT_CONCURRENCY = 3


@dataclass
class SubAgentRef:
    """Reference to another agent profile to be used as a sub-agent tool."""

    profile: str  # Profile name (must be registered in AgentProfileRegistry)
    tool_name: str = ""  # Tool name exposed to the parent agent (defaults to profile name)
    tool_description: str = ""  # Tool description (defaults to profile.description)
    max_concurrency: int = DEFAULT_SUB_AGENT_CONCURRENCY  # Max concurrent calls for THIS sub-agent


@dataclass
class SubAgentsConfig:
    """Sub-agent configuration for an agent profile."""

    max_total_concurrency: int = DEFAULT_SUB_AGENT_CONCURRENCY  # Max concurrent calls across all sub-agents
    agents: list[SubAgentRef] = field(default_factory=list)


# Default output-token budget for the Phase-4 LAST_WORDS note call, shared
# with the generator's constructor default and the TUI compaction panel.
DEFAULT_LAST_WORDS_MAX_OUTPUT_TOKENS = 20000

# Phase-4 side-call spend is unlimited by default; the per-turn round limit
# (``_MAX_DROP_ROUNDS_PER_TURN`` in the compaction strategy) is the safety guard.
PHASE4_SIDE_CALL_TOKEN_BUDGET_UNLIMITED = -1


@dataclass
class CompactionConfig:
    """Per-profile compaction configuration with turn-aware four-phase algorithm.

    **Trigger**: compaction thresholds are derived from the active model's
    context window and maximum output-token budget, with a safety margin.

    **Phase 1** (old turns): tool-call results from completed turns are
    summarised oldest-turn-first (keeping call args), stopping when usage
    drops to the derived target.

    **Phase 2** (old turns): completed-turn tool-call groups are removed
    entirely oldest-first.

    **Phase 3** (old turns): completed text-heavy turns are auto-compressed
    oldest-first when Phase 1/2 cannot reach the request budget because
    there are no old tool-call groups left to trim.

    **Phase 4** (current turn): all current-turn tool-call + inline
    assistant-text groups are dropped in a single shot and replaced by a
    ``<system-reminder>[LAST_WORDS]…</system-reminder>`` block appended to
    the current user message.  The reminder is produced by an LLM call
    using a code-owned format contract and base content guidance, followed by
    the optional agent-creator-defined ``last_words_template`` supplement.
    Generator failures (transport error, empty response) and format violations
    share the bounded retry process, and the note is always LLM-generated.
    Regeneration on subsequent Phase 4 triggers within the same turn feeds the
    previous reminder + newly dropped work as a delta, so the note accumulates
    insight without re-summarising from scratch.

    **Force-compress**: after a turn completes, if usage exceeds the derived
    force threshold, the oldest uncompressed turn is auto-compressed
    (cross-turn fold) as a safety net.
    """

    enabled: bool = True
    """Programmatic-only compaction switch; never loaded from or saved to YAML."""

    last_words_template: str = ""
    """Optional agent-specific supplement for the LAST_WORDS summariser.

    The code-owned Task/Progress/Next contract and per-heading base guidance
    are always included.  A non-empty value is appended after them for extra
    compression emphasis or additional optional sections the profile needs;
    it never replaces the contract or base guidance.  Empty means no extra
    guidance is appended.
    """

    last_words_max_output_tokens: int = DEFAULT_LAST_WORDS_MAX_OUTPUT_TOKENS
    """Hard cap on the LAST_WORDS generator's output tokens.

    On reasoning models (OpenAI Responses style) this budget is shared with
    the model's thinking tokens — an 8k cap observed live left a
    reasoning-heavy side call too little room for the visible note, so the
    default carries headroom.  Clamped to the model profile's
    ``max_output_tokens`` at generation time when that is lower.
    """

    phase4_side_call_token_budget: int = PHASE4_SIDE_CALL_TOKEN_BUDGET_UNLIMITED
    """Aggregate estimated Phase-4 input-token budget per logical turn.

    ``-1`` (the default) means unlimited — Phase 4 is bounded by the per-turn
    compaction round limit instead of a spend cap.  ``0`` disables Phase-4
    side calls outright; a positive integer is an absolute per-turn cap on
    estimated side-call input tokens.
    """


@dataclass
class MemoryConfig:
    """Auto-loaded reference content injected into the agent's system prompt.

    Relative files and folders are resolved against the session workspace cwd.
    File and folder paths may also be absolute.  Only ``.md`` and ``.txt``
    files are loaded.  Folders are scanned breadth-first with hard bounds —
    the folder's own files first, then subfolders up to ``FOLDER_MAX_DEPTH``
    levels deep, at most ``FOLDER_MAX_FILES`` files per folder (see
    ``service/context/memory_loader.py``) — following symlinks with cycle
    detection; a file the bounded scan misses can be pinned as an explicit
    file entry.  Combined content is capped at a token budget; when the cap
    is hit, the excess files are skipped and a ``Warning`` event is
    published.
    """

    files: list[str] = field(default_factory=list)
    """Absolute or workspace-cwd-relative paths to individual ``.md``/``.txt`` files."""

    folders: list[str] = field(default_factory=list)
    """Absolute or workspace-cwd-relative folders, scanned (bounded) for ``.md``/``.txt``."""


@dataclass
class AgentProfile:
    """Complete agent profile definition.

    An agent profile defines the agent's behavior in a specific scenario:
    system prompt, tool set, skills, approval policy, and model preferences.
    Lifecycle hooks are configured globally in ``<config_dir>/hooks/hooks.yaml``
    (see :mod:`chrys.service.hooks`) rather than per profile; a hook can target a
    specific profile via its ``match.profile`` clause.

    ``id`` is the stable identifier used for sync and distribution — it
    survives renames and lets us tell apart "the user's Code agent" from
    "the built-in Code agent" after either is updated.  Built-in profiles
    carry hardcoded ids; user profiles get a UUID-hex (first 12 chars)
    assigned at creation time.  Empty string is reserved for legacy YAMLs
    written before ``id`` existed — the registry migrates those by
    generating an id and rewriting the file in place.
    """

    name: str
    id: str = ""
    display_name: str = ""
    description: str = ""
    sub_agent_only: bool = False
    acp: AcpAgentConfig | None = None
    instructions: str = ""
    tools: ToolsConfig = field(default_factory=ToolsConfig)
    extensions: list[str] = field(default_factory=list)
    """The Python extensions the agent loads, by id, in order; one that is not installed stays listed."""
    skills: SkillsConfig = field(default_factory=SkillsConfig)
    approval: ApprovalConfig = field(default_factory=ApprovalConfig)
    sub_agents: SubAgentsConfig = field(default_factory=SubAgentsConfig)
    model: ModelConfig = field(default_factory=ModelConfig)
    compaction: CompactionConfig = field(default_factory=CompactionConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    metadata: dict[str, Any] = field(default_factory=dict)
