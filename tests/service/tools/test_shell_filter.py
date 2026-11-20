# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for shell command parser and filter."""

from __future__ import annotations

import pytest

from chrys.foundation.models.session_env import SessionEnvironment
from chrys.service.tools.builtins.shell import ShellTools
from chrys.service.tools.builtins.shell_filter import (
    READONLY,
    UNRESTRICTED,
    ShellCommandFilter,
    is_safe_readonly_command,
    parse_commands,
)

# ───────────────────────── Command parser ─────────────────────────────


def test_parse_simple() -> None:
    assert parse_commands("ls -la") == ["ls"]


def test_parse_pipe() -> None:
    assert parse_commands("git log | head") == ["git", "head"]


def test_parse_chain_and() -> None:
    assert parse_commands("cd /tmp && ls") == ["cd", "ls"]


def test_parse_chain_or() -> None:
    assert parse_commands("cmd1 || cmd2") == ["cmd1", "cmd2"]


def test_parse_semicolon() -> None:
    assert parse_commands("echo a; echo b") == ["echo", "echo"]


def test_parse_background_operator() -> None:
    assert parse_commands("ls & python3 script.py") == ["ls", "python3"]


def test_parse_fd_redirect_does_not_treat_ampersand_as_separator() -> None:
    assert parse_commands("echo hi 2>&1") == ["echo"]


def test_parse_mixed() -> None:
    assert parse_commands("git log | grep fix && echo done") == ["git", "grep", "echo"]


def test_parse_quoted_pipe() -> None:
    assert parse_commands("echo 'a|b'") == ["echo"]


def test_parse_quoted_ampersand() -> None:
    assert parse_commands('echo "a&&b"') == ["echo"]


def test_parse_env_var() -> None:
    assert parse_commands("KEY=val command arg") == ["command"]


def test_parse_multiple_env_vars() -> None:
    assert parse_commands("A=1 B=2 python3 script.py") == ["python3"]


def test_parse_empty() -> None:
    assert parse_commands("") == []


def test_parse_whitespace_only() -> None:
    assert parse_commands("   ") == []


def test_parse_single_command() -> None:
    assert parse_commands("whoami") == ["whoami"]


def test_parse_escaped_pipe() -> None:
    assert parse_commands("echo a\\|b") == ["echo"]


# ───────────────────── Whitelist / blacklist ──────────────────────────


def test_whitelist_allows() -> None:
    f = ShellCommandFilter(whitelist=frozenset({"ls", "cat"}))
    result = f.validate("ls -la")
    assert result.allowed


def test_whitelist_blocks() -> None:
    f = ShellCommandFilter(whitelist=frozenset({"ls", "cat"}))
    result = f.validate("rm -rf /")
    assert not result.allowed
    assert "'rm'" in result.reason


def test_blacklist_allows() -> None:
    f = ShellCommandFilter(blacklist=frozenset({"rm", "dd"}))
    result = f.validate("ls -la")
    assert result.allowed


def test_blacklist_blocks() -> None:
    f = ShellCommandFilter(blacklist=frozenset({"rm", "dd"}))
    result = f.validate("rm file.txt")
    assert not result.allowed
    assert "'rm'" in result.reason


def test_pipe_all_must_pass() -> None:
    f = ShellCommandFilter(whitelist=frozenset({"ls", "grep"}))
    result = f.validate("ls | rm")
    assert not result.allowed
    assert "'rm'" in result.reason


def test_pipe_all_allowed() -> None:
    f = ShellCommandFilter(whitelist=frozenset({"git", "head"}))
    result = f.validate("git log | head -20")
    assert result.allowed


# ──────────────────── Redirections ────────────────────────────────────


def test_redirect_blocked() -> None:
    f = ShellCommandFilter(allow_redirections=False)
    result = f.validate("echo foo > file.txt")
    assert not result.allowed
    assert "Redirection" in result.reason


def test_redirect_append_blocked() -> None:
    f = ShellCommandFilter(allow_redirections=False)
    result = f.validate("echo foo >> file.txt")
    assert not result.allowed


def test_redirect_input_blocked() -> None:
    f = ShellCommandFilter(allow_redirections=False)
    result = f.validate("sort < input.txt")
    assert not result.allowed


def test_redirect_allowed() -> None:
    f = ShellCommandFilter(allow_redirections=True)
    result = f.validate("echo foo > file.txt")
    assert result.allowed


def test_redirect_in_quotes_not_blocked() -> None:
    f = ShellCommandFilter(allow_redirections=False)
    result = f.validate("echo 'a > b'")
    assert result.allowed


# ──────────────────── Subshells ───────────────────────────────────────


