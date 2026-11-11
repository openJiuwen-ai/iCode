# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Recognize pasted or dropped image paths and clipboard bitmaps for the chat input."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import tempfile
import time
import uuid
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.error import URLError
from urllib.parse import urlparse
from urllib.request import url2pathname

from chrys.app.tui.terminal.title import running_under_textual_web_driver
from chrys.foundation.platform import get_platform, safe_getcwd
from chrys.foundation.text.images import IMAGE_MIME_BY_EXTENSION

if TYPE_CHECKING:
    from PIL import Image

    from chrys.service.state.store import StateStore

CLIPBOARD_IMAGE_SESSION_SUBDIR = Path("attachments") / "clipboard"
_CLIPBOARD_IMAGE_KEEP_COUNT = 100
_CLIPBOARD_IMAGE_KEEP_BYTES = 512 * 1024 * 1024
# Pillow itself waits 500 ms and retries once while another process holds the
# clipboard, so five attempts already ride out ~2.5 s; the paste runs on the
# UI thread, and each extra attempt freezes it for another half second.
_WINDOWS_CLIPBOARD_GRAB_ATTEMPTS = 5
_WINDOWS_CLIPBOARD_GRAB_DELAY_SECONDS = 0.02
_MACOS_JXA_TIMEOUT_SECONDS = 10.0
_MACOS_JXA_DECODE_FORMATS = ("PNG", "TIFF")
# Tried in order, one fetch each: a PNG Pillow refuses still leaves the TIFF.
_MACOS_JXA_TYPE_GROUPS = (("public.png", "PNGf"), ("public.tiff", "TIFF"))
_CLIPBOARD_IMAGE_SAVE_MODES = frozenset({"1", "L", "LA", "P", "RGB", "RGBA"})


@dataclass(frozen=True)
class _ClipboardImageGrab:
    """A clipboard bitmap plus the temp file its bytes were read from."""

    image: Image.Image
    source_path: Path


def pasted_image_paths(text: str, cwd: Path) -> list[Path]:
    """The image files *text* names when it is a paste/drop payload of image paths only.

    Terminal drag-and-drop generally arrives as bracketed paste text. File
    copy/paste can arrive the same way depending on the terminal and platform.
    Only path-only payloads count; normal prose yields no paths.
    """
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    return _parse_image_path_payload(normalized, cwd=cwd)


def clipboard_image_dir_for_session(state_store: StateStore | None, session_id: str | None) -> Path | None:
    """Return the session attachment directory for saved clipboard images."""
    if state_store is None or not session_id:
        return None
    return state_store.session_dir(session_id) / CLIPBOARD_IMAGE_SESSION_SUBDIR


def save_clipboard_image_to_file(directory: Path | None) -> Path | None:
    """Save a platform clipboard image into *directory* and return its path.

    Screenshot and image-copy workflows on macOS and Windows can put bitmap
    data on the clipboard instead of a file path. Textual's terminal paste
    events only carry text, so the chat input uses this helper from its
    explicit paste action to bridge bitmap clipboard contents back into
    the draft's ``[Image #n]`` labels, which send as ``@file`` attachments. Browser-host sessions
    must not read the server machine's clipboard because it is unrelated to
    the browser client's clipboard and may belong to a different user.
    """
    if directory is None or running_under_textual_web_driver():
        return None
    info = get_platform()
    if not (info.is_macos or info.is_windows):
        return None
    from PIL import Image

    try:
        clipboard_payload = _grab_clipboard()
        return _path_from_clipboard_payload(clipboard_payload, directory=directory)
    # Pillow raises SyntaxError for a broken chunk once it decodes the pixels,
    # and DecompressionBombError for an image too large to open safely.
    except OSError, RuntimeError, SyntaxError, ValueError, Image.DecompressionBombError:
        return None


def _grab_clipboard() -> object:
    """Return the clipboard payload: a macOS grab, a Windows image or file-path list, or ``None``."""
    if get_platform().is_macos:
        return _grab_macos_clipboard_image()
    return _grab_windows_clipboard()


def _grab_windows_clipboard() -> object:
    """Read the Windows clipboard with retries for transient clipboard-owner races."""
    from PIL import ImageGrab

    for attempt in range(_WINDOWS_CLIPBOARD_GRAB_ATTEMPTS):
        try:
            return ImageGrab.grabclipboard()
        except OSError:
            if attempt == _WINDOWS_CLIPBOARD_GRAB_ATTEMPTS - 1:
                raise
            time.sleep(_WINDOWS_CLIPBOARD_GRAB_DELAY_SECONDS)

    return None


def _grab_macos_clipboard_image() -> _ClipboardImageGrab | None:
    """Read the pasteboard image with JXA osascript calls.

    ``get the clipboard as «class PNGf»`` marshals the image as hex text and
    runs past any sane timeout on multi-megabyte screenshots; JXA writes the
    raw bytes to a file instead. PNG goes first so screenshots paste without
    decoding the uncompressed TIFF flavor. AppKit offers a TIFF flavor for
    every image it holds, so JPEG and GIF copies arrive as TIFF.
    """
    # One budget for all fetches: a pasteboard that hangs one fetch would hang the next.
    deadline = time.monotonic() + _MACOS_JXA_TIMEOUT_SECONDS
    for types in _MACOS_JXA_TYPE_GROUPS:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        grab = _grab_macos_clipboard_types(types, timeout=remaining)
        if grab is not None:
            return grab
    return None


