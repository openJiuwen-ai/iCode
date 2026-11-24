# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""One isolated native clipboard write, authorized by a complete stdin payload."""

from __future__ import annotations

import json
import sys

from chrys.foundation.platform import _clipboard_copy_windows


def main() -> int:
    """Return 2 for transient contention, 1 for other failures, and 0 on success."""
    try:
        sys.stdout.buffer.write(b"R")
        sys.stdout.buffer.flush()
        # EOF without a complete request (including failed/cancelled spawn)
        # must never clear the clipboard. Text never travels in process argv.
        text = json.loads(sys.stdin.buffer.read().decode("utf-8"))
        if not isinstance(text, str):
            return 1
        _clipboard_copy_windows(text)
    except PermissionError:
        return 2
    except Exception:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
