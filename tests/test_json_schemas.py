"""Command stdout is checked against the JSON Schemas in docs/orgmode/reference/json/."""

import json
import os
import tempfile
import unittest

from eb_staged_rebuild import __version__  # noqa: F401 — TESTMAP maps this module
from tests.schema_support import validate
from tests.support import run_cli_or_binary

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _run_probe():
    return run_cli_or_binary(
        "probe", "--path", REPO, "--json", "--eb", "missing-eb-for-schema-test", cwd=REPO
    )


def _run_status(state_root):
    return run_cli_or_binary("status", "--state-root", state_root, "--json", cwd=REPO)


def _run_gc(state_root):
    return run_cli_or_binary(
        "gc", "--state-root", state_root, "--grace-hours", "0", "--dry-run", "--json", cwd=REPO
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

    def test_gc_json_validates(self):
        with tempfile.TemporaryDirectory() as root:
            live = os.path.join(root, "software", "demo", "1")
            retired = os.path.join(root, "software", "demo", ".1.old")
            os.makedirs(retired)
            run = os.path.join(root, "state", "runs", "demo")
            os.makedirs(run)
            journal = {
                "schema": 2,
                "run_id": "demo",
                "state": "complete",
                "modules_plan": [
                    {
                        "live_software": live,
                        "retired": retired,
                        "published_at": 1,
                    }
                ],
            }
            with open(os.path.join(run, "journal.json"), "w", encoding="utf-8") as handle:
                json.dump(journal, handle)
            completed = _run_gc(os.path.join(root, "state"))
        self.assertEqual(completed.returncode, 0, completed.stderr)
        validate(json.loads(completed.stdout), "gc.schema.json")
        with tempfile.TemporaryDirectory() as state:
            completed = _run_gc(state)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout), {"actions": []})
        validate(json.loads(completed.stdout), "gc.schema.json")


if __name__ == "__main__":
    unittest.main()