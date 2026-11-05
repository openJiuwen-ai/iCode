# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The one place settings are assembled from their sources.

``Settings`` is a plain value object; this module is what turns the layered
sources into one: the user document, the dotenv files beside it, the process
environment as it stood before bootstrap, the project trust domain under the
caller's ``project_root``, and the pins a frontend passes in.

The load runs in two phases. Phase one reads every file whole and audits the
project layers completely — policy filter, per-key coercion, tighten/loosen
verdicts — whether or not anything above will shadow them, because a rejected
project value must warn even when it would have lost anyway. Phase two picks
each key's winner from the highest layer down; it is the only phase
``InvalidPolicy.SAFE_DEFAULT`` terminates. The non-project layers keep their
established warning scope (a layer below the winner stays unexamined), so
effective values and existing warnings are unchanged by the split.

Callers get three things back and are expected to keep all of them: the
settings, where each value came from, and what was rejected on the way. A
caller that only wants the settings can say so with ``.settings``, but it
cannot pretend the other two do not exist — that is how the panel ends up
unable to explain why the value a user just typed has no effect.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final

from chrys.foundation.config.coercion import Coerced, CoerceReason, CoerceStatus, invalid
from chrys.foundation.config.context import EvalContext
from chrys.foundation.config.env_file import config_env_path
from chrys.foundation.config.env_layers import (
    POINTER_ENV_NAMES,
    canonical_env_name,
    process_env_snapshot,
    read_dotenv_layer,
)
from chrys.foundation.config.runtime_pointer import get_model_pointer
from chrys.foundation.config.settings import DEFAULT_MAX_TRANSIENT_RETRIES, Settings
from chrys.foundation.config.spec import (
    FILE_SOURCES,
    InvalidPolicy,
    Kind,
    ProjectMerge,
    SettingOrigin,
    SettingSpec,
    Source,
    kind_accepts,
    specs_by_field,
)
from chrys.foundation.config.user_settings import apply_settings_patch, flatten_user_doc, user_settings_path
from chrys.foundation.config.yaml_store import (
    LOCK_TIMEOUT_SECONDS,
    read_yaml_doc,
    read_yaml_doc_readonly,
    update_yaml_doc,
)
from chrys.foundation.platform.runtime_paths import same_path

logger = logging.getLogger(__name__)

DEFAULT_EVAL_CONTEXT = EvalContext(frontend_default_max_transient_retries=DEFAULT_MAX_TRANSIENT_RETRIES)
"""Interactive-frontend policy. ``chrys run`` passes its own (15)."""

_FRONTEND_DEFAULT_FIELD = "frontend_default_max_transient_retries"

_EMPTY_MAP: Final[Mapping[str, Any]] = MappingProxyType({})


@dataclass(frozen=True, slots=True)
class SettingsWarning:
    """One value a layer offered and the loader refused, or had to clamp."""

    key: str
    origin: SettingOrigin
    outcome: Coerced
    """The verdict itself, carried whole.

    Copying ``raw``/``reason``/``limit`` out field by field is how the two
    representations start disagreeing — a clamp reports both the bound it hit
    and the value it landed on, and only one of those is ``outcome.value``.
    """

    @property
    def rejected(self) -> bool:
        """Whether the value was dropped entirely rather than adjusted."""
        return self.outcome.status is CoerceStatus.INVALID


@dataclass(frozen=True, slots=True)
class DormantProjectConfig:
    """Project-layer configuration found on disk while the gate was off.

    Reported as a whole file rather than key by key: the user's decision here
    is a single one — enable ``project.config_enabled`` or leave it — and a
    warning per key would say the same thing as many times as the repository
    has opinions.
    """

    path: Path
    keys: tuple[str, ...]
    """The dotted keys that would be considered if the layer were enabled."""


@dataclass(frozen=True, slots=True)
class DormantProjectSource:
    """Project hooks or skills found on disk while their own setting was off."""

    key: str
    """The setting that loads them: ``project.hooks_enabled`` or ``project.skills_enabled``."""

    path: Path


