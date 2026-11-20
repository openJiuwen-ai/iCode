# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Stable display metadata for localized Textual bindings."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from textual.binding import Binding

from chrys.foundation.i18n import MessageDef, msg

logger = logging.getLogger(__name__)

BACK_BINDING = msg("tui.binding.back", fallback="Back")
CLOSE_BINDING = msg("tui.binding.close", fallback="Close")
CANCEL_BINDING = msg("tui.binding.cancel", fallback="Cancel")
COPY_BINDING = msg("tui.binding.copy", fallback="Copy")
DELETE_BINDING = msg("tui.binding.delete", fallback="Delete")
PASTE_BINDING = msg("tui.binding.paste", fallback="Paste")
QUIT_BINDING = msg("tui.binding.quit", fallback="Quit")
TOGGLE_VIEW_BINDING = msg("tui.binding.toggle_view", fallback="Toggle view")

_SHARED_BINDING_DISPLAY_IDS = frozenset(
    {
        BACK_BINDING.key,
        CLOSE_BINDING.key,
        CANCEL_BINDING.key,
        COPY_BINDING.key,
        DELETE_BINDING.key,
        PASTE_BINDING.key,
    }
)


@dataclass(frozen=True, slots=True)
class BindingDisplaySpec:
    """Extractable display definitions associated with one stable binding id."""

    description: MessageDef
    tooltip: MessageDef | None = None


@dataclass(frozen=True, slots=True)
class _BindingDisplayRegistration:
    spec: BindingDisplaySpec
    actions: frozenset[str]


_BINDING_DISPLAY_REGISTRY: dict[str, _BindingDisplayRegistration] = {}


def _register_binding_display(binding_id: str, spec: BindingDisplaySpec, action: str) -> None:
    registration = _BindingDisplayRegistration(spec, frozenset({action}))
    current = _BINDING_DISPLAY_REGISTRY.get(binding_id)
    if current is None:
        _BINDING_DISPLAY_REGISTRY[binding_id] = registration
    elif current.spec != spec:
        raise ValueError(f"Conflicting binding display registration for {binding_id!r}.")
    elif action not in current.actions:
        if binding_id not in _SHARED_BINDING_DISPLAY_IDS:
            raise ValueError(f"Conflicting binding display registration for {binding_id!r}.")
        _BINDING_DISPLAY_REGISTRY[binding_id] = _BindingDisplayRegistration(spec, current.actions | {action})


def localized_binding(
    key: str,
    action: str,
    definition: MessageDef,
    *,
    tooltip: MessageDef | None = None,
    **binding_kwargs: Any,
) -> Binding:
    """Build a Binding and atomically register its stable display metadata."""
    spec = BindingDisplaySpec(description=definition, tooltip=tooltip)
    binding = Binding(
        key,
        action,
        definition.fallback,
        tooltip=tooltip.fallback if tooltip is not None else "",
        id=definition.key,
        **binding_kwargs,
    )
    _register_binding_display(definition.key, spec, action)
    return binding


def get_binding_display_spec(binding_id: str) -> BindingDisplaySpec | None:
    """Return the display spec registered under *binding_id*, if any."""
    registration = _BINDING_DISPLAY_REGISTRY.get(binding_id)
    return registration.spec if registration is not None else None


def get_binding_display_provenance(binding_id: str, action: str | None = None) -> str | None:
    """Return the action recorded alongside *binding_id*, if registered."""
    registration = _BINDING_DISPLAY_REGISTRY.get(binding_id)
    if registration is None:
        return None
    if action is not None:
        return action if action in registration.actions else None
    if len(registration.actions) == 1:
        return next(iter(registration.actions))
    return None


def resolve_binding_display(binding: Binding) -> BindingDisplaySpec | None:
    """Resolve display metadata when id and action provenance both agree."""
    if binding.id is None:
        return None
    registration = _BINDING_DISPLAY_REGISTRY.get(binding.id)
    if registration is None:
        return None
    if binding.action not in registration.actions:
        logger.warning("Binding display provenance mismatch; using literal display text.")
        return None
    return registration.spec


def iter_binding_display_specs() -> Iterator[BindingDisplaySpec]:
    """Iterate a stable snapshot of the exact registered display specs."""
    return iter(tuple(registration.spec for registration in _BINDING_DISPLAY_REGISTRY.values()))


__all__ = [
    "BACK_BINDING",
    "CANCEL_BINDING",
    "CLOSE_BINDING",
    "COPY_BINDING",
    "DELETE_BINDING",
    "PASTE_BINDING",
    "QUIT_BINDING",
    "TOGGLE_VIEW_BINDING",
    "BindingDisplaySpec",
    "get_binding_display_provenance",
    "get_binding_display_spec",
    "iter_binding_display_specs",
    "localized_binding",
    "resolve_binding_display",
]
