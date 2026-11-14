# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Workspace tools — read_file, write_file, edit_file.

Approval for ``write_file`` and ``edit_file`` is handled by
``ApprovalMiddleware`` (a ``FunctionMiddleware``); the tool layer itself never
pauses for approval.

Shell execution tools are in shell.py as instance tools.
"""

from __future__ import annotations

import os
import secrets
import stat
import tempfile
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated

from pydantic import Field

from chrys.foundation.platform import get_platform
from chrys.foundation.platform.files import replace_with_retry, surrogate_safe_text
from chrys.foundation.platform.paths import resolve_existing_path, resolve_workspace_path
from chrys.foundation.text.images import MAX_IMAGE_BYTES, ImageProcessingError, load_image_file
from chrys.foundation.text.lines import normalize_line_endings, split_lines
from chrys.kernel import Content
from chrys.service.tools.approval_targets import ApprovedTargets, approved_write_path, file_write_target
from chrys.service.tools.kinds import KIND_FILESYSTEM_READ, KIND_FILESYSTEM_WRITE, tool
from chrys.service.tools.result_metadata import tool_error
from chrys.service.tools.session_artifacts import (
    is_document_artifact_handle,
    resolve_document_image_artifact_handle,
    resolve_document_markdown_artifact_handle,
    resolve_tool_session_dir,
)
from chrys.service.tools.workspace_paths import missing_base_cwd_error

if TYPE_CHECKING:
    from pathlib import Path

    from chrys.foundation.models.session_env import SessionEnvironment

_DEFAULT_MAX_TOKENS = 10000
_DEFAULT_READ_OFFSET = 1
_DEFAULT_READ_LIMIT = 2000
_MAX_LINE_DISPLAY_CHARS = 2048
_NEW_FILE_MODE = 0o644
_NEW_EXECUTABLE_FILE_MODE = 0o755
_ATOMIC_TEMP_CREATE_ATTEMPTS = 100
_SHELL_SCRIPT_EXTENSIONS = frozenset({".sh", ".bash", ".zsh", ".fish", ".ksh", ".csh", ".tcsh", ".command"})

# Runtime Annotated aliases keep the schema flat and expose int to argument validation.
_ReadMaxTokens = Annotated[
    int,
    Field(
        ge=1,
        description=(
            f"Optional token budget for file content, estimated by iCode. Defaults to {_DEFAULT_MAX_TOKENS}. "
            "Takes priority over limit: content is truncated when this budget is reached, even with fewer lines."
        ),
    ),
]
_ReadOffset = Annotated[
    int,
    Field(
        ge=1,
        description=f"Line number to start reading from (1-indexed). Defaults to {_DEFAULT_READ_OFFSET}.",
    ),
]
_ReadLimit = Annotated[
    int,
    Field(
        ge=1,
        description=(
            f"Optional maximum number of lines to read. Defaults to {_DEFAULT_READ_LIMIT}; "
            "max_tokens may truncate earlier. Use 1 to read a long line in full, within max_tokens."
        ),
    ),
]


@dataclass(slots=True)
class FileToolPreviewError:
    """A deterministic file-tool failure discovered before execution."""

    kind: str
    message: str
    resolved_path: str = ""


@dataclass(slots=True)
class WriteFilePlan:
    """Prepared write_file operation with final in-memory content."""

    resolved_path: str
    write_content: str
    display_content: str
    before_display_content: str = ""
    overwrites_existing: bool = False
    content_only_reason: str = ""
    # An existing UTF-16/UTF-32 file keeps its byte order; everything else is
    # written as plain UTF-8.
    encoding: str = "utf-8"
    bom: bytes = b""


@dataclass(slots=True)
class EditFilePlan:
    """Prepared edit_file operation with before/after preview and write data."""

    resolved_path: str
    encoding: str
    decode_errors: str
    bom: bytes
    before_text: str
    after_text: str
    write_content: str
    replacements: int
    snippet: str


def _new_posix_file_mode(path: str, content: str) -> int:
    extension = os.path.splitext(path)[1].lower()
    if content.startswith("#!") or extension in _SHELL_SCRIPT_EXTENSIONS:
        return _NEW_EXECUTABLE_FILE_MODE
    return _NEW_FILE_MODE


def _posix_write_mode(path: str, content: str) -> tuple[int, bool]:
    """Return requested rwx mode and whether it must exactly match an existing file."""
    try:
        # Preserve existing user/group/other rwx bits. Special bits are
        # deliberately cleared when replacing file contents.
        return stat.S_IMODE(os.stat(path).st_mode) & 0o777, True
    except OSError:
        # A dangling, looping, or inaccessible symlink was replaceable before
        # mode preservation was added. Treat every stat failure as a new-file
        # replacement and let the actual temp creation/replace report any real
        # parent-directory failure.
        return _new_posix_file_mode(path, content), False


def _open_posix_atomic_temp(parent: str, mode: int) -> tuple[int, str]:
    """Securely create a same-directory temp file with *mode* filtered by umask."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    for _ in range(_ATOMIC_TEMP_CREATE_ATTEMPTS):
        tmp_path = os.path.join(parent, f".chrys-{secrets.token_hex(12)}.tmp")
        try:
            # Unlike tempfile.mkstemp's fixed 0600, os.open applies the
            # process umask to the requested new-file mode without reading or
            # mutating that process-global setting.
            return os.open(tmp_path, flags, mode), tmp_path
        except FileExistsError:
            continue
    raise FileExistsError("failed to allocate a unique atomic-write temporary file")


