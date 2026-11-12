# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Src rules: no hand-rolled exchange walkers, no unjustified classifier imports; pins and backstops."""

from __future__ import annotations

import ast
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path

import pytest

from tests.architecture._hygiene_core import (
    _SCOPE_BOUNDARY_NODES,
    _allowlist_target_trees,
    _meta_guard_problem,
    _pins_allowlist,
    _qualified_name,
    _src_sources,
    _tree,
)
from tests.support.ci import CI_LINUX_ONLY

# Platform-independent source analysis: the Linux CI job covers it.
pytestmark = CI_LINUX_ONLY


# --- exchange-walker guard (scans src/chrys, not tests) ---------------------
#
# The transcript exchange grammar lives in chrys.kernel.exchanges
# (iter_exchanges/pair_results); every production consumer reads boundaries and
# pairing from it. This guard is the tripwire against the NEXT hand-rolled
# walker: transcript loops that re-derive exchange structure locally. Tests are
# deliberately out of scope — the invariant oracle and the differential
# reference machine are independent walkers BY DESIGN.


_EXCHANGE_WALKER_ALLOWLIST = {
    # Annotation-occurrence scanner: walks persisted group-annotation runs, not
    # exchanges; result classification only delimits occurrence ends.
    (Path("src/chrys/kernel/compaction.py"), "_partial_tool_call_groups"),
    # Incremental re-annotation rewind: walks BACKWARD along the grammar's
    # member shapes to the enclosing exchange's start so regrouping sees the
    # whole exchange (call/result fusion) without rescanning the prefix —
    # running iter_exchanges forward would cost the full-list pass this
    # rewind exists to avoid. Makes no pairing decision; grouping itself
    # still consumes iter_exchanges output.
    (Path("src/chrys/kernel/compaction.py"), "_reannotation_start"),
    # Group-level pairing consumers: operate WITHIN one already-annotated
    # group, with deliberately local policies (preservation-pinned).
    (Path("src/chrys/service/context/compaction/scoped.py"), "_tool_group_integrity"),
    (Path("src/chrys/service/context/compaction/last_words.py"), "_format_tool_group"),
    (Path("src/chrys/service/context/compaction/summaries.py"), "_build_summary"),
    # Positional current-turn slot bucketing for batch-id stamping — a
    # (call_id, name) presentation scan, not an exchange walk.
    (Path("src/chrys/service/session/history.py"), "SessionHistoryManager.persist_batch_ids"),
    # Pairs file-tool calls positionally with edit-snapshot refs over
    # serialized dicts; fail-soft zip truncation is deliberate.
    (Path("src/chrys/app/tui/screens/main/session_handlers.py"), "SessionHandler.load_file_edit_snapshots"),
    # Deliberately NARROW legacy-sidecar dedup policy over batch-tagged
    # tool-call messages; justified with a preservation pin, not migrated.
    (Path("src/chrys/app/tui/widgets/chat/replay.py"), "_legacy_duplicate_intermediate_sidecars"),
    # Replay-ID minting totals over the globally-coerced presentation key;
    # pairing decisions live in the grammar-backed coordinate map.
    # Per-content image-stub rewrite: rebuilds function results around
    # stubbed images (falsy call ids normalize to ""); makes no pairing or
    # boundary decision.
    (Path("src/chrys/service/vision.py"), "NonVisionImageStubMiddleware.process"),
    # Bounded fallback-timeline renderer over already-scoped groups; the
    # sibling group formatter it dispatches to is display-only pairing.
    (Path("src/chrys/service/context/compaction/last_words.py"), "_format_dropped"),
    # Group-level replay planner: classifies the calls and results WITHIN one
    # group partition_groups already cut from iter_exchanges output; which
    # group's call a result answers comes from pair_results (_result_owners).
    (Path("src/chrys/service/llm/openai_responses/replay.py"), "_plan_group"),
}


# Outside the grammar module, importing the role-gated result-only classifier
# is itself a walker smell: consumers should consume iter_exchanges output,
# not re-classify messages. Justified importers only.
_CLASSIFIER_IMPORT_ALLOWLIST = {
    # The group annotator and the annotation-occurrence scanner share the
    # grammar's follower rule for their secondary, compaction-owned partition.
    Path("src/chrys/kernel/compaction.py"),
}


_EXCHANGE_GRAMMAR_MODULE = Path("src/chrys/kernel/exchanges.py")


_RESULT_ONLY_CLASSIFIER = "is_result_only_message"


_EXCHANGE_GRAMMAR_ENTRYPOINTS = {"iter_exchanges", "pair_results"}


_TYPE_SET_NAMES = ("TOOL_CALL_CONTENT_TYPES", "TOOL_RESULT_CONTENT_TYPES")


