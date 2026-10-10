# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for model profile loading from YAML files."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from chrys.service.profiles.models.loader import (
    ModelProfileLoadError,
    load_profile_from_yaml,
    load_profiles_from_dir,
)


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


@pytest.mark.parametrize("encoding", ["utf-8-sig", "utf-16", "utf-32", "gb18030"])
def test_load_profile_detects_encoding_without_changing_values(tmp_path: Path, encoding: str) -> None:
    path = tmp_path / "legacy.yaml"
    raw = "name: 中文模型配置\napi_key: abcdef\n".encode(encoding)
    path.write_bytes(raw)
    profile = load_profile_from_yaml(path)
    assert profile.name == "中文模型配置"
    assert profile.api_key == "abcdef"
    assert path.read_bytes() == raw


def test_damaged_profile_is_skipped_without_changing_its_bytes(tmp_path: Path) -> None:
    path = tmp_path / "damaged.yaml"
    raw = ("# 中文配置说明\n" * 20).encode() + b"name: damaged\napi_key: abc\xffdef\n"
    path.write_bytes(raw)
    _write(tmp_path / "good.yaml", "name: good\n")
    with pytest.raises(ModelProfileLoadError, match="Cannot read"):
        load_profile_from_yaml(path)
    assert [profile.name for profile in load_profiles_from_dir(tmp_path)] == ["good"]
    assert path.read_bytes() == raw


def test_load_minimal_profile(tmp_path: Path) -> None:
    """Only ``name`` is required; all other fields take their defaults."""
    p = _write(tmp_path / "abc.yaml", "name: My Profile\n")

    profile = load_profile_from_yaml(p)

    # id falls back to filename stem
    assert profile.id == "abc"
    assert profile.name == "My Profile"
    assert profile.provider == "openai"
    assert profile.api_style == "chat_completions"
    assert profile.model_id == ""
    assert profile.max_context_tokens == 200000
    assert profile.max_output_tokens == 32000  # DEFAULT_MAX_OUTPUT_TOKENS
    assert profile.base_url == ""
    assert profile.api_key == ""
    assert profile.http_connect_timeout == 10.0
    assert profile.http_read_timeout == 300.0
    assert profile.http_max_retries == 2
    assert profile.verify_ssl is True
    assert profile.bypass_proxy is False
    assert profile.http_headers == ""
    assert profile.chat_options == ""
    assert profile.stream is True
    assert profile.vision is False
    assert profile.stream_requires_finish_reason is False
    assert profile.thinking_block_binding == "auto"
    assert profile.auto_interleaved_thinking is True


@pytest.mark.parametrize(
    ("value", "loaded"),
    [
        ("auto", "auto"),
        ("drop_block", "drop_block"),
        ("error", "error"),
        ("'off'", "off"),
        # YAML reads these as false.
        ("off", "off"),
        ("false", "off"),
    ],
)
def test_thinking_block_binding_loads_each_choice(tmp_path: Path, value: str, loaded: str) -> None:
    path = _write(tmp_path / "binding.yaml", f"name: Binding\nthinking_block_binding: {value}\n")

    assert load_profile_from_yaml(path).thinking_block_binding == loaded


@pytest.mark.parametrize("value", ["drop", "Error", "''", "true", "on", "1", "null", "[error]"])
def test_an_unknown_thinking_block_binding_fails_the_profile(tmp_path: Path, value: str) -> None:
    path = _write(tmp_path / "binding.yaml", f"name: Binding\nthinking_block_binding: {value}\n")

    with pytest.raises(ModelProfileLoadError, match="thinking_block_binding"):
        load_profile_from_yaml(path)


@pytest.mark.parametrize(
    ("value", "loaded"),
    [("false", False), ("true", True), ("'no'", False), ("'yes'", True), ("0", False), ("null", True)],
)
def test_auto_interleaved_thinking_reads_like_other_switches(tmp_path: Path, value: str, loaded: bool) -> None:
    path = _write(tmp_path / "interleaved.yaml", f"name: Interleaved\nauto_interleaved_thinking: {value}\n")

    assert load_profile_from_yaml(path).auto_interleaved_thinking is loaded


