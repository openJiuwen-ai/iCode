# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Run local Chrys tests in smart or complete non-integration mode.

Smart mode follows nearby runtime imports and filesystem watches with a complete
collection of pytest's actual fixture consumers. It favors quick local feedback
over exhaustive impact coverage. CI does not use this selector; its complete
platform matrix remains the authoritative gate.
"""

from __future__ import annotations

import argparse
import ast
import fnmatch
import inspect
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zlib
from collections import Counter, defaultdict, deque
from collections.abc import Iterable, Iterator, Sequence
from copy import copy
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

_EXCLUDED_MARKERS = "not integration and not gc_calibration"
_HYGIENE_SHARDS = 4
_MAX_TARGET_ARGUMENTS = 180
_MAX_TARGET_CHARACTERS = 20_000
_MAX_RENDERED_REASONS = 3
_PYTEST_NO_TESTS_COLLECTED = 5
_PYTEST_FILE_PATTERNS = ("test_*.py", "*_test.py")
_MAX_SOURCE_IMPORT_HOPS = 1
_MAX_TEST_IMPORT_HOPS = 1
_BELOW_NORMAL_NICENESS = 10


@dataclass(frozen=True, slots=True)
class Change:
    """One repository-relative changed path and the Git states observed for it."""

    path: str
    states: frozenset[str]

    @property
    def deleted_or_renamed(self) -> bool:
        """Return whether current-tree analysis alone is insufficient for this path."""

        return bool(self.states & {"deleted", "renamed"})


@dataclass(frozen=True, slots=True)
class TestRule:
    """Select one test target when a changed path matches a watched surface."""

    target: str
    patterns: tuple[str, ...] = ()


@dataclass(slots=True)
class ImportGraph:
    """Current-tree module paths and reverse dependency edges."""

    path_to_module: dict[str, str]
    module_to_path: dict[str, str]
    reverse: dict[str, set[str]]
    parse_errors: tuple[str, ...]
    scopes: dict[str, str] = field(default_factory=dict)
    uncertain: dict[str, str] = field(default_factory=dict)
    consumers: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    collected: tuple[str, ...] = ()
    change_seeds: dict[str, set[str]] = field(default_factory=dict)
    facades: dict[str, dict[str, str]] = field(default_factory=dict)
    # Changed paths whose AST equals the branch baseline (comments, blank lines, formatting).
    runtime_noop: frozenset[str] = frozenset()
    # Fixture definitions in conftests, plugins and helpers: the scopes whose
    # pytest-resolved consumers the import graph cannot see.
    fixture_scopes: frozenset[str] = frozenset()
    # Plain test modules defining fixtures; importing one is a free test hop.
    fixture_modules: frozenset[str] = frozenset()


@dataclass(slots=True)
class Selection:
    """Smart-mode pytest targets with human-readable selection reasons."""

    regular: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    architecture: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    full_reason: str | None = None
    notes: set[str] = field(default_factory=set)
    # Every graph node the changed paths reach within the hop budgets.
    reached: set[str] = field(default_factory=set)

    def add(self, target: str, reason: str) -> None:
        """Add a pytest target to its execution group."""

        destination = (
            self.architecture
            if target == "tests/architecture" or target.startswith("tests/architecture/")
            else self.regular
        )
        destination[target].add(reason)


# Every architecture test is deliberately classified.  Empty patterns mean
# the test is selected by normal imports, direct edits, or a focused special
# rule below rather than by a filesystem-wide watch.
ARCHITECTURE_RULES = (
    TestRule("tests/architecture/test_analytics_facts.py", ("src/chrys/service/analytics/**",)),
    TestRule(
        "tests/architecture/test_chrys_test.py",
        ("AGENTS.md", "README.md", "scripts/chrys_test.py", "tests/architecture/**", ".github/workflows/**"),
    ),
    TestRule("tests/architecture/test_chrys_test_scope.py", ("scripts/chrys_test.py",)),
    TestRule("tests/architecture/test_chrys_test_summary.py", ("scripts/chrys_test.py",)),
    TestRule("tests/architecture/test_ci_test_partitions.py", (".github/workflows/ci.yml", "tests/**")),
    TestRule("tests/architecture/test_copy_freshness.py"),
    TestRule("tests/architecture/test_engine_fixture.py"),
    TestRule("tests/architecture/test_event_declaration_guard.py", ("src/chrys/foundation/events/**",)),
    TestRule(
        "tests/architecture/test_entrypoint_bootstrap.py",
        ("src/chrys/app/cli/**", "src/chrys/app/installer.py", "src/chrys/app/tui/app.py"),
    ),
    TestRule("tests/architecture/test_env_hermeticity.py"),
    TestRule("tests/architecture/test_hygiene_exchange_walker_shapes.py"),
    TestRule("tests/architecture/test_hygiene_exchange_walkers.py", ("src/chrys/**",)),
    TestRule(
        "tests/architecture/test_hygiene_i18n_messages.py",
        ("src/chrys/app/tui/**", "src/chrys/foundation/i18n/**"),
    ),
    TestRule("tests/architecture/test_hygiene_lazy_imports.py", ("src/chrys/**",)),
    TestRule("tests/architecture/test_hygiene_llm_client_owners.py", ("src/chrys/**",)),
    TestRule("tests/architecture/test_hygiene_pillow_formats.py", ("src/chrys/**",)),
    TestRule("tests/architecture/test_hygiene_reminder_sources.py", ("src/chrys/**",)),
    TestRule("tests/architecture/test_hygiene_session_surface.py"),
    TestRule("tests/architecture/test_hygiene_source_asserts.py"),
    TestRule("tests/architecture/test_hygiene_subprocess_stdin.py"),
    TestRule("tests/architecture/test_hygiene_test_source_rules.py", ("tests/**",)),
    TestRule("tests/architecture/test_hygiene_tui_bindings.py", ("src/chrys/app/tui/**",)),
    TestRule("tests/architecture/test_hygiene_tui_locale_controller.py"),
    TestRule("tests/architecture/test_hygiene_tui_prose.py", ("src/chrys/app/tui/**",)),
    TestRule("tests/architecture/test_hygiene_user_turn_events.py", ("src/chrys/app/tui/**",)),
    TestRule("tests/architecture/test_layering.py", ("src/chrys/**",)),
    TestRule("tests/architecture/test_network_egress.py"),
    TestRule(
        "tests/architecture/test_python_pin.py",
        (".python-version", ".github/workflows/**", "scripts/build.sh", "scripts/build.ps1"),
    ),
    TestRule("tests/architecture/test_quarantine.py"),
    TestRule("tests/architecture/test_test_file_size.py", ("tests/**",)),
    TestRule("tests/architecture/test_test_hygiene.py"),
    TestRule(
        "tests/architecture/test_test_layout.py",
        ("tests/**", ".gitignore", "pyproject.toml"),
    ),
    TestRule(
        "tests/architecture/test_textual_dispatch_isolation.py",
        (
            "tests/conftest.py",
            "tests/foundation/patches/test_textual_dispatch_cache.py",
            "src/chrys/foundation/patches/textual_dispatch_cache.py",
        ),
    ),
    TestRule("tests/architecture/test_trajectory_wait_inventory.py"),
    TestRule("tests/architecture/test_tui_structure.py", ("src/chrys/**",)),
    TestRule("tests/architecture/test_workflow_action_pins.py", (".github/workflows/**",)),
    TestRule(
        "tests/architecture/test_workflow_worker_purity.py",
        ("src/chrys/service/workflows/sdk/**", "src/chrys/service/workflows/worker_host.py"),
    ),
)

# The workflow worker host runs as a subprocess (test_protocol loads it by file
# path) with the SDK copied beside it, and the fake worker is a subprocess too.
_WORKFLOW_WORKER_HOST = ("src/chrys/service/workflows/worker_host.py", "src/chrys/service/workflows/sdk/**")
_WORKFLOW_FAKE_WORKER = ("tests/orchestration/workflows/fake_worker.py",)

# The reminder lifecycle tests drive the whole reminder pipeline, whose
# sources sit more than two imports away.
_REMINDER_PIPELINE = (
    "src/chrys/service/agent_middleware/system_reminder.py",
    "src/chrys/service/agent_middleware/reminders/**",
    "src/chrys/service/context/compaction/last_words_state.py",
)

# The client contracts build every LLM client through ``create_client``, most
# of it more than two imports from the test, and import the package in a
# subprocess.
_LLM_CLIENT_SOURCES = (
    "src/chrys/service/llm/**",
    "src/chrys/service/profiles/models/**",
)

# Regular tests can also consume repository files without importing them.  Keep
# those dependency edges explicit: subprocess fixtures, filesystem scanners,
# and import-every-module checks are invisible to the AST import graph.
REGULAR_RULES = (
    TestRule("tests/service/mcp/test_cache_e2e.py", ("tests/service/mcp/mcp_cache_probe_server.py",)),
    TestRule("tests/orchestration/engine/build/test_initialization.py", ("tests/support/mcp_reload_probe_server.py",)),
    TestRule("tests/app/tui/behaviors/test_startup_imports.py", ("src/chrys/**",)),
    TestRule("tests/service/approval/test_formal_core.py", ("src/chrys/service/approval/principles.json",)),
    TestRule("tests/app/tui/screens/test_approval_review_reason.py", ("src/chrys/service/approval/principles.json",)),
    TestRule("tests/foundation/test_documentation.py", ("pyproject.toml", "docs/index.yaml")),
    # Builds a wheel in a subprocess: the build hook compiles the PO into it.
    TestRule(
        "tests/integration/test_distribution_artifacts.py",
        ("hatch_build.py", "scripts/i18n_mo.py", "locales/**"),
    ),
    TestRule("tests/service/acp_client", ("tests/support/acp_stub_agent.py",)),
    # These install the example extension by copying its source, never importing it.
    TestRule("tests/service/extensions", ("tests/support/acme_release_extension.py",)),
    TestRule("tests/integration/engine/test_pipeline_extensions.py", ("tests/support/acme_release_extension.py",)),
    TestRule("tests/app/tui/screens/test_agent_config_extensions.py", ("tests/support/acme_release_extension.py",)),
    TestRule("tests/service/mcp/test_cache_refresh.py", ("tests/support/mcp_reload_probe_server.py",)),
    TestRule(
        "tests/service/mcp/test_connect_imports.py",
        (
            "src/chrys/service/mcp/**",
            "src/chrys/foundation/util/imports.py",
            "tests/support/mcp_reload_probe_server.py",
        ),
    ),
    TestRule("tests/app/tui/screens/test_reload_mcp.py", ("tests/support/mcp_reload_probe_server.py",)),
    TestRule("tests/service/mcp/test_stdio_cwd.py", ("tests/service/mcp/mcp_cwd_server.py",)),
    TestRule("tests/orchestration/engine/test_settings_reload.py", ("tests/support/mcp_reload_probe_server.py",)),
    TestRule("tests/orchestration/sub_agents/test_acp_engine.py", ("tests/support/acp_stub_agent.py",)),
    TestRule("tests/service/workflows/test_protocol.py", _WORKFLOW_WORKER_HOST),
    TestRule("tests/orchestration/workflows/test_worker_client.py", (*_WORKFLOW_WORKER_HOST, *_WORKFLOW_FAKE_WORKER)),
    TestRule("tests/orchestration/workflows/test_worker_capacity.py", (*_WORKFLOW_WORKER_HOST, *_WORKFLOW_FAKE_WORKER)),
    TestRule(
        "tests/orchestration/workflows/test_worker_lifecycle.py", (*_WORKFLOW_WORKER_HOST, *_WORKFLOW_FAKE_WORKER)
    ),
    TestRule("tests/orchestration/workflows/test_worker_semantics.py", _WORKFLOW_WORKER_HOST),
    TestRule("tests/orchestration/workflows/test_worker_stdout.py", _WORKFLOW_WORKER_HOST),
    TestRule("tests/orchestration/workflows/test_worker_values.py", _WORKFLOW_WORKER_HOST),
    # Previews, runs and the CLI start a real worker without importing the host;
    # many TUI tests only reach it through a shared helper, so they go by directory.
    TestRule("tests/orchestration/workflows", _WORKFLOW_WORKER_HOST),
    TestRule("tests/app/cli/test_workflow.py", _WORKFLOW_WORKER_HOST),
    TestRule("tests/app/cli/test_workflow_validate.py", _WORKFLOW_WORKER_HOST),
    TestRule("tests/app/tui/screens/main", _WORKFLOW_WORKER_HOST),
    TestRule("tests/app/tui/screens/test_workflow_confirm_dialog.py", _WORKFLOW_WORKER_HOST),
    TestRule("tests/app/tui/widgets/test_workflow_transcript_order.py", _WORKFLOW_WORKER_HOST),
    TestRule("tests/support/test_workflow_previews.py", _WORKFLOW_WORKER_HOST),
    TestRule("tests/service/workflows/test_py39_harness.py", _WORKFLOW_WORKER_HOST),
    TestRule("tests/service/agent_middleware/test_reminder_lifecycle.py", _REMINDER_PIPELINE),
    TestRule("tests/service/llm/test_client_contracts.py", _LLM_CLIENT_SOURCES),
    TestRule("tests/service/llm/test_persisted_names.py", ("src/chrys/service/llm/**",)),
    TestRule("tests/app/features/buddy", ("src/chrys/app/features/buddy/sprites/*.toml",)),
    # The install scripts run in a subprocess.
    TestRule("tests/app/test_install_scripts.py", ("scripts/install.sh", "scripts/install.ps1")),
    TestRule("tests/app/tui/behaviors/test_chrys_themes.py", ("src/chrys/app/tui/**",)),
    TestRule("tests/app/tui/i18n/test_bindings.py", ("src/chrys/app/tui/**",)),
    TestRule("tests/app/tui/screens/test_modal_insert_clipboard.py", ("src/chrys/app/tui/screens/**",)),
    TestRule(
        "tests/app/tui/widgets/editor/test_highlighter.py",
        (
            "src/chrys/foundation/patches/textual_*.py",
            "src/chrys/app/tui/widgets/editor/**",
            "src/chrys/app/tui/screens/dialogs/editor.py",
        ),
    ),
)

# Include every pytest configuration filename, even when absent from this tree:
# adding one can override pyproject.toml, and a root conftest affects all tests.
_FULL_TRIGGER_PATHS = frozenset(
    {
        ".pytest.ini",
        ".pytest.toml",
        ".python-version",
        "conftest.py",
        "pyproject.toml",
        "pytest.ini",
        "pytest.toml",
        "setup.cfg",
        "tests/conftest.py",
        "tox.ini",
        "uv.lock",
    }
)

_BUILTIN_PROFILE_TEST_TARGETS = (
    "tests/app/acp/test_session_manager_profiles.py",
    "tests/app/cli/test_workflow_validate.py",
    "tests/app/tui/behaviors/test_chrys_themes.py",
    "tests/app/tui/screens",
    "tests/orchestration/engine/build",
    "tests/service/profiles",
)

# Built-in workflow templates are loaded by path through discovery, never imported.
_BUILTIN_WORKFLOW_TEST_TARGETS = (
    "tests/app/cli/test_app.py",
    "tests/app/cli/test_workflow.py",
    "tests/app/tui/screens/main",
    "tests/app/tui/screens/test_workflow_confirm_dialog.py",
    "tests/app/tui/widgets/markdown/diagram/test_workflow_graph.py",
    "tests/app/tui/widgets/test_trajectory_workflow.py",
    "tests/orchestration/workflows",
    "tests/service/workflows",
    "tests/support/test_workflow_previews.py",
)

_TRAJECTORY_SOURCE_PREFIXES = (
    "src/chrys/foundation/",
    "src/chrys/kernel/",
    "src/chrys/service/",
    "src/chrys/orchestration/",
)


class SmartTestError(RuntimeError):
    """The local selector cannot safely determine or execute a test scope."""


@dataclass
class _ObservedFixtureDefinitions:
    """Observe definitions resolved by pytest's pinned closure traversal.

    pytest 9.1.1 owns override/parent lookup and parametrization semantics.
    This sequence adapter records the definitions it actually indexes, rather
    than treating every same-named fixture as active. The private SDK boundary
    is covered by subprocess tests with overriding and parametrized fixtures.
    """

    definitions: Sequence[Any]
    observed: list[Any] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.definitions)

    def __getitem__(self, index: int) -> Any:
        definition = self.definitions[index]
        self.observed.append(definition)
        return definition


def pytest_addoption(parser: pytest.Parser) -> None:
    """Private subprocess options; unrelated to the public CLI."""
    parser.addoption("--chrys-smart-collection", help="write smart-test fixture consumers after full collection")
    parser.addoption("--chrys-result-line", help="write pytest's final result line for the closing summary")


def pytest_terminal_summary(terminalreporter: pytest.TerminalReporter, config: pytest.Config) -> None:
    """Keep pytest's own result line ("1 failed, 9 passed, ...") for the closing summary."""
    destination = config.getoption("--chrys-result-line")
    # xdist workers get the same options; only the controller holds every result.
    if destination is None or hasattr(config, "workerinput"):
        return
    parts, _color = terminalreporter.build_summary_stats_line()
    Path(destination).write_text(", ".join(text for text, _markup in parts), encoding="utf-8")


