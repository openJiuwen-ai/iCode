# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Cross-session mutation coordination tests.

Peer sessions are simulated by writing registry files directly — the
registry file format IS the cross-process protocol, so tests exercising
hand-written peer files cover exactly what a real concurrent session
would produce.
"""

from __future__ import annotations

import json
import os
import time

import pytest

from chrys.foundation.models.invocations import InvocationOrigin
from chrys.service.mutations.coordination import (
    ATTRIBUTION_DIR_NAME,
    PEER_ACTIVE_WINDOW_WARNING,
    REGISTRY_SCHEMA,
    MutationClaim,
    MutationCoordinator,
    ObservationWindow,
    canonical_key,
    parse_registry_payload,
    repo_key_for_root,
    resolve_tree_root,
)
from chrys.service.mutations.store import SnapshotStore
from chrys.service.mutations.tracker import MutationTracker
from chrys.service.mutations.types import (
    FileMutation,
    FileSnapshot,
    MutationOp,
    MutationProvenance,
    MutationSource,
    RollbackExclusionReason,
    RollbackPlan,
    SnapshotSkipReason,
    TurnMutations,
)

# A pid that cannot exist on any supported platform (pid_max caps well
# below this everywhere we run CI).
DEAD_PID = 2**22 + 12345


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_coordinator(tmp_path, session_id="sess-a", **kwargs):
    return MutationCoordinator(
        registry_root=tmp_path / ATTRIBUTION_DIR_NAME,
        session_id=session_id,
        **kwargs,
    )


def make_tree(tmp_path, name="repo", git="dir"):
    """Create a fake working tree with a .git directory or file."""
    root = tmp_path / name
    root.mkdir(parents=True, exist_ok=True)
    if git == "dir":
        (root / ".git").mkdir(exist_ok=True)
    elif git == "file":
        (root / ".git").write_text("gitdir: /elsewhere/.git/worktrees/x")
    return root


def make_mutation(
    path,
    *,
    provenance=MutationProvenance.ASSUMED,
    source=MutationSource.SHELL,
    op=MutationOp.MODIFY,
    before_hash="aaa",
    after_hash="bbb",
    before_skip=None,
    after_skip=None,
    old_path=None,
    t_start=None,
    t_end=None,
    timestamp=None,
):
    return FileMutation(
        path=str(path),
        operation=op,
        source=source,
        tool_call_id="call-1",
        timestamp=timestamp if timestamp is not None else time.time(),
        old_path=str(old_path) if old_path else None,
        before_hash=before_hash,
        after_hash=after_hash,
        before_skip=before_skip,
        after_skip=after_skip,
        provenance=provenance,
        t_start=t_start,
        t_end=t_end,
    )


def make_tracker(tmp_path, mutations):
    tracker = MutationTracker(SnapshotStore(tmp_path / "session"))
    tracker.with_locked_log(lambda log: log.turns.append(TurnMutations(turn_id=1, mutations=list(mutations))))
    return tracker


def write_peer_file(
    registry_root,
    tree_root,
    session_id,
    *,
    claims=(),
    windows=(),
    pid=DEAD_PID,
    closed_at=None,
    schema=REGISTRY_SCHEMA,
):
    tree_dir = registry_root / repo_key_for_root(str(tree_root))
    tree_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "session_id": session_id,
        "pid": pid,
        "schema": schema,
        "closed_at": closed_at,
        "windows": [w.to_dict() for w in windows],
        "claims": [c.to_dict() for c in claims],
    }
    target = tree_dir / f"{session_id}.json"
    target.write_text(json.dumps(payload))
    return target


def peer_claim(path, *, t0, t1, after_hash="bbb", after_exists=True, old_path=None):
    return MutationClaim(
        path=str(path),
        key=canonical_key(str(path)),
        after_hash=after_hash,
        after_exists=after_exists,
        t0=t0,
        t1=t1,
        source="write_file",
        old_path=str(old_path) if old_path else None,
        old_key=canonical_key(str(old_path)) if old_path else None,
    )


def read_own_file(registry_root, tree_root, session_id):
    path = registry_root / repo_key_for_root(str(tree_root)) / f"{session_id}.json"
    return json.loads(path.read_text())


def sole_mutation(tracker):
    return tracker.with_locked_log(lambda log: log.turns[0].mutations[0])


# ---------------------------------------------------------------------------
# FileMutation interval schema
# ---------------------------------------------------------------------------


class TestIntervalSchema:
    def test_roundtrip_and_legacy_rows_default_to_none(self):
        m = make_mutation("/x/f.py", t_start=1.5, t_end=2.5)
        d = m.to_dict()
        assert (d["t_start"], d["t_end"]) == (1.5, 2.5)
        m2 = FileMutation.from_dict(d)
        assert (m2.t_start, m2.t_end) == (1.5, 2.5)
        legacy = {k: v for k, v in d.items() if k not in ("t_start", "t_end")}
        m3 = FileMutation.from_dict(legacy)
        assert m3.t_start is None
        assert m3.t_end is None

    def test_omitted_when_unset(self):
        d = make_mutation("/x/f.py").to_dict()
        assert "t_start" not in d
        assert "t_end" not in d


# ---------------------------------------------------------------------------
# Tree resolution
# ---------------------------------------------------------------------------


class TestTreeResolution:
    def test_git_directory(self, tmp_path):
        root = make_tree(tmp_path, git="dir")
        nested = root / "src" / "pkg"
        nested.mkdir(parents=True)
        assert resolve_tree_root(str(nested)) == str(root)

    def test_git_file_worktree(self, tmp_path):
        root = make_tree(tmp_path, git="file")
        assert resolve_tree_root(str(root / "anything.py")) == str(root)

    def test_no_repo(self, tmp_path):
        bare = tmp_path / "no-repo"
        bare.mkdir()
        assert resolve_tree_root(str(bare)) is None

    def test_repo_key_is_stable_and_short(self, tmp_path):
        root = make_tree(tmp_path)
        key = repo_key_for_root(str(root))
        assert key == repo_key_for_root(str(root))
        assert len(key) == 16


# ---------------------------------------------------------------------------
# Write side: windows, claims, close
# ---------------------------------------------------------------------------


class TestWriteSide:
    def test_open_window_publishes_then_close_stamps_end(self, tmp_path):
        root = make_tree(tmp_path)
        coord = make_coordinator(tmp_path)
        window_id = coord.open_window([str(root)])
        assert window_id

        data = read_own_file(tmp_path / ATTRIBUTION_DIR_NAME, root, "sess-a")
        assert data["schema"] == REGISTRY_SCHEMA
        assert data["closed_at"] is None
        (window,) = data["windows"]
        assert window["id"] == window_id
        assert window["end"] is None

        coord.close_window(window_id)
        data = read_own_file(tmp_path / ATTRIBUTION_DIR_NAME, root, "sess-a")
        (window,) = data["windows"]
        assert window["end"] is not None
        assert window["end"] >= window["start"]

    def test_open_window_outside_any_tree_uses_fallback_root(self, tmp_path):
        bare = tmp_path / "no-repo"
        bare.mkdir()
        coord = make_coordinator(tmp_path)
        assert coord.open_window([str(bare)]) is None
        window_id = coord.open_window([str(bare)], fallback_root=str(bare))
        assert window_id
        data = read_own_file(tmp_path / ATTRIBUTION_DIR_NAME, bare, "sess-a")
        assert data["windows"]

    def test_proven_changed_mutation_publishes_claim(self, tmp_path):
        root = make_tree(tmp_path)
        target = root / "f.py"
        coord = make_coordinator(tmp_path)
        m = make_mutation(
            target,
            provenance=MutationProvenance.PROVEN,
            source=MutationSource.WRITE_FILE,
            t_start=100.0,
            t_end=101.0,
        )
        assert coord.publish_claims([m]) == 1
        data = read_own_file(tmp_path / ATTRIBUTION_DIR_NAME, root, "sess-a")
        (claim,) = data["claims"]
        assert claim["path"] == str(target)
        assert claim["key"] == canonical_key(str(target))
        assert claim["after_hash"] == "bbb"
        assert claim["after_exists"] is True
        assert (claim["t0"], claim["t1"]) == (100.0, 101.0)

    def test_assumed_mutation_never_publishes(self, tmp_path):
        root = make_tree(tmp_path)
        coord = make_coordinator(tmp_path)
        m = make_mutation(root / "f.py", provenance=MutationProvenance.ASSUMED, t_start=1.0, t_end=2.0)
        assert coord.publish_claims([m]) == 0

    def test_net_zero_mutation_never_publishes(self, tmp_path):
        root = make_tree(tmp_path)
        coord = make_coordinator(tmp_path)
        m = make_mutation(
            root / "f.py",
            provenance=MutationProvenance.PROVEN,
            source=MutationSource.WRITE_FILE,
            before_hash="same",
            after_hash="same",
        )
        assert coord.publish_claims([m]) == 0

    def test_fully_skipped_modify_never_publishes(self, tmp_path):
        # Both hashes withheld by SnapshotPolicy: hash equality (None ==
        # None) plus no existence flip — proves nothing, publishes nothing.
        root = make_tree(tmp_path)
        coord = make_coordinator(tmp_path)
        m = make_mutation(
            root / "big.bin",
            provenance=MutationProvenance.PROVEN,
            source=MutationSource.WRITE_FILE,
            before_hash=None,
            after_hash=None,
            before_skip=SnapshotSkipReason.TOO_LARGE,
            after_skip=SnapshotSkipReason.TOO_LARGE,
        )
        assert coord.publish_claims([m]) == 0

    def test_existence_flip_with_skip_publishes(self, tmp_path):
        # CREATE of a policy-skipped file: no after hash, but existence
        # flipped — that much IS proven.
        root = make_tree(tmp_path)
        coord = make_coordinator(tmp_path)
        m = make_mutation(
            root / "big.bin",
            provenance=MutationProvenance.PROVEN,
            source=MutationSource.WRITE_FILE,
            op=MutationOp.CREATE,
            before_hash=None,
            after_hash=None,
            after_skip=SnapshotSkipReason.TOO_LARGE,
        )
        assert coord.publish_claims([m]) == 1
        data = read_own_file(tmp_path / ATTRIBUTION_DIR_NAME, root, "sess-a")
        assert data["claims"][0]["after_exists"] is True
        assert data["claims"][0]["after_hash"] is None

    def test_claims_capped_fifo(self, tmp_path):
        root = make_tree(tmp_path)
        coord = make_coordinator(tmp_path, max_claims=3)
        for i in range(5):
            m = make_mutation(
                root / f"f{i}.py",
                provenance=MutationProvenance.PROVEN,
                source=MutationSource.WRITE_FILE,
                after_hash=f"h{i}",
            )
            coord.publish_claims([m])
        data = read_own_file(tmp_path / ATTRIBUTION_DIR_NAME, root, "sess-a")
        assert [c["after_hash"] for c in data["claims"]] == ["h2", "h3", "h4"]

    def test_close_stamps_closed_at_and_open_windows(self, tmp_path):
        root = make_tree(tmp_path)
        coord = make_coordinator(tmp_path)
        coord.open_window([str(root)])
        coord.close()
        data = read_own_file(tmp_path / ATTRIBUTION_DIR_NAME, root, "sess-a")
        assert data["closed_at"] is not None
        assert data["windows"][0]["end"] is not None
        # Idempotent.
        coord.close()

    def test_straggler_writes_after_close_keep_closed_stamp(self, tmp_path):
        # An in-flight finalize can race close() (engine shutdown, or the
        # settings-reload teardown): its close_window / publish_claims
        # rewrite the registry file AFTER the close stamp landed, and
        # close() never re-stamps (idempotence guard) — the stamp must be
        # sticky or peers read a live pid as "active" until GC.
        root = make_tree(tmp_path)
        coord = make_coordinator(tmp_path)
        window_id = coord.open_window([str(root)])
        coord.close()
        closed_at = read_own_file(tmp_path / ATTRIBUTION_DIR_NAME, root, "sess-a")["closed_at"]
        assert closed_at is not None

        coord.close_window(window_id)
        m = make_mutation(
            root / "late.py",
            provenance=MutationProvenance.PROVEN,
            source=MutationSource.WRITE_FILE,
            after_hash="late",
        )
        assert coord.publish_claims([m]) == 1

        data = read_own_file(tmp_path / ATTRIBUTION_DIR_NAME, root, "sess-a")
        assert data["closed_at"] == closed_at
        # The straggler's claim itself still lands — it records a real
        # write peers may need for a late rollback.
        assert [c["after_hash"] for c in data["claims"]] == ["late"]

    def test_disabled_coordinator_is_inert(self, tmp_path):
        root = make_tree(tmp_path)
        coord = make_coordinator(tmp_path, enabled=False)
        m = make_mutation(root / "f.py", provenance=MutationProvenance.PROVEN, source=MutationSource.WRITE_FILE)
        assert coord.open_window([str(root)]) is None
        assert coord.publish_claims([m]) == 0
        coord.close()
        assert not (tmp_path / ATTRIBUTION_DIR_NAME).exists()

    def test_resume_merges_prior_file_and_closes_stale_windows(self, tmp_path):
        root = make_tree(tmp_path)
        registry_root = tmp_path / ATTRIBUTION_DIR_NAME
        old_claim = peer_claim(root / "old.py", t0=1.0, t1=2.0, after_hash="old")
        stale_window = ObservationWindow(id="w-stale", scope=[str(root)], start=1.0, end=None)
        write_peer_file(registry_root, root, "sess-a", claims=[old_claim], windows=[stale_window], pid=DEAD_PID)

        coord = make_coordinator(tmp_path, session_id="sess-a")
        m = make_mutation(
            root / "new.py",
            provenance=MutationProvenance.PROVEN,
            source=MutationSource.WRITE_FILE,
            after_hash="new",
        )
        coord.publish_claims([m])

        data = read_own_file(registry_root, root, "sess-a")
        assert data["pid"] == os.getpid()
        # Prior claims preserved (peers' late rollbacks still need them).
        assert [c["after_hash"] for c in data["claims"]] == ["old", "new"]
        # Stale open window closed so the re-stamped live pid can't make
        # it read as an in-flight command.
        assert data["windows"][0]["end"] is not None

    def test_same_repo_move_publishes_single_claim(self, tmp_path):
        """A MOVE names two paths, but a same-repo rename resolves both
        to one tree — the claim must land once, not consume MAX_CLAIMS
        twice with duplicates."""
        root = make_tree(tmp_path)
        coord = make_coordinator(tmp_path)
        m = make_mutation(
            root / "new.py",
            op=MutationOp.MOVE,
            old_path=root / "old.py",
            provenance=MutationProvenance.PROVEN,
            source=MutationSource.EDIT_FILE,
            t_start=10.0,
            t_end=20.0,
        )
        assert coord.publish_claims([m]) == 1
        data = read_own_file(tmp_path / ATTRIBUTION_DIR_NAME, root, "sess-a")
        (claim,) = data["claims"]
        assert claim["key"] == canonical_key(str(root / "new.py"))
        assert claim["old_key"] == canonical_key(str(root / "old.py"))


# ---------------------------------------------------------------------------
# Registry file tolerance + GC
# ---------------------------------------------------------------------------


class TestRegistryFiles:
    def test_parse_rejects_garbage_and_future_schema(self):
        assert parse_registry_payload("not json{") is None
        assert parse_registry_payload(json.dumps({"schema": REGISTRY_SCHEMA + 1, "session_id": "x"})) is None
        assert parse_registry_payload(json.dumps({"session_id": "x"})) is None

    def test_reclassify_skips_malformed_peer_files(self, tmp_path):
        root = make_tree(tmp_path)
        registry_root = tmp_path / ATTRIBUTION_DIR_NAME
        tree_dir = registry_root / repo_key_for_root(str(root))
        tree_dir.mkdir(parents=True)
        (tree_dir / "corrupt.json").write_text("{{{{")
        coord = make_coordinator(tmp_path)
        tracker = make_tracker(tmp_path, [make_mutation(root / "f.py", t_start=1.0, t_end=2.0)])
        assert coord.reclassify(tracker) is False
        assert sole_mutation(tracker).provenance is MutationProvenance.ASSUMED

    def test_gc_removes_aged_peer_files_only(self, tmp_path):
        root = make_tree(tmp_path)
        registry_root = tmp_path / ATTRIBUTION_DIR_NAME
        aged = write_peer_file(registry_root, root, "sess-old", closed_at=1.0)
        fresh = write_peer_file(registry_root, root, "sess-new", closed_at=None)
        old_mtime = time.time() - 8 * 24 * 3600
        os.utime(aged, (old_mtime, old_mtime))

        coord = make_coordinator(tmp_path)
        coord.open_window([str(root)])  # any write triggers GC

        assert not aged.exists()
        assert fresh.exists()
        assert (registry_root / repo_key_for_root(str(root)) / "sess-a.json").exists()


# ---------------------------------------------------------------------------
# Reclassification: conflict-rule matrix
# ---------------------------------------------------------------------------


class TestConflictRules:
    def _run(self, tmp_path, mutation, claim):
        root = resolve_tree_root(mutation.path)
        write_peer_file(tmp_path / ATTRIBUTION_DIR_NAME, root, "sess-peer", claims=[claim])
        coord = make_coordinator(tmp_path)
        tracker = make_tracker(tmp_path, [mutation])
        changed = coord.reclassify(tracker)
        return changed, sole_mutation(tracker)

    def test_assumed_with_identity_demotes_to_foreign(self, tmp_path):
        root = make_tree(tmp_path)
        m = make_mutation(root / "f.py", after_hash="xyz", t_start=10.0, t_end=20.0)
        changed, row = self._run(tmp_path, m, peer_claim(root / "f.py", t0=12.0, t1=13.0, after_hash="xyz"))
        assert changed is True
        assert row.provenance is MutationProvenance.FOREIGN

    def test_assumed_overlap_without_identity_marks_contested(self, tmp_path):
        root = make_tree(tmp_path)
        m = make_mutation(root / "f.py", after_hash="ours", t_start=10.0, t_end=20.0)
        changed, row = self._run(tmp_path, m, peer_claim(root / "f.py", t0=12.0, t1=13.0, after_hash="theirs"))
        assert changed is True
        assert row.provenance is MutationProvenance.ASSUMED
        assert row.contested is True

    def test_proven_overlap_marks_contested_never_demotes(self, tmp_path):
        root = make_tree(tmp_path)
        m = make_mutation(
            root / "f.py",
            provenance=MutationProvenance.PROVEN,
            source=MutationSource.WRITE_FILE,
            after_hash="xyz",
            t_start=10.0,
            t_end=20.0,
        )
        changed, row = self._run(tmp_path, m, peer_claim(root / "f.py", t0=12.0, t1=13.0, after_hash="xyz"))
        assert changed is True
        assert row.provenance is MutationProvenance.PROVEN
        assert row.contested is True

    def test_disjoint_intervals_do_not_match(self, tmp_path):
        root = make_tree(tmp_path)
        m = make_mutation(root / "f.py", after_hash="xyz", t_start=10.0, t_end=20.0)
        changed, row = self._run(tmp_path, m, peer_claim(root / "f.py", t0=20.0, t1=25.0, after_hash="xyz"))
        assert changed is False
        assert row.provenance is MutationProvenance.ASSUMED
        assert row.contested is False

    def test_legacy_row_without_interval_never_matches(self, tmp_path):
        root = make_tree(tmp_path)
        m = make_mutation(root / "f.py", after_hash="xyz", t_start=None, t_end=None)
        changed, row = self._run(tmp_path, m, peer_claim(root / "f.py", t0=0.0, t1=1e12, after_hash="xyz"))
        assert changed is False
        assert row.provenance is MutationProvenance.ASSUMED

    def test_move_with_identity_never_demotes_falls_to_contested(self, tmp_path):
        root = make_tree(tmp_path)
        m = make_mutation(
            root / "new.py",
            op=MutationOp.MOVE,
            old_path=root / "old.py",
            after_hash="xyz",
            t_start=10.0,
            t_end=20.0,
        )
        changed, row = self._run(tmp_path, m, peer_claim(root / "new.py", t0=12.0, t1=13.0, after_hash="xyz"))
        assert changed is True
        assert row.provenance is MutationProvenance.ASSUMED
        assert row.contested is True

    def test_move_matches_on_old_path_too(self, tmp_path):
        root = make_tree(tmp_path)
        m = make_mutation(
            root / "new.py",
            op=MutationOp.MOVE,
            old_path=root / "old.py",
            after_hash="ours",
            t_start=10.0,
            t_end=20.0,
        )
        changed, row = self._run(tmp_path, m, peer_claim(root / "old.py", t0=12.0, t1=13.0, after_hash="theirs"))
        assert changed is True
        assert row.contested is True

    def test_both_gone_identity_demotes(self, tmp_path):
        root = make_tree(tmp_path)
        m = make_mutation(root / "f.py", op=MutationOp.DELETE, after_hash=None, t_start=10.0, t_end=20.0)
        claim = peer_claim(root / "f.py", t0=12.0, t1=13.0, after_hash=None, after_exists=False)
        changed, row = self._run(tmp_path, m, claim)
        assert changed is True
        assert row.provenance is MutationProvenance.FOREIGN

    def test_skip_marked_side_never_demotes(self, tmp_path):
        # Local after content withheld (skip) — no bytes to compare, so
        # "gone vs gone" identity must NOT trigger; falls to contested.
        root = make_tree(tmp_path)
        m = make_mutation(
            root / "f.bin",
            after_hash=None,
            after_skip=SnapshotSkipReason.BINARY,
            t_start=10.0,
            t_end=20.0,
        )
        claim = peer_claim(root / "f.bin", t0=12.0, t1=13.0, after_hash=None, after_exists=False)
        changed, row = self._run(tmp_path, m, claim)
        assert changed is True
        assert row.provenance is MutationProvenance.ASSUMED
        assert row.contested is True

    def test_foreign_rows_left_alone(self, tmp_path):
        root = make_tree(tmp_path)
        m = make_mutation(
            root / "f.py", provenance=MutationProvenance.FOREIGN, after_hash="xyz", t_start=10.0, t_end=20.0
        )
        changed, row = self._run(tmp_path, m, peer_claim(root / "f.py", t0=12.0, t1=13.0, after_hash="other"))
        assert changed is False
        assert row.provenance is MutationProvenance.FOREIGN
        assert row.contested is False

    def test_reclassify_is_idempotent(self, tmp_path):
        root = make_tree(tmp_path)
        m = make_mutation(root / "f.py", after_hash="ours", t_start=10.0, t_end=20.0)
        write_peer_file(
            tmp_path / ATTRIBUTION_DIR_NAME,
            root,
            "sess-peer",
            claims=[peer_claim(root / "f.py", t0=12.0, t1=13.0, after_hash="theirs")],
        )
        coord = make_coordinator(tmp_path)
        tracker = make_tracker(tmp_path, [m])
        assert coord.reclassify(tracker) is True
        assert coord.reclassify(tracker) is False
        assert coord.reclassify(tracker, force=True) is False  # no further changes
        assert sole_mutation(tracker).contested is True

    # ---------------------------------------------------------------------------
    # Reclassify short-circuit triggers
    # ---------------------------------------------------------------------------

    def test_peer_move_claim_with_identity_never_demotes(self, tmp_path):
        """Mirror of the local-MOVE rule: MOVEs never demote on EITHER side.

        A peer MOVE claim is indexed under both destination and source
        keys, so a local simple row on the move destination can carry
        identical bytes — but a rename is not proof the peer authored
        our row's content.  Contested, never FOREIGN.
        """
        root = make_tree(tmp_path)
        m = make_mutation(root / "new.py", after_hash="xyz", t_start=10.0, t_end=20.0)
        changed, row = self._run(
            tmp_path,
            m,
            peer_claim(root / "new.py", t0=12.0, t1=13.0, after_hash="xyz", old_path=root / "old.py"),
        )
        assert changed is True
        assert row.provenance is MutationProvenance.ASSUMED
        assert row.contested is True


class TestShortCircuit:
    def test_new_peer_file_reruns_matching(self, tmp_path):
        root = make_tree(tmp_path)
        m = make_mutation(root / "f.py", after_hash="xyz", t_start=10.0, t_end=20.0)
        coord = make_coordinator(tmp_path)
        tracker = make_tracker(tmp_path, [m])
        assert coord.reclassify(tracker) is False  # no peers yet

        # Peer claim published AFTER our finalize (the finalize-vs-publish
        # race): the next reclassify — e.g. rollback's — must catch it.
        write_peer_file(
            tmp_path / ATTRIBUTION_DIR_NAME,
            root,
            "sess-peer",
            claims=[peer_claim(root / "f.py", t0=12.0, t1=13.0, after_hash="xyz")],
        )
        assert coord.reclassify(tracker) is True
        assert sole_mutation(tracker).provenance is MutationProvenance.FOREIGN

    def test_new_local_rows_rerun_matching_against_old_peers(self, tmp_path):
        # Peer files unchanged, but a NEW local row (long-running shell
        # window) can overlap an OLD claim — the local-log signature must
        # bust the short-circuit on its own.
        root = make_tree(tmp_path)
        write_peer_file(
            tmp_path / ATTRIBUTION_DIR_NAME,
            root,
            "sess-peer",
            claims=[peer_claim(root / "f.py", t0=12.0, t1=13.0, after_hash="xyz")],
        )
        coord = make_coordinator(tmp_path)
        tracker = make_tracker(tmp_path, [make_mutation(root / "other.py", t_start=1.0, t_end=2.0)])
        assert coord.reclassify(tracker) is False

        tracker.with_locked_log(
            lambda log: log.turns[0].mutations.append(
                make_mutation(root / "f.py", after_hash="xyz", t_start=10.0, t_end=20.0)
            )
        )
        assert coord.reclassify(tracker) is True

    def test_t_end_stamp_busts_short_circuit(self, tmp_path):
        """A mid-call read refresh must not cache away the finalize match.

        File-tool rows exist from record() time with an OPEN interval;
        row B is in-flight while a peer claim overlaps it.  A non-forced
        reclassify (row A keeps the candidate list non-empty) caches the
        signature; stamping B's t_end changes neither turn nor row count
        — only the completed-interval count can bust the cache, and the
        finalize-time reclassify must re-run and demote B.
        """
        root = make_tree(tmp_path)
        row_a = make_mutation(root / "a.py", after_hash="other", t_start=1.0, t_end=2.0)
        row_b = make_mutation(root / "b.py", after_hash="xyz", t_start=10.0, t_end=None)
        write_peer_file(
            tmp_path / ATTRIBUTION_DIR_NAME,
            root,
            "sess-peer",
            claims=[peer_claim(root / "b.py", t0=12.0, t1=13.0, after_hash="xyz")],
        )
        coord = make_coordinator(tmp_path)
        tracker = make_tracker(tmp_path, [row_a, row_b])

        # Mid-flight read refresh: B is skipped (open interval), no row
        # changes, signature cached.
        assert coord.reclassify(tracker) is False

        def stamp(log):
            log.turns[0].mutations[1].t_end = 20.0

        tracker.with_locked_log(stamp)

        # Finalize-time non-forced reclassify: must NOT short-circuit.
        assert coord.reclassify(tracker) is True
        row = tracker.with_locked_log(lambda log: log.turns[0].mutations[1])
        assert row.provenance is MutationProvenance.FOREIGN


# ---------------------------------------------------------------------------
# Rollback augmentation: PEER_MODIFIED_SINCE + in-flight warnings
# ---------------------------------------------------------------------------


class TestRollbackAugmentation:
    def _plan_for(self, path):
        snapshot = FileSnapshot(path=str(path), period_index=1, existed=True, content_hash="aaa", size=3)
        return RollbackPlan(entries=[(str(path), snapshot)])

    def test_peer_claim_newer_than_our_last_write_excludes(self, tmp_path):
        root = make_tree(tmp_path)
        target = root / "f.py"
        write_peer_file(
            tmp_path / ATTRIBUTION_DIR_NAME,
            root,
            "sess-peer",
            claims=[peer_claim(target, t0=100.0, t1=101.0, after_hash="peer")],
        )
        coord = make_coordinator(tmp_path)
        tracker = make_tracker(tmp_path, [make_mutation(target, t_start=10.0, t_end=20.0)])
        plan = coord.augment_rollback_plan(tracker, self._plan_for(target))
        assert plan.entries == []
        assert plan.exclusions == [(str(target), RollbackExclusionReason.PEER_MODIFIED_SINCE)]

    def test_peer_claim_older_than_our_last_write_keeps_entry(self, tmp_path):
        root = make_tree(tmp_path)
        target = root / "f.py"
        write_peer_file(
            tmp_path / ATTRIBUTION_DIR_NAME,
            root,
            "sess-peer",
            claims=[peer_claim(target, t0=5.0, t1=6.0, after_hash="peer")],
        )
        coord = make_coordinator(tmp_path)
        tracker = make_tracker(tmp_path, [make_mutation(target, t_start=10.0, t_end=20.0)])
        plan = coord.augment_rollback_plan(tracker, self._plan_for(target))
        assert len(plan.entries) == 1
        assert plan.exclusions == []

    def test_active_peer_open_window_warns(self, tmp_path):
        root = make_tree(tmp_path)
        target = root / "f.py"
        window = ObservationWindow(id="w1", scope=[str(root)], start=1.0, end=None)
        write_peer_file(
            tmp_path / ATTRIBUTION_DIR_NAME,
            root,
            "sess-peer",
            windows=[window],
            pid=os.getpid(),  # provably alive
            closed_at=None,
        )
        coord = make_coordinator(tmp_path)
        tracker = make_tracker(tmp_path, [make_mutation(target, t_start=10.0, t_end=20.0)])
        plan = coord.augment_rollback_plan(tracker, self._plan_for(target))
        assert [w.code for w in plan.warnings] == [PEER_ACTIVE_WINDOW_WARNING]

    @pytest.mark.parametrize(
        ("pid", "closed_at", "window_end"),
        [
            (DEAD_PID, None, None),  # dead pid counts as closed
            ("self", 50.0, None),  # cleanly closed session
            ("self", None, 2.0),  # window already closed
        ],
    )
    def test_no_warning_when_peer_not_actually_in_flight(self, tmp_path, pid, closed_at, window_end):
        # "self" resolves to our own live pid at runtime — os.getpid() in
        # the parametrize list would desync xdist worker collection.
        pid = os.getpid() if pid == "self" else pid
        root = make_tree(tmp_path)
        target = root / "f.py"
        window = ObservationWindow(id="w1", scope=[str(root)], start=1.0, end=window_end)
        write_peer_file(
            tmp_path / ATTRIBUTION_DIR_NAME,
            root,
            "sess-peer",
            windows=[window],
            pid=pid,
            closed_at=closed_at,
        )
        coord = make_coordinator(tmp_path)
        tracker = make_tracker(tmp_path, [make_mutation(target, t_start=10.0, t_end=20.0)])
        plan = coord.augment_rollback_plan(tracker, self._plan_for(target))
        assert plan.warnings == []

    def test_untracked_augmentation_returns_plan_unchanged(self, tmp_path):
        root = make_tree(tmp_path)
        target = root / "f.py"
        coord = make_coordinator(tmp_path)
        tracker = make_tracker(tmp_path, [make_mutation(target, t_start=10.0, t_end=20.0)])
        plan = self._plan_for(target)
        assert coord.augment_rollback_plan(tracker, plan) is plan


# ---------------------------------------------------------------------------
# Pipeline integration: interval stamping + registry protocol threading
# ---------------------------------------------------------------------------


class TestPipelineIntegration:
    async def test_write_tool_stamps_tight_interval_and_publishes_claim(self, tmp_path):
        import chrys.service.agent_middleware  # noqa: F401  — break the pipeline<->middleware import cycle
        from chrys.service.mutations.pipeline import finalize_mutation_tracking, prepare_mutation_tracking

        root = make_tree(tmp_path)
        f = root / "f.txt"
        f.write_text("before")
        tracker = MutationTracker(SnapshotStore(tmp_path / "session"))
        tracker.start_turn(1)
        coord = make_coordinator(tmp_path)

        ctx = await prepare_mutation_tracking(
            tracker,
            "write_file",
            {"path": str(f)},
            "call-1",
            is_shell=False,
            workspace_cwd=str(root),
            coordinator=coord,
        )
        f.write_text("after")
        await finalize_mutation_tracking(tracker, ctx, "call-1", coordinator=coord)

        m = ctx.file_mutation
        assert m is not None
        assert m.t_start is not None and m.t_end is not None
        assert m.t_start == m.timestamp  # tight bound: record() runs pre-write
        assert m.t_start <= m.t_end
        data = read_own_file(tmp_path / ATTRIBUTION_DIR_NAME, root, "sess-a")
        (claim,) = data["claims"]
        assert claim["key"] == canonical_key(str(f))
        assert (claim["t0"], claim["t1"]) == (m.t_start, m.t_end)
        # File tools advertise no observation window.
        assert data["windows"] == []

    async def test_errored_write_tool_publishes_no_claim(self, tmp_path):
        import chrys.service.agent_middleware  # noqa: F401  — break the pipeline<->middleware import cycle
        from chrys.service.mutations.pipeline import finalize_mutation_tracking, prepare_mutation_tracking

        root = make_tree(tmp_path)
        f = root / "f.txt"
        f.write_text("before")
        tracker = MutationTracker(SnapshotStore(tmp_path / "session"))
        tracker.start_turn(1)
        coord = make_coordinator(tmp_path)

        ctx = await prepare_mutation_tracking(
            tracker,
            "write_file",
            {"path": str(f)},
            "call-1",
            is_shell=False,
            workspace_cwd=str(root),
            coordinator=coord,
        )
        f.write_text("after")  # the delta exists but chrys may not have authored it
        await finalize_mutation_tracking(tracker, ctx, "call-1", coordinator=coord, tool_errored=True)

        assert ctx.file_mutation is not None
        assert ctx.file_mutation.t_end is not None  # intervals still stamped
        own = tmp_path / ATTRIBUTION_DIR_NAME / repo_key_for_root(str(root)) / "sess-a.json"
        assert not own.exists()  # nothing published at all

    async def test_shell_window_lifecycle_and_full_interval(self, tmp_path):
        import chrys.service.agent_middleware  # noqa: F401  — break the pipeline<->middleware import cycle
        from chrys.service.mutations.pipeline import finalize_mutation_tracking, prepare_mutation_tracking

        root = make_tree(tmp_path)
        f = root / "out.txt"
        f.write_text("before")
        tracker = MutationTracker(SnapshotStore(tmp_path / "session"))
        tracker.start_turn(1)
        coord = make_coordinator(tmp_path)

        ctx = await prepare_mutation_tracking(
            tracker,
            "shell",
            {"command": f"echo x > {f}", "workdir": str(root)},
            "call-2",
            is_shell=True,
            workspace_cwd=str(root),
            coordinator=coord,
        )
        assert ctx.window_id is not None
        assert ctx.window_opened_at is not None
        data = read_own_file(tmp_path / ATTRIBUTION_DIR_NAME, root, "sess-a")
        (window,) = data["windows"]
        assert window["end"] is None  # advertised open before any observation

        f.write_text("changed by shell")
        result = await finalize_mutation_tracking(tracker, ctx, "call-2", coordinator=coord)

        assert result.shell_snapshots  # the change was detected
        data = read_own_file(tmp_path / ATTRIBUTION_DIR_NAME, root, "sess-a")
        (window,) = data["windows"]
        assert window["end"] is not None
        # ASSUMED inferences are never published as claims.
        assert data["claims"] == []

        def _shell_row(log):
            return next(m for t in log.turns for m in t.mutations if m.path == str(f))

        row = tracker.with_locked_log(_shell_row)
        assert row.provenance is MutationProvenance.ASSUMED
        # Full observation window as the write interval — the point
        # timestamp sits post-window and must not drive matching.
        assert row.t_start == ctx.window_opened_at
        assert row.t_end is not None and row.t_end >= row.t_start


# ---------------------------------------------------------------------------
# Middleware claim gating: structured (no-raise) tool failures
# ---------------------------------------------------------------------------


class TestMiddlewareClaimGating:
    """Filesystem tools report failures as ``"Error: ..."`` RESULTS plus
    structured metadata (``tool_error``/``record_tool_failure``) — they
    don't raise.  The middleware must treat that state as non-claimable:
    the on-disk delta may be a peer's write racing our window.
    """

    def _middleware(self, tracker, coord, root):
        from chrys.foundation.events.bus import EventBus
        from chrys.service.agent_middleware.events.tool_events import ToolEventMiddleware

        return ToolEventMiddleware(
            EventBus(),
            session_id="s1",
            mutation_tracker=tracker,
            workspace_cwd=str(root),
            mutation_coordinator=coord,
            origin=InvocationOrigin("turn", "s1", "turn-test", None),
        )

    def _write_ctx(self, f):
        from types import SimpleNamespace

        from chrys.foundation.tool_kinds import KIND_FILESYSTEM_WRITE

        return SimpleNamespace(
            function=SimpleNamespace(name="write_file", chrys_kind=KIND_FILESYSTEM_WRITE),
            arguments={"path": str(f), "content": "new"},
            result=None,
            metadata={},
        )

    async def test_structured_failure_blocks_claim(self, tmp_path):
        from chrys.service.tools.result_metadata import record_tool_failure

        root = make_tree(tmp_path)
        f = root / "f.txt"
        f.write_text("before")
        tracker = MutationTracker(SnapshotStore(tmp_path / "session"))
        tracker.start_turn(1)
        coord = make_coordinator(tmp_path, session_id="s1")

        async def failing_next() -> None:
            record_tool_failure("write_failed", "refused")
            f.write_text("peer bytes")  # the delta exists; our authorship doesn't

        await self._middleware(tracker, coord, root).process(self._write_ctx(f), failing_next)

        own = tmp_path / ATTRIBUTION_DIR_NAME / repo_key_for_root(str(root)) / "s1.json"
        assert not own.exists()

    async def test_successful_write_publishes_claim(self, tmp_path):
        root = make_tree(tmp_path)
        f = root / "f.txt"
        f.write_text("before")
        tracker = MutationTracker(SnapshotStore(tmp_path / "session"))
        tracker.start_turn(1)
        coord = make_coordinator(tmp_path, session_id="s1")

        async def ok_next() -> None:
            f.write_text("new")

        await self._middleware(tracker, coord, root).process(self._write_ctx(f), ok_next)

        data = read_own_file(tmp_path / ATTRIBUTION_DIR_NAME, root, "s1")
        assert len(data["claims"]) == 1
        assert data["claims"][0]["key"] == canonical_key(str(f))


# ---------------------------------------------------------------------------
# Cancellation: windows must not outlive the interrupted tool call
# ---------------------------------------------------------------------------


class TestCancellationWindowAbort:
    """Interrupt (CancelledError) skips the async finalize entirely — a
    cancelled task raises at its next ``await``.  The middleware's sync
    cancel path must still close the observation window, or this session
    keeps advertising a live command until shutdown (false peer-active
    warnings on every peer's rollback).
    """

    async def test_cancelled_shell_closes_window(self, tmp_path):
        import asyncio
        from types import SimpleNamespace

        from chrys.foundation.events.bus import EventBus
        from chrys.foundation.tool_kinds import KIND_SHELL
        from chrys.service.agent_middleware.events.tool_events import ToolEventMiddleware

        root = make_tree(tmp_path)
        tracker = MutationTracker(SnapshotStore(tmp_path / "session"))
        tracker.start_turn(1)
        coord = make_coordinator(tmp_path, session_id="s1")
        mw = ToolEventMiddleware(
            EventBus(),
            session_id="s1",
            mutation_tracker=tracker,
            workspace_cwd=str(root),
            mutation_coordinator=coord,
            origin=InvocationOrigin("turn", "s1", "turn-test", None),
        )
        ctx = SimpleNamespace(
            function=SimpleNamespace(name="shell", chrys_kind=KIND_SHELL),
            arguments={"command": "true"},
            result=None,
            metadata={},
        )

        async def cancelled_next() -> None:
            raise asyncio.CancelledError

        with pytest.raises(asyncio.CancelledError):
            await mw.process(ctx, cancelled_next)

        data = read_own_file(tmp_path / ATTRIBUTION_DIR_NAME, root, "s1")
        (window,) = data["windows"]
        assert window["end"] is not None
        assert window["end"] >= window["start"]

    async def test_abort_helper_tolerates_missing_window_and_coordinator(self, tmp_path):
        import chrys.service.agent_middleware  # noqa: F401  — break the pipeline<->middleware import cycle
        from chrys.service.mutations.pipeline import MutationContext, abort_mutation_tracking

        # No coordinator / no window: both must be silent no-ops.
        abort_mutation_tracking(MutationContext(), None)
        abort_mutation_tracking(MutationContext(), make_coordinator(tmp_path))


# ---------------------------------------------------------------------------
# Pid liveness
# ---------------------------------------------------------------------------


class TestPidLiveness:
    """A crashed peer must read dead, or its leftovers warn until the 7-day GC."""

    def test_live_and_dead_pids(self):
        from chrys.service.mutations.coordination import _is_pid_alive

        assert _is_pid_alive(os.getpid()) is True
        # DEAD_PID is odd, and real Windows pids are multiples of 4, so it
        # can never exist there.
        assert _is_pid_alive(DEAD_PID) is False
        assert _is_pid_alive(0) is False


class TestCanonicalKeyCaseFolding:
    def test_case_variant_spellings_share_a_key(self, tmp_path):
        """Windows folds via ``normcase``; darwin needs the explicit
        fold (default APFS/HFS+ volumes are case-insensitive but
        ``posixpath.normcase`` is identity).  Case-variant launches of
        the same tree must publish/read the same registry dir.
        """
        import sys

        if sys.platform != "darwin" and os.name != "nt":
            pytest.skip("case folding applies to Windows and macOS only")
        root = tmp_path / "CaseVariant"
        root.mkdir()
        variant = tmp_path / "cASEvARIANT"
        assert canonical_key(str(root)) == canonical_key(str(variant))
        assert repo_key_for_root(str(root)) == repo_key_for_root(str(variant))


class TestUnsavedReclassification:
    def test_flag_set_on_change_and_consumed_once(self, tmp_path):
        """reclassify's signature update hides its own changes from the
        next call — the flag is how the persister learns a change from a
        saver-less context (tool finalize) still needs a save.
        """
        root = make_tree(tmp_path)
        m = make_mutation(root / "f.py", after_hash="xyz", t_start=10.0, t_end=20.0)
        write_peer_file(
            tmp_path / ATTRIBUTION_DIR_NAME,
            root,
            "sess-peer",
            claims=[peer_claim(root / "f.py", t0=12.0, t1=13.0, after_hash="xyz")],
        )
        coord = make_coordinator(tmp_path)
        tracker = make_tracker(tmp_path, [m])

        assert coord.consume_unsaved_reclassification() is False
        assert coord.reclassify(tracker) is True
        assert coord.consume_unsaved_reclassification() is True
        assert coord.consume_unsaved_reclassification() is False
        # A no-change run must not re-arm the flag.
        assert coord.reclassify(tracker) is False
        assert coord.consume_unsaved_reclassification() is False


class TestNonGitKeying:
    def test_untracked_dir_key_is_session_independent(self, tmp_path):
        """Outside any repo the key is a pure function of the path.

        Two sessions with DIFFERENT primary workspaces touching the same
        untracked directory must publish/read the same registry tree —
        keying by the caller's fallback root would make peer claims
        mutually invisible and let rollback clobber the peer's work.
        """
        shared = tmp_path / "shared"
        shared.mkdir()
        ws_a = tmp_path / "wsA"
        ws_a.mkdir()
        ws_b = tmp_path / "wsB"
        ws_b.mkdir()
        f = shared / "notes.txt"

        coord_a = make_coordinator(tmp_path, session_id="sess-a")
        proven = make_mutation(
            f,
            provenance=MutationProvenance.PROVEN,
            source=MutationSource.WRITE_FILE,
            t_start=10.0,
            t_end=20.0,
        )
        assert coord_a.publish_claims([proven], fallback_root=str(ws_a)) == 1
        # The claim keys under the file's own directory, not ws_a.
        data = read_own_file(tmp_path / ATTRIBUTION_DIR_NAME, shared, "sess-a")
        assert len(data["claims"]) == 1

        coord_b = make_coordinator(tmp_path, session_id="sess-b")
        local = make_mutation(f, t_start=12.0, t_end=13.0)  # ASSUMED, identical after bytes
        tracker = make_tracker(tmp_path, [local])
        assert coord_b.reclassify(tracker, fallback_root=str(ws_b)) is True
        assert sole_mutation(tracker).provenance is MutationProvenance.FOREIGN

    def test_untracked_paths_without_fallback_do_not_participate(self, tmp_path):
        """fallback_root still gates participation: no workspace context
        → untracked paths publish nothing."""
        shared = tmp_path / "shared"
        shared.mkdir()
        proven = make_mutation(
            shared / "notes.txt",
            provenance=MutationProvenance.PROVEN,
            source=MutationSource.WRITE_FILE,
            t_start=10.0,
            t_end=20.0,
        )
        coord = make_coordinator(tmp_path)
        assert coord.publish_claims([proven], fallback_root=None) == 0
