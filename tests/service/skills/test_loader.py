# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for the chrys-owned skill loader (discovery + frontmatter parsing)."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest

from chrys.service.skills.loader import (
    MAX_FRONTMATTER_DEPTH,
    MAX_FRONTMATTER_LENGTH,
    build_inline_skill_content,
    discover_file_skills,
    discover_skill_directories,
    load_file_skill,
    parse_frontmatter,
)
from chrys.service.skills.model import Skill, SkillLoadFailure, SkillResource
from chrys.service.skills.provider import ChrysSkillsProvider
from tests.support.cpu_guard import cpu_bounded
from tests.support.symlinks import symlink_or_skip

if TYPE_CHECKING:
    from pathlib import Path


def _write_skill(root: Path, name: str, *, description: str = "A test skill", body: str = "Body.") -> Path:
    skill_dir = root / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n{body}\n",
        encoding="utf-8",
    )
    return skill_dir


def _load(skill_dir: Path, script_extensions: tuple[str, ...] = (".py",)) -> Skill | SkillLoadFailure:
    return load_file_skill(str(skill_dir), script_extensions=script_extensions)


@pytest.mark.parametrize("encoding", ["utf-8-sig", "utf-16", "utf-32", "gb18030"])
@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
async def test_skill_and_resource_detect_encoding(tmp_path: Path, encoding: str, newline: str) -> None:
    description = "阅读项目文档。分析代码结构并说明检查结果。"
    body = "这里是技能的详细操作说明。请保留中文内容和标点。\n第二行操作说明。\n"
    skill_dir = tmp_path / "encoded"
    skill_dir.mkdir()
    content = f"---\nname: encoded\ndescription: {description}\n---\n\n{body}"
    (skill_dir / "SKILL.md").write_bytes(content.replace("\n", newline).encode(encoding))
    (skill_dir / "guide.md").write_bytes(body.replace("\n", newline).encode(encoding))

    skill = _load(skill_dir)

    assert isinstance(skill, Skill)
    assert skill.description == description
    assert skill.content == content
    assert await _read_resource(skill, "guide.md") == body


async def test_damaged_skill_and_resource_are_rejected(tmp_path: Path) -> None:
    skill_dir = _write_skill(tmp_path, "damaged", body="中文操作说明\n" * 20)
    path = skill_dir / "SKILL.md"
    path.write_bytes(path.read_bytes() + b"\xff")

    failure = _load(skill_dir)
    assert isinstance(failure, SkillLoadFailure)
    assert failure.reason.startswith("failed to read SKILL.md:")

    skill_dir = _write_skill(tmp_path, "valid")
    (skill_dir / "guide.md").write_bytes(("中文资源内容\n" * 20).encode() + b"\xff")
    skill = _load(skill_dir)
    assert isinstance(skill, Skill)
    assert (await _read_resource(skill, "guide.md")).startswith("Error: Failed to read resource")


# ---------------------------------------------------------------------------
# Frontmatter parsing
# ---------------------------------------------------------------------------


def test_load_file_skill_parses_all_frontmatter_fields(tmp_path: Path) -> None:
    skill_dir = tmp_path / "full-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "\ufeff---\n"
        "name: full-skill\n"
        'description: "Quoted description"\n'
        "license: MIT\n"
        "compatibility: chrys >= 0.9\n"
        "allowed-tools: shell search\n"
        "metadata:\n"
        "  author: someone\n"
        '  version: "2"\n'
        "---\n\nBody text.\n",
        encoding="utf-8",
    )

    skill = _load(skill_dir)

    assert isinstance(skill, Skill)
    assert skill.name == "full-skill"
    assert skill.description == "Quoted description"
    assert skill.license == "MIT"
    assert skill.compatibility == "chrys >= 0.9"
    assert skill.allowed_tools == "shell search"
    assert skill.metadata == {"author": "someone", "version": "2"}
    assert skill.path == str(skill_dir)
    assert "Body text." in skill.content


def test_load_file_skill_without_frontmatter_fails(tmp_path: Path) -> None:
    skill_dir = tmp_path / "bare"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("Just instructions, no frontmatter.\n", encoding="utf-8")

    failure = _load(skill_dir)

    assert isinstance(failure, SkillLoadFailure)
    assert "frontmatter" in failure.reason


