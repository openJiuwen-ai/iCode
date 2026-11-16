# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for ChrysFooter recompose, binding publication, and localization."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from textual.screen import Screen

from chrys.app.tui import i18n as tui_i18n
from chrys.app.tui.i18n import LocaleSwitchStatus
from chrys.foundation.config.settings import Settings
from tests.support.tui_app_harness import SessionGenerationEngine, make_chrys_app
from tests.support.waiting import wait_for, wait_until_quiet, with_wait_deadline


async def _wait_for_footer_settled(footer: object, app: object, pilot: object) -> None:
    """Wait until the footer has committed the main screen's binding signature.

    Recomposes ride paint scheduling, so a spy installed before the footer
    settles records callbacks the test never asked for.
    """
    await wait_for(
        lambda: (
            not footer._binding_recompose_in_progress
            and not footer._binding_recompose_dirty
            and footer._visible_binding_signature == footer._binding_signature(app._main_screen)
        ),
        pilot=pilot,
        description="footer binding recompose settles",
    )


async def test_chrys_footer_composes_bindings_before_deferred_refresh(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Startup shortcuts render even if the first deferred refresh is stranded."""
    from chrys.app.tui.widgets.chrome.footer import ChrysFooter

    monkeypatch.setattr(ChrysFooter, "call_after_refresh", lambda _self, *_args: False)
    app = make_chrys_app(tmp_path, engine=SessionGenerationEngine())

    async with app.run_test():
        assert app._main_screen is not None
        footer = app._main_screen.query_one(ChrysFooter)
        actions = {key.action for key in footer.query("FooterKey")}

        assert {"sessions", "agents_config", "models_config", "show_log_viewer"} <= actions


async def test_chrys_footer_recomposes_only_for_visible_binding_changes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Focus and no-op binding signals must not rebuild the MainScreen Footer."""
    from textual import events

    from chrys.app.tui.widgets.chrome.footer import ChrysFooter

    app = make_chrys_app(tmp_path, engine=SessionGenerationEngine())

    async with app.run_test() as pilot:
        assert app._main_screen is not None
        footer = app._main_screen.query_one(ChrysFooter)
        await pilot.pause()
        # Exercise the real async recompose before replacing it with a spy.
        # The callback may originate in ChrysApp context, but Footer.compose
        # must bind Footer.compact against the Footer itself.
        app._main_screen._set_agent_running(True)
        await _wait_for_footer_settled(footer, app, pilot)
        running_actions = {key.action for key in footer.query("FooterKey")}
        assert {"pick_theme", "settings"}.isdisjoint(running_actions)

        app._main_screen._set_agent_running(False)
        await pilot.pause()
        # The recompose scheduled by the flip back rides paint scheduling;
        # under load it can land after the spy is installed and pollute the
        # no-change window below. Wait until the footer has committed the
        # settled signature — any callback still queued at that point is
        # generation-stale and no-ops before reaching recompose.
        await _wait_for_footer_settled(footer, app, pilot)
        idle_actions = {key.action for key in footer.query("FooterKey")}
        assert {"pick_theme", "settings"} <= idle_actions
        recomposes: list[None] = []

        async def _record_recompose() -> None:
            recomposes.append(None)

        monkeypatch.setattr(footer, "recompose", _record_recompose)

        app._main_screen.refresh_bindings()
        app._main_screen.refresh_bindings()
        await pilot.pause()
        assert recomposes == []

        app._main_screen._set_agent_running(True)
        # The recompose callback rides screen-paint scheduling; under Windows
        # xdist load a single pause can return before the paint fires. Poll
        # the observable count instead of a fixed wait.
        await wait_for(lambda: recomposes == [None], pilot=pilot, description="running footer recompose")
        assert recomposes == [None]
        app._main_screen._set_agent_running(True)
        await pilot.pause()
        assert recomposes == [None]

        await app.on_event(events.AppBlur())
        app._main_screen._set_agent_running(False)
        await pilot.pause()
        assert recomposes == [None]

        await app.on_event(events.AppFocus())
        await wait_for(lambda: recomposes == [None, None], pilot=pilot, description="refocused footer recompose")
        assert recomposes == [None, None]


async def test_chrys_footer_resume_recovers_stranded_recompose(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A callback lost with an overlay must not commit or suppress its signature."""
    from textual import events

    from chrys.app.tui.widgets.chrome.footer import ChrysFooter

    app = make_chrys_app(tmp_path, engine=SessionGenerationEngine())

    async with app.run_test() as pilot:
        assert app._main_screen is not None
        footer = app._main_screen.query_one(ChrysFooter)
        await _wait_for_footer_settled(footer, app, pilot)
        recomposes = 0

        async def _record_recompose() -> None:
            nonlocal recomposes
            recomposes += 1

        deferred: list[tuple[object, tuple[object, ...], dict[str, object]]] = []

        def _strand_callback(callback: object, *args: object, **kwargs: object) -> bool:
            deferred.append((callback, args, kwargs))
            return True

        monkeypatch.setattr(footer, "recompose", _record_recompose)
        monkeypatch.setattr(footer, "call_after_refresh", _strand_callback)
        rendered_signature = footer._visible_binding_signature
        app._main_screen._set_agent_running(True)
        await pilot.pause()

        assert deferred
        assert footer._visible_binding_signature == rendered_signature
        stranded = tuple(deferred)
        app._main_screen.on_screen_resume(events.ScreenResume())
        assert len(deferred) > len(stranded)

        callback, args, kwargs = deferred[-1]
        await callback(*args, **kwargs)  # type: ignore[operator]
        assert footer._visible_binding_signature != rendered_signature
        assert recomposes == 1

        for callback, args, kwargs in stranded:
            await callback(*args, **kwargs)  # type: ignore[operator]
        assert recomposes == 1

        deferred.clear()
        current_signature = ["A"]
        footer._visible_binding_signature = "A"  # type: ignore[assignment]
        monkeypatch.setattr(footer, "_binding_signature", lambda _screen: current_signature[0])

        current_signature[0] = "B"
        footer.bindings_changed(app._main_screen)
        stale_b_callback = deferred[-1]
        current_signature[0] = "A"
        footer.bindings_changed(app._main_screen)

        callback, args, kwargs = stale_b_callback
        await callback(*args, **kwargs)  # type: ignore[operator]
        assert footer._visible_binding_signature == "A"
        assert recomposes == 1

        current_signature[0] = "B"
        footer.bindings_changed(app._main_screen)
        callback, args, kwargs = deferred[-1]
        await callback(*args, **kwargs)  # type: ignore[operator]
        assert footer._visible_binding_signature == "B"
        assert recomposes == 2

        await app.push_screen(Screen())
        current_signature[0] = "A"
        footer.bindings_changed(app._main_screen)
        callback, args, kwargs = deferred[-1]
        await callback(*args, **kwargs)  # type: ignore[operator]
        assert app.screen is not app._main_screen
        assert footer._visible_binding_signature == "B"
        assert recomposes == 2
        await app.pop_screen()
        await pilot.pause()
        callback, args, kwargs = deferred[-1]
        await callback(*args, **kwargs)  # type: ignore[operator]
        assert footer._visible_binding_signature == "A"
        assert recomposes == 3


async def test_chrys_footer_retries_binding_publish_during_recompose(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A publish during recompose must reconcile rendered and committed state."""
    from chrys.app.tui.widgets.chrome.footer import ChrysFooter

    app = make_chrys_app(tmp_path, engine=SessionGenerationEngine())

    async with app.run_test() as pilot:
        assert app._main_screen is not None
        footer = app._main_screen.query_one(ChrysFooter)
        await _wait_for_footer_settled(footer, app, pilot)
        current_signature = ["A"]
        rendered: list[str] = []
        first_recompose_started = asyncio.Event()
        release_first_recompose = asyncio.Event()
        deferred: list[tuple[object, tuple[object, ...], dict[str, object]]] = []

        monkeypatch.setattr(footer, "_binding_signature", lambda _screen: current_signature[0])

        async def _record_recompose() -> None:
            rendered.append(current_signature[0])
            if len(rendered) == 1:
                first_recompose_started.set()
                await release_first_recompose.wait()

        def _defer(callback: object, *args: object, **kwargs: object) -> bool:
            deferred.append((callback, args, kwargs))
            return True

        monkeypatch.setattr(footer, "recompose", _record_recompose)
        monkeypatch.setattr(footer, "call_after_refresh", _defer)
        footer._visible_binding_signature = "A"  # type: ignore[assignment]
        current_signature[0] = "B"
        footer.bindings_changed(app._main_screen)
        callback, args, kwargs = deferred.pop()
        first_pass = asyncio.create_task(callback(*args, **kwargs))  # type: ignore[operator]
        await first_recompose_started.wait()

        current_signature[0] = "A"
        footer.bindings_changed(app._main_screen)
        release_first_recompose.set()
        await first_pass

        assert rendered == ["B"]
        assert footer._binding_recompose_dirty is True
        callback, args, kwargs = deferred.pop()
        await callback(*args, **kwargs)  # type: ignore[operator]
        assert rendered == ["B", "A"]
        assert footer._visible_binding_signature == "A"
        assert footer._binding_recompose_dirty is False


async def test_chrys_footer_localizes_in_place_with_natural_per_locale_widths(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A locale switch rewrites FooterKey text in place and re-measures widths."""
    from textual.widgets._footer import FooterKey

    from chrys.app.tui.widgets.chat.panel import ChatPanel
    from chrys.app.tui.widgets.chrome.footer import ChrysFooter

    monkeypatch.setattr(tui_i18n, "persist_locale", lambda _locale: None)
    app = make_chrys_app(tmp_path, settings=Settings(locale="en"), engine=SessionGenerationEngine())

    async with app.run_test() as pilot:
        assert app._main_screen is not None
        footer = app._main_screen.query_one(ChrysFooter)
        chat = app._main_screen.query_one(ChatPanel)
        await pilot.pause()
        await _wait_for_footer_settled(footer, app, pilot)

        keys_before = tuple(footer.query(FooterKey))
        assert {key.action for key in keys_before} == {
            "sessions",
            "agents_config",
            "models_config",
            "show_log_viewer",
            "open_guide",
            "toggle_trajectory_dashboard",
            "toggle_sidebar",
            "pick_theme",
            "settings",
        }
        widths_before = {key.action: key.region.width for key in keys_before}
        english_descriptions = {key.action: key.description for key in keys_before}
        # Stock auto-width geometry: every key exactly fits its English text.
        assert all(key.content_region.width == key.render().cell_len for key in keys_before)
        committed_signature = footer._visible_binding_signature

        recomposes: list[None] = []
        layout_refreshes: list[None] = []
        chat_refreshes: list[bool] = []
        chat_restyles: list[None] = []

        async def _record_recompose() -> None:
            recomposes.append(None)

        def _record_chat_refresh(
            *_regions: object,
            repaint: bool = True,
            layout: bool = False,
            recompose: bool = False,
        ) -> ChatPanel:
            del repaint, recompose
            chat_refreshes.append(layout)
            return chat

        original_refresh_layout = app._main_screen._refresh_layout

        def _record_layout(*args: object, **kwargs: object) -> None:
            layout_refreshes.append(None)
            original_refresh_layout(*args, **kwargs)

        monkeypatch.setattr(footer, "recompose", _record_recompose)
        monkeypatch.setattr(app._main_screen, "_refresh_layout", _record_layout)
        monkeypatch.setattr(chat, "refresh", _record_chat_refresh)
        monkeypatch.setattr(chat, "update_node_styles", lambda animate=True: chat_restyles.append(None))
        await wait_until_quiet(
            lambda: (len(layout_refreshes), len(chat_refreshes), len(chat_restyles)),
            description="theme refresh counters",
            pilot=pilot,
        )
        layout_refreshes.clear()
        chat_refreshes.clear()
        chat_restyles.clear()

        result = app.locale_controller.switch_locale("zh-Hans")
        await pilot.pause()
        await pilot.pause()

        keys_after = tuple(footer.query(FooterKey))
        assert result.status is LocaleSwitchStatus.EFFECTIVE_CHANGED
        assert len(keys_after) == len(keys_before)
        assert all(before is after for before, after in zip(keys_before, keys_after, strict=True))
        assert recomposes == []
        assert footer._visible_binding_signature is committed_signature
        assert {key.action: key.description for key in keys_after} == {
            "sessions": "会话",
            "agents_config": "智能体",
            "models_config": "模型",
            "show_log_viewer": "日志",
            "open_guide": "帮助",
            "toggle_trajectory_dashboard": "轨迹",
            "toggle_sidebar": "侧边栏",
            "pick_theme": "主题",
            "settings": "设置",
        }
        # Natural per-locale geometry: each key re-measures to exactly fit its
        # zh-Hans text, so the shorter CJK labels shrink their keys.
        assert all(key.content_region.width == key.render().cell_len for key in keys_after)
        widths_after = {key.action: key.region.width for key in keys_after}
        assert widths_after["sessions"] < widths_before["sessions"]
        assert any(key.description != english_descriptions[key.action] for key in keys_after)
        assert layout_refreshes != []
        assert chat_refreshes == []
        assert chat_restyles == []


@pytest.mark.parametrize("queued_binding_publish", [False, True], ids=["settled", "queued-binding-publish"])
@with_wait_deadline(30.0)
async def test_chrys_footer_locale_change_after_compose_before_mount_is_reconciled(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    queued_binding_publish: bool,
) -> None:
    """A stale compose-node batch is translated once after its async mount."""
    from collections.abc import Iterable

    from textual.widget import Widget
    from textual.widgets._footer import FooterKey

    from chrys.app.tui.widgets.chrome.footer import ChrysFooter

    monkeypatch.setattr(tui_i18n, "persist_locale", lambda _locale: None)
    app = make_chrys_app(tmp_path, settings=Settings(locale="en"), engine=SessionGenerationEngine())

    async with app.run_test() as pilot:
        assert app._main_screen is not None
        footer = app._main_screen.query_one(ChrysFooter)
        await _wait_for_footer_settled(footer, app, pilot)
        compose_nodes_ready = asyncio.Event()
        release_mount = asyncio.Event()
        recompose_calls: list[None] = []
        original_recompose = footer.recompose
        original_mount_all = footer.mount_all

        async def _record_recompose() -> None:
            recompose_calls.append(None)
            await original_recompose()

        async def _pause_mount_all(
            widgets: Iterable[Widget],
            *,
            before: int | str | Widget | None = None,
            after: int | str | Widget | None = None,
        ) -> None:
            compose_nodes_ready.set()
            await release_mount.wait()
            await original_mount_all(widgets, before=before, after=after)

        binding_signal = app._main_screen.bindings_updated_signal
        original_bindings_changed = footer.bindings_changed
        stray_binding_publishes: list[None] = []
        task: asyncio.Task[None] | None = None
        with monkeypatch.context() as patch:
            # Isolate locale reconciliation from new binding changes. The
            # signal retains the original bound method, so unsubscribe as well.
            patch.setattr(footer, "bindings_changed", lambda _screen: stray_binding_publishes.append(None))
            if queued_binding_publish:
                # Real signals queue the original method on the Footer pump;
                # neither the attribute patch nor unsubscribe cancels that work.
                binding_signal.publish(app._main_screen)
            binding_signal.unsubscribe(footer)
            try:
                # Drain already-published callbacks BEFORE choosing the test's
                # generation or intercepting mount_all. A late callback would
                # invalidate that generation, skipping the mount barrier entirely.
                queued_callbacks_drained = asyncio.Event()
                assert footer.call_later(queued_callbacks_drained.set)
                await wait_for(
                    queued_callbacks_drained.is_set,
                    pilot=pilot,
                    description="queued footer binding callbacks drain",
                )
                await _wait_for_footer_settled(footer, app, pilot)
                patch.setattr(footer, "recompose", _record_recompose)
                patch.setattr(footer, "mount_all", _pause_mount_all)
                footer._binding_recompose_generation += 1
                generation = footer._binding_recompose_generation
                task = asyncio.create_task(footer._recompose_bindings(app._main_screen, generation))
                # Don't pump Pilot while mount is deliberately held. Also
                # observe early task completion so a skipped/failed pass is
                # diagnosed at its cause, rather than timing out on the barrier.
                await wait_for(
                    lambda: compose_nodes_ready.is_set() or task.done(),
                    timeout=10.0,
                    description="footer compose reaches the held mount",
                )
                if task.done():
                    await task
                assert compose_nodes_ready.is_set(), "footer recompose exited before the mount barrier"

                assert footer._rendered_locale_revision == 0
                assert app.locale_controller.switch_locale("zh-Hans").status is LocaleSwitchStatus.EFFECTIVE_CHANGED
                assert footer._localization_dirty is True
                release_mount.set()
                await wait_for(task.done, timeout=10.0, description="footer locale reconciliation completes")
                await task
                await pilot.pause()

                assert recompose_calls == [None]
                assert footer._localization_dirty is False
                assert footer._rendered_locale_revision == app.locale_controller.revision == 1
                assert {key.description for key in footer.query(FooterKey)} >= {
                    "会话",
                    "智能体",
                    "模型",
                    "日志",
                    "侧边栏",
                    "主题",
                    "设置",
                }
            finally:
                release_mount.set()
                if task is not None:
                    if not task.done():
                        task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                binding_signal.subscribe(footer, original_bindings_changed)


async def test_diff_screen_footer_uses_controller_and_localizes_without_main_transcript_work(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The production diff navigation wires its active footer into the switch pass."""
    from textual.widgets._footer import FooterKey

    from chrys.app.tui.screens.diff.screen import DiffScreen
    from chrys.app.tui.util.diff_entries import DiffFileEntry
    from chrys.app.tui.widgets.chat.panel import ChatPanel
    from chrys.app.tui.widgets.chrome.footer import ChrysFooter
    from chrys.service.mutations.types import MutationOp

    entry = DiffFileEntry(
        path=str(tmp_path / "localized.py"),
        rel_path="localized.py",
        operation=MutationOp.MODIFY,
        old_path=None,
        before_text="before\n",
        after_text="after\n",
        is_binary=False,
        encoding="utf-8",
        bytes_changed=True,
    )
    monkeypatch.setattr(tui_i18n, "persist_locale", lambda _locale: None)
    app = make_chrys_app(tmp_path, settings=Settings(locale="en"), engine=SessionGenerationEngine())

    async with app.run_test() as pilot:
        assert app._main_screen is not None
        chat = app._main_screen.query_one(ChatPanel)
        app._main_screen._view_adapter.open_diff_screen(
            {1: [entry]},
            cwd=str(tmp_path),
            subtitle_parts=(),
            session_id="session-id",
            all_entries=[entry],
        )
        await wait_for(
            lambda: isinstance(app.screen, DiffScreen) and app.screen.is_mounted and app.screen._content_ready,
            timeout=10.0,
            pilot=pilot,
            description="diff screen content ready",
        )
        assert isinstance(app.screen, DiffScreen)
        diff_screen = app.screen
        assert diff_screen._locale_controller is app.locale_controller
        footer = diff_screen.query_one(ChrysFooter)
        expected_en_keys = {"Back", "Toggle Split View", "Toggle Change List", "Quit"}
        await wait_for(
            lambda: {key.description for key in footer.query("FooterKey")} == expected_en_keys,
            pilot=pilot,
            description="English footer keys",
        )

        chat_refreshes: list[bool] = []
        chat_restyles: list[None] = []

        def _record_chat_refresh(
            *_regions: object,
            repaint: bool = True,
            layout: bool = False,
            recompose: bool = False,
        ) -> ChatPanel:
            del repaint, recompose
            chat_refreshes.append(layout)
            return chat

        monkeypatch.setattr(chat, "refresh", _record_chat_refresh)
        monkeypatch.setattr(chat, "update_node_styles", lambda animate=True: chat_restyles.append(None))

        assert app.locale_controller.switch_locale("zh-Hans").status is LocaleSwitchStatus.EFFECTIVE_CHANGED
        expected_zh_keys = {"返回", "切换并排视图", "显示/隐藏文件列表", "退出"}
        await wait_for(
            lambda: {key.description for key in footer.query("FooterKey")} == expected_zh_keys,
            pilot=pilot,
            description="Chinese footer keys",
        )
        # One settled pass so the freshly mounted keys gain layout geometry.
        await pilot.pause()
        assert all(key.content_region.width == key.render().cell_len for key in footer.query(FooterKey))
        assert chat_refreshes == []
        assert chat_restyles == []


async def test_diff_screen_resume_syncs_footer_bindings(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Resuming the diff screen replays a footer recompose stranded pre-activation."""
    from textual import events

    from chrys.app.tui.screens.diff.screen import DiffScreen
    from chrys.app.tui.util.diff_entries import DiffFileEntry
    from chrys.app.tui.widgets.chrome.footer import ChrysFooter
    from chrys.service.mutations.types import MutationOp

    entry = DiffFileEntry(
        path=str(tmp_path / "resumed.py"),
        rel_path="resumed.py",
        operation=MutationOp.MODIFY,
        old_path=None,
        before_text="before\n",
        after_text="after\n",
        is_binary=False,
        encoding="utf-8",
        bytes_changed=True,
    )
    app = make_chrys_app(tmp_path, settings=Settings(locale="en"), engine=SessionGenerationEngine())

    async with app.run_test() as pilot:
        assert app._main_screen is not None
        app._main_screen._view_adapter.open_diff_screen(
            {1: [entry]},
            cwd=str(tmp_path),
            subtitle_parts=(),
            session_id="session-id",
            all_entries=[entry],
        )
        await wait_for(
            lambda: isinstance(app.screen, DiffScreen) and app.screen.is_mounted and app.screen._content_ready,
            timeout=10.0,
            pilot=pilot,
            description="diff screen content ready",
        )
        diff_screen = app.screen
        assert isinstance(diff_screen, DiffScreen)

        footer = diff_screen.query_one(ChrysFooter)
        synced: list[None] = []
        monkeypatch.setattr(footer, "sync_bindings", lambda: synced.append(None))

        diff_screen.post_message(events.ScreenResume())
        await pilot.pause()
        assert synced == [None]

        # A resume that only honors a deferred close must not touch the footer.
        synced.clear()
        diff_screen._close_when_current = True
        monkeypatch.setattr(diff_screen, "_close_if_current", lambda: None)
        diff_screen.post_message(events.ScreenResume())
        await pilot.pause()
        assert synced == []
