# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The nine test-source hygiene rules, their allowlist liveness pins and red/green proofs."""

from __future__ import annotations

import ast
from collections.abc import Iterator, Mapping, Sequence
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from tests.architecture._hygiene_core import (
    _SCOPE_BOUNDARY_NODES,
    _allowlist_target_trees,
    _meta_guard_problem,
    _pins_allowlist,
    _qualified_name,
    _resolved_import_module,
    _tree,
)
from tests.support.ci import CI_LINUX_ONLY

# Platform-independent source analysis: the Linux CI job covers it.
pytestmark = CI_LINUX_ONLY


_POLLING_HELPER_NAMES = {"_wait_until", "_wait_for", "_poll_until", "_wait_for_ui", "_eventually"}


_LOCAL_POLLING_ALLOWLIST = {
    (Path("tests/orchestration/sub_agents/test_controller.py"), "_wait_until"),
    (Path("tests/integration/engine/test_engine_integration.py"), "_wait_until"),
}


_ENGINE_START_PATH_ALLOWLIST = {
    Path("tests/support/pipeline_helpers.py"),
}


_RAW_TOOL_LOOP_LAYER_ALLOWLIST = {
    # Constructor/delegation pins that never obtain a final response from the
    # layer, so there is no transcript for the oracle to check.
    (Path("tests/kernel/test_loop.py"), "test_getattr_two_hop_delegation"),
    (Path("tests/kernel/test_loop.py"), "test_getattr_attribute_error_passthrough"),
    (Path("tests/kernel/test_loop.py"), "test_ctor_rejects_chat_middleware"),
    (Path("tests/kernel/test_loop.py"), "test_ctor_defaults_mirror_framework_values"),
    # The wire dies mid-stream, no loop iteration ever lands, and the fallback
    # re-merge is not a loop-landed transcript — the invariant oracle's
    # precondition does not hold for that final response.
    (Path("tests/kernel/test_loop_streaming_reconstruction.py"), "test_degenerate_stream_falls_back_to_from_updates"),
    # The checked layer itself wraps the raw layer by deriving from it.
    (Path("tests/support/transcript_invariants.py"), "InvariantCheckedToolLoopLayer"),
}


_DIRECT_TRAJECTORY_PREFIX_ORACLE_ALLOWLIST = {
    # Reader unit tests deliberately compare decoded events with physical
    # slots to pin the distinction the shared oracle must hide from callers.
    Path("tests/foundation/trajectory/test_reader.py"),
    Path("tests/support/trajectory_invariants.py"),
}


def _assert_no_scroll_relative(sources: Mapping[Path, str]) -> None:
    violations = [
        f"{path}:{node.lineno}: use _simulate_chat_panel_user_scroll_y instead of scroll_relative"
        for path, source in sources.items()
        for node in ast.walk(_tree(path, source))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "scroll_relative"
    ]
    assert violations == [], "\n".join(violations)


def _assert_no_unapproved_local_polling_helpers(sources: Mapping[Path, str]) -> None:
    """Ban today's polling identifiers, not structurally similar clones."""
    violations: list[str] = []
    for path, source in sources.items():
        for node in ast.walk(_tree(path, source)):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) or node.name not in _POLLING_HELPER_NAMES:
                continue
            if path == Path("tests/support/waiting.py"):
                continue
            if (path, node.name) in _LOCAL_POLLING_ALLOWLIST:
                continue
            violations.append(f"{path}:{node.lineno}: use tests/support/waiting.py instead of local {node.name}")
    assert violations == [], "\n".join(violations)


# One binding origin per shared helper, so a name bound to wait_for never
# resolves as wait_until and vice versa.
_WAIT_CALLABLES = {helper: f"wait_callable:{helper}" for helper in ("wait_for", "wait_until")}


_WAIT_MODULE = "wait_module"


_WAIT_PACKAGE = "wait_package"


_OTHER_BINDING = "other"


_FUNCTION_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)


def _wait_scope_nodes(scope: ast.AST) -> Iterator[ast.AST]:
    """Walk statements evaluated in one lexical scope, excluding nested scopes."""
    if isinstance(scope, ast.Module):
        stack: list[ast.AST] = list(reversed(scope.body))
    elif isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
        stack = list(reversed(scope.body))
    elif isinstance(scope, ast.Lambda):
        stack = [scope.body]
    else:  # pragma: no cover - callers construct only the scope kinds above
        raise TypeError(f"unsupported scope: {type(scope).__name__}")
    while stack:
        node = stack.pop()
        yield node
        if isinstance(node, _SCOPE_BOUNDARY_NODES):
            continue
        stack.extend(reversed(list(ast.iter_child_nodes(node))))


def _bound_names(target: ast.AST) -> set[str]:
    """Return identifiers bound by an assignment-like target."""
    return {
        node.id
        for node in ast.walk(target)
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del))
    }


def _match_bound_names(pattern: ast.pattern) -> set[str]:
    """Return identifiers captured by a structural-pattern target."""
    names: set[str] = set()
    for node in ast.walk(pattern):
        if isinstance(node, (ast.MatchAs, ast.MatchStar)) and node.name is not None:
            names.add(node.name)
        elif isinstance(node, ast.MatchMapping) and node.rest is not None:
            names.add(node.rest)
    return names


