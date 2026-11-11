# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Public tool-authoring facade for Chrys tool kinds.

chrys classifies tools with a bare ``kind`` string (``shell``,
``filesystem.write``, ...) consumed by middleware, approval policies, hooks,
the TUI and the ACP bridge. The same bare form appears in user-facing YAML —
there is no storage/runtime conversion.

The value lives only on a chrys-owned attribute — ``tool.chrys_kind`` —
written by :func:`set_tool_kind` and read by :func:`get_tool_kind`;
``FunctionTool.kind`` stays ``None``. One channel means every reader sees the
same kind, and the wire serializers, which ignore ``.kind``, always send a
tool exactly as declared.

The vocabulary and out-of-band kind channel live in
``chrys.foundation.tool_kinds`` so lower layers do not depend on ``tools``. This
module stays as the permanent public authoring surface because it also wraps
``chrys.kernel.tool`` with the ``kind=`` convenience parameter.

See ``tests/service/tools/test_tool_kind_wire_boundary.py`` (pins the
``kind is None`` invariant and what each serializer sends).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from chrys.foundation.tool_approval import declare_requires_approval
from chrys.foundation.tool_kinds import (
    KIND_ASK_USER as KIND_ASK_USER,
)
from chrys.foundation.tool_kinds import (
    KIND_CONTEXT as KIND_CONTEXT,
)
from chrys.foundation.tool_kinds import (
    KIND_DOC_CONVERTER as KIND_DOC_CONVERTER,
)
from chrys.foundation.tool_kinds import (
    KIND_FILESYSTEM_READ as KIND_FILESYSTEM_READ,
)
from chrys.foundation.tool_kinds import (
    KIND_FILESYSTEM_WRITE as KIND_FILESYSTEM_WRITE,
)
from chrys.foundation.tool_kinds import (
    KIND_MCP as KIND_MCP,
)
from chrys.foundation.tool_kinds import (
    KIND_SEARCH as KIND_SEARCH,
)
from chrys.foundation.tool_kinds import (
    KIND_SHELL as KIND_SHELL,
)
from chrys.foundation.tool_kinds import (
    KIND_SKILL as KIND_SKILL,
)
from chrys.foundation.tool_kinds import (
    KIND_SLEEP as KIND_SLEEP,
)
from chrys.foundation.tool_kinds import (
    KIND_SUB_AGENT as KIND_SUB_AGENT,
)
from chrys.foundation.tool_kinds import (
    KIND_TODO as KIND_TODO,
)
from chrys.foundation.tool_kinds import KIND_WEB_FETCH as KIND_WEB_FETCH
from chrys.foundation.tool_kinds import KIND_WEB_SEARCH as KIND_WEB_SEARCH
from chrys.foundation.tool_kinds import (
    TOOL_KINDS as TOOL_KINDS,
)
from chrys.foundation.tool_kinds import (
    get_tool_kind as get_tool_kind,
)
from chrys.foundation.tool_kinds import (
    set_tool_kind,
)
from chrys.foundation.tool_kinds import (
    strip_legacy_kind_prefix as strip_legacy_kind_prefix,
)
from chrys.kernel import tool as _kernel_tool

if TYPE_CHECKING:
    from collections.abc import Callable

    from chrys.kernel import FunctionTool

logger = logging.getLogger(__name__)


def tool(
    func: Callable[..., Any] | None = None,
    /,
    *,
    kind: str | None = None,
    requires_approval: bool = False,
    **kwargs: Any,
) -> FunctionTool | Callable[[Callable[..., Any]], FunctionTool]:
    """Drop-in for :func:`chrys.kernel.tool` that stores ``kind`` out of band.

    Identical to :func:`chrys.kernel.tool` except that ``kind=`` is intercepted
    and recorded via :func:`set_tool_kind` — ``FunctionTool.kind`` stays
    ``None`` (see module docstring) — and ``requires_approval=True`` declares
    the tool asks before it runs unless an explicit rule says otherwise
    (:mod:`chrys.foundation.tool_approval`). All other arguments pass through.
    Instance-method tools keep both across descriptor binding: the bound clone
    produced by ``FunctionTool.__get__`` carries them too (pinned in the
    contract tests).
    """

    def decorate(f: Callable[..., Any]) -> FunctionTool:
        wrapped = _kernel_tool(f, **kwargs)
        if kind is not None:
            set_tool_kind(wrapped, kind)
        if requires_approval:
            declare_requires_approval(wrapped)
        return wrapped

    if func is not None:
        return decorate(func)
    return decorate
