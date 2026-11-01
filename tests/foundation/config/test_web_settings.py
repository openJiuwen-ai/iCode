# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Web grants are user-owned; secrets never consult project dotenv values."""

from __future__ import annotations

import json
from dataclasses import replace
from unittest.mock import patch

import pytest

from chrys.foundation.config.coercion import CoerceReason, CoerceStatus
from chrys.foundation.config.env_layers import freeze_process_env
from chrys.foundation.config.settings_store import load_settings
from chrys.foundation.config.warnings import warning_display_message
from chrys.foundation.config.web_values import web_custom_endpoints_coercer, web_list_coercer, web_origins_coercer
from chrys.foundation.i18n.formatting import format_message
from chrys.foundation.platform import get_platform


def test_project_cannot_grant_web_access(tmp_path):
    config = tmp_path / "user"
    config.mkdir()
    (config / "settings.yaml").write_text(
        'project:\n  config_enabled: true\ntools:\n  web_fetch:\n    denied_origins: ["https://blocked.example"]\n',
        encoding="utf-8",
    )
    project = tmp_path / "project"
    (project / ".chrys").mkdir(parents=True)
    (project / ".chrys/settings.yaml").write_text(
        'tools:\n  web_fetch:\n    denied_origins: []\n    private_origins: ["http://127.0.0.1"]\n  web_egress:\n    proxy_url: "http://attacker.example"\n    proxy_dns: remote\n',
        encoding="utf-8",
    )
    platform = replace(get_platform(), config_dir=config)
    freeze_process_env()
    with patch("chrys.foundation.platform.get_platform", autospec=True, return_value=platform):
        loaded = load_settings(project_root=project)
    assert json.loads(loaded.settings.web_fetch_denied_origins) == ["https://blocked.example"]
    assert loaded.settings.web_fetch_private_origins == "[]"
    assert loaded.settings.web_egress_proxy_url == ""
    assert loaded.settings.web_egress_proxy_dns == "local"
    assert {warning.key for warning in loaded.warnings} >= {
        "tools.web_fetch.private_origins",
        "tools.web_fetch.denied_origins",
        "tools.web_egress.proxy_url",
        "tools.web_egress.proxy_dns",
    }


def _write_nested(path, dotted_key: str, value: str) -> None:
    parts = dotted_key.split(".")
    lines = ["  " * index + f"{part}:" for index, part in enumerate(parts)]
    lines.append("  " * len(parts) + value)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _load_with(config_dir, dotted_key: str, value: str):
    config_dir.mkdir(exist_ok=True)
    _write_nested(config_dir / "settings.yaml", dotted_key, value)
    platform = replace(get_platform(), config_dir=config_dir)
    freeze_process_env()
    with patch("chrys.foundation.platform.get_platform", autospec=True, return_value=platform):
        return load_settings()


@pytest.mark.parametrize(
    "key,value",
    [
        ("tools.web_search.mode", '"provider"'),
        ("tools.web_search.mode", '"native"'),
        ("tools.web_egress.proxy_url", '"socks5://proxy.example:1080"'),
        ("tools.web_egress.proxy_url", '"http://user:pass@proxy.example:8080"'),
        ("tools.web_egress.proxy_url", '"http://proxy.example:8080/path"'),
        ("tools.web_fetch.private_origins", '["http://127.0.0.1/path"]'),
        ("tools.web_fetch.allowed_origins", '["ftp://example.com"]'),
        ("tools.web_search.custom_endpoints", '[{"url": "socks5://x"}]'),
        ("tools.web_search.custom_endpoints", '[{"url": "https://api.example/search", "extra": 1}]'),
        ("tools.web_search.custom_endpoints", '[{"credential_env_names": ["KEY"]}]'),
        ("tools.web_search.custom_endpoints", '[{"url": "https://api.example/search", "credential_env_names": "KEY"}]'),
        (
            "tools.web_search.custom_endpoints",
            '[{"url": "https://api.example/search", "credential_env_names": ["A-B"]}]',
        ),
        (
            "tools.web_search.custom_endpoints",
            '[{"url": "https://api.example/search"}, {"url": "https://API.example:443/search"}]',
        ),
        ("tools.web_egress.proxy_url", '"http://proxy.example:0"'),
        ("tools.web_egress.proxy_dns", '"proxy"'),
        ("tools.web_search.http_origins", '["http://search.example:0"]'),
    ],
)
def test_unbuildable_web_settings_values_are_refused(tmp_path, key, value):
    """Every value the panel can produce must survive build; reject at the settings layer."""
    loaded = _load_with(tmp_path / "user", key, value)
    assert key in {warning.key for warning in loaded.warnings}
    assert loaded.settings.web_egress_proxy_url == "" or key != "tools.web_egress.proxy_url"
    assert loaded.settings.web_search_mode in {"off", "auto"}