def _atomic_write(
    path: str,
    content: str,
    encoding: str = "utf-8",
    errors: str = "strict",
    bom: bytes = b"",
) -> None:
    """Write *content* to *path* atomically via temp-file + rename.

    Writes to a temporary file in the same directory (same filesystem) and
    then calls ``os.replace()`` which is atomic on POSIX and Windows/NTFS.
    If the write or flush fails, the temp file is removed and the original
    file is left untouched.

    *bom* — the exact byte prefix (a UTF-16/UTF-32 byte-order mark) to write
    ahead of the encoded text. A text-mode write would let Python pick the
    host's byte order instead, flipping an existing big-endian file.
    """
    path = approved_write_path(path)
    parent = os.path.dirname(path) or "."
    platform = get_platform()
    target_mode: int | None = None
    preserve_existing_mode = False
    if not platform.is_windows:
        target_mode, preserve_existing_mode = _posix_write_mode(path, content)
    payload = bom + content.encode(encoding, errors=errors)
    fd = None
    tmp_path = ""
    try:
        if platform.is_windows:
            fd, tmp_path = tempfile.mkstemp(dir=parent, suffix=".tmp")
        else:
            if target_mode is None:
                raise RuntimeError("A POSIX atomic write requires a target file mode.")
            fd, tmp_path = _open_posix_atomic_temp(parent, target_mode)
        with os.fdopen(fd, "wb") as f:
            fd = None  # os.fdopen takes ownership of the fd
            f.write(payload)
            f.flush()
            if preserve_existing_mode:
                # Existing modes override umask and must be restored exactly.
                # This must run while fdopen still owns an open descriptor.
                if target_mode is None:
                    raise RuntimeError("A POSIX atomic write requires a target file mode.")
                os.fchmod(f.fileno(), target_mode)
            os.fsync(f.fileno())
        replace_with_retry(tmp_path, path)
    except BaseException:
        # Clean up the temp file on any failure
        if fd is not None:
            os.close(fd)
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise


def _is_wide_encoding(encoding: str) -> bool:
    """Whether *encoding* stores a character in two or four whole bytes."""
    return encoding.lower().replace("_", "-").startswith(("utf-16", "utf-32"))


def _line_ending_counts(text: str) -> tuple[int, int, int]:
    """Return *(CRLF, lone CR, lone LF)* counts for *text*."""
    crlf = text.count("\r\n")
    return crlf, text.count("\r") - crlf, text.count("\n") - crlf


def _detect_line_ending(text: str, path: str = "") -> str:
    """Detect the predominant line ending in a text view of a file.

    Must run on decoded text, never on raw bytes: in UTF-16/UTF-32 a newline
    spans several bytes (``0D 00 0A 00``), so a byte scan finds none.

    Falls back to ``_default_eol_for_new_file(path)`` when the file contains
    no line endings (e.g. single-line or empty files).
    """
    crlf, cr, lf = _line_ending_counts(text)
    if crlf >= lf and crlf >= cr and crlf > 0:
        return "\r\n"
    if cr > lf and cr > 0:
        return "\r"
    if lf > 0:
        return "\n"
    # No line endings found — use the smart default for this file type / platform
    return _default_eol_for_new_file(path)


def _line_ending_for(raw: bytes, existing: _ExistingFileText, path: str) -> str:
    """Read an existing file's line ending from the view where it is visible.

    A wide encoding hides 0x0A/0x0D behind a second byte, so its decoded text
    must be scanned — with or without a BOM. When that text carries no line
    ending at all, the wide encoding may be a detection guess over a legacy
    file (whose bytes were never wide), so fall back to the raw bytes: every
    other codec is an ASCII superset whose newline bytes are never part of
    another character, and latin-1 maps them one to one.
    """
    if _is_wide_encoding(existing.encoding) and _line_ending_counts(existing.text) != (0, 0, 0):
        return _detect_line_ending(existing.text, path)
    return _detect_line_ending(raw.decode("latin-1"), path)


# File extensions that MUST use a specific line ending regardless of platform.
_FORCE_LF_EXTENSIONS = _SHELL_SCRIPT_EXTENSIONS | frozenset(
    {
        ".py",
        ".rb",
        ".pl",
        ".php",  # interpreters with shebang
        ".yml",
        ".yaml",  # YAML is LF-only by spec
    }
)
_FORCE_CRLF_EXTENSIONS = frozenset(
    {
        ".bat",
        ".cmd",  # Windows batch
    }
)


def _default_eol_for_new_file(path: str) -> str:
    """Choose the line ending for a newly created file.

    Priority:
    1. File extension overrides (e.g. .sh → LF, .bat → CRLF)
    2. Platform default (CRLF on Windows, LF elsewhere)
    """
    ext = os.path.splitext(path)[1].lower()
    if ext in _FORCE_LF_EXTENSIONS:
        return "\n"
    if ext in _FORCE_CRLF_EXTENSIONS:
        return "\r\n"
    return os.linesep


@dataclass(frozen=True, slots=True)
class _ExistingFileText:
    """An existing file decoded for editing, and how to write it back.

    ``text`` is the decoded content with its line endings intact; callers
    normalize a derived copy, never this value, when they need LF-only text.
    """

    text: str
    encoding: str
    errors: str
    bom: bytes


