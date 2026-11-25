# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Resolve a typed session ID for a TUI restore."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from chrys.foundation.i18n import MessageRef, msg

if TYPE_CHECKING:
    from chrys.service.state.store import SessionMeta, StateStore

logger = logging.getLogger(__name__)

RESUME_TITLE = msg("tui.session.title.resume", fallback="Resume")
SESSION_READ_FAILED = msg(
    "tui.startup.session.read_failed",
    fallback="Could not read session {session_id}: {error}",
)
SESSION_NOT_FOUND = msg(
    "tui.startup.session.not_found",
    fallback="Session {session_id} was not found.",
)
SESSION_IN_USE = msg(
    "tui.startup.session.in_use", fallback="Session {session_id} is open in another window or process."
)


async def lookup_session(
    store: StateStore, selector: str, *, require_inactive: bool = False
) -> SessionMeta | MessageRef:
    """Return the canonical metadata or the reason we cannot open this ID."""
    from chrys.service.state.store import AmbiguousSessionIdError, StartupSessionInUseError

    try:
        meta = await store.resolve_startup_session(selector, require_inactive=require_inactive)
    except StartupSessionInUseError:
        return SESSION_IN_USE.bind(session_id=selector)
    except AmbiguousSessionIdError as exc:
        return exc.display_message or SESSION_READ_FAILED.bind(session_id=selector, error=str(exc))
    except Exception as exc:
        logger.warning("Failed to read session %s", selector, exc_info=True)
        return SESSION_READ_FAILED.bind(session_id=selector, error=str(exc))
    return meta if meta is not None else SESSION_NOT_FOUND.bind(session_id=selector)
