# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Spec tests for the model-name dropdown once Fetch has found a list.

Written from ``.plan/model-list-fetch.md`` §3.1 (overlay, filter, highlight, keyboard and mouse);
the list comes from a recording fake installed as ``ModelConfigScreen.discover_models``.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator

import pytest
from textual import events
from textual.pilot import Pilot

from chrys.app.tui.screens.models.screen import ModelConfigScreen
from chrys.foundation.text.reading_order import reading_order
from tests.app.tui.screens._model_config_support import (
    open_model_config,
    single_profile_registry,
)
from tests.app.tui.screens._model_list_support import (
    QUIET_WINDOW,
    URL,
    FakeDiscover,
    _close_list,
    _open_by_arrow,
    _open_by_down,
    assert_overlay_visible,
    fetch_list,
    focus_handled,
    model_input,
    models,
    no_new_query,
    open_model_config_zh,
    overlay,
    overlay_expanded,
    overlay_texts,
    type_into,
)
from tests.support.tui_helpers import click_when_settled
from tests.support.waiting import wait_for, wait_until, wait_until_quiet

pytestmark = pytest.mark.usefixtures("isolated_model_config_dir", "clear_model_profile_env", "clear_provider_env")


# ── Opening, filtering and choosing (§3.1) ───────────────────────────


LIST = tuple(
    sorted(
        (
            "anthropic/claude-haiku-4.5",
            "anthropic/Claude-Opus-5",
            "openai/gpt-5.5",
            "openai/gpt-5.5-mini",
            "zeta/model",
        ),
        key=reading_order,
    )
)
CLAUDE = [i for i in LIST if "claude" in i.casefold()]
GPT = [i for i in LIST if "gpt" in i.casefold()]


@contextlib.asynccontextmanager
async def opened_list(
    fake: FakeDiscover, model_id: str, *, by: str = "fetch"
) -> AsyncIterator[tuple[ModelConfigScreen, Pilot]]:
    """The list fetched for *model_id*, open: as Fetch opened it, or closed and reopened *by* arrow or ↓."""
    fake.respond = lambda _s: models(*LIST)
    registry, profile = single_profile_registry(base_url=URL, api_key="sk-a", model_id=model_id)
    async with open_model_config(registry, global_default_profile_id=profile.id) as (screen, pilot):
        await fetch_list(screen, pilot)
        if by != "fetch":
            await _close_list(screen, pilot)
            if by == "arrow":
                await _open_by_arrow(screen, pilot)
            else:
                await _open_by_down(screen, pilot)
        await wait_for(lambda: overlay(screen).has_focus, pilot=pilot, description="focus in the list")
        yield screen, pilot


@pytest.mark.parametrize("by", ["fetch", "arrow", "down"])
async def test_exact_id_opens_everything_with_it_highlighted(fake_discover: FakeDiscover, by: str) -> None:
    async with opened_list(fake_discover, "openai/gpt-5.5", by=by) as (screen, _pilot):
        assert overlay_texts(screen) == list(LIST)
        assert overlay(screen).highlighted == LIST.index("openai/gpt-5.5")
        assert overlay(screen).has_focus
        assert_overlay_visible(screen)


@pytest.mark.parametrize("by", ["fetch", "arrow", "down"])
async def test_empty_input_opens_everything_with_the_first_highlighted(fake_discover: FakeDiscover, by: str) -> None:
    """Like the other dropdowns, an empty field opens with a highlighted row that Enter picks."""
    async with opened_list(fake_discover, "", by=by) as (screen, pilot):
        assert overlay_texts(screen) == list(LIST)
        assert overlay(screen).highlighted == 0

        await pilot.press("enter")
        await wait_for(lambda: not overlay_expanded(screen), pilot=pilot)
        assert model_input(screen).value == LIST[0]


async def test_other_text_filters_by_case_insensitive_substring(fake_discover: FakeDiscover) -> None:
    async with opened_list(fake_discover, "Claude") as (screen, _pilot):
        assert overlay_texts(screen) == CLAUDE
        assert overlay(screen).highlighted == 0
        assert model_input(screen).value == "Claude"