def test_subshell_blocked() -> None:
    f = ShellCommandFilter(allow_subshells=False)
    result = f.validate("echo $(whoami)")
    assert not result.allowed
    assert "Subshell" in result.reason


def test_backtick_blocked() -> None:
    f = ShellCommandFilter(allow_subshells=False)
    result = f.validate("echo `whoami`")
    assert not result.allowed


def test_subshell_allowed() -> None:
    f = ShellCommandFilter(allow_subshells=True)
    result = f.validate("echo $(whoami)")
    assert result.allowed


def test_subshell_in_quotes_not_blocked() -> None:
    f = ShellCommandFilter(allow_subshells=False)
    result = f.validate("echo '$(not a subshell)'")
    assert result.allowed


# ──────────────────── Predefined presets ──────────────────────────────


def test_readonly_allows_ls() -> None:
    assert READONLY.validate("ls -la").allowed


def test_readonly_allows_git() -> None:
    assert READONLY.validate("git log --oneline").allowed


def test_readonly_allows_pipe() -> None:
    assert READONLY.validate("git log | head -20").allowed


def test_readonly_blocks_rm() -> None:
    result = READONLY.validate("rm -rf /")
    assert not result.allowed


def test_readonly_blocks_redirect() -> None:
    result = READONLY.validate("echo data > file.txt")
    assert not result.allowed


def test_readonly_blocks_subshell() -> None:
    result = READONLY.validate("echo $(rm -rf /)")
    assert not result.allowed


def test_readonly_blocks_mkdir() -> None:
    result = READONLY.validate("mkdir new_dir")
    assert not result.allowed


def test_readonly_blocks_mv() -> None:
    result = READONLY.validate("mv a.txt b.txt")
    assert not result.allowed


def test_unrestricted_allows_everything() -> None:
    assert UNRESTRICTED.validate("rm -rf /").allowed
    assert UNRESTRICTED.validate("echo > file").allowed
    assert UNRESTRICTED.validate("echo $(whoami)").allowed


# ──────────────────── Integration with ShellTools ─────────────────────


async def test_execute_with_filter_blocks() -> None:
    runtime = SessionEnvironment.capture()
    shell = ShellTools(runtime, command_filter=READONLY)
    result = await shell.execute("rm -rf /tmp/test", reason="test")
    assert "blocked" in result.lower()
    assert "'rm'" in result


async def test_execute_with_filter_allows() -> None:
    runtime = SessionEnvironment.capture()
    shell = ShellTools(runtime, command_filter=READONLY)
    result = await shell.execute("echo hello", reason="test")
    assert "hello" in result
    assert "[exit_code: 0]" in result


async def test_execute_without_filter() -> None:
    runtime = SessionEnvironment.capture()
    shell = ShellTools(runtime)  # no filter
    result = await shell.execute("echo unrestricted", reason="test")
    assert "unrestricted" in result


# ──────────────────── Safe read-only detection ──────────────────────────


@pytest.mark.parametrize(
    "command",
    [
        "rg --pre ./x.sh -e y .",
        "rg --pre=./x.sh -e y .",
        "rg --pre$IFS./x.sh -e y .",
        "rg -n foo .",
        "less README.md",
        "more README.md",
        "tree -o out .",
        "date -s 2020-01-01",
        "hostname evil",
    ],
)
def test_legacy_readonly_check_rejects_exec_or_mutation_commands(command: str) -> None:
    """The legacy best-effort predicate must not label exec/mutation surfaces as read-only.

    Approval no longer uses this function, but keeping it permissive would invite a
    future caller to reintroduce the same bypass.
    """
    assert not is_safe_readonly_command(command)


def test_safe_readonly_simple_ls() -> None:
    assert is_safe_readonly_command("ls -la")


def test_safe_readonly_pipe_chain() -> None:
    assert is_safe_readonly_command("cat file.txt | head -20 | grep pattern")


def test_safe_readonly_pwd() -> None:
    assert is_safe_readonly_command("pwd")


def test_safe_readonly_chain_operators() -> None:
    assert is_safe_readonly_command("ls && pwd")


def test_safe_readonly_semicolon() -> None:
    assert is_safe_readonly_command("whoami; uname -a")


def test_safe_readonly_env_var_prefix() -> None:
    assert is_safe_readonly_command("LC_ALL=C sort file.txt")
    assert not is_safe_readonly_command("GIT_TRACE=/tmp/git.trace git --no-optional-locks status --short")
    assert not is_safe_readonly_command("GIT_EXTERNAL_DIFF=touch git --no-pager diff")
    assert not is_safe_readonly_command("PAGER=cat git log --oneline")
    assert not is_safe_readonly_command("PYTHONPATH=. ls")


