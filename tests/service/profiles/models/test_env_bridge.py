# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Model defaults, live profile selection, and proxy environment validation."""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import yaml

from chrys.foundation.config.runtime_pointer import get_model_pointer
from chrys.foundation.config.spec import Source
from chrys.service.llm.clients import _build_profile_http_client
from chrys.service.profiles.models.env_bridge import (
    activate_model_profile,
    get_active_profile_id,
    get_global_default_profile_id,
    is_valid_no_proxy,
    is_valid_proxy_url,
    sanitize_no_proxy_env,
    sanitize_proxy_url_env,
    set_global_default_profile_id,
)
from chrys.service.profiles.models.schema import ModelProfile


@pytest.fixture(autouse=True)
def config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("chrys.foundation.platform.get_platform", lambda: SimpleNamespace(config_dir=tmp_path))


def test_get_active_profile_id_reads_chrys_model_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHRYS_MODEL_PROFILE", "new-id")
    assert get_active_profile_id() == "new-id"


def test_get_active_profile_id_ignores_retired_legacy_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """The pre-rename ``MODEL_PROFILE`` variable is no longer honoured."""
    monkeypatch.delenv("CHRYS_MODEL_PROFILE", raising=False)
    monkeypatch.setenv("MODEL_PROFILE", "legacy-id")
    assert get_active_profile_id() == ""


def test_get_active_profile_id_empty_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CHRYS_MODEL_PROFILE", raising=False)
    assert get_active_profile_id() == ""


def _read_stored_pointer(config_dir: Path) -> object | None:
    settings_path = config_dir / "settings.yaml"
    if not settings_path.exists():
        return None
    doc = yaml.safe_load(settings_path.read_text(encoding="utf-8"))
    try:
        return doc["model"]["profile"]["active"]
    except KeyError, TypeError:
        return None


def test_activate_model_profile_updates_only_pointer_and_live_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_original = "# keep\nCHRYS_THEME=file-theme\nCHRYS_MODEL_PROFILE=old-id\n"
    path = tmp_path / ".env"
    path.write_text(env_original, encoding="utf-8")
    monkeypatch.setenv("CHRYS_MODEL_PROFILE", "old-id")
    monkeypatch.setenv("CHRYS_THEME", "process-theme")

    activate_model_profile("new-id")
    # The durable pointer lives in the settings document.
    assert path.read_text(encoding="utf-8") == env_original
    assert _read_stored_pointer(tmp_path) == "new-id"
    assert os.environ["CHRYS_MODEL_PROFILE"] == "new-id"
    assert os.environ["CHRYS_THEME"] == "process-theme"
    origin = get_model_pointer()[1]
    assert origin is not None and origin.layer is Source.PROCESS_RUNTIME