async def test_substring_must_be_contiguous(fake_discover: FakeDiscover) -> None:
    async with opened_list(fake_discover, "") as (screen, pilot):
        await pilot.press(*"gpt5")

        await wait_for(lambda: model_input(screen).value == "gpt5", pilot=pilot)
        await wait_for(lambda: overlay_texts(screen) == ["No results"], pilot=pilot)


async def test_typing_while_open_edits_the_input_and_refilters(fake_discover: FakeDiscover) -> None:
    async with opened_list(fake_discover, "") as (screen, pilot):
        await pilot.press(*"gpt")

        await wait_for(lambda: model_input(screen).value == "gpt", pilot=pilot)
        await wait_for(lambda: overlay_texts(screen) == GPT, pilot=pilot)
        assert overlay(screen).highlighted == 0
        assert overlay_expanded(screen)

        await pilot.press(*"-5.5-")
        await wait_for(lambda: overlay_texts(screen) == ["openai/gpt-5.5-mini"], pilot=pilot)
        assert model_input(screen).value == "gpt-5.5-"


@pytest.mark.parametrize("pasted", ["gpt-5", "gpt-5\ndef"], ids=["one-line", "multi-line"])
async def test_pasting_while_open_edits_the_input_and_refilters(fake_discover: FakeDiscover, pasted: str) -> None:
    """A paste into the open list appends to the input like typing; only its first line, as Input does (§3.1)."""
    async with opened_list(fake_discover, "openai/") as (screen, pilot):
        assert overlay(screen).has_focus

        # The driver's path: the app forwards a paste to the focused widget.
        pilot.app.post_message(events.Paste(pasted))

        model = model_input(screen)
        await wait_for(lambda: model.value == "openai/gpt-5", pilot=pilot, description="paste appended")
        expected = [i for i in LIST if "openai/gpt-5" in i.casefold()]
        assert len(expected) == 2
        await wait_for(lambda: overlay_texts(screen) == expected, pilot=pilot, description="list refiltered")
        assert overlay(screen).highlighted == 0
        assert overlay_expanded(screen)
        assert_overlay_visible(screen)
        assert overlay(screen).has_focus
        assert model.cursor_position == len(model.value)


async def test_backspace_while_open_edits_the_input_and_refilters(fake_discover: FakeDiscover) -> None:
    async with opened_list(fake_discover, "claude-o") as (screen, pilot):
        assert overlay_texts(screen) == ["anthropic/Claude-Opus-5"]

        await pilot.press("backspace", "backspace")
        await wait_for(lambda: model_input(screen).value == "claude", pilot=pilot)
        await wait_for(lambda: overlay_texts(screen) == CLAUDE, pilot=pilot)
        assert overlay(screen).highlighted == 0

        await pilot.press(*["backspace"] * 6)
        await wait_for(lambda: model_input(screen).value == "", pilot=pilot)
        await wait_for(lambda: overlay_texts(screen) == list(LIST), pilot=pilot)
        assert overlay(screen).highlighted == 0
        assert overlay_expanded(screen)


async def test_leftover_value_matching_nothing_opens_everything_unhighlighted(fake_discover: FakeDiscover) -> None:
    """A value left from another service, before the user types: all items, none highlighted (§3.1)."""
    async with opened_list(fake_discover, "my-deployment") as (screen, pilot):
        assert overlay_texts(screen) == list(LIST)
        assert overlay(screen).highlighted is None

        await pilot.press("enter")
        changed = await wait_until(
            lambda: model_input(screen).value != "my-deployment", timeout=QUIET_WINDOW, pilot=pilot
        )

        assert not changed, model_input(screen).value


@pytest.mark.parametrize("by", ["fetch", "arrow", "down"])
async def test_leftover_value_opens_everything_by_any_way(fake_discover: FakeDiscover, by: str) -> None:
    async with opened_list(fake_discover, "other-service/model-x", by=by) as (screen, _pilot):
        assert overlay_texts(screen) == list(LIST)
        assert overlay(screen).highlighted is None