class _WaitUntilScopes:
    """Resolve wait helper imports through Python's enclosing function scopes."""

    def __init__(self, tree: ast.Module, path: Path) -> None:
        self.tree = tree
        self._path = path
        self.scopes: list[ast.AST] = [tree]
        self._parents: dict[int, ast.AST | None] = {id(tree): None}
        self._collect_scopes(tree, tree)
        self._globals: dict[int, set[str]] = {}
        self._nonlocals: dict[int, set[str]] = {}
        self._bindings: dict[int, dict[str, set[str]]] = {}
        for scope in self.scopes:
            nodes = tuple(_wait_scope_nodes(scope))
            self._globals[id(scope)] = {name for node in nodes if isinstance(node, ast.Global) for name in node.names}
            self._nonlocals[id(scope)] = {
                name for node in nodes if isinstance(node, ast.Nonlocal) for name in node.names
            }
            self._bindings[id(scope)] = self._scope_bindings(scope, nodes)
        self._relocate_declared_bindings()

    def _collect_scopes(self, node: ast.AST, parent: ast.AST) -> None:
        if isinstance(node, _FUNCTION_SCOPES):
            self.scopes.append(node)
            self._parents[id(node)] = parent
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                signature_nodes: list[ast.AST] = [node.args, *node.decorator_list]
                if node.returns is not None:
                    signature_nodes.append(node.returns)
                signature_nodes.extend(node.type_params)
                for signature_node in signature_nodes:
                    self._collect_scopes(signature_node, parent)
                for statement in node.body:
                    self._collect_scopes(statement, node)
            else:
                self._collect_scopes(node.body, node)
            return
        if isinstance(node, ast.ClassDef):
            # A class namespace is not an enclosing lexical scope for its methods.
            for child in (*node.decorator_list, *node.bases, *node.keywords, *node.type_params, *node.body):
                self._collect_scopes(child, parent)
            return
        for child in ast.iter_child_nodes(node):
            self._collect_scopes(child, parent)

    @staticmethod
    def _arguments(scope: ast.AST) -> set[str]:
        if not isinstance(scope, _FUNCTION_SCOPES):
            return set()
        arguments = scope.args
        return {
            argument.arg
            for argument in (
                *arguments.posonlyargs,
                *arguments.args,
                *arguments.kwonlyargs,
                *((arguments.vararg,) if arguments.vararg is not None else ()),
                *((arguments.kwarg,) if arguments.kwarg is not None else ()),
            )
        }

    def _import_bindings(self, node: ast.Import | ast.ImportFrom) -> Iterator[tuple[str, str]]:
        if isinstance(node, ast.Import):
            for alias in node.names:
                bound = alias.asname or alias.name.split(".", 1)[0]
                if alias.name == "tests.support.waiting":
                    yield bound, _WAIT_MODULE if alias.asname else _WAIT_PACKAGE
                elif alias.asname is None and (alias.name == "tests" or alias.name.startswith("tests.")):
                    # Every unaliased dotted import binds the same top-level
                    # ``tests`` package; importing a sibling must not obscure
                    # the waiting module imported through that package.
                    yield bound, _WAIT_PACKAGE
                else:
                    yield bound, _OTHER_BINDING
            return
        module = _resolved_import_module(self._path, node)
        for alias in node.names:
            bound = alias.asname or alias.name
            if module == "tests.support.waiting" and alias.name in _WAIT_CALLABLES:
                yield bound, _WAIT_CALLABLES[alias.name]
            elif module == "tests.support" and alias.name == "waiting":
                yield bound, _WAIT_MODULE
            else:
                yield bound, _OTHER_BINDING

    def _scope_bindings(self, scope: ast.AST, nodes: Sequence[ast.AST]) -> dict[str, set[str]]:
        bindings: dict[str, set[str]] = {}

        def record(names: set[str], origin: str = _OTHER_BINDING) -> None:
            for name in names:
                bindings.setdefault(name, set()).add(origin)

        record(self._arguments(scope))
        for node in nodes:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                record({node.name})
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                for name, origin in self._import_bindings(node):
                    record({name}, origin)
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    record(_bound_names(target))
            elif isinstance(node, (ast.AnnAssign, ast.AugAssign, ast.NamedExpr, ast.For, ast.AsyncFor)):
                record(_bound_names(node.target))
            elif isinstance(node, ast.withitem) and node.optional_vars is not None:
                record(_bound_names(node.optional_vars))
            elif isinstance(node, ast.ExceptHandler) and node.name is not None:
                record({node.name})
            elif isinstance(node, ast.Match):
                for case in node.cases:
                    record(_match_bound_names(case.pattern))
            elif isinstance(node, ast.Delete):
                for target in node.targets:
                    record(_bound_names(target))
        return bindings

    def _relocate_declared_bindings(self) -> None:
        """Apply ``global``/``nonlocal`` declarations to collected bindings."""
        module_bindings = self._bindings[id(self.tree)]
        for scope in self.scopes[1:]:
            bindings = self._bindings[id(scope)]
            for name in self._globals[id(scope)]:
                origins = bindings.pop(name, set())
                if origins:
                    module_bindings.setdefault(name, set()).update(origins)
            for name in self._nonlocals[id(scope)]:
                origins = bindings.pop(name, set())
                if not origins:
                    continue
                parent = self._parents[id(scope)]
                while parent is not None and parent is not self.tree:
                    if name in self._bindings[id(parent)] and name not in self._globals[id(parent)]:
                        self._bindings[id(parent)][name].update(origins)
                        break
                    parent = self._parents[id(parent)]

    def _binding_scope(self, scope: ast.AST, name: str) -> ast.AST | None:
        current: ast.AST | None = self.tree if name in self._globals[id(scope)] else scope
        if name in self._nonlocals[id(scope)]:
            current = self._parents[id(scope)]
        while current is not None:
            if name in self._bindings[id(current)]:
                return current
            current = self._parents[id(current)]
        return None

    def _resolved_origins(self, scope: ast.AST, name: str) -> set[str]:
        binding_scope = self._binding_scope(scope, name)
        return set() if binding_scope is None else self._bindings[id(binding_scope)][name]

    def shared_wait_helper(self, scope: ast.AST, callee: ast.expr) -> str | None:
        """The ``tests.support.waiting`` helper *callee* resolves to, if any."""
        parts = _qualified_name(callee).split(".")
        if len(parts) == 1:
            origins = self._resolved_origins(scope, parts[0])
            return next((helper for helper, origin in _WAIT_CALLABLES.items() if origins == {origin}), None)
        helper = parts[-1]
        if helper not in _WAIT_CALLABLES:
            return None
        if len(parts) == 2:
            expected = _WAIT_MODULE
        elif len(parts) == 4 and parts[1:3] == ["support", "waiting"]:
            expected = _WAIT_PACKAGE
        else:
            return None
        return helper if self._resolved_origins(scope, parts[0]) == {expected} else None

    def resolves_shared_wait_until(self, scope: ast.AST, callee: ast.expr) -> bool:
        return self.shared_wait_helper(scope, callee) == "wait_until"

    def local_functions(self, scope: ast.AST, name: str) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
        """The ``def``s that bind *name* in the scope a read of it from *scope* resolves to."""
        binding_scope = self._binding_scope(scope, name)
        if binding_scope is None:
            return []
        return [
            node
            for node in _wait_scope_nodes(binding_scope)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name
        ]


