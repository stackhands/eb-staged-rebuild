from __future__ import annotations

import os
import tempfile
import unittest

from tests.support import run_cli, run_cli_optional_binary


class CliStateRootLockParityTests(unittest.TestCase):
    def test_gc_and_rollback_report_matching_fifo_and_file_refusals(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            fifo_root = os.path.join(root, "state.fifo")
            os.mkfifo(fifo_root)
            file_component = os.path.join(root, "state.file")
            with open(file_component, "w", encoding="utf-8") as handle:
                handle.write("state root marker\n")
            file_root = os.path.join(file_component, "nested")

            for label, state_root, needle in (
                ("fifo", fifo_root, "File exists"),
                ("regular-file", file_root, "Not a directory"),
            ):
                for args in (("gc",), ("rollback", "run-1")):
                    with self.subTest(kind=label, command=args[0]):
                        py = run_cli(*args, "--state-root", state_root)
                        self.assertEqual(py.returncode, 2, py.stdout + py.stderr)
                        self.assertEqual(py.stdout, "")
                        self.assertIn(needle, py.stderr)
                        self.assertIn(state_root, py.stderr)

                        zig = run_cli_optional_binary(*args, "--state-root", state_root)
                        if zig is not None:
                            self.assertEqual(zig.returncode, 2, zig.stdout + zig.stderr)
                            self.assertEqual(zig.stdout, "")
                            self.assertEqual(zig.stderr, py.stderr)


if __name__ == "__main__":
    unittest.main()
