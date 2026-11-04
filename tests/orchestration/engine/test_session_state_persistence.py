# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Session state sidecars across save, recovery checkpoint, restore, and reset: last words, manifest, breaker, workspace baseline, and todo list."""

from __future__ import annotations

import json
from collections.abc import Mapping
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import chrys.orchestration.engine.session_lifecycle as session_lifecycle
from chrys.foundation.config.settings import (
    Settings,
)
from chrys.foundation.config.settings_store import LoadedSettings
from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import (
    SessionRestore,
    SessionRestored,
)
from chrys.foundation.models.history_markers import HistoryMarkerKind
from chrys.foundation.models.workspace import Workspace
from chrys.foundation.trajectory.metadata import read_analytics_item_id
from chrys.kernel import LoopRecorder, Message
from chrys.orchestration.engine.assembly import assemble_agent_engine
from chrys.orchestration.engine.build.builder import _render_todo_reminder
from chrys.orchestration.engine.build.construction import StagedBuild
from chrys.orchestration.engine.build.loaded import CompletedBuild
from chrys.service.agent_middleware.injection import ConsumedInjection, InjectionAnchor
from chrys.service.agent_middleware.reminders.archive_pointer import CATALOG_POINTER_RECORD_COUNT_STATE_KEY
from chrys.service.context.compaction.last_words_state import DropRoundBreakerState, ManifestEntry
from chrys.service.context.compaction.spill import CATALOG_RELATIVE_PATH
from chrys.service.mutations.store import SnapshotStore
from chrys.service.mutations.tracker import MutationTracker
from chrys.service.profiles.agents.schema import (
    AgentProfile,
)
from chrys.service.state.store import JsonFileStateStore
from chrys.service.todos.tracker import TodoTracker
from tests.orchestration.engine._recovery_helpers import (
    _TODOS,
    _HistoryStateExecutor,
    _profile,
    _registry,
    _ResetExecutor,
    _seed_checkpoint_engine,
    _tracker_with_todos,
)
from tests.support.event_capture import collect_events
from tests.support.loaded_agents import install_loaded_agent, make_loaded_agent, reminder_resources


async def test_save_session_sets_and_pops_workspace_baseline(tmp_path: Path, *, engine_services) -> None:
    """The live history and disk cannot retain a stale truthy baseline."""
    store = JsonFileStateStore(tmp_path / "sessions")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    engine = assemble_agent_engine(EventBus(), settings=Settings(), state_store=store)
    engine.session.session_id = "workspace_baseline"
    engine.session.workspace = Workspace.from_cwd(str(workspace))
    engine_services(engine).workspace_change_tracker.retarget_roots(engine.session.workspace)
    engine_services(engine).workspace_change_tracker.capture_baseline(1)
    install_loaded_agent(
        engine,
        bindings=_HistoryStateExecutor(  # type: ignore[assignment]
            {"messages": [Message("user", ["hi"])], "compressed_msgs": [], "turn_counter": 1}
        ),
    )

    assert await engine.writer.save_current_session() is True
    assert "chrys_workspace_baseline" in engine.current.loaded.bindings.backend.history_state
    loaded = await store.load_session("workspace_baseline")
    assert loaded is not None and "chrys_workspace_baseline" in loaded

    engine_services(engine).workspace_change_tracker.invalidate()
    assert await engine.writer.save_current_session() is True
    assert "chrys_workspace_baseline" not in engine.current.loaded.bindings.backend.history_state
    loaded = await store.load_session("workspace_baseline")
    assert loaded is not None and "chrys_workspace_baseline" not in loaded


async def test_save_session_persists_last_words_note(tmp_path: Path) -> None:
    """The Phase 4 note lands in the saved session state — it is the only
    replacement for the compacted turn's dropped tool-call history — and a
    cleared note removes the stale key on the next save."""

    store = JsonFileStateStore(tmp_path)
    engine = assemble_agent_engine(EventBus(), settings=Settings(), state_store=store)
    engine.session.session_id = "lw_save"
    install_loaded_agent(
        engine,
        bindings=_HistoryStateExecutor(  # type: ignore[assignment]
            {"messages": [Message("user", ["hi"])], "compressed_msgs": [], "turn_counter": 1}
        ),
    )
    install_loaded_agent(engine, **reminder_resources())
    engine.current.loaded.last_words.set_last_words("[LAST_WORDS] resume from step 3")

    assert await engine.writer.save_current_session() is True
    loaded = await store.load_session("lw_save")
    assert loaded is not None
    assert loaded["last_words"] == "[LAST_WORDS] resume from step 3"

    engine.current.loaded.last_words.set_last_words(None)
    assert await engine.writer.save_current_session() is True
    loaded = await store.load_session("lw_save")
    assert loaded is not None
    assert "last_words" not in loaded


