# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Compact, model-actionable diagnostics for tool argument errors."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from difflib import get_close_matches
from heapq import heappop, heappush
from itertools import islice
from typing import Any, cast

from pydantic import AliasChoices, AliasPath, BaseModel, ValidationError
from pydantic_core import ErrorDetails

_MAX_ARGUMENT_ISSUES = 3
_MAX_SCHEMA_FIELDS = 10
_MAX_SCHEMA_DEPTH = 4
_MAX_SCHEMA_TEXT_CHARS = 700
_MAX_MESSAGE_CHARS = 1200
_MAX_ERROR_DETAIL_CHARS = 160
_MAX_IDENTIFIER_CHARS = 80
# Magnitude bound (~77 digits) for rendering exact-int mapping keys. Must be
# independent of the interpreter's int→str digit limit, which
# sys.set_int_max_str_digits(0) disables entirely — a multi-megabyte repr must
# not be built even transiently.
_MAX_INT_KEY_BITS = 256
# Bounds on formatting-path work over argument values: a hostile container can
# iterate indefinitely, shared references can amplify a small graph into
# exponential traversal work, and comparison operators on non-exact types
# dispatch to user code. Enum/const checks therefore only compare all-exact
# trees within a TOTAL node-visit budget, and the payload scan spends at most
# a fixed work budget (large strings charge by size).
_MAX_COMPARE_ITEMS = 256
_MAX_PAYLOAD_SCAN_ITEMS = 10_000
# Enum rendering cap: the narrowest rendered value is one character plus a
# three-character separator, so 192 values always reach the 700-character
# schema-text bound (1 + 191*4 = 765) — the capped join truncates identically
# to the uncapped one while never serializing a pathologically wide enum.
_MAX_ENUM_RENDER_VALUES = 192
# Caps on per-issue work for degenerate calls: only the first
# ``_MAX_ARGUMENT_ISSUES`` issues are displayed, so sorting/annotating vast
# unexpected-name sets or materializing every error dict of a huge
# ValidationError is wasted work — beyond these bounds the trailing
# issue COUNT degrades (label-only) or issues fall back to schema/kind
# comparison, but the formatter stays under the adversarial time ceiling.
_MAX_UNEXPECTED_NAMES = 4096
_MAX_MATERIALIZED_ERRORS = 50_000
# Entry cap for the display-safe arguments copy: every per-key pass over the
# mapping (exactness check, normalization, kind comparison) must stay bounded
# for degenerate multi-million-key calls. Entries beyond the cap are dropped
# from the DISPLAY view only (validation itself already ran on the full
# mapping); the copy is then marked incomplete so the echo guard suppresses
# detail rather than vouching for strings it never saw.
_MAX_SAFE_ARGUMENT_ENTRIES = 100_000

# Pydantic-core error types whose built-in ``msg`` is derived only from the
# schema or the parser, never from the argument payload. Any other type —
# including ``value_error`` / ``assertion_error`` and ``PydanticCustomError``
# codes — carries arbitrary validator text that may embed input values, so its
# ``msg`` must not reach the model-visible message. ``PydanticCustomError`` can
# spoof an allowlisted code, so allowlisted details additionally pass a
# best-effort payload-echo guard (exact string containment). A validator that
# deliberately transforms the payload to evade it is out of scope: validators
# are in-process tool-author code that can already emit arbitrary tool results.
_DETAIL_SAFE_ERROR_TYPES = frozenset(
    {
        "enum",
        "string_too_short",
        "string_too_long",
        "too_short",
        "too_long",
        "greater_than",
        "greater_than_equal",
        "less_than",
        "less_than_equal",
        "multiple_of",
        "finite_number",
        "string_pattern_mismatch",
        "int_parsing",
        "int_from_float",
        "float_parsing",
        "bool_parsing",
        "decimal_parsing",
        "json_invalid",
        "date_parsing",
        "datetime_parsing",
        "time_parsing",
        "timedelta_parsing",
    }
)


def _is_exact_type(value: Any, *expected: type) -> bool:
    """Identity-compare ``type(value)`` against ``expected``.

    ``type(value) in (int, float)`` compares by EQUALITY, which dispatches to
    a caller-controlled metaclass ``__eq__`` — user code that can lie about
    exactness, raise, or block mid-formatting. ``is`` never dispatches.
    """
    value_type = type(value)
    return any(value_type is candidate for candidate in expected)


