from __future__ import annotations

import os
import sys
import tempfile
import unittest

from tests.support import run_cli, run_cli_optional_binary


class CliParserErrorShapeParityTests(unittest.TestCase):
    def test_parser_error_shapes_match_python(self) -> None:
        if sys.version_info[:2] != (3, 14):
            self.skipTest("parser stderr contract is pinned to Python 3.14")
        with tempfile.TemporaryDirectory() as tmp:
            state_root = os.path.join(tmp, "state")
            module_file = os.path.join(tmp, "module.lua")
            os.makedirs(state_root, exist_ok=True)
            with open(module_file, "w", encoding="utf-8") as handle:
                handle.write('local root = "/tmp/demo"\n')
            env = {"LANG": "en_US.UTF-8", "COLUMNS": "80"}
            cases = (
                ("--wat", "gc", "--state-root", state_root, "extra"),
                ("--wat", "gc", "--state-root", state_root, "--bogus"),
                ("--wat", "retire"),
            )
            for args in cases:
                with self.subTest(args=args):
                    python = run_cli(*args, env=env)
                    self.assertEqual(python.returncode, 2, python.stdout + python.stderr)
                    self.assertEqual(python.stdout, "")
                    binary = run_cli_optional_binary(*args, env=env)
                    if binary is None:
                        self.skipTest("TOOL_BIN is required for parser parity assertions")
                    self.assertEqual(binary.returncode, python.returncode, binary.stdout + binary.stderr)
                    self.assertEqual(binary.stdout, python.stdout)
                    self.assertEqual(binary.stderr, python.stderr)


if __name__ == "__main__":
    unittest.main()
