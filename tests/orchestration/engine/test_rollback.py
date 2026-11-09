# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Rollback snapshot write/prune, available turns, prompt previews, gap tolerance, overlays, and persistence."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import chrys.service.mutations.snapshot_files as snapshot_files_module
from chrys.foundation.config.settings import DEFAULT_ROLLBACK_SNAPSHOTS_KEEP, Settings
from chrys.foundation.events.types import UserRollback, Warning
from chrys.foundation.models.history_markers import HistoryMarkerKind
from chrys.kernel import Message
from chrys.orchestration.engine.rollback import capture_snapshot_writer
from chrys.service.mutations.store import SnapshotStore
from chrys.service.mutations.tracker import MutationTracker
from tests.orchestration.engine._rollback_helpers import (
    _collect_events,
    _make_engine,
    _state_after_turns,
    _turn_marker,
    _write_session_json,
    seed_turns,
)
from tests.support.loaded_agents import install_loaded_agent

# ===========================================================================
# Snapshot write / prune
# ===========================================================================


class TestSnapshotWrite:
    def test_write_is_noop_without_session(self, tmp_path: Path, *, engine_services) -> None:
        engine = _make_engine(tmp_path, engine_services=engine_services)
        engine.session.session_id = None  # no active session
        engine.rollback.write_snapshot()
        # Nothing should have been created
        assert not any(tmp_path.rglob("turn_*.json"))

    def test_write_is_noop_when_session_json_missing(self, tmp_path: Path, *, engine_services) -> None:
        engine = _make_engine(tmp_path, engine_services=engine_services)
        engine.session.turn_number = 2
        engine.rollback.write_snapshot()  # session.json doesn't exist yet
        assert not any(tmp_path.rglob("turn_*.json"))

    def test_write_copies_session_json_to_turn_n(self, tmp_path: Path, *, engine_services) -> None:
        engine = _make_engine(tmp_path, engine_services=engine_services)
        session_dir = tmp_path / "rb_test"
        session_file = session_dir / "session.json"
        _write_session_json(session_file)

        engine.session.turn_number = 3
        engine.rollback.write_snapshot()

        snap = session_dir / "snapshots" / "turn_3.json"
        assert snap.exists()
        # Content matches source
        assert json.loads(snap.read_text()) == json.loads(session_file.read_text())

    def test_captured_writer_freezes_snapshot_metadata(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, engine_services
    ) -> None:
        engine = _make_engine(tmp_path, engine_services=engine_services)
        session_dir = tmp_path / "rb_test"
        _write_session_json(session_dir / "session.json")
        engine.session.turn_number = 3
        path_calls: list[tuple[str, str]] = []
        real_session_dir_for = engine.session.session_dir_for
        real_lock_path_for = engine.session.session_write_lock_path

        def session_dir_for(session_id: str) -> Path:
            path_calls.append(("session", session_id))
            return real_session_dir_for(session_id)

        def lock_path_for(session_id: str) -> Path | None:
            path_calls.append(("lock", session_id))
            return real_lock_path_for(session_id)

        monkeypatch.setattr(engine.session, "session_dir_for", session_dir_for)
        monkeypatch.setattr(engine.session, "session_write_lock_path", lock_path_for)

        write_snapshot = capture_snapshot_writer(engine.session, engine.settings_handle)
        assert path_calls == []
        engine.session.session_id = "other-session"
        engine.session.turn_number = 9
        write_snapshot()

        assert path_calls == [("session", "rb_test"), ("lock", "rb_test")]
        assert (session_dir / "snapshots" / "turn_3.json").exists()
        assert not (tmp_path / "other-session" / "snapshots" / "turn_9.json").exists()

    def test_prune_honors_keep_last(self, tmp_path: Path, *, engine_services) -> None:
        engine = _make_engine(tmp_path, keep_last=3, engine_services=engine_services)
        session_dir = tmp_path / "rb_test"
        session_file = session_dir / "session.json"

        for turn in range(1, 8):  # turns 1..7
            _write_session_json(session_file, messages=[{"turn": turn}])
            engine.session.turn_number = turn
            engine.rollback.write_snapshot()

        snap_dir = session_dir / "snapshots"
        remaining = sorted(p.name for p in snap_dir.glob("turn_*.json"))
        assert remaining == ["turn_5.json", "turn_6.json", "turn_7.json"]


