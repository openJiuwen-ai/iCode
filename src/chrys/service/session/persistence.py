# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""SessionPersistence — session save/load operations.

Owns the ``StateStore`` reference and all direct persistence calls.
File-edit snapshots are now handled by ``MutationTracker`` (serialized
into the session state as ``chrys_mutations``).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from collections.abc import Mapping
from dataclasses import asdict
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from chrys.foundation.models.history_markers import HistoryMarkerKind
from chrys.foundation.util.env_templates import resolve_env_templates
from chrys.kernel import Message
from chrys.service.context.providers.history import CompressedBlock
from chrys.service.profiles.models.options import effective_chat_options, parse_http_headers
from chrys.service.profiles.models.schema import API_STYLE_CHAT_COMPLETIONS, API_STYLE_RESPONSES

if TYPE_CHECKING:
    from chrys.foundation.events.bus import EventBus
    from chrys.foundation.models.session_surface import SessionSurface
    from chrys.foundation.models.workspace import Workspace
    from chrys.service.profiles.agents.schema import AgentProfile
    from chrys.service.profiles.models.schema import ModelProfile
    from chrys.service.state.store import SessionCheckpoint, StateStore

logger = logging.getLogger(__name__)


@runtime_checkable
class _SerializableMessageLike(Protocol):
    additional_properties: dict[str, Any]

    def to_dict(self) -> dict[str, Any]: ...


def _model_api_style_for_persistence(model_profile: ModelProfile | None) -> str | None:
    """Return non-sensitive API-style provenance for persisted session metadata."""
    if model_profile is None:
        return None
    if model_profile.provider in ("openai", "deepseek-openai"):
        return model_profile.api_style
    if model_profile.provider == "glm-openai":
        return API_STYLE_CHAT_COMPLETIONS
    return ""


