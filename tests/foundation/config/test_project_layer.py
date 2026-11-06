# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The project trust domain: gate, whitelist, and merge directions."""

from __future__ import annotations

import dataclasses
import os
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

import chrys.foundation.platform as platform_mod
from chrys.foundation.config.coercion import CoerceReason, CoerceStatus
from chrys.foundation.config.context import EvalContext
from chrys.foundation.config.process_env import freeze_process_env
from chrys.foundation.config.settings import Settings
from chrys.foundation.config.settings_store import load_settings
from chrys.foundation.config.spec import ProjectMerge, Source, specs_by_field


@pytest.fixture
def config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    fake = dataclasses.replace(
        platform_mod.get_platform(),
        config_dir=tmp_path / "config",
        data_dir=tmp_path / "data",
    )
    monkeypatch.setattr(platform_mod, "get_platform", lambda: fake)
    return fake.config_dir


@pytest.fixture(autouse=True)
def _clean_chrys_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """The developer's own ``CHRYS_*`` exports must not reach these loads."""
    for name in list(os.environ):
        if name.startswith("CHRYS_"):
            monkeypatch.delenv(name, raising=False)


@pytest.fixture
def project_root(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    return root


def _write_user_yaml(config_dir: Path, text: str) -> Path:
    config_dir.mkdir(parents=True, exist_ok=True)
    path = config_dir / "settings.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def _write_project_yaml(root: Path, text: str) -> Path:
    (root / ".chrys").mkdir(parents=True, exist_ok=True)
    path = root / ".chrys" / "settings.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def _enable_gate(config_dir: Path) -> Path:
    return _write_user_yaml(config_dir, "project:\n  config_enabled: true\n")


def test_project_cannot_choose_auto_review_strategy(config_dir: Path, project_root: Path) -> None:
    freeze_process_env()
    _enable_gate(config_dir)
    _write_project_yaml(project_root, "approval:\n  auto_review: formal\n")
    assert load_settings(project_root=project_root).settings.approval_auto_review == "direct"


# ── the gate ─────────────────────────────────────────────────────────


def test_project_settings_stay_dormant_until_the_gate_is_enabled(config_dir: Path, project_root: Path) -> None:
    freeze_process_env()
    path = _write_project_yaml(project_root, "session:\n  title:\n    auto: false\nstray: 1\n")

    loaded = load_settings(project_root=project_root)

    assert loaded.settings.session_title_auto is True
    assert loaded.source_for("session.title.auto").layer is Source.DEFAULT
    (dormant,) = loaded.dormant_project
    assert dormant.path == path
    assert dormant.keys == ("session.title.auto", "stray")
    assert loaded.warnings == ()


def test_a_root_with_nothing_to_say_reports_nothing(config_dir: Path, project_root: Path) -> None:
    """No file, or an empty one, is not a dormant configuration."""
    freeze_process_env()

    assert load_settings(project_root=project_root).dormant_project == ()

    _write_project_yaml(project_root, "")
    assert load_settings(project_root=project_root).dormant_project == ()


@pytest.mark.parametrize("spelling", ["home", "HOME"])
def test_user_settings_are_not_reclassified_when_the_workspace_is_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, spelling: str
) -> None:
    home = tmp_path / "home"
    config_dir = home / ".chrys"
    fake = dataclasses.replace(
        platform_mod.get_platform(),
        config_dir=config_dir,
        data_dir=config_dir,
    )
    monkeypatch.setattr(platform_mod, "get_platform", lambda: fake)
    freeze_process_env()
    path = _write_user_yaml(config_dir, "approval:\n  default_mode: bypass\n")
    workspace = tmp_path / spelling
    if not workspace.exists():
        pytest.skip("Case-sensitive filesystem: the other spelling is another folder")

    loaded = load_settings(project_root=workspace)

    assert loaded.settings.default_approval_mode == "bypass"
    origin = loaded.source_for("approval.default_mode")
    assert origin.layer is Source.USER
    assert origin.path == path
    assert loaded.dormant_project == ()
    assert loaded.warnings == ()


