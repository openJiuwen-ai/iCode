# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""``/reload`` following theme and prompt shortcut files, over a populated transcript."""

from __future__ import annotations

from pathlib import Path

import pytest

from chrys.app.tui.app import ChrysApp
from chrys.app.tui.theme_loader import default_theme_directory
from chrys.app.tui.widgets.chat.messages import AgentMessage, UserMessage
from chrys.app.tui.widgets.chat.panel import ChatPanel
from chrys.app.tui.widgets.chrome.footer import ChrysFooter
from chrys.foundation.config.settings import DEFAULT_THEME, persist_theme
from chrys.foundation.events import types as ev
from chrys.service.profiles.models.env_bridge import activate_model_profile
from tests.app.tui.screens.test_manual_reload import reload_report, settings_path, slash_reload
from tests.app.tui.screens.test_prompt_shortcut_send import (
    MAIN_MODEL,
    Session,
    _shortcut_row,
    notices,
    running_session,
    type_text,
    write_shortcut,
)
from tests.app.tui.screens.test_reload_live_settings import edit_key
from tests.support.event_capture import capture_events
from tests.support.pilot_barrier import screen_is_settled
from tests.support.waiting import wait_for, wait_until, wait_until_quiet

pytestmark = pytest.mark.usefixtures("clear_model_profile_env")


@pytest.fixture(autouse=True)
def real_theme_writes(monkeypatch: pytest.MonkeyPatch, prevent_theme_preference_persistence: None) -> None:
    monkeypatch.setattr("chrys.app.tui.app.persist_theme", persist_theme)


def theme_file(name: str, primary: str = "#112233") -> Path:
    root = default_theme_directory()
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{name}.yaml"
    path.write_text(f"primary: '{primary}'\n", encoding="utf-8")
    return path


async def settled(session: Session) -> None:
    await wait_for(
        lambda: screen_is_settled(session.app, session.main), pilot=session.pilot, description="the screen laid out"
    )
    await wait_until_quiet(
        lambda: screen_is_settled(session.app, session.main), pilot=session.pilot, description="it stays laid out"
    )


async def populate(session: Session) -> None:
    panel = session.main.query_one(ChatPanel)
    transcript = []
    for index in range(24):
        transcript += [UserMessage(f"Question {index}"), AgentMessage(f"## Answer {index}\n\nA paragraph.")]
    await panel.mount(*transcript)
    await settled(session)


def count_repaints(app: ChrysApp, monkeypatch: pytest.MonkeyPatch) -> list[None]:
    repaints: list[None] = []
    real = app.refresh_css

    def counting(animate: bool = True) -> None:
        repaints.append(None)
        real(animate=animate)

    monkeypatch.setattr(app, "refresh_css", counting)
    return repaints


async def repainted(session: Session, repaints: list[None], count: int) -> None:
    """Exactly *count* style passes so far: the last one ran and no other follows it."""
    await wait_for(lambda: len(repaints) >= count, pilot=session.pilot, description=f"{count} style passes")
    assert not await wait_until(lambda: len(repaints) > count, timeout=0.2, pilot=session.pilot)


async def test_a_theme_added_edited_broken_deleted_and_restored_is_followed_with_one_repaint_each(
    agent_engine, tmp_path, monkeypatch
) -> None:
    path = settings_path()
    activate_model_profile(MAIN_MODEL)
    async with running_session(agent_engine, tmp_path, monkeypatch) as session:
        app = session.app
        await populate(session)
        repaints = count_repaints(app, monkeypatch)

        # Added, and named by the settings in the same edit.
        mine = theme_file("mine")
        edited = edit_key(path, "ui.theme", "mine")
        lines = await reload_report(session)
        assert app.theme == "mine"
        assert app.current_theme.primary == "#112233"
        assert "ui.theme" in lines["controls.reload_applied"]
        await repainted(session, repaints, 1)
        assert path.read_bytes() == edited

        # The same name, a new body.
        theme_file("mine", "#445566")
        await reload_report(session)
        assert app.theme == "mine"
        assert app.current_theme.primary == "#445566"
        await repainted(session, repaints, 2)

        # A theme nothing shows costs no style pass, new or edited.
        theme_file("other")
        await reload_report(session)
        theme_file("other", "#778899")
        await reload_report(session)
        assert app.get_theme("other").primary == "#778899"
        await repainted(session, repaints, 2)

        # Broken: the working one stays and the file is named.
        warnings = await capture_events(session.bus, ev.Warning)
        mine.write_text("primary: [\n", encoding="utf-8")
        await reload_report(session)
        await wait_for(
            lambda: [warning for warning in warnings if warning.code == "user_theme_skipped"],
            pilot=session.pilot,
            description="the bad file named",
        )
        assert app.theme == "mine"
        assert app.current_theme.primary == "#445566"
        await repainted(session, repaints, 2)

        # Deleted: the screen falls back, the setting survives.
        mine.unlink()
        lines = await reload_report(session)
        assert app.theme == DEFAULT_THEME
        assert "mine" not in app.available_themes
        assert app.settings_handle.settings.theme == "mine"
        assert "mine" in lines["controls.reload_unknown_theme"]
        await repainted(session, repaints, 3)
        assert path.read_bytes() == edited

        # Restored: the setting that survived brings it back.
        theme_file("mine", "#445566")
        await reload_report(session)
        assert app.theme == "mine"
        await repainted(session, repaints, 4)
        assert path.read_bytes() == edited