def _grab_macos_clipboard_types(types: Sequence[str], *, timeout: float) -> _ClipboardImageGrab | None:
    """Fetch the pasteboard data for the first of *types* it holds, and decode it."""
    from PIL import Image, UnidentifiedImageError

    tmp = _reserve_macos_grab_file()
    grab: _ClipboardImageGrab | None = None
    try:
        with suppress(UnidentifiedImageError, Image.DecompressionBombError, OSError, SyntaxError, ValueError):
            if _run_macos_jxa_fetch(tmp, types, timeout=timeout):
                with Image.open(tmp, formats=_MACOS_JXA_DECODE_FORMATS) as image:
                    image.load()
                    grab = _ClipboardImageGrab(image=image, source_path=tmp)
    finally:
        # Until a grab takes the file, whatever the fetch wrote goes with it.
        if grab is None:
            with suppress(OSError):
                tmp.unlink(missing_ok=True)
    return grab


def _reserve_macos_grab_file() -> Path:
    """Create an empty owner-only (0600) file in the user's temp dir for the pasteboard bytes.

    Outside the session dir, a paste with no image creates no attachment
    folder, and a file left behind when iCode dies mid-paste goes with the
    OS's temp cleanup. The JXA fills this file in place, so the clipboard
    image stays private even when ``TMPDIR`` points at a directory other
    users can read; a file JXA created itself would take the umask,
    typically 0644.
    """
    fd, name = tempfile.mkstemp(prefix="chrys-clipboard-grab-", suffix=".tmp")
    os.close(fd)
    return Path(name)


def _run_macos_jxa_fetch(destination: Path, types: Sequence[str], *, timeout: float) -> bool:
    """Write the pasteboard data for the first of *types* it holds to *destination*.

    Returns ``True`` only when the file holds bytes afterwards: a pasteboard
    without any of *types* leaves it empty, and subprocess failures, a
    *timeout* included, count as no image. ``TimeoutExpired`` is a
    ``SubprocessError``, which the caller's ``except OSError`` cannot catch —
    it dies here.
    """
    try:
        subprocess.run(  # noqa: S603 - fixed osascript path and a fixed script with JSON-encoded strings.
            [
                "/usr/bin/osascript",
                "-l",
                "JavaScript",
                "-e",
                _macos_jxa_fetch_script(str(destination), types),
            ],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
            timeout=timeout,
        )
    except subprocess.SubprocessError, FileNotFoundError, OSError:
        return False
    try:
        return destination.is_file() and destination.stat().st_size > 0
    except OSError:
        return False


def _macos_jxa_fetch_script(destination: str, types: Sequence[str]) -> str:
    """Build the JXA that writes the data for the first of *types* on the pasteboard to *destination*.

    Type matching must go through ``containsObject``: JXA does not bridge the
    pasteboard's NSString elements to JS strings, so ``types.js.includes``
    never matches and no image would be written. Strings are JSON-encoded: a
    JSON string is a JavaScript string literal, so any path, newlines
    included, survives. The write is not atomic, so it fills the reserved
    owner-only file in place and keeps its mode; an atomic write's side file
    would also outlive an osascript killed mid-write.
    """
    return (
        "ObjC.import('AppKit');"
        "const pasteboard = $.NSPasteboard.generalPasteboard;"
        f"const imageType = {json.dumps(list(types))}.find((type) => pasteboard.types.containsObject(type));"
        "if (imageType !== undefined) {"
        f"pasteboard.dataForType(imageType).writeToFileAtomically({json.dumps(destination)}, false);"
        "}"
    )


def _path_from_clipboard_payload(payload: object, *, directory: Path) -> Path | None:
    from PIL import Image

    if isinstance(payload, _ClipboardImageGrab):
        return _save_clipboard_image(payload.image, directory=directory, source_path=payload.source_path)
    if isinstance(payload, Image.Image):
        return _save_clipboard_image(payload, directory=directory)
    if isinstance(payload, list):
        return _first_supported_clipboard_file_path(payload)
    return None


def _save_clipboard_image(
    image: Image.Image,
    *,
    directory: Path,
    source_path: Path | None = None,
) -> Path | None:
    """Save *image* as PNG in *directory*; a PNG *source_path* moves in without re-encoding.

    ``shutil.move`` because the temp dir and the session dir can be on
    different volumes.
    """
    converted: Image.Image | None = None
    try:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"clipboard-image-{uuid.uuid4().hex}.png"
        image_to_save = image
        if image.mode not in _CLIPBOARD_IMAGE_SAVE_MODES:
            converted = image.convert("RGBA")
            image_to_save = converted
        if source_path is not None and image_to_save is image and image.format == "PNG":
            shutil.move(source_path, path)
        else:
            image_to_save.save(path, format="PNG")
        saved = path.resolve(strict=True)
        _prune_clipboard_image_dir(directory, keep=saved)
        return saved
    except OSError, RuntimeError, ValueError:
        return None
    finally:
        if source_path is not None:
            with suppress(OSError):
                source_path.unlink(missing_ok=True)
        if converted is not None:
            converted.close()


