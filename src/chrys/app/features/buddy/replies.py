# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""What the buddy says back when it is petted: one short line from a model, or a stock one."""

from __future__ import annotations

import asyncio
import logging
import secrets
import threading
from typing import TYPE_CHECKING

from chrys.foundation.i18n import MessageRef, msg
from chrys.foundation.i18n.formatting import format_message
from chrys.foundation.models.history_markers import HistoryMarkerKind

if TYPE_CHECKING:
    from chrys.app.features.buddy.model import Buddy
    from chrys.kernel import Message
    from chrys.orchestration.engine.engine import AgentEngine
    from chrys.service.profiles.models.registry import ModelProfileRegistry

logger = logging.getLogger(__name__)

# The last three exchanges, each cut to its tail: enough to tell how the work is going, little
# enough to stay cheap whatever was pasted or summarized into them.
_HISTORY_MESSAGES = 6
_HISTORY_MESSAGE_CHARS = 400
_REPLY_MARK = "💛"

# Each stock line is its own message definition, so a translation can rewrite it whole. The mark
# rides in the template: a reference cannot be prefixed by its caller the way a plain string can.
_REPLY_WRIGGLES = msg(
    "tui.buddy.reply.wriggles",
    fallback="💛 {name} wriggles, then pretends nothing happened.",
)
_REPLY_HUMS = msg(
    "tui.buddy.reply.hums",
    fallback="💛 {name} hums a little and goes back to watching your cursor.",
)
_REPLY_SPINS = msg(
    "tui.buddy.reply.spins",
    fallback="💛 {name} does a small, pleased spin.",
)
_REPLY_BLINKS = msg(
    "tui.buddy.reply.blinks",
    fallback="💛 {name} blinks slowly. That means yes.",
)
_REPLY_SCOOTS = msg(
    "tui.buddy.reply.scoots",
    fallback="💛 {name} scoots a little closer to the keyboard.",
)

STOCK_REPLIES = (_REPLY_WRIGGLES, _REPLY_HUMS, _REPLY_SPINS, _REPLY_BLINKS, _REPLY_SCOOTS)

_PROMPT = """You are {name}, a {species} who keeps a programmer company while they work. {persona}
Whenever you are petted, say what {name} would say back: a single line no longer than a text message, in the \
language the programmer has been writing in. Let your species and character show. If the conversation shows \
them stuck or tired, be kind about it. Write that line and nothing else."""


# At most one reply is being written at a time, whichever surface asked for it. A surface takes
# the gate with ``acquire(blocking=False)`` and drops the pet when it cannot have it.
reply_gate = threading.Lock()


async def pet_reply(buddy: Buddy) -> MessageRef | str:
    """The buddy's answer to being petted. Falls back to a stock message; never raises."""
    try:
        text = await _ask_model(buddy)
    except Exception:
        logger.debug("Buddy reply failed; using a stock line", exc_info=True)
        text = ""
    return f"{_REPLY_MARK} {text}" if text else stock_reply(buddy)


def stock_reply(buddy: Buddy) -> MessageRef:
    """An answer to being petted that needs no model."""
    return secrets.choice(STOCK_REPLIES).bind(name=buddy.name)


async def _ask_model(buddy: Buddy) -> str:
    from chrys.foundation.config.settings_store import load_settings
    from chrys.kernel import Message
    from chrys.orchestration.engine.engine import get_current_engine
    from chrys.service.llm.clients import scoped_client
    from chrys.service.llm.one_shot import get_final_response
    from chrys.service.llm.route_sessions import derive_llm_route_session_id
    from chrys.service.profiles.models.options import effective_chat_options
    from chrys.service.profiles.models.resolver import resolve_active_profile, resolve_selectable_profile

    # The live engine knows the session's model, mid-session switches included. Before any
    # session exists there is none, and the settings on disk decide instead. They are read
    # off-thread: this runs on the UI event loop.
    engine = get_current_engine()
    if engine is None:
        settings, registry, session_id, session_dir = (
            (await asyncio.to_thread(load_settings)).settings,
            None,
            None,
            None,
        )
    else:
        settings, registry = engine.settings, engine.model_registry
        session_id, session_dir = engine.session_id, engine.session_dir
    profile = engine.active_model_profile if engine is not None else None
    if profile is None:
        profile = resolve_active_profile(registry, settings)
    if selector := settings.buddy_model_profile:
        if registry is None:
            # The profiles are on disk, read off-thread like the settings.
            registry = await asyncio.to_thread(_profiles_on_disk)
        # The chosen profile answers over its own connection, like the other model roles. One
        # that is gone or not filled in yet has no model to ask: the active one answers.
        profile = resolve_selectable_profile(registry, selector) or profile

    request = "You have just been petted."
    conversation = _recent_conversation(engine)
    if conversation:
        request = f"The conversation so far:\n{conversation}\n\n{request}"
    async with scoped_client(
        profile,
        session_id=(
            derive_llm_route_session_id(session_id, route_kind="buddy-reply", model_profile=profile)
            if session_id
            else None
        ),
        parent_session_id=session_id,
        session_dir=session_dir,
    ) as client:
        response = await get_final_response(
            client,
            [
                Message(
                    role="system",
                    contents=[
                        # The prompt is English throughout; the persona line stays English inside it.
                        _PROMPT.format(
                            name=buddy.name, species=buddy.species.value, persona=format_message(buddy.persona)
                        )
                    ],
                ),
                Message(role="user", contents=[request]),
            ],
            stream=profile.stream,
            options={"model": profile.model_id, **(effective_chat_options(profile) or {})},
            timeout=profile.http_read_timeout,
        )
    return (response.text or "").strip()


def _recent_conversation(engine: AgentEngine | None) -> str:
    if engine is None:
        return ""
    try:
        history = engine.history_messages
    except RuntimeError:
        # No history is bound before the session starts. The buddy can still answer.
        return ""
    spoken = [message for message in history if message.text and not _is_marker(message)]
    return "\n".join(
        f"{'User' if message.role == 'user' else 'Assistant'}: {_tail(message.text)}"
        for message in spoken[-_HISTORY_MESSAGES:]
    )


def _is_marker(message: Message) -> bool:
    # Turn boundaries, compaction summaries and interruptions are bookkeeping, not conversation.
    return HistoryMarkerKind.KEY in message.additional_properties


def _tail(text: str) -> str:
    return text if len(text) <= _HISTORY_MESSAGE_CHARS else "…" + text[-_HISTORY_MESSAGE_CHARS:]


def _profiles_on_disk() -> ModelProfileRegistry:
    from chrys.service.profiles.models.registry import ModelProfileRegistry

    registry = ModelProfileRegistry()
    registry.load_profiles()
    return registry
