# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Contract tests for Smart Test's narrow local scope: the most related tests, quickly."""

from __future__ import annotations

from pathlib import Path

import pytest
from scripts import chrys_test

from tests.support.ci import CI_LINUX_ONLY

pytestmark = CI_LINUX_ONLY


def _write(root: Path, relative: str, source: str = "") -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")


def _changed(path: str) -> chrys_test.Change:
    return chrys_test.Change(path, frozenset({"changed"}))


def _branch_baseline(monkeypatch: pytest.MonkeyPatch, root: Path, before: dict[str, str]) -> None:
    def git_output(*args: str, allow_failure: bool = False) -> bytes | None:
        if args[0] == "merge-base":
            return b"base\n"
        if args[0] == "show":
            text = before.get(args[1].split(":", 1)[1])
            return None if text is None else text.encode()
        raise AssertionError(f"unexpected git call: {args}")

    monkeypatch.setattr(chrys_test, "REPO_ROOT", root)
    monkeypatch.setattr(chrys_test, "_resolve_default_branch", lambda: "refs/remotes/origin/main")
    monkeypatch.setattr(chrys_test, "_git_output", git_output)


@pytest.mark.parametrize(
    ("before", "after", "noop"),
    [
        ("VALUE = 1\n", "# why VALUE is one\nVALUE = 1  # same\n\n", True),
        ("def f(x):\n    return x\n", "def f(x):  # note\n\n    return (x)\n", True),
        ('"""Old."""\n', '"""New."""\n', False),  # docstrings can become runtime data
        ("VALUE = 1\n", "VALUE = 2\n", False),
        (None, "VALUE = 1\n", False),  # a new file has no baseline to equal
        # The same UTF-8 bytes decode to another literal under a new encoding cookie.
        ("# coding: utf-8\nTEXT = 'é'\n", "# coding: latin-1\nTEXT = 'é'\n", False),
    ],
    ids=["comment", "formatting", "docstring", "value", "new-file", "encoding-cookie"],
)
def test_only_an_ast_equal_edit_is_a_runtime_noop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, before: str | None, after: str, noop: bool
) -> None:
    path = "src/chrys/kernel/_types.py"
    _write(tmp_path, path, after)
    _branch_baseline(monkeypatch, tmp_path, {} if before is None else {path: before})

    assert chrys_test.runtime_noop_paths((_changed(path),), root=tmp_path) == (
        frozenset({path}) if noop else frozenset()
    )


def test_deleted_paths_and_missing_baselines_are_never_runtime_noops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = "src/chrys/kernel/_types.py"
    _write(tmp_path, path, "VALUE = 1\n")
    _branch_baseline(monkeypatch, tmp_path, {path: "VALUE = 1\n"})
    deleted = chrys_test.Change(path, frozenset({"deleted"}))
    assert chrys_test.runtime_noop_paths((deleted,), root=tmp_path) == frozenset()

    monkeypatch.setattr(chrys_test, "_resolve_default_branch", lambda: "refs/heads/main")
    monkeypatch.setattr(chrys_test, "_local_main_is_head", lambda: True)
    assert chrys_test.runtime_noop_paths((_changed(path),), root=tmp_path) == frozenset()


def test_runtime_noop_source_keeps_watches_and_mirror_but_skips_consumers_and_fallback(tmp_path: Path) -> None:
    path = "src/chrys/kernel/_types.py"
    _write(tmp_path, path, "VALUE = 1\n")
    _write(tmp_path, "tests/kernel/test_types.py", "def test_types(): pass\n")
    _write(tmp_path, "tests/kernel/test_other.py", "def test_other(): pass\n")
    _write(tmp_path, "tests/test_consumer.py", "from chrys.kernel._types import VALUE\n")
    graph = chrys_test.build_import_graph(tmp_path)

    runtime = chrys_test.select_smart_tests((_changed(path),), graph, root=tmp_path)
    assert {"tests/test_consumer.py", "tests/kernel/test_types.py"} <= set(runtime.regular)

    graph.runtime_noop = frozenset({path})
    noop = chrys_test.select_smart_tests((_changed(path),), graph, root=tmp_path)
    # The private module's mirror test drops the leading underscore.
    assert set(noop.regular) == {"tests/kernel/test_types.py"}
    assert "tests/architecture/test_layering.py" in noop.architecture
    assert any("No runtime change" in note for note in noop.notes)


