# ruff: noqa: RUF001, RUF002, RUF003
"""git 五件套采集（gitRemote/gitBranch/gitRevision/gitOwner/gitRepo）。

subprocess git + 按 cwd 的 TTL 缓存 + 失败静默返回 None（对齐 aixcoding
``gitRepoInfo`` 与 pi-acp ``git-context`` 的容错语义）；remote 选取优先级
``cnb > origin > 第一个``（对齐 aixcoding）。字段名即 csas 报文字段名。
"""

from __future__ import annotations

import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from chrys.foundation.platform.process import _windows_hidden_subprocess_kwargs

REMOTE_PRIORITY = ("cnb", "origin")

_GIT_TIMEOUT_SECONDS = 5.0
_DEFAULT_TTL_SECONDS = 300.0


@dataclass(frozen=True)
class GitInfo:
    git_remote: str | None
    git_branch: str | None
    git_revision: str | None
    git_owner: str | None
    git_repo: str | None
    git_user_name: str | None = None
    """本仓库 git config user.name（ai-code/save 的 gitUserName）。"""
    git_user_email: str | None = None
    """本仓库 git config user.email（ai-code/save 的 gitUserEmail）。"""


_cache_lock = threading.Lock()
_cache: dict[Path, tuple[float, GitInfo | None]] = {}


def clear_git_info_cache() -> None:
    """清缓存（测试辅助）。"""
    with _cache_lock:
        _cache.clear()


def collect_git_info(
    cwd: Path | str,
    *,
    ttl_seconds: float = _DEFAULT_TTL_SECONDS,
    force: bool = False,
) -> GitInfo | None:
    """采集 ``cwd`` 所在仓库的 git 五件套；非 git 目录/执行失败返回 ``None``。"""
    key = Path(cwd).resolve()
    now = time.monotonic()
    with _cache_lock:
        cached = _cache.get(key)
    if cached is not None and not force and now - cached[0] < ttl_seconds:
        return cached[1]
    info = _collect(key)
    with _cache_lock:
        _cache[key] = (now, info)
    return info


def _run_git(cwd: Path, args: list[str]) -> str | None:
    git = shutil.which("git")
    if git is None:
        return None
    try:
        proc = subprocess.run(  # noqa: S603
            [git, *args],
            cwd=str(cwd),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            check=False,
            **_windows_hidden_subprocess_kwargs(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.decode("utf-8", errors="replace").strip() or None


def _collect(cwd: Path) -> GitInfo | None:
    branch = _run_git(cwd, ["rev-parse", "--abbrev-ref", "HEAD"])
    revision = _run_git(cwd, ["rev-parse", "HEAD"])
    # detached HEAD 时 --abbrev-ref 返回字面量 "HEAD"，无分支语义
    branch = None if branch == "HEAD" else branch
    if branch is None and revision is None:
        return None

    remote = None
    owner = None
    repo = None
    names_text = _run_git(cwd, ["remote"])
    if names_text:
        names = [name.strip() for name in names_text.splitlines() if name.strip()]
        chosen = next((name for name in REMOTE_PRIORITY if name in names), names[0])
        remote = _run_git(cwd, ["remote", "get-url", chosen])
        if remote:
            owner, repo = _parse_owner_repo(remote)

    return GitInfo(
        git_remote=remote,
        git_branch=branch,
        git_revision=revision,
        git_owner=owner,
        git_repo=repo,
        git_user_name=_run_git(cwd, ["config", "user.name"]),
        git_user_email=_run_git(cwd, ["config", "user.email"]),
    )


def _parse_owner_repo(url: str) -> tuple[str | None, str | None]:
    """从 remote URL 解析 owner/repo（https 与 scp-like 两种形态）。"""
    cleaned = url.removesuffix(".git")
    if cleaned.startswith(("http://", "https://")):
        parts = [part for part in cleaned.split("/") if part]
        if len(parts) >= 3:
            return parts[-2], parts[-1]
    elif ":" in cleaned:
        tail = cleaned.split(":", 1)[1]
        parts = [part for part in tail.split("/") if part]
        if len(parts) >= 2:
            return parts[-2], parts[-1]
    return None, None