def _prune_clipboard_image_dir(directory: Path, *, keep: Path) -> None:
    """Bound session/temp clipboard-image buildup while keeping recent pasted files available."""
    entries: list[tuple[int, int, Path]] = []
    try:
        candidates = list(directory.glob("clipboard-image-*.png"))
    except OSError:
        return
    for path in candidates:
        try:
            stat = path.stat()
        except OSError:
            continue
        entries.append((stat.st_mtime_ns, stat.st_size, path))
    entries.sort(key=lambda entry: entry[0], reverse=True)

    total_size = 0
    for index, (_mtime_ns, size, path) in enumerate(entries):
        total_size += size
        if path == keep or (index < _CLIPBOARD_IMAGE_KEEP_COUNT and total_size <= _CLIPBOARD_IMAGE_KEEP_BYTES):
            continue
        with suppress(OSError):
            path.unlink()


def _first_supported_clipboard_file_path(paths: Sequence[object]) -> Path | None:
    cwd = Path(safe_getcwd())
    for candidate in paths:
        if not isinstance(candidate, str):
            continue
        path = _resolve_supported_image_path(candidate, cwd=cwd)
        if path is not None:
            return path
    return None


def _parse_image_path_payload(text: str, *, cwd: Path) -> list[Path]:
    stripped = text.strip()
    if not stripped:
        return []

    for candidates in _candidate_path_groups(stripped):
        paths = _resolve_supported_image_group(candidates, cwd=cwd)
        if paths:
            return paths
    return []


def _candidate_path_groups(text: str) -> list[list[str]]:
    groups: list[list[str]] = [[text]]

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) > 1:
        groups.append(lines)

    split_tokens = text.split()
    if len(split_tokens) > 1:
        groups.append(split_tokens)

    if os.name != "nt":
        shlex_tokens = _shlex_split(text)
        if shlex_tokens and shlex_tokens not in groups:
            groups.append(shlex_tokens)

    return groups


def _shlex_split(text: str) -> list[str]:
    try:
        return shlex.split(text)
    except ValueError:
        return []


def _resolve_supported_image_group(candidates: list[str], *, cwd: Path) -> list[Path]:
    paths: list[Path] = []
    for candidate in candidates:
        path = _resolve_supported_image_path(candidate, cwd=cwd)
        if path is None:
            return []
        paths.append(path)
    return paths


def _resolve_supported_image_path(candidate: str, *, cwd: Path) -> Path | None:
    quoted = _candidate_is_quoted(candidate)
    path_text = _path_text_from_candidate(candidate)
    if not _path_text_has_supported_image_suffix(path_text) or not _path_text_looks_like_single_path(
        path_text,
        quoted=quoted,
    ):
        return None
    try:
        path = Path(path_text).expanduser()
    except RuntimeError:
        return None
    if path.suffix.lower() not in IMAGE_MIME_BY_EXTENSION:
        return None
    if not path.is_absolute():
        path = cwd / path
    try:
        resolved = path.resolve(strict=True)
    except OSError, ValueError:
        return None
    if not resolved.is_file():
        return None
    return resolved


def _path_text_has_supported_image_suffix(path_text: str) -> bool:
    if not path_text:
        return False
    try:
        suffix = Path(path_text).suffix.lower()
    except ValueError:
        return False
    return suffix in IMAGE_MIME_BY_EXTENSION


def _path_text_looks_like_single_path(path_text: str, *, quoted: bool) -> bool:
    if not any(char.isspace() for char in path_text):
        return True
    try:
        expanded_path = Path(path_text).expanduser()
    except RuntimeError, ValueError:
        return False
    return (
        quoted or expanded_path.is_absolute() or path_text.startswith("~") or _path_text_is_windows_absolute(path_text)
    )


def _path_text_is_windows_absolute(path_text: str) -> bool:
    return (len(path_text) >= 3 and path_text[1:3] in (":\\", ":/")) or path_text.startswith(("\\\\", "//"))


def _candidate_is_quoted(candidate: str) -> bool:
    text = candidate.strip()
    return len(text) >= 2 and text[0] == text[-1] and text[0] in ("'", '"')


def _path_text_from_candidate(candidate: str) -> str:
    text = candidate.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ("'", '"'):
        text = text[1:-1]
    try:
        parsed = urlparse(text)
    except ValueError:
        return text
    if parsed.scheme == "file":
        path = parsed.path
        if parsed.netloc and parsed.netloc != "localhost":
            path = f"//{parsed.netloc}{path}"
        try:
            return url2pathname(path)
        except URLError, ValueError:
            return ""
    return text