async def test_save_session_persists_manifest_and_breaker_without_note(tmp_path: Path) -> None:

    store = JsonFileStateStore(tmp_path)
    engine = assemble_agent_engine(EventBus(), settings=Settings(), state_store=store)
    engine.session.session_id = "lw_family_save"
    install_loaded_agent(
        engine,
        bindings=_HistoryStateExecutor(  # type: ignore[assignment]
            {"messages": [Message("user", ["hi"])], "compressed_msgs": [], "turn_counter": 1}
        ),
    )
    install_loaded_agent(engine, **reminder_resources())
    engine.current.loaded.last_words.append_manifest(
        [
            ManifestEntry(
                record_id="r1",
                group_id="g1",
                record_dir="compactions/dropped/turn001",
                relative_path="compactions/dropped/turn001/001_tool_r1.md",
                turn=1,
                round=1,
                sequence=1,
                tool="tool",
                display_argument="",
                outcome="ok",
                size_chars=10,
            )
        ]
    )
    breaker = DropRoundBreakerState(attempts=1, consecutive_no_progress=1, tail_override=True, side_call_tokens=42)
    engine.current.loaded.last_words.set_drop_round_breaker(breaker)
    engine.current.loaded.reminder_middleware.sources.archive_pointer.restore_record_count(0)

    assert await engine.writer.save_current_session() is True

    loaded = await store.load_session("lw_family_save")
    assert loaded is not None
    assert loaded["last_words_manifest"][0]["record_id"] == "r1"
    assert loaded["last_words_breaker"] == breaker.to_state()
    assert loaded[CATALOG_POINTER_RECORD_COUNT_STATE_KEY] == 0
    assert "last_words" not in loaded


async def test_save_erasure_protects_note_and_manifest_but_not_breaker(tmp_path: Path) -> None:
    store = JsonFileStateStore(tmp_path)
    engine = assemble_agent_engine(EventBus(), settings=Settings(), state_store=store)
    engine.session.session_id = "lw_erasure"
    manifest = [{"record_id": "r1", "relative_path": "compactions/dropped/turn001/a.md"}]
    install_loaded_agent(
        engine,
        bindings=_HistoryStateExecutor(  # type: ignore[assignment]
            {
                "messages": [Message("user", ["hi"])],
                "compressed_msgs": [],
                "turn_counter": 1,
                "last_words": "keep note",
                "last_words_manifest": manifest,
                "last_words_breaker": {"version": 1, "attempts": 7},
            }
        ),
    )
    last_words = SimpleNamespace(
        get_last_words=lambda: "keep note",
        get_last_words_manifest=lambda: manifest,
        get_last_words_breaker_state=lambda: None,
    )
    reminder = SimpleNamespace(
        sources=SimpleNamespace(archive_pointer=SimpleNamespace(record_count_state=lambda: None))
    )
    install_loaded_agent(engine, reminder_middleware=reminder, last_words=last_words)

    assert await engine.writer.save_current_session() is True

    loaded = await store.load_session("lw_erasure")
    assert loaded is not None
    assert loaded["last_words"] == "keep note"
    assert loaded["last_words_manifest"] == manifest
    assert "last_words_breaker" not in loaded


async def test_recovery_checkpoint_persists_last_words_note(tmp_path: Path) -> None:
    """The crash-recovery sidecar carries the Phase 4 note for the in-flight turn."""

    store = JsonFileStateStore(tmp_path)
    engine = _seed_checkpoint_engine(store, "lw_sidecar")
    install_loaded_agent(engine, **reminder_resources())
    engine.current.loaded.last_words.set_last_words("[LAST_WORDS] mid-turn progress")

    await engine.writer.save_checkpoint()
    await engine.writer.flush()

    loaded = await store.load_session("lw_sidecar", prefer_recovery=True)
    assert loaded is not None
    assert loaded["last_words"] == "[LAST_WORDS] mid-turn progress"