def pytest_collection_finish(session: pytest.Session) -> None:
    """Export fixture identities after pytest has applied markers and overrides."""
    from _pytest.fixtures import traverse_fixture_closure

    destination = session.config.getoption("--chrys-smart-collection")
    if destination is None:
        return
    root = session.config.rootpath.resolve()
    records: list[dict[str, Any]] = []
    for item in session.items:
        observed: list[Any] = []
        info = getattr(item, "_fixtureinfo", None)  # pytest/custom collector boundary
        if info is not None:
            definitions = {
                name: _ObservedFixtureDefinitions(defs) for name, defs in info.name2fixturedefs.items() if defs
            }
            list(traverse_fixture_closure(info.initialnames, getfixturedefs=definitions.get))
            # Validate before filtering third-party fixtures: a test may
            # legitimately have no project fixtures, but every resolvable
            # name in pytest's pruned closure must have been observed.
            missing = sorted(
                name for name in info.names_closure if name in definitions and not definitions[name].observed
            )
            if missing:
                raise SmartTestError(
                    f"Incomplete fixture traversal for {item.nodeid}: no definitions observed for {', '.join(missing)}. "
                    "Check Smart Test's adapter against the installed pytest version."
                )
            observed = [definition for adapter in definitions.values() for definition in adapter.observed]
        fixtures: set[str] = set()
        for definition in observed:
            function = inspect.unwrap(definition.func)
            source = inspect.getsourcefile(function)
            if source is not None:
                try:
                    path = Path(source).resolve().relative_to(root).as_posix()
                except ValueError:
                    continue  # third-party fixtures are covered by dependency/config changes
                if not path.startswith(("tests/", "src/chrys/", "scripts/")):
                    continue  # a local .venv is also third-party code
                fixtures.add(f"{path}::{function.__name__}")
        records.append({"nodeid": item.nodeid, "fixtures": sorted(fixtures), "custom": info is None})
    Path(destination).write_text(json.dumps(records, ensure_ascii=True), encoding="utf-8")


def _collect_fixture_consumers(graph: ImportGraph, roots: Sequence[str] = ("tests",)) -> None:
    """Collect the candidate set below ``roots`` once, without setting up any fixture."""
    scope = "complete" if tuple(roots) == ("tests",) else ", ".join(roots)
    _write(f"Collecting the {scope} non-integration candidate set to resolve fixture consumers (no tests executed).")
    with tempfile.TemporaryDirectory(prefix="chrys-pytest-collection-") as directory:
        report = Path(directory) / "fixtures.json"
        result = _run_process(
            [
                sys.executable,
                "-m",
                "pytest",
                "--collect-only",
                "-q",
                "-n",
                "0",
                "-m",
                _EXCLUDED_MARKERS,
                "-p",
                "scripts.chrys_test",
                "--chrys-smart-collection",
                str(report),
                *roots,
            ],
            capture=True,
        )
        if result.returncode not in {0, _PYTEST_NO_TESTS_COLLECTED}:
            detail = (result.stdout + result.stderr).decode("utf-8", errors="backslashreplace")
            raise SmartTestError(f"Complete fixture collection failed (exit {result.returncode}):\n{detail}")
        if not report.exists():
            raise SmartTestError("Complete fixture collection produced no consumer report")
        try:
            _install_fixture_consumers(graph, json.loads(report.read_text(encoding="utf-8")))
        except (ValueError, KeyError, TypeError) as error:
            raise SmartTestError(f"Invalid fixture collection report: {error}") from error


def _install_fixture_consumers(graph: ImportGraph, records: list[dict[str, Any]]) -> None:
    graph.consumers.clear()
    graph.collected = tuple(record["nodeid"] for record in records)
    for record in records:
        nodeid = record["nodeid"]
        for fixture in record["fixtures"]:
            path = fixture.split("::", 1)[0]
            scope = graph.scopes.get(fixture)
            if scope is None:
                # Test-local fixtures retain the ordinary file import scope.
                scope = graph.path_to_module.get(path)
            if scope is None:
                scope = f"unknown:{fixture}"
                graph.uncertain[scope] = f"fixture implementation could not be resolved: {fixture}"
            graph.consumers[scope].add(nodeid)
        if record["custom"]:
            scope = f"unknown:{nodeid}"
            graph.uncertain[scope] = f"custom collector has no pytest fixture metadata: {nodeid}"
            graph.consumers[scope].add(nodeid)


def build_parser() -> argparse.ArgumentParser:
    """Build the intentionally small public command-line interface."""

    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--smart", action="store_true", help="run tests affected by local and branch changes")
    mode.add_argument("--full", action="store_true", help="run all local non-integration tests")
    parser.add_argument(
        "--paths",
        nargs="+",
        metavar="PATH",
        help="with --smart, analyze only the files changed by the current task",
    )
    return parser


def _lower_priority() -> None:
    """Run this process, and every child it starts, below normal CPU priority.

    Tests still use every idle core, but whatever else the user runs comes
    first, so there is no opt-out. Children inherit the priority: the pytest
    workers and the processes tests start run lower too. A process already
    running lower keeps its priority.
    """
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.GetPriorityClass.argtypes = (wintypes.HANDLE,)
        kernel32.GetPriorityClass.restype = wintypes.DWORD
        kernel32.SetPriorityClass.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        kernel32.SetPriorityClass.restype = wintypes.BOOL
        process = kernel32.GetCurrentProcess()
        current = kernel32.GetPriorityClass(process)
        if current in (subprocess.IDLE_PRIORITY_CLASS, subprocess.BELOW_NORMAL_PRIORITY_CLASS):
            lowered = True
        else:
            # A failed query (0) leaves the priority alone rather than risk raising it.
            lowered = current != 0 and bool(kernel32.SetPriorityClass(process, subprocess.BELOW_NORMAL_PRIORITY_CLASS))
    else:
        try:
            if os.getpriority(os.PRIO_PROCESS, 0) < _BELOW_NORMAL_NICENESS:
                os.setpriority(os.PRIO_PROCESS, 0, _BELOW_NORMAL_NICENESS)
            lowered = True
        except OSError:
            lowered = False
    if not lowered:
        _write("Could not lower the CPU priority; tests run at normal priority.")


def _run_process(args: list[str], *, capture: bool = False) -> subprocess.CompletedProcess[bytes]:
    """Run a non-interactive child from the repository root."""

    return subprocess.run(  # noqa: S603 - argv is constructed locally and never invokes a shell
        args,
        cwd=REPO_ROOT,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
        check=False,
    )


def _git_executable() -> str:
    git = shutil.which("git")
    if git is None:
        raise SmartTestError("Git is required for --smart but was not found on PATH")
    return git


def _git_output(*args: str, allow_failure: bool = False) -> bytes | None:
    command = [_git_executable(), *args]
    result = _run_process(command, capture=True)
    if result.returncode == 0:
        return result.stdout
    if allow_failure:
        return None
    detail = result.stderr.decode("utf-8", errors="backslashreplace").strip()
    raise SmartTestError(f"Git command failed ({' '.join(args)}): {detail or f'exit {result.returncode}'}")


def _decode_git_path(raw: bytes) -> str:
    """Decode a NUL-delimited Git path without losing POSIX surrogate bytes."""

    return os.fsdecode(raw)


def _validate_relative_path(path: str) -> str:
    candidate = Path(path)
    if path in {"", "."} or candidate.is_absolute() or ".." in candidate.parts:
        raise SmartTestError(f"Git returned a path outside the repository: {_display(path)}")
    return candidate.as_posix()


def _parse_name_status(payload: bytes) -> list[tuple[str, str]]:
    """Parse ``git diff --name-status -z`` into ``(path, state)`` pairs."""

    fields = payload.split(b"\0")
    if fields and fields[-1] == b"":
        fields.pop()
    parsed: list[tuple[str, str]] = []
    index = 0
    while index < len(fields):
        status = fields[index].decode("ascii", errors="strict")
        index += 1
        if not status:
            raise SmartTestError("Git returned an empty change status")
        kind = status[0]
        needed = 2 if kind in {"C", "R"} else 1
        if index + needed > len(fields):
            raise SmartTestError(f"Git returned a truncated {status!r} change record")
        paths = [_validate_relative_path(_decode_git_path(field)) for field in fields[index : index + needed]]
        index += needed
        if kind in {"C", "R"}:
            parsed.append((paths[0], "renamed"))
            parsed.append((paths[1], "renamed"))
        elif kind == "D":
            parsed.append((paths[0], "deleted"))
        else:
            parsed.append((paths[0], "changed"))
    return parsed


def _resolve_default_branch() -> str | None:
    symbolic = _git_output("symbolic-ref", "--quiet", "refs/remotes/origin/HEAD", allow_failure=True)
    if symbolic:
        reference = os.fsdecode(symbolic).strip()
        if reference and _git_output("rev-parse", "--verify", "--quiet", reference, allow_failure=True):
            return reference
    origin_main = "refs/remotes/origin/main"
    if _git_output("rev-parse", "--verify", "--quiet", origin_main, allow_failure=True):
        return origin_main
    upstream = _git_output(
        "for-each-ref",
        "--format=%(upstream)",
        "refs/heads/main",
        allow_failure=True,
    )
    if upstream:
        reference = os.fsdecode(upstream).strip()
        if reference and _git_output("rev-parse", "--verify", "--quiet", reference, allow_failure=True):
            return reference
    local_main = "refs/heads/main"
    if _git_output("rev-parse", "--verify", "--quiet", local_main, allow_failure=True):
        return local_main
    return None


def _local_main_is_head() -> bool:
    symbolic = _git_output("symbolic-ref", "--quiet", "HEAD", allow_failure=True)
    if symbolic:
        return os.fsdecode(symbolic).strip() == "refs/heads/main"
    head = _git_output("rev-parse", "--verify", "HEAD")
    local_main = _git_output("rev-parse", "--verify", "refs/heads/main")
    return head == local_main


