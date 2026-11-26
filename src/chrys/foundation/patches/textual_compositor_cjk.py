# Copyright (c) 2021 Will McGugan
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# Contains code adapted from Textual (MIT License; see NOTICE).

"""Patch: fix CJK double-width character rendering corruption in Textual compositor.

Problem
-------
The compositor divides widget strips at "cut" boundaries (vertical lines where
widget edges align).  When a cut falls inside a double-width CJK character,
``strip.divide()`` replaces it with two single-width spaces.  This corrupts the
display whenever overlapping widget boundaries happen to bisect a wide character.

Partial terminal updates have a second boundary: the right edge of a dirty span.
``ChopsUpdate.render_segments()`` crops a winning strip to that edge, and
``Strip.crop()`` likewise substitutes a space when the edge lands inside a
double-width character.  The next cells are left from the previous frame, which
makes the missing character appear to blink as the underlying widget refreshes.

The left edge of a dirty span is a boundary too.  A partial update renders only
the widgets that overlap the dirty region, so the effective cuts above can merge
a strip across a cut whose other side belongs to an unrendered widget.  Such a
merged strip starts left of the span, and writing it from its start paints the
lower widget over the unrendered one: a dialog's body text becomes blank when
the sidebar beside it repaints the dialog's backdrop.

Solution
--------
Introduce "effective cuts" -- only divide strips at cuts where at least one
adjacent chop is already claimed by a higher-priority widget.  When the same
widget owns both sides of a cut, the division is skipped and the wider strip
is preserved intact.  Follower chop positions receive a ``_MERGED_STRIP``
sentinel that prevents lower-priority widgets from filling those slots.

Additionally, ``ChopsUpdate`` no longer pre-computes ``chop_ends`` from the cut
array; instead it derives each strip's end from its actual ``cell_length``, which
correctly accounts for merged (wider-than-bucket) strips.

Before the raw terminal path crops at a dirty-span edge, advance that edge to the
next complete character boundary.  This can overdraw at most one cell for the
double-width characters covered here; ZWJ grapheme sequences and zero-width
combining-mark boundaries remain outside this patch's scope.

The raw terminal path therefore starts a strip that begins left of the span at
the span's left edge, moved back to the start of a double-width character the
edge divides.  The cells it no longer writes lie outside the dirty region, so
the terminal already shows them. Widgets outside the dirty rectangle reserve their
usual space on those rows without rendering. Use full-frame row origins when
cutting a strip containing joined glyphs or symbols with emoji presentation:
terminals may assign them a different width even without a variation selector.

Upgrade warning: ``_compositor.py`` is one all-or-nothing patch group containing
these 19 CJK fragments plus one tab-selection fragment.  A mismatch in any of the
20 fragments aborts the entire file group. Textual is imported before
``bootstrap_runtime()`` on the TUI path, so the runtime patch stages the same CJK
fragments in memory and installs all affected methods on the existing classes.
It preserves imported class aliases and instances, and applies independently of
whether the file patch has already written the next-process implementation.
"""

from __future__ import annotations
import __future__

import ast
import logging
from bisect import bisect_left, bisect_right
from pathlib import Path
from typing import Any

from chrys.foundation.patches.patcher import FilePatch, register

_RUNTIME_PATCH_MARKER = "_chrys_complete_cjk_compositor"
_RUNTIME_PATCH_TEXTUAL_VERSION = "8.2.7"
logger = logging.getLogger(__name__)
_RUNTIME_TARGETS = {
    "ChopsUpdate": ("__init__", "__rich_console__", "render_segments"),
    "Compositor": ("_get_renders", "render_full_update", "render_partial_update", "render_strips", "_render_chops"),
}

# ---------------------------------------------------------------------------
# Patch 1: add bisect imports
# ---------------------------------------------------------------------------
_OLD_IMPORT = "from operator import itemgetter"

_NEW_IMPORT = """\
from bisect import bisect_left, bisect_right
from operator import itemgetter"""

# ---------------------------------------------------------------------------
# Patch 2: add _MERGED_STRIP and _ALWAYS_TRUE sentinels
# ---------------------------------------------------------------------------
_OLD_SENTINELS = """\
from textual.strip import Strip, StripRenderable
from textual.widget import Widget

if TYPE_CHECKING:"""

