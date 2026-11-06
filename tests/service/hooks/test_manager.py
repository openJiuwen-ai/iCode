# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for :class:`HookManager`.

The manager is async, so most tests are ``pytest.mark.asyncio``.  Hook
subprocesses are real ``python -c`` invocations — this keeps the tests
honest about the subprocess plumbing without inventing a mocking layer
that would diverge from production behaviour.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
import textwrap
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from chrys.foundation.trajectory.context import trajectory_scope
from chrys.foundation.trajectory.envelope import EventDraft
from chrys.foundation.trajectory.event_types import EventType, RuntimeFinishReason
from chrys.foundation.trajectory.writer import EmitResult
from chrys.service.hooks.events import HookEvent
from chrys.service.hooks.manager import HookManager
from chrys.service.hooks.outbox import DONE, FAILED, OutboxJob
from chrys.service.hooks.runner import HookResult
from chrys.service.hooks.schema import (
    HookArgMatch,
    HookConfig,
    HookExecution,
    HookMatch,
    HookRun,
    HookSettings,
    HooksFile,
    MergedHooksFile,
)
from chrys.service.trajectory.hooks import HookOutcome
from chrys.service.trajectory.session import SessionStartInfo, SessionTrajectory, trajectory_events_path
from tests.service.trajectory._fakes import FakeSink, make_context
from tests.support.gc_isolation import earlier_tests_heap_frozen
from tests.support.stopped_child import STOPS_ITSELF
from tests.support.trajectory_invariants import assert_trajectory_file_accounted, assert_trajectory_operation_settlement
from tests.support.waiting import wait_until


def _file(*hooks: HookConfig, **settings_overrides: float | int) -> HooksFile:
    return HooksFile(
        version=1,
        settings=HookSettings(**settings_overrides),
        hooks=list(hooks),
    )


def _hook(
    *,
    hook_id: str,
    event: HookEvent,
    mode: str = "blocking",
    timeout: float = 5.0,
    delivery: str = "best_effort",
    detach: bool = False,
    argv: list[str] | None = None,
    script: str = "pass",
    match: HookMatch | None = None,
    on_error: str = "warn",
) -> HookConfig:
    return HookConfig(
        id=hook_id,
        event=event,
        run=HookRun(type="command", argv=argv or [sys.executable, "-c", script]),
        execution=HookExecution(
            mode=mode,
            timeout_seconds=timeout,
            delivery=delivery,
            detach=detach,
            on_error=on_error,
        ),
        match=match or HookMatch(),
    )


@pytest.fixture
def hooks_dir(tmp_path: Path) -> Path:
    d = tmp_path / "hooks"
    d.mkdir()
    return d


class _HeldStartAckSink(FakeSink):
    """Commit a hook start marker, then hold its producer's acknowledgement."""

    def __init__(self) -> None:
        super().__init__()
        self.started_ack_held = asyncio.Event()
        self.release_started_ack = asyncio.Event()

    async def emit(
        self,
        draft: EventDraft,
        *,
        payload_factory: Callable[[int], Mapping[str, Any]] | None = None,
    ) -> EmitResult:
        result = await super().emit(draft, payload_factory=payload_factory)
        if draft.event_type == EventType.HOOK_OPERATION_STARTED:
            self.started_ack_held.set()
            await self.release_started_ack.wait()
        return result


@pytest.mark.asyncio
async def test_no_hooks_is_no_op(hooks_dir: Path) -> None:
    mgr = HookManager(file=MergedHooksFile.from_single(_file()), hooks_dir=hooks_dir)
    assert mgr.has_hooks_for(HookEvent.BEFORE_TOOL_CALL) is False
    decision = await mgr.fire(HookEvent.BEFORE_TOOL_CALL, {"tool": {"name": "shell"}})
    assert decision.blocked is False
    assert decision.args_override is None


def test_plain_hooks_file_is_normalized_to_merged_hooks_file(hooks_dir: Path) -> None:
    mgr = HookManager(file=_file(), hooks_dir=hooks_dir)
    assert isinstance(mgr.file, MergedHooksFile)
    assert mgr.sources() == []


@pytest.mark.asyncio
async def test_blocking_hook_block_action(hooks_dir: Path) -> None:
    script = textwrap.dedent(
        """
        import json, os
        with open(os.environ["CHRYS_HOOK_RESULT"], "w") as f:
            json.dump({"action": "block", "reason": "policy: rm denied"}, f)
        """
    )
    mgr = HookManager(
        file=_file(_hook(hook_id="g", event=HookEvent.BEFORE_TOOL_CALL, script=script)),
        hooks_dir=hooks_dir,
    )
    decision = await mgr.fire(
        HookEvent.BEFORE_TOOL_CALL,
        {"profile": "Code", "tool": {"name": "shell", "args": {"command": "rm -rf /tmp"}}},
    )
    assert decision.blocked is True
    assert "policy: rm denied" in decision.block_reason


@pytest.mark.asyncio
async def test_block_action_on_non_gated_event_does_not_short_circuit_later_hooks(hooks_dir: Path) -> None:
    marker = hooks_dir / "observer-ran.txt"
    block_script = textwrap.dedent(
        """
        import json, os
        with open(os.environ["CHRYS_HOOK_RESULT"], "w") as f:
            json.dump({"action": "block", "reason": "not meaningful here"}, f)
        """
    )
    observer_script = f"open({str(marker)!r}, 'w').write('ran')"
    mgr = HookManager(
        file=_file(
            _hook(hook_id="blocker", event=HookEvent.AFTER_TURN, script=block_script),
            _hook(hook_id="observer", event=HookEvent.AFTER_TURN, mode="async", script=observer_script),
        ),
        hooks_dir=hooks_dir,
    )

    decision = await mgr.fire(HookEvent.AFTER_TURN, {"turn": 1})
    await mgr.drain_turn()

    assert decision.blocked is False
    assert marker.read_text() == "ran"


@pytest.mark.asyncio
async def test_after_tool_block_action_ignored_but_extra_context_kept(hooks_dir: Path) -> None:
    script = textwrap.dedent(
        """
        import json, os
        with open(os.environ["CHRYS_HOOK_RESULT"], "w") as f:
            json.dump({"action": "block", "reason": "ignored", "extra_context": "note"}, f)
        """
    )
    mgr = HookManager(
        file=_file(_hook(hook_id="note", event=HookEvent.AFTER_TOOL_CALL, script=script)),
        hooks_dir=hooks_dir,
    )

    decision = await mgr.fire(HookEvent.AFTER_TOOL_CALL, {"tool": {"name": "read_file"}})

    assert decision.blocked is False
    assert decision.extra_context == ["note"]