def test_load_file_skill_invalid_yaml_without_parseable_lines_fails(tmp_path: Path) -> None:
    skill_dir = tmp_path / "broken"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("---\n{unclosed\n---\nBody.\n", encoding="utf-8")

    failure = _load(skill_dir)

    assert isinstance(failure, SkillLoadFailure)
    assert "invalid YAML frontmatter" in failure.reason


def test_load_file_skill_tolerant_fallback_for_sloppy_plain_scalars(tmp_path: Path) -> None:
    """A colon inside an unquoted value is invalid YAML but parsed by the line fallback."""
    skill_dir = tmp_path / "sloppy"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: sloppy\ndescription: use this: that\n---\nBody.\n",
        encoding="utf-8",
    )

    skill = _load(skill_dir)

    assert isinstance(skill, Skill)
    assert skill.description == "use this: that"


def test_load_file_skill_tolerant_fallback_for_a_character_yaml_does_not_allow(tmp_path: Path) -> None:
    """YAML refuses a control character before parsing anything; the line fallback still reads the fields."""
    skill_dir = tmp_path / "form-feed"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: form-feed\n# page\x0c break\ndescription: d\n---\nBody.\n", encoding="utf-8"
    )

    skill = _load(skill_dir)

    assert isinstance(skill, Skill)
    assert (skill.name, skill.description) == ("form-feed", "d")


def test_load_file_skill_list_frontmatter_fails(tmp_path: Path) -> None:
    skill_dir = tmp_path / "listy"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("---\n- a\n- b\n---\nBody.\n", encoding="utf-8")

    failure = _load(skill_dir)

    assert isinstance(failure, SkillLoadFailure)
    assert "mapping" in failure.reason


def test_load_file_skill_name_dirname_mismatch_fails(tmp_path: Path) -> None:
    skill_dir = tmp_path / "actual-dir"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: other-name\ndescription: d\n---\nBody.\n",
        encoding="utf-8",
    )

    failure = _load(skill_dir)

    assert isinstance(failure, SkillLoadFailure)
    assert "other-name" in failure.reason
    assert "actual-dir" in failure.reason


def test_load_file_skill_invalid_name_fails(tmp_path: Path) -> None:
    skill_dir = tmp_path / "Bad_Name"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: Bad_Name\ndescription: d\n---\nBody.\n",
        encoding="utf-8",
    )

    failure = _load(skill_dir)

    assert isinstance(failure, SkillLoadFailure)
    assert "invalid skill name" in failure.reason


def test_load_file_skill_missing_description_fails(tmp_path: Path) -> None:
    skill_dir = tmp_path / "nodesc"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("---\nname: nodesc\n---\nBody.\n", encoding="utf-8")

    failure = _load(skill_dir)

    assert isinstance(failure, SkillLoadFailure)
    assert "description" in failure.reason


# ---------------------------------------------------------------------------
# Directory discovery
# ---------------------------------------------------------------------------


def test_discover_skill_directories_respects_depth_limit(tmp_path: Path) -> None:
    _write_skill(tmp_path, "top")  # depth 1 — discovered
    _write_skill(tmp_path / "group", "nested")  # depth 2 — discovered
    _write_skill(tmp_path / "group" / "deeper", "deep3")  # depth 3 — beyond limit

    discovered = {os.path.basename(d) for d in discover_skill_directories([str(tmp_path)])}

    assert discovered == {"top", "nested"}


def test_discovery_stops_below_parent_boundary_but_keeps_sibling_skills(tmp_path: Path) -> None:
    parent = _write_skill(tmp_path, "parent")
    _write_skill(parent, "nested")
    _write_skill(tmp_path, "sibling")

    discovered = set(discover_skill_directories([str(tmp_path)]))

    assert discovered == {str(parent.absolute()), str((tmp_path / "sibling").absolute())}


def test_configured_root_with_skill_file_stops_all_discovery_below_it(tmp_path: Path) -> None:
    configured_root = _write_skill(tmp_path, "configured-root")
    _write_skill(configured_root, "nested")

    discovered = discover_skill_directories([str(configured_root)])

    assert discovered == [str(configured_root.absolute())]


def test_discover_skill_directories_skips_missing_roots(tmp_path: Path) -> None:
    assert discover_skill_directories([str(tmp_path / "missing"), ""]) == []


