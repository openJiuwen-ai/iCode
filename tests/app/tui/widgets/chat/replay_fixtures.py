# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Shared fixtures for transcript replay tests."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from chrys.foundation.models.history_markers import HistoryMarkerKind
from tests.support.waiting import DEFAULT_WAIT_TIMEOUT, wait_for, wait_until_quiet

if TYPE_CHECKING:
    import pytest
    from textual.widget import Widget

    from chrys.app.tui.widgets.chat.panel import ChatPanel

_TURNS = 36
_SIZE = (80, 24)


def _turns(
    count: int = _TURNS, *, with_tools: bool = False, leading_summary: str | None = None
) -> list[dict[str, object]]:
    messages: list[dict[str, object]] = []
    if leading_summary is not None:
        messages.append(
            {
                "role": "assistant",
                "contents": [{"type": "text", "text": f"[Compressed context: ctx-prefix]\nSummary: {leading_summary}"}],
                "additional_properties": {HistoryMarkerKind.KEY: HistoryMarkerKind.SUMMARY, "_block_id": "ctx-prefix"},
            }
        )
    for index in range(count):
        messages.append({"role": "user", "contents": [{"type": "text", "text": f"question {index}"}]})
        if with_tools:
            call_id = f"call-{index}"
            messages.append(
                {
                    "role": "assistant",
                    "contents": [
                        {
                            "type": "function_call",
                            "name": "read_file",
                            "call_id": call_id,
                            "arguments": {"path": f"/tmp/file_{index}.py"},
                        }
                    ],
                }
            )
            messages.append(
                {"role": "tool", "contents": [{"type": "function_result", "call_id": call_id, "result": "ok"}]}
            )
        messages.append(
            {"role": "assistant", "contents": [{"type": "text", "text": f"answer {index}\n\nmore about {index}"}]}
        )
        if index + 1 < count:
            messages.append(
                {
                    "role": "system",
                    "contents": [],
                    "additional_properties": {HistoryMarkerKind.KEY: HistoryMarkerKind.TURN},
                }
            )
    return messages


class _PrependGate:
    """Hold the background prepend right after its first batch mounted."""

    def __init__(self, panel: ChatPanel, monkeypatch: pytest.MonkeyPatch) -> None:
        self.release = asyncio.Event()
        self.prepended_batches = 0
        real = panel.mount_replay_batch

        async def gated(batch: list[Widget], *, before: Widget | None) -> None:
            await real(batch, before=before)
            if before is not None:
                self.prepended_batches += 1
                await self.release.wait()

        monkeypatch.setattr(panel, "mount_replay_batch", gated)

    async def wait_held(self, pilot: object) -> None:
        await wait_for(lambda: self.prepended_batches == 1, pilot=pilot, description="first prepend batch held")


async def _wait_complete(panel: ChatPanel) -> None:
    await asyncio.wait_for(panel.wait_replay_complete(), timeout=DEFAULT_WAIT_TIMEOUT)


async def _settle(panel: ChatPanel, pilot: object) -> None:
    await wait_until_quiet(
        lambda: (panel.virtual_size.height, panel.scroll_y),
        description="chat layout settled",
        pilot=pilot,
    )
