# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for filesystem tools — read_file, write_file, edit_file."""

from __future__ import annotations

import base64
import codecs
import json
import os
import re
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from io import BytesIO
from typing import TYPE_CHECKING
from unittest.mock import Mock

import pytest
from PIL import Image

from chrys.foundation.models.session_env import SessionEnvironment
from chrys.foundation.models.workspace import Workspace
from chrys.foundation.platform import get_platform
from chrys.foundation.text.images import LoadedImage
from chrys.service.tools.builtins.filesystem import (
    _MAX_LINE_DISPLAY_CHARS,
    EditFilePlan,
    FilesystemTools,
    FileToolPreviewError,
    WriteFilePlan,
    _default_eol_for_new_file,
    _detect_line_ending,
    _replace_surrogate_escapes,
    edit_file,
    plan_edit_file,
    plan_write_file,
    read_file,
    view_image,
    write_file,
)
from chrys.service.tools.session_artifacts import make_document_artifact_handle
from tests.support.images import image_bytes
from tests.support.windows_replace import briefly_locked_rename

if TYPE_CHECKING:
    from pathlib import Path

_WINDOWS = get_platform().is_windows


@contextmanager
def _temporary_umask(mask: int) -> Iterator[None]:
    previous = os.umask(mask)
    try:
        yield
    finally:
        os.umask(previous)


def _jpeg_bytes() -> bytes:
    image = Image.new("RGB", (1, 1), (240, 80, 80))
    out = BytesIO()
    image.save(out, format="JPEG")
    return out.getvalue()


def test_read_file_basic(tmp_path: Path) -> None:
    """Small file, no truncation, all lines present."""
    f = tmp_path / "small.txt"
    f.write_text("aaa\nbbb\nccc\n", encoding="utf-8")

    result = read_file(str(f))
    assert "3 lines" in result
    assert "1|aaa" in result
    assert "2|bbb" in result
    assert "3|ccc" in result
    assert "[Truncated" not in result


def test_runtime_bound_filesystem_tools_resolve_relative_paths_from_runtime_cwd(tmp_path: Path, monkeypatch) -> None:
    """Instance tools should not resolve relative paths against process cwd."""
    project = tmp_path / "project"
    other = tmp_path / "other"
    project.mkdir()
    other.mkdir()
    monkeypatch.chdir(other)

    runtime = SessionEnvironment.capture(workspace=Workspace.from_cwd(str(project)))
    tools = FilesystemTools(runtime)

    assert "Written" in tools.write_file("notes.txt", "alpha\n")
    assert (project / "notes.txt").read_text(encoding="utf-8") == "alpha\n"
    assert not (other / "notes.txt").exists()

    read_result = tools.read_file("notes.txt")
    assert f"File: {project / 'notes.txt'}" in read_result
    assert "1|alpha" in read_result


def test_read_file_line_number_format(tmp_path: Path) -> None:
    """Line numbers use compact 'N|' format without padding or spaces."""
    f = tmp_path / "fmt.txt"
    f.write_text("line one\nline two\n", encoding="utf-8")

    result = read_file(str(f))
    # New format: "1|line one"
    assert "1|line one" in result
    assert "2|line two" in result
    # Old format should NOT be present
    assert "   1 | " not in result


def test_read_file_truncation(tmp_path: Path) -> None:
    """Large file with small max_tokens triggers truncation message."""
    f = tmp_path / "big.txt"
    lines = [f"line number {i}" for i in range(1, 201)]
    f.write_text("\n".join(lines) + "\n", encoding="utf-8")

    result = read_file(str(f), max_tokens=100)
    assert "[Truncated" in result
    assert "200 lines" in result
    assert "Use offset=" in result


def test_read_file_pagination(tmp_path: Path) -> None:
    """offset=3, limit=5 returns only lines 3-7, header still shows total."""
    f = tmp_path / "ten.txt"
    lines = [f"L{i}" for i in range(1, 11)]
    f.write_text("\n".join(lines) + "\n", encoding="utf-8")

    result = read_file(str(f), offset=3, limit=5)
    assert "10 lines" in result  # header shows total
    assert "3|L3" in result
    assert "7|L7" in result
    assert "2|L2" not in result
    assert "8|L8" not in result
    assert result.endswith("[Showing lines 3-7 of 10. Use offset=8, limit=5 to continue reading if needed.]")
    assert "[Truncated" not in result


def test_read_file_pagination_to_eof(tmp_path: Path) -> None:
    """offset=8 returns lines 8 through end."""
    f = tmp_path / "ten.txt"
    lines = [f"L{i}" for i in range(1, 11)]
    f.write_text("\n".join(lines) + "\n", encoding="utf-8")

    result = read_file(str(f), offset=8)
    assert "8|L8" in result
    assert "9|L9" in result
    assert "10|L10" in result
    assert "7|L7" not in result


def test_read_file_pagination_with_truncation(tmp_path: Path) -> None:
    """Range exceeds max_tokens → correct truncation hint."""
    f = tmp_path / "big.txt"
    lines = [f"line content number {i}" for i in range(1, 101)]
    f.write_text("\n".join(lines) + "\n", encoding="utf-8")

    result = read_file(str(f), max_tokens=50, offset=10, limit=71)
    assert "[Truncated" in result
    assert "100 lines" in result


def test_read_file_default_limit_and_continuation(tmp_path: Path) -> None:
    f = tmp_path / "pages.txt"
    f.write_text("x\n" * 2001, encoding="utf-8")

    result = read_file(str(f), max_tokens=20_000)
    assert "2000|x\n" in result
    assert "2001|x\n" not in result
    assert result.endswith("[Showing lines 1-2000 of 2001. Use offset=2001, limit=2000 to continue reading if needed.]")
    assert "[Truncated" not in result
    continuation = read_file(str(f), offset=2001)
    assert continuation.endswith("2001|x\n")
    assert "[Truncated" not in continuation


@pytest.mark.parametrize("bound", [False, True], ids=["standalone", "runtime-bound"])
async def test_read_file_explicit_limit_can_exceed_default(tmp_path: Path, bound: bool) -> None:
    f = tmp_path / "larger-page.txt"
    f.write_text("x\n" * 2500, encoding="utf-8")
    runtime = SessionEnvironment.capture(workspace=Workspace.from_cwd(str(tmp_path)))
    reader = FilesystemTools(runtime).read_file if bound else read_file

    contents = await reader.invoke(arguments={"path": str(f), "limit": 2500, "max_tokens": 20_000})
    result = "".join(content.text or "" for content in contents)

    assert result.endswith("2500|x\n")
    assert "[Truncated" not in result


def test_read_file_token_budget_hint_preserves_single_line_read(tmp_path: Path) -> None:
    f = tmp_path / "long-line.txt"
    line = "x" * 5000
    f.write_text(line + "\n", encoding="utf-8")

    result = read_file(str(f), limit=1, max_tokens=100)
    continuation = re.search(r"Use offset=(\d+), limit=(\d+) to continue reading", result)
    assert continuation is not None
    resumed = read_file(str(f), offset=int(continuation[1]), limit=int(continuation[2]), max_tokens=2000)

    assert resumed.endswith(f"1|{line}\n")
    assert "[truncated]" not in resumed
    assert "[Truncated" not in resumed
    assert "[Long lines" not in resumed


@pytest.mark.parametrize("limit", [None, 3], ids=["default-limit", "explicit-limit"])
@pytest.mark.parametrize(("max_tokens", "position"), [(1, "before"), (100, "at")])
def test_read_file_combined_truncation_advertises_full_line_recovery(
    tmp_path: Path, limit: int | None, max_tokens: int, position: str
) -> None:
    f = tmp_path / "combined-limits.txt"
    line = "x" * 5000
    f.write_text(f"short\n{line}\n", encoding="utf-8")
    arguments = {} if limit is None else {"limit": limit}

    result = read_file(str(f), offset=2, max_tokens=max_tokens, **arguments)

    assert f"[Truncated {position} line 2," in result
    assert f"Use offset=2, limit={limit or 2000} to continue reading" in result
    assert "[Long lines" not in result
    assert result.count("\n[") == 1
    recovery = re.search(r"To read this line in full, use offset=(\d+), limit=(\d+)", result)
    assert recovery is not None
    resumed = read_file(str(f), offset=int(recovery[1]), limit=int(recovery[2]), max_tokens=2000)

    assert resumed.endswith(f"2|{line}\n")
    assert "[truncated]" not in resumed
    assert "[Truncated" not in resumed
    assert "[Long lines" not in resumed


