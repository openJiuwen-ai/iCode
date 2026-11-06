# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Coercer semantics, including parity with the env-only loaders they replace."""

from __future__ import annotations

import math
import sys
from typing import Any

import pytest

from chrys.foundation.config import settings as settings_module
from chrys.foundation.config.coercion import (
    Coercer,
    CoerceReason,
    CoerceStatus,
    bool_coercer,
    choice_coercer,
    float_coercer,
    int_coercer,
    optional_int_coercer,
    text_coercer,
)
from chrys.foundation.i18n.locale import SUPPORTED_LOCALES


def resolve(coerce: Coercer, raw: object, default: Any) -> Any:
    """Mimic the loader: an unusable result means this layer says nothing.

    In an env-only world the layer below is always the dataclass default, so
    this is exactly what the ``_load_*`` functions do today.
    """
    result = coerce(raw)
    return result.value if result.usable else default


# ── bool ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1", True),
        ("true", True),
        ("TRUE", True),
        (" yes ", True),
        ("on", True),
        ("0", False),
        ("no", False),
        ("off", False),
        ("FALSE", False),
        (True, True),
        (False, False),
        (1, True),
        (0, False),
    ],
)
def test_bool_coercer_accepts_both_spellings(raw: object, expected: bool) -> None:
    result = bool_coercer()(raw)

    assert result.status is CoerceStatus.VALID
    assert result.value is expected


@pytest.mark.parametrize("raw", ["", "   ", None])
def test_bool_coercer_treats_blank_as_missing(raw: object) -> None:
    assert bool_coercer()(raw).status is CoerceStatus.MISSING


@pytest.mark.parametrize("raw", ["nonsense", "7", 7, -1, 2.0, [], "true false"])
def test_bool_coercer_rejects_everything_outside_the_grammar(raw: object) -> None:
    result = bool_coercer()(raw)

    assert result.status is CoerceStatus.INVALID
    assert result.usable is False


def test_bool_coercer_is_agnostic_to_yaml_versus_environment_spelling() -> None:
    """The regression F4 named: ``7`` and ``"7"`` must not disagree."""
    assert bool_coercer()("7").status is bool_coercer()(7).status


@pytest.mark.parametrize("raw", ["1", "true", "0", "no", ""])
def test_bool_coercer_agrees_with_the_env_loader(raw: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHRYS_PROBE_FLAG", raw)

    for default in (True, False):
        expected = settings_module._load_bool_env("CHRYS_PROBE_FLAG", default=default)
        assert resolve(bool_coercer(), raw, default) == expected


def test_bool_coercer_diverges_from_the_env_loader_on_garbage(monkeypatch: pytest.MonkeyPatch) -> None:
    """Declared behaviour change: garbage no longer silently means ``false``."""
    monkeypatch.setenv("CHRYS_PROBE_FLAG", "nonsense")

    assert settings_module._load_bool_env("CHRYS_PROBE_FLAG", default=True) is False
    assert resolve(bool_coercer(), "nonsense", True) is True


# ── int ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("20", 20), (20, 20), ("0", 0), ("-5", 0), ("500", 100), (" 37 ", 37)],
)
def test_int_coercer_reproduces_workspace_mru_semantics(raw: object, expected: int) -> None:
    coerce = int_coercer(non_positive=0, maximum=100)

    assert coerce(raw).value == expected


@pytest.mark.parametrize("raw", ["", "   ", None])
def test_int_coercer_treats_blank_as_missing(raw: object) -> None:
    assert int_coercer()(raw).status is CoerceStatus.MISSING


@pytest.mark.parametrize("raw", ["garbage", "1.5", "0x10", True, False, 2.5, []])
def test_int_coercer_rejects_non_integers(raw: object) -> None:
    assert int_coercer()(raw).status is CoerceStatus.INVALID


def test_int_coercer_clamps_and_reports() -> None:
    result = int_coercer(minimum=1, maximum=100)("500")

    assert result.status is CoerceStatus.CLAMPED
    assert result.usable is True
    assert result.value == 100
    assert result.raw == "500"
    assert result.limit == 100


def test_int_coercer_distinguishes_garbage_from_a_legitimate_default() -> None:
    """The regression F5 named: the loader must be able to tell these apart."""
    garbage = int_coercer(maximum=100)("nope")
    legitimate = int_coercer(maximum=100)("20")

    assert garbage.usable is False
    assert legitimate.usable is True
    assert legitimate.value == 20


