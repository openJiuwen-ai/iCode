# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Hosted-tool renderer and lifecycle tests."""

from __future__ import annotations

import pytest
from rich.text import Text
from textual.app import App, ComposeResult
from textual.widgets import Static

from chrys.app.tui.widgets.chat.renderers.execute import ExecuteToolCall
from chrys.app.tui.widgets.chat.renderers.hosted_code import HostedCodeToolCall
from chrys.app.tui.widgets.chat.renderers.hosted_generic import HostedToolCall, HostedToolDiscoveryCall
from chrys.app.tui.widgets.chat.renderers.hosted_image import HostedImageToolCall
from chrys.app.tui.widgets.chat.renderers.hosted_mcp import HostedMcpToolCall
from chrys.app.tui.widgets.chat.renderers.hosted_search import HostedSearchToolCall
from chrys.app.tui.widgets.chat.renderers.hosted_shell import HostedShellToolCall
from chrys.app.tui.widgets.chat.tool_call import ToolGroup
from chrys.app.tui.widgets.chat.tool_renderers import create_tool_widget
from chrys.foundation.hosted_tools import HostedToolFamily
from chrys.foundation.tool_kinds import KIND_SHELL
from chrys.foundation.tool_result_metadata import (
    TOOL_ERRORED_METADATA_KEY,
    TOOL_FAILURE_TEXT_SYNTHESIZED_METADATA_KEY,
)
from chrys.kernel import Content
from chrys.service.agent_middleware.events.hosted_tools import adapt_hosted_tool
from tests.support.oversized_int import oversized_integer_json
from tests.support.waiting import wait_for

_TINY_PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4z8AAAAMBAQDJ/pLvAAAAAElFTkSuQmCC"

_FAMILY_RENDERERS = [
    (HostedToolFamily.SEARCH, HostedSearchToolCall),
    (HostedToolFamily.FETCH, HostedSearchToolCall),
    (HostedToolFamily.MCP, HostedMcpToolCall),
    (HostedToolFamily.CODE, HostedCodeToolCall),
    (HostedToolFamily.IMAGE, HostedImageToolCall),
    (HostedToolFamily.SHELL, HostedShellToolCall),
    (HostedToolFamily.TOOL_DISCOVERY, HostedToolDiscoveryCall),
    (HostedToolFamily.FILE_OPERATION, HostedToolCall),
    (HostedToolFamily.GENERIC, HostedToolCall),
]


def _plain(static: Static) -> str:
    content = static.content
    return content.plain if isinstance(content, Text) else str(content)


@pytest.mark.parametrize(
    ("family", "expected_type"),
    _FAMILY_RENDERERS,
)
def test_hosted_family_registry_uses_rich_renderer(
    family: HostedToolFamily,
    expected_type: type[HostedToolCall],
) -> None:
    widget = create_tool_widget(
        "hosted:1",
        "bash",
        KIND_SHELL,
        provider_hosted=True,
        hosted_family=family,
        provider="[provider]",
        provider_item_type="server_item",
        provider_status="running",
        provider_call_id="provider_1",
    )

    assert type(widget) is expected_type
    assert widget.provider == "[provider]"
    assert widget.hosted_family == family


def test_unknown_hosted_family_uses_generic_renderer() -> None:
    widget = create_tool_widget(
        "hosted:1",
        "remote_tool",
        "",
        provider_hosted=True,
        hosted_family="future_family",
    )

    assert type(widget) is HostedToolCall


_FAMILY_TITLE_CASES = [
    (HostedToolFamily.SEARCH, "Hosted Search"),
    (HostedToolFamily.FETCH, "Hosted Fetch"),
    (HostedToolFamily.MCP, "Hosted MCP"),
    (HostedToolFamily.CODE, "Hosted Code"),
    (HostedToolFamily.IMAGE, "Hosted Image"),
    (HostedToolFamily.SHELL, "Hosted Shell"),
    (HostedToolFamily.TOOL_DISCOVERY, "Hosted Tool Discovery"),
    (HostedToolFamily.FILE_OPERATION, "Hosted File Operation"),
    (HostedToolFamily.GENERIC, "Hosted Tool"),
]


