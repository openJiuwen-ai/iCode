# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""DocConverter tools — convert PDF and Office documents to Markdown.

Instance-based tool that uses lightweight Python libraries (pypdf,
python-docx, python-pptx, openpyxl, xlrd) to convert documents
into Markdown.  For small documents (below the token threshold), the full
    Markdown is returned inline.  For large documents, the output is saved to
    the session folder and a TOC with metadata is returned so the agent can
    selectively read sections through a session handle or absolute path.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated

from chrys.foundation.platform.files import atomic_write_owner_only_bytes, is_utf8_encodable, surrogate_safe_text
from chrys.foundation.platform.paths import resolve_existing_path, resolve_workspace_path
from chrys.foundation.text.lines import normalize_line_endings, split_lines
from chrys.foundation.text.tokenizer import MixedLanguageTokenizer
from chrys.service.tools.builtins.doc_converter.artifacts import (
    DocumentImageSink,
    artifact_basename_within_limit,
    bounded_artifact_stem,
    prepare_document_artifact_root,
)
from chrys.service.tools.builtins.doc_converter.parsers.base import DocParser, ParsedDocument, VisualOccurrence
from chrys.service.tools.kinds import KIND_DOC_CONVERTER, tool
from chrys.service.tools.result_metadata import tool_error
from chrys.service.tools.session_artifacts import make_document_artifact_handle, resolve_tool_session_dir
from chrys.service.tools.spill import run_spill_finalizer
from chrys.service.tools.workspace_paths import missing_base_cwd_error

if TYPE_CHECKING:
    from pathlib import Path

    from chrys.foundation.models.session_env import SessionEnvironment

_TOKEN_THRESHOLD = 10_000
"""Documents with token counts at or above this threshold are saved to disk."""

_TOC_MAX_TOKENS = 5_000
"""Maximum token budget for the extracted table of contents."""

_VISUAL_PREVIEW_LIMIT = 20
"""Fixed number of visual occurrences shown beside a saved large document."""

_tokenizer = MixedLanguageTokenizer()

_OUTPUT_DIRECTORY_LOCKS: dict[str, threading.Lock] = {}
_OUTPUT_DIRECTORY_LOCKS_GUARD = threading.Lock()


@dataclass(frozen=True, slots=True)
class _SavedMarkdownArtifact:
    """A fork-local handle plus the concrete path written for this session."""

    handle: str
    path: str


def _extract_toc(markdown: str) -> str:
    """Extract a table of contents from Markdown headings with line numbers.

    If the TOC exceeds ``_TOC_MAX_TOKENS`` tokens, it is truncated from the
    bottom with a notice showing how many entries were omitted.
    """
    toc_lines: list[str] = []
    for i, line in enumerate(split_lines(markdown), 1):
        stripped = line.strip()
        if stripped.startswith("#"):
            level = len(stripped) - len(stripped.lstrip("#"))
            title = stripped.lstrip("#").strip()
            if title:  # skip lines that are just "#" with no text
                indent = "  " * (level - 1)
                toc_lines.append(f"{i}|{indent}- {title}")

    if not toc_lines:
        return "(no headings found)"

    # Truncate from the bottom if TOC exceeds token budget
    used = 0
    keep = 0
    for entry in toc_lines:
        entry_tokens = _tokenizer.count_tokens(entry) + 1  # +1 for newline
        if used + entry_tokens > _TOC_MAX_TOKENS:
            break
        used += entry_tokens
        keep += 1

    if keep < len(toc_lines):
        omitted = len(toc_lines) - keep
        truncated = toc_lines[:keep]
        truncated.append(f"[...truncated {omitted} more heading{'s' if omitted != 1 else ''}]")
        return "\n".join(truncated)

    return "\n".join(toc_lines)


def _sanitize_filename(name: str) -> str:
    """Return the shared byte-bounded source stem for generated artifacts."""
    return bounded_artifact_stem(name)


def _normalize_document_text(text: str) -> str:
    """Repair split UTF-16 pairs and replace unpaired surrogates.

    Document parsers can return surrogate code points even though Python text
    normally contains Unicode scalar values.  In particular, PDF ToUnicode
    maps may expose the two UTF-16 code units of one non-BMP character as
    adjacent Python characters.  Round-tripping only surrogate-bearing text
    through UTF-16 repairs valid pairs and replaces malformed leftovers while
    leaving ordinary text on the fast path.
    """
    if is_utf8_encodable(text):
        return text
    return text.encode("utf-16-le", errors="surrogatepass").decode("utf-16-le", errors="replace")


