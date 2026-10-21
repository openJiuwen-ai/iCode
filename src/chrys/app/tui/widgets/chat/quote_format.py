# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Display helpers for quoted conversation selections.

The wire format itself — header, fence, budgets, :class:`Quote`, composing
and parsing — lives in :mod:`chrys.foundation.text.conversation_selections`
so the engine's user-authored mention scanning can share it; the names are
re-exported here for the TUI layers that started importing this module.
What stays here is presentation: the localized source labels the transcript
bubble and the quote-manager dialog share, and the one-line preview.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from chrys.foundation.i18n import msg
from chrys.foundation.text.conversation_selections import (
    MAX_PENDING_QUOTES as MAX_PENDING_QUOTES,
)
from chrys.foundation.text.conversation_selections import (
    SELECTIONS_FENCE as SELECTIONS_FENCE,
)
from chrys.foundation.text.conversation_selections import (
    SELECTIONS_HEADER as SELECTIONS_HEADER,
)
from chrys.foundation.text.conversation_selections import (
    SINGLE_QUOTE_MAX_CHARS as SINGLE_QUOTE_MAX_CHARS,
)
from chrys.foundation.text.conversation_selections import (
    SOURCE_ASSISTANT as SOURCE_ASSISTANT,
)
from chrys.foundation.text.conversation_selections import (
    SOURCE_USER as SOURCE_USER,
)
from chrys.foundation.text.conversation_selections import (
    TOTAL_QUOTE_MAX_CHARS as TOTAL_QUOTE_MAX_CHARS,
)
from chrys.foundation.text.conversation_selections import Quote as Quote
from chrys.foundation.text.conversation_selections import (
    compose_outgoing_text as compose_outgoing_text,
)
from chrys.foundation.text.conversation_selections import (
    selections_span as selections_span,
)
from chrys.foundation.text.conversation_selections import (
    split_selections_block as split_selections_block,
)

if TYPE_CHECKING:
    from chrys.foundation.i18n import MessageRef

# Display labels for the wire sources; shared by the transcript bubble view
# and the quote-manager dialog, which live in different TUI layers.
SOURCE_USER_LABEL = msg("tui.quote.source_user", fallback="User message")
SOURCE_ASSISTANT_LABEL = msg("tui.quote.source_assistant", fallback="Assistant message")
SOURCE_UNKNOWN_LABEL = msg("tui.quote.source_unknown", fallback="Conversation")

_PREVIEW_MAX_CHARS = 80


def source_label_reference(source: str | None) -> MessageRef:
    """The localized label for a quote's wire ``source`` value."""
    if source == SOURCE_USER:
        return SOURCE_USER_LABEL.bind()
    if source == SOURCE_ASSISTANT:
        return SOURCE_ASSISTANT_LABEL.bind()
    return SOURCE_UNKNOWN_LABEL.bind()


def quote_preview(text: str, max_chars: int = _PREVIEW_MAX_CHARS) -> str:
    """Collapse *text* into one short line for chip and dialog rows."""
    collapsed = " ".join(text.split())
    if len(collapsed) <= max_chars:
        return collapsed
    return collapsed[: max_chars - 1].rstrip() + "…"
