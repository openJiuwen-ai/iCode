# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Completed side questions, independent of the main conversation."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SideQuestionEntry:
    created_at: str
    question: str
    answer: str