def test_safe_readonly_env_command_rejects_child_programs() -> None:
    assert is_safe_readonly_command("env")
    assert is_safe_readonly_command("env FOO=bar LANG=C")
    assert not is_safe_readonly_command("env FOO=bar python -c \"import os; os.system('rm -rf x')\"")
    assert not is_safe_readonly_command('env FOO=bar bash -c "rm -rf x"')
    assert not is_safe_readonly_command('env LANG=C node -e "code"')
    assert not is_safe_readonly_command("env python --version")


def test_safe_readonly_tr() -> None:
    """tr is safe (stdin→stdout only, redirections blocked separately)."""
    assert is_safe_readonly_command("cat file.txt | tr '[:upper:]' '[:lower:]'")


def test_safe_readonly_cd() -> None:
    assert is_safe_readonly_command("cd /tmp && ls")


def test_safe_readonly_quoted_redirect_is_safe() -> None:
    """Redirect characters inside quotes are NOT actual redirections."""
    assert is_safe_readonly_command("echo 'a > b'")


def test_safe_readonly_quoted_subshell_is_safe() -> None:
    assert is_safe_readonly_command("echo '$(not real)'")


def test_safe_readonly_double_quoted_subshell_is_blocked() -> None:
    assert not is_safe_readonly_command('echo "$(rm file)"')
    assert not is_safe_readonly_command('Write-Output "$(Remove-Item file)"', shell_name="pwsh")


def test_safe_readonly_rejects_shell_parenthesized_execution() -> None:
    assert not is_safe_readonly_command("echo =(python3 -c 'print(1)')", shell_name="zsh")
    assert not is_safe_readonly_command("echo <(python3 -c 'print(1)')", shell_name="bash")
    assert not is_safe_readonly_command("Write-Output (python3 -c 'print(1)')", shell_name="pwsh")
    assert not is_safe_readonly_command("echo (python3 -c 'print(1)')", shell_name="fish")
    assert is_safe_readonly_command('echo "(not execution)"', shell_name="zsh")
    assert is_safe_readonly_command('Write-Output "(not execution)"', shell_name="pwsh")


def test_safe_readonly_powershell_get() -> None:
    assert is_safe_readonly_command("Get-ChildItem")


def test_safe_readonly_powershell_select_pipe() -> None:
    assert is_safe_readonly_command("Get-Process | Select-Object Name")


def test_safe_readonly_allows_readonly_git() -> None:
    assert is_safe_readonly_command("git --no-optional-locks status --short")
    assert is_safe_readonly_command(
        "git --no-pager diff --no-ext-diff --no-textconv -- src/chrys/service/tools/builtins/shell_filter.py"
    )
    assert is_safe_readonly_command("git log --oneline -5")
    assert is_safe_readonly_command("git log --no-ext-diff --no-textconv -p -1")
    assert is_safe_readonly_command("git rev-parse --show-toplevel")
    assert is_safe_readonly_command("git ls-files -- src/chrys")
    assert is_safe_readonly_command("git ls-tree HEAD")
    assert is_safe_readonly_command("git describe --tags --always")
    assert is_safe_readonly_command("git merge-base HEAD HEAD")
    assert is_safe_readonly_command("git rev-list --count HEAD")
    assert is_safe_readonly_command("git name-rev HEAD")
    assert is_safe_readonly_command("git show-ref --heads")
    assert is_safe_readonly_command("git show-branch --list")
    assert is_safe_readonly_command("git for-each-ref --format='%(refname)' refs/heads")
    assert is_safe_readonly_command("git check-attr --all -- README.md")
    assert is_safe_readonly_command("git check-ignore -v .venv")
    assert is_safe_readonly_command("git check-mailmap '<A U Thor <author@example.com>>'")
    assert is_safe_readonly_command("git grep -ni pattern")
    assert is_safe_readonly_command("git cat-file -p HEAD:README.md")
    assert is_safe_readonly_command("git show --no-ext-diff --no-textconv HEAD:pyproject.toml")
    assert is_safe_readonly_command("git branch --show-current")
    assert is_safe_readonly_command("git branch --list 'feature/*'")
    assert is_safe_readonly_command("git tag --list 'v*'")
    assert is_safe_readonly_command("git remote -v")
    assert is_safe_readonly_command("git stash list")
    assert is_safe_readonly_command("git stash show --stat")
    assert is_safe_readonly_command("git stash show --no-ext-diff --no-textconv -p")
    assert is_safe_readonly_command("git worktree list")
    assert is_safe_readonly_command("git config --global --list")


