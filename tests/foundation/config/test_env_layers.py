# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for the frozen process snapshot, key classification, and dotenv layers."""

from __future__ import annotations

import dataclasses
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from dotenv import load_dotenv

from chrys.foundation.config.env_layers import (
    IPC_ENV_NAMES,
    POINTER_ENV_NAMES,
    EnvNameKind,
    canonical_env_name,
    classify_env_name,
    freeze_process_env,
    inject_bootstrap_dotenv,
    process_env_snapshot,
    read_dotenv_layer,
    read_dotenv_snapshot,
)
from chrys.foundation.config.settings import Settings
from chrys.foundation.config.spec import specs_by_field

# The canonical duplicate-occurrence sample (design §3.1): a dict-folding
# parse loses the first ``A`` and resolves ``B`` wrongly.
_OCCURRENCE_SAMPLE = "A=one\nB=${A}\nA=two\nC=${A}\n"


def _fake_platform(monkeypatch: pytest.MonkeyPatch, os_name: str) -> None:
    import chrys.foundation.platform as platform_mod

    fake = dataclasses.replace(platform_mod.get_platform(), os_name=os_name)
    monkeypatch.setattr(platform_mod, "get_platform", lambda: fake)


def _claim_env_name(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    """Register ``name`` for restore so injected values never outlive the test."""
    monkeypatch.setenv(name, "claimed")
    monkeypatch.delenv(name)


# --- canonical_env_name / classify_env_name ---


def test_canonical_env_name_folds_case_only_on_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_platform(monkeypatch, "windows")
    assert canonical_env_name("chrys_model_profile") == "CHRYS_MODEL_PROFILE"

    _fake_platform(monkeypatch, "linux")
    assert canonical_env_name("chrys_model_profile") == "chrys_model_profile"


def test_every_spec_env_alias_classifies_as_setting_or_pointer() -> None:
    for entry in specs_by_field(Settings).values():
        if entry.env is None:
            continue
        expected = EnvNameKind.POINTER if entry.env in POINTER_ENV_NAMES else EnvNameKind.SETTING
        assert classify_env_name(entry.env) is expected


def test_classification_sets_are_uppercase_so_the_windows_fold_can_match() -> None:
    """The fold uppercases probes, so a lowercase member could never match."""
    declared = {entry.env for entry in specs_by_field(Settings).values() if entry.env is not None}
    for name in declared | IPC_ENV_NAMES | POINTER_ENV_NAMES:
        assert name == name.upper()


def test_classify_env_name_covers_all_three_kinds_and_outsiders() -> None:
    assert classify_env_name("CHRYS_HOOK_ID") is EnvNameKind.IPC
    assert classify_env_name("CHRYS_MODEL_PROFILE") is EnvNameKind.POINTER
    # Exact-name matching, not prefix: the judge profile is a plain setting.
    assert classify_env_name("CHRYS_MODEL_PROFILE_APPROVAL_JUDGE") is EnvNameKind.SETTING
    assert classify_env_name("PATH") is None
    assert classify_env_name("CHRYS_NOT_A_REAL_KEY") is None


def test_classification_is_case_insensitive_only_on_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_platform(monkeypatch, "windows")
    assert classify_env_name("chrys_model_profile") is EnvNameKind.POINTER

    _fake_platform(monkeypatch, "linux")
    assert classify_env_name("chrys_model_profile") is None


# --- ProcessEnvSnapshot ---


def test_snapshot_is_none_until_frozen_and_first_freeze_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    assert process_env_snapshot() is None

    monkeypatch.setenv("SNAPSHOT_PROBE", "before")
    first = freeze_process_env()
    assert process_env_snapshot() is first
    assert first.values["SNAPSHOT_PROBE"] == "before"

    monkeypatch.setenv("SNAPSHOT_PROBE", "after")
    assert freeze_process_env() is first
    assert first.values["SNAPSHOT_PROBE"] == "before"


def test_snapshot_mapping_rejects_writes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SNAPSHOT_PROBE", "value")
    snapshot = freeze_process_env()
    with pytest.raises(TypeError):
        snapshot.values["SNAPSHOT_PROBE"] = "mutated"  # type: ignore[index]


# --- read_dotenv_layer ---


def test_read_dotenv_layer_preserves_duplicate_occurrences(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text(_OCCURRENCE_SAMPLE, encoding="utf-8")

    layer = read_dotenv_layer(path, base={})

    assert layer == {"A": "two", "B": "one", "C": "two"}


def test_read_dotenv_layer_resolves_against_the_given_base_not_the_live_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LAYER_BASE_PROBE", "live")
    path = tmp_path / ".env"
    path.write_text("DERIVED=${LAYER_BASE_PROBE}\n", encoding="utf-8")

    layer = read_dotenv_layer(path, base={"LAYER_BASE_PROBE": "frozen"})

    assert layer == {"DERIVED": "frozen"}


def test_read_dotenv_layer_drops_bare_keys_and_missing_files(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text("BARE_KEY\nREAL=value\n", encoding="utf-8")
    assert read_dotenv_layer(path, base={}) == {"REAL": "value"}

    assert read_dotenv_layer(tmp_path / "absent.env", base={}) == {}


def test_read_dotenv_layer_sees_external_edits_on_the_next_call(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text("KEY=first\n", encoding="utf-8")
    assert read_dotenv_layer(path, base={}) == {"KEY": "first"}

    path.write_text("KEY=second\n", encoding="utf-8")
    assert read_dotenv_layer(path, base={}) == {"KEY": "second"}


# --- contract with load_dotenv ---


@pytest.mark.parametrize("override", [True, False])
def test_injection_matches_load_dotenv_on_the_occurrence_sample(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, override: bool
) -> None:
    """Byte-for-byte parity with the calls this module replaced.

    ``DotEnv.parse`` and ``dotenv.variables`` are undocumented; python-dotenv
    is pinned exactly, and this test is the tripwire if an upgrade changes
    either resolution direction.
    """
    path = tmp_path / ".env"
    path.write_text(_OCCURRENCE_SAMPLE, encoding="utf-8")
    for name in ("A", "B", "C"):
        _claim_env_name(monkeypatch, name)
    monkeypatch.setenv("A", "shell")

    load_dotenv(path, override=override)
    expected = {name: os.environ.get(name) for name in ("A", "B", "C")}

    monkeypatch.delenv("B", raising=False)
    monkeypatch.delenv("C", raising=False)
    monkeypatch.setenv("A", "shell")

    inject_bootstrap_dotenv([path], override=override)

    assert {name: os.environ.get(name) for name in ("A", "B", "C")} == expected
    assert expected == (
        {"A": "two", "B": "one", "C": "two"} if override else {"A": "shell", "B": "shell", "C": "shell"}
    )


def test_injection_matches_load_dotenv_on_non_ascii_values(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """UTF-8 is the file's encoding, not the machine's."""
    # ``load_dotenv`` decodes as UTF-8 whatever the locale is, and a dotenv file
    # is written on one machine and read on every machine that clones it. Read
    # in the ambient encoding instead, a Chinese comment turns the file into
    # mojibake — or into a ``UnicodeDecodeError`` that drops every key in it,
    # including the API keys that have nothing to do with the comment.
    path = tmp_path / ".env"
    path.write_text("# 中文注释\nGREETING=你好\nTOKEN=secret\n", encoding="utf-8")
    for name in ("GREETING", "TOKEN"):
        _claim_env_name(monkeypatch, name)

    load_dotenv(path, override=True)
    expected = {name: os.environ.get(name) for name in ("GREETING", "TOKEN")}

    monkeypatch.delenv("GREETING", raising=False)
    monkeypatch.delenv("TOKEN", raising=False)

    inject_bootstrap_dotenv([path], override=True)

    assert {name: os.environ.get(name) for name in ("GREETING", "TOKEN")} == expected
    assert expected == {"GREETING": "你好", "TOKEN": "secret"}


@pytest.mark.parametrize("encoding", ["utf-8-sig", "utf-16", "utf-32", "gb18030"])
def test_detected_dotenv_encoding_preserves_expansion_and_newlines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, encoding: str
) -> None:
    path = tmp_path / ".env"
    greeting = "你好。这是配置文件中的中文内容。需要正确读取并保留。"
    text = f'GREETING={greeting}\r\nCOPY=${{GREETING}}\r\nGREETING=last\r\nMULTILINE="one\r\ntwo"\r\n'
    path.write_bytes(text.encode(encoding))
    expected = {"GREETING": "last", "COPY": greeting, "MULTILINE": "one\ntwo"}
    for name in expected:
        _claim_env_name(monkeypatch, name)

    assert dict(read_dotenv_layer(path, base={})) == expected
    inject_bootstrap_dotenv([path], override=True)
    assert {name: os.environ.get(name) for name in expected} == expected


def test_damaged_dotenv_is_not_imported_or_migrated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / ".env"
    original = ("# 中文配置说明\n" * 20).encode() + b"BROKEN_TOKEN=abc\xffdef\nCHRYS_THEME=solar\n"
    path.write_bytes(original)
    _claim_env_name(monkeypatch, "BROKEN_TOKEN")

    assert read_dotenv_snapshot(path, base={}) is None
    assert dict(read_dotenv_layer(path, base={})) == {}
    inject_bootstrap_dotenv([path], override=True)

    assert "BROKEN_TOKEN" not in os.environ
    assert path.read_bytes() == original


def test_a_utf8_dotenv_parses_the_same_where_the_locale_is_not_utf8(tmp_path: Path) -> None:
    """The decode must not consult the machine's locale at all."""
    # In a child process with the C locale, so the ambient encoding really is
    # something else: this is the Windows ``GBK``/ANSI case, where the same
    # file either decodes to mojibake or raises — and a raise costs the caller
    # every key in the file, API tokens included, not just the accented one.
    path = tmp_path / ".env"
    path.write_text("# 中文注释\nGREETING=你好\nTOKEN=secret\n", encoding="utf-8")
    script = (
        "import json, sys;"
        "from pathlib import Path;"
        "from chrys.foundation.config.env_layers import read_dotenv_layer;"
        "sys.stdout.write(json.dumps(dict(read_dotenv_layer(Path(sys.argv[1]), base={}))))"
    )
    result = subprocess.run(
        [sys.executable, "-c", script, str(path)],
        capture_output=True,
        text=True,
        check=True,
        env=os.environ | {"PYTHONUTF8": "0", "PYTHONCOERCECLOCALE": "0", "LC_ALL": "C", "LANG": "C"},
    )

    # ``json`` escapes non-ASCII, so the pipe itself cannot be what carries the
    # characters through.
    assert json.loads(result.stdout) == {"GREETING": "你好", "TOKEN": "secret"}


# --- inject_bootstrap_dotenv ---


def test_injection_skips_every_chrys_key_class(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / ".env"
    path.write_text(
        "CHRYS_MODEL_PROFILE_APPROVAL_JUDGE=judge\nCHRYS_MODEL_PROFILE=pointer\nCHRYS_HOOK_ID=ipc\nPLAIN_KEY=kept\n",
        encoding="utf-8",
    )
    for name in ("CHRYS_MODEL_PROFILE_APPROVAL_JUDGE", "CHRYS_MODEL_PROFILE", "CHRYS_HOOK_ID", "PLAIN_KEY"):
        _claim_env_name(monkeypatch, name)

    inject_bootstrap_dotenv([path], override=True)

    assert os.environ["PLAIN_KEY"] == "kept"
    assert "CHRYS_MODEL_PROFILE_APPROVAL_JUDGE" not in os.environ
    assert "CHRYS_MODEL_PROFILE" not in os.environ
    assert "CHRYS_HOOK_ID" not in os.environ


def test_injection_still_resolves_same_file_references_to_skipped_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / ".env"
    path.write_text("CHRYS_MODEL_PROFILE=glm\nDERIVED=${CHRYS_MODEL_PROFILE}-suffix\n", encoding="utf-8")
    for name in ("CHRYS_MODEL_PROFILE", "DERIVED"):
        _claim_env_name(monkeypatch, name)

    inject_bootstrap_dotenv([path], override=True)

    assert os.environ["DERIVED"] == "glm-suffix"
    assert "CHRYS_MODEL_PROFILE" not in os.environ


def test_injection_without_override_keeps_existing_process_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / ".env"
    path.write_text("PRESENT=from-file\nABSENT=from-file\n", encoding="utf-8")
    _claim_env_name(monkeypatch, "ABSENT")
    monkeypatch.setenv("PRESENT", "from-shell")

    inject_bootstrap_dotenv([path], override=False)

    assert os.environ["PRESENT"] == "from-shell"
    assert os.environ["ABSENT"] == "from-file"


def test_later_files_win_between_files_when_overriding(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Mirrors the old back-to-back ``load_dotenv`` calls: config wins over project."""
    project = tmp_path / "project.env"
    project.write_text("SHARED=project\n", encoding="utf-8")
    config = tmp_path / "config.env"
    config.write_text("SHARED=config\n", encoding="utf-8")
    _claim_env_name(monkeypatch, "SHARED")

    inject_bootstrap_dotenv([project, config], override=True)
    assert os.environ["SHARED"] == "config"

    monkeypatch.delenv("SHARED")
    inject_bootstrap_dotenv([project, config], override=False)
    assert os.environ["SHARED"] == "project"


def test_injection_honors_the_dotenv_disable_switch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / ".env"
    path.write_text("DISABLED_PROBE=value\n", encoding="utf-8")
    _claim_env_name(monkeypatch, "DISABLED_PROBE")
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "TRUE")

    inject_bootstrap_dotenv([path], override=True)

    assert "DISABLED_PROBE" not in os.environ


def test_injection_skips_bare_keys_and_missing_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / ".env"
    path.write_text("BARE_PROBE\n", encoding="utf-8")
    _claim_env_name(monkeypatch, "BARE_PROBE")

    inject_bootstrap_dotenv([path, tmp_path / "absent.env"], override=True)

    assert "BARE_PROBE" not in os.environ


def test_read_dotenv_layer_honors_the_dotenv_disable_flag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Until the settings layers existed, these keys reached ``Settings`` only by
    # being loaded into the environment, which this flag switches off — so a
    # layer that read the file directly would quietly revoke the opt-out.
    path = tmp_path / ".env"
    path.write_text("CHRYS_THEME=solar\n", encoding="utf-8")

    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "1")
    assert read_dotenv_layer(path, base={}) == {}

    monkeypatch.delenv("PYTHON_DOTENV_DISABLED")
    assert read_dotenv_layer(path, base={}) == {"CHRYS_THEME": "solar"}


def test_a_dotenv_file_cannot_disable_itself_by_setting_the_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # ``load_dotenv`` decided the opt-out before opening the file it was given,
    # so a file carrying the flag still applied its own keys. Read live, the
    # settings layer would instead see the flag this very file just exported
    # and drop the setting sitting next to it.
    path = tmp_path / ".env"
    path.write_text("PYTHON_DOTENV_DISABLED=1\nCHRYS_THEME=solar\n", encoding="utf-8")
    _claim_env_name(monkeypatch, "PYTHON_DOTENV_DISABLED")
    freeze_process_env()

    inject_bootstrap_dotenv([path], override=True)

    assert os.environ["PYTHON_DOTENV_DISABLED"] == "1"
    assert read_dotenv_layer(path, base={})["CHRYS_THEME"] == "solar"


def test_a_project_dotenv_cannot_switch_off_the_users_own_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Back-to-back ``load_dotenv`` calls re-read the flag between files, so a
    # cloned repository could set it and the user's ``~/.chrys/.env`` was never
    # opened. Deciding once, from the shell, is what takes that away — and it
    # matters more now than it did then, because that file is a settings layer
    # and not just a bag of SDK variables.
    project = tmp_path / "project.env"
    project.write_text("PYTHON_DOTENV_DISABLED=1\n", encoding="utf-8")
    user = tmp_path / "user.env"
    user.write_text("USER_PROBE=kept\nCHRYS_THEME=solar\n", encoding="utf-8")
    _claim_env_name(monkeypatch, "PYTHON_DOTENV_DISABLED")
    _claim_env_name(monkeypatch, "USER_PROBE")
    freeze_process_env()

    inject_bootstrap_dotenv([project, user], override=True)

    assert os.environ["USER_PROBE"] == "kept"
    assert read_dotenv_layer(user, base={})["CHRYS_THEME"] == "solar"


def test_the_dotenv_opt_out_is_decided_once_per_process(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Otherwise anything that writes the flag mid-run — a hook, an SDK, a test
    # helper — silently removes the dotenv layers from the next reload, and the
    # settings panel reports values it can no longer explain.
    path = tmp_path / ".env"
    path.write_text("CHRYS_THEME=solar\n", encoding="utf-8")
    monkeypatch.delenv("PYTHON_DOTENV_DISABLED", raising=False)
    freeze_process_env()

    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "1")

    assert read_dotenv_layer(path, base={}) == {"CHRYS_THEME": "solar"}
