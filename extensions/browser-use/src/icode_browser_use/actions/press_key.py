# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
#
"""``browser_press_key`` - send one real key or key combination."""

from __future__ import annotations

import asyncio
from typing import Any

from ..errors import BadRequest, BrowserError, CDPTimeout, ConnectionClosed, StaleRef
from ..types import ToolBlock, text_block
from ._shared import (
    call_value,
    clear_pending_input_releases,
    eval_value,
    flush_pending_input_releases,
    note_pending_input_release,
    resolve_target,
    send_watching_dialog,
)
from .actionability import (
    amark_dispatched,
    aprepare_action,
    arecord_action,
    evidence_line,
    field_state,
    focus_target,
    is_text_field,
    new_action_id,
    poll_submit_outcome,
    submit_verdict,
    target_changed,
    target_disabled,
    target_facts,
)

NAME = "browser_press_key"

DESCRIPTION = (
    "Send one key press (for example Enter, Tab, Escape, ArrowDown, Ctrl+A, "
    "Shift+Tab or a single character) to the focused element, or focus target "
    "first when it is given. Keys travel as trusted CDP input events, the "
    "focused element and the page are read before and after, and a key that "
    "may submit (Enter) is never sent a second time when the outcome is "
    "unclear. Enter on a single-line text field target (contenteditable or a "
    "text-like input, not a textarea) is judged like browser_fill(submit=true): "
    "succeeded when the field emptied or the page navigated; Shift+Enter only "
    "inserts a line break, and a textarea Enter is a newline."
)

PARAMS: dict[str, Any] = {
    "key": {"type": "string", "required": True},
    "target": {"type": "string", "default": ""},
}

_MODIFIERS = {
    "alt": 1,
    "option": 1,
    "ctrl": 2,
    "control": 2,
    "meta": 4,
    "cmd": 4,
    "command": 4,
    "win": 4,
    "super": 4,
    "shift": 8,
}

_NAMED_KEYS: dict[str, tuple[str, str, int, str | None]] = {
    "enter": ("Enter", "Enter", 13, "\r"),
    "return": ("Enter", "Enter", 13, "\r"),
    "tab": ("Tab", "Tab", 9, None),
    "escape": ("Escape", "Escape", 27, None),
    "esc": ("Escape", "Escape", 27, None),
    "backspace": ("Backspace", "Backspace", 8, None),
    "delete": ("Delete", "Delete", 46, None),
    "del": ("Delete", "Delete", 46, None),
    "space": (" ", "Space", 32, " "),
    "arrowup": ("ArrowUp", "ArrowUp", 38, None),
    "up": ("ArrowUp", "ArrowUp", 38, None),
    "arrowdown": ("ArrowDown", "ArrowDown", 40, None),
    "down": ("ArrowDown", "ArrowDown", 40, None),
    "arrowleft": ("ArrowLeft", "ArrowLeft", 37, None),
    "left": ("ArrowLeft", "ArrowLeft", 37, None),
    "arrowright": ("ArrowRight", "ArrowRight", 39, None),
    "right": ("ArrowRight", "ArrowRight", 39, None),
    "home": ("Home", "Home", 36, None),
    "end": ("End", "End", 35, None),
    "pageup": ("PageUp", "PageUp", 33, None),
    "pagedown": ("PageDown", "PageDown", 34, None),
    "insert": ("Insert", "Insert", 45, None),
    "plus": ("+", "Equal", 187, "+"),
}
_NAMED_KEYS.update({f"f{index}": (f"F{index}", f"F{index}", 111 + index, None) for index in range(1, 13)})

#: ASCII punctuation Chrome identifies by a named physical code.
_PUNCTUATION: dict[str, tuple[str, int]] = {
    "-": ("Minus", 189),
    "=": ("Equal", 187),
    "[": ("BracketLeft", 219),
    "]": ("BracketRight", 221),
    "\\": ("Backslash", 220),
    ";": ("Semicolon", 186),
    "'": ("Quote", 222),
    ",": ("Comma", 188),
    ".": ("Period", 190),
    "/": ("Slash", 191),
    "`": ("Backquote", 192),
}

