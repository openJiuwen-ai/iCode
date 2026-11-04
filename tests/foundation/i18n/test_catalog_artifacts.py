# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Catalog extraction, update, compilation, and pseudo-locale tests."""

from __future__ import annotations

import gettext
import io
import json
import os
from pathlib import Path

import pytest
from babel.messages.catalog import Catalog
from babel.messages.mofile import write_mo
from scripts import i18n

from chrys.foundation.i18n.formatting import parse_placeholder_names, validate_authored_template
from tests.foundation.i18n._catalog_helpers import (
    _assert_po_mo_semantically_consistent,
    _catalog_paths,
    _message,
    _mutate_first_metadata_comment,
    _prepare_translated_catalog,
    _read_catalog,
    _read_mo_catalog,
    _source_root,
    _write_catalog,
    _write_mo_catalog,
)


def _expect_semantic_gate_failure(
    paths: tuple[Path, Path, Path, Path],
    match: str,
    *,
    error: type[Exception] = AssertionError,
) -> None:
    # *paths* is the (source_root, pot_path, po_path, mo_path) tuple that
    # _prepare_translated_catalog and _catalog_paths callers assemble.
    source_root, pot_path, po_path, mo_path = paths
    with pytest.raises(error, match=match):
        _assert_po_mo_semantically_consistent(
            source_root=source_root,
            pot_path=pot_path,
            po_path=po_path,
            mo_path=mo_path,
        )


def _assert_check_and_compile_reject(paths: tuple[Path, Path, Path, Path], match: str) -> None:
    # Both entry points must refuse the tampered PO, and the failed compile
    # must leave the existing MO bytes untouched.
    source_root, pot_path, po_path, mo_path = paths
    original = mo_path.read_bytes()
    with pytest.raises(i18n.CatalogToolError, match=match):
        i18n.check_catalogs(
            source_root=source_root,
            pot_path=pot_path,
            po_path=po_path,
            location_root=source_root,
        )
    with pytest.raises(i18n.CatalogToolError, match=match):
        i18n.compile_catalog(
            source_root=source_root,
            pot_path=pot_path,
            po_path=po_path,
            mo_path=mo_path,
            location_root=source_root,
        )
    assert mo_path.read_bytes() == original


def test_extract_records_plural_shape_and_round_trip_metadata_deterministically(tmp_path: Path) -> None:
    source_root = _source_root(
        tmp_path,
        "from chrys.foundation.i18n import msg\n"
        "TITLE = msg('dialog.title', fallback='Hello {name}')\n"
        "FILES = msg(\n"
        "    'dialog.files',\n"
        "    fallback='One {name}',\n"
        "    plural_fallback='{count} {name}s',\n"
        ")\n"
        "HELP = msg('dialog.help', fallback='First line\\nSecond line', multiline=True)\n",
    )
    pot_path, _, _ = _catalog_paths(tmp_path)

    extracted = i18n.extract_catalog(source_root=source_root, pot_path=pot_path, location_root=source_root)
    first_bytes = pot_path.read_bytes()
    i18n.extract_catalog(source_root=source_root, pot_path=pot_path, location_root=source_root)

    assert pot_path.read_bytes() == first_bytes
    assert [message.key for message in extracted] == ["dialog.files", "dialog.help", "dialog.title"]
    catalog = _read_catalog(pot_path)
    plural = _message(catalog, "dialog.files")
    assert plural.id == ("dialog.files", "dialog.files#plural")
    assert any(comment.startswith("English: ") for comment in plural.auto_comments)
    assert any(comment.startswith("English-plural: ") for comment in plural.auto_comments)
    assert any(comment.startswith("chrys-meta=") for comment in plural.auto_comments)
    metadata = i18n._parse_metadata(plural, label="test POT")
    files = next(message for message in extracted if message.key == "dialog.files")
    assert metadata.fingerprint == files.fingerprint
    assert metadata.placeholders == {"name"}
    assert metadata.multiline is False
    assert i18n._parse_metadata(_message(catalog, "dialog.help"), label="test POT").multiline is True


def test_extractor_ignores_imported_reuse_of_one_definition(tmp_path: Path) -> None:
    source_root = _source_root(
        tmp_path,
        "from chrys.foundation.i18n import msg\nMESSAGE = msg('dialog.close', fallback='Close')\n",
    )
    (source_root / "consumer.py").write_text(
        "from .messages import MESSAGE\nREFERENCE = MESSAGE.bind()\n", encoding="utf-8"
    )

    extracted = i18n.extract_messages(source_root, location_root=source_root)

    assert [message.key for message in extracted] == ["dialog.close"]


