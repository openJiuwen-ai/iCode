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


def test_streaming_reuses_only_identical_top_level_blocks() -> None:
    from chrys.app.tui.widgets.markdown.parser import StreamingBlockCache, _parse_tokens

    stable = "# 开头\n\nA **stable** [link](https://example.com/stable).\n\n"
    source = (
        stable
        + """A [late] link and $x$.

> A quote with a nested list
> - One
> - Two

1. One
   - Nested **bold**
2. Two

| Column | Value |
| --- | --- |
| One | 重音 |

```python
print("waiting")
```

```mermaid
graph TD
  A --> B
```

[late]: https://example.com/one
"""
    )
    cache = StreamingBlockCache()
    inline: InlineCache = {}

    def content_state(content):
        return content.plain, tuple(content._spans)

    def block_state(block):
        return (
            block,
            content_state(block.content),
            [content_state(cell) for cell in block.table_headers or ()],
            [[content_state(cell) for cell in row] for row in block.table_rows or ()],
        )

    sources = [stable, stable + "New text."]
    sources.extend(source[:end] for end in range(len(stable) + 1, len(source), 11))
    sources.extend((source, source.replace("/one", "/two"), source.replace("**stable**", "*stable*"), source[:25], ""))
    previous = []
    for index, current in enumerate(sources):
        document = _parse_streaming_markdown(_create_markdown_parser(), current, inline)
        blocks = _parse_tokens(document, streaming=cache)
        expected = _parse_tokens(_create_markdown_parser().parse(current))
        assert [block_state(b) for b in blocks] == [block_state(b) for b in expected], current
        if index == 1:
            assert cache.reused == 2
            assert blocks[0] is previous[0]
            assert blocks[1] is previous[1]
        previous = blocks


def test_streaming_copyable_fences_keep_one_copy_footer() -> None:
    from chrys.app.tui.widgets.markdown.copyable import CopyableMarkdown

    markdown = CopyableMarkdown()
    markdown.set_streaming(True)
    prefix = "```python\nprint('ready')\n```\n\n"
    [before] = markdown._build_blocks(prefix)
    after = markdown._build_blocks(prefix + "Next paragraph")[0]
    assert before.padding_bottom == after.padding_bottom == 2
    assert before is not after
