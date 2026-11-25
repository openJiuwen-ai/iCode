# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Focused repro tests for tool-runtime bugs found during repo inspection."""

from __future__ import annotations

import contextlib
import sys
from typing import TYPE_CHECKING

from chrys.foundation.platform import script_commands
from chrys.service.skills.model import Skill, SkillScript
from chrys.service.skills.runner import SubprocessScriptRunner
from chrys.service.tools.builtins.shell_filter import ShellCommandFilter, is_safe_readonly_command, parse_commands
from tests.support.processes import ExitedProcess

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def test_shell_parser_treats_unquoted_newline_as_command_separator() -> None:
    """Shell newlines execute separate commands and must be parsed that way."""
    command = "ls\npython3 -c 'print(1)'"

    assert parse_commands(command) == ["ls", "python3"]


def test_shell_parser_treats_unquoted_ampersand_as_command_separator() -> None:
    """A bare ampersand backgrounds one command, then executes the next command."""
    command = "ls & python3 -c 'print(1)'"

    assert parse_commands(command) == ["ls", "python3"]


def test_shell_whitelist_blocks_disallowed_command_after_newline() -> None:
    """A newline must not hide a second command from whitelist validation."""
    command_filter = ShellCommandFilter(whitelist=frozenset({"ls"}))

    result = command_filter.validate("ls\npython3 -c 'print(1)'")

    assert not result.allowed
    assert "python3" in result.reason


def test_safe_readonly_rejects_unsafe_command_after_newline() -> None:
    """Approval auto-bypass must not classify newline-injected commands as read-only."""
    command = 'ls\npython3 -c \'open("/tmp/chrys_probe", "w").write("x")\''

    assert not is_safe_readonly_command(command)


async def test_skill_runner_prepends_positional_arguments_before_keyword_flags(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Subcommand dispatch needs positional arguments before keyword flags."""
    skill_dir = tmp_path / "skill"
    skill_dir.mkdir()
    script_path = skill_dir / "run.py"
    script_path.write_text("print('unused')\n", encoding="utf-8")

    skill = Skill(name="skill", description="test", content="body", path=str(skill_dir))
    script = SkillScript(name="run.py", full_path=str(script_path))
    captured_cmd: list[str] = []

    @contextlib.asynccontextmanager
    async def fake_managed_subprocess(*cmd: object, **_kwargs: object):
        captured_cmd[:] = [str(part) for part in cmd]

        yield ExitedProcess(b"ok\n")

    monkeypatch.setattr("chrys.service.skills.runner.managed_subprocess", fake_managed_subprocess)
    monkeypatch.setattr(script_commands, "_find_python_runner", lambda: [sys.executable])

    result = await SubprocessScriptRunner()(skill, script, args={"mode": "fast"}, arguments=["input.txt"])

    assert result == "ok"
    script_index = captured_cmd.index(str(script_path))
    assert captured_cmd[script_index + 1 :] == ["input.txt", "--mode", "fast"]