def test_noop_conftest_stays_local_but_global_configuration_still_runs_everything(tmp_path: Path) -> None:
    _write(tmp_path, "tests/conftest.py", "VALUE = 1\n")
    graph = chrys_test.build_import_graph(tmp_path)
    graph.runtime_noop = frozenset({"tests/conftest.py"})

    alone = chrys_test.select_smart_tests((_changed("tests/conftest.py"),), graph, root=tmp_path)
    assert alone.full_reason is None

    mixed = chrys_test.select_smart_tests((_changed("tests/conftest.py"), _changed("uv.lock")), graph, root=tmp_path)
    assert mixed.full_reason is not None
    assert "uv.lock" in mixed.full_reason
    assert "conftest" not in mixed.full_reason


def test_test_module_imports_stop_after_one_test_to_test_hop(tmp_path: Path) -> None:
    _write(tmp_path, "src/chrys/feature.py", "VALUE = 1\n")
    _write(tmp_path, "tests/test_direct.py", "from chrys.feature import VALUE\n")
    _write(tmp_path, "tests/test_reuser.py", "from tests.test_direct import VALUE\n")
    _write(tmp_path, "tests/test_far.py", "from tests.test_reuser import VALUE\n")
    graph = chrys_test.build_import_graph(tmp_path)

    selection = chrys_test.select_smart_tests((_changed("src/chrys/feature.py"),), graph, root=tmp_path)

    assert set(selection.regular) == {"tests/test_direct.py", "tests/test_reuser.py"}


def test_a_helper_name_that_is_also_imported_keeps_its_importers_module_wide(tmp_path: Path) -> None:
    _write(tmp_path, "src/chrys/feature.py", "def helper():\n    return 1\n")
    _write(
        tmp_path,
        "tests/support/helpers.py",
        "import sys\nif sys.platform == 'win32':\n    def helper():\n        return 2\n"
        "else:\n    from chrys.feature import helper\ndef twice():\n    return helper() * 2\n",
    )
    _write(tmp_path, "tests/test_user.py", "from tests.support.helpers import helper\n")
    graph = chrys_test.build_import_graph(tmp_path)

    selection = chrys_test.select_smart_tests((_changed("src/chrys/feature.py"),), graph, root=tmp_path)

    assert "tests/test_user.py" in selection.regular


def test_helper_scope_edges_cost_no_test_hop_even_in_a_test_named_module(tmp_path: Path) -> None:
    _write(tmp_path, "src/chrys/feature.py", "VALUE = 1\n")
    _write(tmp_path, "tests/test_direct.py", "from chrys.feature import VALUE\n")
    _write(
        tmp_path,
        "tests/support/test_helpers.py",
        "from tests.test_direct import VALUE\ndef helper():\n    return VALUE\n",
    )
    _write(tmp_path, "tests/test_user.py", "from tests.support.test_helpers import helper\n")
    graph = chrys_test.build_import_graph(tmp_path)

    selection = chrys_test.select_smart_tests((_changed("src/chrys/feature.py"),), graph, root=tmp_path)

    assert "tests/test_user.py" in selection.regular


def test_source_imports_stop_after_one_source_hop(tmp_path: Path) -> None:
    # Pinned literally: the hop limit is the main precision trade-off.
    _write(tmp_path, "src/chrys/feature.py", "VALUE = 1\n")
    # Each module defines its own name: a re-exported name resolves to its definer.
    _write(tmp_path, "src/chrys/near.py", "from chrys.feature import VALUE\ndef near():\n    return VALUE\n")
    _write(tmp_path, "src/chrys/far.py", "from chrys.near import near\ndef far():\n    return near()\n")
    _write(tmp_path, "tests/test_near.py", "from chrys.near import near\n")
    _write(tmp_path, "tests/test_far.py", "from chrys.far import far\n")
    graph = chrys_test.build_import_graph(tmp_path)

    selection = chrys_test.select_smart_tests((_changed("src/chrys/feature.py"),), graph, root=tmp_path)

    assert set(selection.regular) == {"tests/test_near.py"}


@pytest.mark.parametrize("provider", ["tests/test_provider.py", "tests/a/conftest.py"])
def test_fixture_reexports_through_test_modules_keep_their_consumers(tmp_path: Path, provider: str) -> None:
    _write(tmp_path, "src/chrys/feature.py", "def make():\n    return 1\n")
    _write(
        tmp_path,
        provider,
        "import pytest\nfrom chrys.feature import make\n@pytest.fixture\ndef fx():\n    return make()\n",
    )
    module = provider.removesuffix(".py").replace("/", ".")
    _write(tmp_path, "tests/test_bridge1.py", f"from {module} import fx\n")
    _write(tmp_path, "tests/test_bridge2.py", "from tests.test_bridge1 import fx\n")
    _write(tmp_path, "tests/test_consumer.py", "from tests.test_bridge2 import fx\ndef test_uses(fx): pass\n")
    graph = chrys_test.build_import_graph(tmp_path)

    selection = chrys_test.select_smart_tests((_changed("src/chrys/feature.py"),), graph, root=tmp_path)

    assert "tests/test_consumer.py" in selection.regular


