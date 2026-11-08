# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for skills adapter — unit and integration tests.

* :func:`create_skills_provider` is async (it warms the provider's discovery
  before returning so synchronous callers can read ``skill_names`` /
  ``skill_sources`` / ``skill_details`` right away).
* The provider's cached skills live in ``provider._skills`` (a list of chrys
  :class:`Skill`).  Tests read through the public ``skill_names`` /
  ``skill_details`` / ``skill_sources`` accessors where possible.
"""

from __future__ import annotations

import os
from html import escape as xml_escape
from pathlib import Path
from unittest.mock import create_autospec

import pytest

from chrys.foundation.text.tokenizer import MixedLanguageTokenizer
from chrys.service.profiles.agents.loader import load_profile_from_yaml
from chrys.service.profiles.agents.schema import SkillConfig, SkillResourceConfig, SkillsConfig, SkillScriptConfig
from chrys.service.skills import provider
from chrys.service.skills.adapter import (
    SKILLS_STATIC_PROMPT,
    _build_inline_skill,
    create_skills_provider,
)
from chrys.service.skills.constants import RUN_SKILL_SCRIPT_TOOL_NAME
from chrys.service.skills.model import Skill, SkillResource
from chrys.service.skills.provider import (
    ChrysSkillsProvider,
    _create_static_instructions,
    _render_catalog_block,
)


def test_build_inline_skill_basic() -> None:
    config = SkillConfig(
        name="test-skill",
        description="A test skill",
        instructions="Do the thing.",
    )
    skill, warnings = _build_inline_skill(config)
    assert warnings == []
    assert skill.name == "test-skill"
    assert skill.description == "A test skill"
    assert skill.path is None
    assert "<instructions>\nDo the thing.\n</instructions>" in skill.content


def test_build_inline_skill_with_resources() -> None:
    config = SkillConfig(
        name="with-resources",
        description="Has resources",
        instructions="Instructions here.",
        resources=[
            SkillResourceConfig(name="guide", content="Some content"),
            SkillResourceConfig(name="empty", content=""),  # should be skipped (no content)
        ],
    )
    skill, warnings = _build_inline_skill(config)
    assert warnings == []
    assert len(skill.resources) == 1
    assert skill.resources[0].name == "guide"
    assert skill.resources[0].content == "Some content"
    assert '<resource name="guide"/>' in skill.content
    assert "<scripts />" in skill.content


def test_build_inline_skill_with_scripts_emits_warning(caplog: pytest.LogCaptureFixture) -> None:
    """There is no inline file-script representation; declaring scripts logs a warning and drops them."""
    import logging

    config = SkillConfig(
        name="with-scripts",
        description="Has scripts",
        instructions="Run things.",
        scripts=[
            SkillScriptConfig(name="lint", path="./lint.py"),
        ],
    )
    with caplog.at_level(logging.WARNING, logger="chrys.service.skills.adapter"):
        skill, warnings = _build_inline_skill(config)
    assert skill.scripts == []
    assert len(warnings) == 1
    assert "inline file-backed scripts are not supported" in warnings[0].message
    assert any("inline file-backed scripts are not supported" in rec.message for rec in caplog.records)


async def test_create_provider_returns_warnings_for_unsupported_inline_file_features() -> None:
    config = SkillsConfig(
        inline=[
            SkillConfig(
                name="with-file-features",
                description="Has unsupported file-backed inline features",
                instructions="Use files.",
                resources=[SkillResourceConfig(name="guide", path="guide.md")],
                scripts=[SkillScriptConfig(name="run", path="run.py")],
            )
        ],
    )

    provider, warnings = await create_skills_provider(config)

    assert provider is not None
    messages = [w.message for w in warnings]
    assert any("file-based inline resources are not supported" in msg for msg in messages)
    assert any("inline file-backed scripts are not supported" in msg for msg in messages)


@pytest.mark.parametrize(
    "skill",
    [
        SkillConfig(name="legacy", description="", instructions="Body."),
        SkillConfig(name="legacy_name", description="Has underscore", instructions="Body."),
    ],
    ids=["empty-description", "invalid-name"],
)
async def test_create_provider_warns_and_skips_invalid_inline_skill(
    skill: SkillConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _isolate_auto_skill_dirs(tmp_path, monkeypatch)
    provider, warnings = await create_skills_provider(
        SkillsConfig(
            inline=[skill],
            auto_load_user_agents_skills=False,
            auto_load_cwd_agents_skills=False,
        )
    )

    assert provider is not None
    assert provider.skill_names() == []
    assert any(warning.message.startswith(f"Skipping invalid inline skill '{skill.name}':") for warning in warnings)


async def test_skill_details_include_descriptions_for_inline_and_file_skills(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _isolate_auto_skill_dirs(tmp_path, monkeypatch)
    skill_dir = tmp_path / "review"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        """\
---
name: review
description: Review code and identify issues
---

Review the code carefully.
""",
        encoding="utf-8",
    )
    provider, warnings = await create_skills_provider(
        SkillsConfig(
            paths=[str(tmp_path)],
            inline=[
                SkillConfig(
                    name="inline-review",
                    description="Inline review skill",
                    instructions="Review inline.",
                )
            ],
            auto_load_user_agents_skills=False,
            auto_load_cwd_agents_skills=False,
        )
    )

    assert provider is not None
    assert warnings == []
    details = {detail.name: detail for detail in provider.skill_details()}
    assert details["review"].description == "Review code and identify issues"
    assert details["review"].source == str(skill_dir)
    assert details["inline-review"].description == "Inline review skill"
    assert details["inline-review"].source == "Inline profile skills"


@pytest.mark.parametrize(
    ("skill_name", "block_style", "description_body", "expected_description"),
    [
        (
            "folded-description",
            ">",
            "Folded descriptions can span\nmultiple source lines.",
            # YAML 1.2 clip chomping keeps the final line break for folded
            # scalars too; parse_frontmatter must preserve it in the description.
            "Folded descriptions can span multiple source lines.\n",
        ),
        (
            "literal-description",
            "|",
            "Literal descriptions keep\nline breaks.",
            "Literal descriptions keep\nline breaks.\n",
        ),
    ],
    ids=["folded-block-scalar", "literal-block-scalar"],
)
async def test_file_skill_description_accepts_yaml_block_scalars(
    skill_name: str,
    block_style: str,
    description_body: str,
    expected_description: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _isolate_auto_skill_dirs(tmp_path, monkeypatch)
    skill_dir = tmp_path / skill_name
    skill_dir.mkdir()
    indented_description = "\n".join(f"  {line}" for line in description_body.splitlines())
    (skill_dir / "SKILL.md").write_text(
        f"""\