def discover_changes() -> tuple[Change, ...]:
    """Return branch, staged, unstaged, and untracked changes as one set."""

    if _git_output("rev-parse", "--is-inside-work-tree", allow_failure=True) != b"true\n":
        raise SmartTestError(f"{REPO_ROOT} is not a Git working tree")

    observed: dict[str, set[str]] = defaultdict(set)
    default_branch = _resolve_default_branch()
    if default_branch is None:
        raise SmartTestError("Could not resolve the repository default branch; use --smart --paths instead")
    if default_branch == "refs/heads/main" and _local_main_is_head():
        raise SmartTestError(
            "Local main is the only available baseline and points at HEAD, so committed local changes cannot be "
            "inferred; fetch its remote upstream or use --smart --paths instead"
        )
    merge_base = _git_output("merge-base", "HEAD", default_branch)
    if merge_base is None:  # pragma: no cover - a failed required Git command raises above
        raise SmartTestError(f"Could not find a merge base between HEAD and {default_branch}")
    base = merge_base.decode("ascii", errors="strict").strip()
    branch_diff = _git_output("diff", "--name-status", "-z", "--find-renames", f"{base}...HEAD") or b""
    for path, state in _parse_name_status(branch_diff):
        observed[path].add(state)

    for arguments in (
        ("diff", "--name-status", "-z", "--find-renames"),
        ("diff", "--cached", "--name-status", "-z", "--find-renames"),
    ):
        for path, state in _parse_name_status(_git_output(*arguments) or b""):
            observed[path].add(state)

    untracked = (_git_output("ls-files", "--others", "--exclude-standard", "-z") or b"").split(b"\0")
    for raw_path in untracked:
        if raw_path:
            observed[_validate_relative_path(_decode_git_path(raw_path))].add("changed")

    return tuple(Change(path, frozenset(states)) for path, states in sorted(observed.items()))


def changes_from_paths(paths: list[str]) -> tuple[Change, ...]:
    """Normalize an explicit task-local path list without consulting Git."""

    normalized: set[str] = set()
    for raw_path in paths:
        candidate = Path(raw_path)
        if candidate.is_absolute():
            try:
                candidate = candidate.relative_to(REPO_ROOT)
            except ValueError as error:
                raise SmartTestError(f"Path is outside the repository: {_display(raw_path)}") from error
        normalized.add(_validate_relative_path(candidate.as_posix()))
    return tuple(
        Change(path, frozenset({"changed" if (REPO_ROOT / path).exists() else "deleted"}))
        for path in sorted(normalized)
    )


def _module_for_path(relative_path: str) -> str | None:
    path = Path(relative_path)
    parts = path.parts
    if len(parts) >= 3 and parts[:2] == ("src", "chrys"):
        module_parts = list(parts[1:])
    elif len(parts) >= 2 and parts[0] in {"scripts", "tests"}:
        module_parts = list(parts)
    else:
        return None
    if path.suffix != ".py":
        return None
    if module_parts[-1] == "__init__.py":
        module_parts.pop()
    else:
        module_parts[-1] = Path(module_parts[-1]).stem
    return ".".join(module_parts)


def _python_paths(root: Path) -> tuple[Path, ...]:
    paths: list[Path] = []
    for relative_root in (Path("src/chrys"), Path("tests"), Path("scripts")):
        directory = root / relative_root
        if directory.is_dir():
            paths.extend(path for path in directory.rglob("*.py") if "__pycache__" not in path.parts)
    return tuple(sorted(paths))


def _relative_import(module: str, *, package: bool, level: int, imported: str | None) -> str | None:
    package_parts = module.split(".") if package else module.split(".")[:-1]
    ascend = level - 1
    if ascend > len(package_parts):
        return None
    base_parts = package_parts[: len(package_parts) - ascend]
    if imported:
        base_parts.extend(imported.split("."))
    return ".".join(base_parts)


def _longest_known_reference(reference: str, known_modules: set[str]) -> str | None:
    """Return the most specific current-tree module prefix for a reference."""

    parts = reference.split(".")
    for size in range(len(parts), 0, -1):
        candidate = ".".join(parts[:size])
        if candidate in known_modules:
            return candidate
    return None


def _known_references(reference: str, known_modules: set[str]) -> set[str]:
    """Resolve a possibly member-qualified reference to known module prefixes."""

    longest = _longest_known_reference(reference, known_modules)
    if longest is None:
        return set()
    resolved = {longest}
    longest_parts = longest.split(".")
    for size in range(1, len(longest_parts)):
        parent = ".".join(longest_parts[:size])
        if parent in known_modules:
            resolved.add(parent)
    return resolved


def _static_facade_exports(tree: ast.Module, module: str, *, package: bool) -> dict[str, str] | None:
    """Recognize import-only facades without guessing dynamic initialization."""
    exports: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            continue
        if isinstance(node, ast.ImportFrom):
            if node.module == "__future__":
                continue
            base = (
                _relative_import(module, package=package, level=node.level, imported=node.module)
                if node.level
                else node.module
            )
            if not base or any(alias.name == "*" for alias in node.names):
                return None
            for alias in node.names:
                exports[alias.asname or alias.name] = f"{base}.{alias.name}"
        elif isinstance(node, ast.Import):
            for alias in node.names:
                exports[alias.asname or alias.name.split(".")[0]] = (
                    alias.name if alias.asname else alias.name.split(".")[0]
                )
        elif isinstance(node, ast.Assign | ast.AnnAssign) and _assigned_names(node) == {"__all__"}:
            try:
                ast.literal_eval(node.value)
            except ValueError, TypeError:
                return None
        else:
            return None
    return exports


def _dependency_references(reference: str, known: set[str], facades: dict[str, dict[str, str]]) -> set[str]:
    """Follow named re-exports; keep namespace imports and dynamic facades broad.

    Import-only package initialization is a surface dependency, not a runtime
    dependency on every sibling implementation that the package re-exports.
    Direct edits to that surface still select all its importers.
    """
    dependencies: set[str] = set()
    visited_routes: set[tuple[str, str]] = set()
    while True:
        provider = _longest_known_reference(reference, known)
        if provider is None:
            break
        parent, _, member = provider.rpartition(".")
        exported_member = facades.get(parent, {}).get(member)
        if exported_member is not None and exported_member != provider:
            # A named export can shadow a same-named submodule. Keep the
            # facade as well as the submodule when the route is ambiguous.
            dependencies.add(parent)
        for ancestor in _known_references(provider, known) - {provider}:
            dependencies.add(f"{ancestor}::<exports>" if ancestor in facades else ancestor)
        suffix = reference.removeprefix(provider).lstrip(".")
        name, _, tail = suffix.partition(".")
        exported = facades.get(provider, {}).get(name)
        if exported is None:
            dependencies.add(provider)
            break
        route = (provider, name)
        if route in visited_routes:
            # Missing providers can grow a reference on every expansion, so
            # repeated strings do not reliably identify a re-export cycle.
            dependencies.add(provider)
            break
        visited_routes.add(route)
        dependencies.add(f"{provider}::<exports>")
        reference = exported + (f".{tail}" if tail else "")
    return dependencies


