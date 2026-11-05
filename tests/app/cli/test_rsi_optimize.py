# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""RSI optimize CLI flags and the headless candidate runner factory."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from chrys.app.cli import rsi as rsi_cli
from chrys.app.cli.rsi import build_parser
from chrys.app.cli.rsi_headless import build_candidate_runner_factory
from chrys.service.profiles.agents.registry import AgentProfileRegistry
from chrys.service.profiles.agents.schema import AgentProfile
from chrys.service.rsi import build_rsi_service_context


def test_optimize_parser_accepts_agent_and_optimizer_model() -> None:
    args = build_parser().parse_args(
        [
            "optimize",
            "rsi-abc",
            "--agent",
            "Code",
            "-m",
            "fast",
            "--optimizer-model",
            "think",
            "--epochs",
            "3",
            "--timeout",
            "12",
        ]
    )
    assert args.agent == "Code"
    assert args.model == "fast"
    assert args.optimizer_model == "think"
    assert args.epochs == 3
    assert args.timeout == 12.0


def test_candidate_runner_factory_registers_the_mutated_profile() -> None:
    profile = AgentProfile(name="RsiCandidate", instructions="be brief\n")
    runner = build_candidate_runner_factory(timeout=9.0)(profile)
    assert runner.agent == "RsiCandidate"
    assert runner.timeout == 9.0
    assert runner.agent_registry is not None
    loaded = runner.agent_registry.get("RsiCandidate")
    assert loaded is not None
    assert loaded.instructions == "be brief\n"


def test_candidate_runner_factory_keeps_declared_sub_agents() -> None:
    builtins = AgentProfileRegistry()
    builtins.load_builtins()
    code = builtins.get("Code")
    assert code is not None
    assert any(ref.profile == "Explore" for ref in code.sub_agents.agents)
    assert any(ref.profile == "General" for ref in code.sub_agents.agents)

    mutated = replace(code, instructions="rsi mutated instructions\n")
    runner = build_candidate_runner_factory(timeout=9.0)(mutated)
    registry = runner.agent_registry
    assert registry is not None
    assert registry.get("Code") is not None
    assert registry.get("Code").instructions == "rsi mutated instructions\n"
    assert registry.get("Explore") is not None
    assert registry.get("General") is not None


def test_rsi_main_create_get_list_and_stub_optimize(tmp_path: Path, monkeypatch) -> None:
    """Drive ``rsi.main`` end-to-end on the stub path (no live model / BYPASS runner)."""
    dataset = tmp_path / "dataset.json"
    dataset.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "dataset_id": "cli-e2e",
                "cases": [
                    {
                        "case_id": "need-hello",
                        "prompt": "Write hello.txt",
                        "evaluation": {
                            "method": "script_based",
                            "expect": {"response_contains": ["hello"], "files_exist": ["hello.txt"]},
                        },
                        "fixture": {"response": "not yet", "files": {}},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    ctx = build_rsi_service_context(tasks_root=tmp_path / "tasks")

    class _Prepared:
        loaded = object()
        settings = object()

    def _prepared() -> _Prepared:
        return _Prepared()

    def _ctx() -> object:
        return ctx

    monkeypatch.setattr(rsi_cli, "_prepare_runtime", _prepared)
    monkeypatch.setattr(rsi_cli, "build_rsi_service_context", _ctx)

    assert rsi_cli.main(["create", "--name", "e2e", "--dataset", str(dataset), "--json"]) == 0
    tasks = ctx.list_tasks()
    assert len(tasks) == 1
    task_id = tasks[0]["task_id"]
    assert rsi_cli.main(["get", task_id, "--json"]) == 0
    assert rsi_cli.main(["list", "--json"]) == 0
    # Stub optimize (no --agent): gated loop with profile-aware stub runner.
    assert rsi_cli.main(["optimize", task_id, "--epochs", "1", "--json"]) == 0
    detail = ctx.get_task(task_id)
    assert detail["config"].get("last_optimize_best_candidate")
    assert rsi_cli.main(["tree", task_id, "--json"]) == 0
