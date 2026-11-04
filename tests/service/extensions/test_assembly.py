# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Extension tools as every agent build assembles them: last, after all its other tools."""

from __future__ import annotations

import sys
import threading
from typing import TYPE_CHECKING

import pytest

from chrys.foundation.i18n.formatting import format_message
from chrys.foundation.platform import get_platform
from chrys.service import extensions
from chrys.service.tools.packs import ExtensionTools, assemble_extension_tools, assemble_tools
from tests.service.extensions._support import extension_build
from tests.support.extensions import EXAMPLE_EXTENSION_ID, install_distribution_extension, install_file_extension

if TYPE_CHECKING:
    from pathlib import Path

_EXAMPLE_TOOLS = ["acme_project_name", "acme_add_release_note"]


def _extension_naming(*names: str, marker: str = "") -> str:
    """An extension source whose tools have *names*; importing it appends a line to *marker*."""
    lines = ["from pathlib import Path", "from chrys.extensions import Extension", "extension = Extension()"]
    if marker:
        lines.append(f"with open({marker!r}, 'a', encoding='utf-8') as f: f.write('imported\\n')")
    for index, name in enumerate(names):
        lines += [f"@extension.tool(name={name!r})", f"async def tool_{index}() -> str:", "    return ''"]
    return "\n".join(lines) + "\n"