def _assert_no_ignored_wait_until_results(sources: Mapping[Path, str]) -> None:
    """A positive wait must surface timeout instead of discarding ``False``."""
    violations: list[str] = []
    for path, source in sources.items():
        tree = _tree(path, source)
        scopes = _WaitUntilScopes(tree, path)
        for scope in scopes.scopes:
            for node in _wait_scope_nodes(scope):
                if not isinstance(node, ast.Expr) or not isinstance(node.value, ast.Await):
                    continue
                call = node.value.value
                if not isinstance(call, ast.Call) or not scopes.resolves_shared_wait_until(scope, call.func):
                    continue
                violations.append(
                    f"{path}:{node.lineno}: ignored wait_until result hides timeout; "
                    "use wait_for(...) or assert/branch on the bool result"
                )
    assert violations == [], "\n".join(violations)


def _is_top_screen_read(node: ast.expr) -> bool:
    """``<E>.screen`` or ``<E>.screen_stack[...]``, which push_screen updates before the screen composes."""
    if isinstance(node, ast.Attribute):
        return node.attr == "screen"
    return (
        isinstance(node, ast.Subscript) and isinstance(node.value, ast.Attribute) and node.value.attr == "screen_stack"
    )


def _screen_aliases(nodes: Sequence[ast.AST]) -> set[str]:
    """Predicate-local names bound to a screen read: ``screen = app.screen`` or ``(screen := app.screen)``."""
    aliases: set[str] = set()
    for node in nodes:
        if isinstance(node, ast.Assign) and _is_top_screen_read(node.value):
            aliases.update(target.id for target in node.targets if isinstance(target, ast.Name))
        elif isinstance(node, ast.NamedExpr) and _is_top_screen_read(node.value):
            aliases.add(node.target.id)
    return aliases


def _checked_screen(node: ast.expr, aliases: set[str]) -> ast.expr | None:
    """The expression whose ``.is_mounted`` read vouches for the screen *node* reads, if it reads one."""
    if isinstance(node, ast.NamedExpr):
        return ast.Name(id=node.target.id, ctx=ast.Load()) if _is_top_screen_read(node.value) else None
    if _is_top_screen_read(node) or (isinstance(node, ast.Name) and node.id in aliases):
        return node
    return None


def _unmounted_screen_type_checks(predicate: ast.AST) -> Iterator[tuple[ast.Call, ast.expr]]:
    """``isinstance(<screen>, ...)`` checks that read no ``<screen>.is_mounted`` beside them.

    A negated check is a close-wait only in expression position (a lambda body
    or a returned value); in an ``if`` test it gates the wait ("not there yet,
    keep waiting") and needs the mount like a positive check.
    """
    nodes = list(ast.walk(predicate))
    aliases = _screen_aliases(nodes)
    gates = {id(inner) for node in nodes if isinstance(node, ast.If) for inner in ast.walk(node.test)}
    closes = {
        id(node.operand)
        for node in nodes
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not) and id(node) not in gates
    }
    mounted = {ast.dump(node.value) for node in nodes if isinstance(node, ast.Attribute) and node.attr == "is_mounted"}
    for node in nodes:
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "isinstance"):
            continue
        screen = _checked_screen(node.args[0], aliases) if node.args else None
        if screen is not None and id(node) not in closes and ast.dump(screen) not in mounted:
            yield node, screen


