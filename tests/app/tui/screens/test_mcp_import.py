# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The MCP import dialog: what it shows for each server, and what Import adds to the MCP tab."""

from __future__ import annotations

import os
from dataclasses import replace

import pytest
from rich.text import Text
from textual.containers import VerticalScroll
from textual.widgets import Button, Input, OptionList, Static, TextArea
from textual.widgets.collapsible import CollapsibleTitle

from chrys.app.tui.i18n import LocaleController
from chrys.app.tui.screens.agents import import_mcp_dialog
from chrys.app.tui.screens.agents.import_mcp_dialog import ImportMcpServersDialog, _ImportItem
from chrys.app.tui.screens.agents.import_mcp_view import (
    _ORIGIN,
    imported_config,
    names_field,
    problem_text,
    source_error_text,
    warning_text,
)
from chrys.app.tui.screens.agents.panels.mcp import MCPConfigPanel, MCPConnectionCard
from chrys.app.tui.screens.dialogs.confirm import ConfirmDialog
from chrys.app.tui.widgets import HatchedEmptyState
from chrys.foundation.config.settings import Settings
from chrys.foundation.i18n import Localizer
from chrys.foundation.i18n.formatting import format_message, sanitize_legacy_scalar
from chrys.service.mcp.importers import (
    FieldUse,
    ImportContext,
    ImportWarning,
    McpImportCandidate,
    McpImportResult,
    McpSourceError,
    Problem,
    SourceErrorKind,
    ValueReason,
    WarningKind,
)
from chrys.service.mcp.importers.base import SourceServer, convert
from tests.app.tui.screens._mcp_import_support import (
    FakeSource,
    PanelApp,
    ask_to_import,
    badge_text,
    candidate,
    import_ticked,
    notes_text,
    open_dialog,
    stdio,
    wait_for_items,
)
from tests.support.pilot_barrier import screen_is_settled
from tests.support.tui_helpers import resize_when_settled
from tests.support.waiting import wait_for

_AUTH_NOT_IMPORTED = ImportWarning(WarningKind.AUTH_NOT_IMPORTED)


def _unsupported(name: str, problem: Problem, field: str = "") -> McpImportCandidate:
    return McpImportCandidate(name, "/src/config.json", None, problem=problem, problem_field=field)


def test_each_warning_kind_reads_as_one_actionable_sentence() -> None:
    texts = [
        format_message(warning_text(ImportWarning(WarningKind.NOT_IMPORTED, ("a.cwd", "a.x")), "pi")),
        format_message(warning_text(ImportWarning(WarningKind.AUTH_NOT_IMPORTED), "pi")),
        format_message(warning_text(ImportWarning(WarningKind.TOOLS_RESTRICTED), "pi")),
    ]

    assert texts == [
        "Some settings from the original application were not copied. After import, test the connection and check that the features you need are available.",
        "Sign-in from pi cannot be carried over. iCode does not yet support MCP OAuth sign-in. If the server supports token authentication, configure “Headers” as its documentation describes. If it only supports OAuth sign-in, it cannot currently be used in iCode.",
        "The original application's tool restrictions cannot be carried over directly, so this server's tools are initially turned off after import. Choose the tools you need in “Available Tool Scope”.",
    ]


