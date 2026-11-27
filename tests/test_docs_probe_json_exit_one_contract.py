from __future__ import annotations

import json
import os
import tempfile
import unittest

from tests.support import run_cli


def _assert_probe_exit_one_contract(testcase: unittest.TestCase, runner) -> None:
    with tempfile.TemporaryDirectory() as root:
        missing = os.path.join(root, "missing")
        refused = runner("probe", "--path", missing, "--no-create", "--json")
        testcase.assertEqual(refused.returncode, 1)
        testcase.assertEqual(refused.stdout, "")
        testcase.assertIn(f"probe path does not exist: {missing}", refused.stderr)

        reported = runner("probe", "--path", root, "--json", "--require-eb", "--eb", "missing-eb")
        testcase.assertEqual(reported.returncode, 1)
        testcase.assertTrue(reported.stdout.endswith("\n"))
        json.loads(reported.stdout)
        testcase.assertIn(
            "probe requires EasyBuild, but it was not found: missing-eb",
            reported.stderr,
        )


class ProbeJsonExitOneContractDocTests(unittest.TestCase):
    def test_docs_describe_probe_exit_one_contract(self) -> None:
        with open("docs/orgmode/reference/json-output.org", encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn("=--no-create= refused a missing =--path=", text)
        self.assertIn("=--require-eb= did not find the =--eb= command", text)

    def test_binary_cli_matches_probe_exit_one_contract(self) -> None:
        _assert_probe_exit_one_contract(self, run_cli)


if __name__ == "__main__":
    unittest.main()
