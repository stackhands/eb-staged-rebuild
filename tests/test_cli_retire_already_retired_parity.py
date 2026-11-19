from __future__ import annotations

import os
import tempfile
import unittest

from tests.support import run_cli, run_cli_optional_binary


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

    def _run_twice(self, runner, state_root: str, module_file: str):
        first = runner("retire", "--state-root", state_root, "--module-file", module_file)
        second = runner("retire", "--state-root", state_root, "--module-file", module_file)
        return first, second

    def test_retire_refuses_when_module_was_already_retired(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            state_root, module_file = self._setup_module(root)
            first, second = self._run_twice(run_cli, state_root, module_file)
            self.assertEqual(first.returncode, 0)
            self.assertEqual(second.returncode, 2)
            self.assertIn("is already retired (retire-", second.stderr)

        with tempfile.TemporaryDirectory() as root:
            state_root, module_file = self._setup_module(root)
            def run_binary(*args: str):
                return run_cli_optional_binary(*args)

            probe = run_binary("--help")
            if probe is None:
                return

            first, second = self._run_twice(run_binary, state_root, module_file)
            self.assertIsNotNone(first)
            self.assertIsNotNone(second)
            self.assertEqual(first.returncode, 0)
            self.assertEqual(second.returncode, 2)
            self.assertIn("is already retired (retire-", second.stderr)


if __name__ == "__main__":
    unittest.main()