# ===========================================================================
# Available rollback turns
# ===========================================================================


class TestAvailableTurns:
    def test_empty_when_no_session_dir(self, tmp_path: Path, *, engine_services) -> None:
        engine = _make_engine(tmp_path, engine_services=engine_services)
        engine.session.session_id = None
        assert engine.available_rollback_turns() == []

    def test_includes_welcome_when_history_has_messages(self, tmp_path: Path, *, engine_services) -> None:
        engine = _make_engine(tmp_path, engine_services=engine_services)
        engine_services(engine).history.bind({"messages": [Message("user", ["hi"])]})
        # No snapshots on disk yet — only the welcome target (0).
        assert engine.available_rollback_turns() == [0]

    def test_includes_disk_snapshots_and_welcome(self, tmp_path: Path, *, engine_services) -> None:
        engine = _make_engine(tmp_path, engine_services=engine_services)
        session_dir = tmp_path / "rb_test"
        snap_dir = session_dir / "snapshots"
        snap_dir.mkdir(parents=True)
        # ``turn_M.json`` maps to ``target_turn = M - 1`` (keep M-1 turns).
        for n in (2, 3, 5):
            (snap_dir / f"turn_{n}.json").write_text("{}")
        # Give tracker at least one turn so welcome (0) is included
        engine.session.mutation_tracker = MutationTracker(SnapshotStore(session_dir))
        engine.session.mutation_tracker.start_turn(1)

        assert engine.available_rollback_turns() == [0, 1, 2, 4]

    def test_non_utf8_snapshot_falls_back_to_filename_target(self, tmp_path: Path, *, engine_services) -> None:
        engine = _make_engine(tmp_path, engine_services=engine_services)
        session_dir = tmp_path / "rb_test"
        snap_dir = session_dir / "snapshots"
        snap_dir.mkdir(parents=True)
        (snap_dir / "turn_3.json").write_bytes(b"\xff\xfe\x00")
        engine.session.mutation_tracker = MutationTracker(SnapshotStore(session_dir))
        engine.session.mutation_tracker.start_turn(1)

        assert engine.available_rollback_turns() == [0, 2]

    def test_includes_compressed_turns_when_snapshots_exist(self, tmp_path: Path, *, engine_services) -> None:
        """Turns folded into a :class:`CompressedBlock` stay eligible.

        Snapshots are written at the *start* of each turn, so
        ``turn_N.json`` predates any compression that happened during
        turn N or later.  Restoring such a snapshot un-compresses those
        folded turns back into live history, and the Context panel
        rebuilds from the restored ``compressed_msgs``.  Every turn
        with a snapshot on disk should therefore be offered.
        """
        from chrys.service.context.providers.history import CompressedBlock

        engine = _make_engine(tmp_path, engine_services=engine_services)
        session_dir = tmp_path / "rb_test"
        snap_dir = session_dir / "snapshots"
        snap_dir.mkdir(parents=True)
        for n in (2, 3, 4, 5):
            (snap_dir / f"turn_{n}.json").write_text("{}")
        engine.session.mutation_tracker = MutationTracker(SnapshotStore(session_dir))
        engine.session.mutation_tracker.start_turn(1)

        # Live state shows turns 2..3 as compressed — they must still
        # appear in the picker because their pre-turn snapshots exist.
        engine_services(engine).history.bind(
            {
                "messages": [Message("user", ["hi"])],
                "compressed_msgs": [
                    CompressedBlock(
                        compressed_context_id="ctx_x",
                        summary_text="summary",
                        marker_id="turn_3",
                        turn_range=(2, 3),
                    ),
                ],
            }
        )

        # turn_{2..5}.json → keep {1..4} turns; plus welcome (0).
        assert engine.available_rollback_turns() == [0, 1, 2, 3, 4]