def _assert_screen_waits_require_a_mounted_screen(sources: Mapping[Path, str]) -> None:
    """A wait for a pushed screen's type must also wait for that screen to mount.

    push_screen makes the screen current before it composes, so a predicate
    met by the type alone can return before the screen's children exist and
    the next query raises NoMatches. A local name assigned from the screen
    counts as that screen. Close-waits (``not isinstance(...)`` outside an
    ``if`` test) need no mount, and neither does a bounded absence check
    (``assert not await wait_until(...)``), for which the type alone is the
    stricter test.
    """
    violations: list[str] = []
    for path, source in sources.items():
        tree = _tree(path, source)
        scopes = _WaitUntilScopes(tree, path)
        for scope in scopes.scopes:
            nodes = list(_wait_scope_nodes(scope))
            absences = {
                id(node.operand.value)
                for node in nodes
                if isinstance(node, ast.UnaryOp)
                and isinstance(node.op, ast.Not)
                and isinstance(node.operand, ast.Await)
            }
            for node in nodes:
                if (
                    not isinstance(node, ast.Call)
                    or id(node) in absences
                    or scopes.shared_wait_helper(scope, node.func) is None
                ):
                    continue
                predicate = (
                    node.args[0]
                    if node.args
                    else next((keyword.value for keyword in node.keywords if keyword.arg == "predicate"), None)
                )
                if predicate is None:
                    continue
                bodies: list[ast.AST] = (
                    list(scopes.local_functions(scope, predicate.id))
                    if isinstance(predicate, ast.Name)
                    else [predicate]
                )
                for body in bodies:
                    for check, checked in _unmounted_screen_type_checks(body):
                        screen = ast.unparse(checked)
                        violation = (
                            f"{path}:{check.lineno}: a wait met by {screen}'s type can return before that screen "
                            f"composes; require {screen}.is_mounted in the same predicate"
                        )
                        if violation not in violations:
                            violations.append(violation)
    assert violations == [], "\n".join(violations)


def _assert_integration_marker_directory_disjoint(sources: Mapping[Path, str]) -> None:
    violations: list[str] = []
    for path, source in sources.items():
        if not path.is_relative_to(Path("tests/integration")):
            continue
        for node in ast.walk(_tree(path, source)):
            if isinstance(node, ast.Attribute) and _qualified_name(node) == "pytest.mark.integration":
                violations.append(
                    f"{path}:{node.lineno}: tests/integration contains offline cross-layer tests; "
                    "do not mark them integration"
                )
    assert violations == [], "\n".join(violations)


def _agent_engine_constructor_refs(tree: ast.Module) -> set[str]:
    """Return the names by which top-level imports expose assemble_agent_engine."""
    refs = {"assemble_agent_engine"}
    for node in tree.body:
        if isinstance(node, ast.Import):
            for imported in node.names:
                module_ref = imported.asname or imported.name
                refs.add(f"{module_ref}.assemble_agent_engine")
        elif isinstance(node, ast.ImportFrom):
            refs.update(
                imported.asname or imported.name for imported in node.names if imported.name == "assemble_agent_engine"
            )
    return refs


def _assigned_engine_names(
    function: ast.FunctionDef | ast.AsyncFunctionDef,
    constructor_refs: set[str],
) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(function):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        value = node.value
        if not isinstance(value, ast.Call) or _qualified_name(value.func) not in constructor_refs:
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        names.update(target.id for target in targets if isinstance(target, ast.Name))
    return names


def _direct_engine_start_lines(tree: ast.Module) -> list[int]:
    """Return the lines where a locally constructed AgentEngine is started.

    Shared with the allowlist pin so an exemption stays tied to the exact
    construct it exempts: a file that merely builds an engine no longer earns
    one, and the pin cannot certify an entry the guard would never have flagged.
    """
    lines: list[int] = []
    constructor_refs = _agent_engine_constructor_refs(tree)
    for function in (node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))):
        engine_names = _assigned_engine_names(function, constructor_refs)
        lines.extend(
            node.lineno
            for node in ast.walk(function)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "start"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id in engine_names
        )
    return sorted(lines)


def _assert_no_direct_agent_engine_start(sources: Mapping[Path, str]) -> None:
    violations = [
        f"{path}:{line}: construct started AgentEngine instances with the agent_engine fixture"
        for path, source in sources.items()
        if path not in _ENGINE_START_PATH_ALLOWLIST
        for line in _direct_engine_start_lines(_tree(path, source))
    ]
    assert violations == [], "\n".join(violations)


def _assert_quarantine_marker_metadata(sources: Mapping[Path, str], *, today: date | None = None) -> None:
    """Validate quarantine metadata statically — including expiry.

    Expiry must be enforced here, not only at test setup: CI's marker
    expression deselects integration/gc_calibration tests and the collect-only
    prewarm never runs setup, so a quarantine on those tests would otherwise
    outlive its expiry without ever failing CI. The bare (uncalled)
    ``@pytest.mark.quarantine`` spelling is rejected for the same reason — it
    is valid pytest but carries no metadata, and on deselected tests the
    setup-time check would never see it.
    """
    current_date = today or datetime.now(UTC).date()
    violations: list[str] = []
    for path, source in sources.items():
        tree = _tree(path, source)
        call_funcs = {id(node.func) for node in ast.walk(tree) if isinstance(node, ast.Call)}
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Attribute)
                and id(node) not in call_funcs
                and _qualified_name(node) == "pytest.mark.quarantine"
            ):
                violations.append(
                    f"{path}:{node.lineno}: bare quarantine marker carries no metadata; "
                    "call it with reason= and expires='YYYY-MM-DD'"
                )
                continue
            if not isinstance(node, ast.Call) or _qualified_name(node.func) != "pytest.mark.quarantine":
                continue
            keywords = {keyword.arg: keyword.value for keyword in node.keywords if keyword.arg is not None}
            reason = keywords.get("reason")
            expires = keywords.get("expires")
            if not isinstance(reason, ast.Constant) or not isinstance(reason.value, str) or not reason.value.strip():
                violations.append(f"{path}:{node.lineno}: quarantine requires a non-empty reason=")
            if not isinstance(expires, ast.Constant) or not isinstance(expires.value, str):
                violations.append(f"{path}:{node.lineno}: quarantine requires expires='YYYY-MM-DD'")
                continue
            try:
                parsed = date.fromisoformat(expires.value)
            except ValueError:
                violations.append(f"{path}:{node.lineno}: invalid quarantine expiry {expires.value!r}; use YYYY-MM-DD")
                continue
            if parsed.isoformat() != expires.value:
                violations.append(f"{path}:{node.lineno}: invalid quarantine expiry {expires.value!r}; use YYYY-MM-DD")
            elif parsed < current_date:
                violations.append(
                    f"{path}:{node.lineno}: quarantine expired on {expires.value}; "
                    "remove it, extend it with justification, or fix the test"
                )
    assert violations == [], "\n".join(violations)


