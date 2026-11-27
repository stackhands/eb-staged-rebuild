from __future__ import annotations

import os
import tempfile
import unittest

from tests.support import golden, run_cli, scrub


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

            binary = run_cli("retire", "--state-root", state_link, "--module-file", module_file)
            self.assertEqual(binary.returncode, 2, binary.stdout + binary.stderr)
            self.assertEqual(binary.stdout, "")
            self.assertIn("state root is unsafe", binary.stderr)
            with open(module_file, encoding="utf-8") as handle:
                self.assertEqual(handle.read(), module_text)
            self.assertFalse(os.path.isdir(os.path.join(state_real, "runs")))
            golden(
                self,
                "stderr",
                scrub(binary.stderr, LINK=state_link, REAL=state_real, MODULE=module_file, ROOT=root),
            )


if __name__ == "__main__":
    unittest.main()