def test_read_file_max_tokens_takes_priority_and_repeats_partial_line(tmp_path: Path) -> None:
    f = tmp_path / "budget.txt"
    f.write_text("short\n" + "x" * 1000 + "\nthird\n", encoding="utf-8")

    result = read_file(str(f), limit=3, max_tokens=100)
    assert "1|short\n" in result
    assert "2|xxx" in result
    assert "3|third" not in result
    assert "Content exceeds max_tokens=100. Use offset=2" in result
    assert "Content exceeds limit=" not in result
    continuation = read_file(str(f), offset=2, limit=1, max_tokens=1000)
    assert "2|" + "x" * 1000 + "\n" in continuation
    assert "Use offset=3, limit=1" in continuation


@pytest.mark.parametrize("content", ["汉" * 1000 + "x" * 10000 + "\n", "a\n" * 2000])
def test_read_file_token_budget_bounds_returned_content(tmp_path: Path, content: str) -> None:
    from chrys.foundation.text.tokenizer import MixedLanguageTokenizer

    f = tmp_path / "budget.txt"
    f.write_text(content, encoding="utf-8")
    result = read_file(str(f), max_tokens=200)
    header_and_content = result.split("\n\n[", 1)[0]
    assert "1|" in header_and_content
    assert MixedLanguageTokenizer().count_tokens(header_and_content) <= 200
    assert "Content exceeds max_tokens=200" in result


@pytest.mark.parametrize("offset", [1, 2])
@pytest.mark.parametrize("budget_mode", ["below-header", "below-partial-marker"])
def test_read_file_empty_budget_reports_before_requested_line(tmp_path: Path, offset: int, budget_mode: str) -> None:
    from chrys.foundation.text.tokenizer import MixedLanguageTokenizer

    f = tmp_path / "tiny-budget.txt"
    f.write_text(("x" * 200 + "\n") * 2, encoding="utf-8")
    header = read_file(str(f)).partition("\n")[0] + "\n"
    max_tokens = 1 if budget_mode == "below-header" else MixedLanguageTokenizer().count_tokens(header) + 2

    result = read_file(str(f), offset=offset, limit=1, max_tokens=max_tokens)

    assert f"\n{offset}|" not in result
    assert f"[Truncated before line {offset}," in result
    assert f"Use offset={offset}" in result
    assert "Truncated at line 0" not in result
    assert "[Long lines" not in result


def test_read_file_token_truncation_has_one_notice_for_a_short_line(tmp_path: Path) -> None:
    from chrys.foundation.text.tokenizer import MixedLanguageTokenizer

    f = tmp_path / "short-line.txt"
    f.write_text("x" * 200 + "\n", encoding="utf-8")
    header = read_file(str(f)).partition("\n")[0] + "\n"
    result = read_file(str(f), limit=1, max_tokens=MixedLanguageTokenizer().count_tokens(header) + 20)

    assert "1|xxx" in result
    assert "... [truncated]" in result
    assert "Source line 1 has 200 chars." in result
    assert "To read this line in full" not in result
    assert "[Long lines" not in result
    assert result.count("\n[") == 1
    assert "Use offset=1" in result


def test_read_file_short_lines_use_the_cumulative_budget(tmp_path: Path) -> None:
    f = tmp_path / "short-lines.txt"
    f.write_text("abc\n" * 2000, encoding="utf-8")

    result = read_file(str(f), max_tokens=5000)

    assert "2000|abc\n" in result
    assert "[Truncated" not in result


def test_read_file_default_budget_allows_more_than_8000_tokens(tmp_path: Path) -> None:
    f = tmp_path / "larger-default.txt"
    f.write_text(("x" * 20 + "\n") * 1400, encoding="utf-8")

    result = read_file(str(f))

    assert result.endswith("1400|" + "x" * 20 + "\n")
    assert "[Truncated" not in result
    assert "Content exceeds max_tokens=8000" in read_file(str(f), max_tokens=8000)


def test_read_file_budget_exhausted_after_a_complete_line_reports_next_line(tmp_path: Path) -> None:
    from chrys.foundation.text.tokenizer import MixedLanguageTokenizer

    f = tmp_path / "next-line.txt"
    f.write_text("abc\n" + "x" * 200 + "\n", encoding="utf-8")
    header = read_file(str(f)).partition("\n")[0] + "\n"
    budget = MixedLanguageTokenizer().count_tokens(header + "1|abc\n") + 1

    result = read_file(str(f), max_tokens=budget)

    assert "1|abc\n" in result
    assert "2|" not in result
    assert "[Truncated before line 2," in result
    assert "Source line 2 has 200 chars." in result
    assert "Use offset=2" in result


@pytest.mark.parametrize("bound", [False, True], ids=["standalone", "runtime-bound"])
async def test_read_file_glm_schema_and_json_arguments(tmp_path: Path, bound: bool) -> None:
    """The provider sees optional scalar fields, and JSON arguments reach the real tool."""
    from chrys.kernel import Content, Message
    from chrys.service.llm.chat_completions.client import GLM
    from chrys.service.llm.chat_completions.request import build_request

    f = tmp_path / "wire.txt"
    f.write_text("one\ntwo\nthree\nfour\n", encoding="utf-8")
    runtime = SessionEnvironment.capture(workspace=Workspace.from_cwd(str(tmp_path)))
    reader = FilesystemTools(runtime).read_file if bound else read_file
    request = build_request([Message("user", ["Read lines 2-3."])], {"tools": [reader]}, model="glm-5.2", variant=GLM)
    schema = request["tools"][0]["function"]["parameters"]
    assert "$defs" not in schema
    assert schema["required"] == ["path"]
    assert set(schema["properties"]) == {"path", "offset", "limit", "max_tokens"}
    for name, default in (("offset", 1), ("limit", 2000), ("max_tokens", 10000)):
        field = schema["properties"][name]
        assert field["type"] == "integer"
        assert field["minimum"] == 1
        assert field["default"] == default
        assert f"Defaults to {default}" in field["description"]
        assert "$ref" not in field
        assert "anyOf" not in field
    assert "Optional" in schema["properties"]["limit"]["description"]
    assert "Optional" in schema["properties"]["max_tokens"]["description"]
    assert "Takes priority over limit" in schema["properties"]["max_tokens"]["description"]

    call = Content.from_function_call(
        "read", "read_file", arguments=json.dumps({"path": str(f), "offset": "2", "limit": "2"})
    )
    contents = await reader.invoke(arguments=call.parse_arguments())
    result = "".join(content.text or "" for content in contents)
    assert "2|two\n3|three\n" in result
    assert "1|one" not in result
    assert "4|four" not in result
    assert "Use offset=4, limit=2" in result
    default_contents = await reader.invoke(arguments={"path": str(f)})
    defaults = "".join(content.text or "" for content in default_contents)
    assert defaults.endswith("1|one\n2|two\n3|three\n4|four\n")


@pytest.mark.parametrize("parameter", ["offset", "limit", "max_tokens"])
@pytest.mark.parametrize("value", [0, -1, True, 1.5, None, [1, 2]])
async def test_read_file_json_arguments_reject_invalid_numbers(parameter: str, value: object) -> None:
    with pytest.raises(TypeError, match=f"(?s)Invalid arguments for 'read_file'.*{parameter}"):
        await read_file.invoke(arguments={"path": "unused.txt", parameter: value})


def test_read_file_empty_file(tmp_path: Path) -> None:
    """Empty file — no crash, '0 lines'."""
    f = tmp_path / "empty.txt"
    f.write_text("", encoding="utf-8")

    result = read_file(str(f))
    assert "0 lines" in result
    assert "0 chars" in result
    assert "[Truncated" not in result


def test_read_file_not_found(tmp_path: Path) -> None:
    """Non-existent file returns error."""
    result = read_file(str(tmp_path / "nope.txt"))
    assert "Error" in result
    assert "not found" in result


def test_read_file_falls_back_to_narrow_no_break_space_filename(tmp_path: Path) -> None:
    """read_file tolerates macOS screenshot U+202F whitespace in filenames."""
    real = tmp_path / "note 9.00.00\u202fAM.txt"
    real.write_text("hello", encoding="utf-8")
    requested = str(tmp_path / "note 9.00.00 AM.txt")

    result = read_file(requested)

    assert "1 lines" in result
    assert "1|hello" in result


@pytest.mark.skipif(_WINDOWS, reason="symlink creation is not generally available on Windows")
def test_session_document_handle_does_not_use_fuzzy_path_fallback(tmp_path: Path) -> None:
    session_dir = tmp_path / "session"
    artifact_dir = session_dir / "doc_converter"
    artifact_dir.mkdir(parents=True)
    outside = tmp_path / "outside.md"
    outside.write_text("outside secret", encoding="utf-8")
    (artifact_dir / "report\u202fnine.md").symlink_to(outside)
    handle = make_document_artifact_handle("report nine.md")
    runtime = SessionEnvironment.capture(workspace=Workspace.from_cwd(str(tmp_path)))

    result = FilesystemTools(runtime, session_dir=session_dir).read_file(handle)

    assert result.startswith("Error:")
    assert "not found" in result
    assert "outside secret" not in result


