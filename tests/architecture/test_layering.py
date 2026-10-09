# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Architecture layering checks for first-party imports."""

from __future__ import annotations

import ast
import inspect
from dataclasses import dataclass
from functools import cache
from importlib.util import resolve_name
from pathlib import Path
from typing import Any, get_type_hints

import pytest

from chrys.service.agent_middleware.system_reminder import (
    CurrentRunReminderScope,
    CurrentRunReminderTarget,
    SystemReminderMiddleware,
)
from tests.support.ci import CI_LINUX_ONLY
from tests.support.paths import REPO_ROOT, SRC_ROOT

# Platform-independent source analysis: the Linux CI job covers it.
pytestmark = CI_LINUX_ONLY

ROOT = REPO_ROOT
SRC = SRC_ROOT / "chrys"

FOUNDATION = "foundation"
KERNEL = "kernel"
SERVICE = "service"
ORCHESTRATION = "orchestration"
APP = "app"
ROOT_INIT = "__root__"
_AGENTS_SOURCE_MAP_SECTION = 'AGENTS.md "Source map (src/chrys/)" section'

WORKFLOWS_FACADE = "workflows"

# Fork-customization package (aixcoding/docs/): tier 1 pins it to kernel/foundation
# imports only; service (LLM stack), orchestration (assembly) and app (ACP) may
# reference it downwards.
AIXCODING = "aixcoding"

TIER_ORDER = {
    FOUNDATION: 0,
    KERNEL: 1,
    AIXCODING: 1,
    SERVICE: 2,
    # src/chrys/workflows.py re-exports the service-tier SDK for workflow files.
    WORKFLOWS_FACADE: 2,
    ORCHESTRATION: 3,
    APP: 4,
}

ROOT_METADATA_EXPORTS = {"__version__"}

_KERNEL_PRIVATE_PROMOTION_REASON = (
    "preview shaping / provider-specific key shared with service tier; promote to a public kernel module"
)
_KERNEL_PRIVATE_IMPORT_ALLOWLIST = {
    (
        Path("src/chrys/service/agent_middleware/events/tool_events.py"),
        "chrys.kernel._result_ceiling",
    ): _KERNEL_PRIVATE_PROMOTION_REASON,
    (
        Path("src/chrys/service/agent_middleware/events/sub_agent_events.py"),
        "chrys.kernel._result_ceiling",
    ): _KERNEL_PRIVATE_PROMOTION_REASON,
    (
        Path("src/chrys/service/llm/openai_chat_completion.py"),
        "chrys.kernel._content",
    ): _KERNEL_PRIVATE_PROMOTION_REASON,
    (
        Path("src/chrys/service/llm/anthropic_chat.py"),
        "chrys.kernel._content",
    ): _KERNEL_PRIVATE_PROMOTION_REASON,
    (
        Path("src/chrys/service/tools/result_metadata.py"),
        "chrys.kernel._serialization",
    ): _KERNEL_PRIVATE_PROMOTION_REASON,
    (
        Path("src/chrys/service/mcp/owned.py"),
        "chrys.kernel._tool_expansion",
    ): _KERNEL_PRIVATE_PROMOTION_REASON,
}


@dataclass(frozen=True)
class ImportEdge:
    source_path: Path
    source_top: str
    target: str
    target_top: str
    line: int
    is_relative: bool = False
    is_root_alias: bool = False


@cache
def _is_submodule(module: str, name: str) -> bool:
    """Report whether ``name`` names a real submodule file of ``module``."""
    if not module.startswith("chrys"):
        return False
    package = SRC.parent / Path(module.replace(".", "/"))
    return (package / f"{name}.py").is_file() or (package / name / "__init__.py").is_file()


