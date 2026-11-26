# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Safe named-slot formatting for locale-neutral display messages."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from chrys.foundation.i18n.messages import MessageRef

_PLACEHOLDER_NAME_RE = re.compile(r"[a-z][a-z0-9_]*\Z")
_LOOKUP_ID_RE = re.compile(r"[a-z0-9_]+(?:\.[a-z0-9_]+)+(?:#plural)?\Z")
_REPLACEMENT_CHARACTER = "�"


def _cell_len(text: str) -> int:
    from rich.cells import cell_len

    return cell_len(text)


def has_visible_content(text: str) -> bool:
    """Return whether *text* contains a glyph with positive terminal width."""
    candidate = "".join(character for character in text if character != "\n" and not character.isspace())
    return bool(candidate) and _cell_len(candidate) > 0


def parse_placeholder_names(template: str) -> frozenset[str]:
    """Parse the project's strict ``{lowercase_name}`` placeholder grammar.

    A hand-rolled scanner, deliberately not ``string.Formatter.parse``: the
    Formatter cannot distinguish ``{value}`` from ``{value:}`` (both report an
    empty format spec), and the strict grammar admits nothing but a bare
    lowercase name between braces anyway.
    """
    if type(template) is not str:
        raise TypeError("Authored message templates must be strings.")

    names: set[str] = set()
    index = 0
    length = len(template)
    while index < length:
        character = template[index]
        if character == "{":
            if index + 1 < length and template[index + 1] == "{":
                index += 2
                continue
            end = template.find("}", index + 1)
            if end == -1:
                raise ValueError("Invalid message placeholder syntax.")
            name = template[index + 1 : end]
            if not _PLACEHOLDER_NAME_RE.fullmatch(name):
                raise ValueError("Message placeholder names must be lowercase snake_case identifiers.")
            names.add(name)
            index = end + 1
        elif character == "}":
            if index + 1 < length and template[index + 1] == "}":
                index += 2
                continue
            raise ValueError("Invalid message placeholder syntax.")
        else:
            index += 1
    return frozenset(names)


def validate_authored_template(template: str, *, multiline: bool) -> frozenset[str]:
    """Validate controls, markup, and placeholders in an authored template."""
    if type(multiline) is not bool:
        raise TypeError("The multiline flag must be a bool.")
    if type(template) is not str:
        raise TypeError("Authored message templates must be strings.")

    for character in template:
        codepoint = ord(character)
        if character == "\n":
            if multiline:
                continue
            raise ValueError("LF is forbidden in a single-line message template.")
        if codepoint < 0x20 or 0x7F <= codepoint <= 0x9F:
            raise ValueError("Control characters are forbidden in message templates.")
    names = parse_placeholder_names(template)
    _reject_markup_bracket_pairs(template)
    return names


def _reject_markup_bracket_pairs(template: str) -> None:
    # Default-deny: ANY unescaped "[" later followed by "]" reads as markup.
    # Shape-matching Textual's permissive tag lexer is a losing game
    # (whitespace-tolerant tags, "-"-prefixed tags, LF inside multiline
    # bodies), and a non-overlapping pair regex can be masked by an escaped
    # outer bracket swallowing an inner unescaped one. A "[" is escaped only
    # behind an ODD backslash run — Rich's escape — since each backslash pair
    # is one literal backslash; "]" needs no escape in Rich, so any "]" after
    # an unescaped "[" completes a potential tag.
    backslash_run = 0
    unescaped_open_seen = False
    for character in template:
        if character == "\\":
            backslash_run += 1
            continue
        if character == "[" and backslash_run % 2 == 0:
            unescaped_open_seen = True
        elif character == "]" and unescaped_open_seen:
            raise ValueError("Square-bracket pairs read as Rich/Textual markup and are forbidden in message templates.")
        backslash_run = 0


def _sanitize_controls(text: str, *, keep_lf: bool, keep_tab: bool = False) -> str:
    sanitized: list[str] = []
    for character in text:
        codepoint = ord(character)
        if (character == "\n" and keep_lf) or (character == "\t" and keep_tab):
            sanitized.append(character)
        elif codepoint < 0x20 or 0x7F <= codepoint <= 0x9F:
            sanitized.append(_REPLACEMENT_CHARACTER)
        else:
            sanitized.append(character)
    return "".join(sanitized)


def sanitize_legacy_scalar(text: str) -> str:
    """Sanitize literal display text with the single-line control policy."""
    if type(text) is not str:
        raise TypeError("Legacy display text must be a string.")
    return _sanitize_controls(text, keep_lf=False)


def sanitize_legacy_block(text: str) -> str:
    """Sanitize literal display text while preserving LF characters."""
    if type(text) is not str:
        raise TypeError("Legacy display text must be a string.")
    return _sanitize_controls(text, keep_lf=True)


def sanitize_terminal_block(text: str) -> str:
    """Sanitize literal text for a terminal while keeping its layout.

    LF and TAB survive; CR and CRLF become LF, so a bare carriage return cannot
    overwrite a printed line; every other C0/C1 control and DEL is replaced.
    """
    if type(text) is not str:
        raise TypeError("Legacy display text must be a string.")
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    return _sanitize_controls(normalized, keep_lf=True, keep_tab=True)


def format_template(template: str, parameters: Mapping[str, str], *, multiline: bool = False) -> str:
    """Format a validated template with an exact named-parameter schema."""
    names = validate_authored_template(template, multiline=multiline)
    parameter_names = set(parameters)
    expected_names = parameter_names - {"count"}
    template_names = names - {"count"}
    if template_names != expected_names or ("count" in names and "count" not in parameter_names):
        raise ValueError("Message template parameters do not match the bound argument schema.")
    if any(type(value) is not str for value in parameters.values()):
        raise TypeError("Formatted message parameters must already be display strings.")
    return template.format_map(parameters)


def _normalized_lookup_candidate(template: str) -> str:
    # Remove invisible characters FIRST — format/mark categories AND anything
    # with zero terminal cell width (e.g. U+1160, category Lo): stripping
    # whitespace before removing them would leave whitespace they were
    # shielding at the ends.
    without_invisibles = "".join(
        character
        for character in template
        if unicodedata.category(character) not in {"Cf", "Me", "Mn"} and _cell_len(character) != 0
    )
    return without_invisibles.strip()


def _is_lookup_id(template: str) -> bool:
    return bool(_LOOKUP_ID_RE.fullmatch(_normalized_lookup_candidate(template)))


def format_message(
    reference: MessageRef, template: str | None = None, *, render_nested: Callable[[MessageRef], str] | None = None
) -> str:
    """Render *reference*, falling back to English for an invalid translation."""
    from chrys.foundation.i18n.messages import _render_arguments

    definition = reference.definition
    fallback = definition.fallback if reference.count is None or reference.count == 1 else definition.plural_fallback
    if fallback is None:
        raise ValueError("A plural reference requires a plural fallback.")
    parameters = _render_arguments(reference, render_nested or format_message)
    english = format_template(fallback, parameters, multiline=definition.multiline)
    if template is None:
        return english

    try:
        if _is_lookup_id(template):
            raise ValueError("Catalog templates cannot be lookup identifiers.")
        translated = format_template(template, parameters, multiline=definition.multiline)
        if not has_visible_content(translated):
            raise ValueError("The formatted catalog message has no visible content.")
    except KeyError, ValueError:
        return english
    return translated
