# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Hygiene rule registries, the sharded source sweeps, and the meta-guards over both."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.architecture import (
    test_hygiene_exchange_walker_shapes,
    test_hygiene_exchange_walkers,
    test_hygiene_i18n_messages,
    test_hygiene_lazy_imports,
    test_hygiene_llm_client_owners,
    test_hygiene_pillow_formats,
    test_hygiene_reminder_sources,
    test_hygiene_session_surface,
    test_hygiene_source_asserts,
    test_hygiene_subprocess_stdin,
    test_hygiene_test_source_rules,
    test_hygiene_tui_bindings,
    test_hygiene_tui_locale_controller,
    test_hygiene_tui_prose,
    test_hygiene_user_turn_events,
)
from tests.architecture._hygiene_core import (
    _ALLOWLIST_PIN_REGISTRY,
    _SWEEP_SHARDS,
    _architecture_definitions,
    _definition_sites,
    _meta_guard_problem,
    _rule_module_paths,
    _src_sources,
    _test_sources,
    _tree,
)
from tests.architecture.test_hygiene_exchange_walkers import (
    _assert_no_hand_rolled_exchange_walkers,
    _assert_result_only_classifier_imports_are_allowlisted,
)
from tests.architecture.test_hygiene_i18n_messages import _assert_i18n_message_construction_is_canonical
from tests.architecture.test_hygiene_lazy_imports import _assert_lazy_dependency_imports_are_function_scoped
from tests.architecture.test_hygiene_llm_client_owners import _assert_llm_clients_have_reviewed_owners
from tests.architecture.test_hygiene_pillow_formats import _assert_pillow_decodes_only_named_formats
from tests.architecture.test_hygiene_reminder_sources import _assert_reminder_source_members_are_reviewed
from tests.architecture.test_hygiene_session_surface import _assert_launches_state_their_surface
from tests.architecture.test_hygiene_source_asserts import _assert_no_source_asserts
from tests.architecture.test_hygiene_subprocess_stdin import _assert_subprocess_stdin_is_explicit
from tests.architecture.test_hygiene_test_source_rules import (
    _assert_integration_marker_directory_disjoint,
    _assert_no_direct_agent_engine_start,
    _assert_no_ignored_wait_until_results,
    _assert_no_scroll_relative,
    _assert_no_unapproved_local_polling_helpers,
    _assert_quarantine_marker_metadata,
    _assert_tool_loop_layer_uses_are_invariant_checked,
    _assert_trajectory_prefix_checks_use_physical_slots,
)
from tests.architecture.test_hygiene_tui_bindings import _assert_tui_binding_display_construction_is_canonical
from tests.architecture.test_hygiene_tui_locale_controller import (
    _assert_tui_locale_controller_propagation_is_explicit,
)
from tests.architecture.test_hygiene_tui_prose import (
    _assert_tui_border_titles_are_localized,
    _assert_tui_content_from_text_disables_markup,
    _assert_tui_content_markup_prose_is_localized,
    _assert_tui_notify_prose_is_localized,
    _assert_tui_placeholder_tooltip_prose_is_localized,
    _assert_tui_widget_label_prose_is_localized,
)
from tests.architecture.test_hygiene_user_turn_events import _assert_user_turn_events_are_built_only_by_the_input_flow
from tests.support.ci import CI_LINUX_ONLY
from tests.support.paths import REPO_ROOT, SRC_ROOT, TESTS_ROOT

# Platform-independent source analysis: the Linux CI job covers it.
pytestmark = CI_LINUX_ONLY

# Importing every family module is load-bearing twice over: the registries below
# need the rule callables, and each module's ``@_pins_allowlist`` decorators only
# populate _ALLOWLIST_PIN_REGISTRY when that module is imported. Running this
# file alone would otherwise see an empty registry and fail the pin meta-guard
# spuriously. The shapes module carries no rule of its own but stays in the
# tuple so _RULE_MODULES covers every test_hygiene_*.py on disk.
_RULE_MODULES = (
    test_hygiene_exchange_walker_shapes,
    test_hygiene_exchange_walkers,
    test_hygiene_i18n_messages,
    test_hygiene_lazy_imports,
    test_hygiene_llm_client_owners,
    test_hygiene_pillow_formats,
    test_hygiene_reminder_sources,
    test_hygiene_session_surface,
    test_hygiene_source_asserts,
    test_hygiene_subprocess_stdin,
    test_hygiene_test_source_rules,
    test_hygiene_tui_bindings,
    test_hygiene_tui_locale_controller,
    test_hygiene_tui_prose,
    test_hygiene_user_turn_events,
)


