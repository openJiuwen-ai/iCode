# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Headless ``chrys rsi`` commands — RSI task control, eval, optimize, install."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Callable
from pathlib import Path

from chrys.app.cli import headless
from chrys.app.cli.rsi_headless import build_candidate_runner_factory, build_case_runner
from chrys.foundation.branding import APP_COMMAND, APP_DISPLAY_NAME
from chrys.foundation.config.spec import Source
from chrys.foundation.platform.files import surrogate_safe_text
from chrys.foundation.platform.signals import hangup_as_interrupt
from chrys.service.profiles.agents.loader import AgentProfileLoadError, load_profile_from_yaml
from chrys.service.profiles.agents.registry import AgentProfileRegistry
from chrys.service.profiles.agents.schema import AgentProfile
from chrys.service.profiles.models.registry import ModelProfileRegistry
from chrys.service.profiles.models.resolver import (
    format_available_profile_labels,
    loaded_with_active_model_profile,
    resolve_active_profile,
    resolve_profile_selector,
)
from chrys.service.profiles.models.schema import ModelProfile, is_model_profile_selectable
from chrys.service.rsi import RsiError, RsiServiceContext, build_rsi_service_context, validate_dataset_file
from chrys.service.rsi.llm_optimizer import LlmOptimizer, complete_with_model_profile
from chrys.service.rsi.models import TaskStatus


def _write_stderr(message: str) -> None:
    sys.stderr.write(f"{surrogate_safe_text(message)}\n")


def _write_stdout(message: str) -> None:
    sys.stdout.write(f"{surrogate_safe_text(message)}\n")


def _add_help(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "-h", "--help", action="help", default=argparse.SUPPRESS, help="Show this help message and exit"
    )


