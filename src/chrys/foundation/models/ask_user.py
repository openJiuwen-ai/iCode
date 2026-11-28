# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Ask-user data model shared by service, orchestration, TUI and ACP layers."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, cast

from chrys.foundation.util.unicode_scalars import find_unpaired_surrogate, replace_unpaired_surrogates

if TYPE_CHECKING:
    from collections.abc import Sequence

ASK_USER_EVIDENCE_KEY: Final[str] = "ask_user_evidence"

MAX_ASK_USER_QUESTIONS: Final[int] = 5
MAX_ASK_USER_OPTIONS: Final[int] = 8
MAX_ASK_USER_QUESTION_CHARS: Final[int] = 10_000
MAX_ASK_USER_LABEL_CHARS: Final[int] = 200
MAX_ASK_USER_DESCRIPTION_CHARS: Final[int] = 500
MAX_ASK_USER_HEADER_CHARS: Final[int] = 64

_MAX_WIRE_PAYLOAD_BYTES = 1024 * 1024
_MAX_WIRE_PAYLOAD_DEPTH = 32
_MAX_WIRE_COLLECTION_ITEMS = 4_096
_MAX_WIRE_STRING_CHARS = 256 * 1024
_ASK_USER_INVISIBLE_OPTION_CHARS = dict.fromkeys((0x200B, 0x200C, 0x200D, 0x2060, 0xFEFF))
_REQUEST_INPUT_KEYS = frozenset({"sessionId", "requestId", "questions", "callerName", "_meta"})
_QUESTION_KEYS = frozenset({"question", "header", "options", "multiSelect"})
_OPTION_KEYS = frozenset({"label", "description"})
_RESPONSE_KEYS = frozenset({"answers", "cancelled", "_meta"})
_ANSWER_KEYS = frozenset({"values", "note"})
ASK_USER_META_COLLISION_KEYS: Final[frozenset[str]] = frozenset(
    {
        "answers",
        "cancelled",
        "callerName",
        "questions",
        "requestId",
        "sessionId",
        "session_id",
        "toolCall",
        "tool_call",
        "kwargs",
        "method",
        "params",
    }
)


@dataclass(frozen=True, slots=True)
class AskUserOption:
    """One selectable answer offered to the user."""

    label: str
    description: str = ""


@dataclass(frozen=True, slots=True)
class AskUserQuestion:
    """One question in an ask-user batch."""

    question: str
    header: str = ""
    options: tuple[AskUserOption, ...] = ()
    multi_select: bool = False


@dataclass(frozen=True, slots=True)
class AskUserAnswer:
    """One positional answer to an :class:`AskUserQuestion`."""

    values: tuple[str, ...] = ()
    note: str = ""

    @property
    def answered(self) -> bool:
        """Return whether the user supplied a value or note."""
        return bool(self.values) or bool(self.note)


@dataclass(frozen=True, slots=True)
class Cancelled:
    """Validated wire outcome indicating that no response was provided."""


def normalize_ask_user_option_label(value: str) -> str:
    """Apply the historical ask-user option normalization used by the TUI."""
    return value.translate(_ASK_USER_INVISIBLE_OPTION_CHARS).strip()


def _plain_string(value: object, *, strip: bool = False) -> str | None:
    if not isinstance(value, str):
        return None
    # Model output decoded from JSON escapes can carry lone surrogates; they
    # cannot cross the ACP wire or a strict UTF-8 write, so repair them here,
    # the one place every question and answer string passes through.
    text = replace_unpaired_surrogates(value if type(value) is str else str.__str__(value))
    return text.strip() if strip else text


def _parse_option(raw: object) -> AskUserOption | None:
    if isinstance(raw, str):
        label = normalize_ask_user_option_label(_plain_string(raw) or "")
        return AskUserOption(label=label) if label else None
    if type(raw) is not dict:
        return None
    label_raw = raw.get("label")
    label = normalize_ask_user_option_label(_plain_string(label_raw) or "")
    if not label:
        return None
    description = _plain_string(raw.get("description")) or ""
    return AskUserOption(label=label, description=description)


def _parse_question(raw: object) -> AskUserQuestion | None:
    if type(raw) is not dict:
        return None
    question = _plain_string(raw.get("question"), strip=True)
    if not question:
        return None
    header = _plain_string(raw.get("header")) or ""
    options_raw = raw.get("options", [])
    options: list[AskUserOption] = []
    if type(options_raw) is list:
        for option_raw in options_raw:
            option = _parse_option(option_raw)
            if option is not None:
                options.append(option)
    multi_select = raw.get("multi_select", raw.get("multiSelect", False))
    return AskUserQuestion(
        question=question,
        header=header,
        options=tuple(options),
        multi_select=multi_select if type(multi_select) is bool else False,
    )