def _bounded_text(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _sanitize_message_text(text: str) -> str:
    """Escape unprintable and lone-surrogate characters.

    ``json.dumps(..., ensure_ascii=False)`` passes lone surrogates and C1
    controls through raw; either would make the next request body
    non-UTF-8-encodable or inject terminal escapes, so the finished message
    gets one defensive pass.
    """
    if all(char == " " or char.isprintable() for char in text):
        return text
    return "".join(char if char == " " or char.isprintable() else ascii(char)[1:-1] for char in text)


def _bounded_schema_text(text: str) -> str:
    return _bounded_text(text, _MAX_SCHEMA_TEXT_CHARS)


def _schema_field_name(value: Any) -> str:
    if isinstance(value, str) and value.isidentifier():
        return value
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _resolve_local_schema_ref(schema: Mapping[str, Any], root_schema: Mapping[str, Any]) -> Mapping[str, Any]:
    """Resolve a local JSON-schema ref without following external resources."""
    current = schema
    visited: set[str] = set()
    while isinstance(ref := current.get("$ref"), str) and ref.startswith("#/") and ref not in visited:
        visited.add(ref)
        target: Any = root_schema
        for raw_part in ref[2:].split("/"):
            part = raw_part.replace("~1", "/").replace("~0", "~")
            if not isinstance(target, Mapping) or part not in target:
                return current
            target = target[part]
        if not isinstance(target, Mapping):
            return current
        current = target
    return current


def _schema_variants(schema: Mapping[str, Any], root_schema: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    resolved = _resolve_local_schema_ref(schema, root_schema)
    for key in ("anyOf", "oneOf", "allOf"):
        variants = resolved.get(key)
        if isinstance(variants, list):
            return [_resolve_local_schema_ref(item, root_schema) for item in variants if isinstance(item, Mapping)]
    return [resolved]


def _schema_type_text(
    schema: Mapping[str, Any],
    root_schema: Mapping[str, Any],
    *,
    depth: int = 0,
) -> str:
    """Render a compact, model-readable JSON-schema type expression."""
    if depth >= _MAX_SCHEMA_DEPTH:
        return "object" if schema.get("type") == "object" else "value"

    resolved = _resolve_local_schema_ref(schema, root_schema)
    for key in ("anyOf", "oneOf", "allOf"):
        variants = resolved.get(key)
        if isinstance(variants, list):
            rendered = [
                _schema_type_text(item, root_schema, depth=depth + 1) for item in variants if isinstance(item, Mapping)
            ]
            separator = " & " if key == "allOf" else " | "
            return _bounded_schema_text(separator.join(dict.fromkeys(rendered))) or "value"

    enum_values = resolved.get("enum")
    if isinstance(enum_values, list) and enum_values:
        return _bounded_schema_text(
            " | ".join(
                json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
                for value in islice(enum_values, _MAX_ENUM_RENDER_VALUES)
            )
        )
    if "const" in resolved:
        return _bounded_schema_text(
            json.dumps(resolved["const"], ensure_ascii=False, separators=(",", ":"), default=str)
        )

    schema_type = resolved.get("type")
    if isinstance(schema_type, list):
        return _bounded_schema_text(" | ".join(str(item) for item in schema_type))
    if schema_type == "array":
        prefix_items = resolved.get("prefixItems")
        if isinstance(prefix_items, list):
            rendered_items = [
                _schema_type_text(item, root_schema, depth=depth + 1)
                for item in prefix_items
                if isinstance(item, Mapping)
            ]
            return _bounded_schema_text("[" + ", ".join(rendered_items) + "]")
        items = resolved.get("items")
        item_text = _schema_type_text(items, root_schema, depth=depth + 1) if isinstance(items, Mapping) else "value"
        return _bounded_schema_text(f"[{item_text}]")
    if schema_type == "object" or isinstance(resolved.get("properties"), Mapping):
        properties = resolved.get("properties")
        if not isinstance(properties, Mapping) or not properties:
            additional = resolved.get("additionalProperties")
            if additional is False and not resolved.get("patternProperties"):
                return "{}"
            if isinstance(additional, Mapping):
                return _bounded_schema_text(
                    f"{{string: {_schema_type_text(additional, root_schema, depth=depth + 1)}}}"
                )
            return "object"
        typed_properties = cast("Mapping[str, Any]", properties)
        required = {name for name in resolved.get("required", []) if isinstance(name, str)}
        fields: list[str] = []
        for index, (name, field_schema) in enumerate(typed_properties.items()):
            if index >= _MAX_SCHEMA_FIELDS:
                fields.append(f"… +{len(typed_properties) - _MAX_SCHEMA_FIELDS} fields")
                break
            field_type = (
                _schema_type_text(field_schema, root_schema, depth=depth + 1)
                if isinstance(field_schema, Mapping)
                else "value"
            )
            optional = "" if name in required else "?"
            fields.append(f"{_schema_field_name(name)}{optional}: {field_type}")
            if sum(len(field) for field in fields) > _MAX_SCHEMA_TEXT_CHARS:
                fields.append("…")
                break
        return _bounded_schema_text("{" + ", ".join(fields) + "}")
    if isinstance(schema_type, str):
        return schema_type
    return "value"


@dataclass(frozen=True, slots=True)
class _UnionVariantMatch:
    """A union variant identified from a Pydantic branch marker."""

    variant: Mapping[str, Any]
    """The selected variant schema."""

    via_discriminator: bool
    """True for a ``discriminator.mapping`` tag match — authoritative, since
    Pydantic always writes the tag into ``loc`` for tagged-union sub-errors.
    False for a schema ``title`` match, which is only a heuristic."""


def _select_union_variant(
    union_schema: Mapping[str, Any],
    marker: Any,
    root_schema: Mapping[str, Any],
) -> _UnionVariantMatch | None:
    """Pick the union variant a Pydantic branch marker names, if identifiable.

    Primitive-type markers (``'str'``, ``'list[str]'``) match neither the
    discriminator mapping nor a title and return ``None`` — for those the
    caller's first-variant-with-member search is unambiguous because
    primitive variants have no members to collide on.
    """
    if isinstance(marker, str):
        mapping_keys = [marker]
    elif isinstance(marker, bool):
        mapping_keys = [str(marker), json.dumps(marker)]
    elif isinstance(marker, int | float):
        # Numeric discriminator values appear as numbers in ``loc`` but as
        # strings in the JSON-schema ``discriminator.mapping``. Boolean tags
        # surface as ``1``/``0`` in ``loc`` while the mapping uses
        # ``"True"``/``"False"``.
        mapping_keys = [str(marker)]
        if marker in (0, 1):
            mapping_keys.append(str(bool(marker)))
    else:
        return None
    discriminator = union_schema.get("discriminator")
    if isinstance(discriminator, Mapping):
        mapping = discriminator.get("mapping")
        if isinstance(mapping, Mapping):
            for key in mapping_keys:
                ref = mapping.get(key)
                if isinstance(ref, str):
                    resolved = _resolve_local_schema_ref({"$ref": ref}, root_schema)
                    if "$ref" not in resolved:
                        return _UnionVariantMatch(resolved, via_discriminator=True)
    if isinstance(marker, str):
        for variant in _schema_variants(union_schema, root_schema):
            if variant.get("title") == marker:
                return _UnionVariantMatch(variant, via_discriminator=False)
    return None


# Names Pydantic uses as union branch markers for untitled variants; used to
# tell a marker-shaped loc part from a plausible mapping key.
_MARKER_TYPE_NAMES = frozenset(
    {
        "str",
        "int",
        "float",
        "bool",
        "none",
        "list",
        "dict",
        "set",
        "frozenset",
        "tuple",
        "bytes",
        "bytearray",
        "date",
        "datetime",
        "time",
        "timedelta",
        "decimal",
        "complex",
        "enum",
        "literal",
        "json",
        "url",
        "uuid",
        "callable",
        "model",
        "typed-dict",
        "dataclass",
        "is-instance",
        "is-subclass",
    }
)
_MAX_LOCATION_STATES = 500
# Adversarial schemas can make one bad payload fan out into thousands of
# per-variant validation errors; the message is bounded to ~1200 chars anyway,
# so resolving locations past this point is pure wasted work.
_MAX_VALIDATION_ERRORS = 50

_NAV_DISCRIMINATOR_SELECT_WEIGHT = 4.0
_NAV_EXACT_WEIGHT = 3.0
_NAV_TITLE_SELECT_WEIGHT = 2.0
_NAV_MARKER_SKIP_WEIGHT = 1.5
_NAV_ADDITIONAL_WEIGHT = 1.0
_NAV_DESPERATE_SKIP_WEIGHT = 0.5
# Selection after a blind skip must lose to ANY full reading that avoids it —
# additive weights can't express that locally (a later exact move would
# subsidize the selection past a cheap additionalProperties chain), so the
# move carries a penalty larger than any full path can earn. Full consumption
# still ranks above every partial reading regardless, so the move remains the
# resolver of last resort it is meant to be.
_NAV_BLIND_SELECT_WEIGHT = -1000.0

# Per-node skip state carried through the location search. A generic skip
# spends the union level's one marker; whether variant selection remains
# plausible afterwards depends on what the skipped marker's text says it
# wrapped.
_SKIP_NONE = 0
_SKIP_OPAQUE = 1
_SKIP_UNION_MARKER = 2
_SKIP_BLIND = 3

# Wrapper reprs Pydantic writes WITHOUT the wrapped schema — the marker
# carries no evidence for or against a union underneath.
_SCHEMALESS_WRAPPER_PREFIXES = ("function-wrap[", "function-plain[")

# Corroboration cost bounds. Scanning is errors x names x marker-length work,
# so an adversarial 30k-char ``Tag`` marker over a wide union costs real time.
# Genuine union reprs list every member, so one of the first names collected
# matches immediately; parts longer than any genuine repr classify opaque
# (fail-safe: real-key readings win).
_MAX_MARKER_SCAN_CHARS = 4096
_MAX_CORROBORATION_NAMES = 40


def _is_union_node(resolved: Mapping[str, Any]) -> bool:
    return any(isinstance(resolved.get(key), list) for key in ("anyOf", "oneOf"))


def _is_identifier_char(char: str) -> bool:
    return char.isalnum() or char == "_"


def _name_corroborated(name: str, part: str) -> bool:
    """Whether ``name`` appears in ``part`` delimited like a type reference.

    Substring containment alone is spoofable — a variant titled ``B`` must
    not be corroborated by ``union[Business]`` — so the occurrence has to be
    bounded by non-identifier characters (``union[B,C]``, ``union[B]``).
    """
    start = 0
    while (index := part.find(name, start)) != -1:
        before = part[index - 1] if index else ""
        end = index + len(name)
        after = part[end] if end < len(part) else ""
        if not (before and _is_identifier_char(before)) and not (after and _is_identifier_char(after)):
            return True
        start = index + 1
    return False


def _variant_reference_names(node: Mapping[str, Any], root_schema: Mapping[str, Any]) -> list[str]:
    """Names the marker of a union at ``node`` could reference, depth <= 1.

    Pydantic writes CORE names into markers — the class name, which is also
    the ``$defs`` key — while the advertised JSON schema may carry customized
    ``title`` values, so both the ``$ref`` basenames and the resolved titles
    of every variant are candidates, plus (one nested-union level deep) the
    members and discriminator-mapping entries of a variant that is itself a
    union node.
    """
    names: list[str] = []

    def collect(item: Mapping[str, Any], depth: int) -> None:
        reference = item.get("$ref")
        if isinstance(reference, str) and reference:
            names.append(reference.rsplit("/", 1)[-1].replace("~1", "/").replace("~0", "~"))
        resolved = _resolve_local_schema_ref(item, root_schema)
        title = resolved.get("title")
        if isinstance(title, str) and title:
            names.append(title)
        if depth > 0:
            return
        discriminator = resolved.get("discriminator")
        if isinstance(discriminator, Mapping):
            mapping = discriminator.get("mapping")
            if isinstance(mapping, Mapping):
                for tag, target in mapping.items():
                    if isinstance(tag, str) and tag:
                        names.append(tag)
                    if isinstance(target, str) and target:
                        names.append(target.rsplit("/", 1)[-1].replace("~1", "/").replace("~0", "~"))
        for key in ("anyOf", "oneOf"):
            members = resolved.get(key)
            if not isinstance(members, list):
                continue
            for member in members:
                if isinstance(member, Mapping):
                    collect(member, depth + 1)

    for key in ("anyOf", "oneOf"):
        items = node.get(key)
        if not isinstance(items, list):
            continue
        for item in items:
            if isinstance(item, Mapping):
                collect(item, 0)
    return names


def _skip_state_for_marker(
    part: Any,
    node: Mapping[str, Any],
    root_schema: Mapping[str, Any],
) -> int:
    """Classify what selection rights survive skipping this marker.

    Schema generation can flatten a validator-wrapped union
    (``Annotated[Cat | Dog, AfterValidator(...)]``) into the containing
    ``anyOf``, in which case the flattened level's own marker follows the
    wrapper marker at the SAME schema node. The wrapper marker's text is the
    only evidence: ``_SKIP_UNION_MARKER`` requires it to both name a core
    union (``union[``) and mention, delimiter-bounded, a variant name
    reachable here (titles, ``$ref``/discriminator-mapping names, depth
    ``<=`` one nested union) — corroboration that defeats poisoned
    ``Tag("union[...")`` text naming no real variant. Wrapper reprs written
    without the wrapped schema (``function-wrap[fn()]``) prove nothing
    either way, so ``_SKIP_BLIND`` keeps selection possible at a penalty
    below every real-key reading. Everything else named a non-union, so no
    flattened level can follow: ``_SKIP_OPAQUE`` turns selection off.
    """
    # Length gate FIRST: the containment scan is O(len(part)), and a loc part
    # can be a caller-supplied mapping key of arbitrary size repeated across
    # every error of a wide union.
    if isinstance(part, str) and len(part) <= _MAX_MARKER_SCAN_CHARS and "union[" in part:
        for name in _variant_reference_names(node, root_schema)[:_MAX_CORROBORATION_NAMES]:
            if _name_corroborated(name, part):
                return _SKIP_UNION_MARKER
    if isinstance(part, str) and part.startswith(_SCHEMALESS_WRAPPER_PREFIXES):
        return _SKIP_BLIND
    return _SKIP_OPAQUE


def _marker_like(part: Any, variants: Sequence[Mapping[str, Any]]) -> bool:
    """Whether a loc part is shaped like a Pydantic union branch marker."""
    if not isinstance(part, str):
        # Numeric/boolean markers are discriminator tags; JSON object keys
        # are always strings.
        return True
    if len(part) > _MAX_MARKER_SCAN_CHARS:
        # No genuine Pydantic marker repr is this long — this is a real
        # (giant) input key, and the containment scans below are O(len).
        return False
    if "[" in part or "-" in part:
        # Parameterized or wrapped type expressions: ``dict[str,int]``,
        # ``function-after[...]``, ``lax-or-strict``.
        return True
    if part in _MARKER_TYPE_NAMES:
        return True
    return any(variant.get("title") == part for variant in variants)


def _resolve_error_location(
    root_schema: Mapping[str, Any],
    location: Sequence[Any],
    alias_to_property: Mapping[str, str] | None = None,
    location_display: Mapping[str, str] | None = None,
    loc_uses_field_names: bool = False,
) -> tuple[str, Mapping[str, Any] | None]:
    """Map a Pydantic error location to a display path and schema node.

    Pydantic inserts the union variant's tag or type name into ``loc`` for
    union errors (e.g. ``('pet', 'dog', 'barks')``) — but omits it in some
    shapes (nullable single-variant unions) — so a loc part at a union node is
    genuinely ambiguous between a branch marker and a real input key. The
    resolver runs a small best-first search over the readings and keeps the
    best-scoring one: full consumption of the loc beats falling off the
    schema, then summed move weights decide — authoritative variant selection
    (4: a discriminator-tag match, or a title match at a union with two or
    more non-null variants, where Pydantic always writes the marker; includes
    one nested-union level, e.g. an optional discriminated union) beats exact
    property/alias navigation (3), which beats title selection at a nullable
    single-variant union (2 — no marker is written there, so a title match is
    only a heuristic), which beats a generic marker skip (1.5 for
    marker-shaped parts, 0.5 as a last resort), which beats
    ``additionalProperties`` navigation (1 — it matches any string and must
    not swallow markers). The generic skip likewise requires at least two
    non-null variants and runs at most once per union node — except that a
    blind skip may be followed by one more skip of a part carrying its own
    marker evidence, because a schema-less wrapper can hide a second wrapper
    level. After a skip, Pydantic's one-marker-per-union-level rule says
    this node's marker is spent; what may still select here is classified by
    ``_skip_state_for_marker`` from the skipped marker's text. A marker that
    names a core union AND mentions, delimiter-bounded, a reachable variant
    name (corroboration — ``tagged-union[Cat,Dog]``,
    ``function-after[..., union[Cat,Dog]]``, matched against titles and
    ``$ref``/discriminator names since JSON titles may be customized) keeps
    selection available: direct selection demoted below exact property
    navigation (a variant's real key named like a sibling title wins),
    nested selection into an inner union node at full weight (its tag may
    follow the wrapper marker). A schema-less wrapper repr
    (``function-wrap[fn()]``) proves nothing, so selection survives only as
    a last resort: its penalty weight loses to every full reading that
    avoids it, while full consumption still beats any partial reading. Any
    other marker named a non-union, so no flattened level can follow and
    selection — nested included — is off; the following part must be a real
    key. The search runs a greedy
    first-choice dive to secure a full-path incumbent, then a best-first
    refinement ordered by an admissible optimistic bound: the result is never
    worse than the greedy reading, and provably optimal whenever the
    refinement completes within the state cap (a safety valve for
    adversarially wide schemas). Loc parts that used an accepted alias
    spelling navigate via ``alias_to_property`` but keep the spelling the
    model actually sent in the display path; top-level parts Pydantic spelled
    with a name validation does not accept are re-spelled via
    ``location_display``, and with ``loc_uses_field_names``
    (``loc_by_alias=False``) the field-name map takes precedence over a
    same-named sibling property. Both maps cover top-level fields only and
    are consulted only for the top-level part, so nested alias locs degrade
    to the containing object node.
    """
    best_key: tuple[int, float] = (-1, -1.0)
    best_display: list[Any] = list(location)
    best_node: Mapping[str, Any] | None = root_schema

    def offer(key: tuple[int, float], display: list[Any], node: Mapping[str, Any]) -> None:
        nonlocal best_key, best_display, best_node
        if key > best_key:
            best_key, best_display, best_node = key, display, node

    def expand(
        index: int,
        node: Mapping[str, Any],
        skip_state: int,
        display: list[Any],
    ) -> list[tuple[float, Mapping[str, Any], int, list[Any]]]:
        """All readings of ``location[index]`` as (weight, node, skip_state, display)."""
        part = location[index]
        resolved = _resolve_local_schema_ref(node, root_schema)
        variants = _schema_variants(resolved, root_schema)
        union = _is_union_node(resolved)
        non_null_variants = [variant for variant in variants if variant.get("type") != "null"]
        # Pydantic emits branch markers only when there is a real branch
        # choice; a nullable single-variant union gets none, so there a
        # marker-shaped part is a real key and a title match is a guess.
        markers_expected = len(non_null_variants) >= 2
        moves: list[tuple[float, Mapping[str, Any], int, list[Any]]] = []
        selected: _UnionVariantMatch | None = None
        if union and skip_state != _SKIP_OPAQUE:
            selected = _select_union_variant(resolved, part, root_schema)
            if selected is not None:
                if skip_state == _SKIP_UNION_MARKER:
                    # The skip spent this level's one marker, but its text
                    # named a flattened core union whose own marker may
                    # legitimately follow at this node. Demote the move below
                    # exact property navigation so a variant's real key named
                    # like a sibling title still wins.
                    select_weight = _NAV_TITLE_SELECT_WEIGHT
                elif skip_state == _SKIP_BLIND:
                    # The skipped wrapper proved nothing about what it
                    # wrapped; keep selection reachable but below EVERY
                    # real-key reading, additionalProperties included.
                    select_weight = _NAV_BLIND_SELECT_WEIGHT
                else:
                    select_weight = (
                        _NAV_DISCRIMINATOR_SELECT_WEIGHT
                        if selected.via_discriminator or markers_expected
                        else _NAV_TITLE_SELECT_WEIGHT
                    )
                moves.append((select_weight, selected.variant, _SKIP_NONE, display))
            for variant in variants:
                if not _is_union_node(variant):
                    continue
                nested = _select_union_variant(variant, part, root_schema)
                if nested is None or (selected is not None and nested.variant is selected.variant):
                    continue
                if skip_state == _SKIP_BLIND:
                    select_weight = _NAV_BLIND_SELECT_WEIGHT
                else:
                    # _SKIP_UNION_MARKER corroborated the nested union's own
                    # member titles, so the inner tag keeps full weight there
                    # just as with no skip at all.
                    nested_variants = _schema_variants(variant, root_schema)
                    nested_markers_expected = sum(1 for item in nested_variants if item.get("type") != "null") >= 2
                    select_weight = (
                        _NAV_DISCRIMINATOR_SELECT_WEIGHT
                        if nested.via_discriminator or nested_markers_expected
                        else _NAV_TITLE_SELECT_WEIGHT
                    )
                moves.append((select_weight, nested.variant, _SKIP_NONE, display))
        seen_children: list[Mapping[str, Any]] = []
        for variant in variants:
            candidates: list[tuple[Any, float]] = []
            if isinstance(part, str):
                properties = variant.get("properties")
                exact: Any = None
                if isinstance(properties, Mapping):
                    # The alias map covers top-level fields only; consulting
                    # it deeper could resolve to an unrelated sibling.
                    mapped = alias_to_property.get(part) if alias_to_property and index == 0 else None
                    if loc_uses_field_names and mapped is not None:
                        # ``loc_by_alias=False`` locs are field names; the map
                        # is authoritative over a same-named sibling property.
                        exact = properties.get(mapped)
                    if not isinstance(exact, Mapping):
                        exact = properties.get(part)
                    if not isinstance(exact, Mapping) and mapped is not None:
                        exact = properties.get(mapped)
                candidates.append((exact, _NAV_EXACT_WEIGHT))
                candidates.append((variant.get("additionalProperties"), _NAV_ADDITIONAL_WEIGHT))
            elif isinstance(part, int):
                prefix_items = variant.get("prefixItems")
                if isinstance(prefix_items, list) and part < len(prefix_items):
                    candidates.append((prefix_items[part], _NAV_EXACT_WEIGHT))
                else:
                    candidates.append((variant.get("items"), _NAV_EXACT_WEIGHT))
            for child, move_weight in candidates:
                if not isinstance(child, Mapping) or any(child is other for other in seen_children):
                    continue
                seen_children.append(child)
                shown = part
                if isinstance(part, str) and index == 0 and location_display:
                    shown = location_display.get(part, part)
                moves.append((move_weight, child, _SKIP_NONE, [*display, shown]))
        if union and skip_state in (_SKIP_NONE, _SKIP_BLIND) and markers_expected:
            next_skip_state = _skip_state_for_marker(part, resolved, root_schema)
            # From a blind skip a SECOND same-node skip is allowed only for a
            # part carrying its own marker evidence: the schema-less wrapper
            # may have hidden another wrapper level (``function-wrap[...]``
            # around ``Annotated[Cat | Dog, AfterValidator(...)]`` emits two
            # consecutive same-node wrapper markers). An evidence-free part
            # after a blind skip stays a selection/real-key question instead.
            if skip_state == _SKIP_NONE or next_skip_state != _SKIP_OPAQUE:
                skip_weight = _NAV_MARKER_SKIP_WEIGHT if _marker_like(part, variants) else _NAV_DESPERATE_SKIP_WEIGHT
                moves.append((skip_weight, resolved, next_skip_state, display))
        return moves

    # Phase 1: greedy first-choice dive. Costs at most ``len(location)``
    # expansions and secures a full-path incumbent whenever best-local moves
    # reach one, so adversarially wide schemas can't starve the result down
    # to a raw-marker path.
    index, node, skip_state, weight = 0, root_schema, _SKIP_NONE, 0.0
    display: list[Any] = []
    while True:
        if index == len(location):
            offer((1, weight), display, node)
            break
        moves = expand(index, node, skip_state, display)
        if not moves:
            offer((0, weight), [*display, *location[index:]], node)
            break
        move_weight, node, skip_state, display = max(moves, key=lambda move: move[0])
        weight += move_weight
        index += 1

    # Phase 2: best-first refinement. Heap entries: (-optimistic_bound,
    # insertion_order, index, node, skip_state, weight, display). The bound
    # is admissible (no move exceeds the authoritative-selection weight), so
    # the first fully-consumed state popped is the global optimum; the greedy
    # incumbent prunes everything it already dominates.
    counter = 0
    heap: list[tuple[float, int, int, Mapping[str, Any], int, float, list[Any]]] = [
        (-(len(location) * _NAV_DISCRIMINATOR_SELECT_WEIGHT), 0, 0, root_schema, _SKIP_NONE, 0.0, [])
    ]
    states = 0
    while heap:
        states += 1
        if states > _MAX_LOCATION_STATES:
            break
        negated_bound, _, index, node, skip_state, weight, display = heappop(heap)
        if best_key >= (1, -negated_bound):
            continue
        if index == len(location):
            offer((1, weight), display, node)
            break
        moves = expand(index, node, skip_state, display)
        if not moves:
            offer((0, weight), [*display, *location[index:]], node)
            continue
        for move_weight, next_node, skip_state_after, display_after in moves:
            counter += 1
            new_weight = weight + move_weight
            bound = new_weight + (len(location) - index - 1) * _NAV_DISCRIMINATOR_SELECT_WEIGHT
            heappush(heap, (-bound, counter, index + 1, next_node, skip_state_after, new_weight, display_after))
    return _format_argument_location(best_display), best_node


def _format_argument_location(location: Sequence[Any]) -> str:
    parts: list[str] = []
    for part in location:
        if isinstance(part, str) and len(part) > _MAX_IDENTIFIER_CHARS + 1:
            # The rendered path is bounded to ``_MAX_IDENTIFIER_CHARS`` below,
            # but the identifier check, JSON quoting, and join are all O(len)
            # — clamp a giant part (a caller-supplied mapping key, repeated in
            # every error's loc) before any of them run.
            part = part[: _MAX_IDENTIFIER_CHARS + 1]
        if isinstance(part, int):
            if parts:
                parts[-1] += f"[{part}]"
            else:
                parts.append(f"[{part}]")
        elif isinstance(part, str) and part.isidentifier():
            parts.append(part)
        else:
            parts.append(json.dumps(part, ensure_ascii=False, separators=(",", ":"), default=str))
    rendered = ".".join(parts) or "<arguments>"
    return _bounded_text(rendered, _MAX_IDENTIFIER_CHARS)


def _short_identifier(value: str) -> str:
    # A str-subclass input (e.g. a subclassed tool name) must not dispatch
    # ``split`` to user code mid-formatting.
    value = value if type(value) is str else str.__str__(value)
    return _bounded_text(" ".join(value.split()), _MAX_IDENTIFIER_CHARS)


def _quoted_identifier(value: str) -> str:
    shortened = _short_identifier(value)
    if shortened.isidentifier():
        return f"'{shortened}'"
    return json.dumps(shortened, ensure_ascii=False, separators=(",", ":"))


def _safe_type_name(value: object) -> str:
    """Class name of ``value`` without running user code.

    ``type(value).__name__`` dispatches through a custom metaclass's
    ``__getattribute__``, which can raise mid-formatting, and a dynamically
    created class can smuggle arbitrary payload text as its name. Read the
    slot through ``type``'s own descriptor and trust only identifier-shaped
    names.
    """
    try:
        name = type.__dict__["__name__"].__get__(type(value))
    except Exception:
        return "object"
    if not isinstance(name, str):
        return "object"
    if type(name) is not str:
        # A str-subclass name would re-enter user code via its methods.
        name = str.__str__(name)
    if name.isidentifier():
        return _bounded_text(name, _MAX_IDENTIFIER_CHARS)
    return "object"


def _json_value_kind(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, str):
        return "string"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, list | tuple):
        return "array"
    if isinstance(value, Mapping):
        return "object"
    return _safe_type_name(value)


def _bounded_error_detail(value: str) -> str:
    value = value if type(value) is str else str.__str__(value)
    normalized = " ".join(value.split()).removeprefix("Input should be ")
    return _bounded_text(normalized, _MAX_ERROR_DETAIL_CHARS)


def _collect_payload_strings(
    value: Any,
    out: set[str],
    depth: int = 0,
    _budget: list[int] | None = None,
    _seen: set[int] | None = None,
) -> bool:
    """Collect string values from the argument payload for the echo guard.

    Returns False whenever any part of the payload went unverified — budget
    truncation, an unexpanded subtree past the depth cap, or any non-exact
    value. Only EXACT JSON-adjacent types are traversed (including dict KEYS
    and set members): a subclass or foreign object can smuggle strings where
    the scan cannot safely look (attributes, hooks), and its iteration /
    ``len`` / ``hash`` dispatch to user code that can raise or block — so it
    marks the scan incomplete instead of being touched at all; the scan
    itself never runs user code. Work is charged by size (hashing large
    strings), and a repeated reference to one string object is processed
    once (identity memo), so shared references cannot amplify the cost.
    """
    if _budget is None:
        _budget = [_MAX_PAYLOAD_SCAN_ITEMS]
    if _seen is None:
        _seen = set()
    if type(value) is str:
        if id(value) in _seen:
            return True
        _seen.add(id(value))
        cost = 1 + len(value) // 1024
        if _budget[0] < cost:
            return False
        _budget[0] -= cost
        if len(value) >= 3:
            out.add(value)
        return True
    if _is_exact_type(value, int, float, bool) or value is None:
        return True
    if type(value) is dict:
        items: Any = (element for pair in value.items() for element in pair)
    elif _is_exact_type(value, list, tuple, set, frozenset):
        items = value
    else:
        return False
    if depth >= 6:
        # An unexpanded subtree may hold payload strings.
        return False
    for item in items:
        if _budget[0] <= 0:
            return False
        _budget[0] -= 1
        if not _collect_payload_strings(item, out, depth + 1, _budget, _seen):
            return False
    return True


def _matches_json_type(value: Any, schema_type: str) -> bool:
    match schema_type:
        case "string":
            return isinstance(value, str)
        case "integer":
            # As ``tools._matches_json_schema_type``: ``3.0`` is an integer.
            return (isinstance(value, int) and not isinstance(value, bool)) or (
                isinstance(value, float) and value.is_integer()
            )
        case "number":
            return isinstance(value, int | float) and not isinstance(value, bool)
        case "boolean":
            return isinstance(value, bool)
        case "array":
            return isinstance(value, list)
        case "object":
            return isinstance(value, Mapping)
        case "null":
            return value is None
        case _:
            return True


def _all_exact_json_tree(value: Any, depth: int = 0, _budget: list[int] | None = None) -> bool:
    """Whether ``value`` is built solely from exact JSON types (bounded walk).

    Equality over such a tree never leaves CPython's C slots; any subclass or
    foreign object anywhere in it could dispatch a comparison to user code,
    so it must not be equality-compared in the formatting path. The walk
    spends at most ``_MAX_COMPARE_ITEMS`` node visits TOTAL — shared
    references amplify a small graph into exponentially many visits, so a
    per-level size cap is not enough.
    """
    if _budget is None:
        _budget = [_MAX_COMPARE_ITEMS]
    if _is_exact_type(value, str, int, float, bool) or value is None:
        return True
    if depth >= _MAX_SCHEMA_DEPTH:
        return False
    if _is_exact_type(value, list, tuple):
        items: Any = value
    elif type(value) is dict:
        items = value.values()
    else:
        return False
    if len(value) > _budget[0]:
        # Also bounds the key scan below on repeated visits of a shared node.
        return False
    if type(value) is dict and not all(type(key) is str for key in value):
        return False
    for item in items:
        _budget[0] -= 1
        if _budget[0] < 0:
            return False
        if not _all_exact_json_tree(item, depth + 1, _budget):
            return False
    return True


def _comparison_safe_value(value: Any) -> tuple[bool, Any]:
    """``(comparable, safe_value)`` for equality against schema constants.

    Exact JSON scalars compare via C slots; subclass scalars are copied
    through their base slots (never running overridden methods); exact
    containers qualify only when every element does. Anything else is not
    comparable here — the caller accepts it (fail-safe: no issue beats a
    crash or a mislabel).
    """
    if _is_exact_type(value, str, int, float, bool) or value is None:
        return True, value
    if isinstance(value, str):
        return True, str.__str__(value)
    if isinstance(value, int):
        # bool is final, so an int instance here is a plain-int subclass.
        return True, int.__index__(value)
    if isinstance(value, float):
        return True, float.__float__(value)
    if _all_exact_json_tree(value):
        return True, value
    return False, None


def _schema_accepts_value(
    value: Any,
    schema: Mapping[str, Any],
    root_schema: Mapping[str, Any],
    *,
    depth: int = 0,
) -> bool:
    if depth >= _MAX_SCHEMA_DEPTH:
        return True
    resolved = _resolve_local_schema_ref(schema, root_schema)
    enum_values = resolved.get("enum")
    has_const = "const" in resolved
    if isinstance(enum_values, list) or has_const:
        comparable, safe_value = _comparison_safe_value(value)
        if comparable:
            if isinstance(enum_values, list) and safe_value not in enum_values:
                return False
            if has_const and safe_value != resolved["const"]:
                return False
    for key in ("anyOf", "oneOf", "allOf"):
        variants = resolved.get(key)
        if not isinstance(variants, list):
            continue
        matches = [
            _schema_accepts_value(value, item, root_schema, depth=depth + 1)
            for item in variants
            if isinstance(item, Mapping)
        ]
        if key == "allOf":
            return all(matches)
        if key == "oneOf":
            return sum(matches) == 1
        return any(matches)
    schema_type = resolved.get("type")
    if isinstance(schema_type, str):
        return _matches_json_type(value, schema_type)
    if isinstance(schema_type, list):
        return any(isinstance(item, str) and _matches_json_type(value, item) for item in schema_type)
    return True


def _schema_value_issues(
    arguments: Mapping[str, Any],
    schema: Mapping[str, Any],
) -> list[str]:
    properties = schema.get("properties")
    if not isinstance(properties, Mapping):
        return []
    issues: list[str] = []
    # Only the first ``_MAX_ARGUMENT_ISSUES`` issues render with text (the
    # rest fold into a trailing count), so scanning a bounded prefix of a
    # degenerate huge mapping merely degrades that count, never the shown
    # issues.
    for name, value in islice(arguments.items(), _MAX_UNEXPECTED_NAMES):
        field_schema = properties.get(name)
        if not isinstance(field_schema, Mapping) or _schema_accepts_value(value, field_schema, schema):
            continue
        issues.append(
            f"{_format_argument_location((name,))}: expected {_schema_type_text(field_schema, schema)}, "
            f"got {_json_value_kind(value)}"
        )
    return issues


def _rejects_unexpected_arguments(schema: Mapping[str, Any], *, typed_model_dump: bool) -> bool:
    """Return whether this tool schema rejects unadvertised top-level keys."""
    additional = schema.get("additionalProperties")
    if additional is True or isinstance(additional, Mapping):
        return False
    if additional is False:
        return True
    # A composed root (e.g. ``RootModel[dict[str, int] | None]`` emitting
    # ``anyOf``) accepts keys its top-level ``properties`` can't enumerate.
    if any(key in schema for key in ("anyOf", "oneOf", "allOf", "$ref")):
        return False
    return typed_model_dump and isinstance(schema.get("properties"), Mapping)


def _field_spellings(input_model: type[BaseModel] | None) -> dict[str, set[str]]:
    """Map each typed-model field to every top-level spelling validation accepts.

    Mirrors Pydantic's acceptance rules: with an alias, the field name only
    counts under ``validate_by_name``/``populate_by_name``, and aliases only
    count under ``validate_by_alias`` (default on). A spelling Pydantic would
    silently ignore (``extra="ignore"`` + default) must NOT be accepted here —
    the pre-check error is the only signal the model gets that its key was
    dropped.
    """
    if input_model is None:
        return {}
    config = input_model.model_config
    # An explicit ``validate_by_name`` wins over the deprecated
    # ``populate_by_name`` (Pydantic ignores the latter when both are set).
    validate_by_name_setting = config.get("validate_by_name")
    if validate_by_name_setting is None:
        validate_by_name_setting = config.get("populate_by_name")
    validate_by_name = bool(validate_by_name_setting)
    validate_by_alias = config.get("validate_by_alias", True) is not False
    spellings: dict[str, set[str]] = {}
    for field_name, field_info in input_model.model_fields.items():
        alias_source = field_info.validation_alias if field_info.validation_alias is not None else field_info.alias
        names: set[str] = set()
        if validate_by_alias and alias_source is not None:
            if isinstance(alias_source, str):
                names.add(alias_source)
            elif isinstance(alias_source, AliasPath):
                if alias_source.path and isinstance(alias_source.path[0], str):
                    names.add(alias_source.path[0])
            elif isinstance(alias_source, AliasChoices):
                for choice in alias_source.choices:
                    if isinstance(choice, str):
                        names.add(choice)
                    elif isinstance(choice, AliasPath) and choice.path and isinstance(choice.path[0], str):
                        names.add(choice.path[0])
        if alias_source is None or validate_by_name:
            names.add(field_name)
        spellings[field_name] = names or {field_name}
    return spellings


def _accepted_argument_names(
    schema: Mapping[str, Any],
    input_model: type[BaseModel] | None,
) -> set[str]:
    """Top-level names the tool accepts.

    For typed models the spellings map is authoritative — schema property
    names are rendered by-alias even under ``validate_by_alias=False``, where
    Pydantic silently ignores the alias key, so adding them back would
    reintroduce the silent-default trap.
    """
    if input_model is None:
        properties = schema.get("properties")
        if not isinstance(properties, Mapping):
            return set()
        return set(cast("Mapping[str, Any]", properties))
    names: set[str] = set()
    for field_names in _field_spellings(input_model).values():
        names |= field_names
    return names


_CORE_SCHEMA_WRAPPER_TYPES = frozenset({"function-before", "function-wrap", "function-plain"})
_CORE_SCHEMA_STATIC_TERMINAL_TYPES = frozenset({"model-fields", "typed-dict", "dataclass-args"})
_CORE_SCHEMA_CHILD_KEYS: dict[str, tuple[str, ...]] = {
    "function-after": ("schema",),
    "default": ("schema",),
    "nullable": ("schema",),
    "custom-error": ("schema",),
    "json-or-python": ("json_schema", "python_schema"),
    "lax-or-strict": ("lax_schema", "strict_schema"),
}
_MAX_CORE_SCHEMA_DEPTH = 8


def _core_schema_may_rewrite_keys(
    node: object,
    definitions: Mapping[str, Mapping[str, Any]] | None = None,
    depth: int = 0,
) -> bool:
    """True unless the chain provably terminates in static field validation.

    Only the model's top-level chain is walked — field schemas under the
    terminal fields node are never entered, so nested models and field-level
    validators can't trigger a stand-down. Anything unrecognized (including
    branch containers like ``json-or-python``) counts as rewriting unless
    every branch is proven static: standing down too eagerly merely skips the
    name pre-check while Pydantic stays authoritative, whereas missing a
    rewriting wrapper falsely rejects valid calls.
    """
    if depth > _MAX_CORE_SCHEMA_DEPTH or not isinstance(node, Mapping):
        return True
    schema_node = cast("Mapping[str, Any]", node)
    node_type = schema_node.get("type")
    if node_type in _CORE_SCHEMA_STATIC_TERMINAL_TYPES:
        return False
    if node_type in _CORE_SCHEMA_WRAPPER_TYPES:
        return True
    if node_type == "model":
        # A custom ``__init__`` participates in validation and can rewrite
        # argument keys before field validation sees them.
        if schema_node.get("custom_init"):
            return True
        return _core_schema_may_rewrite_keys(schema_node.get("schema"), definitions, depth + 1)
    if node_type == "definitions":
        collected = dict(definitions or {})
        definition_nodes = schema_node.get("definitions", ())
        for definition in cast("Sequence[object]", definition_nodes):
            if isinstance(definition, Mapping):
                definition_map = cast("Mapping[str, Any]", definition)
                ref = definition_map.get("ref")
                if isinstance(ref, str):
                    collected[ref] = definition_map
        return _core_schema_may_rewrite_keys(schema_node.get("schema"), collected, depth + 1)
    if node_type == "definition-ref":
        target = (definitions or {}).get(schema_node.get("schema_ref"))
        return _core_schema_may_rewrite_keys(target, definitions, depth + 1)
    child_keys = _CORE_SCHEMA_CHILD_KEYS.get(node_type)
    if child_keys is None:
        return True
    return any(_core_schema_may_rewrite_keys(schema_node.get(key), definitions, depth + 1) for key in child_keys)


def _accepts_arbitrary_argument_names(input_model: type[BaseModel] | None) -> bool:
    """True when top-level validation may rewrite argument keys.

    Such logic can accept and transform names that appear in neither the field
    spellings nor the schema, so name membership cannot be judged statically
    and the pre-check must stand down. A class-level ``model_validate``
    override is checked first — the loop validates through that method, and
    an override rewrites keys invisibly to every schema-level signal. Beyond
    that, detected from the core schema — the model's top-level chain must
    provably end in static field validation, which covers
    ``model_validator(mode="before"/"wrap")``, the legacy
    ``root_validator(pre=True)``, and ``__get_pydantic_core_schema__``
    overrides alike — with the decorator registries as a fallback for deferred
    core schemas. Field-level validators can't rename top-level keys and don't
    count.
    """
    if input_model is None:
        return False
    base_validate = getattr(BaseModel.model_validate, "__func__", BaseModel.model_validate)
    model_validate = getattr(input_model.model_validate, "__func__", None)
    if model_validate is not base_validate:
        # The loop validates through ``model_validate``; a class-level
        # override can rewrite argument keys before Pydantic ever sees them,
        # invisible to both the core schema and the decorator registries.
        return True
    try:
        core_schema: Any = input_model.__pydantic_core_schema__
    except Exception:
        core_schema = None
    if core_schema is not None and _core_schema_may_rewrite_keys(core_schema):
        return True
    decorators = getattr(input_model, "__pydantic_decorators__", None)
    for attribute in ("model_validators", "root_validators"):
        validators = getattr(decorators, attribute, None) or {}
        if any(
            getattr(getattr(validator, "info", None), "mode", None) in ("before", "wrap")
            for validator in validators.values()
        ):
            return True
    return False


@dataclass(frozen=True, slots=True)
class _TypedModelNaming:
    """Name bookkeeping for a typed model whose spellings diverge from the schema."""

    spellings: dict[str, set[str]]
    """Per-field accepted argument spellings."""

    schema_names: dict[str, str]
    """Each field's schema property name."""

    alias_to_property: dict[str, str]
    """Loc spelling → schema property, for location navigation and missing dedup."""

    property_display: dict[str, str]
    """Schema property → accepted spelling, when the schema advertises a name
    validation does not accept (e.g. ``validate_by_alias=False``)."""

    location_display: dict[str, str]
    """Loc spelling → accepted spelling, for loc parts Pydantic reports with a
    name validation does not accept (bare field names under
    ``loc_by_alias=False``)."""

    loc_uses_field_names: bool
    """``loc_by_alias=False``: error locations use bare field names, making the
    loc → property map authoritative over same-named sibling properties."""


def _typed_model_naming(
    input_model: type[BaseModel] | None,
    property_names: Sequence[str],
) -> _TypedModelNaming:
    """Field-name bookkeeping for typed models whose spellings diverge from the schema.

    ``AliasPath`` fields are left out of the display map — their flat schema
    name can't be replaced by the nested alias root without misstating the
    shape.
    """
    spellings = _field_spellings(input_model)
    schema_names: dict[str, str] = {}
    alias_to_property: dict[str, str] = {}
    property_display: dict[str, str] = {}
    location_display: dict[str, str] = {}
    loc_uses_field_names = input_model is not None and input_model.model_config.get("loc_by_alias", True) is False
    naming = _TypedModelNaming(
        spellings, schema_names, alias_to_property, property_display, location_display, loc_uses_field_names
    )
    if input_model is None:
        return naming
    serialization_schema = input_model.model_config.get("json_schema_mode_override") == "serialization"
    for field_name, field_info in input_model.model_fields.items():
        alias_source = field_info.validation_alias if field_info.validation_alias is not None else field_info.alias
        has_alias_path = isinstance(alias_source, AliasPath)
        validation_candidates: list[str] = []
        if isinstance(alias_source, str):
            validation_candidates.append(alias_source)
        elif isinstance(alias_source, AliasChoices):
            for choice in alias_source.choices:
                if isinstance(choice, str):
                    validation_candidates.append(choice)
                else:
                    has_alias_path = has_alias_path or isinstance(choice, AliasPath)
        # Candidates must follow the schema's generation mode: a
        # serialization-mode schema (``json_schema_mode_override``) renders
        # serialization aliases and never validation aliases, and vice versa.
        # Trying both per-field would let one field's validation alias claim
        # another field's serialization-alias property. Serialization aliases
        # are schema-name candidates only — never accepted spellings.
        if serialization_schema:
            mode_candidates = [field_info.serialization_alias, field_info.alias]
        else:
            mode_candidates = [*validation_candidates, field_info.alias]
        candidates: list[str] = []
        for candidate in mode_candidates:
            if isinstance(candidate, str) and candidate not in candidates:
                candidates.append(candidate)
        candidates.append(field_name)
        schema_name = next((name for name in candidates if name in property_names), field_name)
        schema_names[field_name] = schema_name
        field_spellings = spellings.get(field_name, {field_name})
        if not loc_uses_field_names:
            for spelling in field_spellings:
                if spelling != schema_name:
                    alias_to_property.setdefault(spelling, schema_name)
        if schema_name not in field_spellings and not has_alias_path:
            property_display[schema_name] = field_name if field_name in field_spellings else min(field_spellings)
    if loc_uses_field_names:
        # ``loc_by_alias=False``: every error location uses the bare field
        # name, so field-name entries are authoritative — alias spellings
        # never appear in locs and would only create collisions.
        for field_name in input_model.model_fields:
            schema_name = schema_names[field_name]
            if field_name != schema_name:
                alias_to_property[field_name] = schema_name
            display = property_display.get(schema_name, schema_name)
            if display != field_name:
                location_display[field_name] = display
    else:
        # ``loc_by_alias=True`` locs use accepted spellings, but bare field
        # names can still surface for fields matched by name; map those too
        # when unambiguous. Applied last with ``setdefault`` so accepted
        # spellings win any collision with another field's spelling entries.
        accepted_spellings = {spelling for names in spellings.values() for spelling in names}
        for field_name in input_model.model_fields:
            if field_name in accepted_spellings:
                continue
            schema_name = schema_names[field_name]
            alias_to_property.setdefault(field_name, schema_name)
            display = property_display.get(schema_name, schema_name)
            if display != field_name:
                location_display.setdefault(field_name, display)
    return naming


def _missing_required_names(
    required: Sequence[str],
    arguments: Mapping[str, Any],
    spellings: Mapping[str, set[str]],
    schema_names: Mapping[str, str],
) -> list[str]:
    """Required schema names with no accepted spelling present in the arguments.

    A required name is matched to its field by schema name, field name, or
    spelling — the schema key falls back to the field name for non-string
    validation aliases (e.g. ``AliasPath``), whose accepted spelling is the
    alias root instead.
    """
    missing: list[str] = []
    for name in required:
        if name in arguments:
            continue
        satisfied = any(
            (name == field_name or name == schema_names.get(field_name) or name in field_names)
            and any(spelling in arguments for spelling in field_names)
            for field_name, field_names in spellings.items()
        )
        if not satisfied:
            missing.append(name)
    return missing


def _argument_key_display(name: object) -> str:
    """Safe display text for a non-string mapping key.

    Never calls an arbitrary ``__repr__``: a custom key type's repr is user
    code that may raise (escaping the loop's ``TypeError | ValidationError``
    handler and aborting the turn), run unboundedly, or embed value content
    (a tuple key) the no-echo policy would otherwise never show. Only exact
    JSON-adjacent scalar types render their (safe, bounded) repr; everything
    else degrades to a type placeholder.
    """
    if type(name) is int and name.bit_length() > _MAX_INT_KEY_BITS:
        return "<int key>"
    if _is_exact_type(name, bool, int, float) or name is None:
        try:
            return repr(name)
        except ValueError:
            # Defensive backstop behind the magnitude bound above.
            pass
    return f"<{_safe_type_name(name)} key>"


def _display_safe_arguments(arguments: Mapping[str, Any]) -> tuple[Mapping[str, Any], bool]:
    """``(safe, complete)``: a copy of ``arguments`` whose keys are exact ``str``.

    Mapping-form calls from custom clients are not runtime-checked, so keys
    can be str subclasses or arbitrary objects whose ``__hash__`` /
    ``__eq__`` / ``__lt__`` / ``split`` are user code; left in place they
    would run inside membership probes, ``sorted``, and the close-match
    suggester and could raise past the loop's ``TypeError | ValidationError``
    handler. Non-string keys take their placeholder display form (they are
    always unexpected); colliding normalized keys keep the first value.
    Copying stops at ``_MAX_SAFE_ARGUMENT_ENTRIES`` so degenerate
    multi-million-key calls stay bounded; ``complete`` is False when any
    entry was dropped — past the cap OR because normalization collapsed two
    distinct caller keys — since strings in a dropped entry are unverified
    and the payload scan must not vouch for detail text it never saw.
    """
    if (
        type(arguments) is dict
        and len(arguments) <= _MAX_SAFE_ARGUMENT_ENTRIES
        and all(type(name) is str for name in arguments)
    ):
        return arguments, True
    safe: dict[str, Any] = {}
    complete = True
    for count, (name, value) in enumerate(arguments.items()):
        if count >= _MAX_SAFE_ARGUMENT_ENTRIES:
            return safe, False
        if type(name) is str:
            key = name
        elif isinstance(name, str):
            # The base slot copies without running subclass methods.
            key = str.__str__(name)
        else:
            key = _argument_key_display(name)
        if key in safe:
            complete = False
        else:
            safe[key] = value
    return safe, complete


def _unexpected_argument_names(
    arguments: Mapping[str, Any],
    schema: Mapping[str, Any],
    *,
    reject_unexpected: bool,
    input_model: type[BaseModel] | None = None,
) -> list[str]:
    """List top-level argument keys that the tool does not accept.

    Always returns exact strings: JSON-decoded keys are strings, but
    mapping-form calls from custom clients are not runtime-checked, and a
    hostile key must surface as an argument error rather than crash
    ``sorted`` or the close-match suggester downstream.
    """
    if not reject_unexpected:
        return []
    accepted = _accepted_argument_names(schema, input_model)
    names: list[str] = []
    for visited, name in enumerate(arguments):
        if len(names) >= _MAX_UNEXPECTED_NAMES:
            # Stop collecting, not just displaying: exact-str dict keys are
            # unique, so at most ``len(accepted)`` of them skip this list and
            # the walk stays bounded no matter how many keys the caller sends.
            break
        if visited >= _MAX_SAFE_ARGUMENT_ENTRIES:
            # Only a flood of distinct str-SUBCLASS keys whose base text all
            # normalizes to an accepted name can reach this many visits
            # without filling ``names`` (a legitimate exact-str mapping breaks
            # above within cap + len(accepted) visits). Stand down: the
            # pre-check is an enhancement over Pydantic's own validation,
            # which still runs on the full mapping, so truncating the scan
            # never accepts anything Pydantic itself would reject.
            break
        if type(name) is str:
            if name not in accepted:
                names.append(name)
        elif isinstance(name, str):
            text = str.__str__(name)
            if text not in accepted:
                names.append(text)
        else:
            names.append(_argument_key_display(name))
    return sorted(names)


def _capped_validation_errors(exception: ValidationError) -> list[ErrorDetails]:
    """At most ``_MAX_VALIDATION_ERRORS`` errors, breadth-first across top-level fields.

    A single wide union can fan one bad value out into thousands of
    per-variant errors that Pydantic lists contiguously, so a plain prefix
    slice would spend every slot on duplicates of one field and drop another
    field's only error. Round-robin over the errors grouped by their loc's
    first part keeps at least one error per argument (up to the cap) while
    preserving Pydantic's order for fields and within each field.
    """
    errors = exception.errors(include_url=False, include_context=False)
    for error in errors:
        location = error.get("loc")
        # Clamp giant string parts BEFORE anything hashes them: pydantic
        # materializes a DISTINCT copy of a mapping key per error, so
        # per-object hash caching never amortizes — every schema-map lookup
        # in the resolver would pay O(len) per error. Truncation only
        # degrades resolution for parts no genuine marker or sane property
        # name can reach (the marker scans and display already gate there).
        # Clamp to one char PAST the marker gate: a clamped part must stay in
        # the oversized class, or truncation would launder an oversized
        # marker into one eligible for corroboration (pinned opaque).
        if isinstance(location, tuple) and any(
            isinstance(part, str) and len(part) > _MAX_MARKER_SCAN_CHARS + 1 for part in location
        ):
            error["loc"] = tuple(
                part[: _MAX_MARKER_SCAN_CHARS + 1]
                if isinstance(part, str) and len(part) > _MAX_MARKER_SCAN_CHARS + 1
                else part
                for part in location
            )
    if len(errors) <= _MAX_VALIDATION_ERRORS:
        return errors
    groups: dict[object, list[ErrorDetails]] = {}
    for error in errors:
        location = error.get("loc")
        head = location[0] if isinstance(location, tuple) and location else None
        groups.setdefault(head, []).append(error)
    capped: list[ErrorDetails] = []
    depth = 0
    while len(capped) < _MAX_VALIDATION_ERRORS:
        progressed = False
        for bucket in groups.values():
            if depth >= len(bucket):
                continue
            capped.append(bucket[depth])
            progressed = True
            if len(capped) == _MAX_VALIDATION_ERRORS:
                break
        if not progressed:
            break
        depth += 1
    return capped


def _argument_validation_message(
    *,
    tool_name: str,
    arguments: Mapping[str, Any],
    arguments_unparseable: bool,
    schema: Mapping[str, Any],
    exception: TypeError | ValidationError,
    reject_unexpected: bool,
    input_model: type[BaseModel] | None = None,
) -> str:
    """Build a concise validation error that lets the model repair its next call."""
    arguments, arguments_complete = _display_safe_arguments(arguments)
    issues: list[str] = []
    properties = schema.get("properties")
    property_names = [name for name in properties if isinstance(name, str)] if isinstance(properties, Mapping) else []
    naming = _typed_model_naming(input_model, property_names)

    if arguments_unparseable:
        issues.append("arguments must be a valid JSON object")
    else:
        unexpected = _unexpected_argument_names(
            arguments,
            schema,
            reject_unexpected=reject_unexpected,
            input_model=input_model,
        )
        suggestion_names = sorted(_accepted_argument_names(schema, input_model)) if input_model else property_names
        for index, name in enumerate(unexpected):
            # Close matches are only ever displayed for the first
            # ``_MAX_ARGUMENT_ISSUES`` issues; later entries just add to the
            # trailing count, so skipping their suggestions changes nothing
            # visible while bounding difflib work on vast unknown sets.
            suggestions = (
                get_close_matches(name, suggestion_names, n=1, cutoff=0.6) if index < _MAX_ARGUMENT_ISSUES else []
            )
            suggestion = f" (did you mean {_quoted_identifier(suggestions[0])}?)" if suggestions else ""
            issues.append(f"unknown {_quoted_identifier(name)}{suggestion}")

        required = [name for name in schema.get("required", []) if isinstance(name, str)]
        # A before/wrap validator may populate required fields from rewritten
        # keys, so static missing detection would report fields the validator
        # filled; Pydantic's own ``missing`` errors are authoritative there.
        missing_top_level = (
            []
            if _accepts_arbitrary_argument_names(input_model)
            else _missing_required_names(required, arguments, naming.spellings, naming.schema_names)
        )
        missing_top_level_set = set(missing_top_level)
        issues.extend(
            f"missing {_quoted_identifier(naming.property_display.get(name, name))}" for name in missing_top_level
        )

        if isinstance(exception, ValidationError) and exception.error_count() <= _MAX_MATERIALIZED_ERRORS:
            payload_strings: set[str] = set()
            try:
                # An entry-capped display copy never saw the dropped entries'
                # strings, so its scan can only ever be incomplete.
                payload_scan_complete = arguments_complete and _collect_payload_strings(arguments, payload_strings)
            except Exception:
                # A hostile container's iteration hooks can raise mid-scan; an
                # unverified payload set must suppress detail text rather than
                # risk echoing it.
                payload_scan_complete = False
            for error in _capped_validation_errors(exception):
                error_type = error.get("type")
                location = error.get("loc")
                if not isinstance(location, tuple):
                    continue
                location_text, expected_schema = _resolve_error_location(
                    schema,
                    location,
                    naming.alias_to_property,
                    naming.location_display,
                    naming.loc_uses_field_names,
                )
                if error_type == "missing":
                    # Compare through the spelling → schema-name map so a
                    # Pydantic loc using the accepted spelling dedups against
                    # the schema-derived missing entry.
                    top_level = location[0] if len(location) == 1 else None
                    if isinstance(top_level, str):
                        top_level = naming.alias_to_property.get(top_level, top_level)
                    if top_level not in missing_top_level_set:
                        issues.append(f"missing '{location_text}'")
                    continue
                if error_type == "extra_forbidden":
                    issues.append(f"unknown '{location_text}'")
                    continue
                expected = (
                    _schema_type_text(expected_schema, schema)
                    if isinstance(expected_schema, Mapping)
                    else "valid value"
                )
                if error_type == "literal_error":
                    issues.append(f"{location_text}: expected {expected}")
                elif isinstance(error_type, str) and error_type.endswith("_type"):
                    issues.append(f"{location_text}: expected {expected}, got {_json_value_kind(error.get('input'))}")
                else:
                    detail = (
                        error.get("msg") if payload_scan_complete and error_type in _DETAIL_SAFE_ERROR_TYPES else None
                    )
                    if isinstance(detail, str) and any(value in detail for value in payload_strings):
                        # Best-effort guard: a spoofed allowlisted code must not
                        # echo payload text verbatim.
                        detail = None
                    suffix = f" ({_bounded_error_detail(detail)})" if isinstance(detail, str) and detail else ""
                    issues.append(f"{location_text}: expected {expected}{suffix}")
        else:
            issues.extend(_schema_value_issues(arguments, schema))

    # Union errors repeat per variant; after branch-marker filtering those
    # collapse to identical issue lines.
    issues = list(dict.fromkeys(issues))
    if not issues:
        issues.append("arguments did not match the tool schema")

    issue_text = "; ".join(issues[:_MAX_ARGUMENT_ISSUES])
    if len(issues) > _MAX_ARGUMENT_ISSUES:
        issue_text += f"; … and {len(issues) - _MAX_ARGUMENT_ISSUES} more issue(s)"

    display_schema: Mapping[str, Any] = schema
    if naming.property_display and isinstance(properties, Mapping):
        # Advertise the spellings validation actually accepts, not schema
        # names the pre-check would reject right back.
        display_schema = {
            **schema,
            "properties": {naming.property_display.get(name, name): value for name, value in properties.items()},
        }
        required_names = schema.get("required")
        if isinstance(required_names, list):
            display_schema["required"] = [
                naming.property_display.get(name, name) if isinstance(name, str) else name for name in required_names
            ]
    message = (
        f"Invalid arguments for {_quoted_identifier(tool_name)}: {issue_text}. "
        f"Expected: {_schema_type_text(display_schema, schema)}."
    )
    return _bounded_text(_sanitize_message_text(message), _MAX_MESSAGE_CHARS)
