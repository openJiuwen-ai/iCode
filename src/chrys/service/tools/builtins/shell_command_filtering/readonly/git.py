# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Git-specific read-only command validators."""

from __future__ import annotations

import re

_GIT_GLOBAL_FLAGS = frozenset(
    {
        "--no-pager",
        "--no-optional-locks",
        "--literal-pathspecs",
        "--glob-pathspecs",
        "--noglob-pathspecs",
        "--icase-pathspecs",
    }
)
_GIT_GLOBAL_OPTIONS_WITH_VALUE = frozenset({"-C", "--git-dir", "--work-tree", "--namespace"})
_GIT_GLOBAL_OPTION_PREFIXES = ("--git-dir=", "--work-tree=", "--namespace=")
_GIT_DIFF_HELPER_DISABLE_ARGS = frozenset({"--no-ext-diff", "--no-textconv"})
_GIT_PATCH_RENDERING_ARGS = frozenset(
    {
        "-p",
        "-u",
        "--patch",
        "--patch-with-raw",
        "--patch-with-stat",
        "--cc",
        "-c",
        "--combined",
    }
)
_GIT_PATCH_RENDERING_PREFIXES = ("--patch=", "-U", "--unified=")
_GIT_STATUS_FLAGS = frozenset(
    {
        "--short",
        "-s",
        "--branch",
        "-b",
        "--porcelain",
        "--porcelain=v1",
        "--porcelain=v2",
        "--untracked-files",
        "--untracked-files=all",
        "--untracked-files=normal",
        "--untracked-files=no",
        "--ignored",
        "--ignored=traditional",
        "--ignored=matching",
        "--ignored=no",
        "--no-renames",
        "--renames",
    }
)
_GIT_DIFF_FLAGS = (
    _GIT_DIFF_HELPER_DISABLE_ARGS
    | _GIT_PATCH_RENDERING_ARGS
    | frozenset(
        {
            "--stat",
            "--numstat",
            "--shortstat",
            "--name-only",
            "--name-status",
            "--cached",
            "--staged",
            "--exit-code",
            "--quiet",
        }
    )
)
_GIT_SHOW_FLAGS = _GIT_DIFF_FLAGS | frozenset({"--format=oneline"})
_GIT_LOG_FLAGS = (
    _GIT_DIFF_HELPER_DISABLE_ARGS
    | _GIT_PATCH_RENDERING_ARGS
    | frozenset(
        {
            "--oneline",
            "--graph",
            "--decorate",
            "--no-decorate",
            "--stat",
            "--numstat",
            "--shortstat",
            "--name-only",
            "--name-status",
        }
    )
)
_GIT_DESCRIBE_FLAGS = frozenset(
    {
        "--abbrev",
        "--always",
        "--all",
        "--contains",
        "--debug",
        "--exact-match",
        "--first-parent",
        "--long",
        "--tags",
    }
)
_GIT_DESCRIBE_OPTIONS_WITH_VALUE = frozenset({"--candidates", "--exclude", "--match"})
_GIT_DESCRIBE_OPTION_PREFIXES = ("--abbrev=", "--candidates=", "--exclude=", "--match=")
_GIT_LS_FILES_FLAGS = frozenset(
    {
        "--abbrev",
        "--cached",
        "-c",
        "--debug",
        "--deduplicate",
        "--directory",
        "--error-unmatch",
        "--exclude-standard",
        "--full-name",
        "--killed",
        "-k",
        "--no-empty-directory",
        "--recurse-submodules",
        "--stage",
        "-s",
        "--unmerged",
        "-u",
        "-z",
    }
)
_GIT_LS_FILES_OPTIONS_WITH_VALUE = frozenset({"--exclude", "--format", "--with-tree"})
_GIT_LS_FILES_OPTION_PREFIXES = ("--abbrev=", "--exclude=", "--format=", "--with-tree=")
_GIT_GREP_FLAGS = frozenset(
    {
        "-n",
        "--line-number",
        "-i",
        "--ignore-case",
        "-I",
        "-l",
        "--files-with-matches",
        "-L",
        "--files-without-match",
        "-c",
        "--count",
        "--cached",
        "--untracked",
    }
)
_GIT_GREP_SHORT_OPTION_CHARS = frozenset({"n", "i", "I", "l", "L", "c"})
_GIT_CAT_FILE_FLAGS = frozenset({"-e", "-p", "-s", "-t"})
_GIT_NO_EXEC_READONLY_SUBCOMMANDS = frozenset(
    {
        "check-attr",
        "check-ignore",
        "check-mailmap",
        "for-each-ref",
        "ls-tree",
        "merge-base",
        "name-rev",
        "rev-list",
        "rev-parse",
        "show-branch",
        "show-ref",
    }
)
_GIT_STASH_LIST_FLAGS = frozenset({"--stat"})
_GIT_STASH_SHOW_FLAGS = _GIT_DIFF_FLAGS | frozenset({"--stat"})
_GIT_BRANCH_LIST_FLAGS = frozenset(
    {
        "-a",
        "--all",
        "-r",
        "--remotes",
        "-v",
        "-vv",
        "--verbose",
        "--list",
        "--show-current",
        "--no-color",
        "--color",
    }
)
_GIT_BRANCH_OPTIONS_WITH_VALUE = frozenset(
    {"--format", "--sort", "--points-at", "--contains", "--no-contains", "--merged", "--no-merged"}
)
_GIT_BRANCH_OPTION_PREFIXES = (*(f"{opt}=" for opt in _GIT_BRANCH_OPTIONS_WITH_VALUE), "--color=")
_GIT_TAG_LIST_FLAGS = frozenset({"--list", "-l", "-n", "--no-color", "--color", "--sort", "--format"})
_GIT_TAG_OPTIONS_WITH_VALUE = frozenset({"--format", "--sort", "--points-at", "--contains", "--no-contains"})
_GIT_TAG_OPTION_PREFIXES = (*(f"{opt}=" for opt in _GIT_TAG_OPTIONS_WITH_VALUE), "--color=", "-n")
_GIT_CONFIG_SAFE_ACTIONS = frozenset(
    {"--get", "--get-all", "--get-regexp", "--list", "-l", "--name-only", "--get-color", "--get-colorbool"}
)
_GIT_CONFIG_SCOPE_FLAGS = frozenset(
    {
        "--global",
        "--system",
        "--local",
        "--worktree",
        "--show-origin",
        "--show-scope",
        "-z",
        "--null",
    }
)
_GIT_LOG_SHORT_COUNT_RE = re.compile(r"-\d+$")