def _read_existing_text(raw: bytes, detected: str | None) -> _ExistingFileText:
    """Decode a file read as *raw*, keeping its byte order and BOM to write back.

    Raises whatever the codec raises; callers that tolerate an undecodable
    file catch it themselves.
    """
    from chrys.foundation.text.encoding import resolve_wide_encoding

    encoding, bom = resolve_wide_encoding(detected, raw)
    errors = "replace" if _is_wide_encoding(encoding) else "surrogateescape"
    body = raw[len(bom) :] if bom else raw
    return _ExistingFileText(body.decode(encoding, errors=errors), encoding, errors, bom)


def _decode_write_preview_before(raw: bytes) -> tuple[str, str, _ExistingFileText | None]:
    """Return approval-preview text, a content-only reason, and the decoded file.

    The decoded file is ``None`` for binary or undecodable content; the write
    still happens, but under the default encoding for the path.
    """
    from chrys.foundation.text.encoding import EncodingDetector

    if EncodingDetector.looks_binary(raw):
        return "", f"content-only; existing binary file ({len(raw)} bytes) not shown", None

    detector = EncodingDetector()
    detection = detector.detect_from_bytes(raw)
    if detection.is_binary:
        return "", f"content-only; existing binary file ({len(raw)} bytes) not shown", None

    try:
        existing = _read_existing_text(raw, detection.encoding)
    except LookupError, UnicodeDecodeError:
        return "", "content-only; existing file could not be decoded", None
    return _replace_surrogate_escapes(normalize_line_endings(existing.text)), "", existing


def plan_write_file(
    path: str,
    content: str,
    overwrite: bool = False,
    *,
    base_cwd: str | None = None,
) -> WriteFilePlan | FileToolPreviewError:
    """Prepare a ``write_file`` operation without touching the destination."""
    if is_document_artifact_handle(path):
        return FileToolPreviewError(
            kind="read_only_handle",
            message=tool_error(
                "session_artifact_handle_is_read_only",
                "session document handles are read-only; use a regular filesystem path for write_file.",
                details={"path": path},
            ),
        )
    try:
        resolved = resolve_workspace_path(path, base_cwd=base_cwd)
        if os.path.isdir(resolved):
            return FileToolPreviewError(
                kind="directory",
                message=tool_error(
                    "path_is_directory",
                    f"path is a directory, not a file — {resolved}",
                    details={"path": path, "resolved_path": resolved},
                ),
                resolved_path=resolved,
            )
        if os.path.exists(resolved) and not overwrite:
            return FileToolPreviewError(
                kind="exists",
                message=tool_error(
                    "file_exists",
                    f"file already exists — {resolved}. Set overwrite=true to replace it.",
                    details={"path": path, "resolved_path": resolved},
                ),
                resolved_path=resolved,
            )

        before_display_content = ""
        content_only_reason = ""
        write_encoding, write_bom = "utf-8", b""
        overwrites_existing = overwrite and os.path.isfile(resolved)
        # Determine line ending: preserve existing file's style, or pick a smart default.
        if overwrites_existing:
            with open(resolved, "rb") as f:
                raw = f.read()
            before_display_content, content_only_reason, existing = _decode_write_preview_before(raw)
            if existing is None:
                eol = _default_eol_for_new_file(resolved)
            else:
                eol = _line_ending_for(raw, existing, resolved)
                # A wide BOM is the only deterministic sign of a file whose
                # byte order is worth keeping; every other existing file is
                # still rewritten as plain UTF-8.
                if existing.bom:
                    write_encoding, write_bom = existing.encoding, existing.bom
        else:
            eol = _default_eol_for_new_file(resolved)

        write_content = normalize_line_endings(content)
        if eol != "\n":
            write_content = write_content.replace("\n", eol)
        return WriteFilePlan(
            resolved_path=resolved,
            write_content=write_content,
            display_content=_replace_surrogate_escapes(write_content),
            before_display_content=before_display_content,
            overwrites_existing=overwrites_existing,
            content_only_reason=content_only_reason,
            encoding=write_encoding,
            bom=write_bom,
        )
    except Exception as e:
        return FileToolPreviewError(
            kind="prepare_failed",
            message=tool_error("write_failed", f"failed to write file — {e}", details={"path": path}),
        )


