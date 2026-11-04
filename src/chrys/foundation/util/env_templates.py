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
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

_ENV_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_ESCAPED_OPEN = "\0CHRYS_ENV_TEMPLATE_OPEN\0"
_ESCAPED_CLOSE = "\0CHRYS_ENV_TEMPLATE_CLOSE\0"

COMMAND_HANDLE_RE = re.compile(r"chrys-cmd-[0-9a-f]{32}")
_COMMAND_PLACEHOLDER_RE = re.compile(r"\{\{!.*?\}\}", re.DOTALL)


@dataclass(frozen=True, slots=True)
class CommandPlaceholder:
    """What a handle stands for: the command, and the setting that names it, for errors."""

    command: str
    location: str


_command_placeholders: dict[str, CommandPlaceholder] = {}
"""Only a handle resolved from a real placeholder is here: other text of that shape stays plain text."""


class EnvVarResolutionError(ValueError):
    """Raised when an environment-variable placeholder cannot be resolved."""


def _invalid_placeholder_message(raw: str, location: str) -> str:
    # This text can reach the model, and a command may hold a secret.
    shown = "{{!...}}" if raw.lstrip("{").lstrip().startswith("!") else raw
    return (
        f"Invalid environment variable placeholder {shown!r} in {location}. "
        "Use {{ENV_VAR}} with letters, digits, and underscores only, and no spaces, or {{!command}}."
    )


def _command_handle(body: str, *, raw: str, location: str, allow_commands: bool) -> str:
    """Return the handle of the ``{{!command}}`` whose text after the ``!`` is *body*."""
    command = body.replace(_ESCAPED_OPEN, "{{").replace(_ESCAPED_CLOSE, "}}").strip()
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


def blank_commands(value: str) -> str:
    """Return unresolved *value*, same length, with each ``{{!command}}`` blanked.

    For checking a draft against what can go on the wire: a command's text never
    does, and what it prints is checked when it runs.
    """
    return _COMMAND_PLACEHOLDER_RE.sub(lambda match: "x" * len(match.group()), value)


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
    later (*allow_commands* false), is an error.
    """
    value = value.replace("{{{{", _ESCAPED_OPEN).replace("}}}}", _ESCAPED_CLOSE)
    if "{{" not in value and "}}" not in value:
        return value.replace(_ESCAPED_OPEN, "{{").replace(_ESCAPED_CLOSE, "}}")

    parts: list[str] = []
    pos = 0
    while True:
        start = value.find("{{", pos)
        stray_end = value.find("}}", pos)
        if start == -1:
            if stray_end != -1:
                raise EnvVarResolutionError(_invalid_placeholder_message("}}", location))
            parts.append(value[pos:])
            break
        if stray_end != -1 and stray_end < start:
            raise EnvVarResolutionError(_invalid_placeholder_message("}}", location))

        end = value.find("}}", start + 2)
        if end == -1:
            raise EnvVarResolutionError(_invalid_placeholder_message(value[start:], location))

        parts.append(value[pos:start])
        name = value[start + 2 : end]
        raw_placeholder = value[start : end + 2]
        if name == "":
            raise EnvVarResolutionError(_invalid_placeholder_message(raw_placeholder, location))
        if name.startswith("!"):
            parts.append(
                _command_handle(name[1:], raw=raw_placeholder, location=location, allow_commands=allow_commands)
            )
            pos = end + 2
            continue
        if _ENV_NAME_RE.fullmatch(name) is None:
            stripped = name.strip()
            if stripped != name and (_ENV_NAME_RE.fullmatch(stripped) is not None or stripped.startswith("!")):
                raise EnvVarResolutionError(_invalid_placeholder_message(raw_placeholder, location))
            parts.append(raw_placeholder)
            pos = end + 2
            continue

        resolved = os.environ.get(name)
        if resolved is None or resolved == "":
            raise EnvVarResolutionError(
                f"Environment variable {name!r} is required by {location}, but it is not set. "
                f"Set {name} in the environment or remove the {{{{{name}}}}} placeholder."
            )
        parts.append(resolved)
        pos = end + 2

    return "".join(parts).replace(_ESCAPED_OPEN, "{{").replace(_ESCAPED_CLOSE, "}}")


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
