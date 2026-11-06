# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Shared test fixtures for Chrys."""

from __future__ import annotations

import asyncio
import atexit
import contextlib
import dataclasses
import ipaddress
import itertools
import os
import re
import shutil
import socket
import ssl
import subprocess
import tempfile
import warnings
from collections.abc import AsyncIterator, Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from chrys.foundation import platform as platform_mod
from chrys.foundation.patches import mdurl_cache as _mdurl_cache
from chrys.foundation.patches import textual_block_border as _textual_block_border
from chrys.foundation.patches import textual_box_model_children as _textual_box_model_children
from chrys.foundation.patches import textual_dispatch_cache as _textual_dispatch_cache
from chrys.foundation.patches import textual_lru_acyclic as _textual_lru_acyclic
from chrys.foundation.patches import textual_node_diet as _textual_node_diet
from chrys.foundation.patches import textual_one_shot_timer as _textual_one_shot_timer
from chrys.foundation.patches import textual_pruned_tabs as _textual_pruned_tabs
from chrys.foundation.patches import textual_reflow_reuse as _textual_reflow_reuse
from chrys.foundation.patches import textual_removed_node_caches as _textual_removed_node_caches
from chrys.foundation.patches import textual_removed_screen_callbacks as _textual_removed_screen_callbacks
from chrys.foundation.patches import textual_selection_extract as _textual_selection_extract
from chrys.foundation.patches import textual_strip_cycles as _textual_strip_cycles
from chrys.foundation.patches import textual_tab_selection as _textual_tab_selection
from chrys.foundation.patches import textual_timer_skip as _textual_timer_skip
from chrys.foundation.patches import textual_win_sleep as _textual_win_sleep
from chrys.foundation.patches import watchdog_fsevents as _watchdog_fsevents
from chrys.foundation.patches import watchdog_windows as _watchdog_windows
from chrys.foundation.platform import PlatformInfo
from chrys.foundation.platform import get_platform as _cached_get_platform
from tests.support.env_guard import (
    report_environment_leak,
    restore_environment_and_describe_changes,
    snapshot_environment,
)
from tests.support.network_guard import (
    begin_test_network_scope,
    end_test_network_scope,
    guarded_create_connection,
    guarded_getaddrinfo,
    guarded_hostname_resolution,
    guarded_sendmsg,
    guarded_sendto,
    guarded_socket_connect,
)
from tests.support.quarantine import apply_quarantine_markers, enforce_quarantine_problem
from tests.support.stall_diagnostics import StallSampler, start_stall_sampler_from_environment
from tests.support.waiting import ENGINE_TURN_TIMEOUT

if TYPE_CHECKING:
    from tests.support.notifications import RecordingNotificationDriver

pytest_plugins = ("tests.support.engines", "tests.support.llm_http_clients", "pytester")

warnings.filterwarnings("ignore", message=r"\[SKILLS\].*")
warnings.filterwarnings("ignore", message=r"\[HARNESS\].*")

# Apply Chrys' Textual tab-selection runtime patch so the suite exercises the
# same source-offset-aware selection/copy path as the running app. The app runs
# ``patches.apply_all()`` at startup, which the test process never reaches;
# without this, markdown copy that reads ``_FormattedLine.source_offsets`` raises
# AttributeError on a fresh (unpatched) Textual install such as CI, while passing
# locally only because the dev venv was already site-package-patched by a prior
# app run. The patch is idempotent and version-guarded (no-op off the pinned
# Textual), so re-applying over an already-patched local venv is safe.
_textual_tab_selection.apply_runtime_patch()

# Bare App.run_test() hosts need the same Windows timer ownership as production
# startup. Otherwise Runner.close() can hang joining a cancelled native wait.
_textual_win_sleep.apply_runtime_patch()

# Workspace switches exercise the same native watcher ownership as startup.
_watchdog_windows.apply_runtime_patch()