def _module_dependencies(
    tree: ast.Module,
    *,
    module: str,
    package: bool,
    known_modules: set[str],
    facades: dict[str, dict[str, str]] | None = None,
    routed: frozenset[int] = frozenset(),
) -> set[str]:
    """Module-level dependencies; ``routed`` import nodes were already resolved to symbol scopes."""
    facades = facades or {}
    references: set[str] = set()
    package_name = module if package else module.rpartition(".")[0]

    def add(reference: str | None) -> None:
        if reference:
            references.update(_dependency_references(reference, known_modules, facades))

    for node in _runtime_nodes(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                add(alias.name)
        elif isinstance(node, ast.ImportFrom) and id(node) not in routed:
            base = (
                _relative_import(module, package=package, level=node.level, imported=node.module)
                if node.level
                else node.module
            )
            if base not in facades or any(alias.name == "*" for alias in node.names):
                add(base)
            if base:
                for alias in node.names:
                    if alias.name != "*":
                        add(f"{base}.{alias.name}")
    for value in _string_module_references(tree):
        if value.startswith("."):
            add(
                _relative_import(
                    module, package=package, level=len(value) - len(value.lstrip(".")), imported=value.lstrip(".")
                )
            )
        else:
            add(value)
            if package_name:
                add(f"{package_name}.{value}")

    # Direct initializer edits still reach children. Pure re-exports do not
    # turn a runtime change in one child into a dependency of every sibling.
    parts = module.split(".")
    for size in range(1, len(parts)):
        parent = ".".join(parts[:size])
        if parent in facades:
            references.add(f"{parent}::<exports>")
        else:
            add(parent)
    references.discard(module)
    return references


def _dotted_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{_dotted_name(node.value)}.{node.attr}"
    return ""


def _assigned_names(node: ast.AST) -> set[str]:
    if isinstance(node, ast.Assign):
        return {_dotted_name(target) for target in node.targets}
    if isinstance(node, ast.AnnAssign):
        return {_dotted_name(node.target)}
    return set()


def _plugin_values(tree: ast.Module) -> list[ast.expr]:
    return [
        node.value
        for node in _all_nodes(tree)
        if isinstance(node, ast.Assign | ast.AnnAssign)
        and node.value is not None
        and "pytest_plugins" in _assigned_names(node)
    ]


def _string_module_references(tree: ast.AST) -> set[str]:
    """Recognize import, patch, plugin, facade and Python subprocess module references."""
    references: set[str] = set()
    aliases = {
        alias.asname or alias.name: alias.name
        for node in _runtime_nodes(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }
    for node in _runtime_nodes(tree):
        if isinstance(node, ast.List | ast.Tuple) and (module := _python_module_argument(node.elts, aliases)):
            references.add(module)
        if isinstance(node, ast.Call):
            name = _dotted_name(node.func).rsplit(".", maxsplit=1)[-1]
            name = aliases.get(name, name)
            if name == "create_subprocess_exec" and (module := _python_module_argument(node.args, aliases)):
                references.add(module)
            if name in {"import_module", "__import__", "importorskip", "patch", "setattr", "delattr"}:
                arguments = list(node.args[:1]) + [kw.value for kw in node.keywords if kw.arg in {"name", "target"}]
                references.update(
                    arg.value for arg in arguments if isinstance(arg, ast.Constant) and isinstance(arg.value, str)
                )
        elif isinstance(node, ast.Assign | ast.AnnAssign) and node.value is not None:
            names = _assigned_names(node)
            if "pytest_plugins" in names:
                references.update(
                    n.value for n in ast.walk(node.value) if isinstance(n, ast.Constant) and isinstance(n.value, str)
                )
            elif "_EXPORTS" in names and isinstance(node.value, ast.Dict):
                references.update(
                    n.value for n in node.value.values if isinstance(n, ast.Constant) and isinstance(n.value, str)
                )
    return references


def _python_module_argument(arguments: list[ast.AST], aliases: dict[str, str]) -> str | None:
    """Read literal module targets from Python argv, including assigned argv lists.

    Stop at a script or -c: subsequent -m arguments belong to that program.
    Recognizing an interpreter first also excludes git commit messages and
    ordinary dotted strings from the dependency graph.
    """
    if not arguments:
        return None
    executable = arguments[0]
    name = _dotted_name(executable).rsplit(".", 1)[-1]
    is_python = aliases.get(name, name) == "executable"
    if isinstance(executable, ast.Constant) and isinstance(executable.value, str):
        filename = executable.value.replace("\\", "/").rsplit("/", 1)[-1]
        is_python = re.fullmatch(r"(?:python(?:\d+(?:\.\d+)*)?|py)(?:\.exe)?", filename, re.IGNORECASE) is not None
    if not is_python:
        return None
    index = 1
    while index < len(arguments):
        argument = arguments[index]
        if not isinstance(argument, ast.Constant) or not isinstance(argument.value, str):
            return None
        value = argument.value
        if value == "-m":
            if index + 1 < len(arguments):
                module = arguments[index + 1]
                if isinstance(module, ast.Constant) and isinstance(module.value, str):
                    return module.value
            return None
        if value in {"-c", "-", "--"} or value.startswith("-c") or not value.startswith("-"):
            return None
        index += 2 if value in {"-W", "-X", "--check-hash-based-pycs"} else 1
    return None


# One graph build walks each module tree many times; it registers the trees it
# parsed here for the build's duration. Entries keep their tree alive, so an id
# is never reused while cached.
_WALK_CACHE: dict[int, tuple[ast.AST, list[ast.AST], list[ast.AST]]] = {}


def _all_nodes(tree: ast.AST) -> list[ast.AST]:
    cached = _WALK_CACHE.get(id(tree))
    return cached[2] if cached is not None and cached[0] is tree else list(ast.walk(tree))


def _runtime_nodes(tree: ast.AST) -> Iterator[ast.AST]:
    cached = _WALK_CACHE.get(id(tree))
    if cached is not None and cached[0] is tree:
        return iter(cached[1])
    return _walk_runtime_nodes(tree)


def _side_effect_import(node: ast.Import | ast.ImportFrom, tree: ast.Module) -> bool:
    """A production or third-party import binding a name the module never reads.

    Unused imports from test modules are fixture or hook re-exports, which
    collection and the scoped-provider edges already follow.
    """
    if isinstance(node, ast.ImportFrom) and (node.level or node.module in {None, "__future__"}):
        return False
    modules = [alias.name for alias in node.names] if isinstance(node, ast.Import) else [node.module or ""]
    if any(name == "tests" or name.startswith("tests.") for name in modules):
        return False
    names = {n.id for n in _all_nodes(tree) if isinstance(n, ast.Name)}
    if isinstance(node, ast.ImportFrom):
        return any((alias.asname or alias.name) not in names for alias in node.names)
    # ``import a.b`` binds ``a``; it is read only through ``a.b`` itself, not
    # through another ``import a.c`` that shares the top-level name.
    dotted = {_dotted_name(n) for n in _all_nodes(tree) if isinstance(n, ast.Attribute | ast.Name)}
    return any(
        alias.asname not in names
        if alias.asname
        else not any(d == alias.name or d.startswith(f"{alias.name}.") for d in dotted)
        for alias in node.names
    )


def _index_tree(tree: ast.AST) -> tuple[list[ast.AST], list[ast.AST]]:
    """Return ``(runtime nodes, all nodes)`` from one traversal.

    Runtime nodes keep ``_walk_runtime_nodes``' exact order: alias resolution
    such as ``_string_module_references`` lets a later binding win. All nodes
    are only filtered, so their order is unspecified.
    """
    runtime: list[ast.AST] = []
    everything: list[ast.AST] = []
    pending: list[tuple[ast.AST, bool]] = [(tree, True)]
    while pending:
        node, live = pending.pop()
        everything.append(node)
        if live:
            runtime.append(node)
        if live and isinstance(node, ast.If) and _dotted_name(node.test) in {"TYPE_CHECKING", "typing.TYPE_CHECKING"}:
            children = [(child, False) for child in (node.test, *node.body)]
            children.extend((child, True) for child in node.orelse)
        else:
            children = [(child, live) for child in ast.iter_child_nodes(node)]
        pending.extend(reversed(children))
    return runtime, everything


def _walk_runtime_nodes(tree: ast.AST) -> Iterator[ast.AST]:
    """Skip conventional type-only branches; type checking remains a separate gate."""
    pending = [tree]
    while pending:
        node = pending.pop()
        yield node
        if isinstance(node, ast.If) and _dotted_name(node.test) in {"TYPE_CHECKING", "typing.TYPE_CHECKING"}:
            pending.extend(reversed(node.orelse))
        else:
            pending.extend(reversed(list(ast.iter_child_nodes(node))))


def _module_scope_nodes(tree: ast.AST) -> Iterator[ast.AST]:
    """Walk module control-flow blocks without crossing a function or class scope."""
    pending = [tree]
    while pending:
        node = pending.pop()
        yield node
        if isinstance(node, ast.If) and _dotted_name(node.test) in {"TYPE_CHECKING", "typing.TYPE_CHECKING"}:
            pending.extend(reversed(node.orelse))
        elif not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef | ast.Lambda):
            pending.extend(reversed(list(ast.iter_child_nodes(node))))


def _definition_nodes(tree: ast.Module) -> dict[str, list[ast.AST]]:
    """Keep all branch alternatives for a module binding; do not guess the host branch."""
    definitions: dict[str, list[ast.AST]] = defaultdict(list)
    for node in _module_scope_nodes(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            definitions[node.name].append(node)
        elif isinstance(node, ast.Assign | ast.AnnAssign):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name):
                    definitions[target.id].append(node)
    return definitions


def _initialization_nodes(nodes: list[ast.stmt], *, members: frozenset[str] = frozenset()) -> list[ast.AST]:
    """Separate executed expressions from deferred bodies and annotations.

    Imports execute their providers' initialization, so they remain global
    dependencies. Fixture providers can defer expensive imports to fixture
    bodies; TYPE_CHECKING imports do not execute. Full collection separately
    catches import errors even in candidates not selected for execution.
    """
    result: list[ast.AST] = []
    for node in nodes:
        if isinstance(node, ast.Import | ast.ImportFrom):
            result.append(node)
            continue
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            result.extend(node.decorator_list)
            result.extend(node.args.defaults)
            result.extend(n for n in node.args.kw_defaults if n is not None)
            # Unknown decorators may invoke the function while registering it;
            # fixtures, hooks and descriptors only wrap it.
            if not all(_inert_decorator(d, members) for d in node.decorator_list):
                result.append(node)
        elif isinstance(node, ast.ClassDef):
            result.extend(node.bases)
            result.extend(node.decorator_list)
            result.extend(kw.value for kw in node.keywords)
            result.extend(_initialization_nodes(node.body, members=_member_names(node)))
            if any(
                _dotted_name(d.func if isinstance(d, ast.Call) else d) not in _INERT_CLASS_DECORATORS
                for d in node.decorator_list
            ):
                result.append(node)  # an unknown decorator can invoke class methods during import
        elif "pytest_plugins" in _assigned_names(node):
            continue
        elif isinstance(node, ast.AnnAssign):
            if node.value is not None:
                result.append(node.value)
        elif isinstance(node, ast.If):
            if _dotted_name(node.test) in {"TYPE_CHECKING", "typing.TYPE_CHECKING"}:
                result.extend(_initialization_nodes(node.orelse))
            else:
                result.append(node.test)
                result.extend(_initialization_nodes(node.body))
                result.extend(_initialization_nodes(node.orelse))
        elif isinstance(node, ast.Try | ast.TryStar):
            result.extend(_initialization_nodes(node.body + node.orelse + node.finalbody))
            for handler in node.handlers:
                if handler.type is not None:
                    result.append(handler.type)
                result.extend(_initialization_nodes(handler.body))
        elif isinstance(node, ast.With | ast.AsyncWith):
            result.extend(item.context_expr for item in node.items)
            result.extend(_initialization_nodes(node.body))
        elif isinstance(node, ast.For | ast.AsyncFor | ast.While):
            result.append(node.test if isinstance(node, ast.While) else node.iter)
            result.extend(_initialization_nodes(node.body + node.orelse))
        elif isinstance(node, ast.Match):
            result.append(node.subject)
            for case in node.cases:
                result.append(case.pattern)
                if case.guard is not None:
                    result.append(case.guard)
                result.extend(_initialization_nodes(case.body))
        else:
            result.append(node)
    return result


def _scope_dependencies(
    nodes: list[ast.AST],
    *,
    module: str,
    tree: ast.Module,
    known: set[str],
    symbols: dict[str, str],
    package: bool = False,
    facades: dict[str, dict[str, str]] | None = None,
) -> tuple[set[str], str | None]:
    bindings = {name: {symbols[f"{module}.{name}"]} for name in _definition_nodes(tree)}
    # Module imports supply bindings; importing a fixture's provider alone is
    # not evidence that a hook or a different fixture calls that provider.
    for node in _module_scope_nodes(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                bindings.setdefault(alias.asname or alias.name.split(".")[0], set()).add(
                    alias.name if alias.asname else alias.name.split(".")[0]
                )
        elif isinstance(node, ast.ImportFrom):
            base = (
                _relative_import(module, package=package, level=node.level, imported=node.module)
                if node.level
                else node.module
            )
            for alias in node.names:
                if base:
                    bindings.setdefault(alias.asname or alias.name, set()).add(f"{base}.{alias.name}")
    dependencies: set[str] = set()
    uncertain: str | None = None
    body = ast.Module(body=nodes)
    # Imports inside a body are real dependencies even if the imported name
    # is subsequently passed indirectly through a callback or container.
    dependencies.update(
        _module_dependencies(body, module=module, package=package, known_modules=known, facades=facades)
    )
    for node in ast.walk(body):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            continue
        name = _dotted_name(node)
        head, _, tail = name.partition(".")
        for binding in bindings.get(head, ()):
            reference = binding + (f".{tail}" if tail else "")
            if "::" in reference:
                dependencies.add(binding)
            elif reference in symbols:
                dependencies.add(symbols[reference])
            else:
                dependencies.update(_dependency_references(reference, known, facades or {}))
        if isinstance(node, ast.Call):
            called = _dotted_name(node.func).rsplit(".", 1)[-1]
            if called in {"getfixturevalue", "globals", "locals", "eval", "exec"}:
                uncertain = f"unresolved dynamic fixture/helper call {called} in {module}"
            elif called in {"import_module", "__import__"} and (
                not node.args or not isinstance(node.args[0], ast.Constant)
            ):
                uncertain = f"unresolved dynamic import in {module}"
            elif called == "getattr" and node.args and isinstance(node.args[0], ast.Name):
                owners = bindings.get(node.args[0].id, ())
                if any(owner.startswith(("chrys.", "tests.")) for owner in owners):
                    uncertain = f"unresolved helper attribute in {module}"
    return dependencies, uncertain


def _import_bound_names(tree: ast.Module) -> set[str]:
    return {
        alias.asname or alias.name.split(".", 1)[0]
        for node in _module_scope_nodes(tree)
        if isinstance(node, ast.Import | ast.ImportFrom)
        for alias in node.names
    }


def _symbol_import_routes(
    tree: ast.Module, *, module: str, package: bool, scoped: set[str], symbols: dict[str, str]
) -> tuple[frozenset[int], set[str]]:
    """Resolve named imports from test helper modules to the imported definitions.

    ``from tests.support.x import helper`` depends on ``helper`` and on the
    helper module's import-time behavior, not on every other helper beside it.
    Namespace, wildcard and re-exported (not locally defined) names stay
    module-wide.
    """
    routed: set[int] = set()
    dependencies: set[str] = set()
    for node in _runtime_nodes(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        base = (
            _relative_import(module, package=package, level=node.level, imported=node.module)
            if node.level
            else node.module
        )
        if base not in scoped or any(alias.name == "*" for alias in node.names):
            continue
        keys = [symbols.get(f"{base}.{alias.name}") for alias in node.names]
        if any(key is None for key in keys):
            continue
        routed.add(id(node))
        dependencies.update(key for key in keys if key is not None)
        dependencies.add(f"{base}::<global>")
    return frozenset(routed), dependencies


def build_import_graph(root: Path = REPO_ROOT) -> ImportGraph:
    """Build a conservative current-tree reverse module dependency graph."""
    try:
        return _build_import_graph(root)
    finally:
        _WALK_CACHE.clear()


def _build_import_graph(root: Path) -> ImportGraph:

    path_to_module: dict[str, str] = {}
    module_to_path: dict[str, str] = {}
    absolute_paths: dict[str, Path] = {}
    for path in _python_paths(root):
        relative = path.relative_to(root).as_posix()
        module = _module_for_path(relative)
        if module is None:
            continue
        path_to_module[relative] = module
        module_to_path[module] = relative
        absolute_paths[module] = path

    known_modules = set(module_to_path)
    dependencies: dict[str, set[str]] = defaultdict(set)
    parse_errors: list[str] = []
    trees: dict[str, ast.Module] = {}
    for module, path in absolute_paths.items():
        try:
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=path.as_posix())
        except (OSError, SyntaxError, UnicodeError) as error:
            parse_errors.append(f"{path.relative_to(root).as_posix()}: {error}")
            continue
        trees[module] = tree
        _WALK_CACHE[id(tree)] = (tree, *_index_tree(tree))

    facades = {
        module: exports
        for module, tree in trees.items()
        if module.startswith("chrys")
        and (exports := _static_facade_exports(tree, module, package=absolute_paths[module].name == "__init__.py"))
        is not None
    }
    # Keep ordinary explicit imports conservative. Only pytest's implicit
    # loading edges and named helper imports use scopes instead of modules.
    conftests = {
        path: module
        for path, module in path_to_module.items()
        if path == "tests/conftest.py" or path.endswith("/conftest.py")
    }
    plugins: dict[str, set[str]] = defaultdict(set)
    for module, tree in trees.items():
        for value in _plugin_values(tree):
            plugins[module].update(
                n.value
                for n in ast.walk(value)
                if isinstance(n, ast.Constant) and isinstance(n.value, str) and n.value in known_modules
            )
    scoped_modules = set(conftests.values()) | {p for ps in plugins.values() for p in ps}
    scoped_modules.update(m for m, p in module_to_path.items() if p.startswith("tests/support/"))
    symbols = {
        f"{module}.{name}": f"{module}::{name}"
        for module in scoped_modules
        if module in trees
        for name in _definition_nodes(trees[module])
    }
    # A name a helper module both defines and imports (say, per platform) may be
    # the imported implementation at run time: keep its importers module-wide.
    routable = {
        reference: key
        for reference, key in symbols.items()
        if reference.rsplit(".", 1)[1] not in _import_bound_names(trees[key.split("::", 1)[0]])
    }
    for module, tree in trees.items():
        package = absolute_paths[module].name == "__init__.py"
        routed: frozenset[int] = frozenset()
        if module.startswith("tests.") and module not in scoped_modules:
            routed, routes = _symbol_import_routes(
                tree, module=module, package=package, scoped=scoped_modules, symbols=routable
            )
            dependencies[module].update(routes)
        dependencies[module].update(
            _module_dependencies(
                tree, module=module, package=package, known_modules=known_modules, facades=facades, routed=routed
            )
        )
    scopes: dict[str, str] = {}
    uncertain: dict[str, str] = {}
    for module in sorted(scoped_modules & trees.keys()):
        tree = trees[module]
        package = module_to_path[module].endswith("/__init__.py")
        for name, definition_nodes in _definition_nodes(tree).items():
            key = symbols[f"{module}.{name}"]
            scopes[f"{module_to_path[module]}::{name}"] = key
            deps, unknown = _scope_dependencies(
                definition_nodes,
                module=module,
                tree=tree,
                known=known_modules,
                symbols=symbols,
                package=package,
                facades=facades,
            )
            dependencies[key].update(deps - {key})
            dependencies[module].add(key)
            if unknown:
                uncertain[key] = unknown
        global_key = f"{module}::<global>"
        # Executed initialization and hooks keep their dependencies through the
        # names they use. An import whose name the module uses is reached that
        # way (or only inside fixture and helper bodies, which their own scopes
        # cover); only an import nothing uses is kept global, since it exists
        # for its import-time side effect (a registry, a patch).
        nodes = [
            n
            for n in _initialization_nodes(tree.body)
            if not isinstance(n, ast.Import | ast.ImportFrom) or _side_effect_import(n, tree)
        ]
        nodes.extend(
            n
            for name, definition_nodes in _definition_nodes(tree).items()
            for n in definition_nodes
            if name.startswith("pytest_") and isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)
        )
        deps, unknown = _scope_dependencies(
            nodes, module=module, tree=tree, known=known_modules, symbols=symbols, package=package, facades=facades
        )
        dependencies[global_key].update(deps)
        dependencies[global_key].update(f"{p}::<global>" for p in plugins[module])
        # Preserve imported helpers' implicit pytest behavior as well as the
        # conservative explicit import dependencies above.
        imports = ast.Module(body=[n for n in tree.body if isinstance(n, ast.Import | ast.ImportFrom)])
        dependencies[global_key].update(
            f"{provider}::<global>"
            for provider in _module_dependencies(
                imports, module=module, package=package, known_modules=known_modules, facades=facades
            )
            if provider in scoped_modules and provider != module
        )
        if unknown:
            # An unresolvable global operation may affect every loaded test.
            uncertain[global_key] = unknown
        for node in _all_nodes(tree):
            if isinstance(node, ast.ImportFrom) and any(alias.name == "*" for alias in node.names):
                uncertain[global_key] = f"unresolved wildcard helper import in {module}"
        for declaration in _plugin_values(tree):
            try:
                value = ast.literal_eval(declaration)
            except ValueError:
                uncertain[global_key] = f"unresolved dynamic pytest_plugins registration in {module}"
            else:
                if not isinstance(value, str | tuple | list):
                    uncertain[global_key] = f"unsupported pytest_plugins registration in {module}"
    for module, tree in trees.items():
        if (
            module not in scoped_modules
            and module_to_path[module].startswith("tests/")
            and any(
                isinstance(n, ast.Call) and _dotted_name(n.func).endswith("getfixturevalue") for n in _all_nodes(tree)
            )
        ):
            uncertain[module] = f"unresolved dynamic fixture request in {module}"
    for path, module in path_to_module.items():
        if not _is_test_file(path):
            continue
        parent = Path(path).parent
        while parent == Path("tests") or Path("tests") in parent.parents:
            conftest_path = (parent / "conftest.py").as_posix()
            conftest_module = conftests.get(conftest_path)
            if conftest_module is not None:
                dependencies[module].add(f"{conftest_module}::<global>")
            if parent == Path("tests"):
                break
            parent = parent.parent

    reverse: dict[str, set[str]] = defaultdict(set)
    for owner, imported_modules in dependencies.items():
        for imported in imported_modules:
            reverse[imported].add(owner)
    fixture_scopes = {
        symbols[f"{module}.{node.name}"]
        for module in scoped_modules & trees.keys()
        for node in _module_scope_nodes(trees[module])
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and _is_fixture(node)
    }
    fixture_modules = (
        _fixture_providing_modules(
            {module: tree for module, tree in trees.items() if module.startswith("tests.")},
            packages={module for module, path in absolute_paths.items() if path.name == "__init__.py"},
        )
        - scoped_modules
    )
    return ImportGraph(
        path_to_module,
        module_to_path,
        dict(reverse),
        tuple(parse_errors),
        scopes,
        uncertain,
        facades=facades,
        fixture_scopes=frozenset(fixture_scopes),
        fixture_modules=frozenset(fixture_modules),
    )


def _fixture_providing_modules(trees: dict[str, ast.Module], *, packages: set[str]) -> set[str]:
    """Test modules that define a fixture or re-export one from a module that provides it.

    Importing a fixture makes the importer a consumer, so a chain of re-exports
    must stay as free as the defining module's own import. Conftest and helper
    fixtures seed the chain too; callers drop those scoped modules themselves.
    """
    names: dict[str, set[str]] = {
        module: {
            node.name
            for node in _all_nodes(tree)
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and _is_fixture(node)
        }
        for module, tree in trees.items()
    }
    changed = True
    while changed:
        changed = False
        for module, tree in trees.items():
            for node in _runtime_nodes(tree):
                if not isinstance(node, ast.ImportFrom):
                    continue
                base = (
                    _relative_import(module, package=module in packages, level=node.level, imported=node.module)
                    if node.level
                    else node.module
                )
                provided = names.get(base or "")
                if not provided:
                    continue
                for alias in node.names:
                    if alias.name == "*":
                        exported = provided
                    elif alias.name in provided:
                        exported = {alias.asname or alias.name}
                    else:
                        continue
                    if not exported <= names[module]:
                        names[module] |= exported
                        changed = True
    return {module for module, provided in names.items() if provided}


def _is_fixture(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    return any(
        _dotted_name(d.func if isinstance(d, ast.Call) else d).rsplit(".", 1)[-1] == "fixture"
        for d in node.decorator_list
    )


def fixture_collection_roots(graph: ImportGraph, selection: Selection) -> tuple[str, ...]:
    """Directories whose pytest collection can reveal consumers of the fixtures a change reaches.

    Consumers hang off fixture scopes, so reaching a conftest's globals or helpers alone adds none.
    A nested conftest's fixtures are visible only below its directory; plugin and helper fixtures
    anywhere. A fixture is also visible wherever a conftest imports it, so each reached provider's
    importers widen the roots: a conftest adds its directory (and is followed further), a plain
    test module already runs whole, and any other importer may be a plugin. An empty result skips
    collection, and with it the import check of unselected tests, which CI still performs.
    """
    if graph.uncertain:
        return ("tests",)
    roots: set[str] = set()
    pending = deque(sorted((graph.fixture_scopes | graph.fixture_modules) & selection.reached))
    visited = set(pending)
    while pending:
        node = pending.popleft()
        module = node.split("::", 1)[0]
        path = graph.module_to_path.get(module, "")
        if path.endswith("/conftest.py"):
            roots.add(Path(path).parent.as_posix())
        elif node in graph.fixture_scopes:
            return ("tests",)  # a plugin or helper fixture is visible everywhere
        # Scoped modules import each other by module, not by symbol.
        for owner in sorted(graph.reverse.get(node, set()) | graph.reverse.get(module, set())):
            importer = owner.split("::", 1)[0]
            importer_path = graph.module_to_path.get(importer, "")
            if importer == module or owner in visited:
                continue
            if importer_path.endswith("/conftest.py") or _is_test_file(importer_path):
                # A test module runs whole, but may pass the fixture on to a conftest.
                visited.add(owner)
                pending.append(owner)
            else:
                return ("tests",)  # a helper or plugin, possibly outside tests/
    if "tests" in roots:
        return ("tests",)
    return tuple(sorted(r for r in roots if not any(r.startswith(f"{other}/") for other in roots)))


def _reverse_closure(graph: ImportGraph, seeds: set[str]) -> set[str]:
    affected = set(seeds)
    pending = deque(seeds)
    while pending:
        dependency = pending.popleft()
        for owner in graph.reverse.get(dependency, set()):
            if owner not in affected:
                affected.add(owner)
                pending.append(owner)
    return affected


def _seed_nodes(graph: ImportGraph, module: str) -> set[str]:
    """A direct shared-module edit includes its definitions and global behavior."""
    if module in graph.change_seeds:
        return graph.change_seeds[module]
    scopes = {scope for scope in graph.scopes.values() if scope.startswith(f"{module}::")}
    return {module, *scopes, f"{module}::<global>", f"{module}::<exports>"}


# Decorators that only wrap a definition (a descriptor, a cache, a context
# manager, a typing marker) and never call it. Names must match exactly: a
# ``registry.getter`` that shares a suffix may register the method. Any other
# decorator may expose or invoke the definition at import time.
_INERT_DECORATORS = frozenset(
    {
        *("property", "functools.cached_property", "cached_property", "staticmethod", "classmethod"),
        *("abc.abstractmethod", "abstractmethod", "typing.override", "typing_extensions.override", "override"),
        *("typing.overload", "overload", "typing.final", "final"),
        *("functools.cache", "cache", "functools.lru_cache", "lru_cache"),
        *("contextlib.contextmanager", "contextmanager", "contextlib.asynccontextmanager", "asynccontextmanager"),
    }
)
_DATACLASS_DECORATORS = frozenset({"dataclass", "dataclasses.dataclass"})
# Class decorators that never call the class's methods while decorating it.
_INERT_CLASS_DECORATORS = _DATACLASS_DECORATORS | {
    "typing.final",
    "final",
    "typing.runtime_checkable",
    "runtime_checkable",
}


def _member_names(node: ast.ClassDef) -> frozenset[str]:
    return frozenset(m.name for m in node.body if isinstance(m, ast.FunctionDef | ast.AsyncFunctionDef))


def _inert_decorator(decorator: ast.expr, members: frozenset[str] = frozenset()) -> bool:
    """Whether a decorator only wraps the definition; ``members`` names the enclosing class's methods."""
    name = _dotted_name(decorator.func if isinstance(decorator, ast.Call) else decorator)
    if name in _INERT_DECORATORS or name in {"pytest.fixture", "fixture", "pytest.hookimpl", "hookimpl"}:
        return True
    owner, _, accessor = name.rpartition(".")
    # ``@size.setter`` extends the property ``size`` defined in the same class.
    return not isinstance(decorator, ast.Call) and accessor in {"setter", "getter", "deleter"} and owner in members


def _same_header(old: ast.stmt, new: ast.stmt) -> bool:
    """Name, arguments, bases, decorators and annotations match; only the body may differ."""
    old_header, new_header = copy(old), copy(new)
    old_header.body = new_header.body = []
    return ast.dump(old_header) == ast.dump(new_header)


def _method_body_edit(old: ast.stmt, new: ast.stmt) -> bool:
    """A class edit confined to method bodies, under descriptor decorators only."""
    if not isinstance(old, ast.ClassDef) or not isinstance(new, ast.ClassDef):
        return False
    if (
        not _same_header(old, new)
        or any(
            _dotted_name(d.func if isinstance(d, ast.Call) else d) not in _DATACLASS_DECORATORS
            for d in new.decorator_list
        )
        or len(old.body) != len(new.body)
    ):
        return False
    members = _member_names(new)
    for old_member, new_member in zip(old.body, new.body, strict=True):
        if ast.dump(old_member) == ast.dump(new_member):
            continue
        if not isinstance(old_member, ast.FunctionDef | ast.AsyncFunctionDef) or not isinstance(
            new_member, ast.FunctionDef | ast.AsyncFunctionDef
        ):
            return False
        if not _same_header(old_member, new_member) or not all(
            _inert_decorator(d, members) for d in new_member.decorator_list
        ):
            return False
    return True


def _reflects_on_module(tree: ast.Module) -> bool:
    """Whether code can reach this module's functions without naming them.

    Namespace dictionaries, ``eval``/``exec``, ``sys.modules`` and a module
    ``__getattr__`` can. ``getattr(obj, name)``, ``obj.__dict__`` and
    ``logging.getLogger(__name__)`` act on another object, so they do not block
    narrowing; a dynamic dispatch on ``self`` stays inside its class, the
    narrowed unit.
    """
    nodes = list(ast.walk(tree))
    sys_names = {"sys"} | {
        alias.asname or alias.name
        for n in nodes
        if isinstance(n, ast.Import)
        for alias in n.names
        if alias.name == "sys"
    }
    loaders = {"import_module", "__import__"} | {
        alias.asname or alias.name
        for n in nodes
        if isinstance(n, ast.ImportFrom) and n.module == "importlib"
        for alias in n.names
        if alias.name == "import_module"
    }
    for node in nodes:
        if _dotted_name(node) in {"globals", "locals", "vars", "eval", "exec"}:
            return True
        if isinstance(node, ast.Attribute) and node.attr == "modules" and _dotted_name(node.value) in sys_names:
            return True
        if isinstance(node, ast.ImportFrom) and node.module == "sys" and any(a.name == "modules" for a in node.names):
            return True
        if (
            isinstance(node, ast.Call)
            and _dotted_name(node.func).rsplit(".", 1)[-1] in loaders
            and any(isinstance(arg, ast.Name) and arg.id == "__name__" for arg in node.args)
        ):
            return True
    return any(
        isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == "__getattr__" for node in tree.body
    )


def _changed_function_names(before: str | bytes, after: str | bytes) -> set[str] | None:
    """Narrow function-body and method-body edits; structural edits retain module scope."""
    try:
        previous, current = ast.parse(before), ast.parse(after)
    except SyntaxError, ValueError:
        return None
    if len(previous.body) != len(current.body):
        return None
    changed: set[str] = set()
    for old, new in zip(previous.body, current.body, strict=True):
        if ast.dump(old) == ast.dump(new):
            continue
        if isinstance(new, ast.ClassDef) and _method_body_edit(old, new):
            changed.add(new.name)
            continue
        if not isinstance(old, ast.FunctionDef | ast.AsyncFunctionDef) or not isinstance(
            new, ast.FunctionDef | ast.AsyncFunctionDef
        ):
            return None
        if new.decorator_list or not _same_header(old, new):
            return None
        changed.add(new.name)
    if not changed:
        return None
    if _reflects_on_module(current):
        return None
    definitions = _definition_nodes(current)
    affected = set(changed)
    while True:
        callers = {
            name
            for name, nodes in definitions.items()
            if any(isinstance(node, ast.Name) and node.id in affected for body in nodes for node in ast.walk(body))
        }
        if callers <= affected:
            break
        affected.update(callers)
    if _initialization_executes(current, affected):
        return None
    return affected


def _initialization_executes(tree: ast.Module, affected: set[str]) -> bool:
    """Whether import-time code may run an affected definition.

    Calls (an argument may be invoked too), class bases and keywords (subclass
    hooks) and decorators execute it. A definition under an unknown decorator
    may be registered and called later, so it must neither be affected nor
    name an affected definition; an affected class must not carry such a
    method. Creating an affected subclass runs ``__init_subclass__`` hooks and
    metaclasses, which may call its methods (hooks on imported bases are not
    seen). A plain reference such as a type alias only binds another name,
    which the caller walk already marks as affected.
    """

    def uses(node: ast.AST) -> bool:
        return any(isinstance(inner, ast.Name) and inner.id in affected for inner in ast.walk(node))

    for node in _initialization_nodes(tree.body):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) and (
            node.name in affected or uses(node)
        ):
            return True  # included whole: it carries an unknown decorator
        if any(isinstance(inner, ast.Call) and uses(inner) for inner in ast.walk(node)):
            return True

    # A local hook or metaclass may run for any subclass, inherited through bases.
    # Other class keywords (``total=False``, ``can_focus=False``) are arguments to
    # such a hook: a local one is found here, an imported one is the accepted miss.
    def has_metaclass(node: ast.ClassDef) -> bool:
        # ``**options`` (arg None) may carry a metaclass too.
        return any(keyword.arg in {"metaclass", None} for keyword in node.keywords)

    subclass_hooks = any(
        (isinstance(node, ast.FunctionDef) and node.name == "__init_subclass__")
        or (isinstance(node, ast.ClassDef) and has_metaclass(node))
        for node in ast.walk(tree)
    )
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef) or node.name not in affected:
            continue
        if has_metaclass(node) or (node.bases and subclass_hooks):
            return True
        members = _member_names(node)
        if any(
            not _inert_decorator(d, members)
            for member in node.body
            if isinstance(member, ast.FunctionDef | ast.AsyncFunctionDef)
            for d in member.decorator_list
        ):
            return True
    for node in _module_scope_nodes(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) and any(
            uses(expression) for expression in node.decorator_list
        ):
            return True
        if isinstance(node, ast.ClassDef) and any(
            uses(base) for base in (*node.bases, *(k.value for k in node.keywords))
        ):
            return True
    return False


