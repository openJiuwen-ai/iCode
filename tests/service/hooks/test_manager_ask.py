# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""``action: ask`` through :class:`HookManager`: one check per operation version, for every entry.

Written from the plan (``.plan/hook-ask-confirm.md`` §1, §2, §7): the user's
answer decides the operation; a version is committed only once the hooks
checked it through to the end; an argument change after an approval checks
the new version from the first hook again, within a bound; an empty hook
chain is the same check; and waiting for the user holds no hook slot and
counts toward no script timeout.
"""

from __future__ import annotations

import asyncio
import json
import sys
import textwrap
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest

from chrys.foundation.events.bus import EventBus
from chrys.foundation.models.hook_confirmations import HookArgumentChange
from chrys.foundation.trajectory.context import trajectory_scope
from chrys.foundation.trajectory.event_types import EventType
from chrys.service.hooks.confirmation import HookConfirmationService
from chrys.service.hooks.events import HookEvent
from chrys.service.hooks.manager import HookManager, check_operation
from chrys.service.hooks.operation import HookOperation
from chrys.service.hooks.run_log import HookRunLog
from chrys.service.hooks.schema import (
    HookArgMatch,
    HookConfig,
    HookExecution,
    HookMatch,
    HookRun,
    HookSettings,
    HooksFile,
)
from tests.service.trajectory._fakes import FakeSink, make_context
from tests.support.hook_confirmations import FakeConfirmationFrontend
from tests.support.waiting import ENGINE_TURN_TIMEOUT, wait_for

_SESSION = "s1"


def _result_script(result: dict[str, Any]) -> str:
    return textwrap.dedent(
        f"""
        import json, os
        with open(os.environ["CHRYS_HOOK_RESULT"], "w") as f:
            json.dump({result!r}, f)
        """
    )


def _hook(
    hook_id: str,
    result: dict[str, Any] | None = None,
    *,
    event: HookEvent = HookEvent.BEFORE_TOOL_CALL,
    mode: str = "blocking",
    path: str | None = None,
    timeout: float = 10.0,
    script: str | None = None,
) -> HookConfig:
    return HookConfig(
        id=hook_id,
        event=event,
        run=HookRun(type="command", argv=[sys.executable, "-c", script or _result_script(result or {})]),
        execution=HookExecution(mode=mode, timeout_seconds=timeout),
        match=HookMatch(args={"path": HookArgMatch(equals=path)}) if path is not None else HookMatch(),
    )


def _manager(hooks_dir: Path, *hooks: HookConfig, max_parallel_hooks: int = 4) -> HookManager:
    return HookManager(
        file=HooksFile(settings=HookSettings(max_parallel_hooks=max_parallel_hooks), hooks=list(hooks)),
        hooks_dir=hooks_dir,
    )


def _asking(manager: HookManager | None, confirmations: HookConfirmationService | None) -> HookManager | None:
    if manager is not None:
        manager.confirmations = confirmations
    return manager


def _operation(path: str = "x") -> HookOperation:
    return HookOperation(
        subject="tool", session_id=_SESSION, tool_name="edit_file", tool_kind="edit", arguments={"path": path}
    )


def _payload(operation: HookOperation) -> dict[str, Any]:
    return {"session_id": _SESSION, "tool": {"name": operation.tool_name, "args": operation.arguments()}}


async def _check(
    manager: HookManager | None,
    operation: HookOperation,
    confirmations: HookConfirmationService | None,
    event: HookEvent = HookEvent.BEFORE_TOOL_CALL,
):
    return await check_operation(_asking(manager, confirmations), event, operation, lambda: _payload(operation))


@pytest.fixture
def hooks_dir(tmp_path: Path) -> Path:
    path = tmp_path / "hooks"
    path.mkdir()
    return path


@pytest.fixture
async def bus() -> EventBus:
    return EventBus()


@pytest.fixture
async def confirmations(bus: EventBus) -> HookConfirmationService:
    service = HookConfirmationService(bus=bus, enabled=True)
    await service.start()
    return service


async def _frontend(bus: EventBus, **kwargs: Any) -> FakeConfirmationFrontend:
    frontend = FakeConfirmationFrontend(bus, session_id=_SESSION, **kwargs)
    await frontend.attach()
    return frontend


# ── the user's answer ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_an_approved_ask_goes_on_to_the_later_hooks_and_checks_the_version(
    hooks_dir: Path, bus: EventBus, confirmations: HookConfirmationService
) -> None:
    frontend = await _frontend(bus, answer_inline="approved")
    manager = _manager(
        hooks_dir,
        _hook("guard", {"action": "ask", "reason": "prod change"}),
        _hook("later", {"action": "block", "reason": "later hook ran"}),
    )
    operation = _operation()

    decision = await _check(manager, operation, confirmations)

    assert [(r.hook_id, r.reason, r.arguments) for r in frontend.requests] == [("guard", "prod change", {"path": "x"})]
    assert decision.blocked is True
    assert decision.confirmation is None
    assert "later hook ran" in decision.block_reason


@pytest.mark.asyncio
async def test_an_approved_ask_alone_leaves_the_version_checked(
    hooks_dir: Path, bus: EventBus, confirmations: HookConfirmationService
) -> None:
    await _frontend(bus, answer_inline="approved")
    manager = _manager(hooks_dir, _hook("guard", {"action": "ask"}))
    operation = _operation()

    decision = await _check(manager, operation, confirmations)

    assert (decision.blocked, decision.invalidated) == (False, False)
    assert operation.checked_revision == operation.revision
    assert operation.commit_arguments({"path": "x"}) is None


@pytest.mark.parametrize(
    ("answer", "says", "outcome"),
    [
        ("declined", "the user declined, so the tool call did not run. Do not call it again unless", "declined"),
        ("dismissed", "the user declined, so the tool call did not run. Do not call it again unless", "declined"),
        ("frontend_unavailable", "could not be shown to the user, so the tool call did not run. Tell", "unavailable"),
    ],
)
@pytest.mark.asyncio
async def test_an_ask_not_approved_stops_the_operation_and_says_why(
    hooks_dir: Path, bus: EventBus, confirmations: HookConfirmationService, answer: str, says: str, outcome: str
) -> None:
    await _frontend(bus, answer_inline=answer)
    later = hooks_dir / "later-ran"
    manager = _manager(
        hooks_dir,
        _hook("guard", {"action": "ask", "reason": "prod change"}),
        _hook("later", script=f"open({str(later)!r}, 'w').close()"),
        _hook("watcher", script=f"open({str(later)!r} + '-async', 'w').close()", mode="async"),
    )
    operation = _operation()

    decision = await _check(manager, operation, confirmations)
    await manager.drain_turn()

    assert (decision.blocked, decision.confirmation) == (True, outcome)
    assert decision.block_reason.startswith("hook 'guard' asked for confirmation (prod change); ")
    assert says in decision.block_reason
    assert not later.exists()
    assert not Path(f"{later}-async").exists()
    assert operation.checked_revision is None
    assert operation.commit_arguments({"path": "x"}) == "unchecked"


@pytest.mark.asyncio
async def test_without_anyone_to_ask_an_ask_is_refused_at_once(hooks_dir: Path, bus: EventBus) -> None:
    disabled = HookConfirmationService(bus=bus, enabled=False)
    await disabled.start()
    frontend = await _frontend(bus)
    manager = _manager(hooks_dir, _hook("guard", {"action": "ask"}))

    for service in (disabled, None):
        decision = await _check(manager, _operation(), service)
        assert (decision.blocked, decision.confirmation) == (True, "unavailable")
        assert decision.block_reason == (
            "hook 'guard' asked for confirmation; no one can confirm it in this session, so the tool call did not"
            " run. Calling it again will be refused the same way."
        )
    assert frontend.requests == []


@pytest.mark.asyncio
async def test_a_question_nobody_answers_tells_the_model_to_ask_before_calling_again(
    hooks_dir: Path, bus: EventBus
) -> None:
    # A model told only that nobody answered calls the tool again, and the user is asked once more.
    service = HookConfirmationService(bus=bus, enabled=True, timeout_seconds=0.05)
    await service.start()
    await _frontend(bus)
    manager = _manager(hooks_dir, _hook("guard", {"action": "ask"}))

    decision = await _check(manager, _operation(), service)

    assert (decision.blocked, decision.confirmation) == (True, "timed_out")
    assert decision.block_reason == (
        "hook 'guard' asked for confirmation; the user did not answer within 0.05 s, so the tool call did not run."
        " Ask the user before calling it again."
    )


@pytest.mark.asyncio
async def test_a_refused_input_says_why_without_tool_call_advice(hooks_dir: Path, bus: EventBus) -> None:
    # A refused message's reason is shown to the user, not read by the model.
    manager = _manager(hooks_dir, _hook("secrets", {"action": "ask"}, event=HookEvent.USER_PROMPT_SUBMIT))
    operation = HookOperation(subject="input", session_id=_SESSION, input_text="hi")

    decision = await check_operation(
        _asking(manager, None), HookEvent.USER_PROMPT_SUBMIT, operation, lambda: {"session_id": _SESSION, "text": "hi"}
    )

    assert decision.block_reason == "hook 'secrets' asked for confirmation; no one can confirm it in this session"


@pytest.mark.asyncio
async def test_an_operation_ended_while_its_ask_waits_is_invalidated_not_blocked(
    hooks_dir: Path, bus: EventBus, confirmations: HookConfirmationService
) -> None:
    frontend = await _frontend(bus)
    manager = _manager(hooks_dir, _hook("guard", {"action": "ask"}))
    operation = _operation()

    task = asyncio.create_task(_check(manager, operation, confirmations))
    await wait_for(lambda: frontend.claimed, timeout=ENGINE_TURN_TIMEOUT, description="ask shown")
    operation.invalidate("turn_ended")
    decision = await asyncio.wait_for(task, timeout=ENGINE_TURN_TIMEOUT)

    assert (decision.invalidated, decision.blocked) == (True, False)
    assert [w.outcome for w in frontend.withdrawn] == ["invalidated"]


# ── what an ask applies to ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_an_ask_on_a_non_gating_event_or_from_a_non_blocking_hook_is_ignored(
    hooks_dir: Path, bus: EventBus, confirmations: HookConfirmationService
) -> None:
    frontend = await _frontend(bus)
    log = HookRunLog()
    manager = _manager(
        hooks_dir,
        _hook("after", {"action": "ask"}, event=HookEvent.AFTER_TOOL_CALL),
        _hook("async-ask", {"action": "ask"}, mode="async"),
    )
    manager.record_runs(log, session_id=_SESSION)

    after = await manager.fire(HookEvent.AFTER_TOOL_CALL, {"tool": {"args": {}}})
    operation = _operation()
    before = await _check(manager, operation, confirmations)
    await manager.drain_turn()

    assert (after.blocked, before.blocked) == (False, False)
    assert operation.checked_revision == operation.revision
    assert frontend.requests == []
    applied = {log.detail(r.run_id).record.hook_id: log.detail(r.run_id).applied for r in log.records()}
    assert applied["after"].not_applied == "unsupported"
    assert applied["async-ask"].not_applied == "nonblocking"


@pytest.mark.asyncio
async def test_a_failed_hook_s_ask_is_not_taken(
    hooks_dir: Path, bus: EventBus, confirmations: HookConfirmationService
) -> None:
    frontend = await _frontend(bus)
    script = _result_script({"action": "ask"}) + "\nraise SystemExit(3)\n"
    manager = _manager(hooks_dir, _hook("broken", script=script))

    decision = await _check(manager, _operation(), confirmations)

    assert decision.blocked is False
    assert frontend.requests == []


@pytest.mark.asyncio
async def test_the_payload_says_whether_confirmation_is_available(
    hooks_dir: Path, bus: EventBus, confirmations: HookConfirmationService
) -> None:
    await _frontend(bus)
    seen = hooks_dir / "seen"
    copy_payload = textwrap.dedent(
        f"""
        import json, os, sys
        mode = sys.argv[1]
        with open(os.environ["CHRYS_HOOK_PAYLOAD_FILE"]) as f:
            payload = json.load(f)
        with open({str(seen)!r} + "-" + mode, "w") as f:
            json.dump(payload.get("confirmation_available"), f)
        """
    )

    def copier(mode: str) -> HookConfig:
        hook = _hook(f"copy-{mode}", script=copy_payload, mode=mode)
        hook.run.argv.append(mode)
        return hook

    manager = _manager(hooks_dir, copier("blocking"), copier("async"))
    disabled = HookConfirmationService(bus=bus, enabled=False)
    await disabled.start()

    await _check(manager, _operation(), confirmations)
    await manager.drain_turn()
    blocking, nonblocking = (json.loads(Path(f"{seen}-{m}").read_text()) for m in ("blocking", "async"))
    await _check(manager, _operation(), disabled)
    await manager.drain_turn()
    blocking_headless = json.loads(Path(f"{seen}-blocking").read_text())

    assert (blocking, nonblocking, blocking_headless) == (True, False, False)


@pytest.mark.asyncio
async def test_the_payload_says_whether_extra_context_reaches_the_model(
    hooks_dir: Path,
    bus: EventBus,
    confirmations: HookConfirmationService,
) -> None:
    """Unlike asking, a note needs nobody to answer: a blocking hook can add one even headless."""
    seen = hooks_dir / "seen"
    copy_payload = textwrap.dedent(
        f"""
        import json, os, sys
        mode = sys.argv[1]
        with open(os.environ["CHRYS_HOOK_PAYLOAD_FILE"]) as f:
            payload = json.load(f)
        with open({str(seen)!r} + "-" + mode, "w") as f:
            json.dump(payload.get("extra_context_available"), f)
        """,
    )

    def copier(mode: str) -> HookConfig:
        hook = _hook(f"copy-{mode}", script=copy_payload, mode=mode)
        hook.run.argv.append(mode)
        return hook

    manager = _manager(hooks_dir, copier("blocking"), copier("async"))
    disabled = HookConfirmationService(bus=bus, enabled=False)
    await disabled.start()

    await _check(manager, _operation(), disabled)
    await manager.drain_turn()
    blocking, nonblocking = (json.loads(Path(f"{seen}-{m}").read_text()) for m in ("blocking", "async"))

    assert (blocking, nonblocking) == (True, False)


# ── an argument change after an approval ─────────────────────────────


@pytest.mark.asyncio
async def test_a_change_after_an_approval_asks_again_about_the_new_version(
    hooks_dir: Path, bus: EventBus, confirmations: HookConfirmationService
) -> None:
    frontend = await _frontend(bus, answer_inline="approved")
    manager = _manager(
        hooks_dir,
        _hook("guard", {"action": "ask"}),
        _hook("normalize", {"action": "modify", "args_override": {"path": "y"}}, path="x"),
    )
    operation = _operation("x")

    decision = await _check(manager, operation, confirmations)

    assert decision.blocked is False
    assert [r.arguments for r in frontend.requests] == [{"path": "x"}, {"path": "y"}]
    assert [r.revision for r in frontend.requests] == [1, 2]
    # The second question says it is asked again, and what changed since the user continued.
    assert [(r.asked_again, r.changed_arguments) for r in frontend.requests] == [
        (False, ()),
        (True, (HookArgumentChange(name="path", before='"x"', after='"y"'),)),
    ]
    assert operation.arguments() == {"path": "y"}
    assert operation.checked_revision == operation.revision == 2
    assert operation.commit_arguments({"path": "x"}) == "changed"
    assert operation.commit_arguments({"path": "y"}) is None


@pytest.mark.asyncio
async def test_a_recheck_still_rechecks_a_later_change_after_passing_the_asking_hook(
    hooks_dir: Path, bus: EventBus, confirmations: HookConfirmationService
) -> None:
    # A asks about x; B turns x into y; A passes y without asking; C turns y into z.
    # z was never asked about: D, before C in the file, asks about z only in a third pass.
    frontend = await _frontend(bus, answer_inline="approved")
    manager = _manager(
        hooks_dir,
        _hook("A", {"action": "ask"}, path="x"),
        _hook("D", {"action": "ask"}, path="z"),
        _hook("B", {"action": "modify", "args_override": {"path": "y"}}, path="x"),
        _hook("C", {"action": "modify", "args_override": {"path": "z"}}, path="y"),
    )
    operation = _operation("x")

    decision = await _check(manager, operation, confirmations)

    assert decision.blocked is False
    assert [(r.hook_id, r.arguments["path"]) for r in frontend.requests] == [("A", "x"), ("D", "z")]
    assert operation.arguments() == {"path": "z"}
    assert operation.checked_revision == operation.revision


@pytest.mark.asyncio
async def test_without_an_approval_a_change_only_reaches_the_later_hooks(
    hooks_dir: Path, bus: EventBus, confirmations: HookConfirmationService
) -> None:
    runs = hooks_dir / "first-runs"
    count_first = f"open({str(runs)!r}, 'a').write('.')"
    manager = _manager(
        hooks_dir,
        _hook("first", script=count_first),
        _hook("normalize", {"action": "modify", "args_override": {"path": "y"}}),
    )
    operation = _operation("x")

    decision = await _check(manager, operation, confirmations)

    assert decision.blocked is False
    assert runs.read_text() == "."
    assert operation.arguments() == {"path": "y"}
    assert operation.checked_revision == operation.revision == 2


@pytest.mark.asyncio
async def test_hooks_that_keep_changing_the_arguments_after_an_approval_are_stopped(
    hooks_dir: Path, bus: EventBus, confirmations: HookConfirmationService
) -> None:
    frontend = await _frontend(bus, answer_inline="approved")
    manager = _manager(
        hooks_dir,
        _hook("guard", {"action": "ask"}),
        _hook("to-b", {"action": "modify", "args_override": {"path": "b"}}, path="a"),
        _hook("to-a", {"action": "modify", "args_override": {"path": "a"}}, path="b"),
    )
    operation = _operation("a")

    decision = await _check(manager, operation, confirmations)

    assert decision.blocked is True
    assert "kept changing the arguments" in decision.block_reason
    assert operation.checked_revision is None
    assert len(frontend.requests) <= 4


# ── the same check for every entry ───────────────────────────────────


@pytest.mark.asyncio
async def test_an_empty_hook_chain_checks_the_version_the_same_way(hooks_dir: Path) -> None:
    no_gating_hooks = _manager(hooks_dir, _hook("after", event=HookEvent.AFTER_TOOL_CALL))
    unmatched = _manager(hooks_dir, _hook("only-z", {"action": "block"}, path="z"))
    for manager in (None, _manager(hooks_dir), no_gating_hooks, unmatched):
        operation = _operation()
        decision = await _check(manager, operation, None)
        assert (decision.blocked, decision.invalidated) == (False, False)
        assert operation.commit_arguments({"path": "x"}) is None


@pytest.mark.asyncio
async def test_a_closed_manager_with_hooks_for_the_event_vouches_for_nothing(hooks_dir: Path) -> None:
    manager = _manager(hooks_dir, _hook("guard", {"action": "allow"}))
    await manager.drain_session(close=True)
    operation = _operation()

    decision = await _check(manager, operation, None)

    assert decision.invalidated is True
    assert (operation.state, operation.end_reason) == ("invalidated", "hooks_closed")


@pytest.mark.asyncio
async def test_an_input_operation_goes_through_the_same_check(
    hooks_dir: Path, bus: EventBus, confirmations: HookConfirmationService
) -> None:
    frontend = await _frontend(bus, answer_inline="approved")
    manager = _manager(
        hooks_dir, _hook("secrets", {"action": "ask", "reason": "key"}, event=HookEvent.USER_PROMPT_SUBMIT)
    )
    operation = HookOperation(subject="input", session_id=_SESSION, input_text="deploy with sk-123")

    decision = await check_operation(
        _asking(manager, confirmations),
        HookEvent.USER_PROMPT_SUBMIT,
        operation,
        lambda: {"session_id": _SESSION, "text": operation.input_text},
    )

    assert decision.blocked is False
    assert [(r.subject, r.input_text) for r in frontend.requests] == [("input", "deploy with sk-123")]
    assert operation.commit_input("deploy with sk-123") is None
    assert operation.commit_input("deploy with sk-123") == "committed"


# ── waiting for the user costs no hook resources ─────────────────────


@pytest.mark.asyncio
async def test_waiting_for_the_user_holds_no_slot_and_no_script_timeout(
    hooks_dir: Path, bus: EventBus, confirmations: HookConfirmationService
) -> None:
    frontend = await _frontend(bus)
    manager = _manager(
        hooks_dir,
        _hook("guard", {"action": "ask"}, timeout=0.5),
        _hook("other", {"action": "allow"}, event=HookEvent.USER_PROMPT_SUBMIT),
        max_parallel_hooks=1,
    )
    operation = _operation()

    task = asyncio.create_task(_check(manager, operation, confirmations))
    await wait_for(lambda: frontend.claimed, timeout=ENGINE_TURN_TIMEOUT, description="ask shown")
    # The only slot is free while the user thinks: another hook runs to its end.
    other = HookOperation(subject="input", session_id=_SESSION, input_text="hi")
    await asyncio.wait_for(
        check_operation(manager, HookEvent.USER_PROMPT_SUBMIT, other, lambda: {"text": "hi"}),
        timeout=ENGINE_TURN_TIMEOUT,
    )
    assert other.checked_revision == other.revision
    # Longer than the hook's own timeout, and still the user's answer decides.
    await asyncio.sleep(0.8)
    assert not task.done()
    await frontend.answer(frontend.claimed[0], "approved")
    decision = await asyncio.wait_for(task, timeout=ENGINE_TURN_TIMEOUT)

    assert decision.blocked is False


@pytest.mark.asyncio
async def test_the_run_log_records_the_ask_and_what_came_of_it(
    hooks_dir: Path, bus: EventBus, confirmations: HookConfirmationService
) -> None:
    frontend = await _frontend(bus)
    log = HookRunLog()
    manager = _manager(hooks_dir, _hook("guard", {"action": "ask"}))
    manager.record_runs(log, session_id=_SESSION)

    task = asyncio.create_task(_check(manager, _operation(), confirmations))
    await wait_for(lambda: frontend.claimed, timeout=ENGINE_TURN_TIMEOUT, description="ask shown")
    (record,) = log.records()
    assert (record.state, record.effect, record.confirmation) == ("ended", "asked", "pending")
    await frontend.answer(frontend.claimed[0], "declined")
    await asyncio.wait_for(task, timeout=ENGINE_TURN_TIMEOUT)

    (record,) = log.records()
    assert record.confirmation == "declined"
    assert frontend.requests[0].run_id == record.run_id


@pytest.mark.asyncio
async def test_a_session_drain_ends_an_ask_still_waiting(
    hooks_dir: Path, bus: EventBus, confirmations: HookConfirmationService
) -> None:
    frontend = await _frontend(bus)
    manager = _manager(hooks_dir, _hook("guard", {"action": "ask"}))
    operation = _operation()

    task = asyncio.create_task(_check(manager, operation, confirmations))
    await wait_for(lambda: frontend.claimed, timeout=ENGINE_TURN_TIMEOUT, description="ask shown")
    await asyncio.wait_for(manager.drain_session(close=True), timeout=ENGINE_TURN_TIMEOUT)
    decision = await asyncio.wait_for(task, timeout=ENGINE_TURN_TIMEOUT)

    assert (decision.invalidated, decision.blocked) == (True, False)
    assert (operation.state, operation.end_reason) == ("invalidated", "hooks_closed")
    assert [w.outcome for w in frontend.withdrawn] == ["invalidated"]


@pytest.mark.asyncio
async def test_rechecks_stop_after_three_even_when_every_version_is_new(
    hooks_dir: Path, bus: EventBus, confirmations: HookConfirmationService
) -> None:
    # Every pass appends to the path: no version repeats, so only the bound can stop it.
    grow = textwrap.dedent(
        """
        import json, os
        with open(os.environ["CHRYS_HOOK_PAYLOAD_FILE"]) as f:
            path = json.load(f)["tool"]["args"]["path"]
        with open(os.environ["CHRYS_HOOK_RESULT"], "w") as f:
            json.dump({"action": "modify", "args_override": {"path": path + "+"}}, f)
        """
    )
    frontend = await _frontend(bus, answer_inline="approved")
    manager = _manager(hooks_dir, _hook("guard", {"action": "ask"}), _hook("grow", script=grow))
    operation = _operation("x")

    decision = await _check(manager, operation, confirmations)

    assert decision.blocked is True
    assert "stopped after 3 rechecks" in decision.block_reason
    # The first pass and three rechecks each asked about their own version.
    assert [r.arguments["path"] for r in frontend.requests] == ["x", "x+", "x++", "x+++"]


@pytest.mark.asyncio
async def test_the_run_whose_change_stopped_the_rechecks_says_the_call_was_rejected(
    hooks_dir: Path, bus: EventBus, confirmations: HookConfirmationService
) -> None:
    await _frontend(bus, answer_inline="approved")
    log = HookRunLog()
    manager = _manager(
        hooks_dir,
        _hook("guard", {"action": "ask"}),
        _hook("flip", {"action": "modify", "args_override": {"path": "y"}}, path="x"),
        _hook("flop", {"action": "modify", "args_override": {"path": "x"}}, path="y"),
    )
    manager.record_runs(log, session_id=_SESSION)

    decision = await _check(manager, _operation("x"), confirmations)

    assert decision.blocked is True
    # x → y → x repeats a version: stopped at the run whose change made it, and only that run says so.
    rejected = [(r.hook_id, r.call_rejected) for r in log.records() if r.hook_id != "guard"]
    assert rejected == [("flip", False), ("flop", True)]


@pytest.mark.asyncio
async def test_a_drain_while_non_blocking_hooks_start_ends_the_operation(hooks_dir: Path) -> None:
    manager = _manager(hooks_dir, _hook("watch", {"action": "allow"}, mode="async"))
    starting = asyncio.Event()
    release = asyncio.Event()

    async def held_report(_manager: HookManager) -> None:
        # Reached while the async hook is being started, before the dispatch returns.
        starting.set()
        await release.wait()

    manager.on_change = held_report
    operation = _operation()

    task = asyncio.create_task(_check(manager, operation, None))
    await wait_for(lambda: starting.is_set() or task.done(), description="the async hook starting")
    if task.done():
        await task
    assert starting.is_set()
    drain = asyncio.create_task(manager.drain_session(close=False))
    await asyncio.sleep(0)
    release.set()
    decision = await asyncio.wait_for(task, timeout=ENGINE_TURN_TIMEOUT)
    await asyncio.wait_for(drain, timeout=ENGINE_TURN_TIMEOUT)

    assert decision.invalidated is True
    assert operation.state == "invalidated"
    assert operation.commit_arguments({"path": "x"}) == "invalidated"


@pytest.mark.asyncio
async def test_an_operation_withdrawn_after_the_user_continued_runs_no_later_hook(
    hooks_dir: Path, tmp_path: Path, bus: EventBus, confirmations: HookConfirmationService
) -> None:
    frontend = await _frontend(bus, answer_inline="approved")
    ran = tmp_path / "ran"
    touch = f"open({str(ran)!r}, 'a').write('.')"
    manager = _manager(
        hooks_dir,
        _hook("guard", {"action": "ask"}),
        _hook("later", script=touch),
        _hook("watch", script=touch, mode="async"),
    )
    operation = _operation()

    async def withdraw_once_answered() -> None:
        # The answer is recorded before the next hook would start: the turn ends right then.
        if frontend.requests:
            operation.invalidate("turn_ended")

    manager.record_runs(HookRunLog(), session_id=_SESSION, on_change=withdraw_once_answered)

    decision = await _check(manager, operation, confirmations)

    assert decision.invalidated is True
    assert operation.state == "invalidated"
    assert not ran.exists()


@pytest.mark.asyncio
async def test_a_recheck_back_to_a_checked_version_says_so(
    hooks_dir: Path, bus: EventBus, confirmations: HookConfirmationService
) -> None:
    await _frontend(bus, answer_inline="approved")
    manager = _manager(
        hooks_dir,
        _hook("guard", {"action": "ask"}),
        _hook("flip", {"action": "modify", "args_override": {"path": "y"}}, path="x"),
        _hook("flop", {"action": "modify", "args_override": {"path": "x"}}, path="y"),
    )

    decision = await _check(manager, _operation("x"), confirmations)

    assert decision.blocked is True
    assert "0 rechecks" not in decision.block_reason
    assert "changed back to arguments already checked" in decision.block_reason


@pytest.mark.asyncio
async def test_a_blocking_hook_withdrawn_while_it_queues_never_starts(hooks_dir: Path, tmp_path: Path) -> None:
    ran = tmp_path / "ran"
    manager = _manager(hooks_dir, _hook("guard", script=f"open({str(ran)!r}, 'a').write('.')"))
    operation = _operation()
    log = HookRunLog()

    async def withdraw_once_queued() -> None:
        # Reported while the run queues and takes its slot, before its script would start.
        if log.records():
            operation.invalidate("turn_ended")

    manager.record_runs(log, session_id=_SESSION, on_change=withdraw_once_queued)

    decision = await _check(manager, operation, None)

    assert decision.invalidated is True
    assert not ran.exists()
    (record,) = log.records()
    assert (record.state, record.outcome) == ("ended", "cancelled")


@pytest.mark.asyncio
async def test_a_withdrawal_while_one_async_hook_starts_starts_no_later_one(hooks_dir: Path, tmp_path: Path) -> None:
    first, second = tmp_path / "first", tmp_path / "second"
    manager = _manager(
        hooks_dir,
        _hook("first", script=f"open({str(first)!r}, 'a').write('.')", mode="async"),
        _hook("second", script=f"open({str(second)!r}, 'a').write('.')", mode="async"),
    )
    operation = _operation()
    log = HookRunLog()

    async def withdraw_once_started(_manager: HookManager) -> None:
        # Reported once the first is started, while the dispatch has yet to start the second.
        operation.invalidate("turn_ended")

    manager.on_change = withdraw_once_started
    manager.record_runs(log, session_id=_SESSION)

    decision = await _check(manager, operation, None)
    await asyncio.wait_for(manager.drain_session(close=True), timeout=ENGINE_TURN_TIMEOUT)

    assert decision.invalidated is True
    assert [r.hook_id for r in log.records()] == ["first"]
    assert first.exists() and not second.exists()


class _OnHookStartSink(FakeSink):
    """Runs *on_start* as a hook's start marker is written: between its durable job and its start."""

    def __init__(self, on_start: Callable[[], Awaitable[None]]) -> None:
        super().__init__()
        self._on_start = on_start

    async def emit(self, draft, *, payload_factory=None):  # type: ignore[no-untyped-def]
        if draft.event_type == EventType.HOOK_OPERATION_STARTED:
            await self._on_start()
        return await super().emit(draft, payload_factory=payload_factory)