_NEW_SENTINELS = """\
from textual.strip import Strip, StripRenderable
from textual.widget import Widget

# Sentinel strip used to mark "merged" chop positions.
# When a higher-priority widget wins both sides of a cut, we avoid dividing its
# strip there (which would split double-width CJK characters). The leader chop
# gets the wide un-split strip; follower chops get this sentinel so that
# lower-priority widgets cannot fill them (which would corrupt the display).
_MERGED_STRIP: Strip = Strip([], 0)

_ALWAYS_TRUE: Callable[[int], bool] = lambda _: True

if TYPE_CHECKING:"""

# ---------------------------------------------------------------------------
# Patch 3: remove chop_ends from ChopsUpdate.__init__
# ---------------------------------------------------------------------------
_OLD_CHOPS_INIT = """\
    def __init__(
        self,
        chops: Sequence[Mapping[int, Strip | None]],
        spans: list[tuple[int, int, int]],
        chop_ends: list[list[int]],
    ) -> None:
        \"\"\"A renderable which updates chops (fragments of lines).

        Args:
            chops: A mapping of offsets to list of segments, per line.
            crop: Region to restrict update to.
            chop_ends: A list of the end offsets for each line
        \"\"\"
        self.chops = chops
        self.spans = spans
        self.chop_ends = chop_ends"""

_NEW_CHOPS_INIT = """\
    def __init__(
        self,
        chops: Sequence[Mapping[int, Strip | None]],
        spans: list[tuple[int, int, int]],
    ) -> None:
        \"\"\"A renderable which updates chops (fragments of lines).

        Args:
            chops: A mapping of offsets to list of segments, per line.
            spans: List of (y, x1, x2) spans to update.
        \"\"\"
        self.chops = chops
        self.spans = spans"""

# ---------------------------------------------------------------------------
# Patch 4: update ChopsUpdate.__rich_console__ loop
# ---------------------------------------------------------------------------
_OLD_RICH_CONSOLE = """\
        chop_ends = self.chop_ends
        last_y = self.spans[-1][0]

        _cell_len = cell_len
        for y, x1, x2 in self.spans:
            line = chops[y]
            ends = chop_ends[y]
            for end, (x, strip) in zip(ends, line.items()):
                # TODO: crop to x extents
                if strip is None:
                    continue

                if x > x2 or end <= x1:"""

_NEW_RICH_CONSOLE = """\
        last_y = self.spans[-1][0]

        _cell_len = cell_len
        for y, x1, x2 in self.spans:
            line = chops[y]
            for x, strip in line.items():
                # TODO: crop to x extents
                if strip is None or strip is _MERGED_STRIP:
                    # Skip unfilled chops (None) and _MERGED_STRIP sentinels.
                    continue

                # Use actual strip width rather than the nominal cut-based end so
                # that merged leader strips (wider than their nominal chop bucket)
                # are rendered correctly for double-width character preservation.
                end = x + strip.cell_length

                if x > x2 or end <= x1:"""

# ---------------------------------------------------------------------------
# Patch 5: update ChopsUpdate.render_segments loop
# ---------------------------------------------------------------------------
_OLD_RENDER_SEGMENTS = """\
        chop_ends = self.chop_ends
        last_y = self.spans[-1][0]

        for y, x1, x2 in self.spans:
            line = chops[y]
            ends = chop_ends[y]
            for end, (x, strip) in zip(ends, line.items()):
                if strip is None:
                    continue

                if x > x2 or end <= x1:"""

_NEW_RENDER_SEGMENTS = """\
        last_y = self.spans[-1][0]

        for y, x1, x2 in self.spans:
            line = chops[y]
            for x, strip in line.items():
                if strip is None or strip is _MERGED_STRIP:
                    # Skip unfilled chops (None) and _MERGED_STRIP sentinels.
                    continue

                # Use actual strip width rather than the nominal cut-based end so
                # that merged leader strips (wider than their nominal chop bucket)
                # are rendered correctly for double-width character preservation.
                end = x + strip.cell_length

                if x > x2 or end <= x1:"""

# ---------------------------------------------------------------------------
# Patch 6: keep dirty-span crops on complete character boundaries
# ---------------------------------------------------------------------------
_OLD_RENDER_SEGMENTS_CROP = """\
                strip = strip.crop(0, min(end, x2) - x)
                append(move_to(x, y).segment.text)
                append(strip.render(console))"""