_HYGIENE_RULES = (
    _assert_no_scroll_relative,
    _assert_no_unapproved_local_polling_helpers,
    _assert_no_ignored_wait_until_results,
    _assert_integration_marker_directory_disjoint,
    _assert_no_direct_agent_engine_start,
    _assert_quarantine_marker_metadata,
    _assert_tool_loop_layer_uses_are_invariant_checked,
    _assert_trajectory_prefix_checks_use_physical_slots,
)

_SRC_HYGIENE_RULES = (
    _assert_no_source_asserts,
    _assert_subprocess_stdin_is_explicit,
    _assert_lazy_dependency_imports_are_function_scoped,
    _assert_pillow_decodes_only_named_formats,
    _assert_no_hand_rolled_exchange_walkers,
    _assert_result_only_classifier_imports_are_allowlisted,
    _assert_tui_binding_display_construction_is_canonical,
    _assert_tui_notify_prose_is_localized,
    _assert_tui_border_titles_are_localized,
    _assert_tui_widget_label_prose_is_localized,
    _assert_tui_placeholder_tooltip_prose_is_localized,
    _assert_tui_content_markup_prose_is_localized,
    _assert_tui_content_from_text_disables_markup,
    _assert_i18n_message_construction_is_canonical,
    _assert_llm_clients_have_reviewed_owners,
    _assert_launches_state_their_surface,
    _assert_reminder_source_members_are_reviewed,
    _assert_user_turn_events_are_built_only_by_the_input_flow,
)

_GLOBAL_SRC_HYGIENE_RULES = (_assert_tui_locale_controller_propagation_is_explicit,)


@pytest.mark.parametrize("shard", range(_SWEEP_SHARDS))
def test_hygiene_rules_hold_across_test_sources(shard: int) -> None:
    """Run every hygiene rule against ONE shared read+parse per test file.

    Deliberately NOT one test per rule: that shape re-read and re-parsed every
    test file PER RULE (9-15s each on contended CI macOS runners, measured when
    the sweep scanned 396 Python files, 324 of them test modules; it scans 738
    now, nearly twice as many), and xdist may
    schedule the tests onto different workers, so caching alone cannot share
    the work. Within a shard each file is read and parsed once and every rule
    runs against that single cached parse — rules are all file-local (no
    cross-file state), which is also what makes sharding sound: each file is
    checked exactly once, in exactly one shard.

    The tree is released after each file: holding the whole tree's ASTs
    concurrently spiked worker memory by hundreds of MB, enough to crash an
    xdist worker on memory-constrained macOS CI runners. And the sweep is sharded because the
    monolithic version measured 53-55s on contended CI draws — 90% of the
    global 60s per-test timeout, and the #1 tail pole on every platform.
    Failures are aggregated so one run still reports every rule's violations.
    """
    failures: list[str] = []
    for path, source in _test_sources(shard).items():
        single_source = {path: source}
        for rule in _HYGIENE_RULES:
            try:
                rule(single_source)
            except AssertionError as exc:
                failures.append(str(exc))
        _tree.cache_clear()
    assert not failures, "\n\n".join(failures)


@pytest.mark.parametrize("shard", range(_SWEEP_SHARDS))
def test_exchange_walker_guard_holds_across_src_sources(shard: int) -> None:
    """Run the src-scoped rules against ONE shared read+parse per src file.

    Same sharded single-parse shape as the test-source sweep above, for the
    same CI wall-clock and worker-memory reasons.
    """
    failures: list[str] = []
    for path, source in _src_sources(shard).items():
        single_source = {path: source}
        for rule in _SRC_HYGIENE_RULES:
            try:
                rule(single_source)
            except AssertionError as exc:
                failures.append(str(exc))
        _tree.cache_clear()
    assert not failures, "\n\n".join(failures)


