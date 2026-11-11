# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Fail-closed event declarations and independent runtime identity checks."""

from __future__ import annotations

import ast
import hashlib
import sys
from copy import deepcopy
from dataclasses import dataclass, is_dataclass
from dataclasses import fields as dataclass_fields
from functools import cache
from types import ModuleType

import pytest

from tests.support.ci import CI_LINUX_ONLY
from tests.support.paths import SRC_ROOT

pytestmark = CI_LINUX_ONLY
SRC = SRC_ROOT / "chrys"

# These are caller/request correlation protocols, not agent execution facts.
_INVOCATION_COMMAND_FIELDS = {
    "InvocationRetryRequested": {"invocation_id"},
    "InvocationAbortRequested": {"invocation_id"},
}
_NON_INVOCATION_CORRELATION_FIELDS = {
    "ApprovalRequest": {"call_id", "tool_name"},
    # A hook's question names the tool it asks about, as an approval does.
    "HookConfirmationRequest": {"tool_name"},
    "QuestionToUser": {"call_id"},
    "SleepSkip": {"call_id"},
    # A workflow activation binds its agent invocation ahead of the Invocation*
    # family; the node event is the binding announcement, not an execution fact.
    "WorkflowNodeStateChanged": {"invocation_id"},
    **_INVOCATION_COMMAND_FIELDS,
}
# Existing exchange, content-segment and compaction identifiers are payloads,
# as is the retry wait a retry notice names for ``RetryWaitSkip`` to end.
_INVOCATION_PAYLOAD_IDS = {
    "call_id",
    "parent_call_id",
    "provider_call_id",
    "segment_id",
    "segment_ids",
    "compaction_id",
    "wait_id",
}


# Supported grammar (the finite declaration contract for foundation/events):
# Module: string docstrings; Import/ImportFrom, with future annotations first;
# at most one import-only if TYPE_CHECKING without else; top-level classes;
# Assign/valued AnnAssign to non-dunder Names, with recursive Constant or
# Tuple/List/Set literals. No other statements, aliases or type parameters.
# Decorators: dataclass or dataclass(kw_only=bool, frozen=bool), no positional
# arguments or other keywords. Bases: module-class Names or object; no keywords.
# Class: docstrings, pass, annotated non-dunder Names (Name/Attribute/Subscript/
# Tuple/List/BitOr/Constant annotations; absent/literal/field defaults), literal
# Assign to non-dunder Names, or __slots__ with string tuple/list/set values.
# Field: field with no positional arguments; default=literal; repr/compare=bool;
# default_factory=non-class Name or zero-argument lambda composed only of
# Call/Attribute/Subscript/Name/Constant, with constant slices and no class Names;
# Lambda body = recursive structural grammar AND exactly a registered expression:
# uuid4().hex[:12] or datetime.now(tz=UTC).
# A factory Name in the class table must be a non-Event dataclass, with no bases
# except object, no methods, only supported dataclass decorators, and every
# declaration validated by this same grammar. No class-name exceptions.
# Derived Session correlation: WorkflowRunRequest/WorkflowRunAccepted must
# redeclare session_id with exactly the declaration below; init=False remains
# unsupported elsewhere. Their pinned __post_init__ derives it from the target.
# Methods: only the registered InvocationEvent sealing methods and Workflow
# initializers, undecorated, all present, and pinned by SHA-256 of ast.dump.
# Accepted boundaries, not chased by either line: an import alias for a grammar
# keyword (from copy import copy as dataclass) passes the static line, which
# does not pin import origins, and is held by the runtime attribute allowlist;
# a subclass built with type() that forges __module__ without binding into the
# module namespace passes the runtime closure, which filters by ownership, and
# is held by the static literal-assignment rule. Only a declaration an ordinary
# maintainer would write that passes both lines counts as a guard defect.
_EXTERNAL_EVENT_BASES = {"object"}
_SUPPORTED_FACTORY_BODIES = {
    ast.dump(ast.parse(source, mode="eval").body, include_attributes=False)
    for source in ("uuid4().hex[:12]", "datetime.now(tz=UTC)")
}
_SUPPORTED_EVENT_METHODS = {
    ("InvocationEvent", "__post_init__"): "d90e87198fa8b243c2a3ebb227671b69c8804e2c52a000a376b902b9e8582635",
    ("InvocationEvent", "__setattr__"): "2a012d32a2046ae3e54915d2bb62e12cc194f976c62c95707b1bf647adda266c",
    ("InvocationEvent", "__delattr__"): "2642be74a500cb683c268f46eaf62609b49a8540404f00302a12d879112180c8",
    ("WorkflowRunRequest", "__post_init__"): "111fc948c34d218799c5310b5b2c88c0a9215e38b33ed52bb5a99425fe80a142",
    ("WorkflowRunAccepted", "__post_init__"): "42cd1a388ce8e6616d64270459fe186981056c0aa276eefc9d7dd78969831ed9",
}
_DERIVED_SESSION_EVENTS = {"WorkflowRunRequest", "WorkflowRunAccepted"}
_DERIVED_SESSION_DECLARATION = ast.dump(
    ast.parse("session_id: str | None = field(default=None, init=False)").body[0], include_attributes=False
)


def _is_docstring(node: ast.stmt) -> bool:
    return isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)


def _check_literal(node: ast.AST | None) -> None:
    if isinstance(node, ast.Constant):
        return
    if isinstance(node, ast.Tuple | ast.List | ast.Set):
        for item in node.elts:
            _check_literal(item)
        return
    raise AssertionError(f"line {node.lineno if node is not None else '?'}: literal required")


def _check_annotation(node: ast.expr) -> None:
    if isinstance(node, ast.Name | ast.Constant):
        return
    if isinstance(node, ast.Attribute):
        _check_annotation(node.value)
        return
    if isinstance(node, ast.Subscript):
        _check_annotation(node.value)
        _check_annotation(node.slice)
        return
    if isinstance(node, ast.Tuple | ast.List):
        for item in node.elts:
            _check_annotation(item)
        return
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        _check_annotation(node.left)
        _check_annotation(node.right)
        return
    raise AssertionError(f"line {node.lineno}: unsupported annotation {type(node).__name__}")


def _check_factory_expression(node: ast.expr, classes: dict[str, ast.ClassDef]) -> None:
    if isinstance(node, ast.Constant):
        return
    if isinstance(node, ast.Name):
        assert node.id not in classes, f"line {node.lineno}: factory class Name is unsupported"
        return
    if isinstance(node, ast.Attribute):
        _check_factory_expression(node.value, classes)
        return
    if isinstance(node, ast.Call):
        _check_factory_expression(node.func, classes)
        for arg in node.args:
            _check_factory_expression(arg, classes)
        for keyword in node.keywords:
            assert keyword.arg is not None, f"line {node.lineno}: factory keyword unpacking is unsupported"
            _check_factory_expression(keyword.value, classes)
        return
    if isinstance(node, ast.Subscript):
        _check_factory_expression(node.value, classes)
        slices = (
            (node.slice.lower, node.slice.upper, node.slice.step)
            if isinstance(node.slice, ast.Slice)
            else (node.slice,)
        )
        assert all(item is None or isinstance(item, ast.Constant) for item in slices), (
            f"line {node.lineno}: factory slice must be constant"
        )
        return
    raise AssertionError(f"line {node.lineno}: unsupported factory expression {type(node).__name__}")


def _check_bool(node: ast.expr, context: str) -> None:
    assert isinstance(node, ast.Constant) and type(node.value) is bool, f"line {node.lineno}: {context} requires bool"


def _check_field_call(node: ast.Call, classes: dict[str, ast.ClassDef]) -> None:
    assert isinstance(node.func, ast.Name) and node.func.id == "field", f"line {node.lineno}: field call required"
    assert not node.args, f"line {node.lineno}: field positional arguments are unsupported"
    seen: set[str] = set()
    for kw in node.keywords:
        assert kw.arg in {"default", "default_factory", "repr", "compare"} and kw.arg not in seen, (
            f"line {node.lineno}: unsupported field keyword {kw.arg}"
        )
        seen.add(kw.arg)
        if kw.arg == "default":
            _check_literal(kw.value)
        elif kw.arg in {"repr", "compare"}:
            _check_bool(kw.value, "field " + kw.arg)
        elif isinstance(kw.value, ast.Name):
            name = kw.value.id
            if name in classes:
                factory = classes[name]
                assert name not in _event_descendants(classes, "Event") and all(
                    isinstance(base, ast.Name) and base.id == "object" for base in factory.bases
                ), f"line {node.lineno}: local data factory must be non-Event with only object bases"
                assert not any(isinstance(item, ast.FunctionDef | ast.AsyncFunctionDef) for item in factory.body), (
                    f"line {node.lineno}: local data factory must have no methods"
                )
                assert factory.decorator_list, f"line {node.lineno}: local data factory must be a dataclass"
                _check_decorators(factory)
                # _event_classes validates every class body with the same grammar.
        else:
            factory = kw.value
            assert isinstance(factory, ast.Lambda), f"line {node.lineno}: factory must be Name or zero-argument lambda"
            args = factory.args
            assert not (
                args.posonlyargs or args.args or args.vararg or args.kwonlyargs or args.kwarg or args.defaults
            ), f"line {node.lineno}: factory lambda must have no parameters"
            _check_factory_expression(factory.body, classes)
            assert ast.dump(factory.body, include_attributes=False) in _SUPPORTED_FACTORY_BODIES, (
                f"line {node.lineno}: unsupported factory body"
            )


