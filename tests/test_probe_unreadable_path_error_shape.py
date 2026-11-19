"""Regression test for unreadable probe directory error shape parity."""

from __future__ import annotations

import os
import tempfile
import unittest

from tests.support import run_cli, run_cli_optional_binary


class ProbeUnreadablePathErrorShapeTests(unittest.TestCase):
    def test_unreadable_probe_directory_reports_directory_path(self) -> None:
        if os.geteuid() == 0:
            self.skipTest("root can still create children in mode 000")
        with tempfile.TemporaryDirectory() as root:
            denied = os.path.join(root, "denied")
            os.makedirs(denied, exist_ok=True)
            os.chmod(denied, 0)
            try:
                py = run_cli("probe", "--json", "--path", denied, "--eb", "eb")
                zig = run_cli_optional_binary("probe", "--json", "--path", denied, "--eb", "eb")
            finally:
                os.chmod(denied, 0o700)

            self.assertEqual(py.returncode, 2, py.stdout + py.stderr)
            self.assertEqual(py.stdout, "")
            self.assertNotIn(".rename-exchange-", py.stderr)
            if zig is not None:
                self.assertEqual(zig.returncode, 2, zig.stdout + zig.stderr)
                self.assertEqual(zig.stdout, "")
                self.assertEqual(py.stderr, zig.stderr)


if __name__ == "__main__":
    unittest.main()