@pytest.mark.asyncio
async def test_blocking_hook_modify_action(hooks_dir: Path) -> None:
    script = textwrap.dedent(
        """
        import json, os
        with open(os.environ["CHRYS_HOOK_RESULT"], "w") as f:
            json.dump({"action": "modify", "args_override": {"path": "/safer/path"}}, f)
        """
    )
    mgr = HookManager(
        file=_file(_hook(hook_id="m", event=HookEvent.BEFORE_TOOL_CALL, script=script)),
        hooks_dir=hooks_dir,
    )
    decision = await mgr.fire(HookEvent.BEFORE_TOOL_CALL, {"tool": {"args": {"path": "/etc/passwd"}}})
    assert decision.blocked is False
    assert decision.args_override == {"path": "/safer/path"}


@pytest.mark.asyncio
async def test_blocking_hooks_compose_args_for_later_matches(hooks_dir: Path) -> None:
    normalize = textwrap.dedent(
        """
        import json, os
        with open(os.environ["CHRYS_HOOK_RESULT"], "w") as f:
            json.dump({"action": "modify", "args_override": {"path": "/normalized"}}, f)
        """
    )
    block_normalized = textwrap.dedent(
        """
        import json, os
        with open(os.environ["CHRYS_HOOK_RESULT"], "w") as f:
            json.dump({"action": "block", "reason": "normalized path denied"}, f)
        """
    )
    mgr = HookManager(
        file=_file(
            _hook(hook_id="normalize", event=HookEvent.BEFORE_TOOL_CALL, script=normalize),
            _hook(
                hook_id="deny-normalized",
                event=HookEvent.BEFORE_TOOL_CALL,
                script=block_normalized,
                match=HookMatch(args={"path": HookArgMatch(equals="/normalized")}),
            ),
        ),
        hooks_dir=hooks_dir,
    )
    decision = await mgr.fire(HookEvent.BEFORE_TOOL_CALL, {"tool": {"args": {"path": "/raw"}}})
    assert decision.blocked is True
    assert "normalized path denied" in decision.block_reason


@pytest.mark.asyncio
async def test_matcher_filters_hooks(hooks_dir: Path) -> None:
    script_block = textwrap.dedent(
        """
        import json, os
        with open(os.environ["CHRYS_HOOK_RESULT"], "w") as f:
            json.dump({"action": "block", "reason": "fired"}, f)
        """
    )
    # Hook only fires when profile == "Code".
    h = _hook(
        hook_id="cond",
        event=HookEvent.BEFORE_TOOL_CALL,
        script=script_block,
        match=HookMatch(profile="Code"),
    )
    mgr = HookManager(file=MergedHooksFile.from_single(_file(h)), hooks_dir=hooks_dir)

    # Wrong profile: no fire.
    decision_qa = await mgr.fire(HookEvent.BEFORE_TOOL_CALL, {"profile": "QA"})
    assert decision_qa.blocked is False

    # Right profile: blocks.
    decision_code = await mgr.fire(HookEvent.BEFORE_TOOL_CALL, {"profile": "Code"})
    assert decision_code.blocked is True


@pytest.mark.asyncio
async def test_blocking_hook_timeout_with_on_error_block(hooks_dir: Path) -> None:
    mgr = HookManager(
        file=_file(
            _hook(
                hook_id="slow",
                event=HookEvent.BEFORE_TOOL_CALL,
                script="import time; time.sleep(10)",
                timeout=0.3,
                on_error="block",
            )
        ),
        hooks_dir=hooks_dir,
    )
    decision = await mgr.fire(HookEvent.BEFORE_TOOL_CALL, {})
    assert decision.blocked is True
    assert "timeout" in decision.block_reason


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups and SIGSTOP only")
@pytest.mark.asyncio
async def test_a_block_by_a_stopped_hook_says_it_stopped_past_a_long_stderr(hooks_dir: Path, tmp_path: Path) -> None:
    """The reason is cut at 200 characters of stderr, which the stopped note only ends."""
    pid_file = tmp_path / "child.pid"
    child = f"import os, pathlib; pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid())); {STOPS_ITSELF}"
    script = textwrap.dedent(
        f"""
        import subprocess, sys
        sys.stderr.write("x" * 500)
        sys.stderr.flush()
        subprocess.Popen([sys.executable, "-c", {child!r}])
        """
    )
    hook = _hook(hook_id="stops", event=HookEvent.BEFORE_TOOL_CALL, script=script, timeout=10, on_error="block")
    mgr = HookManager(file=_file(hook), hooks_dir=hooks_dir)

    decision = await mgr.fire(HookEvent.BEFORE_TOOL_CALL, {})

    assert decision.blocked is True
    assert decision.block_reason.startswith("hook 'stops' failed (stopped): xxx")


@pytest.mark.asyncio
async def test_blocking_hook_timeout_with_on_error_warn(hooks_dir: Path) -> None:
    mgr = HookManager(
        file=_file(
            _hook(
                hook_id="slow",
                event=HookEvent.BEFORE_TOOL_CALL,
                script="import time; time.sleep(10)",
                timeout=0.3,
                on_error="warn",
            )
        ),
        hooks_dir=hooks_dir,
    )
    decision = await mgr.fire(HookEvent.BEFORE_TOOL_CALL, {})
    assert decision.blocked is False


@pytest.mark.asyncio
async def test_failed_blocking_hook_ignores_decision_file_when_on_error_warn(hooks_dir: Path) -> None:
    script = textwrap.dedent(
        """
        import json, os, sys
        with open(os.environ["CHRYS_HOOK_RESULT"], "w") as f:
            json.dump({"action": "block", "reason": "partial stale decision"}, f)
        sys.exit(9)
        """
    )
    mgr = HookManager(
        file=_file(_hook(hook_id="partial", event=HookEvent.BEFORE_TOOL_CALL, script=script, on_error="warn")),
        hooks_dir=hooks_dir,
    )
    decision = await mgr.fire(HookEvent.BEFORE_TOOL_CALL, {})
    assert decision.blocked is False


@pytest.mark.asyncio
async def test_blocking_runner_exception_honors_on_error_block(
    monkeypatch: pytest.MonkeyPatch,
    hooks_dir: Path,
) -> None:
    mgr = HookManager(
        file=_file(_hook(hook_id="internal", event=HookEvent.BEFORE_TOOL_CALL, on_error="block")),
        hooks_dir=hooks_dir,
    )

    async def _boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("runner exploded")

    monkeypatch.setattr(mgr._runner, "run_and_wait", _boom)

    decision = await mgr.fire(HookEvent.BEFORE_TOOL_CALL, {})

    assert decision.blocked is True
    assert "runner exploded" in decision.block_reason