class TestTurnPromptPreviews:
    """``engine.turn_prompt_previews()`` is the single source of truth for
    turn → user-prompt mapping shown in the rollback picker."""

    def test_maps_user_prompts_to_turn_indices(self, tmp_path: Path, *, engine_services) -> None:
        engine = _make_engine(tmp_path, engine_services=engine_services)

        # Build a realistic history: user → (assistant work) → turn marker → ...
        engine_services(engine).history.bind(
            {
                "messages": [
                    Message("user", ["hello turn one"]),
                    Message("assistant", ["ok"]),
                    _turn_marker(1),
                    Message("user", ["second question"]),
                    Message("assistant", ["sure"]),
                    _turn_marker(2),
                ],
                "compressed_msgs": [],
            }
        )
        assert engine.turn_prompt_previews() == {
            1: "hello turn one",
            2: "second question",
        }

    def test_returns_empty_when_not_bound(self, tmp_path: Path, *, engine_services) -> None:
        engine = _make_engine(tmp_path, engine_services=engine_services)
        # Reset history to unbound state
        engine_services(engine).history._state = None
        assert engine.turn_prompt_previews() == {}

    def test_flagged_nudge_never_labels_rollback_preview(self, tmp_path: Path, *, engine_services) -> None:
        """§2.4: a crash-leftover synthetic ``continue`` nudge is skipped by
        ``scan_turn_prompts`` — a nudge-only turn region gets NO label rather
        than a fabricated "continue", while injected guidance (real user
        content) still labels its turn."""
        engine = _make_engine(tmp_path, engine_services=engine_services)

        nudge_only = Message("user", ["continue"])
        nudge_only.additional_properties[HistoryMarkerKind.CONTINUATION_KEY] = True
        nudge_before_guidance = Message("user", ["continue"])
        nudge_before_guidance.additional_properties[HistoryMarkerKind.CONTINUATION_KEY] = True
        guidance = Message("user", ["retry with flag X"])
        guidance.additional_properties[HistoryMarkerKind.INJECTED_KEY] = True

        engine_services(engine).history.bind(
            {
                "messages": [
                    Message("user", ["real question"]),
                    Message("assistant", ["ok"]),
                    _turn_marker(1),
                    nudge_only,
                    Message("assistant", ["resumed work"]),
                    _turn_marker(2),
                    nudge_before_guidance,
                    guidance,
                    Message("assistant", ["guided work"]),
                    _turn_marker(3),
                ],
                "compressed_msgs": [],
            }
        )

        previews = engine.turn_prompt_previews()
        assert previews[1] == "real question"
        assert 2 not in previews
        assert previews[3] == "retry with flag X"

    def test_first_rolled_back_user_text_returns_earliest_discarded_prompt(
        self, tmp_path: Path, *, engine_services
    ) -> None:
        engine = _make_engine(tmp_path, engine_services=engine_services)
        engine_services(engine).history.bind(_state_after_turns(10))

        assert engine.first_rolled_back_user_text(5) == "user 6"

    def test_reads_previews_from_compressed_blocks(self, tmp_path: Path, *, engine_services) -> None:
        """Compressed turns preserve their originals inside the block.

        Once a turn is folded into a :class:`CompressedBlock`, its
        messages leave live history — but the block keeps deep copies
        with intact turn markers.  The picker still needs previews for
        those turns so it can label compressed rollback targets with
        the original user prompt, so :meth:`turn_prompt_previews` must
        walk each block's ``messages`` too.
        """
        from chrys.service.context.providers.history import CompressedBlock

        engine = _make_engine(tmp_path, engine_services=engine_services)

        # Turns 1..2 are folded away — their originals now live only
        # inside the block.  Turn 3 remains in live history.
        block = CompressedBlock(
            compressed_context_id="ctx_x",
            messages=[
                Message("user", ["first prompt"]),
                Message("assistant", ["ok"]),
                _turn_marker(1),
                Message("user", ["second prompt"]),
                Message("assistant", ["sure"]),
                _turn_marker(2),
            ],
            summary_text="summary",
            marker_id="turn_2",
            turn_range=(1, 2),
        )
        summary_msg = Message("assistant", ["[Compressed context]"])
        summary_msg.additional_properties[HistoryMarkerKind.KEY] = HistoryMarkerKind.SUMMARY
        summary_msg.additional_properties["_block_id"] = "ctx_x"

        engine_services(engine).history.bind(
            {
                "messages": [
                    summary_msg,
                    Message("user", ["third prompt"]),
                    Message("assistant", ["done"]),
                    _turn_marker(3),
                ],
                "compressed_msgs": [block],
            }
        )

        assert engine.turn_prompt_previews() == {
            1: "first prompt",
            2: "second prompt",
            3: "third prompt",
        }

    def test_live_history_wins_over_block_on_overlap(self, tmp_path: Path, *, engine_services) -> None:
        """Live-first scan order means live entries are never overwritten.

        Shouldn't happen in practice (compression moves messages out),
        but the invariant guards against future regressions.
        """
        from chrys.service.context.providers.history import CompressedBlock

        engine = _make_engine(tmp_path, engine_services=engine_services)

        block = CompressedBlock(
            compressed_context_id="ctx_x",
            messages=[
                Message("user", ["stale prompt"]),
                _turn_marker(1),
            ],
            summary_text="summary",
            marker_id="turn_1",
            turn_range=(1, 1),
        )
        engine_services(engine).history.bind(
            {
                "messages": [
                    Message("user", ["live prompt"]),
                    _turn_marker(1),
                ],
                "compressed_msgs": [block],
            }
        )

        # Live scan runs first and fills turn 1; block scan must not overwrite it.
        assert engine.turn_prompt_previews() == {1: "live prompt"}


