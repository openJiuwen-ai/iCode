# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Centralized configuration for Chrys."""

from __future__ import annotations

import logging
import math
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Literal

from chrys.foundation.config.coercion import (
    MISSING,
    Coerced,
    Coercer,
    CoerceReason,
    CoerceStatus,
    bool_coercer,
    choice_coercer,
    float_coercer,
    int_coercer,
    invalid,
    optional_int_coercer,
    text_coercer,
)
from chrys.foundation.config.context import EvalContext
from chrys.foundation.config.spec import (
    Apply,
    ChoiceProvider,
    InvalidPolicy,
    Kind,
    ProjectMerge,
    Risk,
    spec,
)
from chrys.foundation.config.web_values import (
    web_custom_endpoints_coercer,
    web_mode_coercer,
    web_origins_coercer,
    web_proxy_url_coercer,
)
from chrys.foundation.i18n import msg
from chrys.foundation.i18n.locale import SUPPORTED_LOCALES

logger = logging.getLogger(__name__)

DEFAULT_THEME = "chrys"
"""Fallback theme name when ``CHRYS_THEME`` is empty, missing, or invalid."""

RENAMED_THEMES: Final[dict[str, str]] = {"chrys-dark": "chrys"}
"""Built-in theme names that are gone, and the name each of them goes by now.

The ``ui.theme`` coercer follows this at every layer, so a retired name in an
environment variable or a project file still selects the theme it meant. A
retired name saved in the user document is also rewritten there, by
:func:`chrys.foundation.config.migrations.migrate_renamed_theme`. The old names
stay reserved: a user theme taking one would be renamed away on the next start.
"""

DEFAULT_LOCALE = "system"
"""Fallback locale selector when ``CHRYS_LOCALE`` is empty or missing."""

DEFAULT_APPROVAL_MODE = "manual"
"""Fallback approval mode when ``CHRYS_DEFAULT_APPROVAL_MODE`` is empty, missing, or invalid.

Valid values are ``manual``, ``auto``, ``bypass``.  The persistence helper
(:func:`persist_approval_mode`) deliberately downgrades ``bypass`` to ``auto``
so that Chrys never launches in BYPASS mode — that mode is opt-in per launch:
once chosen it stays in force for every session until the app exits.
"""

DEFAULT_AGENT_PROFILE = "Code"
"""Fallback TUI launch agent profile when ``CHRYS_DEFAULT_AGENT`` is empty or unusable."""

DEFAULT_EDITOR_KEYMAP = "standard"
"""Fallback message-editor keymap when its persisted value is missing or invalid."""

SESSION_ROOT_DIR_ENV_VAR: Final[str] = "CHRYS_SESSION_ROOT_DIR"
"""Base directory for session storage. The actual session store lives under ``sessions/`` within it."""

_VALID_APPROVAL_MODES = ("manual", "auto", "bypass")
_VALID_EDITOR_KEYMAPS = ("standard", "emacs", "vim")

_TRUTHY_ENV = {"1", "true", "yes", "on"}

_RESOLVED_SESSIONS_DIR_CACHE: dict[str, Path] = {}


def _load_default_approval_mode() -> str:
    """Read ``CHRYS_DEFAULT_APPROVAL_MODE`` from env, fall back on invalid/missing."""
    raw = os.environ.get("CHRYS_DEFAULT_APPROVAL_MODE", "").strip().lower()
    return raw if raw in _VALID_APPROVAL_MODES else DEFAULT_APPROVAL_MODE


def _load_default_agent() -> str:
    """Read ``CHRYS_DEFAULT_AGENT`` from env, falling back to the built-in Code profile."""
    return os.environ.get("CHRYS_DEFAULT_AGENT", "").strip() or DEFAULT_AGENT_PROFILE


def _load_editor_keymap() -> str:
    """Read and normalize ``CHRYS_EDITOR_KEYMAP`` with a Standard fallback."""
    raw = os.environ.get("CHRYS_EDITOR_KEYMAP", "").strip().lower()
    return raw if raw in _VALID_EDITOR_KEYMAPS else DEFAULT_EDITOR_KEYMAP


def parse_bool_env_value(raw: str | None, *, default: bool = False) -> bool:
    """Parse one environment-style boolean with Chrys's shared semantics."""
    normalized = (raw or "").strip().lower()
    if not normalized:
        return default
    return normalized in _TRUTHY_ENV


def _load_bool_env(name: str, *, default: bool = False) -> bool:
    """Parse ``name`` as a boolean (1/true/yes/on, case-insensitive)."""
    return parse_bool_env_value(os.environ.get(name), default=default)


DEFAULT_ASK_USER_TIMEOUT_SECONDS = 900
"""Default ``ask_user`` reply wait (15 minutes) when ``CHRYS_ASK_USER_TIMEOUT_SECONDS`` is unset."""

DEFAULT_MAX_TRANSIENT_RETRIES = 10
"""Default application-layer transient retry budget for interactive frontends."""

HEADLESS_DEFAULT_MAX_TRANSIENT_RETRIES = 18
"""Default application-layer transient retry budget for ``chrys run``."""

MAX_TRANSIENT_RETRIES_LIMIT = 50
"""Upper bound for ``CHRYS_MAX_TRANSIENT_RETRIES``."""


@dataclass(frozen=True, slots=True)
class MaxTransientRetriesWarning:
    """Structured warning emitted while parsing the transient retry budget."""

    variant: Literal["invalid", "clamped"]
    raw: str
    value: int | None
    limit: int


DEFAULT_TOOL_RESULT_CEILING_TOKENS = 64_000
"""Default kernel backstop for one local tool result."""

TOOL_RESULT_CEILING_FLOOR = 2_000
"""Minimum positive ``CHRYS_TOOL_RESULT_CEILING_TOKENS`` value."""

DEFAULT_WORKSPACE_MRU_MAX_ENTRIES = 20
"""Default number of recently used workspace directories kept in the MRU index."""

WORKSPACE_MRU_MAX_ENTRIES_LIMIT = 100
"""Upper clamp for ``CHRYS_WORKSPACE_MRU_MAX_ENTRIES`` so quick-access lists stay bounded."""


def _load_workspace_mru_max_entries() -> int:
    """Read ``CHRYS_WORKSPACE_MRU_MAX_ENTRIES``; garbage → default, non-positive → 0 (disabled)."""
    raw = os.environ.get("CHRYS_WORKSPACE_MRU_MAX_ENTRIES", "").strip()
    if not raw:
        return DEFAULT_WORKSPACE_MRU_MAX_ENTRIES
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_WORKSPACE_MRU_MAX_ENTRIES
    if value <= 0:
        return 0
    return min(value, WORKSPACE_MRU_MAX_ENTRIES_LIMIT)


DEFAULT_WORKSPACE_CHANGE_NOTICE_MAX_ENTRIES = 50
WORKSPACE_CHANGE_NOTICE_MAX_ENTRIES_LIMIT = 100

DEFAULT_TRAJECTORY_VERIFY_COMMANDS = (
    "pytest,ruff,ty,mypy,pyright,flake8,pylint,tox,nox,unittest,"
    "npm test,npm run test,pnpm test,yarn test,bun test,deno test,node --test,"
    "vitest,jest,mocha,playwright test,cypress run,eslint,biome check,tsc,"
    "cargo test,cargo clippy,cargo check,cargo nextest,"
    "go test,go vet,golangci-lint,staticcheck,"
    "mvn test,mvn verify,gradle test,gradlew test,gradle check,gradlew check,"
    "rspec,rake test,rubocop,phpunit,phpstan,dotnet test,mix test,swift test,"
    "ctest,shellcheck,make"
)
"""Comma-separated command words and phrases classified as verification work."""


def _load_workspace_change_notice_max_entries() -> int:
    """Read and clamp the workspace-change notice path limit to ``1..100``."""
    raw = os.environ.get("CHRYS_WORKSPACE_CHANGE_NOTICE_MAX_ENTRIES", "").strip()
    if not raw:
        return DEFAULT_WORKSPACE_CHANGE_NOTICE_MAX_ENTRIES
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_WORKSPACE_CHANGE_NOTICE_MAX_ENTRIES
    return min(max(value, 1), WORKSPACE_CHANGE_NOTICE_MAX_ENTRIES_LIMIT)


DEFAULT_MUTATION_SNAPSHOT_MAX_FILE_MB = 50
"""Default per-file cap (in MiB) on mutation backup blobs stored under the session dir."""


def _load_mutation_snapshot_max_file_mb() -> int:
    """Read ``CHRYS_MUTATION_SNAPSHOT_MAX_FILE_MB``; garbage → default, non-positive → 0 (no cap)."""
    raw = os.environ.get("CHRYS_MUTATION_SNAPSHOT_MAX_FILE_MB", "").strip()
    if not raw:
        return DEFAULT_MUTATION_SNAPSHOT_MAX_FILE_MB
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_MUTATION_SNAPSHOT_MAX_FILE_MB
    return max(0, value)


def _load_ask_user_timeout() -> int | None:
    """Read ``CHRYS_ASK_USER_TIMEOUT_SECONDS``; empty → default, non-positive → no timeout."""
    raw = os.environ.get("CHRYS_ASK_USER_TIMEOUT_SECONDS", "").strip()
    if not raw:
        return DEFAULT_ASK_USER_TIMEOUT_SECONDS
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_ASK_USER_TIMEOUT_SECONDS
    return value if value > 0 else None