@pytest.mark.asyncio
async def test_blocking_hook_launch_error_with_on_error_block(hooks_dir: Path) -> None:
    mgr = HookManager(
        file=_file(
            _hook(
                hook_id="missing",
                event=HookEvent.BEFORE_TOOL_CALL,
                argv=["/definitely/not/a/real/chrys-hook-command"],
                on_error="block",
            )
        ),
        hooks_dir=hooks_dir,
    )
    decision = await mgr.fire(HookEvent.BEFORE_TOOL_CALL, {})
    assert decision.blocked is True
    assert "launch_error" in decision.block_reason


@pytest.mark.asyncio
async def test_async_hook_drained_at_turn_end(hooks_dir: Path) -> None:
    marker = hooks_dir / "marker.txt"
    script = textwrap.dedent(
        f"""
        import time
        time.sleep(0.2)
        open({str(marker)!r}, "w").write("done")
        """
    )
    mgr = HookManager(
        file=_file(_hook(hook_id="a", event=HookEvent.AFTER_TURN, mode="async", script=script)),
        hooks_dir=hooks_dir,
    )
    sink = FakeSink()
    with trajectory_scope(make_context(sink)):
        await mgr.fire(HookEvent.AFTER_TURN, {"turn": 1})
    # Marker shouldn't exist yet (script is sleeping).
    assert not marker.exists()
    operation_id = sink.only(EventType.HOOK_OPERATION_STARTED).operation_id
    assert await mgr.drain_turn() == (operation_id,)
    assert mgr.active_turn_drain_operation_ids == (operation_id,)
    # After drain, it must exist.
    assert marker.read_text() == "done"


@pytest.mark.asyncio
async def test_explicit_trajectory_context_applies_only_to_blocking_hooks(hooks_dir: Path) -> None:
    blocking = _hook(hook_id="blocking", event=HookEvent.USER_PROMPT_SUBMIT)
    asynchronous = _hook(hook_id="async", event=HookEvent.USER_PROMPT_SUBMIT, mode="async")
    mgr = HookManager(file=_file(blocking, asynchronous), hooks_dir=hooks_dir)
    release_async = asyncio.Event()

    async def _run_and_wait(
        hook: HookConfig,
        _payload: dict[str, object],
        *,
        payload_text: str | None = None,
        result: HookResult | None = None,
    ) -> HookResult:
        if hook is asynchronous:
            await release_async.wait()
        return HookResult(hook_id=hook.id, exit_code=0)

    mgr._runner.run_and_wait = _run_and_wait  # type: ignore[method-assign]
    sink = FakeSink()
    active_context = make_context(sink)
    admission_context = active_context.with_turn(None).with_run(None)
    mgr.trajectory_context_provider = lambda: active_context
    target_operation_id = "a" * 32

    await mgr.fire(
        HookEvent.USER_PROMPT_SUBMIT,
        {"text": "queued injection"},
        target_operation_id=target_operation_id,
        trajectory_context=admission_context,
    )

    started = {draft.payload["hook_key"]: draft for draft in sink.of_type(EventType.HOOK_OPERATION_STARTED)}
    assert started[blocking.id].turn_id is None
    assert started[asynchronous.id].turn_id == active_context.turn_id
    for draft in started.values():
        assert draft.parent_operation_id == target_operation_id
        assert draft.payload["target_operation_id"] == target_operation_id

    async_operation_id = started[asynchronous.id].operation_id
    drain_task = asyncio.create_task(mgr.drain_turn())
    await asyncio.sleep(0)
    assert mgr.active_turn_drain_operation_ids == (async_operation_id,)
    release_async.set()
    assert await drain_task == (async_operation_id,)

    finished = {draft.operation_id: draft for draft in sink.of_type(EventType.HOOK_OPERATION_FINISHED)}
    assert finished[started[blocking.id].operation_id].turn_id is None
    assert finished[async_operation_id].turn_id == active_context.turn_id
    sink.assert_operations_settled()


@pytest.mark.asyncio
async def test_drain_turn_reaps_tasks_that_finished_before_drain(hooks_dir: Path) -> None:
    mgr = HookManager(
        file=_file(_hook(hook_id="fast", event=HookEvent.AFTER_TURN, mode="async", script="pass")),
        hooks_dir=hooks_dir,
    )
    with trajectory_scope(make_context(FakeSink())):
        await mgr.fire(HookEvent.AFTER_TURN, {"turn": 1})
    # Wait for the "fast" async hook to finish *before* draining: this test
    # pins that drain_turn reaps a task that completed prior to the drain call.
    # A fixed sleep races the subprocess spawn under slow/parallel CI, so poll
    # on the tracked task's completion instead.
    assert await wait_until(lambda: all(t.task.done() for t in mgr._inflight))
    assert await mgr.drain_turn() == ()
    assert mgr.active_turn_drain_operation_ids == ()
    assert mgr._inflight == []


@pytest.mark.asyncio
async def test_fire_and_forget_is_not_awaited_by_turn_drain(hooks_dir: Path) -> None:
    marker = hooks_dir / "fire-and-forget.txt"
    script = textwrap.dedent(
        f"""
        import time
        time.sleep(0.5)
        open({str(marker)!r}, "w").write("done")
        """
    )
    mgr = HookManager(
        file=_file(_hook(hook_id="ff", event=HookEvent.AFTER_TURN, mode="fire_and_forget", script=script)),
        hooks_dir=hooks_dir,
    )
    sink = FakeSink()

    with earlier_tests_heap_frozen():
        start = time.monotonic()
        with trajectory_scope(make_context(sink)):
            await mgr.fire(HookEvent.AFTER_TURN, {"turn": 1}, scope="turn")
        await mgr.drain_turn()
        elapsed = time.monotonic() - start

    assert elapsed < 0.45
    assert not marker.exists()
    started = sink.only(EventType.HOOK_OPERATION_STARTED)
    assert started.payload["hook_key"] == "ff"
    assert started.payload["scope"] == "turn"
    assert started.payload["drain_scope"] == "background"
    # Poll on content, not existence: `open(path, "w")` creates the file
    # before the subsequent `.write("done")`, so a bare-existence check can
    # race in between and observe an empty file.  The budget is generous
    # because the marker only appears after the hook subprocess spawns and
    # runs its 0.5s sleep — under parallel xdist on Windows CI the spawn
    # alone can take several seconds, so a tight budget flakes.
    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline:
        try:
            if marker.read_text() == "done":
                break
        except FileNotFoundError:
            pass
        await asyncio.sleep(0.05)
    else:
        pytest.fail("fire-and-forget hook never wrote its marker within the poll budget")
    assert marker.read_text() == "done"


