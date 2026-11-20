# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The run log a :class:`HookManager` writes: one record per run, how it ended and what iCode applied.

Every hook is a real ``python -c`` subprocess; a held hook polls for a release
file the test creates.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
import textwrap
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from chrys.foundation.models.hook_runs import HookRunApplied, HookRunDetail, HookRunRecord
from chrys.foundation.platform.output_capture import BoundedCapture
from chrys.foundation.trajectory.context import trajectory_scope
from chrys.foundation.trajectory.event_types import EventType
from chrys.service.hooks import manager as manager_module
from chrys.service.hooks import runner as runner_module
from chrys.service.hooks.events import HookEvent
from chrys.service.hooks.manager import HookManager
from chrys.service.hooks.outbox import DONE, FAILED, Outbox
from chrys.service.hooks.run_log import UNWRITABLE_JSON, HookRunLog
from chrys.service.hooks.runner import HookResult, HookRunner
from chrys.service.hooks.schema import (
    HookConfig,
    HookExecution,
    HookMatch,
    HookRun,
    HookSettings,
    HooksFile,
    MergedHooksFile,
)
from chrys.service.trajectory.hooks import HookOutcome
from tests.service.trajectory._fakes import FakeSink, make_context
from tests.support.waiting import ENGINE_TURN_TIMEOUT, wait_for

_SESSION = "s1"


def _result(decision: dict[str, object], *, exit_code: int = 0) -> str:
    return textwrap.dedent(
        f"""
        import json, os, sys
        with open(os.environ["CHRYS_HOOK_RESULT"], "w") as f:
            json.dump({decision!r}, f)
        sys.exit({exit_code})
        """
    )


def _nested_override(depth: int) -> str:
    """A hook overriding ``x`` with *depth* nested lists, deeper than a Python literal may nest."""
    return textwrap.dedent(
        f"""
        import os
        with open(os.environ["CHRYS_HOOK_RESULT"], "w") as f:
            f.write('{{"action": "modify", "args_override": {{"x": ' + "[" * {depth} + "]" * {depth} + "}}}}")
        """
    )


def _deep(depth: int) -> dict[str, object]:
    """Objects nested *depth* deep, deeper than any stack lets JSON write or Python compare them."""
    top: dict[str, object] = {}
    node = top
    for _ in range(depth):
        child: dict[str, object] = {}
        node["a"] = child
        node = child
    return top


def _held(release: Path) -> str:
    return textwrap.dedent(
        f"""
        import os, time
        while not os.path.exists({str(release)!r}):
            time.sleep(0.01)
        """
    )


def _hook(
    hook_id: str,
    event: HookEvent,
    *,
    mode: str = "blocking",
    script: str = "pass",
    argv: list[str] | None = None,
    timeout: float = 30.0,
    on_error: str = "warn",
    tool_name: str | None = None,
    detach: bool = False,
    delivery: str = "best_effort",
) -> HookConfig:
    return HookConfig(
        id=hook_id,
        event=event,
        run=HookRun(type="command", argv=argv or [sys.executable, "-c", script]),
        execution=HookExecution(
            mode=mode, timeout_seconds=timeout, on_error=on_error, detach=detach, delivery=delivery
        ),
        match=HookMatch(tool_name=tool_name),
    )


class _Logged:
    """A manager recording into a run log, with every change callback counted."""

    def __init__(self, manager: HookManager, log: HookRunLog) -> None:
        self.manager = manager
        self.log = log
        self.changes: list[tuple[HookRunRecord, ...]] = []

    async def on_change(self) -> None:
        self.changes.append(self.log.records())

    def only(self, hook_id: str) -> HookRunRecord:
        matches = [record for record in self.log.records() if record.hook_id == hook_id]
        assert len(matches) == 1, matches
        return matches[0]

    def detail(self, hook_id: str) -> HookRunDetail:
        detail = self.log.detail(self.only(hook_id).run_id)
        assert detail is not None
        return detail


def _logged(
    hooks_dir: Path, *hooks: HookConfig, log: HookRunLog | None = None, session_id: str = _SESSION, **settings: float
) -> _Logged:
    manager = HookManager(file=HooksFile(settings=HookSettings(**settings), hooks=list(hooks)), hooks_dir=hooks_dir)
    logged = _Logged(manager, log if log is not None else HookRunLog())
    manager.record_runs(logged.log, session_id=session_id, on_change=logged.on_change)
    return logged


def _tool(name: str = "zsh", args: dict[str, object] | None = None) -> dict[str, object]:
    return {"profile": "Code", "tool": {"name": name, "kind": "shell", "call_id": "call-7", "args": args or {}}}


@pytest.fixture
def hooks_dir(tmp_path: Path) -> Path:
    path = tmp_path / "hooks"
    path.mkdir()
    return path


