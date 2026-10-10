# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Environment source separation: the frozen process snapshot and dotenv layers.

Before this module, ``load_dotenv(override=True)`` folded both dotenv files
into ``os.environ`` and the origin of every value was lost — provenance could
not be trusted, and "the real shell environment wins" could not be enforced.
The separation rests on three rules:

* Only the **real process environment** is frozen, once, before bootstrap
  injects anything. Dotenv files are re-read on every load so external edits
  take effect on the next reload, and so one process can serve several roots.
* Chrys's own keys are **never injected** into ``os.environ`` from dotenv
  files — settings keys travel through the settings layers, the model-profile
  pointer through its registry, and IPC keys only through the real
  environment or an explicit subprocess overlay.
* Whether a name *is* one of Chrys's keys has exactly one implementation,
  :func:`canonical_env_name` + :func:`classify_env_name`, and it is
  case-insensitive on Windows because ``os.environ`` is.
"""

from __future__ import annotations

import io
import logging
import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import Enum, auto
from functools import cache
from pathlib import Path
from types import MappingProxyType
from typing import Final

from chrys.foundation.config.settings import Settings
from chrys.foundation.config.spec import specs_by_field
from chrys.foundation.text.encoding import decode_bytes

logger = logging.getLogger(__name__)

DOTENV_LOCK_TIMEOUT_SECONDS: Final = 10.0
"""Bound on waiting for the dotenv lock, matching the YAML config documents.

Only the readers that must prove what they read take it; the settings layers
stay lock-free, so a stuck lock can never keep the process from loading.
"""


IPC_ENV_NAMES: Final = frozenset(
    {
        # Hook subprocess parameter passing (service/hooks/runner.py).
        "CHRYS_HOOK_ID",
        "CHRYS_HOOK_EVENT",
        "CHRYS_HOOK_PAYLOAD_FILE",
        "CHRYS_HOOK_RESULT",
        # Current ACP sub-agent depth — a counter, not a limit; treating it as
        # a setting would defeat the recursion guard it exists to feed.
        "CHRYS_ACP_SUBAGENT_DEPTH",
        # Packaging probe consulted before subprocess launches.
        "CHRYS_FORCE_FROZEN",
    }
)
"""Process-internal IPC carriers. Never settings, never read from dotenv.