@pytest.mark.parametrize("raw", ["20", "", "   ", "garbage", "0", "-5", "500", "37"])
def test_int_coercer_agrees_with_the_env_loader_it_replaces(raw: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHRYS_WORKSPACE_MRU_MAX_ENTRIES", raw)
    coerce = int_coercer(non_positive=0, maximum=100)

    assert resolve(coerce, raw, 20) == settings_module._load_workspace_mru_max_entries()


@pytest.mark.parametrize("raw", ["50", "", "garbage", "0", "-5", "500", "37"])
def test_int_coercer_agrees_with_the_change_notice_loader(raw: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHRYS_WORKSPACE_CHANGE_NOTICE_MAX_ENTRIES", raw)
    coerce = int_coercer(minimum=1, maximum=100)

    assert resolve(coerce, raw, 50) == settings_module._load_workspace_change_notice_max_entries()


@pytest.mark.parametrize("raw", ["50", "", "garbage", "0", "-5", "9999"])
def test_int_coercer_agrees_with_the_snapshot_size_loader(raw: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHRYS_MUTATION_SNAPSHOT_MAX_FILE_MB", raw)
    coerce = int_coercer(non_positive=0)

    assert resolve(coerce, raw, 50) == settings_module._load_mutation_snapshot_max_file_mb()


# ── optional int ──────────────────────────────────────────────────


@pytest.mark.parametrize(("raw", "expected"), [("600", 600), (600, 600), ("0", None), ("-1", None)])
def test_optional_int_coercer_reproduces_ask_user_timeout_semantics(raw: object, expected: int | None) -> None:
    result = optional_int_coercer()(raw)

    assert result.status is CoerceStatus.VALID
    assert result.value == expected


@pytest.mark.parametrize("raw", ["600", "", "garbage", "0", "-1", "45"])
def test_optional_int_coercer_agrees_with_the_ask_user_loader(raw: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHRYS_ASK_USER_TIMEOUT_SECONDS", raw)

    assert resolve(optional_int_coercer(), raw, 900) == settings_module._load_ask_user_timeout()


# ── float ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(("raw", "expected"), [("0.5", 0.5), (0.5, 0.5), (1, 1.0), (" .25 ", 0.25)])
def test_float_coercer_accepts_finite_numbers(raw: object, expected: float) -> None:
    result = float_coercer(minimum=0.0, maximum=1.0)(raw)

    assert result.status is CoerceStatus.VALID
    assert result.value == expected


def test_float_coercer_clamps() -> None:
    result = float_coercer(minimum=0.0, maximum=1.0)("1.5")

    assert result.status is CoerceStatus.CLAMPED
    assert result.value == 1.0
    assert result.limit == 1.0


@pytest.mark.parametrize("raw", ["nan", "NaN", "inf", "-inf", "Infinity", math.nan, math.inf, -math.inf])
def test_float_coercer_rejects_non_finite_values(raw: object) -> None:
    """The regression F6 named: NaN slips past both bounds and disables the check."""
    result = float_coercer(minimum=0.0, maximum=1.0)(raw)

    assert result.status is CoerceStatus.INVALID
    assert result.usable is False


@pytest.mark.parametrize("raw", ["", "   ", None])
def test_float_coercer_treats_blank_as_missing(raw: object) -> None:
    assert float_coercer()(raw).status is CoerceStatus.MISSING


@pytest.mark.parametrize("raw", ["x", True, []])
def test_float_coercer_rejects_non_numbers(raw: object) -> None:
    assert float_coercer()(raw).status is CoerceStatus.INVALID


def test_float_coercer_rejects_a_native_int_too_large_to_convert() -> None:
    """``yaml.safe_load`` yields these as ``int``; ``float()`` then raises.

    The contract is that a coercer never raises, so a hand-edited YAML number
    has to come back INVALID rather than take the whole load down.
    """
    result = float_coercer(minimum=0.0, maximum=1.0)(10**400)

    assert result.status is CoerceStatus.INVALID
    assert result.usable is False


def test_rendering_a_rejected_value_cannot_itself_raise() -> None:
    """``str()`` of an int past the digit limit raises; the raw field needs it."""
    huge = 10 ** (sys.get_int_max_str_digits() + 10)

    result = float_coercer()(huge)

    assert result.status is CoerceStatus.INVALID
    assert result.raw == "<unrenderable int>"


# ── choice ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("manual", "manual"), ("AUTO", "auto"), (" bypass ", "bypass")],
)
def test_choice_coercer_normalizes_case(raw: str, expected: str) -> None:
    result = choice_coercer(choices=("manual", "auto", "bypass"))(raw)

    assert result.status is CoerceStatus.VALID
    assert result.value == expected


@pytest.mark.parametrize("raw", ["zh-Hans", "zh-hans", "ZH-HANS", " zh-Hans "])
def test_choice_coercer_returns_the_declared_spelling(raw: str) -> None:
    """The regression F8 named: ``zh-Hans`` must survive its own coercer."""
    result = choice_coercer(choices=SUPPORTED_LOCALES)(raw)

    assert result.status is CoerceStatus.VALID
    assert result.value == "zh-Hans"


def test_choice_coercer_rejects_choices_that_collide_under_case_folding() -> None:
    with pytest.raises(ValueError, match="collide"):
        choice_coercer(choices=("Auto", "auto"))


@pytest.mark.parametrize("raw", ["", "   ", None])
def test_choice_coercer_treats_blank_as_missing(raw: object) -> None:
    assert choice_coercer(choices=("manual",))(raw).status is CoerceStatus.MISSING


def test_choice_coercer_reports_unknown_values() -> None:
    result = choice_coercer(choices=("manual", "auto"))("nope")

    assert result.status is CoerceStatus.INVALID
    assert result.reason is CoerceReason.NOT_A_CHOICE
    assert result.choices == ("auto", "manual")
    assert result.raw == "nope"


@pytest.mark.parametrize("raw", ["manual", "AUTO", " bypass ", "", "nope"])
def test_choice_coercer_agrees_with_the_approval_mode_loader(raw: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHRYS_DEFAULT_APPROVAL_MODE", raw)
    coerce = choice_coercer(choices=("manual", "auto", "bypass"))

    assert resolve(coerce, raw, "manual") == settings_module._load_default_approval_mode()


@pytest.mark.parametrize("raw", ["standard", "VIM", " emacs ", "", "nope"])
def test_choice_coercer_agrees_with_the_editor_keymap_loader(raw: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHRYS_EDITOR_KEYMAP", raw)
    coerce = choice_coercer(choices=("standard", "emacs", "vim"))

    assert resolve(coerce, raw, "standard") == settings_module._load_editor_keymap()


# ── text ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(("raw", "expected"), [("Code", "Code"), (" Code ", "Code")])
def test_text_coercer_strips(raw: str, expected: str) -> None:
    assert text_coercer()(raw).value == expected


@pytest.mark.parametrize("raw", ["", "   ", None])
def test_text_coercer_treats_blank_as_missing(raw: object) -> None:
    assert text_coercer()(raw).status is CoerceStatus.MISSING


def test_text_coercer_can_preserve_surrounding_space() -> None:
    assert text_coercer(strip=False)(" chrys-legacy ").value == " chrys-legacy "


def test_text_coercer_rejects_non_strings() -> None:
    assert text_coercer()(7).status is CoerceStatus.INVALID


@pytest.mark.parametrize("raw", ["Code", "", "  ", "Research"])
def test_text_coercer_agrees_with_the_default_agent_loader(raw: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHRYS_DEFAULT_AGENT", raw)
    default = settings_module.DEFAULT_AGENT_PROFILE

    assert resolve(text_coercer(), raw, default) == settings_module._load_default_agent()


# ── the two bespoke parsers, expressed natively ───────────────────


def max_transient_retries_coercer() -> Coercer:
    return int_coercer(reject_negative=True, maximum=settings_module.MAX_TRANSIENT_RETRIES_LIMIT)


@pytest.mark.parametrize("raw", ["7", "0", "50", "51", "999", "-1", "garbage", "", "   "])
def test_int_coercer_agrees_with_the_transient_retries_parser(raw: str) -> None:
    expected_value, _warning = settings_module.parse_max_transient_retries_structured(raw)

    assert resolve(max_transient_retries_coercer(), raw, None) == expected_value


def test_transient_retries_clamp_is_reported_like_the_parser() -> None:
    _value, warning = settings_module.parse_max_transient_retries_structured("999")
    result = max_transient_retries_coercer()("999")

    assert warning is not None
    assert warning.variant == "clamped"
    assert result.status is CoerceStatus.CLAMPED
    assert result.limit == settings_module.MAX_TRANSIENT_RETRIES_LIMIT


def tool_result_ceiling_coercer() -> Coercer:
    return int_coercer(zero=0, reject_negative=True, minimum=settings_module.TOOL_RESULT_CEILING_FLOOR)


@pytest.mark.parametrize("raw", ["64000", "0", "-1", "1", "1999", "2000", "garbage", "", "   "])
def test_int_coercer_agrees_with_the_tool_result_ceiling_parser(raw: str) -> None:
    expected_value, _warning = settings_module.parse_tool_result_ceiling_tokens(raw)
    default = settings_module.DEFAULT_TOOL_RESULT_CEILING_TOKENS

    assert resolve(tool_result_ceiling_coercer(), raw, default) == expected_value
