# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for the convert_document tool: normalization, TOC, inline and saved results, errors, cancellation."""

from __future__ import annotations

import asyncio
import json
import os
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from chrys.foundation.config.settings import SESSION_ROOT_DIR_ENV_VAR
from chrys.foundation.platform import get_platform
from chrys.foundation.text.images import MAX_IMAGE_BYTES as MAX_MODEL_IMAGE_BYTES
from chrys.foundation.text.images import MAX_IMAGE_SOURCE_BYTES
from chrys.foundation.tool_kinds import get_tool_kind
from chrys.foundation.tool_result_metadata import (
    TOOL_ERROR_DETAILS_METADATA_KEY,
    TOOL_ERROR_KIND_METADATA_KEY,
    TOOL_FAILED_METADATA_KEY,
)
from chrys.foundation.util.session_ids import session_short_id
from chrys.kernel import LoopRecorder, Message
from chrys.kernel.tools import SyncToolCancelledAfterCompletion
from chrys.service.state.serializers import serialized_message_payload
from chrys.service.state.store import JsonFileStateStore
from chrys.service.tools.builtins.doc_converter import (
    _TOKEN_THRESHOLD,
    DocConverterTools,
    _extract_toc,
    _normalize_document_text,
    _normalize_parsed_document,
    _render_visual_entries,
    _write_file,
)
from chrys.service.tools.builtins.doc_converter.artifacts import (
    MAX_ARTIFACT_BASENAME_BYTES,
    MAX_IMAGE_OCCURRENCES,
    MAX_SESSION_IMAGE_BYTES,
    MAX_SESSION_IMAGE_FILES,
    MAX_SOURCE_STEM_BYTES,
    MAX_STORED_IMAGE_BYTES,
    MAX_TOTAL_IMAGE_BYTES,
    MAX_UNIQUE_IMAGES,
    DocumentImageSink,
)
from chrys.service.tools.builtins.doc_converter.parsers.base import ParsedDocument, VisualOccurrence
from chrys.service.tools.builtins.filesystem import FilesystemTools, read_file
from chrys.service.tools.result_metadata import tool_result_metadata
from tests.service.tools._doc_converter_fakes import (
    _PATCH_REGISTRY,
    _PATCH_TOK,
    _PATCH_TOOL,
    _FakeParser,
    _make_runtime,
    _png_bytes,
    _saved_markdown_absolute_path,
    _saved_markdown_path,
    _session_artifact_path,
    _session_markdown_artifact_path,
    _visual_paths,
    _VisualFakeParser,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _convert_over_threshold(tools: DocConverterTools, doc: Path, parser: object, *, tokens: int) -> str:
    """Convert ``doc`` with ``parser`` stubbed in and the token count pinned above the large-document threshold."""
    with patch(f"{_PATCH_REGISTRY}.get_parser", return_value=parser), patch(_PATCH_TOK) as mock_tok:
        mock_tok.count_tokens.return_value = tokens
        return await tools.convert_document(str(doc))


def test_convert_document_contract_limits_visual_assets_to_pdf_docx_and_pptx() -> None:
    description = DocConverterTools.convert_document.description

    assert "embedded raster assets from PDF, DOCX, and PPTX" in description
    assert "XLS/XLSX conversion remains text/table-only" in description


def test_document_image_storage_limits_align_with_session_and_model_boundaries() -> None:
    assert MAX_UNIQUE_IMAGES == MAX_IMAGE_OCCURRENCES == 128
    assert MAX_SESSION_IMAGE_FILES == 8192
    assert MAX_STORED_IMAGE_BYTES == MAX_IMAGE_SOURCE_BYTES == 50 * 1024 * 1024
    assert MAX_TOTAL_IMAGE_BYTES == MAX_SESSION_IMAGE_BYTES == 512 * 1024 * 1024
    assert MAX_MODEL_IMAGE_BYTES == 256 * 1024


def test_normalize_document_text_repairs_pairs_and_replaces_lone_surrogates() -> None:
    cases = {
        "ordinary text 🌍": "ordinary text 🌍",
        "split pair \ud83c\udf0d": "split pair 🌍",
        "lone high \ud83c": "lone high �",
        "lone low \udf0d": "lone low �",
        r"literal escape \ud83c": r"literal escape \ud83c",
    }

    for source, expected in cases.items():
        normalized = _normalize_document_text(source)
        normalized.encode("utf-8")
        assert normalized == expected


def test_normalize_parsed_document_covers_every_string_field() -> None:
    parsed = ParsedDocument(
        markdown="body \ud83c",
        visuals=(
            VisualOccurrence(
                location="Page \ud83c",
                ordinal=1,
                reference="chrys-session-document:image-\ud83c.png",
                source_name="source-\ud83c.png",
            ),
        ),
        warnings=("warning \ud83c",),
    )

    normalized = _normalize_parsed_document(parsed)

    normalized.markdown.encode("utf-8")
    normalized.visuals[0].location.encode("utf-8")
    normalized.visuals[0].reference.encode("utf-8")
    normalized.visuals[0].source_name.encode("utf-8")
    normalized.warnings[0].encode("utf-8")
    assert "�" in normalized.markdown


def test_visual_path_arguments_are_valid_json_for_windows_paths() -> None:
    reference = r'C:\Users\name\a "quoted" image.png'

    entry = _render_visual_entries((VisualOccurrence(location="Slide 1", ordinal=1, reference=reference),))[0]
    payload = json.loads(entry[entry.index("{") :])

    assert payload == {"path": reference}


def test_write_file_is_total_against_unpaired_surrogates(tmp_path: Path) -> None:
    target = tmp_path / "converted.md"

    _write_file(str(target), "damaged \ud83c text")

    assert target.read_text(encoding="utf-8") == r"damaged \ud83c text"


# ---------------------------------------------------------------------------
# TOC extraction
# ---------------------------------------------------------------------------


def test_extract_toc_basic() -> None:
    md = "# Title\n\nSome text.\n\n## Section A\n\nMore text.\n\n### Subsection\n\n## Section B\n"
    toc = _extract_toc(md)
    assert "1|- Title" in toc
    assert "5|  - Section A" in toc
    assert "9|    - Subsection" in toc
    assert "11|  - Section B" in toc


def test_extract_toc_no_headings() -> None:
    toc = _extract_toc("Just plain text.\nNo headings here.\n")
    assert toc == "(no headings found)"


def test_extract_toc_skips_empty_hashes() -> None:
    toc = _extract_toc("#\n## Real heading\n###\n")
    assert "Real heading" in toc
    # Bare "#" or "###" with no title text should not appear
    assert toc.count("- ") == 1


def test_extract_toc_truncates_when_large() -> None:
    """TOC with many headings gets truncated at _TOC_MAX_TOKENS budget."""
    lines = [f"# Heading number {i}" for i in range(500)]
    md = "\n\nSome text.\n\n".join(lines)

    with patch(_PATCH_TOK) as mock_tok:
        mock_tok.count_tokens.return_value = 50  # 50 tokens per entry -> ~100 fit in 5k
        toc = _extract_toc(md)

    assert "truncated" in toc.lower()
    assert "heading" in toc.lower()
    assert "Heading number 0" in toc
    assert "Heading number 499" not in toc


# ---------------------------------------------------------------------------
# convert_document — small document (inline return)
# ---------------------------------------------------------------------------


async def test_convert_small_document(tmp_path: Path) -> None:
    """Small doc (< threshold) returns full markdown inline."""
    doc = tmp_path / "report.pdf"
    doc.write_bytes(b"%PDF-fake-content")

    small_md = "# Planet \ud83c\udf0d\n\nDamaged \ud83c text.\n"
    fake_parser = _FakeParser(small_md)

    with patch(f"{_PATCH_REGISTRY}.get_parser", return_value=fake_parser):
        runtime = _make_runtime(tmp_path)
        tools = DocConverterTools(runtime, session_id="test123")
        result = await tools.convert_document(str(doc))

    result.encode("utf-8")
    assert "report.pdf" in result
    assert "# Planet 🌍" in result
    assert "Damaged � text." in result
    assert "lines" in result
    assert "tokens" in result

    async def on_checkpoint() -> None:
        return None

    recorder = LoopRecorder(on_checkpoint=on_checkpoint, message_hasher=serialized_message_payload)
    await recorder.record_pre_call([Message(role="tool", contents=[result])])


async def test_convert_small_document_escapes_surrogate_path_but_parses_raw_path(tmp_path: Path) -> None:
    resolved = "/work/report-\udcff.pdf"
    fake_parser = MagicMock()
    fake_parser.parse.return_value = ParsedDocument(markdown="# Report\n\ncontent\n")
    runtime = _make_runtime(tmp_path)
    tools = DocConverterTools(runtime, session_id="test123")

    with (
        patch(f"{_PATCH_TOOL}.resolve_existing_path", return_value=resolved),
        patch(f"{_PATCH_TOOL}.os.path.isfile", return_value=True),
        patch(f"{_PATCH_REGISTRY}.get_parser", return_value=fake_parser),
    ):
        result = await tools.convert_document("report.pdf")

    result.encode("utf-8")
    assert r"File: /work/report-\udcff.pdf" in result
    fake_parser.parse.assert_called_once_with(resolved, image_sink=None)


async def test_convert_document_falls_back_to_narrow_no_break_space_filename(tmp_path: Path) -> None:
    """convert_document tolerates macOS screenshot U+202F whitespace in filenames."""
    real = tmp_path / "report 9.00.00\u202fAM.pdf"
    real.write_bytes(b"%PDF-fake-content")
    requested = str(tmp_path / "report 9.00.00 AM.pdf")

    small_md = "# Report\n\ncontent\n"
    fake_parser = _FakeParser(small_md)

    with patch(f"{_PATCH_REGISTRY}.get_parser", return_value=fake_parser):
        runtime = _make_runtime(tmp_path)
        tools = DocConverterTools(runtime, session_id="test123")
        result = await tools.convert_document(requested)

    assert "# Report" in result
    assert str(real) in result


async def test_convert_document_default_gate_avoids_all_image_work(tmp_path: Path) -> None:
    doc = tmp_path / "report.pdf"
    doc.write_bytes(b"%PDF-fake")
    parser = _VisualFakeParser(_png_bytes())
    tools = DocConverterTools(_make_runtime(tmp_path), session_dir=tmp_path / "session")

    with patch(f"{_PATCH_REGISTRY}.get_parser", return_value=parser):
        result = await tools.convert_document(str(doc))

    assert parser.last_sink is None
    assert "Extracted visual assets" not in result
    assert "view_image" not in result
    assert not (tmp_path / "session" / "doc_converter").exists()


async def test_convert_document_enabled_flag_without_session_still_avoids_image_work(tmp_path: Path) -> None:
    doc = tmp_path / "report.pdf"
    doc.write_bytes(b"%PDF-fake")
    parser = _VisualFakeParser(_png_bytes())
    tools = DocConverterTools(_make_runtime(tmp_path))
    tools.set_image_extraction_enabled(True)

    with patch(f"{_PATCH_REGISTRY}.get_parser", return_value=parser):
        result = await tools.convert_document(str(doc))

    assert parser.last_sink is None
    assert "Extracted visual assets" not in result
    assert "view_image" not in result


async def test_convert_document_disabled_image_only_pdf_keeps_page_placeholder(tmp_path: Path) -> None:
    from PIL import Image

    from chrys.service.tools.builtins.doc_converter.parsers.pdf import PdfParser

    doc = tmp_path / "image-only.pdf"
    Image.new("RGB", (8, 8), (255, 0, 0)).save(doc, format="PDF")
    session_dir = tmp_path / "session"
    tools = DocConverterTools(_make_runtime(tmp_path), session_dir=session_dir)

    with patch(f"{_PATCH_REGISTRY}.get_parser", return_value=PdfParser()):
        result = await tools.convert_document(str(doc))

    assert "# Page 1\n\n(no text content)" in result
    assert "view_image" not in result
    assert not (session_dir / "doc_converter").exists()


async def test_convert_document_enabled_gate_returns_viewable_image_path(tmp_path: Path) -> None:
    doc = tmp_path / "report.pdf"
    doc.write_bytes(b"%PDF-fake")
    parser = _VisualFakeParser(_png_bytes())
    session_dir = tmp_path / "session"
    runtime = _make_runtime(tmp_path)
    tools = DocConverterTools(runtime, session_dir=session_dir)
    tools.set_image_extraction_enabled(True)

    with patch(f"{_PATCH_REGISTRY}.get_parser", return_value=parser):
        result = await tools.convert_document(str(doc))

    assert parser.last_sink is not None
    assert result.count("Use view_image") == 1
    paths = _visual_paths(result)
    assert len(paths) == 1
    assert paths[0].startswith("chrys-session-document:")
    image_result = FilesystemTools(runtime, session_dir=session_dir).view_image(paths[0])
    assert image_result[0].media_type == "image/png"


async def test_visual_index_participates_in_large_document_threshold(tmp_path: Path) -> None:
    doc = tmp_path / "report.pdf"
    doc.write_bytes(b"%PDF-fake")
    parser = _VisualFakeParser(_png_bytes(), text_content="# Short report")
    session_dir = tmp_path / "session"
    tools = DocConverterTools(_make_runtime(tmp_path), session_dir=session_dir)
    tools.set_image_extraction_enabled(True)

    def count_tokens(text: str) -> int:
        return _TOKEN_THRESHOLD if "Extracted visual assets" in text else 1

    with (
        patch(f"{_PATCH_REGISTRY}.get_parser", return_value=parser),
        patch(_PATCH_TOK) as mock_tok,
    ):
        mock_tok.count_tokens.side_effect = count_tokens
        result = await tools.convert_document(str(doc))

    assert "Document is too large to return inline" in result
    saved_path = _saved_markdown_path(result, session_dir)
    assert "Extracted visual assets" in saved_path.read_text(encoding="utf-8")


async def test_convert_document_empty_text_with_visual_does_not_drop_image(tmp_path: Path) -> None:
    doc = tmp_path / "empty.docx"
    doc.write_bytes(b"PK-fake")
    parser = _VisualFakeParser(_png_bytes(), text_content="")
    tools = DocConverterTools(_make_runtime(tmp_path), session_dir=tmp_path / "session")
    tools.set_image_extraction_enabled(True)

    with patch(f"{_PATCH_REGISTRY}.get_parser", return_value=parser):
        result = await tools.convert_document(str(doc))

    assert "document converted but produced no text content" not in result
    assert len(_visual_paths(result)) == 1


async def test_convert_document_failed_images_warn_without_view_image_instruction(tmp_path: Path) -> None:
    doc = tmp_path / "report.pdf"
    doc.write_bytes(b"%PDF-fake")
    parser = _VisualFakeParser(b"not an image")
    tools = DocConverterTools(_make_runtime(tmp_path), session_dir=tmp_path / "session")
    tools.set_image_extraction_enabled(True)

    with patch(f"{_PATCH_REGISTRY}.get_parser", return_value=parser):
        result = await tools.convert_document(str(doc))

    assert "Image extraction warnings" in result
    assert "view_image" not in result
    assert _visual_paths(result) == []


# ---------------------------------------------------------------------------
# convert_document — large document (save to disk)
# ---------------------------------------------------------------------------


async def test_convert_large_document(tmp_path: Path, monkeypatch) -> None:
    """Large doc (>= threshold) saves to session dir and returns TOC + metadata."""
    monkeypatch.delenv(SESSION_ROOT_DIR_ENV_VAR, raising=False)
    doc = tmp_path / "big.docx"
    doc.write_bytes(b"PK-fake-docx")

    lines = [f"# Chapter {i}\n\nParagraph content for chapter {i}. " + "word " * 200 for i in range(1, 51)]
    large_md = "\n".join(lines)

    fake_parser = _FakeParser(large_md, extensions=frozenset({".docx"}))

    runtime = _make_runtime(tmp_path)
    tools = DocConverterTools(runtime, session_id="sess_abc")

    result = await _convert_over_threshold(tools, doc, fake_parser, tokens=_TOKEN_THRESHOLD + 1000)

    assert "too large to return inline" in result.lower() or "Saved Markdown handle:" in result
    assert "Table of Contents" in result
    assert "read_file" in result

    doc_converter_dir = tmp_path / "sessions" / session_short_id("sess_abc") / "doc_converter"
    saved_files = list(doc_converter_dir.glob("*.md"))
    assert len(saved_files) == 1
    assert saved_files[0].read_text(encoding="utf-8") == large_md


async def test_convert_large_document_prefers_supplied_session_dir(tmp_path: Path, monkeypatch) -> None:
    """A resolved session_dir should win over the environment fallback."""
    doc = tmp_path / "big.docx"
    doc.write_bytes(b"PK-fake-docx")
    large_md = "# Heading\n" + "content " * 5000
    fake_parser = _FakeParser(large_md, extensions=frozenset({".docx"}))
    explicit_session_dir = tmp_path / "explicit" / "sess_abc"
    monkeypatch.setenv(SESSION_ROOT_DIR_ENV_VAR, str(tmp_path / "env-root"))

    runtime = _make_runtime(tmp_path)
    tools = DocConverterTools(runtime, session_id="sess_abc", session_dir=explicit_session_dir)

    await _convert_over_threshold(tools, doc, fake_parser, tokens=_TOKEN_THRESHOLD + 100)

    saved_files = list((explicit_session_dir / "doc_converter").glob("*.md"))
    assert len(saved_files) == 1
    assert not (tmp_path / "env-root" / "sessions").exists()


async def test_convert_large_document_returns_path_usable_without_session_bound_read_file(tmp_path: Path) -> None:
    doc = tmp_path / "big.docx"
    doc.write_bytes(b"PK-fake-docx")
    markdown = "# Heading\n" + "content " * 5000
    parser = _FakeParser(markdown, extensions=frozenset({".docx"}))
    session_dir = tmp_path / "session"
    tools = DocConverterTools(_make_runtime(tmp_path), session_dir=session_dir)

    with patch(f"{_PATCH_REGISTRY}.get_parser", return_value=parser):
        result = await tools.convert_document(str(doc))

    saved_path = _saved_markdown_absolute_path(result)
    assert saved_path == session_dir / "doc_converter" / "big.md"
    assert "1|# Heading" in read_file(os.fspath(saved_path))
    assert "session-bound read_file" in result
    assert "filesystem or shell tool" in result


@pytest.mark.parametrize(
    ("separator", "suffix", "heading_line", "line_count"),
    [("\x0b", ".pptx", 5, 7), ("\r", ".xlsx", 6, 8), ("\r\n", ".xlsx", 6, 8)],
    ids=["slide-soft-break", "cell-carriage-return", "crlf"],
)
async def test_large_document_toc_numbers_lines_as_read_file_does(
    tmp_path: Path, separator: str, suffix: str, heading_line: int, line_count: int
) -> None:
    """A slide's soft line break (a vertical tab) ends no line; a carriage return ends one, as in read_file."""
    doc = tmp_path / f"document{suffix}"
    doc.write_bytes(b"PK-fake")
    markdown = f"# Part 1\n\nfirst{separator}second\n\n# Part 2\n\n" + "content " * 5000 + "\n"
    parser = _FakeParser(markdown, extensions=frozenset({suffix}))
    tools = DocConverterTools(_make_runtime(tmp_path), session_dir=tmp_path / "session")

    with patch(f"{_PATCH_REGISTRY}.get_parser", return_value=parser):
        result = await tools.convert_document(str(doc))

    assert f"({line_count} lines, " in result
    assert "1|- Part 1" in result
    assert f"{heading_line}|- Part 2" in result
    saved_path = os.fspath(_saved_markdown_absolute_path(result))
    assert f"{heading_line}|# Part 2\n" in read_file(saved_path, offset=heading_line, limit=1)


async def test_concurrent_same_name_large_conversions_keep_different_markdown(tmp_path: Path) -> None:
    doc = tmp_path / "report.pdf"
    doc.write_bytes(b"%PDF-fake")
    session_dir = tmp_path / "session"
    tools = DocConverterTools(_make_runtime(tmp_path), session_dir=session_dir)
    first = _FakeParser("# First\n\n" + "alpha " * 6000)
    second = _FakeParser("# Second\n\n" + "beta " * 6000)

    with patch(f"{_PATCH_REGISTRY}.get_parser", side_effect=[first, second]), patch(_PATCH_TOK) as mock_tok:
        mock_tok.count_tokens.return_value = _TOKEN_THRESHOLD + 1
        results = await asyncio.gather(
            tools.convert_document(str(doc)),
            tools.convert_document(str(doc)),
        )

    saved_paths = [_saved_markdown_path(result, session_dir) for result in results]
    assert saved_paths[0] != saved_paths[1]
    assert {path.read_text(encoding="utf-8") for path in saved_paths} == {first._text, second._text}


async def test_convert_large_document_uses_fixed_twenty_visual_preview(tmp_path: Path) -> None:
    class _ManyVisualParser(_FakeParser):
        def parse(self, path: str, *, image_sink: DocumentImageSink | None = None) -> ParsedDocument:
            assert image_sink is not None
            visuals: list[VisualOccurrence] = []
            for index in range(25):
                assert image_sink.try_reserve_occurrence()
                occurrence = image_sink.save_image(
                    _png_bytes((index, 0, 0)),
                    location=f"Page {index + 1}",
                    ordinal=1,
                    source_name=f"{index}.png",
                )
                assert occurrence is not None
                visuals.append(occurrence)
            return ParsedDocument(
                markdown="# Large report\n\ncontent",
                visuals=tuple(visuals),
                warnings=image_sink.warnings,
            )

    doc = tmp_path / "large.pdf"
    doc.write_bytes(b"%PDF-fake")
    session_dir = tmp_path / "session"
    tools = DocConverterTools(_make_runtime(tmp_path), session_dir=session_dir)
    tools.set_image_extraction_enabled(True)

    result = await _convert_over_threshold(tools, doc, _ManyVisualParser(), tokens=_TOKEN_THRESHOLD + 1)

    assert len(_visual_paths(result)) == 20
    assert "5 more image occurrence(s)" in result
    assert result.count("Use view_image") == 1
    saved_path = _saved_markdown_path(result, session_dir)
    saved = saved_path.read_text(encoding="utf-8")
    assert len(_visual_paths(saved)) == 25
    assert saved.count("Use view_image") == 1
    assert len(list((session_dir / "doc_converter").glob("*.png"))) == 25


async def test_convert_large_markdown_filename_is_utf8_byte_bounded(tmp_path: Path) -> None:
    resolved = "/work/" + ("报告" * 100) + ".pdf"
    parser = _FakeParser("# Report\n\n" + "content " * 6000)
    session_dir = tmp_path / "session"
    tools = DocConverterTools(_make_runtime(tmp_path), session_dir=session_dir)

    with (
        patch(f"{_PATCH_TOOL}.resolve_existing_path", return_value=resolved),
        patch(f"{_PATCH_TOOL}.os.path.isfile", return_value=True),
        patch(f"{_PATCH_REGISTRY}.get_parser", return_value=parser),
    ):
        result = await tools.convert_document("report.pdf")

    saved_path = _saved_markdown_path(result, session_dir)
    assert len(saved_path.name.encode("utf-8")) <= MAX_ARTIFACT_BASENAME_BYTES
    assert len(saved_path.stem.encode("utf-8")) <= MAX_SOURCE_STEM_BYTES
    assert saved_path.exists()


async def test_convert_large_document_normalizes_before_count_toc_and_save(tmp_path: Path) -> None:
    doc = tmp_path / "surrogate.pdf"
    doc.write_bytes(b"%PDF-fake")
    raw_markdown = "# Planet \ud83c\udf0d\n\nDamaged \ud83c text\n\n" + "content " * 6000
    fake_parser = _FakeParser(raw_markdown)
    runtime = _make_runtime(tmp_path)
    session_dir = tmp_path / "surrogate_session"
    tools = DocConverterTools(runtime, session_id="surrogate_session", session_dir=session_dir)

    with patch(f"{_PATCH_REGISTRY}.get_parser", return_value=fake_parser):
        result = await tools.convert_document(str(doc))

    saved_files = list((session_dir / "doc_converter").glob("*.md"))
    assert len(saved_files) == 1
    saved = saved_files[0].read_text(encoding="utf-8")
    surrogate_positions = [index for index, char in enumerate(saved) if 0xD800 <= ord(char) <= 0xDFFF]
    assert not surrogate_positions, f"saved text contains surrogates at positions {surrogate_positions[:10]}"
    assert "# Planet 🌍" in saved
    assert "Damaged � text" in saved
    assert f"{len(saved)} chars" in result
    assert "Planet 🌍" in result
    result.encode("utf-8")


async def test_convert_large_document_reports_save_failure(tmp_path: Path) -> None:
    doc = tmp_path / "report.pdf"
    doc.write_bytes(b"%PDF-fake")
    fake_parser = _FakeParser("# Report\n\n" + "content " * 6000)
    runtime = _make_runtime(tmp_path)
    tools = DocConverterTools(runtime, session_id="save_failure")
    metadata: dict[str, object] = {}
    token = tool_result_metadata.set(metadata)

    try:
        with (
            patch(f"{_PATCH_REGISTRY}.get_parser", return_value=fake_parser),
            patch("chrys.service.tools.builtins.doc_converter._write_file", side_effect=OSError("disk full")),
        ):
            result = await tools.convert_document(str(doc))
    finally:
        tool_result_metadata.reset(token)

    assert result.startswith("Error:")
    assert "failed to save converted document" in result
    assert "disk full" in result
    assert metadata[TOOL_FAILED_METADATA_KEY] is True
    assert metadata[TOOL_ERROR_KIND_METADATA_KEY] == "document_save_failed"


async def test_convert_large_document_reports_handle_creation_failure(tmp_path: Path) -> None:
    doc = tmp_path / "report.pdf"
    doc.write_bytes(b"%PDF-fake")
    fake_parser = _FakeParser("# Report\n\n" + "content " * 6000)
    runtime = _make_runtime(tmp_path)
    tools = DocConverterTools(runtime, session_id="handle_failure", session_dir=tmp_path / "session")

    with (
        patch(f"{_PATCH_REGISTRY}.get_parser", return_value=fake_parser),
        patch(f"{_PATCH_TOOL}._unique_path", return_value="/session-\udcff/report.md"),
        patch(f"{_PATCH_TOOL}.make_document_artifact_handle", side_effect=ValueError("invalid artifact name")),
        patch(f"{_PATCH_TOOL}._write_file") as write_file_mock,
    ):
        result = await tools.convert_document(str(doc))

    result.encode("utf-8")
    assert result.startswith("Error:")
    assert "failed to save converted document" in result
    assert "invalid artifact name" in result
    write_file_mock.assert_not_called()


def test_document_converter_and_filesystem_reader_share_derived_session_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(SESSION_ROOT_DIR_ENV_VAR, raising=False)
    import chrys.service.tools.session_artifacts as session_artifacts

    monkeypatch.setattr(
        session_artifacts,
        "resolve_sessions_dir",
        lambda config_dir, *, create: config_dir / "sessions",
    )
    runtime = _make_runtime(tmp_path / "config-\udcff")

    converter = DocConverterTools(runtime, session_id="shared_session")
    filesystem = FilesystemTools(runtime, session_id="shared_session")

    assert converter._session_dir == filesystem._session_dir
    assert converter._session_dir is not None
    assert str(converter._session_dir).startswith(str(runtime.platform.config_dir))


async def test_convert_large_document_creates_addressable_safe_output_path(tmp_path: Path) -> None:
    resolved = "/work/report-\udcff.pdf"
    markdown = "# Report\n\n" + "content " * 6000
    fake_parser = MagicMock()
    fake_parser.parse.return_value = ParsedDocument(markdown=markdown)
    runtime = _make_runtime(tmp_path)
    tools = DocConverterTools(runtime, session_id="surrogate_path", session_dir=tmp_path / "session")

    with (
        patch(f"{_PATCH_TOOL}.resolve_existing_path", return_value=resolved),
        patch(f"{_PATCH_TOOL}.os.path.isfile", return_value=True),
        patch(f"{_PATCH_REGISTRY}.get_parser", return_value=fake_parser),
    ):
        result = await tools.convert_document("report.pdf")

    result.encode("utf-8")
    assert r"File: /work/report-\udcff.pdf" in result
    returned_handle = result.split("Saved Markdown handle: ", 1)[1].splitlines()[0]
    expected_path = tmp_path / "session" / "doc_converter" / "report-_.md"
    assert _session_markdown_artifact_path(tmp_path / "session", returned_handle) == expected_path
    assert expected_path.read_text(encoding="utf-8") == markdown
    assert "1|# Report" in FilesystemTools(runtime, session_dir=tmp_path / "session").read_file(returned_handle)
    fake_parser.parse.assert_called_once_with(resolved, image_sink=None)


async def test_convert_large_document_returns_addressable_handle_for_surrogate_session_dir(
    tmp_path: Path,
) -> None:
    if not get_platform().is_linux:
        pytest.skip("invalid-UTF-8 filesystem paths are Linux-specific")

    raw_session_dir = os.path.join(os.fsencode(tmp_path), b"session-\xff")
    os.mkdir(raw_session_dir)
    session_dir = Path(os.fsdecode(raw_session_dir))
    doc = tmp_path / "report.pdf"
    doc.write_bytes(b"%PDF-fake")
    markdown = "# Report\n\n" + "content " * 6000
    fake_parser = _FakeParser(markdown)
    runtime = _make_runtime(tmp_path)
    tools = DocConverterTools(runtime, session_id="surrogate_session_dir", session_dir=session_dir)

    with patch(f"{_PATCH_REGISTRY}.get_parser", return_value=fake_parser):
        result = await tools.convert_document(str(doc))

    result.encode("utf-8")
    handle = result.split("Saved Markdown handle: ", 1)[1].splitlines()[0]
    expected_path = os.path.join(os.fsdecode(raw_session_dir), "doc_converter", "report.md")

    assert handle.isascii()
    assert "Saved Markdown path:" not in result
    read_result = FilesystemTools(runtime, session_dir=session_dir).read_file(handle)
    read_result.encode("utf-8")
    assert "1|# Report" in read_result
    assert os.fsencode(expected_path) == os.path.join(raw_session_dir, b"doc_converter", b"report.md")
    assert Path(expected_path).read_text(encoding="utf-8") == markdown


async def test_convert_document_returns_image_handle_for_surrogate_session_dir(tmp_path: Path) -> None:
    if not get_platform().is_linux:
        pytest.skip("invalid-UTF-8 filesystem paths are Linux-specific")

    raw_session_dir = os.path.join(os.fsencode(tmp_path), b"image-session-\xff")
    os.mkdir(raw_session_dir)
    session_dir = Path(os.fsdecode(raw_session_dir))
    doc = tmp_path / "report.pdf"
    doc.write_bytes(b"%PDF-fake")
    parser = _VisualFakeParser(_png_bytes())
    runtime = _make_runtime(tmp_path)
    tools = DocConverterTools(runtime, session_dir=session_dir)
    tools.set_image_extraction_enabled(True)

    with patch(f"{_PATCH_REGISTRY}.get_parser", return_value=parser):
        result = await tools.convert_document(str(doc))

    result.encode("utf-8")
    paths = _visual_paths(result)
    assert len(paths) == 1
    assert paths[0].startswith("chrys-session-document:")
    image_result = FilesystemTools(runtime, session_dir=session_dir).view_image(paths[0])
    assert image_result[0].media_type == "image/png"
    source_path = image_result[0].additional_properties["source_path"]
    source_path.encode("utf-8")
    assert r"image-session-\udcff" in source_path

    message = Message(role="tool", contents=image_result)
    wire_payload = serialized_message_payload(message)
    wire_payload.encode("utf-8")
    store = JsonFileStateStore(tmp_path / "persisted-sessions")
    await store.save_session("surrogate-image-metadata", {"messages": [message], "compressed_msgs": []})
    restored = await store.load_session("surrogate-image-metadata")

    assert restored is not None
    restored_content = restored["messages"][0].contents[0]
    assert restored_content.additional_properties["source_path"] == source_path


# ---------------------------------------------------------------------------
# convert_document — no session_id (large doc, no save dir)
# ---------------------------------------------------------------------------


async def test_convert_large_no_session(tmp_path: Path) -> None:
    """Large doc with no session_id returns TOC without saving."""
    doc = tmp_path / "nosess.pdf"
    doc.write_bytes(b"%PDF-fake")

    large_md = "# Heading\n" + "content " * 5000
    fake_parser = _FakeParser(large_md)

    runtime = _make_runtime(tmp_path)
    tools = DocConverterTools(runtime, session_id="")  # no session

    result = await _convert_over_threshold(tools, doc, fake_parser, tokens=_TOKEN_THRESHOLD + 500)

    assert "No session directory" in result
    assert "Table of Contents" in result


# ---------------------------------------------------------------------------
# Error cases
# ---------------------------------------------------------------------------


async def test_convert_file_not_found(tmp_path: Path) -> None:
    runtime = _make_runtime(tmp_path)
    tools = DocConverterTools(runtime)
    result = await tools.convert_document(str(tmp_path / "nonexistent.pdf"))
    assert result.startswith("Error:")
    assert "file not found" in result


async def test_convert_file_not_found_escapes_display_path_but_keeps_raw_metadata(tmp_path: Path) -> None:
    resolved = "/work/missing-\udcff.pdf"
    runtime = _make_runtime(tmp_path)
    runtime.cwd = str(tmp_path / "workspace")
    tools = DocConverterTools(runtime)
    metadata: dict[str, object] = {}
    token = tool_result_metadata.set(metadata)

    try:
        with (
            patch(f"{_PATCH_TOOL}.resolve_existing_path", return_value=None),
            patch(f"{_PATCH_TOOL}.resolve_workspace_path", return_value=resolved),
            patch(f"{_PATCH_TOOL}.os.path.isdir", return_value=False),
        ):
            # Absolute and outside the working directory, so the patched isdir()
            # decides only about the resolved path, not a missing working directory.
            result = await tools.convert_document(str(tmp_path / "missing.pdf"))
    finally:
        tool_result_metadata.reset(token)

    result.encode("utf-8")
    assert r"file not found — /work/missing-\udcff.pdf" in result
    details = metadata[TOOL_ERROR_DETAILS_METADATA_KEY]
    assert isinstance(details, dict)
    assert details["resolved_path"] == resolved


async def test_convert_directory(tmp_path: Path) -> None:
    runtime = _make_runtime(tmp_path)
    tools = DocConverterTools(runtime)
    result = await tools.convert_document(str(tmp_path))
    assert result.startswith("Error:")
    assert "directory" in result


async def test_convert_unsupported_format(tmp_path: Path) -> None:
    f = tmp_path / "image.png"
    f.write_bytes(b"\x89PNG")

    with (
        patch(f"{_PATCH_REGISTRY}.get_parser", return_value=None),
        patch(f"{_PATCH_REGISTRY}.supported_extensions", return_value=frozenset({".pdf"})),
    ):
        runtime = _make_runtime(tmp_path)
        tools = DocConverterTools(runtime)
        result = await tools.convert_document(str(f))

    assert result.startswith("Error:")
    assert "unsupported" in result.lower()


async def test_convert_empty_result(tmp_path: Path) -> None:
    """Document that converts to empty text returns informative message."""
    doc = tmp_path / "empty.pdf"
    doc.write_bytes(b"%PDF-empty")

    fake_parser = _FakeParser("")

    with patch(f"{_PATCH_REGISTRY}.get_parser", return_value=fake_parser):
        runtime = _make_runtime(tmp_path)
        tools = DocConverterTools(runtime)
        result = await tools.convert_document(str(doc))

    assert "no text content" in result.lower()


async def test_convert_exception(tmp_path: Path) -> None:
    """Conversion exception returns error string."""
    doc = tmp_path / "bad.pdf"
    doc.write_bytes(b"%PDF-corrupt")

    fake_parser = _FakeParser(side_effect=RuntimeError("corrupt PDF"))

    with patch(f"{_PATCH_REGISTRY}.get_parser", return_value=fake_parser):
        runtime = _make_runtime(tmp_path)
        tools = DocConverterTools(runtime)
        result = await tools.convert_document(str(doc))

    assert result.startswith("Error:")
    assert "corrupt PDF" in result


async def test_cancelled_completed_conversion_carries_final_result_and_keeps_images(tmp_path: Path) -> None:
    started = threading.Event()
    release = threading.Event()
    image = _png_bytes()

    class _BlockingParser(_FakeParser):
        def parse(self, path: str, *, image_sink: DocumentImageSink | None = None) -> ParsedDocument:
            assert image_sink is not None
            assert image_sink.try_reserve_occurrence()
            occurrence = image_sink.save_image(
                image,
                location="Page 1",
                ordinal=1,
                source_name="pixel.png",
            )
            assert occurrence is not None
            started.set()
            assert release.wait(timeout=5)
            return ParsedDocument(markdown="# Report", visuals=(occurrence,), warnings=image_sink.warnings)

    doc = tmp_path / "report.pdf"
    doc.write_bytes(b"%PDF-fake")
    session_dir = tmp_path / "session"
    runtime = _make_runtime(tmp_path)
    tools = DocConverterTools(runtime, session_dir=session_dir)
    tools.set_image_extraction_enabled(True)

    with patch(f"{_PATCH_REGISTRY}.get_parser", return_value=_BlockingParser()):
        task = asyncio.create_task(tools.convert_document(str(doc)))
        assert await asyncio.to_thread(started.wait, 5)
        task.cancel()
        release.set()
        with pytest.raises(SyncToolCancelledAfterCompletion) as exc_info:
            await task

    completed = exc_info.value.completed_result
    assert isinstance(completed, str)
    paths = _visual_paths(completed)
    assert len(paths) == 1
    assert _session_artifact_path(session_dir, paths[0]).exists()
    assert FilesystemTools(runtime, session_dir=session_dir).view_image(paths[0])[0].media_type == "image/png"


async def test_failed_conversion_cleans_new_image_orphans(tmp_path: Path) -> None:
    image = _png_bytes()

    class _FailingParser(_FakeParser):
        def parse(self, path: str, *, image_sink: DocumentImageSink | None = None) -> ParsedDocument:
            assert image_sink is not None
            assert image_sink.try_reserve_occurrence()
            assert image_sink.save_image(image, location="Page 1", ordinal=1, source_name="pixel.png") is not None
            raise RuntimeError("parse failed after image write")

    doc = tmp_path / "report.pdf"
    doc.write_bytes(b"%PDF-fake")
    session_dir = tmp_path / "session"
    tools = DocConverterTools(_make_runtime(tmp_path), session_dir=session_dir)
    tools.set_image_extraction_enabled(True)

    with patch(f"{_PATCH_REGISTRY}.get_parser", return_value=_FailingParser()):
        result = await tools.convert_document(str(doc))

    assert result.startswith("Error:")
    assert list((session_dir / "doc_converter").glob("*.png")) == []


async def test_successful_conversion_cleans_images_missing_from_returned_occurrences(tmp_path: Path) -> None:
    image = _png_bytes()

    class _DroppingParser(_FakeParser):
        def parse(self, path: str, *, image_sink: DocumentImageSink | None = None) -> ParsedDocument:
            assert image_sink is not None
            assert image_sink.try_reserve_occurrence()
            assert image_sink.save_image(image, location="Page 1", ordinal=1, source_name="pixel.png") is not None
            return ParsedDocument(markdown="# Report")

    doc = tmp_path / "report.pdf"
    doc.write_bytes(b"%PDF-fake")
    session_dir = tmp_path / "session"
    tools = DocConverterTools(_make_runtime(tmp_path), session_dir=session_dir)
    tools.set_image_extraction_enabled(True)

    with patch(f"{_PATCH_REGISTRY}.get_parser", return_value=_DroppingParser()):
        result = await tools.convert_document(str(doc))

    assert "view_image" not in result
    assert list((session_dir / "doc_converter").glob("*.png")) == []


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------


def test_tools_returns_convert_document(tmp_path: Path) -> None:
    runtime = _make_runtime(tmp_path)
    dc = DocConverterTools(runtime, session_id="s1")
    tools = dc.tools()
    assert len(tools) == 1
    assert tools[0].name == "convert_document"
    assert get_tool_kind(tools[0]) == "doc_converter"
    assert tools[0].kind is None


# ---------------------------------------------------------------------------
# File collision handling
# ---------------------------------------------------------------------------


async def test_save_handles_filename_collision(tmp_path: Path, monkeypatch) -> None:
    """Second conversion of same-named file creates a _1 suffixed file."""
    monkeypatch.delenv(SESSION_ROOT_DIR_ENV_VAR, raising=False)
    doc = tmp_path / "report.pdf"
    doc.write_bytes(b"%PDF-fake")

    md_text = "# Report\n" + "content " * 100
    fake_parser = _FakeParser(md_text)

    runtime = _make_runtime(tmp_path)
    session_id = "collision_test"
    tools = DocConverterTools(runtime, session_id=session_id)

    # Pre-create the first output file to simulate collision
    out_dir = tmp_path / "sessions" / session_short_id(session_id) / "doc_converter"
    out_dir.mkdir(parents=True)
    (out_dir / "report.md").write_text("old content")

    result = await _convert_over_threshold(tools, doc, fake_parser, tokens=_TOKEN_THRESHOLD + 100)

    assert "report_1.md" in result
    assert (out_dir / "report_1.md").exists()
