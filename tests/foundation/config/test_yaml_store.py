# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Contract tests for the locked YAML document store."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

from chrys.foundation.config import yaml_store
from chrys.foundation.config.yaml_store import (
    UNTRUSTED_DOC_MAX_BYTES,
    backup_path_for,
    lock_path_for,
    read_yaml_doc,
    read_yaml_doc_readonly,
    update_yaml_doc,
)


def test_lock_path_is_derived_from_the_target_name(tmp_path: Path) -> None:
    assert lock_path_for(tmp_path / "settings.yaml").name == "settings.lock"
    assert lock_path_for(tmp_path / "notifications.yaml").name == "notifications.lock"
    assert backup_path_for(tmp_path / "settings.yaml").name == "settings.yaml.bak"


def test_read_returns_none_without_creating_anything(tmp_path: Path) -> None:
    path = tmp_path / "absent" / "settings.yaml"

    assert read_yaml_doc(path) is None
    assert not path.parent.exists()


@pytest.mark.parametrize("encoding", ["utf-8-sig", "utf-16", "utf-32", "gb18030"])
def test_detected_encoding_preserves_primary_and_unrelated_values(tmp_path: Path, encoding: str) -> None:
    path = tmp_path / "settings.yaml"
    description = "保留用户配置中的中文内容。不应被旧的备份文件覆盖。"
    original = f"description: {description}\nui:\n  theme: chrys\n".encode(encoding)
    path.write_bytes(original)
    backup_path_for(path).write_text("description: stale backup\n", encoding="utf-8")
    expected = {"description": description, "ui": {"theme": "chrys"}}

    assert read_yaml_doc_readonly(path) == expected
    assert read_yaml_doc(path) == expected
    assert path.read_bytes() == original
    committed = update_yaml_doc(path, lambda doc: {**doc, "extra": True})
    assert committed == {**expected, "extra": True}
    assert yaml.safe_load(path.read_text(encoding="utf-8")) == committed


def test_update_merges_disjoint_keys_and_last_writer_wins_on_the_same_key(tmp_path: Path) -> None:
    path = tmp_path / "settings.yaml"

    update_yaml_doc(path, lambda doc: {**doc, "ui": {"theme": "chrys"}, "shared": 1})
    committed = update_yaml_doc(path, lambda doc: {**doc, "approval": {"default_mode": "manual"}, "shared": 2})

    assert committed == {"ui": {"theme": "chrys"}, "approval": {"default_mode": "manual"}, "shared": 2}
    assert yaml.safe_load(path.read_text(encoding="utf-8")) == committed


def test_mutator_sees_the_document_written_by_the_previous_call(tmp_path: Path) -> None:
    path = tmp_path / "settings.yaml"
    update_yaml_doc(path, lambda _doc: {"count": 1})

    seen: list[dict[str, Any]] = []

    def bump(doc: dict[str, Any]) -> dict[str, Any]:
        seen.append(dict(doc))
        return {**doc, "count": doc["count"] + 1}

    update_yaml_doc(path, bump)

    assert seen == [{"count": 1}]
    assert yaml.safe_load(path.read_text(encoding="utf-8")) == {"count": 2}


