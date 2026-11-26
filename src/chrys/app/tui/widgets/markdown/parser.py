# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Markdown token parser — converts markdown-it tokens into MarkdownBlock objects."""

from __future__ import annotations

import html
import re
import string
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import quote

from markdown_it import MarkdownIt
from markdown_it.common.utils import isLinkClose, isLinkOpen
from markdown_it.rules_block import fence as _markdown_it_block_fence
from markdown_it.rules_core.linkify import linkify as _markdown_it_core_linkify
from markdown_it.rules_inline.linkify import SCHEME_RE
from markdown_it.rules_inline.linkify import linkify as _markdown_it_inline_linkify
from markdown_it.token import Token
from textual._cells import cell_len
from textual._slug import slug_for_tcss_id
from textual.content import Content, Span
from textual.highlight import guess_language, highlight
from textual.style import Style

from chrys.app.tui.widgets.markdown.blocks import BULLETS, MarkdownBlock, MarkdownGutter
from chrys.app.tui.widgets.markdown.diagram.model import DiagnosticSeverity
from chrys.app.tui.widgets.markdown.links import terminal_link_target
from chrys.app.tui.widgets.markdown.math import CompiledMath, compile_math
from chrys.app.tui.widgets.markdown.math.markdown import disable_math, enable_math
from chrys.app.tui.widgets.syntax_theme import NoErrorHighlightTheme

if TYPE_CHECKING:
    from markdown_it.ruler import Ruler
    from markdown_it.rules_block.state_block import StateBlock
    from markdown_it.rules_core.state_core import StateCore
    from markdown_it.rules_inline.state_inline import StateInline

    from chrys.app.tui.widgets.markdown.diagram.model import CompiledDiagram

_ZERO_WIDTH_JOINER = "\u200d"
_LINKIFY_SOURCE_META = "_chrys_linkify_source"
_FENCE_CLOSED_META = "_chrys_fence_closed"
_HTML_LINE_BREAK_RULE = "chrys_html_line_break"
_LITERAL_USER_BACKSLASH_RULE = "chrys_literal_user_backslash"
_HTML_LINE_BREAK = re.compile(r"<br\s*/?>[ \t]*(?:\n[ \t]*)?", re.IGNORECASE)
# CJK sentence punctuation (fullwidth/halfwidth variants and their vertical and
# small presentation forms included), the ellipsis leaders CJK prose typically
# doubles, and the full set of CJK/fullwidth paired delimiters (both sides of
# each pair): parentheses, square/curly/angle/corner/lenticular/tortoise
# brackets and quotation marks, including the curly quotes CJK prose shares
# with English typography.  Dash and wave-dash families are deliberately
# excluded: they double as joiners inside prose and URLs.
_CJK_LINK_BOUNDARIES = frozenset(
    "\uff0c\u3002\uff01\uff1f\uff1b\uff1a\u3001\u2026\u2025\uff0e\uff61\uff64"
    "\uff08\uff09\u3010\u3011\u300a\u300b\u300c\u300d\u300e\u300f"
    "\uff3b\uff3d\uff5b\uff5d\u3008\u3009\u3014\u3015\u3016\u3017"
    "\u3018\u3019\u301a\u301b\uff62\uff63\u301d\u301e\u301f\uff5f\uff60"
    "\u201c\u201d\u2018\u2019\uff02\uff07\uff1c\uff1e"
    "\ufe10\ufe11\ufe12\ufe13\ufe14\ufe15\ufe16\ufe17\ufe18\ufe19\ufe30"
    "\ufe35\ufe36\ufe37\ufe38\ufe39\ufe3a\ufe3b\ufe3c\ufe3d\ufe3e"
    "\ufe3f\ufe40\ufe41\ufe42\ufe43\ufe44\ufe47\ufe48"
    "\ufe50\ufe51\ufe52\ufe54\ufe55\ufe56\ufe57"
    "\ufe59\ufe5a\ufe5b\ufe5c\ufe5d\ufe5e\ufe64\ufe65"
)


HEADING_STYLES = {
    1: "virtualized-markdown--h1",
    2: "virtualized-markdown--h2",
    3: "virtualized-markdown--h3",
    4: "virtualized-markdown--h4",
    5: "virtualized-markdown--h5",
    6: "virtualized-markdown--h6",
}


def _is_hex_pair(text: str) -> bool:
    """Whether *text* is exactly two hexadecimal digits."""
    return len(text) == 2 and all(char in "0123456789abcdefABCDEF" for char in text)


