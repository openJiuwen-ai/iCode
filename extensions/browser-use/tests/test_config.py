# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

import os
from pathlib import Path

import pytest

from icode_browser_use.config import CHROME_LOGIN_PREFERENCE, BrowserConfig, BrowserEnv, ConfigError


SETTINGS = [
    ("HEADLESS", "yes", "headless", True),
    ("EXECUTABLE", "chrome", "executable", "chrome"),
    ("CDP_URL", "ws://localhost/test", "cdp_url", "ws://localhost/test"),
    ("BRIDGE", "1", "bridge", True),
    ("BRIDGE_FILE", "bridge.json", "bridge_file", "bridge.json"),
    ("BRIDGE_WAIT", "42", "bridge_wait", 42.0),
    ("PREWARM", "on", "prewarm", True),
    ("EVALUATE", "allow", "evaluate_enabled", True),
    ("SAVE_STATE", "allow", "save_state_enabled", True),
    ("IDLE_CLOSE_SECONDS", "0", "idle_close_seconds", 0.0),
    ("BROKER", "1", "broker", True),
    ("BROKER_AUTOSTART", "true", "broker_autostart", True),
    ("OWNER_ID", "owner", "broker_owner", "owner"),
    ("BROWSER_SESSION_ID", "session", "browser_session_id", "session"),
    ("LEASE_TTL", "45", "broker_lease_ttl", 45.0),
    ("BROKER_IDLE_EXIT", "12", "broker_idle_exit", 12.0),
    ("STATE_DIR", "state", "state_dir", Path("state")),
    ("UPLOAD_ROOTS", os.pathsep.join(["one", "two"]), "upload_roots", (Path("one"), Path("two"))),
    ("SCREENSHOT_KEEP", "5", "screenshot_keep", 5),
    ("DIALOG_TIMEOUT", "2.5", "dialog_timeout", 2.5),
    ("DOWNLOAD_MAX_MB", "5", "download_max_bytes", 5 * 1024 * 1024),
]


@pytest.mark.parametrize("suffix,value,attribute,expected", SETTINGS)
def test_aliases(suffix, value, attribute, expected):
    cfg = BrowserConfig.from_env({"ICODE_BROWSER_ALLOWED_DOMAINS": "example.com", "ICODE_BROWSER_" + suffix: value})
    assert getattr(cfg, attribute) == expected
    assert cfg.allowed_domains == ("example.com",)


@pytest.mark.parametrize("primary", ["invalid", ","])
def test_allowlist_is_fail_closed(primary):
    with pytest.raises(ConfigError):
        BrowserConfig.from_env({"ICODE_BROWSER_ALLOWED_DOMAINS": primary})


@pytest.mark.parametrize("env", [{}, {"ICODE_BROWSER_ALLOWED_DOMAINS": "  "}])
def test_public_web_is_the_default(env):
    assert BrowserConfig.from_env(env).public_web


def test_saved_chrome_login_defaults_to_bridge_and_respects_overrides(tmp_path):
    env = {"ICODE_BROWSER_STATE_DIR": str(tmp_path)}
    assert BrowserConfig.from_env(env).backend == "owned"
    (tmp_path / CHROME_LOGIN_PREFERENCE).touch()
    assert BrowserConfig.from_env(env).backend == "bridge"
    assert BrowserConfig.from_env({**env, "ICODE_BROWSER_BRIDGE": "0"}).backend == "owned"
    assert BrowserConfig.from_env({**env, "ICODE_BROWSER_CDP_URL": "http://127.0.0.1:9222"}).backend == "attach"
    broker = BrowserConfig.from_env({**env, "ICODE_BROWSER_BROKER": "1"})
    assert broker.backend == "broker" and not broker.bridge