_TOOL_CONTENT_TYPE_STRINGS = frozenset(
    {
        "function_call",
        "hosted_tool_call",
        "code_interpreter_tool_call",
        "image_generation_tool_call",
        "mcp_server_tool_call",
        "search_tool_call",
        "shell_tool_call",
        "function_result",
        "hosted_tool_result",
        "code_interpreter_tool_result",
        "image_generation_tool_result",
        "mcp_server_tool_result",
        "search_tool_result",
        "shell_tool_result",
        "legacy",
    }
)


_LOOP_NODES = (ast.For, ast.AsyncFor, ast.While, ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)


_COMPREHENSION_NODES = (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)


def _scoped_walk(root: ast.AST) -> Iterator[ast.AST]:
    """``ast.walk`` confined to the root's own scope.

    A nested function, lambda, or class body is a separate scope whose
    cursors and reads get their own guard evaluation; attributing them to
    the enclosing function would flag a formatter for a helper it merely
    defines.
    """
    stack: list[ast.AST] = [root]
    while stack:
        node = stack.pop()
        yield node
        for child in ast.iter_child_nodes(node):
            if not isinstance(child, _SCOPE_BOUNDARY_NODES):
                stack.append(child)


def _loop_target_names(func: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in _scoped_walk(func):
        targets: list[ast.expr] = []
        if isinstance(node, (ast.For, ast.AsyncFor)):
            targets.append(node.target)
        elif isinstance(node, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
            targets.extend(generator.target for generator in node.generators)
        for target in targets:
            names.update(name.id for name in ast.walk(target) if isinstance(name, ast.Name))
    return names


def _walked_item_names(func: ast.AST) -> set[str]:
    """Loop targets plus simple aliases of them.

    ``message = messages[index]`` and ``message = item`` walk the same item
    as the loop construct itself; the fixpoint keeps chained rebindings from
    hiding a role read behind a fresh name.
    """
    names = _loop_target_names(func)
    changed = True
    while changed:
        changed = False
        for node in _scoped_walk(func):
            target: ast.expr | None = None
            value: ast.expr | None = None
            if isinstance(node, ast.Assign) and len(node.targets) == 1:
                target, value = node.targets[0], node.value
            elif isinstance(node, (ast.AnnAssign, ast.NamedExpr)):
                target, value = node.target, node.value
            if not isinstance(target, ast.Name) or target.id in names or value is None:
                continue
            if isinstance(value, ast.Subscript) or (isinstance(value, ast.Name) and value.id in names):
                names.add(target.id)
                changed = True
    return names


def _role_read_bases(node: ast.AST) -> list[ast.expr]:
    """The expression each ``role`` read is performed on, in any spelling."""
    bases: list[ast.expr] = []
    for child in _scoped_walk(node):
        if isinstance(child, ast.Attribute) and child.attr == "role":
            bases.append(child.value)
        elif (
            isinstance(child, ast.Call)
            and isinstance(child.func, ast.Attribute)
            and child.func.attr == "get"
            and child.args
            and isinstance(child.args[0], ast.Constant)
            and child.args[0].value == "role"
        ):
            bases.append(child.func.value)
        elif isinstance(child, ast.Subscript) and isinstance(child.slice, ast.Constant) and child.slice.value == "role":
            bases.append(child.value)
    return bases


def _reads_walked_role(node: ast.AST, walked_names: set[str], matches_role_helper: Callable[[ast.expr], bool]) -> bool:
    """Role read off a loop-walked item — a loop-target name or a subscript.

    Reading role off a plain parameter (a per-message wire serializer) is not
    a transcript walk; reading it off the item a loop iterates, or off
    ``messages[index]`` in a cursor loop, is. Passing a walked item to a
    local role-reading helper counts the same as reading inline.
    """

    def _is_walked(value: ast.expr) -> bool:
        if isinstance(value, ast.Name) and value.id in walked_names:
            return True
        return isinstance(value, ast.Subscript)

    if any(_is_walked(base) for base in _role_read_bases(node)):
        return True
    return any(
        isinstance(child, ast.Call)
        and matches_role_helper(child.func)
        and any(_is_walked(argument) for argument in [*child.args, *(keyword.value for keyword in child.keywords)])
        for child in _scoped_walk(node)
    )


def _classification_references(node: ast.AST, matches_classifier: Callable[[ast.expr], bool]) -> bool:
    """Tool call/result classification: type sets, type strings, classifiers.

    Helper indirection does not launder a walker: calling a locally defined
    classifier (any local helper that itself references classification)
    counts the same as reading the type sets inline.
    """
    for child in _scoped_walk(node):
        if isinstance(child, (ast.Name, ast.Attribute)):
            name = _qualified_name(child)
            if name.endswith(_TYPE_SET_NAMES) or name.split(".")[-1] == _RESULT_ONLY_CLASSIFIER:
                return True
        if isinstance(child, ast.Constant) and child.value in _TOOL_CONTENT_TYPE_STRINGS:
            return True
        if isinstance(child, ast.Call) and matches_classifier(child.func):
            return True
    return False


def _reads_tool_id(node: ast.AST) -> bool:
    """A call_id/image_id read in any spelling."""
    for child in _scoped_walk(node):
        if isinstance(child, ast.Attribute) and child.attr in ("call_id", "image_id"):
            return True
        if (
            isinstance(child, ast.Call)
            and isinstance(child.func, ast.Attribute)
            and child.func.attr == "get"
            and child.args
            and isinstance(child.args[0], ast.Constant)
            and child.args[0].value in ("call_id", "image_id")
        ):
            return True
        if (
            isinstance(child, ast.Subscript)
            and isinstance(child.slice, ast.Constant)
            and child.slice.value in ("call_id", "image_id")
        ):
            return True
    return False


def _enumerate_target_index(target: ast.expr, source: ast.expr) -> str | None:
    """The index name a ``for index, item in enumerate(...)`` target binds,
    seeing through the identity-preserving container wrappers —
    ``reversed(list(enumerate(...)))`` is a backwards enumerate walk."""
    while (
        isinstance(source, ast.Call)
        and _qualified_name(source.func).split(".")[-1] in _CONTAINER_WRAPPER_CALLEES
        and source.args
    ):
        source = source.args[0]
    if (
        isinstance(source, ast.Call)
        and _qualified_name(source.func) == "enumerate"
        and isinstance(target, ast.Tuple)
        and target.elts
        and isinstance(target.elts[0], ast.Name)
    ):
        return target.elts[0].id
    return None


def _enumerate_index_names(func: ast.AST) -> set[str]:
    """Index targets of statement-level enumerate loops.

    Comprehension targets are Python-scoped to the comprehension and are
    judged per comprehension node, never pooled into the function scope —
    an outer name that happens to match one is not a cursor.
    """
    names: set[str] = set()
    for node in _scoped_walk(func):
        if isinstance(node, (ast.For, ast.AsyncFor)):
            index = _enumerate_target_index(node.target, node.iter)
            if index is not None:
                names.add(index)
    return names


def _comprehension_enumerate_indices(comp_node: ast.AST) -> set[str]:
    """Enumerate indices bound by one comprehension's own generators."""
    names: set[str] = set()
    for generator in getattr(comp_node, "generators", []):
        index = _enumerate_target_index(generator.target, generator.iter)
        if index is not None:
            names.add(index)
    return names


def _contains_name(node: ast.AST, names: set[str]) -> bool:
    return any(isinstance(child, ast.Name) and child.id in names for child in ast.walk(node))


def _is_membership_test(node: ast.AST) -> bool:
    return isinstance(node, ast.Compare) and all(isinstance(op, (ast.In, ast.NotIn)) for op in node.ops)


def _captures_name(node: ast.AST, names: set[str]) -> bool:
    """A membership test asks a question about the index, an f-string
    renders it as text, and a comprehension's value slot collects results
    into a container (the comprehension spelling of append-reporting) —
    none of them capture its value. Comprehension filters and iterables
    still do. Equality/order comparisons are boundary logic and get their
    own construct rule."""
    stack: list[ast.AST] = [node]
    while stack:
        current = stack.pop()
        if _is_membership_test(current) or isinstance(current, ast.JoinedStr):
            continue
        if isinstance(current, _COMPREHENSION_NODES):
            for generator in current.generators:
                stack.append(generator.iter)
                stack.extend(generator.ifs)
            continue
        if isinstance(current, ast.Name) and current.id in names:
            return True
        stack.extend(ast.iter_child_nodes(current))
    return False


def _capture_slots(target: ast.expr) -> list[ast.expr]:
    """The assignable slots of a target, with tuple/list unpacking flattened."""
    if isinstance(target, (ast.Tuple, ast.List)):
        return [slot for element in target.elts for slot in _capture_slots(element)]
    if isinstance(target, ast.Starred):
        return _capture_slots(target.value)
    return [target]


def _is_literal_expression(node: ast.AST) -> bool:
    """A literal in any AST spelling — nothing resolved at runtime."""
    return not any(isinstance(child, (ast.Name, ast.Attribute, ast.Call, ast.Subscript)) for child in ast.walk(node))


def _is_scalar_capture_slot(slot: ast.expr) -> bool:
    """Names, attributes, and FIXED subscript keys hold cursor state; a
    dynamic subscript key is the coordinate/reporter-map shape."""
    if isinstance(slot, (ast.Name, ast.Attribute)):
        return True
    return isinstance(slot, ast.Subscript) and _is_literal_expression(slot.slice)


def _fixed_pairs_capture(node: ast.AST, names: set[str]) -> bool:
    """A mapping-shaped expression writing a captured value under a literal
    key, in any ordinary ``dict.update`` input spelling: a dict literal, a
    ``dict(...)`` call, or an iterable of ``(key, value)`` pairs."""
    if isinstance(node, ast.Dict):
        return any(
            key is not None and _is_literal_expression(key) and _captures_name(value, names)
            for key, value in zip(node.keys, node.values, strict=True)
        )
    if isinstance(node, ast.Call) and _qualified_name(node.func).split(".")[-1] == "dict":
        return any(
            _captures_name(keyword.value, names)
            if keyword.arg is not None
            else _fixed_pairs_capture(keyword.value, names)
            for keyword in node.keywords
        ) or any(_fixed_pairs_capture(argument, names) for argument in node.args)
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return any(
            isinstance(element, ast.Tuple)
            and len(element.elts) == 2
            and _is_literal_expression(element.elts[0])
            and _captures_name(element.elts[1], names)
            for element in node.elts
        )
    return False


def _is_mapping_shaped(node: ast.AST) -> bool:
    """An argument whose SHAPE proves dict.update mapping semantics — with
    one present, sibling keyword arguments are dict fields, not API params.
    A ``|`` union is mapping-shaped when either side is."""
    if isinstance(node, ast.Dict):
        return True
    if isinstance(node, ast.Call) and _qualified_name(node.func).split(".")[-1] == "dict":
        return True
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return bool(node.elts) and all(
            isinstance(element, ast.Tuple) and len(element.elts) == 2 for element in node.elts
        )
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        return _is_mapping_shaped(node.left) or _is_mapping_shaped(node.right)
    return False


_SCALAR_COLLAPSE_CALLEES = ("max", "min", "next", "sum")


_CONTAINER_WRAPPER_CALLEES = ("list", "sorted", "tuple", "reversed", "iter")


def _is_fixed_field_store_call(call: ast.Call, names: set[str]) -> bool:
    """update/setdefault/setattr are the call spellings of a fixed-key store.

    ``bounds.update(end=index)``, ``bounds.update({"end": index})`` (in any
    mapping spelling) and ``bounds.setdefault("end", index)`` /
    ``setattr(bounds, "end", index)`` write cursor state exactly like
    ``bounds["end"] = index``. A dynamic key keeps the coordinate-map
    exemption; value-passing callees (emit, log, append, insert) hand the
    index to another component and stay reporting — as does an ``update``
    call carrying a positional handle argument (``progress.update(task_id,
    completed=position)``), which is the reporter-API shape rather than the
    keyword-only or mapping-shaped dict.update idioms.
    """
    callee = _qualified_name(call.func).split(".")[-1]
    if callee == "update":
        keyword_capture = any(
            _captures_name(keyword.value, names)
            if keyword.arg is not None
            else _fixed_pairs_capture(keyword.value, names)
            for keyword in call.keywords
        )
        if keyword_capture and (not call.args or any(_is_mapping_shaped(argument) for argument in call.args)):
            return True
        return any(_fixed_pairs_capture(argument, names) for argument in call.args)
    if callee == "setdefault":
        return (
            len(call.args) >= 2
            and _is_literal_expression(call.args[0])
            and any(_captures_name(argument, names) for argument in call.args[1:])
        )
    if callee == "setattr":
        return len(call.args) >= 3 and _is_literal_expression(call.args[1]) and _captures_name(call.args[2], names)
    return False


def _comprehension_operand(node: ast.AST) -> ast.AST | None:
    """The comprehension an expression operates on, unwrapping the
    identity-preserving container wrappers (list/sorted/tuple/reversed/iter)."""
    while (
        isinstance(node, ast.Call)
        and _qualified_name(node.func).split(".")[-1] in _CONTAINER_WRAPPER_CALLEES
        and node.args
    ):
        node = node.args[0]
    return node if isinstance(node, _COMPREHENSION_NODES) else None


def _unpacked_scalar_slots(target: ast.expr) -> list[ast.expr]:
    """Non-starred slots a destructuring target extracts as scalars.

    A plain name binds the whole container (a positions report keeps
    reporting); ``end, = ...`` extracts the element. Starred slots bind
    lists and stay containers."""
    if isinstance(target, (ast.Tuple, ast.List)):
        return [
            slot
            for element in target.elts
            if not isinstance(element, ast.Starred)
            for slot in (
                [element] if not isinstance(element, (ast.Tuple, ast.List)) else _unpacked_scalar_slots(element)
            )
        ]
    return []


def _comprehension_collapses_index(node: ast.AST) -> bool:
    """A comprehension (possibly wrapper-wrapped) whose value slot captures
    its OWN enumerate index — collapsing it yields scalar cursor state."""
    comp = _comprehension_operand(node)
    if comp is None:
        return False
    indices = _comprehension_enumerate_indices(comp)
    return bool(indices) and any(_captures_name(value, indices) for value in _comprehension_values(comp))


def _comprehension_values(node: ast.AST) -> list[ast.expr]:
    """The value slots a comprehension collects into its container."""
    if isinstance(node, ast.DictComp):
        return [node.key, node.value]
    if isinstance(node, (ast.ListComp, ast.SetComp, ast.GeneratorExp)):
        return [node.elt]
    return []


def _has_pairing_construct(func: ast.AST, matches_id_reader: Callable[[ast.expr], bool]) -> bool:
    """Pairing/boundary state: id reads, index arithmetic, or a cursor walk.

    A pure content formatter renders types without any of these and stays
    outside the guard's reach. The id read is deliberately coarse — a
    transcript-looping serializer that only ECHOES call ids still matches,
    and earns a justified allowlist entry rather than a narrower trigger:
    under-triggering here is what lets a genuinely new walker ship. A call
    to a local id-reading helper counts the same as reading inline.
    An enumerate index is cursor bookkeeping when its VALUE is captured
    (arithmetic, subscripting by it, returning/yielding it, storing it
    into a name, attribute, or fixed subscript key — through tuple
    unpacking or a fixed-field store call like update/setdefault/setattr)
    or when it GATES control flow (an equality/order comparison, an
    if/while/conditional-expression test, a match subject or case guard).
    Comprehension targets are judged within their own comprehension: a
    filter that gates on the index, or a collapse of an index-valued
    comprehension to a scalar (max/min/next/sum, or a non-slice subscript,
    through identity-preserving wrappers), is cursor bookkeeping there.
    Membership tests, f-string rendering, append-style and keyword
    reporting, comprehension container building (returned or sliced), and
    dynamic-key coordinate maps carry no pairing state; a nested function
    or lambda's cursor belongs to its own scope.
    """
    if _reads_tool_id(func):
        return True
    enumerate_indices = _enumerate_index_names(func)
    for child in _scoped_walk(func):
        if isinstance(child, ast.Call) and matches_id_reader(child.func):
            return True
        if isinstance(child, ast.Subscript) and any(isinstance(node, ast.BinOp) for node in ast.walk(child.slice)):
            return True
        if isinstance(child, ast.While) and any(
            isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Name) for node in _scoped_walk(child)
        ):
            return True
        if (
            isinstance(child, ast.For)
            and isinstance(child.iter, ast.Call)
            and _qualified_name(child.iter.func) == "range"
            and any(
                isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Name) for node in _scoped_walk(child)
            )
        ):
            return True
        if isinstance(child, _COMPREHENSION_NODES):
            comp_indices = _comprehension_enumerate_indices(child)
            if comp_indices and any(
                _captures_name(test, comp_indices) for generator in child.generators for test in generator.ifs
            ):
                return True
        if (
            isinstance(child, ast.Call)
            and _qualified_name(child.func).split(".")[-1] in _SCALAR_COLLAPSE_CALLEES
            and any(_comprehension_collapses_index(argument) for argument in child.args)
        ):
            return True
        if (
            isinstance(child, ast.Subscript)
            and not isinstance(child.slice, ast.Slice)
            and _comprehension_collapses_index(child.value)
        ):
            return True
        if (
            isinstance(child, ast.Call)
            and isinstance(child.func, ast.Attribute)
            and child.func.attr == "pop"
            and _comprehension_collapses_index(child.func.value)
        ):
            return True
        if (
            isinstance(child, ast.Assign)
            and _comprehension_collapses_index(child.value)
            and any(
                _is_scalar_capture_slot(slot) for target in child.targets for slot in _unpacked_scalar_slots(target)
            )
        ):
            return True
        if not enumerate_indices:
            continue
        if isinstance(child, ast.BinOp) and _contains_name(child, enumerate_indices):
            return True
        if (
            isinstance(child, ast.Subscript)
            and isinstance(child.slice, ast.Name)
            and child.slice.id in enumerate_indices
        ):
            return True
        if (
            isinstance(child, ast.Compare)
            and not _is_membership_test(child)
            and _captures_name(child, enumerate_indices)
        ):
            return True
        if isinstance(child, (ast.If, ast.While, ast.IfExp)) and _captures_name(child.test, enumerate_indices):
            return True
        if isinstance(child, ast.Match) and _captures_name(child.subject, enumerate_indices):
            return True
        if (
            isinstance(child, ast.match_case)
            and child.guard is not None
            and _captures_name(child.guard, enumerate_indices)
        ):
            return True
        if isinstance(child, ast.Call) and _is_fixed_field_store_call(child, enumerate_indices):
            return True
        if (
            isinstance(child, (ast.Return, ast.Yield))
            and child.value is not None
            and _captures_name(child.value, enumerate_indices)
        ):
            return True
        capture_targets: list[ast.expr] = []
        capture_value: ast.expr | None = None
        if isinstance(child, ast.Assign):
            capture_targets, capture_value = child.targets, child.value
        elif isinstance(child, (ast.AnnAssign, ast.AugAssign, ast.NamedExpr)):
            capture_targets, capture_value = [child.target], child.value
        if (
            capture_value is not None
            and any(_is_scalar_capture_slot(slot) for target in capture_targets for slot in _capture_slots(target))
            and _captures_name(capture_value, enumerate_indices)
        ):
            return True
    return False


