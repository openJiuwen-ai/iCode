# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for runtime tool-result metadata recording helpers."""

from __future__ import annotations

import json
import math
import sys
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import pytest

from chrys.foundation.platform import script_commands
from chrys.foundation.tool_result_metadata import (
    PROCESS_EXIT_CODE_METADATA_KEY,
    PROCESS_TIMEOUT_SECONDS_METADATA_KEY,
    TOOL_ERROR_DETAILS_METADATA_KEY,
    TOOL_ERROR_KIND_METADATA_KEY,
    TOOL_ERROR_MESSAGE_METADATA_KEY,
    TOOL_FAILED_METADATA_KEY,
)
from chrys.kernel import AgentSession
from chrys.service.context.compaction import UnifiedContextStrategy
from chrys.service.context.providers.context_management import ContextManagementProvider
from chrys.service.profiles.models.resolver import default_profile
from chrys.service.skills.model import Skill, SkillScript
from chrys.service.skills.provider import ChrysSkillsProvider
from chrys.service.skills.runner import SubprocessScriptRunner
from chrys.service.tools.builtins import search as search_module
from chrys.service.tools.builtins.doc_converter import DocConverterTools
from chrys.service.tools.builtins.filesystem import _read_file_impl
from chrys.service.tools.builtins.sleep import sleep
from chrys.service.tools.result_metadata import (
    record_process_result,
    record_process_timeout,
    record_tool_success,
    tool_error,
    tool_result_metadata,
)


def test_tool_error_records_failure_metadata_and_returns_error_text() -> None:
    metadata: dict[str, object] = {}
    token = tool_result_metadata.set(metadata)
    try:
        text = tool_error("validation", "bad input", details={"path": Path("a.txt"), 3: {b"bytes"}})
    finally:
        tool_result_metadata.reset(token)

    assert text == "Error: bad input"
    assert metadata[TOOL_FAILED_METADATA_KEY] is True
    assert metadata[TOOL_ERROR_KIND_METADATA_KEY] == "validation"
    assert metadata[TOOL_ERROR_MESSAGE_METADATA_KEY] == "bad input"
    json.dumps(metadata[TOOL_ERROR_DETAILS_METADATA_KEY])
    json.dumps(metadata[TOOL_ERROR_DETAILS_METADATA_KEY], allow_nan=False)


def test_tool_error_preserves_existing_error_prefix_but_stores_unprefixed_message() -> None:
    metadata: dict[str, object] = {}
    token = tool_result_metadata.set(metadata)
    try:
        text = tool_error("not_found", "Error: missing")
    finally:
        tool_result_metadata.reset(token)

    assert text == "Error: missing"
    assert metadata[TOOL_ERROR_MESSAGE_METADATA_KEY] == "missing"


def test_tool_error_neutralizes_surrogates_in_text_and_display_metadata() -> None:
    metadata: dict[str, object] = {}
    token = tool_result_metadata.set(metadata)
    try:
        text = tool_error("not_found", "missing — /tmp/report-\udcff.md")
    finally:
        tool_result_metadata.reset(token)

    text.encode("utf-8")
    assert text == r"Error: missing — /tmp/report-\udcff.md"
    assert metadata[TOOL_ERROR_MESSAGE_METADATA_KEY] == r"missing — /tmp/report-\udcff.md"


def test_record_process_result_records_meaningful_false_success() -> None:
    metadata: dict[str, object] = {}
    token = tool_result_metadata.set(metadata)
    try:
        record_process_result(0)
    finally:
        tool_result_metadata.reset(token)

    assert metadata[PROCESS_EXIT_CODE_METADATA_KEY] == 0
    assert metadata[TOOL_FAILED_METADATA_KEY] is False


def test_record_tool_success_records_meaningful_false_success() -> None:
    metadata: dict[str, object] = {}
    token = tool_result_metadata.set(metadata)
    try:
        record_tool_success()
    finally:
        tool_result_metadata.reset(token)

    assert metadata == {TOOL_FAILED_METADATA_KEY: False}


def test_record_tool_success_does_not_clear_existing_failure() -> None:
    metadata: dict[str, object] = {TOOL_FAILED_METADATA_KEY: True}
    token = tool_result_metadata.set(metadata)
    try:
        record_tool_success()
    finally:
        tool_result_metadata.reset(token)

    assert metadata[TOOL_FAILED_METADATA_KEY] is True


def test_record_process_result_zero_does_not_clear_existing_failure() -> None:
    metadata: dict[str, object] = {TOOL_FAILED_METADATA_KEY: True}
    token = tool_result_metadata.set(metadata)
    try:
        record_process_result(0)
    finally:
        tool_result_metadata.reset(token)

    assert metadata[PROCESS_EXIT_CODE_METADATA_KEY] == 0
    assert metadata[TOOL_FAILED_METADATA_KEY] is True


def test_tool_error_sanitizes_nonfinite_floats() -> None:
    metadata: dict[str, object] = {}
    token = tool_result_metadata.set(metadata)
    try:
        tool_error("validation", "bad input", details={"value": math.nan})
    finally:
        tool_result_metadata.reset(token)

    assert metadata[TOOL_ERROR_DETAILS_METADATA_KEY] == {"value": "nan"}
    json.dumps(metadata[TOOL_ERROR_DETAILS_METADATA_KEY], allow_nan=False)


