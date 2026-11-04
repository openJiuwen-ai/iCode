# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Check a built wheel the way a PyPI user receives it.

Run from the repository root, with uv and the pinned Python available::

    python scripts/check_wheel.py dist/icode_tui-<version>-py3-none-any.whl [--all-ripgrep]

The wheel's metadata and bundled user guide are read straight from the
archive. Then ``uv tool install`` puts the wheel into a fresh tool
environment, resolving its dependencies from the index as a user's install
does — ``uv.lock`` plays no part — and the installed ``icode`` and its
interpreter are probed: the version, every command's help, the TUI and
optional-feature imports, the bundled guide, the zh-Hans translations the
build compiled into it (the MO is generated, not tracked) and the bundled
ripgrep.
``--all-ripgrep`` requires the ripgrep of every platform the wheel serves
(the release wheel, built after ``fetch_rg.sh --all``).

Stdlib only: the script runs before anything is installed.
"""

from __future__ import annotations

import argparse
import configparser
import email.parser
import os
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

DISTRIBUTION_NAME = "iCode-TUI"
WHEEL_PREFIX = "icode_tui-"
BUNDLED_DOCS_PREFIX = "chrys/app/tui/screens/guides/_docs/"
# Compiled from locales/ by hatch_build.py; scripts/i18n_mo.py::PACKAGE_MO_PATH.
ZH_HANS_CATALOG = "chrys/foundation/i18n/_catalogs/zh-Hans/LC_MESSAGES/chrys.mo"
REPO_ROOT = Path(__file__).resolve().parents[1]
# The one command the wheel installs.
COMMAND = "icode"
# Every command app/cli/app.py dispatches, plus the bare entry point and one plural alias.
HELP_COMMANDS = (
    (),
    ("run",),
    ("agent",),
    ("agents",),
    ("model",),
    ("approval",),
    ("hook",),
    ("acp",),
    ("serve",),
    ("trajectory",),
    ("workflow",),
    ("install",),
    ("uninstall",),
)
# The plural in HELP_COMMANDS, and the command its usage names.
PLURAL_ALIASES = {"agents": "agent"}

# Runs inside the installed tool environment.
_PROBE = """
import subprocess
import sys
from pathlib import Path

import chrys
from chrys.foundation.documentation import resolve_docs_root
from chrys.foundation.i18n.catalogs import load_catalog
from chrys.foundation.vendor import _TRIPLE_MAP, VENDOR_RIPGREP, find_rg

# What a bare install must import: the TUI's whole subtree, the document
# readers, every telemetry module setup_otel loads, the browser host and the
# TUI's services.
import chrys.app.tui.app
import chrys.foundation.observability.exporters
import docx, lxml.etree, openpyxl, pptx, pypdf, xlrd
import opentelemetry.exporter.otlp.proto.grpc._log_exporter
import opentelemetry.exporter.otlp.proto.grpc.metric_exporter
import opentelemetry.exporter.otlp.proto.grpc.trace_exporter
import opentelemetry.instrumentation.logging.handler
import opentelemetry.sdk._logs.export, opentelemetry.sdk.metrics.export, opentelemetry.sdk.trace.export
import PIL.Image, psutil, textual_serve.server, watchdog.observers

package = Path(chrys.__file__).resolve().parent
problems = []
if chrys.__version__ != sys.argv[1]:
    problems.append(f"chrys.__version__ is {chrys.__version__}, expected {sys.argv[1]}")
docs = resolve_docs_root()
if docs is None or not docs.resolve().is_relative_to(package):
    problems.append(f"the user guide resolves to {docs}, not the copy bundled under {package}")
# A loadable catalog without translations would still pass a load check.
cancel = load_catalog("zh-Hans").gettext("tui.theme_editor.cancel")
if cancel != "取消":
    problems.append(f"the bundled zh-Hans catalog translates tui.theme_editor.cancel as {ascii(cancel)}")
rg = find_rg()
if rg is None or not Path(rg).resolve().is_relative_to(VENDOR_RIPGREP):
    problems.append(f"ripgrep resolves to {rg}, not the copy bundled under {VENDOR_RIPGREP}")
else:
    subprocess.run([rg, "--version"], check=True, stdin=subprocess.DEVNULL, capture_output=True)
if sys.argv[2] == "all":
    problems.extend(
        f"no bundled ripgrep {names[0]}"
        for names in _TRIPLE_MAP.values()
        if not (VENDOR_RIPGREP / names[0]).is_file()
    )
if problems:
    sys.exit("\\n".join(problems))
