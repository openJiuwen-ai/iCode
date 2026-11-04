# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Static checks of hooks configuration files, for ``icode hook list`` and ``icode hook validate``.

Files are found and read as a session finds and reads them (the project
file first, then the global one; in each folder the first of
``hooks.yaml``, ``hooks.yml``, ``hooks.json``), but every bad hook entry is
reported instead of only the first, and nothing is run.  A session refuses
a whole file over any decode, syntax or schema error; a hook whose match
regex does not compile is left out alone, and a missing script fails only
when the hook runs.
"""

from __future__ import annotations

import contextlib
import difflib
import json
import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Final, Literal

import yaml

from chrys.foundation.platform.runtime_paths import is_windows_store_python_stub
from chrys.foundation.platform.script_commands import build_script_command
from chrys.foundation.tool_kinds import TOOL_KINDS, is_extension_kind
from chrys.service.hooks.events import GATED_EVENTS, TOOL_EVENTS
from chrys.service.hooks.loader import (
    HookEntryError,
    HooksConfigError,
    attach_source,
    blocking_durable_warning,
    broad_before_tool_call_warning,
    entry_hook_id,
    hooks_file_candidates,
    id_collision_warning,
    invalid_match_regex,
    parse_hooks_dict,
    parse_hooks_text,
    project_hooks_dir,
    read_hooks_text,
    same_hooks_file,
)
from chrys.service.hooks.retired_shell_args import retired_arg_names_warning
from chrys.service.hooks.runner import TEMPLATE_VARIABLES
from chrys.service.profiles.agents.loader import scan_profile_dir
from chrys.service.profiles.agents.registry import BUILTIN_PROFILE_NAMES

if TYPE_CHECKING:
    from collections.abc import Iterable

    from chrys.service.hooks.schema import HookConfig, HooksFile

Severity = Literal["error", "warning"]
Scope = Literal["project", "global", "path"]

_TEMPLATE_RE: Final = re.compile(r"\$\{([^}]*)\}")
_REJECTING_CODES: Final = frozenset({"unreadable", "decode", "syntax", "schema"})
# What the parser quotes with repr(), in either quote.
_QUOTED_TEXT: Final = re.compile(r"'((?:[^'\\]|\\.)*)'|\"((?:[^\"\\]|\\.)*)\"")
_ONE_CHARACTER: Final = re.compile(r"[^\\]|\\(?:x[0-9a-f]{2}|u[0-9a-f]{4}|U[0-9a-f]{8}|.)")
_TOKEN_NAME: Final = re.compile(r"<[a-z ]+>")


@dataclass(frozen=True, slots=True)
class HookDiagnostic:
    """One problem found in a hooks file.

    ``line`` is 1-based: the hook entry's first line, or where parsing
    failed; ``entry`` is the hook's index in the ``hooks`` list.
    """

    severity: Severity
    code: str
    message: str
    file: str | None = None
    line: int | None = None
    column: int | None = None
    hook_id: str | None = None
    entry: int | None = None


@dataclass(frozen=True, slots=True)
class HooksSource:
    """A hooks file to check: where it was found and whether a session started here loads it."""

    scope: Scope
    path: Path
    loaded: bool | None
    """None for a file named on the command line, which loads or not depending on where a session starts."""
    shadowed: tuple[Path, ...] = ()
    """The other hooks files in the same folder, which a session ignores."""


@dataclass(frozen=True, slots=True)
class CheckedFile:
    """What one hooks file holds and whether a session accepts it."""

    source: HooksSource
    hooks_file: HooksFile | None
    """What a session would load; None when it refuses the whole file."""
    rejection: HookDiagnostic | None
    """The first error over which a session refuses the file."""
    entry_count: int


@dataclass(frozen=True, slots=True)
class HooksReport:
    files: tuple[CheckedFile, ...]
    diagnostics: tuple[HookDiagnostic, ...]
    searched: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return not any(diagnostic.severity == "error" for diagnostic in self.diagnostics)

    @property
    def hook_count(self) -> int:
        return sum(checked.entry_count for checked in self.files)


@dataclass(slots=True)
class _FileCheck:
    source: HooksSource
    diagnostics: list[HookDiagnostic] = field(default_factory=list)
    hooks_file: HooksFile | None = None
    entry_lines: list[int] = field(default_factory=list)
    entry_index: dict[str, int] = field(default_factory=dict)
    entry_count: int = 0

    def report(
        self,
        severity: Severity,
        code: str,
        message: str,
        *,
        entry: int | None = None,
        hook_id: str | None = None,
        line: int | None = None,
        column: int | None = None,
    ) -> None:
        if line is None and entry is not None and entry < len(self.entry_lines):
            line = self.entry_lines[entry]
        self.diagnostics.append(
            HookDiagnostic(severity, code, message, str(self.source.path), line, column, hook_id, entry)
        )

    @property
    def rejection(self) -> HookDiagnostic | None:
        return next((d for d in self.diagnostics if d.code in _REJECTING_CODES), None)


def default_sources(config_dir: Path, project_root: Path, *, project_hooks_enabled: bool) -> list[HooksSource]:
    """The hooks files a session started in *project_root* finds, project first.

    A project folder that is the user's own hooks folder (a session started
    in the home directory) names the global file again, which loads once,
    as the user's.
    """
    project = hooks_file_candidates(project_hooks_dir(project_root))
    global_ = hooks_file_candidates(config_dir / "hooks")
    if project and global_ and same_hooks_file(project[0], global_[0]):
        project = []
    sources: list[HooksSource] = []
    if project:
        sources.append(HooksSource("project", project[0], project_hooks_enabled, tuple(project[1:])))
    if global_:
        sources.append(HooksSource("global", global_[0], True, tuple(global_[1:])))
    return sources


def check_default_sources(config_dir: Path, project_root: Path, *, project_hooks_enabled: bool) -> HooksReport:
    """Check the project and global hooks files of a session started in *project_root*."""
    searched = (str(project_hooks_dir(project_root)), str(config_dir / "hooks"))
    sources = default_sources(config_dir, project_root, project_hooks_enabled=project_hooks_enabled)
    if not sources:
        missing = HookDiagnostic("error", "no_config", f"no hooks configuration file in {searched[0]} or {searched[1]}")
        return HooksReport((), (missing,), searched)
    return _check_sources(sources, config_dir, searched)


def check_path(path: Path, *, config_dir: Path) -> HooksReport:
    """Check the hooks file *path*, or the one a session would read from the folder *path*."""
    path = path.absolute()
    searched = (str(path),)
    if path.is_dir():
        candidates = hooks_file_candidates(path)
        if not candidates:
            missing = HookDiagnostic("error", "no_config", f"no hooks.yaml, hooks.yml or hooks.json in {path}")
            return HooksReport((), (missing,), searched)
        source = HooksSource("path", candidates[0], None, tuple(candidates[1:]))
    elif path.exists():
        source = HooksSource("path", path, None)
    else:
        missing = HookDiagnostic("error", "not_found", f"no such file or folder: {path}", str(path))
        return HooksReport((), (missing,), searched)
    return _check_sources([source], config_dir, searched)


def profile_names(config_dir: Path) -> frozenset[str]:
    """The agent profile names ``match.profile`` can name, read without changing any profile file."""
    user = scan_profile_dir(config_dir / "agents")
    return BUILTIN_PROFILE_NAMES | {profile.name for _, profile in user.loaded}


def _check_sources(sources: list[HooksSource], config_dir: Path, searched: tuple[str, ...]) -> HooksReport:
    profiles = profile_names(config_dir)
    checks = [_check_file(source, profiles) for source in sources]
    diagnostics: list[HookDiagnostic] = []
    for check in checks:
        diagnostics.extend(
            HookDiagnostic(
                "warning",
                "shadowed_file",
                f"ignored: {check.source.path.name} in the same folder is read instead",
                str(shadowed),
            )
            for shadowed in check.source.shadowed
        )
        diagnostics.extend(sorted(check.diagnostics, key=lambda diagnostic: diagnostic.line or 0))
    diagnostics.extend(_collisions(checks))
    files = tuple(CheckedFile(check.source, check.hooks_file, check.rejection, check.entry_count) for check in checks)
    return HooksReport(files, tuple(diagnostics), searched)


def _check_file(source: HooksSource, profiles: frozenset[str]) -> _FileCheck:
    check = _FileCheck(source)
    try:
        text = read_hooks_text(source.path)
    except HooksConfigError as exc:
        check.report("error", "decode", f"cannot decode the file: {exc.__cause__ or exc}")
        return check
    except OSError as exc:
        check.report("error", "unreadable", f"cannot read the file: {exc.strerror or exc}")
        return check
    try:
        raw = parse_hooks_text(text, source.path)
    except yaml.YAMLError as exc:
        message, line, column = _yaml_problem(exc)
        check.report("error", "syntax", message, line=line, column=column)
        return check
    except json.JSONDecodeError as exc:
        check.report("error", "syntax", exc.msg, line=exc.lineno, column=exc.colno)
        return check

    entries = raw.get("hooks") if isinstance(raw, dict) else None
    if isinstance(entries, list):
        check.entry_count = len(entries)
        # JSON allows tabs between tokens, where YAML does not; a space keeps every line where it was.
        check.entry_lines = _entry_lines(text.replace("\t", " ") if source.path.suffix.lower() == ".json" else text)
        for index, item in enumerate(entries):
            hook_id = entry_hook_id(item)
            if hook_id is not None:
                check.entry_index.setdefault(hook_id, index)
    entry_errors: list[HookEntryError] = []
    try:
        hooks_file = parse_hooks_dict(raw, entry_errors=entry_errors)
    except HooksConfigError as exc:
        check.report("error", "schema", str(exc))
        return check
    for error in entry_errors:
        # The diagnostic names its hook itself.
        detail = error.detail.removeprefix(f"hook '{error.hook_id}': ")
        check.report("error", "schema", detail, entry=error.index, hook_id=error.hook_id)
    attach_source(hooks_file, source.path)
    for skipped in hooks_file.skipped_hooks:
        check.report(
            "error",
            "regex_invalid",
            f"{skipped.reason}; a session leaves this hook out",
            entry=check.entry_index.get(skipped.id),
            hook_id=skipped.id,
        )
    for hook in hooks_file.hooks:
        _check_hook(check, hook, profiles)
    if check.rejection is None:
        check.hooks_file = hooks_file
    return check


def _check_hook(check: _FileCheck, hook: HookConfig, profiles: frozenset[str]) -> None:
    entry = check.entry_index.get(hook.id)

    def warn(code: str, message: str) -> None:
        check.report("warning", code, message, entry=entry, hook_id=hook.id)

    match = hook.match
    tool_fields = [
        f"match.{name}"
        for name, present in (
            ("tool_kind", match.tool_kind is not None),
            ("tool_name", match.tool_name is not None),
            ("args", bool(match.args)),
        )
        if present
    ]
    if tool_fields and hook.event not in TOOL_EVENTS:
        verb = "never matches" if len(tool_fields) == 1 else "never match"
        warn("match_never_fires", f"{' and '.join(tool_fields)} {verb} on {hook.event}, which has no tool")
    # An extension's kind is valid whether or not it is installed, so a shared hooks file stays quiet.
    if match.tool_kind is not None and match.tool_kind not in TOOL_KINDS and not is_extension_kind(match.tool_kind):
        warn(
            "unknown_tool_kind",
            f"match.tool_kind {match.tool_kind!r} is not a tool kind{_did_you_mean(match.tool_kind, TOOL_KINDS)}",
        )
    named = [("match.profile", match.profile)] if match.profile else []
    named.extend(("match.profiles", name) for name in match.profiles if name)
    for where, name in named:
        if name not in profiles:
            warn("unknown_profile", f"{where} {name!r} is not an agent profile name{_did_you_mean(name, profiles)}")
    execution = hook.execution
    if execution.on_error == "block" and (execution.mode != "blocking" or hook.event not in GATED_EVENTS):
        warn(
            "on_error_block_ignored",
            "execution.on_error 'block' applies only to blocking hooks on before_tool_call and "
            "user_prompt_submit; this hook's failures are handled as 'warn'",
        )
    if (message := blocking_durable_warning(hook)) is not None:
        warn("durable_blocking", message)
    if (message := broad_before_tool_call_warning(hook)) is not None:
        warn("broad_before_tool_call", message)
    if (message := retired_arg_names_warning(hook)) is not None:  # DEPRECATED with the retired names
        warn("retired_shell_arg", message)
    run = hook.run
    known = ", ".join(f"${{{variable}}}" for variable in TEMPLATE_VARIABLES)
    for where, value in [("run.cwd", run.cwd), *((f"run.env.{key}", value) for key, value in run.env.items())]:
        for name in _TEMPLATE_RE.findall(value):
            if name not in TEMPLATE_VARIABLES:
                warn(
                    "unknown_template_var",
                    f"{where} uses {_shown_variable(name)}, which is passed on as written; "
                    f"the variables filled in are {known}",
                )
    # What a hook runs is started as written: no variable in its arguments is filled in.
    arguments = [(f"run.args[{index}]", value) for index, value in enumerate(run.args)] if run.type != "shell" else []
    if run.type == "command":
        arguments[:0] = [(f"run.argv[{index}]", value) for index, value in enumerate(run.argv)]
    for where, value in arguments:
        for name in _TEMPLATE_RE.findall(value):
            warn(
                "unknown_template_var",
                f"{where} uses {_shown_variable(name)}, which is passed on as written; "
                "variables are filled in only in run.cwd and run.env",
            )
    if not hook.enabled and (reason := invalid_match_regex(hook.match)) is not None:
        # A session checks only enabled hooks; this one would be left out once enabled.
        check.report("error", "regex_invalid", reason, entry=entry, hook_id=hook.id)
    if hook.enabled:
        _check_program(check, hook, entry)


def _check_program(check: _FileCheck, hook: HookConfig, entry: int | None) -> None:
    """Check that what an enabled hook runs exists: its script, and the program that starts it.

    The messages never quote run.path or run.argv[0], which can hold more than a path (``notify --token abc``); the
    line and the hook's id point at it.
    """
    run = hook.run
    if run.type == "script":
        script = Path(run.path)
        if not script.is_file():
            message = "the script in run.path was not found; a relative path starts from the folder of this file"
            written = run.path
            with contextlib.suppress(ValueError):
                # What was written below the file's folder, whose own path may hold spaces.
                written = str(script.relative_to(check.source.path.parent.resolve()))
            if _may_hold_arguments(written):
                message += "; if run.path also holds arguments, put them in run.args"
            check.report("error", "script_missing", message, entry=entry, hook_id=hook.id)
            return
        try:
            program = build_script_command(script)[0]
        except FileNotFoundError as error:
            check.report("warning", "program_not_found", error.strerror or str(error), entry=entry, hook_id=hook.id)
            return
        what = f"which runs {script.suffix} scripts" if script.suffix else "which runs this script"
        missing = f"{program!r}, {what}, was not found on PATH"
    elif run.type == "command":
        program = run.argv[0]
        if "${" in program or (not os.path.isabs(program) and os.path.dirname(program)):
            # Relative to the hook's working folder, known only when it runs.
            return
        missing = (
            "the program in run.argv[0] does not exist or is not executable"
            if os.path.isabs(program)
            else "the program in run.argv[0] was not found on PATH"
        )
        if _may_hold_arguments(program):
            missing += (
                "; if run.argv[0] also holds arguments or sets variables, put each argument in its own run.argv item "
                "and variables in run.env"
            )
    else:
        return
    if _sets_path(run.env.keys()):
        return
    found = shutil.which(program)
    if found is None:
        check.report("warning", "program_not_found", missing, entry=entry, hook_id=hook.id)
    elif is_windows_store_python_stub(found):
        # Script runs pass over it; a command is looked up by Windows, which takes it.
        message = (
            "on PATH, the program in run.argv[0] is only Windows' placeholder that offers Python from the Microsoft "
            "Store; write the full path of an installed Python"
        )
        check.report("warning", "program_not_found", message, entry=entry, hook_id=hook.id)


def _may_hold_arguments(target: str) -> bool:
    return any(char.isspace() or char == "=" for char in target)


def _shown_variable(name: str) -> str:
    """``${name}``, or a stand-in when *name* is not a plain name: ``${TOKEN:-default}`` can carry a secret."""
    return f"${{{name}}}" if name.isidentifier() else "a ${...} expression"


def _sets_path(names: Iterable[str]) -> bool:
    return any(name.upper() == "PATH" for name in names)


def _did_you_mean(value: str, choices: Iterable[str]) -> str:
    ordered = sorted(choices)
    close = [choice for choice in ordered if choice.casefold() == value.casefold()]
    close = close or difflib.get_close_matches(value, ordered, n=1)
    return f"; did you mean {close[0]!r}?" if close else ""


def _collisions(checks: list[_FileCheck]) -> list[HookDiagnostic]:
    """Ids a session runs twice: defined by an enabled hook in both the project and the global file while both load."""
    loaded = {check.source.scope: check for check in checks if check.source.loaded and check.hooks_file is not None}
    project, global_ = loaded.get("project"), loaded.get("global")
    if project is None or global_ is None or project.hooks_file is None or global_.hooks_file is None:
        return []
    global_ids = {hook.id for hook in global_.hooks_file.hooks if hook.enabled}
    found: list[HookDiagnostic] = []
    for hook in project.hooks_file.hooks:
        if not hook.enabled or hook.id not in global_ids:
            continue
        entry = project.entry_index.get(hook.id)
        found.append(
            HookDiagnostic(
                "warning",
                "duplicate_id_across_files",
                id_collision_warning(str(project.source.path), str(global_.source.path)),
                str(project.source.path),
                project.entry_lines[entry] if entry is not None and entry < len(project.entry_lines) else None,
                hook_id=hook.id,
                entry=entry,
            )
        )
    return found


def _entry_lines(text: str) -> list[int]:
    """The 1-based first line of each ``hooks`` entry, or nothing when the text cannot be mapped."""
    try:
        root = yaml.compose(text, Loader=yaml.SafeLoader)
    except yaml.YAMLError, RecursionError:
        return []
    lines: list[int] = []
    if isinstance(root, yaml.MappingNode):
        for key, value in root.value:
            # The last ``hooks`` key wins, as it does when the text is loaded.
            if isinstance(key, yaml.ScalarNode) and key.value == "hooks" and isinstance(value, yaml.SequenceNode):
                lines = [item.start_mark.line + 1 for item in value.value]
    return lines


def hooks_config_problem(exc: HooksConfigError) -> str:
    """What is wrong with a hooks file, for a report line: a parse failure quotes nothing from the file."""
    cause = exc.__cause__
    if not isinstance(cause, yaml.YAMLError):
        return str(exc)
    message, line, column = _yaml_problem(cause)
    return f"{message} (line {line}, column {column})" if line is not None else message


def _yaml_problem(exc: yaml.YAMLError) -> tuple[str, int | None, int | None]:
    """The parser's own description of *exc* and where it is, without the excerpt of the file."""
    if isinstance(exc, yaml.MarkedYAMLError) and exc.problem:
        message = _without_file_text(f"{exc.problem} ({exc.context})" if exc.context else exc.problem)
        mark = exc.problem_mark
        if mark is None:
            return message, None, None
        return message, mark.line + 1, mark.column + 1
    return _without_file_text((str(exc).splitlines() or [type(exc).__name__])[0]), None, None


def _without_file_text(message: str) -> str:
    """*message* with the text it quotes from the file left out, except single characters and names like ``<scalar>``.

    The parser quotes a tag or an alias it cannot use, which can be a secret written without quotes (``TOKEN: *abc``).
    """

    def kept(match: re.Match[str]) -> str:
        quoted = match[1] if match[1] is not None else match[2]
        return match[0] if _ONE_CHARACTER.fullmatch(quoted) or _TOKEN_NAME.fullmatch(quoted) else "'…'"

    return _QUOTED_TEXT.sub(kept, message)