def test_problems_and_source_errors_name_the_setting_or_file() -> None:
    value = replace(_unsupported("x", Problem.VALUE_NOT_REPRESENTABLE, "mcpServers.x.url"), field_use=FieldUse.URL)
    located = McpSourceError("/a.json", SourceErrorKind.UNPARSABLE, line=3, column=7)
    invalid = McpSourceError("/a.json", SourceErrorKind.INVALID, field="mcp.x")

    variable = replace(
        _unsupported("x", Problem.VARIABLE_NOT_SUPPORTED, "mcpServers.x.url"),
        field_use=FieldUse.URL,
        reason=ValueReason.VARIABLE,
    )
    external = replace(
        _unsupported("x", Problem.VALUE_FROM_FILE_OR_COMMAND, "mcp.x.environment.T"),
        field_use=FieldUse.ENV_VALUE,
        reason=ValueReason.FILE,
    )
    sse = replace(_unsupported("x", Problem.UNSUPPORTED_CONNECTION), transport="sse")
    ws = replace(sse, transport="ws")
    remote = _unsupported("x", Problem.REMOTE_ENVIRONMENT, "mcp_servers.x.environment_id")
    assert [format_message(problem_text(c, "ChatGPT")) for c in (sse, ws, remote)] == [
        (
            "This server connects over SSE; iCode supports only STDIO and Streamable HTTP. "
            "If the server also offers one of these, add it manually."
        ),
        (
            "This server connects over WebSocket; iCode supports only STDIO and Streamable HTTP. "
            "If the server also offers one of these, add it manually."
        ),
        (
            "This configuration accesses the MCP server through a remote environment, which iCode cannot use. "
            "If you have a Streamable HTTP URL accessible from this computer "
            "or a STDIO command that runs here, add the server manually."
        ),
    ]
    # A connection refusal's sentence says it all; the others are introduced by their source setting.
    assert [names_field(c) for c in (sse, remote, variable, external, value)] == [False, False, True, True, True]
    assert [format_message(problem_text(c, "ChatGPT")) for c in (variable, external, value)] == [
        "The server URL contains an environment variable reference, which iCode does not expand here. Add the server manually and enter its full URL, replacing any environment variable references with their values.",
        "The environment variable value is stored in another file and cannot be imported automatically. Add the server manually and fill in the value using the server instructions. You can also reference an environment variable or get the value from a command.",
        "The server URL cannot be imported as written. Add the server manually and enter its full URL, replacing any environment variable references with their values.",
    ]
    assert format_message(source_error_text(located, "pi")) == (
        "Couldn't parse /a.json: syntax error at line 3, column 7. Correct it, then reopen import."
    )
    assert format_message(source_error_text(invalid, "pi")) == "/a.json has an invalid setting: mcp.x."


@pytest.mark.parametrize("locale", ["en", "zh-Hans"])
def test_imported_server_keeps_its_description_origin_and_warnings(locale) -> None:
    config = stdio("github")
    config.description = "GitHub tools"
    localizer = Localizer(locale)
    omitted = ImportWarning(WarningKind.NOT_IMPORTED, ("mcp.github.setting_a", "mcp.github.setting_b"))

    imported = imported_config(candidate(config, warnings=(omitted, _AUTH_NOT_IMPORTED)), "ChatGPT", localizer.render)

    assert imported is not None
    assert imported.description.splitlines() == [
        "GitHub tools",
        localizer.render(_ORIGIN.bind(agent="ChatGPT", path="/src/config.json")),
        "",
        "⚠ Original configuration fields:" if locale == "en" else "⚠ 原配置项：",  # noqa: RUF001 -- localized punctuation
        "  mcp.github.setting_a",
        "  mcp.github.setting_b",
        localizer.render(warning_text(omitted, "ChatGPT")),
        "",
        f"⚠ {localizer.render(warning_text(_AUTH_NOT_IMPORTED, 'ChatGPT'))}",
    ]
    assert imported_config(_unsupported("x", Problem.UNSUPPORTED_CONNECTION), "ChatGPT", localizer.render) is None


async def test_import_button_adds_ticked_servers_as_unsaved_cards(monkeypatch) -> None:
    imported = replace(stdio("github"), cwd="{{ROOT}}/server")
    cwd_warning = ImportWarning(WarningKind.OPENCODE_VARIABLE_CWD, ("mcp.github.cwd",))
    source = FakeSource(
        "fake", "OpenCode", McpImportResult((candidate(imported, warnings=(_AUTH_NOT_IMPORTED, cwd_warning)),))
    )
    monkeypatch.setattr(import_mcp_dialog, "IMPORT_SOURCES", (source,))
    app = PanelApp(MCPConfigPanel([replace(stdio("existing", "node"), cwd="existing-dir")]))
    description = [
        "Imported from OpenCode (/src/config.json).",
        "",
        f"⚠ {format_message(warning_text(_AUTH_NOT_IMPORTED, 'OpenCode'))}",
        "",
        "⚠ Original configuration field: mcp.github.cwd",
        format_message(warning_text(cwd_warning, "OpenCode")),
    ]
    async with app.run_test(size=(120, 50)) as pilot:
        app.panel.query_one("#mcp-import-btn", Button).press()
        await wait_for(
            lambda: isinstance(app.screen, ImportMcpServersDialog) and app.screen.is_mounted,
            pilot=pilot,
            description="dialog",
        )
        dialog = app.screen
        assert isinstance(dialog, ImportMcpServersDialog)
        (item,) = await wait_for_items(dialog, pilot, 1)
        assert badge_text(item) == "⚠ 2"
        # A folded card mounts its header only; its fields arrive when it is first opened, once.
        assert isinstance(item.card, MCPConnectionCard)
        assert not item.card.query(".config-card-body")
        assert item.tick.region.y == item.card.query_one(".config-card-title").region.y
        item.card.set_collapsed(False, scroll_visible=False)
        await wait_for(lambda: bool(item.card.query("#mcp-desc-0")), pilot=pilot, description="fields mounted")
        item.card.set_collapsed(True)
        item.card.set_collapsed(False, scroll_visible=False)
        await wait_for(
            lambda: any(body.display for body in item.card.query(".config-card-body")),
            pilot=pilot,
            description="reopened",
        )
        assert len(item.card.query(".config-card-body")) == 1
        cwd_input = item.card.query_one("#mcp-cwd-0", Input)
        assert (cwd_input.value, cwd_input.disabled) == ("{{ROOT}}/server", True)
        await wait_for(lambda: screen_is_settled(app, dialog), pilot=pilot, description="expanded layout settled")
        assert item.tick.region.y == item.card.query_one(".config-card-title").region.y
        await resize_when_settled(pilot, 80, 40)
        assert item.tick.region.y == item.card.query_one(".config-card-title").region.y
        # The preview's Description shows what Import saves, warnings included.
        assert item.card.query_one("#mcp-desc-0", TextArea).text.splitlines() == description
        confirm = dialog.query_one("#mcp-import-confirm", Button)
        assert confirm.disabled

        item.tick.value = True
        await import_ticked(dialog, pilot)

        await wait_for(lambda: len(app.panel.query(MCPConnectionCard)) == 2, pilot=pilot, description="card added")
        rows = app.panel._snapshot_rows()
        assert [row.config.name for row in rows] == ["github", "existing"]
        assert [row.config.cwd for row in rows] == ["{{ROOT}}/server", "existing-dir"]
        assert rows[0].config.description.splitlines() == description
        # Closing the window would drop the unsaved server: the user is told to save.
        await wait_for(
            lambda: app.notifications == ["Imported 1 MCP server. Review it, then click Save."],
            pilot=pilot,
            description="told to save",
        )


