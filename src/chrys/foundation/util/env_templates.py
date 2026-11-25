# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Runtime environment-variable template resolution.

Configuration files may carry literal placeholders such as ``{{API_KEY}}``.
Those placeholders are intentionally resolved only at runtime so saved YAML
keeps the user-authored value and never receives resolved secrets.

A ``{{!command}}`` placeholder resolves to a handle standing for the command's
output, and runs nothing: this code is synchronous and runs while sessions save,
and what it returns feeds fingerprints and cache keys that a new token must not
change.  ``service/command_credentials.py`` swaps handles for tokens right before
a request or a child process uses them.
"""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import Any

_ENV_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_ESCAPE_OPEN = "{{{{"
_ESCAPE_CLOSE = "}}}}"

COMMAND_HANDLE_RE = re.compile(r"chrys-cmd-[0-9a-f]{32}")


@dataclass(frozen=True, slots=True)
class CommandPlaceholder:
    """What a handle stands for: the command, and the setting that names it, for errors."""

    command: str
    location: str


_command_placeholders: dict[str, CommandPlaceholder] = {}
"""Only a handle resolved from a real placeholder is here: other text of that shape stays plain text."""


class EnvVarResolutionError(ValueError):
    """Raised when an environment-variable placeholder cannot be resolved."""


def _invalid_placeholder_message(raw: str, location: str, *, after_command: bool = False) -> str:
    # This text can reach the model, and a command may hold a secret.
    shown = "{{!...}}" if raw.lstrip("{").lstrip().startswith("!") else raw
    remedy = (
        "A command placeholder ends at its first }}, so a literal }} in the command must be written as }}}}, "
        "or the command put in a script."
        if after_command
        else "Use {{ENV_VAR}} with letters, digits, and underscores only, and no spaces, or {{!command}}."
    )
    return f"Invalid environment variable placeholder {shown!r} in {location}. {remedy}"


def _command_handle(body: str, *, raw: str, location: str, allow_commands: bool) -> str:
    """Return the handle of the ``{{!command}}`` whose text after the ``!`` is *body*."""
    command = _restore_escapes(body).strip()
    if not command:
        raise EnvVarResolutionError(_invalid_placeholder_message(raw, location))
    if not allow_commands:
        raise EnvVarResolutionError(
            f"{{{{!command}}}} placeholders are not supported in {location}. "
            "To keep the text as written, escape it as {{{{!...}}}}."
        )
    digest = hashlib.sha256(command.encode("utf-8", "surrogatepass")).hexdigest()
    handle = f"chrys-cmd-{digest[:32]}"
    _command_placeholders[handle] = CommandPlaceholder(command, location)
    return handle


def command_placeholder(handle: str) -> CommandPlaceholder:
    """Return what *handle*, one that :func:`command_handles_in` found, stands for."""
    return _command_placeholders[handle]


def command_handles_in(values: Iterable[str]) -> set[str]:
    """Return the handles of real ``{{!command}}`` placeholders in resolved *values*."""
    return {
        handle for value in values for handle in COMMAND_HANDLE_RE.findall(value) if handle in _command_placeholders
    }


@dataclass(frozen=True, slots=True)
class _Text:
    """A run of literal text, in the positions it holds in the value it came from."""

    start: int
    end: int


@dataclass(frozen=True, slots=True)
class _Placeholder:
    """One ``{{...}}``, and the text between its braces as written."""

    start: int
    end: int
    inner: str
    terminated: bool


def _iter_spans(value: str) -> Iterator[_Text | _Placeholder]:
    """Split *value* into literal runs and placeholders, for every reader of placeholders.

    ``{{{{`` and ````}}}}`` are literal braces wherever they appear, inside a
    placeholder's body as much as outside it, so a placeholder ends at the first
    ``}}`` that no doubled pair owns and a doubled pair is never a boundary.
    """
    length = len(value)
    text_start = 0
    index = 0
    while index < length:
        if value.startswith(_ESCAPE_OPEN, index) or value.startswith(_ESCAPE_CLOSE, index):
            index += 4
            continue
        if not value.startswith("{{", index):
            index += 1
            continue
        yield _Text(text_start, index)
        close = _closing_braces(value, index + 2)
        if close is None:
            yield _Placeholder(index, length, value[index + 2 :], terminated=False)
            return
        yield _Placeholder(index, close + 2, value[index + 2 : close], terminated=True)
        index = text_start = close + 2
    yield _Text(text_start, length)


def _closing_braces(value: str, index: int) -> int | None:
    """Return the index of the ``}}`` ending a placeholder whose body starts at *index*, if it has one."""
    length = len(value)
    while index < length:
        if value.startswith(_ESCAPE_CLOSE, index):
            index += 4
            continue
        if value.startswith("}}", index):
            return index
        index += 1
    return None


def _restore_escapes(text: str) -> str:
    """Return *text* with each doubled brace pair collapsed in one left-to-right pass."""
    if _ESCAPE_OPEN not in text and _ESCAPE_CLOSE not in text:
        return text
    parts: list[str] = []
    index = 0
    length = len(text)
    while index < length:
        if text.startswith(_ESCAPE_OPEN, index):
            parts.append("{{")
            index += 4
            continue
        if text.startswith(_ESCAPE_CLOSE, index):
            parts.append("}}")
            index += 4
            continue
        parts.append(text[index])
        index += 1
    return "".join(parts)


def _contains_stray_close(text: str) -> bool:
    """Whether literal *text* holds a ``}}`` that no doubled pair owns."""
    index = 0
    while (found := text.find("}}", index)) != -1:
        if text.startswith(_ESCAPE_CLOSE, found):
            index = found + 4
            continue
        return True
    return False


def blank_commands(value: str) -> str:
    """Return unresolved *value*, same length, with each ``{{!command}}`` blanked.

    A command whose body holds an unescaped ``}}`` ends at that ``}}``, so what
    follows it cannot be told apart from the rest of the command: from its ``{{!``
    to the end of the value is blanked, keeping every remaining position.  A
    ``{{!`` with no closing pair is left as written.

    For checking a draft against what can go on the wire: a command's text never
    does, and what it prints is checked when it runs.
    """
    spans = list(_iter_spans(value))
    parts: list[str] = []
    position = 0
    for index, span in enumerate(spans):
        if not isinstance(span, _Placeholder) or not span.inner.startswith("!"):
            continue
        parts.append(value[position : span.start])
        if not span.terminated:
            parts.append(value[span.start :])
            return "".join(parts)
        parts.append("x" * (span.end - span.start))
        position = span.end
        following = spans[index + 1]
        if isinstance(following, _Text) and _contains_stray_close(value[following.start : following.end]):
            parts.append("x" * (len(value) - position))
            return "".join(parts)
    parts.append(value[position:])
    return "".join(parts)


def has_command_placeholder(value: str) -> bool:
    """Whether unresolved *value* holds a ``{{!command}}`` that resolving would run (escaped braces do not count)."""
    return any(
        isinstance(span, _Placeholder) and span.terminated and span.inner.startswith("!") for span in _iter_spans(value)
    )


def resolve_env_templates(value: str, *, location: str, allow_commands: bool = True) -> str:
    """Resolve ``{{ENV_VAR}}`` and ``{{!command}}`` placeholders in a string.

    Resolution is single-pass: if the environment value itself contains
    ``{{...}}``, that text is preserved literally, and a command in it never
    runs.  Missing or empty environment variables are treated as errors
    because an explicit placeholder means the user expects that runtime value
    to exist.  Only double-brace blocks containing a valid environment
    variable identifier or starting with ``!`` are resolved; other non-empty
    blocks, such as ``{{"kind":"json"}}``, are preserved literally.  A
    ``{{!command}}`` where the value must be known now, or cannot be swapped
    later (*allow_commands* false), is an error.  A literal ``}}`` inside a
    command is written ````}}}}``; written as plain ``}}`` it ends the command,
    which is reported as an error rather than guessed at.
    """
    if "{{" not in value and "}}" not in value:
        return value

    parts: list[str] = []
    command: _Placeholder | None = None
    for span in _iter_spans(value):
        if isinstance(span, _Text):
            literal = value[span.start : span.end]
            if _contains_stray_close(literal):
                # The command's own text is the placeholder before this run: showing the run would show its tail.
                raw = value[command.start : command.end] if command is not None else "}}"
                raise EnvVarResolutionError(
                    _invalid_placeholder_message(raw, location, after_command=command is not None)
                )
            parts.append(_restore_escapes(literal))
            command = None
            continue

        raw_placeholder = value[span.start : span.end]
        if not span.terminated:
            raise EnvVarResolutionError(_invalid_placeholder_message(raw_placeholder, location))
        name = span.inner
        if name == "":
            raise EnvVarResolutionError(_invalid_placeholder_message(raw_placeholder, location))
        if name.startswith("!"):
            parts.append(
                _command_handle(name[1:], raw=raw_placeholder, location=location, allow_commands=allow_commands)
            )
            command = span
            continue
        command = None
        if _ENV_NAME_RE.fullmatch(name) is None:
            stripped = name.strip()
            if stripped != name and (_ENV_NAME_RE.fullmatch(stripped) is not None or stripped.startswith("!")):
                raise EnvVarResolutionError(_invalid_placeholder_message(raw_placeholder, location))
            parts.append(_restore_escapes(raw_placeholder))
            continue

        resolved = os.environ.get(name)
        if resolved is None or resolved == "":
            raise EnvVarResolutionError(
                f"Environment variable {name!r} is required by {location}, but it is not set. "
                f"Set {name} in the environment or remove the {{{{{name}}}}} placeholder."
            )
        parts.append(resolved)

    return "".join(parts)


def resolve_env_template_mapping(values: dict[str, Any], *, location: str) -> dict[str, str]:
    """Resolve placeholders in mapping values, coercing keys and values to strings."""
    return {
        str(key): resolve_env_templates(str(value), location=f"{location}[{key!r}]") for key, value in values.items()
    }


def resolve_env_template_values(value: Any, *, location: str, allow_commands: bool = True) -> Any:
    """Recursively resolve placeholders in string values within JSON-like data."""
    if isinstance(value, str):
        return resolve_env_templates(value, location=location, allow_commands=allow_commands)
    if isinstance(value, list):
        return [
            resolve_env_template_values(item, location=f"{location}[{index}]", allow_commands=allow_commands)
            for index, item in enumerate(value)
        ]
    if isinstance(value, dict):
        return {
            key: resolve_env_template_values(item, location=f"{location}[{key!r}]", allow_commands=allow_commands)
            for key, item in value.items()
        }
    return value
