# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Streamed text and reasoning runs fold in one pass exactly as repeated ``+`` folds them."""

from __future__ import annotations

import random
from collections.abc import Sequence
from copy import deepcopy
from typing import Any, Literal

import pytest

from chrys.foundation.reasoning_origin import REASONING_ORIGIN_KEY
from chrys.kernel import _types as kernel_private_types
from chrys.kernel.exceptions import AdditionItemMismatch, ContentError
from chrys.kernel.types import OPENAI_OUTPUT_MESSAGE_ENVELOPE_KEY, Content
from tests.support.cpu_guard import cpu_bounded

type _RunType = Literal["text", "text_reasoning"]


def _fold_with_add(contents: list[Content], type_str: _RunType) -> None:
    """The repeated-``+`` fold the one-pass coalescing replaced, run through the live ``Content.__add__``."""
    coalesced: list[Content] = []
    folded: Content | None = None
    for content in contents:
        if content.type == type_str:
            if folded is None:
                folded = deepcopy(content)
            else:
                try:
                    folded += content
                except AdditionItemMismatch:
                    coalesced.append(folded)
                    folded = deepcopy(content)
        else:
            if folded is not None:
                coalesced.append(folded)
            folded = None
            coalesced.append(content)
    if folded is not None:
        coalesced.append(folded)
    contents[:] = coalesced


class _Raw:
    """A provider object: compared by identity, never copied."""

    def __init__(self, label: str) -> None:
        self.label = label

    def __repr__(self) -> str:
        return f"_Raw({self.label})"


def _random_props(rng: random.Random) -> dict[str, Any]:
    props: dict[str, Any] = {}
    for key, values in (
        (OPENAI_OUTPUT_MESSAGE_ENVELOPE_KEY, [None, "msg_1", "msg_2"]),
        ("openai_reasoning_format", [None, "reasoning_content", "reasoning_details"]),
        ("anthropic_redacted_thinking", [None, True]),
        (REASONING_ORIGIN_KEY, [None, {"wire": "anthropic_messages", "endpoint": "a"}]),
        ("reasoning_text", [None, "marker"]),
        ("_chrys_analytics_item_id", ["item_1", "item_2"]),
        ("nested", [{"list": [1, 2]}, {"list": [3]}]),
    ):
        if rng.random() < 0.25:
            props[key] = rng.choice(values)
    return props


def _random_annotations(rng: random.Random) -> Sequence[Any] | None:
    roll = rng.random()
    if roll < 0.6:
        return None
    if roll < 0.7:
        return rng.choice([[], ()])
    citation = {"type": "citation", "url": f"https://example.test/{rng.randrange(3)}"}
    return (citation,) if roll < 0.8 else [citation]


def _random_raw(rng: random.Random, index: int) -> Any:
    roll = rng.random()
    if roll < 0.4:
        return None
    if roll < 0.6:
        return _Raw(f"r{index}")
    if roll < 0.75:
        # Falsy values still count as a raw representation; only None is absent.
        return rng.choice([[], "", 0, (_Raw(f"r{index}t"),)])
    if roll < 0.85:
        return [[_Raw(f"r{index}n")], _Raw(f"r{index}m")]
    return [_Raw(f"r{index}a"), _Raw(f"r{index}b")]


def _random_chunk(rng: random.Random, index: int, *, allow_none_text: bool) -> Content:
    roll = rng.random()
    if roll < 0.15:
        return Content.from_function_call(call_id=f"call_{index}", name="tool", arguments="{}")
    text_choices: list[str | None] = ["", "a", "bc", "多字节"]
    if allow_none_text:
        text_choices.append(None)
    if roll < 0.55:
        return Content(
            "text",
            text=rng.choice(text_choices),
            annotations=_random_annotations(rng),
            additional_properties=_random_props(rng),
            raw_representation=_random_raw(rng, index),
        )
    return Content(
        "text_reasoning",
        id=rng.choice([None, None, "", "rs_1", "rs_2"]),
        text=rng.choice([None, *text_choices]),
        protected_data=rng.choice([None, None, "p1", "p2"]),
        annotations=_random_annotations(rng),
        additional_properties=_random_props(rng),
        raw_representation=_random_raw(rng, index),
    )