@pytest.mark.skipif(_WINDOWS, reason="symlink creation is not generally available on Windows")
def test_session_document_handle_rejects_redirected_artifact_root(tmp_path: Path) -> None:
    session_dir = tmp_path / "session"
    outside = tmp_path / "outside"
    session_dir.mkdir()
    outside.mkdir()
    (outside / "report.md").write_text("outside secret", encoding="utf-8")
    (session_dir / "doc_converter").symlink_to(outside, target_is_directory=True)
    handle = make_document_artifact_handle("report.md")
    runtime = SessionEnvironment.capture(workspace=Workspace.from_cwd(str(tmp_path)))

    result = FilesystemTools(runtime, session_dir=session_dir).read_file(handle)

    assert result.startswith("Error:")
    assert "must not be redirected" in result
    assert "outside secret" not in result


def test_read_file_directory(tmp_path: Path) -> None:
    """Directory path returns error."""
    result = read_file(str(tmp_path))
    assert "Error" in result
    assert "directory" in result


@pytest.mark.parametrize("parameter", ["offset", "limit", "max_tokens"])
@pytest.mark.parametrize("value", [0, -1, True, 1.5, None, "2", [1, 2]])
def test_read_file_pagination_validation(tmp_path: Path, parameter: str, value: object) -> None:
    """Direct Python callers also receive actionable errors for invalid pagination."""
    f = tmp_path / "val.txt"
    f.write_text("a\nb\nc\n", encoding="utf-8")

    result = read_file(str(f), **{parameter: value})
    assert result.startswith(f"Error: {parameter} must be an integer >= 1")


def test_read_file_pagination_beyond_file(tmp_path: Path) -> None:
    """Start past EOF returns error."""
    f = tmp_path / "short.txt"
    f.write_text("one\ntwo\n", encoding="utf-8")

    result = read_file(str(f), offset=10)
    assert "Error" in result
    assert "beyond" in result


def test_read_file_pagination_end_beyond_file(tmp_path: Path) -> None:
    """A limit extending beyond EOF returns the remaining lines without error."""
    f = tmp_path / "short.txt"
    f.write_text("one\ntwo\nthree\n", encoding="utf-8")

    result = read_file(str(f), offset=2, limit=998)
    assert "Error" not in result
    assert "2|two" in result
    assert "3|three" in result
    assert "3 lines" in result  # header still shows total


def test_read_file_pagination_single_line(tmp_path: Path) -> None:
    """limit=1 returns exactly the requested line."""
    f = tmp_path / "five.txt"
    lines = [f"L{i}" for i in range(1, 6)]
    f.write_text("\n".join(lines) + "\n", encoding="utf-8")

    result = read_file(str(f), offset=3, limit=1)
    assert "3|L3" in result
    assert "2|L2" not in result
    assert "4|L4" not in result
    assert "5 lines" in result  # header shows total


def test_read_file_binary(tmp_path: Path) -> None:
    """Binary file returns error instead of garbled content."""
    f = tmp_path / "image.png"
    f.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)

    result = read_file(str(f))
    assert "Error" in result
    assert "binary" in result


def test_view_image_returns_image_and_metadata(tmp_path: Path) -> None:
    """view_image returns image Content with persisted display metadata."""
    f = tmp_path / "pixel.png"
    f.write_bytes(
        base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAFgwJ/lG9n1wAAAABJRU5ErkJggg=="
        )
    )

    result = view_image(str(f))

    assert len(result) == 1
    image = result[0]
    assert image.type == "data"
    assert image.media_type == "image/png"
    assert image.additional_properties["width"] == 1
    assert image.additional_properties["height"] == 1
    assert image.additional_properties["media_type"] == "image/png"
    assert image.additional_properties["size"] > 0
    restored = image.to_dict(exclude_none=True)
    assert restored["additional_properties"]["width"] == 1


def test_view_image_uses_magic_bytes_when_extension_disagrees(tmp_path: Path) -> None:
    """view_image should not label JPEG bytes as PNG just because the extension says .png."""
    f = tmp_path / "mismatch.png"
    f.write_bytes(_jpeg_bytes())

    result = view_image(str(f))

    assert len(result) == 1
    image = result[0]
    assert image.type == "data"
    assert image.media_type == "image/jpeg"
    assert image.uri is not None
    assert image.uri.startswith("data:image/jpeg;base64,")
    assert image.additional_properties["media_type"] == "image/jpeg"


def test_view_image_converts_an_unsupported_format_behind_a_supported_name(tmp_path: Path) -> None:
    """A renamed BMP is no PNG model APIs read: it goes out converted, like an oversized image."""
    f = tmp_path / "renamed.png"
    f.write_bytes(image_bytes("BMP"))

    result = view_image(str(f))

    assert len(result) == 1
    image = result[0]
    assert image.type == "data"
    assert image.media_type == "image/jpeg"
    assert image.uri is not None
    assert base64.b64decode(image.uri.partition(",")[2]).startswith(b"\xff\xd8\xff")


def test_view_image_refuses_a_format_pillow_may_not_decode(tmp_path: Path) -> None:
    """A TIFF behind an image name never reaches Pillow's TIFF decoder."""
    f = tmp_path / "renamed.png"
    f.write_bytes(image_bytes("TIFF"))

    result = view_image(str(f))

    assert len(result) == 1
    assert result[0].type == "text"
    text = result[0].text or ""
    assert text.startswith("Error: ")
    assert "could not be read as a supported image" in text


def test_runtime_bound_view_image_resolves_relative_paths_from_runtime_cwd(tmp_path: Path, monkeypatch) -> None:
    """Instance view_image should resolve relative paths against the workspace cwd."""
    project = tmp_path / "project"
    other = tmp_path / "other"
    project.mkdir()
    other.mkdir()
    image = project / "pixel.png"
    image.write_bytes(
        base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAFgwJ/lG9n1wAAAABJRU5ErkJggg=="
        )
    )
    monkeypatch.chdir(other)

    runtime = SessionEnvironment.capture(workspace=Workspace.from_cwd(str(project)))
    result = FilesystemTools(runtime).view_image("pixel.png")

    assert len(result) == 1
    assert result[0].media_type == "image/png"
    assert result[0].additional_properties["source_path"] == str(image)


def test_runtime_bound_view_image_resolves_exact_session_artifact_handle(tmp_path: Path) -> None:
    session_dir = tmp_path / "session"
    artifact_dir = session_dir / "doc_converter"
    artifact_dir.mkdir(parents=True)
    image = artifact_dir / "pixel.png"
    image.write_bytes(
        base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAFgwJ/lG9n1wAAAABJRU5ErkJggg=="
        )
    )
    runtime = SessionEnvironment.capture(workspace=Workspace.from_cwd(str(tmp_path)))

    result = FilesystemTools(runtime, session_dir=session_dir).view_image("chrys-session-document:pixel.png")

    assert len(result) == 1
    assert result[0].media_type == "image/png"
    assert result[0].additional_properties["source_path"] == str(image)

    read_result = FilesystemTools(runtime, session_dir=session_dir).read_file("chrys-session-document:pixel.png")
    assert "invalid session artifact handle" in read_result


def test_runtime_bound_view_image_rejects_markdown_session_artifact_handle(tmp_path: Path) -> None:
    session_dir = tmp_path / "session"
    artifact_dir = session_dir / "doc_converter"
    artifact_dir.mkdir(parents=True)
    (artifact_dir / "report.md").write_text("# Report", encoding="utf-8")
    runtime = SessionEnvironment.capture(workspace=Workspace.from_cwd(str(tmp_path)))

    result = FilesystemTools(runtime, session_dir=session_dir).view_image("chrys-session-document:report.md")

    assert len(result) == 1
    assert result[0].type == "text"
    assert "invalid session artifact handle" in (result[0].text or "")


def test_runtime_bound_view_image_does_not_fuzzy_resolve_session_artifact_handle(tmp_path: Path) -> None:
    session_dir = tmp_path / "session"
    artifact_dir = session_dir / "doc_converter"
    artifact_dir.mkdir(parents=True)
    (artifact_dir / "pixel\u202f.png").write_bytes(
        base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAFgwJ/lG9n1wAAAABJRU5ErkJggg=="
        )
    )
    runtime = SessionEnvironment.capture(workspace=Workspace.from_cwd(str(tmp_path)))

    result = FilesystemTools(runtime, session_dir=session_dir).view_image("chrys-session-document:pixel .png")

    assert len(result) == 1
    assert result[0].type == "text"
    assert "file not found" in (result[0].text or "")


