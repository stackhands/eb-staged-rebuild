from __future__ import annotations

import os
import tempfile
import unittest

from tests.support import run_cli


class CliRetireAlreadyRetiredParityTests(unittest.TestCase):
    def _setup_module(self, root: str) -> tuple[str, str]:
        state_root = os.path.join(root, "state")
        os.makedirs(state_root, exist_ok=True)
        live = os.path.join(root, "software", "pkg", "1")
        os.makedirs(live, exist_ok=True)
        module_file = os.path.join(root, "modules", "all", "pkg", "1.lua")
        os.makedirs(os.path.dirname(module_file), exist_ok=True)
        with open(module_file, "w", encoding="utf-8") as handle:
            handle.write(f'local root = "{live}"\n')
        return state_root, module_file

    def test_retire_refuses_when_module_was_already_retired(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            state_root, module_file = self._setup_module(root)
            first = run_cli("retire", "--state-root", state_root, "--module-file", module_file)
            second = run_cli("retire", "--state-root", state_root, "--module-file", module_file)
            self.assertEqual(first.returncode, 0)
            self.assertEqual(second.returncode, 2)
            self.assertIn("is already retired (retire-", second.stderr)


if __name__ == "__main__":
    unittest.main()