def _parse_question_entries(raw_questions: object) -> tuple[AskUserQuestion, ...]:
    if type(raw_questions) is not list:
        return ()
    questions: list[AskUserQuestion] = []
    for entry in raw_questions:
        question = _parse_question(entry)
        if question is not None:
            questions.append(question)
    return tuple(questions)


def parse_ask_user_questions(raw: object) -> tuple[AskUserQuestion, ...]:
    """Tolerantly decode a ``questions`` list or an arguments dict; never raise."""
    try:
        if type(raw) is dict:
            return _parse_question_entries(raw.get("questions"))
        return _parse_question_entries(raw)
    except Exception:
        return ()


def parse_recorded_ask_user_questions(raw: object) -> tuple[AskUserQuestion, ...]:
    """Decode recorded ask-user arguments for transcript replay; never raise.

    Sessions recorded before the ``questions`` schema carry a single top-level
    ``question`` with plain-string ``options``. Only replay reads that shape;
    live tool calls and the ACP wire accept ``questions`` alone.
    """
    questions = parse_ask_user_questions(raw)
    if questions or type(raw) is not dict:
        return questions
    try:
        question = _plain_string(raw.get("question"), strip=True)
        if not question:
            return ()
        return _parse_question_entries([{"question": question, "options": raw.get("options", [])}])
    except Exception:
        return ()


def _repaired_answer(answer: AskUserAnswer) -> AskUserAnswer:
    """Return *answer* with every string passed through the surrogate repair."""
    values = tuple(replace_unpaired_surrogates(value) for value in answer.values)
    note = replace_unpaired_surrogates(answer.note)
    if values == answer.values and note == answer.note:
        return answer
    return AskUserAnswer(values=values, note=note)


def parse_ask_user_answers(raw: object, *, question_count: int) -> tuple[AskUserAnswer, ...]:
    """Tolerantly decode answers, padding or dropping to fixed positional length."""
    count = max(0, question_count)
    answers: list[AskUserAnswer] = []
    if type(raw) is list:
        entries = cast("list[object]", raw)
    elif type(raw) is tuple:
        entries = raw
    else:
        entries = ()
    if entries:
        for entry in entries[:count]:
            if isinstance(entry, AskUserAnswer):
                answers.append(_repaired_answer(entry))
                continue
            if type(entry) is not dict:
                answers.append(AskUserAnswer())
                continue
            values_raw = entry.get("values", [])
            values: list[str] = []
            if type(values_raw) is list:
                value_entries = cast("list[object]", values_raw)
            elif type(values_raw) is tuple:
                value_entries = values_raw
            else:
                value_entries = ()
            if value_entries:
                for value in value_entries:
                    text = _plain_string(value)
                    if text is not None:
                        values.append(text)
            note = _plain_string(entry.get("note")) or ""
            answers.append(AskUserAnswer(values=tuple(values), note=note))
    answers.extend(AskUserAnswer() for _ in range(count - len(answers)))
    return tuple(answers)


def format_ask_user_result(
    questions: tuple[AskUserQuestion, ...],
    answers: tuple[AskUserAnswer, ...],
) -> str:
    """Format a lossless model-facing result for a completed ask-user call."""
    normalized = parse_ask_user_answers(answers, question_count=len(questions))
    if len(questions) == 1 and len(normalized[0].values) == 1 and not normalized[0].note:
        return f"User response: {normalized[0].values[0]}"
    responses: list[dict[str, object]] = []
    for question, answer in zip(questions, normalized, strict=True):
        entry: dict[str, object] = {
            "question": question.question,
            "answers": list(answer.values),
        }
        if answer.note:
            entry["note"] = answer.note
        if not answer.answered:
            entry["unanswered"] = True
        responses.append(entry)
    return json.dumps({"responses": responses}, ensure_ascii=False, indent=2)


