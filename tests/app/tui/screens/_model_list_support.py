# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Shared fakes, readers and actions for the model-list Fetch and dropdown screen tests.

The fixtures that install the fakes (``fake_discover``, ``real_discover``, ``clear_provider_env``)
live in this directory's ``conftest.py``.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
from collections.abc import AsyncIterator, Callable
from typing import Any, Literal

import httpx
import pytest
from textual.color import Color
from textual.pilot import Pilot
from textual.widget import Widget
from textual.widgets import Button, Input, Label, OptionList

from chrys.app.tui.screens.models.screen import ModelConfigScreen
from chrys.app.tui.util.spinner import SPINNER_FRAMES, STATIC_SPINNER
from chrys.service.llm import model_list as model_list_module
from chrys.service.llm.model_list import ConnectionSnapshot, ModelDiscovery, ModelInfo
from chrys.service.llm.providers import PROVIDERS
from chrys.service.profiles.models.registry import ModelProfileRegistry
from chrys.service.profiles.models.schema import ModelProfile
from tests.app.tui.screens._model_config_support import open_model_config
from tests.service.llm._model_list_support import Recorder
from tests.support.tui_helpers import click_when_settled
from tests.support.waiting import wait_for, wait_until, wait_until_quiet

URL = "https://gw.example.test/v1"
ENV_URL = "https://env.example.test/v1"
# Bounded window for "nothing starts / nothing changes".
QUIET_WINDOW = 0.4
BASE_MODEL_LABEL = "* OpenAI Model"
FETCH_EN = "Fetch"
# While fetching, the row under the model input shows "<spinner frame> " + one of these, muted;
# the model title and the button label never change.
FETCHING_EN = "Fetching the model list"
FETCHING_ZH = "获取模型列表中"
RETRYING_EN = "Couldn't connect; retry round {round}/{rounds}"
RETRYING_ZH = "连接失败，第 {round}/{rounds} 轮重试中"  # noqa: RUF001
NOT_FOUND_EN = "No model list found"
NOT_FOUND_ZH = "未找到模型列表"
KEY_NEEDED_EN = "Try an API key"
KEY_REJECTED_EN = "The list request was rejected"
KEY_REJECTED_ZH = "列表请求被拒绝"
# A correction found a strict list: the Base URL input is rewritten and one message under it lists
# the changes in the order they were applied.
PART_ADDED_V1_EN = "added /v1"
PART_REMOVED_V1_EN = "removed /v1"
PART_REMOVED_ENDPOINT_EN = "removed the endpoint path"
PART_ADDED_SCHEME_EN = "added https://"
PART_ADDED_V1_ZH = "补上 /v1"
PART_REMOVED_ENDPOINT_ZH = "去掉接口路径"
PART_ADDED_SCHEME_ZH = "补上 https://"


def corrected_en(*parts: str) -> str:
    return "Automatically " + "; ".join(parts)


def corrected_zh(*parts: str) -> str:
    return "已自动" + "，".join(parts)  # noqa: RUF001


URL_ADDED_V1_EN = corrected_en(PART_ADDED_V1_EN)
URL_REMOVED_V1_EN = corrected_en(PART_REMOVED_V1_EN)
UNREACHABLE_EN = "Couldn't connect; check address or network"
UNREACHABLE_ZH = "无法连接，请检查地址或网络"  # noqa: RUF001
KEY_PLACEHOLDER = "sk-..."


Answer = ModelDiscovery | list[ModelInfo]


class FakeDiscover:
    """Stand-in for ``discover_models``: records every snapshot and answers through *respond*.

    While ``hold`` is an event, calls that start wait on it before answering.
    """

    def __init__(self) -> None:
        self.calls: list[ConnectionSnapshot] = []
        # The progress callback the screen passed with each call.
        self.on_retry: list[Callable[[int, int], None] | None] = []
        self.finished = 0
        self.respond: Callable[[ConnectionSnapshot], Answer] = lambda _snapshot: []
        self.hold: asyncio.Event | None = None

    async def __call__(
        self,
        snapshot: ConnectionSnapshot,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        on_retry: Callable[[int, int], None] | None = None,
    ) -> ModelDiscovery:
        self.calls.append(snapshot)
        self.on_retry.append(on_retry)
        gate = self.hold
        try:
            if gate is not None:
                await gate.wait()
            answer = self.respond(snapshot)
            return answer if isinstance(answer, ModelDiscovery) else ModelDiscovery(models=list(answer))
        finally:
            self.finished += 1

    @property
    def idle(self) -> bool:
        return self.finished == len(self.calls)