---
name: {skill_name}
description: {block_style}
{indented_description}
---

Use the skill.
""",
        encoding="utf-8",
    )

    provider, warnings = await create_skills_provider(
        SkillsConfig(
            paths=[str(tmp_path)],
            auto_load_user_agents_skills=False,
            auto_load_cwd_agents_skills=False,
        )
    )

    assert provider is not None
    assert warnings == []
    details = {detail.name: detail for detail in provider.skill_details()}
    assert details[skill_name].description == expected_description


async def test_create_provider_returns_empty_provider_when_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Point both auto-loaded skill dirs (~/.chrys/skills and ~/.agents/skills)
    # at empty temp dirs so the real ones on the developer's machine don't leak in.
    fake_platform = type("P", (), {"config_dir": tmp_path})()
    monkeypatch.setattr("chrys.foundation.platform.get_platform", lambda: fake_platform)
    monkeypatch.setattr("chrys.service.skills.adapter.user_agents_dir", lambda: tmp_path / ".agents")
    config = SkillsConfig()
    provider, warnings = await create_skills_provider(config)
    assert provider is not None
    assert provider.skill_names() == []
    catalog = provider.render_catalog_reminder()
    assert catalog.startswith("<available_skills>")
    assert "No runtime skills are available for the current agent and workspace." in catalog
    assert [tool.name for tool in provider._chrys_tools] == [
        "load_skill",
        "read_skill_resource",
        "run_skill_script",
    ]
    assert provider.tool_names == [
        "load_skill",
        "read_skill_resource",
        RUN_SKILL_SCRIPT_TOOL_NAME,
    ]
    assert warnings == []


def test_collect_skill_paths_auto_loads_user_agents_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """With the toggle on (default), non-empty ~/.agents/skills is auto-included."""
    from chrys.service.skills.adapter import _collect_skill_paths

    fake_chrys_dir = tmp_path / "chrys"
    fake_agents_root = tmp_path / "agents_root"
    fake_agents_skills = fake_agents_root / "skills"
    fake_agents_skills.mkdir(parents=True)
    (fake_agents_skills / "demo").mkdir()  # non-empty sentinel

    fake_platform = type("P", (), {"config_dir": fake_chrys_dir})()
    monkeypatch.setattr("chrys.foundation.platform.get_platform", lambda: fake_platform)
    monkeypatch.setattr("chrys.service.skills.adapter.user_agents_dir", lambda: fake_agents_root)

    paths = _collect_skill_paths(SkillsConfig())  # default: auto_load_user_agents_skills=True
    assert str(fake_agents_skills) in paths


def test_collect_skill_paths_skips_user_agents_dir_when_toggle_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the toggle off, ~/.agents/skills is NOT auto-included even when present."""
    from chrys.service.skills.adapter import _collect_skill_paths

    fake_chrys_dir = tmp_path / "chrys"
    fake_agents_root = tmp_path / "agents_root"
    fake_agents_skills = fake_agents_root / "skills"
    fake_agents_skills.mkdir(parents=True)
    (fake_agents_skills / "demo").mkdir()

    fake_platform = type("P", (), {"config_dir": fake_chrys_dir})()
    monkeypatch.setattr("chrys.foundation.platform.get_platform", lambda: fake_platform)
    monkeypatch.setattr("chrys.service.skills.adapter.user_agents_dir", lambda: fake_agents_root)

    paths = _collect_skill_paths(SkillsConfig(auto_load_user_agents_skills=False))
    assert str(fake_agents_skills) not in paths


def _make_runtime(cwd: Path) -> object:
    """Build a minimal ``SessionEnvironment`` for the cwd-skills tests.

    Only ``runtime.cwd`` is read by ``_collect_skill_paths``; we capture a real
    one to avoid coupling these tests to platform/shell detection.
    """
    from chrys.foundation.models.session_env import SessionEnvironment
    from chrys.foundation.models.workspace import Workspace

    return SessionEnvironment.capture(workspace=Workspace.from_cwd(str(cwd)))


def _isolate_auto_skill_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point auto-loaded skill roots at empty temp locations."""
    fake_chrys_dir = tmp_path / "chrys-config"
    fake_agents_root = tmp_path / "agents-root"
    fake_platform = type("P", (), {"config_dir": fake_chrys_dir})()
    monkeypatch.setattr("chrys.foundation.platform.get_platform", lambda: fake_platform)
    monkeypatch.setattr("chrys.service.skills.adapter.user_agents_dir", lambda: fake_agents_root)


def test_collect_skill_paths_auto_loads_cwd_agents_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """With project skills enabled and the toggle on (default), non-empty <cwd>/.agents/skills is auto-included."""
    from chrys.service.skills.adapter import _collect_skill_paths

    # Isolate the user-level dirs so only the cwd path can show up.
    fake_chrys_dir = tmp_path / "chrys"  # does not exist
    fake_agents_root = tmp_path / "agents_root"  # does not exist
    fake_platform = type("P", (), {"config_dir": fake_chrys_dir})()
    monkeypatch.setattr("chrys.foundation.platform.get_platform", lambda: fake_platform)
    monkeypatch.setattr("chrys.service.skills.adapter.user_agents_dir", lambda: fake_agents_root)

    cwd = tmp_path / "project"
    cwd_skills = cwd / ".agents" / "skills"
    cwd_skills.mkdir(parents=True)
    (cwd_skills / "demo").mkdir()  # non-empty sentinel

    paths = _collect_skill_paths(SkillsConfig(), runtime=_make_runtime(cwd), project_skills_enabled=True)
    assert str(cwd_skills) in paths


def test_collect_skill_paths_skips_cwd_dir_until_project_skills_are_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The user's ``project.skills_enabled`` setting gates ``<cwd>/.agents/skills`` before the profile toggle."""
    from chrys.service.skills.adapter import _collect_skill_paths

    monkeypatch.setattr("chrys.service.skills.adapter.user_agents_dir", lambda: tmp_path / "no_user")
    cwd = tmp_path / "project"
    cwd_skills = cwd / ".agents" / "skills"
    (cwd_skills / "demo").mkdir(parents=True)

    assert str(cwd_skills) not in _collect_skill_paths(SkillsConfig(), runtime=_make_runtime(cwd))