def _check_decorators(node: ast.ClassDef) -> None:
    for decorator in node.decorator_list:
        if isinstance(decorator, ast.Name) and decorator.id == "dataclass":
            continue
        assert (
            isinstance(decorator, ast.Call)
            and isinstance(decorator.func, ast.Name)
            and decorator.func.id == "dataclass"
            and not decorator.args
        ), f"line {decorator.lineno}: unsupported class decorator"
        seen: set[str] = set()
        for kw in decorator.keywords:
            assert kw.arg in {"kw_only", "frozen"} and kw.arg not in seen, (
                f"line {decorator.lineno}: unsupported dataclass keyword {kw.arg}"
            )
            seen.add(kw.arg)
            _check_bool(kw.value, "dataclass " + kw.arg)


def _check_targets(statement: ast.Assign | ast.AnnAssign, *, slots: bool = False) -> set[str]:
    targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
    assert all(isinstance(target, ast.Name) for target in targets), (
        f"line {statement.lineno}: assignment target must be a Name"
    )
    names = {target.id for target in targets}
    assert all(not name.startswith("__") or (slots and name == "__slots__") for name in names), (
        f"line {statement.lineno}: dunder assignment target is unsupported"
    )
    return names


def _top_level_event_classes(source: str) -> dict[str, ast.ClassDef]:
    """Raw declaration inventory; runtime checks must not call the static validator."""
    return {node.name: node for node in ast.parse(source).body if isinstance(node, ast.ClassDef)}


def _event_classes(source: str) -> dict[str, ast.ClassDef]:
    tree = ast.parse(source)
    classes: dict[str, ast.ClassDef] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            assert node in tree.body, f"line {node.lineno}: {node.name}: non-top-level class is unsupported"
            assert node.name not in classes, f"line {node.lineno}: {node.name}: duplicate class definition"
            classes[node.name] = node
    imports = [node for node in tree.body if isinstance(node, ast.Import | ast.ImportFrom)]
    assert (
        imports
        and isinstance(imports[0], ast.ImportFrom)
        and imports[0].module == "__future__"
        and [(alias.name, alias.asname) for alias in imports[0].names] == [("annotations", None)]
    ), "line 1: first import must be from __future__ import annotations"
    type_checking_seen = False
    for statement in tree.body:
        if (
            isinstance(statement, ast.If)
            and isinstance(statement.test, ast.Name)
            and statement.test.id == "TYPE_CHECKING"
        ):
            assert not type_checking_seen, f"line {statement.lineno}: duplicate TYPE_CHECKING branch"
            type_checking_seen = True
            assert all(isinstance(item, ast.Import | ast.ImportFrom) for item in statement.body), (
                f"line {statement.lineno}: TYPE_CHECKING true branch permits imports only"
            )
            assert not statement.orelse, f"line {statement.lineno}: TYPE_CHECKING else branch is unsupported"
        elif isinstance(statement, ast.Assign | ast.AnnAssign):
            _check_targets(statement)
            if isinstance(statement, ast.AnnAssign):
                _check_annotation(statement.annotation)
                assert statement.value is not None, f"line {statement.lineno}: module annotation requires a value"
            _check_literal(statement.value)
        elif not isinstance(statement, ast.ClassDef | ast.Import | ast.ImportFrom) and not _is_docstring(statement):
            raise AssertionError(f"line {statement.lineno}: unsupported module statement {type(statement).__name__}")
    for node in classes.values():
        assert not node.type_params, f"line {node.lineno}: class type parameters are unsupported"
        _check_decorators(node)
        assert not node.keywords, f"line {node.lineno}: class keywords are unsupported"
        for base in node.bases:
            assert isinstance(base, ast.Name), f"line {base.lineno}: {node.name}: base must be a Name"
            assert base.id in classes or base.id in _EXTERNAL_EVENT_BASES, (
                f"line {base.lineno}: {node.name}: unknown external base {base.id}"
            )
        _declared_event_fields(node, classes)
    for name in classes:
        _effective_event_fields(classes, name)
    return classes


def _event_descendants(classes: dict[str, ast.ClassDef], root: str) -> set[str]:
    descendants = {root} if root in classes else set()
    for _ in classes:
        descendants.update(
            name
            for name, node in classes.items()
            if any(isinstance(base, ast.Name) and base.id in descendants for base in node.bases)
        )
    return descendants


def _declared_event_fields(node: ast.ClassDef, classes: dict[str, ast.ClassDef]) -> set[str]:
    declared: set[str] = set()
    methods: set[str] = set()
    derived_session = False
    for statement in node.body:
        if _is_docstring(statement) or isinstance(statement, ast.Pass):
            continue
        if isinstance(statement, ast.FunctionDef | ast.AsyncFunctionDef):
            key = node.name, statement.name
            assert key in _SUPPORTED_EVENT_METHODS, f"line {statement.lineno}: unsupported event method {key}"
            assert not statement.decorator_list, f"line {statement.lineno}: method decorators are unsupported"
            digest = hashlib.sha256(ast.dump(statement, include_attributes=False).encode()).hexdigest()
            assert digest == _SUPPORTED_EVENT_METHODS[key], f"line {statement.lineno}: event method hash mismatch {key}"
            methods.add(statement.name)
            continue
        assert isinstance(statement, ast.Assign | ast.AnnAssign), (
            f"line {statement.lineno}: {node.name}: unsupported class statement {type(statement).__name__}"
        )
        names = _check_targets(statement, slots=isinstance(statement, ast.Assign))
        declared.update(names - {"__slots__"})
        if node.name in _DERIVED_SESSION_EVENTS and "session_id" in names:
            assert ast.dump(statement, include_attributes=False) == _DERIVED_SESSION_DECLARATION, (
                f"{node.name}: derived session_id declaration mismatch"
            )
            derived_session = True
            continue
        if "__slots__" in names:
            value = statement.value
            assert isinstance(value, ast.Tuple | ast.List | ast.Set) and all(
                isinstance(item, ast.Constant) and isinstance(item.value, str) for item in value.elts
            ), f"line {statement.lineno}: {node.name}: __slots__ must be a constant string tuple/list/set"
            declared.update(item.value for item in value.elts)
        elif isinstance(statement, ast.AnnAssign):
            _check_annotation(statement.annotation)
            if isinstance(statement.value, ast.Call):
                _check_field_call(statement.value, classes)
            elif statement.value is not None:
                _check_literal(statement.value)
        else:
            _check_literal(statement.value)
    # The hash pin only constrains methods that exist; the registered set is
    # also a lower bound, so deleting a sealing method is rejected too.
    required = {method for cls, method in _SUPPORTED_EVENT_METHODS if cls == node.name}
    assert methods == required, (
        f"line {node.lineno}: {node.name}: required event methods: missing {sorted(required - methods)}"
    )
    if node.name in _DERIVED_SESSION_EVENTS:
        assert derived_session, f"{node.name}: derived session_id declaration missing"
    return declared


def _effective_event_fields(classes: dict[str, ast.ClassDef], name: str, visiting: tuple[str, ...] = ()) -> set[str]:
    assert name not in visiting, f"{name}: cyclic class inheritance"
    node = classes[name]
    fields = _declared_event_fields(node, classes)
    for base in node.bases:
        if base.id in classes:
            fields |= _effective_event_fields(classes, base.id, (*visiting, name))
    return fields


def _check_invocation_event_family(source: str) -> None:
    classes = _event_classes(source)
    assert not [name for name in classes if name.startswith("SubAgent")], "Old SubAgent event family must stay removed"
    assert not ({"ContextPressure", "ToolCallArgsUpdated"} & classes.keys()), "Execution facts must not split families"
    event_classes = _event_descendants(classes, "Event")
    family = _event_descendants(classes, "InvocationEvent")
    assert "InvocationEvent" in event_classes, "InvocationEvent must inherit Event"
    envelope = _effective_event_fields(classes, "Event")
    for name in event_classes - family - {"Event"}:
        if name.startswith("Invocation"):
            assert name in _INVOCATION_COMMAND_FIELDS, f"{name}: counterfeit invocation fact"
            assert _effective_event_fields(classes, name) - envelope == _INVOCATION_COMMAND_FIELDS[name], (
                f"{name}: command fields minus Event envelope must be exactly invocation_id"
            )
    for name in family:
        node = classes[name]
        fields = _effective_event_fields(classes, name)
        assert "invocation_id" not in fields, f"{name}: second identity invocation_id is forbidden"
        identities = {
            field for field in fields - (envelope & {"event_id", "session_id"}) if field.endswith(("_id", "_ids"))
        }
        assert identities <= _INVOCATION_PAYLOAD_IDS, f"{name}: route facts through origin, not a second identity"
        if name == "InvocationEvent" or "origin" in _declared_event_fields(node, classes):
            origins = [
                field
                for field in node.body
                if isinstance(field, ast.AnnAssign)
                and isinstance(field.target, ast.Name)
                and field.target.id == "origin"
            ]
            assert len(origins) == 1, f"{name}: live origin must be annotated and required"
            assert ast.unparse(origins[0].annotation) == "InvocationOrigin"
            assert origins[0].value is None, "Live origin must be required, never repaired from ambient state"
            assert not any(
                isinstance(field, ast.Assign) and "origin" in {n.id for n in ast.walk(field) if isinstance(n, ast.Name)}
                for field in node.body
            ), f"{name}: origin must not be overwritten"


