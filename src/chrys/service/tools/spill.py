# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Session-local spill files for truncated tool output."""

from __future__ import annotations

import asyncio
import logging
import os
import stat
from collections.abc import Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from chrys.foundation.platform import get_platform
from chrys.foundation.platform.files import (
    atomic_write_owner_only_bytes,
    secure_unlink_owner_verified,
    surrogate_safe_text,
)
from chrys.foundation.platform.output_capture import CapturedOutput, capture_limit_footer
from chrys.foundation.text.tokenizer import MixedLanguageTokenizer
from chrys.foundation.text.tool_output import truncate_output
from chrys.foundation.tool_result_metadata import (
    record_payload_truncation,
    result_text_exit_code,
    result_text_without_exit_code,
)
from chrys.kernel.tools import SyncToolCancelledAfterCompletion

logger = logging.getLogger(__name__)

TOOL_RESULTS_DIR_NAME = "tool_results"

_tokenizer = MixedLanguageTokenizer()

_MARKER_TOKENS = 20
"""Room for a truncation marker, whose counts stay under nine digits for any output a capture keeps."""


@dataclass(frozen=True, slots=True)
class SpillInfo:
    """A successfully written spill and its model-facing notice."""

    path: Path
    notice: str


def discard_spill(info: SpillInfo) -> None:
    """Best-effort cleanup when a defensive truncation path loses its notice."""
    if info.path.exists() and not secure_unlink_owner_verified(info.path):
        logger.warning("Failed to discard unreferenced tool-output spill %s", info.path)


def _ensure_owner_only_directory(path: Path) -> None:
    """Create the spill directory privately and tighten existing POSIX modes."""
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if get_platform().is_windows:
        return

    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        info = os.fstat(fd)
        if not stat.S_ISDIR(info.st_mode):
            raise OSError("Tool-result spill path is not a directory.")
        os.fchmod(fd, 0o700)
    finally:
        os.close(fd)


def format_spill_notice(path: Path, text: str, *, kept_only: bool = False) -> str:
    """Format the bounded-result notice for a spill at *path*.

    *kept_only* says the spill holds only what a bounded capture kept. The notice
    goes to the model, so a lone surrogate in *path* (a POSIX folder name that is
    not UTF-8) is escaped; the file itself stays at the raw path.
    """
    line_count = text.count("\n") + 1
    token_count = _tokenizer.count_tokens(text)
    saved = "Kept output" if kept_only else "Full output"
    return (
        f"[{saved} saved to: {surrogate_safe_text(str(path))}\n"
        f"{line_count} lines, ~{token_count} tokens. Use read_file or shell tools to inspect it.]"
    )


def _spill_fit_marker(text: str) -> str:
    total_tokens = _tokenizer.count_tokens(text)
    line_marker = f"[...truncated {len(text.splitlines())} lines (estimated {total_tokens} tokens total)...]"
    char_marker = f"[...truncated ~{len(text)} chars (estimated {total_tokens} tokens total)...]"
    return max((line_marker, char_marker), key=_tokenizer.count_tokens)


def try_spill_text(
    dir_path: Path | None,
    prefix: str,
    text: str,
    budget: int,
    reserved_footer: str = "",
    *,
    kept_only: bool = False,
) -> SpillInfo | None:
    """Write *text* under the session if its complete notice can fit."""
    if dir_path is None:
        record_payload_truncation(text)
        return None

    try:
        canonical_dir = dir_path.resolve(strict=False)
    except OSError, RuntimeError:
        logger.warning("Failed to resolve tool-output spill directory %s", dir_path, exc_info=True)
        record_payload_truncation(text)
        return None

    # Canonicalize only the trusted session directory.  The owned child path
    # stays lexical so the secure writer can still reject a planted
    # ``tool_results`` symlink/reparse point instead of following it.
    path = canonical_dir / TOOL_RESULTS_DIR_NAME / f"{prefix}_{uuid4().hex[:8]}.txt"
    notice = format_spill_notice(path, text, kept_only=kept_only)
    footer = "\n".join(part for part in (_spill_fit_marker(text), reserved_footer, notice) if part)
    if _tokenizer.count_tokens(footer) > budget:
        record_payload_truncation(text)
        return None

    try:
        _ensure_owner_only_directory(path.parent)
        atomic_write_owner_only_bytes(path, text.encode("utf-8", errors="surrogateescape"))
    except OSError:
        logger.warning("Failed to spill truncated tool output to %s", path, exc_info=True)
        with suppress(OSError):
            secure_unlink_owner_verified(path)
        record_payload_truncation(text)
        return None
    record_payload_truncation(text, artifact_name=path.name)
    return SpillInfo(path=path, notice=notice)


