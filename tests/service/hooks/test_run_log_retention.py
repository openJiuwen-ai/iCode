# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""What the hook run log keeps: queue timing, the payload as registered, and its row and detail limits.

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

import pytest

from chrys.foundation.models.hook_runs import HookRunApplied, HookRunDetail, HookRunRecord
from chrys.service.hooks.events import HookEvent
from chrys.service.hooks.manager import HookManager
from chrys.service.hooks.run_log import (
    HOOK_RUN_LOG_MAX_DETAIL_CHARS,
    HOOK_RUN_LOG_MAX_ENDED,
    HOOK_RUN_TEXT_MAX_BYTES,
    UNWRITABLE_JSON,
    HookRunLog,
    indented_json,
)
from chrys.service.hooks.runner import HookResult, HookRunner
from chrys.service.hooks.schema import HookConfig, HookExecution, HookMatch, HookRun, HookSettings, HooksFile
from chrys.service.trajectory.hooks import HookOutcome
from tests.support.waiting import ENGINE_TURN_TIMEOUT, wait_for

_SESSION = "s1"


def _held(release: Path) -> str:
    return textwrap.dedent(
        f"""
        import os, time
        while not os.path.exists({str(release)!r}):
            time.sleep(0.01)
        """
    )


def _copy_payload(target: Path, then: str = "") -> str:
    return (
        textwrap.dedent(
            f"""
            import json, os
            with open(os.environ["CHRYS_HOOK_PAYLOAD_FILE"], "rb") as source:
                data = source.read()
            with open({str(target)!r}, "wb") as copy:
                copy.write(data)
            """
        )
        + then
    )


def _hook(
    hook_id: str,
    event: HookEvent,
    *,
    mode: str = "blocking",
    script: str = "pass",
    tool_name: str | None = None,
    delivery: str = "best_effort",
) -> HookConfig:
    return HookConfig(
        id=hook_id,
        event=event,
        run=HookRun(type="command", argv=[sys.executable, "-c", script]),
        execution=HookExecution(mode=mode, timeout_seconds=30.0, delivery=delivery),
        match=HookMatch(tool_name=tool_name),
    )


def _manager(hooks_dir: Path, *hooks: HookConfig, **settings: float) -> HookManager:
    return HookManager(file=HooksFile(settings=HookSettings(**settings), hooks=list(hooks)), hooks_dir=hooks_dir)


def _tool(name: str = "zsh", args: dict[str, object] | None = None) -> dict[str, object]:
    return {"tool": {"name": name, "kind": "shell", "call_id": "c1", "args": args or {}}}


def _by_id(log: HookRunLog, hook_id: str) -> list[HookRunRecord]:
    return [record for record in log.records() if record.hook_id == hook_id]


@pytest.fixture
def hooks_dir(tmp_path: Path) -> Path:
    path = tmp_path / "hooks"
    path.mkdir()
    return path


@pytest.fixture
async def release(tmp_path: Path) -> AsyncIterator[Path]:
    path = tmp_path / "release"
    yield path
    path.touch()


def test_the_limits_are_the_planned_ones() -> None:
    assert HOOK_RUN_LOG_MAX_ENDED == 100
    assert HOOK_RUN_LOG_MAX_DETAIL_CHARS == 16 * 1024 * 1024
    assert HOOK_RUN_TEXT_MAX_BYTES == 64 * 1024
    # The block reasons of every ended run are kept, so they must fit the budget together.
    assert HOOK_RUN_LOG_MAX_ENDED * HOOK_RUN_TEXT_MAX_BYTES <= HOOK_RUN_LOG_MAX_DETAIL_CHARS


# --- Queueing (max_parallel_hooks=1) --------------------------------------------


