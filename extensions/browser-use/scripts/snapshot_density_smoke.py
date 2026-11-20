# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Real-browser acceptance for the W12 batch-④ snapshot density.

Drives the adapter against a real Chrome on the fixture's ``/dense`` page and
checks what the density change promises:

* three same-name text siblings under one parent collapse to one line with
  ``×3``, while their three refs are still returned by ``browser_find``;
* an unnamed, non-interactive, unscrollable node is not printed (its ref still
  exists for ``browser_find``);
* an unnamed scrollable container is printed with the ``*`` marker;
* ``browser_snapshot(target=...)`` serializes only that element's subtree and
  names it in the header.

Run from the repository root::

    python scripts/snapshot_density_smoke.py --allow-browser
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

import icode_browser_use.extension as ext  # noqa: E402 - the path shim above must run first
from chrys.extensions import ToolContext  # noqa: E402

from fixture_site import serve  # noqa: E402
from smoke_common import chrome_executable  # noqa: E402

REF = re.compile(r"@e(\d+)#(\d+)")


def report(event: str, **fields: object) -> None:
    print(json.dumps({"event": event, **fields}, ensure_ascii=True), flush=True)


def require(condition: bool, message: str) -> None:
    """Plain asserts vanish under ``python -O``; a smoke gate must not."""
    if not condition:
        raise RuntimeError(message)


def refs(text: str) -> list[str]:
    return [match.group(0) for match in REF.finditer(text)]


async def run(args: argparse.Namespace) -> None:
    directory = tempfile.TemporaryDirectory(prefix="density-smoke-", ignore_cleanup_errors=True)
    root = Path(directory.name).resolve()
    with directory, contextlib.ExitStack() as stack:
        origin, _state = stack.enter_context(serve())
        os.environ.update(
            {
                "ICODE_BROWSER_ALLOWED_ORIGINS": origin,
                "ICODE_BROWSER_STATE_DIR": str(root / "state"),
                "ICODE_BROWSER_IDLE_CLOSE_SECONDS": "0",
                "ICODE_BROWSER_HEADLESS": "0" if args.headed else "1",
            }
        )
        if args.executable:
            os.environ["ICODE_BROWSER_EXECUTABLE"] = str(args.executable)
        context = ToolContext.for_testing(
            root / "workspace",
            data_dir=root / "data",
            session_data_dir=root / "session",
            agent_name="density-smoke",
            session_id="density-smoke",
        )
        (root / "workspace").mkdir()
        report("fixture", origin=origin)

        browser_id = ""
        child = None
        try:
            opened = await ext.browser_open(context)
            browser_id = opened.split(": ", 1)[1].splitlines()[0].strip()
            await ext.browser_navigate(context, origin + "/dense", browser_id)

            snapshot = await ext.browser_snapshot(context, browser_id)
            report("snapshot", text=snapshot[:2000])
            fold_lines = [line for line in snapshot.splitlines() if "Item row" in line]
            require(len(fold_lines) == 1, f"three same-name siblings were not folded: {snapshot[:1200]}")
            require("×3" in fold_lines[0], f"the folded line has no count: {fold_lines[0]!r}")
            report("folded", line=fold_lines[0].strip())

            found_rows = await ext.browser_find(context, browser_id, "Item row")
            rows = refs(found_rows)
            require(len(rows) == 3, f"browser_find lost the folded refs: {found_rows}")
            require(
                all(row not in snapshot for row in rows[1:]),
                f"a folded ref is also printed as its own line: {found_rows}",
            )
            require("role=generic" in found_rows, f"the rows are not generic text: {found_rows}")
            report("find_all_refs", count=len(rows))

            ghost = refs(await ext.browser_find(context, browser_id, "section"))
            require(ghost, "the empty section has no ref at all; find must still see it")
            require(ghost[0] not in snapshot, f"the unnamed empty section is printed: {ghost[0]}")
            report("structural_hidden", ref=ghost[0])

            scroller_lines = [
                line for line in snapshot.splitlines() if line.rstrip().endswith("*") and 'generic ""' in line
            ]
            require(scroller_lines, f"the unnamed scroller has no starred line: {snapshot[:1200]}")
            report("scroller", line=scroller_lines[0].strip())

            subtree = await ext.browser_snapshot(context, browser_id, target="Panel")
            report("subtree", text=subtree[:1500])
            require("subtree @" in subtree.splitlines()[0], f"the subtree header is missing: {subtree[:200]}")
            for label in ("Inner A", "Inner B", "Nested"):
                require(label in subtree, f"{label} is missing from the panel subtree: {subtree[:1000]}")
            for outside in ("Item row", "Save"):
                require(outside not in subtree, f"{outside} leaked into the panel subtree: {subtree[:1000]}")

            session = ext._browsers[browser_id].session
            child = session._child
        finally:
            if browser_id:
                with contextlib.suppress(Exception):
                    await ext.browser_close(context, browser_id)
        if child is not None:
            require(not child.alive, "the Owned browser process outlived browser_close")
            report("browser_exit", pid=child.pid, alive=child.alive)
    report(
        "acceptance_passed",
        checks=[
            "same-name siblings fold to x3",
            "folded refs all returned by find",
            "unnamed empty node hidden but findable",
            "unnamed scroller keeps its star",
            "target= serializes only the subtree",
            "owned browser exits",
        ],
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--allow-browser", action="store_true", help="Opt in to launching a real browser")
    parser.add_argument("--executable", help="Explicit Chrome/Edge executable; otherwise plugin discovery")
    parser.add_argument("--headed", action="store_true", help="Show the browser window (default: headless)")
    args = parser.parse_args()
    if not args.allow_browser:
        raise SystemExit("snapshot_density_smoke launches Chrome; pass --allow-browser")
    args.executable = args.executable or chrome_executable()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