def _measure_wire_payload(value: object, *, depth: int = 0) -> tuple[int, int]:
    if depth > _MAX_WIRE_PAYLOAD_DEPTH:
        raise ValueError("ACP payload nesting is too deep.")
    if isinstance(value, str):
        if len(value) > _MAX_WIRE_STRING_CHARS:
            raise ValueError("ACP payload string is too large.")
        if find_unpaired_surrogate(value) >= 0:
            raise ValueError("ACP payload string contains an unpaired surrogate.")
        return len(value.encode("utf-8", errors="surrogatepass")), 1
    if type(value) is dict:
        if len(value) > _MAX_WIRE_COLLECTION_ITEMS:
            raise ValueError("ACP payload object has too many fields.")
        total = 0
        items = 1
        for key, item in value.items():
            if type(key) is not str or len(key) > _MAX_WIRE_STRING_CHARS or find_unpaired_surrogate(key) >= 0:
                raise ValueError("ACP payload has an invalid object key.")
            total += len(key.encode("utf-8", errors="surrogatepass"))
            child_bytes, child_items = _measure_wire_payload(item, depth=depth + 1)
            total += child_bytes
            items += child_items
        return total, items
    if type(value) is list:
        sequence = cast("list[object]", value)
    elif type(value) is tuple:
        sequence = value
    else:
        sequence = None
    if sequence is not None:
        if len(sequence) > _MAX_WIRE_COLLECTION_ITEMS:
            raise ValueError("ACP payload sequence has too many items.")
        total = 0
        items = 1
        for item in sequence:
            child_bytes, child_items = _measure_wire_payload(item, depth=depth + 1)
            total += child_bytes
            items += child_items
        return total, items
    return 16, 1


def _validate_wire_payload_caps(value: object) -> None:
    _total_bytes, total_items = _measure_wire_payload(value)
    if total_items > _MAX_WIRE_COLLECTION_ITEMS:
        raise ValueError("ACP payload has too many items in total.")
    try:
        encoded = json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValueError("ACP payload is not JSON serializable.") from exc
    if len(encoded) > _MAX_WIRE_PAYLOAD_BYTES:
        raise ValueError("ACP retained payload exceeds the byte limit.")


def _validate_meta(meta: object) -> None:
    if type(meta) is not dict or ASK_USER_META_COLLISION_KEYS.intersection(meta):
        raise ValueError("Invalid request-input metadata.")


def _validate_question_caps(questions: tuple[AskUserQuestion, ...]) -> None:
    if not questions or len(questions) > MAX_ASK_USER_QUESTIONS:
        raise ValueError("Invalid request-input question count.")
    for question in questions:
        if not question.question.strip() or len(question.question) > MAX_ASK_USER_QUESTION_CHARS:
            raise ValueError("Invalid request-input question.")
        if len(question.header) > MAX_ASK_USER_HEADER_CHARS:
            raise ValueError("Invalid request-input header.")
        if len(question.options) > MAX_ASK_USER_OPTIONS:
            raise ValueError("Invalid request-input option count.")
        seen: set[str] = set()
        for option in question.options:
            if not option.label or len(option.label) > MAX_ASK_USER_LABEL_CHARS:
                raise ValueError("Invalid request-input option label.")
            if len(option.description) > MAX_ASK_USER_DESCRIPTION_CHARS:
                raise ValueError("Invalid request-input option description.")
            if option.label in seen:
                raise ValueError("Duplicate request-input option label.")
            seen.add(option.label)


def validate_request_input_params(params: object) -> tuple[AskUserQuestion, ...]:
    """Strictly validate a ``chrys/request_input`` payload."""
    _validate_wire_payload_caps(params)
    if type(params) is not dict:
        raise ValueError("Invalid request-input parameters.")
    params_dict = cast("dict[str, object]", params)
    if set(params_dict).difference(_REQUEST_INPUT_KEYS):
        raise ValueError("Invalid request-input parameters.")
    if not {"sessionId", "requestId", "questions"}.issubset(params_dict):
        raise ValueError("Missing request-input parameters.")
    if (
        type(params_dict["sessionId"]) is not str
        or type(params_dict["requestId"]) is not str
        or (
            "callerName" in params_dict
            and params_dict["callerName"] is not None
            and type(params_dict["callerName"]) is not str
        )
    ):
        raise ValueError("Invalid request-input parameter type.")
    if "_meta" in params_dict:
        _validate_meta(params_dict["_meta"])

    raw_questions = params_dict["questions"]
    if type(raw_questions) is not list:
        raise ValueError("Invalid request-input questions.")
    question_objects = cast("list[object]", raw_questions)
    for raw_question in question_objects:
        if type(raw_question) is not dict:
            raise ValueError("Invalid request-input question object.")
        question_dict = cast("dict[str, object]", raw_question)
        if set(question_dict).difference(_QUESTION_KEYS):
            raise ValueError("Invalid request-input question object.")
        if type(question_dict.get("question")) is not str:
            raise ValueError("Invalid request-input question text.")
        if "header" in question_dict and type(question_dict["header"]) is not str:
            raise ValueError("Invalid request-input question header.")
        if "multiSelect" in question_dict and type(question_dict["multiSelect"]) is not bool:
            raise ValueError("Invalid request-input multiSelect.")
        question_options = question_dict.get("options", [])
        if type(question_options) is not list:
            raise ValueError("Invalid request-input question options.")
        for raw_option in cast("list[object]", question_options):
            if type(raw_option) is not dict:
                raise ValueError("Invalid request-input option object.")
            option_dict = cast("dict[str, object]", raw_option)
            if set(option_dict).difference(_OPTION_KEYS):
                raise ValueError("Invalid request-input option object.")
            if type(option_dict.get("label")) is not str:
                raise ValueError("Invalid request-input option label.")
            if "description" in option_dict and type(option_dict["description"]) is not str:
                raise ValueError("Invalid request-input option description.")
    questions = parse_ask_user_questions(question_objects)
    if len(questions) != len(question_objects):
        raise ValueError("Invalid request-input question.")
    _validate_question_caps(questions)
    return questions


