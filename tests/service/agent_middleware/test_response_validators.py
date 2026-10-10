# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Rule-level tests for DefaultResponseValidator and the interactions between its rules."""

from __future__ import annotations

import re

import pytest

from chrys.foundation.hosted_tools import HostedToolFamily, HostedToolPhase
from chrys.foundation.trajectory.event_types import ValidationReason
from chrys.kernel import (
    ChatResponse,
    Content,
    Message,
)
from chrys.service.agent_middleware.response_validation import (
    ResponseValidationMiddleware,
)
from chrys.service.agent_middleware.validators import (
    CONTENT_FILTERED_REASON,
    HOSTED_EVIDENCE_MISSING_FINAL_TEXT_REASON,
    NO_VISIBLE_OUTPUT_REASON,
    OUTPUT_TRUNCATED_REASON,
    REASONING_EXHAUSTED_OUTPUT_REASON,
    UNNAMED_TOOL_CALL_REASON,
    DefaultResponseValidator,
    RegexRule,
    ValidationResult,
)
from tests.service.agent_middleware._response_validation_fakes import (
    _assistant,
    _assistant_truncated,
    _FakeCallNext,
    _make_context,
    _search_without_final_text,
)

# ---------------------------------------------------------------------------
# DefaultResponseValidator — rule-level tests
# ---------------------------------------------------------------------------


