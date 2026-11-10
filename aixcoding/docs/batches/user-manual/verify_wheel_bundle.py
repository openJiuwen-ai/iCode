# ruff: noqa: T201
# (batch-local CLI check — print is the output channel, same as tools/check_docs.py)
"""Batch-local wheel bundle check: bundled docs vs aixcoding/manual/.

Equivalent gate for SAC-restricted machines where scripts/check_wheel.py
cannot spawn subprocess exes (os error 4551). Linux CI keeps using
scripts/check_wheel.py as the authoritative gate.

Usage (repo root): <venv> python aixcoding/docs/batches/user-manual/verify_wheel_bundle.py
"""

import hashlib
import pathlib
import sys
import zipfile

WHEEL = pathlib.Path("dist")
wheel = max(WHEEL.glob("icode_tui-*.whl"))
PREFIX = "chrys/app/tui/screens/guides/_docs/"
MANUAL = pathlib.Path("aixcoding/manual")

z = zipfile.ZipFile(wheel)
bundled = sorted(n[len(PREFIX) :] for n in z.namelist() if n.startswith(PREFIX))
manual = sorted(p.relative_to(MANUAL).as_posix() for p in MANUAL.rglob("*") if p.is_file())
print(f"wheel: {wheel.name}")
print(f"bundled {len(bundled)} vs manual {len(manual)}, list match: {bundled == manual}")


def h(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()[:12]


diff = [f for f in bundled if f in manual and h(z.read(PREFIX + f)) != h((MANUAL / f).read_bytes())]
print("content hash mismatch:", diff or "none")
sys.exit(0 if bundled == manual and not diff else 1)
