# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Offline distribution artifact guards."""

from __future__ import annotations

import gettext
import io
import subprocess
from pathlib import Path
from zipfile import ZipFile

import pytest
from scripts import i18n_mo


@pytest.fixture(scope="module")
def built_wheel(tmp_path_factory: pytest.TempPathFactory) -> Path:
    repository = Path(__file__).resolve().parents[2]
    output = tmp_path_factory.mktemp("wheel-build") / "artifacts"
    completed = subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(output)],
        cwd=repository,
        check=False,
        capture_output=True,
        text=True,
        timeout=50,
    )
    assert completed.returncode == 0, completed.stderr
    wheels = sorted(output.glob("*.whl"))
    assert len(wheels) == 1
    return wheels[0]


def test_wheel_contains_the_simplified_chinese_catalog_compiled_from_the_po(built_wheel: Path, tmp_path: Path) -> None:
    # The MO is not tracked: the build hook (hatch_build.py) compiles it from
    # the PO, so the wheel must carry exactly that compiler's output.
    with ZipFile(built_wheel) as wheel:
        assert wheel.namelist().count(i18n_mo.PACKAGE_MO_PATH) == 1
        catalog_bytes = wheel.read(i18n_mo.PACKAGE_MO_PATH)
    expected = tmp_path / "chrys.mo"
    i18n_mo.compile_mo(Path(__file__).resolve().parents[2] / i18n_mo.PO_PATH, expected)
    assert catalog_bytes == expected.read_bytes()

    translations = gettext.GNUTranslations(io.BytesIO(catalog_bytes))
    assert translations.gettext("missing.key") == "missing.key"
    assert translations.gettext("tui.theme_editor.cancel") == "取消"
    assert translations.info()["plural-forms"] == "nplurals=1; plural=0;"
