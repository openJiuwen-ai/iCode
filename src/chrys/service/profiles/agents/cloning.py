# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Independent profile identities and stable sub-agent interfaces for copies."""

from __future__ import annotations

import re
import unicodedata
import uuid
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from chrys.service.profiles.agents.schema import AgentProfile


def name_key(name: str) -> str:
    return unicodedata.normalize("NFD", unicodedata.normalize("NFD", name).casefold())


@dataclass(frozen=True)
class CloneIdentity:
    name: str
    display_name: str
    id: str


def allocate_identity(
    profile: AgentProfile, occupied: set[str], ids: set[str], *, backup: bool = False
) -> CloneIdentity:
    """Reserve a name, display label and id; occupied names are normalized keys."""
    root = profile.name
    display_root = profile.display_name or root
    counter = 1
    if not backup and (match := re.fullmatch(r"(.+?)-copy(?:-(\d+))?", root, flags=re.IGNORECASE)):
        root = match[1]
        display_root = re.sub(r" copy(?: \d+)?$", "", display_root, flags=re.IGNORECASE)
        indices = [int(match[2] or 1)]
        indices.extend(
            int(other[1] or 1)
            for key in occupied
            if (other := re.fullmatch(re.escape(name_key(root)) + r"-copy(?:-(\d+))?", key))
        )
        counter = max(indices) + 1
    while True:
        suffix = "" if counter == 1 else f"-{counter}"
        display_suffix = "" if counter == 1 else f" {counter}"
        name = f"{root}-{'Modified' if backup else 'copy'}{suffix}"
        display = f"{display_root} (Modified{display_suffix})" if backup else f"{display_root} Copy{display_suffix}"
        if name_key(name) not in occupied and name_key(display) not in occupied:
            break
        counter += 1
    occupied.update((name_key(name), name_key(display)))
    profile_id = uuid.uuid4().hex[:12]
    while profile_id in ids:
        profile_id = uuid.uuid4().hex[:12]
    ids.add(profile_id)
    return CloneIdentity(name, display, profile_id)


def copy_profile(
    profile: AgentProfile,
    identity: CloneIdentity,
    replacements: Mapping[str, tuple[CloneIdentity, AgentProfile]] | None = None,
) -> AgentProfile:
    """Copy content, retarget copied children and preserve their exposed tool interface."""
    duplicate = deepcopy(profile)
    duplicate.name, duplicate.display_name, duplicate.id = identity.name, identity.display_name, identity.id
    targets = dict(replacements or {})
    targets.setdefault(profile.name, (identity, profile))
    for ref in duplicate.sub_agents.agents:
        if target := targets.get(ref.profile):
            new_identity, original = target
            ref.tool_name = ref.tool_name or ref.profile
            ref.tool_description = (
                ref.tool_description
                or original.description
                or f"Delegate task to {original.display_name or original.name}"
            )
            ref.profile = new_identity.name
    return duplicate