@dataclass(frozen=True, slots=True)
class LoadedSettings:
    """Settings plus the two things a caller must not silently drop."""

    settings: Settings
    provenance: Mapping[str, SettingOrigin]
    warnings: tuple[SettingsWarning, ...] = ()
    sealed_keys: frozenset[str] = frozenset()
    """Keys forced to their built-in default by :attr:`InvalidPolicy.SAFE_DEFAULT`.

    A sealed key is the one case where a value the user can see in their own
    ``settings.yaml`` is deliberately not in effect, so the panel has to be able
    to say why instead of showing a saved value that does nothing.
    """

    unknown_keys: tuple[str, ...] = ()
    """Dotted keys the user document holds that no field declares.

    Reported once, not dropped: the keys survive every write untouched (a
    patch never rewrites what it did not name), so a downgrade to the version
    that understood them loses nothing — but silence here would leave a typo'd
    key looking configured forever.
    """

    dormant_project: tuple[DormantProjectConfig, ...] = ()
    """Project configuration discovered while ``project.config_enabled`` is off.

    Carried rather than dropped for the same reason warnings are: a repository
    shipped configuration, the user never authorised it, and silence would
    leave them believing it is in force. The caller says so once per file.
    """

    dormant_project_sources: tuple[DormantProjectSource, ...] = ()
    """Project hooks and skills discovered while their setting is off, reported the same way."""

    def overlay(self, layer: Source, **values: Any) -> LoadedSettings:
        """Return a copy carrying *values*, credited to *layer*.

        The only sanctioned way to change settings after a load. A bare
        ``dataclasses.replace`` on ``.settings`` — which is what every runtime
        override did — leaves provenance describing the value the field used to
        hold, and provenance that is right only some of the time is worse for
        the panel than none: it would offer to remove an override the user never
        set, and hide the one they did.

        Values are checked exactly as pins are, for the same reason: they arrive
        already typed, from our own code, and never meet a coercer otherwise.

        Only the layers without a file can be spelled this way. A file layer's
        origin must name the file it came from, which a caller holding one value
        does not know.
        """
        if layer in FILE_SOURCES:
            msg = f"{layer.name} values come from a file, so they cannot be overlaid"
            raise ValueError(msg)
        specs = specs_by_field(Settings)
        unknown = sorted(set(values) - set(specs))
        if unknown:
            msg = f"Unknown settings fields: {', '.join(unknown)}"
            raise TypeError(msg)
        origin = SettingOrigin(layer=layer)
        canonical = {name: _pinned(specs[name], value).value for name, value in values.items()}
        overlaid = {specs[name].key for name in canonical}
        return replace(
            self,
            settings=replace(self.settings, **canonical),
            provenance={**self.provenance, **dict.fromkeys(overlaid, origin)},
            # A sealed key is one nothing was allowed to write, so a key that
            # has just been written is no longer one. Leaving it in would have
            # the panel explain a built-in default that is not what is in use.
            sealed_keys=self.sealed_keys - overlaid,
        )

    def source_for(self, key: str) -> SettingOrigin:
        """Where *key*'s effective value came from, including which file.

        Scoped to this instance on purpose: with per-session project roots and
        model pins, a process-global ``effective_source(key)`` could not answer
        honestly for more than one session at a time.
        """
        return self.provenance.get(key, SettingOrigin(layer=Source.DEFAULT))


@dataclass(frozen=True, slots=True, kw_only=True)
class PreparedSettings:
    """A layered load, the session's own selection over it, and the effective value computed from both."""

    base: LoadedSettings
    effective: LoadedSettings
    session: Mapping[str, Any] = field(default_factory=dict)
    """The ``Source.SESSION`` values ``effective`` carries over ``base``: the session's own choices."""
    runtime_revision: int = 0
    """Which live choices ``effective`` was composed with (``SettingsHandle``'s count of them)."""


class SettingsHandle:
    """The one settings value that everything sharing a session points at.

    :class:`LoadedSettings` is replaced whole rather than written into, which
    keeps a single holder honest and lets two holders drift: the TUI and the
    engine start from the same load, and a live write rebinds one of them. The
    holders share this instead, so a change is installed once and everyone
    reading through it sees the same value, the same provenance and the same
    seals — the three that only mean something together.

    It is the mutable half of the split. ``LoadedSettings`` stays immutable so
    a caller can hold one across a rebuild and know it did not move underneath
    them; this names the place where moving is the point.

    Three parts, composed in this order: the layered load (``base``), the
    session's own choices (``Source.SESSION``: the model a session runs on
    when it is not the default), and live choices (``Source.RUNTIME``). The two
    upper parts are kept apart from the load so that reloading and rolling back
    — both of which replace the whole layered value — need not know about them,
    and so that a session's own choice can end without a reload.

    Per session, never process-global. ACP runs many sessions in one process
    with their own project roots and model pins, so a shared cell above the
    session is a cell where one session's pin becomes another's.
    """

    __slots__ = ("_base", "_effective", "_runtime", "_runtime_revision", "_session")

    def __init__(self, loaded: LoadedSettings) -> None:
        self._base = loaded
        self._session: dict[str, Any] = {}
        self._runtime: dict[str, Any] = {}
        self._runtime_revision = 0
        self._effective = loaded

    @property
    def loaded(self) -> LoadedSettings:
        """Current settings together with where each value came from."""
        return self._effective

    @property
    def settings(self) -> Settings:
        """Current values, for the readers that have no use for provenance."""
        return self._effective.settings

    @property
    def base(self) -> LoadedSettings:
        """The layered load alone: what a re-load is compared against and a rebuild on the current settings stages.

        Never :attr:`loaded` for those: installing an effective value as the
        next load would fold the session's own choices into it, where ending
        them could no longer take them back out.
        """
        return self._base

    @property
    def session_values(self) -> dict[str, Any]:
        """The session's own choices (a copy), as ``Settings`` field names."""
        return dict(self._session)

    @property
    def session_model(self) -> str:
        """The model profile the session chose for itself; empty when it runs on the default."""
        return str(self._session.get("model_profile", ""))

    def install(self, loaded: LoadedSettings) -> None:
        """Replace the layered load. The session's own and live choices stay on top of the new one.

        A reload re-reads the files and the environment, which is every layer
        *except* the one a live choice lives in — ``Source.RUNTIME`` is defined
        as the layer nothing loads from. Dropping those values here would undo
        the user's theme the next time anything triggered a reload, and the TUI
        would not even follow it back: Textual keeps showing the theme that was
        chosen, so the settings would simply stop describing the screen.

        The window where that is not merely a misattribution is real, because
        ``persist_theme`` and its siblings log and swallow their failures: when
        the write to disk did not land, this overlay is the only record that the
        choice was ever made.
        """
        self.install_prepared(self.prepare(loaded))

    def prepare(self, loaded: LoadedSettings, *, session: Mapping[str, Any] | None = None) -> PreparedSettings:
        """Compute *loaded* with the session's own choices (*session*, else the current ones) and live choices on top.

        Changes nothing installed: a rebuild is configured from the result and
        its commit installs it.
        """
        values = dict(self._session if session is None else session)
        return PreparedSettings(
            base=loaded,
            effective=self._compose(loaded, values),
            session=values,
            runtime_revision=self._runtime_revision,
        )

    def install_prepared(self, prepared: PreparedSettings) -> None:
        """Install values whose overlays have already succeeded, with the session's own choices they carry.

        A live choice made while a rebuild ran (after :meth:`prepare`) is not
        lost when the rebuild commits: the effective value is composed again
        with it, from values that were checked when they were chosen. Otherwise
        nothing is recomputed — a commit installs what was prepared.
        """
        self._base = prepared.base
        self._session = dict(prepared.session)
        if prepared.runtime_revision == self._runtime_revision:
            self._effective = prepared.effective
        else:
            self._reapply()

    def override(self, **values: Any) -> None:
        """Record a live choice, credited to ``Source.RUNTIME``.

        Kept apart from the load rather than folded into it so that reloading
        and rolling back — both of which replace the whole layered value — can
        do so without deciding what to do about choices they know nothing about.
        """
        self._runtime.update(values)
        self._runtime_revision += 1
        self._reapply()

    def persist_approval_default(self, mode: str, *, persist: Callable[[str], None]) -> str:
        """Persist a global choice and mirror its safe launch default in the runtime overlay."""
        persist(mode)
        default = "auto" if mode == "bypass" else mode
        self.override(default_approval_mode=default)
        return default

    def _compose(self, base: LoadedSettings, session: Mapping[str, Any]) -> LoadedSettings:
        effective = base.overlay(Source.SESSION, **session) if session else base
        return effective.overlay(Source.RUNTIME, **self._runtime) if self._runtime else effective

    def _reapply(self) -> None:
        """Rebuild the effective value: the load, the session's own choices, then live choices on top.

        Recomputed from ``_base`` every time rather than accumulated, so a
        rollback that restores an older load cannot resurrect an override that
        was replaced in the meantime, and repeating one is a no-op.
        """
        self._effective = self._compose(self._base, self._session)