async def test_a_queued_run_has_no_start_until_it_takes_the_slot(hooks_dir: Path, release: Path) -> None:
    manager = _manager(
        hooks_dir,
        _hook("holder", HookEvent.AFTER_TOOL_CALL, mode="fire_and_forget", script=_held(release)),
        _hook("waiting", HookEvent.BEFORE_TOOL_CALL),
        max_parallel_hooks=1,
    )
    log = HookRunLog()
    manager.record_runs(log, session_id=_SESSION)
    await manager.fire(HookEvent.AFTER_TOOL_CALL, _tool())
    await wait_for(
        lambda: any(r.hook_id == "holder" and r.state == "running" for r in log.records()),
        timeout=ENGINE_TURN_TIMEOUT,
        description="the holder running",
    )
    wall_before = time.time()
    fire = asyncio.create_task(manager.fire(HookEvent.BEFORE_TOOL_CALL, _tool()))
    try:
        await wait_for(
            lambda: bool(_by_id(log, "waiting")) or fire.done(), timeout=ENGINE_TURN_TIMEOUT, description="queued"
        )
        (queued,) = _by_id(log, "waiting")
        assert queued.state == "queued"
        assert queued.started_at is None and queued.ended_at is None
        assert wall_before <= queued.registered_wall <= time.time()
        detail = log.detail(queued.run_id)
        assert detail is not None and detail.io is not None, "a queued run's input is already kept"
        assert detail.applied is None
        seen_queued_at = time.monotonic()
        release.touch()
        await asyncio.wait_for(fire, ENGINE_TURN_TIMEOUT)
    finally:
        release.touch()
    (ended,) = _by_id(log, "waiting")
    assert ended.state == "ended"
    assert ended.registered_at == queued.registered_at
    assert ended.started_at is not None and ended.started_at > seen_queued_at
    await manager.drain_session()


async def test_a_run_cancelled_while_queued_never_started(hooks_dir: Path, release: Path) -> None:
    manager = _manager(
        hooks_dir,
        _hook("holder", HookEvent.AFTER_TOOL_CALL, mode="fire_and_forget", script=_held(release)),
        _hook("waiting", HookEvent.BEFORE_TOOL_CALL),
        max_parallel_hooks=1,
    )
    log = HookRunLog()
    changes: list[tuple[HookRunRecord, ...]] = []

    async def on_change() -> None:
        changes.append(log.records())

    manager.record_runs(log, session_id=_SESSION, on_change=on_change)
    await manager.fire(HookEvent.AFTER_TOOL_CALL, _tool())
    await wait_for(
        lambda: any(r.hook_id == "holder" and r.state == "running" for r in log.records()),
        timeout=ENGINE_TURN_TIMEOUT,
        description="the holder running",
    )
    fire = asyncio.create_task(manager.fire(HookEvent.BEFORE_TOOL_CALL, _tool()))
    await wait_for(
        lambda: any(r.hook_id == "waiting" and r.state == "queued" for r in log.records()) or fire.done(),
        timeout=ENGINE_TURN_TIMEOUT,
        description="queued",
    )
    fire.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await fire
    (record,) = _by_id(log, "waiting")
    assert (record.state, record.outcome, record.started_at) == ("ended", HookOutcome.CANCELLED, None)
    assert changes[-1] == log.records()
    release.touch()
    await manager.drain_session()
    # No drain waits on a fire_and_forget run: the holder ends once released.
    await wait_for(lambda: not manager._inflight, timeout=ENGINE_TURN_TIMEOUT, description="the holder ended")


# --- The payload as registered (5.3) --------------------------------------------


async def test_the_payload_is_what_the_hook_received_at_registration(hooks_dir: Path, tmp_path: Path) -> None:
    first_received, second_received = tmp_path / "first", tmp_path / "second"
    rewrite = (
        'with open(os.environ["CHRYS_HOOK_RESULT"], "w") as f:\n'
        '    json.dump({"action": "modify", "args_override": {"path": "/b"}}, f)\n'
    )
    manager = _manager(
        hooks_dir,
        _hook("rewrite", HookEvent.BEFORE_TOOL_CALL, script=_copy_payload(first_received, rewrite)),
        _hook("after-rewrite", HookEvent.BEFORE_TOOL_CALL, script=_copy_payload(second_received)),
    )
    log = HookRunLog()
    manager.record_runs(log, session_id=_SESSION)
    await manager.fire(HookEvent.BEFORE_TOOL_CALL, _tool(args={"path": "/a"}))

    (first,) = _by_id(log, "rewrite")
    (second,) = _by_id(log, "after-rewrite")
    first_detail, second_detail = log.detail(first.run_id), log.detail(second.run_id)
    assert first_detail is not None and first_detail.io is not None
    assert second_detail is not None and second_detail.io is not None
    # Byte for byte what each hook read from its payload file.
    assert first_detail.io.payload == first_received.read_text(encoding="utf-8")
    assert second_detail.io.payload == second_received.read_text(encoding="utf-8")
    # The earlier run's payload is not changed by the later merge.
    assert json.loads(first_detail.io.payload)["tool"]["args"]["path"] == "/a"
    assert json.loads(second_detail.io.payload)["tool"]["args"]["path"] == "/b"
    assert first_detail.io.payload_size == len(first_detail.io.payload)


