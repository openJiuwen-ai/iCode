# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Presentation bounds for results produced by remote MCP tools."""

from __future__ import annotations

from copy import copy
from pathlib import Path
from typing import Any

from chrys.foundation.text.tool_output import truncate_content_texts, truncate_output
from chrys.kernel import Content
from chrys.service.tools.spill import bound_result_text, discard_spill, run_spill_finalizer, try_spill_text

DEFAULT_MCP_TOOL_RESULT_MAX_TOKENS = 8000
MCP_BINARY_MAX_BYTES = 3 * 1024 * 1024
MCP_RESULT_MAX_TOTAL_BINARY_BYTES = 10 * 1024 * 1024
MCP_ERROR_TEXT_MAX_TOKENS = 2000

_MCP_PROMPT_TOOL_KEY = "_chrys_mcp_prompt_tool"


def resolve_mcp_result_cap(value: int | None) -> int | None:
    """Resolve an MCP profile cap; ``None`` means default and zero disables."""
    return DEFAULT_MCP_TOOL_RESULT_MAX_TOKENS if value is None else (None if value == 0 else value)


async def truncate_mcp_result(result: Any, budget: int, spill_dir: Path | None) -> Any:
    """Copy and bound the text-bearing MCP result shapes used by Chrys."""
    if isinstance(result, str):
        return await bound_result_text(result, budget, spill_dir, "mcp")

    if not isinstance(result, list):
        return result
    text_items = [
        item for item in result if isinstance(item, Content) and item.type == "text" and item.text is not None
    ]
    texts = [item.text for item in text_items]
    if not texts:
        return result
    if truncate_content_texts(texts, budget) is None:
        return result

    return await run_spill_finalizer(_truncate_mcp_list_result, result, texts, budget, spill_dir)


def _truncate_mcp_list_result(
    result: list[Any],
    texts: list[str],
    budget: int,
    spill_dir: Path | None,
) -> list[Any]:

    worst_dropped_count = max(0, len(texts) - 1)
    reserved_footer = f"[{worst_dropped_count} further text items omitted.]" if worst_dropped_count else ""
    joined_texts = "\n\n".join(texts)
    info = try_spill_text(spill_dir, "mcp", joined_texts, budget, reserved_footer)
    bounded = truncate_content_texts(texts, budget, truncation_suffix=info.notice if info else "")
    if bounded is None:
        raise RuntimeError("MCP text truncation did not return bounded content.")
    if info is not None and info.notice not in "\n".join(value for value in bounded if value is not None):
        discard_spill(info)
        bounded = truncate_content_texts(texts, budget)
        if bounded is None:
            raise RuntimeError("MCP text truncation did not return bounded content.")

    output: list[Content] = []
    text_index = 0
    for item in result:
        if isinstance(item, Content) and item.type == "text" and item.text is not None:
            replacement = bounded[text_index]
            text_index += 1
            if replacement is None:
                continue
            if replacement == item.text:
                output.append(item)
            else:
                item_copy = copy(item)
                item_copy.text = replacement
                output.append(item_copy)
        else:
            output.append(item)
    return output


def truncate_mcp_error(text: str) -> str:
    """Bound server-controlled error text while favoring its leading cause."""
    return truncate_output(text, MCP_ERROR_TEXT_MAX_TOKENS, head_ratio=2 / 3)


def _base64_payload(data: str) -> str:
    return data.split(",", 1)[1] if data.startswith("data:") and "," in data else data


def _estimated_decoded_size(data: str) -> int:
    return len(_base64_payload(data)) * 3 // 4


def _binary_item_info(item: Any) -> tuple[str, int] | None:
    from mcp import types

    if isinstance(item, types.ImageContent):
        return "image", _estimated_decoded_size(item.data)
    if isinstance(item, types.AudioContent):
        return "audio", _estimated_decoded_size(item.data)
    if isinstance(item, types.EmbeddedResource) and isinstance(item.resource, types.BlobResourceContents):
        return "blob", _estimated_decoded_size(item.resource.blob)
    return None


def sanitize_mcp_result_binary(result: Any) -> Any:
    """Copy an SDK CallToolResult and replace binary payloads before decode."""
    from mcp import types

    content = list(result.content)
    sanitized: list[Any] = []
    cumulative = 0
    index = 0
    while index < len(content):
        item = content[index]
        info = _binary_item_info(item)
        if info is None:
            sanitized.append(item)
            index += 1
            continue

        kind, size = info
        if size > MCP_BINARY_MAX_BYTES:
            sanitized.append(
                types.TextContent(
                    type="text",
                    text=f"[{kind} omitted: ~{size} exceeds {MCP_BINARY_MAX_BYTES} limit]",
                )
            )
            index += 1
            continue

        if cumulative + size > MCP_RESULT_MAX_TOTAL_BINARY_BYTES:
            dropped = sum(1 for remaining in content[index:] if _binary_item_info(remaining) is not None)
            sanitized.append(
                types.TextContent(
                    type="text",
                    text=(
                        f"[{dropped} binary items omitted: result exceeds "
                        f"{MCP_RESULT_MAX_TOTAL_BINARY_BYTES} byte limit]"
                    ),
                )
            )
            sanitized.extend(remaining for remaining in content[index:] if _binary_item_info(remaining) is None)
            break

        cumulative += size
        sanitized.append(item)
        index += 1

    return result.model_copy(update={"content": sanitized})
