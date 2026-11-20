# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""JavaScript dialog records: type plus a redacted message, and their model text.

A JS dialog pauses the page's renderer, so while one is pending every action that
needs the renderer is refused (``BrowserSession.action_lock``) until it is
accepted/dismissed or the configured timeout dismisses it. Records never keep a
prompt's default value (``defaultPrompt``), which is where a page commonly puts a
secret, and the message itself is whitespace-normalised, secret-masked and
truncated before it reaches the model or the journal.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from typing import Any

#: Longest message kept after redaction (model context and journal).
MESSAGE_LIMIT = 400

#: Largest raw message text that is normalised; a page cannot make redaction expensive.
RAW_LIMIT = MESSAGE_LIMIT * 10

_SECRET_RE = re.compile(
    r"(?i)\b(password|passwd|token|secret|api[_-]?key|authorization|cookie)\b\s*[:=]\s*"
    r"(?:bearer|basic|token)?\s*\S+"
)
_SPACE_RE = re.compile(r"\s+")


def redact_message(raw: Any) -> str:
    """Collapse control/format characters, mask ``secret=value`` runs, truncate."""
    text = "".join(
        " " if unicodedata.category(ch) == "Cf" or ch.isspace() or ord(ch) < 32 or ord(ch) == 127 else ch
        for ch in str(raw or "")[:RAW_LIMIT]
    )
    text = _SPACE_RE.sub(" ", text).strip()
    text = _SECRET_RE.sub(lambda match: f"{match.group(1)}=***", text)
    if len(text) > MESSAGE_LIMIT:
        text = text[:MESSAGE_LIMIT] + f"… (+{len(text) - MESSAGE_LIMIT} chars)"
    return text


@dataclass
class DialogInfo:
    """One ``Page.javascriptDialogOpening`` record and how it ended (if it did)."""

    seq: int
    kind: str
    message: str
    url: str = ""
    page: str = ""
    session: str = ""
    closed: str = ""
    reason: str = ""
    prompt_chars: int = 0

    @property
    def pending(self) -> bool:
        return not self.closed

    def line(self) -> str:
        """One line: ``<kind>: <message>`` plus the closing state when known."""
        head = f"{self.kind}: {self.message}" if self.message else self.kind
        if not self.closed:
            return head
        tail = f" (closed: {self.closed}{'; ' + self.reason if self.reason else ''})"
        return head + tail

    def fields(self) -> str:
        """Machine-readable ``key=value`` spelling used in blocked-action results.

        The page-controlled message is JSON-quoted: an ``alert`` text that looks
        like ``state=... reason=...`` must not forge the surrounding fields.
        """
        quoted = json.dumps(self.message or "(empty)", ensure_ascii=False)
        parts = [f"dialog={self.kind}", f"message={quoted}"]
        if self.closed:
            parts.append(f"state={self.closed}")
        if self.reason:
            parts.append(f"reason={json.dumps(self.reason, ensure_ascii=False)}")
        return " ".join(parts)
