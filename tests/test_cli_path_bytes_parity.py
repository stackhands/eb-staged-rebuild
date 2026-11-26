from __future__ import annotations

import os
import tempfile
import unittest

from tests.support import golden, normalize_userns_helper, path_without_eb, run_cli, scrub


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
                "--backend",
                "installpath",
                "--dry-run",
                "--",
                "Demo-1.0.eb",
            )
            binary = run_cli(*args, env={"PATH": path_without_eb()})
            self.assertEqual(binary.returncode, 0, binary.stdout + binary.stderr)
            self.assertEqual(binary.stderr, "")
            self.assertNotIn("Traceback", binary.stdout)
            golden(
                self,
                "stdout",
                scrub(normalize_userns_helper(binary.stdout), STATE=bad_state_root, ROOT=root),
            )


if __name__ == "__main__":
    unittest.main()