def test_safe_readonly_rejects_mutating_git() -> None:
    assert not is_safe_readonly_command("git status --short")
    assert not is_safe_readonly_command("git checkout main")
    assert not is_safe_readonly_command("git switch feature")
    assert not is_safe_readonly_command("git restore file.txt")
    assert not is_safe_readonly_command("git reset --hard")
    assert not is_safe_readonly_command("git commit -m test")
    assert not is_safe_readonly_command("git push origin main")
    assert not is_safe_readonly_command("git branch new-branch")
    assert not is_safe_readonly_command("git branch --list -D main")
    assert not is_safe_readonly_command("git branch --list -m old new")
    assert not is_safe_readonly_command("git branch -l new-branch")
    assert not is_safe_readonly_command("git tag v1.0.0")
    assert not is_safe_readonly_command("git tag --list -d v1.0.0")
    assert not is_safe_readonly_command("git tag --list -f v1.0.0")
    assert not is_safe_readonly_command("git --no-pager diff -- src/chrys/service/tools/builtins/shell_filter.py")
    assert not is_safe_readonly_command("git show HEAD")
    assert not is_safe_readonly_command("git log -p -1")
    assert not is_safe_readonly_command("git log --patch --no-ext-diff -1")
    assert not is_safe_readonly_command("git diff --output patch.txt")
    assert not is_safe_readonly_command("git diff --textconv")
    assert not is_safe_readonly_command("git diff --no-ext-diff --no-textconv --ext")
    assert not is_safe_readonly_command("git blame --textconv README.md")
    assert not is_safe_readonly_command("git grep --open-files-in-pager='rm -f' pattern")
    assert not is_safe_readonly_command("git grep --open=vim pattern")
    assert not is_safe_readonly_command("git grep --outp=f pattern")
    assert not is_safe_readonly_command("git grep -O pattern")
    assert not is_safe_readonly_command("git grep -nOvim pattern")
    assert not is_safe_readonly_command("git cat-file --filters HEAD:README.md")
    assert not is_safe_readonly_command("git stash show -p")
    assert not is_safe_readonly_command("git stash show --patch")
    assert not is_safe_readonly_command("git stash show --no-ext-diff -p")
    assert not is_safe_readonly_command("git log -p --no-ext-diff --no-textconv --textc")


def test_safe_readonly_rejects_python() -> None:
    assert not is_safe_readonly_command("python3 script.py")
    assert not is_safe_readonly_command("python3 -c 'print(1)'")
    assert is_safe_readonly_command("python3 --version")


def test_safe_readonly_rejects_curl() -> None:
    assert not is_safe_readonly_command("curl https://example.com")
    assert is_safe_readonly_command("curl --version")


def test_safe_readonly_rejects_npm() -> None:
    assert not is_safe_readonly_command("npm install")
    assert is_safe_readonly_command("npm --version")


def test_safe_readonly_rejects_sed() -> None:
    """sed can modify files with -i, not safe for auto-approve."""
    assert not is_safe_readonly_command("sed -i 's/foo/bar/' file.txt")
    assert is_safe_readonly_command("sed -n '1,120p' file.txt")
    assert is_safe_readonly_command("sed --quiet -e '1,20p' -e '30,40p' file.txt")
    assert is_safe_readonly_command("sed 's/.*\\.//'")
    assert is_safe_readonly_command("sed -e 's/foo/bar/' -e 's/baz/qux/' file.txt")
    assert not is_safe_readonly_command("sed -n 's/foo/bar/p' file.txt")
    assert not is_safe_readonly_command("sed -n '1,20p' -i file.txt")
    assert not is_safe_readonly_command("sed 's/foo/bar/w output.txt' file.txt")
    assert not is_safe_readonly_command("sed 's/foo/bar/e' file.txt")
    assert not is_safe_readonly_command("sed 's/foo/bar/;w output.txt' file.txt")


def test_safe_readonly_rejects_text_utility_output_files() -> None:
    assert is_safe_readonly_command("sort input.txt")
    assert is_safe_readonly_command("sort -k2 -n -r")
    assert not is_safe_readonly_command("sort -o out.txt in.txt")
    assert not is_safe_readonly_command("sort -oout.txt in.txt")
    assert not is_safe_readonly_command("sort -bo out.txt in.txt")
    assert not is_safe_readonly_command("sort -mo out.txt in.txt")
    assert not is_safe_readonly_command("sort -boout.txt in.txt")
    assert not is_safe_readonly_command("sort -bo/tmp/out.txt in.txt")
    assert not is_safe_readonly_command("sort --output out.txt in.txt")
    assert not is_safe_readonly_command("sort --output=out.txt in.txt")
    assert not is_safe_readonly_command("sort --out=out.txt in.txt")
    assert not is_safe_readonly_command("sort --o=out.txt in.txt")
    assert not is_safe_readonly_command("sort --compress-program=gzip input.txt")
    assert not is_safe_readonly_command("sort --compress-prog=gzip input.txt")
    assert not is_safe_readonly_command("sort --comp=gzip input.txt")
    assert is_safe_readonly_command("sort -T/home/foo input.txt")
    assert is_safe_readonly_command("sort -ko1 input.txt")
    assert is_safe_readonly_command("sort -- --output")
    assert is_safe_readonly_command("uniq")
    assert is_safe_readonly_command("uniq -c input.txt")
    assert is_safe_readonly_command("uniq --skip-fields=1 input.txt")
    assert not is_safe_readonly_command("uniq input.txt out.txt")


