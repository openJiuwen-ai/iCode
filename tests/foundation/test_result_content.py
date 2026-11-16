# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for extract_result_text / extract_result_images over the tool-result content shapes."""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import pytest

from chrys.foundation.io.result_content import extract_result_attachments, extract_result_images, extract_result_text
from chrys.kernel import Content


def test_extract_result_none() -> None:
    assert extract_result_text(None) == ""


def test_extract_result_string() -> None:
    assert extract_result_text("hello") == "hello"


def test_extract_result_list_with_text() -> None:
    items = [SimpleNamespace(text="line1", result=None), SimpleNamespace(text="line2", result=None)]
    assert extract_result_text(items) == "line1\nline2"


def test_extract_result_list_with_result_attr() -> None:
    # Mirrors ``Content.from_function_result(..., result="output")``, which
    # leaves ``text`` unset (None) and populates ``result`` with the joined
    # text from the items list.
    items = [SimpleNamespace(text=None, result="output")]
    assert extract_result_text(items) == "output"


def test_extract_result_list_of_strings() -> None:
    assert extract_result_text(["a", "b"]) == "a\nb"


def test_extract_result_list_empty() -> None:
    # Items with no recognizable text-bearing attribute and no other
    # extractable shape contribute nothing; the joined result is empty.
    items = [SimpleNamespace(text="", result="")]
    assert extract_result_text(items) == ""


def test_extract_result_list_with_output_attr() -> None:
    # ``output`` is the text-bearing attr on Content(type="mcp_server_tool_result").
    items = [SimpleNamespace(text=None, result=None, output="payload")]
    assert extract_result_text(items) == "payload"


def test_extract_result_list_uri_content() -> None:
    # Image / blob / resource Content items (text=None) get a placeholder
    # without leaking base64 data into event reprs or ACP raw_output.
    items = [SimpleNamespace(text=None, result=None, uri="data:image/png;base64,AAA", media_type="image/png")]
    assert extract_result_text(items) == "[image/png image]"


def test_extract_result_images_from_direct_content_list() -> None:
    image = Content.from_uri("data:image/png;base64,AAA", media_type="image/png")
    audio = Content.from_uri("data:audio/wav;base64,AAA", media_type="audio/wav")

    assert extract_result_images([Content.from_text("caption"), image, audio]) == [image]


def test_extract_result_images_from_function_result_items() -> None:
    image = Content.from_uri("data:image/png;base64,AAA", media_type="image/png")
    result = Content.from_function_result("call_1", result=[Content.from_text("caption"), image])

    assert extract_result_images(result) == [image]


def test_extract_result_images_ignores_unknown_uri_without_media_type() -> None:
    unknown = Content.from_uri("https://example.com/blob")

    assert extract_result_images([unknown]) == []


def test_extract_result_attachments_keeps_every_media_item() -> None:
    image = Content.from_uri("data:image/png;base64,AAA", media_type="image/png")
    audio = Content.from_uri("data:audio/wav;base64,AAA", media_type="audio/wav")
    document = Content.from_uri("file:///tmp/report.pdf", media_type="application/pdf")

    assert extract_result_attachments([Content.from_text("caption"), image, audio, document]) == [
        image,
        audio,
        document,
    ]


def test_extract_result_attachments_reaches_inside_each_kind_of_result_content() -> None:
    """Each kind keeps what it holds in its own slot: ``items``, ``outputs`` or ``output``."""
    image = Content.from_uri("data:image/png;base64,AAA", media_type="image/png")

    hosted = Content.from_hosted_tool_result("call_1", tool_name="web", items=[image])
    search = Content.from_search_tool_result("call_1", tool_name="search", items=[image])
    shell = Content.from_shell_tool_result(call_id="call_1", outputs=[image])
    mcp = Content.from_mcp_server_tool_result(call_id="call_1", output=[image])

    for result in (hosted, search, shell, mcp):
        assert extract_result_attachments(result) == [image], result.type


def test_extract_result_attachments_from_function_result_items() -> None:
    audio = Content.from_uri("data:audio/wav;base64,AAA", media_type="audio/wav")
    result = Content.from_function_result("call_1", result=[Content.from_text("caption"), audio])

    assert extract_result_attachments(result) == [audio]


# Same shapes again, but constructed via real Chrys ``Content`` factories —
# ``Content`` always has structural fields like ``type`` and
# ``additional_properties``, which would otherwise sneak through the
# JSON-dump fallback.  These tests pin down the contract for the actual
# objects produced by the MCP adapter.


def test_extract_result_real_content_text() -> None:
    items = [Content.from_text("hello")]
    assert extract_result_text(items) == "hello"


def test_extract_result_real_content_text_empty() -> None:
    """An empty-string text item must render as "", not as ``{"type": "text", ...}``."""

    items = [Content.from_text("")]
    assert extract_result_text(items) == ""


def test_extract_result_real_content_function_result_none() -> None:
    """``Content.from_function_result(..., result=None)`` builds a single empty-text item."""

    items = [Content.from_function_result(call_id="call_1", result=None)]
    assert extract_result_text(items) == ""


def test_extract_result_real_content_data_uri() -> None:
    """Image data items render as a short placeholder, never base64 or a class repr."""

    items = [Content.from_data(data=b"\x89PNG", media_type="image/png")]
    out = extract_result_text(items)
    assert out == "[image/png image]"
    assert " object at " not in out


def test_extract_result_other_type() -> None:
    assert extract_result_text(42) == "42"


# ────── Non-string text-bearing attributes (don't render as obj repr) ──────