def test_sweep_shard_partitions_are_complete_disjoint_and_non_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """The sharded sweeps must cover every enumerated path exactly once."""
    # Warm the definition-site cache BEFORE read_text is stubbed out: every
    # _meta_guard_problem below resolves a name through it, and a stubbed read
    # would index an empty parse.
    _definition_sites()
    monkeypatch.setattr(Path, "read_text", lambda _path, *, encoding: "")
    problems: list[str] = []
    for collector, root in ((_test_sources, TESTS_ROOT), (_src_sources, SRC_ROOT)):
        enumerated_keys = {path.relative_to(REPO_ROOT) for path in root.rglob("*.py")}
        all_keys = set(collector())
        collector_missing = enumerated_keys - all_keys
        collector_unexpected = all_keys - enumerated_keys
        if collector_missing or collector_unexpected:
            details = []
            if collector_missing:
                details.append(
                    f"omitted from shard=None: {', '.join(path.as_posix() for path in sorted(collector_missing))}"
                )
            if collector_unexpected:
                details.append(
                    "not present in the filesystem enumeration: "
                    f"{', '.join(path.as_posix() for path in sorted(collector_unexpected))}"
                )
            problems.append(
                _meta_guard_problem(
                    collector.__name__,
                    f"{collector.__name__}(shard=None) differs from the complete filesystem key-set "
                    f"({'; '.join(details)})",
                    "enumerate every *.py file under the collector root without an extra skip or early continue",
                )
            )
        shard_keys = [set(collector(shard)) for shard in range(_SWEEP_SHARDS)]
        union = set().union(*shard_keys)
        missing = all_keys - union
        unexpected = union - all_keys
        if missing or unexpected:
            details = []
            if missing:
                details.append(f"missing from all shards: {', '.join(path.as_posix() for path in sorted(missing))}")
            if unexpected:
                details.append(
                    f"absent from the unfiltered collector: {', '.join(path.as_posix() for path in sorted(unexpected))}"
                )
            problems.append(
                _meta_guard_problem(
                    collector.__name__,
                    f"{collector.__name__} shard union differs from its shard=None key-set ({'; '.join(details)})",
                    "make _shard_of return only values in range(_SWEEP_SHARDS) and keep the filtered and "
                    "unfiltered collector paths identical",
                )
            )
        for left in range(_SWEEP_SHARDS):
            for right in range(left + 1, _SWEEP_SHARDS):
                overlap = shard_keys[left] & shard_keys[right]
                if overlap:
                    problems.append(
                        _meta_guard_problem(
                            collector.__name__,
                            f"{collector.__name__} shards {left} and {right} overlap at "
                            f"{', '.join(path.as_posix() for path in sorted(overlap))}",
                            "assign every relative path to exactly one shard in _shard_of",
                        )
                    )
        for shard, keys in enumerate(shard_keys):
            if not keys:
                problems.append(
                    _meta_guard_problem(
                        collector.__name__,
                        f"{collector.__name__} shard {shard} is empty",
                        "choose _SWEEP_SHARDS and _shard_of so every parametrized shard receives at least one file",
                    )
                )

    assert problems == [], "\n".join(problems)


def test_global_src_hygiene_rules_hold_across_all_sources() -> None:
    """Run cross-file rules once over the complete source graph."""
    assert _GLOBAL_SRC_HYGIENE_RULES, "global source hygiene registry must not be empty"
    sources = _src_sources()
    failures: list[str] = []
    for rule in _GLOBAL_SRC_HYGIENE_RULES:
        try:
            rule(sources)
        except AssertionError as exc:
            failures.append(str(exc))
    _tree.cache_clear()
    assert not failures, "\n\n".join(failures)


def _unscanned_rule_defining_modules() -> list[Path]:
    """Architecture modules that define an _assert_* rule _RULE_MODULES misses.

    The vars() scan below only sees the modules the tuple names, so this disk
    scan is what stops a whole new rule module from opting out of it. Only the
    _assert_* rule convention is checked directory-wide: *_ALLOWLIST is a
    generic name that unrelated architecture guards (test_layering.py) use with
    their own liveness handling, and the glob pin above already covers every
    test_hygiene_*.py family module's allowlists.
    """
    scanned = {Path(module.__file__).resolve().relative_to(REPO_ROOT) for module in _RULE_MODULES}
    return sorted(
        module_path
        for module_path, definitions in _architecture_definitions().items()
        if module_path not in scanned and any(name.startswith("_assert_") for name in definitions)
    )