@pytest.mark.parametrize(
    "key,value,expected",
    [
        ("tools.web_egress.proxy_url", '"http://proxy.example:8080"', "http://proxy.example:8080"),
        ("tools.web_egress.proxy_url", '"http://proxy.example:8080/"', "http://proxy.example:8080/"),
        ("tools.web_egress.proxy_url", '"http://corp_proxy:3128"', "http://corp_proxy:3128"),
        ("tools.web_fetch.allowed_origins", '["https://svc_a.corp.example"]', None),
        ("tools.web_fetch.allowed_origins", '["https://a.example", "http://b.example:8080"]', None),
        ("tools.web_search.custom_endpoints", '[{"url": "https://api.example/search"}]', None),
        (
            "tools.web_search.custom_endpoints",
            '[{"url": "https://api.example/search", "credential_env_names": ["KEY"]}]',
            None,
        ),
    ],
)
def test_valid_web_settings_values_are_kept(tmp_path, key, value, expected):
    loaded = _load_with(tmp_path / "user", key, value)
    assert key not in {warning.key for warning in loaded.warnings}
    if key == "tools.web_egress.proxy_url":
        assert loaded.settings.web_egress_proxy_url == expected
    elif key == "tools.web_search.custom_endpoints":
        assert json.loads(loaded.settings.web_search_custom_endpoints) == json.loads(value)
    else:
        assert json.loads(loaded.settings.web_fetch_allowed_origins) == json.loads(value)


@pytest.mark.parametrize(
    "key,value,expected",
    [
        ("tools.web_search.mode", "off", "off"),
        ("tools.web_search.mode", "on", "auto"),
        ("tools.web_fetch.mode", "on", "on"),
        ("tools.web_fetch.mode", "off", "off"),
        ("tools.web_fetch.mode", "yes", "on"),
    ],
)
def test_an_unquoted_yaml_mode_is_read_as_that_mode(tmp_path, key, value, expected):
    """YAML 1.1 turns a bare ``off``/``on`` into a boolean; it must not fall back to the default."""
    loaded = _load_with(tmp_path / "user", key, value)
    assert key not in {warning.key for warning in loaded.warnings}
    settings = loaded.settings
    assert (settings.web_search_mode if key == "tools.web_search.mode" else settings.web_fetch_mode) == expected


@pytest.mark.parametrize(
    "key,value,expected",
    [
        ("tools.web_fetch.allowed_origins", '"https://example.com"', "expected a JSON array"),
        ("tools.web_fetch.allowed_origins", '["https://example.com/path"]', "http(s) origin"),
        ("tools.web_egress.proxy_url", '"proxy.example:8080"', "http(s) origin"),
        ("tools.web_search.custom_endpoints", '[{"url": "https://a.example/s", "extra": 1}]', "credential_env_names"),
    ],
)
def test_a_rejected_web_value_says_what_is_wrong(tmp_path, key, value, expected):
    loaded = _load_with(tmp_path / "user", key, value)
    [warning] = [warning for warning in loaded.warnings if warning.key == key]
    assert expected in format_message(warning_display_message(warning))


@pytest.mark.parametrize("coerce", [web_origins_coercer, web_custom_endpoints_coercer])
def test_a_blank_web_list_is_the_empty_list(coerce):
    """Clearing the panel field resets the list instead of reporting that no text was typed."""
    outcome = coerce("  ")
    assert outcome.status is CoerceStatus.VALID
    assert outcome.value == "[]"


@pytest.mark.parametrize("coerce", [web_origins_coercer, web_custom_endpoints_coercer])
def test_deeply_nested_json_is_refused_not_raised(coerce):
    outcome = coerce("[" * 200_000)
    assert outcome.status is CoerceStatus.INVALID
    assert outcome.reason is CoerceReason.EXPECTED_JSON_ARRAY


def test_web_list_coercer_keeps_any_json_array_unchanged() -> None:
    assert web_list_coercer("  ").value == "[]"
    assert web_list_coercer(None).status is CoerceStatus.MISSING
    kept = web_list_coercer('[1, "a", {"b": null}, "a"]')
    assert kept.status is CoerceStatus.VALID
    # No item check and no dedupe: those belong to each setting's own coercer.
    assert json.loads(kept.value) == [1, "a", {"b": None}, "a"]
    assert web_list_coercer(["x"]).value == '["x"]'
    refused = web_list_coercer('{"a": 1}')
    assert refused.status is CoerceStatus.INVALID
    assert refused.reason is CoerceReason.EXPECTED_JSON_ARRAY
    deep = web_list_coercer("[" * 200_000)
    assert deep.status is CoerceStatus.INVALID
    assert deep.reason is CoerceReason.EXPECTED_JSON_ARRAY