def test_collect_skill_paths_skips_cwd_dir_when_toggle_off(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Toggle off → ``<cwd>/.agents/skills`` is not auto-included even when present."""
    from chrys.service.skills.adapter import _collect_skill_paths

    fake_chrys_dir = tmp_path / "chrys"
    fake_agents_root = tmp_path / "agents_root"
    fake_platform = type("P", (), {"config_dir": fake_chrys_dir})()
    monkeypatch.setattr("chrys.foundation.platform.get_platform", lambda: fake_platform)
    monkeypatch.setattr("chrys.service.skills.adapter.user_agents_dir", lambda: fake_agents_root)

    cwd = tmp_path / "project"
    cwd_skills = cwd / ".agents" / "skills"
    cwd_skills.mkdir(parents=True)
    (cwd_skills / "demo").mkdir()

    paths = _collect_skill_paths(
        SkillsConfig(auto_load_cwd_agents_skills=False),
        runtime=_make_runtime(cwd),
        project_skills_enabled=True,
    )
    assert str(cwd_skills) not in paths


def test_collect_skill_paths_includes_cwd_when_dir_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Toggle on and missing ``<cwd>/.agents/skills`` is still tracked for later turns."""
    from chrys.service.skills.adapter import _collect_skill_paths

    fake_chrys_dir = tmp_path / "chrys"
    fake_agents_root = tmp_path / "agents_root"
    fake_platform = type("P", (), {"config_dir": fake_chrys_dir})()
    monkeypatch.setattr("chrys.foundation.platform.get_platform", lambda: fake_platform)
    monkeypatch.setattr("chrys.service.skills.adapter.user_agents_dir", lambda: fake_agents_root)

    cwd = tmp_path / "empty_project"
    cwd.mkdir()

    paths = _collect_skill_paths(SkillsConfig(), runtime=_make_runtime(cwd), project_skills_enabled=True)
    assert str((cwd / ".agents" / "skills").resolve()) in paths


def test_collect_skill_paths_includes_cwd_when_dir_empty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Empty ``<cwd>/.agents/skills`` is tracked so later-added skills are discovered."""
    from chrys.service.skills.adapter import _collect_skill_paths

    fake_chrys_dir = tmp_path / "chrys"
    fake_agents_root = tmp_path / "agents_root"
    fake_platform = type("P", (), {"config_dir": fake_chrys_dir})()
    monkeypatch.setattr("chrys.foundation.platform.get_platform", lambda: fake_platform)
    monkeypatch.setattr("chrys.service.skills.adapter.user_agents_dir", lambda: fake_agents_root)

    cwd = tmp_path / "project"
    (cwd / ".agents" / "skills").mkdir(parents=True)  # exists but empty

    paths = _collect_skill_paths(SkillsConfig(), runtime=_make_runtime(cwd), project_skills_enabled=True)
    assert str((cwd / ".agents" / "skills").resolve()) in paths


def test_collect_skill_paths_no_runtime_skips_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No runtime supplied → cwd branch is never taken, even with a populated dir on disk."""
    from chrys.service.skills.adapter import _collect_skill_paths

    # Empty fakes for the two user-level branches.
    fake_chrys_dir = tmp_path / "chrys"
    fake_agents_root = tmp_path / "agents_root"
    fake_platform = type("P", (), {"config_dir": fake_chrys_dir})()
    monkeypatch.setattr("chrys.foundation.platform.get_platform", lambda: fake_platform)
    monkeypatch.setattr("chrys.service.skills.adapter.user_agents_dir", lambda: fake_agents_root)

    # Populated cwd-skills + chdir into the project so os.getcwd() would yield it.
    cwd = tmp_path / "project"
    cwd_skills = cwd / ".agents" / "skills"
    cwd_skills.mkdir(parents=True)
    (cwd_skills / "demo").mkdir()
    monkeypatch.chdir(cwd)

    paths = _collect_skill_paths(SkillsConfig(), runtime=None, project_skills_enabled=True)
    assert str(cwd_skills) not in paths


def test_collect_skill_paths_resolves_relative_profile_paths_against_runtime_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Explicit relative ``skills.paths`` are workspace-relative when runtime is bound."""
    from chrys.service.skills.adapter import _collect_skill_paths

    fake_chrys_dir = tmp_path / "chrys"
    fake_agents_root = tmp_path / "agents_root"
    fake_platform = type("P", (), {"config_dir": fake_chrys_dir})()
    monkeypatch.setattr("chrys.foundation.platform.get_platform", lambda: fake_platform)
    monkeypatch.setattr("chrys.service.skills.adapter.user_agents_dir", lambda: fake_agents_root)

    cwd = tmp_path / "project"
    cwd.mkdir()

    paths = _collect_skill_paths(
        SkillsConfig(
            paths=["./skills"],
            auto_load_user_agents_skills=False,
            auto_load_cwd_agents_skills=False,
        ),
        runtime=_make_runtime(cwd),
    )

    assert paths[0] == str((cwd / "skills").resolve())


def test_collect_skill_paths_dedupes_equivalent_relative_config_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from chrys.service.skills.adapter import _collect_skill_paths

    _isolate_auto_skill_dirs(tmp_path, monkeypatch)
    cwd = tmp_path / "project"
    cwd.mkdir()

    paths = _collect_skill_paths(
        SkillsConfig(
            paths=["skills", "./skills"],
            auto_load_user_agents_skills=False,
            auto_load_cwd_agents_skills=False,
        ),
        runtime=_make_runtime(cwd),
    )

    assert paths.count(str((cwd / "skills").resolve())) == 1


def test_collect_skill_paths_dedupes_relative_and_absolute_config_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from chrys.service.skills.adapter import _collect_skill_paths

    _isolate_auto_skill_dirs(tmp_path, monkeypatch)
    cwd = tmp_path / "project"
    cwd.mkdir()

    paths = _collect_skill_paths(
        SkillsConfig(
            paths=["skills", str(cwd / "skills")],
            auto_load_user_agents_skills=False,
            auto_load_cwd_agents_skills=False,
        ),
        runtime=_make_runtime(cwd),
    )

    assert paths.count(str((cwd / "skills").resolve())) == 1


@pytest.mark.skipif(os.name == "nt", reason="POSIX-specific foreign Windows absolute path behavior")
def test_collect_skill_paths_windows_absolute_on_posix_is_not_joined_to_runtime_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from chrys.service.skills.adapter import _collect_skill_paths

    _isolate_auto_skill_dirs(tmp_path, monkeypatch)
    cwd = tmp_path / "project"
    cwd.mkdir()

    paths = _collect_skill_paths(
        SkillsConfig(
            paths=[r"C:\foo"],
            auto_load_user_agents_skills=False,
            auto_load_cwd_agents_skills=False,
        ),
        runtime=_make_runtime(cwd),
    )

    assert paths[0] == r"C:\foo"
    assert paths.count(r"C:\foo") == 1


def test_collect_skill_paths_dedups_when_cwd_equals_user_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """If ``<cwd>/.agents/skills`` is also listed explicitly in ``config.paths``, no duplicates."""
    from chrys.service.skills.adapter import _collect_skill_paths

    fake_chrys_dir = tmp_path / "chrys"  # does not exist
    fake_agents_root = tmp_path / "agents_root"  # does not exist
    fake_platform = type("P", (), {"config_dir": fake_chrys_dir})()
    monkeypatch.setattr("chrys.foundation.platform.get_platform", lambda: fake_platform)
    monkeypatch.setattr("chrys.service.skills.adapter.user_agents_dir", lambda: fake_agents_root)

    cwd = tmp_path / "project"
    cwd_skills = cwd / ".agents" / "skills"
    cwd_skills.mkdir(parents=True)
    (cwd_skills / "demo").mkdir()

    cfg = SkillsConfig(paths=[str(cwd_skills)])
    paths = _collect_skill_paths(cfg, runtime=_make_runtime(cwd))
    assert paths.count(str(cwd_skills)) == 1, f"expected exactly one entry, got: {paths}"
    cfg_no_auto = SkillsConfig(paths=[str(cwd_skills)], auto_load_cwd_agents_skills=False)
    paths_no_auto = _collect_skill_paths(cfg_no_auto, runtime=_make_runtime(cwd))
    assert paths_no_auto.count(str(cwd_skills)) == 1


def test_collect_skill_paths_dedups_user_and_cwd_branches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """If ``user_agents_dir()/skills`` and ``<cwd>/.agents/skills`` produce the same string, no duplicates."""
    from chrys.service.skills.adapter import _collect_skill_paths

    fake_chrys_dir = tmp_path / "chrys"
    fake_platform = type("P", (), {"config_dir": fake_chrys_dir})()
    monkeypatch.setattr("chrys.foundation.platform.get_platform", lambda: fake_platform)

    cwd = tmp_path / "project"
    cwd_agents = cwd / ".agents"
    cwd_skills = cwd_agents / "skills"
    cwd_skills.mkdir(parents=True)
    (cwd_skills / "demo").mkdir()

    monkeypatch.setattr("chrys.service.skills.adapter.user_agents_dir", lambda: cwd_agents)

    paths = _collect_skill_paths(SkillsConfig(), runtime=_make_runtime(cwd))
    assert paths.count(str(cwd_skills)) == 1, f"expected exactly one entry, got: {paths}"


async def test_create_provider_with_inline_skills() -> None:
    config = SkillsConfig(
        inline=[
            SkillConfig(name="my-skill", description="desc", instructions="Do stuff"),
        ],
    )
    provider, warnings = await create_skills_provider(config)
    assert provider is not None
    assert warnings == []


async def test_create_provider_with_paths(tmp_path: Path) -> None:
    config = SkillsConfig(paths=[str(tmp_path)])
    provider, _warnings = await create_skills_provider(config)
    assert provider is not None


async def test_create_provider_accepts_a_profile_that_leaves_skills_fields_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A commented-out skills value parses as unset instead of reaching the provider as None."""
    _isolate_auto_skill_dirs(tmp_path, monkeypatch)
    profile_path = tmp_path / "null-skills.yaml"
    profile_path.write_text(
        "name: nully-skills\nskills:\n  paths:\n  inline:\n  script_timeout:\n"
        "  script_extensions:\n  auto_load_user_agents_skills:\n  auto_load_cwd_agents_skills:\n",
        encoding="utf-8",
    )
    profile = load_profile_from_yaml(profile_path)
    provider, warnings = await create_skills_provider(profile.skills)
    assert provider is not None
    assert warnings == []


async def test_file_skill_catalog_includes_absolute_skill_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _isolate_auto_skill_dirs(tmp_path, monkeypatch)
    root = tmp_path / "skills & docs"
    skill_dir = root / "path-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        """\
---
name: path-skill
description: Uses package-local files
---

Use files in this skill directory.
""",
        encoding="utf-8",
    )

    provider, warnings = await create_skills_provider(
        SkillsConfig(
            paths=[str(root)],
            auto_load_user_agents_skills=False,
            auto_load_cwd_agents_skills=False,
        )
    )

    assert provider is not None
    assert warnings == []
    instructions = provider._static_instructions
    assert instructions is not None
    assert "<available_skills>" not in instructions
    catalog = provider.render_catalog_reminder()
    assert catalog is not None
    assert f"<skill_dir>{xml_escape(str(skill_dir))}</skill_dir>" in catalog


async def test_inline_skill_catalog_omits_skill_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _isolate_auto_skill_dirs(tmp_path, monkeypatch)

    provider, warnings = await create_skills_provider(
        SkillsConfig(
            inline=[SkillConfig(name="inline-skill", description="From YAML", instructions="Inline instructions.")],
            auto_load_user_agents_skills=False,
            auto_load_cwd_agents_skills=False,
        )
    )

    assert provider is not None
    assert warnings == []
    instructions = provider._static_instructions
    assert instructions is not None
    assert "<available_skills>" not in instructions
    catalog = provider.render_catalog_reminder()
    assert catalog is not None
    assert "<skill_dir>" not in catalog


def _make_file_skill(name: str, description: str, path: str) -> Skill:
    """Build a bare file-backed :class:`Skill` for prompt-rendering unit tests."""
    return Skill(name=name, description=description, content="body", path=path)


async def test_file_skill_load_emits_empty_resource_and_script_blocks(tmp_path: Path) -> None:
    skill_dir = tmp_path / "empty-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("body", encoding="utf-8")
    skill = _make_file_skill("empty-skill", "Empty skill", str(skill_dir))

    async def load_skills() -> list[Skill]:
        return [skill]

    provider = ChrysSkillsProvider(load_skills)

    loaded = await provider._load_skill([skill], "empty-skill")

    assert "<resources />" in loaded
    assert "<scripts />" in loaded
    assert "<resources>\n" not in loaded
    assert "<scripts>\n" not in loaded


def _extract_revision(text: str) -> str:
    """Return the first revision from text; intended for single-skill fixtures."""
    return text.split("<revision>", 1)[1].split("</revision>", 1)[0]


def test_blank_skill_path_catalog_omits_skill_dir() -> None:
    skill = _make_file_skill("blank-path", "Blank path skill", "  ")

    instructions = _create_static_instructions(SKILLS_STATIC_PROMPT)
    catalog = _render_catalog_block([skill])

    assert instructions is not None
    assert "<available_skills>" not in instructions
    assert catalog is not None
    assert "<skill_dir>" not in catalog


@pytest.mark.parametrize(
    "win_dir",
    [
        r"C:\Users\agent\Skills\path-skill",
        r"\\server\share\Skills\path-skill",
    ],
)
def test_skill_catalog_preserves_windows_absolute_skill_dir(win_dir: str, monkeypatch: pytest.MonkeyPatch) -> None:
    skill = _make_file_skill("path-skill", "Windows path skill", win_dir)
    # The revision hash stats <skill_dir>/SKILL.md; a fabricated drive or UNC
    # share must not be touched on a host where it could resolve. A missing
    # file signs as its own path, so the fake answers the same way.
    signature = create_autospec(provider._file_signature, side_effect=lambda path: path)
    monkeypatch.setattr(provider, "_file_signature", signature)

    catalog = _render_catalog_block([skill])

    assert catalog is not None
    assert catalog.startswith("<available_skills>")
    assert f"<skill_dir>{xml_escape(win_dir)}</skill_dir>" in catalog
    signature.assert_called_once_with(str(Path(win_dir) / "SKILL.md"))


def test_skill_catalog_normalizes_relative_skill_dir_to_absolute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    relative_dir = Path("skills") / "path-skill"
    skill = _make_file_skill("path-skill", "Relative path skill", str(relative_dir))

    catalog = _render_catalog_block([skill])

    assert catalog is not None
    assert f"<skill_dir>{xml_escape(str((tmp_path / relative_dir).resolve()))}</skill_dir>" in catalog


def test_skill_prompt_mentions_scripts_directory_script_name_format() -> None:
    instructions = _create_static_instructions(SKILLS_STATIC_PROMPT)

    assert instructions is not None
    assert "<available_skills>" not in instructions
    script_guidance = instructions.split("- `script_name`", 1)[1].split("## Constraints", 1)[0]
    assert "scripts/{skill_script_name}.py" in script_guidance
    assert '"{skill_script_name}.py"' in script_guidance
    assert "not" in script_guidance


async def test_create_provider_warns_for_missing_configured_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate_auto_skill_dirs(tmp_path, monkeypatch)
    missing = tmp_path / "missing-skills"

    provider, warnings = await create_skills_provider(
        SkillsConfig(
            paths=[str(missing)],
            auto_load_user_agents_skills=False,
            auto_load_cwd_agents_skills=False,
        )
    )

    assert provider is not None
    assert len(warnings) == 1
    assert warnings[0].code == "skill_path_missing"
    assert str(missing) in warnings[0].message


async def test_create_provider_does_not_warn_for_missing_relative_configured_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate_auto_skill_dirs(tmp_path, monkeypatch)
    cwd = tmp_path / "project"
    cwd.mkdir()

    provider, warnings = await create_skills_provider(
        SkillsConfig(
            paths=["./skills"],
            auto_load_user_agents_skills=False,
            auto_load_cwd_agents_skills=False,
        ),
        runtime=_make_runtime(cwd),
    )

    assert provider is not None
    assert warnings == []


@pytest.mark.integration
async def test_create_provider_loads_file_skill(tmp_path: Path) -> None:
    """Create a real SKILL.md file and verify ChrysSkillsProvider discovers it."""
    skill_dir = tmp_path / "greet"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        """\
---
name: greet
description: Greet the user
---

Say hello to the user in a friendly way.
""",
        encoding="utf-8",
    )

    config = SkillsConfig(paths=[str(tmp_path)])
    provider, warnings = await create_skills_provider(config)
    assert provider is not None
    assert warnings == []

    # Provider's cached context now holds the discovered skills.
    names = provider.skill_names()
    assert "greet" in names
    details = {d.name: d for d in provider.skill_details()}
    assert details["greet"].description == "Greet the user"


@pytest.mark.integration
async def test_create_provider_warns_for_nested_invalid_file_skill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate_auto_skill_dirs(tmp_path, monkeypatch)
    skill_dir = tmp_path / "group" / "actual-name"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        """\
---
name: wrong-name
description: Invalid because name does not match directory
---

Body.
""",
        encoding="utf-8",
    )

    provider, warnings = await create_skills_provider(
        SkillsConfig(
            paths=[str(tmp_path)],
            auto_load_user_agents_skills=False,
            auto_load_cwd_agents_skills=False,
        )
    )

    assert provider is not None
    assert provider.skill_names() == []
    assert any("actual-name" in warning.message for warning in warnings)


@pytest.mark.integration
async def test_create_provider_does_not_warn_for_invalid_skill_below_parent_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate_auto_skill_dirs(tmp_path, monkeypatch)
    parent = tmp_path / "parent"
    child = parent / "child"
    child.mkdir(parents=True)
    (parent / "SKILL.md").write_text(
        "---\nname: parent\ndescription: Parent boundary\n---\n",
        encoding="utf-8",
    )
    (child / "SKILL.md").write_text(
        "---\nname: wrong-name\ndescription: No longer independently loaded\n---\n",
        encoding="utf-8",
    )
    (child / "guide.md").write_text("belongs to parent", encoding="utf-8")

    provider, warnings = await create_skills_provider(
        SkillsConfig(
            paths=[str(tmp_path)],
            auto_load_user_agents_skills=False,
            auto_load_cwd_agents_skills=False,
        )
    )

    assert provider is not None
    assert provider.skill_names() == ["parent"]
    assert [resource.name for resource in provider._skills[0].resources] == ["child/guide.md"]
    assert warnings == []


@pytest.mark.integration
async def test_file_skill_load_lists_exact_script_relative_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate_auto_skill_dirs(tmp_path, monkeypatch)
    skill_dir = tmp_path / "scripted"
    scripts_dir = skill_dir / "scripts"
    scripts_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        """\
---
name: scripted
description: Scripted skill
---

Use the convert script.
""",
        encoding="utf-8",
    )
    (scripts_dir / "convert.py").write_text(
        """\
print("converted")
""",
        encoding="utf-8",
    )

    provider, warnings = await create_skills_provider(
        SkillsConfig(
            paths=[str(tmp_path)],
            auto_load_user_agents_skills=False,
            auto_load_cwd_agents_skills=False,
        )
    )

    assert provider is not None
    assert warnings == []
    skills = provider._skills
    loaded = await provider._load_skill(skills, "scripted")
    catalog = provider.render_catalog_reminder()
    assert catalog is not None
    assert f"<revision>{_extract_revision(catalog)}</revision>" in loaded
    assert "<skill_dir>" in loaded
    assert "<resources />" in loaded
    assert '<script name="scripts/convert.py"/>' in loaded
    assert '<script name="convert.py"/>' not in loaded
    assert loaded.count("<scripts>") == 1
    assert "parameters_schema" not in loaded

    result = await provider._run_skill_script_chrys(skills, "scripted", "convert.py")
    assert "Did you mean 'scripts/convert.py'?" in result


async def test_inline_skill_load_includes_revision(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _isolate_auto_skill_dirs(tmp_path, monkeypatch)
    provider, warnings = await create_skills_provider(
        SkillsConfig(
            inline=[
                SkillConfig(name="inline-skill", description="Inline skill", instructions="Follow inline steps."),
            ],
            auto_load_user_agents_skills=False,
            auto_load_cwd_agents_skills=False,
        )
    )

    assert provider is not None
    assert warnings == []
    skills = provider._skills
    loaded = await provider._load_skill(skills, "inline-skill")
    catalog = provider.render_catalog_reminder()
    assert catalog is not None
    assert f"<revision>{_extract_revision(catalog)}</revision>" in loaded
    assert "<resources />" in loaded
    assert "<scripts />" in loaded
    assert "<skill_dir>" not in loaded


async def test_disable_caching_before_run_updates_cached_context_for_stable_tools() -> None:
    skill = _make_file_skill("fresh", "Fresh skill", "  ")

    async def _load_skills() -> list[Skill]:
        return [skill]

    provider = ChrysSkillsProvider(_load_skills, instruction_template=SKILLS_STATIC_PROMPT, disable_caching=True)

    class Context:
        def __init__(self) -> None:
            self.instructions: list[tuple[str, str]] = []
            self.tools: list[object] = []

        def extend_instructions(self, source_id: str, instructions: str) -> None:
            self.instructions.append((source_id, instructions))

        def extend_tools(self, source_id: str, tools: list[object]) -> None:
            self.tools.extend(tools)

    context = Context()

    await provider.before_run(agent=None, session=None, context=context, state={})

    assert provider.skill_names() == ["fresh"]
    load_tool = next(tool for tool in context.tools if tool.name == "load_skill")
    assert "<revision>" in await load_tool.func("fresh")


@pytest.mark.integration
async def test_create_provider_merges_file_and_inline(tmp_path: Path) -> None:
    """File-based and inline skills should both be available."""
    skill_dir = tmp_path / "file-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        """\
---
name: file-skill
description: From file
---

File-based instructions.
""",
        encoding="utf-8",
    )

    config = SkillsConfig(
        paths=[str(tmp_path)],
        inline=[
            SkillConfig(name="inline-skill", description="From YAML", instructions="Inline instructions."),
        ],
    )
    provider, warnings = await create_skills_provider(config)
    assert provider is not None
    assert warnings == []

    names = set(provider.skill_names())
    assert "file-skill" in names
    assert "inline-skill" in names


@pytest.mark.integration
async def test_file_skill_script_runs_with_uv(tmp_path: Path) -> None:
    """End-to-end: discover a file-based skill with a .py script and execute it via uv run."""
    skill_dir = tmp_path / "calculator"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        """\
---
name: calculator
description: Simple calculator skill
---

Use the add script to add two numbers.
""",
        encoding="utf-8",
    )
    (skill_dir / "add.py").write_text(
        """\
import argparse
p = argparse.ArgumentParser()
p.add_argument("--a", type=int, required=True)
p.add_argument("--b", type=int, required=True)
args = p.parse_args()
print(args.a + args.b)
""",
        encoding="utf-8",
    )

    config = SkillsConfig(paths=[str(tmp_path)])
    provider, _warnings = await create_skills_provider(config)
    assert provider is not None
    skills = provider._skills
    calc = next((s for s in skills if s.name == "calculator"), None)
    assert calc is not None
    assert len(calc.scripts) == 1
    assert calc.scripts[0].name == "add.py"

    # Execute the script through the chrys-flavored run helper.
    result = await provider._run_skill_script_chrys(skills, "calculator", "add.py", args={"a": 17, "b": 25})
    assert "42" in result


async def test_create_provider_includes_script_runner() -> None:
    """Provider exposes the chrys runner so file-skill execution works end-to-end."""
    config = SkillsConfig(
        inline=[SkillConfig(name="s", description="d", instructions="i")],
    )
    provider, _warnings = await create_skills_provider(config)
    assert provider is not None
    assert provider._chrys_runner is not None


async def test_create_provider_uses_custom_timeout() -> None:
    config = SkillsConfig(
        inline=[SkillConfig(name="s", description="d", instructions="i")],
        script_timeout=120,
    )
    provider, _warnings = await create_skills_provider(config)
    assert provider is not None
    assert provider._chrys_runner is not None
    assert provider._chrys_runner._timeout == 120


async def test_read_skill_resource_caps_file_and_names_original_path(tmp_path: Path) -> None:
    resource_path = tmp_path / "reference.txt"
    resource_path.write_text("HEAD\n" + "x" * 10_000 + "\nTAIL")
    skill = Skill(
        name="resource-skill",
        description="d",
        content="instructions",
        resources=[SkillResource(name="reference.txt", full_path=str(resource_path))],
    )

    async def load_skills() -> list[Skill]:
        return [skill]

    provider = ChrysSkillsProvider(load_skills)
    await provider.initialize()
    result = await provider._read_skill_resource(provider._skills, skill.name, "reference.txt", max_tokens=100)

    assert "truncated" in result
    assert str(resource_path) in result
    assert "read_skill_resource again with a higher max_tokens" in result
    assert "read_file" not in result
    assert "shell tools" not in result
    assert MixedLanguageTokenizer().count_tokens(result) <= 100
    assert not (tmp_path / "tool_results").exists()


async def test_read_skill_resource_keeps_in_budget_file_when_path_notice_would_not_fit(tmp_path: Path) -> None:
    text = "中" * 132
    resource_path = tmp_path / ("long-resource-name-" * 5 + ".txt")
    resource_path.write_text(text, encoding="utf-8")
    skill = Skill(
        name="resource-skill",
        description="d",
        content="instructions",
        resources=[SkillResource(name="reference.txt", full_path=str(resource_path))],
    )

    async def load_skills() -> list[Skill]:
        return [skill]

    provider = ChrysSkillsProvider(load_skills)
    await provider.initialize()
    result = await provider._read_skill_resource(provider._skills, skill.name, "reference.txt", max_tokens=100)

    assert MixedLanguageTokenizer().count_tokens(text) <= 100
    assert result == text


async def test_read_inline_resource_caps_and_zero_clamps() -> None:
    skill = Skill(
        name="inline-resource",
        description="d",
        content="instructions",
        resources=[SkillResource(name="inline.txt", content="x" * 10_000)],
    )

    async def load_skills() -> list[Skill]:
        return [skill]

    provider = ChrysSkillsProvider(load_skills)
    await provider.initialize()
    result = await provider._read_skill_resource(provider._skills, skill.name, "inline.txt", max_tokens=0)

    assert "truncated" in result
    assert MixedLanguageTokenizer().count_tokens(result) <= 100


async def test_load_skill_has_no_producer_cap() -> None:
    skill = Skill(name="large-skill", description="d", content="x" * 40_000)

    async def load_skills() -> list[Skill]:
        return [skill]

    provider = ChrysSkillsProvider(load_skills)
    await provider.initialize()
    result = await provider._load_skill(provider._skills, skill.name)

    assert "truncated" not in result
    assert MixedLanguageTokenizer().count_tokens(result) > 5_000


@pytest.mark.integration
async def test_stage_context_refresh_discovers_skill_without_committing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Staged runtime refresh can be discarded without mutating the provider cache."""
    _isolate_auto_skill_dirs(tmp_path, monkeypatch)
    root = tmp_path / "skills"
    root.mkdir()
    config = SkillsConfig(
        paths=[str(root)],
        auto_load_user_agents_skills=False,
        auto_load_cwd_agents_skills=False,
    )

    provider, warnings = await create_skills_provider(config)
    assert provider is not None
    assert warnings == []
    assert provider.skill_names() == []

    skill_dir = root / "demo"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        """\
---
name: demo
description: Added later
---

Use the later skill.
""",
        encoding="utf-8",
    )

    staged = await provider.stage_context_refresh()

    assert staged.warnings == []
    assert staged.skill_names() == ["demo"]
    assert provider.skill_names() == []

    committed_warnings = provider.commit_context_refresh(staged)

    assert committed_warnings == []
    assert provider.skill_names() == ["demo"]


