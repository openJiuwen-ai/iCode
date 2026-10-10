# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""ACP session ownership for Chrys."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import tempfile
import threading
import uuid
from collections.abc import Callable
from copy import deepcopy
from dataclasses import asdict, dataclass, field, is_dataclass
from pathlib import Path
from typing import Any, TypeGuard, TypeVar, cast

from acp import schema as acp_schema

from chrys.app.acp.json_values import JsonValue, to_json_object
from chrys.app.features.session_title.updater import SessionTitleUpdater
from chrys.foundation.branding import APP_DISPLAY_NAME
from chrys.foundation.config.context import EvalContext
from chrys.foundation.config.process_settings import reattribute_command_line, route_restart_settings
from chrys.foundation.config.runtime_pointer import MODEL_POINTER_ENV, set_model_pointer
from chrys.foundation.config.settings_store import LoadedSettings, load_settings, persist
from chrys.foundation.config.spec import SettingOrigin, Source
from chrys.foundation.config.user_settings import flatten_user_doc, user_settings_path
from chrys.foundation.config.warnings import settings_warning_events
from chrys.foundation.config.yaml_store import read_yaml_doc
from chrys.foundation.events.types import (
    AgentLoadFailed,
    AgentProfileSwitch,
    ApprovalModeUpdated,
    Error,
    Event,
    InvocationAbortRequested,
    InvocationRetryRequested,
    ModelProfileSwitched,
    ProfileSwitched,
    RollbackResult,
    SetApprovalMode,
    SetModelProfile,
    SettingsReload,
    SettingsReloaded,
    SleepSkip,
    UserInject,
    UserRollback,
    Warning,
    WorkspaceChange,
    WorkspaceUpdated,
)
from chrys.foundation.models.session_surface import SessionSurface
from chrys.foundation.models.workspace import WorkingDir, Workspace
from chrys.foundation.platform.files import surrogate_safe_text
from chrys.foundation.text.yaml_io import dump_yaml
from chrys.foundation.util.session_ids import SESSION_SHORT_ID_LEN, session_short_id
from chrys.orchestration.session_host import ChrysSessionHost
from chrys.service.approval.policy import ApprovalMode
from chrys.service.mcp.adapter import MCPAdapter
from chrys.service.profiles.agents.loader import AgentProfileLoadError, is_filename_safe_profile_name
from chrys.service.profiles.agents.loader import load_profile_from_yaml as load_agent_profile_from_yaml
from chrys.service.profiles.agents.registry import AgentProfileRegistry
from chrys.service.profiles.agents.schema import AgentProfile, MCPServerConfig
from chrys.service.profiles.agents.serializer import delete_profile as delete_agent_profile
from chrys.service.profiles.agents.serializer import profile_to_dict as agent_profile_to_dict
from chrys.service.profiles.agents.serializer import save_profile as save_agent_profile
from chrys.service.profiles.agents.serializer import stored_profile_name as stored_agent_profile_name
from chrys.service.profiles.models.loader import load_profile_from_yaml as load_model_profile_from_yaml
from chrys.service.profiles.models.registry import ModelProfileRegistry
from chrys.service.profiles.models.schema import ModelProfile
from chrys.service.profiles.models.serializer import delete_profile as delete_model_profile
from chrys.service.profiles.models.serializer import save_profile as save_model_profile
from chrys.service.state.store import ChatSessionMeta, JsonFileStateStore, StateStore
from chrys.service.tools.names import chrys_reserved_tool_names

logger = logging.getLogger(__name__)

_RuntimeEventT = TypeVar("_RuntimeEventT", bound=Event)


@dataclass(frozen=True)
class AcpConfigOption:
    """One supported config key, under all three of the names it answers to.

    The wire protocol's logical name predates the settings document and the
    ``CHRYS_*`` aliases predate both, so requests may arrive in either
    spelling; the setting key is where the value actually lives.
    """

    logical_key: str
    """Wire-protocol name, e.g. ``theme``."""

    setting_key: str
    """Dotted settings-document key, e.g. ``ui.theme``."""

    env_alias: str
    """Legacy ``CHRYS_*`` spelling — accepted on input, echoed as ``envKey``."""


_SUPPORTED_CONFIG_OPTIONS: tuple[AcpConfigOption, ...] = (
    AcpConfigOption("default_agent", "agent.default_profile", "CHRYS_DEFAULT_AGENT"),
    AcpConfigOption("model_profile", "model.profile.active", "CHRYS_MODEL_PROFILE"),
    AcpConfigOption("approval_judge_model_profile", "model.role.approval_judge", "CHRYS_MODEL_PROFILE_APPROVAL_JUDGE"),
    AcpConfigOption("default_approval_mode", "approval.default_mode", "CHRYS_DEFAULT_APPROVAL_MODE"),
    AcpConfigOption("theme", "ui.theme", "CHRYS_THEME"),
    AcpConfigOption("rollback_snapshots_keep", "rollback.snapshots_keep", "CHRYS_ROLLBACK_SNAPSHOTS_KEEP"),
)


class AcpSessionError(RuntimeError):
    """Raised when an ACP session request cannot be fulfilled."""


@dataclass
class ManagedSession:
    """One active Chrys session exposed over ACP."""

    session_id: str
    cwd: str
    profile_name: str
    host: ChrysSessionHost
    # Per-session auto-title generator; the bridge forwards its
    # ``SessionTitleUpdated`` events as ACP ``session_info_update``.
    title_updater: SessionTitleUpdater | None = None
    # Serializes runtime-control mutations (model/workspace/profile/settings) so
    # two concurrent requests on one session can't each resolve from the other's
    # completion event — ``_await_runtime_mutation`` correlates only by session id
    # and result type, which is ambiguous under overlap.
    mutation_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # Prompts serialize per session, not process-wide. Lifecycle operations
    # acquire this lock before shutdown so a host cannot be torn down while a
    # prompt is still draining its terminal events.
    prompt_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    closing: bool = False

    async def shutdown(self) -> None:
        """Shut down the host, then drain the title updater.

        Order matters: the engine's shutdown drains a still-finalizing run
        whose success callback can schedule one last title task; the updater
        must be torn down after so that task is cancelled and awaited —
        even when the host shutdown itself fails.
        """
        try:
            await self.host.shutdown()
        finally:
            if self.title_updater is not None:
                await self.title_updater.shutdown()


@dataclass
class ManagedLoadResult:
    """Result of an ACP session load request."""

    session: ManagedSession
    reused_existing: bool
    recovered_from_sidecar: bool = False


