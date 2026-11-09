# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""One rendered setting: label, control, badges and hint, plus the save routing."""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, Any

from rich.text import Text
from textual import on
from textual.containers import Horizontal, Vertical
from textual.widgets import Label, Static

from chrys.app.tui.i18n import render_str, widget_localizer
from chrys.app.tui.language import LANGUAGE_OPTIONS
from chrys.app.tui.screens.settings.danger import is_dangerous_transition
from chrys.app.tui.screens.settings.layout import (
    HINT_PROJECT_CONFIG_DORMANT,
    PROJECT_CONFIG_KEY,
    HintArgs,
    Placeholder,
    RowKind,
    SettingRowSpec,
)
from chrys.app.tui.screens.settings.provenance import ProvenanceView, is_greyed_layer, provenance_view
from chrys.app.tui.theme_loader import theme_is_read_only
from chrys.app.tui.widgets import Checkbox, Select
from chrys.app.tui.widgets import EnhancedInput as Input
from chrys.foundation.config.coercion import CoerceReason, CoerceStatus
from chrys.foundation.config.settings import (
    DEFAULT_MAX_TRANSIENT_RETRIES,
    MAX_TRANSIENT_RETRIES_LIMIT,
    Settings,
    default_session_root_dir,
)
from chrys.foundation.config.spec import Apply, ChoiceProvider, Kind, Risk, SettingSpec, field_names_by_key
from chrys.foundation.i18n import DisplayPath, DisplaySequence, MessageDef, MessageRef, msg

if TYPE_CHECKING:
    from textual.app import ComposeResult

    from chrys.app.tui.screens.settings.ports import SettingsPanelPorts

_BADGE_DANGEROUS = msg("tui.settings.badge.dangerous", fallback="⚠")

HINT_TREE_GLYPH = "└─"
"""Leads the description under a control, tree-style; the notifications pane uses it too."""

_SELECT_DEFAULT = msg("tui.settings.select.default", fallback="(default)")
_SELECT_ACTIVE = msg("tui.settings.select.active", fallback="Use active model")
_SELECT_CURRENT = msg("tui.settings.select.current", fallback="{value} (current)")

_ERROR_REQUIRED = msg("tui.settings.error.required", fallback="A value is required.")
_ERROR_INVALID = msg("tui.settings.error.invalid", fallback="Invalid value.")
_ERROR_EXPECTED_INT = msg("tui.settings.error.expected_int", fallback="Expected an integer.")
_ERROR_EXPECTED_NON_NEGATIVE_INT = msg(
    "tui.settings.error.expected_non_negative_int",
    fallback="Expected a non-negative integer.",
)
_ERROR_EXPECTED_NUMBER = msg("tui.settings.error.expected_number", fallback="Expected a number.")
_ERROR_EXPECTED_FINITE_NUMBER = msg("tui.settings.error.expected_finite_number", fallback="Expected a finite number.")
_ERROR_EXPECTED_TEXT = msg("tui.settings.error.expected_text", fallback="Expected text.")
_ERROR_NOT_A_CHOICE = msg("tui.settings.error.not_a_choice", fallback="Must be one of: {choices}.")
_ERROR_EXPECTED_JSON_ARRAY = msg(
    "tui.settings.error.expected_json_array",
    fallback="Expected a JSON array.",
)
_ERROR_EXPECTED_WEB_ORIGIN = msg(
    "tui.settings.error.expected_web_origin",
    fallback="Each address must be an http(s) origin such as https://example.com, with no path or credentials.",
)
_ERROR_EXPECTED_ENDPOINT_GRANT = msg(
    "tui.settings.error.expected_endpoint_grant",
    fallback="Each entry needs a distinct http(s) url and may list credential_env_names.",
)
_ERROR_BELOW_MINIMUM = msg("tui.settings.error.below_minimum", fallback="Must be at least {limit}.")
_ERROR_ABOVE_MAXIMUM = msg("tui.settings.error.above_maximum", fallback="Must be at most {limit}.")

