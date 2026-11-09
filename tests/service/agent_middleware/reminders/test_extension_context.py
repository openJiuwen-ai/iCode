# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Extension contexts as the reminder middleware sends them: one catalog per context, asked once per turn."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

from chrys.extensions import CONTEXT_MAX_TOKENS, Extension, TurnContext
from chrys.foundation.i18n.formatting import format_message
from chrys.foundation.models.history_markers import HistoryMarkerKind
from chrys.foundation.text.tokenizer import MixedLanguageTokenizer
from chrys.kernel import Content, Message
from chrys.service.agent_middleware.system_reminder import wrap_system_reminder
from chrys.service.extensions import bind_extension
from tests.service.extensions._support import extension_build
from tests.support.reminder_calls import enrich_call
from tests.support.reminder_stack import reminder_pair

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

    import pytest

    from chrys.service.agent_middleware.system_reminder import SystemReminderMiddleware

_WITHDRAWN = "No longer applies: disregard the earlier version of this context."


def _block(name: str, body: str, extension_id: str = "acme") -> str:
    return wrap_system_reminder(
        f'<extension_context extension="{extension_id}" name="{name}">\n{body}\n</extension_context>'
    )


def _middleware(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *extensions: tuple[str, Extension]
) -> SystemReminderMiddleware:
    """A middleware asking *extensions*' contexts, offering nothing else but its turn line."""
    build = extension_build(tmp_path)
    contexts = [context for ext_id, ext in extensions for context in bind_extension(ext_id, ext, build).contexts]
    middleware, _last_words = reminder_pair(runtime=MagicMock(), extension_contexts=contexts)
    monkeypatch.setattr(middleware.sources.turn_line, "clock", lambda: "turn")
    monkeypatch.setattr(middleware.sources.runtime_env, "snapshot", lambda: None)
    return middleware


def _answering(answers: dict[str, Any]) -> Extension:
    """An extension with one context per key of *answers*, each returning that key's current value."""
    extension = Extension()

    def answering(name: str) -> Callable[[TurnContext], Awaitable[Any]]:
        async def answer(_ctx: TurnContext) -> Any:
            return answers[name]

        return answer

    for name in answers:
        extension.context(name=name)(answering(name))
    return extension


def _user(text: str) -> Message:
    return Message(role="user", contents=[Content.from_text(text)])


def _assistant(text: str) -> Message:
    return Message(role="assistant", contents=[Content.from_text(text)])


def _contexts_sent(message: Message) -> list[str]:
    return [c.text for c in message.contents if c.type == "text" and c.text and "<extension_context" in c.text]


async def _turn(middleware: SystemReminderMiddleware, history: list[Message], prompt: str = "") -> list[Message]:
    middleware.prepare_turn()
    assert await middleware.refresh_extension_context(prompt) == ()
    return await enrich_call(middleware, history)


async def test_context_text_is_sent_once_and_again_only_when_it_changes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    answers = {"ticket": "ACME-1: fix login"}
    middleware = _middleware(monkeypatch, tmp_path, ("acme", _answering(answers)))
    first = _user("first")
    sent = await _turn(middleware, [first])
    assert _contexts_sent(sent[0]) == [_block("ticket", "ACME-1: fix login")]
    record = first.additional_properties[HistoryMarkerKind.SYSTEM_REMINDERS_KEY]
    assert {"kind": "catalog", "name": "extension.acme.ticket"}.items() <= record[-1].items()

    second = _user("second")
    unchanged = await _turn(middleware, [first, _assistant("a"), second])
    # The first turn's copy is rendered again byte for byte; the second turn sends nothing new.
    assert _contexts_sent(unchanged[0]) == _contexts_sent(sent[0])
    assert _contexts_sent(unchanged[2]) == []

    answers["ticket"] = "ACME-2: fix logout"
    third = _user("third")
    changed = await _turn(middleware, [first, _assistant("a"), second, _assistant("b"), third])
    assert _contexts_sent(changed[-1]) == [_block("ticket", "ACME-2: fix logout")]


async def test_a_context_offering_nothing_is_withdrawn_as_is_one_whose_extension_is_gone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    answers: dict[str, Any] = {"ticket": "ACME-1", "branch": "main"}
    middleware = _middleware(monkeypatch, tmp_path, ("acme", _answering(answers)))
    first = _user("first")
    await _turn(middleware, [first])

    answers.update(ticket=None, branch="  \n")
    second = _user("second")
    history = [first, _assistant("a"), second]
    withdrawn = await _turn(middleware, history)
    assert _contexts_sent(withdrawn[-1]) == [_block("ticket", _WITHDRAWN), _block("branch", _WITHDRAWN)]

    # The extension was turned off (or failed to load) since: the agent's new reminders still withdraw it.
    rebuilt = _middleware(monkeypatch, tmp_path)
    third = _user("third")
    after_rebuild = await _turn(rebuilt, [first, _assistant("a"), third])
    assert _contexts_sent(after_rebuild[-1]) == [_block("ticket", _WITHDRAWN), _block("branch", _WITHDRAWN)]


