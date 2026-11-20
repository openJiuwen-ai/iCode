# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from browser_use import chrome, diagnose
from browser_use.config import CHROME_DEVTOOLS_PREFERENCE, CHROME_LOGIN_PREFERENCE, BrowserConfig


def test_connect_saves_only_after_handshake_and_disconnect_restores_owned(tmp_path, monkeypatch, capsys):
    for name in ("BRIDGE", "CDP_URL", "BROKER", "ALLOWED_DOMAINS", "ALLOWED_ORIGINS"):
        monkeypatch.delenv(f"ICODE_BROWSER_{name}", raising=False)
    monkeypatch.setenv("ICODE_BROWSER_STATE_DIR", str(tmp_path))
    preference = tmp_path / CHROME_LOGIN_PREFERENCE
    monkeypatch.setattr(diagnose, "probe_bridge", lambda: ({"connected": True, "protocol": -1}, []))
    assert chrome.main(["connect", "--bridge"]) == 1
    assert not preference.exists()
    monkeypatch.setattr(
        diagnose, "probe_bridge", lambda: ({"connected": True, "protocol": chrome.PROTOCOL_VERSION}, [])
    )
    assert chrome.main(["connect", "--bridge"]) == 0
    assert BrowserConfig.from_env().backend == "bridge"
    assert chrome.main(["status"]) == 0
    assert "backend for new browsers: bridge" in capsys.readouterr().out
    assert chrome.main(["disconnect"]) == 0
    assert BrowserConfig.from_env().backend == "owned"


def test_fresh_install_connects_without_a_bridge(tmp_path, monkeypatch, capsys):
    from browser_use.bridge import install

    for name in ("BRIDGE", "CDP_URL", "BROKER", "ALLOWED_DOMAINS", "ALLOWED_ORIGINS"):
        monkeypatch.delenv(f"ICODE_BROWSER_{name}", raising=False)
    monkeypatch.setenv("ICODE_BROWSER_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(install, "registered_manifest", lambda _browser: None)
    monkeypatch.setattr(install, "_default_chrome_user_data_dir", lambda: tmp_path)
    (tmp_path / "DevToolsActivePort").write_text("12345\n/devtools/browser/chrome\n", encoding="utf-8")

    assert chrome.main(["connect"]) == 0
    assert (tmp_path / CHROME_DEVTOOLS_PREFERENCE).is_file()
    assert not (tmp_path / CHROME_LOGIN_PREFERENCE).exists()
    cfg = BrowserConfig.from_env()
    assert cfg.backend == "attach" and cfg.chrome_devtools and not cfg.cdp_url
    assert "Approve Chrome" in capsys.readouterr().out
    assert chrome.main(["disconnect"]) == 0
    assert BrowserConfig.from_env().backend == "owned"