def _references_exchange_grammar(func: ast.AST, matches_grammar_caller: Callable[[ast.expr], bool]) -> bool:
    """A grammar CALL whose result flows — naming or discarding is not consuming.

    Calling a local helper that itself calls the grammar counts too: a
    consumer may precompute pairing in one function and walk with the
    result in another. A bare expression-statement call throws its result
    away and exempts nothing.
    """
    discarded = {node.value for node in _scoped_walk(func) if isinstance(node, ast.Expr)}
    return any(
        isinstance(node, ast.Call)
        and node not in discarded
        and (
            _qualified_name(node.func).split(".")[-1] in _EXCHANGE_GRAMMAR_ENTRYPOINTS
            or matches_grammar_caller(node.func)
        )
        for node in _scoped_walk(func)
    )


class _ModuleScopes:
    """Lexical scope index for one module.

    Records every function-like scope (defs AND lambdas — each gets exactly
    one guard evaluation of its own) with its enclosing-scope chain,
    qualified name, and owning class, plus every named helper binding: def
    statements and name-bound lambdas alike. Helper calls resolve with
    lexical fidelity — a bare name walks the enclosing def scopes out to
    module level and the NEAREST binding shadows outer ones (class bodies
    are not name scopes), where a non-helper binding at a level (parameter,
    assignment, loop target) makes the name OPAQUE and stops resolution;
    ``self.``/``cls.`` attributes resolve to methods of the calling scope's
    class lineage (local base classes included); any other attribute base
    falls back to coarse last-name matching against module-level functions
    and methods, where a bare-name call could not resolve — strict callers
    demand ALL same-named candidates agree before trusting the fallback.
    """

    def __init__(self, tree: ast.Module) -> None:
        self.scopes: list[ast.AST] = []
        self.qualified: dict[int, str] = {}
        self._tree = tree
        self._chains: dict[int, tuple[ast.AST, ...]] = {}
        self._owners: dict[int, ast.ClassDef | None] = {}
        # name -> [(helper node, id of binding def scope or None, class the
        # binding hangs off when it is a direct class attribute)]
        self._helpers: dict[str, list[tuple[ast.AST, int | None, ast.ClassDef | None]]] = {}
        self._classes: dict[str, list[ast.ClassDef]] = {}
        self._opaque: dict[int | None, frozenset[str]] = {}
        self._lineages: dict[int, frozenset[int]] = {}
        self._collect(tree, (), "", None)

    def _add_helper(
        self, name: str, node: ast.AST, chain: tuple[ast.AST, ...], method_class: ast.ClassDef | None
    ) -> None:
        binding = id(chain[-1]) if chain else None
        self._helpers.setdefault(name, []).append((node, binding, method_class))

    def _collect(self, node: ast.AST, chain: tuple[ast.AST, ...], qual: str, owner: ast.ClassDef | None) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                name = getattr(child, "name", "<lambda>")
                self.scopes.append(child)
                self.qualified[id(child)] = qual + name
                self._chains[id(child)] = (*chain, child)
                self._owners[id(child)] = owner
                if not isinstance(child, ast.Lambda):
                    self._add_helper(child.name, child, chain, owner if isinstance(node, ast.ClassDef) else None)
                self._collect(child, (*chain, child), qual + name + ".", owner)
            elif isinstance(child, ast.ClassDef):
                self._classes.setdefault(child.name, []).append(child)
                self._collect(child, chain, qual + child.name + ".", child)
            else:
                if isinstance(child, (ast.Assign, ast.AnnAssign, ast.NamedExpr)) and isinstance(
                    child.value, ast.Lambda
                ):
                    targets = child.targets if isinstance(child, ast.Assign) else [child.target]
                    for target in targets:
                        if isinstance(target, ast.Name):
                            self._add_helper(
                                target.id, child.value, chain, owner if isinstance(node, ast.ClassDef) else None
                            )
                self._collect(child, chain, qual, owner)

    def _opaque_names(self, level: ast.AST | None) -> frozenset[str]:
        """Names a def scope (or the module) binds OUTSIDE the helper index.

        A parameter, assignment, or loop target rebinds the name to a value
        the index knows nothing about — a call through it must not resolve
        to an outer helper of the same name."""
        key = None if level is None else id(level)
        cached = self._opaque.get(key)
        if cached is not None:
            return cached
        names: set[str] = set()
        scope: ast.AST = self._tree if level is None else level
        if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            arguments = scope.args
            names.update(arg.arg for arg in (*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs))
            names.update(arg.arg for arg in (arguments.vararg, arguments.kwarg) if arg is not None)
        for child in _scoped_walk(scope):
            if isinstance(child, ast.ExceptHandler) and child.name:
                names.add(child.name)
            targets: tuple[ast.expr, ...] = ()
            if isinstance(child, ast.Assign):
                targets = tuple(child.targets)
            elif isinstance(child, (ast.AnnAssign, ast.AugAssign, ast.NamedExpr, ast.For, ast.AsyncFor)):
                targets = (child.target,)
            elif isinstance(child, ast.withitem) and child.optional_vars is not None:
                targets = (child.optional_vars,)
            for target in targets:
                names.update(
                    node.id
                    for node in ast.walk(target)
                    if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store)
                )
        result = frozenset(names)
        self._opaque[key] = result
        return result

    def _lineage(self, owner: ast.ClassDef) -> frozenset[int]:
        """The class and its LOCAL ancestors — where ``self.helper`` can live."""
        cached = self._lineages.get(id(owner))
        if cached is not None:
            return cached
        seen: set[int] = set()
        queue = [owner]
        while queue:
            cls = queue.pop()
            if id(cls) in seen:
                continue
            seen.add(id(cls))
            for base in cls.bases:
                if isinstance(base, ast.Subscript):
                    base = base.value
                base_name = _qualified_name(base).split(".")[-1]
                queue.extend(self._classes.get(base_name, []))
        result = frozenset(seen)
        self._lineages[id(owner)] = result
        return result

    def resolves_flagged(
        self, caller: ast.AST, callee: ast.expr, flags: dict[int, bool], *, strict_attr: bool = False
    ) -> bool:
        """Whether a call from ``caller`` resolves to a flagged helper."""
        name = _qualified_name(callee)
        if not name:
            return False
        bare = name.split(".")[-1]
        candidates = self._helpers.get(bare, [])
        if isinstance(callee, ast.Attribute):
            base = callee.value
            if isinstance(base, ast.Name) and base.id in ("self", "cls"):
                owner = self._owners[id(caller)]
                if owner is None:
                    return False
                lineage = self._lineage(owner)
                return any(
                    flags.get(id(node), False)
                    for node, _binding, method_class in candidates
                    if method_class is not None and id(method_class) in lineage
                )
            module_flags = [
                flags.get(id(node), False) for node, binding, _method_class in candidates if binding is None
            ]
            if strict_attr:
                return bool(module_flags) and all(module_flags)
            return any(module_flags)
        for level in (*reversed(self._chains[id(caller)]), None):
            level_id = None if level is None else id(level)
            bound = [node for node, binding, method_class in candidates if binding == level_id and method_class is None]
            if bound:
                return any(flags.get(id(node), False) for node in bound)
            if bare in self._opaque_names(level):
                return False
        return False


