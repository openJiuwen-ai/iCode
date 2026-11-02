# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for Settings defaults and persistence helpers."""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from chrys.foundation.config.coercion import Coercer, CoerceStatus
from chrys.foundation.config.settings import (
    DEFAULT_AGENT_PROFILE,
    DEFAULT_APPROVAL_MODE,
    DEFAULT_ASK_USER_TIMEOUT_SECONDS,
    DEFAULT_EDITOR_KEYMAP,
    DEFAULT_LOCALE,
    DEFAULT_MAX_TRANSIENT_RETRIES,
    DEFAULT_TOOL_RESULT_CEILING_TOKENS,
    DEFAULT_WORKSPACE_CHANGE_NOTICE_MAX_ENTRIES,
    DEFAULT_WORKSPACE_MRU_MAX_ENTRIES,
    HEADLESS_DEFAULT_MAX_TRANSIENT_RETRIES,
    MAX_TRANSIENT_RETRIES_LIMIT,
    TOOL_RESULT_CEILING_FLOOR,
    WORKSPACE_CHANGE_NOTICE_MAX_ENTRIES_LIMIT,
    WORKSPACE_MRU_MAX_ENTRIES_LIMIT,
    Settings,
    parse_max_transient_retries,
    parse_tool_result_ceiling_tokens,
    persist_approval_mode,
    persist_editor_keymap,
    persist_locale,
    persist_theme,
    persist_tool_groups_expanded,
)
from chrys.foundation.config.settings_store import LoadedSettings, SettingsHandle, load_settings
from chrys.foundation.config.spec import Apply, ProjectMerge, Source, kind_accepts, specs_by_field, specs_by_key

# ── Settings.max_transient_retries env loading ──────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, None),
        ("", None),
        ("7", 7),
        ("0", 0),
        (" 12 ", 12),
    ],
)
def test_parse_max_transient_retries_valid_values(raw: str | None, expected: int | None) -> None:
    assert parse_max_transient_retries(raw) == (expected, None)


@pytest.mark.parametrize("raw", ["abc", "-3"])
def test_parse_max_transient_retries_invalid_values_warn(raw: str) -> None:
    value, warning = parse_max_transient_retries(raw)

    assert value is None
    assert warning is not None
    assert "CHRYS_MAX_TRANSIENT_RETRIES" in warning
    assert "non-negative integer" in warning


def test_parse_max_transient_retries_clamps_with_warning() -> None:
    value, warning = parse_max_transient_retries("250")

    assert value == MAX_TRANSIENT_RETRIES_LIMIT == 50
    assert warning is not None
    assert "clamping to 50" in warning


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, DEFAULT_TOOL_RESULT_CEILING_TOKENS),
        ("", DEFAULT_TOOL_RESULT_CEILING_TOKENS),
        ("0", 0),
        ("25000", 25_000),
        ("50000", 50_000),
    ],
)
def test_parse_tool_result_ceiling_valid_values(raw: str | None, expected: int) -> None:
    assert parse_tool_result_ceiling_tokens(raw) == (expected, None)


@pytest.mark.parametrize("raw", ["garbage", "-1"])
def test_parse_tool_result_ceiling_invalid_values_use_default(raw: str) -> None:
    value, warning = parse_tool_result_ceiling_tokens(raw)

    assert value == DEFAULT_TOOL_RESULT_CEILING_TOKENS
    assert warning is not None
    assert "CHRYS_TOOL_RESULT_CEILING_TOKENS" in warning


def test_parse_tool_result_ceiling_clamps_positive_subfloor() -> None:
    value, warning = parse_tool_result_ceiling_tokens("100")

    assert value == TOOL_RESULT_CEILING_FLOOR == 2_000
    assert warning is not None
    assert "clamping" in warning


def test_settings_loads_tool_result_ceiling_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHRYS_TOOL_RESULT_CEILING_TOKENS", "4000")

    assert Settings.from_env().tool_result_ceiling_tokens == 4_000


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(None, None), ("7", 7), ("0", 0), (" 12 ", 12), ("abc", None), ("-3", None), ("250", 50)],
)
def test_settings_max_transient_retries_loader_never_raises(
    monkeypatch: pytest.MonkeyPatch,
    raw: str | None,
    expected: int | None,
) -> None:
    if raw is None:
        monkeypatch.delenv("CHRYS_MAX_TRANSIENT_RETRIES", raising=False)
    else:
        monkeypatch.setenv("CHRYS_MAX_TRANSIENT_RETRIES", raw)

    loaded = load_settings()

    assert loaded.settings.max_transient_retries == expected
    # Warnings are returned rather than logged: whether a rejected value is
    # user-visible depends on which layer offered it, which only the caller
    # knows.
    assert bool(loaded.warnings) is (raw in {"abc", "-3", "250"})


