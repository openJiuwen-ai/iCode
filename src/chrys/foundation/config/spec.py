# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Per-field settings metadata, declared on the dataclass field itself.

``Settings`` stays a plain dataclass — no registry to keep in sync, no schema
DSL, and ``ty`` still checks every field's type. Everything the loader, the
settings panel and the project-layer policy need rides along in
``field(metadata=spec(...))``:

    theme: str = field(
        default=DEFAULT_THEME,
        metadata=spec(key="ui.theme", env="CHRYS_THEME", apply=Apply.LIVE, ...),
    )

Adding a new setting is then one field declaration, which is the test of
whether this design holds up when the remaining hidden parameters land.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, fields
from enum import Enum, auto
from typing import TYPE_CHECKING, Any

from chrys.foundation.config.coercion import Coercer
from chrys.foundation.config.context import EvalContext

if TYPE_CHECKING:
    from pathlib import Path

    from _typeshed import DataclassInstance

    from chrys.foundation.i18n.messages import MessageDef

METADATA_KEY = "chrys_setting"
"""Key under which :class:`SettingSpec` rides in ``field(metadata=...)``."""


class Source(Enum):
    """Which layer a value came from, ordered low to high precedence."""

    DEFAULT = auto()
    """Built-in dataclass default."""

    USER = auto()
    """``~/.chrys/settings.yaml`` — the only layer the settings panel writes."""

    PROJECT = auto()
    """``<root>/.chrys/settings.yaml`` — gated by the project trust domain."""

    ENV = auto()
    """The real process environment, snapshotted before bootstrap."""

    CLI = auto()
    """A command-line argument, whatever channel carries it."""

    PROCESS_RUNTIME = auto()
    """Set at runtime for this process (e.g. activating a model profile).

    Above ``CLI``: activating a profile mid-session is a later decision than
    the ``--model`` the process started with. Below ``SESSION``: a per-host pin
    beats the process-wide pointer, or one ACP session's pin would leak into
    every other session in the process.
    """

    SESSION = auto()
    """A per-session pin or a restored session's saved value."""

    RUNTIME = auto()
    """A live change made in this process: the theme, locale or approval mode
    the user just picked.

    Nothing loads *from* this layer — it exists only on the in-memory
    ``Settings`` instance. The ``persist_*`` helpers do write the same choice
    to a lower layer, but that is for the next process; recording it here is
    what keeps provenance honest about who won in *this* one, which a lower
    layer could not claim while a higher one is set.
    """


FILE_SOURCES = frozenset({Source.USER, Source.PROJECT})
"""The layers backed by a file on disk, which must name it in their origin."""


@dataclass(frozen=True, slots=True)
class SettingOrigin:
    """Where an effective value actually came from.

    The layer alone cannot answer the panel's question. ``PROJECT`` says "some
    ``<root>/.chrys/settings.yaml``", but a session that changed workspace has
    had more than one root; without the path the panel can neither show the
    real file nor offer to remove the right override.
    """

    layer: Source
    path: Path | None = None
    """The file that supplied the value. ``None`` for the non-file layers."""

    def __post_init__(self) -> None:
        # An invariant rather than a convention: a file layer that forgot its
        # path degrades silently into the bare-layer provenance this type
        # exists to replace, and nothing downstream can detect the loss.
        if self.layer in FILE_SOURCES and self.path is None:
            msg = f"{self.layer.name} origins must name the file they came from"
            raise ValueError(msg)
        if self.layer not in FILE_SOURCES and self.path is not None:
            msg = f"{self.layer.name} has no backing file, so it cannot carry a path"
            raise ValueError(msg)


class Kind(Enum):
    """The editor the settings panel should render for a field."""

    BOOL = auto()
    INT = auto()
    OPTIONAL_INT = auto()
    """``int | None``, where clearing the field means "use the default"."""

    FLOAT = auto()
    TEXT = auto()
    ENUM = auto()
    PATH = auto()


_KIND_TYPES: Mapping[Kind, tuple[type, ...]] = {
    Kind.BOOL: (bool,),
    Kind.INT: (int,),
    Kind.OPTIONAL_INT: (int,),
    Kind.FLOAT: (int, float),
    Kind.TEXT: (str,),
    Kind.ENUM: (str,),
    Kind.PATH: (str,),
}