async def test_an_unopened_preview_keeps_cwd_on_import(monkeypatch) -> None:
    source = FakeSource("fake", "Fake", McpImportResult((candidate(replace(stdio("github"), cwd=".")),)))
    monkeypatch.setattr(import_mcp_dialog, "IMPORT_SOURCES", (source,))
    app = PanelApp(MCPConfigPanel([]))
    async with app.run_test(size=(100, 40)) as pilot:
        dialog = await open_dialog(app, pilot, [source])
        (item,) = await wait_for_items(dialog, pilot, 1)
        assert not item.card.query("#mcp-cwd-0")
        item.tick.value = True
        await import_ticked(dialog, pilot)
        await wait_for(lambda: len(app.panel.query(MCPConnectionCard)) == 1, pilot=pilot, description="card added")
        assert app.panel._snapshot_rows()[0].config.cwd == "."


async def test_imported_name_is_trimmed_before_preview_confirmation_and_save() -> None:
    imported = convert(SourceServer(" github ", "/cfg.json", "stdio", command="run"))
    source = FakeSource("fake", "Fake", McpImportResult((imported,)))
    app = PanelApp(MCPConfigPanel([]))
    async with app.run_test(size=(120, 50)) as pilot:
        dialog = await open_dialog(app, pilot, [source])
        (item,) = await wait_for_items(dialog, pilot, 1)
        assert item.imported is not None
        assert item.imported.name == "github"
        assert item.card.get_config().name == item.imported.name
        assert str(item.card.query_one(CollapsibleTitle).label) == "github"
        prefix = item.imported.tool_name_prefix
        assert prefix == "github"
        item.tick.value = True
        confirmation = await ask_to_import(dialog, pilot)
        assert str(confirmation.query_one("#confirm-message", Static).render()).splitlines()[-1] == "  github"
        confirmation.query_one("#confirm-yes", Button).press()
        await wait_for(lambda: len(app.panel.query(MCPConnectionCard)) == 1, pilot=pilot, description="card added")
        saved = app.panel.query_one(MCPConnectionCard).get_config()
        assert (saved.name, saved.tool_name_prefix) == ("github", prefix)