def _raw_boundary_index(display: str, raw: str, start: int) -> tuple[int | None, int, bool]:
    """Align linkified display text against its raw source spelling.

    Walks *display* and ``raw[start:]`` in lockstep.  A display character that
    the source spells as a percent escape, an HTML entity, or a backslash
    escape was explicitly written into the construct and is never treated as a
    prose boundary.  Returns ``(boundary, end, complete)``: *boundary* is the
    display index of the first literally-written CJK boundary character
    (``None`` when every one is escaped), *end* is the raw index one past the
    aligned text, and *complete* is False when the walk stopped early because
    ``raw`` stopped spelling *display* — normalization this walk cannot model,
    such as a punycode host rendered decoded.  ``raw`` is always the
    construct's own stamped source, so a boundary found before that point was
    aligned lockstep against it and remains trustworthy; only positions after
    the stop are unknown.
    """
    boundary: int | None = None
    raw_index = start
    display_index = 0
    while display_index < len(display):
        char = display[display_index]
        if raw_index >= len(raw):
            return boundary, raw_index, False
        # ``urllib.parse.quote`` always treats RFC 3986 unreserved ASCII as
        # safe, even when ``safe`` is empty.  Build the byte spelling directly
        # so source escapes such as ``%41`` and ``%2D`` align as decoded text.
        encoded = "".join(f"%{byte:02X}" for byte in char.encode())
        if (
            len(encoded) > 1
            and raw[raw_index : raw_index + len(encoded)].upper() == encoded.upper()
            and display[display_index : display_index + len(encoded)] != raw[raw_index : raw_index + len(encoded)]
        ):
            # Percent-encoded in the source: written intent, never a boundary.
            # If the display also spells the same ``%XX`` text, linkify kept it
            # verbatim rather than decoding it (notably ``%25``).
            raw_index += len(encoded)
            display_index += 1
            continue
        if (
            char == "%"
            and raw[raw_index] == "%"
            and display[display_index + 1 : display_index + 3].upper() == raw[raw_index + 1 : raw_index + 3].upper()
            and _is_hex_pair(display[display_index + 1 : display_index + 3])
        ):
            # An escape the renderer kept verbatim but re-emitted in uppercase
            # (``%2c`` displays as ``%2C``); consume the whole triple on both
            # sides.  Same-case triples take this branch too, equivalently.
            raw_index += 3
            display_index += 3
            continue
        if (
            raw[raw_index] == "&"
            and (semi := raw.find(";", raw_index + 1, raw_index + 40)) > 0
            and html.unescape(raw[raw_index : semi + 1]) == char
            and display[display_index : display_index + semi + 1 - raw_index] != raw[raw_index : semi + 1]
        ):
            # An HTML entity or numeric character reference the renderer decoded
            # (linkify keeps entities verbatim — then display spells the same
            # ``&...;`` and the literal branch below must consume it instead).
            raw_index = semi + 1
            display_index += 1
            continue
        if (
            raw[raw_index] == "\\"
            and raw_index + 1 < len(raw)
            and raw[raw_index + 1] == char
            and char.isascii()
            and not char.isalnum()
        ):
            # A backslash escape (CommonMark escapes ASCII punctuation only).
            raw_index += 2
            display_index += 1
            continue
        if raw[raw_index] == char:
            if boundary is None and char in _CJK_LINK_BOUNDARIES:
                boundary = display_index
            raw_index += 1
            display_index += 1
            continue
        return boundary, raw_index, False
    return boundary, raw_index, True


def _path_start(url: str) -> int:
    """The index where a URL's path/query/fragment begins (its length if none)."""
    if url.startswith("//"):
        # Protocol-relative: the authority follows immediately.
        index = 2
    else:
        scheme_end = url.find("://")
        index = scheme_end + 3 if scheme_end >= 0 else 0
    while index < len(url) and url[index] not in "/?#":
        index += 1
    return index


def _boundary_past_authority(display: str, raw: str) -> int | None:
    """Fallback boundary alignment that skips the authority component.

    A punycode host is spelled ``xn--…`` in the source but rendered decoded
    (``例え.jp``), so the lockstep walk cannot align it.  IDNA labels cannot
    contain CJK punctuation, so no prose boundary is lost by fast-forwarding
    both sides to where the path begins and walking only the remainder.
    """
    display_start = _path_start(display)
    boundary, _, _ = _raw_boundary_index(display[display_start:], raw, _path_start(raw))
    return None if boundary is None else display_start + boundary


def _inline_linkify_with_source(state: StateInline, silent: bool) -> bool:
    """Run markdown-it's inline linkifier and retain the exact source spelling."""
    start = state.pos
    token_count = len(state.tokens)
    scheme_match = SCHEME_RE.search(state.pending)
    matched = _markdown_it_inline_linkify(state, silent)
    if not matched or silent or scheme_match is None:
        return matched

    source = state.src[start - len(scheme_match.group(1)) : state.pos]
    for token in state.tokens[token_count:]:
        if token.type == "link_open" and token.markup == "linkify":
            token.meta[_LINKIFY_SOURCE_META] = source
            break
    return matched


def _core_linkify_sources(state: StateCore, tokens: list[Token]) -> list[str]:
    """Collect the raw matches markdown-it's core linkifier will replace."""
    linkify = state.md.linkify
    if linkify is None:
        return []
    html_link_level = 0
    matches_by_position: list[tuple[int, list[str]]] = []
    index = len(tokens)
    while index >= 1:
        index -= 1
        current = tokens[index]

        if current.type == "link_close":
            index -= 1
            while tokens[index].level != current.level and tokens[index].type != "link_open":
                index -= 1
            continue

        if current.type == "html_inline":
            if isLinkOpen(current.content) and html_link_level > 0:
                html_link_level -= 1
            if isLinkClose(current.content):
                html_link_level += 1
        if html_link_level > 0 or current.type != "text" or not linkify.test(current.content):
            continue

        links = linkify.match(current.content) or []
        if links and links[0].index == 0 and index > 0 and tokens[index - 1].type == "text_special":
            links = links[1:]
        sources = [link.text for link in links if state.md.validateLink(state.md.normalizeLink(link.url))]
        if sources:
            matches_by_position.append((index, sources))

    return [source for _, sources in reversed(matches_by_position) for source in sources]