class TestSnapshotGapTolerance:
    """Regression: every snapshot-touching code path must tolerate gaps.

    A user (or an external tool) may delete individual ``turn_N.json``
    files manually.  The rollback logic must never assume contiguous
    snapshot ranges — enumeration is always glob-based, and the
    mutation-tracker revert counts turn IDs strictly greater than the
    target (see ``_on_user_rollback`` comment about "tolerates gaps
    in the tracker's turn IDs").  This test exercises a non-contiguous
    layout end-to-end.
    """

    def test_available_turns_skips_deleted_snapshots(self, tmp_path: Path, *, engine_services) -> None:
        engine = _make_engine(tmp_path, engine_services=engine_services)
        session_dir = tmp_path / "rb_test"
        snap_dir = session_dir / "snapshots"
        snap_dir.mkdir(parents=True)
        # User deleted turn_2 and turn_4; turn_3 and turn_5 survive.
        for n in (3, 5):
            (snap_dir / f"turn_{n}.json").write_text("{}")
        engine.session.mutation_tracker = MutationTracker(SnapshotStore(session_dir))
        engine.session.mutation_tracker.start_turn(1)

        # turn_3.json → keep 2 turns; turn_5.json → keep 4 turns; + welcome 0.
        assert engine.available_rollback_turns() == [0, 2, 4]

    async def test_rollback_to_non_contiguous_surviving_turn(self, tmp_path: Path, *, engine_services) -> None:
        """Pick a surviving snapshot whose neighbours are gone — must still swap cleanly."""
        engine = _make_engine(tmp_path, engine_services=engine_services)
        engine_services(engine).history.bind(
            {"messages": [Message(role="user", contents=["hi"])], "compressed_msgs": []}
        )

        session_dir = tmp_path / "rb_test"
        session_file = session_dir / "session.json"
        snap_dir = session_dir / "snapshots"
        snap_dir.mkdir(parents=True)

        # Layout: snapshots 3 and 5 exist (2 and 4 deleted by user).
        (snap_dir / "turn_3.json").write_text(
            json.dumps({"meta": {"session_id": "rb_test"}, "state": {"messages": ["t3"], "compressed_msgs": []}})
        )
        (snap_dir / "turn_5.json").write_text(
            json.dumps({"meta": {"session_id": "rb_test"}, "state": {"messages": ["t5"], "compressed_msgs": []}})
        )
        # Current live session.json (post-turn-5 state).
        session_file.write_text(
            json.dumps({"meta": {"session_id": "rb_test"}, "state": {"messages": ["live"], "compressed_msgs": []}})
        )

        async def _fake_restore(_event: Any) -> None: ...

        engine.lifecycle.on_session_restore = _fake_restore  # type: ignore[assignment]

        # target_turn=2 means "keep 2 turns" → restore turn_3.json.
        # turn_4.json is absent (no target=3); turn_5.json exists (target=4).
        await engine._on_user_rollback(UserRollback(target_turn=2, revert_changes=False))

        restored = json.loads(session_file.read_text(encoding="utf-8"))
        assert restored["state"]["messages"] == ["t3"]

        # Post-swap cleanup keeps the promoted turn_3 snapshot as the
        # recovery anchor for the new current state, but removes newer
        # rollback targets.  Absent turn_4 should not cause errors.
        assert (snap_dir / "turn_3.json").exists()
        assert not (snap_dir / "turn_5.json").exists()

    async def test_rollback_to_deleted_snapshot_is_refused(self, tmp_path: Path, *, engine_services) -> None:
        """Picking a target whose snapshot was deleted surfaces a Warning, not a crash."""
        engine = _make_engine(tmp_path, engine_services=engine_services)
        engine_services(engine).history.bind(
            {"messages": [Message(role="user", contents=["hi"])], "compressed_msgs": []}
        )

        session_dir = tmp_path / "rb_test"
        snap_dir = session_dir / "snapshots"
        snap_dir.mkdir(parents=True)
        # Only turn_3.json exists → available targets are {0, 2}.
        (snap_dir / "turn_3.json").write_text("{}")

        warnings: list[Warning] = []
        await _collect_events(engine.event_bus, Warning, warnings)

        # target_turn=1 would need turn_2.json which is missing.
        await engine._on_user_rollback(UserRollback(target_turn=1, revert_changes=False))
        await asyncio.sleep(0)

        assert any(w.code == "rollback_unavailable" for w in warnings)