@pytest.mark.parametrize(
    ("family", "expected_title"),
    _FAMILY_TITLE_CASES,
)
def test_empty_hosted_identifiers_use_family_message_title(
    family: HostedToolFamily,
    expected_title: str,
) -> None:
    widget = create_tool_widget(
        "hosted:family-title",
        "",
        "",
        provider_hosted=True,
        hosted_family=family,
    )

    assert isinstance(widget, HostedToolCall)
    assert widget._title_text() == expected_title


@pytest.mark.parametrize("family", ["", "future_family"], ids=["absent", "unknown"])
def test_unrecognized_hosted_family_keeps_literal_tool_fallback(family: str) -> None:
    widget = create_tool_widget(
        "hosted:unknown-title",
        "",
        "",
        provider_hosted=True,
        hosted_family=family,
    )

    assert isinstance(widget, HostedToolCall)
    assert widget._title_text() == "tool"


def test_hosted_tool_name_remains_authoritative_over_family_title() -> None:
    widget = create_tool_widget(
        "hosted:named-title",
        "remote_tool",
        "",
        provider_hosted=True,
        hosted_family=HostedToolFamily.SEARCH,
        provider="openai",
        provider_item_type="web_search_call",
    )

    assert isinstance(widget, HostedToolCall)
    assert widget._title_text() == "openai/remote_tool"


def test_hosted_adapter_keeps_oversized_integer_arguments_as_raw_input() -> None:
    """An integer past Python's conversion limit cannot be decoded; the raw text is exposed."""
    arguments = oversized_integer_json()

    view = adapt_hosted_tool(
        Content.from_hosted_tool_call(
            "hosted:big-int",
            tool_name="search",
            arguments=arguments,
            hosted_family=HostedToolFamily.SEARCH,
            hosted_provider="openai",
        )
    )

    assert view.arguments == {"input": arguments}


@pytest.mark.parametrize(
    "family",
    [case[0] for case in _FAMILY_TITLE_CASES],
)
def test_adapter_backfilled_family_code_name_renders_raw_code(
    family: HostedToolFamily,
) -> None:
    # The hosted adapter substitutes the family code when a provider omits the
    # tool name, so a natural event arrives with tool_name == family. The card
    # keeps that code as the wire-level identity instead of promoting it to
    # the localized family title.
    view = adapt_hosted_tool(
        Content.from_hosted_tool_call(
            "hosted:backfilled-title",
            tool_name="",
            hosted_family=family.value,
            hosted_provider="openai",
        )
    )
    assert view.tool_name == family.value

    widget = create_tool_widget(
        "hosted:backfilled-title",
        view.tool_name,
        "",
        provider_hosted=True,
        hosted_family=family.value,
        provider="openai",
    )

    assert isinstance(widget, HostedToolCall)
    assert widget._title_text() == f"openai/{family.value}"


def test_hosted_name_never_falls_back_to_same_name_local_renderer() -> None:
    hosted = create_tool_widget(
        "hosted:1",
        "bash",
        KIND_SHELL,
        provider_hosted=True,
        hosted_family="shell",
    )
    local = create_tool_widget("local:1", "bash", KIND_SHELL)

    assert isinstance(hosted, HostedToolCall)
    assert isinstance(local, ExecuteToolCall)


class _ToolGroupApp(App[None]):
    def compose(self) -> ComposeResult:
        yield ToolGroup()


class _HostedCardApp(App[None]):
    def __init__(self, card: HostedToolCall) -> None:
        super().__init__()
        self.card = card

    def compose(self) -> ComposeResult:
        yield self.card


@pytest.mark.parametrize(("family", "expected_type"), _FAMILY_RENDERERS)
@pytest.mark.asyncio
async def test_hosted_renderers_treat_hostile_provider_text_as_literal(
    family: HostedToolFamily,
    expected_type: type[HostedToolCall],
) -> None:
    hostile_provider = "[red]x[/red] [[ ]]"
    hostile_value = "]] [bold]literal[/bold] [["
    card = create_tool_widget(
        "hosted:1",
        "[[tool]]",
        KIND_SHELL,
        args={"query": hostile_value, "server": hostile_value, "commands": [hostile_value]},
        provider_hosted=True,
        hosted_family=family,
        provider=hostile_provider,
        provider_item_type="[[item]]",
        provider_status="running",
    )
    assert type(card) is expected_type
    assert isinstance(card, HostedToolCall)

    async with _HostedCardApp(card).run_test() as pilot:
        card.set_complete(
            f"output {hostile_value}",
            metadata={"stdout": hostile_value},
            artifacts=[{"path": hostile_value}],
        )
        await pilot.pause()

        label = _plain(card.query_one("#tc-label", Static))
        body = _plain(card.query_one("#tc-body", Static))
        assert "[[" in label or "[[" in body
        assert "[bold]literal[/bold]" in body
        assert card.styles.padding.left == 2


