# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for installing the bundled security guard hooks.

Migrated from the 0.28.0 branch ``tests/app/test_guard_install.py``; the two
startup-wiring cases moved to ``test_anchors.py`` (the wiring now lives inside
``bootstrap_runtime``, not in the CLI dispatcher).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import yaml

from chrys.kernel.aixcoding.guard import guard_install
from chrys.kernel.aixcoding.guard.guard_install import (
    GUARD_PAYLOAD_FILES,
    GUARD_SUBDIR,
    MANAGED_HOOK_IDS,
    install_guard_hooks,
    installed_guard_hooks,
)


def _hook_ids(hooks_file: Path) -> list[str]:
    document = yaml.safe_load(hooks_file.read_text(encoding="utf-8-sig"))
    return [str(entry["id"]) for entry in document["hooks"]]


def test_fresh_install_writes_template_and_payload(tmp_path: Path) -> None:
    hooks_dir = tmp_path / "hooks"

    report = install_guard_hooks(hooks_dir=hooks_dir)

    assert report.merged_into_existing is False
    assert report.backup is None
    assert report.hooks_file == hooks_dir / "hooks.yaml"
    assert _hook_ids(report.hooks_file) == list(MANAGED_HOOK_IDS)
    for name in GUARD_PAYLOAD_FILES:
        assert (hooks_dir / GUARD_SUBDIR / name).is_file()
    assert installed_guard_hooks(hooks_dir) == (True, [])


def test_reinstall_is_idempotent_and_overwrites_payload(tmp_path: Path) -> None:
    hooks_dir = tmp_path / "hooks"
    install_guard_hooks(hooks_dir=hooks_dir)
    first = yaml.safe_load((hooks_dir / "hooks.yaml").read_text(encoding="utf-8"))
    stale = hooks_dir / GUARD_SUBDIR / "rules.json"
    stale.write_text("{}", encoding="utf-8")

    install_guard_hooks(hooks_dir=hooks_dir)
    install_guard_hooks(hooks_dir=hooks_dir)

    reloaded = yaml.safe_load((hooks_dir / "hooks.yaml").read_text(encoding="utf-8"))
    assert reloaded["hooks"] == first["hooks"]
    assert stale.read_text(encoding="utf-8") != "{}"


def test_existing_hooks_and_settings_survive_merge(tmp_path: Path) -> None:
    hooks_dir = tmp_path / "hooks"
    hooks_dir.mkdir(parents=True)
    (hooks_dir / "hooks.yaml").write_text(
        "# hand written\n"
        "version: 1\n"
        "settings:\n"
        "  shutdown_grace_seconds: 5\n"
        "  custom_setting: keep\n"
        "hooks:\n"
        "  - id: telemetry\n"
        "    event: after_turn\n"
        "    run:\n"
        "      type: command\n"
        "      argv: [echo, hi]\n",
        encoding="utf-8",
    )

    report = install_guard_hooks(hooks_dir=hooks_dir)

    text = report.hooks_file.read_text(encoding="utf-8")
    document = yaml.safe_load(text)
    assert report.merged_into_existing is True
    assert report.backup is not None and report.backup.is_file()
    assert "# hand written" in text
    # Template-managed settings keys follow the shipped pack; unknown keys survive.
    assert document["settings"]["shutdown_grace_seconds"] == 30
    assert document["settings"]["max_parallel_hooks"] == 4
    assert document["settings"]["custom_setting"] == "keep"
    assert document["hooks"][0]["id"] == "telemetry"
    assert [entry["id"] for entry in document["hooks"]] == ["telemetry", *MANAGED_HOOK_IDS]


def test_stale_setting_value_and_comment_are_synced_in_place(tmp_path: Path) -> None:
    hooks_dir = tmp_path / "hooks"
    hooks_dir.mkdir(parents=True)
    (hooks_dir / "hooks.yaml").write_text(
        "version: 1\n"
        "settings:\n"
        "  shutdown_grace_seconds: 5  # 旧值 注释应保留\n"
        "hooks:\n"
        "  - id: telemetry\n"
        "    event: after_turn\n"
        "    run:\n"
        "      type: command\n"
        "      argv: [echo, hi]\n",
        encoding="utf-8",
    )

    install_guard_hooks(hooks_dir=hooks_dir)

    text = (hooks_dir / "hooks.yaml").read_text(encoding="utf-8")
    document = yaml.safe_load(text)
    assert document["settings"]["shutdown_grace_seconds"] == 30
    assert "旧值 注释应保留" in text