def load_settings(
    *,
    project_root: Path | None = None,
    eval_context: EvalContext = DEFAULT_EVAL_CONTEXT,
    env: Mapping[str, str] | None = None,
    **pins: Any,
) -> LoadedSettings:
    """Assemble settings from every source, resolving each key downwards.

    Args:
        project_root: Workspace root whose project layer applies —
            ``<root>/.chrys/settings.yaml``, behind ``project.config_enabled``.
            ``None`` loads without a project layer, which is what a caller
            that serves no particular root (the ACP manager base) means by it.
            The root a caller passes is a trust decision, not a discovery
            hint: there is deliberately no walk upwards from it.
        eval_context: Frontend policy in force. Must be passed at load time —
            the project layer is evaluated *during* the load, so substituting
            it afterwards would arrive after the verdicts it informs.
        env: Environment to read instead of ``os.environ``. Tests inject here;
            production passes nothing.
        **pins: Explicit per-caller values that outrank every source, e.g. a
            session's restored model profile or a frontend's own default.
    """
    if env is not None:
        # An injected environment is the whole environment: hermetic, and not
        # this process's, so nothing about the real one may leak into it.
        source: Mapping[str, str] = env
        real_process = False
    else:
        # The environment layer is the environment *as bootstrap found it*.
        # Reading it live would let anything that writes ``os.environ`` mid-run
        # mint an ENV-layer setting that outranks the user's document and is
        # then reported as a shell export they never made — and would make two
        # loads of the same files disagree. Before bootstrap there is no
        # snapshot and the live environment is still the real one.
        snapshot = process_env_snapshot()
        source = snapshot.values if snapshot is not None else os.environ
        real_process = True

    if _FRONTEND_DEFAULT_FIELD in pins:
        # It is an input to the load, not a value the load produces, and the
        # project layer's tighten/loosen verdicts are evaluated against it. A
        # pin would arrive after those verdicts and be silently overwritten
        # here, so say so instead.
        msg = f"{_FRONTEND_DEFAULT_FIELD} is set through eval_context, not as a pin"
        raise TypeError(msg)

    values: dict[str, Any] = {}
    provenance: dict[str, SettingOrigin] = {}
    specs = specs_by_field(Settings)
    # An injected environment asks for a hermetic load, so it turns the file
    # layers off along with the real environment — a test passing ``env={}``
    # must not read the developer's own ``~/.chrys`` *or* the project files:
    # hermetic means hermetic, whatever root was passed alongside.
    file_layers = _read_file_layers(specs, project_root) if env is None else None
    # Phase one: the project trust domain is audited completely before any
    # winner is chosen, so a project value a higher layer shadows still gets
    # its policy verdict and its warning.
    project = _project_layers(specs, file_layers, eval_context) if file_layers is not None else _NO_PROJECT_LAYERS

    warnings: list[SettingsWarning] = list(project.warnings)
    sealed: set[str] = set()

    for name, entry in specs.items():
        for coerced, origin in _layers(name, entry, pins, source, real_process, file_layers, project):
            if coerced.status is CoerceStatus.INVALID:
                # Project verdicts were already reported whole in phase one
                # (shadowed-still-warns); consulting one here only decides.
                if origin.layer is not Source.PROJECT:
                    warnings.append(SettingsWarning(key=entry.key, origin=origin, outcome=coerced))
                if entry.invalid_policy is InvalidPolicy.SAFE_DEFAULT:
                    # Stop the walk. The built-in default is the safe landing
                    # spot this policy names, and no lower layer may replace it
                    # — which is also what makes "sealed" imply "source is
                    # DEFAULT": nothing below ever gets to write the key.
                    sealed.add(entry.key)
                    break
                continue
            if coerced.status is CoerceStatus.CLAMPED:
                warnings.append(SettingsWarning(key=entry.key, origin=origin, outcome=coerced))
            values[name] = coerced.value
            provenance[entry.key] = origin
            break

    # The frontend's retry policy is a property of how Chrys was launched, not
    # a configured value, so it never appears in any file and is never saved.
    values[_FRONTEND_DEFAULT_FIELD] = eval_context.frontend_default_max_transient_retries
    provenance["llm.retry.frontend_default"] = SettingOrigin(layer=Source.RUNTIME)

    # A pin naming a field that carries no spec still has to reach the instance;
    # an unknown name falls out of ``Settings(**values)`` as a ``TypeError``.
    values.update({name: value for name, value in pins.items() if name not in specs})

    settings = Settings(**values)
    return LoadedSettings(
        settings=settings,
        provenance=provenance,
        warnings=tuple(warnings),
        sealed_keys=frozenset(sealed),
        unknown_keys=file_layers.unknown_keys if file_layers is not None else (),
        dormant_project=project.dormant,
        dormant_project_sources=(
            _dormant_project_sources(settings, project_root)
            if file_layers is not None and project_root is not None
            else ()
        ),
    )


