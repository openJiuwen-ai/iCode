# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""``browser_save_state`` - export cookies + localStorage to a named archive."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from typing import Any

from .._proc import windows_hidden_kwargs
from ..errors import BadRequest
from ..types import ToolBlock, text_block
from ._shared import eval_value, url_origin

NAME = "browser_save_state"

DESCRIPTION = (
    "Export the current origin's cookies (Network.getCookies) and localStorage into "
    "a named JSON archive written under the plugin's state directory (never the "
    "workspace). "
    "name must match ^[a-z0-9_-]{1,32}$; the file is mode 0600 and its directory "
    "0700 (on Windows, icacls tightens the ACL to the current user where available). "
    "Use this to back up / migrate a login state; there is no restore tool yet."
)

PARAMS: dict[str, Any] = {
    "name": {"type": "string", "required": True, "pattern": "^[a-z0-9_-]{1,32}$"},
}

_NAME_RE = re.compile(r"^[a-z0-9_-]{1,32}$")
_LS_EXPR = "JSON.stringify(Object.fromEntries(Object.entries(localStorage)))"


def _parse_storage(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, str) or not raw:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _harden_dir(directory: Any) -> str:
    try:
        os.chmod(directory, 0o700)
    except OSError:
        return "目录 0700（chmod 不受支持）"
    return "目录 0700"


def _icacls_restrict(path: Any) -> bool:
    """On Windows, drop inheritance and grant the current user full control."""
    user = os.environ.get("USERNAME") or os.environ.get("USER") or ""
    if not user:
        return False
    try:
        result = subprocess.run(
            ["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
            stdin=subprocess.DEVNULL,
            **windows_hidden_kwargs(),
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def _harden_file(path: Any) -> str:
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    if sys.platform == "win32":
        if _icacls_restrict(path):
            return "文件 0600（icacls 已收紧到当前用户）"
        return "文件 0600（icacls 不可用，仅 chmod）"
    return "文件 0600"


async def run(session: Any, arguments: dict[str, Any]) -> list[ToolBlock]:
    if not session.cfg.save_state_enabled:
        raise BadRequest("browser_save_state 未启用（ICODE_BROWSER_SAVE_STATE=allow 才注册）")
    name = arguments["name"]
    if not _NAME_RE.fullmatch(name or ""):
        raise BadRequest(f"name 非法（应匹配 ^[a-z0-9_-]{{1,32}}$）：{name!r}")

    client = session.require_client()
    storage = _parse_storage(await eval_value(session, _LS_EXPR))
    origin = str(await eval_value(session, "location.origin") or "") or url_origin(session.snapshot.url)
    if not origin or origin == "null":
        raise BadRequest("无法确定当前 origin，拒绝导出登录态")
    cookies_response = await client.send(
        "Network.getCookies",
        {"urls": [origin]},
        session_id=session.session_id,
        timeout=session.cfg.cdp_timeout,
    )
    cookies = list(cookies_response.get("cookies") or [])

    payload = {
        "version": 1,
        "saved_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "cookies": cookies,
        "localStorage": {origin: storage},
    }

    directory = session.cfg.state_archive_dir
    directory.mkdir(parents=True, exist_ok=True)
    dir_note = _harden_dir(directory)
    path = directory / f"{name}.json"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(json.dumps(payload, ensure_ascii=False, indent=2))
    file_note = _harden_file(path)

    session.note_success(session.target_id)
    text = (
        f"已保存登录态存档：{path.resolve()}\n"
        f"cookie {len(cookies)} 项；origin {len(payload['localStorage'])} 个\n"
        f"权限：{dir_note}；{file_note}"
    )
    return [text_block(text)]