def _outbox_jobs(hooks_dir: Path, state: str) -> list[Path]:
    return list((hooks_dir / "outbox" / state).glob("*.json"))


@pytest.mark.parametrize(
    ("ending", "marker_fails", "kept"),
    [
        # The operation withdrawn: it never happens, so its job is not left to recover...
        ("withdrawn", False, False),
        # ...also when writing the start marker is then interrupted...
        ("withdrawn", True, False),
        # ...or hooks close after it: the withdrawal came first and stands.
        ("withdrawn_then_closed", False, False),
        # Hooks closing (a drain, a shutdown) keep the promise: recovery runs it later.
        ("hooks_closed", False, True),
        # An interrupted marker alone, as before: the job stays for recovery.
        ("none", True, True),
    ],
)
@pytest.mark.asyncio
async def test_a_durable_job_written_for_an_operation_that_ends_before_its_hook_starts(
    hooks_dir: Path, ending: str, marker_fails: bool, kept: bool
) -> None:
    hook = HookConfig(
        id="audit",
        event=HookEvent.BEFORE_TOOL_CALL,
        run=HookRun(type="command", argv=[sys.executable, "-c", "pass"]),
        execution=HookExecution(mode="async", delivery="durable", timeout_seconds=10.0),
    )
    manager = _manager(hooks_dir, hook)
    operation = _operation()

    async def on_start() -> None:
        assert len(_outbox_jobs(hooks_dir, "pending")) == 1
        if ending in ("withdrawn", "withdrawn_then_closed"):
            operation.invalidate("turn_ended")
        if ending == "withdrawn_then_closed" or ending == "hooks_closed":
            await manager.drain_session(close=True)
        if marker_fails:
            # Interrupted while the marker awaits its write (a failed write alone is only logged).
            raise asyncio.CancelledError

    with trajectory_scope(make_context(_OnHookStartSink(on_start))):
        if marker_fails:
            with pytest.raises(asyncio.CancelledError):
                await _check(manager, operation, None)
        else:
            decision = await _check(manager, operation, None)
            assert decision.blocked is False
    await manager.drain_session(close=True)

    assert operation.state == ("pending" if ending == "none" else "invalidated")
    assert len(_outbox_jobs(hooks_dir, "pending")) == (1 if kept else 0)
    assert len(_outbox_jobs(hooks_dir, "failed")) == (0 if kept else 1)
    assert _outbox_jobs(hooks_dir, "done") == []