def test_collapsing_complete_files_keeps_the_scope_and_partial_files(tmp_path: Path) -> None:
    selection = chrys_test.Selection()
    selection.add("tests/test_a.py::test_one", "fixture dependency a")
    selection.add("tests/test_a.py::test_two", "fixture dependency b")
    selection.add("tests/test_b.py::test_one", "fixture dependency c")
    collected = (
        "tests/test_a.py::test_one",
        "tests/test_a.py::test_two[x]",
        "tests/test_b.py::test_one",
        "tests/test_b.py::test_two",
    )

    chrys_test._collapse_complete_files(selection, collected)

    assert dict(selection.regular) == {
        "tests/test_a.py": {"fixture dependency a", "fixture dependency b"},
        "tests/test_b.py::test_one": {"fixture dependency c"},
    }


@pytest.mark.parametrize(
    ("target", "nodeid", "covered"),
    [
        ("tests/app", "tests/app/test_x.py::test_y", True),
        ("tests/app", "tests/apple/test_x.py::test_y", False),
        ("tests/test_x.py", "tests/test_x.py::Test::test_y[1]", True),
        ("tests/test_x.py::test_y", "tests/test_x.py::test_y[a/b]", True),
        ("tests/test_x.py::test_y", "tests/test_x.py::test_yz", False),
        # Parameter ids are opaque: separators inside them are not structure.
        ("tests/test_x.py::test_y[a]", "tests/test_x.py::test_y[a]::extra", False),
        ("tests/test_x.py::test_y[a]", "tests/test_x.py::test_y[a]/extra", False),
        ("tests/test_x.py::test_y[a]", "tests/test_x.py::test_y[a][extra]", False),
        ("tests/test_x.py::test_y[a]", "tests/test_x.py::test_y[a]", True),
    ],
)
def test_indexed_coverage_matches_target_covers(target: str, nodeid: str, covered: bool) -> None:
    assert chrys_test._target_covers(target, nodeid) is covered
    assert chrys_test._covered_by(nodeid, {target}) is covered


def test_hop_budgets_keep_incomparable_routes_to_the_same_test(tmp_path: Path) -> None:
    # test_shared is reached both by a test hop (0 source, 1 test) and by a
    # source hop (1 source, 0 test); only the second leaves a test hop for test_far.
    _write(tmp_path, "src/chrys/feature.py", "VALUE = 1\n")
    _write(tmp_path, "src/chrys/middle.py", "from chrys.feature import VALUE\n")
    _write(tmp_path, "tests/test_a.py", "from chrys.feature import VALUE\n")
    _write(tmp_path, "tests/test_shared.py", "from tests.test_a import VALUE\nfrom chrys.middle import VALUE as V\n")
    _write(tmp_path, "tests/test_far.py", "from tests.test_shared import VALUE\n")
    graph = chrys_test.build_import_graph(tmp_path)

    chains = chrys_test._dependency_chains(graph, "chrys.feature")

    assert "tests.test_far" in chains


def test_named_helper_imports_depend_only_on_the_imported_helper(tmp_path: Path) -> None:
    _write(tmp_path, "src/chrys/feature.py", "VALUE = 1\n")
    _write(
        tmp_path,
        "tests/support/helpers.py",
        "def uses():\n    from chrys.feature import VALUE\n    return VALUE\ndef other():\n    return 2\n",
    )
    _write(tmp_path, "tests/test_uses.py", "from tests.support.helpers import uses\n")
    _write(tmp_path, "tests/test_other.py", "from tests.support.helpers import other\n")
    _write(tmp_path, "tests/test_namespace.py", "from tests.support import helpers\n")
    graph = chrys_test.build_import_graph(tmp_path)

    selection = chrys_test.select_smart_tests((_changed("src/chrys/feature.py"),), graph, root=tmp_path)

    assert {"tests/test_uses.py", "tests/test_namespace.py"} <= set(selection.regular)
    assert "tests/test_other.py" not in selection.regular