They accept exactly two sources: the real process environment, and the
explicit overlay Chrys applies when it creates its own subprocesses. A
repository ``.env`` must not be able to mint IPC state — a checked-in
``CHRYS_ACP_SUBAGENT_DEPTH=3`` would silently strip every external sub-agent
from whoever clones it.
"""

POINTER_ENV_NAMES: Final = frozenset({"CHRYS_MODEL_PROFILE"})
"""The live in-process pointer (§7): a runtime channel, not a config source."""


class EnvNameKind(Enum):
    """What a ``CHRYS_*`` environment name is to the settings machinery."""

    SETTING = auto()
    """An A-class user setting; its durable home is the settings store."""

    POINTER = auto()
    """The B-class runtime pointer; provenance comes from its registry."""

    IPC = auto()
    """A C-class process-internal carrier; not a user setting at all."""


def canonical_env_name(name: str) -> str:
    """Fold an environment name to its platform identity.

    ``os.environ`` is case-insensitive on Windows, so ``chrys_theme`` and
    ``CHRYS_THEME`` are one key there and two keys on POSIX. Every membership
    test against Chrys's key sets must go through this fold — a check that
    compares raw spellings lets a lower-cased line in a dotenv file slip past
    both the no-injection rule and the migration's removal matching.
    """
    from chrys.foundation.platform import get_platform

    return name.upper() if get_platform().is_windows else name


@cache
def _setting_env_names() -> frozenset[str]:
    """Env aliases of every Settings field, pointer excluded.

    Derived from the dataclass metadata so a field added with an ``env``
    alias is classified without anyone remembering this module exists. All
    declared aliases are uppercase (a test pins this), so the set needs no
    per-platform folding — only the *probed* name does.
    """
    names = frozenset(entry.env for entry in specs_by_field(Settings).values() if entry.env is not None)
    return names - POINTER_ENV_NAMES


def classify_env_name(name: str) -> EnvNameKind | None:
    """Classify one environment name; ``None`` means "not Chrys's key"."""
    canonical = canonical_env_name(name)
    if canonical in IPC_ENV_NAMES:
        return EnvNameKind.IPC
    if canonical in POINTER_ENV_NAMES:
        return EnvNameKind.POINTER
    if canonical in _setting_env_names():
        return EnvNameKind.SETTING
    return None


@dataclass(frozen=True, slots=True)
class ProcessEnvSnapshot:
    """``os.environ`` as it stood before bootstrap — the one frozen layer."""

    values: Mapping[str, str]


_snapshot: ProcessEnvSnapshot | None = None


def freeze_process_env() -> ProcessEnvSnapshot:
    """Snapshot the real environment, once; later calls return the first.

    Idempotent rather than erroring: ``bootstrap_runtime`` is the single
    production caller, but embedded callers and tests may bootstrap more than
    once in a process, and the *first* environment is the only one that is
    still "real" — everything after may contain our own injections.
    """
    global _snapshot
    if _snapshot is None:
        _snapshot = ProcessEnvSnapshot(values=MappingProxyType(dict(os.environ)))
    return _snapshot


def process_env_snapshot() -> ProcessEnvSnapshot | None:
    """Return the frozen snapshot, or ``None`` before bootstrap.

    ``None`` is a real state, not an error: tools and unit tests read
    settings without bootstrapping. Callers fall back to the live
    environment in that state and must not cache the fallback.
    """
    return _snapshot


def _reset_process_env_snapshot_for_tests() -> None:
    global _snapshot
    _snapshot = None


def dotenv_disabled() -> bool:
    """Whether the user asked for dotenv files to be left alone entirely.

    ``load_dotenv`` consumes ``PYTHON_DOTENV_DISABLED``; ``dotenv_values``
    does not, so both explicit paths have to judge it themselves (same truthy
    set as ``dotenv.main._load_dotenv_disabled`` on 1.2.2). It gates the
    settings layer as well as the injection: until this refactor Chrys's own
    keys reached ``Settings`` only by being loaded into the environment, so a
    disabled boot ignored them, and a layer that read the same file directly
    would quietly take the opt-out away.

    Judged against the frozen snapshot for the same reason the ENV layer is:
    the opt-out belongs to the shell that started the process. Read live, a
    dotenv file that sets the flag would disable *itself* halfway through
    bootstrap — injection exports the flag, and the settings layer then
    refuses to read the very file it came from, so the Chrys keys beside it
    vanish where ``load_dotenv`` would have applied them — and any later
    write to ``os.environ`` would silently drop the dotenv layers out of the
    next reload.
    """
    snapshot = process_env_snapshot()
    source: Mapping[str, str] = snapshot.values if snapshot is not None else os.environ
    value = source.get("PYTHON_DOTENV_DISABLED", "")
    return value.casefold() in {"1", "true", "t", "yes", "y"}


def _occurrences(path: Path) -> list[tuple[str, str | None]]:
    """The file's assignments in order, duplicates preserved, uninterpolated.

    ``dotenv_values(..., interpolate=False)`` is NOT equivalent: it folds to a
    dict first, so ``A=one / B=${A} / A=two`` loses the ``A=one`` occurrence
    and ``B`` resolves to the wrong value. The occurrence list is the only
    faithful intermediate representation. ``DotEnv.parse`` is undocumented,
    which is why python-dotenv is pinned exactly and a contract test compares
    this module against ``load_dotenv`` byte for byte.

    Raises ``OSError`` for a file it cannot read, where ``DotEnv(path)`` would
    have parsed an empty stream. Every caller already treats "unreadable" as
    its own case, and the one that deletes what it read depends on the two not
    being confused.
    """
    return _occurrences_of(path.read_bytes())


def _occurrences_of(raw: bytes) -> list[tuple[str, str | None]]:
    """The same reading of *raw*, for a caller that already holds the bytes.

    The shared decoder prefers UTF-8 and detects legacy encodings, matching
    other user configuration readers without relying on the ambient locale.
    ``StringIO(newline=None)`` retains ``load_dotenv``'s universal-newline
    translation, including inside quoted multiline values. Parsing and
    interpolation still belong to python-dotenv.
    """
    from dotenv.main import DotEnv

    with io.StringIO(decode_bytes(raw, errors="strict"), newline=None) as stream:
        return list(DotEnv(None, stream=stream, interpolate=False, override=True).parse())


def _resolve_occurrences(
    pairs: Iterable[tuple[str, str | None]],
    base: Mapping[str, str],
    *,
    override: bool = True,
) -> dict[str, str | None]:
    """Resolve ``${VAR}`` references against an explicit base.

    Mirrors ``dotenv.main.resolve_variables`` — with ``override`` keys earlier
    in the same file win over the base, without it the base wins — except the
    base is a parameter instead of hard-coded ``os.environ``. That difference
    is the point: the settings layers must resolve against the frozen process
    snapshot so the same file content always parses to the same values, no
    matter what has been injected since.
    """
    from dotenv.variables import parse_variables

    resolved: dict[str, str | None] = {}
    for name, value in pairs:
        if value is None:
            resolved[name] = None
            continue
        earlier = {key: val for key, val in resolved.items() if val is not None}
        visible = {**base, **earlier} if override else {**earlier, **base}
        resolved[name] = "".join(atom.resolve(visible) for atom in parse_variables(value))
    return resolved


def read_dotenv_layer(path: Path, *, base: Mapping[str, str]) -> Mapping[str, str]:
    """Parse one dotenv file for the settings layers. Re-reads every call.

    Returns the fully-resolved assignments; bare keys (no ``=``) are dropped,
    and an absent or unreadable file reads as empty. Never cached and never a
    singleton: an ACP process serves several roots, and external edits must
    take effect on the next reload — both die the moment this holds state.

    A file the user disabled reads as empty too — see :func:`dotenv_disabled`.
    """
    if dotenv_disabled() or not path.is_file():
        return {}
    try:
        resolved = _resolve_occurrences(_occurrences(path), base)
    except OSError, UnicodeError:
        logger.warning("Unreadable dotenv file ignored: %s", path, exc_info=True)
        return {}
    return {name: value for name, value in resolved.items() if value is not None}


@dataclass(frozen=True, slots=True)
class DotenvSnapshot:
    """One dotenv read that succeeded, and the identity of what it read."""

    values: Mapping[str, str]
    fingerprint: str | None
    """Digest of the bytes ``values`` came from; ``None`` when the file is absent."""


def read_dotenv_snapshot(path: Path, *, base: Mapping[str, str]) -> DotenvSnapshot | None:
    """Parse *path* and digest what was parsed, or ``None`` if it cannot be read.

    For the caller that will later *delete* from this file and must prove two
    things about the values it imported: that they came from the bytes it is
    about to edit, and that they are the file's real contents rather than the
    empty mapping :func:`read_dotenv_layer` returns for a file it could not
    read. Those two are not the same question, and answering only the first is
    how an unreadable file becomes an erased one: an empty parse reads as "no
    keys here", the digest still matches because the bytes never changed, and
    the cleanup deletes every key from a file nothing was imported from.

    So a read failure is reported as ``None`` and never as an empty file. And
    the digest is over the bytes that were parsed, from a single read: the lock
    holds off every Chrys writer, but nothing holds off a text editor, and two
    reads are two different files as far as one is concerned. It takes one
    write between them for the values to come from a file the digest does not
    describe — and one more, back to the original, for the cleanup's recheck to
    pass and delete lines that were never imported.

    An absent file is not a failure; it reads as empty with no fingerprint,
    which is what a fresh install looks like.
    """
    from chrys.foundation.config.env_file import env_lock_path
    from chrys.foundation.platform.files import digest_bytes
    from chrys.foundation.util.lock import FileLock

    if dotenv_disabled() or not path.is_file():
        return DotenvSnapshot(values={}, fingerprint=None)
    with FileLock(env_lock_path(path), timeout=DOTENV_LOCK_TIMEOUT_SECONDS):
        try:
            raw = path.read_bytes()
            occurrences = _occurrences_of(raw)
        except OSError, UnicodeError:
            logger.warning("Unreadable dotenv file left in place: %s", path, exc_info=True)
            return None
    resolved = _resolve_occurrences(occurrences, base)
    values = {name: value for name, value in resolved.items() if value is not None}
    return DotenvSnapshot(values=values, fingerprint=digest_bytes(raw))


def inject_bootstrap_dotenv(
    paths: Iterable[Path],
    *,
    override: bool,
) -> None:
    """Inject non-Chrys dotenv variables into ``os.environ`` at bootstrap.

    This is the replacement for the ``load_dotenv`` calls: same file order,
    same per-line visibility (live ``os.environ`` plus earlier keys of the
    same file, the latter winning). Chrys's own keys (settings, pointer, IPC
    alike) are skipped — the reason this function exists. Values still
    *resolve* inside their own file, so a same-file ``${CHRYS_*}`` reference
    keeps working; the key just never lands in the process environment.

    The opt-out is decided once, from the shell, rather than re-evaluated
    between files the way back-to-back ``load_dotenv`` calls did. That was a
    real capability and it is deliberately not kept: the project file is read
    first, so a ``PYTHON_DOTENV_DISABLED=1`` line in a cloned repository used
    to switch off the user's own ``~/.chrys/.env`` — for its SDK variables
    then, and for its settings layer now. A repository does not get to decide
    which of the user's files Chrys is allowed to read.

    Inside each file Chrys elects to read, SDK-facing variables (API keys,
    proxies) keep python-dotenv's parsing and precedence semantics. The
    cross-file opt-out difference above is intentional.
    """
    if dotenv_disabled():
        return
    for path in paths:
        if not path.is_file():
            continue
        try:
            resolved = _resolve_occurrences(_occurrences(path), dict(os.environ), override=override)
        except OSError, UnicodeError:
            logger.warning("Unreadable dotenv file skipped at bootstrap: %s", path, exc_info=True)
            continue
        for name, value in resolved.items():
            if value is None or classify_env_name(name) is not None:
                continue
            if not override and name in os.environ:
                continue
            os.environ[name] = value