def test_the_gate_enables_the_project_layer_with_the_file_as_origin(config_dir: Path, project_root: Path) -> None:
    freeze_process_env()
    _enable_gate(config_dir)
    path = _write_project_yaml(project_root, "session:\n  title:\n    auto: false\n")

    loaded = load_settings(project_root=project_root)

    assert loaded.settings.session_title_auto is False
    origin = loaded.source_for("session.title.auto")
    assert origin.layer is Source.PROJECT
    assert origin.path == path
    assert loaded.dormant_project == ()
    assert loaded.warnings == ()


def test_project_verify_commands_replace_the_builtin_default_when_the_gate_is_enabled(
    config_dir: Path,
    project_root: Path,
) -> None:
    freeze_process_env()
    _enable_gate(config_dir)
    path = _write_project_yaml(project_root, "trajectory:\n  verify_commands: project-check,project build\n")

    loaded = load_settings(project_root=project_root)

    assert loaded.settings.trajectory_verify_commands == "project-check,project build"
    assert loaded.settings.trajectory_verify_commands != Settings().trajectory_verify_commands
    origin = loaded.source_for("trajectory.verify_commands")
    assert origin.layer is Source.PROJECT
    assert origin.path == path


def test_the_gate_counts_only_from_the_user_document(config_dir: Path, project_root: Path) -> None:
    """A pin outranks every source for its field — but it is not the user document."""
    freeze_process_env()
    _write_project_yaml(project_root, "session:\n  title:\n    auto: false\n")

    loaded = load_settings(project_root=project_root, project_config_enabled=True)

    assert loaded.settings.project_config_enabled is True
    assert loaded.settings.session_title_auto is True
    assert len(loaded.dormant_project) == 1


def test_a_project_file_may_not_touch_its_own_gate(config_dir: Path, project_root: Path) -> None:
    """The layer authorising itself, in either direction, is the one closed loop."""
    freeze_process_env()

    # Gate off: the file's own ``config_enabled: true`` is just another dormant key.
    _write_project_yaml(project_root, "project:\n  config_enabled: true\n")
    loaded = load_settings(project_root=project_root)
    assert loaded.settings.project_config_enabled is False
    (dormant,) = loaded.dormant_project
    assert dormant.keys == ("project.config_enabled",)

    # Gate on: the key is denied like any other non-whitelisted one.
    _enable_gate(config_dir)
    loaded = load_settings(project_root=project_root)
    assert loaded.settings.project_config_enabled is True
    (warning,) = loaded.warnings
    assert warning.key == "project.config_enabled"
    assert warning.outcome.reason is CoerceReason.NOT_ALLOWED_IN_PROJECT


# ── the whitelist ────────────────────────────────────────────────────


def test_the_project_whitelist_is_exactly_the_engineering_knobs() -> None:
    """DENY is the default; every exception is named here, so a new field
    cannot drift into project control without this test changing with it."""
    merges = {
        entry.key: entry.project_merge
        for entry in specs_by_field(Settings).values()
        if entry.project_merge is not ProjectMerge.DENY
    }

    assert merges == {
        "context.warn_threshold_pct": ProjectMerge.FREE,
        "llm.retry.max_transient": ProjectMerge.TIGHTEN_ONLY,
        "mutations.coordination.enabled": ProjectMerge.ENABLE_ONLY,
        "mutations.parallel_implicit_tools": ProjectMerge.DISABLE_ONLY,
        "session.title.auto": ProjectMerge.DISABLE_ONLY,
        "tools.result.ceiling_tokens": ProjectMerge.TIGHTEN_ONLY,
        "trajectory.verify_commands": ProjectMerge.FREE,
        "workspace.change_notice.enabled": ProjectMerge.ENABLE_ONLY,
        "workspace.change_notice.max_entries": ProjectMerge.FREE,
    }


