# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""ACP session replay must keep user image attachments, not only text."""

from __future__ import annotations

from typing import Any

from acp import schema as acp_schema

from chrys.app.acp.history import replay_session_history
from chrys.kernel import Content, Message
from chrys.service.state.store import JsonFileStateStore


class _FakeClient:
    def __init__(self) -> None:
        self.updates: list[Any] = []

    async def session_update(self, session_id: str, update: Any, **kwargs: Any) -> None:
        self.updates.append(update)


async def test_replay_session_history_keeps_user_image_content(tmp_path) -> None:
    image = Content.from_data(data=b"\x89PNG\r\n\x1a\n" + b"0" * 16, media_type="image/png")
    store = JsonFileStateStore(tmp_path / "sessions")
    await store.save_session(
        "s1",
        {"messages": [Message("user", ["look at this", image])]},
        agent_profile="Code",
        primary_cwd=str(tmp_path),
    )
    client = _FakeClient()

    await replay_session_history(client, store, "s1")

    user_updates = [u for u in client.updates if u.session_update == "user_message_chunk"]
    assert any(
        isinstance(u.content, acp_schema.ImageContentBlock) for u in user_updates
    ), user_updates