def test_view_image_errors_are_text_content(tmp_path: Path) -> None:
    """view_image returns Error-prefixed text content for failures."""
    result = view_image(str(tmp_path / "missing.png"))

    assert len(result) == 1
    assert result[0].type == "text"
    assert (result[0].text or "").startswith("Error: ")


def test_view_image_error_with_surrogate_path_is_utf8_safe(tmp_path: Path) -> None:
    result = view_image(str(tmp_path / "missing-\udcff.png"))
    text = result[0].text or ""

    text.encode("utf-8")
    assert r"missing-\udcff.png" in text


def test_view_image_success_metadata_with_surrogate_path_is_utf8_safe(monkeypatch) -> None:
    import chrys.service.tools.builtins.filesystem as filesystem_module

    raw_path = "/work/image-\udcff.png"
    monkeypatch.setattr(filesystem_module, "resolve_existing_path", lambda path, *, base_cwd=None: raw_path)
    monkeypatch.setattr(
        filesystem_module,
        "load_image_file",
        lambda path, *, max_bytes: LoadedImage(data=_jpeg_bytes(), media_type="image/jpeg", width=2, height=2),
    )

    result = view_image(raw_path)

    source_path = result[0].additional_properties["source_path"]
    assert source_path == r"/work/image-\udcff.png"
    payload = json.dumps(result[0].to_dict(exclude_none=True), ensure_ascii=False)
    payload.encode("utf-8")


def test_view_image_falls_back_to_narrow_no_break_space_filename(tmp_path: Path) -> None:
    """view_image tolerates macOS screenshot U+202F whitespace in filenames."""
    real = tmp_path / "shot 9.00.00\u202fAM.png"
    real.write_bytes(
        base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAFgwJ/lG9n1wAAAABJRU5ErkJggg=="
        )
    )
    requested = str(tmp_path / "shot 9.00.00 AM.png")

    result = view_image(requested)

    assert len(result) == 1
    assert result[0].type == "data"
    assert result[0].media_type == "image/png"
    assert result[0].additional_properties["source_path"] == str(real)


# -- edit_file ---------------------------------------------------------------


def test_edit_file_gbk_roundtrip(tmp_path: Path) -> None:
    """edit_file reads and writes back GBK files preserving encoding."""
    f = tmp_path / "gbk.txt"
    # Larger sample so charset_normalizer can reliably detect GBK
    gbk_content = "你好世界\n这是一个使用GBK编码的测试文件\n再见\n" * 5
    f.write_bytes(gbk_content.encode("gbk"))

    result = edit_file(str(f), old_string="你好世界", new_string="哈喽世界", replace_all=True)
    assert "Replaced 5" in result

    raw = f.read_bytes()
    assert "哈喽世界".encode("gbk") in raw
    assert "再见".encode("gbk") in raw


def test_edit_file_damaged_utf8_stays_utf8(tmp_path: Path) -> None:
    """A mostly-valid UTF-8 file should not be rewritten through a legacy codec."""
    f = tmp_path / "damaged_utf8.txt"
    original = "你好世界\n中文文本\n" * 10
    f.write_bytes(original.encode("utf-8") + b"\xff\n")

    result = edit_file(str(f), old_string="你好世界", new_string="再见世界", replace_all=True)
    assert "Replaced 10" in result

    raw = f.read_bytes()
    assert "再见世界".encode() in raw
    assert "再见世界".encode("gbk") not in raw
    assert raw.endswith(b"\xff\n")
    assert "\udcff" not in result


def test_surrogate_display_replacement_is_utf8_safe_and_length_preserving() -> None:
    source = "before-\ud800-middle-\udcff-after"

    display = _replace_surrogate_escapes(source)

    display.encode("utf-8")
    assert len(display) == len(source)
    assert display == "before-�-middle-�-after"


def test_edit_file_snippet_keeps_raw_offset_after_surrogateescaped_byte(tmp_path: Path) -> None:
    f = tmp_path / "damaged_before_match.txt"
    prefix = ("valid utf8 你好世界\n" * 40).encode("utf-8")
    f.write_bytes(prefix + b"damaged-\xff\nTARGET\ntrailing\n")

    result = edit_file(str(f), old_string="TARGET", new_string="REPLACED")

    result.encode("utf-8")
    after = result.split("After:\n", 1)[1]
    assert "42|REPLACED" in after
    assert "REPLACEDARGET" not in after
    assert f.read_bytes() == prefix + b"damaged-\xff\nREPLACED\ntrailing\n"


def test_edit_file_preserves_invalid_utf8_outside_detection_sample(tmp_path: Path) -> None:
    """edit_file decodes the full file losslessly even if the sample is valid."""
    f = tmp_path / "late_damage.txt"
    prefix = ("target line\n" + ("valid utf8 你好世界\n" * 5000)).encode()
    f.write_bytes(prefix + b"\xff\n")

    result = edit_file(str(f), old_string="target line", new_string="updated line")
    assert "Replaced 1" in result

    raw = f.read_bytes()
    assert raw.startswith(b"updated line\n")
    assert raw.endswith(b"\xff\n")


def test_edit_file_binary_rejected(tmp_path: Path) -> None:
    """edit_file rejects binary files."""
    f = tmp_path / "bin.dat"
    f.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)

    result = edit_file(str(f), old_string="a", new_string="b")
    assert "Error" in result
    assert "binary" in result


def test_plan_edit_file_previews_without_writing(tmp_path: Path) -> None:
    """edit_file preview returns the planned diff inputs without mutating disk."""
    f = tmp_path / "preview.py"
    f.write_text("before\nneedle\nafter\n", encoding="utf-8")

    plan = plan_edit_file(str(f), "needle", "replacement")

    assert isinstance(plan, EditFilePlan)
    assert plan.before_text == "before\nneedle\nafter\n"
    assert plan.after_text == "before\nreplacement\nafter\n"
    assert plan.replacements == 1
    assert f.read_text(encoding="utf-8") == "before\nneedle\nafter\n"


def test_plan_edit_file_old_not_found_returns_tool_error(tmp_path: Path) -> None:
    f = tmp_path / "preview.py"
    f.write_text("hello\n", encoding="utf-8")

    plan = plan_edit_file(str(f), "missing", "replacement")

    assert isinstance(plan, FileToolPreviewError)
    assert plan.kind == "not_found"
    assert "old_string not found" in plan.message
    assert edit_file(str(f), "missing", "replacement") == plan.message


def test_edit_file_error_with_surrogate_path_is_utf8_safe(tmp_path: Path) -> None:
    result = edit_file(str(tmp_path / "missing-\udcff.txt"), "old", "new")

    result.encode("utf-8")
    assert r"missing-\udcff.txt" in result


def test_write_and_edit_reject_document_handles_without_creating_files(tmp_path: Path) -> None:
    handle = make_document_artifact_handle("report.md")
    runtime = SessionEnvironment.capture(workspace=Workspace.from_cwd(str(tmp_path)))
    tools = FilesystemTools(runtime)

    write_result = tools.write_file(handle, "junk")
    edit_result = tools.edit_file(handle, "old", "new")

    assert "read-only" in write_result
    assert "read-only" in edit_result
    assert not (tmp_path / handle).exists()


# -- _detect_line_ending / _default_eol_for_new_file -------------------------


def test_detect_line_ending_crlf() -> None:
    assert _detect_line_ending(b"a\r\nb\r\nc\r\n") == "\r\n"


def test_detect_line_ending_lf() -> None:
    assert _detect_line_ending(b"a\nb\nc\n") == "\n"


def test_detect_line_ending_cr() -> None:
    assert _detect_line_ending(b"a\rb\rc\r") == "\r"


def test_detect_line_ending_mixed_crlf_wins() -> None:
    """When CRLF count >= LF count, CRLF wins."""
    assert _detect_line_ending(b"a\r\nb\nc\r\n") == "\r\n"


def test_detect_line_ending_mixed_lf_wins() -> None:
    """When LF count > CRLF count, LF wins."""
    assert _detect_line_ending(b"a\r\nb\nc\nd\n") == "\n"


def test_detect_line_ending_no_newlines_fallback() -> None:
    """No newlines → falls back to _default_eol_for_new_file."""
    assert _detect_line_ending(b"single line", "/tmp/script.sh") == "\n"
    assert _detect_line_ending(b"single line", "/tmp/run.bat") == "\r\n"


def test_detect_line_ending_empty_fallback() -> None:
    """Empty content → falls back to _default_eol_for_new_file."""
    assert _detect_line_ending(b"", "/tmp/test.sh") == "\n"
    assert _detect_line_ending(b"", "/tmp/test.bat") == "\r\n"