class TestDefaultValidatorRules:
    def test_empty_contents_is_invalid(self) -> None:
        v = DefaultResponseValidator()
        result = v.validate(_assistant([]))
        assert result == ValidationResult.invalid("empty contents")

    def test_text_only_empty_string_is_invalid(self) -> None:
        v = DefaultResponseValidator()
        result = v.validate(_assistant([Content.from_text("")]))
        assert not result.ok
        assert "empty or whitespace" in result.reason

    def test_text_only_whitespace_newlines_is_invalid(self) -> None:
        """contents=[{type=text, text='\\n\\n\\n'}] must be flagged."""
        v = DefaultResponseValidator()
        result = v.validate(_assistant([Content.from_text("\n\n\n")]))
        assert not result.ok
        assert "whitespace" in result.reason

    def test_text_only_spaces_and_tabs_is_invalid(self) -> None:
        v = DefaultResponseValidator()
        result = v.validate(_assistant([Content.from_text("   \t  \n ")]))
        assert not result.ok

    def test_real_text_is_valid(self) -> None:
        v = DefaultResponseValidator()
        result = v.validate(_assistant([Content.from_text("Here is the answer.")]))
        assert result.ok

    def test_tool_call_only_is_valid(self) -> None:
        """A pure tool-calling turn with NO text is perfectly legal."""
        v = DefaultResponseValidator()
        tool_call = Content.from_function_call("call_1", "read_file", arguments={"path": "x"})
        result = v.validate(_assistant([tool_call]))
        assert result.ok

    def test_tool_call_with_empty_text_is_valid(self) -> None:
        """Don't flag empty text when the message has a tool call."""
        v = DefaultResponseValidator()
        tool_call = Content.from_function_call("call_1", "read_file", arguments={"path": "x"})
        result = v.validate(_assistant([Content.from_text(""), tool_call]))
        assert result.ok

    def test_informational_custom_tool_call_only_is_valid(self) -> None:
        """A preserved non-executable Responses custom call is still output."""
        call = Content.from_function_call(
            "call_custom_1",
            "python",
            arguments="print('hi')",
            informational_only=True,
            additional_properties={"item_type": "custom_tool_call"},
        )

        assert DefaultResponseValidator().validate(_assistant([call])).ok

    def test_terminal_hosted_only_response_is_valid(self) -> None:
        """A provider-hosted terminal item is usable output without text."""
        v = DefaultResponseValidator()
        hosted = Content.from_hosted_tool_result(
            "hosted_1",
            tool_name="server_task",
            status="completed",
            provider_phase=HostedToolPhase.TERMINAL,
            provider_status="completed",
            result="done",
        )

        assert v.validate(_assistant([hosted])).ok

    def test_running_hosted_only_response_is_not_terminal_output(self) -> None:
        v = DefaultResponseValidator()
        hosted = Content.from_hosted_tool_call(
            "hosted_1",
            tool_name="server_task",
            status="running",
            provider_phase=HostedToolPhase.START,
            provider_status="running",
        )

        result = v.validate(_assistant([hosted]))

        assert not result.ok

    def test_terminal_search_after_intermediate_text_requires_final_answer(self) -> None:
        result = DefaultResponseValidator().validate(_search_without_final_text())

        assert result == ValidationResult.invalid(
            HOSTED_EVIDENCE_MISSING_FINAL_TEXT_REASON,
            terminal_on_giveup=True,
        )

    @pytest.mark.parametrize(
        "family",
        [HostedToolFamily.SEARCH, HostedToolFamily.FETCH, HostedToolFamily.TOOL_DISCOVERY],
    )
    @pytest.mark.parametrize("content_type", ["call", "result"])
    def test_terminal_evidence_only_hosted_content_requires_final_answer(
        self,
        family: HostedToolFamily,
        content_type: str,
    ) -> None:
        if content_type == "call":
            content = Content.from_hosted_tool_call(
                "evidence_1",
                tool_name="provider_tool",
                hosted_family=family,
                status="completed",
                provider_phase=HostedToolPhase.TERMINAL,
                provider_status="completed",
            )
        else:
            content = Content.from_hosted_tool_result(
                "evidence_1",
                tool_name="provider_tool",
                hosted_family=family,
                status="completed",
                provider_phase=HostedToolPhase.TERMINAL,
                provider_status="completed",
                result={"evidence": "found"},
            )

        assert DefaultResponseValidator().validate(_assistant([content])) == ValidationResult.invalid(
            HOSTED_EVIDENCE_MISSING_FINAL_TEXT_REASON,
            terminal_on_giveup=True,
        )

    def test_terminal_tool_discovery_followed_by_final_text_is_valid(self) -> None:
        discovery = Content.from_hosted_tool_result(
            "discovery_1",
            tool_name="tool_search",
            hosted_family=HostedToolFamily.TOOL_DISCOVERY,
            status="completed",
            provider_phase=HostedToolPhase.TERMINAL,
            provider_status="completed",
            result={"tools": [{"name": "get_weather"}]},
        )

        assert (
            DefaultResponseValidator()
            .validate(_assistant([discovery, Content.from_text("I found the appropriate tool.")]))
            .ok
        )

    def test_terminal_search_followed_by_final_text_is_valid(self) -> None:
        response = _search_without_final_text()
        response.messages[0].contents.append(Content.from_text("Here is the answer."))

        assert DefaultResponseValidator().validate(response).ok

    def test_terminal_search_followed_by_image_result_is_valid(self) -> None:
        response = _search_without_final_text()
        response.messages[0].contents.append(
            Content.from_image_generation_tool_result(
                image_id="image_1",
                outputs=["data:image/png;base64,AA=="],
                provider_phase=HostedToolPhase.TERMINAL,
                provider_status="completed",
            )
        )

        assert DefaultResponseValidator().validate(response).ok

    def test_local_function_call_before_terminal_search_is_valid(self) -> None:
        response = _search_without_final_text()
        response.messages[0].contents[0] = Content.from_function_call(
            "call_1", "read_file", arguments={"path": "README.md"}
        )

        assert DefaultResponseValidator().validate(response).ok

    # -- Leaked tool-call markers -----------------------------------------

    def test_leaked_minimax_tool_call_plain(self) -> None:
        v = DefaultResponseValidator()
        text = 'minimax:tool_call {"name":"read_file"} </minimax:tool_call>'
        result = v.validate(_assistant([Content.from_text(text)]))
        assert not result.ok
        assert "leaked tool-call" in result.reason

    def test_leaked_minimax_tool_call_with_prefix_text(self) -> None:
        """Prefix text should not hide the leak."""
        v = DefaultResponseValidator()
        text = "Sure, I will check the file.\nminimax:tool_call {} </minimax:tool_call>"
        result = v.validate(_assistant([Content.from_text(text)]))
        assert not result.ok

    def test_leaked_minimax_tool_call_with_suffix_text(self) -> None:
        """Suffix text should not hide the leak."""
        v = DefaultResponseValidator()
        text = "minimax:tool_call {} </minimax:tool_call>\nI will do that now."
        result = v.validate(_assistant([Content.from_text(text)]))
        assert not result.ok

    def test_leaked_minimax_tool_call_surrounded(self) -> None:
        v = DefaultResponseValidator()
        text = "Here is my plan.\nminimax:tool_call {} </minimax:tool_call>\nDone."
        result = v.validate(_assistant([Content.from_text(text)]))
        assert not result.ok

    def test_leaked_tool_use_tag(self) -> None:
        v = DefaultResponseValidator()
        text = "<tool_use name='read_file'>...</tool_use>"
        result = v.validate(_assistant([Content.from_text(text)]))
        assert not result.ok

    def test_leaked_function_call_tag(self) -> None:
        v = DefaultResponseValidator()
        text = "<function_call>...</function_call>"
        result = v.validate(_assistant([Content.from_text(text)]))
        assert not result.ok

    def test_word_minimax_in_prose_is_not_flagged(self) -> None:
        """Only the tool-call token patterns are flagged — prose is fine."""
        v = DefaultResponseValidator()
        text = "The MiniMax model was released in 2024."
        result = v.validate(_assistant([Content.from_text(text)]))
        assert result.ok

    def test_empty_response_no_messages(self) -> None:
        """ChatResponse with no messages at all."""
        v = DefaultResponseValidator()
        result = v.validate(ChatResponse(messages=[]))
        assert not result.ok

    def test_length_finish_reason_empty_contents_is_terminal(self) -> None:
        # Empty output + finish_reason="length" → no room to reply; terminal.
        v = DefaultResponseValidator()
        result = v.validate(_assistant_truncated([]))
        assert result == ValidationResult.invalid(OUTPUT_TRUNCATED_REASON, retryable=False)
        assert result.retryable is False

    def test_length_finish_reason_whitespace_text_is_terminal(self) -> None:
        v = DefaultResponseValidator()
        result = v.validate(_assistant_truncated([Content.from_text("\n\n  \t")]))
        assert not result.ok
        assert result.retryable is False
        assert result.reason == OUTPUT_TRUNCATED_REASON

    def test_length_finish_reason_with_real_text_is_valid(self) -> None:
        # A response truncated mid-sentence that still has content is valid —
        # "length" alone must not fabricate a failure (no false positive).
        v = DefaultResponseValidator()
        result = v.validate(_assistant_truncated([Content.from_text("partial answer")]))
        assert result.ok

    def test_length_finish_reason_with_reasoning_text_is_retryable(self) -> None:
        # Reasoning-only output cut off by the output budget means the model
        # over-thought — the generated reasoning proves the input did NOT fill
        # the context window, so a re-roll plausibly recovers.
        # Give-up must still fail through the Error path (nothing visible to
        # return), hence terminal_on_giveup.
        v = DefaultResponseValidator()
        result = v.validate(_assistant_truncated([Content.from_text_reasoning(text="partial thought")]))
        assert result == ValidationResult.invalid(REASONING_EXHAUSTED_OUTPUT_REASON, terminal_on_giveup=True)
        assert result.retryable is True

    def test_length_finish_reason_with_protected_reasoning_is_retryable(self) -> None:
        # Signed/private reasoning is generated content too: the length cutoff
        # came from over-thinking, not a full context window.
        v = DefaultResponseValidator()
        result = v.validate(_assistant_truncated([Content.from_text_reasoning(protected_data="sig-123")]))
        assert result == ValidationResult.invalid(REASONING_EXHAUSTED_OUTPUT_REASON, terminal_on_giveup=True)

    def test_stop_finish_reason_with_reasoning_text_is_retryable(self) -> None:
        # Reasoning-only stop has no user-visible answer, but a fresh sample
        # plausibly produces one — retry instead of failing on first sight.
        # Exhausted retries still raise (terminal_on_giveup)
        # rather than allowing a blank final AgentMessage.
        v = DefaultResponseValidator()
        result = v.validate(_assistant([Content.from_text_reasoning(text="complete thought")]))
        assert result == ValidationResult.invalid(NO_VISIBLE_OUTPUT_REASON, terminal_on_giveup=True)
        assert result.retryable is True

    def test_stop_finish_reason_with_protected_reasoning_is_retryable(self) -> None:
        v = DefaultResponseValidator()
        result = v.validate(_assistant([Content.from_text_reasoning(protected_data="sig-123")]))
        assert result == ValidationResult.invalid(NO_VISIBLE_OUTPUT_REASON, terminal_on_giveup=True)

    def test_reasoning_with_visible_text_is_valid(self) -> None:
        v = DefaultResponseValidator()
        result = v.validate(
            _assistant(
                [
                    Content.from_text_reasoning(text="complete thought"),
                    Content.from_text("visible answer"),
                ]
            )
        )
        assert result.ok


