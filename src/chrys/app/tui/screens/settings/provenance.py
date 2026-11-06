# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""How a setting's provenance renders: editable or greyed, badge, and hint."""

from __future__ import annotations

from dataclasses import dataclass

from chrys.foundation.config.settings import Settings
from chrys.foundation.config.settings_store import LoadedSettings
from chrys.foundation.config.spec import SettingOrigin, Source, specs_by_key
from chrys.foundation.config.warnings import warning_display_message
from chrys.foundation.i18n import DisplayPath, MessageRef, msg

_BADGE_PROJECT = msg("tui.settings.badge.project", fallback="project")
_BADGE_ENV = msg("tui.settings.badge.env", fallback="env")
_BADGE_THIS_SESSION = msg("tui.settings.badge.this_session", fallback="this session")
_BADGE_PINNED = msg("tui.settings.badge.pinned", fallback="pinned")
_BADGE_SEALED = msg("tui.settings.badge.sealed", fallback="sealed")

_HINT_SET_BY_PROJECT = msg("tui.settings.provenance.project", fallback="Set by {path}.")
_HINT_SET_BY_ENV = msg(
    "tui.settings.provenance.env",
    fallback="Set by environment variable {variable}.",
)
_HINT_CLI = msg(
    "tui.settings.provenance.cli",
    fallback="Set for this session by a command-line option; the saved value applies at the next launch.",
)
_HINT_SESSION_PIN = msg(
    "tui.settings.provenance.session",
    fallback="Pinned for this session; the saved value applies to new sessions.",
)
_HINT_SEALED = msg(
    "tui.settings.provenance.sealed",
    fallback="The saved value was rejected, so the built-in default is in effect.",
)

_GREYED_LAYERS = frozenset({Source.PROJECT, Source.ENV})


@dataclass(frozen=True, slots=True)
class ProvenanceView:
    """What the row shows for one key's origin."""

    editable: bool
    badge: MessageRef | None = None
    hint: MessageRef | None = None
    sealed: bool = False


def is_greyed_layer(origin: SettingOrigin) -> bool:
    """Whether the winning layer is one the panel cannot write over."""
    return origin.layer in _GREYED_LAYERS


def provenance_view(loaded: LoadedSettings, key: str) -> ProvenanceView:
    """Describe how *key* renders given where its effective value came from."""
    origin = loaded.source_for(key)
    if key in loaded.sealed_keys:
        return ProvenanceView(editable=True, badge=_BADGE_SEALED.bind(), hint=_sealed_hint(loaded, key), sealed=True)
    layer = origin.layer
    if layer is Source.PROJECT:
        path = DisplayPath(origin.path) if origin.path is not None else DisplayPath("settings.yaml")
        return ProvenanceView(editable=False, badge=_BADGE_PROJECT.bind(), hint=_HINT_SET_BY_PROJECT.bind(path=path))
    if layer is Source.ENV:
        return ProvenanceView(
            editable=False,
            badge=_BADGE_ENV.bind(),
            hint=_HINT_SET_BY_ENV.bind(variable=_env_name(key)),
        )
    if layer is Source.CLI:
        return ProvenanceView(editable=True, badge=_BADGE_THIS_SESSION.bind(), hint=_HINT_CLI.bind())
    if layer in (Source.SESSION, Source.PROCESS_RUNTIME):
        return ProvenanceView(editable=True, badge=_BADGE_PINNED.bind(), hint=_HINT_SESSION_PIN.bind())
    return ProvenanceView(editable=True)


def _sealed_hint(loaded: LoadedSettings, key: str) -> MessageRef:
    """The verdict that sealed *key*: the last rejection the loader recorded for it."""
    for warning in reversed(loaded.warnings):
        if warning.key == key and warning.rejected:
            return warning_display_message(warning)
    return _HINT_SEALED.bind()


def _env_name(key: str) -> str:
    entry = specs_by_key(Settings).get(key)
    return entry.env if entry is not None and entry.env is not None else key