def test_tool_error_sanitizes_sets_in_stable_order() -> None:
    metadata: dict[str, object] = {}
    token = tool_result_metadata.set(metadata)
    try:
        tool_error("validation", "bad input", details={"values": {"b", "a", "c"}})
    finally:
        tool_result_metadata.reset(token)

    assert metadata[TOOL_ERROR_DETAILS_METADATA_KEY] == {"values": ["a", "b", "c"]}


def test_record_process_timeout_sanitizes_nonfinite_timeout() -> None:
    metadata: dict[str, object] = {}
    token = tool_result_metadata.set(metadata)
    try:
        record_process_timeout(math.inf)
    finally:
        tool_result_metadata.reset(token)

    assert metadata[PROCESS_TIMEOUT_SECONDS_METADATA_KEY] == "inf"
    json.dumps(metadata, allow_nan=False)


def _capture_tool_metadata() -> tuple[dict[str, object], object]:
    metadata: dict[str, object] = {}
    return metadata, tool_result_metadata.set(metadata)


def test_filesystem_read_file_records_production_failure_metadata(tmp_path: Path) -> None:
    metadata, token = _capture_tool_metadata()
    try:
        result = _read_file_impl("missing.txt", base_cwd=str(tmp_path))
    finally:
        tool_result_metadata.reset(token)

    assert result.startswith("Error: file not found")
    assert metadata[TOOL_FAILED_METADATA_KEY] is True
    assert metadata[TOOL_ERROR_KIND_METADATA_KEY] == "file_not_found"


async def test_search_process_failure_records_production_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_run_rg(
        args: list[str],
        *,
        timeout: int = 30,
        cwd: str | None = None,
        consume: Callable[[bytes], bool] | None = None,
    ) -> tuple[str, str, int]:
        return "", "regex parse error", 2

    monkeypatch.setattr(search_module, "_run_rg", fake_run_rg)
    metadata, token = _capture_tool_metadata()
    try:
        result = await search_module._grep_impl("[", path=str(tmp_path))
    finally:
        tool_result_metadata.reset(token)

    assert result == "Error: regex parse error"
    assert metadata[TOOL_FAILED_METADATA_KEY] is True
    assert metadata[TOOL_ERROR_KIND_METADATA_KEY] == "search_process_failed"
    assert metadata[PROCESS_EXIT_CODE_METADATA_KEY] == 2


async def test_sleep_validation_records_production_failure_metadata() -> None:
    metadata, token = _capture_tool_metadata()
    try:
        result = await sleep(-1)
    finally:
        tool_result_metadata.reset(token)

    assert result == "Error: sleep duration must be 0 seconds or greater."
    assert metadata[TOOL_FAILED_METADATA_KEY] is True
    assert metadata[TOOL_ERROR_KIND_METADATA_KEY] == "invalid_sleep_duration"


async def test_doc_converter_missing_file_records_production_failure_metadata(tmp_path: Path) -> None:
    runtime = SimpleNamespace(cwd=str(tmp_path), platform=SimpleNamespace(config_dir=tmp_path))
    tools = DocConverterTools(runtime)
    metadata, token = _capture_tool_metadata()
    try:
        result = await tools.convert_document("missing.pdf")
    finally:
        tool_result_metadata.reset(token)

    assert result.startswith("Error: file not found")
    assert metadata[TOOL_FAILED_METADATA_KEY] is True
    assert metadata[TOOL_ERROR_KIND_METADATA_KEY] == "file_not_found"


async def test_skill_provider_records_production_failure_metadata() -> None:
    async def load_skills() -> list[Skill]:
        return []

    provider = ChrysSkillsProvider(load_skills)
    metadata, token = _capture_tool_metadata()
    try:
        result = await provider._load_skill([], "missing")
    finally:
        tool_result_metadata.reset(token)

    assert result == "Error: Skill 'missing' not found."
    assert metadata[TOOL_FAILED_METADATA_KEY] is True
    assert metadata[TOOL_ERROR_KIND_METADATA_KEY] == "skill_not_found"


async def test_skill_script_runner_records_production_process_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(script_commands, "_find_python_runner", lambda: [sys.executable])
    script_path = tmp_path / "fail.py"
    script_path.write_text("import sys\nprint('before exit')\nsys.exit(7)\n", encoding="utf-8")
    skill = Skill(
        name="scripted",
        description="",
        content="",
        path=str(tmp_path),
        scripts=[SkillScript(name="fail.py", full_path=str(script_path))],
    )
    script = skill.scripts[0]
    runner = SubprocessScriptRunner(timeout=30)
    metadata, token = _capture_tool_metadata()
    try:
        result = await runner(skill, script)
    finally:
        tool_result_metadata.reset(token)

    assert "before exit" in result
    assert "[exit_code: 7]" in result
    assert metadata[PROCESS_EXIT_CODE_METADATA_KEY] == 7
    assert metadata[TOOL_FAILED_METADATA_KEY] is True


async def test_context_management_records_production_failure_metadata() -> None:
    provider = ContextManagementProvider(default_profile(), strategy=UnifiedContextStrategy())
    provider._session = AgentSession()
    metadata, token = _capture_tool_metadata()
    try:
        result = await provider._recall_context("what happened?", compressed_context_id="ctx_missing")
    finally:
        tool_result_metadata.reset(token)

    assert result.startswith("Error: compressed_context_id 'ctx_missing' not found")
    assert metadata[TOOL_FAILED_METADATA_KEY] is True
    assert metadata[TOOL_ERROR_KIND_METADATA_KEY] == "compressed_context_not_found"