async def test_no_match_after_typing_shows_one_disabled_row_and_enter_keeps_the_input(
    fake_discover: FakeDiscover,
) -> None:
    async with opened_list(fake_discover, "my-deployment") as (screen, pilot):
        await pilot.press("x")

        await wait_for(lambda: model_input(screen).value == "my-deploymentx", pilot=pilot)
        await wait_for(lambda: overlay_texts(screen) == ["No results"], pilot=pilot)
        assert overlay(screen).get_option_at_index(0).disabled is True

        await pilot.press("enter")
        changed = await wait_until(
            lambda: model_input(screen).value != "my-deploymentx", timeout=QUIET_WINDOW, pilot=pilot
        )

        assert not changed, model_input(screen).value


async def test_typing_after_a_leftover_value_filters_strictly(fake_discover: FakeDiscover) -> None:
    async with opened_list(fake_discover, "my-deployment") as (screen, pilot):
        await pilot.press(*["backspace"] * len("my-deployment"), *"mini")

        await wait_for(lambda: model_input(screen).value == "mini", pilot=pilot)
        await wait_for(lambda: overlay_texts(screen) == ["openai/gpt-5.5-mini"], pilot=pilot)
        assert overlay(screen).highlighted == 0


async def test_no_results_row_is_localized(fake_discover: FakeDiscover) -> None:
    fake_discover.respond = lambda _s: models(*LIST)
    registry, profile = single_profile_registry(base_url=URL, api_key="sk-a", model_id="my-deployment")

    async with open_model_config_zh(registry, global_default_profile_id=profile.id) as (screen, pilot):
        await fetch_list(screen, pilot)
        await wait_for(lambda: overlay(screen).has_focus, pilot=pilot)
        await pilot.press("z")

        await wait_for(lambda: overlay_texts(screen) == ["无结果"], pilot=pilot)


async def test_choosing_a_filtered_item_fills_the_input_and_returns_focus(fake_discover: FakeDiscover) -> None:
    async with opened_list(fake_discover, "Claude") as (screen, pilot):
        await pilot.press("down", "enter")

        await wait_for(lambda: not overlay_expanded(screen), pilot=pilot)
        model = model_input(screen)
        assert model.value == CLAUDE[1]
        await wait_for(lambda: model.has_focus, pilot=pilot)
        assert screen._build_profile_from_form().model_id == CLAUDE[1]


async def test_choosing_from_the_full_list(fake_discover: FakeDiscover) -> None:
    async with opened_list(fake_discover, "openai/gpt-5.5") as (screen, pilot):
        await pilot.press("down", "enter")

        await wait_for(lambda: not overlay_expanded(screen), pilot=pilot)
        assert model_input(screen).value == "openai/gpt-5.5-mini"
        await wait_for(lambda: model_input(screen).has_focus, pilot=pilot)


async def test_clicking_an_item_fills_the_input(fake_discover: FakeDiscover) -> None:
    async with opened_list(fake_discover, "") as (screen, pilot):
        menu = overlay(screen)
        row = LIST.index("zeta/model")
        target_y = menu.content_region.y + row - menu.scroll_offset.y
        offset = (menu.content_region.x - menu.region.x + 1, target_y - menu.region.y)

        await click_when_settled(pilot, menu, offset=offset)

        await wait_for(lambda: not overlay_expanded(screen), pilot=pilot)
        assert model_input(screen).value == "zeta/model"


async def test_clicking_outside_collapses_without_changing_the_value(fake_discover: FakeDiscover) -> None:
    async with opened_list(fake_discover, "openai/gpt-5.5") as (screen, pilot):
        await pilot.press("down")

        await click_when_settled(pilot, "#mc-name")

        await wait_for(lambda: not overlay_expanded(screen), pilot=pilot)
        assert model_input(screen).value == "openai/gpt-5.5"


