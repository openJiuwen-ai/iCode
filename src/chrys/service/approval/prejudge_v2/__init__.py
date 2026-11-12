# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Conservative AST and structured YAML pre-judge, independent of the legacy shell filter.

This module never executes or rewrites a command. Unsupported or incomplete
syntax cannot yield ALLOW. Only Tree-sitter is an optional runtime dependency.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml
from yaml.resolver import BaseResolver

logger = logging.getLogger(__name__)


class _UniqueKeyLoader(yaml.SafeLoader):
    """Reject duplicate YAML keys instead of silently replacing a rule field."""


def _construct_unique_mapping(loader: _UniqueKeyLoader, node: yaml.MappingNode) -> dict[Any, Any]:
    result: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=True)
        if key in result:
            raise ValueError(f"Duplicate YAML policy key: {key!r}")
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
class CommandNode:
    source_span: tuple[int, int]
    argv: tuple[str, ...] | None
    prefix: tuple[str, ...]
    text: str


@dataclass(frozen=True, slots=True)
class Analysis:
    syntax_valid: bool
    coverage_complete: bool
    executable_nodes: tuple[CommandNode, ...]
    unsupported: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PreJudgeResult:
    route: Route
    reason_code: str
    matched_rule_ids: tuple[str, ...]
    matched_node_spans: tuple[tuple[int, int], ...]
    policy_id: str
    analysis_digest: str


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


