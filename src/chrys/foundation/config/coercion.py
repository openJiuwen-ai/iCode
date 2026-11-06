# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Value coercion for settings, shared by every configuration layer.

One coercer per field, owned by the field's ``SettingSpec`` — so parsing,
clamping and fallback live in exactly one place instead of being re-derived by
each reader (dataclass default, ACP validator, headless warning path, …).

Coercers take a **raw** value from any layer and normalise it to one canonical
value. YAML hands over already-typed values (``true``, ``50``); the process
environment supplies strings. Both must land on the same result, so
the grammar below is defined once and every coercer accepts the native type
and the string spelling of it.

A coercer never raises and never invents a fallback: it reports what the raw
value *is*.

* ``MISSING`` — this layer says nothing about the field; the loader moves to
  the next layer down. Absent keys, blank strings and YAML ``null`` all land
  here.
* ``VALID`` / ``CLAMPED`` — a usable canonical value, the latter adjusted to a
  declared bound.
* ``INVALID`` — unusable. The layer contributes nothing, exactly as if it were
  ``MISSING``, but a record is produced. What to do with that record is the
  loader's call: user-visible for a project file (which must be able to say
  "this key of this file was rejected"), a log line for the historical env
  behaviour.

Encoding "invalid" as *the field default* — the shape this module had first —
makes the project layer undecidable: the loader cannot tell a repository that
legitimately set the default value from one whose garbage was silently
swallowed, and would label the default as coming from the project.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import Enum, auto
from typing import Any

TRUTHY = frozenset({"1", "true", "yes", "on"})
FALSY = frozenset({"0", "false", "no", "off"})
"""The complete boolean grammar. Any other spelling is invalid, not false."""


class CoerceStatus(Enum):
    """Outcome of coercing one raw value."""

    MISSING = auto()
    VALID = auto()
    CLAMPED = auto()
    INVALID = auto()


class CoerceReason(Enum):
    """Why a value was refused or adjusted, in no particular language.

    An English sentence here would be the one part of a settings warning that
    cannot be translated: the warning shell is a catalog entry, but a free-text
    detail composed down in ``foundation`` would arrive already written and
    render as "Ignoring CHRYS_SESSION_TITLE_AUTO=nonsense" followed by the
    English "expected a boolean" in the middle of a Chinese sentence.
    A member is picked instead, and the composer that owns the user-facing copy
    binds it to a message with the rest of the sentence.
    """

    EXPECTED_BOOL = auto()
    EXPECTED_INT = auto()
    EXPECTED_NON_NEGATIVE_INT = auto()
    EXPECTED_NUMBER = auto()
    EXPECTED_FINITE_NUMBER = auto()
    EXPECTED_TEXT = auto()
    NOT_A_CHOICE = auto()
    EXPECTED_JSON_ARRAY = auto()
    EXPECTED_WEB_ORIGIN = auto()
    """An address that is not a bare http(s) origin: it has a path, credentials or no scheme."""

    EXPECTED_ENDPOINT_GRANT = auto()
    BELOW_MINIMUM = auto()
    ABOVE_MAXIMUM = auto()
    NOT_A_DIRECTORY = auto()
    """Reserved for the operational checks a pure coercer cannot make."""

    NOT_ALLOWED_IN_PROJECT = auto()
    """The key is outside the project-merge whitelist (or not a key at all).

    Not produced by a coercer: the project trust domain rejects the key before
    any value question is asked, and rides the verdict type so the one warning
    pipeline carries it.
    """

    LOOSENS_USER_BASELINE = auto()
    """A whitelisted key's value moves in the direction the policy forbids.

    Also policy, not grammar: the value coerced fine, but ``TIGHTEN_ONLY`` /
    ``ENABLE_ONLY`` / ``DISABLE_ONLY`` compare it against the user's own
    ``DEFAULT + USER`` baseline, and it landed on the loosening side.
    """


@dataclass(frozen=True, slots=True)
class Coerced:
    """A coercion outcome.

    ``value`` is meaningful only for ``VALID`` and ``CLAMPED``. ``raw`` is the
    offending value as written; callers rendering it for a sensitive field must
    redact it themselves. ``reason``, ``limit`` and ``choices`` carry everything
    a message needs, so no caller has to parse a sentence back apart.
    """

    status: CoerceStatus
    value: Any = None
    raw: str = ""
    reason: CoerceReason | None = None
    limit: float | None = None
    choices: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        # The composer maps reasons to sentences exhaustively, so a verdict that
        # arrives without one has no way to be shown to anyone.
        if self.status in (CoerceStatus.INVALID, CoerceStatus.CLAMPED) and self.reason is None:
            msg = f"{self.status.name} must name the reason it was rejected or adjusted"
            raise ValueError(msg)

    @property
    def usable(self) -> bool:
        """Whether this layer contributes ``value``."""
        return self.status in (CoerceStatus.VALID, CoerceStatus.CLAMPED)


