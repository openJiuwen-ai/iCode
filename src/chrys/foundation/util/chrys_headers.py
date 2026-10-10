# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Shared Chrys-managed HTTP header names."""

MODEL_ID_HEADER = "Chrys-Model-Id"
PARENT_SESSION_ID_HEADER = "Chrys-Parent-Session-Id"
REQUEST_ATTEMPT_ID_HEADER = "Chrys-Request-Attempt-Id"
SESSION_ID_HEADER = "Chrys-Session-Id"
X_PARENT_SESSION_ID_HEADER = "X-Parent-Session-ID"
X_SESSION_ID_HEADER = "X-Session-ID"

_RESERVED_CHRYS_HEADER_PREFIXES = ("chrys-", "chrys_")
_CHRYS_MANAGED_HEADER_NAMES = frozenset({X_PARENT_SESSION_ID_HEADER.casefold(), X_SESSION_ID_HEADER.casefold()})


def _is_reserved_chrys_header_name(name: str) -> bool:
    """Return whether an HTTP header name is reserved for Chrys-managed metadata."""
    return name.strip().casefold().startswith(_RESERVED_CHRYS_HEADER_PREFIXES)


def is_chrys_managed_header_name(name: str) -> bool:
    """Return whether an HTTP header name is managed by Chrys."""
    normalized = name.strip().casefold()
    return _is_reserved_chrys_header_name(name) or normalized in _CHRYS_MANAGED_HEADER_NAMES
