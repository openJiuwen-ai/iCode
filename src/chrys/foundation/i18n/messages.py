# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Immutable locale-neutral message definitions and bound references."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Iterable
from dataclasses import InitVar, dataclass

from chrys.foundation.i18n.formatting import (
    format_message,
    format_template,
    has_visible_content,
    parse_placeholder_names,
    sanitize_legacy_block,
    sanitize_legacy_scalar,
    validate_authored_template,
)
from chrys.foundation.platform.files import surrogate_safe_text

_CONSTRUCTION_TOKEN = object()
_MESSAGE_KEY_RE = re.compile(r"[a-z0-9_]+(?:\.[a-z0-9_]+)+\Z")


class _UnsetCount:
    """Sentinel distinguishing an omitted ``count`` from an explicit ``None``."""


_UNSET_COUNT = _UnsetCount()


@dataclass(frozen=True, slots=True)
class DisplayPath:
    """A filesystem path explicitly selected for safe display conversion."""

    value: str

    def __init__(self, value: str | os.PathLike[str]) -> None:
        raw_value = os.fspath(value)
        if type(raw_value) is not str:
            raise TypeError("DisplayPath requires a text path.")
        object.__setattr__(self, "value", raw_value)


@dataclass(frozen=True, slots=True)
class DisplayBlock:
    """Verbatim display text whose LF characters must be preserved."""

    value: str

    def __post_init__(self) -> None:
        if type(self.value) is not str:
            raise TypeError("DisplayBlock requires a string.")


type MessageScalar = str | int
type _DisplaySequenceItem = MessageScalar | DisplayPath


@dataclass(frozen=True, slots=True)
class DisplaySequence:
    """An immutable flat sequence rendered with a fixed comma separator."""

    values: tuple[_DisplaySequenceItem, ...]

    def __init__(self, values: Iterable[_DisplaySequenceItem] = ()) -> None:
        if isinstance(values, (str, bytes)):
            raise TypeError("DisplaySequence requires an iterable of elements, not text.")
        normalized = tuple(values)
        object.__setattr__(self, "values", normalized)
        for value in normalized:
            if not _is_sequence_item(value):
                raise TypeError("DisplaySequence element has an unsupported type.")
            if not has_visible_content(_render_sequence_item(value)):
                raise ValueError("Every DisplaySequence element must have visible content.")


@dataclass(frozen=True, slots=True)
class DisplayJoined:
    """Display fragments rendered in the surrounding message's locale."""

    values: tuple[str | MessageRef | DisplayJoined, ...]
    separator: str

    def __post_init__(self) -> None:
        if type(self.values) is not tuple or any(
            type(value) not in {str, MessageRef, DisplayJoined} for value in self.values
        ):
            raise TypeError("DisplayJoined requires a tuple of display fragments.")
        if type(self.separator) is not str:
            raise TypeError("DisplayJoined requires a text separator.")

    @property
    def multiline(self) -> bool:
        return "\n" in self.separator or any(_is_multiline_arg(value) for value in self.values)


type MessageArg = MessageScalar | DisplayPath | DisplaySequence | DisplayBlock | DisplayJoined | MessageRef