async def test_each_context_is_asked_with_the_turn_s_prompt_and_the_agent_s_folders(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    asked: list[TurnContext] = []
    extension = Extension()

    @extension.context
    async def ticket(ctx: TurnContext) -> str:
        asked.append(ctx)
        return "ACME-1"

    middleware = _middleware(monkeypatch, tmp_path, ("acme", extension))
    await _turn(middleware, [_user("Fix the login bug")], prompt="Fix the login bug")
    (ctx,) = asked
    assert (ctx.prompt, ctx.cwd, ctx.agent_name, ctx.session_id) == ("Fix the login bug", tmp_path, "Coder", "s-1")


async def test_contexts_are_asked_together_and_each_under_its_own_timeout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    released = asyncio.Event()
    extension = Extension()

    @extension.context(timeout=5)
    async def waits(_ctx: TurnContext) -> str:
        await released.wait()
        return "released"

    @extension.context
    async def releases(_ctx: TurnContext) -> str:
        released.set()
        return "releasing"

    @extension.context(timeout=0.05)
    async def hangs(_ctx: TurnContext) -> str:
        await asyncio.Event().wait()
        return "never"

    middleware = _middleware(monkeypatch, tmp_path, ("acme", extension))
    middleware.prepare_turn()
    (warning,) = await middleware.refresh_extension_context("")
    assert (
        warning.message
        == "Agent Coder goes without context hangs from extension acme: it took longer than 0.05 seconds"
    )
    sent = await enrich_call(middleware, [_user("first")])
    assert _contexts_sent(sent[0]) == [_block("waits", "released"), _block("releases", "releasing")]


async def test_a_failing_context_offers_nothing_and_is_reported_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    answers: list[Any] = [RuntimeError("tracker down"), 42]
    extension = Extension()

    @extension.context
    async def ticket(_ctx: TurnContext) -> Any:
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    middleware = _middleware(monkeypatch, tmp_path, ("acme", extension), ("acme_ok", _answering({"note": "ok"})))
    middleware.prepare_turn()
    with caplog.at_level(logging.WARNING):
        (warning,) = await middleware.refresh_extension_context("")
    assert (warning.code, warning.session_id) == ("extension_context_failed", "s-1")
    assert warning.message == "Agent Coder goes without context ticket from extension acme: RuntimeError: tracker down"
    assert format_message(warning.display_message) == warning.message
    assert "Extension context extension.acme.ticket failed for agent Coder" in caplog.text
    sent = await enrich_call(middleware, [_user("first")])
    assert _contexts_sent(sent[0]) == [_block("note", "ok", "acme_ok")]

    # Failing again, now by returning something other than text, is logged but not reported again.
    middleware.prepare_turn()
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        assert await middleware.refresh_extension_context("") == ()
    assert "it returned int, not text" in caplog.text


async def test_long_text_is_cut_to_the_cap_and_a_lone_surrogate_is_made_safe(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    middleware = _middleware(monkeypatch, tmp_path, ("acme", _answering({"log": "x" * 200_000, "path": "a\udcffb"})))
    sent = await _turn(middleware, [_user("first")])
    long, path = _contexts_sent(sent[0])
    body = long.split('name="log">\n', 1)[1].removesuffix("\n</extension_context>\n</system-reminder>")
    assert MixedLanguageTokenizer().count_tokens(body) <= CONTEXT_MAX_TOKENS
    assert "[...truncated ~" in body
    assert path == _block("path", "a\\udcffb")


async def test_a_retry_that_keeps_the_turn_s_reminders_keeps_the_context_text(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    asked: list[str] = []
    extension = Extension()

    @extension.context
    async def ticket(ctx: TurnContext) -> str:
        asked.append(ctx.prompt)
        return f"answer {len(asked)}"

    middleware = _middleware(monkeypatch, tmp_path, ("acme", extension))
    assert await middleware.refresh_extension_context("before any turn") == ()
    middleware.prepare_turn()
    await middleware.refresh_extension_context("first")

    middleware.prepare_turn(preserve_turn_reminders=True)
    await middleware.refresh_extension_context("first", keep=True)
    sent = await enrich_call(middleware, [_user("first")])
    assert asked == ["first"]
    assert _contexts_sent(sent[0]) == [_block("ticket", "answer 1")]

    # After a restart nothing was kept, so the retry asks.
    restarted = _middleware(monkeypatch, tmp_path, ("acme", extension))
    restarted.prepare_turn(preserve_turn_reminders=True)
    await restarted.refresh_extension_context("first", keep=True)
    assert asked == ["first", "first"]
