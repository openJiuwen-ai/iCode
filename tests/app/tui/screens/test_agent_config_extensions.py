# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The Tools tab's extension switches."""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

import pytest
from textual.widgets import Button, Checkbox, Label

from chrys.app.tui.screens.agents.panels.tools import ToolsConfigPanel
from chrys.foundation.platform import get_platform
from chrys.service import extensions
from chrys.service.extensions import ExtensionCatalog, ExtensionSource, user_extensions_dir
from chrys.service.profiles.agents.loader import load_profile_from_yaml
from chrys.service.profiles.agents.schema import ToolsConfig
from tests.app.tui.screens._agent_config_support import (
    _DEFAULT_WAIT_TIMEOUT,
    _activate_agent_config_tab,
    _wait_for_hydrated,
    make_profile,
    open_agent_config,
    registry_with,
)
from tests.support.extensions import EXAMPLE_EXTENSION_ID, install_distribution_extension, install_file_extension
from tests.support.tui_helpers import WidgetApp
from tests.support.waiting import wait_for

if TYPE_CHECKING:
    from pathlib import Path

    from textual.dom import DOMNode
    from textual.pilot import Pilot

# As in test_agent_config_panels.py: a first panel mount can be slow, and the
# waits below keep their own 45 s ceiling so a stall fails cleanly first.
pytestmark = [pytest.mark.timeout(120)]

_EXPLODING = '"""Continuous integration status."""\nraise SystemExit("ran while listing")\n'


def _switches(root: DOMNode) -> dict[str, Checkbox]:
    return {switch.label.plain: switch for switch in root.query_one("#tc-extensions").query(Checkbox).results(Checkbox)}


def _notes(root: DOMNode) -> list[str]:
    return [
        str(label.render()) for label in root.query_one("#tc-extensions").query(Label).results(Label) if label.display
    ]


async def _listed(root: DOMNode, pilot: Pilot, count: int) -> dict[str, Checkbox]:
    await wait_for(
        lambda: len(_switches(root)) == count,
        pilot=pilot,
        timeout=_DEFAULT_WAIT_TIMEOUT,
        description=f"{count} extension switches",
    )
    return _switches(root)