def parse_max_transient_retries_structured(
    raw: str | None,
) -> tuple[int | None, MaxTransientRetriesWarning | None]:
    """Parse ``CHRYS_MAX_TRANSIENT_RETRIES`` and retain warning arguments."""
    normalized = (raw or "").strip()
    if not normalized:
        return None, None
    try:
        value = int(normalized)
    except ValueError:
        return None, MaxTransientRetriesWarning(
            variant="invalid",
            raw=str(raw),
            value=None,
            limit=MAX_TRANSIENT_RETRIES_LIMIT,
        )
    if value < 0:
        return None, MaxTransientRetriesWarning(
            variant="invalid",
            raw=str(raw),
            value=None,
            limit=MAX_TRANSIENT_RETRIES_LIMIT,
        )
    if value > MAX_TRANSIENT_RETRIES_LIMIT:
        return MAX_TRANSIENT_RETRIES_LIMIT, MaxTransientRetriesWarning(
            variant="clamped",
            raw=str(raw),
            value=value,
            limit=MAX_TRANSIENT_RETRIES_LIMIT,
        )
    return value, None


def parse_max_transient_retries(raw: str | None) -> tuple[int | None, str | None]:
    """Parse ``CHRYS_MAX_TRANSIENT_RETRIES`` without raising."""
    value, warning = parse_max_transient_retries_structured(raw)
    if warning is None:
        return value, None
    if warning.variant == "invalid":
        message = (
            f"Ignoring invalid CHRYS_MAX_TRANSIENT_RETRIES={raw!r}; "
            "expected a non-negative integer and will use the frontend default."
        )
    else:
        message = (
            f"CHRYS_MAX_TRANSIENT_RETRIES={warning.value} exceeds the limit of {warning.limit}; "
            f"clamping to {warning.limit}."
        )
    return value, message


def _load_max_transient_retries() -> int | None:
    """Read the transient retry budget from the environment without raising."""
    value, warning = parse_max_transient_retries(os.environ.get("CHRYS_MAX_TRANSIENT_RETRIES"))
    if warning is not None:
        logger.warning(warning)
    return value


def parse_tool_result_ceiling_tokens(raw: str | None) -> tuple[int, str | None]:
    """Parse ``CHRYS_TOOL_RESULT_CEILING_TOKENS`` without raising."""
    normalized = (raw or "").strip()
    if not normalized:
        return DEFAULT_TOOL_RESULT_CEILING_TOKENS, None
    try:
        value = int(normalized)
    except ValueError:
        return DEFAULT_TOOL_RESULT_CEILING_TOKENS, (
            f"Ignoring invalid CHRYS_TOOL_RESULT_CEILING_TOKENS={raw!r}; "
            f"using the default of {DEFAULT_TOOL_RESULT_CEILING_TOKENS}."
        )
    if value == 0:
        return 0, None
    if value < 0:
        return DEFAULT_TOOL_RESULT_CEILING_TOKENS, (
            f"Ignoring invalid CHRYS_TOOL_RESULT_CEILING_TOKENS={raw!r}; "
            f"using the default of {DEFAULT_TOOL_RESULT_CEILING_TOKENS}."
        )
    if value < TOOL_RESULT_CEILING_FLOOR:
        return TOOL_RESULT_CEILING_FLOOR, (
            f"CHRYS_TOOL_RESULT_CEILING_TOKENS={value} is below the minimum of "
            f"{TOOL_RESULT_CEILING_FLOOR}; clamping to {TOOL_RESULT_CEILING_FLOOR}."
        )
    return value, None


def _load_tool_result_ceiling_tokens() -> int:
    value, warning = parse_tool_result_ceiling_tokens(os.environ.get("CHRYS_TOOL_RESULT_CEILING_TOKENS"))
    if warning is not None:
        logger.warning(warning)
    return value


def default_session_root_dir(config_dir: str | Path | None = None) -> Path:
    """Return the default root under which session storage lives."""
    if config_dir is None:
        from chrys.foundation.platform import get_platform

        config_dir = get_platform().config_dir
    return Path(config_dir)


def resolve_session_root_dir(config_dir: str | Path | None = None, *, create: bool = True) -> Path:
    """Return the active base root for session storage.

    Reads the process snapshot rather than the environment: the twelve callers
    that resolve a sessions directory hold no ``Settings`` between them, and
    threading one through all of them would only move the problem — the value
    is ``Apply.RESTART``, so a single per-process answer is the honest one.
    """
    # Local import: ``process_settings`` falls back to a fresh ``load_settings``
    # when bootstrap has not installed a snapshot, which needs this module.
    from chrys.foundation.config.process_settings import process_settings

    fallback = default_session_root_dir(config_dir)
    raw = process_settings().session_root_dir.strip()
    if raw:
        candidate = _session_root_candidate(raw)
        if candidate is not None and _session_root_dir_is_valid(candidate, create=create):
            return candidate
        logger.warning("Ignoring invalid %s=%r; using %s", SESSION_ROOT_DIR_ENV_VAR, raw, fallback)

    if create:
        fallback.mkdir(parents=True, exist_ok=True)
    return fallback


def resolve_sessions_dir(config_dir: str | Path | None = None, *, create: bool = True) -> Path:
    """Return the active directory containing saved session folders."""
    root = resolve_session_root_dir(config_dir, create=create)
    sessions_dir = root / "sessions"
    if create:
        cache_key = str(sessions_dir)
        cached = _RESOLVED_SESSIONS_DIR_CACHE.get(cache_key)
        if cached is not None:
            return cached
    if _session_storage_dir_is_valid(sessions_dir, create=create):
        if create:
            _RESOLVED_SESSIONS_DIR_CACHE[str(sessions_dir)] = sessions_dir
        return sessions_dir

    fallback = default_session_root_dir(config_dir) / "sessions"
    if sessions_dir != fallback:
        logger.warning("Ignoring invalid session storage directory %s; using %s", sessions_dir, fallback)
    if _session_storage_dir_is_valid(fallback, create=create) and create:
        _RESOLVED_SESSIONS_DIR_CACHE[str(fallback)] = fallback
    return fallback


def session_root_is_ruled_out(raw: str) -> bool:
    """Whether *raw* can be rejected as a session root without creating it.

    Answers only what can be settled by looking: that the system takes the path
    at all, and that nothing already sitting on the way down to
    ``<root>/sessions`` rules out a directory ever being there. A root that gets
    past this is one the settings may go on naming.

    It deliberately does not predict whether the write will be *permitted* —
    whether a directory that exists will accept a new one, whether one that does
    not exist can be created. Those cannot be answered without performing the
    operation being predicted, on the real names, which is the creation this is
    supposed to avoid; every approximation of it (a file probe standing in for a
    ``mkdir``, one level standing in for the descent, a scratch name standing in
    for the real one) is wrong in both directions for some real permission
    scheme. So permission is left to the moment it is exercised, where the
    answer is observed rather than guessed: :func:`resolve_sessions_dir` falls
    back to the default location and logs that it did. The cost of that choice
    is bounded and known — until first use the settings can name a root that
    turns out to be unwritable — and it is the only part of this that a
    check running here cannot get right.
    """
    candidate = _session_root_candidate(raw)
    if candidate is None:
        return True
    # The root is only ever used through this child, and the walk to the child
    # passes through the root, so asking about the child asks about both.
    sessions = candidate / "sessions"
    start = _deepest_existing_directory(sessions)
    if start is None:
        return True
    return any(_name_is_refused(start, part) for part in sessions.relative_to(start).parts)


def _name_is_refused(parent: Path, name: str) -> bool:
    """Whether *name* is one this filesystem will not take as a child of *parent*.

    Asking about the whole path only gets as far as the first component that is
    not there: resolution stops at that one with ``ENOENT``, so a name below it
    that no filesystem would accept is never looked at, and the ``mkdir`` that
    creates the level above then fails on it. Each of them therefore gets asked
    about separately, hung off the deepest directory that does exist.

    Which is the right place to ask, not an approximation of it: nothing can be
    mounted under a directory that is not there, so every level still to be
    created lands in that one's filesystem and is held to its limits. And it
    stays a question — ``lstat`` on a name that is merely free answers ``ENOENT``
    and creates nothing. Some FUSE filesystems also answer ``ENOENT`` for a
    name they refuse as too long, so that ambiguous answer is checked against
    the filesystem's advertised name limit.
    """
    try:
        (parent / name).lstat()
    except FileNotFoundError:
        if not hasattr(os, "statvfs"):
            return False
        try:
            name_max = os.statvfs(parent).f_namemax
        except OSError, ValueError:
            return False
        return name_max > 0 and len(os.fsencode(name)) > name_max
    except OSError, ValueError:
        return True
    return False


def _deepest_existing_directory(path: Path) -> Path | None:
    """The directory ``mkdir(parents=True)`` would build *path* down from.

    ``None`` when there is none, which is a rejection: everything below it the
    ``mkdir`` creates, and it creates none of it if what it finds is not a
    directory. A dangling link two levels up leaves the root reading as absent
    while the entry holding its name is very much present.

    ``lstat`` rather than ``exists`` or ``lexists``, both of which answer two
    questions with one ``False``: "nothing is there" and "the system would not
    even take the question". Only the first keeps the walk going. The second
    covers a component too long for the filesystem, an embedded NUL, a path
    Windows spells illegally, a symlink loop, a parent that is a regular file —
    every one of which reads as free to create, and then fails the ``mkdir``,
    leaving storage on the default while the settings go on naming this root.
    Asking the system beats keeping a list of what it rejects, which would grow
    a case per platform and still be a case behind.

    ``exists`` misreports one more: it follows the link, so a dangling one reads
    as absent while its entry still occupies the name. ``lstat`` does not follow
    the last component and ``is_dir`` does, which is what keeps a link resolving
    to a real directory usable while a dangling one is not.
    """
    for existing in (path, *path.parents):
        try:
            existing.lstat()
        except FileNotFoundError:
            continue
        except OSError, ValueError:
            return None
        return existing if existing.is_dir() else None
    # Nothing on the way up is there — not even the anchor the walk ends at.
    # POSIX bottoms out at ``/``, which is always there, but a Windows path ends
    # at its drive or share, and an unmapped ``Z:\`` is as missing as its
    # children.
    return None


def _session_root_candidate(raw: str) -> Path | None:
    """Parse a session root env value, returning ``None`` on unusable path syntax."""
    try:
        return Path(raw).expanduser()
    except OSError, RuntimeError, ValueError:
        return None


