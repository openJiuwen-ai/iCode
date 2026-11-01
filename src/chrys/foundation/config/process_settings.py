# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The settings that are fixed for the life of the process.

A handful of settings are consumed by code that has no engine, no session and
no ``Settings`` in reach: an import-time module constant, a probe cached behind
a process lock, a TUI helper called from a widget. They read ``os.environ``
directly today, which works only because bootstrap injects every ``CHRYS_*``
into it. Once the dotenv layers stop being injected, those readers go blind —
so they need a source that is neither the environment nor a per-session object.

That source is this module: one snapshot, written once by the entrypoint
bootstrap, read by everyone else. It carries **only** fields declared
``Apply.RESTART``, which is the same statement from the other side — a value
that a restart is required to change is exactly a value a process snapshot can
hold honestly. Nothing here may be a ``RELOAD`` or ``LIVE`` field: those change
under a live process, and a reader that took them from here would silently keep
a stale value forever.

Until bootstrap installs a snapshot, reads fall back to a fresh load. Tests and
tooling that never bootstrap therefore keep seeing what they configure, and
production is unaffected because every entrypoint bootstraps before any of
these readers can run.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields, replace
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping

    from chrys.foundation.config.settings import Settings
    from chrys.foundation.config.settings_store import LoadedSettings
    from chrys.foundation.config.spec import SettingOrigin


@dataclass(frozen=True, slots=True)
class ProcessSettings:
    """RESTART-scoped settings, valid for this process only."""

    raw_http_capture: bool
    prompt_history_enabled: bool
    chat_file_snapshot_inline_chars: int
    mutation_trace_mode: str
    mutation_trace_fsatrace_path: str
    session_root_dir: str
    credential_command_ttl_seconds: int

    @classmethod
    def from_settings(cls, settings: Settings) -> ProcessSettings:
        """Project the RESTART-scoped subset out of a loaded ``Settings``."""
        return cls(
            raw_http_capture=settings.raw_http_capture,
            prompt_history_enabled=settings.prompt_history_enabled,
            chat_file_snapshot_inline_chars=settings.chat_file_snapshot_inline_chars,
            mutation_trace_mode=settings.mutation_trace_mode,
            mutation_trace_fsatrace_path=settings.mutation_trace_fsatrace_path,
            session_root_dir=settings.session_root_dir,
            credential_command_ttl_seconds=settings.credential_command_ttl_seconds,
        )


def _snapshot_field_names() -> tuple[str, ...]:
    """The ``Settings`` field names this snapshot covers.

    Derived from the dataclass rather than listed again, so a field added to
    the snapshot cannot be left out of the freeze below and quietly go back to
    changing under a live process.
    """
    return tuple(entry.name for entry in fields(ProcessSettings))


def _snapshot_keys() -> tuple[str, ...]:
    from chrys.foundation.config.settings import Settings
    from chrys.foundation.config.spec import specs_by_field

    specs = specs_by_field(Settings)
    return tuple(specs[name].key for name in _snapshot_field_names())


def settle_session_root(loaded: LoadedSettings) -> LoadedSettings:
    """Resolve the configured session root against the disk, once, before install.

    Whether a path works is not a question a coercer can answer: ``/etc/passwd``
    is a perfectly good string and a hopeless session root, and the difference
    is on disk rather than in the text. So the loader accepted it and the
    consumer quietly fell back — leaving the settings object claiming a value
    nothing in the process was using, with provenance crediting a layer that had
    in fact lost. A panel reading that would show the wrong effective value and
    the wrong source, and the rejection the user needed to see was a log line.

    Deciding it here makes one answer true everywhere: what the settings say is
    what the consumers will do, and the rejected value is reported through the
    same channel as every other. Nothing is created — see
    :func:`session_root_is_ruled_out` for what that does and does not catch.
    """
    from chrys.foundation.config.coercion import Coerced, CoerceReason, CoerceStatus
    from chrys.foundation.config.settings import Settings, session_root_is_ruled_out
    from chrys.foundation.config.settings_store import SettingsWarning
    from chrys.foundation.config.spec import specs_by_field

    raw = loaded.settings.session_root_dir.strip()
    if not raw or not session_root_is_ruled_out(raw):
        return loaded

    key = specs_by_field(Settings)["session_root_dir"].key
    warning = SettingsWarning(
        key=key,
        origin=loaded.source_for(key),
        outcome=Coerced(status=CoerceStatus.INVALID, raw=raw, reason=CoerceReason.NOT_A_DIRECTORY),
    )
    return replace(
        loaded,
        # Emptied rather than replaced with the fallback path: "unset" is how
        # every layer spells "use the default", and writing the resolved default
        # in would make the panel show a value nobody chose.
        settings=replace(loaded.settings, session_root_dir=""),
        provenance={name: origin for name, origin in loaded.provenance.items() if name != key},
        warnings=(*loaded.warnings, warning),
    )


