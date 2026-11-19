from __future__ import annotations

import os
import tempfile
import unittest

from tests.support import run_cli, run_cli_optional_binary


class CliRetireSymlinkedStateRootParityTests(unittest.TestCase):
    def test_retire_refuses_symlinked_state_root_and_keeps_module(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            state_real = os.path.join(root, "state-real")
            os.makedirs(state_real, exist_ok=True)
            state_link = os.path.join(root, "state-link")
            os.symlink(state_real, state_link)

            software = os.path.join(root, "software", "pkg", "1")
            os.makedirs(software, exist_ok=True)
            module_file = os.path.join(root, "modules", "all", "pkg", "1.lua")
            os.makedirs(os.path.dirname(module_file), exist_ok=True)
            module_text = f'local root = "{software}"\n'
            with open(module_file, "w", encoding="utf-8") as handle:
                handle.write(module_text)

            proc = run_cli("retire", "--state-root", state_link, "--module-file", module_file)
            self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
            self.assertEqual(proc.stdout, "")
            self.assertIn("state root is unsafe", proc.stderr)
            with open(module_file, encoding="utf-8") as handle:
                self.assertEqual(handle.read(), module_text)
            self.assertFalse(os.path.isdir(os.path.join(state_real, "runs")))

            binary = run_cli_optional_binary("retire", "--state-root", state_link, "--module-file", module_file)
            if binary is not None:
                self.assertEqual(binary.returncode, 2, binary.stdout + binary.stderr)
                self.assertEqual(binary.stdout, "")
                self.assertEqual(binary.stderr, proc.stderr)
                with open(module_file, encoding="utf-8") as handle:
                    self.assertEqual(handle.read(), module_text)
                self.assertFalse(os.path.isdir(os.path.join(state_real, "runs")))


if __name__ == "__main__":
    unittest.main()
