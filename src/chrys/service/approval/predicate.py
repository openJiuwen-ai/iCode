# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Strict predicate assets, structured responses, and Formal routing."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum
from importlib.resources import files
from typing import Any

# Version the approval routing contract independently of the risk definitions.
_DECISION_VERSION = 2
_MAX_PRINCIPLES = 32
_MAX_TEXT_LENGTH = 4_000


class PredicateAssetError(ValueError):
    """The packaged predicate asset cannot safely be used."""


class PredicateDecision(StrEnum):
    ALLOW = "allow"
    NEEDS_REVIEW = "needs_review"


@dataclass(frozen=True, slots=True)
class Principle:
    """One independent semantic condition, including its complete definition."""

    id: str
    instructions: str
    criterion_true: str
    criterion_false: str
    consent_bar: str = ""


@dataclass(frozen=True, slots=True)
class PredicateAsset:
    """Validated immutable predicate definition used for one evaluation."""

    asset_version: str
    decision_version: int
    digest: str
    principles: tuple[Principle, ...]

    def __post_init__(self) -> None:
        validate_asset_snapshot(self)


@dataclass(frozen=True, slots=True)
class PredicateValue:
    """A validated predicate match and optional consent result."""

    id: str
    value: bool
    consent_bar_met: bool | None = None


@dataclass(frozen=True, slots=True)
class PredicateEvaluation:
    """Result of strict parsing followed by the fixed routing decision."""

    decision: PredicateDecision
    reason: str
    values: tuple[PredicateValue, ...] = ()
    blockers: tuple[str, ...] = ()


def load_default_asset() -> PredicateAsset:
    """Load the packaged, read-only Formal asset."""
    raw = files("chrys.service.approval").joinpath("principles.json").read_bytes()
    return parse_asset(raw)


def parse_asset(raw: bytes | str) -> PredicateAsset:
    """Validate the complete array; no legacy schema or role classification."""
    try:
        value = json.loads(raw, object_pairs_hook=reject_duplicate_object)
    except (TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PredicateAssetError("invalid predicate asset JSON") from exc
    if not isinstance(value, list) or not 1 <= len(value) <= _MAX_PRINCIPLES:
        raise PredicateAssetError("predicate asset needs principles")
    if any(not isinstance(item, dict) for item in value):
        raise PredicateAssetError("invalid principle fields")
    principles = tuple(_parse_structured_principle(item) for item in value)
    digest_source = raw.encode("utf-8") if isinstance(raw, str) else raw
    return PredicateAsset(
        asset_version="risk-predicates-v2",
        decision_version=_DECISION_VERSION,
        digest=asset_digest(digest_source),
        principles=principles,
    )


def _parse_structured_principle(item: dict[str, Any]) -> Principle:
    required = {"id", "instructions", "criterion"}
    optional = {"consent_bar"}
    criterion = item.get("criterion")
    if (
        set(item) not in (required, required | optional)
        or not isinstance(criterion, dict)
        or set(criterion) != {"true", "false"}
        or not all(_valid_text(value) for key, value in item.items() if key != "criterion")
        or not all(_valid_text(value) for value in criterion.values())
        or (item["id"] == "user_boundary" and optional.intersection(item))
    ):
        raise PredicateAssetError("invalid structured principle")
    return Principle(
        id=item["id"],
        instructions=item["instructions"],
        criterion_true=criterion["true"],
        criterion_false=criterion["false"],
        consent_bar=item.get("consent_bar", ""),
    )


def structured_definitions(asset: PredicateAsset) -> list[dict[str, Any]]:
    """Render the configured criteria and any applicable release conditions."""
    definitions = []
    for principle in asset.principles:
        definition: dict[str, Any] = {
            "id": principle.id,
            "instructions": principle.instructions,
            "criterion": {"true": principle.criterion_true, "false": principle.criterion_false},
        }
        if principle.consent_bar:
            definition["consent_bar"] = principle.consent_bar
        definitions.append(definition)
    return definitions


def structured_schema(asset: PredicateAsset) -> str:
    """Describe the exact response contract for the configured predicates."""

    def object_schema(properties: dict[str, Any]) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": properties,
            "required": list(properties),
            "additionalProperties": False,
        }

    predicates = {}
    for principle in asset.principles:
        fields = {"matches": {"type": "boolean"}}
        if principle.consent_bar:
            fields["consent_bar_met"] = {"type": "boolean"}
        predicates[principle.id] = object_schema(fields)
    return json.dumps(object_schema({"predicates": object_schema(predicates)}))