async def test_a_large_payload_is_cut_and_its_size_kept(hooks_dir: Path, tmp_path: Path) -> None:
    received = tmp_path / "received"
    manager = _manager(hooks_dir, _hook("big", HookEvent.BEFORE_TOOL_CALL, script=_copy_payload(received)))
    log = HookRunLog()
    manager.record_runs(log, session_id=_SESSION)
    await manager.fire(HookEvent.BEFORE_TOOL_CALL, _tool(args={"content": "x" * 200_000}))
    (record,) = log.records()
    detail = log.detail(record.run_id)
    assert detail is not None and detail.io is not None
    full = received.read_text(encoding="utf-8")
    assert len(full) > 200_000, "the hook itself gets the whole payload"
    assert len(detail.io.payload) <= HOOK_RUN_TEXT_MAX_BYTES
    assert full.startswith(detail.io.payload)
    assert detail.io.payload_size == len(full)


async def test_without_a_log_nothing_is_serialized_for_it(hooks_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str | None] = []
    original = HookRunner.run_and_wait

    async def spy(
        self: HookRunner,
        hook: HookConfig,
        payload: dict[str, object],
        *,
        payload_text: str | None = None,
        result: HookResult | None = None,
    ) -> HookResult:
        seen.append(payload_text)
        return await original(self, hook, payload, payload_text=payload_text, result=result)

    monkeypatch.setattr(HookRunner, "run_and_wait", spy)
    unrecorded = _manager(hooks_dir, _hook("plain", HookEvent.AFTER_TURN))
    await unrecorded.fire(HookEvent.AFTER_TURN, {"turn": 1})
    assert seen == [None]

    recorded = _manager(hooks_dir, _hook("plain", HookEvent.AFTER_TURN))
    log = HookRunLog()
    recorded.record_runs(log, session_id=_SESSION)
    await recorded.fire(HookEvent.AFTER_TURN, {"turn": 1})
    assert seen[1] is not None
    (record,) = log.records()
    detail = log.detail(record.run_id)
    assert detail is not None and detail.io is not None and detail.io.payload == seen[1]


# --- Rows kept ------------------------------------------------------------------


async def test_only_the_latest_ended_runs_are_kept_and_running_ones_do_not_count(
    hooks_dir: Path, release: Path
) -> None:
    manager = _manager(
        hooks_dir,
        _hook("long", HookEvent.SESSION_START, mode="async", script=_held(release)),
        _hook("quick", HookEvent.AFTER_TURN),
    )
    log = HookRunLog(max_ended=3)
    manager.record_runs(log, session_id=_SESSION)
    await manager.fire(HookEvent.SESSION_START, {}, scope="session")
    await wait_for(
        lambda: any(r.hook_id == "long" and r.state == "running" for r in log.records()),
        timeout=ENGINE_TURN_TIMEOUT,
        description="the long run running",
    )
    run_ids: list[str] = []
    for turn in range(5):
        await manager.fire(HookEvent.AFTER_TURN, {"turn": turn})
        run_ids.extend(r.run_id for r in _by_id(log, "quick") if r.run_id not in run_ids)
    assert len(run_ids) == 5
    kept = _by_id(log, "quick")
    assert {record.run_id for record in kept} == set(run_ids[-3:])
    assert [r.hook_id for r in log.records() if r.state != "ended"] == ["long"]
    assert log.record(run_ids[0]) is None
    assert log.detail(run_ids[0]) is None
    # Numbered in registration order from 1; the numbers stay as older runs are dropped.
    assert _by_id(log, "long")[0].number == 1
    assert [record.number for record in sorted(kept, key=lambda r: r.number)] == [4, 5, 6]
    release.touch()
    await asyncio.wait_for(manager.drain_session(), ENGINE_TURN_TIMEOUT)
    # Once ended it counts, and the oldest of the quick ones goes.
    assert {r.run_id for r in log.records()} == {*run_ids[-2:], _by_id(log, "long")[0].run_id}


