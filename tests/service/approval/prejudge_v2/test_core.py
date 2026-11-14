# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Structured YAML matcher tests, independent of optional Tree-sitter."""

from __future__ import annotations

import pytest

from chrys.service.approval import prejudge_v2 as pj


def _policy(*rules: pj.Rule) -> pj.Policy:
    return pj.Policy("enforce", "pilot", tuple(rules))


def _rule(rid: str, field: pj.MatcherField, *tokens: str) -> pj.Rule:
    return pj.Rule(rid, pj.Route.ALLOW, pj.Matcher(field, tuple(tokens)))


def _command(argv: list[str] | None, prefix: list[str] | None = None) -> pj.CommandNode:
    return pj.CommandNode(
        (0, 12),
        tuple(argv) if argv is not None else None,
        tuple(prefix if prefix is not None else (argv or ())),
        "",
    )


def test_prejudge_exposes_only_allow_and_defer() -> None:
    assert {route.value for route in pj.Route} == {"allow", "defer"}


@pytest.mark.parametrize("mode", ["enforce", "shadow"])
def test_block_rules_are_rejected_instead_of_silently_ignored(tmp_path, monkeypatch, caplog, mode):
    monkeypatch.setattr(pj, "_ensure_bash_parser", lambda: None)
    path = tmp_path / "prejudge.yaml"
    path.write_text(
        f'schema_version: 1\nmode: "{mode}"\npolicy_id: old-config\n'
        "rules:\n  - id: old-block\n    effect: block\n    target: command\n"
        "    match:\n      argv_exact: [rm]\n",
        encoding="utf-8",
    )
    if mode == "enforce":
        with pytest.raises(ValueError, match="effect: allow") as failure:
            pj.load_policy(path)
        assert str(path) in str(failure.value)
        assert "old-block" in str(failure.value)
    else:
        with caplog.at_level("WARNING"):
            assert pj.load_policy(path).mode == "off"
        assert str(path) in caplog.text
        assert "old-block" in caplog.text
        assert "effect: allow" in caplog.text


def test_every_executable_must_match_an_allow_rule(monkeypatch):
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
    rules = _policy(_rule("A-echo", "argv_first", "echo"))
    result = pj.route("echo rm; rm file", "bash", rules)
    assert result.route is pj.Route.DEFER
    assert result.matched_rule_ids == ("A-echo",)
    complete = _policy(*rules.rules, _rule("A-rm", "argv_exact", "rm", "file"))
    assert pj.route("echo rm; rm file", "bash", complete).route is pj.Route.ALLOW


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
    rules = _policy(_rule("status", "argv_exact", "git", "status"))
    assert pj.route("git status && unknown", "bash", rules).route is pj.Route.DEFER


def test_incomplete_argv_defers_even_when_the_static_head_matches(monkeypatch):
    monkeypatch.setattr(
        pj,
        "analyze",
        lambda *a, **kw: pj.Analysis(True, False, (_command(None, ["rm"]),), ("dynamic_word",)),
    )
    allow = _rule("A", "argv_first", "rm")
    assert pj.route('rm "$X"', "bash", _policy(allow)).route is pj.Route.DEFER


def test_quoted_argument_is_not_an_executable(monkeypatch):
    monkeypatch.setattr(
        pj,
        "analyze",
        lambda *a, **kw: pj.Analysis(True, True, (_command(["echo", "rm"]),), ()),
    )
    policy = _policy(_rule("echo", "argv_exact", "echo", "rm"))
    assert pj.route('echo "rm"', "bash", policy).route is pj.Route.ALLOW
    assert pj.route('echo "rm"', "bash", _policy(_rule("rm", "argv_first", "rm"))).route is pj.Route.DEFER


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
    rules = _policy(_rule("echo", "argv_first", "echo"))
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
        {"prefix_first": ["rm"]},
        {"argv_exact": ["git"], "argv_first": ["git"]},
        {"unknown": ["git"]},
        {"argv_exact": [True]},
        {"argv_exact": [None]},
        {"command_in": ["ls"]},
        {"argv_in": [["ls"]]},
        {"args_none_of": ["--delete"]},
        {"args_prefix_none_of": ["--output"]},
        {"all": [{"command_in": ["ls"]}]},
        {"any": [{"command_in": ["ls"]}]},
        {"cli": {"command": "git", "subcommand": "status"}},
        {"args_regex": "safe"},
        {"argument_at_regex": [1, "safe"]},
        {"args_max_positionals": 1},
        {"env_names_allowed": ["LANG"]},
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
    with pytest.raises(ValueError, match="Unsupported matcher field"):
        pj.load_policy(path)


