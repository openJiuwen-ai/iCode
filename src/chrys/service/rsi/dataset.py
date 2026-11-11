# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""RSI evaluation dataset format (chrys-rsi.dataset.v1, script_based only)."""

from __future__ import annotations

import json
import re
from collections.abc import Collection
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from chrys.service.rsi.errors import RsiBadRequest, RsiDatasetInvalid
from chrys.service.rsi.paths import is_forbidden_absolute_relpath, safe_relpath_under, validate_case_id
from chrys.service.rsi.workdir_seed import (
    WORKDIR_STRATEGIES,
    WorkdirSeed,
    WorkdirStrategy,
)

SCHEMA_VERSION = 1
METHOD_SCRIPT_BASED = "script_based"


@dataclass(slots=True, frozen=True)
class ScriptExpect:
    """Declarative checks against the agent response and case workdir."""

    response_contains: tuple[str, ...] = ()
    response_not_contains: tuple[str, ...] = ()
    response_regex: tuple[str, ...] = ()
    files_exist: tuple[str, ...] = ()
    file_contains: dict[str, tuple[str, ...]] = field(default_factory=dict)
    file_equals: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True, frozen=True)
class CaseFixture:
    """Optional stub inputs for ``StubCaseRunner`` (omitted in production datasets)."""

    response: str = ""
    files: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True, frozen=True)
class RsiCase:
    """One evaluation case."""

    case_id: str
    prompt: str
    expect: ScriptExpect
    script: str | None = None
    fixture: CaseFixture | None = None
    workdir: WorkdirSeed | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True, frozen=True)
class RsiDataset:
    """Loaded dataset with resolved source path."""

    dataset_id: str
    cases: tuple[RsiCase, ...]
    source_path: Path
    schema_version: int = SCHEMA_VERSION
    # Files / folders next to the dataset that check scripts read; staged with them.
    support_files: tuple[str, ...] = ()

    @property
    def sample_count(self) -> int:
        return len(self.cases)