class AcpSessionManager:
    """Own active Chrys hosts for one ACP process."""

    def __init__(
        self,
        *,
        loaded_settings: LoadedSettings,
        profile_name: str,
        approval_mode: ApprovalMode,
        process_cwd: str | None,
        state_store: StateStore | None = None,
        agent_registry: AgentProfileRegistry | None = None,
        model_registry: ModelProfileRegistry | None = None,
        on_successful_turn: Callable[[], None] | None = None,
    ) -> None:
        self._loaded_settings = loaded_settings
        self._profile_name = profile_name
        self._approval_mode = approval_mode
        self._process_cwd = _resolve_dir(process_cwd) if process_cwd is not None else None
        self._state_store = state_store or JsonFileStateStore()
        self._agent_registry = agent_registry or AgentProfileRegistry()
        self._model_registry = model_registry or ModelProfileRegistry()
        self._on_successful_turn = on_successful_turn
        self._sessions: dict[str, ManagedSession] = {}
        # Guards process-wide session-map/lifecycle mutations only. Prompt
        # turns use ManagedSession.prompt_lock so unrelated sessions never
        # share a turn-length critical section.
        self._lock = asyncio.Lock()
        # Guards the whole global-settings transaction — document write, model
        # pointer, published base settings — so the three can never disagree.
        # A thread lock, not an asyncio one: these run under
        # ``asyncio.to_thread`` because they touch the settings file, so two
        # config requests genuinely execute them in parallel. Reentrant because
        # the refresh takes it for its own callers as well as inside a write.
        self._global_settings_lock = threading.RLock()

    @property
    def state_store(self) -> StateStore:
        """State store used for ACP session operations."""
        return self._state_store

    @property
    def process_cwd(self) -> str | None:
        """Default cwd supplied by ``chrys acp --workdir``, if any."""
        return self._process_cwd

    def load_registries(self) -> None:
        """Load profile registries once for this ACP process."""
        if not self._agent_registry.list_names():
            self._agent_registry.load_all()
        if not self._model_registry.list_ids():
            self._model_registry.load_all()

    def list_agent_profiles(self) -> list[dict[str, object]]:
        """Return available agent profile summaries."""
        self.load_registries()
        return [
            _agent_profile_info(profile, builtin=self._agent_registry.is_builtin(profile.name))
            for profile in self._agent_registry.list_profiles(include_sub_agent_only=True)
        ]

    def read_agent_profile(self, name: str) -> dict[str, JsonValue]:
        """Return one agent profile as JSON-like data with launch secrets masked."""
        self.load_registries()
        profile = self._agent_registry.resolve_selector(name)
        if profile is None:
            msg = f"Agent profile not found: {name}"
            raise AcpSessionError(msg)
        data = jsonable_dataclass(profile)
        if profile.acp is None:
            data.pop("acp", None)
        # Web patches distinguish absence from explicit null. Use their profile
        # representation so the editable ACP payload round-trips that contract.
        serialized_tools = agent_profile_to_dict(profile).get("tools", {})
        for key in ("web_search", "web_fetch"):
            data["tools"].pop(key, None)
            if key in serialized_tools:
                data["tools"][key] = serialized_tools[key]
        # Free-form YAML (``metadata``, web provider ``request``/``auth``) keeps
        # the dates and sets the parser made; the reply must be plain JSON.
        return to_json_object(_redact_agent_profile_secrets(data))

    @staticmethod
    def _stored_agent_profile_name(name: str) -> str | None:
        """Read disk ownership without trusting the process's cached registry."""
        try:
            return stored_agent_profile_name(name)
        except AgentProfileLoadError as err:
            msg = f"Cannot verify ownership of agent profile file for {name!r}; refusing to modify it. {err}"
            raise AcpSessionError(msg) from err

    @staticmethod
    def _require_agent_profile_file_owner(name: str, stored_name: str | None) -> None:
        """Reject mutations through a filename that currently belongs to another profile."""
        if stored_name is not None and stored_name != name:
            msg = f"Agent profile file for {name!r} belongs to {stored_name!r}; refusing to modify it."
            raise AcpSessionError(msg)

    def write_agent_profile(self, data: dict[str, object]) -> dict[str, object]:
        """Validate and persist an agent profile.

        Masked MCP and ACP values (``"***"``) coming back from a prior read
        are restored from the stored profile so a round-trip never destroys
        real secrets.
        """
        if not data.get("name"):
            msg = "Agent profile name is required."
            raise AcpSessionError(msg)
        self.load_registries()
        profile_data = dict(data)
        profile_data["name"] = _safe_profile_file_stem(profile_data["name"], field_name="agent profile name")
        name = str(profile_data["name"])
        self._require_agent_profile_file_owner(name, self._stored_agent_profile_name(name))
        profile_data.setdefault("id", uuid.uuid4().hex[:12])
        existing = self._agent_registry.get(str(profile_data["name"]))
        ambiguous_identity = False
        if existing is None:
            # ``AgentProfile.id`` is the identity that survives renames: a
            # read → rename → write round-trip keeps the stored id while the
            # name lookup misses, yet the payload's masked values still refer
            # to that stored profile's secrets. Restore from the id match so
            # a rename never persists literal ``"***"`` over real secrets.
            profile_id = profile_data.get("id")
            matches = [
                candidate
                for candidate in self._agent_registry.list_profiles(include_sub_agent_only=True)
                if profile_id and candidate.id == profile_id
            ]
            if len(matches) == 1:
                existing = matches[0]
            elif matches:
                # Twin profiles sharing an id (this CRUD's rename keeps the
                # old file, so copies are reachable): a masked read happened
                # but there is no single source of truth — fail closed and
                # make the caller re-enter the secrets instead of guessing.
                ambiguous_identity = True
        acp_restoration = _AcpMaskRestoration(args_were_masked=ambiguous_identity)
        if existing is not None:
            _restore_masked_mcp_secrets(profile_data, existing)
            acp_restoration = _restore_masked_acp_secrets(profile_data, existing)
        _reject_unresolved_masked_mcp_secrets(profile_data)
        _reject_unresolved_masked_acp_secrets(profile_data, restoration=acp_restoration)
        profile = _agent_profile_from_data(profile_data)
        profile.name = _safe_profile_file_stem(profile.name, field_name="agent profile name")
        path = save_agent_profile(profile)
        self._agent_registry.register(profile)
        return {
            "profile": _agent_profile_info(profile, builtin=self._agent_registry.is_builtin(profile.name)),
            "path": str(path),
        }

    def delete_agent_profile(self, name: str) -> dict[str, object]:
        """Delete a user agent profile from disk and registry."""
        self.load_registries()
        name = _safe_profile_file_stem(name, field_name="agent profile name")
        if self._agent_registry.is_builtin(name):
            msg = f"Built-in agent profiles cannot be deleted; reset {name!r} instead."
            raise AcpSessionError(msg)
        if self._agent_registry.get(name) is None and (
            builtin_name := self._agent_registry.resolve_builtin_filename(name)
        ):
            # No registered profile owns this spelling, but its filename may
            # address a built-in's shadow on a case-insensitive filesystem.
            msg = f"Built-in agent profiles cannot be deleted; reset {builtin_name!r} instead."
            raise AcpSessionError(msg)
        stored_name = self._stored_agent_profile_name(name)
        self._require_agent_profile_file_owner(name, stored_name)
        if stored_name is None:
            return {"name": name, "deleted": False}
        if self._agent_registry.get(name) is not None:
            # Removal also saves changed user parents. Check the entire write
            # set before deleting the child or changing any registry entries.
            for parent in self._agent_registry.profiles_referencing(name):
                if not self._agent_registry.is_builtin(parent.name):
                    parent_owner = self._stored_agent_profile_name(parent.name)
                    if parent_owner is None:
                        # Do not recreate stale parents: multiple absent paths
                        # can alias each other once cascade starts saving them.
                        msg = (
                            f"Parent agent profile file for {parent.name!r} is missing; "
                            "refusing to recreate it during deletion."
                        )
                        raise AcpSessionError(msg)
                    self._require_agent_profile_file_owner(parent.name, parent_owner)
        deleted = delete_agent_profile(name)
        if deleted:
            self._agent_registry.remove(name)
        return {"name": name, "deleted": deleted}

    def reset_agent_profile(self, name: str) -> dict[str, object]:
        """Reset a built-in agent while retaining Skills, MCP, and Memory settings."""
        self.load_registries()
        name = _safe_profile_file_stem(name, field_name="agent profile name")
        current = self._agent_registry.get(name)
        template = self._agent_registry.get_builtin_template(name)
        if current is None or template is None:
            msg = f"Built-in agent profile not found: {name}"
            raise AcpSessionError(msg)
        stored_name = self._stored_agent_profile_name(name)
        reset = self._agent_registry.build_builtin_reset(name, preserve_from=current)
        if _agent_profile_reset_comparison_dict(reset) == _agent_profile_reset_comparison_dict(template):
            changed = reset != current
            # Only the actual owner may remove the file. A known twin is
            # preserved; unreadable ownership already raised before this point.
            if stored_name == name:
                changed = delete_agent_profile(name) or changed
        else:
            changed = reset != current
            if changed:
                self._require_agent_profile_file_owner(name, stored_name)
                save_agent_profile(reset)
        if changed:
            self._agent_registry.register(reset)
        return {
            "profile": _agent_profile_info(reset, builtin=True),
            "changed": changed,
        }

    def list_model_profiles(self) -> list[dict[str, object]]:
        """Return available model profile summaries."""
        self.load_registries()
        return [_model_profile_info(profile) for profile in self._model_registry.list_profiles()]

    def read_model_profile(self, profile_id: str) -> dict[str, object]:
        """Return one model profile as JSON-like data without secrets."""
        self.load_registries()
        profile = self._model_registry.get(profile_id)
        if profile is None:
            msg = f"Model profile not found: {profile_id}"
            raise AcpSessionError(msg)
        data = jsonable_dataclass(profile)
        data["api_key"] = "***" if data.get("api_key") else ""
        # NOTE: only api_key is masked. ``http_headers`` and ``chat_options`` are
        # JSON strings that can also carry auth (e.g. an Authorization header or a
        # provider token) and are returned verbatim to the ACP client. Accepted
        # for now — ACP is a same-user local process — but if this surface is ever
        # exposed to a less-trusted client, mask/redact these too (and add the
        # unresolved-mask write rejection used for MCP secrets).
        return data

    def write_model_profile(self, data: dict[str, object]) -> dict[str, object]:
        """Validate and persist a model profile."""
        if not data.get("name"):
            msg = "Model profile name is required."
            raise AcpSessionError(msg)
        self.load_registries()
        profile_data = dict(data)
        profile_id = str(profile_data.get("id") or "")
        existing = self._model_registry.get(profile_id) if profile_id else None
        if existing is not None:
            merged = jsonable_dataclass(existing)
            # Skip only absent (None) fields; an explicit "" clears the field (e.g.
            # base_url, http_headers, chat_options) so stale endpoints/auth don't linger.
            # api_key is the sole exception: ""/"***"/None all preserve the stored secret.
            merged.update({key: value for key, value in profile_data.items() if value is not None})
            if profile_data.get("api_key") in {None, "", "***"}:
                merged["api_key"] = existing.api_key
            profile_data = merged
        else:
            # No profile to restore the secret from (e.g. read-copy-write with a new
            # id), so a still-masked api_key would persist literally. Reject it, like
            # the MCP secret handling in _reject_unresolved_masked_mcp_secrets.
            if profile_data.get("api_key") == _MASK:
                msg = (
                    "Model profile api_key is still masked; re-enter the secret after renaming or copying the profile."
                )
                raise AcpSessionError(msg)
            profile_data.setdefault("id", uuid.uuid4().hex[:12])
        profile_data["id"] = _safe_profile_file_stem(profile_data["id"], field_name="model profile id")
        profile = _model_profile_from_data(profile_data)
        profile.id = _safe_profile_file_stem(profile.id, field_name="model profile id")
        path = save_model_profile(profile)
        self._model_registry.register(profile)
        return {"profile": _model_profile_info(profile), "path": str(path)}

    def delete_model_profile(self, profile_id: str) -> dict[str, object]:
        """Delete a model profile from disk and registry."""
        self.load_registries()
        profile_id = _safe_profile_file_stem(profile_id, field_name="model profile id")
        deleted = delete_model_profile(profile_id)
        if deleted:
            self._model_registry.remove(profile_id)
        return {"id": profile_id, "deleted": deleted}

    def set_config_option(self, key: str, value: object) -> dict[str, object]:
        """Persist a supported Chrys config option to the user settings document.

        Validation is the settings store's own: the same coercer that will
        read the value back judges it on the way in, and a rejected value
        never reaches the file. ``bypass`` never persists —
        :func:`chrys.foundation.config.settings_store.persist` downgrades it
        to ``auto`` so the next launch cannot start in unattended
        auto-approval mode.

        No ``os.environ`` mirror: a mirrored value would come back as the
        ``ENV`` layer and outrank the very document this just wrote. The one
        exception is the model pointer, whose environment entry is this
        process's live selection channel, not a configuration source.

        Document, pointer and published settings move together under one lock.
        Concurrent requests reach this from separate worker threads, and the
        pointer outranks the document it was written from — so two clients
        setting a model interleaved can otherwise commit one profile to disk
        and install the other as the live pointer, leaving a process whose
        model changes when it restarts.
        """
        option = _config_option(key)
        text_value = "" if value is None else str(value)
        with self._global_settings_lock:
            if text_value:
                result = persist({option.setting_key: text_value})
                if not result.ok:
                    reason = result.rejected[option.setting_key].reason
                    detail = reason.name.lower() if reason is not None else "rejected"
                    msg = f"Invalid value for config option {key!r}: {text_value!r} ({detail})."
                    raise AcpSessionError(msg)
                stored = result.written[option.setting_key]
            else:
                persist({}, remove=(option.setting_key,))
                stored = ""
            if option.env_alias == MODEL_POINTER_ENV:
                pointer = str(stored) if stored else None
                set_model_pointer(pointer, origin=SettingOrigin(layer=Source.PROCESS_RUNTIME))
            self._refresh_default_settings_from_env()
        return {
            "key": key,
            "envKey": option.env_alias,
            "settingKey": option.setting_key,
            "value": _config_display_value(stored),
        }

    def get_config_options(self, session_id: str | None = None) -> dict[str, object]:
        """Return supported config options: durable values plus provenance.

        ``value`` is what the user document stores. The effective pair is
        named ``baseValue``/``baseSource`` because without a ``session_id``
        the manager — which holds many sessions — can only answer for its
        base settings, not claim any one session's view. Passing a
        ``session_id`` adds ``sessionValue``/``sessionSource`` read from that
        session's own loaded settings.

        The whole snapshot is taken inside the same transaction lock the
        writers hold. Without it this read falls into the gap a write leaves
        between committing the document and republishing the base settings —
        the document's file lock is released at the first of those, not the
        second — and the reply pairs the newly stored ``value`` with the
        superseded ``baseValue``/``baseSource``, which is exactly the
        contradiction the three fields exist to rule out. Taking the lock
        before the document's own is the order every writer uses.
        """
        with self._global_settings_lock:
            session_loaded = self.get(session_id).host.engine.loaded_settings if session_id else None
            doc = read_yaml_doc(user_settings_path()) or {}
            keys = frozenset(option.setting_key for option in _SUPPORTED_CONFIG_OPTIONS)
            values, _ = flatten_user_doc(doc, keys)
            base = self._loaded_settings
            base_values = asdict(base.settings)
            session_values = asdict(session_loaded.settings) if session_loaded is not None else {}
        fields = _config_setting_fields()

        options: list[dict[str, object]] = []
        for option in _SUPPORTED_CONFIG_OPTIONS:
            entry: dict[str, object] = {
                "key": option.logical_key,
                "envKey": option.env_alias,
                "settingKey": option.setting_key,
                "value": _config_display_value(values.get(option.setting_key, "")),
                "baseValue": _config_display_value(base_values[fields[option.setting_key]]),
                "baseSource": base.source_for(option.setting_key).layer.name.lower(),
            }
            if session_loaded is not None:
                entry["sessionValue"] = _config_display_value(session_values[fields[option.setting_key]])
                entry["sessionSource"] = session_loaded.source_for(option.setting_key).layer.name.lower()
            options.append(entry)
        payload: dict[str, object] = {"options": options}
        if session_id:
            payload["sessionId"] = session_id
        return payload

    async def test_mcp_server(self, data: dict[str, object]) -> dict[str, object]:
        """Run a one-shot MCP connection test for a config payload."""
        config = _mcp_config_from_data(data)
        adapter = MCPAdapter(reserved_tool_names=chrys_reserved_tool_names())
        try:
            await adapter.test_connection(config)
        except Exception as exc:
            return {
                "ok": False,
                "name": config.name,
                "message": str(exc),
                "bannerLines": list(getattr(exc, "banner_lines", []) or []),
            }
        return {"ok": True, "name": config.name, "message": "Connected."}

    def _build_host(
        self,
        *,
        profile_name: str,
        session_id: str | None = None,
        cwd: str,
        loaded_settings: LoadedSettings,
        workspace: Workspace | None,
        mcp_overlay: list[MCPServerConfig],
    ) -> tuple[ChrysSessionHost, SessionTitleUpdater]:
        """Construct a session host plus its per-session auto-title updater.

        The updater's turn callbacks must be wired at engine construction,
        but the updater itself needs the constructed host's bus and engine —
        hence the late-bound slot.
        """
        updater_slot: list[SessionTitleUpdater] = []
        process_on_successful_turn = self._on_successful_turn

        def _on_successful_turn() -> None:
            if process_on_successful_turn is not None:
                process_on_successful_turn()
            if updater_slot:
                updater_slot[0].on_turn_finished()

        def _on_turn_started() -> None:
            if updater_slot:
                updater_slot[0].on_turn_started()

        host = ChrysSessionHost(
            profile_name=profile_name,
            session_id=session_id,
            loaded_settings=loaded_settings,
            agent_registry=self._agent_registry,
            model_registry=self._model_registry,
            state_store=self._state_store,
            approval_mode=self._approval_mode,
            cwd=cwd,
            workspace=workspace,
            mcp_overlay=mcp_overlay,
            allow_user_interaction=True,
            on_successful_turn=_on_successful_turn,
            on_turn_started=_on_turn_started,
            surface=SessionSurface.ACP,
        )
        updater = SessionTitleUpdater(host.event_bus, self._state_store, engine_getter=lambda: host.engine)
        updater_slot.append(updater)
        return host, updater

    async def new_session(
        self,
        *,
        cwd: str | None,
        additional_directories: list[str] | None = None,
        mcp_servers: list[acp_schema.HttpMcpServer | acp_schema.SseMcpServer | acp_schema.McpServerStdio] | None,
        warnings: list[Warning] | None = None,
    ) -> ManagedSession:
        """Create and start a new Chrys session.

        Args:
            warnings: Collects what this session's settings load had to
                reject — a denied or dormant project key, for one. Session
                creation runs outside any prompt turn, so nothing forwards bus
                events; a caller that does not collect these has no other way
                to learn the cwd it named carries settings that did not apply.
        """
        async with self._lock:
            self.load_registries()
            profile = self._agent_registry.resolve_selector(self._profile_name)
            if profile is None:
                msg = f"Agent profile not found: {self._profile_name}"
                raise AcpSessionError(msg)
            mcp_overlay = _mcp_overlay(mcp_servers)
            resolved_cwd = self._resolve_requested_cwd(cwd)
            # Off-thread: the load reads settings files. Derived per session
            # because each session's cwd names its own project trust domain.
            session_loaded = await asyncio.to_thread(self._session_loaded_settings, resolved_cwd)
            if warnings is not None:
                warnings.extend(settings_warning_events(session_loaded))
            host, title_updater = self._build_host(
                profile_name=profile.name,
                cwd=resolved_cwd,
                loaded_settings=session_loaded,
                workspace=_workspace_from_dirs(resolved_cwd, additional_directories)
                if additional_directories
                else None,
                mcp_overlay=mcp_overlay,
            )
            # ACP owns the ask_user lifetime, so its injected timeout survives a
            # settings reload instead of reverting to the env default.
            host.engine.pin_ask_user_timeout()
            try:
                await host.start()
            except BaseException:
                await _shutdown_after_failed_start(host, title_updater)
                raise
            session_id = host.session_id
            if session_id is None:
                msg = f"{APP_DISPLAY_NAME} did not create a session id."
                await _shutdown_after_failed_start(host, title_updater)
                raise AcpSessionError(msg)
            managed = ManagedSession(
                session_id=session_id,
                cwd=resolved_cwd,
                profile_name=profile.name,
                host=host,
                title_updater=title_updater,
            )
            self._sessions[session_id] = managed
            return managed

    async def load_session(
        self,
        *,
        cwd: str | None,
        session_id: str,
        additional_directories: list[str] | None = None,
        mcp_servers: list[acp_schema.HttpMcpServer | acp_schema.SseMcpServer | acp_schema.McpServerStdio] | None,
        warnings: list[Warning] | None = None,
    ) -> ManagedLoadResult:
        """Restore an existing Chrys session.

        Args:
            warnings: Same contract as :meth:`new_session`; empty when an
                already-active session is reused, because nothing was loaded.
        """
        async with self._lock:
            self.load_registries()
            resolved_cwd = self._resolve_requested_cwd(cwd)
            mcp_overlay = _mcp_overlay(mcp_servers)
            existing = self._resolve_active_session(session_id)
            if existing is not None:
                self._validate_active_cwd(existing, resolved_cwd)
                # Overlays (MCP servers / extra directories) cannot be applied to a
                # running host, so reject rather than silently dropping them.
                if mcp_overlay:
                    msg = (
                        f"Session '{session_short_id(existing.session_id)}' is already active. "
                        "Close it before reloading with MCP server overlays."
                    )
                    raise AcpSessionError(msg)
                if additional_directories is not None:
                    # Changing roots (including clearing them) means rebuilding the
                    # workspace, which a running host can't do — reject rather than
                    # silently keeping the old scope.
                    msg = (
                        f"Session '{session_short_id(existing.session_id)}' is already active. "
                        "Close it before reloading with additional directories."
                    )
                    raise AcpSessionError(msg)
                return ManagedLoadResult(
                    session=existing,
                    reused_existing=True,
                    recovered_from_sidecar=existing.host.engine.recovered_from_sidecar,
                )
            canonical_id, meta = await self._resolve_session_meta(session_id)
            self._validate_saved_cwd(meta, resolved_cwd)
            # Off-thread: the load reads settings files. The restore itself
            # re-derives against the saved cwd, but the host must not exist —
            # even pre-restore — under another project's trust domain.
            session_loaded = await asyncio.to_thread(self._session_loaded_settings, resolved_cwd)
            host, title_updater = self._build_host(
                profile_name="",
                session_id=canonical_id,
                cwd=resolved_cwd,
                loaded_settings=session_loaded,
                # ``None`` keeps the saved roots; a list (even empty) is authoritative,
                # so pass a workspace whenever the client specified the field at all.
                workspace=_workspace_from_dirs(resolved_cwd, additional_directories)
                if additional_directories is not None
                else None,
                mcp_overlay=mcp_overlay,
            )
            # ACP owns the ask_user lifetime, so its injected timeout survives a
            # settings reload instead of reverting to the env default.
            host.engine.pin_ask_user_timeout()
            try:
                await host.start()
            except BaseException:
                await _shutdown_after_failed_start(host, title_updater)
                raise
            if warnings is not None:
                # Composed from the restore's committed load, not the
                # pre-restore snapshot above: the restore re-reads the same
                # root, and an edit landing between the two reads would
                # otherwise be reported stale — or a rejection only the
                # committed load hit would go unheard, since its bus warnings
                # have no subscriber here.
                warnings.extend(settings_warning_events(host.engine.loaded_settings))
            managed = ManagedSession(
                session_id=canonical_id,
                cwd=resolved_cwd,
                profile_name=host.engine.current_profile_snapshot().to_profile or meta.agent_profile,
                host=host,
                title_updater=title_updater,
            )
            self._sessions[canonical_id] = managed
            return ManagedLoadResult(
                session=managed,
                reused_existing=False,
                recovered_from_sidecar=host.engine.recovered_from_sidecar,
            )

    async def list_sessions(
        self,
        *,
        cwd: str | None,
        cursor: str | None,
        additional_directories: list[str] | None = None,
    ) -> tuple[list[acp_schema.SessionInfo], str | None]:
        """List saved sessions scoped to this ACP workspace.

        ``additional_directories`` is an exact, ordered additional-root filter (ACP
        workspace identity). When supplied with at least one root, a session matches
        only if its saved extra roots equal the request exactly; when omitted or
        empty, all sessions for ``cwd`` are returned (their roots are reported back
        via ``additionalDirectories``).
        """
        if cwd is None and self._process_cwd is None:
            return [], None
        requested_cwd = self._resolve_requested_cwd(cwd)
        matches = [
            meta
            for meta in cast(list[ChatSessionMeta], await self._state_store.list_sessions(kind="chat"))
            if _same_dir(meta.primary_cwd, requested_cwd)
        ]
        if additional_directories:
            # Validate filter roots like new/load: absolute, existing dirs. This also
            # normalizes them so the exact-match comparison is apples-to-apples.
            resolved_additional = [_resolve_dir(path) for path in additional_directories]
            matches = [meta for meta in matches if _extra_dirs_match(resolved_additional, meta)]
        sessions = sorted(
            matches,
            key=lambda meta: (meta.updated_at, meta.session_id),
            reverse=True,
        )
        start = _decode_cursor(cursor)
        page_size = 50
        page = sessions[start : start + page_size]
        next_cursor = str(start + page_size) if start + page_size < len(sessions) else None
        return [_session_info(meta) for meta in page], next_cursor

    def get(self, session_id: str) -> ManagedSession:
        """Return an active managed session."""
        session = self._sessions.get(session_id)
        if session is None or session.closing:
            msg = f"ACP session is not active: {session_id}"
            raise AcpSessionError(msg)
        return session

    def tool_kind_resolver(self, session_id: str) -> Callable[[str], str]:
        """Return a resolver for live tool kinds in an active session."""
        session = self.get(session_id)
        return session.host.engine.tool_kind_for_name

    async def cancel(self, session_id: str) -> None:
        """Cancel an active session turn.

        Deliberately tolerates a session already marked closing: close and
        delete stop prompt admission first and interrupt second, so the
        interrupt must still reach the turn that is being drained.
        """
        session = self._sessions.get(session_id)
        if session is None:
            msg = f"ACP session is not active: {session_id}"
            raise AcpSessionError(msg)
        await session.host.cancel_current_turn()

    async def inject(self, session_id: str, text: str) -> None:
        """Inject a prompt into the active turn (rejects when none is running)."""
        session = self.get(session_id)
        if not session.host.engine.is_turn_active:
            # Publishing UserMessage here would start a brand-new turn outside any
            # ACP session/prompt stream (no PromptResponse, no standard updates).
            # session/prompt is the path for new turns; injection requires an active one.
            msg = "No active turn to inject into."
            raise AcpSessionError(msg)
        # UserInject (not UserMessage) never starts a turn, so even if the turn ends
        # between the guard above and event delivery, this cannot spawn a stray run.
        await session.host.event_bus.publish(UserInject(text=text, session_id=session.host.session_id))

    async def rollback(
        self,
        session_id: str,
        *,
        target_turn: int,
        revert_changes: bool,
        selected_paths: list[str] | None,
    ) -> RollbackResult:
        """Request rollback and wait for the backend result."""
        session = self.get(session_id)
        bus = session.host.event_bus
        loop = asyncio.get_running_loop()
        result_future: asyncio.Future[RollbackResult] = loop.create_future()

        async def _on_result(event: RollbackResult) -> None:
            if event.session_id == session.host.session_id and not result_future.done():
                result_future.set_result(event)

        async def _on_error(event: Error) -> None:
            if event.session_id == session.host.session_id and not result_future.done():
                result_future.set_exception(AcpSessionError(event.message or event.code or "Rollback failed."))

        async def _on_warning(event: Warning) -> None:
            # Only rollback refusals (rollback_*) fail the RPC. A successful rollback
            # restores the session first, which can emit unrelated non-fatal warnings
            # (service_session_incompatible, sub_agents_reload_discarded) before the
            # RollbackResult — those must not be mistaken for a refusal.
            if (
                event.session_id == session.host.session_id
                and (event.code or "").startswith("rollback_")
                and not result_future.done()
            ):
                result_future.set_exception(AcpSessionError(event.message or event.code or "Rollback was refused."))

        await bus.subscribe(RollbackResult, _on_result)
        await bus.subscribe(Error, _on_error)
        await bus.subscribe(Warning, _on_warning)
        try:
            await bus.publish(
                UserRollback(
                    target_turn=target_turn,
                    revert_changes=revert_changes,
                    selected_paths=selected_paths,
                    session_id=session.host.session_id,
                )
            )
            return await asyncio.wait_for(result_future, timeout=30)
        except TimeoutError as exc:
            msg = "Timed out waiting for rollback result."
            raise AcpSessionError(msg) from exc
        finally:
            await bus.unsubscribe(RollbackResult, _on_result)
            await bus.unsubscribe(Error, _on_error)
            await bus.unsubscribe(Warning, _on_warning)

    async def retry_sub_agent(self, session_id: str, invocation_id: str) -> None:
        """Request retry for a paused sub-agent invocation."""
        session = self.get(session_id)
        await session.host.event_bus.publish(
            InvocationRetryRequested(invocation_id=invocation_id, session_id=session.host.session_id)
        )

    async def abort_sub_agent(self, session_id: str, invocation_id: str) -> None:
        """Request abort for a paused sub-agent invocation."""
        session = self.get(session_id)
        await session.host.event_bus.publish(
            InvocationAbortRequested(invocation_id=invocation_id, session_id=session.host.session_id)
        )

    async def skip_sleep(self, session_id: str, call_id: str) -> None:
        """Request that an active sleep tool call finish early."""
        if not call_id:
            msg = "call_id is required."
            raise AcpSessionError(msg)
        session = self.get(session_id)
        await session.host.event_bus.publish(SleepSkip(call_id=call_id))

    async def set_approval_mode(self, session_id: str, mode: str) -> ApprovalModeUpdated:
        """Set the runtime approval mode and wait for backend confirmation."""
        session = self.get(session_id)
        bus = session.host.event_bus

        async with session.mutation_lock:
            loop = asyncio.get_running_loop()
            result_future: asyncio.Future[ApprovalModeUpdated] = loop.create_future()

            async def _on_updated(event: ApprovalModeUpdated) -> None:
                if event.session_id == session.host.session_id and event.mode == mode and not result_future.done():
                    result_future.set_result(event)

            await bus.subscribe(ApprovalModeUpdated, _on_updated)
            try:
                await bus.publish(SetApprovalMode(mode=mode, persist=False, session_id=session.host.session_id))
                return await asyncio.wait_for(result_future, timeout=5)
            except TimeoutError as exc:
                msg = f"Timed out waiting for approval mode update: {mode}"
                raise AcpSessionError(msg) from exc
            finally:
                await bus.unsubscribe(ApprovalModeUpdated, _on_updated)

    async def switch_agent(self, session_id: str, profile_name: str) -> ProfileSwitched:
        """Switch the active session to another agent profile."""
        session = self.get(session_id)
        self.load_registries()
        profile = self._agent_registry.resolve_selector(profile_name)
        if profile is None:
            # Invalid profiles publish an Error rather than ProfileSwitched, so reject
            # here instead of waiting out the 60s timeout for an event that never comes.
            msg = f"Agent profile not found: {profile_name}"
            available = ", ".join(sorted(self._agent_registry.list_names()))
            if available:
                msg = f"{msg}. Available profiles: {available}"
            raise AcpSessionError(msg)
        async with session.mutation_lock:
            if session.profile_name == profile.name:
                return session.host.engine.current_profile_snapshot()
            result = await self._await_runtime_mutation_locked(
                session,
                AgentProfileSwitch(profile_name=profile.name, session_id=session.host.session_id),
                ProfileSwitched,
                timeout_message=f"Timed out waiting for profile switch: {profile.name}",
                failure_message="Profile switch failed.",
            )
            session.profile_name = result.to_profile or profile.name
            return result

    async def _await_runtime_mutation(
        self,
        session: ManagedSession,
        command: Event,
        result_type: type[_RuntimeEventT],
        *,
        timeout_message: str,
        failure_message: str,
        timeout: float = 60,
        warnings: list[Warning] | None = None,
    ) -> _RuntimeEventT:
        """Publish a runtime-control command and await its result event.

        Resolves on the success event, or fails fast on ``Error`` /
        ``AgentLoadFailed``. A rebuild failure publishes ``AgentLoadFailed`` and
        then raises inside the bus handler (swallowed by default), so without
        watching it the request would hang until the timeout.

        Args:
            warnings: Collects the ``Warning`` events this session publishes
                while the mutation runs. Passed as a list rather than returned
                so the caller decides whether the diagnosis is worth carrying —
                only the reload has anything to say.
        """
        # Serialize mutations on this session: two concurrent calls of the same
        # result type would otherwise each be eligible to resolve from whichever
        # completion event fires first, crossing the wires.
        async with session.mutation_lock:
            return await self._await_runtime_mutation_locked(
                session,
                command,
                result_type,
                timeout_message=timeout_message,
                failure_message=failure_message,
                timeout=timeout,
                warnings=warnings,
            )

    async def _await_runtime_mutation_locked(
        self,
        session: ManagedSession,
        command: Event,
        result_type: type[_RuntimeEventT],
        *,
        timeout_message: str,
        failure_message: str,
        timeout: float = 60,
        warnings: list[Warning] | None = None,
    ) -> _RuntimeEventT:
        """Publish a runtime-control command while the session mutation lock is held."""
        bus = session.host.event_bus
        sid = session.host.session_id
        loop = asyncio.get_running_loop()

        future: asyncio.Future[_RuntimeEventT] = loop.create_future()

        async def _on_result(event: _RuntimeEventT) -> None:
            if event.session_id == sid and not future.done():
                future.set_result(event)

        async def _on_error(event: Error) -> None:
            if event.session_id == sid and not future.done():
                future.set_exception(AcpSessionError(event.message or event.code or failure_message))

        async def _on_load_failed(event: AgentLoadFailed) -> None:
            if event.session_id == sid and not future.done():
                future.set_exception(AcpSessionError(event.message or failure_message))

        async def _on_warning(event: Warning) -> None:
            if event.session_id == sid and warnings is not None:
                warnings.append(event)

        await bus.subscribe(result_type, _on_result)
        await bus.subscribe(Error, _on_error)
        await bus.subscribe(AgentLoadFailed, _on_load_failed)
        await bus.subscribe(Warning, _on_warning)
        try:
            await bus.publish(command)
            return await asyncio.wait_for(future, timeout=timeout)
        except TimeoutError as exc:
            raise AcpSessionError(timeout_message) from exc
        finally:
            await bus.unsubscribe(result_type, _on_result)
            await bus.unsubscribe(Error, _on_error)
            await bus.unsubscribe(AgentLoadFailed, _on_load_failed)
            await bus.unsubscribe(Warning, _on_warning)

    async def set_model_profile(self, session_id: str, profile_id: str) -> ModelProfileSwitched:
        """Switch the active model for this session only (no global .env write)."""
        self.load_registries()
        if not profile_id or self._model_registry.get(profile_id) is None:
            msg = f"Model profile not found: {profile_id}"
            raise AcpSessionError(msg)
        session = self.get(session_id)
        return await self._await_runtime_mutation(
            session,
            SetModelProfile(profile_id=profile_id, session_id=session.host.session_id),
            ModelProfileSwitched,
            timeout_message=f"Timed out waiting for model switch: {profile_id}",
            failure_message="Model switch failed.",
        )

    async def session_history(self, *, cwd: str | None, session_id: str) -> tuple[str, list[dict[str, object]] | None]:
        """Return raw persisted messages, scoped to the requesting workspace cwd.

        Mirrors ``load``/``delete`` scoping so a client cannot read another
        workspace's session history just by knowing its id.
        """
        resolved_cwd = self._resolve_requested_cwd(cwd)
        existing = self._resolve_active_session(session_id)
        if existing is not None:
            self._validate_active_cwd(existing, resolved_cwd)
            messages = await self._state_store.load_session_raw(
                existing.session_id,
                prefer_recovery=existing.host.engine.recovered_from_sidecar,
            )
            return existing.session_id, messages
        canonical_id, meta = await self._resolve_session_meta(session_id)
        self._validate_saved_cwd(meta, resolved_cwd)
        prefer_recovery = await self._meta_resolved_from_recovery(canonical_id, meta)
        return canonical_id, await self._state_store.load_session_raw(canonical_id, prefer_recovery=prefer_recovery)

    async def reload_settings(self, session_id: str, *, warnings: list[Warning] | None = None) -> SettingsReloaded:
        """Reload settings/profile files for an active session.

        Awaits the rebuild so a failed soft-restart (``AgentLoadFailed``, which
        the bus otherwise swallows) surfaces as an error instead of a silent
        success — mirroring the model/workspace/profile mutations.

        Args:
            warnings: Collects the settings this reload had to reject. A reload
                is a request whose whole point is "apply what I just wrote", so
                a client that is told it succeeded and never told which of its
                values was dropped has been misinformed. Nothing else on the bus
                is listening: warnings published outside a prompt turn reach no
                subscriber, and the bus does not replay them to later ones.
        """
        session = self.get(session_id)
        self._agent_registry.load_all()
        self._model_registry.load_all()
        result = await self._await_runtime_mutation(
            session,
            SettingsReload(),
            SettingsReloaded,
            timeout_message="Timed out waiting for settings reload.",
            failure_message="Settings reload failed.",
            warnings=warnings,
        )
        await asyncio.to_thread(self._refresh_default_settings_from_env)
        return result

    async def apply_config_option(
        self,
        session_id: str,
        key: str,
        value: object,
        *,
        warnings: list[Warning] | None = None,
    ) -> dict[str, object]:
        """Persist a config option and reload the target session.

        The session is validated *before* the document write so a missing,
        stale, or inactive ``session_id`` fails the request without leaving the
        persisted config mutated. The write itself is global — it lands in the
        user settings document, whose keys are process-wide defaults — and the
        reload applies it to the live session.

        Args:
            warnings: Collects what the reload had to reject or clamp, for the
                same reason ``reload_settings`` does — more so here, because
                this path *persists* the value first. Without it a client that
                sets ``rollback_snapshots_keep`` to ``0`` is told it succeeded,
                the ``0`` is written to disk, and the runtime quietly uses ``1``.
        """
        self.get(session_id)
        # Off-thread: the write takes the settings file's lock and the refresh
        # reads the file back, and this server answers every other request on
        # the same event loop.
        result = await asyncio.to_thread(self.set_config_option, key, value)
        await self.reload_settings(session_id, warnings=warnings)
        return result

    async def set_workspace(self, session_id: str, primary_cwd: str) -> WorkspaceUpdated:
        """Change the session primary workspace cwd."""
        session = self.get(session_id)
        # Validate strictly (must exist, be a dir, resolve absolutely) like new/load.
        # WorkspaceChange → Workspace.from_cwd only absolutizes, so an unvalidated typo
        # would rebuild/persist a broken workspace or resolve a relative path against the
        # process cwd instead of the session.
        resolved_cwd = _resolve_dir(primary_cwd)
        result = await self._await_runtime_mutation(
            session,
            WorkspaceChange(primary_cwd=resolved_cwd, session_id=session.host.session_id),
            WorkspaceUpdated,
            timeout_message=f"Timed out waiting for workspace update: {resolved_cwd}",
            failure_message="Workspace update failed.",
        )
        session.cwd = result.primary_cwd or resolved_cwd
        return result

    async def begin_close(self, session_id: str) -> None:
        """Stop prompt admission for a session about to be closed.

        Callers release pending client waits between this call and
        :meth:`close`. Marking the session closing first means a prompt queued
        on the session's prompt lock fails its post-acquisition re-check
        instead of being admitted into a fresh turn that the lock-protected
        teardown would then wedge behind. No-op when the session is not
        active.
        """
        async with self._lock:
            session = self._sessions.get(session_id)
            if session is not None:
                session.closing = True

    async def close(self, session_id: str) -> None:
        """Close an active session."""
        async with self._lock:
            session = self._sessions.get(session_id)
            if session is not None:
                session.closing = True
                async with session.prompt_lock:
                    self._sessions.pop(session_id, None)
                    await session.shutdown()

    async def begin_delete_session(self, *, cwd: str | None, session_id: str) -> str:
        """Validate a delete request, stop prompt admission, and return the canonical id.

        Applies workspace scoping and short-id canonicalization, then marks an
        active target closing — validation first, inside one lock critical
        section, so a rejected request has no side effects. Callers release
        per-session waits under the canonical id next and finish with
        :meth:`finish_delete_session`; a prompt queued behind the released
        turn then fails its re-check instead of starting a turn the teardown
        would wedge behind.
        """
        async with self._lock:
            resolved_cwd = self._resolve_requested_cwd(cwd)
            canonical_id, meta = await self._resolve_session_meta(session_id)
            self._validate_saved_cwd(meta, resolved_cwd)
            session = self._sessions.get(canonical_id)
            if session is not None:
                session.closing = True
            return canonical_id

    async def finish_delete_session(self, canonical_id: str) -> None:
        """Tear down and delete a session claimed by :meth:`begin_delete_session`."""
        async with self._lock:
            session = self._sessions.get(canonical_id)
            if session is not None:
                session.closing = True
                async with session.prompt_lock:
                    self._sessions.pop(canonical_id, None)
                    await session.shutdown()
            await self._state_store.delete_session(canonical_id)

    async def delete_session(self, *, cwd: str | None, session_id: str) -> None:
        """Delete a saved session scoped to this ACP process cwd."""
        canonical_id = await self.begin_delete_session(cwd=cwd, session_id=session_id)
        await self.finish_delete_session(canonical_id)

    async def shutdown(self) -> None:
        """Close every active session."""
        sessions = list(self._sessions.values())
        self._sessions.clear()
        for session in sessions:
            session.closing = True
        for session in sessions:
            async with session.prompt_lock:
                await session.shutdown()

    def _resolve_requested_cwd(self, cwd: str | None) -> str:
        if cwd is None:
            if self._process_cwd is not None:
                if not os.path.isdir(self._process_cwd):
                    msg = f"working directory no longer exists: {surrogate_safe_text(self._process_cwd)}"
                    raise AcpSessionError(msg)
                return self._process_cwd
            msg = "cwd is required when this ACP process was not started with --workdir."
            raise AcpSessionError(msg)
        return _resolve_dir(cwd)

    async def _resolve_session_meta(self, session_id: str) -> tuple[str, ChatSessionMeta]:
        raw = (session_id or "").strip()
        if not raw:
            msg = "session_id is required."
            raise AcpSessionError(msg)
        sessions = cast(list[ChatSessionMeta], await self._state_store.list_sessions(kind="chat"))
        normalized = raw.replace("-", "").lower()
        if len(normalized) <= SESSION_SHORT_ID_LEN:
            matches = [meta for meta in sessions if session_short_id(meta.session_id).lower() == normalized]
        else:
            matches = [
                meta
                for meta in sessions
                if meta.session_id == raw or meta.session_id.replace("-", "").lower() == normalized
            ]
        if len(matches) == 1:
            return matches[0].session_id, matches[0]
        if len(matches) > 1:
            raise AcpSessionError(f"Session id '{raw}' is ambiguous.")
        raise AcpSessionError(f"Session not found: {raw}")

    async def _meta_resolved_from_recovery(self, session_id: str, meta: ChatSessionMeta) -> bool:
        """Return whether lock-aware listing metadata came from the recovery sidecar."""
        primary_meta = await self._state_store.load_session_meta(session_id, prefer_recovery=False)
        if primary_meta is None:
            return True
        return meta.updated_at > primary_meta.updated_at

    def _resolve_active_session(self, session_id: str) -> ManagedSession | None:
        """Resolve an already-active session without consulting persisted session listings."""
        raw = (session_id or "").strip()
        if not raw:
            msg = "session_id is required."
            raise AcpSessionError(msg)
        direct = self._sessions.get(raw)
        if direct is not None:
            return self._reject_closing(direct)
        normalized = raw.replace("-", "").lower()
        if len(normalized) <= SESSION_SHORT_ID_LEN:
            matches = [
                session
                for session in self._sessions.values()
                if session_short_id(session.session_id).lower() == normalized
            ]
        else:
            matches = [
                session
                for session in self._sessions.values()
                if session.session_id == raw or session.session_id.replace("-", "").lower() == normalized
            ]
        if len(matches) == 1:
            return self._reject_closing(matches[0])
        if len(matches) > 1:
            raise AcpSessionError(f"Session id '{raw}' is ambiguous.")
        return None

    @staticmethod
    def _reject_closing(session: ManagedSession) -> ManagedSession:
        """Refuse to hand out a session that a close/delete is tearing down.

        A closing session stays in the map only while pending waits drain.
        Returning it would report a successful reuse for a session about to
        shut down — and callers must not treat it as inactive and fall
        through to a fresh load either: that would overwrite the live map
        entry the in-flight teardown is about to pop, so the teardown would
        shut down the replacement. Raising is the only safe answer until the
        teardown finishes.
        """
        if session.closing:
            msg = f"ACP session is closing: {session.session_id}"
            raise AcpSessionError(msg)
        return session

    def _validate_saved_cwd(self, meta: ChatSessionMeta, requested_cwd: str) -> None:
        if not meta.primary_cwd:
            msg = (
                f"Session '{session_short_id(meta.session_id)}' has no saved cwd metadata and cannot be loaded "
                "over ACP."
            )
            raise AcpSessionError(msg)
        if not os.path.isdir(meta.primary_cwd):
            msg = (
                f"working directory of session '{session_short_id(meta.session_id)}' no longer exists: "
                f"{surrogate_safe_text(meta.primary_cwd)}"
            )
            raise AcpSessionError(msg)
        if not _same_dir(meta.primary_cwd, requested_cwd):
            logger.warning(
                "ACP session load cwd mismatch for %s: saved cwd %s requested cwd %s",
                session_short_id(meta.session_id),
                meta.primary_cwd,
                requested_cwd,
            )
            msg = f"Session '{session_short_id(meta.session_id)}' belongs to a different workspace."
            raise AcpSessionError(msg)

    def _validate_active_cwd(self, session: ManagedSession, requested_cwd: str) -> None:
        if not _same_dir(session.cwd, requested_cwd):
            logger.warning(
                "ACP active session load cwd mismatch for %s: active cwd %s requested cwd %s",
                session_short_id(session.session_id),
                session.cwd,
                requested_cwd,
            )
            msg = (
                f"Session '{session_short_id(session.session_id)}' belongs to cwd '{session.cwd}', "
                f"not requested cwd '{requested_cwd}'."
            )
            raise AcpSessionError(msg)

    def _session_loaded_settings(self, cwd: str) -> LoadedSettings:
        """Derive one session's settings under its own project trust domain.

        The manager's base settings are deliberately project-free — a
        manager-level project layer would leak one session's trust decisions
        into every other — so each session re-loads with its resolved cwd as
        the project root. Everything process-wide still flows from the base:
        RESTART values are held at what this process is running on, the
        command line keeps the credit for what it decided, and the launch-time
        ``--ask-user-timeout`` is carried across as the CLI layer it is, since
        a re-read of the environment cannot produce it. Under the global
        settings lock so the base read cannot straddle a concurrent write's
        document/publish gap.
        """
        with self._global_settings_lock:
            base = self._loaded_settings
            candidate = load_settings(
                project_root=Path(cwd),
                eval_context=EvalContext(
                    frontend_default_max_transient_retries=base.settings.frontend_default_max_transient_retries
                ),
            )
            loaded, _deferred = route_restart_settings(reattribute_command_line(candidate, base), base)
            return loaded.overlay(Source.CLI, ask_user_timeout_seconds=base.settings.ask_user_timeout_seconds)

    def _refresh_default_settings_from_env(self) -> None:
        # Read, route and publish as one step. Every writer refreshes after its
        # own write, so serializing this makes the last refresh to publish also
        # the last to read — without the lock, two clients configuring two
        # sessions at once can have the slower one publish a snapshot taken
        # before the faster one's write, and every session created afterwards
        # runs on settings that are on disk but not in this manager. Taken here
        # rather than only at the write sites because a reload refreshes too.
        with self._global_settings_lock:
            # The launch-time ``--ask-user-timeout`` is a CLI value, not something a
            # re-read of the environment can produce, so it is carried across as the
            # layer it came from rather than folded into the new load.
            timeout = self._loaded_settings.settings.ask_user_timeout_seconds
            # RESTART routing applies to future sessions too: one process, one
            # value, so a host built after this refresh must not act on a value
            # the already-running hosts cannot. The deferred keys need no report
            # here — the session that triggered the refresh had its own reload,
            # which already said so on its bus.
            loaded, _deferred = route_restart_settings(load_settings(), self._loaded_settings)
            self._loaded_settings = loaded.overlay(Source.CLI, ask_user_timeout_seconds=timeout)


