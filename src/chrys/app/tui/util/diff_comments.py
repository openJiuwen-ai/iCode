# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Draft contract and retained comments shared by the chat owner and Diff screen."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Literal, Protocol
from weakref import ReferenceType

from chrys.foundation.i18n import msg
from chrys.foundation.text.draft_references import DraftReference

type LineIdentity = tuple[Literal["before", "after"], int]
type CommentProbe = Literal["absent", "original", "changed"]
type CommentReplacement = Literal["success", "changed", "unavailable"]

# A quoted line as the comment bar and the quote preview name it.
QUOTED_BEFORE = msg("tui.diff_quote.before", fallback="Before · Line {line}")
QUOTED_AFTER = msg("tui.diff_quote.after", fallback="After · Line {line}")
QUOTED_IN_TURN = msg("tui.diff_quote.source.turn", fallback="Turn {turn}")
QUOTED_IN_SESSION = msg("tui.diff_quote.source.session", fallback="All turns")
CONTEXT_OMITTED = msg(
    "tui.diff_quote.context_omitted", fallback="The lines around this one are too long, so only this line is quoted."
)


def line_hash(raw: str) -> str:
    """Keep raw line equality without retaining another copy of an unbounded line."""
    return hashlib.sha256(raw.encode("utf-8", errors="surrogatepass")).hexdigest()


class DiffDraft(Protocol):
    """Synchronous edits of the live composer, without moving its focus."""

    def add_diff_quote(self, content: str, comment: str) -> DraftReference | None: ...
    def probe_diff_comment(self, handle: DraftReference) -> CommentProbe: ...
    def diff_comment(self, handle: DraftReference) -> str | None: ...
    def replace_diff_comment(self, handle: DraftReference, old: str, new: str) -> CommentReplacement: ...


@dataclass
class DiffComment:
    """A quote keeps its object identity even while its label is temporarily absent.

    The comment itself lives in the draft's reference table, so undo and redo keep it with the text.
    """

    handle: ReferenceType[DraftReference]
    tab: str
    path: str
    line: LineIdentity
    raw_hash: str