def test_safe_readonly_allows_narrow_awk_field_print() -> None:
    assert is_safe_readonly_command("awk '{print $1}' file.txt")
    assert is_safe_readonly_command("awk '{ print $12 }'")
    assert not is_safe_readonly_command("awk '{print}' file.txt")
    assert not is_safe_readonly_command("awk '{system(\"rm file\")}' file.txt")
    assert not is_safe_readonly_command("awk -f script.awk file.txt")


def test_safe_readonly_rejects_xargs() -> None:
    """xargs executes arbitrary commands."""
    assert not is_safe_readonly_command("ls | xargs rm")


def test_safe_readonly_rejects_yq() -> None:
    """-i flag modifies files in-place."""
    assert not is_safe_readonly_command("yq -i '.key = \"val\"' file.yaml")


def test_safe_readonly_rejects_foreach_object() -> None:
    """Script blocks can run destructive cmdlets."""
    assert not is_safe_readonly_command("Get-Process | ForEach-Object { Stop-Process $_ }")
    assert not is_safe_readonly_command("Get-Process | Where-Object {Remove-Item $_}", shell_name="pwsh")
    assert not is_safe_readonly_command("Get-Process | Where-Object {$_.Name}", shell_name="pwsh")
    assert is_safe_readonly_command('Write-Output "{not a script block}"', shell_name="pwsh")


def test_safe_readonly_new_commands() -> None:
    """Verify newly added safe commands."""
    assert is_safe_readonly_command("readlink /usr/bin/python")
    assert is_safe_readonly_command("ps aux")
    assert is_safe_readonly_command("tac file.txt")
    assert is_safe_readonly_command("md5sum file.txt")
    assert is_safe_readonly_command("sha256sum file.txt")
    assert is_safe_readonly_command("shasum -a 256 file.txt")


def test_safe_readonly_allows_dev_version_help_probes() -> None:
    assert is_safe_readonly_command("node --version")
    assert is_safe_readonly_command("node -v")
    assert is_safe_readonly_command("uv --version")
    assert is_safe_readonly_command("pytest --version")
    assert is_safe_readonly_command("mypy --help")
    assert is_safe_readonly_command("cargo --version")
    assert is_safe_readonly_command("go version")
    assert is_safe_readonly_command("java -version")
    assert is_safe_readonly_command("dotnet --info")
    assert is_safe_readonly_command("wget --version")


def test_safe_readonly_rejects_bare_version_help_targets() -> None:
    assert not is_safe_readonly_command("python3 version")
    assert not is_safe_readonly_command("python3 help")
    assert not is_safe_readonly_command("python version")
    assert not is_safe_readonly_command("node version")
    assert not is_safe_readonly_command("ruby version")
    assert not is_safe_readonly_command("pytest version")
    assert not is_safe_readonly_command("pytest -v")
    assert not is_safe_readonly_command("uv version")
    assert not is_safe_readonly_command("npm version")
    assert not is_safe_readonly_command("npx version")
    assert not is_safe_readonly_command("mypy help")


def test_safe_readonly_rejects_arbitrary_dev_execution() -> None:
    assert not is_safe_readonly_command("uv run pytest")
    assert not is_safe_readonly_command("node script.js")
    assert not is_safe_readonly_command("cargo test")
    assert not is_safe_readonly_command("go test ./...")
    assert not is_safe_readonly_command("ruby script.rb")


def test_safe_readonly_allows_ruff_check_only() -> None:
    assert is_safe_readonly_command("ruff check --no-cache --no-fix src tests")
    assert is_safe_readonly_command("ruff check --no-cache --diff src tests")
    assert is_safe_readonly_command("ruff format --check src tests")
    assert is_safe_readonly_command("ruff format --diff src tests")
    assert not is_safe_readonly_command("ruff check --no-cache src tests")
    assert not is_safe_readonly_command("ruff check --no-cache --config fix=true src")
    assert not is_safe_readonly_command("ruff check --no-cache --no-fix -o report.txt src")
    assert not is_safe_readonly_command("ruff check --no-cache --no-fix -no report.txt src")
    assert not is_safe_readonly_command("ruff check --no-cache --no-fix --output-file report.txt src")
    assert not is_safe_readonly_command("ruff check --no-cache --no-fix --output-file=report.txt src")
    assert not is_safe_readonly_command("ruff check src tests")
    assert not is_safe_readonly_command("ruff check --fix src")
    assert not is_safe_readonly_command("ruff check --fix-only src")
    assert not is_safe_readonly_command("ruff check --add-noqa src")
    assert not is_safe_readonly_command("ruff check --output-file report.txt src")
    assert not is_safe_readonly_command("ruff format src")