def _resolver(
    index: _ModuleScopes, caller: ast.AST, flags: dict[int, bool], *, strict_attr: bool = False
) -> Callable[[ast.expr], bool]:
    def matches(callee: ast.expr) -> bool:
        return index.resolves_flagged(caller, callee, flags, strict_attr=strict_attr)

    return matches


def _matches_no_helper(_callee: ast.expr) -> bool:
    """Null matcher for the one-hop base predicates."""
    return False


def _assert_no_hand_rolled_exchange_walkers(sources: Mapping[Path, str]) -> None:
    """Transcript walkers must read exchange structure from the shared grammar.

    A function is flagged when a loop reads message role off the items it
    walks AND classifies tool call/result contents (directly or through a
    local classifier helper), and the function carries pairing/boundary state
    — unless it reads the grammar itself (iter_exchanges/pair_results) or
    holds a justified allowlist entry. History-marker handling is exactly what
    hand-rolled walkers forget, and no mechanical rule can require what is
    absent — so the guard keys on the walking constructs themselves.
    Every scope (functions, methods, lambdas) is judged exactly once under
    its qualified name, and helper propagation resolves calls with lexical
    fidelity (nearest binding for bare names, own class for self/cls).
    """
    violations: list[str] = []
    for path, source in sources.items():
        if path == _EXCHANGE_GRAMMAR_MODULE:
            continue
        tree = _tree(path, source)
        index = _ModuleScopes(tree)
        role_flags = {id(scope): bool(_role_read_bases(scope)) for scope in index.scopes}
        id_flags = {id(scope): _reads_tool_id(scope) for scope in index.scopes}
        classifier_flags = {id(scope): _classification_references(scope, _matches_no_helper) for scope in index.scopes}
        grammar_flags = {id(scope): _references_exchange_grammar(scope, _matches_no_helper) for scope in index.scopes}
        for func in index.scopes:
            name = index.qualified[id(func)]
            if (path, name) in _EXCHANGE_WALKER_ALLOWLIST or _references_exchange_grammar(
                func, _resolver(index, func, grammar_flags, strict_attr=True)
            ):
                continue
            walked_names = _walked_item_names(func)
            matches_role_helper = _resolver(index, func, role_flags)
            matches_classifier = _resolver(index, func, classifier_flags)
            flagged = any(
                _reads_walked_role(loop, walked_names, matches_role_helper)
                and _classification_references(loop, matches_classifier)
                for loop in _scoped_walk(func)
                if isinstance(loop, _LOOP_NODES)
            )
            if flagged and _has_pairing_construct(func, _resolver(index, func, id_flags)):
                violations.append(
                    f"{path}:{func.lineno}: {name} hand-rolls an exchange walk; read boundaries and "
                    "pairing from chrys.kernel.exchanges (iter_exchanges/pair_results) or add a justified "
                    "_EXCHANGE_WALKER_ALLOWLIST entry"
                )
    assert violations == [], "\n".join(violations)


