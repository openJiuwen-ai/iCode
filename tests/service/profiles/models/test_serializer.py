# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for model profile serialization (``profile_to_dict``, ``save_profile``, ``delete_profile``)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from chrys.service.profiles.models.loader import load_profile_from_yaml
from chrys.service.profiles.models.schema import ModelProfile
from chrys.service.profiles.models.serializer import delete_profile, profile_to_dict, save_profile


@pytest.fixture
def fake_config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect ``get_platform().config_dir`` to a temp directory."""
    fake_platform = type("P", (), {"config_dir": tmp_path})()
    monkeypatch.setattr("chrys.foundation.platform.get_platform", lambda: fake_platform)
    return tmp_path


def test_profile_to_dict_defaults_omitted() -> None:
    """Fields at their default value are omitted, except id/name."""
    p = ModelProfile(id="abc", name="My Profile")
    d = profile_to_dict(p)

    # Always emitted
    assert d["id"] == "abc"
    assert d["name"] == "My Profile"

    # Defaults omitted
    assert "provider" not in d  # default "openai"
    assert "api_style" not in d  # default "chat_completions"
    assert "model_id" not in d  # default ""
    assert "max_context_tokens" not in d  # default 200000
    assert "max_output_tokens" not in d  # default 32000 (DEFAULT_MAX_OUTPUT_TOKENS)
    assert "base_url" not in d
    assert "api_key" not in d
    assert "http_connect_timeout" not in d
    assert "http_read_timeout" not in d
    assert "http_max_retries" not in d
    assert "verify_ssl" not in d
    assert "bypass_proxy" not in d
    assert "http_headers" not in d
    assert "chat_options" not in d
    assert "stream" not in d
    assert "vision" not in d


def test_profile_to_dict_non_defaults_emitted() -> None:
    """Non-default values are included in output."""
    p = ModelProfile(
        id="abc",
        name="My Profile",
        provider="anthropic",
        api_style="responses",
        model_id="claude-opus-4-6",
        max_context_tokens=250000,
        max_output_tokens=64000,
        base_url="https://api.example.com",
        api_key="sk-xxx",
        http_connect_timeout=20.0,
        http_read_timeout=500.0,
        http_max_retries=5,
        verify_ssl=False,
        bypass_proxy=True,
        http_headers='{"X-Foo": "bar"}',
        chat_options='{"temperature": 0.5}',
        stream=False,
        vision=True,
    )
    d = profile_to_dict(p)

    assert d["id"] == "abc"
    assert d["name"] == "My Profile"
    assert d["provider"] == "anthropic"
    assert d["api_style"] == "responses"
    assert d["model_id"] == "claude-opus-4-6"
    assert d["max_context_tokens"] == 250000
    assert d["max_output_tokens"] == 64000
    assert d["base_url"] == "https://api.example.com"
    assert d["api_key"] == "sk-xxx"
    assert d["http_connect_timeout"] == 20.0
    assert d["http_read_timeout"] == 500.0
    assert d["http_max_retries"] == 5
    assert d["verify_ssl"] is False
    assert d["bypass_proxy"] is True
    assert d["http_headers"] == '{"X-Foo": "bar"}'
    assert d["chat_options"] == '{"temperature": 0.5}'
    assert d["stream"] is False
    assert d["vision"] is True


def test_profile_to_dict_id_and_name_always_emitted_even_if_empty() -> None:
    """``id`` and ``name`` are always included, even at default-ish values."""
    p = ModelProfile(id="", name="")
    d = profile_to_dict(p)
    assert "id" in d
    assert "name" in d
    assert d["id"] == ""
    assert d["name"] == ""


def test_save_profile_writes_yaml(fake_config_dir: Path) -> None:
    """``save_profile`` writes ``{config_dir}/models/{id}.yaml``."""
    p = ModelProfile(id="profile-xyz", name="Test", provider="anthropic", model_id="claude-sonnet-4-6")
    path = save_profile(p)

    assert path == fake_config_dir / "models" / "profile-xyz.yaml"
    assert path.is_file()

    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert loaded["id"] == "profile-xyz"
    assert loaded["name"] == "Test"
    assert loaded["provider"] == "anthropic"
    assert loaded["model_id"] == "claude-sonnet-4-6"
    # Defaults should not be in the YAML
    assert "max_context_tokens" not in loaded
    assert "stream" not in loaded
    assert "vision" not in loaded


def test_save_and_load_preserves_env_templates(fake_config_dir: Path) -> None:
    """Env placeholders stay literal on disk and are resolved only at runtime."""
    profile = ModelProfile(
        id="templated",
        name="Templated",
        model_id="gpt-test",
        api_key="{{CHRYS_OPENAI_KEY}}",
        http_headers='{"Authorization": "Bearer {{CHRYS_HEADER_TOKEN}}"}',
        chat_options='{"metadata": {"token": "{{CHRYS_CHAT_TOKEN}}"}}',
    )

    path = save_profile(profile)
    loaded = load_profile_from_yaml(path)

    assert loaded.api_key == "{{CHRYS_OPENAI_KEY}}"
    assert loaded.http_headers == '{"Authorization": "Bearer {{CHRYS_HEADER_TOKEN}}"}'
    assert loaded.chat_options == '{"metadata": {"token": "{{CHRYS_CHAT_TOKEN}}"}}'


def test_save_profile_creates_parent_dir(fake_config_dir: Path) -> None:
    """``save_profile`` creates the models/ directory if it does not exist."""
    assert not (fake_config_dir / "models").exists()
    p = ModelProfile(id="new-id", name="New")
    path = save_profile(p)
    assert path.parent.is_dir()


def test_save_profile_overwrites_existing(fake_config_dir: Path) -> None:
    """Re-saving the same id overwrites the existing file."""
    p1 = ModelProfile(id="same-id", name="First")
    save_profile(p1)

    p2 = ModelProfile(id="same-id", name="Second")
    path = save_profile(p2)

    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert loaded["name"] == "Second"


def test_delete_profile_removes_existing_file(fake_config_dir: Path) -> None:
    """``delete_profile`` returns True and removes the file when present."""
    p = ModelProfile(id="to-delete", name="Delete me")
    path = save_profile(p)
    assert path.is_file()

    assert delete_profile("to-delete") is True
    assert not path.exists()


def test_delete_profile_returns_false_if_missing(fake_config_dir: Path) -> None:
    """``delete_profile`` returns False when there is no such file (no error)."""
    assert delete_profile("does-not-exist") is False


def test_delete_profile_false_when_dir_missing(fake_config_dir: Path) -> None:
    """Works even if the models/ directory has never been created."""
    # fake_config_dir exists, but models/ subdir doesn't
    assert not (fake_config_dir / "models").exists()
    assert delete_profile("anything") is False


@pytest.mark.parametrize(
    "unsafe_id",
    ["../evil", "sub/dir", "..", ".", "a\\b", "/abs", "C:\\drive", "a:b", ""],
)
def test_save_profile_rejects_unsafe_id(fake_config_dir: Path, unsafe_id: str) -> None:
    """A path-shaped id must not resolve ``<id>.yaml`` outside the models directory."""
    with pytest.raises(ValueError, match="filename-safe"):
        save_profile(ModelProfile(id=unsafe_id, name="Unsafe"))


def test_save_profile_traversal_writes_nothing(fake_config_dir: Path) -> None:
    """The ``../`` id raises before any file lands outside the models directory."""
    with pytest.raises(ValueError):
        save_profile(ModelProfile(id="../escaped", name="Escape"))
    assert not (fake_config_dir / "escaped.yaml").exists()


def test_delete_profile_rejects_unsafe_id(fake_config_dir: Path) -> None:
    """A path-shaped id must not unlink anything outside the models directory."""
    with pytest.raises(ValueError, match="filename-safe"):
        delete_profile("../evil")


def test_stream_requires_finish_reason_round_trips(tmp_path: Path) -> None:
    data = profile_to_dict(ModelProfile(id="glm", name="GLM", stream_requires_finish_reason=True))
    path = tmp_path / "glm.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")

    assert data["stream_requires_finish_reason"] is True
    assert "stream_requires_finish_reason" not in profile_to_dict(ModelProfile(id="d", name="D"))
    assert load_profile_from_yaml(path).stream_requires_finish_reason is True


@pytest.mark.parametrize("binding", ["drop_block", "error", "off"])
def test_thinking_settings_round_trip_through_disk(fake_config_dir: Path, binding: Any) -> None:
    profile = ModelProfile(id="c", name="Claude", thinking_block_binding=binding, auto_interleaved_thinking=False)

    raw = yaml.safe_load(save_profile(profile).read_text(encoding="utf-8"))
    loaded = load_profile_from_yaml(fake_config_dir / "models" / "c.yaml")

    assert (raw["thinking_block_binding"], raw["auto_interleaved_thinking"]) == (binding, False)
    assert (loaded.thinking_block_binding, loaded.auto_interleaved_thinking) == (binding, False)
    defaults = profile_to_dict(ModelProfile(id="d", name="D"))
    assert "thinking_block_binding" not in defaults
    assert "auto_interleaved_thinking" not in defaults


@pytest.mark.parametrize("stream", [True, False])
def test_stream_round_trips_through_disk(fake_config_dir: Path, stream: bool) -> None:
    """Streaming on is the default and is omitted on disk; off is written out and survives a re-save."""
    profile = ModelProfile(id="rt", name="RT", stream=stream, stream_requires_finish_reason=True)
    path = save_profile(profile)

    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert ("stream" in raw) is (not stream)
    if not stream:
        assert raw["stream"] is False
    loaded = load_profile_from_yaml(path)
    assert loaded.stream is stream
    assert loaded.stream_requires_finish_reason is True

    save_profile(loaded)
    assert yaml.safe_load(path.read_text(encoding="utf-8")) == raw