def _session_root_dir_is_valid(path: Path, *, create: bool) -> bool:
    """Return whether *path* can be used as a session storage root."""
    try:
        if path.exists():
            return path.is_dir()
        if not create:
            return True
        path.mkdir(parents=True, exist_ok=True)
        return path.is_dir()
    except OSError, ValueError:
        return False


DEFAULT_ROLLBACK_SNAPSHOTS_KEEP = 20
"""Per-turn ``session.json`` snapshots retained for ``/rollback``."""

DEFAULT_FILE_SNAPSHOT_INLINE_CHARS = 128 * 1024
"""Combined before/after snapshot bytes chat tool records keep inline."""

DEFAULT_MUTATION_TRACE_MODE = "auto"
MUTATION_TRACE_MODES = ("auto", "off")
"""``auto`` probes for fsatrace once per process; ``off`` skips tracing."""

_TRACE_MODE_OFF_SPELLINGS = frozenset({"off", "0", "false", "no"})


def _trace_mode_env_coercer() -> Coercer:
    """Coerce ``CHRYS_MUTATION_TRACE``, preserving its historical spellings.

    The consumer only ever asked "is it off", so anything that is not one of
    the off spellings has always meant ``auto`` — including typos. Keep that
    for the variable rather than start rejecting values a working setup may
    rely on. The dotted key is a new surface with no such history, so it gets
    the ordinary closed-choice grammar instead of inheriting this one.
    """

    def coerce(raw: object) -> Coerced:
        if raw is None:
            return MISSING
        if not isinstance(raw, str):
            return invalid(raw, CoerceReason.EXPECTED_TEXT)
        if not raw.strip():
            return MISSING
        off = raw.strip().lower() in _TRACE_MODE_OFF_SPELLINGS
        return Coerced(status=CoerceStatus.VALID, value="off" if off else DEFAULT_MUTATION_TRACE_MODE)

    return coerce


def _theme_coercer() -> Coercer:
    """Coerce a theme name, following a retired built-in name to the one it goes by now.

    Whether the name is a theme at all is still the app's call — themes are
    registered in a tier this one cannot import. A rename is different: it is
    a fact about the value, the same whichever layer wrote it.
    """
    text = text_coercer()

    def coerce(raw: object) -> Coerced:
        outcome = text(raw)
        if outcome.status is not CoerceStatus.VALID:
            return outcome
        return Coerced(status=CoerceStatus.VALID, value=RENAMED_THEMES.get(outcome.value, outcome.value))

    return coerce


def _history_disable_coercer() -> Coercer:
    """Coerce ``CHRYS_HISTORY_DISABLE`` into the positive key it feeds.

    Exactly ``"1"`` disables history; every other value, including ``"0"`` and
    the empty string, leaves it on. Narrower than the general boolean grammar
    on purpose — that is what the variable has always meant.
    """

    def coerce(raw: object) -> Coerced:
        if raw is None:
            return MISSING
        if not isinstance(raw, str):
            return invalid(raw, CoerceReason.EXPECTED_TEXT)
        return Coerced(status=CoerceStatus.VALID, value=raw != "1")

    return coerce


def _semantic_max_transient_retries(settings: Settings, ctx: EvalContext) -> int:
    """Comparable retry budget: ``None`` means "whatever this frontend uses".

    The stored value cannot be compared directly — a project value of 10 is a
    tightening against headless (15) and a loosening against the TUI (7), and
    ``None`` is neither until the frontend is known.
    """
    if settings.max_transient_retries is not None:
        return settings.max_transient_retries
    return ctx.frontend_default_max_transient_retries


def _semantic_tool_result_ceiling(settings: Settings, _ctx: EvalContext) -> float:
    """Comparable ceiling: ``0`` disables the backstop, so it is the largest."""
    ceiling = settings.tool_result_ceiling_tokens
    return math.inf if ceiling <= 0 else float(ceiling)


def _session_storage_dir_is_valid(path: Path, *, create: bool) -> bool:
    """Return whether the concrete sessions directory is usable."""
    try:
        if path.exists():
            if not path.is_dir():
                return False
        elif not create:
            return True
        else:
            path.mkdir(parents=True, exist_ok=True)
        if create:
            with tempfile.TemporaryFile(dir=path):
                pass
        return True
    except OSError, ValueError:
        return False


def probe_session_root(raw: str) -> Path | None:
    """Try the sessions directory a session-root value would resolve to.

    Where :func:`session_root_is_ruled_out` only looks, this creates: it makes
    ``<root>/sessions`` (or the default location's, for a blank *raw*) and
    checks that it takes a write, exactly as :func:`resolve_sessions_dir` will
    on first use. The returned path is for validation and display only — the
    persisted value stays the string, and ``None`` means the value would send
    storage to the fallback.

    A blank value is probed at the default location directly rather than
    through the ruled-out check, whose empty-string candidate is the current
    directory: a file named ``sessions`` sitting there would otherwise reject
    the one value that never touches it.
    """
    # The persisted value is the trimmed string, so that is what gets probed.
    raw = raw.strip()
    if not raw:
        default_sessions = default_session_root_dir() / "sessions"
        return default_sessions if _session_storage_dir_is_valid(default_sessions, create=True) else None
    if session_root_is_ruled_out(raw):
        return None
    candidate = _session_root_candidate(raw)
    if candidate is None:
        return None
    sessions = candidate / "sessions"
    return sessions if _session_storage_dir_is_valid(sessions, create=True) else None


# ── Panel labels ─────────────────────────────────────────────────────
# One per persisted field, keyed ``settings.<dotted key>.label``. Declared at
# module level so extraction sees them; ``SettingSpec`` refuses a persisted
# field without one.

_LABEL_MODEL_PROFILE_ACTIVE = msg("settings.model.profile.active.label", fallback="Active model profile")
_LABEL_AGENT_DEFAULT_PROFILE = msg("settings.agent.default_profile.label", fallback="Default agent")
_LABEL_MODEL_ROLE_APPROVAL_JUDGE = msg("settings.model.role.approval_judge.label", fallback="Approval judge model")
_LABEL_MODEL_ROLE_SESSION_TITLE = msg("settings.model.role.session_title.label", fallback="Session title model")
_LABEL_SESSION_TITLE_AUTO = msg("settings.session.title.auto.label", fallback="Auto-generate session titles")
_LABEL_CONTEXT_WARN_THRESHOLD_PCT = msg(
    "settings.context.warn_threshold_pct.label", fallback="Context warning threshold"
)
_LABEL_TRAJECTORY_VERIFY_COMMANDS = msg(
    "settings.trajectory.verify_commands.label", fallback="Trajectory verification commands"
)
_LABEL_LLM_RETRY_MAX_TRANSIENT = msg("settings.llm.retry.max_transient.label", fallback="Max transient retries")
_LABEL_TOOLS_RESULT_CEILING_TOKENS = msg(
    "settings.tools.result.ceiling_tokens.label", fallback="Tool result ceiling (tokens)"
)
_LABEL_SEARCH_RESPECT_GITIGNORE = msg("settings.tools.search.respect_gitignore.label", fallback="Respect .gitignore")
_LABEL_UI_THEME = msg("settings.ui.theme.label", fallback="Theme")
_LABEL_UI_LOCALE = msg("settings.ui.locale.label", fallback="UI Language")
_LABEL_UI_CHAT_TOOL_GROUPS_EXPANDED = msg(
    "settings.ui.chat.tool_groups_expanded.label",
    fallback="Expand tool details by default",
)
_LABEL_UI_EDITOR_KEYMAP = msg("settings.ui.editor.keymap.label", fallback="Editor keymap")
_LABEL_WORKSPACE_MRU_MAX_ENTRIES = msg("settings.workspace.mru_max_entries.label", fallback="Recent workspaces to keep")
_LABEL_APPROVAL_DEFAULT_MODE = msg("settings.approval.default_mode.label", fallback="Default approval mode")
_LABEL_DAA_MINIMAL = msg(
    "settings.approval.daa_minimal.label", fallback="Enable experimental exact/prefix approval reuse"
)
_LABEL_APP_DEV_MODE = msg("settings.app.dev_mode.label", fallback="Developer mode")
_LABEL_MUTATIONS_PARALLEL_IMPLICIT_TOOLS = msg(
    "settings.mutations.parallel_implicit_tools.label", fallback="Parallel implicit tools"
)
_LABEL_MUTATIONS_COORDINATION_ENABLED = msg(
    "settings.mutations.coordination.enabled.label", fallback="Mutation coordination"
)
_LABEL_MUTATIONS_SNAPSHOT_MAX_FILE_MB = msg(
    "settings.mutations.snapshot.max_file_mb.label", fallback="Snapshot max file size (MB)"
)
_LABEL_MUTATIONS_SNAPSHOT_SKIP_BINARY = msg(
    "settings.mutations.snapshot.skip_binary.label", fallback="Skip binary files in snapshots"
)
_LABEL_WORKSPACE_CHANGE_NOTICE_ENABLED = msg(
    "settings.workspace.change_notice.enabled.label", fallback="Workspace change notices"
)
_LABEL_WORKSPACE_CHANGE_NOTICE_MAX_ENTRIES = msg(
    "settings.workspace.change_notice.max_entries.label", fallback="Workspace change notice entries"
)
_LABEL_ROLLBACK_SNAPSHOTS_KEEP = msg("settings.rollback.snapshots_keep.label", fallback="Rollback snapshots to keep")
_LABEL_OTEL_ENABLED = msg("settings.otel.enabled.label", fallback="OpenTelemetry export")
_LABEL_OTEL_ENDPOINT = msg("settings.otel.endpoint.label", fallback="Telemetry endpoint")
_LABEL_OTEL_SENSITIVE_DATA = msg("settings.otel.sensitive_data.label", fallback="Include sensitive data in telemetry")
_LABEL_TOOLS_ASK_USER_TIMEOUT_SECONDS = msg(
    "settings.tools.ask_user.timeout_seconds.label", fallback="Ask-user timeout (seconds)"
)
_LABEL_TOOLS_ASK_USER_INLINE = msg("settings.tools.ask_user.inline.label", fallback="Show questions in the chat")
_LABEL_STORAGE_SESSION_ROOT_DIR = msg("settings.storage.session_root_dir.label", fallback="Session storage root")
_LABEL_HISTORY_PROMPT_ENABLED = msg("settings.history.prompt.enabled.label", fallback="Save prompt history")
_LABEL_UI_CHAT_FILE_SNAPSHOT_INLINE_CHARS = msg(
    "settings.ui.chat.file_snapshot_inline_chars.label", fallback="Inline file snapshot limit (chars)"
)
_LABEL_MODEL_ROLE_BUDDY_MODEL_ID = msg("settings.model.role.buddy_model_id.label", fallback="Buddy model")
_LABEL_LOG_RAW_HTTP_CAPTURE = msg("settings.log.raw_http_capture.label", fallback="Capture raw HTTP traffic")
_LABEL_MUTATIONS_TRACE_MODE = msg("settings.mutations.trace.mode.label", fallback="Mutation trace mode")
_LABEL_MUTATIONS_TRACE_FSATRACE_PATH = msg("settings.mutations.trace.fsatrace_path.label", fallback="fsatrace path")
_LABEL_PROJECT_CONFIG_ENABLED = msg("settings.project.config_enabled.label", fallback="Load project settings")
_LABEL_PROJECT_HOOKS_ENABLED = msg("settings.project.hooks_enabled.label", fallback="Load project hooks")
_LABEL_NOTIFICATIONS_ENABLED = msg("settings.notifications.enabled.label", fallback="Enable notifications")
_LABEL_NOTIFICATIONS_DELIVERY_DESKTOP = msg("settings.notifications.delivery.desktop.label", fallback="Desktop popup")
_LABEL_NOTIFICATIONS_DELIVERY_SOUND = msg("settings.notifications.delivery.sound.label", fallback="Sound")
_LABEL_NOTIFICATIONS_SUPPRESS_WHEN_FOCUSED = msg(
    "settings.notifications.suppress_when_focused.label", fallback="Suppress while focused"
)
_LABEL_NOTIFICATIONS_EVENTS_APPROVAL_REQUIRED = msg(
    "settings.notifications.events.approval_required.label", fallback="Approval required"
)
_LABEL_NOTIFICATIONS_EVENTS_ASK_USER = msg("settings.notifications.events.ask_user.label", fallback="Question asked")
_LABEL_NOTIFICATIONS_EVENTS_TURN_COMPLETE = msg(
    "settings.notifications.events.turn_complete.label", fallback="Turn complete"
)
_LABEL_NOTIFICATIONS_EVENTS_TURN_ERROR = msg("settings.notifications.events.turn_error.label", fallback="Turn error")


