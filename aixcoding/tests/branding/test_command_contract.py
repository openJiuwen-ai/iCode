# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Static contract for the C4 command rename chain (wheel -> build -> CD -> install).

Guards every hop of the new-name chain plus the names that must stay unchanged
(distribution identity, internal chrys names), so no single hop can drift.
Behavioral migration is covered by the upstream installer/uninstaller tests,
not by these static checks.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]


def _read(relative: str) -> str:
    return (REPO_ROOT / relative).read_text(encoding="utf-8")


def test_wheel_has_exactly_one_entry_point_and_keeps_distribution_identity() -> None:
    project = tomllib.loads(_read("pyproject.toml"))["project"]
    assert project["scripts"] == {"aixcoding-cli": "chrys.app.cli.app:main"}
    assert project["name"] == "iCode-TUI"


def test_check_wheel_probes_the_new_command_only() -> None:
    text = _read("scripts/check_wheel.py")
    assert 'COMMAND = "aixcoding-cli"' in text
    assert 'DISTRIBUTION_NAME = "iCode-TUI"' in text
    assert 'WHEEL_PREFIX = "icode_tui-"' in text


def test_build_scripts_emit_the_new_binary_and_keep_wheel_names() -> None:
    build_sh = _read("scripts/build.sh")
    assert 'OUTPUT="$PROJECT_ROOT/dist/aixcoding-cli"' in build_sh
    assert 'OUTPUT="$PROJECT_ROOT/dist/aixcoding-cli.exe"' in build_sh
    assert "icode_tui-" in build_sh
    build_ps1 = _read("scripts/build.ps1")
    assert '$Output = Join-Path $DistDir "aixcoding-cli.exe"' in build_ps1
    assert "icode_tui-" in build_ps1


def test_cd_release_chain_uses_the_new_name_at_every_hop() -> None:
    cd = _read(".github/workflows/cd.yml")
    for artifact in (
        "aixcoding-cli-linux-x86_64",
        "aixcoding-cli-linux-aarch64",
        "aixcoding-cli-macos-aarch64",
        "aixcoding-cli-macos-x86_64",
        "aixcoding-cli-windows-x86_64.exe",
        "aixcoding-cli-windows-aarch64.exe",
    ):
        assert f'artifact":"{artifact}"' in cd
    assert 'echo "binary=aixcoding-cli" >> "$GITHUB_OUTPUT"' in cd
    assert 'echo "binary=aixcoding-cli.exe" >> "$GITHUB_OUTPUT"' in cd
    assert "for dir in artifacts/aixcoding-cli-*/; do" in cd
    assert 'zip -q "$OLDPWD/release/${artifact}.zip" "aixcoding-cli.exe"' in cd
    assert 'tar -czf "release/${artifact}.tar.gz" -C "$dir" "aixcoding-cli"' in cd
    assert 'installed=$("$HOME/.local/bin/aixcoding-cli" --version)' in cd
    assert "'chrys\\bin\\aixcoding-cli.exe'" in cd
    # No stale hop of the old artifact prefix survives.
    assert '"artifact":"icode-' not in cd
    assert "artifacts/icode-*/" not in cd


def test_install_scripts_use_the_new_package_and_alias_names() -> None:
    install_sh = _read("scripts/install.sh")
    assert 'prefix="aixcoding-cli-$os-$arch-v"' in install_sh
    assert '"$tmp/package/aixcoding-cli" install' in install_sh
    assert "check_aixcoding_cli_command" in install_sh
    assert '"$bin_dir/aixcoding-cli"' in install_sh
    install_ps1 = _read("scripts/install.ps1")
    assert '$prefix = "aixcoding-cli-windows-$arch-v"' in install_ps1
    assert "Invoke-Install (Join-Path $packageDir 'aixcoding-cli.exe')" in install_ps1
    assert "Get-Command aixcoding-cli" in install_ps1
    assert "Join-Path $binDir 'aixcoding-cli.exe'" in install_ps1


def test_install_scripts_keep_distribution_and_internal_names() -> None:
    for relative in ("scripts/install.sh", "scripts/install.ps1"):
        text = _read(relative)
        assert "uv tool upgrade iCode-TUI" in text
        assert "openJiuwen-ai/iCode" in text
    assert "Join-Path $env:LOCALAPPDATA 'chrys\\bin'" in _read("scripts/install.ps1")
    assert 'bin_dir="$HOME/.local/bin"' in _read("scripts/install.sh")


@pytest.mark.parametrize(
    "relative",
    [
        "AGENTS.md",
        "aixcoding/mock/__main__.py",
        "aixcoding/mock/auth/server.py",
        "aixcoding/mock/auth/README.md",
        "aixcoding/mock/model_catalog/server.py",
        "aixcoding/mock/model_catalog/README.md",
        "aixcoding/mock/update/server.py",
        "aixcoding/docs/standards/UPGRADE-RUNBOOK.md",
    ],
)
def test_current_dev_recipes_never_use_the_removed_entry(relative: str) -> None:
    assert "uv run icode" not in _read(relative)


def test_agents_documents_the_new_entry() -> None:
    agents = _read("AGENTS.md")
    assert "uv run aixcoding-cli" in agents
    assert "whose only command is `aixcoding-cli`" in agents
