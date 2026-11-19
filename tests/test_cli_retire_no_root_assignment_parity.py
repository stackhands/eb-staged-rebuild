from __future__ import annotations

import os
import tempfile
import unittest

from tests.support import run_cli, run_cli_optional_binary


class CliRetireNoRootAssignmentParityTests(unittest.TestCase):
    def test_retire_refuses_modulefile_without_root_assignment(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            state_root = os.path.join(root, "state")
            os.makedirs(state_root, exist_ok=True)
            module_file = os.path.join(root, "modules", "all", "pkg", "1.lua")
            os.makedirs(os.path.dirname(module_file), exist_ok=True)
            with open(module_file, "w", encoding="utf-8") as handle:
                handle.write('whatis("no root here")\n')

            proc = run_cli("retire", "--state-root", state_root, "--module-file", module_file, "--dry-run")
            self.assertEqual(proc.returncode, 2)
            self.assertIn("has no root assignment, so its software directory is unknown", proc.stderr)

            binary = run_cli_optional_binary(
                "retire", "--state-root", state_root, "--module-file", module_file, "--dry-run"
            )
            if binary is not None:
                self.assertEqual(binary.returncode, 2)
                self.assertIn("has no root assignment, so its software directory is unknown", binary.stderr)


if __name__ == "__main__":
    unittest.main()