def test_safe_readonly_allows_pip_readonly_inspection() -> None:
    assert is_safe_readonly_command("pip list")
    assert is_safe_readonly_command("pip list --format=json")
    assert is_safe_readonly_command("pip show chrys")
    assert is_safe_readonly_command("pip freeze")
    assert not is_safe_readonly_command("pip list --outdated")
    assert not is_safe_readonly_command("pip list --out")
    assert not is_safe_readonly_command("pip list --upt")
    assert not is_safe_readonly_command("pip install chrys")


def test_safe_readonly_windows_readonly_commands() -> None:
    assert is_safe_readonly_command("findstr /s /n pattern *.py", shell_name="cmd")
    assert is_safe_readonly_command("fc a.txt b.txt", shell_name="cmd")
    assert is_safe_readonly_command("ver", shell_name="cmd")
    assert is_safe_readonly_command("Get-Module", shell_name="pwsh")
    assert is_safe_readonly_command("Get-PSDrive", shell_name="pwsh")
    assert is_safe_readonly_command("Compare-Object $a $b", shell_name="pwsh")


# ──────────────── Dangerous arguments detection ─────────────────────────


def test_safe_readonly_find_allowed() -> None:
    """Plain find with no destructive flags is safe."""
    assert is_safe_readonly_command("find . -name '*.py' -type f")


def test_safe_readonly_find_delete_blocked() -> None:
    assert not is_safe_readonly_command("find . -name '*.pyc' -delete")


def test_safe_readonly_find_exec_blocked() -> None:
    assert not is_safe_readonly_command("find . -name '*.pyc' -exec rm {} \\;")


def test_safe_readonly_find_exec_allows_readonly_commands() -> None:
    assert is_safe_readonly_command("find src/chrys -name '*.py' -exec wc -l {} + | tail -1")
    assert is_safe_readonly_command("find src/chrys -name '*.py' -exec cat {} + | wc -l")


def test_safe_readonly_find_execdir_blocked() -> None:
    assert not is_safe_readonly_command("find . -execdir mv {} {}.bak \\;")


def test_safe_readonly_find_ok_blocked() -> None:
    assert not is_safe_readonly_command("find . -name '*.tmp' -ok rm {} \\;")


def test_safe_readonly_dangerous_rm_as_arg() -> None:
    """rm appearing anywhere in tokens triggers approval."""
    assert not is_safe_readonly_command("echo rm")


def test_safe_readonly_dangerous_mv_as_arg() -> None:
    assert not is_safe_readonly_command("echo mv")


def test_safe_readonly_dangerous_chmod_as_arg() -> None:
    assert not is_safe_readonly_command("find . -name script.sh -exec chmod +x {} \\;")


def test_safe_readonly_dangerous_kill_as_arg() -> None:
    assert not is_safe_readonly_command("echo kill")


def test_safe_readonly_dangerous_tee_as_arg() -> None:
    assert not is_safe_readonly_command("echo hello | tee")


def test_safe_readonly_dangerous_powershell_remove_item() -> None:
    assert not is_safe_readonly_command("Get-ChildItem | Remove-Item")


def test_safe_readonly_dangerous_in_quotes_safe() -> None:
    """Dangerous tokens inside quotes are single tokens after shlex.split."""
    assert is_safe_readonly_command("echo 'find -delete is dangerous'")


def test_safe_readonly_rsync_delete_blocked() -> None:
    assert not is_safe_readonly_command("echo --delete")


def test_safe_readonly_rejects_redirect() -> None:
    assert not is_safe_readonly_command("ls > output.txt")


def test_safe_readonly_allows_output_to_devnull() -> None:
    assert is_safe_readonly_command("find . -name '*.py' 2>/dev/null")
    assert is_safe_readonly_command("ls > /dev/null")
    assert is_safe_readonly_command("ls > '/dev/null'")
    assert is_safe_readonly_command("printf hello >> /dev/null")


def test_safe_readonly_allows_fd_duplication() -> None:
    assert is_safe_readonly_command("ls >/dev/null 2>&1")
    assert is_safe_readonly_command("ls 2>&1 | head -20")
    assert is_safe_readonly_command("ls 2>&-")


def test_safe_readonly_rejects_non_standard_fd_duplication() -> None:
    assert not is_safe_readonly_command("ls 1>&3")
    assert not is_safe_readonly_command("cat <&3")