class FirstPartyImportCollector(ast.NodeVisitor):
    """Collect first-party static and dynamic imports, excluding TYPE_CHECKING blocks."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.source_top = _source_top(path)
        self.edges: list[ImportEdge] = []

    def visit_If(self, node: ast.If) -> None:
        if _is_type_checking_guard(node.test):
            for stmt in node.orelse:
                self.visit(stmt)
            return
        self.generic_visit(node)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self._add_edge(alias.name, node.lineno)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.level != 0:
            for target in _resolve_relative_import(self.path, node):
                self._add_edge(target, node.lineno, is_relative=True)
                # ``from ...kernel import _types`` resolves to the package
                # alone, exactly as the absolute form does. The bare ``from
                # ... import x`` spelling is already per-alias above, so only
                # the named-module spelling needs expanding here.
                if node.module is not None:
                    self._add_submodule_edges(target, node, is_relative=True)
            return
        if node.module is None:
            return
        if node.module == "chrys":
            for alias in node.names:
                if alias.name in ROOT_METADATA_EXPORTS:
                    continue
                self._add_edge(f"chrys.{alias.name}", node.lineno, is_root_alias=True)
            return
        self._add_edge(node.module, node.lineno)
        self._add_submodule_edges(node.module, node)

    def _add_submodule_edges(self, module: str, node: ast.ImportFrom, *, is_relative: bool = False) -> None:
        """Add an edge per alias that names a real submodule of *module*.

        ``from chrys.kernel import _types`` imports a module, not a member, yet
        the statement alone only names the package. Without resolving the alias
        against the tree, every module-level rule below sees the public package
        and the private submodule slips through.
        """
        for alias in node.names:
            if _is_submodule(module, alias.name):
                self._add_edge(f"{module}.{alias.name}", node.lineno, is_relative=is_relative)

    def visit_Call(self, node: ast.Call) -> None:
        if _call_name(node.func) in {"__import__", "import_module"} and node.args:
            first_arg = node.args[0]
            if isinstance(first_arg, ast.Constant) and isinstance(first_arg.value, str):
                self._add_edge(first_arg.value, node.lineno)
        self.generic_visit(node)

    def _add_edge(
        self,
        target: str,
        line: int,
        *,
        is_relative: bool = False,
        is_root_alias: bool = False,
    ) -> None:
        target_top = _target_top(target)
        if target_top is None:
            return
        self.edges.append(
            ImportEdge(
                source_path=self.path,
                source_top=self.source_top,
                target=target,
                target_top=target_top,
                line=line,
                is_relative=is_relative,
                is_root_alias=is_root_alias,
            )
        )


def _is_kernel_private_edge(path: Path, edge: ImportEdge) -> bool:
    return not path.is_relative_to(SRC / KERNEL) and edge.target.startswith("chrys.kernel._")


def _kernel_private_import_problem(path: Path, source: Path, edge: ImportEdge) -> str | None:
    """Return the violation text for an unexempted kernel-private import."""
    if not _is_kernel_private_edge(path, edge):
        return None
    if (source, edge.target) in _KERNEL_PRIVATE_IMPORT_ALLOWLIST:
        return None
    return (
        f"{source}:{edge.line}: kernel-private-modules-are-kernel-only forbids import of "
        f"{edge.target!r} outside src/chrys/kernel; violates {_AGENTS_SOURCE_MAP_SECTION} (underscore-prefixed "
        "kernel modules are private helpers). Fix: import the public chrys.kernel facade, or promote the shared "
        "API to a public kernel module"
    )


def test_first_party_imports_follow_layer_dag() -> None:
    violations: list[str] = []
    observed_private_import_exemptions: set[tuple[Path, str]] = set()
    for path in sorted(SRC.rglob("*.py")):
        if _is_root_init(path):
            violations.extend(_root_init_violations(path))
            continue

        source_top = _source_top(path)
        if _tier_for_top(source_top) is None:
            violations.append(f"{path.relative_to(ROOT)}:1: unregistered first-party top-level package {source_top!r}")
            continue

        tree = _parse(path)
        collector = FirstPartyImportCollector(path)
        collector.visit(tree)
        for edge in collector.edges:
            source = edge.source_path.relative_to(ROOT)
            problem = _kernel_private_import_problem(path, source, edge)
            if problem is not None:
                violations.append(problem)
            elif _is_kernel_private_edge(path, edge):
                observed_private_import_exemptions.add((source, edge.target))
            if _is_violation(edge):
                violations.append(_format_violation(edge))

    for source, target in sorted(_KERNEL_PRIVATE_IMPORT_ALLOWLIST.keys() - observed_private_import_exemptions):
        reason = _KERNEL_PRIVATE_IMPORT_ALLOWLIST[(source, target)]
        violations.append(
            f"{source}:1: kernel private-import allowlist entry for {target!r} has no real import edge "
            f"(reason: {reason}); violates the AGENTS.md testing-rules section (\"A guard that can't go red is "
            'worse than none — it is believed."). Fix: remove the stale allowlist entry or update it to the '
            "exact live source/target pair"
        )

    assert violations == []


def test_relative_cross_tier_imports_are_checked() -> None:
    path = SRC / "service" / "llm" / "foo.py"
    tree = ast.parse("from ...orchestration.engine import engine\n", filename=str(path))
    collector = FirstPartyImportCollector(path)

    collector.visit(tree)

    # ``engine`` is itself a module, so the alias expands to a second edge.
    assert collector.edges == [
        ImportEdge(
            source_path=path,
            source_top=SERVICE,
            target="chrys.orchestration.engine",
            target_top=ORCHESTRATION,
            line=1,
            is_relative=True,
        ),
        ImportEdge(
            source_path=path,
            source_top=SERVICE,
            target="chrys.orchestration.engine.engine",
            target_top=ORCHESTRATION,
            line=1,
            is_relative=True,
        ),
    ]
    assert all(_is_violation(edge) for edge in collector.edges)


@pytest.mark.parametrize(
    "statement",
    [
        "from chrys.kernel import _types, Message",
        "from ...kernel import _types, Message",
    ],
)
def test_package_form_private_kernel_import_is_seen(statement: str) -> None:
    """A private submodule must not hide behind the package name, either spelling."""
    path = SRC / "service" / "llm" / "foo.py"
    tree = ast.parse(f"{statement}\n", filename=str(path))
    collector = FirstPartyImportCollector(path)

    collector.visit(tree)

    targets = [edge.target for edge in collector.edges]
    assert "chrys.kernel._types" in targets
    # ``Message`` is a re-exported member, not a module, so it stays unresolved.
    assert "chrys.kernel.Message" not in targets

    private_edge = next(edge for edge in collector.edges if edge.target == "chrys.kernel._types")
    problem = _kernel_private_import_problem(path, Path("src/chrys/service/llm/foo.py"), private_edge)
    assert problem is not None
    assert "chrys.kernel._types" in problem
    assert _AGENTS_SOURCE_MAP_SECTION in problem


def test_relative_same_tier_imports_are_allowed() -> None:
    path = SRC / "service" / "llm" / "foo.py"
    tree = ast.parse("from .clients import create_client\n", filename=str(path))
    collector = FirstPartyImportCollector(path)

    collector.visit(tree)

    assert collector.edges == [
        ImportEdge(
            source_path=path,
            source_top=SERVICE,
            target="chrys.service.llm.clients",
            target_top=SERVICE,
            line=1,
            is_relative=True,
        )
    ]
    assert not _is_violation(collector.edges[0])


def test_root_version_import_is_allowed() -> None:
    path = SRC / "app" / "cli" / "foo.py"
    tree = ast.parse("from chrys import __version__\n", filename=str(path))
    collector = FirstPartyImportCollector(path)

    collector.visit(tree)

    assert collector.edges == []


def test_root_non_tier_alias_import_is_rejected() -> None:
    path = SRC / "app" / "cli" / "foo.py"
    tree = ast.parse("from chrys import not_a_tier\n", filename=str(path))
    collector = FirstPartyImportCollector(path)

    collector.visit(tree)

    assert collector.edges == [
        ImportEdge(
            source_path=path,
            source_top=APP,
            target="chrys.not_a_tier",
            target_top="not_a_tier",
            line=1,
            is_root_alias=True,
        )
    ]
    assert _is_violation(collector.edges[0])


def test_root_tier_alias_import_is_classified() -> None:
    path = SRC / "app" / "cli" / "foo.py"
    tree = ast.parse("from chrys import kernel\n", filename=str(path))
    collector = FirstPartyImportCollector(path)

    collector.visit(tree)

    assert collector.edges == [
        ImportEdge(
            source_path=path,
            source_top=APP,
            target="chrys.kernel",
            target_top=KERNEL,
            line=1,
            is_root_alias=True,
        )
    ]
    assert not _is_violation(collector.edges[0])


def test_bare_root_imports_are_rejected_by_default() -> None:
    path = SRC / "app" / "cli" / "foo.py"
    sources = [
        "import chrys\n",
        "__import__('chrys')\n",
        "from importlib import import_module\nimport_module('chrys')\n",
    ]

    for source in sources:
        tree = ast.parse(source, filename=str(path))
        collector = FirstPartyImportCollector(path)
        collector.visit(tree)

        assert len(collector.edges) == 1
        assert collector.edges[0].target == "chrys"
        assert _is_violation(collector.edges[0])


def test_dynamic_subpackage_import_is_classified() -> None:
    path = SRC / "app" / "cli" / "foo.py"
    tree = ast.parse("from importlib import import_module\nimport_module('chrys.app.tui')\n", filename=str(path))
    collector = FirstPartyImportCollector(path)

    collector.visit(tree)

    assert collector.edges == [
        ImportEdge(
            source_path=path,
            source_top=APP,
            target="chrys.app.tui",
            target_top=APP,
            line=2,
        )
    ]
    assert not _is_violation(collector.edges[0])


def test_system_reminder_scoped_current_run_api_stays_service_owned_and_typed() -> None:
    """Scoped reminder tokens must stay service-owned and explicitly typed."""
    path = SRC / "service" / "agent_middleware" / "system_reminder.py"
    tree = _parse(path)
    orchestration_imports = [
        target
        for target in _import_targets_including_type_checking(path, tree)
        if target == "chrys.orchestration" or target.startswith("chrys.orchestration.")
    ]
    assert orchestration_imports == []

    assert get_type_hints(SystemReminderMiddleware.create_current_run_scope)["return"] is CurrentRunReminderScope

    capture_hints = get_type_hints(SystemReminderMiddleware.capture_current_run_target)
    assert capture_hints["reminder_scope"] is CurrentRunReminderScope
    assert capture_hints["return"] == CurrentRunReminderTarget | None

    queue_hints = get_type_hints(SystemReminderMiddleware.queue_hook_reminders_for_current_run)
    assert queue_hints["target"] is CurrentRunReminderTarget
    assert queue_hints["return"] is bool

    catalog_hints = get_type_hints(SystemReminderMiddleware.update_skill_catalog_for_current_run)
    assert catalog_hints["target"] is CurrentRunReminderTarget
    assert catalog_hints["return"] is bool

    set_catalog_hints = get_type_hints(SystemReminderMiddleware.set_skill_catalog_for_current_run)
    assert set_catalog_hints["target"] is CurrentRunReminderTarget
    assert set_catalog_hints["skill_catalog"] == str | None
    assert set_catalog_hints["return"] is bool

    valid_hints = get_type_hints(SystemReminderMiddleware.is_current_run_target_valid)
    assert valid_hints["target"] is CurrentRunReminderTarget
    assert valid_hints["return"] is bool

    expire_hints = get_type_hints(SystemReminderMiddleware.expire_current_run_scope)
    assert expire_hints["reminder_scope"] is CurrentRunReminderScope
    assert expire_hints["return"] is type(None)

    prepare_hints = get_type_hints(SystemReminderMiddleware.prepare_turn)
    assert prepare_hints["reminder_scope"] == CurrentRunReminderScope | None
    assert get_type_hints(CurrentRunReminderTarget)["scope"] is CurrentRunReminderScope

    scoped_api_names = {
        "create_current_run_scope",
        "capture_current_run_target",
        "queue_hook_reminders_for_current_run",
        "update_skill_catalog_for_current_run",
        "set_skill_catalog_for_current_run",
        "is_current_run_target_valid",
        "expire_current_run_scope",
        "prepare_turn",
    }
    for name in scoped_api_names:
        signature = inspect.signature(getattr(SystemReminderMiddleware, name))
        hints = get_type_hints(getattr(SystemReminderMiddleware, name))
        for parameter in signature.parameters.values():
            if parameter.name == "self":
                continue
            annotation = hints.get(parameter.name, parameter.annotation)
            assert annotation is not Any, f"{name}.{parameter.name} degraded to Any"
            assert annotation is not object, f"{name}.{parameter.name} degraded to object"
        return_annotation = hints.get("return", signature.return_annotation)
        assert return_annotation is not Any, f"{name} return degraded to Any"
        assert return_annotation is not object, f"{name} return degraded to object"


def _import_targets_including_type_checking(path: Path, tree: ast.AST) -> list[str]:
    """Collect static/dynamic first-party import targets without skipping TYPE_CHECKING."""
    targets: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            targets.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                targets.extend(_resolve_relative_import(path, node))
            elif node.module == "chrys":
                targets.extend(f"chrys.{alias.name}" for alias in node.names)
            elif node.module is not None:
                targets.append(node.module)
        elif isinstance(node, ast.Call) and _call_name(node.func) in {"__import__", "import_module"} and node.args:
            first_arg = node.args[0]
            if isinstance(first_arg, ast.Constant) and isinstance(first_arg.value, str):
                targets.append(first_arg.value)
    return [target for target in targets if target == "chrys" or target.startswith("chrys.")]


def _root_init_violations(path: Path) -> list[str]:
    tree = _parse(path)
    collector = FirstPartyImportCollector(path)
    collector.visit(tree)
    return [_format_violation(edge) for edge in collector.edges]


@cache
def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def teardown_module() -> None:
    """Release cached source trees after this worker finishes the module."""
    _parse.cache_clear()


def _is_violation(edge: ImportEdge) -> bool:
    source_tier = _tier_for_top(edge.source_top)
    target_tier = _tier_for_top(edge.target_top)
    if edge.is_root_alias and edge.target_top not in TIER_ORDER:
        return True
    if source_tier is None or target_tier is None:
        return True
    if source_tier == KERNEL and target_tier == KERNEL and not edge.is_relative:
        return True
    return TIER_ORDER[target_tier] > TIER_ORDER[source_tier]


def _format_violation(edge: ImportEdge) -> str:
    source = edge.source_path.relative_to(ROOT)
    source_tier = _tier_for_top(edge.source_top) or "unknown"
    target_tier = _tier_for_top(edge.target_top) or "unknown"
    root_alias = " root alias" if edge.is_root_alias else ""
    return (
        f"{source}:{edge.line}: {edge.source_top} ({source_tier}) "
        f"must not import{root_alias} {edge.target!r} ({edge.target_top}, {target_tier})"
    )


def _source_top(path: Path) -> str:
    rel = path.relative_to(SRC)
    first = rel.parts[0]
    if first == "__init__.py":
        return ROOT_INIT
    if first.endswith(".py"):
        return first.removesuffix(".py")
    return first


def _source_package(path: Path) -> str:
    rel = path.relative_to(SRC)
    package_parts = rel.parts[:-1]
    return ".".join(("chrys", *package_parts))


def _resolve_relative_import(path: Path, node: ast.ImportFrom) -> list[str]:
    package_parts = _source_package(path).split(".")
    if node.level > len(package_parts):
        return []
    base_parts = package_parts[: len(package_parts) - node.level + 1]
    base = ".".join(base_parts)
    if node.module is not None:
        return [f"{base}.{node.module}" if base else node.module]
    return [base if alias.name == "*" else f"{base}.{alias.name}" for alias in node.names]


def _target_top(name: str) -> str | None:
    if name == "chrys":
        return ROOT_INIT
    if not name.startswith("chrys."):
        return None
    parts = name.split(".")
    return parts[1] if len(parts) > 1 else ROOT_INIT


def _tier_for_top(top: str) -> str | None:
    return top if top in TIER_ORDER else None


def _is_root_init(path: Path) -> bool:
    return path == SRC / "__init__.py"


def _is_type_checking_guard(node: ast.expr) -> bool:
    return (isinstance(node, ast.Name) and node.id == "TYPE_CHECKING") or (
        isinstance(node, ast.Attribute)
        and node.attr == "TYPE_CHECKING"
        and isinstance(node.value, ast.Name)
        and node.value.id == "typing"
    )


def _call_name(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


# Invoker layering: caller-package back edges, including lazy imports.
_INVOKER_ROOT = SRC_ROOT / "chrys" / "orchestration"
_INVOKER_PREFIX = "chrys.orchestration."
_INVOKER_FORBIDDEN = {"invoker": ("engine", "sub_agents"), "sub_agents": ("engine",), "engine": ()}


def _import_call_argument(node: ast.Call, position: int, name: str) -> ast.expr | None:
    if len(node.args) > position:
        return node.args[position]
    return next((keyword.value for keyword in node.keywords if keyword.arg == name), None)


def _builtin_import_package(globals_arg: ast.expr | None, package: str) -> str | None:
    """Follow _calc___package__ for literal globals without interpreting specs."""
    if (
        isinstance(globals_arg, ast.Call)
        and isinstance(globals_arg.func, ast.Name)
        and globals_arg.func.id == "globals"
        and not globals_arg.args
        and not globals_arg.keywords
    ):
        return package
    # Missing/None globals do not implicitly receive the caller's globals.
    if not isinstance(globals_arg, ast.Dict):
        return None
    try:
        literal_globals = ast.literal_eval(globals_arg)
    except ValueError, TypeError:
        # Unpacking or computed keys/values can change package resolution.
        return None
    if "__spec__" in literal_globals:
        return None
    declared_package = literal_globals.get("__package__")
    if isinstance(declared_package, str):
        return declared_package
    if declared_package is not None:
        return None
    name = literal_globals.get("__name__")
    if not isinstance(name, str):
        return None
    return name if "__path__" in literal_globals else name.rpartition(".")[0]


def _builtin_import_targets(node: ast.Call, package: str) -> list[str]:
    """Resolve __import__(name, globals, locals, fromlist, level) literal edges."""
    name = _import_call_argument(node, 0, "name")
    if not isinstance(name, ast.Constant) or not isinstance(name.value, str):
        return []
    level = _import_call_argument(node, 4, "level")
    if level is None:
        relative_level = 0
    elif isinstance(level, ast.Constant) and isinstance(level.value, int) and level.value >= 0:
        relative_level = level.value
    else:
        return []
    target = name.value
    if relative_level:
        relative_package = _builtin_import_package(_import_call_argument(node, 1, "globals"), package)
        if relative_package is None:
            return []
        try:
            target = resolve_name("." * relative_level + target, relative_package)
        except ImportError, ValueError:
            return []
    targets = [target]
    fromlist = _import_call_argument(node, 3, "fromlist")
    if isinstance(fromlist, ast.List | ast.Tuple):
        # As with static ImportFrom, retain literal child edges without importing
        # the package; this also covers every invoker module.
        targets.extend(
            f"{target}.{item.value}"
            for item in fromlist.elts
            if isinstance(item, ast.Constant) and isinstance(item.value, str) and item.value != "*"
        )
    return targets


def _invoker_import_violations(source: str, module: str) -> list[str]:
    """Inspect static and literal dynamic imports, including nested functions.

    Type-only edges are included: these three packages must not acquire even a
    type-level dependency on their caller. Unknown computed import strings are
    outside this static guard, as in the existing layering guard.
    """
    tree = ast.parse(source)
    package = module.removesuffix(".__init__") if module.endswith(".__init__") else module.rpartition(".")[0]
    owner = module.removeprefix(_INVOKER_PREFIX).split(".")[0]
    import_modules = {"importlib"}
    import_functions: set[str] = set()
    builtin_import_modules: set[str] = set()
    builtin_import_functions = {"__import__"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            import_modules.update(alias.asname or alias.name for alias in node.names if alias.name == "importlib")
            builtin_import_modules.update(
                alias.asname or alias.name for alias in node.names if alias.name in {"builtins", "importlib"}
            )
        elif isinstance(node, ast.ImportFrom) and node.module == "importlib":
            import_functions.update(alias.asname or alias.name for alias in node.names if alias.name == "import_module")
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module in {"builtins", "importlib"}:
            builtin_import_functions.update(
                alias.asname or alias.name for alias in node.names if alias.name == "__import__"
            )
    edges: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            edges.extend((node.lineno, alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            target = ("." * node.level) + (node.module or "")
            if node.level:
                target = resolve_name(target, package)
            edges.append((node.lineno, target))
            edges.extend((node.lineno, f"{target}.{alias.name}") for alias in node.names)
        elif isinstance(node, ast.Call):
            func = node.func
            builtin_dynamic = (isinstance(func, ast.Name) and func.id in builtin_import_functions) or (
                isinstance(func, ast.Attribute)
                and func.attr == "__import__"
                and isinstance(func.value, ast.Name)
                and func.value.id in builtin_import_modules
            )
            if builtin_dynamic:
                edges.extend((node.lineno, target) for target in _builtin_import_targets(node, package))
                continue
            dynamic = (isinstance(func, ast.Name) and func.id in import_functions) or (
                isinstance(func, ast.Attribute)
                and func.attr == "import_module"
                and isinstance(func.value, ast.Name)
                and func.value.id in import_modules
            )
            argument = _import_call_argument(node, 0, "name")
            if dynamic and isinstance(argument, ast.Constant) and isinstance(argument.value, str):
                target = argument.value
                if target.startswith("."):
                    dynamic_package = package
                    if len(node.args) > 1 and isinstance(node.args[1], ast.Constant):
                        dynamic_package = str(node.args[1].value)
                    for keyword in node.keywords:
                        if keyword.arg == "package" and isinstance(keyword.value, ast.Constant):
                            dynamic_package = str(keyword.value.value)
                    target = resolve_name(target, dynamic_package)
                edges.append((node.lineno, target))
    return sorted(
        {
            f"{module}:{line}: {owner} must not import {target}"
            for line, target in edges
            for denied in _INVOKER_FORBIDDEN[owner]
            if target == _INVOKER_PREFIX + denied or target.startswith(_INVOKER_PREFIX + denied + ".")
        }
    )


def test_invoker_package_import_directions() -> None:
    violations: list[str] = []
    for owner in _INVOKER_FORBIDDEN:
        for path in sorted((_INVOKER_ROOT / owner).rglob("*.py")):
            module = _INVOKER_PREFIX + path.relative_to(_INVOKER_ROOT).with_suffix("").as_posix().replace("/", ".")
            violations.extend(_invoker_import_violations(path.read_text(encoding="utf-8"), module))
    assert not violations, "\n".join(violations)


@pytest.mark.parametrize(
    "source",
    [
        "import chrys.orchestration.engine.executor",
        "from chrys.orchestration import engine",
        "from ..engine import executor",
        "def late():\n    from chrys.orchestration.sub_agents import tools",
        "import importlib as il\ndef late():\n    il.import_module('chrys.orchestration.engine')",
        "from importlib import import_module as load\nload('..sub_agents', package='chrys.orchestration.invoker')",
        "__import__('chrys.orchestration.engine.executor')",
        "__import__(name='chrys.orchestration.engine.executor')",
        "import importlib\nimportlib.import_module(name='chrys.orchestration.engine')",
        "import importlib as il\nil.import_module(name='..engine', package='chrys.orchestration.invoker')",
        "from importlib import import_module as load\nload(name='..sub_agents', package='chrys.orchestration.invoker')",
        "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    from ..engine import executor",
    ],
)
def test_invoker_rejects_caller_imports(source: str) -> None:
    assert _invoker_import_violations(source, _INVOKER_PREFIX + "invoker.kernel")


@pytest.mark.parametrize(
    ("module", "source", "targets"),
    [
        pytest.param(
            "sub_agents.__init__",
            "__import__('chrys.orchestration', fromlist=['engine'])",
            ("engine",),
            id="absolute-fromlist",
        ),
        pytest.param(
            "invoker.kernel",
            "__import__(name='chrys.orchestration', fromlist=('sub_agents',))",
            ("sub_agents",),
            id="keyword-name-tuple-fromlist",
        ),
        pytest.param(
            "sub_agents.__init__",
            "__import__('engine', globals={'__package__': 'chrys.orchestration'}, fromlist=['executor'], level=1)",
            ("engine", "engine.executor"),
            id="relative-globals-fromlist",
        ),
        pytest.param(
            "sub_agents.__init__",
            "__import__('engine', globals={'__name__': 'chrys.orchestration.sub_agents'}, "
            "fromlist=['executor'], level=1)",
            ("engine", "engine.executor"),
            id="name-fallback",
        ),
        pytest.param(
            "sub_agents.__init__",
            "__import__('invoker', globals={'__name__': 'chrys.orchestration.sub_agents'}, "
            "fromlist=['kernel'], level=1)",
            (),
            id="allowed-name-fallback",
        ),
        pytest.param(
            "sub_agents.__init__",
            "__import__('engine', globals={'__package__': None, '__name__': 'chrys.orchestration.sub_agents'}, "
            "fromlist=['executor'], level=1)",
            ("engine", "engine.executor"),
            id="package-none-fallback",
        ),
        pytest.param(
            "sub_agents.__init__",
            "__import__('invoker', globals={'__package__': None, '__name__': 'chrys.orchestration.sub_agents'}, "
            "fromlist=['kernel'], level=1)",
            (),
            id="allowed-package-none-fallback",
        ),
        pytest.param(
            "sub_agents.__init__",
            "__import__('engine', globals={'__name__': 'chrys.orchestration', '__path__': []}, "
            "fromlist=['executor'], level=1)",
            ("engine", "engine.executor"),
            id="name-path-fallback",
        ),
        pytest.param(
            "sub_agents.__init__",
            "__import__('invoker', globals={'__name__': 'chrys.orchestration', '__path__': []}, "
            "fromlist=['kernel'], level=1)",
            (),
            id="allowed-name-path-fallback",
        ),
        pytest.param(
            "sub_agents.__init__",
            "__import__('engine', globals={'__package__': 'chrys.orchestration', '__name__': 'other.module'}, "
            "fromlist=['executor'], level=1)",
            ("engine", "engine.executor"),
            id="package-precedes-name",
        ),
        pytest.param(
            "sub_agents.__init__",
            "__import__('engine', globals={'__package__': 'other', '__package__': 'chrys.orchestration'}, "
            "fromlist=['executor'], level=1)",
            ("engine", "engine.executor"),
            id="last-literal-package-wins",
        ),
        pytest.param(
            "sub_agents.__init__",
            "__import__('engine', globals={'__name__': 'chrys.orchestration', '__path__': None}, "
            "fromlist=['executor'], level=1)",
            ("engine", "engine.executor"),
            id="path-presence-not-truthiness",
        ),
        pytest.param(
            "invoker.kernel",
            "__import__('', {'__package__': 'chrys.orchestration'}, {}, ('engine', 'sub_agents'), 1)",
            ("engine", "sub_agents"),
            id="all-positional",
        ),
        pytest.param(
            "invoker.kernel",
            "__import__(name='', globals={'__package__': 'chrys.orchestration.invoker'}, "
            "locals={}, fromlist=['engine'], level=2)",
            ("engine",),
            id="all-keyword-level-two",
        ),
        pytest.param(
            "sub_agents.__init__",
            "__import__('', globals(), None, ['engine'], 2)",
            ("engine",),
            id="current-init-package",
        ),
        pytest.param(
            "invoker.kernel",
            "__import__('', globals=globals(), fromlist=('sub_agents',), level=2)",
            ("sub_agents",),
            id="current-module-package",
        ),
        pytest.param(
            "invoker.kernel",
            "__import__('', fromlist=['engine'], level=2)",
            (),
            id="missing-globals-is-invalid",
        ),
        pytest.param(
            "invoker.kernel",
            "__import__('chrys.orchestration', {'__package__': 'unrelated'}, None, ['engine'], 0)",
            ("engine",),
            id="absolute-ignores-package",
        ),
        pytest.param(
            "invoker.kernel",
            "__import__('chrys.kernel', fromlist=['Agent'])",
            (),
            id="allowed-public-kernel",
        ),
        pytest.param(
            "sub_agents.tools",
            "__import__('', globals={'__package__': 'chrys.orchestration'}, fromlist=['invoker'], level=1)",
            (),
            id="allowed-child-to-invoker",
        ),
        pytest.param(
            "engine.build.builder",
            "__import__('chrys.orchestration', fromlist=['invoker', 'sub_agents'])",
            (),
            id="allowed-composition-root",
        ),
    ],
)
def test_invoker_builtin_import_directions(module: str, source: str, targets: tuple[str, ...]) -> None:
    qualified_module = _INVOKER_PREFIX + module
    owner = module.split(".")[0]
    assert _invoker_import_violations(source, qualified_module) == sorted(
        f"{qualified_module}:1: {owner} must not import {_INVOKER_PREFIX}{target}" for target in targets
    )


@pytest.mark.parametrize(
    "globals_source",
    [
        pytest.param("None", id="explicit-none"),
        pytest.param("{}", id="missing-name"),
        pytest.param("{'__package__': 1, '__name__': 'chrys.orchestration.child'}", id="invalid-package"),
        pytest.param("{'__name__': 1}", id="invalid-name"),
        pytest.param("{'__name__': 'chrys.orchestration.child', '__spec__': None}", id="spec-none"),
        pytest.param("{'__package__': 'chrys.orchestration', '__spec__': None}", id="package-with-spec"),
        pytest.param("{'__name__': 'chrys.orchestration.child', '__spec__': spec}", id="computed-spec"),
        pytest.param("{'__package__': 'chrys.orchestration', **extra}", id="dict-unpacking"),
        pytest.param("{'__package__': 'chrys.orchestration', key: 'other'}", id="computed-key"),
        pytest.param("{'__package__': package}", id="computed-package"),
        pytest.param("{'__name__': name}", id="computed-name"),
        pytest.param("{'__name__': 'chrys.orchestration', '__path__': path}", id="computed-path"),
        pytest.param("{'__package__': 'chrys.orchestration', 'other': value}", id="computed-other-value"),
        pytest.param("{'__package__': 'chrys.orchestration', []: None}", id="invalid-dict-key"),
    ],
)
def test_invoker_builtin_import_unresolved_globals(globals_source: str) -> None:
    call = ast.parse(
        f"__import__('engine', globals={globals_source}, fromlist=['executor'], level=1)", mode="eval"
    ).body
    assert isinstance(call, ast.Call)
    assert _builtin_import_targets(call, _INVOKER_PREFIX + "sub_agents") == []


@pytest.mark.parametrize("stdlib_module", ["builtins", "importlib"])
@pytest.mark.parametrize(
    ("source", "targets"),
    [
        pytest.param(
            "import {stdlib_module}; {stdlib_module}.__import__('chrys.orchestration', fromlist=['engine'])",
            ("engine",),
            id="attribute-fromlist",
        ),
        pytest.param(
            "import {stdlib_module}; {stdlib_module}.__import__('chrys.orchestration.engine.executor')",
            ("engine.executor",),
            id="attribute-absolute-name",
        ),
        pytest.param(
            "import {stdlib_module} as lib; lib.__import__('chrys.orchestration', fromlist=['engine'])",
            ("engine",),
            id="module-alias",
        ),
        pytest.param(
            "from {stdlib_module} import __import__; __import__('chrys.orchestration', fromlist=['engine'])",
            ("engine",),
            id="from-import",
        ),
        pytest.param(
            "from {stdlib_module} import __import__ as load; load(name='chrys.orchestration', fromlist=['engine'])",
            ("engine",),
            id="from-import-alias",
        ),
        pytest.param(
            "import {stdlib_module} as lib; lib.__import__('', globals(), None, ['engine'], 2)",
            ("engine",),
            id="attribute-relative-level",
        ),
        pytest.param(
            "from {stdlib_module} import __import__ as load; "
            "load('', globals=globals(), locals=None, fromlist=['engine'], level=2)",
            ("engine",),
            id="from-import-relative-level",
        ),
        pytest.param(
            "import {stdlib_module}; {stdlib_module}.__import__('chrys.orchestration', fromlist=['invoker'])",
            (),
            id="allowed-attribute-child-to-invoker",
        ),
        pytest.param(
            "from {stdlib_module} import __import__ as load; "
            "load('', globals=globals(), fromlist=['invoker'], level=2)",
            (),
            id="allowed-alias-child-to-invoker",
        ),
    ],
)
def test_invoker_stdlib_builtin_import_directions(stdlib_module: str, source: str, targets: tuple[str, ...]) -> None:
    module = _INVOKER_PREFIX + "sub_agents.__init__"
    assert _invoker_import_violations(source.format(stdlib_module=stdlib_module), module) == sorted(
        f"{module}:1: sub_agents must not import {_INVOKER_PREFIX}{target}" for target in targets
    )


@pytest.mark.parametrize(
    "source",
    [
        "other.__import__('chrys.orchestration.engine')",
        "builtins.__import__('chrys.orchestration.engine')",
        "importlib.__import__('chrys.orchestration.engine')",
        "import other as builtins; builtins.__import__('chrys.orchestration.engine')",
        "import other as importlib; importlib.__import__('chrys.orchestration.engine')",
        "from other import __import__ as load; load('chrys.orchestration.engine')",
        "from .builtins import __import__ as load; load('chrys.orchestration.engine')",
        "from .importlib import __import__ as load; load('chrys.orchestration.engine')",
    ],
)
def test_invoker_ignores_unrecognized_builtin_import_callables(source: str) -> None:
    assert _invoker_import_violations(source, _INVOKER_PREFIX + "sub_agents.tools") == []


def test_child_rejects_engine_but_composition_root_can_import_both() -> None:
    assert _invoker_import_violations("from ..engine import executor", _INVOKER_PREFIX + "sub_agents.controller")
    for module in ("invoker.kernel", "sub_agents.tools"):
        assert (
            _invoker_import_violations(f"import {_INVOKER_PREFIX}{module}", _INVOKER_PREFIX + "engine.build.builder")
            == []
        )
    assert _invoker_import_violations("from ..invoker import kernel", _INVOKER_PREFIX + "sub_agents.tools") == []
    assert _invoker_import_violations("from chrys.kernel import Agent", _INVOKER_PREFIX + "invoker.kernel") == []


def _borrowed_resource_close_lines(source: str) -> list[int]:
    """Engine convenience fields borrow resources; their owners release them."""
    borrowed = {"agent", "mcp_adapter", "sub_agent_tools"}
    closing = {"close", "aclose", "__aexit__", "cleanup", "disconnect_all"}
    return [
        node.lineno
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in closing
        and isinstance(node.func.value, ast.Attribute)
        and node.func.value.attr in borrowed
    ]


def test_engine_borrowed_resources_have_no_close_authority() -> None:
    violations = [
        f"{path.relative_to(ROOT)}:{line}"
        for path in sorted((SRC / "orchestration" / "engine").rglob("*.py"))
        for line in _borrowed_resource_close_lines(path.read_text(encoding="utf-8"))
    ]
    assert violations == [], "Close through Prepared/Conversation owners: " + ", ".join(violations)


@pytest.mark.parametrize("field", ["agent", "mcp_adapter", "sub_agent_tools"])
@pytest.mark.parametrize("method", ["close", "aclose", "__aexit__", "cleanup", "disconnect_all"])
def test_borrowed_resource_close_guard_rejects_direct_calls(field: str, method: str) -> None:
    assert _borrowed_resource_close_lines(f"current.loaded.{field}.{method}()") == [1]


def test_borrowed_resource_close_guard_accepts_owners_and_borrowed_reads() -> None:
    assert (
        _borrowed_resource_close_lines("self._prepared_agent.aclose()\nself._conversation.aclose()\nself._agent.run()")
        == []
    )


def test_lifecycle_permit_operations_have_one_owner() -> None:
    """The engine exposes clocks while permit operations belong to their component."""
    from chrys.orchestration.engine.engine import AgentEngine
    from chrys.orchestration.engine.run.coordinator import TurnCoordinator
    from chrys.orchestration.engine.run.turn_state import TurnRuntimeState
    from chrys.orchestration.engine.state.lifecycle_permits import LifecyclePermits

    for name in ("capture_rebuild_control_token", "acquire_rebuild_permit", "release_rebuild_permit"):
        assert name not in AgentEngine.__dict__, name
    assert "capture_control_token" in LifecyclePermits.__dict__
    assert "acquire_rebuild_permit" in LifecyclePermits.__dict__
    assert "release_rebuild_permit" in LifecyclePermits.__dict__
    assert "drain_run_task_chain_for_boundary" not in AgentEngine.__dict__
    assert "drain_run_task_chain_for_boundary" not in TurnCoordinator.__dict__
    assert "drain_for_boundary" in TurnRuntimeState.__dict__


def test_session_load_bodies_are_called_only_by_permit_wrappers() -> None:
    """Only the public permit wrappers may enter the locked load bodies."""
    root = SRC / "orchestration" / "engine"
    callers: dict[str, set[tuple[str, str, str]]] = {"_start_locked": set(), "_reload_locked": set()}

    def collect(node: ast.AST, module: str, classes: tuple[str, ...] = (), functions: tuple[str, ...] = ()) -> None:
        if isinstance(node, ast.ClassDef):
            classes = (*classes, node.name)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            functions = (*functions, node.name)
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in callers:
            callers[node.func.attr].add((module, ".".join(classes), ".".join(functions)))
        for child in ast.iter_child_nodes(node):
            collect(child, module, classes, functions)

    for path in sorted(root.rglob("*.py")):
        collect(ast.parse(path.read_text(encoding="utf-8")), path.relative_to(root).with_suffix("").as_posix())
    assert callers == {
        "_start_locked": {("session_lifecycle", "SessionLifecycle", "start")},
        "_reload_locked": {("session_lifecycle", "SessionLifecycle", "reload")},
    }


def _build_host_assignment_lines(source: str) -> list[int]:
    """Candidate construction cannot assign through a live state receiver."""
    components = {"session", "permits", "current", "writer", "usage_publisher", "loader", "turns", "lifecycle"}
    violations: list[int] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, (ast.Attribute, ast.Subscript)) or not isinstance(node.ctx, ast.Store):
            continue
        receiver: ast.expr = node
        chain: list[str] = []
        while isinstance(receiver, (ast.Attribute, ast.Subscript)):
            if isinstance(receiver, ast.Attribute):
                chain.append(receiver.attr)
            receiver = receiver.value
        if isinstance(receiver, ast.Name) and (
            receiver.id in {"engine", "host", "loader", "session", "current"}
            or (receiver.id == "self" and chain and chain[-1].removeprefix("_") in components)
        ):
            violations.append(node.lineno)
    return violations


def test_build_package_returns_values_instead_of_assigning_engine_state() -> None:
    violations = [
        f"{path.relative_to(ROOT)}:{line}"
        for path in sorted((SRC / "orchestration" / "engine" / "build").rglob("*.py"))
        for line in _build_host_assignment_lines(path.read_text(encoding="utf-8"))
    ]
    assert violations == [], "Build candidates cannot write live state: " + ", ".join(violations)


@pytest.mark.parametrize(
    "statement",
    [
        "engine._agent = result.agent",
        "host.session.todo_tracker = tracker",
        "session.workspace = workspace",
        "current.loaded = result",
        "loader.current.manifest = manifest",
        "self._current.loaded = result",
        "engine._intermediate_texts[batch_id] = text",
    ],
)
def test_build_assignment_guard_rejects_live_state_writes(statement: str) -> None:
    assert _build_host_assignment_lines(statement) == [1]


def test_build_assignment_guard_accepts_candidate_wiring() -> None:
    assert (
        _build_host_assignment_lines("result.bindings.backend.history_state = preserved\nlocal[batch_id] = text") == []
    )


def _current_record_assignment_lines(source: str, *, module: str) -> list[int]:
    """Only installation, refresh, and close may replace the current records."""
    allowed = {
        "orchestration/engine/loader.py": {"install", "release_current", "apply_skill_refresh"},
        "orchestration/engine/state/current_agent.py": {"__init__"},
    }
    violations: list[int] = []

    def walk(node: ast.AST, method: str = "") -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            method = node.name
        if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Store) and node.attr in {"loaded", "manifest"}:
            receiver = ast.unparse(node.value)
            if (
                receiver == "current"
                or receiver.endswith((".current", "._current"))
                or (receiver == "self" and module.endswith("/current_agent.py"))
            ) and method not in allowed.get(module, set()):
                violations.append(node.lineno)
        for child in ast.iter_child_nodes(node):
            walk(child, method)

    walk(ast.parse(source))
    return violations


def test_current_agent_records_have_explicit_writers() -> None:
    violations = [
        f"{path.relative_to(SRC)}:{line}"
        for path in sorted(SRC.rglob("*.py"))
        for line in _current_record_assignment_lines(
            path.read_text(encoding="utf-8"), module=path.relative_to(SRC).as_posix()
        )
    ]
    assert violations == []


@pytest.mark.parametrize("receiver", ["current", "self._current", "engine.current"])
@pytest.mark.parametrize("field", ["loaded", "manifest"])
def test_current_record_guard_rejects_unowned_assignment(receiver: str, field: str) -> None:
    assert _current_record_assignment_lines(f"{receiver}.{field} = None", module="other.py") == [1]


def test_test_build_installation_has_one_writer() -> None:
    violations = [
        f"{path.relative_to(ROOT)}:{line}"
        for path in sorted((ROOT / "tests").rglob("*.py"))
        if path.relative_to(ROOT).as_posix() != "tests/support/loaded_agents.py"
        for line in _current_record_assignment_lines(path.read_text(encoding="utf-8"), module="test")
    ]
    assert violations == []


def _manifest_mutation_lines(source: str) -> list[int]:
    """Reject ordinary assignments and container mutation through manifest values."""
    mutators = {"append", "extend", "update", "clear", "pop", "setdefault", "remove"}

    def belongs_to_manifest(node: ast.AST, aliases: set[str]) -> bool:
        while isinstance(node, (ast.Attribute, ast.Subscript)):
            if isinstance(node, ast.Attribute):
                if node.attr == "manifest":
                    return True
                node = node.value
            else:
                node = node.value
        return isinstance(node, ast.Name) and node.id in {"manifest", "runtime_details", "active_profile", *aliases}

    violations: set[int] = set()

    def walk(node: ast.AST, aliases: set[str] | None = None) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            aliases = set()
        elif isinstance(node, ast.ClassDef):
            aliases = None
        if (
            aliases is not None
            and isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
        ):
            name = node.targets[0].id
            if belongs_to_manifest(node.value, set()):
                aliases.add(name)
            else:
                aliases.discard(name)
        if (
            (
                isinstance(node, (ast.Attribute, ast.Subscript))
                and isinstance(node.ctx, (ast.Store, ast.Del))
                and belongs_to_manifest(node.value, aliases or set())
            )
            or (
                isinstance(node, ast.AugAssign)
                and isinstance(node.target, ast.Name)
                and belongs_to_manifest(node.target, aliases or set())
            )
            or (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in mutators
                and belongs_to_manifest(node.func.value, aliases or set())
            )
        ):
            violations.add(node.lineno)
        for child in ast.iter_child_nodes(node):
            walk(child, aliases)

    walk(ast.parse(source))
    return sorted(violations)


def test_agent_manifest_is_never_mutated_in_place() -> None:
    violations = [
        f"{path.relative_to(SRC)}:{line}"
        for path in sorted(SRC.rglob("*.py"))
        for line in _manifest_mutation_lines(path.read_text(encoding="utf-8"))
    ]
    assert violations == []


@pytest.mark.parametrize(
    "statement",
    [
        "manifest.skill_names = []",
        "current.manifest.runtime_details.skill_sources.clear()",
        "engine.current.manifest.tool_kinds['shell'] = 'shell'",
        "runtime_details.skill_details.append(item)",
        "active_profile.model_id = 'changed'",
        "manifest.skill_names += ('new',)",
        "del manifest.tool_kinds['shell']",
    ],
)
def test_manifest_guard_rejects_in_place_writes(statement: str) -> None:
    assert _manifest_mutation_lines(statement) == [1]


@pytest.mark.parametrize("function", ["def", "async def"])
@pytest.mark.parametrize(
    "statement",
    [
        "details.skill_sources.clear()",
        "details.skill_details.append(item)",
        "details.skill_sources['skill'] = []",
        "details.skill_sources = {}",
        "details.skill_details += [item]",
        "del details.skill_sources['skill']",
        "del details.skill_details",
    ],
)
def test_manifest_guard_rejects_local_alias_writes(function: str, statement: str) -> None:
    source = f"{function} refresh(current):\n    details = current.manifest.runtime_details\n    {statement}\n"
    assert _manifest_mutation_lines(source) == [3]


@pytest.mark.parametrize("statement", ["values.clear()", "values[0] = item", "values += [item]", "del values[0]"])
def test_manifest_guard_rejects_mutation_of_aliased_container(statement: str) -> None:
    source = f"def refresh(current):\n    values = current.manifest.runtime_details.skill_details\n    {statement}\n"
    assert _manifest_mutation_lines(source) == [3]


@pytest.mark.parametrize(
    "source",
    [
        "def refresh(current):\n    details = replace(current.manifest.runtime_details)\n    details.skill_sources.clear()",
        "def refresh(current):\n    details.skill_sources.clear()\n    details = current.manifest.runtime_details",
        (
            "def refresh(current):\n    details = current.manifest.runtime_details\n    details = fresh()\n"
            "    details.skill_sources.clear()"
        ),
        (
            "def read(current):\n    details = current.manifest.runtime_details\n"
            "def refresh(details):\n    details.skill_sources.clear()"
        ),
        (
            "def read(current):\n    details = current.manifest.runtime_details\n"
            "    def refresh(details):\n        details.skill_sources.clear()"
        ),
    ],
)
def test_manifest_guard_keeps_aliases_local_and_distinguishes_new_values(source: str) -> None:
    assert _manifest_mutation_lines(source) == []


@pytest.mark.parametrize("method", ["commit_build", "apply_skill_refresh", "shutdown"])
def test_current_record_guard_rejects_engine_writer_methods(method: str) -> None:
    source = f"def {method}(self):\n    self._current.loaded = None\n"
    assert _current_record_assignment_lines(source, module="orchestration/engine/engine.py") == [2]


def _loader_install_violations(source: str) -> list[str]:
    """Installation may only assign values already prepared by construction."""
    allowed_calls = {
        "ReplacedBuild",
        "self._settings_handle.install_prepared",
        "self._session.install_build",
        "self._history.bind",
        "self._workspace_change_tracker.apply_retarget",
        "self._permits.advance_build_generation",
    }
    return [
        ast.unparse(node)
        for node in ast.walk(ast.parse(source))
        if isinstance(node, (ast.Await, ast.Try, ast.TryStar))
        or (isinstance(node, ast.Call) and ast.unparse(node.func) not in allowed_calls)
    ]


def test_loader_install_only_swaps_prepared_values() -> None:
    module = ast.parse((SRC / "orchestration" / "engine" / "loader.py").read_text(encoding="utf-8"))
    loader = next(node for node in module.body if isinstance(node, ast.ClassDef) and node.name == "AgentLoader")
    install = next(node for node in loader.body if isinstance(node, ast.FunctionDef) and node.name == "install")
    assert _loader_install_violations(ast.unparse(install)) == []


@pytest.mark.parametrize(
    "statement",
    [
        "await work()",
        "try:\n    pass\nfinally:\n    pass",
        "self._settings_handle.install(completed.staged.loaded)",
        "stamp_history_item_ids(state)",
        "self._workspace_change_tracker.retarget_roots(workspace)",
        "self._session.runtime_meta.restore_context_calibration(strategy)",
        "os.path.realpath(cwd)",
    ],
)
def test_loader_install_guard_rejects_fallible_work(statement: str) -> None:
    assert _loader_install_violations(statement)


_SESSION_IDENTITY_OPERATIONS = {
    "__init__",
    "begin",
    "reset",
    "adopt_restore_identity",
    "mark_closing",
    "mark_session_end_fired",
    "mark_recovered_from_sidecar",
    "detach_for_delete",
    "reattach_after_failed_delete",
}


def _session_identity_write_lines(source: str, *, owner_module: bool = False) -> list[int]:
    identity = {"session_id", "shutting_down", "session_end_fired", "recovered_from_sidecar"}
    violations: list[int] = []

    class Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.function = ""

        def visit_FunctionDef(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
            previous = self.function
            self.function = node.name
            self.generic_visit(node)
            self.function = previous

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_Attribute(self, node: ast.Attribute) -> None:
            if isinstance(node.ctx, (ast.Store, ast.Del)) and node.attr in identity:
                receiver = node.value
                session_receiver = (
                    (isinstance(receiver, ast.Name) and receiver.id == "session")
                    or (isinstance(receiver, ast.Attribute) and receiver.attr == "session")
                    or (
                        isinstance(receiver, ast.Attribute)
                        and receiver.attr == "_session"
                        and isinstance(receiver.value, ast.Name)
                        and receiver.value.id == "self"
                    )
                )
                owner_receiver = owner_module and isinstance(receiver, ast.Name) and receiver.id == "self"
                if session_receiver or (owner_receiver and self.function not in _SESSION_IDENTITY_OPERATIONS):
                    violations.append(node.lineno)
            self.generic_visit(node)

    Visitor().visit(ast.parse(source))
    return violations


def test_session_identity_changes_through_session_owners() -> None:
    violations = []
    owners = {
        SRC / "orchestration" / "engine" / "state" / "active_session.py",
        SRC / "orchestration" / "session_resources.py",
    }
    for path in sorted(SRC.rglob("*.py")):
        violations.extend(
            f"{path.relative_to(ROOT)}:{line}"
            for line in _session_identity_write_lines(path.read_text(encoding="utf-8"), owner_module=path in owners)
        )
    assert violations == [], "Session identity must change through session owner operations: " + ", ".join(violations)


@pytest.mark.parametrize("receiver", ["session", "self._session", "engine.session", "container.engine.session"])
@pytest.mark.parametrize("field", ["session_id", "shutting_down", "session_end_fired", "recovered_from_sidecar"])
def test_session_identity_guard_rejects_direct_writes(receiver: str, field: str) -> None:
    assert _session_identity_write_lines(f"{receiver}.{field} = None") == [1]
    assert _session_identity_write_lines(f"{receiver}.{field}: object = None") == [1]


@pytest.mark.parametrize("method", ["__init__", "begin", "mark_closing", "unowned_write"])
def test_session_identity_guard_checks_owner_operations(method: str) -> None:
    source = f"class ActiveSession:\n def {method}(self):\n  self.shutting_down = True"
    assert _session_identity_write_lines(source, owner_module=True) == ([] if method != "unowned_write" else [3])


@pytest.mark.parametrize("path", ["kernel/sessions.py", "app/acp/server.py", "service/state/session_mru.py"])
def test_session_identity_guard_accepts_other_objects_session_ids(path: str) -> None:
    assert _session_identity_write_lines((SRC / path).read_text(encoding="utf-8")) == []


@pytest.mark.parametrize(
    "name",
    [
        "session",
        "permits",
        "current",
        "writer",
        "usage_publisher",
        "loader",
        "lifecycle",
        "rollback",
        "controls",
        "turns",
    ],
)
def test_engine_components_are_read_only_properties(name: str) -> None:
    """The composition root exposes component ownership without writable slots."""
    from chrys.foundation.config.settings import Settings
    from chrys.foundation.events.bus import EventBus
    from chrys.orchestration.engine.assembly import assemble_agent_engine
    from chrys.orchestration.engine.engine import AgentEngine

    member = AgentEngine.__dict__[name]
    assert isinstance(member, property), name
    assert member.fset is None, name
    assert member.fget is not None, name
    engine = assemble_agent_engine(EventBus(), settings=Settings())
    assert type(member.fget(engine)) is get_type_hints(member.fget)["return"], name


def _ownership_nodes(
    node: ast.AST,
    module: str,
    classes: tuple[str, ...] = (),
    functions: tuple[str, ...] = (),
):
    """Visit nested bodies with module, class-chain, and function-chain identity."""
    if isinstance(node, ast.ClassDef):
        classes = (*classes, node.name)
    elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        functions = (*functions, node.name)
    yield node, (module, ".".join(classes), ".".join(functions))
    for child in ast.iter_child_nodes(node):
        yield from _ownership_nodes(child, module, classes, functions)


def test_engine_is_only_received_by_its_assembly() -> None:
    """Component modules cannot name the engine or receive its facade as a host."""
    root = SRC / "orchestration" / "engine"
    violations = []
    paths = sorted(root.rglob("*.py")) + sorted((SRC / "service").rglob("*.py"))
    for path in paths:
        if path in {root / "engine.py", root / "assembly.py"}:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        module = path.relative_to(SRC).with_suffix("").as_posix()
        annotation_strings = set()
        for node in ast.walk(tree):
            annotation = None
            if isinstance(node, (ast.arg, ast.AnnAssign)):
                annotation = node.annotation
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                annotation = node.returns
            if annotation is not None:
                annotation_strings.update(
                    id(part)
                    for part in ast.walk(annotation)
                    if isinstance(part, ast.Constant) and isinstance(part.value, str)
                )
        for node, site in _ownership_nodes(tree, module):
            engine_reference = (
                (isinstance(node, ast.Name) and node.id == "AgentEngine")
                or (isinstance(node, ast.Attribute) and node.attr == "AgentEngine")
                or (isinstance(node, ast.alias) and node.name.split(".")[-1] == "AgentEngine")
            )
            if isinstance(node, ast.Constant) and id(node) in annotation_strings and "AgentEngine" in node.value:
                try:
                    quoted = ast.parse(node.value, mode="eval")
                except SyntaxError:
                    continue
                engine_reference = any(
                    (isinstance(part, ast.Name) and part.id == "AgentEngine")
                    or (isinstance(part, ast.Attribute) and part.attr == "AgentEngine")
                    for part in ast.walk(quoted)
                )
            if engine_reference:
                violations.append((site, node.lineno, "AgentEngine reference"))
    tree = ast.parse((root / "engine.py").read_text(encoding="utf-8"))
    quoted_hosts = set()
    for node in ast.walk(tree):
        annotation = None
        if isinstance(node, (ast.arg, ast.AnnAssign)):
            annotation = node.annotation
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            annotation = node.returns
        if annotation is None:
            continue
        for part in ast.walk(annotation):
            if not isinstance(part, ast.Constant) or not isinstance(part.value, str):
                continue
            try:
                quoted = ast.parse(part.value, mode="eval")
            except SyntaxError:
                continue
            if any(
                (isinstance(value, ast.Name) and value.id.endswith("Host"))
                or (isinstance(value, ast.Attribute) and value.attr.endswith("Host"))
                for value in ast.walk(quoted)
            ):
                quoted_hosts.add(id(part))
    for node, site in _ownership_nodes(tree, "orchestration/engine/engine"):
        if id(node) in quoted_hosts or (isinstance(node, ast.ClassDef) and node.name.endswith("Host")):
            violations.append((site, node.lineno, "Host type reference"))
        if isinstance(node, (ast.Name, ast.Attribute)):
            name = node.id if isinstance(node, ast.Name) else node.attr
            if name.endswith("Host"):
                violations.append((site, node.lineno, "Host type reference"))
        if not isinstance(node, ast.Call):
            continue
        target = node.func.id if isinstance(node.func, ast.Name) else ""
        if target in {"_set_current_engine", "_unset_current_engine"}:
            continue
        if any(
            isinstance(value, ast.Name) and value.id == "self"
            for value in [*node.args, *(kw.value for kw in node.keywords)]
        ):
            violations.append((site, node.lineno, "bare self passed to a call"))
    assert violations == [], f"Inject explicit components or capabilities: {violations}"


def _engine_component_access_violations(source: str, module: str) -> list[tuple[tuple[str, str, str], int, str]]:
    """Reject borrowing a component, including a read saved into a local alias."""
    components = {
        "session",
        "permits",
        "current",
        "writer",
        "usage_publisher",
        "loader",
        "turns",
        "rollback",
        "controls",
        "lifecycle",
    }
    # These names denote admission records, a kernel session, and a recorder method.
    record_reads = {
        (("execution", "PreAdmissionPreparationTracker", "preparation"), "self.current.preparation"),
        (("run/active_injection", "_PreparationOwnership", "hand_off"), "self.tracker.current"),
        (("run/resume", "TurnResumePolicy", "retry_request"), "self.backend.session"),
        (("run/resume", "TurnResumePolicy", "retry_request"), "self.backend.session.state"),
        (
            ("rollback", "RollbackController", "_execute_user_rollback_after_resource_preflight"),
            "self._trajectory_recorder.rollback",
        ),
    }
    violations = []
    for node, site in _ownership_nodes(ast.parse(source), module):
        if not isinstance(node, ast.Attribute):
            continue
        expression = ast.unparse(node)
        if (site, expression) in record_reads:
            continue
        middle = node.value
        if node.attr.removeprefix("_") in components and (
            isinstance(middle, ast.Attribute)
            or (isinstance(middle, ast.Name) and middle.id.removeprefix("_") in components)
        ):
            violations.append((site, node.lineno, expression))
            continue
        if not isinstance(middle, ast.Attribute):
            continue
        component = middle.attr.removeprefix("_")
        if component not in components:
            continue
        if middle.attr.startswith("_") and isinstance(middle.value, ast.Name) and middle.value.id == "self":
            continue
        if (
            component == "current"
            and node.attr in {"loaded", "manifest"}
            and isinstance(middle.value, ast.Name)
            and middle.value.id == "self"
        ):
            continue
        violations.append((site, node.lineno, expression))
    return violations


def test_engine_components_are_not_reached_through_each_other() -> None:
    """Direct dependencies stay explicit instead of being fetched from another owner."""
    root = SRC / "orchestration" / "engine"
    violations = []
    for path in sorted(root.rglob("*.py")):
        if path == root / "engine.py":
            continue
        module = path.relative_to(root).with_suffix("").as_posix()
        violations.extend(_engine_component_access_violations(path.read_text(encoding="utf-8"), module))
    assert violations == [], f"Use a directly injected owner: {violations}"


@pytest.mark.parametrize(
    "expression",
    [
        "self._writer._session",
        "self._writer.session",
        "self.writer.session",
        "self._writer._session.reset",
        "self._hooks._session",
        "self._skills._current",
        "self._content._current",
        "writer._session",
        "loader.current",
        "self.current.writer",
        "self.current.loaded.writer",
    ],
)
def test_component_access_guard_rejects_borrowed_component_aliases(expression: str) -> None:
    source = f"alias = {expression}\nalias.reset(session_id='x')"
    assert _engine_component_access_violations(source, "run/finalizer")


@pytest.mark.parametrize(
    "source",
    [
        "alias = self._session",
        "self._writer.save_current_session()",
        "self._session.session_dir",
        "self._current.loaded.bindings",
        "self.current.loaded.bindings",
        "self.current.manifest.tool_names",
        "turns.turn_state.lease",
    ],
)
def test_component_access_guard_accepts_direct_dependencies_and_owned_records(source: str) -> None:
    assert _engine_component_access_violations(source, "run/finalizer") == []


@pytest.mark.parametrize(
    ("module", "owner", "method", "expression", "neighbor"),
    [
        (
            "execution",
            "PreAdmissionPreparationTracker",
            "preparation",
            "self.current.preparation",
            "self.current.writer",
        ),
        ("run/active_injection", "_PreparationOwnership", "hand_off", "self.tracker.current", "self.tracker.session"),
        (
            "run/resume",
            "TurnResumePolicy",
            "retry_request",
            "self.backend.session.state",
            "self.backend.session.writer",
        ),
        (
            "rollback",
            "RollbackController",
            "_execute_user_rollback_after_resource_preflight",
            "self._trajectory_recorder.rollback",
            "self._trajectory_recorder.session",
        ),
    ],
)
def test_component_record_exemptions_are_exact(
    module: str, owner: str, method: str, expression: str, neighbor: str
) -> None:
    source = f"class {owner}:\n    def {method}(self):\n        alias = {expression}"
    assert _engine_component_access_violations(source, module) == []
    assert _engine_component_access_violations(source, "elsewhere")
    assert _engine_component_access_violations(source.replace(expression, neighbor), module)


def test_background_tasks_have_declared_owners() -> None:
    """Each task creation belongs to an explicitly reviewed lifetime owner."""
    root = SRC / "orchestration" / "engine"
    declared = {
        ("build/construction", "", "build_agent._on_injection_batch_consumed"): (
            1,
            "loader injection notification set",
        ),
        ("engine", "AgentEngine", "_release"): (1, "engine release task"),
        ("execution", "ExecutionLease", "_notify_execution"): (
            1,
            "lease snapshot chain; each publication awaits its predecessor, drained at run completion and owner shutdown",
        ),
        ("loader", "AgentLoader", "start_outbox_recovery"): (1, "session outbox recovery task"),
        ("run/coordinator", "TurnCoordinator", "_admit_user_message"): (2, "execution lease run task"),
        ("run/retry", "RetryCoordinator", "_handle_user_retry"): (2, "execution lease run task"),
        ("run/retry", "RetryCoordinator", "_create_retry_run_task"): (2, "execution lease run task"),
        ("run/turn_hooks", "TurnHookDispatcher", "schedule_user_interrupt"): (1, "dispatcher pending set"),
        (
            "session_lifecycle",
            "SessionLifecycle",
            "_start_locked._report_trajectory_activation_failure._publish_warning",
        ): (1, "activation callback local set"),
        ("state/session_writer", "SessionWriter", "save_checkpoint"): (1, "writer recovery drain task"),
        ("state/session_writer", "SessionWriter", "persist_now"): (1, "writer strict write set"),
        ("state/session_writer", "SessionWriter", "persist_barrier"): (1, "writer strict write set"),
        ("trajectory", "TrajectoryRecorder", "close"): (1, "recorder close task"),
        ("trajectory", "TrajectoryRecorder", "turn_started"): (1, "recorder settlement task"),
        ("session_usage", "SessionUsagePublisher", "enqueue_usage_event"): (
            1,
            "usage publication chain, settled by its session owner",
        ),
    }
    actual = {}
    for path in sorted([*root.rglob("*.py"), root.parent / "session_usage.py"]):
        module = path.stem if path.parent == root.parent else path.relative_to(root).with_suffix("").as_posix()
        for node, site in _ownership_nodes(ast.parse(path.read_text(encoding="utf-8")), module):
            if not isinstance(node, ast.Call):
                continue
            name = (
                node.func.attr
                if isinstance(node.func, ast.Attribute)
                else node.func.id
                if isinstance(node.func, ast.Name)
                else ""
            )
            if name in {"create_task", "ensure_future"}:
                actual[site] = actual.get(site, 0) + 1
    assert actual == {site: count for site, (count, _owner) in declared.items()}, (
        f"Task sites need a declared owner, replacement boundary, and termination policy: {actual}"
    )