Coercer = Callable[[object], Coerced]

MISSING = Coerced(status=CoerceStatus.MISSING)


def render_raw(raw: object) -> str:
    """Render *raw* for a warning, without ever raising.

    Public because a field may need its own coercer (a legacy alias whose
    grammar differs from its key's), and such a coercer reaching for ``str()``
    or ``repr()`` directly is how the no-raise contract gets broken one field
    at a time.
    """
    if isinstance(raw, str):
        return raw
    try:
        return str(raw)
    except Exception:
        # Two different failures, one answer. ``str()`` of an integer past
        # ``sys.get_int_max_str_digits()`` raises ``ValueError``, and YAML
        # happily parses one; a raw value that reached us from an unvetted
        # source can carry any ``__str__`` at all. The declared input type is
        # ``object``, so narrowing this catch would make the no-raise contract
        # true only for the values we happened to think of.
        return f"<unrenderable {type(raw).__name__}>"


def invalid(raw: object, reason: CoerceReason, *, choices: tuple[str, ...] = ()) -> Coerced:
    """Build an ``INVALID`` outcome with a safely rendered ``raw``."""
    return Coerced(status=CoerceStatus.INVALID, raw=render_raw(raw), reason=reason, choices=choices)


def _clamped(raw: object, value: Any, reason: CoerceReason, limit: float) -> Coerced:
    return Coerced(status=CoerceStatus.CLAMPED, value=value, raw=render_raw(raw), reason=reason, limit=limit)


def _valid(value: Any) -> Coerced:
    return Coerced(status=CoerceStatus.VALID, value=value)


def bool_coercer() -> Coercer:
    """Coerce to bool.

    Accepts native ``bool``, the integers ``0``/``1`` (a common YAML flag
    spelling), and the string spellings in :data:`TRUTHY` / :data:`FALSY`.
    Everything else is invalid.

    Note this is stricter than the historical env reader, which treated every
    non-truthy string as false — so ``CHRYS_SESSION_TITLE_AUTO=nonsense``
    silently turned the feature off. Garbage now falls through to the layer
    below instead of meaning "off".
    """

    def coerce(raw: object) -> Coerced:
        if raw is None:
            return MISSING
        if isinstance(raw, bool):
            return _valid(raw)
        if isinstance(raw, str):
            normalized = raw.strip().lower()
            if not normalized:
                return MISSING
            if normalized in TRUTHY:
                return _valid(True)
            if normalized in FALSY:
                return _valid(False)
            return invalid(raw, CoerceReason.EXPECTED_BOOL)
        if isinstance(raw, int) and raw in (0, 1):
            return _valid(bool(raw))
        return invalid(raw, CoerceReason.EXPECTED_BOOL)

    return coerce


def _to_int(raw: object) -> int | Coerced:
    """Parse *raw* as an int, or return the failing :class:`Coerced`."""
    if raw is None:
        return MISSING
    if isinstance(raw, bool):
        # ``bool`` is an ``int`` subclass; accepting it would let ``true`` mean 1.
        return invalid(raw, CoerceReason.EXPECTED_INT)
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str):
        normalized = raw.strip()
        if not normalized:
            return MISSING
        try:
            return int(normalized)
        except ValueError:
            return invalid(raw, CoerceReason.EXPECTED_INT)
    return invalid(raw, CoerceReason.EXPECTED_INT)


