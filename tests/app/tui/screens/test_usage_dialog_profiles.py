# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The /usage dialog by model profile: the Profile group, and a model told apart by the profiles that served it."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from chrys.app.tui.screens.dialogs.usage import UsageDialog, _ChoiceButton
from chrys.app.tui.screens.main.screen import MainScreen
from chrys.foundation.trajectory.envelope import Actor
from chrys.foundation.trajectory.event_types import EventType, ExchangeOutcome
from chrys.service.analytics.usage_report import UsageReport
from chrys.service.profiles.models.registry import ModelProfileRegistry
from chrys.service.profiles.models.schema import ModelProfile
from tests.app.tui.screens.test_usage_dialog import (
    _TODAY,
    _call,
    _chosen,
    _headers,
    _HostApp,
    _open,
    _open_calendar,
    _report,
    _row_names,
    _text,
)
from tests.service.analytics._events import EventLog
from tests.support.tui_app_harness import make_chrys_app
from tests.support.tui_helpers import click_when_settled
from tests.support.waiting import wait_for

if TYPE_CHECKING:
    from pathlib import Path

_WORK = "a" * 32
_PERSONAL = "b" * 32
_DELETED = "c" * 32
_NAMES = {_WORK: "Work", _PERSONAL: "Personal"}


def _with_names(report: UsageReport) -> UsageReport:
    return UsageReport(
        calls=report.calls,
        today=report.today,
        new_tokens_by_day=report.new_tokens_by_day,
        new_tokens_by_hour=report.new_tokens_by_hour,
        profile_names=_NAMES,
    )


def _two_subscriptions() -> UsageReport:
    """model-a through both profiles and through none known; model-b through one."""
    return _with_names(
        _report(
            _call(profile=_WORK, new_input=8000),
            _call(profile=_PERSONAL, new_input=4000),
            _call(new_input=500),
            _call(model="model-b", profile=_WORK, new_input=6000),
        )
    )


async def test_the_profile_group_names_each_profile_as_it_is_called_now() -> None:
    async with _HostApp().run_test(size=(100, 30)) as pilot:
        dialog = await _open(pilot, _two_subscriptions())

        await click_when_settled(pilot, dialog.query_one("#usage-group-profile", _ChoiceButton))
        await wait_for(lambda: _chosen(dialog, "usage-groups") == "Profile", pilot=pilot, description="profile")

        assert _headers(dialog) == ["Profile", "", "New", "Input", "Output", "Hit", "Calls", "Failed"]
        assert _row_names(dialog) == ["Work", "Personal", "Other profiles"]


async def test_a_model_two_profiles_served_is_a_row_for_each() -> None:
    async with _HostApp().run_test(size=(100, 30)) as pilot:
        dialog = await _open(pilot, _two_subscriptions())

        assert _chosen(dialog, "usage-groups") == "Model"
        # Each profile's share keeps its own speed: the same model, compared across subscriptions.
        assert "TPS" in _headers(dialog)
        assert _row_names(dialog) == [
            "model-a · Work",
            "model-b",
            "model-a · Personal",
            "model-a · Other profiles",
        ]


async def test_the_calendar_names_the_profile_of_a_split_model() -> None:
    async with _HostApp().run_test(size=(120, 40)) as pilot:
        dialog = await _open_calendar(pilot, _two_subscriptions())

        detail = _text(dialog, "usage-calendar-detail")
        assert "model-a · Work" in detail
        assert "model-a · Personal" in detail


async def test_a_profile_name_shows_as_written_and_never_as_markup() -> None:
    report = _report(_call(profile=_WORK), _call(profile=_PERSONAL))
    names = {_WORK: "[red]x\x1b[31m", _PERSONAL: "[/oops]"}
    named = UsageReport(calls=report.calls, today=report.today, profile_names=names)
    async with _HostApp().run_test(size=(100, 30)) as pilot:
        dialog = await _open(pilot, named)

        await pilot.press("g", "g", "g")
        rows = _row_names(dialog)
        assert len(rows) == 2
        assert "[/oops]" in rows
        assert any(row.startswith("[red]x") for row in rows)
        assert not any("\x1b" in row for row in rows)


async def test_usage_opened_from_the_main_screen_names_the_profiles_the_app_knows(tmp_path: Path) -> None:
    """The names come from the app's model profiles; the log holds only their ids."""
    models = ModelProfileRegistry()
    for profile_id, name in _NAMES.items():
        models.register(ModelProfile(id=profile_id, name=name, provider="openai", model_id="model-a"))
    log = EventLog()
    ended = datetime.combine(_TODAY, datetime.min.time(), tzinfo=UTC).replace(hour=12).isoformat()
    for profile_id, new_input in ((_WORK, 900), (_PERSONAL, 300), (_DELETED, 100)):
        log.add(
            EventType.MODEL_EXCHANGE_FINISHED,
            log.next_sequence,
            actor=Actor(kind="agent", role="sub_agent"),
            occurred_at=ended,
            payload={
                "outcome": ExchangeOutcome.SUCCESS,
                "response_model": "model-a",
                "model_profile_id": profile_id,
                "usage": {"normalized": {"input_total": new_input, "output_total": 0}},
            },
        )
    path = tmp_path / "sessions" / "s1" / "trajectory" / "events.jsonl"
    path.parent.mkdir(parents=True)
    log.write(path)
    app = make_chrys_app(tmp_path / "sessions", model_registry=models)

    async with app.run_test(size=(120, 36)) as pilot:
        main_screen = app.screen
        assert isinstance(main_screen, MainScreen)
        main_screen._config_actions.open_usage()
        await wait_for(lambda: isinstance(app.screen, UsageDialog), pilot=pilot, description="usage open")
        dialog = app.screen
        assert isinstance(dialog, UsageDialog)
        await wait_for(lambda: not dialog._reading, pilot=pilot, description="usage read")
        # The calls ended at noon UTC on a fixed day: the whole history holds them in any time zone.
        await pilot.press("right", "right")
        await pilot.press("g", "g", "g")

        await wait_for(
            lambda: _row_names(dialog) == ["Work", "Personal", "Other profiles"],
            pilot=pilot,
            description="profiles by name",
        )
        await pilot.press("g")
        assert _row_names(dialog) == ["model-a · Work", "model-a · Personal", "model-a · Other profiles"]
