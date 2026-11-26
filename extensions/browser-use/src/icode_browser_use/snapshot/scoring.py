# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Target scoring and disambiguation (design section 6).

A natural-language spec is scored against every fused node; the top candidate is
only executed when it is clearly the winner - a single high-confidence match or
a wide enough gap to the runner-up. Anything else returns the top three
candidates for the model to disambiguate with a reference.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass

from ..config import AMBIGUITY_GAP, MIN_TOP_SCORE
from ..errors import TargetAmbiguous, TargetNotFound
from .fusion import FusedNode
from .serializer import escape_label

_EXACT_NAME_POINTS = 40
_EXACT_HINT_FALLBACK_POINTS = 30
_ROLE_OR_TAG_POINTS = 20
_CONTAINS_POINTS = 15
_VIEWPORT_POINTS = 10
_UNIQUE_POINTS = 10
_INTERACTIVE_POINTS = 5
_HINT_POINTS = 5
_SIMILARITY_POINTS = 15
_HIDDEN_POINTS = -20
_COLLAPSED_POINTS = -15
_MAX_CANDIDATES = 3
_LABEL_ROLES = frozenset({"labeltext", "label"})
_FORM_CONTROL_TAGS = frozenset({"input", "textarea", "select"})
_FORM_CONTROL_ROLES = frozenset(
    {"textbox", "slider", "combobox", "checkbox", "radio", "switch", "spinbutton", "colorwell", "listbox"}
)

#: Minimum fuzzy-similarity ratio for a name to count as a spec match at all.
_SIMILARITY_MIN_RATIO = 0.5


@dataclass
class Candidate:
    """A scored target node."""

    node: FusedNode
    score: int
    exact_match: bool = False


def _norm(text: str) -> str:
    """Lower-case and collapse whitespace for comparison."""
    return " ".join((text or "").split()).lower()


def _ref_token(node: FusedNode) -> str:
    """The ``@eN#epoch`` spelling, or ``-`` when the node was never registered."""
    if node.ref is None:
        return "-"
    return f"@e{node.ref}#{node.epoch if node.epoch is not None else '-'}"


def _href_name_match(name: str, target: str) -> int:
    """Points for a name that is a link's path: exact, or contained in a path-like spec.

    A path shares words with ordinary labels ("/chat/c1" vs "Chat"), so substring
    and fuzzy credit would pull every icon-only link into unrelated lookups.
    """
    if name == target:
        return _EXACT_NAME_POINTS
    if "/" in target and target in name:
        return _CONTAINS_POINTS
    return 0


def _use_hint_as_name(node: FusedNode, name: str, hint: str) -> bool:
    """Fall back to ``text_hint`` for unnamed elements except caption ``<label>`` nodes."""
    if name or not hint:
        return False
    return _norm(node.role) not in _LABEL_ROLES and _norm(node.tag) != "label"


def _effective_name(node: FusedNode) -> tuple[str, bool]:
    """Return ``(effective_name, used_hint_fallback)`` for *node*."""
    name = _norm(node.name)
    hint = _norm(node.text_hint)
    hint_fallback = _use_hint_as_name(node, name, hint)
    return (hint if hint_fallback else name), hint_fallback


def score(node: FusedNode, spec: str, *, seen_names: dict[str, int]) -> int:
    """Score *node* against *spec* (design section 6.1, additive terms)."""
    name = _norm(node.name)
    hint = _norm(node.text_hint)
    effective_name, hint_fallback = _effective_name(node)
    target = _norm(spec)
    total = 0

    if target and effective_name and node.name_from_href and name:
        total += _href_name_match(name, target)
    elif target and effective_name:
        if effective_name == target:
            total += _EXACT_HINT_FALLBACK_POINTS if hint_fallback else _EXACT_NAME_POINTS
        elif target in effective_name or effective_name in target:
            total += _CONTAINS_POINTS

    if target and target in {_norm(node.role), _norm(node.tag)}:
        total += _ROLE_OR_TAG_POINTS

    if node.in_viewport and node.visible:
        total += _VIEWPORT_POINTS

    if node.interactive:
        total += _INTERACTIVE_POINTS

    if effective_name and seen_names.get(effective_name, 0) == 1:
        total += _UNIQUE_POINTS

    if target and hint and not hint_fallback and hint != name and (target == hint or target in hint or hint in target):
        total += _HINT_POINTS

    if target and effective_name and not (node.name_from_href and name):
        ratio = difflib.SequenceMatcher(None, target, effective_name).ratio()
        total += max(0, min(_SIMILARITY_POINTS, round(ratio * _SIMILARITY_POINTS)))

    if not node.visible or node.disabled:
        total += _HIDDEN_POINTS
    if node.scrollable and not node.in_viewport:
        total += _COLLAPSED_POINTS

    return total