# ---------------------------------------------------------------------------
# Content filter without an answer
# ---------------------------------------------------------------------------


def _filtered(contents: list[Content]) -> ChatResponse:
    return ChatResponse(messages=[Message(role="assistant", contents=contents)], finish_reason="content_filter")


def _terminal_search_result() -> Content:
    return Content.from_search_tool_result(
        "ws_1",
        tool_name="web_search",
        status="completed",
        provider_phase=HostedToolPhase.TERMINAL,
        provider_status="completed",
        result={"query": "Chrys"},
    )


def _image_result() -> Content:
    return Content.from_image_generation_tool_result(
        image_id="image_1",
        outputs=["data:image/png;base64,AA=="],
        provider_phase=HostedToolPhase.TERMINAL,
        provider_status="completed",
    )


_FILTERED = ValidationResult.invalid(CONTENT_FILTERED_REASON, retryable=False)


class TestContentFilteredWithoutAnswer:
    @pytest.mark.parametrize(
        "response",
        [
            pytest.param(ChatResponse(messages=[], finish_reason="content_filter"), id="no_message"),
            pytest.param(_filtered([]), id="empty_contents"),
            pytest.param(_filtered([Content.from_text(" \n ")]), id="whitespace_text"),
            pytest.param(_filtered([Content.from_text_reasoning(text="thinking")]), id="reasoning_only"),
            pytest.param(
                _filtered(
                    [
                        Content.from_hosted_tool_call(
                            "hosted_1",
                            tool_name="server_task",
                            status="running",
                            provider_phase=HostedToolPhase.START,
                            provider_status="running",
                        )
                    ]
                ),
                id="running_hosted_work",
            ),
            pytest.param(_filtered([_terminal_search_result()]), id="evidence_only"),
            pytest.param(
                _filtered([Content.from_text("Checking sources."), _terminal_search_result()]),
                id="text_before_evidence",
            ),
        ],
    )
    def test_is_terminal_and_never_retried(self, response: ChatResponse) -> None:
        result = DefaultResponseValidator().validate(response)

        assert result == _FILTERED
        assert result.code == ValidationReason.CONTENT_FILTERED

    @pytest.mark.parametrize(
        "contents",
        [
            pytest.param([Content.from_text("Part of the answer")], id="visible_text"),
            pytest.param(
                [Content.from_function_call("call_1", "read_file", arguments={"path": "README.md"})],
                id="local_call",
            ),
            pytest.param(
                [
                    Content.from_function_call("call_1", "read_file", arguments={"path": "README.md"}),
                    _terminal_search_result(),
                ],
                id="local_call_before_evidence",
            ),
            pytest.param([_image_result()], id="answer_bearing_hosted_output"),
            pytest.param(
                [_terminal_search_result(), Content.from_text("Here is the answer.")], id="answer_after_evidence"
            ),
        ],
    )
    def test_an_answer_the_filter_cut_short_is_kept(self, contents: list[Content]) -> None:
        assert DefaultResponseValidator().validate(_filtered(contents)).ok

    @pytest.mark.parametrize(
        ("validator", "code"),
        [
            pytest.param(DefaultResponseValidator(), ValidationReason.LEAKED_TOOL_CALL, id="leaked_marker"),
            pytest.param(
                DefaultResponseValidator(
                    disable_leaked_tool_call=True,
                    extra_rules=[RegexRule("no_drafts", re.compile("DRAFT"), "draft text")],
                ),
                ValidationReason.RULE_VIOLATION,
                id="extra_rule",
            ),
        ],
    )
    def test_filtered_text_a_rule_rejects_is_reported_as_filtered(
        self, validator: DefaultResponseValidator, code: ValidationReason
    ) -> None:
        # Retrying it would meet the same filter.
        text = 'DRAFT minimax:tool_call {"name":"read_file"} </minimax:tool_call>'

        filtered = validator.validate(_filtered([Content.from_text(text)]))
        stopped = validator.validate(_assistant([Content.from_text(text)]))

        assert (filtered, filtered.code) == (_FILTERED, ValidationReason.CONTENT_FILTERED)
        assert (stopped.ok, stopped.retryable, stopped.code) == (False, True, code)

    def test_disabled_rules_stay_disabled(self) -> None:
        no_op = DefaultResponseValidator(
            disable_empty_contents=True, disable_whitespace_text=True, disable_leaked_tool_call=True
        )
        whitespace_allowed = DefaultResponseValidator(disable_whitespace_text=True)
        empty_allowed = DefaultResponseValidator(disable_empty_contents=True)

        assert no_op.validate(_filtered([])).ok
        assert whitespace_allowed.validate(_filtered([Content.from_text(" ")])).ok
        assert whitespace_allowed.validate(_filtered([])) == _FILTERED
        # The whitespace rule still reads an empty message as blank.
        assert empty_allowed.validate(_filtered([])) == _FILTERED

    def test_other_finish_reasons_keep_their_rules(self) -> None:
        empty = DefaultResponseValidator().validate(_assistant([]))

        assert empty == ValidationResult.invalid("empty contents")
        assert empty.code == ValidationReason.EMPTY_CONTENTS