@pytest.fixture
def fed(monkeypatch: pytest.MonkeyPatch) -> list[bytes]:
    """Every chunk the runner reads from a hook's stdout and stderr, as it reads them."""
    chunks: list[bytes] = []

    class _Seen(BoundedCapture):
        def feed(self, chunk: bytes) -> None:
            chunks.append(chunk)
            super().feed(chunk)

    monkeypatch.setattr(runner_module, "BoundedCapture", _Seen)
    return chunks


@pytest.fixture
async def release(tmp_path: Path) -> AsyncIterator[Path]:
    path = tmp_path / "release"
    yield path
    path.touch()


# --- How a run ended and what was applied (sections 3, 5.1) ---------------------


async def test_a_successful_run_with_no_decision_is_recorded_ended_with_no_effect(hooks_dir: Path) -> None:
    logged = _logged(hooks_dir, _hook("quiet", HookEvent.BEFORE_TOOL_CALL, script="print('hi')"))
    before = time.monotonic()
    await logged.manager.fire(HookEvent.BEFORE_TOOL_CALL, _tool("zsh"))
    record = logged.only("quiet")
    assert record.state == "ended"
    assert record.outcome == HookOutcome.SUCCESS
    assert record.effect is None
    assert record.exit_code == 0
    assert (record.session_id, record.hook_event, record.execution_mode) == (_SESSION, "before_tool_call", "blocking")
    assert (record.tool_name, record.sub_agent, record.retry, record.detach) == ("zsh", "", False, False)
    assert record.started_at is not None and record.ended_at is not None
    assert before <= record.registered_at <= record.started_at <= record.ended_at <= time.monotonic()
    detail = logged.detail("quiet")
    assert detail.tool_call_id == "call-7"
    assert detail.io is not None and detail.applied is not None
    assert detail.io.result_file == "missing"
    assert "hi" in detail.io.stdout
    assert detail.applied.not_applied is None
    assert (detail.applied.block_reason, detail.applied.args) == ("", "")
    assert logged.changes, "every write is followed by the change callback"
    assert logged.changes[-1] == logged.log.records()


async def test_an_empty_result_object_is_kept_as_written(hooks_dir: Path) -> None:
    logged = _logged(hooks_dir, _hook("nothing-to-say", HookEvent.BEFORE_TOOL_CALL, script=_result({})))
    await logged.manager.fire(HookEvent.BEFORE_TOOL_CALL, _tool())
    detail = logged.detail("nothing-to-say")
    assert detail.io is not None
    assert (detail.io.result_file, detail.io.decision) == ("ok", "{}")


async def test_a_block_is_recorded_as_success_with_the_blocked_effect(hooks_dir: Path) -> None:
    decision = {"action": "block", "reason": "rm denied", "extra_context": "ignored"}
    logged = _logged(hooks_dir, _hook("guard", HookEvent.BEFORE_TOOL_CALL, script=_result(decision)))
    result = await logged.manager.fire(HookEvent.BEFORE_TOOL_CALL, _tool())
    assert result.blocked is True
    record = logged.only("guard")
    assert (record.outcome, record.effect, record.exit_code) == (HookOutcome.SUCCESS, "blocked", 0)
    detail = logged.detail("guard")
    assert detail.io is not None and detail.applied is not None
    assert detail.io.result_file == "ok"
    assert json.loads(detail.io.decision) == decision
    assert "rm denied" in detail.applied.block_reason
    # A block returns at once: nothing else is merged.
    assert detail.applied.extra_context == ""


async def test_modified_args_records_the_merged_arguments(hooks_dir: Path) -> None:
    decision = {"action": "modify", "args_override": {"path": "/safe"}}
    logged = _logged(hooks_dir, _hook("rewrite", HookEvent.BEFORE_TOOL_CALL, script=_result(decision)))
    result = await logged.manager.fire(HookEvent.BEFORE_TOOL_CALL, _tool(args={"path": "/etc/passwd", "mode": "r"}))
    assert result.args_override == {"path": "/safe"}
    record = logged.only("rewrite")
    assert (record.outcome, record.effect) == (HookOutcome.SUCCESS, "modified_args")
    applied = logged.detail("rewrite").applied
    assert applied is not None
    assert json.loads(applied.args) == {"path": "/safe", "mode": "r"}


async def test_returning_the_same_arguments_is_not_a_modification(hooks_dir: Path) -> None:
    """Section 3: compared by value before and after the merge, not by whether an override was returned."""
    same = {"action": "modify", "args_override": {"path": "/a"}}
    logged = _logged(hooks_dir, _hook("echo-args", HookEvent.BEFORE_TOOL_CALL, script=_result(same)))
    await logged.manager.fire(HookEvent.BEFORE_TOOL_CALL, _tool(args={"path": "/a"}))
    assert logged.only("echo-args").effect is None


