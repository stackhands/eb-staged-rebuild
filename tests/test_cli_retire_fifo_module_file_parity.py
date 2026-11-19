from __future__ import annotations

import os
import tempfile
import unittest

from tests.support import run_cli, run_cli_optional_binary


class CliRetireFifoModuleFileParityTests(unittest.TestCase):
    def test_retire_refuses_fifo_module_file(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            state_root = os.path.join(root, "state")
            os.makedirs(state_root, exist_ok=True)
            fifo_path = os.path.join(root, "module.fifo")
            os.mkfifo(fifo_path)

            proc = run_cli("retire", "--state-root", state_root, "--module-file", fifo_path)
            self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
            self.assertEqual(proc.stdout, "")
            self.assertIn("is not a regular file (other)", proc.stderr)
            self.assertTrue(os.path.exists(fifo_path))

            binary = run_cli_optional_binary("retire", "--state-root", state_root, "--module-file", fifo_path)
            if binary is not None:
                self.assertEqual(binary.returncode, 2, binary.stdout + binary.stderr)
                self.assertEqual(binary.stdout, "")
                self.assertEqual(binary.stderr, proc.stderr)
                self.assertTrue(os.path.exists(fifo_path))


if __name__ == "__main__":
    unittest.main()
