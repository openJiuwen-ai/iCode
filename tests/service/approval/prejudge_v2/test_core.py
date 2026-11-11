# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Structured YAML matcher tests, independent of optional Tree-sitter."""

from __future__ import annotations

import pytest

from chrys.service.approval import prejudge_v2 as pj


def _policy(*rules: pj.Rule) -> pj.Policy:
    return pj.Policy("enforce", "pilot", tuple(rules))


def _rule(rid: str, effect: pj.Route, field: pj.MatcherField, *tokens: str) -> pj.Rule:
    return pj.Rule(rid, effect, pj.Matcher(field, tuple(tokens)))


def _command(argv: list[str] | None, prefix: list[str] | None = None) -> pj.CommandNode:
    return pj.CommandNode(
        (0, 12),
        tuple(argv) if argv is not None else None,
        tuple(prefix if prefix is not None else (argv or ())),
        "",
    )


def test_block_precedes_allow_and_targets_actual_executable(monkeypatch):
    monkeypatch.setattr(
        pj,
        "analyze",
        lambda *a, **kw: pj.Analysis(
            True,
            True,
            (_command(["echo", "rm"]), _command(["rm", "file"])),
            (),
        ),
    )
    rules = _policy(
        _rule("A-echo", pj.Route.ALLOW, "argv_first", "echo"),
        _rule("B-rm", pj.Route.BLOCK, "prefix_first", "rm"),
    )
    result = pj.route("echo rm; rm file", "bash", rules)
    assert result.route is pj.Route.BLOCK
    assert result.matched_rule_ids[-1] == "B-rm"


def test_unknown_command_must_defer(monkeypatch):
    monkeypatch.setattr(
        pj,
        "analyze",
        lambda *a, **kw: pj.Analysis(
            True,
            True,
            (_command(["git", "status"]), _command(["unknown"])),
            (),
        ),
    )
    rules = _policy(_rule("status", pj.Route.ALLOW, "argv_exact", "git", "status"))
    assert pj.route("git status && unknown", "bash", rules).route is pj.Route.DEFER


def test_incomplete_argv_cannot_allow_but_static_head_can_block(monkeypatch):
    monkeypatch.setattr(
        pj,
        "analyze",
        lambda *a, **kw: pj.Analysis(True, False, (_command(None, ["rm"]),), ("dynamic_word",)),
    )
    allow = _rule("A", pj.Route.ALLOW, "argv_first", "rm")
    block = _rule("B", pj.Route.BLOCK, "prefix_first", "rm")
    assert pj.route('rm "$X"', "bash", _policy(allow)).route is pj.Route.DEFER
    assert pj.route('rm "$X"', "bash", _policy(block)).route is pj.Route.BLOCK


def test_quoted_dangerous_word_does_not_block(monkeypatch):
    monkeypatch.setattr(
        pj,
        "analyze",
        lambda *a, **kw: pj.Analysis(True, True, (_command(["echo", "rm"]),), ()),
    )
    policy = _policy(_rule("B", pj.Route.BLOCK, "prefix_first", "rm"))
    assert pj.route('echo "rm"', "bash", policy).route is pj.Route.DEFER


def test_unsupported_syntax_never_auto_allows(monkeypatch):
    monkeypatch.setattr(
        pj,
        "analyze",
        lambda *a, **kw: pj.Analysis(
            True,
            False,
            (_command(["echo", "ok"]),),
            ("command_substitution",),
        ),
    )
    rules = _policy(_rule("echo", pj.Route.ALLOW, "argv_first", "echo"))
    assert pj.route('echo "$(rm f)"', "bash", rules).route is pj.Route.DEFER


def test_off_and_missing_config(tmp_path):
    assert pj.route("ignored", "bash", pj.Policy("off", "", ())).route is pj.Route.DEFER
    assert pj.load_policy(tmp_path / "missing.yaml").mode == "off"


@pytest.mark.parametrize(
    "match",
    [
        {},
        {"argv_exact": []},
        {"argv_exact": ["git", 3]},
        {"argv_exact": "git status"},
        {"argv_first": ["git", "status"]},
        {"argv_first": []},
        {"prefix_first": [""]},
        {"argv_exact": ["git"], "argv_first": ["git"]},
        {"unknown": ["git"]},
        {"argv_exact": [True]},
        {"argv_exact": [None]},
    ],
)
def test_invalid_match_schema_fails_closed(match, tmp_path, monkeypatch):
    monkeypatch.setattr(pj, "_ensure_bash_parser", lambda: None)
    path = tmp_path / "prejudge.yaml"
    import yaml

    path.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "mode": "enforce",
                "policy_id": "pilot",
                "rules": [{"id": "A", "effect": "allow", "target": "command", "match": match}],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        pj.load_policy(path)