def build_parser() -> argparse.ArgumentParser:
    """Build the ``chrys rsi`` parser."""
    parser = argparse.ArgumentParser(
        prog=f"{APP_COMMAND} rsi",
        description=(
            f"Experimental RSI (recursive self-improvement) for {APP_DISPLAY_NAME}. "
            "Tasks live under <config_dir>/rsi/tasks/. "
            "Phases 1-3: mock train, script_based eval, gated profile/skills optimize "
            "(stub by default; --agent runs live eval and an LLM proposer)."
        ),
        add_help=False,
    )
    _add_help(parser)
    commands = parser.add_subparsers(dest="command", metavar="command", required=True)

    create = commands.add_parser("create", help="Create a PROFILE RSI task", add_help=False)
    _add_help(create)
    create.add_argument("--name", required=True, help="Task display name")
    create.add_argument(
        "--max-iterations",
        type=int,
        default=2,
        metavar="N",
        help="Mock epoch count for rsi start (default: 2)",
    )
    create.add_argument("--instruction", default=None, help="Optional optimization instruction")
    create.add_argument(
        "--dataset",
        metavar="PATH",
        default=None,
        help="Optional chrys-rsi.dataset.v1 JSON to copy into the task input/",
    )
    create.add_argument("--json", action="store_true", help="Emit JSON")

    list_parser = commands.add_parser("list", help="List RSI tasks", add_help=False)
    _add_help(list_parser)
    list_parser.add_argument("--json", action="store_true", help="Emit JSON")

    get_parser = commands.add_parser("get", help="Show one RSI task", add_help=False)
    _add_help(get_parser)
    get_parser.add_argument("task_id", help="Task id (rsi-…)")
    get_parser.add_argument("--json", action="store_true", help="Emit JSON")

    start = commands.add_parser("start", help="Queue and run a task with the mock provider", add_help=False)
    _add_help(start)
    start.add_argument("task_id", help="Task id (rsi-…)")
    start.add_argument("--json", action="store_true", help="Emit JSON")
    start.add_argument("--no-wait", action="store_true", help="Enqueue only; do not wait for completion")

    tree = commands.add_parser("tree", help="Show the evolution tree projection", add_help=False)
    _add_help(tree)
    tree.add_argument("task_id", help="Task id (rsi-…)")
    tree.add_argument("--json", action="store_true", help="Emit JSON")

    delete = commands.add_parser("delete", help="Delete a terminal or CREATED task", add_help=False)
    _add_help(delete)
    delete.add_argument("task_id", help="Task id (rsi-…)")

    dataset = commands.add_parser("dataset", help="Dataset helpers", add_help=False)
    _add_help(dataset)
    dataset_commands = dataset.add_subparsers(dest="dataset_command", metavar="command", required=True)
    validate = dataset_commands.add_parser("validate", help="Validate a dataset JSON file", add_help=False)
    _add_help(validate)
    validate.add_argument("path", help="Path to dataset JSON")
    validate.add_argument("--json", action="store_true", help="Emit JSON")

    eval_parser = commands.add_parser(
        "eval",
        help="Run script_based evaluation over a dataset (or a task's attached dataset)",
        add_help=False,
    )
    _add_help(eval_parser)
    eval_parser.add_argument("--dataset", metavar="PATH", default=None, help="Dataset JSON path")
    eval_parser.add_argument("--task", metavar="ID", default=None, help="Use the dataset attached to this task")
    eval_parser.add_argument(
        "--stub",
        action="store_true",
        help="Use case fixtures instead of a live agent (required unless --agent is set)",
    )
    eval_parser.add_argument("-a", "--agent", default=None, help="Agent profile for live headless case runs")
    eval_parser.add_argument("-m", "--model", default=None, help="Optional model profile for live runs")
    eval_parser.add_argument(
        "--out",
        metavar="DIR",
        default=None,
        help="Output directory for scorecard artifacts (default: task run/evaluations or <tasks>/_evals)",
    )
    eval_parser.add_argument(
        "--timeout",
        type=float,
        default=300.0,
        metavar="SECONDS",
        help="Per-case headless timeout (default: 300)",
    )
    eval_parser.add_argument("--json", action="store_true", help="Emit JSON")

    optimize = commands.add_parser(
        "optimize",
        help="Gate profile/skills mutations on the task dataset (stub by default)",
        add_help=False,
    )
    _add_help(optimize)
    optimize.add_argument("task_id", help="Task id with an attached dataset")
    optimize.add_argument(
        "--epochs",
        type=int,
        default=None,
        metavar="N",
        help="Max optimize epochs (default: task max_iterations)",
    )
    optimize.add_argument(
        "--profile",
        metavar="PATH",
        default=None,
        help="Optional base agent profile YAML (default: --agent, else empty RsiCandidate)",
    )
    optimize.add_argument(
        "-a",
        "--agent",
        default=None,
        help="Live-eval each candidate (approval bypass). Names the base profile unless --profile is set",
    )
    optimize.add_argument(
        "-m",
        "--model",
        default=None,
        help="Model profile for live case runs (default: the active profile)",
    )
    optimize.add_argument(
        "--optimizer-model",
        default=None,
        metavar="PROFILE",
        help="Model profile that proposes mutations (default with --agent: the case-run model)",
    )
    optimize.add_argument(
        "--timeout",
        type=float,
        default=300.0,
        metavar="SECONDS",
        help="Per-case headless timeout when --agent is set (default: 300)",
    )
    optimize.add_argument("--json", action="store_true", help="Emit JSON")

    install = commands.add_parser(
        "install",
        help="Export the best gated candidate YAML (never auto-writes user agents)",
        add_help=False,
    )
    _add_help(install)
    install.add_argument("task_id", help="Task id that has been optimized")
    install.add_argument(
        "--out",
        metavar="PATH",
        default=None,
        help="Explicit export path (default: <task>/export/<name>.yaml)",
    )
    install.add_argument(
        "--user",
        action="store_true",
        help="Also require --user-dir; write into that agents directory (opt-in)",
    )
    install.add_argument(
        "--user-dir",
        metavar="DIR",
        default=None,
        help="User agents directory when --user is set (explicit; no default)",
    )
    install.add_argument(
        "--candidate",
        metavar="ID",
        default=None,
        help="Export a specific candidate id instead of best.json",
    )
    install.add_argument(
        "--force",
        action="store_true",
        help="Overwrite an existing export destination",
    )
    install.add_argument("--json", action="store_true", help="Emit JSON")

    return parser