def test_activate_model_profile_leaves_the_dotenv_untouched(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Activation leaves legacy files untouched."""
    env_original = "OPENAI_API_KEY=user-owned\nCHRYS_MODEL_PROFILE=old-id\n"
    path = tmp_path / ".env"
    path.write_text(env_original, encoding="utf-8")
    monkeypatch.delenv("CHRYS_MODEL_PROFILE", raising=False)

    activate_model_profile("new-id")

    assert path.read_text(encoding="utf-8") == env_original
    os.environ.pop("CHRYS_MODEL_PROFILE", None)


def test_set_global_default_profile_id_is_file_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHRYS_MODEL_PROFILE", "process-id")

    set_global_default_profile_id("file-id")

    assert _read_stored_pointer(tmp_path) == "file-id"
    assert os.environ["CHRYS_MODEL_PROFILE"] == "process-id"
    assert not (tmp_path / ".env").exists()


def test_set_global_default_profile_id_empty_removes_the_stored_pointer(tmp_path: Path) -> None:
    set_global_default_profile_id("file-id")
    assert get_global_default_profile_id() == "file-id"

    set_global_default_profile_id("")

    assert _read_stored_pointer(tmp_path) is None
    assert get_global_default_profile_id() == ""


def test_get_global_default_profile_id_reads_file_instead_of_process_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "settings.yaml").write_text(
        yaml.safe_dump({"model": {"profile": {"active": "file-id"}}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("CHRYS_MODEL_PROFILE", "process-id")

    assert get_global_default_profile_id() == "file-id"


def test_get_global_default_profile_id_ignores_the_dotenv(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A leftover dotenv pointer line never supplies the stored default."""
    (tmp_path / ".env").write_text("CHRYS_MODEL_PROFILE=legacy-file-id\n", encoding="utf-8")
    monkeypatch.setenv("CHRYS_MODEL_PROFILE", "legacy-process-id")

    assert get_global_default_profile_id() == ""


# ── NO_PROXY validation / scrub ─────────────────────────────────────


def test_is_valid_no_proxy_accepts_typical_inputs() -> None:
    """Common shapes the placeholder advertises must validate."""
    assert is_valid_no_proxy("") is True
    assert is_valid_no_proxy("localhost") is True
    assert is_valid_no_proxy("127.0.0.1") is True
    assert is_valid_no_proxy("::1") is True  # bare IPv6, no brackets
    assert is_valid_no_proxy("localhost,127.0.0.1,.example.com") is True
    assert is_valid_no_proxy("*.example.com") is True
    assert is_valid_no_proxy("*") is True  # bypass-everything wildcard
    assert is_valid_no_proxy("http://x.y") is True


def test_is_valid_no_proxy_rejects_non_printable() -> None:
    """Non-printable ASCII (tab/LF/control) — fails ``urlparse`` in httpx."""
    assert is_valid_no_proxy("host\twithtab") is False
    assert is_valid_no_proxy("a\nb") is False
    assert is_valid_no_proxy("\x7f") is False  # DEL


def test_is_valid_no_proxy_rejects_bracketed_ipv6() -> None:
    """``[::1]`` is printable but URLPattern rejects it (brackets are added by httpx)."""
    assert is_valid_no_proxy("[::1]") is False


def test_is_valid_no_proxy_rejects_when_any_entry_is_bad() -> None:
    """One bad entry in a comma-separated list invalidates the whole value."""
    assert is_valid_no_proxy("localhost,[::1]") is False
    assert is_valid_no_proxy("good,host\twithtab,also-good") is False


def test_is_valid_no_proxy_wildcard_short_circuits() -> None:
    """``*`` short-circuits httpx's bypass parsing; later entries are ignored.

    ``httpx._utils.get_environment_proxies`` returns ``{}`` immediately on
    ``*``, so malformed entries that follow never reach ``URLPattern`` —
    the validator must mirror that to avoid false rejections.
    """
    assert is_valid_no_proxy("*,[::1]") is True
    assert is_valid_no_proxy("localhost,*,host\twithtab") is True


def test_sanitize_no_proxy_env_noop_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    assert sanitize_no_proxy_env() == {}


def test_sanitize_no_proxy_env_keeps_valid(monkeypatch: pytest.MonkeyPatch) -> None:
    # ``delenv`` first because Windows ``os.environ`` is case-insensitive —
    # deleting ``no_proxy`` after setting ``NO_PROXY`` would wipe the same key.
    monkeypatch.delenv("no_proxy", raising=False)
    monkeypatch.setenv("NO_PROXY", "localhost,*.example.com")
    assert sanitize_no_proxy_env() == {}
    assert os.environ["NO_PROXY"] == "localhost,*.example.com"


def test_sanitize_no_proxy_env_scrubs_uppercase(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("no_proxy", raising=False)
    monkeypatch.setenv("NO_PROXY", "host\twithtab")
    assert sanitize_no_proxy_env() == {"NO_PROXY": "host\twithtab"}
    assert "NO_PROXY" not in os.environ


@pytest.mark.skipif(
    os.name == "nt",
    reason="Windows env vars are case-insensitive: setting 'no_proxy' actually sets 'NO_PROXY'.",
)
def test_sanitize_no_proxy_env_scrubs_lowercase(monkeypatch: pytest.MonkeyPatch) -> None:
    """httpx falls back to lowercase when NO_PROXY is empty — must scrub it too."""
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.setenv("no_proxy", "bad\nvalue")
    assert sanitize_no_proxy_env() == {"no_proxy": "bad\nvalue"}
    assert "no_proxy" not in os.environ


@pytest.mark.skipif(
    os.name == "nt",
    reason="Windows env vars are case-insensitive: 'NO_PROXY' and 'no_proxy' are the same key.",
)
def test_sanitize_no_proxy_env_scrubs_both_when_both_bad(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NO_PROXY", "U\tBAD")
    monkeypatch.setenv("no_proxy", "l\tbad")
    scrubbed = sanitize_no_proxy_env()
    assert scrubbed == {"NO_PROXY": "U\tBAD", "no_proxy": "l\tbad"}
    assert "NO_PROXY" not in os.environ
    assert "no_proxy" not in os.environ


# ── Proxy address validation / scrub ────────────────────────────────

_PROXY_URL_ENV_VARS = ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy")


def _clear_proxy_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drop every ``*_proxy`` variable: urllib reads any of them, in either casing."""
    for name in list(os.environ):
        if name.lower().endswith("_proxy"):
            monkeypatch.delenv(name)


async def _constructs(build: Callable[[], httpx.Client | httpx.AsyncClient]) -> bool:
    try:
        client = build()
    except httpx.InvalidURL, ValueError:
        return False
    if isinstance(client, httpx.Client):
        client.close()
    else:
        await client.aclose()
    return True


def _profile(provider: str, *, bypass_proxy: bool) -> ModelProfile:
    return ModelProfile(
        id="p",
        name="p",
        provider=provider,
        model_id="test-model",
        api_key="sk-test",
        base_url="https://provider.example",
        bypass_proxy=bypass_proxy,
    )


@pytest.mark.parametrize(
    "value",
    [
        "socks://127.0.0.1:7897",
        "socks://127.0.0.1:7897/",
        "socks4://127.0.0.1:1080",
        "socks4a://127.0.0.1:1080",
        "ftp://127.0.0.1:21",
        "http://[::1",
        "127.0.0.1:abc",
        " http://127.0.0.1:8080",
        "http://127.0.0.1:8080",
        "https://127.0.0.1:8443",
        "socks5://127.0.0.1:1080",
        "socks5h://127.0.0.1:1080",
        "SOCKS5H://127.0.0.1:1080",
        "127.0.0.1:7897",
        "http://user:pass@127.0.0.1:8080",
    ],
)
async def test_is_valid_proxy_url_matches_client_construction(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    """The verdict is exactly whether every client that reads the environment can be built.

    Covers plain httpx (HTTP MCP servers, the update check) and the model
    client builder for both SDKs, with and without the profile's proxy
    bypass, whose mounts come too late to save a proxy httpx can't build.
    """
    _clear_proxy_env(monkeypatch)
    monkeypatch.setenv("ALL_PROXY", value)
    builders: dict[str, Callable[[], httpx.Client | httpx.AsyncClient]] = {
        "httpx.Client": httpx.Client,
        "httpx.AsyncClient": httpx.AsyncClient,
        **{
            f"{provider} bypass={bypass}": (
                lambda provider=provider, bypass=bypass: _build_profile_http_client(
                    _profile(provider, bypass_proxy=bypass), httpx.Timeout(5.0)
                )
            )
            for provider in ("anthropic", "openai")
            for bypass in (False, True)
        },
    }

    verdict = is_valid_proxy_url(value)

    assert {name: await _constructs(build) for name, build in builders.items()} == dict.fromkeys(builders, verdict)


def test_is_valid_proxy_url_accepts_empty() -> None:
    """urllib skips an empty value, so httpx never sees it."""
    assert is_valid_proxy_url("") is True


def test_sanitize_proxy_url_env_noop_when_clean(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_proxy_env(monkeypatch)
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:7897")
    monkeypatch.setenv("ALL_PROXY", "")

    assert sanitize_proxy_url_env() == {}
    assert os.environ["HTTPS_PROXY"] == "http://127.0.0.1:7897"
    assert os.environ["ALL_PROXY"] == ""


def test_sanitize_proxy_url_env_scrubs_only_the_invalid_value(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_proxy_env(monkeypatch)
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:7897")
    monkeypatch.setenv("ALL_PROXY", "socks://127.0.0.1:7897")

    assert sanitize_proxy_url_env() == {"ALL_PROXY": "socks://127.0.0.1:7897"}
    assert "ALL_PROXY" not in os.environ
    assert os.environ["HTTPS_PROXY"] == "http://127.0.0.1:7897"
    assert httpx._utils.get_environment_proxies() == {"https://": "http://127.0.0.1:7897"}


@pytest.mark.skipif(
    os.name == "nt",
    reason="Windows env vars are case-insensitive: 'all_proxy' and 'ALL_PROXY' are the same key.",
)
def test_sanitize_proxy_url_env_checks_each_casing(monkeypatch: pytest.MonkeyPatch) -> None:
    """urllib prefers the lowercase name, so a bad lowercase value is the one httpx would read."""
    _clear_proxy_env(monkeypatch)
    monkeypatch.setenv("all_proxy", "socks://127.0.0.1:7897")
    monkeypatch.setenv("ALL_PROXY", "socks5h://127.0.0.1:7897")
    monkeypatch.setenv("http_proxy", "socks4://127.0.0.1:1080")
    monkeypatch.setenv("HTTP_PROXY", "socks://127.0.0.1:1080")

    assert sanitize_proxy_url_env() == {
        "HTTP_PROXY": "socks://127.0.0.1:1080",
        "http_proxy": "socks4://127.0.0.1:1080",
        "all_proxy": "socks://127.0.0.1:7897",
    }
    assert httpx._utils.get_environment_proxies() == {"all://": "socks5h://127.0.0.1:7897"}


@pytest.mark.parametrize(
    ("value", "shown"),
    [
        ("socks://alice:s3cret@proxy.example:1080", "socks://***@proxy.example:1080"),
        ("socks4://s3cret@proxy.example:1080/", "socks4://***@proxy.example:1080/"),
        ("socks://alice:pa/s3cret@proxy.example:1080", "socks://***@proxy.example:1080"),
        ("socks://alice:p@s3cret@proxy.example:1080", "socks://***@proxy.example:1080"),
        ("alice:s3cret@proxy.example:abc", "***@proxy.example:abc"),
        ("alice:s3cret://part@proxy.example:8080", "***@proxy.example:8080"),
        ("socks://proxy.example:1080", "socks://proxy.example:1080"),
    ],
)
def test_sanitize_proxy_url_env_masks_credentials(monkeypatch: pytest.MonkeyPatch, value: str, shown: str) -> None:
    """The returned value feeds a warning, so a password in the address never leaves the environment."""
    _clear_proxy_env(monkeypatch)
    monkeypatch.setenv("ALL_PROXY", value)

    assert sanitize_proxy_url_env() == {"ALL_PROXY": shown}
    assert "ALL_PROXY" not in os.environ