class TestRollbackSessionOverlayPreservation:
    """The custom title and last surface are session-scoped and must survive a snapshot restore."""

    def test_read_and_reapply_session_overlays(self, tmp_path: Path) -> None:
        from chrys.orchestration.engine.rollback import _read_session_overlays, _reapply_session_overlays

        session_file = tmp_path / "session.json"
        session_file.write_text(
            json.dumps(
                {
                    "meta": {
                        "title": "first msg",
                        "custom_title": "Pinned",
                        "generated_title": "Auto",
                        "last_surface": "tui",
                    },
                    "state": {},
                }
            ),
            encoding="utf-8",
        )
        overlays = _read_session_overlays(session_file)
        assert overlays == {"custom_title": "Pinned", "last_surface": "tui"}

        # Simulate the snapshot restore wiping the overlays wholesale.
        session_file.write_text(
            json.dumps({"meta": {"title": "first msg", "last_surface": "cli"}, "state": {}}), encoding="utf-8"
        )
        _reapply_session_overlays(session_file, overlays)
        meta = json.loads(session_file.read_text(encoding="utf-8"))["meta"]
        assert meta["custom_title"] == "Pinned"
        assert meta["last_surface"] == "tui"
        assert "generated_title" not in meta
        assert meta["title"] == "first msg"

    def test_generated_title_from_snapshot_wins_on_rollback(self, tmp_path: Path) -> None:
        """The current generated title summarizes turns the rollback discards;
        the snapshot's own value is the one describing the restored history."""
        from chrys.orchestration.engine.rollback import _read_session_overlays, _reapply_session_overlays

        session_file = tmp_path / "session.json"
        session_file.write_text(
            json.dumps({"meta": {"title": "x", "generated_title": "New topic"}, "state": {}}),
            encoding="utf-8",
        )
        overlays = _read_session_overlays(session_file)

        session_file.write_text(
            json.dumps({"meta": {"title": "x", "generated_title": "Old topic"}, "state": {}}),
            encoding="utf-8",
        )
        _reapply_session_overlays(session_file, overlays)
        meta = json.loads(session_file.read_text(encoding="utf-8"))["meta"]
        assert meta["generated_title"] == "Old topic"

    def test_read_overlays_tolerates_missing_or_invalid_file(self, tmp_path: Path) -> None:
        from chrys.orchestration.engine.rollback import _read_session_overlays

        assert _read_session_overlays(tmp_path / "missing.json") == {}
        bad = tmp_path / "bad.json"
        bad.write_text("{not json", encoding="utf-8")
        assert _read_session_overlays(bad) == {}

    def test_cleared_custom_title_survives_rollback(self, tmp_path: Path) -> None:
        """An explicit empty custom_title (user cleared the pin) must override a snapshot's old pin."""
        from chrys.orchestration.engine.rollback import _read_session_overlays, _reapply_session_overlays

        session_file = tmp_path / "session.json"
        session_file.write_text(
            json.dumps({"meta": {"title": "x", "custom_title": "", "generated_title": "Auto"}, "state": {}}),
            encoding="utf-8",
        )
        overlays = _read_session_overlays(session_file)
        assert overlays == {"custom_title": ""}

        # Snapshot from before the clear still carries the pin.
        session_file.write_text(
            json.dumps({"meta": {"title": "x", "custom_title": "Pinned"}, "state": {}}),
            encoding="utf-8",
        )
        _reapply_session_overlays(session_file, overlays)
        meta = json.loads(session_file.read_text(encoding="utf-8"))["meta"]
        assert meta["custom_title"] == ""

    @pytest.mark.parametrize(("sidecar_hour", "expected"), [(11, "acp"), (9, "cli")], ids=["newer", "older"])
    def test_the_surface_of_a_newer_sidecar_is_the_one_kept(
        self, tmp_path: Path, sidecar_hour: int, expected: str
    ) -> None:
        """A sidecar newer than the primary holds the session's last turn, cut short by a crash."""
        from chrys.orchestration.engine.rollback import _read_session_overlays

        def envelope(hour: int, surface: str) -> str:
            updated_at = f"2026-09-01T{hour:02d}:00:00+00:00"
            return json.dumps({"meta": {"title": "x", "updated_at": updated_at, "last_surface": surface}, "state": {}})

        session_file = tmp_path / "session.json"
        session_file.write_text(envelope(10, "cli"), encoding="utf-8")
        (tmp_path / "session.recovery.json").write_text(envelope(sidecar_hour, "acp"), encoding="utf-8")

        assert _read_session_overlays(session_file) == {"last_surface": expected}

    def test_a_session_that_never_recorded_a_surface_keeps_the_snapshots(self, tmp_path: Path) -> None:
        """Only a surface the current session recorded outranks the snapshot's."""
        from chrys.orchestration.engine.rollback import _read_session_overlays, _reapply_session_overlays

        session_file = tmp_path / "session.json"
        session_file.write_text(json.dumps({"meta": {"title": "x"}, "state": {}}), encoding="utf-8")
        overlays = _read_session_overlays(session_file)
        assert overlays == {}

        session_file.write_text(
            json.dumps({"meta": {"title": "x", "last_surface": "acp"}, "state": {}}), encoding="utf-8"
        )
        _reapply_session_overlays(session_file, overlays)
        meta = json.loads(session_file.read_text(encoding="utf-8"))["meta"]
        assert meta["last_surface"] == "acp"


