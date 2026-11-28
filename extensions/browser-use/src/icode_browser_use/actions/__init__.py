# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""One module per browser action plus the single ``dispatch`` entry point.

``build_tool_defs`` assembles the ``tools/list`` payload from the ordered module
registry (``browser_evaluate`` is included only when evaluation was explicitly
enabled). ``dispatch`` routes a ``tools/call`` to the matching module's ``run``;
the registry always contains ``browser_evaluate`` so a stray call yields a clear
``browser_bad_request`` rather than a crash - the enable check itself lives at
the top of ``evaluate.run``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..errors import BadRequest
from ..types import ToolBlock, ToolDef
from ..validation import to_json_schema
from . import (
    action_status,
    back,
    click,
    close,
    dialog,
    download_cancel,
    downloads,
    drag,
    evaluate,
    extract,
    fill,
    find,
    forward,
    hover,
    logs,
    navigate,
    network_body,
    press_key,
    query,
    reload,
    save_state,
    screenshot,
    scroll,
    select_option,
    snapshot,
    tab_close,
    tab_switch,
    tabs,
    upload,
    wait_for,
)

if TYPE_CHECKING:
    from ..session import BrowserSession

#: Ordered exactly as the design's §9 tool table.
_MODULES = (
    navigate,
    snapshot,
    find,
    tabs,
    tab_switch,
    tab_close,
    action_status,
    click,
    drag,
    hover,
    fill,
    press_key,
    select_option,
    wait_for,
    dialog,
    downloads,
    download_cancel,
    upload,
    scroll,
    extract,
    query,
    logs,
    network_body,
    evaluate,
    back,
    forward,
    reload,
    screenshot,
    save_state,
    close,
)

#: Modules withheld from ``tools/list`` unless explicitly enabled.
_OPTIONAL = frozenset({evaluate.NAME, save_state.NAME})

#: Registry used by ``dispatch`` - always includes every module, optional or not.
_REGISTRY = {module.NAME: module for module in _MODULES}

__all__ = ["build_tool_defs", "dispatch"]


def build_tool_defs(*, evaluate_enabled: bool = False, save_state_enabled: bool = False) -> list[ToolDef]:
    """Return the advertised tool definitions for the enabled optional surfaces."""
    definitions: list[ToolDef] = []
    for module in _MODULES:
        if module.NAME == evaluate.NAME and not evaluate_enabled:
            continue
        if module.NAME == save_state.NAME and not save_state_enabled:
            continue
        definitions.append(
            ToolDef(
                name=module.NAME,
                description=module.DESCRIPTION,
                params=module.PARAMS,
                schema=to_json_schema(module.PARAMS),
            )
        )
    return definitions


async def dispatch(session: BrowserSession, name: str, arguments: dict[str, Any]) -> list[ToolBlock]:
    """Route a validated ``tools/call`` to its action module."""
    module = _REGISTRY.get(name)
    if module is None:
        raise BadRequest(f"未知工具：{name}")
    return await module.run(session, arguments)
