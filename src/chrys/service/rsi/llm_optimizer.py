# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""LLM mutation proposer for RSI Phase 3.

The model may only emit the whitelist in :mod:`chrys.service.rsi.mutations`.
A call failure or an unparsable proposal is marked dropped so the optimize
loop can try the next epoch. The score gate itself is unchanged.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from chrys.foundation.platform.files import surrogate_safe_text
from chrys.kernel import Message
from chrys.service.llm.clients import scoped_client
from chrys.service.llm.one_shot import get_final_response
from chrys.service.profiles.agents.schema import AgentProfile
from chrys.service.profiles.models.options import effective_chat_options
from chrys.service.profiles.models.schema import ModelProfile
from chrys.service.rsi.errors import RsiBadRequest
from chrys.service.rsi.evaluator import CaseEvalRecord
from chrys.service.rsi.failure_summary import FailureSummary, summarize_failures
from chrys.service.rsi.mutations import (
    AppendInstructions,
    Mutation,
    RemoveInlineSkill,
    UpsertInlineSkill,
    parse_mutation,
)
from chrys.service.rsi.optimizer import MutationProposal, ProposalOutcome
from chrys.service.rsi.paths import write_json_atomic

logger = logging.getLogger(__name__)

_MAX_MUTATIONS = 4
_MAX_FIELD_CHARS = 4_000
# Appended instructions land at the end, so the excerpt keeps both ends.
_EXCERPT_HEAD = 3_000
_EXCERPT_TAIL = 1_000
_HISTORY_FIELD_CHARS = 600
_MAX_ADOPTED_SHOWN = 10
_MAX_REJECTED_SHOWN = 6

_CONTRACT = """\
You propose edits to an agent profile that failed a fixed script_based evaluation.
Reply with one JSON object and no markdown:
{"mutations": [ ... ]}
Allowed mutation objects:
- {"kind": "append_instructions", "text": "..."}  — append only; never replace existing instructions
- {"kind": "upsert_inline_skill", "name": "...", "description": "...", "instructions": "..."}
- {"kind": "remove_inline_skill", "name": "..."}
Inline skill names are 1-64 characters: lowercase letters, digits, and single hyphens.
Do not change approval, tools, MCP, or identity.
At most 4 mutations. If you cannot propose a safe change, reply {"mutations": []}.
adopted_mutations are already part of the profile: do not propose them again.
rejected_proposals were tried earlier in this run and did not raise the score
(verdict "inconclusive": some case could not run, so they were not judged): try something different.
"""

CompleteFn = Callable[[str], Awaitable[str]]


class LlmOptimizer:
    """Ask one model profile for whitelist mutations from failure summaries."""

    def __init__(
        self,
        complete: CompleteFn,
        *,
        analysis_dir: Path | None = None,
    ) -> None:
        self._complete = complete
        self._analysis_dir = Path(analysis_dir) if analysis_dir is not None else None

    def redirect_analysis(self, directory: Path) -> None:
        """Point analysis JSON at a run-scoped directory (avoids epoch overwrite)."""
        self._analysis_dir = Path(directory)

    async def propose(
        self,
        profile: AgentProfile,
        failed: list[CaseEvalRecord],
        *,
        instruction: str | None,
        epoch: int,
        history: tuple[ProposalOutcome, ...],
    ) -> MutationProposal:
        summaries = summarize_failures(failed)
        if not summaries:
            return MutationProposal.none()
        self._write_analysis(epoch, summaries, proposal="pending", mutations=())
        prompt = render_optimizer_prompt(profile, summaries, instruction=instruction, epoch=epoch, history=history)
        try:
            raw = await self._complete(surrogate_safe_text(prompt))
        except Exception as exc:
            logger.warning("RSI optimizer model call failed (%s); skipping this epoch", type(exc).__name__)
            self._write_analysis(epoch, summaries, proposal="model_call_failed", mutations=())
            return MutationProposal.drop()
        try:
            mutations = parse_optimizer_mutations(raw)
        except RsiBadRequest as exc:
            logger.warning("RSI optimizer proposal rejected: %s", exc.message)
            self._write_analysis(epoch, summaries, proposal="invalid_proposal", mutations=())
            return MutationProposal.drop()
        self._write_analysis(epoch, summaries, proposal="accepted", mutations=tuple(mutations))
        if not mutations:
            return MutationProposal.none()
        return MutationProposal.ready(mutations)

    def _write_analysis(
        self,
        epoch: int,
        summaries: tuple[FailureSummary, ...],
        *,
        proposal: str,
        mutations: tuple[Mutation, ...],
    ) -> None:
        if self._analysis_dir is None:
            return
        write_json_atomic(
            self._analysis_dir / f"epoch-{epoch}.json",
            {
                "epoch": epoch,
                "proposal": proposal,
                "failures": [item.to_dict() for item in summaries],
                "mutations": [item.to_dict() for item in mutations],
            },
        )


