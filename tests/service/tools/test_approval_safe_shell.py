# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for the approval-safe shell boundary."""

from __future__ import annotations

import pytest

from chrys.service.approval.safety_classifier import shell_command_may_access_sensitive_data
from chrys.service.tools.builtins.shell_filter import classify_shell_for_approval


@pytest.mark.parametrize(
    ("command", "argv", "execution"),
    [
        ("ls -la src", ("ls", "-la", "src"), "exec"),
        ("cat README.md", ("cat", "README.md"), "exec"),
        ("cat 'file with space'", ("cat", "file with space"), "exec"),
        ('cat "file with space"', ("cat", "file with space"), "exec"),
        ("grep TODO src/chrys/service", ("grep", "TODO", "src/chrys/service"), "exec"),
        ("pwd", ("pwd",), "shell"),
        ("ps", ("ps",), "exec"),
        ("ps aux", ("ps", "aux"), "exec"),
        ("ps -f", ("ps", "-f"), "exec"),
        ("ps -l", ("ps", "-l"), "exec"),
        ("git log --oneline -5", ("git", "log", "--oneline", "-5"), "exec"),
        ("git ls-files --cached", ("git", "ls-files", "--cached"), "exec"),
        ("git ls-files --stage -z", ("git", "ls-files", "--stage", "-z"), "exec"),
        ("git describe --tags --always", ("git", "describe", "--tags", "--always"), "exec"),
        ("git describe --abbrev", ("git", "describe", "--abbrev"), "exec"),
        ("git describe --abbrev=12 --tags", ("git", "describe", "--abbrev=12", "--tags"), "exec"),
        (
            "git diff --cached --no-ext-diff --no-textconv",
            ("git", "diff", "--cached", "--no-ext-diff", "--no-textconv"),
            "exec",
        ),
        (
            "git for-each-ref --format='%(refname)' refs/heads",
            ("git", "for-each-ref", "--format=%(refname)", "refs/heads"),
            "exec",
        ),
        ("git branch --list 'feature/*'", ("git", "branch", "--list", "feature/*"), "exec"),
        ("git tag --list 'v*'", ("git", "tag", "--list", "v*"), "exec"),
        (
            "git check-mailmap '<A U Thor <author@example.com>>'",
            ("git", "check-mailmap", "<A U Thor <author@example.com>>"),
            "exec",
        ),
        ("sed -n 1p README.md", ("sed", "-n", "1p", "README.md"), "exec"),
        ("python --version", ("python", "--version"), "exec"),
        ("uniq -- input.txt", ("uniq", "--", "input.txt"), "exec"),
        ("ruff --version", ("ruff", "--version"), "exec"),
        ("ruff format --check --no-cache", ("ruff", "format", "--check", "--no-cache"), "exec"),
        ("ruff format --diff --no-cache", ("ruff", "format", "--diff", "--no-cache"), "exec"),
        (
            "ruff format --check --no-cache -- --check",
            ("ruff", "format", "--check", "--no-cache", "--", "--check"),
            "exec",
        ),
        ("echo hello", ("echo", "hello"), "shell"),
        ('echo "a b"', ("echo", "a b"), "shell"),
        ("echo '(not execution)'", ("echo", "(not execution)"), "shell"),
        ("printf %s", ("printf", "%s"), "shell"),
    ],
)
def test_classifier_returns_validated_argv(command: str, argv: tuple[str, ...], execution: str) -> None:
    decision = classify_shell_for_approval(command, shell_name="bash")

    assert decision.safe is True
    assert decision.command is not None
    assert decision.command.argv == argv
    assert decision.command.execution == execution
    assert decision.reason == ""