_NEW_RENDER_SEGMENTS_CROP = """\
                crop_end = min(end, x2) - x
                if 0 < crop_end < strip.cell_length:
                    position = 0
                    for segment in strip:
                        segment_end = position + cell_len(segment.text)
                        if segment_end < crop_end:
                            position = segment_end
                            continue
                        if segment_end == crop_end:
                            break
                        for character in segment.text:
                            position += cell_len(character)
                            if position >= crop_end:
                                crop_end = position
                                break
                        break
                strip = strip.crop(0, crop_end)
                append(move_to(x, y).segment.text)
                append(strip.render(console))"""

# ---------------------------------------------------------------------------
# Patch 6b: start strips that begin left of a dirty span at the span
# ---------------------------------------------------------------------------
# Anchored on stock code, not on patch 6's output, so it applies the same way to
# a pristine install and to one already patched by an earlier release.
_OLD_RENDER_SEGMENTS_LEFT_EDGE = """\
                if x2 > x >= x1 and end <= x2:
                    append(move_to(x, y).segment.text)
                    append(strip.render(console))
                    continue
"""

_NEW_RENDER_SEGMENTS_LEFT_EDGE = """\
                if x2 > x >= x1 and end <= x2:
                    append(move_to(x, y).segment.text)
                    append(strip.render(console))
                    continue

                if x < x1:
                    # The compositor falls back to complete rows for joined
                    # text. Only ordinary characters have stable crop columns.
                    text = strip.text
                    crop_start = x1 - x
                    position = 0
                    if text.isascii() and text.isprintable():
                        if crop_start >= len(text):
                            continue
                        position = crop_start
                    elif _has_stable_crop_widths(text):
                        for character in text:
                            next_position = position + cell_len(character)
                            if next_position > crop_start:
                                break
                            position = next_position
                        else:
                            # A divided strip may reserve more cells than it paints.
                            continue
                    if position:
                        strip = strip.crop(position)
                        x += position
"""

# Rich's table includes ASCII keycap bases (#, *, digits). On their own
# these stay one cell even in terminals that widen bare pictographs.
_CROP_WIDTH_HELPER = """\
def _has_stable_crop_widths(text: str) -> bool:
    from rich.cells import load_cell_table

    narrow_to_wide = load_cell_table("auto").narrow_to_wide
    return all(
        cell_len(c)
        and not 0x1F1E6 <= ord(c) <= 0x1F1FF
        and (c.isascii() or c not in narrow_to_wide)
        for c in text
    )
"""

_CROP_WIDTH_HELPER_PATCH = FilePatch(
    package="textual",
    module_file="_compositor.py",
    old_fragment="class ReflowResult(NamedTuple):",
    new_fragment=_CROP_WIDTH_HELPER + "\n\nclass ReflowResult(NamedTuple):",
    description="Share the terminal width check between crop and repaint paths",
)


def _crop_width_patches(*, old_fragment: str, new_fragment: str, description: str) -> tuple[FilePatch, FilePatch]:
    """Stage the earlier implementation, then upgrade it on new and existing installs."""
    previous = new_fragment.replace(
        "_has_stable_crop_widths(text)",
        "all(cell_len(c) and not 0x1F1E6 <= ord(c) <= 0x1F1FF for c in text)",
    )
    return (
        FilePatch(
            "textual", "_compositor.py", old_fragment, previous, description, equivalent_fragments=(new_fragment,)
        ),
        FilePatch(
            "textual", "_compositor.py", previous, new_fragment, "Recognize bare emoji when checking crop widths"
        ),
    )


_DIRTY_SPAN_LEFT_EDGE_PATCH, _LEFT_WIDTH_UPGRADE = _crop_width_patches(
    old_fragment=_OLD_RENDER_SEGMENTS_LEFT_EDGE,
    new_fragment=_NEW_RENDER_SEGMENTS_LEFT_EDGE,
    description="Start partial-update strips at the dirty span's left edge",
)

_DIRTY_SPAN_CROP_PATCH = FilePatch(
    package="textual",
    module_file="_compositor.py",
    old_fragment=_OLD_RENDER_SEGMENTS_CROP,
    new_fragment=_NEW_RENDER_SEGMENTS_CROP,
    description="Keep dirty-span crops on complete character boundaries",
)


