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
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated

from chrys.foundation.platform import get_platform
from chrys.foundation.platform.files import surrogate_safe_text
from chrys.foundation.platform.paths import resolve_existing_path, resolve_workspace_path
from chrys.foundation.text.images import ImageProcessingError, load_image_file
from chrys.kernel import Content
from chrys.service.tools.file_approval import FileWriteTarget, approved_write_path, file_write_target
from chrys.service.tools.kinds import KIND_FILESYSTEM_READ, KIND_FILESYSTEM_WRITE, tool
from chrys.service.tools.result_metadata import tool_error
from chrys.service.tools.session_artifacts import (
    is_document_artifact_handle,
    resolve_document_image_artifact_handle,
    resolve_document_markdown_artifact_handle,
    resolve_tool_session_dir,
)

if TYPE_CHECKING:
    from pathlib import Path

    from chrys.foundation.models.session_env import SessionEnvironment

_DEFAULT_MAX_TOKENS = 5000
_MAX_LINE_DISPLAY_CHARS = 2048
_WINDOWS_REPLACE_MAX_ATTEMPTS = 6
_WINDOWS_REPLACE_RETRY_DELAY_SECONDS = 0.01
_NEW_FILE_MODE = 0o644
_NEW_EXECUTABLE_FILE_MODE = 0o755
_ATOMIC_TEMP_CREATE_ATTEMPTS = 100
_SHELL_SCRIPT_EXTENSIONS = frozenset({".sh", ".bash", ".zsh", ".fish", ".ksh", ".csh", ".tcsh", ".command"})


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


@dataclass(slots=True)
class EditFilePlan:
    """Prepared edit_file operation with before/after preview and write data."""

    resolved_path: str
    encoding: str
    decode_errors: str
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


def _atomic_write(path: str, content: str, encoding: str = "utf-8", errors: str = "strict") -> None:
    """Write *content* to *path* atomically via temp-file + rename.

    Writes to a temporary file in the same directory (same filesystem) and
    then calls ``os.replace()`` which is atomic on POSIX and Windows/NTFS.
    If the write or flush fails, the temp file is removed and the original
    file is left untouched.
    """
    path = approved_write_path(path)
    parent = os.path.dirname(path) or "."
    platform = get_platform()
    target_mode: int | None = None
    preserve_existing_mode = False
    if not platform.is_windows:
        target_mode, preserve_existing_mode = _posix_write_mode(path, content)
    fd = None
    tmp_path = ""
    try:
        if platform.is_windows:
            fd, tmp_path = tempfile.mkstemp(dir=parent, suffix=".tmp")
        else:
            if target_mode is None:
                raise RuntimeError("A POSIX atomic write requires a target file mode.")
            fd, tmp_path = _open_posix_atomic_temp(parent, target_mode)
        with os.fdopen(fd, "w", encoding=encoding, errors=errors, newline="") as f:
            fd = None  # os.fdopen takes ownership of the fd
            f.write(content)
            f.flush()
            if preserve_existing_mode:
                # Existing modes override umask and must be restored exactly.
                # This must run while fdopen still owns an open descriptor.
                if target_mode is None:
                    raise RuntimeError("A POSIX atomic write requires a target file mode.")
                os.fchmod(f.fileno(), target_mode)
            os.fsync(f.fileno())
        for attempt in range(_WINDOWS_REPLACE_MAX_ATTEMPTS):
            try:
                os.replace(tmp_path, path)
                break
            except PermissionError:
                if not platform.is_windows or attempt == _WINDOWS_REPLACE_MAX_ATTEMPTS - 1:
                    raise
                time.sleep(_WINDOWS_REPLACE_RETRY_DELAY_SECONDS * 2**attempt)
    except BaseException:
        # Clean up the temp file on any failure
        if fd is not None:
            os.close(fd)
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise


def _detect_line_ending(raw: bytes, path: str = "") -> str:
    """Detect the predominant line ending in raw file bytes.

    Falls back to ``_default_eol_for_new_file(path)`` when the file contains
    no line endings (e.g. single-line or empty files).
    """
    crlf = raw.count(b"\r\n")
    cr = raw.count(b"\r") - crlf  # standalone \r only
    lf = raw.count(b"\n") - crlf  # standalone \n only
    if crlf >= lf and crlf >= cr and crlf > 0:
        return "\r\n"
    if cr > lf and cr > 0:
        return "\r"
    if lf > 0:
        return "\n"
    # No line endings found — use the smart default for this file type / platform
    return _default_eol_for_new_file(path)


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


def _normalize_tool_line_endings(text: str) -> str:
    """Normalize LLM-supplied text to the internal LF-only representation."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _decode_write_preview_before(raw: bytes) -> tuple[str, str]:
    """Return display-safe existing content for overwrite approval previews."""
    from chrys.foundation.text.encoding import EncodingDetector

    if EncodingDetector.looks_binary(raw):
        return "", f"content-only; existing binary file ({len(raw)} bytes) not shown"

    detector = EncodingDetector()
    detection = detector.detect_from_bytes(raw)
    if detection.is_binary:
        return "", f"content-only; existing binary file ({len(raw)} bytes) not shown"

    encoding = detection.encoding or "utf-8"
    normalized_encoding = encoding.lower().replace("_", "-")
    decode_errors = "replace" if normalized_encoding.startswith(("utf-16", "utf-32")) else "surrogateescape"
    try:
        before_text = raw.decode(encoding, errors=decode_errors)
    except LookupError, UnicodeDecodeError:
        return "", "content-only; existing file could not be decoded"
    return _replace_surrogate_escapes(_normalize_tool_line_endings(before_text)), ""


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
        overwrites_existing = overwrite and os.path.isfile(resolved)
        # Determine line ending: preserve existing file's style, or pick a smart default.
        if overwrites_existing:
            with open(resolved, "rb") as f:
                raw = f.read()
            eol = _detect_line_ending(raw, resolved)
            before_display_content, content_only_reason = _decode_write_preview_before(raw)
        else:
            eol = _default_eol_for_new_file(resolved)

        write_content = _normalize_tool_line_endings(content)
        if eol != "\n":
            write_content = write_content.replace("\n", eol)
        return WriteFilePlan(
            resolved_path=resolved,
            write_content=write_content,
            display_content=_replace_surrogate_escapes(write_content),
            before_display_content=before_display_content,
            overwrites_existing=overwrites_existing,
            content_only_reason=content_only_reason,
        )
    except Exception as e:
        return FileToolPreviewError(
            kind="prepare_failed",
            message=tool_error("write_failed", f"failed to write file — {e}", details={"path": path}),
        )


def _read_file_impl(
    path: str,
    max_tokens: int = _DEFAULT_MAX_TOKENS,
    line_range: list[int] | None = None,
    *,
    base_cwd: str | None = None,
    session_dir: Path | None = None,
) -> str:
    from pathlib import Path

    from chrys.foundation.text.encoding import EncodingDetector
    from chrys.foundation.text.tokenizer import MixedLanguageTokenizer

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

        lines = content.splitlines()
        total_lines = len(lines)
        total_chars = len(content)

        # Validate and apply line_range
        offset = 0  # 0-indexed offset into `lines`
        if line_range is not None:
            if len(line_range) != 2:
                return tool_error(
                    "invalid_line_range",
                    "line_range must be a list of exactly 2 integers [start, end].",
                    details={"line_range": line_range},
                )
            start, end = line_range
            if start < 1:
                return tool_error(
                    "invalid_line_range",
                    "line_range start must be >= 1.",
                    details={"line_range": line_range},
                )
            if end != -1 and end < start:
                return tool_error(
                    "invalid_line_range",
                    "line_range end must be >= start or -1 for EOF.",
                    details={"line_range": line_range},
                )
            if start > total_lines:
                return tool_error(
                    "invalid_line_range",
                    f"line_range start ({start}) is beyond the end of the file ({total_lines} lines).",
                    details={"line_range": line_range, "total_lines": total_lines},
                )
            offset = start - 1
            end_idx = total_lines if end == -1 else min(end, total_lines)
            lines = lines[offset:end_idx]

        header = f"File: {display_resolved} ({total_lines} lines, {total_chars} chars)\n"
        header_tokens = tokenizer.count_tokens(header)
        remaining = max_tokens - header_tokens

        output_lines: list[str] = []
        truncated = False
        last_line_num = 0
        long_lines: list[tuple[int, int]] = []

        for i, line in enumerate(lines):
            line_num = offset + i + 1
            if line_range is None and len(line) > _MAX_LINE_DISPLAY_CHARS:
                long_lines.append((line_num, len(line)))
                line = line[:_MAX_LINE_DISPLAY_CHARS] + "... [truncated]"
            formatted = f"{line_num}|{line}\n"
            line_tokens = tokenizer.count_tokens(formatted)
            if line_tokens > remaining:
                # Include a partial line instead of dropping it entirely
                if remaining > 0:
                    fraction = remaining / line_tokens
                    cut = max(int(len(line) * fraction), 1)
                    output_lines.append(f"{line_num}|{line[:cut]}... [truncated]\n")
                    # Record for targeted reads so the LLM knows the actual length.
                    # Without line_range the char-level truncation (above) or the
                    # standard "[Truncated at line N]" message already covers it.
                    if line_range is not None:
                        long_lines.append((line_num, len(line)))
                    last_line_num = line_num
                truncated = True
                if last_line_num == 0:
                    last_line_num = line_num - 1 if i > 0 else offset
                break
            remaining -= line_tokens
            output_lines.append(formatted)
            last_line_num = line_num

        result = header + "".join(output_lines)

        if long_lines:
            details = ", ".join(f"line {ln} ({length} chars)" for ln, length in long_lines)
            result += (
                f"\n[Long lines truncated to {_MAX_LINE_DISPLAY_CHARS} chars: {details}."
                " Use line_range to read specific lines in full.]"
            )

        if truncated:
            next_line = last_line_num + 1
            result += (
                f"\n[Truncated at line {last_line_num}, file has {total_lines} lines. "
                f"Content exceeds max_tokens={max_tokens}. "
                f"Use line_range=[{next_line}, -1] to continue reading.]"
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
    max_tokens: Annotated[
        int,
        "Token budget for the returned content. Large files are truncated to fit.",
    ] = _DEFAULT_MAX_TOKENS,
    line_range: Annotated[
        list[int] | None,
        "Optional [start, end] 1-indexed line range. Use -1 for end to read to EOF.",
    ] = None,
) -> str:
    """Read the contents of a text file and return them with line numbers."""
    return _read_file_impl(path, max_tokens=max_tokens, line_range=line_range)


def _view_image_impl(
    path: str,
    *,
    base_cwd: str | None = None,
    session_dir: Path | None = None,
) -> list[Content]:
    from pathlib import Path

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

        loaded = load_image_file(source_path)
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
    """Read an image file and return it as model-visible image content."""
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

            _atomic_write(plan.resolved_path, plan.write_content)
        return (
            f"Written {len(content)} chars ({len(content.splitlines())} lines) "
            f"to {surrogate_safe_text(plan.resolved_path)}."
        )
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

    On POSIX, new regular files request mode 0644 and new shell/shebang scripts
    request mode 0755, with both filtered by the process umask. Overwriting an
    existing file preserves its user/group/other rwx bits.

    By default, refuses to overwrite existing files unless ``overwrite=true``.
    """
    return _write_file_impl(path, content, overwrite=overwrite)