@pytest.mark.asyncio
async def test_async_hook_payload_is_snapshotted_at_dispatch(hooks_dir: Path) -> None:
    marker = hooks_dir / "payload.txt"
    script = textwrap.dedent(
        f"""
        import json, os
        with open(os.environ["CHRYS_HOOK_PAYLOAD_FILE"]) as f:
            payload = json.load(f)
        open({str(marker)!r}, "w").write(payload["tool"]["args"]["path"])
        """
    )
    mgr = HookManager(
        file=_file(_hook(hook_id="snapshot", event=HookEvent.AFTER_TOOL_CALL, mode="async", script=script)),
        hooks_dir=hooks_dir,
    )
    await mgr._sem.acquire()
    payload = {"tool": {"name": "write_file", "args": {"path": "/original"}}}
    await mgr.fire(HookEvent.AFTER_TOOL_CALL, payload)
    payload["tool"]["args"]["path"] = "/mutated"
    mgr._sem.release()
    await mgr.drain_turn()
    assert marker.read_text() == "/original"


@pytest.mark.asyncio
async def test_durable_hook_writes_to_done_on_success(hooks_dir: Path) -> None:
    mgr = HookManager(
        file=_file(
            _hook(
                hook_id="d",
                event=HookEvent.AFTER_TURN,
                mode="async",
                delivery="durable",
                script="pass",
            )
        ),
        hooks_dir=hooks_dir,
    )
    await mgr.fire(HookEvent.AFTER_TURN, {"turn": 1})
    await mgr.drain_turn()
    done_files = list((hooks_dir / "outbox" / DONE).glob("*.json"))
    assert len(done_files) == 1


class _CancelOnFinishedSink(FakeSink):
    """A sink whose terminal hook write is interrupted, the way a shutdown would."""

    async def emit(self, draft, *, payload_factory=None):  # type: ignore[no-untyped-def]
        if draft.event_type == EventType.HOOK_OPERATION_FINISHED:
            raise asyncio.CancelledError
        return await super().emit(draft, payload_factory=payload_factory)


@pytest.mark.asyncio
async def test_a_durable_hook_is_committed_before_its_span_is_written(hooks_dir: Path) -> None:
    """Recording must not be able to make a finished hook run a second time."""
    mgr = HookManager(
        file=_file(
            _hook(
                hook_id="d",
                event=HookEvent.AFTER_TURN,
                mode="async",
                delivery="durable",
                script="pass",
            )
        ),
        hooks_dir=hooks_dir,
    )

    with trajectory_scope(make_context(_CancelOnFinishedSink())):
        await mgr.fire(HookEvent.AFTER_TURN, {"turn": 1})
        await mgr.drain_turn()

    # The subprocess ran to completion; an interrupt landing on the awaited
    # span marker must not leave its job pending for the next runtime to replay.
    assert len(list((hooks_dir / "outbox" / DONE).glob("*.json"))) == 1
    assert list((hooks_dir / "outbox" / "pending").glob("*.json")) == []


@pytest.mark.asyncio
async def test_durable_project_hook_persists_source_path(hooks_dir: Path) -> None:
    source = "/project-a/.chrys/hooks/hooks.yaml"
    project_hook = _hook(
        hook_id="project-durable",
        event=HookEvent.AFTER_TURN,
        mode="async",
        delivery="durable",
        script="pass",
    )
    mgr = HookManager(
        file=MergedHooksFile(
            project=HooksFile(hooks=[project_hook], source=source),
            hooks=[project_hook],
            settings=HookSettings(outbox_retry_age_seconds=0.0),
        ),
        hooks_dir=hooks_dir,
    )

    await mgr._sem.acquire()
    try:
        await mgr.fire(HookEvent.AFTER_TURN, {"turn": 1})
        pending_files = list((hooks_dir / "outbox" / "pending").glob("*.json"))
        assert len(pending_files) == 1
        data = json.loads(pending_files[0].read_text(encoding="utf-8"))
        assert data["hook_source"] == "project"
        assert data["hook_source_path"] == source
    finally:
        mgr._sem.release()
        await mgr.drain_turn()


