# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Deterministic summary of the turns a fold replaces.

A fold made without the model (Phase 3, or a provider without a strategy)
keeps, per turn and in order of worth: the assistant's final answer, every
real user message, the files the turn changed, the tools it called and, of a
turn whose tool history was dropped mid-turn, what its progress note says was
done.  A turn that never got to a final answer has only that note to say how
far it came, so the note then takes the answer's place.  The
summary is extracted, never generated, so the same history always folds into
the same text and a fold costs no model call.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from typing import TYPE_CHECKING

from chrys.foundation.models.history_markers import HistoryMarkerKind, read_last_words_record
from chrys.foundation.models.turns import is_continuation_message
from chrys.foundation.tool_kinds import KIND_FILESYSTEM_WRITE, TOOL_CALL_KIND_METADATA_KEY
from chrys.kernel import GROUP_ANNOTATION_KEY, SUMMARY_OF_GROUP_IDS_KEY, TOOL_CALL_CONTENT_TYPES
from chrys.kernel.exchanges import LiveAccessor, iter_exchanges
from chrys.service.context.tool_content import tool_call_name

if TYPE_CHECKING:
    from chrys.kernel import Message

EMPTY_FOLD_SUMMARY = "Earlier conversation"
"""What a fold with nothing to extract is summarised as."""

_TURN_CHARS_MIN = 600
_TURN_CHARS_MAX = 2000
_TURN_CHARS_PER_WINDOW_TOKEN = 0.005
"""A turn's summary takes about half a percent of the window, in characters."""

_ANSWER_CHARS_MAX = 1500
_USER_CHARS_MAX = 500
_FILES_MAX = 20
_EXCERPT_CHARS_MIN = 20
"""Fewer characters than this say nothing: an excerpt is not cut below it."""

_LIVE = LiveAccessor()

_USER_LABEL = "User: "
_ANSWER_LABEL = "Assistant: "
_FILES_LABEL = "Files changed: "
_TOOLS_LABEL = "Tools: "
_NOTE_LABEL = "Progress note: "
_NOTE_PROGRESS_HEADING = "## progress"


def fold_summary_turn_chars(max_context_tokens: int) -> int:
    """Characters one turn's summary may take; an unknown window gets the largest."""
    if max_context_tokens <= 0:
        return _TURN_CHARS_MAX
    scaled = round(max_context_tokens * _TURN_CHARS_PER_WINDOW_TOKEN)
    return max(_TURN_CHARS_MIN, min(_TURN_CHARS_MAX, scaled))


def build_fold_summary(fold_range: list[Message], *, turn_chars: int = _TURN_CHARS_MAX) -> str:
    """Summarise *fold_range*, one block per turn it holds, each within *turn_chars*."""
    blocks = [block for turn in _split_turns(fold_range) if (block := _summarise_turn(turn, turn_chars))]
    return "\n".join(blocks) if blocks else EMPTY_FOLD_SUMMARY


def first_user_text(messages: list[Message], limit: int) -> str:
    """The first real user message of *messages* on one line, at most *limit* characters."""
    for message in messages:
        if _is_real_user(message) and (text := " ".join((message.text or "").split())):
            return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"
    return ""


def _split_turns(fold_range: list[Message]) -> list[list[Message]]:
    """Cut *fold_range* after each turn marker; a trailing unmarked stretch is a turn too."""
    turns: list[list[Message]] = [[]]
    for message in fold_range:
        if message.additional_properties.get(HistoryMarkerKind.KEY) == HistoryMarkerKind.TURN:
            turns.append([])
            continue
        turns[-1].append(message)
    return [turn for turn in turns if turn]


def _is_marker(message: Message) -> bool:
    return HistoryMarkerKind.KEY in message.additional_properties


def _is_real_user(message: Message) -> bool:
    """A message the user wrote: injections count, the synthetic ``continue`` nudge does not."""
    return message.role == "user" and not _is_marker(message) and not is_continuation_message(message)


