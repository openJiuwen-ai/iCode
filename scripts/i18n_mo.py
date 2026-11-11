# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Compile the zh-Hans PO into the MO the runtime reads.

The MO is generated and never tracked: Git cannot merge a binary file, so any
two branches that both changed translations used to conflict on it. Two callers
share this one compiler, so they write the same bytes:

- ``scripts/i18n.py compile``, which validates the source, POT and PO first;
- the wheel build hook ``hatch_build.py``, which runs in an isolated build
  environment and validates nothing. ``scripts/i18n.py check`` and the catalog
  oracle tests own validation.

This module therefore imports only the standard library and Babel (pinned in
``[build-system].requires``), and never ``chrys``, which the build environment
does not have. Its own checks are limited to what any compile needs: the PO
must parse, and the MO must load in stdlib gettext before it replaces the old
one.

The MO header is built from the PO's header fields, so the two agree by
construction (Babel stamps its own version as ``Generated-By``; the build and
dev environments pin the same one). An entry is
compiled when it is not fuzzy and every one of its forms has a non-whitespace
character; ``scripts/i18n.py check`` rejects a form that has non-whitespace
characters but nothing visible, so on a checked PO this is exactly the set of
visibly translated entries.
"""

from __future__ import annotations

import gettext
import io
import os
import tempfile
from collections.abc import Callable
from pathlib import Path

from babel.messages.catalog import Message
from babel.messages.mofile import write_mo
from babel.messages.pofile import read_po

DOMAIN = "chrys"
PO_PATH = Path("locales", "zh-Hans", "LC_MESSAGES", f"{DOMAIN}.po")
"""The maintained zh-Hans PO, relative to the repository root."""
PACKAGE_MO_PATH = f"chrys/foundation/i18n/_catalogs/zh-Hans/LC_MESSAGES/{DOMAIN}.mo"
"""Where the MO sits inside the installed ``chrys`` package (and the wheel)."""
SOURCE_MO_PATH = Path("src", *PACKAGE_MO_PATH.split("/"))
"""The gitignored copy an editable install reads, relative to the repository root."""


def compile_mo(po_path: Path, mo_path: Path, *, verify: Callable[[Path], None] | None = None) -> None:
    """Compile *po_path* into *mo_path*, replacing it atomically.

    *verify* runs on the finished temporary MO before it replaces *mo_path*;
    whatever it raises leaves the existing MO untouched.
    """
    with Path(po_path).open("rb") as stream:
        catalog = read_po(stream, domain=DOMAIN, abort_invalid=True)
    for message in [message for message in catalog if message.id and not _is_effective(message)]:
        del catalog[message.id]

    buffer = io.BytesIO()
    write_mo(buffer, catalog, use_fuzzy=False)
    mo_path = Path(mo_path)
    mo_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=mo_path.parent, prefix=f".{mo_path.name}.", suffix=".tmp", delete=False
        ) as stream:
            temporary_path = Path(stream.name)
            stream.write(buffer.getvalue())
        with temporary_path.open("rb") as stream:
            gettext.GNUTranslations(stream)
        if verify is not None:
            verify(temporary_path)
        temporary_path.chmod(mo_path.stat().st_mode & 0o777 if mo_path.exists() else 0o644)
        os.replace(temporary_path, mo_path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _is_effective(message: Message) -> bool:
    if message.fuzzy:
        return False
    forms = message.string if isinstance(message.string, tuple) else (message.string,)
    return bool(forms) and all(form and form.strip() for form in forms)