def _mcp_overlay(
    servers: list[acp_schema.HttpMcpServer | acp_schema.SseMcpServer | acp_schema.McpServerStdio] | None,
) -> list[MCPServerConfig]:
    configs: list[MCPServerConfig] = []
    for server in servers or []:
        if isinstance(server, acp_schema.HttpMcpServer):
            configs.append(
                MCPServerConfig(
                    name=server.name,
                    transport="http",
                    url=server.url,
                    headers={header.name: header.value for header in server.headers},
                    resolve_header_templates=False,
                )
            )
            continue
        if isinstance(server, acp_schema.SseMcpServer):
            msg = f"SSE MCP servers are not supported by {APP_DISPLAY_NAME} ACP phase 1."
            raise AcpSessionError(msg)
        if isinstance(server, acp_schema.McpServerStdio):
            msg = f"Client-supplied stdio MCP servers are not supported by {APP_DISPLAY_NAME} ACP phase 1."
            raise AcpSessionError(msg)
        msg = f"Unsupported MCP server type: {type(server).__name__}"
        raise AcpSessionError(msg)
    return configs


async def _shutdown_after_failed_start(
    host: ChrysSessionHost, title_updater: SessionTitleUpdater | None = None
) -> None:
    try:
        await host.shutdown()
    except Exception:
        logger.exception("Error shutting down ACP session host after failed startup")
    if title_updater is not None:
        with contextlib.suppress(Exception):
            await title_updater.shutdown()


