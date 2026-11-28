# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Language-neutral facts behind a "selector resolved to nothing" error.

Lives in ``service`` so the workflow admission path can assemble the same
diagnosis as the CLI and ACP without importing orchestration or app (which the
layering forbids).  Each caller keeps its own error code and message prefix;
this module only supplies the shared facts and their plain-text rendering.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from chrys.service.profiles.file_scan import ProfileDirScan, unreadable_notes
from chrys.service.profiles.models.schema import ModelProfile, is_model_profile_selectable


@dataclass(frozen=True, slots=True)
class ProfileMissFacts:
    """What is true when no profile matched a selector, independent of locale."""

    selector: str
    available: tuple[str, ...]
    unreadable: tuple[str, ...]


def profile_miss_facts(
    selector: str,
    available: Iterable[str],
    scan: ProfileDirScan[Any] | None = None,
) -> ProfileMissFacts:
    """Collect the two facts a "not found" report must keep side by side.

    ``available`` answers "which name did you mean"; ``unreadable`` answers
    "why was my name not there".  A malformed file and a mistyped selector read
    the same to a resolver, so one must never replace the other.
    """
    notes = unreadable_notes(scan) if scan is not None else None
    return ProfileMissFacts(
        selector=selector,
        available=tuple(available),
        unreadable=tuple(str(value) for value in notes.values) if notes is not None else (),
    )


def profile_miss_sentence(
    facts: ProfileMissFacts,
    *,
    base: str,
    available_label: str = "Available profiles",
    unreadable_label: str = "Could not read these profile files",
    include_available: bool = True,
) -> str:
    """Render *facts* into one plain sentence after the caller's *base* prefix.

    A caller whose own hint line already lists the available names should pass
    ``include_available=False`` so the same list is not said twice; the
    unreadable notes stay in the sentence either way, because nothing else
    reports them.
    """
    parts = [base.rstrip(".")]
    if facts.available and include_available:
        parts.append(f"{available_label}: {', '.join(facts.available)}")
    if facts.unreadable:
        parts.append(f"{unreadable_label}: {', '.join(facts.unreadable)}")
    return ". ".join(parts) + "."


def selectable_model_labels(profiles: Iterable[ModelProfile]) -> list[str]:
    """Labels for model profiles a node can actually select: ``Name (ID)``, sorted.

    A profile without a ``model_id`` is rejected at admission, so listing it only
    sends the reader after a profile that cannot work; two profiles sharing a name
    are told apart by the id, the same form ``workflow validate`` prints.
    """
    return sorted(f"{profile.name} ({profile.id})" for profile in profiles if is_model_profile_selectable(profile))