def _read_file_impl(
    path: str,
    max_tokens: int = _DEFAULT_MAX_TOKENS,
    offset: int = _DEFAULT_READ_OFFSET,
    limit: int = _DEFAULT_READ_LIMIT,
    *,
    base_cwd: str | None = None,
    session_dir: Path | None = None,
) -> str:
    from pathlib import Path

    from chrys.foundation.text.encoding import EncodingDetector
    from chrys.foundation.text.tokenizer import TOKEN_UNITS_PER_TOKEN, MixedLanguageTokenizer

    if type(offset) is not int or offset < 1:
        return tool_error("invalid_offset", "offset must be an integer >= 1 (the first line is 1).")
    if type(limit) is not int or limit < 1:
        return tool_error("invalid_limit", "limit must be an integer >= 1 (the maximum number of lines to read).")
    if type(max_tokens) is not int or max_tokens < 1:
        return tool_error("invalid_max_tokens", "max_tokens must be an integer >= 1 (the file content token budget).")

    missing_base = missing_base_cwd_error(path, base_cwd)
    if missing_base is not None:
        return missing_base

    tokenizer = MixedLanguageTokenizer()
    detector = EncodingDetector()
    effective_path = path

    try:
        try:
            artifact_path = resolve_document_markdown_artifact_handle(path, session_dir)
        except ValueError as e:
            return tool_error(
                "invalid_session_artifact_handle",
                f"invalid session artifact handle — {surrogate_safe_text(str(e))}",
                details={"path": path},
            )
        if artifact_path is None:
            resolved = resolve_existing_path(effective_path, base_cwd=base_cwd)
        else:
            effective_path = artifact_path
            # Handles name one exact session artifact.  Do not feed them back
            # into the user-path convenience resolver: its Unicode/whitespace
            # fallbacks may select a different sibling than the handle names.
            resolved = artifact_path if os.path.exists(artifact_path) else None
        if resolved is None:
            resolved = resolve_workspace_path(effective_path, base_cwd=base_cwd)
            return tool_error(
                "file_not_found",
                f"file not found — {surrogate_safe_text(resolved)}",
                details={"path": path, "resolved_path": resolved},
            )
        display_resolved = surrogate_safe_text(resolved)
        detection = detector.detect_file(Path(resolved))
        if detection.is_binary:
            return tool_error(
                "binary_file",
                f"file appears to be binary — {display_resolved}",
                details={"path": path, "resolved_path": resolved},
            )
        encoding = detection.encoding or "utf-8"
        with open(resolved, encoding=encoding, errors="replace") as f:
            content = f.read()

        lines = split_lines(content)
        total_lines = len(lines)
        total_chars = len(content)

        # The default offset also admits an empty file.
        if offset > max(total_lines, 1):
            return tool_error(
                "invalid_offset",
                f"offset ({offset}) is beyond the end of the file ({total_lines} lines).",
                details={"offset": offset, "total_lines": total_lines},
            )
        lines = lines[offset - 1 : offset - 1 + limit]

        header = f"File: {display_resolved} ({total_lines} lines, {total_chars} chars)\n"
        # Keep fractional estimates across lines instead of rounding each line.
        remaining_units = max_tokens * TOKEN_UNITS_PER_TOKEN - tokenizer.count_token_units(header)

        output_lines: list[str] = []
        truncation_position = ""
        truncated_line_chars = 0
        full_line_hint = ""
        last_line_num = offset - 1
        next_line = offset
        long_lines: list[tuple[int, int]] = []

        for i, line in enumerate(lines):
            line_num = offset + i
            original_length = len(line)
            # A single-line read can recover a long line, within the token budget.
            char_truncated = limit != 1 and original_length > _MAX_LINE_DISPLAY_CHARS
            if char_truncated:
                line = line[:_MAX_LINE_DISPLAY_CHARS] + "... [truncated]"
            formatted = f"{line_num}|{line}\n"
            line_units = tokenizer.count_token_units(formatted)
            if line_units > remaining_units:
                truncation_position = f"before line {line_num}"
                truncated_line_chars = original_length
                if char_truncated:
                    full_line_hint = (
                        f" To read this line in full, use offset={line_num}, limit=1 "
                        "with a sufficient max_tokens budget."
                    )
                # Include a partial line instead of dropping it entirely
                if remaining_units > 0:
                    # Token density can differ along a mixed-language line.
                    # Measure the returned prefix, including its truncation marker.
                    low, high = 0, len(line)
                    while low < high:
                        mid = (low + high + 1) // 2
                        partial = f"{line_num}|{line[:mid]}... [truncated]\n"
                        if tokenizer.count_token_units(partial) <= remaining_units:
                            low = mid
                        else:
                            high = mid - 1
                    if low > 0:
                        output_lines.append(f"{line_num}|{line[:low]}... [truncated]\n")
                        truncation_position = f"at line {line_num}"
                break
            remaining_units -= line_units
            output_lines.append(formatted)
            if char_truncated:
                long_lines.append((line_num, original_length))
            last_line_num = line_num
            next_line = line_num + 1

        result = header + "".join(output_lines)

        if long_lines:
            details = ", ".join(f"line {ln} ({length} chars)" for ln, length in long_lines)
            result += (
                f"\n[Long lines truncated to {_MAX_LINE_DISPLAY_CHARS} chars: {details}."
                f" Use offset={long_lines[0][0]}, limit=1 with a sufficient max_tokens budget to read a line in full.]"
            )

        if truncation_position:
            result += (
                f"\n[Truncated {truncation_position}, file has {total_lines} lines. "
                f"Content exceeds max_tokens={max_tokens}. "
                f"Use offset={next_line}, limit={limit} to continue reading, increasing max_tokens if needed. "
                f"Source line {next_line} has {truncated_line_chars} chars.{full_line_hint}]"
            )
        elif next_line <= total_lines:
            result += (
                f"\n[Showing lines {offset}-{last_line_num} of {total_lines}. "
                f"Use offset={next_line}, limit={limit} to continue reading if needed.]"
            )

        return result
    except FileNotFoundError:
        resolved = resolve_workspace_path(effective_path, base_cwd=base_cwd)
        return tool_error(
            "file_not_found",
            f"file not found — {surrogate_safe_text(resolved)}",
            details={"path": path, "resolved_path": resolved},
        )
    except IsADirectoryError:
        resolved = resolve_workspace_path(effective_path, base_cwd=base_cwd)
        return tool_error(
            "path_is_directory",
            f"path is a directory, not a file — {surrogate_safe_text(resolved)}",
            details={"path": path, "resolved_path": resolved},
        )
    except Exception as e:
        return tool_error("read_failed", f"failed to read file — {surrogate_safe_text(str(e))}", details={"path": path})


@tool(kind=KIND_FILESYSTEM_READ)
def read_file(
    path: Annotated[str, "Absolute or relative path to the file to read."],
    max_tokens: _ReadMaxTokens = _DEFAULT_MAX_TOKENS,
    offset: _ReadOffset = _DEFAULT_READ_OFFSET,
    limit: _ReadLimit = _DEFAULT_READ_LIMIT,
) -> str:
    """Read a text file with line numbers. For lines 10-20, use offset=10, limit=11."""
    return _read_file_impl(path, max_tokens=max_tokens, offset=offset, limit=limit)