@pytest.mark.skipif(os.name == "nt", reason="symlink creation requires privileges on Windows")
def test_discovery_accepts_symlinked_skill_root(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside_skill = _write_skill(tmp_path, "outside")
    linked_skill = root / "outside"
    linked_skill.symlink_to(outside_skill, target_is_directory=True)

    assert discover_skill_directories([str(root)]) == [str(linked_skill.absolute())]


def test_discover_file_skills_collects_failures_and_dedupes_first_wins(tmp_path: Path) -> None:
    _write_skill(tmp_path / "root-a", "demo", description="First wins")
    _write_skill(tmp_path / "root-b", "demo", description="Loses dedup")
    bad_dir = tmp_path / "root-a" / "broken"
    bad_dir.mkdir()
    (bad_dir / "SKILL.md").write_text("---\nname: not-broken\ndescription: d\n---\n", encoding="utf-8")

    skills, failures = discover_file_skills(
        [str(tmp_path / "root-a"), str(tmp_path / "root-b")],
        script_extensions=(".py",),
    )

    assert [s.name for s in skills] == ["demo"]
    assert skills[0].description == "First wins"
    assert len(failures) == 1
    assert failures[0].skill_dir == str(bad_dir)
    assert "not-broken" in failures[0].reason


# ---------------------------------------------------------------------------
# Resource and script scanning
# ---------------------------------------------------------------------------


def test_resources_scanned_recursively_by_extension(tmp_path: Path) -> None:
    skill_dir = _write_skill(tmp_path, "rich")
    (skill_dir / "notes.md").write_text("root resource", encoding="utf-8")
    (skill_dir / "ignored.bin").write_text("wrong extension", encoding="utf-8")
    (skill_dir / "references").mkdir()
    (skill_dir / "references" / "faq.md").write_text("faq", encoding="utf-8")
    (skill_dir / "references" / "nested").mkdir()
    (skill_dir / "references" / "nested" / "hidden.md").write_text("not scanned", encoding="utf-8")
    (skill_dir / "assets").mkdir()
    (skill_dir / "assets" / "data.json").write_text("{}", encoding="utf-8")
    (skill_dir / "scripts").mkdir()
    (skill_dir / "scripts" / "README.md").write_text("script docs", encoding="utf-8")
    (skill_dir / "extra").mkdir()
    (skill_dir / "extra" / "details.yaml").write_text("key: value", encoding="utf-8")

    skill = _load(skill_dir)

    assert isinstance(skill, Skill)
    names = [r.name for r in skill.resources]
    # Sorted, forward-slash relative paths; SKILL.md and non-resource
    # extensions excluded; default depth scans root plus one subdirectory level.
    assert names == ["assets/data.json", "extra/details.yaml", "notes.md", "references/faq.md", "scripts/README.md"]
    assert all(r.full_path and os.path.isabs(r.full_path) for r in skill.resources)
    assert "SKILL.md" not in {os.path.basename(n) for n in names}


def test_scripts_scanned_with_extension_filter(tmp_path: Path) -> None:
    skill_dir = _write_skill(tmp_path, "scripted")
    (skill_dir / "root_tool.py").write_text("print('root')", encoding="utf-8")
    scripts = skill_dir / "scripts"
    scripts.mkdir()
    (scripts / "convert.py").write_text("print('convert')", encoding="utf-8")
    (scripts / "setup.sh").write_text("echo setup", encoding="utf-8")
    (scripts / "README.md").write_text("docs, not a script", encoding="utf-8")
    (skill_dir / "references").mkdir()
    (skill_dir / "references" / "embedded.py").write_text("print('embedded')", encoding="utf-8")

    skill = _load(skill_dir, script_extensions=(".py", ".sh"))

    assert isinstance(skill, Skill)
    assert [s.name for s in skill.scripts] == [
        "references/embedded.py",
        "root_tool.py",
        "scripts/convert.py",
        "scripts/setup.sh",
    ]
    assert all(os.path.isabs(s.full_path) for s in skill.scripts)


def test_search_depth_controls_recursive_file_scan(tmp_path: Path) -> None:
    skill_dir = _write_skill(tmp_path, "depthy")
    (skill_dir / "root.md").write_text("root", encoding="utf-8")
    (skill_dir / "root.py").write_text("print('root')", encoding="utf-8")
    first = skill_dir / "first"
    first.mkdir()
    (first / "one.md").write_text("one", encoding="utf-8")
    (first / "one.py").write_text("print('one')", encoding="utf-8")
    second = first / "second"
    second.mkdir()
    (second / "two.md").write_text("two", encoding="utf-8")
    (second / "two.py").write_text("print('two')", encoding="utf-8")

    root_only = load_file_skill(str(skill_dir), script_extensions=(".py",), search_depth=1)
    deep = load_file_skill(str(skill_dir), script_extensions=(".py",), search_depth=3)

    assert isinstance(root_only, Skill)
    assert [r.name for r in root_only.resources] == ["root.md"]
    assert [s.name for s in root_only.scripts] == ["root.py"]
    assert isinstance(deep, Skill)
    assert [r.name for r in deep.resources] == ["first/one.md", "first/second/two.md", "root.md"]
    assert [s.name for s in deep.scripts] == ["first/one.py", "first/second/two.py", "root.py"]


def test_nested_skill_files_are_attached_to_parent_with_relative_names(tmp_path: Path) -> None:
    skill_dir = _write_skill(tmp_path, "parent")
    (skill_dir / "parent.md").write_text("parent", encoding="utf-8")
    (skill_dir / "parent.py").write_text("print('parent')", encoding="utf-8")
    child_dir = _write_skill(skill_dir, "child")
    (child_dir / "child.md").write_text("child", encoding="utf-8")
    (child_dir / "child.py").write_text("print('child')", encoding="utf-8")

    skill = load_file_skill(str(skill_dir), script_extensions=(".py",), search_depth=3)

    assert isinstance(skill, Skill)
    assert [r.name for r in skill.resources] == ["child/child.md", "parent.md"]
    assert [s.name for s in skill.scripts] == ["child/child.py", "parent.py"]
    assert "child/SKILL.md" not in [r.name for r in skill.resources]


def test_nested_skill_files_still_obey_parent_scan_depth(tmp_path: Path) -> None:
    skill_dir = _write_skill(tmp_path, "parent")
    child_dir = _write_skill(skill_dir, "child")
    (child_dir / "direct.md").write_text("direct", encoding="utf-8")
    deep_dir = child_dir / "deep"
    deep_dir.mkdir()
    (deep_dir / "deep.md").write_text("deep", encoding="utf-8")

    default_depth = load_file_skill(str(skill_dir), script_extensions=(".py",))
    raised_depth = load_file_skill(str(skill_dir), script_extensions=(".py",), search_depth=3)

    assert isinstance(default_depth, Skill)
    assert [resource.name for resource in default_depth.resources] == ["child/direct.md"]
    assert isinstance(raised_depth, Skill)
    assert [resource.name for resource in raised_depth.resources] == ["child/deep/deep.md", "child/direct.md"]


def test_nested_name_mismatch_no_longer_emits_independent_load_failure(tmp_path: Path) -> None:
    parent = _write_skill(tmp_path, "parent")
    nested = parent / "nested"
    nested.mkdir()
    (nested / "SKILL.md").write_text(
        "---\nname: wrong-name\ndescription: no longer independently loaded\n---\n",
        encoding="utf-8",
    )
    (nested / "guide.md").write_text("belongs to parent", encoding="utf-8")

    skills, failures = discover_file_skills([str(tmp_path)], script_extensions=(".py",), search_depth=2)

    assert [skill.name for skill in skills] == ["parent"]
    assert [resource.name for resource in skills[0].resources] == ["nested/guide.md"]
    assert failures == []


def test_resource_and_script_filters_receive_skill_name_and_relative_path(tmp_path: Path) -> None:
    skill_dir = _write_skill(tmp_path, "filtered")
    (skill_dir / "keep.md").write_text("keep", encoding="utf-8")
    (skill_dir / "drop.md").write_text("drop", encoding="utf-8")
    (skill_dir / "keep.py").write_text("print('keep')", encoding="utf-8")
    (skill_dir / "drop.py").write_text("print('drop')", encoding="utf-8")
    seen_resources: list[tuple[str, str]] = []
    seen_scripts: list[tuple[str, str]] = []

    def resource_filter(skill_name: str, rel_path: str) -> bool:
        seen_resources.append((skill_name, rel_path))
        return rel_path.startswith("keep")

    def script_filter(skill_name: str, rel_path: str) -> bool:
        seen_scripts.append((skill_name, rel_path))
        return rel_path.startswith("keep")

    skill = load_file_skill(
        str(skill_dir),
        script_extensions=(".py",),
        resource_filter=resource_filter,
        script_filter=script_filter,
    )

    assert isinstance(skill, Skill)
    assert [r.name for r in skill.resources] == ["keep.md"]
    assert [s.name for s in skill.scripts] == ["keep.py"]
    assert sorted(seen_resources) == [("filtered", "drop.md"), ("filtered", "keep.md")]
    assert sorted(seen_scripts) == [("filtered", "drop.py"), ("filtered", "keep.py")]


def test_nested_files_pass_parent_identity_and_relative_path_to_filters(tmp_path: Path) -> None:
    skill_dir = _write_skill(tmp_path, "parent")
    child_dir = _write_skill(skill_dir, "child")
    (child_dir / "guide.md").write_text("guide", encoding="utf-8")
    (child_dir / "run.py").write_text("print('run')", encoding="utf-8")
    seen_resources: list[tuple[str, str]] = []
    seen_scripts: list[tuple[str, str]] = []

    def keep_resource(skill_name: str, rel_path: str) -> bool:
        seen_resources.append((skill_name, rel_path))
        return True

    def keep_script(skill_name: str, rel_path: str) -> bool:
        seen_scripts.append((skill_name, rel_path))
        return True

    skill = load_file_skill(
        str(skill_dir),
        script_extensions=(".py",),
        search_depth=2,
        resource_filter=keep_resource,
        script_filter=keep_script,
    )

    assert isinstance(skill, Skill)
    assert seen_resources == [("parent", "child/guide.md")]
    assert seen_scripts == [("parent", "child/run.py")]


def test_search_depth_less_than_one_raises(tmp_path: Path) -> None:
    skill_dir = _write_skill(tmp_path, "invalid-depth")

    with pytest.raises(ValueError, match="search_depth must be >= 1"):
        load_file_skill(str(skill_dir), script_extensions=(".py",), search_depth=0)
    with pytest.raises(ValueError, match="search_depth must be >= 1"):
        discover_file_skills([str(tmp_path)], script_extensions=(".py",), search_depth=0)


@pytest.mark.skipif(os.name == "nt", reason="symlink creation requires privileges on Windows")
def test_symlinked_resource_outside_skill_dir_is_skipped(tmp_path: Path) -> None:
    secret = tmp_path / "secret.md"
    secret.write_text("outside content", encoding="utf-8")
    skill_dir = _write_skill(tmp_path, "linker")
    (skill_dir / "references").mkdir()
    (skill_dir / "references" / "leak.md").symlink_to(secret)
    (skill_dir / "references" / "safe.md").write_text("inside", encoding="utf-8")

    skill = _load(skill_dir)

    assert isinstance(skill, Skill)
    assert [r.name for r in skill.resources] == ["references/safe.md"]


@pytest.mark.skipif(os.name == "nt", reason="symlink creation requires privileges on Windows")
def test_symlinked_script_directory_is_skipped(tmp_path: Path) -> None:
    outside = tmp_path / "outside-scripts"
    outside.mkdir()
    (outside / "evil.py").write_text("print('evil')", encoding="utf-8")
    skill_dir = _write_skill(tmp_path, "dirlink")
    (skill_dir / "scripts").symlink_to(outside, target_is_directory=True)

    skill = _load(skill_dir)

    assert isinstance(skill, Skill)
    assert skill.scripts == []


def test_junction_resource_directory_is_skipped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """NT junctions trip the below-root escape guard exactly like symlinks.

    Junctions do not exist on POSIX (and is_symlink() is False for them on
    Windows), so simulate the reparse point via os.path.isjunction, which
    Path.is_junction delegates to.
    """
    skill_dir = _write_skill(tmp_path, "junctioned")
    (skill_dir / "references").mkdir()
    (skill_dir / "references" / "safe.md").write_text("inside", encoding="utf-8")
    (skill_dir / "linked").mkdir()
    (skill_dir / "linked" / "trapped.md").write_text("via junction", encoding="utf-8")

    junction_dir = os.fspath(skill_dir / "linked")
    real_isjunction = os.path.isjunction

    def fake_isjunction(path: object) -> bool:
        if os.fspath(path) == junction_dir:  # type: ignore[arg-type]
            return True
        return real_isjunction(path)  # type: ignore[arg-type]

    monkeypatch.setattr(os.path, "isjunction", fake_isjunction)

    skill = _load(skill_dir)

    assert isinstance(skill, Skill)
    assert [r.name for r in skill.resources] == ["references/safe.md"]


@pytest.mark.skipif(os.name == "nt", reason="symlink creation requires privileges on Windows")
def test_nested_skill_boundary_does_not_weaken_symlink_escape_guard(tmp_path: Path) -> None:
    secret = tmp_path / "secret.md"
    secret.write_text("outside content", encoding="utf-8")
    skill_dir = _write_skill(tmp_path, "parent")
    child_dir = _write_skill(skill_dir, "child")
    (child_dir / "safe.md").write_text("inside", encoding="utf-8")
    (child_dir / "leak.md").symlink_to(secret)

    skill = load_file_skill(str(skill_dir), script_extensions=(".py",), search_depth=2)

    assert isinstance(skill, Skill)
    assert [resource.name for resource in skill.resources] == ["child/safe.md"]


def _backslash_escape_skill(tmp_path: Path) -> tuple[Path, Path]:
    """A skill whose file names rebuild, with ``\\`` read as a separator, into a path outside it."""
    secret = tmp_path / "secret.md"
    secret.write_text("outside content", encoding="utf-8")
    skill_dir = _write_skill(tmp_path / "skills", "bslash")
    (skill_dir / "..\\..\\secret.md").write_text("inside resource", encoding="utf-8")
    (skill_dir / "..\\..\\run.py").write_text("print('inside')", encoding="utf-8")
    return skill_dir, secret


@pytest.mark.skipif(os.name == "nt", reason="a backslash is a path separator on Windows")
def test_backslash_named_files_keep_the_path_that_passed_the_checks(tmp_path: Path) -> None:
    skill_dir, _secret = _backslash_escape_skill(tmp_path)

    skill = _load(skill_dir)

    assert isinstance(skill, Skill)
    assert [(r.name, r.full_path) for r in skill.resources] == [
        ("../../secret.md", str(skill_dir / "..\\..\\secret.md"))
    ]
    assert [(s.name, s.full_path) for s in skill.scripts] == [("../../run.py", str(skill_dir / "..\\..\\run.py"))]


async def _read_resource(skill: Skill, resource_name: str) -> str:
    async def load_skills() -> list[Skill]:
        return [skill]

    skills_provider = ChrysSkillsProvider(load_skills)
    await skills_provider.initialize()
    return await skills_provider._read_skill_resource(skills_provider._skills, skill.name, resource_name)


@pytest.mark.skipif(os.name == "nt", reason="a backslash is a path separator on Windows")
async def test_backslash_named_resource_reads_the_file_inside_the_skill(tmp_path: Path) -> None:
    skill_dir, _secret = _backslash_escape_skill(tmp_path)
    skill = _load(skill_dir)
    assert isinstance(skill, Skill)

    assert await _read_resource(skill, "../../secret.md") == "inside resource"


async def test_resource_swapped_for_a_symlink_after_loading_is_not_read(tmp_path: Path) -> None:
    secret = tmp_path / "secret.md"
    secret.write_text("outside content", encoding="utf-8")
    skill_dir = _write_skill(tmp_path, "swapped")
    (skill_dir / "references").mkdir()
    guide = skill_dir / "references" / "guide.md"
    guide.write_text("inside", encoding="utf-8")
    skill = _load(skill_dir)
    assert isinstance(skill, Skill)
    assert await _read_resource(skill, "references/guide.md") == "inside"

    guide.unlink()
    symlink_or_skip(guide, secret)
    result = await _read_resource(skill, "references/guide.md")

    assert result.startswith("Error: ")
    assert "outside content" not in result
    assert "resolves outside skill 'swapped'" in result


# ---------------------------------------------------------------------------
# Inline content building
# ---------------------------------------------------------------------------


def test_build_inline_skill_content_format_with_resources_and_escaping() -> None:
    content = build_inline_skill_content(
        name="demo",
        description="Uses <angle> & ampersand",
        instructions="Step one.\nStep two.",
        resources=[
            SkillResource(name="guide", description='Say "hi"', content="g"),
            SkillResource(name="plain", content="p"),
        ],
    )

    assert content == (
        "<name>demo</name>\n"
        "<description>Uses &lt;angle&gt; &amp; ampersand</description>\n"
        "\n"
        "<instructions>\n"
        "Step one.\nStep two.\n"
        "</instructions>\n"
        "\n"
        "<resources>\n"
        '  <resource name="guide" description="Say &quot;hi&quot;"/>\n'
        '  <resource name="plain"/>\n'
        "</resources>\n"
        "\n"
        "<scripts />"
    )


def test_build_inline_skill_content_without_resources_emits_empty_blocks() -> None:
    content = build_inline_skill_content(name="demo", description="d", instructions="i")

    assert content.endswith("<resources />\n\n<scripts />")


# ---------------------------------------------------------------------------
# SKILL.md files from untrusted repositories
# ---------------------------------------------------------------------------


def test_frontmatter_without_a_closing_line_is_rejected_in_linear_time(tmp_path: Path) -> None:
    skill_dir = tmp_path / "blank-lines"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("---" + " \n" * 30_000 + "Body.\n", encoding="utf-8")

    loaded = cpu_bounded(lambda: _load(skill_dir))

    assert isinstance(loaded, SkillLoadFailure)
    assert "does not contain YAML frontmatter" in loaded.reason


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_frontmatter_blocks_match_with_either_newline(tmp_path: Path, newline: str) -> None:
    for name, block in (("empty-block", ""), ("plain-block", f"name: plain-block{newline}description: d{newline}")):
        skill_dir = tmp_path / name
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_bytes(f"---{newline}{block}---{newline}Body.{newline}".encode())

    empty, plain = _load(tmp_path / "empty-block"), _load(tmp_path / "plain-block")

    assert isinstance(empty, SkillLoadFailure) and empty.reason == "frontmatter is missing a `name`"
    assert isinstance(plain, Skill) and plain.description == "d"


def test_a_skill_md_that_cannot_be_read_or_parsed_fails_alone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from pathlib import Path

    _write_skill(tmp_path, "good")
    unreadable = _write_skill(tmp_path, "unreadable") / "SKILL.md"
    read_bytes = Path.read_bytes

    def refuse_one_file(path: Path) -> bytes:
        if path == unreadable:
            raise PermissionError("permission denied")
        return read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", refuse_one_file)
    no_such_day = tmp_path / "no-such-day"
    no_such_day.mkdir()
    # YAML reads this as a date, and building it raises ValueError rather than a YAML error.
    (no_such_day / "SKILL.md").write_text(
        "---\nname: no-such-day\ndescription: d\nlicense: 2024-02-30\n---\n", encoding="utf-8"
    )

    skills, failures = discover_file_skills([str(tmp_path)], script_extensions=(".py",))

    assert [skill.name for skill in skills] == ["good"]
    reasons = {os.path.basename(failure.skill_dir): failure.reason for failure in failures}
    assert reasons.keys() == {"unreadable", "no-such-day"}
    assert reasons["unreadable"] == "failed to read SKILL.md: permission denied"
    assert reasons["no-such-day"].startswith("failed to load SKILL.md: day ")


@pytest.mark.parametrize(
    "fields",
    ["description: &shared d\nlicense: *shared", "description: d\nlicense: &unused MIT"],
    ids=["alias", "anchor"],
)
def test_frontmatter_with_an_anchor_or_alias_fails(tmp_path: Path, fields: str) -> None:
    skill_dir = tmp_path / "aliased"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(f"---\nname: aliased\n{fields}\n---\n", encoding="utf-8")

    loaded = _load(skill_dir)

    assert isinstance(loaded, SkillLoadFailure)
    assert loaded.reason == "YAML frontmatter must not use anchors or aliases"


@pytest.mark.parametrize(("lists", "loads"), [(MAX_FRONTMATTER_DEPTH - 1, True), (MAX_FRONTMATTER_DEPTH, False)])
def test_frontmatter_nested_past_the_depth_limit_fails(tmp_path: Path, lists: int, loads: bool) -> None:
    skill_dir = tmp_path / "nested"
    skill_dir.mkdir()
    # The top mapping is one level, so the innermost list sits one level below *lists*.
    nested = "[" * lists + "]" * lists
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: nested\ndescription: d\nlicense: {nested}\n---\n", encoding="utf-8"
    )

    loaded = _load(skill_dir)

    if loads:
        assert isinstance(loaded, Skill)
    else:
        assert isinstance(loaded, SkillLoadFailure)
        assert loaded.reason == f"YAML frontmatter must not nest more than {MAX_FRONTMATTER_DEPTH} levels deep"