def test_missing_settings_section_is_created_before_hooks(tmp_path: Path) -> None:
    hooks_dir = tmp_path / "hooks"
    hooks_dir.mkdir(parents=True)
    (hooks_dir / "hooks.yaml").write_text(
        "version: 1\n"
        "hooks:\n"
        "  - id: telemetry\n"
        "    event: after_turn\n"
        "    run:\n"
        "      type: command\n"
        "      argv: [echo, hi]\n",
        encoding="utf-8",
    )

    install_guard_hooks(hooks_dir=hooks_dir)

    document = yaml.safe_load((hooks_dir / "hooks.yaml").read_text(encoding="utf-8"))
    assert document["settings"]["shutdown_grace_seconds"] == 30
    assert document["settings"]["max_parallel_hooks"] == 4


def test_stale_guard_entries_are_replaced_not_duplicated(tmp_path: Path) -> None:
    hooks_dir = tmp_path / "hooks"
    hooks_dir.mkdir(parents=True)
    (hooks_dir / "hooks.yaml").write_text(
        "version: 1\n"
        "hooks:\n"
        "  - id: security-prompt-guard\n"
        "    event: user_prompt_submit\n"
        "    run:\n"
        "      type: script\n"
        "      path: old/prompt_guard.py\n"
        "  - id: telemetry\n"
        "    event: after_turn\n"
        "    run:\n"
        "      type: command\n"
        "      argv: [echo, hi]\n",
        encoding="utf-8",
    )

    install_guard_hooks(hooks_dir=hooks_dir)

    document = yaml.safe_load((hooks_dir / "hooks.yaml").read_text(encoding="utf-8"))
    runs = {entry["id"]: entry["run"] for entry in document["hooks"]}
    assert list(runs) == ["telemetry", *MANAGED_HOOK_IDS]
    if sys.platform == "win32":
        # Template entries are the PowerShell launcher form, merged verbatim on Windows.
        launcher_run = runs["security-prompt-guard"]
        assert launcher_run["type"] == "command"
        assert any("_launcher.ps1" in argument for argument in launcher_run["argv"])
        assert launcher_run["env"]["GUARD_SCRIPT"].endswith("_prompt_guard.py")
    else:
        # POSIX: the engine rewrites the launcher form back to type: script.
        assert runs["security-prompt-guard"] == {"type": "script", "path": "guard/_prompt_guard.py"}


def test_unparsable_configuration_is_backed_up_and_replaced(tmp_path: Path) -> None:
    hooks_dir = tmp_path / "hooks"
    hooks_dir.mkdir(parents=True)
    broken = hooks_dir / "hooks.yaml"
    broken.write_text("hooks: [unclosed\n", encoding="utf-8")

    report = install_guard_hooks(hooks_dir=hooks_dir)

    assert report.replaced_unparsable_config is True
    assert report.backup is not None
    assert report.backup.read_text(encoding="utf-8") == "hooks: [unclosed\n"
    assert _hook_ids(broken) == list(MANAGED_HOOK_IDS)


def test_configuration_without_hooks_section_gains_one(tmp_path: Path) -> None:
    hooks_dir = tmp_path / "hooks"
    hooks_dir.mkdir(parents=True)
    (hooks_dir / "hooks.yaml").write_text("version: 1\nsettings:\n  max_parallel_hooks: 3\n", encoding="utf-8")

    install_guard_hooks(hooks_dir=hooks_dir)

    document = yaml.safe_load((hooks_dir / "hooks.yaml").read_text(encoding="utf-8"))
    # max_parallel_hooks is template-managed and synced to the shipped value.
    assert document["settings"]["max_parallel_hooks"] == 4
    assert [entry["id"] for entry in document["hooks"]] == list(MANAGED_HOOK_IDS)