async def test_dropped_counts_only_ended_runs_dropped_past_the_cap(hooks_dir: Path, release: Path) -> None:
    manager = _manager(
        hooks_dir,
        _hook("long", HookEvent.SESSION_START, mode="async", script=_held(release)),
        _hook("quick", HookEvent.AFTER_TURN),
    )
    log = HookRunLog(max_ended=3)
    assert log.dropped == 0
    manager.record_runs(log, session_id=_SESSION)
    await manager.fire(HookEvent.SESSION_START, {}, scope="session")
    await wait_for(
        lambda: any(r.hook_id == "long" and r.state == "running" for r in log.records()),
        timeout=ENGINE_TURN_TIMEOUT,
        description="the long run running",
    )
    for turn in range(3):
        await manager.fire(HookEvent.AFTER_TURN, {"turn": turn})
    # Three ended at the cap, one running beside them: nothing dropped yet.
    assert len(log.records()) == 4
    assert log.dropped == 0
    for turn in range(3, 5):
        await manager.fire(HookEvent.AFTER_TURN, {"turn": turn})
    assert log.dropped == 2
    release.touch()
    await asyncio.wait_for(manager.drain_session(), ENGINE_TURN_TIMEOUT)
    # The long run ended and counts now: one more ended run goes.
    assert log.dropped == 3
    assert len(log.records()) == 3


async def test_dropped_does_not_count_queued_runs(hooks_dir: Path, release: Path) -> None:
    manager = _manager(
        hooks_dir,
        _hook("holder", HookEvent.AFTER_TOOL_CALL, mode="fire_and_forget", script=_held(release)),
        _hook("waiting", HookEvent.BEFORE_TOOL_CALL),
        max_parallel_hooks=1,
    )
    log = HookRunLog(max_ended=1)
    manager.record_runs(log, session_id=_SESSION)
    await manager.fire(HookEvent.AFTER_TOOL_CALL, _tool())
    await wait_for(
        lambda: any(r.hook_id == "holder" and r.state == "running" for r in log.records()),
        timeout=ENGINE_TURN_TIMEOUT,
        description="the holder running",
    )
    fires = [asyncio.create_task(manager.fire(HookEvent.BEFORE_TOOL_CALL, _tool())) for _ in range(3)]
    try:
        await wait_for(
            lambda: len([r for r in log.records() if r.state == "queued"]) == 3 or any(f.done() for f in fires),
            timeout=ENGINE_TURN_TIMEOUT,
            description="three queued",
        )
        assert [r.state for r in _by_id(log, "waiting")] == ["queued"] * 3
        assert log.dropped == 0, "queued and running runs are never dropped"
        release.touch()
        await asyncio.wait_for(asyncio.gather(*fires), ENGINE_TURN_TIMEOUT)
    finally:
        release.touch()
    await asyncio.wait_for(manager.drain_session(), ENGINE_TURN_TIMEOUT)
    # Four ended, one kept.
    assert len(log.records()) == 1
    assert log.dropped == 3


async def test_records_stay_in_registration_order_as_runs_start_and_end(hooks_dir: Path, tmp_path: Path) -> None:
    releases = {name: tmp_path / f"release-{name}" for name in ("a", "b", "c", "d")}
    manager = _manager(
        hooks_dir,
        *(
            _hook(name, HookEvent.AFTER_TOOL_CALL, mode="fire_and_forget", script=_held(path), tool_name=name)
            for name, path in releases.items()
        ),
        max_parallel_hooks=3,
    )
    log = HookRunLog()
    manager.record_runs(log, session_id=_SESSION)
    try:
        for name in releases:
            await manager.fire(HookEvent.AFTER_TOOL_CALL, _tool(name))
            await wait_for(
                lambda name=name: bool(_by_id(log, name)), timeout=ENGINE_TURN_TIMEOUT, description=f"{name} in"
            )
        # End b first, then a: c, d stay active (d queued behind three slots, then running).
        releases["b"].touch()
        await wait_for(lambda: _by_id(log, "b")[0].state == "ended", timeout=ENGINE_TURN_TIMEOUT, description="b")
        releases["a"].touch()
        await wait_for(lambda: _by_id(log, "a")[0].state == "ended", timeout=ENGINE_TURN_TIMEOUT, description="a")
        # Oldest first, each where it was registered, however and whenever it ended.
        assert [r.hook_id for r in log.records()] == ["a", "b", "c", "d"]
        assert [r.number for r in log.records()] == [1, 2, 3, 4]
    finally:
        for path in releases.values():
            path.touch()
    await asyncio.wait_for(manager.drain_session(), ENGINE_TURN_TIMEOUT)
    # No drain waits on a fire_and_forget run: c and d end once released.
    await wait_for(lambda: not manager._inflight, timeout=ENGINE_TURN_TIMEOUT, description="every run ended")


