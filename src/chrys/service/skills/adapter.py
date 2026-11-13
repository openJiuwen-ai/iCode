# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Skills adapter — converts chrys SkillsConfig into a ChrysSkillsProvider.

Composes the chrys-owned skills stack:

* File-based skills are discovered by :mod:`chrys.service.skills.loader`
  (``SKILL.md`` frontmatter plus recursive, extension-based resource/script
  discovery up to the loader's ``search_depth``). The first ``SKILL.md`` on a
  branch owns all nested content; load failures for discovered roots surface
  as TUI warnings carrying the actual reason.
* Inline YAML skills become :class:`chrys.service.skills.model.Skill`
  instances with synthesized XML content.
* Both are merged and deduplicated (file skills win) before being served
  to :class:`ChrysSkillsProvider` through an async loader callable.

The provider's :meth:`initialize` is awaited inside
:func:`create_skills_provider` so callers can synchronously query
``skill_names()`` / ``skill_sources()`` / ``skill_details()`` right after the
provider is built, even though discovery is otherwise lazy/per-refresh.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING

from chrys.foundation.platform.paths import is_absolute_path, resolve_cross_platform_path
from chrys.service.skills.loader import (
    SKILL_FILE_NAME,
    build_inline_skill_content,
    discover_file_skills,
    validate_skill_metadata,
)
from chrys.service.skills.model import Skill, SkillLoadFailure, SkillProviderWarning, SkillResource
from chrys.service.skills.provider import ChrysSkillsProvider
from chrys.service.skills.runner import SubprocessScriptRunner

if TYPE_CHECKING:
    from chrys.foundation.models.session_env import SessionEnvironment
    from chrys.service.profiles.agents.schema import SkillConfig, SkillsConfig
    from chrys.service.skills.discovery import SkillDiscoveryScope

logger = logging.getLogger(__name__)


SKILLS_STATIC_PROMPT = """\
You have access to skills — packaged domain expertise with instructions, \
reference resources, and executable scripts.

Skills use progressive disclosure: the current runtime skill catalog is appended \
to the latest user message in a system reminder. That catalog shows names, \
descriptions, revisions, and file-based skill directories when available. Full \
content is loaded on demand via skill tools.

## Using skills

1. **Match**: When a task aligns with a skill's description, or the user \
explicitly requests one, proceed to step 2. If multiple skills match, choose \
the most specific. If none apply, proceed normally without loading any skill.

2. **Load**: Call `load_skill` with the skill name to retrieve full \
instructions. The response may list resources and scripts available for that \
skill. Do not re-load a skill already loaded in this conversation unless the \
latest runtime catalog shows a changed skill or the user asks you to refresh it.

3. **Follow**: Execute the loaded instructions. When they reference \
supporting material:
   - Call `read_skill_resource` to read any referenced resource.
{runner_instructions}
   - Pass exact ordered script tokens via the `arguments` array \
(e.g. `arguments: ["input.txt", "output.txt"]`). Use `arguments` for \
subcommands, short flags, repeated flags, global flags before a subcommand, \
and `--` passthrough. Use `args` only as optional sugar for flat, \
order-insensitive CLI flags.

## Parameter naming

Use names exactly as listed — preserve directory prefixes, do not append \
file extensions to skill names:
- `skill_name`: e.g. `"style-guide"` (not `"style-guide.md"`)
- `resource_name`: e.g. `"references/FAQ.md"` (not `"FAQ.md"`)
- `script_name`: e.g. `"scripts/convert.py"` (not `"convert.py"`)
  File-based script names are paths relative to the skill directory, using \
forward slashes on every platform. A script stored at \
`<skill_dir>/scripts/{{skill_script_name}}.py` must be invoked with \
`script_name: "scripts/{{skill_script_name}}.py"`, not \
`"{{skill_script_name}}.py"`.

## Constraints

- Always call `load_skill` before accessing a skill's resources or scripts.
- Only use skill tools to interact with skill content — do not use \
file-read or shell tools to access skill files directly.
- Always execute skill scripts via `run_skill_script`. Never run them \
through shell/bash/zsh/pwsh/powershell tools (e.g. `bash scripts/convert.py`, \
`python scripts/convert.py`, `./scripts/convert.py`) — the skill runner \
handles skill-path validation, interpreter selection, timeouts, approval, \
and provenance consistently. The runner is not a security sandbox: scripts \
retain the user's normal file, network, and process privileges and inherit \
most of the iCode process environment. Use executable skills only from \
sources you trust.
- Do not verify script file existence before calling `run_skill_script`; \
the skill system resolves paths internally.
- Scripts run in the user's primary working directory by default. \
When the catalog lists a `skill_dir` for a skill, it is an absolute path for \
the current platform. Use it to build an absolute `cwd` or argument when the \
skill needs paths relative to its package. Override via the `cwd` parameter \
only when needed, and pass an absolute path (relative paths are rejected).
"""


def _build_inline_skill(config: SkillConfig) -> tuple[Skill, list[SkillProviderWarning]]:
    """Convert a chrys inline ``SkillConfig`` into a chrys :class:`Skill`.

    Only resources with inline ``content`` are translatable.  File-backed
    inline resources (those declared with a ``path``) and inline scripts
    cannot be expressed without a parent ``SKILL.md`` directory, so they are
    dropped and reported through the returned warnings (also logged).

    Raises:
        ValueError: If the skill name or description fails specification
            validation.
    """
    error = validate_skill_metadata(config.name, config.description)
    if error:
        raise ValueError(error)

    resources = [
        SkillResource(
            name=r.name,
            description=r.description or None,
            content=r.content,
        )
        for r in config.resources
        if r.content  # only inline content resources are representable
    ]

    warnings: list[SkillProviderWarning] = []
    skipped_path_resources = [r.name for r in config.resources if r.path and not r.content]
    if skipped_path_resources:
        warnings.append(
            SkillProviderWarning(
                code="skill_load_error",
                message=(
                    f"Inline skill '{config.name}' references resources by path without content "
                    f"({', '.join(skipped_path_resources)}); file-based inline resources are not supported. "
                    "Move the skill into a SKILL.md directory to expose those files."
                ),
            )
        )
    if config.scripts:
        warnings.append(
            SkillProviderWarning(
                code="skill_load_error",
                message=(
                    f"Inline skill '{config.name}' declares {len(config.scripts)} script(s); "
                    "inline file-backed scripts are not supported. Move the skill into a SKILL.md "
                    "directory listed under skills.paths to expose its scripts."
                ),
            )
        )
    for warning in warnings:
        logger.warning(warning.message)

    skill = Skill(
        name=config.name,
        description=config.description,
        content=build_inline_skill_content(
            name=config.name,
            description=config.description,
            instructions=config.instructions,
            resources=resources,
        ),
        resources=resources,
    )
    return skill, warnings


class _FileSkillLoadTracker:
    """Accumulates file-skill load failures as chrys-shaped TUI warnings.

    Each failing skill directory is reported once per provider lifetime so the
    per-turn skill refresh does not re-publish the same warning every turn;
    the expected directories are rediscovered on refresh so later-added
    invalid skills still surface without an agent rebuild.
    """

    def __init__(self) -> None:
        self._warned_skill_dirs: set[str] = set()
        self._pending: list[SkillProviderWarning] = []

    def record(self, failures: list[SkillLoadFailure]) -> None:
        for failure in failures:
            if failure.skill_dir in self._warned_skill_dirs:
                continue
            self._warned_skill_dirs.add(failure.skill_dir)
            skill_dir = Path(failure.skill_dir)
            msg = f"Skill '{skill_dir.name}' failed to load — {failure.reason} ({skill_dir / SKILL_FILE_NAME})."
            logger.warning(msg)
            self._pending.append(SkillProviderWarning(code="skill_load_error", message=msg))

    def drain(self) -> list[SkillProviderWarning]:
        """Return accumulated load warnings and clear the pending queue."""
        warnings = list(self._pending)
        self._pending.clear()
        return warnings


def user_agents_dir() -> Path:
    """Return the shared user agents directory at ``~/.agents``.

    Cross-platform: resolves to ``<drive>:\\Users\\<user>\\.agents`` on Windows
    and ``~/.agents`` on POSIX via ``Path.home()``.  Callers append the relevant
    sub-directory (e.g. ``"skills"``).  Exposed as a module-level helper so
    tests can monkeypatch it to point at a temporary directory, and so the TUI
    can render the concrete path to the user.
    """
    return Path.home() / ".agents"


def _resolve_config_path(raw: str, runtime: SessionEnvironment | None) -> str:
    """Resolve a profile-configured skill path for discovery.

    Relative profile paths are workspace-relative when a ``SessionEnvironment`` is
    available. Returning canonical absolute paths via ``Path.resolve()`` keeps
    path de-duplication stable across equivalent profile entries.
    """
    expanded = os.path.expanduser(raw.strip())
    if runtime is not None and runtime.cwd:
        return resolve_cross_platform_path(expanded, base=runtime.cwd)
    return resolve_cross_platform_path(expanded)


def _is_absolute_config_path(raw: str) -> bool:
    """Return whether a profile-configured path is absolute after user expansion."""
    expanded = os.path.expanduser(raw.strip())
    return Path(expanded).is_absolute() or is_absolute_path(expanded)


def _collect_skill_paths(
    config: SkillsConfig, runtime: SessionEnvironment | None = None, *, project_skills_enabled: bool = False
) -> list[str]:
    """Merge profile skill paths with the default user/project skills directories.

    Three auto-loaded locations:

    * ``~/.chrys/skills/`` — always included so Chrys-specific skills added
      later in a session can be discovered without an agent rebuild.
    * ``~/.agents/skills/`` — included when ``config.auto_load_user_agents_skills``
      is True.  This is a shared cross-tool skills directory opt-in per
      profile (default: True).
    * ``<runtime.cwd>/.agents/skills/`` — included when ``runtime`` is provided,
      the user's ``project.skills_enabled`` setting (*project_skills_enabled*)
      is on AND ``config.auto_load_cwd_agents_skills`` is True.  Reloaded
      automatically when the workspace cwd changes because the agent is rebuilt
      with a fresh ``SessionEnvironment``.
    """
    # Local import: tests monkeypatch chrys.foundation.platform.get_platform, and a
    # module-level from-import would pin the unpatched function at import time.
    from chrys.foundation.platform import get_platform

    paths: list[str] = []
    seen: set[str] = set()

    def append_unique(path: str) -> None:
        if path not in seen:
            paths.append(path)
            seen.add(path)

    for raw in config.paths or []:
        append_unique(_resolve_config_path(raw, runtime))

    # Always-on: ~/.chrys/skills/. Include even when currently empty/missing so
    # a later turn can discover newly-added skills without rebuilding the agent.
    # Missing roots cost one is_dir() check per refresh, which is negligible.
    chrys_user_dir = get_platform().config_dir / "skills"
    append_unique(str(chrys_user_dir.expanduser().resolve()))

    # Opt-in (default on): ~/.agents/skills/.
    if config.auto_load_user_agents_skills:
        shared_dir = user_agents_dir() / "skills"
        append_unique(str(shared_dir.expanduser().resolve()))

    # Project trust (default off), then the profile opt-in (default on):
    # <cwd>/.agents/skills/.  Tied to the agent's SessionEnvironment (not
    # os.getcwd()) so it follows the workspace through soft-restarts triggered
    # by /chdir or the file picker.
    if runtime is not None and project_skills_enabled and config.auto_load_cwd_agents_skills and runtime.cwd:
        cwd_dir = Path(runtime.cwd) / ".agents" / "skills"
        append_unique(str(cwd_dir.expanduser().resolve()))

    return paths


async def create_skills_provider(
    config: SkillsConfig,
    runtime: SessionEnvironment | None = None,
    session_dir: Path | None = None,
    *,
    project_skills_enabled: bool = False,
    discovery_scope: SkillDiscoveryScope | None = None,
) -> tuple[ChrysSkillsProvider | None, list[SkillProviderWarning]]:
    """Create a ChrysSkillsProvider from chrys SkillsConfig.

    Args:
        config: Skill configuration from the agent profile.
        runtime: Optional ``SessionEnvironment`` used by the script runner as the
            default subprocess cwd when the agent does not supply ``cwd``.
        session_dir: Current session directory for recoverable script-output spills.
        project_skills_enabled: The user's ``project.skills_enabled`` setting;
            the working folder's ``.agents/skills`` loads only when it is on.
        discovery_scope: Optional build-local initial file discovery. It is
            never retained by the provider's runtime refresh callback.

    Returns ``(provider, warnings)`` where *warnings* collects any non-fatal
    errors (e.g. a single invalid skill that was skipped).  Empty
    configurations still return a provider so the static skill prompt and
    three skill tools remain stable across profile switches; startup failures
    return ``None``.

    Async because discovery runs eagerly once (off the event loop via
    ``asyncio.to_thread``) so callers can query ``skill_names`` /
    ``skill_sources`` / ``skill_details`` synchronously during startup.
    """
    paths = _collect_skill_paths(config, runtime, project_skills_enabled=project_skills_enabled)
    warnings: list[SkillProviderWarning] = []
    missing_seen: set[str] = set()
    for raw in config.paths:
        resolved = _resolve_config_path(raw, runtime)
        if resolved in missing_seen:
            continue
        missing_seen.add(resolved)
        # Relative skill paths are cwd-scoped and often optional across session
        # restores or directory changes; warn only for stable absolute config.
        if not Path(resolved).is_dir() and _is_absolute_config_path(raw):
            warnings.append(
                SkillProviderWarning(
                    code="skill_path_missing",
                    message=f"Skill path '{raw}' (resolved to '{resolved}') does not exist; skipping.",
                )
            )

    inline_skills: list[Skill] = []
    for s in config.inline:
        try:
            skill, inline_warnings = _build_inline_skill(s)
        except Exception as exc:
            msg = f"Skipping invalid inline skill '{s.name}': {exc}"
            logger.warning(msg)
            warnings.append(SkillProviderWarning(code="skill_load_error", message=msg))
        else:
            inline_skills.append(skill)
            warnings.extend(inline_warnings)

    runner = SubprocessScriptRunner(timeout=config.script_timeout, runtime=runtime, session_dir=session_dir)
    script_ext = tuple(config.script_extensions)
    tracker = _FileSkillLoadTracker()

    def _merge_skills(file_skills: list[Skill], failures: list[SkillLoadFailure]) -> list[Skill]:
        tracker.record(failures)
        merged: list[Skill] = []
        seen: set[str] = set()
        for skill in (*file_skills, *inline_skills):
            key = skill.name.casefold()
            if key in seen:
                logger.warning(
                    "Duplicate skill name '%s': skill skipped in favor of existing skill",
                    skill.name,
                )
                continue
            seen.add(key)
            merged.append(skill)
        return merged

    async def _load_all_skills() -> list[Skill]:
        # Runtime refreshes deliberately never capture the initial discovery scope.
        file_skills: list[Skill] = []
        failures: list[SkillLoadFailure] = []
        if paths:
            file_skills, failures = await asyncio.to_thread(
                discover_file_skills,
                paths,
                script_extensions=script_ext,
            )
        return _merge_skills(file_skills, failures)

    try:
        provider = ChrysSkillsProvider(
            _load_all_skills,
            instruction_template=SKILLS_STATIC_PROMPT,
            script_runner=runner,
            warning_drain=tracker.drain,
        )
    except Exception as exc:
        msg = f"Failed to initialize skills provider: {exc}"
        logger.warning(msg)
        warnings.append(SkillProviderWarning(code="skill_load_error", message=msg))
        return None, warnings

    # Eagerly trigger discovery so synchronous callers (TUI startup screen)
    # can read skill_names / skill_sources / skill_details without awaiting.
    try:
        if discovery_scope is None:
            await provider.initialize()
        else:
            file_skills, failures = await discovery_scope.discover(paths, script_extensions=script_ext)
            await provider.initialize(initial_skills=_merge_skills(file_skills, failures))
    except Exception as exc:
        msg = f"Failed to discover skills: {exc}"
        logger.warning(msg)
        warnings.append(SkillProviderWarning(code="skill_load_error", message=msg))
        return None, warnings

    warnings.extend(tracker.drain())

    return provider, warnings