# The first PR version appended a per-segment crop to a stock fragment
# which later versions also matched. An environment upgraded in place can
# therefore contain both crops. Once both current edges are staged, match
# their adjacency to remove the obsolete block only when it is present.
_LEGACY_SEGMENT_CROP = r"""
                if x < x1:
                    # A merged strip can start over cells whose widgets this
                    # partial update did not render. Start at the span, or at
                    # the double-width character that its edge divides. A
                    # segment whose characters join (emoji sequences, flags,
                    # combining marks) is written whole, as stock Textual does.
                    crop_start = x1 - x
                    position = 0
                    for segment in strip:
                        segment_end = position + cell_len(segment.text)
                        if segment_end > crop_start:
                            break
                        position = segment_end
                    text = segment.text
                    if text.isascii() or all(
                        cell_len(character) and not "\U0001f1e6" <= character <= "\U0001f1ff"
                        for character in text
                    ):
                        for character in text:
                            character_end = position + cell_len(character)
                            if character_end > crop_start:
                                break
                            position = character_end
                        strip = strip.crop(position)
                        x += position
"""
_RIGHT_CROP_START = "                crop_end = min(end, x2) - x"
_REMOVE_LEGACY_CROP = FilePatch(
    package="textual",
    module_file="_compositor.py",
    old_fragment=_LEFT_WIDTH_UPGRADE.new_fragment + _LEGACY_SEGMENT_CROP + "\n" + _RIGHT_CROP_START,
    new_fragment=_LEFT_WIDTH_UPGRADE.new_fragment + "\n" + _RIGHT_CROP_START,
    description="Remove the obsolete per-segment crop from upgraded Textual installs",
)


def apply_runtime_patch() -> None:
    """Install the complete CJK fix on classes already imported by the TUI."""
    try:
        import textual
        import textual._compositor as compositor_mod
    except ImportError:
        return

    if getattr(textual, "__version__", None) != _RUNTIME_PATCH_TEXTUAL_VERSION:
        logger.warning(
            "Skipping Textual compositor CJK runtime patch: loaded Textual is not the pinned %s that the patch targets.",
            _RUNTIME_PATCH_TEXTUAL_VERSION,
        )
        return

    targets = [
        (getattr(compositor_mod, class_name), name) for class_name, names in _RUNTIME_TARGETS.items() for name in names
    ]
    if all(getattr(getattr(cls, name), _RUNTIME_PATCH_MARKER, False) for cls, name in targets):
        return

    # Read the module source directly: file patching shifts line numbers after
    # import, so inspect.getsource(live_method) can return an unrelated body.
    # Stage the same fragments for both pristine and already-patched installs.
    source = Path(compositor_mod.__file__).read_text(encoding="utf-8")
    for patch in _PATCHES:
        if patch.new_fragment in source or any(fragment in source for fragment in patch.equivalent_fragments):
            continue
        if patch.old_fragment not in source:
            logger.warning("Skipping Textual compositor CJK runtime patch: fragment drifted: %s", patch.description)
            return
        source = source.replace(patch.old_fragment, patch.new_fragment, 1)

    tree = ast.parse(source)
    classes = {node.name: node for node in tree.body if isinstance(node, ast.ClassDef)}
    width_helper = next(
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "_has_stable_crop_widths"
    )
    methods: list[ast.stmt] = [width_helper]
    for class_name, names in _RUNTIME_TARGETS.items():
        definitions = {node.name: node for node in classes[class_name].body if isinstance(node, ast.FunctionDef)}
        for name in names:
            method = definitions[name]
            if method.decorator_list:
                raise ValueError(f"Unexpected decorator on Textual {class_name}.{name}")
            methods.append(method)
    code = compile(
        ast.Module(body=methods, type_ignores=[]),
        "<Chrys CJK patch: textual._compositor>",
        "exec",
        flags=__future__.annotations.compiler_flag,
        dont_inherit=True,
    )
    namespace: dict[str, Any] = {}
    # Compile only the selected methods from the pinned, fragment-validated
    # dependency. Their globals stay bound to the live module. Do not reload
    # its classes: textual.screen and existing instances retain those identities.
    exec(code, vars(compositor_mod), namespace)  # noqa: S102
    replacements = [(cls, name, namespace[name]) for cls, name in targets]
    for cls, name, implementation in replacements:
        implementation.__qualname__ = f"{cls.__name__}.{name}"
        setattr(implementation, _RUNTIME_PATCH_MARKER, True)

    # Publish only after every method has been staged and compiled successfully.
    # Keep an existing sentinel so already-produced chops still recognize it.
    merged_strip = getattr(compositor_mod, "_MERGED_STRIP", None)
    if merged_strip is None:
        merged_strip = compositor_mod.Strip([], 0)
    compositor_mod._has_stable_crop_widths = namespace["_has_stable_crop_widths"]
    compositor_mod.bisect_left = bisect_left
    compositor_mod.bisect_right = bisect_right
    compositor_mod._MERGED_STRIP = merged_strip
    compositor_mod._ALWAYS_TRUE = getattr(compositor_mod, "_ALWAYS_TRUE", lambda _: True)
    for cls, name, implementation in replacements:
        setattr(cls, name, implementation)


