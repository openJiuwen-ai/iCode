# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Field metadata: the contract the loader and the settings panel both read."""

from __future__ import annotations

from dataclasses import FrozenInstanceError, dataclass, field, fields, replace
from pathlib import Path
from typing import get_type_hints

import pytest

from chrys.foundation.config.coercion import bool_coercer, int_coercer, text_coercer
from chrys.foundation.config.context import EvalContext
from chrys.foundation.config.settings import Settings
from chrys.foundation.config.spec import (
    Apply,
    ChoiceProvider,
    InvalidPolicy,
    Kind,
    ProjectMerge,
    Risk,
    SettingOrigin,
    SettingSpec,
    Source,
    field_names_by_key,
    kind_accepts,
    spec,
    spec_of,
    specs_by_field,
    specs_by_key,
)
from chrys.foundation.i18n.messages import msg

_THEME_LABEL = msg("test.settings.theme", fallback="Theme")
_RETRIES_LABEL = msg("test.settings.retries", fallback="Transient retries")
_PLAIN_LABEL = msg("test.settings.plain", fallback="Plain")


@dataclass
class Sample:
    theme: str = field(
        default="chrys",
        metadata=spec(
            key="ui.theme",
            coerce=text_coercer(),
            apply=Apply.LIVE,
            group="ui",
            kind=Kind.ENUM,
            label=_THEME_LABEL,
            env="CHRYS_THEME",
            choices=ChoiceProvider.THEMES,
        ),
    )
    retries: int = field(
        default=7,
        metadata=spec(
            key="llm.retry.max_transient",
            coerce=int_coercer(reject_negative=True, maximum=50),
            apply=Apply.RELOAD,
            group="llm",
            kind=Kind.INT,
            label=_RETRIES_LABEL,
            project_merge=ProjectMerge.TIGHTEN_ONLY,
            semantic_value=lambda settings, ctx: settings.retries or ctx.frontend_default_max_transient_retries,
        ),
    )
    untracked: str = "no metadata"


def test_specs_are_reachable_from_the_dataclass() -> None:
    by_field = specs_by_field(Sample)

    assert set(by_field) == {"theme", "retries"}
    assert by_field["theme"].env == "CHRYS_THEME"
    assert specs_by_key(Sample)["ui.theme"] is by_field["theme"]
    assert field_names_by_key(Sample) == {"ui.theme": "theme", "llm.retry.max_transient": "retries"}


def test_a_field_without_metadata_is_simply_absent() -> None:
    assert "untracked" not in specs_by_field(Sample)
    assert spec_of({}) is None
    assert spec_of({"chrys_setting": "not a spec"}) is None


def test_defaults_stay_conservative() -> None:
    found = specs_by_field(Sample)["theme"]

    assert found.risk.name == "SAFE"
    assert found.project_merge is ProjectMerge.DENY
    assert found.persist is True
    assert found.invalid_policy is InvalidPolicy.FALL_THROUGH


def test_semantic_value_receives_the_settings_and_the_context() -> None:
    """The comparator needs the frontend policy, which the bare field cannot carry."""
    found = specs_by_field(Sample)["retries"]
    assert found.semantic_value is not None

    unset = Sample(retries=0)

    assert found.semantic_value(unset, EvalContext(frontend_default_max_transient_retries=15)) == 15
    assert found.semantic_value(unset, EvalContext(frontend_default_max_transient_retries=7)) == 7
    assert found.semantic_value(Sample(retries=10), EvalContext(frontend_default_max_transient_retries=15)) == 10


def test_enum_without_choices_is_rejected_at_construction() -> None:
    with pytest.raises(ValueError, match="requires choices"):
        SettingSpec(
            key="ui.broken",
            coerce=text_coercer(),
            apply=Apply.LIVE,
            group="ui",
            kind=Kind.ENUM,
            label=_PLAIN_LABEL,
        )