@pytest.mark.parametrize("transport", ["stdio", "http"])
async def test_preview_and_imported_fields_keep_raw_values_through_copy_edit_and_save(monkeypatch, transport) -> None:
    payload = "\x1b]52;c;VEVTVA==\x1b\\\u202e\u200b"
    raw = "value" + payload
    config = replace(
        stdio("srv" + payload),
        transport=transport,
        description=raw + "\nsecond line",
        args=[raw],
        env={"KEY" + payload: raw},
        url="https://example.test/" + payload,
        headers={"X-Test" + payload: raw},
        allowed_tools=[raw],
        use_progressive_disclosure=True,
        always_load=[raw],
        tool_name_prefix="srv",
        enabled=False,
    )
    copied: list[str] = []
    monkeypatch.setattr("chrys.app.tui.clipboard.platform_helpers.clipboard_copy", copied.append)
    app = PanelApp(MCPConfigPanel([]))
    async with app.run_test(size=(120, 100)) as pilot:
        source = FakeSource("fake", "Fake", McpImportResult((candidate(config),)))
        dialog = await open_dialog(app, pilot, [source])
        (item,) = await wait_for_items(dialog, pilot, 1)
        item.card.set_collapsed(False, scroll_visible=False)
        await wait_for(
            lambda: bool(item.card.query(".mcp-item-value-input")), pilot=pilot, description="fields mounted"
        )
        await pilot.pause()
        value_input = item.card.query_one(".mcp-item-value-input", Input)
        assert value_input.value == raw
        value_input.select_all()
        value_input.action_copy()
        assert copied[-1] == raw
        preview_description = item.card.query_one("#mcp-desc-0", TextArea)
        assert raw in preview_description.text
        preview_description.action_select_all()
        preview_description.action_copy()
        assert copied[-1] == preview_description.text
        assert item.imported is not None and item.imported.enabled is False
        item.tick.value = True
        await import_ticked(dialog, pilot)
        await wait_for(lambda: len(app.panel.query(MCPConnectionCard)) == 1, pilot=pilot, description="card added")
        card = app.panel.query_one(MCPConnectionCard)
        await pilot.pause()
        description = card.query_one("#mcp-desc-0", TextArea)
        assert raw in description.text
        description.action_select_all()
        description.action_copy()
        assert copied[-1] == description.text
        saved = card.get_config()
        assert saved.name == config.name
        assert saved.allowed_tools == config.allowed_tools
        assert saved.always_load == config.always_load
        assert saved.enabled is False
        assert raw in saved.description
        if transport == "stdio":
            assert saved.args == config.args
            assert saved.env == config.env
        else:
            assert saved.url == config.url
            assert saved.headers == config.headers

        value_input = card.query_one(".mcp-item-value-input", Input)
        assert value_input.value == raw
        value_input.selection = (len(raw), len(raw))
        value_input.insert_text_at_cursor(" edited")
        value_input.select_all()
        value_input.action_copy()
        assert copied[-1] == raw + " edited"
        original_description = description.text
        description.insert("\nedited " + raw, description.document.end)
        description.action_select_all()
        description.action_copy()
        assert copied[-1] == original_description + "\nedited " + raw
        edited = card.get_config()
        assert edited.description == original_description + "\nedited " + raw
        if transport == "stdio":
            assert edited.env == {"KEY" + payload: raw + " edited"}
            assert edited.args == config.args
        else:
            assert edited.headers == {"X-Test" + payload: raw + " edited"}
            assert edited.url == config.url


async def test_localization_and_source_switch_keep_current_folds() -> None:
    result = McpImportResult((candidate(stdio("srv")), _unsupported("refused", Problem.UNSUPPORTED_CONNECTION)))
    sources = [FakeSource("a", "Alpha", result), FakeSource("b", "Beta", result)]
    app = PanelApp(MCPConfigPanel([]))
    async with app.run_test(size=(120, 70)) as pilot:
        dialog = await open_dialog(app, pilot, sources)
        items = await wait_for_items(dialog, pilot, 2)
        for collapsed in (False, True, False):
            for item in items:
                item.card.set_collapsed(collapsed, scroll_visible=False)
            previous = items[0]
            dialog.refresh_localization()
            await wait_for(
                lambda previous=previous: (
                    bool(dialog.query(_ImportItem)) and dialog.query_one(_ImportItem) is not previous
                ),
                pilot=pilot,
                description="localized pane replaced",
            )
            items = await wait_for_items(dialog, pilot, 2)
            assert [item.card.collapsed for item in items] == [collapsed, collapsed]
        for source_id in ("b", "a"):
            dialog._request_source(source_id)
            await wait_for(
                lambda source_id=source_id: (
                    bool(dialog.query(_ImportItem)) and dialog.query_one(_ImportItem).source.source_id == source_id
                ),
                pilot=pilot,
                description="requested source mounted",
            )
            items = await wait_for_items(dialog, pilot, 2)
            assert [item.card.collapsed for item in items] == ([True, True] if source_id == "b" else [False, False])


