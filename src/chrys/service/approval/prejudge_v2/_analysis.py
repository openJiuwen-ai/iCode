# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Parser-independent representation for the shared shell policy matcher."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tree_sitter import Node


def covered(node: Node, data: bytes, blanks: bytes) -> bool:
    """Require ordered child spans and account for every gap in the parent."""
    offset = node.start_byte
    for child in node.children:
        if (
            child.is_missing
            or not offset <= child.start_byte < child.end_byte <= node.end_byte
            or data[offset : child.start_byte].strip(blanks)
        ):
            return False
        offset = child.end_byte
    return not data[offset : node.end_byte].strip(blanks)


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