@pytest.mark.parametrize("value", [1, 2, 99, 0, -1])
def test_too_small_context_window_uses_lenient_default(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    value: int,
) -> None:
    path = _write(tmp_path / f"small-{value}.yaml", f"name: Small\nmax_context_tokens: {value}\n")

    with caplog.at_level(logging.WARNING):
        profile = load_profile_from_yaml(path)

    assert profile.max_context_tokens == 200_000
    assert "must be at least 100" in caplog.text


@pytest.mark.parametrize("value", [100, 101, 10_000])
def test_derivable_context_window_loads_verbatim(tmp_path: Path, value: int) -> None:
    path = _write(tmp_path / f"valid-{value}.yaml", f"name: Valid\nmax_context_tokens: {value}\n")

    assert load_profile_from_yaml(path).max_context_tokens == value


def test_load_full_profile(tmp_path: Path) -> None:
    """All fields are respected when present in YAML."""
    p = _write(
        tmp_path / "ignored-stem.yaml",
        """
id: explicit-id
name: Full Profile
provider: anthropic
api_style: responses
model_id: claude-opus-4-6
max_context_tokens: 200000
max_output_tokens: 64000
base_url: https://example.com
api_key: sk-xyz
http_connect_timeout: 5.5
http_read_timeout: 120.0
http_max_retries: 7
verify_ssl: false
bypass_proxy: true
http_headers: '{"X-A": "1"}'
chat_options: '{"temperature": 0.7}'
stream: true
vision: true
""",
    )

    profile = load_profile_from_yaml(p)

    # Explicit id wins over filename stem
    assert profile.id == "explicit-id"
    assert profile.name == "Full Profile"
    assert profile.provider == "anthropic"
    assert profile.api_style == "responses"
    assert profile.model_id == "claude-opus-4-6"
    assert profile.max_context_tokens == 200000
    assert profile.max_output_tokens == 64000
    assert profile.base_url == "https://example.com"
    assert profile.api_key == "sk-xyz"
    assert profile.http_connect_timeout == 5.5
    assert profile.http_read_timeout == 120.0
    assert profile.http_max_retries == 7
    assert profile.verify_ssl is False
    assert profile.bypass_proxy is True
    assert profile.http_headers == '{"X-A": "1"}'
    assert profile.chat_options == '{"temperature": 0.7}'
    assert profile.stream is True
    assert profile.vision is True


def test_load_ignores_legacy_no_proxy(tmp_path: Path) -> None:
    """Old profiles with ``no_proxy`` still load; the value is no longer model config."""
    p = _write(
        tmp_path / "legacy.yaml",
        """
name: Legacy
no_proxy: localhost,127.0.0.1
""",
    )

    profile = load_profile_from_yaml(p)

    assert profile.name == "Legacy"
    assert profile.verify_ssl is True
    assert profile.bypass_proxy is False


def test_load_id_defaults_to_filename_stem(tmp_path: Path) -> None:
    """Without an explicit ``id`` field, the YAML file's stem is used."""
    p = _write(tmp_path / "my-uuid-stem.yaml", "name: X\n")
    assert load_profile_from_yaml(p).id == "my-uuid-stem"