def test_default_eol_shell_scripts() -> None:
    for ext in (".sh", ".bash", ".zsh", ".fish", ".ksh", ".csh", ".tcsh", ".command"):
        assert _default_eol_for_new_file(f"/tmp/script{ext}") == "\n", ext


def test_default_eol_interpreters() -> None:
    for ext in (".py", ".rb", ".pl", ".php"):
        assert _default_eol_for_new_file(f"/tmp/file{ext}") == "\n", ext


def test_default_eol_yaml() -> None:
    assert _default_eol_for_new_file("/tmp/config.yml") == "\n"
    assert _default_eol_for_new_file("/tmp/config.yaml") == "\n"


def test_default_eol_batch() -> None:
    assert _default_eol_for_new_file("/tmp/run.bat") == "\r\n"
    assert _default_eol_for_new_file("/tmp/run.cmd") == "\r\n"


def test_default_eol_case_insensitive() -> None:
    assert _default_eol_for_new_file("/tmp/RUN.BAT") == "\r\n"
    assert _default_eol_for_new_file("/tmp/SCRIPT.SH") == "\n"


# -- write_file ---------------------------------------------------------------


def test_write_file_new(tmp_path: Path) -> None:
    """New file is created with correct content."""
    f = tmp_path / "new.txt"
    result = write_file(str(f), "hello\nworld\n")
    assert "Written" in result
    assert f.read_text(encoding="utf-8") == "hello\nworld\n"


@pytest.mark.skipif(_WINDOWS, reason="POSIX file modes are not available on Windows")
def test_write_file_new_uses_posix_mode_policy(tmp_path: Path) -> None:
    regular = tmp_path / ".env"
    python_without_shebang = tmp_path / "worker.py"
    shell_by_extension = tmp_path / "deploy.sh"
    tcsh_by_extension = tmp_path / "legacy.tcsh"
    command_by_extension = tmp_path / "launch.command"
    script_by_shebang = tmp_path / "runner"

    with _temporary_umask(0o077):
        write_file(str(regular), "notes\n")
        write_file(str(python_without_shebang), "print('ok')\n")
        write_file(str(shell_by_extension), "echo ok\n")
        write_file(str(tcsh_by_extension), "echo ok\n")
        write_file(str(command_by_extension), "open .\n")
        write_file(str(script_by_shebang), "#!/usr/bin/env python3\nprint('ok')\n")

    assert stat.S_IMODE(regular.stat().st_mode) == 0o600
    assert stat.S_IMODE(python_without_shebang.stat().st_mode) == 0o600
    assert stat.S_IMODE(shell_by_extension.stat().st_mode) == 0o700
    assert stat.S_IMODE(tcsh_by_extension.stat().st_mode) == 0o700
    assert stat.S_IMODE(command_by_extension.stat().st_mode) == 0o700
    assert stat.S_IMODE(script_by_shebang.stat().st_mode) == 0o700


def test_write_file_exists_no_overwrite(tmp_path: Path) -> None:
    """Existing file without overwrite=True is rejected."""
    f = tmp_path / "exist.txt"
    f.write_text("old", encoding="utf-8")
    result = write_file(str(f), "new")
    assert "Error" in result
    assert "already exists" in result
    assert f.read_text(encoding="utf-8") == "old"


def test_write_file_overwrite(tmp_path: Path) -> None:
    """overwrite=True replaces existing file."""
    f = tmp_path / "exist.txt"
    f.write_text("old", encoding="utf-8")
    result = write_file(str(f), "new content\n", overwrite=True)
    assert "Written" in result
    assert f.read_text(encoding="utf-8") == "new content\n"


@pytest.mark.skipif(_WINDOWS, reason="POSIX file modes are not available on Windows")
def test_write_file_overwrite_preserves_posix_mode(tmp_path: Path) -> None:
    f = tmp_path / "existing.sh"
    f.write_text("echo old\n", encoding="utf-8")
    f.chmod(0o640)

    with _temporary_umask(0o077):
        result = write_file(str(f), "echo new\n", overwrite=True)

    assert "Written" in result
    assert stat.S_IMODE(f.stat().st_mode) == 0o640


def test_write_file_creates_parent_dirs(tmp_path: Path) -> None:
    """Parent directories are created automatically."""
    f = tmp_path / "a" / "b" / "c" / "deep.txt"
    result = write_file(str(f), "deep\n")
    assert "Written" in result
    assert f.exists()


def test_write_file_preserves_crlf_on_overwrite(tmp_path: Path) -> None:
    """Overwriting a CRLF file preserves CRLF."""
    f = tmp_path / "win.txt"
    f.write_bytes(b"old\r\ncontent\r\n")
    write_file(str(f), "new\ncontent\n", overwrite=True)
    assert f.read_bytes() == b"new\r\ncontent\r\n"


def test_write_file_new_sh_gets_lf(tmp_path: Path) -> None:
    """New .sh file always uses LF."""
    f = tmp_path / "deploy.sh"
    write_file(str(f), "#!/bin/bash\necho hi\n")
    assert b"\r" not in f.read_bytes()


def test_write_file_new_bat_gets_crlf(tmp_path: Path) -> None:
    """New .bat file always uses CRLF."""
    f = tmp_path / "run.bat"
    write_file(str(f), "@echo off\necho hi\n")
    assert f.read_bytes() == b"@echo off\r\necho hi\r\n"


def test_write_file_normalizes_mixed_eol(tmp_path: Path) -> None:
    """LLM sends mixed \\r\\n and \\n — output is consistent."""
    f = tmp_path / "crlf.txt"
    f.write_bytes(b"old\r\n")
    write_file(str(f), "line1\r\nline2\nline3\r\n", overwrite=True)
    raw = f.read_bytes()
    # All should be CRLF (preserving original style), no \r\r\n
    assert b"\r\r\n" not in raw
    assert raw == b"line1\r\nline2\r\nline3\r\n"


def test_plan_write_file_previews_without_writing(tmp_path: Path) -> None:
    """write_file preview returns final content and leaves disk untouched."""
    f = tmp_path / "preview.txt"

    plan = plan_write_file(str(f), "hello\nworld\n")

    assert isinstance(plan, WriteFilePlan)
    expected = "hello\nworld\n".replace("\n", _default_eol_for_new_file(str(f)))
    assert plan.display_content == expected
    assert not f.exists()


def test_plan_write_file_exists_no_overwrite_returns_tool_error(tmp_path: Path) -> None:
    f = tmp_path / "exist.txt"
    f.write_text("old", encoding="utf-8")

    plan = plan_write_file(str(f), "new")

    assert isinstance(plan, FileToolPreviewError)
    assert plan.kind == "exists"
    assert "already exists" in plan.message
    assert write_file(str(f), "new") == plan.message
    assert f.read_text(encoding="utf-8") == "old"


def test_plan_write_file_error_with_surrogate_path_is_utf8_safe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import chrys.service.tools.builtins.filesystem as filesystem_module

    monkeypatch.setattr(filesystem_module.os.path, "isdir", lambda _path: True)
    plan = plan_write_file(str(tmp_path / "missing-\udcff.txt"), "content")

    assert isinstance(plan, FileToolPreviewError)
    plan.message.encode("utf-8")
    assert r"missing-\udcff.txt" in plan.message


def test_plan_write_file_preserves_crlf_in_preview(tmp_path: Path) -> None:
    f = tmp_path / "win.txt"
    f.write_bytes(b"old\r\n")

    plan = plan_write_file(str(f), "new\ncontent\n", overwrite=True)

    assert isinstance(plan, WriteFilePlan)
    assert plan.write_content == "new\r\ncontent\r\n"
    assert plan.before_display_content == "old\n"
    assert plan.overwrites_existing is True
    assert plan.content_only_reason == ""
    assert f.read_bytes() == b"old\r\n"


def test_plan_write_file_binary_signature_uses_content_only_preview(tmp_path: Path) -> None:
    f = tmp_path / "existing.pdf"
    raw = b"%PDF-1.7\n1 0 obj\n<< /Type /Catalog >>\nendobj\n"
    f.write_bytes(raw)

    plan = plan_write_file(str(f), "replacement\n", overwrite=True)

    assert isinstance(plan, WriteFilePlan)
    assert plan.before_display_content == ""
    assert plan.content_only_reason == f"content-only; existing binary file ({len(raw)} bytes) not shown"
    assert f.read_bytes() == raw


# -- UTF-8 BOM handling -------------------------------------------------------