_WEB_LABEL_WEB_SEARCH_MODE = msg("settings.tools.web_search.mode.label", fallback="Web search mode")
_WEB_LABEL_WEB_SEARCH_NUM_RESULTS = msg(
    "settings.tools.web_search.num_results.label", fallback="Results per web search"
)
_WEB_LABEL_WEB_SEARCH_TIMEOUT_SECONDS = msg(
    "settings.tools.web_search.timeout_seconds.label", fallback="Web search timeout (seconds)"
)
_WEB_LABEL_WEB_FETCH_MODE = msg("settings.tools.web_fetch.mode.label", fallback="Web fetch mode")
_WEB_LABEL_WEB_FETCH_MAX_TOKENS = msg("settings.tools.web_fetch.max_tokens.label", fallback="Web fetch token budget")
_WEB_LABEL_WEB_FETCH_TIMEOUT_SECONDS = msg(
    "settings.tools.web_fetch.timeout_seconds.label", fallback="Web fetch timeout (seconds)"
)
_WEB_LABEL_WEB_EGRESS_PROXY_URL = msg("settings.tools.web_egress.proxy_url.label", fallback="Web proxy URL")
_WEB_LABEL_WEB_EGRESS_PROXY_DNS = msg("settings.tools.web_egress.proxy_dns.label", fallback="Web proxy DNS")
_WEB_LABEL_WEB_SEARCH_CUSTOM_ENDPOINTS = msg(
    "settings.tools.web_search.custom_endpoints.label", fallback="Custom search endpoints"
)
_WEB_LABEL_WEB_SEARCH_PRIVATE_ORIGINS = msg(
    "settings.tools.web_search.private_origins.label", fallback="Web search private-network origins"
)
_WEB_LABEL_WEB_SEARCH_HTTP_ORIGINS = msg(
    "settings.tools.web_search.http_origins.label", fallback="Web search plain-HTTP origins"
)
_WEB_LABEL_WEB_FETCH_PRIVATE_ORIGINS = msg(
    "settings.tools.web_fetch.private_origins.label", fallback="Web fetch private-network origins"
)
_WEB_LABEL_WEB_FETCH_HTTP_ORIGINS = msg(
    "settings.tools.web_fetch.http_origins.label", fallback="Web fetch plain-HTTP origins"
)
_WEB_LABEL_WEB_FETCH_ALLOWED_ORIGINS = msg(
    "settings.tools.web_fetch.allowed_origins.label", fallback="Web fetch allowed origins"
)
_WEB_LABEL_WEB_FETCH_DENIED_ORIGINS = msg(
    "settings.tools.web_fetch.denied_origins.label", fallback="Web fetch denied origins"
)


