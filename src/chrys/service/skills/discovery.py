# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Initial file-skill snapshots owned by one agent build.

The scope only prepares initial catalogs. Providers keep an uncached loader for
later turns, so neither the scope nor its first snapshot outlives the build via
a provider's refresh callback. Each consumer receives fresh mutable skill data.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from chrys.foundation.util.once_close import OnceClose
from chrys.service.skills.loader import DEFAULT_RESOURCE_EXTENSIONS, DEFAULT_SEARCH_DEPTH, discover_file_skills
from chrys.service.skills.provider import StagedSkillRefresh

if TYPE_CHECKING:
    from collections.abc import Sequence
    from types import TracebackType

    from chrys.service.skills.model import Skill, SkillLoadFailure


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _DiscoveryKey:
    paths: tuple[str, ...]
    script_extensions: tuple[str, ...]
    resource_extensions: tuple[str, ...]
    search_depth: int


@dataclass(frozen=True)
class _Snapshot:
    skills: tuple[Skill, ...]
    failures: tuple[SkillLoadFailure, ...]


def _discover(key: _DiscoveryKey) -> _Snapshot:
    started = time.perf_counter()
    outcome = "failed"
    skill_count = failure_count = 0
    try:
        skills, failures = discover_file_skills(
            key.paths,
            script_extensions=key.script_extensions,
            resource_extensions=key.resource_extensions,
            search_depth=key.search_depth,
        )
        skill_count, failure_count = len(skills), len(failures)
        # Stage once with discovery, off the event loop: revisions stat every
        # discovered file and must remain fixed across all consumers of this snapshot.
        staged = StagedSkillRefresh(skills)
        outcome = "discovered"
        return _Snapshot(tuple(staged.skills), tuple(failures))
    finally:
        logger.debug(
            "Skill discovery scan: %s, %d skills, %d failures, %.1f ms",
            outcome,
            skill_count,
            failure_count,
            (time.perf_counter() - started) * 1000,
        )


def _copy_skill(skill: Skill) -> Skill:
    """Copy mutable catalog data, retaining immutable strings and the staged revision."""
    return replace(
        skill,
        resources=[replace(resource) for resource in skill.resources],
        scripts=[replace(script) for script in skill.scripts],
        metadata=None if skill.metadata is None else dict(skill.metadata),
    )


def _consume_result(task: asyncio.Task[_Snapshot]) -> None:
    if not task.cancelled():
        task.exception()


class SkillDiscoveryScope:
    """Share initial file discovery for one build, including completed failures.

    Ordered paths preserve first-wins skill precedence. Cancelling a consumer
    cannot cancel a shared scan; closing the scope drains its worker tasks,
    since cancelling an asyncio waiter does not stop filesystem IO in a thread.
    """

    def __init__(self) -> None:
        self._tasks: dict[_DiscoveryKey, asyncio.Task[_Snapshot]] = {}
        self._closed = False
        self._close_once = OnceClose(self._drain)

    async def __aenter__(self) -> SkillDiscoveryScope:
        if self._closed:
            raise RuntimeError("Skill discovery scope is closed")
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, traceback: TracebackType | None
    ) -> None:
        await self.aclose()

    async def discover(
        self,
        paths: Sequence[str],
        *,
        script_extensions: Sequence[str],
        resource_extensions: Sequence[str] = DEFAULT_RESOURCE_EXTENSIONS,
        search_depth: int = DEFAULT_SEARCH_DEPTH,
    ) -> tuple[list[Skill], list[SkillLoadFailure]]:
        """Return private mutable data from a shared, revision-stamped snapshot."""
        if self._closed:
            raise RuntimeError("Skill discovery scope is closed")
        key = _DiscoveryKey(tuple(paths), tuple(script_extensions), tuple(resource_extensions), search_depth)
        task = self._tasks.get(key)
        if task is None:
            task = asyncio.create_task(asyncio.to_thread(_discover, key))
            task.add_done_callback(_consume_result)
            self._tasks[key] = task
        else:
            logger.debug("Reusing this build's skill discovery")
        snapshot = await asyncio.shield(task)
        if self._closed:
            raise RuntimeError("Skill discovery scope is closed")
        return [_copy_skill(skill) for skill in snapshot.skills], list(snapshot.failures)

    async def aclose(self) -> None:
        """Reject new consumers and drain all owned scans, including after cancellation."""
        self._closed = True
        await self._close_once()

    async def _drain(self) -> None:
        try:
            if self._tasks:
                await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        finally:
            self._tasks.clear()