# --- Details kept ---------------------------------------------------------------


def _printer(size: int) -> str:
    return f"import sys\nsys.stdout.write('o' * {size})\n"


async def test_details_over_the_cap_are_dropped_from_the_oldest_ended_run(hooks_dir: Path) -> None:
    manager = _manager(hooks_dir, _hook("noisy", HookEvent.AFTER_TURN, script=_printer(4000)))
    log = HookRunLog(max_detail_chars=10_000)
    manager.record_runs(log, session_id=_SESSION)
    for turn in range(5):
        await manager.fire(HookEvent.AFTER_TURN, {"turn": turn})
    records = log.records()
    assert len(records) == 5, "every row stays; only details go"
    newest_first = [log.detail(record.run_id) for record in reversed(records)]
    assert all(detail is not None for detail in newest_first)
    kept = [detail.io is not None for detail in newest_first if detail is not None]
    # Newest kept, oldest dropped, and nothing older kept once one is dropped.
    assert kept[0] is True and kept[-1] is False
    assert kept == sorted(kept, reverse=True)
    gone = next(detail for detail in newest_first if detail is not None and detail.io is None)
    assert gone.record.outcome == HookOutcome.SUCCESS
    assert gone.run_argv


async def test_details_trimmed_by_size_do_not_count_as_dropped_runs(hooks_dir: Path) -> None:
    manager = _manager(hooks_dir, _hook("noisy", HookEvent.AFTER_TURN, script=_printer(4000)))
    log = HookRunLog(max_detail_chars=10_000)
    manager.record_runs(log, session_id=_SESSION)
    for turn in range(5):
        await manager.fire(HookEvent.AFTER_TURN, {"turn": turn})
    assert any((detail := log.detail(record.run_id)) is not None and detail.io is None for record in log.records()), (
        "some details were trimmed"
    )
    assert len(log.records()) == 5
    assert log.dropped == 0


async def test_a_running_runs_detail_is_never_dropped(hooks_dir: Path, release: Path) -> None:
    manager = _manager(
        hooks_dir,
        _hook("long", HookEvent.BEFORE_TOOL_CALL, mode="async", script=_held(release)),
        _hook("noisy", HookEvent.AFTER_TURN, script=_printer(4000)),
    )
    log = HookRunLog(max_detail_chars=10_000)
    manager.record_runs(log, session_id=_SESSION)
    # Its payload alone is over the cap.
    await manager.fire(HookEvent.BEFORE_TOOL_CALL, _tool(args={"content": "y" * 12_000}))
    await wait_for(
        lambda: any(r.hook_id == "long" and r.state == "running" for r in log.records()),
        timeout=ENGINE_TURN_TIMEOUT,
        description="the long run running",
    )
    for turn in range(4):
        await manager.fire(HookEvent.AFTER_TURN, {"turn": turn})
    (long,) = _by_id(log, "long")
    detail = log.detail(long.run_id)
    assert detail is not None and detail.io is not None
    assert "y" * 1000 in detail.io.payload
    release.touch()
    await asyncio.wait_for(manager.drain_turn(), ENGINE_TURN_TIMEOUT)


def _register(log: HookRunLog, run_id: str) -> None:
    log.register(
        run_id=run_id,
        session_id=_SESSION,
        hook=_hook("h", HookEvent.BEFORE_TOOL_CALL),
        source="",
        source_path="",
        payload_text="{}",
        tool_name="",
        tool_call_id="",
        sub_agent="",
        retry=False,
    )


def _kept_text(detail: HookRunDetail) -> int:
    """Every text *detail* holds, counted here apart from the log's own count."""
    size = 0
    if detail.io is not None:
        size += sum(len(text) for text in (detail.io.payload, detail.io.decision, detail.io.stdout, detail.io.stderr))
    if detail.applied is not None:
        applied = detail.applied
        size += sum(
            len(text)
            for text in (
                applied.block_reason,
                applied.args,
                applied.reminder,
                applied.extra_context,
                applied.result_override,
            )
        )
    return size


