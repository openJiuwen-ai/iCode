# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Imported cwd values preserve source path and template semantics."""

from __future__ import annotations

import json
import ntpath
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from chrys.service.mcp.cache import resolve_stdio_cwd
from chrys.service.mcp.importers import ImportContext, ImportWarning, Problem, WarningKind, base, codex, opencode, pi
from chrys.service.mcp.importers.codex import CodexSource
from chrys.service.mcp.importers.opencode import OpenCodeSource
from chrys.service.mcp.importers.pi import PiSource
from tests.support.symlinks import symlink_or_skip


def discover(home: Path, source: str, fields: dict[str, object]):
    if source == "codex":
        path = home / ".codex" / "config.toml"
        text = '[mcp_servers.srv]\ncommand = "run"\n' + "".join(
            f"{key} = {json.dumps(value)}\n" for key, value in fields.items()
        )
        importer = CodexSource()
    elif source == "opencode":
        path = home / ".config" / "opencode" / "opencode.json"
        text = json.dumps({"mcp": {"srv": {"type": "local", "command": ["run"], **fields}}})
        importer = OpenCodeSource()
    else:
        path = home / ".pi" / "agent" / "mcp.json"
        text = json.dumps({"mcpServers": {"srv": {"command": "run", **fields}}})
        importer = PiSource()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return importer.discover(ImportContext(str(home / "workspace"), home, {}))


def set_source_platform(monkeypatch: pytest.MonkeyPatch, *, windows: bool) -> None:
    for module in (base, codex, opencode, pi):
        monkeypatch.setattr(module, "get_platform", lambda: SimpleNamespace(is_windows=windows))


@pytest.mark.parametrize("source", ["codex", "opencode", "pi"])
@pytest.mark.parametrize("cwd", [None, ".", "tools/server", "absolute"])
def test_absent_relative_and_absolute_paths(home_path: Path, source: str, cwd: str | None) -> None:
    if cwd == "absolute":
        cwd = str(home_path / "fixed")
    (candidate,) = discover(home_path, source, {} if cwd is None else {"cwd": cwd}).candidates

    assert candidate.config is not None
    assert candidate.config.cwd == (os.path.normpath(cwd) if cwd and source != "codex" else cwd or "")
    assert candidate.warnings == ()


@pytest.fixture
def home_path(tmp_path: Path) -> Path:
    return tmp_path / "home"


@pytest.mark.parametrize("source", ["codex", "opencode", "pi"])
def test_empty_is_distinct_from_absent(home_path: Path, source: str) -> None:
    (candidate,) = discover(home_path, source, {"cwd": ""}).candidates
    if source == "codex":
        assert (candidate.config, candidate.problem, candidate.problem_field) == (
            None,
            Problem.VALUE_NOT_REPRESENTABLE,
            "mcp_servers.srv.cwd",
        )
    else:
        assert candidate.config is not None
        assert candidate.config.cwd == ""
        assert candidate.warnings == ()


@pytest.mark.parametrize("source", ["codex", "opencode", "pi"])
@pytest.mark.parametrize(
    "cwd",
    [
        " ",
        " tools",
        "tools ",
        "x\0y",
        "x\ny",
        "x\ry",
        "x\ty",
        "x\x1by",
        "x\x7fy",
        "x\x85y",
        "x\u202ey",
        "x\u2066y",
        "x\u200fy",
        "x\u061cy",
        "x\u2028y",
        "bad/\x1b/../server",
    ],
)
def test_unrepresentable_paths_name_the_source_field(home_path: Path, source: str, cwd: str) -> None:
    (candidate,) = discover(home_path, source, {"cwd": cwd}).candidates
    prefix = {"codex": "mcp_servers", "opencode": "mcp", "pi": "mcpServers"}[source]
    assert (candidate.config, candidate.problem, candidate.problem_field) == (
        None,
        Problem.VALUE_NOT_REPRESENTABLE,
        f"{prefix}.srv.cwd",
    )


@pytest.mark.parametrize("source", ["codex", "opencode", "pi"])
@pytest.mark.parametrize("cwd", [0, 1, [], False, True, {}])
def test_invalid_cwd_types_are_rejected_by_the_source(home_path: Path, source: str, cwd: object) -> None:
    assert discover(home_path, source, {"cwd": cwd}).candidates == ()


def test_pi_rejects_null_cwd_before_applying_its_runtime_default(home_path: Path) -> None:
    assert discover(home_path, "pi", {"cwd": None}).candidates == ()