def test_frontmatter_nested_far_too_deep_stops_after_bounded_work() -> None:
    nested = "[" * 8_000 + "]" * 8_000
    content = f"---\nname: nested\ndescription: d\nlicense: {nested}\n---\n"

    fields, reason = cpu_bounded(lambda: parse_frontmatter(content), bound_seconds=2)

    assert fields is None
    assert reason == f"YAML frontmatter must not nest more than {MAX_FRONTMATTER_DEPTH} levels deep"


@pytest.mark.parametrize(("length", "loads"), [(MAX_FRONTMATTER_LENGTH, True), (MAX_FRONTMATTER_LENGTH + 1, False)])
def test_frontmatter_longer_than_the_limit_fails(tmp_path: Path, length: int, loads: bool) -> None:
    skill_dir = tmp_path / "sized"
    skill_dir.mkdir()
    head = "name: sized\ndescription: d\n"
    block = head + "#" + "x" * (length - len(head) - 2) + "\n"
    assert len(block) == length
    (skill_dir / "SKILL.md").write_text(f"---\n{block}---\n", encoding="utf-8")

    loaded = _load(skill_dir)

    if loads:
        assert isinstance(loaded, Skill)
    else:
        assert isinstance(loaded, SkillLoadFailure)
        assert loaded.reason == f"YAML frontmatter must be {MAX_FRONTMATTER_LENGTH} characters or fewer"


