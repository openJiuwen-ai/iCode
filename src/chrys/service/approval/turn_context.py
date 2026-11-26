# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Shared current-turn user context for approval consumers."""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable

from chrys.foundation.util.time import parse_created_at, utc_iso


@dataclass(frozen=True, slots=True)
class QueuedApprovalInput:
    text: str
    submitted_at: str | None
    item_id: str


type ApprovalUserInput = str | QueuedApprovalInput

formal_user_inputs: ContextVar[tuple[ApprovalUserInput, ...] | None] = ContextVar("formal_user_inputs", default=None)


@runtime_checkable
class TurnContextReader(Protocol):
    """Read-only view consumed by approval judges."""

    @property
    def user_message(self) -> str: ...

    @property
    def user_messages(self) -> list[str]: ...

    @property
    def user_inputs(self) -> tuple[ApprovalUserInput, ...]: ...


class TurnContextHolder:
    """Mutable run-owned context shared by main and ACP approval paths."""

    def __init__(self) -> None:
        self._messages: list[ApprovalUserInput] = []

    @property
    def user_message(self) -> str:
        entry = self._messages[-1] if self._messages else ""
        return entry.text if isinstance(entry, QueuedApprovalInput) else entry

    @property
    def user_messages(self) -> list[str]:
        return [entry.text if isinstance(entry, QueuedApprovalInput) else entry for entry in self._messages]

    @property
    def user_inputs(self) -> tuple[ApprovalUserInput, ...]:
        return tuple(self._messages)

    def replace(self, messages: list[ApprovalUserInput]) -> None:
        self._messages = [message for message in messages if message]

    def append(self, text: str, *, created_at: datetime | str | None = None, item_id: str | None = None) -> None:
        if text:
            at = parse_created_at(created_at)
            self._messages.append(QueuedApprovalInput(text, utc_iso(at) if at else None, item_id) if item_id else text)

    def remove(self, text: str, *, item_id: str | None = None) -> None:
        for index in range(len(self._messages) - 1, -1, -1):
            entry = self._messages[index]
            if isinstance(entry, QueuedApprovalInput):
                match = entry.item_id == item_id if item_id is not None else entry.text == text
            else:
                match = item_id is None and entry == text
            if match:
                del self._messages[index]
                break

    def reset(self) -> None:
        self._messages.clear()