def _git_diff_helpers_disabled(args: list[str]) -> bool:
    return set(_git_option_tokens(args)) >= _GIT_DIFF_HELPER_DISABLE_ARGS


def _git_args_request_patch_rendering(args: list[str]) -> bool:
    for arg in _git_option_tokens(args):
        if arg in _GIT_PATCH_RENDERING_ARGS or arg.startswith(_GIT_PATCH_RENDERING_PREFIXES):
            return True
    return False


def _git_option_tokens(args: list[str]) -> list[str]:
    tokens: list[str] = []
    for arg in args:
        if arg == "--":
            break
        if arg.startswith("-") and arg != "-":
            tokens.append(arg)
    return tokens


def _git_args_use_only_allowed_options(
    args: list[str],
    *,
    flags: frozenset[str],
    options_with_value: frozenset[str] = frozenset(),
    option_prefixes: tuple[str, ...] = (),
    short_option_chars: frozenset[str] = frozenset(),
    short_option_patterns: tuple[re.Pattern[str], ...] = (),
) -> bool:
    """Accept only exact known-safe git options before ``--``.

    Git accepts abbreviated long options, so all long options here are matched
    as exact strings or exact ``--name=`` prefixes for known value-taking
    options. Unknown options fall back to normal approval.
    """
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--":
            return True
        if arg.startswith("--"):
            if arg in flags:
                i += 1
                continue
            if arg in options_with_value:
                if i + 1 >= len(args):
                    return False
                i += 2
                continue
            if any(arg.startswith(prefix) for prefix in option_prefixes):
                i += 1
                continue
            return False
        if arg.startswith("-") and arg != "-":
            if arg in flags or any(pattern.fullmatch(arg) for pattern in short_option_patterns):
                i += 1
                continue
            if len(arg) > 2 and short_option_chars and all(ch in short_option_chars for ch in arg[1:]):
                i += 1
                continue
            if arg in options_with_value:
                if i + 1 >= len(args):
                    return False
                i += 2
                continue
            return False
        i += 1
    return True


def _strip_git_global_options(args: list[str]) -> list[str] | None:
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--":
            return args[i + 1 :]
        if arg in _GIT_GLOBAL_FLAGS:
            i += 1
            continue
        if arg in _GIT_GLOBAL_OPTIONS_WITH_VALUE:
            if i + 1 >= len(args):
                return None
            i += 2
            continue
        if any(arg.startswith(prefix) for prefix in _GIT_GLOBAL_OPTION_PREFIXES):
            i += 1
            continue
        if arg.startswith("-"):
            return None
        return args[i:]
    return []


def _is_safe_git_branch(args: list[str]) -> bool:
    if not args:
        return True
    saw_list_mode = False
    i = 0
    while i < len(args):
        arg = args[i]
        if arg in {"--list", "--show-current", "-a", "--all", "-r", "--remotes"}:
            saw_list_mode = True
        if arg in _GIT_BRANCH_LIST_FLAGS:
            i += 1
            continue
        if arg in _GIT_BRANCH_OPTIONS_WITH_VALUE:
            if i + 1 >= len(args):
                return False
            i += 2
            continue
        if any(arg.startswith(prefix) for prefix in _GIT_BRANCH_OPTION_PREFIXES):
            i += 1
            continue
        if saw_list_mode and not arg.startswith("-"):
            i += 1
            continue
        return False
    return True