def _matches_spec(node: FusedNode, spec: str) -> bool:
    """Whether *spec* produced a real signal for *node*.

    Visibility/scrollability bonuses alone must not manufacture a candidate:
    otherwise a spec that matches nothing would be reported as ambiguous
    instead of not-found.
    """
    name = _norm(node.name)
    target = _norm(spec)
    if not target:
        return False
    if name and node.name_from_href:
        return _href_name_match(name, target) > 0 or target in {_norm(node.role), _norm(node.tag)}
    if name and (name == target or target in name or name in target):
        return True
    if target in {_norm(node.role), _norm(node.tag)}:
        return True
    hint = _norm(node.text_hint)
    if hint and hint != name and (target == hint or target in hint or hint in target):
        return True
    if not name:
        return False
    # real_quick_ratio() >= quick_ratio() >= ratio(): the cheap upper bounds reject
    # most nodes without the quadratic ratio(), and never change the verdict.
    matcher = difflib.SequenceMatcher(None, target, name)
    return (
        matcher.real_quick_ratio() >= _SIMILARITY_MIN_RATIO
        and matcher.quick_ratio() >= _SIMILARITY_MIN_RATIO
        and matcher.ratio() >= _SIMILARITY_MIN_RATIO
    )


def _drop_redundant_ancestors(candidates: list[Candidate], spec: str) -> list[Candidate]:
    """Suppress non-interactive ancestors that merely echo an interactive descendant's name.

    A ``<td><button>Resolve INC-0117</button></td>`` exposes both the ``button``
    and its non-interactive ``cell`` ancestor with the same accessible name.
    Unless the caller explicitly targeted the ancestor's role/tag, keep the
    interactive descendant as the sole contender instead of failing with a
    spurious ambiguity error.
    """
    target = _norm(spec)
    interactive_by_ancestor: dict[tuple[str, str | None, int, str], int] = {}
    for candidate in candidates:
        node = candidate.node
        if not node.interactive or not node.ancestor_ids:
            continue
        name, _ = _effective_name(node)
        if not name:
            continue
        for ancestor_id in node.ancestor_ids:
            key = (node.frame_id, node.cdp_session_id, ancestor_id, name)
            prev = interactive_by_ancestor.get(key)
            if prev is None or candidate.score > prev:
                interactive_by_ancestor[key] = candidate.score
    if not interactive_by_ancestor:
        return candidates
    filtered: list[Candidate] = []
    for candidate in candidates:
        node = candidate.node
        if not node.interactive and target not in {_norm(node.role), _norm(node.tag)}:
            anc_name, _ = _effective_name(node)
            key = (node.frame_id, node.cdp_session_id, node.backend_node_id, anc_name)
            child_score = interactive_by_ancestor.get(key)
            if child_score is not None and child_score >= candidate.score:
                continue
        filtered.append(candidate)
    return filtered


