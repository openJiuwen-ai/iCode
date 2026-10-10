# Copyright (c) Microsoft. All rights reserved.
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# Contains code adapted from Microsoft Agent Framework (MIT License; see NOTICE).

"""Chrys-owned file-based skill discovery and frontmatter parsing.

Scans skill directories directly, following the
`Agent Skills specification <https://agentskills.io/specification>`_:

* a skill is a directory containing a ``SKILL.md`` with YAML frontmatter
  whose ``name`` matches the directory name;
* discovery searches each configured root up to 2 directory levels deep;
* the first ``SKILL.md`` found on a branch establishes the skill boundary,
  so nested directories and files belong to that parent skill;
* resource and script files are discovered recursively within each skill
  directory by extension, up to :data:`DEFAULT_SEARCH_DEPTH`.

Parsing and failure reporting contracts:

* Frontmatter is parsed with PyYAML (full YAML fidelity) with a tolerant
  line-based fallback for files whose frontmatter is not valid YAML but
  contain scalar fields accepted by the legacy line-based parser.
* Load failures are returned as :class:`SkillLoadFailure` records carrying
  the actual reason, instead of being silently skipped and reconstructed
  by directory comparison in the adapter.

Path-traversal and symlink-escape guards are required: skill directories
may come from third-party repositories, so discovered resource/script paths
must stay inside the skill directory.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import date
from html import escape as xml_escape
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

import yaml

from chrys.foundation.text.encoding import decode_bytes
from chrys.service.skills.model import Skill, SkillLoadFailure, SkillResource, SkillScript

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

logger = logging.getLogger(__name__)

SKILL_FILE_NAME = "SKILL.md"
# How deep to search for SKILL.md files within configured skill roots.
# This is separate from DEFAULT_SEARCH_DEPTH, which controls per-skill file scanning.
MAX_SEARCH_DEPTH = 2
MAX_NAME_LENGTH = 64
MAX_DESCRIPTION_LENGTH = 1024
MAX_COMPATIBILITY_LENGTH = 500
MAX_FRONTMATTER_LENGTH = 16 * 1024
MAX_FRONTMATTER_DEPTH = 64

# Text-based formats that are safe to advertise (and read) as skill resources.
# Binary assets (images, fonts, archives) are deliberately excluded.
DEFAULT_RESOURCE_EXTENSIONS: tuple[str, ...] = (
    # Documentation / markup
    ".md",
    ".txt",
    ".rst",
    ".html",
    ".htm",
    ".xml",
    ".svg",
    # Structured data
    ".json",
    ".jsonl",
    ".yaml",
    ".yml",
    ".toml",
    ".csv",
    ".tsv",
    # Config / styling
    ".ini",
    ".cfg",
    ".css",
)

# How deep to scan resource/script files within an individual skill directory.
# Depth 1 means the skill root only; depth 2 means root plus one subdirectory level.
DEFAULT_SEARCH_DEPTH = 2

# Lowercase letters, numbers, hyphens; no leading/trailing/consecutive hyphens.
VALID_NAME_RE = re.compile(r"^[a-z0-9]([a-z0-9]*-[a-z0-9])*[a-z0-9]*$")

# YAML frontmatter delimited by "---" lines; the \uFEFF (regex-level escape)
# allows an optional UTF-8 BOM before the opening delimiter. Each delimiter is one
# line, so a file of blank lines with no closing one fails in linear time.
FRONTMATTER_RE = re.compile(r"\A\uFEFF?---[ \t]*\r?\n(.*?)^---[ \t]*\r?$", re.MULTILINE | re.DOTALL)

# What a YAML scalar loads as; a list or mapping is none of these.
_SCALAR_TYPES = (str, int, float, date)


class _FrontmatterRefused(Exception):
    """Frontmatter the loader will not compose; the message is the reason."""


class _FrontmatterLoader(yaml.SafeLoader):
    """``SafeLoader`` that refuses anchors, aliases and deep nesting as it composes each node.

    An alias expands before any length check: a few KiB of them can load as
    gigabytes. Refusing node by node keeps the parse lazy, so nesting past
    :data:`MAX_FRONTMATTER_DEPTH` stops after work that does not grow with the
    document; scanning it whole first would cost time that grows with its nesting.
    """

    def __init__(self, stream: str) -> None:
        super().__init__(stream)
        self._depth = 0

    def compose_node(self, parent: yaml.Node | None, index: object) -> yaml.Node | None:
        event = self.peek_event()
        if isinstance(event, yaml.NodeEvent) and event.anchor is not None:
            raise _FrontmatterRefused("YAML frontmatter must not use anchors or aliases")
        if self._depth >= MAX_FRONTMATTER_DEPTH:
            raise _FrontmatterRefused(f"YAML frontmatter must not nest more than {MAX_FRONTMATTER_DEPTH} levels deep")
        self._depth += 1
        try:
            return super().compose_node(parent, index)
        finally:
            self._depth -= 1


# Tolerant fallback for frontmatter that is not valid YAML: top-level
# "key: value" lines with optional single/double quoting. The value runs to its
# line's end with no backtracking, so a long line costs linear time.
_LINE_KV_RE = re.compile(r"^([\w-]+)\s*:\s*(\S.*)$", re.MULTILINE)
_QUOTES = "\"'"


def validate_skill_metadata(name: str | None, description: str | None, compatibility: str | None = None) -> str | None:
    """Validate spec naming rules; return a diagnostic string or ``None`` when valid."""
    if not name or not name.strip():
        return "frontmatter is missing a `name`"
    if len(name) > MAX_NAME_LENGTH or not VALID_NAME_RE.match(name):
        return (
            f"invalid skill name '{name}': must be {MAX_NAME_LENGTH} characters or fewer, "
            "using only lowercase letters, numbers, and hyphens, with no leading, trailing, "
            "or consecutive hyphens"
        )
    if not description or not description.strip():
        return f"skill '{name}' is missing a `description`"
    if len(description) > MAX_DESCRIPTION_LENGTH:
        return f"skill '{name}' has an invalid description: must be {MAX_DESCRIPTION_LENGTH} characters or fewer"
    if compatibility is not None and len(compatibility) > MAX_COMPATIBILITY_LENGTH:
        return f"skill '{name}' has an invalid compatibility: must be {MAX_COMPATIBILITY_LENGTH} characters or fewer"
    return None


def _coerce_scalar(value: object) -> str | None:
    """Return a scalar field as text; None when it is absent, a list or a mapping."""
    if not isinstance(value, _SCALAR_TYPES):
        return None
    return value if isinstance(value, str) else str(value)


def _fallback_line_parse(yaml_block: str) -> dict[str, object]:
    """Parse top-level ``key: value`` lines from frontmatter that is not valid YAML."""
    fields: dict[str, object] = {}
    for match in _LINE_KV_RE.finditer(yaml_block):
        value = match.group(2).rstrip()
        if len(value) > 2 and value[0] in _QUOTES and value[-1] in _QUOTES:
            value = value[1:-1]
        fields.setdefault(match.group(1).lower(), value)
    return fields


def parse_frontmatter(content: str) -> tuple[dict[str, object] | None, str | None]:
    """Extract frontmatter fields from SKILL.md text.

    Returns ``(fields, None)`` on success — *fields* keyed by lowercased
    top-level keys — or ``(None, reason)`` when the frontmatter block is
    missing or unparseable.
    """
    match = FRONTMATTER_RE.search(content)
    if not match:
        return None, "SKILL.md does not contain YAML frontmatter delimited by '---' lines"

    yaml_block = match.group(1)
    if len(yaml_block) > MAX_FRONTMATTER_LENGTH:
        return None, f"YAML frontmatter must be {MAX_FRONTMATTER_LENGTH} characters or fewer"
    loader: _FrontmatterLoader | None = None
    try:
        loader = _FrontmatterLoader(yaml_block)  # Raises at once on a character YAML does not allow.
        data = loader.get_single_data()
    except _FrontmatterRefused as exc:
        return None, str(exc)
    except yaml.YAMLError as exc:
        fields = _fallback_line_parse(yaml_block)
        if fields:
            logger.warning("SKILL.md frontmatter is not valid YAML (%s); using tolerant line parsing", exc)
            return fields, None
        return None, f"invalid YAML frontmatter: {exc}"
    finally:
        if loader is not None:
            loader.dispose()

    if data is None:
        return {}, None
    if not isinstance(data, dict):
        return None, "YAML frontmatter must be a key-value mapping"
    return {str(key).lower(): value for key, value in data.items()}, None


def _parse_metadata(value: object) -> dict[str, str] | None:
    """Return a flat mapping of scalars as text; anything else is dropped whole."""
    if not isinstance(value, dict) or not all(
        item is None or isinstance(item, _SCALAR_TYPES) for pair in value.items() for item in pair
    ):
        return None
    return {str(k): str(v) for k, v in value.items()}


def discover_skill_directories(skill_paths: Sequence[str]) -> list[str]:
    """Return absolute paths of all directories containing a ``SKILL.md`` file.

    Searches each existing root up to :data:`MAX_SEARCH_DEPTH` levels deep.
    Once a ``SKILL.md`` is found, that directory is the skill root and the
    search does not descend further: everything below the boundary belongs to
    that skill rather than defining another independent skill.
    """
    discovered: list[str] = []

    def search(directory: Path, depth: int) -> None:
        if (directory / SKILL_FILE_NAME).is_file():
            discovered.append(str(directory.absolute()))
            return
        if depth >= MAX_SEARCH_DEPTH:
            return
        try:
            entries = list(directory.iterdir())
        except OSError:
            return
        for entry in entries:
            if entry.is_dir():
                search(entry, depth + 1)

    for root in skill_paths:
        if not root or not root.strip() or not Path(root).is_dir():
            continue
        search(Path(root), 0)

    return discovered


def _normalize_rel_path(path: str) -> str:
    """Normalize a relative path to canonical forward-slash form."""
    return PurePosixPath(path.replace("\\", "/")).as_posix()


def _is_path_within_directory(path: str, directory: str) -> bool:
    try:
        return Path(path).is_relative_to(directory)
    except ValueError, OSError:
        return False


def is_contained_skill_path(path: str, skill_dir: str) -> bool:
    """Whether *path* lies under *skill_dir* with no symlink or junction below it.

    The scan admits only such files; a reader re-checks before opening one,
    since the tree may have changed since the scan. The window between this
    check and the open stays: a process that can swap files in the skill
    directory while a read is in flight could as well copy any file it can
    read into it.
    """
    root = str(Path(skill_dir).absolute())
    full_path = str(Path(os.path.normpath(path)).absolute())
    try:
        return _is_path_within_directory(full_path, root) and not _has_symlink_in_path(full_path, root)
    except OSError:
        return False


def _has_symlink_in_path(path: str, directory: str) -> bool:
    """Detect symlinks or NT junctions in the segments of *path* below *directory*.

    Precondition: *path* is a descendant of *directory* (checked via
    :func:`_is_path_within_directory` first).
    """
    dir_path = Path(directory)
    relative = Path(path).relative_to(dir_path)
    current = dir_path
    for part in relative.parts:
        current = current / part
        if current.is_symlink() or current.is_junction():
            return True
    return False


def _scan_skill_files(
    skill_dir_path: str,
    extensions: Sequence[str],
    *,
    search_depth: int,
    exclude_skill_file: bool,
    skill_name: str,
    file_filter: Callable[[str, str], bool] | None = None,
) -> list[tuple[str, str]]:
    """Recursively scan a skill directory for files matching *extensions*.

    ``search_depth=1`` scans only the skill root; ``2`` scans the root plus
    one subdirectory level.  Candidates failing containment or symlink checks
    are skipped with a warning. Nested ``SKILL.md`` files do not create a new
    skill boundary during this per-skill scan: their sibling resources and
    scripts belong to the parent, while every ``SKILL.md`` remains excluded.
    Returns ``(name, full_path)`` pairs sorted by name: the skill-relative
    forward-slash display name and the absolute path that passed the checks.
    Readers use that path, never one rebuilt from the name: on POSIX a
    backslash is an ordinary file-name character, so ``..\\x`` would
    rebuild into a path outside the skill.
    """
    if search_depth < 1:
        raise ValueError(f"search_depth must be >= 1, got {search_depth}")

    skill_dir = Path(skill_dir_path).absolute()
    root_directory = str(skill_dir)
    normalized_extensions = {e.lower() for e in extensions}
    found: list[tuple[str, str]] = []

    def scan_directory(target_dir: Path, current_depth: int) -> None:
        if current_depth > search_depth:
            return

        is_root = target_dir == skill_dir
        resolved_target = str(Path(os.path.normpath(target_dir)).absolute())

        if not is_root:
            if not _is_path_within_directory(resolved_target, root_directory):
                logger.warning(
                    "Skipping directory '%s': resolves outside skill directory '%s'",
                    target_dir,
                    root_directory,
                )
                return
            if _has_symlink_in_path(resolved_target, root_directory):
                logger.warning(
                    "Skipping directory '%s': symlink detected under skill directory '%s'",
                    target_dir,
                    root_directory,
                )
                return

        try:
            entries = list(target_dir.iterdir())
        except OSError:
            logger.warning(
                "Failed to list directory '%s' in skill directory '%s'; skipping", target_dir, root_directory
            )
            return

        subdirectories: list[Path] = []

        for entry in entries:
            if entry.is_dir():
                subdirectories.append(entry)
                continue
            if not entry.is_file():
                continue
            if exclude_skill_file and entry.name.upper() == SKILL_FILE_NAME.upper():
                continue
            if entry.suffix.lower() not in normalized_extensions:
                continue

            full_path = str(Path(os.path.normpath(entry)).absolute())
            if not _is_path_within_directory(full_path, root_directory):
                logger.warning("Skipping file '%s': resolves outside skill directory '%s'", entry, root_directory)
                continue
            if _has_symlink_in_path(full_path, root_directory):
                logger.warning("Skipping file '%s': symlink detected under skill directory '%s'", entry, root_directory)
                continue

            rel_path = _normalize_rel_path(str(entry.relative_to(skill_dir)))
            if file_filter is not None and not file_filter(skill_name, rel_path):
                continue

            found.append((rel_path, full_path))

        if current_depth < search_depth:
            for subdir in subdirectories:
                scan_directory(subdir, current_depth + 1)

    scan_directory(skill_dir, 1)

    found.sort()
    return found


def load_file_skill(
    skill_dir: str,
    *,
    resource_extensions: Sequence[str] = DEFAULT_RESOURCE_EXTENSIONS,
    script_extensions: Sequence[str],
    search_depth: int = DEFAULT_SEARCH_DEPTH,
    script_filter: Callable[[str, str], bool] | None = None,
    resource_filter: Callable[[str, str], bool] | None = None,
) -> Skill | SkillLoadFailure:
    """Load a single skill from a directory containing ``SKILL.md``."""
    if search_depth < 1:
        raise ValueError(f"search_depth must be >= 1, got {search_depth}")

    skill_dir = str(Path(skill_dir).absolute())
    skill_file = Path(skill_dir) / SKILL_FILE_NAME

    try:
        content = decode_bytes(skill_file.read_bytes(), errors="strict").replace("\r\n", "\n").replace("\r", "\n")
    except (OSError, UnicodeError) as exc:
        return SkillLoadFailure(skill_dir=skill_dir, reason=f"failed to read SKILL.md: {exc}")

    fields, parse_error = parse_frontmatter(content)
    if fields is None:
        return SkillLoadFailure(skill_dir=skill_dir, reason=parse_error or "invalid frontmatter")

    for key in ("name", "description"):
        value = fields.get(key)
        if value is not None and not isinstance(value, _SCALAR_TYPES):
            return SkillLoadFailure(
                skill_dir=skill_dir, reason=f"frontmatter `{key}` must be text, not a list or mapping"
            )
    name = _coerce_scalar(fields.get("name"))
    description = _coerce_scalar(fields.get("description"))
    compatibility = _coerce_scalar(fields.get("compatibility"))

    error = validate_skill_metadata(name, description, compatibility)
    if error or name is None or description is None:
        return SkillLoadFailure(skill_dir=skill_dir, reason=error or "invalid frontmatter")

    dir_name = Path(skill_dir).name
    if name != dir_name:
        return SkillLoadFailure(
            skill_dir=skill_dir,
            reason=(
                f"frontmatter `name` '{name}' does not match the directory name '{dir_name}' "
                "(required by the Agent Skills specification)"
            ),
        )

    resources = [
        SkillResource(name=rel, full_path=full_path)
        for rel, full_path in _scan_skill_files(
            skill_dir,
            resource_extensions,
            search_depth=search_depth,
            exclude_skill_file=True,
            skill_name=name,
            file_filter=resource_filter,
        )
    ]
    scripts = [
        SkillScript(name=rel, full_path=full_path)
        for rel, full_path in _scan_skill_files(
            skill_dir,
            script_extensions,
            search_depth=search_depth,
            exclude_skill_file=True,
            skill_name=name,
            file_filter=script_filter,
        )
    ]

    return Skill(
        name=name,
        description=description,
        content=content,
        path=skill_dir,
        resources=resources,
        scripts=scripts,
        license=_coerce_scalar(fields.get("license")),
        compatibility=compatibility,
        allowed_tools=_coerce_scalar(fields.get("allowed-tools")),
        metadata=_parse_metadata(fields.get("metadata")),
    )


def discover_file_skills(
    skill_paths: Sequence[str],
    *,
    resource_extensions: Sequence[str] = DEFAULT_RESOURCE_EXTENSIONS,
    script_extensions: Sequence[str],
    search_depth: int = DEFAULT_SEARCH_DEPTH,
    script_filter: Callable[[str, str], bool] | None = None,
    resource_filter: Callable[[str, str], bool] | None = None,
) -> tuple[list[Skill], list[SkillLoadFailure]]:
    """Discover all file-based skills under *skill_paths*.

    Returns ``(skills, failures)``.  Duplicate skill names keep the first
    discovered skill (with a log warning, matching prior behavior); duplicates
    are not reported as failures.
    """
    if search_depth < 1:
        raise ValueError(f"search_depth must be >= 1, got {search_depth}")

    skills: dict[str, Skill] = {}
    failures: list[SkillLoadFailure] = []

    discovered = discover_skill_directories(skill_paths)
    logger.info("Discovered %d potential skill directories", len(discovered))

    for skill_dir in discovered:
        try:
            loaded = load_file_skill(
                skill_dir,
                resource_extensions=resource_extensions,
                script_extensions=script_extensions,
                search_depth=search_depth,
                script_filter=script_filter,
                resource_filter=resource_filter,
            )
        except Exception as exc:
            # One SKILL.md that breaks the loader (a YAML date with no such day, say) fails alone.
            loaded = SkillLoadFailure(skill_dir=skill_dir, reason=f"failed to load SKILL.md: {exc}")
        if isinstance(loaded, SkillLoadFailure):
            logger.error("Failed to load skill from '%s': %s", loaded.skill_dir, loaded.reason)
            failures.append(loaded)
            continue

        key = loaded.name.casefold()
        if key in skills:
            logger.warning(
                "Duplicate skill name '%s': skill from '%s' skipped in favor of existing skill",
                loaded.name,
                skill_dir,
            )
            continue

        skills[key] = loaded
        logger.info("Loaded skill: %s", loaded.name)

    return list(skills.values()), failures


def build_inline_skill_content(
    name: str,
    description: str,
    instructions: str,
    resources: Sequence[SkillResource] | None = None,
) -> str:
    """Build the XML ``load_skill`` document for an inline (profile YAML) skill.

    Empty resource and script categories are emitted as self-closing elements
    (``<resources />`` / ``<scripts />``) so the model knows none are available
    and does not hallucinate their names.
    """
    result = (
        f"<name>{xml_escape(name)}</name>\n"
        f"<description>{xml_escape(description)}</description>\n"
        "\n"
        "<instructions>\n"
        f"{instructions}\n"
        "</instructions>"
    )
    lines = []
    for resource in resources or ():
        attrs = f'name="{xml_escape(resource.name, quote=True)}"'
        if resource.description:
            attrs += f' description="{xml_escape(resource.description, quote=True)}"'
        lines.append(f"  <resource {attrs}/>")
    result += f"\n\n{_build_element_block('resources', lines)}"
    result += "\n\n<scripts />"
    return result


def _build_element_block(tag_name: str, lines: list[str]) -> str:
    """Return a named XML-ish block, self-closing when it has no child lines."""
    if not lines:
        return f"<{tag_name} />"
    return f"<{tag_name}>\n" + "\n".join(lines) + f"\n</{tag_name}>"
