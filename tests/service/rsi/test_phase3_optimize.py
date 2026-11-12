# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Phase 3 RSI profile/skills mutations, gated optimize, and install export."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from chrys.service.profiles.agents.schema import AgentProfile, SkillConfig, SkillsConfig
from chrys.service.rsi import build_rsi_service_context
from chrys.service.rsi.case_runner import ProfileAwareStubRunner
from chrys.service.rsi.mutations import (
    AppendInstructions,
    RemoveInlineSkill,
    UpsertInlineSkill,
    apply_mutations,
    case_marker,
    parse_mutation,
)
from chrys.service.rsi.snapshot import load_candidate, read_best_pointer

_OPTIMIZE_FIXTURE = Path(__file__).parent / "fixtures" / "optimize_dataset.json"


def test_apply_append_and_inline_skill_mutations() -> None:
    base = AgentProfile(name="RsiCandidate", instructions="base\n")
    mutated = apply_mutations(
        base,
        [
            AppendInstructions(text=case_marker("need-hello")),
            UpsertInlineSkill(
                name="rsi-fix-need-hello",
                description="fix",
                instructions=f"do it\n{case_marker('need-hello')}\n",
            ),
        ],
    )
    assert case_marker("need-hello") in (mutated.instructions or "")
    assert any(skill.name == "rsi-fix-need-hello" for skill in mutated.skills.inline)
    removed = apply_mutations(mutated, [RemoveInlineSkill(name="rsi-fix-need-hello")])
    assert all(skill.name != "rsi-fix-need-hello" for skill in removed.skills.inline)


def test_parse_mutation_rejects_unknown_kind() -> None:
    with pytest.raises(Exception, match="unsupported mutation"):
        parse_mutation({"kind": "rewrite_system_prompt", "text": "x"})


@pytest.mark.asyncio
async def test_optimize_stub_improves_and_install_exports(tmp_path: Path) -> None:
    ctx = build_rsi_service_context(tasks_root=tmp_path / "tasks")
    created = ctx.create_task(
        name="opt-demo",
        max_iterations=2,
        optimization_instruction="add skill markers",
        dataset_path=_OPTIMIZE_FIXTURE,
    )
    task_id = created["task_id"]

    result = await ctx.optimize_task(
        task_id,
        base_profile=AgentProfile(name="RsiCandidate", instructions=""),
        max_epochs=2,
    )
    assert result.baseline_score == 0.0
    assert result.best_score == 1.0
    assert result.perfect is True
    assert result.best_candidate_id != "baseline"
    assert len(result.epochs) >= 1

    pointer = read_best_pointer(ctx.store, task_id)
    snap = load_candidate(ctx.store, task_id, str(pointer["candidate_id"]))
    assert case_marker("need-hello") in (snap.profile.instructions or "")
    assert case_marker("need-ready") in (snap.profile.instructions or "")
    # instruction contained "skill" → stub also upserts an inline skill
    assert any(skill.name.startswith("rsi-fix-") for skill in snap.profile.skills.inline)

    tree = ctx.get_tree(task_id)
    by_description = {node.get("description"): node for node in tree["nodes"]}
    assert by_description["scored baseline"]["type"] == "ADOPTED"
    assert by_description["scored baseline"]["parent_id"] == "ROOT"
    epoch_nodes = [
        node
        for node in tree["nodes"]
        if isinstance(node.get("description"), str) and "append_instructions" in node["description"]
    ]
    assert epoch_nodes
    assert epoch_nodes[0]["type"] == "ADOPTED"
    assert epoch_nodes[0]["parent_id"] == by_description["scored baseline"]["node_id"]
    progress = ctx.get_task(task_id)["progress"]
    assert progress["baseline"] == 0.0
    assert progress["score"] == 1.0

    export_dir = tmp_path / "user_agents"
    summary = ctx.install_best(task_id)
    export_path = Path(summary["export_path"])
    assert export_path.is_file()
    assert "RsiCandidate" in export_path.name
    text = export_path.read_text(encoding="utf-8")
    assert case_marker("need-hello") in text

    # Explicit user-dir write is opt-in only.
    user_summary = ctx.install_best(
        task_id,
        user_agents_dir=export_dir,
        write_user=True,
    )
    user_path = Path(user_summary["export_path"])
    assert user_path.is_file()
    assert user_path.parent == export_dir.resolve()