def test_invocation_event_family_requires_origin_without_legacy_routes() -> None:
    _check_invocation_event_family((SRC / "foundation/events/types.py").read_text(encoding="utf-8"))


def _runtime_mro_fields(cls: type) -> set[str]:
    names: set[str] = set()
    for base in cls.__mro__:
        names.update(vars(base))
        slots = vars(base).get("__slots__", ())
        assert isinstance(slots, tuple | list | set) and all(isinstance(slot, str) for slot in slots), (
            f"{base.__name__}: runtime __slots__ must be a string tuple/list/set"
        )
        names.update(slots)
    return names


def _static_ancestors(classes: dict[str, ast.ClassDef], name: str, visiting: tuple[str, ...] = ()) -> set[str]:
    assert name not in visiting, f"{name}: runtime reverse check: cyclic bases"
    result = {name}
    for base in classes[name].bases:
        assert isinstance(base, ast.Name), f"{name}: runtime reverse check: unresolved base"
        if base.id != "object":
            assert base.id in classes, f"{name}: runtime reverse check: foreign base {base.id}"
            result |= _static_ancestors(classes, base.id, (*visiting, name))
    return result


def _check_runtime_reverse(module: ModuleType, classes: dict[str, ast.ClassDef]) -> None:
    namespace = vars(module)
    for name in classes:
        obj = namespace.get(name)
        assert isinstance(obj, type), f"{name}: runtime reverse check: missing type binding"
        assert obj.__name__ == obj.__qualname__ == name and obj.__module__ == module.__name__, (
            f"{name}: runtime reverse check: class identity mismatch"
        )
    event, invocation = namespace["Event"], namespace["InvocationEvent"]
    descendants, family = _event_descendants(classes, "Event"), _event_descendants(classes, "InvocationEvent")
    for name in classes:
        obj = namespace[name]
        assert issubclass(obj, event) is (name in descendants), f"{name}: runtime reverse check: Event family mismatch"
        assert issubclass(obj, invocation) is (name in family), (
            f"{name}: runtime reverse check: runtime/static invocation family mismatch"
        )
        mro = [base for base in obj.__mro__ if base is not object]
        assert all(base is namespace.get(base.__name__) for base in mro), (
            f"{name}: runtime reverse check: MRO binding mismatch"
        )
        assert {base.__name__ for base in mro} == _static_ancestors(classes, name), (
            f"{name}: runtime reverse check: MRO ancestors mismatch"
        )


def _check_runtime_subclass_closure(module: ModuleType, classes: dict[str, ast.ClassDef]) -> None:
    namespace = vars(module)
    pending = list(namespace["Event"].__subclasses__())
    seen: set[type] = set()
    while pending:
        cls = pending.pop()
        if cls in seen:
            continue
        seen.add(cls)
        pending.extend(cls.__subclasses__())
        if cls.__module__ == module.__name__:
            assert cls.__name__ in classes and cls is namespace.get(cls.__name__), (
                f"{cls.__name__}: runtime subclass closure: hidden or replaced class"
            )


def _runtime_dataclass_options(node: ast.ClassDef) -> tuple[tuple[str, bool], ...] | None:
    # Read only supported decorator shapes, never execute a source expression.
    for decorator in node.decorator_list:
        if isinstance(decorator, ast.Name) and decorator.id == "dataclass":
            return ()
        if (
            isinstance(decorator, ast.Call)
            and isinstance(decorator.func, ast.Name)
            and decorator.func.id == "dataclass"
        ):
            return tuple(
                sorted(
                    (kw.arg, kw.value.value)
                    for kw in decorator.keywords
                    if kw.arg in {"kw_only", "frozen"}
                    and isinstance(kw.value, ast.Constant)
                    and type(kw.value.value) is bool
                )
            )
    return None


@cache
def _generated_class_attributes(options: tuple[tuple[str, bool], ...] | None) -> frozenset[str]:
    # Probe lives in this test module, never in the Event module or its closure.
    result: set[str] = set()
    # Empty dataclasses also exercise Python 3.14's lazy annotation cache keys.
    bodies = ('probe: str = ""', "pass")
    for body in bodies:
        namespace = {"__name__": __name__}
        exec(
            compile(f"from __future__ import annotations\nclass _Probe:\n    {body}\n", "<baseline>", "exec"), namespace
        )
        probe = namespace["_Probe"]
        if options is not None:
            probe = dataclass(probe, **dict(options))
        result.update(vars(probe))
        # Standard copying lazily caches class metadata (including slot names).
        # Exercise only our literal-default probe, never a declaration's factory.
        deepcopy(probe())
        result.update(vars(probe))
    return frozenset(result) - {"probe"}


def _runtime_declared_attributes(node: ast.ClassDef) -> set[str]:
    allowed: set[str] = set()
    for statement in node.body:
        if isinstance(statement, ast.Assign):
            for target in statement.targets:
                if isinstance(target, ast.Name):
                    allowed.add(target.id)
                    if target.id == "__slots__" and isinstance(statement.value, ast.Tuple | ast.List | ast.Set):
                        allowed.update(item.value for item in statement.value.elts if isinstance(item, ast.Constant))
        elif isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name):
            annotation = statement.annotation
            if (
                isinstance(annotation, ast.Subscript)
                and isinstance(annotation.value, ast.Name)
                and annotation.value.id == "ClassVar"
            ):
                allowed.add(statement.target.id)
    return allowed


def _check_runtime_class_attributes(module: ModuleType, classes: dict[str, ast.ClassDef]) -> None:
    for name, node in classes.items():
        obj = vars(module)[name]
        fields = {field.name for field in dataclass_fields(obj)} if is_dataclass(obj) else set()
        methods = {method for cls, method in _SUPPORTED_EVENT_METHODS if cls == name}
        assert methods <= set(vars(obj)), (
            f"{name}: runtime required event methods: missing {sorted(methods - set(vars(obj)))}"
        )
        allowed = (
            fields
            | _runtime_declared_attributes(node)
            | methods
            | _generated_class_attributes(_runtime_dataclass_options(node))
        )
        assert set(vars(obj)) <= allowed, (
            f"{name}: runtime attribute allowlist: unexpected {sorted(set(vars(obj)) - allowed)}"
        )


def _runtime_literal(value: object) -> bool:
    if type(value) in {str, int, float, bool, type(None)}:
        return True
    return type(value) in {tuple, list, set, frozenset} and all(_runtime_literal(item) for item in value)


def _check_runtime_module_bindings(module: ModuleType, source: str) -> None:
    # Fourth runtime defense: module binding values. Non-class assignment values
    # must be str/int/float/bool/None/tuple/list/set/frozenset, recursively without
    # callable/type. A deferred factory has no subclass yet; never execute it.
    for statement in ast.parse(source).body:
        if isinstance(statement, ast.Assign | ast.AnnAssign):
            targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
            for target in targets:
                if isinstance(target, ast.Name):
                    value = vars(module).get(target.id)
                    # Direct class aliases have their own legacy diagnostic.
                    if isinstance(value, type) and issubclass(value, vars(module)["Event"]):
                        continue
                    assert not target.id.startswith("__") and _runtime_literal(value), (
                        f"{target.id}: runtime module binding: nonliteral or dunder constant"
                    )


def _check_runtime_invocation_event_family(module: ModuleType, classes: dict[str, ast.ClassDef], source: str) -> None:
    """Independent runtime defenses; no call to the static syntax validator."""
    _check_runtime_reverse(module, classes)
    _check_runtime_class_attributes(module, classes)
    _check_runtime_subclass_closure(module, classes)
    _check_runtime_module_bindings(module, source)
    namespace = vars(module)
    event, invocation = namespace["Event"], namespace["InvocationEvent"]
    envelope = {field.name for field in dataclass_fields(event)}
    for name, obj in namespace.items():
        if not isinstance(obj, type) or not issubclass(obj, event):
            continue
        assert name in classes and obj.__name__ == name, f"{name}: runtime alias or undeclared/conditional class"
        fields = {field.name for field in dataclass_fields(obj)}
        effective = fields | _runtime_mro_fields(obj)
        if issubclass(obj, invocation):
            assert "invocation_id" not in effective, f"{name}: runtime second identity invocation_id"
            identities = {
                field
                for field in effective - (envelope & {"event_id", "session_id"})
                if field.endswith(("_id", "_ids"))
            }
            assert identities <= _INVOCATION_PAYLOAD_IDS, f"{name}: runtime second payload identity"
        else:
            if name in _INVOCATION_COMMAND_FIELDS:
                assert fields - envelope == _INVOCATION_COMMAND_FIELDS[name], f"{name}: runtime command fields"
            correlation = effective & {"call_id", "invocation_id", "tool_name"}
            assert correlation == _NON_INVOCATION_CORRELATION_FIELDS.get(name, set()), (
                f"{name}: runtime correlation allowlist mismatch"
            )