@pytest.mark.asyncio
async def test_hosted_provider_status_renders_as_literal_text() -> None:
    card = create_tool_widget(
        "hosted:1",
        "remote_tool",
        "",
        provider_hosted=True,
        hosted_family="generic",
        provider_status="x[/bold]",
    )
    assert isinstance(card, HostedToolCall)

    async with _HostedCardApp(card).run_test() as pilot:
        card.update_hosted_status("running", "x[/bold]")
        await pilot.pause()

        subtitle = card.query_one("#tc-panel").border_subtitle
        assert subtitle is not None
        assert Text.from_markup(str(subtitle)).plain == "X[/bold]"


@pytest.mark.parametrize(("family", "expected_type"), _FAMILY_RENDERERS)
@pytest.mark.asyncio
async def test_failed_provider_status_renders_error_for_every_hosted_family(
    family: HostedToolFamily,
    expected_type: type[HostedToolCall],
) -> None:
    card = create_tool_widget(
        "hosted:1",
        "remote_tool",
        "",
        provider_hosted=True,
        hosted_family=family,
        provider_status="failed",
        canonical_status="failed",
    )
    assert type(card) is expected_type
    assert isinstance(card, HostedToolCall)

    async with _HostedCardApp(card).run_test() as pilot:
        card.set_complete(
            "Error: provider failed",
            metadata={TOOL_ERRORED_METADATA_KEY: True, "provider_status": "failed"},
        )
        await pilot.pause()

        assert card.status == "error"
        assert card.has_class("-error")
        assert "Error: provider failed" in _plain(card.query_one("#tc-body", Static))
        assert card.query_one("#tc-panel").border_subtitle == "Failed"


@pytest.mark.asyncio
async def test_synthesized_failure_result_resolves_message_definition() -> None:
    card = create_tool_widget(
        "hosted:synthesized-result",
        "remote_tool",
        "",
        provider_hosted=True,
        hosted_family="generic",
        canonical_status="failed",
    )
    assert isinstance(card, HostedToolCall)

    async with _HostedCardApp(card).run_test() as pilot:
        card.set_complete(
            "Error: stale compatibility text",
            metadata={
                TOOL_ERRORED_METADATA_KEY: True,
                TOOL_FAILURE_TEXT_SYNTHESIZED_METADATA_KEY: True,
            },
        )
        await pilot.pause()

        assert _plain(card.query_one("#tc-body", Static)) == "Result:\nError: Provider-hosted tool failed."


@pytest.mark.asyncio
async def test_provider_failure_result_remains_literal_without_synthesized_signal() -> None:
    card = create_tool_widget(
        "hosted:provider-result",
        "remote_tool",
        "",
        provider_hosted=True,
        hosted_family="generic",
        canonical_status="failed",
    )
    assert isinstance(card, HostedToolCall)

    async with _HostedCardApp(card).run_test() as pilot:
        card.set_complete(
            "Error: provider-specific failure",
            metadata={TOOL_ERRORED_METADATA_KEY: True},
        )
        await pilot.pause()

        assert _plain(card.query_one("#tc-body", Static)) == "Result:\nError: provider-specific failure"


@pytest.mark.asyncio
async def test_synthesized_failure_status_resolves_message_definition() -> None:
    async with _ToolGroupApp().run_test() as pilot:
        group = pilot.app.query_one(ToolGroup)
        await group.add_tool(
            "hosted:synthesized-status",
            "remote_tool",
            "",
            provider_hosted=True,
            hosted_family="generic",
        )

        group.update_tool_status(
            "hosted:synthesized-status",
            "failed",
            metadata={
                "result_text": "Error: stale compatibility text",
                TOOL_FAILURE_TEXT_SYNTHESIZED_METADATA_KEY: True,
            },
        )
        await pilot.pause()

        card = group.get_tool("hosted:synthesized-status")
        assert isinstance(card, HostedToolCall)
        assert _plain(card.query_one("#tc-body", Static)) == "Error: Provider-hosted tool failed."


