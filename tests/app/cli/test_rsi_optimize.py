# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""RSI optimize CLI flags and the headless candidate runner factory."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

from chrys.app.cli import rsi as rsi_cli
from chrys.app.cli.rsi import build_parser
from chrys.app.cli.rsi_headless import build_candidate_runner_factory
from chrys.service.profiles.agents.registry import AgentProfileRegistry
from chrys.service.profiles.agents.schema import AgentProfile
from chrys.service.profiles.agents.serializer import save_profile
from chrys.service.rsi import build_rsi_service_context
from chrys.service.rsi.case_runner import CaseRunResult
from chrys.service.rsi.dataset import RsiCase


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


def _drive_cli(tmp_path: Path, monkeypatch) -> object:
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
    return ctx


def test_rsi_optimize_reports_install_candidate_and_install_flags(tmp_path: Path, monkeypatch, capsys) -> None:
    fixture = Path(__file__).parents[2] / "service" / "rsi" / "fixtures" / "optimize_dataset.json"
    ctx = _drive_cli(tmp_path, monkeypatch)
    assert rsi_cli.main(["create", "--name", "flags", "--dataset", str(fixture), "--json"]) == 0
    task_id = ctx.list_tasks()[0]["task_id"]
    capsys.readouterr()

    assert rsi_cli.main(["optimize", task_id, "--epochs", "1"]) == 0
    out = capsys.readouterr().out
    assert "inconclusive=0" in out
    assert "best_json_updated=True" in out
    assert "install_kind=stub" in out
    assert rsi_cli.main(["get", task_id]) == 0
    assert "last_optimize_install_candidate=" in capsys.readouterr().out

    target = tmp_path / "exported.yaml"
    user_dir = tmp_path / "agents"
    assert rsi_cli.main(["install", task_id, "--out", str(target), "--user", "--user-dir", str(user_dir)]) == 2
    assert "only one of --out or --user" in capsys.readouterr().err
    assert not target.exists()
    assert rsi_cli.main(["install", task_id, "--out", str(target)]) == 0
    assert rsi_cli.main(["install", task_id, "--out", str(target)]) == 1
    assert "--force" in capsys.readouterr().err
    assert rsi_cli.main(["install", task_id, "--out", str(target), "--force"]) == 0


class _ReadyErrors:
    """``need-ready`` cannot run; every other case answers without satisfying its checks."""

    async def run_case(self, case: RsiCase, workdir: Path) -> CaseRunResult:
        workdir.mkdir(parents=True, exist_ok=True)
        if case.case_id == "need-ready":
            return CaseRunResult(case_id=case.case_id, response_text="", workdir=workdir, error="provider timed out")
        return CaseRunResult(case_id=case.case_id, response_text="no", workdir=workdir)


def test_rsi_eval_runs_a_profile_yaml_and_reports_errored_cases(tmp_path: Path, monkeypatch, capsys) -> None:
    fixture = Path(__file__).parents[2] / "service" / "rsi" / "fixtures" / "optimize_dataset.json"
    _drive_cli(tmp_path, monkeypatch)
    profile_path = save_profile(AgentProfile(name="Exported", instructions="be exact\n"), target_dir=tmp_path / "p")
    built: list[AgentProfile] = []

    def fake_factory(
        *, loaded_settings: object, model_registry: object, timeout: float | None
    ) -> Callable[[AgentProfile], _ReadyErrors]:
        del loaded_settings, model_registry
        assert timeout == 30.0

        def make(candidate: AgentProfile) -> _ReadyErrors:
            built.append(candidate)
            return _ReadyErrors()

        return make

    monkeypatch.setattr(rsi_cli, "build_candidate_runner_factory", fake_factory)
    argv = ["eval", "--dataset", str(fixture), "--profile", str(profile_path), "--timeout", "30"]
    assert rsi_cli.main(argv) == 1
    out = capsys.readouterr().out
    assert [(item.name, item.instructions) for item in built] == [("Exported", "be exact\n")]
    assert "passed=0/2" in out
    assert "errored=1" in out
    assert rsi_cli.main([*argv, "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["errored"] == 1

    assert rsi_cli.main(["eval", "--dataset", str(fixture), "--profile", str(profile_path), "--stub"]) == 2
    assert "without --agent or --stub" in capsys.readouterr().err
    assert rsi_cli.main(["eval", "--dataset", str(fixture), "--profile", str(profile_path), "-a", "Code"]) == 2
    assert rsi_cli.main(["eval", "--dataset", str(fixture)]) == 2
    assert "requires --agent or --profile" in capsys.readouterr().err


def test_rsi_eval_refuses_out_inside_git_repository(tmp_path: Path, monkeypatch, git_repo_factory, capsys) -> None:
    fixture = Path(__file__).parents[2] / "service" / "rsi" / "fixtures" / "optimize_dataset.json"
    repo = git_repo_factory(tmp_path / "repo")
    _drive_cli(tmp_path, monkeypatch)
    assert rsi_cli.main(["eval", "--dataset", str(fixture), "--stub", "--out", str(repo / "rsi-out")]) == 2
    assert "inside the git repository" in capsys.readouterr().err
    assert not (repo / "rsi-out").exists()
    outside = tmp_path / "outside"
    assert rsi_cli.main(["eval", "--dataset", str(fixture), "--stub", "--out", str(outside)]) in {0, 1}
    assert outside.is_dir()
