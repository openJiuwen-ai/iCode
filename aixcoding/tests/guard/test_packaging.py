# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The wheel packages the guard rule pack from its kernel-tier home.

The runtime files live in ``src/chrys/kernel/aixcoding/guard/pack/`` — inside the
only package hatch ships (``packages = ["src/chrys"]``).  There is no second
checked-in copy and no force-include mapping: a root-level copy once drifted and
shipped stale rules (the 127.0.0.1 loopback mis-block), so a regression that
reintroduces one fails here.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from chrys.kernel.aixcoding.guard.guard_install import GUARD_PAYLOAD_FILES

REPO_ROOT = Path(__file__).resolve().parents[3]
GUARD_SOURCE_DIR = REPO_ROOT / "src" / "chrys" / "kernel" / "aixcoding" / "guard" / "pack"
PACK_TEMPLATE = "hooks.yaml"
PACK_PACKAGE_FILE = "__init__.py"
PACK_RUNTIME_FILES = (*GUARD_PAYLOAD_FILES, PACK_TEMPLATE)
PACK_DEV_ASSETS = {
    "guard_pack_selftest": REPO_ROOT / "aixcoding" / "tests" / "guard" / "guard_pack_selftest.py",
    "verify_guard_deployed": REPO_ROOT / "aixcoding" / "tests" / "guard" / "verify_guard_deployed.py",
    "manual_installer": REPO_ROOT / "aixcoding" / "tools" / "guard_install_merge.py",
    "README": REPO_ROOT / "aixcoding" / "docs" / "batches" / "guard-hooks" / "rule-pack" / "README.md",
    "OPERATIONS": REPO_ROOT / "aixcoding" / "docs" / "batches" / "guard-hooks" / "rule-pack" / "OPERATIONS.md",
    "PORTING": REPO_ROOT / "aixcoding" / "docs" / "batches" / "guard-hooks" / "rule-pack" / "PORTING.md",
    "MANUAL_TESTS": REPO_ROOT / "aixcoding" / "docs" / "batches" / "guard-hooks" / "rule-pack" / "MANUAL_TESTS.md",
}


def _pack_files() -> set[str]:
    return {path.name for path in GUARD_SOURCE_DIR.iterdir() if path.is_file() and not path.name.startswith(".")}


def test_runtime_pack_is_complete_and_fully_classified() -> None:
    expected = {*PACK_RUNTIME_FILES, PACK_PACKAGE_FILE}
    assert _pack_files() == expected, (
        "every file in the packaged rule-pack directory must be declared runtime content "
        f"(expected {sorted(expected)}, found {sorted(_pack_files())})"
    )
    for name in PACK_RUNTIME_FILES:
        assert (GUARD_SOURCE_DIR / name).is_file(), f"rule pack file missing: {GUARD_SOURCE_DIR / name}"


def test_operator_assets_live_in_their_customization_directories() -> None:
    for label, path in PACK_DEV_ASSETS.items():
        assert path.is_file(), f"{label} asset missing: {path}"


def test_no_second_rule_pack_copy_exists() -> None:
    """A repo-root ``guard_hooks/`` copy is the historical drift source.

    A residual directory holding only local caches cannot shadow the packaged
    copy, so only rule-pack files count as a reintroduced second copy.
    """
    stale_dir = REPO_ROOT / "guard_hooks"
    leftovers = [name for name in PACK_RUNTIME_FILES if (stale_dir / name).is_file()]
    assert not leftovers, f"the root guard_hooks/ copy must not return: {leftovers}"


def test_hatch_packages_the_pack_without_a_force_include_mapping() -> None:
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    wheel = pyproject["tool"]["hatch"]["build"]["targets"]["wheel"]
    force_include = wheel.get("force-include", {})

    assert wheel["packages"] == ["src/chrys"]
    assert not any(source.startswith("guard_hooks/") for source in force_include)