_CONFIRM_APPROVAL_AUTO = msg(
    "tui.settings.confirm.approval_auto",
    fallback="Auto mode lets a model approve tool calls without asking you. Continue?",
)
_CONFIRM_RAW_HTTP_CAPTURE = msg(
    "tui.settings.confirm.raw_http_capture",
    fallback=(
        "This writes raw model HTTP requests and responses in plain text to <session>/llm_raw_http.jsonl,"
        " including sensitive data in the requests such as API keys. Continue?"
    ),
)
_CONFIRM_OTEL_SENSITIVE = msg(
    "tui.settings.confirm.otel_sensitive_data",
    fallback="This records prompts, model responses, tool arguments, and tool results in telemetry. Continue?",
)
_CONFIRM_DANGEROUS = msg("tui.settings.confirm.dangerous", fallback="This lowers a safety setting. Continue?")

_ERROR_REASONS: dict[CoerceReason, MessageDef] = {
    CoerceReason.EXPECTED_INT: _ERROR_EXPECTED_INT,
    CoerceReason.EXPECTED_NON_NEGATIVE_INT: _ERROR_EXPECTED_NON_NEGATIVE_INT,
    CoerceReason.EXPECTED_NUMBER: _ERROR_EXPECTED_NUMBER,
    CoerceReason.EXPECTED_FINITE_NUMBER: _ERROR_EXPECTED_FINITE_NUMBER,
    CoerceReason.EXPECTED_TEXT: _ERROR_EXPECTED_TEXT,
    CoerceReason.EXPECTED_JSON_ARRAY: _ERROR_EXPECTED_JSON_ARRAY,
    CoerceReason.EXPECTED_WEB_ORIGIN: _ERROR_EXPECTED_WEB_ORIGIN,
    CoerceReason.EXPECTED_ENDPOINT_GRANT: _ERROR_EXPECTED_ENDPOINT_GRANT,
}
_CONFIRM_MESSAGES: dict[str, MessageDef] = {
    "approval.default_mode": _CONFIRM_APPROVAL_AUTO,
    "log.raw_http_capture": _CONFIRM_RAW_HTTP_CAPTURE,
    "otel.sensitive_data": _CONFIRM_OTEL_SENSITIVE,
}
_ACTIVE_PLACEHOLDER_KEYS = frozenset({"model.role.session_title", "model.role.approval_judge", "model.role.buddy"})
_APPROVAL_MODE_KEY = "approval.default_mode"
_LOCALE_KEY = "ui.locale"
_LOCALE_LABELS: dict[str, MessageDef] = dict(LANGUAGE_OPTIONS)
_AUTO_REVIEW_DIRECT = msg("tui.settings.approval.auto_review.direct", fallback="Direct review")
_AUTO_REVIEW_FORMAL = msg("tui.settings.approval.auto_review.formal", fallback="Individual risk checks")
_AUTO_REVIEW_LABELS = {"direct": _AUTO_REVIEW_DIRECT, "formal": _AUTO_REVIEW_FORMAL}

BADGE_SEPARATOR = " · "


def confirm_message_for(key: str) -> MessageRef:
    return _CONFIRM_MESSAGES.get(key, _CONFIRM_DANGEROUS).bind()


def field_default(key: str) -> Any:
    """The built-in default of the ``Settings`` field behind *key*."""
    name = field_names_by_key(Settings)[key]
    for entry in dataclasses.fields(Settings):
        if entry.name == name:
            return entry.default
    raise KeyError(key)