class RealDiscover(Recorder):
    """The real ``discover_models`` over a recording ``MockTransport``."""

    def __init__(self) -> None:
        super().__init__(
            lambda _request: httpx.Response(200, json={"object": "list", "data": [{"id": "m-1", "object": "model"}]})
        )
        self.results: list[ModelDiscovery] = []

    async def __call__(
        self,
        snapshot: ConnectionSnapshot,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        on_retry: Callable[[int, int], None] | None = None,
    ) -> ModelDiscovery:
        result = await model_list_module.discover_models(snapshot, transport=self.transport, on_retry=on_retry)
        self.results.append(result)
        return result


def models(*ids: str) -> list[ModelInfo]:
    return [ModelInfo(id=i) for i in ids]


def rejected() -> ModelDiscovery:
    return ModelDiscovery(models=[], auth_rejected=True)


V1Fix = Literal["missing_v1", "extra_v1"]


def v1_fixed(kind: V1Fix, base: str, *ids: str) -> ModelDiscovery:
    """A list step 5 found after adding (``missing_v1``) or removing (``extra_v1``) ``/v1``."""
    correction = "added_v1" if kind == "missing_v1" else "removed_v1"
    return ModelDiscovery(models=models(*ids), url_corrections=(correction,), corrected_base_url=base)


# The profile's URL ends in /v1; the list turned up one level up.
REMOVED_BASE = "https://gw.example.test"


def removed_v1() -> ModelDiscovery:
    return v1_fixed("extra_v1", REMOVED_BASE, "m")


def no_response() -> ModelDiscovery:
    return ModelDiscovery(models=[], unreachable=True)


# ── Screen readers ───────────────────────────────────────────────────


def fetch_button(screen: ModelConfigScreen) -> Button:
    return screen.query_one("#mc-model-fetch", Button)


def fetch_ready(screen: ModelConfigScreen, label: str = FETCH_EN) -> bool:
    button = fetch_button(screen)
    return not button.disabled and str(button.label) == label


def arrow_shown(screen: ModelConfigScreen) -> bool:
    arrows = list(screen.query("#mc-model-arrow"))
    return bool(arrows) and bool(arrows[0].display)


def overlay(screen: ModelConfigScreen) -> OptionList:
    return screen.query_one("#mc-model-overlay", OptionList)


def overlay_expanded(screen: ModelConfigScreen) -> bool:
    return any(node.has_class("-expanded") for node in screen.query("#mc-model-overlay"))


def assert_overlay_visible(screen: ModelConfigScreen) -> None:
    """The menu is really drawn: focus alone passes for a ``display: none`` OptionList."""
    menu = overlay(screen)
    assert menu.display, "overlay has display: none"
    assert menu.region.height > 0, menu.region
    assert screen.region.contains_region(menu.region), (menu.region, screen.region)


def overlay_texts(screen: ModelConfigScreen) -> list[str]:
    menu = overlay(screen)
    return [str(menu.get_option_at_index(i).prompt) for i in range(menu.option_count)]


def picker(screen: ModelConfigScreen) -> Widget:
    return screen.query_one("#mc-model-picker")


def input_fills_picker(screen: ModelConfigScreen) -> bool:
    """No ▼: the model input takes the picker's whole width."""
    return model_input(screen).region.right == picker(screen).region.right


def model_input(screen: ModelConfigScreen) -> Input:
    return screen.query_one("#mc-model", Input)


def model_label(screen: ModelConfigScreen) -> str:
    return str(screen.query_one("#mc-model-label", Label).render())


def url_label(screen: ModelConfigScreen) -> str:
    return str(screen.query_one("#mc-url-label", Label).render())


# Progress and result hints live in rows below the inputs: #mc-url-hint under the Base URL input,
# #mc-model-hint under the model line; hidden and taking no space when empty.


def hint_row(screen: ModelConfigScreen, selector: str) -> str | None:
    """The text of the result row *selector*, or None while it is hidden."""
    row = screen.query_one(selector, Label)
    if not row.display:
        return None
    text = str(row.render())
    assert text, f"{selector} is shown but empty"
    return text


def url_hint(screen: ModelConfigScreen) -> str | None:
    """The correction hint under the Base URL input."""
    return hint_row(screen, "#mc-url-hint")


def url_hinted(screen: ModelConfigScreen) -> bool:
    """A correction hint is shown (the /v1 ones the tests use)."""
    return url_hint(screen) in (URL_ADDED_V1_EN, URL_REMOVED_V1_EN)


