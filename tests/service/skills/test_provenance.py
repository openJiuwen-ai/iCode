# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for skill tool provenance: kinds, context builders, staged revision cache."""

from __future__ import annotations

from typing import TYPE_CHECKING

from chrys.foundation.tool_approval import declares_requires_approval
from chrys.foundation.tool_call_context import (
    MAX_CONTEXT_VALUE_CHARS,
    resolve_tool_call_context,
)
from chrys.foundation.tool_kinds import KIND_SKILL, get_tool_kind
from chrys.service.skills.model import Skill, SkillResource, SkillScript
from chrys.service.skills.provider import ChrysSkillsProvider, _skill_revision

if TYPE_CHECKING:
    from pathlib import Path


def _inline_skill() -> Skill:
    return Skill(
        name="pdf",
        description="Render PDFs",
        content="body",
        resources=[SkillResource(name="Reference.md", content="ref")],
        scripts=[SkillScript(name="scripts/render.py", full_path="/nonexistent/scripts/render.py")],
    )


async def _provider(*skills: Skill) -> ChrysSkillsProvider:
    skill_list = list(skills)

    async def load() -> list[Skill]:
        return skill_list

    provider = ChrysSkillsProvider(load)
    await provider.initialize()
    return provider


# --------------------------------------------------------------------------- #
# Kind + builder wiring
# --------------------------------------------------------------------------- #


async def test_all_three_skill_tools_carry_kind_and_builder() -> None:
    provider = await _provider(_inline_skill())
    tools = {t.name: t for t in provider._chrys_tools}
    assert set(tools) == {"load_skill", "read_skill_resource", "run_skill_script"}
    for t in tools.values():
        assert get_tool_kind(t) == KIND_SKILL
        assert t.kind is None, "chrys kinds must stay off FunctionTool.kind"
    context = resolve_tool_call_context(tools["load_skill"], {"skill_name": "pdf"})
    assert context["skill_name"] == "pdf"
    assert context["skill_revision"] == _skill_revision(_provider_skill(provider))


async def test_only_the_script_tool_declares_the_approval_floor() -> None:
    """A skill script is an ordinary subprocess; loading or reading a skill only reads text."""
    provider = await _provider(_inline_skill())
    declared = {t.name: declares_requires_approval(t) for t in provider._chrys_tools}
    assert declared == {"load_skill": False, "read_skill_resource": False, "run_skill_script": True}


def _provider_skill(provider: ChrysSkillsProvider) -> Skill:
    return provider._skills[0]


# --------------------------------------------------------------------------- #
# Canonical resolution + per-stage misses
# --------------------------------------------------------------------------- #


async def test_load_skill_context_persists_canonical_name_not_requested_casing() -> None:
    provider = await _provider(_inline_skill())
    context = provider._load_skill_call_context({"skill_name": "PDF"})
    assert context == {"skill_name": "pdf", "skill_revision": _skill_revision(_provider_skill(provider))}


async def test_skill_not_found_persists_requested_name_without_revision() -> None:
    provider = await _provider(_inline_skill())
    context = provider._load_skill_call_context({"skill_name": "Ghost"})
    assert context == {"skill_name": "Ghost"}


async def test_resource_context_resolves_canonical_names() -> None:
    provider = await _provider(_inline_skill())
    context = provider._read_skill_resource_call_context({"skill_name": "PDF", "resource_name": "reference.MD"})
    assert context is not None
    assert context["skill_name"] == "pdf"
    assert context["resource_name"] == "Reference.md"
    assert "skill_revision" in context


async def test_resource_not_found_keeps_resolved_skill_identity_plus_raw_miss() -> None:
    """Per-stage misses: the skill DID resolve — its identity must persist."""
    provider = await _provider(_inline_skill())
    context = provider._read_skill_resource_call_context({"skill_name": "PDF", "resource_name": "missing.md"})
    assert context is not None
    assert context["skill_name"] == "pdf"
    assert context["skill_revision"] == _skill_revision(_provider_skill(provider))
    assert context["resource_name"] == "missing.md"


async def test_script_context_resolves_canonical_names_and_misses_per_stage() -> None:
    provider = await _provider(_inline_skill())
    hit = provider._run_skill_script_call_context({"skill_name": "pdf", "script_name": "SCRIPTS/Render.PY"})
    assert hit is not None
    assert hit["script_name"] == "scripts/render.py"
    miss = provider._run_skill_script_call_context({"skill_name": "pdf", "script_name": "nope.py"})
    assert miss is not None
    assert miss["skill_name"] == "pdf"
    assert miss["script_name"] == "nope.py"
    both_miss = provider._run_skill_script_call_context({"skill_name": "Ghost", "script_name": "nope.py"})
    assert both_miss == {"skill_name": "Ghost", "script_name": "nope.py"}


