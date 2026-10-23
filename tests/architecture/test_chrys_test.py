# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Contract tests for the local smart-test selector."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path
from textwrap import dedent, indent

import pytest
from scripts import chrys_test

from tests.architecture import _hygiene_core
from tests.architecture import test_test_hygiene as hygiene_sweep
from tests.support.ci import CI_LINUX_ONLY
from tests.support.paths import REPO_ROOT

pytestmark = CI_LINUX_ONLY

_AGENTS_NUMERIC_ANCHOR = re.compile(r"AGENTS[.]md:\d+")


@pytest.fixture(autouse=True)
def priority_lowerings(monkeypatch: pytest.MonkeyPatch) -> list[None]:
    """Record ``main()``'s priority drop instead of lowering the test worker's own priority for good."""
    lowerings: list[None] = []
    monkeypatch.setattr(chrys_test, "_lower_priority", lambda: lowerings.append(None))
    return lowerings


def _write(root: Path, relative: str, source: str = "") -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")


def test_main_runs_below_normal_priority(monkeypatch: pytest.MonkeyPatch, priority_lowerings: list[None]) -> None:
    monkeypatch.setattr(chrys_test, "changes_from_paths", lambda paths: ())

    assert chrys_test.main(["--smart", "--paths", "README.md"]) == 0
    assert len(priority_lowerings) == 1


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX niceness")
@pytest.mark.parametrize("start", [None, chrys_test._BELOW_NORMAL_NICENESS + 5])
def test_lowered_priority_reaches_the_processes_tests_start(start: int | None) -> None:
    # A separate interpreter: lowering this worker's priority could not be undone.
    probe = dedent(
        f"""
        import os, subprocess, sys
        from scripts import chrys_test
        if {start!r} is not None:
            os.setpriority(os.PRIO_PROCESS, 0, max({start!r}, os.getpriority(os.PRIO_PROCESS, 0)))
        before = os.getpriority(os.PRIO_PROCESS, 0)
        chrys_test._lower_priority()
        child = subprocess.run(
            [sys.executable, "-c", "import os; print(os.getpriority(os.PRIO_PROCESS, 0))"],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, check=True,
        )
        print(before, child.stdout.strip())
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=REPO_ROOT,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    before, child = (int(value) for value in result.stdout.split())
    # Lowered to the floor, never raised above where it already was. Under a
    # parent already at the floor (Smart Test itself) the default case only
    # shows inheritance; at normal priority, as in CI, it shows the drop.
    assert child == max(before, chrys_test._BELOW_NORMAL_NICENESS)


def test_every_architecture_test_has_an_explicit_smart_test_classification() -> None:
    actual = {
        path.relative_to(REPO_ROOT).as_posix()
        for pattern in chrys_test._PYTEST_FILE_PATTERNS
        for path in (REPO_ROOT / "tests" / "architecture").glob(pattern)
    }
    classified = {rule.target for rule in chrys_test.ARCHITECTURE_RULES}

    assert classified == actual
    assert len(classified) == len(chrys_test.ARCHITECTURE_RULES), "architecture rules must not be duplicated"


def test_every_regular_watch_target_exists() -> None:
    missing = [rule.target for rule in chrys_test.REGULAR_RULES if not (REPO_ROOT / rule.target).exists()]

    assert missing == []


def test_ci_workflows_never_execute_the_local_smart_test_selector() -> None:
    workflow_root = REPO_ROOT / ".github" / "workflows"
    workflows = [*workflow_root.glob("*.yml"), *workflow_root.glob("*.yaml")]

    contents = [path.read_text(encoding="utf-8") for path in workflows]
    assert all("chrys_test.py --smart" not in content for content in contents)
    assert all("chrys_test.py --full" not in content for content in contents)


def test_ci_lints_the_smart_test_entrypoint() -> None:
    workflow = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    guidance = (REPO_ROOT / "AGENTS.md").read_text(encoding="utf-8")
    ruff_commands = [line for line in workflow.splitlines() if "uv run ruff " in line]
    documented = [line for line in guidance.splitlines() if "uv run ruff " in line]

    assert len(ruff_commands) == 2
    assert all("scripts/chrys_test.py" in command for command in ruff_commands)
    # The developer lint set lives in AGENTS.md; the README is product-facing.
    assert len(documented) == 2
    assert all("scripts/chrys_test.py" in command for command in documented)


def test_agents_guidance_defaults_to_task_local_smart_tests() -> None:
    guidance = (REPO_ROOT / "AGENTS.md").read_text(encoding="utf-8")

    assert "scripts/chrys_test.py --smart --paths <changed files...>" in guidance
    assert "Smart Test is a local optimization only" in guidance
    assert "CI/CD keeps its direct complete PR gates and must never use Smart Test" in guidance


def test_architecture_diagnostics_use_stable_agents_section_anchors() -> None:
    violations = [
        f"{path.relative_to(REPO_ROOT)}:{line_number}: {line.strip()}"
        for path in sorted((REPO_ROOT / "tests" / "architecture").glob("*.py"))
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
        if _AGENTS_NUMERIC_ANCHOR.search(line)
    ]

    assert violations == []


def test_public_cli_has_only_two_modes_and_task_local_paths() -> None:
    parser = chrys_test.build_parser()
    options = {option for action in parser._actions for option in action.option_strings}

    assert options == {"-h", "--help", "--smart", "--full", "--paths"}
    assert parser.parse_args(["--smart", "--paths", "src/chrys/kernel/loop.py"]).paths == ["src/chrys/kernel/loop.py"]


def test_explicit_paths_are_normalized_and_deduplicated() -> None:
    changes = chrys_test.changes_from_paths(
        ["./src/chrys/kernel/loop.py", "src/chrys/kernel/loop.py", str(REPO_ROOT / "tests/kernel/test_loop.py")]
    )

    assert changes == (
        chrys_test.Change("src/chrys/kernel/loop.py", frozenset({"changed"})),
        chrys_test.Change("tests/kernel/test_loop.py", frozenset({"changed"})),
    )


def test_missing_explicit_path_is_treated_as_deleted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(chrys_test, "REPO_ROOT", tmp_path)

    assert chrys_test.changes_from_paths(["src/chrys/kernel/removed.py"]) == (
        chrys_test.Change("src/chrys/kernel/removed.py", frozenset({"deleted"})),
    )


def test_explicit_path_outside_repository_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(chrys_test.SmartTestError, match="outside the repository"):
        chrys_test.changes_from_paths([str(tmp_path / "elsewhere.py")])


def test_name_status_parser_preserves_delete_and_both_sides_of_rename() -> None:
    parsed = chrys_test._parse_name_status(
        b"M\0tests/test_changed.py\0D\0src/chrys/gone.py\0R091\0src/chrys/old.py\0src/chrys/new.py\0"
    )

    assert parsed == [
        ("tests/test_changed.py", "changed"),
        ("src/chrys/gone.py", "deleted"),
        ("src/chrys/old.py", "renamed"),
        ("src/chrys/new.py", "renamed"),
    ]


def test_default_branch_resolution_ignores_a_dangling_origin_head(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_git_output(*args: str, allow_failure: bool = False) -> bytes | None:
        assert allow_failure is True
        if args[0] == "symbolic-ref":
            return b"refs/remotes/origin/missing\n"
        if args[0] == "for-each-ref":
            return b""
        if args[-1] == "refs/heads/main":
            return b"commit\n"
        return None

    monkeypatch.setattr(chrys_test, "_git_output", fake_git_output)

    assert chrys_test._resolve_default_branch() == "refs/heads/main"


def test_default_branch_resolution_uses_local_main_upstream_before_local_main(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_git_output(*args: str, allow_failure: bool = False) -> bytes | None:
        assert allow_failure is True
        if args[0] == "symbolic-ref":
            return None
        if args[0] == "for-each-ref":
            return b"refs/remotes/upstream/main\n"
        if args[-1] == "refs/remotes/upstream/main":
            return b"commit\n"
        return None

    monkeypatch.setattr(chrys_test, "_git_output", fake_git_output)

    assert chrys_test._resolve_default_branch() == "refs/remotes/upstream/main"


def test_git_discovery_rejects_local_main_as_its_own_only_baseline(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_git_output(*args: str, allow_failure: bool = False) -> bytes | None:
        if args == ("rev-parse", "--is-inside-work-tree"):
            assert allow_failure is True
            return b"true\n"
        if args == ("symbolic-ref", "--quiet", "HEAD"):
            assert allow_failure is True
            return b"refs/heads/main\n"
        raise AssertionError(f"unexpected Git query after unsafe baseline: {args}")

    monkeypatch.setattr(chrys_test, "_resolve_default_branch", lambda: "refs/heads/main")
    monkeypatch.setattr(chrys_test, "_git_output", fake_git_output)

    with pytest.raises(chrys_test.SmartTestError, match=r"committed local changes.*--smart --paths"):
        chrys_test.discover_changes()


def test_git_discovery_unions_branch_index_worktree_and_untracked_changes(monkeypatch: pytest.MonkeyPatch) -> None:
    outputs: dict[tuple[str, ...], bytes] = {
        ("rev-parse", "--is-inside-work-tree"): b"true\n",
        ("merge-base", "HEAD", "refs/remotes/origin/main"): b"abc123\n",
        ("diff", "--name-status", "-z", "--find-renames", "abc123...HEAD"): b"M\0src/chrys/kernel/branch.py\0",
        ("diff", "--name-status", "-z", "--find-renames"): b"D\0src/chrys/kernel/removed.py\0",
        ("diff", "--cached", "--name-status", "-z", "--find-renames"): (
            b"R100\0src/chrys/service/old.py\0src/chrys/service/new.py\0"
        ),
        ("ls-files", "--others", "--exclude-standard", "-z"): b"tests/kernel/test_new.py\0",
    }

    def fake_git_output(*args: str, allow_failure: bool = False) -> bytes | None:
        assert allow_failure is (args == ("rev-parse", "--is-inside-work-tree"))
        return outputs[args]

    monkeypatch.setattr(chrys_test, "_resolve_default_branch", lambda: "refs/remotes/origin/main")
    monkeypatch.setattr(chrys_test, "_git_output", fake_git_output)

    assert chrys_test.discover_changes() == (
        chrys_test.Change("src/chrys/kernel/branch.py", frozenset({"changed"})),
        chrys_test.Change("src/chrys/kernel/removed.py", frozenset({"deleted"})),
        chrys_test.Change("src/chrys/service/new.py", frozenset({"renamed"})),
        chrys_test.Change("src/chrys/service/old.py", frozenset({"renamed"})),
        chrys_test.Change("tests/kernel/test_new.py", frozenset({"changed"})),
    )


def test_surrogateescaped_python_path_fails_closed_with_a_displayable_name() -> None:
    path = "tests/test_\udcff.py"

    with pytest.raises(chrys_test.SmartTestError, match=r"test_\\udcff\.py"):
        chrys_test._hygiene_shard(path)


def test_hygiene_shard_selection_matches_the_architecture_sweep() -> None:
    samples = (
        "src/chrys/kernel/loop.py",
        "tests/orchestration/engine/run/test_stream_stall.py",
    )

    assert chrys_test._HYGIENE_SHARDS == _hygiene_core._SWEEP_SHARDS
    assert all(chrys_test._hygiene_shard(path) == _hygiene_core._shard_of(Path(path)) for path in samples)


def test_global_hygiene_scope_is_pinned_to_the_tui_rule() -> None:
    assert tuple(rule.__name__ for rule in hygiene_sweep._GLOBAL_SRC_HYGIENE_RULES) == (
        "_assert_tui_locale_controller_propagation_is_explicit",
    )


def test_import_graph_follows_dynamic_facade_pytest_plugins_and_ancestor_conftest(tmp_path: Path) -> None:
    _write(tmp_path, "src/chrys/__init__.py")
    _write(
        tmp_path,
        "src/chrys/pkg/__init__.py",
        "from importlib import import_module\n"
        "_EXPORTS = {'Thing': 'api'}\n"
        "def load(name):\n"
        "    return import_module(f'{__name__}.{_EXPORTS[name]}')\n",
    )
    _write(tmp_path, "src/chrys/pkg/api.py", "VALUE = 1\n")
    _write(tmp_path, "tests/__init__.py")
    _write(tmp_path, "tests/conftest.py", "pytest_plugins = ('tests.support.plugin',)\n")
    _write(tmp_path, "tests/support/__init__.py")
    _write(tmp_path, "tests/support/plugin.py", "from chrys.pkg import Thing\nGLOBAL = Thing()\n")
    _write(tmp_path, "tests/pkg/__init__.py")
    _write(tmp_path, "tests/pkg/test_consumer.py", "def test_consumer():\n    pass\n")

    graph = chrys_test.build_import_graph(tmp_path)
    affected = chrys_test._reverse_closure(graph, {"chrys.pkg.api"})

    assert graph.parse_errors == ()
    assert "chrys.pkg" in affected
    assert "tests.support.plugin" in affected
    assert "tests.conftest" in affected
    assert "tests.pkg.test_consumer" in affected

    selection = chrys_test.select_smart_tests(
        (chrys_test.Change("src/chrys/pkg/api.py", frozenset({"changed"})),),
        graph,
        root=tmp_path,
    )
    assert "tests/pkg/test_consumer.py" in selection.regular
    assert any(
        "chrys.pkg.api" in reason and "::<global>" in reason
        for reason in selection.regular["tests/pkg/test_consumer.py"]
    )


def test_nested_conftest_change_selects_every_test_below_it(tmp_path: Path) -> None:
    _write(tmp_path, "tests/__init__.py")
    _write(tmp_path, "tests/feature/__init__.py")
    _write(tmp_path, "tests/feature/conftest.py", "VALUE = 1\n")
    _write(tmp_path, "tests/feature/test_first.py", "def test_first():\n    pass\n")
    _write(tmp_path, "tests/feature/nested/__init__.py")
    _write(tmp_path, "tests/feature/nested/test_second.py", "def test_second():\n    pass\n")
    graph = chrys_test.build_import_graph(tmp_path)

    affected = chrys_test._reverse_closure(graph, chrys_test._seed_nodes(graph, "tests.feature.conftest"))

    assert {"tests.feature.test_first", "tests.feature.nested.test_second"} <= affected


def _facade_project(root: Path) -> chrys_test.ImportGraph:
    _write(root, "src/chrys/__init__.py")
    _write(root, "src/chrys/pkg/__init__.py", "from .api import First as Public\nfrom .second import Second\n")
    _write(root, "src/chrys/pkg/api.py", "from .first import First\n__all__ = ['First']\n")
    _write(root, "src/chrys/pkg/first.py", "class First: pass\n")
    _write(root, "src/chrys/pkg/second.py", "class Second: pass\n")
    _write(root, "tests/test_first.py", "from chrys.pkg import Public\n")
    _write(root, "tests/test_second.py", "from chrys.pkg import Second\n")
    _write(root, "tests/test_direct.py", "from chrys.pkg.second import Second\n")
    return chrys_test.build_import_graph(root)


def test_named_facade_exports_do_not_pull_in_unrelated_siblings(tmp_path: Path) -> None:
    graph = _facade_project(tmp_path)
    selection = chrys_test.select_smart_tests(
        (chrys_test.Change("src/chrys/pkg/first.py", frozenset({"changed"})),), graph, root=tmp_path
    )

    assert set(selection.regular) == {"tests/test_first.py"}
    assert selection.full_reason is None


@pytest.mark.parametrize("bridge", [False, True], ids=["self-expansion", "mutual-expansion"])
def test_deleted_reexport_provider_cannot_hang_import_graph(tmp_path: Path, bridge: bool) -> None:
    provider = "other" if bridge else "api"
    _write(tmp_path, "src/chrys/pkg/__init__.py", f"from .{provider} import api\n")
    if bridge:
        _write(tmp_path, "src/chrys/pkg/other.py", "from .api import api\n")
    _write(tmp_path, "tests/test_api.py", "from chrys.pkg import api\n")
    # Isolate a regression to an expanding reference so a hang cannot kill
    # the pytest worker or leave an unbounded background thread behind.
    program = dedent("""
        import sys
        from pathlib import Path
        from scripts import chrys_test

        root = Path(sys.argv[1])
        graph = chrys_test.build_import_graph(root)
        selection = chrys_test.select_smart_tests(
            (chrys_test.Change("src/chrys/pkg/api.py", frozenset({"deleted"})),), graph, root=root
        )
        assert "tests/test_api.py" in selection.regular
        assert selection.full_reason is None
        dependencies = chrys_test._dependency_references("chrys.pkg.api", set(graph.module_to_path), graph.facades)
        assert "chrys.pkg" in dependencies
        """)
    result = subprocess.run(
        [sys.executable, "-c", program, str(tmp_path)],
        cwd=REPO_ROOT,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_reexport_cycle_across_modules_falls_back_to_module_scope(tmp_path: Path) -> None:
    _write(tmp_path, "src/chrys/pkg/first.py", "from .second import Public\n")
    _write(tmp_path, "src/chrys/pkg/second.py", "from .first import Public\n")
    graph = chrys_test.build_import_graph(tmp_path)

    dependencies = chrys_test._dependency_references("chrys.pkg.first.Public", set(graph.module_to_path), graph.facades)

    assert "chrys.pkg.first" in dependencies


def test_long_reexport_chain_can_reuse_names_at_distinct_providers(tmp_path: Path) -> None:
    _write(tmp_path, "src/chrys/impl.py", "class Public: pass\n")
    for index in range(32):
        provider = f"api{index + 1}" if index < 31 else "impl"
        _write(tmp_path, f"src/chrys/api{index}.py", f"from .{provider} import Public\n")
    _write(tmp_path, "src/chrys/unrelated.py", "class Other: pass\n")
    _write(tmp_path, "tests/test_public.py", "from chrys.api0 import Public\n")
    _write(tmp_path, "tests/test_other.py", "from chrys.unrelated import Other\n")
    graph = chrys_test.build_import_graph(tmp_path)
    selection = chrys_test.select_smart_tests(
        (chrys_test.Change("src/chrys/impl.py", frozenset({"changed"})),), graph, root=tmp_path
    )

    assert set(selection.regular) == {"tests/test_public.py"}
    assert selection.full_reason is None


@pytest.mark.parametrize("path", ["src/chrys/pkg/__init__.py", "src/chrys/pkg/api.py"])
def test_direct_facade_edits_keep_their_importers(tmp_path: Path, path: str) -> None:
    graph = _facade_project(tmp_path)
    selection = chrys_test.select_smart_tests((chrys_test.Change(path, frozenset({"changed"})),), graph, root=tmp_path)

    assert "tests/test_first.py" in selection.regular
    if path.endswith("__init__.py"):
        assert {"tests/test_second.py", "tests/test_direct.py"} <= selection.regular.keys()


@pytest.mark.parametrize("statement", ["import chrys.pkg", "from chrys.pkg import *"])
def test_namespace_and_wildcard_facade_imports_keep_all_exports(tmp_path: Path, statement: str) -> None:
    _facade_project(tmp_path)
    _write(tmp_path, "tests/test_namespace.py", statement + "\n")
    graph = chrys_test.build_import_graph(tmp_path)
    selection = chrys_test.select_smart_tests(
        (chrys_test.Change("src/chrys/pkg/first.py", frozenset({"changed"})),), graph, root=tmp_path
    )

    assert "tests/test_namespace.py" in selection.regular
    assert "tests/test_second.py" not in selection.regular


def test_executable_package_initialization_keeps_sibling_consumers(tmp_path: Path) -> None:
    _facade_project(tmp_path)
    _write(tmp_path, "src/chrys/pkg/__init__.py", "from .first import First\nSTATE = First()\n")
    graph = chrys_test.build_import_graph(tmp_path)
    selection = chrys_test.select_smart_tests(
        (chrys_test.Change("src/chrys/pkg/first.py", frozenset({"changed"})),), graph, root=tmp_path
    )

    assert "tests/test_direct.py" in selection.regular


@pytest.mark.parametrize("path", ["src/chrys/pkg/impl.py", "src/chrys/pkg/clean.py"])
def test_export_submodule_name_collision_keeps_both_possible_providers(tmp_path: Path, path: str) -> None:
    _write(tmp_path, "src/chrys/pkg/__init__.py", "from .impl import clean\n")
    _write(tmp_path, "src/chrys/pkg/impl.py", "def clean(): return 1\n")
    _write(tmp_path, "src/chrys/pkg/clean.py", "VALUE = 1\n")
    _write(tmp_path, "tests/test_export.py", "from chrys.pkg import clean\n")
    _write(tmp_path, "tests/test_module.py", "import chrys.pkg.clean\n")
    _write(tmp_path, "tests/test_unrelated.py", "def test_unrelated(): pass\n")
    graph = chrys_test.build_import_graph(tmp_path)
    selection = chrys_test.select_smart_tests((chrys_test.Change(path, frozenset({"changed"})),), graph, root=tmp_path)

    assert {"tests/test_export.py", "tests/test_module.py"} <= selection.regular.keys()
    assert "tests/test_unrelated.py" not in selection.regular
    assert selection.full_reason is None


def test_runtime_imports_skip_type_checking_but_preserve_else_branch(tmp_path: Path) -> None:
    _write(tmp_path, "src/chrys/typed.py", "class Typed: pass\n")
    _write(tmp_path, "src/chrys/runtime.py", "class Runtime: pass\n")
    _write(
        tmp_path,
        "tests/test_consumer.py",
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n    from chrys.typed import Typed\n"
        "else:\n    from chrys.runtime import Runtime\n",
    )
    graph = chrys_test.build_import_graph(tmp_path)

    assert "tests.test_consumer" not in chrys_test._dependency_chains(graph, "chrys.typed")
    assert "tests.test_consumer" in chrys_test._dependency_chains(graph, "chrys.runtime")


def test_source_hop_limit_keeps_nearby_tests_and_fixture_consumers(tmp_path: Path) -> None:
    _write(tmp_path, "src/chrys/feature/seed.py", "VALUE = 1\n")
    previous = "seed"
    for index in range(chrys_test._MAX_SOURCE_IMPORT_HOPS + 2):
        name = f"layer{index}"
        _write(tmp_path, f"src/chrys/feature/{name}.py", f"from .{previous} import VALUE\ndef read(): return VALUE\n")
        _write(tmp_path, f"tests/test_{name}.py", f"from chrys.feature.{name} import VALUE\n")
        previous = name
    boundary = f"chrys.feature.layer{chrys_test._MAX_SOURCE_IMPORT_HOPS - 1}"
    _write(tmp_path, "tests/test_fixture.py", "def test_fixture(nearby): pass\n")
    graph = chrys_test.build_import_graph(tmp_path)
    graph.reverse[boundary].add("tests.support.provider::nearby")
    graph.consumers["tests.support.provider::nearby"].add("tests/test_fixture.py::test_fixture")
    selection = chrys_test.select_smart_tests(
        (chrys_test.Change("src/chrys/feature/seed.py", frozenset({"changed"})),), graph, root=tmp_path
    )

    assert selection.full_reason is None
    assert set(selection.regular) == {
        *(f"tests/test_layer{index}.py" for index in range(chrys_test._MAX_SOURCE_IMPORT_HOPS)),
        "tests/test_fixture.py::test_fixture",
    }


def test_unrelated_dynamic_fixture_does_not_expand_every_source_edit(tmp_path: Path) -> None:
    _write(tmp_path, "tests/test_near.py", "def test_near(): pass\n")
    _write(tmp_path, "tests/test_far.py", "def test_far(): pass\n")
    graph = chrys_test.ImportGraph(
        {"src/chrys/feature.py": "chrys.feature"},
        {"tests.test_near": "tests/test_near.py", "tests.test_far": "tests/test_far.py"},
        {"chrys.feature": {"tests.test_near"}, "tests.support.far::dynamic": {"tests.test_far"}},
        (),
        uncertain={"tests.support.far::dynamic": "unresolved dynamic fixture request"},
    )
    selection = chrys_test.select_smart_tests(
        (chrys_test.Change("src/chrys/feature.py", frozenset({"changed"})),), graph, root=tmp_path
    )

    assert set(selection.regular) == {"tests/test_near.py"}


@pytest.mark.parametrize("custom", [False, True], ids=["unresolved-fixture", "custom-collector"])
def test_unresolved_collection_records_keep_only_their_observed_consumers(tmp_path: Path, custom: bool) -> None:
    _write(tmp_path, "src/chrys/feature.py", "VALUE = 1\n")
    _write(tmp_path, "tests/test_feature.py", "from chrys.feature import VALUE\n")
    _write(tmp_path, "tests/test_unknown.py", "def test_unknown(): pass\ndef test_other(): pass\n")
    _write(tmp_path, "tests/test_unrelated.py", "def test_unrelated(): pass\n")
    graph = chrys_test.build_import_graph(tmp_path)
    nodeid = "tests/test_unknown.py::test_unknown"
    chrys_test._install_fixture_consumers(
        graph,
        [
            {
                "nodeid": nodeid,
                "fixtures": [] if custom else ["tests/generated/provider.py::mystery"],
                "custom": custom,
            },
            {"nodeid": "tests/test_unknown.py::test_other", "fixtures": [], "custom": False},
        ],
    )
    selection = chrys_test.select_smart_tests(
        (chrys_test.Change("src/chrys/feature.py", frozenset({"changed"})),), graph, root=tmp_path
    )

    assert set(selection.regular) == {"tests/test_feature.py", nodeid}
    assert selection.full_reason is None
    reason = (
        "custom collector has no pytest fixture metadata" if custom else "fixture implementation could not be resolved"
    )
    assert any(reason in explanation for explanation in selection.regular[nodeid])


def test_unmapped_source_fallback_uses_the_nearest_subsystem(tmp_path: Path) -> None:
    _write(tmp_path, "tests/service/mcp/test_adapter.py", "def test_adapter(): pass\n")
    selection = chrys_test.select_smart_tests(
        (chrys_test.Change("src/chrys/service/mcp/new.py", frozenset({"changed"})),),
        chrys_test.ImportGraph({}, {}, {}, ()),
        root=tmp_path,
    )

    assert set(selection.regular) == {"tests/service/mcp"}
    assert selection.full_reason is None


def test_function_body_edits_skip_unrelated_named_imports_and_keep_local_callers(tmp_path: Path) -> None:
    before = "def clean(): return 1\ndef truncate(): return 2\ndef wrapper(): return clean()\n"
    path = "src/chrys/output.py"
    _write(tmp_path, path, before.replace("return 1", "return 3"))
    for name in ("clean", "truncate", "wrapper"):
        _write(tmp_path, f"tests/test_{name}.py", f"from chrys.output import {name}\n")
    graph = chrys_test.build_import_graph(tmp_path)
    chrys_test._narrow_source_imports(graph, path, before, root=tmp_path)
    selection = chrys_test.select_smart_tests((chrys_test.Change(path, frozenset({"changed"})),), graph, root=tmp_path)

    assert set(selection.regular) == {"tests/test_clean.py", "tests/test_wrapper.py"}
    assert graph.change_seeds["chrys.output"] == {"chrys.output::clean", "chrys.output::wrapper"}


@pytest.mark.parametrize(
    "access",
    [
        "import chrys.output\n",
        "from chrys import output\n",
        "from chrys.output import *\n",
        "from unittest.mock import patch\npatch('chrys.output.clean')\n",
    ],
)
def test_function_narrowing_keeps_namespace_and_dynamic_access(tmp_path: Path, access: str) -> None:
    before = "def clean(): return 1\ndef truncate(): return 2\n"
    path = "src/chrys/output.py"
    _write(tmp_path, path, before.replace("return 1", "return 3"))
    _write(tmp_path, "tests/test_namespace.py", "from chrys.output import truncate\n" + access)
    graph = chrys_test.build_import_graph(tmp_path)
    chrys_test._narrow_source_imports(graph, path, before, root=tmp_path)

    assert "tests.test_namespace" in chrys_test._dependency_chains(graph, "chrys.output")


@pytest.mark.parametrize(
    "access",
    [
        "from .output import clean",
        "from .output import clean as scrub",
        "from chrys.facade import clean",
        "from .facade import clean as scrub",
    ],
)
def test_function_narrowing_keeps_mixed_import_routes(tmp_path: Path, access: str) -> None:
    before = "def clean(): return 1\ndef truncate(): return 2\n"
    path = "src/chrys/output.py"
    _write(tmp_path, path, before.replace("return 1", "return 3"))
    _write(tmp_path, "src/chrys/facade.py", "from .output import clean\n")
    _write(tmp_path, "src/chrys/consumer.py", "from chrys.output import truncate\n" + access + "\ndef run(): pass\n")
    _write(tmp_path, "tests/test_consumer.py", "from chrys.consumer import run\n")
    _write(tmp_path, "tests/test_unrelated.py", "from chrys.output import truncate\n")
    graph = chrys_test.build_import_graph(tmp_path)
    chrys_test._narrow_source_imports(graph, path, before, root=tmp_path)
    selection = chrys_test.select_smart_tests((chrys_test.Change(path, frozenset({"changed"})),), graph, root=tmp_path)

    assert set(selection.regular) == {"tests/test_consumer.py"}


@pytest.mark.parametrize(
    ("before", "after"),
    [
        ("def clean(): return 1\n", "def clean(value): return 1\n"),
        ("VALUE = 1\n", "VALUE = 2\n"),
        ("@register\ndef clean(): return 1\n", "@register\ndef clean(): return 2\n"),
        ("def clean(): return 1\nVALUE = clean()\n", "def clean(): return 2\nVALUE = clean()\n"),
        (
            "def clean(): return 1\ndef read(): return globals()\n",
            "def clean(): return 2\ndef read(): return globals()\n",
        ),
    ],
)
def test_structural_initialization_and_reflective_edits_keep_module_scope(before: str, after: str) -> None:
    assert chrys_test._changed_function_names(before, after) is None


def test_function_refinement_uses_branch_baseline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = "src/chrys/output.py"
    before = "def clean(): return 1\n"
    _write(tmp_path, path, "def clean(): return 2\n")
    graph = chrys_test.build_import_graph(tmp_path)
    calls: list[tuple[str, ...]] = []

    def git_output(*args: str, allow_failure: bool = False) -> bytes | None:
        assert allow_failure
        calls.append(args)
        return b"abc123\n" if args[0] == "merge-base" else before.encode()

    monkeypatch.setattr(chrys_test, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(chrys_test, "_resolve_default_branch", lambda: "refs/remotes/origin/main")
    monkeypatch.setattr(chrys_test, "_git_output", git_output)
    chrys_test._refine_source_changes(graph, (chrys_test.Change(path, frozenset({"changed"})),))

    assert calls == [("merge-base", "HEAD", "refs/remotes/origin/main"), ("show", f"abc123:{path}")]
    assert graph.change_seeds == {"chrys.output": {"chrys.output::clean"}}


@pytest.mark.parametrize("branch", [None, "refs/remotes/origin/main", "refs/heads/main"])
def test_missing_branch_baseline_does_not_refine_against_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, branch: str | None
) -> None:
    path = "src/chrys/output.py"
    _write(tmp_path, path, "def clean(): return 2\n")
    graph = chrys_test.build_import_graph(tmp_path)
    calls: list[tuple[str, ...]] = []

    def git_output(*args: str, allow_failure: bool = False) -> bytes | None:
        calls.append(args)
        return None

    monkeypatch.setattr(chrys_test, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(chrys_test, "_resolve_default_branch", lambda: branch)
    monkeypatch.setattr(chrys_test, "_local_main_is_head", lambda: True)
    monkeypatch.setattr(chrys_test, "_git_output", git_output)
    chrys_test._refine_source_changes(graph, (chrys_test.Change(path, frozenset({"changed"})),))

    assert graph.change_seeds == {}
    assert all(args[0] != "show" for args in calls)
    if branch == "refs/heads/main":
        assert calls == []


def test_changed_unimported_source_syntax_error_aborts_smart_test(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = "src/chrys/unimported.py"
    _write(tmp_path, path, "def broken(:\n")
    graph = chrys_test.build_import_graph(tmp_path)
    monkeypatch.setattr(
        chrys_test, "changes_from_paths", lambda paths: (chrys_test.Change(path, frozenset({"changed"})),)
    )
    monkeypatch.setattr(chrys_test, "build_import_graph", lambda: graph)
    monkeypatch.setattr(chrys_test, "_refine_source_changes", lambda graph, changes: None)
    monkeypatch.setattr(
        chrys_test, "_collect_fixture_consumers", lambda graph: pytest.fail("must fail before collection")
    )

    assert chrys_test.main(["--smart", "--paths", path]) == 2
    assert path in capsys.readouterr().err


def test_unrelated_syntax_error_keeps_local_selection(tmp_path: Path) -> None:
    _write(tmp_path, "src/chrys/unrelated.py", "def broken(:\n")
    _write(tmp_path, "tests/test_changed.py", "def test_changed(): pass\n")
    graph = chrys_test.build_import_graph(tmp_path)
    selection = chrys_test.select_smart_tests(
        (chrys_test.Change("tests/test_changed.py", frozenset({"changed"})),), graph, root=tmp_path
    )

    assert set(selection.regular) == {"tests/test_changed.py"}
    assert selection.notes
    assert selection.full_reason is None


def test_changed_test_selects_itself_and_only_its_hygiene_shard(tmp_path: Path) -> None:
    relative = "tests/orchestration/engine/run/test_stream_stall.py"
    _write(tmp_path, relative, "def test_stall():\n    pass\n")
    _write(tmp_path, "tests/orchestration/__init__.py")
    selection = chrys_test.select_smart_tests(
        (chrys_test.Change(relative, frozenset({"changed"})),),
        chrys_test.build_import_graph(tmp_path),
        root=tmp_path,
    )

    shard = chrys_test._hygiene_shard(relative)
    assert relative in selection.regular
    assert selection.regular[relative] == {"test file changed directly"}
    assert (
        f"tests/architecture/test_test_hygiene.py::test_hygiene_rules_hold_across_test_sources[{shard}]"
        in selection.architecture
    )
    assert not any(
        target.endswith(f"test_hygiene_rules_hold_across_test_sources[{other}]")
        for other in range(chrys_test._HYGIENE_SHARDS)
        if other != shard
        for target in selection.architecture
    )


def test_changed_pytest_suffix_pattern_selects_itself(tmp_path: Path) -> None:
    relative = "tests/service/new_feature_test.py"
    _write(tmp_path, relative, "def test_feature():\n    pass\n")
    selection = chrys_test.select_smart_tests(
        (chrys_test.Change(relative, frozenset({"changed"})),),
        chrys_test.build_import_graph(tmp_path),
        root=tmp_path,
    )

    assert relative in selection.regular


def test_unmapped_production_change_expands_to_its_subsystem(tmp_path: Path) -> None:
    _write(tmp_path, "src/chrys/kernel/new_module.py", "VALUE = 1\n")
    _write(tmp_path, "tests/kernel/__init__.py")
    selection = chrys_test.select_smart_tests(
        (chrys_test.Change("src/chrys/kernel/new_module.py", frozenset({"changed"})),),
        chrys_test.build_import_graph(tmp_path),
        root=tmp_path,
    )

    assert "tests/kernel" in selection.regular
    assert "tests/architecture/test_layering.py" in selection.architecture


def test_each_unmapped_production_change_expands_when_another_change_has_tests(tmp_path: Path) -> None:
    _write(tmp_path, "src/chrys/kernel/mapped.py", "VALUE = 1\n")
    _write(tmp_path, "src/chrys/service/unmapped.py", "VALUE = 1\n")
    _write(tmp_path, "tests/kernel/test_mapped.py", "from chrys.kernel import mapped\n")
    _write(tmp_path, "tests/kernel/__init__.py")
    _write(tmp_path, "tests/service/__init__.py")
    selection = chrys_test.select_smart_tests(
        (
            chrys_test.Change("src/chrys/kernel/mapped.py", frozenset({"changed"})),
            chrys_test.Change("src/chrys/service/unmapped.py", frozenset({"changed"})),
        ),
        chrys_test.build_import_graph(tmp_path),
        root=tmp_path,
    )

    assert "tests/kernel/test_mapped.py" in selection.regular
    assert "tests/service" in selection.regular


def test_deleted_production_module_expands_even_when_an_importer_remains(tmp_path: Path) -> None:
    _write(tmp_path, "src/chrys/service/__init__.py")
    _write(tmp_path, "tests/service/__init__.py")
    _write(tmp_path, "tests/service/test_consumer.py", "from chrys.service import removed\n")
    selection = chrys_test.select_smart_tests(
        (chrys_test.Change("src/chrys/service/removed.py", frozenset({"deleted"})),),
        chrys_test.build_import_graph(tmp_path),
        root=tmp_path,
    )

    assert "tests/service" in selection.regular


def test_deleted_nested_conftest_expands_to_its_surviving_test_directory(tmp_path: Path) -> None:
    _write(tmp_path, "tests/service/test_consumer.py", "def test_consumer():\n    pass\n")
    selection = chrys_test.select_smart_tests(
        (chrys_test.Change("tests/service/conftest.py", frozenset({"deleted"})),),
        chrys_test.build_import_graph(tmp_path),
        root=tmp_path,
    )

    assert "tests/service" in selection.regular


def test_deleted_test_module_selects_its_directory_and_surviving_importers(tmp_path: Path) -> None:
    for package in (
        "tests/__init__.py",
        "tests/app/__init__.py",
        "tests/app/tui/__init__.py",
        "tests/app/tui/behaviors/__init__.py",
        "tests/unrelated/__init__.py",
    ):
        _write(tmp_path, package)
    _write(
        tmp_path,
        "tests/app/tui/test_theme_loader.py",
        "from tests.app.tui.behaviors.test_app import helper\n",
    )
    _write(tmp_path, "tests/unrelated/test_other.py", "def test_other():\n    pass\n")
    removed = "tests/app/tui/behaviors/test_app.py"
    selection = chrys_test.select_smart_tests(
        (chrys_test.Change(removed, frozenset({"deleted"})),),
        chrys_test.build_import_graph(tmp_path),
        root=tmp_path,
    )

    assert "tests/app/tui/behaviors" in selection.regular
    assert "tests/app/tui/test_theme_loader.py" in selection.regular
    assert "tests/unrelated/test_other.py" not in selection.regular


def test_deleted_shared_test_support_module_stays_local(tmp_path: Path) -> None:
    _write(tmp_path, "tests/support/test_helper.py", "def test_helper(): pass\n")
    path = "tests/support/removed.py"
    selection = chrys_test.select_smart_tests(
        (chrys_test.Change(path, frozenset({"deleted"})),),
        chrys_test.ImportGraph({}, {}, {}, ()),
        root=tmp_path,
    )

    assert selection.full_reason is None
    assert set(selection.regular) == {"tests/support"}


def test_executable_test_support_module_selects_its_path_consumers() -> None:
    path = "tests/support/acp_stub_agent.py"
    selection = chrys_test.select_smart_tests(
        (chrys_test.Change(path, frozenset({"changed"})),),
        chrys_test.ImportGraph({}, {}, {}, ()),
    )

    assert "tests/service/acp_client" in selection.regular
    assert "tests/orchestration/sub_agents/test_acp_engine.py" in selection.regular
    assert selection.full_reason is None


def test_a_file_watch_selects_its_test_only_where_that_test_exists(tmp_path: Path) -> None:
    change = (chrys_test.Change("src/chrys/service/approval/principles.json", frozenset({"changed"})),)
    target = "tests/service/approval/test_formal_core.py"
    absent = chrys_test.select_smart_tests(change, chrys_test.ImportGraph({}, {}, {}, ()), root=tmp_path)

    _write(tmp_path, target, "def test_formal_core(): pass\n")
    present = chrys_test.select_smart_tests(change, chrys_test.ImportGraph({}, {}, {}, ()), root=tmp_path)

    assert target not in absent.regular
    assert present.regular[target] == {"regular filesystem watch matched src/chrys/service/approval/principles.json"}


def test_unmapped_test_support_module_without_import_consumers_stays_local(tmp_path: Path) -> None:
    _write(tmp_path, "tests/support/test_helper.py", "def test_helper(): pass\n")
    path = "tests/support/unimported_helper.py"
    selection = chrys_test.select_smart_tests(
        (chrys_test.Change(path, frozenset({"changed"})),),
        chrys_test.ImportGraph({}, {}, {}, ()),
        root=tmp_path,
    )

    assert selection.full_reason is None
    assert set(selection.regular) == {"tests/support"}


def test_imported_test_support_module_uses_reverse_dependencies_without_full_escalation(tmp_path: Path) -> None:
    _write(tmp_path, "tests/__init__.py")
    _write(tmp_path, "tests/support/__init__.py")
    _write(tmp_path, "tests/support/helper.py", "VALUE = 1\n")
    _write(tmp_path, "tests/service/__init__.py")
    _write(tmp_path, "tests/service/test_consumer.py", "from tests.support import helper\n")
    path = "tests/support/helper.py"
    selection = chrys_test.select_smart_tests(
        (chrys_test.Change(path, frozenset({"changed"})),),
        chrys_test.build_import_graph(tmp_path),
        root=tmp_path,
    )

    assert "tests/service/test_consumer.py" in selection.regular
    assert selection.full_reason is None


def test_architecture_only_test_support_consumer_prevents_full_escalation(tmp_path: Path) -> None:
    _write(tmp_path, "tests/__init__.py")
    _write(tmp_path, "tests/support/__init__.py")
    _write(tmp_path, "tests/support/trajectory_wait_inventory.py", "VALUE = 1\n")
    _write(tmp_path, "tests/architecture/__init__.py")
    _write(
        tmp_path,
        "tests/architecture/test_trajectory_wait_inventory.py",
        "from tests.support.trajectory_wait_inventory import VALUE\n",
    )
    path = "tests/support/trajectory_wait_inventory.py"
    selection = chrys_test.select_smart_tests(
        (chrys_test.Change(path, frozenset({"changed"})),),
        chrys_test.build_import_graph(tmp_path),
        root=tmp_path,
    )

    assert "tests/architecture/test_trajectory_wait_inventory.py" in selection.architecture
    assert selection.full_reason is None


@pytest.mark.parametrize(
    ("path", "state"),
    [
        ("scripts/i18n.py", "deleted"),
        ("scripts/gc_freeze_calibration_math.py", "renamed"),
    ],
)
def test_deleted_or_renamed_python_script_keeps_known_consumers(tmp_path: Path, path: str, state: str) -> None:
    module = chrys_test._module_for_path(path)
    assert module is not None
    graph = chrys_test.ImportGraph(
        {}, {"tests.test_script": "tests/test_script.py"}, {module: {"tests.test_script"}}, ()
    )
    _write(tmp_path, "tests/test_script.py", "def test_script(): pass\n")
    selection = chrys_test.select_smart_tests(
        (chrys_test.Change(path, frozenset({state})),),
        graph,
        root=tmp_path,
    )

    assert selection.full_reason is None
    assert "tests/test_script.py" in selection.regular


@pytest.mark.parametrize(
    "path",
    [
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
    ],
)
def test_global_test_environment_or_configuration_change_escalates_to_full(path: str) -> None:
    selection = chrys_test.select_smart_tests(
        (chrys_test.Change(path, frozenset({"changed"})),),
        chrys_test.ImportGraph({}, {}, {}, ()),
    )

    assert selection.full_reason == f"global test environment or configuration changed: {path}"


@pytest.mark.parametrize("path", ["conftest.py", "pytest.toml"])
def test_global_pytest_configuration_change_runs_tests_and_propagates_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    path: str,
) -> None:
    _write(tmp_path, path)
    _write(tmp_path, "tests/test_smoke.py", "def test_smoke():\n    pass\n")
    graph = chrys_test.build_import_graph(tmp_path)
    monkeypatch.setattr(chrys_test, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(chrys_test, "build_import_graph", lambda: graph)
    commands: list[list[str]] = []

    def failing_pytest(args: list[str], *, capture: bool = False) -> subprocess.CompletedProcess[bytes]:
        assert capture is False
        commands.append(args)
        return subprocess.CompletedProcess(args, returncode=1)

    monkeypatch.setattr(chrys_test, "_run_process", failing_pytest)

    assert chrys_test.main(["--smart", "--paths", path]) == 1
    assert len(commands) == 1
    assert "tests" in commands[0]


def test_runtime_assets_have_explicit_safe_test_scopes(tmp_path: Path) -> None:
    _write(tmp_path, "tests/app/__init__.py")
    _write(tmp_path, "tests/app/tui/__init__.py")
    selection = chrys_test.select_smart_tests(
        (chrys_test.Change("src/chrys/app/tui/chrys.tcss", frozenset({"changed"})),),
        chrys_test.ImportGraph({}, {}, {}, ()),
        root=tmp_path,
    )

    assert "tests/app/tui" in selection.regular
    assert "tests/architecture/test_tui_structure.py" in selection.architecture


def test_builtin_profiles_select_service_engine_and_application_consumers() -> None:
    path = "src/chrys/service/profiles/agents/builtins/Code.yaml"
    expected = {
        "tests/app/acp/test_session_manager_profiles.py",
        "tests/app/cli/test_workflow_validate.py",
        "tests/app/tui/behaviors/test_chrys_themes.py",
        "tests/app/tui/screens",
        "tests/orchestration/engine/build",
        "tests/service/profiles",
    }
    selection = chrys_test.select_smart_tests(
        (chrys_test.Change(path, frozenset({"changed"})),),
        chrys_test.ImportGraph({}, {}, {}, ()),
    )

    assert set(chrys_test._BUILTIN_PROFILE_TEST_TARGETS) == expected
    assert expected <= selection.regular.keys()
    assert all((REPO_ROOT / target).exists() for target in expected)


def _selects(selection: chrys_test.Selection, test_path: str) -> bool:
    return any(test_path == target or test_path.startswith(f"{target}/") for target in selection.regular)


def _select_alone(path: str) -> chrys_test.Selection:
    # No import edges: these consumers start a worker or discover a template by path. The nearby-directory
    # fallback is off, so only the rules can select them.
    return chrys_test.select_smart_tests(
        (chrys_test.Change(path, frozenset({"changed"})),),
        chrys_test.ImportGraph({}, {}, {}, ()),
        defer_fixture_fallbacks=True,
    )


@pytest.mark.parametrize(
    "path", ["src/chrys/service/workflows/worker_host.py", "src/chrys/service/workflows/sdk/_builder.py"]
)
def test_a_worker_host_or_sdk_change_selects_the_tests_that_start_a_real_worker(path: str) -> None:
    selection = _select_alone(path)

    for consumer in (
        "tests/orchestration/workflows/test_catalog.py",
        "tests/orchestration/workflows/test_preview_trust.py",
        "tests/orchestration/workflows/test_run_faults.py",
        "tests/orchestration/workflows/test_worker_diagnostics.py",
        "tests/app/cli/test_workflow.py",
        "tests/app/cli/test_workflow_validate.py",
        "tests/app/tui/screens/main/test_workflow_chrome.py",
        "tests/app/tui/widgets/test_workflow_transcript_order.py",
        "tests/service/workflows/test_py39_harness.py",
    ):
        assert (REPO_ROOT / consumer).exists()
        assert _selects(selection, consumer), consumer


@pytest.mark.parametrize(
    "path",
    [
        "src/chrys/service/workflows/builtins/demo-workflow.py",
        "src/chrys/service/workflows/builtins/demo-workflow.manifest.json",
    ],
)
def test_a_builtin_workflow_change_selects_the_tests_that_discover_it(path: str) -> None:
    selection = _select_alone(path)

    for consumer in (
        "tests/orchestration/workflows/test_catalog.py",
        "tests/app/tui/screens/main/test_workflow_run_settings.py",
        "tests/app/cli/test_workflow.py",
    ):
        assert (REPO_ROOT / consumer).exists()
        assert _selects(selection, consumer), consumer
    assert all((REPO_ROOT / target).exists() for target in chrys_test._BUILTIN_WORKFLOW_TEST_TARGETS)


@pytest.mark.parametrize(
    "changed_path",
    [
        "scripts/build.sh",
        "scripts/build.ps1",
        "scripts/build_offline_dist.sh",
        "scripts/build_offline_dist.ps1",
        "scripts/offline_wheel_overrides.txt",
        "scripts/offline_build_constraints.txt",
        "scripts/check_wheel.py",
        ".github/workflows/ci.yml",
        ".github/workflows/cd.yml",
        ".github/workflows/tag-release.yml",
    ],
)
def test_build_contract_files_select_the_cli_contract_tests(changed_path: str) -> None:
    selection = chrys_test.select_smart_tests(
        (chrys_test.Change(changed_path, frozenset({"changed"})),),
        chrys_test.ImportGraph({}, {}, {}, ()),
    )

    assert "tests/app/cli/test_app.py" in selection.regular


@pytest.mark.parametrize("path", ["tests/service/fixtures/example.json", "tests/service/fixtures/prompt.md"])
def test_unknown_test_runtime_asset_uses_nearest_existing_directory(tmp_path: Path, path: str) -> None:
    _write(tmp_path, path, "fixture payload")
    _write(tmp_path, "tests/service/test_consumer.py", "def test_consumer(): pass\n")
    selection = chrys_test.select_smart_tests(
        (chrys_test.Change(path, frozenset({"changed"})),),
        chrys_test.ImportGraph({}, {}, {}, ()),
        root=tmp_path,
    )

    assert selection.full_reason is None
    assert set(selection.regular) == {"tests/service"}


def test_runtime_asset_without_a_subsystem_scope_reports_the_gap() -> None:
    path = "src/chrys/new_layer/runtime.dat"
    selection = chrys_test.select_smart_tests(
        (chrys_test.Change(path, frozenset({"changed"})),),
        chrys_test.ImportGraph({}, {}, {}, ()),
    )

    assert selection.full_reason is None
    assert any(path in note for note in selection.notes)


@pytest.mark.parametrize(
    ("changed_path", "expected_target"),
    [
        ("AGENTS.md", "tests/architecture/test_chrys_test.py"),
        ("README.md", "tests/architecture/test_chrys_test.py"),
        ("src/chrys/app/tui/app.py", "tests/architecture/test_entrypoint_bootstrap.py"),
        ("src/chrys/app/cli/run.py", "tests/architecture/test_tui_structure.py"),
        ("src/chrys/app/cli/run.py", "tests/architecture/test_hygiene_lazy_imports.py"),
        (
            "src/chrys/app/features/session_title/generator.py",
            "tests/architecture/test_trajectory_wait_inventory.py::test_pending_retry_clear_calls_declare_a_terminal_reason",
        ),
        (
            "src/chrys/kernel/loop.py",
            "tests/architecture/test_trajectory_wait_inventory.py::test_wait_manifest_matches_source",
        ),
        (
            "src/chrys/kernel/loop.py",
            "tests/architecture/test_trajectory_wait_inventory.py::test_wait_inventory_covers_every_explicit_and_implicit_async_wait",
        ),
    ],
)
def test_filesystem_scanning_architecture_guards_watch_their_complete_scope(
    tmp_path: Path,
    changed_path: str,
    expected_target: str,
) -> None:
    _write(tmp_path, "tests/app/__init__.py")
    selection = chrys_test.select_smart_tests(
        (chrys_test.Change(changed_path, frozenset({"changed"})),),
        chrys_test.ImportGraph({}, {}, {}, ()),
        root=tmp_path,
    )

    assert expected_target in selection.architecture


@pytest.mark.parametrize(
    ("changed_path", "expected_target"),
    [
        ("src/chrys/app/tui/behaviors/submit_state.py", "tests/app/tui/behaviors/test_chrys_themes.py"),
        ("src/chrys/app/tui/behaviors/submit_state.py", "tests/app/tui/i18n/test_bindings.py"),
        (
            "src/chrys/app/tui/screens/dialogs/confirm.py",
            "tests/app/tui/screens/test_modal_insert_clipboard.py",
        ),
        (
            "src/chrys/foundation/patches/textual_option_list.py",
            "tests/app/tui/widgets/editor/test_highlighter.py",
        ),
        (
            "src/chrys/app/tui/widgets/editor/highlighter.py",
            "tests/app/tui/widgets/editor/test_highlighter.py",
        ),
    ],
)
def test_regular_filesystem_scanners_watch_their_complete_scope(
    tmp_path: Path,
    changed_path: str,
    expected_target: str,
) -> None:
    _write(tmp_path, "tests/app/__init__.py")
    _write(tmp_path, expected_target)
    selection = chrys_test.select_smart_tests(
        (chrys_test.Change(changed_path, frozenset({"changed"})),),
        chrys_test.ImportGraph({}, {}, {}, ()),
        root=tmp_path,
    )

    # The target exists here, so a source mirror could also pick it: the watch must be why.
    assert f"regular filesystem watch matched {changed_path}" in selection.regular.get(expected_target, set())


def test_target_output_caps_reasons_without_discarding_selection_evidence(
    capsys: pytest.CaptureFixture[str],
) -> None:
    selection = chrys_test.Selection()
    target = "tests/service/test_consumer.py"
    input_reasons = ("reason-5", "reason-2", "reason-4", "reason-0", "reason-3", "reason-1")
    for reason in input_reasons:
        selection.add(target, reason)

    chrys_test._print_targets(selection)

    assert selection.regular[target] == set(input_reasons)
    assert capsys.readouterr().out.splitlines() == [
        "Smart Test selected 1 pytest target(s):",
        f"  {target}",
        "    <- reason-0; reason-1; reason-2; +3 more",
    ]


def test_no_regular_matches_does_not_prevent_architecture_tests(monkeypatch: pytest.MonkeyPatch) -> None:
    selection = chrys_test.Selection()
    selection.add("tests/service/mcp/test_cache_e2e.py", "changed directly")
    selection.add("tests/architecture/test_test_layout.py", "test tree changed")
    return_codes = iter((chrys_test._PYTEST_NO_TESTS_COLLECTED, 0))
    commands: list[list[str]] = []

    def fake_run(args: list[str], *, capture: bool = False) -> object:
        assert capture is False
        commands.append(args)
        return type("Result", (), {"returncode": next(return_codes)})()

    monkeypatch.setattr(chrys_test, "_run_process", fake_run)

    assert chrys_test._run_pytest_targets(selection) == 0
    assert len(commands) == 2


def test_covered_targets_are_pruned_before_pytest_and_directories_keep_configured_parallelism() -> None:
    targets = [
        "tests/service",
        "tests/service/test_api.py",
        "tests/service/test_api.py::test_one",
        "tests/kernel/test_loop.py",
        "tests/kernel/test_loop.py::test_one",
    ]

    pruned = chrys_test._prune_covered_targets(targets)

    assert pruned == ["tests/service", "tests/kernel/test_loop.py"]
    command = chrys_test._pytest_command(pruned, architecture=False)
    assert "-n" not in command
    assert "--dist" in command


def test_full_mode_uses_worksteal_then_runs_architecture_without_xdist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[list[str]] = []

    def fake_run(args: list[str], *, capture: bool = False) -> object:
        assert capture is False
        commands.append(args)
        return type("Result", (), {"returncode": 0})()

    monkeypatch.setattr(chrys_test, "_run_process", fake_run)

    assert chrys_test._run_full() == 0
    assert commands[0][commands[0].index("--dist") + 1] == "worksteal"
    assert "-n" not in commands[0]
    assert commands[1][commands[1].index("-n") + 1] == "0"


def test_architecture_directory_is_classified_as_architecture() -> None:
    selection = chrys_test.Selection()

    selection.add("tests/architecture", "architecture helper was renamed")

    assert "tests/architecture" in selection.architecture
    assert not selection.regular


@pytest.mark.parametrize("architecture", [False, True])
def test_long_target_transport_preserves_every_argument_and_cleans_up(
    monkeypatch: pytest.MonkeyPatch, architecture: bool
) -> None:
    from _pytest.config.argparsing import Parser

    targets = [f"tests/space dir/test_file_{i}.py::test_case[value with spaces]" for i in range(181)]
    files: list[Path] = []

    def run(args: list[str], *, capture: bool = False) -> subprocess.CompletedProcess[bytes]:
        assert capture is False
        assert args[-1].startswith("@")
        path = Path(args[-1][1:])
        files.append(path)
        assert Parser(_ispytest=True).parse([args[-1]]).file_or_dir == targets
        assert args[:-1] == chrys_test._pytest_command(targets, architecture=architecture)[: -len(targets)]
        return subprocess.CompletedProcess(args, 1)

    monkeypatch.setattr(chrys_test, "_run_process", run)
    assert chrys_test._execute_targets(targets, architecture=architecture).returncode == 1
    assert files and all(not path.exists() for path in files)


def test_argument_file_runs_exact_nodes_without_collecting_sibling_files(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytester.makepyfile(
        test_many="import pytest\n@pytest.mark.parametrize('value', range(182))\ndef test_value(value): assert value < 181",
        test_unselected="raise AssertionError('unselected sibling must not be collected')",
    )
    _write(pytester.path, "space dir/test_space.py", "def test_space(): pass\n")
    targets = [f"test_many.py::test_value[{value}]" for value in range(181)] + ["space dir/test_space.py"]

    def run(args: list[str], *, capture: bool = False) -> subprocess.CompletedProcess[bytes]:
        assert args[:3] == [sys.executable, "-m", "pytest"]
        result = pytester.runpytest_subprocess(*args[3:])
        result.assert_outcomes(passed=182)
        return subprocess.CompletedProcess(args, result.ret)

    monkeypatch.setattr(chrys_test, "_run_process", run)
    assert chrys_test._execute_targets(targets, architecture=False).returncode == 0


def test_character_limit_also_uses_exact_argument_file(monkeypatch: pytest.MonkeyPatch) -> None:
    targets = [f"tests/test_long.py::test_case[{'x' * 11_000}{i}]" for i in range(2)]

    def run(args: list[str], *, capture: bool = False) -> subprocess.CompletedProcess[bytes]:
        assert Path(args[-1][1:]).read_text().splitlines() == targets
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(chrys_test, "_run_process", run)
    assert chrys_test._execute_targets(targets, architecture=False).returncode == 0


def _fixture_project(pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = pytester.path
    _write(root, "scripts/chrys_test.py", (REPO_ROOT / "scripts/chrys_test.py").read_text(encoding="utf-8"))
    _write(root, "pytest.ini", "[pytest]\npythonpath = src .\n")
    for package in ("tests", "tests/support", "src/chrys", "src/chrys/kernel"):
        _write(root, f"{package}/__init__.py")
    _write(root, "tests/conftest.py", "pytest_plugins: tuple[str, ...] = ('tests.support.providers',)\n")
    _write(root, "src/chrys/kernel/engine.py", "def create(): return 1\n")
    _write(root, "src/chrys/kernel/unrelated.py", "def create(): return 2\n")
    _write(
        root,
        "tests/support/providers.py",
        """import pytest
def build_engine():
    from chrys.kernel import engine
    return engine.create()
@pytest.fixture
def engine_fixture():
    raise AssertionError('collection must not set up fixtures')
    return build_engine()
@pytest.fixture
def other_fixture():
    from chrys.kernel import unrelated
    return unrelated.create()
""",
    )
    monkeypatch.setattr(chrys_test, "REPO_ROOT", root)
    return root


def _fixture_selection(root: Path) -> tuple[chrys_test.ImportGraph, chrys_test.Selection]:
    graph = chrys_test.build_import_graph(root)
    chrys_test._collect_fixture_consumers(graph)
    selection = chrys_test.select_smart_tests(
        (chrys_test.Change("src/chrys/kernel/engine.py", frozenset({"changed"})),), graph, root=root
    )
    assert selection.full_reason is None
    return graph, selection


def test_fixture_collection_finds_consumers_overrides_parents_and_parametrization(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _fixture_project(pytester, monkeypatch)
    _write(
        root,
        "tests/test_consumers.py",
        """import pytest
@pytest.fixture
def indirect_fixture(engine_fixture): return engine_fixture
def test_explicit(engine_fixture): pass
def test_transitive(indirect_fixture): pass
@pytest.mark.usefixtures('engine_fixture')
def test_marked(): pass
def test_plain(): pass
def test_other(other_fixture): pass
@pytest.mark.parametrize('engine_fixture', [1])
def test_shadowed(engine_fixture): pass
@pytest.mark.parametrize('engine_fixture', [1], indirect=True)
def test_parametrized(engine_fixture): pass
""",
    )
    _write(
        root,
        "tests/test_override.py",
        """import pytest
@pytest.fixture
def engine_fixture(): return 0
def test_overridden(engine_fixture): pass
""",
    )
    _write(
        root,
        "tests/test_parent.py",
        """import pytest
@pytest.fixture
def engine_fixture(engine_fixture): return engine_fixture
def test_parent(engine_fixture): pass
""",
    )
    _write(
        root,
        "tests/nested/conftest.py",
        """import pytest
@pytest.fixture(autouse=True)
def auto_engine(engine_fixture): pass
""",
    )
    _write(root, "tests/nested/test_auto.py", "def test_auto(): pass\n")
    graph, selection = _fixture_selection(root)
    assert set(selection.regular) == {
        "tests/test_consumers.py::test_explicit",
        "tests/test_consumers.py::test_transitive",
        "tests/test_consumers.py::test_marked",
        "tests/test_consumers.py::test_parametrized[1]",
        "tests/test_parent.py::test_parent",
        "tests/nested/test_auto.py::test_auto",
    }
    assert len(graph.collected) == 10
    assert not graph.uncertain  # built-in fixtures in a repository's .venv are not unknown local fixtures
    assert any("build_engine" in reason for reason in selection.regular["tests/test_consumers.py::test_explicit"])


@pytest.mark.parametrize(
    "global_code",
    [
        "GLOBAL = build_engine()\n",
        "from chrys.kernel import engine\n",  # providers can execute global side effects on import
        """
        from typing import TYPE_CHECKING
        if TYPE_CHECKING:
            pass
        else:
            GLOBAL = build_engine()
        """,
        """
        def register(cls):
            cls.initialize()
            return cls
        @register
        class Registration:
            @staticmethod
            def initialize():
                build_engine()
        """,
        """
        def pytest_runtest_setup(item):
            build_engine()
        """,
        """
        if True:
            def pytest_runtest_setup(item):
                build_engine()
        """,
        """
        try:
            GLOBAL = build_engine()
        finally:
            pass
        """,
        """
        from contextlib import nullcontext
        with nullcontext():
            GLOBAL = build_engine()
        """,
        """
        @pytest.fixture(autouse=True)
        def always():
            build_engine()
        """,
    ],
)
def test_global_initialization_hooks_and_autouse_keep_their_consumers(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, global_code: str
) -> None:
    root = _fixture_project(pytester, monkeypatch)
    provider = root / "tests/support/providers.py"
    provider.write_text(provider.read_text() + dedent(global_code))
    _write(root, "tests/test_plain.py", "def test_plain(): pass\n")
    _, selection = _fixture_selection(root)
    assert any(chrys_test._target_covers(target, "tests/test_plain.py::test_plain") for target in selection.regular)


@pytest.mark.parametrize("body", ["request.getfixturevalue('engine_fixture')", "globals()['build_engine']()"])
def test_unresolved_fixture_and_helper_calls_expand_only_their_consumers_with_a_reason(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, body: str
) -> None:
    root = _fixture_project(pytester, monkeypatch)
    provider = root / "tests/support/providers.py"
    provider.write_text(provider.read_text() + f"\n@pytest.fixture\ndef dynamic(request):\n    return {body}\n")
    _write(
        root,
        "tests/test_dynamic.py",
        "def test_dynamic(dynamic): pass\ndef test_known(engine_fixture): pass\ndef test_plain(): pass\n",
    )
    _, selection = _fixture_selection(root)
    assert set(selection.regular) == {"tests/test_dynamic.py::test_dynamic", "tests/test_dynamic.py::test_known"}
    assert any(
        "local fallback: unresolved" in reason for reason in selection.regular["tests/test_dynamic.py::test_dynamic"]
    )


def test_complete_collection_rejects_import_errors_outside_the_initial_selection(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _fixture_project(pytester, monkeypatch)
    _write(root, "tests/test_consumer.py", "def test_consumer(engine_fixture): pass\n")
    _write(root, "tests/test_broken.py", "raise RuntimeError('collection sentinel')\n")
    graph = chrys_test.build_import_graph(root)
    with pytest.raises(chrys_test.SmartTestError, match="collection sentinel"):
        chrys_test._collect_fixture_consumers(graph)


@pytest.mark.parametrize("missing", ["all", "engine_fixture"])
def test_fixture_collection_rejects_unobserved_definitions(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    root = _fixture_project(pytester, monkeypatch)
    conftest = root / "tests/conftest.py"
    conftest.write_text(
        conftest.read_text()
        + dedent(f"""
            def pytest_collection_modifyitems():
                from _pytest import fixtures
                original = fixtures.traverse_fixture_closure

                def broken_traversal(initialnames, *, getfixturedefs):
                    def omit_definition(name):
                        if {missing!r} in ('all', name):
                            return None
                        return getfixturedefs(name)
                    return original(initialnames, getfixturedefs=omit_definition)

                fixtures.traverse_fixture_closure = broken_traversal
            """)
    )
    _write(root, "tests/test_consumer.py", "def test_consumer(engine_fixture, tmp_path): pass\n")
    graph = chrys_test.build_import_graph(root)
    with pytest.raises(chrys_test.SmartTestError, match="Incomplete fixture traversal") as caught:
        chrys_test._collect_fixture_consumers(graph)
    assert "tests/test_consumer.py::test_consumer" in str(caught.value)
    assert "engine_fixture" in str(caught.value)


@pytest.mark.parametrize("parameters", ["", "tmp_path"])
def test_fixture_collection_allows_tests_without_project_fixture_consumers(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, parameters: str
) -> None:
    root = _fixture_project(pytester, monkeypatch)
    _write(root, "tests/test_plain.py", f"def test_plain({parameters}): pass\n")
    graph = chrys_test.build_import_graph(root)
    chrys_test._collect_fixture_consumers(graph)
    assert graph.collected == ("tests/test_plain.py::test_plain",)
    assert not graph.consumers
    assert not graph.uncertain


def test_string_dependencies_ignore_message_keys_but_keep_dynamic_imports_and_patch_targets(tmp_path: Path) -> None:
    _write(tmp_path, "src/chrys/pkg/retry.py")
    _write(tmp_path, "src/chrys/pkg/provider.py")
    _write(tmp_path, "src/chrys/pkg/bindings.py", "KEY = 'retry.stream_stalled'\n")
    _write(
        tmp_path, "tests/test_dynamic.py", "from importlib import import_module as load\nload('chrys.pkg.provider')\n"
    )
    _write(
        tmp_path,
        "tests/test_patch.py",
        "from unittest.mock import patch as replace\nreplace('chrys.pkg.provider.value')\n",
    )
    _write(
        tmp_path,
        "tests/test_monkeypatch.py",
        "def test_patch(monkeypatch):\n    monkeypatch.setattr('chrys.pkg.provider.value', 1)\n",
    )
    graph = chrys_test.build_import_graph(tmp_path)
    assert "chrys.pkg.bindings" not in chrys_test._reverse_closure(graph, {"chrys.pkg.retry"})
    assert {"tests.test_dynamic", "tests.test_patch", "tests.test_monkeypatch"} <= chrys_test._reverse_closure(
        graph, {"chrys.pkg.provider"}
    )


def test_cross_module_helpers_preserve_fixture_and_initialization_dependencies(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _fixture_project(pytester, monkeypatch)
    _write(
        root,
        "tests/support/helpers.py",
        """def build():
    from chrys.kernel import engine
    return engine.create()
""",
    )
    _write(
        root,
        "tests/support/providers.py",
        """import pytest
from tests.support.helpers import build
@pytest.fixture
def engine_fixture(): return build()
""",
    )
    _write(root, "tests/test_helper.py", "def test_helper(engine_fixture): pass\ndef test_plain(): pass\n")
    _, selection = _fixture_selection(root)
    # An ordinary module-level helper import is deliberately conservative:
    # its provider can initialize shared state. Moving it inside the fixture
    # makes the dependency opt-in while preserving the local helper walk.
    assert "tests/test_helper.py" in selection.regular
    _write(
        root,
        "tests/support/providers.py",
        """import pytest
@pytest.fixture
def engine_fixture():
    from tests.support.helpers import build
    return build()
""",
    )
    _, selection = _fixture_selection(root)
    assert set(selection.regular) == {"tests/test_helper.py::test_helper"}


def test_dynamic_plugin_registration_keeps_observed_fixture_consumers(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _fixture_project(pytester, monkeypatch)
    _write(root, "tests/conftest.py", "PLUGINS = ('tests.support.providers',)\npytest_plugins = PLUGINS\n")
    _write(root, "tests/test_plain.py", "def test_plain(): pass\ndef test_known(engine_fixture): pass\n")
    _, selection = _fixture_selection(root)
    assert any(chrys_test._target_covers(target, "tests/test_plain.py::test_known") for target in selection.regular)
    assert not any("dynamic pytest_plugins" in reason for reasons in selection.regular.values() for reason in reasons)


@pytest.mark.parametrize("reason", ["fixture dependency of provider", "local fallback: unresolved helper"])
def test_selection_diagnostics_count_additions_without_duplicate_file_node_targets(
    capsys: pytest.CaptureFixture[str],
    reason: str,
) -> None:
    initial = chrys_test.Selection()
    initial.add("tests/test_a.py", "direct import")
    final = chrys_test.Selection()
    final.add("tests/test_a.py", "direct import")
    final.add("tests/test_a.py::test_one", "fixture dependency of provider")
    final.add("tests/test_b.py::test_two", reason)
    graph = chrys_test.ImportGraph({}, {}, {}, ())
    graph.collected = ("tests/test_a.py::test_one", "tests/test_b.py::test_two", "tests/test_b.py::test_other")
    chrys_test._print_selection_counts(initial, final, graph)
    output = capsys.readouterr().out
    assert "Initial selection: 1 targets / 1 collected tests" in output
    assert "Fixture/fallback additions: 1 tests" in output
    assert "Final execution scope: 2 targets / 2 of 3" in output


def test_long_argument_file_rejects_unrepresentable_newline_paths() -> None:
    targets = [f"tests/test_{i}.py" for i in range(181)] + ["tests/new\nline.py"]
    with pytest.raises(chrys_test.SmartTestError, match="newline path"):
        chrys_test._execute_targets(targets, architecture=False)


@pytest.mark.parametrize(
    "command",
    [
        "subprocess.Popen([sys.executable, '-m', 'chrys.pkg.worker'])",
        "command = (sys.executable, '-I', '-m', 'chrys.pkg.worker')\nsubprocess.run(command)",
        "asyncio.create_subprocess_exec(sys.executable, '-m', 'chrys.pkg.worker')",
        "command = ['python3.14', '-W', 'error', '-m', 'chrys.pkg.worker']",
        "from sys import executable as python\nsubprocess.Popen(args=[python, '-m', 'chrys.pkg.worker'])",
    ],
)
def test_python_module_subprocesses_keep_transitive_source_consumers(tmp_path: Path, command: str) -> None:
    _write(tmp_path, "src/chrys/pkg/engine.py")
    _write(tmp_path, "src/chrys/pkg/worker.py", "from . import engine\n")
    _write(tmp_path, "tests/test_child.py", f"import sys, subprocess, asyncio\n{command}\n")
    graph = chrys_test.build_import_graph(tmp_path)
    selection = chrys_test.select_smart_tests(
        (chrys_test.Change("src/chrys/pkg/engine.py", frozenset({"changed"})),), graph, root=tmp_path
    )
    assert "tests/test_child.py" in selection.regular


def test_changed_cli_dispatcher_selects_the_real_stdio_subprocess_test(tmp_path: Path) -> None:
    target = "tests/app/acp/test_stdio.py"
    _write(tmp_path, "src/chrys/app/cli/app.py")
    _write(tmp_path, target, (REPO_ROOT / target).read_text(encoding="utf-8"))
    selection = chrys_test.select_smart_tests(
        (chrys_test.Change("src/chrys/app/cli/app.py", frozenset({"changed"})),),
        chrys_test.build_import_graph(tmp_path),
        root=tmp_path,
    )
    assert target in selection.regular


@pytest.mark.parametrize(
    "command",
    [
        "['git', 'commit', '-m', 'chrys.pkg.engine']",
        "[sys.executable, '-c', 'pass', '-m', 'chrys.pkg.engine']",
        "['python3', 'script.py', '-m', 'chrys.pkg.engine']",
    ],
)
def test_non_module_command_arguments_do_not_become_module_dependencies(tmp_path: Path, command: str) -> None:
    _write(tmp_path, "src/chrys/pkg/engine.py")
    _write(tmp_path, "tests/test_command.py", f"import sys\ncommand = {command}\n")
    graph = chrys_test.build_import_graph(tmp_path)
    assert "tests.test_command" not in chrys_test._reverse_closure(graph, {"chrys.pkg.engine"})


@pytest.mark.parametrize("layout", ["if", "else", "try", "except", "match", "with", "for", "while"])
def test_conditional_helpers_keep_fixture_consumers_without_losing_branch_definitions(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, layout: str
) -> None:
    root = _fixture_project(pytester, monkeypatch)
    helper = dedent("""
        def build_engine():
            from chrys.kernel import engine
            return engine.create()
        """).strip()
    alternate = dedent("""
        def build_engine():
            return 0
        """).strip()
    bodies = {
        "if": """
            if True:
                {helper}
            else:
                {alternate}
            """,
        "else": """
            if False:
                {alternate}
            else:
                {helper}
            """,
        "try": """
            try:
                {helper}
            except RuntimeError:
                {alternate}
            """,
        "except": """
            try:
                raise RuntimeError
            except RuntimeError:
                {helper}
            """,
        "match": """
            match True:
                case True:
                    {helper}
                case _:
                    {alternate}
            """,
        "with": """
            from contextlib import nullcontext
            with nullcontext():
                {helper}
            """,
        "for": """
            for _ in (1,):
                {helper}
            """,
        "while": """
            first = True
            while first:
                {helper}
                first = False
            """,
    }
    definition_indent = "        " if layout == "match" else "    "
    body = dedent(bodies[layout]).format(
        helper=indent(helper, definition_indent).lstrip(),
        alternate=indent(alternate, definition_indent).lstrip(),
    )
    _write(
        root,
        "tests/support/providers.py",
        "import pytest\n"
        + body
        + """
def unrelated_factory():
    def build_engine():
        from chrys.kernel import unrelated
        return unrelated.create()
    return build_engine
class Unrelated:
    def build_engine(self):
        from chrys.kernel import unrelated
        return unrelated.create()
@pytest.fixture
def engine_fixture(): return build_engine()
""",
    )
    _write(root, "tests/test_consumer.py", "def test_consumer(engine_fixture): pass\ndef test_plain(): pass\n")
    # A known direct consumer prevents a broad missing-consumer fallback from
    # concealing a dropped fixture edge in the reproducer.
    _write(root, "tests/test_direct.py", "from chrys.kernel import engine, unrelated\ndef test_direct(): pass\n")
    graph, selection = _fixture_selection(root)
    assert "tests/test_consumer.py::test_consumer" in selection.regular
    assert "tests/test_consumer.py" not in selection.regular
    assert "tests/test_consumer.py::test_plain" not in selection.regular
    other_selection = chrys_test.select_smart_tests(
        (chrys_test.Change("src/chrys/kernel/unrelated.py", frozenset({"changed"})),), graph, root=root
    )
    assert other_selection.full_reason is None
    assert not any(target.startswith("tests/test_consumer.py") for target in other_selection.regular)
