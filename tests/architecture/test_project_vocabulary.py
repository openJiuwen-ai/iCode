# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Guard the names of the working-directory settings and source scope."""

from __future__ import annotations

import argparse
import re
import tokenize
from pathlib import Path

from tests.support.ci import CI_LINUX_ONLY

pytestmark = CI_LINUX_ONLY

ROOT = Path(__file__).resolve().parents[2]
_RETIRED = re.compile(
    r"project(?:[._-](?:config|hooks?|skills?|scope|source|trust|workflows?|shortcuts?|directory|cwd|root)\w*"
    r"|[- ](?:settings|hooks?|skills|scope|source|trust|workflows?|shortcuts?|directory)\b)"
    r"|--project\b|[`\"]project[`\"]"
    r"|项目(?:级设置|设置|钩子|信任| Skills|工作流)|本项目",
    re.IGNORECASE,
)
_PYTHON_PROJECT = re.compile(
    r"\bproject_(?:config|hooks?|skills?|scope|source|trust|workflows?|shortcuts?|cwd|root|merge|path)\w*"
    r"|(?:Project(?:Merge|Path|Scope|Source)|(?:SOURCE_KIND|NOT_ALLOWED_IN|EXACT|PREFIX)_PROJECT)\w*|\bSource\.PROJECT\b",
)
_UNDERSCORED_SCOPE = re.compile(r"(?:\w*_)?working_dir_(?:config|hooks?|skills?|scope|source|trust)\w*", re.IGNORECASE)
# Compatibility reads and the provider account field.
_ALLOW = (
    '"project.config_enabled": "workdir.config_enabled"',
    '"project.hooks_enabled": "workdir.hooks_enabled"',
    '"project.skills_enabled": "workdir.skills_enabled"',
    'legacy_section = "project"',
    'record.get(field) != "project"',
    'row.get("scope") == "PROJECT"',
    'and "project" in row',
    'row.pop("project", None)',
    'row["scope"] == "PROJECT"',
    '"project": os.environ.get("OPENAI_PROJECT_ID", "")',
)


def retired_words(text: str) -> str:
    for fragment in _ALLOW:
        text = text.replace(fragment, "")
    return ", ".join(_RETIRED.findall(text))


_SOURCE_LITERAL = re.compile(r'["\'](?:PROJECT|project)["\'.]|["\']--project\b')


def _code_without_prose(path: Path, lines: list[str]) -> list[str]:
    """Blank out comments and string literals, keeping identifiers and syntax."""
    code = [list(line) for line in lines]
    prose = {tokenize.COMMENT, tokenize.STRING, getattr(tokenize, "FSTRING_MIDDLE", -1)}
    with path.open("rb") as handle:
        for token in tokenize.tokenize(handle.readline):
            if token.type not in prose:
                continue
            (start_row, start_col), (end_row, end_col) = token.start, token.end
            for row in range(start_row, end_row + 1):
                first = start_col if row == start_row else 0
                last = end_col if row == end_row else len(code[row - 1])
                code[row - 1][first:last] = " " * (last - first)
    return ["".join(line) for line in code]


def test_no_retired_directory_vocabulary_in_source_docs_or_catalogs() -> None:
    offenders = []
    for directory in ("src", "docs", "locales"):
        for path in sorted((ROOT / directory).rglob("*")):
            if path.suffix not in {".py", ".md", ".po", ".pot", ".json", ".yaml", ".yml", ".tcss"}:
                continue
            relative = path.relative_to(ROOT).as_posix()
            lines = path.read_text(encoding="utf-8").splitlines()
            # Python prose (comments, docstrings, prompts to the model) is not a
            # concept name; user-facing strings reach the scan through the catalogs.
            texts = _code_without_prose(path, lines) if path.suffix == ".py" else lines
            for number, (line, text) in enumerate(zip(lines, texts, strict=True), 1):
                if path.suffix == ".po" and line.startswith("#~"):
                    continue
                if relative == "src/chrys/app/cli/approvals.py" and line.strip() == (
                    'parser.add_argument("--project", dest="workdir", help=argparse.SUPPRESS)'
                ):
                    continue
                if relative == "src/chrys/service/state/_session_files.py":
                    line = line.replace("b'\"project\"'", "")  # legacy checkpoint probe
                old_names = _PYTHON_PROJECT.search(text) if path.suffix == ".py" else None
                legacy_literal = path.suffix == ".py" and retired_words(line) if _SOURCE_LITERAL.search(line) else ""
                if directory == "docs" and path.name == "python-extensions.md":
                    text = text.replace('data["project"]["version"]', "")  # pyproject.toml metadata example
                if old_names or legacy_literal or retired_words(text):
                    offenders.append(f"{relative}:{number}: {line.strip()}")
    assert not offenders, "\n".join(offenders)


def test_directory_scope_identifiers_use_workdir() -> None:
    offenders = []
    for directory in ("src", "tests"):
        for path in sorted((ROOT / directory).rglob("*.py")):
            with path.open("rb") as handle:
                for token in tokenize.tokenize(handle.readline):
                    if token.type == tokenize.NAME and _UNDERSCORED_SCOPE.fullmatch(token.string):
                        offenders.append(f"{path.relative_to(ROOT)}:{token.start[0]}: {token.string}")
    assert not offenders, "Use workdir in directory-scope identifiers:\n" + "\n".join(offenders)


def test_no_new_test_file_uses_the_underscored_spelling() -> None:
    existing = {
        "tests/app/cli/test_missing_working_dir.py",
        "tests/app/tui/behaviors/test_missing_working_dir_flows.py",
        "tests/app/tui/screens/main/test_missing_working_dir.py",
        "tests/orchestration/engine/run/test_working_dir_gate.py",
        "tests/orchestration/workflows/test_session_working_dir.py",
        "tests/service/agent_middleware/test_approval_reuse_working_dir.py",
        "tests/service/tools/test_working_dir_missing.py",
    }
    found = {path.relative_to(ROOT).as_posix() for path in (ROOT / "tests").rglob("test_*working_dir*.py")}
    assert not (found - existing), f"Use workdir in new test filenames: {sorted(found - existing)}"


def _help(parser: argparse.ArgumentParser) -> str:
    children = [
        choice
        for action in parser._actions
        if isinstance(action, argparse._SubParsersAction)
        for choice in action.choices.values()
    ]
    return "\n".join([parser.format_help(), *(_help(child) for child in children)])


def test_cli_help_uses_current_directory_vocabulary() -> None:
    from chrys.app.cli import acp, app, approvals, hooks, rsi, run, serve, trajectory, workflow

    for module in (acp, app, approvals, hooks, rsi, run, serve, trajectory, workflow):
        assert not retired_words(_help(module.build_parser()))
    assert "--workdir" in approvals.build_parser().format_help()
    assert "--workspace" in approvals.build_parser().format_help()
    assert approvals.build_parser().parse_args(["--project", "."]).workdir == "."
