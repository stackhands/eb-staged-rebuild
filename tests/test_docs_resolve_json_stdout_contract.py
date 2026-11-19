from __future__ import annotations

import json
import os
import tempfile
import unittest

from tests.support import run_cli, run_cli_optional_binary


def _assert_resolve_contract(testcase: unittest.TestCase, runner) -> None:
    with tempfile.TemporaryDirectory() as root:
        missing = os.path.join(root, "missing.lua")
        absent = runner("resolve", missing)
        testcase.assertEqual(absent.returncode, 1)
        testcase.assertEqual(absent.stderr, "")
        parsed = json.loads(absent.stdout)
        testcase.assertEqual(parsed["kind"], "missing")

        if os.geteuid() == 0:
            return
        unreadable = os.path.join(root, "unreadable.lua")
        with open(unreadable, "w", encoding="utf-8") as handle:
            handle.write('local root = "/opt/demo"\n')
        os.chmod(unreadable, 0)
        try:
            denied = runner("resolve", unreadable)
        finally:
            os.chmod(unreadable, 0o644)
        testcase.assertEqual(denied.returncode, 2)
        testcase.assertEqual(denied.stdout, "")
        testcase.assertIn("eb-staged-rebuild:", denied.stderr)


class ResolveJsonStdoutDocTests(unittest.TestCase):
    def test_docs_describe_when_resolve_prints_json(self) -> None:
        with open("docs/orgmode/reference/json-output.org", encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn("when the path was read", text)
        self.assertIn("unreadable file", text)

    def test_python_cli_matches_resolve_stdout_contract(self) -> None:
        _assert_resolve_contract(self, run_cli)

    def test_binary_cli_matches_resolve_stdout_contract(self) -> None:
        binary = run_cli_optional_binary("--help")
        if binary is None:
            self.skipTest("TOOL_BIN is unset")
        _assert_resolve_contract(self, run_cli_optional_binary)


if __name__ == "__main__":
    unittest.main()