async def test_recovery_checkpoint_carries_manifest_and_breaker(tmp_path: Path) -> None:

    store = JsonFileStateStore(tmp_path)
    engine = _seed_checkpoint_engine(store, "lw_family_sidecar")
    install_loaded_agent(engine, **reminder_resources())
    entry = ManifestEntry(
        record_id="r1",
        group_id="g1",
        record_dir="compactions/dropped/turn001",
        relative_path="compactions/dropped/turn001/001_tool_r1.md",
        turn=1,
        round=1,
        sequence=1,
        tool="tool",
        display_argument="",
        outcome="unknown",
        size_chars=10,
    )
    engine.current.loaded.last_words.append_manifest([entry])
    breaker = DropRoundBreakerState(attempts=2, side_call_tokens=333)
    engine.current.loaded.last_words.set_drop_round_breaker(breaker)
    engine.current.loaded.reminder_middleware.sources.archive_pointer.restore_record_count(0)

    await engine.writer.save_checkpoint()
    await engine.writer.flush()

    loaded = await store.load_session("lw_family_sidecar", prefer_recovery=True)
    assert loaded is not None
    assert loaded["last_words_manifest"] == [entry.to_state()]
    assert loaded["last_words_breaker"] == breaker.to_state()
    assert loaded[CATALOG_POINTER_RECORD_COUNT_STATE_KEY] == 0


async def test_recovery_sidecar_keeps_new_same_text_injection_alongside_earlier_one(tmp_path: Path) -> None:
    """Identity-dedup end-to-end: sidecar keeps a new injection repeating old text.

    Resumed-turn shape: the turn region already holds a persisted injected
    "same note" (own ``_injection_id``); the user injects the same text again
    mid-run and the process would crash before finalization.  The recovery
    checkpoint replays the consumed-injection mirror — region-wide TEXT dedup
    would match the earlier note and drop the new one (user-input loss); the
    ``_injection_id`` stamp must land BOTH copies in the sidecar.
    """

    store = JsonFileStateStore(tmp_path)
    engine = assemble_agent_engine(EventBus(), settings=Settings(), state_store=store)
    engine.session.session_id = "inj-dup"
    opener = Message("user", ["task"])
    earlier = Message("user", ["same note"])
    earlier.additional_properties[HistoryMarkerKind.INJECTED_KEY] = True
    earlier.additional_properties[HistoryMarkerKind.INJECTION_ID_KEY] = "id-old"
    done = Message("assistant", ["done"])

    install_loaded_agent(
        engine,
        bindings=_HistoryStateExecutor(  # type: ignore[assignment]
            {"messages": [opener, earlier, done], "compressed_msgs": [], "turn_counter": 1}
        ),
    )
    capture = LoopRecorder()
    capture._initial_count = 3
    capture._captured = [opener, earlier, done]
    install_loaded_agent(engine, loop_recorder=capture)
    engine.turns.turn_state.set_current_input("task", None, None)
    engine.current.loaded.consumed_injections.append(
        ConsumedInjection(text="same note", anchor=InjectionAnchor.from_message(done), consumption_id="id-new")
    )

    await engine.writer.save_checkpoint()
    await engine.writer.flush()

    loaded = await store.load_session("inj-dup", prefer_recovery=True)
    assert loaded is not None
    notes = [m for m in loaded["messages"] if m.role == "user" and m.text == "same note"]
    assert len(notes) == 2
    assert [m.additional_properties.get(HistoryMarkerKind.INJECTION_ID_KEY) for m in notes] == ["id-old", "id-new"]
    assert all(m.additional_properties.get(HistoryMarkerKind.INJECTED_KEY) for m in notes)