_PROJECT_HOOKS_FILE_NAMES: Final = ("hooks.yaml", "hooks.yml", "hooks.json")


def _holds_skill(directory: Path, depth: int = 0) -> bool:
    """Whether *directory* has a ``SKILL.md`` as deep as skill discovery looks.

    Mirrors ``service/skills/loader.py::discover_skill_directories``: the
    folder itself and two levels below it.
    """
    if (directory / "SKILL.md").is_file():
        return True
    if depth >= 2 or not directory.is_dir():
        return False
    try:
        children = [child for child in directory.iterdir() if child.is_dir()]
    except OSError:
        return False  # Discovery skips a folder it cannot list, and goes on with the rest.
    return any(_holds_skill(child, depth + 1) for child in children)


def _is_same_entry(left: Path, right: Path) -> bool:
    """Whether two spellings name one file or folder; on a caseless disk they may differ in case."""
    try:
        return left.samefile(right)
    except OSError:
        return same_path(left, right)


def _dormant_project_sources(settings: Settings, project_root: Path) -> tuple[DormantProjectSource, ...]:
    """The project hooks and skills *project_root* holds that their setting keeps unloaded.

    A workspace rooted at the user's home names the user's own hooks and skills
    folders again; those load as the user's and are never reported.
    """
    found: list[DormantProjectSource] = []
    try:
        if not settings.project_hooks_enabled:
            hooks_dir = project_root / ".chrys" / "hooks"
            if not _is_same_entry(hooks_dir, user_settings_path().parent / "hooks"):
                hooks_file = next(
                    (hooks_dir / name for name in _PROJECT_HOOKS_FILE_NAMES if (hooks_dir / name).is_file()), None
                )
                if hooks_file is not None:
                    found.append(DormantProjectSource(key="project.hooks_enabled", path=hooks_file))
        if not settings.project_skills_enabled:
            skills_dir = project_root / ".agents" / "skills"
            if not _is_same_entry(skills_dir, Path.home() / ".agents" / "skills") and _holds_skill(skills_dir):
                found.append(DormantProjectSource(key="project.skills_enabled", path=skills_dir))
    except OSError:
        # A notice, never a reason to fail a settings load.
        logger.debug("Could not check %s for project hooks or skills", project_root, exc_info=True)
    return tuple(found)


@dataclass(frozen=True, slots=True)
class _FileLayers:
    """The file-borne layers, read once per load, whole.

    Reading happens here and not in :func:`_layers` because a settings document
    is parsed once for every key it mentions — one pinned field cannot excuse
    the loader from reading the rest of the file.
    """

    user_yaml: Mapping[str, Any]
    """Dotted key → raw value, from ``~/.chrys/settings.yaml``."""

    user_yaml_path: Path
    user_env: Mapping[str, str]
    """Canonically folded env name → value, from ``~/.chrys/.env``."""

    user_env_path: Path
    unknown_keys: tuple[str, ...]

    project_yaml: Mapping[str, Any] = _EMPTY_MAP
    """Dotted key → raw value, from ``<root>/.chrys/settings.yaml``.

    The project layer's only file. A repository's ``.env`` is deliberately
    not read: dotenv is the legacy channel on its way out, so its ``CHRYS_*``
    lines stay inert rather than becoming a second project entrance.
    """

    project_yaml_unknown: tuple[str, ...] = ()
    project_yaml_path: Path | None = None
    """``None`` when no distinct project document applies."""