@pytest.mark.parametrize(
    ("source", "match"),
    [
        ("KEY = 'dialog.close'\nMESSAGE = msg(KEY, fallback='Close')\n", "key must be a literal string"),
        ("MESSAGE = msg('Close', fallback='Close')\n", "lowercase dotted segments"),
        ("FALLBACK = 'Close'\nMESSAGE = msg('dialog.close', fallback=FALLBACK)\n", "fallback must be a literal"),
        ("MESSAGE = msg('dialog.close')\n", "requires a literal fallback"),
        (
            "FIRST = msg('dialog.close', fallback='Close')\nSECOND = msg('dialog.close', fallback='Close')\n",
            r"Duplicate msg\(\) key",
        ),
        ("MESSAGE = msg('dialog.close', fallback='{value!r}')\n", "placeholder names"),
        ("MESSAGE = msg('dialog.close', fallback='{value.attr}')\n", "placeholder names"),
        ("MESSAGE = msg('dialog.close', fallback='{value[index]}')\n", "placeholder names"),
        ("MESSAGE = msg('dialog.close', fallback='{value:>10}')\n", "placeholder names"),
        ("MESSAGE = msg('dialog.close', fallback='{value:{width}}')\n", "placeholder names"),
        ("MESSAGE = msg('dialog.close', fallback='{value:}')\n", "placeholder names"),
        (
            "MESSAGE = msg('dialog.close', fallback='{name}', plural_fallback='{value}s')\n",
            "share their non-count placeholders",
        ),
        ("MESSAGE = msg('dialog.close', fallback='{count}')\n", "count placeholder requires plural_fallback"),
        ("MESSAGE = msg('dialog.close', fallback='')\n", "visible content"),
        ("MESSAGE = msg('dialog.close', fallback=' \\n ', multiline=True)\n", "visible content"),
        ("MESSAGE = msg('dialog.close', fallback='\u200b')\n", "visible content"),
        ("MESSAGE = msg('dialog.close', fallback='\u200b \u200b')\n", "visible content"),
        (
            "MESSAGE = msg('dialog.close', fallback='One', plural_fallback='\u200b')\n",
            "plural_fallback must have visible content",
        ),
        ("MESSAGE = msg('dialog.close', fallback='Bad\\ttext')\n", "Control characters"),
        ("MESSAGE = msg('dialog.close', fallback='Bad\\rtext')\n", "Control characters"),
        ("MESSAGE = msg('dialog.close', fallback='Bad\\x1btext')\n", "Control characters"),
        ("MESSAGE = msg('dialog.close', fallback='First\\nSecond')\n", "LF is forbidden"),
        ("MESSAGE = msg('dialog.close', fallback='[bold]Close[/bold]')\n", "markup"),
        (
            "def build():\n    MESSAGE = msg('dialog.close', fallback='Close')\n",
            "module-level assignment",
        ),
        (
            "class Messages:\n    CLOSE = msg('dialog.close', fallback='Close')\n",
            "module-level assignment",
        ),
        (
            "if enabled:\n    MESSAGE = msg('dialog.close', fallback='Close')\n",
            "module-level assignment",
        ),
        ("REFERENCE = msg('dialog.close', fallback='Close').bind()\n", "module-level assignment"),
        (
            (
                "PLURAL = msg('dialog.item', fallback='One', plural_fallback='Many')\n"
                "COLLISION = msg('dialog.item#plural', fallback='Collision')\n"
            ),
            "lookup ID.*collides",
        ),
    ],
    ids=[
        "dynamic-key",
        "invalid-key",
        "dynamic-fallback",
        "missing-fallback",
        "duplicate-key",
        "conversion",
        "attribute-traversal",
        "index-traversal",
        "format-spec",
        "nested-format-spec",
        "empty-format-spec",
        "schema-disagreement",
        "count-without-plural",
        "empty-fallback",
        "whitespace-fallback",
        "zero-width-fallback",
        "mixed-invisible-fallback",
        "zero-width-plural-fallback",
        "tab",
        "carriage-return",
        "escape",
        "single-line-lf",
        "markup",
        "function-local",
        "class-local",
        "conditional",
        "inline-bind",
        "lookup-collision",
    ],
)
def test_extractor_rejects_invalid_definitions(tmp_path: Path, source: str, match: str) -> None:
    source_root = _source_root(tmp_path, "from chrys.foundation.i18n import msg\n" + source)

    with pytest.raises(i18n.CatalogToolError, match=match):
        i18n.extract_messages(source_root, location_root=source_root)


def test_update_refreshes_english_metadata_and_marks_wording_change_fuzzy(tmp_path: Path) -> None:
    source_root = _source_root(
        tmp_path,
        "from chrys.foundation.i18n import msg\nMESSAGE = msg('dialog.close', fallback='Close {name}')\n",
    )
    pot_path, po_path, _ = _catalog_paths(tmp_path)
    i18n.extract_catalog(source_root=source_root, pot_path=pot_path, location_root=source_root)
    i18n.update_catalog(source_root=source_root, po_path=po_path, location_root=source_root)
    catalog = _read_catalog(po_path)
    _message(catalog, "dialog.close").string = "关闭 {name}"
    _write_catalog(po_path, catalog)

    (source_root / "messages.py").write_text(
        "from chrys.foundation.i18n import msg\nMESSAGE = msg('dialog.close', fallback='Dismiss {name}')\n",
        encoding="utf-8",
    )
    i18n.update_catalog(source_root=source_root, po_path=po_path, location_root=source_root)

    updated = _read_catalog(po_path)
    message = _message(updated, "dialog.close")
    metadata = i18n._parse_metadata(message, label="updated PO")
    fresh = i18n.extract_messages(source_root, location_root=source_root)
    assert metadata.fingerprint == fresh[0].fingerprint
    assert any("Dismiss" in comment for comment in message.auto_comments)
    assert message.string == "关闭 {name}"
    assert message.fuzzy


def test_compile_filters_fuzzy_and_untranslated_entries(tmp_path: Path) -> None:
    _, _, _, mo_path = _prepare_translated_catalog(tmp_path)

    with mo_path.open("rb") as stream:
        translations = gettext.GNUTranslations(stream)

    assert translations.gettext("dialog.close") == "关闭 {name}"
    assert translations.ngettext("dialog.files", "dialog.files#plural", 2) == "{count} 个 {name}"
    assert translations.gettext("dialog.empty") == "dialog.empty"
    assert translations.gettext("dialog.fuzzy") == "dialog.fuzzy"


@pytest.mark.parametrize("failure", ["corrupt", "partial-plural", "stale-source"])
def test_compile_failure_preserves_existing_mo_bytes(tmp_path: Path, failure: str) -> None:
    source_root, pot_path, po_path, mo_path = _prepare_translated_catalog(tmp_path)
    original = mo_path.read_bytes()

    if failure == "stale-source":
        source = (source_root / "messages.py").read_text(encoding="utf-8")
        (source_root / "messages.py").write_text(source.replace("Close {name}", "Dismiss {name}"), encoding="utf-8")
    elif failure == "corrupt":
        catalog = _read_catalog(po_path)
        _message(catalog, "dialog.close").string = "\x1b[31m坏翻译"
        _write_catalog(po_path, catalog)
    else:
        # A truthful nplurals=1 header cannot represent a partial plural —
        # Babel's parser rejects surplus forms — so the on-disk shape of this
        # failure class is a hand-tampered multi-form header.
        po_text = po_path.read_text(encoding="utf-8")
        po_text = po_text.replace("nplurals=1; plural=0;", "nplurals=2; plural=(n != 1);")
        po_text = po_text.replace('msgstr[0] "{count} 个 {name}"', 'msgstr[0] "{count} 个 {name}"\nmsgstr[1] ""')
        po_path.write_text(po_text, encoding="utf-8")

    with pytest.raises(i18n.CatalogToolError):
        i18n.compile_catalog(
            source_root=source_root,
            pot_path=pot_path,
            po_path=po_path,
            mo_path=mo_path,
            location_root=source_root,
        )

    assert mo_path.read_bytes() == original


@pytest.mark.parametrize("fuzzy", [False, True], ids=["active", "fuzzy"])
def test_a_non_blank_translation_with_nothing_visible_is_a_gate_error(tmp_path: Path, fuzzy: bool) -> None:
    # The shared compiler (scripts/i18n_mo.py) keeps every form with a
    # non-whitespace character, so the gate must refuse one the runtime would
    # not show instead of letting it into the MO.
    paths = _prepare_translated_catalog(tmp_path)
    catalog = _read_catalog(paths[2])
    message = _message(catalog, "dialog.close")
    message.string = "\u200b"
    if fuzzy:
        message.flags.add("fuzzy")
    _write_catalog(paths[2], catalog)

    _assert_check_and_compile_reject(paths, "no visible content")