def _prepare_runtime() -> headless.PreparedRuntime:
    prepared = headless.prepare_runtime()
    headless.write_pending_warnings(prepared, as_json=False)
    return prepared


def _emit(payload: object, *, as_json: bool, lines: list[str] | None = None) -> None:
    if as_json:
        _write_stdout(json.dumps(payload, ensure_ascii=True, indent=2))
        return
    if lines is not None:
        for line in lines:
            _write_stdout(line)
        return
    _write_stdout(str(payload))


def _expanded_path(value: str | None) -> Path | None:
    if value is None or not str(value).strip():
        return None
    return Path(value).expanduser()


def _get_detail_lines(detail: dict[str, object]) -> list[str]:
    config = detail.get("config")
    config_dict = config if isinstance(config, dict) else {}
    lines = [
        f"task_id={detail['task_id']}",
        f"name={detail['name']}",
        f"status={detail['status']}",
        f"scenario={detail['scenario']}",
        f"max_iterations={config_dict.get('max_iterations')}",
        f"input_file={config_dict.get('input_file')}",
    ]
    lines.extend(
        f"{key}={config_dict[key]}"
        for key in sorted(config_dict)
        if str(key).startswith(("last_eval_", "last_optimize_"))
    )
    return lines


def main(argv: list[str] | None = None) -> int:
    """Entry point for ``chrys rsi`` / ``icode rsi``."""
    parser = build_parser()
    args = parser.parse_args(argv)
    prepared = _prepare_runtime()
    ctx = build_rsi_service_context()
    try:
        with hangup_as_interrupt():
            return _dispatch(args, ctx, prepared, parser)
    except KeyboardInterrupt:
        _write_stderr("Interrupted")
        return 130
    except RsiError as exc:
        _write_stderr(f"Error: [{exc.code}] {exc.message}")
        return 1
    except AgentProfileLoadError as exc:
        _write_stderr(f"Error: {exc}")
        return 1
    except OSError as exc:
        _write_stderr(f"Error: {exc}")
        return 1
    except ValueError as exc:
        _write_stderr(f"Error: {exc}")
        return 1


def _dispatch(args: argparse.Namespace, ctx: RsiServiceContext, prepared: headless.PreparedRuntime, parser) -> int:
    if args.command == "create":
        result = ctx.create_task(
            name=args.name,
            max_iterations=args.max_iterations,
            optimization_instruction=args.instruction,
            dataset_path=_expanded_path(args.dataset),
        )
        _emit(
            result,
            as_json=args.json,
            lines=[f"{result['task_id']}\t{result['status']}"],
        )
        return 0
    if args.command == "list":
        tasks = ctx.list_tasks()
        _emit(
            tasks,
            as_json=args.json,
            lines=[f"{item['task_id']}\t{item['status']}\t{item['scenario']}\t{item['name']}" for item in tasks]
            or ["(no RSI tasks)"],
        )
        return 0
    if args.command == "get":
        detail = ctx.get_task(args.task_id)
        _emit(
            detail,
            as_json=args.json,
            lines=_get_detail_lines(detail),
        )
        return 0
    if args.command == "start":
        return asyncio.run(_start(ctx, args.task_id, as_json=args.json, wait=not args.no_wait))
    if args.command == "tree":
        tree_payload = ctx.get_tree(args.task_id)
        nodes = tree_payload.get("nodes") or []
        _emit(
            tree_payload,
            as_json=args.json,
            lines=[
                f"{node.get('node_id')}\t{node.get('type')}\tscore={node.get('score')}\t{node.get('description') or ''}"
                for node in nodes
            ]
            or ["(empty tree)"],
        )
        return 0
    if args.command == "delete":
        ctx.delete_task(args.task_id)
        _write_stdout(f"deleted {args.task_id}")
        return 0
    if args.command == "dataset" and args.dataset_command == "validate":
        result = validate_dataset_file(str(Path(args.path).expanduser()))
        _emit(
            result,
            as_json=args.json,
            lines=(
                [f"valid={result['valid']}", f"sample_count={result['sample_count']}"]
                if result["valid"]
                else ["valid=false", *(f"{err['code']}: {err['reason']}" for err in result["errors"])]
            ),
        )
        return 0 if result["valid"] else 1
    if args.command == "eval":
        return asyncio.run(_eval(ctx, args, prepared))
    if args.command == "optimize":
        return asyncio.run(_optimize(ctx, args, prepared))
    if args.command == "install":
        return _install(ctx, args)
    _write_stderr(f"Error: unknown command {args.command!r}")
    return 2


