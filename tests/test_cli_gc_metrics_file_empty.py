"""CLI coverage for rejecting an empty --metrics-file value."""

from __future__ import annotations

import json
import os
import tempfile
import time
import unittest

from tests.support import golden, run_cli, scrub


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
            done = run_cli(*args)
            self.assertEqual(done.returncode, 2, done.stdout + done.stderr)
            self.assertIn("--metrics-file must name a file; it is empty", done.stderr)
            golden(self, "stdout", scrub(done.stdout, STATE=state, TMP=tmp))
            golden(self, "stderr", scrub(done.stderr, STATE=state, TMP=tmp))


if __name__ == "__main__":
    unittest.main()