def _read_file_layers(specs: Mapping[str, SettingSpec], project_root: Path | None) -> _FileLayers | None:
    """Read the file-borne layers, or ``None`` before bootstrap froze the env.

    The snapshot gate is the hermeticity gate: a process that never called
    :func:`freeze_process_env` — every test that constructs settings directly —
    has not opted into this machine's home directory, so the files stay
    unread rather than leaking one developer's ``~/.chrys`` into assertions.
    It is also what the dotenv layer resolves against: values interpolate over
    the environment as it was at bootstrap, never the mutated live one.
    """
    snapshot = process_env_snapshot()
    if snapshot is None:
        return None
    yaml_path = user_settings_path()
    # ``None`` covers both "no document yet" and "nothing parseable", and an
    # unparseable document must load as empty rather than not at all: the
    # environment layers above it still apply, and the panel still opens.
    try:
        doc = read_yaml_doc(yaml_path) or {}
    except TimeoutError:
        # Another process was inside the document's lock for the whole wait.
        # The same rule as an unparseable file, for a stronger reason: this
        # read is on the startup path of every entry point, so raising here
        # means a second Chrys cannot start at all while the first is stalled
        # mid-write. The values genuinely are not in force this launch, which
        # is exactly what provenance will report, and the dotenv layer below
        # needs no lock and still applies.
        logger.warning("Settings document was busy; loading without it: %s", yaml_path, exc_info=True)
        doc = {}
    known_keys = frozenset(entry.key for entry in specs.values())
    values, unknown = flatten_user_doc(doc, known_keys)
    env_path = config_env_path()
    layer = read_dotenv_layer(env_path, base=snapshot.values)
    # Fold names the way the OS does, so on Windows a lowercase spelling in
    # the file still answers for the uppercase alias. Later spellings win the
    # collision, matching dotenv's own later-assignment-wins rule.
    user_env = {canonical_env_name(env_name): value for env_name, value in layer.items()}

    project_yaml: Mapping[str, Any] = _EMPTY_MAP
    project_yaml_unknown: tuple[str, ...] = ()
    project_yaml_path: Path | None = None
    if project_root is not None:
        candidate = project_root / ".chrys" / "settings.yaml"
        # A workspace rooted at the user's home names the user document again.
        # It remains the trusted USER layer; reading it a second time as an
        # untrusted PROJECT layer would reject preferences and, with the gate
        # off, report the user's own settings as dormant project configuration.
        if not _is_same_entry(candidate, yaml_path):
            project_yaml_path = candidate
            # Same empty-on-unparseable rule as the user document, read without
            # lock, backup or repair — the file lives in someone's working tree.
            project_doc = read_yaml_doc_readonly(project_yaml_path) or {}
            project_yaml, project_yaml_unknown = flatten_user_doc(project_doc, known_keys)

    return _FileLayers(
        user_yaml=values,
        user_yaml_path=yaml_path,
        user_env=user_env,
        user_env_path=env_path,
        unknown_keys=unknown,
        project_yaml=project_yaml,
        project_yaml_unknown=project_yaml_unknown,
        project_yaml_path=project_yaml_path,
    )


@dataclass(frozen=True, slots=True)
class _ProjectLayers:
    """Phase-one verdicts for the project trust domain, taken whole.

    ``values`` holds only what survived the policy — already coerced to
    canonical, so phase two contributes it without a second verdict and a
    shadowed value never re-warns. Everything that did not survive is in
    ``warnings`` (or ``dormant``, when the gate was off and no per-key
    question was ever asked).
    """

    values: Mapping[str, Any] = _EMPTY_MAP
    """Field name → canonical value accepted from ``<root>/.chrys/settings.yaml``."""

    rejected: Mapping[str, Coerced] = _EMPTY_MAP
    """Field name → the phase-one verdict on a value the project may set but wrote wrong.

    Only genuine value invalids — a policy rejection
    (:attr:`CoerceReason.NOT_ALLOWED_IN_PROJECT`,
    :attr:`CoerceReason.LOOSENS_USER_BASELINE`) means the layer had no standing
    for the key at all and stays mute in phase two. A garbage value on a key the
    whitelist *does* grant is a different thing: the layer had standing and
    spoke unintelligibly, and phase two must see that so
    :attr:`InvalidPolicy.SAFE_DEFAULT` can land the key on the built-in default
    instead of letting the bad value uncap whatever a lower layer holds.
    """

    warnings: tuple[SettingsWarning, ...] = ()
    dormant: tuple[DormantProjectConfig, ...] = ()


_NO_PROJECT_LAYERS = _ProjectLayers()


def _user_doc_verdict(entry: SettingSpec, raw: Any) -> Coerced:
    """One user-document value's verdict, including the written-``null`` case."""
    coerced = entry.coerce(raw)
    if coerced.status is CoerceStatus.MISSING and raw is None and kind_accepts(entry.kind, raw):
        # A written ``null`` is a value, not a blank. The document is typed
        # — unlike an env string, which is why the coercers read a blank as
        # "say nothing" — and :func:`persist` writes exactly this spelling
        # for an ``OPTIONAL_INT`` the user cleared. Dropping it here would
        # make "no timeout" unstorable: the panel writes ``null``, the load
        # ignores it, and the field snaps back to its default with no
        # warning. Only ``null`` is read back this way, and
        # :func:`kind_accepts` is what makes that exact: it accepts ``None``
        # for no other kind. ``""`` deliberately stays a blank — persisting
        # an empty value removes the key instead of writing one, so an
        # empty string in the document is a hand-edit that most likely
        # means "unset", which is what falling through already gives.
        coerced = Coerced(status=CoerceStatus.VALID, value=raw)
    return coerced


