"""The Python tool with the test clock EB_STAGED_REBUILD_NOW.

The installed tool does not read it. The binary reads it only in the
-Dtest-ops artifact and under zig build test. A number of seconds since
the epoch, from 0 through 253402300799, replaces the wall clock for run,
resume and rollback. gc keeps the wall clock.

    python3 tests/now_cli.py ARGS...
"""

import math
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from eb_staged_rebuild.errors import Refused  # noqa: E402

_SPACE = " \t\n\r\v\f"
# 10000-01-01T00:00:00Z. The second before it is 9999-12-31T23:59:59Z.
_LIMIT = 253402300800


def _install() -> None:
    raw = os.environ.get("EB_STAGED_REBUILD_NOW")
    if raw is None or raw.strip(_SPACE) == "":
        return
    text = raw.strip(_SPACE)
    try:
        stamp = float(text)
    except ValueError as exc:
        raise Refused(f"EB_STAGED_REBUILD_NOW is not a number: {raw!r}") from exc
    if not math.isfinite(stamp):
        raise Refused(f"EB_STAGED_REBUILD_NOW is not a finite number: {raw!r}")
    if stamp < 0 or stamp >= _LIMIT:
        raise Refused(f"EB_STAGED_REBUILD_NOW is not a representable timestamp: {raw!r}")
    try:
        time.gmtime(stamp)
    except (OverflowError, OSError, ValueError) as exc:
        raise Refused(f"EB_STAGED_REBUILD_NOW is not a representable timestamp: {raw!r}") from exc
    import eb_staged_rebuild.engine as engine

    engine.current_time = lambda: stamp


try:
    _install()
except Refused as error:
    print(f"eb-staged-rebuild: {error}", file=sys.stderr)
    raise SystemExit(2) from error

from eb_staged_rebuild.cli import main  # noqa: E402

raise SystemExit(main())
