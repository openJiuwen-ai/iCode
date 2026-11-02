# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""How a key's origin renders in the panel: editability, badge and hint."""

from __future__ import annotations

from pathlib import Path

from chrys.app.tui.screens.settings.provenance import is_greyed_layer, provenance_view
from chrys.foundation.config.coercion import Coerced, CoerceReason, CoerceStatus
from chrys.foundation.config.settings import Settings
from chrys.foundation.config.settings_store import LoadedSettings, SettingsWarning
from chrys.foundation.config.spec import FILE_SOURCES, SettingOrigin, Source
from chrys.foundation.i18n.formatting import format_message

_KEY = "llm.retry.max_transient"


def _warning(*, rejected: bool, path: str = "/home/me/.chrys/settings.yaml") -> SettingsWarning:
    outcome = (
        Coerced(status=CoerceStatus.INVALID, raw="lots", reason=CoerceReason.EXPECTED_NON_NEGATIVE_INT)
        if rejected
        else Coerced(status=CoerceStatus.CLAMPED, value=50, raw="99", reason=CoerceReason.ABOVE_MAXIMUM, limit=50)
    )
    return SettingsWarning(key=_KEY, origin=SettingOrigin(layer=Source.USER, path=Path(path)), outcome=outcome)


def test_sealed_hint_is_the_verdict_that_sealed_the_key() -> None:
    loaded = LoadedSettings(
        settings=Settings(),
        provenance={},
        warnings=(_warning(rejected=True, path="/first.yaml"), _warning(rejected=True, path="/last.yaml")),
        sealed_keys=frozenset({_KEY}),
    )
    view = provenance_view(loaded, _KEY)

    assert view.sealed is True and view.editable is True
    assert view.hint is not None
    # The path renders in the platform's own spelling (backslashes on Windows).
    assert (
        format_message(view.hint)
        == f"Ignoring llm.retry.max_transient in {Path('/last.yaml')}=lots: expected a non-negative integer."
    )


def test_sealed_hint_falls_back_when_no_rejection_was_recorded() -> None:
    loaded = LoadedSettings(
        settings=Settings(),
        provenance={},
        warnings=(_warning(rejected=False),),
        sealed_keys=frozenset({_KEY}),
    )
    view = provenance_view(loaded, _KEY)

    assert view.hint is not None
    assert format_message(view.hint) == "The saved value was rejected, so the built-in default is in effect."


def _origin(layer: Source) -> SettingOrigin:
    return SettingOrigin(layer=layer, path=Path("/layer/file") if layer in FILE_SOURCES else None)


def test_greyed_layers_are_exactly_project_and_env() -> None:
    greyed = {layer for layer in Source if is_greyed_layer(_origin(layer))}

    assert greyed == {Source.PROJECT, Source.ENV}
    for layer in Source:
        loaded = LoadedSettings(settings=Settings(), provenance={_KEY: _origin(layer)})
        assert provenance_view(loaded, _KEY).editable is (layer not in greyed)
