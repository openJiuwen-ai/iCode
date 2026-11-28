# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Manifest admission: the spec digest, and the agent/model binding every agent node needs before a run starts.

Admission is deterministic and runs no user code. It validates the data-only
manifest as a graph, then binds each agent node to the profile and model the
run will use. The binding snapshot is what ``run.json`` and
``WorkflowRunStarted`` carry, so the user sees exactly which agent and model
each node runs on, and the shell opens the same objects at activation time.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Final

from chrys.foundation.config.settings import Settings
from chrys.service.llm.clients import effective_model_base_url
from chrys.service.profiles.agents.loader import read_profile_dir as read_agent_profile_dir
from chrys.service.profiles.agents.registry import AgentProfileRegistry
from chrys.service.profiles.agents.schema import AgentProfile
from chrys.service.profiles.diagnostics import (
    profile_miss_facts,
    profile_miss_sentence,
    selectable_model_labels,
)
from chrys.service.profiles.models.loader import read_profile_dir as read_model_profile_dir
from chrys.service.profiles.models.registry import ModelProfileRegistry
from chrys.service.profiles.models.resolver import resolve_for_agent, resolve_selectable_profile
from chrys.service.profiles.models.schema import ModelProfile, is_model_profile_selectable
from chrys.service.session.sub_agent_logs import sanitize_base_url_origin
from chrys.service.workflows.graph import KIND_AGENT, GraphSpec, ManifestError
from chrys.service.workflows.values import canonical_json

REJECT_MANIFEST_INVALID: Final = "manifest_invalid"
REJECT_AGENT_PROFILE_MISSING: Final = "agent_profile_missing"
REJECT_MODEL_UNRESOLVABLE: Final = "model_unresolvable"


class AdmissionError(Exception):
    """The manifest cannot be run against the current registries; ``code`` is the rejection reason.

    ``node_id`` names the agent node that could not be bound, when one is at fault.
    """

    def __init__(self, code: str, message: str, *, node_id: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.node_id = node_id


def spec_digest(entry_digest: str, manifest_digest: str, schema_version: int) -> str:
    """One digest over the triple the worker reports; the ledger and every run record pin this."""
    payload = canonical_json({"entry": entry_digest, "manifest": manifest_digest, "schema": schema_version})
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class AgentBinding:
    """The profile and model an agent node runs on, plus the data-only snapshot the run records."""

    node_id: str
    agent: AgentProfile
    model: ModelProfile | None
    instructions_suffix: str

    def snapshot(self) -> dict[str, Any]:
        model = self.model
        acp = self.agent.acp
        return {
            "node_id": self.node_id,
            "agent": self.agent.name,
            "agent_id": self.agent.id,
            "agent_display_name": self.agent.display_name or self.agent.name,
            "acp": self.agent.acp is not None,
            "model_profile_id": model.id if model is not None else "",
            "model_profile_name": model.name if model is not None else "",
            "provider": model.provider if model is not None else "",
            "api_style": model.api_style if model is not None else "",
            "model_id": model.model_id if model is not None else acp.model_id if acp is not None else "",
            "base_url_origin": sanitize_base_url_origin(effective_model_base_url(model)) if model is not None else "",
            "instructions_suffix": self.instructions_suffix,
        }


@dataclass(frozen=True, slots=True)
class AdmittedManifest:
    graph: GraphSpec
    bindings: Mapping[str, AgentBinding]

    def binding(self, node_id: str) -> AgentBinding:
        return self.bindings[node_id]

    def resolved_nodes(self) -> list[dict[str, Any]]:
        return [binding.snapshot() for binding in self.bindings.values()]


def admit_manifest(
    manifest: Mapping[str, Any],
    *,
    agent_registry: AgentProfileRegistry,
    model_registry: ModelProfileRegistry | None,
    settings: Settings,
) -> AdmittedManifest:
    """Validate the manifest as a graph and bind every agent node, in node order.

    Agent nodes name a profile by selector (id, name or display name); a node
    that names a model resolves it as a selectable profile, otherwise the
    profile's own model binding applies exactly as it does for a sub-agent.
    ACP profiles use their remote model id unless the node explicitly overrides
    it with a selectable Chrys model profile; no kernel client is constructed.
    """
    try:
        graph = GraphSpec.from_manifest(manifest)
    except ManifestError as exc:
        raise AdmissionError(REJECT_MANIFEST_INVALID, str(exc)) from exc
    bindings: dict[str, AgentBinding] = {}
    for node_id in graph.node_order:
        node = graph.nodes[node_id]
        if node.kind != KIND_AGENT:
            continue
        agent = node.agent
        if agent is None:
            raise RuntimeError("A validated agent node has no agent specification.")
        selector = agent.profile
        profile = agent_registry.resolve_selector(selector)
        if profile is None:
            user_dir = agent_registry.user_directory
            scan = read_agent_profile_dir(user_dir) if user_dir is not None else None
            facts = profile_miss_facts(selector, agent_registry.list_names(), scan)
            raise AdmissionError(
                REJECT_AGENT_PROFILE_MISSING,
                # The caller's hint line already lists the available agent
                # profiles, so repeating them here would print one list twice.
                # Unreadable files are still reported, since nothing else does.
                profile_miss_sentence(
                    facts,
                    base=f"Node {node_id!r} names agent profile {selector!r}, which is not available",
                    include_available=False,
                ),
                node_id=node_id,
            )
        model_selector = agent.model
        if profile.acp is not None and not model_selector:
            bindings[node_id] = AgentBinding(node_id, profile, None, agent.instructions_suffix)
            continue
        if model_selector is not None:
            model = resolve_selectable_profile(model_registry, model_selector)
            if model is None:
                user_dir = model_registry.user_directory if model_registry is not None else None
                scan = read_model_profile_dir(user_dir) if user_dir is not None else None
                facts = profile_miss_facts(model_selector, _selectable_model_labels(model_registry), scan)
                raise AdmissionError(
                    REJECT_MODEL_UNRESOLVABLE,
                    profile_miss_sentence(
                        facts,
                        base=f"Node {node_id!r} names model profile {model_selector!r}, which is not available",
                        available_label="Available model profiles",
                    ),
                    node_id=node_id,
                )
        else:
            model = resolve_for_agent(model_registry, settings, profile)
            if not is_model_profile_selectable(model):
                # A corrupt model file reads exactly like an unconfigured machine, so
                # name it: the agent's own binding cannot be fixed by choosing another.
                selector = profile.model.profile_id if profile.model else ""
                user_dir = model_registry.user_directory if model_registry is not None else None
                scan = read_model_profile_dir(user_dir) if user_dir is not None else None
                facts = profile_miss_facts(selector, _selectable_model_labels(model_registry), scan)
                raise AdmissionError(
                    REJECT_MODEL_UNRESOLVABLE,
                    profile_miss_sentence(
                        facts,
                        base=(
                            f"Node {node_id!r} has no usable model: agent profile {profile.name!r} resolves to no model"
                        ),
                        available_label="Available model profiles",
                        # The old message listed nothing; keep that shape so only the
                        # unreadable-file notes are new.
                        include_available=False,
                    ),
                    node_id=node_id,
                )
        bindings[node_id] = AgentBinding(node_id, profile, model, agent.instructions_suffix)
    return AdmittedManifest(graph, MappingProxyType(bindings))


def _selectable_model_labels(model_registry: ModelProfileRegistry | None) -> list[str]:
    """The labels a caller may pick from, or nothing when no registry was supplied."""
    return selectable_model_labels(model_registry.list_profiles()) if model_registry is not None else []