def test_read_file_strips_utf8_bom(tmp_path: Path) -> None:
    """read_file must not surface the BOM character (U+FEFF) to the LLM.
    Python's utf-8-sig codec strips it on read; verify nothing breaks that."""
    f = tmp_path / "bom.txt"
    f.write_bytes(codecs.BOM_UTF8 + b"hello\nworld\n")

    out = read_file(str(f))
    assert "\ufeff" not in out
    assert "1|hello" in out
    assert "2|world" in out


def test_read_file_utf8_bom_with_cjk(tmp_path: Path) -> None:
    """UTF-8-BOM file with CJK content reads cleanly (no BOM, no garbling)."""
    f = tmp_path / "bom_cjk.txt"
    text = "你好世界\n中文测试\n"
    f.write_bytes(codecs.BOM_UTF8 + text.encode("utf-8"))

    out = read_file(str(f))
    assert "\ufeff" not in out
    assert "你好世界" in out
    assert "中文测试" in out


def test_write_file_new_has_no_bom(tmp_path: Path) -> None:
    """New files must NOT be written with a BOM under any circumstances."""
    f = tmp_path / "fresh.txt"
    write_file(str(f), "hello\nworld\n")
    raw = f.read_bytes()
    assert not raw.startswith(codecs.BOM_UTF8)


def test_write_file_overwrite_strips_existing_bom(tmp_path: Path) -> None:
    """Overwriting a UTF-8-BOM file with write_file produces plain UTF-8.
    write_file always uses the default utf-8 encoding (not utf-8-sig), so
    the BOM is dropped — this is the desired behavior."""
    f = tmp_path / "was_bom.txt"
    f.write_bytes(codecs.BOM_UTF8 + b"old content\n")

    result = write_file(str(f), "new content\n", overwrite=True)
    assert "Written" in result
    raw = f.read_bytes()
    assert not raw.startswith(codecs.BOM_UTF8)
    assert raw == b"new content\n"


def test_edit_file_preserves_utf8_bom(tmp_path: Path) -> None:
    """edit_file currently round-trips the BOM (the source encoding is
    detected as utf-8-sig and reused for write). Documents current behavior;
    if BOM stripping becomes the policy, update this test."""
    f = tmp_path / "edit_bom.txt"
    f.write_bytes(codecs.BOM_UTF8 + b"hello world\n")

    result = edit_file(str(f), "hello", "goodbye")
    assert "Replaced 1" in result
    raw = f.read_bytes()
    assert raw.startswith(codecs.BOM_UTF8)
    assert raw == codecs.BOM_UTF8 + b"goodbye world\n"


def test_edit_file_utf8_bom_cjk_roundtrip(tmp_path: Path) -> None:
    """edit_file on a UTF-8-BOM CJK file: content edits correctly, BOM
    preserved, no double-BOM."""
    f = tmp_path / "edit_bom_cjk.txt"
    original = "你好世界\n中文文本\n"
    f.write_bytes(codecs.BOM_UTF8 + original.encode("utf-8"))

    result = edit_file(str(f), "你好", "再见")
    assert "Replaced 1" in result
    raw = f.read_bytes()
    # Exactly one BOM at the start, not two.
    assert raw.startswith(codecs.BOM_UTF8)
    assert codecs.BOM_UTF8 not in raw[3:]
    decoded = raw[3:].decode("utf-8")
    assert decoded == "再见世界\n中文文本\n"


# -- edit_file — basic --------------------------------------------------------


def test_edit_file_basic(tmp_path: Path) -> None:
    """Simple single replacement."""
    f = tmp_path / "e.txt"
    f.write_text("aaa\nbbb\nccc\n", encoding="utf-8")
    result = edit_file(str(f), "bbb", "XXX")
    assert "Replaced 1" in result
    assert f.read_text(encoding="utf-8") == "aaa\nXXX\nccc\n"


@pytest.mark.skipif(_WINDOWS, reason="POSIX file modes are not available on Windows")
def test_edit_file_preserves_posix_mode(tmp_path: Path) -> None:
    f = tmp_path / "run.sh"
    f.write_text("#!/bin/sh\necho old\n", encoding="utf-8")
    f.chmod(0o751)

    with _temporary_umask(0o077):
        result = edit_file(str(f), "echo old", "echo new")

    assert "Replaced 1" in result
    assert stat.S_IMODE(f.stat().st_mode) == 0o751


def test_edit_file_replace_all(tmp_path: Path) -> None:
    """replace_all=True replaces every occurrence."""
    f = tmp_path / "e.txt"
    f.write_text("foo bar foo baz foo\n", encoding="utf-8")
    result = edit_file(str(f), "foo", "qux", replace_all=True)
    assert "Replaced 3" in result
    assert f.read_text(encoding="utf-8") == "qux bar qux baz qux\n"


def test_edit_file_not_found(tmp_path: Path) -> None:
    result = edit_file(str(tmp_path / "nope.txt"), "a", "b")
    assert "Error" in result
    assert "not found" in result


def test_edit_file_directory(tmp_path: Path) -> None:
    result = edit_file(str(tmp_path), "a", "b")
    assert "Error" in result
    assert "directory" in result


def test_edit_file_old_not_found(tmp_path: Path) -> None:
    """old_string not in file → error with helpful message."""
    f = tmp_path / "e.txt"
    f.write_text("hello world\n", encoding="utf-8")
    result = edit_file(str(f), "xyz", "abc")
    assert "Error" in result
    assert "not found" in result
    assert "line number" in result  # mentions the prefix hint


def test_edit_file_not_unique(tmp_path: Path) -> None:
    """old_string appears multiple times without replace_all → error."""
    f = tmp_path / "e.txt"
    f.write_text("aaa\nbbb\naaa\n", encoding="utf-8")
    result = edit_file(str(f), "aaa", "zzz")
    assert "Error" in result
    assert "2 times" in result


def test_edit_file_identical(tmp_path: Path) -> None:
    """old_string == new_string → error."""
    f = tmp_path / "e.txt"
    f.write_text("hello\n", encoding="utf-8")
    result = edit_file(str(f), "hello", "hello")
    assert "Error" in result
    assert "identical" in result


@pytest.mark.parametrize("existing", [b"abc\n", b"", None], ids=["text", "empty", "missing"])
@pytest.mark.parametrize("replace_all", [False, True], ids=["one", "all"])
def test_edit_file_rejects_an_empty_old_string(tmp_path: Path, existing: bytes | None, replace_all: bool) -> None:
    """An empty old_string names no place in the file: the model is pointed at write_file."""
    f = tmp_path / "e.txt"
    if existing is not None:
        f.write_bytes(existing)

    plan = plan_edit_file(str(f), "", "X", replace_all=replace_all)
    result = edit_file(str(f), "", "X", replace_all=replace_all)

    assert isinstance(plan, FileToolPreviewError)
    assert plan.kind == "empty_old_string"
    assert result.startswith(
        "Error: old_string is empty. To create a file or replace a file's whole content, use write_file"
    )
    if existing is None:
        assert not f.exists()
    else:
        assert f.read_bytes() == existing


# -- edit_file — EOL handling --------------------------------------------------


def test_edit_file_preserves_crlf(tmp_path: Path) -> None:
    """Editing a CRLF file preserves CRLF line endings."""
    f = tmp_path / "win.py"
    f.write_bytes(b"import os\r\nimport sys\r\n\r\ndef hello():\r\n    pass\r\n")
    edit_file(str(f), "import os\nimport sys", "import os\nimport sys\nimport json")
    raw = f.read_bytes()
    assert raw == b"import os\r\nimport sys\r\nimport json\r\n\r\ndef hello():\r\n    pass\r\n"


def test_edit_file_preserves_lf(tmp_path: Path) -> None:
    """Editing a LF file keeps LF — no CRLF introduced."""
    f = tmp_path / "unix.py"
    f.write_bytes(b"import os\nimport sys\n")
    edit_file(str(f), "import os\nimport sys", "import os\nimport sys\nimport json")
    raw = f.read_bytes()
    assert b"\r" not in raw
    assert raw == b"import os\nimport sys\nimport json\n"


def test_edit_file_llm_sends_crlf_in_old_string(tmp_path: Path) -> None:
    """LLM sends \\r\\n in old_string — still matches \\n-normalized content."""
    f = tmp_path / "e.txt"
    f.write_bytes(b"aaa\r\nbbb\r\n")
    result = edit_file(str(f), "aaa\r\nbbb", "aaa\r\nXXX")
    assert "Replaced 1" in result
    assert f.read_bytes() == b"aaa\r\nXXX\r\n"


def test_edit_file_llm_sends_crlf_on_lf_file(tmp_path: Path) -> None:
    """LLM sends \\r\\n but file uses \\n — match still works, LF preserved."""
    f = tmp_path / "e.txt"
    f.write_bytes(b"aaa\nbbb\n")
    result = edit_file(str(f), "aaa\r\nbbb", "aaa\r\nXXX")
    assert "Replaced 1" in result
    assert f.read_bytes() == b"aaa\nXXX\n"