_FIXTURE = "import pytest\nfrom chrys.dep import make\n@pytest.fixture\ndef fx():\n    return make()\n"


@pytest.mark.parametrize(
    ("files", "roots"),
    [
        ({"tests/a/conftest.py": _FIXTURE}, ("tests/a",)),
        ({"tests/conftest.py": _FIXTURE}, ("tests",)),
        ({"tests/support/fixtures.py": _FIXTURE}, ("tests",)),
        # Another conftest re-exports the fixture to its own directory.
        (
            {"tests/a/conftest.py": _FIXTURE, "tests/b/conftest.py": "from tests.a.conftest import fx\n"},
            ("tests/a", "tests/b"),
        ),
        # A plain test module's fixture becomes visible below a conftest that imports it.
        (
            {"tests/test_provider.py": _FIXTURE, "tests/sub/conftest.py": "from tests.test_provider import fx\n"},
            ("tests/sub",),
        ),
        # A test module passes a conftest fixture on to another conftest.
        (
            {
                "tests/a/conftest.py": _FIXTURE,
                "tests/test_bridge.py": "from tests.a.conftest import fx\n",
                "tests/b/conftest.py": "from tests.test_bridge import fx\n",
            },
            ("tests/a", "tests/b"),
        ),
        # A plugin outside tests/ may expose a test module's fixture everywhere.
        (
            {"tests/test_provider.py": _FIXTURE, "scripts/fixture_plugin.py": "from tests.test_provider import fx\n"},
            ("tests",),
        ),
        # A plain test module's fixture used only by itself needs no collection.
        ({"tests/test_provider.py": _FIXTURE}, ()),
        (
            {
                "tests/a/conftest.py": "import pytest\n@pytest.fixture\ndef fx():\n    return 1\n",
                "tests/test_dep.py": "from chrys.dep import make\n",
            },
            (),
        ),
    ],
    ids=[
        "nested-conftest",
        "root-conftest",
        "support",
        "conftest-reexport",
        "test-module-reexport",
        "test-module-bridge",
        "plugin-outside-tests",
        "test-module",
        "no-fixture",
    ],
)
def test_fixture_collection_covers_every_directory_that_can_see_a_reached_fixture(
    tmp_path: Path, files: dict[str, str], roots: tuple[str, ...]
) -> None:
    _write(tmp_path, "src/chrys/dep.py", "def make():\n    return 1\n")
    _write(tmp_path, "tests/sub/test_user.py", "def test_user(fx): pass\n")
    for path, source in files.items():
        _write(tmp_path, path, source)
    graph = chrys_test.build_import_graph(tmp_path)
    selection = chrys_test.select_smart_tests(
        (_changed("src/chrys/dep.py"),), graph, root=tmp_path, defer_fixture_fallbacks=True
    )

    assert chrys_test.fixture_collection_roots(graph, selection) == roots


def test_smart_architecture_targets_run_in_parallel() -> None:
    targets = [f"tests/architecture/test_{name}.py" for name in ("layering", "tui_structure", "hygiene_lazy_imports")]

    command = chrys_test._pytest_command(targets)

    assert command[command.index("-n") + 1] == "3"
    assert command[command.index("--dist") + 1] == "worksteal"
    assert chrys_test._pytest_command(targets[:1])[-3:-1] == ["-n", "0"]


def test_target_sources_count_each_target_once_under_its_most_specific_reason(
    capsys: pytest.CaptureFixture[str],
) -> None:
    selection = chrys_test.Selection()
    selection.add("tests/test_a.py", "test file changed directly")
    selection.add("tests/test_a.py", "reverse import dependency of changed path src/a.py; chain: a -> tests.test_a")
    selection.add("tests/test_b.py", "reverse import dependency of changed path src/a.py; chain: a -> tests.test_b")
    selection.add("tests/test_c.py::test_c", "fixture dependency of src/a.py; chain: a -> tests/test_c.py::test_c")
    selection.add("tests/architecture/test_d.py", "architecture filesystem watch matched src/a.py")
    selection.add("tests/test_e.py", "Textual stylesheet changed: src/a.tcss")

    chrys_test._print_targets(selection)

    assert (
        capsys.readouterr().out.splitlines()[-1] == "Targets by source: direct 1, watch 1, import 1, fixture 1, rule 1."
    )


_CLASSES = """\
class Engine:
    def run(self):
        return 1

    @property
    def size(self):
        return 2


class Other:
    def run(self):
        return 3
"""


