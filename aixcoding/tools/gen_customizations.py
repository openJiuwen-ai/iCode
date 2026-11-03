# ruff: noqa: T201, RUF001
# (CLI tool — print is the output channel; Chinese prose is the data)
"""Customization registry auditor/viewer (DEVELOPMENT.md §5 / §8; ADR-0015).

Three modes:

**Check** (default) — read-only reconciliation between code facts and design
semantics, writing NOTHING (the CUSTOMIZATIONS.md manifest was retired with
the customization-registry batch):

- code facts: upstream-side ``AIxCoding`` comment markers (scan, ``_common``);
- semantics: the point-level registration tables ("定制点登记预告") of
  **signed-off** design.md files — one row per custom point (or per twin
  group): ``id | 落点 | marker 锚 | 预期计数 | anchor | risk | removal_condition``;
- matching key = (落点, marker 锚): the anchor is a verbatim prefix of the
  marker text; twin groups (identical markers in one file) share one row with
  an expected count; longest-anchor-first resolves anchor-prefix overlap
  within a file;
- errors (exit 1): marker without registration (code before design), count
  mismatch, duplicate/malformed id (active + retired high-water), ambiguous
  anchor, missing landing file; ``--strict`` additionally fails orphan
  registrations (registered but not implemented) — the CI posture;
- warnings: orphan registrations in the default (local) mode, unsigned design
  tables (never authorize), parse anomalies.

**Report** (``--report``) — the same in-memory model rendered to stdout as the
on-demand registry view (new-session AI / newcomer panorama; deterministic,
no timestamp). Upgrade batches paste it into their verification.md as a dated
immutable snapshot (no freshness obligation).

**Archaeology** (``--repo <path>``) — scans a foreign checkout (e.g. the
legacy branch copy) and prints/writes a candidate report instead. No
registration parsing, no reconciliation (ROADMAP §4 input).

Usage (repo root)::

    uv run python aixcoding/tools/gen_customizations.py            # check
    uv run python aixcoding/tools/gen_customizations.py --strict   # CI gate
    uv run python aixcoding/tools/gen_customizations.py --report   # full view
    uv run python aixcoding/tools/gen_customizations.py --repo <path> [--out <file>]

Exit code 0 = ok, 1 = errors, 2 = environment error.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from aixcoding.tools import _common as common

REG_SECTION = "定制点登记预告"
ID_RE = re.compile(r"C\d{3}")
ID_RANGE_RE = re.compile(r"(C\d{3})\s*[-–]\s*(C\d{3})")


def expand_ids(cell: str) -> list[str]:
    """Expand an id cell: 'C001' → [C001]; 'C009-C012' (hyphen or en dash) → [C009..C012].

    Twin-group rows register the whole member range so every member id stays
    addressable; malformed cells return [] (the row is then flagged elsewhere).
    """
    cell = cell.strip()
    if not cell:
        return []
    m = ID_RANGE_RE.fullmatch(cell)
    if m:
        a, b = int(m.group(1)[1:]), int(m.group(2)[1:])
        return [f"C{i:03d}" for i in range(a, b + 1)] if b >= a else []
    return [cell]


EXCLUDED_DIRS = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".idea",
    ".vscode",
}
# Build-output names are excluded only at the repository root: the same names
# deeper in the tree are source directories (e.g. tests/orchestration/engine/build).
ROOT_EXCLUDED_DIRS = {"dist", "build", "target"}


def _is_excluded(rel_parts: tuple[str, ...]) -> bool:
    return any(part in EXCLUDED_DIRS for part in rel_parts) or rel_parts[0] in ROOT_EXCLUDED_DIRS


@dataclass
class Registration:
    """One point-level registration row (or one twin-group row) in a signed-off design."""

    design: str  # docs-relative posix path of the design.md
    cid: str  # id cell verbatim (may be a range like C009-C012); display label
    ids: list[str]  # expanded member ids (uniqueness / high-water scope)
    landing: str  # 落点: the upstream file that carries the marker(s)
    anchor: str  # marker 锚: verbatim prefix of the marker comment body
    count: int  # 预期计数 (twin group size; default 1)
    fields: dict[str, str] = field(default_factory=dict)  # anchor/risk/removal_condition …


@dataclass
class Retired:
    """A tombstone row (### 退役记录): the id is spent forever (high-water)."""

    design: str
    cid: str
    ids: list[str]
    landing: str


def iter_upstream_files(repo: Path):
    for path in sorted(repo.rglob("*")):
        if not path.is_file():
            continue
        rel_parts = path.relative_to(repo).parts
        if _is_excluded(rel_parts):
            continue
        rel = common.to_posix(path.relative_to(repo))
        if common.is_custom(rel):
            continue
        yield rel, path


def scan_markers(repo: Path) -> list[common.MarkerHit]:
    hits: list[common.MarkerHit] = []
    for rel, path in iter_upstream_files(repo):
        hits.extend(common.scan_file(rel, path))
    hits.sort(key=lambda h: (h.file, h.line))
    return hits


def _pick(row: dict[str, str], *keywords: str) -> str:
    for key, val in row.items():
        if any(kw.lower() in key.lower() for kw in keywords):
            v = val.strip()
            if v:
                return v
    return ""


def _registration_section(text: str) -> str:
    """The `## 定制点登记预告` section's own text (### subsections included),
    fenced-code aware, stopping at the next `## ` heading.

    Both registration parsers stay scoped to this section (review F1/F2): the
    active-table parser must not absorb the retirement table, and the
    retirement parser must not leak past the section into unrelated ones.
    """
    out: list[str] = []
    in_section = False
    in_code = False
    for line in text.splitlines():
        if line.startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            continue
        if line.startswith("## "):
            if in_section:
                break
            in_section = line[3:].strip() == REG_SECTION
            if in_section:
                out.append(line)
            continue
        if in_section:
            out.append(line)
    return "\n".join(out)


def _active_part(section_text: str) -> str:
    """The section up to its first `### ` subsection — the active table only."""
    out: list[str] = []
    for line in section_text.splitlines():
        if line.startswith("### "):
            break
        out.append(line)
    return "\n".join(out)


def _parse_retired_rows(section_text: str) -> list[dict[str, str]]:
    """Rows of the '### 退役记录' subsection within the registration section text.

    Hand-rolled because _common.parse_table_section matches only '## ' titles;
    scoped by the caller to the registration section (see _registration_section).
    """
    rows: list[dict[str, str]] = []
    in_sub = False
    header: list[str] | None = None
    for line in section_text.splitlines():
        if line.startswith("### "):
            in_sub = line[4:].strip().startswith("退役记录")
            header = None
            continue
        if not in_sub or not line.strip().startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if all(re.fullmatch(r":?-+:?", c) for c in cells):
            continue
        if header is None:
            header = cells
        elif header:
            rows.append(dict(zip(header, cells, strict=False)))
    return rows


def load_registrations(docs_root: Path) -> tuple[list[Registration], list[Retired], list[str]]:
    """Point-level rows + tombstones from signed-off designs only; unsigned ones noted."""
    regs: list[Registration] = []
    retired: list[Retired] = []
    notes: list[str] = []
    for doc in common.find_design_docs(docs_root):
        rel = common.to_posix(doc.relative_to(docs_root.parents[1]))
        try:
            text = doc.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            # Fail-closed and observable (§9): an unreadable design never silently authorizes.
            notes.append(f"{rel}: design.md unreadable — ignored ({exc})")
            continue
        section = _registration_section(text)
        rows = common.parse_table_section(_active_part(section), REG_SECTION)
        retired_rows = _parse_retired_rows(section)
        if not rows and not retired_rows:
            continue
        status = common.signoff_status(text) or ""
        if status != common.SIGNED_OFF:
            notes.append(f"{rel}: registration table present but sign-off is '{status or 'missing'}' — ignored")
            continue
        for row in rows:
            landing = _pick(row, "落点").strip().strip("`")
            if not landing:
                # Header drift must be loud, not a silently skipped row.
                notes.append(f"{rel}: registration row without a recognizable '落点' column — header drift? ignored")
                continue
            anchor = _pick(row, "锚")
            count_raw = _pick(row, "计数")
            try:
                count = int(count_raw) if count_raw else 1
            except ValueError:
                notes.append(f"{rel}: '预期计数' not an integer ({count_raw!r}) — row ignored")
                continue
            cid_cell = row.get("id", "").strip()
            ids = expand_ids(cid_cell)
            if cid_cell and not ids:
                notes.append(f"{rel}: unparseable id cell `{cid_cell}` — row ignored")
                continue
            regs.append(
                Registration(
                    design=rel,
                    cid=cid_cell,
                    ids=ids,
                    landing=common.to_posix(landing),
                    anchor=anchor,
                    count=count,
                    fields=row,
                )
            )
        for row in retired_rows:
            cid_cell = (row.get("id") or "").strip()
            landing = (row.get("原落点") or row.get("落点") or "").strip().strip("`")
            if not cid_cell:
                notes.append(f"{rel}: retired row without an id — tombstone row ignored")
                continue
            ids = expand_ids(cid_cell)
            if not ids:
                notes.append(f"{rel}: unparseable retired id `{cid_cell}` — tombstone row ignored")
                continue
            retired.append(Retired(design=rel, cid=cid_cell, ids=ids, landing=landing))
    return regs, retired, notes


def match_markers_to_regs(
    markers: list[common.MarkerHit], regs: list[Registration], retired: list[Retired], repo: Path
) -> tuple[list[tuple[Registration, list[common.MarkerHit]]], list[str], list[str]]:
    """Returns (row→hits pairs, errors, warnings). Longest-anchor-first per file."""
    errors: list[str] = []
    warnings: list[str] = []

    # --- id uniqueness & format across all designs (active + retired high-water) ---
    seen: dict[str, str] = {}
    for entry in [*regs, *retired]:
        if not entry.ids:
            errors.append(f"{entry.design}: registration row without a parseable id: 落点 {entry.landing}")
            continue
        for cid in entry.ids:
            if ID_RE.fullmatch(cid) is None:
                errors.append(f"{entry.design}: malformed id `{cid}` (expected C###)")
                continue
            if cid in seen:
                errors.append(f"{entry.design}: duplicate id `{cid}` (already used by {seen[cid]})")
            else:
                seen[cid] = entry.design
    errors.extend(
        f"{r.design} {r.cid}: id range holds {len(r.ids)} member id(s) but 预期计数 is {r.count} "
        f"— twin-group row must keep them equal"
        for r in regs
        if r.ids and len(r.ids) != r.count
    )

    # --- landing files must exist ---
    errors.extend(
        f"{r.design} {r.cid}: landing file not found in repo: {r.landing}"
        for r in regs
        if not (repo / r.landing).is_file()
    )

    # --- group comment markers per file ---
    by_file: dict[str, list[common.MarkerHit]] = {}
    for hit in markers:
        if hit.is_comment:
            by_file.setdefault(hit.file, []).append(hit)

    # --- longest-anchor-first matching per file ---
    pairs: list[tuple[Registration, list[common.MarkerHit]]] = []
    matched: set[int] = set()
    by_landing: dict[str, list[Registration]] = {}
    for r in regs:
        by_landing.setdefault(r.landing, []).append(r)
    for landing in sorted(by_landing):
        group_regs = sorted(by_landing[landing], key=lambda r: len(r.anchor), reverse=True)
        remaining = list(by_file.get(landing, []))
        for r in group_regs:
            if not r.anchor:
                warnings.append(f"{r.design} {r.cid}: registration row has no marker 锚 — cannot match")
                pairs.append((r, []))
                continue
            hits_group = [h for h in remaining if h.text.startswith(r.anchor)]
            if not hits_group:
                warnings.append(
                    f"{r.design} {r.cid}: orphan registration — no marker at {r.landing} starts with "
                    f"anchor `{r.anchor}` (designed but not implemented, or anchor drift)"
                )
                pairs.append((r, []))
                continue
            if len(hits_group) != r.count:
                direction = (
                    f"found {len(hits_group)} > expected {r.count} (unregistered marker, or anchor too short)"
                    if len(hits_group) > r.count
                    else f"found {len(hits_group)} < expected {r.count} (marker lost, or count wrong)"
                )
                errors.append(f"{r.design} {r.cid}: twin-group count mismatch at {r.landing} — {direction}")
            pairs.append((r, hits_group))
            matched.update(id(h) for h in hits_group)
            remaining = [h for h in remaining if id(h) not in matched]

    # --- markers no registration absorbed ---
    errors.extend(
        f"{hit.file}:{hit.line}: marker `{hit.text}` has no registration row in any signed-off "
        f"design.md — code before design (R2/R10)"
        for hit in markers
        if hit.is_comment and id(hit) not in matched
    )
    return pairs, errors, warnings


def _marker_summary(text: str, limit: int = 60) -> str:
    s = text.replace("|", "\\|")
    return s if len(s) <= limit else s[: limit - 1] + "…"


def render_report(
    pairs: list[tuple[Registration, list[common.MarkerHit]]],
    warnings: list[str],
    retired: list[Retired],
    error_count: int = 0,
) -> str:
    banner = [
        "# 定制点登记全景（report，实时对账视图）",
        "",
        "> 事实源 = 代码 marker + 已签认 design.md 登记表；本视图为衍生输出（`gen_customizations.py --report`），",
        "> 勿编辑、不落盘。升级批次可将本输出附入 verification.md 作时点快照。",
    ]
    if error_count:
        banner += [
            "",
            (
                f"> ⚠ 对账存在 {error_count} 个 error——本视图仍按当前登记渲染，可能不完整；"
                f"跑 check（或 --strict）看详情。"
            ),
        ]
    lines = [
        *banner,
        "",
        f"## 定制点（{len(pairs)} 行）",
        "",
        "| id | 落点 | marker 行 | marker | 归属模块 | removal_condition | 来源 |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    ordered = sorted(pairs, key=lambda p: p[0].ids[0] if p[0].ids else p[0].cid)
    for reg, hits in ordered:
        lines_no = ", ".join(str(h.line) for h in sorted(hits, key=lambda h: h.line)) or "—"
        marker_cell = _marker_summary(hits[0].text) + (f" ×{len(hits)}" if len(hits) > 1 else "") if hits else "—"
        module = sorted({h.module or "—" for h in hits})
        module_cell = "/".join(module) if hits else (reg.fields.get("归属模块", "") or "—")
        removal = _pick(reg.fields, "removal") or "待补"
        lines.append(
            f"| {reg.cid or '—'} | {reg.landing} | {lines_no} | {marker_cell} | {module_cell} "
            f"| {removal} | {reg.design} |"
        )
    lines += ["", f"## 孤儿登记（{len(warnings)}）", ""]
    if warnings:
        lines.extend(f"- {w}" for w in warnings)
    else:
        lines.append("- （无。）")
    if retired:
        lines += ["", f"## 退役记录（{len(retired)}，id 永不复用）", ""]
        lines.extend(f"- {t.cid}（{t.landing}）— {t.design}" for t in sorted(retired, key=lambda t: t.cid))
    lines += [
        "",
        "## 标记豁免（无法加注释的格式；源：`aixcoding/tools/_common.py::MARKER_EXEMPTIONS`）",
        "",
        "| 文件 | 改动概述 | 依据 |",
        "| --- | --- | --- |",
    ]
    lines.extend(f"| `{f}` | {d} | {src} |" for f, d, src in common.MARKER_EXEMPTIONS)
    lines.append("")
    return "\n".join(lines)


def _reconcile(
    repo: Path,
) -> tuple[
    list[tuple[Registration, list[common.MarkerHit]]],
    list[str],
    list[str],
    list[Registration],
    list[Retired],
]:
    docs_root = repo / "aixcoding" / "docs"
    markers = scan_markers(repo)
    regs, retired, notes = load_registrations(docs_root)
    pairs, errors, warnings = match_markers_to_regs(markers, regs, retired, repo)
    return pairs, errors, [*notes, *warnings], regs, retired


def run_check(repo: Path | None = None, *, strict: bool = False) -> int:
    repo = (repo or common.REPO_ROOT).resolve()
    pairs, errors, warnings, regs, retired = _reconcile(repo)
    total_markers = sum(len(hits) for _, hits in pairs)
    if strict:
        errors = [*errors, *warnings]  # CI posture: orphan registrations fail
        warnings = []
    if errors:
        print(f"gen_customizations: check FAILED — {len(errors)} error(s), {len(warnings)} warning(s):")
        for e in errors:
            print(f"  ERROR {e}")
        for w in warnings:
            print(f"  warn  {w}")
        return 1
    print(
        f"gen_customizations: check OK — {len(pairs)} registration row(s) / {total_markers} marker(s) "
        f"across {len({r.design for r in regs})} design(s), {len(retired)} retired id(s), "
        f"{len(warnings)} warning(s)"
    )
    for w in warnings:
        print(f"  warn  {w}")
    return 0


def run_report(repo: Path | None = None) -> int:
    repo = (repo or common.REPO_ROOT).resolve()
    pairs, errors, warnings, _regs, retired = _reconcile(repo)
    sys.stdout.write(render_report(pairs, warnings, retired, error_count=len(errors)))
    return 0


def run_archaeology(repo: Path, out: Path | None) -> int:
    repo = repo.resolve()
    if not repo.is_dir():
        print(f"gen_customizations: --repo not a directory: {repo}", file=sys.stderr)
        return 2
    hits = scan_markers(repo)

    custom_assets: list[str] = []
    # Legacy single-point layout kept for archaeology scans of pre-ADR-0011 checkouts;
    # current repos carry src/chrys/{tier}/aixcoding/ (ADR-0011).
    bases = ["aixcoding", "src/chrys/aixcoding"]
    bases += [common.to_posix(p.relative_to(repo)) for p in sorted((repo / "src" / "chrys").glob("*/aixcoding"))]
    for base in bases:
        base_dir = repo / base
        if base_dir.is_dir():
            custom_assets.extend(
                common.to_posix(p.relative_to(repo))
                for p in sorted(base_dir.rglob("*"))
                if p.is_file() and not _is_excluded(p.relative_to(repo).parts)
            )

    lines = [
        "# 考古扫描报告（candidate custom points）",
        "",
        f"- 扫描目标：`{repo}`",
        (
            f"- 上游侧标记命中：{len(hits)}（注释标记 {sum(h.is_comment for h in hits)} /"
            f" 非注释命中 {sum(not h.is_comment for h in hits)}）"
        ),
        f"- 定制资产文件数：{len(custom_assets)}（aixcoding/ + src/chrys/*/aixcoding/，兼容考古目标旧单点形态）",
        "",
        "## 上游侧命中（甄别分类；非注释命中多为品牌字符串，需人工判定归属）",
        "",
        "| 类型 | 文件:行 | 内容 | 归属模块 |",
        "| --- | --- | --- | --- |",
    ]
    lines.extend(
        f"| {'注释标记' if h.is_comment else '非注释命中'} | {h.file}:{h.line} "
        f"| {h.text.replace('|', chr(92) + '|')} | {h.module or '—'} |"
        for h in hits
    )
    lines += ["", "## 定制资产清单（ROADMAP §4 盘点输入）", ""]
    lines.extend(f"- `{p}`" for p in custom_assets)

    report = "\n".join(lines) + "\n"
    if out is None:
        sys.stdout.write(report)
    else:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(report, encoding="utf-8")
        print(f"gen_customizations: archaeology report written to {out}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Customization registry auditor/viewer / archaeology scanner")
    parser.add_argument("--repo", type=Path, default=None, help="archaeology mode: foreign checkout to scan")
    parser.add_argument("--out", type=Path, default=None, help="archaeology mode: report file (default stdout)")
    parser.add_argument("--report", action="store_true", help="print the full registry view instead of checking")
    parser.add_argument("--strict", action="store_true", help="check: orphan registrations also fail (CI posture)")
    args = parser.parse_args(argv)
    if args.repo is not None:
        return run_archaeology(args.repo, args.out)
    if args.report:
        return run_report()
    return run_check(strict=args.strict)


if __name__ == "__main__":
    raise SystemExit(main())