async def test_typing_in_the_model_input_does_not_open_the_list(fake_discover: FakeDiscover) -> None:
    async with opened_list(fake_discover, "") as (screen, pilot):
        await _close_list(screen, pilot)

        await type_into(pilot, model_input(screen), "openai/")
        await wait_for(lambda: model_input(screen).value == "openai/", pilot=pilot)

        assert not overlay_expanded(screen)
        await no_new_query(fake_discover, pilot, after=1)


async def test_down_without_a_list_scrolls_the_form_as_before(fake_discover: FakeDiscover) -> None:
    registry, profile = single_profile_registry(base_url="", api_key="")

    async with open_model_config(registry, global_default_profile_id=profile.id, size=(100, 24)) as (screen, pilot):
        scroll = screen.query_one("#mc-scroll")
        await wait_for(lambda: scroll.max_scroll_y > 0, pilot=pilot, description="form scrolls")
        model = model_input(screen)
        model.focus()
        await wait_for(lambda: model.has_focus, pilot=pilot)
        await focus_handled(model, pilot)
        await wait_until_quiet(lambda: scroll.scroll_y, description="focus scroll settled", pilot=pilot)
        before = scroll.scroll_y

        await pilot.press("down")

        await wait_for(lambda: scroll.scroll_y > before, pilot=pilot, description="down scrolled the form")
        assert not overlay_expanded(screen)


async def test_save_keeps_exactly_the_typed_text(fake_discover: FakeDiscover) -> None:
    fake_discover.respond = lambda _s: models(*LIST)
    registry, profile = single_profile_registry(base_url=URL, api_key="sk-a", model_id="openai/gpt-5.5")

    async with open_model_config(registry, global_default_profile_id=profile.id) as (screen, pilot):
        await fetch_list(screen, pilot)
        await _close_list(screen, pilot)
        model_input(screen).value = "openai/gpt-5.5-custom"
        await wait_for(lambda: model_input(screen).value == "openai/gpt-5.5-custom", pilot=pilot)

        saved = await screen._save_only()

    assert saved is not None
    assert saved.model_id == "openai/gpt-5.5-custom"
    stored = registry.get(profile.id)
    assert stored is not None
    assert stored.model_id == "openai/gpt-5.5-custom"


async def test_escape_with_the_list_open_closes_the_whole_dialog(fake_discover: FakeDiscover) -> None:
    """Accepted existing behavior (Q7): the dialog's priority Esc wins over the open list."""
    async with opened_list(fake_discover, "openai/gpt-5.5") as (screen, pilot):
        assert overlay(screen).has_focus

        await pilot.press("escape")

        await wait_for(lambda: pilot.app.screen is not screen, pilot=pilot, description="dialog closed by Esc")
        assert screen.is_attached is False or pilot.app.screen is not screen


# ── Clicking the model input while the list is open (§3.1) ───────────


async def _click_input_keeping_list(screen: ModelConfigScreen, pilot: Pilot) -> None:
    await click_when_settled(pilot, "#mc-model")
    await wait_for(lambda: model_input(screen).has_focus, pilot=pilot, description="input focused")
    await focus_handled(model_input(screen), pilot)
    assert overlay_expanded(screen)


async def test_clicking_the_input_keeps_the_list_open(fake_discover: FakeDiscover) -> None:
    async with opened_list(fake_discover, "openai/gpt-5.5") as (screen, pilot):
        await _click_input_keeping_list(screen, pilot)

        stayed = not await wait_until(lambda: not overlay_expanded(screen), timeout=QUIET_WINDOW, pilot=pilot)
        assert stayed
        assert model_input(screen).has_focus


async def test_editing_anywhere_in_the_input_refilters_live(fake_discover: FakeDiscover) -> None:
    async with opened_list(fake_discover, "") as (screen, pilot):
        await _click_input_keeping_list(screen, pilot)

        await pilot.press("end", "5")
        await wait_for(lambda: model_input(screen).value == "5", pilot=pilot)
        await wait_for(lambda: overlay_texts(screen) == [i for i in LIST if "5" in i], pilot=pilot)

        await pilot.press("home", *"opus-")
        await wait_for(lambda: model_input(screen).value == "opus-5", pilot=pilot)
        await wait_for(lambda: overlay_texts(screen) == ["anthropic/Claude-Opus-5"], pilot=pilot)
        assert overlay_expanded(screen)
        assert model_input(screen).has_focus