@pytest.mark.parametrize("source", ["codex", "opencode", "pi"])
@pytest.mark.parametrize("cwd", ["{{VAR}}", "{{!command}}", "$VAR", "${VAR}", "!command"])
def test_source_literals_never_activate_templates(home_path: Path, source: str, cwd: str) -> None:
    (candidate,) = discover(home_path, source, {"cwd": cwd}).candidates
    assert candidate.config is not None
    assert resolve_stdio_cwd(candidate.config.cwd, str(home_path)) == os.path.join(home_path, cwd)


@pytest.mark.parametrize("source", ["codex", "opencode", "pi"])
@pytest.mark.parametrize("cwd", ["~", "~/srv", "~other/srv"])
def test_home_prefixes_follow_each_source(home_path: Path, source: str, cwd: str) -> None:
    (candidate,) = discover(home_path, source, {"cwd": cwd}).candidates
    assert candidate.config is not None
    if source == "pi" and cwd in ("~", "~/srv"):
        expected = str(home_path) if cwd == "~" else str(home_path / "srv")
        assert candidate.config.cwd == expected
    else:
        assert candidate.config.cwd == "./" + (os.path.normpath(cwd) if source != "codex" else cwd)
        assert os.path.normpath(resolve_stdio_cwd(candidate.config.cwd, str(home_path))) == os.path.normpath(
            home_path / cwd
        )


