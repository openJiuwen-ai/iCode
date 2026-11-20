# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for approval safety classification helpers."""

from __future__ import annotations

import pytest

from chrys.service.approval.safety_classifier import (
    path_arg_may_access_sensitive_data,
    shell_arg_may_access_sensitive_data,
    shell_command_may_access_sensitive_data,
    target_may_access_sensitive_data,
)


def test_shell_sensitive_analysis_caps_pathological_unclosed_substitutions() -> None:
    """Large malformed substitution input is conservatively judged instead of rescanned quadratically."""
    command = "$(" * 5000
    assert shell_command_may_access_sensitive_data(command, shell_name="zsh") is True


@pytest.mark.parametrize(
    "path",
    [
        "~/.git-credentials",
        "~/Library/Keychains/login.keychain-db",
        "~/.kube/config",
        "~/.config/gh/hosts.yml",
        "~/.azure/accessTokens.json",
        "~/.azure/msal_token_cache.json",
        "~/.azure/msal_token_cache.bin",
    ],
)
def test_path_arg_flags_common_credential_stores(path: str) -> None:
    assert path_arg_may_access_sensitive_data({"path": path}) is True


@pytest.mark.parametrize(
    "raw_args",
    [
        {"arguments": ["~/.git-credentials"]},
        {"args": {"input": "~/.ssh/id_rsa"}},
    ],
)
def test_opaque_skill_script_arguments_are_not_reinterpreted_as_path_fields(raw_args: dict[str, object]) -> None:
    """Script tokens stay opaque; mandatory script approval is their security boundary."""
    assert path_arg_may_access_sensitive_data(raw_args) is False


@pytest.mark.parametrize(
    "path",
    [
        ".git/config",
        ".git/config.worktree",
        ".git/hooks/pre-commit",
        "/workspace/repo/.git/hooks/post-checkout",
    ],
)
def test_target_flags_git_execution_surfaces(path: str) -> None:
    assert target_may_access_sensitive_data(path) is True


@pytest.mark.parametrize("command", ["echo $PATH", "echo %PATH%", "echo hi"])
def test_shell_classification_does_not_escalate_non_sensitive_commands(command: str) -> None:
    assert shell_command_may_access_sensitive_data(command) is False


@pytest.mark.parametrize("path", [".env.example", ".env.sample", ".env.template"])
def test_env_template_exclusions_are_not_sensitive_but_env_is(path: str) -> None:
    assert target_may_access_sensitive_data(path) is False
    assert target_may_access_sensitive_data(".env") is True


@pytest.mark.parametrize(
    "command",
    [
        "cat .env*",
        "cat .env?",
        "cat *.env",
        "cat '*.env'",
        "cat ~/.ssh/id_rsa*",
        "grep secret .env*",
    ],
)
def test_shell_classification_flags_globbed_sensitive_targets(command: str) -> None:
    """Globs must not hide .env or private-key reads from the approval gate."""
    assert shell_command_may_access_sensitive_data(command) is True


@pytest.mark.parametrize(
    "command",
    [
        "cat /proc/self/environ",
        "cat /proc/thread-self/environ",
        "cat /proc/1234/environ",
    ],
)
def test_shell_classification_flags_proc_environ(command: str) -> None:
    assert shell_command_may_access_sensitive_data(command) is True


def test_shell_args_json_payload_classifies_and_malformed_json_fails_open() -> None:
    assert shell_arg_may_access_sensitive_data('{"command": "echo $API_KEY"}') is True
    assert shell_arg_may_access_sensitive_data('{"command":') is False