def test_invocation_event_family_runtime_matches_static_contract() -> None:
    import chrys.foundation.events.types as module

    _check_runtime_invocation_event_family(
        module,
        _top_level_event_classes((SRC / "foundation/events/types.py").read_text(encoding="utf-8")),
        (SRC / "foundation/events/types.py").read_text(encoding="utf-8"),
    )


def _non_invocation_correlation_fields(source: str) -> dict[str, set[str]]:
    classes = _event_classes(source)
    event_classes = _event_descendants(classes, "Event")
    family = _event_descendants(classes, "InvocationEvent")
    fields: dict[str, set[str]] = {}
    for name in sorted(event_classes - family - {"Event"}):
        correlation = _effective_event_fields(classes, name) & {"call_id", "invocation_id", "tool_name"}
        if correlation:
            fields[name] = correlation
    return fields


def _check_non_invocation_correlation_fields(source: str, allowed: dict[str, set[str]]) -> None:
    assert _non_invocation_correlation_fields(source) == allowed, (
        "Execution facts require InvocationOrigin; only explicit caller/request protocols may carry legacy routing fields"
    )


def test_non_invocation_events_only_declare_allowlisted_correlation_fields() -> None:
    _check_non_invocation_correlation_fields(
        (SRC / "foundation/events/types.py").read_text(encoding="utf-8"), _NON_INVOCATION_CORRELATION_FIELDS
    )


@pytest.mark.parametrize("field", ["call_id", "invocation_id", "tool_name"])
@pytest.mark.parametrize("base", ["Event", "Intermediate"])
@pytest.mark.parametrize("annotation", ["", ": str"])
def test_event_correlation_guard_detects_direct_and_inherited_declarations(
    field: str, base: str, annotation: str
) -> None:
    source = f'from __future__ import annotations\nclass Event: pass\nclass Intermediate(Event): pass\nclass UnscopedFact({base}):\n    {field}{annotation} = ""\n'
    assert _non_invocation_correlation_fields(source) == {"UnscopedFact": {field}}


_PINNED_METHODS = {
    "__post_init__": """    def __post_init__(self) -> None:
        if not isinstance(self.origin, InvocationOrigin):
            raise ValueError("Cannot route an invocation event without a live origin")
        if self.session_id and self.session_id != self.origin.session_id:
            raise ValueError("Event session does not match its invocation origin")
        object.__setattr__(self, "_sealed", True)
""",
    "__setattr__": """    def __setattr__(self, name: str, value: object) -> None:
        # User action envelopes are mutable; execution facts seal only after
        # their dataclass initializer has populated every inherited field.
        if self._sealed:
            raise FrozenInstanceError(f"cannot assign to field {name!r}")
        object.__setattr__(self, name, value)
""",
    "__delattr__": """    def __delattr__(self, name: str) -> None:
        raise FrozenInstanceError(f"cannot delete field {name!r}")
""",
}
_PINNED_METHOD_SOURCE = "\n".join(_PINNED_METHODS.values())
_INVOCATION_GUARD_SOURCE = (
    "\nfrom __future__ import annotations\nclass Event: pass\n"
    "class InvocationEvent(Event):\n    origin: InvocationOrigin\n"
    + _PINNED_METHOD_SOURCE
    + "class Intermediate(InvocationEvent): pass\n"
)
_CORRELATION_GUARD_SOURCE = """
class ApprovalRequest(Event):
    call_id: str = ""
    tool_name: str = ""
class HookConfirmationRequest(Event): tool_name: str = ""
class QuestionToUser(Event): call_id: str = ""
class SleepSkip(Event): call_id: str = ""
class InvocationRetryRequested(Event): invocation_id: str = ""
class InvocationAbortRequested(Event): invocation_id: str = ""
class WorkflowNodeStateChanged(Event): invocation_id: str = ""
"""


def test_event_correlation_guard_separates_invocation_facts_and_non_events() -> None:
    source = _INVOCATION_GUARD_SOURCE + (
        'class InvocationToolCallStart(Intermediate): call_id: str = ""\nclass PlainData: tool_name: str = ""\n'
    )
    _check_invocation_event_family(source)
    assert _non_invocation_correlation_fields(source) == {}
    counterfeit = 'class InvocationCounterfeit(Event): call_id: str = ""\n'
    with pytest.raises(AssertionError, match="counterfeit"):
        _check_invocation_event_family(source + counterfeit)
    assert _non_invocation_correlation_fields(source + counterfeit) == {"InvocationCounterfeit": {"call_id"}}


@pytest.mark.parametrize(
    "declaration",
    [
        'class InvocationCounterfeit(Event): call_id: str = ""',
        'class InvocationFact(InvocationEvent): invocation_id = ""',
        'class UnprefixedFact(Intermediate): invocation_id: str = ""',
        'class UnprefixedFact(Intermediate): sibling_invocation_id = ""',
        "class UnprefixedFact(Intermediate): origin: InvocationOrigin = None",
        "class UnprefixedFact(Intermediate): origin = None",
        'class InvocationRetryRequested(Event): invocation_id: str = ""; call_id: str = ""',
        "class InvocationAbortRequested(Event): pass",
    ],
)
def test_invocation_family_guard_rejects_counterfeit_and_second_identity(declaration: str) -> None:
    with pytest.raises(AssertionError):
        _check_invocation_event_family(_INVOCATION_GUARD_SOURCE + declaration)


def test_event_correlation_guard_pins_exact_allowlist() -> None:
    _check_invocation_event_family(_INVOCATION_GUARD_SOURCE + _CORRELATION_GUARD_SOURCE)
    _check_non_invocation_correlation_fields(
        _INVOCATION_GUARD_SOURCE + _CORRELATION_GUARD_SOURCE, _NON_INVOCATION_CORRELATION_FIELDS
    )
    assert {
        "ApprovalRequest": {"call_id", "tool_name"},
        "HookConfirmationRequest": {"tool_name"},
        "QuestionToUser": {"call_id"},
        "SleepSkip": {"call_id"},
        "WorkflowNodeStateChanged": {"invocation_id"},
        "InvocationRetryRequested": {"invocation_id"},
        "InvocationAbortRequested": {"invocation_id"},
    } == _NON_INVOCATION_CORRELATION_FIELDS


@pytest.mark.parametrize("mutation", ["extra-field", "missing-class"])
def test_event_correlation_guard_rejects_allowlist_drift(mutation: str) -> None:
    allowed = {name: set(fields) for name, fields in _NON_INVOCATION_CORRELATION_FIELDS.items()}
    if mutation == "extra-field":
        allowed["ApprovalRequest"].add("invocation_id")
    else:
        del allowed["InvocationAbortRequested"]
    with pytest.raises(AssertionError):
        _check_non_invocation_correlation_fields(_INVOCATION_GUARD_SOURCE + _CORRELATION_GUARD_SOURCE, allowed)