async def test_installed_extensions_are_listed_without_running_them_and_saved_in_order(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    release = install_file_extension(monkeypatch)
    ci = install_file_extension(monkeypatch, "acme_ci", _EXPLODING)
    twice = install_file_extension(monkeypatch, "acme_twice")
    install_distribution_extension(monkeypatch, tmp_path / "site", "acme_twice", module="acme_twice_module")
    (user_extensions_dir() / "Bad-Id.py").write_text("", encoding="utf-8")
    app = WidgetApp(lambda: ToolsConfigPanel(ToolsConfig(), extensions=["acme_gone", "acme_ci"]))
    async with app.run_test(size=(100, 60)) as pilot:
        switches = await _listed(app, pilot, 4)
        assert list(switches) == ["acme_ci", EXAMPLE_EXTENSION_ID, "acme_twice", "acme_gone"]
        assert [(s.value, s.disabled) for s in switches.values()] == [
            (True, False),
            (False, False),
            (False, True),
            (True, False),
        ]
        # Where each one is installed has a line of its own.
        notes = _notes(app)
        assert notes[0].splitlines() == ["Continuous integration status.", str(ci)]
        assert notes[1].splitlines() == ["Release metadata for Acme projects.", str(release)]
        assert notes[2].splitlines() == [
            "Installed more than once, so neither copy loads:",
            "acme-icode-tools 1.0",
            str(twice),
        ]
        assert notes[3] == "Not installed: agents start without it"
        assert notes[4].startswith("Skipped ") and "'Bad-Id'" in notes[4]
        assert "_chrys_extension_acme_ci" not in sys.modules

        panel = app.query_one(ToolsConfigPanel)
        # Unchanged, the profile's own order stands; a newly enabled extension goes last.
        assert panel.get_config().extensions == ["acme_gone", "acme_ci"]
        switches[EXAMPLE_EXTENSION_ID].value = True
        switches["acme_gone"].value = False
        assert panel.get_config().extensions == ["acme_ci", EXAMPLE_EXTENSION_ID]


async def test_an_extension_checked_in_the_editor_is_saved_to_the_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    install_file_extension(monkeypatch)
    mine = make_profile("Mine", display_name="Mine", description="My agent", extensions=["acme_gone"])
    registry = registry_with(mine)
    async with open_agent_config(registry, current_profile="Mine") as (screen, pilot):
        await _activate_agent_config_tab(screen, pilot, "tools")
        switches = await _listed(screen, pilot, 2)
        switches[EXAMPLE_EXTENSION_ID].value = True
        save = screen.query_one("#ac-save", Button)
        await wait_for(
            lambda: not save.disabled, pilot=pilot, timeout=_DEFAULT_WAIT_TIMEOUT, description="Save enabled"
        )
        save.press()
        await pilot.pause()
        await _wait_for_hydrated(screen, pilot)

    assert registry.get("Mine").extensions == ["acme_gone", EXAMPLE_EXTENSION_ID]
    saved = load_profile_from_yaml(get_platform().config_dir / "agents" / "Mine.yaml")
    assert saved.extensions == ["acme_gone", EXAMPLE_EXTENSION_ID]


async def test_an_extension_s_undecodable_folder_is_shown_without_lone_surrogates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    location = "/home/caf\udce9/.chrys/extensions/acme_cafe.py"
    source = ExtensionSource(id="acme_cafe", origin="file", location=location, summary="Cafe tools.")
    catalog = ExtensionCatalog(sources={"acme_cafe": source}, conflicts={})
    monkeypatch.setattr(extensions, "discover_extensions", lambda: catalog)
    app = WidgetApp(lambda: ToolsConfigPanel(ToolsConfig()))
    async with app.run_test(size=(100, 40)) as pilot:
        await _listed(app, pilot, 1)
        summary, shown = _notes(app)[0].splitlines()
        assert summary == "Cafe tools."
        assert shown.encode("utf-8") and shown.startswith("/home/caf") and shown.endswith("acme_cafe.py")


async def test_the_section_descriptions_wrap_instead_of_being_cut_off() -> None:
    app = WidgetApp(lambda: ToolsConfigPanel(ToolsConfig()))
    async with app.run_test(size=(40, 40)) as pilot:
        description = app.query(".tc-section-desc").last(Label)
        text = str(description.render())
        assert text.startswith("Python extensions installed for") and len(text) > 2 * 40
        await wait_for(
            lambda: description.size.height > 1 and description.size.width <= 40,
            pilot=pilot,
            timeout=_DEFAULT_WAIT_TIMEOUT,
            description="the extensions description laid out on several lines",
        )
        painted = "".join(strip.text for strip in description.render_lines(description.region.reset_offset))
        assert "extensions you trust" in painted


async def test_with_no_extensions_installed_the_tab_says_how_to_get_one() -> None:
    app = WidgetApp(lambda: ToolsConfigPanel(ToolsConfig()))
    async with app.run_test(size=(100, 40)) as pilot:
        status = app.query_one("#tc-extensions-status", Label)
        await wait_for(
            lambda: str(status.render()).startswith("No extensions installed"),
            pilot=pilot,
            timeout=_DEFAULT_WAIT_TIMEOUT,
            description="the no-extensions note",
        )
        assert app.query_one(ToolsConfigPanel).get_config().extensions == []


async def test_a_read_only_profile_shows_its_extensions_switched_off_for_editing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_file_extension(monkeypatch)
    app = WidgetApp(lambda: ToolsConfigPanel(ToolsConfig(), extensions=[EXAMPLE_EXTENSION_ID], read_only=True))
    async with app.run_test(size=(100, 40)) as pilot:
        (switch,) = (await _listed(app, pilot, 1)).values()
        assert (switch.value, switch.disabled) == (True, True)