@dataclass(frozen=True)
class Settings:
    """Application settings.

    Field defaults are plain literals: every source (built-in default, user
    YAML, project layer, dotenv, real process env, CLI, session pin) is merged
    by :func:`chrys.foundation.config.settings_store.load_settings`, which is
    the only thing that reads the environment. A bare ``Settings()`` is
    therefore a pure value with no hidden process dependency.

    Each field carries its own :class:`SettingSpec` in ``field(metadata=...)``:
    the dotted key, the coercer both YAML and env values go through, when a
    change takes effect, and whether a repository may set it.

    **Frozen on purpose.** A value only means something alongside the layer it
    came from and whether that layer was sealed, and those three live in
    :class:`~chrys.foundation.config.settings_store.LoadedSettings`. Writing a
    field in place changes one of the three and silently leaves the other two
    describing the value it replaced — which is how a key sealed at its
    built-in default keeps its seal while holding a value nobody defaulted to.
    Go through ``LoadedSettings.overlay()`` and install the result; use
    ``dataclasses.replace`` only where no provenance exists to keep (tests
    building a fixture, a caller composing a fresh value).
    """

    # ── Active model profile pointers ─────────────────────────────
    # Active model profile selector for the main agent (id or unique name).
    # Looked up against ``ModelProfileRegistry`` by ``resolve_active_profile``.
    model_profile: str = field(
        default="",
        metadata=spec(
            key="model.profile.active",
            label=_LABEL_MODEL_PROFILE_ACTIVE,
            env="CHRYS_MODEL_PROFILE",
            coerce=text_coercer(),
            apply=Apply.RELOAD,
            group="model",
            kind=Kind.TEXT,
        ),
    )

    # Explicit per-session model profile selector override (id or unique name).
    # This is not loaded from env; frontends set it when the user provides an
    # out-of-band model selection that must win over agent-level
    # ``model.profile_id`` bindings.
    model_profile_override: str = field(
        default="",
        metadata=spec(
            key="model.profile.override",
            coerce=text_coercer(),
            apply=Apply.RELOAD,
            group="model",
            kind=Kind.TEXT,
            persist=False,
        ),
    )
    model_profile_override_sub_agents: bool = field(
        default=False,
        metadata=spec(
            key="model.profile.override_sub_agents",
            coerce=bool_coercer(),
            apply=Apply.RELOAD,
            group="model",
            kind=Kind.BOOL,
            persist=False,
        ),
    )

    # Agent profile used by the TUI for fresh sessions.  Invalid/missing
    # profile names are resolved by the TUI after the agent registry is loaded.
    default_agent: str = field(
        default=DEFAULT_AGENT_PROFILE,
        metadata=spec(
            key="agent.default_profile",
            label=_LABEL_AGENT_DEFAULT_PROFILE,
            env="CHRYS_DEFAULT_AGENT",
            coerce=text_coercer(),
            apply=Apply.RELOAD,
            group="agent",
            kind=Kind.TEXT,
            # A profile carries its own approval policy, which ``builder.py``
            # adopts wholesale — a repository must not get to pick it.
            project_merge=ProjectMerge.DENY,
            risk=Risk.CAUTION,
        ),
    )

    # Approval judge model profile ID.  Empty = use the main agent's
    # active profile.  Looked up by ``resolve_judge_profile``.
    approval_judge_model_profile: str = field(
        default="",
        metadata=spec(
            key="model.role.approval_judge",
            label=_LABEL_MODEL_ROLE_APPROVAL_JUDGE,
            env="CHRYS_MODEL_PROFILE_APPROVAL_JUDGE",
            coerce=text_coercer(),
            apply=Apply.RELOAD,
            group="model",
            kind=Kind.TEXT,
        ),
    )

    # ── Session titles ────────────────────────────────────────────
    # Session-title summarizer model profile selector (id or unique name).
    # Empty = use the main agent's active profile.  Looked up by
    # ``resolve_session_title_profile``.
    session_title_model_profile: str = field(
        default="",
        metadata=spec(
            key="model.role.session_title",
            label=_LABEL_MODEL_ROLE_SESSION_TITLE,
            env="CHRYS_MODEL_PROFILE_SESSION_TITLE",
            coerce=text_coercer(),
            apply=Apply.RELOAD,
            group="model",
            kind=Kind.TEXT,
        ),
    )

    # Whether each successful turn schedules an async LLM call that
    # summarizes the conversation into a short session title (skipped
    # forever once the user sets a custom title).  Set
    # ``CHRYS_SESSION_TITLE_AUTO=0`` to disable the extra LLM traffic.
    session_title_auto: bool = field(
        default=True,
        metadata=spec(
            key="session.title.auto",
            label=_LABEL_SESSION_TITLE_AUTO,
            env="CHRYS_SESSION_TITLE_AUTO",
            coerce=bool_coercer(),
            apply=Apply.RELOAD,
            group="session",
            kind=Kind.BOOL,
            # A repository may switch the extra LLM traffic off, never back on.
            project_merge=ProjectMerge.DISABLE_ONLY,
        ),
    )

    # ── Compaction / context-display thresholds ───────────────────
    # Threshold (fraction of context window) at which the TUI starts
    # warning the user about high context usage.  App-level because it
    # tunes UI behaviour, not the LLM call itself.
    warn_threshold_pct: float = field(
        default=0.50,
        metadata=spec(
            key="context.warn_threshold_pct",
            label=_LABEL_CONTEXT_WARN_THRESHOLD_PCT,
            coerce=float_coercer(minimum=0.0, maximum=1.0),
            apply=Apply.RELOAD,
            group="context",
            kind=Kind.FLOAT,
            project_merge=ProjectMerge.FREE,
        ),
    )

    # Comma-separated rather than a list so Settings remains an all-scalar
    # value object and project-layer FREE replacement has unambiguous
    # override semantics.
    trajectory_verify_commands: str = field(
        default=DEFAULT_TRAJECTORY_VERIFY_COMMANDS,
        metadata=spec(
            key="trajectory.verify_commands",
            label=_LABEL_TRAJECTORY_VERIFY_COMMANDS,
            coerce=text_coercer(),
            apply=Apply.LIVE,
            group="trajectory",
            kind=Kind.TEXT,
            project_merge=ProjectMerge.FREE,
        ),
    )

    # ── Application-layer transient retries ──────────────────────
    # The environment-derived override stays separate from the frontend
    # policy default so a settings reload can re-read the former while
    # retaining the launch mode's default.
    max_transient_retries: int | None = field(
        default=None,
        metadata=spec(
            key="llm.retry.max_transient",
            label=_LABEL_LLM_RETRY_MAX_TRANSIENT,
            env="CHRYS_MAX_TRANSIENT_RETRIES",
            coerce=int_coercer(reject_negative=True, maximum=MAX_TRANSIENT_RETRIES_LIMIT),
            apply=Apply.RELOAD,
            group="llm",
            kind=Kind.OPTIONAL_INT,
            # Raising it multiplies cost and wait against the backoff (up to
            # 600 s a retry), so a repository may only lower it.
            project_merge=ProjectMerge.TIGHTEN_ONLY,
            semantic_value=_semantic_max_transient_retries,
        ),
    )
    frontend_default_max_transient_retries: int = field(
        default=DEFAULT_MAX_TRANSIENT_RETRIES,
        metadata=spec(
            key="llm.retry.frontend_default",
            coerce=int_coercer(minimum=0),
            apply=Apply.RESTART,
            group="llm",
            kind=Kind.INT,
            persist=False,
        ),
    )

    # Static sanity bound for every result on the local tool-invocation path.
    # Zero disables the backstop; producer-specific caps remain independent.
    tool_result_ceiling_tokens: int = field(
        default=DEFAULT_TOOL_RESULT_CEILING_TOKENS,
        metadata=spec(
            key="tools.result.ceiling_tokens",
            label=_LABEL_TOOLS_RESULT_CEILING_TOKENS,
            env="CHRYS_TOOL_RESULT_CEILING_TOKENS",
            coerce=int_coercer(zero=0, reject_negative=True, minimum=TOOL_RESULT_CEILING_FLOOR),
            apply=Apply.RELOAD,
            group="tools",
            kind=Kind.INT,
            # ``0`` means "no ceiling", so it is the loosest value, not the
            # strictest — see the comparator. That also makes falling through
            # to a lower layer a posture reversal rather than a preference
            # question: a rejected value must not uncap the backstop.
            invalid_policy=InvalidPolicy.SAFE_DEFAULT,
            project_merge=ProjectMerge.TIGHTEN_ONLY,
            semantic_value=_semantic_tool_result_ceiling,
        ),
    )

    search_respect_gitignore: bool = field(
        default=True,
        metadata=spec(
            key="tools.search.respect_gitignore",
            label=_LABEL_SEARCH_RESPECT_GITIGNORE,
            coerce=bool_coercer(),
            apply=Apply.RELOAD,
            group="tools",
            kind=Kind.BOOL,
        ),
    )

    # ── TUI theme ─────────────────────────────────────────────────
    # Persisted theme name.  Validated against registered themes at app
    # startup; invalid/unknown values fall back to :data:`DEFAULT_THEME`.
    theme: str = field(
        default=DEFAULT_THEME,
        metadata=spec(
            key="ui.theme",
            label=_LABEL_UI_THEME,
            env="CHRYS_THEME",
            # Themes are registered in the app layer, which foundation cannot
            # import, so validation stays where it is today: at app startup.
            coerce=_theme_coercer(),
            apply=Apply.LIVE,
            group="ui",
            kind=Kind.ENUM,
            choices=ChoiceProvider.THEMES,
        ),
    )

    # ── Display locale ───────────────────────────────────────────────
    # Persisted locale selector. Resolution and fallback are owned by
    # ``Localizer``; Settings only strips surrounding whitespace.
    locale: str = field(
        default=DEFAULT_LOCALE,
        metadata=spec(
            key="ui.locale",
            label=_LABEL_UI_LOCALE,
            env="CHRYS_LOCALE",
            # Deliberately not a ``choice_coercer``: ``normalize_locale``
            # accepts far more spellings than the picker offers (``zh_CN``,
            # ``en_US.UTF-8``, ``C``, …) and maps them itself. ``choices``
            # here describes the picker, not the accepted set.
            coerce=text_coercer(),
            apply=Apply.LIVE,
            group="ui",
            kind=Kind.ENUM,
            choices=(DEFAULT_LOCALE, *SUPPORTED_LOCALES),
        ),
    )

    # ── Message editor ────────────────────────────────────────────
    # Persisted keymap selected in the message editor.
    editor_keymap: str = field(
        default=DEFAULT_EDITOR_KEYMAP,
        metadata=spec(
            key="ui.editor.keymap",
            label=_LABEL_UI_EDITOR_KEYMAP,
            env="CHRYS_EDITOR_KEYMAP",
            coerce=choice_coercer(choices=_VALID_EDITOR_KEYMAPS),
            apply=Apply.LIVE,
            group="ui",
            kind=Kind.ENUM,
            choices=_VALID_EDITOR_KEYMAPS,
        ),
    )

    # ── Workspace MRU ─────────────────────────────────────────────
    # Maximum entries kept in the persisted workspace MRU index that feeds
    # quick-access surfaces such as the Change Directory picker's recent
    # paths.  ``0`` disables the MRU entirely (no index file is created,
    # loaded, or updated).  Workspace-level UX config, not bound to any one
    # consumer; read once at TUI startup (no live reload).
    workspace_mru_max_entries: int = field(
        default=DEFAULT_WORKSPACE_MRU_MAX_ENTRIES,
        metadata=spec(
            key="workspace.mru_max_entries",
            label=_LABEL_WORKSPACE_MRU_MAX_ENTRIES,
            env="CHRYS_WORKSPACE_MRU_MAX_ENTRIES",
            coerce=int_coercer(non_positive=0, maximum=WORKSPACE_MRU_MAX_ENTRIES_LIMIT),
            apply=Apply.RESTART,
            group="workspace",
            kind=Kind.INT,
        ),
    )

    # ── Approval ──────────────────────────────────────────────────
    # Persisted default approval mode applied at engine startup.  One of
    # ``manual`` / ``auto`` / ``bypass``.  Invalid or missing values fall
    # back to :data:`DEFAULT_APPROVAL_MODE`.  :func:`persist_approval_mode`
    # maps ``bypass`` → ``auto`` on write so Chrys never boots directly
    # into BYPASS mode.
    default_approval_mode: str = field(
        default=DEFAULT_APPROVAL_MODE,
        metadata=spec(
            key="approval.default_mode",
            label=_LABEL_APPROVAL_DEFAULT_MODE,
            env="CHRYS_DEFAULT_APPROVAL_MODE",
            coerce=choice_coercer(choices=_VALID_APPROVAL_MODES),
            apply=Apply.RELOAD,
            group="approval",
            kind=Kind.ENUM,
            choices=_VALID_APPROVAL_MODES,
            risk=Risk.DANGEROUS,
            # Falling through could land on a persisted ``bypass``.
            invalid_policy=InvalidPolicy.SAFE_DEFAULT,
        ),
    )

    daa_minimal: bool = field(
        default=False,
        metadata=spec(
            key="approval.daa_minimal",
            label=_LABEL_DAA_MINIMAL,
            env="CHRYS_DAA_MINIMAL",
            coerce=bool_coercer(),
            apply=Apply.RELOAD,
            group="approval",
            kind=Kind.BOOL,
        ),
    )

    # ── Developer mode ─────────────────────────────────────────────
    # Enables developer-only behaviors (debugging hooks, review surfaces, etc).
    dev_mode: bool = field(
        default=False,
        metadata=spec(
            key="app.dev_mode",
            label=_LABEL_APP_DEV_MODE,
            env="CHRYS_DEV_MODE",
            coerce=bool_coercer(),
            # Its one consumer takes it from ``Settings`` during the rebuild a
            # reload performs, so a reload genuinely applies it — RESTART would
            # promise a stability this process never had.
            apply=Apply.RELOAD,
            group="app",
            kind=Kind.BOOL,
        ),
    )

    # ── Mutation tracking ──────────────────────────────────────────
    # Whether shell-like tools that can mutate files implicitly may run
    # concurrently inside one session.  True preserves the historical
    # parallel behaviour.  Set ``CHRYS_PARALLEL_IMPLICIT_TOOLS=0`` to
    # serialize shell/run_skill_script mutation-observation windows for
    # stronger in-session /diff attribution.
    parallel_implicit_tools: bool = field(
        default=True,
        metadata=spec(
            key="mutations.parallel_implicit_tools",
            label=_LABEL_MUTATIONS_PARALLEL_IMPLICIT_TOOLS,
            env="CHRYS_PARALLEL_IMPLICIT_TOOLS",
            coerce=bool_coercer(),
            apply=Apply.RELOAD,
            group="mutations",
            kind=Kind.BOOL,
            # Setting it false buys stronger /diff attribution; a repository
            # must not undo a user who chose that.
            project_merge=ProjectMerge.DISABLE_ONLY,
        ),
    )

    # Cross-session mutation coordination: sessions sharing a working
    # tree publish proven writes to a registry and reclassify their
    # window inferences against peers' claims.  Escape hatch only — the feature is inert (one
    # listdir) when no peer sessions exist.  Set
    # ``CHRYS_MUTATION_COORDINATION=0`` to disable.
    mutation_coordination: bool = field(
        default=True,
        metadata=spec(
            key="mutations.coordination.enabled",
            label=_LABEL_MUTATIONS_COORDINATION_ENABLED,
            env="CHRYS_MUTATION_COORDINATION",
            coerce=bool_coercer(),
            apply=Apply.RELOAD,
            group="mutations",
            kind=Kind.BOOL,
            # Turning it off loses cross-session attribution.
            project_merge=ProjectMerge.ENABLE_ONLY,
        ),
    )

    # Per-file cap (MiB) on mutation backup blobs written under
    # ``{session_dir}/mutations/``.  Files larger than this are still
    # *recorded* as mutated, but their content is not backed up — no
    # diff rendering and no rollback restore for them.  ``0`` (or any
    # non-positive env value) disables the size cap.  Consumed via
    # ``SnapshotPolicy.from_settings``.
    mutation_snapshot_max_file_mb: int = field(
        default=DEFAULT_MUTATION_SNAPSHOT_MAX_FILE_MB,
        metadata=spec(
            key="mutations.snapshot.max_file_mb",
            label=_LABEL_MUTATIONS_SNAPSHOT_MAX_FILE_MB,
            env="CHRYS_MUTATION_SNAPSHOT_MAX_FILE_MB",
            coerce=int_coercer(non_positive=0),
            # ``SnapshotPolicy`` is captured when ``SnapshotStore`` is first
            # built and never rebuilt on reload, so RELOAD would be a promise
            # nothing keeps.
            apply=Apply.RESTART,
            group="mutations",
            kind=Kind.INT,
        ),
    )

    # Whether binary (non-text) file contents are excluded from mutation
    # backups, regardless of size.  Detection uses
    # ``EncodingDetector.looks_binary`` — the same heuristic diff
    # surfaces use for rendering.  Set
    # ``CHRYS_MUTATION_SNAPSHOT_SKIP_BINARY=0`` to back up binary files.
    mutation_snapshot_skip_binary: bool = field(
        default=True,
        metadata=spec(
            key="mutations.snapshot.skip_binary",
            label=_LABEL_MUTATIONS_SNAPSHOT_SKIP_BINARY,
            env="CHRYS_MUTATION_SNAPSHOT_SKIP_BINARY",
            coerce=bool_coercer(),
            apply=Apply.RESTART,  # Same captured policy as max_file_mb.
            group="mutations",
            kind=Kind.BOOL,
        ),
    )

    # One-shot reminders describing agent and external workspace changes at
    # real turn boundaries.
    workspace_change_notice: bool = field(
        default=True,
        metadata=spec(
            key="workspace.change_notice.enabled",
            label=_LABEL_WORKSPACE_CHANGE_NOTICE_ENABLED,
            env="CHRYS_WORKSPACE_CHANGE_NOTICE",
            coerce=bool_coercer(),
            apply=Apply.RELOAD,
            group="workspace",
            kind=Kind.BOOL,
            # A repository must not hide file changes from the user.
            project_merge=ProjectMerge.ENABLE_ONLY,
        ),
    )
    workspace_change_notice_max_entries: int = field(
        default=DEFAULT_WORKSPACE_CHANGE_NOTICE_MAX_ENTRIES,
        metadata=spec(
            key="workspace.change_notice.max_entries",
            label=_LABEL_WORKSPACE_CHANGE_NOTICE_MAX_ENTRIES,
            env="CHRYS_WORKSPACE_CHANGE_NOTICE_MAX_ENTRIES",
            coerce=int_coercer(minimum=1, maximum=WORKSPACE_CHANGE_NOTICE_MAX_ENTRIES_LIMIT),
            apply=Apply.RELOAD,
            group="workspace",
            kind=Kind.INT,
            # Already clamped to 1..100, so there is no room to hide in.
            project_merge=ProjectMerge.FREE,
        ),
    )

    # ── Rollback ──────────────────────────────────────────────────
    # How many per-turn ``session.json`` snapshots to retain under
    # ``{session_dir}/snapshots/``.  Each snapshot is a full copy of
    # ``session.json`` taken just before a new user turn begins, enabling
    # ``/rollback`` to restore the session (and optionally file-system
    # changes) to any of the last N turn boundaries.  Older snapshots
    # are pruned on every new turn.
    rollback_snapshots_keep: int = field(
        default=DEFAULT_ROLLBACK_SNAPSHOTS_KEEP,
        metadata=spec(
            key="rollback.snapshots_keep",
            label=_LABEL_ROLLBACK_SNAPSHOTS_KEEP,
            env="CHRYS_ROLLBACK_SNAPSHOTS_KEEP",
            # Behaviour change: a non-numeric value used to raise ``ValueError``
            # out of ``Settings()`` and take the process down at startup. It is
            # now rejected like any other invalid value.
            coerce=int_coercer(minimum=1),
            apply=Apply.RELOAD,
            group="rollback",
            kind=Kind.INT,
        ),
    )

    # ── Observability ─────────────────────────────────────────────
    # When True, :func:`chrys.foundation.observability.setup.setup_otel` builds the OTel
    # providers/exporters and opens TELEMETRY_GATE. Kernel chat, agent and
    # function spans and the MCP spans from service/mcp/owned.py share this gate.
    #
    # Exporter destination priority:
    #   1. If ``OTEL_EXPORTER_OTLP_ENDPOINT`` (or a signal-specific
    #      variant) is set, or ``otel_endpoint`` below is non-blank, Chrys
    #      builds OTLP exporters from the OTel-spec'd env vars
    #      through _get_exporters_from_env; the setting supplies a fallback
    #      base endpoint.
    #   2. Otherwise, Chrys installs a local JSONL file exporter that
    #      writes to ``{session_dir}/otel/{traces,logs}.jsonl`` once
    #      a session is live.  Zero-config default; safe in the TUI
    #      (no stdout contention).
    #
    # ``otel_sensitive_data`` (``CHRYS_OTEL_SENSITIVE_DATA``) feeds TELEMETRY_GATE.
    # When enabled, prompt, response and tool-argument content is captured;
    # SessionJsonlLogExporter writes message events alongside span traces.
    otel_enabled: bool = field(
        default=False,
        metadata=spec(
            key="otel.enabled",
            label=_LABEL_OTEL_ENABLED,
            env="CHRYS_OTEL",
            coerce=bool_coercer(),
            apply=Apply.RESTART,  # Providers are installed once per process.
            group="otel",
            kind=Kind.BOOL,
        ),
    )
    otel_sensitive_data: bool = field(
        default=False,
        metadata=spec(
            key="otel.sensitive_data",
            label=_LABEL_OTEL_SENSITIVE_DATA,
            env="CHRYS_OTEL_SENSITIVE_DATA",
            coerce=bool_coercer(),
            apply=Apply.RESTART,
            group="otel",
            kind=Kind.BOOL,
            # Full prompt / response / tool-arg content is exported.
            risk=Risk.DANGEROUS,
            invalid_policy=InvalidPolicy.SAFE_DEFAULT,
        ),
    )
    # Base OTLP endpoint for the exporters when the standard
    # ``OTEL_EXPORTER_OTLP_ENDPOINT`` is not set; the standard variables keep
    # winning so an OpenTelemetry-configured environment behaves as documented.
    # Blank keeps the session-folder JSONL fallback.
    otel_endpoint: str = field(
        default="",
        metadata=spec(
            key="otel.endpoint",
            label=_LABEL_OTEL_ENDPOINT,
            env="CHRYS_OTEL_ENDPOINT",
            coerce=text_coercer(),
            apply=Apply.RESTART,  # Read once by setup_otel with the other otel keys.
            group="otel",
            kind=Kind.TEXT,
        ),
    )

    # ── Ask-user ──────────────────────────────────────────────────
    # Seconds to wait for a user reply to an ``ask_user`` tool call before
    # the middleware gives up and reports a timeout to the model.  Read from
    # ``CHRYS_ASK_USER_TIMEOUT_SECONDS`` (non-positive/empty → no timeout via
    # the loader).  Per-session: each engine carries its own ``Settings``, so
    # frontends pick their own policy — the TUI/CLI keep the bounded default,
    # while ``chrys acp`` defaults to ``None`` so the ACP client owns timing.
    ask_user_timeout_seconds: int | None = field(
        default=DEFAULT_ASK_USER_TIMEOUT_SECONDS,
        metadata=spec(
            key="tools.ask_user.timeout_seconds",
            label=_LABEL_TOOLS_ASK_USER_TIMEOUT_SECONDS,
            env="CHRYS_ASK_USER_TIMEOUT_SECONDS",
            coerce=optional_int_coercer(),
            apply=Apply.RELOAD,
            group="tools",
            kind=Kind.OPTIONAL_INT,
        ),
    )
    # Where an ``ask_user`` question first appears in the TUI: inside the
    # tool card in the transcript (True) or as a modal dialog (False, from
    # which "Answer Inline" still hands the question over to the transcript).
    ask_user_inline: bool = field(
        default=False,
        metadata=spec(
            key="tools.ask_user.inline",
            label=_LABEL_TOOLS_ASK_USER_INLINE,
            env="CHRYS_ASK_USER_INLINE",
            coerce=bool_coercer(),
            apply=Apply.RELOAD,
            group="tools",
            kind=Kind.BOOL,
        ),
    )

    # ── Storage ───────────────────────────────────────────────────
    # Base directory under which session storage lives; the session store
    # itself is ``<root>/sessions``.  Empty selects the platform config dir.
    # Resolution and validation stay in ``resolve_session_root_dir``.
    session_root_dir: str = field(
        default="",
        metadata=spec(
            key="storage.session_root_dir",
            label=_LABEL_STORAGE_SESSION_ROOT_DIR,
            env=SESSION_ROOT_DIR_ENV_VAR,
            coerce=text_coercer(),
            apply=Apply.RESTART,
            group="storage",
            kind=Kind.PATH,
            # Where a user's sessions live is not a repository's business.
            project_merge=ProjectMerge.DENY,
            risk=Risk.CAUTION,
        ),
    )

    # ── Prompt history ────────────────────────────────────────────
    # Whether the TUI keeps a persistent prompt history.  The key is
    # positive; the legacy variable is negative, hence the separate alias
    # coercer.
    prompt_history_enabled: bool = field(
        default=True,
        metadata=spec(
            key="history.prompt.enabled",
            label=_LABEL_HISTORY_PROMPT_ENABLED,
            env="CHRYS_HISTORY_DISABLE",
            coerce=bool_coercer(),
            env_coerce=_history_disable_coercer(),
            # The consumer is a module-level function on the TUI side with no
            # engine in reach, and ``ChrysApp._settings`` is never refreshed on
            # reload, so RELOAD would be a promise nothing keeps. This is not a
            # regression: today the variable can only be set before launch.
            apply=Apply.RESTART,
            group="history",
            kind=Kind.BOOL,
        ),
    )

    # ── Chat rendering ────────────────────────────────────────────
    tool_groups_expanded: bool = field(
        default=False,
        metadata=spec(
            key="ui.chat.tool_groups_expanded",
            label=_LABEL_UI_CHAT_TOOL_GROUPS_EXPANDED,
            coerce=bool_coercer(),
            apply=Apply.LIVE,
            group="ui",
            kind=Kind.BOOL,
            project_merge=ProjectMerge.DENY,
        ),
    )

    # Maximum combined before/after snapshot bytes chat tool records keep
    # inline.  ``0`` keeps nothing inline.
    chat_file_snapshot_inline_chars: int = field(
        default=DEFAULT_FILE_SNAPSHOT_INLINE_CHARS,
        metadata=spec(
            key="ui.chat.file_snapshot_inline_chars",
            label=_LABEL_UI_CHAT_FILE_SNAPSHOT_INLINE_CHARS,
            env="CHRYS_TUI_FILE_SNAPSHOT_INLINE_CHARS",
            coerce=int_coercer(non_positive=0),
            apply=Apply.RESTART,  # Read once, from the process snapshot.
            group="ui",
            kind=Kind.INT,
        ),
    )

    # ── Buddy ─────────────────────────────────────────────────────
    # Model id used for what the buddy says when petted.  Empty = the active
    # profile's model.  Sits under ``model.role`` with the other per-role
    # selectors, but holds a *model id* rather than a profile selector —
    # hence the ``_model_id`` leaf, which is the only thing telling a reader
    # of the YAML that a profile name will not work here.
    buddy_model: str = field(
        default="",
        metadata=spec(
            key="model.role.buddy_model_id",
            label=_LABEL_MODEL_ROLE_BUDDY_MODEL_ID,
            env="CHRYS_PET_MODEL",
            coerce=text_coercer(),
            apply=Apply.RELOAD,
            group="model",
            kind=Kind.TEXT,
        ),
    )

    # ── Diagnostics ───────────────────────────────────────────────
    # Full LLM HTTP exchange capture, written under the session dir.
    raw_http_capture: bool = field(
        default=False,
        metadata=spec(
            key="log.raw_http_capture",
            label=_LABEL_LOG_RAW_HTTP_CAPTURE,
            env="CHRYS_DEBUG_LLM_RAW_HTTP_LOG",
            coerce=bool_coercer(),
            apply=Apply.RESTART,
            group="log",
            kind=Kind.BOOL,
            # Captured exchanges contain API keys and full message content.
            risk=Risk.DANGEROUS,
            invalid_policy=InvalidPolicy.SAFE_DEFAULT,
        ),
    )

    # ── Mutation tracing ──────────────────────────────────────────
    # ``auto`` probes for fsatrace once per process; ``off`` skips tracing.
    mutation_trace_mode: str = field(
        default=DEFAULT_MUTATION_TRACE_MODE,
        metadata=spec(
            key="mutations.trace.mode",
            label=_LABEL_MUTATIONS_TRACE_MODE,
            env="CHRYS_MUTATION_TRACE",
            coerce=choice_coercer(choices=MUTATION_TRACE_MODES),
            env_coerce=_trace_mode_env_coercer(),
            apply=Apply.RESTART,  # The probe result is cached process-wide.
            group="mutations",
            kind=Kind.ENUM,
            choices=MUTATION_TRACE_MODES,
        ),
    )

    # Explicit fsatrace binary, overriding the ``PATH`` lookup.
    mutation_trace_fsatrace_path: str = field(
        default="",
        metadata=spec(
            key="mutations.trace.fsatrace_path",
            label=_LABEL_MUTATIONS_TRACE_FSATRACE_PATH,
            env="CHRYS_FSATRACE_PATH",
            coerce=text_coercer(),
            apply=Apply.RESTART,
            group="mutations",
            kind=Kind.PATH,
            # This value becomes a subprocess argv[0]. A cloned repository
            # naming its own binary here would have Chrys execute it.
            project_merge=ProjectMerge.DENY,
            risk=Risk.DANGEROUS,
            invalid_policy=InvalidPolicy.SAFE_DEFAULT,
        ),
    )

    # ── Project layer ─────────────────────────────────────────────
    # Master switch for the project trust domain: whether
    # ``<root>/.chrys/settings.yaml`` is consulted at all.  Off by
    # default — cloning a repository must not hand it configuration
    # authority.
    project_config_enabled: bool = field(
        default=False,
        metadata=spec(
            key="project.config_enabled",
            label=_LABEL_PROJECT_CONFIG_ENABLED,
            coerce=bool_coercer(),
            apply=Apply.RELOAD,
            group="project",
            kind=Kind.BOOL,
            # Read only from DEFAULT/USER: letting a project enable the
            # project layer would be the layer authorising itself.
            project_merge=ProjectMerge.DENY,
            risk=Risk.CAUTION,
        ),
    )

    # Whether ``<root>/.chrys/hooks/hooks.{yaml,yml,json}`` is loaded.  On
    # by default: project hooks are the workspace's own automation and each
    # hook still runs under the usual approval and outbox rules.
    project_hooks_enabled: bool = field(
        default=True,
        metadata=spec(
            key="project.hooks_enabled",
            label=_LABEL_PROJECT_HOOKS_ENABLED,
            coerce=bool_coercer(),
            apply=Apply.RELOAD,
            group="project",
            kind=Kind.BOOL,
            # A project must not be able to switch its own hooks back on.
            project_merge=ProjectMerge.DENY,
            risk=Risk.CAUTION,
        ),
    )

    # ── Desktop notifications ─────────────────────────────────────
    # Flattened from the former ``notifications.yaml`` document.  The TUI
    # consumes these through the ``NotificationSettings`` view object
    # (``app/tui/notifications``); delivery behaviour stays there.
    notifications_enabled: bool = field(
        default=True,
        metadata=spec(
            key="notifications.enabled",
            label=_LABEL_NOTIFICATIONS_ENABLED,
            coerce=bool_coercer(),
            apply=Apply.LIVE,
            group="notifications",
            kind=Kind.BOOL,
        ),
    )

    notifications_desktop: bool = field(
        default=True,
        metadata=spec(
            key="notifications.delivery.desktop",
            label=_LABEL_NOTIFICATIONS_DELIVERY_DESKTOP,
            coerce=bool_coercer(),
            apply=Apply.LIVE,
            group="notifications",
            kind=Kind.BOOL,
        ),
    )

    notifications_sound: bool = field(
        default=True,
        metadata=spec(
            key="notifications.delivery.sound",
            label=_LABEL_NOTIFICATIONS_DELIVERY_SOUND,
            coerce=bool_coercer(),
            apply=Apply.LIVE,
            group="notifications",
            kind=Kind.BOOL,
        ),
    )

    notifications_suppress_when_focused: bool = field(
        default=True,
        metadata=spec(
            key="notifications.suppress_when_focused",
            label=_LABEL_NOTIFICATIONS_SUPPRESS_WHEN_FOCUSED,
            coerce=bool_coercer(),
            apply=Apply.LIVE,
            group="notifications",
            kind=Kind.BOOL,
        ),
    )

    notifications_event_approval_required: bool = field(
        default=True,
        metadata=spec(
            key="notifications.events.approval_required",
            label=_LABEL_NOTIFICATIONS_EVENTS_APPROVAL_REQUIRED,
            coerce=bool_coercer(),
            apply=Apply.LIVE,
            group="notifications",
            kind=Kind.BOOL,
        ),
    )

    notifications_event_ask_user: bool = field(
        default=True,
        metadata=spec(
            key="notifications.events.ask_user",
            label=_LABEL_NOTIFICATIONS_EVENTS_ASK_USER,
            coerce=bool_coercer(),
            apply=Apply.LIVE,
            group="notifications",
            kind=Kind.BOOL,
        ),
    )

    notifications_event_turn_complete: bool = field(
        default=True,
        metadata=spec(
            key="notifications.events.turn_complete",
            label=_LABEL_NOTIFICATIONS_EVENTS_TURN_COMPLETE,
            coerce=bool_coercer(),
            apply=Apply.LIVE,
            group="notifications",
            kind=Kind.BOOL,
        ),
    )

    notifications_event_turn_error: bool = field(
        default=True,
        metadata=spec(
            key="notifications.events.turn_error",
            label=_LABEL_NOTIFICATIONS_EVENTS_TURN_ERROR,
            coerce=bool_coercer(),
            apply=Apply.LIVE,
            group="notifications",
            kind=Kind.BOOL,
        ),
    )

    web_search_mode: str = field(
        default="auto",
        metadata=spec(
            key="tools.web_search.mode",
            label=_WEB_LABEL_WEB_SEARCH_MODE,
            # "provider" needs a profile-level provider map, so it cannot build from
            # global settings and the panel must not offer it.
            coerce=web_mode_coercer(on="auto"),
            apply=Apply.RELOAD,
            group="tools",
            kind=Kind.ENUM,
            project_merge=ProjectMerge.DENY,
            choices=("off", "auto"),
        ),
    )

    web_search_num_results: int = field(
        default=8,
        metadata=spec(
            key="tools.web_search.num_results",
            label=_WEB_LABEL_WEB_SEARCH_NUM_RESULTS,
            coerce=int_coercer(minimum=1, maximum=20),
            apply=Apply.RELOAD,
            group="tools",
            kind=Kind.INT,
            project_merge=ProjectMerge.DENY,
        ),
    )

    web_search_timeout_seconds: int = field(
        default=30,
        metadata=spec(
            key="tools.web_search.timeout_seconds",
            label=_WEB_LABEL_WEB_SEARCH_TIMEOUT_SECONDS,
            coerce=int_coercer(minimum=1, maximum=120),
            apply=Apply.RELOAD,
            group="tools",
            kind=Kind.INT,
            project_merge=ProjectMerge.DENY,
        ),
    )

    web_fetch_mode: str = field(
        default="on",
        metadata=spec(
            key="tools.web_fetch.mode",
            label=_WEB_LABEL_WEB_FETCH_MODE,
            coerce=web_mode_coercer(on="on"),
            apply=Apply.RELOAD,
            group="tools",
            kind=Kind.ENUM,
            project_merge=ProjectMerge.DENY,
            choices=("off", "on"),
        ),
    )

    web_fetch_max_tokens: int = field(
        default=16000,
        metadata=spec(
            key="tools.web_fetch.max_tokens",
            label=_WEB_LABEL_WEB_FETCH_MAX_TOKENS,
            coerce=int_coercer(minimum=1, maximum=64000),
            apply=Apply.RELOAD,
            group="tools",
            kind=Kind.INT,
            project_merge=ProjectMerge.DENY,
        ),
    )

    web_fetch_timeout_seconds: int = field(
        default=60,
        metadata=spec(
            key="tools.web_fetch.timeout_seconds",
            label=_WEB_LABEL_WEB_FETCH_TIMEOUT_SECONDS,
            coerce=int_coercer(minimum=1, maximum=120),
            apply=Apply.RELOAD,
            group="tools",
            kind=Kind.INT,
            project_merge=ProjectMerge.DENY,
        ),
    )

    web_egress_proxy_url: str = field(
        default="",
        metadata=spec(
            key="tools.web_egress.proxy_url",
            label=_WEB_LABEL_WEB_EGRESS_PROXY_URL,
            coerce=web_proxy_url_coercer,
            apply=Apply.RELOAD,
            group="tools",
            kind=Kind.TEXT,
            project_merge=ProjectMerge.DENY,
        ),
    )

    web_egress_proxy_dns: str = field(
        default="local",
        metadata=spec(
            key="tools.web_egress.proxy_dns",
            label=_WEB_LABEL_WEB_EGRESS_PROXY_DNS,
            # remote hands the final-address check to the proxy, so only the user may choose it.
            coerce=choice_coercer(choices=("local", "remote")),
            apply=Apply.RELOAD,
            group="tools",
            kind=Kind.ENUM,
            project_merge=ProjectMerge.DENY,
            choices=("local", "remote"),
        ),
    )

    web_search_custom_endpoints: str = field(
        default="[]",
        metadata=spec(
            key="tools.web_search.custom_endpoints",
            label=_WEB_LABEL_WEB_SEARCH_CUSTOM_ENDPOINTS,
            coerce=web_custom_endpoints_coercer,
            apply=Apply.RELOAD,
            group="tools",
            kind=Kind.TEXT,
            project_merge=ProjectMerge.DENY,
        ),
    )

    web_search_private_origins: str = field(
        default="[]",
        metadata=spec(
            key="tools.web_search.private_origins",
            label=_WEB_LABEL_WEB_SEARCH_PRIVATE_ORIGINS,
            coerce=web_origins_coercer,
            apply=Apply.RELOAD,
            group="tools",
            kind=Kind.TEXT,
            project_merge=ProjectMerge.DENY,
        ),
    )

    web_search_http_origins: str = field(
        default="[]",
        metadata=spec(
            key="tools.web_search.http_origins",
            label=_WEB_LABEL_WEB_SEARCH_HTTP_ORIGINS,
            coerce=web_origins_coercer,
            apply=Apply.RELOAD,
            group="tools",
            kind=Kind.TEXT,
            project_merge=ProjectMerge.DENY,
        ),
    )

    web_fetch_private_origins: str = field(
        default="[]",
        metadata=spec(
            key="tools.web_fetch.private_origins",
            label=_WEB_LABEL_WEB_FETCH_PRIVATE_ORIGINS,
            coerce=web_origins_coercer,
            apply=Apply.RELOAD,
            group="tools",
            kind=Kind.TEXT,
            project_merge=ProjectMerge.DENY,
        ),
    )

    web_fetch_http_origins: str = field(
        default="[]",
        metadata=spec(
            key="tools.web_fetch.http_origins",
            label=_WEB_LABEL_WEB_FETCH_HTTP_ORIGINS,
            coerce=web_origins_coercer,
            apply=Apply.RELOAD,
            group="tools",
            kind=Kind.TEXT,
            project_merge=ProjectMerge.DENY,
        ),
    )

    web_fetch_allowed_origins: str = field(
        default="[]",
        metadata=spec(
            key="tools.web_fetch.allowed_origins",
            label=_WEB_LABEL_WEB_FETCH_ALLOWED_ORIGINS,
            coerce=web_origins_coercer,
            apply=Apply.RELOAD,
            group="tools",
            kind=Kind.TEXT,
            project_merge=ProjectMerge.DENY,
        ),
    )

    web_fetch_denied_origins: str = field(
        default="[]",
        metadata=spec(
            key="tools.web_fetch.denied_origins",
            label=_WEB_LABEL_WEB_FETCH_DENIED_ORIGINS,
            coerce=web_origins_coercer,
            apply=Apply.RELOAD,
            group="tools",
            kind=Kind.TEXT,
            project_merge=ProjectMerge.DENY,
        ),
    )

    @classmethod
    def from_env(cls, **overrides: Any) -> Settings:
        """Create settings from every source, dropping provenance and warnings.

        Compatibility shim. Production paths must call ``load_settings`` and
        keep all three parts of its result — a caller that throws provenance
        away cannot tell the user why the value they typed has no effect, and
        one that throws warnings away silently swallows rejected values.

        ``overrides`` are pins, so each one must already be the field's
        canonical value: they outrank every layer and never meet a coercer, and
        a ``TypeError`` here is better than a value that is stored as one thing
        and shown to the panel as another. ``ask_user_timeout_seconds=0`` is
        rejected in favour of ``None``, for instance, rather than quietly
        becoming a zero-second timeout.
        """
        from chrys.foundation.config.settings_store import load_settings

        return load_settings(**overrides).settings

    def effective_max_transient_retries(self) -> int:
        """Return the environment override or this frontend's policy default."""
        if self.max_transient_retries is not None:
            return self.max_transient_retries
        return self.frontend_default_max_transient_retries


