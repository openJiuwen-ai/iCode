# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Pinned message ids for importing MCP servers from other applications."""

from __future__ import annotations

MCP_IMPORT_MESSAGE_IDS: frozenset[str | tuple[str, str]] = frozenset(
    {
        "tui.mcp.import",
        "tui.mcp.import.agent_separator",
        "tui.mcp.import.button.cancel",
        "tui.mcp.import.button.import",
        "tui.mcp.import.confirm.back",
        ("tui.mcp.import.confirm.message", "tui.mcp.import.confirm.message#plural"),
        "tui.mcp.import.confirm.title",
        "tui.mcp.import.detect_failed",
        "tui.mcp.import.detecting",
        "tui.mcp.import.empty_source",
        "tui.mcp.import.error.invalid",
        "tui.mcp.import.error.invalid_field",
        "tui.mcp.import.error.unparsable",
        "tui.mcp.import.error.unparsable_at",
        "tui.mcp.import.error.unreadable",
        "tui.mcp.import.loading",
        "tui.mcp.import.no_agents",
        "tui.mcp.import.note.conflict",
        "tui.mcp.import.notes",
        "tui.mcp.import.origin",
        "tui.mcp.import.problem.remote_environment",
        "tui.mcp.import.problem.unsupported_connection",
        "tui.mcp.import.problem.value_from_file_or_command",
        "tui.mcp.import.problem.value_not_representable",
        "tui.mcp.import.problem.variable_not_supported",
        "tui.mcp.import.read_failed",
        "tui.mcp.import.source",
        "tui.mcp.import.status.connection_in_use",
        "tui.mcp.import.status.duplicate",
        "tui.mcp.import.status.name_in_use",
        "tui.mcp.import.status.unavailable",
        "tui.mcp.import.ticked_count",
        "tui.mcp.import.title",
        "tui.mcp.import.warning.auth_not_imported",
        "tui.mcp.import.warning.not_imported",
        "tui.mcp.import.warning.tools_restricted",
        "tui.mcp.import.warning.variable_required",
        ("tui.mcp.imported", "tui.mcp.imported#plural"),
        "tui.mcp.wait_before_importing",
    }
)
