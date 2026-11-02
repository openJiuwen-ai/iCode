# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""RSI service-domain errors (fail closed; English messages for CLI/logs)."""

from __future__ import annotations


class RsiError(Exception):
    """Base RSI error with a stable machine code."""

    code: str = "RSI_ERROR"

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


class RsiBadRequest(RsiError):
    """Caller input failed validation."""

    code = "BAD_REQUEST"


class RsiTaskNotFound(RsiError):
    """No durable task for the given id."""

    code = "TASK_NOT_FOUND"


class RsiNotFound(RsiError):
    """A requested RSI artifact (candidate, export, pointer) is missing."""

    code = "NOT_FOUND"


class RsiTaskStateConflict(RsiError):
    """Status transition refused by the task state machine."""

    code = "TASK_STATE_CONFLICT"


class RsiNotReady(RsiError):
    """Provider or workspace capability is not available yet."""

    code = "NOT_READY"


class RsiDatasetInvalid(RsiError):
    """Dataset file failed schema or content validation."""

    code = "DATASET_INVALID"
