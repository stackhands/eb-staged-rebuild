"""probe names the EasyBuild install guide when eb is not on PATH."""

import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class MissingEasybuildTests(unittest.TestCase):
    def test_probe_says_how_to_install_easybuild(self):
        with tempfile.TemporaryDirectory() as directory:
            proc = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "eb_staged_rebuild",
                    "probe",
                    "--path",
                    directory,
                    "--eb",
                    "missing-eb-for-this-test",
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("missing-eb-for-this-test was not found on PATH", proc.stdout)
        self.assertIn("Install EasyBuild and put eb on PATH", proc.stdout)
        self.assertIn("https://docs.easybuild.io/installation/", proc.stdout)
        self.assertIn("pass --eb with its path", proc.stdout)


if __name__ == "__main__":
    unittest.main()
