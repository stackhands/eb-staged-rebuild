"""Parity checks for snapshot behavior on unreadable directories."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest

from tests.support import run_cli_optional_binary


def _python_snapshot(path: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import json,sys\n"
                "from eb_staged_rebuild.livecheck import snapshot\n"
                "try:\n"
                "  print(json.dumps(snapshot(sys.argv[1]), indent=2, sort_keys=True))\n"
                "except OSError as exc:\n"
                "  print(f'eb-staged-rebuild: {exc}', file=sys.stderr)\n"
                "  raise SystemExit(2)\n"
            ),
            path,
        ],
        capture_output=True,
        text=True,
        check=False,
    )


class SnapshotParityUnreadableTests(unittest.TestCase):
    def test_snapshot_refuses_unreadable_subdirectories(self) -> None:
        if os.geteuid() == 0:
            self.skipTest("permission checks are not meaningful as root")

        with tempfile.TemporaryDirectory() as tmp:
            keep = os.path.join(tmp, "keep")
            os.makedirs(keep, exist_ok=True)
            with open(os.path.join(keep, "file.txt"), "w", encoding="utf-8") as handle:
                handle.write("x")

            unreadable = os.path.join(tmp, "deny")
            os.makedirs(unreadable, exist_ok=True)
            os.chmod(unreadable, 0)
            try:
                py = _python_snapshot(tmp)
                zig = run_cli_optional_binary("__snapshot", tmp)
            finally:
                os.chmod(unreadable, 0o700)

            self.assertEqual(py.returncode, 2, py.stdout + py.stderr)
            self.assertEqual(py.stdout, "")
            if zig is not None:
                self.assertEqual(zig.returncode, 2, zig.stdout + zig.stderr)
                self.assertEqual(zig.stdout, "")
                self.assertEqual(py.stderr, zig.stderr)


if __name__ == "__main__":
    unittest.main()