async def _start(ctx: RsiServiceContext, task_id: str, *, as_json: bool, wait: bool) -> int:
    result = ctx.start(task_id)
    if wait:
        await ctx.wait_idle()
        result = {"task_id": task_id, "status": ctx.get_task(task_id)["status"]}
    else:
        result = {"task_id": task_id, "status": TaskStatus.QUEUED.value}
    _emit(
        result,
        as_json=as_json,
        lines=[f"{result['task_id']}\t{result['status']}"],
    )
    return 0


async def _eval(ctx: RsiServiceContext, args: argparse.Namespace, prepared: headless.PreparedRuntime) -> int:
    if bool(args.dataset) == bool(args.task):
        _write_stderr("Error: provide exactly one of --dataset or --task")
        return 2
    if not args.stub and not args.agent:
        _write_stderr("Error: live evaluation requires --agent (or pass --stub)")
        return 2

    loaded = prepared.loaded
    model_registry = None
    if args.model and not args.stub:
        registry = ModelProfileRegistry()
        registry.load_all()
        profile = resolve_profile_selector(registry, args.model)
        if profile is None:
            available = format_available_profile_labels(registry)
            message = f"Model profile not found: {args.model}"
            if available:
                message = f"{message}. Available model profiles: {available}"
            _write_stderr(f"Error: {message}")
            return 1
        loaded = loaded_with_active_model_profile(loaded, profile, Source.CLI)
        model_registry = registry

    runner = build_case_runner(
        stub=bool(args.stub),
        agent=args.agent,
        loaded_settings=None if args.stub else loaded,
        model_registry=model_registry,
        timeout=args.timeout,
    )
    out_dir = _expanded_path(args.out)
    if args.task:
        result = await ctx.evaluate_task(args.task, runner=runner, output_dir=out_dir)
    else:
        dataset_path = _expanded_path(args.dataset)
        if dataset_path is None:
            _write_stderr("Error: --dataset is required")
            return 2
        result = await ctx.evaluate_dataset(dataset_path, runner=runner, output_dir=out_dir)

    payload = {
        "run_id": result.run_id,
        "dataset_id": result.dataset_id,
        "passed": result.passed_count,
        "total": result.total_count,
        "score": result.score,
        "scorecard": str(result.scorecard_path),
        "cases": str(result.cases_path),
    }
    _emit(
        payload,
        as_json=args.json,
        lines=[
            f"run_id={result.run_id}",
            f"dataset_id={result.dataset_id}",
            f"passed={result.passed_count}/{result.total_count}",
            f"score={result.score}",
            f"scorecard={result.scorecard_path}",
        ],
    )
    return 0 if result.passed_count == result.total_count else 1


async def _optimize(ctx: RsiServiceContext, args: argparse.Namespace, prepared: headless.PreparedRuntime) -> int:
    base: AgentProfile | None = None
    if args.profile:
        profile_path = _expanded_path(args.profile)
        if profile_path is None:
            _write_stderr("Error: --profile path is empty")
            return 2
        base = load_profile_from_yaml(profile_path)
    elif args.agent:
        base = _load_agent_profile(args.agent)
        if base is None:
            _write_stderr(f"Error: agent profile not found: {args.agent}")
            return 1

    optimizer = None
    runner_factory = None
    if args.agent or args.optimizer_model:
        bound = _bind_live_optimizer(args, prepared, ctx)
        if isinstance(bound, int):
            return bound
        optimizer, runner_factory = bound

    result = await ctx.optimize_task(
        args.task_id,
        base_profile=base,
        max_epochs=args.epochs,
        optimizer=optimizer,
        runner_factory=runner_factory,
    )
    payload = result.to_dict()
    _emit(
        payload,
        as_json=args.json,
        lines=[
            f"task_id={result.task_id}",
            f"baseline_score={result.baseline_score}",
            f"best_score={result.best_score}",
            f"best_candidate={result.best_candidate_id}",
            f"epochs={len(result.epochs)}",
            f"dropped={sum(1 for epoch in result.epochs if epoch.dropped)}",
            f"perfect={result.perfect}",
        ],
    )
    return 0 if result.perfect or result.best_score > result.baseline_score else 1