# ---------------------------------------------------------------------------
# Cross-session coordination re-check at rollback
# ---------------------------------------------------------------------------


class TestRollbackCoordinatorRecheck:
    """The destructive path must hand the coordinator its discovery inputs.

    Outside a git repo the registry is keyed by the workspace fallback
    root — without it, the rollback-time peer re-check silently finds
    no peer files at all.
    """

    async def test_recheck_passes_workspace_fallback_and_scope(self, tmp_path: Path, *, engine_services) -> None:
        engine = _make_engine(tmp_path, engine_services=engine_services)
        engine_services(engine).history.bind(
            {"messages": [Message(role="user", contents=["hi"])], "compressed_msgs": []}
        )

        session_dir = tmp_path / "rb_test"
        session_file = session_dir / "session.json"
        snap_dir = session_dir / "snapshots"
        snap_dir.mkdir(parents=True)
        (snap_dir / "turn_2.json").write_text(
            json.dumps({"meta": {"session_id": "rb_test"}, "state": {"messages": ["t2"], "compressed_msgs": []}})
        )
        session_file.write_text(
            json.dumps({"meta": {"session_id": "rb_test"}, "state": {"messages": ["live"], "compressed_msgs": []}})
        )

        engine.session.mutation_tracker = MutationTracker(SnapshotStore(session_dir))
        engine.session.mutation_tracker.start_turn(2)

        calls: list[tuple[str, object, object]] = []

        class _RecordingCoordinator:
            def reclassify(self, tracker, *, force=False, fallback_root=None):
                calls.append(("reclassify", force, fallback_root))
                return False

            def augment_rollback_plan(self, tracker, plan, *, scope_paths=None, fallback_root=None):
                calls.append(("augment", scope_paths, fallback_root))
                return plan

        engine.session.mutation_coordinator = _RecordingCoordinator()  # type: ignore[assignment]

        async def _fake_restore(_event: Any) -> None: ...

        engine.lifecycle.on_session_restore = _fake_restore  # type: ignore[assignment]

        await engine._on_user_rollback(UserRollback(target_turn=1, revert_changes=True))

        cwd = engine.session.workspace_cwd()
        assert ("reclassify", True, cwd) in calls
        assert ("augment", [cwd], cwd) in calls