def render_optimizer_prompt(
    profile: AgentProfile,
    summaries: tuple[FailureSummary, ...],
    *,
    instruction: str | None,
    epoch: int,
    history: tuple[ProposalOutcome, ...] = (),
) -> str:
    """Build the user message: profile excerpt, this run's earlier proposals, clipped failures."""
    skills = []
    if profile.skills is not None:
        skills = [skill.name for skill in profile.skills.inline]
    adopted = [
        _clip_mutation(item) for outcome in history if outcome.verdict == "adopted" for item in outcome.mutations
    ]
    rejected = [
        {
            "epoch": outcome.epoch,
            "verdict": outcome.verdict,
            "score": outcome.score,
            "mutations": [_clip_mutation(item) for item in outcome.mutations],
        }
        for outcome in history
        if outcome.verdict != "adopted"
    ]
    body = {
        "epoch": epoch,
        "optimization_instruction": instruction or "",
        "profile_name": profile.name,
        "instructions_excerpt": _instructions_excerpt(profile.instructions or ""),
        "inline_skills": skills,
        "adopted_mutations": adopted[-_MAX_ADOPTED_SHOWN:],
        "rejected_proposals": rejected[-_MAX_REJECTED_SHOWN:],
        "failures": [item.to_dict() for item in summaries],
    }
    encoded = json.dumps(body, ensure_ascii=True, indent=2)
    return f"{_CONTRACT}\nFailed cases:\n{encoded}\n"


def _instructions_excerpt(text: str) -> str:
    if len(text) <= _EXCERPT_HEAD + _EXCERPT_TAIL:
        return text
    omitted = len(text) - _EXCERPT_HEAD - _EXCERPT_TAIL
    return f"{text[:_EXCERPT_HEAD]}\n[... {omitted} characters omitted ...]\n{text[-_EXCERPT_TAIL:]}"


def _clip_mutation(mutation: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value[:_HISTORY_FIELD_CHARS] + "..."
        if isinstance(value, str) and len(value) > _HISTORY_FIELD_CHARS
        else value
        for key, value in mutation.items()
    }


def parse_optimizer_mutations(text: str) -> list[Mutation]:
    """Parse a model reply into whitelist mutations.

    Invalid JSON, unknown kinds, oversize fields, or too many items raise
    :class:`RsiBadRequest`. An empty ``mutations`` array is valid.
    """
    payload = _load_json_object(text)
    raw_list = payload.get("mutations")
    if not isinstance(raw_list, list):
        raise RsiBadRequest("optimizer JSON must contain a mutations array")
    if len(raw_list) > _MAX_MUTATIONS:
        raise RsiBadRequest("optimizer proposed too many mutations")
    mutations: list[Mutation] = []
    for item in raw_list:
        if not isinstance(item, dict):
            raise RsiBadRequest("optimizer mutation must be an object")
        mutation = parse_mutation(item)
        _check_size(mutation)
        mutations.append(mutation)
    return mutations


async def complete_with_model_profile(profile: ModelProfile, prompt: str) -> str:
    """One non-streaming completion on *profile*. The caller owns no client."""
    async with scoped_client(profile) as client:
        response = await get_final_response(
            client,
            [Message("user", [prompt])],
            stream=False,
            options=effective_chat_options(profile),
        )
    return response.text


def _load_json_object(text: str) -> dict[str, Any]:
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError:
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start < 0 or end <= start:
            raise RsiBadRequest("optimizer proposal was not valid JSON") from None
        try:
            payload = json.loads(stripped[start : end + 1])
        except json.JSONDecodeError:
            raise RsiBadRequest("optimizer proposal was not valid JSON") from None
    if not isinstance(payload, dict):
        raise RsiBadRequest("optimizer proposal must be a JSON object")
    return payload


def _check_size(mutation: Mutation) -> None:
    if isinstance(mutation, AppendInstructions) and len(mutation.text) > _MAX_FIELD_CHARS:
        raise RsiBadRequest("append_instructions.text is too long")
    if isinstance(mutation, UpsertInlineSkill) and (
        len(mutation.description) > _MAX_FIELD_CHARS or len(mutation.instructions) > _MAX_FIELD_CHARS
    ):
        raise RsiBadRequest("upsert_inline_skill fields are too long")
    if isinstance(mutation, RemoveInlineSkill) and len(mutation.name) > 64:
        raise RsiBadRequest("remove_inline_skill.name is too long")