def test_what_ended_runs_applied_counts_toward_the_detail_budget() -> None:
    log = HookRunLog(max_detail_chars=10_000)
    for n in range(5):
        _register(log, f"r{n}")
        log.finish(
            f"r{n}",
            outcome=HookOutcome.SUCCESS,
            effect="added_reminder",
            applied=HookRunApplied(reminder="r" * 4000),
            stdout="o" * 10,
        )
    details = [log.detail(f"r{n}") for n in range(5)]
    assert all(detail is not None for detail in details)
    kept = [detail for detail in details if detail is not None]
    assert sum(_kept_text(detail) for detail in kept) <= 10_000
    assert log._detail_chars == sum(_kept_text(detail) for detail in kept), "the count is what the details hold"
    # The oldest go first, the texts iCode took from them with what they read and wrote.
    assert kept[0].io is None and kept[0].applied is not None and kept[0].applied.reminder == ""
    assert kept[-1].applied is not None and kept[-1].applied.reminder == "r" * 4000


def test_a_replaced_result_counts_toward_the_detail_budget() -> None:
    """A hook may replace the text the model reads with one nearly a megabyte: it is a detail like the rest."""
    log = HookRunLog(max_detail_chars=10_000)
    for n in range(5):
        _register(log, f"r{n}")
        log.finish(
            f"r{n}",
            outcome=HookOutcome.SUCCESS,
            effect="replaced_result",
            applied=HookRunApplied(result_override="x" * 4000),
        )
    details = [log.detail(f"r{n}") for n in range(5)]
    kept = [detail for detail in details if detail is not None]
    assert len(kept) == 5, "every row stays; only details go"
    assert sum(_kept_text(detail) for detail in kept) <= 10_000
    assert log._detail_chars == sum(_kept_text(detail) for detail in kept), "the count is what the details hold"
    # The oldest go first, the text iCode took from them with what they read and wrote.
    assert kept[0].applied is not None and kept[0].applied.result_override == ""
    assert kept[-1].applied is not None and kept[-1].applied.result_override == "x" * 4000


def test_a_replaced_result_is_forgotten_once_its_details_go() -> None:
    """The replacement is the whole result file's worth of text: dropping the details must drop it too."""
    log = HookRunLog(max_detail_chars=1_000)
    _register(log, "rewrite")
    log.finish(
        "rewrite",
        outcome=HookOutcome.SUCCESS,
        effect="replaced_result",
        applied=HookRunApplied(result_override="x" * 4_000),
    )
    detail = log.detail("rewrite")
    assert detail is not None
    assert detail.io is None, "the details went"
    assert detail.applied is not None and detail.applied.result_override == ""
    assert log._detail_chars == 0


def test_a_block_whose_details_went_still_says_why() -> None:
    log = HookRunLog(max_detail_chars=1_000)
    _register(log, "guard")
    log.finish("guard", outcome=HookOutcome.SUCCESS, effect="blocked", applied=HookRunApplied(block_reason="no"))
    _register(log, "noisy")
    log.finish("noisy", outcome=HookOutcome.SUCCESS, stdout="o" * 2_000)
    detail = log.detail(log.records()[0].run_id)
    assert detail is not None and detail.io is None
    assert detail.applied is not None and detail.applied.block_reason == "no"


@pytest.mark.parametrize("unit", ["a", "é", "\udc80"], ids=["ascii", "two-byte", "lone-surrogate"])
def test_changed_arguments_past_the_cap_are_cut_with_their_size_kept(unit: str) -> None:
    full = json.dumps({"content": unit * 70_000}, ensure_ascii=False)
    log = HookRunLog()
    _register(log, "rewrite")
    log.finish("rewrite", outcome=HookOutcome.SUCCESS, effect="modified_args", applied=HookRunApplied(args=full))
    detail = log.detail("rewrite")
    assert detail is not None and detail.applied is not None
    kept = detail.applied.args
    # Cut short of the limit by the character it would split, at most: a path that is not UTF-8 included.
    assert HOOK_RUN_TEXT_MAX_BYTES - 4 < len(kept.encode("utf-8", "surrogatepass")) <= HOOK_RUN_TEXT_MAX_BYTES
    assert detail.applied.args_size == len(full.encode("utf-8", "surrogatepass"))
    assert full.startswith(kept)


def test_changed_arguments_within_the_cap_are_kept_whole() -> None:
    full = json.dumps({"path": "/é/b"}, ensure_ascii=False)
    log = HookRunLog()
    _register(log, "rewrite")
    log.finish("rewrite", outcome=HookOutcome.SUCCESS, effect="modified_args", applied=HookRunApplied(args=full))
    detail = log.detail("rewrite")
    assert detail is not None and detail.applied is not None
    assert (detail.applied.args, detail.applied.args_size) == (full, 0)