def _resolve_dir(path: str) -> str:
    raw = (path or "").strip()
    if not raw:
        msg = "cwd is required."
        raise AcpSessionError(msg)
    expanded = Path(raw).expanduser()
    if not expanded.is_absolute():
        # ACP requires absolute paths. Resolving a relative path against the ACP
        # server's process cwd would silently bind the session to the wrong
        # workspace, so reject it instead of guessing.
        msg = f"path must be absolute: {raw}"
        raise AcpSessionError(msg)
    try:
        resolved = expanded.resolve(strict=True)
    except FileNotFoundError, NotADirectoryError:
        msg = f"cwd does not exist: {surrogate_safe_text(raw)}"
        raise AcpSessionError(msg) from None
    except OSError:
        msg = f"cwd is not accessible: {surrogate_safe_text(raw)}"
        raise AcpSessionError(msg) from None
    if not resolved.is_dir():
        msg = f"cwd is not a directory: {resolved}"
        raise AcpSessionError(msg)
    return os.fspath(resolved)


def _same_dir(left: str, right: str) -> bool:
    if not left or not right:
        return False
    try:
        return Path(left).expanduser().resolve(strict=True) == Path(right).expanduser().resolve(strict=True)
    except OSError:
        return False


def _workspace_from_dirs(primary_cwd: str, additional_directories: list[str] | None) -> Workspace:
    dirs = [WorkingDir(path=primary_cwd, is_primary=True)]
    for path in additional_directories or []:
        resolved = _resolve_dir(path)
        if not _same_dir(resolved, primary_cwd):
            dirs.append(WorkingDir(path=resolved))
    return Workspace(primary_cwd=primary_cwd, working_dirs=dirs)