#: Shifted US-layout symbols -> (unshifted key, physical code, virtual key code).
_SHIFTED_SYMBOLS: dict[str, tuple[str, str, int]] = {
    "!": ("1", "Digit1", 49),
    "@": ("2", "Digit2", 50),
    "#": ("3", "Digit3", 51),
    "$": ("4", "Digit4", 52),
    "%": ("5", "Digit5", 53),
    "^": ("6", "Digit6", 54),
    "&": ("7", "Digit7", 55),
    "*": ("8", "Digit8", 56),
    "(": ("9", "Digit9", 57),
    ")": ("0", "Digit0", 48),
    "_": ("-", "Minus", 189),
    "+": ("=", "Equal", 187),
    "{": ("[", "BracketLeft", 219),
    "}": ("]", "BracketRight", 221),
    "|": ("\\", "Backslash", 220),
    ":": (";", "Semicolon", 186),
    '"': ("'", "Quote", 222),
    "<": (",", "Comma", 188),
    ">": (".", "Period", 190),
    "?": ("/", "Slash", 191),
    "~": ("`", "Backquote", 192),
}
_UNSHIFTED_TO_SHIFTED: dict[str, str] = {unshifted: sym for sym, (unshifted, _, _) in _SHIFTED_SYMBOLS.items()}

#: macOS Chromium editing commands required for keyboard navigation/editing (Playwright mapping).
_MAC_COMMANDS: dict[tuple[int, str], list[str]] = {
    (0, "Backspace"): ["deleteBackward"],
    (0, "Delete"): ["deleteForward"],
    (0, "ArrowLeft"): ["moveLeft"],
    (0, "ArrowRight"): ["moveRight"],
    (0, "ArrowUp"): ["moveUp"],
    (0, "ArrowDown"): ["moveDown"],
    (0, "Home"): ["moveToBeginningOfLine"],
    (0, "End"): ["moveToEndOfLine"],
    (8, "ArrowLeft"): ["moveLeftAndModifySelection"],
    (8, "ArrowRight"): ["moveRightAndModifySelection"],
    (8, "ArrowUp"): ["moveUpAndModifySelection"],
    (8, "ArrowDown"): ["moveDownAndModifySelection"],
    (4, "a"): ["selectAll"],
    (4, "c"): ["copy"],
    (4, "v"): ["paste"],
    (4, "x"): ["cut"],
    (4, "z"): ["undo"],
    (12, "z"): ["redo"],
    (4, "ArrowLeft"): ["moveToBeginningOfLine"],
    (4, "ArrowRight"): ["moveToEndOfLine"],
    (4, "ArrowUp"): ["moveToBeginningOfDocument"],
    (4, "ArrowDown"): ["moveToEndOfDocument"],
    (4, "Backspace"): ["deleteToBeginningOfLine"],
}

_POLL_SECONDS = 0.075
_POLLS = 3
_sleep = asyncio.sleep


def _split_key_spec(text: str) -> list[str]:
    """Split a key spec on ``+`` while supporting ``+`` itself, ``Ctrl++``, and literal space."""
    if text == " ":
        return [" "]
    if text == "+":
        return ["+"]
    if text.endswith("++"):
        head = [part.strip() for part in text[:-2].split("+")]
        return [*head, "+"]
    if text.endswith("+ ") and text[:-2].strip():
        head = [part.strip() for part in text[:-2].split("+")]
        return [*head, " "]
    return [part.strip() for part in text.split("+")]


