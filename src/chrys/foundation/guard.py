# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The one place iCode's core meets a process-level guard (the file-access sandbox).

A guard confines what iCode and every program it starts can read and change.
It is a separate distribution (``icode-sandbox``) that iCode loads only when
the person asked for it, at startup and before any thread exists; the rest of
iCode talks to it only through this module. With no guard asked for, every
function here does nothing, so iCode behaves as it does without one.

Rules this module enforces, not the guard:

- **Asked for or not** is decided here, once, from ``--sandbox``/``--no-sandbox``,
  then ``CHRYS_FILE_SANDBOX``, then ``sandbox.files.enabled`` in the working directory's
  settings (only when the person enabled those, and they can only turn it on), then in the user's ``settings.yaml``.
- **Where it comes from**: the ``chrys.runtime_guards`` entry point of the
  ``icode-sandbox`` distribution only; another distribution offering the same
  entry is ignored.
- **Asked for but unavailable** (not installed, failing to load, failing its
  own check): iCode refuses to start, or with ``on_unavailable: warn`` starts
  without it and shows that it is not active. It never runs a program outside
  a guard that is on.
"""

from __future__ import annotations

import contextlib
import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from chrys.foundation.branding import APP_DISPLAY_NAME

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

ENTRY_POINT_GROUP = "chrys.runtime_guards"
DISTRIBUTION = "icode-sandbox"
ENV_ENABLED = "CHRYS_FILE_SANDBOX"
ENV_ON_UNAVAILABLE = "CHRYS_FILE_SANDBOX_ON_UNAVAILABLE"
EXIT_UNAVAILABLE = 78

_TRUE = {"1", "true", "on", "yes"}
_FALSE = {"0", "false", "off", "no"}


@dataclass
class Decision:
    """Whether a guard was asked for, and what to do when it can't be had."""

    enabled: bool = False
    source: str = "default"  # default | settings | workdir | env | cli
    on_unavailable: str = "refuse"  # refuse | warn
    workdir_off_ignored: bool = False
    """The working directory's settings asked to turn it off, which they cannot."""
    launch_roots: tuple[str, ...] | None = None
    """Startup directories resolved by the frontend; None uses the basic cwd parser."""
    settings_problem: str = ""
    """Why the user's settings file could not be read; its sandbox settings were not used."""


@dataclass
class GuardStatus:
    state: str = "inactive"  # active | degraded | inactive
    requested: bool = False
    mechanism: str = ""
    reasons: list[str] = field(default_factory=list)
    inherited: bool = False
    capabilities: dict[str, bool] = field(default_factory=dict)
    """What the guard's backend on this platform can do, as it declares it."""

    def label(self) -> str:
        if self.state == "inactive" and self.requested:
            return "requested-not-active"
        return self.state


class ToolCallHost(Protocol):
    """What a tool call's approval middleware offers the guard."""

    @property
    def session_id(self) -> str | None: ...

    @property
    def workspace_roots(self) -> list[str]: ...

    @property
    def workspace_cwd(self) -> str | None: ...

    @property
    def can_request(self) -> bool:
        """False for sub-agents: they share their parent's grants but can't ask for more."""
        ...

    def tool_kind(self, context: Any) -> str: ...

    def may_ask(self) -> bool:
        """Whether the person can be asked now (manual approval)."""
        ...

    def user_messages(self) -> list[str]: ...

    async def ask(self, context: Any, *, path: str, tool_name: str, kind: str, intent: str, offer: Any) -> Any:
        """Ask the person about *path*; the ``ApprovalResponse``, or ``None`` when nobody answered."""
        ...

    def refuse(self, context: Any, message: str) -> None:
        """End the call with *message* as its result, recorded as refused by the guard."""
        ...

    async def publish(self, event: Any) -> None: ...


class ToolCalls(Protocol):
    """A guard's part in one agent's tool calls."""

    async def run(self, context: Any, proceed: Callable[[], Awaitable[None]]) -> None:
        """Run the call within the guard's scope, asking or refusing first as needed; *proceed* runs it."""
        ...

    def end_turn(self) -> None: ...

    def close(self) -> None: ...


