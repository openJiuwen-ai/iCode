# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Phase 3 LLM proposer: failure summaries, whitelist parse, gated adopt/reject."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from chrys.service.profiles.agents.schema import AgentProfile
from chrys.service.rsi import build_rsi_service_context
from chrys.service.rsi.evaluator import CaseEvalRecord
from chrys.service.rsi.failure_summary import summarize_failures
from chrys.service.rsi.llm_optimizer import LlmOptimizer, parse_optimizer_mutations
from chrys.service.rsi.mutations import AppendInstructions, case_marker
from chrys.service.rsi.optimizer import MutationProposal

_OPTIMIZE_FIXTURE = Path(__file__).parent / "fixtures" / "optimize_dataset.json"


def test_summarize_failures_clips_response_and_skips_passes() -> None:
    long = "x" * 5_000
    summaries = summarize_failures(
        [
            CaseEvalRecord(
                case_id="ok",
                passed=True,
                score=1.0,
                response_text="fine",
                findings=(),
                error=None,
                workdir="/tmp/ok",
                session_id=None,
                prompt="ignored",
            ),
            CaseEvalRecord(
                case_id="need-hello",
                passed=False,
                score=0.0,
                response_text=long,
                findings=({"code": "files_exist", "message": "missing file: hello.txt"},),
                error=None,
                workdir="/tmp/fail",
                session_id=None,
                prompt="Write hello.txt containing hello world",
            ),
        ]
    )
    assert len(summaries) == 1
    assert summaries[0].case_id == "need-hello"
    assert summaries[0].prompt.startswith("Write hello.txt")
    assert len(summaries[0].response_excerpt) < len(long)
    assert summaries[0].response_excerpt.endswith("[…truncated]")
    assert summaries[0].findings[0]["code"] == "files_exist"


def test_parse_optimizer_mutations_accepts_fenced_json_and_rejects_unknown_kind() -> None:
    parsed = parse_optimizer_mutations(
        '```json\n{"mutations": [{"kind": "append_instructions", "text": "be brief"}]}\n```'
    )
    assert len(parsed) == 1
    assert isinstance(parsed[0], AppendInstructions)
    with pytest.raises(Exception, match="unsupported mutation"):
        parse_optimizer_mutations('{"mutations": [{"kind": "rewrite_system_prompt", "text": "x"}]}')
    with pytest.raises(Exception, match="not valid JSON"):
        parse_optimizer_mutations("not json")
    assert parse_optimizer_mutations('{"mutations": []}') == []


@pytest.mark.asyncio
async def test_llm_optimizer_adopts_marker_proposal_and_writes_analysis(tmp_path: Path) -> None:
    seen: list[str] = []

    async def complete(prompt: str) -> str:
        seen.append(prompt)
        return json.dumps(
            {
                "mutations": [
                    {
                        "kind": "append_instructions",
                        "text": f"{case_marker('need-hello')}\n{case_marker('need-ready')}",
                    }
                ]
            }
        )

    ctx = build_rsi_service_context(tasks_root=tmp_path / "tasks")
    created = ctx.create_task(name="llm", max_iterations=1, dataset_path=_OPTIMIZE_FIXTURE)
    task_id = created["task_id"]
    task = ctx.store.get(task_id)
    optimizer = LlmOptimizer(complete, analysis_dir=Path(task.run_dir) / "analysis")
    result = await ctx.optimize_task(
        task_id,
        base_profile=AgentProfile(name="RsiCandidate", instructions=""),
        max_epochs=1,
        optimizer=optimizer,
    )
    assert result.baseline_score == 0.0
    assert result.best_score == 1.0
    assert result.epochs[0].adopted is True
    assert seen and "Write hello.txt" in seen[0]
    assert "missing file: hello.txt" in seen[0]
    analysis = json.loads((Path(task.run_dir) / "analysis" / "epoch-1.json").read_text(encoding="utf-8"))
    assert analysis["proposal"] == "accepted"
    assert analysis["failures"][0]["case_id"] == "need-hello"

    tree = ctx.get_tree(task_id)
    adopted = [node for node in tree["nodes"] if node["type"] == "ADOPTED" and node["node_id"] != "ROOT"]
    assert any(node["description"] == "scored baseline" for node in adopted)
    assert any("append_instructions" in (node["description"] or "") for node in adopted)


