# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from icode_browser_use import chrome, diagnose
from icode_browser_use.config import CHROME_DEVTOOLS_PREFERENCE, CHROME_LOGIN_PREFERENCE, BrowserConfig


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
    import socket

    from icode_browser_use.bridge import install

    for name in ("BRIDGE", "CDP_URL", "BROKER", "ALLOWED_DOMAINS", "ALLOWED_ORIGINS", "CHROME_DEVTOOLS"):
        monkeypatch.delenv(f"ICODE_BROWSER_{name}", raising=False)
    monkeypatch.setenv("ICODE_BROWSER_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(install, "registered_manifest", lambda _browser: None)
    monkeypatch.setattr(install, "_default_chrome_user_data_dir", lambda: tmp_path)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as srv:
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]
        (tmp_path / "DevToolsActivePort").write_text(f"{port}\n/devtools/browser/chrome\n", encoding="utf-8")

        assert chrome.main(["connect"]) == 0
    assert (tmp_path / CHROME_DEVTOOLS_PREFERENCE).is_file()
    assert not (tmp_path / CHROME_LOGIN_PREFERENCE).exists()
    cfg = BrowserConfig.from_env()
    assert cfg.backend == "attach" and cfg.chrome_devtools and not cfg.cdp_url
    assert "Approve Chrome" in capsys.readouterr().out
    assert chrome.main(["disconnect"]) == 0
    assert BrowserConfig.from_env().backend == "owned"


def test_remember_native_login_rejects_chrome_devtools_zero_and_stale_port(tmp_path, monkeypatch):
    import asyncio
    import socket

    import pytest

    from icode_browser_use.bridge import install

    for name in ("BRIDGE", "CDP_URL", "BROKER", "ALLOWED_DOMAINS", "ALLOWED_ORIGINS"):
        monkeypatch.delenv(f"ICODE_BROWSER_{name}", raising=False)
    monkeypatch.setenv("ICODE_BROWSER_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("ICODE_BROWSER_CHROME_DEVTOOLS", "0")
    with pytest.raises(RuntimeError, match="environment override"):
        asyncio.run(chrome.remember_native_login())

    # Stale DevToolsActivePort pointing at a closed port must not pass _devtools_ready.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as closed_sock:
        closed_sock.bind(("127.0.0.1", 0))
        dead_port = closed_sock.getsockname()[1]
    monkeypatch.delenv("ICODE_BROWSER_CHROME_DEVTOOLS", raising=False)
    monkeypatch.setattr(install, "_default_chrome_user_data_dir", lambda: tmp_path)
    (tmp_path / "DevToolsActivePort").write_text(f"{dead_port}\n/devtools/browser/chrome\n", encoding="utf-8")
    assert asyncio.run(chrome._devtools_ready(install.chrome_devtools_ws)) is False
