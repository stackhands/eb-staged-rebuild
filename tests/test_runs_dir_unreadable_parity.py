"""CLI parity when STATE_ROOT/runs exists as an unreadable directory."""

from __future__ import annotations

import os
import tempfile
import unittest

from tests.support import run_cli, run_cli_optional_binary


def _make_fixture(tmp: str) -> tuple[str, str, str]:
    state = os.path.join(tmp, "state")
    runs = os.path.join(state, "runs")
    os.makedirs(runs)
    software = os.path.join(tmp, "software", "demo", "1")
    os.makedirs(software)
    module = os.path.join(tmp, "modules", "all", "demo", "1.lua")
    os.makedirs(os.path.dirname(module))
    with open(module, "w", encoding="utf-8") as handle:
        handle.write(f'local root = "{software}"\n')
    return state, runs, module


class UnreadableRunsDirParityTests(unittest.TestCase):
    def test_python_refuses_unreadable_runs_directory(self) -> None:
        if os.geteuid() == 0:
            self.skipTest("root ignores directory read permissions")
        with tempfile.TemporaryDirectory(prefix="unreadable-runs-py-") as tmp:
            state, runs, module = _make_fixture(tmp)
            expected = f"[Errno 13] Permission denied: '{runs}'"
            cases = (
                ("status", "--state-root", state),
                ("status", "--state-root", state, "--json"),
                ("status", "--state-root", state, "run1"),
                ("gc", "--state-root", state, "--dry-run"),
                ("gc", "--state-root", state),
                ("retire", "--state-root", state, "--module-file", module, "--dry-run"),
            )
            os.chmod(runs, 0)
            try:
                for args in cases:
                    with self.subTest(cmd=args[0], args=args):
                        done = run_cli(*args)
                        self.assertEqual(done.returncode, 2, done.stdout + done.stderr)
                        self.assertEqual(done.stdout, "")
                        self.assertIn(expected, done.stderr)
            finally:
                os.chmod(runs, 0o700)

    def test_python_and_zig_match_on_unreadable_runs_directory(self) -> None:
        if os.geteuid() == 0:
            self.skipTest("root ignores directory read permissions")
        if run_cli_optional_binary("--help") is None:
            self.skipTest("TOOL_BIN is unset")
        with tempfile.TemporaryDirectory(prefix="unreadable-runs-parity-") as tmp:
            state, runs, module = _make_fixture(tmp)
            expected = f"[Errno 13] Permission denied: '{runs}'"
            cases = (
                ("status", "--state-root", state),
                ("status", "--state-root", state, "--json"),
                ("status", "--state-root", state, "run1"),
                ("gc", "--state-root", state, "--dry-run"),
                ("gc", "--state-root", state),
                ("retire", "--state-root", state, "--module-file", module, "--dry-run"),
            )
            os.chmod(runs, 0)
            try:
                for args in cases:
                    with self.subTest(cmd=args[0], args=args):
                        py_done = run_cli(*args)
                        zig_done = run_cli_optional_binary(*args)
                        assert zig_done is not None
                        self.assertEqual(py_done.returncode, 2, py_done.stdout + py_done.stderr)
                        self.assertEqual(zig_done.returncode, 2, zig_done.stdout + zig_done.stderr)
                        self.assertEqual(py_done.stdout, "")
                        self.assertEqual(zig_done.stdout, "")
                        self.assertIn(expected, zig_done.stderr)
                        self.assertEqual(py_done.stderr, zig_done.stderr)
            finally:
                os.chmod(runs, 0o700)

    def test_missing_or_file_runs_path_stays_empty_across_frontends(self) -> None:
        with tempfile.TemporaryDirectory(prefix="non-dir-runs-") as tmp:
            missing_state = os.path.join(tmp, "missing-state")
            os.makedirs(missing_state)
            file_state = os.path.join(tmp, "file-state")
            os.makedirs(file_state)
            with open(os.path.join(file_state, "runs"), "w", encoding="utf-8") as handle:
                handle.write("not a directory\n")
            for state in (missing_state, file_state):
                args = ("status", "--state-root", state)
                py_done = run_cli(*args)
                self.assertEqual(py_done.returncode, 0, py_done.stdout + py_done.stderr)
                self.assertEqual(py_done.stdout, "no runs\n")
                self.assertEqual(py_done.stderr, "")
                zig_done = run_cli_optional_binary(*args)
                if zig_done is not None:
                    self.assertEqual(zig_done.returncode, 0, zig_done.stdout + zig_done.stderr)
                    self.assertEqual(zig_done.stdout, py_done.stdout)
                    self.assertEqual(zig_done.stderr, py_done.stderr)


if __name__ == "__main__":
    unittest.main()