def _resolve_export(reference: str, known: set[str], facades: dict[str, dict[str, str]]) -> str:
    """Follow named re-exports to the defining module's member reference."""
    visited: set[tuple[str, str]] = set()
    while True:
        provider = _longest_known_reference(reference, known)
        if provider is None:
            return reference
        name, _, tail = reference.removeprefix(provider).lstrip(".").partition(".")
        exported = facades.get(provider, {}).get(name)
        if exported is None or (provider, name) in visited:
            return reference
        visited.add((provider, name))
        reference = exported + (f".{tail}" if tail else "")


def _narrow_source_imports(graph: ImportGraph, path: str, before: str | bytes, *, root: Path = REPO_ROOT) -> None:
    module = graph.path_to_module.get(path)
    if module is None or path.endswith("/__init__.py"):
        return
    try:
        # Raw bytes keep encoding-cookie semantics, as the runtime no-op check does.
        names = _changed_function_names(before, (root / path).read_bytes())
    except OSError:
        return
    if not names:
        return
    seeds = {f"{module}::{name}" for name in names}
    for owner in graph.reverse.get(module, ()):
        owner_path = graph.module_to_path.get(owner)
        relevant = True
        if owner_path is not None:
            try:
                tree = ast.parse((root / owner_path).read_text(encoding="utf-8"))
            except OSError, SyntaxError, UnicodeError:
                tree = None
            if tree is not None:
                imports: set[str] = set()
                other_imports: list[ast.stmt] = []
                known = set(graph.module_to_path)
                # Only prune explicit named imports, directly or through named
                # re-exports. Namespace access, dynamic references, wildcard
                # facades and fixture scopes stay included.
                package = owner_path.endswith("/__init__.py")
                for node in _runtime_nodes(tree):
                    if isinstance(node, ast.ImportFrom):
                        base = (
                            _relative_import(owner, package=package, level=node.level, imported=node.module)
                            if node.level
                            else node.module
                        )
                        if base == module:
                            imports.update(alias.name for alias in node.names)
                            continue
                        if not base or any(alias.name == "*" for alias in node.names):
                            other_imports.append(node)
                            continue
                        kept: list[ast.alias] = []
                        for alias in node.names:
                            target = _resolve_export(f"{base}.{alias.name}", known, graph.facades)
                            if target.startswith(f"{module}."):
                                imports.add(target.removeprefix(f"{module}.").split(".", 1)[0])
                            else:
                                kept.append(alias)
                        if kept:
                            other_imports.append(ast.ImportFrom(module=node.module, names=kept, level=node.level))
                    elif isinstance(node, ast.Import):
                        other_imports.append(node)
                # Reuse the graph resolver so relative and facade routes cannot
                # disappear when mixed with an unrelated absolute named import.
                other_dependencies = _module_dependencies(
                    ast.Module(body=other_imports),
                    module=owner,
                    package=package,
                    known_modules=set(graph.module_to_path),
                    facades=graph.facades,
                )
                owner_package = owner if package else owner.rpartition(".")[0]
                if (
                    imports
                    and module not in other_dependencies
                    and not any(
                        _string_reaches(reference, module, owner_package, known, graph.facades)
                        for reference in _string_module_references(tree)
                    )
                ):
                    relevant = "*" in imports or bool(imports & names)
        if relevant:
            for seed in seeds:
                graph.reverse.setdefault(seed, set()).add(owner)
    graph.change_seeds[module] = seeds


