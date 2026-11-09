# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Endpoint resolution tests: tier selection, app injection and origin override."""

from __future__ import annotations

import pytest

from chrys.kernel.aixcoding.auth import Environment
from chrys.kernel.aixcoding.auth.environments import (
    ENVIRONMENT_VARIABLE,
    configure_environment,
    resolve_endpoints,
    resolve_environment,
)


@pytest.fixture(autouse=True)
def reset_configured_environment():
    configure_environment(None)
    yield
    configure_environment(None)


def test_default_environment_is_prod() -> None:
    assert resolve_environment({}) is Environment.PROD


@pytest.mark.parametrize(
    "raw,expected", [("local", Environment.LOCAL), ("DEV", Environment.DEV), ("prod", Environment.PROD)]
)
def test_environment_variable_selects_tier(raw: str, expected: Environment) -> None:
    assert resolve_environment({ENVIRONMENT_VARIABLE: raw}) is expected


@pytest.mark.parametrize("raw", ["", "staging", "prod ", "0", "null"])
def test_unknown_environment_falls_back_to_prod(raw: str) -> None:
    assert resolve_environment({ENVIRONMENT_VARIABLE: raw}) is Environment.PROD


@pytest.mark.parametrize("old_name", ["AIXCODING_AUTH_ENVIRONMENT", "CHRYS_AUTH_ENVIRONMENT"])
def test_old_environment_names_are_ignored(old_name: str) -> None:
    assert resolve_environment({old_name: "local"}) is Environment.PROD


def test_configure_environment_injects_production_default() -> None:
    configure_environment("local")
    assert resolve_environment() is Environment.LOCAL
    assert resolve_environment({ENVIRONMENT_VARIABLE: "dev"}) is Environment.DEV


def test_resolve_endpoints_preserves_injected_environment() -> None:
    configure_environment("local")
    auth_url, data_url = resolve_endpoints()
    assert auth_url == "http://localhost:7777/api/v1"
    assert data_url == "http://localhost:7777/api/v1"


def test_local_tier_points_at_mock_port() -> None:
    auth_url, data_url = resolve_endpoints(Environment.LOCAL, {})
    assert auth_url == "http://localhost:7777/api/v1"
    assert data_url == "http://localhost:7777/api/v1"


def test_prod_tier_uses_csas_and_aicoding_prefixes() -> None:
    auth_url, data_url = resolve_endpoints(Environment.PROD, {})
    assert auth_url == "http://82.187.34.98/csas/api/v1"
    assert data_url == "http://82.187.34.98/aicoding/api/v1"


def test_dev_tier_uses_dev_host() -> None:
    auth_url, data_url = resolve_endpoints(Environment.DEV, {})
    assert auth_url.startswith("http://81.89.182.150/csas/")
    assert data_url.startswith("http://81.89.182.150/aicoding/")


def test_server_url_overrides_origin_but_keeps_paths() -> None:
    auth_url, data_url = resolve_endpoints(Environment.PROD, {"AIXCODING_AUTH_SERVER_URL": "http://10.0.0.5:9000"})
    assert auth_url == "http://10.0.0.5:9000/csas/api/v1"
    assert data_url == "http://10.0.0.5:9000/aicoding/api/v1"


def test_server_url_without_scheme_is_accepted() -> None:
    auth_url, _ = resolve_endpoints(Environment.PROD, {"AIXCODING_AUTH_SERVER_URL": "10.0.0.5"})
    assert auth_url == "http://10.0.0.5/csas/api/v1"


@pytest.mark.parametrize("bad", ["", "   ", "not a url", "http://", "://missing-host"])
def test_invalid_server_url_is_ignored(bad: str) -> None:
    auth_url, data_url = resolve_endpoints(Environment.PROD, {"AIXCODING_AUTH_SERVER_URL": bad})
    assert auth_url == "http://82.187.34.98/csas/api/v1"
    assert data_url == "http://82.187.34.98/aicoding/api/v1"


def test_endpoints_follow_environment_variable_when_tier_not_passed() -> None:
    auth_url, _ = resolve_endpoints(environ={ENVIRONMENT_VARIABLE: "local"})
    assert auth_url == "http://localhost:7777/api/v1"