def _narrowed_selection(root: Path, before: str, after: str) -> chrys_test.Selection:
    path = "src/chrys/pkg/impl.py"
    _write(root, path, after)
    _write(root, "src/chrys/pkg/__init__.py", "from .impl import Engine, Other\n")
    _write(root, "tests/test_engine_direct.py", "from chrys.pkg.impl import Engine\n")
    _write(root, "tests/test_engine_facade.py", "from chrys.pkg import Engine\n")
    _write(root, "tests/test_other.py", "from chrys.pkg import Other\n")
    _write(root, "tests/test_patched_elsewhere.py", "from chrys.pkg import Other\npatch('chrys.unrelated.thing')\n")
    _write(root, "tests/test_patched_here.py", "from chrys.pkg import Other\npatch('chrys.pkg.Engine.run')\n")
    graph = chrys_test.build_import_graph(root)
    chrys_test._narrow_source_imports(graph, path, before, root=root)
    return chrys_test.select_smart_tests((_changed(path),), graph, root=root)


@pytest.mark.parametrize(
    "after",
    [
        _CLASSES.replace("return 1", "return 10"),
        _CLASSES.replace("return 2", "return 20"),  # a descriptor-decorated method
    ],
    ids=["method", "property"],
)
def test_method_body_edits_select_only_importers_of_that_class(tmp_path: Path, after: str) -> None:
    selection = _narrowed_selection(tmp_path, _CLASSES, after)

    # Direct and re-exported named imports narrow alike; a patch string naming
    # the changed module keeps its owner, one naming another module does not.
    assert set(selection.regular) == {
        "tests/test_engine_direct.py",
        "tests/test_engine_facade.py",
        "tests/test_patched_here.py",
    }


_SUBCLASS_HOOK = "class Base:\n    def __init_subclass__(cls): REGISTRY.append(cls().run())\n"


@pytest.mark.parametrize(
    ("before", "after"),
    [
        # A signature change is structural.
        ("class A:\n    def run(self): return 1\n", "class A:\n    def run(self, x): return 1\n"),
        # An unknown class or method decorator may expose methods at import time,
        # even when the decorator itself is unchanged.
        ("@register\nclass A:\n    def run(self): return 1\n", "@register\nclass A:\n    def run(self): return 2\n"),
        (
            "class A:\n    @register\n    def run(self): return 1\n",
            "class A:\n    @register\n    def run(self): return 2\n",
        ),
        # A class attribute or a nested class is not a method body.
        ("class A:\n    X = 1\n", "class A:\n    X = 2\n"),
        # Initialization that uses the class (a subclass, an instance) keeps module scope.
        (
            "class A:\n    def run(self): return 1\nclass B(A): pass\n",
            "class A:\n    def run(self): return 2\nclass B(A): pass\n",
        ),
        (
            "class A:\n    def run(self): return 1\nINSTANCE = A()\n",
            "class A:\n    def run(self): return 2\nINSTANCE = A()\n",
        ),
        # A decorator that only shares a descriptor's suffix may register the method.
        (
            "class A:\n    @registry.getter\n    def run(self): return 1\n",
            "class A:\n    @registry.getter\n    def run(self): return 2\n",
        ),
        # An unchanged registered method can call the edited one later.
        (
            "class A:\n    def run(self): return 1\n    @register\n    def hook(self): return self.run()\n",
            "class A:\n    def run(self): return 2\n    @register\n    def hook(self): return self.run()\n",
        ),
        # Creating the class runs a base's subclass hook or a metaclass.
        (
            _SUBCLASS_HOOK + "class A(Base):\n    def run(self): return 1\n",
            _SUBCLASS_HOOK + "class A(Base):\n    def run(self): return 2\n",
        ),
        (
            "class A(metaclass=Meta):\n    def run(self): return 1\n",
            "class A(metaclass=Meta):\n    def run(self): return 2\n",
        ),
        # Unpacked keywords may carry a metaclass.
        ("class A(**OPTIONS):\n    def run(self): return 1\n", "class A(**OPTIONS):\n    def run(self): return 2\n"),
        # A local metaclass is inherited through an unchanged base.
        (
            "class Base(metaclass=Meta): pass\nclass A(Base):\n    def run(self): return 1\n",
            "class Base(metaclass=Meta): pass\nclass A(Base):\n    def run(self): return 2\n",
        ),
        # Only pytest's own fixture decorator is inert, not any ``.fixture``.
        (
            "class A:\n    @registry.fixture\n    def run(self): return 1\n",
            "class A:\n    @registry.fixture\n    def run(self): return 2\n",
        ),
        # A registered factory returns the class without calling it at import.
        (
            "class A:\n    def run(self): return 1\n@register\ndef make(): return A\n",
            "class A:\n    def run(self): return 2\n@register\ndef make(): return A\n",
        ),
    ],
    ids=[
        "signature",
        "class-decorator",
        "method-registry",
        "class-attribute",
        "subclass",
        "instance",
        "registry-accessor",
        "registered-sibling",
        "subclass-hook",
        "metaclass",
        "unpacked-keywords",
        "inherited-metaclass",
        "registry-fixture",
        "registered-factory",
    ],
)
def test_structural_class_edits_keep_module_scope(before: str, after: str) -> None:
    assert chrys_test._changed_function_names(before, after) is None