def _view_image_impl(
    path: str,
    *,
    base_cwd: str | None = None,
    session_dir: Path | None = None,
    max_image_bytes: int = MAX_IMAGE_BYTES,
) -> list[Content]:
    from pathlib import Path

    missing_base = missing_base_cwd_error(path, base_cwd)
    if missing_base is not None:
        return [Content.from_text(missing_base)]
    effective_path = path
    try:
        try:
            artifact_path = resolve_document_image_artifact_handle(path, session_dir)
        except ValueError as exc:
            return [
                Content.from_text(
                    tool_error(
                        "invalid_session_artifact_handle",
                        f"invalid session artifact handle — {surrogate_safe_text(str(exc))}",
                        details={"path": path},
                    )
                )
            ]
        if artifact_path is None:
            resolved = resolve_existing_path(effective_path, base_cwd=base_cwd)
        else:
            effective_path = artifact_path
            resolved = artifact_path if os.path.exists(artifact_path) else None
        if resolved is None:
            resolved = resolve_workspace_path(effective_path, base_cwd=base_cwd)
            return [
                Content.from_text(
                    tool_error(
                        "file_not_found",
                        f"file not found — {surrogate_safe_text(resolved)}",
                        details={"path": path, "resolved_path": resolved},
                    )
                )
            ]
        source_path = Path(resolved)
        if source_path.is_dir():
            return [
                Content.from_text(
                    tool_error(
                        "path_is_directory",
                        f"path is a directory, not a file — {surrogate_safe_text(resolved)}",
                        details={"path": path, "resolved_path": resolved},
                    )
                )
            ]

        loaded = load_image_file(source_path, max_bytes=max_image_bytes)
        metadata = {
            "width": loaded.width,
            "height": loaded.height,
            "media_type": loaded.media_type,
            "size": loaded.size,
            "source_path": surrogate_safe_text(resolved),
        }
        return [Content.from_data(data=loaded.data, media_type=loaded.media_type, additional_properties=metadata)]
    except ImageProcessingError as exc:
        return [
            Content.from_text(tool_error("view_image_failed", f"failed to view image — {exc}", details={"path": path}))
        ]
    except Exception as exc:
        return [
            Content.from_text(tool_error("view_image_failed", f"failed to view image — {exc}", details={"path": path}))
        ]


@tool(kind=KIND_FILESYSTEM_READ)
def view_image(
    path: Annotated[str, "Absolute or relative path to the image file to inspect."],
) -> list[Content]:
    """Read an image file and return it as model-visible image content.

    Images the user attached to a message are already visible to you, at the
    same size this tool returns; call it for one only when it is not in the
    conversation.
    """
    return _view_image_impl(path)


_FS_WRITE_LOCKS: dict[str, threading.Lock] = {}
_FS_WRITE_LOCKS_GUARD = threading.Lock()


def _fs_write_lock(path: str, base_cwd: str | None) -> threading.Lock:
    """Return the lock that serializes write/edit operations on *path*.

    Chrys runs sync ``FunctionTool``s through ``asyncio.to_thread``, so two
    write/edit calls in one tool batch can run concurrently in worker threads.
    ``edit_file`` (and overwrite-aware ``write_file``) is read-modify-write, so
    concurrent calls on the same file would race and lose updates. Lock per
    resolved path so same-file writes stay deterministic while distinct files
    still proceed in parallel; reads remain lock-free.
    """
    try:
        # realpath (not just normpath) so symlink aliases to the same physical
        # file share one lock — matching chrys's path-identity convention
        # (runtime_paths.paths_equal, approval middleware).
        key = os.path.normcase(os.path.realpath(resolve_workspace_path(path, base_cwd=base_cwd)))
    except Exception:
        key = "\x00unresolved"
    with _FS_WRITE_LOCKS_GUARD:
        lock = _FS_WRITE_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _FS_WRITE_LOCKS[key] = lock
        return lock


def _write_file_impl(
    path: str,
    content: str,
    overwrite: bool = False,
    *,
    base_cwd: str | None = None,
) -> str:
    # Checked first: a relative write would otherwise recreate the deleted
    # working directory through ``os.makedirs`` below.
    missing_base = missing_base_cwd_error(path, base_cwd)
    if missing_base is not None:
        return missing_base
    try:
        path = approved_write_path(path, base_cwd=base_cwd)
        with _fs_write_lock(path, base_cwd):
            plan = plan_write_file(path, content, overwrite=overwrite, base_cwd=base_cwd)
            if isinstance(plan, FileToolPreviewError):
                return plan.message

            # Ensure parent directory exists
            parent = os.path.dirname(plan.resolved_path)
            if parent:
                os.makedirs(parent, exist_ok=True)

            _atomic_write(plan.resolved_path, plan.write_content, encoding=plan.encoding, bom=plan.bom)
        line_count = len(split_lines(normalize_line_endings(content)))
        return f"Written {len(content)} chars ({line_count} lines) to {surrogate_safe_text(plan.resolved_path)}."
    except Exception as e:
        return tool_error("write_failed", f"failed to write file — {e}", details={"path": path})


