# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Target scoring and disambiguation (design section 6).

A natural-language spec is scored against every fused node; the top candidate is
only executed when it is clearly the winner - a single high-confidence match or
a wide enough gap to the runner-up. Anything else returns the top three
candidates for the model to disambiguate with a reference.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass
from html import unescape

from ..config import AMBIGUITY_GAP, MIN_TOP_SCORE
from ..errors import TargetAmbiguous, TargetNotFound
from .fusion import FusedNode
from .serializer import escape_label, sanitize_role

_EXACT_NAME_POINTS = 40
_EXACT_HINT_FALLBACK_POINTS = 30
_ROLE_OR_TAG_POINTS = 20
_CONTAINS_POINTS = 15
_PREFIX_BONUS_POINTS = 8
_REVERSE_CONTAINS_POINTS = 6
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
_REAL_CONTROL_TAGS = frozenset({"a", "button", "input", "select", "textarea", "summary", "option"})
_REAL_CONTROL_ROLES = frozenset(
    {
        "link",
        "button",
        "textbox",
        "searchbox",
        "combobox",
        "listbox",
        "option",
        "checkbox",
        "radio",
        "switch",
        "slider",
        "spinbutton",
        "tab",
        "menuitem",
        "menuitemcheckbox",
        "menuitemradio",
        "treeitem",
        "colorwell",
    }
)
_SNAPSHOT_SPEC_RE = re.compile(r'^\s*([a-zA-Z][a-zA-Z0-9_-]{0,31})\s+"((?:\\.|[^"\\])+)"\s*$')
_SEMANTIC_CONTAINER_RANK: dict[str, int] = {
    "dialog": 3,
    "alertdialog": 3,
    "main": 3,
    "article": 2,
    "heading": 2,
    "cell": 2,
    "rowheader": 2,
    "columnheader": 2,
    "section": 2,
    "region": 2,
    "listitem": 2,
    "figure": 2,
    "table": 2,
    "paragraph": 1,
}
_LANDMARK_ROLES = frozenset(
    {
        "main",
        "navigation",
        "nav",
        "banner",
        "header",
        "contentinfo",
        "footer",
        "complementary",
        "aside",
        "dialog",
        "alertdialog",
        "search",
        "form",
        "article",
        "region",
        "table",
    }
)
_LANDMARK_TAGS = frozenset(
    {"main", "nav", "header", "footer", "aside", "dialog", "form", "article", "section", "table"}
)
_HEAD_META_TAGS = frozenset({"head", "title", "meta", "link", "style", "script", "noscript", "base", "template"})
_STRUCTURAL_TAG_SPECS = _LANDMARK_TAGS | frozenset({"h1", "h2", "h3", "h4", "h5", "h6", "iframe"})
_KNOWN_ROLE_OR_TAG_HINTS = (
    _REAL_CONTROL_TAGS
    | _REAL_CONTROL_ROLES
    | _LANDMARK_ROLES
    | _LANDMARK_TAGS
    | _LABEL_ROLES
    | _STRUCTURAL_TAG_SPECS
    | frozenset(
        {
            "generic",
            "heading",
            "img",
            "image",
            "cell",
            "row",
            "rowheader",
            "columnheader",
            "gridcell",
            "list",
            "listitem",
            "paragraph",
            "section",
            "figure",
            "group",
            "status",
            "alert",
            "progressbar",
            "tablist",
            "tabpanel",
            "menu",
            "menubar",
            "tree",
            "grid",
            "toolbar",
            "tooltip",
            "separator",
            "div",
            "span",
            "p",
            "ul",
            "ol",
            "li",
            "td",
            "th",
            "tr",
            "tbody",
            "thead",
            "tfoot",
            "svg",
            "canvas",
            "video",
            "audio",
            "details",
            "fieldset",
            "legend",
            "pre",
            "code",
            "blockquote",
            "area",
        }
    )
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
    """Unescape HTML entities, lower-case, and collapse whitespace for comparison."""
    return " ".join(unescape(text or "").split()).lower()


def _parse_spec(spec: str, *, allow_role_hint: bool = True) -> tuple[str, str]:
    """Parse ``role "label"`` specs copied from snapshots, returning ``(role_hint, target)``."""
    raw = (spec or "").strip()
    if allow_role_hint:
        match = _SNAPSHOT_SPEC_RE.match(raw)
        if match is not None:
            hint = _norm(match.group(1))
            if hint in _KNOWN_ROLE_OR_TAG_HINTS:
                label = match.group(2).replace('\\"', '"').replace("\\\\", "\\")
                return hint, _norm(label)
    return "", _norm(raw)


def _role_tag_matches(node: FusedNode, wanted: str) -> bool:
    if not wanted:
        return False
    role = _norm(node.role)
    tag = _norm(node.tag)
    if wanted in {role, tag}:
        return True
    if wanted in {"img", "image"} and (role in {"img", "image"} or tag == "img"):
        return True
    if wanted == "textbox" and getattr(node, "editable", False):
        return True
    return False


def _is_meaningful_reverse_match(short: str, long_target: str) -> bool:
    """Whether *short* (a node label) is a substantial part of *long_target* (the query).

    Blocks 1-2 character noise labels like ``"0"``, ``"p"``, ``"f"``, or ``"EN"`` from
    hijacking long multi-word queries simply because ``"en" in "independent"``.
    """
    if len(short) < 3 or short not in long_target:
        return False
    if len(short) * 2 >= len(long_target):
        return True
    if len(short) < 4:
        return False
    pattern = rf"(?<![a-z0-9]){re.escape(short)}(?![a-z0-9])"
    return re.search(pattern, long_target) is not None


def _ref_token(node: FusedNode) -> str:
    """The ``@eN#epoch`` spelling, or ``-`` when the node was never registered."""
    if node.ref is None:
        return "-"
    return f"@e{node.ref}#{node.epoch if node.epoch is not None else '-'}"


def build_node_index(nodes: list[FusedNode] | None) -> dict[tuple[str, str | None, int], FusedNode]:
    """Index snapshot nodes by ``(frame_id, cdp_session_id, backend_node_id)`` for region lookup."""
    if not nodes:
        return {}
    return {(node.frame_id, node.cdp_session_id, node.backend_node_id): node for node in nodes}


def describe_region(node: FusedNode, nodes_by_key: dict[tuple[str, str | None, int], FusedNode]) -> str:
    """Return a concise landmark/container breadcrumb for *node* (e.g. ``main → dialog "Price"``)."""
    segments: list[str] = []
    if node.cdp_session_id:
        segments.append("iframe")
    if nodes_by_key and node.ancestor_ids:
        for ancestor_id in node.ancestor_ids:
            anc = nodes_by_key.get((node.frame_id, node.cdp_session_id, ancestor_id))
            if anc is None:
                continue
            role = _norm(anc.role)
            tag = _norm(anc.tag)
            if role not in _LANDMARK_ROLES and tag not in _LANDMARK_TAGS:
                continue
            kind = role if role in _LANDMARK_ROLES else tag
            if kind == "navigation":
                kind = "nav"
            elif kind == "banner":
                kind = "header"
            elif kind == "contentinfo":
                kind = "footer"
            elif kind == "complementary":
                kind = "aside"
            label = escape_label(anc.name, limit=28) if anc.name else ""
            seg = (
                f'{kind} "{label}"'
                if label and kind in {"dialog", "alertdialog", "region", "form", "article"}
                else kind
            )
            if not segments or segments[-1] != seg:
                segments.append(seg)
    if len(segments) > 3:
        segments = segments[-3:]
    return " → ".join(segments)


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


def score(node: FusedNode, spec: str, *, seen_names: dict[str, int], allow_role_hint: bool = True) -> int:
    """Score *node* against *spec* (design section 6.1, additive terms)."""
    name = _norm(node.name)
    hint = _norm(node.text_hint)
    effective_name, hint_fallback = _effective_name(node)
    role_hint, target = _parse_spec(spec, allow_role_hint=allow_role_hint)
    total = 0

    if target and effective_name and node.name_from_href and name:
        total += _href_name_match(name, target)
    elif target and effective_name:
        if effective_name == target:
            total += _EXACT_HINT_FALLBACK_POINTS if hint_fallback else _EXACT_NAME_POINTS
        elif target in effective_name:
            total += _CONTAINS_POINTS
            if len(target) >= 12 and effective_name.startswith(target):
                total += _PREFIX_BONUS_POINTS
        elif _is_meaningful_reverse_match(effective_name, target):
            total += _REVERSE_CONTAINS_POINTS

    if role_hint and _role_tag_matches(node, role_hint):
        total += _ROLE_OR_TAG_POINTS
    elif not role_hint and target and _role_tag_matches(node, target):
        total += _ROLE_OR_TAG_POINTS

    if node.in_viewport and node.visible:
        total += _VIEWPORT_POINTS

    if node.interactive:
        total += _INTERACTIVE_POINTS

    if effective_name and seen_names.get(effective_name, 0) == 1:
        total += _UNIQUE_POINTS

    if (
        target
        and hint
        and not hint_fallback
        and hint != name
        and (target == hint or target in hint or _is_meaningful_reverse_match(hint, target))
    ):
        total += _HINT_POINTS

    if target and effective_name and not (node.name_from_href and name):
        ratio = difflib.SequenceMatcher(None, target, effective_name).ratio()
        total += max(0, min(_SIMILARITY_POINTS, round(ratio * _SIMILARITY_POINTS)))

    if not node.visible or node.disabled:
        total += _HIDDEN_POINTS
    if node.scrollable and not node.in_viewport:
        total += _COLLAPSED_POINTS

    return total


def _matches_spec(node: FusedNode, spec: str, *, allow_role_hint: bool = True) -> bool:
    """Whether *spec* produced a real signal for *node*.

    Visibility/scrollability bonuses alone must not manufacture a candidate:
    otherwise a spec that matches nothing would be reported as ambiguous
    instead of not-found.
    """
    name = _norm(node.name)
    role_hint, target = _parse_spec(spec, allow_role_hint=allow_role_hint)
    if not target:
        return False
    if role_hint and not _role_tag_matches(node, role_hint):
        return False
    if name and node.name_from_href:
        return _href_name_match(name, target) > 0 or (not role_hint and _role_tag_matches(node, target))
    if name and (name == target or target in name or _is_meaningful_reverse_match(name, target)):
        return True
    if not role_hint and _role_tag_matches(node, target):
        return True
    hint = _norm(node.text_hint)
    if hint and hint != name and (target == hint or target in hint or _is_meaningful_reverse_match(hint, target)):
        return True
    if not name:
        return False
    # Never fuzzy-match 1-2 character targets/names (e.g. "h1" vs "1") or bare structural tag
    # names ("main" vs "Menu", "aside" vs "Tasmin"): those must match explicitly.
    if len(target) < 3 or len(name) < 3 or target in _STRUCTURAL_TAG_SPECS:
        return False
    # real_quick_ratio() >= quick_ratio() >= ratio(): the cheap upper bounds reject
    # most nodes without the quadratic ratio(), and never change the verdict.
    matcher = difflib.SequenceMatcher(None, target, name)
    return (
        matcher.real_quick_ratio() >= _SIMILARITY_MIN_RATIO
        and matcher.quick_ratio() >= _SIMILARITY_MIN_RATIO
        and matcher.ratio() >= _SIMILARITY_MIN_RATIO
    )


def _drop_redundant_ancestors(
    candidates: list[Candidate], spec: str, *, allow_role_hint: bool = True
) -> list[Candidate]:
    """Suppress non-control ancestors that merely echo an interactive/control descendant's name.

    A ``<td><button>Resolve INC-0117</button></td>`` exposes both the ``button``
    and its ``cell`` ancestor (which may also inherit ``cursor: pointer``) with
    the same accessible name. Unless the caller explicitly targeted the
    ancestor's role/tag, keep the descendant control as the sole contender
    instead of failing with a spurious ambiguity error.
    """
    role_hint, target = _parse_spec(spec, allow_role_hint=allow_role_hint)
    interactive_by_ancestor: dict[tuple[str, str | None, int, str], int] = {}
    control_by_ancestor: dict[tuple[str, str | None, int, str], int] = {}
    for candidate in candidates:
        node = candidate.node
        if not node.ancestor_ids:
            continue
        is_control = _is_real_control(node)
        if not node.interactive and not is_control:
            continue
        name, _ = _effective_name(node)
        if not name:
            continue
        for ancestor_id in node.ancestor_ids:
            key = (node.frame_id, node.cdp_session_id, ancestor_id, name)
            if node.interactive:
                prev = interactive_by_ancestor.get(key)
                if prev is None or candidate.score > prev:
                    interactive_by_ancestor[key] = candidate.score
            if is_control:
                prev_ctrl = control_by_ancestor.get(key)
                if prev_ctrl is None or candidate.score > prev_ctrl:
                    control_by_ancestor[key] = candidate.score
    if not interactive_by_ancestor and not control_by_ancestor:
        return candidates
    filtered: list[Candidate] = []
    for candidate in candidates:
        node = candidate.node
        explicit_role = _role_tag_matches(node, role_hint) or (not role_hint and _role_tag_matches(node, target))
        if not explicit_role:
            anc_name, _ = _effective_name(node)
            key = (node.frame_id, node.cdp_session_id, node.backend_node_id, anc_name)
            if not node.interactive:
                child_score = interactive_by_ancestor.get(key)
                if child_score is not None and child_score >= candidate.score:
                    continue
            elif not _is_real_control(node):
                ctrl_score = control_by_ancestor.get(key)
                if ctrl_score is not None and ctrl_score >= candidate.score:
                    continue
        filtered.append(candidate)
    return filtered


def _is_real_control(node: FusedNode) -> bool:
    """True for genuine controls, excluding plain wrappers that only inherited ``cursor: pointer``."""
    return (
        _norm(node.tag) in _REAL_CONTROL_TAGS
        or _norm(node.role) in _REAL_CONTROL_ROLES
        or bool(getattr(node, "editable", False))
        or bool(getattr(node, "focusable", False))
    )


def _drop_redundant_descendants(
    candidates: list[Candidate], spec: str, *, allow_role_hint: bool = True
) -> list[Candidate]:
    """Collapse wrapper descendants that merely echo a same-name ancestor.

    Handles both:
    - ``<button><span>Filters</span></button>`` / ``<a><h3><span>Title</span></h3></a>`` /
      ``<a><img alt="Floorplan"></a>``: keep the interactive ancestor and drop echoing
      non-control descendants (even when Chrome's inherited ``cursor: pointer`` set
      ``interactive=True`` on the inner ``<h3>``/``<span>``).
    - ``<dialog aria-label="Price"><h2>Price</h2></dialog>`` / ``<h2><span>3.6k results</span></h2>`` /
      ``<td><p>Warranty</p></td>``: when two non-control candidates share an ancestor-descendant
      link and the exact same effective name, keep the stronger semantic container.
    """
    role_hint, target = _parse_spec(spec, allow_role_hint=allow_role_hint)
    interactive_ancestors: set[tuple[str, str | None, int, str]] = set()
    for candidate in candidates:
        node = candidate.node
        name, _ = _effective_name(node)
        if not name:
            continue
        if node.interactive or _is_real_control(node):
            interactive_ancestors.add((node.frame_id, node.cdp_session_id, node.backend_node_id, name))

    # First pass: drop non-control descendants inside an interactive/control ancestor of the same name.
    after_interactive: list[Candidate] = []
    for candidate in candidates:
        node = candidate.node
        explicit_role = _role_tag_matches(node, role_hint) or _role_tag_matches(node, target)
        if not _is_real_control(node) and not explicit_role and node.ancestor_ids:
            name, _ = _effective_name(node)
            if name and any(
                (node.frame_id, node.cdp_session_id, anc_id, name) in interactive_ancestors
                for anc_id in node.ancestor_ids
            ):
                continue
        after_interactive.append(candidate)

    # Second pass: collapse non-control ancestor/descendant pairs with identical names.
    def _semantic_priority(cand: Candidate) -> tuple[int, int]:
        n = cand.node
        rank_val = max(_SEMANTIC_CONTAINER_RANK.get(_norm(n.role), 0), _SEMANTIC_CONTAINER_RANK.get(_norm(n.tag), 0))
        return (rank_val, cand.score)

    dropped_ids: set[int] = set()
    remaining_non_controls: dict[tuple[str, str | None, int, str], Candidate] = {}
    for cand in after_interactive:
        if _is_real_control(cand.node):
            continue
        name, _ = _effective_name(cand.node)
        if name:
            remaining_non_controls[(cand.node.frame_id, cand.node.cdp_session_id, cand.node.backend_node_id, name)] = (
                cand
            )
    for cand in after_interactive:
        node = cand.node
        if _is_real_control(node) or not node.ancestor_ids or id(cand) in dropped_ids:
            continue
        name, _ = _effective_name(node)
        if not name:
            continue
        for anc_id in node.ancestor_ids:
            anc = remaining_non_controls.get((node.frame_id, node.cdp_session_id, anc_id, name))
            if anc is None or id(anc) in dropped_ids:
                continue
            cand_explicit = _role_tag_matches(node, role_hint) or _role_tag_matches(node, target)
            anc_explicit = _role_tag_matches(anc.node, role_hint) or _role_tag_matches(anc.node, target)
            if cand_explicit and not anc_explicit:
                dropped_ids.add(id(anc))
            elif anc_explicit and not cand_explicit:
                dropped_ids.add(id(cand))
                break
            elif _semantic_priority(anc) >= _semantic_priority(cand):
                dropped_ids.add(id(cand))
                break
            else:
                dropped_ids.add(id(anc))
    return [cand for cand in after_interactive if id(cand) not in dropped_ids]


def _drop_shadowed_labels(candidates: list[Candidate], spec: str, *, allow_role_hint: bool = True) -> list[Candidate]:
    """Suppress ``<label>`` caption nodes when a same-name form control is also a candidate.

    ``<label for="x">Priority Level</label><input id="x" type="range" aria-label="Priority Level">``
    exposes both a clickable ``<label>`` and the actual focusable form control with the same name.
    Unless the caller explicitly targeted ``label``/``labeltext``, resolve to the control itself.
    """
    role_hint, target = _parse_spec(spec, allow_role_hint=allow_role_hint)
    if target in _LABEL_ROLES or role_hint in _LABEL_ROLES:
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


def _rank_pass(
    eligible: list[FusedNode],
    seen_names: dict[str, int],
    spec: str,
    *,
    allow_role_hint: bool,
) -> list[Candidate]:
    role_hint, target = _parse_spec(spec, allow_role_hint=allow_role_hint)
    candidates: list[Candidate] = []
    for node in eligible:
        if not _matches_spec(node, spec, allow_role_hint=allow_role_hint):
            continue
        value = score(node, spec, seen_names=seen_names, allow_role_hint=allow_role_hint)
        if value > 0:
            name = _norm(node.name)
            effective_name, _ = _effective_name(node)
            exact = bool(
                target
                and effective_name
                and effective_name == target
                and not (node.name_from_href and name)
                and (not role_hint or _role_tag_matches(node, role_hint))
            )
            candidates.append(Candidate(node=node, score=value, exact_match=exact))
    if any(not candidate.node.css_hidden for candidate in candidates):
        candidates = [candidate for candidate in candidates if not candidate.node.css_hidden]
    candidates = _drop_redundant_ancestors(candidates, spec, allow_role_hint=allow_role_hint)
    candidates = _drop_redundant_descendants(candidates, spec, allow_role_hint=allow_role_hint)
    candidates = _drop_shadowed_labels(candidates, spec, allow_role_hint=allow_role_hint)
    candidates.sort(key=lambda candidate: candidate.score, reverse=True)
    return candidates


def rank(nodes: list[FusedNode], spec: str) -> list[Candidate]:
    """Score every spec-matching node, keep the positive ones and sort descending."""
    eligible = [node for node in nodes if _norm(node.tag) not in _HEAD_META_TAGS]
    seen_names: dict[str, int] = {}
    for node in eligible:
        if node.css_hidden:
            continue
        key, _ = _effective_name(node)
        if key:
            seen_names[key] = seen_names.get(key, 0) + 1

    role_hint, _ = _parse_spec(spec, allow_role_hint=True)
    candidates = _rank_pass(eligible, seen_names, spec, allow_role_hint=True)
    if not candidates and role_hint:
        candidates = _rank_pass(eligible, seen_names, spec, allow_role_hint=False)
    return candidates


def top3_to_text(candidates: list[Candidate], *, all_nodes: list[FusedNode] | None = None) -> str:
    """Render the top three candidates with visibility and region context, one per line."""
    nodes_by_key = build_node_index(all_nodes)
    lines: list[str] = []
    for candidate in candidates[:_MAX_CANDIDATES]:
        node = candidate.node
        label = escape_label(node.name or node.text_hint, limit=120)
        role = sanitize_role(node.role)
        vis = "in-view" if node.in_viewport else "off-screen"
        region = describe_region(node, nodes_by_key)
        where = f" in {region}" if region else ""
        lines.append(f'  {_ref_token(node)} {role} "{label}" [{vis}]{where} (score {candidate.score})')
    return "\n".join(lines)


def _summary(candidates: list[Candidate]) -> str:
    parts = [
        f'{_ref_token(c.node)} {sanitize_role(c.node.role)} "{escape_label(c.node.name or c.node.text_hint, limit=120)}"'
        for c in candidates[:_MAX_CANDIDATES]
    ]
    return "; ".join(parts)


def resolve_ambiguity(candidates: list[Candidate], *, all_nodes: list[FusedNode] | None = None) -> Candidate:
    """Pick the single best candidate or raise ``TargetNotFound``/``TargetAmbiguous``."""
    if not candidates:
        raise TargetNotFound("未找到匹配的元素，请重新 snapshot 后重试")

    ordered = sorted(candidates, key=lambda candidate: candidate.score, reverse=True)
    top = ordered[:_MAX_CANDIDATES]
    details = top3_to_text(top, all_nodes=all_nodes)
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