@pytest.mark.asyncio
async def test_search_renderer_shows_query_count_citations_and_url() -> None:
    card = create_tool_widget(
        "hosted:1",
        "web_search",
        "search",
        args={"type": "search", "query": "Chrys"},
        provider_hosted=True,
        hosted_family="search",
        provider="openai",
        provider_status="completed",
        canonical_status="completed",
    )
    assert isinstance(card, HostedSearchToolCall)

    async with _HostedCardApp(card).run_test() as pilot:
        card.set_complete('{"results":[{"title":"Docs","url":"https://example.test/docs"}]}')
        await pilot.pause()

        assert _plain(card.query_one("#tc-label", Static)) == "• openai/web_search · Chrys · 1 result"
        assert str(card.query_one("#tc-panel").border_title) == "search"
        assert card.query_one("#tc-panel").border_subtitle == "Completed"
        body = _plain(card.query_one("#tc-body", Static))
        assert "Citations and URLs" in body
        assert "https://example.test/docs" in body


@pytest.mark.asyncio
async def test_search_renderer_uses_nested_action_type_and_hides_result_echo() -> None:
    url = "https://example.test/docs"
    card = create_tool_widget(
        "hosted:1",
        "web_search",
        "search",
        args={"action": {"type": "open_page", "url": url}},
        provider_hosted=True,
        hosted_family="search",
        provider="openai",
        provider_status="completed",
        canonical_status="completed",
    )
    assert isinstance(card, HostedSearchToolCall)

    async with _HostedCardApp(card).run_test() as pilot:
        card.set_complete(f'{{"action":{{"type":"open_page","url":"{url}"}}}}')
        await pilot.pause()

        assert str(card.query_one("#tc-panel").border_title) == "open_page"
        body = _plain(card.query_one("#tc-body", Static))
        assert f"Query/action: {url}" in body
        assert "Result:" not in body


@pytest.mark.asyncio
async def test_search_renderer_failed_action_echo_keeps_status_without_fake_error_details() -> None:
    url = "https://example.test/private"
    card = create_tool_widget(
        "hosted:1",
        "web_search",
        "search",
        args={"type": "open_page", "url": url},
        provider_hosted=True,
        hosted_family="search",
        provider="deepseek-openai",
        provider_status="failed",
        canonical_status="failed",
    )
    assert isinstance(card, HostedSearchToolCall)

    async with _HostedCardApp(card).run_test() as pilot:
        card.set_complete(
            f'Error: {{"action":{{"type":"open_page","url":"{url}"}}}}',
            metadata={TOOL_ERRORED_METADATA_KEY: True, "provider_status": "failed"},
        )
        await pilot.pause()

        assert str(card.query_one("#tc-panel").border_title) == "open_page"
        assert card.query_one("#tc-panel").border_subtitle == "Failed"
        body = _plain(card.query_one("#tc-body", Static))
        assert f"Query/action: {url}" in body
        assert "Result:" not in body


@pytest.mark.asyncio
async def test_terminal_item_args_land_after_a_status_only_completion() -> None:
    # OpenAI web_search starts with an empty action; the query arrives with the
    # terminal item, after the status-only "completed" transition.
    async with _ToolGroupApp().run_test() as pilot:
        group = pilot.app.query_one(ToolGroup)
        await group.add_tool(
            "hosted:1",
            "web_search",
            "search",
            args={},
            provider_hosted=True,
            hosted_family="search",
            provider="openai",
            provider_status="searching",
        )
        group.update_tool_status("hosted:1", "completed", provider_status="completed")
        group.update_tool_args("hosted:1", {"type": "search", "queries": ["chrys docs"]})
        group.complete_tool("hosted:1", "", provider_status="completed", canonical_status="completed")
        await pilot.pause()

        card = group.get_tool("hosted:1")
        assert isinstance(card, HostedSearchToolCall)
        assert "chrys docs" in _plain(card.query_one("#tc-label", Static))
        title = card.query_one("#tc-panel").border_title
        assert (title.plain if isinstance(title, Text) else str(title)) == "search"
        assert group._tool_records["hosted:1"].args == {"type": "search", "queries": ["chrys docs"]}


