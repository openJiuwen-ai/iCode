# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Trusted RSI check: build complex_calc and verify complex arithmetic cases."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

# Keep expected answers out of the case prompt / workdir seed.
# Values are (expression, real, imag).
_CASES: tuple[tuple[str, float, float], ...] = (
    # Basics
    ("(1+2i)+(3-4i)", 4.0, -2.0),
    ("(1+2i)*(3-4i)", 11.0, 2.0),
    ("(1+2i)/(1-2i)", -0.6, 0.8),
    ("((1+2i)*3)-(2-i)", 1.0, 7.0),
    ("-(1+2i)", -1.0, -2.0),
    ("(2+0i)*(0+3i)", 0.0, 6.0),
    # Easy to get wrong
    ("i", 0.0, 1.0),
    ("-i", 0.0, -1.0),
    ("i*i", -1.0, 0.0),
    ("1/i", 0.0, -1.0),
    # Precedence without parentheses: 1 + (2i*3) - 4i
    ("1+2i*3-4i", 1.0, 2.0),
    # Nested product
    ("(1+2i)*(1+2i)*(1+2i)", -11.0, -2.0),
    # Mixed fraction then product → integers
    ("((1-2i)/(3+4i))*((5+6i)-(7-8i))", 6.0, -2.0),
    # Integer powers (right-associative ^)
    ("(1+i)^2", 0.0, 2.0),
    ("(1+i)^3", -2.0, 2.0),
    ("i^4", 1.0, 0.0),
    ("(2-i)^(-1)", 0.4, 0.2),
    ("2^3^2", 512.0, 0.0),  # 2^(3^2), not (2^3)^2
)

_INVALID: tuple[str, ...] = (
    "1/0",
    "1/(0+0i)",
    "(1+2i",
    "1+2i)",
    "1++2i",
    "",
)

_RESULT_RE = re.compile(
    r"^(?P<re>-?(?:\d+(?:\.\d+)?|\.\d+))"
    r"(?P<sign>[+-])"
    r"(?P<im>(?:\d+(?:\.\d+)?|\.\d+))i$"
)
_TOL = 1e-9


def _fail(message: str) -> None:
    sys.stderr.write(message + "\n")
    raise SystemExit(1)


def _parse_complex(text: str) -> tuple[float, float]:
    match = _RESULT_RE.fullmatch(text.strip())
    if match is None:
        _fail(f"result {text!r} does not match A+Bi / A-Bi with no spaces")
    real = float(match.group("re"))
    imag = float(match.group("im"))
    if match.group("sign") == "-":
        imag = -imag
    return real, imag


def _close(got: float, want: float) -> bool:
    return abs(got - want) <= _TOL


def _run(command: list[str], *, cwd: Path, timeout: float) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed cargo argv; workdir is the RSI case tree
        command,
        cwd=str(cwd),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )


def main() -> None:
    workdir = Path(os.environ["CHRYS_RSI_WORKDIR"]).resolve()
    if shutil.which("cargo") is None:
        _fail("cargo not found on PATH")
    if not (workdir / "Cargo.toml").is_file():
        _fail("Cargo.toml missing in workdir")

    build = _run(["cargo", "build", "-q"], cwd=workdir, timeout=240)
    if build.returncode != 0:
        detail = (build.stderr or build.stdout or "").strip() or f"exit {build.returncode}"
        _fail(f"cargo build failed: {detail}")

    for expression, want_re, want_im in _CASES:
        result = _run(["cargo", "run", "-q", "--", expression], cwd=workdir, timeout=60)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip() or f"exit {result.returncode}"
            _fail(f"expression {expression!r} failed: {detail}")
        lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        if len(lines) != 1:
            _fail(f"expression {expression!r}: expected one stdout line, got {lines!r}")
        got_re, got_im = _parse_complex(lines[0])
        if not (_close(got_re, want_re) and _close(got_im, want_im)):
            _fail(
                f"expression {expression!r}: expected {want_re}+{want_im}i, "
                f"got {got_re}+{got_im}i (raw {lines[0]!r})"
            )

    for expression in _INVALID:
        result = _run(["cargo", "run", "-q", "--", expression], cwd=workdir, timeout=60)
        if result.returncode == 0:
            _fail(f"invalid expression {expression!r} should exit non-zero")


if __name__ == "__main__":
    main()