def test_flow_style_hooks_keep_unrelated_entries(tmp_path: Path) -> None:
    hooks_dir = tmp_path / "hooks"
    hooks_dir.mkdir(parents=True)
    (hooks_dir / "hooks.yaml").write_text(
        "version: 1\nhooks: [{id: telemetry, event: after_turn, run: {type: command, argv: [echo, hi]}}]\n",
        encoding="utf-8",
    )

    install_guard_hooks(hooks_dir=hooks_dir)

    document = yaml.safe_load((hooks_dir / "hooks.yaml").read_text(encoding="utf-8"))
    assert [entry["id"] for entry in document["hooks"]] == ["telemetry", *MANAGED_HOOK_IDS]


def test_flow_style_settings_keep_unrelated_keys(tmp_path: Path) -> None:
    hooks_dir = tmp_path / "hooks"
    hooks_dir.mkdir(parents=True)
    (hooks_dir / "hooks.yaml").write_text(
        "version: 1\n"
        "settings: {shutdown_grace_seconds: 5, custom_setting: keep}\n"
        "hooks: [{id: telemetry, event: after_turn, run: {type: command, argv: [echo, hi]}}]\n",
        encoding="utf-8",
    )

    report = install_guard_hooks(hooks_dir=hooks_dir)

    document = yaml.safe_load((hooks_dir / "hooks.yaml").read_text(encoding="utf-8"))
    assert report.replaced_unparsable_config is False
    assert document["settings"]["shutdown_grace_seconds"] == 30
    assert document["settings"]["max_parallel_hooks"] == 4
    assert document["settings"]["custom_setting"] == "keep"
    assert [entry["id"] for entry in document["hooks"]] == ["telemetry", *MANAGED_HOOK_IDS]


def test_missing_assets_are_reported(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        install_guard_hooks(hooks_dir=tmp_path / "hooks", asset_dir=tmp_path / "empty")


def test_installed_report_flags_missing_payload(tmp_path: Path) -> None:
    hooks_dir = tmp_path / "hooks"
    install_guard_hooks(hooks_dir=hooks_dir)
    (hooks_dir / GUARD_SUBDIR / "rules.json").unlink()

    listed, missing = installed_guard_hooks(hooks_dir)

    assert listed is True
    assert missing == [str(hooks_dir / GUARD_SUBDIR / "rules.json")]


def test_source_checkout_asset_dir_is_the_packaged_rule_pack() -> None:
    """Source checkout and installed builds read the same packaged rule pack."""
    resolved = guard_install.resolve_asset_dir()

    assert (resolved / "hooks.yaml").is_file()
    assert resolved == guard_install.GUARD_ASSET_DIR
    assert resolved.name == "pack"


def test_switch_state_survives_a_reinstall(tmp_path: Path) -> None:
    """``guard_state.json`` holds the user's switch positions, not shipped content.

    Every startup reinstalls the rule pack, which overwrites ``rules.json``; the
    switch file must stay untouched or the user's master switch and per-rule
    switches would silently reset on the next launch.
    """
    hooks_dir = tmp_path / "hooks"
    install_guard_hooks(hooks_dir=hooks_dir)
    state_file = hooks_dir / GUARD_SUBDIR / "guard_state.json"
    state = {"version": 1, "enabled": False, "rules": {"IN-07": False}}
    state_file.write_text(json.dumps(state), encoding="utf-8")

    install_guard_hooks(hooks_dir=hooks_dir)

    assert json.loads(state_file.read_text(encoding="utf-8")) == state


def test_switch_manager_is_installed_next_to_the_rules(tmp_path: Path) -> None:
    """``guardctl`` only works beside ``rules.json`` (it edits the state file)."""
    hooks_dir = tmp_path / "hooks"

    install_guard_hooks(hooks_dir=hooks_dir)

    assert (hooks_dir / GUARD_SUBDIR / "guardctl.py").is_file()
    assert (hooks_dir / GUARD_SUBDIR / "guardctl.cmd").is_file()
    assert (hooks_dir / GUARD_SUBDIR / "guardctl.sh").is_file()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_posix_switch_entry_point_is_installed_executable(tmp_path: Path) -> None:
    """``guardctl.sh`` must be runnable straight after the install."""
    hooks_dir = tmp_path / "hooks"

    install_guard_hooks(hooks_dir=hooks_dir)

    mode = (hooks_dir / GUARD_SUBDIR / "guardctl.sh").stat().st_mode
    assert mode & 0o111, "guardctl.sh must be executable after install"