def _core_linkify_with_source(state: StateCore) -> None:
    """Run markdown-it's core linkifier and annotate its generated links."""
    sources_by_inline = [
        (inline_token, _core_linkify_sources(state, inline_token.children))
        for inline_token in state.tokens
        if (
            inline_token.type == "inline"
            and state.md.linkify
            and state.md.linkify.pretest(inline_token.content)
            and inline_token.children is not None
        )
    ]

    _markdown_it_core_linkify(state)

    for inline_token, sources in sources_by_inline:
        source_index = 0
        for token in inline_token.children or []:
            if (
                token.type == "link_open"
                and token.markup == "linkify"
                and _LINKIFY_SOURCE_META not in token.meta
                and source_index < len(sources)
            ):
                token.meta[_LINKIFY_SOURCE_META] = sources[source_index]
                source_index += 1


def _fence_closing_line(state: StateBlock, line: int, markup: str) -> bool:
    """Whether *line* is the closing marker consumed for a fence token."""
    if line < 0 or line >= len(state.bMarks) or not markup:
        return False

    position = state.bMarks[line] + state.tShift[line]
    maximum = state.eMarks[line]
    if position >= maximum or state.is_code_block(line) or state.src[position] != markup[0]:
        return False

    marker_end = state.skipCharsStr(position, markup[0])
    if marker_end - position < len(markup):
        return False
    return state.skipSpaces(marker_end) >= maximum


def _block_fence_with_closed_meta(state: StateBlock, start_line: int, end_line: int, silent: bool) -> bool:
    """Run markdown-it's fence rule and retain whether it found an end marker."""
    token_count = len(state.tokens)
    matched = _markdown_it_block_fence(state, start_line, end_line, silent)
    if not matched or silent or len(state.tokens) == token_count:
        return matched

    token = state.tokens[-1]
    token.meta[_FENCE_CLOSED_META] = bool(
        token.map is not None and _fence_closing_line(state, token.map[1] - 1, token.markup)
    )
    return matched


def _installed_rule_fn(ruler: Ruler[Any], name: str) -> Callable[..., Any] | None:
    """The function currently installed for rule *name* (``None`` when absent)."""
    for rule in ruler.__rules__:
        if rule.name == name:
            return rule.fn
    return None


def _replace_rule_fn(ruler: Ruler[Any], name: str, fn: Callable[..., Any]) -> None:
    """Swap rule *name*'s function, keeping the rules it may interrupt.

    ``Ruler.at`` resets ``alt`` unless given one; a fence that lost its
    ``alt`` no longer interrupts a paragraph, so a code block right after a
    line of text would parse as that paragraph's text.
    """
    alt = next(list(rule.alt) for rule in ruler.__rules__ if rule.name == name)
    ruler.at(name, fn, {"alt": alt})


def _configure_markdown_parser(parser: MarkdownIt) -> MarkdownIt:
    """Instrument a MarkdownIt parser to preserve raw auto-link spellings.

    Only the stock linkify rules are replaced, with wrappers that delegate to
    them.  A caller-supplied parser factory may have installed custom linkify
    rules; the source-stamping wrappers are semantically valid only around the
    stock behavior they mirror, so custom rules are left in place and their
    links simply render without boundary splitting.
    """
    if _installed_rule_fn(parser.inline.ruler, "linkify") is _markdown_it_inline_linkify:
        _replace_rule_fn(parser.inline.ruler, "linkify", _inline_linkify_with_source)
    if _installed_rule_fn(parser.core.ruler, "linkify") is _markdown_it_core_linkify:
        _replace_rule_fn(parser.core.ruler, "linkify", _core_linkify_with_source)
    if _installed_rule_fn(parser.block.ruler, "fence") is _markdown_it_block_fence:
        _replace_rule_fn(parser.block.ruler, "fence", _block_fence_with_closed_meta)
    return parser


def _restore_ipv6_authority(url: str) -> str:
    """Restore IPv6 authority brackets in an already normalized destination."""
    # mdurl percent-encodes IPv6 brackets as if they were path characters.
    # Restore only authority brackets; encoded path/query data stays intact.
    return re.sub(
        r"^(https?://(?:[^/?#]*@)?)%5B([^/?#]+)%5D(?=[:/?#]|$)",
        r"\1[\2]",
        url,
        flags=re.IGNORECASE,
    )


def _restore_ipv6_link_targets(state: StateCore) -> None:
    """Repair normalized link and image targets after the linkify rules run."""
    for token in state.tokens:
        for child in token.children or []:
            if child.type not in {"link_open", "image"}:
                continue
            attribute = "href" if child.type == "link_open" else "src"
            if isinstance(target := child.attrGet(attribute), str):
                child.attrSet(attribute, _restore_ipv6_authority(target))


def _html_line_break(state: StateInline, silent: bool) -> bool:
    """Read ``<br>`` as a line break, the one HTML tag with a meaning in plain text.

    Models write it for line breaks inside table cells. Like a newline, it
    takes the spaces around it and the newline after it, so ``one<br>``
    followed by a new line breaks once, not twice. One that ends the text
    breaks nothing, as in a browser, instead of adding an empty row.
    """
    if state.src[state.pos] != "<":
        return False
    match = _HTML_LINE_BREAK.match(state.src, state.pos, state.posMax)
    if match is None:
        return False
    if not silent:
        state.pending = state.pending.rstrip(" ")
        if match.end() < state.posMax:
            state.push("hardbreak", "br", 0)
    state.pos = match.end()
    return True