@tool(kind=KIND_FILESYSTEM_WRITE)
def write_file(
    path: Annotated[str, "Absolute or relative path to the file to write."],
    content: Annotated[str, "The full file content to write."],
    overwrite: Annotated[
        bool,
        "Set to true to overwrite an existing file. "
        "If false (default) and the file already exists, "
        "the write will be rejected.",
    ] = False,
) -> str:
    """Write content to a file. Creates parent directories if needed.

    Use ``\\n`` for line breaks — the tool automatically applies the correct
    line ending for the platform and file type.  When overwriting an existing
    file, the original line ending style is preserved.  For new files, the
    line ending is chosen by file extension (e.g. ``.sh`` → LF, ``.bat`` →
    CRLF) and platform default (CRLF on Windows, LF on macOS/Linux).
    Only a UTF-16/UTF-32 file that carries a byte-order mark keeps its byte
    order and that mark; every other existing file is written as UTF-8.

    On POSIX, new regular files request mode 0644 and new shell/shebang scripts
    request mode 0755, with both filtered by the process umask. Overwriting an
    existing file preserves its user/group/other rwx bits.

    By default, refuses to overwrite existing files unless ``overwrite=true``.
    """
    return _write_file_impl(path, content, overwrite=overwrite)


_CONTEXT_LINES = 2
# A side of the snippet longer than this shows only its first and last halves.
_MAX_SNIPPET_SIDE_LINES = 30


def _format_diff_context(content: str, match_start: int, old_string: str, new_string: str) -> str:
    """Return a before/after snippet with line numbers and surrounding context."""
    lines = split_lines(content)

    # Find which lines the old_string spans by counting newlines before the match
    prefix = content[:match_start]
    old_start_line = prefix.count("\n")
    old_end_line = old_start_line + old_string.count("\n")

    ctx_start = max(0, old_start_line - _CONTEXT_LINES)
    ctx_end = min(len(lines), old_end_line + 1 + _CONTEXT_LINES)

    # "Before" — lines from original content
    before = _snippet_side(lines, ctx_start, ctx_end)

    # "After" — apply replacement and show the same region
    after_content = content[:match_start] + new_string + content[match_start + len(old_string) :]
    after_lines = split_lines(after_content)
    new_end_line = old_start_line + new_string.count("\n")
    after_ctx_end = min(len(after_lines), new_end_line + 1 + _CONTEXT_LINES)

    after = _snippet_side(after_lines, ctx_start, after_ctx_end)

    return f"Before:\n{before}\nAfter:\n{after}"


def _snippet_side(lines: list[str], start: int, end: int) -> str:
    """Number ``lines[start:end]``, eliding the middle of a side too long to read at a glance."""
    numbered = [f"  {index + 1}|{lines[index]}" for index in range(start, end)]
    # The marker takes a line's place, so hiding a single line would save nothing.
    if len(numbered) <= _MAX_SNIPPET_SIDE_LINES + 1:
        return "\n".join(numbered)
    keep = _MAX_SNIPPET_SIDE_LINES // 2
    first_omitted = start + keep + 1
    last_omitted = end - keep
    omitted = last_omitted - first_omitted + 1
    marker = f"  [... {omitted} lines omitted ({first_omitted}-{last_omitted}) ...]"
    return "\n".join([*numbered[:keep], marker, *numbered[-keep:]])


def _replace_surrogate_escapes(text: str) -> str:
    """Return display-safe text with one output character per input character.

    This must remain length-preserving: ``plan_edit_file`` passes a raw-content
    offset to ``_format_diff_context`` for indexing the display-safe copy.
    """
    return "".join("\ufffd" if "\ud800" <= char <= "\udfff" else char for char in text)