@pytest.mark.parametrize("effect", ["block", "defer", "deny"])
def test_rule_effect_must_be_allow(tmp_path, monkeypatch, effect):
    monkeypatch.setattr(pj, "_ensure_bash_parser", lambda: None)
    path = tmp_path / "prejudge.yaml"
    path.write_text(
        'schema_version: 1\nmode: "enforce"\npolicy_id: pilot\n'
        f"rules:\n  - id: invalid-effect\n    effect: {effect}\n    target: command\n"
        "    match:\n      argv_first: [rm]\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="effect: allow"):
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
        "rules:\n  - id: invalid-match\n    effect: allow\n    target: command\n"
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


def test_structured_route_does_not_invoke_legacy_checker(monkeypatch):
    import chrys.service.tools.builtins.shell_filter as old

    monkeypatch.setattr(old, "is_safe_readonly_command", lambda *a, **kw: pytest.fail("Legacy checker invoked"))
    node = pj.CommandNode((0, 2), ("ls",), ("ls",), "ls")
    monkeypatch.setattr(pj, "analyze", lambda *a, **kw: pj.Analysis(True, True, (node,), ()))
    assert pj.route("ls", "bash", _policy()).route is pj.Route.DEFER
    assert pj.route("ls", "bash", _policy(_rule("ls", "argv_exact", "ls"))).route is pj.Route.ALLOW


@pytest.mark.parametrize(
    "config",
    [
        "",
        "mode: off\n",
        "mode: false\n",
        'schema_version: 1\nmode: "off"\n',
    ],
)
def test_off_config_never_requires_parser(tmp_path, monkeypatch, config):
    def forbidden_parser():
        pytest.fail("Turning the policy off must not load Tree-sitter")

    monkeypatch.setattr(pj, "_ensure_bash_parser", forbidden_parser)
    path = tmp_path / "prejudge.yaml"
    path.write_text(config, encoding="utf-8")
    assert pj.load_policy(path).mode == "off"


def test_invalid_enforce_rule_reports_file_and_id(tmp_path, monkeypatch):
    monkeypatch.setattr(pj, "_ensure_bash_parser", lambda: None)
    path = tmp_path / "prejudge.yaml"
    path.write_text(
        'schema_version: 1\nmode: "enforce"\npolicy_id: pilot\n'
        "rules:\n  - id: typo-deny\n    effect: deny\n"
        "    target: command\n    match:\n      argv_first: [rm]\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError) as failure:
        pj.load_policy(path)
    assert str(path) in str(failure.value)
    assert "typo-deny" in str(failure.value)


def test_invalid_shadow_yaml_parse_error_is_not_suppressed(tmp_path):
    path = tmp_path / "prejudge.yaml"
    path.write_text('mode: "shadow"\nrules: [\n', encoding="utf-8")
    with pytest.raises(ValueError, match=r"prejudge\.yaml"):
        pj.load_policy(path)


def test_invalid_shadow_rule_warns_with_rule_context(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(pj, "_ensure_bash_parser", lambda: None)
    path = tmp_path / "prejudge.yaml"
    path.write_text(
        'schema_version: 1\nmode: "shadow"\npolicy_id: pilot\n'
        "rules:\n  - id: bad-shadow\n    effect: deny\n"
        "    target: command\n    match:\n      argv_first: [rm]\n",
        encoding="utf-8",
    )
    with caplog.at_level("WARNING"):
        assert pj.load_policy(path).mode == "off"
    assert str(path) in caplog.text
    assert "bad-shadow" in caplog.text


def test_unsupported_dialect_warns_allow_is_inactive(caplog):
    policy = _policy(_rule("allow", "argv_first", "rm"))
    with caplog.at_level("WARNING"):
        result = pj.route("rm x", "fish", policy)
    assert result.route is pj.Route.DEFER
    assert "ALLOW rules are inactive" in caplog.text
    assert "fish" in caplog.text
    assert "legacy readonly shortcut" in caplog.text
