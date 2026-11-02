# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""RSI optimize CLI flags and the headless candidate runner factory."""

from __future__ import annotations

from dataclasses import replace

from chrys.app.cli.rsi import build_parser
from chrys.app.cli.rsi_headless import build_candidate_runner_factory
from chrys.service.profiles.agents.registry import AgentProfileRegistry
from chrys.service.profiles.agents.schema import AgentProfile


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
