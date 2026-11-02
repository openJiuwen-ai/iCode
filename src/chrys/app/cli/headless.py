# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Runtime preparation, warnings and output primitives shared by headless commands."""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Final

from chrys.foundation.config.context import EvalContext
from chrys.foundation.config.settings import (
    HEADLESS_DEFAULT_MAX_TRANSIENT_RETRIES,
    MAX_TRANSIENT_RETRIES_LIMIT,
    Settings,
)
from chrys.foundation.config.settings_store import LoadedSettings, SettingsWarning
from chrys.foundation.config.spec import Source
from chrys.foundation.config.warnings import settings_warning_events
from chrys.foundation.events.types import Warning
from chrys.foundation.i18n import Localizer, MessageRef, msg
from chrys.foundation.i18n.formatting import format_message, sanitize_legacy_scalar
from chrys.orchestration.startup import bootstrap_runtime

EXIT_INTERRUPTED: Final = 130
"""Exit code of a headless command stopped by Ctrl+C, as a shell reports SIGINT."""

_MAX_TRANSIENT_RETRIES_INVALID = msg(
    "settings.max_transient_retries_invalid",
    fallback=(
        "Ignoring invalid CHRYS_MAX_TRANSIENT_RETRIES={raw}; "
        "expected a non-negative integer and will use the frontend default."
    ),
)


_MAX_TRANSIENT_RETRIES_CLAMPED = msg(
    "settings.max_transient_retries_clamped",
    fallback=("CHRYS_MAX_TRANSIENT_RETRIES={value} exceeds the limit of {limit}; clamping to {limit}."),
)


@dataclasses.dataclass(frozen=True, slots=True)
class PreparedRuntime:
    """Settings, localization, and warnings established by runtime preparation."""

    loaded: LoadedSettings
    localizer: Localizer
    pending_warnings: list[Warning]

    @property
    def settings(self) -> Settings:
        """The assembled settings, for callers that need nothing else."""
        return self.loaded.settings


_RETRY_KEY = "llm.retry.max_transient"


def speaks_for_the_env_var(warning: SettingsWarning) -> bool:
    """Whether this verdict gets the wording headless callers have been parsing.

    Only where the value really is spelled ``CHRYS_MAX_TRANSIENT_RETRIES``.
    The same key rejected in ``settings.yaml`` is a different thing for the
    user to go and fix, and telling them to check an environment variable they
    never set sends them looking in the wrong place. One predicate for both the
    skip and the report below, so the two cannot drift into double-reporting a
    warning or dropping one.
    """
    return warning.key == _RETRY_KEY and warning.origin.layer is Source.ENV


def retry_warning_ref(warning: SettingsWarning) -> MessageRef:
    if warning.rejected:
        return _MAX_TRANSIENT_RETRIES_INVALID.bind(raw=repr(warning.outcome.raw))
    # ``outcome.value`` is the post-clamp value (50); this message reports what
    # the user wrote ("999 exceeds the limit of 50"), so it binds the raw text.
    return _MAX_TRANSIENT_RETRIES_CLAMPED.bind(
        value=warning.outcome.raw.strip(),
        limit=MAX_TRANSIENT_RETRIES_LIMIT,
    )


def prepare_runtime(*, restoring_session: bool = False) -> PreparedRuntime:
    """Freeze the startup environment, apply runtime patches, and prepare display state without writing stderr.

    A ``--session`` restore loads its settings from the saved session's own
    root, which need not be the process cwd — bootstrapping project-free keeps
    the pending warnings from describing a repository the run doesn't live in.
    A failed restore aborts the run outright, so no fallback session ever runs
    on these project-free settings.
    """
    bootstrap = bootstrap_runtime(
        configure_stdio=True,
        eval_context=EvalContext(
            frontend_default_max_transient_retries=HEADLESS_DEFAULT_MAX_TRANSIENT_RETRIES,
        ),
        project_root=None if restoring_session else Path(os.getcwd()),
    )
    localizer = Localizer("en")
    # Every warning composed here is root-independent — environment and user
    # layers, plus ``settle_session_root``'s verdict — so the list is right for
    # a restored session too, whatever root it lives in. The target root's own
    # additions (its project layer) are printed by ``run_command`` after the
    # restore, as the delta over this list.
    # This one key keeps its own wording, which predates the shared composer and
    # is what headless callers have been parsing; everything else gets the
    # generic message rather than being dropped.
    pending_warnings = [
        *bootstrap.warnings,
        *settings_warning_events(bootstrap.loaded, skip=speaks_for_the_env_var),
    ]

    for warning in bootstrap.loaded.warnings:
        if not speaks_for_the_env_var(warning):
            continue
        display_message = retry_warning_ref(warning)
        pending_warnings.append(
            Warning(
                code="invalid_max_transient_retries",
                message=format_message(display_message),
                display_message=display_message,
            )
        )

    return PreparedRuntime(
        loaded=bootstrap.loaded,
        localizer=localizer,
        pending_warnings=pending_warnings,
    )