def _hint(screen: ModelConfigScreen) -> str | None:
    """What the row under the model input says: progress or a result."""
    return hint_row(screen, "#mc-model-hint")


def is_warning_row(screen: ModelConfigScreen, selector: str) -> bool:
    """*selector* is shown and drawn like the TLS warning, in the theme's warning colour."""
    row = screen.query_one(selector, Label)
    warning = Color.parse(screen.app.theme_variables["warning"])
    return (
        bool(row.display)
        and not row.has_class("-progress")
        and row.has_class("mc-tls-hint")
        and row.styles.color == screen.query_one("#mc-skip-tls-hint", Label).styles.color
        and row.styles.color.rgb == warning.rgb
    )


def row_shows_whole(screen: ModelConfigScreen, selector: str, text: str) -> bool:
    """The row wraps *text* over as many lines as it needs: every character drawn, no ellipsis."""
    row = screen.query_one(selector, Label)
    drawn = "".join(row.render_line(y).text for y in range(row.size.height))
    return "…" not in drawn and "".join(drawn.split()) == "".join(text.split())


def fetching_frame(screen: ModelConfigScreen, text: str = FETCHING_EN) -> str | None:
    """The spinner frame of the progress ``<frame> <text>``, or None when that progress is not shown."""
    hint = hint_row(screen, "#mc-model-hint")
    if hint is None:
        return None
    frame, separator, rest = hint.partition(" ")
    if not separator or rest != text or frame not in (*SPINNER_FRAMES, STATIC_SPINNER):
        return None
    return frame


def is_muted_progress(screen: ModelConfigScreen) -> bool:
    """The progress row is drawn muted, not in the warning colour."""
    row = screen.query_one("#mc-model-hint", Label)
    warning = Color.parse(screen.app.theme_variables["warning"])
    return bool(row.display) and row.has_class("-progress") and row.styles.color.rgb != warning.rgb


def screen_state(screen: ModelConfigScreen) -> tuple[object, ...]:
    button = fetch_button(screen)
    return (
        arrow_shown(screen),
        overlay_expanded(screen),
        model_label(screen),
        url_label(screen),
        str(screen.query_one("#mc-max-tokens-label", Label).render()),
        str(screen.query_one("#mc-max-output-label", Label).render()),
        button.disabled,
        str(button.label),
    )


# ── Actions and waits ────────────────────────────────────────────────


async def settle(screen: ModelConfigScreen, fake: FakeDiscover, pilot: Pilot) -> None:
    """Every started query has answered and the screen stopped changing."""
    await wait_for(lambda: fake.idle, pilot=pilot, description="fake queries answered")
    await wait_until_quiet(lambda: screen_state(screen), description="model row settled", pilot=pilot)


async def no_new_query(fake: FakeDiscover, pilot: Pilot, *, after: int) -> None:
    """Bounded wait: no query beyond the first *after* starts within the quiet window."""
    started = await wait_until(lambda: len(fake.calls) > after, timeout=QUIET_WINDOW, pilot=pilot)
    assert not started, [(s.base_url, s.api_key) for s in fake.calls]


async def bring_into_view(screen: ModelConfigScreen, widget: Widget, pilot: Pilot) -> None:
    """Scroll the form the way a user would until *widget* is inside its clipped content area.

    ``scroll_visible`` can stop with the widget on the form's bottom padding row, which draws
    nothing there and takes no click; a user scrolls further, and so does this helper.
    """
    scroll = screen.query_one("#mc-scroll")

    def inside() -> bool:
        return widget.region.area > 0 and scroll.scrollable_content_region.contains_region(widget.region)

    if not inside():
        scroll.scroll_to_center(widget, animate=False, immediate=True)
    await wait_for(inside, pilot=pilot, description=f"{widget.id} scrolled into view")


async def click_fetch(screen: ModelConfigScreen, pilot: Pilot) -> None:
    button = fetch_button(screen)
    await wait_for(lambda: not button.disabled, pilot=pilot, description="Fetch enabled")
    await bring_into_view(screen, button, pilot)
    await click_when_settled(pilot, button)


async def fetch_list(screen: ModelConfigScreen, pilot: Pilot) -> None:
    """Click Fetch and wait for the found list to open, as it does on success."""
    await click_fetch(screen, pilot)
    await wait_for(lambda: overlay_expanded(screen), pilot=pilot, description="list opened by Fetch")


