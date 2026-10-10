# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Persistent themes: protected names, fidelity, atomic writes and conflicts."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from textual.theme import BUILTIN_THEMES, Theme

from chrys.app.tui.theme import CHRYS_ANSI_THEME, CHRYS_LEGACY_THEME, CHRYS_THEME, CHRYS_THEMES
from chrys.app.tui.theme_loader import load_user_themes
from chrys.app.tui.themes.document import copy_theme
from chrys.app.tui.themes.store import ThemeFileRevision, ThemeStoreError, UserThemeStore, theme_data
from chrys.foundation.config.settings import RENAMED_THEMES


@pytest.mark.parametrize("name", sorted(set(BUILTIN_THEMES) | CHRYS_THEMES | set(RENAMED_THEMES)))
def test_every_builtin_is_protected_at_the_write_boundary(tmp_path: Path, name: str) -> None:
    store = UserThemeStore(tmp_path)
    for candidate in (name, name.upper()):
        with pytest.raises(ThemeStoreError, match="read_only"):
            store.save(copy_theme(CHRYS_LEGACY_THEME, name=candidate), None)
        with pytest.raises(ThemeStoreError, match="read_only"):
            store.delete(candidate, ThemeFileRevision(tmp_path / f"{candidate}.yaml", ""))
    assert not list(tmp_path.glob("*.yaml"))


@pytest.mark.parametrize("name", ["", "../x", "a/b", "a\\b", "CON", "nul.txt", "COM1", "a.", "x.YAML", "x" * 101])
def test_names_are_portable_and_cannot_escape_the_theme_directory(tmp_path: Path, name: str) -> None:
    with pytest.raises(ThemeStoreError, match="invalid_name"):
        UserThemeStore(tmp_path).save(copy_theme(CHRYS_LEGACY_THEME, name=name), None)


@pytest.mark.parametrize("source", [CHRYS_THEME, CHRYS_LEGACY_THEME, CHRYS_ANSI_THEME, BUILTIN_THEMES["ansi-dark"]])
def test_saved_data_round_trips_through_startup_loader(tmp_path: Path, source: Theme) -> None:
    store = UserThemeStore(tmp_path)
    theme = copy_theme(source, name=f"{source.name}-custom")
    theme.variables["foreground-muted"] = "ansi_white 40%"
    before = copy_theme(source)
    revision = store.save(theme, None)
    loaded, warnings = load_user_themes(tmp_path)
    assert warnings == []
    assert loaded == [theme]
    assert store.load(theme.name) == (theme, revision)
    assert source == before
    theme.success = "rgba(12, 34, 56, 0.123456789)"
    updated = store.save(theme, revision)
    assert updated.digest != revision.digest
    assert store.load(theme.name) == (theme, updated)


@pytest.mark.parametrize("encoding", ["gb18030", "utf-32"])
def test_a_legacy_encoded_theme_the_startup_loader_lists_opens_and_saves(tmp_path: Path, encoding: str) -> None:
    text = '# 这是用户自定义主题。保留中文注释并正确加载颜色设置。\nprimary: "#875FAF"\n'
    (tmp_path / "encoded.yaml").write_bytes(text.encode(encoding))
    listed, warnings = load_user_themes(tmp_path)
    store = UserThemeStore(tmp_path)

    theme, revision = store.load("encoded")

    assert warnings == []
    assert listed == [theme]
    theme.primary = "#123456"
    saved = store.save(theme, revision)
    assert store.load("encoded") == (theme, saved)
    assert "#123456" in saved.path.read_text(encoding="utf-8")


def test_yaml_preserves_omitted_fields_and_string_keys(tmp_path: Path) -> None:
    theme = Theme("custom", "#123456", variables={"on": "no", "yes": "false"})
    assert "background" not in theme_data(theme)
    store = UserThemeStore(tmp_path)
    store.save(theme, None)
    assert store.load(theme.name)[0] == theme


def test_existing_yaml_yml_and_case_variants_are_never_overwritten_as_new(tmp_path: Path) -> None:
    path = tmp_path / "Custom.yml"
    payload = "primary: red\n"
    path.write_text(payload)
    store = UserThemeStore(tmp_path)
    for name in ("Custom", "custom", "CUSTOM"):
        with pytest.raises(ThemeStoreError, match="exists"):
            store.save(copy_theme(CHRYS_LEGACY_THEME, name=name), None)
    assert path.read_text() == payload
    theme, revision = store.load("Custom")
    theme.primary = "blue"
    assert store.save(theme, revision).path == path
    assert not (tmp_path / "Custom.yaml").exists()