@pytest.mark.parametrize(
    ("command", "reason"),
    [
        ("rg --pre ./x.sh -e y .", "not_approval_safe:rg"),
        ("rg --pre=./x.sh -e y .", "not_approval_safe:rg"),
        ("rg -n foo .", "not_approval_safe:rg"),
        ("rg --pre$IFS./x.sh -e y .", "shell_syntax"),
        ("rg --p''re ./x.sh -e y .", "not_approval_safe:rg"),
        ("r''g --pre ./x.sh -e y .", "not_approval_safe:rg"),
        ("\\rg --pre ./x.sh -e y .", "shell_syntax"),
        ("rg --pre-glob README.md --pre ./x.sh -e y .", "not_approval_safe:rg"),
        ("cat .env*", "shell_syntax"),
        ('cat "$HOME/x"', "shell_syntax"),
        ("cat {a,b}", "shell_syntax"),
        ("ls | cat", "shell_syntax"),
        ("cat > out", "shell_syntax"),
        ("date -s 2020-01-01", "not_approval_safe:date"),
        ("hostname evil", "not_approval_safe:hostname"),
        ("tree -o out .", "not_approval_safe:tree"),
        ("less README.md", "not_approval_safe:less"),
        ("more README.md", "not_approval_safe:more"),
        ("bash -c id", "not_approval_safe:bash"),
        ("git status --short", "not_approval_safe:git-worktree"),
        ("git --no-optional-locks status --short", "not_approval_safe:git-worktree"),
        ("git describe --dirty", "not_approval_safe:git"),
        ("git describe --broken", "not_approval_safe:git"),
        ("git describe --abbrev --dirty", "not_approval_safe:git"),
        ("git describe --abbrev=12 --dirty", "not_approval_safe:git"),
        ("git ls-files --modified", "not_approval_safe:git"),
        ("git ls-files -m", "not_approval_safe:git"),
        ("git ls-files --deleted", "not_approval_safe:git"),
        ("git ls-files --others", "not_approval_safe:git"),
        ("git ls-files --eol", "not_approval_safe:git"),
        ("git ls-files --format=path", "not_approval_safe:git"),
        ("find . -name README.md", "not_approval_safe:find"),
        ("find -f . -name x", "not_approval_safe:find"),
        ("find -fpath . -name x", "not_approval_safe:find"),
        ("find --files0-from=paths -name x", "not_approval_safe:find"),
        ("xargs echo", "not_approval_safe:xargs"),
        ("xargs --eof=EOF echo", "not_approval_safe:xargs"),
        ("git config --global --list", "not_approval_safe:git-config"),
        ("git --no-pager config --list", "not_approval_safe:git-config"),
        ("git --no-pager --literal-pathspecs --glob-pathspecs config --list", "not_approval_safe:git-config"),
        ("git -C . config --list", "not_approval_safe:git-config"),
        ("git -c core.pager=evil log", "not_approval_safe:git"),
        ("git diff --no-ext-diff --no-textconv", "not_approval_safe:git-worktree"),
        ("ps eww -p 1", "not_approval_safe:ps"),
        ("ps -E", "not_approval_safe:ps"),
        ("ps -e", "not_approval_safe:ps"),
        ("ps -ef", "not_approval_safe:ps"),
        ("ps auxe", "not_approval_safe:ps"),
        ("ps -o command", "not_approval_safe:ps"),
        ("go version", "not_approval_safe:go"),
        ("go help", "not_approval_safe:go"),
        ("ruff format --check", "not_approval_safe:ruff"),
        ("ruff format --diff", "not_approval_safe:ruff"),
        ("find . -exec sh -c id +", "not_approval_safe:find"),
        ("find . -delete", "not_approval_safe:find"),
        ("sed -i s/a/b/ file", "not_approval_safe:sed"),
        ("sed -e s/a/b/e file", "not_approval_safe:sed"),
        ("python -c id", "not_approval_safe:python"),
        ("pytest --version", "not_approval_safe:pytest"),
        ("pytest --help", "not_approval_safe:pytest"),
        ("pytest -h", "not_approval_safe:pytest"),
        ("pip list", "not_approval_safe:pip"),
        ("pip list --format=json", "not_approval_safe:pip"),
        ("pip list --log out.log", "not_approval_safe:pip"),
        ("pip list -ou", "not_approval_safe:pip"),
        ("pip show --log out.log", "not_approval_safe:pip"),
        ("pip freeze --log out.log", "not_approval_safe:pip"),
        ("cargo --version", "not_approval_safe:cargo"),
        ("cargo --help", "not_approval_safe:cargo"),
        ("ruff format --no-cache -- --check", "not_approval_safe:ruff"),
        ("uniq -- input.txt output.txt", "not_approval_safe:uniq"),
        ("jq -n env", "not_approval_safe:jq"),
        ("env", "not_approval_safe:env"),
        ("env A=", "not_approval_safe:env"),
        ("en''v", "not_approval_safe:env"),
        ("'env'", "not_approval_safe:env"),
    ],
)
def test_classifier_requires_approval(command: str, reason: str) -> None:
    decision = classify_shell_for_approval(command, shell_name="bash")

    assert decision.safe is False
    assert decision.command is None
    assert decision.reason == reason