def _assert_result_only_classifier_imports_are_allowlisted(sources: Mapping[Path, str]) -> None:
    """Importing the role-gated result-only classifier is a walker smell.

    Consumers get result-only handling from iter_exchanges output; the module
    backstop catches a walker whose loop shape evades the mechanical trigger.
    """
    violations: list[str] = []
    for path, source in sources.items():
        if path == _EXCHANGE_GRAMMAR_MODULE or path in _CLASSIFIER_IMPORT_ALLOWLIST:
            continue
        for node in ast.walk(_tree(path, source)):
            named_import = isinstance(node, ast.ImportFrom) and any(
                alias.name == _RESULT_ONLY_CLASSIFIER for alias in node.names
            )
            # Module-alias access (``ex.is_result_only_message``) reaches the
            # classifier without any ImportFrom, so the use site counts too.
            aliased_use = isinstance(node, ast.Attribute) and node.attr == _RESULT_ONLY_CLASSIFIER
            if named_import or aliased_use:
                violations.append(
                    f"{path}:{node.lineno}: reaching {_RESULT_ONLY_CLASSIFIER} outside the grammar module "
                    "requires a justified _CLASSIFIER_IMPORT_ALLOWLIST entry; consume iter_exchanges instead"
                )
    assert violations == [], "\n".join(violations)


