"""Snapshot behavior on unreadable directories."""

from __future__ import annotations

import os
import tempfile
import unittest

from tests.support import golden, run_cli, scrub


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
                zig = run_cli("__snapshot", tmp)
            finally:
                os.chmod(unreadable, 0o700)

            self.assertEqual(zig.returncode, 2, zig.stdout + zig.stderr)
            self.assertEqual(zig.stdout, "")
            golden(self, "stderr", scrub(zig.stderr, TMP=tmp))


if __name__ == "__main__":
    unittest.main()