def _raw_tool_loop_layer_aliases(tree: ast.Module) -> set[str]:
    """Local names bound to the raw ToolLoopLayer by imports, aliases included."""
    names = {"ToolLoopLayer"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == "ToolLoopLayer" and alias.asname:
                    names.add(alias.asname)
    return names


def _raw_tool_loop_layer_uses(tree: ast.Module) -> list[tuple[int, str | None, str]]:
    """(line, scope, kind) per raw-layer use.

    Constructions are attributed to their nearest enclosing function and
    subclass bases to the class being defined, so an allowlist entry vouches
    only for the exact scope it names.
    """
    aliases = _raw_tool_loop_layer_aliases(tree)

    def _is_raw_layer_ref(node: ast.expr) -> bool:
        name = _qualified_name(node)
        return name in aliases or name.endswith(".ToolLoopLayer")

    found: list[tuple[int, str | None, str]] = []

    def _walk(node: ast.AST, enclosing: str | None) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.Call) and _is_raw_layer_ref(child.func):
                found.append((child.lineno, enclosing, "construction"))
            if isinstance(child, ast.ClassDef) and any(_is_raw_layer_ref(base) for base in child.bases):
                found.append((child.lineno, child.name, "subclass"))
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                _walk(child, child.name)
            else:
                _walk(child, enclosing)

    _walk(tree, None)
    return found


def _assert_tool_loop_layer_uses_are_invariant_checked(sources: Mapping[Path, str]) -> None:
    """Loop transcripts are only guarded if the invariant oracle sees them.

    A bare ``ToolLoopLayer`` in a test — constructed directly (under any
    import alias) or used as a base class — silently opts final responses out
    of the transcript-invariant oracle
    (``tests.support.transcript_invariants``); tests must build or derive
    from ``InvariantCheckedToolLoopLayer`` instead so the oracle's coverage
    claim stays mechanically true. Only sites that never produce a
    loop-landed final response (constructor pins, the degenerate-stream
    fallback) plus the checked layer's own definition may use the raw layer,
    each via an explicit allowlist entry naming its scope.
    """
    violations: list[str] = []
    for path, source in sources.items():
        for lineno, scope, kind in _raw_tool_loop_layer_uses(_tree(path, source)):
            if (path, scope) in _RAW_TOOL_LOOP_LAYER_ALLOWLIST:
                continue
            action = "build" if kind == "construction" else "subclass"
            violations.append(
                f"{path}:{lineno}: {action} InvariantCheckedToolLoopLayer "
                "(tests.support.transcript_invariants) so the transcript-invariant oracle checks "
                "final responses; a raw ToolLoopLayer needs an allowlist entry"
            )
    assert violations == [], "\n".join(violations)


def _assert_trajectory_prefix_checks_use_physical_slots(sources: Mapping[Path, str]) -> None:
    """Route trajectory-file assertions through the slots-only shared oracle."""
    violations = [
        f"{path}:{node.lineno}: use tests.support.trajectory_invariants.assert_trajectory_accounted "
        "so the prefix check sees physical slots"
        for path, source in sources.items()
        if path not in _DIRECT_TRAJECTORY_PREFIX_ORACLE_ALLOWLIST
        for node in ast.walk(_tree(path, source))
        if isinstance(node, ast.Call) and _qualified_name(node.func).endswith("verify_accounted_prefix")
    ]
    assert violations == [], "\n".join(violations)


@_pins_allowlist("_LOCAL_POLLING_ALLOWLIST")
def test_local_polling_allowlist_entries_are_live() -> None:
    trees = _allowlist_target_trees(frozenset(path for path, _name in _LOCAL_POLLING_ALLOWLIST))
    problems: list[str] = []
    for path, helper_name in sorted(_LOCAL_POLLING_ALLOWLIST):
        tree = trees.get(path)
        if tree is None:
            problems.append(
                _meta_guard_problem(
                    "_LOCAL_POLLING_ALLOWLIST",
                    f"entry ({path!s}, {helper_name}) names a missing file",
                    "remove the stale entry or point it at the file that defines the approved polling helper",
                )
            )
            continue
        matches = [
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == helper_name
        ]
        if helper_name not in _POLLING_HELPER_NAMES:
            problems.append(
                _meta_guard_problem(
                    "_LOCAL_POLLING_ALLOWLIST",
                    f"entry ({path!s}, {helper_name}) cannot be consumed because the name is not a guarded polling helper",
                    "remove the entry or add the actual guarded helper name from _POLLING_HELPER_NAMES",
                )
            )
        elif not matches:
            problems.append(
                _meta_guard_problem(
                    "_LOCAL_POLLING_ALLOWLIST",
                    f"entry ({path!s}, {helper_name}) names no function in {path}",
                    "remove the stale entry or update it to the helper's current function name",
                )
            )
        elif len(matches) > 1:
            lines = ", ".join(str(node.lineno) for node in matches)
            problems.append(
                _meta_guard_problem(
                    "_LOCAL_POLLING_ALLOWLIST",
                    f"entry ({path!s}, {helper_name}) is ambiguous at {path}:{lines}",
                    "use a unique helper name or narrow the allowlist key before retaining the exemption",
                )
            )

    assert problems == [], "\n".join(problems)


