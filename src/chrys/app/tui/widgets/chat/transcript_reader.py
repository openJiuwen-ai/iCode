# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Read and copy chat transcript text in document order."""

from __future__ import annotations

from textual.widget import Widget

from chrys.app.tui.widgets.chat.messages import AgentMessage, UserMessage
from chrys.app.tui.widgets.chat.ports import TranscriptReadPort


class TranscriptReader:
    """Build copy payloads from chat message widgets in document order.

    Cold entries can sit between mounted ones. Their document-order stamps
    establish their positions independently of viewport visits.
    """

    def __init__(self, query: TranscriptReadPort) -> None:
        self._query = query

    def get_agent_responses(self) -> list[tuple[str, str]]:
        """Return (profile_name, raw_text) of all agent messages in document order."""
        messages: list[AgentMessage] = []
        for child in self._document_children():
            if isinstance(child, AgentMessage):
                messages.append(child)
            elif child.is_attached:
                messages.extend(child.query(AgentMessage))
        return [(message.profile_name or "Agent", message.text) for message in messages if message.text]

    def get_user_messages(self) -> list[tuple[str, str]]:
        """Return (role_label, raw_text) of all user messages in document order."""
        return [
            ("You", child.text) for child in self._document_children() if isinstance(child, UserMessage) and child.text
        ]

    def get_all_messages(self) -> list[tuple[str, str]]:
        """Return (role_label, raw_text) of all user+agent messages in document order."""
        result: list[tuple[str, str]] = []
        for child in self._document_children():
            if isinstance(child, UserMessage) and child.text:
                result.append(("You", child.text))
            elif isinstance(child, AgentMessage) and child.text:
                result.append((child.profile_name or "Agent", child.text))
        return result

    def _document_children(self) -> list[Widget]:
        children = self._query.direct_children()
        pending = iter(self._query.pending_transcript_widgets())
        unmounted = next(pending, None)
        if unmounted is None:
            return children
        seq = self._query.transcript_seq(unmounted)
        document: list[Widget] = []
        for child in children:
            child_seq = self._query.transcript_seq(child)
            while unmounted is not None and seq is not None and child_seq is not None and seq < child_seq:
                document.append(unmounted)
                unmounted = next(pending, None)
                seq = self._query.transcript_seq(unmounted) if unmounted is not None else None
            document.append(child)
        if unmounted is not None:
            document.append(unmounted)
            document.extend(pending)
        return document