class Guard(Protocol):
    """What iCode's core asks of a loaded guard."""

    @property
    def active(self) -> bool:
        """Whether this process runs under the guard (it confined itself at start)."""
        ...

    def status(self) -> GuardStatus: ...

    def tool_calls(self, host: ToolCallHost) -> ToolCalls: ...

    def session_scope(
        self, session_id: str | None, roots: list[str], cwd: str | None
    ) -> contextlib.AbstractContextManager[None]: ...

    def user_reads(self) -> contextlib.AbstractContextManager[None]: ...

    def launching_configured(
        self,
        command: str | None = None,
        args: tuple[str, ...] | list[str] = (),
        cwd: str | None = None,
        *,
        reads: list[str] | tuple[str, ...] = (),
        writes: list[str] | tuple[str, ...] = (),
    ) -> contextlib.AbstractContextManager[None]: ...

    def controlling_terminal(self) -> contextlib.AbstractContextManager[None]: ...

    def walk_start(self, directory: str) -> str | None: ...

    def cli(self, argv: list[str]) -> int: ...

    def indicator(self) -> Any:
        """The footer widget showing the state, or ``None``."""
        ...

    def panel(self) -> Any:
        """The screen to push for the panel (F7), or ``None``."""
        ...


class _NoGuard:
    """No guard: every hook does nothing."""

    def __init__(self, status: GuardStatus | None = None) -> None:
        self._status = status or GuardStatus()

    @property
    def active(self) -> bool:
        return False

    def status(self) -> GuardStatus:
        return self._status

    def tool_calls(self, host: ToolCallHost) -> ToolCalls:
        return _NoToolCalls()

    def session_scope(
        self, session_id: str | None, roots: list[str], cwd: str | None
    ) -> contextlib.AbstractContextManager[None]:
        return contextlib.nullcontext()

    def user_reads(self) -> contextlib.AbstractContextManager[None]:
        return contextlib.nullcontext()

    def launching_configured(
        self,
        command: str | None = None,
        args: tuple[str, ...] | list[str] = (),
        cwd: str | None = None,
        *,
        reads: list[str] | tuple[str, ...] = (),
        writes: list[str] | tuple[str, ...] = (),
    ) -> contextlib.AbstractContextManager[None]:
        return contextlib.nullcontext()

    def controlling_terminal(self) -> contextlib.AbstractContextManager[None]:
        return contextlib.nullcontext()

    def walk_start(self, directory: str) -> str | None:
        return None

    def cli(self, argv: list[str]) -> int:
        sys.stderr.write(
            f"{APP_DISPLAY_NAME}: the file-access sandbox is not installed (the {DISTRIBUTION} package).\n"
        )
        return 2

    def indicator(self) -> Any:
        return None

    def panel(self) -> Any:
        return None


class _NoToolCalls:
    async def run(self, context: Any, proceed: Callable[[], Awaitable[None]]) -> None:
        await proceed()

    def end_turn(self) -> None:
        return None

    def close(self) -> None:
        return None


_current: Guard = _NoGuard()
_launch_decision: Decision | None = None


def current() -> Guard:
    """The guard of this process (a do-nothing one when none was asked for)."""
    return _current


def status() -> GuardStatus:
    return _current.status()


def status_line() -> str | None:
    """One line on the guard for headless output, or ``None`` when it was not asked for."""
    st = _current.status()
    if not st.requested:
        return None
    mechanism = f" ({st.mechanism})" if st.mechanism else ""
    reasons = f": {'; '.join(st.reasons)}" if st.reasons else ""
    return f"Sandbox: {st.label()}{mechanism}{reasons}"


def status_report() -> dict[str, Any]:
    """The guard's status as data (ACP ``initialize`` metadata, JSON output)."""
    st = _current.status()
    return {"state": st.state, "requested": st.requested, "mechanism": st.mechanism, "reasons": list(st.reasons)}


def set_current(guard: Guard, *, decision: Decision | None = None) -> None:
    """Install *guard* for this process (the loader and tests only)."""
    global _current, _launch_decision
    _current = guard
    _launch_decision = decision


# ---------------------------------------------------------------- deciding


def as_bool(value: Any) -> bool | None:
    from chrys.foundation.config.coercion import bool_coercer

    coerced = bool_coercer()(value)
    return coerced.value if isinstance(coerced.value, bool) else None


