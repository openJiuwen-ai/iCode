# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Live catalog oracle: the tracked POT, PO, and MO carry exactly the pinned message ids and load through stdlib gettext."""

from __future__ import annotations

import gettext

from scripts import i18n

from tests.foundation.i18n._catalog_helpers import (
    _assert_po_mo_semantically_consistent,
    _effective_catalog_entries,
    _entry_id,
    _read_catalog,
)
from tests.foundation.i18n._catalog_oracle_ids import EXPECTED_MESSAGE_IDS
from tests.support.ci import CI_LINUX_ONLY


@CI_LINUX_ONLY
def test_live_catalog_artifacts_contain_effective_translations_and_are_loadable() -> None:
    pot = _read_catalog(i18n.POT_PATH)
    po = _read_catalog(i18n.ZH_PO_PATH)

    assert len(EXPECTED_MESSAGE_IDS) == 2183
    assert {_entry_id(message) for message in pot if message.id} == EXPECTED_MESSAGE_IDS
    assert {_entry_id(message) for message in po if message.id} == EXPECTED_MESSAGE_IDS
    assert set(_effective_catalog_entries(po)) == EXPECTED_MESSAGE_IDS
    assert po.num_plurals == 1
    assert po.plural_expr == "0"
    with i18n.ZH_MO_PATH.open("rb") as stream:
        translations = gettext.GNUTranslations(stream)
    assert translations.gettext("missing.key") == "missing.key"
    assert "nplurals=1" in translations.info()["plural-forms"]
    assert not (i18n.REPO_ROOT / "locales" / "en" / "LC_MESSAGES" / "chrys.po").exists()
    assert not (i18n.REPO_ROOT / "locales" / "en" / "LC_MESSAGES" / "chrys.mo").exists()
    _assert_po_mo_semantically_consistent(
        source_root=i18n.SOURCE_ROOT,
        pot_path=i18n.POT_PATH,
        po_path=i18n.ZH_PO_PATH,
        mo_path=i18n.ZH_MO_PATH,
    )