@pytest.mark.parametrize(
    ("declaration", "reason"),
    [
        pytest.param(
            'class Bare(Event): note: str = field(default_factory=lambda: eval("1"))',
            "unsupported factory body",
            id="factory-eval",
        ),
        pytest.param(
            "class Bare(Event): note: str = field(default_factory=lambda: arbitrary())",
            "unsupported factory body",
            id="factory-arbitrary-call",
        ),
        pytest.param(
            'registry = [type("Hidden", (globals()["InvocationEvent"],), {"invocation_id": "second"})]',
            "literal required",
            id="a3-dynamic-container",
        ),
        pytest.param(
            "@(lambda cls: [cls])\nclass Bare(InvocationEvent): pass",
            "unsupported class decorator",
            id="a3-decorator-container",
        ),
        pytest.param(
            "class Bare(InvocationEvent):\n    def __getattr__(self, name): return self.origin.invocation_id",
            "unsupported event method",
            id="a3-getattr-identity",
        ),
        pytest.param(
            'class Helper:\n    InvocationRetryRequested.extra = ""',
            "assignment target must be a Name",
            id="a3-class-write-target",
        ),
        pytest.param(
            'registry = (Alias := globals()["InvocationEvent"])', "literal required", id="a3-namedexpr-binding"
        ),
        pytest.param(
            'registry = [make_dataclass("Hidden", [("invocation_id", str)], bases=(globals()["InvocationEvent"],))]',
            "literal required",
            id="a3-dynamic-dataclass",
        ),
        pytest.param("holder = (first := 1)", "literal required", id="walrus-value"),
        pytest.param('holder = (Alias := globals()["InvocationEvent"])', "literal required", id="walrus-hidden-alias"),
        pytest.param("name: str", "module annotation requires a value", id="no-value-name"),
        pytest.param("type Number = int", "unsupported module statement TypeAlias", id="nonclass-pep695"),
        pytest.param(
            '@(lambda cls: [type(cls.__name__, (cls,), {"invocation_id": property(lambda self: self.origin.invocation_id + "/shadow")})])\nclass Decorated(InvocationEvent): pass',
            "unsupported class decorator",
            id="replacement-decorator",
        ),
        pytest.param(
            'class Hook:\n    def __init_subclass__(cls):\n        cls.invocation_id = property(lambda self: self.origin.invocation_id + "/shadow")\nclass Bare(Hook, InvocationEvent): pass',
            "unsupported event method",
            id="init-subclass",
        ),
        pytest.param(
            'class Descriptor:\n    def __set_name__(self, owner, name):\n        owner.invocation_id = property(lambda self: self.origin.invocation_id + "/shadow")\nclass Bare(InvocationEvent):\n    marker = Descriptor()',
            "unsupported event method",
            id="set-name",
        ),
        pytest.param(
            'class Bare(InvocationEvent):\n    @property\n    def invocation_id(self): return self.origin.invocation_id + "/shadow"',
            "unsupported event method",
            id="property",
        ),
        pytest.param(
            'class Bare(InvocationEvent):\n    def __getattr__(self, name):\n        if name == "invocation_id": return self.origin.invocation_id + "/shadow"\n        raise AttributeError(name)',
            "unsupported event method",
            id="getattr",
        ),
        pytest.param(
            'registry = [type("Hidden", (globals()["InvocationEvent"],), {"invocation_id": property(lambda self: self.origin.invocation_id + "/shadow")})]',
            "literal required",
            id="hidden-list-type",
        ),
        pytest.param(
            'factory = (lambda cls: lambda: cls)(type("Hidden", (globals()["InvocationEvent"],), {"invocation_id": property(lambda self: self.origin.invocation_id + "/shadow")}))',
            "literal required",
            id="closure-type",
        ),
        pytest.param(
            'import sys\nregistry = {"events": type("Hidden", (vars(sys.modules[__name__])["InvocationEvent"],), {"invocation_id": property(lambda self: self.origin.invocation_id + "/shadow")})}',
            "literal required",
            id="sysmodules-type",
        ),
        pytest.param(
            'class Helper:\n    InvocationRetryRequested.extra = ""',
            "assignment target must be a Name",
            id="class-attribute-injection",
        ),
        pytest.param(
            "@dataclass(slots=True)\nclass Bare(Event): pass",
            "unsupported dataclass keyword slots",
            id="dataclass-slots",
        ),
        pytest.param(
            "@dataclass(init=False)\nclass Bare(Event): pass", "unsupported dataclass keyword init", id="dataclass-init"
        ),
        pytest.param("@final\nclass Bare(Event): pass", "unsupported class decorator", id="non-dataclass-decorator"),
        pytest.param('__module__ = "x"', "dunder assignment target", id="module-dunder"),
        pytest.param('__all__ = ["InvocationEvent"]', "dunder assignment target", id="module-all"),
        pytest.param('class Bare(Event): __module__ = "x"', "dunder assignment target", id="class-dunder"),
        pytest.param("marker: str", "module annotation requires a value", id="module-no-value"),
        pytest.param(
            'class Bare(Event): note: str = field(default="", init=False)',
            "unsupported field keyword init",
            id="field-init",
        ),
        pytest.param(
            "class Bare(Event): note: str = field(default=InvocationEvent)",
            "literal required",
            id="field-class-default",
        ),
        pytest.param(
            'class Bare(Event): note: str = field("positional")', "field positional arguments", id="field-positional"
        ),
        pytest.param(
            "class Bare(Event): note: str = field(default_factory=InvocationEvent)",
            "local data factory must be non-Event with only object bases",
            id="factory-class",
        ),
        pytest.param(
            "class Bare(Event): note: str = field(default_factory=lambda: (x := 1))",
            "unsupported factory expression NamedExpr",
            id="factory-namedexpr",
        ),
        pytest.param("class Bare(Event): note: factory()", "unsupported annotation Call", id="annotation-call"),
        pytest.param(
            "def __getattr__(name): return 1", "unsupported module statement FunctionDef", id="module-getattr"
        ),
        pytest.param("class Bare(Event):\n    def read(self): return 1", "unsupported event method", id="method-name"),
        pytest.param(
            "class Bare(Event): first, second = 1, 2", "assignment target must be a Name", id="class-tuple-target"
        ),
        pytest.param(
            "class Bare(Event): table[0] = 1", "assignment target must be a Name", id="class-subscript-target"
        ),
        pytest.param(
            "class RuntimeModelDetails(InvocationEvent): pass\nclass Bare(Event): data: str = field(default_factory=RuntimeModelDetails)",
            "local data factory must be non-Event with only object bases",
            id="local-factory-base",
        ),
        pytest.param(
            "class Bare(Event): data: str = field(default_factory=DataRecord)\n"
            "@dataclass\nclass DataRecord:\n    def read(self): return 1",
            "local data factory must have no methods",
            id="local-factory-method",
        ),
        pytest.param(
            "class Bare(Event): note: str = field(default_factory=lambda: lambda: 1)",
            "unsupported factory expression Lambda",
            id="factory-nested-lambda",
        ),
        pytest.param(
            "class Bare(Event): note: str = field(default_factory=lambda: value[index])",
            "factory slice must be constant",
            id="factory-slice",
        ),
        pytest.param(
            "class Bare(Event): note: str = field(default_factory=lambda x: x)",
            "factory lambda must have no parameters",
            id="factory-args",
        ),
        pytest.param(
            "@dataclass(frozen=1)\nclass Bare(Event): pass", "dataclass frozen requires bool", id="dataclass-bool"
        ),
        pytest.param("class Bare[T](Event): pass", "class type parameters", id="class-generic"),
        pytest.param("Alias = InvocationEvent\nclass Bare(Alias): pass", "literal required", id="simple-alias"),
        pytest.param("Alias: type = InvocationEvent", "literal required", id="annotated-alias"),
        pytest.param("type Alias = InvocationEvent", "unsupported module statement TypeAlias", id="type-alias"),
        pytest.param("Alias = (InvocationEvent,)", "literal required", id="tuple-alias"),
        pytest.param('Alias = NewType("Alias", InvocationEvent).__supertype__', "literal required", id="newtype-alias"),
        pytest.param(
            "class Mix: invocation_id = None\nclass Bare(Mix, Intermediate): pass",
            "second identity",
            id="mixin-identity",
        ),
        pytest.param(
            'class Bare(Intermediate): __slots__ = ("invocation_id",)', "second identity", id="slots-identity"
        ),
        pytest.param(
            "if not TYPE_CHECKING:\n    class Bare(InvocationEvent): pass",
            "line .*non-top-level",
            id="conditional-class",
        ),
        pytest.param(
            'class Mix: extra: str = ""\nclass InvocationRetryRequested(Mix, Event): invocation_id = ""',
            "command fields",
            id="command-mixin-extra",
        ),
        pytest.param(
            'class InvocationAbortRequested(Event): invocation_id = ""; extra = ""',
            "command fields",
            id="command-body-extra",
        ),
        pytest.param("def factory():\n    class Bare(Event): pass", "line .*non-top-level", id="function-class"),
        pytest.param("class Outer:\n    class Bare(Event): pass", "line .*non-top-level", id="nested-class"),
        pytest.param(
            "try:\n    class Bare(Event): pass\nexcept Exception: pass", "line .*non-top-level", id="try-class"
        ),
        pytest.param("with context():\n    class Bare(Event): pass", "line .*non-top-level", id="with-class"),
        pytest.param(
            "if TYPE_CHECKING:\n    import typing\nelse:\n    class Bare(Event): pass",
            "line .*non-top-level",
            id="type-checking-else-class",
        ),
        pytest.param(
            "if TYPE_CHECKING:\n    class Bare(Event): pass", "line .*non-top-level", id="type-checking-class"
        ),
        pytest.param("if TYPE_CHECKING:\n    marker = 1", "imports only", id="type-checking-assignment"),
        pytest.param(
            "if TYPE_CHECKING:\n    import typing\nelse:\n    Alias = InvocationEvent",
            "else branch",
            id="type-checking-else-alias",
        ),
        pytest.param("class Bare(namespace.InvocationEvent): pass", "base must be a Name", id="attribute-base"),
        pytest.param("class Bare(InvocationEvent[str]): pass", "base must be a Name", id="subscript-base"),
        pytest.param("class Bare(*bases): pass", "base must be a Name", id="starred-base"),
        pytest.param("class Bare(factory()): pass", "base must be a Name", id="call-base"),
        pytest.param('class Bare("InvocationEvent"): pass', "base must be a Name", id="string-base"),
        pytest.param("class Bare(External): pass", "unknown external base", id="unknown-base"),
        pytest.param("class Bare(Intermediate): __slots__ = slots", "constant string", id="dynamic-slots"),
        pytest.param('class Bare(Intermediate): __slots__ = "invocation_id"', "constant string", id="string-slots"),
        pytest.param("class Bare(Intermediate): __slots__ = (42,)", "constant string", id="non-string-slots"),
        pytest.param("class Bare: pass\nclass Bare: pass", "duplicate class", id="duplicate-class"),
        pytest.param("class Bare(A): pass\nclass A(Bare, Event): pass", "cyclic", id="cyclic-bases"),
        pytest.param("class Bare(Event, metaclass=Meta): pass", "class keywords", id="metaclass"),
        pytest.param("if enabled:\n    marker = 1", "unsupported module statement", id="module-control-flow"),
        pytest.param('Intermediate.sibling_id = ""', "assignment target", id="attribute-target"),
        pytest.param('Intermediate.sibling_id: str = ""', "assignment target", id="annotated-attribute-target"),
        pytest.param("first, second = 1, 2", "assignment target", id="tuple-target"),
        pytest.param(
            'class Bare(Intermediate):\n    if enabled: invocation_id = ""',
            "unsupported class statement",
            id="class-control-flow",
        ),
    ],
)
def test_invocation_guard_rejects_unsupported_declarations(declaration: str, reason: str) -> None:
    with pytest.raises(AssertionError, match=reason):
        _check_invocation_event_family(_INVOCATION_GUARD_SOURCE + declaration)