def settings_doc(path: Path, *, owned: bool = False, problems: list[str] | None = None) -> dict[str, Any]:
    """``sandbox.files.*`` and ``workdir.config_enabled`` from a settings document (nested or dotted keys).

    *owned* is the user's own document: read by its real path, so a config
    folder or settings file kept elsewhere through a link (dotfiles) still counts.
    A working directory's document is read as untrusted: never through a link.
    Trust keys saved under their previous names count as the loader reads them.

    A missing document is empty. One that exists but can't be read or parsed is
    empty too, and for the user's own document the reason goes to *problems*:
    a broken file must not turn the sandbox off without a word.
    """
    try:
        from chrys.foundation.config.user_settings import settings_edit
        from chrys.foundation.config.yaml_store import read_yaml_doc_readonly

        doc = read_yaml_doc_readonly(Path(os.path.realpath(path)) if owned else path, strict=owned) or {}
        settings_edit(doc)
    except Exception as exc:
        if owned and problems is not None:
            problems.append(str(exc) or type(exc).__name__)
        return {}
    flat: dict[str, Any] = {}

    def walk(prefix: str, node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                walk(f"{prefix}.{key}" if prefix else str(key), value)
        else:
            flat[prefix] = node

    walk("", doc)
    return {k: v for k, v in flat.items() if k.startswith("sandbox.files.") or k == "workdir.config_enabled"}


def launch_dirs(argv: list[str]) -> list[str]:
    """The working directory a command starts in (``-C``/``--workdir`` or cwd); none if cwd is gone.

    A deleted current directory is the command's to report, as it does
    without a guard: nothing here may fail first.
    """
    for index, arg in enumerate(argv):
        if arg in ("-C", "--workdir") and index + 1 < len(argv):
            return [os.path.abspath(os.path.expanduser(argv[index + 1]))]
        if arg.startswith("--workdir="):
            return [os.path.abspath(os.path.expanduser(arg.split("=", 1)[1]))]
    try:
        return [os.getcwd()]
    except OSError:
        return []


def parse_flags(argv: list[str]) -> tuple[list[str], bool | None, str | None]:
    """Take startup sandbox options, leaving literal arguments after ``--`` alone."""
    rest: list[str] = []
    wanted: bool | None = None
    policy: str | None = None
    args = iter(argv)
    for arg in args:
        if arg == "--":
            rest.extend([arg, *args])
            break
        if arg in ("--sandbox", "--no-sandbox"):
            wanted = arg == "--sandbox"
        elif arg == "--sandbox-on-unavailable" or arg.startswith("--sandbox-on-unavailable="):
            value = next(args, "") if arg == "--sandbox-on-unavailable" else arg.split("=", 1)[1]
            if value not in {"refuse", "warn"}:
                sys.stderr.write(f"{APP_DISPLAY_NAME}: --sandbox-on-unavailable must be refuse or warn\n")
                raise SystemExit(2)
            policy = value
        else:
            rest.append(arg)
    return rest, wanted, policy


def strip_flags(argv: list[str]) -> tuple[list[str], bool | None]:
    """The command arguments and the last sandbox switch, ignoring literal arguments after ``--``."""
    rest, wanted, _policy = parse_flags(argv)
    return rest, wanted


def startup_flags(enabled: bool | None, on_unavailable: str | None = None) -> list[str]:
    """Explicit startup choices for a newly launched iCode process."""
    flags = [] if enabled is None else ["--sandbox" if enabled else "--no-sandbox"]
    if on_unavailable is not None:
        flags.append(f"--sandbox-on-unavailable={on_unavailable}")
    return flags


def fork_flags() -> list[str]:
    """Carry the parent's choice and effective failure policy across desktop launches.

    A workdir-only request may warn; promoting it to an explicit request in the
    child must not silently turn that warning into a startup refusal. Preserve
    the policy even when a terminal host does not inherit our environment.
    """
    decision = _launch_decision
    if decision is None:
        return startup_flags(True) if status().requested else []
    if not decision.enabled:
        return startup_flags(False) if decision.source != "default" else []
    policy = "warn" if decision.source == "workdir" else decision.on_unavailable
    return startup_flags(True, policy)


def decide(
    argv: list[str],
    config_dir: Path,
    env: Mapping[str, str] | None = None,
    *,
    resolve_launch_roots: Callable[[list[str]], list[str]] | None = None,
) -> tuple[list[str], Decision]:
    """Strip ``--sandbox``/``--no-sandbox`` from *argv* and decide whether a guard is asked for."""
    environ: Mapping[str, str] = os.environ if env is None else env
    problems: list[str] = []
    doc = settings_doc(config_dir / "settings.yaml", owned=True, problems=problems)
    decision = Decision(settings_problem="; ".join(problems))
    user_enabled = as_bool(doc.get("sandbox.files.enabled"))
    if user_enabled is not None:
        decision.enabled, decision.source = user_enabled, "settings"

    def roots() -> tuple[str, ...]:
        if decision.launch_roots is None:
            decision.launch_roots = tuple((resolve_launch_roots or launch_dirs)(argv))
        return decision.launch_roots

    workdir_dirs = roots() if as_bool(doc.get("workdir.config_enabled")) is True else ()
    if workdir_dirs:
        workdir_doc = settings_doc(Path(workdir_dirs[0]) / ".chrys" / "settings.yaml")
        workdir_enabled = as_bool(workdir_doc.get("sandbox.files.enabled"))
        if workdir_enabled is True and not decision.enabled:
            decision.enabled, decision.source = True, "workdir"
        elif workdir_enabled is False:
            decision.workdir_off_ignored = True
    on_unavailable = str(doc.get("sandbox.files.on_unavailable", "")).lower()
    if on_unavailable in {"refuse", "warn"}:
        decision.on_unavailable = on_unavailable
    env_value = environ.get(ENV_ENABLED, "").strip().lower()
    if env_value in _TRUE | _FALSE:
        decision.enabled, decision.source = env_value in _TRUE, "env"
    env_unavailable = environ.get(ENV_ON_UNAVAILABLE, "").strip().lower()
    if env_unavailable in {"refuse", "warn"}:
        decision.on_unavailable = env_unavailable
    rest, flag, policy = parse_flags(argv)
    if policy is not None:
        decision.on_unavailable = policy
    if flag is not None:
        decision.enabled, decision.source = flag, "cli"
    if decision.enabled:
        roots()
    return rest, decision


# ---------------------------------------------------------------- loading


class _Plugin(Protocol):
    """The guard distribution's entry point: a module with these two names."""

    GUARD: Guard

    def install(self, argv: list[str], decision: Decision, config_dir: Path) -> Guard: ...


ENTRY_POINT_TARGET = "icode_sandbox.guard"


def _load_plugin() -> _Plugin:
    """The ``icode-sandbox`` distribution's entry point; raises ``LookupError`` when it isn't there.

    A distribution names itself, so the name alone proves little: there must be
    exactly one, its entry must point where ours does, and the module that loads
    must be one of that distribution's own files.
    """
    import importlib.metadata

    offered = importlib.metadata.entry_points(group=ENTRY_POINT_GROUP)
    ours = [ep for ep in offered if ep.dist is not None and ep.dist.name.lower() == DISTRIBUTION]
    others = sorted({ep.dist.name if ep.dist is not None else "?" for ep in offered} - {DISTRIBUTION})
    if others:
        sys.stderr.write(f"{APP_DISPLAY_NAME}: ignoring guards offered by other packages: {', '.join(others)}\n")
    if not ours:
        raise LookupError(f"the {DISTRIBUTION} package is not installed")
    if len(ours) > 1:
        raise LookupError(f"{len(ours)} installed packages call themselves {DISTRIBUTION}")
    (entry,) = ours
    if entry.value != ENTRY_POINT_TARGET:
        raise LookupError(f"the {DISTRIBUTION} entry point names {entry.value!r}")
    plugin = entry.load()
    if not _belongs_to(plugin, entry.dist):
        raise LookupError(f"{ENTRY_POINT_TARGET} did not load from the {DISTRIBUTION} package's own files")
    return plugin


def _belongs_to(module: Any, dist: Any) -> bool:
    """Whether *module*'s file is one of *dist*'s files (or under its source folder, for an editable install)."""
    module_file = os.path.realpath(getattr(module, "__file__", "") or "")
    if not module_file:
        return False
    for file in dist.files or ():
        if os.path.realpath(str(dist.locate_file(file))) == module_file:
            return True
    import json
    import urllib.parse
    import urllib.request

    try:
        direct = json.loads(dist.read_text("direct_url.json") or "{}")
    except ValueError:
        return False
    if not (direct.get("dir_info") or {}).get("editable"):
        return False
    url = urllib.parse.urlparse(direct.get("url", ""))
    if url.scheme != "file":
        return False
    source = os.path.realpath(urllib.request.url2pathname(url.path))
    return module_file.startswith(source + os.sep)


def cli(argv: list[str]) -> int:
    """``icode sandbox …``: the guard's own commands, whether or not this process runs under it."""
    if _current.active:
        return _current.cli(argv)
    try:
        plugin = _load_plugin()
    except Exception as exc:
        sys.stderr.write(f"{APP_DISPLAY_NAME}: the file-access sandbox is not available: {exc}\n")
        return 2
    return plugin.GUARD.cli(argv)


ENV_INHERITED = "CHRYS_FILE_SANDBOX_INHERITED"
"""Set by the guard on the programs it starts confined (a workflow worker, an ``icode`` run in a command)."""


_process_confined = False


def note_confined() -> None:
    """Called by a guard once it has confined this process: that is for good, whichever guard is current later."""
    global _process_confined
    _process_confined = True


def confined() -> bool:
    """Whether this process runs under a guard: its own, or one it was started inside."""
    return _process_confined or _current.active or os.environ.get(ENV_INHERITED) == "1"


def installed() -> bool:
    """Whether a guard distribution is installed (its entry point is offered); nothing is loaded.

    The settings page and ``--help`` offer the switch only then: turned on
    without the package, every later start would refuse.
    """
    import importlib.metadata

    return any(
        ep.dist is not None and ep.dist.name.lower() == DISTRIBUTION
        for ep in importlib.metadata.entry_points(group=ENTRY_POINT_GROUP)
    )


def unavailable(decision: Decision, reason: str) -> GuardStatus:
    """A guard was asked for and can't be had: refuse to start, or warn and run without it.

    A working directory's own settings can ask for it but never stop iCode
    starting: from a repository, the request only warns.
    """
    st = GuardStatus(state="inactive", requested=True, reasons=[reason])
    set_current(_NoGuard(st), decision=decision)
    if decision.on_unavailable == "refuse" and decision.source != "workdir":
        sys.stderr.write(
            f"{APP_DISPLAY_NAME}: the sandbox was requested but cannot be enabled: {reason}\n"
            f"  Start with --no-sandbox, or set {ENV_ON_UNAVAILABLE}=warn to start without it.\n"
        )
        sys.stderr.flush()
        raise SystemExit(EXIT_UNAVAILABLE)
    sys.stderr.write(f"{APP_DISPLAY_NAME}: WARNING: sandbox requested but NOT active: {reason}\n")
    sys.stderr.flush()
    return st


def install_at_startup(
    argv: list[str],
    *,
    config_dir: Path | None = None,
    resolve_launch_roots: Callable[[list[str]], list[str]] | None = None,
) -> tuple[list[str], GuardStatus]:
    """Decide and, when asked for, load and start the guard; return the remaining argv.

    Called first thing in the command dispatcher, before any thread exists.
    """
    if _current.active:
        # A second entry into ``main`` in the same process: the decision was made once.
        rest, _decision = decide(argv, config_dir or _config_dir())
        return rest, _current.status()
    if config_dir is None:
        config_dir = _config_dir()
    rest, decision = decide(argv, config_dir, resolve_launch_roots=resolve_launch_roots)
    if decision.settings_problem and installed():
        sys.stderr.write(
            f"{APP_DISPLAY_NAME}: the file-access sandbox settings could not be read: {decision.settings_problem}\n"
            "  They are not used for this start; fix the file, or start with --sandbox.\n"
        )
        sys.stderr.flush()
    if not decision.enabled:
        reasons = [f"settings not read: {decision.settings_problem}"] if decision.settings_problem else []
        set_current(_NoGuard(GuardStatus(reasons=reasons)), decision=decision)
        return rest, _current.status()
    try:
        plugin = _load_plugin()
    except Exception as exc:  # not installed, or broken: never start without the guard silently
        return rest, unavailable(decision, f"{exc}")
    try:
        guard = plugin.install(rest, decision, config_dir)
    except SystemExit:
        raise
    except Exception as exc:
        return rest, unavailable(decision, f"{type(exc).__name__}: {exc}")
    set_current(guard, decision=decision)
    return rest, guard.status()


def _config_dir() -> Path:
    from chrys.foundation.platform import get_platform

    return get_platform().config_dir