def test_a_whitespace_only_translation_stays_untranslated(tmp_path: Path) -> None:
    source_root, pot_path, po_path, mo_path = _prepare_translated_catalog(tmp_path)
    catalog = _read_catalog(po_path)
    _message(catalog, "dialog.close").string = " \u3000 "
    _write_catalog(po_path, catalog)

    i18n.compile_catalog(
        source_root=source_root,
        pot_path=pot_path,
        po_path=po_path,
        mo_path=mo_path,
        location_root=source_root,
    )

    with mo_path.open("rb") as stream:
        translations = gettext.GNUTranslations(stream)
    assert translations.gettext("dialog.close") == "dialog.close"
    assert translations.ngettext("dialog.files", "dialog.files#plural", 2) == "{count} 个 {name}"


def test_partially_translated_plural_is_a_gate_error_for_multi_form_locales(tmp_path: Path) -> None:
    source_root = _source_root(
        tmp_path,
        "from chrys.foundation.i18n import msg\n"
        "FILES = msg('dialog.files', fallback='One {name}', plural_fallback='{count} {name}s')\n",
    )
    extracted = i18n.extract_messages(source_root, location_root=source_root)
    catalog = Catalog(domain="chrys")
    assert catalog.num_plurals == 2
    catalog.add(("dialog.files", "dialog.files#plural"), string=("一个 {name}", ""))

    with pytest.raises(i18n.CatalogToolError, match="Partially translated plural"):
        i18n.validate_translation_catalog(extracted, catalog)


def test_semantic_gate_rejects_an_effective_entry_missing_from_the_mo(tmp_path: Path) -> None:
    source_root, pot_path, po_path, mo_path = _prepare_translated_catalog(tmp_path)
    mo = _read_mo_catalog(mo_path)
    mo.delete("dialog.close")
    _write_mo_catalog(mo_path, mo)

    _expect_semantic_gate_failure((source_root, pot_path, po_path, mo_path), "effective entry set")


def test_semantic_gate_rejects_an_extra_effective_entry_in_the_mo(tmp_path: Path) -> None:
    source_root, pot_path, po_path, mo_path = _prepare_translated_catalog(tmp_path)
    mo = _read_mo_catalog(mo_path)
    mo.add("dialog.extra", string="额外")
    _write_mo_catalog(mo_path, mo)

    _expect_semantic_gate_failure((source_root, pot_path, po_path, mo_path), "effective entry set")


def test_semantic_gate_rejects_an_invisible_extra_mo_entry(tmp_path: Path) -> None:
    source_root, pot_path, po_path, mo_path = _prepare_translated_catalog(tmp_path)
    mo = _read_mo_catalog(mo_path)
    mo.add("dialog.sneaky", string="​⁠")
    _write_mo_catalog(mo_path, mo)

    _expect_semantic_gate_failure((source_root, pot_path, po_path, mo_path), "visible translation content")


def test_semantic_gate_rejects_mo_content_compiled_from_stale_po_text(tmp_path: Path) -> None:
    source_root, pot_path, po_path, mo_path = _prepare_translated_catalog(tmp_path)
    po = _read_catalog(po_path)
    _message(po, "dialog.close").string = "关闭窗口 {name}"
    _write_catalog(po_path, po)

    _expect_semantic_gate_failure((source_root, pot_path, po_path, mo_path), "translation content is stale")


def test_semantic_gate_rejects_mo_plural_metadata_that_differs_from_the_po(tmp_path: Path) -> None:
    source_root, pot_path, po_path, mo_path = _prepare_translated_catalog(tmp_path)
    original = mo_path.read_bytes()
    tampered = original.replace(b"nplurals=1; plural=0;", b"nplurals=2; plural=0;")
    assert tampered != original
    mo_path.write_bytes(tampered)

    _expect_semantic_gate_failure((source_root, pot_path, po_path, mo_path), "metadata is stale")


def test_semantic_gate_rejects_a_physically_stripped_mo_header(tmp_path: Path) -> None:
    # An all-ASCII translation keeps the tampered MO loadable — stdlib gettext
    # needs the header charset only for non-ASCII text — while Babel's MO
    # reader synthesizes the missing Content-Type on the parsed-catalog side.
    source_root = _source_root(
        tmp_path,
        "from chrys.foundation.i18n import msg\nOK = msg('dialog.ok', fallback='OK')\n",
    )
    pot_path, po_path, mo_path = _catalog_paths(tmp_path)
    i18n.extract_catalog(source_root=source_root, pot_path=pot_path, location_root=source_root)
    i18n.update_catalog(source_root=source_root, po_path=po_path, location_root=source_root)
    po = _read_catalog(po_path)
    _message(po, "dialog.ok").string = "Confirmed"
    _write_catalog(po_path, po)
    i18n.compile_catalog(
        source_root=source_root,
        pot_path=pot_path,
        po_path=po_path,
        mo_path=mo_path,
        location_root=source_root,
    )
    original = mo_path.read_bytes()
    tampered = original.replace(b"Content-Type", b"Xontent-Type")
    assert tampered != original
    mo_path.write_bytes(tampered)

    _expect_semantic_gate_failure((source_root, pot_path, po_path, mo_path), "physical header")


def test_semantic_gate_rejects_a_corrupt_mo(tmp_path: Path) -> None:
    source_root, pot_path, po_path, mo_path = _prepare_translated_catalog(tmp_path)
    mo_path.write_bytes(b"not a GNU MO file")

    _expect_semantic_gate_failure((source_root, pot_path, po_path, mo_path), "loadable by Babel and stdlib gettext")


@pytest.mark.parametrize(
    ("key", "translation", "match"),
    [
        ("dialog.empty", "\x1b", "catalog text safety"),
        ("dialog.fuzzy", "[bold]需要复核[/bold]", "catalog text safety"),
        ("dialog.fuzzy", "\u200b dialog.files#plural \u200b", "any catalog lookup ID"),
    ],
    ids=["control-in-non-effective-form", "markup-in-fuzzy-form", "foreign-plural-lookup-id-in-fuzzy-form"],
)
def test_semantic_gate_rejects_unsafe_text_in_every_active_translation_form(
    tmp_path: Path,
    key: str,
    translation: str,
    match: str,
) -> None:
    source_root, pot_path, po_path, mo_path = _prepare_translated_catalog(tmp_path)
    po = _read_catalog(po_path)
    _message(po, key).string = translation
    _write_catalog(po_path, po)

    _expect_semantic_gate_failure((source_root, pot_path, po_path, mo_path), match, error=i18n.CatalogToolError)


