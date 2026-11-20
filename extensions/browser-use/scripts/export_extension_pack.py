# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Export the plugin as an offline folder package: ``<out>/extensions/browser/``.

The wheel is for an iCode that has a Python package installer; a standalone
distribution instead drops this folder where iCode's own extension loader reads
it (``<config_dir>/extensions/browser``). The folder package vendors the whole
core under ``browser_use/``, which uses only relative imports, so it needs
neither the wheel nor a source checkout.

Example::

    python scripts/export_extension_pack.py --out dist
    python dist/extensions/browser/doctor.py --json

The build fails if a copied module imports ``browser_use`` by absolute
name: that would silently depend on an installed wheel.
"""

from __future__ import annotations

import argparse
import compileall
import json
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src" / "browser_use"
ID = "browser"

_ABSOLUTE_IMPORT = re.compile(r"^\s*(?:from|import)\s+browser_use\b", re.MULTILINE)
_IGNORED = shutil.ignore_patterns(
    "__pycache__",
    "*.pyc",
    "*.test.js",
    "*.test.mjs",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    ".DS_Store",
)

INIT = '''# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""iCode browser extension, installed as a folder package instead of a wheel.

iCode loads this file as the extension module for id ``{id}`` and reads
``extension`` from it. The core below is vendored in ``browser_use/`` and
uses only relative imports, so nothing here needs the ``browser-use`` wheel or
a source checkout.

Install: run ``python doctor.py install`` here, or copy this folder to
``<config_dir>/extensions/{id}`` (``~/.chrys/extensions`` on macOS/Linux,
``%APPDATA%/chrys/extensions`` on Windows). Diagnostics: ``python doctor.py``.

The Owned and Attach backends work from this package. Bridge and Broker need
``browser-use-host`` / ``browser-use-broker``, which only an installed wheel
provides; ``doctor.py`` reports them as unavailable rather than starting a
command that cannot work.
"""

from __future__ import annotations

from .browser_use.extension import extension

__all__ = ["extension"]
__version__ = "{version}"
'''

DOCTOR = '''# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Run the browser extension diagnostics without installing the wheel.

    python doctor.py [doctor] [--json] [--connect] [--config-dir PATH]
    python doctor.py install [--config-dir PATH] [--replace]
    python doctor.py uninstall [--config-dir PATH]

The core is loaded the way iCode would load it, but through the package's own
``browser_use/`` entry instead of this folder's ``__init__.py``: the
extension module needs ``chrys.extensions`` members that an older iCode may not
have, and the diagnostics must still run there and say which members are
missing instead of failing to start.
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
import types
from pathlib import Path

MODULE = "_chrys_extension_{id}"
CORE = "browser_use"


