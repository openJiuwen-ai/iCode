# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Declarative + optional trusted-script judging for script_based cases."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from chrys.foundation.platform.process import windows_hidden_subprocess_kwargs
from chrys.service.rsi.dataset import RsiCase, ScriptExpect
from chrys.service.rsi.errors import RsiDatasetInvalid
from chrys.service.rsi.paths import safe_relpath_under

# Trusted check scripts may build toolchains (e.g. cargo); keep parity with the
# default per-case headless timeout rather than failing healthy cold builds.
_SCRIPT_TIMEOUT_SECONDS = 300


@dataclass(slots=True, frozen=True)
class JudgeFinding:
    """One failed check."""

    code: str
    message: str


@dataclass(slots=True, frozen=True)
class JudgeResult:
    """Pass/fail for one case."""

    passed: bool
    score: float
    findings: tuple[JudgeFinding, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "passed": self.passed,
            "score": self.score,
            "findings": [{"code": item.code, "message": item.message} for item in self.findings],
        }


def judge_case(
    case: RsiCase,
    *,
    response_text: str,
    workdir: Path,
    dataset_dir: Path,
) -> JudgeResult:
    """Evaluate declarative expects and an optional trusted check script."""
    findings: list[JudgeFinding] = []
    findings.extend(_judge_expect(case.expect, response_text=response_text, workdir=workdir))
    if case.script:
        findings.extend(_run_check_script(case, response_text=response_text, workdir=workdir, dataset_dir=dataset_dir))
    passed = not findings
    return JudgeResult(passed=passed, score=1.0 if passed else 0.0, findings=tuple(findings))


def _judge_expect(expect: ScriptExpect, *, response_text: str, workdir: Path) -> list[JudgeFinding]:
    findings: list[JudgeFinding] = [
        JudgeFinding("response_contains", f"response missing substring: {needle!r}")
        for needle in expect.response_contains
        if needle not in response_text
    ]
    findings.extend(
        JudgeFinding("response_not_contains", f"response must not contain: {needle!r}")
        for needle in expect.response_not_contains
        if needle in response_text
    )
    for pattern in expect.response_regex:
        try:
            compiled = re.compile(pattern)
        except re.error as exc:
            findings.append(JudgeFinding("response_regex", f"invalid regex {pattern!r}: {exc}"))
            continue
        if compiled.search(response_text) is None:
            findings.append(JudgeFinding("response_regex", f"response did not match regex: {pattern!r}"))

    root = workdir.resolve()
    for rel in expect.files_exist:
        path = _safe_under(root, rel)
        if path is None or not path.is_file():
            findings.append(JudgeFinding("files_exist", f"missing file: {rel}"))

    for rel, needles in expect.file_contains.items():
        path = _safe_under(root, rel)
        if path is None or not path.is_file():
            findings.append(JudgeFinding("file_contains", f"missing file: {rel}"))
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            findings.append(JudgeFinding("file_contains", f"cannot read {rel}: {exc}"))
            continue
        findings.extend(
            JudgeFinding("file_contains", f"{rel} missing substring: {needle!r}")
            for needle in needles
            if needle not in text
        )

    for rel, expected in expect.file_equals.items():
        path = _safe_under(root, rel)
        if path is None or not path.is_file():
            findings.append(JudgeFinding("file_equals", f"missing file: {rel}"))
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            findings.append(JudgeFinding("file_equals", f"cannot read {rel}: {exc}"))
            continue
        if text != expected:
            findings.append(JudgeFinding("file_equals", f"{rel} content mismatch"))
    return findings


def _run_check_script(
    case: RsiCase,
    *,
    response_text: str,
    workdir: Path,
    dataset_dir: Path,
) -> list[JudgeFinding]:
    script_rel = case.script
    if not script_rel:
        return []
    root = dataset_dir.resolve()
    try:
        script_path = safe_relpath_under(root, script_rel, field="evaluation.script")
    except ValueError:
        return [JudgeFinding("script", "script path escaped dataset directory")]
    if not script_path.is_file():
        return [JudgeFinding("script", f"script not found: {script_rel}")]

    response_file = workdir / ".chrys_rsi_response.txt"
    try:
        response_file.write_text(response_text, encoding="utf-8")
    except OSError as exc:
        return [JudgeFinding("script", f"could not write response file: {exc}")]

    env = {
        **os.environ,
        "CHRYS_RSI_CASE_ID": case.case_id,
        "CHRYS_RSI_WORKDIR": str(workdir.resolve()),
        "CHRYS_RSI_RESPONSE_PATH": str(response_file.resolve()),
        "CHRYS_RSI_DATASET_DIR": str(root),
    }
    popen_kwargs: dict[str, object] = {}
    if sys.platform != "win32":
        popen_kwargs["start_new_session"] = True
    try:
        completed = subprocess.run(  # noqa: S603 - trusted dataset-local script, absolute interpreter
            [sys.executable, str(script_path)],
            cwd=str(workdir.resolve()),
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_SCRIPT_TIMEOUT_SECONDS,
            check=False,
            **popen_kwargs,
            **windows_hidden_subprocess_kwargs(),
        )
    except subprocess.TimeoutExpired:
        return [JudgeFinding("script", f"script timed out after {_SCRIPT_TIMEOUT_SECONDS}s")]
    except OSError as exc:
        return [JudgeFinding("script", f"failed to start script: {exc}")]

    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip() or f"exit {completed.returncode}"
        return [JudgeFinding("script", f"script failed: {detail}")]
    return []


def _safe_under(root: Path, rel: str) -> Path | None:
    try:
        return safe_relpath_under(root, rel, field="path")
    except ValueError:
        return None


def ensure_case_script_valid(case: RsiCase, dataset_dir: Path) -> None:
    """Re-check script containment (for callers that build cases manually)."""
    if not case.script:
        return
    root = dataset_dir.resolve()
    try:
        candidate = safe_relpath_under(root, case.script, field="evaluation.script")
    except ValueError as exc:
        raise RsiDatasetInvalid(f"case {case.case_id}: script escapes dataset directory") from exc
    if not candidate.is_file():
        raise RsiDatasetInvalid(f"case {case.case_id}: script not found: {case.script}")
