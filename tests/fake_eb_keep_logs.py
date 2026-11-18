"""Stand-in for ``eb-keep-logs used`` for usage-log guard tests."""

import os
import sys


def _flag(argv, name):
    return argv[argv.index(name) + 1]


def main(argv):
    if len(argv) < 1 or argv[0] != "used":
        print(f"unsupported: {argv}", file=sys.stderr)
        return 2
    prefix = _flag(argv, "--prefix")
    mode = os.environ.get("FAKE_KEEP_LOGS_MODE", "clear")
    if mode == "clear":
        print(f"nothing under {prefix}")
        return 0
    if mode == "in-use":
        print(f"path={prefix}/bin/tool kind=run last=1 count=1")
        return 2
    if mode == "error":
        print("injected failure", file=sys.stderr)
        return 1
    print(f"unknown mode {mode!r}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
