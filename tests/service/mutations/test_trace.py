# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""fsatrace write-trace tests.

The parser is validated against the upstream emit format (``emitOp`` in
fsatrace's ``src/emit.c``): lines are ``op|path``, moves are
``m|destination|source``, ops go UPPERCASE when a side could not be
realpath'd, and ``<unknown>`` stands in for unresolvable paths.
"""

from __future__ import annotations

import stat
import subprocess
import sys
import time

import pytest

import chrys.service.mutations.trace as trace_mod
from chrys.foundation.models.invocations import InvocationOrigin
from chrys.service.mutations.coordination import ATTRIBUTION_DIR_NAME, canonical_key, repo_key_for_root
from chrys.service.mutations.store import SnapshotStore
from chrys.service.mutations.trace import (
    FSATRACE_PATH_ENV,
    TRACE_MODE_ENV,
    allocate_trace_file,
    discard_trace_file,
    parse_trace_write_keys,
    resolve_fsatrace,
    shell_trace_argv,
)
from chrys.service.mutations.tracker import MutationTracker
from chrys.service.mutations.types import (
    FileMutation,
    MutationOp,
    MutationProvenance,
    MutationSource,
)
from tests.support.waiting import wait_for

IS_WINDOWS = sys.platform == "win32"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def write_trace(path, lines) -> str:
    path.write_text("".join(f"{line}\n" for line in lines))
    return str(path)


def keys_for(*paths) -> set[str]:
    return {canonical_key(str(p)) for p in paths}


@pytest.fixture
def probe_cache(monkeypatch):
    """Isolate the per-process probe cache; monkeypatch restores after."""
    monkeypatch.setattr(trace_mod, "_probe_done", False)
    monkeypatch.setattr(trace_mod, "_probe_result", None)


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


class TestTraceParser:
    def test_write_move_delete_touch_all_match(self, tmp_path):
        a, b, c, d = (tmp_path / n for n in ("a.txt", "b.txt", "c.txt", "d.txt"))
        # Move order is m|destination|source per upstream emit.c; matching
        # is deliberately side-symmetric (both sides are write evidence),
        # so a swapped order could never flip a result — this pins the
        # documented order in a fixture regardless.
        trace = write_trace(tmp_path / "t.log", [f"w|{a}", f"m|{b}|{c}", f"t|{d}"])
        assert parse_trace_write_keys(trace, keys_for(a, b, c, d)) == keys_for(a, b, c, d)

    def test_delete_lines_match(self, tmp_path):
        gone = tmp_path / "gone.txt"
        trace = write_trace(tmp_path / "t.log", [f"d|{gone}"])
        assert parse_trace_write_keys(trace, keys_for(gone)) == keys_for(gone)

    def test_move_matches_on_either_side_alone(self, tmp_path):
        src, dst = tmp_path / "src.txt", tmp_path / "dst.txt"
        trace = write_trace(tmp_path / "t.log", [f"m|{dst}|{src}"])
        assert parse_trace_write_keys(trace, keys_for(src)) == keys_for(src)
        assert parse_trace_write_keys(trace, keys_for(dst)) == keys_for(dst)

    def test_read_lines_ignored(self, tmp_path):
        a = tmp_path / "a.txt"
        trace = write_trace(tmp_path / "t.log", [f"r|{a}", f"R|{a}", f"q|{a}"])
        assert parse_trace_write_keys(trace, keys_for(a)) == set()

    def test_uppercase_ops_still_count(self, tmp_path):
        # Upstream uppercases the op when a side could not be realpath'd;
        # M|dst (unknown source) is still positive write evidence for dst.
        a, b = tmp_path / "a.txt", tmp_path / "b.txt"
        trace = write_trace(tmp_path / "t.log", [f"M|{a}", f"D|{b}"])
        assert parse_trace_write_keys(trace, keys_for(a, b)) == keys_for(a, b)

    def test_unknown_placeholder_and_garbage_skipped(self, tmp_path):
        a = tmp_path / "a.txt"
        trace = write_trace(
            tmp_path / "t.log",
            ["w|<unknown>", "garbage without separator", "w", "", f"w|{a}"],
        )
        assert parse_trace_write_keys(trace, keys_for(a)) == keys_for(a)

    def test_non_candidates_never_leak_in(self, tmp_path):
        a, other = tmp_path / "a.txt", tmp_path / "other.txt"
        trace = write_trace(tmp_path / "t.log", [f"w|{other}"])
        assert parse_trace_write_keys(trace, keys_for(a)) == set()

    def test_missing_trace_file_is_no_evidence(self, tmp_path):
        assert parse_trace_write_keys(str(tmp_path / "nope.log"), keys_for(tmp_path / "a.txt")) == set()

    def test_empty_candidates_short_circuits(self, tmp_path):
        trace = write_trace(tmp_path / "t.log", ["w|/anything"])
        assert parse_trace_write_keys(trace, set()) == set()

    def test_oversized_trace_discarded_whole(self, tmp_path, monkeypatch):
        a = tmp_path / "a.txt"
        trace = write_trace(tmp_path / "t.log", [f"w|{a}"])
        monkeypatch.setattr(trace_mod, "TRACE_MAX_BYTES", 4)
        assert parse_trace_write_keys(trace, keys_for(a)) == set()

    def test_case_variant_path_matches_iff_platform_folds(self, tmp_path):
        target = tmp_path / "CamelCase.txt"
        variant = str(tmp_path / "camelcase.txt")
        trace = write_trace(tmp_path / "t.log", [f"w|{variant}"])
        expected = keys_for(target) if canonical_key(variant) == canonical_key(str(target)) else set()
        assert parse_trace_write_keys(trace, keys_for(target)) == expected

    @pytest.mark.skipif(IS_WINDOWS, reason="POSIX symlinks")
    def test_symlinked_directory_route_matches(self, tmp_path):
        real_dir = tmp_path / "real"
        real_dir.mkdir()
        target = real_dir / "f.txt"
        target.write_text("x")
        link_dir = tmp_path / "link"
        link_dir.symlink_to(real_dir)
        trace = write_trace(tmp_path / "t.log", [f"w|{link_dir / 'f.txt'}"])
        assert parse_trace_write_keys(trace, keys_for(target)) == keys_for(target)

    @pytest.mark.skipif(IS_WINDOWS, reason="POSIX symlinks")
    def test_symlinked_final_component_with_different_basename_is_missed(self, tmp_path):
        # Documented pre-filter limitation: a symlink whose NAME differs
        # from its target's basename never reaches canonicalization.
        # Evidence is one-directional, so the row just stays ASSUMED.
        target = tmp_path / "actual.txt"
        target.write_text("x")
        alias = tmp_path / "alias.txt"
        alias.symlink_to(target)
        trace = write_trace(tmp_path / "t.log", [f"w|{alias}"])
        assert parse_trace_write_keys(trace, keys_for(target)) == set()


# ---------------------------------------------------------------------------
# Probe
# ---------------------------------------------------------------------------


class TestProbe:
    def test_mode_off_disables(self, monkeypatch, probe_cache):
        monkeypatch.setenv(TRACE_MODE_ENV, "off")
        monkeypatch.setenv(FSATRACE_PATH_ENV, sys.executable)  # would otherwise be probed
        assert resolve_fsatrace(force_probe=True) is None

    def test_override_pointing_nowhere_fails_probe(self, monkeypatch, probe_cache):
        monkeypatch.setenv(FSATRACE_PATH_ENV, "/nonexistent/fsatrace-xyz")
        assert resolve_fsatrace(force_probe=True) is None

    @pytest.mark.skipif(IS_WINDOWS, reason="shebang script")
    def test_override_broken_binary_fails_self_test(self, monkeypatch, tmp_path, probe_cache):
        broken = tmp_path / "fsatrace"
        broken.write_text("#!/bin/sh\nexit 1\n")
        broken.chmod(broken.stat().st_mode | stat.S_IEXEC)
        monkeypatch.setenv(FSATRACE_PATH_ENV, str(broken))
        assert resolve_fsatrace(force_probe=True) is None

    @pytest.mark.skipif(IS_WINDOWS, reason="shebang script")
    def test_override_silent_binary_fails_self_test(self, monkeypatch, tmp_path, probe_cache):
        # Exit 0 but empty trace — the hardened-macOS signature.  The
        # probe demands a parse hit, not just a clean exit.
        silent = tmp_path / "fsatrace"
        silent.write_text('#!/bin/sh\n: > "$2"\nexit 0\n')
        silent.chmod(silent.stat().st_mode | stat.S_IEXEC)
        monkeypatch.setenv(FSATRACE_PATH_ENV, str(silent))
        assert resolve_fsatrace(force_probe=True) is None

    @pytest.mark.skipif(IS_WINDOWS, reason="shebang script")
    def test_fake_tracer_passes_probe_and_result_is_cached(self, monkeypatch, tmp_path, probe_cache):
        fake = _write_fake_tracer(tmp_path)
        monkeypatch.setenv(FSATRACE_PATH_ENV, str(fake))
        assert resolve_fsatrace(force_probe=True) == str(fake)
        # Cached: env changes no longer matter without force_probe.
        monkeypatch.setenv(TRACE_MODE_ENV, "off")
        assert resolve_fsatrace() == str(fake)

    def test_allocate_returns_none_when_unavailable(self, monkeypatch, tmp_path, probe_cache):
        monkeypatch.setenv(TRACE_MODE_ENV, "off")
        resolve_fsatrace(force_probe=True)
        assert allocate_trace_file(tmp_path / "mutations") is None

    @pytest.mark.skipif(IS_WINDOWS, reason="shebang script")
    def test_allocate_builds_wrapper_prefix(self, monkeypatch, tmp_path, probe_cache):
        fake = _write_fake_tracer(tmp_path)
        monkeypatch.setenv(FSATRACE_PATH_ENV, str(fake))
        resolve_fsatrace(force_probe=True)
        allocation = allocate_trace_file(tmp_path / "mutations")
        assert allocation is not None
        prefix, trace_file = allocation
        assert prefix == [str(fake), trace_mod.TRACE_OPS, trace_file, "--"]
        assert trace_file.startswith(str(tmp_path / "mutations"))
        # Nonce-named: two allocations never collide.
        assert allocate_trace_file(tmp_path / "mutations")[1] != trace_file

    def test_discard_tolerates_missing_and_none(self, tmp_path):
        discard_trace_file(None)
        discard_trace_file(str(tmp_path / "never-existed.log"))
        f = tmp_path / "t.log"
        f.write_text("w|/x")
        discard_trace_file(str(f))
        assert not f.exists()


def _write_fake_tracer(tmp_path):
    """A stand-in fsatrace: runs the command, then logs the probe target.

    Mirrors the real argv contract ``fsatrace <ops> <trace> -- cmd...``;
    it extracts the written path from the probe's ``python -c`` payload,
    which is enough to satisfy the self-test end to end.
    """
    fake = tmp_path / "fake-fsatrace.py"
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "import re, subprocess, sys\n"
        "trace, cmd = sys.argv[2], sys.argv[4:]\n"
        "rc = subprocess.run(cmd).returncode\n"
        "m = re.search(r\"open\\('([^']+)'\", cmd[-1] if cmd else '')\n"
        "with open(trace, 'w') as fh:\n"
        "    if m:\n"
        "        fh.write(f'w|{m.group(1)}\\n')\n"
        "sys.exit(rc)\n"
    )
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    return fake


# ---------------------------------------------------------------------------
# Real-binary integration (auto-skips wherever fsatrace can't operate)
# ---------------------------------------------------------------------------


class TestRealBinaryIntegration:
    def test_traced_python_write_appears_in_trace(self, tmp_path):
        binary = resolve_fsatrace()
        if binary is None:
            pytest.skip("fsatrace unavailable (not installed, or injection blocked on this OS)")
        allocation = allocate_trace_file(tmp_path / "mutations")
        assert allocation is not None
        prefix, trace_file = allocation
        target = tmp_path / "written-by-child.txt"
        code = f"open({str(target)!r}, 'w').write('x')"
        subprocess.run([*prefix, sys.executable, "-c", code], check=True, capture_output=True, timeout=60)
        assert parse_trace_write_keys(trace_file, keys_for(target)) == keys_for(target)
        discard_trace_file(trace_file)


# ---------------------------------------------------------------------------
# Pipeline integration: prepare arms, finalize upgrades + publishes + releases
# ---------------------------------------------------------------------------


def _arm_fake_allocation(monkeypatch, tmp_path):
    """Route prepare's allocation to a fixture path without a probe."""
    trace_file = tmp_path / "session" / "mutations" / "trace-fixture.log"

    def fake_allocate(mutations_dir):
        mutations_dir.mkdir(parents=True, exist_ok=True)
        return ["/fake/fsatrace", trace_mod.TRACE_OPS, str(trace_file), "--"], str(trace_file)

    monkeypatch.setattr("chrys.service.mutations.pipeline.allocate_trace_file", fake_allocate)
    return trace_file


def make_tree(tmp_path, name="repo"):
    root = tmp_path / name
    root.mkdir(parents=True, exist_ok=True)
    (root / ".git").mkdir(exist_ok=True)
    return root


class TestPipelineTraceLifecycle:
    async def test_shell_prepare_arms_wrapper_and_finalize_releases(self, tmp_path, monkeypatch):
        import chrys.service.agent_middleware  # noqa: F401  — break the pipeline<->middleware import cycle
        from chrys.service.mutations.pipeline import finalize_mutation_tracking, prepare_mutation_tracking

        trace_file = _arm_fake_allocation(monkeypatch, tmp_path)
        root = make_tree(tmp_path)
        tracker = MutationTracker(SnapshotStore(tmp_path / "session"))
        tracker.start_turn(1)

        ctx = await prepare_mutation_tracking(
            tracker, "shell", {"command": "true", "workdir": str(root)}, "c1", is_shell=True
        )
        assert ctx.trace_file == str(trace_file)
        prefix = shell_trace_argv.get()
        assert prefix is not None and prefix[0] == "/fake/fsatrace" and prefix[-1] == "--"

        trace_file.write_text("")  # as if fsatrace created it
        await finalize_mutation_tracking(tracker, ctx, "c1")
        assert shell_trace_argv.get() is None
        assert ctx.trace_token is None
        assert not trace_file.exists()

    async def test_non_shell_prepare_never_arms(self, tmp_path, monkeypatch):
        import chrys.service.agent_middleware  # noqa: F401  — break the pipeline<->middleware import cycle
        from chrys.service.mutations.pipeline import prepare_mutation_tracking

        _arm_fake_allocation(monkeypatch, tmp_path)
        root = make_tree(tmp_path)
        f = root / "f.txt"
        f.write_text("before")
        tracker = MutationTracker(SnapshotStore(tmp_path / "session"))
        tracker.start_turn(1)

        ctx = await prepare_mutation_tracking(tracker, "write_file", {"path": str(f)}, "c1", is_shell=False)
        assert ctx.trace_file is None
        assert shell_trace_argv.get() is None

    async def test_unavailable_tracer_leaves_shell_unwrapped(self, tmp_path, monkeypatch):
        import chrys.service.agent_middleware  # noqa: F401  — break the pipeline<->middleware import cycle
        from chrys.service.mutations.pipeline import prepare_mutation_tracking

        monkeypatch.setattr("chrys.service.mutations.pipeline.allocate_trace_file", lambda _dir: None)
        tracker = MutationTracker(SnapshotStore(tmp_path / "session"))
        tracker.start_turn(1)

        ctx = await prepare_mutation_tracking(tracker, "shell", {"command": "true"}, "c1", is_shell=True)
        assert ctx.trace_file is None
        assert shell_trace_argv.get() is None

    async def test_trace_hit_upgrades_row_and_publishes_claim(self, tmp_path, monkeypatch):
        import chrys.service.agent_middleware  # noqa: F401  — break the pipeline<->middleware import cycle
        from chrys.service.mutations.pipeline import finalize_mutation_tracking, prepare_mutation_tracking

        trace_file = _arm_fake_allocation(monkeypatch, tmp_path)
        root = make_tree(tmp_path)
        f = root / "built.txt"
        tracker = MutationTracker(SnapshotStore(tmp_path / "session"))
        tracker.start_turn(1)
        coord = _make_coordinator(tmp_path)

        ctx = await prepare_mutation_tracking(
            tracker,
            "shell",
            {"command": f"echo x > {f}", "workdir": str(root)},
            "c1",
            is_shell=True,
            workspace_cwd=str(root),
            coordinator=coord,
        )
        f.write_text("payload")  # the "shell" writes during the window
        trace_file.write_text(f"w|{f}\n")
        await finalize_mutation_tracking(tracker, ctx, "c1", coordinator=coord)

        rows = [m for t in tracker.with_locked_log(lambda log: list(log.turns)) for m in t.mutations]
        row = next(m for m in rows if m.path.endswith("built.txt"))
        assert row.provenance is MutationProvenance.PROVEN
        assert row.source is MutationSource.SHELL

        own = _read_own_registry(tmp_path, root)
        (claim,) = own["claims"]
        assert claim["key"] == canonical_key(str(f))
        assert claim["source"] == "shell"
        # Interval is the full observation window.
        assert claim["t0"] == ctx.window_opened_at
        assert not trace_file.exists()

    async def test_no_trace_hit_stays_assumed_and_publishes_nothing(self, tmp_path, monkeypatch):
        import chrys.service.agent_middleware  # noqa: F401  — break the pipeline<->middleware import cycle
        from chrys.service.mutations.pipeline import finalize_mutation_tracking, prepare_mutation_tracking

        trace_file = _arm_fake_allocation(monkeypatch, tmp_path)
        root = make_tree(tmp_path)
        f = root / "built.txt"
        tracker = MutationTracker(SnapshotStore(tmp_path / "session"))
        tracker.start_turn(1)
        coord = _make_coordinator(tmp_path)

        ctx = await prepare_mutation_tracking(
            tracker,
            "shell",
            {"command": f"echo x > {f}", "workdir": str(root)},
            "c1",
            is_shell=True,
            workspace_cwd=str(root),
            coordinator=coord,
        )
        f.write_text("payload")
        trace_file.write_text("")  # trace ran but observed nothing (blind spot)
        await finalize_mutation_tracking(tracker, ctx, "c1", coordinator=coord)

        rows = [m for t in tracker.with_locked_log(lambda log: list(log.turns)) for m in t.mutations]
        row = next(m for m in rows if m.path.endswith("built.txt"))
        assert row.provenance is MutationProvenance.ASSUMED
        own = _read_own_registry(tmp_path, root)
        assert own["claims"] == []
        (window,) = own["windows"]
        assert window["end"] is not None

    async def test_trace_proven_rows_publish_even_when_tool_errored(self, tmp_path, monkeypatch):
        """A failing build still wrote what it wrote — the trace IS the
        authorship evidence, unlike the delta-only write/edit gate."""
        import chrys.service.agent_middleware  # noqa: F401  — break the pipeline<->middleware import cycle
        from chrys.service.mutations.pipeline import finalize_mutation_tracking, prepare_mutation_tracking

        trace_file = _arm_fake_allocation(monkeypatch, tmp_path)
        root = make_tree(tmp_path)
        f = root / "partial.o"
        tracker = MutationTracker(SnapshotStore(tmp_path / "session"))
        tracker.start_turn(1)
        coord = _make_coordinator(tmp_path)

        ctx = await prepare_mutation_tracking(
            tracker,
            "shell",
            {"command": f"echo x > {f}", "workdir": str(root)},
            "c1",
            is_shell=True,
            workspace_cwd=str(root),
            coordinator=coord,
        )
        f.write_text("partial output")
        trace_file.write_text(f"w|{f}\n")
        await finalize_mutation_tracking(tracker, ctx, "c1", coordinator=coord, tool_errored=True)

        own = _read_own_registry(tmp_path, root)
        (claim,) = own["claims"]
        assert claim["key"] == canonical_key(str(f))

    async def test_move_row_upgrades_via_source_side(self, tmp_path):
        import chrys.service.agent_middleware  # noqa: F401  — break the pipeline<->middleware import cycle
        from chrys.service.mutations.pipeline import MutationContext, _apply_trace_upgrades

        src, dst = tmp_path / "old.txt", tmp_path / "new.txt"
        row = FileMutation(
            path=str(dst),
            operation=MutationOp.MOVE,
            source=MutationSource.SHELL,
            tool_call_id="c1",
            timestamp=time.time(),
            old_path=str(src),
            provenance=MutationProvenance.ASSUMED,
        )
        trace = tmp_path / "t.log"
        trace.write_text(f"w|{src}\n")  # only the source side appears
        ctx = MutationContext(trace_file=str(trace))
        upgraded = _apply_trace_upgrades(ctx, [row])
        assert upgraded == [row]
        assert row.provenance is MutationProvenance.PROVEN
        assert ctx.trace_file is None
        assert not trace.exists()

    async def test_row_matched_on_both_sides_upgrades_once(self, tmp_path):
        import chrys.service.agent_middleware  # noqa: F401  — break the pipeline<->middleware import cycle
        from chrys.service.mutations.pipeline import MutationContext, _apply_trace_upgrades

        src, dst = tmp_path / "old.txt", tmp_path / "new.txt"
        row = FileMutation(
            path=str(dst),
            operation=MutationOp.MOVE,
            source=MutationSource.SHELL,
            tool_call_id="c1",
            timestamp=time.time(),
            old_path=str(src),
            provenance=MutationProvenance.ASSUMED,
        )
        trace = tmp_path / "t.log"
        trace.write_text(f"m|{dst}|{src}\n")
        ctx = MutationContext(trace_file=str(trace))
        assert _apply_trace_upgrades(ctx, [row]) == [row]


def _make_coordinator(tmp_path, session_id="sess-a"):
    from chrys.service.mutations.coordination import MutationCoordinator

    return MutationCoordinator(registry_root=tmp_path / ATTRIBUTION_DIR_NAME, session_id=session_id)


def _read_own_registry(tmp_path, root, session_id="sess-a"):
    import json

    own = tmp_path / ATTRIBUTION_DIR_NAME / repo_key_for_root(str(root)) / f"{session_id}.json"
    return json.loads(own.read_text())


# ---------------------------------------------------------------------------
# Abort paths: cancellation and approval rejection release the wrapper
# ---------------------------------------------------------------------------


class TestAbortReleasesTrace:
    async def test_abort_releases_wrapper_and_removes_trace(self, tmp_path, monkeypatch):
        import chrys.service.agent_middleware  # noqa: F401  — break the pipeline<->middleware import cycle
        from chrys.service.mutations.pipeline import abort_mutation_tracking, prepare_mutation_tracking

        trace_file = _arm_fake_allocation(monkeypatch, tmp_path)
        root = make_tree(tmp_path)
        tracker = MutationTracker(SnapshotStore(tmp_path / "session"))
        tracker.start_turn(1)
        coord = _make_coordinator(tmp_path)

        ctx = await prepare_mutation_tracking(
            tracker,
            "shell",
            {"command": "true", "workdir": str(root)},
            "c1",
            is_shell=True,
            workspace_cwd=str(root),
            coordinator=coord,
        )
        trace_file.write_text("")
        assert shell_trace_argv.get() is not None

        abort_mutation_tracking(ctx, coord)

        assert shell_trace_argv.get() is None
        assert ctx.trace_token is None
        assert ctx.trace_file is None
        assert not trace_file.exists()
        own = _read_own_registry(tmp_path, root)
        (window,) = own["windows"]
        assert window["end"] is not None

    async def test_rejected_tool_releases_trace_and_window(self, tmp_path, monkeypatch):
        """Approval rejection after prepare skips finalize — the middleware
        must abort or the wrapper stays armed and the window advertises a
        live command until shutdown."""
        from types import SimpleNamespace

        from chrys.foundation.events.bus import EventBus
        from chrys.foundation.tool_kinds import KIND_SHELL
        from chrys.service.agent_middleware._metadata_keys import _APPROVAL_REJECTED_KEY
        from chrys.service.agent_middleware.events.tool_events import ToolEventMiddleware

        trace_file = _arm_fake_allocation(monkeypatch, tmp_path)
        root = make_tree(tmp_path)
        tracker = MutationTracker(SnapshotStore(tmp_path / "session"))
        tracker.start_turn(1)
        coord = _make_coordinator(tmp_path, session_id="s1")
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

        async def rejecting_next() -> None:
            trace_file.write_text("")  # as if fsatrace had started
            ctx.metadata[_APPROVAL_REJECTED_KEY] = True

        await mw.process(ctx, rejecting_next)

        assert shell_trace_argv.get() is None
        assert not trace_file.exists()
        own = _read_own_registry(tmp_path, root, session_id="s1")
        (window,) = own["windows"]
        assert window["end"] is not None
        assert own["claims"] == []
        # Rejected tools never executed — detection is complete, not truncated.
        assert tracker.get_turn_mutations(1).detection_truncated is False

    async def test_cancelled_tool_releases_trace(self, tmp_path, monkeypatch):
        import asyncio
        from types import SimpleNamespace

        from chrys.foundation.events.bus import EventBus
        from chrys.foundation.tool_kinds import KIND_SHELL
        from chrys.service.agent_middleware.events.tool_events import ToolEventMiddleware

        trace_file = _arm_fake_allocation(monkeypatch, tmp_path)
        root = make_tree(tmp_path)
        tracker = MutationTracker(SnapshotStore(tmp_path / "session"))
        tracker.start_turn(1)
        mw = ToolEventMiddleware(
            EventBus(),
            session_id="s1",
            mutation_tracker=tracker,
            workspace_cwd=str(root),
            origin=InvocationOrigin("turn", "s1", "turn-test", None),
        )
        ctx = SimpleNamespace(
            function=SimpleNamespace(name="shell", chrys_kind=KIND_SHELL),
            arguments={"command": "true"},
            result=None,
            metadata={},
        )

        async def cancelled_next() -> None:
            trace_file.write_text("")
            raise asyncio.CancelledError

        with pytest.raises(asyncio.CancelledError):
            await mw.process(ctx, cancelled_next)

        assert shell_trace_argv.get() is None
        assert not trace_file.exists()
        # The command may have written before the interrupt and post-command
        # detection never ran — the turn must flag incomplete detection or
        # that write vanishes from the next workspace notice entirely.
        assert tracker.get_turn_mutations(1).detection_truncated is True

    async def test_cancel_during_finalize_releases_trace_and_window(self, tmp_path, monkeypatch):
        """An interrupt landing DURING finalize (its executor hops) bypasses
        the ``cancelled`` flag (which only covers call_next) — the middleware
        must still abort synchronously on the way out."""
        import asyncio
        from types import SimpleNamespace

        from chrys.foundation.events.bus import EventBus
        from chrys.foundation.tool_kinds import KIND_SHELL
        from chrys.service.agent_middleware.events.tool_events import ToolEventMiddleware

        trace_file = _arm_fake_allocation(monkeypatch, tmp_path)
        root = make_tree(tmp_path)
        tracker = MutationTracker(SnapshotStore(tmp_path / "session"))
        tracker.start_turn(1)
        coord = _make_coordinator(tmp_path, session_id="s1")
        mw = ToolEventMiddleware(
            EventBus(),
            session_id="s1",
            mutation_tracker=tracker,
            workspace_cwd=str(root),
            mutation_coordinator=coord,
            origin=InvocationOrigin("turn", "s1", "turn-test", None),
        )

        async def cancelled_finalize(*_args, **_kwargs):
            raise asyncio.CancelledError

        monkeypatch.setattr(
            "chrys.service.agent_middleware.events.tool_events.finalize_mutation_tracking",
            cancelled_finalize,
        )
        ctx = SimpleNamespace(
            function=SimpleNamespace(name="shell", chrys_kind=KIND_SHELL),
            arguments={"command": "true"},
            result=None,
            metadata={},
        )

        async def ok_next() -> None:
            trace_file.write_text("")

        with pytest.raises(asyncio.CancelledError):
            await mw.process(ctx, ok_next)

        assert shell_trace_argv.get() is None
        assert not trace_file.exists()
        own = _read_own_registry(tmp_path, root, session_id="s1")
        (window,) = own["windows"]
        assert window["end"] is not None
        # The command ran; its aborted detection is truncated.
        assert tracker.get_turn_mutations(1).detection_truncated is True

    async def test_cancel_during_prepare_closes_window_and_releases_trace(self, tmp_path, monkeypatch):
        """A cancellation on prepare's later awaits (git capture, pre-scan)
        propagates before the caller owns a MutationContext — prepare must
        clean up its own just-opened window and armed wrapper."""
        import asyncio

        import chrys.service.agent_middleware  # noqa: F401  — break the pipeline<->middleware import cycle
        from chrys.service.mutations.pipeline import prepare_mutation_tracking

        _arm_fake_allocation(monkeypatch, tmp_path)
        root = make_tree(tmp_path)
        tracker = MutationTracker(SnapshotStore(tmp_path / "session"))
        tracker.start_turn(1)
        coord = _make_coordinator(tmp_path)

        async def cancelled_capture(*_args, **_kwargs):
            raise asyncio.CancelledError

        monkeypatch.setattr("chrys.service.mutations.pipeline._track_git_capture_before", cancelled_capture)

        with pytest.raises(asyncio.CancelledError):
            await prepare_mutation_tracking(
                tracker,
                "shell",
                {"command": "true", "workdir": str(root)},
                "c1",
                is_shell=True,
                workspace_cwd=str(root),
                coordinator=coord,
            )

        assert shell_trace_argv.get() is None
        own = _read_own_registry(tmp_path, root)
        (window,) = own["windows"]
        assert window["end"] is not None
        # The command never ran — the prepare-phase abort must not flag
        # incomplete detection.
        assert tracker.get_turn_mutations(1).detection_truncated is False

    async def test_abort_without_detection_artifacts_does_not_mark_truncated(self, tmp_path):
        """A cancelled file-tool call keeps its explicit mutation row —
        aborting it must not flag implicit-detection truncation."""
        import chrys.service.agent_middleware  # noqa: F401  — break the pipeline<->middleware import cycle
        from chrys.service.mutations.pipeline import MutationContext, abort_mutation_tracking

        tracker = MutationTracker(SnapshotStore(tmp_path / "session"))
        tracker.start_turn(1)

        abort_mutation_tracking(MutationContext(), None, tracker=tracker)

        assert tracker.get_turn_mutations(1).detection_truncated is False

    async def test_cancelled_shell_with_zero_artifacts_still_marks_truncated(self, tmp_path, monkeypatch):
        """Coordination off + non-Git cwd + opaque command + no fsatrace
        leaves every detection artifact None, yet the command executed —
        the abort must key off the call class, not the artifacts."""
        import chrys.service.agent_middleware  # noqa: F401  — break the pipeline<->middleware import cycle
        from chrys.service.mutations import pipeline as pipeline_mod
        from chrys.service.mutations.pipeline import abort_mutation_tracking, prepare_mutation_tracking

        monkeypatch.setattr(pipeline_mod, "allocate_trace_file", lambda _dir: None)
        root = tmp_path / "ws"
        root.mkdir()
        tracker = MutationTracker(SnapshotStore(tmp_path / "session"))
        tracker.start_turn(1)

        ctx = await prepare_mutation_tracking(
            tracker,
            "shell",
            {"command": "true", "workdir": str(root)},
            "c1",
            is_shell=True,
            workspace_cwd=str(root),
            coordinator=None,
        )

        assert ctx.window_id is None
        assert ctx.git_calibrator is None
        assert ctx.shell_scan_before is None
        assert ctx.trace_file is None
        assert ctx.implicit_detection is True

        abort_mutation_tracking(ctx, None, tracker=tracker)

        assert tracker.get_turn_mutations(1).detection_truncated is True

    async def test_abort_after_finalize_consumed_trace_is_idempotent(self, tmp_path):
        """The claim-then-discard discipline: once _apply_trace_upgrades has
        claimed the path, a late abort sees None and must be a no-op."""
        import chrys.service.agent_middleware  # noqa: F401  — break the pipeline<->middleware import cycle
        from chrys.service.mutations.pipeline import (
            MutationContext,
            _apply_trace_upgrades,
            abort_mutation_tracking,
        )

        trace = tmp_path / "t.log"
        trace.write_text("")
        ctx = MutationContext(trace_file=str(trace))
        _apply_trace_upgrades(ctx, [])
        assert ctx.trace_file is None
        assert not trace.exists()
        abort_mutation_tracking(ctx, None)  # must not raise or resurrect state
        assert ctx.trace_file is None


class TestWindowOpenCancellation:
    async def test_cancel_during_window_open_still_closes_window(self, tmp_path, monkeypatch):
        """Cancellation landing exactly on the open_window await abandons
        the executor call with the window id unassigned — the handshake
        must make the thread close its own orphaned window."""
        import asyncio
        import threading

        import chrys.service.agent_middleware  # noqa: F401  — break the pipeline<->middleware import cycle
        from chrys.service.mutations.pipeline import prepare_mutation_tracking

        root = make_tree(tmp_path)
        tracker = MutationTracker(SnapshotStore(tmp_path / "session"))
        tracker.start_turn(1)
        coord = _make_coordinator(tmp_path)

        entered = threading.Event()
        release = threading.Event()
        closed = threading.Event()
        closed_ids: list[str | None] = []
        real_open = coord.open_window
        real_close = coord.close_window

        def gated_open(scope, *, fallback_root=None):
            entered.set()
            # Only the test releases this barrier, including on failure. An
            # expiring gate would stop controlling the cancellation ordering.
            release.wait()
            return real_open(scope, fallback_root=fallback_root)

        def observed_close(window_id: str | None) -> None:
            try:
                real_close(window_id)
                closed_ids.append(window_id)
            finally:
                closed.set()

        monkeypatch.setattr(coord, "open_window", gated_open)
        monkeypatch.setattr(coord, "close_window", observed_close)

        task = asyncio.ensure_future(
            prepare_mutation_tracking(
                tracker,
                "shell",
                {"command": "true", "workdir": str(root)},
                "c1",
                is_shell=True,
                workspace_cwd=str(root),
                coordinator=coord,
            )
        )
        try:
            await wait_for(entered.is_set, timeout=10, description="executor reached window-open barrier")
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            release.set()

            # A concurrent read_text opens a Windows handle without delete
            # sharing and can make close_window's atomic replace fail. Observe
            # completion without opening the registry, then inspect persistence.
            await wait_for(closed.is_set, timeout=10, description="orphaned window close completed")
            (window,) = _read_own_registry(tmp_path, root)["windows"]
            assert closed_ids == [window["id"]]
            assert window["end"] is not None
            assert window["end"] >= window["start"]
        finally:
            release.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            if entered.is_set():
                await wait_for(closed.is_set, timeout=10, description="window worker teardown")
            coord.close()
