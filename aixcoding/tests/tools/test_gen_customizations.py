# (tests — asserts are pytest idiom, Chinese prose is the data)
"""Unit tests for aixcoding/tools/gen_customizations.py (check / report / archaeology).

Error-injection matrix from aixcoding/docs/batches/customization-registry/test-plan.md
(marked \"注入\") plus the drift-regression pair and the zero-write guarantee.
"""

from __future__ import annotations

from pathlib import Path

from aixcoding.tools import gen_customizations as gc


def signed_design(rows: str, retired: str = "") -> str:
    return (
        "# 设计:demo\n\n- 责任人:t\n- 状态:已确认\n- 确认日期:2026-10-25\n\n"
        "## 定制点登记预告\n\n"
        "| id | 落点 | marker 锚 | 预期计数 | anchor | risk | removal_condition |\n"
        "|---|---|---|---|---|---|---|\n"
        f"{rows}"
        f"{retired}"
    )


ROW_SINGLE = "| C001 | src/chrys/app/up.py | AIxCoding demo: whitelist |  | MODULE_ORDER | 低 | 〔常驻〕demo |\n"
UNSIGNED = signed_design(ROW_SINGLE).replace("- 状态:已确认", "- 状态:草案")

MARKER_SINGLE = "X = 1  # AIxCoding demo: whitelist one entry (src/chrys/kernel/aixcoding/demo)\n"
MARKER_TWIN = "T = 1  # AIxCoding demo: twins same text\n"
MARKER_TWIN_LONG = "T = 2  # AIxCoding demo: twins same text extended\n"

ROW_TWIN = "| C002–C003 | src/chrys/app/tw.py | AIxCoding demo: twins same text | 2 | — | 低 | 〔常驻〕twin |\n"