@_pins_allowlist("_EXCHANGE_WALKER_ALLOWLIST")
def test_exchange_walker_allowlist_entries_are_live() -> None:
    """Every justified walker entry must resolve to exactly ONE scope by its
    qualified name — a stale entry would silently vouch for a walker added
    later under the same name, and an ambiguous one for a scope it never
    justified."""
    sources = _src_sources()
    problems: list[str] = []
    for path, qualified in sorted(_EXCHANGE_WALKER_ALLOWLIST):
        source = sources.get(path)
        if source is None:
            problems.append(f"{path}: file missing")
            continue
        index = _ModuleScopes(_tree(path, source))
        matches = [scope for scope in index.scopes if index.qualified[id(scope)] == qualified]
        if not matches:
            problems.append(f"{path}: no scope named {qualified}")
        elif len(matches) > 1:
            problems.append(f"{path}: {qualified} is ambiguous ({len(matches)} scopes)")
    _tree.cache_clear()
    assert problems == [], "\n".join(problems)


@_pins_allowlist("_CLASSIFIER_IMPORT_ALLOWLIST")
def test_classifier_import_allowlist_entries_are_live() -> None:
    trees = _allowlist_target_trees(frozenset(_CLASSIFIER_IMPORT_ALLOWLIST))
    problems: list[str] = []
    for path in sorted(_CLASSIFIER_IMPORT_ALLOWLIST):
        tree = trees.get(path)
        if tree is None:
            problems.append(
                _meta_guard_problem(
                    "_CLASSIFIER_IMPORT_ALLOWLIST",
                    f"entry {path} names a missing file",
                    "remove the stale entry or update it to the live result-only-classifier importer",
                )
            )
            continue
        references = [
            node
            for node in ast.walk(tree)
            if (isinstance(node, ast.ImportFrom) and any(alias.name == _RESULT_ONLY_CLASSIFIER for alias in node.names))
            or (isinstance(node, ast.Attribute) and node.attr == _RESULT_ONLY_CLASSIFIER)
        ]
        if not references:
            problems.append(
                _meta_guard_problem(
                    "_CLASSIFIER_IMPORT_ALLOWLIST",
                    f"entry {path} contains no {_RESULT_ONLY_CLASSIFIER} import or qualified use",
                    "remove the stale entry or update it to the module that reaches the result-only classifier",
                )
            )

    assert problems == [], "\n".join(problems)


def test_classifier_import_backstop_rejects_unallowlisted_module() -> None:
    source = "from chrys.kernel.exchanges import is_result_only_message\n"
    with pytest.raises(AssertionError, match="_CLASSIFIER_IMPORT_ALLOWLIST"):
        _assert_result_only_classifier_imports_are_allowlisted({Path("src/chrys/service/bad.py"): source})


def test_classifier_import_backstop_accepts_justified_importer() -> None:
    source = "from .exchanges import is_result_only_message\n"
    _assert_result_only_classifier_imports_are_allowlisted({Path("src/chrys/kernel/compaction.py"): source})


def test_classifier_backstop_rejects_module_alias_attribute_use() -> None:
    source = (
        "import chrys.kernel.exchanges as ex\n"
        "\n"
        "def check(message, accessor):\n"
        "    return ex.is_result_only_message(message, accessor)\n"
    )
    with pytest.raises(AssertionError, match="_CLASSIFIER_IMPORT_ALLOWLIST"):
        _assert_result_only_classifier_imports_are_allowlisted({Path("src/chrys/service/bad.py"): source})