def test_git_bash_keeps_safe_commands_on_its_shell() -> None:
    """Git Bash's core tools live in its own usr/bin; keep shell lookup semantics."""
    for command in ("ls -la", "cat README.md", "git log --oneline"):
        decision = classify_shell_for_approval(command, shell_name="git_bash")

        assert decision.safe is True
        assert decision.command is not None
        assert decision.command.execution == "shell"


@pytest.mark.parametrize("shell_name", ["pwsh", "powershell", "cmd"])
def test_windows_shells_are_not_auto_approved(shell_name: str) -> None:
    """Their argument grammar needs a separate parser; fail closed until then."""
    decision = classify_shell_for_approval("Get-ChildItem", shell_name=shell_name)

    assert decision.safe is False
    assert decision.reason == f"shell_not_supported:{shell_name}"


def test_windows_shells_do_not_auto_approve_even_simple_external_commands() -> None:
    assert classify_shell_for_approval("git log --oneline", shell_name="pwsh").safe is False
    assert classify_shell_for_approval("ls", shell_name="cmd").safe is False


def test_classifier_is_pure_and_has_no_sensitive_target_gate() -> None:
    """Sensitivity is a second gate: the classifier only proves the command shape.

    ``cat .env`` and ``/proc/self/environ`` are safe shapes but must never reach
    auto-approval; the approval middleware checks the sensitive-target predicate
    separately. ``env`` is not an auto-approved shape at all: it dumps the
    environment with or without an empty assignment.
    """
    for command in ("cat .env", "cat /proc/self/environ"):
        decision = classify_shell_for_approval(command, shell_name="bash")
        assert decision.safe is True
        assert shell_command_may_access_sensitive_data(command) is True
    for command in ("env", "env A=", "en''v"):
        assert classify_shell_for_approval(command, shell_name="bash").safe is False


@pytest.mark.parametrize("shell_name", ["", "ash", "fish", "csh", "tcsh"])
def test_unmodeled_shells_are_not_auto_approved(shell_name: str) -> None:
    """An unknown or non-POSIX shell grammar fails closed instead of assuming bash."""
    decision = classify_shell_for_approval("ls", shell_name=shell_name)

    assert decision.safe is False
    assert decision.command is None
    assert decision.reason.startswith("shell_not_supported:")


def test_zsh_equals_expansion_requires_approval() -> None:
    """zsh expands ``=cmd`` even after quotes; the parsed argv is not what runs."""
    decision = classify_shell_for_approval("echo =foo", shell_name="zsh")

    assert decision.safe is False
    assert decision.reason == "zsh_equals_expansion"


@pytest.mark.parametrize("command", ["", "   ", "\n", "ls\ncat"])
def test_empty_or_multiline_commands_are_not_safe(command: str) -> None:
    assert classify_shell_for_approval(command, shell_name="bash").safe is False
