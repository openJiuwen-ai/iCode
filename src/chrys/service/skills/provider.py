# Copyright (c) Microsoft. All rights reserved.
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# Contains code adapted from Microsoft Agent Framework (MIT License; see NOTICE).

"""Chrys skills context provider.

Owns the full skills surface on top of the chrys skill model
(:mod:`chrys.service.skills.model`):

1. Static skill instructions and the three stable skill tools
   (``load_skill`` / ``read_skill_resource`` / ``run_skill_script``)
   injected on every run.
2. Volatile runtime skill catalog (names, descriptions, revisions, skill
   directories) rendered into system reminders once per user turn.
3. ``run_skill_script`` with positional ``arguments`` and explicit ``cwd``
   parameters, routed through chrys's :class:`SubprocessScriptRunner`,
   plus fuzzy script-name suggestions on a miss.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import logging
from dataclasses import dataclass, field
from difflib import get_close_matches
from html import escape as xml_escape
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from chrys.foundation.events.types import RuntimeSkillDetails
from chrys.foundation.platform.files import surrogate_safe_text
from chrys.foundation.platform.paths import resolve_cross_platform_path
from chrys.foundation.text.tokenizer import MixedLanguageTokenizer
from chrys.foundation.text.tool_output import truncate_output
from chrys.foundation.tool_call_context import set_tool_context_builder
from chrys.foundation.tool_kinds import KIND_SKILL, set_tool_kind
from chrys.kernel import ContextProvider, FunctionTool
from chrys.service.skills.constants import (
    DEFAULT_RESOURCE_MAX_TOKENS,
    DEFAULT_SCRIPT_RESULT_MAX_TOKENS,
    RUN_SKILL_SCRIPT_TOOL_NAME,
)
from chrys.service.skills.loader import is_contained_skill_path
from chrys.service.skills.model import Skill, SkillResource, SkillScript
from chrys.service.tools.result_metadata import tool_error

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping, Sequence

    from chrys.service.skills.model import SkillProviderWarning, SkillScriptRunner

logger = logging.getLogger(__name__)

_tokenizer = MixedLanguageTokenizer()


# Inserted as the {runner_instructions} value of SKILLS_STATIC_PROMPT.format(...);
# format values are never re-formatted, so braces here are literal prompt text
# and must stay single.
_SCRIPT_RUNNER_INSTRUCTIONS = """
- Use `run_skill_script` to run referenced scripts, using the name exactly as listed.
- File-based script names are relative paths from the skill directory. For a script in the skill's `scripts`
  directory, pass `script_name: "scripts/{skill_script_name}.py"`, not `"{skill_script_name}.py"`.
- Use forward slashes in `script_name` exactly as listed, including on Windows.
- Pass the exact ordered script tokens inside `arguments` (e.g. `arguments: ["input.txt", "output.txt"]`).
- Use `arguments` for anything order-sensitive: subcommands, short flags, repeated flags, global flags
  before a subcommand, or `--` passthrough.
- Use `args` only as optional sugar for flat, order-insensitive CLI flags
  (e.g. `args: {"length": 24}` appends `--length 24` after `arguments`).