@_pins_allowlist("_ENGINE_START_PATH_ALLOWLIST")
def test_engine_start_path_allowlist_entries_are_live() -> None:
    trees = _allowlist_target_trees(frozenset(_ENGINE_START_PATH_ALLOWLIST))
    problems: list[str] = []
    for path in sorted(_ENGINE_START_PATH_ALLOWLIST):
        tree = trees.get(path)
        if tree is None:
            problems.append(
                _meta_guard_problem(
                    "_ENGINE_START_PATH_ALLOWLIST",
                    f"entry {path} names a missing file",
                    "remove the stale entry or update it to the live AgentEngine helper module",
                )
            )
            continue
        if not _direct_engine_start_lines(tree):
            problems.append(
                _meta_guard_problem(
                    "_ENGINE_START_PATH_ALLOWLIST",
                    f"entry {path} starts no locally constructed AgentEngine, so the guard would not flag it "
                    "even without the exemption",
                    "remove the stale entry or move it to the helper module that starts the engine directly",
                )
            )

    assert problems == [], "\n".join(problems)


@_pins_allowlist("_RAW_TOOL_LOOP_LAYER_ALLOWLIST")
def test_raw_tool_loop_layer_allowlist_entries_are_live() -> None:
    trees = _allowlist_target_trees(frozenset(path for path, _scope in _RAW_TOOL_LOOP_LAYER_ALLOWLIST))
    problems: list[str] = []
    for path, scope in sorted(_RAW_TOOL_LOOP_LAYER_ALLOWLIST):
        tree = trees.get(path)
        if tree is None:
            problems.append(
                _meta_guard_problem(
                    "_RAW_TOOL_LOOP_LAYER_ALLOWLIST",
                    f"entry ({path!s}, {scope}) names a missing file",
                    "remove the stale entry or point it at the live raw ToolLoopLayer scope",
                )
            )
            continue
        definitions = [
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name == scope
        ]
        matches = [(line, kind) for line, found_scope, kind in _raw_tool_loop_layer_uses(tree) if found_scope == scope]
        if not definitions:
            problems.append(
                _meta_guard_problem(
                    "_RAW_TOOL_LOOP_LAYER_ALLOWLIST",
                    f"entry ({path!s}, {scope}) names no test function or class in {path}",
                    "remove the stale entry or update it to the exact live test function or class name",
                )
            )
        elif len(definitions) > 1:
            sites = ", ".join(f"{path}:{node.lineno}" for node in definitions)
            problems.append(
                _meta_guard_problem(
                    "_RAW_TOOL_LOOP_LAYER_ALLOWLIST",
                    f"entry ({path!s}, {scope}) ambiguously resolves to multiple scopes: {sites}",
                    "rename the scopes or narrow the allowlist key so the entry resolves to exactly one symbol",
                )
            )
        elif not matches:
            problems.append(
                _meta_guard_problem(
                    "_RAW_TOOL_LOOP_LAYER_ALLOWLIST",
                    f"entry ({path!s}, {scope}) resolves to a scope with no raw ToolLoopLayer construction or subclass",
                    "remove the stale entry or update it to the live scope that directly uses ToolLoopLayer",
                )
            )

    assert problems == [], "\n".join(problems)


@_pins_allowlist("_DIRECT_TRAJECTORY_PREFIX_ORACLE_ALLOWLIST")
def test_direct_trajectory_prefix_oracle_allowlist_entries_are_live() -> None:
    trees = _allowlist_target_trees(frozenset(_DIRECT_TRAJECTORY_PREFIX_ORACLE_ALLOWLIST))
    problems: list[str] = []
    for path in sorted(_DIRECT_TRAJECTORY_PREFIX_ORACLE_ALLOWLIST):
        tree = trees.get(path)
        if tree is None:
            problems.append(
                _meta_guard_problem(
                    "_DIRECT_TRAJECTORY_PREFIX_ORACLE_ALLOWLIST",
                    f"entry {path} names a missing file",
                    "remove the stale entry or update it to the live direct prefix-oracle module",
                )
            )
            continue
        calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and _qualified_name(node.func).endswith("verify_accounted_prefix")
        ]
        if not calls:
            problems.append(
                _meta_guard_problem(
                    "_DIRECT_TRAJECTORY_PREFIX_ORACLE_ALLOWLIST",
                    f"entry {path} contains no verify_accounted_prefix call and no longer consumes the exemption",
                    "remove the stale entry or update it to the module that directly calls verify_accounted_prefix",
                )
            )

    assert problems == [], "\n".join(problems)


def test_scroll_relative_guard_rejects_counterexample() -> None:
    with pytest.raises(AssertionError, match="scroll_relative"):
        _assert_no_scroll_relative({Path("tests/test_bad.py"): "panel.scroll_relative(y=1)\n"})


def test_local_polling_guard_rejects_counterexample() -> None:
    with pytest.raises(AssertionError, match=r"tests/support/waiting\.py"):
        _assert_no_unapproved_local_polling_helpers(
            {Path("tests/test_bad.py"): "async def _eventually(predicate):\n    return predicate()\n"}
        )


@pytest.mark.parametrize(
    "source",
    [
        "from tests.support.waiting import wait_until\nasync def test_bad():\n    await wait_until(lambda: True)\n",
        "import tests.support.waiting as waits\nasync def test_bad():\n    await waits.wait_until(lambda: True)\n",
        (
            "import tests.support.waiting\n"
            "async def test_bad():\n"
            "    await tests.support.waiting.wait_until(lambda: True)\n"
        ),
        (
            "import tests.support.waiting\n"
            "import tests.support.engines\n"
            "async def test_bad():\n"
            "    await tests.support.waiting.wait_until(lambda: True)\n"
        ),
        "from tests.support import waiting\nasync def test_bad():\n    await waiting.wait_until(lambda: True)\n",
        (
            "async def test_bad():\n"
            "    from tests.support.waiting import wait_until\n"
            "    await wait_until(lambda: True)\n"
        ),
        (
            "from tests.support.waiting import wait_until\n"
            "async def outer():\n"
            "    async def test_bad():\n"
            "        await wait_until(lambda: True)\n"
        ),
    ],
)
def test_ignored_wait_until_guard_rejects_counterexamples(source: str) -> None:
    with pytest.raises(AssertionError, match="ignored wait_until result"):
        _assert_no_ignored_wait_until_results({Path("tests/test_bad.py"): source})


