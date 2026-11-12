# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Compact failure summaries for the RSI optimizer (prompt, response, expect misses)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from chrys.service.rsi.evaluator import CaseEvalRecord

_PROMPT_LIMIT = 2_000
_RESPONSE_LIMIT = 2_000
_FINDING_LIMIT = 500
_MAX_FINDINGS = 12
_TRUNCATION_MARK = "\n[…truncated]"


@dataclass(frozen=True, slots=True)
class FailureSummary:
    """One failed case, clipped for an optimizer prompt."""

    case_id: str
    prompt: str
    response_excerpt: str
    findings: tuple[dict[str, str], ...]
    error: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "prompt": self.prompt,
            "response_excerpt": self.response_excerpt,
            "findings": [dict(item) for item in self.findings],
            "error": self.error,
        }


def summarize_failures(records: Sequence[CaseEvalRecord]) -> tuple[FailureSummary, ...]:
    """Return clipped summaries for records that did not pass.

    Full responses stay on the eval ``cases.jsonl``; this view is what the
    optimizer is allowed to read.
    """
    summaries: list[FailureSummary] = []
    for record in records:
        if record.passed:
            continue
        findings = tuple(
            {
                "code": str(item.get("code") or ""),
                "message": _clip(str(item.get("message") or ""), _FINDING_LIMIT),
            }
            for item in list(record.findings)[:_MAX_FINDINGS]
        )
        summaries.append(
            FailureSummary(
                case_id=record.case_id,
                prompt=_clip(record.prompt, _PROMPT_LIMIT),
                response_excerpt=_clip(record.response_text, _RESPONSE_LIMIT),
                findings=findings,
                error=_clip(record.error, _FINDING_LIMIT) if record.error else None,
            )
        )
    return tuple(summaries)


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    keep = max(0, limit - len(_TRUNCATION_MARK))
    return text[:keep] + _TRUNCATION_MARK