def _agent_profile_from_data(data: dict[str, object]) -> AgentProfile:
    with tempfile.TemporaryDirectory(prefix="chrys-agent-profile-") as temp_dir:
        stem = _safe_profile_file_stem(data.get("name", "profile"), field_name="agent profile name")
        path = Path(temp_dir) / f"{stem}.yaml"
        path.write_text(dump_yaml(data), encoding="utf-8")
        return load_agent_profile_from_yaml(path)


def _model_profile_from_data(data: dict[str, object]) -> ModelProfile:
    with tempfile.TemporaryDirectory(prefix="chrys-model-profile-") as temp_dir:
        stem = _safe_profile_file_stem(data.get("id", "model"), field_name="model profile id")
        path = Path(temp_dir) / f"{stem}.yaml"
        path.write_text(dump_yaml(data), encoding="utf-8")
        return load_model_profile_from_yaml(path)


def _mcp_config_from_data(data: dict[str, object]) -> MCPServerConfig:
    payload = dict(data)
    if "name" not in payload:
        msg = "MCP server name is required."
        raise AcpSessionError(msg)
    transport = payload.get("transport")
    if transport == "stdio":
        msg = f"Client-supplied stdio MCP servers are not supported by {APP_DISPLAY_NAME} ACP."
        raise AcpSessionError(msg)
    if transport != "http":
        msg = "mcp/test supports HTTP MCP servers only."
        raise AcpSessionError(msg)
    # ACP client-supplied headers must stay literal; profile-owned YAML is the
    # only MCP path that resolves {{ENV_VAR}} templates.
    payload["resolve_header_templates"] = False
    agent = _agent_profile_from_data(
        {
            "name": "MCP Test",
            "id": "mcp-test",
            "tools": {"mcp": [payload]},
        }
    )
    if not agent.tools.mcp:
        msg = "MCP server config is invalid."
        raise AcpSessionError(msg)
    return agent.tools.mcp[0]


