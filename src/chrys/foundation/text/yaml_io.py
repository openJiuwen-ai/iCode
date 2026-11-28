# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Shared YAML dump helper — readable output for human-edited profile files.

``yaml.dump``'s default behaviour wraps long / multi-line strings into
double-quoted scalars with ``\\n`` escapes.  Agent and model profile YAMLs
are meant to be opened and hand-edited by users, so we override string
serialisation to use the literal block style (``|``) whenever a string
contains a newline.  The result is natural, line-broken text with no
escape noise.

Scoped to a custom ``Dumper`` subclass so the representer does not leak
into other ``yaml.dump`` call sites in the process.
"""

from __future__ import annotations

from typing import Any

import yaml


class _BlockStyleDumper(yaml.SafeDumper):
    """SafeDumper variant that emits multi-line strings in block style."""


# YAML's reader treats NEL and the Unicode line/paragraph separators as line
# breaks in every scalar style, so a literal block would silently turn them
# into "\n" on the next load.  Emit such strings double-quoted instead: that
# style escapes them as ``\N`` / ``\L`` / ``\P`` and round-trips faithfully.
_BLOCK_STYLE_UNSAFE = "\x85\u2028\u2029"


def _str_representer(dumper: yaml.SafeDumper, data: str) -> yaml.ScalarNode:
    """Use ``|`` for multi-line strings; leave single-line strings to the default."""
    if any(char in data for char in _BLOCK_STYLE_UNSAFE):
        return dumper.represent_scalar("tag:yaml.org,2002:str", data, style='"')
    if "\n" in data:
        # ``|`` preserves line breaks verbatim and is trivial for users to
        # edit.  Strip trailing whitespace per line to avoid YAML's
        # "chomping"-style surprises where trailing spaces change meaning.
        # Split on "\n" alone: ``str.splitlines`` also breaks on vertical
        # tab, form feed, NEL and the Unicode separators, rewriting them into
        # real newlines before the emitter ever sees them.  The trailing empty
        # element keeps a final newline intact, so no extra append is needed.
        cleaned = "\n".join(line.rstrip() for line in data.split("\n"))
        return dumper.represent_scalar("tag:yaml.org,2002:str", cleaned, style="|")
    return dumper.represent_scalar("tag:yaml.org,2002:str", data)


_BlockStyleDumper.add_representer(str, _str_representer)


def dump_yaml(data: Any) -> str:
    """Serialise *data* to YAML using human-friendly block-style strings.

    Args:
        data: Any ``yaml.dump``-compatible mapping / sequence / scalar.

    Returns:
        YAML text with ``default_flow_style=False``, keys in insertion
        order, unicode preserved, and multi-line strings rendered with
        ``|`` block scalars.
    """
    return yaml.dump(
        data,
        Dumper=_BlockStyleDumper,
        default_flow_style=False,
        allow_unicode=True,
        sort_keys=False,
    )