def _literal_user_backslash(state: StateInline, silent: bool) -> bool:
    """Keep a typed backslash before punctuation or a newline, so a path reads as typed.

    It still does the escape's other job: the punctuation after it is plain
    text, so ``\\*`` starts no emphasis. Before a newline the backslash stays
    and the newline breaks the line as any other does.
    """
    pos = state.pos
    if state.src[pos] != "\\" or pos + 1 >= state.posMax:
        return False
    escaped = state.src[pos + 1]
    if escaped == "\n":
        if not silent:
            state.pending += "\\"
        state.pos = pos + 1
        return True
    if escaped not in string.punctuation:
        return False
    if not silent:
        state.pending += "\\" + escaped
    state.pos = pos + 2
    return True


def _create_markdown_parser() -> MarkdownIt:
    """Create the default Markdown parser used by the TUI.

    HTML stays text: the renderer draws none, and ``List<String>`` or
    ``<file>`` mean those characters, so reading them as tags would drop them
    along with the text an HTML block holds. Only ``<br>`` breaks the line.
    """
    parser = MarkdownIt("gfm-like", {"html": False})
    enable_math(parser)
    parser.inline.ruler.before("html_inline", _HTML_LINE_BREAK_RULE, _html_line_break)
    parser.core.ruler.after("linkify", "chrys_ipv6_links", _restore_ipv6_link_targets)
    if parser.linkify is not None:
        # Bare filenames and identifiers can also be valid domain names.
        # Only explicit HTTP(S) prefixes opt ordinary prose into auto-linking.
        parser.linkify.set({"fuzzy_link": False, "fuzzy_email": False, "fuzzy_ip": False})
        for scheme in ("ftp:", "mailto:", "//"):
            parser.linkify.add(scheme, None)
    return _configure_markdown_parser(parser)


type InlineCache = dict[str, list[Token]]


def _parse_streaming_markdown(parser: MarkdownIt, source: str, cache: InlineCache) -> list[Token]:
    """Parse one snapshot with a fresh default parser, reusing the previous snapshot's inline children."""
    # The block pass resolves document-wide structure and link definitions. Between it and
    # math cleanup, these specific core rules process each inline independently. A new
    # rule may need the entire document (e.g. for footnotes).
    if parser.core.ruler.get_active_rules() != [
        "normalize",
        "block",
        "inline",
        "linkify",
        "chrys_ipv6_links",
        "text_join",
        "chrys_math_cleanup",
    ]:
        cache.clear()
        return parser.parse(source)

    document: list[Token] | None = None

    def select_changed(state: StateCore) -> None:
        nonlocal document
        if state.env.get("references"):
            # Even an unchanged paragraph can acquire or lose a link target.
            cache.clear()
            return
        document = state.tokens
        pending = []
        for token in document:
            if token.type == "inline":
                children = cache.get(token.content)
                if children is None:
                    pending.append(token)
                else:
                    token.children = children
        state.tokens = pending

    def restore_document(state: StateCore) -> None:
        if document is not None:
            state.tokens = document
            cache.clear()
            cache.update((t.content, t.children) for t in document if t.type == "inline" and t.children is not None)

    parser.core.ruler.before("inline", "chrys_changed_inlines", select_changed)
    parser.core.ruler.after("text_join", "chrys_restore_blocks", restore_document)
    return parser.parse(source)


def _hard_line_breaks(state: StateCore) -> None:
    for token in state.tokens:
        for child in token.children or []:
            if child.type == "softbreak":
                child.type = "hardbreak"


def create_line_break_markdown_parser() -> MarkdownIt:
    """The default parser, keeping each newline inside a paragraph as a line break."""
    parser = _create_markdown_parser()
    parser.core.ruler.push("chrys_hard_line_breaks", _hard_line_breaks)
    return parser


def create_user_text_markdown_parser() -> MarkdownIt:
    """The line-break parser for text a person typed, where ``<br>`` and backslashes stay text too.

    A person typing ``<br>`` means those characters, as with any other tag, and
    a backslash is usually part of a Windows path (``D:\\project\\.code``), so it
    shows as typed (``_literal_user_backslash``).
    """
    parser = create_line_break_markdown_parser()
    parser.inline.ruler.disable(_HTML_LINE_BREAK_RULE)
    parser.inline.ruler.before("escape", _LITERAL_USER_BACKSLASH_RULE, _literal_user_backslash)
    disable_math(parser)
    return parser


