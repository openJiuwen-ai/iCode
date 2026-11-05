# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Shared process bootstrap for Chrys entrypoints."""

from __future__ import annotations

import contextlib
import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

from chrys.foundation.config.context import EvalContext
from chrys.foundation.config.process_settings import install_process_settings, settle_session_root
from chrys.foundation.config.runtime_pointer import MODEL_POINTER_KEY, set_model_pointer
from chrys.foundation.config.settings import Settings
from chrys.foundation.config.settings_store import DEFAULT_EVAL_CONTEXT, LoadedSettings, load_settings
from chrys.foundation.events.types import Warning
from chrys.foundation.i18n import CatalogLoadWarning, CatalogWarningCode, msg
from chrys.foundation.i18n.formatting import format_message

logger = logging.getLogger(__name__)

_INVALID_NO_PROXY_ENV = msg(
    "startup.invalid_no_proxy_env",
    fallback="{var} is invalid for httpx and was ignored ({value}). Remove or fix this environment value.",
)
_CATALOG_LOAD_FAILED = msg(
    "i18n.catalog_load_failed",
    fallback="Could not load translations for locale {locale}; using English.",
)
_RAW_HTTP_CAPTURE_ON = msg(
    "startup.raw_http_capture_on",
    fallback=(
        "Raw HTTP capture is on ({source}): request and response logs record API keys, "
        "prompts and model output unredacted. Turn it off when you are done."
    ),
)
_SETTINGS_MIGRATION_FAILED = msg(
    "startup.settings_migration_failed",
    fallback=(
        "Could not migrate settings into {path} ({error}). "
        "Your existing configuration still applies; migration retries on the next start."
    ),
)
# Deliberately not the sentence above: the legacy ``.env`` keeps loading as a
# layer when its migration fails, so that configuration really does still
# apply — but nothing reads the legacy notifications file any more, so a
# failure there means the preferences in it are not in force at all.
_NOTIFICATIONS_MIGRATION_FAILED = msg(
    "startup.notifications_migration_failed",
    fallback=(
        "Could not migrate notification preferences into {path} ({error}). "
        "This start uses the default notification settings; migration retries on the next start."
    ),
)


@dataclass(frozen=True)
class RuntimeBootstrap:
    """Result of entrypoint runtime bootstrapping."""

    loaded: LoadedSettings
    warnings: list[Warning] = field(default_factory=list)

    @property
    def settings(self) -> Settings:
        """The assembled settings, for callers that need nothing else."""
        return self.loaded.settings


def _dangerous_setting_warnings(loaded: LoadedSettings) -> list[Warning]:
    """Say out loud what a dangerous switch is doing to this process.

    Not a rejected value, so it never appears in ``LoadedSettings.warnings``:
    the setting is valid, took effect, and that is the problem. Raw HTTP capture
    writes API keys, prompts and model output to disk unredacted, and it
    persists across restarts by design — someone who turned it on to chase one
    bug should not find out months later that it never stopped.

    Composed from the installed process values rather than the candidate
    settings, because this field is ``Apply.RESTART``: a reload that changed it
    would report a state this process is not in.
    """
    from chrys.foundation.config.process_settings import process_settings
    from chrys.foundation.config.warnings import setting_source_label

    if not process_settings().raw_http_capture:
        return []
    key = "log.raw_http_capture"
    display_message = _RAW_HTTP_CAPTURE_ON.bind(source=setting_source_label(key, loaded.source_for(key)))
    return [
        Warning(
            code="raw_http_capture_on",
            message=format_message(display_message),
            display_message=display_message,
        )
    ]


def _migrate_legacy_settings() -> list[Warning]:
    """Run the legacy-source imports, reporting rather than failing the boot.

    A migration that cannot run — a stuck lock, a full disk — leaves the world
    exactly as the previous version ran in it: the legacy source still loads
    the way it always did, and the untouched ledger retries next start. That
    is a degraded state worth a warning, never a reason chrys does not open.
    Each component runs independently, so one failing does not hold back the
    other.

    The theme rename is a rewrite inside the document, not an import, and the
    loader follows the rename whatever the document says — so a rewrite that
    cannot happen degrades nothing, and is logged rather than reported.
    """
    from chrys.foundation.config.migrations import (
        migrate_dotenv_v0,
        migrate_notifications_v0,
        migrate_renamed_theme,
    )
    from chrys.foundation.config.user_settings import user_settings_path
    from chrys.foundation.config.warnings import migration_warning_events

    warnings: list[Warning] = []
    components = (
        (migrate_dotenv_v0, _SETTINGS_MIGRATION_FAILED, "settings_migration_failed"),
        (migrate_notifications_v0, _NOTIFICATIONS_MIGRATION_FAILED, "notifications_migration_failed"),
    )
    for migrate, failure, code in components:
        try:
            migration = migrate()
        except Exception as error:
            display_message = failure.bind(path=str(user_settings_path()), error=str(error))
            warnings.append(
                Warning(
                    code=code,
                    message=format_message(display_message),
                    display_message=display_message,
                )
            )
            continue
        warnings.extend(migration_warning_events(migration.warnings))
    try:
        migrate_renamed_theme()
    except Exception:
        logger.info("Could not rename the saved theme in %s", user_settings_path(), exc_info=True)
    return warnings