def _config_setting_fields() -> dict[str, str]:
    """Map each supported setting key to its ``Settings`` attribute name."""
    from chrys.foundation.config.settings import Settings
    from chrys.foundation.config.spec import field_names_by_key

    names = field_names_by_key(Settings)
    return {option.setting_key: names[option.setting_key] for option in _SUPPORTED_CONFIG_OPTIONS}


def _config_display_value(stored: object) -> str:
    """Render a stored value the way the wire protocol always has: as text."""
    if stored is None or stored == "":
        return ""
    if stored is True or stored is False:
        return "1" if stored else "0"
    return str(stored)


def _config_option(key: str) -> AcpConfigOption:
    normalized = key.strip()
    for option in _SUPPORTED_CONFIG_OPTIONS:
        if normalized in (option.logical_key, option.env_alias):
            return option
    msg = f"Unsupported config option: {key}"
    raise AcpSessionError(msg)


def _decode_cursor(cursor: str | None) -> int:
    if not cursor:
        return 0
    try:
        return max(0, int(cursor))
    except ValueError as exc:
        msg = f"Invalid cursor: {cursor}"
        raise AcpSessionError(msg) from exc


def _session_extra_dirs(meta: ChatSessionMeta) -> list[str]:
    # meta.working_dirs stores the full workspace list, which includes the primary
    # cwd for ACP multi-dir sessions. ``cwd`` already carries it, so the additional
    # roots are everything except the primary.
    return [d for d in meta.working_dirs if not _same_dir(d, meta.primary_cwd)]


