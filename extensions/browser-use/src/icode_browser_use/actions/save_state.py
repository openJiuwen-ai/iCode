# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""``browser_save_state`` - export cookies + localStorage to a named archive."""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from .._proc import atomic_write_private, harden_private
from ..downloads import _RESERVED
from ..errors import BadRequest, Blocked, Unavailable
from ..policy import canonical_origin, scheme_allowed, url_host_allowed
from ..types import ToolBlock, text_block
from ._shared import eval_value

NAME = "browser_save_state"

DESCRIPTION = (
    "Export the current origin's cookies (Network.getCookies) and localStorage into "
    "a named JSON archive written under the plugin's state directory (never the "
    "workspace). "
    "name must match ^[a-z0-9_-]{1,32}$ and must not be a Windows reserved device name; "
    "the archive is written atomically with owner-only permissions (0600 on POSIX, "
    "current-user SID ACL on Windows). "
    "Use this to back up / migrate a login state; there is no restore tool yet."
)

PARAMS: dict[str, Any] = {
    "name": {"type": "string", "required": True, "pattern": "^[a-z0-9_-]{1,32}$"},
}

_NAME_RE = re.compile(r"^[a-z0-9_-]{1,32}$")
_STATE_EXPR = (
    "(() => ({"
    "origin: String(location.origin || ''), "
    "href: String(location.href || ''), "
    "storage: JSON.stringify(Object.fromEntries(Object.entries(localStorage)))"
    "}))()"
)
_LS_EXPR = _STATE_EXPR


def _is_reserved_name(name: str) -> bool:
    stem = name.split(".", 1)[0].upper()
    if stem in _RESERVED:
        return True
    isreserved = getattr(os.path, "isreserved", None)
    if callable(isreserved) and isreserved(name):
        return True
    return False


def _parse_storage(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, str) or not raw:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _strip_userinfo(url: str) -> str:
    """Remove any embedded ``user:pass@`` from *url* while keeping scheme/host/port/path."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return ""
    if not parts.scheme or not parts.hostname:
        return ""
    host = parts.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    netloc = f"{host}:{parts.port}" if parts.port is not None else host
    return urlunsplit((parts.scheme.lower(), netloc, parts.path, "", ""))


def _write_archive(directory: Path, path: Path, content: str) -> tuple[str, str]:
    directory.mkdir(parents=True, exist_ok=True)
    dir_ok = harden_private(directory, is_dir=True, strict=False)
    file_ok = atomic_write_private(path, content, strict=(sys.platform == "win32"), reject_symlink=True)
    if sys.platform == "win32":
        dir_note = "目录 ACL 已收紧到当前用户" if dir_ok else "目录 ACL 收紧不可用"
        file_note = "文件 ACL 已收紧到当前用户" if file_ok else "文件 ACL 收紧失败"
    else:
        dir_note = "目录 0700" if dir_ok else "目录 0700（chmod 不受支持）"
        file_note = "文件 0600" if file_ok else "文件 0600（chmod 不受支持）"
    return dir_note, file_note


async def run(session: Any, arguments: dict[str, Any]) -> list[ToolBlock]:
    if not session.cfg.save_state_enabled:
        raise BadRequest("browser_save_state 未启用（ICODE_BROWSER_SAVE_STATE=allow 才注册）")
    name = str(arguments.get("name") or "")
    if not _NAME_RE.fullmatch(name):
        raise BadRequest(f"name 非法（应匹配 ^[a-z0-9_-]{{1,32}}$）：{name!r}")
    if _is_reserved_name(name):
        raise BadRequest(f"name 不能使用系统保留设备名：{name!r}")

    client = session.require_client()
    raw_state = await eval_value(session, _STATE_EXPR)
    if isinstance(raw_state, dict):
        origin = str(raw_state.get("origin") or "")
        href = str(raw_state.get("href") or "")
        storage = _parse_storage(raw_state.get("storage"))
    else:
        # Compat fallback for legacy unit test fakes that answer separate localStorage & location.origin probes
        storage = _parse_storage(raw_state)
        origin = str(await eval_value(session, "location.origin") or "")
        href = origin or str(getattr(session.snapshot, "url", "") or "")

    clean_href = (
        _strip_userinfo(href)
        or _strip_userinfo(origin)
        or _strip_userinfo(str(getattr(session.snapshot, "url", "") or ""))
    )
    resolved_origin = canonical_origin(origin) or canonical_origin(clean_href)
    if not resolved_origin or origin == "null":
        raise BadRequest("无法确定当前 origin，拒绝导出登录态")

    parts = urlsplit(clean_href or resolved_origin)
    if not scheme_allowed(parts.scheme):
        raise Blocked(f"不支持的 scheme：{parts.scheme}（拒绝导出登录态）")
    domains = getattr(session.cfg, "allowed_domains", ())
    origins = getattr(session.cfg, "allowed_origins", ())
    if not url_host_allowed(clean_href or resolved_origin, domains, origins):
        raise Blocked(f"当前站点不在允许范围内，拒绝导出登录态：{resolved_origin}", blocked_origin=resolved_origin)

    cookie_url = clean_href if (clean_href and canonical_origin(clean_href) == resolved_origin) else resolved_origin
    cookies_response = await client.send(
        "Network.getCookies",
        {"urls": [cookie_url]},
        session_id=session.session_id,
        timeout=session.cfg.cdp_timeout,
    )
    cookies = list(cookies_response.get("cookies") or [])

    payload = {
        "version": 1,
        "saved_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "cookies": cookies,
        "localStorage": {resolved_origin: storage},
    }

    directory = session.cfg.state_archive_dir
    path = directory / f"{name}.json"
    if path.is_symlink():
        raise BadRequest(f"拒绝写入符号链接目标：{path}")
    serialized = json.dumps(payload, ensure_ascii=False, indent=2)
    try:
        dir_note, file_note = await asyncio.to_thread(_write_archive, directory, path, serialized)
    except OSError as exc:
        raise Unavailable(f"保存登录态存档失败：{exc}") from exc

    session.note_success(session.target_id)
    text = (
        f"已保存登录态存档：{path.resolve()}\n"
        f"cookie {len(cookies)} 项；origin {len(payload['localStorage'])} 个\n"
        f"权限：{dir_note}；{file_note}"
    )
    return [text_block(text)]
