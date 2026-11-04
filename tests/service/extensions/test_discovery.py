# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Finding the installed extensions, which runs none of their code."""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

from chrys.service import extensions
from tests.support.extensions import EXAMPLE_EXTENSION_ID, install_distribution_extension, install_file_extension

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

_EXPLODING = '"""Would break iCode if it ran."""\nraise SystemExit("ran at discovery")\n'


def test_file_extensions_are_found_by_name_with_their_docstring_summary(monkeypatch: pytest.MonkeyPatch) -> None:
    module = install_file_extension(monkeypatch)
    package = install_file_extension(monkeypatch, "acme_folder", package=True)
    catalog = extensions.discover_extensions()
    assert set(catalog.sources) == {EXAMPLE_EXTENSION_ID, "acme_folder"}
    assert catalog.sources[EXAMPLE_EXTENSION_ID] == extensions.ExtensionSource(
        id=EXAMPLE_EXTENSION_ID,
        origin="file",
        location=str(module),
        summary="Release metadata for Acme projects.",
        path=module,
    )
    assert catalog.sources["acme_folder"].location == str(package.parent)
    assert catalog.sources["acme_folder"].path == package
    assert (catalog.conflicts, catalog.skipped) == ({}, ())


def test_a_distribution_extension_is_found_by_its_entry_point(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    install_distribution_extension(monkeypatch, tmp_path / "site")
    source = extensions.discover_extensions().sources[EXAMPLE_EXTENSION_ID]
    assert (source.origin, source.location, source.summary, source.path) == (
        "distribution",
        "acme-icode-tools 1.0",
        "Acme tools for iCode",
        None,
    )
    assert source.entry_point is not None
    assert source.entry_point.value == "acme_icode_tools:extension"


def test_finding_extensions_imports_none_of_them(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    install_file_extension(monkeypatch, "loud_file", _EXPLODING)
    install_distribution_extension(monkeypatch, tmp_path / "site", "loud_dist", _EXPLODING, module="loud_dist_module")
    catalog = extensions.discover_extensions()
    assert {"loud_file", "loud_dist"} <= set(catalog.sources)
    assert catalog.sources["loud_file"].summary == "Would break iCode if it ran."
    assert "_chrys_extension_loud_file" not in sys.modules
    assert "loud_dist_module" not in sys.modules


def test_entries_that_are_not_extensions_are_ignored_and_bad_ids_are_skipped(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    extensions_dir = install_file_extension(monkeypatch).parent
    for name in ("_private.py", ".hidden.py", "notes.txt", "Bad-Id.py", "todo.py", "broken.py"):
        (extensions_dir / name).write_text("x = (" if name == "broken.py" else "", encoding="utf-8")
    (extensions_dir / "not_a_package").mkdir()
    catalog = extensions.discover_extensions()
    # A file that does not parse is still offered; importing it reports the error.
    assert set(catalog.sources) == {EXAMPLE_EXTENSION_ID, "broken"}
    assert catalog.sources["broken"].summary == ""
    assert len(catalog.skipped) == 2
    assert "'Bad-Id' must be lowercase letters" in catalog.skipped[0]
    assert "'todo' is the name of one of iCode's own tool categories" in catalog.skipped[1]
    assert all(problem in caplog.text for problem in catalog.skipped)


def test_an_id_two_sources_claim_is_a_conflict(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    module = install_file_extension(monkeypatch)
    install_distribution_extension(monkeypatch, tmp_path / "site")
    catalog = extensions.discover_extensions()
    assert EXAMPLE_EXTENSION_ID not in catalog.sources
    assert [copy.location for copy in catalog.conflicts[EXAMPLE_EXTENSION_ID]] == ["acme-icode-tools 1.0", str(module)]


def test_no_extensions_folder_means_no_file_extensions(tmp_path: Path) -> None:
    catalog = extensions.discover_extensions(tmp_path / "missing")
    assert catalog.conflicts == {}
    assert all(source.origin == "distribution" for source in catalog.sources.values())