def _drop_shadowed_labels(candidates: list[Candidate], spec: str) -> list[Candidate]:
    """Suppress ``<label>`` caption nodes when a same-name form control is also a candidate.

    ``<label for="x">Priority Level</label><input id="x" type="range" aria-label="Priority Level">``
    exposes both a clickable ``<label>`` and the actual focusable form control with the same name.
    Unless the caller explicitly targeted ``label``/``labeltext``, resolve to the control itself.
    """
    target = _norm(spec)
    if target in _LABEL_ROLES:
        return candidates
    control_keys: set[tuple[str, str | None, str]] = set()
    for candidate in candidates:
        node = candidate.node
        role = _norm(node.role)
        tag = _norm(node.tag)
        if tag in _FORM_CONTROL_TAGS or role in _FORM_CONTROL_ROLES or node.editable:
            eff_name, _ = _effective_name(node)
            if eff_name:
                control_keys.add((node.frame_id, node.cdp_session_id, eff_name))
    if not control_keys:
        return candidates
    filtered: list[Candidate] = []
    for candidate in candidates:
        node = candidate.node
        if _norm(node.tag) == "label" or _norm(node.role) in _LABEL_ROLES:
            eff_name, _ = _effective_name(node)
            if (node.frame_id, node.cdp_session_id, eff_name) in control_keys:
                continue
        filtered.append(candidate)
    return filtered


def rank(nodes: list[FusedNode], spec: str) -> list[Candidate]:
    """Score every spec-matching node, keep the positive ones and sort descending."""
    seen_names: dict[str, int] = {}
    for node in nodes:
        key, _ = _effective_name(node)
        if key:
            seen_names[key] = seen_names.get(key, 0) + 1

    target = _norm(spec)
    candidates: list[Candidate] = []
    for node in nodes:
        if not _matches_spec(node, spec):
            continue
        value = score(node, spec, seen_names=seen_names)
        if value > 0:
            name = _norm(node.name)
            effective_name, _ = _effective_name(node)
            exact = bool(target and effective_name and effective_name == target and not (node.name_from_href and name))
            candidates.append(Candidate(node=node, score=value, exact_match=exact))
    candidates = _drop_redundant_ancestors(candidates, spec)
    candidates = _drop_shadowed_labels(candidates, spec)
    candidates.sort(key=lambda candidate: candidate.score, reverse=True)
    return candidates


def top3_to_text(candidates: list[Candidate]) -> str:
    """Render the top three candidates, one per line."""
    lines: list[str] = []
    for candidate in candidates[:_MAX_CANDIDATES]:
        node = candidate.node
        label = escape_label(node.name or node.text_hint, limit=120)
        lines.append(f'  {_ref_token(node)} {node.role} "{label}" (score {candidate.score})')
    return "\n".join(lines)


def _summary(candidates: list[Candidate]) -> str:
    parts = [
        f'{_ref_token(c.node)} {c.node.role} "{escape_label(c.node.name or c.node.text_hint, limit=120)}"'
        for c in candidates[:_MAX_CANDIDATES]
    ]
    return "; ".join(parts)


def resolve_ambiguity(candidates: list[Candidate]) -> Candidate:
    """Pick the single best candidate or raise ``TargetNotFound``/``TargetAmbiguous``."""
    if not candidates:
        raise TargetNotFound("未找到匹配的元素，请重新 snapshot 后重试")

    ordered = sorted(candidates, key=lambda candidate: candidate.score, reverse=True)
    top = ordered[:_MAX_CANDIDATES]
    details = top3_to_text(top)
    details += "\n用上面某个候选的完整 @eN#epoch ref 作为 target；landmark 名称（navigation/main）不适合当目标"
    if any(not candidate.node.in_viewport for candidate in top):
        details += "\n部分候选不在当前视口，可先 scroll 后重拍快照"
    if len(ordered) == 1:
        if ordered[0].score >= MIN_TOP_SCORE or ordered[0].exact_match:
            return ordered[0]
        message = f"目标匹配度过低（仅找到 1 个弱匹配候选）：{_summary(top)}"
        raise TargetAmbiguous(message, candidates=top, details=details)

    message = f"目标不唯一，候选：{_summary(top)}"
    if ordered[0].score < MIN_TOP_SCORE:
        raise TargetAmbiguous(message, candidates=top, details=details)
    if ordered[0].score - ordered[1].score >= AMBIGUITY_GAP:
        return ordered[0]
    raise TargetAmbiguous(message, candidates=top, details=details)
