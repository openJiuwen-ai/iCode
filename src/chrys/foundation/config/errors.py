# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Configuration errors with English diagnostics and a localized display message."""

from __future__ import annotations

from chrys.foundation.i18n import MessageRef, msg
from chrys.foundation.i18n.formatting import format_message

MODEL_ID_EXPECTED = msg(
    "settings.model_id_expected", fallback="{path}: model.profile.active must be a model profile name or ID."
)
RELOAD_SESSION_CHANGED = msg(
    "controls.reload_session_changed", fallback="The session changed before settings could reload."
)
INVALID_SETTING = msg("settings.invalid_value_for_key", fallback="Invalid value for {key}.")


class ConfigError(Exception):
    def __init__(self, display: MessageRef, *, hint: MessageRef | None = None) -> None:
        self.display = display
        self.display_hint = hint
        super().__init__(format_message(display))


class ConfigValueError(ConfigError, ValueError):
    """A configuration value was rejected."""


def config_error_display(exc: Exception) -> str | MessageRef:
    """Keep the diagnostic of third-party and OS errors."""
    return exc.display if isinstance(exc, ConfigError) else str(exc)