def _token_to_content(
    token: Token,
    *,
    open_links: bool = True,
    math_compiler: Callable[[str], CompiledMath] = compile_math,
) -> Content:
    """Convert an inline token to Textual Content.

    Args:
        token: A markdown token.
        open_links: Emit terminal hyperlinks for allowed external destinations.

    Returns:
        Content instance.
    """
    if token.children is None:
        return Content("")

    tokens: list[str] = []
    spans: list[Span] = []
    style_stack: list[tuple[Style | str, int]] = []
    position: int = 0
    split_linkified_text: tuple[str, str] | None = None
    skip_link_close = False

    def add_content(text: str) -> None:
        nonlocal position
        tokens.append(text)
        position += len(text)

    def add_style(style: Style | str) -> None:
        style_stack.append((style, position))

    def close_tag() -> None:
        style, start = style_stack.pop()
        spans.append(Span(start, position, style))

    for child_index, child in enumerate(token.children):
        child_type = child.type
        if child_type == "text":
            if split_linkified_text is None:
                add_content(re.sub(r"\s+", " ", child.content))
            else:
                link_text, suffix = split_linkified_text
                add_content(link_text)
                close_tag()
                add_content(suffix)
                split_linkified_text = None
                skip_link_close = True
        if child_type == "hardbreak":
            add_content("\n")
        if child_type == "softbreak":
            add_content(" ")
        elif child_type == "code_inline":
            add_style(".code_inline")
            add_content(child.content)
            close_tag()
        elif child_type == "math_inline":
            formula = math_compiler(child.content)
            add_content(formula.linear if formula.rows else child.meta["math_source"])
        elif child_type == "math_literal":
            add_content(child.content)
        elif child_type == "em_open":
            add_style(".em")
        elif child_type == "strong_open":
            add_style(".strong")
        elif child_type == "s_open":
            add_style(".s")
        elif child_type == "link_open":
            href = cast("str", child.attrs.get("href", ""))
            if child.markup == "linkify" and child_index + 1 < len(token.children):
                next_child = token.children[child_index + 1]
                if next_child.type == "text" and not _CJK_LINK_BOUNDARIES.isdisjoint(next_child.content):
                    display = next_child.content
                    boundary = None
                    raw_source = child.meta.get(_LINKIFY_SOURCE_META)
                    if isinstance(raw_source, str):
                        boundary, _, complete = _raw_boundary_index(display, raw_source, 0)
                        if boundary is None and not complete:
                            # A punycode host renders decoded and defeats the
                            # lockstep walk before any literal boundary was
                            # seen; retry from where the path begins.
                            boundary = _boundary_past_authority(display, raw_source)
                    if boundary is not None:
                        link_text = display[:boundary]
                        suffix = display[boundary:]
                        # Truncate the href at the occurrence matching the display
                        # boundary: earlier occurrences may be explicitly encoded
                        # URL content that must be preserved.
                        encoded_boundary = quote(display[boundary], safe="").upper()
                        occurrence = link_text.count(display[boundary]) + 1
                        href_upper = href.upper()
                        href_boundary = -1
                        for _ in range(occurrence):
                            href_boundary = href_upper.find(encoded_boundary, href_boundary + 1)
                            if href_boundary < 0:
                                break
                        href = href[:href_boundary] if href_boundary >= 0 else link_text
                        split_linkified_text = (link_text, suffix)
            action = f"link({href!r})"
            target = terminal_link_target(href) if open_links else None
            # None inherits a nested image's link; an empty link explicitly clears it.
            add_style(Style(underline=True, link=target or "") + Style.from_meta({"@click": action}))
        elif child_type == "image":
            href = cast("str", child.attrs.get("src", ""))
            alt = child.attrs.get("alt", "")
            action = f"link({href!r})"
            target = terminal_link_target(href) if open_links else None
            add_style(Style(underline=True, link=target or "") + Style.from_meta({"@click": action}))
            add_content("\U0001f5bc  ")
            if alt:
                add_content(f"({alt})")
            if child.children is not None:
                for grandchild in child.children:
                    add_content(grandchild.content)
            close_tag()
        elif child_type.endswith("_close"):
            if child_type == "link_close" and skip_link_close:
                skip_link_close = False
            else:
                close_tag()

    content = Content("".join(tokens), spans=spans)
    return content


_FENCE_GUESS_SAMPLE_CHARS = 2048
"""An unlabeled code block guesses its language from at most this much leading code.

Pygments' ``guess_lexer`` scores every lexer's ``analyse_text`` over its whole
input: on a long bare fence (a log, command output) that is hundreds of
milliseconds of GIL-held time on every parse of the message. The deciding
signals (shebangs, doctypes, leading keywords) sit at the top, and a block
that fits in the sample is guessed exactly as before.
"""


def _guess_code_language(code: str) -> str:
    """Return the lexer name for an unlabeled code block, guessed from a line-aligned prefix."""
    if len(code) > _FENCE_GUESS_SAMPLE_CHARS:
        cut = code.rfind("\n", 0, _FENCE_GUESS_SAMPLE_CHARS)
        code = code[: cut if cut > 0 else _FENCE_GUESS_SAMPLE_CHARS]
    return guess_language(code, None)


def highlight_code(code: str, language: str) -> Content:
    """Syntax-highlight a code block, guessing its language when the fence names none."""
    return highlight(code, language=language or _guess_code_language(code), theme=NoErrorHighlightTheme)


def _get_list_indent(stack: list[dict]) -> int:
    """Get the indent from the nearest list_item in the stack."""
    for parent in reversed(stack):
        if parent["type"] == "list_item":
            return parent.get("indent", 0)
    return 0


def _apply_containers(block: MarkdownBlock, stack: list[dict]) -> None:
    """Preserve container order for every block, including non-paragraph items."""
    gutter: list[MarkdownGutter] = []
    indent = 0
    for parent in stack:
        if parent["type"] == "blockquote":
            gutter.append(MarkdownGutter(parent["container_id"], "quote", 2))
        elif parent["type"] == "list_item":
            prefix = "" if parent["first_block_done"] else parent["prefix"]
            parent["first_block_done"] = True
            gutter.append(MarkdownGutter(parent["container_id"], "list", parent["indent"] - indent, prefix))
            indent = parent["indent"]
    if not gutter:
        return
    block.gutter = tuple(gutter)
    block.indent = indent
    block.bq_depth = sum(part.kind == "quote" for part in gutter)
    block.border_left = "▌ " * block.bq_depth
    block.prefix = next((part.prefix for part in reversed(gutter) if part.prefix), "")