def parse_key(spec: str) -> tuple[str, str, int | None, str | None, int]:
    """Parse ``Ctrl+A`` / ``Shift+Tab`` / ``Enter`` / ``Shift+1`` / ``!`` / ``Ctrl++`` / ``" "`` into CDP key event fields.

    Returns ``(key, code, virtual_key, text, modifiers)``; *text* is the
    character a no-modifier (or Shift-only) key inserts, or ``None`` for keys
    that insert nothing.
    """
    raw = spec or ""
    if raw == " ":
        text = " "
    elif raw.endswith("+ ") and raw[:-2].strip():
        text = raw.lstrip()
    else:
        text = raw.strip()
    if not text:
        raise BadRequest("key 不能为空")
    parts = _split_key_spec(text)
    if any(not part for part in parts):
        raise BadRequest(f"按键格式非法：{text}（用 Ctrl+A、Shift+Tab、Enter 这样的写法）")
    *prefix, base = parts
    mask = 0
    for part in prefix:
        bit = _MODIFIERS.get(part.lower())
        if bit is None:
            raise BadRequest(f"未知修饰键：{part}（支持 Ctrl/Alt/Shift/Meta）")
        mask |= bit
    named = _NAMED_KEYS.get("space" if base == " " else base.lower())
    if named is not None:
        key, code, virtual_key, inserted = named
    elif len(base) == 1:
        if not base.isascii():
            raise BadRequest(f"只支持 ASCII 单字符按键；{base!r} 请改用 browser_fill 输入")
        key = inserted = base
        code = ""
        virtual_key: int | None = None
        if base.isalpha():
            code = f"Key{base.upper()}"
            virtual_key = ord(base.upper())
            if mask & 0b0111 and not mask & 8:  # Ctrl/Alt/Meta-only letter events report a lowercase key
                key = base.lower()
            elif mask & 8:
                key = base.upper()
            inserted = key
        elif base.isdigit():
            code = f"Digit{base}"
            virtual_key = ord(base)
            if mask & 8 and base in _UNSHIFTED_TO_SHIFTED:
                key = _UNSHIFTED_TO_SHIFTED[base]
                inserted = key
        elif base in _PUNCTUATION:
            code, virtual_key = _PUNCTUATION[base]
            if mask & 8 and base in _UNSHIFTED_TO_SHIFTED:
                key = _UNSHIFTED_TO_SHIFTED[base]
                inserted = key
        elif base in _SHIFTED_SYMBOLS:
            _, code, virtual_key = _SHIFTED_SYMBOLS[base]
            key = base
            inserted = base
    else:
        raise BadRequest(
            f"不支持的按键：{base}（可用 Enter、Tab、Escape、Backspace、Delete、Space、方向键、"
            "Home、End、PageUp、PageDown、F1–F12 或单个字符）"
        )
    if mask & 0b0111:  # Ctrl/Alt/Meta combinations do not insert text
        inserted = None
    return key, code or key, virtual_key, inserted, mask


async def _send_key(
    session: Any,
    key: str,
    code: str,
    virtual_key: int | None,
    inserted: str | None,
    mask: int,
    cdp_session_id: str | None,
) -> None:
    """Emit the keyDown/keyUp pair, with a ``char`` event for plain characters.

    The shape follows the proven ``browser_fill`` paths: plain characters insert
    through ``char`` (so the text is inserted exactly once), Enter carries
    ``text="\\r"`` on keyDown, and combinations only set ``modifiers``.
    """
    await flush_pending_input_releases(session)
    client = session.require_client()
    common: dict[str, Any] = {
        "key": key,
        "code": code,
        "windowsVirtualKeyCode": virtual_key,
        "nativeVirtualKeyCode": virtual_key,
        "modifiers": mask,
    }
    plain_character = bool(inserted) and mask == 0 and inserted.isprintable() and inserted != "\r"
    down: dict[str, Any] = {"type": "keyDown", **common}
    if inserted and not plain_character:
        down["text"] = inserted
        down["unmodifiedText"] = inserted
    if getattr(session, "is_macos", False):
        commands = _MAC_COMMANDS.get((mask, key)) or _MAC_COMMANDS.get((mask, key.lower()))
        if commands:
            down["commands"] = list(commands)
    down = {key: value for key, value in down.items() if value is not None}
    up_event = {key: value for key, value in {"type": "keyUp", **common}.items() if value is not None}
    session_id = cdp_session_id or session.session_id
    note_pending_input_release(session, "Input.dispatchKeyEvent", up_event, session_id)
    await client.send("Input.dispatchKeyEvent", down, session_id=session_id, timeout=session.cfg.cdp_timeout)
    if plain_character:
        char_event = {
            key: value
            for key, value in {"type": "char", **common, "text": inserted, "unmodifiedText": inserted}.items()
            if value is not None
        }
        await client.send("Input.dispatchKeyEvent", char_event, session_id=session_id, timeout=session.cfg.cdp_timeout)
    await client.send(
        "Input.dispatchKeyEvent",
        up_event,
        session_id=session_id,
        timeout=session.cfg.cdp_timeout,
    )
    clear_pending_input_releases(session)


