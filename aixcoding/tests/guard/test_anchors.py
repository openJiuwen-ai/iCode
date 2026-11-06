# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Behaviour anchors for the guard migration (DEVELOPMENT.md §5).

Each case really invokes the upstream symbol it depends on — ``get_platform``,
the hooks loader, ``bootstrap_runtime``, the build scripts' PyApp project name —
and never mocks the anchor itself. The bootstrap cases run in a subprocess so
the temporary config directory gives complete isolation (no process-wide
settings or env snapshots leak into the test session).
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import textwrap
from pathlib import Path

from chrys.foundation.platform import conventional_config_dir, get_platform
from chrys.kernel.aixcoding.guard.guard_install import GUARD_ASSET_DIR, MANAGED_HOOK_IDS
from chrys.service.hooks.events import GATED_EVENTS, HookEvent
from chrys.service.hooks.loader import load_hooks_file

REPO_ROOT = Path(__file__).resolve().parents[3]
GUARD_ROOT = GUARD_ASSET_DIR

_BOOTSTRAP_SCRIPT = textwrap.dedent(
    """
    from chrys.orchestration.startup import bootstrap_runtime

    bootstrap_runtime(dotenv_override=False, apply_patches=False, setup_telemetry=False)
    print("BOOTSTRAP_OK")
    """
)

_BOOTSTRAP_SURVIVES_FAILURE_SCRIPT = textwrap.dedent(
    """
    import chrys.kernel.aixcoding.guard.guard_install as guard_install
    from chrys.orchestration.startup import bootstrap_runtime

    def _boom():
        raise RuntimeError("boom")

    guard_install.install_guard_hooks = _boom
    bootstrap_runtime(dotenv_override=False, apply_patches=False, setup_telemetry=False)
    print("BOOTSTRAP_SURVIVED")
    """
)


def _isolated_env(tmp_path: Path) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if not key.upper().startswith("CHRYS_")}
    env["HOME"] = str(tmp_path)
    env["USERPROFILE"] = str(tmp_path)
    env["APPDATA"] = str(tmp_path / "AppData" / "Roaming")
    return env


def _config_dir(tmp_path: Path) -> Path:
    if sys.platform == "win32":
        return tmp_path / "AppData" / "Roaming" / "chrys"
    return tmp_path / ".chrys"


def _run_bootstrap(script: str, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", script],
        env=_isolated_env(tmp_path),
        cwd=str(tmp_path),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )


def test_get_platform_provides_the_guard_config_dir() -> None:
    """Anchor: ``get_platform().config_dir`` (hooks load from ``config_dir/hooks``).

    ``tests/conftest.py`` pins ``config_dir`` to a session temp redirect, so the
    user-facing shape is asserted on ``conventional_config_dir`` instead.
    """
    info = get_platform()

    assert info.config_dir.is_absolute()
    # Windows: %APPDATA%\chrys — macOS / Linux: ~/.chrys
    assert conventional_config_dir().name in ("chrys", ".chrys")


def test_guard_hooks_yaml_loads_under_the_upstream_contract() -> None:
    """Anchor: the hooks schema/loader really parse the shipped template."""
    hooks_file = load_hooks_file(GUARD_ROOT / "hooks.yaml")

    assert [hook.id for hook in hooks_file.hooks] == list(MANAGED_HOOK_IDS)
    events = {hook.event for hook in hooks_file.hooks}
    assert events == {HookEvent.USER_PROMPT_SUBMIT, HookEvent.BEFORE_TOOL_CALL}
    assert events <= GATED_EVENTS
    for hook in hooks_file.hooks:
        assert hook.execution.mode == "blocking"
        assert hook.execution.on_error == "block"
        assert hook.run.type in {"command", "script"}
        assert hook.enabled is True


def test_bootstrap_installs_the_guard(tmp_path: Path) -> None:
    """Anchor: the C136 wiring — bootstrap really installs into the config dir."""
    completed = _run_bootstrap(_BOOTSTRAP_SCRIPT, tmp_path)

    assert completed.returncode == 0, completed.stderr[-2000:]
    assert "BOOTSTRAP_OK" in completed.stdout
    hooks_dir = _config_dir(tmp_path) / "hooks"
    assert (hooks_dir / "hooks.yaml").is_file()
    for name in ("rules.json", "_prompt_guard.py", "_tool_guard.py", "_guard_core.py", "_launcher.ps1"):
        assert (hooks_dir / "guard" / name).is_file(), name


def test_bootstrap_survives_install_failure(tmp_path: Path) -> None:
    """Fail-open: a broken installer must never take the runtime down."""
    completed = _run_bootstrap(_BOOTSTRAP_SURVIVES_FAILURE_SCRIPT, tmp_path)

    assert completed.returncode == 0, completed.stderr[-2000:]
    assert "BOOTSTRAP_SURVIVED" in completed.stdout


def test_pyapp_project_name_stays_chrys() -> None:
    """Anchor: the launcher's embedded-interpreter fallback path assumes this name."""
    build_ps1 = (REPO_ROOT / "scripts" / "build.ps1").read_text(encoding="utf-8")
    build_sh = (REPO_ROOT / "scripts" / "build.sh").read_text(encoding="utf-8")

    assert re.search(r'PYAPP_PROJECT_NAME\s*=\s*"chrys"', build_ps1)
    assert "PYAPP_PROJECT_NAME=chrys" in build_sh