def test_the_high_risk_keys_are_denied_to_projects() -> None:
    """Redundant with the exact whitelist above, and deliberately so: these are
    the fields whose project control would be an escalation, not a preference."""
    specs = {entry.key: entry for entry in specs_by_field(Settings).values()}

    for key in (
        "agent.default_profile",
        "model.profile.active",
        "approval.default_mode",
        "storage.session_root_dir",
        "mutations.trace.fsatrace_path",
        "log.raw_http_capture",
        "project.config_enabled",
    ):
        assert specs[key].project_merge is ProjectMerge.DENY, key


def test_a_denied_key_warns_and_does_not_apply(config_dir: Path, project_root: Path) -> None:
    freeze_process_env()
    _enable_gate(config_dir)
    path = _write_project_yaml(project_root, "agent:\n  default_profile: evil\n")

    loaded = load_settings(project_root=project_root)

    assert loaded.settings.default_agent == Settings().default_agent
    (warning,) = loaded.warnings
    assert warning.key == "agent.default_profile"
    assert warning.origin.layer is Source.PROJECT
    assert warning.origin.path == path
    assert warning.outcome.reason is CoerceReason.NOT_ALLOWED_IN_PROJECT


def test_an_unknown_project_key_is_rejected_not_tolerated(config_dir: Path, project_root: Path) -> None:
    """In the user document a stranger is a downgrade artefact; a repository
    gets no such benefit of the doubt."""
    freeze_process_env()
    _enable_gate(config_dir)
    _write_project_yaml(project_root, "stray: 1\n")

    loaded = load_settings(project_root=project_root)

    (warning,) = loaded.warnings
    assert warning.key == "stray"
    assert warning.outcome.reason is CoerceReason.NOT_ALLOWED_IN_PROJECT
    assert loaded.unknown_keys == ()


# ── merge directions ─────────────────────────────────────────────────


def test_a_project_may_tighten_the_retry_budget_but_not_loosen_it(config_dir: Path, project_root: Path) -> None:
    freeze_process_env()
    _enable_gate(config_dir)

    _write_project_yaml(project_root, "llm:\n  retry:\n    max_transient: 3\n")
    loaded = load_settings(project_root=project_root)
    assert loaded.settings.max_transient_retries == 3
    assert loaded.source_for("llm.retry.max_transient").layer is Source.PROJECT

    _write_project_yaml(project_root, "llm:\n  retry:\n    max_transient: 12\n")
    loaded = load_settings(project_root=project_root)
    assert loaded.settings.max_transient_retries is None
    assert loaded.source_for("llm.retry.max_transient").layer is Source.DEFAULT
    (warning,) = loaded.warnings
    assert warning.outcome.reason is CoerceReason.LOOSENS_USER_BASELINE
    assert warning.outcome.raw == "12"


def test_the_frontend_context_informs_the_tighten_verdict(config_dir: Path, project_root: Path) -> None:
    """The same file is a loosening for the TUI (10) and a tightening for
    headless (18) — which is why the context is an input to the load."""
    freeze_process_env()
    _enable_gate(config_dir)
    _write_project_yaml(project_root, "llm:\n  retry:\n    max_transient: 12\n")

    tui = load_settings(project_root=project_root)
    headless = load_settings(
        project_root=project_root, eval_context=EvalContext(frontend_default_max_transient_retries=18)
    )

    assert tui.settings.max_transient_retries is None
    assert headless.settings.max_transient_retries == 12
    assert headless.warnings == ()


