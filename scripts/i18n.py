# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Extract, update, compile, check, and pseudo-localize Chrys catalogs.

The Python ``fallback=`` literals are the sole English source of truth.  Each
POT/PO entry carries deterministic Babel auto-comments (``#.`` records):

``English: <prose>`` / ``English-plural: <prose>``
    Translator-facing English fallback text, refreshed by ``update``.  These
    lines are NEVER parsed back: Babel wraps every comment at 76 columns even
    when ``write_po`` is told not to wrap (mirroring xgettext), so prose
    comments are lossy across a read/write round-trip.

``chrys-meta=<json>``
    One compact JSON object — the SHA-256 ``fingerprint`` of the ordered
    singular/plural fallback pair, the boolean ``multiline`` policy, and the
    sorted non-count ``placeholders`` schema.  This line is the machine-read
    source-freshness identity, and it must remain a single whitespace-free
    token: the comment wrapper splits only at whitespace, so a spaceless line
    is the one shape guaranteed to survive the round-trip intact.

Extraction is AST-based and scans only ``src/chrys``.  Babel is deliberately
limited to PO/MO file IO.
"""

from __future__ import annotations

import argparse
import ast
import gettext
import hashlib
import io
import json
import os
import sys
import tempfile
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from babel.messages.catalog import Catalog, Message
from babel.messages.mofile import write_mo
from babel.messages.pofile import PoFileError, denormalize, read_po, write_po

from chrys import __version__
from chrys.foundation.i18n import msg as build_message_definition

if __package__:
    from scripts.i18n_mo import SOURCE_MO_PATH, compile_mo
else:  # run as ``python scripts/i18n.py``: only scripts/ is on sys.path
    from i18n_mo import SOURCE_MO_PATH, compile_mo
from chrys.foundation.i18n.formatting import (
    _normalized_lookup_candidate,
    has_visible_content,
    parse_placeholder_names,
    validate_authored_template,
)

DOMAIN = "chrys"
PSEUDO_LOCALE = "en-XA"
PSEUDO_CATALOG_LOCALE = "en_XA"
ZH_HANS_LOCALE = "zh_Hans"
REPO_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPO_ROOT / "src" / "chrys"
POT_PATH = REPO_ROOT / "locales" / f"{DOMAIN}.pot"
ZH_PO_PATH = REPO_ROOT / "locales" / "zh-Hans" / "LC_MESSAGES" / f"{DOMAIN}.po"
ZH_MO_PATH = REPO_ROOT / SOURCE_MO_PATH

_CATALOG_DATE = datetime(2026, 1, 1, tzinfo=UTC)
_ENGLISH_SINGULAR_PREFIX = "English:"
_ENGLISH_PLURAL_PREFIX = "English-plural:"
_META_COMMENT_PREFIX = "chrys-meta="
_POT_HEADER = (
    "# Chrys translation template.\n"
    "# English fallbacks are generated metadata; edit the Python msg() definition instead.\n"
)
_ZH_HEADER = "# Simplified Chinese translations for Chrys.\n"
_PSEUDO_ACCENTS = str.maketrans(
    {
        "A": "Å",
        "B": "Ɓ",
        "C": "Ç",
        "D": "Ð",
        "E": "É",
        "F": "Ƒ",
        "G": "Ğ",
        "H": "Ĥ",
        "I": "Î",
        "J": "Ĵ",
        "K": "Ķ",
        "L": "Ŀ",
        "M": "Ṁ",
        "N": "Ñ",
        "O": "Ö",
        "P": "Þ",
        "Q": "Ǫ",
        "R": "Ŕ",
        "S": "Š",
        "T": "Ţ",
        "U": "Û",
        "V": "Ṽ",
        "W": "Ŵ",
        "X": "Ẋ",
        "Y": "Ý",
        "Z": "Ž",
        "a": "å",
        "b": "ƀ",
        "c": "ç",
        "d": "ð",
        "e": "é",
        "f": "ƒ",
        "g": "ğ",
        "h": "ĥ",
        "i": "î",
        "j": "ĵ",
        "k": "ķ",
        "l": "ŀ",
        "m": "ṁ",
        "n": "ñ",
        "o": "ö",
        "p": "þ",
        "q": "ǫ",
        "r": "ŕ",
        "s": "š",
        "t": "ţ",
        "u": "û",
        "v": "ṽ",
        "w": "ŵ",
        "x": "ẋ",
        "y": "ý",
        "z": "ž",
    }
)


class CatalogToolError(ValueError):
    """The catalog source or an artifact violates the i18n contract."""


@dataclass(frozen=True, slots=True)
class ExtractedMessage:
    """One validated module-level ``msg(...)`` definition."""

    key: str
    fallback: str
    plural_fallback: str | None
    multiline: bool
    placeholders: frozenset[str]
    locations: tuple[tuple[str, int], ...]

    @property
    def fingerprint(self) -> str:
        """Return the stable fallback-pair fingerprint."""
        payload = json.dumps(
            [self.fallback, self.plural_fallback],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    @property
    def plural(self) -> bool:
        """Return whether the definition has a plural fallback."""
        return self.plural_fallback is not None


@dataclass(frozen=True, slots=True)
class SourceMetadata:
    """Source identity decoded from the machine-read PO auto-comment."""

    fingerprint: str
    multiline: bool
    placeholders: frozenset[str]


def extract_messages(
    source_root: Path = SOURCE_ROOT,
    *,
    location_root: Path | None = None,
) -> tuple[ExtractedMessage, ...]:
    """Extract and validate definitions from Python files below *source_root*."""
    source_root = Path(source_root)
    if location_root is None:
        location_root = REPO_ROOT if source_root.resolve() == SOURCE_ROOT.resolve() else source_root

    parsed: list[ExtractedMessage] = []
    for path in sorted(source_root.rglob("*.py")):
        try:
            source = path.read_text(encoding="utf-8")
            if not _may_name_msg(source):
                continue
            tree = ast.parse(source, filename=path.as_posix())
        except (OSError, SyntaxError, UnicodeError) as error:
            raise CatalogToolError(f"Could not parse catalog source {path}: {error}") from error
        parsed.extend(_extract_file(path, tree, location_root=location_root))

    _reject_duplicate_keys(parsed)
    _validate_lookup_id_collisions(parsed)
    validated = tuple(_validate_extracted_message(message) for message in parsed)
    return tuple(sorted(validated, key=lambda message: message.key))


def _may_name_msg(source: str) -> bool:
    """Return whether *source* can spell the name ``msg``; a file that cannot defines no message.

    Most source files never mention msg, so extraction skips them unparsed.
    Python reads identifiers NFKC-normalized, so a non-ASCII spelling of msg
    still counts.
    """
    return "msg" in (source if source.isascii() else unicodedata.normalize("NFKC", source))


def _extract_file(path: Path, tree: ast.Module, *, location_root: Path) -> list[ExtractedMessage]:
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "msg"
    ]
    if not calls:
        # Every check below is about a msg() call; a file without one defines nothing.
        return []
    parents = {id(child): parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    location = _relative_location(path, location_root)
    canonical_import_ordinal = _canonical_msg_import_ordinal(tree)
    # Ordinals, not line numbers: semicolon-joined statements share a line
    # while still executing strictly in statement order.
    top_level_ordinal = {
        id(descendant): index for index, statement in enumerate(tree.body) for descendant in ast.walk(statement)
    }
    shadowed = _msg_binding_shadowed(tree)
    extracted: list[ExtractedMessage] = []
    for node in calls:
        if canonical_import_ordinal is None or top_level_ordinal.get(id(node), -1) < canonical_import_ordinal:
            # A bare msg() call keyed on the name alone may be an unrelated
            # local callable; extracting it would forge catalog entries.  A
            # call textually before the import raises NameError at runtime
            # while extraction would still record it.
            raise CatalogToolError(
                f"{location}:{node.lineno}: msg() calls require a preceding canonical "
                "'from chrys.foundation.i18n import msg' import"
            )
        if shadowed:
            # def/class/assignment shadows make the runtime binding ambiguous
            # even though the canonical import is present.
            raise CatalogToolError(
                f"{location}:{node.lineno}: msg() calls are ambiguous because msg is rebound in this module"
            )
        if not _is_module_assignment_value(node, parents, tree):
            raise CatalogToolError(
                f"{location}:{node.lineno}: msg() definitions must be direct module-level assignment values"
            )
        key, fallback, plural_fallback, multiline = _parse_msg_call(node, location)
        extracted.append(
            ExtractedMessage(
                key=key,
                fallback=fallback,
                plural_fallback=plural_fallback,
                multiline=multiline,
                placeholders=frozenset(),
                locations=((location, node.lineno),),
            )
        )
    return extracted


def _canonical_msg_import_ordinal(tree: ast.Module) -> int | None:
    # Only an unconditional top-level import counts: a conditional canonical
    # import can lose to a rogue same-name binding at runtime while the
    # extractor still records the call.
    for index, node in enumerate(tree.body):
        if (
            isinstance(node, ast.ImportFrom)
            and node.level == 0
            and node.module == "chrys.foundation.i18n"
            and any(alias.name == "msg" and alias.asname is None for alias in node.names)
        ):
            return index
    return None


def _msg_binding_shadowed(tree: ast.Module) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name == "msg":
            return True
        if isinstance(node, ast.Name) and node.id == "msg" and isinstance(node.ctx, (ast.Store, ast.Del)):
            return True
        if isinstance(node, ast.arg) and node.arg == "msg":
            return True
        if isinstance(node, (ast.MatchAs, ast.MatchStar)) and node.name == "msg":
            return True
        if isinstance(node, ast.MatchMapping) and node.rest == "msg":
            return True
        # ExceptHandler.name is a plain string, not a Name node, so the
        # Store/Del check above never sees `except ... as msg`.
        if isinstance(node, ast.ExceptHandler) and node.name == "msg":
            return True
        if isinstance(node, ast.Import) and any(
            # An unaliased plain import binds its ROOT component, so
            # ``import msg.submodule`` rebinds msg.
            (alias.asname or alias.name.split(".", maxsplit=1)[0]) == "msg"
            for alias in node.names
        ):
            return True
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if (alias.asname or alias.name) != "msg":
                    continue
                canonical = (
                    node.level == 0
                    and node.module == "chrys.foundation.i18n"
                    and alias.name == "msg"
                    and alias.asname is None
                )
                if not canonical:
                    return True
    return False


def _is_module_assignment_value(
    call: ast.Call,
    parents: dict[int, ast.AST],
    tree: ast.Module,
) -> bool:
    parent = parents.get(id(call))
    if isinstance(parent, ast.Assign):
        return (
            parent.value is call
            and len(parent.targets) == 1
            and isinstance(parent.targets[0], ast.Name)
            and parent in tree.body
        )
    if isinstance(parent, ast.AnnAssign):
        return parent.value is call and isinstance(parent.target, ast.Name) and parent in tree.body
    return False


def _parse_msg_call(call: ast.Call, location: str) -> tuple[str, str, str | None, bool]:
    if any(isinstance(argument, ast.Starred) for argument in call.args) or len(call.args) > 1:
        raise CatalogToolError(f"{location}:{call.lineno}: msg() requires one literal key argument")

    keywords: dict[str, ast.expr] = {}
    for keyword in call.keywords:
        if keyword.arg is None or keyword.arg in keywords:
            raise CatalogToolError(f"{location}:{call.lineno}: msg() does not accept expanded or duplicate keywords")
        keywords[keyword.arg] = keyword.value
    unknown = set(keywords) - {"key", "fallback", "plural_fallback", "multiline"}
    if unknown:
        raise CatalogToolError(
            f"{location}:{call.lineno}: msg() has unsupported keyword(s): {', '.join(sorted(unknown))}"
        )

    if call.args and "key" in keywords:
        raise CatalogToolError(f"{location}:{call.lineno}: msg() key was supplied more than once")
    key_node = call.args[0] if call.args else keywords.pop("key", None)
    if key_node is None:
        raise CatalogToolError(f"{location}:{call.lineno}: msg() requires a literal key")
    fallback_node = keywords.get("fallback")
    if fallback_node is None:
        raise CatalogToolError(f"{location}:{call.lineno}: msg() requires a literal fallback")

    key = _literal_string(key_node, "key", location, call.lineno)
    fallback = _literal_string(fallback_node, "fallback", location, call.lineno)
    plural_node = keywords.get("plural_fallback")
    if plural_node is None or (isinstance(plural_node, ast.Constant) and plural_node.value is None):
        plural_fallback = None
    else:
        plural_fallback = _literal_string(plural_node, "plural_fallback", location, call.lineno)

    multiline_node = keywords.get("multiline")
    if multiline_node is None:
        multiline = False
    elif isinstance(multiline_node, ast.Constant) and type(multiline_node.value) is bool:
        multiline = multiline_node.value
    else:
        raise CatalogToolError(f"{location}:{call.lineno}: multiline must be a literal bool")
    return key, fallback, plural_fallback, multiline


def _literal_string(node: ast.expr, name: str, location: str, lineno: int) -> str:
    if not isinstance(node, ast.Constant) or type(node.value) is not str:
        raise CatalogToolError(f"{location}:{lineno}: msg() {name} must be a literal string")
    return node.value


def _relative_location(path: Path, location_root: Path) -> str:
    try:
        return path.relative_to(location_root).as_posix()
    except ValueError:
        return path.as_posix()


def _reject_duplicate_keys(messages: Sequence[ExtractedMessage]) -> None:
    owners: dict[str, tuple[str, int]] = {}
    for message in messages:
        location = message.locations[0]
        previous = owners.get(message.key)
        if previous is not None:
            raise CatalogToolError(
                f"Duplicate msg() key {message.key!r} at {previous[0]}:{previous[1]} and {location[0]}:{location[1]}"
            )
        owners[message.key] = location


def _validate_lookup_id_collisions(messages: Sequence[ExtractedMessage]) -> None:
    owners: dict[str, str] = {}
    for message in messages:
        lookup_ids = [message.key]
        if message.plural:
            lookup_ids.append(f"{message.key}#plural")
        for lookup_id in lookup_ids:
            previous = owners.get(lookup_id)
            if previous is not None and previous != message.key:
                raise CatalogToolError(
                    f"Catalog lookup ID {lookup_id!r} collides between keys {previous!r} and {message.key!r}"
                )
            owners[lookup_id] = message.key


def _validate_extracted_message(message: ExtractedMessage) -> ExtractedMessage:
    location, lineno = message.locations[0]
    try:
        # Lone-surrogate literals are valid Python but crash Babel's UTF-8
        # writers downstream; UnicodeEncodeError is a ValueError.
        message.key.encode("utf-8")
        message.fallback.encode("utf-8")
        if message.plural_fallback is not None:
            message.plural_fallback.encode("utf-8")
        singular_names = parse_placeholder_names(message.fallback)
        validate_authored_template(message.fallback, multiline=message.multiline)
        if not has_visible_content(message.fallback):
            raise ValueError("fallback must have visible content")

        if message.plural_fallback is None:
            if "count" in singular_names:
                raise ValueError("the count placeholder requires plural_fallback")
            plural_names: frozenset[str] = frozenset()
        else:
            plural_names = parse_placeholder_names(message.plural_fallback)
            validate_authored_template(message.plural_fallback, multiline=message.multiline)
            if not has_visible_content(message.plural_fallback):
                raise ValueError("plural_fallback must have visible content")
            if singular_names - {"count"} != plural_names - {"count"}:
                raise ValueError("fallback and plural_fallback must share their non-count placeholders")

        build_message_definition(
            message.key,
            fallback=message.fallback,
            plural_fallback=message.plural_fallback,
            multiline=message.multiline,
        )
    except (TypeError, ValueError) as error:
        raise CatalogToolError(f"{location}:{lineno}: invalid msg() definition: {error}") from error

    return ExtractedMessage(
        key=message.key,
        fallback=message.fallback,
        plural_fallback=message.plural_fallback,
        multiline=message.multiline,
        placeholders=singular_names - {"count"},
        locations=message.locations,
    )


def build_template_catalog(messages: Sequence[ExtractedMessage]) -> Catalog:
    """Build the deterministic key-based POT catalog."""
    catalog = _new_catalog(locale=None, header_comment=_POT_HEADER)
    for message in messages:
        catalog.add(
            _message_id(message),
            locations=message.locations,
            auto_comments=_metadata_comments(message),
        )
    return catalog


def _new_catalog(*, locale: str | None, header_comment: str) -> Catalog:
    return Catalog(
        locale=locale,
        domain=DOMAIN,
        header_comment=header_comment,
        project="Chrys",
        version=__version__,
        copyright_holder="Chrys",
        msgid_bugs_address="",
        creation_date=_CATALOG_DATE,
        revision_date=_CATALOG_DATE if locale is not None else None,
        last_translator="Chrys translators" if locale is not None else None,
        language_team="Simplified Chinese" if locale == ZH_HANS_LOCALE else None,
        charset="utf-8",
        fuzzy=False,
    )


def _message_id(message: ExtractedMessage) -> str | tuple[str, str]:
    if message.plural:
        return message.key, f"{message.key}#plural"
    return message.key


def _metadata_comments(message: ExtractedMessage) -> tuple[str, ...]:
    meta = json.dumps(
        {
            "fingerprint": message.fingerprint,
            "multiline": message.multiline,
            "placeholders": sorted(message.placeholders),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    comments = [f"{_ENGLISH_SINGULAR_PREFIX} {message.fallback}"]
    if message.plural_fallback is not None:
        comments.append(f"{_ENGLISH_PLURAL_PREFIX} {message.plural_fallback}")
    comments.append(f"{_META_COMMENT_PREFIX}{meta}")
    return tuple(comments)


def extract_catalog(
    *,
    source_root: Path = SOURCE_ROOT,
    pot_path: Path = POT_PATH,
    location_root: Path | None = None,
) -> tuple[ExtractedMessage, ...]:
    """Extract source definitions and replace the generated POT."""
    messages = extract_messages(source_root, location_root=location_root)
    _write_po(Path(pot_path), build_template_catalog(messages))
    return messages


def update_catalog(
    *,
    source_root: Path = SOURCE_ROOT,
    po_path: Path = ZH_PO_PATH,
    location_root: Path | None = None,
) -> tuple[ExtractedMessage, ...]:
    """Merge fresh source metadata into the maintained zh-Hans PO."""
    messages = extract_messages(source_root, location_root=location_root)
    template = build_template_catalog(messages)
    po_path = Path(po_path)
    if po_path.exists():
        catalog = _read_po(po_path)
        _assert_zh_header(catalog, po_path)
        previous_fingerprints = _catalog_fingerprints(catalog)
        catalog.update(
            template,
            no_fuzzy_matching=True,
            update_header_comment=False,
            keep_user_comments=True,
            update_creation_date=False,
        )
    else:
        catalog = _new_catalog(locale=ZH_HANS_LOCALE, header_comment=_ZH_HEADER)
        previous_fingerprints = {}
        catalog.update(template, no_fuzzy_matching=True, update_creation_date=False)

    catalog.fuzzy = False
    # Babel's update() merges messages but keeps the existing header's
    # Project-Id-Version; sync it so a version bump propagates to the PO
    # (and therefore matches the MO, which is always stamped fresh).
    catalog.project = template.project
    catalog.version = template.version
    fresh_by_key = {message.key: message for message in messages}
    for message in _active_messages(catalog):
        key = _key_from_message(message)
        previous = previous_fingerprints.get(key)
        fresh = fresh_by_key[key]
        if key in previous_fingerprints and previous != fresh.fingerprint:
            message.flags.add("fuzzy")
    _write_po(po_path, catalog)
    return messages


def check_catalogs(
    *,
    source_root: Path = SOURCE_ROOT,
    pot_path: Path = POT_PATH,
    po_path: Path = ZH_PO_PATH,
    location_root: Path | None = None,
) -> tuple[tuple[ExtractedMessage, ...], Catalog]:
    """Run the non-mutating source/POT/PO freshness and PO preflight gate."""
    messages = extract_messages(source_root, location_root=location_root)
    pot = _read_po(Path(pot_path))
    _assert_catalog_fresh(messages, pot, label="POT", require_untranslated=True)
    po = _read_po(Path(po_path))
    _assert_zh_header(po, Path(po_path))
    _assert_catalog_fresh(messages, po, label="zh-Hans PO", require_untranslated=False)
    validate_translation_catalog(messages, po)
    return messages, po


def _assert_catalog_fresh(
    expected: Sequence[ExtractedMessage],
    catalog: Catalog,
    *,
    label: str,
    require_untranslated: bool,
) -> None:
    # The mutating commands always stamp the current package version, so a
    # header left behind by a version bump is stale by definition.
    if (catalog.project, catalog.version) != ("Chrys", __version__):
        raise CatalogToolError(
            f"{label} header Project-Id-Version is stale: "
            f"found {catalog.project!r} {catalog.version!r}, expected 'Chrys' {__version__!r}"
        )
    actual: dict[str, Message] = {}
    for message in _active_messages(catalog):
        if message.context is not None:
            raise CatalogToolError(f"{label} contains forbidden msgctxt for {_key_from_message(message)!r}")
        key = _key_from_message(message)
        if key in actual:
            raise CatalogToolError(f"{label} contains duplicate key {key!r}")
        actual[key] = message

    expected_by_key = {message.key: message for message in expected}
    missing = sorted(set(expected_by_key) - set(actual))
    extra = sorted(set(actual) - set(expected_by_key))
    if missing or extra:
        raise CatalogToolError(f"{label} key set is stale (missing={missing}, extra={extra})")

    for key, extracted in expected_by_key.items():
        message = actual[key]
        if message.id != _message_id(extracted):
            raise CatalogToolError(f"{label} plural lookup shape is stale for {key!r}")
        metadata = _parse_metadata(message, label=label)
        expected_metadata = SourceMetadata(
            fingerprint=extracted.fingerprint,
            multiline=extracted.multiline,
            placeholders=extracted.placeholders,
        )
        if metadata != expected_metadata:
            raise CatalogToolError(f"{label} source metadata is stale for {key!r}")
        # ``#:`` comments name the defining FILE only: line numbers would make
        # every edit above a ``msg()`` regenerate both catalogs and conflict
        # between branches. A definition moving to another module still goes
        # stale without any key, fingerprint or placeholder changing, and a
        # leftover ``path:line`` spelling is not what the writer emits.
        expected_locations = tuple((path, None) for path, _lineno in extracted.locations)
        if tuple(message.locations) != expected_locations:
            raise CatalogToolError(
                f"{label} source locations are stale for {key!r}: "
                f"found {tuple(message.locations)}, expected {expected_locations}"
            )
        if require_untranslated and any(has_visible_content(form) for form in _translation_forms(message)):
            raise CatalogToolError(f"{label} must not contain translations for {key!r}")


def _parse_metadata(message: Message, *, label: str) -> SourceMetadata:
    # English prose may legally contain the metadata prefix, and Babel's
    # 76-column comment wrapping can spill such a fragment onto its own
    # comment line.  Only lines decoding to the full canonical object count
    # as machine metadata; everything else is documentation debris.
    key = _key_from_message(message)
    candidates = [
        metadata
        for line in message.auto_comments
        if line.startswith(_META_COMMENT_PREFIX)
        and (metadata := _decode_metadata(line[len(_META_COMMENT_PREFIX) :])) is not None
    ]
    if len(candidates) != 1:
        raise CatalogToolError(f"{label} entry {key!r} has missing or duplicate Chrys source metadata")
    return candidates[0]


def _decode_metadata(payload: str) -> SourceMetadata | None:
    try:
        data = json.loads(payload)
    except ValueError:
        return None
    if type(data) is not dict or set(data) != {"fingerprint", "multiline", "placeholders"}:
        return None
    fingerprint = data["fingerprint"]
    multiline = data["multiline"]
    placeholders = data["placeholders"]

    valid = (
        type(fingerprint) is str
        and len(fingerprint) == 64
        and set(fingerprint) <= set("0123456789abcdef")
        and type(multiline) is bool
        and type(placeholders) is list
        and all(type(name) is str for name in placeholders)
        and placeholders == sorted(set(placeholders))
    )
    if not valid:
        return None
    # Only the writer's exact compact encoding counts: any other spelling
    # (spaces, extra keys) re-wraps differently on the next Babel write and
    # would corrupt the very line this parser just accepted.
    if json.dumps(data, sort_keys=True, separators=(",", ":")) != payload:
        return None
    return SourceMetadata(
        fingerprint=fingerprint,
        multiline=multiline,
        placeholders=frozenset(placeholders),
    )


def _catalog_fingerprints(catalog: Catalog) -> dict[str, str | None]:
    fingerprints: dict[str, str | None] = {}
    for message in _active_messages(catalog):
        try:
            fingerprints[_key_from_message(message)] = _parse_metadata(message, label="existing PO").fingerprint
        except CatalogToolError:
            fingerprints[_key_from_message(message)] = None
    return fingerprints


def _assert_zh_header(catalog: Catalog, path: Path) -> None:
    locale_name = str(catalog.locale) if catalog.locale is not None else ""
    if locale_name != ZH_HANS_LOCALE:
        raise CatalogToolError(f"zh-Hans PO must declare Language: {ZH_HANS_LOCALE}")
    if catalog.num_plurals != 1 or catalog.plural_expr != "0":
        raise CatalogToolError("zh-Hans PO must declare nplurals=1; plural=0;")
    # Babel infers both values from ``Language: zh_Hans`` alone, so the
    # semantic pin above cannot notice a deleted Plural-Forms line — while
    # external gettext tools reading the same file would fall back to their
    # own plural defaults.
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as error:
        raise CatalogToolError(f"Could not read catalog {path}: {error}") from error
    if _physical_header_lines(lines, '"Plural-Forms:') != ['"Plural-Forms: nplurals=1; plural=0;\\n"']:
        raise CatalogToolError(
            "zh-Hans PO must physically contain exactly one 'Plural-Forms: nplurals=1; plural=0;' header line"
        )
    if _physical_header_lines(lines, '"Content-Type:') != ['"Content-Type: text/plain; charset=utf-8\\n"']:
        # Babel synthesizes a utf-8 default when the declaration is missing,
        # while GNU msgfmt rejects the headerless file.
        raise CatalogToolError(
            "zh-Hans PO must physically contain exactly one 'Content-Type: text/plain; charset=utf-8' header line"
        )


def _physical_header_lines(lines: Sequence[str], prefix: str) -> list[str]:
    # Scan only the header entry's own lines: a multiline translation may
    # legally contain the identical text, so a file-wide scan would let a
    # deleted header masquerade behind a translated copy.
    in_header = False
    collected: list[str] = []
    for raw_line in lines:
        line = raw_line.strip()
        if not in_header:
            if line == 'msgid ""':
                in_header = True
            continue
        if not line.startswith("msgstr") and not line.startswith('"'):
            break
        if line.startswith(prefix):
            collected.append(line)
    return collected


def validate_translation_catalog(messages: Sequence[ExtractedMessage], catalog: Catalog) -> None:
    """Validate active translations before either checking or compiling."""
    extracted_by_key = {message.key: message for message in messages}
    lookup_ids = {
        lookup_id
        for extracted in messages
        for lookup_id in ((extracted.key, f"{extracted.key}#plural") if extracted.plural else (extracted.key,))
    }
    for message in _active_messages(catalog):
        key = _key_from_message(message)
        extracted = extracted_by_key.get(key)
        if extracted is None:
            continue
        forms = _translation_forms(message)
        for form in forms:
            _validate_translation_safety(form, multiline=extracted.multiline, key=key)
            if _normalized_lookup_candidate(form) in lookup_ids:
                raise CatalogToolError(f"Translation for {key!r} must not equal any catalog lookup ID")
            # The shared compiler keeps every form with a non-whitespace
            # character (it cannot measure terminal width in the build
            # environment), so a non-blank form must also be visible.
            if form.strip() and not has_visible_content(form):
                raise CatalogToolError(
                    f"Translation for {key!r} has no visible content; leave it empty to keep it untranslated"
                )

        visibility = [has_visible_content(form) for form in forms]
        if extracted.plural:
            if any(visibility) and not all(visibility):
                raise CatalogToolError(f"Partially translated plural entry {key!r} is forbidden")
            if len(forms) != catalog.num_plurals:
                raise CatalogToolError(
                    f"Plural entry {key!r} has {len(forms)} forms; catalog requires {catalog.num_plurals}"
                )
            effective = all(visibility)
        else:
            if len(forms) != 1:
                raise CatalogToolError(f"Singular entry {key!r} has an invalid translation shape")
            effective = visibility[0]

        if message.fuzzy or not effective:
            continue
        for form in forms:
            try:
                names = validate_authored_template(form, multiline=extracted.multiline)
            except (TypeError, ValueError) as error:
                raise CatalogToolError(f"Translation for {key!r} is invalid: {error}") from error
            if names - {"count"} != extracted.placeholders:
                raise CatalogToolError(f"Translation for {key!r} does not match the source placeholder schema")
            if not extracted.plural and "count" in names:
                raise CatalogToolError(f"Singular translation for {key!r} cannot use the count placeholder")


def _validate_translation_safety(template: str, *, multiline: bool, key: str) -> None:
    escaped_braces = template.replace("{", "{{").replace("}", "}}")
    try:
        validate_authored_template(escaped_braces, multiline=multiline)
    except (TypeError, ValueError) as error:
        raise CatalogToolError(f"Translation for {key!r} violates catalog text safety: {error}") from error


def compile_catalog(
    *,
    source_root: Path = SOURCE_ROOT,
    pot_path: Path = POT_PATH,
    po_path: Path = ZH_PO_PATH,
    mo_path: Path = ZH_MO_PATH,
    location_root: Path | None = None,
) -> None:
    """Validate the source/PO state and atomically replace the generated MO.

    The compile step itself is ``i18n_mo.compile_mo``, which the wheel build
    hook runs too, so both write the same MO for the same PO.
    """
    check_catalogs(
        source_root=source_root,
        pot_path=pot_path,
        po_path=po_path,
        location_root=location_root,
    )
    try:
        compile_mo(Path(po_path), Path(mo_path), verify=_validate_mo)
    except (OSError, EOFError, PoFileError, SyntaxError, ValueError) as error:
        if isinstance(error, CatalogToolError):
            raise
        raise CatalogToolError(f"Could not compile {mo_path}: {error}") from error


def _validate_mo(path: Path) -> None:
    try:
        with path.open("rb") as stream:
            translations = gettext.GNUTranslations(stream)
    except (OSError, EOFError, LookupError, SyntaxError, ValueError) as error:
        raise CatalogToolError("The temporary MO artifact is not loadable by stdlib gettext") from error
    plural_forms = translations.info().get("plural-forms", "")
    fields = [field.strip() for field in plural_forms.split(";") if field.strip()]
    if fields != ["nplurals=1", "plural=0"]:
        raise CatalogToolError("The temporary MO artifact has invalid zh-Hans plural metadata")


def generate_pseudo_catalog(
    output: Path,
    *,
    source_root: Path = SOURCE_ROOT,
    repo_root: Path = REPO_ROOT,
    location_root: Path | None = None,
) -> Path:
    """Generate a loadable en-XA MO tree outside the repository."""
    resolved_output = Path(output).resolve()
    resolved_repo = Path(repo_root).resolve()
    if _is_inside_repository(resolved_output, resolved_repo):
        raise CatalogToolError("Pseudo-catalog output must be outside the repository root")

    messages = extract_messages(source_root, location_root=location_root)
    catalog = _new_catalog(
        locale=PSEUDO_CATALOG_LOCALE,
        header_comment="# Mechanically generated Chrys pseudo translations.\n",
    )
    for message in messages:
        if message.plural_fallback is None:
            catalog.add(message.key, string=pseudo_localize(message.fallback, multiline=message.multiline))
        else:
            catalog.add(
                (message.key, f"{message.key}#plural"),
                (
                    pseudo_localize(message.fallback, multiline=message.multiline),
                    pseudo_localize(message.plural_fallback, multiline=message.multiline),
                ),
            )

    target = resolved_output / PSEUDO_LOCALE / "LC_MESSAGES" / f"{DOMAIN}.mo"
    try:
        # Re-resolve the full target BEFORE creating directories: a descendant
        # of an outside output root may itself be a symlink back into the
        # repository, and mkdir(parents=True) would follow it.
        final_target = target.resolve()
        if _is_inside_repository(final_target, resolved_repo):
            raise CatalogToolError("Pseudo-catalog output must be outside the repository root")
        final_target.parent.mkdir(parents=True, exist_ok=True)
        # Write a fresh sibling inode and swap it in with os.replace: writing
        # the target in place would stream bytes through any pre-existing
        # hardlink it may share with a repository file.
        temporary_target = final_target.with_name(f"{final_target.name}.tmp")
        temporary_target.unlink(missing_ok=True)
        with temporary_target.open("xb") as stream:
            write_mo(stream, catalog, use_fuzzy=False)
    except OSError as error:
        raise CatalogToolError(f"Could not write the pseudo catalog below {output}: {error}") from error
    try:
        _validate_loadable_mo(temporary_target)
        os.replace(temporary_target, final_target)
    except OSError as error:
        raise CatalogToolError(f"Could not write the pseudo catalog below {output}: {error}") from error
    finally:
        temporary_target.unlink(missing_ok=True)
    return final_target


def _is_inside_repository(candidate: Path, repository: Path) -> bool:
    # Lexical containment alone is spoofable on case-insensitive filesystems:
    # a case-variant spelling of the repository root survives ``resolve()``
    # verbatim yet names the same physical directory.  Compare filesystem
    # identity against every existing ancestor instead.
    if candidate == repository or repository in candidate.parents:
        return True
    try:
        repository_stat = repository.stat()
    except OSError:
        return False
    for ancestor in (candidate, *candidate.parents):
        try:
            ancestor_stat = ancestor.stat()
        except OSError:
            continue
        if os.path.samestat(ancestor_stat, repository_stat):
            return True
    return False


def pseudo_localize(template: str, *, multiline: bool = False) -> str:
    """Expand prose while preserving strict placeholders verbatim."""
    parts: list[str] = ["«"]
    index = 0
    prose_start = 0
    while index < len(template):
        if template[index] == "{" and index + 1 < len(template) and template[index + 1] == "{":
            index += 2
            continue
        if template[index] == "{" and (end := template.find("}", index + 1)) != -1:
            parts.append(_pseudo_prose(template[prose_start:index]))
            parts.append(template[index : end + 1])
            index = end + 1
            prose_start = index
            continue
        index += 1
    parts.append(_pseudo_prose(template[prose_start:]))
    parts.append("··»")
    localized = "".join(parts)
    parse_placeholder_names(localized)
    validate_authored_template(localized, multiline=multiline)
    if not has_visible_content(localized):
        raise CatalogToolError("Pseudo-localization produced an invisible template")
    return localized


def _pseudo_prose(text: str) -> str:
    accented = text.translate(_PSEUDO_ACCENTS)
    expanded: list[str] = []
    for character in accented:
        expanded.append(character)
        if character.lower() in {"å", "é", "î", "ö", "û", "ý"}:
            expanded.append("·")
    return "".join(expanded)


def _validate_loadable_mo(path: Path) -> None:
    try:
        with path.open("rb") as stream:
            gettext.GNUTranslations(stream)
    except (OSError, EOFError, LookupError, SyntaxError, ValueError) as error:
        raise CatalogToolError(f"Generated MO is not loadable: {path}") from error


def _active_messages(catalog: Catalog) -> tuple[Message, ...]:
    return tuple(message for message in catalog if message.id)


def _key_from_message(message: Message) -> str:
    if isinstance(message.id, tuple):
        return message.id[0]
    return message.id


def _translation_forms(message: Message) -> tuple[str, ...]:
    if isinstance(message.string, tuple):
        return tuple(form or "" for form in message.string)
    return (message.string or "",)


def _read_po(path: Path) -> Catalog:
    try:
        data = path.read_bytes()
        catalog = read_po(io.BytesIO(data), domain=DOMAIN, abort_invalid=True)
        if (catalog.charset or "").lower() != "utf-8":
            # A non-UTF-8 declaration makes Babel decode the UTF-8 bytes
            # under the wrong codec and ship mojibake translations.
            raise CatalogToolError(f"Catalog {path} must declare charset=UTF-8, not {catalog.charset!r}")
        text = data.decode("utf-8")
        _reject_duplicate_physical_entries(path, text)
        _reject_duplicate_entry_fields(path, text)
        _reject_physical_msgctxt(path, text)
    except (OSError, LookupError, PoFileError, UnicodeError, ValueError) as error:
        if isinstance(error, CatalogToolError):
            raise
        raise CatalogToolError(f"Could not read catalog {path}: {error}") from error
    return catalog


def _reject_duplicate_physical_entries(path: Path, text: str) -> None:
    # Babel's reader keeps the FIRST of two entries sharing a msgid and drops
    # the rest silently, so duplicates in a hand-edited tracked PO would ship
    # invisible dead content past every catalog-level check.
    entries: list[list[str]] = []
    collecting = False
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line.startswith("#") or not line:
            collecting = False
            continue
        # Keyword-parse instead of prefix-matching: Babel accepts arbitrary
        # whitespace between a field keyword and its quoted payload.
        keyword, quote, payload = line.partition('"')
        keyword = keyword.strip()
        if not quote:
            collecting = False
            continue
        if keyword == "msgid":
            entries.append([f'"{payload}'])
            collecting = True
            continue
        if not keyword:
            if collecting:
                entries[-1].append(line)
            continue
        collecting = False

    seen: set[str] = set()
    for parts in entries:
        msgid = denormalize("\n".join(parts))
        if msgid in seen:
            label = msgid or "<header>"
            raise CatalogToolError(f"Catalog {path} contains duplicate entries for {label!r}")
        seen.add(msgid)


def _reject_duplicate_entry_fields(path: Path, text: str) -> None:
    # Babel keeps the FIRST of duplicated fields within one entry while GNU
    # msgfmt rejects the file, so a tampered duplicate msgstr would ship
    # invisible dead content past every catalog-level check.  Entry state is
    # tracked strictly: fields outside an entry are stray, indexes are
    # numerically normalized, and msgstr shape must match the presence of
    # msgid_plural — Babel exposes only the first form of a malformed entry,
    # so a surplus form would otherwise dodge translation validation.
    fields: set[str] = set()
    in_entry = False
    for lineno, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if line.startswith("#") or not line:
            fields.clear()
            in_entry = False
            continue
        if line.startswith('"'):
            continue
        keyword = line.split('"', maxsplit=1)[0].strip()
        if not keyword:
            continue
        if not _is_strict_field_keyword(keyword) or '"' not in line:
            # Babel retains spellings like ``msgstr [0] "…"`` by folding the
            # garbage into the value and collapses signed indexes like
            # ``msgstr[+0]`` onto real forms, while GNU msgfmt rejects both —
            # so anything but the exact ASCII field grammar is a hard error
            # before any shape reasoning.
            raise CatalogToolError(f"Catalog {path} contains a malformed entry field {keyword!r} on line {lineno}")
        if keyword == "msgid":
            fields = {"msgid"}
            in_entry = True
            continue
        if not in_entry:
            raise CatalogToolError(f"Catalog {path} contains a stray {keyword} field on line {lineno}")
        normalized = _normalized_field_keyword(keyword)
        if normalized in fields:
            raise CatalogToolError(f"Catalog {path} contains a duplicate {keyword} field on line {lineno}")
        if normalized == "msgid_plural" and any(field.startswith("msgstr") for field in fields):
            raise CatalogToolError(f"Catalog {path} has msgid_plural after msgstr on line {lineno}")
        if normalized == "msgstr" and "msgid_plural" in fields:
            raise CatalogToolError(f"Catalog {path} has a plain msgstr on a plural entry on line {lineno}")
        if normalized.startswith("msgstr[") and "msgid_plural" not in fields:
            raise CatalogToolError(f"Catalog {path} has an indexed msgstr without msgid_plural on line {lineno}")
        fields.add(normalized)


def _is_strict_field_keyword(keyword: str) -> bool:
    if keyword in {"msgid", "msgid_plural", "msgstr", "msgctxt"}:
        return True
    base, bracket, index = keyword.partition("[")
    return base == "msgstr" and bracket == "[" and index.endswith("]") and _is_ascii_digits(index[:-1])


def _is_ascii_digits(text: str) -> bool:
    # str.isdigit accepts Unicode digits and int() additionally tolerates
    # signs and surrounding whitespace; msgstr indexes must be plain ASCII.
    return bool(text) and all(character in "0123456789" for character in text)


def _normalized_field_keyword(keyword: str) -> str:
    base, bracket, index = keyword.partition("[")
    if bracket and index.endswith("]") and _is_ascii_digits(index[:-1]):
        return f"{base}[{int(index[:-1])}]"
    return keyword


def _reject_physical_msgctxt(path: Path, text: str) -> None:
    # Chrys catalogs forbid msgctxt everywhere, and the catalog-level check
    # cannot see every physical occurrence: Babel silently drops a msgctxt
    # attached to the header entry while GNU tools reject the file outright.
    for lineno, raw_line in enumerate(text.splitlines(), start=1):
        if raw_line.strip().partition('"')[0].strip() == "msgctxt":
            raise CatalogToolError(f"Catalog {path} contains forbidden msgctxt on line {lineno}")


def _write_po(path: Path, catalog: Catalog) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False
        ) as stream:
            temporary_path = Path(stream.name)
            write_po(stream, catalog, width=0, sort_output=True, include_lineno=False)
        _replace_generated_file(temporary_path, path)
        temporary_path = None
    except OSError as error:
        raise CatalogToolError(f"Could not write catalog {path}: {error}") from error
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _replace_generated_file(temporary_path: Path, target: Path) -> None:
    permissions = target.stat().st_mode & 0o777 if target.exists() else 0o644
    temporary_path.chmod(permissions)
    os.replace(temporary_path, target)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("extract", help="extract src/chrys msg() definitions into locales/chrys.pot")
    subparsers.add_parser("update", help="merge source metadata into the maintained zh-Hans PO")
    subparsers.add_parser("compile", help="validate, then atomically compile the gitignored zh-Hans MO")
    subparsers.add_parser("check", help="non-mutating source/catalog freshness and safety gate")
    pseudo = subparsers.add_parser("pseudo", help="generate a loadable en-XA catalog tree")
    pseudo.add_argument("--output", required=True, type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the requested catalog command."""
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "extract":
            extract_catalog()
        elif args.command == "update":
            update_catalog()
        elif args.command == "compile":
            compile_catalog()
        elif args.command == "check":
            check_catalogs()
        elif args.command == "pseudo":
            generate_pseudo_catalog(args.output)
    except CatalogToolError as error:
        sys.stderr.write(f"Error: {error}\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