def test_extract_result_mcp_server_tool_result_list_output() -> None:
    """Anthropic-managed MCP servers surface tool results as
    ``Content.from_mcp_server_tool_result(output=list[Content])``.  The
    ``output`` slot is typed ``Any``, so a naive ``str(output)`` renders
    as a list of object reprs.  We must
    recurse into the inner Contents so the actual text reaches the LLM.
    """

    inner = [Content.from_text("actual response"), Content.from_text("line 2")]
    items = [Content("mcp_server_tool_result", call_id="c1", output=inner)]
    out = extract_result_text(items)
    assert "actual response" in out
    assert "line 2" in out
    assert " object at " not in out, f"raw object repr leaked: {out!r}"


def test_extract_result_function_result_list_result() -> None:
    """Same pattern for ``Content`` with ``result=list[Content]`` — recurse
    rather than stringifying the list.
    """

    items = [Content("function_result", call_id="c1", result=[Content.from_text("inner-text")])]
    out = extract_result_text(items)
    assert out == "inner-text"
    assert " object at " not in out


def test_extract_result_error_message_dict_renders_as_json() -> None:
    """Structured error payloads (``message=dict``) must render as JSON
    so the LLM sees ``{"code": 500, ...}`` instead of Python's
    ``{'code': 500, ...}`` repr (subtly different — single vs double
    quotes — and not parseable as JSON downstream).
    """

    items = [Content("error", message={"code": 500, "body": "Server Error"})]
    out = extract_result_text(items)
    assert out == '{"code": 500, "body": "Server Error"}'


def test_extract_result_mcp_output_dict_renders_as_json() -> None:
    """MCP servers may return structured JSON in ``output`` directly."""

    items = [Content("mcp_server_tool_result", call_id="c1", output={"result": "ok", "status": 200})]
    out = extract_result_text(items)
    assert out == '{"result": "ok", "status": 200}'


# ──────────── Unrecognized Content fallback (treated as error) ──────────


def test_extract_result_unrecognized_content_returns_error_prefix(caplog: pytest.LogCaptureFixture) -> None:
    """A Content with no text/result/output/message/uri (e.g. ``from_function_call``)
    is an unexpected shape in a tool result list — render it as an
    ``"Error: …"`` string and log a WARNING so the LLM and TUI both
    see it as a failure rather than a silent JSON dump.
    """

    items = [Content.from_function_call(call_id="c1", name="some_tool", arguments={"k": "v"})]

    with caplog.at_level(logging.WARNING, logger="chrys.foundation.io.result_content"):
        out = extract_result_text(items)

    assert out.startswith("Error: "), f"expected error prefix, got {out!r}"
    assert "function_call" in out, "type hint should be present in the error string"
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("Unrecognized tool result Content" in r.getMessage() for r in warnings), (
        "expected a WARNING log for the unrecognized content shape"
    )


def test_extract_result_mixed_recognized_and_unrecognized_promotes_to_error() -> None:
    """If any item triggers the unrecognized fallback, the joined result
    must surface as an error so the TUI styling and the chrys
    ``Error: …`` convention apply to the whole string, not just the
    middle of it.
    """

    items = [
        Content.from_text("normal output"),
        Content.from_function_call(call_id="c1", name="oops"),
    ]
    out = extract_result_text(items)
    assert out.startswith("Error: "), f"expected error prefix, got {out!r}"
    assert "normal output" in out, "recognized item content should still appear in the joined result"


def test_extract_result_debug_log_truncates_large_strings(caplog: pytest.LogCaptureFixture) -> None:
    """The DEBUG dump must summarize, not embed, large string fields.

    Otherwise an MCP server returning a big base64 ``data:`` URI would
    leak its full payload into log files.
    """

    big = "data:image/png;base64," + ("A" * 10_000)
    items = [Content.from_data(data=b"\x89PNG", media_type="image/png")]
    items[0].uri = big  # type: ignore[attr-defined]

    with caplog.at_level(logging.DEBUG, logger="chrys.foundation.io.result_content"):
        extract_result_text(items)

    log_text = "\n".join(rec.getMessage() for rec in caplog.records)
    assert big not in log_text, "Full URI must not appear in DEBUG log"
    assert "<str len=" in log_text, "Long string fields should be summarized as <str len=…>"


def test_extract_result_debug_log_truncates_plain_string_items(caplog: pytest.LogCaptureFixture) -> None:
    """Plain ``str`` items in the result list must also be summarized in DEBUG.

    ``extract_result_text`` accepts ``list[str]`` directly, so a 10 KB
    string in the list would otherwise leak into the log via the
    non-``__dict__`` branch of the shape summarizer.
    """

    big = "X" * 10_000
    with caplog.at_level(logging.DEBUG, logger="chrys.foundation.io.result_content"):
        extract_result_text([big])

    log_text = "\n".join(rec.getMessage() for rec in caplog.records)
    assert big not in log_text, "Full plain-string item must not appear in DEBUG log"
    assert "<str len=" in log_text, "Long plain-string items should be summarized as <str len=…>"


def test_extract_result_coroutine_returns_empty() -> None:
    """Dangling coroutine objects (from cancelled tool calls) should not leak their repr."""

    async def _dummy() -> str:
        return "never"

    coro = _dummy()
    result = extract_result_text(coro)
    assert result == ""
    # Coroutine should be closed to prevent ResourceWarning
    # (calling close() on an already-closed coroutine is a no-op)
    coro.close()


async def test_extract_result_awaitable_returns_empty() -> None:
    """Any awaitable (not just coroutines) should be handled safely."""

    fut = asyncio.get_running_loop().create_future()
    result = extract_result_text(fut)
    assert result == ""
    fut.cancel()  # Clean up
