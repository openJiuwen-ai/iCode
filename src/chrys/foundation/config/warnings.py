# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Turning a rejected setting into something the user actually sees.

Separate from the loader because the loader must stay a pure function of its
sources: whether a rejected value is worth interrupting someone over depends on
which frontend is running, and only the caller knows that.

Separate from any one entrypoint because every entrypoint had been dropping
them. ``LoadedSettings`` carried the warnings and each root kept only
``.settings``, so a value the user had written simply had no effect and said
nothing about why.

One catalog entry per reason rather than a shell with a ``{detail}`` slot: a
message argument is a scalar, so a detail composed anywhere but here arrives as
finished English and lands untranslated in the middle of a translated
sentence. Whole sentences are also what a translator can actually work with.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping

from chrys.foundation.config.coercion import CoerceReason
from chrys.foundation.config.settings import Settings
from chrys.foundation.config.settings_store import LoadedSettings, SettingsWarning
from chrys.foundation.config.spec import ENV_SOURCES, SettingOrigin, specs_by_key
from chrys.foundation.config.user_settings import user_settings_path
from chrys.foundation.events.types import Warning
from chrys.foundation.i18n import DisplaySequence, MessageDef, MessageRef, msg
from chrys.foundation.i18n.formatting import format_message

_REJECTED_BOOL = msg(
    "settings.rejected.bool",
    fallback="Ignoring {source}={raw}: expected a boolean.",
)
_REJECTED_INT = msg(
    "settings.rejected.int",
    fallback="Ignoring {source}={raw}: expected an integer.",
)
_REJECTED_NON_NEGATIVE_INT = msg(
    "settings.rejected.non_negative_int",
    fallback="Ignoring {source}={raw}: expected a non-negative integer.",
)
_REJECTED_NUMBER = msg(
    "settings.rejected.number",
    fallback="Ignoring {source}={raw}: expected a number.",
)
_REJECTED_FINITE_NUMBER = msg(
    "settings.rejected.finite_number",
    fallback="Ignoring {source}={raw}: expected a finite number.",
)
_REJECTED_TEXT = msg(
    "settings.rejected.text",
    fallback="Ignoring {source}={raw}: expected text.",
)
_REJECTED_CHOICE = msg(
    "settings.rejected.choice",
    fallback="Ignoring {source}={raw}: expected one of {choices}.",
)
_REJECTED_JSON_ARRAY = msg(
    "settings.rejected.json_array",
    fallback="Ignoring {source}={raw}: expected a JSON array.",
)
_REJECTED_WEB_ORIGIN = msg(
    "settings.rejected.web_origin",
    fallback=(
        "Ignoring {source}={raw}: each address must be an http(s) origin such as https://example.com, "
        "with no path or credentials."
    ),
)
_REJECTED_ENDPOINT_GRANT = msg(
    "settings.rejected.endpoint_grant",
    fallback="Ignoring {source}={raw}: each entry needs a distinct http(s) url and may list credential_env_names.",
)
_REJECTED_DIRECTORY = msg(
    "settings.rejected.directory",
    fallback="Ignoring {source}={raw}: not a usable directory.",
)
_CLAMPED_MINIMUM = msg(
    "settings.clamped.minimum",
    fallback="Raising {source}={raw} to the minimum of {limit}.",
)
_CLAMPED_MAXIMUM = msg(
    "settings.clamped.maximum",
    fallback="Lowering {source}={raw} to the maximum of {limit}.",
)
_UNKNOWN_KEYS = msg(
    "settings.unknown_keys",
    fallback="Ignoring unknown settings keys in {path}: {keys}.",
)
_REJECTED_PROJECT_KEY = msg(
    "settings.rejected.project_key",
    fallback="Ignoring {source}: projects may not set this key.",
)
_REJECTED_PROJECT_LOOSENS = msg(
    "settings.rejected.project_loosens",
    fallback="Ignoring {source}={raw}: a project may only tighten this setting, not loosen it.",
)
_PROJECT_CONFIG_DORMANT = msg(
    "settings.project_config_dormant",
    fallback=(
        "Found project settings in {path} ({keys}), but they are not loaded. To apply them, turn on "
        "Load project settings under Settings → Safety → Project trust (project.config_enabled)."
    ),
)
_PROJECT_HOOKS_DORMANT = msg(
    "settings.project_hooks_dormant",
    fallback=(
        "Found project hooks in {path}, but they are not loaded. To run them, turn on "
        "Load project hooks under Settings → Safety → Project trust (project.hooks_enabled)."
    ),
)
_PROJECT_SKILLS_DORMANT = msg(
    "settings.project_skills_dormant",
    fallback=(
        "Found project skills in {path}. They are not loaded automatically; to load them, turn on "
        "Load project skills under Settings → Safety → Project trust (project.skills_enabled) and keep the "
        "agent's Load skills from working folder option on."
    ),
)
_DORMANT_SOURCE_MESSAGES: Mapping[str, tuple[str, MessageDef]] = {
    "project.hooks_enabled": ("project_hooks_dormant", _PROJECT_HOOKS_DORMANT),
    "project.skills_enabled": ("project_skills_dormant", _PROJECT_SKILLS_DORMANT),
}