# Every mounted MainScreen starts a git HEAD watcher; unpatched, each one on
# macOS keeps its whole App alive for the rest of the worker.
_watchdog_fsevents.apply_runtime_patch()

# Markdown blocks are built in executor threads, as in the app; unpatched, two
# of them normalizing their first links together can index a half-built table.
_mdurl_cache.apply_runtime_patch()

# Exercise production dispatch deterministically, rather than only after a
# startup or patch test happens to run on this worker.
_textual_dispatch_cache.apply_runtime_patch()

# Copy paths call ``screen.get_selected_text()`` exactly as the app does; unpatched,
# a selection starting on a trailing blank row raises instead of copying.
_textual_selection_extract.apply_runtime_patch()

# Tab bars are removed with their screens exactly as in the app; unpatched, a bar
# removed before its tabs mount raises "No Tab with id" and takes the App down.
_textual_pruned_tabs.apply_runtime_patch()

# Loading dialogs open and close over the main screen exactly as in the app; unpatched, work
# a widget under one scheduled after a refresh can be dropped with the dialog.
_textual_removed_screen_callbacks.apply_runtime_patch()

# Widgets are removed exactly as in the app; unpatched, a parent that looked a removed child up
# keeps its whole subtree alive, and GC freeze captures it.
_textual_removed_node_caches.apply_runtime_patch()

# Tab bars move their underline from a one-shot timer exactly as in the app; unpatched, a
# collection or descheduling right after that timer starts means it never fires.
_textual_one_shot_timer.apply_runtime_patch()

# Every rendered line reads its colors through the same cache as in the app; unpatched, that
# cache keeps up to 1024 removed widgets' rendered lines alive, and a worker's gen2 collections
# grow into multi-second pauses.
_textual_block_border.apply_runtime_patch()

# Screens refresh from a repeating timer exactly as in the app; unpatched, every late frame
# skips one tick too many and the refresh rate halves under load.
_textual_timer_skip.apply_runtime_patch()

# Screens lay out through the reusing reflow exactly as in the app; it must be installed before
# the first widget exists.
_textual_reflow_reuse.apply_runtime_patch()

# Widgets sized to their children measure them anew once one shows or hides, exactly as in the app;
# unpatched, a layout another widget asked for measures a folded entry at its open height. It wraps
# the NodeList.updated the reusing reflow installs.
_textual_box_model_children.apply_runtime_patch()

# Screens paint strips exactly as in the app; unpatched, every line painted at its own width is
# a reference cycle that only the cyclic collector frees.
_textual_strip_cycles.apply_runtime_patch()

# Widgets cache box models, queries and rendered lines exactly as in the app; it must be installed
# before the first cache exists.
_textual_lru_acyclic.apply_runtime_patch()

# Widgets keep their unchanged defaults on the class and build pump containers lazily exactly as
# in the app; it must be installed before the first message pump exists.
_textual_node_diet.apply_runtime_patch()


@pytest.fixture(autouse=True)
def notification_driver(monkeypatch: pytest.MonkeyPatch) -> RecordingNotificationDriver:
    """Keep notification policy live, but never deliver to the developer's desktop.

    Native-driver tests still exercise their own explicitly mocked OS commands.
    Acknowledging sound delivery also prevents NotificationService's bell fallback.
    """
    from chrys.app.tui.notifications import service
    from tests.support.notifications import RecordingNotificationDriver

    driver = RecordingNotificationDriver()
    monkeypatch.setattr(service, "default_notification_driver", lambda: driver)
    return driver


@pytest.fixture(autouse=True)
def _isolated_textual_dispatch_plans() -> Iterator[None]:
    """Keep cached handler references inside the test that acquired them."""
    from textual.message_pump import MessagePump

    initial_cache = getattr(MessagePump._get_dispatch_methods, "_plan_cache", None)
    if initial_cache is not None:
        initial_cache.clear()
    try:
        yield
    finally:
        # A test may have installed a different dispatcher in the meantime.
        current_cache = getattr(MessagePump._get_dispatch_methods, "_plan_cache", None)
        if current_cache is not None:
            current_cache.clear()
        if initial_cache is not None:
            initial_cache.clear()