def _normalize_parsed_document(parsed: ParsedDocument) -> ParsedDocument:
    """Normalize every parser-derived string at the tool-output boundary."""
    return ParsedDocument(
        markdown=_normalize_document_text(parsed.markdown),
        visuals=tuple(
            VisualOccurrence(
                location=_normalize_document_text(visual.location),
                ordinal=visual.ordinal,
                reference=_normalize_document_text(visual.reference),
                source_name=_normalize_document_text(visual.source_name),
            )
            for visual in parsed.visuals
        ),
        warnings=tuple(_normalize_document_text(warning) for warning in parsed.warnings),
    )


def _write_file(path: str, content: str) -> None:
    """Atomically write owner-only UTF-8 text while remaining total on surrogates."""
    from pathlib import Path

    atomic_write_owner_only_bytes(Path(path), content.encode("utf-8", errors="backslashreplace"))


def _unique_path(directory: str, stem: str, suffix: str) -> str:
    """Return a non-colliding path under *directory*."""
    safe_stem = bounded_artifact_stem(stem)
    candidate = os.path.join(directory, f"{safe_stem}{suffix}")
    if not artifact_basename_within_limit(os.path.basename(candidate)):
        candidate = os.path.join(directory, f"document{suffix}")
    if not os.path.exists(candidate):
        return candidate
    for i in range(1, 1000):
        candidate = os.path.join(directory, f"{safe_stem}_{i}{suffix}")
        if not artifact_basename_within_limit(os.path.basename(candidate)):
            candidate = os.path.join(directory, f"document_{i}{suffix}")
        if not os.path.exists(candidate):
            return candidate
    return candidate  # fallback — extremely unlikely


def _output_directory_lock(directory: str) -> threading.Lock:
    key = os.path.normcase(os.path.abspath(directory))
    with _OUTPUT_DIRECTORY_LOCKS_GUARD:
        lock = _OUTPUT_DIRECTORY_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _OUTPUT_DIRECTORY_LOCKS[key] = lock
        return lock


def _write_unique_markdown(directory: str, stem: str, markdown: str) -> _SavedMarkdownArtifact:
    """Choose and publish one Markdown artifact without an in-process name race."""
    from pathlib import Path

    artifact_root = prepare_document_artifact_root(Path(directory))
    canonical_directory = os.fspath(artifact_root)
    with _output_directory_lock(canonical_directory):
        saved_path = _unique_path(canonical_directory, stem, ".md")
        saved_handle = make_document_artifact_handle(os.path.basename(saved_path))
        _write_file(saved_path, markdown)
    return _SavedMarkdownArtifact(handle=saved_handle, path=os.path.abspath(saved_path))


def _render_visual_entries(visuals: tuple[VisualOccurrence, ...]) -> list[str]:
    """Render stable, directly callable ``view_image`` argument entries."""
    return [
        f"- {visual.location} / image {visual.ordinal}: {json.dumps({'path': visual.reference}, ensure_ascii=False)}"
        for visual in visuals
    ]


def _render_visual_section(visuals: tuple[VisualOccurrence, ...]) -> str:
    """Render the complete visual index, or an empty string when none exist."""
    if not visuals:
        return ""
    lines = ["## Extracted visual assets", *_render_visual_entries(visuals)]
    lines.extend(
        [
            "",
            "Use view_image with one of the path arguments above when visual inspection is needed.",
        ]
    )
    return "\n".join(lines)


def _render_visual_preview(visuals: tuple[VisualOccurrence, ...]) -> str:
    """Render the fixed-size large-result visual preview."""
    if not visuals:
        return ""
    shown = visuals[:_VISUAL_PREVIEW_LIMIT]
    lines = ["## Extracted visual assets (preview)", *_render_visual_entries(shown)]
    omitted = len(visuals) - len(shown)
    if omitted:
        lines.append(f"- [...{omitted} more image occurrence(s); the saved Markdown contains the complete index.]")
    lines.extend(
        [
            "",
            "Use view_image with one of the path arguments above when visual inspection is needed.",
        ]
    )
    return "\n".join(lines)


def _render_warning_section(warnings: tuple[str, ...]) -> str:
    """Render bounded image-extraction warnings."""
    if not warnings:
        return ""
    return "## Image extraction warnings\n" + "\n".join(f"- {warning}" for warning in warnings)