@dataclass(slots=True)
class StreamingBlockCache:
    """The last streaming snapshot, indexed at independent top-level token boundaries."""

    tokens: list[Token] = field(default_factory=list)
    blocks: list[MarkdownBlock] = field(default_factory=list)
    boundaries: list[tuple[int, int]] = field(default_factory=list)
    reused: int = 0
    context: tuple[str, bool] | None = None

    def matching_boundary(self, tokens: list[Token]) -> tuple[int, int]:
        matching = min(len(tokens), len(self.tokens))
        for index, (old, new) in enumerate(zip(self.tokens, tokens, strict=False)):
            if old != new:
                matching = index
                break
        return next((boundary for boundary in reversed(self.boundaries) if boundary[0] <= matching), (0, 0))


def _parse_tokens(
    tokens: Iterable[Token],
    unhandled_token: Callable[[Token], MarkdownBlock | None] | None = None,
    compile_diagram: Callable[[str], CompiledDiagram] | None = None,
    *,
    open_links: bool = True,
    math_enabled: bool = True,
    math_compiler: Callable[[str], CompiledMath] = compile_math,
    highlight_closed_fence: Callable[[str, str], Content] = highlight_code,
    streaming: StreamingBlockCache | None = None,
) -> list[MarkdownBlock]:
    """Parse markdown-it tokens into a flat list of MarkdownBlock objects.

    Args:
        tokens: Iterable of markdown-it tokens.
        unhandled_token: Optional callback for unhandled token types.
        open_links: Emit terminal hyperlinks for allowed external destinations.
        highlight_closed_fence: Highlights the code of a closed fence, which
            streamed reparses leave unchanged; other code blocks use ``highlight_code``.

    Returns:
        A list of MarkdownBlock data objects.
    """
    blocks: list[MarkdownBlock] = []
    boundaries: list[tuple[int, int]] = []
    start = 0
    document: list[Token] = []
    if streaming is not None:
        document = tokens if isinstance(tokens, list) else list(tokens)
        start, streaming.reused = streaming.matching_boundary(document)
        blocks = streaming.blocks[: streaming.reused]
        boundaries = [boundary for boundary in streaming.boundaries if boundary[0] < start]
        tokens = document[start:]

    stack: list[dict] = []
    list_stack: list[dict] = []

    def append_block(block: MarkdownBlock) -> None:
        _apply_containers(block, stack)
        blocks.append(block)

    for token_index, token in enumerate(tokens, start):
        if streaming is not None and not stack and (not blocks or blocks[-1].bottom_margin == 1):
            # A later quote may give the preceding block a margin. Keep
            # boundaries only where the prefix is already independent of it.
            boundaries.append((token_index, len(blocks)))
        token_type = token.type
        source_range = (token.map[0], token.map[1]) if token.map is not None else (0, 0)

        if token_type == "heading_open":
            level = int(token.tag[1])
            stack.append(
                {
                    "type": "heading",
                    "level": level,
                    "source_range": source_range,
                }
            )

        elif token_type == "heading_close":
            ctx = stack.pop()
            content = ctx.get("content", Content(""))
            slug_text = content.plain
            block_id = f"heading-{slug_for_tcss_id(slug_text)}"

            top_margin = 2 if ctx["level"] <= 2 else 1
            append_block(
                MarkdownBlock(
                    block_type="heading",
                    content=content,
                    level=ctx["level"],
                    block_id=block_id,
                    source_range=ctx["source_range"],
                    style_name=HEADING_STYLES.get(ctx["level"], "virtualized-markdown--h1"),
                    top_margin=top_margin,
                    bottom_margin=1,
                    text_align="center" if ctx["level"] == 1 else "left",
                )
            )

        elif token_type == "paragraph_open":
            stack.append({"type": "paragraph", "source_range": source_range})

        elif token_type == "paragraph_close":
            ctx = stack.pop()
            content = ctx.get("content", Content(""))
            append_block(
                MarkdownBlock(
                    block_type="paragraph",
                    content=content,
                    source_range=ctx["source_range"],
                    style_name="virtualized-markdown--paragraph",
                    top_margin=0 if token.hidden else 1,
                    bottom_margin=0 if token.hidden else 1,
                )
            )

        elif token_type == "blockquote_open":
            if blocks and blocks[-1].bottom_margin < 1:
                blocks[-1].bottom_margin = 1
            stack.append(
                {
                    "type": "blockquote",
                    "container_id": token_index,
                    "source_range": source_range,
                    "block_count_at_open": len(blocks),
                }
            )

        elif token_type == "blockquote_close":
            if len(blocks) == stack[-1]["block_count_at_open"]:
                append_block(MarkdownBlock("paragraph", Content(" "), bottom_margin=0))
            stack.pop()
            if blocks and blocks[-1].bottom_margin != 1:
                blocks[-1].bottom_margin = 1

        elif token_type == "bullet_list_open":
            depth = sum(1 for s in list_stack if s["type"] in ("bullet_list", "ordered_list"))
            list_stack.append(
                {
                    "type": "bullet_list",
                    "depth": depth,
                    "item_count": 0,
                }
            )
            stack.append({"type": "bullet_list", "source_range": source_range})

        elif token_type == "bullet_list_close":
            list_stack.pop()
            stack.pop()
            if not list_stack and blocks and blocks[-1].bottom_margin != 1:
                blocks[-1].bottom_margin = 1

        elif token_type == "ordered_list_open":
            depth = sum(1 for s in list_stack if s["type"] in ("bullet_list", "ordered_list"))
            list_stack.append(
                {
                    "type": "ordered_list",
                    "depth": depth,
                    "item_count": 0,
                    "start": int(token.attrGet("start") or 1),
                }
            )
            stack.append({"type": "ordered_list", "source_range": source_range})

        elif token_type == "ordered_list_close":
            list_stack.pop()
            stack.pop()
            if not list_stack and blocks and blocks[-1].bottom_margin != 1:
                blocks[-1].bottom_margin = 1

        elif token_type == "list_item_open":
            if not list_stack:
                continue
            list_ctx = list_stack[-1]
            list_ctx["item_count"] += 1

            depth = list_ctx["depth"]
            if list_ctx["type"] == "bullet_list":
                bullet_idx = depth % len(BULLETS)
                prefix = BULLETS[bullet_idx]
            else:
                number = list_ctx["start"] + list_ctx["item_count"] - 1
                prefix = f"{number}. "

            indent = _get_list_indent(stack) + max(4 if depth == 0 else 2, cell_len(prefix))
            stack.append(
                {
                    "type": "list_item",
                    "container_id": token_index,
                    "indent": indent,
                    "prefix": prefix,
                    "first_block_done": False,
                    "source_range": source_range,
                }
            )

        elif token_type == "list_item_close":
            if not stack[-1]["first_block_done"]:
                append_block(MarkdownBlock("paragraph", Content(" "), bottom_margin=0))
            stack.pop()

        elif token_type == "hr":
            append_block(
                MarkdownBlock(
                    block_type="hr",
                    content=Content(""),
                    source_range=source_range,
                    style_name="virtualized-markdown--hr",
                    top_margin=2,
                    bottom_margin=1,
                )
            )

        elif token_type == "table_open":
            stack.append(
                {
                    "type": "table",
                    "headers": [],
                    "rows": [],
                    "current_row": [],
                    "in_head": False,
                    "source_range": source_range,
                }
            )

        elif token_type == "thead_open":
            if stack and stack[-1]["type"] == "table":
                stack[-1]["in_head"] = True

        elif token_type == "thead_close":
            if stack and stack[-1]["type"] == "table":
                stack[-1]["in_head"] = False

        elif token_type == "tbody_open" or token_type == "tbody_close":
            pass

        elif token_type == "tr_open":
            if stack and stack[-1]["type"] == "table":
                stack[-1]["current_row"] = []

        elif token_type == "tr_close":
            if stack and stack[-1]["type"] == "table":
                if stack[-1]["in_head"]:
                    stack[-1]["headers"] = stack[-1]["current_row"][:]
                else:
                    stack[-1]["rows"].append(stack[-1]["current_row"][:])
                stack[-1]["current_row"] = []

        elif token_type in ("th_open", "td_open"):
            stack.append({"type": "table_cell"})

        elif token_type in ("th_close", "td_close"):
            ctx = stack.pop()
            content = ctx.get("content", Content(""))
            for parent in reversed(stack):
                if parent["type"] == "table":
                    parent["current_row"].append(content)
                    break

        elif token_type == "table_close":
            ctx = stack.pop()
            headers = ctx.get("headers", [])
            rows = ctx.get("rows", [])
            for block in _build_table_blocks(headers, rows, ctx["source_range"]):
                append_block(block)

        elif token_type == "inline":
            if stack:
                content = _token_to_content(token, open_links=open_links, math_compiler=math_compiler)
                stack[-1]["content"] = content

        elif token_type in ("fence", "code_block", "math_block"):
            code = token.content.rstrip()
            language = token.info or ""
            normalized_language = language.split(maxsplit=1)[0].casefold() if language.strip() else ""

            if math_enabled and (
                token_type == "math_block"
                or (
                    token_type == "fence"
                    and normalized_language == "math"
                    and token.meta.get(_FENCE_CLOSED_META, False)
                )
            ):
                formula = math_compiler(token.content.strip())
                source = token.meta.get("math_source", token.content.rstrip("\n"))
                append_block(
                    MarkdownBlock(
                        block_type="math",
                        content=Content.from_text(source, markup=False),
                        source_range=source_range,
                        math=formula,
                        style_name="virtualized-markdown--fence",
                        top_margin=1,
                        padding_left=2,
                        padding_right=2,
                    )
                )
                continue

            if token_type == "fence" and normalized_language == "mermaid" and token.meta.get(_FENCE_CLOSED_META, False):
                if compile_diagram is None:
                    from chrys.app.tui.widgets.markdown.diagram import compile_mermaid

                    compile_diagram = compile_mermaid
                diagram_source = token.content.rstrip("\n")
                diagram = compile_diagram(diagram_source)

                if not any(diagnostic.severity is DiagnosticSeverity.ERROR for diagnostic in diagram.diagnostics):
                    append_block(
                        MarkdownBlock(
                            block_type="diagram",
                            content=Content.from_text("", markup=False),
                            source_range=source_range,
                            style_name="virtualized-markdown--diagram",
                            top_margin=1,
                            bottom_margin=1,
                            code_language="mermaid",
                            padding_top=1,
                            padding_bottom=1,
                            padding_left=1,
                            padding_right=1,
                            diagram=diagram,
                        )
                    )
                    continue

            if token_type == "fence" and token.meta.get(_FENCE_CLOSED_META, False):
                highlighted = highlight_closed_fence(code, language)
            else:
                highlighted = highlight_code(code, language)

            append_block(
                MarkdownBlock(
                    block_type="fence",
                    content=highlighted,
                    source_range=source_range,
                    style_name="virtualized-markdown--fence",
                    top_margin=1,
                    bottom_margin=1,
                    code_language=language,
                    padding_top=1,
                    padding_bottom=1,
                    padding_left=2,
                    padding_right=1,
                )
            )

        elif unhandled_token is not None:
            external = unhandled_token(token)
            if external is not None:
                append_block(external)

    if streaming is not None:
        if not stack and (not blocks or blocks[-1].bottom_margin == 1):
            boundaries.append((len(document), len(blocks)))
        streaming.tokens = document
        streaming.blocks = blocks
        streaming.boundaries = boundaries
    return blocks