async def test_a_later_hook_returning_the_already_merged_arguments_did_not_modify_them(hooks_dir: Path) -> None:
    first = {"action": "modify", "args_override": {"path": "/b"}}
    logged = _logged(
        hooks_dir,
        _hook("first", HookEvent.BEFORE_TOOL_CALL, script=_result(first)),
        _hook("second", HookEvent.BEFORE_TOOL_CALL, script=_result(first)),
    )
    await logged.manager.fire(HookEvent.BEFORE_TOOL_CALL, _tool(args={"path": "/a"}))
    assert logged.only("first").effect == "modified_args"
    assert logged.only("second").effect is None


async def test_an_argument_turned_from_1_to_true_was_modified(hooks_dir: Path) -> None:
    """Python finds ``{"n": 1}`` and ``{"n": True}`` equal; the tool gets ``true`` all the same."""
    logged = _logged(
        hooks_dir,
        _hook("first", HookEvent.BEFORE_TOOL_CALL, script=_result({"action": "modify", "args_override": {"n": 1}})),
        _hook("second", HookEvent.BEFORE_TOOL_CALL, script=_result({"action": "modify", "args_override": {"n": True}})),
    )
    result = await logged.manager.fire(HookEvent.BEFORE_TOOL_CALL, _tool(args={"n": 0}))
    assert result.args_override is not None and result.args_override["n"] is True
    assert logged.only("second").effect == "modified_args"
    applied = logged.detail("second").applied
    assert applied is not None
    assert json.loads(applied.args) == {"n": True}


async def test_arguments_nested_past_the_recursion_limit_still_end_every_run(hooks_dir: Path) -> None:
    plain = {"action": "modify", "args_override": {"y": 1}}
    logged = _logged(
        hooks_dir,
        _hook("deep", HookEvent.BEFORE_TOOL_CALL, script=_nested_override(3_000)),
        _hook("plain", HookEvent.BEFORE_TOOL_CALL, script=_result(plain)),
    )
    result = await logged.manager.fire(HookEvent.BEFORE_TOOL_CALL, _tool(args={"a": 1}))
    assert result.args_override is not None and result.args_override["y"] == 1
    records = [(record.hook_id, record.state, record.effect) for record in logged.log.records()]
    assert records == [("deep", "ended", "modified_args"), ("plain", "ended", "modified_args")]


async def test_a_result_nested_deeper_than_json_writes_still_ends_every_run(hooks_dir: Path) -> None:
    """The decoder takes objects nested deeper than the encoder writes: recording them must not fail the call."""
    deep = _deep(100_000)
    logged = _logged(hooks_dir, _hook("deep", HookEvent.BEFORE_TOOL_CALL), _hook("after", HookEvent.BEFORE_TOOL_CALL))

    # Handed over as parsed, without a subprocess: how deep a decoder goes depends on the stack.
    async def run_and_wait(
        hook: HookConfig, _payload: dict[str, Any], *, payload_text: str | None = None, result: HookResult | None = None
    ) -> HookResult:
        if hook.id != "deep":
            return HookResult(hook_id=hook.id, exit_code=0)
        override = {"action": "modify", "args_override": {"x": deep}}
        return HookResult(hook_id=hook.id, exit_code=0, decision=override, result_file="ok")

    logged.manager._runner.run_and_wait = run_and_wait  # type: ignore[method-assign]
    result = await logged.manager.fire(HookEvent.BEFORE_TOOL_CALL, _tool(args={"a": 1}))
    assert result.args_override is not None and result.args_override["x"] is deep
    records = [(record.hook_id, record.state, record.effect) for record in logged.log.records()]
    assert records == [("deep", "ended", "modified_args"), ("after", "ended", None)]
    detail = logged.detail("deep")
    assert detail.io is not None and detail.applied is not None
    assert (detail.io.decision, detail.applied.args) == (UNWRITABLE_JSON, UNWRITABLE_JSON)