@dataclass(frozen=True, slots=True)
class _Installed:
    """What bootstrap fixed, where each of those values came from, and why."""

    settings: ProcessSettings
    provenance: Mapping[str, SettingOrigin]
    sealed: frozenset[str]


_installed: _Installed | None = None


def install_process_settings(loaded: LoadedSettings) -> None:
    """Fix the RESTART-scoped values for the rest of this process.

    Called once per process, from the entrypoint bootstrap. Calling it again
    with different values does not retroactively change what earlier readers
    saw, which is the honest meaning of ``RESTART`` — a soft restart replays
    the settings but not the module constants derived from them.

    The origins and seals are kept alongside the values because
    :func:`freeze_process_settings` has to be able to say not just *what* is in
    force but *where it came from* and *why*: a user who deletes the variable
    and reloads would otherwise be told the surviving value is the built-in
    default, and one whose typo was sealed out at bootstrap would lose the only
    explanation of why their file is being ignored.
    """
    global _installed
    keys = _snapshot_keys()
    _installed = _Installed(
        settings=ProcessSettings.from_settings(loaded.settings),
        provenance={key: loaded.provenance[key] for key in keys if key in loaded.provenance},
        sealed=frozenset(key for key in keys if key in loaded.sealed_keys),
    )


def freeze_process_settings(loaded: LoadedSettings) -> LoadedSettings:
    """Make a re-loaded *loaded* agree with what this process actually does.

    A reload re-reads every key, RESTART ones included — but a RESTART key is
    exactly the one this process will not act on again: the snapshot was taken
    at bootstrap and every reader listed at the top of this module is still
    holding it. Left alone, the reloaded settings would report a value nothing
    in the process uses, crediting the layer that supplied it. That is the same
    divergence :func:`settle_session_root` removes for one field, arriving by a
    different route, and it is the reading a settings panel would show.

    The pending value is not lost so much as not yet in force; showing it as
    "saved, applies after restart" is the job of the Apply-tier routing, and
    that routing needs this to be true first.

    Only the fields this snapshot actually holds are frozen. The other
    ``Apply.RESTART`` fields have no process-wide reader — their consumers take
    them from ``Settings`` during the rebuild a reload performs — so freezing
    them here would change behaviour rather than describe it.

    A no-op before bootstrap: with nothing installed there is nothing a reader
    could already be holding, so nothing can have diverged.
    """
    if _installed is None:
        return loaded

    # ``asdict`` rather than a getattr loop: still derived from the dataclass,
    # so a field added to the snapshot cannot be left out of the freeze, but
    # without reaching into a first-party type by name (``AGENTS.md``).
    return _hold(
        loaded,
        keys=set(_snapshot_keys()),
        values=asdict(_installed.settings),
        provenance=_installed.provenance,
        sealed=_installed.sealed,
    )


def _hold(
    loaded: LoadedSettings,
    *,
    keys: set[str],
    values: Mapping[str, Any],
    provenance: Mapping[str, SettingOrigin],
    sealed: frozenset[str],
) -> LoadedSettings:
    """Return *loaded* with *keys* held at the given value, origin and seal.

    The one place those three are swapped together, because they describe one
    decision: a seal carried across from a load that is not in force answers
    "why is my file being ignored" about the wrong load, in both directions.
    ``replace`` on the whole object, so warnings and unknown keys ride along —
    a value typed wrong is worth reporting whether or not this process was
    ever going to act on it.
    """
    return replace(
        loaded,
        settings=replace(loaded.settings, **values),
        provenance={
            **{key: origin for key, origin in loaded.provenance.items() if key not in keys},
            **provenance,
        },
        sealed_keys=(loaded.sealed_keys - keys) | sealed,
    )