def _outcome(contents: list[Content], type_str: _RunType) -> tuple[list[Content] | None, BaseException | None]:
    try:
        kernel_private_types._coalesce_text_content(contents, type_str)
    except ContentError as exc:
        return None, exc
    return contents, None


def _expected_outcome(contents: list[Content], type_str: _RunType) -> tuple[list[Content] | None, BaseException | None]:
    try:
        _fold_with_add(contents, type_str)
    except ContentError as exc:
        return None, exc
    return contents, None


def _assert_same_fold(actual: Content, expected: Content, sources: list[Content]) -> None:
    assert type(actual) is type(expected)
    assert actual == expected
    # ``Content.__eq__`` compares ``to_dict``, which leaves out the raw
    # representation and the order of the property keys.
    assert actual.raw_representation == expected.raw_representation
    assert list(actual.additional_properties) == list(expected.additional_properties)
    if any(actual is source for source in sources):
        assert any(expected is source for source in sources)
    else:
        assert not any(expected is source for source in sources)


@pytest.mark.parametrize("seed", range(8))
@pytest.mark.parametrize("type_str", ["text", "text_reasoning"])
def test_one_pass_fold_matches_repeated_add(seed: int, type_str: _RunType) -> None:
    rng = random.Random(f"{type_str}-{seed}")
    for _ in range(250):
        length = rng.randrange(0, 12)
        allow_none_text = rng.random() < 0.1
        stream: list[Content] = []
        for index in range(length):
            # The same chunk object can appear twice in one stream.
            repeat = stream and rng.random() < 0.1
            stream.append(rng.choice(stream) if repeat else _random_chunk(rng, index, allow_none_text=allow_none_text))
        before = list(stream)
        source_states = [deepcopy(chunk.to_dict(exclude_none=False)) for chunk in stream]

        actual, actual_error = _outcome(list(stream), type_str)
        expected, expected_error = _expected_outcome(list(stream), type_str)

        if expected_error is not None:
            assert actual_error is not None
            assert type(actual_error) is type(expected_error)
            assert str(actual_error) == str(expected_error)
            continue
        assert actual_error is None
        assert actual is not None and expected is not None
        assert len(actual) == len(expected)
        for actual_item, expected_item in zip(actual, expected, strict=True):
            _assert_same_fold(actual_item, expected_item, before)
        # The fold reads its sources without changing them.
        assert [chunk.to_dict(exclude_none=False) for chunk in stream] == source_states


def test_folded_run_does_not_share_the_head_s_mutable_values() -> None:
    head = Content(
        "text",
        text="a",
        annotations=[{"type": "citation", "url": "https://example.test/a"}],
        additional_properties={"nested": {"list": [1]}},
    )
    contents = [head, Content.from_text("b")]

    kernel_private_types._coalesce_text_content(contents, "text")

    head.additional_properties["nested"]["list"].append(2)
    head.annotations[0]["url"] = "https://example.test/changed"  # type: ignore[index]
    folded = contents[0]
    assert folded.text == "ab"
    assert folded.additional_properties["nested"] == {"list": [1]}
    assert folded.annotations == [{"type": "citation", "url": "https://example.test/a"}]


@pytest.mark.parametrize("type_str", ["text", "text_reasoning"])
def test_long_stream_folds_in_linear_time(type_str: _RunType) -> None:
    chunks = [
        Content("text", text="x", raw_representation=_Raw(str(index)))
        if type_str == "text"
        else Content("text_reasoning", text="x", raw_representation=_Raw(str(index)))
        for index in range(40_000)
    ]

    def fold() -> list[Content]:
        contents = list(chunks)
        kernel_private_types._coalesce_text_content(contents, type_str)
        return contents

    folded = cpu_bounded(fold)

    assert len(folded) == 1
    assert folded[0].text == "x" * 40_000
    assert len(folded[0].raw_representation) == 40_000