async def test_restore_rearms_persisted_last_words_for_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A persisted LAST_WORDS note is re-armed on session restore: resuming the
    interrupted turn re-injects it into the next request, exactly like an
    in-process retry; it also survives a restore that is closed without a run."""

    profile = _profile()
    store = JsonFileStateStore(tmp_path)
    await store.save_session(
        "restore_lw",
        {
            "messages": [Message("user", ["primary"])],
            "compressed_msgs": [],
            "turn_counter": 1,
            "last_words": "[LAST_WORDS] resume from step 3",
            CATALOG_POINTER_RECORD_COUNT_STATE_KEY: 4,
        },
        agent_profile=profile.name,
    )
    events: list[SessionRestored] = []
    bus = EventBus()
    await bus.subscribe(SessionRestored, lambda event: collect_events(events, event))
    engine = assemble_agent_engine(bus, settings=Settings(), state_store=store, agent_registry=_registry(profile))

    async def fake_shutdown(*, keep_side_answers: bool = False) -> None:
        pass

    async def fake_start(
        start_profile: AgentProfile,
        *,
        operation: str = "startup",
        staged_loaded: LoadedSettings | None = None,
        session: Mapping[str, Any] | None = None,
        workspace: Workspace | None = None,
    ) -> None:
        _ = start_profile, operation
        install_loaded_agent(engine, bindings=_HistoryStateExecutor())  # type: ignore[assignment]
        install_loaded_agent(engine, **reminder_resources())

    monkeypatch.setattr(engine.lifecycle, "shutdown", fake_shutdown)
    monkeypatch.setattr(engine.lifecycle, "close_session", fake_shutdown)
    monkeypatch.setattr(engine.lifecycle, "close_session_in_place", fake_shutdown)
    monkeypatch.setattr(engine.lifecycle, "start", fake_start)

    try:
        await engine.on_session_restore(SessionRestore(session_id="restore_lw"))
    finally:
        engine.session.guard.release()

    assert len(events) == 1
    mw = engine.current.loaded.reminder_middleware
    lw = engine.current.loaded.last_words
    assert mw is not None
    # The note is visible to an idle save (restore → quit must not erase it)...
    assert lw.get_last_words() == "[LAST_WORDS] resume from step 3"
    # The pointer's turn-start count comes back with it, for that retry to reuse.
    assert mw.sources.archive_pointer.record_count_state() == 4
    # ...and a post-restart retry (Continue) injects it into the next request.
    mw.prepare_turn(usage={}, preserve_last_words=True)
    appended = lw.render()
    assert len(appended) == 1
    assert "[LAST_WORDS] resume from step 3" in appended[0]


async def test_restore_reconciles_spill_quota_manifest_and_availability(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    profile = _profile()
    store = JsonFileStateStore(tmp_path)
    session_id = "restore_lw_family"
    session_dir = store.session_dir(session_id)
    live_relative = "compactions/dropped/turn001/001_tool_11111111.md"
    missing_relative = "compactions/dropped/turn001/002_tool_22222222.md"
    live_path = session_dir / live_relative
    live_path.parent.mkdir(parents=True, exist_ok=True)
    live_path.write_text("record\n<!-- end of record -->\n", encoding="utf-8")
    orphan = live_path.with_name("orphan.md")
    orphan.write_text("partial", encoding="utf-8")
    manifest_projection = live_path.with_name("manifest.md")
    manifest_projection.write_text("stale projection", encoding="utf-8")
    catalog = session_dir / CATALOG_RELATIVE_PATH
    catalog.parent.mkdir(parents=True, exist_ok=True)
    catalog.write_text(
        "\n".join(
            json.dumps(
                {
                    "record_id": record_id,
                    "relative_path": relative_path,
                    "turn": 1,
                    "round": 1,
                    "tool": "tool",
                    "bytes": 999,
                    "created_at": "2026-01-01T00:00:00+00:00",
                }
            )
            for record_id, relative_path in (("r-live", live_relative), ("r-missing", missing_relative))
        )
        + "\n",
        encoding="utf-8",
    )

    def entry(record_id: str, relative_path: str, sequence: int) -> dict[str, Any]:
        return ManifestEntry(
            record_id=record_id,
            group_id=f"g-{sequence}",
            record_dir="compactions/dropped/turn001",
            relative_path=relative_path,
            turn=1,
            round=1,
            sequence=sequence,
            tool="tool",
            display_argument="",
            outcome="unknown",
            size_chars=10,
        ).to_state()

    breaker = DropRoundBreakerState(attempts=3, consecutive_no_progress=1, tail_override=True, side_call_tokens=777)
    await store.save_session(
        session_id,
        {
            "messages": [Message("user", ["primary"])],
            "compressed_msgs": [],
            "turn_counter": 1,
            "last_words_manifest": [entry("r-live", live_relative, 1), entry("r-missing", missing_relative, 2)],
            "last_words_breaker": breaker.to_state(),
        },
        agent_profile=profile.name,
    )
    engine = assemble_agent_engine(
        EventBus(),
        settings=Settings(),
        state_store=store,
        agent_registry=_registry(profile),
    )

    async def fake_shutdown(*, keep_side_answers: bool = False) -> None:
        pass

    async def fake_start(
        start_profile: AgentProfile,
        *,
        operation: str = "startup",
        staged_loaded: LoadedSettings | None = None,
        session: Mapping[str, Any] | None = None,
        workspace: Workspace | None = None,
    ) -> None:
        _ = start_profile, operation
        install_loaded_agent(engine, bindings=_HistoryStateExecutor())  # type: ignore[assignment]
        install_loaded_agent(
            engine,
            **reminder_resources(
                session_root=engine.session.session_dir,
                file_read_available=True,
            ),
        )

    monkeypatch.setattr(engine.lifecycle, "shutdown", fake_shutdown)
    monkeypatch.setattr(engine.lifecycle, "close_session", fake_shutdown)
    monkeypatch.setattr(engine.lifecycle, "close_session_in_place", fake_shutdown)
    monkeypatch.setattr(engine.lifecycle, "start", fake_start)

    try:
        await engine.on_session_restore(SessionRestore(session_id=session_id))
    finally:
        engine.session.guard.release()

    assert not orphan.exists()
    assert engine.session.spill_quota.spent_bytes == live_path.stat().st_size
    assert "stale projection" not in manifest_projection.read_text(encoding="utf-8")
    assert live_path.name in manifest_projection.read_text(encoding="utf-8")
    mw = engine.current.loaded.reminder_middleware
    lw = engine.current.loaded.last_words
    assert mw is not None
    mw.prepare_turn(usage={}, preserve_last_words=True)
    assert lw.get_drop_round_breaker() == breaker
    rendered = lw.render()[0]
    assert live_path.name in rendered
    assert "002_tool_22222222.md (record missing)" in rendered


def test_todo_tracker_property_is_read_only() -> None:
    """``todo_tracker`` mirrors ``mutation_tracker``: a read-only view of the
    private attribute for hosts (e.g. the ACP plan-update sender)."""
    engine = assemble_agent_engine(EventBus(), settings=Settings())
    assert engine.todo_tracker is None
    tracker = TodoTracker()
    engine.session.todo_tracker = tracker
    assert engine.todo_tracker is tracker
    with pytest.raises(AttributeError):
        engine.todo_tracker = TodoTracker()  # type: ignore[misc]


async def test_reset_for_restart_nulls_todo_tracker(tmp_path: Path) -> None:
    """No cross-session leak: a fresh start must not inherit the old list."""
    store = JsonFileStateStore(tmp_path)
    engine = assemble_agent_engine(EventBus(), settings=Settings(), state_store=store)
    engine.session.todo_tracker = await _tracker_with_todos(_TODOS)

    engine.lifecycle.reset_for_restart(None)

    assert engine.session.todo_tracker is None


def test_reset_for_restart_preserves_but_clears_workspace_tracker(tmp_path: Path, *, engine_services) -> None:
    engine = assemble_agent_engine(EventBus(), settings=Settings())
    workspace = Workspace.from_cwd(str(tmp_path))
    tracker = engine_services(engine).workspace_change_tracker
    tracker.retarget_roots(workspace)
    tracker.capture_baseline(1)
    tracker.queue_safety_notice("old safety")
    engine.session.mutation_tracker = MutationTracker(SnapshotStore(tmp_path / "mutations"))
    engine.session.todo_tracker = TodoTracker()

    engine.lifecycle.reset_for_restart(None)

    assert engine_services(engine).workspace_change_tracker is tracker
    assert tracker.baseline is None
    assert tracker.take_pending_notice() is None
    assert engine.session.mutation_tracker is None
    assert engine.session.todo_tracker is None


async def test_save_session_persists_todo_list(tmp_path: Path) -> None:
    """Save reads the TRACKER (not prior state): set when non-empty, and a
    cleared tracker pops the stale key on the next save (empty ≡ absent)."""
    store = JsonFileStateStore(tmp_path)
    engine = assemble_agent_engine(EventBus(), settings=Settings(), state_store=store)
    engine.session.session_id = "todo_save"
    install_loaded_agent(
        engine,
        bindings=_HistoryStateExecutor(  # type: ignore[assignment]
            {"messages": [Message("user", ["hi"])], "compressed_msgs": [], "turn_counter": 1}
        ),
    )
    engine.session.todo_tracker = await _tracker_with_todos(_TODOS)

    assert await engine.writer.save_current_session() is True
    loaded = await store.load_session("todo_save")
    assert loaded is not None
    assert loaded["chrys_todos"] == _TODOS

    await engine.session.todo_tracker.clear()
    assert await engine.writer.save_current_session() is True
    loaded = await store.load_session("todo_save")
    assert loaded is not None
    assert "chrys_todos" not in loaded


async def test_recovery_checkpoint_persists_todo_list(tmp_path: Path) -> None:
    """The crash-recovery sidecar carries the todo list for the in-flight turn."""
    store = JsonFileStateStore(tmp_path)
    engine = _seed_checkpoint_engine(store, "todo_sidecar")
    engine.session.todo_tracker = await _tracker_with_todos(_TODOS)

    await engine.writer.save_checkpoint()
    await engine.writer.flush()

    loaded = await store.load_session("todo_sidecar", prefer_recovery=True)
    assert loaded is not None
    assert loaded["chrys_todos"] == _TODOS


async def test_restore_hydrates_todo_tracker_before_last_words(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, engine_services
) -> None:
    """Restore hydrates ``chrys_todos`` into the tracker, and does so BEFORE
    ``restore_last_words`` — the restored note re-captures its todo section
    from the already-hydrated tracker."""

    profile = _profile()
    stamped_states = []
    stamp = session_lifecycle.stamp_history_item_ids

    def observe_stamping(state):
        stamp(state)
        stamped_states.append(state)

    monkeypatch.setattr(session_lifecycle, "stamp_history_item_ids", observe_stamping)
    store = JsonFileStateStore(tmp_path)
    await store.save_session(
        "restore_todo",
        {
            "messages": [Message("user", ["primary"])],
            "compressed_msgs": [],
            "turn_counter": 1,
            "chrys_todos": _TODOS,
            "last_words": "[LAST_WORDS] resume from step 3",
        },
        agent_profile=profile.name,
    )
    events: list[SessionRestored] = []
    bus = EventBus()
    await bus.subscribe(SessionRestored, lambda event: collect_events(events, event))
    engine = assemble_agent_engine(bus, settings=Settings(), state_store=store, agent_registry=_registry(profile))

    async def fake_shutdown(*, keep_side_answers: bool = False) -> None:
        pass

    async def fake_start(
        start_profile: AgentProfile,
        *,
        operation: str = "startup",
        staged_loaded: LoadedSettings | None = None,
        session: Mapping[str, Any] | None = None,
        workspace: Workspace | None = None,
    ) -> None:
        _ = start_profile, operation
        install_loaded_agent(engine, bindings=_HistoryStateExecutor())  # type: ignore[assignment]
        # Mirror the builder wiring: the provider reads the engine's tracker,
        # which _hydrate_restored_session must have populated already.
        install_loaded_agent(
            engine,
            **reminder_resources(
                todo_state_provider=partial(_render_todo_reminder, engine.session.todo_tracker),
            ),
        )

    monkeypatch.setattr(engine.lifecycle, "shutdown", fake_shutdown)
    monkeypatch.setattr(engine.lifecycle, "close_session", fake_shutdown)
    monkeypatch.setattr(engine.lifecycle, "close_session_in_place", fake_shutdown)
    monkeypatch.setattr(engine.lifecycle, "start", fake_start)

    try:
        await engine.on_session_restore(SessionRestore(session_id="restore_todo"))
    finally:
        engine.session.guard.release()

    assert stamped_states == [engine_services(engine).history.state]
    assert all(
        read_analytics_item_id(message.additional_properties) for message in engine_services(engine).history.messages
    )
    assert len(events) == 1
    tracker = engine.todo_tracker
    assert tracker is not None
    assert tracker.serialize() == _TODOS
    mw = engine.current.loaded.reminder_middleware
    lw = engine.current.loaded.last_words
    assert mw is not None
    mw.prepare_turn(usage={}, preserve_last_words=True)
    appended = lw.render()
    assert len(appended) == 1
    assert "[LAST_WORDS] resume from step 3" in appended[0]
    assert "- [>] implement" in appended[0]


async def test_restore_without_todos_leaves_tracker_empty(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Restoring a session without ``chrys_todos`` yields a fresh empty
    tracker (never ``None`` — the tracker is profile/state independent)."""
    profile = _profile()
    store = JsonFileStateStore(tmp_path)
    await store.save_session(
        "restore_no_todo",
        {"messages": [Message("user", ["primary"])], "compressed_msgs": [], "turn_counter": 1},
        agent_profile=profile.name,
    )
    engine = assemble_agent_engine(
        EventBus(), settings=Settings(), state_store=store, agent_registry=_registry(profile)
    )
    engine.session.todo_tracker = await _tracker_with_todos(_TODOS)  # stale previous-session state

    async def fake_shutdown(*, keep_side_answers: bool = False) -> None:
        pass

    async def fake_start(
        start_profile: AgentProfile,
        *,
        operation: str = "startup",
        staged_loaded: LoadedSettings | None = None,
        session: Mapping[str, Any] | None = None,
        workspace: Workspace | None = None,
    ) -> None:
        _ = start_profile, operation
        install_loaded_agent(engine, bindings=_HistoryStateExecutor())  # type: ignore[assignment]

    monkeypatch.setattr(engine.lifecycle, "shutdown", fake_shutdown)
    monkeypatch.setattr(engine.lifecycle, "close_session", fake_shutdown)
    monkeypatch.setattr(engine.lifecycle, "close_session_in_place", fake_shutdown)
    monkeypatch.setattr(engine.lifecycle, "start", fake_start)

    try:
        await engine.on_session_restore(SessionRestore(session_id="restore_no_todo"))
    finally:
        engine.session.guard.release()

    tracker = engine.todo_tracker
    assert tracker is not None
    assert tracker.snapshot() == ()