# ---------------------------------------------------------------------------
# A recheck inside one dispatch reports itself as one
# ---------------------------------------------------------------------------


def _recording_operation_script(out: Path, result: dict[str, Any] | None = None) -> str:
    """A hook that appends the ``operation`` it was handed, then answers ``result`` (pass when None)."""
    answer = (
        textwrap.dedent(
            f"""
        with open(os.environ["CHRYS_HOOK_RESULT"], "w", encoding="utf-8") as f:
            json.dump({result!r}, f)
        """,
        )
        if result is not None
        else ""
    )
    return (
        textwrap.dedent(
            f"""
        import json, os
        with open(os.environ["CHRYS_HOOK_PAYLOAD_FILE"], encoding="utf-8") as f:
            payload = json.load(f)
        with open(r"{out}", "a", encoding="utf-8") as f:
            f.write(json.dumps(payload.get("operation")) + "\\n")
        """,
        )
        + answer
    )


@pytest.mark.asyncio
async def test_a_recheck_tells_the_hooks_it_is_a_later_check(
    hooks_dir: Path,
    bus: EventBus,
    confirmations: HookConfirmationService,
) -> None:
    """A hook re-run after an approved change must see the new check number, not the first one.

    ``ask`` about x, then ``normalize`` turns x into y. The change ends the pass
    and the chain starts over, so ``normalize``'s second run is a check of the
    operation's second version: a hook that de-duplicates by ``check`` would skip
    it if the payload still said ``check: 1``.
    """
    seen = hooks_dir / "seen.jsonl"
    await _frontend(bus, answer_inline="approved")
    manager = _manager(
        hooks_dir,
        _hook("guard", {"action": "ask"}),
        _hook(
            "normalize",
            path="x",
            script=_recording_operation_script(seen, {"action": "modify", "args_override": {"path": "y"}}),
        ),
        _hook("watch", script=_recording_operation_script(seen)),
    )
    operation = _operation("x")

    decision = await _check(manager, operation, confirmations)

    assert decision.blocked is False
    rows = [json.loads(line) for line in seen.read_text(encoding="utf-8").splitlines()]
    # Two passes over the chain: the first checks x, the second checks y.
    watch_checks = [row["check"] for row in rows if row is not None]
    assert 1 in watch_checks, "the first pass must report the first check"
    assert max(watch_checks) > 1, f"the recheck must report a later check, got {watch_checks}"
    assert len({row["id"] for row in rows}) == 1, "every pass names the same operation"
    assert operation.check_count > 1