def _extra_dirs_match(requested: list[str], meta: ChatSessionMeta) -> bool:
    """Exact, ordered match of a session's additional roots against a request."""
    saved = _session_extra_dirs(meta)
    if len(requested) != len(saved):
        return False
    return all(_same_dir(req, have) for req, have in zip(requested, saved, strict=True))


def _session_info(meta: ChatSessionMeta) -> acp_schema.SessionInfo:
    extra_dirs = _session_extra_dirs(meta)
    return acp_schema.SessionInfo(
        sessionId=meta.session_id,
        cwd=meta.primary_cwd,
        title=meta.display_title or None,
        updatedAt=meta.updated_at.isoformat(),
        additionalDirectories=extra_dirs or None,
        field_meta={
            "agent_profile": meta.agent_profile,
            "agent_display_name": meta.agent_display_name,
            "message_count": meta.message_count,
            "model_provider": meta.model_provider,
            "model_api_style": meta.model_api_style,
            "model_id": meta.model_id,
        },
    )


def _agent_profile_info(profile: AgentProfile, *, builtin: bool) -> dict[str, object]:
    return {
        "id": profile.id,
        "name": profile.name,
        "displayName": profile.display_name,
        "description": profile.description,
        "subAgentOnly": profile.sub_agent_only,
        "builtin": builtin,
    }


