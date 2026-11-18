"""Regression test for unreadable probe directory error shape parity."""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys
import tempfile
import unittest


def _python_probe(path: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return subprocess.run(
        [sys.executable, "-m", "eb_staged_rebuild", "probe", "--json", "--path", path, "--eb", "eb"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _zig_probe(path: str) -> subprocess.CompletedProcess[str]:
    repo = pathlib.Path(__file__).resolve().parents[1]
    zig_bin = repo / "zig-out" / "bin" / "eb-staged-rebuild"
    if not zig_bin.exists():
        raise unittest.SkipTest(f"zig binary not found at {zig_bin}")
    return subprocess.run(
        [str(zig_bin), "probe", "--json", "--path", path, "--eb", "eb"],
        capture_output=True,
        text=True,
        check=False,
    )


class ProbeUnreadablePathErrorShapeTests(unittest.TestCase):
    def test_unreadable_probe_directory_reports_directory_path(self) -> None:
        if os.geteuid() == 0:
            self.skipTest("root can still create children in mode 000")
        with tempfile.TemporaryDirectory() as root:
            denied = os.path.join(root, "denied")
            os.makedirs(denied, exist_ok=True)
            os.chmod(denied, 0)
            try:
                py = _python_probe(denied)
                zig = _zig_probe(denied)
            finally:
                os.chmod(denied, 0o700)

            self.assertEqual(py.returncode, 2, py.stdout + py.stderr)
            self.assertEqual(zig.returncode, 2, zig.stdout + zig.stderr)
            self.assertEqual(py.stdout, "")
            self.assertEqual(zig.stdout, "")
            self.assertEqual(py.stderr, zig.stderr)
            self.assertNotIn(".rename-exchange-", py.stderr)


if __name__ == "__main__":
    unittest.main()