@pytest.mark.parametrize(
    ("allowed", "progressive", "always"),
    [(None, False, ()), ((), False, ()), (("read", "delete"), True, ("read",)), (None, True, ("read",))],
)
async def test_import_keeps_representable_tool_policy_through_editor(allowed, progressive, always) -> None:
    server = SourceServer(
        "srv",
        "/cfg.json",
        "stdio",
        command="python",
        allowed_tools=allowed,
        progressive=progressive,
        always_load=always,
    )
    app = PanelApp(MCPConfigPanel([]))
    async with app.run_test(size=(120, 80)) as pilot:
        dialog = await open_dialog(app, pilot, [FakeSource("fake", "Fake", McpImportResult((convert(server),)))])
        (item,) = await wait_for_items(dialog, pilot, 1)
        item.tick.value = True
        await import_ticked(dialog, pilot)
        await wait_for(lambda: len(app.panel.query(MCPConnectionCard)) == 1, pilot=pilot, description="card added")
        card = app.panel.query_one(MCPConnectionCard)
        assert card.validate() == []
        saved = card.get_config()
        assert saved.allowed_tools == (None if allowed is None else list(allowed))
        assert saved.use_progressive_disclosure is progressive
        assert saved.always_load == list(always)


def test_codex_relative_cwd_warning_survives_import_in_both_languages() -> None:
    warning = ImportWarning(WarningKind.CODEX_RELATIVE_CWD)
    for locale in ("en", "zh-Hans"):
        localizer = Localizer(locale)
        config = imported_config(
            candidate(replace(stdio("srv"), cwd="."), warnings=(warning,)), "ChatGPT", localizer.render
        )
        assert config is not None
        assert config.cwd == "."
        assert localizer.render(warning_text(warning, "ChatGPT")) in config.description
        assert "/src" not in localizer.render(warning_text(warning, "ChatGPT"))
        assert "ChatGPT" in config.description
        assert "Codex" not in config.description


def test_opencode_variable_cwd_warning_survives_import_in_both_languages() -> None:
    warning = ImportWarning(WarningKind.OPENCODE_VARIABLE_CWD, ("mcp.srv.cwd",))
    for locale in ("en", "zh-Hans"):
        localizer = Localizer(locale)
        text = localizer.render(warning_text(warning, "OpenCode"))
        config = imported_config(
            candidate(replace(stdio("srv"), cwd="{{ROOT}}"), warnings=(warning,)),
            "OpenCode",
            localizer.render,
        )
        assert config is not None
        assert config.cwd == "{{ROOT}}"
        assert text in config.description
        assert "mcp.srv.cwd" in config.description
        assert "OpenCode" in text
        assert ("Click “Test”" if locale == "en" else "测试") in text
        assert config.description.count("⚠") == 1


async def test_pressing_import_twice_opens_one_dialog_and_one_confirmation(monkeypatch) -> None:
    source = FakeSource("fake", "Fake", McpImportResult((candidate(stdio("github")),)))
    monkeypatch.setattr(import_mcp_dialog, "IMPORT_SOURCES", (source,))
    app = PanelApp(MCPConfigPanel([]))
    async with app.run_test(size=(120, 50)) as pilot:
        button = app.panel.query_one("#mcp-import-btn", Button)
        button.press()
        button.press()
        await wait_for(
            lambda: isinstance(app.screen, ImportMcpServersDialog) and app.screen.is_mounted,
            pilot=pilot,
            description="dialog",
        )
        dialog = app.screen
        assert isinstance(dialog, ImportMcpServersDialog)
        (item,) = await wait_for_items(dialog, pilot, 1)
        # A dialog stacked over this one would drop what this one imports.
        assert sum(isinstance(screen, ImportMcpServersDialog) for screen in app.screen_stack) == 1

        item.tick.value = True
        confirm = dialog.query_one("#mcp-import-confirm", Button)
        await wait_for(lambda: not confirm.disabled, pilot=pilot, description="Import is enabled")
        confirm.press()
        confirm.press()
        await wait_for(
            lambda: isinstance(app.screen, ConfirmDialog) and app.screen.is_mounted,
            pilot=pilot,
            description="confirmation",
        )
        assert sum(isinstance(screen, ConfirmDialog) for screen in app.screen_stack) == 1
        app.screen.query_one("#confirm-yes", Button).press()

        await wait_for(lambda: len(app.panel.query(MCPConnectionCard)) == 1, pilot=pilot, description="card added")
        assert [row.config.name for row in app.panel._snapshot_rows()] == ["github"]