def coercion_error(reason: CoerceReason | None, *, limit: float | None, choices: tuple[str, ...]) -> MessageRef:
    """Bind the inline error for a rejected input."""
    if reason is CoerceReason.NOT_A_CHOICE:
        return _ERROR_NOT_A_CHOICE.bind(choices=DisplaySequence(choices))
    if reason in (CoerceReason.BELOW_MINIMUM, CoerceReason.ABOVE_MAXIMUM):
        rendered = "" if limit is None else (str(int(limit)) if float(limit).is_integer() else str(limit))
        definition = _ERROR_BELOW_MINIMUM if reason is CoerceReason.BELOW_MINIMUM else _ERROR_ABOVE_MAXIMUM
        return definition.bind(limit=rendered)
    if reason is not None and reason in _ERROR_REASONS:
        return _ERROR_REASONS[reason].bind()
    return _ERROR_INVALID.bind()


class SettingRow(Vertical):
    """Base row: owns the label/hint/badge chrome and the save routing.

    Subclasses provide the control and translate its events into
    :meth:`_edited`; everything after that — confirmation, LIVE versus
    persisted routing, projection back from the ports — lives here.
    """

    DEFAULT_CLASSES = "settings-row"

    def __init__(self, spec: SettingSpec, row: SettingRowSpec, ports: SettingsPanelPorts) -> None:
        super().__init__(classes="settings-row")
        self.spec = spec
        self.row = row
        self._ports = ports
        self._last_value: Any = None
        self._error: MessageRef | None = None
        self._provenance: ProvenanceView = ProvenanceView(editable=True)
        self._projected = False
        self.read_only_reason: MessageRef | None = None

    # ── composition ─────────────────────────────────────────────────
    def compose(self) -> ComposeResult:
        with Horizontal(classes="settings-row-main"):
            yield from self.compose_main()
            yield Static("", classes="settings-row-badges")
        with Horizontal(classes="settings-row-hint-line"):
            yield Static(HINT_TREE_GLYPH, classes="settings-row-hint-tree")
            yield Static("", classes="settings-row-hint")

    def compose_main(self) -> ComposeResult:
        """Yield the label and control; overridden per control kind."""
        yield Label("", classes="settings-row-label")
        yield from self.compose_control()

    def compose_control(self) -> ComposeResult:
        raise NotImplementedError

    def on_mount(self) -> None:
        if self._pruning or not self.is_attached or not self.app.is_running:
            # Removed while mounting, or the App quit mid-mount: Textual never
            # started the controls but still sends Mount, and there is nothing
            # to project into. ``projected`` stays False, so nothing commits.
            return
        self.project()

    # ── projection ─────────────────────────────────────────────────
    @property
    def projected(self) -> bool:
        """Whether the controls show the ports' value; until then there is nothing to commit."""
        return self._projected

    def project(self) -> None:
        """Re-read the projected value and provenance and paint them."""
        loaded = self._ports.loaded
        provenance = provenance_view(loaded, self.spec.key)
        self._provenance = provenance
        value = self._ports.projected_value(self.spec.key)
        self._last_value = value
        self._error = None
        self.write_control(value, editable=provenance.editable and self.read_only_reason is None)
        self.refresh_localization()
        self._projected = True

    def write_control(self, value: Any, *, editable: bool) -> None:
        """Push *value* into the control without triggering a save."""
        raise NotImplementedError

    def commit_pending(self) -> None:
        """Commit an edit the control has not reported yet (text inputs)."""
        return

    def refresh_localization(self) -> None:
        """Repaint every piece of text without touching control state."""
        for label in self.query(".settings-row-label"):
            if isinstance(label, Label):
                label.update(self.label_text())
        self.query_one(".settings-row-badges", Static).update(Text(BADGE_SEPARATOR.join(self.badge_texts())))
        hint = self.query_one(".settings-row-hint", Static)
        hint.set_class(self._error is not None, "-error")
        text = self.hint_text()
        self.query_one(".settings-row-hint-line", Horizontal).display = bool(text)
        hint.update(Text(text))

    def label_text(self) -> str:
        label = self.spec.label
        if label is None:
            return self.spec.key
        return render_str(widget_localizer(self), label.bind())

    def badge_texts(self) -> list[str]:
        """Danger and origin only; when a change takes effect is the dialog's status line."""
        localizer = widget_localizer(self)
        badges: list[str] = []
        if self.spec.risk is Risk.DANGEROUS:
            badges.append(render_str(localizer, _BADGE_DANGEROUS.bind()))
        if self._provenance.badge is not None:
            badges.append(render_str(localizer, self._provenance.badge))
        return badges

    def hint_text(self) -> str:
        localizer = widget_localizer(self)
        if self.read_only_reason is not None:
            return render_str(localizer, self.read_only_reason)
        if self._error is not None:
            return render_str(localizer, self._error)
        if self._provenance.hint is not None:
            return render_str(localizer, self._provenance.hint)
        hint = self.row.hint
        if hint is None:
            return ""
        return render_str(localizer, self.bind_hint(hint))

    def bind_hint(self, hint: MessageDef) -> MessageRef:
        if self.spec.key == PROJECT_CONFIG_KEY and not self._last_value:
            dormant = sum(len(entry.keys) for entry in self._ports.loaded.dormant_project)
            if dormant:
                return HINT_PROJECT_CONFIG_DORMANT.bind(count=dormant)
        args = self.row.hint_args
        if args is HintArgs.FIELD_DEFAULT:
            return hint.bind(default=str(field_default(self.spec.key)))
        if args is HintArgs.RETRY_DEFAULTS:
            return hint.bind(default=DEFAULT_MAX_TRANSIENT_RETRIES, maximum=MAX_TRANSIENT_RETRIES_LIMIT)
        if args is HintArgs.SESSION_ROOT_DEFAULT:
            return hint.bind(default=DisplayPath(default_session_root_dir()))
        return hint.bind()

    # ── editing ────────────────────────────────────────────────────
    def _edited(self, value: Any) -> None:
        """A user edit arrived from the control."""
        if self.read_only_reason is not None:
            if value != self._last_value:
                self.project()
            return
        if not self._projected or value == self._last_value:
            # Controls announce their initial state while mounting; nothing
            # counts as an edit until the row has painted the real value.
            return
        self._error = None
        if is_dangerous_transition(self.spec.key, self._last_value, value):
            previous = self._last_value

            def _on_confirm(confirmed: bool) -> None:
                if confirmed:
                    self._dispatch(value)
                else:
                    self.write_control(previous, editable=self._provenance.editable)
                    self.refresh_localization()

            self._ports.confirm(confirm_message_for(self.spec.key), _on_confirm)
            return
        self._dispatch(value)

    def _dispatch(self, value: Any) -> None:
        self._last_value = value
        if self.spec.apply is Apply.LIVE:
            self._ports.apply_live(self.spec.key, value)
        else:
            self._ports.schedule_persist({self.spec.key: value})
        self.refresh_localization()

    def show_error(self, error: MessageRef) -> None:
        self._error = error
        self.refresh_localization()