@pytest.mark.integration
async def test_stage_context_refresh_does_not_drain_invalid_skill_warning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Discarding a staged refresh must not lose pending load warnings."""
    _isolate_auto_skill_dirs(tmp_path, monkeypatch)
    root = tmp_path / "skills"
    root.mkdir()
    config = SkillsConfig(
        paths=[str(root)],
        auto_load_user_agents_skills=False,
        auto_load_cwd_agents_skills=False,
    )

    provider, warnings = await create_skills_provider(config)
    assert provider is not None
    assert warnings == []

    skill_dir = root / "demo"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        """\
---
name: different-name
description: Invalid because the name mismatches its directory
---

Invalid.
""",
        encoding="utf-8",
    )

    staged = await provider.stage_context_refresh()

    assert staged.warnings == []
    assert provider.skill_names() == []

    committed_warnings = await provider.refresh_context()

    assert provider.skill_names() == []
    assert len(committed_warnings) == 1
    assert committed_warnings[0].code == "skill_load_error"
    assert "Skill 'demo' failed to load" in committed_warnings[0].message


@pytest.mark.integration
async def test_refresh_context_discovers_skill_added_after_provider_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Runtime refresh picks up skills added after the agent was built."""
    _isolate_auto_skill_dirs(tmp_path, monkeypatch)
    root = tmp_path / "skills"
    root.mkdir()
    config = SkillsConfig(
        paths=[str(root)],
        auto_load_user_agents_skills=False,
        auto_load_cwd_agents_skills=False,
    )

    provider, warnings = await create_skills_provider(config)
    assert provider is not None
    assert warnings == []
    assert provider.skill_names() == []
    assert "No runtime skills are available for the current agent and workspace." in provider.render_catalog_reminder()
    instructions_before = provider._static_instructions

    skill_dir = root / "demo"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        """\
---
name: demo
description: Added later
---

Use the later skill.
""",
        encoding="utf-8",
    )

    refresh_warnings = await provider.refresh_context()

    assert refresh_warnings == []
    assert provider.skill_names() == ["demo"]
    assert provider._static_instructions == instructions_before
    catalog = provider.render_catalog_reminder()
    assert catalog is not None
    assert "<available_skills>" in catalog
    assert "<name>demo</name>" in catalog
    assert "<revision>" in catalog