@pytest.mark.asyncio
async def test_nonblocking_durable_outbox_write_failure_does_not_escape(
    monkeypatch: pytest.MonkeyPatch,
    hooks_dir: Path,
) -> None:
    mgr = HookManager(
        file=_file(
            _hook(
                hook_id="outbox-fails",
                event=HookEvent.AFTER_TURN,
                mode="async",
                delivery="durable",
                script="raise SystemExit(99)",
            )
        ),
        hooks_dir=hooks_dir,
    )

    def _boom(**_kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(mgr._outbox, "write_pending", _boom)

    decision = await mgr.fire(HookEvent.AFTER_TURN, {"turn": 1})

    assert decision.blocked is False
    assert mgr._inflight == []


@pytest.mark.asyncio
async def test_durable_hook_writes_to_failed_on_nonzero(hooks_dir: Path) -> None:
    mgr = HookManager(
        file=_file(
            _hook(
                hook_id="d",
                event=HookEvent.AFTER_TURN,
                mode="async",
                delivery="durable",
                script="import sys; sys.exit(3)",
            )
        ),
        hooks_dir=hooks_dir,
    )
    await mgr.fire(HookEvent.AFTER_TURN, {"turn": 1})
    await mgr.drain_turn()
    failed_files = list((hooks_dir / "outbox" / FAILED).glob("*.json"))
    assert len(failed_files) == 1


@pytest.mark.skipif(sys.platform == "win32", reason="detached worker timing is flaky on Windows CI")
@pytest.mark.asyncio
async def test_durable_detached_hook_is_completed_by_worker(hooks_dir: Path) -> None:
    mgr = HookManager(
        file=_file(
            _hook(
                hook_id="detached-durable",
                event=HookEvent.SESSION_END,
                mode="fire_and_forget",
                delivery="durable",
                detach=True,
                script="pass",
            )
        ),
        hooks_dir=hooks_dir,
    )
    await mgr.fire(HookEvent.SESSION_END, {"session_id": "s1"}, scope="session")

    # The detached worker is a separate interpreter process: allow a loaded CI
    # runner plenty of wall clock, and poll BOTH conditions together — _move
    # writes done/ before unlinking pending/, so observing done alone can race
    # the pending cleanup.
    done_root = hooks_dir / "outbox" / DONE
    pending_root = hooks_dir / "outbox" / "pending"
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        if list(done_root.glob("*.json")) and not list(pending_root.glob("*.json")):
            break
        await asyncio.sleep(0.1)
    assert list(done_root.glob("*.json"))
    assert not list(pending_root.glob("*.json"))


@pytest.mark.asyncio
async def test_drain_session_grace_cancels_long_tasks(hooks_dir: Path) -> None:
    mgr = HookManager(
        file=_file(
            _hook(
                hook_id="long",
                event=HookEvent.SESSION_END,
                mode="async",
                timeout=30.0,
                script="import time; time.sleep(30)",
            ),
            shutdown_grace_seconds=0.3,
        ),
        hooks_dir=hooks_dir,
    )
    await mgr.fire(HookEvent.SESSION_END, {}, scope="session")
    # drain_session should return promptly after grace expires; the
    # cancellation path runs without raising.
    await mgr.drain_session()


@pytest.mark.asyncio
async def test_drain_session_without_close_awaits_hooks_but_keeps_manager_open(hooks_dir: Path) -> None:
    """``close=False`` = the same wait as a closing drain, minus the close.

    Used when the live session is deleted underneath the engine: ``session_end``
    must have *finished* before the files go, yet the manager keeps serving
    later events (a failed delete leaves the session running).
    """
    marker = hooks_dir / "session_end.txt"
    script = textwrap.dedent(
        f"""
        import time
        time.sleep(0.2)
        open({str(marker)!r}, "a").write("x")
        """
    )
    mgr = HookManager(
        file=_file(_hook(hook_id="end", event=HookEvent.SESSION_END, mode="async", script=script)),
        hooks_dir=hooks_dir,
    )
    await mgr.fire(HookEvent.SESSION_END, {}, scope="session")
    assert not marker.exists()  # fire() only spawned the async hook

    await mgr.drain_session(close=False)

    assert marker.read_text() == "x"  # drained: the hook ran to completion
    assert mgr._inflight == []
    assert mgr._closed is False
    # Still open: a later fire spawns and a closing drain awaits it as usual.
    await mgr.fire(HookEvent.SESSION_END, {}, scope="session")
    await mgr.drain_session()
    assert marker.read_text() == "xx"
    assert mgr._closed is True
    # Closed: further fires are inert.
    await mgr.fire(HookEvent.SESSION_END, {}, scope="session")
    assert mgr._inflight == []


@pytest.mark.parametrize("close", [True, False])
@pytest.mark.asyncio
@pytest.mark.parametrize("queued", [False, True], ids=["running", "queued"])
@pytest.mark.parametrize(
    ("mode", "scope", "event"),
    [("fire_and_forget", "session", HookEvent.SESSION_START), ("async", "detached", HookEvent.USER_INTERRUPT)],
    ids=["fire-and-forget", "interrupt-observer"],
)
async def test_session_drain_preserves_background_result_while_log_stays_open(
    hooks_dir: Path, *, close: bool, queued: bool, mode: str, scope: str, event: HookEvent
) -> None:
    hook = _hook(
        hook_id="background",
        event=event,
        mode=mode,
        delivery="durable",
    )
    mgr = HookManager(file=_file(hook, max_parallel_hooks=1), hooks_dir=hooks_dir)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def run_and_wait(
        config: HookConfig,
        _payload: dict[str, Any],
        *,
        payload_text: str | None = None,
        result: HookResult | None = None,
    ) -> HookResult:
        entered.set()
        await release.wait()
        return HookResult(hook_id=config.id, exit_code=0)

    mgr._runner.run_and_wait = run_and_wait  # type: ignore[method-assign]
    sink = FakeSink()
    mgr.trajectory_context_provider = lambda: make_context(sink)
    if queued:
        await mgr._sem.acquire()
    await mgr.fire(event, {}, scope=scope)
    task = mgr._inflight[0].task
    try:
        if not queued:
            await asyncio.wait_for(entered.wait(), timeout=5.0)
        started = sink.only(EventType.HOOK_OPERATION_STARTED)
        await asyncio.wait_for(mgr.drain_session(close=close), timeout=5.0)

        assert not task.done()
        assert sink.of_type(EventType.HOOK_OPERATION_FINISHED) == []
        assert list((hooks_dir / "outbox" / "pending").glob("*.json"))
    finally:
        if queued:
            mgr._sem.release()
        release.set()
        result = await asyncio.wait_for(task, timeout=5.0)

    assert result.exit_code == 0
    assert len(list((hooks_dir / "outbox" / DONE).glob("*.json"))) == 1
    assert not list((hooks_dir / "outbox" / "pending").glob("*.json"))
    finished = sink.only(EventType.HOOK_OPERATION_FINISHED)
    assert finished.operation_id == started.operation_id
    assert finished.payload["outcome"] == HookOutcome.SUCCESS
    assert finished.payload["exit_code"] == 0
    sink.assert_operations_settled()
    events_after_completion = list(sink.drafts)
    await mgr.drain_session()
    assert sink.drafts == events_after_completion


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "detach"),
    [("blocking", False), ("async", False), ("fire_and_forget", False), ("fire_and_forget", True)],
    ids=["blocking-start-ack", "async-start-ack", "fire-and-forget-start-ack", "detached-handoff"],
)
async def test_committed_hook_dispatch_is_settled_before_log_close(
    hooks_dir: Path, monkeypatch: pytest.MonkeyPatch, mode: str, detach: bool
) -> None:
    session_dir = hooks_dir / "session"
    session_dir.mkdir()
    trajectory = SessionTrajectory(
        session_id=str(uuid4()),
        session_dir=session_dir,
        config_dir=hooks_dir / "config",
        session_start_info=lambda: SessionStartInfo(str(hooks_dir), "a" * 64, "b" * 64),
    )
    started = asyncio.Event()
    release_ack = asyncio.Event()
    original_emit = trajectory.emit

    async def held_emit(
        draft: EventDraft, *, payload_factory: Callable[[int], Mapping[str, Any]] | None = None
    ) -> EmitResult:
        result = await original_emit(draft, payload_factory=payload_factory)
        if not detach and draft.event_type == EventType.HOOK_OPERATION_STARTED:
            started.set()
            await release_ack.wait()
        return result

    monkeypatch.setattr(trajectory, "emit", held_emit)
    mgr = HookManager(
        file=_file(_hook(hook_id="interrupt", event=HookEvent.USER_INTERRUPT, mode=mode, detach=detach)),
        hooks_dir=hooks_dir,
    )

    async def held_spawn(
        hook: HookConfig, payload: dict[str, Any], *, job: OutboxJob | None = None, payload_text: str | None = None
    ) -> Path:
        started.set()
        await release_ack.wait()
        return Path("hook.log")

    # Model the asynchronous OS handoff without creating an orphan process.
    monkeypatch.setattr(mgr._runner, "spawn_detached", held_spawn)
    mgr.trajectory_context_provider = trajectory.context
    fire = asyncio.create_task(mgr.fire(HookEvent.USER_INTERRUPT, {}, scope="detached"))
    try:
        await asyncio.wait_for(started.wait(), timeout=5.0)
        await mgr.drain_session()
        assert not fire.done()
        await trajectory.close(reason=RuntimeFinishReason.GRACEFUL_SHUTDOWN)

        recorded = assert_trajectory_file_accounted(trajectory_events_path(session_dir))
        assert_trajectory_operation_settlement(recorded.events)
        terminals = [event for event in recorded.events if event.event_type == EventType.HOOK_OPERATION_FINISHED]
        assert len(terminals) == 1
        assert terminals[0].payload["outcome"] == HookOutcome.ABANDONED
    finally:
        # Fence dispatch even when waiting for the barrier failed: releasing
        # its ack must not launch a new background task during test cleanup.
        await mgr.drain_session()
        release_ack.set()
        try:
            await asyncio.wait_for(fire, timeout=5.0)
        finally:
            await mgr.drain_session()
            await trajectory.close(reason=RuntimeFinishReason.GRACEFUL_SHUTDOWN)


