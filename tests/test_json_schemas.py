"""Command stdout is checked against the JSON Schemas in docs/orgmode/reference/json/."""

from eb_staged_rebuild import __version__  # noqa: F401 — TESTMAP maps this module

import json
import os
import subprocess
import sys
import tempfile
import unittest

from tests.schema_support import validate

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLI = [os.environ["TOOL_BIN"]] if os.environ.get("TOOL_BIN") else [sys.executable, "-m", "eb_staged_rebuild"]


def _run_probe():
    completed = subprocess.run(
        [*CLI, "probe", "--path", REPO, "--json", "--eb", "missing-eb-for-schema-test"],
        cwd=REPO,
        env={**os.environ, "PYTHONPATH": REPO},
        capture_output=True,
        text=True,
        check=False,
    )
    return completed


def _run_status(state_root):
    return subprocess.run(
        [*CLI, "status", "--state-root", state_root, "--json"],
        cwd=REPO,
        env={**os.environ, "PYTHONPATH": REPO},
        capture_output=True,
        text=True,
        check=False,
    )


class JsonSchemaTests(unittest.TestCase):
    def test_probe_json_validates(self):
        completed = _run_probe()
        self.assertEqual(completed.returncode, 0, completed.stderr)
        validate(json.loads(completed.stdout), "probe.schema.json")

    def test_status_json_validates(self):
        with tempfile.TemporaryDirectory() as root:
            runs = os.path.join(root, "runs", "demo")
            os.makedirs(runs)
            journal = {
                "schema": 2,
                "run_id": "demo",
                "state": "done",
                "backend": "bwrap",
                "message": "published",
                "modules_plan": [
                    {
                        "rel": "x/1",
                        "status": "published",
                        "live_software": "/sw/x/1",
                        "retired": "/sw/x/.1.old",
                    }
                ],
            }
            with open(os.path.join(runs, "journal.json"), "w", encoding="utf-8") as handle:
                json.dump(journal, handle)
            completed = _run_status(root)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        validate(json.loads(completed.stdout), "status.schema.json")

    def test_status_empty_root_validates(self):
        with tempfile.TemporaryDirectory() as root:
            completed = _run_status(root)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        validate(json.loads(completed.stdout), "status.schema.json")


if __name__ == "__main__":
    unittest.main()