def evaluate_structured_response(text: str, asset: PredicateAsset) -> PredicateEvaluation:
    """Compose the final decision from validated evidence, in policy order."""
    data = _parse_response_json(text)
    if not isinstance(data, dict) or set(data) != {"predicates"}:
        raise PredicateAssetError("invalid structured response")
    facts = data["predicates"]
    if not isinstance(facts, dict) or set(facts) != {p.id for p in asset.principles}:
        raise PredicateAssetError("invalid predicate IDs")
    for principle in asset.principles:
        entry = facts[principle.id]
        keys = {"matches"}
        if principle.consent_bar:
            keys.add("consent_bar_met")
        if (
            not isinstance(entry, dict)
            or set(entry) != keys
            or any(type(value) is not bool for value in entry.values())
        ):
            raise PredicateAssetError("invalid predicate facts")
        if not entry["matches"] and any(entry.values()):
            raise PredicateAssetError("release conditions require a match")
    values = tuple(
        PredicateValue(p.id, facts[p.id]["matches"], facts[p.id].get("consent_bar_met")) for p in asset.principles
    )
    hard = [p.id for p in asset.principles if p.id != "user_boundary" and not p.consent_bar and facts[p.id]["matches"]]
    soft = [
        p.id
        for p in asset.principles
        if p.consent_bar and facts[p.id]["matches"] and not facts[p.id]["consent_bar_met"]
    ]
    for blockers in (
        hard,
        ["user_boundary"] if facts.get("user_boundary", {}).get("matches") else [],
        soft,
    ):
        if blockers:
            return PredicateEvaluation(
                PredicateDecision.NEEDS_REVIEW,
                f"Requires human review: {', '.join(blockers)}.",
                values,
                blockers=tuple(blockers),
            )
    return PredicateEvaluation(PredicateDecision.ALLOW, "No unresolved policy match; automatically approved.", values)


def _parse_response_json(text: str) -> Any:
    lines = text.strip().splitlines()
    # Unwrap only one complete block; extra prose or nested blocks remain invalid.
    if len(lines) >= 3 and lines[0].strip().lower() in {"```", "```json"} and lines[-1].strip() == "```":
        text = "\n".join(lines[1:-1])
    try:
        data = json.loads(text, object_pairs_hook=reject_duplicate_object)
    except (json.JSONDecodeError, PredicateAssetError) as exc:
        raise PredicateAssetError("invalid predicate response JSON") from exc
    return data


def asset_digest(raw: bytes) -> str:
    """Compute the principle asset's stable audit digest."""
    return hashlib.sha256(raw).hexdigest()


def validate_asset_snapshot(asset: PredicateAsset) -> None:
    """Validate once when constructing the immutable asset."""
    if (
        type(asset.decision_version) is not int
        or asset.decision_version != _DECISION_VERSION
        or not _valid_text(asset.asset_version)
        or not isinstance(asset.digest, str)
        or len(asset.digest) != 64
        or any(character not in "0123456789abcdef" for character in asset.digest)
        or not isinstance(asset.principles, tuple)
        or not 1 <= len(asset.principles) <= _MAX_PRINCIPLES
        or any(
            not isinstance(principle, Principle) or not _valid_principle(principle) for principle in asset.principles
        )
        or len({principle.id for principle in asset.principles}) != len(asset.principles)
    ):
        raise PredicateAssetError("invalid Formal asset snapshot")


def _valid_principle(principle: Principle) -> bool:
    return (
        all(
            _valid_text(text)
            for text in (principle.id, principle.instructions, principle.criterion_true, principle.criterion_false)
        )
        and (principle.consent_bar == "" or _valid_text(principle.consent_bar))
        and not (principle.id == "user_boundary" and principle.consent_bar)
    )


def reject_duplicate_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise PredicateAssetError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _valid_text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip()) and len(value) <= _MAX_TEXT_LENGTH
