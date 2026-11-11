# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""A child that stops itself, for tests of job-control stopped state.

Stopped, a process never ends by itself, and a test run killed outright
(SIGTERM, SIGKILL) runs none of the cleanup under test, so the child would
outlive the run, stopped, until someone kills it. :data:`STOPS_ITSELF` starts
a watchdog first: once any test would have ended the child, it kills the child
if the child is still its parent. The cleanup under test kills the child's
process group, the watchdog with it; where a test kills only the child, the
watchdog finds another parent and leaves.
"""

from __future__ import annotations

_WATCHDOG = (
    "import os, signal, sys, time; time.sleep(30); child = int(sys.argv[1]); "
    "os.getppid() == child and os.kill(child, signal.SIGKILL)"
)

# One line of statements, for ``python -c`` and for a line of a script.
STOPS_ITSELF = (
    "import os, signal, subprocess, sys, time; "
    f"subprocess.Popen([sys.executable, '-c', {_WATCHDOG!r}, str(os.getpid())], "
    "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); "
    "os.kill(os.getpid(), signal.SIGSTOP); time.sleep(30)"
)
