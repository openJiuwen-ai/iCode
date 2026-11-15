# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Compaction configuration panel — composable widget for the Compaction tab."""

from __future__ import annotations

import contextlib
from dataclasses import replace
from typing import TYPE_CHECKING

from textual.containers import VerticalScroll
from textual.css.query import NoMatches
from textual.widgets import Label

from chrys.app.tui.i18n import render_str, widget_localizer
from chrys.app.tui.screens.agents.agent_draft_store import (
    MAX_LAST_WORDS_TOKENS,
    MIN_LAST_WORDS_TOKENS,
)
from chrys.app.tui.screens.agents.prompt_editor import PromptTextArea
from chrys.app.tui.screens.agents.validation_messages import (
    COMPACTION_MAX_OUTPUT_TOKENS as _MAX_OUTPUT_TOKENS,
)
from chrys.app.tui.screens.agents.validation_messages import (
    COMPACTION_RANGE,
    COMPACTION_REQUIRED,
    COMPACTION_WHOLE_NUMBER,
    LAST_WORDS_FIELD,
)
from chrys.app.tui.widgets import EnhancedInput as Input
from chrys.app.tui.widgets import EnhancedTextArea
from chrys.foundation.i18n import DisplayBlock, msg
from chrys.service.profiles.agents.schema import (
    DEFAULT_LAST_WORDS_MAX_OUTPUT_TOKENS,
)

if TYPE_CHECKING:
    from textual.app import ComposeResult

    from chrys.service.profiles.agents.schema import CompactionConfig


_COMPACTION = msg("tui.agent_config.compaction.title", fallback="Compaction")
_DESCRIPTION = msg(
    "tui.agent_config.compaction.description",
    fallback=(
        "Compaction triggers automatically at the context window minus the model's maximum output tokens and a "
        "safety margin. Both limits are configured on the model profile."
    ),
)
_LAST_WORDS_SUPPLEMENT = msg(
    "tui.agent_config.compaction.last_words_supplement",
    fallback="Last Words Supplement (Optional emphasis)",
)


class CompactionConfigPanel(VerticalScroll):
    """Composable widget for editing the per-profile compaction settings.

    Exposes the fields agent creators most need to tune:

    - ``last_words_max_output_tokens`` — hard cap on the LAST_WORDS
      generator's output tokens.
    - ``last_words_template`` — optional agent-specific supplementary
      emphasis appended after the always-on format contract and base guidance.

    Compaction thresholds are derived from the active model profile.
    """

    DEFAULT_CSS = """
    CompactionConfigPanel {
        height: 1fr;
        padding: 0 2;
        scrollbar-size-vertical: 1;
    }
    CompactionConfigPanel .cc-section-title {
        color: $secondary;
        text-style: bold;
        height: 1;
    }
    CompactionConfigPanel .cc-section-desc {
        color: $text-muted;
        height: auto;
        width: 1fr;
        margin: 0 0 1 0;
    }
    CompactionConfigPanel .cc-label {
        height: 1;
        color: $text-muted;
        margin: 1 0 0 0;
    }
    CompactionConfigPanel .cc-label-first {
        height: 1;
        color: $text-muted;
        margin: 0;
    }
    CompactionConfigPanel Input {
        height: 1;
        border: none;
        background: $foreground 8%;
        padding: 0 1;
        margin: 0;
    }
    CompactionConfigPanel Input:focus {
        border: none;
        background: $foreground 12%;
    }
    CompactionConfigPanel EnhancedTextArea {
        height: 1fr;
        border: none;
        background: $foreground 8%;
        padding: 0 0;
        margin: 0;
        scrollbar-size-vertical: 1;
    }
    CompactionConfigPanel EnhancedTextArea:focus {
        border: none;
        background: $foreground 12%;
    }
    CompactionConfigPanel EnhancedTextArea .text-area--cursor-line {
        background: $primary 15%;
    }
    """

    def __init__(self, compaction: CompactionConfig, *, prompt_locked: bool = False) -> None:
        self._compaction = compaction
        self._prompt_locked = prompt_locked
        super().__init__()

    def compose(self) -> ComposeResult:
        c = self._compaction
        localizer = widget_localizer(self)

        yield Label(render_str(localizer, _COMPACTION.bind()), classes="cc-section-title")
        yield Label(
            render_str(localizer, _DESCRIPTION.bind()),
            classes="cc-section-desc",
        )

        yield Label(
            render_str(
                localizer,
                _MAX_OUTPUT_TOKENS.bind(
                    minimum=MIN_LAST_WORDS_TOKENS,
                    maximum=MAX_LAST_WORDS_TOKENS,
                ),
            ),
            classes="cc-label-first",
        )
        yield Input(
            value=str(c.last_words_max_output_tokens),
            placeholder=str(DEFAULT_LAST_WORDS_MAX_OUTPUT_TOKENS),
            id="cc-last-words-max-tokens",
        )

        yield Label(render_str(localizer, _LAST_WORDS_SUPPLEMENT.bind()), classes="cc-label")
        yield PromptTextArea(c.last_words_template, locked=self._prompt_locked, id="cc-last-words-template")

    def get_config(self) -> CompactionConfig:
        """Return an updated ``CompactionConfig`` preserving unrelated fields."""
        max_tokens = self._read_int("#cc-last-words-max-tokens", self._compaction.last_words_max_output_tokens)

        template = self._compaction.last_words_template
        with contextlib.suppress(NoMatches):
            template = self.query_one("#cc-last-words-template", EnhancedTextArea).text

        return replace(
            self._compaction,
            last_words_max_output_tokens=max_tokens,
            last_words_template=template,
        )

    def _read_int(self, selector: str, fallback: int) -> int:
        """Parse an int from an Input by selector; fall back if validate() was skipped."""
        try:
            raw = self.query_one(selector, Input).value.strip()
        except NoMatches:
            return fallback
        try:
            return int(raw)
        except ValueError:
            return fallback

    def validate(self) -> list[str]:
        """Validate numeric ranges for every editable field."""
        localizer = widget_localizer(self)
        errors: list[str] = []
        self._validate_int(
            "#cc-last-words-max-tokens",
            render_str(localizer, LAST_WORDS_FIELD.bind()),
            MIN_LAST_WORDS_TOKENS,
            MAX_LAST_WORDS_TOKENS,
            str(DEFAULT_LAST_WORDS_MAX_OUTPUT_TOKENS),
            errors,
        )
        return errors

    def _validate_int(
        self,
        selector: str,
        label: str,
        lo: int,
        hi: int,
        default_hint: str,
        errors: list[str],
    ) -> None:
        localizer = widget_localizer(self)
        raw = self.query_one(selector, Input).value.strip()
        if not raw:
            errors.append(
                render_str(
                    localizer,
                    COMPACTION_REQUIRED.bind(label=label, default_value=default_hint),
                )
            )
            return
        try:
            value = int(raw)
        except ValueError:
            errors.append(
                render_str(
                    localizer,
                    COMPACTION_WHOLE_NUMBER.bind(label=label, value=DisplayBlock(repr(raw))),
                )
            )
            return
        if not lo <= value <= hi:
            errors.append(
                render_str(
                    localizer,
                    COMPACTION_RANGE.bind(label=label, minimum=lo, maximum=hi),
                )
            )
