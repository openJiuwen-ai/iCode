# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Parsed Textual CSS, shared by the Apps one test worker builds.

Textual 8.2.7 keeps parsed rules per ``Stylesheet``, and every App builds its own: each new
App parses again the default CSS of every widget class it mounts, its own CSS and its
screens', with its theme's variables substituted. A worker builds hundreds of Apps from the
same few sources and themes. A parse depends only on its arguments and the stylesheet's
variables, and the ``RuleSet``s it returns are not changed afterwards, so stylesheets with
equal variables can share them. A parse whose rules carry errors is not kept: an invalid
theme or source goes through Textual's error path, and the app's recovery, every time.
"""

from __future__ import annotations

import weakref
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pytest
    from textual.css.model import RuleSet
    from textual.css.stylesheet import CSSLocation, Stylesheet

_CSS_PARSE_TEXTUAL_VERSION = "8.2.7"
_MAX_ENTRIES = 4096
"""Bounds the retained rules: one entry per source and distinct variable set (theme) a worker meets."""

_parsed: dict[tuple[object, ...], list[RuleSet]] = {}
_variables_keys: weakref.WeakKeyDictionary[Stylesheet, tuple[dict[str, str], tuple[tuple[str, str], ...]]] = (
    weakref.WeakKeyDictionary()
)


def _variables_key(stylesheet: Stylesheet) -> tuple[tuple[str, str], ...]:
    """*stylesheet*'s variables as a key, computed once per variables mapping it is given."""
    variables = stylesheet._variables
    known = _variables_keys.get(stylesheet)
    if known is not None and known[0] is variables:
        return known[1]
    key = tuple(sorted(variables.items()))
    _variables_keys[stylesheet] = (variables, key)
    return key


def install_shared_css_parse(monkeypatch: pytest.MonkeyPatch) -> bool:
    """Share parsed rules across this worker's stylesheets on the pinned Textual; False leaves
    an unpinned install untouched."""
    import textual
    from textual.css.stylesheet import Stylesheet

    if textual.__version__ != _CSS_PARSE_TEXTUAL_VERSION:
        return False
    upstream = Stylesheet._parse_rules

    def _parse_rules(
        self: Stylesheet,
        css: str,
        read_from: CSSLocation,
        is_default_rules: bool = False,
        tie_breaker: int = 0,
        scope: str = "",
    ) -> list[RuleSet]:
        key = (css, read_from, is_default_rules, tie_breaker, scope, _variables_key(self))
        rules = _parsed.get(key)
        if rules is not None:
            return rules
        rules = upstream(self, css, read_from, is_default_rules, tie_breaker, scope)
        if not any(rule.errors for rule in rules):
            if len(_parsed) >= _MAX_ENTRIES:
                del _parsed[next(iter(_parsed))]
            _parsed[key] = rules
        return rules

    monkeypatch.setattr(Stylesheet, "_parse_rules", _parse_rules)
    return True