def test_edit_file_preserves_cr(tmp_path: Path) -> None:
    """Old Mac CR line endings are preserved."""
    f = tmp_path / "mac.txt"
    f.write_bytes(b"aaa\rbbb\rccc\r")
    edit_file(str(f), "bbb", "XXX")
    assert f.read_bytes() == b"aaa\rXXX\rccc\r"


# -- edit_file — diff output ---------------------------------------------------


def test_edit_file_diff_output_structure(tmp_path: Path) -> None:
    """Result contains Before/After sections with line numbers."""
    f = tmp_path / "d.txt"
    f.write_text("aaa\nbbb\nccc\nddd\neee\n", encoding="utf-8")
    result = edit_file(str(f), "ccc", "CCC")
    assert "Before:" in result
    assert "After:" in result
    assert "3|ccc" in result  # before
    assert "3|CCC" in result  # after


def test_edit_file_diff_context_lines(tmp_path: Path) -> None:
    """Diff shows 2 lines of context before and after the change."""
    f = tmp_path / "d.txt"
    f.write_text("L1\nL2\nL3\nL4\nL5\nL6\nL7\n", encoding="utf-8")
    result = edit_file(str(f), "L4", "X4")
    # Context: 2 before (L2, L3) + changed (L4) + 2 after (L5, L6)
    assert "2|L2" in result
    assert "3|L3" in result
    assert "5|L5" in result
    assert "6|L6" in result
    # L1 and L7 should NOT be in context
    assert "1|L1" not in result
    assert "7|L7" not in result


def test_edit_file_diff_at_start(tmp_path: Path) -> None:
    """Edit at line 1 — context doesn't go negative."""
    f = tmp_path / "d.txt"
    f.write_text("first\nsecond\nthird\n", encoding="utf-8")
    result = edit_file(str(f), "first", "FIRST")
    assert "1|FIRST" in result
    assert "Before:" in result


def test_edit_file_diff_at_end(tmp_path: Path) -> None:
    """Edit near the end — context clamps to file length."""
    f = tmp_path / "d.txt"
    f.write_text("aaa\nbbb\nccc\n", encoding="utf-8")
    result = edit_file(str(f), "ccc", "CCC")
    assert "3|CCC" in result


def test_edit_file_diff_multiline_replace(tmp_path: Path) -> None:
    """Multi-line replacement shows correct before/after."""
    f = tmp_path / "d.txt"
    f.write_text("A\nB\nC\nD\nE\nF\nG\n", encoding="utf-8")
    result = edit_file(str(f), "C\nD\nE", "X\nY")
    # Before should show C, D, E; After should show X, Y
    assert "3|C" in result
    assert "5|E" in result
    assert "3|X" in result
    assert "4|Y" in result


def test_edit_file_diff_insertion(tmp_path: Path) -> None:
    """Insertion (expanding replacement) shows added lines."""
    f = tmp_path / "d.txt"
    f.write_text("A\nB\nC\n", encoding="utf-8")
    result = edit_file(str(f), "B", "B\nB2\nB3")
    assert "2|B" in result  # original
    assert "3|B2" in result  # inserted
    assert "4|B3" in result  # inserted


def test_edit_file_diff_elides_the_middle_of_each_side_over_thirty_lines(tmp_path: Path) -> None:
    """A side longer than 30 lines keeps its first and last 15 and names the lines left out."""
    f = tmp_path / "d.txt"
    before = [f"L{number}" for number in range(1, 51)]
    f.write_text("\n".join(before) + "\n", encoding="utf-8")
    inserted = [f"N{index}" for index in range(1, 41)]

    plan = plan_edit_file(str(f), "\n".join(before[5:40]), "\n".join(inserted))

    assert isinstance(plan, EditFilePlan)
    after = [*before[:5], *inserted, *before[40:]]
    assert plan.snippet == "\n".join(
        [
            "Before:",
            *(f"  {number}|{before[number - 1]}" for number in range(4, 19)),
            "  [... 9 lines omitted (19-27) ...]",
            *(f"  {number}|{before[number - 1]}" for number in range(28, 43)),
            "After:",
            *(f"  {number}|{after[number - 1]}" for number in range(4, 19)),
            "  [... 14 lines omitted (19-32) ...]",
            *(f"  {number}|{after[number - 1]}" for number in range(33, 48)),
        ]
    )


@pytest.mark.parametrize("side_lines", [30, 31])
def test_edit_file_diff_keeps_a_side_of_up_to_thirty_one_lines_whole(tmp_path: Path, side_lines: int) -> None:
    """Hiding a single line would save nothing: the marker takes its place."""
    f = tmp_path / "d.txt"
    f.write_text("L1\nL2\nL3\nL4\nL5\n", encoding="utf-8")
    inserted = [f"N{index}" for index in range(1, side_lines - 3)]

    plan = plan_edit_file(str(f), "L3", "\n".join(inserted))

    assert isinstance(plan, EditFilePlan)
    after = ["L1", "L2", *inserted, "L4", "L5"]
    assert len(after) == side_lines
    assert plan.snippet.split("After:\n")[1] == "\n".join(
        f"  {number}|{line}" for number, line in enumerate(after, start=1)
    )


# -- line numbers count newlines only ------------------------------------------

# str.splitlines also breaks at these; the file's lines, and grep's, do not.
# A vertical tab is the soft line break python-pptx keeps from a slide. The
# lines around one are long enough that the file still reads as text.
_IN_LINE_BREAKS = pytest.mark.parametrize(
    "separator",
    ["\x0b", "\x0c", "\x85", "\u2028"],
    ids=["vertical-tab", "form-feed", "next-line", "line-separator"],
)


@_IN_LINE_BREAKS
def test_read_file_numbers_lines_by_newline_only(tmp_path: Path, separator: str) -> None:
    f = tmp_path / "r.txt"
    f.write_bytes(f"first half{separator}second half\nnext line\n".encode())

    result = read_file(str(f))

    assert "(2 lines," in result
    assert result.endswith(f"1|first half{separator}second half\n2|next line\n")


@_IN_LINE_BREAKS
def test_edit_file_diff_numbers_lines_by_newline_only(tmp_path: Path, separator: str) -> None:
    f = tmp_path / "d.txt"
    f.write_bytes(f"first half{separator}second half\nL2\nL3\nL4\nL5\n".encode())

    result = edit_file(str(f), "L4", "X4")

    assert result.endswith("Before:\n  2|L2\n  3|L3\n  4|L4\n  5|L5\nAfter:\n  2|L2\n  3|L3\n  4|X4\n  5|L5")


@_IN_LINE_BREAKS
def test_write_file_counts_lines_by_newline_only(tmp_path: Path, separator: str) -> None:
    content = f"first half{separator}second half\nnext line\n"

    result = write_file(str(tmp_path / "w.txt"), content)

    assert result.startswith(f"Written {len(content)} chars (2 lines) to ")


# -- read_file — long-line truncation ------------------------------------------


def test_read_file_long_line_truncated(tmp_path: Path) -> None:
    """Lines exceeding _MAX_LINE_DISPLAY_CHARS are truncated with a summary."""
    short = "short line"
    long_line = "x" * (_MAX_LINE_DISPLAY_CHARS + 500)
    f = tmp_path / "mixed.txt"
    f.write_text(f"{short}\n{long_line}\n{short}\n", encoding="utf-8")

    result = read_file(str(f))
    # Short lines intact
    assert f"1|{short}" in result
    assert f"3|{short}" in result
    # Long line truncated to _MAX_LINE_DISPLAY_CHARS + marker
    assert "x" * _MAX_LINE_DISPLAY_CHARS + "... [truncated]" in result
    # Content beyond the limit is NOT present
    assert "x" * (_MAX_LINE_DISPLAY_CHARS + 1) not in result
    # Summary footer reports original length
    assert f"line 2 ({len(long_line)} chars)" in result
    assert "Use offset=2, limit=1 with a sufficient max_tokens budget" in result


def test_read_file_line_exactly_at_limit(tmp_path: Path) -> None:
    """A line with exactly _MAX_LINE_DISPLAY_CHARS is NOT truncated."""
    exact = "a" * _MAX_LINE_DISPLAY_CHARS
    f = tmp_path / "exact.txt"
    f.write_text(exact + "\n", encoding="utf-8")

    result = read_file(str(f))
    assert f"1|{exact}" in result
    assert "[truncated]" not in result
    assert "[Long lines" not in result