def _agent_profile_reset_comparison_dict(profile: AgentProfile) -> dict[str, Any]:
    """Serialize after normalizing the order-insensitive Skills extension list."""
    normalized = deepcopy(profile)
    normalized.skills.script_extensions = sorted(normalized.skills.script_extensions)
    return agent_profile_to_dict(normalized)


_MASK = "***"


def _is_string_object_dict(value: object) -> TypeGuard[dict[str, object]]:
    """Narrow ACP JSON objects, whose keys are necessarily strings."""
    return isinstance(value, dict) and all(isinstance(key, str) for key in value)


def _iter_profile_mcp_servers(data: dict[str, object]) -> list[dict[str, object]]:
    raw_tools = data.get("tools")
    if not _is_string_object_dict(raw_tools):
        return []
    tools = raw_tools
    servers = tools.get("mcp")
    if not isinstance(servers, list):
        return []
    return [server for server in servers if _is_string_object_dict(server)]


def _redact_agent_profile_secrets(data: dict[str, object]) -> dict[str, object]:
    """Mask MCP and ACP launch secrets, mirroring model-profile api_key masking."""
    for server in _iter_profile_mcp_servers(data):
        for field_name in ("headers", "env"):
            mapping = server.get(field_name)
            if isinstance(mapping, dict):
                server[field_name] = {key: (_MASK if value else value) for key, value in mapping.items()}
    raw_acp = data.get("acp")
    if _is_string_object_dict(raw_acp):
        acp = raw_acp
        env = acp.get("env")
        if isinstance(env, dict):
            acp["env"] = dict.fromkeys(env, _MASK)
        args = acp.get("args")
        if isinstance(args, list):
            acp["args"] = [_MASK for _value in args]
        options = acp.get("config_options")
        if isinstance(options, dict):
            acp["config_options"] = {
                key: (_MASK if isinstance(value, str) else value) for key, value in options.items()
            }
    return data


def _restore_masked_mcp_secrets(data: dict[str, object], existing: AgentProfile) -> None:
    """Replace masked ("***") MCP header/env values with the stored originals."""
    prior_by_name = {server.name: server for server in existing.tools.mcp}
    for server in _iter_profile_mcp_servers(data):
        prior = prior_by_name.get(server.get("name"))
        if prior is None:
            continue
        _restore_masked_values(server.get("headers"), prior.headers)
        _restore_masked_values(server.get("env"), prior.env)


def _restore_masked_values(incoming: object, prior: dict[str, str]) -> None:
    if not _is_string_object_dict(incoming):
        return
    incoming_values = incoming
    for key, value in incoming_values.items():
        if value == _MASK and key in prior:
            incoming_values[key] = prior[key]


@dataclass(frozen=True)
class _AcpMaskRestoration:
    """Which masked ACP fields were restored from the stored profile.

    The unresolved-mask rejection must skip restored keys: a restored value can
    legitimately EQUAL the mask when the stored secret is the literal ``***``.
    """

    args_restored: bool = False
    args_were_masked: bool = False
    """True when the stored profile has a non-empty args list, i.e. the read
    that produced this write masked every element — any leftover mask in an
    edited list is then a placeholder, never literal user input."""
    env_keys: frozenset[str] = frozenset()
    option_keys: frozenset[str] = frozenset()


def _restore_masked_acp_secrets(data: dict[str, object], existing: AgentProfile) -> _AcpMaskRestoration:
    """Restore masks only where ACP fields retain an unambiguous identity."""
    raw_incoming = data.get("acp")
    prior = existing.acp
    if not _is_string_object_dict(raw_incoming) or prior is None:
        return _AcpMaskRestoration()
    incoming = raw_incoming
    env_keys: set[str] = set()
    raw_env = incoming.get("env")
    if _is_string_object_dict(raw_env):
        env = raw_env
        for key, value in env.items():
            if value == _MASK and key in prior.env:
                env[key] = prior.env[key]
                env_keys.add(key)
    option_keys: set[str] = set()
    raw_options = incoming.get("config_options")
    if _is_string_object_dict(raw_options):
        options = raw_options
        for key, value in options.items():
            prior_value = prior.config_options.get(key)
            if value == _MASK and isinstance(prior_value, str):
                options[key] = prior_value
                option_keys.add(key)
    args = incoming.get("args")
    args_restored = False
    if isinstance(args, list) and len(args) == len(prior.args) and all(value == _MASK for value in args):
        incoming["args"] = list(prior.args)
        args_restored = True
    return _AcpMaskRestoration(args_restored, bool(prior.args), frozenset(env_keys), frozenset(option_keys))


def _reject_unresolved_masked_mcp_secrets(data: dict[str, object]) -> None:
    for server in _iter_profile_mcp_servers(data):
        name = server.get("name") or "<unnamed>"
        for field_name in ("headers", "env"):
            mapping = server.get(field_name)
            if not isinstance(mapping, dict):
                continue
            for key, value in mapping.items():
                if value == _MASK:
                    msg = (
                        f"MCP server {name!r} {field_name}.{key} is still masked; "
                        "re-enter the secret after renaming or adding the server."
                    )
                    raise AcpSessionError(msg)


def _reject_unresolved_masked_acp_secrets(
    data: dict[str, object],
    *,
    restoration: _AcpMaskRestoration | None = None,
) -> None:
    restoration = restoration or _AcpMaskRestoration()
    acp = data.get("acp")
    if not isinstance(acp, dict):
        return
    args = acp.get("args")
    if isinstance(args, list) and not restoration.args_restored:
        msg = (
            "ACP args still contain masked values; after editing the positional list, "
            "resubmit the complete unmasked argument list."
        )
        # After a masked read of a non-empty stored list, positional identity
        # is lost the moment the list is edited: ANY leftover mask (e.g.
        # ["***", "***", "--verbose"]) is a placeholder whose acceptance would
        # silently persist the literal mask over the stored secrets.
        if restoration.args_were_masked and any(value == _MASK for value in args):
            raise AcpSessionError(msg)
        # Without a masked read (new profile, rename, empty stored list) a
        # mask can only be literal user input; reject only the fully-masked
        # placeholder shape.
        if args and all(value == _MASK for value in args):
            raise AcpSessionError(msg)
    for field_name, restored_keys in (("env", restoration.env_keys), ("config_options", restoration.option_keys)):
        mapping = acp.get(field_name)
        if not isinstance(mapping, dict):
            continue
        for key, value in mapping.items():
            if value == _MASK and key not in restored_keys:
                msg = f"ACP {field_name}.{key} is still masked; re-enter the secret after renaming or adding it."
                raise AcpSessionError(msg)


def _safe_profile_file_stem(value: object, *, field_name: str) -> str:
    if not isinstance(value, str):
        msg = f"{field_name} must be a string."
        raise AcpSessionError(msg)
    stem = value.strip()
    if not stem:
        msg = f"{field_name} is required."
        raise AcpSessionError(msg)
    if not is_filename_safe_profile_name(stem):
        msg = f"{field_name} must be a filename-safe value, not a path."
        raise AcpSessionError(msg)
    return stem


def _model_profile_info(profile: ModelProfile) -> dict[str, object]:
    return {
        "id": profile.id,
        "name": profile.name,
        "provider": profile.provider,
        "apiStyle": profile.api_style,
        "modelId": profile.model_id,
        "maxContextTokens": profile.max_context_tokens,
        "stream": profile.stream,
        "vision": profile.vision,
    }


def jsonable_dataclass(value: object) -> dict[Any, Any]:
    if is_dataclass(value):
        if isinstance(value, type):
            # Match dataclasses.asdict's existing class-vs-instance failure.
            raise TypeError("asdict() should be called on dataclass instances")
        return asdict(value)
    if isinstance(value, dict):
        return value
    return {}
