# ruff: noqa: RUF001, RUF002
"""csas 报文组装层：reporter 只给业务字段，公共字段在此统一补齐。

对齐 aixcoding ``buildToolDetailSaveItem`` 的收口语义（方案 §4.2）：
sessionId 之外的身份/环境字段（userId、git 五件套、channel 三元组、
pluginVersion、projectName）由本层自动附加，端点 reporter 不重复组装。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

BOUNDED_MEMORY_LIMIT = 1024
"""有界记忆表容量：长驻进程防泄漏（取消等无终态的残留也会被淘汰）。"""


def remember_bounded(mapping: dict, key: str, value: Any, *, limit: int = BOUNDED_MEMORY_LIMIT) -> None:
    """写入并维持容量上限（超限丢最旧，按插入序）。"""
    mapping[key] = value
    while len(mapping) > limit:
        mapping.pop(next(iter(mapping)))


def common_fields(workspace_cwd: str | None = None) -> dict[str, Any]:
    """csas 报文公共字段（channel/git/plugin/project/userId）。

    ``workspace_cwd``：会话工作区（事件携带，``SessionEnvironment.cwd`` 同源）——
    projectName / git 五件套的取值基；缺省回退进程 cwd（2026-10-09 修正，
    对齐 aixcoding workspace 语义）。
    """
    from chrys.aixcoding.config import load_settings
    from chrys.aixcoding.context import current_channel, plugin_version

    fields: dict[str, Any] = {"pluginVersion": plugin_version()}
    settings = load_settings()
    if settings.user_id:
        fields["userId"] = settings.user_id

    channel = current_channel()
    fields["channelType"] = channel.channel_type
    fields["channelName"] = channel.channel_name
    if channel.channel_version:
        fields["channelVersion"] = channel.channel_version

    cwd = Path(workspace_cwd) if workspace_cwd else Path.cwd()
    fields["projectName"] = cwd.name or str(cwd)

    from chrys.aixcoding.git_info import collect_git_info

    git = collect_git_info(cwd)
    if git is not None:
        fields.update(
            {
                key: value
                for key, value in (
                    ("gitRemote", git.git_remote),
                    ("gitBranch", git.git_branch),
                    ("gitRevision", git.git_revision),
                    ("gitOwner", git.git_owner),
                    ("gitRepo", git.git_repo),
                )
                if value
            }
        )
    return fields
