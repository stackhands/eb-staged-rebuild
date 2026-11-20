from __future__ import annotations

import os
import tempfile
import unittest

from tests.support import normalize_userns_helper, run_cli, run_cli_optional_binary


class CliPathBytesParityTests(unittest.TestCase):
    def test_run_dry_run_stays_clean_under_strict_utf8_stdout(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            software = os.path.join(root, "software")
            modules = os.path.join(root, "modules")
            os.makedirs(software, exist_ok=True)
            os.makedirs(modules, exist_ok=True)
            bad_state_root = os.fsdecode(os.path.join(root.encode("utf-8"), b"state-\xff"))
            args = (
                "run",
                "--software",
                software,
                "--modules",
                modules,
                "--state-root",
                bad_state_root,
                "--dry-run",
                "--",
                "Demo-1.0.eb",
            )
            strict = {"PYTHONIOENCODING": "utf-8:strict"}
            proc = run_cli(*args, env=strict)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertEqual(proc.stderr, "")
            self.assertNotIn("Traceback", proc.stdout)

            binary = run_cli_optional_binary(*args)
            if binary is not None:
                self.assertEqual(binary.returncode, 0, binary.stdout + binary.stderr)
                self.assertEqual(
                    normalize_userns_helper(proc.stdout),
                    normalize_userns_helper(binary.stdout),
                )
                self.assertEqual(proc.stderr, binary.stderr)


if __name__ == "__main__":
    unittest.main()