_PAGE_STATE_EXPR = (
    "(()=>{"
    " let a=document.activeElement;"
    " while(a && a.shadowRoot && a.shadowRoot.activeElement){ a=a.shadowRoot.activeElement; }"
    " return {href:location.href, mutations:window.__chrys_mutations||0,"
    "   expanded:document.querySelectorAll('[aria-expanded=true]').length,"
    "   dialogs:document.querySelectorAll('dialog[open],[role=dialog]').length,"
    "   active:(a&&(a.tagName||'').toLowerCase())||'',"
    "   activeId:(a&&(a.id||''))||'',"
    "   activeValue:(a&&a.value!==undefined)?String(a.value):'',"
    "   activeChecked:!!(a&&a.checked),"
    "   activeText:(a?String(a.innerText||a.textContent||'').trim().slice(0,120):''),"
    "   toasts:[...document.querySelectorAll('[role=alert],[role=status],[aria-live]')]"
    "     .map(e=>(e.innerText||'').trim().slice(0,200)).filter(Boolean).slice(-3)};"
    "})()"
)
_SUBMIT_POLL_SECONDS = 0.15
_SUBMIT_POLLS = 6


async def _target_facts(session: Any, object_id: str, cdp_session_id: str | None) -> dict[str, Any]:
    return await target_facts(session, object_id, cdp_session_id, call=call_value)


async def _page_state(session: Any, cdp_session_id: str | None) -> dict[str, Any]:
    value = await eval_value(session, _PAGE_STATE_EXPR, cdp_session_id=cdp_session_id)
    return value if isinstance(value, dict) else {}


def _classify(
    before: dict[str, Any],
    after: dict[str, Any],
    facts_before: dict[str, Any],
    facts_after: dict[str, Any],
    *,
    target_given: bool,
) -> str:
    """Attribute the observed change to this key, never to unrelated page churn.

    ``window.__chrys_mutations`` counts the whole document, so a page's own timer
    moving it is not evidence that the key did anything: with a target the target's
    own facts must change, and with no target the focused element's value/checked/
    text/identity must change. A structural change (dialog, aria-expanded) is
    attributed because a key commonly opens a menu or dialog. Mutations alone are
    reported as ``dom_elsewhere`` - honest but not proof.
    """
    if not before or not after:
        return "unknown"
    if before.get("href") != after.get("href"):
        return "navigation"
    structural = before.get("expanded") != after.get("expanded") or before.get("dialogs") != after.get("dialogs")
    if target_given:
        if target_changed(facts_before, facts_after) or structural:
            return "dom"
    else:
        active_tag = str(after.get("active") or "").lower()
        body_like = active_tag in {"", "body", "html"}
        edited = (
            before.get("activeValue") != after.get("activeValue")
            or before.get("activeChecked") != after.get("activeChecked")
            or (not body_like and before.get("activeText") != after.get("activeText"))
        )
        if edited or structural:
            return "dom"
    if before.get("mutations") != after.get("mutations"):
        return "dom_elsewhere"
    if before.get("active") != after.get("active") or before.get("activeId") != after.get("activeId"):
        return "focus"
    return "none"


async def _blocked_dialog_result(session: Any, key: str, label: str, info: Any, action_id: str) -> ToolBlock:
    """The key was sent, then a dialog opened: the effect is unproven, not a success."""
    text = "\n".join(
        [
            f"已发送按键 {key} 到 {label}",
            info.fields(),
            "对话框出现：页面脚本暂停，按键是否生效未确认（不要重复发送）",
            "status=blocked retryable=false",
            "请先调用 browser_dialog（action=accept 或 dismiss），再 browser_snapshot 复核",
            evidence_line(session, action_id),
        ]
    )
    await arecord_action(session, action_id, NAME, "blocked", text)
    return text_block(text)