class TestAttributionRefreshPersistence:
    """The engine refresh must save when the coordinator carries unsaved
    reclassification changes — a finalize-time reclassify has no saver
    and updates the short-circuit signatures, so this very refresh call
    reports "unchanged" while session.json is behind.
    """

    async def test_unsaved_finalize_reclassification_forces_save(self, tmp_path: Path, *, engine_services) -> None:
        engine = _make_engine(tmp_path, engine_services=engine_services)
        engine.session.mutation_tracker = MutationTracker(SnapshotStore(tmp_path / "rb_test"))

        saves: list[bool] = []

        async def _record_save(*, raise_on_error: bool = False) -> bool:
            saves.append(True)
            return True

        engine.writer.save_current_session = _record_save  # type: ignore[assignment]

        class _StubCoordinator:
            def __init__(self) -> None:
                self.dirty = True

            def reclassify(self, tracker, *, force=False, fallback_root=None):
                return False  # signatures already updated by the finalize-time run

            def consume_unsaved_reclassification(self):
                dirty, self.dirty = self.dirty, False
                return dirty

        engine.session.mutation_coordinator = _StubCoordinator()  # type: ignore[assignment]

        assert await engine.refresh_mutation_attribution() is False
        assert saves == [True]  # saved despite changed=False
        assert await engine.refresh_mutation_attribution() is False
        assert saves == [True]  # flag consumed — no redundant save

    async def test_mid_run_refresh_defers_save_to_turn_end(self, tmp_path: Path, *, engine_services) -> None:
        """A mid-run primary save would delete the recovery sidecar — the only
        durable copy of committed-but-unmerged tool exchanges — so the refresh
        must leave the unsaved flag set and defer persistence to the turn-end
        save."""
        engine = _make_engine(tmp_path, engine_services=engine_services)
        engine.session.mutation_tracker = MutationTracker(SnapshotStore(tmp_path / "rb_test"))

        saves: list[bool] = []

        async def _record_save(*, raise_on_error: bool = False) -> bool:
            saves.append(True)
            return True

        engine.writer.save_current_session = _record_save  # type: ignore[assignment]

        class _StubCoordinator:
            def __init__(self) -> None:
                self.dirty = True

            def reclassify(self, tracker, *, force=False, fallback_root=None):
                return True

            def consume_unsaved_reclassification(self):
                dirty, self.dirty = self.dirty, False
                return dirty

        coordinator = _StubCoordinator()
        engine.session.mutation_coordinator = coordinator  # type: ignore[assignment]
        install_loaded_agent(engine, bindings=SimpleNamespace(state=SimpleNamespace(running=True)))  # type: ignore[assignment]

        assert await engine.refresh_mutation_attribution() is True
        assert saves == []
        assert coordinator.dirty is True  # flag preserved for the turn-end save

        install_loaded_agent(engine, bindings=SimpleNamespace(state=SimpleNamespace(running=False)))  # type: ignore[assignment]
        assert await engine.refresh_mutation_attribution() is True
        assert saves == [True]