def _compose_document_markdown(parsed: ParsedDocument) -> str:
    """Append enabled-only image warnings and the complete visual index."""
    if not parsed.warnings and not parsed.visuals:
        return parsed.markdown
    parts = [parsed.markdown.rstrip(), _render_warning_section(parsed.warnings), _render_visual_section(parsed.visuals)]
    return "\n\n".join(part for part in parts if part)


class DocConverterTools:
    """Instance-based document conversion tool backed by SessionEnvironment.

    Converts PDF and Office documents to Markdown using lightweight Python
    libraries.  For small documents (below the token threshold) the Markdown
    is returned directly.  For large documents the output is saved to the
    session directory and a table of contents with metadata is returned.

    Usage::

        runtime = SessionEnvironment.capture()
        dc = DocConverterTools(runtime, session_id="abc123")
        tools = dc.tools()  # -> [dc.convert_document]
    """

    def __init__(self, runtime: SessionEnvironment, session_id: str = "", session_dir: Path | None = None) -> None:
        self._runtime = runtime
        self._image_extraction_enabled = False
        self._session_dir = resolve_tool_session_dir(
            runtime.platform.config_dir,
            session_id=session_id,
            session_dir=session_dir,
        )

    def set_image_extraction_enabled(self, enabled: bool) -> None:
        """Enable image extraction for future invocations when storage is available."""
        self._image_extraction_enabled = enabled

    def tools(self) -> list:
        """Return all tool instances bound to this object."""
        return [self.convert_document]

    def _session_doc_converter_dir(self) -> str | None:
        """Compute the doc_converter output directory under the session folder."""
        if self._session_dir is None:
            return None
        return str(self._session_dir / "doc_converter")

    def _convert_document_sync(
        self,
        parser: DocParser,
        resolved: str,
        original_path: str,
        display_resolved: str,
        display_ext: str,
        extension: str,
        extract_images: bool,
    ) -> str:
        """Run parse-through-final-result work as one cancellation-safe transaction."""
        from pathlib import Path

        sink: DocumentImageSink | None = None
        out_dir = self._session_doc_converter_dir()
        if extract_images and out_dir is not None:
            source_stem = os.path.splitext(os.path.basename(resolved))[0]
            sink = DocumentImageSink(Path(out_dir), source_stem=source_stem)

        try:
            parsed = parser.parse(resolved, image_sink=sink)
            if sink is not None:
                sink_warnings = tuple(warning for warning in sink.warnings if warning not in parsed.warnings)
                parsed = ParsedDocument(
                    markdown=parsed.markdown,
                    visuals=parsed.visuals,
                    warnings=(*parsed.warnings, *sink_warnings),
                )
            parsed = _normalize_parsed_document(parsed)
        except Exception as exc:
            if sink is not None:
                sink.abort()
            return tool_error(
                "document_conversion_failed",
                f"failed to convert document — {surrogate_safe_text(str(exc))}",
                details={"path": original_path, "resolved_path": resolved, "extension": extension},
            )

        try:
            result = self._render_result(
                parsed,
                resolved=resolved,
                display_resolved=display_resolved,
                out_dir=out_dir,
            )
        except (OSError, ValueError) as exc:
            if sink is not None:
                sink.abort()
            return tool_error(
                "document_save_failed",
                f"failed to save converted document — {surrogate_safe_text(str(exc))}",
                details={"path": original_path, "resolved_path": resolved, "output_directory": out_dir},
            )
        except Exception as exc:
            if sink is not None:
                sink.abort()
            return tool_error(
                "document_conversion_failed",
                f"failed to convert document — {surrogate_safe_text(str(exc))}",
                details={"path": original_path, "resolved_path": resolved, "extension": extension},
            )

        if sink is not None:
            sink.commit_occurrences(parsed.visuals)
        return result

    def _render_result(
        self,
        parsed: ParsedDocument,
        *,
        resolved: str,
        display_resolved: str,
        out_dir: str | None,
    ) -> str:
        """Render inline output or save complete Markdown and return its stable preview."""
        # LF-only, as read_file reads the saved copy back, so the table of
        # contents and the line count match its line numbers.
        markdown = normalize_line_endings(_compose_document_markdown(parsed))
        if not markdown.strip():
            return f"File: {display_resolved}\n(document converted but produced no text content)"

        line_count = len(split_lines(markdown))
        char_count = len(markdown)
        token_count = _tokenizer.count_tokens(markdown)
        header = f"File: {display_resolved} ({line_count} lines, {char_count} chars, ~{token_count} tokens)\n"
        if token_count < _TOKEN_THRESHOLD:
            return header + markdown

        if out_dir is None:
            return (
                header
                + f"[Document too large to return inline ({token_count} tokens). "
                + "No session directory available to save output.]\n\n"
                + "## Table of Contents\n"
                + _extract_toc(markdown)
            )

        basename = os.path.splitext(os.path.basename(resolved))[0]
        saved = _write_unique_markdown(out_dir, _sanitize_filename(basename), markdown)
        saved_path = saved.path if is_utf8_encodable(saved.path) else None

        preview_sections = [
            _render_warning_section(parsed.warnings),
            _render_visual_preview(parsed.visuals),
            "## Table of Contents\n" + _extract_toc(markdown),
        ]
        preview = "\n\n".join(section for section in preview_sections if section)
        saved_location = f"Saved Markdown handle: {saved.handle}\n"
        summary_location = f"- Saved handle: {saved.handle}\n"
        access_instruction = "Use the handle above as a session-bound read_file path argument with offset and limit."
        if saved_path is not None:
            saved_location += f"Saved Markdown path: {saved_path}\n"
            summary_location += f"- Saved path: {saved_path}\n"
            access_instruction = (
                "Use the handle above with session-bound read_file, or use the absolute path with any available "
                "filesystem or shell tool."
            )
        return (
            header
            + "Document is too large to return inline.\n"
            + saved_location
            + "\n"
            + "## Summary\n"
            + f"- Lines: {line_count}\n"
            + f"- Characters: {char_count}\n"
            + f"- Tokens: ~{token_count}\n"
            + summary_location
            + "\n"
            + preview
            + "\n\n"
            + access_instruction
        )

    @tool(kind=KIND_DOC_CONVERTER)
    async def convert_document(
        self,
        path: Annotated[
            str,
            "Absolute or relative path to the document to convert. Supported formats: PDF, DOCX, PPTX, XLSX, XLS.",
        ],
    ) -> str:
        """Convert PDF/Office files to Markdown, not page or slide layouts.

        Returns a session handle and, when representable as strict UTF-8, an absolute path.
        Small output is inline; large output has a table of contents for read_file with
        offset/limit. With view_image, PDF/DOCX/PPTX raster images are lazy references;
        XLS/XLSX are text and tables only."""
        from chrys.service.tools.builtins.doc_converter.registry import get_parser, supported_extensions

        # Resolve and validate
        missing_base = missing_base_cwd_error(path, self._runtime.cwd)
        if missing_base is not None:
            return missing_base
        resolved = resolve_existing_path(path, base_cwd=self._runtime.cwd)
        if resolved is None:
            resolved = resolve_workspace_path(path, base_cwd=self._runtime.cwd)
            display_resolved = surrogate_safe_text(resolved)
            if os.path.isdir(resolved):
                return tool_error(
                    "path_is_directory",
                    f"path is a directory, not a file — {display_resolved}",
                    details={"path": path, "resolved_path": resolved},
                )
            return tool_error(
                "file_not_found",
                f"file not found — {display_resolved}",
                details={"path": path, "resolved_path": resolved},
            )
        display_resolved = surrogate_safe_text(resolved)
        if not os.path.isfile(resolved):
            if os.path.isdir(resolved):
                return tool_error(
                    "path_is_directory",
                    f"path is a directory, not a file — {display_resolved}",
                    details={"path": path, "resolved_path": resolved},
                )
            return tool_error(
                "file_not_found",
                f"file not found — {display_resolved}",
                details={"path": path, "resolved_path": resolved},
            )

        ext = os.path.splitext(resolved)[1].lower()
        display_ext = surrogate_safe_text(ext)
        parser = get_parser(ext)
        if parser is None:
            exts = supported_extensions()
            return tool_error(
                "unsupported_file_format",
                f"unsupported file format '{display_ext}'. Supported: {', '.join(sorted(exts))}",
                details={"path": path, "resolved_path": resolved, "extension": ext},
            )

        extract_images = self._image_extraction_enabled and self._session_dir is not None
        # Cancellation intentionally drains the complete synchronous
        # transaction. Returning before parsing/writes finish could publish an
        # interrupted result whose artifact commit/cleanup is still racing in
        # the background. Large-document interruption can therefore remain
        # pending until the current conversion finishes.
        return await run_spill_finalizer(
            self._convert_document_sync,
            parser,
            resolved,
            path,
            display_resolved,
            display_ext,
            ext,
            extract_images,
        )