def test_method_edit_includes_local_callers_of_the_class() -> None:
    before = "class A:\n    def run(self): return 1\ndef make(): return A()\ndef unrelated(): return 0\n"
    after = before.replace("return 1", "return 2")

    assert chrys_test._changed_function_names(before, after) == {"A", "make"}


@pytest.mark.parametrize(
    ("extra", "narrowed"),
    [
        ("import logging\nLOG = logging.getLogger(__name__)\n", True),
        ("def read(obj): return getattr(obj, 'name')\n", True),
        ("class Record:\n    def dump(self): return dict(self.__dict__)\n", True),
        ("import sys\ndef read(name): return getattr(sys.modules[__name__], name)\n", False),
        ("from sys import modules\ndef read(name): return getattr(modules[__name__], name)\n", False),
        ("import sys as system\ndef read(name): return getattr(system.modules[__name__], name)\n", False),
        ("import importlib\ndef read(name): return getattr(importlib.import_module(__name__), name)\n", False),
        ("from importlib import import_module as load\ndef read(name): return getattr(load(__name__), name)\n", False),
        ("def read(name): return globals()[name]\n", False),
        ("def __getattr__(name): return clean\n", False),
    ],
    ids=[
        "logger-name",
        "getattr-other-object",
        "instance-dict",
        "sys-modules",
        "aliased-modules",
        "aliased-sys",
        "import-self",
        "aliased-import-self",
        "globals",
        "module-getattr",
    ],
)
def test_only_reflection_on_the_module_itself_blocks_narrowing(extra: str, narrowed: bool) -> None:
    before = extra + "def clean(): return 1\n"
    after = before.replace("return 1", "return 2")

    assert (chrys_test._changed_function_names(before, after) is not None) is narrowed


@pytest.mark.parametrize(
    ("decorator", "narrowed"),
    [("property", True), ("classmethod", True), ("functools.cache", True), ("register", False)],
)
def test_a_caller_method_runs_at_import_only_under_an_unknown_decorator(decorator: str, narrowed: bool) -> None:
    before = f"class A:\n    def run(self): return 1\nclass B:\n    @{decorator}\n    def a(self): return A()\n"
    after = before.replace("return 1", "return 2")

    assert chrys_test._changed_function_names(before, after) == ({"A", "B"} if narrowed else None)


def test_a_class_keyword_without_a_local_hook_keeps_narrowing() -> None:
    before = "class A(Widget, can_focus=False):\n    def run(self): return 1\n"
    after = before.replace("return 1", "return 2")

    assert chrys_test._changed_function_names(before, after) == {"A"}


def test_narrowing_reads_an_encoding_cookie_change_as_a_module_change() -> None:
    before = "# coding: utf-8\nTEXT = 'é'\nclass A:\n    def run(self): return 1\n"
    after = before.replace("utf-8", "latin-1").replace("return 1", "return 2")

    assert chrys_test._changed_function_names(before.encode(), after.encode()) is None


def test_a_type_alias_of_a_changed_class_joins_its_scope_without_blocking_narrowing() -> None:
    before = "class A:\n    def run(self): return 1\nInputs = str | A\nCALLS = [A]\n"
    after = before.replace("return 1", "return 2")

    assert chrys_test._changed_function_names(before, after) == {"A", "Inputs", "CALLS"}


@pytest.mark.parametrize(
    "use",
    ["register(A)\n", "@decorate(A)\ndef other(): pass\n", "class B(Base, meta=A): pass\n"],
    ids=["call-argument", "decorator", "class-keyword"],
)
def test_import_time_execution_of_a_changed_class_keeps_module_scope(use: str) -> None:
    before = "class A:\n    def run(self): return 1\n" + use
    after = before.replace("return 1", "return 2")

    assert chrys_test._changed_function_names(before, after) is None