def test_opencode_cwd_variables_are_converted_and_required(home_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (candidate,) = discover(
        home_path, "opencode", {"cwd": "{env:ROOT}/srv", "environment": {"X": "{env:ROOT}"}}
    ).candidates
    assert candidate.config is not None
    assert candidate.config.cwd == "{{ROOT}}/srv"
    assert candidate.warnings == (ImportWarning(WarningKind.OPENCODE_VARIABLE_CWD, ("mcp.srv.cwd",)),)
    monkeypatch.setenv("ROOT", str(home_path / "fixed"))
    assert resolve_stdio_cwd(candidate.config.cwd, str(home_path)) == str(home_path / "fixed" / "srv")


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlink traversal differs from lexical normalization")
@pytest.mark.parametrize("value", ["~/server", "link/.."])
def test_opencode_warns_when_future_variable_values_change_directory(
    home_path: Path, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    (home_path / "workspace").mkdir(parents=True)
    (home_path / "outside" / "child").mkdir(parents=True)
    symlink_or_skip(home_path / "workspace" / "link", home_path / "outside" / "child", target_is_directory=True)
    monkeypatch.setenv("HOME", str(home_path))
    monkeypatch.delenv("ROOT", raising=False)
    (candidate,) = discover(home_path, "opencode", {"cwd": "{env:ROOT}"}).candidates
    assert candidate.config is not None
    assert candidate.config.cwd == "{{ROOT}}"
    assert ImportWarning(WarningKind.OPENCODE_VARIABLE_CWD, ("mcp.srv.cwd",)) in candidate.warnings

    # The warning must cover values supplied after import, without capturing them.
    monkeypatch.setenv("ROOT", value)
    actual = Path(resolve_stdio_cwd(candidate.config.cwd, str(home_path / "workspace"))).resolve()
    source = Path(os.path.normpath(home_path / "workspace" / value))
    assert actual == (home_path / "server" if value.startswith("~") else home_path / "outside").resolve()
    assert actual != source.resolve()


@pytest.mark.parametrize(
    ("cwd", "problem"),
    [
        ("{env:ROOT}/../srv", Problem.VALUE_NOT_REPRESENTABLE),
        ("{file:secret}", Problem.VALUE_FROM_FILE_OR_COMMAND),
        ("{env:not-a-name}", Problem.VALUE_NOT_REPRESENTABLE),
        ("{{env:ROOT}", Problem.VALUE_NOT_REPRESENTABLE),
        ("{env:ROOT}}}", Problem.VALUE_NOT_REPRESENTABLE),
    ],
)
def test_opencode_refusals_include_cwd(home_path: Path, cwd: str, problem: Problem) -> None:
    (candidate,) = discover(home_path, "opencode", {"cwd": cwd}).candidates
    assert (candidate.config, candidate.problem, candidate.problem_field) == (None, problem, "mcp.srv.cwd")


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlink traversal differs from lexical normalization")
@pytest.mark.parametrize("source", ["codex", "opencode", "pi"])
def test_dotdot_is_normalized_only_by_sources_that_do_so(home_path: Path, source: str) -> None:
    (home_path / "elsewhere" / "child").mkdir(parents=True)
    symlink_or_skip(home_path / "link", home_path / "elsewhere" / "child", target_is_directory=True)
    (candidate,) = discover(home_path, source, {"cwd": "link/.."}).candidates
    assert candidate.config is not None
    expected = home_path / "elsewhere" if source == "codex" else home_path
    assert candidate.config.cwd == ("link/.." if source == "codex" else ".")
    assert os.path.samefile(resolve_stdio_cwd(candidate.config.cwd, str(home_path)), expected)


@pytest.mark.parametrize(
    ("cwd", "expected"),
    [
        (r"D:\servers\..\mcp", r"D:\mcp"),
        (r"tools\..\server", "server"),
        (r"\\host\share\a\..\server", r"\\host\share\server"),
        ("~/server", r"~\server"),
    ],
)
def test_windows_lexical_paths_on_every_host(monkeypatch: pytest.MonkeyPatch, cwd: str, expected: str) -> None:
    monkeypatch.setattr(base, "get_platform", lambda: SimpleNamespace(is_windows=True))
    assert base.lexical_cwd(cwd) == expected


@pytest.mark.parametrize("source", ["codex", "opencode", "pi"])
@pytest.mark.parametrize("windows", [False, True])
@pytest.mark.parametrize("cwd", ["C:tools", "C:", r"d:server\sub"])
def test_drive_relative_cwd_is_refused_only_on_windows(
    home_path: Path, monkeypatch: pytest.MonkeyPatch, source: str, windows: bool, cwd: str
) -> None:
    set_source_platform(monkeypatch, windows=windows)
    (candidate,) = discover(home_path, source, {"cwd": cwd}).candidates
    if windows:
        prefix = {"codex": "mcp_servers", "opencode": "mcp", "pi": "mcpServers"}[source]
        assert (candidate.config, candidate.problem, candidate.problem_field) == (
            None,
            Problem.VALUE_NOT_REPRESENTABLE,
            f"{prefix}.srv.cwd",
        )
    else:
        assert candidate.config is not None


@pytest.mark.parametrize("source", ["codex", "opencode", "pi"])
@pytest.mark.parametrize("cwd", [r"C:\tools", "D:/server", r"\\host\share\server", r"\tools", "tools"])
def test_supported_windows_cwd_remains_importable(
    home_path: Path, monkeypatch: pytest.MonkeyPatch, source: str, cwd: str
) -> None:
    set_source_platform(monkeypatch, windows=True)
    (candidate,) = discover(home_path, source, {"cwd": cwd}).candidates
    assert candidate.config is not None


def test_opencode_variable_drive_path_keeps_reference_with_warning(
    home_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_source_platform(monkeypatch, windows=True)
    (candidate,) = discover(home_path, "opencode", {"cwd": "C:{env:ROOT}"}).candidates
    assert candidate.config is not None
    assert candidate.config.cwd == "C:{{ROOT}}"
    assert ImportWarning(WarningKind.OPENCODE_VARIABLE_CWD, ("mcp.srv.cwd",)) in candidate.warnings


@pytest.mark.parametrize("windows", [False, True])
def test_pi_backslash_home_prefix_matches_runtime_on_each_platform(
    home_path: Path, monkeypatch: pytest.MonkeyPatch, windows: bool
) -> None:
    set_source_platform(monkeypatch, windows=windows)
    (candidate,) = discover(
        home_path, "pi", {"command": r"~\bin\run", "args": [r"~\data"], "cwd": r"~\server"}
    ).candidates
    assert candidate.config is not None
    if windows:
        assert candidate.config.cwd == ntpath.normpath(str(home_path) + r"\server")
        assert candidate.config.command == ntpath.normpath(str(home_path) + r"\bin\run")
        assert candidate.config.args == [ntpath.normpath(str(home_path) + r"\data")]
    else:
        assert candidate.config.cwd == r"./~\server"
        assert candidate.config.command == r"~\bin\run"
        assert candidate.config.args == [r"~\data"]


@pytest.mark.parametrize("windows", [False, True])
def test_opencode_variable_parent_components_follow_platform_separators(
    home_path: Path, monkeypatch: pytest.MonkeyPatch, windows: bool
) -> None:
    set_source_platform(monkeypatch, windows=windows)
    (candidate,) = discover(home_path, "opencode", {"cwd": r"{env:ROOT}\..\srv"}).candidates
    if windows:
        assert (candidate.config, candidate.problem, candidate.problem_field) == (
            None,
            Problem.VALUE_NOT_REPRESENTABLE,
            "mcp.srv.cwd",
        )
    else:
        assert candidate.config is not None
        assert candidate.config.cwd == r"{{ROOT}}\..\srv"


def test_opencode_leading_literal_tilde_is_shielded_before_a_variable(home_path: Path) -> None:
    (candidate,) = discover(home_path, "opencode", {"cwd": "~/{env:DIR}"}).candidates
    assert candidate.config is not None
    assert candidate.config.cwd == "./~/{{DIR}}"
