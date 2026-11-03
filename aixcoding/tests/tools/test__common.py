# (tests — asserts are pytest idiom, Chinese prose is the data)
"""Unit tests for aixcoding/tools/_common.py (classification / markers / parsing)."""

from __future__ import annotations

from pathlib import Path

from aixcoding.tools import _common as common


def _write(tmp_path: Path, name: str, content: str) -> Path:
    p = tmp_path / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p


class TestClassification:
    def test_custom_paths(self):
        assert common.is_custom("aixcoding/tools/_common.py")
        assert common.is_custom("src/chrys/kernel/aixcoding/auth/client.py")  # ADR-0011 tier placement
        assert common.is_custom(".github/workflows/aixcoding-tests.yml")
        assert common.is_custom(r"aixcoding\tests\tools\test_x.py")  # windows-style input

    def test_upstream_paths(self):
        assert common.is_upstream("src/chrys/app/tui/screen.py")
        assert common.is_upstream("tests/support/ci.py")
        assert common.is_upstream("pyproject.toml")
        assert common.is_upstream(".github/workflows/ci.yml")
        assert common.is_upstream("src/chrys/aixcoding/legacy.py")  # pre-ADR-0011 single-point layout is upstream now
        assert not common.is_upstream("aixcoding/docs/CUSTOMIZATIONS.md")


class TestMarkers:
    def test_python_comment_marker(self, tmp_path):
        p = _write(
            tmp_path,
            "up.py",
            "X = 1  # AIxCoding telemetry: emit tool events (src/chrys/kernel/aixcoding/tool_events)\n",
        )
        hits = common.scan_python_file("up.py", p)
        assert len(hits) == 1
        hit = hits[0]
        assert hit.is_comment
        assert hit.module == "src/chrys/kernel/aixcoding/tool_events"
        assert hit.line == 1

    def test_brand_string_is_not_comment_marker(self, tmp_path):
        p = _write(tmp_path, "brand.py", 'APP = "AIxCoding"\nNAME = "AIxCoding CLI"\n')
        hits = common.scan_python_file("brand.py", p)
        assert hits and all(not h.is_comment for h in hits)

    def test_bracketed_legacy_form_rejected(self, tmp_path):
        p = _write(tmp_path, "legacy.py", "# [AIxCoding] old bracketed form\n")
        hits = common.scan_python_file("legacy.py", p)
        assert all(not h.is_comment for h in hits)

    def test_lowercase_variant_rejected(self, tmp_path):
        p = _write(tmp_path, "case.py", "# aixcoding telemetry: wrong case\n")
        assert not any(h.is_comment for h in common.scan_python_file("case.py", p))

    def test_keyword_suffix_rejected_by_word_boundary(self, tmp_path):
        p = _write(tmp_path, "suffix.py", "# AIxCodingSuffix: glued word is not the keyword\n")
        assert not any(h.is_comment for h in common.scan_python_file("suffix.py", p))

    def test_module_word_before_colon(self, tmp_path):
        p = _write(tmp_path, "short.py", "# AIxCoding diagnostics: panel whitelist\n")
        hits = common.scan_python_file("short.py", p)
        assert hits[0].is_comment and hits[0].module == "diagnostics"

    def test_text_hash_comment(self, tmp_path):
        p = _write(tmp_path, "conf.toml", "key = 1  # AIxCoding demo: tweak (src/chrys/kernel/aixcoding/demo)\n")
        hits = common.scan_text_file("conf.toml", p)
        assert len(hits) == 1 and hits[0].is_comment
        assert hits[0].module == "src/chrys/kernel/aixcoding/demo"

    def test_text_html_comment(self, tmp_path):
        p = _write(tmp_path, "doc.md", "text\n<!-- AIxCoding demo: panel note -->\n")
        assert len(common.scan_file("doc.md", p)) == 1
        assert common.scan_file("doc.md", p)[0].is_comment

    def test_html_comment_body_drops_closer(self, tmp_path):
        p = _write(tmp_path, "doc.md", "<!-- AIxCoding demo: panel note -->\n")
        assert "-->" not in common.scan_file("doc.md", p)[0].text

    def test_markdown_heading_is_brand_not_marker(self, tmp_path):
        p = _write(tmp_path, "guide.md", "# AIxCoding-CLI settings file reference\n")
        hits = common.scan_file("guide.md", p)
        assert hits and all(not h.is_comment for h in hits)

    def test_marker_exempt_formats_not_scanned(self, tmp_path):
        p = _write(tmp_path, "data.json", '{"note": "# AIxCoding demo: impossible"}\n')
        assert common.scan_file("data.json", p) == []

    def test_added_line_has_marker(self):
        assert common.added_line_has_marker("X = 2  # AIxCoding demo: x")
        assert common.added_line_has_marker("<!-- AIxCoding demo: x -->")
        assert not common.added_line_has_marker("X = 2  # unrelated comment")
        assert not common.added_line_has_marker('name = "AIxCoding"')
        assert not common.added_line_has_marker("")
        assert not common.added_line_has_marker("# AIxCoding heading", allow_hash=False)
        assert not common.added_line_has_marker("X = 2  # AIxCodingSuffix: x")