def catalog_load_warning(warning: CatalogLoadWarning) -> Warning:
    """Compose a catalog-load failure for a root's normal warning route."""
    display_message = _CATALOG_LOAD_FAILED.bind(locale=warning.requested_locale)
    return Warning(
        code=str(CatalogWarningCode.LOAD_FAILED),
        message=format_message(display_message),
        display_message=display_message,
    )


def configure_utf8_stdio() -> None:
    """Force UTF-8 for this process and spawned subprocesses."""
    os.environ.setdefault("PYTHONUTF8", "1")
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            with contextlib.suppress(Exception):
                reconfigure(encoding="utf-8", errors="replace")


def set_process_title(title: str = "chrys") -> None:
    """Set the visible process title when the optional platform support works."""
    try:
        from setproctitle import setproctitle
    except Exception:
        return

    with contextlib.suppress(Exception):
        setproctitle(title)


def bootstrap_runtime(
    *,
    dotenv_override: bool,
    dotenv_cwd: str | os.PathLike[str] | None = None,
    configure_stdio: bool = False,
    apply_patches: bool = True,
    setup_telemetry: bool = True,
    eval_context: EvalContext = DEFAULT_EVAL_CONTEXT,
    project_root: Path | None = None,
) -> RuntimeBootstrap:
    """Load environment, apply process patches, and build settings.

    Args:
        eval_context: The frontend's own policy, passed *into* the load rather
            than substituted afterwards. ``chrys run`` raises the transient
            retry default, and that number is an input to the project layer's
            tighten/loosen verdicts — replacing it after the fact would arrive
            after the decisions it informs.
        project_root: The workspace root whose project trust domain the loaded
            settings live under. The TUI and ``chrys run`` pass their working
            directory — except a ``chrys run --session`` restore, which passes
            nothing because the saved session's own root is the one the restore
            loads from; the ACP manager passes nothing — its base settings are
            deliberately project-free because each session derives its own
            root, and a manager-level project layer would leak one session's
            trust decisions into every other.
    """
    set_process_title()

    # AIxCoding guard: install bundled security guard hooks (src/chrys/kernel/aixcoding/guard)
    try:
        from chrys.kernel.aixcoding.guard.guard_install import install_guard_hooks

        install_guard_hooks()
    except Exception:
        logger.warning("Security guard hooks could not be installed; the guard is not active.", exc_info=True)

    if configure_stdio:
        configure_utf8_stdio()

    from chrys.foundation.config.env_file import config_env_path
    from chrys.foundation.config.env_layers import freeze_process_env, inject_bootstrap_dotenv
    from chrys.service.profiles.models.env_bridge import sanitize_no_proxy_env

    # The snapshot must predate the injection below: it is the "real shell
    # environment" every later settings load resolves against.
    freeze_process_env()
    inject_bootstrap_dotenv(
        [Path(dotenv_cwd if dotenv_cwd is not None else os.getcwd()) / ".env", config_env_path()],
        override=dotenv_override,
    )

    warnings = [
        Warning(
            code="invalid_no_proxy",
            message=f"{var} is invalid for httpx and was ignored ({value!r}). Remove or fix this environment value.",
            display_message=_INVALID_NO_PROXY_ENV.bind(var=var, value=repr(value)),
        )
        for var, value in sanitize_no_proxy_env().items()
    ]
    warnings.extend(_migrate_legacy_settings())

    if apply_patches:
        from chrys.foundation.patches import apply_all

        apply_all()

    loaded = settle_session_root(load_settings(project_root=project_root, eval_context=eval_context))
    install_process_settings(loaded)
    # Seed the live model pointer with the layered verdict, origin intact —
    # never an unconditional overwrite: when the shell exported a value, that
    # value IS the verdict, so the write is a no-op and the origin stays ENV.
    if loaded.settings.model_profile:
        set_model_pointer(loaded.settings.model_profile, origin=loaded.source_for(MODEL_POINTER_KEY))
    warnings.extend(_dangerous_setting_warnings(loaded))

    if setup_telemetry:
        from chrys.foundation.observability.setup import setup_otel

        setup_otel(loaded.settings)

    return RuntimeBootstrap(loaded=loaded, warnings=warnings)