_OPAQUE = frozenset(
    {
        ".",
        "bash",
        "builtin",
        "chrt",
        "command",
        "dash",
        "doas",
        "env",
        "eval",
        "exec",
        "find",
        "ionice",
        "nice",
        "nohup",
        "sh",
        "source",
        "stdbuf",
        "su",
        "sudo",
        "time",
        "timeout",
        "trap",
        "xargs",
    }
)
_DYNAMIC = frozenset(
    {
        "brace_expression",
        "glob_pattern",
        "extglob_pattern",
        "word_expansion",
        "expansion",
        "simple_expansion",
        "command_substitution",
        "process_substitution",
        "arithmetic_expansion",
        "variable_assignment",
        "heredoc_redirect",
        "heredoc_body",
        "here_string",
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
        if ch.isspace():
            return None
        result.append(ch)
        i += 1
    return "".join(result)


def _dynamic(node: Any) -> bool:
    if node.type in _DYNAMIC or node.type in {"ERROR", "MISSING"} or node.is_missing:
        return True
    return any(_dynamic(child) for child in node.named_children)


def _parse_command(node: Any, data: bytes) -> CommandNode:
    text = data[node.start_byte : node.end_byte].decode("utf-8")
    head = node.child_by_field_name("name")
    if head is None:
        return CommandNode((node.start_byte, node.end_byte), None, (), text)
    words = [head]
    # Assignments and redirects are not argv.
    for child in node.named_children:
        if child.id == head.id:
            continue
        if child.type in _QUOTED:
            words.append(child)
    prefix: list[str] = []
    prefix_reliable = True
    # Assignment prefixes and redirections prevent certifying complete argv.
    # Retain the static head to identify opaque command wrappers.
    complete = all(child.type in _QUOTED or child.id == head.id for child in node.named_children)
    for word in words:
        raw = data[word.start_byte : word.end_byte].decode("utf-8")
        if _dynamic(word):
            complete = False
            prefix_reliable = False
            continue
        token = _literal_word(raw)
        if token is None:
            complete = False
            prefix_reliable = False
            continue
        if prefix_reliable:
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
    if dialect not in {"bash", "git_bash"}:
        return Analysis(False, False, (), ("unsupported_dialect",))
    try:
        data = command.encode("utf-8")
    except UnicodeError:
        return Analysis(False, False, (), ("parse_error",))
    if len(data) > 65536:
        return Analysis(False, False, (), ("limit_exceeded",))
    contains_line_continuation = b"\\\n" in data
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
    if contains_line_continuation:
        unsupported.append("line_continuation")
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
            if any(c.type not in _QUOTED and c.type != "command_name" for c in node.named_children):
                unsupported.append("unsupported_construct")
            # Substitutions make the whole command ineligible for ALLOW.
            for child in reversed(node.named_children):
                if _dynamic(child):
                    unsupported.append("dynamic_word")
                    pending.extend((c, depth + 1) for c in reversed(child.named_children))
            continue
        if node.type not in _CONTAINERS and node.type != "comment":
            unsupported.append(node.type)
        # Keep unsupported constructs visible in the analysis.
        pending.extend((child, depth + 1) for child in reversed(node.named_children))
    nodes.sort(key=lambda item: item.source_span)
    if not nodes:
        unsupported.append("no_commands")
    if any(n.argv is None for n in nodes):
        unsupported.append("dynamic_word")
    if any(n.prefix and n.prefix[0] in _OPAQUE for n in nodes):
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
        raise ValueError("Each match must have exactly one matcher field")
    field, tokens = next(iter(raw.items()))
    if field not in {"argv_exact", "argv_first"}:
        raise ValueError(f"Unsupported matcher field: {field!r}")
    if (
        not isinstance(tokens, list)
        or not tokens
        or len(tokens) > 128
        or any(type(token) is not str or not token or len(token) > 4096 for token in tokens)
    ):
        raise ValueError(f"Matcher {field} needs a non-empty list of literal string tokens")
    if field != "argv_exact" and len(tokens) != 1:
        raise ValueError(f"Matcher {field} requires exactly one token")
    return Matcher(field, tuple(tokens))


def _compile_enabled_policy(content: dict[str, Any], mode: str) -> Policy:
    ident = content.get("policy_id")
    raw_rules = content.get("rules")
    if not isinstance(ident, str) or not ident or not isinstance(raw_rules, list) or len(raw_rules) > 1000:
        raise ValueError("Invalid pre-judge policy_id or rules")
    _ensure_bash_parser()
    rules: list[Rule] = []
    seen: set[str] = set()
    for raw in raw_rules:
        # A policy author needs to know which rule failed, including rules
        # whose match expression is not well formed.
        rid = raw.get("id", "<missing>") if isinstance(raw, dict) else "<invalid>"
        try:
            if not isinstance(raw, dict) or set(raw) != {"id", "effect", "target", "match"}:
                raise ValueError("Invalid pre-judge rule fields; expected id, effect, target, match")
            rid, effect, target = (raw[key] for key in ("id", "effect", "target"))
            if not isinstance(rid, str) or not rid or rid in seen:
                raise ValueError("Duplicate or invalid pre-judge rule ID")
            if effect != "allow" or target != "command":
                raise ValueError("Pre-Judge rules require effect: allow and target: command")
            matcher = _compile_matcher(raw["match"])
        except (ValueError, KeyError, TypeError, StopIteration) as exc:
            raise ValueError(f"rule {rid!r}: {exc}") from exc
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


def load_policy(path: Path) -> Policy:
    """Load the trusted policy; never require a parser to turn it off."""
    if not path.exists():
        return Policy("off", "", ())
    content: Any = None
    try:
        # _UniqueKeyLoader derives from SafeLoader and rejects duplicate keys.
        content = yaml.load(path.read_text(encoding="utf-8"), Loader=_UniqueKeyLoader)  # noqa: S506
        # PyYAML's YAML 1.1 resolver turns unquoted 'off' into False.
        if content is None or content is False:
            return Policy("off", "", ())
        if not isinstance(content, dict) or set(content) - {"schema_version", "mode", "policy_id", "rules"}:
            raise ValueError("Invalid pre-judge configuration")
        mode = content.get("mode")
        if mode is False or mode == "off":
            return Policy("off", "", ())
        if not isinstance(mode, str) or mode not in ("shadow", "enforce"):
            raise ValueError("mode must be 'off', 'shadow', or 'enforce'")
        if type(content.get("schema_version")) is not int or content["schema_version"] != 1:
            raise ValueError("Invalid pre-judge schema_version")
        return _compile_enabled_policy(content, mode)
    except Exception as exc:
        message = f"Pre-Judge policy {path}: {exc}"
        if isinstance(content, dict) and content.get("mode") == "shadow":
            logger.warning("%s; disabling shadow observation", message, exc_info=True)
            return Policy("off", "", ())
        if isinstance(exc, RuntimeError):
            raise RuntimeError(message) from exc
        raise ValueError(message) from exc


def route(command: str, dialect: str, policy: Policy) -> PreJudgeResult:
    digest = _digest(command, dialect)
    if policy.mode == "off":
        return PreJudgeResult(Route.DEFER, "off", (), (), policy.policy_id, digest)
    analysis = analyze(command, dialect=dialect)
    if dialect not in {"bash", "git_bash"}:
        _warn_unsupported_dialect(dialect)
    matched: list[str] = []
    spans: list[tuple[int, int]] = []
    all_allowed = analysis.coverage_complete
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
    if all_allowed:
        return PreJudgeResult(Route.ALLOW, "all_units_allowed", tuple(matched), tuple(spans), policy.policy_id, digest)
    return PreJudgeResult(
        Route.DEFER,
        analysis.unsupported[0] if analysis.unsupported else "unmatched_unit",
        tuple(matched),
        tuple(spans),
        policy.policy_id,
        digest,
    )