def _persist_one(key: str, value: Any, *, log_value: bool = True) -> None:
    """Write one dotted key to the user settings document, without raising.

    The thin shared body of the ``persist_*`` helpers: failures are logged and
    swallowed so a disk/permission issue doesn't crash the TUI, and a value the
    store refuses is logged rather than written. No ``os.environ`` mirror —
    the mirror would turn the panel's own write into an ``ENV``-layer value,
    and provenance would then call it "overridden, editing has no effect".
    """
    from chrys.foundation.config.settings_store import persist

    try:
        result = persist({key: value})
    except Exception:
        if log_value:
            logger.warning("Failed to persist %s=%r", key, value, exc_info=True)
        else:
            logger.warning("Failed to persist %s", key, exc_info=True)
        return
    if not result.ok:
        if log_value:
            logger.warning("Refusing to persist %s: %r", key, value)
        else:
            logger.warning("Refusing to persist %s: value rejected", key)


def persist_theme(theme: str) -> None:
    """Write ``ui.theme`` to the user settings document so it survives restart."""
    _persist_one("ui.theme", theme)


def persist_locale(locale: str) -> None:
    """Write ``ui.locale`` to the user settings document without raising."""
    # Content-free logging by design: the requested value must not reach the log.
    _persist_one("ui.locale", locale, log_value=False)


def persist_tool_groups_expanded(value: bool) -> None:
    """Persist whether newly created live tool groups start expanded."""
    _persist_one("ui.chat.tool_groups_expanded", value)


def persist_approval_mode(mode: str) -> None:
    """Write ``approval.default_mode`` to the user settings document.

    ``bypass`` never persists — :func:`chrys.foundation.config.settings_store.persist`
    downgrades it to ``auto`` so the *next* app launch cannot start in BYPASS
    mode; that mode must always be an explicit opt-in within a launch. Unknown
    values are ignored (logged, not written).
    """
    _persist_one("approval.default_mode", mode)


def persist_editor_keymap(mode: str) -> None:
    """Persist the selected message-editor keymap."""
    _persist_one("ui.editor.keymap", mode)