def _default_user_baseline(specs: Mapping[str, SettingSpec], file_layers: _FileLayers) -> Settings:
    """``DEFAULT + USER`` merged — what every project verdict compares against.

    The *semantic* baseline, never the effective value: the layers above the
    project (env, CLI, pins) are other people's decisions for this process,
    and measuring a repository's tightening against them would let the
    environment move the fence the user set. This is also where the gate is
    read — ``project.config_enabled`` counts only from here, because reading
    it from any layer a project file feeds would let the layer authorise
    itself.
    """
    values: dict[str, Any] = {}
    for name, entry in specs.items():
        if not entry.persist or entry.key not in file_layers.user_yaml:
            continue
        coerced = _user_doc_verdict(entry, file_layers.user_yaml[entry.key])
        if coerced.usable:
            values[name] = coerced.value
    return Settings(**values)


_PROJECT_POLICY_REASONS = frozenset({CoerceReason.NOT_ALLOWED_IN_PROJECT, CoerceReason.LOOSENS_USER_BASELINE})
"""Rejections that mean "the project has no standing here", not "bad value"."""


def _project_verdict(
    name: str,
    entry: SettingSpec,
    raw: Any,
    *,
    baseline: Settings,
    baseline_values: Mapping[str, Any],
    eval_context: EvalContext,
) -> Coerced:
    """One project value through the whole gauntlet, in the fixed order.

    Policy first (a denied key's value is not worth a grammar opinion), then
    the coercer to a canonical value, then the direction check against the
    user baseline — on the *canonical* value, so ``60`` clamped to ``50`` is
    judged as the ``50`` that would actually apply. ``MISSING`` means the
    layer says nothing (``null`` and blanks are absences here, never values:
    the written-``null`` reading belongs to the document the panel writes,
    and nothing writes these).
    """
    if entry.project_merge is ProjectMerge.DENY or not entry.persist:
        return invalid(raw, CoerceReason.NOT_ALLOWED_IN_PROJECT)
    coerced = entry.coerce(raw)
    if not coerced.usable:
        return coerced
    if not _within_project_bounds(name, entry, coerced.value, baseline, baseline_values, eval_context):
        return invalid(raw, CoerceReason.LOOSENS_USER_BASELINE)
    return coerced


def _within_project_bounds(
    name: str,
    entry: SettingSpec,
    value: Any,
    baseline: Settings,
    baseline_values: Mapping[str, Any],
    eval_context: EvalContext,
) -> bool:
    """Whether *value* respects the field's merge direction over the baseline."""
    merge = entry.project_merge
    if merge is ProjectMerge.FREE:
        return True
    current = baseline_values[name]
    if merge is ProjectMerge.ENABLE_ONLY:
        return bool(value) or not bool(current)
    if merge is ProjectMerge.DISABLE_ONLY:
        return not bool(value) or bool(current)
    # TIGHTEN_ONLY — the spec's ``__post_init__`` guarantees the comparator.
    semantic = entry.semantic_value
    if semantic is None:
        raise RuntimeError("A tighten-only setting requires a semantic comparator.")
    return semantic(replace(baseline, **{name: value}), eval_context) <= semantic(baseline, eval_context)


def _project_layers(
    specs: Mapping[str, SettingSpec],
    file_layers: _FileLayers,
    eval_context: EvalContext,
) -> _ProjectLayers:
    """Audit the project document completely — phase one of the load."""
    yaml_path = file_layers.project_yaml_path
    if yaml_path is None:
        return _NO_PROJECT_LAYERS

    baseline = _default_user_baseline(specs, file_layers)
    if not baseline.project_config_enabled:
        keys = (*file_layers.project_yaml, *file_layers.project_yaml_unknown)
        if not keys:
            return _NO_PROJECT_LAYERS
        return _ProjectLayers(dormant=(DormantProjectConfig(path=yaml_path, keys=keys),))

    baseline_values = asdict(baseline)
    warnings: list[SettingsWarning] = []
    origin = SettingOrigin(layer=Source.PROJECT, path=yaml_path)

    # An unknown key in the *user* document is tolerated as a downgrade
    # artefact; in a project file it is simply not something a repository
    # may configure, and the warning has to name which file said so.
    warnings.extend(
        SettingsWarning(key=dotted, origin=origin, outcome=invalid("", CoerceReason.NOT_ALLOWED_IN_PROJECT))
        for dotted in file_layers.project_yaml_unknown
    )

    by_key = {entry.key: (name, entry) for name, entry in specs.items()}
    values: dict[str, Any] = {}
    rejected: dict[str, Coerced] = {}
    for dotted, raw in file_layers.project_yaml.items():
        name, entry = by_key[dotted]
        coerced = _project_verdict(
            name,
            entry,
            raw,
            baseline=baseline,
            baseline_values=baseline_values,
            eval_context=eval_context,
        )
        if coerced.status is CoerceStatus.MISSING:
            continue
        if not coerced.usable or coerced.status is CoerceStatus.CLAMPED:
            warnings.append(SettingsWarning(key=dotted, origin=origin, outcome=coerced))
        if coerced.usable:
            values[name] = coerced.value
        elif coerced.reason not in _PROJECT_POLICY_REASONS:
            rejected[name] = coerced

    return _ProjectLayers(values=values, rejected=rejected, warnings=tuple(warnings))


