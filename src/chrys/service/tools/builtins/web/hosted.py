# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Web tools a model's provider runs itself, declared in the model's chat options."""

from __future__ import annotations

import re
from typing import Any, Final

from chrys.foundation.branding import APP_DISPLAY_NAME
from chrys.foundation.events.types import Warning
from chrys.foundation.i18n import msg

# Local tool names a provider-hosted declaration can own.
HOSTED_WEB_TOOL_NAMES: Final[frozenset[str]] = frozenset({"web_search", "web_fetch"})
# Hosted declaration types that need no name: OpenAI's web_search / web_search_preview (with or
# without a dated suffix such as _2025_03_11) and Anthropic's dated web_search / web_fetch.
_HOSTED_DECLARATION_TYPE = re.compile(r"(web_search(?:_preview)?|web_fetch)(?:_\d{4}_\d{2}_\d{2}|_\d{8})?")
_HOSTED_WEB_TOOLS_PREFERRED = msg(
    "builder.hosted_web_tools_preferred",
    # AIxCoding branding: hosted-tools notice placeholder
    fallback=(
        "This model's provider runs web search, so the local {tools} tool(s) were disabled for this run. "
        "Provider-run search is not covered by {app} approval rules or network grants. "
        "Remove the hosted web tool declaration from the model's chat options to use local tools instead."
    ),
)


def _declaration_name(declaration: dict[str, Any]) -> str | None:
    """Return the effective tool name a hosted declaration claims."""
    name = declaration.get("name")
    declaration_type = declaration.get("type")
    if not name and isinstance(declaration_type, str):
        matched = _HOSTED_DECLARATION_TYPE.fullmatch(declaration_type)
        if matched is not None:
            name = "web_fetch" if matched.group(1) == "web_fetch" else "web_search"
    return name if isinstance(name, str) else None


def declared_web_tool_names(options: dict[str, Any] | None) -> tuple[str, ...]:
    """Return every web tool name the hosted declarations in *options* claim, repeats included."""
    names: list[str] = []
    for declaration in (options or {}).get("tools", []) or []:
        if isinstance(declaration, dict):
            name = _declaration_name(declaration)
            if name in HOSTED_WEB_TOOL_NAMES:
                names.append(name)
    return tuple(names)


def hosted_web_tools_warning(suppressed: frozenset[str], *, model_profile_id: str, session_id: str | None) -> Warning:
    """Build the user-facing warning for local tools displaced by provider-hosted web tools.

    The model profile id keeps the TUI notice to one per configuration.
    """
    tools = ", ".join(sorted(suppressed))
    return Warning(
        code="hosted_web_tools_preferred",
        message=f"Provider-hosted web tools own local {tools}; model profile {model_profile_id}",
        # AIxCoding branding: bind app for hosted-tools notice
        display_message=_HOSTED_WEB_TOOLS_PREFERRED.bind(tools=tools, app=APP_DISPLAY_NAME),
        session_id=session_id,
    )