@pytest.mark.asyncio
async def test_status_only_completion_rebuilds_as_completed_after_release() -> None:
    # A card completed by status transition alone keeps completed_via_result
    # False (a late terminal result may still land), so the release/rebuild
    # cycle must read canonical_status instead of resurrecting it as running.
    async with _ToolGroupApp().run_test() as pilot:
        group = pilot.app.query_one(ToolGroup)
        await group.add_tool(
            "hosted:1",
            "web_search",
            "search",
            args={},
            provider_hosted=True,
            hosted_family="search",
            provider="openai",
            provider_status="searching",
        )
        group.update_tool_status("hosted:1", "completed", provider_status="completed")
        await pilot.pause()
        original = group.get_tool("hosted:1")

        group.collapsed = True
        await wait_for(
            lambda: not group._content_mounted,
            pilot=pilot,
            description="collapsed hosted tool subtree is removed",
        )
        group.collapsed = False
        await wait_for(
            lambda: group._content_mounted and group.get_tool("hosted:1") is not None,
            pilot=pilot,
            description="expanded hosted tool subtree is restored",
        )

        card = group.get_tool("hosted:1")
        assert isinstance(card, HostedSearchToolCall)
        assert card is not original
        assert card.query_one("#tc-panel").border_subtitle == "Completed"


@pytest.mark.asyncio
async def test_mcp_renderer_shows_server_tool_arguments_and_output() -> None:
    card = create_tool_widget(
        "hosted:1",
        "lookup",
        "mcp",
        args={"server": "docs", "query": "Chrys"},
        provider_hosted=True,
        hosted_family="mcp",
        provider_status="completed",
        canonical_status="completed",
    )
    assert isinstance(card, HostedMcpToolCall)

    async with _HostedCardApp(card).run_test() as pilot:
        card.set_complete('{"answer":"found"}')
        await pilot.pause()

        assert _plain(card.query_one("#tc-label", Static)) == "• lookup · docs"
        assert card.query_one("#tc-panel").border_subtitle == "Completed"
        body = _plain(card.query_one("#tc-body", Static))
        assert "Arguments:" in body
        assert '"query": "Chrys"' in body
        assert '"answer":"found"' in body


@pytest.mark.asyncio
async def test_code_renderer_shows_code_streams_files_and_images() -> None:
    image = {"type": "data", "uri": f"data:image/png;base64,{_TINY_PNG}", "media_type": "image/png"}
    card = create_tool_widget(
        "hosted:1",
        "code",
        "",
        args={"language": "python", "code": "print('hi')"},
        provider_hosted=True,
        hosted_family="code",
        provider="anthropic",
        provider_status="completed",
        canonical_status="completed",
    )
    assert isinstance(card, HostedCodeToolCall)

    async with _HostedCardApp(card).run_test() as pilot:
        card.set_complete(
            "",
            metadata={"stdout": "hi", "stderr": "warning"},
            artifacts=[{"path": "report.csv", "mime": "text/csv"}],
            image_contents=[image],
        )
        await pilot.pause()

        assert _plain(card.query_one("#tc-label", Static)) == "• anthropic/code"
        assert str(card.query_one("#tc-panel").border_title) == "code"
        assert card.query_one("#tc-panel").border_subtitle == "Completed"
        body = _plain(card.query_one("#tc-body", Static))
        assert "Code/input:" in body
        assert "stdout:\nhi" in body
        assert "stderr:\nwarning" in body
        assert "report.csv" in body
        assert "Images: 1" in body