@dataclass(frozen=True, slots=True, kw_only=True)
class MessageDef:
    """One immutable, extractable semantic message definition."""

    _token: InitVar[object]
    key: str
    fallback: str
    plural_fallback: str | None = None
    multiline: bool = False

    def __init_subclass__(cls, **kwargs: object) -> None:
        raise TypeError("MessageDef cannot be subclassed.")

    def __post_init__(self, _token: object) -> None:
        if _token is not _CONSTRUCTION_TOKEN:
            raise TypeError("MessageDef values must be constructed with msg().")
        _validate_definition(self)

    def bind(self, *, count: int | _UnsetCount | None = _UNSET_COUNT, **args: MessageArg) -> MessageRef:
        """Bind one occurrence's named arguments and optional plural count."""
        if count is _UNSET_COUNT:
            resolved_count = None
        elif self.plural_fallback is None:
            raise ValueError("count is forbidden for a singular message definition.")
        elif type(count) is not int:
            raise TypeError("count must be exactly int.")
        else:
            resolved_count = count
        return MessageRef(
            _token=_CONSTRUCTION_TOKEN,
            definition=self,
            args=tuple(sorted(args.items())),
            count=resolved_count,
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class MessageRef:
    """One immutable occurrence of a semantic message definition."""

    _token: InitVar[object]
    definition: MessageDef
    args: tuple[tuple[str, MessageArg], ...] = ()
    count: int | None = None

    def __init_subclass__(cls, **kwargs: object) -> None:
        raise TypeError("MessageRef cannot be subclassed.")

    def __post_init__(self, _token: object) -> None:
        if _token is not _CONSTRUCTION_TOKEN:
            raise TypeError("MessageRef values must be constructed with MessageDef.bind().")
        _validate_reference(self)


def msg(
    key: str,
    *,
    fallback: str,
    plural_fallback: str | None = None,
    multiline: bool = False,
) -> MessageDef:
    """Declare one extractable semantic message definition."""
    return MessageDef(
        _token=_CONSTRUCTION_TOKEN,
        key=key,
        fallback=fallback,
        plural_fallback=plural_fallback,
        multiline=multiline,
    )


def _validate_definition(definition: MessageDef) -> None:
    if type(definition.key) is not str or not _MESSAGE_KEY_RE.fullmatch(definition.key):
        raise ValueError("Message key must contain two or more lowercase dotted segments.")
    if type(definition.multiline) is not bool:
        raise TypeError("The multiline flag must be a bool.")
    if type(definition.fallback) is not str:
        raise TypeError("Message fallback must be a string.")
    if definition.plural_fallback is not None and type(definition.plural_fallback) is not str:
        raise TypeError("Message plural fallback must be a string or None.")

    singular_names = validate_authored_template(definition.fallback, multiline=definition.multiline)
    if not has_visible_content(definition.fallback):
        raise ValueError("Message fallback must have visible content.")

    if definition.plural_fallback is None:
        if "count" in singular_names:
            raise ValueError("The reserved count slot requires a plural fallback.")
        return

    plural_names = validate_authored_template(definition.plural_fallback, multiline=definition.multiline)
    if not has_visible_content(definition.plural_fallback):
        raise ValueError("Message plural fallback must have visible content.")
    if singular_names - {"count"} != plural_names - {"count"}:
        raise ValueError("Singular and plural fallbacks must share one argument schema.")


def _validate_reference(reference: MessageRef) -> None:
    if type(reference.definition) is not MessageDef:
        raise TypeError("MessageRef.definition must be a MessageDef.")
    if type(reference.args) is not tuple:
        raise TypeError("MessageRef.args must be an immutable tuple.")

    names: list[str] = []
    has_multiline_arg = False
    for item in reference.args:
        if type(item) is not tuple or len(item) != 2:
            raise TypeError("Every bound argument must be a name/value tuple.")
        name, value = item
        if type(name) is not str:
            raise TypeError("Bound argument names must be strings.")
        if name == "count":
            raise ValueError("count is reserved and cannot appear in bound arguments.")
        if not _is_message_arg(value):
            raise TypeError(f"Message argument {name!r} has an unsupported type.")
        names.append(name)
        has_multiline_arg = has_multiline_arg or _is_multiline_arg(value)
    if names != sorted(set(names)):
        raise ValueError("Bound arguments must have unique names in deterministic order.")

    definition = reference.definition
    if definition.plural_fallback is None:
        if reference.count is not None:
            raise ValueError("count is forbidden for a singular message definition.")
    elif type(reference.count) is not int:
        if reference.count is None:
            raise ValueError("count is required for a plural message definition.")
        raise TypeError("count must be exactly int.")
    if has_multiline_arg and not definition.multiline:
        raise ValueError("Multiline arguments require a multiline message definition.")

    expected_names = parse_placeholder_names(definition.fallback) - {"count"}
    if set(names) != expected_names:
        raise ValueError("Bound arguments do not match the message argument schema.")

    fallback = definition.fallback if reference.count is None or reference.count == 1 else definition.plural_fallback
    if fallback is None:
        raise ValueError("A plural reference requires a plural fallback.")
    rendered = format_template(fallback, _render_arguments(reference), multiline=definition.multiline)
    if not has_visible_content(rendered):
        raise ValueError("The formatted English message must have visible content.")


def _is_sequence_item(value: object) -> bool:
    return type(value) in {str, int, DisplayPath}


def _is_message_arg(value: object) -> bool:
    return type(value) in {str, int, DisplayPath, DisplaySequence, DisplayBlock, DisplayJoined, MessageRef}


def _is_multiline_arg(value: MessageArg) -> bool:
    if isinstance(value, MessageRef):
        return value.definition.multiline
    if isinstance(value, DisplayJoined):
        return value.multiline
    return isinstance(value, DisplayBlock)


def _int_display(value: int) -> str:
    try:
        return str(value)
    except ValueError as error:  # CPython's int-to-str digit-count limit
        raise ValueError("Integer message arguments must be displayable in decimal form.") from error


def _render_sequence_item(value: _DisplaySequenceItem) -> str:
    if type(value) is DisplayPath:
        path = surrogate_safe_text(value.value)
        return sanitize_legacy_scalar(path) if has_visible_content(path) else json.dumps(path)
    if type(value) is int:
        return sanitize_legacy_scalar(_int_display(value))
    return sanitize_legacy_scalar(surrogate_safe_text(str(value)))


def _render_message_arg(value: MessageArg, render_nested: Callable[[MessageRef], str]) -> str:
    # Every textual branch neutralizes lone surrogates, not just DisplayPath:
    # scalar and block arguments routinely carry filesystem-derived values
    # (profile names, path lists), and a surrogate that reaches a strict
    # encode crashes the render sink.
    if type(value) is DisplayPath:
        return _render_sequence_item(value)
    if type(value) is DisplaySequence:
        return ", ".join(_render_sequence_item(element) for element in value.values)
    if type(value) is DisplayJoined:
        separator = sanitize_legacy_block(surrogate_safe_text(value.separator))
        return separator.join(_render_message_arg(part, render_nested) for part in value.values)
    if type(value) is MessageRef:
        return render_nested(value)
    if type(value) is DisplayBlock:
        return sanitize_legacy_block(surrogate_safe_text(value.value))
    if type(value) is int:
        return sanitize_legacy_scalar(_int_display(value))
    return sanitize_legacy_scalar(surrogate_safe_text(str(value)))


def _render_arguments(
    reference: MessageRef, render_nested: Callable[[MessageRef], str] = format_message
) -> dict[str, str]:
    rendered = {name: _render_message_arg(value, render_nested) for name, value in reference.args}
    if reference.count is not None:
        rendered["count"] = _int_display(reference.count)
    return rendered