async def test_down_in_the_input_returns_to_the_list_and_moves_the_highlight(fake_discover: FakeDiscover) -> None:
    async with opened_list(fake_discover, "Claude") as (screen, pilot):
        assert overlay(screen).highlighted == 0
        await _click_input_keeping_list(screen, pilot)

        await pilot.press("down")

        await wait_for(lambda: overlay(screen).has_focus, pilot=pilot, description="focus back in the list")
        assert overlay(screen).highlighted == 1
        assert overlay_expanded(screen)


async def test_enter_in_the_input_picks_the_highlighted_item(fake_discover: FakeDiscover) -> None:
    async with opened_list(fake_discover, "Claude") as (screen, pilot):
        await _click_input_keeping_list(screen, pilot)

        await pilot.press("enter")

        await wait_for(lambda: not overlay_expanded(screen), pilot=pilot)
        assert model_input(screen).value == CLAUDE[0]


async def test_enter_in_the_input_without_a_highlight_keeps_the_text(fake_discover: FakeDiscover) -> None:
    async with opened_list(fake_discover, "my-deployment") as (screen, pilot):
        assert overlay(screen).highlighted is None
        await _click_input_keeping_list(screen, pilot)

        await pilot.press("enter")

        await wait_for(lambda: not overlay_expanded(screen), pilot=pilot)
        assert model_input(screen).value == "my-deployment"


@pytest.mark.parametrize(
    ("leave", "lands_on"),
    [("click", "#mc-name"), ("tab", "#mc-model-fetch"), ("shift+tab", "#mc-api-key")],
)
async def test_leaving_the_input_closes_the_list_without_changing_the_value(
    fake_discover: FakeDiscover, leave: str, lands_on: str
) -> None:
    async with opened_list(fake_discover, "Claude") as (screen, pilot):
        await _click_input_keeping_list(screen, pilot)

        if leave == "click":
            await click_when_settled(pilot, "#mc-name")
        else:
            await pilot.press(leave)

        await wait_for(lambda: not overlay_expanded(screen), pilot=pilot)
        target = screen.query_one(lands_on)
        await wait_for(lambda: target.has_focus, pilot=pilot, description=f"focus on {lands_on}")
        assert model_input(screen).value == "Claude"


# ── Long ids ─────────────────────────────────────────────────────────


LONG_ID = "vendor/" + "a" * 90 + "-match-" + "b" * 90 + "-tail"


def _prompt_plain_and_bold(prompt: object) -> tuple[str, list[tuple[int, int]]]:
    """Plain text of an option prompt (Rich ``Text`` or Textual ``Content``) and its bold span ranges."""
    plain = str(getattr(prompt, "plain", prompt))
    bold: list[tuple[int, int]] = []
    for span in getattr(prompt, "spans", []):
        style = span.style
        is_bold = getattr(style, "bold", None) is True or "bold" in str(style).split()
        if is_bold:
            bold.append((span.start, span.end))
    return plain, sorted(bold)


def _bold_chars(plain: str, ranges: list[tuple[int, int]]) -> str:
    return "".join(plain[start:end] for start, end in ranges)


@contextlib.asynccontextmanager
async def _long_list_open(fake: FakeDiscover, model_id: str) -> AsyncIterator[tuple[ModelConfigScreen, Pilot]]:
    fake.respond = lambda _s: models(*sorted([*LIST, LONG_ID], key=reading_order))
    registry, profile = single_profile_registry(base_url=URL, api_key="sk-a", model_id=model_id)
    async with open_model_config(registry, global_default_profile_id=profile.id) as (screen, pilot):
        await fetch_list(screen, pilot)
        # The first frame after opening may still be laid out; judge the rows the user is left with.
        await wait_until_quiet(lambda: overlay_texts(screen), description="rows settled", pilot=pilot)
        yield screen, pilot


