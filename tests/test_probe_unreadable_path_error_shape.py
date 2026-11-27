"""Regression test for the error shape of an unreadable probe directory."""

from __future__ import annotations

import os
import tempfile
import unittest

from tests.support import golden, run_cli, scrub


class ProbeUnreadablePathErrorShapeTests(unittest.TestCase):
    def test_unreadable_probe_directory_reports_directory_path(self) -> None:
        if os.geteuid() == 0:
            self.skipTest("root can still create children in mode 000")
        with tempfile.TemporaryDirectory() as root:
            denied = os.path.join(root, "denied")
            os.makedirs(denied, exist_ok=True)
            os.chmod(denied, 0)
            try:
                zig = run_cli("probe", "--json", "--path", denied, "--eb", "eb")
            finally:
                os.chmod(denied, 0o700)

            self.assertEqual(zig.returncode, 2, zig.stdout + zig.stderr)
            self.assertEqual(zig.stdout, "")
            self.assertNotIn(".rename-exchange-", zig.stderr)
            golden(self, "stderr", scrub(zig.stderr, ROOT=root))


if __name__ == "__main__":
    unittest.main()