def _string_reaches(
    reference: str, module: str, package: str, known: set[str], facades: dict[str, dict[str, str]]
) -> bool:
    """Whether a patch/import/plugin string may name ``module``, a member of it, or a package holding it."""
    if reference.startswith("."):
        return True
    for candidate in (reference, f"{package}.{reference}" if package else reference):
        target = _resolve_export(candidate, known, facades)
        if target == module or target.startswith(f"{module}.") or module.startswith(f"{target}."):
            return True
    return False


def _baseline_revision() -> str | None:
    """Return the branch merge base, or None when the baseline cannot be told apart from HEAD."""
    branch = _resolve_default_branch()
    if branch == "refs/heads/main" and _local_main_is_head():
        # Local main alone cannot distinguish committed task changes from
        # the baseline, even when dirty edits remain in the same file.
        return None
    base = _git_output("merge-base", "HEAD", branch, allow_failure=True) if branch else None
    return base.decode("ascii").strip() if base else None


def _is_runtime_noop(before: bytes, after: bytes) -> bool:
    """Equal ASTs differ only in comments, blank lines or formatting; docstrings stay semantic.

    Parse the raw bytes: an encoding cookie or BOM changes how string literals decode.
    """
    try:
        return ast.dump(ast.parse(before)) == ast.dump(ast.parse(after))
    except SyntaxError, ValueError:
        return False


def runtime_noop_paths(changes: tuple[Change, ...], *, root: Path = REPO_ROOT) -> frozenset[str]:
    """Changed Python files whose AST equals the branch baseline.

    New, deleted and renamed paths, and any missing baseline, never qualify.
    """
    paths = [
        c.path
        for c in changes
        if c.path.endswith(".py")
        and c.path.startswith(("src/chrys/", "tests/", "scripts/"))
        and not c.deleted_or_renamed
        and (root / c.path).is_file()
    ]
    if not paths:
        return frozenset()
    noop: set[str] = set()
    try:
        revision = _baseline_revision()
        if revision is None:
            return frozenset()
        for path in paths:
            before = _git_output("show", f"{revision}:{path}", allow_failure=True)
            if before is not None and _is_runtime_noop(before, (root / path).read_bytes()):
                noop.add(path)
    except SmartTestError, OSError:
        return frozenset()
    return frozenset(noop)


def _refine_source_changes(graph: ImportGraph, changes: tuple[Change, ...]) -> None:
    """Use the branch baseline when available; missing Git data keeps module scope."""
    paths = [
        c.path
        for c in changes
        if c.path.startswith("src/chrys/")
        and c.path.endswith(".py")
        and not c.deleted_or_renamed
        and c.path not in graph.runtime_noop
    ]
    if not paths:
        return
    try:
        revision = _baseline_revision()
        if revision is None:
            return
        for path in paths:
            before = _git_output("show", f"{revision}:{path}", allow_failure=True)
            if before is not None:
                _narrow_source_imports(graph, path, before, root=REPO_ROOT)
    except SmartTestError, UnicodeError:
        return


def _dependency_chains(graph: ImportGraph, module: str) -> dict[str, str]:
    """Reach owners within both hop budgets, keeping each node's shortest explanation.

    A source hop enters another production module; a test hop is one test file
    importing another. Helper, conftest and fixture scope edges stay free: they
    are the precise routes fixture consumers depend on. A node can be reached
    through incomparable budget states, so every non-dominated state is kept.
    """
    seeds = sorted(_seed_nodes(graph, module))
    paths = {seed: [module] if seed == module else [module, seed] for seed in seeds}
    costs = dict.fromkeys(paths, 0)
    states: dict[str, list[tuple[int, int]]] = {seed: [(0, 0)] for seed in seeds}
    pending = deque((seed, 0, 0, paths[seed]) for seed in seeds)
    while pending:
        dependency, source_hops, test_hops, path = pending.popleft()
        dependency_module = dependency.split("::", 1)[0]
        # Scope nodes (helpers, fixtures, ``<global>``) are free routes even in a test-named module.
        dependency_is_test = "::" not in dependency and _is_test_file(graph.module_to_path.get(dependency_module, ""))
        for owner in sorted(graph.reverse.get(dependency, ())):
            owner_module = owner.split("::", 1)[0]
            crossing = owner_module != dependency_module
            owner_path = graph.module_to_path.get(owner_module, "")
            sources = source_hops + int(crossing and owner_path.startswith("src/chrys/"))
            tests = test_hops + int(
                crossing
                and dependency_is_test
                and "::" not in owner
                and _is_test_file(owner_path)
                # Importing a test module's fixtures makes its importer a consumer.
                and dependency_module not in graph.fixture_modules
            )
            if sources > _MAX_SOURCE_IMPORT_HOPS or tests > _MAX_TEST_IMPORT_HOPS:
                continue
            known = states.setdefault(owner, [])
            if any(s <= sources and t <= tests for s, t in known):
                continue
            known[:] = [(s, t) for s, t in known if not (sources <= s and tests <= t)]
            known.append((sources, tests))
            chain = [*path, owner]
            if owner not in paths or (sources + tests, len(chain)) < (costs[owner], len(paths[owner])):
                costs[owner] = sources + tests
                paths[owner] = chain
            entry = (owner, sources, tests, chain)
            if sources + tests == source_hops + test_hops:
                pending.appendleft(entry)
            else:
                pending.append(entry)
    return {node: " -> ".join(path) for node, path in paths.items()}


def _test_dependents(graph: ImportGraph, module: str | None, *, root: Path) -> set[str]:
    if module is None:
        return set()
    affected = _dependency_chains(graph, module)
    return {
        candidate_path
        for affected_module in affected
        if (candidate_path := graph.module_to_path.get(affected_module)) is not None
        and _is_test_file(candidate_path)
        and (root / candidate_path).is_file()
    } | {nodeid.split("::", 1)[0] for scope in affected for nodeid in graph.consumers.get(scope, ())}


def _regular_test_dependents(graph: ImportGraph, module: str | None, *, root: Path) -> set[str]:
    return {path for path in _test_dependents(graph, module, root=root) if not path.startswith("tests/architecture/")}


def _matches(path: str, pattern: str) -> bool:
    if pattern.endswith("/**"):
        prefix = pattern[:-3].rstrip("/")
        return path == prefix or path.startswith(f"{prefix}/")
    return fnmatch.fnmatchcase(path, pattern)


def _is_test_file(path: str) -> bool:
    candidate = Path(path)
    return path.startswith("tests/") and any(
        fnmatch.fnmatchcase(candidate.name, pattern) for pattern in _PYTEST_FILE_PATTERNS
    )


def _add_rule_targets(
    selection: Selection,
    changes: tuple[Change, ...],
    rules: tuple[TestRule, ...],
    *,
    kind: str,
) -> set[str]:
    matched_paths: set[str] = set()
    for rule in rules:
        matches = [
            change.path for change in changes if any(_matches(change.path, pattern) for pattern in rule.patterns)
        ]
        if matches:
            matched_paths.update(matches)
            selection.add(rule.target, f"{kind} watch matched {_display(matches[0])}")
    return matched_paths


def _mirror_tests(path: str) -> list[str]:
    """Candidate direct tests for a source module; a private ``_x.py`` is also tested as ``test_x.py``."""
    mirrored = Path("tests") / Path(path).relative_to("src/chrys")
    names = [mirrored.name]
    if mirrored.name.startswith("_") and not mirrored.name.startswith("__"):
        names.append(mirrored.name.lstrip("_"))
    return [mirrored.with_name(f"test_{name}").as_posix() for name in names]