async def test_overrides_too_deep_to_compare_still_report_every_end(hooks_dir: Path) -> None:
    first, second = _deep(100_000), _deep(100_000)
    overrides = {"first": first, "second": second}
    logged = _logged(hooks_dir, _hook("first", HookEvent.BEFORE_TOOL_CALL), _hook("second", HookEvent.BEFORE_TOOL_CALL))

    async def run_and_wait(
        hook: HookConfig, _payload: dict[str, Any], *, payload_text: str | None = None, result: HookResult | None = None
    ) -> HookResult:
        decision = {"action": "modify", "args_override": {"x": overrides[hook.id]}}
        return HookResult(hook_id=hook.id, exit_code=0, decision=decision, result_file="ok")

    logged.manager._runner.run_and_wait = run_and_wait  # type: ignore[method-assign]
    result = await logged.manager.fire(HookEvent.BEFORE_TOOL_CALL, _tool(args={"a": 1}))
    assert result.args_override is not None and result.args_override["x"] is second
    assert [(record.hook_id, record.state) for record in logged.log.records()] == [
        ("first", "ended"),
        ("second", "ended"),
    ]
    # Both ends reported: the indicator and the list show nothing running.
    assert logged.manager.executions() == ()
    assert logged.changes[-1] == logged.log.records()


@pytest.mark.parametrize("mode", ["blocking", "async"])
async def test_only_a_string_names_an_action(hooks_dir: Path, mode: str) -> None:
    """Any other value names none, though one nested too deep to write as text."""
    logged = _logged(hooks_dir, _hook("odd", HookEvent.AFTER_TURN, mode=mode))

    async def run_and_wait(
        hook: HookConfig, _payload: dict[str, Any], *, payload_text: str | None = None, result: HookResult | None = None
    ) -> HookResult:
        return HookResult(hook_id=hook.id, exit_code=0, decision={"action": _deep(100_000)}, result_file="ok")

    logged.manager._runner.run_and_wait = run_and_wait  # type: ignore[method-assign]
    decision = await logged.manager.fire(HookEvent.AFTER_TURN, {"turn": 1})
    await asyncio.wait_for(logged.manager.drain_turn(), ENGINE_TURN_TIMEOUT)
    assert (decision.blocked, decision.args_override) == (False, None)
    record = logged.only("odd")
    assert (record.state, record.outcome, record.effect) == ("ended", HookOutcome.SUCCESS, None)
    assert logged.detail("odd").applied == HookRunApplied()
    assert logged.manager.executions() == ()