@pytest.mark.parametrize(
    ("translation", "match"),
    [
        ("关闭", "placeholder schema"),
        ("关闭 {other}", "placeholder schema"),
        ("关闭 {name!r}", "placeholder names"),
        ("关闭 {name.attr}", "placeholder names"),
        ("关闭 {name[index]}", "catalog text safety"),
        ("关闭 {name:>10}", "placeholder names"),
    ],
    ids=["omitted-slot", "unresolved-slot", "conversion", "attribute-traversal", "index-traversal", "format-spec"],
)
def test_semantic_gate_rejects_schema_violations_in_effective_translations(
    tmp_path: Path,
    translation: str,
    match: str,
) -> None:
    source_root, pot_path, po_path, mo_path = _prepare_translated_catalog(tmp_path)
    po = _read_catalog(po_path)
    _message(po, "dialog.close").string = translation
    _write_catalog(po_path, po)

    _expect_semantic_gate_failure((source_root, pot_path, po_path, mo_path), match, error=i18n.CatalogToolError)


def test_semantic_gate_exempts_fuzzy_text_only_from_schema_dependent_checks(tmp_path: Path) -> None:
    source_root, pot_path, po_path, mo_path = _prepare_translated_catalog(tmp_path)
    po = _read_catalog(po_path)
    fuzzy = _message(po, "dialog.fuzzy")
    assert fuzzy.fuzzy
    fuzzy.string = "旧占位符 {old!r}"
    _write_catalog(po_path, po)

    _assert_po_mo_semantically_consistent(
        source_root=source_root,
        pot_path=pot_path,
        po_path=po_path,
        mo_path=mo_path,
    )


@pytest.mark.parametrize(
    ("original", "changed"),
    [
        ("fallback='Original'", "fallback='Changed'"),
        ("multiline=True", "multiline=False"),
    ],
    ids=["fallback-fingerprint", "multiline-policy"],
)
def test_semantic_gate_rejects_source_metadata_drift(
    tmp_path: Path,
    original: str,
    changed: str,
) -> None:
    source_root = _source_root(
        tmp_path,
        "from chrys.foundation.i18n import msg\nMESSAGE = msg('dialog.message', fallback='Original', multiline=True)\n",
    )
    pot_path, po_path, mo_path = _catalog_paths(tmp_path)
    i18n.extract_catalog(source_root=source_root, pot_path=pot_path, location_root=source_root)
    i18n.update_catalog(source_root=source_root, po_path=po_path, location_root=source_root)
    i18n.compile_catalog(
        source_root=source_root,
        pot_path=pot_path,
        po_path=po_path,
        mo_path=mo_path,
        location_root=source_root,
    )
    source_path = source_root / "messages.py"
    source_path.write_text(source_path.read_text(encoding="utf-8").replace(original, changed), encoding="utf-8")

    _expect_semantic_gate_failure((source_root, pot_path, po_path, mo_path), "stale", error=i18n.CatalogToolError)


def test_check_is_non_mutating_and_detects_stale_source(tmp_path: Path) -> None:
    source_root, pot_path, po_path, mo_path = _prepare_translated_catalog(tmp_path)
    before = {path: path.read_bytes() for path in (pot_path, po_path, mo_path)}

    i18n.check_catalogs(
        source_root=source_root,
        pot_path=pot_path,
        po_path=po_path,
        location_root=source_root,
    )

    assert {path: path.read_bytes() for path in before} == before
    source = (source_root / "messages.py").read_text(encoding="utf-8")
    (source_root / "messages.py").write_text(source.replace("Review me", "Review this"), encoding="utf-8")
    with pytest.raises(i18n.CatalogToolError, match="stale"):
        i18n.check_catalogs(
            source_root=source_root,
            pot_path=pot_path,
            po_path=po_path,
            location_root=source_root,
        )


@pytest.mark.parametrize("stale_header", ["version", "project"])
@pytest.mark.parametrize("target", ["pot", "po"])
def test_check_rejects_stale_project_id_version_headers(tmp_path: Path, target: str, stale_header: str) -> None:
    source_root, pot_path, po_path, _mo_path = _prepare_translated_catalog(tmp_path)

    path = pot_path if target == "pot" else po_path
    catalog = _read_catalog(path)
    if stale_header == "version":
        catalog.version = "0.0.0"
    else:
        catalog.project = "NotChrys"
    _write_catalog(path, catalog)

    with pytest.raises(i18n.CatalogToolError, match="Project-Id-Version is stale"):
        i18n.check_catalogs(
            source_root=source_root,
            pot_path=pot_path,
            po_path=po_path,
            location_root=source_root,
        )


def test_duplicate_physical_po_entries_are_rejected_not_collapsed(tmp_path: Path) -> None:
    source_root, pot_path, po_path, mo_path = _prepare_translated_catalog(tmp_path)
    po_text = po_path.read_text(encoding="utf-8")
    po_path.write_text(po_text + '\nmsgid "dialog.close"\nmsgstr "重复条目"\n', encoding="utf-8")

    _assert_check_and_compile_reject((source_root, pot_path, po_path, mo_path), "duplicate")


def test_missing_physical_plural_forms_header_is_rejected(tmp_path: Path) -> None:
    # Babel infers nplurals=1/plural=0 from Language: zh_Hans alone, so the
    # semantic header pin passes even after the Plural-Forms line is deleted;
    # external gettext tools reading the same PO would fall back to their own
    # plural defaults instead.
    source_root, pot_path, po_path, mo_path = _prepare_translated_catalog(tmp_path)
    lines = po_path.read_text(encoding="utf-8").splitlines()
    stripped = [line for line in lines if "Plural-Forms" not in line]
    assert len(stripped) == len(lines) - 1
    po_path.write_text("\n".join(stripped) + "\n", encoding="utf-8")

    _assert_check_and_compile_reject((source_root, pot_path, po_path, mo_path), "Plural-Forms")


def test_missing_physical_content_type_header_is_rejected(tmp_path: Path) -> None:
    # Babel synthesizes catalog.charset == "utf-8" when the Content-Type
    # declaration is missing, so the parsed-charset pin passes while GNU
    # msgfmt rejects the headerless file.
    source_root, pot_path, po_path, _mo_path = _prepare_translated_catalog(tmp_path)
    lines = po_path.read_text(encoding="utf-8").splitlines()
    stripped = [line for line in lines if "Content-Type" not in line]
    assert len(stripped) == len(lines) - 1
    po_path.write_text("\n".join(stripped) + "\n", encoding="utf-8")

    with pytest.raises(i18n.CatalogToolError, match="Content-Type"):
        i18n.check_catalogs(
            source_root=source_root,
            pot_path=pot_path,
            po_path=po_path,
            location_root=source_root,
        )