async def _settle_submit(
    session: Any,
    key_spec: str,
    label: str,
    object_id: str,
    cdp_session_id: str | None,
    before_field: dict[str, Any],
    before_page: dict[str, Any],
    dialog_seq: int,
    action_id: str,
) -> ToolBlock:
    """Judge a plain Enter in a text field with the same verdict as ``browser_fill(submit=true)``."""
    after_field, after_page, replaced, dialog = await poll_submit_outcome(
        session,
        object_id,
        cdp_session_id,
        before_field,
        before_page,
        dialog_seq,
        budget=_SUBMIT_POLLS,
        poll_interval=_SUBMIT_POLL_SECONDS,
        sleep=_sleep,
        read_field=lambda: field_state(session, object_id, cdp_session_id, call=call_value),
        read_page=lambda: _page_state(session, cdp_session_id),
    )
    if dialog is not None:
        return await _blocked_dialog_result(session, key_spec, label, dialog, action_id)
    verdict, submitted = submit_verdict(before_field, after_field, before_page, after_page, replaced=replaced)
    session.mark_dirty()
    if submitted:
        status = "succeeded"
        session.note_success(session.target_id)
    elif verdict.startswith("提交被拒"):
        status = "failed"
    else:
        status = "uncertain"
    effect = "navigation" if submitted and "跳转" in verdict else ("dom" if submitted else "unknown")
    lines = [
        f"已按键 {key_spec} 到 {label}",
        f"effect={effect}",
        f"status={status}" + ("" if status == "succeeded" else " retryable=false"),
        evidence_line(session, action_id),
        verdict,
    ]
    text = "\n".join(lines)
    await arecord_action(session, action_id, NAME, status, text)
    return text_block(text)