async def test_cancel_queued_after_import_keeps_confirmation_and_import_result() -> None:
    source = FakeSource("fake", "Fake", McpImportResult((candidate(stdio("github")),)))
    app = PanelApp(MCPConfigPanel([]))
    async with app.run_test(size=(120, 50)) as pilot:
        dialog = await open_dialog(app, pilot, [source])
        (item,) = await wait_for_items(dialog, pilot, 1)
        item.tick.value = True
        confirm = dialog.query_one("#mcp-import-confirm", Button)
        await wait_for(lambda: not confirm.disabled, pilot=pilot, description="Import is enabled")
        confirm.press()
        dialog.query_one("#mcp-import-cancel", Button).press()
        await pilot.pause()

        assert isinstance(app.screen, ConfirmDialog)
        assert not dialog.dismiss_requested
        app.screen.query_one("#confirm-yes", Button).press()
        await wait_for(lambda: len(app.panel.query(MCPConnectionCard)) == 1, pilot=pilot, description="card added")
        assert [row.config.name for row in app.panel._snapshot_rows()] == ["github"]


async def test_cancel_action_only_closes_the_topmost_import_dialog() -> None:
    source = FakeSource("fake", "Fake", McpImportResult((candidate(stdio("github")),)))
    app = PanelApp(MCPConfigPanel([]))
    async with app.run_test(size=(120, 50)) as pilot:
        dialog = await open_dialog(app, pilot, [source])
        (item,) = await wait_for_items(dialog, pilot, 1)
        item.tick.value = True
        confirmation = await ask_to_import(dialog, pilot)
        await dialog.run_action("cancel")
        await pilot.pause()

        assert app.screen is confirmation
        assert not dialog.dismiss_requested
        await pilot.press("escape")
        assert app.screen is dialog
        await pilot.press("escape")
        assert app.screen is not dialog
        assert not app.panel._snapshot_rows()


async def test_servers_that_can_not_be_added_are_locked_as_the_add_picker_locks_them() -> None:
    present, taken = candidate(stdio("present", "node")), candidate(stdio("taken", "node"))
    legacy = _unsupported("legacy", Problem.VALUE_NOT_REPRESENTABLE, "s.url")
    result = McpImportResult((taken, present, legacy, candidate(stdio("new")), candidate(stdio("Mid"))))
    added = imported_config(present, "Fake", Localizer("en").render)
    assert added is not None
    app = PanelApp(MCPConfigPanel([added, stdio("TAKEN", "other")]))
    async with app.run_test(size=(120, 50)) as pilot:
        dialog = await open_dialog(app, pilot, [FakeSource("fake", "Fake", result)])
        items = await wait_for_items(dialog, pilot, 5)

        # Importable first, then those clashing with the agent's servers, then those that can't be imported;
        # each group by name.
        assert [item.index for item in items] == [4, 3, 1, 0, 2]
        assert [badge_text(item) for item in items] == [
            "",
            "",
            "Already added",
            "Name in use",
            "Can't import",
        ]
        # The badge is short; its tooltip gives the "+ Add" picker's reason in full.
        assert [item.card.badge.tooltip for item in items] == [
            None,
            None,
            None,
            Text("Same name already exists with different settings"),
            None,
        ]
        # A server that can't be ticked shows no tick.
        assert [(item.tick.disabled, item.tick.value, item.tick.display) for item in items] == [
            (False, False, True),
            (False, False, True),
            (True, False, False),
            (True, False, False),
            (True, False, False),
        ]
        assert len({item.card.region.x for item in items}) == 1
        assert notes_text(items[4]).splitlines() == [
            "Original configuration field: s.url",
            (
                "The setting's content cannot be imported as written. "
                "Add the server manually and fill in this setting using the server instructions."
            ),
            "Configuration source: /src/config.json",
        ]

        # An open card's title moves down a line; its item is marked so the tick follows it.
        assert not items[4].has_class("-open")
        items[4].card.set_collapsed(False, scroll_visible=False)
        assert items[4].has_class("-open")


class _ChineseApp(PanelApp):
    locale_controller = LocaleController(Settings(locale="zh-Hans"))


async def test_a_server_imported_in_another_language_or_redescribed_is_still_already_added() -> None:
    toolexp = candidate(stdio("toolexp", "npx"), warnings=(ImportWarning(WarningKind.NOT_IMPORTED, ("t.write_*",)),))
    edited = candidate(stdio("edited", "npx", "e"))
    english = Localizer("en")
    in_english = imported_config(toolexp, "pi", english.render)
    assert in_english is not None
    redescribed = replace(stdio("edited", "npx", "e"), description="My own notes")
    app = _ChineseApp(MCPConfigPanel([in_english, redescribed]))
    async with app.run_test(size=(120, 50)) as pilot:
        dialog = await open_dialog(app, pilot, [FakeSource("pi", "pi", McpImportResult((toolexp, edited)))])
        items = await wait_for_items(dialog, pilot, 2)

        # The description the import writes changes with the language and the user's edits; settings don't.
        assert [badge_text(item) for item in items] == ["已添加", "已添加"]
        assert [(item.tick.disabled, item.tick.display) for item in items] == [(True, False), (True, False)]