# ---------------------------------------------------------------------------
# Tool call without a function name
# ---------------------------------------------------------------------------


def _unnamed_call(name: str = "") -> Content:
    return Content.from_function_call("call_1", name, arguments={"path": "x"})


class TestToolCallWithoutAName:
    @pytest.mark.parametrize(
        "contents",
        [
            pytest.param([_unnamed_call()], id="alone"),
            pytest.param([Content.from_text("Reading it."), _unnamed_call()], id="after_text"),
            pytest.param(
                [Content.from_function_call("call_0", "read_file", arguments={"path": "a"}), _unnamed_call()],
                id="beside_a_named_call",
            ),
        ],
    )
    def test_a_response_with_a_call_that_names_no_function_is_retried_and_fails_when_retries_run_out(
        self, contents: list[Content]
    ) -> None:
        result = DefaultResponseValidator().validate(_assistant(contents))

        assert result == ValidationResult.invalid(UNNAMED_TOOL_CALL_REASON, terminal_on_giveup=True)
        assert result.code == ValidationReason.UNNAMED_TOOL_CALL

    @pytest.mark.parametrize(
        "call",
        [
            pytest.param(
                Content.from_function_call("call_custom_1", "", arguments="x", informational_only=True),
                id="informational",
            ),
            pytest.param(
                Content(
                    "function_call", call_id="hosted_1", name="", provider_hosted=True, provider_status="completed"
                ),
                id="provider_hosted",
            ),
        ],
    )
    def test_a_call_no_tool_loop_runs_needs_no_name(self, call: Content) -> None:
        assert call.provider_hosted or call.informational_only
        result = DefaultResponseValidator().validate(_assistant([call, Content.from_text("Done.")]))

        assert result.ok

    def test_the_content_filter_outranks_a_call_without_a_name(self) -> None:
        assert DefaultResponseValidator().validate(_filtered([_unnamed_call()])) == _FILTERED

    def test_the_rule_can_be_disabled(self) -> None:
        assert DefaultResponseValidator(disable_unnamed_tool_call=True).validate(_assistant([_unnamed_call()])).ok

    async def test_a_named_call_on_the_next_attempt_is_kept(self) -> None:
        named = _assistant([_unnamed_call("read_file")])
        fake = _FakeCallNext([_assistant([_unnamed_call()]), named], stream=False)
        ctx = _make_context(stream=False)
        fake.bind(ctx)

        await ResponseValidationMiddleware(backoff_schedule=[0.0]).process(ctx, fake)

        assert fake.call_count == 2
        assert ctx.result is named