def test_read_file_multiple_long_lines(tmp_path: Path) -> None:
    """Multiple long lines are all truncated and listed in the summary."""
    content_lines = []
    for i in range(5):
        if i % 2 == 0:
            content_lines.append("normal")
        else:
            content_lines.append("y" * (_MAX_LINE_DISPLAY_CHARS + 100 + i * 100))
    f = tmp_path / "multi.txt"
    f.write_text("\n".join(content_lines) + "\n", encoding="utf-8")

    result = read_file(str(f))
    # Lines 2 and 4 (1-indexed) should be truncated
    assert f"line 2 ({_MAX_LINE_DISPLAY_CHARS + 200} chars)" in result
    assert f"line 4 ({_MAX_LINE_DISPLAY_CHARS + 400} chars)" in result
    assert result.count("... [truncated]") == 2
    # Normal lines are intact
    assert "1|normal" in result
    assert "3|normal" in result
    assert "5|normal" in result


def test_read_file_pagination_bypasses_truncation(tmp_path: Path) -> None:
    """limit=1 skips the _MAX_LINE_DISPLAY_CHARS cap — full content returned."""
    long_line = "z" * 3000
    f = tmp_path / "long.txt"
    f.write_text(f"short\n{long_line}\nshort\n", encoding="utf-8")

    result = read_file(str(f), offset=2, limit=1)
    # Full line content should be present
    assert "z" * 3000 in result
    assert "[Long lines" not in result
    assert "[truncated]" not in result


def test_read_file_pagination_token_overflow_partial(tmp_path: Path) -> None:
    """limit=1 targets a huge line with small max_tokens — partial content, not empty."""
    long_line = "A" * 50_000
    f = tmp_path / "huge.txt"
    f.write_text(f"{long_line}\n", encoding="utf-8")

    result = read_file(str(f), max_tokens=100, offset=1, limit=1)
    # Should have SOME content (partial line), not just the header
    assert "1|" in result
    assert "AAAA" in result
    # Partial marker present
    assert "... [truncated]" in result
    # The token-budget notice reports the length without a second long-line notice.
    assert "Source line 1 has 50000 chars." in result
    assert "[Long lines" not in result
    assert result.count("\n[") == 1
    assert "[Truncated at line 1" in result


def test_read_file_token_overflow_after_char_truncation(tmp_path: Path) -> None:
    """Long line capped at _MAX_LINE_DISPLAY_CHARS still exceeds tiny token budget."""
    long_line = "B" * 5000
    f = tmp_path / "overflow.txt"
    f.write_text(f"{long_line}\n", encoding="utf-8")

    result = read_file(str(f), max_tokens=80)
    # Should have some partial content, not empty
    assert "1|" in result
    assert "... [truncated]" in result
    # Token truncation cuts before the character cap; report only its actual cause.
    assert "Source line 1 has 5000 chars." in result
    assert "[Long lines" not in result
    assert result.count("\n[") == 1


def test_read_file_all_lines_long(tmp_path: Path) -> None:
    """Every line in the file exceeds the limit — all truncated and listed."""
    content_lines = [chr(ord("a") + i) * (_MAX_LINE_DISPLAY_CHARS + 500 + i * 500) for i in range(4)]
    f = tmp_path / "alllong.txt"
    f.write_text("\n".join(content_lines) + "\n", encoding="utf-8")

    result = read_file(str(f))
    for i in range(4):
        expected_len = _MAX_LINE_DISPLAY_CHARS + 500 + i * 500
        assert f"line {i + 1} ({expected_len} chars)" in result
    assert result.count("... [truncated]") >= 4


def test_read_file_long_first_line_tiny_budget(tmp_path: Path) -> None:
    """First line overflows token budget — partial content instead of empty output."""
    long_line = "C" * 10_000
    f = tmp_path / "first.txt"
    f.write_text(f"{long_line}\nsecond\n", encoding="utf-8")

    result = read_file(str(f), max_tokens=50)
    # Should have partial content of line 1 (not dropped)
    assert "1|" in result
    assert "CCCC" in result
    assert "[Truncated" in result


def test_concurrent_same_file_edits_are_serialized(tmp_path: Path, monkeypatch) -> None:
    """Chrys runs sync tools via ``asyncio.to_thread``, so two same-file edits
    in one tool batch can run concurrently in worker threads. The per-path
    write lock must serialize the read-modify-write so neither edit's change is
    lost.
    """
    import threading
    import time
    from concurrent.futures import ThreadPoolExecutor

    import chrys.service.tools.builtins.filesystem as fs

    f = tmp_path / "doc.txt"
    f.write_text("alpha\nbeta\n", encoding="utf-8")

    active = 0
    max_active = 0
    counter_lock = threading.Lock()
    real_atomic_write = fs._atomic_write

    def _instrumented_atomic_write(*args: object, **kwargs: object) -> object:
        nonlocal active, max_active
        with counter_lock:
            active += 1
            max_active = max(max_active, active)
        try:
            time.sleep(0.05)  # widen the critical section to expose any overlap
            return real_atomic_write(*args, **kwargs)
        finally:
            with counter_lock:
                active -= 1

    monkeypatch.setattr(fs, "_atomic_write", _instrumented_atomic_write)

    with ThreadPoolExecutor(max_workers=2) as ex:
        fut_a = ex.submit(edit_file, str(f), old_string="alpha", new_string="ALPHA")
        fut_b = ex.submit(edit_file, str(f), old_string="beta", new_string="BETA")
        res_a = fut_a.result()
        res_b = fut_b.result()

    assert "Replaced 1 occurrence" in res_a
    assert "Replaced 1 occurrence" in res_b
    # The lock must have serialized the two same-file edits …
    assert max_active == 1
    # … so neither read-modify-write clobbered the other.
    assert f.read_text(encoding="utf-8") == "ALPHA\nBETA\n"


def test_atomic_write_retries_transient_windows_replace_lock(tmp_path: Path, monkeypatch) -> None:
    """Windows scanners may briefly lock the destination between serialized writes."""
    import chrys.service.tools.builtins.filesystem as fs

    target = tmp_path / "doc.txt"
    target.write_text("before", encoding="utf-8")
    monkeypatch.setattr(fs, "get_platform", Mock(return_value=Mock(is_windows=True)))
    rename = briefly_locked_rename(monkeypatch, failures=2)

    fs._atomic_write(str(target), "after")

    assert target.read_text(encoding="utf-8") == "after"
    assert len(rename.attempts) == 3
    assert rename.sleeps == [0.01, 0.02]


@pytest.mark.skipif(_WINDOWS, reason="POSIX fchmod behavior is not available on Windows")
def test_atomic_write_fchmod_failure_preserves_original(tmp_path: Path, monkeypatch) -> None:
    import chrys.service.tools.builtins.filesystem as fs

    target = tmp_path / "doc.txt"
    target.write_text("before", encoding="utf-8")
    target.chmod(0o640)
    monkeypatch.setattr(fs.os, "fchmod", Mock(side_effect=OSError("chmod failed")))

    with pytest.raises(OSError, match="chmod failed"):
        fs._atomic_write(str(target), "after")

    assert target.read_text(encoding="utf-8") == "before"
    assert stat.S_IMODE(target.stat().st_mode) == 0o640
    assert list(tmp_path.iterdir()) == [target]


@pytest.mark.skipif(_WINDOWS, reason="POSIX symlink replacement behavior is not available on Windows")
def test_write_file_replaces_looping_symlink_when_stat_fails(tmp_path: Path) -> None:
    target = tmp_path / "loop.txt"
    target.symlink_to(target.name)

    result = write_file(str(target), "replacement\n", overwrite=True)

    assert "Written" in result
    assert not target.is_symlink()
    assert target.read_text(encoding="utf-8") == "replacement\n"


def test_posix_write_mode_treats_permission_error_as_new_file(monkeypatch) -> None:
    import chrys.service.tools.builtins.filesystem as fs

    monkeypatch.setattr(fs.os, "stat", Mock(side_effect=PermissionError("target inaccessible")))

    assert fs._posix_write_mode("deploy.sh", "echo ok\n") == (0o755, False)


def test_atomic_write_skips_fchmod_on_windows(tmp_path: Path, monkeypatch) -> None:
    import chrys.service.tools.builtins.filesystem as fs

    target = tmp_path / "doc.txt"
    target.write_text("before", encoding="utf-8")
    fchmod = Mock(side_effect=AssertionError("fchmod must not be called on Windows"))
    monkeypatch.setattr(fs, "get_platform", Mock(return_value=Mock(is_windows=True)))
    monkeypatch.setattr(fs.os, "fchmod", fchmod, raising=False)

    fs._atomic_write(str(target), "after")

    assert target.read_text(encoding="utf-8") == "after"
    fchmod.assert_not_called()