_CONTEXT_LINES = 2


def _format_diff_context(content: str, match_start: int, old_string: str, new_string: str) -> str:
    """Return a before/after snippet with line numbers and surrounding context."""
    lines = content.splitlines()

    # Find which lines the old_string spans by counting newlines before the match
    prefix = content[:match_start]
    old_start_line = prefix.count("\n")
    old_end_line = old_start_line + old_string.count("\n")

    ctx_start = max(0, old_start_line - _CONTEXT_LINES)
    ctx_end = min(len(lines), old_end_line + 1 + _CONTEXT_LINES)

    # "Before" — lines from original content
    before = "\n".join(f"  {ctx_start + i + 1}|{lines[ctx_start + i]}" for i in range(ctx_end - ctx_start))

    # "After" — apply replacement and show the same region
    after_content = content[:match_start] + new_string + content[match_start + len(old_string) :]
    after_lines = after_content.splitlines()
    new_end_line = old_start_line + new_string.count("\n")
    after_ctx_end = min(len(after_lines), new_end_line + 1 + _CONTEXT_LINES)

    after = "\n".join(f"  {ctx_start + i + 1}|{after_lines[ctx_start + i]}" for i in range(after_ctx_end - ctx_start))

    return f"Before:\n{before}\nAfter:\n{after}"


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
        encoding = detection.encoding or "utf-8"

        # Read once in binary — detect EOL from raw bytes, then decode.
        with open(resolved, "rb") as f:
            raw = f.read()
        original_eol = _detect_line_ending(raw, resolved)
        decode_errors = "replace" if encoding.startswith(("utf-16", "utf-32")) else "surrogateescape"
        content = _normalize_tool_line_endings(raw.decode(encoding, errors=decode_errors))

        normalized_old = _normalize_tool_line_endings(old_string)
        normalized_new = _normalize_tool_line_endings(new_string)

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
    try:
        path = approved_write_path(path, base_cwd=base_cwd)
        with _fs_write_lock(path, base_cwd):
            plan = plan_edit_file(path, old_string, new_string, replace_all=replace_all, base_cwd=base_cwd)
            if isinstance(plan, FileToolPreviewError):
                return plan.message

            _atomic_write(plan.resolved_path, plan.write_content, encoding=plan.encoding, errors=plan.decode_errors)

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
    line ending style is detected and preserved on write. On POSIX, the file's
    existing user/group/other rwx bits are also preserved.

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
    ) -> None:
        self._runtime = runtime
        self._session_dir = resolve_tool_session_dir(
            runtime.platform.config_dir,
            session_id=session_id,
            session_dir=session_dir,
        )

    def tools(self) -> list:
        """Return filesystem tools for this runtime context."""
        return [self.read_file, self.view_image, self.write_file, self.edit_file]

    def approval_targets(self, arguments: dict[str, object]) -> tuple[FileWriteTarget, ...]:
        """One-time targets, including final symlinks; unresolved paths authorize nothing."""
        path = arguments.get("path")
        if not isinstance(path, str) or not path or "\0" in path:
            return ()
        try:
            return (file_write_target(path, base_cwd=self._runtime.cwd),)
        except OSError, ValueError:
            return ()

    def affected_paths(self, arguments: dict[str, object]) -> tuple[str, ...] | None:
        """Reusable physical destinations exclude mixed read/replace symlink operations."""
        targets = self.approval_targets(arguments)
        if not targets or any(target.link_target is not None for target in targets):
            return None
        return tuple(target.path for target in targets)

    @tool(kind=KIND_FILESYSTEM_READ)
    def read_file(
        self,
        path: Annotated[str, "Absolute or relative path to the file to read."],
        max_tokens: Annotated[
            int,
            "Token budget for the returned content. Large files are truncated to fit.",
        ] = _DEFAULT_MAX_TOKENS,
        line_range: Annotated[
            list[int] | None,
            "Optional [start, end] 1-indexed line range. Use -1 for end to read to EOF.",
        ] = None,
    ) -> str:
        """Read the contents of a text file and return them with line numbers."""
        return _read_file_impl(
            path,
            max_tokens=max_tokens,
            line_range=line_range,
            base_cwd=self._runtime.cwd,
            session_dir=self._session_dir,
        )

    @tool(kind=KIND_FILESYSTEM_READ)
    def view_image(
        self,
        path: Annotated[str, "Absolute or relative path to the image file to inspect."],
    ) -> list[Content]:
        """Read an image file and return it as model-visible image content."""
        return _view_image_impl(path, base_cwd=self._runtime.cwd, session_dir=self._session_dir)

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
        line ending style is detected and preserved on write. On POSIX, the file's
        existing user/group/other rwx bits are also preserved.

        When ``replace_all=false`` (default), the ``old_string`` must appear exactly
        once. If it appears multiple times, the edit is rejected — include more
        surrounding context in ``old_string`` to make the match unique.
        """
        return _edit_file_impl(path, old_string, new_string, replace_all=replace_all, base_cwd=self._runtime.cwd)
