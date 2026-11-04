# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for session-local tool-result spills."""

from __future__ import annotations

import asyncio
import stat
import threading
from pathlib import Path
from threading import Event as ThreadEvent

import pytest

from chrys.foundation.platform import get_platform
from chrys.foundation.platform.files import secure_open_owner_only_binary
from chrys.foundation.platform.output_capture import CapturedOutput, capture_limit_footer
from chrys.foundation.text.tokenizer import MixedLanguageTokenizer
from chrys.kernel.tools import SyncToolCancelledAfterCompletion
from chrys.service.tools import spill
from chrys.service.tools.spill import TOOL_RESULTS_DIR_NAME, bound_process_output, format_spill_notice, try_spill_text

_tokenizer = MixedLanguageTokenizer()


def test_spill_creates_directory_lazily_and_round_trips_surrogates(tmp_path: Path) -> None:
    text = "full output\nraw byte: \udcff"
    info = try_spill_text(tmp_path, "shell", text, 1_000)

    assert info is not None
    assert info.path.parent == tmp_path / TOOL_RESULTS_DIR_NAME
    assert info.path.name.startswith("shell_")
    assert info.path.read_text(encoding="utf-8", errors="surrogateescape") == text
    assert str(info.path) in info.notice
    with secure_open_owner_only_binary(info.path) as source:
        assert source.read().decode("utf-8", errors="surrogateescape") == text
    if not get_platform().is_windows:
        assert stat.S_IMODE(info.path.parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(info.path.stat().st_mode) == 0o600


def test_the_spill_notice_names_a_folder_that_is_not_utf8_escaped_for_the_model() -> None:
    # A POSIX session folder whose name is not UTF-8 holds a lone surrogate; sent
    # raw, it would fail the model request's strict encode.
    path = Path("/sessions/bad\udcff/tool_results/shell_12345678.txt")

    notice = format_spill_notice(path, "output")

    assert str(path).replace("\udcff", "\\udcff") in notice
    notice.encode("utf-8")


def test_spill_tightens_existing_directory_mode(tmp_path: Path) -> None:
    directory = tmp_path / TOOL_RESULTS_DIR_NAME
    directory.mkdir(mode=0o755)

    info = try_spill_text(tmp_path, "shell", "sensitive", 1_000)

    assert info is not None
    if not get_platform().is_windows:
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700


@pytest.mark.skipif(get_platform().is_windows, reason="Creating symlinks is not generally available on Windows")
def test_spill_canonicalizes_session_directory_with_symlink_ancestor(tmp_path: Path) -> None:
    real_root = tmp_path / "real"
    real_root.mkdir()
    alias_root = tmp_path / "alias"
    alias_root.symlink_to(real_root, target_is_directory=True)
    aliased_session = alias_root / "sessions" / "demo"
    aliased_session.mkdir(parents=True)

    info = try_spill_text(aliased_session, "shell", "sensitive output", 1_000)

    assert info is not None
    expected_dir = real_root.resolve() / "sessions" / "demo" / TOOL_RESULTS_DIR_NAME
    assert info.path.parent == expected_dir
    assert str(info.path) in info.notice
    assert str(alias_root) not in info.notice
    assert stat.S_IMODE(expected_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(info.path.stat().st_mode) == 0o600


@pytest.mark.skipif(get_platform().is_windows, reason="Creating symlinks is not generally available on Windows")
def test_spill_rejects_symlinked_owned_results_directory(tmp_path: Path) -> None:
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    redirected = tmp_path / "redirected"
    redirected.mkdir()
    (session_dir / TOOL_RESULTS_DIR_NAME).symlink_to(redirected, target_is_directory=True)

    info = try_spill_text(session_dir, "shell", "sensitive output", 1_000)

    assert info is None
    assert list(redirected.iterdir()) == []


async def test_spill_finalizer_carries_completed_result_through_cancellation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = ThreadEvent()
    release = ThreadEvent()
    real_write = spill.atomic_write_owner_only_bytes

    def blocking_write(path: Path, payload: bytes) -> None:
        started.set()
        assert release.wait(timeout=5)
        real_write(path, payload)

    monkeypatch.setattr(spill, "atomic_write_owner_only_bytes", blocking_write)

    async def capture_completed() -> str:
        try:
            return await bound_process_output(tmp_path, "shell", "x" * 10_000, 100)
        except SyncToolCancelledAfterCompletion as exc:
            return exc.completed_result

    task = asyncio.create_task(capture_completed())
    assert await asyncio.to_thread(started.wait, 2)
    task.cancel()
    release.set()

    result = await task

    assert "truncated" in result
    assert "Full output saved to:" in result
    assert list((tmp_path / TOOL_RESULTS_DIR_NAME).glob("shell_*.txt"))


def test_spills_get_unique_names(tmp_path: Path) -> None:
    first = try_spill_text(tmp_path, "mcp", "x" * 1_000, 1_000)
    second = try_spill_text(tmp_path, "mcp", "x" * 1_000, 1_000)

    assert first is not None
    assert second is not None
    assert first.path != second.path


def test_notice_that_cannot_fit_does_not_write_or_create_directory(tmp_path: Path) -> None:
    info = try_spill_text(tmp_path, "skill", "x" * 10_000, 1)

    assert info is None
    assert not (tmp_path / TOOL_RESULTS_DIR_NAME).exists()


def test_reserved_footer_is_included_in_single_fit_check(tmp_path: Path, monkeypatch) -> None:
    class _Uuid:
        hex = "12345678deadbeef"

    def _uuid4() -> _Uuid:
        return _Uuid()

    monkeypatch.setattr(spill, "uuid4", _uuid4)
    text = "x" * 10_000
    path = tmp_path / TOOL_RESULTS_DIR_NAME / "mcp_12345678.txt"
    marker = spill._spill_fit_marker(text)
    notice = format_spill_notice(path, text)
    without_reserved = _tokenizer.count_tokens(f"{marker}\n{notice}")
    reserved = "中" * 20
    assert _tokenizer.count_tokens(f"{marker}\n{reserved}\n{notice}") > without_reserved

    info = try_spill_text(tmp_path, "mcp", text, without_reserved, reserved_footer=reserved)

    assert info is None
    assert not path.exists()


async def test_a_lead_opens_the_bounded_result_and_stays_out_of_the_spill(tmp_path: Path) -> None:
    lead = "Error: command timed out after 7 seconds.\n"
    text = "\n".join(f"line {i} " + "x" * 60 for i in range(400))
    notice = format_spill_notice(tmp_path.resolve() / TOOL_RESULTS_DIR_NAME / "shell_12345678.txt", text)
    # Room for little more than the lead and the spill notice: a bound that
    # kept the start of the lead and text together would lose the lead.
    budget = _tokenizer.count_tokens(lead) + _tokenizer.count_tokens(f"{spill._spill_fit_marker(text)}\n{notice}") + 4

    result = await bound_process_output(tmp_path, "shell", text, budget, lead=lead)

    assert result.startswith(lead)
    assert "Full output saved to:" in result
    assert _tokenizer.count_tokens(result) <= budget
    [spilled] = (tmp_path / TOOL_RESULTS_DIR_NAME).glob("shell_*.txt")
    assert spilled.read_text(encoding="utf-8") == text


# 230: room is left for only part of a truncation marker beside the lead and
# the note; 300: the lead and the note alone fill the budget.
@pytest.mark.parametrize("name_length", [230, 300])
async def test_a_budget_too_small_for_the_lead_and_the_capture_note_keeps_the_error_line(
    tmp_path: Path, name_length: int
) -> None:
    lead = "Error: Script '" + "x" * name_length + ".py' timed out after 30s.\n[partial output]\n"
    text = "\n".join(f"line {i}" for i in range(400))
    captures = (CapturedOutput(b"a", b"b", 100_000),)

    result = await bound_process_output(tmp_path, "skill", text, 100, captures, lead=lead)

    assert result.startswith("Error: Script 'xxx")
    assert "timed out after 30s." in result
    assert "[partial output]" not in result
    assert result.endswith(f"\n{capture_limit_footer(captures)}")
    assert _tokenizer.count_tokens(result) <= 100


async def test_a_text_too_long_to_fit_is_bounded_off_the_event_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    loop_thread = threading.get_ident()
    counted_on_loop: list[int] = []
    truncate_output = spill.truncate_output

    def recording_truncate_output(
        text: str, max_tokens: int, *, head_ratio: float = 1 / 3, truncation_suffix: str = ""
    ) -> str:
        if threading.get_ident() == loop_thread:
            counted_on_loop.append(len(text))
        return truncate_output(text, max_tokens, head_ratio=head_ratio, truncation_suffix=truncation_suffix)

    monkeypatch.setattr(spill, "truncate_output", recording_truncate_output)

    result = await bound_process_output(tmp_path, "shell", "x" * 1_000, 100)

    assert counted_on_loop == []
    assert _tokenizer.count_tokens(result) <= 100