def kind_accepts(kind: Kind, value: object) -> bool:
    """Whether *value* is a legal **in-memory** value for a field of *kind*.

    The check a coercer cannot make. A coercer normalises what a user *typed*
    and reports "say nothing" for blanks; an already-typed value supplied by
    our own code is a different thing, and the two disagree exactly where it
    matters: ``None`` is the real value of an unset ``OPTIONAL_INT`` and ``""``
    is the real value of an unset ``TEXT``, but a coercer calls both ``MISSING``
    and would drop them.

    ``bool`` is rejected everywhere except :attr:`Kind.BOOL` because it is an
    ``int`` subclass, so ``True`` would otherwise pass as a perfectly good
    integer setting.
    """
    if value is None:
        return kind is Kind.OPTIONAL_INT
    if isinstance(value, bool):
        return kind is Kind.BOOL
    return isinstance(value, _KIND_TYPES[kind])


class ChoiceProvider(Enum):
    """Identifier for a choice set only a higher layer can enumerate.

    ``Settings`` lives in ``foundation`` and the layering test forbids it from
    importing ``app``, so a field whose values are registered up there (themes)
    cannot hold a callable or a literal tuple. It holds this identifier, and the
    panel resolves it at render time.
    """

    THEMES = auto()


class Apply(Enum):
    """When a changed value actually takes effect."""

    LIVE = auto()
    """A real hot-apply path exists and runs on save."""

    RELOAD = auto()
    """Takes effect when the agent is rebuilt via ``SettingsReload``."""

    RESTART = auto()
    """Read once per process; only a restart picks it up."""


class Risk(Enum):
    """How much damage a wrong value can do — drives panel confirmations."""

    SAFE = auto()
    CAUTION = auto()
    DANGEROUS = auto()


class InvalidPolicy(Enum):
    """What a rejected value means for the layers *below* the one that wrote it."""

    FALL_THROUGH = auto()
    """Default. The layer abstains and the next one down decides.

    Right for a preference: a typo in ``CHRYS_THEME`` should leave the theme
    the user saved, not reset it.
    """

    SAFE_DEFAULT = auto()
    """Resolution stops at the built-in default; no lower layer may supply one.

    Right where falling through *reverses a safety posture*. Under
    ``FALL_THROUGH``, a user who once persisted ``log.raw_http_capture: true``
    and then starts with ``CHRYS_DEBUG_LLM_RAW_HTTP_LOG=fales`` gets capture
    **on** — the typo they wrote to turn it off instead re-enabled writing API
    keys and full prompts to disk. Every field carrying this policy has a
    fail-closed built-in default, which is what makes it a safe landing spot.
    """


class ProjectMerge(Enum):
    """Whether, and in which direction, a repository may set this field.

    The project layer may only change *engineering behaviour*, never user
    preference, credentials, storage, telemetry — and never in the direction
    of loosening a safety or cost bound the user already chose.
    """

    DENY = auto()
    """Default. A project value is discarded with a warning."""

    FREE = auto()
    """Any value the coercer accepts."""

    TIGHTEN_ONLY = auto()
    """Only values at least as strict as the user baseline (needs a comparator)."""

    ENABLE_ONLY = auto()
    """Only ``false`` -> ``true``."""

    DISABLE_ONLY = auto()
    """Only ``true`` -> ``false``."""