def test_a_project_value_is_judged_at_its_clamped_canonical(config_dir: Path, project_root: Path) -> None:
    """60 clamps to 50 before the direction check, so against a user baseline
    of 50 it is a no-op to accept — not a raw 60 to reject."""
    freeze_process_env()
    _write_user_yaml(config_dir, "project:\n  config_enabled: true\nllm:\n  retry:\n    max_transient: 50\n")
    _write_project_yaml(project_root, "llm:\n  retry:\n    max_transient: 60\n")

    loaded = load_settings(project_root=project_root)

    assert loaded.settings.max_transient_retries == 50
    assert loaded.source_for("llm.retry.max_transient").layer is Source.PROJECT
    (warning,) = loaded.warnings
    assert warning.outcome.status is CoerceStatus.CLAMPED
    assert warning.outcome.reason is CoerceReason.ABOVE_MAXIMUM
    assert warning.origin.layer is Source.PROJECT


def test_a_project_may_enable_coordination_but_not_disable_it(config_dir: Path, project_root: Path) -> None:
    freeze_process_env()
    # The user turned coordination off; enabling it back is the allowed direction.
    _write_user_yaml(config_dir, "project:\n  config_enabled: true\nmutations:\n  coordination:\n    enabled: false\n")
    _write_project_yaml(project_root, "mutations:\n  coordination:\n    enabled: true\n")

    loaded = load_settings(project_root=project_root)

    assert loaded.settings.mutation_coordination is True
    assert loaded.source_for("mutations.coordination.enabled").layer is Source.PROJECT
    assert loaded.warnings == ()

    # Losing cross-session attribution is not a repository's call to make.
    _enable_gate(config_dir)
    _write_project_yaml(project_root, "mutations:\n  coordination:\n    enabled: false\n")
    loaded = load_settings(project_root=project_root)
    assert loaded.settings.mutation_coordination is True
    (warning,) = loaded.warnings
    assert warning.key == "mutations.coordination.enabled"
    assert warning.outcome.reason is CoerceReason.LOOSENS_USER_BASELINE


def test_a_project_may_disable_the_auto_title_but_not_enable_it(config_dir: Path, project_root: Path) -> None:
    freeze_process_env()
    # Default True: switching the extra LLM traffic off is the allowed direction.
    _enable_gate(config_dir)
    _write_project_yaml(project_root, "session:\n  title:\n    auto: false\n")
    loaded = load_settings(project_root=project_root)
    assert loaded.settings.session_title_auto is False

    # The user opted out; the repository may not opt them back in.
    _write_user_yaml(config_dir, "project:\n  config_enabled: true\nsession:\n  title:\n    auto: false\n")
    _write_project_yaml(project_root, "session:\n  title:\n    auto: true\n")
    loaded = load_settings(project_root=project_root)
    assert loaded.settings.session_title_auto is False
    (warning,) = loaded.warnings
    assert warning.key == "session.title.auto"
    assert warning.outcome.reason is CoerceReason.LOOSENS_USER_BASELINE


def test_a_free_key_takes_any_value_the_coercer_accepts(config_dir: Path, project_root: Path) -> None:
    freeze_process_env()
    _enable_gate(config_dir)
    _write_project_yaml(project_root, "context:\n  warn_threshold_pct: 0.9\n")

    loaded = load_settings(project_root=project_root)

    assert loaded.settings.warn_threshold_pct == 0.9
    assert loaded.source_for("context.warn_threshold_pct").layer is Source.PROJECT
    assert loaded.warnings == ()