async def run(session: Any, arguments: dict[str, Any]) -> list[ToolBlock]:
    key, code, virtual_key, inserted, mask = parse_key(arguments["key"])
    target = arguments["target"]
    entry = None
    object_id = ""
    cdp_session_id = None
    facts: dict[str, Any] = {}
    if target:
        entry = await resolve_target(session, target)
        object_id = await session.resolve_object_id(entry)
        cdp_session_id = entry.cdp_session_id
        facts = await _target_facts(session, object_id, cdp_session_id)
        if target_disabled(facts):
            action_id = new_action_id()
            text = (
                f"未按键 {entry.token}：目标处于 disabled 状态"
                f"；status=blocked retryable=false\n{evidence_line(session, action_id)}"
            )
            await arecord_action(session, action_id, NAME, "blocked", text)
            return [text_block(text)]

    action_id = new_action_id()
    label = entry.token if entry is not None else "当前焦点"
    # Focusing can fire onfocus handlers, so the action is journalled before it too.
    await aprepare_action(session, action_id, NAME, f"prepared key {arguments['key']} on {label}")
    submit_field = False
    before_field: dict[str, Any] = {}
    if target:
        try:
            focused = await focus_target(session, object_id, cdp_session_id, call=call_value)
        except ConnectionClosed:
            raise
        except (CDPTimeout, BrowserError) as exc:
            focused = {}
            focus_reason = f"focus_failed:{type(exc).__name__}"
        else:
            focus_reason = "" if focused.get("focused") else "unfocusable"
        if focus_reason:
            text = (
                f"未按键 {arguments['key']} 到 {label}：{focus_reason}（未发送任何按键）\n"
                f"status=blocked retryable=false\n"
                f"{evidence_line(session, action_id)}"
            )
            await arecord_action(session, action_id, NAME, "blocked", text)
            return [text_block(text)]
    facts_before: dict[str, Any] = {}
    baseline_ok = True
    try:
        before = await _page_state(session, cdp_session_id)
        if object_id:
            facts_before = await _target_facts(session, object_id, cdp_session_id)
    except ConnectionClosed:
        raise
    except (StaleRef, BrowserError):
        before = {}
        baseline_ok = False
    if entry is None:
        label = f"当前焦点（{before.get('active') or '无'}）"
    if target:
        # A plain Enter in a single-line text field is a submit attempt: judge it like
        # browser_fill(submit=true). A textarea Enter is a newline and takes the generic path.
        submit_field = (
            key == "Enter"
            and mask == 0
            and baseline_ok
            and str(focused.get("tag") or "").lower() != "textarea"
            and is_text_field(focused.get("tag"), focused.get("type"), focused.get("editable"))
        )
        if submit_field:
            try:
                before_field = await field_state(session, object_id, cdp_session_id, call=call_value)
            except ConnectionClosed:
                raise
            except (CDPTimeout, BrowserError):
                before_field = {}
    dialog_seq = session.dialog_seq
    try:
        _, dialog = await send_watching_dialog(
            session, dialog_seq, lambda: _send_key(session, key, code, virtual_key, inserted, mask, cdp_session_id)
        )
    except ConnectionClosed:
        session.mark_dirty()
        raise
    except (CDPTimeout, BrowserError) as exc:
        session.mark_dirty()
        text = (
            f"已发送按键 {arguments['key']} 到 {label}\n"
            f"effect=unknown\nstatus=uncertain retryable=false\n"
            f"reason=dispatch_failed:{type(exc).__name__}；不要重复发送\n"
            f"{evidence_line(session, action_id)}"
        )
        await arecord_action(session, action_id, NAME, "uncertain", text)
        return [text_block(text)]
    session.mark_dirty()
    if dialog is not None:
        await amark_dispatched(
            session, action_id, NAME, f"key {arguments['key']} on {label} is in flight; a dialog opened"
        )
        return [await _blocked_dialog_result(session, arguments["key"], label, dialog, action_id)]
    await amark_dispatched(session, action_id, NAME, f"dispatched key {arguments['key']} on {label}")
    if submit_field:
        block = await _settle_submit(
            session, arguments["key"], label, object_id, cdp_session_id, before_field, before, dialog_seq, action_id
        )
        return [block]
    after: dict[str, Any] = before
    facts_after: dict[str, Any] = {}
    for attempt in range(_POLLS + 1):
        if attempt:
            await _sleep(_POLL_SECONDS)
        try:
            after = await _page_state(session, cdp_session_id)
            if object_id:
                facts_after = await _target_facts(session, object_id, cdp_session_id)
        except ConnectionClosed:
            raise
        except (StaleRef, BrowserError):
            after = {}
            break
        dialog = session.dialog_since(dialog_seq)
        if dialog is not None:
            break
        if _classify(before, after, facts_before, facts_after, target_given=bool(target)) in {"navigation", "dom"}:
            break
    dialog = session.dialog_since(dialog_seq)
    if dialog is not None:
        return [await _blocked_dialog_result(session, arguments["key"], label, dialog, action_id)]
    effect = (
        _classify(before, after, facts_before, facts_after, target_given=bool(target)) if baseline_ok else "unknown"
    )

    session.mark_dirty()
    # Enter may submit; a navigation is the only proof, and a textarea Enter is a newline anyway.
    result_tag = str(facts.get("tag") or "").lower() if target else str(after.get("active") or "").lower()
    submit_key = key == "Enter" and mask == 0 and result_tag != "textarea"
    submitted = submit_key and effect == "navigation"
    status = "succeeded" if effect in {"navigation", "dom", "focus"} else "uncertain"
    if submit_key and not submitted:
        status = "uncertain"
    if status == "succeeded":
        session.note_success(session.target_id)
    lines = [
        f"已按键 {arguments['key']} 到 {label}",
        f"effect={effect}",
        f"status={status}" + ("" if status == "succeeded" else " retryable=false"),
        evidence_line(session, action_id),
    ]
    if key == "Enter" and mask == 0 and result_tag == "textarea":
        lines.append("Enter 在 textarea 中只插入换行；要发送请用 browser_fill(submit=true) 或点击发送按钮")
    if submit_key and not submitted:
        if effect in {"navigation", "dom", "focus"}:
            lines.append("页面/焦点已变化，但提交未确认；不要重复按 Enter，请 browser_snapshot 核对")
        else:
            lines.append("页面未变化：提交未确认，不要重复按 Enter；请 browser_snapshot 核对")
    elif effect == "focus":
        lines.append("仅观察到焦点变化；页面内容未变化，请 browser_snapshot 复核")
    elif effect == "dom_elsewhere":
        lines.append("页面其他区域发生变化，目标本身未确认；请 browser_snapshot 复核，不要重复按键")
    elif effect == "unknown":
        lines.append("结果不可判定（探针失败/连接异常）；不要重复发送，请 snapshot 复核")
    elif effect == "none":
        lines.append("页面未变化；按键可能未被目标处理，或副作用不在页面 DOM 上，请 browser_snapshot 复核")
    text = "\n".join(lines)
    await arecord_action(session, action_id, NAME, status, text)
    return [text_block(text)]