class BoolRow(SettingRow):
    """A checkbox whose own label is the setting label."""

    def compose_main(self) -> ComposeResult:
        yield Checkbox("", classes="settings-row-checkbox")

    def compose_control(self) -> ComposeResult:
        yield from ()

    def write_control(self, value: Any, *, editable: bool) -> None:
        checkbox = self.query_one(Checkbox)
        with checkbox.prevent(Checkbox.Changed):
            checkbox.value = bool(value)
        checkbox.disabled = not editable

    def refresh_localization(self) -> None:
        checkbox = self.query_one(Checkbox)
        checkbox.label = self.label_text()
        super().refresh_localization()

    @on(Checkbox.Changed)
    def _on_checkbox_changed(self, event: Checkbox.Changed) -> None:
        event.stop()
        self._edited(bool(event.value))


class SelectRow(SettingRow):
    """A closed choice (ENUM) or an open TEXT key with a suggestion list."""

    def compose_control(self) -> ComposeResult:
        yield Select[str]([("", "")], allow_blank=False, value="", classes="settings-row-select")

    def _base_options(self) -> list[tuple[str, str]]:
        """``(display, stored value)`` pairs before the current-value injection.

        Plain strings here; :meth:`_prompts` wraps them for the widget so a
        profile name is never read as markup.
        """
        localizer = widget_localizer(self)
        options: list[tuple[str, str]] = []
        if self.row.suggestions is not None:
            placeholder = _SELECT_ACTIVE if self.spec.key in _ACTIVE_PLACEHOLDER_KEYS else _SELECT_DEFAULT
            options.append((render_str(localizer, placeholder.bind()), ""))
            options.extend((display, stored) for stored, display in self._ports.resolve_choices(self.row.suggestions))
            return options
        choices = self.spec.choices
        if isinstance(choices, ChoiceProvider):
            options.extend((display, stored) for stored, display in self._ports.resolve_choices(choices))
            return options
        for value in choices or ():
            if self.spec.key == _APPROVAL_MODE_KEY and value == "bypass":
                continue
            options.append((self._display_for(value), value))
        return options

    def _display_for(self, value: str) -> str:
        if self.spec.key == "approval.auto_review":
            return render_str(widget_localizer(self), _AUTO_REVIEW_LABELS[value].bind())
        if self.spec.key == _LOCALE_KEY and value in _LOCALE_LABELS:
            return render_str(widget_localizer(self), _LOCALE_LABELS[value].bind())
        return value

    def _options_with(self, value: str) -> list[tuple[str, str]]:
        options = self._base_options()
        if all(stored != value for _display, stored in options):
            current = render_str(widget_localizer(self), _SELECT_CURRENT.bind(value=value or '""'))
            options.append((current, value))
        if self.spec.choices is ChoiceProvider.THEMES:
            options.sort(key=lambda option: (theme_is_read_only(option[1]), option[1]))
        return options

    @staticmethod
    def _prompts(options: list[tuple[str, str]]) -> list[tuple[Text, str]]:
        """Display names come from user-authored profiles: literal text, not markup."""
        return [(Text(display), stored) for display, stored in options]

    def _repaint_select(self, select: Select[str], text: str) -> None:
        """Rebuild the options around *text* and select it, silently.

        ``value`` is a reactive that only notifies on change: when the same
        value is re-selected over new options (a projection that lands on the
        value already shown, a locale switch that renames the placeholder), the
        current-value label would keep the old prompt, so the watcher is forced.
        """
        options = self._options_with(text)
        separators = (
            [stored for _display, stored in options if theme_is_read_only(stored)][:1]
            if self.spec.choices is ChoiceProvider.THEMES
            else []
        )
        with select.prevent(Select.Changed):
            select.set_options(self._prompts(options), separators_before=separators)
            select.value = text
            select.mutate_reactive(Select.value)

    def write_control(self, value: Any, *, editable: bool) -> None:
        select = self.query_one(Select)
        self._repaint_select(select, "" if value is None else str(value))
        select.disabled = not editable

    def refresh_localization(self) -> None:
        select = self.query_one(Select)
        current = select.value
        self._repaint_select(select, "" if not isinstance(current, str) else current)
        super().refresh_localization()

    @on(Select.Changed)
    def _on_select_changed(self, event: Select.Changed) -> None:
        event.stop()
        value = event.value
        if not isinstance(value, str) or value != event.select.value:
            # A stale announcement (the initial mount value overtaken by a
            # projection) no longer describes the control; ignore it.
            return
        self._edited(value)


