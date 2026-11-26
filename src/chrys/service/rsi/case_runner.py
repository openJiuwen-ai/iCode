# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Case runners: stub fixtures and profile-aware stub for gated optimize.

Live headless runs live in ``chrys.app.cli.rsi_headless`` so the service tier
does not import orchestration.
"""

from __future__ import annotations

import itertools
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from chrys.service.profiles.agents.schema import AgentProfile
from chrys.service.rsi.dataset import RsiCase, ScriptExpect
from chrys.service.rsi.mutations import case_marker
from chrys.service.rsi.paths import safe_relpath_under
from chrys.service.rsi.script_judge import response_findings

CaseErrorCause = Literal["environment", "timeout", "agent"]
"""What ended a case run with an error.

``environment``: something the agent does not control (the model service, the
network, the case setup). ``timeout``: the case's time limit, which a slow
environment and a stuck agent both reach. ``agent``: the agent's own run, e.g.
its conversation outgrew the model's context window.
"""


@dataclass(slots=True, frozen=True)
class CaseRunResult:
    """Outcome of executing one case prompt in an isolated workdir."""

    case_id: str
    response_text: str
    workdir: Path
    error: str | None = None
    session_id: str | None = None
    error_cause: CaseErrorCause = "environment"
    """Meaningful only when ``error`` is set."""

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
    """Write files / response that satisfy declarative expect checks.

    The response is chosen by ``_synthesize_response``: the legacy construction
    (``response_contains``, else ``ok``, plus every regex literal that does not
    match yet) is preferred, so a case that already passed keeps byte-identical
    text.  When that text trips ``response_not_contains`` or a later append
    invalidates an earlier match, orderings of the contains/regex segments are
    validated against the full response checks; if none passes, the legacy text
    is returned unchanged.
    """
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
    response_text = _synthesize_response(expect)
    return CaseRunResult(
        case_id=case.case_id,
        response_text=response_text,
        workdir=workdir,
    )


def _response_satisfies(expect: ScriptExpect, text: str) -> bool:
    """True when ``text`` passes every declarative response check."""
    return not response_findings(expect, response_text=text)


def _synthesize_response(expect: ScriptExpect) -> str:
    """Pick a response text that satisfies the declarative response checks.

    The legacy construction is tried first so previously passing cases keep their
    exact output.  Two regressions it had are worked around by trying other
    orderings after that: the ``ok`` fallback can itself contain a forbidden
    substring, and appending a literal can invalidate a regex that matched
    earlier text.  Segment orderings are tried last because a literal that one
    regex needs can break another (e.g. a negative lookahead before a digit).
    """
    contains = list(expect.response_contains)
    patterns = list(expect.response_regex)

    legacy = " ".join(contains) or "ok"
    for pattern in patterns:
        if re.search(pattern, legacy) is None:
            legacy = f"{legacy} {pattern}"

    candidates = [legacy]
    if patterns:
        candidates.append(" ".join(patterns))
        candidates.append(" ".join(reversed(patterns)))
    if contains:
        candidates.append(" ".join(contains))
        if patterns:
            candidates.append(" ".join([*contains, *patterns]))
            candidates.append(" ".join([*patterns, *contains]))
    else:
        candidates.append("")
    candidates.extend(_segment_orderings([*contains, *patterns]))

    for candidate in candidates:
        if _response_satisfies(expect, candidate):
            return candidate
    return legacy


def _segment_orderings(segments: list[str]) -> list[str]:
    """Return the joined orderings of ``segments``, or two heuristics when long.

    Order matters because a literal appended for one regex can invalidate
    another: a negative lookahead followed by a digit literal only passes when
    the digit comes first.  The caller re-validates every ordering against all
    response checks, so only orders that actually satisfy the case are kept.
    """
    unique = list(dict.fromkeys(segment for segment in segments if segment))
    if not unique:
        return []
    if len(unique) > 6:
        return [" ".join(unique), " ".join(reversed(unique))]
    return [" ".join(order) for order in itertools.permutations(unique)]