async def test_a_result_that_cannot_be_taken_still_ends_and_reports_its_run(
    hooks_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The error goes on to the caller, as it always did, but the run is not left running."""
    logged = _logged(hooks_dir, _hook("odd", HookEvent.BEFORE_TOOL_CALL, script=_result({"action": "block"})))

    # As a reason nested too deep to write as text raises, where the stack is too small to write it.
    def unwritable(*_args: object, **_kwargs: object) -> None:
        raise RecursionError("maximum recursion depth exceeded")

    monkeypatch.setattr(manager_module, "_merge_blocking_result", unwritable)
    sink = FakeSink()
    with trajectory_scope(make_context(sink)), pytest.raises(RecursionError):
        await logged.manager.fire(HookEvent.BEFORE_TOOL_CALL, _tool())
    record = logged.only("odd")
    assert (record.state, record.outcome) == ("ended", HookOutcome.SUCCESS)
    assert logged.manager.executions() == ()
    assert logged.changes[-1] == logged.log.records()
    finished = [draft.payload["outcome"] for draft in sink.of_type(EventType.HOOK_OPERATION_FINISHED)]
    assert finished == [HookOutcome.SUCCESS]


async def test_a_deeply_nested_result_is_recorded_compact(hooks_dir: Path) -> None:
    """Indented, 3,000 levels would take some 18 million characters: the depth on every line."""
    logged = _logged(hooks_dir, _hook("deep", HookEvent.BEFORE_TOOL_CALL, script=_nested_override(3_000)))
    await logged.manager.fire(HookEvent.BEFORE_TOOL_CALL, _tool(args={"a": 1}))
    nested = "[" * 3_000 + "]" * 3_000
    detail = logged.detail("deep")
    assert detail.io is not None and detail.applied is not None
    assert detail.io.decision == f'{{"action": "modify", "args_override": {{"x": {nested}}}}}'
    assert detail.applied.args == f'{{"a": 1, "x": {nested}}}'


async def test_an_added_reminder_is_recorded(hooks_dir: Path) -> None:
    decision = {"system_reminder": "remember the style guide"}
    logged = _logged(hooks_dir, _hook("remind", HookEvent.USER_PROMPT_SUBMIT, script=_result(decision)))
    result = await logged.manager.fire(HookEvent.USER_PROMPT_SUBMIT, {"prompt": "hello"})
    assert result.system_reminders == ["remember the style guide"]
    assert logged.only("remind").effect == "added_reminder"
    applied = logged.detail("remind").applied
    assert applied is not None and applied.reminder == "remember the style guide"


@pytest.mark.parametrize("event", [HookEvent.AFTER_TOOL_CALL, HookEvent.TOOL_ERROR])
async def test_added_context_is_recorded(hooks_dir: Path, event: HookEvent) -> None:
    decision = {"extra_context": "ruff found 2 issues"}
    logged = _logged(hooks_dir, _hook("ruff", event, script=_result(decision)))
    result = await logged.manager.fire(event, _tool("write_file"))
    assert result.extra_context == ["ruff found 2 issues"]
    record = logged.only("ruff")
    assert (record.outcome, record.effect, record.tool_name) == (HookOutcome.SUCCESS, "added_context", "write_file")
    applied = logged.detail("ruff").applied
    assert applied is not None and applied.extra_context == "ruff found 2 issues"


async def test_consecutive_hooks_that_withhold_the_result_each_get_an_audit_entry(hooks_dir: Path) -> None:
    event = HookEvent.AFTER_TOOL_CALL
    logged = _logged(
        hooks_dir,
        _hook("first", event, script=_result({"result_override": ""})),
        _hook("second", event, script=_result({"result_override": "", "extra_context": "checked"})),
        _hook("third", event),
    )
    decision = await logged.manager.fire(event, _tool())
    assert decision.result_override == ""
    assert [logged.only(name).effect for name in ("first", "second", "third")] == [
        "replaced_result",
        "replaced_result",
        None,
    ]
    assert logged.detail("second").applied == HookRunApplied(result_override="", extra_context="checked")


async def test_a_block_on_an_event_that_cannot_block_is_not_recorded_as_blocked(hooks_dir: Path) -> None:
    logged = _logged(
        hooks_dir, _hook("late-block", HookEvent.AFTER_TURN, script=_result({"action": "block", "reason": "x"}))
    )
    result = await logged.manager.fire(HookEvent.AFTER_TURN, {"turn": 1})
    assert result.blocked is False
    record = logged.only("late-block")
    assert (record.outcome, record.effect) == (HookOutcome.SUCCESS, None)
    applied = logged.detail("late-block").applied
    assert applied is not None and applied.not_applied == "unsupported"


@pytest.mark.parametrize(
    ("mode", "event", "decision"),
    [
        ("async", HookEvent.BEFORE_TOOL_CALL, {"action": "block", "reason": "too late"}),
        ("fire_and_forget", HookEvent.AFTER_TOOL_CALL, {"extra_context": "never read"}),
    ],
    ids=["async-block", "fire-and-forget-context"],
)
async def test_a_nonblocking_runs_decision_is_never_applied(
    hooks_dir: Path, mode: str, event: HookEvent, decision: dict[str, object]
) -> None:
    logged = _logged(hooks_dir, _hook("observer", event, mode=mode, script=_result(decision)))
    assert logged.manager.has_hooks_for(event)
    assert not logged.manager.has_hooks_for(event, blocking_only=True)
    result = await logged.manager.fire(event, _tool())
    assert result.blocked is False
    await wait_for(
        lambda: any(r.hook_id == "observer" and r.state == "ended" for r in logged.log.records()),
        timeout=ENGINE_TURN_TIMEOUT,
        description="the nonblocking run ended",
    )
    record = logged.only("observer")
    assert (record.outcome, record.effect, record.execution_mode) == (HookOutcome.SUCCESS, None, mode)
    applied = logged.detail("observer").applied
    assert applied is not None and applied.not_applied == "nonblocking"
    await logged.manager.drain_turn()


async def test_a_failed_run_is_recorded_with_its_exit_code_and_its_result_file_ignored(hooks_dir: Path) -> None:
    script = _result({"action": "block", "reason": "ignored"}, exit_code=1)
    logged = _logged(hooks_dir, _hook("fails", HookEvent.BEFORE_TOOL_CALL, script=script))
    result = await logged.manager.fire(HookEvent.BEFORE_TOOL_CALL, _tool())
    assert result.blocked is False
    record = logged.only("fails")
    assert (record.outcome, record.exit_code, record.effect) == (HookOutcome.FAILED, 1, None)
    applied = logged.detail("fails").applied
    assert applied is not None and applied.not_applied == "failed"


async def test_a_failure_that_blocks_by_on_error_is_failed_and_blocked(hooks_dir: Path) -> None:
    logged = _logged(
        hooks_dir,
        _hook("gate", HookEvent.BEFORE_TOOL_CALL, script="import sys; sys.exit(2)", on_error="block"),
    )
    result = await logged.manager.fire(HookEvent.BEFORE_TOOL_CALL, _tool())
    assert result.blocked is True
    record = logged.only("gate")
    assert (record.outcome, record.exit_code, record.effect) == (HookOutcome.FAILED, 2, "blocked")
    applied = logged.detail("gate").applied
    assert applied is not None and applied.not_applied == "failed_blocked"


@pytest.mark.parametrize(("on_error", "effect"), [("warn", None), ("block", "blocked")])
async def test_a_timed_out_run_is_recorded_with_its_partial_output(
    hooks_dir: Path, on_error: str, effect: str | None
) -> None:
    script = "import sys, time\nprint('half done', flush=True)\ntime.sleep(30)\n"
    logged = _logged(
        hooks_dir, _hook("slow", HookEvent.BEFORE_TOOL_CALL, script=script, timeout=1.0, on_error=on_error)
    )
    await logged.manager.fire(HookEvent.BEFORE_TOOL_CALL, _tool())
    record = logged.only("slow")
    assert (record.outcome, record.effect) == (HookOutcome.TIMED_OUT, effect)
    io = logged.detail("slow").io
    assert io is not None and "half done" in io.stdout


async def test_a_run_that_could_not_start_is_recorded_as_a_launch_error(hooks_dir: Path) -> None:
    logged = _logged(
        hooks_dir, _hook("gone", HookEvent.BEFORE_TOOL_CALL, argv=["/definitely/not/a/real/chrys-hook-command"])
    )
    await logged.manager.fire(HookEvent.BEFORE_TOOL_CALL, _tool())
    record = logged.only("gone")
    assert (record.outcome, record.state, record.effect) == (HookOutcome.LAUNCH_ERROR, "ended", None)


async def test_a_cancelled_running_run_is_recorded_cancelled_and_reported(hooks_dir: Path, release: Path) -> None:
    logged = _logged(hooks_dir, _hook("held", HookEvent.BEFORE_TOOL_CALL, script=_held(release)))
    fire = asyncio.create_task(logged.manager.fire(HookEvent.BEFORE_TOOL_CALL, _tool()))
    await wait_for(
        lambda: any(r.state == "running" for r in logged.log.records()) or fire.done(),
        timeout=ENGINE_TURN_TIMEOUT,
        description="the run running",
    )
    assert not fire.done()
    fire.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await fire
    record = logged.only("held")
    assert (record.state, record.outcome) == ("ended", HookOutcome.CANCELLED)
    assert record.started_at is not None and record.ended_at is not None
    # Reported by the time the cancelled task finished.
    assert logged.changes[-1] == logged.log.records()


@pytest.mark.parametrize("mode", ["blocking", "async"])
async def test_a_cancelled_run_keeps_what_it_printed(
    hooks_dir: Path, release: Path, fed: list[bytes], mode: str
) -> None:
    """Opened to see why a hook stalled, its details show what it printed until then."""
    script = "import sys\nprint('contacting server', flush=True)\nprint('slow', file=sys.stderr, flush=True)\n"
    event = HookEvent.BEFORE_TOOL_CALL if mode == "blocking" else HookEvent.SESSION_END
    logged = _logged(
        hooks_dir, _hook("stalls", event, mode=mode, script=script + _held(release)), shutdown_grace_seconds=0
    )

    def printed() -> bool:
        read = b"".join(fed)
        return b"contacting server" in read and b"slow" in read

    if mode == "blocking":
        fire = asyncio.create_task(logged.manager.fire(event, _tool()))
        await wait_for(lambda: printed() or fire.done(), timeout=ENGINE_TURN_TIMEOUT, description="output read")
        assert not fire.done()
        fire.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await fire
    else:
        await logged.manager.fire(event, {}, scope="session")
        await wait_for(printed, timeout=ENGINE_TURN_TIMEOUT, description="output read")
        await asyncio.wait_for(logged.manager.drain_session(close=False), ENGINE_TURN_TIMEOUT)
    record = logged.only("stalls")
    assert (record.state, record.outcome) == ("ended", HookOutcome.CANCELLED)
    io = logged.detail("stalls").io
    assert io is not None
    assert "contacting server" in io.stdout
    assert "slow" in io.stderr
    await logged.manager.drain_session()


@pytest.mark.parametrize("runs", [1, 2])
async def test_runs_a_zero_grace_drain_cancels_before_their_first_step_are_recorded_cancelled(
    hooks_dir: Path, release: Path, runs: int
) -> None:
    """With no grace the drain cancels a run's task before its body runs: the drain ends its record."""
    hooks = [_hook(f"end-{i}", HookEvent.SESSION_END, mode="async", script=_held(release)) for i in range(runs)]
    logged = _logged(hooks_dir, *hooks, shutdown_grace_seconds=0)
    await logged.manager.fire(HookEvent.SESSION_END, {}, scope="session")
    assert {record.state for record in logged.log.records()} == {"queued"}
    # No await in between: the drain gets there before the runs' tasks take a step.
    await asyncio.wait_for(logged.manager.drain_session(close=False), ENGINE_TURN_TIMEOUT)
    records = logged.log.records()
    assert len(records) == runs
    assert {(record.state, record.outcome, record.started_at) for record in records} == {
        ("ended", HookOutcome.CANCELLED, None)
    }
    # Reported once the drain ended them.
    assert logged.changes[-1] == records
    await logged.manager.drain_session()


@pytest.mark.skipif(sys.platform == "win32", reason="detached worker timing is flaky on Windows CI")
async def test_a_detached_run_is_recorded_as_detached_with_its_log(hooks_dir: Path) -> None:
    hook = _hook("handed-off", HookEvent.SESSION_END, mode="fire_and_forget", detach=True, script="print('x')")
    logged = _logged(hooks_dir, hook)
    await logged.manager.fire(HookEvent.SESSION_END, {"session_id": _SESSION}, scope="session")
    record = logged.only("handed-off")
    assert (record.state, record.outcome, record.detach) == ("ended", HookOutcome.DETACHED, True)
    detail = logged.detail("handed-off")
    assert detail.log_path
    assert Path(detail.log_path).parent == hooks_dir / "logs" / _SESSION
    assert logged.changes


async def test_a_failed_detached_hand_off_is_recorded_as_spawn_failed(
    hooks_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def refuse(*_args: object, **_kwargs: object) -> Path:
        raise OSError("no more processes")

    monkeypatch.setattr(HookRunner, "spawn_detached", refuse)
    hook = _hook("handed-off", HookEvent.SESSION_END, mode="fire_and_forget", detach=True)
    logged = _logged(hooks_dir, hook)
    await logged.manager.fire(HookEvent.SESSION_END, {"session_id": _SESSION}, scope="session")
    record = logged.only("handed-off")
    assert (record.state, record.outcome) == ("ended", HookOutcome.SPAWN_FAILED)


# --- The trajectory join (5.1) --------------------------------------------------


async def test_run_ids_are_the_trajectory_operation_ids_and_outcomes_match(hooks_dir: Path) -> None:
    logged = _logged(
        hooks_dir,
        _hook("ok", HookEvent.BEFORE_TOOL_CALL, script="pass", tool_name="a"),
        _hook("fails", HookEvent.BEFORE_TOOL_CALL, script="import sys; sys.exit(4)", tool_name="b"),
        _hook("gone", HookEvent.BEFORE_TOOL_CALL, argv=["/definitely/not/a/real/chrys-hook"], tool_name="c"),
        _hook("slow", HookEvent.BEFORE_TOOL_CALL, script="import time; time.sleep(30)", timeout=0.5, tool_name="d"),
        _hook("guard", HookEvent.BEFORE_TOOL_CALL, script=_result({"action": "block", "reason": "no"}), tool_name="e"),
    )
    sink = FakeSink()
    with trajectory_scope(make_context(sink)):
        for name in "abcde":
            await logged.manager.fire(HookEvent.BEFORE_TOOL_CALL, _tool(name))
    finished = {draft.operation_id: draft.payload for draft in sink.of_type(EventType.HOOK_OPERATION_FINISHED)}
    started = {draft.operation_id: draft.payload for draft in sink.of_type(EventType.HOOK_OPERATION_STARTED)}
    records = {record.hook_id: record for record in logged.log.records()}
    assert set(records) == {"ok", "fails", "gone", "slow", "guard"}
    for record in records.values():
        assert record.run_id in finished, record
        assert started[record.run_id]["hook_key"] == record.hook_id
        trajectory_outcome = finished[record.run_id]["outcome"]
        if record.effect == "blocked" and record.outcome == HookOutcome.SUCCESS:
            assert trajectory_outcome == HookOutcome.BLOCKED
        else:
            assert trajectory_outcome == record.outcome, record
    assert records["guard"].outcome == HookOutcome.SUCCESS
    assert records["guard"].effect == "blocked"


async def test_an_outbox_job_whose_payload_is_no_object_fails_and_the_jobs_after_it_still_run(
    hooks_dir: Path,
) -> None:
    """As unrecorded: a job edited by hand, or written by another version, is read back as it is."""
    outbox = Outbox(hooks_dir / "outbox")
    odd = outbox.write_pending(hook_id="durable", event="after_turn", payload={})
    odd.payload = []  # type: ignore[assignment]
    outbox._write(odd)
    outbox.write_pending(hook_id="durable", event="after_turn", payload={"turn": 1})
    hook = _hook("durable", HookEvent.AFTER_TURN, mode="async", delivery="durable")
    logged = _logged(hooks_dir, hook, outbox_retry_age_seconds=0.0)
    assert await logged.manager.recover_outbox() == 2
    await asyncio.wait_for(logged.manager.drain_session(close=False), ENGINE_TURN_TIMEOUT)
    assert [record.state for record in logged.log.records()] == ["ended", "ended"]
    assert logged.manager.executions() == ()
    assert len(list((hooks_dir / "outbox" / FAILED).glob("*.json"))) == 1
    assert len(list((hooks_dir / "outbox" / DONE).glob("*.json"))) == 1
    await logged.manager.drain_session()


async def test_runs_are_recorded_without_a_trajectory(hooks_dir: Path) -> None:
    logged = _logged(hooks_dir, _hook("a", HookEvent.AFTER_TURN), _hook("b", HookEvent.AFTER_TURN))
    await logged.manager.fire(HookEvent.AFTER_TURN, {"turn": 1})
    records = logged.log.records()
    assert {record.hook_id for record in records} == {"a", "b"}
    assert len({record.run_id for record in records}) == 2
    assert all(record.run_id for record in records)


# --- Configured fields of the detail (section 4) --------------------------------


async def test_the_detail_carries_the_configured_run_but_only_env_names(hooks_dir: Path) -> None:
    hook = HookConfig(
        id="configured",
        event=HookEvent.BEFORE_TOOL_CALL,
        run=HookRun(
            type="command",
            argv=[sys.executable, "-c", "pass"],
            env={"API_TOKEN": "sk-very-secret-value", "MODE": "strict-mode-value"},
            cwd=str(hooks_dir),
        ),
        execution=HookExecution(mode="blocking", timeout_seconds=12.5, on_error="block"),
    )
    logged = _logged(hooks_dir, hook)
    await logged.manager.fire(HookEvent.BEFORE_TOOL_CALL, _tool())
    detail = logged.detail("configured")
    assert detail.run_type == "command"
    assert detail.run_argv == (sys.executable, "-c", "pass")
    assert detail.run_cwd == str(hooks_dir)
    assert set(detail.run_env_names) == {"API_TOKEN", "MODE"}
    assert (detail.timeout_seconds, detail.on_error, detail.delivery) == (12.5, "block", "best_effort")
    assert "sk-very-secret-value" not in repr(detail)
    assert "strict-mode-value" not in repr(detail)


async def test_the_detail_names_the_source_of_each_hook(hooks_dir: Path) -> None:
    workdir_hook = _hook("from-workdir", HookEvent.AFTER_TURN)
    global_hook = _hook("from-global", HookEvent.AFTER_TURN)
    manager = HookManager(
        file=MergedHooksFile(
            workdir=HooksFile(hooks=[workdir_hook], source="/workdir/.chrys/hooks/hooks.yaml"),
            global_=HooksFile(hooks=[global_hook], source="/config/hooks/hooks.yaml"),
            hooks=[workdir_hook, global_hook],
        ),
        hooks_dir=hooks_dir,
    )
    log = HookRunLog()
    manager.record_runs(log, session_id=_SESSION)
    await manager.fire(HookEvent.AFTER_TURN, {"turn": 1})
    by_id = {record.hook_id: log.detail(record.run_id) for record in log.records()}
    workdir, global_ = by_id["from-workdir"], by_id["from-global"]
    assert workdir is not None and global_ is not None
    assert (workdir.source, workdir.source_path) == ("workdir", "/workdir/.chrys/hooks/hooks.yaml")
    assert (global_.source, global_.source_path) == ("global", "/config/hooks/hooks.yaml")


# --- Callbacks only observe (5.2) -----------------------------------------------


async def test_a_raising_change_callback_changes_neither_the_decision_nor_the_trajectory(hooks_dir: Path) -> None:
    script = _result({"action": "modify", "args_override": {"cmd": "safe"}})

    async def run(manager: HookManager) -> tuple[object, list[str], object]:
        sink = FakeSink()
        with trajectory_scope(make_context(sink)):
            decision = await manager.fire(HookEvent.BEFORE_TOOL_CALL, _tool())
        outcome = sink.only(EventType.HOOK_OPERATION_FINISHED).payload["outcome"]
        return decision, [str(kind) for kind in sink.event_types], outcome

    control = HookManager(
        file=HooksFile(hooks=[_hook("rewrite", HookEvent.BEFORE_TOOL_CALL, script=script)]), hooks_dir=hooks_dir
    )
    expected = await run(control)

    observed = HookManager(
        file=HooksFile(hooks=[_hook("rewrite", HookEvent.BEFORE_TOOL_CALL, script=script)]), hooks_dir=hooks_dir
    )
    calls: list[str] = []

    async def explode() -> None:
        calls.append("runs")
        raise RuntimeError("subscriber exploded")

    log = HookRunLog()
    observed.record_runs(log, session_id=_SESSION, on_change=explode)
    actual = await run(observed)
    assert calls
    assert actual == expected
    (record,) = log.records()
    assert (record.state, record.effect) == ("ended", "modified_args")