def test_tighten_only_without_a_comparator_is_rejected_at_construction() -> None:
    with pytest.raises(ValueError, match="requires a semantic_value"):
        SettingSpec(
            key="llm.broken",
            coerce=int_coercer(),
            apply=Apply.RELOAD,
            group="llm",
            kind=Kind.INT,
            label=_PLAIN_LABEL,
            project_merge=ProjectMerge.TIGHTEN_ONLY,
        )


def test_a_dangerous_field_may_not_fall_through_on_invalid() -> None:
    """Falling through re-enables whatever a lower layer persisted."""
    with pytest.raises(ValueError, match=r"requires InvalidPolicy\.SAFE_DEFAULT"):
        SettingSpec(
            key="log.broken",
            coerce=bool_coercer(),
            apply=Apply.RESTART,
            group="log",
            kind=Kind.BOOL,
            label=_PLAIN_LABEL,
            risk=Risk.DANGEROUS,
        )


def test_every_dangerous_shipped_field_is_sealed_and_defaults_safe() -> None:
    """The policy is only worth declaring if the default it lands on is safe."""
    dangerous = {key: found for key, found in specs_by_key(Settings).items() if found.risk is Risk.DANGEROUS}
    assert dangerous, "the audit below is vacuous if nothing is marked dangerous"

    assert all(found.invalid_policy is InvalidPolicy.SAFE_DEFAULT for found in dangerous.values())

    fail_closed = Settings()
    assert fail_closed.default_approval_mode == "manual"
    assert fail_closed.otel_sensitive_data is False
    assert fail_closed.raw_http_capture is False
    assert fail_closed.mutation_trace_fsatrace_path == ""


def test_a_persisted_field_without_a_label_is_rejected_at_construction() -> None:
    """The panel renders every persisted field, so a missing label is a hole in it."""
    with pytest.raises(ValueError, match="persisted settings require a label"):
        SettingSpec(
            key="ui.unlabelled",
            coerce=text_coercer(),
            apply=Apply.LIVE,
            group="ui",
            kind=Kind.TEXT,
        )


def test_a_runtime_only_field_may_go_without_a_label() -> None:
    """``persist=False`` is never rendered, so there is nothing to label."""
    found = SettingSpec(
        key="model.profile.override",
        coerce=text_coercer(),
        apply=Apply.RELOAD,
        group="model",
        kind=Kind.TEXT,
        persist=False,
    )

    assert found.label is None


def test_directional_merges_need_no_comparator() -> None:
    found = SettingSpec(
        key="workspace.change_notice.enabled",
        coerce=bool_coercer(),
        apply=Apply.RELOAD,
        group="workspace",
        kind=Kind.BOOL,
        label=_PLAIN_LABEL,
        project_merge=ProjectMerge.ENABLE_ONLY,
    )

    assert found.semantic_value is None


def test_origin_carries_the_file_a_value_came_from() -> None:
    """A layer alone cannot answer it: one session can have several project roots."""
    first = SettingOrigin(layer=Source.PROJECT, path=Path("/repo/a/.chrys/settings.yaml"))
    second = SettingOrigin(layer=Source.PROJECT, path=Path("/repo/b/.chrys/settings.yaml"))

    assert first != second
    assert SettingOrigin(layer=Source.DEFAULT).path is None


def test_a_file_layer_origin_must_name_its_file() -> None:
    """Otherwise it silently degrades into the bare layer this type replaces."""
    for layer in (Source.USER, Source.PROJECT):
        with pytest.raises(ValueError, match="must name the file"):
            SettingOrigin(layer=layer)


def test_a_layer_without_a_file_cannot_carry_a_path() -> None:
    with pytest.raises(ValueError, match="no backing file"):
        SettingOrigin(layer=Source.ENV, path=Path("/repo/.env"))


