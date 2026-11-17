"""The Python tool with the test hook EB_STAGED_REBUILD_FAIL_AT.

Each step of the comma-separated list fails with OSError(EIO), as in the
binary's -Dtest-ops artifact. An installed eb-staged-rebuild of either build
does not read the variable. tests/fail_matrix.py runs the Python tool as

    python3 tests/fail_cli.py ARGS...
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from eb_staged_rebuild.cli import main  # noqa: E402
from eb_staged_rebuild.faults import FaultInjector  # noqa: E402

FaultInjector.failing = frozenset(os.environ.get("EB_STAGED_REBUILD_FAIL_AT", "").split(","))
raise SystemExit(main())
