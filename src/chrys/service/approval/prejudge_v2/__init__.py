# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Conservative AST and structured YAML pre-judge, independent of the legacy shell filter.

This module never executes or rewrites a command. Unsupported or incomplete
syntax cannot yield ALLOW. Only Tree-sitter is an optional runtime dependency.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml
from yaml.resolver import BaseResolver

from chrys.service.approval.safety_classifier import (
    POSIX_SHELL_NAMES,
    normalize_executable_name,
    prejudge_source_may_access_sensitive_data,
    shell_argv_may_access_sensitive_data,
)

from ._analysis import Analysis as Analysis
from ._analysis import CommandNode as CommandNode
from ._analysis import covered

logger = logging.getLogger(__name__)


class _PolicyValidationError(ValueError):
    """Validation detail consisting only of fixed messages and rule positions."""


class _UniqueKeyLoader(yaml.SafeLoader):
    """Reject duplicate YAML keys instead of silently replacing a rule field."""


def _construct_unique_mapping(loader: _UniqueKeyLoader, node: yaml.MappingNode) -> dict[Any, Any]:
    result: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=True)
        if key in result:
            raise _PolicyValidationError("Duplicate YAML policy key")
        result[key] = loader.construct_object(value_node, deep=True)
    return result


_UniqueKeyLoader.add_constructor(
    BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


class Route(StrEnum):
    ALLOW = "allow"
    DEFER = "defer"


@dataclass(frozen=True, slots=True)
class PreJudgeResult:
    route: Route
    reason_code: str
    matched_rule_ids: tuple[str, ...]
    matched_node_spans: tuple[tuple[int, int], ...]
    policy_id: str
    analysis_digest: str
    executables: tuple[str, ...] = ()
    sensitive: bool = False
    decoded_sensitive_target: bool = False
    policy_candidate: Route = Route.DEFER
    unresolved_carrier: bool = False


MatcherField = Literal["argv_exact", "argv_first"]


@dataclass(frozen=True, slots=True)
class Matcher:
    """A single checked operation on complete static argv."""

    field: MatcherField
    tokens: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Rule:
    id: str
    effect: Literal[Route.ALLOW]
    match: Matcher


@dataclass(frozen=True, slots=True)
class Policy:
    mode: str
    policy_id: str
    rules: tuple[Rule, ...]


@lru_cache(maxsize=32)
def _warn_unsupported_dialect(dialect: str) -> None:
    logger.warning(
        "Structured Pre-Judge ALLOW rules are inactive for unsupported shell %r; "
        "AST rules cannot evaluate this dialect; ordinary approval, including the legacy readonly shortcut, applies",
        dialect,
    )


_BASH_DIALECTS = frozenset({"bash", "git_bash"})
_OPAQUE = POSIX_SHELL_NAMES | frozenset(
    {
        ".",
        "[",
        "alias",
        "builtin",
        "cd",
        "chrt",
        "command",
        "doas",
        "declare",
        "enable",
        "env",
        "eval",
        "exec",
        "export",
        "find",
        "getopts",
        "hash",
        "history",
        "ionice",
        "let",
        "mapfile",
        "nice",
        "nohup",
        "open",
        "osascript",
        "popd",
        "pushd",
        "read",
        "readarray",
        "readonly",
        "set",
        "shopt",
        "source",
        "stdbuf",
        "su",
        "sudo",
        "test",
        "time",
        "timeout",
        "trap",
        "typeset",
        "unalias",
        "unset",
        "xargs",
    }
)
_CONTAINERS = frozenset(
    {
        "program",
        "list",
        "pipeline",
        "compound_statement",
        "subshell",
    }
)
_QUOTED = frozenset({"string", "raw_string", "ansi_c_string", "word", "number", "concatenation", "command_name"})
_HORIZONTAL = b" \t"
_LAYOUT = b" \t\n"
_CONTROL = re.compile(rb"[^\x20-\x7e\t\n]")
_ESCAPED_LAYOUT = re.compile(rb"\\[ \t\n]")
_CONTAINER_PUNCTUATION = {
    "program": {b";"},
    "list": {b"&&", b"||"},
    "pipeline": {b"|"},
    "compound_statement": {b"{", b";", b"}"},
    "subshell": {b"(", b";", b")"},
}


def _digest(command: str, dialect: str) -> str:
    return hashlib.sha256((dialect + "\x00" + command).encode("utf-8", errors="surrogatepass")).hexdigest()


def _ansi_c_literal(source: str) -> str | None:
    """Decode verified, locale-independent Bash ANSI-C escapes only."""
    escapes = {
        "a": "\a",
        "b": "\b",
        "e": "\x1b",
        "E": "\x1b",
        "f": "\f",
        "n": "\n",
        "r": "\r",
        "t": "\t",
        "v": "\v",
        "\\": "\\",
        "'": "'",
        '"': '"',
        "?": "?",
    }
    result: list[str] = []
    i = 0
    while i < len(source):
        if source[i] != "\\":
            result.append(source[i])
            i += 1
            continue
        if i + 1 >= len(source):
            return None
        kind = source[i + 1]
        if kind in escapes:
            result.append(escapes[kind])
            i += 2
            continue
        if kind in "01234567":
            j = i + 1
            while j < min(len(source), i + 4) and source[j] in "01234567":
                j += 1
            value = int(source[i + 1 : j], 8)
        elif kind == "x":
            j = i + 2
            while j < min(len(source), i + 4) and source[j] in "0123456789abcdefABCDEF":
                j += 1
            if j == i + 2:
                return None
            value = int(source[i + 2 : j], 16)
        else:
            return None
        if not 0 < value < 128:
            return None
        result.append(chr(value))
        i = j
    return "".join(result)


def _literal_word(raw: str) -> str | None:
    """Certify static Bash words; never interpret shlex output as Bash argv.

    Quoted concatenation and escapes are recognized, but globs, braces,
    unquoted tildes, parameter substitution and unknown syntax remain dynamic.
    """
    result: list[str] = []
    i = 0
    while i < len(raw):
        ch = raw[i]
        if raw.startswith("$'", i):
            j = i + 2
            while j < len(raw):
                if raw[j] == "\\":
                    j += 2
                elif raw[j] == "'":
                    break
                else:
                    j += 1
            if j >= len(raw):
                return None
            decoded = _ansi_c_literal(raw[i + 2 : j])
            if decoded is None:
                return None
            result.append(decoded)
            i = j + 1
            continue
        if ch == "'":
            j = raw.find("'", i + 1)
            if j < 0:
                return None
            result.append(raw[i + 1 : j])
            i = j + 1
            continue
        if ch == '"':
            i += 1
            while i < len(raw) and raw[i] != '"':
                if raw[i] in ("$", chr(96)):
                    return None
                if raw[i] == "\\":
                    if i + 1 >= len(raw):
                        return None
                    following = raw[i + 1]
                    if following in ("$", chr(96), '"', "\\"):
                        result.append(following)
                        i += 2
                        continue
                    if following == "\n":
                        i += 2
                        continue
                    result.append("\\")
                    i += 1
                    continue
                result.append(raw[i])
                i += 1
            if i >= len(raw):
                return None
            i += 1
            continue
        if ch == "\\":
            if i + 1 >= len(raw):
                return None
            if raw[i + 1] != "\n":
                result.append(raw[i + 1])
            i += 2
            continue
        if ch in ("$", chr(96), "*", "?", "[", "{", "}", "~", ";", "&", "|", "(", ")", "<", ">"):
            return None
        if ch in " \t\n#":
            return None
        result.append(ch)
        i += 1
    return "".join(result)


def _bash_word_shape(node: Any, data: bytes) -> bool:
    """Accept only literal subtrees, including their anonymous delimiters."""
    pending = [node]
    while pending:
        part = pending.pop()
        children = part.children
        if part.type in {"word", "number", "raw_string", "ansi_c_string", "string_content"}:
            if children:
                return False
        elif part.type in {"command_name", "concatenation"}:
            if not children or not covered(part, data, b"") or any(not child.is_named for child in children):
                return False
            pending.extend(children)
        elif part.type == "string":
            if (
                len(children) < 2
                or children[0].type != '"'
                or children[-1].type != '"'
                or any(child.type != "string_content" or child.children for child in children[1:-1])
            ):
                return False
        else:
            return False
    return True


def _parse_command(node: Any, data: bytes) -> CommandNode:
    text = data[node.start_byte : node.end_byte].decode("utf-8")
    head = node.child_by_field_name("name")
    if head is None:
        return CommandNode((node.start_byte, node.end_byte), None, (), text)
    children = node.children
    complete = bool(children and children[0].id == head.id and covered(node, data, _HORIZONTAL))
    prefix: list[str] = []
    for index, word in enumerate(children):
        field = "name" if word.id == head.id else "argument"
        if (
            not word.is_named
            or word.type not in _QUOTED
            or node.field_name_for_child(index) != field
            or (index and word.start_byte <= children[index - 1].end_byte)
            or not _bash_word_shape(word, data)
        ):
            complete = False
            continue
        raw = data[word.start_byte : word.end_byte].decode("ascii")
        token = _literal_word(raw)
        if token is None:
            complete = False
        elif (field == "name" and not prefix) or complete:
            prefix.append(token)
    return CommandNode(
        (node.start_byte, node.end_byte),
        tuple(prefix) if complete else None,
        tuple(prefix),
        text,
    )


@lru_cache(maxsize=1)
def _language() -> Any:
    import tree_sitter_bash
    from tree_sitter import Language

    return Language(tree_sitter_bash.language())


def analyze(command: str, *, dialect: str = "bash") -> Analysis:
    if dialect not in _BASH_DIALECTS:
        return Analysis(False, False, (), ("unsupported_dialect",))
    try:
        data = command.encode("utf-8")
    except UnicodeError:
        return Analysis(False, False, (), ("parse_error",))
    if len(data) > 65536:
        return Analysis(False, False, (), ("limit_exceeded",))
    if _CONTROL.search(data):
        return Analysis(False, False, (), ("unsupported_input",))
    if _ESCAPED_LAYOUT.search(data):
        return Analysis(False, False, (), ("line_continuation",))
    if b'$"' in data:
        return Analysis(False, False, (), ("locale_translation",))
    try:
        from tree_sitter import Parser
    except ImportError as exc:
        raise RuntimeError("Enable the optional pre-judge dependencies first") from exc
    tree = Parser(_language()).parse(data)
    root = tree.root_node
    if root.has_error:
        return Analysis(False, False, (), ("parse_error",))
    nodes: list[CommandNode] = []
    unsupported: list[str] = []
    if (data[: root.start_byte] + data[root.end_byte :]).strip(_LAYOUT):
        unsupported.append("unaccounted_source")
    pending = [(root, 0)]
    count = 0
    while pending:
        node, depth = pending.pop()
        count += 1
        if count > 20000 or depth > 128:
            return Analysis(True, False, tuple(nodes), ("limit_exceeded",))
        if node.type in {"ERROR", "MISSING"} or node.is_missing:
            # Recovered syntax is not reliable executable evidence.
            continue
        if node.type == "command":
            if node.has_error:
                unsupported.append("parse_error")
                continue
            nodes.append(_parse_command(node, data))
            continue
        if node.type not in _CONTAINERS:
            unsupported.append(node.type)
        elif not covered(node, data, _LAYOUT):
            unsupported.append("unaccounted_source")
        allowed = _CONTAINER_PUNCTUATION.get(node.type, ())
        for child in reversed(node.children):
            if child.is_named:
                pending.append((child, depth + 1))
            elif data[child.start_byte : child.end_byte] not in allowed:
                unsupported.append("unsupported_punctuation")
    nodes.sort(key=lambda item: item.source_span)
    if not nodes:
        unsupported.append("no_commands")
    if any(n.argv is None for n in nodes):
        unsupported.append("dynamic_word")
    if any(_opaque(n.prefix) for n in nodes):
        unsupported.append("opaque_carrier")
    return Analysis(not root.has_error, not unsupported, tuple(nodes), tuple(dict.fromkeys(unsupported)))


def _ensure_bash_parser() -> None:
    """Fail during configuration, not at the first shell invocation."""
    try:
        from tree_sitter import Parser

        Parser(_language())
    except Exception as exc:
        raise RuntimeError(
            "Enabled pre-judge requires tree-sitter==0.25.1 and tree-sitter-bash==0.25.1 with a compatible Bash grammar"
        ) from exc


def _compile_matcher(raw: object) -> Matcher:
    if not isinstance(raw, dict) or len(raw) != 1:
        raise _PolicyValidationError("Each match must have exactly one matcher field")
    field, tokens = next(iter(raw.items()))
    if field not in {"argv_exact", "argv_first"}:
        raise _PolicyValidationError("Unsupported matcher field")
    if (
        not isinstance(tokens, list)
        or not tokens
        or len(tokens) > 128
        or any(type(token) is not str or not token or len(token) > 4096 for token in tokens)
    ):
        raise _PolicyValidationError(f"Matcher {field} needs a non-empty list of literal string tokens")
    if field != "argv_exact" and len(tokens) != 1:
        raise _PolicyValidationError(f"Matcher {field} requires exactly one token")
    return Matcher(field, tuple(tokens))


def _compile_enabled_policy(content: dict[str, Any], mode: str) -> Policy:
    ident = content.get("policy_id")
    raw_rules = content.get("rules")
    if not isinstance(ident, str) or not ident or not isinstance(raw_rules, list) or len(raw_rules) > 1000:
        raise _PolicyValidationError("Invalid pre-judge policy_id or rules")
    _ensure_bash_parser()
    rules: list[Rule] = []
    seen: set[str] = set()
    for position, raw in enumerate(raw_rules, start=1):
        try:
            if not isinstance(raw, dict) or set(raw) != {"id", "effect", "target", "match"}:
                raise _PolicyValidationError("Invalid pre-judge rule fields; expected id, effect, target, match")
            rid, effect, target = (raw[key] for key in ("id", "effect", "target"))
            if not isinstance(rid, str) or not rid or rid in seen:
                raise _PolicyValidationError("Duplicate or invalid pre-judge rule ID")
            if effect != "allow" or target != "command":
                raise _PolicyValidationError("Pre-Judge rules require effect: allow and target: command")
            matcher = _compile_matcher(raw["match"])
        except _PolicyValidationError as exc:
            raise _PolicyValidationError(f"rule {position}: {exc}") from None
        seen.add(rid)
        rules.append(Rule(rid, Route.ALLOW, matcher))
    return Policy(mode, ident, tuple(rules))


def _matches(matcher: Matcher, node: CommandNode) -> bool:
    """No expansions, expression interpreter or implicit prefix matching."""
    if node.argv is None:
        return False
    if matcher.field == "argv_first":
        return bool(node.argv) and node.argv[0] == matcher.tokens[0]
    return node.argv == matcher.tokens


def load_policy(path: Path, *, report_shadow_failure: bool = False) -> Policy:
    """Load the trusted policy; never require a parser to turn it off."""
    content: Any = None
    parser_failure = False
    try:
        try:
            source = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return Policy("off", "", ())
        # _UniqueKeyLoader derives from SafeLoader and rejects duplicate keys.
        content = yaml.load(source, Loader=_UniqueKeyLoader)  # noqa: S506
        # PyYAML's YAML 1.1 resolver turns unquoted 'off' into False.
        if content is None or content is False:
            return Policy("off", "", ())
        if not isinstance(content, dict) or set(content) - {"schema_version", "mode", "policy_id", "rules"}:
            raise _PolicyValidationError("Invalid pre-judge configuration")
        mode = content.get("mode")
        if mode is False or mode == "off":
            return Policy("off", "", ())
        if not isinstance(mode, str) or mode not in ("shadow", "enforce"):
            raise _PolicyValidationError("mode must be 'off', 'shadow', or 'enforce'")
        if type(content.get("schema_version")) is not int or content["schema_version"] != 1:
            raise _PolicyValidationError("Invalid pre-judge schema_version")
        return _compile_enabled_policy(content, mode)
    except _PolicyValidationError as exc:
        detail = str(exc)
    except yaml.YAMLError:
        detail = "Invalid YAML syntax"
    except OSError, UnicodeError:
        detail = "Could not read UTF-8 policy"
    except RuntimeError:
        parser_failure = True
        detail = "Parser unavailable; check tree-sitter==0.25.1 and tree-sitter-bash==0.25.1"
    except Exception:
        detail = "Could not validate policy"
    # Startup callers can log the exception. Keep parser and YAML internals out of its chain.
    message = f"Pre-Judge policy {path}: {detail}. Check the YAML and parser installation, then restart iCode."
    if not report_shadow_failure and isinstance(content, dict) and content.get("mode") == "shadow":
        logger.warning("%s Disabling shadow observation.", message)
        return Policy("off", "", ())
    if parser_failure:
        raise RuntimeError(message)
    raise ValueError(message)


def _opaque(argv: tuple[str, ...]) -> bool:
    if not argv:
        return False
    head = normalize_executable_name(argv[0])
    return head in _OPAQUE or (
        head == "printf"
        and (
            any(arg.startswith("-v") for arg in argv[1:2])
            or any(re.search(r"%[-+ #'0-9.*]*n", arg) for arg in argv[1:])
        )
    )


def route(command: str, dialect: str, policy: Policy) -> PreJudgeResult:
    digest = _digest(command, dialect)
    if policy.mode == "off":
        return PreJudgeResult(Route.DEFER, "off", (), (), policy.policy_id, digest)
    analysis = analyze(command, dialect=dialect)
    if dialect not in _BASH_DIALECTS:
        _warn_unsupported_dialect(dialect)
    matched: list[str] = []
    spans: list[tuple[int, int]] = []
    all_allowed = analysis.coverage_complete
    reason = analysis.unsupported[0] if analysis.unsupported else "unmatched_unit"
    raw = prejudge_source_may_access_sensitive_data(command, shell_name=dialect)
    raw_sensitive = raw is True
    argv_sensitive = False
    unresolved_carrier = raw is None
    for node in analysis.executable_nodes:
        allowed = False
        for rule in policy.rules:
            # ALLOW requires complete argv, never only a static prefix.
            if _matches(rule.match, node):
                matched.append(rule.id)
                spans.append(node.source_span)
                allowed = True
        if not allowed:
            all_allowed = False
        classified = shell_argv_may_access_sensitive_data(
            node.argv or node.prefix, shell_name=dialect, complete=node.argv is not None
        )
        argv_sensitive |= classified is True
        unresolved_carrier |= classified is None
    policy_candidate = Route.ALLOW if all_allowed else Route.DEFER
    sensitive = raw_sensitive or argv_sensitive
    if sensitive or unresolved_carrier:
        all_allowed = False
        reason = "sensitive_target" if sensitive else "unresolved_carrier"
    return PreJudgeResult(
        Route.ALLOW if all_allowed else Route.DEFER,
        "all_units_allowed" if all_allowed else reason,
        tuple(matched),
        tuple(spans),
        policy.policy_id,
        digest,
        tuple(node.argv[0] for node in analysis.executable_nodes if node.argv) if all_allowed else (),
        sensitive,
        argv_sensitive and not raw_sensitive,
        policy_candidate,
        unresolved_carrier,
    )
