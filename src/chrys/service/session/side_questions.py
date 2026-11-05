# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Small, owner-verified JSON histories containing completed side questions only."""

from __future__ import annotations

import errno
import json
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from chrys.foundation.models.session_surface import SessionSurface
from chrys.foundation.models.side_questions import SideQuestionEntry
from chrys.foundation.platform.files import atomic_write_owner_only_text, read_owner_verified_bounded

BTW_FILE_NAME = "btw.json"
_MAX_HISTORY_BYTES = 32 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class SideQuestionSessionSeed:
    """Non-secret metadata used only if a side question creates the session's first envelope."""

    agent_profile: str = ""
    agent_display_name: str = ""
    agent_profile_id: str = ""
    primary_cwd: str = ""
    working_dirs: tuple[str, ...] = ()
    model_provider: str = ""
    model_api_style: str = ""
    model_id: str = ""
    model_profile_id: str = ""
    last_surface: SessionSurface | None = None


def order_side_questions(entries: tuple[SideQuestionEntry, ...]) -> tuple[SideQuestionEntry, ...]:
    """Navigation follows question time, including old files saved in completion order."""
    return tuple(sorted(entries, key=lambda entry: datetime.fromisoformat(entry.created_at)))


def load_side_questions(directory: Path, session_id: str) -> tuple[SideQuestionEntry, ...]:
    """An absent file is empty; an invalid file must never be silently overwritten."""
    if not session_id:
        raise ValueError("Side question history requires a session identity")
    try:
        payload = read_owner_verified_bounded(directory / BTW_FILE_NAME, max_bytes=_MAX_HISTORY_BYTES)
    except OSError as exc:
        if exc.errno == errno.ENOENT:
            return ()
        raise
    data = json.loads(payload)
    if not isinstance(data, dict) or data.get("session_id") != session_id or not isinstance(data.get("items"), list):
        raise ValueError("Invalid side question history")
    entries: list[SideQuestionEntry] = []
    for item in data["items"]:
        if not isinstance(item, dict) or any(
            not isinstance(item.get(key), str) or not item[key].strip() for key in ("created_at", "question", "answer")
        ):
            raise ValueError("Invalid side question entry")
        if datetime.fromisoformat(item["created_at"]).utcoffset() is None:
            raise ValueError("Side question timestamps require a timezone")
        entries.append(SideQuestionEntry(item["created_at"], item["question"], item["answer"]))
    return order_side_questions(tuple(entries))


def write_side_questions(directory: Path, session_id: str, entries: tuple[SideQuestionEntry, ...]) -> None:
    """Replace one complete snapshot; the caller owns the session's write lock."""
    if not session_id:
        raise ValueError("Side question history requires a session identity")
    payload = json.dumps(
        {"session_id": session_id, "items": [asdict(entry) for entry in order_side_questions(entries)]},
        ensure_ascii=False,
        indent=2,
    )
    payload += "\n"
    if len(payload.encode("utf-8", errors="backslashreplace")) > _MAX_HISTORY_BYTES:
        raise ValueError("Side question history exceeds the storage limit")
    atomic_write_owner_only_text(directory / BTW_FILE_NAME, payload)