@pytest.mark.integration
async def test_refresh_context_updates_catalog_revision_when_skill_body_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Runtime refresh changes the catalog when SKILL.md content changes."""
    _isolate_auto_skill_dirs(tmp_path, monkeypatch)
    skill_dir = tmp_path / "demo"
    skill_dir.mkdir()
    skill_file = skill_dir / "SKILL.md"
    skill_file.write_text(
        """\
---
name: demo
description: Stable description
---

Version one.
""",
        encoding="utf-8",
    )
    config = SkillsConfig(
        paths=[str(tmp_path)],
        auto_load_user_agents_skills=False,
        auto_load_cwd_agents_skills=False,
    )

    provider, warnings = await create_skills_provider(config)
    assert provider is not None
    assert warnings == []
    original_catalog = provider.render_catalog_reminder()
    assert original_catalog is not None
    instructions_before = provider._static_instructions

    skill_file.write_text(
        """\
---
name: demo
description: Stable description
---

Version two.
""",
        encoding="utf-8",
    )

    refresh_warnings = await provider.refresh_context()

    assert refresh_warnings == []
    assert provider._static_instructions == instructions_before
    refreshed_catalog = provider.render_catalog_reminder()
    assert refreshed_catalog is not None
    assert refreshed_catalog != original_catalog
    assert "<revision>" in refreshed_catalog


@pytest.mark.integration
async def test_refresh_context_reports_invalid_skill_added_after_provider_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Runtime refresh surfaces load warnings for newly-added invalid skills."""
    _isolate_auto_skill_dirs(tmp_path, monkeypatch)
    root = tmp_path / "skills"
    root.mkdir()
    config = SkillsConfig(
        paths=[str(root)],
        auto_load_user_agents_skills=False,
        auto_load_cwd_agents_skills=False,
    )

    provider, warnings = await create_skills_provider(config)
    assert provider is not None
    assert warnings == []

    skill_dir = root / "demo"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        """\
---
name: different-name
description: Invalid because the name mismatches its directory
---

Invalid.
""",
        encoding="utf-8",
    )

    refresh_warnings = await provider.refresh_context()

    assert provider.skill_names() == []
    assert len(refresh_warnings) == 1
    assert refresh_warnings[0].code == "skill_load_error"
    assert "Skill 'demo' failed to load" in refresh_warnings[0].message