@pytest.mark.parametrize(
    ("path", "relative_import"),
    [
        (Path("tests/kernel/test_bad.py"), "from ..support.waiting import wait_until"),
        (Path("tests/service/context/test_bad.py"), "from ...support.waiting import wait_until"),
    ],
)
def test_ignored_wait_until_guard_resolves_relative_imports(path: Path, relative_import: str) -> None:
    source = f"{relative_import}\nasync def test_bad():\n    await wait_until(lambda: True)\n"

    with pytest.raises(AssertionError, match="ignored wait_until result"):
        _assert_no_ignored_wait_until_results({path: source})


def test_ignored_wait_until_guard_allows_consumed_results() -> None:
    source = (
        "from tests.support.waiting import wait_until\n"
        "async def test_good():\n"
        "    assert await wait_until(lambda: True)\n"
        "    observed = await wait_until(lambda: True)\n"
        "    return observed\n"
    )
    _assert_no_ignored_wait_until_results({Path("tests/test_good.py"): source})


def test_ignored_wait_until_guard_allows_consumed_relative_import_result() -> None:
    source = (
        "from ..support.waiting import wait_until\nasync def test_good():\n    assert await wait_until(lambda: True)\n"
    )

    _assert_no_ignored_wait_until_results({Path("tests/kernel/test_good.py"): source})


@pytest.mark.parametrize(
    "source",
    [
        ("from tests.support.waiting import wait_until\nasync def helper(wait_until):\n    await wait_until()\n"),
        (
            "from tests.support.waiting import wait_until\n"
            "async def helper():\n"
            "    wait_until = callback\n"
            "    await wait_until()\n"
        ),
        (
            "from tests.support.waiting import wait_until\n"
            "async def helper():\n"
            "    async def wait_until():\n"
            "        pass\n"
            "    await wait_until()\n"
        ),
        (
            "from tests.support.waiting import wait_until\n"
            "async def helper():\n"
            "    await wait_until()\n"
            "    from other import wait_until\n"
        ),
        (
            "from tests.support.waiting import wait_until\n"
            "from other import wait_until\n"
            "async def helper():\n"
            "    await wait_until()\n"
        ),
        ("from tests.support import waiting\nasync def helper(waiting):\n    await waiting.wait_until()\n"),
        (
            "import tests.support.waiting\n"
            "import other as tests\n"
            "async def helper():\n"
            "    await tests.support.waiting.wait_until()\n"
        ),
    ],
)
def test_ignored_wait_until_guard_honors_lexical_shadowing(source: str) -> None:
    _assert_no_ignored_wait_until_results({Path("tests/test_good.py"): source})


@pytest.mark.parametrize(
    "body",
    [
        "    await wait_for(lambda: isinstance(app.screen, Dialog))\n",
        "    def opened():\n        return isinstance(app.screen, Dialog)\n    await wait_for(opened)\n",
        "    assert await wait_until(lambda: isinstance(app.screen, Dialog) or task.done())\n",
        "    await wait_for(lambda: isinstance(app.screen_stack[-1], Dialog))\n",
        # Mounted must be read on the screen the isinstance reads.
        "    await wait_for(lambda: isinstance(pilot.app.screen, Dialog) and app.screen.is_mounted)\n",
        # A local name for the screen is that screen.
        "    def opened():\n        screen = app.screen\n        return isinstance(screen, Dialog)\n    await wait_for(opened)\n",
        # A negated check in an ``if`` test gates the wait: it is no close-wait.
        (
            "    def opened():\n"
            "        screen = app.screen\n"
            "        if not isinstance(screen, Dialog):\n"
            "            return False\n"
            "        return bool(screen.query('#ok'))\n"
            "    await wait_for(opened)\n"
        ),
    ],
)
def test_screen_wait_guard_rejects_a_type_only_wait(body: str) -> None:
    source = f"from tests.support.waiting import wait_for, wait_until\nasync def test_bad(app, pilot, task):\n{body}"

    with pytest.raises(AssertionError, match=r"tests/test_bad\.py:\d+: a wait met by .*is_mounted"):
        _assert_screen_waits_require_a_mounted_screen({Path("tests/test_bad.py"): source})


@pytest.mark.parametrize(
    ("imports", "callee"),
    [
        ("from ..support.waiting import wait_for", "wait_for"),
        ("from ..support import waiting", "waiting.wait_for"),
        ("import tests.support.waiting", "tests.support.waiting.wait_for"),
    ],
)
def test_screen_wait_guard_resolves_every_import_form(imports: str, callee: str) -> None:
    source = f"{imports}\nasync def test_bad(app):\n    await {callee}(lambda: isinstance(app.screen, Dialog))\n"

    with pytest.raises(AssertionError, match=r"app\.screen\.is_mounted"):
        _assert_screen_waits_require_a_mounted_screen({Path("tests/app/test_bad.py"): source})