print(f"    chrys {chrys.__version__} at {package}")
print(f"    guide {docs}")
print(f"    rg    {rg}")
"""


def _wheel_version(wheel: Path) -> str:
    if not wheel.name.startswith(WHEEL_PREFIX) or not wheel.name.endswith("-py3-none-any.whl"):
        sys.exit(f"{wheel.name} is not a {WHEEL_PREFIX}<version>-py3-none-any.whl wheel")
    return wheel.name.removeprefix(WHEEL_PREFIX).split("-", 1)[0]


def check_archive(wheel: Path, version: str) -> None:
    """Check the metadata and the bundled user guide without installing anything."""
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        dist_info = f"{WHEEL_PREFIX.rstrip('-')}-{version}.dist-info"
        metadata = email.parser.HeaderParser().parsestr(archive.read(f"{dist_info}/METADATA").decode("utf-8"))
        entry_points = configparser.ConfigParser(delimiters=("=",), interpolation=None)
        entry_points.read_string(archive.read(f"{dist_info}/entry_points.txt").decode("utf-8"))
    problems: list[str] = []
    if metadata["Name"] != DISTRIBUTION_NAME:
        problems.append(f"METADATA names {metadata['Name']!r}, not {DISTRIBUTION_NAME!r}")
    if metadata["Version"] != version:
        problems.append(f"METADATA version {metadata['Version']} does not match the file name's {version}")
    extras = metadata.get_all("Provides-Extra") or []
    if extras:
        problems.append(f"METADATA declares extras {extras}; every dependency belongs to the base install")
    if metadata["Description-Content-Type"] != "text/markdown" or not str(metadata.get_payload()).strip():
        problems.append("METADATA carries no Markdown project description")
    commands = sorted(entry_points["console_scripts"]) if entry_points.has_section("console_scripts") else []
    if commands != [COMMAND]:
        problems.append(f"the wheel installs the commands {commands}, not only {COMMAND}")

    bundled = {name.removeprefix(BUNDLED_DOCS_PREFIX) for name in names if name.startswith(BUNDLED_DOCS_PREFIX)}
    listed = subprocess.run(
        ["git", "ls-files", "-z", "docs"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        encoding="utf-8",
        stdin=subprocess.DEVNULL,
    )
    tracked = {path.removeprefix("docs/") for path in listed.stdout.split("\0") if path}
    missing = sorted(tracked - bundled)
    if missing:
        problems.append(f"the wheel is missing user-guide files: {missing}")
    catalogs = names.count(ZH_HANS_CATALOG)
    if catalogs != 1:
        problems.append(f"the wheel carries {catalogs} copies of {ZH_HANS_CATALOG}, not one")
    if problems:
        sys.exit("\n".join(problems))
    print(
        f"==> {wheel.name}: metadata, all {len(tracked)} user-guide files and the zh-Hans catalog present",
        flush=True,
    )


def check_install(wheel: Path, version: str, *, all_ripgrep: bool) -> None:
    """Install the wheel into a fresh uv tool environment and probe it."""
    python = (REPO_ROOT / ".python-version").read_text(encoding="utf-8").strip()
    with tempfile.TemporaryDirectory(prefix="icode-wheel-") as scratch:
        root = Path(scratch)
        bin_dir = root / "bin"
        tools = {
            "UV_TOOL_DIR": str(root / "tools"),
            "UV_TOOL_BIN_DIR": str(bin_dir),
            "PATH": os.pathsep.join([str(bin_dir), os.environ.get("PATH", "")]),
        }
        # uv keeps the caller's cache, managed interpreters and index settings;
        # iCode itself runs with no settings and an empty home.
        home = root / "home"
        home.mkdir()
        icode_env = {
            # No settings, and no PYTHONPATH/PYTHONHOME that could supply a package the install lacks.
            **{key: value for key, value in os.environ.items() if not key.startswith(("CHRYS_", "PYTHON"))},
            **tools,
            "HOME": str(home),
            "USERPROFILE": str(home),
            "APPDATA": str(home / "AppData" / "Roaming"),
            "LOCALAPPDATA": str(home / "AppData" / "Local"),
        }

        def run(*args: str | Path) -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                [str(arg) for arg in args],
                cwd=root,
                env=icode_env,
                check=True,
                capture_output=True,
                text=True,
                stdin=subprocess.DEVNULL,
            )

        print(f"==> uv tool install {wheel.name} (Python {python}, dependencies from the index)", flush=True)
        subprocess.run(
            ["uv", "tool", "install", "--python", python, str(wheel.resolve())],
            cwd=root,
            env={**os.environ, **tools},
            check=True,
            stdin=subprocess.DEVNULL,
        )

        windows = sys.platform == "win32"
        icode = bin_dir / (f"{COMMAND}.exe" if windows else COMMAND)
        installed = sorted(path.name for path in bin_dir.iterdir())
        if installed != [icode.name]:
            sys.exit(f"uv installed the commands {installed}, not only {icode.name}")
        reported = run(icode, "--version").stdout.strip()
        if reported != version:
            sys.exit(f"{COMMAND} --version printed {reported!r}, expected {version}")
        for subcommand in HELP_COMMANDS:
            # A command the dispatcher misses falls through to the TUI, whose help also exits 0.
            usage = run(icode, *subcommand, "--help").stdout.partition("\n")[0]
            expected = " ".join(["usage:", COMMAND, *(PLURAL_ALIASES.get(word, word) for word in subcommand)])
            if not usage.startswith(f"{expected} "):
                sys.exit(f"{' '.join([COMMAND, *subcommand])} --help printed {usage!r}, not {expected!r}")
        print(f"==> {COMMAND} reports {version}; {len(HELP_COMMANDS)} help screens render", flush=True)

        tool_env = root / "tools" / DISTRIBUTION_NAME.lower()
        interpreter = tool_env / ("Scripts/python.exe" if windows else "bin/python")
        probe = run(interpreter, "-I", "-c", _PROBE, version, "all" if all_ripgrep else "current")
        print("==> installed package probes pass")
        sys.stdout.write(probe.stdout)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("wheel", type=Path)
    parser.add_argument("--all-ripgrep", action="store_true", help="require every platform's bundled ripgrep")
    args = parser.parse_args()
    version = _wheel_version(args.wheel)
    check_archive(args.wheel, version)
    try:
        check_install(args.wheel, version, all_ripgrep=args.all_ripgrep)
    except subprocess.CalledProcessError as exc:
        command = " ".join("<probe>" if arg == _PROBE else str(arg) for arg in exc.cmd)
        sys.stderr.write(f"{command} exited {exc.returncode}\n{exc.stdout or ''}{exc.stderr or ''}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
