# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

from browser_use.dialog import MESSAGE_LIMIT, DialogInfo, redact_message


def test_redacts_secret_assignments_and_keeps_surrounding_text() -> None:
    message = redact_message("Login failed: password=hunter2 token: abc123 retry")
    assert "hunter2" not in message
    assert "abc123" not in message
    assert "password=***" in message
    assert "token=***" in message
    assert message.endswith("retry")


def test_redacts_authorization_schemes() -> None:
    message = redact_message("headers Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.payload.sig")
    assert "eyJhbGciOiJIUzI1NiJ9" not in message
    assert "=***" in message
    assert "Bearer" not in message


def test_collapses_control_and_format_characters() -> None:
    assert redact_message("line one\n\tline two\x00\x07") == "line one line two"
    # A bidi override must not reorder what the model or the journal shows.
    assert redact_message("safe\u202egnp.exe") == "safe gnp.exe"


def test_truncates_a_long_message_with_a_visible_marker() -> None:
    message = redact_message("x" * (MESSAGE_LIMIT + 25))
    assert message.startswith("x" * MESSAGE_LIMIT)
    assert "(+25 chars)" in message
    assert len(message) < MESSAGE_LIMIT + 30


def test_a_huge_raw_message_is_bounded_before_masking() -> None:
    message = redact_message("y" * 100_000)
    assert len(message) < MESSAGE_LIMIT + 30


def test_empty_message_stays_empty() -> None:
    assert redact_message(None) == ""


def test_line_and_fields_describe_state_with_a_quoted_message() -> None:
    info = DialogInfo(seq=1, kind="confirm", message='Delete this item? state=accepted reason="x"')
    assert info.pending is True
    assert info.line() == 'confirm: Delete this item? state=accepted reason="x"'
    assert info.fields() == 'dialog=confirm message="Delete this item? state=accepted reason=\\"x\\""'
    info.closed = "dismissed"
    info.reason = "auto-dismissed after 10s (headless)"
    assert info.pending is False
    assert "closed: dismissed" in info.line()
    assert 'reason="auto-dismissed after 10s (headless)"' in info.fields()
