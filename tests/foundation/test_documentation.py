# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Shared documentation resolution across checkout and installed packages."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from chrys.foundation import documentation as docs_module
from chrys.foundation.documentation import BUNDLED_DOCS_PATH, resolve_docs_root
from tests.support.ci import CI_LINUX_ONLY

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _write(docs_root: Path, content: str = "locales: [en]\ntopics: []\n") -> Path:
    docs_root.mkdir(parents=True, exist_ok=True)
    (docs_root / "index.yaml").write_text(content, encoding="utf-8")
    return docs_root


def test_resolve_docs_root_prefers_env_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    docs = _write(tmp_path / "docs")
    monkeypatch.setenv("CHRYS_DOCS_ROOT", str(docs))

    assert resolve_docs_root() == docs


def test_resolve_docs_root_reports_an_override_without_index(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A wrong override shows the guide's missing-docs state, not another copy."""
    monkeypatch.setenv("CHRYS_DOCS_ROOT", str(tmp_path / "does-not-exist"))

    assert resolve_docs_root() is None


def test_resolve_docs_root_finds_repository_docs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CHRYS_DOCS_ROOT", raising=False)

    assert resolve_docs_root() == _REPO_ROOT / "docs"


def _installed_module(site_packages: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the shared resolver at a fake installed package."""
    module_dir = site_packages / "chrys" / "foundation"
    module_dir.mkdir(parents=True)
    monkeypatch.setattr(docs_module, "__file__", str(module_dir / "documentation.py"))
    monkeypatch.delenv("CHRYS_DOCS_ROOT", raising=False)
    return module_dir


def test_resolve_docs_root_prefers_the_copy_bundled_in_the_wheel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module_dir = _installed_module(tmp_path / "site-packages", monkeypatch)
    # A checkout-shaped docs tree around the install loses to the bundled copy.
    _write(tmp_path / "docs")
    (tmp_path / "pyproject.toml").write_text("", encoding="utf-8")
    assert resolve_docs_root() == tmp_path / "docs"

    bundled = _write(module_dir.parent / BUNDLED_DOCS_PATH)

    assert resolve_docs_root() == bundled


def test_resolve_docs_root_ignores_docs_outside_a_checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _installed_module(tmp_path / "site-packages", monkeypatch)
    _write(tmp_path / "docs")

    assert resolve_docs_root() is None


@CI_LINUX_ONLY
def test_wheel_bundles_docs_where_the_resolver_looks() -> None:
    pyproject = tomllib.loads((_REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    force_include = pyproject["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"]
    # AIxCoding manual bundle: source key follows the fork-owned manual tree (user-manual C147)
    assert force_include["aixcoding/manual"] == (Path("chrys") / BUNDLED_DOCS_PATH).as_posix()


def test_relative_override_uses_current_working_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    docs = _write(tmp_path / "docs")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CHRYS_DOCS_ROOT", "docs")
    assert resolve_docs_root() == docs


@pytest.mark.parametrize("error", [RuntimeError("unknown user"), OSError("unreadable"), ValueError("invalid path")])
def test_invalid_override_does_not_break_resolution_or_fall_back(
    monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    def expanduser(path: Path) -> Path:
        raise error

    monkeypatch.setenv("CHRYS_DOCS_ROOT", "~invalid/docs")
    monkeypatch.setattr(Path, "expanduser", expanduser)
    assert resolve_docs_root() is None


def test_unknown_user_override_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHRYS_DOCS_ROOT", "~icode_nonexistent_documentation_user/docs")
    assert resolve_docs_root() is None