def _is_safe_git_tag(args: list[str]) -> bool:
    if not args:
        return True
    saw_list_mode = False
    i = 0
    while i < len(args):
        arg = args[i]
        if arg in {"--list", "-l"}:
            saw_list_mode = True
        if arg in _GIT_TAG_LIST_FLAGS:
            i += 1
            continue
        if arg in _GIT_TAG_OPTIONS_WITH_VALUE:
            if i + 1 >= len(args):
                return False
            i += 2
            continue
        if any(arg.startswith(prefix) for prefix in _GIT_TAG_OPTION_PREFIXES):
            i += 1
            continue
        if saw_list_mode and not arg.startswith("-"):
            i += 1
            continue
        return False
    return True


def _is_safe_git_config(args: list[str]) -> bool:
    if not args:
        return False
    saw_action = False
    i = 0
    while i < len(args):
        arg = args[i]
        if arg in _GIT_CONFIG_SCOPE_FLAGS:
            i += 1
            continue
        if arg in _GIT_CONFIG_SAFE_ACTIONS:
            saw_action = True
            i += 1
            continue
        if arg.startswith("-"):
            return False
        i += 1
    return saw_action


def _is_safe_git_stash(args: list[str]) -> bool:
    if not args:
        return False
    action = args[0]
    action_args = args[1:]
    if action == "list":
        return _git_args_use_only_allowed_options(action_args, flags=_GIT_STASH_LIST_FLAGS)
    if action == "show":
        if not _git_args_use_only_allowed_options(
            action_args,
            flags=_GIT_STASH_SHOW_FLAGS,
            option_prefixes=_GIT_PATCH_RENDERING_PREFIXES,
        ):
            return False
        return not _git_args_request_patch_rendering(action_args) or _git_diff_helpers_disabled(action_args)
    return False


def _is_safe_git_ls_files(args: list[str]) -> bool:
    """Accept only index/tree listing; worktree comparisons can run filters."""
    return _git_args_use_only_allowed_options(
        args,
        flags=_GIT_LS_FILES_FLAGS,
        options_with_value=_GIT_LS_FILES_OPTIONS_WITH_VALUE,
        option_prefixes=_GIT_LS_FILES_OPTION_PREFIXES,
    )


def _is_safe_git_describe(args: list[str]) -> bool:
    """Accept only object queries; ``--dirty``/``--broken`` refresh the worktree."""
    return _git_args_use_only_allowed_options(
        args,
        flags=_GIT_DESCRIBE_FLAGS,
        options_with_value=_GIT_DESCRIBE_OPTIONS_WITH_VALUE,
        option_prefixes=_GIT_DESCRIBE_OPTION_PREFIXES,
    )


def _is_safe_git(args: list[str]) -> bool:
    no_optional_locks = "--no-optional-locks" in args
    args = _strip_git_global_options(args) or []
    if not args:
        return False

    subcommand = args[0]
    sub_args = args[1:]
    if subcommand == "status":
        return no_optional_locks and _git_args_use_only_allowed_options(sub_args, flags=_GIT_STATUS_FLAGS)
    if subcommand == "diff":
        return _git_args_use_only_allowed_options(
            sub_args,
            flags=_GIT_DIFF_FLAGS,
            option_prefixes=_GIT_PATCH_RENDERING_PREFIXES,
        ) and _git_diff_helpers_disabled(sub_args)
    if subcommand == "show":
        return _git_args_use_only_allowed_options(
            sub_args,
            flags=_GIT_SHOW_FLAGS,
            option_prefixes=_GIT_PATCH_RENDERING_PREFIXES,
        ) and _git_diff_helpers_disabled(sub_args)
    if subcommand == "log":
        if not _git_args_use_only_allowed_options(
            sub_args,
            flags=_GIT_LOG_FLAGS,
            option_prefixes=_GIT_PATCH_RENDERING_PREFIXES,
            short_option_patterns=(_GIT_LOG_SHORT_COUNT_RE,),
        ):
            return False
        return not _git_args_request_patch_rendering(sub_args) or _git_diff_helpers_disabled(sub_args)
    if subcommand == "grep":
        return _git_args_use_only_allowed_options(
            sub_args,
            flags=_GIT_GREP_FLAGS,
            short_option_chars=_GIT_GREP_SHORT_OPTION_CHARS,
        )
    if subcommand == "cat-file":
        return _git_args_use_only_allowed_options(sub_args, flags=_GIT_CAT_FILE_FLAGS)
    if subcommand == "describe":
        return _is_safe_git_describe(sub_args)
    if subcommand == "ls-files":
        return _is_safe_git_ls_files(sub_args)
    if subcommand in _GIT_NO_EXEC_READONLY_SUBCOMMANDS:
        return True
    if subcommand == "remote":
        return not sub_args or sub_args in (["-v"], ["--verbose"])
    if subcommand == "stash":
        return _is_safe_git_stash(sub_args)
    if subcommand == "worktree":
        return sub_args == ["list"]
    if subcommand == "branch":
        return _is_safe_git_branch(sub_args)
    if subcommand == "tag":
        return _is_safe_git_tag(sub_args)
    if subcommand == "config":
        return _is_safe_git_config(sub_args)
    return False