@pytest.fixture(autouse=True)
def _settled_pilot_waits(monkeypatch: pytest.MonkeyPatch) -> None:
    """A Pilot wait also drains what its handlers posted, and a timeout names the widgets holding it.

    See ``tests/support/pilot_barrier.py``: Textual's own wait covers only the messages
    queued when it starts, and a starved runner's idle probe does not cover the rest.
    """
    from tests.support.pilot_barrier import install_settled_wait_for_screen

    install_settled_wait_for_screen(monkeypatch)


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Apply validated, expiry-enforced quarantines during collection."""
    apply_quarantine_markers(items)


def pytest_runtest_setup(item: pytest.Item) -> None:
    """Fail tests whose quarantine metadata is invalid or expired."""
    enforce_quarantine_problem(item)


@pytest.hookimpl(wrapper=True)
def pytest_runtest_protocol(item: pytest.Item, nextitem: pytest.Item | None):
    """Bracket setup, call, and teardown to detect residual env mutations."""
    before = snapshot_environment()
    network_token = begin_test_network_scope(integration=item.get_closest_marker("integration") is not None)
    try:
        result = yield
    finally:
        end_test_network_scope(network_token)
        changes = restore_environment_and_describe_changes(before)
        if changes is not None:
            report_environment_leak(item, changes)
    return result


@pytest.hookimpl(wrapper=True)
def pytest_runtest_call(item: pytest.Item):
    """Give the stall sampler the call phase, so a long or failed one is reported with where its time went."""
    sampler = item.config.stash.get(_STALL_SAMPLER, None)
    if sampler is None:
        return (yield)
    sampler.begin_phase(item.nodeid)
    try:
        result = yield
    except pytest.skip.Exception, pytest.xfail.Exception:
        sampler.end_phase(failed=False)
        raise
    except BaseException:
        sampler.end_phase(failed=True)
        raise
    sampler.end_phase(failed=False)
    return result


_NETWORK_GUARD_PATCHER: pytest.StashKey[pytest.MonkeyPatch] = pytest.StashKey()
_STRIPPED_CHRYS_ENV: pytest.StashKey[dict[str, str]] = pytest.StashKey()
_STALL_SAMPLER: pytest.StashKey[StallSampler] = pytest.StashKey()


def pytest_configure(config: pytest.Config) -> None:
    """Arm the config-dir redirect and the loopback-only egress guard for the whole process.

    The egress guard is installed at configure time rather than as a session fixture so that
    module-level code imported during collection is guarded too. This covers
    TCP connects, unconnected UDP sends, and name resolution; the documented
    gaps (Windows proactor asyncio, child processes, reverse DNS) live in
    tests/support/network_guard.py. Windows asyncio emulates socketpair with
    loopback TCP; loopback remains allowed by construction so the guard does
    not break that implementation.

    Also runs the suite in a shell that never exported Chrys's own settings. A
    developer who exports ``CHRYS_MODEL_PROFILE`` is configuring their own
    Chrys, not their test run — but the ENV layer cannot tell the difference,
    and neither can the assertions that prove a code path did *not* mirror a
    value into the environment: they read ``"CHRYS_X" not in os.environ`` and
    would fail on that developer's machine while passing in CI. Setting aliases
    come from metadata; internal process carriers are listed explicitly below.
    Both are stripped here rather than in a session fixture because the
    leak guard above snapshots the environment before the first fixture runs
    and would restore whatever a fixture removed.
    """
    _arm_config_dir_redirect()

    from chrys.foundation.config.process_env import canonical_env_name
    from chrys.foundation.config.settings import Settings
    from chrys.foundation.config.spec import specs_by_field

    names = {entry.env for entry in specs_by_field(Settings).values() if entry.env is not None}
    # Internal carriers are maintained here alongside their production owners:
    # hooks/runner.py, acp_client/spawn.py, and platform/runtime_paths.py.
    names.update(
        {
            "CHRYS_HOOK_ID",
            "CHRYS_HOOK_EVENT",
            "CHRYS_HOOK_PAYLOAD_FILE",
            "CHRYS_HOOK_RESULT",
            "CHRYS_ACP_SUBAGENT_DEPTH",
            "CHRYS_FORCE_FROZEN",
        }
    )
    exported = {name: value for name, value in os.environ.items() if canonical_env_name(name) in names}
    for name in exported:
        del os.environ[name]
    config.stash[_STRIPPED_CHRYS_ENV] = exported

    patcher = pytest.MonkeyPatch()
    patcher.setattr(socket.socket, "connect", guarded_socket_connect(socket.socket.connect))
    patcher.setattr(socket.socket, "connect_ex", guarded_socket_connect(socket.socket.connect_ex))
    patcher.setattr(socket.socket, "sendto", guarded_sendto(socket.socket.sendto))
    if hasattr(socket.socket, "sendmsg"):
        patcher.setattr(socket.socket, "sendmsg", guarded_sendmsg(socket.socket.sendmsg))
    patcher.setattr(socket, "create_connection", guarded_create_connection(socket.create_connection))
    patcher.setattr(socket, "getaddrinfo", guarded_getaddrinfo(socket.getaddrinfo))
    patcher.setattr(socket, "gethostbyname", guarded_hostname_resolution(socket.gethostbyname))
    patcher.setattr(socket, "gethostbyname_ex", guarded_hostname_resolution(socket.gethostbyname_ex))
    config.stash[_NETWORK_GUARD_PATCHER] = patcher

    # CI sets ``CHRYS_TEST_STALL_DIAGNOSTICS`` (an unclassified name, so it survives the strip above):
    # a slow or failed test then gets a report of where its call phase spent its time.
    sampler = start_stall_sampler_from_environment()
    if sampler is not None:
        config.stash[_STALL_SAMPLER] = sampler


def pytest_unconfigure(config: pytest.Config) -> None:
    """Remove the egress guard, give back the stripped environment and the real platform."""
    sampler = config.stash.get(_STALL_SAMPLER, None)
    if sampler is not None:
        sampler.close()
    patcher = config.stash.get(_NETWORK_GUARD_PATCHER, None)
    if patcher is not None:
        patcher.undo()
    os.environ.update(config.stash.get(_STRIPPED_CHRYS_ENV, {}))
    restore_real_platform()
    shutil.rmtree(_SESSION_CONFIG_DIR, ignore_errors=True)


@pytest.fixture(autouse=True)
def _strip_ambient_terminal_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep ambient terminal color settings from changing test output."""
    for name in ("FORCE_COLOR", "COLORTERM", "NO_COLOR", "CLICOLOR_FORCE"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def _english_system_locale(monkeypatch: pytest.MonkeyPatch) -> None:
    """Answer ``locale: system`` with English, whatever language the machine running the tests speaks.

    ``system`` is the default, so every app a test builds from default settings would otherwise
    render its prose in the developer's language and fail each assertion on English text. Tests of
    the detection itself opt back in by overriding this fixture (see
    ``tests/foundation/i18n/test_locale.py``).
    """
    from chrys.foundation.i18n import locale as locale_module

    monkeypatch.setattr(locale_module, "_system_locale_name", lambda: "en-US")


@pytest.fixture(autouse=True)
def _uninstalled_process_settings() -> Iterator[None]:
    """Keep the RESTART-scoped process snapshot out of cross-test state.

    Anything that runs ``bootstrap_runtime`` installs one for the rest of the
    process, which would then outrank the env every later test sets up.
    """
    from chrys.foundation.config.process_env import _reset_process_env_snapshot_for_tests
    from chrys.foundation.config.process_settings import reset_process_settings
    from chrys.foundation.config.runtime_pointer import _reset_model_pointer_for_tests

    reset_process_settings()
    _reset_process_env_snapshot_for_tests()
    _reset_model_pointer_for_tests()
    yield
    reset_process_settings()
    _reset_process_env_snapshot_for_tests()
    _reset_model_pointer_for_tests()


@pytest.fixture(autouse=True)
def _no_reached_first_hops() -> Iterator[None]:
    """Start every test with no LLM first hop marked reached.

    The set is process-wide on purpose (a hop once reached makes a later
    NONAME retryable); across tests it would make DNS verdicts order-dependent.
    """
    from chrys.service.llm.route_facts import reset_reached_first_hops

    reset_reached_first_hops()
    yield
    reset_reached_first_hops()


@pytest.fixture(autouse=True)
def _disable_prompt_history(monkeypatch: pytest.MonkeyPatch) -> None:
    """Block tests from writing to the real ``~/.chrys/prompt_history.jsonl``.

    TUI integration tests that drive a full ``ChrysApp`` through ``pilot``
    will reach ``append_history`` when the input bar records a submitted
    prompt; without this guard they would persist test prompts into the
    developer's real history file. Tests that exercise the history layer
    itself opt back in by deleting the env var inside their own fixture (see
    ``tests/app/tui/support/test_prompt_history.py::history_dir``).
    """
    monkeypatch.setenv("CHRYS_HISTORY_DISABLE", "1")


@pytest.fixture(autouse=True)
def _unhurried_run_record_writes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give workflow run records a test wait budget instead of the product's write ack bound.

    A run record that misses its ack fails the run as storage-failed (the record
    is the run's source of truth), and a loaded CI disk stalls plain writes for
    seconds under fsync pressure — long enough to end any host-driven workflow
    test in a storage failure it is not about. Tests of the bound itself pass
    ``write_ack_timeout`` to ``WorkflowRunStore.open`` explicitly
    (``tests/service/workflows/test_store.py``).
    """
    from chrys.service.workflows import store as store_module

    monkeypatch.setattr(store_module, "RUN_RECORD_WRITE_ACK_TIMEOUT_SECONDS", ENGINE_TURN_TIMEOUT)


_REAL_PLATFORM = _cached_get_platform()
"""The developer's actual platform, detected once before the redirect goes in."""

_SESSION_CONFIG_DIR = Path(tempfile.mkdtemp(prefix="chrys-session-config-"))
"""Where the config dir points outside any test — collection, session fixtures.

A test's own ``tmp_path`` does not exist yet at those moments, and pointing at
the real directory until the first test starts would leave exactly the setup
code that runs earliest unguarded.
"""

atexit.register(shutil.rmtree, _SESSION_CONFIG_DIR, ignore_errors=True)

_pinned_config_dir: Path | None = _SESSION_CONFIG_DIR
"""Current redirect; ``None`` hands back the real directory (opt-out marker)."""


def _detect_with_pinned_config_dir() -> PlatformInfo:
    """Real detection with only the two directories redirected.

    Installed over ``platform.detect_platform`` for the whole session rather
    than for the instant it takes to prime the cache: ``get_platform`` is
    ``functools.cache``d, and a bare ``cache_clear()`` — from a test, or from
    product code — would otherwise refill the cache from the real detection and
    silently hand the developer's own ``~/.chrys`` back to every writer.

    Detection still runs, so the tests that exercise it keep exercising it;
    only ``config_dir`` and ``data_dir`` are answered from here.
    """
    detected = _original_detect_platform()
    if _pinned_config_dir is None:
        return detected
    return dataclasses.replace(detected, config_dir=_pinned_config_dir, data_dir=_pinned_config_dir)


def _pin_config_dir(config_dir: Path | None) -> None:
    """Point every holder of ``get_platform`` at *config_dir*.

    ``get_platform`` is imported by value throughout the codebase (``from
    chrys.foundation.platform import get_platform``), so rebinding the name in
    the platform module reaches only the modules that did not import it
    directly — which is why every existing guard has to patch its own importer
    one module at a time. Refilling the cache reaches all of them at once.

    The cache is reached through the function object captured at import, not
    through ``platform.get_platform``: ``monkeypatch`` is set up before the
    autouse guards and therefore finalizes after them, so a test that patched
    that attribute still has its plain-function stand-in — which owns no cache
    — bound when this unwinds.

    Refilling is done from the platform detected at import, not by detecting
    again: detection costs milliseconds and this runs twice per test.
    """
    global _pinned_config_dir

    _pinned_config_dir = config_dir
    primed = (
        _REAL_PLATFORM
        if config_dir is None
        else dataclasses.replace(_REAL_PLATFORM, config_dir=config_dir, data_dir=config_dir)
    )
    platform_mod.detect_platform = lambda: primed
    try:
        _cached_get_platform.cache_clear()
        _cached_get_platform()
    finally:
        platform_mod.detect_platform = _detect_with_pinned_config_dir


def restore_real_platform() -> None:
    """Put genuine detection back — the import-time override does not own the process.

    ``pytest`` from the command line exits straight after ``pytest_unconfigure``,
    which is why leaving this undone stays invisible there. Under
    ``pytest.main()`` inside a longer-lived process it is not: the run returns,
    and every ``get_platform()`` afterwards still answers with a session temp
    directory that is about to be deleted. The ``atexit`` cleanup registered
    beside ``_SESSION_CONFIG_DIR`` remains the fallback for the exits that never
    reach the hook.
    """
    global _pinned_config_dir

    _pinned_config_dir = None
    platform_mod.detect_platform = _original_detect_platform
    _cached_get_platform.cache_clear()
    _cached_get_platform()


def _arm_config_dir_redirect() -> None:
    """Send the config dir to the session temp directory, creating it if it is gone.

    Called at import so collection is covered from the first module read, and
    again from ``pytest_configure`` for every run after the first: a second
    ``pytest.main()`` in the same process finds this module already imported,
    so nothing here would run again — while the previous run's
    ``pytest_unconfigure`` has handed the real directory back and deleted the
    one below. Without the second call that run's collection and its
    session-scoped setup would write to the developer's own config dir.
    """
    _SESSION_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    _pin_config_dir(_SESSION_CONFIG_DIR)


_original_detect_platform = platform_mod.detect_platform
_arm_config_dir_redirect()


@pytest.fixture(autouse=True)
def _isolate_platform_config_dir(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[None]:
    """Point ``get_platform().config_dir`` at the per-test temp directory.

    The backstop under every writer that takes its path from the platform
    singleton — the settings document, model profiles,
    skills. Those writers are best-effort by design (``persist_theme`` and its
    siblings log and swallow), so a test that forgets to redirect the config
    dir does not fail: it silently edits the developer's real ``~/.chrys``,
    and the damage is found days later in a config that no longer says what
    the user set. Isolating by default makes a forgotten redirect cost a temp
    directory instead.

    ``data_dir`` moves with it because the two are the same directory in
    production; leaving it real would keep exactly the writers that use it
    pointed at the developer's home.

    A test that genuinely needs the real directory opts out with
    ``@pytest.mark.real_config_dir``. This also exposes the real workspace
    MRU file and hook discovery directory; marked tests must guard writes.
    """
    marked = request.node.get_closest_marker("real_config_dir") is not None
    _pin_config_dir(None if marked else tmp_path / "platform-config")
    try:
        yield
    finally:
        _pin_config_dir(_SESSION_CONFIG_DIR)


@pytest.fixture(scope="session")
def real_platform_config_dir() -> Path:
    """The developer's actual config directory — for guards proving they miss it."""
    return _REAL_PLATFORM.config_dir


@pytest.fixture(autouse=True)
def _isolate_session_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Keep default session storage for tests under the per-test temp directory."""
    from chrys.foundation.config import settings as settings_module

    settings_module._RESOLVED_SESSIONS_DIR_CACHE.clear()
    monkeypatch.setenv(settings_module.SESSION_ROOT_DIR_ENV_VAR, str(tmp_path / "session-root"))
    try:
        yield
    finally:
        settings_module._RESOLVED_SESSIONS_DIR_CACHE.clear()


_TEST_DIRECTORY_SEQUENCE = itertools.count()


def _new_test_directory(factory: pytest.TempPathFactory, prefix: str) -> Path:
    """Create ``<basetemp>/<prefix><n>`` for the next free ``n``, in constant time."""
    basetemp = factory.getbasetemp()
    while True:
        candidate = basetemp / f"{prefix}{next(_TEST_DIRECTORY_SEQUENCE)}"
        try:
            candidate.mkdir(mode=0o700)
        except FileExistsError:
            continue
        return candidate


@pytest.fixture
def tmp_path(request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory) -> Path:
    """pytest's ``tmp_path`` contract without its per-test directory listing.

    pytest numbers every new directory by listing all entries already in the
    base temp directory, so each test's ``tmp_path`` costs more than the one
    before it on the same worker. Every test pays that here, because the
    autouse isolation fixtures request ``tmp_path``, and on Windows runners the
    listing was the largest single fixture cost in the suite. A process-wide
    counter yields the same unique, test-named directory under the same base
    temp. The suite keeps pytest's default ``tmp_path_retention_policy``
    ("all"), the only policy this override implements.
    """
    return _new_test_directory(tmp_path_factory, re.sub(r"\W", "_", request.node.name)[:30])


@pytest.fixture(autouse=True)
def _isolated_working_directory(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    """Run every test from its own empty directory rather than the checkout.

    Product code falls back to the process working directory for the
    workspace, project settings and relative writes. From the repository
    root, every engine turn in a test captured the whole checkout as a git
    workspace (``rev-parse``, ``symbolic-ref`` and ``status`` over thousands of
    files, twice per turn) and could read local project configuration. The
    directory sits beside ``tmp_path`` rather than inside it, so tests that
    list their ``tmp_path`` see only what they created, and its name carries
    no test name, so text that echoes the working directory stays neutral.
    """
    monkeypatch.chdir(_new_test_directory(tmp_path_factory, "cwd-"))


@pytest.fixture(scope="session")
def git_template_repo(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Build one hermetic committed repository for cheap per-test copies."""
    git = shutil.which("git")
    if git is None:
        pytest.skip("git is required for repository-backed tests")

    repo = tmp_path_factory.mktemp("git-template")
    env = os.environ.copy()
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_CONFIG_SYSTEM"] = os.devnull
    subprocess.run([git, "init", "-q"], cwd=repo, env=env, check=True, capture_output=True)
    subprocess.run([git, "config", "user.name", "Chrys Tests"], cwd=repo, env=env, check=True, capture_output=True)
    subprocess.run(
        [git, "config", "user.email", "tests@chrys.local"], cwd=repo, env=env, check=True, capture_output=True
    )
    # `git commit` spawns detached auto-maintenance whose lock files appear
    # and vanish inside .git while this worker's test process copytrees the
    # template (reproduced as a setup error under -n 8). Disable it
    # repo-locally BEFORE committing; the config is copied with the template,
    # so per-test copies inherit the same guarantee for any git commands they
    # run.
    for key, value in (("maintenance.auto", "false"), ("gc.auto", "0"), ("gc.autoDetach", "false")):
        subprocess.run([git, "config", key, value], cwd=repo, env=env, check=True, capture_output=True)
    (repo / "README.md").write_text("committed content", encoding="utf-8")
    subprocess.run([git, "add", "README.md"], cwd=repo, env=env, check=True, capture_output=True)
    subprocess.run(
        [git, "commit", "-q", "-m", "init"],
        cwd=repo,
        env=env,
        check=True,
        capture_output=True,
    )
    return repo


@pytest.fixture
def git_repo_factory(
    git_template_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[[Path], Path]:
    """Return a copier for isolated repositories with ambient Git config disabled."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)

    def _copy(destination: Path) -> Path:
        shutil.copytree(git_template_repo, destination, dirs_exist_ok=True)
        return destination

    return _copy


@dataclass
class LocalHTTPServer:
    """Small local HTTP server used by transport tests."""

    url: str
    hits: list[str]


@pytest.fixture
def clear_proxy_env(monkeypatch: pytest.MonkeyPatch) -> Callable[[], None]:
    """Return a helper that clears proxy-related env vars."""

    def _clear() -> None:
        for key in (
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "ALL_PROXY",
            "NO_PROXY",
            "http_proxy",
            "https_proxy",
            "all_proxy",
            "no_proxy",
        ):
            monkeypatch.delenv(key, raising=False)

    return _clear


@pytest.fixture
def direct_route(monkeypatch: pytest.MonkeyPatch, clear_proxy_env: Callable[[], None]) -> None:
    """Send HTTP straight to its host, as a loopback stub or an injected fault needs.

    With no proxy env at all, httpx falls back to the macOS/Windows system proxy.
    """
    clear_proxy_env()
    monkeypatch.setenv("NO_PROXY", "*")


@pytest.fixture
def local_http_server() -> Callable[..., AsyncIterator[LocalHTTPServer]]:
    """Factory for a local one-response-per-request HTTP/HTTPS server."""

    @contextlib.asynccontextmanager
    async def _factory(
        body: bytes = b"ok",
        *,
        scheme: str = "http",
        ssl_context: ssl.SSLContext | None = None,
    ) -> AsyncIterator[LocalHTTPServer]:
        hits: list[str] = []

        async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            try:
                raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=3)
                request_line = raw.split(b"\r\n", 1)[0].decode("ascii", errors="replace")
                hits.append(request_line)
                response = (
                    b"HTTP/1.1 200 OK\r\n"
                    + f"Content-Length: {len(body)}\r\n".encode()
                    + b"Content-Type: text/plain\r\n"
                    + b"Connection: close\r\n\r\n"
                    + body
                )
                writer.write(response)
                await writer.drain()
            finally:
                writer.close()
                # ``wait_closed`` on a TLS writer can hang on Windows IOCP when
                # the peer half-closed the connection — bound it so teardown
                # never blocks the fixture indefinitely.
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(writer.wait_closed(), timeout=2.0)

        server = await asyncio.start_server(_handle, host="127.0.0.1", port=0, ssl=ssl_context)
        try:
            socket = server.sockets[0]
            port = socket.getsockname()[1]
            yield LocalHTTPServer(url=f"{scheme}://127.0.0.1:{port}", hits=hits)
        finally:
            server.close()
            # Same Windows IOCP rationale as ``writer.wait_closed`` above:
            # any in-flight handler task must drain or be abandoned within a
            # bounded window so the fixture exits.
            with contextlib.suppress(Exception):
                await asyncio.wait_for(server.wait_closed(), timeout=2.0)

    return _factory


@pytest.fixture(scope="session")
def self_signed_server_cert_material() -> tuple[bytes, bytes]:
    """Generate localhost self-signed certificate material once per worker session."""
    cryptography = pytest.importorskip("cryptography")
    assert cryptography is not None

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.DNSName("localhost"),
                    x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
                ]
            ),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )

    return (
        cert.public_bytes(serialization.Encoding.PEM),
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        ),
    )


@pytest.fixture
def self_signed_server_ssl_context(
    tmp_path: Path,
    self_signed_server_cert_material: tuple[bytes, bytes],
) -> ssl.SSLContext:
    """Build a fresh localhost TLS context from session-cached key material."""
    cert_material, key_material = self_signed_server_cert_material

    cert_path = tmp_path / "server.crt"
    key_path = tmp_path / "server.key"
    cert_path.write_bytes(cert_material)
    key_path.write_bytes(key_material)

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certfile=cert_path, keyfile=key_path)
    return context