# ---------------------------------------------------------------------------
# Rule-interaction edge cases
# ---------------------------------------------------------------------------


class TestRuleInteractions:
    async def test_multiple_text_fragments_concat_for_whitespace_check(self) -> None:
        """contents=[text='', text='\\n', text='   '] — all empty, must retry."""
        bad = _assistant([Content.from_text(""), Content.from_text("\n"), Content.from_text("   ")])
        good = _assistant([Content.from_text("something")])
        fake = _FakeCallNext([bad, good], stream=False)
        ctx = _make_context(stream=False)
        fake.bind(ctx)

        mw = ResponseValidationMiddleware(backoff_schedule=[0.0])
        await mw.process(ctx, fake)
        assert fake.call_count == 2

    async def test_leaked_marker_in_second_text_fragment(self) -> None:
        """Leak in ANY text fragment flags the response."""
        bad = _assistant(
            [
                Content.from_text("Thinking...\n"),
                Content.from_text("minimax:tool_call {} </minimax:tool_call>"),
            ]
        )
        good = _assistant([Content.from_text("OK")])
        fake = _FakeCallNext([bad, good], stream=False)
        ctx = _make_context(stream=False)
        fake.bind(ctx)

        mw = ResponseValidationMiddleware(backoff_schedule=[0.0])
        await mw.process(ctx, fake)
        assert fake.call_count == 2