@pytest.mark.parametrize("slots", ['("note",)', '["note"]', '{"note"}'])
def test_invocation_guard_accepts_identity_free_mixin_and_constant_slots(slots: str) -> None:
    source = _INVOCATION_GUARD_SOURCE + (
        "if TYPE_CHECKING:\n    import typing\n"
        f'class Mix(object):\n    description = ""\n    __slots__ = {slots}\n'
        "class Bare(Mix, Intermediate): pass\n"
    )
    _check_invocation_event_family(source)
    assert "Mix" not in _event_descendants(_event_classes(source), "InvocationEvent")
    assert _non_invocation_correlation_fields(source) == {}


@pytest.mark.parametrize("field", ["call_id", "invocation_id", "tool_name"])
@pytest.mark.parametrize("declaration", ['{field} = ""', '__slots__ = ("{field}",)'])
def test_event_correlation_guard_includes_non_event_ancestors(field: str, declaration: str) -> None:
    source = _INVOCATION_GUARD_SOURCE + (
        f"class Mix: {declaration.format(field=field)}\nclass Bare(Mix, Event): pass\n"
    )
    assert _non_invocation_correlation_fields(source) == {"Bare": {field}}
    with pytest.raises(AssertionError, match="explicit caller/request"):
        _check_non_invocation_correlation_fields(source, {})


_RUNTIME_EVENT_SOURCE = (
    """
from __future__ import annotations
from dataclasses import FrozenInstanceError, dataclass, field, make_dataclass
from typing import ClassVar, TYPE_CHECKING
from chrys.foundation.models.invocations import InvocationOrigin
@dataclass
class Event:
    event_id: str = ""
    session_id: str = ""
@dataclass(kw_only=True)
class InvocationEvent(Event):
    origin: InvocationOrigin
    _sealed: ClassVar[bool] = False
"""
    + _PINNED_METHOD_SOURCE
)