def test_safe_readonly_allows_input_from_devnull() -> None:
    assert is_safe_readonly_command("cat < /dev/null")


def test_safe_readonly_rejects_devnull_lookalike() -> None:
    assert not is_safe_readonly_command("ls > /dev/null2")


def test_safe_readonly_uses_shell_specific_escape_rules() -> None:
    assert is_safe_readonly_command("echo \\> output.txt")
    assert not is_safe_readonly_command("Get-ChildItem \\> output.txt", shell_name="pwsh")
    assert is_safe_readonly_command("Get-ChildItem `> output.txt", shell_name="pwsh")
    assert not is_safe_readonly_command("Get-ChildItem \\$(Remove-Item)", shell_name="pwsh")
    assert not is_safe_readonly_command("Get-ChildItem `$(Get-Location)", shell_name="pwsh")
    assert not is_safe_readonly_command("dir \\> output.txt", shell_name="cmd")
    assert is_safe_readonly_command("dir ^> output.txt", shell_name="cmd")


def test_safe_readonly_cmd_single_quotes_do_not_hide_redirect() -> None:
    assert not is_safe_readonly_command("dir '>' output.txt", shell_name="cmd")


def test_safe_readonly_rejects_redirect_to_normal_stderr_file() -> None:
    assert not is_safe_readonly_command("find . -name '*.py' 2> errors.log")


def test_safe_readonly_shell_specific_null_targets() -> None:
    assert not is_safe_readonly_command("ls > $null")
    assert is_safe_readonly_command("Get-ChildItem > $null", shell_name="pwsh")
    assert is_safe_readonly_command("Get-ChildItem 2> $null", shell_name="powershell")
    assert is_safe_readonly_command("Get-ChildItem *> $null", shell_name="pwsh")
    assert is_safe_readonly_command("Get-ChildItem 3> $null", shell_name="pwsh")
    assert not is_safe_readonly_command("Get-ChildItem > '$null'", shell_name="pwsh")
    assert not is_safe_readonly_command('Get-ChildItem > "$null"', shell_name="pwsh")
    assert is_safe_readonly_command("dir 2> NUL", shell_name="cmd")
    assert is_safe_readonly_command("dir >NUL 2>&1", shell_name="cmd")
    assert not is_safe_readonly_command("dir 2> NUL")


def test_safe_readonly_allows_line_count_examples() -> None:
    assert is_safe_readonly_command(
        "find src/chrys -name '*.py' | xargs wc -l | tail -1; "
        'echo "---"; '
        "find tests -name '*.py' | xargs wc -l | tail -1; "
        'echo "---"; '
        "find src/chrys -name '*.py' -o -name '*.pyi' | xargs cat | wc -l",
        shell_name="zsh",
    )
    assert is_safe_readonly_command(
        'echo "=== Python source files ===" && '
        "find src/chrys -name '*.py' -exec wc -l {} + | tail -1; "
        'echo "=== Test files ===" && '
        "find tests -name '*.py' -exec wc -l {} + | tail -1; "
        'echo "=== All text files (py, md, yaml, toml, cfg, json, sh, ps1) ===" && '
        "find src/chrys tests -type f \\( -name '*.py' -o -name '*.md' -o -name '*.yaml' "
        "-o -name '*.yml' -o -name '*.toml' -o -name '*.cfg' -o -name '*.json' "
        "-o -name '*.sh' -o -name '*.ps1' \\) -exec wc -l {} + | tail -1; "
        'echo "=== Project total (all files) ===" && '
        "find . -type f -not -path './.git/*' -not -path './.venv/*' -not -path './node_modules/*' "
        "-not -path './__pycache__/*' -not -path '*.egg-info/*' -not -path '*.pyc' "
        "| xargs wc -l 2>/dev/null | tail -1",
        shell_name="zsh",
    )


def test_safe_readonly_allows_file_type_summary_example() -> None:
    assert is_safe_readonly_command(
        'echo "=== 按文件类型 ===" && '
        'find . -type f \\( -name "*.py" -o -name "*.md" -o -name "*.yaml" -o -name "*.yml" '
        '-o -name "*.toml" -o -name "*.sh" -o -name "*.ps1" \\) '
        '-not -path "./.venv/*" -not -path "./.git/*" -not -path "./node_modules/*" '
        '-not -path "./dist/*" -not -path "./build/*" '
        "| sed 's/.*\\.//' | sort | uniq -c | sort -rn",
        shell_name="zsh",
    )