def test_duplicated_physical_plural_forms_header_is_rejected(tmp_path: Path) -> None:
    source_root, pot_path, po_path, _mo_path = _prepare_translated_catalog(tmp_path)
    header_line = '"Plural-Forms: nplurals=1; plural=0;\\n"'
    po_text = po_path.read_text(encoding="utf-8")
    assert po_text.count(header_line) == 1
    po_path.write_text(po_text.replace(header_line, header_line + "\n" + header_line), encoding="utf-8")

    with pytest.raises(i18n.CatalogToolError, match="Plural-Forms"):
        i18n.check_catalogs(
            source_root=source_root,
            pot_path=pot_path,
            po_path=po_path,
            location_root=source_root,
        )


def test_translated_copy_of_plural_forms_line_cannot_replace_the_header(tmp_path: Path) -> None:
    # A multiline translation may legally contain the header text verbatim;
    # the physical pin must scan only the header entry, or a deleted header
    # could masquerade behind the translated copy.
    source_root = _source_root(
        tmp_path,
        "from chrys.foundation.i18n import msg\nNOTE = msg('dialog.note', fallback='First\\nSecond', multiline=True)\n",
    )
    pot_path = tmp_path / "locales" / "chrys.pot"
    po_path = tmp_path / "locales" / "zh-Hans" / "LC_MESSAGES" / "chrys.po"
    i18n.extract_catalog(source_root=source_root, pot_path=pot_path, location_root=source_root)
    i18n.update_catalog(source_root=source_root, po_path=po_path, location_root=source_root)

    header_line = '"Plural-Forms: nplurals=1; plural=0;\\n"'
    text = po_path.read_text(encoding="utf-8")
    assert text.count(header_line) == 1
    text = text.replace(header_line + "\n", "")
    text = text.replace('msgstr ""\n\n', 'msgstr ""\n"第一行\\n"\n' + header_line + "\n\n", 1)
    po_path.write_text(text, encoding="utf-8")
    assert po_path.read_text(encoding="utf-8").count("Plural-Forms") == 1

    with pytest.raises(i18n.CatalogToolError, match="Plural-Forms"):
        i18n.check_catalogs(
            source_root=source_root,
            pot_path=pot_path,
            po_path=po_path,
            location_root=source_root,
        )


def test_physical_msgctxt_on_the_header_entry_is_rejected(tmp_path: Path) -> None:
    # Babel drops a msgctxt attached to the header entry before any
    # catalog-level check can see it, while GNU tools reject the file.
    source_root, pot_path, po_path, _mo_path = _prepare_translated_catalog(tmp_path)
    text = po_path.read_text(encoding="utf-8")
    po_path.write_text(text.replace('msgid ""', 'msgctxt "evil"\nmsgid ""', 1), encoding="utf-8")

    with pytest.raises(i18n.CatalogToolError, match="msgctxt"):
        i18n.check_catalogs(
            source_root=source_root,
            pot_path=pot_path,
            po_path=po_path,
            location_root=source_root,
        )


def test_invalid_declared_po_charset_is_reported_not_a_crash(tmp_path: Path) -> None:
    source_root, pot_path, po_path, _mo_path = _prepare_translated_catalog(tmp_path)
    po_text = po_path.read_text(encoding="utf-8")
    assert "charset=utf-8" in po_text
    po_path.write_text(po_text.replace("charset=utf-8", "charset=no_such_codec"), encoding="utf-8")

    with pytest.raises(i18n.CatalogToolError, match="Could not read catalog"):
        i18n.check_catalogs(
            source_root=source_root,
            pot_path=pot_path,
            po_path=po_path,
            location_root=source_root,
        )


def test_valid_non_utf8_declared_charset_is_rejected(tmp_path: Path) -> None:
    # A loadable charset like iso-8859-1 makes Babel decode the UTF-8 bytes
    # under the wrong codec, shipping mojibake translations silently.
    source_root, pot_path, po_path, _mo_path = _prepare_translated_catalog(tmp_path)
    po_text = po_path.read_text(encoding="utf-8")
    assert "charset=utf-8" in po_text
    po_path.write_text(po_text.replace("charset=utf-8", "charset=iso-8859-1"), encoding="utf-8")

    with pytest.raises(i18n.CatalogToolError, match="charset=UTF-8"):
        i18n.check_catalogs(
            source_root=source_root,
            pot_path=pot_path,
            po_path=po_path,
            location_root=source_root,
        )


def test_extraction_rejects_msg_calls_without_the_canonical_import(tmp_path: Path) -> None:
    # A file-local callable that happens to be named msg must fail extraction
    # loudly instead of forging catalog entries from its call sites.
    source_root = _source_root(
        tmp_path,
        "def msg(key, fallback=None):\n"
        "    return (key, fallback)\n"
        "\n"
        "MESSAGE = msg('rogue.key', fallback='Rogue {name}')\n",
    )

    with pytest.raises(i18n.CatalogToolError, match="canonical"):
        i18n.extract_messages(source_root, location_root=source_root)


def test_extraction_reads_a_file_that_spells_msg_only_in_fullwidth(tmp_path: Path) -> None:
    # Extraction skips files that never mention msg, but Python reads
    # identifiers NFKC-normalized: a fullwidth msg() call is still a msg()
    # call, so it is still checked.
    source_root = _source_root(tmp_path, "MESSAGE = \uff4d\uff53\uff47('rogue.key', fallback='Rogue')\n")

    with pytest.raises(i18n.CatalogToolError, match="canonical"):
        i18n.extract_messages(source_root, location_root=source_root)


def test_extraction_rejects_a_conditional_canonical_import(tmp_path: Path) -> None:
    # A conditional canonical import can lose to a rogue same-name binding at
    # runtime while the extractor would still record the call site.
    source_root = _source_root(
        tmp_path,
        "import os\n"
        "if os.environ.get('ROGUE'):\n"
        "    from rogue_module import msg\n"
        "else:\n"
        "    from chrys.foundation.i18n import msg\n"
        "MESSAGE = msg('dialog.rogue', fallback='Rogue')\n",
    )

    with pytest.raises(i18n.CatalogToolError, match="canonical"):
        i18n.extract_messages(source_root, location_root=source_root)


def test_lone_surrogate_fallback_is_a_catalog_tool_error(tmp_path: Path) -> None:
    # A lone surrogate is a valid Python literal but cannot be encoded to
    # UTF-8, so it must fail validation instead of crashing the PO writer.
    source_root = _source_root(
        tmp_path,
        "from chrys.foundation.i18n import msg\nMESSAGE = msg('dialog.close', fallback='Bad\\udcff')\n",
    )

    with pytest.raises(i18n.CatalogToolError, match="surrogates not allowed"):
        i18n.extract_catalog(source_root=source_root, pot_path=tmp_path / "chrys.pot", location_root=source_root)


