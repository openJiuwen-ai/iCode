# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Pure conversation table-of-contents state for ChatPanel."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from chrys.app.tui.widgets.sidebar.toc_types import TocItem, summarize_prompt
from chrys.service.context.providers.history import TURN_INDEX_KEY


def turn_index_from_extra(extra: dict[str, Any]) -> int | None:
    """Return the backend turn index carried by a history marker."""
    turn_index = extra.get(TURN_INDEX_KEY)
    return turn_index if isinstance(turn_index, int) else None


@dataclass
class TurnTocModel:
    """Own turn ids, backend turn indexes, injections, and compression flags."""

    _turn_items: list[TocItem] = field(default_factory=list)
    _turn_counter: int = 0
    _backend_turn_counter: int = 0
    _injection_counter: int = 0

    def clear(self) -> None:
        """Reset all per-transcript TOC state."""
        self._turn_items.clear()
        self._turn_counter = 0
        self._backend_turn_counter = 0
        self._injection_counter = 0

    def has_items(self) -> bool:
        """Return whether any top-level turn has been recorded."""
        return bool(self._turn_items)

    def begin_user_turn(
        self, prompt: str, *, compressed: bool = False, turn_index: int | None = None
    ) -> tuple[str, TocItem]:
        """Create an opener; live turns wait for the backend's authoritative index."""
        self._turn_counter += 1
        turn_id = f"turn-{self._turn_counter}"
        item = TocItem(
            turn_id=turn_id,
            summary=summarize_prompt(prompt),
            compressed=compressed,
            turn_index=turn_index,
        )
        self._turn_items.append(item)
        return turn_id, item

    def begin_replayed_turn(self, prompt: str, *, compressed: bool = False) -> tuple[str, TocItem]:
        """Keep the positional display fallback for legacy histories without markers."""
        self._backend_turn_counter += 1
        return self.begin_user_turn(prompt, compressed=compressed, turn_index=self._backend_turn_counter)

    def add_injection(self, prompt: str, *, compressed: bool = False) -> tuple[str, TocItem | None]:
        """Create an injected prompt child under the latest top-level turn."""
        if not self._turn_items:
            return "", None
        self._injection_counter += 1
        turn_id = f"inj-{self._injection_counter}"
        item = TocItem(
            turn_id=turn_id,
            summary=summarize_prompt(prompt),
            compressed=compressed,
            turn_index=self._turn_items[-1].turn_index,
        )
        self._turn_items[-1].children.append(item)
        return turn_id, item

    def remove_failed_prompt(self, turn_id: str | None) -> None:
        """Remove the TOC entry for a deleted failed user prompt."""
        if not turn_id:
            return
        for index in range(len(self._turn_items) - 1, -1, -1):
            item = self._turn_items[index]
            if item.turn_id == turn_id:
                del self._turn_items[index]
                return
            for child_index in range(len(item.children) - 1, -1, -1):
                if item.children[child_index].turn_id == turn_id:
                    del item.children[child_index]
                    return

    def apply_turn_marker(self, turn_index: int | None) -> None:
        """Attach a persisted end-of-turn backend index to the latest TOC item."""
        if turn_index is None or not self._turn_items:
            return
        self._set_item_turn_index(self._turn_items[-1], turn_index)
        self._backend_turn_counter = max(self._backend_turn_counter, turn_index)

    def mark_turn_range_compressed(self, turn_range: tuple[int, int]) -> bool:
        """Mark live TOC entries in the backend turn range as compressed."""
        start, end = turn_range
        if start <= 0 or end < start:
            return False

        changed = False
        for item in self._turn_items:
            turn_index = item.turn_index
            if turn_index is not None and start <= turn_index <= end and self._mark_item_compressed(item):
                changed = True
        return changed

    def items(self) -> list[TocItem]:
        """Return a copy of the visible TOC entries."""
        return self._turn_items.copy()

    @classmethod
    def _mark_item_compressed(cls, item: TocItem) -> bool:
        changed = not item.compressed
        item.compressed = True
        for child in item.children:
            if cls._mark_item_compressed(child):
                changed = True
        return changed

    @classmethod
    def _set_item_turn_index(cls, item: TocItem, turn_index: int) -> None:
        item.turn_index = turn_index
        for child in item.children:
            cls._set_item_turn_index(child, turn_index)
