# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Behavior anchors for the C4 runtime branding points (C050-C062, C118).

Each test consumes the real upstream symbol and asserts the AIxCoding rendering;
nothing being anchored is mocked (DEVELOPMENT.md §5).
"""

from __future__ import annotations

import inspect
from pathlib import Path

import yaml

from chrys.app.cli import serve as serve_cli
from chrys.foundation.branding import APP_COMMAND, APP_DISPLAY_NAME
from chrys.foundation.i18n.formatting import format_message
from chrys.foundation.observability import setup as otel_setup
from chrys.kernel.aixcoding.command_migration import LEGACY_APP_COMMAND
from chrys.service.agent_middleware.control.approval import _REMEMBER_FAILED
from chrys.service.context.providers.context_management import _INSTRUCTIONS
from chrys.service.context.providers.documentation import DocumentationProvider
from chrys.service.skills.adapter import SKILLS_STATIC_PROMPT
from chrys.service.skills.provider import _create_static_instructions
from chrys.service.tools.builtins.filesystem import _ReadMaxTokens
from chrys.service.tools.builtins.web.hosted import hosted_web_tools_warning

_BUILTINS = Path(serve_cli.__file__).parents[2] / "service" / "profiles" / "agents" / "builtins"


def test_c050_branding_constants() -> None:
    assert APP_DISPLAY_NAME == "AIxCoding"
    assert APP_COMMAND == "aixcoding-cli"
    assert LEGACY_APP_COMMAND == "icode"


def test_c051_c052_documentation_prompt_self_reference() -> None:
    instructions = DocumentationProvider(file_read_available=False)._instructions
    assert instructions.startswith("## AIxCoding documentation\n")
    assert "You run inside AIxCoding " in instructions
    assert "always call the product AIxCoding when talking to the user" in instructions
    assert "Read product docs only for AIxCoding" in instructions
    # No old-brand self-reference; paths may legitimately contain "iCode".
    assert "You run inside iCode" not in instructions
    assert "always call the product iCode" not in instructions
    assert "Read product docs only for iCode" not in instructions


def test_c053_context_management_instructions() -> None:
    assert "{APP_DISPLAY_NAME}'s reminder mechanism" not in _INSTRUCTIONS
    assert "AIxCoding's reminder mechanism" in _INSTRUCTIONS
    assert "AIxCoding's context management" in _INSTRUCTIONS
    assert "iCode" not in _INSTRUCTIONS


def test_c054_c055_hosted_notice_binds_app() -> None:
    warning = hosted_web_tools_warning(frozenset({"web_search"}), model_profile_id="m", session_id=None)
    display = format_message(warning.display_message)
    assert "not covered by AIxCoding approval rules" in display
    assert "iCode" not in display


def test_c056_read_tool_description() -> None:
    description = _ReadMaxTokens.__metadata__[0].description
    assert "estimated by AIxCoding" in description
    assert "iCode" not in description


def test_c057_c058_skill_prompt_probe_and_render_bind_app() -> None:
    # The probe path raises on an unbound placeholder, so a clean return proves
    # both format() calls received app=; {{skill_script_name}} stays escaped.
    rendered = _create_static_instructions(SKILLS_STATIC_PROMPT)
    assert rendered is not None
    assert "most of the AIxCoding process environment" in rendered
    assert "{skill_script_name}" in rendered
    assert "iCode" not in rendered


def test_c059_otlp_error_message_uses_brand_constant() -> None:
    # The ImportError branch needs an absent OTLP HTTP exporter to trigger, so the
    # anchor is the raise site's source: interpolated, no hardcoded old brand.
    source = inspect.getsource(otel_setup)
    assert "which {APP_DISPLAY_NAME} does not include" in source
    assert "which iCode does not include" not in source


def test_c060_c061_builtin_agent_self_reference() -> None:
    for name in ("Code.yaml", "QA.yaml"):
        instructions = yaml.safe_load((_BUILTINS / name).read_text(encoding="utf-8"))["instructions"]
        assert 'named "AIxCoding"' in instructions
        assert '"iCode"' not in instructions


def test_c062_serve_banner_is_ascii_aixcoding() -> None:
    banner = serve_cli._icode_serve_logo()
    assert "####   ##     ##  #     #  #  #  #   ##   # ##  # ##" in banner
    assert "░" not in banner


def test_c118_approval_fallback_names_the_new_command() -> None:
    assert "aixcoding-cli approval" in _REMEMBER_FAILED.fallback
    assert "icode approval" not in _REMEMBER_FAILED.fallback
