# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Case runners: stub fixtures and profile-aware stub for gated optimize.

Live headless runs live in ``chrys.app.cli.rsi_headless`` so the service tier
does not import orchestration.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from chrys.service.profiles.agents.schema import AgentProfile
from chrys.service.rsi.dataset import RsiCase
from chrys.service.rsi.mutations import case_marker
from chrys.service.rsi.paths import safe_relpath_under


@dataclass(slots=True, frozen=True)
class CaseRunResult:
    """Outcome of executing one case prompt in an isolated workdir."""

    case_id: str
    response_text: str
    workdir: Path
    error: str | None = None
    session_id: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


class CaseRunner(Protocol):
    """Runs one case prompt against a subject under test."""

    async def run_case(self, case: RsiCase, workdir: Path) -> CaseRunResult: ...


def case_sessions_dir(workdir: Path) -> Path:
    """Session store for a live case run: beside *workdir*, outside the agent's cwd.

    Case ids start with a letter or digit, so the leading dot never names another case.
    """
    return workdir.parent / f".{workdir.name}.sessions"


class StubCaseRunner:
    """Materialize optional ``fixture`` data without calling a model."""

    async def run_case(self, case: RsiCase, workdir: Path) -> CaseRunResult:
        workdir.mkdir(parents=True, exist_ok=True)
        fixture = case.fixture
        if fixture is None:
            return CaseRunResult(
                case_id=case.case_id,
                response_text="",
                workdir=workdir,
                error="stub runner requires case.fixture (use headless runner for live agents)",
            )
        for rel, content in fixture.files.items():
            try:
                target = safe_relpath_under(workdir, rel, field="fixture.files")
            except ValueError as exc:
                return CaseRunResult(
                    case_id=case.case_id,
                    response_text="",
                    workdir=workdir,
                    error=f"fixture path escapes workdir: {exc}",
                )
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        return CaseRunResult(
            case_id=case.case_id,
            response_text=fixture.response,
            workdir=workdir,
        )


class ProfileAwareStubRunner:
    """Stub runner that passes a case when the profile carries ``rsi:case:<id>``.

    Used by Phase 3 optimize tests so mutation markers improve the gate score
    without calling a model. Without a marker, falls back to the case fixture,
    or answers nothing (a judged failure, not a run error) when there is none.
    """

    def __init__(self, profile: AgentProfile) -> None:
        self.profile = profile
        self._fallback = StubCaseRunner()

    async def run_case(self, case: RsiCase, workdir: Path) -> CaseRunResult:
        workdir.mkdir(parents=True, exist_ok=True)
        instructions = self.profile.instructions or ""
        skill_blob = _inline_skill_text(self.profile)
        if case_marker(case.case_id) in instructions or case_marker(case.case_id) in skill_blob:
            return _synthesize_passing_case(case, workdir)
        if case.fixture is None:
            return CaseRunResult(case_id=case.case_id, response_text="", workdir=workdir)
        return await self._fallback.run_case(case, workdir)


def _inline_skill_text(profile: AgentProfile) -> str:
    skills = profile.skills
    if skills is None:
        return ""
    parts: list[str] = []
    for skill in skills.inline:
        parts.append(skill.instructions or "")
        parts.append(skill.description or "")
    return "\n".join(parts)


def _synthesize_passing_case(case: RsiCase, workdir: Path) -> CaseRunResult:
    """Write files / response that satisfy declarative expect checks."""
    expect = case.expect
    try:
        for rel, content in expect.file_equals.items():
            target = safe_relpath_under(workdir, rel, field="file_equals")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        for rel, needles in expect.file_contains.items():
            if rel in expect.file_equals:
                continue
            target = safe_relpath_under(workdir, rel, field="file_contains")
            target.parent.mkdir(parents=True, exist_ok=True)
            body = "\n".join(needles) if needles else ""
            target.write_text(body + ("\n" if body and not body.endswith("\n") else ""), encoding="utf-8")
        for rel in expect.files_exist:
            target = safe_relpath_under(workdir, rel, field="files_exist")
            if target.is_file():
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("", encoding="utf-8")
    except ValueError as exc:
        return CaseRunResult(
            case_id=case.case_id,
            response_text="",
            workdir=workdir,
            error=f"expect path escapes workdir: {exc}",
        )
    response_parts = list(expect.response_contains) or ["ok"]
    return CaseRunResult(
        case_id=case.case_id,
        response_text=" ".join(response_parts),
        workdir=workdir,
    )