def _spill_and_truncate_text(
    dir_path: Path | None, prefix: str, lead: str, text: str, budget: int, capture_footer: str
) -> str:
    body, footer = text, ""
    if capture_footer:
        # The capture's note goes right before a trailing exit code, which
        # readers take from the very end of the result; a later bound that
        # keeps the end of the result keeps both.
        footer = f"\n{capture_footer}"
        exit_code = result_text_exit_code(text)
        if exit_code is not None:
            body = result_text_without_exit_code(text)
            footer += f"\n[exit_code: {exit_code}]"
        whole = lead + body + footer
        if truncate_output(whole, budget) == whole:
            return whole
    # Token counts are floored per text, so two texts can count one more
    # together than apart.
    reserved = sum(_tokenizer.count_tokens(part) + 1 for part in (lead, footer) if part)
    if reserved + _MARKER_TOKENS > budget:
        # No room for the output beside the error line and the note: keep the
        # error line alone (without one, the output), cut in the middle if
        # need be, then the note if it still fits.
        room = budget - _tokenizer.count_tokens(footer) - 1
        if not footer or room < 1:
            footer, room = "", budget
        return truncate_output(lead.partition("\n")[0] or body, room, head_ratio=1 / 2) + footer
    budget -= reserved
    info = try_spill_text(dir_path, prefix, text, budget, kept_only=bool(footer))
    bounded = truncate_output(body, budget, truncation_suffix=info.notice if info else "")
    if info is not None and info.notice not in bounded:
        discard_spill(info)
        bounded = truncate_output(body, budget)
    return lead + bounded + footer


async def bound_result_text(text: str, budget: int, spill_dir: Path | None, prefix: str) -> str:
    """Bound *text* to *budget* tokens, saving the whole text under *spill_dir* when its notice fits."""
    plain = truncate_output(text, budget)
    if plain == text:
        return text
    return await run_spill_finalizer(_spill_and_bound_result_text, text, budget, spill_dir, prefix, plain)


def _spill_and_bound_result_text(text: str, budget: int, spill_dir: Path | None, prefix: str, plain: str) -> str:
    info = try_spill_text(spill_dir, prefix, text, budget)
    bounded = truncate_output(text, budget, truncation_suffix=info.notice if info else "")
    if info is not None and info.notice not in bounded:
        discard_spill(info)
        return plain
    return bounded


async def run_spill_finalizer[T](function: Callable[..., T], /, *args: Any) -> T:
    """Drain one threaded spill finalizer and carry its result through cancellation."""
    worker = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        while not worker.done():
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                continue
        try:
            completed = worker.result()
        except Exception as exc:
            logger.warning("Tool-result spill finalizer failed while its caller was cancelled", exc_info=True)
            raise asyncio.CancelledError from exc
        raise SyncToolCancelledAfterCompletion(completed) from None


async def bound_process_output(
    dir_path: Path | None,
    prefix: str,
    text: str,
    budget: int,
    captures: Sequence[CapturedOutput] = (),
    *,
    lead: str = "",
) -> str:
    """Bound a finished process's result to *budget*, spilling it when it does not fit.

    When *captures* dropped output, the trajectory records how many bytes the
    process wrote and a note saying what was not kept comes last, or just
    before a trailing exit code. *lead* (an error line, perhaps with a label
    under it) opens the result whatever the bound keeps of *text*; when the
    budget leaves *text* no room beside the lead and that note, only the
    lead's first line is kept, cut short if need be. A spill holds only
    *text*. The spill and the bounded view survive cancellation of the caller.
    """
    footer = capture_limit_footer(captures)
    whole = lead + text
    # Every character counts at least a quarter token, so a longer text cannot
    # fit, and counting it here would hold the event loop.
    if not footer and len(whole) <= 4 * (budget + 1) and truncate_output(whole, budget) == whole:
        return whole
    record_payload_truncation(whole, original_bytes=sum(capture.seen for capture in captures) if footer else None)
    return await run_spill_finalizer(_spill_and_truncate_text, dir_path, prefix, lead, text, budget, footer)