def test_effective_max_transient_retries_uses_interactive_frontend_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CHRYS_MAX_TRANSIENT_RETRIES", raising=False)
    settings = Settings.from_env()

    assert settings.effective_max_transient_retries() == DEFAULT_MAX_TRANSIENT_RETRIES == 10


def test_effective_max_transient_retries_uses_custom_frontend_default() -> None:
    settings = Settings(
        max_transient_retries=None,
        frontend_default_max_transient_retries=HEADLESS_DEFAULT_MAX_TRANSIENT_RETRIES,
    )

    assert settings.effective_max_transient_retries() == HEADLESS_DEFAULT_MAX_TRANSIENT_RETRIES == 18


def test_effective_max_transient_retries_env_value_wins_over_frontend_default() -> None:
    settings = Settings(max_transient_retries=0, frontend_default_max_transient_retries=10)

    assert settings.effective_max_transient_retries() == 0


# ── Settings.default_agent env loading ────────────────────────────────


def test_default_agent_falls_back_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CHRYS_DEFAULT_AGENT", raising=False)
    assert Settings.from_env().default_agent == DEFAULT_AGENT_PROFILE


def test_default_agent_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHRYS_DEFAULT_AGENT", "QA")
    assert Settings.from_env().default_agent == "QA"


def test_default_agent_normalizes_whitespace(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHRYS_DEFAULT_AGENT", "  QA  ")
    assert Settings.from_env().default_agent == "QA"


# ── Settings editor preferences env loading ───────────────────────────


@pytest.mark.parametrize("raw", [None, "", "unknown", "standard-ish"])
def test_editor_keymap_falls_back_to_standard(monkeypatch: pytest.MonkeyPatch, raw: str | None) -> None:
    if raw is None:
        monkeypatch.delenv("CHRYS_EDITOR_KEYMAP", raising=False)
    else:
        monkeypatch.setenv("CHRYS_EDITOR_KEYMAP", raw)
    assert Settings.from_env().editor_keymap == DEFAULT_EDITOR_KEYMAP == "standard"


@pytest.mark.parametrize(("raw", "expected"), [("standard", "standard"), (" EMACS ", "emacs"), ("ViM", "vim")])
def test_editor_keymap_normalizes_valid_values(
    monkeypatch: pytest.MonkeyPatch,
    raw: str,
    expected: str,
) -> None:
    monkeypatch.setenv("CHRYS_EDITOR_KEYMAP", raw)
    assert Settings.from_env().editor_keymap == expected


# ── Settings.ask_user_timeout_seconds env loading ─────────────────────


def test_ask_user_timeout_falls_back_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CHRYS_ASK_USER_TIMEOUT_SECONDS", raising=False)
    assert Settings.from_env().ask_user_timeout_seconds == DEFAULT_ASK_USER_TIMEOUT_SECONDS


def test_ask_user_timeout_reads_positive_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHRYS_ASK_USER_TIMEOUT_SECONDS", "120")
    assert Settings.from_env().ask_user_timeout_seconds == 120


@pytest.mark.parametrize("raw", ["0", "-5", "  ", "not_an_int"])
def test_ask_user_timeout_non_positive_or_garbage(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    monkeypatch.setenv("CHRYS_ASK_USER_TIMEOUT_SECONDS", raw)
    result = Settings.from_env().ask_user_timeout_seconds
    if raw.strip().lstrip("-").isdigit():
        assert result is None  # non-positive disables the timeout
    else:
        assert result == DEFAULT_ASK_USER_TIMEOUT_SECONDS  # empty/garbage → default


# ── Settings.default_approval_mode env loading ─────────────────────────


def test_default_approval_mode_falls_back_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CHRYS_DEFAULT_APPROVAL_MODE", raising=False)
    assert Settings.from_env().default_approval_mode == DEFAULT_APPROVAL_MODE


@pytest.mark.parametrize("raw,expected", [("manual", "manual"), ("auto", "auto"), ("bypass", "bypass")])
def test_default_approval_mode_reads_valid_env(monkeypatch: pytest.MonkeyPatch, raw: str, expected: str) -> None:
    monkeypatch.setenv("CHRYS_DEFAULT_APPROVAL_MODE", raw)
    assert Settings.from_env().default_approval_mode == expected


def test_default_approval_mode_normalizes_case_and_whitespace(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHRYS_DEFAULT_APPROVAL_MODE", "  AUTO  ")
    assert Settings.from_env().default_approval_mode == "auto"


def test_default_approval_mode_rejects_garbage(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHRYS_DEFAULT_APPROVAL_MODE", "not_a_mode")
    assert Settings.from_env().default_approval_mode == DEFAULT_APPROVAL_MODE


# ── Settings.parallel_implicit_tools env loading ──────────────────────


def test_parallel_implicit_tools_defaults_to_true(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CHRYS_PARALLEL_IMPLICIT_TOOLS", raising=False)
    assert Settings.from_env().parallel_implicit_tools is True


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("1", True),
        ("true", True),
        ("yes", True),
        ("on", True),
        ("0", False),
        ("false", False),
        ("no", False),
        ("off", False),
    ],
)
def test_parallel_implicit_tools_reads_env(
    monkeypatch: pytest.MonkeyPatch,
    raw: str,
    expected: bool,
) -> None:
    monkeypatch.setenv("CHRYS_PARALLEL_IMPLICIT_TOOLS", raw)
    assert Settings.from_env().parallel_implicit_tools is expected


@pytest.mark.parametrize("raw", ["disabled", "2", "nonsense"])
def test_a_flag_spelled_outside_the_grammar_no_longer_means_false(
    monkeypatch: pytest.MonkeyPatch,
    raw: str,
) -> None:
    """Declared behaviour change: garbage is rejected, not read as "off".

    The env reader used to treat every non-truthy string as false, so a typo
    silently disabled a feature. An unrecognised value now contributes nothing
    and the layer below decides — here, the built-in default.
    """
    monkeypatch.setenv("CHRYS_PARALLEL_IMPLICIT_TOOLS", raw)
    loaded = load_settings()

    assert loaded.settings.parallel_implicit_tools is True
    assert [warning.key for warning in loaded.warnings] == ["mutations.parallel_implicit_tools"]
    assert loaded.warnings[0].rejected is True
    assert loaded.warnings[0].outcome.raw == raw


# ── Settings.workspace_mru_max_entries env loading ────────────────────


def test_workspace_mru_max_entries_falls_back_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CHRYS_WORKSPACE_MRU_MAX_ENTRIES", raising=False)
    assert Settings.from_env().workspace_mru_max_entries == DEFAULT_WORKSPACE_MRU_MAX_ENTRIES
    assert DEFAULT_WORKSPACE_MRU_MAX_ENTRIES == 20


def test_workspace_mru_max_entries_reads_positive_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHRYS_WORKSPACE_MRU_MAX_ENTRIES", "35")
    assert Settings.from_env().workspace_mru_max_entries == 35


def test_workspace_mru_max_entries_clamps_to_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHRYS_WORKSPACE_MRU_MAX_ENTRIES", "5000")
    assert Settings.from_env().workspace_mru_max_entries == WORKSPACE_MRU_MAX_ENTRIES_LIMIT


@pytest.mark.parametrize("raw", ["0", "-5"])
def test_workspace_mru_max_entries_non_positive_disables(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    monkeypatch.setenv("CHRYS_WORKSPACE_MRU_MAX_ENTRIES", raw)
    assert Settings.from_env().workspace_mru_max_entries == 0


@pytest.mark.parametrize("raw", ["  ", "not_an_int", "1.5"])
def test_workspace_mru_max_entries_garbage_falls_back(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    monkeypatch.setenv("CHRYS_WORKSPACE_MRU_MAX_ENTRIES", raw)
    assert Settings.from_env().workspace_mru_max_entries == DEFAULT_WORKSPACE_MRU_MAX_ENTRIES


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("0", 1),
        ("-20", 1),
        ("1", 1),
        ("35", 35),
        ("100", 100),
        ("5000", WORKSPACE_CHANGE_NOTICE_MAX_ENTRIES_LIMIT),
        ("not-an-int", DEFAULT_WORKSPACE_CHANGE_NOTICE_MAX_ENTRIES),
    ],
)
def test_workspace_change_notice_max_entries_clamps(
    monkeypatch: pytest.MonkeyPatch,
    raw: str,
    expected: int,
) -> None:
    monkeypatch.setenv("CHRYS_WORKSPACE_CHANGE_NOTICE_MAX_ENTRIES", raw)
    assert Settings.from_env().workspace_change_notice_max_entries == expected


@pytest.mark.parametrize(("raw", "expected"), [("0", False), ("false", False), ("1", True), ("true", True)])
def test_workspace_change_notice_reads_env(monkeypatch: pytest.MonkeyPatch, raw: str, expected: bool) -> None:
    monkeypatch.setenv("CHRYS_WORKSPACE_CHANGE_NOTICE", raw)
    assert Settings.from_env().workspace_change_notice is expected


@pytest.mark.parametrize("raw", [None, "", "   "])
def test_settings_locale_defaults_to_system_when_environment_is_missing_or_blank(
    monkeypatch: pytest.MonkeyPatch,
    raw: str | None,
) -> None:
    if raw is None:
        monkeypatch.delenv("CHRYS_LOCALE", raising=False)
    else:
        monkeypatch.setenv("CHRYS_LOCALE", raw)

    assert Settings.from_env().locale == DEFAULT_LOCALE == "system"


def test_settings_locale_strips_and_preserves_configured_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHRYS_LOCALE", "  custom_LOCALE  ")

    assert Settings.from_env().locale == "custom_LOCALE"


# ── mutations.trace.mode: two grammars, one field ─────────────────────


@pytest.mark.parametrize(("raw", "expected"), [("off", "off"), ("0", "off"), ("no", "off"), ("auto", "auto")])
def test_mutation_trace_env_keeps_its_historical_off_spellings(
    monkeypatch: pytest.MonkeyPatch,
    raw: str,
    expected: str,
) -> None:
    monkeypatch.setenv("CHRYS_MUTATION_TRACE", raw)

    assert Settings.from_env().mutation_trace_mode == expected


def test_mutation_trace_env_still_reads_an_unknown_spelling_as_auto(monkeypatch: pytest.MonkeyPatch) -> None:
    """The variable has always meant "anything but off is auto"; keep that."""
    monkeypatch.setenv("CHRYS_MUTATION_TRACE", "verbose")
    loaded = load_settings()

    assert loaded.settings.mutation_trace_mode == "auto"
    assert loaded.warnings == ()


def test_the_dotted_trace_key_uses_the_closed_choice_grammar() -> None:
    """The new YAML surface has no history to preserve, so it validates.

    Inheriting the variable's leniency would let a container or a bool land as
    ``VALID("auto")``, which is exactly what the closed-choice rule forbids.
    """
    coerce = specs_by_key(Settings)["mutations.trace.mode"].coerce

    assert coerce("off").value == "off"
    assert coerce("auto").value == "auto"
    for raw in (["off"], {"mode": "off"}, True, 0):
        assert coerce(raw).status is CoerceStatus.INVALID
    assert coerce("verbose").status is CoerceStatus.INVALID


# ── the no-throw contract, swept over the whole schema ────────────────


def _coercers_with_keys() -> list[pytest.ParameterSet]:
    """Every coercer the loader can reach, both grammars where they differ."""
    params: list[pytest.ParameterSet] = []
    for key, entry in specs_by_key(Settings).items():
        params.append(pytest.param(key, "coerce", entry.coerce, id=f"{key}:coerce"))
        if entry.env_coerce is not None:
            params.append(pytest.param(key, "env_coerce", entry.env_coerce, id=f"{key}:env_coerce"))
    return params


def _coercers_under_test() -> list[pytest.ParameterSet]:
    """The same set, for the checks that do not need the field's identity."""
    return [pytest.param(param.values[2], id=param.id) for param in _coercers_with_keys()]


# One raw value per shape a layer can hand over: YAML parses native types and
# containers, the environment hands over strings, and an oversized
# integer is the value whose *rendering* raises rather than its parsing.
_RAW_SHAPES = [
    pytest.param(None, id="null"),
    pytest.param(True, id="true"),
    pytest.param(False, id="false"),
    pytest.param(0, id="zero"),
    pytest.param(7, id="int"),
    pytest.param(-7, id="negative"),
    pytest.param(1.5, id="float"),
    pytest.param(float("nan"), id="nan"),
    pytest.param(float("inf"), id="inf"),
    pytest.param("", id="empty"),
    pytest.param("   ", id="blank"),
    pytest.param("x", id="word"),
    pytest.param("9" * 100, id="long-digits"),
    pytest.param([1, 2], id="list"),
    pytest.param({"a": 1}, id="mapping"),
    pytest.param(b"x", id="bytes"),
    pytest.param(10 ** (sys.get_int_max_str_digits() + 10), id="unrenderable-int"),
]


@pytest.mark.parametrize("coerce", _coercers_under_test())
@pytest.mark.parametrize("raw", _RAW_SHAPES)
def test_no_shipped_coercer_raises_on_any_raw_shape(coerce: Coercer, raw: object) -> None:
    """A hand-edited YAML file must never crash the load.

    Swept rather than hand-listed because the hole this closes was in the two
    fields that carry their *own* coercer — exactly the ones a per-field test
    is least likely to cover.
    """
    outcome = coerce(raw)

    # Rendering the outcome is part of the contract: a warning that cannot be
    # formatted breaks the load just as thoroughly as a parse that raises.
    assert isinstance(f"{outcome.raw}", str)


@pytest.mark.parametrize(("key", "grammar", "coerce"), _coercers_with_keys())
@pytest.mark.parametrize("raw", _RAW_SHAPES)
def test_an_accepted_value_matches_the_kind_the_field_declares(
    key: str,
    grammar: str,
    coerce: Coercer,
    raw: object,
) -> None:
    """The other half of the contract: not raising is not the same as correct.

    A coercer that returns ``VALID`` with the wrong Python type puts that type
    straight into ``Settings``, past the annotation and past the pin check that
    trusts ``Kind`` — the field would be a ``str`` where every reader expects an
    ``int``, and nothing between here and the crash would notice.
    """
    outcome = coerce(raw)
    if not outcome.usable:
        return

    kind = specs_by_key(Settings)[key].kind
    assert kind_accepts(kind, outcome.value), f"{key}/{grammar} returned {type(outcome.value).__name__} for {kind.name}"


# ── InvalidPolicy.SAFE_DEFAULT ────────────────────────────────────────


def test_a_rejected_dangerous_value_seals_the_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """A typo meant to disable capture must not fall through to a saved ``true``."""
    monkeypatch.setenv("CHRYS_DEBUG_LLM_RAW_HTTP_LOG", "fales")
    loaded = load_settings()

    assert loaded.settings.raw_http_capture is False
    assert "log.raw_http_capture" in loaded.sealed_keys
    assert [warning.key for warning in loaded.warnings] == ["log.raw_http_capture"]


def test_a_rejected_preference_leaves_the_key_open(monkeypatch: pytest.MonkeyPatch) -> None:
    """The default policy: a lower layer still gets to answer for a preference."""
    monkeypatch.setenv("CHRYS_SESSION_TITLE_AUTO", "nonsense")
    loaded = load_settings()

    assert loaded.sealed_keys == frozenset()
    assert [warning.key for warning in loaded.warnings] == ["session.title.auto"]


def test_a_sealed_key_always_reports_the_default_as_its_source() -> None:
    """The invariant that makes ``sealed_keys`` usable by the panel.

    A sealed key whose provenance still named the layer that wrote it would
    have the panel point at a file whose value is deliberately not in force.
    """
    loaded = load_settings(env={"CHRYS_DEBUG_LLM_RAW_HTTP_LOG": "fales"})

    for key in loaded.sealed_keys:
        assert loaded.source_for(key).layer is Source.DEFAULT
    assert loaded.settings.raw_http_capture is False


# ── pins ──────────────────────────────────────────────────────────────


def test_a_pin_outranks_a_rejected_env_value_instead_of_being_sealed_out() -> None:
    """Sealing must not reach a layer that never got to answer.

    Resolving upwards, the typo below would seal the key *after* the pin had
    already supplied a value — recording SESSION as the source of a value the
    seal says is the built-in default.
    """
    loaded = load_settings(env={"CHRYS_DEBUG_LLM_RAW_HTTP_LOG": "fales"}, raw_http_capture=False)

    assert loaded.settings.raw_http_capture is False
    assert loaded.source_for("log.raw_http_capture").layer is Source.SESSION
    assert loaded.sealed_keys == frozenset()
    assert loaded.warnings == ()


@pytest.mark.parametrize(
    ("pin", "value"),
    [
        ("raw_http_capture", "false"),
        ("workspace_mru_max_entries", True),
        ("model_profile", 3),
        ("ask_user_timeout_seconds", "30"),
        ("theme", None),
    ],
)
def test_a_pin_of_the_wrong_type_is_a_programming_error(pin: str, value: object) -> None:
    """Not a warning: no user can fix it, and the old code stored it verbatim.

    ``raw_http_capture="false"`` is the one that motivates this — a non-empty
    string is truthy, so the pin meant to disable capture would have enabled it.
    """
    with pytest.raises(TypeError):
        load_settings(**{pin: value})


@pytest.mark.parametrize(("pin", "value"), [("ask_user_timeout_seconds", None), ("model_profile", "")])
def test_a_pin_may_carry_the_values_a_coercer_calls_missing(pin: str, value: object) -> None:
    """Why pins are type-checked rather than run through the field's coercer.

    ``None`` and ``""`` are the real in-memory values of an unset optional and
    an unset text field; a coercer reports both as ``MISSING``, so routing pins
    through one would drop exactly the pins ACP and session restore rely on.
    Closed enums are the exception, in the test below.
    """
    loaded = load_settings(**{pin: value})

    assert getattr(loaded.settings, pin) == value
    assert loaded.source_for(specs_by_field(Settings)[pin].key).layer is Source.SESSION


@pytest.mark.parametrize(
    "pin",
    ["default_approval_mode", "theme", "locale", "editor_keymap", "mutation_trace_mode"],
)
def test_a_blank_pin_is_rejected_for_a_closed_choice_set(pin: str) -> None:
    """The kind whose domain is written down in full has no unset spelling.

    Every coercer answers ``MISSING`` for ``""``, so taking that verbatim — the
    concession the fields above need — would let a blank into a field whose
    legal values are exactly its ``choices``, past the canonical check that
    rejects every other value outside the set.
    """
    with pytest.raises(TypeError):
        load_settings(**{pin: ""})


def test_a_live_choice_survives_a_reload_of_the_layers_beneath_it() -> None:
    """``Source.RUNTIME`` is the layer nothing loads from, so a load cannot carry it.

    A reload re-reads the files and the environment and knows nothing about a
    theme the user picked this session. Installing it wholesale would revert the
    choice — and the TUI would not follow it back, because Textual goes on
    showing the theme that was chosen, so the settings would simply stop
    describing the screen. It matters beyond attribution because
    ``persist_theme`` logs and swallows its failures: when the write never
    landed, this overlay is the only record the choice was made.
    """
    handle = SettingsHandle(LoadedSettings(settings=Settings(theme="chrys", locale="en"), provenance={}))
    handle.override(theme="chrys-legacy")

    handle.install(LoadedSettings(settings=Settings(theme="chrys", locale="zh-Hans"), provenance={}))

    assert handle.settings.theme == "chrys-legacy"
    assert handle.loaded.source_for("ui.theme").layer is Source.RUNTIME
    # The reload still lands for every key nobody overrode.
    assert handle.settings.locale == "zh-Hans"


def test_a_rollback_restores_the_load_without_erasing_a_concurrent_choice() -> None:
    """A rebuild snapshots the settings, then awaits; the user can act in between.

    Restoring the snapshot whole would take the theme they picked during those
    awaits with it. Runtime choices are re-applied over whatever load is
    installed, so the rollback undoes only the layers it actually owns.
    """
    base = LoadedSettings(settings=Settings(theme="chrys"), provenance={})
    handle = SettingsHandle(base)
    snapshot = handle.loaded

    handle.override(theme="chrys-ansi")
    handle.install(snapshot)

    assert handle.settings.theme == "chrys-ansi"

    # ...while a value the rebuild itself pinned is still rolled back.
    handle.install(snapshot.overlay(Source.SESSION, model_profile="pinned-by-rebuild"))
    handle.install(snapshot)

    assert handle.settings.model_profile == ""


def test_a_blank_pin_cannot_launder_away_a_seal(monkeypatch: pytest.MonkeyPatch) -> None:
    """The sharp end of the case above, on the field that fails least safely.

    A rejected value is what seals a dangerous key at its built-in default, so
    a blank accepted verbatim would not just store a nonsense mode — it would
    replace the sealed one, dropping the record that the environment had asked
    for something illegal.
    """
    monkeypatch.setenv("CHRYS_DEFAULT_APPROVAL_MODE", "garbage")
    key = specs_by_field(Settings)["default_approval_mode"].key

    sealed = load_settings()
    assert sealed.settings.default_approval_mode == "manual"
    assert key in sealed.sealed_keys

    with pytest.raises(TypeError):
        load_settings(default_approval_mode="")
    with pytest.raises(TypeError):
        sealed.overlay(Source.RUNTIME, default_approval_mode="")


def test_the_frontend_retry_default_refuses_to_be_pinned() -> None:
    """It travels as the eval context, and a pin would be silently overwritten."""
    with pytest.raises(TypeError, match="eval_context"):
        load_settings(frontend_default_max_transient_retries=15)


@pytest.mark.parametrize(
    ("pin", "value"),
    [
        pytest.param("tool_result_ceiling_tokens", -1, id="uncaps-a-safe-default-backstop"),
        pytest.param("default_approval_mode", "definitely-not-a-mode", id="outside-a-closed-choice-set"),
        pytest.param("warn_threshold_pct", 999.0, id="past-a-declared-bound"),
        pytest.param("ask_user_timeout_seconds", -30, id="negative-where-none-means-unset"),
        pytest.param("model_profile", "  padded  ", id="not-the-canonical-spelling"),
    ],
)
def test_a_pin_outside_the_fields_domain_is_a_programming_error(pin: str, value: object) -> None:
    """The right type is not the same as a legal value.

    ``tool_result_ceiling_tokens=-1`` is the one that matters: the field carries
    ``SAFE_DEFAULT`` so that a bad value can never uncap the backstop, and the
    consumer reads any non-positive ceiling as "no ceiling" — so a pin that only
    had to be an ``int`` would walk straight past the policy.
    """
    with pytest.raises(TypeError):
        load_settings(**{pin: value})


def test_a_pin_that_survives_the_domain_check_keeps_its_value() -> None:
    """The check must not start rewriting what callers asked for."""
    loaded = load_settings(env={}, tool_result_ceiling_tokens=4096, default_approval_mode="bypass")

    assert loaded.settings.tool_result_ceiling_tokens == 4096
    assert loaded.settings.default_approval_mode == "bypass"


def test_an_overlay_records_the_layer_that_supplied_the_value() -> None:
    """``--model`` is a CLI value; a bare ``replace()`` reads back as whatever won."""
    loaded = load_settings(env={}).overlay(Source.CLI, model_profile="chosen-by-flag")

    assert loaded.settings.model_profile == "chosen-by-flag"
    assert loaded.source_for("model.profile.active").layer is Source.CLI


def test_an_overlay_refuses_a_value_the_field_would_never_accept() -> None:
    with pytest.raises(TypeError):
        load_settings(env={}).overlay(Source.CLI, tool_result_ceiling_tokens=-1)


def test_an_overlay_refuses_to_impersonate_a_file() -> None:
    """A file layer's origin has to name its file, which an overlay cannot."""
    with pytest.raises(ValueError, match="file"):
        load_settings(env={}).overlay(Source.PROJECT, model_profile="x")


def test_an_overlay_unseals_the_key_it_writes() -> None:
    """Otherwise the same key is both "in force from CLI" and "sealed to the default".

    ``sealed_keys`` is what the panel uses to explain why a value the user can
    see is not the one running. Left set after an overlay, it would explain a
    built-in default that nothing is using.
    """
    sealed = load_settings(env={"CHRYS_DEBUG_LLM_RAW_HTTP_LOG": "fales"})
    assert "log.raw_http_capture" in sealed.sealed_keys

    loaded = sealed.overlay(Source.CLI, raw_http_capture=True)

    assert loaded.settings.raw_http_capture is True
    assert loaded.source_for("log.raw_http_capture").layer is Source.CLI
    assert loaded.sealed_keys == frozenset()


def test_an_overlay_leaves_the_seal_on_keys_it_did_not_write() -> None:
    sealed = load_settings(env={"CHRYS_DEBUG_LLM_RAW_HTTP_LOG": "fales"})

    loaded = sealed.overlay(Source.CLI, model_profile="chosen-by-flag")

    assert loaded.sealed_keys == frozenset({"log.raw_http_capture"})


# ── panel labels ───────────────────────────────────────────────────────


def test_every_persisted_field_carries_a_label_keyed_by_its_dotted_key() -> None:
    """The panel renders the label; the catalog finds it under the field's key.

    Both halves are checked: that no persisted field is left unlabelled (the
    spec invariant guards a single construction, this guards the shipped set),
    and that the message id follows ``settings.<key>.label`` so the catalog
    entry can be found from the field alone.
    """
    persisted = {key: found for key, found in specs_by_key(Settings).items() if found.persist}
    assert persisted, "the audit below is vacuous without persisted fields"

    unlabelled = sorted(key for key, found in persisted.items() if found.label is None)
    assert unlabelled == []

    misnamed = {
        key: found.label.key
        for key, found in persisted.items()
        if found.label is not None and found.label.key != f"settings.{key}.label"
    }
    assert misnamed == {}

    # The panel never shows a runtime-only field, so a label there is dead copy.
    runtime_only = [key for key, found in specs_by_key(Settings).items() if not found.persist]
    assert runtime_only == ["model.profile.override", "model.profile.override_sub_agents", "llm.retry.frontend_default"]
    assert all(specs_by_key(Settings)[key].label is None for key in runtime_only)


# ── persistence helpers ────────────────────────────────────────────────


@pytest.fixture
def fake_config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Redirect ``get_platform().config_dir`` to a temp directory."""
    import dataclasses

    from chrys.foundation import platform as platform_mod

    config_dir = tmp_path / "nested" / "config"
    fake = dataclasses.replace(platform_mod.get_platform(), config_dir=config_dir)
    monkeypatch.setattr(platform_mod, "get_platform", lambda: fake)
    # Clear any ambient env var so a mirror write (a contract violation —
    # persist_* must never touch os.environ) is reliably observable.
    for key in ("CHRYS_DEFAULT_APPROVAL_MODE", "CHRYS_EDITOR_KEYMAP", "CHRYS_LOCALE", "CHRYS_THEME"):
        monkeypatch.delenv(key, raising=False)
    yield config_dir


def _read_setting(config_dir: Path, dotted: str) -> object | None:
    """Read one dotted key back from the user settings document, or None."""
    settings_path = config_dir / "settings.yaml"
    if not settings_path.exists():
        return None
    node: object = yaml.safe_load(settings_path.read_text(encoding="utf-8"))
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def test_persist_theme_writes_the_settings_document_without_an_env_mirror(fake_config_dir: Path) -> None:
    persist_theme("midnight")

    assert _read_setting(fake_config_dir, "ui.theme") == "midnight"
    # A mirror would come back as the ENV layer and outrank the document.
    assert "CHRYS_THEME" not in os.environ
    assert not (fake_config_dir / ".env").exists()


def test_persist_locale_writes_the_settings_document_without_an_env_mirror(fake_config_dir: Path) -> None:
    persist_locale("zh-Hans")

    assert _read_setting(fake_config_dir, "ui.locale") == "zh-Hans"
    assert "CHRYS_LOCALE" not in os.environ


def test_thinking_streams_folded_by_default_and_only_the_user_settings_change_it_live() -> None:
    found = specs_by_key(Settings)["ui.chat.show_thinking"]

    assert Settings().chat_thinking_expanded is False
    assert (found.apply, found.env, found.project_merge) == (Apply.LIVE, None, ProjectMerge.DENY)


def test_completed_turn_folding_is_user_owned_and_enabled_by_default() -> None:
    found = specs_by_key(Settings)["ui.chat.fold_finished_turns"]
    assert Settings().chat_fold_finished_turns is True
    assert (found.apply, found.env, found.project_merge) == (Apply.LIVE, None, ProjectMerge.DENY)


def test_tool_groups_default_collapsed_and_persist_native_bool(fake_config_dir: Path) -> None:
    assert Settings().tool_groups_expanded is False

    persist_tool_groups_expanded(True)

    value = _read_setting(fake_config_dir, "ui.chat.tool_groups_expanded")
    assert value is True
    assert type(value) is bool


@pytest.mark.parametrize(
    "value",
    ['"zh-Hans"', "weird value # hash", "single'quote", "multi\nline"],
    ids=["double-quoted", "hash-comment", "single-quote", "multiline"],
)
def test_persist_locale_round_trips_hostile_values(
    fake_config_dir: Path,
    value: str,
) -> None:
    # A persisted value that reloads differently from what was written would
    # make this start and the next one disagree about the effective locale.
    # YAML carries quoting, comments, and newlines that a line-based store
    # had to reject.
    persist_locale(value)

    assert _read_setting(fake_config_dir, "ui.locale") == value


def test_persist_locale_logs_disk_failure_without_the_value(
    fake_config_dir: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    with patch("chrys.foundation.config.settings_store.update_yaml_doc", side_effect=OSError("disk full")):
        persist_locale("sentinel-locale-value")

    # Content-free logging by design: the locale value must not reach the log.
    assert "Failed to persist ui.locale" in caplog.text
    assert "sentinel-locale-value" not in caplog.text
    assert _read_setting(fake_config_dir, "ui.locale") is None


# ── persist_approval_mode ──────────────────────────────────────────────


def test_persist_writes_manual_and_auto(fake_config_dir: Path) -> None:
    persist_approval_mode("manual")
    assert _read_setting(fake_config_dir, "approval.default_mode") == "manual"

    persist_approval_mode("auto")
    assert _read_setting(fake_config_dir, "approval.default_mode") == "auto"
    assert "CHRYS_DEFAULT_APPROVAL_MODE" not in os.environ


def test_persist_downgrades_bypass_to_auto(fake_config_dir: Path) -> None:
    """BYPASS must never survive to next startup — written as AUTO."""
    persist_approval_mode("bypass")
    assert _read_setting(fake_config_dir, "approval.default_mode") == "auto"


def test_persist_ignores_invalid_mode(fake_config_dir: Path, caplog: pytest.LogCaptureFixture) -> None:
    persist_approval_mode("garbage")

    assert "Refusing to persist approval.default_mode" in caplog.text
    assert _read_setting(fake_config_dir, "approval.default_mode") is None


def test_persist_swallows_io_errors(fake_config_dir: Path) -> None:
    """A filesystem error must be logged, not raised — same contract as persist_theme."""
    with patch("chrys.foundation.config.settings_store.update_yaml_doc", side_effect=OSError("disk full")):
        # Should not raise.
        persist_approval_mode("auto")


# ── persist_editor_keymap ────────────────────────────────────────────


@pytest.mark.parametrize("mode", ["standard", " EMACS ", "ViM"])
def test_persist_editor_keymap_normalizes_and_writes_the_document(
    fake_config_dir: Path,
    mode: str,
) -> None:
    persist_editor_keymap(mode)

    assert _read_setting(fake_config_dir, "ui.editor.keymap") == mode.strip().lower()
    assert "CHRYS_EDITOR_KEYMAP" not in os.environ


def test_persist_editor_keymap_rejects_unknown_mode(fake_config_dir: Path) -> None:
    persist_editor_keymap("unknown")

    assert _read_setting(fake_config_dir, "ui.editor.keymap") is None
    assert "CHRYS_EDITOR_KEYMAP" not in os.environ


def test_persist_editor_keymap_logs_and_swallows_failures(
    fake_config_dir: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    with patch("chrys.foundation.config.settings_store.update_yaml_doc", side_effect=OSError("disk full")):
        persist_editor_keymap("vim")

    assert "Failed to persist ui.editor.keymap" in caplog.text
    assert "CHRYS_EDITOR_KEYMAP" not in os.environ