- Do not pass script arguments as top-level tool parameters.
"""


def _catalog_skill_dir(skill: Skill) -> str | None:
    """Return an absolute, platform-aware skill directory string for prompt display.

    Inline skills have no filesystem directory, so the catalog hides the
    ``<skill_dir>`` element for them.
    """
    if not skill.path or not skill.path.strip():
        return None
    return resolve_cross_platform_path(skill.path)


@dataclass(frozen=True)
class StagedSkillRefresh:
    """Discovered skill state that has not yet been committed to the provider cache."""

    skills: list[Skill]
    warnings: list[SkillProviderWarning] = field(default_factory=list)

    def __post_init__(self) -> None:
        # Fix each staged skill's revision at staging time — before any tool
        # body can run — so catalog rendering and persisted provenance agree.
        _warm_skill_revisions(self.skills)

    def skill_names(self) -> list[str]:
        """Return staged skill names in source order."""
        return [skill.name for skill in self.skills]

    def skill_sources(self) -> dict[str, list[str]]:
        """Return staged skill names grouped by source directory."""
        sources: dict[str, list[str]] = {}
        for skill in self.skills:
            source = _catalog_skill_dir(skill) or "Inline profile skills"
            sources.setdefault(source, []).append(skill.name)
        return sources

    def skill_details(self) -> list[RuntimeSkillDetails]:
        """Return staged runtime skill metadata."""
        return _runtime_skill_details(self.skills)

    def render_catalog_reminder(self) -> str:
        """Return the staged runtime skill catalog for a system reminder."""
        return _render_catalog_block(self.skills)


def _runtime_skill_details(skills: Sequence[Skill]) -> list[RuntimeSkillDetails]:
    """Describe *skills* for display and slash-reference reminders, which may reach the model."""
    return [
        RuntimeSkillDetails(
            name=skill.name,
            description=surrogate_safe_text(skill.description or ""),
            source=surrogate_safe_text(_catalog_skill_dir(skill) or "Inline profile skills"),
            content_sha256=hashlib.sha256(skill.content.encode("utf-8", "surrogatepass")).hexdigest(),
            file_based=bool(skill.path),
        )
        for skill in skills
    ]


def _create_static_instructions(
    prompt_template: str | None,
) -> str | None:
    """Create the stable skill instructions injected into the system prompt."""
    if prompt_template is None:
        return None

    try:
        result = prompt_template.format(runner_instructions="__EXEC_PROBE__")
    except (KeyError, IndexError, ValueError) as exc:
        raise ValueError(
            "The provided instruction_template is not a valid format string. "
            "Escape any literal '{' or '}' by doubling them ('{{' or '}}')."
        ) from exc
    if "__EXEC_PROBE__" not in result:
        raise ValueError("The provided instruction_template must contain a '{runner_instructions}' placeholder.")

    return prompt_template.format(runner_instructions=_SCRIPT_RUNNER_INSTRUCTIONS)


def _file_signature(path: str) -> str:
    """Return a compact file identity string for catalog revision hashes.

    Each catalog refresh stats one SKILL.md plus the discovered resource/script
    files per skill.  That is cheap on local filesystems; network-mounted skill
    roots may need a batched strategy if this ever shows up in profiling.
    """
    try:
        stat = Path(path).stat()
    except OSError:
        return path
    return f"{path}:{stat.st_mtime_ns}:{stat.st_size}"


def _skill_revision(skill: Skill) -> str:
    """Return the catalog revision for *skill*, computing and caching it once.

    Cached on the staged ``Skill`` (``Skill.revision``) so every consumer —
    catalog block, ``load_skill`` metadata suffix, persisted call provenance —
    sees one stable value per staging, hashed from the file signatures as
    staged. Provenance builders run *after* the tool body, so recomputing
    there would let a ``run_skill_script`` that mutates its own skill files
    persist a post-run revision instead of the version the model consulted.
    """
    if skill.revision:
        return skill.revision
    digest = hashlib.sha256()

    def add(value: object) -> None:
        # Surrogatepass encodes every lone surrogate, a YAML "\ud800" escape
        # as well as an undecodable path byte, so hashing never fails.
        digest.update(str(value).encode("utf-8", "surrogatepass"))
        digest.update(b"\0")

    add(skill.name)
    add(skill.description or "")
    add(skill.content)
    skill_dir = _catalog_skill_dir(skill)
    if skill_dir is not None:
        add(_file_signature(str(Path(skill_dir) / "SKILL.md")))
    for resource in sorted(skill.resources, key=lambda r: r.name):
        add(resource.name)
        add(resource.description or "")
        if resource.full_path is not None:
            add(_file_signature(resource.full_path))
    for script in sorted(skill.scripts, key=lambda s: s.name):
        add(script.name)
        add(script.description or "")
        add(_file_signature(script.full_path))
    skill.revision = digest.hexdigest()[:12]
    return skill.revision


def _warm_skill_revisions(skills: Sequence[Skill]) -> None:
    """Eagerly stamp the per-staging revision cache on freshly discovered skills.

    Discovery constructs fresh ``Skill`` objects, so warming at every staging
    point both invalidates the previous cache and guarantees the revision is
    fixed *before* any tool body can run.
    """
    for skill in skills:
        _skill_revision(skill)


def _render_catalog_block(skills: Sequence[Skill]) -> str:
    """Render the volatile runtime skill catalog for system reminders."""
    lines: list[str] = []
    lines.append("<available_skills>")
    if not skills:
        lines.append("  <none>No runtime skills are available for the current agent and workspace.</none>")
        lines.append("</available_skills>")
        return "\n".join(lines)
    for skill in sorted(skills, key=lambda s: s.name):
        skill_dir = _catalog_skill_dir(skill)
        lines.append("  <skill>")
        lines.append(f"    <name>{xml_escape(skill.name)}</name>")
        lines.append(f"    <description>{xml_escape(skill.description or '')}</description>")
        lines.append(_skill_revision_element(skill, indent="    "))
        if skill_dir is not None:
            lines.append(f"    <skill_dir>{xml_escape(skill_dir)}</skill_dir>")
        lines.append("  </skill>")
    lines.append("</available_skills>")

    return surrogate_safe_text("\n".join(lines))


def _skill_revision_element(skill: Skill, *, indent: str = "") -> str:
    """Return the model-visible revision element for a skill."""
    return f"{indent}<revision>{xml_escape(_skill_revision(skill))}</revision>"


def _resource_element(resource: SkillResource) -> str:
    """Create a flat resource element for the load-skill metadata block."""
    attrs = f'name="{xml_escape(resource.name, quote=True)}"'
    if resource.description:
        attrs += f' description="{xml_escape(resource.description, quote=True)}"'
    return f"  <resource {attrs}/>"


def _script_element(script: SkillScript) -> str:
    """Create a flat script element for the load-skill metadata block."""
    attrs = f'name="{xml_escape(script.name, quote=True)}"'
    if script.description:
        attrs += f' description="{xml_escape(script.description, quote=True)}"'
    return f"  <script {attrs}/>"


def _metadata_block(tag_name: str, lines: list[str]) -> str:
    """Return a metadata block, self-closing when it has no child lines."""
    if not lines:
        return f"<{tag_name} />"
    return f"<{tag_name}>\n" + "\n".join(lines) + f"\n</{tag_name}>"


def _file_skill_metadata(skill: Skill, skill_dir: str) -> str:
    """Return load-time metadata for file-based skills, including exact script names."""
    lines = [f"<skill_dir>{xml_escape(skill_dir)}</skill_dir>"]

    resource_lines = [_resource_element(resource) for resource in sorted(skill.resources, key=lambda r: r.name)]
    lines.append(_metadata_block("resources", resource_lines))

    script_lines = [_script_element(script) for script in sorted(skill.scripts, key=lambda s: s.name)]
    lines.append(_metadata_block("scripts", script_lines))
    return "\n".join(lines)


def _find_named[T: (SkillResource, SkillScript)](items: Sequence[T], requested: str) -> T | None:
    """Find the item named *requested*, ignoring case.

    The model sees an undecodable byte in a name as ``\\udcXX`` text, so a name
    it sends back may match only that shown form. That match counts when no
    name matches as given and exactly one shows that way.
    """
    wanted = requested.lower()
    found = next((item for item in items if item.name.lower() == wanted), None)
    if found is not None:
        return found
    shown = [item for item in items if surrogate_safe_text(item.name).lower() == wanted]
    return shown[0] if len(shown) == 1 else None


def _suggest_script_name(query: str, scripts: Sequence[SkillScript]) -> str | None:
    """Find the best matching script name and return a suggestion message.

    Checks for suffix matches (e.g. "abc.py" matches "scripts/abc.py") and
    fuzzy matches via ``difflib.get_close_matches``.

    Returns a suggestion string, or ``None`` if no plausible match is found.
    """
    names = [s.name for s in scripts]

    # Exact suffix match: "abc.py" → "scripts/abc.py"
    query_lower = query.lower()
    suffix_matches = [
        n for n in names if n.lower().endswith("/" + query_lower) or n.lower().endswith("\\" + query_lower)
    ]
    if suffix_matches:
        return suffix_matches[0]

    # Basename match: strip directories from both sides
    query_base = query_lower.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    for name in names:
        name_base = name.lower().rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
        if name_base == query_base:
            return name

    # Fuzzy match on full names
    close = get_close_matches(query_lower, [n.lower() for n in names], n=1, cutoff=0.5)
    if close:
        # Map back to original casing
        for name in names:
            if name.lower() == close[0]:
                return name

    return None


class ChrysSkillsProvider(ContextProvider):
    """Chrys-owned skills provider on the chrys skill model.

    Subclasses the chrys-owned kernel :class:`ContextProvider` base so the
    chrys kernel agent invokes ``before_run`` like any other context provider;
    everything else (discovery, catalog, tools, script execution) is chrys code.

    * Runtime catalog rendering includes a ``<skill_dir>`` element for file-based skills.
    * ``run_skill_script`` exposes chrys's ``args`` / ``arguments`` / ``cwd``
      schema and routes execution through chrys's :class:`SubprocessScriptRunner`.
    * ``load_skill`` appends file-skill resource/script metadata.
    * Convenience accessors (:meth:`skill_names`, :meth:`skill_sources`,
      :meth:`skill_details`) read from the provider's cached skills, which
      Chrys refreshes before user turns so later skill edits can be discovered
      without rebuilding the agent.
    """

    DEFAULT_SOURCE_ID: ClassVar[str] = "agent_skills"

    def __init__(
        self,
        load_skills: Callable[[], Awaitable[Sequence[Skill]]],
        *,
        instruction_template: str | None = None,
        disable_caching: bool = False,
        source_id: str | None = None,
        script_runner: SkillScriptRunner | None = None,
        warning_drain: Callable[[], list[SkillProviderWarning]] | None = None,
    ) -> None:
        """Initialize the chrys provider.

        Args:
            load_skills: Async callable performing skill discovery; invoked
                once eagerly via :meth:`initialize` and again on every
                :meth:`refresh_context`.
            instruction_template: Custom static system-prompt template. When
                runner instructions should be embedded, it must contain
                ``{runner_instructions}``.
            disable_caching: Re-run discovery on every ``before_run`` instead
                of serving the cached skill list.
            source_id: Identifier used for instruction/tool attribution in the
                session context. Defaults to :attr:`DEFAULT_SOURCE_ID`.
            script_runner: chrys :class:`SubprocessScriptRunner` instance,
                called with chrys's full ``args``/``arguments``/``cwd``
                signature for file-based scripts.
            warning_drain: Optional callback that returns newly accumulated
                non-fatal discovery warnings after a refresh.
        """
        super().__init__(source_id or self.DEFAULT_SOURCE_ID)
        self._load_skills = load_skills
        self._instruction_template = instruction_template
        self._disable_caching = disable_caching
        self._chrys_runner = script_runner
        self._warning_drain = warning_drain
        self._skills: list[Skill] = []
        self._loaded = False
        self._static_instructions = _create_static_instructions(prompt_template=instruction_template)
        self._chrys_tools: list[FunctionTool] = self._create_tools()

    @property
    def tool_names(self) -> list[str]:
        """Return the stable skill tool names injected before each run."""
        return [tool.name for tool in self._chrys_tools]

    # ------------------------------------------------------------------ #
    # Discovery and cached-skill accessors                                #
    # ------------------------------------------------------------------ #

    async def _reload(self) -> None:
        """Re-run discovery; on failure the previous skill list is retained."""
        skills = list(await self._load_skills())
        _warm_skill_revisions(skills)
        self._skills = skills
        self._loaded = True

    async def initialize(self) -> None:
        """Run first discovery so synchronous accessors are populated.

        ``create_skills_provider`` always calls this before returning, so
        chrys callers reading :meth:`skill_names` / :meth:`skill_sources` /
        :meth:`skill_details` synchronously see the full list at startup.
        """
        if not self._loaded:
            await self._reload()

    def skill_names(self) -> list[str]:
        """Return loaded skill names in source order."""
        return [s.name for s in self._skills]

    def skill_sources(self) -> dict[str, list[str]]:
        """Return loaded skill names grouped by their source directory."""
        sources: dict[str, list[str]] = {}
        for skill in self._skills:
            source = _catalog_skill_dir(skill) or "Inline profile skills"
            sources.setdefault(source, []).append(skill.name)
        return sources

    def skill_details(self) -> list[RuntimeSkillDetails]:
        """Return loaded skill metadata for runtime UI surfaces."""
        return _runtime_skill_details(self._skills)

    async def refresh_context(self) -> list[SkillProviderWarning]:
        """Refresh discovered skills and return newly accumulated warnings.

        Chrys snapshots the runtime skill catalog into system reminders once
        per user turn.  Refreshing here lets later-added or edited file skills
        become visible without rebuilding the whole agent.

        On failure the previous skill list is retained because assignment
        only happens after discovery succeeds; callers decide how to log or
        surface the exception.
        """
        staged = await self.stage_context_refresh()
        return self.commit_context_refresh(staged)

    async def stage_context_refresh(self) -> StagedSkillRefresh:
        """Discover skills without mutating the live provider cache."""
        skills = list(await self._load_skills())
        return StagedSkillRefresh(skills=skills)

    def commit_context_refresh(self, staged: StagedSkillRefresh) -> list[SkillProviderWarning]:
        """Commit a staged runtime skill refresh to the provider cache."""
        self._skills = list(staged.skills)
        self._loaded = True
        warnings = [] if self._warning_drain is None else self._warning_drain()
        return [*staged.warnings, *warnings]

    def render_catalog_reminder(self) -> str:
        """Return the current runtime skill catalog for a system reminder."""
        return _render_catalog_block(self._skills)

    # ------------------------------------------------------------------ #
    # ContextProvider hook                                                #
    # ------------------------------------------------------------------ #

    async def before_run(
        self,
        *,
        agent: Any,
        session: Any,
        context: Any,
        state: dict[str, Any],
    ) -> None:
        """Inject stable skill instructions/tools even when no skills exist."""
        if self._disable_caching or not self._loaded:
            await self._reload()

        if self._static_instructions:
            context.extend_instructions(self.source_id, self._static_instructions)
        context.extend_tools(self.source_id, self._chrys_tools)

    # ------------------------------------------------------------------ #
    # Skill tools                                                         #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _find_skill(skills: Sequence[Skill], name: str) -> Skill | None:
        """Find a skill by name (case-insensitive linear scan)."""
        name_lower = name.lower()
        return next((s for s in skills if s.name.lower() == name_lower), None)

    async def _load_skill(self, skills: Sequence[Skill], skill_name: str) -> str:
        """Return skill content, appending the revision and exact file-based metadata."""
        if not skill_name or not skill_name.strip():
            return tool_error("invalid_skill_name", "Skill name cannot be empty.")

        skill = self._find_skill(skills, skill_name)
        if skill is None:
            return tool_error("skill_not_found", f"Skill '{skill_name}' not found.", details={"skill_name": skill_name})

        logger.info("Loading skill: %s", skill_name)

        metadata = [_skill_revision_element(skill)]
        skill_dir = _catalog_skill_dir(skill)
        if skill_dir is not None:
            metadata.append(_file_skill_metadata(skill, skill_dir))

        suffix = "\n".join(metadata)
        return surrogate_safe_text(f"{skill.content.rstrip()}\n\n{suffix}")

    async def _read_skill_resource(
        self,
        skills: Sequence[Skill],
        skill_name: str,
        resource_name: str,
        max_tokens: int | None = None,
    ) -> Any:
        """Read a named resource from a skill."""
        if not skill_name or not skill_name.strip():
            return tool_error("invalid_skill_name", "Skill name cannot be empty.")

        if not resource_name or not resource_name.strip():
            return tool_error("invalid_resource_name", "Resource name cannot be empty.")

        skill = self._find_skill(skills, skill_name)
        if skill is None:
            return tool_error("skill_not_found", f"Skill '{skill_name}' not found.", details={"skill_name": skill_name})

        resource = _find_named(skill.resources, resource_name)
        if resource is None:
            return tool_error(
                "resource_not_found",
                f"Resource '{resource_name}' not found in skill '{skill_name}'.",
                details={"skill_name": skill_name, "resource_name": resource_name},
            )

        if (
            resource.full_path
            and skill.path
            and not await asyncio.to_thread(is_contained_skill_path, resource.full_path, skill.path)
        ):
            return tool_error(
                "resource_path_outside_skill",
                f"Resource '{resource_name}' resolves outside skill '{skill_name}'.",
                details={"skill_name": skill_name, "resource_name": resource_name},
            )

        try:
            text = await resource.read()
        except Exception:
            logger.exception("Failed to read resource '%s' from skill '%s'", resource_name, skill_name)
            return tool_error(
                "resource_read_failed",
                f"Failed to read resource '{resource_name}' from skill '{skill_name}'.",
                details={"skill_name": skill_name, "resource_name": resource_name},
            )

        # Escaped before it is measured: each lone surrogate grows to six characters.
        text = surrogate_safe_text(text)
        requested = DEFAULT_RESOURCE_MAX_TOKENS if max_tokens is None else max_tokens
        budget = max(100, requested)
        if _tokenizer.count_tokens(text) <= budget:
            return text
        suffix = ""
        if resource.full_path:
            path = surrogate_safe_text(str(Path(resource.full_path)))
            suffix = (
                f"[Full resource available at: {path}\n"
                f"{text.count(chr(10)) + 1} lines, ~{_tokenizer.count_tokens(text)} tokens. "
                "Use read_file or shell tools to inspect it.]"
            )
        return truncate_output(text, budget, head_ratio=2 / 3, truncation_suffix=suffix)

    # ------------------------------------------------------------------ #
    # Persisted call provenance (context builders for the skill tools)
    # ------------------------------------------------------------------ #

    def _skill_provenance(self, args: Mapping[str, Any]) -> tuple[Skill | None, dict[str, Any]]:
        """Resolve canonical skill identity from the final call args.

        Mirrors the tool bodies' own resolution (case-insensitive
        ``_find_skill``): a resolved skill records its canonical name plus the
        staged revision; ``skill_not_found`` records the requested name
        verbatim with no revision (nothing resolved).
        """
        requested = args.get("skill_name")
        if not isinstance(requested, str) or not requested.strip():
            return None, {}
        skill = self._find_skill(self._skills, requested)
        if skill is None:
            return None, {"skill_name": requested}
        return skill, {"skill_name": skill.name, "skill_revision": _skill_revision(skill)}

    def _load_skill_call_context(self, args: Mapping[str, Any]) -> dict[str, Any] | None:
        _, context = self._skill_provenance(args)
        return context or None

    def _read_skill_resource_call_context(self, args: Mapping[str, Any]) -> dict[str, Any] | None:
        skill, context = self._skill_provenance(args)
        requested = args.get("resource_name")
        if isinstance(requested, str) and requested.strip():
            name = requested
            if skill is not None:
                match = _find_named(skill.resources, requested)
                if match is not None:
                    name = match.name
            context["resource_name"] = name
        return context or None

    def _run_skill_script_call_context(self, args: Mapping[str, Any]) -> dict[str, Any] | None:
        skill, context = self._skill_provenance(args)
        requested = args.get("script_name")
        if isinstance(requested, str) and requested.strip():
            name = requested
            if skill is not None:
                match = _find_named(skill.scripts, requested)
                if match is not None:
                    name = match.name
            context["script_name"] = name
        return context or None

    def _create_tools(self) -> list[FunctionTool]:
        """Create stable tools that resolve against the provider's current skills."""

        async def _load(skill_name: str) -> str:
            return await self._load_skill(self._skills, skill_name)

        tools: list[FunctionTool] = [
            FunctionTool(
                name="load_skill",
                description="Loads the full instructions for a specific skill.",
                func=_load,
                input_model={
                    "type": "object",
                    "properties": {
                        "skill_name": {"type": "string", "description": "The name of the skill to load."},
                    },
                    "required": ["skill_name"],
                    "additionalProperties": False,
                },
            ),
        ]

        async def _read_resource(
            skill_name: str,
            resource_name: str,
            max_tokens: int | None = DEFAULT_RESOURCE_MAX_TOKENS,
        ) -> Any:
            return await self._read_skill_resource(self._skills, skill_name, resource_name, max_tokens)

        tools.append(
            FunctionTool(
                name="read_skill_resource",
                description=(
                    "Reads a resource associated with a skill, such as references, assets, or dynamic data. "
                    "Large resources are truncated to max_tokens; file-backed resources include their original path."
                ),
                func=_read_resource,
                input_model={
                    "type": "object",
                    "properties": {
                        "skill_name": {"type": "string", "description": "The name of the skill."},
                        "resource_name": {
                            "type": "string",
                            "description": "The name of the resource.",
                        },
                        "max_tokens": {
                            "type": ["integer", "null"],
                            "default": DEFAULT_RESOURCE_MAX_TOKENS,
                            "description": (
                                "Estimated-token budget for returned content. Values below 100 are clamped to 100."
                            ),
                        },
                    },
                    "required": ["skill_name", "resource_name"],
                    "additionalProperties": False,
                },
            )
        )

        async def _run_script(
            skill_name: str,
            script_name: str,
            args: dict[str, Any] | list[str] | None = None,
            arguments: list[str] | None = None,
            cwd: str | None = None,
            max_tokens: int | None = DEFAULT_SCRIPT_RESULT_MAX_TOKENS,
        ) -> Any:
            return await self._run_skill_script_chrys(
                self._skills,
                skill_name,
                script_name,
                args,
                arguments,
                cwd,
                max_tokens,
            )

        tools.append(
            FunctionTool(
                name=RUN_SKILL_SCRIPT_TOOL_NAME,
                description=(
                    "Runs a script associated with a skill. Large output is truncated to max_tokens; very large "
                    "output keeps only its beginning and end, and the middle cannot be recovered. When possible, "
                    "the kept output is saved under the current session directory."
                ),
                func=_run_script,
                input_model={
                    "type": "object",
                    "properties": {
                        "skill_name": {"type": "string", "description": "The name of the skill."},
                        "script_name": {
                            "type": "string",
                            "description": (
                                "Use the exact name from load_skill, preserving any directory prefix "
                                "(e.g. scripts/convert.py, not convert.py). "
                                "Use forward slashes on all platforms."
                            ),
                        },
                        "args": {
                            "type": ["object", "null"],
                            "additionalProperties": True,
                            "default": None,
                            "description": (
                                "Optional sugar for flat, order-insensitive CLI flags appended after arguments. "
                                "Keys without leading dashes map to long flags (--key value); keys that begin "
                                "with '-' pass through verbatim for short or combined flags. "
                                'For example: {"length": 24, "uppercase": true, "-i": true}. '
                                "If token ordering matters, put every token in arguments instead."
                            ),
                        },
                        "arguments": {
                            "type": ["array", "null"],
                            "items": {"type": "string"},
                            "default": None,
                            "description": (
                                "Exact, ordered CLI tokens to pass after the script name, before any flags "
                                "derived from args. Use this for subcommands, positionals, repeated flags, "
                                "global flags before a subcommand, or -- passthrough. "
                                'For example: ["log", "--oneline", "--max-count", "5"].'
                            ),
                        },
                        "cwd": {
                            "type": ["string", "null"],
                            "default": None,
                            "description": (
                                "Absolute working directory for the script. "
                                "When omitted, defaults to the primary working directory. "
                                "Relative paths are rejected — pass an absolute path only."
                            ),
                        },
                        "max_tokens": {
                            "type": ["integer", "null"],
                            "default": DEFAULT_SCRIPT_RESULT_MAX_TOKENS,
                            "description": (
                                "Estimated-token budget for returned output. Values below 100 are clamped to 100."
                            ),
                        },
                    },
                    "required": ["skill_name", "script_name"],
                    "additionalProperties": False,
                },
            )
        )

        context_builders = {
            "load_skill": self._load_skill_call_context,
            "read_skill_resource": self._read_skill_resource_call_context,
            RUN_SKILL_SCRIPT_TOOL_NAME: self._run_skill_script_call_context,
        }
        for tool in tools:
            set_tool_kind(tool, KIND_SKILL)
            set_tool_context_builder(tool, context_builders[tool.name])

        return tools

    async def _run_skill_script_chrys(
        self,
        skills: Sequence[Skill],
        skill_name: str,
        script_name: str,
        args: dict[str, Any] | list[str] | None = None,
        arguments: list[str] | None = None,
        cwd: str | None = None,
        max_tokens: int | None = None,
    ) -> Any:
        """Run a named script with positional argument support and fuzzy matching.

        Scripts in the chrys model are always file-backed (inline profile
        skills cannot declare scripts), so execution always goes through the
        provider's :class:`SubprocessScriptRunner`.  Suggests the correct
        script name on a miss using fuzzy matching.
        """
        if not skill_name or not skill_name.strip():
            return tool_error("invalid_skill_name", "Skill name cannot be empty.")

        if not script_name or not script_name.strip():
            return tool_error("invalid_script_name", "Script name cannot be empty.")

        skill = self._find_skill(skills, skill_name)
        if not skill:
            return tool_error("skill_not_found", f"Skill '{skill_name}' not found.", details={"skill_name": skill_name})

        scripts = skill.scripts
        script = _find_named(scripts, script_name)
        if not script:
            suggestion = _suggest_script_name(script_name, scripts)
            if suggestion:
                return tool_error(
                    "script_not_found",
                    f"Script '{script_name}' not found in skill '{skill_name}'. Did you mean '{suggestion}'?",
                    details={"skill_name": skill_name, "script_name": script_name, "suggestion": suggestion},
                )
            available = ", ".join(f"'{s.name}'" for s in scripts)
            return tool_error(
                "script_not_found",
                f"Script '{script_name}' not found in skill '{skill_name}'. Available scripts: {available}",
                details={"skill_name": skill_name, "script_name": script_name},
            )

        runner = self._chrys_runner
        if runner is None:
            return tool_error(
                "script_runner_missing",
                (
                    f"Script '{script_name}' in skill '{skill_name}' requires a runner. "
                    "Provide a script_runner when constructing the provider for file-based scripts."
                ),
                details={"skill_name": skill_name, "script_name": script_name},
            )

        try:
            result = runner(skill, script, args, arguments=arguments, cwd=cwd, max_tokens=max_tokens)
            if inspect.isawaitable(result):
                result = await result
            return result
        except Exception:
            logger.exception("Error running script '%s' in skill '%s'", script_name, skill_name)
            return tool_error(
                "script_run_failed",
                f"Failed to run script '{script_name}' in skill '{skill_name}'.",
                details={"skill_name": skill_name, "script_name": script_name},
            )
