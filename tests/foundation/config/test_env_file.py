# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for atomic Chrys configuration dotenv updates."""

from __future__ import annotations

import multiprocessing
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import pytest
from dotenv import dotenv_values

from chrys.foundation.config.env_file import update_env_file
from chrys.foundation.platform.files import file_fingerprint


def _process_env_updates(path: str, prefix: str) -> None:
    with patch("chrys.foundation.config.env_file.config_env_path", return_value=Path(path)):
        for index in range(12):
            key = f"{prefix}_{index}"
            update_env_file({key: f"value-{index}"})


@pytest.fixture
def env_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "nested" / ".env"
    monkeypatch.setattr("chrys.foundation.config.env_file.config_env_path", lambda: path)
    return path


def test_update_env_file_creates_missing_file_and_stable_sidecar(env_path: Path) -> None:
    update_env_file({"NEW_KEY": "value"})

    assert env_path.read_text(encoding="utf-8") == 'NEW_KEY="value"\n'
    assert env_path.with_name(".env.lock").is_file()


def test_update_env_file_preserves_unrelated_lines_and_updates_in_place(env_path: Path) -> None:
    original = "# keep exactly\r\n\r\nUNTOUCHED='x y'\r\nnot parseable text\r\nTARGET=old\r\n"
    env_path.parent.mkdir(parents=True)
    env_path.write_bytes(original.encode())

    update_env_file({"TARGET": "new", "APPENDED": "last"})

    assert env_path.read_bytes().decode() == (
        '# keep exactly\r\n\r\nUNTOUCHED=\'x y\'\r\nnot parseable text\r\nTARGET="new"\r\nAPPENDED="last"\n'
    )


@pytest.mark.parametrize("encoding", ["utf-8-sig", "utf-16", "utf-32", "gb18030"])
def test_update_detects_encoding_and_preserves_unrelated_text(env_path: Path, encoding: str) -> None:
    env_path.parent.mkdir(parents=True)
    prefix = "# 保留用户配置中的中文注释和其他参数。更新时不应损坏。\r\nGREETING=你好\r\n"
    env_path.write_bytes((prefix + "TARGET=old\r\n").encode(encoding))

    assert update_env_file({"TARGET": "new"}, expect_fingerprint=file_fingerprint(env_path))

    assert env_path.read_bytes().decode("utf-8") == prefix + 'TARGET="new"\r\n'


def test_update_refuses_lossy_decoding_without_changing_the_file(env_path: Path) -> None:
    env_path.parent.mkdir(parents=True)
    original = ("# 中文配置说明\n" * 20).encode() + b"SECRET=abc\xffdef\nTARGET=old\n"
    env_path.write_bytes(original)

    with pytest.raises(UnicodeError):
        update_env_file({"TARGET": "new"})

    assert env_path.read_bytes() == original


def test_update_env_file_always_quotes_and_escapes_backslashes_and_quotes(env_path: Path) -> None:
    value = 'path\\to\\"quoted"'

    update_env_file({"ESCAPED": value})

    assert env_path.read_text(encoding="utf-8") == 'ESCAPED="path\\\\to\\\\\\"quoted\\""\n'
    assert dotenv_values(env_path)["ESCAPED"] == value


def test_update_env_file_distinguishes_empty_value_from_removal(env_path: Path) -> None:
    env_path.parent.mkdir(parents=True)
    env_path.write_text("EMPTY=old\nREMOVE=old\nKEEP=old\n", encoding="utf-8")

    update_env_file({"EMPTY": ""}, remove_keys=("REMOVE",))

    assert env_path.read_text(encoding="utf-8") == 'EMPTY=""\nKEEP=old\n'
    assert dotenv_values(env_path)["EMPTY"] == ""
    assert "REMOVE" not in dotenv_values(env_path)