async def fetch_and_settle(screen: ModelConfigScreen, fake: FakeDiscover, pilot: Pilot) -> None:
    """Click Fetch, wait for its lookup to answer and the button to be ready again."""
    before = len(fake.calls)
    await click_fetch(screen, pilot)
    await wait_for(lambda: len(fake.calls) > before, pilot=pilot, description="lookup started")
    await settle(screen, fake, pilot)
    await wait_for(lambda: fetch_ready(screen), pilot=pilot, description="Fetch ready again")


async def listed(screen: ModelConfigScreen, pilot: Pilot) -> list[str]:
    """The ids the dropdown offers, read with the list open.

    The list filters by the model input (§3.1): callers keep that input empty or an exact id.
    """
    was_open = overlay_expanded(screen)
    if not was_open:
        await click_when_settled(pilot, "#mc-model-arrow")
        await wait_for(lambda: overlay_expanded(screen), pilot=pilot)
    texts = overlay_texts(screen)
    if not was_open:
        await _close_list(screen, pilot)
    return texts


async def focus_handled(widget: Widget, pilot: Pilot) -> None:
    """Wait until *widget* has handled its Focus event.

    Input selects its whole value when that event lands; on a slow runner it can land after keys
    pressed right after ``has_focus`` turns true, and the selection then swallows the typed text.
    A callback queued on the widget runs after the Focus event already in its queue.
    """
    handled: list[bool] = []
    widget.call_later(handled.append, True)
    await wait_for(lambda: bool(handled), pilot=pilot, description="focus event handled")


async def type_into(pilot: Pilot, widget: Input, text: str) -> None:
    """Focus *widget* and type *text* after its value.

    End goes in a press of its own. Keys one press sends can reach the App back to back on a
    starved runner, and an Input runs its End binding only once the key has bubbled up to the
    App, while it inserts a typed character as soon as it reads it: the text would replace the
    value that focus selected. Each press returns once the App and the screen have handled its
    keys.
    """
    widget.focus()
    await wait_for(lambda: widget.has_focus, pilot=pilot)
    await focus_handled(widget, pilot)
    await pilot.press("end")
    await wait_for(lambda: widget.selection.is_empty and widget.cursor_position == len(widget.value), pilot=pilot)
    await pilot.press(*text)


async def _open_by_arrow(screen: ModelConfigScreen, pilot: Pilot) -> None:
    arrow = screen.query_one("#mc-model-arrow")
    await bring_into_view(screen, arrow, pilot)
    await click_when_settled(pilot, arrow)
    await wait_for(lambda: overlay_expanded(screen), pilot=pilot)


async def _open_by_down(screen: ModelConfigScreen, pilot: Pilot) -> None:
    model = model_input(screen)
    model.focus()
    await wait_for(lambda: model.has_focus, pilot=pilot)
    await focus_handled(model, pilot)
    await pilot.press("down")
    await wait_for(lambda: overlay_expanded(screen), pilot=pilot)


async def _close_list(screen: ModelConfigScreen, pilot: Pilot) -> None:
    name = screen.query_one("#mc-name")
    await bring_into_view(screen, name, pilot)
    await click_when_settled(pilot, name)
    await wait_for(lambda: not overlay_expanded(screen), pilot=pilot)


@contextlib.asynccontextmanager
async def open_model_config_zh(
    registry: ModelProfileRegistry, *, size: tuple[int, int] = (120, 40), **screen_kwargs: Any
) -> AsyncIterator[tuple[ModelConfigScreen, Pilot]]:
    async with open_model_config(registry, size=size, locale="zh-Hans", **screen_kwargs) as opened:
        yield opened


def _two_profiles() -> ModelProfileRegistry:
    registry = ModelProfileRegistry()
    for profile_id, name in (("a", "A"), ("b", "B")):
        registry.register(
            ModelProfile(
                id=profile_id,
                name=name,
                model_id="m1",
                base_url=f"https://{profile_id}.example.test/v1",
                api_key=f"sk-{profile_id}",
                max_context_tokens=200_000,
            )
        )
    return registry


async def _select_profile(screen: ModelConfigScreen, pilot: Pilot, profile_id: str) -> None:
    sidebar = screen.query_one("#mc-list", OptionList)
    sidebar.highlighted = sidebar.get_option_index(profile_id)
    sidebar.action_select()
    await wait_for(lambda: screen._selected_profile_id == profile_id, pilot=pilot)


def _without_default_url(monkeypatch: pytest.MonkeyPatch, provider: str) -> None:
    """Pretend *provider* is a future one with no default address."""
    monkeypatch.setitem(PROVIDERS, provider, dataclasses.replace(PROVIDERS[provider], default_base_url=""))
