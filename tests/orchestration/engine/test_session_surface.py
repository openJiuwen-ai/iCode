# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Only a turn records its launch's surface on the session; opening and saving keep the recorded one."""

from __future__ import annotations

import json
from pathlib import Path

from chrys.foundation.config.settings import Settings
from chrys.foundation.events.bus import EventBus
from chrys.foundation.models.session_surface import SessionSurface
from chrys.kernel import Message
from chrys.orchestration.engine.assembly import assemble_agent_engine
from chrys.orchestration.engine.engine import AgentEngine
from chrys.service.state.store import JsonFileStateStore
from tests.orchestration.engine._recovery_helpers import _HistoryStateExecutor
from tests.support.loaded_agents import install_loaded_agent

_STATE = {"messages": [Message("user", ["hi"])], "compressed_msgs": [], "turn_counter": 1}


def _engine(store: JsonFileStateStore, session_id: str, surface: SessionSurface | None) -> AgentEngine:
    engine = assemble_agent_engine(EventBus(), settings=Settings(), state_store=store, surface=surface)
    engine.session.session_id = session_id
    install_loaded_agent(engine, bindings=_HistoryStateExecutor(dict(_STATE)))  # type: ignore[assignment]
    return engine


async def _surface(store: JsonFileStateStore, session_id: str) -> SessionSurface | None:
    meta = await store.load_session_meta(session_id)
    assert meta is not None
    return meta.last_surface


async def test_saves_without_a_turn_keep_the_recorded_surface(tmp_path: Path) -> None:
    store = JsonFileStateStore(tmp_path)
    await store.save_session("s", dict(_STATE), last_surface=SessionSurface.ACP)
    engine = _engine(store, "s", SessionSurface.TUI)

    assert await engine.writer.save_current_session() is True
    assert await _surface(store, "s") is SessionSurface.ACP

    # What ``pre_run`` does for every fresh and retry turn.
    engine.session.mark_surface()
    assert await engine.writer.save_current_session() is True
    assert await _surface(store, "s") is SessionSurface.TUI


async def test_a_fresh_session_records_nothing_until_its_first_turn(tmp_path: Path) -> None:
    store = JsonFileStateStore(tmp_path)
    engine = _engine(store, "fresh", SessionSurface.CLI)

    assert await engine.writer.save_current_session() is True
    assert await _surface(store, "fresh") is None
    envelope = json.loads((store.session_dir("fresh") / "session.json").read_text(encoding="utf-8"))
    assert "last_surface" not in envelope["meta"]


async def test_a_launch_without_a_surface_never_records_one(tmp_path: Path) -> None:
    store = JsonFileStateStore(tmp_path)
    await store.save_session("s", dict(_STATE), last_surface=SessionSurface.CLI)
    engine = _engine(store, "s", None)

    engine.session.mark_surface()
    assert engine.session.marked_surface() is None
    assert await engine.writer.save_current_session() is True
    assert await _surface(store, "s") is SessionSurface.CLI


async def test_reopening_a_session_retires_its_mark(tmp_path: Path) -> None:
    """Another launch may work in a session while this one is elsewhere; reopening it is not a turn."""
    store = JsonFileStateStore(tmp_path)
    engine = _engine(store, "a", SessionSurface.TUI)
    engine.session.mark_surface()
    captured = engine.writer.session_identity()

    engine.session.adopt_restore_identity(session_id="b", recovered_from_sidecar=False)
    assert engine.session.marked_surface() is None
    engine.session.adopt_restore_identity(session_id="a", recovered_from_sidecar=False)
    assert engine.session.marked_surface() is None
    # A recovery snapshot captured before the switch still writes the surface of its turn.
    assert captured.session_id == "a"
    assert captured.metadata["last_surface"] is SessionSurface.TUI

    engine.session.mark_surface()
    engine.session.reset(session_id="c")
    assert engine.session.marked_surface() is None


async def test_a_mark_never_follows_a_changed_session_id(tmp_path: Path) -> None:
    store = JsonFileStateStore(tmp_path)
    await store.save_session("other", dict(_STATE), last_surface=SessionSurface.ACP)
    engine = _engine(store, "mine", SessionSurface.CLI)
    engine.session.mark_surface()

    engine.session.session_id = "other"
    assert engine.session.marked_surface() is None
    assert await engine.writer.save_current_session() is True
    assert await _surface(store, "other") is SessionSurface.ACP
