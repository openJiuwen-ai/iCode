# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Local web metadata renders literally and unknown payloads keep text fallback."""

from __future__ import annotations

from textual.app import App, ComposeResult
from textual.widgets import Static

from chrys.app.tui.screens.agents.panels.tools import ToolsConfigPanel
from chrys.app.tui.widgets.chat.panel import ChatPanel
from chrys.app.tui.widgets.chat.renderers.web import WebToolCall
from chrys.app.tui.widgets.chat.tool_call import ToolGroup
from chrys.app.tui.widgets.chat.tool_renderers import create_tool_widget
from chrys.foundation.tool_kinds import KIND_WEB_FETCH, KIND_WEB_SEARCH
from chrys.foundation.tool_result_metadata import TOOL_RESULT_METADATA_KEY
from chrys.service.profiles.agents.schema import ToolsConfig
from tests.support.waiting import wait_for


async def test_web_sources_are_literal_and_keep_result_for_copy():
    widget = create_tool_widget("call", "web_search", "web_search", args={"query": "[question]"})
    assert isinstance(widget, WebToolCall)

    class Host(App):
        def compose(self) -> ComposeResult:
            yield widget

    async with Host().run_test():
        widget.set_complete(
            "unparseable result",
            metadata={
                "web_search": {
                    "provider": "main",
                    "results": [{"title": "[red]literal", "url": "https://example.com/"}],
                }
            },
        )
        assert "[red]literal" in str(widget.query_one("#tc-body", Static).content)
        assert widget.result_text == "unparseable result"
        widget.set_complete("fallback text", metadata={})
        assert "fallback text" in str(widget.query_one("#tc-body", Static).content)


async def test_web_cards_strip_terminal_control_sequences():
    search = create_tool_widget("call-a", "web_search", "web_search", args={"query": "q"})
    fetch = create_tool_widget("call-b", "web_fetch", "web_fetch", args={"url": "https://example.com/"})
    assert isinstance(search, WebToolCall)
    assert isinstance(fetch, WebToolCall)

    class Host(App):
        def compose(self) -> ComposeResult:
            yield search
            yield fetch

    async with Host().run_test():
        search.set_complete(
            "result",
            metadata={
                "web_search": {
                    "provider": "prov\x1b]0;pwned",
                    "results": [{"title": "\x1b]52;c;evil\x1b\\ title", "url": "https://example.com/"}],
                }
            },
        )
        rendered = str(search.query_one("#tc-body", Static).content)
        assert "\x1b" not in rendered
        assert "\ufffd" in rendered
        assert "title" in rendered

        fetch.set_complete(
            "body \x1b[2Jclear\x07bell",
            metadata={"web_fetch_final_url": "https://example.com/final"},
        )
        rendered = str(fetch.query_one("#tc-body", Static).content)
        assert "\x1b" not in rendered
        assert "\x07" not in rendered
        assert "\ufffd" in rendered
        assert "clear" in rendered


async def test_tools_panel_save_preserves_web_categories():
    panel = ToolsConfigPanel(ToolsConfig(builtins=["web_search", "web_fetch", "search"]))

    class Host(App):
        def compose(self) -> ComposeResult:
            yield panel

    async with Host().run_test():
        assert set(panel.get_config().builtins) == {"web_search", "web_fetch", "search"}


async def test_tools_panel_save_keeps_categories_it_has_no_switch_for():
    panel = ToolsConfigPanel(ToolsConfig(builtins=["search", "future_tool", "future_tool"]))

    class Host(App):
        def compose(self) -> ComposeResult:
            yield panel

    async with Host().run_test():
        assert panel.get_config().builtins == ["search", "future_tool"]


async def test_web_search_card_drops_sources_whose_url_carries_c1_controls():
    widget = create_tool_widget("call", "web_search", "web_search", args={"query": "q"})
    assert isinstance(widget, WebToolCall)

    class Host(App):
        def compose(self) -> ComposeResult:
            yield widget

    async with Host().run_test():
        widget.set_complete(
            "plain result",
            metadata={
                "web_search": {
                    "provider": "main",
                    "results": [{"title": "title", "url": "https://example.com/\x9b31mred"}],
                }
            },
        )
        rendered = str(widget.query_one("#tc-body", Static).content)
        assert "\x9b" not in rendered
        assert "plain result" in rendered


async def test_web_search_card_titles_drop_bidi_and_zero_width_characters():
    widget = create_tool_widget("call", "web_search", "web_search", args={"query": "q"})
    assert isinstance(widget, WebToolCall)
    rlo, zwsp, zwj = chr(0x202E), chr(0x200B), chr(0x200D)

    class Host(App):
        def compose(self) -> ComposeResult:
            yield widget

    async with Host().run_test():
        widget.set_complete(
            "result",
            metadata={
                "web_search": {
                    "provider": "main",
                    "results": [{"title": f"safe{rlo}exe.txt{zwsp} family{zwj}", "url": "https://example.com/"}],
                }
            },
        )
        rendered = str(widget.query_one("#tc-body", Static).content)
        assert rlo not in rendered
        assert zwsp not in rendered
        # Joiners are part of how scripts and emoji are written.
        assert f"safeexe.txt family{zwj}" in rendered


async def test_replayed_web_cards_render_from_the_persisted_metadata():
    """Reopening a session shows the same sources as the live card, not the JSON the model read."""

    def exchange(call_id: str, name: str, arguments: dict, result: str, metadata: dict) -> list[dict]:
        return [
            {
                "role": "assistant",
                "contents": [{"type": "function_call", "name": name, "call_id": call_id, "arguments": arguments}],
            },
            {
                "role": "tool",
                "contents": [
                    {
                        "type": "function_result",
                        "call_id": call_id,
                        "result": result,
                        "additional_properties": {TOOL_RESULT_METADATA_KEY: metadata},
                    }
                ],
            },
        ]

    messages = [
        {"role": "user", "contents": [{"type": "text", "text": "look it up"}]},
        *exchange(
            "search-1",
            "web_search",
            {"query": "q"},
            '{"query": "q", "provider": "exa", "results": [{"title": "Found", "url": "https://example.com/a"}]}',
            {"web_search": {"provider": "exa", "results": [{"title": "Found", "url": "https://example.com/a"}]}},
        ),
        *exchange(
            "fetch-1",
            "web_fetch",
            {"url": "https://example.com/a", "prompt": "read"},
            "Fetched content from https://example.com/final (status 200, text/html).",
            {"web_fetch_final_url": "https://example.com/final"},
        ),
    ]

    class PanelApp(App):
        def compose(self) -> ComposeResult:
            yield ChatPanel()

    async with PanelApp().run_test() as pilot:
        panel = pilot.app.query_one(ChatPanel)
        panel.set_tool_kinds({"web_search": KIND_WEB_SEARCH, "web_fetch": KIND_WEB_FETCH})
        await panel.replay_history(messages)
        panel.query_one(ToolGroup).collapsed = False

        def bodies() -> dict[str, str]:
            return {
                card.tool_name: str(card.query_one("#tc-body", Static).content) for card in panel.query(WebToolCall)
            }

        await wait_for(lambda: len(bodies()) == 2, pilot=pilot, description="both web cards mounted")
        rendered = bodies()
        assert rendered["web_search"].startswith("exa · 1\nFound\nhttps://example.com/a")
        assert '"provider"' not in rendered["web_search"]
        assert rendered["web_fetch"].startswith("https://example.com/final\n")