def _subsystem_test_directory(path: str, *, root: Path = REPO_ROOT) -> str | None:
    parts = Path(path).parts
    if len(parts) < 3 or parts[:2] != ("src", "chrys"):
        return None
    for size in range(len(parts) - 1, 2, -1):
        candidate = Path("tests", *parts[2:size])
        if (root / candidate).is_dir():
            return candidate.as_posix()
    return None


def _add_local_fallback(selection: Selection, path: str, reason: str, *, root: Path) -> None:
    """Unknown impact expands locally; only global configuration warrants --full."""
    target = _subsystem_test_directory(path, root=root)
    if path.startswith("tests/"):
        for parent in Path(path).parents:
            if parent == Path("tests"):
                break
            if (root / parent).is_dir() and (
                path.endswith(".py")
                or any(
                    candidate.is_file()
                    for pattern in _PYTEST_FILE_PATTERNS
                    for candidate in (root / parent).rglob(pattern)
                )
            ):
                target = parent.as_posix()
                break
    if target is not None:
        selection.add(target, f"local fallback: {reason}: {path}")
    else:
        selection.notes.add(f"No nearby test directory for {path}; using known dependencies and file watches only.")


def _hygiene_shard(path: str) -> int:
    try:
        encoded = path.encode("utf-8")
    except UnicodeEncodeError as error:
        raise SmartTestError(
            f"Python path contains non-UTF-8 bytes and cannot be matched to an architecture shard: {_display(path)}"
        ) from error
    return zlib.crc32(encoded) % _HYGIENE_SHARDS


def _add_hygiene_targets(selection: Selection, changes: tuple[Change, ...]) -> None:
    src_shards: set[int] = set()
    test_shards: set[int] = set()
    tui_changed = False
    hygiene_family_changed = False
    hygiene_core_changed = False
    architecture_python_changed = False
    for change in changes:
        path = change.path
        if not path.endswith(".py"):
            continue
        if path.startswith("src/chrys/"):
            src_shards.add(_hygiene_shard(path))
            tui_changed = tui_changed or path.startswith("src/chrys/app/tui/")
        elif path.startswith("tests/"):
            test_shards.add(_hygiene_shard(path))
        hygiene_family_changed = hygiene_family_changed or path.startswith("tests/architecture/test_hygiene_")
        hygiene_core_changed = hygiene_core_changed or path == "tests/architecture/_hygiene_core.py"
        architecture_python_changed = architecture_python_changed or path.startswith("tests/architecture/")

    base = "tests/architecture/test_test_hygiene.py"
    for shard in sorted(src_shards):
        selection.add(
            f"{base}::test_exchange_walker_guard_holds_across_src_sources[{shard}]",
            f"source hygiene shard {shard}",
        )
    for shard in sorted(test_shards):
        selection.add(
            f"{base}::test_hygiene_rules_hold_across_test_sources[{shard}]",
            f"test hygiene shard {shard}",
        )
    if tui_changed:
        selection.add(
            f"{base}::test_global_src_hygiene_rules_hold_across_all_sources",
            "TUI changes require the cross-file locale-controller guard",
        )
    if architecture_python_changed:
        selection.add(
            f"{base}::test_every_hygiene_rule_is_registered",
            "architecture rule definitions changed",
        )
    if hygiene_family_changed or hygiene_core_changed:
        selection.add(
            f"{base}::test_every_non_empty_allowlist_has_a_liveness_pin",
            "hygiene registry changed",
        )
    if hygiene_core_changed:
        selection.add(
            f"{base}::test_sweep_shard_partitions_are_complete_disjoint_and_non_empty",
            "hygiene shard implementation changed",
        )


def _add_trajectory_targets(selection: Selection, changes: tuple[Change, ...]) -> None:
    inventory_source_changed = any(change.path.startswith(_TRAJECTORY_SOURCE_PREFIXES) for change in changes)
    production_python_changed = any(
        change.path.startswith("src/chrys/") and change.path.endswith(".py") for change in changes
    )
    inventory_changed = any(
        change.path
        in {
            "tests/architecture/trajectory_wait_manifest.json",
            "tests/support/trajectory_wait_inventory.py",
        }
        for change in changes
    )
    test_file = "tests/architecture/test_trajectory_wait_inventory.py"
    if inventory_changed:
        selection.add(test_file, "trajectory wait inventory or manifest changed")
        return
    if inventory_source_changed:
        for test_name in (
            "test_wait_manifest_matches_source",
            "test_wait_inventory_covers_every_explicit_and_implicit_async_wait",
        ):
            selection.add(f"{test_file}::{test_name}", "async-capable production source changed")
    if production_python_changed:
        selection.add(
            f"{test_file}::test_pending_retry_clear_calls_declare_a_terminal_reason",
            "production source changed",
        )


def _add_runtime_asset_targets(selection: Selection, changes: tuple[Change, ...], *, root: Path) -> None:
    for change in changes:
        path = change.path
        if path in {
            ".github/workflows/ci.yml",
            ".github/workflows/cd.yml",
            ".github/workflows/tag-release.yml",
            "scripts/check_wheel.py",
            "scripts/offline_wheel_overrides.txt",
            "scripts/offline_build_constraints.txt",
        } or any(_matches(path, pattern) for pattern in ("scripts/build*.sh", "scripts/build*.ps1")):
            selection.add("tests/app/cli/test_app.py", f"build contract file changed: {path}")
        elif path.endswith(".tcss"):
            selection.add("tests/app/tui", f"Textual stylesheet changed: {path}")
        elif path.startswith("src/chrys/service/profiles/agents/builtins/") and path.endswith(".yaml"):
            for target in _BUILTIN_PROFILE_TEST_TARGETS:
                selection.add(target, f"built-in profile changed: {path}")
        elif path.startswith("src/chrys/service/workflows/builtins/"):
            for target in _BUILTIN_WORKFLOW_TEST_TARGETS:
                selection.add(target, f"built-in workflow changed: {path}")
        elif path.startswith("locales/") or path in {"hatch_build.py", "scripts/i18n_mo.py"}:
            selection.add("tests/foundation/i18n", f"i18n artifact changed: {path}")
            selection.add("tests/app/tui/i18n", f"i18n artifact changed: {path}")
        elif path.startswith("src/chrys/") and not path.endswith((".py", ".md")):
            subsystem = _subsystem_test_directory(path, root=root)
            if subsystem is not None:
                selection.add(subsystem, f"runtime asset changed: {path}")
            else:
                _add_local_fallback(selection, path, "runtime asset changed", root=root)
        elif (
            path.startswith("tests/")
            and not path.endswith(".py")
            and path != "tests/architecture/trajectory_wait_manifest.json"
        ):
            _add_local_fallback(selection, path, "test runtime asset changed", root=root)


def select_smart_tests(
    changes: tuple[Change, ...], graph: ImportGraph, *, root: Path = REPO_ROOT, defer_fixture_fallbacks: bool = False
) -> Selection:
    """Return nearby affected tests for quick local feedback, with explicit watches."""

    selection = Selection()
    changed_paths = {change.path for change in changes}
    for error in graph.parse_errors:
        if any(error.startswith(f"{path}: ") for path in changed_paths):
            raise SmartTestError(f"Cannot analyze changed Python file: {error}")
    noop = graph.runtime_noop
    full_trigger = sorted((changed_paths & _FULL_TRIGGER_PATHS) - noop)
    if full_trigger:
        selection.full_reason = (
            f"global test environment or configuration changed: {', '.join(_display(path) for path in full_trigger)}"
        )
        return selection
    if graph.parse_errors:
        selection.notes.add(f"Import analysis skipped an unparseable file: {graph.parse_errors[0]}")

    for change in changes:
        if _is_test_file(change.path) and (root / change.path).is_file():
            selection.add(change.path, "test file changed directly")

    for change in changes:
        path = change.path
        if not (change.deleted_or_renamed and path.startswith("tests/") and path.endswith(".py")):
            continue
        _add_local_fallback(selection, path, "test module, helper, or conftest was deleted or renamed", root=root)

    for path in sorted(noop & changed_paths):
        selection.notes.add(f"No runtime change (comments or formatting only): {path}")

    seed_paths: dict[str, set[str]] = defaultdict(set)
    known_modules = set(graph.module_to_path)
    for change in changes:
        if change.path in noop:
            continue
        module = graph.path_to_module.get(change.path) or _module_for_path(change.path)
        if module is None:
            continue
        seed_paths[module].add(change.path)
        if change.deleted_or_renamed:
            # The vanished leaf cannot be a known current-tree module, but its
            # most specific surviving package still reveals consumers such as
            # tests that import helpers from another test module.  Broader
            # ancestors would make every module below ``tests`` or ``chrys``
            # look affected and turn routine renames into full-suite runs.
            surviving_parent = _longest_known_reference(module, known_modules)
            if surviving_parent is not None:
                seed_paths[surviving_parent].add(change.path)
    affected_modules: set[str] = set()
    for seed_module, changed_seed_paths in sorted(seed_paths.items()):
        chains = _dependency_chains(graph, seed_module)
        affected_modules.update(node.split("::", 1)[0] for node in chains)
        selection.reached.update(chains)
        for affected_module in sorted(chains):
            path = graph.module_to_path.get(affected_module)
            for changed_path in sorted(changed_seed_paths):
                if (
                    affected_module != seed_module
                    and path is not None
                    and _is_test_file(path)
                    and (root / path).is_file()
                ):
                    selection.add(
                        path,
                        f"reverse import dependency of changed path {_display(changed_path)}; chain: {chains[affected_module]}",
                    )
                for nodeid in sorted(graph.consumers.get(affected_module, ())):
                    selection.add(
                        nodeid, f"fixture dependency of {changed_path}; chain: {chains[affected_module]} -> {nodeid}"
                    )

    if seed_paths:
        for unknown, reason in sorted(graph.uncertain.items()):
            # Collection-only unknowns have no resolvable module. Keep their
            # observed consumers without widening to unrelated test files.
            if not unknown.startswith("unknown:") and unknown.split("::", 1)[0] not in affected_modules:
                continue
            for affected in _dependency_chains(graph, unknown):
                path = graph.module_to_path.get(affected)
                if path is not None and _is_test_file(path):
                    selection.add(path, f"local fallback: {reason}; reaches {affected}")
                for nodeid in sorted(graph.consumers.get(affected, ())):
                    selection.add(nodeid, f"local fallback: {reason}; reaches fixture {affected}")

    # Like an import consumer, a watched test is selected only where it exists.
    present_rules = tuple(rule for rule in REGULAR_RULES if (root / rule.target).exists())
    regular_rule_matches = _add_rule_targets(selection, changes, present_rules, kind="regular filesystem")

    for change in changes:
        path = change.path
        if (
            not path.startswith("tests/support/")
            or not path.endswith(".py")
            or _is_test_file(path)
            or change.deleted_or_renamed
            or path in noop
        ):
            continue
        module = graph.path_to_module.get(path) or _module_for_path(path)
        if (
            not defer_fixture_fallbacks
            and not _test_dependents(graph, module, root=root)
            and path not in regular_rule_matches
        ):
            _add_local_fallback(selection, path, "no known import or file-watch consumers", root=root)

    for change in changes:
        path = change.path
        if path.startswith("src/chrys/") and path.endswith(".py"):
            mirrors = [test for test in _mirror_tests(path) if (root / test).is_file()]
            for direct_test in mirrors:
                selection.add(direct_test, f"mirrors changed source module {path}")
            if path in noop:
                continue  # no runtime consumers to find, so no fallback either
            module = graph.path_to_module.get(path) or _module_for_path(path)
            dependent_regular_tests = _regular_test_dependents(graph, module, root=root) | set(mirrors)
            if change.deleted_or_renamed or (not dependent_regular_tests and not defer_fixture_fallbacks):
                subsystem = _subsystem_test_directory(path, root=root)
                if subsystem is not None:
                    reason = (
                        f"deleted or renamed production module: {path}"
                        if change.deleted_or_renamed
                        else f"no importing test was found for changed module: {path}"
                    )
                    selection.add(subsystem, reason)
                else:
                    _add_local_fallback(selection, path, "no nearby importing tests", root=root)

    _add_rule_targets(selection, changes, ARCHITECTURE_RULES, kind="architecture")

    _add_hygiene_targets(selection, changes)
    _add_trajectory_targets(selection, changes)
    _add_runtime_asset_targets(selection, changes, root=root)

    return selection


def _display(value: str) -> str:
    return value.encode("utf-8", errors="backslashreplace").decode("utf-8")


def _write(line: str = "") -> None:
    sys.stdout.write(f"{_display(line)}\n")
    sys.stdout.flush()


def _print_changes(changes: tuple[Change, ...]) -> None:
    _write(f"Smart Test found {len(changes)} changed path(s):")
    for change in changes:
        _write(f"  {change.path} [{', '.join(sorted(change.states))}]")


def _print_targets(selection: Selection) -> None:
    targets = {**selection.regular, **selection.architecture}
    targets = {target: targets[target] for target in _prune_covered_targets(sorted(targets))}
    _write(f"Smart Test selected {len(targets)} pytest target(s):")
    for target in sorted(targets):
        ordered_reasons = sorted(targets[target])
        rendered_reasons = ordered_reasons[:_MAX_RENDERED_REASONS]
        hidden_count = len(ordered_reasons) - len(rendered_reasons)
        if hidden_count:
            rendered_reasons.append(f"+{hidden_count} more")
        _write(f"  {target}")
        _write(f"    <- {'; '.join(rendered_reasons)}")
    sources = Counter(_selection_source(reasons) for reasons in targets.values())
    _write(
        f"Targets by source: {', '.join(f'{name} {sources[name]}' for name, _ in _SELECTION_SOURCES if sources[name])}."
    )