def test_duplicate_msgstr_fields_inside_one_entry_are_rejected(tmp_path: Path) -> None:
    # Babel keeps the first msgstr of a tampered duplicate pair while GNU
    # msgfmt rejects the file, so the duplicate would ship invisibly.
    source_root, pot_path, po_path, mo_path = _prepare_translated_catalog(tmp_path)
    text = po_path.read_text(encoding="utf-8")
    target = 'msgid "dialog.close"\nmsgstr "关闭 {name}"'
    assert target in text
    po_path.write_text(
        text.replace(target, target + '\nmsgstr "\\x1b[31m邪恶 {name}"'),
        encoding="utf-8",
    )

    _assert_check_and_compile_reject((source_root, pot_path, po_path, mo_path), "duplicate msgstr")


def test_extraction_rejects_a_shadowed_canonical_import(tmp_path: Path) -> None:
    # A class named msg rebinds the canonical import at runtime while the
    # extractor would still read literal call arguments.
    source_root = _source_root(
        tmp_path,
        "from chrys.foundation.i18n import msg\nclass msg:\n    pass\nMESSAGE = msg('rogue.key', fallback='Rogue')\n",
    )

    with pytest.raises(i18n.CatalogToolError, match="rebound"):
        i18n.extract_messages(source_root, location_root=source_root)


def test_extraction_rejects_a_match_star_capture_shadow(tmp_path: Path) -> None:
    # A match-pattern rest capture rebinds msg at runtime, so later calls
    # would hit the captured list while extraction still reads the literals.
    source_root = _source_root(
        tmp_path,
        "from chrys.foundation.i18n import msg\n"
        "FIRST = msg('dialog.first', fallback='First')\n"
        "def sort_names(names):\n"
        "    match names:\n"
        "        case [*msg]:\n"
        "            return list(msg)\n"
        "SECOND = msg('dialog.second', fallback='Second')\n",
    )

    with pytest.raises(i18n.CatalogToolError, match="rebound"):
        i18n.extract_messages(source_root, location_root=source_root)


def test_extraction_rejects_an_except_handler_shadow(tmp_path: Path) -> None:
    # ExceptHandler.name is a plain string in the AST, and Python deletes the
    # target after the handler, so later msg() calls raise NameError at
    # runtime while the extractor would still read the literals.
    source_root = _source_root(
        tmp_path,
        "from chrys.foundation.i18n import msg\n"
        "FIRST = msg('dialog.first', fallback='First')\n"
        "try:\n"
        "    pass\n"
        "except* RuntimeError as msg:\n"
        "    pass\n"
        "SECOND = msg('dialog.second', fallback='Second')\n",
    )

    with pytest.raises(i18n.CatalogToolError, match="rebound"):
        i18n.extract_messages(source_root, location_root=source_root)


@pytest.mark.parametrize(
    "source",
    [
        "MESSAGE = msg('dialog.close', fallback='Close')\nfrom chrys.foundation.i18n import msg\n",
        "MESSAGE = msg('dialog.close', fallback='Close'); from chrys.foundation.i18n import msg\n",
    ],
    ids=["previous-line", "same-line-semicolon"],
)
def test_extraction_rejects_a_msg_call_before_the_canonical_import(tmp_path: Path, source: str) -> None:
    # A definition in a statement before the import raises NameError when the
    # module executes, while extraction would still record the literals;
    # semicolon-joined statements share a line, so ordinals decide, not
    # line numbers.
    source_root = _source_root(tmp_path, source)

    with pytest.raises(i18n.CatalogToolError, match="preceding canonical"):
        i18n.extract_messages(source_root, location_root=source_root)


def test_extraction_rejects_a_dotted_plain_import_root_rebind(tmp_path: Path) -> None:
    # ``import msg.submodule`` binds the ROOT name msg, so later calls hit a
    # module object at runtime while extraction still reads the literals.
    source_root = _source_root(
        tmp_path,
        "from chrys.foundation.i18n import msg\n"
        "import msg.submodule\n"
        "MESSAGE = msg('dialog.close', fallback='Close')\n",
    )

    with pytest.raises(i18n.CatalogToolError, match="rebound"):
        i18n.extract_messages(source_root, location_root=source_root)


def test_extraction_rejects_a_member_import_rebind(tmp_path: Path) -> None:
    # A later i18n member import bound to the name msg wins at runtime, so
    # the calls would construct raw MessageDef objects, not references.
    source_root = _source_root(
        tmp_path,
        "from chrys.foundation.i18n import msg\n"
        "from chrys.foundation.i18n import MessageDef as msg\n"
        "MESSAGE = msg('dialog.close', fallback='Close')\n",
    )

    with pytest.raises(i18n.CatalogToolError, match="rebound"):
        i18n.extract_messages(source_root, location_root=source_root)


@pytest.mark.parametrize(
    "respell",
    [
        lambda data: json.dumps(data, sort_keys=True, separators=(", ", ": ")),
        lambda data: json.dumps({**data, "rogue": 1}, sort_keys=True, separators=(",", ":")),
        lambda data: json.dumps(
            {**data, "fingerprint": data["fingerprint"].upper()}, sort_keys=True, separators=(",", ":")
        ),
    ],
    ids=["spaced-json", "extra-key", "uppercase-fingerprint"],
)
def test_noncanonical_metadata_spellings_are_not_machine_metadata(tmp_path: Path, respell) -> None:
    # Babel's comment wrapping re-wraps any spelling other than the writer's
    # compact encoding on the next update, corrupting the very line a lenient
    # parser would have accepted, so check must reject it up front.
    source_root, pot_path, po_path, _ = _prepare_translated_catalog(tmp_path)
    _mutate_first_metadata_comment(po_path, respell)

    with pytest.raises(i18n.CatalogToolError, match="missing or duplicate Chrys source metadata"):
        i18n.check_catalogs(source_root=source_root, pot_path=pot_path, po_path=po_path, location_root=source_root)