def test_screen_wait_guard_allows_mounted_close_and_absence_waits() -> None:
    source = (
        "from tests.support.waiting import wait_for, wait_until\n"
        "async def test_good(app, task):\n"
        "    await wait_for(lambda: isinstance(app.screen, Dialog) and app.screen.is_mounted)\n"
        "    await wait_for(lambda: (isinstance(app.screen, Dialog) and app.screen.is_mounted) or task.done())\n"
        "    def opened():\n"
        "        return isinstance(app.screen_stack[-1], Dialog) and app.screen_stack[-1].is_mounted\n"
        "    await wait_for(opened)\n"
        "    await wait_for(lambda: not isinstance(app.screen, Dialog))\n"
        "    def closed():\n"
        "        return not isinstance(app.screen, Dialog)\n"
        "    await wait_for(closed)\n"
        "    def gated():\n"
        "        screen = app.screen\n"
        "        if not isinstance(screen, Dialog) or not screen.is_mounted:\n"
        "            return False\n"
        "        return bool(screen.query('#ok'))\n"
        "    await wait_for(gated)\n"
        "    assert not await wait_until(lambda: isinstance(app.screen, Dialog), timeout=0.3)\n"
        "    await other.wait_for(lambda: isinstance(app.screen, Dialog))\n"
    )

    _assert_screen_waits_require_a_mounted_screen({Path("tests/test_good.py"): source})


def test_tool_loop_layer_guard_rejects_counterexample() -> None:
    source = "def test_bad():\n    layer = ToolLoopLayer(ChatMiddlewareLayer(wire))\n"
    with pytest.raises(AssertionError, match="InvariantCheckedToolLoopLayer"):
        _assert_tool_loop_layer_uses_are_invariant_checked({Path("tests/test_bad.py"): source})


def test_tool_loop_layer_guard_attributes_calls_to_nearest_enclosing_function() -> None:
    # A construction inside a nested helper must be attributed to the helper,
    # not the (possibly allowlisted) outer test — otherwise an allowlist entry
    # would silently cover raw constructions it never vouched for.
    source = "def test_ctor_defaults_mirror_framework_values():\n    def _build():\n        return ToolLoopLayer(inner)\n    return _build()\n"
    with pytest.raises(AssertionError, match="InvariantCheckedToolLoopLayer"):
        _assert_tool_loop_layer_uses_are_invariant_checked({Path("tests/kernel/test_loop.py"): source})


def test_tool_loop_layer_guard_rejects_import_alias_counterexample() -> None:
    # Renaming the import must not launder a raw construction.
    source = "from chrys.kernel.loop import ToolLoopLayer as RawLoop\n\ndef test_bad():\n    layer = RawLoop(inner)\n"
    with pytest.raises(AssertionError, match="InvariantCheckedToolLoopLayer"):
        _assert_tool_loop_layer_uses_are_invariant_checked({Path("tests/test_bad.py"): source})


def test_tool_loop_layer_guard_rejects_subclass_counterexample() -> None:
    # Deriving from the raw layer runs the real loop with no oracle on its
    # final responses — the subclass form must be flagged like a construction.
    source = "class _Client(ToolLoopLayer):\n    pass\n"
    with pytest.raises(AssertionError, match="subclass InvariantCheckedToolLoopLayer"):
        _assert_tool_loop_layer_uses_are_invariant_checked({Path("tests/test_bad.py"): source})


def test_trajectory_prefix_guard_rejects_a_decoded_events_check() -> None:
    source = "def test_bad(result):\n    assert verify_accounted_prefix(result.events) == []\n"
    with pytest.raises(AssertionError, match="physical slots"):
        _assert_trajectory_prefix_checks_use_physical_slots({Path("tests/test_bad.py"): source})


def test_integration_marker_directory_guard_rejects_counterexample() -> None:
    source = "import pytest\n\n@pytest.mark.integration\ndef test_offline_cross_layer():\n    pass\n"
    with pytest.raises(AssertionError, match="offline cross-layer"):
        _assert_integration_marker_directory_disjoint({Path("tests/integration/test_bad.py"): source})


@pytest.mark.parametrize(
    ("imports", "constructor"),
    [
        ("", "assemble_agent_engine"),
        ("import chrys.orchestration.engine.assembly as engine_module", "engine_module.assemble_agent_engine"),
        ("from chrys.orchestration.engine.assembly import assemble_agent_engine as AE", "AE"),
    ],
)
def test_direct_agent_engine_start_guard_rejects_counterexample(imports: str, constructor: str) -> None:
    source = f"{imports}\n\nasync def test_bad():\n    engine = {constructor}(bus)\n    await engine.start(profile)\n"
    with pytest.raises(AssertionError, match="agent_engine fixture"):
        _assert_no_direct_agent_engine_start({Path("tests/test_bad.py"): source})


@pytest.mark.parametrize(
    "marker",
    [
        '@pytest.mark.quarantine(expires="2099-01-01")',
        '@pytest.mark.quarantine(reason="known flake", expires="2099-1-1")',
        "@pytest.mark.quarantine",
    ],
)
def test_quarantine_metadata_guard_rejects_counterexamples(marker: str) -> None:
    source = f"import pytest\n\n{marker}\ndef test_bad():\n    pass\n"
    with pytest.raises(AssertionError, match="quarantine"):
        _assert_quarantine_marker_metadata({Path("tests/test_bad.py"): source})


def test_quarantine_expiry_guard_rejects_expired_counterexample() -> None:
    marker = '@pytest.mark.quarantine(reason="known flake", expires="2026-07-15")'
    source = f"import pytest\n\n{marker}\ndef test_bad():\n    pass\n"
    with pytest.raises(AssertionError, match="quarantine expired on 2026-07-15"):
        _assert_quarantine_marker_metadata({Path("tests/test_bad.py"): source}, today=date(2026, 7, 16))