@pytest.mark.parametrize("close", [True, False])
@pytest.mark.asyncio
async def test_drain_session_fences_inflight_blocking_hook_dispatch(hooks_dir: Path, *, close: bool) -> None:
    blocking_one = _hook(hook_id="blocking-one", event=HookEvent.USER_PROMPT_SUBMIT)
    blocking_two = _hook(hook_id="blocking-two", event=HookEvent.USER_PROMPT_SUBMIT)
    nonblocking = _hook(hook_id="nonblocking", event=HookEvent.USER_PROMPT_SUBMIT, mode="async")
    mgr = HookManager(file=_file(blocking_one, blocking_two, nonblocking), hooks_dir=hooks_dir)
    entered = asyncio.Event()
    release = asyncio.Event()
    executed: list[str] = []

    async def _run_and_wait(
        hook: HookConfig,
        _payload: dict[str, object],
        *,
        payload_text: str | None = None,
        result: HookResult | None = None,
    ) -> HookResult:
        executed.append(hook.id)
        if hook is blocking_one:
            entered.set()
            await release.wait()
        return HookResult(hook_id=hook.id, exit_code=0)

    mgr._runner.run_and_wait = _run_and_wait  # type: ignore[method-assign]
    sink = FakeSink()
    mgr.trajectory_context_provider = lambda: make_context(sink)
    fire_task = asyncio.create_task(mgr.fire(HookEvent.USER_PROMPT_SUBMIT, {"text": "held open"}))
    try:
        await asyncio.wait_for(entered.wait(), timeout=5.0)
        started = sink.only(EventType.HOOK_OPERATION_STARTED)

        await mgr.drain_session(close=close)

        assert sink.of_type(EventType.HOOK_OPERATION_FINISHED) == []

        release.set()
        decision = await asyncio.wait_for(fire_task, timeout=5.0)

        assert decision.blocked is False
        assert executed == [blocking_one.id]
        finished = sink.only(EventType.HOOK_OPERATION_FINISHED)
        assert finished.operation_id == started.operation_id
        assert finished.payload["outcome"] == HookOutcome.SUCCESS
        assert finished.payload["exit_code"] == 0
        sink.assert_operations_settled()
        assert len(sink.of_type(EventType.HOOK_OPERATION_STARTED)) == 1
        assert len(sink.of_type(EventType.HOOK_OPERATION_FINISHED)) == 1
        assert mgr._inflight == []
    finally:
        release.set()
        if not fire_task.done():
            fire_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await fire_task


@pytest.mark.parametrize("close", [True, False])
@pytest.mark.asyncio
async def test_drain_session_fences_blocking_dispatch_during_start_ack(hooks_dir: Path, *, close: bool) -> None:
    hook = _hook(hook_id="blocking", event=HookEvent.USER_PROMPT_SUBMIT)
    mgr = HookManager(file=_file(hook), hooks_dir=hooks_dir)
    executed: list[str] = []

    async def _run_and_wait(
        config: HookConfig,
        _payload: dict[str, object],
        *,
        payload_text: str | None = None,
        result: HookResult | None = None,
    ) -> HookResult:
        executed.append(config.id)
        return HookResult(hook_id=config.id, exit_code=0)

    mgr._runner.run_and_wait = _run_and_wait  # type: ignore[method-assign]
    sink = _HeldStartAckSink()
    mgr.trajectory_context_provider = lambda: make_context(sink)
    fire_task = asyncio.create_task(mgr.fire(HookEvent.USER_PROMPT_SUBMIT, {"text": "held ack"}))
    try:
        await asyncio.wait_for(sink.started_ack_held.wait(), timeout=5.0)
        started = sink.only(EventType.HOOK_OPERATION_STARTED)

        await mgr.drain_session(close=close)

        assert sink.event_types == [EventType.HOOK_OPERATION_STARTED]
        sink.release_started_ack.set()
        decision = await asyncio.wait_for(fire_task, timeout=5.0)

        finished = sink.only(EventType.HOOK_OPERATION_FINISHED)
        assert finished.operation_id == started.operation_id
        assert finished.payload["outcome"] == HookOutcome.ABANDONED
        assert decision.blocked is False
        assert executed == []
        assert sink.event_types == [EventType.HOOK_OPERATION_STARTED, EventType.HOOK_OPERATION_FINISHED]
        await asyncio.sleep(0)
        assert len(sink.drafts) == 2
        sink.assert_operations_settled()
    finally:
        sink.release_started_ack.set()
        if not fire_task.done():
            fire_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await fire_task