@pytest.mark.asyncio
async def test_image_partial_progress_refreshes_preview_without_completing() -> None:
    image = {"type": "data", "uri": f"data:image/png;base64,{_TINY_PNG}", "media_type": "image/png"}
    async with _ToolGroupApp().run_test() as pilot:
        group = pilot.app.query_one(ToolGroup)
        await group.add_tool(
            "hosted:1",
            "image_generation",
            "",
            args={"prompt": "a chrysanthemum"},
            provider_hosted=True,
            hosted_family="image",
            provider="openai",
            provider_status="generating",
        )

        group.update_tool_progress(
            "hosted:1",
            [],
            image_contents=[image],
            snapshot_metadata={"partial_index": 1},
            provider_status="generating",
        )
        await pilot.pause()
        card = group.get_tool("hosted:1")
        assert isinstance(card, HostedImageToolCall)
        assert card.status == "running"
        assert group._tool_records["hosted:1"].status == "running"
        assert len(card.image_contents) == 1
        card._spin()
        assert "Partial preview" in _plain(card.query_one("#tc-body", Static))
        assert len(card.query("#tc-images")) == 1

        group.complete_tool(
            "hosted:1",
            "created",
            image_contents=[image],
            metadata={"quality": "high"},
            provider_status="completed",
            canonical_status="completed",
        )
        await pilot.pause()

        assert card.status == "complete"
        body = _plain(card.query_one("#tc-body", Static))
        assert "Prompt/operation:" in body
        assert "Result:" in body
        assert "Final previews" not in body
        assert "Metadata:" not in body
        assert card.query_one("#tc-body", Static).display is True
        assert card.metadata == {"quality": "high"}
        assert len(card.query("#tc-images")) == 1


@pytest.mark.asyncio
async def test_progress_preview_survives_spinner_tick_and_releases_on_empty() -> None:
    async with _ToolGroupApp().run_test() as pilot:
        group = pilot.app.query_one(ToolGroup)
        await group.add_tool(
            "hosted:1",
            "web_search",
            "",
            provider_hosted=True,
            hosted_family="search",
            provider="openai",
            provider_status="searching",
        )
        group.update_tool_progress("hosted:1", ["Searching: chrysanthemum"], provider_status="searching")
        await pilot.pause()
        card = group.get_tool("hosted:1")
        card._spin()
        assert "Searching: chrysanthemum" in _plain(card.query_one("#tc-body", Static))

        group.update_tool_progress("hosted:1", [], provider_status="searching")
        card._spin()
        assert "running" in _plain(card.query_one("#tc-body", Static))


@pytest.mark.asyncio
async def test_status_only_completion_preserves_partial_image_snapshot() -> None:
    image = {"type": "data", "uri": f"data:image/png;base64,{_TINY_PNG}", "media_type": "image/png"}
    async with _ToolGroupApp().run_test() as pilot:
        group = pilot.app.query_one(ToolGroup)
        await group.add_tool(
            "hosted:1",
            "image_generation",
            "",
            provider_hosted=True,
            hosted_family="image",
            provider="openai",
            provider_status="generating",
        )
        group.update_tool_progress(
            "hosted:1",
            [],
            image_contents=[image],
            provider_status="generating",
        )

        group.update_tool_status("hosted:1", "completed", provider_status="completed")
        await pilot.pause()

        record = group._tool_records["hosted:1"]
        card = group.get_tool("hosted:1")
        assert isinstance(card, HostedImageToolCall)
        assert record.image_contents == [image]
        assert card.image_contents == [image]
        body = _plain(card.query_one("#tc-body", Static))
        assert body == ""
        assert "Final previews" not in body
        assert "Metadata:" not in body
        assert card.query_one("#tc-body", Static).display is False
        assert len(card.query("#tc-images")) == 1


