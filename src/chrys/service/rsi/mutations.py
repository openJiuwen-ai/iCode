# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Whitelisted profile/skills mutations for RSI Phase 3."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass, replace
from typing import Any, Literal

from chrys.service.profiles.agents.loader import _SKILL_NAME_RE
from chrys.service.profiles.agents.schema import AgentProfile, SkillConfig, SkillsConfig
from chrys.service.rsi.errors import RsiBadRequest

CASE_MARKER_PREFIX = "rsi:case:"


@dataclass(frozen=True, slots=True)
class AppendInstructions:
    """Append text to ``AgentProfile.instructions`` (never replaces the base)."""

    text: str
    kind: Literal["append_instructions"] = "append_instructions"

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "text": self.text}


@dataclass(frozen=True, slots=True)
class UpsertInlineSkill:
    """Add or replace one inline skill by name."""

    name: str
    description: str
    instructions: str
    kind: Literal["upsert_inline_skill"] = "upsert_inline_skill"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class RemoveInlineSkill:
    """Remove an inline skill by name (no-op if absent)."""

    name: str
    kind: Literal["remove_inline_skill"] = "remove_inline_skill"

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "name": self.name}


type Mutation = AppendInstructions | UpsertInlineSkill | RemoveInlineSkill


def case_marker(case_id: str) -> str:
    """Stable instruction marker that profile-aware stub eval treats as a fix."""
    return f"{CASE_MARKER_PREFIX}{case_id}"


def _valid_inline_skill_name(name: str) -> bool:
    """Match agent profile loader rules for inline skill names (``_SKILL_NAME_RE``)."""
    return bool(name) and _SKILL_NAME_RE.fullmatch(name) is not None


def parse_mutation(raw: dict[str, Any]) -> Mutation:
    """Parse one mutation object; unknown kinds fail closed."""
    kind = str(raw.get("kind") or "").strip()
    if kind == "append_instructions":
        text = str(raw.get("text") or "")
        if not text.strip():
            raise RsiBadRequest("append_instructions.text is required")
        return AppendInstructions(text=text)
    if kind == "upsert_inline_skill":
        name = str(raw.get("name") or "").strip()
        if not _valid_inline_skill_name(name):
            raise RsiBadRequest(
                "upsert_inline_skill.name is invalid; use 1-64 lowercase letters, numbers, and single hyphens"
            )
        description = str(raw.get("description") or "")
        if not description.strip():
            raise RsiBadRequest("upsert_inline_skill.description is required")
        if len(description) > 1024:
            raise RsiBadRequest("upsert_inline_skill.description must be 1024 characters or fewer")
        return UpsertInlineSkill(
            name=name,
            description=description,
            instructions=str(raw.get("instructions") or ""),
        )
    if kind == "remove_inline_skill":
        name = str(raw.get("name") or "").strip()
        if not name:
            raise RsiBadRequest("remove_inline_skill.name is required")
        return RemoveInlineSkill(name=name)
    raise RsiBadRequest(f"unsupported mutation kind: {kind!r}")


def apply_mutations(profile: AgentProfile, mutations: list[Mutation] | tuple[Mutation, ...]) -> AgentProfile:
    """Return a deep-copied profile with whitelisted mutations applied."""
    current = deepcopy(profile)
    for mutation in mutations:
        current = _apply_one(current, mutation)
    return current


def _apply_one(profile: AgentProfile, mutation: Mutation) -> AgentProfile:
    if isinstance(mutation, AppendInstructions):
        base = profile.instructions or ""
        addition = mutation.text.strip("\n")
        if not addition:
            return profile
        if addition in base:
            return profile
        merged = f"{base.rstrip()}\n\n{addition}\n" if base.strip() else f"{addition}\n"
        return replace(profile, instructions=merged)

    if isinstance(mutation, UpsertInlineSkill):
        skills = deepcopy(profile.skills) if profile.skills is not None else SkillsConfig()
        inline = [skill for skill in skills.inline if skill.name != mutation.name]
        inline.append(
            SkillConfig(
                name=mutation.name,
                description=mutation.description,
                instructions=mutation.instructions,
            )
        )
        skills.inline = inline
        return replace(profile, skills=skills)

    if isinstance(mutation, RemoveInlineSkill):
        skills = deepcopy(profile.skills) if profile.skills is not None else SkillsConfig()
        skills.inline = [skill for skill in skills.inline if skill.name != mutation.name]
        return replace(profile, skills=skills)

    raise RsiBadRequest(f"unsupported mutation: {type(mutation)!r}")