def test_external_changes_and_deletion_preserve_the_draft_and_disk(tmp_path: Path) -> None:
    store = UserThemeStore(tmp_path)
    draft = copy_theme(CHRYS_LEGACY_THEME, name="custom")
    revision = store.save(draft, None)
    external = "primary: red\n"
    revision.path.write_text(external)
    draft.primary = "blue"
    with pytest.raises(ThemeStoreError, match="conflict"):
        store.save(draft, revision)
    assert revision.path.read_text() == external
    assert draft.primary == "blue"
    revision.path.unlink()
    with pytest.raises(ThemeStoreError, match="conflict"):
        store.save(draft, revision)


def test_write_failure_keeps_existing_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = UserThemeStore(tmp_path)
    draft = copy_theme(CHRYS_LEGACY_THEME, name="custom")
    revision = store.save(draft, None)
    before = revision.path.read_bytes()

    def fail_write(*_args: object, **_kwargs: object) -> bytes:
        raise OSError("disk full")

    monkeypatch.setattr("chrys.app.tui.themes.store.atomic_write_text", fail_write)
    draft.primary = "red"
    with pytest.raises(ThemeStoreError, match="io_error"):
        store.save(draft, revision)
    assert revision.path.read_bytes() == before


def test_unreadable_directory_reports_an_error_before_saving(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = UserThemeStore(tmp_path)

    def unreadable(_path: Path):
        raise PermissionError("permission denied")

    monkeypatch.setattr(Path, "iterdir", unreadable)
    # The name field and New action validate before entering the save worker.
    with pytest.raises(ThemeStoreError, match="io_error"):
        store.validate_name("custom")
    with pytest.raises(ThemeStoreError, match="io_error"):
        store.suggest_name("chrys")


def test_two_saves_from_the_same_revision_do_not_lose_an_update(tmp_path: Path) -> None:
    store = UserThemeStore(tmp_path)
    theme = copy_theme(CHRYS_LEGACY_THEME, name="custom")
    revision = store.save(theme, None)

    def write(color: str) -> str:
        draft = copy_theme(theme)
        draft.primary = color
        try:
            store.save(draft, revision)
        except ThemeStoreError:
            return "conflict"
        return color

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(write, ["red", "blue"]))
    assert outcomes.count("conflict") == 1
    assert store.load("custom")[0].primary in outcomes


@pytest.mark.parametrize("suffix", [".yaml", ".yml"])
def test_delete_removes_only_the_opened_revision(tmp_path: Path, suffix: str) -> None:
    path = tmp_path / f"custom{suffix}"
    path.write_text("primary: red\n")
    unrelated = tmp_path / "other.yaml"
    unrelated.write_text("primary: blue\n")
    store = UserThemeStore(tmp_path)
    _, revision = store.load("custom")
    store.delete("custom", revision)
    assert not path.exists()
    assert unrelated.read_text() == "primary: blue\n"
    assert [theme.name for theme in load_user_themes(tmp_path)[0]] == ["other"]


@pytest.mark.parametrize("change", ["modified", "missing", "duplicate", "symlink", "outside"])
def test_delete_rejects_stale_or_replaced_files(tmp_path: Path, change: str) -> None:
    directory = tmp_path / "themes"
    store = UserThemeStore(directory)
    revision = store.save(copy_theme(CHRYS_LEGACY_THEME, name="custom"), None)
    outside = tmp_path / "custom.yaml"
    outside.write_bytes(revision.path.read_bytes())
    if change == "modified":
        revision.path.write_text("primary: blue\n")
    elif change == "missing":
        revision.path.unlink()
    elif change == "duplicate":
        (directory / "custom.yml").write_text("primary: blue\n")
    elif change == "symlink":
        revision.path.unlink()
        try:
            revision.path.symlink_to(outside)
        except OSError:
            pytest.skip("Symlinks unavailable")
    else:
        revision = ThemeFileRevision(outside, revision.digest)
    with pytest.raises(ThemeStoreError, match="delete_conflict"):
        store.delete("custom", revision)
    assert outside.exists()
    if change != "missing":
        assert revision.path.exists()


def test_delete_io_failure_preserves_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = UserThemeStore(tmp_path)
    revision = store.save(copy_theme(CHRYS_LEGACY_THEME, name="custom"), None)
    payload = revision.path.read_bytes()

    def fail(_path: Path, **_kwargs: object) -> None:
        raise PermissionError("read-only directory")

    monkeypatch.setattr(Path, "unlink", fail)
    with pytest.raises(ThemeStoreError, match="delete_error"):
        store.delete("custom", revision)
    assert revision.path.read_bytes() == payload
