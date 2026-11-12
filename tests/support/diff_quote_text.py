# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The text a Diff quote sends the model, spelled out from the design rather than from the code that writes it."""

from __future__ import annotations


def diff_quote_text(
    path: str,
    version: str,
    scope: str,
    quoted: int,
    lines: list[tuple[int, str, str]],
    fence: str = "```",
) -> str:
    """*lines* are ``(number, mark, text)``; the line numbered *quoted* gets ``>``."""
    width = len(str(max(number for number, _mark, _text in lines)))
    target = f'Quoted line: {quoted}, marked with ">"'
    if len(lines) > 1:
        target += "; the lines around it are context"
    body = [f"{'>' if number == quoted else ' '} {number:>{width}} {mark} {text}" for number, mark, text in lines]
    return "\n".join(
        [
            f"File: {path} (as it was {version} the changes made in {scope}; line numbers count in that version)",
            target,
            fence,
            *body,
            fence,
        ]
    )