def test_invalid_allowlist_diagnostic_does_not_leak_credentials(capsys):
    sentinel = "ALLOWLIST_CREDENTIAL_SENTINEL"
    cfg = BrowserConfig.from_env(
        {
            "ICODE_BROWSER_ALLOWED_DOMAINS": f"example.com,https://user:{sentinel}@private.invalid,bad/{sentinel}",
        }
    )
    assert cfg.allowed_domains == ("example.com",)
    captured = capsys.readouterr()
    assert sentinel not in captured.err
    assert sentinel not in captured.out
    assert "2 invalid allowlist entries" in captured.err
    assert "ICODE_BROWSER_ALLOWED_DOMAINS" in captured.err


@pytest.mark.parametrize("primary", [None, ""])
def test_explicit_mapping_does_not_inherit_state(monkeypatch, primary):
    monkeypatch.setenv("ICODE_BROWSER_STATE_DIR", "new-secret")
    env = {"ICODE_BROWSER_ALLOWED_DOMAINS": "example.com"}
    if primary is not None:
        env["ICODE_BROWSER_STATE_DIR"] = primary
    cfg = BrowserConfig.from_env(env)
    assert cfg.state_dir == Path.home() / ".cache" / "icode-browser-use"
    assert BrowserEnv({}).state_dir() == cfg.state_dir


def test_implicit_environment_and_default_factory(monkeypatch, tmp_path):
    for name in tuple(os.environ):
        if name.startswith("ICODE_BROWSER_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("ICODE_BROWSER_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("ICODE_BROWSER_ALLOWED_DOMAINS", "example.com")
    assert BrowserConfig.from_env().state_dir == tmp_path
    assert BrowserConfig().state_dir == tmp_path


@pytest.mark.parametrize(
    "suffix,value",
    [
        ("ALLOWED_DOMAINS", "invalid"),
        ("BRIDGE_WAIT", "invalid"),
        ("BRIDGE_WAIT", "-1"),
        ("IDLE_CLOSE_SECONDS", "invalid"),
        ("IDLE_CLOSE_SECONDS", "-1"),
        ("SCREENSHOT_KEEP", "invalid"),
        ("SCREENSHOT_KEEP", "0"),
        ("DIALOG_TIMEOUT", "invalid"),
        ("DIALOG_TIMEOUT", "0"),
        ("DOWNLOAD_MAX_MB", "invalid"),
        ("DOWNLOAD_MAX_MB", "0"),
    ],
)
def test_config_errors_name_the_variable(suffix, value):
    with pytest.raises(ConfigError) as info:
        BrowserConfig.from_env(
            {
                "ICODE_BROWSER_ALLOWED_DOMAINS": "example.com",
                "ICODE_BROWSER_" + suffix: value,
            }
        )
    assert str(info.value).startswith("ICODE_BROWSER_" + suffix)


@pytest.mark.parametrize(
    "suffix", ["BRIDGE_WAIT", "IDLE_CLOSE_SECONDS", "SCREENSHOT_KEEP", "DIALOG_TIMEOUT", "DOWNLOAD_MAX_MB"]
)
def test_invalid_number_does_not_fall_back(suffix):
    with pytest.raises(ConfigError):
        BrowserConfig.from_env(
            {
                "ICODE_BROWSER_ALLOWED_DOMAINS": "example.com",
                "ICODE_BROWSER_" + suffix: "invalid",
            }
        )


def test_downloads_are_opt_in_and_use_the_state_dir(tmp_path):
    base = {"ICODE_BROWSER_ALLOWED_DOMAINS": "example.com", "ICODE_BROWSER_STATE_DIR": str(tmp_path)}
    assert BrowserConfig.from_env(base).download_dir is None
    assert BrowserConfig.from_env({**base, "ICODE_BROWSER_DOWNLOADS": "yes"}).download_dir is None
    enabled = BrowserConfig.from_env({**base, "ICODE_BROWSER_DOWNLOADS": "allow"})
    assert enabled.download_dir == tmp_path / "downloads"
    override = BrowserConfig.from_env(
        {**base, "ICODE_BROWSER_DOWNLOADS": "allow", "ICODE_BROWSER_DOWNLOAD_DIR": "elsewhere"}
    )
    assert override.download_dir == Path("elsewhere")
