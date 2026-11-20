# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Measure agent dependency initialization using isolated, reproducible inputs.

Manual development acceptance, not a CI timing gate. Run through ``uv run``.
The real main/child builders, MCP adapters/cache and skill loader are retained;
only model clients and user skill roots are replaced. Synthetic MCP scenarios
inject a fixed transport-opening delay. STDIO scenarios run the local probe.

Compare an archived source tree with the working tree using the same script::

    uv run python scripts/profile_initialization.py --output /tmp/after.json
    PYTHONPATH=/tmp/baseline/src:. uv run --no-sync python \
        scripts/profile_initialization.py --source-root /tmp/baseline/src \
        --label baseline --output /tmp/before.json

Fresh means a new MCP cache, not cold OS file caches or a new interpreter.
Warm and refresh prepare an untimed first build with the same cache. Reported
build time stops when the builder returns; commit/old-runtime cleanup is also
reported as cycle time. Use ``--surface tui --modes fresh refresh`` for real
test-harness TUI readiness, including its loading-dialog holds. That excludes
interpreter startup and native-terminal first paint.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import hashlib
import json
import logging
import math
import os
import platform
import statistics
import sys
import tempfile
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
SCENARIOS = (
    "empty",
    "mcp-success",
    "mcp-failure",
    "skills",
    "mixed",
    "stdio-success",
    "stdio-failure",
    "stdio-distinct",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source-root", type=Path, default=REPO / "src")
    parser.add_argument("--label", default="working-tree")
    parser.add_argument("--surface", choices=("builder", "tui"), default="builder")
    parser.add_argument("--children", nargs="+", type=int, default=[0, 1, 4, 8])
    parser.add_argument("--scenarios", nargs="+", choices=SCENARIOS, default=list(SCENARIOS[:5]))
    parser.add_argument(
        "--modes", nargs="+", choices=("fresh", "warm", "refresh"), default=["fresh", "warm", "refresh"]
    )
    parser.add_argument("--iterations", type=int, default=10)
    parser.add_argument("--warmups", type=int, default=1)
    parser.add_argument("--skills", type=int, default=200)
    parser.add_argument("--delay-ms", type=float, default=100)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.iterations < 1 or args.warmups < 0 or args.skills < 0 or any(n < 0 for n in args.children):
        parser.error("iterations must be positive; warmups, skills and child counts must be nonnegative")
    if not math.isfinite(args.delay_ms) or args.delay_ms < 0:
        parser.error("delay must be nonnegative")
    if args.surface == "tui" and "warm" in args.modes:
        parser.error("TUI runs support --modes fresh refresh; ordinary warm builds use --surface builder")
    return args


@dataclasses.dataclass
class Counts:
    connections: int = 0
    discovery: int = 0
    skill_files: int = 0
    connection_ms: float = 0
    skill_file_ms: float = 0


def summarize(samples: list[dict[str, float | int]]) -> dict[str, dict[str, float]]:
    return {
        key: {
            "median": statistics.median(sample[key] for sample in samples),
            "min": min(sample[key] for sample in samples),
            "max": max(sample[key] for sample in samples),
        }
        for key in samples[0]
    }


async def measure(args: argparse.Namespace) -> dict[str, Any]:
    imports_started = time.perf_counter()
    import pytest
    from tests.support.initialization_build import InitializationBuild, create_skills

    from chrys import __file__ as package_file
    from chrys.foundation import platform as platform_module
    from chrys.kernel import FunctionTool
    from chrys.service.mcp.cache import MCPConnectionCache
    from chrys.service.profiles.agents.schema import MCPServerConfig
    from chrys.service.skills import loader

    imports_ms = (time.perf_counter() - imports_started) * 1000
    package_path = Path(package_file).resolve()
    if not package_path.is_relative_to(args.source_root):
        raise RuntimeError(f"requested source {args.source_root}, imported {package_path}")
    source_digest = hashlib.sha256()
    for source in sorted((args.source_root / "chrys").rglob("*.py")):
        source_digest.update(str(source.relative_to(args.source_root)).encode("utf-8"))
        source_digest.update(source.read_bytes())
    original_open = MCPConnectionCache._open_mcp_tool
    original_discover = loader.discover_skill_directories
    original_load = loader.load_file_skill
    detected = platform_module.get_platform()
    report: dict[str, Any] = {
        "label": args.label,
        "surface": args.surface,
        "source_root": str(args.source_root.resolve()),
        "imported_package": str(package_path),
        "source_sha256": source_digest.hexdigest(),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "imports_ms": imports_ms,
        "iterations": args.iterations,
        "warmups": args.warmups,
        "delay_ms": args.delay_ms,
        "skill_count": args.skills,
        "limits": [
            "Isolated synthetic-configuration benchmark; surface selects builder-return or test-harness TUI readiness.",
            "Models and user skill roots are replaced; synthetic MCP opens are injected.",
            "Fresh means new connection cache, with interpreter/modules and OS caches potentially warm.",
            "Warm/refresh each have an untimed first build; cleanup follows every measured cycle.",
            "Discovery counts real loader calls; skill_file_ms excludes directory discovery and revision stamping.",
            "Logging is disabled consistently in both runs; no external network or real model is used.",
        ],
        "cases": [],
    }

    async def tui_cycle(fixture, monkeypatch, mode, counts, expected_skills, state):
        from tests.app.tui.screens.test_prompt_shortcut_send import running_session
        from tests.app.tui.screens.test_reload_mcp import reloaded
        from tests.support.engines import EngineTracker

        from chrys.foundation.config.settings import Settings
        from chrys.orchestration.engine.assembly import assemble_agent_engine
        from chrys.service.profiles.agents.serializer import save_profile
        from chrys.service.profiles.models.env_bridge import activate_model_profile
        from chrys.service.profiles.models.serializer import save_profile as save_model_profile

        save_profile(fixture.profile)
        model = fixture.models.get("mock-model")
        if model is None:
            raise RuntimeError("missing synthetic model")
        save_model_profile(model)
        activate_model_profile(model.id)
        for reference in fixture.profile.sub_agents.agents:
            child = fixture.registry.get(reference.profile)
            if child is None:
                raise RuntimeError("missing synthetic child profile")
            save_profile(child)
        tracker = EngineTracker()

        def engine_factory(*positional, **keywords):
            return tracker.register(assemble_agent_engine(*positional, **keywords))

        before = dataclasses.asdict(counts)
        starts_file = state / "starts"
        process_before = len(starts_file.read_text(encoding="utf-8").splitlines()) if starts_file.exists() else 0
        started = time.perf_counter()
        try:
            async with running_session(
                engine_factory,
                fixture.root,
                monkeypatch,
                registry=fixture.registry,
                models=fixture.models,
                settings=Settings(model_profile="mock-model", workspace_change_notice=False),
                startup_agent="Parent",
                load_holds=True,
            ) as session:
                if mode == "refresh":
                    before = dataclasses.asdict(counts)
                    process_before = (
                        len(starts_file.read_text(encoding="utf-8").splitlines()) if starts_file.exists() else 0
                    )
                    started = time.perf_counter()
                    await reloaded(session)
                elapsed_ms = (time.perf_counter() - started) * 1000
                if len(session.engine.current.manifest.skill_names) != expected_skills:
                    raise RuntimeError("the TUI did not load all synthetic skills")
                sample = {key: value - before[key] for key, value in dataclasses.asdict(counts).items()}
                process_after = len(starts_file.read_text(encoding="utf-8").splitlines()) if starts_file.exists() else 0
                sample.update(build_ms=elapsed_ms, cycle_ms=elapsed_ms, processes=process_after - process_before)
                return sample
        finally:
            await tracker.finalize()

    if args.surface == "tui":
        report["limits"].extend(
            [
                "Real ChrysApp.run_test TUI and engine; timer ends after readiness and loading dialog dismissal.",
                "TUI imports and fixture file creation are outside timing; this is not native-terminal process startup.",
                "Production loading-dialog holds are retained; model clients are deterministic and dependencies follow the selected scenario.",
                "TUI build_ms/cycle_ms both mean application readiness (fresh) or explicit slash-reload completion.",
            ]
        )

    async def probe(label: str = "") -> str:
        return label or "synthetic response"

    class SyntheticTool:
        def __init__(self) -> None:
            self.name = "probe"
            self.functions = [FunctionTool(func=probe, name="probe", description="Synthetic local probe")]
            self._server_instructions = ""

        async def probe_session(self, *, timeout: float) -> None:
            return None

        async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
            return None

    with tempfile.TemporaryDirectory(prefix="icode-initialization-profile-") as temporary:
        root = Path(temporary).resolve()
        with pytest.MonkeyPatch.context() as isolation:
            isolated_platform = dataclasses.replace(detected, config_dir=root / "config", data_dir=root / "config")
            isolation.setattr(platform_module, "detect_platform", lambda: isolated_platform)
            platform_module.get_platform.cache_clear()
            isolation.chdir(root)
            for key in tuple(os.environ):
                if key.startswith("CHRYS_"):
                    isolation.delenv(key)
            for scenario in args.scenarios:
                case_root = root / scenario
                case_root.mkdir()
                has_skills = scenario in {"skills", "mixed"}
                create_skills(case_root, args.skills if has_skills else 0)
                state = case_root / "server"
                state.mkdir()
                is_stdio = scenario.startswith("stdio-")
                fails = scenario in {"mcp-failure", "mixed", "stdio-failure"}
                if fails and is_stdio:
                    (state / "refuse").touch()
                configs = []
                if scenario not in {"skills", "empty"}:
                    configs = [
                        MCPServerConfig(
                            name="probe",
                            transport="stdio",
                            command=sys.executable if is_stdio else "synthetic",
                            args=[str(REPO / "tests/support/mcp_reload_probe_server.py"), "--state-dir", str(state)]
                            if is_stdio
                            else [],
                        )
                    ]
                for child_count in args.children:
                    fixture = InitializationBuild(case_root, child_count, configs)
                    if scenario == "stdio-distinct":
                        for reference in fixture.profile.sub_agents.agents:
                            child = fixture.registry.get(reference.profile)
                            if child is None:
                                raise RuntimeError("missing synthetic child profile")
                            child.tools.mcp = [dataclasses.replace(configs[0], name=f"probe-{reference.profile}")]
                    with pytest.MonkeyPatch.context() as monkeypatch:
                        fixture.install(monkeypatch)
                        for mode in args.modes:
                            samples = []
                            for iteration in range(args.warmups + args.iterations):
                                counts = Counts()

                                async def open_tool(
                                    cache, effective, *, counters=counts, real_stdio=is_stdio, failure=fails
                                ):
                                    counters.connections += 1
                                    started = time.perf_counter()
                                    try:
                                        if real_stdio:
                                            return await original_open(cache, effective)
                                        # Deliberate latency injection, not a readiness wait.
                                        await asyncio.sleep(args.delay_ms / 1000)
                                        if failure:
                                            raise RuntimeError("synthetic connection failure")
                                        return SyntheticTool()
                                    finally:
                                        counters.connection_ms += (time.perf_counter() - started) * 1000

                                def discover(paths, *, counters=counts):
                                    counters.discovery += 1
                                    return original_discover(paths)

                                def load(
                                    skill_dir,
                                    *,
                                    resource_extensions=loader.DEFAULT_RESOURCE_EXTENSIONS,
                                    script_extensions,
                                    search_depth=loader.DEFAULT_SEARCH_DEPTH,
                                    script_filter=None,
                                    resource_filter=None,
                                    counters=counts,
                                ):
                                    counters.skill_files += 1
                                    started = time.perf_counter()
                                    try:
                                        return original_load(
                                            skill_dir,
                                            resource_extensions=resource_extensions,
                                            script_extensions=script_extensions,
                                            search_depth=search_depth,
                                            script_filter=script_filter,
                                            resource_filter=resource_filter,
                                        )
                                    finally:
                                        counters.skill_file_ms += (time.perf_counter() - started) * 1000

                                cache = MCPConnectionCache()
                                builds = []
                                with (
                                    patch.object(MCPConnectionCache, "_open_mcp_tool", open_tool),
                                    patch.object(loader, "discover_skill_directories", discover),
                                    patch.object(loader, "load_file_skill", load),
                                ):
                                    try:
                                        if args.surface == "tui":
                                            sample = await tui_cycle(
                                                fixture,
                                                monkeypatch,
                                                mode,
                                                counts,
                                                args.skills if has_skills else 0,
                                                state,
                                            )
                                            if iteration >= args.warmups:
                                                samples.append(sample)
                                            continue
                                        if mode != "fresh":
                                            builds.append(await fixture.build(cache))
                                        before = dataclasses.asdict(counts)
                                        starts_file = state / "starts"
                                        process_before = (
                                            len(starts_file.read_text(encoding="utf-8").splitlines())
                                            if starts_file.exists()
                                            else 0
                                        )
                                        if mode == "refresh":
                                            cache.begin_refresh()
                                        started = time.perf_counter()
                                        result = await fixture.build(cache)
                                        builds.append(result)
                                        build_ms = (time.perf_counter() - started) * 1000
                                        if mode == "refresh":
                                            await cache.commit_refresh()
                                        if len(builds) > 1:
                                            await builds[0].prepared.aclose()
                                        cycle_ms = (time.perf_counter() - started) * 1000
                                        if len(result.skill_names) != (args.skills if has_skills else 0):
                                            raise RuntimeError("the real builder did not load all synthetic skills")
                                        if is_stdio and not fails:
                                            if result.mcp_adapter is None:
                                                raise RuntimeError("missing MCP adapter")
                                            lease = result.mcp_adapter._leases["probe"]
                                            (answer,) = await lease.functions[0].invoke(label="acceptance")
                                            if "answered by" not in answer.text:
                                                raise RuntimeError("the real MCP tool was not callable")
                                        process_after = (
                                            len(starts_file.read_text(encoding="utf-8").splitlines())
                                            if starts_file.exists()
                                            else 0
                                        )
                                        sample = {
                                            key: value - before[key]
                                            for key, value in dataclasses.asdict(counts).items()
                                        }
                                        sample.update(
                                            build_ms=build_ms,
                                            cycle_ms=cycle_ms,
                                            processes=process_after - process_before,
                                        )
                                        if iteration >= args.warmups:
                                            samples.append(sample)
                                    finally:
                                        if cache.refreshing:
                                            await cache.abort_refresh()
                                        for build in reversed(builds):
                                            await build.prepared.aclose()
                                        await cache.close_all()
                            case = {
                                "scenario": scenario,
                                "children": child_count,
                                "mode": mode,
                                "summary": summarize(samples),
                                "samples": samples,
                            }
                            report["cases"].append(case)
                            args.output.write_text(
                                json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
                            )
                            sys.stdout.write(
                                f"{args.label} {scenario} children={child_count} {mode}: "
                                f"{case['summary']['build_ms']['median']:.1f} ms, "
                                f"connect={case['summary']['connections']['median']}, "
                                f"discover={case['summary']['discovery']['median']}\n",
                            )
                            sys.stdout.flush()
        platform_module.get_platform.cache_clear()
    return report


def main() -> None:
    args = parse_args()
    args.source_root = args.source_root.resolve()
    args.output = args.output.resolve()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    # The source override precedes the checkout's test helper package for baseline runs.
    sys.path[:0] = [str(args.source_root), str(REPO)]
    logging.disable(logging.CRITICAL)
    asyncio.run(measure(args))


if __name__ == "__main__":
    main()