# Most specific first: a target selected for several reasons counts once, under the first match.
_SELECTION_SOURCES = (
    ("direct", "test file changed directly"),
    ("mirror", "mirrors changed source module"),
    ("watch", " watch matched "),
    ("import", "reverse import dependency"),
    ("fixture", "fixture dependency"),
    ("fallback", "local fallback"),
    ("rule", ""),
)


def _selection_source(reasons: set[str]) -> str:
    return next(name for name, marker in _SELECTION_SOURCES if any(marker in reason for reason in reasons))


def _parameters_start(nodeid: str) -> int:
    """Index of a node id's parameter part (``[...]`` after the first ``::``), or -1.

    Parameter ids are opaque text: ``]::`` or ``/`` inside them is not structure.
    """
    separator = nodeid.find("::")
    return -1 if separator < 0 else nodeid.find("[", separator)


def _target_covers(target: str, nodeid: str) -> bool:
    if nodeid == target or _parameters_start(target) >= 0:
        return nodeid == target
    return _covered_by(nodeid, {target})


def _covered_by(nodeid: str, targets: set[str] | frozenset[str]) -> bool:
    """``_target_covers`` against a target set, by looking up each prefix a target could be."""
    if nodeid in targets:
        return True
    parameters = _parameters_start(nodeid)
    structure = nodeid if parameters < 0 else nodeid[: parameters + 1]
    for index, character in enumerate(structure):
        if (character in "/[" or nodeid.startswith("::", index)) and nodeid[:index] in targets:
            return True
    return False


def _covered_nodeids(targets: Iterable[str], nodeids: Iterable[str]) -> set[str]:
    index = frozenset(targets)
    return {nodeid for nodeid in nodeids if _covered_by(nodeid, index)}


def _collapse_complete_files(selection: Selection, collected: Sequence[str]) -> None:
    """Replace a file's node targets with the file when they cover every collected test in it.

    The execution scope is unchanged (pytest's marker filter still excludes
    what collection excluded); a fixture used across a large directory would
    otherwise pass thousands of node ids to pytest and to the target listing.
    """
    by_file: dict[str, list[str]] = defaultdict(list)
    for nodeid in collected:
        by_file[nodeid.split("::", 1)[0]].append(nodeid)
    for group in (selection.regular, selection.architecture):
        node_targets: dict[str, list[str]] = defaultdict(list)
        for target in group:
            if "::" in target:
                node_targets[target.split("::", 1)[0]].append(target)
        for file, targets in node_targets.items():
            nodeids = by_file.get(file)
            if not nodeids or file in group:
                continue  # an uncollected file, or one already selected whole
            index = frozenset(targets)
            if all(_covered_by(nodeid, index) for nodeid in nodeids):
                group[file] = set().union(*(group.pop(target) for target in targets))


def _print_selection_counts(
    initial: Selection, final: Selection, graph: ImportGraph, roots: Sequence[str] = ("tests",)
) -> None:
    initial_targets = _prune_covered_targets(sorted({**initial.regular, **initial.architecture}))
    final_targets = _prune_covered_targets(sorted({**final.regular, **final.architecture}))
    initially_covered = _covered_nodeids(initial_targets, graph.collected)
    finally_covered = _covered_nodeids(final_targets, graph.collected)
    fixture_targets = {
        target
        for target, reasons in {**final.regular, **final.architecture}.items()
        if any(reason.startswith(("fixture dependency", "local fallback")) for reason in reasons)
    }
    fixture_added = _covered_nodeids(fixture_targets, finally_covered - initially_covered)
    other_added = finally_covered - initially_covered - fixture_added
    final_files = {nodeid.split("::", 1)[0] for nodeid in finally_covered}
    _write(f"Initial selection: {len(initial_targets)} targets / {len(initially_covered)} collected tests.")
    _write(f"Fixture/fallback additions: {len(fixture_added)} tests; other local additions: {len(other_added)} tests.")
    _write(
        f"Final execution scope: {len(final_targets)} targets / {len(finally_covered)} of "
        f"{len(graph.collected)} collected non-integration tests; {len(final_files)} files."
    )
    if tuple(roots) != ("tests",):
        _write(f"Counts cover only the collected directories: {', '.join(roots)}.")


def _execute_targets(targets: list[str], *, report: Path | None = None) -> subprocess.CompletedProcess[bytes]:
    """Transport exact targets, keeping the argument file alive through worker shutdown."""
    command = _pytest_command(targets, report=report)
    if len(targets) <= _MAX_TARGET_ARGUMENTS and sum(len(target) + 1 for target in targets) <= _MAX_TARGET_CHARACTERS:
        return _run_process(command)
    # pytest/argparse reads one argument per physical line. Never silently
    # reinterpret unusual filesystem identities as several targets.
    if any("\n" in target or "\r" in target for target in targets):
        raise SmartTestError("A long target list contains a newline path; pytest argument files cannot represent it")
    with tempfile.TemporaryDirectory(prefix="chrys-pytest-targets-") as directory:
        arguments = Path(directory) / "targets.txt"
        arguments.write_text(
            "\n".join(targets) + "\n", encoding=sys.getfilesystemencoding(), errors=sys.getfilesystemencodeerrors()
        )
        _write(f"Passing {len(targets)} exact targets through a pytest argument file (scope unchanged).")
        return _run_process([*command[: -len(targets)], f"@{arguments}"])


def _prune_covered_targets(targets: list[str]) -> list[str]:
    """Remove node/file targets already covered by a selected file/directory."""

    directories = {target.rstrip("/") for target in targets if "::" not in target and not target.endswith(".py")}
    whole_files = {target for target in targets if "::" not in target and target.endswith(".py")}
    pruned: list[str] = []
    for target in targets:
        path = target.split("::", maxsplit=1)[0]
        if target != path and path in whole_files:
            continue
        if any(
            target != directory and (path == directory or path.startswith(f"{directory}/")) for directory in directories
        ):
            continue
        pruned.append(target)
    return pruned


def _pytest_command(targets: list[str], *, report: Path | None = None) -> list[str]:
    """Smart runs parallelize architecture targets too: each whole-repo guard is seconds of serial work."""
    command = [sys.executable, "-m", "pytest", "-q", "--tb=short", *_report_options(report), "-m", _EXCLUDED_MARKERS]
    directory_selected = any("::" not in target and not target.endswith(".py") for target in targets)
    if len(targets) <= 2 and not directory_selected:
        command.extend(("-n", "0"))
    elif directory_selected:
        # Explicit subsystem fallbacks can represent almost the complete
        # suite. Preserve pyproject.toml's configured worker count.
        command.extend(("--dist", "worksteal"))
    else:
        workers = min(4, len(targets))
        command.extend(("-n", str(workers), "--dist", "worksteal"))
    command.extend(targets)
    return command


@dataclass(frozen=True, slots=True)
class SuiteOutcome:
    """One pytest suite for the closing summary; a ``None`` exit code means it never ran."""

    name: str
    returncode: int | None
    result_line: str | None = None


def _report_options(report: Path | None) -> list[str]:
    return [] if report is None else ["-p", "scripts.chrys_test", f"--chrys-result-line={report}"]


def _suite_outcome(name: str, returncode: int, report: Path) -> SuiteOutcome:
    try:
        result_line = report.read_text(encoding="utf-8")
    except OSError:
        result_line = None
    return SuiteOutcome(name, returncode, result_line)


def _describe_outcome(outcome: SuiteOutcome) -> str:
    if outcome.returncode is None:
        return "not run: the suite above failed"
    if outcome.returncode == _PYTEST_NO_TESTS_COLLECTED:
        return f"no tests collected (exit {outcome.returncode})"
    return f"{outcome.result_line or 'no result line'} (exit {outcome.returncode})"


def _print_summary(outcomes: Sequence[SuiteOutcome], returncode: int) -> None:
    """End with every suite's result: each pytest summary scrolls away under the next suite's output."""

    _write()
    _write(f"Summary: {'PASSED' if returncode == 0 else 'FAILED'} (exit {returncode})")
    width = max((len(outcome.name) for outcome in outcomes), default=0)
    for outcome in outcomes:
        _write(f"  {outcome.name.ljust(width)}  {_describe_outcome(outcome)}")


def _run_pytest_targets(selection: Selection) -> int:
    regular = _prune_covered_targets(sorted(selection.regular))
    architecture = _prune_covered_targets(sorted(selection.architecture))
    outcomes: list[SuiteOutcome] = []
    returncode = 0
    with tempfile.TemporaryDirectory(prefix="chrys-pytest-reports-") as directory:
        reports = Path(directory)
        if regular:
            result = _execute_targets(regular, report=reports / "tests.txt")
            outcomes.append(_suite_outcome("tests", result.returncode, reports / "tests.txt"))
            if result.returncode == _PYTEST_NO_TESTS_COLLECTED:
                _write(
                    "No selected regular tests matched the non-integration marker scope; "
                    "continuing with architecture tests."
                )
            elif result.returncode != 0:
                returncode = result.returncode
        if architecture:
            if returncode != 0:
                outcomes.append(SuiteOutcome("architecture", None))
            else:
                result = _execute_targets(architecture, report=reports / "architecture.txt")
                outcomes.append(_suite_outcome("architecture", result.returncode, reports / "architecture.txt"))
                returncode = result.returncode
    _print_summary(outcomes, returncode)
    return returncode


def _run_full() -> int:
    _write("Running the complete local non-integration pytest scope (integration and gc_calibration excluded).")
    with tempfile.TemporaryDirectory(prefix="chrys-pytest-reports-") as directory:
        reports = Path(directory)
        regular = _run_process(
            [
                sys.executable,
                "-m",
                "pytest",
                "-q",
                "--tb=short",
                *_report_options(reports / "tests.txt"),
                "--dist",
                "worksteal",
                "-m",
                _EXCLUDED_MARKERS,
                "tests",
                "--ignore=tests/architecture",
            ]
        )
        outcomes = [_suite_outcome("tests", regular.returncode, reports / "tests.txt")]
        returncode = regular.returncode
        if returncode != 0:
            outcomes.append(SuiteOutcome("architecture", None))
        else:
            architecture = _run_process(
                [
                    sys.executable,
                    "-m",
                    "pytest",
                    "-q",
                    "--tb=short",
                    *_report_options(reports / "architecture.txt"),
                    "-m",
                    _EXCLUDED_MARKERS,
                    "-n",
                    "0",
                    "tests/architecture",
                ]
            )
            outcomes.append(_suite_outcome("architecture", architecture.returncode, reports / "architecture.txt"))
            returncode = architecture.returncode
    _print_summary(outcomes, returncode)
    return returncode


def main(argv: list[str] | None = None) -> int:
    """Run the selected local verification mode."""

    parser = build_parser()
    options = parser.parse_args(argv)
    if options.paths and options.full:
        parser.error("--paths can only be used with --smart")
    _lower_priority()
    if options.full:
        return _run_full()
    try:
        changes = changes_from_paths(options.paths) if options.paths else discover_changes()
        if not changes:
            _write("Smart Test found no branch or working-tree changes; no tests were run.")
            return 0
        _print_changes(changes)
        _write(
            f"Local feedback scope: up to {_MAX_SOURCE_IMPORT_HOPS} source import hop(s) and "
            f"{_MAX_TEST_IMPORT_HOPS} test-to-test import hop(s), plus direct tests, fixture consumers "
            "and file watches. Accepts some misses for speed; CI retains complete coverage."
        )
        noop = runtime_noop_paths(changes)
        runtime_python = any(change.path.endswith(".py") and change.path not in noop for change in changes)
        # Nothing importable changed at runtime: watches and asset rules need no import graph.
        graph = build_import_graph() if runtime_python else ImportGraph({}, {}, {}, ())
        graph.runtime_noop = noop
        _refine_source_changes(graph, changes)
        for module, seeds in sorted(graph.change_seeds.items()):
            _write(f"Function-body scope for {module}: {', '.join(sorted(seed.split('::', 1)[1] for seed in seeds))}")
        initial = select_smart_tests(changes, graph, defer_fixture_fallbacks=True)
        selection = initial
        if selection.full_reason is not None:
            _write(f"Smart Test escalated to --full: {selection.full_reason}")
            return _run_full()
        roots = fixture_collection_roots(graph, initial) if runtime_python else ()
        if roots:
            _collect_fixture_consumers(graph, roots)
            selection = select_smart_tests(changes, graph)
            if selection.full_reason is not None:
                _write(f"Smart Test escalated to --full: {selection.full_reason}")
                return _run_full()
            _collapse_complete_files(selection, graph.collected)
            _print_selection_counts(initial, selection, graph, roots)
        elif runtime_python:
            _write(
                "Skipped fixture collection: no change reaches a conftest, plugin or shared fixture "
                "(unselected tests' import errors are left to CI)."
            )
            selection = select_smart_tests(changes, graph)
        for note in sorted(selection.notes):
            _write(note)
        if not selection.regular and not selection.architecture:
            _write("Smart Test found no applicable pytest tests for these non-runtime changes.")
            return 0
        _print_targets(selection)
        return _run_pytest_targets(selection)
    except SmartTestError as error:
        sys.stderr.write(f"Smart Test error: {_display(str(error))}\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