@pytest.mark.parametrize(
    ("declaration", "reason"),
    [
        pytest.param("Alias = InvocationEvent", "runtime alias", id="alias"),
        pytest.param("if True:\n    class Bare(InvocationEvent): pass", "runtime subclass closure", id="conditional"),
        pytest.param(
            "class Mix: invocation_id = None\nclass Bare(Mix, InvocationEvent): pass",
            "runtime second identity",
            id="mixin",
        ),
        pytest.param(
            'class Bare(InvocationEvent): __slots__ = ("invocation_id",)', "runtime second identity", id="slots"
        ),
        pytest.param(
            '@dataclass\nclass Bare(InvocationEvent): sibling_id: str = ""', "second payload identity", id="payload-id"
        ),
        pytest.param(
            '@dataclass\nclass Mix: extra: str = ""\n'
            '@dataclass\nclass InvocationRetryRequested(Mix, Event): invocation_id: str = ""',
            "runtime command fields",
            id="command-mixin-extra",
        ),
        pytest.param(
            '@dataclass\nclass InvocationAbortRequested(Event): invocation_id: str = ""; extra: str = ""',
            "runtime command fields",
            id="command-body-extra",
        ),
        pytest.param(
            'class Mix: call_id = ""\nclass Bare(Mix, Event): pass', "correlation allowlist", id="correlation-mixin"
        ),
        pytest.param('class Bare(Event): __slots__ = ("tool_name",)', "correlation allowlist", id="correlation-slots"),
        pytest.param(
            '@dataclass\nclass Bare(Event): invocation_id: str = ""', "correlation allowlist", id="correlation-field"
        ),
        pytest.param(
            "class Bare(InvocationEvent): pass\nBare.__name__ = 'Renamed'",
            "runtime reverse check: class identity mismatch",
            id="renamed-class",
        ),
    ],
)
def test_runtime_invocation_guard_independently_rejects_escapes(
    declaration: str, reason: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Deliberately bypass static syntax/field validation: this tests the runtime
    # defense itself, including when the static guard would already reject.
    source = _RUNTIME_EVENT_SOURCE + declaration
    classes = {node.name: node for node in ast.parse(source).body if isinstance(node, ast.ClassDef)}
    module = ModuleType("_event_guard_runtime")
    monkeypatch.setitem(sys.modules, module.__name__, module)
    exec(compile(source, "<event-guard-test>", "exec", dont_inherit=True), vars(module))
    with pytest.raises(AssertionError, match=reason):
        _check_runtime_invocation_event_family(module, classes, source)


def test_runtime_invocation_guard_rejects_family_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    source = _RUNTIME_EVENT_SOURCE + "class Bare(Event): pass"
    classes = _event_classes(source.replace("class Bare(Event)", "class Bare(InvocationEvent)"))
    module = ModuleType("_event_guard_runtime")
    monkeypatch.setitem(sys.modules, module.__name__, module)
    exec(compile(source, "<event-guard-test>", "exec", dont_inherit=True), vars(module))
    with pytest.raises(AssertionError, match="runtime/static invocation family mismatch"):
        _check_runtime_invocation_event_family(module, classes, source)


@pytest.mark.parametrize("runtime", [False, True], ids=["static", "runtime"])
def test_event_guard_does_not_exempt_new_envelope_identity(runtime: bool, monkeypatch: pytest.MonkeyPatch) -> None:
    source = _RUNTIME_EVENT_SOURCE.replace('    event_id: str = ""', '    event_id: str = ""\n    sibling_id: str = ""')
    classes = _event_classes(source)
    if runtime:
        module = ModuleType("_event_guard_runtime")
        monkeypatch.setitem(sys.modules, module.__name__, module)
        exec(compile(source, "<event-guard-test>", "exec", dont_inherit=True), vars(module))
        with pytest.raises(AssertionError, match="runtime second payload identity"):
            _check_runtime_invocation_event_family(module, classes, source)
    else:
        with pytest.raises(AssertionError, match="second identity"):
            _check_invocation_event_family(source)


_A3_RUNTIME_SOURCE = (
    _RUNTIME_EVENT_SOURCE + '@dataclass\nclass InvocationRetryRequested(Event): invocation_id: str = ""\n'
)

_A3_CASES = [
    pytest.param(
        "from typing import TypeAlias\nAlias: TypeAlias = InvocationEvent",
        "literal required",
        "runtime alias",
        id="new-TypeAlias",
    ),
    pytest.param(
        'registry = [type("Hidden", (globals()["InvocationEvent"],), {"invocation_id": property(lambda self: self.origin.invocation_id + "/shadow")})]\n__all__ = ["Hidden"]\n__getattr__ = lambda name: registry[0]',
        "literal required",
        "runtime subclass closure",
        id="new-all-getattr",
    ),
    pytest.param(
        'class Helper:\n    InvocationRetryRequested.extra = ""',
        "assignment target must be a Name",
        "runtime attribute allowlist",
        id="new-class-attribute-injection",
    ),
    pytest.param(
        'class Bare(InvocationEvent): invocation_id: ClassVar[str] = "second"',
        "second identity",
        "runtime second identity",
        id="new-classvar",
    ),
    pytest.param(
        'factory = (lambda cls: lambda: cls)(type("Hidden", (globals()["InvocationEvent"],), {"invocation_id": property(lambda self: self.origin.invocation_id + "/shadow")}))',
        "literal required",
        "runtime subclass closure",
        id="new-closure-type",
    ),
    pytest.param(
        '@dataclass\nclass Bare(InvocationEvent): invocation_id: str = field(default="second", init=False)',
        "unsupported field keyword init",
        "runtime second identity",
        id="new-field-init-false",
    ),
    pytest.param(
        'class Bare(InvocationEvent):\n    def __getattr__(self, name):\n        if name == "invocation_id": return self.origin.invocation_id + "/shadow"\n        raise AttributeError(name)',
        "unsupported event method",
        "runtime attribute allowlist",
        id="new-getattr",
    ),
    pytest.param('globals()["tag"] = 1', "assignment target must be a Name", None, id="new-globals-target"),
    pytest.param(
        'registry = [type("Hidden", (globals()["InvocationEvent"],), {"invocation_id": property(lambda self: self.origin.invocation_id + "/shadow")})]',
        "literal required",
        "runtime subclass closure",
        id="new-hidden-list-type",
    ),
    pytest.param(
        'class Hook:\n    def __init_subclass__(cls):\n        cls.__getattr__ = lambda self, name: self.origin.invocation_id + "/shadow"\nclass Bare(Hook, InvocationEvent): pass',
        "unsupported event method",
        "runtime attribute allowlist",
        id="new-hook-getattr",
    ),
    pytest.param(
        'class Hook:\n    def __init_subclass__(cls):\n        cls.invocation_id = property(lambda self: self.origin.invocation_id + "/shadow")\nclass Bare(Hook, InvocationEvent): pass',
        "unsupported event method",
        "runtime attribute allowlist",
        id="new-init-subclass",
    ),
    pytest.param("[first, second] = [1, 2]", "assignment target must be a Name", None, id="new-list-target"),
    pytest.param(
        'from dataclasses import make_dataclass\nregistry = [make_dataclass("Hidden", [("invocation_id", str, field(default="second", init=False))], bases=(globals()["InvocationEvent"],))]',
        "literal required",
        "runtime subclass closure",
        id="new-make-dataclass-hidden",
    ),
    pytest.param(
        'from typing import NewType\nAlias = NewType("Alias", InvocationEvent)',
        "literal required",
        "runtime module binding",
        id="new-newtype",
    ),
    pytest.param(
        "InvocationRetryRequested.extra: str", "assignment target must be a Name", None, id="new-no-value-attribute"
    ),
    pytest.param("type Alias = InvocationEvent", "unsupported module statement TypeAlias", None, id="new-pep695"),
    pytest.param(
        'class Bare(InvocationEvent):\n    @property\n    def invocation_id(self): return self.origin.invocation_id + "/shadow"',
        "unsupported event method",
        "runtime attribute allowlist",
        id="new-property",
    ),
    pytest.param(
        '@(lambda cls: [type(cls.__name__, (cls,), {"invocation_id": property(lambda self: self.origin.invocation_id + "/shadow")})])\nclass Decorated(InvocationEvent): pass',
        "unsupported class decorator",
        "runtime reverse check",
        id="new-replacement-decorator",
    ),
    pytest.param(
        '@(lambda cls: type(cls.__name__, (cls,), {"sibling_id": property(lambda self: self.origin.invocation_id + "/shadow")}))\nclass Decorated(InvocationEvent): pass',
        "unsupported class decorator",
        "runtime reverse check",
        id="new-same-name-decorator",
    ),
    pytest.param(
        'class Descriptor:\n    def __set_name__(self, owner, name):\n        owner.invocation_id = property(lambda self: self.origin.invocation_id + "/shadow")\nclass Bare(InvocationEvent):\n    marker = Descriptor()',
        "unsupported event method",
        "runtime attribute allowlist",
        id="new-set-name",
    ),
    pytest.param("*first, = [1, 2]", "assignment target must be a Name", None, id="new-starred-target"),
    pytest.param('table = [0]\ntable[0] = ""', "assignment target must be a Name", None, id="new-subscript-target"),
    pytest.param(
        'import sys\nregistry = [sys.modules.setdefault(__name__ + ".hidden", type("Hidden", (globals()["InvocationEvent"],), {"invocation_id": property(lambda self: self.origin.invocation_id + "/shadow")}))]',
        "literal required",
        "runtime subclass closure",
        id="new-sysmodules-inject",
    ),
    pytest.param(
        'import sys\nregistry = {"events": type("Hidden", (vars(sys.modules[__name__])["InvocationEvent"],), {"invocation_id": property(lambda self: self.origin.invocation_id + "/shadow")})}',
        "literal required",
        "runtime subclass closure",
        id="new-sysmodules-type",
    ),
    pytest.param(
        'registry = [(factory := lambda: type("Hidden", (globals()["InvocationEvent"],), {"invocation_id": property(lambda self: self.origin.invocation_id + "/shadow")}))]',
        "literal required",
        "runtime module binding",
        id="new-walrus-hidden-class",
    ),
    pytest.param("holder = (first := 1)", "literal required", None, id="new-walrus-value"),
]


@pytest.mark.parametrize(("declaration", "static_reason", "runtime_reason"), _A3_CASES)
def test_a3_probe_static_independence(declaration: str, static_reason: str, runtime_reason: str | None) -> None:
    with pytest.raises(AssertionError, match=static_reason):
        _check_invocation_event_family(_A3_RUNTIME_SOURCE + declaration)


@pytest.mark.parametrize(
    ("declaration", "static_reason", "runtime_reason"),
    [case for case in _A3_CASES if case.values[2] is not None],
)
def test_a3_probe_runtime_independence(
    declaration: str, static_reason: str, runtime_reason: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _A3_RUNTIME_SOURCE + declaration
    module = ModuleType("prc_runtime_events")
    monkeypatch.setitem(sys.modules, module.__name__, module)
    # Register the side key with monkeypatch before the sys.modules injection.
    monkeypatch.setitem(sys.modules, module.__name__ + ".hidden", None)
    del sys.modules[module.__name__ + ".hidden"]
    exec(compile(source, "<event-guard-test>", "exec", dont_inherit=True), vars(module))
    with pytest.raises(AssertionError, match=runtime_reason):
        _check_runtime_invocation_event_family(module, _top_level_event_classes(source), source)


_SUPPORTED_SOURCE = (
    _RUNTIME_EVENT_SOURCE
    + """
@dataclass(frozen=True)
class Data:
    position: tuple[int, int] = (0, 0)
@dataclass
class Bare(InvocationEvent):
    description: ClassVar[str] = "plain"
    token: str = field(default_factory=lambda: uuid4().hex[:12])
    items: list[str] = field(default_factory=list, repr=False, compare=False)
    created: datetime = field(default_factory=lambda: datetime.now(tz=UTC))
"""
)


@pytest.mark.parametrize("runtime", [False, True], ids=["static", "runtime"])
def test_event_guard_accepts_supported_grammar(runtime: bool, monkeypatch: pytest.MonkeyPatch) -> None:
    source = _SUPPORTED_SOURCE
    if runtime:
        from dataclasses import FrozenInstanceError
        from datetime import UTC, datetime
        from uuid import uuid4

        module = ModuleType("prc_supported_events")
        monkeypatch.setitem(sys.modules, module.__name__, module)
        vars(module).update(FrozenInstanceError=FrozenInstanceError, UTC=UTC, datetime=datetime, uuid4=uuid4)
        exec(compile(source, "<supported>", "exec", dont_inherit=True), vars(module))
        _check_runtime_invocation_event_family(module, _top_level_event_classes(source), source)
    else:
        _check_invocation_event_family(source)


@pytest.mark.parametrize("base", ["", "(object)"], ids=["no-base", "object-base"])
def test_event_guard_accepts_structural_local_factory(base: str) -> None:
    source = _SUPPORTED_SOURCE + (
        f"@dataclass\nclass FreshData{base}:\n    value: str = ''\n"
        "@dataclass\nclass Record(Event):\n    data: FreshData = field(default_factory=FreshData)\n"
    )
    _check_invocation_event_family(source)


@pytest.mark.parametrize(
    "decorator",
    ["", "@dataclass", "@dataclass(kw_only=True)", "@dataclass(frozen=True)"],
    ids=["plain", "dataclass", "kw-only", "frozen"],
)
def test_runtime_attribute_baseline_accepts_standard_copy_cache(
    decorator: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _RUNTIME_EVENT_SOURCE + f"{decorator}\nclass Data: pass\n"
    module = ModuleType("prc_copy_events")
    monkeypatch.setitem(sys.modules, module.__name__, module)
    exec(compile(source, "<standard-copy>", "exec", dont_inherit=True), vars(module))
    data_class = vars(module)["Data"]
    before = set(vars(data_class))
    deepcopy(data_class())
    assert set(vars(data_class)) > before, "standard copying must populate the lazy class cache in this regression"
    _check_runtime_invocation_event_family(module, _top_level_event_classes(source), source)


@pytest.mark.parametrize("method", ["__post_init__", "__setattr__", "__delattr__"])
@pytest.mark.parametrize("mutation", ["body", "decorator"], ids=["method-hash", "method-decorator"])
def test_event_guard_pins_allowed_method_structure(method: str, mutation: str) -> None:
    replacement = f"    @staticmethod\n    def {method}(" if mutation == "decorator" else f"    def {method}("
    source = _SUPPORTED_SOURCE.replace(f"    def {method}(", replacement)
    if mutation == "body":
        tree = ast.parse(source)
        node = next(
            child
            for cls in tree.body
            if isinstance(cls, ast.ClassDef) and cls.name == "InvocationEvent"
            for child in cls.body
            if isinstance(child, ast.FunctionDef) and child.name == method
        )
        node.body.append(ast.Pass())
        source = ast.unparse(ast.fix_missing_locations(tree))
    reason = "method decorators" if mutation == "decorator" else "event method hash mismatch"
    with pytest.raises(AssertionError, match=reason):
        _check_invocation_event_family(source)


@pytest.mark.parametrize("event_name", sorted(_DERIVED_SESSION_EVENTS))
@pytest.mark.parametrize(
    "declaration",
    [
        "",
        '__slots__ = ("session_id",)',
        "session_id = None",
        "session_id: str | None = None",
        "session_id: str | None = field(default=None, init=True)",
        'session_id: str | None = field(default="other", init=False)',
    ],
)
def test_event_guard_rejects_independent_workflow_session_identity(event_name: str, declaration: str) -> None:
    tree = ast.parse((SRC / "foundation/events/types.py").read_text(encoding="utf-8"))
    node = next(item for item in tree.body if isinstance(item, ast.ClassDef) and item.name == event_name)
    field = next(
        item
        for item in node.body
        if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name) and item.target.id == "session_id"
    )
    index = node.body.index(field)
    node.body[index : index + 1] = ast.parse(declaration).body
    with pytest.raises(AssertionError, match="derived session_id declaration"):
        _check_invocation_event_family(ast.unparse(ast.fix_missing_locations(tree)))


@pytest.mark.parametrize("event_name", sorted(_DERIVED_SESSION_EVENTS))
@pytest.mark.parametrize("remove", [False, True], ids=["changed-derivation", "missing-derivation"])
def test_event_guard_pins_workflow_session_derivation(event_name: str, remove: bool) -> None:
    tree = ast.parse((SRC / "foundation/events/types.py").read_text(encoding="utf-8"))
    node = next(item for item in tree.body if isinstance(item, ast.ClassDef) and item.name == event_name)
    method = next(item for item in node.body if isinstance(item, ast.FunctionDef) and item.name == "__post_init__")
    if remove:
        node.body.remove(method)
    else:
        method.body = ast.parse('self.session_id = "other"').body
    with pytest.raises(AssertionError, match="required event methods" if remove else "event method hash mismatch"):
        _check_invocation_event_family(ast.unparse(ast.fix_missing_locations(tree)))


@pytest.mark.parametrize("method", ["__post_init__", "__setattr__", "__delattr__"])
@pytest.mark.parametrize("runtime", [False, True], ids=["static", "runtime"])
def test_event_guard_requires_every_pinned_method(method: str, runtime: bool, monkeypatch: pytest.MonkeyPatch) -> None:
    # Deleting a sealing method is an ordinary cleanup edit, so both lines must
    # reject it independently: the hash pin alone only checks methods that exist.
    source = _SUPPORTED_SOURCE.replace(_PINNED_METHODS[method], "")
    assert f"def {method}(" not in source, "fixture mutation must remove the whole method"
    if not runtime:
        with pytest.raises(AssertionError, match="InvocationEvent: required event methods"):
            _check_invocation_event_family(source)
        return
    module = ModuleType("_event_guard_runtime")
    monkeypatch.setitem(sys.modules, module.__name__, module)
    exec(compile(source, "<event-guard-test>", "exec", dont_inherit=True), vars(module))
    with pytest.raises(AssertionError, match="InvocationEvent: runtime required event methods"):
        _check_runtime_invocation_event_family(module, _top_level_event_classes(source), source)


@pytest.mark.parametrize("shadowed", [False, True], ids=["empty-table", "class-named-datetime"])
def test_factory_expression_rejects_class_names_independently(shadowed: bool) -> None:
    # Pins the root-Name rule of the structural factory grammar on its own, with
    # a registered lambda body so registry equality cannot mask the rule.
    expression = ast.parse("datetime.now(tz=UTC)", mode="eval").body
    node = ast.parse("class datetime: pass").body[0]
    assert isinstance(node, ast.ClassDef)
    classes: dict[str, ast.ClassDef] = {"datetime": node} if shadowed else {}
    if shadowed:
        with pytest.raises(AssertionError, match="factory class Name is unsupported"):
            _check_factory_expression(expression, classes)
    else:
        _check_factory_expression(expression, classes)


@pytest.mark.parametrize("prefix", ["", "import typing\n"], ids=["missing-future", "future-not-first-import"])
def test_event_guard_requires_future_annotations(prefix: str) -> None:
    source = _RUNTIME_EVENT_SOURCE.replace("from __future__ import annotations\n", "")
    if prefix:
        source = prefix + "from __future__ import annotations\n" + source
    with pytest.raises(AssertionError, match="first import must be from __future__ import annotations"):
        _event_classes(source)


@pytest.mark.parametrize("runtime", [False, True], ids=["static", "runtime"])
def test_event_guard_rejects_foreign_mro(runtime: bool, monkeypatch: pytest.MonkeyPatch) -> None:
    source = _RUNTIME_EVENT_SOURCE + "class Bare(External, InvocationEvent): pass"
    if runtime:

        class External:
            pass

        module = ModuleType("prc_foreign_events")
        monkeypatch.setitem(sys.modules, module.__name__, module)
        vars(module)["External"] = External
        exec(compile(source, "<foreign>", "exec", dont_inherit=True), vars(module))
        with pytest.raises(AssertionError, match="runtime reverse check: foreign base"):
            _check_runtime_invocation_event_family(module, _top_level_event_classes(source), source)
    else:
        with pytest.raises(AssertionError, match="unknown external base"):
            _check_invocation_event_family(source)


def test_runtime_subclass_closure_ignores_other_test_modules(monkeypatch: pytest.MonkeyPatch) -> None:
    module = ModuleType("prc_foreign_events")
    source = _RUNTIME_EVENT_SOURCE
    monkeypatch.setitem(sys.modules, module.__name__, module)
    exec(compile(source, "<other-test-module>", "exec", dont_inherit=True), vars(module))
    event = vars(module)["Event"]

    class ForeignEvent(event):
        invocation_id = "not owned by the declarations module"

    assert ForeignEvent.__module__ == __name__
    assert ForeignEvent in event.__subclasses__()
    _check_runtime_invocation_event_family(module, _top_level_event_classes(source), source)


@pytest.mark.parametrize(
    ("declaration", "reason"),
    [
        pytest.param(
            'registry = [type("Hidden", (globals()["InvocationEvent"],), {"invocation_id": "second"})]',
            "runtime subclass closure",
            id="a3-dynamic-container",
        ),
        pytest.param(
            "@(lambda cls: [cls])\nclass Bare(InvocationEvent): pass",
            "runtime reverse check",
            id="a3-decorator-container",
        ),
        pytest.param(
            "class Bare(InvocationEvent):\n    def __getattr__(self, name): return self.origin.invocation_id",
            "runtime attribute allowlist",
            id="a3-getattr-identity",
        ),
        pytest.param(
            'class Helper:\n    InvocationRetryRequested.extra = ""',
            "runtime attribute allowlist",
            id="a3-class-write-target",
        ),
        pytest.param('registry = (Alias := globals()["InvocationEvent"])', "runtime alias", id="a3-namedexpr-binding"),
        pytest.param(
            'registry = [make_dataclass("Hidden", [("invocation_id", str)], bases=(globals()["InvocationEvent"],))]',
            "runtime subclass closure",
            id="a3-dynamic-dataclass",
        ),
        pytest.param('class Bare(Event): __module__ = "x"', "runtime reverse check", id="class-dunder"),
        pytest.param('__all__ = ["InvocationEvent"]', "runtime module binding", id="module-all"),
        pytest.param(
            "class Bare(Event):\n    def read(self): return 1", "runtime attribute allowlist", id="method-name"
        ),
        pytest.param(
            '@dataclass(slots=True)\nclass Bare(Event): note: str = ""',
            "runtime attribute allowlist",
            id="dataclass-slots",
        ),
        pytest.param(
            'class Bare(InvocationEvent): sibling_id: ClassVar[str] = "second"',
            "runtime second payload identity",
            id="classvar-payload",
        ),
    ],
)
def test_syntax_counterexamples_visible_at_runtime(
    declaration: str, reason: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # kw_only envelope permits the original six-cell make_dataclass declaration
    # to reach the guard instead of failing Python's required/default ordering.
    source = (
        _A3_RUNTIME_SOURCE.replace("@dataclass\nclass Event:", "@dataclass(kw_only=True)\nclass Event:") + declaration
    )
    module = ModuleType("prc_syntax_runtime")
    monkeypatch.setitem(sys.modules, module.__name__, module)
    exec(compile(source, "<syntax-runtime>", "exec", dont_inherit=True), vars(module))
    with pytest.raises(AssertionError, match=reason):
        _check_runtime_invocation_event_family(module, _top_level_event_classes(source), source)