async def test_a_theme_the_styles_reject_is_never_shown_and_costs_no_style_pass(
    agent_engine, tmp_path, monkeypatch
) -> None:
    path = settings_path()
    activate_model_profile(MAIN_MODEL)
    async with running_session(agent_engine, tmp_path, monkeypatch) as session:
        app = session.app
        await populate(session)
        mine = theme_file("mine", "#445566")
        edit_key(path, "ui.theme", "mine")
        await reload_report(session)
        assert app.theme == "mine"
        repaints = count_repaints(app, monkeypatch)
        warnings = await capture_events(session.bus, ev.Warning)

        # The theme in use: it stays, with the body that worked.
        bad = "primary: '#778899'\nvariables:\n  footer-background: not-a-color\n"
        mine.write_text(bad, encoding="utf-8")
        await reload_report(session)
        await wait_for(
            lambda: [warning for warning in warnings if "mine" in warning.message],
            pilot=session.pilot,
            description="the rejected theme named",
        )
        assert app.theme == "mine"
        assert app.current_theme.primary == "#445566"
        await repainted(session, repaints, 0)

        # A reload with no further edit tries again and still shows nothing new.
        await reload_report(session)
        await repainted(session, repaints, 0)

        # Fixed: shown, once.
        theme_file("mine", "#778899")
        await reload_report(session)
        assert app.current_theme.primary == "#778899"
        await repainted(session, repaints, 1)

        # A new theme named by the settings, rejected: the screen keeps what it has.
        (default_theme_directory() / "worse.yaml").write_text(bad, encoding="utf-8")
        edited = edit_key(path, "ui.theme", "worse")
        await reload_report(session)
        assert app.theme == "mine"
        await repainted(session, repaints, 1)
        assert path.read_bytes() == edited


async def test_a_failed_reload_puts_no_theme_on_screen(agent_engine, tmp_path, monkeypatch) -> None:
    path = settings_path()
    activate_model_profile(MAIN_MODEL)
    async with running_session(agent_engine, tmp_path, monkeypatch) as session:
        theme_file("mine")
        path.write_text("ui: [broken", encoding="utf-8")
        await slash_reload(session)
        await wait_for(
            lambda: any(str(path) in message for _, message in notices(session.app)),
            pilot=session.pilot,
            description="the read failure shown",
        )
        assert "mine" not in session.app.available_themes


async def test_a_reload_with_no_edit_leaves_the_main_screen_and_the_draft_alone(
    agent_engine, tmp_path, monkeypatch
) -> None:
    settings_path()
    activate_model_profile(MAIN_MODEL)
    theme_file("mine")
    async with running_session(agent_engine, tmp_path, monkeypatch) as session:
        await populate(session)
        await type_text(session.pilot, "keep me")
        await wait_for(lambda: session.chat.has_focus, pilot=session.pilot, description="the composer has focus")
        await settled(session)
        repaints = count_repaints(session.app, monkeypatch)
        style_updates: list[bool] = []
        recomposes: list[None] = []

        async def record_recompose() -> None:
            recomposes.append(None)

        monkeypatch.setattr(session.main, "update_node_styles", lambda animate=True: style_updates.append(animate))
        monkeypatch.setattr(session.main.query_one(ChrysFooter), "recompose", record_recompose)

        session.main._start_manual_reload()
        await wait_for(
            lambda: session.seen.of(ev.SettingsReloaded), pilot=session.pilot, description="the reload finished"
        )
        await wait_for(
            lambda: not session.bar.agent_loading, pilot=session.pilot, description="the loading state ended"
        )
        await repainted(session, repaints, 0)

        assert style_updates == []
        assert recomposes == []
        assert session.bar.draft().text == "keep me"
        assert session.chat.has_focus


async def test_a_reload_is_refused_while_the_theme_editor_is_open(agent_engine, tmp_path, monkeypatch) -> None:
    settings_path()
    activate_model_profile(MAIN_MODEL)
    async with running_session(agent_engine, tmp_path, monkeypatch) as session:
        await session.main.manage_themes()
        await wait_for(
            lambda: session.main.theme_editor is not None, pilot=session.pilot, description="the editor opened"
        )
        session.main._start_manual_reload()
        await wait_for(
            lambda: any("theme editor" in message for _, message in notices(session.app)),
            pilot=session.pilot,
            description="the refusal shown",
        )
        assert not session.seen.of(ev.SettingsReload)


async def test_a_prompt_shortcut_added_by_hand_is_offered_after_a_reload(agent_engine, tmp_path, monkeypatch) -> None:
    settings_path()
    activate_model_profile(MAIN_MODEL)
    async with running_session(agent_engine, tmp_path, monkeypatch) as session:
        suggestions = session.main._suggestions
        await wait_for(lambda: suggestions._shortcuts() is not None, pilot=session.pilot, description="first read")
        write_shortcut("zzfresh", "fresh text")
        # Started directly: typing the ``/`` of ``/reload`` would itself read the folders again.
        session.main._start_manual_reload()
        await wait_for(
            lambda: session.seen.of(ev.SettingsReloaded), pilot=session.pilot, description="the reload finished"
        )
        await wait_for(
            lambda: "zzfresh" in [shortcut.name for shortcut in suggestions._shortcuts() or ()],
            pilot=session.pilot,
            description="the new shortcut read",
        )
        await wait_for(
            lambda: session.app.screen is session.main and not session.bar.agent_loading,
            pilot=session.pilot,
            description="ready",
        )
        session.bar.focus_input()
        await wait_for(lambda: session.chat.has_focus, pilot=session.pilot, description="the composer has focus")
        await type_text(session.pilot, "/zzfr")
        await wait_for(
            lambda: _shortcut_row(session.main, "zzfresh") is not None,
            pilot=session.pilot,
            description="the new shortcut offered",
        )