@pytest.mark.asyncio
async def test_status_only_failure_preserves_partial_image_after_release() -> None:
    image = {"type": "data", "uri": f"data:image/png;base64,{_TINY_PNG}", "media_type": "image/png"}
    async with _ToolGroupApp().run_test() as pilot:
        group = pilot.app.query_one(ToolGroup)
        await group.add_tool(
            "hosted:1",
            "image_generation",
            "",
            provider_hosted=True,
            hosted_family="image",
            provider="openai",
            provider_status="generating",
        )
        group.update_tool_progress(
            "hosted:1",
            [],
            image_contents=[image],
            snapshot_metadata={"partial_index": 1},
            provider_status="generating",
        )
        group.update_tool_status(
            "hosted:1",
            "failed",
            provider_status="failed",
            metadata={"result_text": "Error: generation failed"},
        )
        await pilot.pause()
        original = group.get_tool("hosted:1")
        assert isinstance(original, HostedImageToolCall)
        assert len(original.query("#tc-images")) == 1

        group.collapsed = True
        await wait_for(
            lambda: not group._content_mounted,
            pilot=pilot,
            description="collapsed hosted tool subtree is removed",
        )
        group.collapsed = False
        await wait_for(
            lambda: group._content_mounted and group.get_tool("hosted:1") is not None,
            pilot=pilot,
            description="expanded hosted tool subtree is restored",
        )

        card = group.get_tool("hosted:1")
        assert isinstance(card, HostedImageToolCall)
        assert card is not original
        assert card.status == "error"
        assert card.image_contents == [image]
        assert card.metadata == {"partial_index": 1, "result_text": "Error: generation failed"}
        assert len(card.query("#tc-images")) == 1
        assert "generation failed" in _plain(card.query_one("#tc-body", Static))


@pytest.mark.asyncio
async def test_image_terminal_status_overrides_stale_provider_generating_status() -> None:
    image = {"type": "data", "uri": f"data:image/png;base64,{_TINY_PNG}", "media_type": "image/png"}
    card = create_tool_widget(
        "hosted:1",
        "image",
        "",
        provider_hosted=True,
        hosted_family="image",
        provider="openai",
        provider_status="generating",
        canonical_status="completed",
    )
    assert isinstance(card, HostedImageToolCall)

    async with _HostedCardApp(card).run_test() as pilot:
        card.set_complete(
            "",
            image_contents=[image],
            provider_status="generating",
            canonical_status="completed",
        )
        await pilot.pause()

        assert card.query_one("#tc-panel").border_subtitle == "Completed"


@pytest.mark.asyncio
async def test_shell_renderer_shows_commands_streams_exit_timeout_and_files() -> None:
    card = create_tool_widget(
        "hosted:1",
        "bash",
        KIND_SHELL,
        args={"commands": ["printf hi", "pwd"]},
        provider_hosted=True,
        hosted_family="shell",
        provider="anthropic",
        provider_status="completed",
        canonical_status="completed",
    )
    assert isinstance(card, HostedShellToolCall)
    assert not isinstance(card, ExecuteToolCall)

    async with _HostedCardApp(card).run_test() as pilot:
        card.set_complete(
            "",
            metadata={"stdout": "hi", "stderr": "warn", "exit_code": 7, "timed_out": False},
            artifacts=[{"path": "output.txt", "size": 12}],
        )
        await pilot.pause()

        assert _plain(card.query_one("#tc-label", Static)) == "• anthropic/bash · printf hi +1 · exit 7"
        assert card.query_one("#tc-panel").border_subtitle == "Completed"
        body = _plain(card.query_one("#tc-body", Static))
        assert "Commands:\nprintf hi\npwd" in body
        assert "stdout:\nhi" in body
        assert "stderr:\nwarn" in body
        assert "Exit: 7" in body
        assert "Timed out: no" in body
        assert "output.txt" in body


@pytest.mark.asyncio
async def test_tool_discovery_renderer_shows_query_and_discovered_count() -> None:
    card = create_tool_widget(
        "hosted:1",
        "tool_search",
        "",
        args={"namespace": "weather", "query": "forecast"},
        provider_hosted=True,
        hosted_family="tool_discovery",
        provider_status="completed",
        canonical_status="completed",
    )
    assert isinstance(card, HostedToolDiscoveryCall)

    async with _HostedCardApp(card).run_test() as pilot:
        card.set_complete('{"tools":[{"name":"current"},{"name":"forecast"}]}')
        await pilot.pause()

        assert _plain(card.query_one("#tc-label", Static)) == "• tool_search · weather · 2 discovered"
        assert card.query_one("#tc-panel").border_subtitle == "Completed"
        assert "Discovered tools:" in _plain(card.query_one("#tc-body", Static))


@pytest.mark.asyncio
async def test_hosted_output_is_bounded() -> None:
    card = create_tool_widget(
        "hosted:1",
        "bash",
        KIND_SHELL,
        provider_hosted=True,
        hosted_family="shell",
    )
    assert isinstance(card, HostedShellToolCall)

    async with _HostedCardApp(card).run_test() as pilot:
        card.set_complete("", metadata={"stdout": "x" * 5_000})
        await pilot.pause()

        body = _plain(card.query_one("#tc-body", Static))
        assert len(body) < 600
        assert body.endswith("…")