_REASON_MESSAGES: Mapping[CoerceReason, MessageDef] = {
    CoerceReason.EXPECTED_BOOL: _REJECTED_BOOL,
    CoerceReason.EXPECTED_INT: _REJECTED_INT,
    CoerceReason.EXPECTED_NON_NEGATIVE_INT: _REJECTED_NON_NEGATIVE_INT,
    CoerceReason.EXPECTED_NUMBER: _REJECTED_NUMBER,
    CoerceReason.EXPECTED_FINITE_NUMBER: _REJECTED_FINITE_NUMBER,
    CoerceReason.EXPECTED_TEXT: _REJECTED_TEXT,
    CoerceReason.NOT_A_CHOICE: _REJECTED_CHOICE,
    CoerceReason.EXPECTED_JSON_ARRAY: _REJECTED_JSON_ARRAY,
    CoerceReason.EXPECTED_WEB_ORIGIN: _REJECTED_WEB_ORIGIN,
    CoerceReason.EXPECTED_ENDPOINT_GRANT: _REJECTED_ENDPOINT_GRANT,
    CoerceReason.NOT_A_DIRECTORY: _REJECTED_DIRECTORY,
    CoerceReason.BELOW_MINIMUM: _CLAMPED_MINIMUM,
    CoerceReason.ABOVE_MAXIMUM: _CLAMPED_MAXIMUM,
    CoerceReason.NOT_ALLOWED_IN_PROJECT: _REJECTED_PROJECT_KEY,
    CoerceReason.LOOSENS_USER_BASELINE: _REJECTED_PROJECT_LOOSENS,
}
"""Every reason, so the composer is total and a new one cannot ship unsayable."""


def setting_source_label(key: str, origin: SettingOrigin) -> str:
    """Name the thing the user has to go and edit, not the internal key.

    ``llm.retry.max_transient`` is the panel's identifier; someone who set
    ``CHRYS_MAX_TRANSIENT_RETRIES`` has never seen it, and quoting the dotted
    key would send them looking for a file they do not have. That holds for
    every layer that reads the variable, not just the process environment: a
    dotenv file spells the variable name too, so naming the file is the only
    part that changes.
    """
    entry = specs_by_key(Settings).get(key)
    name = entry.env if origin.layer in ENV_SOURCES and entry is not None and entry.env is not None else key
    if origin.path is not None:
        return f"{name} in {origin.path}"
    return name


def _render_limit(limit: float | None) -> str:
    """Spell a bound the way the field does — ``50``, not ``50.0``."""
    if limit is None:
        return ""
    return str(int(limit)) if float(limit).is_integer() else str(limit)


def warning_display_message(warning: SettingsWarning) -> MessageRef:
    """Bind the sentence for one verdict, with the arguments it declares.

    Public so a surface that already knows the key (the Settings panel, next
    to a sealed row) can show the reason instead of a generic explanation.
    """
    outcome = warning.outcome
    reason = outcome.reason
    # ``Coerced`` refuses to be built without one for these two statuses, and
    # only these two reach here.
    if reason is None:
        raise RuntimeError("A coercion warning requires a reason.")
    definition = _REASON_MESSAGES[reason]
    source = setting_source_label(warning.key, warning.origin)
    if reason is CoerceReason.NOT_A_CHOICE:
        return definition.bind(source=source, raw=outcome.raw, choices=DisplaySequence(outcome.choices))
    if reason in (CoerceReason.BELOW_MINIMUM, CoerceReason.ABOVE_MAXIMUM):
        return definition.bind(source=source, raw=outcome.raw, limit=_render_limit(outcome.limit))
    if reason is CoerceReason.NOT_ALLOWED_IN_PROJECT:
        # The key is the problem, not its value — echoing what a repository
        # tried to set would only lend it column inches.
        return definition.bind(source=source)
    return definition.bind(source=source, raw=outcome.raw)


def _verdict_event(warning: SettingsWarning) -> Warning:
    display_message = warning_display_message(warning)
    code = "setting_rejected" if warning.rejected else "setting_clamped"
    return Warning(code=code, message=format_message(display_message), display_message=display_message)


def migration_warning_events(warnings: Iterable[SettingsWarning]) -> list[Warning]:
    """Compose events for values a migration dropped or adjusted.

    The same sentences as a load's, because they are the same verdicts from
    the same coercers — only discovered while moving the value instead of
    while reading it. After migration the offending line is gone, so this is
    the one time the user hears about it.
    """
    return [_verdict_event(warning) for warning in warnings]


def settings_warning_events(
    loaded: LoadedSettings,
    *,
    skip: Callable[[SettingsWarning], bool] | None = None,
) -> list[Warning]:
    """Compose one user-visible warning per value a layer offered and lost.

    Args:
        skip: Warnings the caller reports itself, with wording that predates
            this function. Asked per warning rather than per key: one key can
            be rejected in several layers at once, and special wording that
            names an environment variable fits only the layers where the value
            was actually spelled as one.
    """
    events: list[Warning] = [
        _verdict_event(warning) for warning in loaded.warnings if skip is None or not skip(warning)
    ]
    if loaded.unknown_keys:
        # One warning for the batch, not one per key: a whole section of
        # misindented YAML is a single mistake, not twelve.
        display_message = _UNKNOWN_KEYS.bind(
            path=str(user_settings_path()),
            keys=DisplaySequence(loaded.unknown_keys),
        )
        events.append(
            Warning(
                code="setting_unknown_keys",
                message=format_message(display_message),
                display_message=display_message,
            )
        )
    for dormant in loaded.dormant_project:
        # Also one per file: enabling the project layer is one decision, and
        # the keys ride along so the user knows what saying yes would apply.
        display_message = _PROJECT_CONFIG_DORMANT.bind(
            path=str(dormant.path),
            keys=DisplaySequence(dormant.keys),
        )
        events.append(
            Warning(
                code="project_config_dormant",
                message=format_message(display_message),
                display_message=display_message,
            )
        )
    for source in loaded.dormant_project_sources:
        code, message = _DORMANT_SOURCE_MESSAGES[source.key]
        display_message = message.bind(path=str(source.path))
        events.append(Warning(code=code, message=format_message(display_message), display_message=display_message))
    return events