def _layers(
    name: str,
    entry: SettingSpec,
    pins: Mapping[str, Any],
    source: Mapping[str, str],
    real_process: bool,
    file_layers: _FileLayers | None,
    project: _ProjectLayers,
) -> Iterator[tuple[Coerced, SettingOrigin]]:
    """Yield each layer's verdict for one field, **highest precedence first**.

    Resolving downwards is what makes a rejected value local to its own layer.
    Sweeping upwards instead — the shape this had while there was only one
    layer — a high layer's typo could seal a key that a pin above it had
    already answered, so the pin would be recorded as the effective source
    while the value in force was the built-in default.

    A generator, so a layer that nothing asks about costs nothing to skip.

    Note what this is *not*: it is not where a file gets read. A settings
    document is parsed once, whole, for every key it mentions — one pinned
    field cannot excuse the loader from reading the rest of the file — and the
    project layer's values must be policy-checked and their rejections reported
    even when a higher layer shadows them. The reading happens in
    :func:`_read_file_layers` before this function is ever called, and it keeps
    the one job it has: picking the winner.
    """
    if name in pins:
        yield _pinned(entry, pins[name]), SettingOrigin(layer=Source.SESSION)

    if entry.env is not None:
        # The model-profile pointer is the one environment name that is *not* a
        # bootstrap fact: activation, ``--model`` and session restore write it
        # mid-run, and the reload that immediately follows has to see what was
        # just written rather than the value the process started with. Its
        # value and its origin are taken in a single read, because they are one
        # state: fetched separately, a write landing between the two hands this
        # layer one writer's value under another writer's origin, and naming
        # the writer is the only thing the layer adds over the carrier. An
        # unregistered value is a real shell export and stays ``ENV``. Injected
        # test environments are hermetic, so the process-global registration
        # only speaks for the process environment.
        if real_process and entry.env in POINTER_ENV_NAMES:
            raw, registered = get_model_pointer()
            origin = registered if registered is not None else SettingOrigin(layer=Source.ENV)
        else:
            raw = source.get(entry.env, "")
            origin = SettingOrigin(layer=Source.ENV)
        # Presence is read off the value because the coercers already fold a
        # blank string into ``MISSING``; an exported empty name never spoke.
        if raw:
            coerced = (entry.env_coerce or entry.coerce)(raw)
            if coerced.status is not CoerceStatus.MISSING:
                yield coerced, origin

    if file_layers is None:
        return

    if entry.env is not None and (folded := canonical_env_name(entry.env)) in file_layers.user_env:
        coerced = (entry.env_coerce or entry.coerce)(file_layers.user_env[folded])
        if coerced.status is not CoerceStatus.MISSING:
            yield coerced, SettingOrigin(layer=Source.USER_ENV, path=file_layers.user_env_path)

    # The project layer contributes what phase one already judged, at the
    # verdict it judged it — a shadowed project value was audited there, so
    # nothing here may warn a second time. Accepted values arrive canonical;
    # a value invalid arrives as its ``INVALID`` verdict, because the walk is
    # where ``SAFE_DEFAULT`` turns "this layer wrote garbage on a dangerous
    # key" into a seal, and a verdict that never enters the walk can never
    # seal (the exact uncap-the-backstop path the policy exists to close).
    if name in project.values:
        yield (
            Coerced(status=CoerceStatus.VALID, value=project.values[name]),
            SettingOrigin(layer=Source.PROJECT, path=file_layers.project_yaml_path),
        )
    elif name in project.rejected:
        yield project.rejected[name], SettingOrigin(layer=Source.PROJECT, path=file_layers.project_yaml_path)

    # ``persist=False`` marks a field runtime-only: :func:`persist` refuses to
    # write it, and for the same reason a hand-written spelling does not load.
    if entry.persist and entry.key in file_layers.user_yaml:
        coerced = _user_doc_verdict(entry, file_layers.user_yaml[entry.key])
        if coerced.status is not CoerceStatus.MISSING:
            yield coerced, SettingOrigin(layer=Source.USER, path=file_layers.user_yaml_path)


def _pinned(entry: SettingSpec, value: Any) -> Coerced:
    """Accept one caller-supplied value for *entry*, or raise.

    Pins come from our own code — a restored session, a frontend's own default
    — so a wrong one is a bug no user can fix and no warning can route around.
    They are also the one layer that reaches ``Settings`` without passing a
    coercer, which is why the check has to be made here rather than trusted:
    ``tools.result.ceiling_tokens`` declares ``SAFE_DEFAULT`` precisely so a bad
    value cannot uncap the backstop, and a pinned ``-1`` would do exactly that,
    because the consumer reads any non-positive ceiling as "no ceiling".

    Two checks, because neither alone is enough. :func:`kind_accepts` answers
    the type question a coercer cannot (``None`` and ``""`` are real values for
    the fields that declare them, not blanks to be dropped); the coercer answers
    the domain question a type cannot. A value the coercer has nothing to say
    about — those spellings, which come back ``MISSING`` — is taken verbatim.

    ``Kind.ENUM`` is the exception, because it is the one kind whose domain is
    written down in full: ``choices`` *is* the domain, so a value outside it is
    outside the domain by construction, and there is no unset spelling left over
    to mean anything. Everywhere else the blank carries meaning the coercer
    cannot see — ``model_profile=""`` is "no pinned profile", and ACP's
    ``ask_user_timeout_seconds=None`` is "no timeout, the client owns timing" —
    so those stay verbatim. Without the exception a blank sails past the
    canonical check below and lands in ``Settings`` as a value no ``choices``
    list contains, and, because a rejected value is what seals a dangerous key,
    it takes the seal with it: pinning ``default_approval_mode=""`` over an
    ``ENV`` value that failed its coercer leaves the key holding ``""``,
    unsealed, credited to the pin.
    """
    if not kind_accepts(entry.kind, value):
        msg = f"{entry.key}: {type(value).__name__} is not a valid {entry.kind.name} value"
        raise TypeError(msg)
    coerced = entry.coerce(value)
    if coerced.status is CoerceStatus.MISSING:
        if entry.kind is Kind.ENUM:
            # ``choices`` may be a ChoiceProvider, whose members only a higher
            # layer can enumerate — name it rather than pretending to list it.
            offered = list(entry.choices) if isinstance(entry.choices, tuple) else entry.choices
            msg = f"{entry.key}: {value!r} is not one of {offered}"
            raise TypeError(msg)
        return Coerced(status=CoerceStatus.VALID, value=value)
    if coerced.status is not CoerceStatus.VALID:
        msg = f"{entry.key}: {value!r} is not a valid value ({coerced.status.name.lower()})"
        raise TypeError(msg)
    if coerced.value != value:
        # A pin that only survives normalisation is still a caller passing the
        # wrong thing: it would be stored as one value and echoed back to the
        # panel as another, and the caller would never learn which won.
        msg = f"{entry.key}: {value!r} is not canonical; pass {coerced.value!r}"
        raise TypeError(msg)
    return coerced