async def test_extensions_load_in_the_profile_s_order(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    install_file_extension(monkeypatch)
    install_file_extension(monkeypatch, "acme_ci", _extension_naming("acme_ci_status"))
    added = await assemble_extension_tools(
        extension_build(tmp_path), ["acme_ci", EXAMPLE_EXTENSION_ID], taken=["sleep"]
    )
    assert [tool.name for tool in added.tools] == ["acme_ci_status", *_EXAMPLE_TOOLS]
    assert added.by_extension == {"acme_ci": ["acme_ci_status"], EXAMPLE_EXTENSION_ID: _EXAMPLE_TOOLS}
    assert (added.unavailable, added.warnings) == ({}, ())


async def test_icode_s_own_tools_assemble_without_looking_for_extensions(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    install_file_extension(monkeypatch)

    def fail(_extensions_dir: object = None) -> extensions.ExtensionCatalog:
        raise AssertionError("discovered extensions")

    monkeypatch.setattr(extensions, "discover_extensions", fail)
    assembled = await assemble_tools(extension_build(tmp_path, builtins=["todo"]))
    assert [tool.name for tool in assembled.tools] == ["todo_write", "sleep"]
    assert assembled.by_pack == {"todo": ["todo_write"], "sleep": ["sleep"]}
    assert assembled.warnings == ()


async def test_a_distribution_extension_loads_like_a_file_extension(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    install_distribution_extension(monkeypatch, tmp_path / "site")
    added = await assemble_extension_tools(extension_build(tmp_path), [EXAMPLE_EXTENSION_ID], taken=[])
    assert added.by_extension == {EXAMPLE_EXTENSION_ID: _EXAMPLE_TOOLS}


async def test_only_the_enabled_extensions_are_imported_off_the_event_loop_and_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    enabled, other = tmp_path / "enabled.log", tmp_path / "other.log"
    source = _extension_naming("acme_one", marker=str(enabled)).replace(
        "extension = Extension()",
        "import threading\nextension = Extension()\nIMPORT_THREAD = threading.current_thread()",
    )
    install_file_extension(monkeypatch, "acme_one", source)
    install_file_extension(monkeypatch, "acme_two", _extension_naming("acme_two", marker=str(other)))
    for _ in range(2):
        added = await assemble_extension_tools(extension_build(tmp_path), ["acme_one"], taken=[])
        assert added.by_extension == {"acme_one": ["acme_one"]}
    assert enabled.read_text(encoding="utf-8") == "imported\n"
    assert not other.exists()
    assert sys.modules["_chrys_extension_acme_one"].IMPORT_THREAD is not threading.main_thread()


async def test_a_profile_without_extensions_never_looks_for_them(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def fail(_extensions_dir: object = None) -> extensions.ExtensionCatalog:
        raise AssertionError("discovered extensions")

    monkeypatch.setattr(extensions, "discover_extensions", fail)
    added = await assemble_extension_tools(extension_build(tmp_path), [], taken=[])
    assert added == ExtensionTools()


@pytest.mark.parametrize(
    ("source", "reason", "ran"),
    [
        ("raise RuntimeError('boom')\n", "importing it failed: RuntimeError: boom", False),
        ("import sys\nsys.exit(3)\n", "importing it failed: SystemExit: 3", False),
        ("x = (\n", "importing it failed: SyntaxError:", False),
        (
            "tools = 1\n",
            "importing it failed: TypeError: it must provide 'extension = Extension()', not a NoneType",
            True,
        ),
        (
            "extension = object()\n",
            "importing it failed: TypeError: it must provide 'extension = Extension()', not a object",
            True,
        ),
    ],
    ids=["raises", "exits", "syntax", "no-extension", "not-an-extension"],
)
async def test_an_extension_that_cannot_import_is_left_out_and_the_agent_starts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, source: str, reason: str, ran: bool
) -> None:
    install_file_extension(monkeypatch, "acme_bad", source)
    install_file_extension(monkeypatch)
    added = await assemble_extension_tools(extension_build(tmp_path), ["acme_bad", EXAMPLE_EXTENSION_ID], taken=[])
    assert added.by_extension == {EXAMPLE_EXTENSION_ID: _EXAMPLE_TOOLS}
    assert added.unavailable["acme_bad"].startswith(reason)
    # A module that stopped part-way through leaves nothing behind.
    assert ("_chrys_extension_acme_bad" in sys.modules) is ran


async def test_extensions_that_are_missing_or_installed_twice_are_left_out(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    module = install_file_extension(monkeypatch)
    install_distribution_extension(monkeypatch, tmp_path / "site")
    added = await assemble_extension_tools(extension_build(tmp_path), ["acme_gone", EXAMPLE_EXTENSION_ID], taken=[])
    assert added.by_extension == {}
    assert added.unavailable == {
        "acme_gone": "it is not installed",
        EXAMPLE_EXTENSION_ID: f"it is installed more than once (acme-icode-tools 1.0; {module})",
    }


@pytest.mark.parametrize(
    ("name", "taken"),
    [("read_file", []), (get_platform().shell.name, []), ("lookup", ["lookup"])],
    ids=["icode-tool-not-loaded", "shell", "another-tool-of-the-agent"],
)
async def test_an_extension_whose_tool_takes_a_name_in_use_is_left_out_whole(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, name: str, taken: list[str]
) -> None:
    install_file_extension(monkeypatch, "acme_clash", _extension_naming("acme_fine", name))
    added = await assemble_extension_tools(extension_build(tmp_path), ["acme_clash"], taken=taken)
    assert added.tools == []
    assert added.unavailable == {"acme_clash": f"its tools {name} have names already in use"}


async def test_a_later_extension_cannot_reuse_an_earlier_extension_s_name(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    install_file_extension(monkeypatch, "acme_first", _extension_naming("acme_shared"))
    install_file_extension(monkeypatch, "acme_second", _extension_naming("acme_shared", "acme_own"))
    added = await assemble_extension_tools(extension_build(tmp_path), ["acme_first", "acme_second"], taken=[])
    assert added.by_extension == {"acme_first": ["acme_shared"]}
    assert set(added.unavailable) == {"acme_second"}


async def test_each_extension_left_out_is_one_warning_for_this_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    install_file_extension(monkeypatch, "acme_bad", "raise RuntimeError('boom')\n")
    added = await assemble_extension_tools(
        extension_build(tmp_path, session_id="s-9"), ["acme_bad", "acme_gone"], taken=[]
    )
    assert [(w.code, w.session_id) for w in added.warnings] == [("extension_unavailable", "s-9")] * 2
    assert added.warnings[1].message == "Agent Coder starts without extension acme_gone: it is not installed"
    assert format_message(added.warnings[1].display_message) == added.warnings[1].message
    assert "Agent Coder starts without extension acme_bad: importing it failed" in caplog.text
