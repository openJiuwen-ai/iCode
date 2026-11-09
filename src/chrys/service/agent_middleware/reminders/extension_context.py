# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The ``extension.<id>.<name>`` catalogs: what the agent's extension contexts say this turn.

Each ``@extension.context`` function the agent's extensions declare is asked
once per turn, all of them concurrently, each under its own timeout. The text
it returns becomes a catalog named after it, so the middleware's catalog rules
apply: sent again only when it changed or left the model's view, withdrawn
with a notice once it is no longer offered. A context that returns None, fails
or times out offers nothing that turn; a failure is logged, and reported once
per context for the life of the source.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from chrys.extensions import CONTEXT_MAX_TOKENS
from chrys.foundation.platform.files import surrogate_safe_text
from chrys.foundation.text.tool_output import truncate_output
from chrys.service.extensions import EXTENSION_CATALOG_PREFIX, context_failed_warning

if TYPE_CHECKING:
    from collections.abc import Sequence

    from chrys.foundation.events.types import Warning
    from chrys.service.extensions import BoundContext

logger = logging.getLogger(__name__)

_WITHDRAWN = "No longer applies: disregard the earlier version of this context."


class ExtensionContextSource:
    """The extension contexts one middleware asks."""

    def __init__(self, contexts: Sequence[BoundContext]) -> None:
        self._contexts = tuple(contexts)
        self._reported: set[str] = set()

    async def collect(self, prompt: str) -> tuple[tuple[tuple[str, str], ...], tuple[Warning, ...]]:
        """This turn's ``(catalog name, text)`` pairs in declaration order, and the failures not reported before."""
        if not self._contexts:
            return (), ()
        answers = await asyncio.gather(*(self._ask(context, prompt) for context in self._contexts))
        catalogs: list[tuple[str, str]] = []
        warnings: list[Warning] = []
        for context, (text, failure) in zip(self._contexts, answers, strict=True):
            if text is not None:
                catalogs.append((context.catalog_name, _render(context, text)))
            elif failure is not None and context.catalog_name not in self._reported:
                self._reported.add(context.catalog_name)
                warnings.append(context_failed_warning(context, failure))
        return tuple(catalogs), tuple(warnings)

    @staticmethod
    async def _ask(context: BoundContext, prompt: str) -> tuple[str | None, str | None]:
        """The context's text, or None and why it gave none when it failed."""
        declaration = context.declaration
        timeout = asyncio.timeout(declaration.timeout)
        try:
            async with timeout:
                text = await declaration.func(context.turn_context(prompt))
        except Exception as exc:
            failure = (
                f"it took longer than {declaration.timeout:g} seconds"
                if timeout.expired()
                else f"{type(exc).__name__}: {exc}"
            )
            logger.warning(
                "Extension context %s failed for agent %s: %s",
                context.catalog_name,
                context.agent_name,
                failure,
                exc_info=not timeout.expired(),
            )
            return None, failure
        if text is None or isinstance(text, str):
            return (text if text and text.strip() else None), None
        failure = f"it returned {type(text).__name__}, not text"
        logger.warning(
            "Extension context %s failed for agent %s: %s", context.catalog_name, context.agent_name, failure
        )
        return None, failure


def withdrawn_notice(catalog_name: str) -> str | None:
    """What replaces an extension context the agent no longer offers; None for any other catalog.

    Built from the name alone, so it also covers an extension that was turned
    off, removed or failed to load since its context was sent.
    """
    if not catalog_name.startswith(EXTENSION_CATALOG_PREFIX):
        return None
    extension_id, _, name = catalog_name.removeprefix(EXTENSION_CATALOG_PREFIX).partition(".")
    return _wrap(extension_id, name, _WITHDRAWN)


def _render(context: BoundContext, text: str) -> str:
    # A lone surrogate (a POSIX file name that is not UTF-8) cannot be sent to the model as is.
    bounded = truncate_output(surrogate_safe_text(text), CONTEXT_MAX_TOKENS)
    return _wrap(context.extension_id, context.declaration.name, bounded)


def _wrap(extension_id: str, name: str, body: str) -> str:
    return f'<extension_context extension="{extension_id}" name="{name}">\n{body}\n</extension_context>'