@pytest.mark.parametrize("close", [True, False])
@pytest.mark.asyncio
async def test_drain_session_fences_durable_async_dispatch_during_start_ack(hooks_dir: Path, *, close: bool) -> None:
    hook = _hook(
        hook_id="durable-async",
        event=HookEvent.SESSION_END,
        mode="async",
        delivery="durable",
    )
    file = _file(hook, outbox_retry_age_seconds=0.0)
    mgr = HookManager(file=file, hooks_dir=hooks_dir)
    executed: list[str] = []

    async def _run_and_wait(
        config: HookConfig,
        _payload: dict[str, object],
        *,
        payload_text: str | None = None,
        result: HookResult | None = None,
    ) -> HookResult:
        executed.append(config.id)
        return HookResult(hook_id=config.id, exit_code=0)

    mgr._runner.run_and_wait = _run_and_wait  # type: ignore[method-assign]
    sink = _HeldStartAckSink()
    mgr.trajectory_context_provider = lambda: make_context(sink)
    fire_task = asyncio.create_task(mgr.fire(HookEvent.SESSION_END, {}, scope="session"))
    try:
        await asyncio.wait_for(sink.started_ack_held.wait(), timeout=5.0)
        started = sink.only(EventType.HOOK_OPERATION_STARTED)
        pending_files = list((hooks_dir / "outbox" / "pending").glob("*.json"))
        assert len(pending_files) == 1

        await mgr.drain_session(close=close)

        assert sink.event_types == [EventType.HOOK_OPERATION_STARTED]
        assert mgr._inflight == []
        sink.release_started_ack.set()
        decision = await asyncio.wait_for(fire_task, timeout=5.0)

        finished = sink.only(EventType.HOOK_OPERATION_FINISHED)
        assert finished.operation_id == started.operation_id
        assert finished.payload["outcome"] == HookOutcome.ABANDONED
        assert decision.blocked is False
        assert executed == []
        assert mgr._inflight == []
        assert sink.event_types == [EventType.HOOK_OPERATION_STARTED, EventType.HOOK_OPERATION_FINISHED]
        await asyncio.sleep(0)
        assert len(sink.drafts) == 2
        sink.assert_operations_settled()

        pending_data = json.loads(pending_files[0].read_text(encoding="utf-8"))
        assert pending_data["state"] == "pending"
        assert pending_data["retries"] == 0
        assert pending_data["last_started_at"] == ""
        assert not list((hooks_dir / "outbox" / DONE).glob("*.json"))
        assert not list((hooks_dir / "outbox" / FAILED).glob("*.json"))

        recovery_mgr = HookManager(file=file, hooks_dir=hooks_dir)
        recovered: list[OutboxJob] = []

        async def _capture_spawn(
            _hook_config: HookConfig,
            _payload: dict[str, Any],
            *,
            scope: str,
            session_drain_generation: int,
            existing_job: OutboxJob | None = None,
            target_operation_id: str | None = None,
        ) -> None:
            assert scope == "session"
            assert session_drain_generation == recovery_mgr._session_drain_generation
            assert target_operation_id is None
            assert existing_job is not None
            recovered.append(existing_job)

        recovery_mgr._spawn_nonblocking = _capture_spawn  # type: ignore[method-assign]
        assert await recovery_mgr.recover_outbox() == 1
        assert [job.job_id for job in recovered] == [pending_data["job_id"]]
    finally:
        sink.release_started_ack.set()
        if not fire_task.done():
            fire_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await fire_task


@pytest.mark.asyncio
async def test_detached_scope_async_hook_is_reaped_only_after_completion(hooks_dir: Path) -> None:
    hook = _hook(hook_id="interrupt", event=HookEvent.USER_INTERRUPT, mode="async")
    mgr = HookManager(file=_file(hook), hooks_dir=hooks_dir)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def _run_and_wait(
        config: HookConfig,
        _payload: dict[str, object],
        *,
        payload_text: str | None = None,
        result: HookResult | None = None,
    ) -> HookResult:
        entered.set()
        await release.wait()
        return HookResult(hook_id=config.id, exit_code=0)

    mgr._runner.run_and_wait = _run_and_wait  # type: ignore[method-assign]
    await mgr.fire(HookEvent.USER_INTERRUPT, {}, scope="detached")
    try:
        await asyncio.wait_for(entered.wait(), timeout=5.0)
        assert len(mgr._inflight) == 1
        assert mgr._inflight[0].scope == "detached"
        assert mgr._inflight[0].task.done() is False

        release.set()
        assert await wait_until(lambda: mgr._inflight == [])
    finally:
        release.set()
        if mgr._inflight:
            await asyncio.gather(*(tracked.task for tracked in mgr._inflight), return_exceptions=True)


@pytest.mark.asyncio
async def test_durable_async_cancel_stays_pending_for_retry(hooks_dir: Path) -> None:
    mgr = HookManager(
        file=_file(
            _hook(
                hook_id="long-durable",
                event=HookEvent.SESSION_END,
                mode="async",
                delivery="durable",
                timeout=30.0,
                script="import time; time.sleep(30)",
            ),
            shutdown_grace_seconds=0.2,
        ),
        hooks_dir=hooks_dir,
    )
    await mgr.fire(HookEvent.SESSION_END, {}, scope="session")
    await mgr.drain_session()
    assert list((hooks_dir / "outbox" / "pending").glob("*.json"))
    assert not list((hooks_dir / "outbox" / FAILED).glob("*.json"))


@pytest.mark.asyncio
async def test_recover_outbox_force_fails_over_retry_cap(hooks_dir: Path) -> None:
    # Pre-create a pending job with retries >= cap by hand.
    from chrys.service.hooks.outbox import Outbox

    outbox = Outbox(hooks_dir / "outbox")
    job = outbox.write_pending(hook_id="missing", event="after_turn", payload={})
    job.retries = 99
    outbox._write(job)

    mgr = HookManager(
        file=_file(outbox_max_retries=3, outbox_retry_age_seconds=0.0),
        hooks_dir=hooks_dir,
    )
    n = await mgr.recover_outbox()
    assert n == 0
    failed = list((hooks_dir / "outbox" / FAILED).glob("*.json"))
    assert len(failed) == 1


@pytest.mark.asyncio
async def test_recover_outbox_uses_source_marker_for_duplicate_project_global_ids(hooks_dir: Path) -> None:
    project_hook = _hook(
        hook_id="dup",
        event=HookEvent.AFTER_TURN,
        mode="async",
        delivery="durable",
        argv=["project"],
    )
    global_hook = _hook(
        hook_id="dup",
        event=HookEvent.AFTER_TURN,
        mode="async",
        delivery="durable",
        argv=["global"],
    )
    mgr = HookManager(
        file=MergedHooksFile(
            project=HooksFile(hooks=[project_hook], source="/project/.chrys/hooks/hooks.yaml"),
            global_=HooksFile(hooks=[global_hook], source="/config/hooks/hooks.yaml"),
            hooks=[project_hook, global_hook],
            settings=HookSettings(outbox_retry_age_seconds=0.0),
        ),
        hooks_dir=hooks_dir,
    )
    mgr._outbox.write_pending(
        hook_id="dup",
        event=str(HookEvent.AFTER_TURN),
        payload={"event": str(HookEvent.AFTER_TURN)},
        hook_source="project",
        hook_source_path="/project/.chrys/hooks/hooks.yaml",
    )
    retried: list[str] = []

    async def _capture_spawn(
        hook: HookConfig,
        _payload: dict[str, object],
        *,
        scope: str,
        session_drain_generation: int,
        existing_job: object | None = None,
    ) -> None:
        retried.append(hook.run.argv[0])

    mgr._spawn_nonblocking = _capture_spawn  # type: ignore[method-assign]

    queued = await mgr.recover_outbox()

    assert queued == 1
    assert retried == ["project"]