class TestDesignParsing:
    SIGNED = (
        "# 设计：demo\n\n- 责任人：t\n- 状态：已确认\n- 确认日期：2026-10-25\n\n"
        "## 上游改动清单（R2）\n\n"
        "| 文件 | 改什么 | 预估行数 | 标记注释位置 |\n|---|---|---|---|\n"
        "| src/chrys/app/up.py | tweak | 1 | 行尾 |\n"
        "| src/chrys/app/up2.py | tweak2 | 2 | 行尾 |\n\n"
        "## 定制点登记预告\n\n"
        "| 落点 | anchor（依赖的上游符号） | 风险等级 |\n|---|---|---|\n"
        "| src/chrys/kernel/aixcoding/demo | MODULE_ORDER | 低 |\n"
    )
    UNSIGNED = SIGNED.replace("- 状态：已确认", "- 状态：草案")

    def test_signoff_status(self):
        assert common.signoff_status(self.SIGNED) == "已确认"
        assert common.signoff_status(self.UNSIGNED) == "草案"

    def test_parse_r2_table(self):
        rows = common.parse_table_section(self.SIGNED, "上游改动清单（R2）")
        assert [r["文件"] for r in rows] == ["src/chrys/app/up.py", "src/chrys/app/up2.py"]

    def test_parse_registration_table(self):
        rows = common.parse_table_section(self.SIGNED, "定制点登记预告")
        assert rows[0]["落点"] == "src/chrys/kernel/aixcoding/demo"

    def test_missing_section_returns_empty(self):
        assert common.parse_table_section(self.SIGNED, "不存在的节") == []

    def test_r2_pool_signed_and_unsigned(self, tmp_path):
        # docs_root.parents[1] is the repo root the pool verifies files against,
        # so the tree must mirror the real layout: repo/aixcoding/docs/batches/…
        # plus the R2-named files themselves.
        repo = tmp_path / "repo"
        docs = repo / "aixcoding" / "docs"
        for name, text in (("signed", self.SIGNED), ("unsigned", self.UNSIGNED)):
            d = docs / "batches" / name
            d.mkdir(parents=True)
            (d / "design.md").write_text(text, encoding="utf-8")
        for rel in ("src/chrys/app/up.py", "src/chrys/app/up2.py"):
            _write(repo, rel, "# up\n")
        approved, notes = common.r2_approved_upstream_files(docs)
        assert approved == {"src/chrys/app/up.py", "src/chrys/app/up2.py"}
        assert len(notes) == 1 and "unsigned/design.md" in notes[0]

    def test_r2_pool_ignores_names_not_in_repo(self, tmp_path):
        # A misspelled R2 name must be a loud note, never a silent pool entry.
        repo = tmp_path / "repo"
        docs = repo / "aixcoding" / "docs"
        d = docs / "batches" / "signed"
        d.mkdir(parents=True)
        (d / "design.md").write_text(self.SIGNED, encoding="utf-8")
        _write(repo, "src/chrys/app/up.py", "# up\n")
        approved, notes = common.r2_approved_upstream_files(docs)
        assert approved == {"src/chrys/app/up.py"}
        assert any("up2.py" in n and "not a file in the repo" in n for n in notes)

    def test_r2_header_drift_is_loud(self, tmp_path):
        drifted = self.SIGNED.replace("| 文件 | 改什么 |", "| file | what |")
        docs = tmp_path / "docs" / "batches" / "drifted"
        docs.mkdir(parents=True)
        (docs / "design.md").write_text(drifted, encoding="utf-8")
        approved, notes = common.r2_approved_upstream_files(docs.parents[1])
        assert approved == set()
        assert any("no recognizable '文件' column" in n for n in notes)


class TestFindDesignDocs:
    # AIxCoding docs-reorg batch: exact scan surface — single-level glob under
    # batches/ only (aixcoding/docs/batches/docs-reorg/design.md).
    def test_batches_layout_only(self, tmp_path):
        docs = tmp_path / "docs"
        inside = _write(docs / "batches" / "demo", "design.md", "# 设计\ndemo\n")
        _write(docs / "topdemo", "design.md", "# 设计\ntop\n")
        assert common.find_design_docs(docs) == [inside]

    def test_no_recursion_into_nested_dirs(self, tmp_path):
        # A recursive glob (batches/**/design.md) would pick this bait up — red then.
        docs = tmp_path / "docs"
        _write(docs / "batches" / "group" / "demo", "design.md", "# 设计\nnested\n")
        assert common.find_design_docs(docs) == []

    def test_empty_and_missing_docs_root(self, tmp_path):
        empty = tmp_path / "empty-docs"
        empty.mkdir()
        assert common.find_design_docs(empty) == []
        assert common.find_design_docs(tmp_path / "absent") == []