def test_source_ordering_is_low_to_high_precedence() -> None:
    """Every member, in order — a partial list is what let the pin regress."""
    order = [
        Source.DEFAULT,
        Source.USER,
        Source.PROJECT,
        Source.ENV,
        Source.CLI,
        Source.PROCESS_RUNTIME,
        Source.SESSION,
        Source.RUNTIME,
    ]

    assert order == list(Source)
    assert [member.value for member in order] == sorted(member.value for member in order)


def test_a_per_session_pin_outranks_the_process_wide_pointer() -> None:
    """One ACP session's model pin must not be overridden by another's activation."""
    assert Source.SESSION.value > Source.PROCESS_RUNTIME.value
    assert Source.PROCESS_RUNTIME.value > Source.CLI.value


# ── Kind as a runtime type ────────────────────────────────────────────


_KIND_ANNOTATIONS = {
    Kind.BOOL: "bool",
    Kind.INT: "int",
    Kind.OPTIONAL_INT: "int | None",
    Kind.FLOAT: "float",
    Kind.TEXT: "str",
    Kind.ENUM: "str",
    Kind.PATH: "str",
}


def test_settings_cannot_be_written_in_place() -> None:
    """The guard behind ``overlay()`` being the only write path.

    A value, the layer it came from, and whether that layer was sealed are one
    decision held in three places. An in-place write moves one of the three,
    which is how the TUI used to switch approval mode and leave the key sealed
    at a default it no longer held. Frozen makes that unwritable rather than
    merely discouraged, so a new write site fails at the assignment instead of
    surviving until someone reads provenance.
    """
    settings = Settings()

    with pytest.raises(FrozenInstanceError):
        settings.theme = "chrys-legacy"  # type: ignore[misc]

    assert replace(settings, theme="chrys-legacy").theme == "chrys-legacy"


def test_every_field_of_settings_carries_a_spec() -> None:
    """Without this, the agreement test below is vacuous for a forgotten field.

    ``specs_by_field`` skips fields with no metadata, so one added without a
    ``spec(...)`` is invisible to every check that iterates it — and invisible
    to the loader and the panel too, while a pin of the same name still passes
    straight through to ``Settings``.
    """
    undeclared = sorted({entry.name for entry in fields(Settings)} - set(specs_by_field(Settings)))

    assert undeclared == []


def test_every_shipped_field_declares_the_kind_its_annotation_says() -> None:
    """``Kind`` is metadata for the panel *and* the loader's pin type check.

    The pin check trusts ``Kind`` rather than re-deriving the annotation, so a
    field whose two disagree would let a wrongly-typed pin into ``Settings``.
    """
    hints = get_type_hints(Settings)
    mismatched = {
        name: (str(hints[name]), found.kind.name)
        for name, found in specs_by_field(Settings).items()
        if str(hints[name]).removeprefix("<class '").removesuffix("'>") != _KIND_ANNOTATIONS[found.kind]
    }

    assert mismatched == {}


@pytest.mark.parametrize(
    ("kind", "value", "accepted"),
    [
        (Kind.BOOL, True, True),
        (Kind.BOOL, 1, False),
        (Kind.BOOL, None, False),
        # ``bool`` is an ``int`` subclass, so this is the one that silently
        # passes without an explicit guard.
        (Kind.INT, True, False),
        (Kind.INT, 5, True),
        (Kind.INT, None, False),
        (Kind.OPTIONAL_INT, None, True),
        (Kind.OPTIONAL_INT, 5, True),
        (Kind.OPTIONAL_INT, "5", False),
        (Kind.FLOAT, 1.5, True),
        (Kind.FLOAT, 2, True),
        (Kind.TEXT, "", True),
        (Kind.TEXT, None, False),
        (Kind.PATH, "/tmp/x", True),
        (Kind.ENUM, "dark", True),
    ],
)
def test_kind_accepts_matches_the_in_memory_type(kind: Kind, value: object, accepted: bool) -> None:
    assert kind_accepts(kind, value) is accepted