async def test_retention_window_keeps_session_start_reachable(tmp_path: Path, *, engine_services) -> None:
    """Pruning hides older turns, but the session start needs no snapshot and stays a target."""
    engine = _make_engine(tmp_path, keep_last=5, engine_services=engine_services)
    store = await seed_turns(engine, 6, engine_services=engine_services)
    assert engine.available_rollback_turns() == [0, 1, 2, 3, 4, 5]
    engine.session.turn_number = 7
    await asyncio.to_thread(engine.rollback.write_snapshot)
    assert engine.available_rollback_turns() == [0, 2, 3, 4, 5, 6]
    warnings: list[Warning] = []
    await _collect_events(engine.event_bus, Warning, warnings)

    await engine._on_user_rollback(UserRollback(target_turn=0, revert_changes=False))

    assert "rollback_unavailable" not in [event.code for event in warnings]
    assert not (store.session_dir("rb_test") / "session.json").exists()


def test_empty_session_has_no_targets_even_with_snapshot_files(tmp_path: Path, *, engine_services) -> None:
    engine = _make_engine(tmp_path, engine_services=engine_services)
    directory = tmp_path / "rb_test" / "snapshots"
    directory.mkdir(parents=True)
    (directory / "turn_2.json").write_text('{"state":{"turn_counter":1}}', encoding="utf-8")
    engine_services(engine).history.bind({"messages": [], "turn_counter": 0})

    assert engine.available_rollback_turns() == []


async def test_repeated_target_refresh_decodes_each_snapshot_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, engine_services
) -> None:
    engine = _make_engine(tmp_path, engine_services=engine_services)
    store = await seed_turns(engine, 3, engine_services=engine_services)
    snapshots = sorted((store.session_dir("rb_test") / "snapshots").glob("*.json"))
    originals = {path.read_text(encoding="utf-8") for path in snapshots}
    parsed: list[str] = []

    def decode(value: str | bytes | bytearray) -> Any:
        if isinstance(value, str) and value in originals:
            parsed.append(value)
        return json.loads(value)

    monkeypatch.setattr(
        snapshot_files_module, "json", SimpleNamespace(loads=decode, JSONDecodeError=json.JSONDecodeError)
    )

    assert engine.available_rollback_turns() == [0, 1, 2]
    assert engine.available_rollback_turns() == [0, 1, 2]
    assert len(parsed) == len(snapshots)
    assert set(parsed) == originals

    changed = json.loads(snapshots[-1].read_bytes())
    changed["state"]["turn_counter"] = 1
    changed["meta"]["cache_invalidation"] = "changed size and content"
    snapshots[-1].write_text(json.dumps(changed), encoding="utf-8")
    assert engine.available_rollback_turns() == [0, 1]


def test_default_snapshot_retention_is_twenty() -> None:
    assert DEFAULT_ROLLBACK_SNAPSHOTS_KEEP == 20
    assert Settings().rollback_snapshots_keep == 20
