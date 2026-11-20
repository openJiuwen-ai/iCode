# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Every hook manager an engine builds, a chat session's or a workflow run's, asks through the engine's one service."""

from __future__ import annotations

import json

import pytest

from chrys.foundation.events.bus import EventBus
from chrys.foundation.platform import get_platform
from chrys.orchestration.session_hooks import SessionHookFactory
from chrys.service.hooks.confirmation import HookConfirmationService


@pytest.mark.asyncio
async def test_the_factory_attaches_the_engine_s_confirmation_service(tmp_path) -> None:
    hooks_dir = get_platform().config_dir / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    document = {
        "version": 1,
        "hooks": [{"id": "h", "event": "before_tool_call", "run": {"type": "command", "argv": ["true"]}}],
    }
    (hooks_dir / "hooks.json").write_text(json.dumps(document), encoding="utf-8")
    bus = EventBus()
    service = HookConfirmationService(bus=bus, enabled=False)

    managers = [
        await SessionHookFactory(bus, confirmations=service)(
            workdir=str(tmp_path), workdir_hooks_enabled=False, session_id=session_id
        )
        for session_id in ("chat", "workflow-run")
    ]

    assert all(manager is not None and manager.confirmations is service for manager in managers)
