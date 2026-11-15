# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""ask_user tool — allows the agent to ask the user questions."""

from __future__ import annotations

import json
from typing import Annotated, Any, cast

from pydantic import AliasChoices, BaseModel, BeforeValidator, Field

from chrys.foundation.models.ask_user import MAX_ASK_USER_QUESTIONS, parse_ask_user_questions
from chrys.service.tools.kinds import KIND_ASK_USER, tool

_OPTION_STRING_KEYS = ("label", "value", "content", "text", "title", "name", "description")
_OPTION_NEST_KEYS = ("item", "items", "option", "options", "choices", "children")
_QUESTION_NEST_KEYS = ("item", "items", "question", "questions", "children")
_MAX_OPTION_NORMALIZATION_DEPTH = 8
_MAX_OPTION_NORMALIZATION_VISITS = 10_000
_MAX_STRINGIFIED_OPTIONS_CHARS = 100_000
_OPTION_FALLBACK_SKIP_KEYS = frozenset({"header", "multiselect", "preview", "question", "questions"})


class _NormalizationBudgetExhausted(ValueError):
    """The bounded tolerant normalizer exhausted its work budget."""


type _NormalizationMemo = dict[object, tuple[object, Any]]


def _charge(budget: list[int], amount: int = 1) -> None:
    budget[0] -= amount
    if budget[0] < 0:
        raise _NormalizationBudgetExhausted("ask_user argument normalization budget exhausted")