def test_load_invalid_api_style_falls_back_to_chat_completions(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    p = _write(tmp_path / "x.yaml", "name: X\napi_style: completions\n")

    profile = load_profile_from_yaml(p)

    assert profile.api_style == "chat_completions"
    assert "api_style" in caplog.text


def test_numeric_coercion_from_strings(tmp_path: Path) -> None:
    """Numeric fields are coerced via int()/float()/bool()."""
    p = _write(
        tmp_path / "x.yaml",
        """
name: N
max_context_tokens: "50000"
http_connect_timeout: "2.5"
http_read_timeout: "99"
http_max_retries: "3"
verify_ssl: "false"
bypass_proxy: "true"
stream: 1
vision: 1
""",
    )
    profile = load_profile_from_yaml(p)
    assert profile.max_context_tokens == 50000
    assert profile.http_connect_timeout == 2.5
    assert profile.http_read_timeout == 99.0
    assert profile.http_max_retries == 3
    assert profile.verify_ssl is False
    assert profile.bypass_proxy is True
    assert profile.stream is True
    assert profile.vision is True


@pytest.mark.parametrize(
    ("yaml_value", "verify_ssl", "stream", "vision"),
    [
        (None, True, True, False),
        ("null", True, True, False),
        ('" FaLsE "', False, False, False),
        ('"on"', True, True, True),
        ('""', False, False, False),
        ('"sometimes"', True, True, True),
        ("0", False, False, False),
    ],
    ids=["missing", "null", "spaced-false", "on", "empty", "unknown-string", "zero"],
)
def test_bool_fields_read_yaml_spellings_and_keep_their_defaults(
    tmp_path: Path, yaml_value: str | None, verify_ssl: bool, stream: bool, vision: bool
) -> None:
    fields = "" if yaml_value is None else f"verify_ssl: {yaml_value}\nstream: {yaml_value}\nvision: {yaml_value}\n"
    profile = load_profile_from_yaml(_write(tmp_path / "x.yaml", f"name: B\n{fields}"))

    assert (profile.verify_ssl, profile.stream, profile.vision) == (verify_ssl, stream, vision)


def test_invalid_numeric_field_raises_load_error(tmp_path: Path) -> None:
    p = _write(tmp_path / "bad.yaml", "name: Bad\nhttp_read_timeout: nope\n")

    with pytest.raises(ModelProfileLoadError, match="http_read_timeout"):
        load_profile_from_yaml(p)


def test_load_profiles_from_dir_skips_invalid_numeric_fields(tmp_path: Path) -> None:
    _write(tmp_path / "good.yaml", "name: Good\n")
    _write(tmp_path / "bad.yaml", "name: Bad\nhttp_max_retries: nope\n")

    profiles = load_profiles_from_dir(tmp_path)

    assert [p.name for p in profiles] == ["Good"]


def test_load_profiles_from_dir_logs_skipped_invalid_numeric_fields(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _write(tmp_path / "good.yaml", "name: Good\n")
    bad_path = _write(tmp_path / "bad.yaml", "name: Bad\nhttp_max_retries: nope\n")

    with caplog.at_level("WARNING"):
        profiles = load_profiles_from_dir(tmp_path)

    assert [p.name for p in profiles] == ["Good"]
    assert f"Skipping invalid model profile {bad_path}" in caplog.text


def test_missing_file_raises(tmp_path: Path) -> None:
    """Raises ``ModelProfileLoadError`` for a nonexistent file."""
    with pytest.raises(ModelProfileLoadError, match="not found"):
        load_profile_from_yaml(tmp_path / "nope.yaml")


def test_invalid_yaml_raises(tmp_path: Path) -> None:
    """Raises ``ModelProfileLoadError`` when YAML is malformed."""
    p = _write(tmp_path / "bad.yaml", "name: x\n  bad: : :\n")
    with pytest.raises(ModelProfileLoadError, match="Invalid YAML"):
        load_profile_from_yaml(p)


def test_yaml_not_a_mapping_raises(tmp_path: Path) -> None:
    """A YAML list (not a mapping) is rejected."""
    p = _write(tmp_path / "list.yaml", "- one\n- two\n")
    with pytest.raises(ModelProfileLoadError, match="must be a mapping"):
        load_profile_from_yaml(p)


def test_yaml_scalar_rejected(tmp_path: Path) -> None:
    """A YAML scalar (not a mapping) is rejected."""
    p = _write(tmp_path / "scalar.yaml", "just a string\n")
    with pytest.raises(ModelProfileLoadError, match="must be a mapping"):
        load_profile_from_yaml(p)


def test_missing_name_raises(tmp_path: Path) -> None:
    """``name`` is a required field."""
    p = _write(tmp_path / "x.yaml", "provider: openai\n")
    with pytest.raises(ModelProfileLoadError, match="missing required 'name'"):
        load_profile_from_yaml(p)


def test_load_profiles_from_dir_sorted(tmp_path: Path) -> None:
    """Loads all .yaml and .yml files, skipping unrelated ones."""
    _write(tmp_path / "b.yaml", "name: B\n")
    _write(tmp_path / "a.yaml", "name: A\n")
    _write(tmp_path / "c.yml", "name: C\n")
    _write(tmp_path / "notes.txt", "ignore me\n")

    profiles = load_profiles_from_dir(tmp_path)

    names = [p.name for p in profiles]
    # .yaml loaded first (sorted), then .yml (sorted)
    assert names == ["A", "B", "C"]


def test_load_profiles_from_dir_skips_invalid(tmp_path: Path) -> None:
    """Invalid files are silently skipped, valid ones still loaded."""
    _write(tmp_path / "good.yaml", "name: Good\n")
    _write(tmp_path / "no_name.yaml", "provider: openai\n")  # missing name
    _write(tmp_path / "broken.yaml", "name: x\n  bad: : :\n")  # malformed YAML

    profiles = load_profiles_from_dir(tmp_path)

    assert [p.name for p in profiles] == ["Good"]


def test_load_profiles_from_dir_missing_returns_empty(tmp_path: Path) -> None:
    """Nonexistent directory returns an empty list (not an error)."""
    missing = tmp_path / "does-not-exist"
    assert load_profiles_from_dir(missing) == []


def test_load_profiles_from_dir_not_a_directory(tmp_path: Path) -> None:
    """A path that points to a file (not a directory) returns empty."""
    f = _write(tmp_path / "file.yaml", "name: X\n")
    assert load_profiles_from_dir(f) == []


def test_load_profile_handles_utf8_bom(tmp_path: Path) -> None:
    """Files with a UTF-8 BOM are decoded correctly via ``decode_bytes``."""
    p = tmp_path / "bom.yaml"
    p.write_bytes(b"\xef\xbb\xbfname: BOM Profile\n")
    profile = load_profile_from_yaml(p)
    assert profile.name == "BOM Profile"


def test_load_migrates_chat_options_max_tokens(tmp_path: Path) -> None:
    """A legacy chat-options ``max_tokens`` moves into ``max_output_tokens``."""
    p = _write(
        tmp_path / "legacy.yaml",
        """
name: Legacy
chat_options: '{"max_tokens": 8000, "temperature": 0.5}'
""",
    )

    profile = load_profile_from_yaml(p)

    assert profile.max_output_tokens == 8000
    assert json.loads(profile.chat_options) == {"temperature": 0.5}


def test_load_migration_empties_chat_options_when_max_tokens_was_only_key(tmp_path: Path) -> None:
    p = _write(
        tmp_path / "only.yaml",
        """
name: Only
chat_options: '{"max_tokens": 8000}'
""",
    )

    profile = load_profile_from_yaml(p)

    assert profile.max_output_tokens == 8000
    assert profile.chat_options == ""


def test_load_migrates_provider_native_output_cap_aliases(tmp_path: Path) -> None:
    """Responses ``max_output_tokens`` and chat ``max_completion_tokens`` migrate too."""
    p = _write(
        tmp_path / "responses.yaml",
        """
name: Responses Native
chat_options: '{"max_output_tokens": 4096}'
""",
    )
    profile = load_profile_from_yaml(p)
    assert profile.max_output_tokens == 4096
    assert profile.chat_options == ""

    p = _write(
        tmp_path / "chat.yaml",
        """
name: Chat Native
chat_options: '{"max_completion_tokens": 4096}'
""",
    )
    profile = load_profile_from_yaml(p)
    assert profile.max_output_tokens == 4096
    assert profile.chat_options == ""


def test_load_migration_multiple_aliases_canonical_max_tokens_wins(tmp_path: Path) -> None:
    """With several spellings present, canonical ``max_tokens`` wins (it also won on the wire)."""
    p = _write(
        tmp_path / "multi.yaml",
        """
name: Multi
chat_options: '{"max_tokens": 8000, "max_output_tokens": 4096, "temperature": 0.5}'
""",
    )

    profile = load_profile_from_yaml(p)

    assert profile.max_output_tokens == 8000
    assert json.loads(profile.chat_options) == {"temperature": 0.5}


def test_load_migration_conflict_keeps_explicit_max_output_tokens(tmp_path: Path) -> None:
    """When both are set, the explicit field wins and the legacy value is dropped."""
    p = _write(
        tmp_path / "conflict.yaml",
        """
name: Conflict
max_output_tokens: 8192
chat_options: '{"max_tokens": 20000}'
""",
    )

    profile = load_profile_from_yaml(p)

    assert profile.max_output_tokens == 8192
    assert profile.chat_options == ""


def test_load_migration_leaves_non_positive_or_templated_max_tokens_untouched(tmp_path: Path) -> None:
    """Values the migration cannot interpret stay in chat_options verbatim.

    The field itself still falls back to the default cap — nothing migrated
    into it — but the alias's presence keeps ``effective_chat_options`` from
    injecting a competing ``max_tokens``.
    """
    templated = 'name: T\nchat_options: \'{"max_tokens": "{{CHRYS_MAX_TOKENS}}"}\'\n'
    p = _write(tmp_path / "templated.yaml", templated)
    profile = load_profile_from_yaml(p)
    assert profile.max_output_tokens == 32000  # DEFAULT_MAX_OUTPUT_TOKENS
    assert profile.chat_options == '{"max_tokens": "{{CHRYS_MAX_TOKENS}}"}'

    p = _write(tmp_path / "zero.yaml", "name: Z\nchat_options: '{\"max_tokens\": 0}'\n")
    profile = load_profile_from_yaml(p)
    assert profile.max_output_tokens == 32000  # DEFAULT_MAX_OUTPUT_TOKENS
    assert profile.chat_options == '{"max_tokens": 0}'


def test_load_non_positive_max_output_tokens_falls_back(tmp_path: Path) -> None:
    """The cap is always positive: an explicit 0 is invalid, not an opt-out."""
    p = _write(tmp_path / "zero-field.yaml", "name: Z\nmax_output_tokens: 0\n")
    profile = load_profile_from_yaml(p)
    assert profile.max_output_tokens == 32000  # DEFAULT_MAX_OUTPUT_TOKENS

    # With a legacy alias present, the migrated value beats the default.
    p = _write(
        tmp_path / "zero-with-alias.yaml",
        """
name: Z2
max_output_tokens: 0
chat_options: '{"max_tokens": 8000}'
""",
    )
    profile = load_profile_from_yaml(p)
    assert profile.max_output_tokens == 8000
    assert profile.chat_options == ""


def test_bool_numeric_fields_raise_load_error(tmp_path: Path) -> None:
    """YAML ``true`` must not coerce to 1 (e.g. a silent 1-token output cap)."""
    p = _write(tmp_path / "bool-int.yaml", "name: B\nmax_output_tokens: true\n")
    with pytest.raises(ModelProfileLoadError, match="max_output_tokens"):
        load_profile_from_yaml(p)

    p = _write(tmp_path / "bool-context.yaml", "name: B\nmax_context_tokens: yes\n")
    with pytest.raises(ModelProfileLoadError, match="max_context_tokens"):
        load_profile_from_yaml(p)

    p = _write(tmp_path / "bool-float.yaml", "name: B\nhttp_connect_timeout: true\n")
    with pytest.raises(ModelProfileLoadError, match="http_connect_timeout"):
        load_profile_from_yaml(p)


def test_load_non_string_chat_options_warns(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """A YAML mapping (not a JSON string) is flagged at load time.

    The runtime parser ignores it with a generic warning; the load-time
    warning names the offending file.  A null value stays silent — it is
    equivalent to leaving the field out.
    """
    p = _write(tmp_path / "mapping.yaml", "name: M\nchat_options:\n  max_tokens: 8000\n")
    with caplog.at_level("WARNING"):
        profile = load_profile_from_yaml(p)
    assert "'chat_options' must be a JSON string" in caplog.text
    assert profile.max_output_tokens == 32000  # not migrated — DEFAULT_MAX_OUTPUT_TOKENS

    caplog.clear()
    p = _write(tmp_path / "null.yaml", "name: N\nchat_options:\n")
    with caplog.at_level("WARNING"):
        load_profile_from_yaml(p)
    assert "chat_options" not in caplog.text
