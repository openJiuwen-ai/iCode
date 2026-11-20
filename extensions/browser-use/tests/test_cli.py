# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import importlib.metadata
from pathlib import Path

import pytest

from browser_use import cli, distribution


def _pack(root: Path) -> Path:
    pack = root / "extensions" / "browser"
    (pack / "browser_use").mkdir(parents=True)
    (pack / "__init__.py").write_text("extension = None\n", encoding="utf-8")
    (pack / "browser_use" / "__init__.py").write_text("", encoding="utf-8")
    return pack


def _no_entry_points(monkeypatch) -> None:
    monkeypatch.setattr(importlib.metadata, "entry_points", lambda **_: ())


def _wheel(monkeypatch) -> None:
    monkeypatch.setattr(
        importlib.metadata,
        "entry_points",
        lambda **_: (
            importlib.metadata.EntryPoint(
                name="browser", value="browser_use.extension:extension", group=distribution.ENTRY_POINT_GROUP
            ),
        ),
    )


def test_help_and_unknown_commands(capsys):
    assert cli.main([]) == 0
    printed = capsys.readouterr().out
    assert "browser-use doctor" in printed
    assert "--bridge" in printed

    assert cli.main(["bogus"]) == 2
    assert "unknown command" in capsys.readouterr().err


def test_doctor_is_delegated_with_its_own_flags(monkeypatch):
    seen: list[list[str]] = []

    def fake(argv):
        seen.append(list(argv))
        return 7

    monkeypatch.setattr(cli.diagnose, "main", fake)
    assert cli.main(["doctor", "--json", "--connect"]) == 7
    assert seen == [["--json", "--connect"]]


def test_install_copies_a_pack_and_uninstall_removes_only_it(tmp_path, monkeypatch, capsys):
    _no_entry_points(monkeypatch)
    source = _pack(tmp_path / "export")
    config = tmp_path / "config"
    state = config / "state"
    (state / "chrome").mkdir(parents=True)
    (state / "journal.sqlite3").write_bytes(b"journal")

    assert cli.main(["install", "--source", str(source), "--config-dir", str(config)]) == 0
    target = config / "extensions" / "browser"
    assert (target / "__init__.py").is_file()
    assert "installed:" in capsys.readouterr().out

    assert cli.main(["install", "--source", str(source), "--config-dir", str(config)]) == 1
    assert "already exists" in capsys.readouterr().err

    assert cli.main(["uninstall", "--config-dir", str(config)]) == 0
    assert not target.exists()
    assert (state / "chrome").is_dir()
    assert (state / "journal.sqlite3").read_bytes() == b"journal"
    assert "kept:" in capsys.readouterr().out

    assert cli.main(["uninstall", "--config-dir", str(config)]) == 0
    assert "no folder package" in capsys.readouterr().out


def test_install_without_a_source_explains_the_distribution(tmp_path, capsys):
    assert cli.main(["install", "--config-dir", str(tmp_path)]) == 2
    assert "already loads" in capsys.readouterr().err


def test_install_refuses_to_shadow_the_wheel(tmp_path, monkeypatch, capsys):
    _wheel(monkeypatch)
    source = _pack(tmp_path / "export")

    assert cli.main(["install", "--source", str(source), "--config-dir", str(tmp_path / "config")]) == 1
    assert "already installed" in capsys.readouterr().err
    assert not (tmp_path / "config" / "extensions" / "browser").exists()


def test_uninstall_removes_the_folder_and_reports_a_remaining_wheel(tmp_path, monkeypatch, capsys):
    _wheel(monkeypatch)
    _pack(tmp_path)

    # The id is still provided by the wheel afterwards, so the command reports that.
    assert cli.main(["uninstall", "--config-dir", str(tmp_path)]) == 1
    captured = capsys.readouterr()
    assert "removed:" in captured.out
    assert "uv pip uninstall browser-use" in captured.err
    assert not (tmp_path / "extensions" / "browser").exists()

    assert cli.main(["uninstall", "--config-dir", str(tmp_path)]) == 1
    assert "no folder package" in capsys.readouterr().out


def test_uninstall_reports_filesystem_failures(tmp_path, monkeypatch, capsys):
    def refuse(**_: object) -> list[Path]:
        raise PermissionError("browser folder is in use")

    monkeypatch.setattr(cli.distribution, "uninstall_pack", refuse)
    assert cli.main(["uninstall", "--config-dir", str(tmp_path)]) == 1
    captured = capsys.readouterr()
    assert "in use" in captured.err
    assert "Traceback" not in captured.err


@pytest.mark.parametrize("command", ["install", "uninstall"])
def test_pack_commands_reject_unknown_flags(command, tmp_path):
    with pytest.raises(SystemExit) as info:
        cli.main([command, "--nope", "--config-dir", str(tmp_path)])
    assert info.value.code == 2