def test_prefix_match_cannot_allow(tmp_path, monkeypatch):
    monkeypatch.setattr(pj, "_ensure_bash_parser", lambda: None)
    path = tmp_path / "prejudge.yaml"
    path.write_text(
        'schema_version: 1\nmode: "enforce"\npolicy_id: pilot\n'
        "rules:\n  - id: A\n    effect: allow\n    target: command\n"
        "    match:\n      prefix_first: [git]\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="cannot be used to ALLOW"):
        pj.load_policy(path)


def test_argv_first_block_is_rejected_at_policy_load(tmp_path, monkeypatch):
    monkeypatch.setattr(pj, "_ensure_bash_parser", lambda: None)
    path = tmp_path / "prejudge.yaml"
    path.write_text(
        'schema_version: 1\nmode: "enforce"\npolicy_id: pilot\n'
        "rules:\n  - id: B\n    effect: block\n    target: command\n"
        "    match:\n      argv_first: [rm]\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="use prefix_first"):
        pj.load_policy(path)


def test_actual_yaml_rule_load_and_route(tmp_path, monkeypatch):
    monkeypatch.setattr(pj, "_ensure_bash_parser", lambda: None)
    monkeypatch.setattr(
        pj,
        "analyze",
        lambda *a, **kw: pj.Analysis(
            True,
            True,
            (_command(["git", "status"]),),
            (),
        ),
    )
    path = tmp_path / "prejudge.yaml"
    path.write_text(
        'schema_version: 1\nmode: "enforce"\npolicy_id: pilot\n'
        "rules:\n  - id: allow-status\n    effect: allow\n"
        "    target: command\n    match:\n      argv_exact: [git, status]\n",
        encoding="utf-8",
    )
    policy = pj.load_policy(path)
    assert policy.rules[0].match.field == "argv_exact"
    assert pj.route("git status", "bash", policy).route is pj.Route.ALLOW


@pytest.mark.parametrize(
    "config",
    [
        "schema_version: 1\nmode: off\npolicy_id: pilot\nrules: []",
        'schema_version: 1\nmode: "enfroce"\npolicy_id: pilot\nrules: []',
        'schema_version: 1\nmode: "off"\nunknown: true',
        (
            'schema_version: 1\nmode: "enforce"\npolicy_id: pilot\n'
            "rules:\n  - id: A\n    effect: allow\n    target: command\n"
            "    when: 'argv == [\"git\"]'"
        ),
    ],
)
def test_invalid_config(tmp_path, config, monkeypatch):
    # Exercise YAML schema validation without the optional Tree-sitter backend.
    monkeypatch.setattr(pj, "_ensure_bash_parser", lambda: None)
    file = tmp_path / "prejudge.yaml"
    file.write_text(config, encoding="utf-8")
    with pytest.raises(ValueError):
        pj.load_policy(file)


def test_duplicate_yaml_match_key_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(pj, "_ensure_bash_parser", lambda: None)
    file = tmp_path / "prejudge.yaml"
    file.write_text(
        'schema_version: 1\nmode: "enforce"\npolicy_id: pilot\n'
        "rules:\n  - id: A\n    effect: allow\n    target: command\n"
        "    match:\n      argv_exact: [git, status]\n"
        "      argv_exact: [rm]\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="Duplicate YAML policy key"):
        pj.load_policy(file)


def test_shadow_invalid_policy_disables_observation(tmp_path, monkeypatch):
    monkeypatch.setattr(pj, "_ensure_bash_parser", lambda: None)
    file = tmp_path / "prejudge.yaml"
    file.write_text(
        'schema_version: 1\nmode: "shadow"\npolicy_id: pilot\n'
        "rules:\n  - id: B\n    effect: block\n    target: command\n"
        "    match:\n      unsupported: [rm]\n",
        encoding="utf-8",
    )
    assert pj.load_policy(file).mode == "off"


def test_enforce_requires_parser_at_load(tmp_path, monkeypatch):
    file = tmp_path / "prejudge.yaml"
    file.write_text(
        'schema_version: 1\nmode: "enforce"\npolicy_id: pilot\nrules: []',
        encoding="utf-8",
    )

    def bad_parser():
        raise RuntimeError("tree-sitter not installed")

    monkeypatch.setattr(pj, "_ensure_bash_parser", bad_parser)
    with pytest.raises(RuntimeError, match="tree-sitter"):
        pj.load_policy(file)


@pytest.mark.parametrize(
    ("dialect", "command"),
    [
        ("bash", "ls *.py"),
        ("bash", "cat pyproject.toml"),
        ("bash", "git status --short"),
        ("bash", "git push"),
        ("bash", "find . -delete"),
        ("bash", "ls > output.txt"),
        ("bash", "echo rm"),
        ("bash", "ruff check --fix"),
        ("zsh", "ls *.py"),
        ("pwsh", "Get-Content foo"),
    ],
)
def test_builtin_policy_exactly_preserves_legacy_readonly(monkeypatch, dialect, command):
    from chrys.service.tools.builtins.shell_filter import is_safe_readonly_command

    monkeypatch.setattr(
        pj,
        "analyze",
        lambda *args, **kwargs: pj.Analysis(False, False, (), ("unsupported_construct",)),
    )
    policy = _policy(pj._BUILTIN_READONLY_RULE)
    expected = is_safe_readonly_command(command, shell_name=dialect)
    actual = pj.route(command, dialect, policy)
    assert (actual.route is pj.Route.ALLOW) == expected
    if expected:
        assert actual.reason_code == "builtin_readonly"
        assert actual.matched_rule_ids == ("__builtin_readonly__",)


def test_explicit_block_precedes_builtin_readonly(monkeypatch):
    monkeypatch.setattr(
        pj,
        "analyze",
        lambda *a, **kw: pj.Analysis(True, True, (_command(["ls", "x"]),), ()),
    )
    policy = _policy(_rule("no-ls", pj.Route.BLOCK, "prefix_first", "ls"), pj._BUILTIN_READONLY_RULE)
    assert pj.route("ls x", "bash", policy).route is pj.Route.BLOCK


def test_policy_includes_readonly_without_user_configuration(tmp_path, monkeypatch):
    monkeypatch.setattr(pj, "_ensure_bash_parser", lambda: None)
    path = tmp_path / "prejudge.yaml"
    path.write_text('schema_version: 1\nmode: "enforce"\npolicy_id: test\nrules: []\n', encoding="utf-8")
    policy = pj.load_policy(path)
    assert policy.rules == (pj._BUILTIN_READONLY_RULE,)