def load() -> None:
    here = Path(__file__).resolve().parent
    parent = types.ModuleType(MODULE)
    parent.__path__ = [str(here)]
    sys.modules[MODULE] = parent
    name = f"{{MODULE}}.{{CORE}}"
    spec = importlib.util.spec_from_file_location(
        name, here / CORE / "__init__.py", submodule_search_locations=[str(here / CORE)]
    )
    if spec is None or spec.loader is None:
        raise SystemExit(f"doctor: cannot load the extension folder {{here}}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)


def main(argv: list[str]) -> int:
    load()
    command = importlib.import_module(f"{{MODULE}}.{{CORE}}.cli")
    return command.main(argv)


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args or args[0].startswith("-"):
        args = ["doctor", *args]
    raise SystemExit(main(args))
'''

README = """# iCode browser extension (folder package)

This folder is the `{id}` extension for an iCode that was installed without a
Python package installer. It is self-contained: the standard library is the only
dependency and nothing here reaches back into the repository it was built from.

## Install

```powershell
python doctor.py install
```

That copies this folder to `<config_dir>/extensions/{id}` (`~/.chrys/extensions`
on macOS/Linux, `%APPDATA%/chrys/extensions` on Windows), where iCode's extension
loader reads it. Then enable the `browser` extension for an agent.

## Check

```powershell
python doctor.py            # short report
python doctor.py --json     # one machine-readable document
```

`doctor` reads files and process ids only: it never launches a browser, and it
only opens a connection to a running Bridge or Broker with `--connect`.

## Backends

Owned and Attach work from this package. Bridge and Broker need the
`browser-use-host` and `browser-use-broker` commands, which only the wheel
installs; `doctor` reports them as unavailable instead of starting a broken
child process.

## Uninstall

```powershell
python doctor.py uninstall
```

That removes the installed extension folder only. Browser profiles, logins and
action journals stay in the state folder.
"""


def build(out: Path, source: Path = SOURCE) -> Path:
    """Write the folder package under *out*; return the ``extensions/browser`` path."""
    source = Path(source).resolve()
    version = _source_version(source)
    _check_relative_imports(source)
    _check_assets(source)
    target = Path(out).resolve() / "extensions" / ID
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)
    try:
        shutil.copytree(source, target / "browser_use", ignore=_IGNORED)
        _compile(target / "browser_use")
    except BaseException:
        # Never leave a half-written package behind for the host to discover.
        shutil.rmtree(target, ignore_errors=True)
        raise
    (target / "__init__.py").write_text(INIT.format(id=ID, version=version), encoding="utf-8")
    (target / "doctor.py").write_text(DOCTOR.format(id=ID), encoding="utf-8")
    (target / "README.md").write_text(README.format(id=ID), encoding="utf-8")
    return target


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, required=True, help="folder that receives extensions/browser/")
    parser.add_argument("--source", type=Path, default=SOURCE, help="plugin package to vendor")
    args = parser.parse_args()
    target = build(args.out, args.source)
    print(f"exported: {target}")
    print(f"diagnose: python {target / 'doctor.py'} --json")


def _source_version(source: Path) -> str:
    """The version the exported package reports: the plugin source's own literal."""
    match = re.search(r'^__version__ = "([^"]+)"', (source / "__init__.py").read_text(encoding="utf-8"), re.MULTILINE)
    if match is None:
        raise SystemExit(f"{source / '__init__.py'} has no __version__ literal")
    return match.group(1)


def _check_relative_imports(core: Path) -> None:
    offenders = [
        str(path.relative_to(core))
        for path in sorted(core.rglob("*.py"))
        if _ABSOLUTE_IMPORT.search(path.read_text(encoding="utf-8"))
    ]
    if offenders:
        raise SystemExit(
            "these modules import browser_use absolutely and would need the wheel: " + ", ".join(offenders)
        )


def _check_assets(core: Path) -> None:
    """Every file the Bridge manifest names must ship; a pack without them fails at run time."""
    manifest_path = core / "extension" / "manifest.json"
    if not manifest_path.is_file():
        raise SystemExit("the package is missing extension/manifest.json")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    action = manifest.get("action") or {}
    options = manifest.get("options_ui") or {}
    background = manifest.get("background") or {}
    referenced = {
        str(action.get("default_popup") or ""),
        str(options.get("page") or manifest.get("options_page") or ""),
        str(background.get("service_worker") or ""),
    }
    required = {"extension/background.js", "extension/chunks.js", "extension/permissions.js"}
    required |= {f"extension/{name}" for name in referenced if name}
    for name in sorted(required):
        if not (core / name).is_file():
            raise SystemExit(f"the package is missing {name}; the Bridge extension or the core cannot run")


def _compile(core: Path) -> None:
    """A truncated or half-written copy must fail here, not in iCode's log."""
    compiled = compileall.compile_dir(str(core), quiet=1, force=True)
    for cache in core.rglob("__pycache__"):
        shutil.rmtree(cache, ignore_errors=True)
    if not compiled:
        raise SystemExit(f"the exported package under {core} does not compile")


if __name__ == "__main__":
    sys.exit(main())