@pytest.mark.asyncio
async def test_hosted_statuses_are_monotonic_and_late_progress_is_ignored() -> None:
    async with _ToolGroupApp().run_test() as pilot:
        group = pilot.app.query_one(ToolGroup)
        await group.add_tool(
            "hosted:1",
            "web_search",
            "search",
            provider_hosted=True,
            hosted_family="search",
            provider="openai",
            provider_status="queued",
            canonical_status="pending",
        )

        group.update_tool_status("hosted:1", "running", provider_status="searching")
        group.update_tool_status("hosted:1", "pending", provider_status="queued")
        assert group._tool_records["hosted:1"].canonical_status == "running"
        assert group._tool_records["hosted:1"].provider_status == "searching"
        group.update_tool_status("hosted:1", "completed", provider_status="completed")
        group.update_tool_progress("hosted:1", ["late"], provider_status="searching")
        group.update_tool_status("hosted:1", "running", provider_status="searching")

        record = group._tool_records["hosted:1"]
        assert record.canonical_status == "completed"
        assert record.provider_status == "completed"
        assert record.status == "complete"


@pytest.mark.asyncio
async def test_lazy_hosted_replay_record_keeps_payload_without_building_widget() -> None:
    async with _ToolGroupApp().run_test() as pilot:
        group = pilot.app.query_one(ToolGroup)

        await group.add_collapsed_replay_tool(
            "hosted:1",
            "server_task",
            "",
            args={"value": 1},
            result="done",
            artifacts=[{"id": "file_1", "mime": "text/plain"}],
            provider_hosted=True,
            hosted_family="generic",
            provider="openai",
            provider_item_type="server_task_call",
            provider_status="completed",
            provider_call_id="provider_1",
            canonical_status="completed",
            lazy=True,
        )

        record = group._tool_records["hosted:1"]
        assert "hosted:1" not in group._tools
        assert record.provider_hosted is True
        assert record.provider == "openai"
        assert record.provider_item_type == "server_task_call"
        assert record.provider_call_id == "provider_1"
        assert record.artifacts == [{"id": "file_1", "mime": "text/plain"}]


@pytest.mark.asyncio
async def test_lazy_hosted_failure_replay_resolves_synthesized_message_definition() -> None:
    async with _ToolGroupApp().run_test() as pilot:
        group = pilot.app.query_one(ToolGroup)
        await group.add_collapsed_replay_tool(
            "hosted:synthesized-replay",
            "remote_tool",
            "",
            result="Error: stale compatibility text",
            metadata={TOOL_FAILURE_TEXT_SYNTHESIZED_METADATA_KEY: True},
            provider_hosted=True,
            hosted_family="generic",
            canonical_status="failed",
            lazy=True,
        )

        group.collapsed = False
        await wait_for(
            lambda: group._content_mounted,
            pilot=pilot,
            description="tool group content is restored",
        )

        card = group.get_tool("hosted:synthesized-replay")
        assert isinstance(card, HostedToolCall)
        assert _plain(card.query_one("#tc-body", Static)) == "Error: Provider-hosted tool failed."


@pytest.mark.asyncio
async def test_unanswered_running_hosted_replay_expands_as_interrupted_error() -> None:
    async with _ToolGroupApp().run_test() as pilot:
        group = pilot.app.query_one(ToolGroup)
        await group.add_collapsed_replay_tool(
            "hosted:1",
            "web_search",
            "search",
            result="interrupted",
            provider_hosted=True,
            hosted_family="search",
            provider="openai",
            provider_status="interrupted",
            provider_call_id="provider_1",
            canonical_status="interrupted",
            lazy=True,
        )

        record = group._tool_records["hosted:1"]
        assert record.status == "error"
        assert record.canonical_status == "interrupted"

        group.collapsed = False
        await wait_for(
            lambda: group._content_mounted,
            pilot=pilot,
            description="tool group content is restored",
        )
        tool = group.get_tool("hosted:1")
        assert tool.status == "error"
        assert tool.canonical_status == "interrupted"