@pytest.mark.asyncio
async def test_install_refuses_user_without_dir(tmp_path: Path) -> None:
    ctx = build_rsi_service_context(tasks_root=tmp_path / "tasks")
    created = ctx.create_task(name="opt", max_iterations=1, dataset_path=_OPTIMIZE_FIXTURE)
    await ctx.optimize_task(created["task_id"], max_epochs=1)
    with pytest.raises(Exception, match="user_agents_dir"):
        ctx.install_best(created["task_id"], write_user=True)


def test_mutations_preserve_existing_inline_skills() -> None:
    base = AgentProfile(
        name="RsiCandidate",
        skills=SkillsConfig(
            inline=[SkillConfig(name="keep", description="d", instructions="i")],
        ),
    )
    mutated = apply_mutations(
        base,
        [UpsertInlineSkill(name="added", description="x", instructions="y")],
    )
    names = {skill.name for skill in mutated.skills.inline}
    assert names == {"keep", "added"}


@pytest.mark.asyncio
async def test_optimize_reruns_only_failing_cases(tmp_path: Path) -> None:
    dataset = tmp_path / "suite.json"
    dataset.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "dataset_id": "partial",
                "cases": [
                    {
                        "case_id": "already-ok",
                        "prompt": "Say ready",
                        "evaluation": {"method": "script_based", "expect": {"response_contains": ["ready"]}},
                        "fixture": {"response": "ready"},
                    },
                    {
                        "case_id": "need-hello",
                        "prompt": "Write hello.txt",
                        "evaluation": {
                            "method": "script_based",
                            "expect": {"response_contains": ["hello"], "files_exist": ["hello.txt"]},
                        },
                        "fixture": {"response": "not yet", "files": {}},
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    calls: list[list[str]] = []

    def factory(profile: AgentProfile):
        bucket: list[str] = []
        calls.append(bucket)
        inner = ProfileAwareStubRunner(profile)

        class _Runner:
            async def run_case(self, case, workdir):
                bucket.append(case.case_id)
                return await inner.run_case(case, workdir)

        return _Runner()

    ctx = build_rsi_service_context(tasks_root=tmp_path / "tasks")
    created = ctx.create_task(name="partial", max_iterations=1, dataset_path=dataset)
    result = await ctx.optimize_task(created["task_id"], max_epochs=1, runner_factory=factory)
    assert result.baseline_score == 0.5
    assert result.best_score == 1.0
    assert result.epochs[0].adopted is True
    assert calls[0] == ["already-ok", "need-hello"]  # baseline full suite
    assert calls[1] == ["need-hello"]  # gate: only still-failing ids
    assert calls[2] == ["already-ok", "need-hello"]  # full-suite confirm before adopt


@pytest.mark.asyncio
async def test_optimize_stub_synthesizes_regex_response(tmp_path: Path) -> None:
    dataset = tmp_path / "suite.json"
    dataset.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "dataset_id": "regex-stub",
                "cases": [
                    {
                        "case_id": "regex-only",
                        "prompt": "Announce the deployment",
                        "evaluation": {
                            "method": "script_based",
                            "expect": {"response_regex": ["deployed successfully"]},
                        },
                    },
                    {
                        "case_id": "mixed-checks",
                        "prompt": "Confirm the rollout",
                        "evaluation": {
                            "method": "script_based",
                            "expect": {
                                "response_contains": ["rollout complete"],
                                "response_regex": ["zero downtime"],
                            },
                        },
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    ctx = build_rsi_service_context(tasks_root=tmp_path / "tasks")
    created = ctx.create_task(name="regex-opt", max_iterations=1, dataset_path=dataset)
    result = await ctx.optimize_task(created["task_id"], max_epochs=1)
    assert result.baseline_score == 0.0
    assert result.best_score == 1.0
    assert result.perfect is True
