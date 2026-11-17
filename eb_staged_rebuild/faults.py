"""Named steps at which a test pauses, fails or crashes a command.

The engine calls ``FaultInjector.reach`` with the name of each step before it takes it. A
test passes a ``FaultInjector`` that crashes at one step, or names steps in
``EB_STAGED_REBUILD_FAIL_AT`` and ``EB_STAGED_REBUILD_PAUSE_AT``.
"""

import errno
import os
import time


class InjectedCrash(BaseException):
    """Test-only crash. A BaseException so ``except Exception`` does not swallow it."""

    def __init__(self, step):
        super().__init__(step)
        self.step = step


class FaultInjector:
    """Record each named step and, when asked, raise before that step runs."""

    def __init__(self, fail_at=None):
        self.fail_at = fail_at
        self.reached = []

    def reach(self, step):
        """Record ``step``, and stop there if a test asked to.

        A step equal to ``fail_at`` raises ``InjectedCrash``; one that ``EB_STAGED_REBUILD_FAIL_AT``
        lists raises ``OSError(EIO)``; one that ``EB_STAGED_REBUILD_PAUSE_AT`` names pauses.
        """
        self.reached.append(step)
        if self.fail_at is not None and step == self.fail_at:
            raise InjectedCrash(step)
        # A failed operation at each named step (a comma-separated list), for
        # tests that drive the command line: the same I/O error the Zig test
        # artifact returns there. No filename, so str() is
        # "[Errno 5] injected failure at STEP".
        if step in os.environ.get("EB_STAGED_REBUILD_FAIL_AT", "").split(","):
            raise OSError(errno.EIO, f"injected failure at {step}")
        # A real SIGKILL cannot be injected in-process. Tests set this to
        # pause after the previous step has happened and before this one.
        # A trailing "*" names every step that starts with what precedes it.
        token = os.environ.get("EB_STAGED_REBUILD_PAUSE_AT")
        if token and (token == step or (token.endswith("*") and step.startswith(token[:-1]))):
            ready = os.environ.get("EB_STAGED_REBUILD_PAUSE_READY")
            if ready:
                fd = os.open(ready, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o644)
                os.close(fd)
            time.sleep(float(os.environ.get("EB_STAGED_REBUILD_PAUSE_SECONDS", "30")))
