"""CLI coverage for rejecting an empty --metrics-file value."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ZIG_BIN = os.path.join(REPO, "zig-out", "bin", "eb-staged-rebuild")


def _run_python(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "eb_staged_rebuild.cli", *args],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )


def _run_binary(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [ZIG_BIN, *args],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )


def _state_fixture(root: str) -> str:
    state = os.path.join(root, "state")
    run_dir = os.path.join(state, "runs", "current")
    os.makedirs(run_dir, exist_ok=True)
    journal = {
        "schema": 2,
        "publication": "directory-swap",
        "run_id": "current",
        "state": "complete",
        "created_at": time.time(),
        "modules_plan": [],
    }
    with open(os.path.join(run_dir, "journal.json"), "w", encoding="utf-8") as handle:
        json.dump(journal, handle)
    return state


class EmptyMetricsFileCliTests(unittest.TestCase):
    def test_gc_refuses_an_empty_metrics_file_path(self) -> None:
        with tempfile.TemporaryDirectory(prefix="gc-metrics-empty-") as tmp:
            state = _state_fixture(tmp)
            args = ("gc", "--state-root", state, "--metrics-file", "")
            py_done = _run_python(*args)
            self.assertEqual(py_done.returncode, 2, py_done.stdout + py_done.stderr)
            self.assertIn("--metrics-file must name a file; it is empty", py_done.stderr)
            if os.path.exists(ZIG_BIN):
                zig_done = _run_binary(*args)
                self.assertEqual(zig_done.returncode, 2, zig_done.stdout + zig_done.stderr)
                self.assertEqual(py_done.stdout, zig_done.stdout)
                self.assertEqual(py_done.stderr, zig_done.stderr)


if __name__ == "__main__":
    unittest.main()