def agent_profile_context_fingerprint(
    agent_profile: AgentProfile | None,
    *,
    memory_text: str = "",
    effective_sub_agents: list[dict[str, Any]] | None = None,
    allow_user_interaction: bool = True,
) -> str:
    """Return a stable fingerprint for agent and loaded-memory context sent to the model."""
    if agent_profile is None:
        return ""
    payload = {
        "agent_profile": asdict(agent_profile),
        "memory_text": memory_text,
        "effective_sub_agents": effective_sub_agents or [],
        "capabilities": {"user_interaction": allow_user_interaction},
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _stable_digest(value: Any) -> str:
    """Return a stable SHA-256 digest for JSON-like values."""
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def has_real_messages(state: dict[str, Any]) -> bool:
    """Return True when state contains at least one non-marker live or compressed message."""
    messages = state.get("messages")
    if isinstance(messages, list) and any(_is_real_message(message) for message in messages):
        return True

    compressed_blocks = state.get("compressed_msgs")
    if not isinstance(compressed_blocks, list):
        return False
    for block in compressed_blocks:
        block_messages: list[Any] | None = None
        if isinstance(block, CompressedBlock):
            block_messages = block.messages
        elif isinstance(block, dict):
            raw_messages = block.get("messages")
            if isinstance(raw_messages, list):
                block_messages = raw_messages
        if block_messages is not None and any(_is_real_message(message) for message in block_messages):
            return True
    return False


def _is_real_message(message: Any) -> bool:
    if isinstance(message, Message):
        return HistoryMarkerKind.KEY not in message.additional_properties
    if isinstance(message, _SerializableMessageLike):
        return HistoryMarkerKind.KEY not in message.additional_properties
    if isinstance(message, dict):
        properties = message.get("additional_properties")
        if isinstance(properties, dict):
            return HistoryMarkerKind.KEY not in properties
        return True
    return False


def _effective_api_key_for_fingerprint(model_profile: ModelProfile) -> str:
    """Resolve the effective credential only to hash it into model-context provenance."""
    explicit = resolve_env_templates(model_profile.api_key, location=f"model profile {model_profile.name!r} API Key")
    if explicit:
        return explicit
    if model_profile.provider == "openai":
        return os.environ.get("OPENAI_API_KEY", "")
    if model_profile.provider == "deepseek-openai":
        return os.environ.get("DEEPSEEK_API_KEY", "")
    if model_profile.provider == "glm-openai":
        return os.environ.get("ZAI_API_KEY", "")
    if model_profile.provider == "anthropic":
        return os.environ.get("ANTHROPIC_API_KEY", "")
    return ""


def _account_scope_for_fingerprint(model_profile: ModelProfile) -> dict[str, str]:
    """Return provider account-scope values that can make service ids non-portable."""
    if model_profile.provider == "openai":
        return {
            "organization": os.environ.get("OPENAI_ORG_ID", ""),
            "project": os.environ.get("OPENAI_PROJECT_ID", ""),
        }
    return {}


def model_profile_context_fingerprint(
    model_profile: ModelProfile | None,
    *,
    chat_options: Mapping[str, Any] | None = None,
) -> str:
    """Return non-sensitive provenance for model service-session compatibility."""
    if model_profile is None:
        return ""
    from chrys.service.llm.clients import effective_model_base_url

    payload = {
        "id": model_profile.id,
        "provider": model_profile.provider,
        "api_style": model_profile.api_style,
        "model_id": model_profile.model_id,
        "base_url": effective_model_base_url(model_profile),
        "api_key_sha256": _stable_digest(_effective_api_key_for_fingerprint(model_profile)),
        "account_scope_sha256": _stable_digest(_account_scope_for_fingerprint(model_profile)),
        "http_headers_sha256": _stable_digest(parse_http_headers(model_profile)),
        "chat_options_sha256": _stable_digest(chat_options),
    }
    return _stable_digest(payload)


class SessionPersistence:
    """Handles all session persistence operations.

    Usage::

        persistence = SessionPersistence(state_store, bus)
        await persistence.save_session(session_id, history_state, ...)
    """

    def __init__(self, state_store: StateStore | None, bus: EventBus) -> None:
        self._state_store = state_store
        self._bus = bus
        self._last_checkpoint: tuple[str, SessionCheckpoint] | None = None

    def checkpoint_for(self, session_id: str | None) -> SessionCheckpoint | None:
        """Identity of *session_id*'s most recent successful primary save.

        Bound to the session it came from and dropped as soon as a save fails:
        anything derived from a revision that was never written must not be
        able to name one — a stale id would claim provenance the file cannot
        confirm, which is worse than claiming none.
        """
        last = self._last_checkpoint
        if last is None or session_id is None or last[0] != session_id:
            return None
        return last[1]

    @property
    def state_store(self) -> StateStore | None:
        """The underlying state store (may be ``None``)."""
        return self._state_store

    async def save_session(
        self,
        session_id: str | None,
        history_state: dict[str, Any],
        *,
        agent_profile_name: str,
        agent_display_name: str,
        agent_profile_id: str,
        workspace: Workspace | None,
        model_profile: ModelProfile | None = None,
        service_session_id: str | None = None,
        agent_profile_fingerprint: str = "",
        model_profile_fingerprint: str | None = None,
        last_surface: SessionSurface | None = None,
        raise_on_error: bool = False,
        retain_empty: bool = False,
    ) -> bool:
        """Persist session state to disk and publish ``SessionSaved``.

        Chat saves normally require real messages. Explicit interactive handoffs
        may retain an empty native checkpoint without inventing a user turn.
        Workflow state is owned separately.
        """
        if self._state_store is None or session_id is None:
            return False

        from chrys.foundation.events.types import SessionSaved

        try:
            # Skip saving if every message is a chrys marker (no real conversation).
            if not retain_empty and not has_real_messages(history_state):
                return False

            # Extract agent profile history from switch records
            agent_profile_history: list[str] = []
            last_profile = ""
            for sw in history_state.get("agent_profile_switches", []):
                for key in [("from_display", "from"), ("to_display", "to")]:
                    name = sw.get(key[0]) or sw.get(key[1]) or ""
                    if name and name != last_profile:
                        agent_profile_history.append(name)
                        last_profile = name

            working_dirs: list[str] = []
            primary_cwd = ""
            if workspace is not None:
                primary_cwd = workspace.primary_cwd
                working_dirs = [d.path for d in workspace.working_dirs]

            # Strip the model profile down to the non-sensitive identification
            # fields we persist.  Anything else on the ``ModelProfile``
            # (api_key, headers, timeouts, chat_options) belongs to the
            # *environment* and must not land in the session file.
            model_provider = model_profile.provider if model_profile is not None else ""
            model_api_style = _model_api_style_for_persistence(model_profile)
            model_id = model_profile.model_id if model_profile is not None else ""
            model_profile_id = model_profile.id if model_profile is not None else ""
            if model_profile is not None:
                from chrys.service.llm.clients import effective_model_base_url

                model_base_url = effective_model_base_url(model_profile)
                if model_provider == "openai" and model_api_style == API_STYLE_RESPONSES:
                    chat_options = effective_chat_options(model_profile)
                    if model_profile_fingerprint is None:
                        model_profile_fingerprint = model_profile_context_fingerprint(
                            model_profile,
                            chat_options=chat_options,
                        )
                    if chat_options is None or chat_options.get("store") is not True:
                        service_session_id = ""
                else:
                    model_profile_fingerprint = ""
                    service_session_id = ""
            else:
                model_base_url = ""

            checkpoint = await self._state_store.save_session(
                session_id,
                history_state,
                agent_profile=agent_profile_name,
                agent_display_name=agent_display_name,
                agent_profile_id=agent_profile_id,
                agent_profile_fingerprint=agent_profile_fingerprint,
                primary_cwd=primary_cwd,
                working_dirs=working_dirs,
                agent_profile_history=agent_profile_history or None,
                model_provider=model_provider,
                model_api_style=model_api_style,
                model_id=model_id,
                model_profile_id=model_profile_id,
                model_base_url=model_base_url,
                model_profile_fingerprint=model_profile_fingerprint,
                service_session_id=service_session_id,
                last_surface=last_surface,
            )
            self._last_checkpoint = (session_id, checkpoint) if checkpoint is not None else None
            await self._bus.publish(SessionSaved(session_id=session_id))
            return True
        except Exception:
            logger.warning("Failed to save session %s", session_id, exc_info=True)
            self._last_checkpoint = None
            if raise_on_error:
                raise
            return False

    async def save_recovery_session(
        self,
        session_id: str | None,
        history_state: dict[str, Any],
        *,
        agent_profile_name: str,
        agent_display_name: str,
        agent_profile_id: str,
        workspace: Workspace | None,
        model_profile: ModelProfile | None = None,
        agent_profile_fingerprint: str = "",
        model_profile_fingerprint: str | None = None,
        last_surface: SessionSurface | None = None,
    ) -> None:
        """Persist a recovery sidecar, logging and swallowing write failures."""
        try:
            await self.save_recovery_session_strict(
                session_id,
                history_state,
                agent_profile_name=agent_profile_name,
                agent_display_name=agent_display_name,
                agent_profile_id=agent_profile_id,
                workspace=workspace,
                model_profile=model_profile,
                agent_profile_fingerprint=agent_profile_fingerprint,
                model_profile_fingerprint=model_profile_fingerprint,
                last_surface=last_surface,
            )
        except Exception:
            logger.warning("Failed to save recovery session %s", session_id, exc_info=True)

    async def save_recovery_session_strict(
        self,
        session_id: str | None,
        history_state: dict[str, Any],
        *,
        agent_profile_name: str,
        agent_display_name: str,
        agent_profile_id: str,
        workspace: Workspace | None,
        model_profile: ModelProfile | None = None,
        agent_profile_fingerprint: str = "",
        model_profile_fingerprint: str | None = None,
        last_surface: SessionSurface | None = None,
    ) -> bool:
        """Persist a recovery sidecar and propagate every write-path failure."""
        if self._state_store is None or session_id is None or not has_real_messages(history_state):
            return False

        agent_profile_history: list[str] = []
        last_profile = ""
        for sw in history_state.get("agent_profile_switches", []):
            for key in [("from_display", "from"), ("to_display", "to")]:
                name = sw.get(key[0]) or sw.get(key[1]) or ""
                if name and name != last_profile:
                    agent_profile_history.append(name)
                    last_profile = name

        working_dirs: list[str] = []
        primary_cwd = ""
        if workspace is not None:
            primary_cwd = workspace.primary_cwd
            working_dirs = [d.path for d in workspace.working_dirs]

        model_provider = model_profile.provider if model_profile is not None else ""
        model_api_style = _model_api_style_for_persistence(model_profile)
        model_id = model_profile.model_id if model_profile is not None else ""
        model_profile_id = model_profile.id if model_profile is not None else ""
        if model_profile is not None:
            from chrys.service.llm.clients import effective_model_base_url

            model_base_url = effective_model_base_url(model_profile)
            if model_provider == "openai" and model_api_style == API_STYLE_RESPONSES:
                chat_options = effective_chat_options(model_profile)
                if model_profile_fingerprint is None:
                    model_profile_fingerprint = model_profile_context_fingerprint(
                        model_profile,
                        chat_options=chat_options,
                    )
            else:
                model_profile_fingerprint = ""
        else:
            model_base_url = ""

        await asyncio.to_thread(
            self._state_store.save_recovery_session,
            session_id,
            history_state,
            agent_profile=agent_profile_name,
            agent_display_name=agent_display_name,
            agent_profile_id=agent_profile_id,
            agent_profile_fingerprint=agent_profile_fingerprint,
            primary_cwd=primary_cwd,
            working_dirs=working_dirs,
            agent_profile_history=agent_profile_history or None,
            model_provider=model_provider,
            model_api_style=model_api_style,
            model_id=model_id,
            model_profile_id=model_profile_id,
            model_base_url=model_base_url,
            model_profile_fingerprint=model_profile_fingerprint,
            last_surface=last_surface,
        )
        return True

    async def recovery_session_wins(self, session_id: str) -> bool:
        """Return whether the crash-recovery sidecar is the winning source."""
        if self._state_store is None:
            return False
        return await self._state_store.recovery_session_wins(session_id)

    async def load_session(self, session_id: str, *, prefer_recovery: bool = False) -> dict | None:
        """Load a session's state from disk."""
        if self._state_store is None:
            return None
        return await self._state_store.load_session(session_id, prefer_recovery=prefer_recovery)

    async def load_session_meta(self, session_id: str, *, prefer_recovery: bool = False):
        """Load metadata for one session by id."""
        if self._state_store is None:
            return None
        return await self._state_store.load_session_meta(session_id, prefer_recovery=prefer_recovery)

    async def list_sessions(self) -> list:
        """List all saved sessions."""
        if self._state_store is None:
            return []
        return await self._state_store.list_sessions()

    async def delete_session(self, session_id: str, *, allow_active: bool = False) -> None:
        """Delete a session from disk."""
        if self._state_store is None:
            return
        await self._state_store.delete_session(session_id, allow_active=allow_active)