async def test_long_id_is_middle_elided_with_bold_on_the_visible_match(fake_discover: FakeDiscover) -> None:
    async with _long_list_open(fake_discover, "-TAIL") as (screen, pilot):
        menu = overlay(screen)
        assert menu.option_count == 1
        plain, bold = _prompt_plain_and_bold(menu.get_option_at_index(0).prompt)

        assert "…" in plain
        assert plain.startswith("vendor/")
        assert plain.endswith("-tail")
        assert len(plain) < len(LONG_ID)
        assert len(plain) <= menu.scrollable_content_region.width
        assert bold, "the visible match is bold"
        assert _bold_chars(plain, bold) == "-tail"
        assert bold[-1][1] == len(plain)

        await pilot.press("enter")
        await wait_for(lambda: not overlay_expanded(screen), pilot=pilot)
        assert model_input(screen).value == LONG_ID


async def test_match_inside_the_elided_middle_is_not_bold(fake_discover: FakeDiscover) -> None:
    async with _long_list_open(fake_discover, "-match-") as (screen, _pilot):
        menu = overlay(screen)
        assert menu.option_count == 1
        plain, bold = _prompt_plain_and_bold(menu.get_option_at_index(0).prompt)

        assert "…" in plain
        assert "-match-" not in plain
        assert bold == []


async def test_match_at_the_visible_start_is_bold(fake_discover: FakeDiscover) -> None:
    async with _long_list_open(fake_discover, "VENDOR/") as (screen, _pilot):
        menu = overlay(screen)
        assert menu.option_count == 1
        plain, bold = _prompt_plain_and_bold(menu.get_option_at_index(0).prompt)

        assert _bold_chars(plain, bold) == "vendor/"
        assert bold[0][0] == 0


async def test_short_ids_keep_their_full_text_and_bold_match(fake_discover: FakeDiscover) -> None:
    async with _long_list_open(fake_discover, "Claude") as (screen, _pilot):
        menu = overlay(screen)
        rows = [_prompt_plain_and_bold(menu.get_option_at_index(i).prompt) for i in range(menu.option_count)]

        assert [plain for plain, _bold in rows] == CLAUDE
        assert [_bold_chars(plain, bold).casefold() for plain, bold in rows] == ["claude", "claude"]


# ── Typed text versus a leftover value (§3.1) ────────────────────────


async def _type_model_closed(screen: ModelConfigScreen, pilot: Pilot, text: str) -> None:
    model = model_input(screen)
    model.focus()
    await wait_for(lambda: model.has_focus, pilot=pilot)
    await focus_handled(model, pilot)
    await pilot.press("ctrl+a", "backspace", *text)
    await wait_for(lambda: model.value == text, pilot=pilot)
    assert not overlay_expanded(screen)


async def test_text_typed_while_closed_filters_strictly_on_every_open(fake_discover: FakeDiscover) -> None:
    async with opened_list(fake_discover, "openai/gpt-5.5") as (screen, pilot):
        await _close_list(screen, pilot)
        await _type_model_closed(screen, pilot, "zzz")

        await pilot.press("down")
        await wait_for(lambda: overlay_expanded(screen), pilot=pilot)
        assert overlay_texts(screen) == ["No results"]

        await _close_list(screen, pilot)
        await _open_by_arrow(screen, pilot)
        assert overlay_texts(screen) == ["No results"]
        assert model_input(screen).value == "zzz"


async def test_picked_item_reopens_the_full_list_highlighted(fake_discover: FakeDiscover) -> None:
    async with opened_list(fake_discover, "") as (screen, pilot):
        await _close_list(screen, pilot)
        await _type_model_closed(screen, pilot, "mini")
        await pilot.press("down")
        await wait_for(lambda: overlay_texts(screen) == ["openai/gpt-5.5-mini"], pilot=pilot)
        await pilot.press("enter")
        await wait_for(lambda: not overlay_expanded(screen), pilot=pilot)
        assert model_input(screen).value == "openai/gpt-5.5-mini"

        await _open_by_arrow(screen, pilot)

        assert overlay_texts(screen) == list(LIST)
        assert overlay(screen).highlighted == LIST.index("openai/gpt-5.5-mini")


