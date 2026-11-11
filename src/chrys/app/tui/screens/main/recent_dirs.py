# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""RecentPaths implementation backed by the persisted workspace MRU index."""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime
from typing import TYPE_CHECKING

from chrys.app.tui.support.path_shortcuts import format_recent_path_label, get_default_favorite_paths
from chrys.app.tui.support.workspace_mru import (
    ensure_workspace_mru_index,
    load_workspace_mru,
    session_root_key,
    workspace_mru_backfilled_for,
    workspace_mru_exists,
)
from chrys.foundation.config.settings import DEFAULT_WORKSPACE_MRU_MAX_ENTRIES

if TYPE_CHECKING:
    from chrys.service.state.store import StateStore

logger = logging.getLogger(__name__)


class WorkspaceMruRecentDirs:
    """Serves recently used working directories from the workspace MRU index.

    Implements the :class:`~chrys.app.tui.screens.dialogs.file_picker.RecentPaths`
    protocol so it can be passed directly to :class:`FilePicker`.

    ``StateStore.list_sessions()`` (a full sessions-directory scan) runs only
    for the one-time seed — when the index is missing or the current session
    root has never been backfilled.  Once a root is marked backfilled, opening
    the picker reads just the small index file, even if the index is empty or
    corrupt.  All store access is offloaded to a worker thread: even the
    one-time seed takes a file lock and does an atomic write, which must not
    block the TUI event loop.
    """

    def __init__(
        self,
        state_store: StateStore | None,
        *,
        max_entries: int = DEFAULT_WORKSPACE_MRU_MAX_ENTRIES,
    ) -> None:
        self._state_store = state_store
        self._max_entries = max_entries

    async def get_recent_paths(self) -> list[tuple[str, str]]:
        if self._max_entries <= 0:
            return []
        index_exists = await asyncio.to_thread(workspace_mru_exists)
        if not index_exists and self._state_store is None:
            # Never create an empty index without a state store — that would
            # block the first real backfill once a state store is available.
            return []
        if index_exists and self._state_store is None:
            return self._format(await asyncio.to_thread(load_workspace_mru, self._max_entries))
        root_key = await asyncio.to_thread(self._session_root_key)
        if index_exists and await asyncio.to_thread(workspace_mru_backfilled_for, root_key):
            return self._format(await asyncio.to_thread(load_workspace_mru, self._max_entries))
        return self._format(await self._seed_and_ensure(root_key))

    async def _seed_and_ensure(self, root_key: str) -> list[str]:
        """Run the one-time session backfill for *root_key* and return the index.

        A failed scan still marks the root as backfilled (with an empty seed)
        so a persistent failure cannot re-trigger the expensive scan on every
        picker open; live workspace events fill the index incrementally.
        """
        seed: list[tuple[str, datetime]] = []
        try:
            seed = await self._collect_seed_paths()
        except Exception:
            logger.warning("Workspace MRU seed scan failed; creating empty index", exc_info=True)
        return await asyncio.to_thread(
            ensure_workspace_mru_index,
            seed,
            max_entries=self._max_entries,
            root_key=root_key,
        )

    async def _collect_seed_paths(self) -> list[tuple[str, datetime]]:
        """Collect ``(path, last_used_at)`` seeds from saved session metadata."""
        exclude = get_default_favorite_paths()
        seed: list[tuple[str, datetime]] = []
        seen: set[str] = set()

        sessions = sorted(await self._require_state_store().list_sessions(), key=lambda s: s.updated_at, reverse=True)
        for meta in sessions:
            for cwd in [meta.primary_cwd, *meta.working_dirs]:
                if not cwd:
                    continue
                cwd = os.path.normpath(cwd)
                key = os.path.normcase(cwd)
                if key in seen or key in exclude:
                    continue
                seen.add(key)
                if not os.path.isdir(cwd):
                    continue
                seed.append((cwd, meta.updated_at))
                if len(seed) >= self._max_entries:
                    return seed
        return seed

    def _require_state_store(self) -> StateStore:
        if self._state_store is None:
            raise RuntimeError("Reading recent session directories requires a state store.")
        return self._state_store

    def _session_root_key(self) -> str:
        return session_root_key(self._require_state_store().session_dir("__workspace_mru_probe__").parent)

    @staticmethod
    def _format(paths: list[str]) -> list[tuple[str, str]]:
        return [(format_recent_path_label(path), path) for path in paths]