def plan_edit_file(
    path: str,
    old_string: str,
    new_string: str,
    replace_all: bool = False,
    *,
    base_cwd: str | None = None,
) -> EditFilePlan | FileToolPreviewError:
    """Prepare an ``edit_file`` operation in memory without writing the file."""
    from pathlib import Path

    from chrys.foundation.text.encoding import EncodingDetector

    detector = EncodingDetector()

    if is_document_artifact_handle(path):
        return FileToolPreviewError(
            kind="read_only_handle",
            message=tool_error(
                "session_artifact_handle_is_read_only",
                "session document handles are read-only; use a regular filesystem path for edit_file.",
                details={"path": path},
            ),
        )

    # An empty old_string "matches" between every two characters: it can
    # neither name one place nor create a file.
    if not old_string:
        return FileToolPreviewError(
            kind="empty_old_string",
            message=tool_error(
                "empty_old_string",
                "old_string is empty. To create a file or replace a file's whole content, use write_file "
                "(overwrite=true for an existing file). To insert text, put an existing neighbouring line "
                "in old_string and repeat it in new_string.",
                details={"path": path},
            ),
        )

    try:
        resolved = resolve_workspace_path(path, base_cwd=base_cwd)
        if not os.path.exists(resolved):
            return FileToolPreviewError(
                kind="missing",
                message=tool_error(
                    "file_not_found",
                    f"file not found — {resolved}",
                    details={"path": path, "resolved_path": resolved},
                ),
                resolved_path=resolved,
            )
        if os.path.isdir(resolved):
            return FileToolPreviewError(
                kind="directory",
                message=tool_error(
                    "path_is_directory",
                    f"path is a directory, not a file — {resolved}",
                    details={"path": path, "resolved_path": resolved},
                ),
                resolved_path=resolved,
            )

        detection = detector.detect_file(Path(resolved))
        if detection.is_binary:
            return FileToolPreviewError(
                kind="binary",
                message=tool_error(
                    "binary_file",
                    f"file appears to be binary — {resolved}",
                    details={"path": path, "resolved_path": resolved},
                ),
                resolved_path=resolved,
            )
        # Read once in binary, decode, and only then look at line endings:
        # a wide encoding spreads a newline over several bytes.
        with open(resolved, "rb") as f:
            raw = f.read()
        existing = _read_existing_text(raw, detection.encoding)
        encoding, decode_errors, bom = existing.encoding, existing.errors, existing.bom
        original_eol = _line_ending_for(raw, existing, resolved)
        content = normalize_line_endings(existing.text)

        normalized_old = normalize_line_endings(old_string)
        normalized_new = normalize_line_endings(new_string)

        if normalized_old == normalized_new:
            return FileToolPreviewError(
                kind="identical",
                message=tool_error("no_change", "old_string and new_string are identical — nothing to change."),
                resolved_path=resolved,
            )

        count = content.count(normalized_old)
        if count == 0:
            return FileToolPreviewError(
                kind="not_found",
                message=tool_error(
                    "old_string_not_found",
                    f"old_string not found in {resolved}. "
                    "Make sure the string matches exactly (including whitespace and indentation). "
                    "Do not include line number prefixes (e.g. '13|') from read_file output.",
                    details={"path": path, "resolved_path": resolved},
                ),
                resolved_path=resolved,
            )

        if not replace_all and count > 1:
            return FileToolPreviewError(
                kind="ambiguous",
                message=tool_error(
                    "ambiguous_match",
                    f"old_string appears {count} times in {resolved}. "
                    "Include more surrounding context in old_string to make the match unique, "
                    "or set replace_all=true to replace every occurrence.",
                    details={"path": path, "resolved_path": resolved, "matches": count},
                ),
                resolved_path=resolved,
            )

        match_start = content.find(normalized_old)
        display_content = _replace_surrogate_escapes(content)
        display_old = _replace_surrogate_escapes(normalized_old)
        display_new = _replace_surrogate_escapes(normalized_new)
        snippet = _format_diff_context(display_content, match_start, display_old, display_new)

        new_content = (
            content.replace(normalized_old, normalized_new)
            if replace_all
            else content.replace(normalized_old, normalized_new, 1)
        )
        write_content = new_content.replace("\n", original_eol) if original_eol != "\n" else new_content
        replacements = count if replace_all else 1
        return EditFilePlan(
            resolved_path=resolved,
            encoding=encoding,
            decode_errors=decode_errors,
            bom=bom,
            before_text=display_content,
            after_text=_replace_surrogate_escapes(new_content),
            write_content=write_content,
            replacements=replacements,
            snippet=snippet,
        )
    except Exception as e:
        return FileToolPreviewError(
            kind="prepare_failed",
            message=tool_error("edit_failed", f"failed to edit file — {e}", details={"path": path}),
        )


def _edit_file_impl(
    path: str,
    old_string: str,
    new_string: str,
    replace_all: bool = False,
    *,
    base_cwd: str | None = None,
) -> str:
    missing_base = missing_base_cwd_error(path, base_cwd)
    if missing_base is not None:
        return missing_base
    try:
        path = approved_write_path(path, base_cwd=base_cwd)
        with _fs_write_lock(path, base_cwd):
            plan = plan_edit_file(path, old_string, new_string, replace_all=replace_all, base_cwd=base_cwd)
            if isinstance(plan, FileToolPreviewError):
                return plan.message

            _atomic_write(
                plan.resolved_path,
                plan.write_content,
                encoding=plan.encoding,
                errors=plan.decode_errors,
                bom=plan.bom,
            )

        plural = "s" if plan.replacements != 1 else ""
        return (
            f"Replaced {plan.replacements} occurrence{plural} in {surrogate_safe_text(plan.resolved_path)}.\n"
            f"{plan.snippet}"
        )
    except Exception as e:
        return tool_error("edit_failed", f"failed to edit file — {e}", details={"path": path})


@tool(kind=KIND_FILESYSTEM_WRITE)
def edit_file(
    path: Annotated[str, "Absolute or relative path to the file to edit."],
    old_string: Annotated[
        str,
        "The exact text to find and replace. "
        "Match the file content exactly — preserve indentation (tabs/spaces) as shown "
        "by read_file. The line number prefix (e.g. '13|') is NOT part of the file "
        "content; never include it in old_string or new_string. "
        "When the text contains unicode escape sequences like \\u2192, "
        "use the actual Unicode character (e.g. →) instead of the escape "
        "sequence to avoid JSON encoding ambiguity.",
    ],
    new_string: Annotated[
        str,
        "The replacement text. "
        "Preserve the surrounding indentation style (tabs or spaces). "
        "Use actual Unicode characters instead of \\uXXXX escape sequences.",
    ],
    replace_all: Annotated[
        bool,
        "If true, replace all occurrences. If false (default), replace only "
        "one occurrence and require it to be unique.",
    ] = False,
) -> str:
    """Replace exact string occurrences in a file.

    Line endings are handled automatically — use ``\\n`` in old_string and
    new_string regardless of the file's actual line ending style.  The original
    line ending style is detected and preserved on write. UTF-16/UTF-32 files
    keep their byte order and BOM. On POSIX, the file's existing
    user/group/other rwx bits are also preserved.

    When ``replace_all=false`` (default), the ``old_string`` must appear exactly
    once. If it appears multiple times, the edit is rejected — include more
    surrounding context in ``old_string`` to make the match unique.
    """
    return _edit_file_impl(path, old_string, new_string, replace_all=replace_all)