_APPROVAL_MODE_KEY: Final = "approval.default_mode"

_KIND_REASONS: Final = {
    Kind.BOOL: CoerceReason.EXPECTED_BOOL,
    Kind.INT: CoerceReason.EXPECTED_INT,
    Kind.OPTIONAL_INT: CoerceReason.EXPECTED_INT,
    Kind.FLOAT: CoerceReason.EXPECTED_NUMBER,
    Kind.TEXT: CoerceReason.EXPECTED_TEXT,
    Kind.ENUM: CoerceReason.NOT_A_CHOICE,
    Kind.PATH: CoerceReason.EXPECTED_TEXT,
}
"""How to word the rejection when a value's *type* is what disqualified it."""


@dataclass(frozen=True, slots=True)
class PersistResult:
    """What one :func:`persist` call did, in full.

    Mirrors :class:`LoadedSettings`' contract: a caller gets the outcome and
    the rejections together, and the panel that ignores ``rejected`` is the
    panel that cannot say why a field snapped back to its old value.
    """

    written: Mapping[str, Any]
    """Dotted key → the canonical value actually stored (post-clamp)."""

    rejected: Mapping[str, Coerced]
    """Dotted key → the verdict that kept the whole batch out of the file."""

    @property
    def ok(self) -> bool:
        return not self.rejected


def persist(
    updates: Mapping[str, Any],
    *,
    remove: Iterable[str] = (),
    lock_timeout: float = LOCK_TIMEOUT_SECONDS,
) -> PersistResult:
    """Write dotted-key values into the user settings document, all or nothing.

    Validation happens first, against the same coercers the loader applies on
    the way back in, and one rejection keeps the entire batch out of the file:
    a partial write would leave the document describing a state no caller ever
    asked for. Clamped values are stored at their clamped canonical — the file
    holds what will be in effect, not what was typed. ``remove`` deletes keys;
    a batch of only removals is a legal write.

    An unknown key or a runtime-only (``persist=False``) key raises instead of
    rejecting: both are our-code bugs no user input can produce, exactly like
    a bad pin.

    ``approval.default_mode`` is the one key with a store-time rule of its
    own: ``bypass`` is a per-launch decision, so persisting it downgrades to
    ``auto`` rather than arming every future session.
    """
    specs = specs_by_field(Settings)
    by_key = {entry.key: entry for entry in specs.values()}
    removals = tuple(dict.fromkeys(remove))
    path = user_settings_path()

    unknown = sorted(key for key in {*updates, *removals} if key not in by_key)
    if unknown:
        msg = f"Unknown settings keys: {', '.join(unknown)}"
        raise TypeError(msg)
    runtime_only = sorted(key for key in {*updates, *removals} if not by_key[key].persist)
    if runtime_only:
        msg = f"Runtime-only settings keys cannot be persisted: {', '.join(runtime_only)}"
        raise TypeError(msg)

    written: dict[str, Any] = {}
    rejected: dict[str, Coerced] = {}
    for key, value in updates.items():
        entry = by_key[key]
        coerced = entry.coerce(value)
        if coerced.status is CoerceStatus.MISSING:
            # The unset spellings are real values for the fields that declare
            # them (``""`` is "no pinned profile"), same as for a pin — and
            # ``ENUM`` is the same exception, its domain being written in full.
            if kind_accepts(entry.kind, value) and entry.kind is not Kind.ENUM:
                written[key] = value
            else:
                rejected[key] = _kind_rejection(entry, value)
            continue
        if coerced.status is CoerceStatus.INVALID:
            rejected[key] = coerced
            continue
        written[key] = coerced.value

    if written.get(_APPROVAL_MODE_KEY) == "bypass":
        written[_APPROVAL_MODE_KEY] = "auto"

    if rejected:
        return PersistResult(written={}, rejected=rejected)
    if written or removals:
        update_yaml_doc(path, lambda doc: apply_settings_patch(doc, written, removals), lock_timeout=lock_timeout)
    return PersistResult(written=written, rejected={})


def _kind_rejection(entry: SettingSpec, value: Any) -> Coerced:
    """Word the refusal of a value whose type no coercer had a verdict for."""
    if entry.kind is Kind.ENUM:
        choices = entry.choices if isinstance(entry.choices, tuple) else ()
        return invalid(value, CoerceReason.NOT_A_CHOICE, choices=choices)
    return invalid(value, _KIND_REASONS[entry.kind])