def test_the_line_fallback_reads_a_long_line_in_linear_time() -> None:
    padding = " " * (MAX_FRONTMATTER_LENGTH - 64)
    content = f"---\nnote: not: yaml\nname: spaced{padding}out\ndescription: 'quoted'\n---\n"

    fields, reason = cpu_bounded(lambda: parse_frontmatter(content), bound_seconds=0.1)

    assert reason is None
    assert fields == {"note": "not: yaml", "name": f"spaced{padding}out", "description": "quoted"}


@pytest.mark.parametrize("field", ["name", "description"])
@pytest.mark.parametrize("value", ["[typed]", "{text: typed}"], ids=["list", "mapping"])
def test_a_name_or_description_that_is_not_text_fails(tmp_path: Path, field: str, value: str) -> None:
    skill_dir = tmp_path / "typed"
    skill_dir.mkdir()
    fields = {"name": "typed", "description": "d", field: value}
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {fields['name']}\ndescription: {fields['description']}\n---\n", encoding="utf-8"
    )

    loaded = _load(skill_dir)

    assert isinstance(loaded, SkillLoadFailure)
    assert loaded.reason == f"frontmatter `{field}` must be text, not a list or mapping"


def test_optional_fields_that_are_not_text_are_dropped(tmp_path: Path) -> None:
    skill_dir = tmp_path / "loose-fields"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: loose-fields\n"
        "description: d\n"
        "license: [MIT]\n"
        "compatibility: {chrys: 1}\n"
        "allowed-tools: [shell, search]\n"
        "metadata:\n"
        "  author: someone\n"
        "  nested: {deep: value}\n"
        "---\n",
        encoding="utf-8",
    )

    skill = _load(skill_dir)

    assert isinstance(skill, Skill)
    assert (skill.license, skill.compatibility, skill.allowed_tools, skill.metadata) == (None, None, None, None)


def test_flat_metadata_keeps_numbers_and_dates_as_text(tmp_path: Path) -> None:
    skill_dir = tmp_path / "flat-metadata"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: flat-metadata\ndescription: d\nlicense: 2\nmetadata:\n  count: 3\n  released: 2026-10-05\n---\n",
        encoding="utf-8",
    )

    skill = _load(skill_dir)

    assert isinstance(skill, Skill)
    assert skill.license == "2"
    assert skill.metadata == {"count": "3", "released": "2026-10-05"}