def _is_tool_summary(message: Message) -> bool:
    """Whether *message* is the summary compaction left in place of tool calls and their results."""
    annotation = message.additional_properties.get(GROUP_ANNOTATION_KEY)
    return isinstance(annotation, dict) and isinstance(annotation.get(SUMMARY_OF_GROUP_IDS_KEY), list)


def _final_answer(turn: list[Message]) -> str:
    """The assistant's text after the turn's last tool work and user message: where it reports what it did."""
    tool_work = [
        index
        for exchange in iter_exchanges(turn, _LIVE)
        for index in (*exchange.response_indices, *exchange.output_indices)
    ]
    tool_work.extend(index for index, message in enumerate(turn) if _is_tool_summary(message))
    last_tool_work = max(tool_work, default=-1)
    last_user = max((index for index, message in enumerate(turn) if _is_real_user(message)), default=-1)
    start = max(last_tool_work, last_user) + 1
    parts: list[str] = []
    if last_tool_work > last_user and turn[last_tool_work].role == "assistant":
        # A provider that runs a tool itself can answer in the message that
        # holds the call and its result: the text after them is the answer.
        contents = turn[last_tool_work].contents
        after_tools = max((index for index, content in enumerate(contents) if content.type != "text"), default=-1) + 1
        parts.extend(text for content in contents[after_tools:] if (text := (content.text or "").strip()))
    parts.extend(
        text
        for message in turn[start:]
        if message.role == "assistant" and not _is_marker(message)
        for content in message.contents
        if content.type == "text" and (text := (content.text or "").strip())
    )
    return "\n".join(parts)


def _progress_note(turn: list[Message]) -> str:
    """What the turn's progress note says was done: its Progress section, or all of a note without one.

    The note is the one recorded on the turn's carrier message; the todo
    list and the dropped-call manifest sent with it are not part of it.
    """
    for message in reversed(turn):
        if message.role != "user":
            continue
        record = read_last_words_record(message.additional_properties)
        if record is None or not record.note.strip():
            continue
        lines = record.note.strip().splitlines()
        start = next((i for i, line in enumerate(lines) if line.strip().lower() == _NOTE_PROGRESS_HEADING), None)
        if start is None:
            return "\n".join(lines)
        end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
        return "\n".join(lines[start + 1 : end]).strip() or "\n".join(lines)
    return ""


def _changed_files(turn: list[Message]) -> list[str]:
    """Paths the turn's write and edit calls named, first use first."""
    paths: dict[str, None] = {}
    for message in turn:
        for content in message.contents:
            if content.type != "function_call":
                continue
            if content.additional_properties.get(TOOL_CALL_KIND_METADATA_KEY) != KIND_FILESYSTEM_WRITE:
                continue
            # A call the kernel refused for its arguments stays in the transcript, whatever shape they had.
            arguments = content.parse_arguments()
            path = arguments.get("path") if isinstance(arguments, Mapping) else None
            if isinstance(path, str) and path:
                paths.setdefault(path)
    return list(paths)


def _tool_counts(turn: list[Message]) -> Counter[str]:
    return Counter(
        name
        for message in turn
        for content in message.contents
        if content.type in TOOL_CALL_CONTENT_TYPES and (name := tool_call_name(content))
    )


def _clip(text: str, limit: int) -> str:
    """The start of *text*, at most *limit* characters."""
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def _clip_middle(text: str, limit: int) -> str:
    """Both ends of *text* (two thirds head, one third tail): a conclusion is often at the end."""
    if len(text) <= limit:
        return text
    omitted_note = f" … ({len(text) - limit:,} chars omitted) … "
    keep = limit - len(omitted_note)
    if keep < len(omitted_note):
        return _clip(text, limit)
    head = keep * 2 // 3
    tail = keep - head
    return f"{text[:head].rstrip()}{omitted_note}{text[len(text) - tail :].lstrip()}"


