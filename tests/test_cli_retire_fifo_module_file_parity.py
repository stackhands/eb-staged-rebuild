from __future__ import annotations

import os
import tempfile
import unittest

from tests.support import golden, run_cli, scrub


class CliRetireFifoModuleFileParityTests(unittest.TestCase):
    def test_retire_refuses_fifo_module_file(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            state_root = os.path.join(root, "state")
            os.makedirs(state_root, exist_ok=True)
            fifo_path = os.path.join(root, "module.fifo")
            os.mkfifo(fifo_path)

            binary = run_cli("retire", "--state-root", state_root, "--module-file", fifo_path)
            self.assertEqual(binary.returncode, 2, binary.stdout + binary.stderr)
            self.assertEqual(binary.stdout, "")
            self.assertIn("is not a regular file (other)", binary.stderr)
            self.assertTrue(os.path.exists(fifo_path))
            golden(self, "stderr", scrub(binary.stderr, STATE=state_root, FIFO=fifo_path, ROOT=root))


if __name__ == "__main__":
    unittest.main()