class FilesystemTools:
    """Runtime-bound filesystem tools that resolve relative paths per session."""

    def __init__(
        self,
        runtime: SessionEnvironment,
        session_dir: Path | None = None,
        session_id: str = "",
        *,
        max_image_bytes: int = MAX_IMAGE_BYTES,
    ) -> None:
        self._runtime = runtime
        self._session_dir = resolve_tool_session_dir(
            runtime.platform.config_dir,
            session_id=session_id,
            session_dir=session_dir,
        )
        self._max_image_bytes = max_image_bytes

    def tools(self) -> list:
        """Return filesystem tools for this runtime context."""
        return [self.read_file, self.view_image, self.write_file, self.edit_file]

    def approval_targets(self, arguments: dict[str, object]) -> ApprovedTargets:
        """The entries a write/edit replaces; an unresolved path authorizes no write."""
        path = arguments.get("path")
        if isinstance(path, str) and is_document_artifact_handle(path):
            # The tool refuses to write a session document; nothing to pin.
            return ApprovedTargets()
        if not isinstance(path, str) or not path or "\0" in path:
            return ApprovedTargets(files=())
        try:
            return ApprovedTargets(files=(file_write_target(path, base_cwd=self._runtime.cwd),))
        except OSError, ValueError:
            return ApprovedTargets(files=())

    @tool(kind=KIND_FILESYSTEM_READ)
    def read_file(
        self,
        path: Annotated[str, "Absolute or relative path to the file to read."],
        max_tokens: _ReadMaxTokens = _DEFAULT_MAX_TOKENS,
        offset: _ReadOffset = _DEFAULT_READ_OFFSET,
        limit: _ReadLimit = _DEFAULT_READ_LIMIT,
    ) -> str:
        """Read a text file with line numbers. For lines 10-20, use offset=10, limit=11."""
        return _read_file_impl(
            path,
            max_tokens=max_tokens,
            offset=offset,
            limit=limit,
            base_cwd=self._runtime.cwd,
            session_dir=self._session_dir,
        )

    @tool(kind=KIND_FILESYSTEM_READ)
    def view_image(
        self,
        path: Annotated[str, "Absolute or relative path to the image file to inspect."],
    ) -> list[Content]:
        """Read an image file and return it as model-visible image content.

        Images the user attached to a message are already visible to you, at the
        same size this tool returns; call it for one only when it is not in the
        conversation.
        """
        return _view_image_impl(
            path, base_cwd=self._runtime.cwd, session_dir=self._session_dir, max_image_bytes=self._max_image_bytes
        )

    @tool(kind=KIND_FILESYSTEM_WRITE)
    def write_file(
        self,
        path: Annotated[str, "Absolute or relative path to the file to write."],
        content: Annotated[str, "The full file content to write."],
        overwrite: Annotated[
            bool,
            "Set to true to overwrite an existing file. "
            "If false (default) and the file already exists, "
            "the write will be rejected.",
        ] = False,
    ) -> str:
        """Write content to a file. Creates parent directories if needed.

        Use ``\\n`` for line breaks — the tool automatically applies the correct
        line ending for the platform and file type.  When overwriting an existing
        file, the original line ending style is preserved.  For new files, the
        line ending is chosen by file extension (e.g. ``.sh`` → LF, ``.bat`` →
        CRLF) and platform default (CRLF on Windows, LF on macOS/Linux).
        Only a UTF-16/UTF-32 file that carries a byte-order mark keeps its byte
        order and that mark; every other existing file is written as UTF-8.

        On POSIX, new regular files request mode 0644 and new shell/shebang scripts
        request mode 0755, with both filtered by the process umask. Overwriting an
        existing file preserves its user/group/other rwx bits.

        By default, refuses to overwrite existing files unless ``overwrite=true``.
        """
        return _write_file_impl(path, content, overwrite=overwrite, base_cwd=self._runtime.cwd)

    @tool(kind=KIND_FILESYSTEM_WRITE)
    def edit_file(
        self,
        path: Annotated[str, "Absolute or relative path to the file to edit."],
        old_string: Annotated[
            str,
            "The exact text to find and replace. "
            "Match the file content exactly — preserve indentation (tabs/spaces) as shown "
            "by read_file. The line number prefix (e.g. '13|') is NOT part of the file "
            "content; never include it in old_string or new_string. "
            "When the text contains unicode escape sequences like \\u2192, "
            "use the actual Unicode character (e.g. →) instead of the escape "
            "sequence to avoid JSON encoding ambiguity.",
        ],
        new_string: Annotated[
            str,
            "The replacement text. "
            "Preserve the surrounding indentation style (tabs or spaces). "
            "Use actual Unicode characters instead of \\uXXXX escape sequences.",
        ],
        replace_all: Annotated[
            bool,
            "If true, replace all occurrences. If false (default), replace only "
            "one occurrence and require it to be unique.",
        ] = False,
    ) -> str:
        """Replace exact string occurrences in a file.

        Line endings are handled automatically — use ``\\n`` in old_string and
        new_string regardless of the file's actual line ending style.  The original
        line ending style is detected and preserved on write. UTF-16/UTF-32 files
        keep their byte order and BOM. On POSIX, the file's existing
        user/group/other rwx bits are also preserved.

        When ``replace_all=false`` (default), the ``old_string`` must appear exactly
        once. If it appears multiple times, the edit is rejected — include more
        surrounding context in ``old_string`` to make the match unique.
        """
        return _edit_file_impl(path, old_string, new_string, replace_all=replace_all, base_cwd=self._runtime.cwd)