def test_every_hygiene_rule_is_registered() -> None:
    """A rule that exists but is missing from the registries is silently unenforced.

    The rules live in sibling family modules now, so the scan walks the explicit
    _RULE_MODULES tuple instead of this module's globals — a rule defined in a
    family module but never imported here would otherwise be invisible.
    """
    registries = (_HYGIENE_RULES, _SRC_HYGIENE_RULES, _GLOBAL_SRC_HYGIENE_RULES)
    registered = [rule for registry in registries for rule in registry]
    assert len(registered) == len(set(registered)), "hygiene rule registries must be pairwise disjoint"
    assert _assert_tui_locale_controller_propagation_is_explicit in _GLOBAL_SRC_HYGIENE_RULES, (
        "locale-controller propagation needs the complete source graph and must stay in the global registry"
    )
    problems: list[str] = []
    scanned_paths = {Path(module.__file__).resolve() for module in _RULE_MODULES}
    unscanned = sorted(path for path in _rule_module_paths() if path not in scanned_paths)
    if unscanned:
        problems.append(
            _meta_guard_problem(
                "_RULE_MODULES",
                "hygiene family modules on disk are absent from _RULE_MODULES: "
                + ", ".join(path.relative_to(REPO_ROOT).as_posix() for path in unscanned),
                "import the family module here and add it to _RULE_MODULES so its rules are scanned",
            )
        )
    for module_path in _unscanned_rule_defining_modules():
        problems.append(
            _meta_guard_problem(
                "_RULE_MODULES",
                f"{module_path.as_posix()} defines an _assert_* rule but is not in _RULE_MODULES",
                "move the rule into a scanned test_hygiene_*.py family module, or add its module to _RULE_MODULES",
            )
        )

    declared = {
        obj
        for module in _RULE_MODULES
        for name, obj in vars(module).items()
        if name.startswith("_assert_") and callable(obj)
    }
    for rule in sorted(declared - set(registered), key=lambda rule: rule.__name__):
        problems.append(
            _meta_guard_problem(
                rule.__name__,
                f"{rule.__name__} is defined in a scanned rule module but registered in no hygiene registry",
                "add the rule to _HYGIENE_RULES, _SRC_HYGIENE_RULES or _GLOBAL_SRC_HYGIENE_RULES",
            )
        )
    for rule in sorted(set(registered) - declared, key=lambda rule: rule.__name__):
        problems.append(
            _meta_guard_problem(
                rule.__name__,
                f"{rule.__name__} is registered but is defined in no scanned rule module",
                "define the rule in a test_hygiene_*.py family module listed in _RULE_MODULES",
            )
        )

    assert problems == [], "\n".join(problems)


def test_every_non_empty_allowlist_has_a_liveness_pin() -> None:
    # Empty allowlists have no entry that can go stale, so they need no pin.
    # The allowlists moved next to the rules they justify, so the scan unions
    # the family modules' globals instead of this module's.
    allowlists = {
        name: value
        for module in _RULE_MODULES
        for name, value in vars(module).items()
        if name.startswith("_") and name.endswith("_ALLOWLIST")
    }
    expected = {name for name, value in allowlists.items() if value}
    registered = set(_ALLOWLIST_PIN_REGISTRY)
    problems = [
        _meta_guard_problem(
            name,
            f"non-empty {name} has no registered *_allowlist_entries_are_live* test",
            f"decorate a focused liveness test with @_pins_allowlist({name!r}) and make it resolve every entry",
        )
        for name in sorted(expected - registered)
    ]
    for name in sorted(expected & registered):
        pins = _ALLOWLIST_PIN_REGISTRY[name]
        if not any(pin.__name__.startswith("test_") and "_allowlist_entries_are_live" in pin.__name__ for pin in pins):
            problems.append(
                _meta_guard_problem(
                    name,
                    f"{name} is registered only to tests that do not match *_allowlist_entries_are_live*",
                    "rename the registered test to describe its liveness contract or register the intended liveness test",
                )
            )
    for name in sorted(registered - set(allowlists)):
        pin_names = ", ".join(pin.__name__ for pin in _ALLOWLIST_PIN_REGISTRY[name])
        problems.append(
            _meta_guard_problem(
                name,
                f"{name} has liveness-pin registration ({pin_names}) but no matching global allowlist",
                "remove the stale registration or restore the allowlist global whose entries the test resolves",
            )
        )

    assert problems == [], "\n".join(problems)