def _nonempty_string(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = (value if type(value) is str else str.__str__(value)).strip()
    return text or None


def _strip_memoized(value: object, memo: _NormalizationMemo) -> str | None:
    if not isinstance(value, str):
        return None
    key = ("strip", id(value))
    entry = memo.get(key)
    if entry is not None and entry[0] is value:  # type: ignore[index]
        return entry[1]  # type: ignore[index]
    text = _nonempty_string(value)
    memo[key] = (value, text)
    return text


def _decode_stringified_container(text: str, budget: list[int]) -> list[object] | dict[str, object] | None:
    if len(text) > _MAX_STRINGIFIED_OPTIONS_CHARS:
        return None
    if not ((text.startswith("[") and text.endswith("]")) or (text.startswith("{") and text.endswith("}"))):
        return None
    _charge(budget, len(text) // 1024)
    try:
        decoded = json.loads(text)
    except ValueError, RecursionError:
        return None
    return decoded if type(decoded) in {list, dict} else None


def _fallback_key_allowed(key: object) -> bool:
    if not isinstance(key, str):
        return False
    normalized = (key if type(key) is str else str.__str__(key)).replace("_", "").lower()
    return normalized not in _OPTION_FALLBACK_SKIP_KEYS


def _dict_entries(value: dict[object, object], budget: list[int]) -> dict[str, object]:
    entries: dict[str, object] = {}
    for key, item in value.items():
        _charge(budget)
        if isinstance(key, str):
            entries[key if type(key) is str else str.__str__(key)] = item
    return entries


def _flatten_option_params(
    value: object,
    *,
    depth: int,
    budget: list[int],
    memo: _NormalizationMemo,
) -> list[dict[str, str]]:
    _charge(budget)
    if isinstance(value, str):
        key = ("option", id(value), depth)
        entry = memo.get(key)
        if entry is not None and entry[0] is value:  # type: ignore[index]
            cached = entry[1]  # type: ignore[index]
            _charge(budget, len(cached))
            return cached
        text = _strip_memoized(value, memo)
        if not text:
            result: list[dict[str, str]] = []
        else:
            decoded = _decode_stringified_container(text, budget) if depth < _MAX_OPTION_NORMALIZATION_DEPTH else None
            if decoded is None:
                result = [{"label": text}]
            elif decoded == []:
                result = []
            else:
                result = _flatten_option_params(decoded, depth=depth + 1, budget=budget, memo=memo)
                if not result:
                    result = [{"label": text}]
        memo[key] = (value, result)
        return result
    if depth >= _MAX_OPTION_NORMALIZATION_DEPTH:
        return []
    if type(value) is list:
        options: list[dict[str, str]] = []
        for item in value:
            options.extend(_flatten_option_params(item, depth=depth + 1, budget=budget, memo=memo))
        return options
    if type(value) is not dict:
        return []

    entries = _dict_entries(cast("dict[object, object]", value), budget)
    options: list[dict[str, str]] = []
    label = _strip_memoized(entries.get("label"), memo)
    if label:
        decoded = _decode_stringified_container(label, budget) if depth < _MAX_OPTION_NORMALIZATION_DEPTH else None
        expanded = (
            _flatten_option_params(decoded, depth=depth + 1, budget=budget, memo=memo) if decoded is not None else []
        )
        if decoded == []:
            pass
        elif expanded:
            options.extend(expanded)
        else:
            option = {"label": label}
            if description := _strip_memoized(entries.get("description"), memo):
                option["description"] = description
            options.append(option)
    else:
        for key in _OPTION_STRING_KEYS[1:]:
            if text := _strip_memoized(entries.get(key), memo):
                options.extend(_flatten_option_params(text, depth=depth + 1, budget=budget, memo=memo))
                break
    for key in _OPTION_NEST_KEYS:
        if key in entries:
            options.extend(_flatten_option_params(entries[key], depth=depth + 1, budget=budget, memo=memo))
    if options:
        return options
    for key, item in entries.items():
        if _fallback_key_allowed(key) and (text := _strip_memoized(item, memo)):
            return _flatten_option_params(text, depth=depth + 1, budget=budget, memo=memo)
    return []


def _coerce_options_with_budget(
    value: object,
    *,
    budget: list[int],
    memo: _NormalizationMemo,
) -> list[dict[str, str]]:
    if value is None:
        return []
    return _flatten_option_params(value, depth=0, budget=budget, memo=memo)


def _coerce_question_entries(
    value: object,
    *,
    depth: int,
    budget: list[int],
    memo: _NormalizationMemo,
) -> list[dict[str, object]]:
    _charge(budget)
    if isinstance(value, str):
        text = _strip_memoized(value, memo)
        if not text or depth >= _MAX_OPTION_NORMALIZATION_DEPTH:
            return []
        decoded = _decode_stringified_container(text, budget)
        return (
            _coerce_question_entries(decoded, depth=depth + 1, budget=budget, memo=memo) if decoded is not None else []
        )
    if depth >= _MAX_OPTION_NORMALIZATION_DEPTH:
        return []
    if type(value) is list:
        questions: list[dict[str, object]] = []
        for item in value:
            questions.extend(_coerce_question_entries(item, depth=depth + 1, budget=budget, memo=memo))
        return questions
    if type(value) is not dict:
        return []
    entries = _dict_entries(cast("dict[object, object]", value), budget)
    question = _strip_memoized(entries.get("question"), memo)
    if question:
        normalized: dict[str, object] = {"question": question}
        if header := _strip_memoized(entries.get("header"), memo):
            normalized["header"] = header
        if "options" in entries:
            normalized["options"] = _coerce_options_with_budget(entries["options"], budget=budget, memo=memo)
        multi_select = entries.get("multi_select", entries.get("multiSelect"))
        if type(multi_select) is bool:
            normalized["multi_select"] = multi_select
        return [normalized]
    questions: list[dict[str, object]] = []
    for key in _QUESTION_NEST_KEYS:
        if key in entries:
            questions.extend(_coerce_question_entries(entries[key], depth=depth + 1, budget=budget, memo=memo))
    return questions


def _coerce_questions(value: object) -> object:
    if value is None:
        return []
    budget = [_MAX_OPTION_NORMALIZATION_VISITS]
    memo: _NormalizationMemo = {}
    return _coerce_question_entries(value, depth=0, budget=budget, memo=memo)


class AskUserOptionParam(BaseModel):
    """Model-facing option arguments."""

    label: Annotated[
        str,
        Field(description="The display text for this option that the user selects. Concise, 1-5 words."),
    ]
    description: Annotated[
        str,
        Field(description="One short sentence explaining what choosing this option means or implies."),
    ] = ""


class AskUserQuestionParam(BaseModel):
    """Model-facing question arguments."""

    question: Annotated[
        str,
        Field(
            description="The complete question to ask. Clear, specific, ends with a question mark. Markdown is supported."
        ),
    ]
    header: Annotated[
        str,
        Field(
            description='Very short label shown as this question\'s tab chip (max 12 chars), e.g. "Auth method", "Library", "Rollout".'
        ),
    ] = ""
    options: Annotated[
        list[AskUserOptionParam],
        Field(
            description='2-4 distinct choices. Do NOT include an "Other" option - a free-text box is always shown. '
            "Omit entirely for an open-ended question."
        ),
    ] = []
    multi_select: Annotated[
        bool,
        Field(
            validation_alias=AliasChoices("multi_select", "multiSelect"),
            description="Set true when the user may pick several options at once.",
        ),
    ] = False


def _question_param_dict(question: AskUserQuestionParam | dict[str, object]) -> dict[str, object]:
    return question.model_dump() if isinstance(question, AskUserQuestionParam) else question


@tool(kind=KIND_ASK_USER)
async def ask_user(
    questions: Annotated[
        list[AskUserQuestionParam],
        BeforeValidator(_coerce_questions),
        Field(
            json_schema_extra={"minItems": 1, "maxItems": MAX_ASK_USER_QUESTIONS},
            description=f"The questions to ask, 1-{MAX_ASK_USER_QUESTIONS} of them. Ask everything you need in ONE call.",
        ),
    ],
) -> str:
    """Ask for missing context or a user decision that changes the next action.

    Check the conversation, repository, and environment first. Bundle up to 5 questions
    in one call. Put each complete question in question; omit options for free text.
    Do not add an Other option: free text is built in. Put a recommended choice first
    and suffix its label with (Recommended). Use multi_select for compatible choices.
    Unanswered entries are skipped, not approval; state any nonessential assumption
    or continue independent work. Do not ask follow-ups after completing the task,
    or call this tool when the user has asked you to stop asking questions."""
    canonical = parse_ask_user_questions([_question_param_dict(item) for item in questions])
    if not canonical:
        return "[Questions for user]"
    lines = ["[Questions for user]"]
    for index, item in enumerate(canonical, start=1):
        lines.append(f"{index}. {item.question}")
        if item.options:
            lines.append(f"   options: {'; '.join(option.label for option in item.options)}")
    return "\n".join(lines)


__all__ = [
    "AskUserOptionParam",
    "AskUserQuestionParam",
    "ask_user",
]
