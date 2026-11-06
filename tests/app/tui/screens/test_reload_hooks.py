# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""``/reload`` comparing hooks before replacing them, from the real TUI through real hook processes."""

from __future__ import annotations

import json
import os
import sys
import textwrap
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
import yaml

from chrys.foundation.config.settings import Settings
from chrys.foundation.events import types as ev
from chrys.foundation.events.bus import EventBus
from chrys.foundation.i18n.formatting import format_message
from chrys.foundation.platform import get_platform
from chrys.orchestration import session_hooks
from chrys.orchestration.engine import side_questions
from chrys.orchestration.session_hooks import SessionHookFactory, hooks_unchanged, read_hooks_strict
from chrys.service.llm.mock import MockResponse
from chrys.service.profiles.agents.schema import ApprovalConfig, ToolsConfig
from chrys.service.profiles.models.env_bridge import activate_model_profile
from tests.app.tui.screens.main.test_btw import SideAnswer
from tests.app.tui.screens.test_manual_reload import ask_after_reload, settings_path, slash_reload
from tests.app.tui.screens.test_prompt_shortcut_send import (
    ALPHA,
    MAIN_MODEL,
    Session,
    make_agent,
    notices,
    running_session,
)
from tests.support.symlinks import junction_or_skip, symlink_or_skip
from tests.support.waiting import ENGINE_TURN_TIMEOUT, wait_for, wait_until

pytestmark = pytest.mark.usefixtures("clear_model_profile_env")


def settings_with_trust(trusted: bool) -> str:
    """The user's settings file, which also holds the model they selected."""
    return f"model.profile.active: {MAIN_MODEL}\nproject.hooks_enabled: {str(trusted).lower()}\n"


def global_hooks() -> Path:
    return get_platform().config_dir / "hooks" / "hooks.json"


def project_hooks() -> Path:
    return Path.cwd() / ".chrys" / "hooks" / "hooks.json"