# ---------------------------------------------------------------------------
# Patch 7: _get_renders -- lists to generators
# ---------------------------------------------------------------------------
_OLD_GET_RENDERS = """\
            widget_regions = [
                (widget, region, clip)
                for widget, (region, clip) in visible_widgets.items()
                if crop_overlaps(clip)
            ]
        else:
            widget_regions = [
                (widget, region, clip)
                for widget, (region, clip) in visible_widgets.items()
            ]

        intersection = _Region.intersection
        contains_region = _Region.contains_region

        for widget, region, clip in widget_regions:"""

_NEW_GET_RENDERS = """\
            widgets_iter = (
                (widget, region, clip)
                for widget, (region, clip) in visible_widgets.items()
                if crop_overlaps(clip)
            )
        else:
            widgets_iter = (
                (widget, region, clip)
                for widget, (region, clip) in visible_widgets.items()
            )

        intersection = _Region.intersection
        contains_region = _Region.contains_region

        for widget, region, clip in widgets_iter:"""

# Keep cuts imposed by unrendered widgets. Both anchors are outside the older
# generator fragment, so an already-patched Textual install picks them up too.
_PARTIAL_ROW_PATCHES = (
    FilePatch(
        package="textual",
        module_file="_compositor.py",
        old_fragment="            crop_overlaps = crop.overlaps",
        new_fragment="            crop_overlaps = _Region(0, crop.y, self.size.width, crop.height).overlaps",
        description="Consider widgets on the affected rows in their original paint order",
    ),
    FilePatch(
        package="textual",
        module_file="_compositor.py",
        old_fragment="            if contains_region(clip, region):\n                yield (",
        new_fragment="""\
            if crop is not None and not crop.overlaps(clip):
                # This widget owns its cells but has no dirty pixels. A blank
                # strip reserves the same chops without calling render_lines.
                # It lies outside every span, so it is never printed.
                visible = intersection(region, clip)
                yield region, clip, [Strip.blank(visible.width)] * visible.height
                continue
            if contains_region(clip, region):
                yield (""",
        description="Reserve unrendered widgets' cells before composing lower widgets",
    ),
)

# ---------------------------------------------------------------------------
# Patch 8: render_full_update -- lambda to _ALWAYS_TRUE
# ---------------------------------------------------------------------------
_OLD_FULL_UPDATE = """\
        chops = self._render_chops(crop, lambda y: True)
        render_strips: list[Iterable[Strip]]"""

_NEW_FULL_UPDATE = """\
        chops = self._render_chops(crop, _ALWAYS_TRUE)
        render_strips: list[Iterable[Strip]]"""

# ---------------------------------------------------------------------------
# Patch 9: render_partial_update -- remove chop_ends
# ---------------------------------------------------------------------------
_OLD_PARTIAL_UPDATE = """\
        chops = self._render_chops(crop, is_rendered_line)
        chop_ends = [cut_set[1:] for cut_set in self.cuts]
        return ChopsUpdate(chops, spans, chop_ends)"""

_NEW_PARTIAL_UPDATE = """\
        chops = self._render_chops(crop, is_rendered_line)
        return ChopsUpdate(chops, spans)"""

