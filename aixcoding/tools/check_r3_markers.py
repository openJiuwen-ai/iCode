# ruff: noqa: T201
# (CLI tool — print is the output channel; Chinese prose is the data)
"""R3 marker gate (ADR-0007 machine check): every changed upstream file that
can carry a comment must carry at least one ``AIxCoding`` marker on its
diff-added lines; formats where a comment is impossible (JSON/binary/…)
must be registered in ``_common.MARKER_EXEMPTIONS`` (the shared machine-
readable exemption source, retired from CUSTOMIZATIONS.md with the
customization-registry batch).

File-level granularity by design (aixcoding/docs/batches/devtools/design.md):
one marker per changed upstream file is the machine-checked floor; marker
density within a file is a peer-review concern, not a gate — hunk-level
matching would misfire on reformat/move noise.

Usage (repo root)::

    uv run python aixcoding/tools/check_r3_markers.py --base <ref>

Exit code 0 = ok, 1 = violations, 2 = environment error.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from aixcoding.tools import _common as common


def load_exempt_files() -> set[str]:
    return {f for f, _d, _src in common.MARKER_EXEMPTIONS}


def main(argv: list[str] | None = None, repo: Path | None = None) -> int:
    parser = argparse.ArgumentParser(description="R3 marker comment gate")
    parser.add_argument("--base", required=True, help="git ref to diff against (PR base / upstream tag)")
    args = parser.parse_args(argv)

    root = (repo or common.REPO_ROOT).resolve()
    try:
        changed = common.changed_files(root, args.base)
    except common.GitError as exc:
        print(f"check_r3_markers: git error — {exc}", file=sys.stderr)
        return 2

    upstream_changed = sorted(f for f in changed if common.is_upstream(f))
    untracked = set(common.untracked_files(root))
    exempt = load_exempt_files()
    violations: list[str] = []
    checked = 0

    for rel in upstream_changed:
        if rel in exempt:
            continue
        suffix = Path(rel).suffix.lower()
        if suffix in common.MARKER_EXEMPT_SUFFIXES:
            violations.append(
                f"{rel}: marker-exempt format ('{suffix}') changed but not in the "
                f"_common.MARKER_EXEMPTIONS registry (R3)"
            )
            continue
        scanner_supported = Path(rel).name in common.HASH_COMMENT_FILENAMES or suffix in common.HASH_COMMENT_SUFFIXES
        if not scanner_supported:
            violations.append(
                f"{rel}: unsupported suffix '{suffix or '(none)'}' — scanner cannot verify a "
                f"marker; register an exemption or extend the scanner (aixcoding/tools/_common.py)"
            )
            continue
        checked += 1
        if rel in untracked:
            # Untracked: no diff exists yet — the whole file is the addition.
            try:
                added = (root / rel).read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError as exc:
                # Fail closed: an unreadable upstream addition must not slip past.
                violations.append(f"{rel}: cannot read untracked file for marker check — {exc}")
                continue
        else:
            try:
                added = common.diff_added_lines(root, args.base, rel)
            except common.GitError as exc:
                print(f"check_r3_markers: git error on {rel} — {exc}", file=sys.stderr)
                return 2
        content_added = [line for line in added if line.strip()]
        allow_hash = suffix != ".md"  # '#' is a heading in Markdown, not a comment
        if content_added and not any(
            common.added_line_has_marker(line, allow_hash=allow_hash) for line in content_added
        ):
            violations.append(
                f"{rel}: upstream diff adds {len(content_added)} content line(s) with no AIxCoding marker comment (R3)"
            )

    print(
        f"check_r3_markers: base={args.base} upstream_changed={len(upstream_changed)} "
        f"marker_checked={checked} exempt={len(exempt)}"
    )
    if violations:
        print("check_r3_markers: violation(s):")
        for v in violations:
            print(f"  ERROR {v}")
        print("  -> add '# AIxCoding <module>: <why>' at the change site, or register the file exemption.")
        return 1
    print("check_r3_markers: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