async def test_typed_text_becomes_a_leftover_after_a_new_fetch(fake_discover: FakeDiscover) -> None:
    """Any completed fetch makes earlier typed text a leftover, even one returning an identical list."""
    async with opened_list(fake_discover, "openai/gpt-5.5") as (screen, pilot):
        await _close_list(screen, pilot)
        await _type_model_closed(screen, pilot, "zzz")

        await fetch_list(screen, pilot)

        assert len(fake_discover.calls) == 2
        assert overlay_texts(screen) == list(LIST)
        assert overlay(screen).highlighted is None
        assert model_input(screen).value == "zzz"


async def test_refetch_highlights_the_current_value(fake_discover: FakeDiscover) -> None:
    async with opened_list(fake_discover, "") as (screen, pilot):
        await _close_list(screen, pilot)
        await _type_model_closed(screen, pilot, "zeta/model")

        await fetch_list(screen, pilot)

        assert overlay_texts(screen) == list(LIST)
        assert overlay(screen).highlighted == LIST.index("zeta/model")
        await wait_for(lambda: overlay(screen).has_focus, pilot=pilot)


# ── The open list stays below the model input ────────────────────────


MANY = tuple(f"vendor/model-{i:03d}" for i in range(120))


async def _open_many(screen: ModelConfigScreen, pilot: Pilot, by: str) -> None:
    await fetch_list(screen, pilot)
    if by == "fetch":
        return
    # Leave the list by Tab (it closes and moves on), then reopen it from the model row.
    model = model_input(screen)
    await click_when_settled(pilot, model)
    await wait_for(lambda: model.has_focus, pilot=pilot)
    await pilot.press("tab")
    await wait_for(lambda: not overlay_expanded(screen), pilot=pilot)
    if by == "arrow":
        await _open_by_arrow(screen, pilot)
    else:
        await _open_by_down(screen, pilot)


@pytest.mark.parametrize("size", [(120, 36), (80, 24)], ids=["120x36", "80x24"])
@pytest.mark.parametrize("by", ["fetch", "arrow", "down"])
async def test_opening_scrolls_the_form_so_the_list_sits_below_the_input(
    fake_discover: FakeDiscover, size: tuple[int, int], by: str
) -> None:
    fake_discover.respond = lambda _s: models(*MANY)
    registry, profile = single_profile_registry(base_url=URL, api_key="sk-a", model_id="")

    async with open_model_config(registry, global_default_profile_id=profile.id, size=size) as (screen, pilot):
        await _open_many(screen, pilot, by)
        scroll = screen.query_one("#mc-scroll")
        menu = overlay(screen)
        model = model_input(screen)
        await wait_until_quiet(
            lambda: (scroll.scroll_y, menu.region, model.region), description="form scrolled", pilot=pilot
        )

        assert_overlay_visible(screen)
        assert model.region.height > 0, model.region
        assert scroll.region.contains_region(model.region), (model.region, scroll.region)
        assert menu.region.y > model.region.y, (menu.region, model.region)
        assert not menu.region.overlaps(model.region), (menu.region, model.region)


@pytest.mark.parametrize("by", ["fetch", "down"])
async def test_opening_on_a_screen_too_short_for_the_list_does_not_crash(fake_discover: FakeDiscover, by: str) -> None:
    fake_discover.respond = lambda _s: models(*MANY)
    registry, profile = single_profile_registry(base_url=URL, api_key="sk-a", model_id="")

    async with open_model_config(registry, global_default_profile_id=profile.id, size=(80, 14)) as (screen, pilot):
        await fetch_list(screen, pilot)
        if by == "down":
            await _close_list(screen, pilot)
            await _open_by_down(screen, pilot)
        await wait_until_quiet(lambda: overlay(screen).region, description="list placed", pilot=pilot)

        assert overlay_expanded(screen)
        assert pilot.app.is_running
        assert pilot.app.screen is screen