def _build_table_blocks(
    headers: list[Content],
    rows: list[list[Content]],
    source_range: tuple[int, int],
) -> list[MarkdownBlock]:
    """Build MarkdownBlock objects for a table.

    Stores raw table data; actual rendering is done at layout time
    when the available width is known.
    """
    if not headers:
        return []

    return [
        MarkdownBlock(
            block_type="table",
            content=Content(""),
            source_range=source_range,
            style_name="virtualized-markdown--table",
            bottom_margin=1,
            table_headers=headers,
            table_rows=rows,
        )
    ]


def _wrap_cell_text(text: str, width: int) -> list[str]:
    """Wrap text to fit within width cells (CJK-aware).

    Args:
        text: The text to wrap.
        width: Maximum cell width per line.

    Returns:
        A list of wrapped lines.
    """
    if width <= 0:
        return [text]
    if text.isascii() and "\t" not in text:
        result: list[str] = []
        for paragraph in text.split("\n"):
            if paragraph:
                result.extend(paragraph[index : index + width] for index in range(0, len(paragraph), width))
            else:
                result.append("")
        return result or [""]
    result: list[str] = []
    for paragraph in text.split("\n"):
        current = ""
        for char in paragraph:
            candidate = current + char
            if cell_len(candidate) > width and current and not _is_cluster_continuation(current, char):
                result.append(current)
                current = char
            else:
                current = candidate
        result.append(current)
    return result or [""]