@pytest.mark.asyncio
async def test_invalid_proposal_is_not_adopted(tmp_path: Path) -> None:
    async def complete(prompt: str) -> str:
        del prompt
        return "please rewrite the system prompt entirely"

    ctx = build_rsi_service_context(tasks_root=tmp_path / "tasks")
    created = ctx.create_task(name="bad", max_iterations=2, dataset_path=_OPTIMIZE_FIXTURE)
    task_id = created["task_id"]
    task = ctx.store.get(task_id)
    result = await ctx.optimize_task(
        task_id,
        max_epochs=2,
        optimizer=LlmOptimizer(complete, analysis_dir=Path(task.run_dir) / "analysis"),
    )
    assert result.best_score == result.baseline_score == 0.0
    assert [epoch.dropped for epoch in result.epochs] == [True, True]
    assert all(not epoch.adopted for epoch in result.epochs)
    analysis = json.loads((Path(task.run_dir) / "analysis" / "epoch-1.json").read_text(encoding="utf-8"))
    assert analysis["proposal"] == "invalid_proposal"
    later = json.loads((Path(task.run_dir) / "analysis" / "epoch-2.json").read_text(encoding="utf-8"))
    assert later["proposal"] == "invalid_proposal"
    descriptions = [node.get("description") for node in ctx.get_tree(task_id)["nodes"]]
    assert "scored baseline" in descriptions
    assert descriptions.count("proposal dropped") == 2
    assert all("append_instructions" not in (item or "") for item in descriptions)


@pytest.mark.asyncio
async def test_rejected_candidate_stays_off_the_best_pointer(tmp_path: Path) -> None:
    class _Scripted:
        async def propose(self, profile, failed, *, instruction, epoch):
            del profile, instruction
            if epoch == 1:
                return MutationProposal.ready([AppendInstructions(text="this does not satisfy the checks")])
            return MutationProposal.ready(
                [AppendInstructions(text="\n".join(case_marker(record.case_id) for record in failed))]
            )

    ctx = build_rsi_service_context(tasks_root=tmp_path / "tasks")
    created = ctx.create_task(name="gate", max_iterations=2, dataset_path=_OPTIMIZE_FIXTURE)
    result = await ctx.optimize_task(created["task_id"], max_epochs=2, optimizer=_Scripted())
    assert [epoch.adopted for epoch in result.epochs] == [False, True]
    nodes = ctx.get_tree(created["task_id"])["nodes"]
    assert any(node["type"] == "REJECTED" for node in nodes)
    assert any(node["type"] == "ADOPTED" and node["node_id"] != "ROOT" for node in nodes)


@pytest.mark.asyncio
async def test_dropped_proposal_retries_the_next_epoch(tmp_path: Path) -> None:
    calls = {"n": 0}

    async def complete(prompt: str) -> str:
        calls["n"] += 1
        if calls["n"] == 1:
            return "not json at all"
        return json.dumps(
            {
                "mutations": [
                    {
                        "kind": "append_instructions",
                        "text": f"{case_marker('need-hello')}\n{case_marker('need-ready')}",
                    }
                ]
            }
        )

    ctx = build_rsi_service_context(tasks_root=tmp_path / "tasks")
    created = ctx.create_task(name="retry", max_iterations=2, dataset_path=_OPTIMIZE_FIXTURE)
    result = await ctx.optimize_task(
        created["task_id"],
        max_epochs=2,
        optimizer=LlmOptimizer(complete),
    )
    assert [epoch.dropped for epoch in result.epochs] == [True, False]
    assert result.epochs[1].adopted is True
    assert result.best_score == 1.0
    assert calls["n"] == 2