def test_prose_containing_the_metadata_prefix_survives_comment_wrapping(tmp_path: Path) -> None:
    # Babel wraps prose comments at 76 columns, so legal English fallback text
    # can spill a chrys-meta=-prefixed fragment onto its own comment line;
    # only lines decoding to the canonical object are machine metadata.
    filler = "x" * 68
    source_root = _source_root(
        tmp_path,
        "from chrys.foundation.i18n import msg\n"
        f"MESSAGE = msg('dialog.close', fallback='{filler} chrys-meta=forged')\n",
    )
    pot_path = tmp_path / "locales" / "chrys.pot"
    po_path = tmp_path / "locales" / "zh-Hans" / "LC_MESSAGES" / "chrys.po"

    i18n.extract_catalog(source_root=source_root, pot_path=pot_path, location_root=source_root)
    assert "#. chrys-meta=forged" in pot_path.read_text(encoding="utf-8").splitlines()
    i18n.update_catalog(source_root=source_root, po_path=po_path, location_root=source_root)
    i18n.check_catalogs(source_root=source_root, pot_path=pot_path, po_path=po_path, location_root=source_root)


@pytest.mark.parametrize(
    ("anchor", "replacement", "match"),
    [
        (
            'msgid "dialog.close"\nmsgstr "关闭 {name}"',
            'msgid "dialog.close"\nmsgstr "关闭 {name}"\n\nmsgstr "\\x1b[31m隐藏"',
            "stray msgstr",
        ),
        (
            'msgstr[0] "{count} 个 {name}"',
            'msgstr[0] "{count} 个 {name}"\nmsgstr[00] "\\x1b[31m重复"',
            "duplicate msgstr",
        ),
        (
            'msgid "dialog.close"\nmsgstr "关闭 {name}"',
            'msgid "dialog.close"\nmsgstr[0] "关闭"\nmsgstr[1] "坏[bold]文本"',
            "without msgid_plural",
        ),
        (
            'msgstr[0] "{count} 个 {name}"',
            'msgstr "{count} 个 {name}"',
            "plain msgstr on a plural entry",
        ),
        (
            'msgid "dialog.close"\nmsgstr "关闭 {name}"',
            'msgid "dialog.close"\nmsgstr "关闭 {name}"\n\nmsgid  "dialog.close"\nmsgstr "\\x1b[31m邪恶"',
            "duplicate entries",
        ),
        (
            'msgid "dialog.close"\nmsgstr "关闭 {name}"',
            'msgid "dialog.close"\nmsgstr [0] "\\x1b[31m翻译"',
            "malformed entry field",
        ),
        (
            'msgstr[0] "{count} 个 {name}"',
            'msgstr[0] "{count} 个 {name}"\nmsgstr[+0] "\\x1b[31m恶意"',
            "malformed entry field",
        ),
    ],
    ids=[
        "stray-after-blank",
        "zero-padded-index-duplicate",
        "indexed-forms-on-singular-entry",
        "plain-msgstr-on-plural-entry",
        "double-space-duplicate-msgid",
        "space-before-index-bracket",
        "signed-index-duplicate",
    ],
)
def test_physical_entry_field_tampering_is_rejected(tmp_path: Path, anchor: str, replacement: str, match: str) -> None:
    # Babel tolerates these shapes by keeping the first field it saw, while
    # GNU msgfmt rejects each of them outright.
    source_root, pot_path, po_path, _mo_path = _prepare_translated_catalog(tmp_path)
    text = po_path.read_text(encoding="utf-8")
    assert anchor in text
    po_path.write_text(text.replace(anchor, replacement, 1), encoding="utf-8")

    with pytest.raises(i18n.CatalogToolError, match=match):
        i18n.check_catalogs(
            source_root=source_root,
            pot_path=pot_path,
            po_path=po_path,
            location_root=source_root,
        )


def test_mo_plural_metadata_validation_is_exact_not_substring(tmp_path: Path) -> None:
    # nplurals=10 contains nplurals=1 as a substring; the temp-MO gate must
    # compare the parsed fields exactly.
    catalog = Catalog(locale="zh_Hans", domain="chrys", fuzzy=False)
    catalog.add("dialog.close", string="关闭")
    buffer = io.BytesIO()
    write_mo(buffer, catalog, use_fuzzy=False)
    tampered = buffer.getvalue().replace(b"nplurals=1; plural=0;", b"nplurals=10; plural=0")
    assert tampered != buffer.getvalue()
    mo_path = tmp_path / "chrys.mo"
    mo_path.write_bytes(tampered)

    loaded = gettext.GNUTranslations(io.BytesIO(tampered))
    assert "nplurals=10" in loaded.info()["plural-forms"]
    with pytest.raises(i18n.CatalogToolError, match="plural metadata"):
        i18n._validate_mo(mo_path)