def test_mutator_exception_leaves_both_copies_untouched(tmp_path: Path) -> None:
    path = tmp_path / "settings.yaml"
    update_yaml_doc(path, lambda _doc: {"ui": {"theme": "chrys"}})
    before = path.read_text(encoding="utf-8")
    backup_before = backup_path_for(path).read_text(encoding="utf-8")

    def explode(_doc: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("validation failed")

    with pytest.raises(RuntimeError):
        update_yaml_doc(path, explode)

    assert path.read_text(encoding="utf-8") == before
    assert backup_path_for(path).read_text(encoding="utf-8") == backup_before


def test_primary_is_committed_before_the_backup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "settings.yaml"
    order: list[str] = []
    real_write = yaml_store.atomic_write_text

    def tracking_write(target: Path, payload: str, **kwargs: Any) -> None:
        order.append("backup" if target.name.endswith(".bak") else "primary")
        real_write(target, payload, **kwargs)

    monkeypatch.setattr(yaml_store, "atomic_write_text", tracking_write)
    update_yaml_doc(path, lambda _doc: {"ui": {"theme": "chrys"}})

    assert order == ["primary", "backup"]
    assert path.read_text(encoding="utf-8") == backup_path_for(path).read_text(encoding="utf-8")


def test_a_failed_backup_write_does_not_fail_the_commit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "settings.yaml"
    real_write = yaml_store.atomic_write_text

    def refuse_backup(target: Path, payload: str, **kwargs: Any) -> None:
        if target.name.endswith(".bak"):
            raise OSError("no space left on device")
        real_write(target, payload, **kwargs)

    monkeypatch.setattr(yaml_store, "atomic_write_text", refuse_backup)
    committed = update_yaml_doc(path, lambda _doc: {"ui": {"theme": "chrys"}})

    assert committed == {"ui": {"theme": "chrys"}}
    assert yaml.safe_load(path.read_text(encoding="utf-8")) == committed
    assert not backup_path_for(path).exists()


def test_read_falls_back_to_the_backup_and_repairs_the_primary(tmp_path: Path) -> None:
    path = tmp_path / "settings.yaml"
    update_yaml_doc(path, lambda _doc: {"ui": {"theme": "chrys"}})
    path.write_text("ui: [not valid yaml", encoding="utf-8")

    assert read_yaml_doc(path) == {"ui": {"theme": "chrys"}}
    assert yaml.safe_load(path.read_text(encoding="utf-8")) == {"ui": {"theme": "chrys"}}


def test_read_without_backup_does_not_fall_back(tmp_path: Path) -> None:
    path = tmp_path / "settings.yaml"
    update_yaml_doc(path, lambda _doc: {"ui": {"theme": "chrys"}})
    path.write_text("ui: [not valid yaml", encoding="utf-8")

    assert read_yaml_doc(path, backup=False) is None


# Only the first of these fails inside PyYAML's *parser*. The rest escape as
# ``ValueError`` / ``RecursionError`` / ``UnicodeDecodeError`` or fail the YAML character rules, and
# every one is reachable by hand-editing the file. Legacy encodings alone
# are valid input, so corruption must remain invalid after decoding.
_POISONED_PRIMARIES = [
    pytest.param(b"ui: [not valid yaml\n", id="parse-error"),
    pytest.param(b"ui: 2026-99-99\n", id="impossible-date"),
    pytest.param(b"ui: " + b"9" * (sys.get_int_max_str_digits() + 10) + b"\n", id="oversized-int"),
    pytest.param(b"ui: " + b"[" * 5_000 + b"]" * 5_000 + b"\n", id="deeply-nested"),
    pytest.param(b"ui: chrys\n# invalid control character: \x00\n", id="control-character"),
    pytest.param(("# 中文配置说明\n" * 20).encode() + b"ui: solar\xff\n", id="damaged-utf8"),
    pytest.param("ui: chrys\n".encode("utf-16")[:-1], id="truncated-utf16"),
]


@pytest.mark.parametrize("poison", _POISONED_PRIMARIES)
def test_every_way_of_corrupting_the_primary_still_reaches_the_backup(tmp_path: Path, poison: bytes) -> None:
    """A narrow ``except yaml.YAMLError`` would let most of these past.

    They would then propagate out of the read and abort startup — on exactly
    the file the ``.bak`` copy exists to recover from.
    """
    path = tmp_path / "settings.yaml"
    update_yaml_doc(path, lambda _doc: {"ui": {"theme": "chrys"}})

    path.write_bytes(poison)
    assert read_yaml_doc(path) == {"ui": {"theme": "chrys"}}

    path.write_bytes(poison)
    assert update_yaml_doc(path, lambda doc: {**doc, "approval": {"default_mode": "manual"}}) == {
        "ui": {"theme": "chrys"},
        "approval": {"default_mode": "manual"},
    }


def test_update_keeps_the_backup_content_when_the_primary_is_corrupt(tmp_path: Path) -> None:
    path = tmp_path / "settings.yaml"
    update_yaml_doc(path, lambda _doc: {"ui": {"theme": "chrys"}})
    path.write_text("ui: [not valid yaml", encoding="utf-8")

    committed = update_yaml_doc(path, lambda doc: {**doc, "approval": {"default_mode": "manual"}})

    assert committed == {"ui": {"theme": "chrys"}, "approval": {"default_mode": "manual"}}


def test_a_non_mapping_document_reads_as_absent(tmp_path: Path) -> None:
    path = tmp_path / "settings.yaml"
    path.write_text("- just\n- a list\n", encoding="utf-8")

    assert read_yaml_doc(path) is None
    assert update_yaml_doc(path, lambda doc: {**doc, "ui": {"theme": "chrys"}}) == {"ui": {"theme": "chrys"}}


@pytest.mark.parametrize("falsey", ["false\n", "0\n", "[]\n", "''\n"])
def test_a_falsey_non_mapping_does_not_pass_for_an_empty_document(tmp_path: Path, falsey: str) -> None:
    """``safe_load(raw) or {}`` counted these as a valid empty config.

    They are what a truncation or a bad editor macro leaves behind, and reading
    them as ``{}`` skips the backup and then mirrors the emptiness over it on
    the next write — losing the only intact copy.
    """
    path = tmp_path / "settings.yaml"
    update_yaml_doc(path, lambda _doc: {"ui": {"theme": "chrys"}})

    path.write_text(falsey, encoding="utf-8")
    assert read_yaml_doc(path) == {"ui": {"theme": "chrys"}}

    path.write_text(falsey, encoding="utf-8")
    assert update_yaml_doc(path, lambda doc: doc)["ui"] == {"theme": "chrys"}


@pytest.mark.parametrize("empty", ["", "\n", "# only a comment\n", "null\n", "~\n"])
def test_a_genuinely_empty_document_still_reads_as_an_empty_mapping(tmp_path: Path, empty: str) -> None:
    """The other half of the same decision: ``None`` is the only falsey YAML
    that means "this file holds nothing", and it must not reach the backup."""
    path = tmp_path / "settings.yaml"
    path.write_text(empty, encoding="utf-8")

    assert read_yaml_doc(path) == {}


def test_a_read_still_works_when_the_lock_cannot_be_created(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A read-only config directory must not make the document unreadable."""
    # The read takes the lock as a courtesy to Windows writers, and taking it
    # means creating a sidecar. On a locked-down machine — or a directory owned
    # by someone else — that fails, and failing the read with it would abort
    # every entry point over a file we can read perfectly well.
    path = tmp_path / "settings.yaml"
    path.write_text(yaml.safe_dump({"ui": {"theme": "solar"}}), encoding="utf-8")

    def refuse(_self: object) -> None:
        raise OSError(13, "Permission denied")

    monkeypatch.setattr(yaml_store.FileLock, "acquire", refuse)

    assert read_yaml_doc(path) == {"ui": {"theme": "solar"}}


def test_a_read_still_refuses_a_lock_another_process_is_holding(tmp_path: Path) -> None:
    """The other half: unavailable is not the same as held."""
    # ``TimeoutError`` is an ``OSError``, so without the distinction the
    # read-only fallback above would answer "somebody is writing this right
    # now" by reading behind them — the one thing the lock exists to stop.
    path = tmp_path / "settings.yaml"
    path.write_text(yaml.safe_dump({"ui": {"theme": "solar"}}), encoding="utf-8")
    held = yaml_store.FileLock(lock_path_for(path), timeout=0)
    held.acquire()
    try:
        with pytest.raises(TimeoutError):
            read_yaml_doc(path, lock_timeout=0)
    finally:
        held.release()


def test_lock_timeout_is_forwarded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    timeouts: list[float] = []

    class FakeLock:
        def __init__(self, _path: object, *, timeout: float) -> None:
            timeouts.append(timeout)

        def __enter__(self) -> FakeLock:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    monkeypatch.setattr(yaml_store, "FileLock", FakeLock)
    update_yaml_doc(tmp_path / "settings.yaml", lambda _doc: {"a": 1}, lock_timeout=0.25)

    assert timeouts == [0.25]


# ──────────────────── the untrusted read-only reader ────────────────────────
#
# ``read_yaml_doc_readonly`` reads files Chrys does not own — a repository's
# ``.chrys/settings.yaml`` is attacker-supplied the moment a repo is cloned —
# so unlike the owned readers it must refuse anything that is not a plain,
# bounded, regular file: a FIFO blocks the open, a symlink reads whatever it
# points at, and an unbounded read hands ``/dev/zero`` to the parser.


def test_readonly_read_parses_a_regular_document_and_refuses_an_absent_one(tmp_path: Path) -> None:
    path = tmp_path / "settings.yaml"
    path.write_text("session:\n  title:\n    auto: false\n", encoding="utf-8")

    assert read_yaml_doc_readonly(path) == {"session": {"title": {"auto": False}}}
    assert read_yaml_doc_readonly(tmp_path / "absent.yaml") is None


def test_readonly_read_refuses_a_symlinked_document(tmp_path: Path) -> None:
    target = tmp_path / "target.yaml"
    target.write_text("a: 1\n", encoding="utf-8")
    link = tmp_path / "settings.yaml"
    try:
        link.symlink_to(target)
    except OSError, NotImplementedError:  # pragma: no cover - Windows without symlink rights
        pytest.skip("platform cannot create symlinks")

    assert read_yaml_doc_readonly(link) is None
    assert read_yaml_doc_readonly(target) == {"a": 1}


def test_readonly_read_refuses_a_fifo_without_blocking(tmp_path: Path) -> None:
    if not hasattr(os, "mkfifo"):  # pragma: no cover - POSIX-only shape
        pytest.skip("platform has no FIFOs")
    path = tmp_path / "settings.yaml"
    os.mkfifo(path)

    # The assertion is the return itself: a plain ``open`` of a FIFO with no
    # writer never returns, so reaching ``None`` proves the no-block flags.
    assert read_yaml_doc_readonly(path) is None


def test_readonly_read_bounds_the_document_size(tmp_path: Path) -> None:
    path = tmp_path / "settings.yaml"
    payload = b"a: 1\n"
    at_bound = payload + b"#" * (UNTRUSTED_DOC_MAX_BYTES - len(payload))
    path.write_bytes(at_bound)
    assert read_yaml_doc_readonly(path) == {"a": 1}

    path.write_bytes(at_bound + b"#")
    assert read_yaml_doc_readonly(path) is None


def test_readonly_read_refuses_a_symlinked_containing_directory(tmp_path: Path) -> None:
    """O_NOFOLLOW covers only the final component; the parent hop is checked
    separately, so a committed ``.chrys`` symlink cannot carry the read out."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "settings.yaml").write_text("a: 1\n", encoding="utf-8")
    project = tmp_path / "project"
    project.mkdir()
    try:
        (project / ".chrys").symlink_to(outside, target_is_directory=True)
    except OSError, NotImplementedError:  # pragma: no cover - Windows without symlink rights
        pytest.skip("platform cannot create symlinks")

    assert read_yaml_doc_readonly(project / ".chrys" / "settings.yaml") is None
    assert read_yaml_doc_readonly(outside / "settings.yaml") == {"a": 1}


def test_readonly_read_refuses_an_alias_cycle(tmp_path: Path) -> None:
    """A mapping cycle spelled with anchors would keep the flattener's walk
    running forever; the document is refused whole, like any unparseable one."""
    path = tmp_path / "settings.yaml"
    path.write_text("z: &z\n  q: *z\n", encoding="utf-8")

    assert read_yaml_doc_readonly(path) is None
    # The user-document reader shares the parse, so a hand-edited cycle there
    # degrades to "no document" instead of overflowing the flattener.
    assert read_yaml_doc(path) is None


def test_readonly_read_bounds_the_expanded_mapping_tree(tmp_path: Path) -> None:
    """Shared aliases multiply: a few hundred bytes can expand to millions of
    walkable mappings. The expansion is bounded, not the byte count."""
    lines = ["a0: &a0 {x: 1}"]
    for index in range(1, 25):
        lines.append(f"a{index}: &a{index} {{l: *a{index - 1}, r: *a{index - 1}}}")
    path = tmp_path / "settings.yaml"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    assert read_yaml_doc_readonly(path) is None

    modest = tmp_path / "modest.yaml"
    modest.write_text("shared: &s {x: 1}\nleft: *s\nright: *s\n", encoding="utf-8")
    assert read_yaml_doc_readonly(modest) == {
        "shared": {"x": 1},
        "left": {"x": 1},
        "right": {"x": 1},
    }


def test_readonly_read_bounds_the_expanded_mapping_depth(tmp_path: Path) -> None:
    """An alias chain expands deeper than the text ever nested, past what the
    parser's own recursion guard can see."""
    lines = ["a0: &a0 {x: 1}"]
    for index in range(1, 130):
        lines.append(f"a{index}: &a{index} {{n: *a{index - 1}}}")
    path = tmp_path / "settings.yaml"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    assert read_yaml_doc_readonly(path) is None


def test_readonly_read_bounds_sequence_aliases_too(tmp_path: Path) -> None:
    """Sequences are leaves to the flattener but not to ``str()``: an invalid
    raw value is rendered verbatim for its warning, and that walk expands every
    shared branch. The bound covers every container kind, not just mappings."""
    lines = ["a0: &a0 [1]"]
    for index in range(1, 25):
        lines.append(f"a{index}: &a{index} [*a{index - 1}, *a{index - 1}]")
    path = tmp_path / "settings.yaml"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    assert read_yaml_doc_readonly(path) is None

    cyclic = tmp_path / "cyclic.yaml"
    cyclic.write_text("a: &a [*a]\n", encoding="utf-8")
    assert read_yaml_doc_readonly(cyclic) is None

    # ``!!pairs`` builds tuples between the lists; the walk must pass through
    # them or a doubling chain hides one hop below every sequence.
    lines = ["p0: &p0 !!pairs [x: 1]"]
    for index in range(1, 25):
        lines.append(f"p{index}: &p{index} !!pairs [l: *p{index - 1}, r: *p{index - 1}]")
    paired = tmp_path / "paired.yaml"
    paired.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert read_yaml_doc_readonly(paired) is None

    modest = tmp_path / "modest.yaml"
    modest.write_text("shared: &s [1, 2]\nleft: *s\nright: *s\n", encoding="utf-8")
    assert read_yaml_doc_readonly(modest) == {
        "shared": [1, 2],
        "left": [1, 2],
        "right": [1, 2],
    }


def test_readonly_read_bounds_the_expanded_scalar_volume(tmp_path: Path) -> None:
    """Counting containers bounds the walk, not the output: one anchored
    kilobyte string aliased through a 14-level list DAG stays well under the
    container ceiling while rendering to ~16 million characters. The scalar
    volume of the expansion is budgeted too."""
    big = "x" * 1024
    lines = [f'a0: &a0 ["{big}"]']
    for index in range(1, 15):
        lines.append(f"a{index}: &a{index} [*a{index - 1}, *a{index - 1}]")
    path = tmp_path / "settings.yaml"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    assert read_yaml_doc_readonly(path) is None

    # Sheer size without alias multiplication is not the attack: a document
    # carrying one large string once still loads.
    plain = tmp_path / "plain.yaml"
    plain.write_text('blob: "' + "y" * 100_000 + '"\n', encoding="utf-8")
    assert read_yaml_doc_readonly(plain) == {"blob": "y" * 100_000}