@pytest.mark.asyncio
async def test_recover_outbox_rejects_project_job_from_different_source_path(hooks_dir: Path) -> None:
    active_source = "/project-b/.chrys/hooks/hooks.yaml"
    stale_source = "/project-a/.chrys/hooks/hooks.yaml"
    project_hook = _hook(
        hook_id="same-id",
        event=HookEvent.AFTER_TURN,
        mode="async",
        delivery="durable",
        argv=["project-b"],
    )
    mgr = HookManager(
        file=MergedHooksFile(
            project=HooksFile(hooks=[project_hook], source=active_source),
            hooks=[project_hook],
            settings=HookSettings(outbox_retry_age_seconds=0.0),
        ),
        hooks_dir=hooks_dir,
    )
    mgr._outbox.write_pending(
        hook_id="same-id",
        event=str(HookEvent.AFTER_TURN),
        payload={"event": str(HookEvent.AFTER_TURN)},
        hook_source="project",
        hook_source_path=stale_source,
    )
    retried: list[str] = []

    async def _capture_spawn(
        hook: HookConfig,
        _payload: dict[str, object],
        *,
        scope: str,
        session_drain_generation: int,
        existing_job: object | None = None,
    ) -> None:
        retried.append(hook.run.argv[0])

    mgr._spawn_nonblocking = _capture_spawn  # type: ignore[method-assign]

    queued = await mgr.recover_outbox()

    assert queued == 0
    assert retried == []
    failed = list((hooks_dir / "outbox" / FAILED).glob("*.json"))
    assert len(failed) == 1
    data = json.loads(failed[0].read_text(encoding="utf-8"))
    assert stale_source in data["error"]


@pytest.mark.asyncio
async def test_recover_outbox_fails_ambiguous_legacy_duplicate_id(hooks_dir: Path) -> None:
    project_hook = _hook(
        hook_id="dup",
        event=HookEvent.AFTER_TURN,
        mode="async",
        delivery="durable",
    )
    global_hook = _hook(
        hook_id="dup",
        event=HookEvent.AFTER_TURN,
        mode="async",
        delivery="durable",
    )
    mgr = HookManager(
        file=MergedHooksFile(
            project=HooksFile(hooks=[project_hook], source="/project/.chrys/hooks/hooks.yaml"),
            global_=HooksFile(hooks=[global_hook], source="/config/hooks/hooks.yaml"),
            hooks=[project_hook, global_hook],
            settings=HookSettings(outbox_retry_age_seconds=0.0),
        ),
        hooks_dir=hooks_dir,
    )
    mgr._outbox.write_pending(
        hook_id="dup",
        event=str(HookEvent.AFTER_TURN),
        payload={"event": str(HookEvent.AFTER_TURN)},
    )

    queued = await mgr.recover_outbox()

    assert queued == 0
    failed = list((hooks_dir / "outbox" / FAILED).glob("*.json"))
    assert len(failed) == 1
    data = json.loads(failed[0].read_text(encoding="utf-8"))
    assert "ambiguous legacy hook id 'dup'" in data["error"]


class _NeverFreeSlot:
    """Stands in for a full ``max_parallel_hooks`` semaphore: the wait never ends."""

    def __init__(self) -> None:
        self.queued = asyncio.Event()

    async def __aenter__(self) -> None:
        self.queued.set()
        await asyncio.Event().wait()

    async def __aexit__(self, *exc_info: object) -> None:
        return None


@pytest.mark.asyncio
async def test_detached_hook_cancelled_at_the_hand_off_still_closes_its_span(hooks_dir: Path) -> None:
    """A detached spawn ends the span at the hand-off; a hand-off the user
    interrupts has no task left to close what the start marker opened."""
    hook = _hook(hook_id="detached", event=HookEvent.AFTER_TURN, mode="fire_and_forget", detach=True)
    mgr = HookManager(file=MergedHooksFile.from_single(_file(hook)), hooks_dir=hooks_dir)

    async def _cancelled(*args: object, **kwargs: object) -> None:
        raise asyncio.CancelledError

    mgr._runner.spawn_detached = _cancelled  # type: ignore[method-assign]
    sink = FakeSink()

    with trajectory_scope(make_context(sink)), pytest.raises(asyncio.CancelledError):
        await mgr._spawn_nonblocking(
            hook,
            {},
            scope="turn",
            session_drain_generation=mgr._session_drain_generation,
        )

    started = sink.only(EventType.HOOK_OPERATION_STARTED)
    assert started.payload["hook_key"] == "detached"
    assert started.payload["scope"] == "turn"
    assert started.payload["detach"] is True
    assert started.payload["delivery"] == "best_effort"
    assert "drain_scope" not in started.payload
    assert sink.only(EventType.HOOK_OPERATION_FINISHED).payload["outcome"] == HookOutcome.CANCELLED


@pytest.mark.asyncio
async def test_async_hook_cancelled_while_queued_still_closes_its_span(hooks_dir: Path) -> None:
    """The span opens before the slot does; an interrupt in the queue must still end it."""
    hook = _hook(hook_id="queued", event=HookEvent.AFTER_TURN, mode="async")
    mgr = HookManager(file=MergedHooksFile.from_single(_file(hook)), hooks_dir=hooks_dir)
    slot = _NeverFreeSlot()
    mgr._sem = slot
    sink = FakeSink()

    with trajectory_scope(make_context(sink)):
        trace = mgr._open_trace(None)
        assert trace is not None
        await trace.started(
            hook_id=hook.id,
            hook_event=str(hook.event),
            execution_mode=hook.execution.mode,
            detach=hook.execution.detach,
            delivery=hook.execution.delivery,
        )
        task = asyncio.create_task(mgr._run_async_tracked(hook, {}, None, trace))
        await slot.queued.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    finished = sink.only(EventType.HOOK_OPERATION_FINISHED)
    assert finished.payload["outcome"] == HookOutcome.CANCELLED