def test_the_block_reasons_kept_fit_the_detail_budget() -> None:
    """A block reason outlives the other details, so each is cut as the payload is."""
    reason = "r" * (HOOK_RUN_LOG_MAX_DETAIL_CHARS // HOOK_RUN_LOG_MAX_ENDED + 1)
    log = HookRunLog()
    for index in range(HOOK_RUN_LOG_MAX_ENDED):
        _register(log, f"guard-{index}")
        log.finish(
            f"guard-{index}", outcome=HookOutcome.SUCCESS, effect="blocked", applied=HookRunApplied(block_reason=reason)
        )
    details = [log.detail(record.run_id) for record in log.records()]
    assert len(details) == HOOK_RUN_LOG_MAX_ENDED
    assert sum(_kept_text(detail) for detail in details if detail is not None) <= HOOK_RUN_LOG_MAX_DETAIL_CHARS
    for detail in details:
        assert detail is not None and detail.applied is not None
        assert detail.applied.block_reason == reason[:HOOK_RUN_TEXT_MAX_BYTES]


def _nested(depth: int) -> object:
    return json.loads("[" * depth + "]" * depth)


def test_json_is_indented_while_that_keeps_it_small() -> None:
    value = {"path": "/é", "lines": [1, 2], "deep": _nested(5)}
    assert indented_json(value) == json.dumps(value, indent=2, ensure_ascii=False)


@pytest.mark.parametrize(
    "value",
    # Indenting adds the depth to every line: 200 levels of 400 characters would indent to 80,000.
    [_nested(200), {"pad": "x" * 2_000_000, "deep": _nested(3_000)}],
    ids=["swelling", "past-the-recursion-limit"],
)
def test_json_that_indenting_would_swell_is_kept_compact(value: object) -> None:
    assert indented_json(value) == json.dumps(value, ensure_ascii=False)


def test_json_nested_deeper_than_the_encoder_writes_reads_as_a_note() -> None:
    # Deeper than any stack lets the encoder go; parsed JSON can be deeper than it writes.
    deep: dict[str, object] = {}
    node = deep
    for _ in range(100_000):
        child: dict[str, object] = {}
        node["a"] = child
        node = child
    assert indented_json({"action": "allow", "meta": deep}) == UNWRITABLE_JSON


# --- Outbox retries and sessions ------------------------------------------------


async def test_an_outbox_retry_is_recorded_as_a_retry(hooks_dir: Path) -> None:
    hook = _hook("durable", HookEvent.AFTER_TURN, mode="async", delivery="durable")
    manager = _manager(hooks_dir, hook, outbox_retry_age_seconds=0.0)
    manager._outbox.write_pending(hook_id="durable", event=str(HookEvent.AFTER_TURN), payload={"turn": 1})
    log = HookRunLog()
    manager.record_runs(log, session_id=_SESSION)
    assert await manager.recover_outbox() == 1
    await wait_for(
        lambda: any(r.state == "ended" for r in log.records()), timeout=ENGINE_TURN_TIMEOUT, description="retried"
    )
    (record,) = log.records()
    assert (record.hook_id, record.retry, record.outcome) == ("durable", True, HookOutcome.SUCCESS)

    await manager.fire(HookEvent.AFTER_TURN, {"turn": 2})
    await asyncio.wait_for(manager.drain_turn(), ENGINE_TURN_TIMEOUT)
    fresh = [r for r in log.records() if r.run_id != record.run_id]
    assert [r.retry for r in fresh] == [False]
    await manager.drain_session()


async def test_one_log_holds_the_runs_of_every_session(hooks_dir: Path) -> None:
    log = HookRunLog()
    first = _manager(hooks_dir, _hook("lint", HookEvent.AFTER_TURN))
    first.record_runs(log, session_id="session-a")
    second = _manager(hooks_dir, _hook("lint", HookEvent.AFTER_TURN))
    second.record_runs(log, session_id="session-b")
    await first.fire(HookEvent.AFTER_TURN, {"turn": 1})
    await second.fire(HookEvent.AFTER_TURN, {"turn": 1})
    await first.fire(HookEvent.AFTER_TURN, {"turn": 2})
    assert [record.session_id for record in log.records()] == ["session-a", "session-b", "session-a"]
