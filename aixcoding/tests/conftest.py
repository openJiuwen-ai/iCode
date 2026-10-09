# ruff: noqa: RUF002, S603
"""aixcoding 专区测试的公共设置（独立于上游 ``tests/`` 树）。

定制测试放仓库根 ``aixcoding/tests/``（不进 wheel、不侵入上游 ``tests/`` 目录白名单），
因此不继承 ``tests/conftest.py`` 的 autouse 隔离与 fixtures——这里自建最小等价物：

- 把 ``aixcoding/telemetry-mock/`` 加入 ``sys.path``：mock 的 ``server``/``store``/
  ``faults`` 以顶层模块名 import，factory 驱动、零子进程（故无需改 chrys_test.py）；
- config_dir 隔离：与 ``tests/conftest.py`` 同款机制（monkeypatch ``detect_platform``
  + ``get_platform.cache_clear()``），防止 ``config.py`` 测试读写真实的
  ``~/.chrys/aixcoding.yaml``；
- 清宿主机残留的 ``AIXCODING_*`` / ``CHRYS_TELEMETRY_MOCK_*`` 环境变量，避免污染
  profile 判定（``monkeypatch`` 在测试结束后自动还原）；
- ``git_repo``：一次性小型 git 仓库（上游 ``git_template_repo`` 的单测试简化版，
  供 ``git_info`` 测试用）。

运行方式（pytest ``testpaths`` 只含 ``tests/``，本目录须显式指定）::

    uv run --extra all pytest aixcoding/tests -m "not integration and not gc_calibration"
"""

from __future__ import annotations

import dataclasses
import os
import shutil
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

_MOCK_DIR = Path(__file__).resolve().parents[1] / "telemetry-mock"
if str(_MOCK_DIR) not in sys.path:
    sys.path.insert(0, str(_MOCK_DIR))

_AMBIENT_ENV = (
    "AIXCODING_EXTENSION_PROFILE",
    "AIXCODING_EXTENSION_BASE_URL",
    "AIXCODING_TOKEN",
    "AIXCODING_TELEMETRY_DISABLED",
    "CHRYS_TELEMETRY_MOCK_PORT",
    "CHRYS_TELEMETRY_MOCK_TOKEN",
)


@pytest.fixture(autouse=True)
def _isolated_config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """把 ``get_platform().config_dir/data_dir`` 指向本测试的临时目录。

    ``get_platform`` 是 ``functools.cache`` 的，且全代码库以 ``from ... import`` 持有
    同一函数对象——与 ``tests/conftest.py`` 相同，经 ``detect_platform`` 补丁 +
    缓存清空让所有持有者一并重定向。teardown 先撤销补丁再重填缓存，避免 pinned
    值泄漏到后续测试。
    """
    from chrys.foundation import platform as platform_mod
    from chrys.foundation.platform import get_platform

    config_dir = tmp_path / "platform-config"
    pinned = dataclasses.replace(platform_mod.detect_platform(), config_dir=config_dir, data_dir=config_dir)
    monkeypatch.setattr(platform_mod, "detect_platform", lambda: pinned)
    get_platform.cache_clear()
    for name in _AMBIENT_ENV:
        monkeypatch.delenv(name, raising=False)
    yield config_dir
    monkeypatch.undo()
    get_platform.cache_clear()
    get_platform()


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """建一个带单次提交的最小 git 仓库（隔离全局/系统 git 配置）。"""
    git = shutil.which("git")
    if git is None:
        pytest.skip("git is required for repository-backed tests")
    repo = tmp_path / "repo"
    repo.mkdir()
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}

    def run_git(*args: str) -> None:
        subprocess.run(
            [git, *args],
            cwd=repo,
            env=env,
            check=True,
            capture_output=True,
            stdin=subprocess.DEVNULL,
        )

    run_git("init", "-q")
    run_git("config", "user.name", "aixcoding tests")
    run_git("config", "user.email", "tests@aixcoding.local")
    (repo / "README.md").write_text("committed content", encoding="utf-8")
    run_git("add", "README.md")
    run_git("commit", "-q", "-m", "init")
    return repo