def snapshot(root: Path) -> dict[str, tuple[int, int]]:
    return {
        str(p.relative_to(root)): (p.stat().st_mtime_ns, p.stat().st_size)
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


# --- check: happy paths -------------------------------------------------------


def test_check_ok(file_tree):
    repo = file_tree(
        {
            "src/chrys/app/up.py": MARKER_SINGLE,
            "aixcoding/docs/batches/demo/design.md": signed_design(ROW_SINGLE),
        }
    )
    assert gc.run_check(repo) == 0


def test_check_zero_writes(file_tree, capsys):
    repo = file_tree(
        {
            "src/chrys/app/up.py": MARKER_SINGLE,
            "aixcoding/docs/batches/demo/design.md": signed_design(ROW_SINGLE),
        }
    )
    before = snapshot(repo)
    assert gc.run_check(repo) == 0
    capsys.readouterr()
    assert snapshot(repo) == before  # nothing rewritten, nothing created
    assert not (repo / "aixcoding" / "docs" / "CUSTOMIZATIONS.md").exists()


def test_twin_group_count_ok(file_tree):
    repo = file_tree(
        {
            "src/chrys/app/tw.py": MARKER_TWIN + "T2 = 1\n" + MARKER_TWIN,
            "aixcoding/docs/batches/demo/design.md": signed_design(ROW_TWIN),
        }
    )
    assert gc.run_check(repo) == 0


def test_twin_id_range_ok_and_rendered(file_tree, capsys):
    row = "| C001–C002 | src/chrys/app/tw.py | AIxCoding demo: twins same text | 2 | — | 低 | — |\n"
    repo = file_tree(
        {
            "src/chrys/app/tw.py": MARKER_TWIN + "T2 = 1\n" + MARKER_TWIN,
            "aixcoding/docs/batches/demo/design.md": signed_design(row),
        }
    )
    assert gc.run_check(repo) == 0  # 区间 2 个 id == 预期计数 2
    assert gc.run_report(repo) == 0
    out = capsys.readouterr().out
    assert "| C001–C002 | src/chrys/app/tw.py | 1, 3 |" in out  # range shown compactly


def test_twin_id_range_count_mismatch_errors(file_tree, capsys):
    mismatch = "| C001–C002 | src/chrys/app/tw.py | AIxCoding demo: twins same text | 1 | — | 低 | — |\n"
    repo = file_tree(
        {
            "src/chrys/app/tw.py": MARKER_TWIN + "T2 = 1\n" + MARKER_TWIN,
            "aixcoding/docs/batches/demo/design.md": signed_design(mismatch),
        }
    )
    assert gc.run_check(repo) == 1  # 区间 2 id ≠ 计数 1
    assert "must keep them equal" in capsys.readouterr().out


def test_longest_anchor_first_resolves_prefix_overlap(file_tree):
    # The long-anchor row (extended text) must absorb its marker before the
    # twin row's short anchor sees it (the commands.py C008-vs-twins shape).
    rows = (
        ROW_TWIN
        + "| C004 | src/chrys/app/tw.py | AIxCoding demo: twins same text extended | 1 | — | 低 | 〔常驻〕long |\n"
    )
    repo = file_tree(
        {
            "src/chrys/app/tw.py": MARKER_TWIN + MARKER_TWIN_LONG + MARKER_TWIN,
            "aixcoding/docs/batches/demo/design.md": signed_design(rows),
        }
    )
    assert gc.run_check(repo) == 0


def test_clean_baseline_no_registrations(file_tree, capsys):
    repo = file_tree({"src/chrys/app/up.py": "X = 1\n"})
    assert gc.run_check(repo) == 0
    assert "0 registration row" in capsys.readouterr().out


# --- check: error injections --------------------------------------------------


def test_marker_without_registration_errors(file_tree, capsys):
    repo = file_tree({"src/chrys/app/up.py": MARKER_SINGLE})  # 注入:无登记
    assert gc.run_check(repo) == 1
    assert "no registration row" in capsys.readouterr().out


def test_unsigned_design_does_not_authorize(file_tree):
    repo = file_tree(
        {
            "src/chrys/app/up.py": MARKER_SINGLE,
            "aixcoding/docs/batches/demo/design.md": UNSIGNED,
        }
    )
    assert gc.run_check(repo) == 1  # 注入:未签认 → marker 视为未登记


def test_orphan_registration_warns_not_fails(file_tree, capsys):
    repo = file_tree(
        {
            "src/chrys/app/up.py": "X = 1\n",  # 注入:登记未实施
            "aixcoding/docs/batches/demo/design.md": signed_design(ROW_SINGLE),
        }
    )
    assert gc.run_check(repo) == 0
    assert "orphan registration" in capsys.readouterr().out


def test_orphan_registration_fails_strict(file_tree, capsys):
    repo = file_tree(
        {
            "src/chrys/app/up.py": "X = 1\n",
            "aixcoding/docs/batches/demo/design.md": signed_design(ROW_SINGLE),
        }
    )
    assert gc.run_check(repo, strict=True) == 1  # 注入:strict 孤儿升级
    assert "orphan registration" in capsys.readouterr().out


def test_id_duplicate_across_designs_errors(file_tree, capsys):
    twin_as_c001 = "| C001–C002 | src/chrys/app/tw.py | AIxCoding demo: twins same text | 2 | — | 低 | — |\n"
    repo = file_tree(
        {
            "src/chrys/app/up.py": MARKER_SINGLE,
            "src/chrys/app/tw.py": MARKER_TWIN,
            "aixcoding/docs/batches/demo/design.md": signed_design(ROW_SINGLE),
            "aixcoding/docs/batches/other/design.md": signed_design(twin_as_c001),
        }
    )
    assert gc.run_check(repo) == 1  # 注入:跨 design id 重复(区间成员也参与唯一性)
    assert "duplicate id `C001`" in capsys.readouterr().out


def test_id_malformed_errors(file_tree, capsys):
    bad = signed_design(ROW_SINGLE.replace("C001", "X01"))
    repo = file_tree(
        {
            "src/chrys/app/up.py": MARKER_SINGLE,
            "aixcoding/docs/batches/demo/design.md": bad,
        }
    )
    assert gc.run_check(repo) == 1  # 注入:id 格式
    assert "malformed id" in capsys.readouterr().out


def test_retired_id_reuse_errors(file_tree, capsys):
    retired = (
        "\n### 退役记录\n\n| id | 原落点 | 退役日期 | 依据 |\n|---|---|---|---|\n"
        "| C001 | src/chrys/app/old.py | 2026-10-31 | demo |\n"
    )
    repo = file_tree(
        {
            "src/chrys/app/up.py": MARKER_SINGLE,
            "aixcoding/docs/batches/demo/design.md": signed_design(ROW_SINGLE, retired=retired),
        }
    )
    assert gc.run_check(repo) == 1  # 注入:退役号复用(高水位唯一性)
    assert "duplicate id `C001`" in capsys.readouterr().out


def test_retired_id_highwater_coexists(file_tree):
    retired = (
        "\n### 退役记录\n\n| id | 原落点 | 退役日期 | 依据 |\n|---|---|---|---|\n"
        "| C001 | src/chrys/app/old.py | 2026-10-31 | demo |\n"
    )
    rows = "| C002 | src/chrys/app/up.py | AIxCoding demo: whitelist |  | MODULE_ORDER | 低 | 〔常驻〕demo |\n"
    repo = file_tree(
        {
            "src/chrys/app/up.py": MARKER_SINGLE,
            "aixcoding/docs/batches/demo/design.md": signed_design(rows, retired=retired),
        }
    )
    assert gc.run_check(repo) == 0  # 退役号占坑、新行用新号 → 各自合法


def test_retired_strict_coexists(file_tree, capsys):
    # review F1 regression: a legal retirement table must survive CI strict mode —
    # the active-table parser stops at the ### subsection (no phantom count rows).
    retired = (
        "\n### 退役记录\n\n| id | 原落点 | 退役日期 | 依据 |\n|---|---|---|---|\n"
        "| C001 | src/chrys/app/old.py | 2026-10-31 | demo |\n"
    )
    rows = "| C002 | src/chrys/app/up.py | AIxCoding demo: whitelist |  | MODULE_ORDER | 低 | 〔常驻〕demo |\n"
    repo = file_tree(
        {
            "src/chrys/app/up.py": MARKER_SINGLE,
            "aixcoding/docs/batches/demo/design.md": signed_design(rows, retired=retired),
        }
    )
    assert gc.run_check(repo, strict=True) == 0
    out = capsys.readouterr().out
    assert "not an integer" not in out
    assert "check OK" in out


def test_retired_malformed_id_noted(file_tree, capsys):
    # review F2: a malformed tombstone id (reversed range) must be observable, not dropped.
    retired = (
        "\n### 退役记录\n\n| id | 原落点 | 退役日期 | 依据 |\n|---|---|---|---|\n"
        "| C005–C003 | src/chrys/app/old.py | 2026-10-31 | demo |\n"
    )
    repo = file_tree(
        {
            "src/chrys/app/up.py": MARKER_SINGLE,
            "aixcoding/docs/batches/demo/design.md": signed_design(ROW_SINGLE, retired=retired),
        }
    )
    assert gc.run_check(repo, strict=True) == 1
    assert "unparseable retired id" in capsys.readouterr().out


def test_retired_parser_scoped_to_section(file_tree, capsys):
    # review F2: a '### 退役记录' subsection under an UNRELATED '## ' section
    # must not be absorbed as tombstones.
    design = signed_design(ROW_SINGLE) + (
        "\n## 变更记录\n\n### 退役记录\n\n"
        "| id | 原落点 | 退役日期 | 依据 |\n|---|---|---|---|\n"
        "| C099 | src/chrys/app/elsewhere.py | 2026-10-31 | decoy |\n"
    )
    repo = file_tree(
        {
            "src/chrys/app/up.py": MARKER_SINGLE,
            "aixcoding/docs/batches/demo/design.md": design,
        }
    )
    assert gc.run_check(repo, strict=True) == 0
    out = capsys.readouterr().out
    assert "0 retired id(s)" in out  # decoy absorbed anywhere would show as 1 retired
    assert "C099" not in out
    assert "decoy" not in out


def test_design_unreadable_fails_closed(file_tree, monkeypatch, capsys):
    # review F4: an unreadable design.md must be observable, never silently skipped.
    repo = file_tree(
        {
            "src/chrys/app/up.py": MARKER_SINGLE,
            "aixcoding/docs/batches/demo/design.md": signed_design(ROW_SINGLE),
        }
    )

    def boom(self, *args, **kwargs):
        raise OSError("boom")

    monkeypatch.setattr(Path, "read_text", boom)
    assert gc.run_check(repo, strict=True) == 1
    out = capsys.readouterr().out
    assert "unreadable" in out and "demo/design.md" in out


def test_main_exit_codes(file_tree, monkeypatch, tmp_path):
    # review F4: the CLI surface itself — 0 ok / 1 strict error / 2 environment.
    ok_repo = file_tree(
        {
            "src/chrys/app/up.py": MARKER_SINGLE,
            "aixcoding/docs/batches/demo/design.md": signed_design(ROW_SINGLE),
        }
    )
    monkeypatch.setattr(gc.common, "REPO_ROOT", ok_repo)
    assert gc.main([]) == 0
    assert gc.main(["--strict"]) == 0
    orphan = tmp_path / "orphan_repo"
    (orphan / "aixcoding" / "docs" / "batches" / "demo").mkdir(parents=True)
    (orphan / "aixcoding" / "docs" / "batches" / "demo" / "design.md").write_text(
        signed_design(ROW_SINGLE), encoding="utf-8"
    )
    monkeypatch.setattr(gc.common, "REPO_ROOT", orphan)
    assert gc.main(["--strict"]) == 1  # orphan registration fails under the CI posture
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setattr(gc.common, "REPO_ROOT", empty)
    assert gc.main(["--report"]) == 0  # empty registry renders fine
    assert gc.main(["--repo", str(tmp_path / "nope")]) == 2


def test_anchor_too_short_count_overflow_errors(file_tree, capsys):
    short = "| C001 | src/chrys/app/up.py | AIxCoding demo | 1 | — | 低 | — |\n"
    repo = file_tree(
        {
            "src/chrys/app/up.py": MARKER_SINGLE + "X = 2  # AIxCoding demo: other\n",
            "aixcoding/docs/batches/demo/design.md": signed_design(short),
        }
    )
    assert gc.run_check(repo) == 1  # 注入:锚过短命中 2 marker
    assert "anchor too short" in capsys.readouterr().out


def test_twin_count_short_errors(file_tree, capsys):
    repo = file_tree(
        {
            "src/chrys/app/tw.py": MARKER_TWIN,  # 登记预期 2,实有 1
            "aixcoding/docs/batches/demo/design.md": signed_design(ROW_TWIN),
        }
    )
    assert gc.run_check(repo) == 1  # 注入:孪生计数少
    assert "marker lost" in capsys.readouterr().out


def test_twin_count_overflow_direction_errors(file_tree, capsys):
    # review F4: the overflow direction (more markers than expected) as its own case —
    # distinct from the anchor-too-short shape (same anchor, no ambiguity, count 2 vs 3).
    row = "| C002–C003 | src/chrys/app/tw.py | AIxCoding demo: twins same text | 2 | — | 低 | — |\n"
    repo = file_tree(
        {
            "src/chrys/app/tw.py": MARKER_TWIN + "T2 = 1\n" + MARKER_TWIN + "T3 = 1\n" + MARKER_TWIN,
            "aixcoding/docs/batches/demo/design.md": signed_design(row),
        }
    )
    assert gc.run_check(repo) == 1  # 3 markers vs expected 2 → unregistered direction
    out = capsys.readouterr().out
    assert "unregistered marker, or anchor too short" in out


def test_landing_missing_errors(file_tree, capsys):
    repo = file_tree(
        {
            "aixcoding/docs/batches/demo/design.md": signed_design(ROW_SINGLE),
            # src/chrys/app/up.py 不存在 → 注入:落点缺失(同时孤儿)
        }
    )
    assert gc.run_check(repo, strict=True) == 1
    assert "landing file not found" in capsys.readouterr().out


def test_fenced_example_tables_not_parsed(file_tree, capsys):
    # 契约示例(```markdown 围栏内)不得被当作真实登记/退役表——customization-registry
    # design 的格式示例曾整表混入对账(duplicate id / malformed id 实证),fail-closed。
    design = (
        "# 设计:demo\n\n- 责任人:t\n- 状态:已确认\n- 确认日期:2026-10-25\n\n"
        "## 定制点登记预告\n\n(不适用。)\n\n"
        "## 登记表格式示例\n\n```markdown\n"
        "## 定制点登记预告\n\n"
        "| id | 落点 | marker 锚 | 预期计数 | anchor | risk | removal_condition |\n"
        "|---|---|---|---|---|---|---|\n"
        "| C099 | src/chrys/app/up.py | AIxCoding demo: whitelist | 1 | — | 低 | — |\n\n"
        "### 退役记录\n\n| id | 原落点 | 退役日期 | 依据 |\n|---|---|---|---|\n"
        "| C098 | src/chrys/app/old.py | 2026-10-31 | demo |\n```\n"
    )
    repo = file_tree(
        {
            "src/chrys/app/up.py": MARKER_SINGLE,
            "aixcoding/docs/batches/demo/design.md": design,
        }
    )
    assert gc.run_check(repo) == 1  # marker 无真实登记(围栏内 C099 不算数)
    out = capsys.readouterr().out
    assert "no registration row" in out
    assert "C099" not in out and "C098" not in out  # 围栏内容零混入


def test_header_drift_fail_closed(file_tree, capsys):
    drifted = signed_design(ROW_SINGLE).replace("| id | 落点 | marker 锚 | 预期计数 |", "| 编号 | 位置 | 锚 | 数 |")
    repo = file_tree(
        {
            "src/chrys/app/up.py": MARKER_SINGLE,
            "aixcoding/docs/batches/demo/design.md": drifted,
        }
    )
    assert gc.run_check(repo) == 1  # 注入:表头损坏 → 行不可识别 → marker 视为未登记
    out = capsys.readouterr().out
    assert "no registration row" in out or "without an id" in out


# --- drift regression ---------------------------------------------------------


def _two_point_tree(file_tree) -> Path:
    rows = (
        "| C001 | src/chrys/app/up.py | AIxCoding demo: whitelist |  | — | 低 | — |\n"
        "| C002 | src/chrys/app/tw.py | AIxCoding demo: twins same text |  | — | 低 | — |\n"
    )
    return file_tree(
        {
            "src/chrys/app/up.py": MARKER_SINGLE,
            "src/chrys/app/tw.py": MARKER_TWIN,
            "aixcoding/docs/batches/demo/design.md": signed_design(rows),
        }
    )


def test_drift_delete_one_marker_keeps_other_ids(file_tree, capsys):
    repo = _two_point_tree(file_tree)
    assert gc.run_report(repo) == 0
    before = capsys.readouterr().out
    assert "| C002 | src/chrys/app/tw.py | 1 |" in before

    (repo / "src" / "chrys" / "app" / "tw.py").write_text("T = 1\n", encoding="utf-8")  # 删 marker
    assert gc.run_report(repo) == 0
    after = capsys.readouterr().out
    # 防漂移 A:C001 行原样,C002 变孤儿但 id/落点不变
    assert "| C001 | src/chrys/app/up.py | 1 |" in after
    assert "| C002 | src/chrys/app/tw.py | — |" in after


def test_drift_insert_earlier_file_keeps_ids(file_tree, capsys):
    repo = _two_point_tree(file_tree)
    assert gc.run_report(repo) == 0
    before = capsys.readouterr().out

    rows_extra = (
        "| C001 | src/chrys/app/up.py | AIxCoding demo: whitelist |  | — | 低 | — |\n"
        "| C002 | src/chrys/app/tw.py | AIxCoding demo: twins same text |  | — | 低 | — |\n"
        "| C003 | src/chrys/app/aa.py | AIxCoding demo: earlier |  | — | 低 | — |\n"
    )
    (repo / "src" / "chrys" / "app" / "aa.py").write_text(
        "A = 1  # AIxCoding demo: earlier file\n", encoding="utf-8"
    )  # 字典序更前的新文件
    (repo / "aixcoding" / "docs" / "batches" / "demo" / "design.md").write_text(
        signed_design(rows_extra), encoding="utf-8"
    )
    assert gc.run_report(repo) == 0
    after = capsys.readouterr().out
    # 防漂移 B:扫描排序位次变化不影响存量 id(id 住登记行)
    assert after.index("| C001 | src/chrys/app/up.py |") < after.index("| C003 |")
    for line in before.splitlines():
        if line.startswith("| C00"):
            assert line in after


# --- report -------------------------------------------------------------------


def test_report_renders_full_view(file_tree, capsys):
    repo = file_tree(
        {
            "src/chrys/app/up.py": MARKER_SINGLE,
            "aixcoding/docs/batches/demo/design.md": signed_design(ROW_SINGLE),
        }
    )
    assert gc.run_report(repo) == 0
    out = capsys.readouterr().out
    assert "| id | 落点 | marker 行 |" in out
    assert "| C001 | src/chrys/app/up.py | 1 |" in out
    assert "〔常驻〕demo" in out  # removal_condition semantics from the row
    assert "aixcoding/docs/batches/demo/design.md" in out
    assert "标记豁免" in out  # exemption table from _common.MARKER_EXEMPTIONS
    assert "chrys.po" in out
    assert "勿编辑" in out  # derived-view banner


def test_report_twin_group_row(file_tree, capsys):
    repo = file_tree(
        {
            "src/chrys/app/tw.py": MARKER_TWIN + "T2 = 1\n" + MARKER_TWIN,
            "aixcoding/docs/batches/demo/design.md": signed_design(ROW_TWIN),
        }
    )
    assert gc.run_report(repo) == 0
    out = capsys.readouterr().out
    assert "| C002–C003 | src/chrys/app/tw.py | 1, 3 |" in out  # twin lines listed
    assert "×2" in out  # twin multiplicity marker


def test_report_orphan_section(file_tree, capsys):
    repo = file_tree(
        {
            "src/chrys/app/up.py": "X = 1\n",
            "aixcoding/docs/batches/demo/design.md": signed_design(ROW_SINGLE),
        }
    )
    assert gc.run_report(repo) == 0
    assert "孤儿登记" in capsys.readouterr().out


def test_report_flags_reconciliation_errors(file_tree, capsys):
    # review F2: report must surface reconciliation errors (silent degrade violates section 9 spirit)
    repo = file_tree(
        {
            "src/chrys/app/up.py": MARKER_SINGLE + "X = 2  # AIxCoding demo: unregistered\n",
            "aixcoding/docs/batches/demo/design.md": signed_design(ROW_SINGLE),  # 第二个 marker 无登记
        }
    )
    assert gc.run_report(repo) == 0
    out = capsys.readouterr().out
    assert "对账存在 1 个 error" in out
    assert "跑 check" in out


# --- archaeology (unchanged behavior) ------------------------------------------


def test_archaeology_mode(file_tree, tmp_path):
    repo = file_tree(
        {
            "src/chrys/app/up.py": MARKER_SINGLE,
            "src/chrys/foundation/branding.py": 'APP = "AIxCoding"\n',
            "src/chrys/kernel/aixcoding/demo.py": "X = 1\n",
        }
    )
    out = tmp_path / "report.md"
    assert gc.run_archaeology(repo, out) == 0
    report = out.read_text(encoding="utf-8")
    assert "注释标记" in report and "非注释命中" in report
    assert "src/chrys/app/up.py:1" in report
    assert "src/chrys/foundation/branding.py" in report
    assert "src/chrys/kernel/aixcoding/demo.py" in report  # custom asset listing


def test_archaeology_bad_repo_path(tmp_path):
    assert gc.run_archaeology(tmp_path / "nope", None) == 2
