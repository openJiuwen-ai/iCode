# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Bounded JSON stdin/stdout bridge for local Buddy frontends."""

from __future__ import annotations

import argparse
import contextlib
import json
import sys

from chrys.app.features.buddy.frontend import PROTOCOL, handle
from chrys.foundation.branding import APP_COMMAND
from chrys.orchestration.startup import bootstrap_runtime


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog=f"{APP_COMMAND} buddy", description="Local Buddy JSON bridge (chrys-buddy-v1)"
    )
    parser.add_argument("--json", action="store_true", required=True)
    parser.parse_args(argv)
    try:
        with contextlib.redirect_stdout(sys.stderr):
            bootstrap_runtime(dotenv_override=False, setup_telemetry=False)
            payload = sys.stdin.buffer.read(65537)
            if len(payload) > 65536:
                raise ValueError("Buddy request exceeds 64 KiB")
            result = handle(json.loads(payload.decode("utf-8")))
    except (OSError, ValueError, RecursionError) as error:
        json.dump({"protocol": PROTOCOL, "error": str(error)}, sys.stdout)
        sys.stdout.write("\n")
        return 1
    json.dump(result, sys.stdout)
    sys.stdout.write("\n")
    return 0