# A terminal may expand a joined emoji across styles or count its component
# glyphs differently from Rich. Neither an absolute cursor move after it nor a
# replay up to a guessed right column is safe. Preserve ChopsUpdate's contract
# while emitting the same rows, at the same origins, as a full terminal frame.
_FULL_FRAME_FOR_JOINED_TEXT, _FRAME_WIDTH_UPGRADE = _crop_width_patches(
    old_fragment=_NEW_PARTIAL_UPDATE,
    new_fragment="""\
        background_screens = visible_screen_stack.get([]).copy()
        chops = self._render_chops(crop, is_rendered_line)
        for y, left, _right in spans:
            if left <= 0:
                continue
            for x, strip in chops[y].items():
                if strip is None or not x < left < x + strip.cell_length:
                    continue
                text = strip.text
                if text.isascii() or _has_stable_crop_widths(text):
                    continue
                # Rendering a modal may consume its background screen stack.
                token = visible_screen_stack.set(background_screens)
                try:
                    rows = self._render_chops(screen_region, _ALWAYS_TRUE)
                finally:
                    visible_screen_stack.reset(token)
                width = screen_region.width
                full_chops = [
                    {0: Strip((segment for part in row.values() if part is not None for segment in part), width)}
                    for row in rows
                ]
                return ChopsUpdate(full_chops, [(y, 0, width) for y in range(screen_region.height)])
        return ChopsUpdate(chops, spans)""",
    description="Use complete frame rows when terminal-dependent widths prevent a left crop",
)

# ---------------------------------------------------------------------------
# Patch 10: render_strips -- lambda to _ALWAYS_TRUE
# ---------------------------------------------------------------------------
_OLD_RENDER_STRIPS = """\
        chops = self._render_chops(size.region, lambda y: True)
        render_strips = [Strip.join(chop.values())"""

_NEW_RENDER_STRIPS = """\
        chops = self._render_chops(size.region, _ALWAYS_TRUE)
        render_strips = [Strip.join(chop.values())"""

# ---------------------------------------------------------------------------
# Patch 11: _render_chops -- CJK-safe effective cuts + bisect optimization
# ---------------------------------------------------------------------------
_OLD_RENDER_CHOPS = """\
        renders = self._get_renders(crop)
        intersection = Region.intersection

        for region, clip, strips in renders:
            render_region = intersection(region, clip)
            render_x = render_region.x
            first_cut, last_cut = render_region.column_span

            for y, strip in zip(render_region.line_range, strips):
                if not is_rendered_line(y):
                    continue

                chops_line = chops[y]
                final_cuts = [cut for cut in cuts[y] if (last_cut >= cut >= first_cut)]
                cut_strips = strip.divide([cut - render_x for cut in final_cuts[1:]])

                # Since we are painting front to back, the first segments for a cut "wins"
                get_chops_line = chops_line.get
                for cut, strip in zip(final_cuts, cut_strips):
                    if get_chops_line(cut) is None:
                        chops_line[cut] = strip
        return cast("Sequence[Mapping[int, Strip]]", chops)"""

_NEW_RENDER_CHOPS = """\
        renders = self._get_renders(crop)
        intersection = Region.intersection

        _bisect_left = bisect_left
        _bisect_right = bisect_right

        for region, clip, strips in renders:
            render_region = intersection(region, clip)
            render_x = render_region.x
            first_cut, last_cut = render_region.column_span

            for y, strip in zip(render_region.line_range, strips):
                if not is_rendered_line(y):
                    continue

                chops_line = chops[y]
                # cuts[y] is sorted; use bisect to extract the relevant range.
                line_cuts = cuts[y]
                lo = _bisect_left(line_cuts, first_cut)
                hi = _bisect_right(line_cuts, last_cut)
                final_cuts = line_cuts[lo:hi]

                if len(final_cuts) < 2:
                    continue

                # Compute effective cuts: skip "internal" cuts where both adjacent
                # chops are currently unfilled (None).  When the same widget wins
                # both sides of a cut the division is unnecessary, and -- crucially
                # -- it can split a double-width (CJK) character into two spaces.
                # A cut is a *real* boundary only when the chop on at least one side
                # is already claimed by a higher-priority widget (not None).
                effective_cuts = [final_cuts[0]]
                for _i in range(1, len(final_cuts) - 1):
                    if (
                        chops_line.get(final_cuts[_i - 1]) is not None
                        or chops_line.get(final_cuts[_i]) is not None
                    ):
                        effective_cuts.append(final_cuts[_i])
                effective_cuts.append(final_cuts[-1])

                cut_strips = strip.divide([cut - render_x for cut in effective_cuts[1:]])

                # Since we are painting front to back, the first segments for a cut "wins".
                # Leader chops (at effective_cuts boundaries) receive the actual strip.
                # Follower chops (internal cuts that were skipped) receive _MERGED_STRIP
                # so that lower-priority widgets cannot claim those positions -- the leader
                # strip is wider than its nominal chop size and covers them visually.
                #
                # Walk effective_cuts by index instead of building a set -- both
                # lists are sorted and effective_cuts is a subsequence of
                # final_cuts, so a simple index comparison suffices.
                get_chops_line = chops_line.get
                num_effective = len(effective_cuts) - 1
                eff_idx = 0
                eff_iter = iter(cut_strips)
                for cut in final_cuts[:-1]:
                    if eff_idx < num_effective and cut == effective_cuts[eff_idx]:
                        strip_part = next(eff_iter, None)
                        if strip_part is None:
                            # strip.divide() filters out cuts beyond the
                            # strip's cell_length, so fewer strips may be
                            # returned than effective cuts.  Once exhausted,
                            # remaining chops stay None for lower-priority
                            # widgets to fill.
                            break
                        eff_idx += 1
                        if get_chops_line(cut) is None:
                            chops_line[cut] = strip_part
                    else:
                        if get_chops_line(cut) is None:
                            chops_line[cut] = _MERGED_STRIP
        return cast("Sequence[Mapping[int, Strip]]", chops)"""