def test_the_environment_does_not_move_the_project_fence(
    config_dir: Path, project_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The baseline is DEFAULT + USER, never the effective value: the shell's
    own loosening must not widen what a repository may ask for — and a project
    value a higher layer shadows anyway is still audited and still warns."""
    monkeypatch.setenv("CHRYS_MAX_TRANSIENT_RETRIES", "30")
    freeze_process_env()
    _enable_gate(config_dir)
    _write_project_yaml(project_root, "llm:\n  retry:\n    max_transient: 12\n")

    loaded = load_settings(project_root=project_root)

    assert loaded.settings.max_transient_retries == 30
    assert loaded.source_for("llm.retry.max_transient").layer is Source.ENV
    (warning,) = loaded.warnings
    assert warning.origin.layer is Source.PROJECT
    assert warning.outcome.reason is CoerceReason.LOOSENS_USER_BASELINE


def test_null_and_blank_project_values_are_absences_not_values(config_dir: Path, project_root: Path) -> None:
    """The written-``null`` reading belongs to the document the panel writes;
    nothing writes these files, so ``null`` and ``""`` say nothing here."""
    freeze_process_env()
    _enable_gate(config_dir)
    _write_project_yaml(
        project_root,
        "llm:\n  retry:\n    max_transient: null\nsession:\n  title:\n    auto: ''\n",
    )

    loaded = load_settings(project_root=project_root)

    assert loaded.settings.max_transient_retries is None
    assert loaded.source_for("llm.retry.max_transient").layer is Source.DEFAULT
    assert loaded.settings.session_title_auto is True
    assert loaded.warnings == ()


def test_a_wrongly_shaped_project_value_is_that_settings_invalid_value(config_dir: Path, project_root: Path) -> None:
    freeze_process_env()
    _enable_gate(config_dir)
    # A mapping under a known leaf, and a bool where an int belongs.
    _write_project_yaml(
        project_root,
        "llm:\n  retry:\n    max_transient:\n      nested: 1\ntools:\n  result:\n    ceiling_tokens: true\n",
    )

    loaded = load_settings(project_root=project_root)

    reasons = {warning.key: warning.outcome.reason for warning in loaded.warnings}
    assert reasons == {
        "llm.retry.max_transient": CoerceReason.EXPECTED_INT,
        "tools.result.ceiling_tokens": CoerceReason.EXPECTED_INT,
    }
    assert loaded.settings.max_transient_retries is None
    assert loaded.settings.tool_result_ceiling_tokens == Settings().tool_result_ceiling_tokens


# ── invalid project values and the SAFE_DEFAULT backstop ─────────────


def test_an_invalid_project_value_on_a_dangerous_key_seals_the_built_in_default(
    config_dir: Path, project_root: Path
) -> None:
    """The ceiling declares ``SAFE_DEFAULT`` so a rejected value cannot uncap
    the backstop — including a *project's* rejected value. Falling through to
    the user's ``0`` here would let a repository's garbage remove the cap."""
    freeze_process_env()
    _write_user_yaml(config_dir, "project:\n  config_enabled: true\ntools:\n  result:\n    ceiling_tokens: 0\n")
    _write_project_yaml(project_root, "tools:\n  result:\n    ceiling_tokens: -1\n")

    loaded = load_settings(project_root=project_root)

    assert loaded.settings.tool_result_ceiling_tokens == Settings().tool_result_ceiling_tokens
    assert loaded.source_for("tools.result.ceiling_tokens").layer is Source.DEFAULT
    assert "tools.result.ceiling_tokens" in loaded.sealed_keys
    # Phase one owns the project warning; the seal must not report it twice.
    (warning,) = loaded.warnings
    assert warning.origin.layer is Source.PROJECT
    assert warning.outcome.reason is CoerceReason.EXPECTED_NON_NEGATIVE_INT


def test_a_policy_rejected_project_value_does_not_seal(config_dir: Path, project_root: Path) -> None:
    """DENY and LOOSENS mean the project has no standing for the key — mute,
    not garbage. Sealing on them would let any repository reset a user's own
    dangerous-key choice to the default just by writing the key at all."""
    freeze_process_env()
    _write_user_yaml(config_dir, "project:\n  config_enabled: true\ntools:\n  result:\n    ceiling_tokens: 3000\n")

    # A loosening value on a whitelisted dangerous key: refused, user wins.
    _write_project_yaml(project_root, "tools:\n  result:\n    ceiling_tokens: 999999\n")
    loaded = load_settings(project_root=project_root)
    assert loaded.settings.tool_result_ceiling_tokens == 3000
    assert loaded.source_for("tools.result.ceiling_tokens").layer is Source.USER
    assert loaded.sealed_keys == frozenset()

    # A denied dangerous key: refused before any value question is asked.
    _write_project_yaml(project_root, "log:\n  raw_http_capture: banana\n")
    loaded = load_settings(project_root=project_root)
    assert loaded.settings.raw_http_capture is False
    assert loaded.sealed_keys == frozenset()
    (warning,) = loaded.warnings
    assert warning.outcome.reason is CoerceReason.NOT_ALLOWED_IN_PROJECT


def test_a_shadowed_invalid_project_value_still_warns_but_cannot_seal(
    config_dir: Path, project_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The walk consults layers highest first: a valid env value answers the
    key before the project's garbage is reached, so the seal never fires —
    while the phase-one audit still reports what the repository wrote."""
    monkeypatch.setenv("CHRYS_TOOL_RESULT_CEILING_TOKENS", "5000")
    freeze_process_env()
    _enable_gate(config_dir)
    _write_project_yaml(project_root, "tools:\n  result:\n    ceiling_tokens: -1\n")

    loaded = load_settings(project_root=project_root)

    assert loaded.settings.tool_result_ceiling_tokens == 5000
    assert loaded.source_for("tools.result.ceiling_tokens").layer is Source.ENV
    assert loaded.sealed_keys == frozenset()
    (warning,) = loaded.warnings
    assert warning.origin.layer is Source.PROJECT
    assert warning.outcome.reason is CoerceReason.EXPECTED_NON_NEGATIVE_INT


# ── reading the file ─────────────────────────────────────────────────


def test_a_hermetic_load_ignores_the_project_root(config_dir: Path, project_root: Path) -> None:
    freeze_process_env()
    _enable_gate(config_dir)
    _write_project_yaml(project_root, "session:\n  title:\n    auto: false\n")

    loaded = load_settings(env={}, project_root=project_root)

    assert loaded.settings.session_title_auto is True
    assert loaded.source_for("session.title.auto").layer is Source.DEFAULT
    assert loaded.dormant_project == ()


def test_an_unparseable_project_file_loads_as_empty(config_dir: Path, project_root: Path) -> None:
    freeze_process_env()
    _enable_gate(config_dir)
    _write_project_yaml(project_root, "just: [broken\n")

    loaded = load_settings(project_root=project_root)

    assert loaded.warnings == ()
    assert loaded.dormant_project == ()


def test_reading_a_project_file_leaves_no_droppings_in_the_repository(config_dir: Path, project_root: Path) -> None:
    """The file lives in someone's working tree: no lock sidecar, no backup,
    no repair — a settings load must not dirty their ``git status``."""
    freeze_process_env()
    _enable_gate(config_dir)
    path = _write_project_yaml(project_root, "session:\n  title:\n    auto: false\n")

    load_settings(project_root=project_root)

    assert list((project_root / ".chrys").iterdir()) == [path]


# ── project hooks and skills ─────────────────────────────────────────


def _write_project_hooks(root: Path) -> Path:
    (root / ".chrys" / "hooks").mkdir(parents=True, exist_ok=True)
    path = root / ".chrys" / "hooks" / "hooks.yml"
    path.write_text("hooks: []\n", encoding="utf-8")
    return path


def _write_project_skill(root: Path) -> Path:
    skills = root / ".agents" / "skills"
    (skills / "review").mkdir(parents=True)
    (skills / "review" / "SKILL.md").write_text("---\nname: review\ndescription: d\n---\n", encoding="utf-8")
    return skills


def test_project_hooks_and_skills_are_off_by_default_and_reported(config_dir: Path, project_root: Path) -> None:
    freeze_process_env()
    hooks = _write_project_hooks(project_root)
    skills = _write_project_skill(project_root)

    loaded = load_settings(project_root=project_root)

    assert loaded.settings.project_hooks_enabled is False
    assert loaded.settings.project_skills_enabled is False
    assert [(source.key, source.path) for source in loaded.dormant_project_sources] == [
        ("project.hooks_enabled", hooks),
        ("project.skills_enabled", skills),
    ]


def test_enabled_project_hooks_and_skills_are_not_reported(config_dir: Path, project_root: Path) -> None:
    freeze_process_env()
    _write_user_yaml(config_dir, "project:\n  hooks_enabled: true\n  skills_enabled: true\n")
    _write_project_hooks(project_root)
    _write_project_skill(project_root)

    loaded = load_settings(project_root=project_root)

    assert loaded.settings.project_hooks_enabled is True
    assert loaded.settings.project_skills_enabled is True
    assert loaded.dormant_project_sources == ()


def test_a_project_cannot_turn_its_own_hooks_or_skills_on(config_dir: Path, project_root: Path) -> None:
    freeze_process_env()
    _enable_gate(config_dir)
    _write_project_yaml(project_root, "project:\n  hooks_enabled: true\n  skills_enabled: true\n")

    loaded = load_settings(project_root=project_root)

    assert loaded.settings.project_hooks_enabled is False
    assert loaded.settings.project_skills_enabled is False


@pytest.mark.parametrize(
    ("skill_file", "reported"),
    [
        ("SKILL.md", True),
        ("review/SKILL.md", True),
        ("team/review/SKILL.md", True),
        ("team/group/review/SKILL.md", False),
        ("review/notes.md", False),
    ],
)
def test_the_skills_folder_is_reported_only_when_discovery_would_find_a_skill(
    config_dir: Path, project_root: Path, skill_file: str, reported: bool
) -> None:
    """Skill discovery looks for SKILL.md in the folder and two levels below it, and so does the notice."""
    freeze_process_env()
    skills = project_root / ".agents" / "skills"
    (skills / skill_file).parent.mkdir(parents=True, exist_ok=True)
    (skills / skill_file).write_text("---\nname: review\ndescription: d\n---\n", encoding="utf-8")

    sources = load_settings(project_root=project_root).dormant_project_sources

    assert [(source.key, source.path) for source in sources] == (
        [("project.skills_enabled", skills)] if reported else []
    )


@pytest.mark.parametrize("spelling", ["home", "HOME"])
def test_the_users_own_hooks_and_skills_are_not_reported_when_the_workspace_is_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, spelling: str
) -> None:
    home = tmp_path / "home"
    config_dir = home / ".chrys"
    fake = dataclasses.replace(platform_mod.get_platform(), config_dir=config_dir, data_dir=config_dir)
    monkeypatch.setattr(platform_mod, "get_platform", lambda: fake)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    freeze_process_env()
    _write_project_hooks(home)
    _write_project_skill(home)
    workspace = tmp_path / spelling
    if not workspace.exists():
        pytest.skip("Case-sensitive filesystem: the other spelling is another folder")

    assert load_settings(project_root=workspace).dormant_project_sources == ()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
def test_a_folder_that_cannot_be_listed_does_not_hide_the_project_skills(
    config_dir: Path, project_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    freeze_process_env()
    skills = _write_project_skill(project_root)
    locked = skills / "locked"
    locked.mkdir()
    list_folder = Path.iterdir

    def locked_first(self: Path) -> Iterator[Path]:
        return iter(sorted(list_folder(self), key=lambda child: child.name != "locked"))

    monkeypatch.setattr(Path, "iterdir", locked_first)
    locked.chmod(0)
    try:
        if os.access(locked, os.R_OK):
            pytest.skip("Permission bits do not apply to this user")
        sources = load_settings(project_root=project_root).dormant_project_sources
    finally:
        locked.chmod(0o755)

    assert [(source.key, source.path) for source in sources] == [("project.skills_enabled", skills)]


def test_a_hermetic_load_reports_no_project_hooks_or_skills(config_dir: Path, project_root: Path) -> None:
    freeze_process_env()
    _write_project_hooks(project_root)
    _write_project_skill(project_root)

    assert load_settings(env={}, project_root=project_root).dormant_project_sources == ()
