# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Download records and the filename safety rules for browser downloads.

Chrome writes each download under its GUID (``allowAndName``), and this module
owns the step that gives it a safe, unique name inside the plugin's own download
folder: no absolute paths, no traversal, no reserved device names, no overwrite.
"""

from __future__ import annotations

import contextlib
import os
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

#: Longest file name (not path) kept; Chrome's own limit is larger but this
#: leaves room for a ``-12`` suffix.
NAME_LIMIT = 120
#: Longest UTF-8 byte length kept so multi-byte names (CJK/emoji) never exceed
#: ext4/APFS's 255-byte component cap after adding a ``-1000`` suffix.
NAME_BYTE_LIMIT = 200

_RESERVED = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}
_INVALID = re.compile(r'[<>:"/\\|?*]')
_SPACE = re.compile(r"\s+")


def _safe_char(ch: str) -> str:
    """One file-name character: control/format characters become ``_``."""
    if ch.isspace() and ch not in " \u00a0":
        return "_"
    if ord(ch) < 32 or ord(ch) == 127 or unicodedata.category(ch) == "Cf":
        return "_"
    return ch


@dataclass
class DownloadInfo:
    """One tracked download; ``path`` is the final, safe file once completed."""

    guid: str
    name: str
    state: str = "in_progress"
    bytes_received: int = 0
    total_bytes: int | None = None
    path: Path | None = None
    partial_path: Path | None = None
    url: str = ""
    reason: str = ""


def sanitize_filename(raw: str, *, fallback: str = "download") -> str:
    """Return a single, Windows-safe file name derived from a page-supplied name."""
    name = "".join(_safe_char(ch) for ch in str(raw or "").replace("\\", "/").split("/")[-1])
    name = _SPACE.sub(" ", name).strip()
    name = _INVALID.sub("_", name).strip(" .")
    if not name or name in {".", ".."}:
        return fallback
    stem, dot, suffix = name.rpartition(".")
    if not dot:
        stem, suffix = name, ""
    if suffix and (len(suffix) > 16 or not re.fullmatch(r"[A-Za-z0-9_-]+", suffix)):
        stem, suffix = name, ""
    if stem.upper() in _RESERVED or name.split(".")[0].upper() in _RESERVED:
        stem = f"_{stem}"
    if suffix:
        name = f"{stem}.{suffix}"
    else:
        name = stem
    if len(name) > NAME_LIMIT or len(name.encode("utf-8")) > NAME_BYTE_LIMIT:
        suffix_part = f".{suffix}" if suffix else ""
        max_chars = max(1, NAME_LIMIT - len(suffix_part))
        max_bytes = max(1, NAME_BYTE_LIMIT - len(suffix_part.encode("utf-8")))
        trimmed = stem[:max_chars]
        while trimmed and len(trimmed.encode("utf-8")) > max_bytes:
            trimmed = trimmed[:-1]
        name = f"{trimmed}{suffix_part}" if trimmed else (f"{fallback}{suffix_part}" if suffix_part else fallback)
        name = name.rstrip(" .") or fallback
    return name


def reserve_path(directory: Path, name: str) -> Path:
    """Create an empty placeholder for *name* inside *directory*, never overwriting.

    ``O_EXCL`` makes the reservation atomic against Chromium or another plugin
    instance; a numbered suffix keeps the newest download instead of replacing an
    older file. ``directory`` is created owner-only on POSIX.
    """
    existed = directory.exists()
    directory.mkdir(parents=True, exist_ok=True)
    if os.name == "posix" and not existed:
        with contextlib.suppress(OSError):
            os.chmod(directory, 0o700)
    base = directory.resolve()
    stem, suffix = os.path.splitext(name)
    for attempt in range(1, 1001):
        candidate = directory / (name if attempt == 1 else f"{stem}-{attempt}{suffix}")
        if candidate.parent.resolve() != base:
            raise OSError(f"download path escaped its folder: {candidate}")
        try:
            handle = os.open(candidate, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            continue
        os.close(handle)
        return candidate
    raise OSError("too many files with the same download name")


def display_url(url: str, limit: int = 200) -> str:
    """Origin+path view of a download URL with credentials, ``;params`` and query stripped."""
    from .policy import sanitize_display_url

    value = sanitize_display_url(url)
    if len(value) > limit:
        value = value[:limit] + f"… (+{len(value) - limit} chars)"
    return value


def remove_quiet(path: Path | None) -> None:
    """Remove a partial/placeholder file; a missing file is success."""
    if path is None:
        return
    try:
        path.unlink()
    except OSError:
        return


def file_size(path: Path) -> int | None:
    try:
        return path.stat().st_size
    except OSError:
        return None