@pytest.mark.integration
async def test_relative_profile_skill_paths_isolate_per_runtime_cwd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``./skills`` resolved against different workspaces must produce independent discovery.

    Relative profile paths are resolved against the active runtime cwd so
    provider_a sees folderA's skills and provider_b sees folderB's.
    """
    fake_platform = type("P", (), {"config_dir": tmp_path / "chrys-config"})()
    monkeypatch.setattr("chrys.foundation.platform.get_platform", lambda: fake_platform)

    folder_a = tmp_path / "folderA"
    folder_b = tmp_path / "folderB"
    folder_a.mkdir()
    (folder_a / "skills").mkdir()
    skill_dir = folder_b / "skills" / "demo"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        """\
---
name: demo
description: Folder B skill
---

Folder B instructions.
""",
        encoding="utf-8",
    )

    config = SkillsConfig(
        paths=["./skills"],
        auto_load_user_agents_skills=False,
        auto_load_cwd_agents_skills=False,
    )

    provider_a, warnings_a = await create_skills_provider(
        config,
        runtime=_make_runtime(folder_a),
    )
    provider_b, warnings_b = await create_skills_provider(
        config,
        runtime=_make_runtime(folder_b),
    )

    assert provider_a is not None
    assert provider_a.skill_names() == []
    assert warnings_a == []
    assert provider_b is not None
    assert warnings_b == []
    details_b = {d.name: d for d in provider_b.skill_details()}
    assert "demo" in details_b
    assert details_b["demo"].description == "Folder B skill"


@pytest.mark.integration
async def test_parent_relative_profile_skill_path_loads_outside_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _isolate_auto_skill_dirs(tmp_path, monkeypatch)
    cwd = tmp_path / "project"
    cwd.mkdir()
    skill_dir = tmp_path / "shared-skills" / "demo"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        """\
---
name: demo
description: Shared parent skill
---

Shared instructions.
""",
        encoding="utf-8",
    )

    provider, warnings = await create_skills_provider(
        SkillsConfig(
            paths=["../shared-skills"],
            auto_load_user_agents_skills=False,
            auto_load_cwd_agents_skills=False,
        ),
        runtime=_make_runtime(cwd),
    )

    assert provider is not None
    assert warnings == []
    assert "demo" in provider.skill_names()