async def test_ticking_a_server_locks_a_conflicting_one_in_other_applications() -> None:
    name = "gh[/b]\x1b\u202e\u200b"
    first = FakeSource("a", "Alpha", McpImportResult((candidate(stdio(name, "npx", "a")),)))
    beta = (candidate(stdio(name, "npx", "b")), candidate(stdio("zz", "zz")), candidate(stdio("added", "added")))
    second = FakeSource("b", "Beta", McpImportResult(beta))
    app = PanelApp(MCPConfigPanel([stdio("added", "added")]))
    async with app.run_test(size=(120, 50)) as pilot:
        dialog = await open_dialog(app, pilot, [first, second])
        (one,) = await wait_for_items(dialog, pilot, 1)
        # Titles and conflict details remain plain display text; field values keep the source text.
        assert str(one.card.query_one(CollapsibleTitle).label) == sanitize_legacy_scalar(name)
        one.tick.value = True
        await wait_for(lambda: 0 in dialog._ticked["a"], pilot=pilot, description="tick recorded")

        dialog._request_source("b")
        await wait_for(lambda: dialog._shown_source == "b", pilot=pilot, description="Beta is shown")
        free, two, added = await wait_for_items(dialog, pilot, 3)

        # A duplicate follows the importable servers, before those the agent already has.
        assert [item.index for item in (free, two, added)] == [1, 0, 2]
        assert badge_text(added) == "Already added"
        assert two.tick.disabled and not two.tick.display
        assert badge_text(two) == "Duplicate"
        tooltip = two.card.badge.tooltip
        assert isinstance(tooltip, Text)
        assert tooltip.plain == (
            f"Conflicts with the selected {sanitize_legacy_scalar(name)} (Alpha); only one of them can be imported."
        )
        assert str(two.card.query_one(CollapsibleTitle).label) == sanitize_legacy_scalar(name)


async def test_import_lists_the_ticked_servers_by_application_before_adding_them() -> None:
    alpha = FakeSource("a", "Alpha", McpImportResult((candidate(stdio("zeta", "z")), candidate(stdio("beta", "b")))))
    beta = FakeSource("b", "Beta", McpImportResult((candidate(stdio("[b]docs", "d")),)))
    app = PanelApp(MCPConfigPanel([]))
    async with app.run_test(size=(120, 50)) as pilot:
        dialog = await open_dialog(app, pilot, [alpha, beta])
        sidebar = dialog.query_one("#mcp-import-sources", OptionList)

        def counts() -> list[str]:
            return [sidebar.get_option(source_id).prompt.plain for source_id in ("a", "b")]

        for item in await wait_for_items(dialog, pilot, 2):
            item.tick.value = True
        await wait_for(lambda: counts() == ["Alpha (2)", "Beta"], pilot=pilot, description="Alpha's count")
        dialog._request_source("b")
        await wait_for(lambda: dialog._shown_source == "b", pilot=pilot, description="Beta is shown")
        (docs,) = await wait_for_items(dialog, pilot, 1)
        # Each application says how many of its servers are ticked, so ticks out of view aren't forgotten.
        assert counts() == ["Alpha (2)", "Beta"]
        docs.tick.value = True
        await wait_for(lambda: counts() == ["Alpha (2)", "Beta (1)"], pilot=pilot, description="Beta's count")

        confirmation = await ask_to_import(dialog, pilot)
        message = confirmation.query_one("#confirm-message", Static).render()
        assert str(message).splitlines() == [
            "Import 3 MCP server configurations:",
            "",
            "Alpha (2)",
            "  beta",
            "  zeta",
            "",
            "Beta (1)",
            "  [b]docs",
        ]
        # Back returns to the ticks as they were.
        confirmation.query_one("#confirm-no", Button).press()
        await wait_for(lambda: app.screen is dialog, pilot=pilot, description="back to the import dialog")
        assert dialog._ticked == {"a": {0, 1}, "b": {0}}

        await import_ticked(dialog, pilot)
        await wait_for(lambda: len(app.panel.query(MCPConnectionCard)) == 3, pilot=pilot, description="cards added")