def configure_logging() -> None:
    """Prevent library logs from writing to stderr by default in headless CLI mode."""
    logging.basicConfig(handlers=[logging.NullHandler()])


def write_error(
    message: str, *, as_json: bool, code: str = "error", session_id: str | None = None, detail: str | None = None
) -> None:
    """Write one error to stderr; text mode puts *detail* (the raw error text) on an indented line below."""
    if as_json:
        payload = {"error": message, "code": code}
        if session_id:
            # Failed runs still persist their session; surfacing the id lets a
            # batch runner locate and export the trajectory from JSON output
            # alone.
            payload["session_id"] = session_id
        sys.stderr.write(json.dumps(payload, ensure_ascii=False))
        sys.stderr.write("\n")
        return
    sys.stderr.write(f"Error: {sanitize_legacy_scalar(message)}\n")
    if detail:
        sys.stderr.write(f"  detail: {sanitize_legacy_scalar(detail)}\n")


def write_warning(message: str, *, as_json: bool, code: str = "warning") -> None:
    if as_json:
        sys.stderr.write(json.dumps({"warning": message, "code": code}, ensure_ascii=False))
        sys.stderr.write("\n")
        return
    sys.stderr.write(f"Warning: {sanitize_legacy_scalar(message)}\n")


def write_warning_events(warnings: Iterable[Warning], localizer: Localizer, *, as_json: bool) -> None:
    for warning in warnings:
        if as_json:
            message = warning.message
        elif warning.display_message is not None:
            message = localizer.render(warning.display_message)
        else:
            message = warning.message
        write_warning(message, code=warning.code, as_json=as_json)


def write_pending_warnings(runtime: PreparedRuntime, *, as_json: bool) -> None:
    write_warning_events(runtime.pending_warnings, runtime.localizer, as_json=as_json)


def restore_delta_warnings(loaded: LoadedSettings, pending: Iterable[Warning]) -> list[Warning]:
    """Warnings the restored session's settings load adds over the bootstrap's.

    The two loads share every root-independent layer (environment, user
    document), so their verdicts overlap almost entirely; the delta is what
    the project-free bootstrap could not see — the target root's project
    layer and dormant project files. Comparing the composed events keeps the
    overlap out while leaving the already-printed pending list — with its
    settle verdict and its compatibility retry wording — untouched.
    """
    already = {(warning.code, warning.message) for warning in pending}
    return [
        warning
        for warning in settings_warning_events(loaded, skip=speaks_for_the_env_var)
        if (warning.code, warning.message) not in already
    ]


def reported_warning_keys(runtime: PreparedRuntime) -> set[tuple[str, str]]:
    """``(code, message)`` of every pending warning, under both of its wordings.

    The pending list reports the retry environment variable in its
    compatibility wording, while a later settings load (an engine start, a
    workflow's admission) composes the same verdict generically; progress
    output counts either spelling as already reported.
    """
    keys = {(warning.code, warning.message) for warning in runtime.pending_warnings}
    keys.update((warning.code, warning.message) for warning in settings_warning_events(runtime.loaded))
    return keys


def exception_message(exc: BaseException) -> str:
    # KeyError's ``str()`` wraps the message in quotes; ``args[0]`` keeps it clean.
    if isinstance(exc, KeyError) and exc.args:
        return str(exc.args[0])
    return str(exc) or type(exc).__name__


def write_json(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False))
    sys.stdout.write("\n")