async def test_nested_script_provenance_uses_parent_identity_and_relative_path() -> None:
    parent = Skill(
        name="parent",
        description="Parent boundary",
        content="body",
        scripts=[SkillScript(name="child/run.py", full_path="/nonexistent/child/run.py")],
    )
    provider = await _provider(parent)
    tool = next(tool for tool in provider._chrys_tools if tool.name == "run_skill_script")

    resolved = resolve_tool_call_context(tool, {"skill_name": "parent", "script_name": "child/run.py"})
    former_child_identity = resolve_tool_call_context(tool, {"skill_name": "child", "script_name": "run.py"})

    assert resolved["skill_name"] == "parent"
    assert resolved["script_name"] == "child/run.py"
    assert resolved["skill_revision"] == _skill_revision(_provider_skill(provider))
    assert former_child_identity == {"skill_name": "child", "script_name": "run.py"}


async def test_empty_args_produce_no_context() -> None:
    provider = await _provider(_inline_skill())
    assert provider._load_skill_call_context({}) is None
    assert provider._load_skill_call_context({"skill_name": "   "}) is None


async def test_over_cap_requested_name_on_miss_path_is_dropped_not_truncated() -> None:
    """The one model-authored value that reaches the context is exact-or-absent."""
    provider = await _provider(_inline_skill())
    tools = {t.name: t for t in provider._chrys_tools}
    oversized = "x" * (MAX_CONTEXT_VALUE_CHARS + 1)
    context = resolve_tool_call_context(tools["load_skill"], {"skill_name": oversized})
    assert context == {}, "an over-cap requested name must vanish, never be mangled"


# --------------------------------------------------------------------------- #
# Staged revision cache (§3.6 revision capture timing)
# --------------------------------------------------------------------------- #


def _write_file_skill(tmp_path: Path) -> Skill:
    skill_dir = tmp_path / "pdf"
    scripts_dir = skill_dir / "scripts"
    scripts_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("---\nname: pdf\n---\nbody\n", encoding="utf-8")
    script = scripts_dir / "render.py"
    script.write_text("print('v1')\n", encoding="utf-8")
    return Skill(
        name="pdf",
        description="Render PDFs",
        content="body",
        path=str(skill_dir),
        scripts=[SkillScript(name="scripts/render.py", full_path=str(script))],
    )


async def test_self_mutating_script_persists_staged_revision_not_post_run_hash(tmp_path: Path) -> None:
    provider = await _provider(_write_file_skill(tmp_path))
    skill = _provider_skill(provider)
    staged_revision = skill.revision
    assert staged_revision, "revision must be warmed eagerly at staging"

    # Simulate a run_skill_script that mutates its own skill files, then the
    # provenance builder firing in the middleware `finally`.
    script_path = tmp_path / "pdf" / "scripts" / "render.py"
    script_path.write_text("print('v2 — self-modified')\n", encoding="utf-8")
    context = provider._run_skill_script_call_context({"skill_name": "pdf", "script_name": "scripts/render.py"})

    assert context is not None
    assert context["skill_revision"] == staged_revision, "builder must read the staged cache, never recompute"


async def test_restaging_after_mutation_yields_a_fresh_revision(tmp_path: Path) -> None:
    skill_v1 = _write_file_skill(tmp_path)

    loads = {"count": 0}

    async def load() -> list[Skill]:
        loads["count"] += 1
        # Real discovery constructs fresh Skill objects per refresh.
        return [
            Skill(
                name=skill_v1.name,
                description=skill_v1.description,
                content=skill_v1.content,
                path=skill_v1.path,
                scripts=[SkillScript(name=s.name, full_path=s.full_path) for s in skill_v1.scripts],
            )
        ]

    provider = ChrysSkillsProvider(load)
    await provider.initialize()
    first_revision = _provider_skill(provider).revision

    (tmp_path / "pdf" / "scripts" / "render.py").write_text("print('v2, longer content')\n", encoding="utf-8")
    staged = await provider.stage_context_refresh()
    provider.commit_context_refresh(staged)
    second_revision = _provider_skill(provider).revision

    assert second_revision
    assert second_revision != first_revision


async def test_catalog_reminder_and_builder_agree_on_the_staged_revision(tmp_path: Path) -> None:
    provider = await _provider(_write_file_skill(tmp_path))
    context = provider._load_skill_call_context({"skill_name": "pdf"})
    assert context is not None
    assert context["skill_revision"] in provider.render_catalog_reminder()
