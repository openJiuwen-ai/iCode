# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Streaming reuses only the inline children a new, full block parse would produce."""

from __future__ import annotations

from markdown_it import MarkdownIt

from chrys.app.tui.widgets.markdown.parser import InlineCache, _create_markdown_parser, _parse_streaming_markdown
from chrys.app.tui.widgets.markdown.widget import VirtualizedMarkdown


def test_streaming_tokens_match_full_parse_across_structure_and_reference_changes() -> None:
    inline = r"**重音** [ref] $x$ \\* http://[::1]/path" + "\uff0c句尾"
    sections = f"# {inline}\n\n> {inline}\n\n| header |\n| --- |\n| {inline} |\n\n"
    sources = [
        inline,
        sections + "```py\nopen fence",
        sections + "```py\nopen fence\n```",
        sections + "[ref]: /one",
        sections + "[ref]: /two",
        sections + inline,
        "unrelated",
        "",
    ]
    cache: InlineCache = {}
    for index, source in enumerate(sources):
        old_children = cache.get(inline)
        tokens = _parse_streaming_markdown(_create_markdown_parser(), source, cache)
        assert tokens == _create_markdown_parser().parse(source)
        if index in (1, 2):
            assert old_children is not None
            repeated = [t for t in tokens if t.type == "inline" and t.content == inline]
            assert len(repeated) == 3
            assert all(t.children is old_children for t in repeated)
        if "[ref]:" in source or not source:
            assert cache == {}
        if source == "unrelated":
            assert set(cache) == {source}


def test_streaming_with_a_custom_parser_always_runs_its_document_rules() -> None:
    calls = []

    def custom_parser() -> MarkdownIt:
        parser = _create_markdown_parser()
        parser.core.ruler.push("record_structure", lambda state: calls.append([t.type for t in state.tokens]))
        return parser

    widget = VirtualizedMarkdown(parser_factory=custom_parser)
    widget.set_streaming(True)
    widget._build_blocks("same")
    widget._build_blocks("same\n\n# title")
    assert calls == [
        ["paragraph_open", "inline", "paragraph_close"],
        ["paragraph_open", "inline", "paragraph_close", "heading_open", "inline", "heading_close"],
    ]
