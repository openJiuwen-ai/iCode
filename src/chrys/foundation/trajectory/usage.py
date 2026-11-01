# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Usage buckets for ``model.exchange.finished``.

``provider_reported`` keeps the usage mapping as the wire adapter handed it
over — provider-specific extras and their nested breakdowns included, under a
soft budget on the shapes adapters produce. ``normalized`` is the explicit
bucket set analytics sums over; every bucket is copied, never guessed, and a
missing bucket stays missing (missing ≠ 0).

``USAGE_ADAPTER_VERSION`` 1 defines:

* ``input_total`` — the provider's input/prompt count as reported. Whether
  it includes cache reads is provider-defined, so it is *not* exclusive of
  ``cache_read`` / ``cache_creation``; consumers that need an uncached input
  figure derive it per provider.
* ``cache_read`` / ``cache_creation`` — provider-managed prompt-cache counts.
* ``output_total`` — every generated token, reasoning included.
* ``reasoning`` — the reasoning subset of ``output_total``.
* ``output_visible`` — ``output_total - reasoning``, present only when both
  operands are; this is the one exclusive bucket the adapter derives.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

USAGE_ADAPTER_VERSION: Final = 1
PROVIDER_REPORTED_MAX_BYTES: Final = 1024
"""Soft budget for the mirror: past this the nested breakdowns give way, the scalars stay."""
_MAX_NESTING_DEPTH: Final = 3
_UNSUPPORTED: Final = object()

_BUCKETS: Final[tuple[tuple[str, str], ...]] = (
    ("input_total", "input_token_count"),
    ("cache_read", "cache_read_input_token_count"),
    ("cache_creation", "cache_creation_input_token_count"),
    ("output_total", "output_token_count"),
    ("reasoning", "reasoning_output_token_count"),
)


@dataclass(frozen=True, slots=True)
class ProviderReported:
    """The provider's usage mapping as recorded, plus the keys that did not fit."""

    values: dict[str, Any]
    omitted: tuple[str, ...] = ()


def provider_reported_usage(usage: Mapping[str, Any] | None) -> ProviderReported | None:
    """Mirror the adapter's usage mapping, JSON-safe and under a soft budget.

    Providers report their breakdowns (cache tiers, prompt-token details) as
    nested objects, and those are the numbers a reader cannot reconstruct from
    the normalized buckets — so they are copied, not flattened away. Size is a
    soft budget over what adapters produce, and the breakdowns are what give
    way first: a mapping whose mirror does not fit keeps its scalar members and
    names the rest in ``omitted`` rather than dropping them silently. Scalars
    themselves are always kept, so this returns a bound on adapter-shaped usage
    rather than a hard cap on any mapping; a line that still overruns is
    refused by the writer and accounted for by a gap.
    """
    if usage is None:
        return None
    values: dict[str, Any] = {}
    omitted: list[str] = []
    for key, value in usage.items():
        if not isinstance(key, str):
            continue
        copied = _json_safe(value, depth=0)
        if copied is _UNSUPPORTED:
            omitted.append(key)
            continue
        values[key] = copied
    if _encoded_size(values) > PROVIDER_REPORTED_MAX_BYTES:
        # Scalars are what every consumer reads first; the breakdowns are what
        # grow without bound, so those are the ones that give way.
        kept = {key: value for key, value in values.items() if not isinstance(value, dict | list)}
        omitted.extend(key for key in values if key not in kept)
        values = kept
    return ProviderReported(values=values, omitted=tuple(omitted))


def _json_safe(value: Any, *, depth: int) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        # A NaN or infinity has no JSON to be encoded as, and this mirror is
        # built on the caller's thread: leaving one in would raise there, out
        # of the writer's reach, and fail the very response it is describing.
        return _UNSUPPORTED
    if isinstance(value, bool | int | float | str):
        return value
    if depth >= _MAX_NESTING_DEPTH:
        return _UNSUPPORTED
    if isinstance(value, Mapping):
        copied: dict[str, Any] = {}
        for key, member in value.items():
            if not isinstance(key, str):
                return _UNSUPPORTED
            member_copy = _json_safe(member, depth=depth + 1)
            if member_copy is _UNSUPPORTED:
                return _UNSUPPORTED
            copied[key] = member_copy
        return copied
    if isinstance(value, list | tuple):
        members = [_json_safe(member, depth=depth + 1) for member in value]
        if any(member is _UNSUPPORTED for member in members):
            return _UNSUPPORTED
        return members
    return _UNSUPPORTED


def _encoded_size(values: Mapping[str, Any]) -> int:
    from chrys.foundation.trajectory.envelope import encode_json_value

    return len(encode_json_value(values))


def normalized_usage(usage: Mapping[str, Any] | None) -> dict[str, Any]:
    """Build the ``usage.normalized`` object for one exchange."""
    normalized: dict[str, Any] = {"adapter_version": USAGE_ADAPTER_VERSION}
    if usage is None:
        normalized["normalization_unavailable"] = True
        return normalized
    for bucket, key in _BUCKETS:
        value = usage.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            normalized[bucket] = value
    output_total = normalized.get("output_total")
    reasoning = normalized.get("reasoning")
    if isinstance(output_total, int) and isinstance(reasoning, int) and reasoning <= output_total:
        normalized["output_visible"] = output_total - reasoning
    if "input_total" not in normalized and "output_total" not in normalized:
        normalized["normalization_unavailable"] = True
    return normalized


def exchange_usage_payload(usage: Mapping[str, Any] | None) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """The ``usage`` member of ``model.exchange.finished`` and its field-level measurements."""
    reported = provider_reported_usage(usage)
    normalized = normalized_usage(usage)
    payload: dict[str, Any] = {"normalized": normalized}
    if reported is not None:
        payload["provider_reported"] = reported.values
        if reported.omitted:
            # Naming what did not fit keeps a reader from reading the mirror
            # as everything the provider said.
            payload["provider_reported_omitted"] = list(reported.omitted)
    return payload, usage_measurements(normalized)


def usage_measurements(
    normalized: Mapping[str, Any], *, pointer_prefix: str = "/payload/usage/normalized"
) -> dict[str, dict[str, Any]]:
    """Field-level provenance: every normalized bucket is provider-sourced."""
    from chrys.foundation.trajectory.envelope import MeasurementSource, measurement

    return {
        f"{pointer_prefix}/{bucket}": measurement(MeasurementSource.PROVIDER, adapter_version=USAGE_ADAPTER_VERSION)
        for bucket in normalized
        if bucket not in {"adapter_version", "normalization_unavailable"}
    }