def route_restart_settings(
    candidate: LoadedSettings, in_force: LoadedSettings
) -> tuple[LoadedSettings, tuple[str, ...]]:
    """Apply the RESTART tier to a re-load: every RESTART value stays in force.

    The snapshot fields come back from :func:`freeze_process_settings`. The
    RESTART fields outside the snapshot have no process-wide reader to freeze
    from, but their consumers re-read ``Settings`` during the rebuild a reload
    performs — left alone, a reload would apply them, making the RESTART label
    a lie in the other direction. Those are held at the values already in
    force (*in_force*, the loaded settings the process is running on), which
    are the bootstrap values by induction: every earlier reload passed through
    this same routing. Value, origin and seal move together, for the reason
    the freeze gives.

    Also returns the dotted keys whose newly loaded value was deferred — the
    list a frontend needs to say "saved, takes effect after restart". A no-op
    before bootstrap, like the freeze it composes.
    """
    if _installed is None:
        return candidate, ()

    from chrys.foundation.config.settings import Settings
    from chrys.foundation.config.spec import Apply, specs_by_field

    specs = specs_by_field(Settings)
    snapshot_names = set(_snapshot_field_names())
    held_names = tuple(
        name for name, entry in specs.items() if entry.apply is Apply.RESTART and name not in snapshot_names
    )
    held_keys = {specs[name].key for name in held_names}
    in_force_values = asdict(in_force.settings)
    routed = _hold(
        freeze_process_settings(candidate),
        keys=held_keys,
        values={name: in_force_values[name] for name in held_names},
        provenance={key: origin for key, origin in in_force.provenance.items() if key in held_keys},
        sealed=in_force.sealed_keys & held_keys,
    )

    candidate_values = asdict(candidate.settings)
    routed_values = asdict(routed.settings)
    deferred = tuple(
        entry.key
        for name, entry in specs.items()
        if entry.apply is Apply.RESTART and routed_values[name] != candidate_values[name]
    )
    return routed, deferred


def reattribute_command_line(loaded: LoadedSettings, previous: LoadedSettings) -> LoadedSettings:
    """Keep crediting the command line for what it decided, across a re-load.

    The command line cannot change while the process runs, but a re-load reads
    from the environment, where startup parked ``--model`` so it would survive
    exactly this moment — so the same value comes back labelled ``ENV``, telling
    the panel the user configured a variable they never touched. Here rather
    than in one frontend's handler because every re-load a live process
    performs — settings reload, workspace change, restore — repeats the same
    moment; it is the process-lifetime sibling of :func:`route_restart_settings`
    and is applied wherever that is.

    Re-attribution only, never re-imposition: the label moves back to ``CLI``
    when the re-load produced the very value the command line chose, and stays
    where the re-load put it when the value has moved on — a model applied from
    the config screen replaces the parked value, and must not be relabelled or
    overridden as the flag's.

    Which is why it is the *carrier* that qualifies, not the value. Picking the
    profile the flag already named is a real runtime choice that happens to
    land on the same string, and the re-load labels it ``PROCESS_RUNTIME``
    correctly; relabelling it would tell the panel — and ACP's session source —
    that a flag decided what the user just decided, and it would keep saying so
    for the rest of the session, because this function's own output is the
    ``previous`` of the next re-load.
    """
    from chrys.foundation.config.settings import Settings
    from chrys.foundation.config.spec import Source, specs_by_field

    # ``asdict`` rather than a getattr loop: still derived from the dataclass,
    # so the comparison cannot drift from the fields, but without reaching into
    # a first-party type by name (``AGENTS.md``).
    previous_values = asdict(previous.settings)
    loaded_values = asdict(loaded.settings)
    kept = {
        name: previous_values[name]
        for name, entry in specs_by_field(Settings).items()
        if previous.source_for(entry.key).layer is Source.CLI
        and loaded.source_for(entry.key).layer is Source.ENV
        and loaded_values[name] == previous_values[name]
    }
    return loaded.overlay(Source.CLI, **kept) if kept else loaded


def reset_process_settings() -> None:
    """Drop the installed snapshot so reads fall back to a fresh load."""
    global _installed
    _installed = None


def process_settings() -> ProcessSettings:
    """The RESTART-scoped settings, loading them if bootstrap has not run."""
    if _installed is not None:
        return _installed.settings

    from chrys.foundation.config.settings_store import load_settings

    return ProcessSettings.from_settings(load_settings().settings)