def _shares(wants: list[int], room: int) -> list[int]:
    """Split *room* among *wants*: one that wants less than an equal share leaves the rest to the others."""
    shares = [0] * len(wants)
    for position, index in enumerate(sorted(range(len(wants)), key=wants.__getitem__)):
        shares[index] = min(wants[index], max(0, room) // (len(wants) - position))
        room -= shares[index]
    return shares


def _files_line(files: list[str], limit: int) -> str:
    if not files:
        return ""
    shown = files[:_FILES_MAX]
    while len(shown) > 1 and len(_FILES_LABEL) + len(", ".join(shown)) + len(" (+99 more)") > limit:
        shown = shown[:-1]
    more = f" (+{len(files) - len(shown)} more)" if len(shown) < len(files) else ""
    return _clip(f"{_FILES_LABEL}{', '.join(shown)}{more}", limit)


def _tools_line(tools: Counter[str], limit: int) -> str:
    if not tools:
        return ""
    return _clip(_TOOLS_LABEL + ", ".join(f"{name} x{count}" for name, count in sorted(tools.items())), limit)


def _summarise_turn(turn: list[Message], turn_chars: int) -> str:
    """One turn's block within *turn_chars*; room is given by worth, lines are written in reading order."""
    # The file and tool lines are short and say what no excerpt can: they are
    # sized first, and the two long parts share what is left.
    files_line = _files_line(_changed_files(turn), max(60, turn_chars // 4))
    tools_line = _tools_line(_tool_counts(turn), max(40, turn_chars // 8))
    room = turn_chars - sum(len(line) + 1 for line in (files_line, tools_line) if line)

    answer = _final_answer(turn)
    note = _progress_note(turn)
    # A turn without a final answer is told by its progress note instead.
    answer_label = _ANSWER_LABEL
    if not answer and note:
        answer, answer_label, note = note, _NOTE_LABEL, ""
    user_texts = [text for message in turn if _is_real_user(message) and (text := (message.text or "").strip())]
    answer_want = len(answer_label) + min(len(answer), _ANSWER_CHARS_MAX) + 1 if answer else 0
    user_want = sum(len(_USER_LABEL) + min(len(text), _USER_CHARS_MAX) + 1 for text in user_texts)

    # The answer is worth most, but never everything: the request is what
    # gives it meaning, so it keeps two fifths of the room when both are long.
    answer_room = min(answer_want, max(room * 3 // 5, room - user_want))
    answer_line = ""
    if answer and (answer_room >= answer_want or answer_room > len(answer_label) + _EXCERPT_CHARS_MIN):
        answer_line = answer_label + _clip_middle(answer, answer_room - len(answer_label) - 1)
        room -= len(answer_line) + 1

    user_lines: list[str] = []
    shown = min(len(user_texts), max(1, room // (len(_USER_LABEL) + 2 * _EXCERPT_CHARS_MIN)))
    more = f"(+{len(user_texts) - shown} more user messages)" if shown < len(user_texts) else ""
    if more:
        room -= len(more) + 1
    wants = [len(_USER_LABEL) + min(len(text), _USER_CHARS_MAX) + 1 for text in user_texts[:shown]]
    for text, share in zip(user_texts[:shown], _shares(wants, room), strict=True):
        user_lines.append(_USER_LABEL + _clip(text, max(_EXCERPT_CHARS_MIN, share - len(_USER_LABEL) - 1)))
    # Beside a final answer the note is worth least: it gets what the rest left.
    room -= sum(len(line) + 1 for line in user_lines)
    if more:
        user_lines.append(more)
    note_line = ""
    if note and room > len(_NOTE_LABEL) + _EXCERPT_CHARS_MIN:
        note_line = _NOTE_LABEL + _clip(note, room - len(_NOTE_LABEL) - 1)

    return "\n".join(line for line in (*user_lines, answer_line, files_line, tools_line, note_line) if line)