def test_update_env_file_removal_folds_case_the_way_the_os_reads(
    env_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On Windows a lowercase spelling answers for the key and must go with it."""
    import dataclasses

    import chrys.foundation.platform as platform_mod

    env_path.parent.mkdir(parents=True)
    for os_name, expected in (("windows", "KEEP=old\n"), ("linux", "chrys_probe=old\nKEEP=old\n")):
        fake = dataclasses.replace(platform_mod.get_platform(), os_name=os_name)
        monkeypatch.setattr(platform_mod, "get_platform", lambda fake=fake: fake)
        env_path.write_text("chrys_probe=old\nKEEP=old\n", encoding="utf-8")

        update_env_file({}, remove_keys=("CHRYS_PROBE",))

        assert env_path.read_text(encoding="utf-8") == expected, os_name


@pytest.mark.parametrize(
    ("updates", "remove_keys"),
    [
        ({"BAD\nKEY": "value"}, ()),
        ({"KEY": "bad\rvalue"}, ()),
        ({}, ("BAD\nKEY",)),
    ],
)
def test_update_env_file_rejects_newlines(
    env_path: Path,
    updates: dict[str, str],
    remove_keys: tuple[str, ...],
) -> None:
    with pytest.raises(ValueError, match="newline"):
        update_env_file(updates, remove_keys=remove_keys)

    assert not env_path.exists()


def test_update_env_file_serializes_threaded_writers_without_lost_keys(env_path: Path) -> None:
    def write(index: int) -> None:
        update_env_file({f"THREAD_{index}": f"value-{index}"})

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(write, range(24)))

    values = dotenv_values(env_path)
    assert {f"THREAD_{index}": f"value-{index}" for index in range(24)}.items() <= values.items()


def test_update_env_file_serializes_process_writers_without_lost_keys(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    context = multiprocessing.get_context("spawn")
    processes = [
        context.Process(target=_process_env_updates, args=(str(path), f"PROCESS_{index}")) for index in range(3)
    ]

    for process in processes:
        process.start()
    # One shared wall-clock budget rather than a fixed per-process join: a
    # spawned interpreter on a loaded Windows CI worker can spend most of a
    # short budget just importing, and serial joins would compound that.
    # Stay under the pytest per-test timeout so a genuine hang still reports.
    deadline = time.monotonic() + 45
    for process in processes:
        process.join(timeout=max(0.0, deadline - time.monotonic()))
    stragglers = [process for process in processes if process.is_alive()]
    for process in stragglers:
        process.terminate()
        process.join(timeout=5)

    exit_codes = [process.exitcode for process in processes]
    assert not stragglers, f"writer processes still running at deadline: exit codes {exit_codes}"
    assert exit_codes == [0, 0, 0], f"writer processes failed: exit codes {exit_codes}"
    values = dotenv_values(path)
    expected = {
        f"PROCESS_{process_index}_{value_index}": f"value-{value_index}"
        for process_index in range(3)
        for value_index in range(12)
    }
    assert expected.items() <= values.items()


def test_a_removal_refuses_when_the_file_no_longer_digests_to_what_was_read(env_path: Path) -> None:
    """The gate exists for removals: they delete lines chosen from an earlier read."""
    # Between that read and this call the file may have grown — or changed — a
    # key the read never saw, and removing it would destroy the only record of
    # a setting nothing imported. Refusing costs one idempotent retry.
    env_path.parent.mkdir(parents=True, exist_ok=True)
    env_path.write_text("CHRYS_THEME=solar\n", encoding="utf-8")
    stale = file_fingerprint(env_path)
    env_path.write_text("CHRYS_THEME=midnight\n", encoding="utf-8")

    assert update_env_file({}, remove_keys=("CHRYS_THEME",), expect_fingerprint=stale) is False
    assert env_path.read_text(encoding="utf-8") == "CHRYS_THEME=midnight\n"


def test_a_removal_proceeds_when_the_file_still_digests_to_what_was_read(env_path: Path) -> None:
    env_path.parent.mkdir(parents=True, exist_ok=True)
    env_path.write_text("MY_TOKEN=secret\nCHRYS_THEME=solar\n", encoding="utf-8")

    assert update_env_file(
        {},
        remove_keys=("CHRYS_THEME",),
        expect_fingerprint=file_fingerprint(env_path),
    )
    assert env_path.read_text(encoding="utf-8") == "MY_TOKEN=secret\n"


def test_the_gate_is_evaluated_against_the_bytes_the_rewrite_uses(env_path: Path) -> None:
    """One read answers both questions, so no write can land between them."""
    # Digesting the path and then opening it is two reads, and any writer that
    # does not take our lock — a text editor — can land in between: the check
    # passes on the old bytes and the rewrite then strips keys out of the new
    # ones, which is a removal decided from a state that no longer exists.
    env_path.parent.mkdir(parents=True, exist_ok=True)
    # Byte-exact fixture: text mode would translate the newline on Windows,
    # and the assertion below is about the very bytes the rewrite read.
    env_path.write_bytes(b"CHRYS_THEME=solar\n")
    fingerprint = file_fingerprint(env_path)
    seen: list[bytes] = []
    real_read_bytes = Path.read_bytes

    def record(self: Path) -> bytes:
        data = real_read_bytes(self)
        if self == env_path:
            seen.append(data)
        return data

    with patch.object(Path, "read_bytes", record):
        assert update_env_file({"CHRYS_LOCALE": "zh-Hans"}, expect_fingerprint=fingerprint)

    assert seen == [b"CHRYS_THEME=solar\n"]