async def test_a_long_name_is_cut_short_and_another_application_starts_at_its_top() -> None:
    long_name = "n" * 150
    servers = McpImportResult(
        (*(candidate(stdio(f"s{i:02}", f"c{i}")) for i in range(20)), candidate(stdio(long_name)))
    )
    app = PanelApp(MCPConfigPanel([]))
    async with app.run_test(size=(120, 30)) as pilot:
        dialog = await open_dialog(app, pilot, [FakeSource("a", "Alpha", servers), FakeSource("b", "Beta", servers)])
        items = await wait_for_items(dialog, pilot, 21)

        # One line, cut short, rather than wrapped onto a second line the folded card hides.
        long = next(item for item in items if item.card.header_title == long_name)
        line = long.card.query_one(CollapsibleTitle).render_line(0).text.rstrip()
        assert line.startswith("▶ nnn") and line.endswith("…")

        pane = dialog.query_one("#mcp-import-servers", VerticalScroll)
        pane.scroll_end(animate=False)
        await wait_for(lambda: pane.scroll_y > 0, pilot=pilot, description="Alpha scrolled down")
        dialog._request_source("b")
        await wait_for(
            lambda: [item.source.source_id for item in dialog.query(_ImportItem)][:1] == ["b"],
            pilot=pilot,
            description="Beta is listed",
        )
        await wait_for_items(dialog, pilot, 21)
        assert pane.scroll_y == 0


async def test_refused_source_paths_escape_surrogates_and_use_the_import_home(tmp_path) -> None:
    home = tmp_path / "home\udcfe"
    paths = [str(home / "bad\udcff\x1b.json"), str(tmp_path / "home\udcfe-other" / "bad\udcff.json")]
    source = FakeSource(
        "fake",
        "Fake",
        McpImportResult(
            tuple(
                replace(_unsupported(str(index), Problem.VALUE_NOT_REPRESENTABLE), source_path=path)
                for index, path in enumerate(paths)
            )
        ),
    )
    app = PanelApp(MCPConfigPanel([]))
    async with app.run_test(size=(120, 50)) as pilot:
        dialog = ImportMcpServersDialog(
            workspace_cwd=None,
            existing=[],
            locale_controller=app.locale_controller,
            sources=[source],
            context=ImportContext(None, home),
        )
        app.push_screen(dialog)
        items = await wait_for_items(dialog, pilot, 2)
        inside = f"~{os.sep}bad\\udcff�.json"
        assert dialog._display_path(paths[0]) == inside
        assert notes_text(items[0]).splitlines()[-1] == f"Configuration source: {inside}"
        outside = f"{tmp_path}{os.sep}home\\udcfe-other{os.sep}bad\\udcff.json"
        assert dialog._display_path(paths[1]) == outside
        assert notes_text(items[1]).splitlines()[-1] == f"Configuration source: {outside}"


async def test_file_errors_and_empty_sources_are_shown_in_the_pane() -> None:
    broken = McpImportResult(source_errors=(McpSourceError("/a.json", SourceErrorKind.UNREADABLE),))
    app = PanelApp(MCPConfigPanel([]))
    async with app.run_test(size=(120, 50)) as pilot:
        dialog = await open_dialog(
            app, pilot, [FakeSource("a", "Alpha", broken), FakeSource("b", "Beta", McpImportResult())]
        )

        def pane() -> str:
            statics = [str(widget.render()) for widget in dialog.query("#mcp-import-servers Static")]
            return " ".join([*statics, *(empty.label for empty in dialog.query(HatchedEmptyState))])

        await wait_for(lambda: "Couldn't read /a.json." in pane(), pilot=pilot, description="file error")
        dialog._request_source("b")
        await wait_for(lambda: "No MCP servers found." in pane(), pilot=pilot, description="empty source")
        assert not dialog.query(Static).filter(".mcp-import-error")


async def test_imported_servers_are_scrolled_into_view() -> None:
    source = FakeSource("fake", "Fake", McpImportResult((candidate(stdio("github")),)))
    app = PanelApp(MCPConfigPanel([stdio(f"server-{i}", "node") for i in range(20)]))
    async with app.run_test(size=(120, 30)) as pilot:
        app.panel.scroll_end(animate=False)
        await wait_for(lambda: app.panel.scroll_y > 0, pilot=pilot, description="scrolled down")
        dialog = await open_dialog(app, pilot, [source])
        (item,) = await wait_for_items(dialog, pilot, 1)
        item.tick.value = True
        await import_ticked(dialog, pilot)

        await wait_for(lambda: len(app.panel.query(MCPConnectionCard)) == 21, pilot=pilot, description="card added")
        await wait_for(lambda: app.panel.scroll_y == 0, pilot=pilot, description="new card in view")