@dataclass(frozen=True, slots=True)
class SettingSpec:
    """Everything about a settings field that is not its type or default."""

    key: str
    """Dotted key: the YAML path and the panel's stable identifier."""

    coerce: Coercer
    """Normalises a raw value from any layer. Never raises."""

    apply: Apply

    group: str
    """Panel section this field belongs to, e.g. ``"ui"``."""

    kind: Kind

    label: MessageDef | None = None
    """Localised panel label, declared with ``msg()`` so it is extractable.

    The settings panel renders it, so every persisted field must declare one
    (``__post_init__`` enforces it). Only a runtime-only field — ``persist``
    false, which the panel never shows — may leave it ``None``.
    """

    env: str | None = None
    """Legacy ``CHRYS_*`` name. Read-only compatibility alias, not a target."""

    env_coerce: Coercer | None = None
    """Coercer for the ``env`` alias when its grammar differs from the key's.

    Needed where the legacy variable is not simply the key spelled another way.
    ``CHRYS_HISTORY_DISABLE`` is the case that forces it: the key is positive
    (``history.prompt.enabled``) and the variable is negative, so one coercer
    cannot serve both. Defaults to ``coerce``.
    """

    risk: Risk = Risk.SAFE

    invalid_policy: InvalidPolicy = InvalidPolicy.FALL_THROUGH
    """Where resolution goes when a layer's value is rejected."""

    project_merge: ProjectMerge = ProjectMerge.DENY

    persist: bool = True
    """``False`` marks a runtime-only field the panel must not render or write."""

    choices: tuple[str, ...] | ChoiceProvider | None = None
    """Closed value set: either literal values, or an identifier to resolve."""

    semantic_value: Callable[[Any, EvalContext], Any] | None = None
    """Maps the whole ``Settings`` to the quantity ``TIGHTEN_ONLY`` compares.

    Takes the settings and the :class:`EvalContext`, not the bare field value:
    ``None`` means "use the frontend policy default" (7 or 15, known only from
    the context), and ``0`` can mean "no limit", which is the *largest* value
    rather than the smallest. Comparing raw fields gets both backwards.
    """

    def __post_init__(self) -> None:
        if self.kind is Kind.ENUM and self.choices is None:
            msg = f"{self.key}: Kind.ENUM requires choices"
            raise ValueError(msg)
        if self.project_merge is ProjectMerge.TIGHTEN_ONLY and self.semantic_value is None:
            msg = f"{self.key}: TIGHTEN_ONLY requires a semantic_value comparator"
            raise ValueError(msg)
        if self.risk is Risk.DANGEROUS and self.invalid_policy is not InvalidPolicy.SAFE_DEFAULT:
            # Declaring a field dangerous is precisely the statement that its
            # wrong value costs more than its absence, so it may not silently
            # inherit a lower layer's value when a higher one is rejected.
            msg = f"{self.key}: Risk.DANGEROUS requires InvalidPolicy.SAFE_DEFAULT"
            raise ValueError(msg)
        if self.persist and self.label is None:
            msg = f"{self.key}: persisted settings require a label"
            raise ValueError(msg)


def spec(
    *,
    key: str,
    coerce: Coercer,
    apply: Apply,
    group: str,
    kind: Kind,
    label: MessageDef | None = None,
    env: str | None = None,
    env_coerce: Coercer | None = None,
    risk: Risk = Risk.SAFE,
    invalid_policy: InvalidPolicy = InvalidPolicy.FALL_THROUGH,
    project_merge: ProjectMerge = ProjectMerge.DENY,
    persist: bool = True,
    choices: tuple[str, ...] | ChoiceProvider | None = None,
    semantic_value: Callable[[Any, EvalContext], Any] | None = None,
) -> Mapping[str, SettingSpec]:
    """Build the ``field(metadata=...)`` mapping for one setting."""
    return {
        METADATA_KEY: SettingSpec(
            key=key,
            coerce=coerce,
            apply=apply,
            group=group,
            kind=kind,
            label=label,
            env=env,
            env_coerce=env_coerce,
            risk=risk,
            invalid_policy=invalid_policy,
            project_merge=project_merge,
            persist=persist,
            choices=choices,
            semantic_value=semantic_value,
        )
    }


def spec_of(field_metadata: Mapping[str, Any]) -> SettingSpec | None:
    """Return the spec carried by a dataclass field's metadata, if any."""
    value = field_metadata.get(METADATA_KEY)
    return value if isinstance(value, SettingSpec) else None


def specs_by_field(cls: type[DataclassInstance]) -> dict[str, SettingSpec]:
    """Map ``{attribute name: spec}`` for every annotated field of *cls*."""
    result: dict[str, SettingSpec] = {}
    for entry in fields(cls):
        found = spec_of(entry.metadata)
        if found is not None:
            result[entry.name] = found
    return result


def specs_by_key(cls: type[DataclassInstance]) -> dict[str, SettingSpec]:
    """Map ``{dotted key: spec}`` for every annotated field of *cls*."""
    return {found.key: found for found in specs_by_field(cls).values()}


def field_names_by_key(cls: type[DataclassInstance]) -> dict[str, str]:
    """Map ``{dotted key: attribute name}`` for every annotated field of *cls*."""
    return {found.key: name for name, found in specs_by_field(cls).items()}
