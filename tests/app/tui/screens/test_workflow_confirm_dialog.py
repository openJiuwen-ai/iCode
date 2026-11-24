# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Trust review keeps source, verification values and actions accessible across tabs and terminal sizes."""

from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
from io import StringIO
from pathlib import Path

import pytest
from rich.console import Console
from rich.syntax import Syntax
from rich.table import Table
from textual.containers import VerticalScroll
from textual.widgets import Button, Collapsible, Static, TabbedContent

from chrys.app.tui.screens.dialogs.workflow_confirm import WorkflowConfirmDialog
from chrys.foundation.config.settings import Settings
from chrys.orchestration.workflows.catalog import WorkflowCatalog
from chrys.orchestration.workflows.preview import WorkflowInspection
from chrys.service.workflows.discovery import BUILTIN_DIR, read_source
from tests.orchestration.workflows._hosting import make_workdir, write_workflow, write_workflow_package
from tests.support.tui_app_harness import make_chrys_app
from tests.support.tui_helpers import click_when_settled, rich_plain
from tests.support.waiting import wait_for
from tests.support.workflow_workers import python_workflow


@pytest.mark.parametrize(
    ("locale", "theme", "size", "accept"),
    [
        ("en", "chrys", (144, 44), True),
        ("zh-Hans", "chrys-ansi", (100, 34), False),
        ("en", "textual-light", (80, 30), False),
        ("zh-Hans", "chrys", (60, 24), True),
    ],
)
async def test_trust_tabs_preserve_review_details_and_fixed_actions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    locale: str,
    theme: str,
    size: tuple[int, int],
    accept: bool,
) -> None:
    workdir = make_workdir(tmp_path / ("long-project-directory-" * 3) / "项目 [literal]")
    monkeypatch.chdir(workdir)
    title = "Workflow Demo · Project Tour"
    source = (BUILTIN_DIR / "demo-workflow.py").read_text(encoding="utf-8")
    assert source.count(f'"{title}",') == 1
    source = source.replace(f'"{title}",', f'"{title} [literal]",')
    source += "\n# " + "Long source line [literal] " * 30 + "\n"
    write_workflow(workdir, "review", source.encode())
    preview = await WorkflowCatalog(config_dir=tmp_path / "config", workdir=workdir).preview("review", trust=True)
    # A manifest diagnostic stays literal even when it contains markup-like source text.
    preview = replace(
        preview,
        manifest={
            **preview.manifest,
            "warnings": [{"code": "test", "node_id": "read_request", "message": "Check [literal] configuration"}],
        },
    )
    app = make_chrys_app(tmp_path / "sessions", settings=Settings(locale=locale, theme=theme))
    decisions: list[bool] = []
    async with app.run_test(size=size) as pilot:
        dialog = WorkflowConfirmDialog(preview, locale_controller=app.locale_controller)
        await app.push_screen(dialog, decisions.append)
        tabs = dialog.query_one(TabbedContent)
        assert tabs.active == "workflow-confirm-info-tab"
        assert tabs.get_tab("workflow-confirm-info-tab").label_text == ("信息" if locale == "zh-Hans" else "Info")
        assert tabs.get_tab("workflow-confirm-source-tab").label_text == ("源代码" if locale == "zh-Hans" else "Source")
        assert str(dialog.query_one("#workflow-info-title", Static).content) == f"{title} [literal]"
        assert str(dialog.query_one("#workflow-info-path", Static).content) == preview.source.canonical_path
        assert any(
            str(widget.content) == "⚠ Check [literal] configuration"
            for widget in dialog.query(".workflow-manifest-warning").results(Static)
        )
        node_table = dialog.query_one("#workflow-info-nodes", Static).content
        assert isinstance(node_table, Table)
        node_text = rich_plain(node_table)
        for node in preview.manifest["nodes"]:
            assert node["id"] in node_text
        assert ("最多 2 次迭代" if locale == "zh-Hans" else "Up to 2 iterations") in node_text
        verification = dialog.query_one(Collapsible)
        assert not verification.collapsed
        assert preview.load.entry_digest in [str(widget.content) for widget in verification.query(Static)]
        assert preview.spec_digest in [str(widget.content) for widget in verification.query(Static)]
        info_scroll = dialog.query_one("#workflow-confirm-info-scroll", VerticalScroll)
        info_scroll.scroll_end(animate=False)
        await wait_for(lambda: info_scroll.scroll_y > 0, pilot=pilot)
        assert info_scroll.max_scroll_x == 0
        assert not decisions

        await click_when_settled(pilot, tabs.get_tab("workflow-confirm-source-tab"))
        await wait_for(lambda: tabs.active == "workflow-confirm-source-tab", pilot=pilot)
        rendered_source = dialog.query_one("#workflow-confirm-source", Static).content
        assert isinstance(rendered_source, Syntax) and rendered_source.code == source
        source_scroll = dialog.query_one("#workflow-confirm-source-scroll", VerticalScroll)
        await wait_for(lambda: source_scroll.max_scroll_y > 0 and source_scroll.max_scroll_x > 0, pilot=pilot)
        source_scroll.scroll_end(animate=False)
        assert info_scroll.scroll_y > 0  # Switching tabs does not reset the other reading position.
        action = dialog.query_one("#workflow-confirm-yes" if accept else "#workflow-confirm-no", Button)
        await wait_for(
            lambda: (
                action.region.width > 0
                and action.region.bottom <= app.size.height
                and app.get_widget_at(action.region.x, action.region.y)[0] is action
            ),
            pilot=pilot,
        )
        await click_when_settled(pilot, action)
        await wait_for(lambda: decisions == [accept], pilot=pilot)