# ---------------------------------------------------------------------------
# Register all patches (order: imports first, then code changes)
# ---------------------------------------------------------------------------
_TARGET = "_compositor.py"

_PATCHES = (
    _CROP_WIDTH_HELPER_PATCH,
    FilePatch(
        package="textual",
        module_file=_TARGET,
        old_fragment=_OLD_IMPORT,
        new_fragment=_NEW_IMPORT,
        description="Add bisect imports for CJK compositor fix",
    ),
    FilePatch(
        package="textual",
        module_file=_TARGET,
        old_fragment=_OLD_SENTINELS,
        new_fragment=_NEW_SENTINELS,
        description="Add _MERGED_STRIP and _ALWAYS_TRUE sentinels",
    ),
    FilePatch(
        package="textual",
        module_file=_TARGET,
        old_fragment=_OLD_CHOPS_INIT,
        new_fragment=_NEW_CHOPS_INIT,
        description="Remove chop_ends from ChopsUpdate.__init__",
    ),
    FilePatch(
        package="textual",
        module_file=_TARGET,
        old_fragment=_OLD_RICH_CONSOLE,
        new_fragment=_NEW_RICH_CONSOLE,
        description="Update ChopsUpdate.__rich_console__ for merged strips",
    ),
    FilePatch(
        package="textual",
        module_file=_TARGET,
        old_fragment=_OLD_RENDER_SEGMENTS,
        new_fragment=_NEW_RENDER_SEGMENTS,
        description="Update ChopsUpdate.render_segments for merged strips",
    ),
    _DIRTY_SPAN_LEFT_EDGE_PATCH,
    _LEFT_WIDTH_UPGRADE,
    _DIRTY_SPAN_CROP_PATCH,
    _REMOVE_LEGACY_CROP,
    *_PARTIAL_ROW_PATCHES,
    FilePatch(
        package="textual",
        module_file=_TARGET,
        old_fragment=_OLD_GET_RENDERS,
        new_fragment=_NEW_GET_RENDERS,
        description="Use generators in _get_renders for lazy evaluation",
    ),
    FilePatch(
        package="textual",
        module_file=_TARGET,
        old_fragment=_OLD_FULL_UPDATE,
        new_fragment=_NEW_FULL_UPDATE,
        description="Use cached _ALWAYS_TRUE in render_full_update",
    ),
    FilePatch(
        package="textual",
        module_file=_TARGET,
        old_fragment=_OLD_PARTIAL_UPDATE,
        new_fragment=_NEW_PARTIAL_UPDATE,
        description="Remove chop_ends from render_partial_update",
        equivalent_fragments=(_FULL_FRAME_FOR_JOINED_TEXT.new_fragment, _FRAME_WIDTH_UPGRADE.new_fragment),
    ),
    _FULL_FRAME_FOR_JOINED_TEXT,
    _FRAME_WIDTH_UPGRADE,
    FilePatch(
        package="textual",
        module_file=_TARGET,
        old_fragment=_OLD_RENDER_STRIPS,
        new_fragment=_NEW_RENDER_STRIPS,
        description="Use cached _ALWAYS_TRUE in render_strips",
    ),
    FilePatch(
        package="textual",
        module_file=_TARGET,
        old_fragment=_OLD_RENDER_CHOPS,
        new_fragment=_NEW_RENDER_CHOPS,
        description="CJK-safe effective cuts with bisect optimization in _render_chops",
    ),
)

register(*_PATCHES)