async def test_reset_session_to_welcome_delete_failure_restores_todo_tracker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, engine_services
) -> None:
    """A failed reset rehydrates the TRACKER, not just ``history_state`` —
    save reads the tracker, so an empty one would pop ``chrys_todos`` on the
    next save even with the key still present in the reattached state."""
    store = JsonFileStateStore(tmp_path)
    engine = assemble_agent_engine(EventBus(), settings=Settings(), state_store=store)
    engine.session.session_id = "reset_me"
    engine.session.agent_profile = _profile()
    engine.session.turn_number = 5
    session_dir = store.session_dir("reset_me")
    session_dir.mkdir(parents=True)
    (session_dir / "session.json").write_text("{}", encoding="utf-8")
    engine.session.todo_tracker = await _tracker_with_todos(_TODOS)

    install_loaded_agent(
        engine,
        bindings=_ResetExecutor(  # type: ignore[assignment]
            {"messages": [Message("user", ["saved"])], "compressed_msgs": [], "turn_counter": 5}
        ),
    )

    async def fake_build_agent(_profile: AgentProfile, staged: StagedBuild) -> CompletedBuild:
        return CompletedBuild(
            staged=staged,
            settings=engine.settings_handle.prepare(staged.loaded),
            workspace_retarget=engine_services(engine).workspace_change_tracker.resolve_retarget(
                staged.workspace,
                resolve_scope=engine.settings_handle.prepare(staged.loaded).effective.settings.workspace_change_notice,
            ),
            loaded=make_loaded_agent(
                bindings=_ResetExecutor({"messages": [], "compressed_msgs": [], "turn_counter": 0})
            ),
            manifest=engine.current.manifest,
            mutation_tracker=None,
            todo_tracker=None,
            compaction_strategy=None,
        )

    def fail_delete(_session_dir: Path) -> None:
        raise OSError("delete failed")

    monkeypatch.setattr(engine.loader, "build", fake_build_agent)
    monkeypatch.setattr(session_lifecycle, "_delete_reset_session_files", fail_delete)

    try:
        reset_succeeded = await engine.lifecycle.reset_session_to_welcome("reset_me")

        assert reset_succeeded is False
        tracker = engine.todo_tracker
        assert tracker is not None
        assert tracker.serialize() == _TODOS
        assert engine.current.loaded is not None
        assert engine.current.loaded.bindings.backend.history_state["chrys_todos"] == _TODOS
    finally:
        engine.session.guard.release()