async def test_both_source_views_neutralize_terminal_controls_but_preserve_whitespace_and_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workdir = make_workdir(tmp_path)
    monkeypatch.chdir(workdir)
    source = 'if True:\r\n\tvalue = "\x1b[24Dmasked\x07\x9b"\r\n# trailing\r'.encode()
    compile(source, "display-only.py", "exec")  # Control characters can occur in valid Python source.
    write_workflow(workdir, "display", source)
    inspection = WorkflowInspection.read(read_source(workdir / ".chrys/workflows/display.py", "workdir", layout="file"))
    app = make_chrys_app(tmp_path / "sessions")
    async with app.run_test(size=(100, 32)) as pilot:
        dialog = WorkflowConfirmDialog(inspection)
        await app.push_screen(dialog)
        dialog.query_one(TabbedContent).active = "workflow-confirm-source-tab"
        await wait_for(lambda: dialog.query_one("#workflow-confirm-source").size.width > 0, pilot=pilot)
        confirmed_source = dialog.query_one("#workflow-confirm-source", Static).content
        assert app._main_screen is not None
        panel = app._main_screen._workflow_panel
        panel.show_code(source, differs=True)
        main_source = panel.query_one("#workflow-code-source", Static).content
        expected = 'if True:\n    value = "�[24Dmasked��"\n# trailing\n'
        for rendered in (confirmed_source, main_source):
            assert isinstance(rendered, Syntax) and rendered.code == expected
            buffer = StringIO()
            Console(file=buffer, force_terminal=True, no_color=True, width=100).print(rendered)
            assert "\x1b[24D" not in buffer.getvalue() and "\x9b" not in buffer.getvalue()
        assert inspection.source.source == source
        assert inspection.source.entry_sha256 == sha256(source).hexdigest()
        await pilot.press("escape")


@pytest.mark.parametrize(
    ("layout", "note"),
    [
        ("file", "Trust allows this file and its selected interpreter"),
        ("package", "Trust allows this workflow's folder and its selected interpreter"),
    ],
)
async def test_the_trust_note_says_what_a_confirmation_covers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, layout: str, note: str
) -> None:
    workdir = make_workdir(tmp_path)
    monkeypatch.chdir(workdir)
    body = python_workflow("def fn(value):\n    return value\n", "fn")
    if layout == "package":
        entry = write_workflow_package(workdir, "kit", body, {"helpers.py": b"VALUE = 1\n"})
    else:
        entry = write_workflow(workdir, "kit", body)
    inspection = WorkflowInspection.read(read_source(entry, "workdir", layout=layout))
    app = make_chrys_app(tmp_path / "sessions")
    async with app.run_test(size=(100, 32)) as pilot:
        dialog = WorkflowConfirmDialog(inspection)
        await app.push_screen(dialog)
        await wait_for(lambda: bool(dialog.query("#workflow-info-description")), pilot=pilot)

        shown = str(dialog.query_one("#workflow-info-description", Static).content)

        assert shown.startswith(note)
        assert ("Source shows only the entry file" in shown) is (layout == "package")