def _cell_wrapped_height(text: str, width: int) -> int:
    """Return the number of visual rows ``_wrap_cell_text`` would produce."""
    if width <= 0:
        return 1
    if text.isascii() and "\t" not in text:
        return sum(max(1, (len(paragraph) + width - 1) // width) for paragraph in text.split("\n"))
    height = 0
    for paragraph in text.split("\n"):
        current_chars: list[str] = []
        current_width = 0
        previous_char = ""
        for char in paragraph:
            continuation = char == _ZERO_WIDTH_JOINER or previous_char == _ZERO_WIDTH_JOINER or cell_len(char) == 0
            if continuation:
                current_chars.append(char)
                current_width = cell_len("".join(current_chars))
                previous_char = char
                continue

            char_width = cell_len(char)
            if current_width + char_width > width and current_chars:
                height += 1
                current_chars = [char]
                current_width = char_width
            else:
                current_chars.append(char)
                current_width += char_width
            previous_char = char
        height += 1
    return max(1, height)


def _is_cluster_continuation(current: str, char: str) -> bool:
    """Return whether ``char`` should stay attached to the current cluster."""
    return char == _ZERO_WIDTH_JOINER or current.endswith(_ZERO_WIDTH_JOINER) or cell_len(char) == 0


def _cell_natural_width(text: str) -> int:
    """Return the width a cell needs unwrapped: its widest line (``<br>`` splits lines)."""
    return max(cell_len(line) for line in text.split("\n"))


def _cell_min_width(text: str) -> int:
    """Return the narrowest width that can keep each display cluster intact."""
    if text.isascii():
        return 1
    max_width = 1
    cluster = ""
    for char in text:
        if cluster and not _is_cluster_continuation(cluster, char):
            max_width = max(max_width, cell_len(cluster))
            cluster = char
        else:
            cluster += char
    if cluster:
        max_width = max(max_width, cell_len(cluster))
    return max_width


def _cell_prefix_length(text: str, target_cells: int) -> int:
    """Return the character count that fits without splitting a wide glyph.

    When the text is wider than the target — e.g. a double-width CJK glyph in a
    column the layout squeezed narrower than the glyph — only the whole leading
    characters that fit are kept; the table renderer pads the remainder.  A cell
    that renders even one cell wide of its column shifts every separator to its
    right (the misaligned-bars bug).
    """
    if target_cells <= 0:
        return 0
    if text.isascii() and "\t" not in text:
        return min(len(text), target_cells)
    out = ""
    for char in text:
        candidate = out + char
        if cell_len(candidate) > target_cells:
            break
        out = candidate
    return len(out)