def write_hooks(path: Path, *entries: dict, settings: dict | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    document: dict = {"version": 1, "hooks": list(entries)}
    if settings is not None:
        document["settings"] = settings
    path.write_text(json.dumps(document), encoding="utf-8")


def script(body: str) -> list[str]:
    return [sys.executable, "-c", textwrap.dedent(body)]


def blocking(hook_id: str, event: str, argv: list[str], **fields) -> dict:
    return {
        "id": hook_id,
        "event": event,
        "run": {"type": "command", "argv": argv},
        "execution": {"mode": "blocking", "timeout_seconds": 30},
        **fields,
    }


def blocks_reading(hook_id: str = "guard", *, match: dict | None = None, **fields) -> dict:
    """Refuses every ``read_file`` call, in the words the test looks for."""
    answer = {"action": "block", "reason": f"blocked by {hook_id}"}
    argv = script(f"""
        import json, os
        with open(os.environ["CHRYS_HOOK_RESULT"], "w") as f:
            json.dump({answer!r}, f)
        """)
    return blocking(hook_id, "before_tool_call", argv, match=match or {"tool_name": "read_file"}, **fields)


def waits_for(entered: Path, release: Path) -> list[str]:
    """Signals it started, and ends once the test lets it."""
    return script(f"""
        import pathlib, time
        pathlib.Path({str(entered)!r}).write_text("x")
        while not pathlib.Path({str(release)!r}).exists():
            time.sleep(0.02)
        """)


def reading_agent():
    profile = make_agent(ALPHA)
    profile.tools = ToolsConfig(builtins=["filesystem.read"])
    profile.approval = ApprovalConfig(default="auto", overrides={})
    return profile


@asynccontextmanager
async def hook_session(agent_engine, tmp_path, monkeypatch, *, trusted: bool = False):
    if trusted:
        settings_path().write_text(settings_with_trust(True), encoding="utf-8")
    activate_model_profile(MAIN_MODEL)
    settings = Settings(model_profile=MAIN_MODEL, workspace_change_notice=False, project_hooks_enabled=trusted)
    async with running_session(
        agent_engine, tmp_path, monkeypatch, agents=[reading_agent()], settings=settings
    ) as session:
        yield session


async def reloaded(session: Session) -> list[str]:
    before = len(session.seen.of(ev.SettingsReloaded))
    session.bar.focus_input()
    await slash_reload(session)
    await wait_for(
        lambda: len(session.seen.of(ev.SettingsReloaded)) > before and session.app.screen is session.main,
        pilot=session.pilot,
        timeout=ENGINE_TURN_TIMEOUT,
        description="manual reload completed",
    )
    return [format_message(line) for line in session.seen.of(ev.SettingsReloaded)[-1].report]


async def read_attempt(session: Session, tmp_path: Path) -> str:
    """Make the model call ``read_file`` for real and return what the call answered."""
    target = tmp_path / "target.txt"
    target.write_text("the file's content", encoding="utf-8")
    session.model.scripted = [
        MockResponse(tool_calls=[("read_file", "call-read", {"path": str(target)})]),
        MockResponse(text="Done."),
    ]
    await ask_after_reload(session, "read the file")
    messages, _options = session.model.call_history[-1]
    results = [content for message in messages for content in message.contents if content.type == "function_result"]
    return str(results[-1].result)


def manager(session: Session):
    return session.engine.session.hook_manager


def count_drains(monkeypatch, hook_manager) -> list[None]:
    drains: list[None] = []
    original = hook_manager.drain_session

    async def counted(**kwargs):
        drains.append(None)
        await original(**kwargs)

    monkeypatch.setattr(hook_manager, "drain_session", counted)
    return drains


async def test_an_edited_rule_applies_to_the_next_event(agent_engine, tmp_path, monkeypatch):
    write_hooks(global_hooks(), blocks_reading("old-rule"))
    async with hook_session(agent_engine, tmp_path, monkeypatch) as session:
        assert "blocked by old-rule" in await read_attempt(session, tmp_path)
        write_hooks(global_hooks(), blocks_reading("new-rule"))

        await reloaded(session)

        assert "blocked by new-rule" in await read_attempt(session, tmp_path)


@pytest.mark.parametrize("source", ["global", "project"])
@pytest.mark.parametrize("damage", ["unparsable", "invalid-entry", "skipped-rule"])
async def test_a_damaged_file_keeps_the_rule_that_was_blocking(agent_engine, tmp_path, monkeypatch, source, damage):
    path = global_hooks() if source == "global" else project_hooks()
    write_hooks(path, blocks_reading("old-rule"))
    async with hook_session(agent_engine, tmp_path, monkeypatch, trusted=source == "project") as session:
        running = manager(session)
        drains = count_drains(monkeypatch, running)
        if damage == "unparsable":
            path.write_text("{not json", encoding="utf-8")
        elif damage == "invalid-entry":
            write_hooks(path, {"id": "broken", "event": "no_such_event", "run": {"type": "command", "argv": ["x"]}})
        else:
            bad_regex = blocks_reading("bad-regex", match={"tool_name": "read_file", "args": {"path": {"regex": "("}}})
            write_hooks(path, bad_regex)

        report = await reloaded(session)

        assert manager(session) is running and not drains
        (line,) = [line for line in report if "Hooks were not updated" in line]
        assert str(path) in line
        if damage == "skipped-rule":
            assert "bad-regex" in line
        assert "blocked by old-rule" in await read_attempt(session, tmp_path)


@pytest.mark.parametrize("link", ["file", "folder", "folder-now-a-file", "parent", "junction"])
async def test_a_link_whose_target_is_gone_is_not_a_deleted_file(agent_engine, tmp_path, monkeypatch, link):
    dotfiles = tmp_path / "dotfiles"
    dotfiles.mkdir()
    write_hooks(dotfiles / "hooks.json", blocks_reading("old-rule"))
    global_hooks().parent.parent.mkdir(parents=True, exist_ok=True)
    if link == "file":
        global_hooks().parent.mkdir()
        symlink_or_skip(global_hooks(), dotfiles / "hooks.json")
    elif link == "parent":
        shared = dotfiles / "hooks"
        shared.mkdir()
        (dotfiles / "hooks.json").rename(shared / "hooks.json")
        symlink_or_skip(project_hooks().parent.parent, dotfiles, target_is_directory=True)
    elif link == "junction":
        junction_or_skip(global_hooks().parent, dotfiles)
    else:
        symlink_or_skip(global_hooks().parent, dotfiles, target_is_directory=True)
    async with hook_session(agent_engine, tmp_path, monkeypatch, trusted=link == "parent") as session:
        running = manager(session)
        assert running is not None
        # As when the disk holding them is unmounted.
        dotfiles.rename(tmp_path / "elsewhere")
        if link == "folder-now-a-file":
            dotfiles.write_text("", encoding="utf-8")

        report = await reloaded(session)

        assert manager(session) is running
        assert [line for line in report if "Hooks were not updated" in line]
        # With its whole folder gone the hook itself cannot start, and its own on_error decides.
        if link == "file":
            assert "blocked by old-rule" in await read_attempt(session, tmp_path)


def test_a_config_folder_known_by_a_link_whose_target_is_gone_is_not_deleted(tmp_path, monkeypatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(session_hooks, "conventional_config_dir", lambda: home / "config")
    symlink_or_skip(home / "config", tmp_path / "unmounted", target_is_directory=True)

    read = read_hooks_strict(project_root=str(tmp_path), project_hooks_enabled=False)

    assert [problem.path for problem in read.problems] == [str(home / "config")]


async def test_a_parse_failure_reports_where_it_is_and_quotes_nothing_from_the_file(
    agent_engine, tmp_path, monkeypatch
):
    path = global_hooks().parent / "hooks.yaml"
    path.parent.mkdir(parents=True)
    path.write_text(yaml.safe_dump({"version": 1, "hooks": [blocks_reading("old-rule")]}), encoding="utf-8")
    async with hook_session(agent_engine, tmp_path, monkeypatch) as session:
        path.write_text('version: 1\nhooks:\n  - env:\n      API_TOKEN: "xoxb-secret-value\n', encoding="utf-8")

        report = await reloaded(session)

        (line,) = [line for line in report if "Hooks were not updated" in line]
        assert str(path) in line and "line " in line
        assert "xoxb-secret-value" not in line


def test_the_same_project_and_global_files_read_as_unchanged(tmp_path):
    project = tmp_path / "project"
    project_file = project / ".chrys" / "hooks" / "hooks.json"
    write_hooks(project_file, blocks_reading("project-rule"))
    write_hooks(global_hooks(), blocks_reading("global-rule"), settings={"shutdown_grace_seconds": 7})
    first = read_hooks_strict(project_root=str(project), project_hooks_enabled=True)
    running = SessionHookFactory(EventBus()).manager_for(first.merged)

    assert not first.problems
    assert hooks_unchanged(running, read_hooks_strict(project_root=str(project), project_hooks_enabled=True).merged)
    assert not hooks_unchanged(
        running, read_hooks_strict(project_root=str(project), project_hooks_enabled=False).merged
    )


async def test_the_same_content_keeps_the_manager_and_a_running_async_hook(agent_engine, tmp_path, monkeypatch):
    entered, release, finished = tmp_path / "entered", tmp_path / "release", tmp_path / "finished"
    body = waits_for(entered, release)
    body[-1] += f"\npathlib.Path({str(finished)!r}).write_text('x')\n"
    write_hooks(
        global_hooks(),
        {
            # Started by the session, so it outlives the turn and only a drain could end it.
            "id": "slow",
            "event": "session_start",
            "run": {"type": "command", "argv": body},
            "execution": {"mode": "async", "timeout_seconds": 30},
        },
        settings={"shutdown_grace_seconds": 0.1},
    )
    async with hook_session(agent_engine, tmp_path, monkeypatch) as session:
        running = manager(session)
        drains = count_drains(monkeypatch, running)
        await wait_for(
            entered.exists, pilot=session.pilot, timeout=ENGINE_TURN_TIMEOUT, description="async hook running"
        )

        await reloaded(session)

        assert manager(session) is running and not drains
        release.write_text("x", encoding="utf-8")
        await wait_for(
            finished.exists, pilot=session.pilot, timeout=ENGINE_TURN_TIMEOUT, description="async hook finished"
        )


async def test_turning_trust_on_with_a_bad_project_file_changes_nothing(agent_engine, tmp_path, monkeypatch):
    write_hooks(global_hooks(), blocks_reading("global-rule"))
    project_hooks().parent.mkdir(parents=True)
    project_hooks().write_text("{not json", encoding="utf-8")
    async with hook_session(agent_engine, tmp_path, monkeypatch) as session:
        running = manager(session)
        runtime = session.engine.current.require_loaded()
        settings_path().write_text(settings_with_trust(True), encoding="utf-8")
        session.bar.focus_input()
        await slash_reload(session)
        await wait_for(
            lambda: [e for e in session.seen.of(ev.Error) if e.code == "manual_settings_reload_failed"],
            pilot=session.pilot,
            timeout=ENGINE_TURN_TIMEOUT,
            description="the reload stopped",
        )
        await wait_for(
            lambda: any("with project hooks still not loaded" in message for _, message in notices(session.app)),
            pilot=session.pilot,
            description="the failure names what stays in effect",
        )
        assert not session.seen.of(ev.SettingsReloaded)
        assert manager(session) is running
        assert session.engine.current.require_loaded() is runtime
        assert session.engine.settings_handle.settings.project_hooks_enabled is False
        assert "blocked by global-rule" in await read_attempt(session, tmp_path)


async def test_turning_trust_off_replaces_the_project_rules_when_the_rest_is_valid(agent_engine, tmp_path, monkeypatch):
    write_hooks(project_hooks(), blocks_reading("project-rule"))
    async with hook_session(agent_engine, tmp_path, monkeypatch, trusted=True) as session:
        assert "blocked by project-rule" in await read_attempt(session, tmp_path)
        settings_path().write_text(settings_with_trust(False), encoding="utf-8")

        await reloaded(session)

        assert manager(session) is None
        assert "the file's content" in await read_attempt(session, tmp_path)


@pytest.mark.parametrize("change", ["deleted", "disabled"])
async def test_a_deleted_or_disabled_rule_stops_applying(agent_engine, tmp_path, monkeypatch, change):
    write_hooks(global_hooks(), blocks_reading("old-rule"))
    async with hook_session(agent_engine, tmp_path, monkeypatch) as session:
        old = manager(session)
        drains = count_drains(monkeypatch, old)
        if change == "deleted":
            global_hooks().unlink()
        else:
            write_hooks(global_hooks(), blocks_reading("old-rule", enabled=False))

        await reloaded(session)

        assert manager(session) is not old and len(drains) == 1
        assert (manager(session) is None) == (change == "deleted")
        assert "the file's content" in await read_attempt(session, tmp_path)


@pytest.mark.skipif(sys.platform == "win32" or os.geteuid() == 0, reason="needs a folder its owner cannot read")
async def test_an_unreadable_hooks_folder_is_not_a_deleted_one(agent_engine, tmp_path, monkeypatch):
    write_hooks(global_hooks(), blocks_reading("old-rule"))
    folder = global_hooks().parent
    async with hook_session(agent_engine, tmp_path, monkeypatch) as session:
        running = manager(session)
        folder.chmod(0)
        try:
            report = await reloaded(session)
        finally:
            folder.chmod(0o700)
        assert manager(session) is running
        assert [line for line in report if "Hooks were not updated" in line]


async def test_a_replacement_fires_no_session_start_and_redelivers_nothing(agent_engine, tmp_path, monkeypatch):
    log = tmp_path / "hook.log"
    appends = script(f"""
        import os
        with open({str(log)!r}, "a") as f:
            f.write(os.environ["CHRYS_HOOK_EVENT"] + "\\n")
        """)
    release = tmp_path / "release"
    holding = [*appends[:-1], appends[-1] + waits_for(tmp_path / "entered", release)[-1]]
    durable = {
        "id": "durable",
        "event": "after_turn",
        "run": {"type": "command", "argv": holding},
        "execution": {"mode": "fire_and_forget", "delivery": "durable", "timeout_seconds": 30},
    }
    started = blocking("started", "session_start", appends)
    # With no retry age, recovery would take the job still running for a dead process's.
    write_hooks(global_hooks(), started, durable, settings={"outbox_retry_age_seconds": 0})
    async with hook_session(agent_engine, tmp_path, monkeypatch) as session:
        await ask_after_reload(session, "one turn")
        lines = lambda: log.read_text(encoding="utf-8").splitlines() if log.exists() else []  # noqa: E731
        await wait_for(
            lambda: lines().count("after_turn") == 1,
            pilot=session.pilot,
            timeout=ENGINE_TURN_TIMEOUT,
            description="the durable hook delivered",
        )
        assert lines().count("session_start") == 1
        write_hooks(
            global_hooks(), started, durable, blocks_reading("new-rule"), settings={"outbox_retry_age_seconds": 0}
        )

        await reloaded(session)

        assert session.engine.session.outbox_recovery_task is None
        assert not await wait_until(
            lambda: lines().count("after_turn") > 1 or lines().count("session_start") > 1,
            pilot=session.pilot,
            timeout=2.0,
        )
        release.write_text("x", encoding="utf-8")


async def _send_side_question(session: Session, text: str) -> None:
    session.chat.load_text(f"/btw {text}")
    session.bar.submit_input()


async def _side_failures(session: Session) -> list:
    failures: list = []

    async def record(event: ev.InvocationSideQuestionFailed) -> None:
        failures.append(event)

    await session.bus.subscribe(ev.InvocationSideQuestionFailed, record)
    return failures


async def test_replacing_hooks_during_a_side_question_check_fails_only_that_question(
    agent_engine, tmp_path, monkeypatch
):
    side = SideAnswer()
    monkeypatch.setattr(side_questions, "answer_side_question", side)
    entered, release = tmp_path / "entered", tmp_path / "release"
    fast = {"shutdown_grace_seconds": 0.1}
    write_hooks(global_hooks(), blocking("check", "user_prompt_submit", waits_for(entered, release)), settings=fast)
    async with hook_session(agent_engine, tmp_path, monkeypatch) as session:
        failures = await _side_failures(session)
        await _send_side_question(session, "why?")
        await wait_for(
            entered.exists, pilot=session.pilot, timeout=ENGINE_TURN_TIMEOUT, description="the check is running"
        )
        write_hooks(global_hooks(), blocks_reading("new-rule"))

        await reloaded(session)

        release.write_text("x", encoding="utf-8")
        await wait_for(
            lambda: failures, pilot=session.pilot, timeout=ENGINE_TURN_TIMEOUT, description="the side question failed"
        )
        assert side.calls == []
        assert "blocked by new-rule" in await read_attempt(session, tmp_path)


async def test_a_side_question_past_its_check_finishes_across_a_replacement(agent_engine, tmp_path, monkeypatch):
    side = SideAnswer(answer="The side answer.")
    monkeypatch.setattr(side_questions, "answer_side_question", side)
    allow = script("""
        import json, os
        with open(os.environ["CHRYS_HOOK_RESULT"], "w") as f:
            json.dump({"action": "allow"}, f)
        """)
    write_hooks(global_hooks(), blocking("check", "user_prompt_submit", allow))
    async with hook_session(agent_engine, tmp_path, monkeypatch) as session:
        failures = await _side_failures(session)
        await _send_side_question(session, "why?")
        await wait_for(
            side.entered.is_set, pilot=session.pilot, timeout=ENGINE_TURN_TIMEOUT, description="check passed"
        )
        write_hooks(global_hooks(), blocks_reading("new-rule"))

        await reloaded(session)

        side.release.set()
        questions = session.engine._side_questions
        await wait_for(lambda: not questions._run_tasks, pilot=session.pilot, description="side question ended")
        assert not failures and [question for question, _context in side.calls] == ["why?"]


async def test_the_same_content_lets_a_running_side_question_check_finish(agent_engine, tmp_path, monkeypatch):
    side = SideAnswer(answer="The side answer.")
    monkeypatch.setattr(side_questions, "answer_side_question", side)
    entered, release = tmp_path / "entered", tmp_path / "release"
    write_hooks(global_hooks(), blocking("check", "user_prompt_submit", waits_for(entered, release)))
    async with hook_session(agent_engine, tmp_path, monkeypatch) as session:
        failures = await _side_failures(session)
        await _send_side_question(session, "why?")
        await wait_for(
            entered.exists, pilot=session.pilot, timeout=ENGINE_TURN_TIMEOUT, description="the check is running"
        )

        await reloaded(session)

        release.write_text("x", encoding="utf-8")
        await wait_for(
            side.entered.is_set, pilot=session.pilot, timeout=ENGINE_TURN_TIMEOUT, description="the check passed"
        )
        side.release.set()
        questions = session.engine._side_questions
        await wait_for(lambda: not questions._run_tasks, pilot=session.pilot, description="side question ended")
        assert not failures
