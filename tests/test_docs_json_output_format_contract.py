from __future__ import annotations

import json
import tempfile
import unittest

from tests.support import run_cli, run_cli_optional_binary


def _assert_sorted_json_stdout(testcase: unittest.TestCase, runner) -> None:
    with tempfile.TemporaryDirectory() as root:
        probe = runner("probe", "--json", "--path", root, "--eb", "missing-eb")
        testcase.assertEqual(probe.returncode, 0, probe.stdout + probe.stderr)
        testcase.assertEqual(probe.stderr, "")
        probe_doc = json.loads(probe.stdout)
        testcase.assertEqual(probe.stdout, json.dumps(probe_doc, indent=2, sort_keys=True) + "\n")

        state_root = f"{root}/state"
        gc = runner("gc", "--state-root", state_root, "--dry-run", "--json")
        testcase.assertEqual(gc.returncode, 0, gc.stdout + gc.stderr)
        testcase.assertEqual(gc.stderr, "")
        gc_doc = json.loads(gc.stdout)
        testcase.assertEqual(gc.stdout, json.dumps(gc_doc, indent=2, sort_keys=True) + "\n")

        status = runner("status", "--state-root", state_root, "--json")
        testcase.assertEqual(status.returncode, 0, status.stdout + status.stderr)
        testcase.assertEqual(status.stderr, "")
        status_doc = json.loads(status.stdout)
        testcase.assertEqual(status.stdout, json.dumps(status_doc, indent=2, sort_keys=True) + "\n")


class JsonOutputFormatDocTests(unittest.TestCase):
    def test_docs_describe_sorted_json_and_trailing_newline(self) -> None:
        with open("docs/orgmode/reference/json-output.org", encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn("sorted keys", text)
        self.assertIn("indented by two spaces", text)
        self.assertIn("single trailing", text)
        self.assertIn("newline.", text)

    def test_python_cli_matches_documented_json_format(self) -> None:
        _assert_sorted_json_stdout(self, run_cli)

    def test_binary_cli_matches_documented_json_format(self) -> None:
        binary = run_cli_optional_binary("--help")
        if binary is None:
            self.skipTest("TOOL_BIN is unset")
        _assert_sorted_json_stdout(self, run_cli_optional_binary)


if __name__ == "__main__":
    unittest.main()