def test_safe_readonly_allows_simple_posix_for_line_count_examples() -> None:
    assert is_safe_readonly_command(
        "for d in src/chrys/*/; do "
        'printf "%-30s " "$d"; '
        "find \"$d\" -name '*.py' | xargs wc -l 2>/dev/null | tail -1 | awk '{print $1}'; "
        "done | sort -k2 -n -r",
        shell_name="zsh",
    )
    assert is_safe_readonly_command(
        "for d in tests/*/; do "
        'printf "%-30s " "$d"; '
        "find \"$d\" -name '*.py' | xargs wc -l 2>/dev/null | tail -1 | awk '{print $1}'; "
        "done | sort -k2 -n -r",
        shell_name="zsh",
    )


def test_safe_readonly_rejects_broad_or_unsafe_for_loops() -> None:
    assert not is_safe_readonly_command(
        'for d in tests/*/; do rm -rf "$d"; done',
        shell_name="zsh",
    )
    assert not is_safe_readonly_command(
        "for d in tests/*/; do printf '%s' \"$d\"; done",
        shell_name="pwsh",
    )
    assert not is_safe_readonly_command(
        'for d in tests/*/; do for f in "$d"/*; do wc -l "$f"; done; done',
        shell_name="zsh",
    )
    assert not is_safe_readonly_command(
        "for d in tests/*/; do find \"$d\" -name '*.py' | awk '{system(\"rm file\")}'; done",
        shell_name="zsh",
    )


def test_safe_readonly_rejects_command_substitution_line_count_example() -> None:
    assert not is_safe_readonly_command(
        "echo \"Python source files: $(find src/chrys -name '*.py' | wc -l)\" && "
        "echo \"Test files: $(find tests -name '*.py' | wc -l)\"",
        shell_name="zsh",
    )


def test_safe_readonly_rejects_append_redirect() -> None:
    assert not is_safe_readonly_command("echo foo >> log.txt")


def test_safe_readonly_rejects_input_redirect() -> None:
    assert not is_safe_readonly_command("sort < input.txt")


def test_safe_readonly_rejects_subshell() -> None:
    assert not is_safe_readonly_command("echo $(whoami)")


def test_safe_readonly_rejects_backtick() -> None:
    assert not is_safe_readonly_command("echo `date`")


def test_safe_readonly_rejects_mixed_safe_unsafe() -> None:
    """If ANY command in the pipeline is unsafe, reject the whole thing."""
    assert not is_safe_readonly_command("ls | python3 -c 'import os'")


def test_safe_readonly_allows_xargs_with_readonly_commands() -> None:
    assert is_safe_readonly_command("find src/chrys -name '*.py' | xargs wc -l | tail -1")
    assert is_safe_readonly_command("find src/chrys -name '*.py' | xargs cat | wc -l")


def test_safe_readonly_rejects_xargs_with_executing_commands() -> None:
    assert not is_safe_readonly_command("find . -name '*.py' | xargs sh -c 'rm file'")
    assert not is_safe_readonly_command("find . -name '*.py' | xargs python3 script.py")


def test_safe_readonly_rejects_file_commands() -> None:
    assert not is_safe_readonly_command("file README.md")
    assert not is_safe_readonly_command("file -C -m magic")
    assert not is_safe_readonly_command("file -bC -m magic")
    assert not is_safe_readonly_command("file --comp magic")
    assert not is_safe_readonly_command("file --compi magic")
    assert not is_safe_readonly_command("file --compile --magic-file magic")
    assert not is_safe_readonly_command("file -p README.md")
    assert not is_safe_readonly_command("file -bp README.md")
    assert not is_safe_readonly_command("find . -exec file {} +")
    assert not is_safe_readonly_command("find . -exec file -C -m magic {} +")
    assert not is_safe_readonly_command("find . -exec file -bC -m magic {} +")
    assert not is_safe_readonly_command("find . -exec file --compile --magic-file magic {} +")
    assert not is_safe_readonly_command("find . -exec file -p {} +")
    assert not is_safe_readonly_command("find . -name '*.py' | xargs file --mime-type")
    assert not is_safe_readonly_command("find . -name '*.py' | xargs file -C -m magic")
    assert not is_safe_readonly_command("find . -name '*.py' | xargs file -bC -m magic")
    assert not is_safe_readonly_command("find . -name '*.py' | xargs file --comp magic")
    assert not is_safe_readonly_command("find . -name '*.py' | xargs file --compi magic")
    assert not is_safe_readonly_command("find . -name '*.py' | xargs file --preserve-date")


def test_safe_readonly_rejects_backgrounded_unsafe_command() -> None:
    assert not is_safe_readonly_command('ls & python3 -c \'open("/tmp/chrys_probe", "w").write("x")\'')


def test_safe_readonly_rejects_empty() -> None:
    assert not is_safe_readonly_command("")


def test_safe_readonly_rejects_whitespace() -> None:
    assert not is_safe_readonly_command("   ")