def int_coercer(
    *,
    minimum: int | None = None,
    maximum: int | None = None,
    non_positive: int | None = None,
    zero: int | None = None,
    reject_negative: bool = False,
) -> Coercer:
    """Coerce to int, with the sentinel and bound policy the field declares.

    The knobs exist because "out of range" already means three different things
    across the existing variables, and flattening them would change behaviour:
    ``CHRYS_WORKSPACE_MRU_MAX_ENTRIES=-5`` disables the MRU, the same value on
    ``CHRYS_WORKSPACE_CHANGE_NOTICE_MAX_ENTRIES`` clamps up to 1, and on
    ``CHRYS_MAX_TRANSIENT_RETRIES`` it is rejected outright.

    Args:
        minimum: Values below this are raised to it.
        maximum: Values above this are lowered to it.
        non_positive: When set, any value ``<= 0`` becomes this, bypassing the
            bounds — these fields spell "disabled" or "no limit" as ``0`` or as
            any negative number, so clamping would invert their meaning.
        zero: When set, exactly ``0`` becomes this, bypassing the bounds.
        reject_negative: Treat negative values as invalid rather than clamping
            them. Checked first, so it composes with *zero*.
    """

    def coerce(raw: object) -> Coerced:
        parsed = _to_int(raw)
        if isinstance(parsed, Coerced):
            return parsed
        if reject_negative and parsed < 0:
            return invalid(raw, CoerceReason.EXPECTED_NON_NEGATIVE_INT)
        if zero is not None and parsed == 0:
            return _valid(zero)
        if non_positive is not None and parsed <= 0:
            return _valid(non_positive)
        if minimum is not None and parsed < minimum:
            return _clamped(raw, minimum, CoerceReason.BELOW_MINIMUM, minimum)
        if maximum is not None and parsed > maximum:
            return _clamped(raw, maximum, CoerceReason.ABOVE_MAXIMUM, maximum)
        return _valid(parsed)

    return coerce


def optional_int_coercer(*, non_positive_means_none: bool = True) -> Coercer:
    """Coerce to ``int | None`` where a non-positive value spells "no limit"."""

    def coerce(raw: object) -> Coerced:
        parsed = _to_int(raw)
        if isinstance(parsed, Coerced):
            return parsed
        if non_positive_means_none and parsed <= 0:
            return _valid(None)
        return _valid(parsed)

    return coerce


def float_coercer(*, minimum: float | None = None, maximum: float | None = None) -> Coercer:
    """Coerce to a **finite** float with optional clamping.

    NaN and infinities are rejected rather than clamped: NaN compares false
    against every bound, so it would sail through both checks and then silently
    disable whatever threshold the field guards.
    """

    def coerce(raw: object) -> Coerced:
        if raw is None:
            return MISSING
        if isinstance(raw, bool):
            return invalid(raw, CoerceReason.EXPECTED_NUMBER)
        candidate: int | float | str
        if isinstance(raw, int | float):
            candidate = raw
        elif isinstance(raw, str):
            normalized = raw.strip()
            if not normalized:
                return MISSING
            candidate = normalized
        else:
            return invalid(raw, CoerceReason.EXPECTED_NUMBER)

        try:
            value = float(candidate)
        except ValueError, OverflowError:
            # ``OverflowError`` is the native-int path: YAML parses an
            # arbitrarily long decimal into a Python ``int``, which ``float()``
            # then refuses. A hand-edited YAML file must not crash the load.
            return invalid(raw, CoerceReason.EXPECTED_NUMBER)

        if not math.isfinite(value):
            return invalid(raw, CoerceReason.EXPECTED_FINITE_NUMBER)
        if minimum is not None and value < minimum:
            return _clamped(raw, minimum, CoerceReason.BELOW_MINIMUM, minimum)
        if maximum is not None and value > maximum:
            return _clamped(raw, maximum, CoerceReason.ABOVE_MAXIMUM, maximum)
        return _valid(value)

    return coerce


def choice_coercer(*, choices: Iterable[str]) -> Coercer:
    """Coerce to one of *choices*, matched case-insensitively.

    The **declared spelling** is returned, not the user's — ``zh-Hans`` stays
    ``zh-Hans`` however it was typed, so downstream identity comparisons against
    the canonical constant keep working.
    """
    canonical: dict[str, str] = {}
    for choice in choices:
        folded = choice.casefold()
        if folded in canonical:
            msg = f"Choices collide under case folding: {canonical[folded]!r} and {choice!r}"
            raise ValueError(msg)
        canonical[folded] = choice

    def coerce(raw: object) -> Coerced:
        if raw is None:
            return MISSING
        if not isinstance(raw, str):
            return invalid(raw, CoerceReason.EXPECTED_TEXT)
        normalized = raw.strip()
        if not normalized:
            return MISSING
        found = canonical.get(normalized.casefold())
        if found is None:
            return invalid(raw, CoerceReason.NOT_A_CHOICE, choices=tuple(sorted(canonical.values())))
        return _valid(found)

    return coerce


def text_coercer(*, strip: bool = True) -> Coercer:
    """Coerce to a plain string. Empty means "unset", not "empty string"."""

    def coerce(raw: object) -> Coerced:
        if raw is None:
            return MISSING
        if not isinstance(raw, str):
            return invalid(raw, CoerceReason.EXPECTED_TEXT)
        value = raw.strip() if strip else raw
        if not value.strip():
            return MISSING
        return _valid(value)

    return coerce