def test_check_command_reports_clear_nonzero_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fail_check() -> None:
        raise i18n.CatalogToolError("catalog metadata is stale")

    monkeypatch.setattr(i18n, "check_catalogs", fail_check)

    assert i18n.main(["check"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "Error: catalog metadata is stale\n"


def test_pseudo_refuses_direct_repository_destination(tmp_path: Path) -> None:
    source_root = _source_root(tmp_path, "")
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    with pytest.raises(i18n.CatalogToolError, match="outside the repository"):
        i18n.generate_pseudo_catalog(
            repo_root / "locales",
            source_root=source_root,
            repo_root=repo_root,
            location_root=source_root,
        )


def test_pseudo_command_refuses_relative_locales_destination(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(i18n.REPO_ROOT)

    assert i18n.main(["pseudo", "--output", "locales"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "outside the repository root" in captured.err


def test_pseudo_refuses_symlink_resolving_into_repository(tmp_path: Path) -> None:
    source_root = _source_root(tmp_path, "")
    repo_root = tmp_path / "repo"
    destination = repo_root / "generated"
    destination.mkdir(parents=True)
    symlink = tmp_path / "outside-link"
    try:
        symlink.symlink_to(destination, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"symlinks are unavailable: {error}")

    with pytest.raises(i18n.CatalogToolError, match="outside the repository"):
        i18n.generate_pseudo_catalog(
            symlink,
            source_root=source_root,
            repo_root=repo_root,
            location_root=source_root,
        )


def test_pseudo_refuses_descendant_symlink_back_into_repository(tmp_path: Path) -> None:
    source_root = _source_root(tmp_path, "")
    repo_root = tmp_path / "repo"
    trap_target = repo_root / "generated"
    trap_target.mkdir(parents=True)
    output = tmp_path / "outside"
    output.mkdir()
    try:
        (output / "en-XA").symlink_to(trap_target, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"symlinks are unavailable: {error}")

    with pytest.raises(i18n.CatalogToolError, match="outside the repository"):
        i18n.generate_pseudo_catalog(
            output,
            source_root=source_root,
            repo_root=repo_root,
            location_root=source_root,
        )

    assert list(trap_target.iterdir()) == []


def test_pseudo_refuses_case_variant_spelling_of_repository_root(tmp_path: Path) -> None:
    # On case-insensitive filesystems a case-variant spelling survives
    # resolve() verbatim while naming the repository directory itself.
    source_root = _source_root(tmp_path, "")
    repo_root = tmp_path / "RepoCase"
    repo_root.mkdir()
    if not (tmp_path / "rEPOcASE").exists():
        pytest.skip("requires a case-insensitive filesystem")

    with pytest.raises(i18n.CatalogToolError, match="outside the repository"):
        i18n.generate_pseudo_catalog(
            tmp_path / "rEPOcASE" / "generated",
            source_root=source_root,
            repo_root=repo_root,
            location_root=source_root,
        )

    assert list(repo_root.iterdir()) == []


def test_pseudo_never_writes_through_a_hardlink_to_a_repository_file(tmp_path: Path) -> None:
    # A pre-existing MO hardlinked to a repository file shares its inode, so
    # an in-place open("wb") would rewrite the repository file's bytes even
    # though the target path itself sits outside the repository.
    source_root = _source_root(
        tmp_path,
        "from chrys.foundation.i18n import msg\nMESSAGE = msg('dialog.close', fallback='Close')\n",
    )
    repo_root = tmp_path / "repo"
    victim = repo_root / "docs" / "notes.txt"
    victim.parent.mkdir(parents=True)
    victim.write_text("precious repository content", encoding="utf-8")
    output = tmp_path / "outside"
    target_dir = output / "en-XA" / "LC_MESSAGES"
    target_dir.mkdir(parents=True)
    try:
        os.link(victim, target_dir / "chrys.mo")
    except OSError as error:
        pytest.skip(f"hardlinks are unavailable: {error}")

    generated = i18n.generate_pseudo_catalog(
        output,
        source_root=source_root,
        repo_root=repo_root,
        location_root=source_root,
    )

    assert victim.read_text(encoding="utf-8") == "precious repository content"
    assert not os.path.samestat(generated.stat(), victim.stat())
    translations = gettext.GNUTranslations(io.BytesIO(generated.read_bytes()))
    assert translations.gettext("dialog.close") != "dialog.close"


def test_repository_identity_guard_contains_unbuilt_descendants(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    assert i18n._is_inside_repository(repo_root, repo_root)
    assert i18n._is_inside_repository(repo_root / "locales" / "unbuilt", repo_root)
    assert not i18n._is_inside_repository(tmp_path / "elsewhere" / "unbuilt", repo_root)


def test_pseudo_reports_a_file_typed_output_as_a_catalog_tool_error(tmp_path: Path) -> None:
    source_root = _source_root(tmp_path, "")
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    output = tmp_path / "occupied"
    output.write_text("not a directory", encoding="utf-8")

    with pytest.raises(i18n.CatalogToolError, match="pseudo catalog"):
        i18n.generate_pseudo_catalog(
            output,
            source_root=source_root,
            repo_root=repo_root,
            location_root=source_root,
        )


def test_pseudo_generates_safe_loadable_plural_catalog_and_preserves_placeholders(tmp_path: Path) -> None:
    source_root = _source_root(
        tmp_path,
        "from chrys.foundation.i18n import msg\n"
        "FILES = msg(\n"
        "    'dialog.files',\n"
        "    fallback='One {name}',\n"
        "    plural_fallback='{count} files for {name}',\n"
        ")\n",
    )
    output = tmp_path / "pseudo-output"
    repo_root = tmp_path / "different-repository"
    repo_root.mkdir()

    mo_path = i18n.generate_pseudo_catalog(
        output,
        source_root=source_root,
        repo_root=repo_root,
        location_root=source_root,
    )

    assert mo_path == output / "en-XA" / "LC_MESSAGES" / "chrys.mo"
    with mo_path.open("rb") as stream:
        translations = gettext.GNUTranslations(stream)
    singular = translations.ngettext("dialog.files", "dialog.files#plural", 1)
    plural = translations.ngettext("dialog.files", "dialog.files#plural", 2)
    assert singular.startswith("«") and singular.endswith("··»")
    assert plural.startswith("«") and plural.endswith("··»")
    assert "{name}" in singular
    assert "{count}" in plural and "{name}" in plural
    assert "[" not in singular + plural and "]" not in singular + plural
    assert parse_placeholder_names(singular) == {"name"}
    assert parse_placeholder_names(plural) == {"count", "name"}
    validate_authored_template(singular, multiline=False)
    validate_authored_template(plural, multiline=False)


def test_pseudo_output_guard_uses_real_paths(tmp_path: Path) -> None:
    source_root = _source_root(tmp_path, "")
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    output = tmp_path / "outside"
    result = i18n.generate_pseudo_catalog(
        output / "nested" / os.pardir,
        source_root=source_root,
        repo_root=repo_root,
        location_root=source_root,
    )

    assert result.is_file()


def test_catalog_locations_name_the_file_only_so_line_shifts_stay_fresh(tmp_path: Path) -> None:
    source_root, pot_path, po_path, mo_path = _prepare_translated_catalog(tmp_path)
    before = {path: path.read_bytes() for path in (pot_path, po_path, mo_path)}
    assert "#: messages.py\n" in pot_path.read_text(encoding="utf-8")
    assert "#: messages.py:" not in po_path.read_text(encoding="utf-8")

    source = (source_root / "messages.py").read_text(encoding="utf-8")
    shifted = source.replace("\nCLOSE = ", "\n\n\n# padding\nCLOSE = ", 1)
    assert shifted != source
    (source_root / "messages.py").write_text(shifted, encoding="utf-8")
    i18n.check_catalogs(source_root=source_root, pot_path=pot_path, po_path=po_path, location_root=source_root)
    i18n.extract_catalog(source_root=source_root, pot_path=pot_path, location_root=source_root)
    i18n.update_catalog(source_root=source_root, po_path=po_path, location_root=source_root)

    assert {path: path.read_bytes() for path in before} == before


@pytest.mark.parametrize("target", ["pot", "po"])
def test_check_rejects_line_numbered_or_moved_locations(tmp_path: Path, target: str) -> None:
    source_root, pot_path, po_path, _mo_path = _prepare_translated_catalog(tmp_path)
    path = pot_path if target == "pot" else po_path
    text = path.read_text(encoding="utf-8")
    path.write_text(text.replace("#: messages.py\n", "#: messages.py:2\n", 1), encoding="utf-8")
    with pytest.raises(i18n.CatalogToolError, match="source locations are stale"):
        i18n.check_catalogs(source_root=source_root, pot_path=pot_path, po_path=po_path, location_root=source_root)

    path.write_text(text.replace("#: messages.py\n", "#: other.py\n", 1), encoding="utf-8")
    with pytest.raises(i18n.CatalogToolError, match="source locations are stale"):
        i18n.check_catalogs(source_root=source_root, pot_path=pot_path, po_path=po_path, location_root=source_root)