class InputRow(SettingRow):
    """Free text or a number, committed on Enter or blur and validated first."""

    def __init__(self, spec: SettingSpec, row: SettingRowSpec, ports: SettingsPanelPorts) -> None:
        super().__init__(spec, row, ports)
        self._painted = ""

    def compose_control(self) -> ComposeResult:
        numeric = self.spec.kind in (Kind.INT, Kind.OPTIONAL_INT)
        # Whole numbers only: no sign, no separators (nothing rendered here is negative).
        yield Input(classes="settings-row-input", restrict=r"[0-9]*" if numeric else None)

    def _text_for(self, value: Any) -> str:
        if value is None:
            # ``None`` is "unset" only for a field whose default is ``None``
            # (``llm.retry.max_transient``: the frontend decides). Where the
            # default is a number, ``None`` is the "no limit" sentinel the
            # coercer spells as a non-positive value, so it shows as ``0``.
            if self.spec.kind is Kind.OPTIONAL_INT and field_default(self.spec.key) is not None:
                return "0"
            return ""
        return str(value)

    def write_control(self, value: Any, *, editable: bool) -> None:
        field = self.query_one(Input)
        if not (field.has_focus and field.value != self._painted):
            # A projection landing mid-edit (another row's write, a reload)
            # must not wipe what the user is typing; the commit reconciles.
            self._paint(field, self._text_for(value))
        field.placeholder = self._placeholder_text()
        field.disabled = not editable

    def _placeholder_text(self) -> str:
        """What a blank field falls back to; re-resolved on every projection."""
        if self.row.placeholder is Placeholder.RETRY_DEFAULT:
            return str(DEFAULT_MAX_TRANSIENT_RETRIES)
        return ""

    def _paint(self, field: Input, text: str) -> None:
        with field.prevent(Input.Changed):
            field.value = text
        self._painted = text

    def commit_pending(self) -> None:
        field = self.query_one(Input)
        if field.disabled:
            return
        if field.value == self._text_for(self._last_value):
            return
        self._commit(field.value)

    def _commit(self, raw: str) -> None:
        outcome = self.spec.coerce(raw)
        if outcome.status is CoerceStatus.MISSING:
            if self.spec.kind is Kind.OPTIONAL_INT:
                # Blank means the built-in default, as the hints promise:
                # ``None`` for the retry count, the settings-module default for the ask-user timeout.
                value: Any = field_default(self.spec.key)
            elif self.spec.kind in (Kind.TEXT, Kind.PATH):
                value = ""
            else:
                self.show_error(_ERROR_REQUIRED.bind())
                return
        elif outcome.status is CoerceStatus.INVALID:
            self.show_error(coercion_error(outcome.reason, limit=outcome.limit, choices=outcome.choices))
            return
        else:
            value = outcome.value
        # Paint the canonical text (clamped, trimmed) and mark it as painted:
        # what is committed is no longer "being typed", so a projection that
        # follows — a failed write snapping back, a reload — may replace it
        # even while the field keeps focus.
        self._paint(self.query_one(Input), self._text_for(value))
        self._edited(value)

    @on(Input.Submitted)
    def _on_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self.commit_pending()

    @on(Input.Blurred)
    def _on_blurred(self, event: Input.Blurred) -> None:
        event.stop()
        self.commit_pending()


ROW_CLASSES: dict[Kind, type[SettingRow]] = {
    Kind.BOOL: BoolRow,
    Kind.ENUM: SelectRow,
    Kind.INT: InputRow,
    Kind.OPTIONAL_INT: InputRow,
    Kind.FLOAT: InputRow,
    Kind.TEXT: InputRow,
}


def row_class_for(spec: SettingSpec, row: SettingRowSpec) -> type[SettingRow]:
    """Pick the row class from the spec kind and the layout's row variant."""
    if row.special is RowKind.SESSION_ROOT:
        msg_text = f"{spec.key}: SESSION_ROOT rows are built by the sessions pane"
        raise ValueError(msg_text)
    if spec.kind is Kind.TEXT and row.suggestions is not None:
        return SelectRow
    if spec.kind is Kind.PATH:
        msg_text = f"{spec.key}: PATH rows are only rendered as SESSION_ROOT"
        raise ValueError(msg_text)
    return ROW_CLASSES[spec.kind]


__all__ = [
    "BADGE_SEPARATOR",
    "HINT_TREE_GLYPH",
    "BoolRow",
    "InputRow",
    "SelectRow",
    "SettingRow",
    "confirm_message_for",
    "field_default",
    "is_greyed_layer",
    "row_class_for",
]