def load_dataset(path: str | Path) -> RsiDataset:
    """Load and validate a dataset JSON file."""
    source = Path(path).expanduser()
    if not source.is_file():
        raise RsiDatasetInvalid(f"dataset not found: {source}")
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RsiDatasetInvalid(f"dataset is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise RsiDatasetInvalid("dataset root must be an object")
    return parse_dataset(payload, source_path=source.resolve())


def parse_dataset(payload: dict[str, Any], *, source_path: Path) -> RsiDataset:
    """Validate an in-memory dataset object."""
    version = payload.get("schema_version", SCHEMA_VERSION)
    try:
        version_i = int(version)
    except (TypeError, ValueError) as exc:
        raise RsiDatasetInvalid("schema_version must be an integer") from exc
    if version_i != SCHEMA_VERSION:
        raise RsiDatasetInvalid(f"unsupported schema_version: {version_i} (expected {SCHEMA_VERSION})")

    dataset_id = str(payload.get("dataset_id") or "").strip()
    if not dataset_id:
        raise RsiDatasetInvalid("dataset_id is required")

    cases_raw = payload.get("cases")
    if not isinstance(cases_raw, list) or not cases_raw:
        raise RsiDatasetInvalid("cases must be a non-empty array")

    cases: list[RsiCase] = []
    seen: set[str] = set()
    seen_casefold: set[str] = set()
    for index, item in enumerate(cases_raw, start=1):
        if not isinstance(item, dict):
            raise RsiDatasetInvalid(f"cases[{index}] must be an object")
        case = _parse_case(item, index=index, dataset_dir=source_path.parent)
        if case.case_id in seen:
            raise RsiDatasetInvalid(f"duplicate case_id: {case.case_id}")
        folded = case.case_id.casefold()
        if folded in seen_casefold:
            raise RsiDatasetInvalid(f"duplicate case_id under case-insensitive filesystems: {case.case_id!r}")
        seen.add(case.case_id)
        seen_casefold.add(folded)
        cases.append(case)

    return RsiDataset(
        dataset_id=dataset_id,
        cases=tuple(cases),
        source_path=source_path,
        schema_version=version_i,
        support_files=_parse_support_files(payload.get("support_files"), dataset_dir=source_path.parent),
    )


def subset_dataset(dataset: RsiDataset, case_ids: Collection[str]) -> RsiDataset:
    """Return a dataset containing only *case_ids*, in the original order."""
    wanted = {str(case_id) for case_id in case_ids}
    if not wanted:
        raise RsiBadRequest("case_ids must not be empty")
    selected = tuple(case for case in dataset.cases if case.case_id in wanted)
    missing = wanted - {case.case_id for case in selected}
    if missing:
        names = ", ".join(sorted(missing))
        raise RsiBadRequest(f"unknown case_ids: {names}")
    return RsiDataset(
        dataset_id=dataset.dataset_id,
        cases=selected,
        source_path=dataset.source_path,
        schema_version=dataset.schema_version,
        support_files=dataset.support_files,
    )


def validate_dataset_file(path: str | Path) -> dict[str, Any]:
    """Return a validation projection for CLI / API callers."""
    try:
        dataset = load_dataset(path)
    except RsiDatasetInvalid as exc:
        return {
            "valid": False,
            "sample_count": None,
            "errors": [{"code": exc.code, "reason": exc.message}],
        }
    return {
        "valid": True,
        "sample_count": dataset.sample_count,
        "dataset_id": dataset.dataset_id,
        "schema_version": dataset.schema_version,
        "errors": [],
    }


def _parse_case(item: dict[str, Any], *, index: int, dataset_dir: Path) -> RsiCase:
    try:
        case_id = validate_case_id(str(item.get("case_id") or ""))
    except ValueError as exc:
        raise RsiDatasetInvalid(f"cases[{index}] has invalid case_id: {exc}") from exc
    prompt = str(item.get("prompt") or "")
    if not prompt.strip():
        raise RsiDatasetInvalid(f"case {case_id}: prompt is required")

    evaluation = item.get("evaluation")
    if not isinstance(evaluation, dict):
        raise RsiDatasetInvalid(f"case {case_id}: evaluation object is required")
    method = str(evaluation.get("method") or "").strip()
    if method != METHOD_SCRIPT_BASED:
        raise RsiDatasetInvalid(
            f"case {case_id}: only evaluation.method={METHOD_SCRIPT_BASED!r} is supported in Phase 2"
        )

    expect_raw = evaluation.get("expect")
    if expect_raw is None:
        expect_raw = {}
    if not isinstance(expect_raw, dict):
        raise RsiDatasetInvalid(f"case {case_id}: evaluation.expect must be an object")
    expect = _parse_expect(expect_raw, case_id=case_id)

    script = evaluation.get("script")
    script_rel: str | None = None
    if script is not None:
        script_rel = str(script).strip()
        if not script_rel:
            raise RsiDatasetInvalid(f"case {case_id}: evaluation.script must not be empty")
        _ensure_script_inside_dataset(dataset_dir, script_rel, case_id=case_id)

    has_expect = bool(
        expect.response_contains
        or expect.response_not_contains
        or expect.response_regex
        or expect.files_exist
        or expect.file_contains
        or expect.file_equals
    )
    if not has_expect and not script_rel:
        raise RsiDatasetInvalid(f"case {case_id}: evaluation needs expect checks and/or a script")

    fixture = _parse_fixture(item.get("fixture"), case_id=case_id)
    workdir = _parse_workdir(item.get("workdir"), case_id=case_id)
    return RsiCase(
        case_id=case_id,
        prompt=prompt,
        expect=expect,
        script=script_rel,
        fixture=fixture,
        workdir=workdir,
        raw=dict(item),
    )


def _parse_expect(raw: dict[str, Any], *, case_id: str) -> ScriptExpect:
    response_regex = _string_tuple(raw.get("response_regex"), field="response_regex", case_id=case_id)
    for pattern in response_regex:
        try:
            re.compile(pattern)
        except re.error as exc:
            raise RsiDatasetInvalid(f"case {case_id}: response_regex {pattern!r} is not a valid regex: {exc}") from exc
    return ScriptExpect(
        response_contains=_string_tuple(raw.get("response_contains"), field="response_contains", case_id=case_id),
        response_not_contains=_string_tuple(
            raw.get("response_not_contains"), field="response_not_contains", case_id=case_id
        ),
        response_regex=response_regex,
        files_exist=_string_tuple(raw.get("files_exist"), field="files_exist", case_id=case_id),
        file_contains=_string_map_tuple(raw.get("file_contains"), field="file_contains", case_id=case_id),
        file_equals=_string_map(raw.get("file_equals"), field="file_equals", case_id=case_id),
    )


def _parse_support_files(raw: Any, *, dataset_dir: Path) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise RsiDatasetInvalid("support_files must be an array of relative paths")
    out: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            raise RsiDatasetInvalid("support_files entries must be strings")
        cleaned = item.strip().replace("\\", "/").rstrip("/")
        if (
            not cleaned
            or is_forbidden_absolute_relpath(cleaned)
            or any(part in {"", ".", ".."} for part in cleaned.split("/"))
        ):
            raise RsiDatasetInvalid(
                f"support_files entry must name a file or folder inside the dataset folder: {item!r}"
            )
        try:
            path = safe_relpath_under(dataset_dir, cleaned, field="support_files")
        except ValueError as exc:
            raise RsiDatasetInvalid(f"support_files entry escapes the dataset folder: {item!r}") from exc
        if not path.exists():
            raise RsiDatasetInvalid(f"support_files entry not found: {item!r}")
        if cleaned in out:
            raise RsiDatasetInvalid(f"duplicate support_files entry: {item!r}")
        out.append(cleaned)
    return tuple(out)


def _parse_fixture(raw: Any, *, case_id: str) -> CaseFixture | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise RsiDatasetInvalid(f"case {case_id}: fixture must be an object")
    files_raw = raw.get("files") or {}
    if not isinstance(files_raw, dict):
        raise RsiDatasetInvalid(f"case {case_id}: fixture.files must be an object")
    files: dict[str, str] = {}
    for key, value in files_raw.items():
        try:
            rel = _validate_workdir_relpath(str(key), field="fixture.files", case_id=case_id)
        except RsiDatasetInvalid:
            raise
        files[rel] = str(value)
    return CaseFixture(response=str(raw.get("response") or ""), files=files)


def _parse_workdir(raw: Any, *, case_id: str) -> WorkdirSeed | None:
    """Parse optional ``workdir`` seed (source tree materialized into the case cwd)."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise RsiDatasetInvalid(f"case {case_id}: workdir must be an object")
    source = str(raw.get("from") or "").strip()
    if not source:
        raise RsiDatasetInvalid(f"case {case_id}: workdir.from is required")
    strategy_raw = raw.get("strategy", "copy")
    strategy = str(strategy_raw or "copy").strip()
    if strategy not in WORKDIR_STRATEGIES:
        allowed = ", ".join(sorted(WORKDIR_STRATEGIES))
        raise RsiDatasetInvalid(f"case {case_id}: workdir.strategy must be one of: {allowed}")
    ref_raw = raw.get("ref")
    ref: str | None
    if ref_raw is None:
        ref = None
    else:
        ref = str(ref_raw).strip()
        if not ref:
            raise RsiDatasetInvalid(f"case {case_id}: workdir.ref must not be empty when set")
        if strategy == "copy":
            raise RsiDatasetInvalid(f"case {case_id}: workdir.ref requires strategy git_clone or git_worktree")
    return WorkdirSeed(source=source, strategy=cast(WorkdirStrategy, strategy), ref=ref)


def _ensure_script_inside_dataset(dataset_dir: Path, script_rel: str, *, case_id: str) -> None:
    try:
        candidate = safe_relpath_under(dataset_dir, script_rel, field="evaluation.script")
    except ValueError as exc:
        raise RsiDatasetInvalid(f"case {case_id}: script escapes dataset directory") from exc
    if not candidate.is_file():
        raise RsiDatasetInvalid(f"case {case_id}: script not found: {script_rel}")


def _validate_workdir_relpath(rel: str, *, field: str, case_id: str) -> str:
    cleaned = str(rel).strip().replace("\\", "/")
    if not cleaned or is_forbidden_absolute_relpath(cleaned) or ".." in Path(cleaned).parts:
        raise RsiDatasetInvalid(f"case {case_id}: {field} path is invalid: {rel!r}")
    if any(part in {"", ".", ".."} for part in Path(cleaned).parts):
        raise RsiDatasetInvalid(f"case {case_id}: {field} path is invalid: {rel!r}")
    return cleaned


def _string_tuple(value: Any, *, field: str, case_id: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise RsiDatasetInvalid(f"case {case_id}: {field} must be an array of strings")
    out: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise RsiDatasetInvalid(f"case {case_id}: {field} entries must be strings")
        if not item:
            # "" is in every response and matches everywhere, so the check could never fail (or never pass).
            raise RsiDatasetInvalid(f"case {case_id}: {field} entries must not be empty")
        entry = _validate_workdir_relpath(item, field=field, case_id=case_id) if field == "files_exist" else item
        if field == "files_exist" and entry in out:
            raise RsiDatasetInvalid(f"case {case_id}: {field} lists {item!r} twice (after '\\' → '/')")
        out.append(entry)
    return tuple(out)


def _string_map(value: Any, *, field: str, case_id: str) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise RsiDatasetInvalid(f"case {case_id}: {field} must be an object")
    out: dict[str, str] = {}
    for key, item in value.items():
        rel = _validate_workdir_relpath(str(key), field=field, case_id=case_id)
        if not isinstance(item, str):
            raise RsiDatasetInvalid(f"case {case_id}: {field} values must be strings")
        if rel in out:
            raise RsiDatasetInvalid(f"case {case_id}: {field} lists {key!r} twice (after '\\' → '/')")
        out[rel] = item
    return out


def _string_map_tuple(value: Any, *, field: str, case_id: str) -> dict[str, tuple[str, ...]]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise RsiDatasetInvalid(f"case {case_id}: {field} must be an object")
    out: dict[str, tuple[str, ...]] = {}
    for key, item in value.items():
        rel = _validate_workdir_relpath(str(key), field=field, case_id=case_id)
        if rel in out:
            raise RsiDatasetInvalid(f"case {case_id}: {field} lists {key!r} twice (after '\\' → '/')")
        out[rel] = _string_tuple(item, field=f"{field}[{key}]", case_id=case_id)
    return out