def validate_request_input_response(
    payload: object,
    *,
    questions: tuple[AskUserQuestion, ...],
) -> tuple[AskUserAnswer, ...] | Cancelled:
    """Strictly validate a request-input response without partial fallback."""
    try:
        _validate_wire_payload_caps(payload)
    except ValueError:
        return Cancelled()
    if type(payload) is not dict:
        return Cancelled()
    payload_dict = cast("dict[str, object]", payload)
    if set(payload_dict).difference(_RESPONSE_KEYS):
        return Cancelled()
    if "_meta" in payload_dict:
        try:
            _validate_meta(payload_dict["_meta"])
        except ValueError:
            return Cancelled()
    cancelled = payload_dict.get("cancelled", False)
    if type(cancelled) is not bool:
        return Cancelled()
    if cancelled or "answers" not in payload_dict:
        return Cancelled()
    raw_answers = payload_dict["answers"]
    if type(raw_answers) is not list:
        return Cancelled()
    answers: list[AskUserAnswer] = []
    for raw_answer in cast("list[object]", raw_answers):
        if type(raw_answer) is not dict:
            return Cancelled()
        answer_dict = cast("dict[str, object]", raw_answer)
        if set(answer_dict).difference(_ANSWER_KEYS):
            return Cancelled()
        raw_values = answer_dict.get("values", [])
        raw_note = answer_dict.get("note", "")
        if type(raw_values) is not list or any(type(value) is not str for value in raw_values):
            return Cancelled()
        if type(raw_note) is not str:
            return Cancelled()
        answers.append(AskUserAnswer(values=tuple(cast("list[str]", raw_values)), note=raw_note))
    validated = validate_ask_user_answers(tuple(answers), questions=questions)
    return Cancelled() if validated is None else validated


def validate_ask_user_answers(
    answers: Sequence[AskUserAnswer],
    *,
    questions: tuple[AskUserQuestion, ...],
) -> tuple[AskUserAnswer, ...] | None:
    """Normalize one positional answer per question, or return None when any breaks the contract.

    Values are stripped, non-empty and unique; a single-select question takes
    at most one value; several values must all be offered labels; a note only
    accompanies values that are all labels.
    """
    if not isinstance(answers, (list, tuple)) or len(answers) != len(questions):
        return None
    validated: list[AskUserAnswer] = []
    for answer, question in zip(answers, questions, strict=True):
        if (
            not isinstance(answer, AskUserAnswer)
            or not isinstance(answer.values, tuple)
            or not all(isinstance(value, str) for value in answer.values)
            or not isinstance(answer.note, str)
        ):
            return None
        # Lone surrogates are repaired (the ACP wire check already rejected
        # them there); this also joins the pairs a JSON decoder would have
        # joined, so the values encode strictly.
        values = tuple(_plain_string(value, strip=True) or "" for value in answer.values)
        if any(not value for value in values) or len(set(values)) != len(values):
            return None
        labels = {option.label for option in question.options}
        if not question.multi_select and len(values) > 1:
            return None
        if len(values) > 1 and any(value not in labels for value in values):
            return None
        note = _plain_string(answer.note, strip=True) or ""
        if note and (not values or any(value not in labels for value in values)):
            return None
        validated.append(AskUserAnswer(values=values, note=note))
    return tuple(validated)