def _load_agent_profile(name: str) -> AgentProfile | None:
    registry = AgentProfileRegistry()
    registry.load_all()
    return registry.resolve_selector(name)


def _select_model(
    registry: ModelProfileRegistry,
    selector: str | None,
    settings: headless.PreparedRuntime,
) -> tuple[ModelProfile | None, str | None]:
    """Resolve a model profile. ``selector`` empty means the active profile."""
    if selector:
        profile = resolve_profile_selector(registry, selector)
        if profile is None:
            available = format_available_profile_labels(registry)
            message = f"Model profile not found: {selector}"
            if available:
                message = f"{message}. Available model profiles: {available}"
            return None, message
    else:
        profile = resolve_active_profile(registry, settings.settings)
    if not is_model_profile_selectable(profile):
        return None, "no selectable model profile; pass -m or --optimizer-model"
    return profile, None


def _bind_live_optimizer(
    args: argparse.Namespace,
    prepared: headless.PreparedRuntime,
    ctx: RsiServiceContext,
) -> tuple[LlmOptimizer, Callable[..., object] | None] | int:
    """Build the LLM proposer and, when ``--agent`` is set, the headless runner factory."""
    registry = ModelProfileRegistry()
    registry.load_all()
    optimizer_profile, error = _select_model(
        registry,
        args.optimizer_model or args.model,
        prepared,
    )
    if optimizer_profile is None:
        _write_stderr(f"Error: {error}")
        return 1
    # Cases run on -m / the active model. --optimizer-model alone must not
    # pull case eval onto the proposer model.
    tester = optimizer_profile
    if args.agent:
        # None → active profile (not args.optimizer_model).
        tester, error = _select_model(registry, args.model, prepared)
        if tester is None:
            _write_stderr(f"Error: {error}")
            return 1

    task = ctx.store.get(args.task_id)
    analysis_dir = Path(task.run_dir) / "analysis"

    async def complete(prompt: str, profile: ModelProfile = optimizer_profile) -> str:
        return await complete_with_model_profile(profile, prompt)

    optimizer = LlmOptimizer(complete, analysis_dir=analysis_dir)
    runner_factory = None
    if args.agent:
        loaded = loaded_with_active_model_profile(prepared.loaded, tester, Source.CLI)
        runner_factory = build_candidate_runner_factory(
            loaded_settings=loaded,
            model_registry=registry,
            timeout=args.timeout,
        )
    return optimizer, runner_factory


def _install(ctx: RsiServiceContext, args: argparse.Namespace) -> int:
    if args.user and not args.user_dir:
        _write_stderr("Error: --user requires an explicit --user-dir")
        return 2
    if args.user_dir and not args.user:
        _write_stderr("Error: --user-dir requires --user")
        return 2
    if args.user and args.out:
        _write_stderr("Error: pass only one of --out or --user/--user-dir")
        return 2
    summary = ctx.install_best(
        args.task_id,
        destination=_expanded_path(args.out),
        user_agents_dir=_expanded_path(args.user_dir),
        write_user=bool(args.user),
        candidate_id=args.candidate,
        force=bool(args.force),
    )
    _emit(
        summary,
        as_json=args.json,
        lines=[
            f"candidate_id={summary['candidate_id']}",
            f"profile_name={summary['profile_name']}",
            f"score={summary.get('score')}",
            f"export_path={summary['export_path']}",
        ],
    )
    return 0
